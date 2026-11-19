# 能力兜底链设计（第一版：depth5 + 外围行情）

> 日期：2026-11-18
> 依据：`docs/猫爪数据对接/猫爪数据对接规划.md` 第 5 节、阶段 2 验收「depth5 切换演练」、本仓库现有代码。

## 1. 意图

现在每个能力只能路由到一个源，主源失效（Key 过期、限频、网络故障）时，相关功能只能停摆，直到用户手动改路由。

兜底链要做到三件事：
- 主源失效时，**自动**切到同一能力的下一个可用源；
- 切换**不混用口径**：同一份数据只来自一个源；
- 切换**可见、可追溯**：设置页能看出当前在兜底及原因，返回的数据带实际来源。

它是阶段 2 中「depth5 猫爪兜底」「龙虎榜双源」的前提，也是「depth5 切换演练」的验收对象。

## 2. 目标、非目标、反目标

**目标**
- depth5、外围行情（overseas）两个能力接入兜底链。
- 一个通用的中心调度机制，后续接参考数据（`price_limits` 等）、龙虎榜、实时、日K 时，不用修改机制本身。
- 按错误类型熔断，熔断状态在能力矩阵和设置页可见。

**非目标（本版不做）**
- 实时、日K、分钟、除权因子、财务接入兜底链（阶段 4，单独评审）。
- `price_limits` / `st_status` / `limit_events` 接入：目前只有猫爪一个候选，缺数据时读取方已会回退本地推算，等有第二个源时再接。
- 龙虎榜接入：先要有猫爪龙虎榜适配器，并把 `dragon_tiger.py` 写死的 fuyao 迁入路由矩阵。
- 熔断状态持久化、跨进程共享。
- 按能力的调用配额计数与告警（规划第 6 节，另做）。

**反目标（出现即视为失败）**
- 同一轮 depth5 结果里混有两个源的数据。
- 兜底悄悄发生，页面和日志看不出来。
- 某个源失效后，把正常的源也拖慢（例如每轮都先等失效源超时）。
- 插件抛出未知异常导致服务崩溃或其它能力不可用。

## 3. 约束

- CONTRIBUTING §4「能力路由矩阵契约」：矩阵是单一权威，各页门控以 `usable` 为准，能力层中立（不出现 TickFlow 档位词汇）。
- CONTRIBUTING §4 现有条款「`depth5` 独立路由且失败时不跨源回退」**需修改**为「轮内不跨源、轮间可切换」，随本改动同步更新。
- 短线博弈外围行情 `market_game_overseas.collect_overseas_quotes` 目前显式「never try another source」。本设计改为允许兜底，并在结果中标注实际来源（见 5.6）。
- Key 不落盘、不打印；熔断原因文本不得包含 Key。
- 修改 preferences 结构时，存量配置无 `*_fallbacks` 字段要能自愈（用注册表默认值），不做迁移。

## 4. 方案比较

| 方案 | 做法 | 取舍 | 结论 |
|---|---|---|---|
| **A. 中心调度** | 新增 `routing_health` 模块，提供 `call_with_fallback`：按兜底链找第一个健康源、调用、按错误类型记录熔断，返回数据 + 实际来源 + 降级信息 | 熔断、来源标记、矩阵展示只写一份；需要定义中立错误类型 | **采用**（用户选定） |
| B. 各服务自己循环 | 健康状态共用，链遍历在三个服务里各写一遍 | 每处粒度可精确控制，但重复三份，后续能力继续复制 | 否 |
| C. 组合 provider | 把链包成一个假 provider，服务零改动 | 切换不可见；depth5 多批时可能轮内混源，违反反目标 | 否 |

## 5. 设计

### 5.1 配置

- 注册表每个能力新增可选键 `fallback: bool`。本版只有 `depth5`、`overseas` 为 `True`；其余能力行为不变。
- preferences 新增 `<field>_fallbacks: list[str]`，例如 `depth5_data_provider_fallbacks`、`overseas_data_provider_fallbacks`。
- **生效链** = `[主源] + fallbacks`，去重；过滤掉不在当前 candidates 中的源（未声明、未就绪、TickFlow 档位不足）。
  - 主源仍是现有 `<field>` 单选，保持设置页和存量配置兼容。
- 默认 fallbacks（getter 在字段缺失或非法时自愈为默认，非法项剔除并告警）：
  - `depth5`：所有其它已声明 depth5 的候选，按 `tdxquant, tickflow, meoz` 的顺序；
  - `overseas`：`cn_overseas, yahoo_overseas` 中除主源以外的那个。
- 用户可以在设置页调整顺序、移除某个兜底源；**空列表 = 关闭兜底**（等价于现状）。

### 5.2 中立错误类型

