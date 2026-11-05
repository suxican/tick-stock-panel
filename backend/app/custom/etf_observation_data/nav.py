"""Collect published ETF unit NAVs and cross-check them against Eastmoney fund history."""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import re
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode

import httpx

from app.custom.etf_observation_data.shares import DEFAULT_OUTPUT, QUARTER, SPLIT, SSE, SZSE, _write
from app.services.huichun_calendar import BUNDLED_CALENDAR_PATH, load_market_calendar

EASTMONEY = "https://api.fund.eastmoney.com/f10/lsjz"
EFUNDS = "https://api.efunds.com.cn/xcowch/front/fund/nav"
CHINAAMC = "https://www.chinaamc.com/product/fundLishijingzhi.do"
JSFUND = "https://www.jsfund.cn/Services/cn/jsp/product/NavHistory.jsp"
NFFUND = "https://www.nffund.com/nfwebApi/fund/newValueList"
CSRC = "http://eid.csrc.gov.cn/fund/disclose"
SSE_PCF = "https://query.sse.com.cn/commonQuery.do"

# Both managers put the valuation date first and the unadjusted unit NAV second.
TABLE_ROW = re.compile(
    rb"<td>\s*(\d{4}-\d{2}-\d{2})\s*</td>\s*<td>\s*(?:<span>)?(\d+\.\d+)",
    re.IGNORECASE,
)


