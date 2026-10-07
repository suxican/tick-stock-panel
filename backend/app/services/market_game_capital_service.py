"""Explicit refreshes append observations without changing the frozen plan."""
# ruff: noqa: RUF001
from __future__ import annotations

import threading
from datetime import date, datetime
from pathlib import Path

from app.market_time import CN_TZ, cn_now, in_continuous_session
from app.services.market_game_capital import build_capital_behavior, capital_background
from app.services.market_game_capital_models import CapitalEnvelope, CapitalObservation
from app.services.market_game_disclosures import fetch_institutional_disclosures
from app.services.market_game_feedback import observe_feedback
from app.services.market_game_models import GameReport
from app.services.market_game_overseas import build_overseas_context, collect_overseas_quotes
from app.services.market_game_plan import plan_status

NEGATIVE_SCENARIOS = {"panic_withdrawal", "hot_distribution"}
LIMITATIONS = [
    "机构披露与分钟行为独立展示；机构净额不能代表未披露资金，也不能识别量化账户。",
    "缺少逐笔成交、逐笔委托及撤单序列，量化交易身份与订单行为暂不可验证。",
    "新增约束仅收紧观察计划，不授权入场、不提高仓位；原计划全部条件仍须另行核验。",
    "未完成情绪单独、行为单独和组合信号的滚动对照验证，不输出胜率或收益承诺。",
    "变化记录来自实际刷新存档；盘中曲线是本次重建的分钟量价轨迹，不代表当时已触发的事件。",
]


class CapitalBusyError(RuntimeError):
    pass


def _now(value: datetime | None) -> datetime:
    current = value or cn_now()
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("observation time requires an explicit timezone")
    return current.astimezone(CN_TZ)


def _auto_allowed(report: dict, now: datetime) -> bool:
    validity = report.get("validity") or {}
    return bool(validity.get("status") == "scheduled" and validity.get("calendar_verified")
                and now.date().isoformat() in validity.get("observation_sessions", [])
                and in_continuous_session(now))


def _overseas_pressure(candidate: dict, overseas: dict | None) -> bool:
    if not overseas:
        return False
    pressure = {factor["id"] for factor in overseas["factors"] if factor["state"] == "pressure"}
    sector = candidate["sector"].casefold()
    chips = any(key in sector for key in ("半导体", "芯片", "存储", "hbm", "集成电路", "光刻"))
    tech = chips or any(key in sector for key in ("算力", "人工智能", "服务器", "电子", "通信", "光模块", "cpo"))
    return bool((tech and pressure & {"us_tech", "korea_market"}) or (chips and "memory_chain" in pressure))


