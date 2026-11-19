# 能力兜底链（depth5 + 外围行情）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal：** depth5、外围行情（overseas）主源失效时，自动沿用户配置的兜底链切换到下一个健康源。切换不混源，并在矩阵、设置页、返回数据中可见。

**Architecture：**
- 新增源中立错误类型（`data_providers/errors.py`）和进程内健康调度模块（`data_providers/routing_health.py`）。
- 调度模块负责解析生效链、熔断与半开状态、按链调用。
- `depth_service` 按「整轮一个源」接入；`overseas_indices`、`market_game_overseas` 按「单次请求一个源」接入。
- 能力矩阵和设置页展示链状态。

**Tech Stack：** Python 3 / FastAPI / polars / pytest（`uv run pytest`）；React + TypeScript（`pnpm`）。

**Spec：** `docs/superpowers/specs/2026-11-18-capability-fallback-chain-design.md`

## Global Constraints

- 只有注册表中 `fallback: True` 的能力（本版仅 `depth5`、`overseas`）走兜底链；其余能力行为与现状逐字节一致。
- 熔断参数（模块常量）：
  - 连续瞬时失败阈值 `3`；
  - 瞬时熔断时长 `300` 秒；
  - 配额熔断到次日 `00:00` 北京时间（有 `retry_after` 时取较短者）；
  - 认证熔断直到 `reset`。
- 所有时间判断用 `app` 现有的 `cn_now()`（本机时钟是 UTC-8，不可用 `datetime.now()`）。
- 熔断原因、日志不得包含 API Key。
- `fallbacks` 偏好字段名 = `<主源字段>_fallbacks`：`depth5_data_provider_fallbacks`、`overseas_data_provider_fallbacks`。空列表 = 关闭兜底。
- depth5：一轮结果只来自一个源；外围行情：一次请求结果只来自一个源。
- 工作区是 CRLF（`autocrlf=true`），脚本化改写须保留 `\r\n`。
- 前端用 `pnpm exec`，禁止 `npx`/`npm`。
- 不改 tdxquant、TickFlow 插件代码。
- 每个任务完成后跑 `uv run ruff check` 覆盖改动文件，以及 `git diff --check`。

## Review Focus

1. **兜底源在运行中被卸载或清空 Key**：生效链按调用时的 candidates 过滤，该源被跳过、不报错 → Task 3 `test_chain_skips_source_no_longer_candidate`。
2. **主源同时出现在 fallbacks 里、或 fallbacks 有重复**：生效链去重，主源只尝试一次 → Task 2 `test_fallbacks_dedupe_and_drop_primary`。
3. **全链熔断**：立即返回 `data=None`，不调用任何源（不等超时）→ Task 3 `test_all_open_returns_without_calling`。
4. **半开并发**：depth 定时轮询线程和做T盘口线程同时到达，半开源只放行一个探测 → Task 3 `test_half_open_admits_single_probe`。
5. **配额熔断跨日**：北京时间 23:59 触发的 429 在北京时间次日 00:00 解除，而不是本机日期 → Task 3 `test_quota_open_until_beijing_midnight`。

---

### Task 1：源中立错误类型 + 猫爪映射

**Files:**
- Create: `backend/app/data_providers/errors.py`
- Modify: `backend/app/plugins/meoz/client.py`（`MeozAuthError` 继承；429 处改抛新子类）
- Test: `backend/tests/test_provider_errors.py`

**Interfaces:**
- Produces:
  - `class ProviderError(Exception)`；
  - `class ProviderAuthError(ProviderError)`；
  - `class ProviderQuotaError(ProviderError)`，带属性 `retry_after: float | None = None`；
  - `class ProviderDataError(ProviderError)`；
  - meoz：`class MeozAuthError(MeozError, ProviderAuthError)`、`class MeozRateLimitError(MeozError, ProviderQuotaError)`。

- [ ] **Step 1：写失败测试**