class NavSource:
    def __init__(self, client: httpx.Client, raw: Path, refresh: bool):
        self.client = client
        self.raw = raw
        self.refresh = refresh
        self.pending: dict[Path, bytes] = {}

    def fetch(self, name, url, **kwargs) -> bytes:
        path = self.raw / f"{name}.gz"
        if path.exists() and not self.refresh:
            return gzip.decompress(path.read_bytes())
        method = "POST" if "json" in kwargs or "data" in kwargs else "GET"
        response = self.client.request(method, url, **kwargs)
        response.raise_for_status()
        # Reject error pages from fund APIs before caching them.
        if name.endswith(".json"):
            payload = response.json()
            if payload.get("ErrCode", 0) or payload.get("status", 1) != 1:
                raise ValueError(f"{name}: fund NAV service error")
            if "code" in payload and payload["code"] != "ETS-5BP00000":
                raise ValueError(f"{name}: fund NAV service error {payload['code']}")
        elif not TABLE_ROW.search(response.content):
            raise ValueError(f"{name}: no historical unit NAV table")
        self.pending[path] = gzip.compress(response.content, mtime=0)
        return response.content

    def eastmoney(self, code, start, end):
        page = 1
        out = {}
        while True:
            params = dict(fundCode=code, pageIndex=page, pageSize=20, startDate=start, endDate=end)
            url = f"{EASTMONEY}?{urlencode(params)}"
            body = self.fetch(
                f"eastmoney_{code}_{start}_{end}_{page}.json",
                url,
                headers={"Referer": "https://fundf10.eastmoney.com/"},
            )
            payload = json.loads(body)
            rows = payload["Data"]["LSJZList"]
            for row in rows:
                day = row["FSRQ"]
                if day in out or not start <= day <= end:
                    raise ValueError(f"{code} {day}: duplicate or out-of-range NAV")
                out[day] = (Decimal(row["DWJZ"]), "天天基金历史单位净值", url)
            if len(out) == int(payload["TotalCount"]):
                return out
            if not rows or len(out) > int(payload["TotalCount"]):
                raise ValueError(f"{code}: incomplete fund history")
            page += 1

    def regulatory_reports(self, code, dates):
        observations = {}
        for start, end in _groups(sorted(dates), 100):
            fields = dict(
                sEcho=1,
                iDisplayStart=0,
                iDisplayLength=100,
                fundType="all",
                fundCode=code,
                fundName="",
                fundCompanyShortName="",
                startDate=start,
                endDate=end,
            )
            query = [{"name": k, "value": v} for k, v in fields.items()]
            params = {"aoData": json.dumps(query, separators=(",", ":"))}
            raw = self.fetch(
                f"csrc_{code}_{start}_{end}.json",
                f"{CSRC}/getPublicFundJZInfoMore.do",
                params=params,
            )
            payload = json.loads(raw)
            rows = payload["aaData"]
            if len(rows) != int(payload["iTotalDisplayRecords"]):
                raise ValueError(f"{code}: incomplete regulator NAV history")
            for row in rows:
                day = row["valuationDate"]
                if row["code"] != code or not start <= day <= end or day in observations:
                    raise ValueError(f"{code} {day}: unexpected regulator report key")
                url = f"{CSRC}/fund_detail.do?fundId={int(row['fund']['idStr'])}"
                observations[day] = (
                    Decimal(row["shareNetValue"]),
                    "证监会基金电子披露净值日报",
                    url,
                )
        return observations

    def latest_pcf(self, code, expected_day):
        # The public exchange endpoint serves only the latest basket. Its date
        # parameters do not select past baskets. Keep snapshots by valuation
        # date so a refresh cannot overwrite an older dated basket.
        params = dict(
            isPagination="false", FUNDID2=code, sqlId="COMMON_SSE_CP_JJLB_ETFJJGK_GGSGSHQD_JBXX_C"
        )
        url = f"{SSE_PCF}?{urlencode(params)}"
        path = self.raw / f"sse_pcf_{code}_{expected_day}.json.gz"
        if path.exists():
            return _pcf_observation(gzip.decompress(path.read_bytes()), code)
        response = self.client.get(url, headers={"Referer": "https://www.sse.com.cn/"})
        response.raise_for_status()
        day, value = _pcf_observation(response.content, code)
        if day == expected_day:
            self.pending[path] = gzip.compress(response.content, mtime=0)
        return day, value

    def manager(self, code, dates):
        """Fetch primary evidence for the requested days (may span several years)."""
        days = sorted(dates)
        out = {}
        if code in {"159919", "159922"}:
            url = f"{JSFUND}?FundCode={code}&SiteID=1"
            raw = self.fetch(f"harvest_{code}_asof_{days[-1]}.html", url)
            for day, value in TABLE_ROW.findall(raw):
                out[day.decode()] = (Decimal(value.decode()), "嘉实基金历史净值", url)
        elif code in {"510050", "510330", "588000"}:
            # Small date intervals fit one published page even when the site
            # includes nontrading quarter-end valuations.
            for first, last in _groups(days, 9):
                params = dict(fundcode=code, beginDate=first, endDate=last, isQuery=1)
                url = f"{CHINAAMC}?{urlencode(params)}"
                raw = self.fetch(f"chinaamc_{code}_{first}_{last}.html", url)
                for day, value in TABLE_ROW.findall(raw):
                    out[day.decode()] = (Decimal(value.decode()), "华夏基金历史净值", url)
        elif code in {"510310", "159915"}:
            url = f"https://www.efunds.com.cn/Mobile/fund/{code}.shtml"
            for first, last in _groups(days, 100):
                params = dict(
                    fundCode=code,
                    pageIndex=0,
                    pageSize=100,
                    startDate=first,
                    endDate=last,
                    siteID=21,
                )
                raw = self.fetch(f"efunds_{code}_{first}_{last}.json", EFUNDS, json=params)
                data = json.loads(raw)["data"]
                if len(data["data"]) != int(data["total"]):
                    raise ValueError(f"{code}: incomplete manager NAV history")
                for row in data["data"]:
                    out[row["navDate"]] = (Decimal(row["netValue"]), "易方达基金历史净值", url)
        elif code in {"510500", "512100"}:
            url = f"https://www.nffund.com/new/personal-financing/detail.html?fundCode={code}"
            page = 1
            while True:
                raw = self.fetch(
                    f"southern_{code}_asof_{days[-1]}_{page}.json",
                    NFFUND,
                    data={"fundCode": code, "curPage": page, "pageSize": 100},
                )
                data = json.loads(raw)["data"]["jjjzList"]
                for row in data["list"]:
                    day = datetime.strptime(row["fdate"], "%Y%m%d").date().isoformat()
                    out[day] = (Decimal(row["fundNav"]), "南方基金历史净值", url)
                if not data["list"] or min(out) <= days[0] or page >= int(data["pages"]):
                    break
                page += 1
        return out