`app/data_providers/errors.py`（新增）：

| 类型 | 含义 | 熔断 |
|---|---|---|
| `ProviderAuthError` | 401/403，Key 失效或权限不足 | 熔断该源的该能力，直到 Key 变更或用户点「重试」 |
| `ProviderQuotaError` | 429 或日配额耗尽 | 熔断到次日 00:00（北京时间）；带 `retry_after` 时取较短者 |
| `ProviderDataError` | 返回空、格式不符、校验失败 | 当次视为失败，计入连续失败 |
| 其它任何异常 | 视为超时 / 5xx 类瞬时错误 | 连续 3 次失败后熔断 5 分钟，到期后半开：放行一次探测，成功即恢复，失败再熔断 5 分钟 |

- 插件可直接抛出这些类型或其子类。本版改造：
  - 猫爪：`MeozAuthError` 同时继承 `ProviderAuthError`；HTTP 429 的 `MeozError(code=429)` 改为抛出继承 `ProviderQuotaError` 的子类。
  - tdxquant、TickFlow：本版不改插件，它们的失败表现为异常或空结果，由调度层按「其它异常」或 `ProviderDataError` 处理。
- `MeozTruncatedError` 不属于源失效，仍由插件内部拆分重试，不应冒泡到调度层。

### 5.3 `routing_health` 模块

`app/data_providers/routing_health.py`（新增），进程内单例，线程安全。

```text
call_with_fallback(cap_id, method, *args, validate=None, **kwargs) -> FallbackResult
  FallbackResult: data, source, degraded: bool, reason: str | None, attempts: [(source, outcome)]

chain_status(cap_id) -> [{source, state: healthy|open|half_open, reason, until}]
reset(cap_id, source=None)        # 设置页「重试」、Key 变更时调用
```

- 源实例的解析统一在这里：`tickflow` 走 `registry.get_provider("tickflow")`，其它走 `custom.get_provider(name)`，并检查方法存在；解析失败视为该源本次不可用（不熔断）。
- 按生效链依次尝试；跳过熔断中（open）的源；半开（half_open）的源只放行一个探测请求，并发的其它调用视其为熔断中。
- 时间一律用北京时间（`cn_now()`），不用本机时钟。
- `validate(data) -> bool` 由调用方提供，返回 False 视为 `ProviderDataError`。
- 链上所有源都失败或都熔断时，返回 `data=None`、`source=None`、`degraded=True`，`reason` 汇总各源原因；调用方按现状处理（跳过本轮、显示不可用）。
- `degraded` = 实际来源不是主源。`reason` 是主源（及之前的源）未被使用的原因，例如「tdxquant：连续 3 次失败，熔断至 10:35」。
- Key 变更：设置页保存某插件 Key 成功后，调用 `reset(source=该插件)` 清除该源在所有能力上的熔断。
- 日志：每次状态迁移（healthy→open、open→half_open、half_open→healthy）记一行 `INFO`，形如「五档盘口 ← tdxquant 熔断 5 分钟：连续 3 次失败」。

### 5.4 能力矩阵与设置页

- `build_capability_matrix` / `resolve_capability_sources` 对 `fallback=True` 的能力新增：
  - `fallbacks`：用户配置的兜底列表；
  - `chain`：生效链及每个源的健康状态（来自 `chain_status`）；
  - `active_source`：链上第一个健康源；
  - `degraded`、`fallback_reason`。
- `usable` 改为「生效链中存在 candidates 内的源」。只要有一个兜底源可用，页面门控就不再提示缺能力。
- `capability_gate` 的日志标签在降级时形如「五档盘口 ← 猫爪数据（兜底：tdxquant 熔断）」。
- 设置页能力路由卡片（`frontend/src/pages/settings/DataSources.tsx`）：
  - 主源下拉保持不变；
  - 下方新增「兜底顺序」：已勾选的候选源可拖动或上下移排序，可取消勾选；
  - 降级时卡片顶部显示一行「当前由 X 兜底：原因」，旁边有「重试主源」按钮（调用 `reset`）。
  - 遵循 CONTRIBUTING §7 信息精简：熔断明细（每个源的状态和恢复时间）放进展开区。
- 新增 API：`POST /api/settings/capability/{cap_id}/reset`。保存 fallbacks 复用现有偏好保存接口。

### 5.5 depth5：整轮切换

`depth_service._call_depth_batch` 改造：

1. 轮开始时通过 `routing_health` 选出本轮源（链上第一个健康或半开的源）。
2. 用该源按现有分批、限速逻辑取完整轮。
3. 判定本轮结果：
   - **整轮失败**：所有批次都抛异常，或请求非空却拿到 0 只 → 记一次失败；**立即用链上下一个源重跑整轮**。每轮最多尝试链长度次。
   - **部分成功**：保留已拿到的数据，记一次失败（但不立即换源），避免轮内混源。
   - **成功**：记成功，清零连续失败计数。