```python
def test_meoz_auth_error_is_neutral_auth():
    from app.data_providers.errors import ProviderAuthError
    from app.plugins.meoz.client import MeozAuthError
    assert issubclass(MeozAuthError, ProviderAuthError)

def test_meoz_429_raises_neutral_quota(monkeypatch):
    # 用 MagicMock 让 MeozClient._post 恒返回 status_code=429、headers={}
    # 并把重试 sleep 打桩为 no-op
    with pytest.raises(ProviderQuotaError) as exc:
        client.query("tick", {})
    assert exc.value.code == 429  # 仍是 MeozError, 现有调用方不受影响
    assert isinstance(exc.value, MeozError)
```

- [ ] **Step 2：** `cd backend && uv run pytest tests/test_provider_errors.py -q`，预期 FAIL（模块不存在）。
- [ ] **Step 3：** 实现 `errors.py`。改 `client.py`：新增 `MeozRateLimitError`，在 `if attempt == _RATE_RETRIES:` 处抛出它（保留 `code=429`），`MeozAuthError` 增加 `ProviderAuthError` 基类。
- [ ] **Step 4：** 跑 `tests/test_provider_errors.py tests/test_meoz_provider.py -q`，预期 PASS。
- [ ] **Step 5：** 提交 `feat(providers): 源中立错误类型，猫爪认证/限频映射`。

---

### Task 2：注册表 `fallback` 键 + fallbacks 偏好

**Files:**
- Modify: `backend/app/data_providers/capabilities.py`（`depth5`、`overseas` 条目加 `"fallback": True`）
- Modify: `backend/app/services/preferences.py`（新增 getter）
- Modify: `backend/app/api/settings.py`（`DataProvidersIn` 新增两个可选 list 字段；`update_data_providers` 的卸载检查和返回体含 fallbacks）
- Test: `backend/tests/test_fallback_preferences.py`

**Interfaces:**
- Produces:
  - `preferences.get_capability_fallbacks(cap_id: str) -> list[str]`：返回清洗后的兜底列表，已去重、已剔除主源、只保留已注册且声明了该能力的源；字段缺失时取默认值。
  - `capabilities.DEFAULT_FALLBACK_ORDER: dict[str, list[str]] = {"depth5": ["tdxquant", "tickflow", "meoz"], "overseas": ["cn_overseas", "yahoo_overseas"]}`；默认值 = 该顺序中除主源外、已声明该能力的源。
  - API：`PUT /api/settings/preferences/data-providers` 接受 `depth5_data_provider_fallbacks: list[str] | None`、`overseas_data_provider_fallbacks: list[str] | None`；`[]` 被保存为空列表（关闭兜底），`None` 表示不修改。

- [ ] **Step 1：写失败测试**

```python
def test_missing_field_uses_registry_default(monkeypatch):  # 主源 tdxquant, 三源都声明 depth5
    assert get_capability_fallbacks("depth5") == ["tickflow", "meoz"]

def test_fallbacks_dedupe_and_drop_primary(monkeypatch):    # 存储值 ["meoz","tdxquant","meoz"], 主源 tdxquant
    assert get_capability_fallbacks("depth5") == ["meoz"]

def test_unknown_or_undeclared_source_dropped(monkeypatch): # 存储 ["ghost","fuyao","meoz"]; fuyao 未声明 depth5
    assert get_capability_fallbacks("depth5") == ["meoz"]

def test_empty_list_disables(monkeypatch):
    assert get_capability_fallbacks("depth5") == []

def test_non_fallback_capability_returns_empty():
    assert get_capability_fallbacks("daily") == []

def test_api_saves_and_returns_fallbacks(client, monkeypatch):  # PUT 后 GET preferences 含该列表
def test_api_rejects_pending_remove_in_fallbacks(client, monkeypatch):  # 400, 与主源同语义
```