def _links(report: dict, behavior: dict, history: list[dict], now: datetime, background: bool,
           overseas: dict | None = None) -> list[dict]:
    state = plan_status(report, now)
    rows = {row["symbol"]: row for row in behavior["rows"]}
    # Carry a veto forward for this report's entry session, including across
    # process restarts and subsequent missing-data / recovery observations.
    blocked = {link["symbol"] for item in history
               if item["created_at"][:10] == now.date().isoformat()
               for link in item["plan_links"] if link["status"] == "blocked"}
    result = []
    for candidate in report.get("candidates", []):
        row = rows.get(candidate["symbol"])
        cap = candidate["max_position"]
        prior_caps = []
        for item in history:
            if item["created_at"][:10] != now.date().isoformat():
                continue
            for link in item["plan_links"]:
                if link["symbol"] != candidate["symbol"]:
                    continue
                if link.get("retained_cap") is not None:
                    prior_caps.append(link["retained_cap"])
                elif link["status"] == "watch":
                    prior_caps.append(link["observation_cap"])
        retained_cap = min([cap, *prior_caps])
        status, reason, observed_cap = "unverified", "分钟或上一交易日情绪证据不足，暂不形成附加判断", 0
        if state != "entry_window":
            status, reason = "inactive", "当前不在原计划入场观察日内，仅保留研究观察"
        elif candidate["symbol"] in blocked:
            status, reason = "blocked", "本入场日此前已记录负反馈约束，随后修复也不自动恢复计划"
        elif (row and row["status"] == "ready" and background
              and behavior["data_date"] == now.date().isoformat()):
            if row["scenario"]["id"] in NEGATIVE_SCENARIOS:
                status, reason = "blocked", f"新增负反馈约束：{row['scenario']['label']}；取消本日新增仓位观察"
            else:
                status, reason, observed_cap = "watch", "尚未触发新增负反馈约束；保留原上限观察，未确认入场", cap
        if status == "watch":
            # One haircut against the original cap, never successive 50% cuts
            # for correlated markets or repeated polls. Retain it for the day.
            observed_cap = min(observed_cap, retained_cap)
            if _overseas_pressure(candidate, overseas):
                observed_cap = min(observed_cap, cap * .5)
                reason = "外围科技或半导体压力：观察上限收紧至原上限50%，相关市场不叠加减仓；仍待原条件核验"
            elif observed_cap < cap:
                reason = "保留本入场日此前记录的外围收紧约束，不因随后上涨或数据缺失自动恢复上限"
            retained_cap = observed_cap
        elif status == "blocked":
            retained_cap = 0
        conditions = [item["description"] for item in candidate.get("conditions", [])]
        if not conditions:
            conditions = ["旧报告未冻结结构化条件，须重新生成计划后逐项核验"]
            if status == "watch":
                status, observed_cap, reason = "unverified", 0, "旧报告缺少结构化条件，无法关联当前计划"
        result.append({"symbol": candidate["symbol"], "name": candidate["name"], "status": status,
                       "original_max_position": cap, "observation_cap": observed_cap,
                       "retained_cap": retained_cap,
                       "reason": reason, "unchecked_conditions": conditions})
    return result


def _changes(previous: dict | None, current: dict) -> list[str]:
    if previous is None:
        return ["首次保存资金观察；此前没有实际刷新记录"]
    changes = []
    old, new = previous["behavior"], current["behavior"]
    if old["data_date"] != new["data_date"]:
        changes.append(f"行情日期切换：{old['data_date'] or '未知'} → {new['data_date'] or '未知'}")
    if old["status"] != new["status"]:
        changes.append(f"数据状态变化：{new['reason']}")
    rows = {row["symbol"]: row for row in old["rows"]}
    for row in new["rows"]:
        prior = rows.get(row["symbol"])
        if prior and prior["scenario"]["id"] != row["scenario"]["id"]:
            changes.append(f"{row['name']}：{prior['scenario']['label']} → {row['scenario']['label']}")
        elif prior and prior["status"] != row["status"]:
            changes.append(f"{row['name']}：{row['reason']}")
    earlier = {link["symbol"]: link["status"] for link in previous["plan_links"]}
    for link in current["plan_links"]:
        if earlier.get(link["symbol"]) != "blocked" and link["status"] == "blocked":
            changes.append(f"{link['name']}：本入场日增加取消约束")
        prior = next((item for item in previous["plan_links"] if item["symbol"] == link["symbol"]), None)
        if prior and link["status"] == "watch" and link["observation_cap"] < prior["observation_cap"]:
            changes.append(f"{link['name']}：外围因素收紧观察上限至{link['observation_cap']:.1%}")
    old_overseas, new_overseas = previous.get("overseas"), current.get("overseas")
    if new_overseas and (not old_overseas or old_overseas["risk_state"] != new_overseas["risk_state"]):
        changes.append(f"外围环境更新：{new_overseas['summary']}")
    prior_disclosure, disclosure = previous["disclosures"], current["disclosures"]
    if any(prior_disclosure[key] != disclosure[key] for key in ("state", "trade_date", "rows")):
        changes.append(f"机构披露更新：{disclosure['status']}")
    return changes or ["本次刷新未发现数据状态、场景或取消约束变化"]


