"""兜底链调度与源健康熔断 (进程内, 重启清空)。

生效链 = [主源] + 兜底顺序 (preferences.get_capability_fallbacks), 去重后只保留当前
candidates 中的源。call_with_fallback 依次尝试链上第一个可用源, 结果只来自一个源。

熔断按错误类型 (data_providers.errors):
  - ProviderAuthError: open 直到 reset (Key 变更 / 设置页「重试主源」)
  - ProviderQuotaError: open 到北京时间次日 00:00, retry_after 更短时取之
  - 其它异常 / ProviderDataError / 校验失败: 连续 3 次后 open 300 秒
open 到期后进入 half_open, 只放行一个探测: 成功 → healthy, 失败 → 再 open。
状态以 (能力, 源) 为键。原因文案只由分类和时间拼成, 不拼异常原文 (可能含 Key)。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.data_providers.errors import ProviderAuthError, ProviderDataError, ProviderQuotaError
from app.market_time import cn_now

logger = logging.getLogger(__name__)

FAILURE_THRESHOLD = 3
TRANSIENT_OPEN_S = 300
# 半开探测权的租期: 拿到探测权却没有登记结果 (调用方异常退出) 时, 超期后允许下一个探测
PROBE_LEASE_S = 120

_HEALTHY, _OPEN, _HALF_OPEN = "healthy", "open", "half_open"
_TRANSIENT, _AUTH, _QUOTA = "transient", "auth", "quota"


@dataclass(frozen=True)
class FallbackResult:
    data: object | None
    source: str | None
    degraded: bool
    reason: str | None
    attempts: tuple[tuple[str, str], ...]


@dataclass
class _Health:
    state: str = _HEALTHY
    failures: int = 0
    until: datetime | None = None
    reason: str | None = None
    probing: bool = False
    probe_at: datetime | None = None
    kind: str | None = None  # open 的原因类别: transient / auth / quota


_lock = threading.Lock()
_health: dict[tuple[str, str], _Health] = {}


# ---------------------------------------------------------------- 链解析 (测试可替换)

def _primary(cap_id: str) -> str:
    from app.data_providers.capabilities import _cap_specs_by_id, _current_routing

    spec = _cap_specs_by_id()[cap_id]
    return _current_routing().get(spec["field"], spec["default"])


def _fallbacks(cap_id: str) -> list[str]:
    from app.services import preferences

    return preferences.get_capability_fallbacks(cap_id)


def _candidate_names(cap_id: str) -> set[str]:
    from app.data_providers.capabilities import candidate_names
    from app.tickflow import policy

    return candidate_names(cap_id, policy.base_tier_name())


def _label(cap_id: str) -> str:
    try:
        from app.data_providers.capabilities import _cap_specs_by_id

        return _cap_specs_by_id()[cap_id]["label"]
    except Exception:  # noqa: BLE001
        return cap_id


def effective_chain(cap_id: str) -> list[str]:
    candidates = _candidate_names(cap_id)
    chain = dict.fromkeys([_primary(cap_id), *_fallbacks(cap_id)])
    return [name for name in chain if name in candidates]


def resolve_provider(name: str) -> object:
    """tickflow → 内置 registry, 其它 → 插件/自定义源; 失败抛 LookupError。"""
    try:
        if name == "tickflow":
            from app.data_providers.registry import get_provider

            return get_provider("tickflow")
        from app.data_providers import custom as custom_sources

        return custom_sources.get_provider(name)
    except Exception as exc:  # 解析失败不等于源失效
        raise LookupError(name) from exc


# ---------------------------------------------------------------- 状态机

def _get(cap_id: str, source: str, now: datetime) -> _Health:
    """取状态并推进到期的 open → half_open (调用方持锁)。"""
    h = _health.setdefault((cap_id, source), _Health())
    if h.state == _OPEN and h.until is not None and now >= h.until:
        h.state, h.probing = _HALF_OPEN, False
        logger.info("%s ← %s 熔断到期, 放行一次探测", _label(cap_id), source)
    return h


def _claim(cap_id: str, source: str, now: datetime, *, sole: bool = False) -> bool:
    """能否使用该源 (调用方持锁)。

    half_open 时只有第一个调用者拿到探测权; 探测权超过 PROBE_LEASE_S 未登记结果视为作废。
    sole=True (链上只有这一个源): 瞬时熔断不拦截 —— 没有别的源可换时熔断只会造成数据空窗,
    比不兜底的现状更差; 认证与额度熔断仍拦截 (继续请求注定失败)。
    """
    h = _get(cap_id, source, now)
    if h.state == _HEALTHY:
        return True
    if h.state == _HALF_OPEN and (
        not h.probing or (h.probe_at is not None and (now - h.probe_at).total_seconds() > PROBE_LEASE_S)
    ):
        h.probing, h.probe_at = True, now
        return True
    return sole and h.kind == _TRANSIENT


def _hm(t: datetime) -> str:
    return t.strftime("%H:%M")


def _open(h: _Health, until: datetime | None, reason: str, kind: str) -> None:
    h.state, h.until, h.reason, h.probing, h.kind = _OPEN, until, reason, False, kind


def _outcome_kind(exc: BaseException) -> str:
    if isinstance(exc, ProviderAuthError):
        return "auth"
    if isinstance(exc, ProviderQuotaError):
        return "quota"
    if isinstance(exc, ProviderDataError):
        return "data"
    return "error"


def record(cap_id: str, source: str, outcome: BaseException | None) -> None:
    """登记一次调用结果; outcome=None 表示成功。"""
    now = cn_now()
    with _lock:
        h = _get(cap_id, source, now)
        before = h.state
        if outcome is None:
            h.state, h.failures, h.until, h.reason, h.probing, h.kind = _HEALTHY, 0, None, None, False, None
        elif isinstance(outcome, ProviderAuthError):
            _open(h, None, "认证失败或无权限，熔断至重试", _AUTH)
        elif isinstance(outcome, ProviderQuotaError):
            midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            until = midnight
            if outcome.retry_after is not None:
                until = min(midnight, now + timedelta(seconds=float(outcome.retry_after)))
            text = "次日 00:00" if until == midnight else _hm(until)
            _open(h, until, f"调用额度或频率超限，熔断至 {text}", _QUOTA)
        else:
            h.failures += 1
            if before != _HEALTHY or h.failures >= FAILURE_THRESHOLD:
                until = now + timedelta(seconds=TRANSIENT_OPEN_S)
                if before == _HALF_OPEN:
                    _open(h, until, f"探测失败，熔断至 {_hm(until)}", _TRANSIENT)
                else:
                    _open(h, until, f"连续 {h.failures} 次失败，熔断至 {_hm(until)}", _TRANSIENT)
            else:
                h.reason = f"请求失败（连续 {h.failures} 次）"
        after, reason = h.state, h.reason
    if before != after:
        logger.info("%s ← %s %s", _label(cap_id), source, reason if after == _OPEN else "恢复正常")


def release(cap_id: str, source: str) -> None:
    """放弃已拿到的半开探测权 (源未被实际调用, 例如 provider 解析失败)。"""
    with _lock:
        h = _health.get((cap_id, source))
        if h is not None:
            h.probing = False


def pick_source(cap_id: str, exclude: Collection[str] = ()) -> str | None:
    """链上第一个可用源 (healthy, 或抢到半开探测权), 不调用。"""
    chain = effective_chain(cap_id)  # 读偏好/插件状态, 不持锁
    now = cn_now()
    with _lock:
        for source in chain:
            if source not in exclude and _claim(cap_id, source, now, sole=len(chain) == 1):
                return source
    return None


def chain_status(cap_id: str) -> list[dict]:
    return source_status(cap_id, effective_chain(cap_id))


def source_status(cap_id: str, sources: list[str]) -> list[dict]:
    """给定源列表的健康状态 (不读偏好, 供能力矩阵按注入的路由组装)。"""
    now = cn_now()
    rows = []
    with _lock:
        for source in sources:
            h = _get(cap_id, source, now)
            rows.append({
                "source": source,
                "state": h.state,
                "reason": h.reason,
                "until": h.until.isoformat() if h.state == _OPEN and h.until else None,
            })
    return rows


def reset(cap_id: str | None = None, source: str | None = None) -> None:
    with _lock:
        for key in list(_health):
            if (cap_id is None or key[0] == cap_id) and (source is None or key[1] == source):
                del _health[key]


# ---------------------------------------------------------------- 调用

def _display(name: str) -> str:
    try:
        from app.data_providers.capabilities import _declared_sources, _display_of

        return _display_of(_declared_sources(), name)
    except Exception:  # noqa: BLE001
        return name


def call_with_fallback(
    cap_id: str,
    method: str,
    *args,
    validate: Callable[[object], bool] | None = None,
    invoke: Callable[[str, Callable[[], object]], object] | None = None,
    **kwargs,
) -> FallbackResult:
    """沿生效链调用 provider.method; 结果只来自一个源, 异常不外抛。

    invoke(源名, 无参调用) 用于包装每次源调用 (例如按源缓存), 缺省直接调用。
    invoke 未真正执行上游调用 (缓存命中、本地背压拒绝、共享同一请求的并发等待者) 时
    不登记源的健康状态: 失败只由真正执行调用的那一方登记一次, 本地拒绝也不换源。
    """
    primary = _primary(cap_id)
    chain = effective_chain(cap_id)
    attempts: list[tuple[str, str]] = []
    reasons: list[str] = []
    if primary not in chain:
        reasons.append(f"{_display(primary)}：当前不可用")

    for source in chain:
        with _lock:
            usable = _claim(cap_id, source, cn_now(), sole=len(chain) == 1)
            skipped_reason = None if usable else _health[(cap_id, source)].reason
        if not usable:
            attempts.append((source, "open"))
            reasons.append(f"{_display(source)}：{skipped_reason or '熔断中'}")
            continue
        try:
            fn = getattr(resolve_provider(source), method)
            if not callable(fn):
                raise LookupError(method)
        except (LookupError, AttributeError):
            release(cap_id, source)
            attempts.append((source, "unresolved"))
            reasons.append(f"{_display(source)}：未加载或未实现")
            continue

        ran = [False]

        def call(fn=fn, ran=ran):
            ran[0] = True
            return fn(*args, **kwargs)

        try:
            data = invoke(source, call) if invoke is not None else call()
        except Exception as exc:  # noqa: BLE001 — 任何源失败都不能拖垮调用方
            if not ran[0]:
                release(cap_id, source)
                attempts.append((source, "skipped"))
                logger.info("%s ← %s 本次未发起请求 (%s), 不计入健康状态",
                            _label(cap_id), source, type(exc).__name__)
                return FallbackResult(None, None, False, "请求繁忙，请稍后重试", tuple(attempts))
            logger.warning("%s ← %s 调用失败: %s", _label(cap_id), source, type(exc).__name__)
            record(cap_id, source, exc)
            attempts.append((source, _outcome_kind(exc)))
            reasons.append(f"{_display(source)}：{_health_reason(cap_id, source) or '请求失败'}")
            continue
        try:
            valid = validate is None or bool(validate(data))
        except Exception:  # noqa: BLE001 — 校验抛异常等同数据无效
            valid = False
        if not valid:
            if ran[0]:
                record(cap_id, source, ProviderDataError("validate"))
            else:
                release(cap_id, source)
            attempts.append((source, "data"))
            reasons.append(f"{_display(source)}：返回数据无效")
            continue
        if ran[0]:
            record(cap_id, source, None)
        else:
            release(cap_id, source)  # 缓存命中: 未实际探测, 不据此判定恢复
        attempts.append((source, "ok"))
        degraded = source != primary
        return FallbackResult(data, source, degraded, "；".join(reasons) if degraded else None, tuple(attempts))

    return FallbackResult(None, None, True, "；".join(reasons) or "无可用数据源", tuple(attempts))


def _health_reason(cap_id: str, source: str) -> str | None:
    with _lock:
        h = _health.get((cap_id, source))
        return h.reason if h else None