- [ ] **Step 2：** 跑测试，预期 FAIL。
- [ ] **Step 3：** 实现 getter（复用 `_allowed_data_providers` 与 `custom.provider_has_dataset`；`tickflow` 视为已声明 depth5、未声明 overseas）。改 API 模型与卸载检查：遍历值时展开 list。
- [ ] **Step 4：** 跑 `tests/test_fallback_preferences.py tests/test_overseas_routing.py tests/test_capability_matrix.py tests/test_preferences_cache.py -q`，预期 PASS。
- [ ] **Step 5：** 提交 `feat(routing): depth5/外围行情的兜底顺序偏好`。

---

### Task 3：`routing_health` 调度核心

**Files:**
- Create: `backend/app/data_providers/routing_health.py`
- Test: `backend/tests/test_routing_health.py`

**Interfaces:**
- Consumes：Task 1 错误类型；Task 2 `get_capability_fallbacks`；`resolve_capability_sources`（candidates 口径）；`registry.get_provider("tickflow")`、`custom.get_provider(name)`。
- Produces:

```python
@dataclass(frozen=True)
class FallbackResult:
    data: object | None
    source: str | None          # 实际提供数据的源; 全链失败为 None
    degraded: bool              # source != 主源 (全链失败也为 True)
    reason: str | None          # 主源未被使用的原因 / 全链失败的汇总
    attempts: tuple[tuple[str, str], ...]   # (源, "ok"|"auth"|"quota"|"data"|"error"|"open"|"unresolved")

def effective_chain(cap_id: str) -> list[str]           # [主源]+fallbacks, 过滤当前 candidates
def pick_source(cap_id: str) -> str | None               # 第一个可用(healthy 或抢到半开探测)的源, 不调用
def resolve_provider(name: str) -> object                # tickflow→registry, 其它→custom; 失败抛 LookupError
def record(cap_id: str, source: str, outcome: BaseException | None) -> None   # None=成功
def call_with_fallback(cap_id: str, method: str, *args,
                       validate: Callable[[object], bool] | None = None,
                       invoke: Callable[[str, Callable[[], object]], object] | None = None,
                       **kwargs) -> FallbackResult
    # invoke(源名, 无参调用) 用于包装每次源调用 (例如按源缓存); 缺省直接调用
def chain_status(cap_id: str) -> list[dict]   # [{source, state: "healthy"|"open"|"half_open", reason, until: iso|None}]
def reset(cap_id: str | None = None, source: str | None = None) -> None
```

- 状态键为 `(cap_id, source)`，用一把 `threading.Lock` 保护。
- `record` 的分类：
  - `ProviderAuthError` → open，until=None；
  - `ProviderQuotaError` → open 到北京时间次日 00:00 和 `now + retry_after` 中较早者；
  - `ProviderDataError` 及其它异常 → 连续失败数 +1，达到 3 时 open 300 秒；
  - 成功 → 清零并 healthy。
- 熔断到期 → half_open：只放行一个探测，其余调用者视为 open；探测成功 → healthy，失败 → 再 open 300 秒（认证、配额按各自规则）。
- `call_with_fallback` 不会把异常抛给调用方；`validate` 返回 False 时按 `ProviderDataError` 记录。
- `resolve_provider` 失败、或方法不存在 → attempts 记 `"unresolved"`，**不**改变健康状态。
- 状态迁移打一行 `INFO` 日志，用能力 label 和源展示名。

- [ ] **Step 1：写失败测试**（用假 provider 和打桩的 `cn_now`，不触网）

