"""后台核验研究提醒；行情热路径只读有时限的不可变准入快照。"""
from __future__ import annotations

import logging
import math
import threading
from datetime import datetime

from app.market_time import cn_now
from app.services.holding_analysis import read_inputs
from app.services.holding_analysis_store import HoldingAnalysisStore
from app.strategy import monitor_rules

logger = logging.getLogger(__name__)


class HoldingAlertGuard:
    def __init__(self, repo, quote_service, position_service, *, clock=cn_now):
        self.repo = repo
        self.quotes = quote_service
        self.positions = position_service
        self.store = HoldingAnalysisStore(repo.store.data_dir)
        self.clock = clock
        self._allowed: dict[tuple[str, str], float] = {}
        self._stop = threading.Event()
        self._refresh_lock = threading.Lock()
        self._thread = None

    def allow(self, rule, now):
        return not self._stop.is_set() and now <= self._allowed.get((rule.get("id"), rule.get("holding_plan_id")), -1)

    def revoke(self):
        with self._refresh_lock:
            self._allowed = {}

    def refresh(self):
        with self._refresh_lock:
            # Discard the previous admission before I/O: failures never revive stale permissions.
            self._allowed = {}
            now = self.clock()
            allowed = {}
            contexts = {}
            plans = [p for p in self.store.all_plans()
                     if p["status"] == "adopted" and p.get("alert_rule_ids")]
            if not plans or self.quotes is None:
                return
            status = self.quotes.status()
            if not status.get("enabled") or status.get("paused") or not status.get("realtime_allowed"):
                return
            quotes = self.quotes.get_market_records(list({p["row"]["symbol"] for p in plans}))
            for plan in plans:
                if (not plan.get("expires_on") or plan["expires_on"] < now.date().isoformat()
                        or datetime.fromisoformat(plan["generated_at"]).date() != now.date()):
                    continue
                context = (plan["source"], plan.get("account_id", ""))
                if context not in contexts:
                    inputs = read_inputs(self.repo, self.positions, *context)
                    keys = [p["key"] for p in inputs["positions"]]
                    contexts[context] = (inputs["input_version"], self.store.profile_version(keys))
                if contexts[context] != (plan["input_version"], plan["profile_version"]):
                    continue
                quote = quotes.get(plan["row"]["symbol"], {})
                stamp = quote.get("timestamp")
                price = quote.get("last_price")
                if (isinstance(stamp, bool) or not isinstance(stamp, (int, float))
                        or not math.isfinite(stamp) or not 0 <= now.timestamp() - stamp / 1000 <= 10
                        or isinstance(price, bool) or not isinstance(price, (int, float))
                        or not math.isfinite(price) or price <= 0):
                    continue
                reference = plan["row"].get("price", {})
                if not reference.get("reference_date") or reference.get("reference_close") is None:
                    continue
                if reference.get("reference_date") and reference["reference_date"] < now.date().isoformat():
                    anchor, previous = reference.get("reference_close"), quote.get("prev_close")
                    if (isinstance(anchor, bool) or not isinstance(anchor, (int, float))
                            or isinstance(previous, bool) or not isinstance(previous, (int, float))
                            or not math.isfinite(anchor) or not math.isfinite(previous)
                            or anchor <= 0 or previous <= 0
                            or not math.isclose(anchor, previous, abs_tol=.011)):
                        continue
                # The worker must refresh its permission lease; a stopped worker fails closed.
                deadline = min(now.timestamp() + 6, stamp / 1000 + 10)
                for rule_id in plan["alert_rule_ids"]:
                    if plan.get("alert_bindings", {}).get(rule_id) != "active":
                        continue
                    rule = monitor_rules.load_one(self.repo.store.data_dir, rule_id)
                    if rule and rule.get("enabled") is True and rule.get("holding_plan_id") == plan["id"]:
                        allowed[(rule_id, plan["id"])] = deadline
            self._allowed = allowed

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception:  # noqa: BLE001 -- worker isolation, no user data in the log
                self._allowed = {}
                logger.warning("持仓研究提醒检查失败，已暂停本轮提醒")
            self._stop.wait(5)

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="holding-alert-guard", daemon=True)
        self._thread.start()

    def stop(self):
        self._allowed = {}
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