4. 结果的每个条目加 `source` 字段；`sealed` 缓存记录 `_sealed_source`，落盘 parquet 新增 `source` 列。
   - 新增列对旧文件读取兼容：读取方按「列不存在 = 未知来源」处理。
5. `get_order_books`（做T盘口）走同一路径，返回值带 `source`。

限速：每个源的 `resolve_limit(capset, DEPTH5_BATCH)` 目前按 TickFlow capset 取值。兜底源沿用同一组限速参数（保守）。按源分别限速作为已知遗留问题，不在本版处理。

> 为什么「请求非空却拿到 0 只」算失败：tdxquant 的 `get_depth_batch` 会吞掉批次异常，凭据失效时整轮返回空 dict，不抛异常。不把空结果算失败，tdxquant 失效就永远不会触发切换。depth5 只在交易时段轮询，交易时段内请求涨跌停股票的盘口不应为空。

### 5.6 外围行情：按请求切换

三个调用点都改用 `call_with_fallback("overseas", method, ...)`：

| 调用点 | 方法 | 校验 |
|---|---|---|
| `overseas_indices.get_quotes` | `get_overseas_index_quotes` | 返回 list |
| `overseas_indices.get_daily` | `get_overseas_index_daily(symbol, start, end)` | 返回 list；一只指数的一段日线只来自一个源 |
| `market_game_overseas.collect_overseas_quotes` | `get_overseas_quotes` | 返回 dict 且 `quotes` 为 list |

- `overseas_indices` 的 `SharedWork` 缓存键已含 `source`，切换源后不会命中旧源的缓存。
- 返回体沿用已有的 `source` 字段（改为实际来源），新增 `degraded`、`fallback_reason`。
- 【指数】页和短线博弈外围区：降级时在来源标注处显示「X（兜底）」，悬停显示原因。
- `market_game_overseas` 的 docstring 和提示文案「未替换其他来源」相应改写。
- 「空结果」：外围行情的空 list 不算失败（休市日可能为空），只有异常和格式不符才算。

### 5.7 文档同步

- CONTRIBUTING §4 能力路由矩阵契约：改写 depth5 不跨源回退的条款，新增兜底链与中立错误类型的说明。中英文两版都要改。
- `docs/plugin-development.md`：插件可抛出的中立错误类型及其熔断含义。

## 6. 测试

- `routing_health` 单测（mock provider，不触网）：
  - auth → 熔断直到 reset；
  - quota → 熔断到次日 00:00；
  - 连续 3 次瞬时错误 → 熔断 5 分钟 → 半开探测成功恢复、失败再熔断；
  - `validate` 返回 False → 计为失败；
  - 全链失败 → `data=None` 且 reason 汇总；
  - 空 fallbacks → 行为等同现状。
- preferences：字段缺失或含非法源时的自愈。
- 能力矩阵：`fallbacks`、`chain`、`active_source`、`degraded`、`usable` 新口径。
- depth5：主源整轮抛异常 → 同轮切到下一个源；主源返回空 → 切换；部分批次失败 → 不切换、不混源；结果和落盘带 `source`。
- 外围行情：主源异常 → 用兜底源，`source` 为实际来源、`degraded=True`；空 list 不触发切换。
- 猫爪错误映射：401/403 → `ProviderAuthError` 子类，429 → `ProviderQuotaError` 子类。
- 前端：兜底顺序编辑、降级提示的组件测试；设置页手动验证。
- **切换演练**（阶段 2 验收）：交易时段让 tdxquant 凭据失效（或 mock），确认下一轮 depth5 由下一个源接管、梯队封单不中断、日志和设置页可见；恢复后「重试主源」能切回。

## 7. 留给实现者决定

- `routing_health` 内部数据结构、锁粒度。
- 设置页「兜底顺序」的具体交互（拖动还是上下按钮），以现有设置页组件风格为准。
- 「连续 3 次」「熔断 5 分钟」写成模块常量，不做成用户配置。

## 8. 我替用户做的决定（请审阅）

1. 熔断状态只在内存，重启清空。
2. depth5 整轮失败时在同一轮内立即换源重跑；部分失败不换源。
3. depth5「请求非空返回 0 只」视为失败。
4. 短线博弈外围行情也走兜底，结果标注实际来源。
5. 主源仍是原单选字段，兜底列表是另一个字段；空列表即关闭兜底。
6. 兜底源沿用主源的限速参数，按源限速留到以后。
7. 本版只改猫爪插件的错误类型，tdxquant、TickFlow 不改。