```python
def test_primary_ok_not_degraded():
    r = call_with_fallback("depth5", "get_depth_batch", ["A"])
    assert (r.source, r.degraded, r.reason) == ("tdxquant", False, None)

def test_auth_error_switches_and_opens_until_reset():
    # tdxquant 抛 ProviderAuthError → 结果来自 tickflow, degraded=True
    # 第二次调用不再调用 tdxquant; reset("depth5","tdxquant") 后再次调用 tdxquant

def test_three_transient_failures_open_300s_then_half_open():
    # 前两次失败仍先试 tdxquant; 第 3 次后 open; cn_now +299s 仍跳过, +301s 进入 half_open
    # half_open 探测成功 → healthy; 另测探测失败 → 再 open 300s

def test_quota_open_until_beijing_midnight():
    # cn_now = 2026-11-18 23:59:00 抛 ProviderQuotaError(retry_after=None)
    # until == 2026-11-19 00:00:00 (北京); 00:00:01 时 half_open

def test_quota_retry_after_shorter_wins():
    # 10:00 抛 ProviderQuotaError(retry_after=60) → until 10:01

def test_validate_false_counts_as_data_failure():

def test_all_open_returns_without_calling():
    # 所有源 open → data None, source None, degraded True; 各 provider 调用次数 0

def test_half_open_admits_single_probe():
    # 两线程同时 pick_source, 只有一个拿到半开源, 另一个拿到下一个源

def test_chain_skips_source_no_longer_candidate():
    # fallbacks 含 meoz, 但 meoz 已不在 candidates → effective_chain 不含 meoz

def test_unresolved_provider_does_not_open_breaker():

def test_empty_fallbacks_equals_current_behavior():
    # 主源失败 → data None, attempts 只有主源

def test_invoke_wrapper_receives_source():
    # invoke 记录收到的源名: 主源失败后依次收到 "tdxquant", "tickflow"

def test_reason_never_contains_key(monkeypatch):
    # 异常消息含 "apikey=SECRET" → reason 不含 "SECRET"(reason 只用分类文案+源名, 不拼异常原文)
```

- [ ] **Step 2：** 跑 `tests/test_routing_health.py -q`，预期 FAIL。
- [ ] **Step 3：** 实现模块。`reason` 文案只由分类和源展示名拼出，例如「tdxquant：认证失败」「tdxquant：连续 3 次失败，熔断至 10:35」「meoz：调用额度用尽，熔断至次日 00:00」；不拼接异常原文，详细异常只进 `logger.warning(..., exc_info=False)`，且只记录异常类型名。
- [ ] **Step 4：** 跑测试，预期 PASS。
- [ ] **Step 5：** 提交 `feat(routing): 兜底链调度与熔断`。

---

### Task 4：能力矩阵展示 + 重试接口 + Key 变更重置

**Files:**
- Modify: `backend/app/data_providers/capabilities.py`（`build_capability_matrix`、`resolve_capability_sources`、`capability_gate`）
- Modify: `backend/app/api/settings.py`（`get_capability_matrix` 注入 fallbacks；新增 reset 路由；`save_plugin_key`、`clear_plugin_key` 成功后调用 `routing_health.reset(source=name)`）
- Test: `backend/tests/test_capability_matrix.py`（追加）、`backend/tests/test_fallback_api.py`

**Interfaces:**
- Consumes：Task 2、Task 3。
- Produces：
  - 矩阵中 `fallback=True` 的能力行新增 `fallbacks: list[str]`、`chain: list[dict]`（即 `chain_status`）、`active_source: str | None`、`degraded: bool`、`fallback_reason: str | None`；其它能力行不新增这些键。
  - `usable` 对兜底能力改为 `effective_chain(cap_id)` 非空。
  - `build_capability_matrix(current, tickflow_tier, fallbacks: dict[str, list[str]] | None = None)`：新参数缺省时等同空列表，保持模块不反向依赖 services。
  - `capability_gate` 降级时标签为 `f"{label} ← {active 展示名}（兜底：{fallback_reason}）"`。
  - `POST /api/settings/capability/{cap_id}/reset`，请求体可选 `{"source": str}` → 返回该能力新的矩阵行；未知能力或非兜底能力返回 404。

- [ ] **Step 1：写失败测试**