class CapitalObservationService:
    def __init__(self, repo, store, data_dir: Path):
        self.repo, self.store, self.data_dir = repo, store, Path(data_dir)
        self._guard = threading.Lock()
        self._refreshing = False

    def _report(self, report_id: str) -> dict:
        return GameReport.model_validate(self.store.get(report_id)).model_dump(mode="json")

    def _history(self, report_id: str) -> list[dict]:
        return [CapitalObservation.model_validate(item).model_dump(mode="json", by_alias=True)
                for item in self.store.capital_observations(report_id)]

    def _envelope(self, report: dict, history: list[dict], now: datetime) -> dict:
        automatic = _auto_allowed(report, now)
        result = {
            "report_id": report["id"], "latest": history[0] if history else None,
            "observations": [{"id": item["id"], "created_at": item["created_at"],
                              "observed_at": item["behavior"]["observed_at"], "data_date": item["behavior"]["data_date"],
                              "summary": item["summary"], "changes": item["changes"]} for item in history],
            "refresh_allowed": True,
            "refresh_reason": ("可手动刷新；开启后仅当前页面在计划交易时段每60秒观察一次" if automatic else
                               "可手动补充观察；当前不在计划交易时段，自动刷新暂停"),
            "auto_refresh_allowed": automatic, "history_limit": 60,
        }
        return CapitalEnvelope.model_validate(result).model_dump(mode="json", by_alias=True)

    def get(self, report_id: str, *, now: datetime | None = None) -> dict:
        """GET is a local, read-only archive view, even when no snapshot exists."""
        return self._envelope(self._report(report_id), self._history(report_id), _now(now))

    def refresh(self, report_id: str, *, now: datetime | None = None) -> dict:
        fixed_time = now is not None
        now = _now(now)
        # The lock protects only the busy flag, never network I/O or calculation.
        with self._guard:
            if self._refreshing:
                raise CapitalBusyError("已有资金观察正在刷新")
            self._refreshing = True
        try:
            report, history = self._report(report_id), self._history(report_id)
            if history:
                elapsed = (now - datetime.fromisoformat(history[0]["created_at"])).total_seconds()
                if 0 <= elapsed < 60:
                    return self._envelope(report, history, now)
            snapshot = self.store.snapshot(report_id)
            overseas_raw = collect_overseas_quotes()
            if not fixed_time:
                now = _now(None)
            behavior = build_capital_behavior(self.repo, report, snapshot, now=now)
            target = date.fromisoformat(behavior["data_date"]) if behavior["data_date"] else now.date()
            background, background_reason = capital_background(self.repo, report, snapshot, data_date=target.isoformat(), now=now)
            disclosures = fetch_institutional_disclosures(self.data_dir, now=now, target=target)
            overseas = build_overseas_context(overseas_raw, cutoff=now,
                                              psychology=report.get("psychology") if background else None)
            links = _links(report, behavior, history, now, background, overseas)
            vetoed = sum(link["status"] == "blocked" for link in links)
            valid = sum(row["status"] == "ready" for row in behavior["rows"])
            summary = f"{valid}/{len(behavior['rows'])}只样本具备当前分钟分位；{vetoed}项新增仓位观察被取消。"
            item = {
                "report_id": report_id, "created_at": now.isoformat(timespec="seconds"),
                "background_date": report["as_of"], "background_usable": background,
                "background_reason": background_reason, "behavior": behavior, "disclosures": disclosures,
                "plan_status": plan_status(report, now), "plan_links": links, "entry_authorized": False,
                "summary": summary, "changes": [], "limitations": list(LIMITATIONS),
                "overseas": overseas,
                "feedback": observe_feedback(report, behavior, now=now),
            }
            item["changes"] = _changes(history[0] if history else None, item)
            item = CapitalObservation.model_validate(item).model_dump(mode="json", by_alias=True)
            saved = self.store.save_capital_observation(report_id, item)
            return self._envelope(report, [saved, *history][:60], now)
        finally:
            with self._guard:
                self._refreshing = False
