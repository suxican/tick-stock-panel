"""Build the audited ETF observation dataset from the saved source rows.

From backend: python -m app.custom.etf_observation_data.ledger [--publish] [--end YYYY-MM-DD]
The original release covers BASELINE..LAST. Later sessions are appended one
complete day at a time; published days are never rewritten. Scope reviews end
at each fund's verified_through, after which rows are stored as unverified.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import date
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

import polars as pl

from app.custom.etf_observation_data.shares import SSE, SZSE, _write
from app.market_time import cn_now
from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField, write_ext_parquet
from app.services.huichun_calendar import BUNDLED_CALENDAR_PATH, load_market_calendar

RESOURCES = Path(__file__).parent
BASKET = tuple([s + ".SH" for s in SSE] + [s + ".SZ" for s in SZSE])
BASELINE, LAST = date(2026, 8, 4), date(2026, 11, 4)
TABLE = "etf_broad_flow_daily"
SPLIT_URL = "https://static.cninfo.com.cn/finalpage/2024-09-23/1221270195.PDF"


def csv_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def observations(path: Path, field: str) -> dict:
    by_key = {}
    for row in csv_rows(path):
        symbol = row["symbol"]
        key = symbol, date.fromisoformat(row["date"]).isoformat()
        value = Decimal(row[field])
        url = row["source_url"]
        # The official CSRC fund-disclosure system serves its directory over HTTP.
        if (
            symbol not in BASKET
            or key in by_key
            or not url.startswith(("https://", "http://eid.csrc.gov.cn/fund/disclose/"))
        ):
            raise ValueError(f"Unknown symbol, duplicate or unsourced {field}: {key}")
        if field == "nav" and (
            row["cross_checked"] != "True" or row["regulator_checked"] != "True"
        ):
            raise ValueError(f"Unit NAV lacks regulatory and second-source confirmation: {key}")
        if not value.is_finite() or value <= 0 or (field == "shares" and value % 1):
            raise ValueError(f"Invalid {field}: {key} {value}")
        by_key[key] = (int(value) if field == "shares" else value, url)
    return by_key


def daily_rows(sessions, symbols, shares, navs, scopes, verified_through=None):
    rows = []
    for previous, today in pairwise(sessions):
        for symbol in symbols:
            scope = scopes[symbol]
            # A scope review only vouches for the sessions it covered.
            if verified_through and today > verified_through[symbol]:
                scope = "unverified"
            s0, src0 = shares.get((symbol, previous), (None, None))
            s1, src1 = shares.get((symbol, today), (None, None))
            nav, nav_src = navs.get((symbol, today), (None, None))
            issues = [
                name
                for name, val in (
                    ("missing_previous_shares", s0),
                    ("missing_shares", s1),
                    ("missing_nav", nav),
                )
                if val is None
            ]
            delta = s1 - s0 if s0 is not None and s1 is not None else None
            amount = None if issues else delta * nav
            rows.append(
                dict(
                    symbol=symbol,
                    date=today,
                    previous_date=previous,
                    previous_listed_units=s0,
                    listed_units=s1,
                    unit_nav_cny=nav,
                    listed_change_units=delta,
                    listed_change_value_cny=amount,
                    scope=scope,
                    fund_net_units=delta if scope == "fund_total_verified" else None,
                    fund_net_estimate_cny=amount if scope == "fund_total_verified" else None,
                    status=",".join(issues) or "ok",
                    shares_source=src1,
                    previous_shares_source=src0,
                    nav_source=nav_src,
                )
            )
    return rows


def reconcile_quarters(shares, navs):
    output = []
    previous_by_symbol = {}
    for report in sorted(
        csv_rows(RESOURCES / "quarterly_reports.csv"), key=lambda r: (r["symbol"], r["date"])
    ):
        key = report["symbol"], report["date"]
        quoted, shares_url = shares.get(key, (None, None))
        nav, nav_url = navs.get(key, (None, None))
        total = int(report["closing"])
        off_market = int(report["otc_closing"]) if report["otc_closing"] else None
        expected = total - (off_market or 0)
        residual = None if quoted is None else quoted - expected
        rollforward = (
            int(report["opening"])
            + int(report["subscriptions"])
            - int(report["redemptions"])
            - total
        )
        prior = previous_by_symbol.get(report["symbol"])
        net = int(report["subscriptions"]) - int(report["redemptions"])
        observed_change = otc_change = bridge_error = None
        if prior is not None and quoted is not None and prior["listed_units"] is not None:
            if int(report["opening"]) != int(prior["closing"]):
                raise ValueError(f"Quarterly opening does not match prior close: {key}")
            observed_change = quoted - prior["listed_units"]
            # No separately disclosed OTC balance stays null in the evidence.
            if off_market is not None and prior["otc_closing"]:
                otc_change = off_market - int(prior["otc_closing"])
            bridge_error = observed_change + (otc_change or 0) - net
        agreed = (
            residual is not None
            and abs(residual) < 100
            and nav == Decimal(report["unit_nav"])
            and rollforward == 0
            and (prior is None or (bridge_error is not None and abs(bridge_error) < 200))
        )
        output.append(
            dict(
                **report,
                listed_units=quoted,
                daily_nav=nav,
                comparator="total_less_disclosed_otc" if off_market is not None else "fund_total",
                listed_minus_fund=quoted - total if quoted is not None else None,
                shares_residual=residual,
                report_rollforward_residual=rollforward,
                report_net_units=net,
                listed_change_from_prior_quarter=observed_change,
                disclosed_otc_change_from_prior_quarter=otc_change,
                quarterly_change_residual=bridge_error,
                status="pass" if agreed else "check",
                shares_source=shares_url,
                nav_source=nav_url,
            )
        )
        previous_by_symbol[report["symbol"]] = output[-1]
    return output


def split_check(shares, navs):
    symbol = "510310.SH"
    before, event, after = "2024-09-19", "2024-09-20", "2024-09-23"
    missing_shares = [day for day in (before, event, after) if (symbol, day) not in shares]
    missing_navs = [day for day in (event, after) if (symbol, day) not in navs]
    if missing_shares or missing_navs:
        return dict(
            symbol=symbol, status="check", missing_shares=missing_shares, missing_navs=missing_navs
        )
    s0, src0 = shares[symbol, before]
    s1, src1 = shares[symbol, event]
    s2, src2 = shares[symbol, after]
    nav, nav_src = navs[symbol, event]
    next_nav, next_nav_src = navs[symbol, after]
    # The same-day notice uses actual per-holder rounded registrations. Normal
    # subscriptions were open on the record date, before the conversion at close.
    old, new = 124_750_307_415, 62_347_244_017
    ratio, old_nav = Decimal("0.49977589"), Decimal("1.6003")
    passed = abs(s1 - new) < 100 and nav == Decimal("3.2021")
    return dict(
        symbol=symbol,
        record_date=event,
        ex_date=after,
        ratio=ratio,
        notice_pre_listed=old,
        notice_post_listed=new,
        notice_pre_otc=708_041_821,
        notice_post_otc=353_862_232,
        pre_unit_nav=old_nav,
        post_unit_nav=nav,
        exchange_before=s0,
        exchange_event=s1,
        exchange_next=s2,
        naive_change_units=s1 - s0,
        mechanical_change_units=new - old,
        rounding_units=new - old * ratio,
        post_registration_residual=s1 - new,
        pre_conversion_change_old_units=old - s0,
        event_listed_change_value_cny=(old - s0) * old_nav if passed else None,
        next_change_units=s2 - s1,
        next_unit_nav=next_nav,
        next_listed_change_value_cny=(s2 - s1) * next_nav if passed else None,
        status="pass" if passed else "check",
        source_url=SPLIT_URL,
        timetable_url="https://www.sse.com.cn/disclosure/fund/announcement/c/new/2024-09-13/510310_20240913_TKPG.pdf",
        previous_shares_source=src0,
        shares_source=src1,
        next_shares_source=src2,
        nav_source=nav_src,
        next_nav_source=next_nav_src,
    )


def publish(frame: pl.DataFrame, data_dir: Path) -> list[str]:
    """Publish validated partitions; never overwrite another table or a published day.

    An existing release only grows: every published day must match exactly, and
    new days must all come after the last published one. Returns the days added.
    """
    fields = [
        ExtField(col, "int" if typ.is_integer() else "float" if typ.is_decimal() else "string")
        for col, typ in frame.schema.items()
    ]
    config = ExtConfig(
        TABLE,
        "宽基 ETF 日度份额与净申购估算",
        "timeseries",
        fields,
        description="登记日口径；场内等值覆盖全池，全基金估算只填已验证产品。金额单位为元；历史回补。",
    )
    # The extension contract uses Float64 for money. daily.csv retains the exact decimals.
    frame = frame.with_columns(pl.col(pl.Decimal).cast(pl.Float64))
    target = data_dir / "ext_data" / TABLE
    groups = {day: part for (day,), part in frame.partition_by("date", as_dict=True).items()}
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=data_dir) as temp:
            stage = Path(temp)
            _stage(config, stage, groups)
            (stage / "ext_data" / TABLE).rename(target)
        return sorted(groups)
    stored = ExtConfigStore(data_dir).get(TABLE)
    if not stored or stored.label != config.label or stored.mode != config.mode:
        raise ValueError(f"Destination belongs to another dataset: {target}")
    folder = target / "timeseries"
    published = {p.name.removeprefix("date=") for p in folder.glob("date=*") if p.is_dir()}
    for day in sorted(published & groups.keys()):
        existing = pl.read_parquet(folder / f"date={day}" / "part.parquet")
        if not _same_release(existing, groups[day]):
            raise ValueError(
                f"Published snapshot differs on {day}; review the revision before replacing it"
            )
    added = sorted(groups.keys() - published)
    if added and published and added[0] <= max(published):
        raise ValueError(f"Refusing to backfill {added[0]} inside the published release")
    if added:
        with TemporaryDirectory(dir=data_dir) as temp:
            stage = Path(temp)
            _stage(config, stage, {day: groups[day] for day in added})
            # Ascending renames keep the release a gap-free prefix if interrupted.
            for day in added:
                (stage / "ext_data" / TABLE / "timeseries" / f"date={day}").rename(folder / f"date={day}")
    return added


SOURCE_COLUMNS = ("shares_source", "previous_shares_source", "nav_source")


def _same_release(existing: pl.DataFrame, rebuilt: pl.DataFrame) -> bool:
    """Values must match exactly; a source may only be re-cited from the same host.

    Each run queries an exchange or manager over a range ending on the latest
    session, so the cited query URL widens while the observation stays the same.
    """
    if existing.schema != rebuilt.schema:
        return False
    existing, rebuilt = existing.sort("symbol"), rebuilt.sort("symbol")
    sources = [c for c in SOURCE_COLUMNS if c in existing.columns]
    if not existing.drop(sources).equals(rebuilt.drop(sources)):
        return False
    for column in sources:
        for old, new in zip(existing[column], rebuilt[column], strict=True):
            if (old is None) != (new is None) or (old and urlsplit(old).netloc != urlsplit(new).netloc):
                return False
    return True


def _stage(config: ExtConfig, stage: Path, groups: dict[str, pl.DataFrame]) -> None:
    ExtConfigStore(stage).create(config)
    for day, part in groups.items():
        write_ext_parquet(
            part, config, stage, snapshot_date=date.fromisoformat(day), keep_strategy_cache=True
        )


def build(data_dir: Path, end: date = LAST, *, publish_release: bool = False) -> list[str]:
    """Rebuild BASELINE..end from the saved source rows; returns newly published days."""
    if end < LAST:
        raise ValueError(f"The audited release already runs to {LAST}")
    base = data_dir / "etf_observation"
    inputs = [
        base / "shares.csv",
        base / "nav.csv",
        RESOURCES / "scope.json",
        RESOURCES / "quarterly_reports.csv",
        BUNDLED_CALENDAR_PATH,
    ]
    input_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs}
    shares = observations(base / "shares.csv", "shares")
    navs = observations(base / "nav.csv", "nav")
    scope_records = json.loads((RESOURCES / "scope.json").read_text(encoding="utf-8-sig"))
    scopes = {r["symbol"]: r["scope"] for r in scope_records}
    verified_through = {r["symbol"]: r["verified_through"] for r in scope_records}
    if (
        len(scope_records) != len(BASKET)
        or set(scopes) != set(BASKET)
        or not set(scopes.values()) <= {"fund_total_verified", "listed_only", "unverified"}
        or any(
            date.fromisoformat(r["verified_from"]) > BASELINE
            or date.fromisoformat(r["verified_through"]) < LAST
            or not r["reason"]
            or not r["source_url"].startswith("https://")
            for r in scope_records
        )
    ):
        raise ValueError("Incomplete scope evidence")
    sessions = [
        d.isoformat() for d in load_market_calendar(BUNDLED_CALENDAR_PATH, start=BASELINE, end=end)
    ]
    quarters = reconcile_quarters(shares, navs)
    expected_checks = {(s, d) for s in BASKET for d in ("2026-06-30", "2026-09-30")}
    if {(r["symbol"], r["date"]) for r in quarters} != expected_checks or len(quarters) != len(
        expected_checks
    ):
        raise ValueError("Incomplete or duplicate quarter references")
    for check in quarters:
        if (
            check["otc_closing"]
            and int(check["otc_closing"]) > 0
            and scopes[check["symbol"]] == "fund_total_verified"
        ):
            raise ValueError(
                f"Whole-fund scope conflicts with OTC holdings in {check['symbol']} report"
            )
    split = split_check(shares, navs)
    rows = daily_rows(sessions, BASKET, shares, navs, scopes, verified_through)
    frame = pl.DataFrame(
        rows,
        infer_schema_length=None,
        schema_overrides={
            "unit_nav_cny": pl.Decimal(18, 6),
            "listed_change_value_cny": pl.Decimal(30, 6),
            "fund_net_estimate_cny": pl.Decimal(30, 6),
        },
    )
    summary = (
        frame.group_by("symbol", "scope")
        .agg(
            pl.len().alias("sessions"),
            pl.col("listed_change_value_cny").count().alias("listed_days"),
            pl.col("fund_net_estimate_cny").count().alias("whole_fund_days"),
            *[
                pl.when(pl.col(c).is_not_null().all()).then(pl.col(c).sum()).alias(c)
                for c in ("listed_change_value_cny", "fund_net_estimate_cny")
            ],
        )
        .sort("symbol")
    )
    complete = (
        sessions[-1] == end.isoformat()
        and all(r["status"] == "ok" for r in rows)
        and len(quarters) == 20
        and all(r["status"] == "pass" for r in quarters)
        and split["status"] == "pass"
    )
    if any(hashlib.sha256(p.read_bytes()).hexdigest() != input_hashes[p.name] for p in inputs):
        raise ValueError("Source changed during this run; retry against the completed snapshot.")
    # Once published, even an export-only retry must match the native table.
    added = []
    if complete and (publish_release or (data_dir / "ext_data" / TABLE).exists()):
        added = publish(frame, data_dir)
    # Never replace an audited release with partial exports on a failed retry.
    destination = base if complete else base / "incomplete"
    for name, table in (
        ("daily", frame),
        ("summary", summary),
        ("reconciliation", pl.DataFrame(quarters)),
    ):
        _write(destination / f"{name}.csv", table.write_csv().encode("utf-8"))
    _write(
        destination / "split_510310.json",
        json.dumps(split, indent=2, ensure_ascii=False, default=str).encode(),
    )
    manifest = dict(
        generated_at=cn_now().isoformat(),
        baseline=sessions[0],
        first_session=sessions[1],
        last_session=sessions[-1],
        symbols=list(BASKET),
        sessions=len(sessions) - 1,
        daily_rows=frame.height,
        complete=complete,
        checked_quarter_ends=len(quarters),
        split_check=split["status"],
        input_sha256=input_hashes,
        method="相邻登记日份额差 × 当日未复权单位净值，元。全基金字段只在份额范围有证据时填值。",
        timing=(
            f"{LAST} 及以前为历史回补，之后在证据齐备后按日追加；date 是份额登记/净值归属日，"
            "不可视为当日盘中可用。"
        ),
    )
    _write(
        destination / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode()
    )
    if not complete:
        raise ValueError(
            f"Incomplete or conflicting data; see {destination}. Publication withheld."
        )
    return added


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=RESOURCES.resolve().parents[3] / "data")
    parser.add_argument("--end", type=date.fromisoformat, default=LAST)
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    added = build(args.data_dir, args.end, publish_release=args.publish)
    print(f"Ledger complete through {args.end}; newly published days: {added or 'none'}")


if __name__ == "__main__":
    main()