def _pcf_observation(raw: bytes, code: str) -> tuple[str, Decimal]:
    (record,) = json.loads(raw)["result"]
    valuation_date = datetime.strptime(record["PRE_TRADING_DAY"], "%Y%m%d").date()
    basket_date = datetime.strptime(record["TRADING_DAY"], "%Y%m%d").date()
    if record["TRADE_CODE"] != code or valuation_date >= basket_date:
        raise ValueError(f"{code}: invalid PCF valuation date or fund")
    value = Decimal(record["NAV"].removeprefix("￥"))
    if not value.is_finite() or value <= 0:
        raise ValueError(f"{code}: invalid PCF unit NAV")
    return valuation_date.isoformat(), value


def _groups(days: list[str], max_calendar_days: int) -> Iterator[tuple[str, str]]:
    first = previous = date.fromisoformat(days[0])
    for item in days[1:]:
        day = date.fromisoformat(item)
        if day - first >= timedelta(days=max_calendar_days):
            yield first.isoformat(), previous.isoformat()
            first = day
        previous = day
    yield first.isoformat(), previous.isoformat()


def collect_nav(output: Path, start: date, end: date, *, refresh=False) -> list[tuple[str, str]]:
    if start > end:
        raise ValueError("NAV date range must be chronological")
    days = load_market_calendar(BUNDLED_CALENDAR_PATH, start=start, end=end)
    with httpx.Client(
        timeout=25, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}
    ) as client:
        source = NavSource(client, output / "raw" / "nav", refresh)

        def collect(code):
            expected = {d.isoformat() for d in (*days, *QUARTER)}
            if code == "510310":
                expected.update(d.isoformat() for d in SPLIT)
            secondary = {}
            for first, last in _groups(sorted(expected), 100):
                secondary.update(source.eastmoney(code, first, last))
            primary = source.manager(code, expected)
            regulator = source.regulatory_reports(code, expected)
            pcf = {}
            if code == "510300":
                pcf_day, pcf_value = source.latest_pcf(code, max(expected))
                pcf[pcf_day] = pcf_value
            result = []
            for day in sorted(expected):
                fund = primary.get(day)
                report = regulator.get(day)
                vendor = secondary.get(day)
                available = [r for r in (fund, report, vendor) if r]
                if report is None or len(available) < 2:
                    continue
                value, label, url = available[0]
                if any(r[0] != value for r in available) or pcf.get(day, value) != value:
                    raise ValueError(f"{code} {day}: conflicting unit NAVs among sources")
                if not value.is_finite() or value <= 0:
                    raise ValueError(f"{code} {day}: invalid unit NAV {value}")
                symbol = f"{code}.{'SH' if code in SSE else 'SZ'}"
                result.append(
                    (
                        symbol,
                        day,
                        str(value),
                        url,
                        label,
                        len(available) > 1,
                        bool(report),
                        day in pcf,
                    )
                )
            return result, sorted(expected - {r[1] for r in result})

        rows = []
        gaps = []
        codes = (*SSE, *SZSE)
        with ThreadPoolExecutor(max_workers=3) as pool:
            for code, (records, missing) in zip(codes, pool.map(collect, codes), strict=True):
                rows.extend(records)
                gaps.extend((code, day) for day in missing)
    if gaps:
        return gaps
    for path, contents in source.pending.items():
        _write(path, contents)
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        (
            "symbol",
            "date",
            "nav",
            "source_url",
            "source_name",
            "cross_checked",
            "regulator_checked",
            "pcf_checked",
        )
    )
    writer.writerows(sorted(rows))
    _write(output / "nav.csv", buffer.getvalue().encode("utf-8"))
    return gaps


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2026, 8, 4))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 11, 4))
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    missing = collect_nav(args.output_dir, args.start, args.end, refresh=args.refresh)
    print(f"Missing NAVs: {len(missing)} {missing}")
    raise SystemExit(int(bool(missing)))