```python
def test_matrix_depth5_exposes_chain_and_degraded():
    # 打桩 chain_status: tdxquant open → active_source "tickflow", degraded True, fallback_reason 非空

def test_matrix_non_fallback_capability_has_no_chain_keys():
    assert "chain" not in row_of("daily")

def test_usable_true_when_primary_unavailable_but_fallback_candidate():
    # 主源 tdxquant 未就绪(pending), fallbacks ["meoz"] 可用 → usable True

def test_gate_label_shows_fallback():

def test_reset_endpoint_clears_breaker(client):
def test_reset_unknown_capability_404(client):
def test_save_plugin_key_resets_breakers(client, monkeypatch):
```

- [ ] **Step 2：** 跑测试，预期 FAIL。
- [ ] **Step 3：** 实现。三处显式列出的 `current` 字典保持不变，另加 `fallbacks={"depth5": ..., "overseas": ...}` 注入。
- [ ] **Step 4：** 跑 `tests/test_capability_matrix.py tests/test_capability_augment.py tests/test_fallback_api.py tests/test_overseas_routing.py -q`，预期 PASS。
- [ ] **Step 5：** 提交 `feat(routing): 能力矩阵展示兜底状态与重试主源`。

---

### Task 5：depth5 整轮切换

**Files:**
- Modify: `backend/app/services/depth_service.py`（`_call_depth_batch`、sealed 缓存、`_persist`、`get_order_books`）
- Test: `backend/tests/test_depth_fallback.py`；更新 `backend/tests/test_custom_depth_provider.py` 中断言「不跨源回退」的用例

**Interfaces:**
- Consumes：Task 3 的 `pick_source`、`resolve_provider`、`record`、`effective_chain`。
- Produces：
  - `_call_depth_batch(symbols) -> dict`：签名不变，每个条目新增 `"source": str`。
  - 新增 `self._sealed_source: str | None`。
  - depth5 落盘 parquet 新增 `source: Utf8` 列。

算法（调用方逻辑，测试无法唯一确定，故写出）：

```text
tried = set()
loop:
    src = pick_source("depth5") 且 src 不在 tried, 否则结束返回 {}
    tried.add(src)
    provider = resolve_provider(src)  (失败 → 继续 loop, 不 record)
    按现有分批/限速取全部批次 → result, failed_batches, total_batches
    if total_batches > 0 且 (failed_batches == total_batches 或 len(result) == 0):
        record(src, 该轮最后一个异常 或 ProviderDataError("empty")); continue   # 同轮换下一个源重跑
    record(src, None if failed_batches == 0 else ProviderDataError("partial"))
    给 result 每项加 source=src; return result
```

- 「部分失败」也调用 `record(..., ProviderDataError)` 计一次失败，但不换源。
- `_request_lock` 节流在不同源之间共享（保守），不改。

- [ ] **Step 1：写失败测试**

```python
def test_primary_raises_all_batches_switches_same_round():
    # tdxquant 每批抛异常, meoz 正常 → 结果全部 source=="meoz"; tdxquant 调用了所有批

def test_primary_returns_empty_switches():
    # tdxquant 返回 {} (模拟凭据失效被吞) → 用 meoz

def test_partial_failure_keeps_primary_no_mixing():
    # tdxquant 第 2 批抛异常 → 结果只含其余批, 全部 source=="tdxquant"; meoz 未被调用

def test_all_sources_fail_returns_empty():

def test_breaker_opened_primary_skipped_next_round():
    # 连续 3 轮整轮失败后, 第 4 轮不再调用 tdxquant

def test_persist_writes_source_column(tmp_path):

def test_order_books_entries_carry_source():
```

- [ ] **Step 2：** 跑测试，预期 FAIL。
- [ ] **Step 3：** 按算法改 `_call_depth_batch`；sealed 拉取处记录 `_sealed_source`；`_persist` 的 schema 加 `"source": pl.Utf8`。改写 `test_custom_depth_provider.py` 里与新语义冲突的断言（只改断言，不删用例），并在提交说明里列出。
- [ ] **Step 4：** 跑 `tests/test_depth_fallback.py tests/test_custom_depth_provider.py tests/test_depth_continuous_trading.py -q` 以及 `uv run pytest -q -k depth`，预期 PASS。
- [ ] **Step 5：** 提交 `feat(depth5): 主源整轮失败时同轮切换兜底源`。

---

### Task 6：外围行情按请求切换

**Files:**
- Modify: `backend/app/services/overseas_indices.py`（`_read`）
- Modify: `backend/app/services/market_game_overseas.py`（`collect_overseas_quotes`，改写 docstring 与「未替换其他来源」文案）
- Test: `backend/tests/test_overseas_fallback.py`

**Interfaces:**
- Consumes：Task 3 `call_with_fallback`。
- Produces：
  - `overseas_indices._read` 返回 `{"source", "rows", "degraded", "fallback_reason", ["reason"]}`，`source` 为实际来源。
  - `collect_overseas_quotes()` 返回值新增 `degraded: bool`、`fallback_reason: str | None`，`source` 为实际来源。
  - API `/api/indices/*` 透传这两个字段（`api/indices.py` 只需确认展开不丢键）。

规则：
- 指数报价的 `validate`：返回值是 list。
- 日线的 `validate`：返回值是 list。空 list 视为有效（休市日），不触发切换。
- 博弈外围行情的 `validate`：返回 dict 且 `quotes` 是 list。
- 按源缓存：`overseas_indices` 向 `call_with_fallback` 传入 `invoke=lambda src, fn: _work.run((src, method, *args), fn, ttl_s=ttl)`，缓存键里包含实际尝试的源。

- [ ] **Step 1：写失败测试**

```python
def test_index_quotes_fall_back_on_exception():
    # yahoo 抛异常 → source "cn_overseas", degraded True

def test_daily_empty_list_does_not_switch():
    # yahoo 返回 [] → source "yahoo_overseas", degraded False, cn_overseas 未调用

def test_daily_one_series_one_source():

def test_market_game_quotes_fall_back_and_label_source():

def test_market_game_invalid_shape_counts_failure_and_falls_back():
    # yahoo 返回 {"quotes": None} → 用 cn_overseas

def test_cache_key_separates_sources():
```

- [ ] **Step 2：** 跑测试，预期 FAIL。
- [ ] **Step 3：** 实现。
- [ ] **Step 4：** 跑 `tests/test_overseas_fallback.py tests/test_overseas_routing.py tests/test_routing_health.py -q` 以及 `uv run pytest -q -k "overseas or market_game"`，预期 PASS。
- [ ] **Step 5：** 提交 `feat(overseas): 外围行情按请求沿兜底链切换`。

---

### Task 7：前端：兜底顺序、降级提示、来源标注

**Files:**
- Modify: `frontend/src/lib/api.ts`（`CapabilityRoute` 加可选字段；`updateDataProviders` 允许 fallbacks；新增 `resetCapability(capId, source?)`；指数与博弈外围返回类型加 `degraded`、`fallback_reason`）
- Modify: `frontend/src/lib/provider-routing.ts`（如需在 `ProviderField` 之外加 fallbacks 字段类型）
- Modify: `frontend/src/pages/settings/DataSources.tsx`（`CapabilityCard`）
- Modify：【指数】页海外来源标注、短线博弈外围区来源标注所在组件（用 `grep -rn "overseas" frontend/src/pages` 定位，改动只限来源文字）
- Test: `frontend/src/pages/settings/DataSources.test.tsx`（新建）

**Interfaces:**
- Consumes：Task 4 矩阵字段与 reset API；Task 6 返回字段。
- Produces（类型）：

```ts
export interface CapabilityChainEntry { source: string; state: 'healthy' | 'open' | 'half_open'; reason: string | null; until: string | null }
// CapabilityRoute 追加:
fallbacks?: string[]; chain?: CapabilityChainEntry[]; active_source?: string | null; degraded?: boolean; fallback_reason?: string | null
```

`CapabilityCard` 的 UI（仅当 `cap.fallbacks !== undefined` 时渲染）：
- 主源选择保持不变。
- 下方一行「兜底」：按顺序列出已选的兜底源标签，每个带上移、下移和 × 按钮；后面跟「+」下拉，列出其余候选。每次改动立即调用 `updateDataProviders`，然后刷新矩阵。
- `degraded` 时卡片顶部显示琥珀色一行「当前由 {active 展示名} 兜底：{fallback_reason}」，右侧「重试主源」按钮。
- `chain` 明细（每个源的状态和恢复时间）放进现有展开区，不常驻。
- 外部页面：`degraded` 时把来源文字显示为「{展示名}（兜底）」，`title` 显示原因。

- [ ] **Step 1：写失败组件测试**

```tsx
it('renders fallback order and saves on move', ...)        // 点「下移」→ updateDataProviders 收到 {depth5_data_provider_fallbacks: [...新顺序]}
it('shows degraded banner and calls reset', ...)            // degraded=true → 文案含「当前由 猫爪数据 兜底」; 点「重试主源」→ resetCapability('depth5')
it('does not render fallback row for non-fallback capability', ...)
```

- [ ] **Step 2：** `cd frontend && pnpm exec vitest run src/pages/settings/DataSources.test.tsx`，预期 FAIL。
- [ ] **Step 3：** 实现。
- [ ] **Step 4：** 跑上面的测试，以及 `pnpm exec vitest run src/lib/provider-routing.test.ts` 和 `pnpm build`，预期 PASS。
- [ ] **Step 5：** 页面验证：按记忆中的「临时数据目录 E2E 预览」方式（设置 `DATA_DIR` 和 `TICKFLOW_ENV_FILE`）启动。在设置页调整兜底顺序；打桩或断开主源后，确认出现提示、「重试主源」可用；截图留证。
- [ ] **Step 6：** 提交 `feat(settings): 能力卡片兜底顺序与降级提示`。

---

### Task 8：文档同步与整体验证

**Files:**
- Modify: `CONTRIBUTING.md` §4「能力路由矩阵契约」中英文两版：
  - 把「`depth5` 独立路由且失败时不跨源回退」改为「`depth5`、`overseas` 支持兜底链：轮内（单次请求内）不跨源，轮间按健康状态切换；其余能力不跨源回退」；
  - 新增一条：中立错误类型及其熔断语义。
- Modify: `docs/plugin-development.md`：插件可抛出 `ProviderAuthError`、`ProviderQuotaError`、`ProviderDataError`（或其子类），以及各自的含义；未识别异常按瞬时错误处理。

- [ ] **Step 1：** 改文档。
- [ ] **Step 2：** 后端回归：`cd backend && uv run pytest -q`，预期全部 PASS；失败用例如与本改动无关，记录下来，不在本分支修。
- [ ] **Step 3：** `uv run ruff check` 覆盖所有改动文件；在仓库根目录跑 `git diff --check`。
- [ ] **Step 4：** 前端 `pnpm build`。
- [ ] **Step 5：** 提交 `docs: 能力兜底链契约与插件错误类型`。
- [ ] **Step 6：交易时段切换演练（阶段 2 验收，手动）**：
  1. depth5 主源设为 tdxquant，兜底为 `[meoz]`；
  2. 在设置页清除 tdxquant Key（或拔掉其依赖）；
  3. 确认下一轮梯队封单由 meoz 接管、日志出现熔断行、设置页显示「当前由 猫爪数据 兜底」；
  4. 恢复 Key 后点「重试主源」，确认切回。
  记录时间点与截图。此步不能用单测替代；未执行就在交付说明里明确标注「未演练」。
