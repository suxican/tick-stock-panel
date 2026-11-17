# 开盘啦盘中情绪/连板数据接入设计

日期：2026-11-16
状态：草案，待用户审阅

## 1. 意图

盘中盯短线时，现有「连板梯队」页能实时看到谁涨停、谁炸板、谁断板，但**不知道为什么涨停**——涨停原因只有 zzshare 的盘后数据，收盘后才有。开盘啦 App 在盘中就给出涨停原因、题材归属、全量封单，以及它自己的情绪值和晋级率。

目标：把开盘啦的盘中数据**补进现有的连板梯队页**，让盘中就能看到涨停原因、题材集中度和第三方情绪口径，并把每天的盘中观测留档，供复盘和日后口径校准。

数据来源是用户提供的非官方接口文档 `E:\股市学习\开发相关\开盘啦 API 接口文档\开盘啦 API 接口文档.md`（抓包整理，无官方背书）。

## 2. 现状（与本设计相关）

| 能力 | 位置 | 说明 |
|---|---|---|
| 连板梯队页 | `frontend/src/custom/limit-ladder/Page.tsx`；后端 `GET /api/screener/limit-ladder`（`backend/app/api/screener.py`） | 按连板数分档，涨停/炸板/断板三状态；SSE 前缀失效实现盘中刷新；`SealedBadge` 封单来自五档订阅池 |
| 涨停原因 | `frontend/src/lib/useZzshareReasons.ts` 读 ext_data 表 `zzshare_limit_reasons` | 盘后导入，按 `symbol → reason` 叠加到梯队卡片 |
| 连板/晋级/周期 | `consecutive_limit_ups` 列；`backend/app/services/market_phase.py` | 本地口径，全历史可回算 |
| 非数据源型外部客户端范式 | `backend/app/plugins/cninfo/`（无 `plugin.yaml`，不注册为可切换数据源） | 本设计照此范式 |
| 扩展路由 + 定时采集范式 | `backend/app/custom/zzshare.py`（`_lifespan` 里向 `app.state.scheduler` 注册 cron；`in_continuous_session` + `is_trading_day` 门控） | 本设计照此范式 |
| 盘中观测留档范式 | `backend/app/services/zzshare_home.py`（`_shared` 合并请求 + TTL 缓存，`atomic_write_text` 落盘） | 本设计照此范式 |

## 3. 目标 / 非目标 / 反目标

**目标**
1. 连板梯队页（涨停方向、当日）盘中显示开盘啦涨停原因，与 zzshare 盘后原因并存，标明来源。
2. 页头增加一条「开盘啦情绪条」：情绪值、涨停数、最高板、破板率、晋级率（能拿到哪个显示哪个）。
3. 题材集中度：按开盘啦题材归属汇总当日涨停数，可点题材筛选梯队。
4. 每个交易日留存盘中观测与收盘定版快照，可按日期回看。
5. 默认关闭，开启后失败时明确提示，不影响任何其他功能。

**非目标**
- 不新建独立「短线情绪」页面（现有梯队页已覆盖梯队与断板名单）。
- 不替换本地 `consecutive_limit_ups`、`market_phase`、`emotion_score` 的口径。
- 不接需要 `Token`/`UserID` 的接口（竞价类、板块竞价）。
- 不做历史回填（`apphis` 历史接口批量拉取）。
- 第一期不做首板策略、看板总览联动（见 §9 第二期）。

**反目标（出现即视为失败）**
- 开盘啦数据进入回测、因子、策略信号或监控规则判断。
- 开盘啦不可用时，梯队页或其他页面报错、空白或悄悄改用别的口径而不提示。
- 无人查看时仍高频请求开盘啦。

## 4. 方案比较

| 方案 | 做法 | 取舍 |
|---|---|---|
| **A. 扩展插件 + 补进现有梯队页（采用）** | `plugins/kaipanla` 客户端 + `custom/kaipanla.py` 路由与采集 + 梯队页叠加 | 复用 cninfo/zzshare 现成范式，L2 级改动，隔离好；用户已选定 |
| B. 新数据集进能力矩阵 | 定义「涨停池」数据集契约，provider 路由 | 需改核心契约（L3），对非官方源代价过高 |
| C. 只做盘后存档 | 只接 `apphis` 历史接口 | 拿不到实时，背离意图 |
| ~~新建独立短线页~~ | — | 调研发现梯队页已存在，属于平行实现，放弃 |

## 5. 设计

### 5.0 第 0 步：接口实测（已完成，结论与修正见 §10，**以 §10.5 为准**）

一次性脚本（不提交，放 `scratch/` 或临时目录）在**交易时段**请求下表接口，录下响应作为测试 fixture：

| 用途 | 地址 | `c` / `a` |
|---|---|---|
| 各板位涨停数 | `apphwhq` | `HomeDingPan` / `DailyLimitIndex` |
| 涨停列表（`PidType` 1–5, `Type=4`） | `apphwhq` | `HomeDingPan` / `DailyLimitPerformance` |
| 涨停天梯（题材归属） | `apphwhq` | `FuPanLa` / `GetZhangTingTianTi` |
| 情绪值 | `apphis`，并试 `apphwhq` + `HomeDingPan` | `ChangeStatistics` |
| 晋级率/破板率 | `apphis`，并试 `apphwhq` + `HomeDingPan` | `ZhangTingExpression` |

需回答：
1. 今日接口盘中是否实时变化（隔 1 分钟两次请求比对）。
2. 情绪值、晋级率能否拿到**当日盘中**值；拿不到则这两项只在收盘后展示。
3. 单次耗时、连续请求是否被限流（不做压测，只观察 30–60 秒节奏下是否稳定）。
4. 收盘后开盘啦连板数与本地 `consecutive_limit_ups` 的差异样例（用于 §5.4 对照说明）。
5. 股票代码格式（文档示例为 6 位无后缀）与 `Date`/`date` 字段差异。

结论写回本文档 §10，再进入实现。

### 5.1 后端客户端 `backend/app/plugins/kaipanla/`

- `__init__.py` 写明：非官方接口、不含 `plugin.yaml`、不注册为数据源，只供 `kaipanla_intraday` 服务调用。
- `client.py`：
  - 用项目已有 `httpx`；请求头与公共参数按接口文档（`PhoneOSNew`、`VerSion`、`apiv`、`DeviceID`）。
  - `DeviceID` 首次生成后持久化在数据目录（见 §5.3），不每次随机。
  - 不传 `Token`/`UserID`。
  - 全局串行 + 最小请求间隔（默认 1 秒），单请求超时 10 秒；`errcode != "0"` 或结构不符抛 `KaipanlaError`。
  - 只暴露标准化后的记录，调用方不接触厂商数组下标。
- 标准化规则：
  - 代码：6 位 → `XXXXXX.SH/SZ/BJ`（复用项目现有代码规范化工具，实现时查找，不新写一套）。
  - 时间戳：秒级 Unix → 北京时间 ISO 字符串。
  - 金额：元；涨跌幅/比率按 `CONTRIBUTING.md` 单位约定（实现时以该文档表格为准）。
  - 情绪值、晋级率等厂商模型值原样保留数值，字段名标明 `kpl_` 前缀，不与本地口径同名。

### 5.2 服务 `backend/app/services/kaipanla_intraday.py`

- `read_intraday(data_dir, day)`：合并并发请求，TTL 30 秒（照 `zzshare_home._shared`）；返回一份聚合快照：
  ```
  {
    "date", "fetched_at", "stale_seconds",
    "summary": {kpl_strong?, limit_up, max_boards, broken_rate?, promo_rate_2?, promo_rate_3?, promo_rate_top?},
    "tiers": [{boards, count}],
    "stocks": [{symbol, name, boards, limit_time, reason, theme_code, theme_name, seal_amount, max_seal, reseal}],
    "themes": [{code, name, limit_up_count, symbols}],
    "missing": ["kpl_strong", ...]   // 本次没拿到的项，前端据此隐藏或提示
  }
  ```
- 失败策略：本次请求失败时返回上一次有效快照并带 `stale_seconds`；从未成功过则抛 `KaipanlaUnavailableError`，API 返回 503 + 中文原因。单个子接口失败只把对应字段记入 `missing`，不整体失败。
- 交易日、时段判断复用 `app.market_time.in_continuous_session` 与 `app.services.trading_day.is_trading_day`。
- 历史日期请求只读已留档数据，不请求开盘啦。

### 5.3 留档

- 目录：`data/kaipanla/v1/<date>/`
  - `observations.jsonl`：盘中每次采集追加一行聚合快照（含 `fetched_at`）。
  - `close.json`：15:05 后采集一次并定版，之后不再覆写。
  - `data/kaipanla/device_id`：持久化的设备 ID。
- 写入用 `atomic_write_text`（`close.json`）与加锁追加（`observations.jsonl`）。
- 在 `docs/data-directory.md` 增加 `kaipanla/` 一行说明。

### 5.4 扩展路由与采集 `backend/app/custom/kaipanla.py`

- `EXTENSION_ID = "kaipanla.intraday"`，`APIRouter(prefix="/api/kaipanla")`：
  - `GET /intraday?date=`：不传日期取当日；当日走 `read_intraday`，历史日期读 `close.json`（无则 404）。
  - `GET /status`：开关状态、最近成功时间、最近错误。
- 定时采集：照 `custom/zzshare.py` 的 `_lifespan`，交易日 9–15 点每 5 分钟（仅连续竞价时段）+ 15:05 定版；**开关关闭时任务直接返回**。用户打开梯队页时由前端 30 秒轮询触发按需采集，两者共享 TTL 缓存，不会叠加请求。
- 开关：偏好项 `kaipanla_enabled`，默认 `False`，读写照 `limit_ladder_monitor_enabled` 的现有模式（`services/preferences.py` + `api/settings.py`）。关闭时 `/intraday` 返回 409 + 「未启用」。

### 5.5 前端：补进连板梯队页

改动集中在 `frontend/src/custom/limit-ladder/`，新增 `frontend/src/lib/useKaipanla.ts`（查询 hook），`api.ts`/`queryKeys.ts` 加对应方法与 key。

1. **情绪条**（仅 `direction=up`）：页头下方一行
   ```
   开盘啦  情绪 56 · 涨停 69 · 最高 7 板 · 破板率 21.8% · 二板晋级 10.9%     10:32 更新
   ```
   - 晋级率只在收盘定版后显示（§10.5）；盘中字段以 §10.5 第 3 条为准。
   - `missing` 的项不显示；`stale_seconds > 120` 时更新时间变为警示色并显示「数据已过期」。
   - 开关关闭时整条不渲染（不放引导，设置页说明即可）；已开启但不可用时显示一行灰色「开盘啦暂不可用：<原因>」。
2. **涨停原因**：卡片原因优先级——当日且开盘啦可用 → 开盘啦原因；否则 zzshare 盘后原因。原因旁用小字标来源（「开」/「zz」），悬停显示完整来源名。收盘后两者都有时仍以 zzshare 为准（盘后数据更完整），开盘啦原因放进悬停详情。
3. **题材集中度**：情绪条下一行横向列出涨停数前 8 的题材（`算力 20 · 一季报 5 …`），点击即按该题材 `symbols` 过滤梯队，再点取消。默认折叠为一行，不展开二级详情。
4. 刷新：当日 30 秒 `refetchInterval`，页面不可见时停止（react-query 默认行为）；历史日期不轮询。
5. 封单：第一期不改 `SealedBadge`；开盘啦封单只放进卡片悬停详情。

### 5.6 设置页

在数据源设置页（`frontend/src/pages/settings/DataSources.tsx` 附近，按现有 zzshare 卡片样式）加一张「开盘啦（非官方）」卡片：开关、最近成功时间、最近错误；说明文字写明「非官方接口，仅供个人研究；App 升级可能失效」。

## 6. 约束

- 遵循 `CONTRIBUTING.md` §4：开盘啦数据不经能力矩阵，不得被通用功能当作标准数据集消费；不可用时明确提示。
- 遵循 `CONTRIBUTING.md` §7：页面只加一行情绪条、一行题材，次要信息放悬停。
- 不改 `/api/screener/limit-ladder` 的返回结构；叠加在前端完成。
- Windows 工作区为 CRLF，前端用 `pnpm exec`。

## 7. 测试

后端（`backend/tests/`）：
- 客户端解析：用第 0 步录下的 fixture 测每个接口的标准化（代码后缀、时间戳、单位、`Date`/`date`），以及空列表、缺字段、`errcode` 非 0。
- 服务：TTL 内并发只发一次请求；子接口失败进 `missing`；整体失败返回旧快照与 `stale_seconds`；从未成功时抛不可用；历史日期不发请求。
- 采集：开关关闭不请求；非交易日/午休不请求；`close.json` 定版后不覆写。
- 路由：409（未启用）、503（不可用）、404（历史无档）。

前端（vitest）：
- 情绪条：`missing` 项隐藏；过期警示；关闭时不渲染；不可用提示。
- 原因优先级与来源标记；题材点击过滤与取消。

验收：交易日盘中打开梯队页，情绪条与原因随时间刷新；收盘后 `close.json` 生成；断网或关开关时梯队页其余功能正常。

## 8. 风险

| 风险 | 应对 |
|---|---|
| 违反 App 用户协议 | 默认关闭、设置页明示、仅个人研究，不随安装包默认启用 |
| 接口随 App 升级失效 | 隔离在插件内；失败即提示不可用，其他功能不受影响；版本号集中在客户端常量 |
| 被限流/封禁 | 串行、最小间隔、30 秒 TTL、5 分钟定时，仅开启时运行 |
| 口径与本地不一致造成误读 | `kpl_` 前缀命名、来源标记；不参与任何计算 |

## 9. 第二期（不在本次范围）

- 首板策略 `PriorityCell`/`DataStatus` 盘中改用开盘啦原因。
- 看板总览梯队旁显示开盘啦对照值。
- 用留档数据对比开盘啦情绪值与本地 `emotion_score` / `market_phase` 的一致性。

## 10. 第 0 步实测结论

实测时间：2026-11-17（周二）北京时间 14:04–14:07 盘中两轮，间隔约 90 秒；收盘后复测见 §10.4。原始响应留在本机临时目录，实现时挑选后转为 `backend/tests/fixtures/kaipanla/`。

### 10.1 接口可用性（盘中）

| 接口 | 结果 | 结论 |
|---|---|---|
| `apphwhq` `HomeDingPan/DailyLimitIndex` | `[35,4,1,1,0]`，合计 41 | ✅ 可用 |
| `apphwhq` `HomeDingPan/DailyLimitPerformance` PidType 1–5 | 41 只，与上一行一致；封单额每分钟变化 | ✅ 可用，**主数据源** |
| `apphwhq` `HomeDingPan/DailyLimitPerformance2` PidType 1/2 | 首板炸板 10 只；昨日首板今日未涨停列表正常 | ✅ 可用 |
| `apphwhq` `HomeDingPan/ChangeStatistics` | 当日 `strong=36, ztjs=41, lbgd=4, df_num 6→5` | ✅ **情绪值盘中可得** |
| `apphwhq` `HomeDingPan/MarketStockZDNum` | `SJZT 41, SJDT 41→44` | ✅ 涨停/跌停数盘中可得 |
| `apphwhq` `HomeDingPan/ZhangTingExpression` | 返回占位垃圾（数组全为 `"longhuvip.com"`） | ❌ **晋级率盘中拿不到** |
| `apphis` `HisHomeDingPan/ZhangTingExpression` `Day=当日` | `errcode 1020 参数出错` | ❌ 盘中不可用；`Day=前一交易日` 正常返回 |
| `apphis` `HisHomeDingPan/ChangeStatistics` | 一次返回 1000 个交易日（2022-09-27 起），不含当日 | 历史回填可一次取得（本期非目标） |
| `FuPanLa/GetZhangTingTianTi`（`apphwhq` 带或不带 Date） | **返回前一交易日（11-16）的天梯**，成交额为今日值 | ❌ 不能用于当日题材归属 |

性能：单请求 330–2500 ms（每轮首个请求偏慢），约 35 个请求、间隔 1 秒，未遇限流或错误。

### 10.2 字段核实（与接口文档的差异）

`DailyLimitPerformance` 行（今日涨停列表）：
- `[4]` 首次涨停时间戳（秒，北京时间）；`[5]` 涨停原因（单一题材名，如「芯片」）；`[19]` 该原因对应的题材代码；`[12]` 所属板块（顿号分隔多个）。
- **`[15]` 是连板数**（文档未写明，PidType=2/3/4 行分别为 2/3/4）。
- `[16]` 是否回封；`[18]` 标签（如「3天2板」，可为空）；`[6]` 当前封单额、`[7]` 最大封单；`[21]` 价格、`[22]` 涨跌幅（百分数）。
- `[2]`、`[20]` 含义不明（`[20]` 取值 1–17），**不使用**。

`DailyLimitPerformance2` 行（破板列表）：字段布局随 `PidType` 变化——PidType=1 时 `[13]` 是最近炸板时间戳、`[17]` 最大封单；PidType=2 时 `[13]` 为 1。解析须按 PidType 分别处理，并以 fixture 测试锁定。

返回编码为 UTF-8 JSON（中文以 `\uXXXX` 转义），无需特殊处理。

### 10.3 与本地口径对照

| 日期 | 本地 `consecutive_limit_ups ≥ 1` | 开盘啦 | 差异 |
|---|---|---|---|
| 2026-11-16（已定版） | 33 只，最高 4 板 | 32 只，最高 4 板 | 本地多 `600530.SH` 一只；共同股票连板数完全一致 |
| 2026-11-17 盘中（本地 `quote_ts` 14:06:39） | 41 只 | 41 只 | 名单与连板数完全一致 |

结论：**涨停名单与连板数本地已足够准确**，开盘啦在这两项上没有增量价值，与 §1 判断一致；增量在涨停原因、题材代码、封单全覆盖、情绪值与跌停/大幅回撤计数。

### 10.4 收盘后复测

北京时间 15:10 复测（2026-11-17）：

| 接口 | 结果 |
|---|---|
| `apphwhq` `DailyLimitPerformance` PidType=1 | 仍返回当日，31 只（盘中 35，部分首板尾盘开板） |
| `apphis` `DailyLimitPerformance` `Day=当日` | `1020`，尚未收录 |
| `apphis` `ZhangTingExpression` `Day=当日` | `1020`，尚未收录（`apphwhq` 仍为占位垃圾） |
| `apphis` `ChangeStatistics` | 最新一行仍是前一交易日 |
| `apphis` 天梯 `Date=当日` | 空列表；`apphwhq` 天梯仍为前一交易日 |

结论：收盘后 10 分钟内，**历史地址都还没收录当日**；收盘定版只能用 `apphwhq` 的今日接口。当日晋级率最早何时可得未测出（至少晚于 15:10），只能事后补取。

### 10.5 对设计的修正

1. **题材归属改用涨停列表**：§5.0/§5.2 中的 `GetZhangTingTianTi` 删除；`themes` 由 `DailyLimitPerformance` 的 `[5]`/`[19]` 聚合（本次实测：芯片 17、算力 4、家电 3…）。
2. **晋级率事后补取**：情绪条盘中不显示晋级率。`close.json` 在 15:05 用 `apphwhq` 今日接口定版，不含晋级率；另写 `data/kaipanla/v1/<date>/expression.json`，由下一次采集任务（次日首轮或当晚补跑）对「最近一个缺 `expression.json` 的交易日」请求 `apphis ZhangTingExpression`，取到即写入、之后不改写。前端查看历史日期时有则显示，梯队页当日显示「昨日晋级率」。
3. **情绪条盘中字段**确定为：情绪值 `strong`、涨停数、跌停数 `SJDT`、最高板 `lbgd`、大幅回撤数 `df_num`、首板破板率（由破板 PidType=1 数量 ÷ (涨停数 + 首板破板数) 计算，标注为开盘啦列表派生）。
4. 盘中每轮请求固定为：`DailyLimitIndex`、`DailyLimitPerformance`×5、`DailyLimitPerformance2`（PidType=1）、`ChangeStatistics`、`MarketStockZDNum`，共 9 个，按 1 秒间隔约 10 秒完成；30 秒 TTL 下可接受。

## 11. 留给实现者的决定

- 代码规范化工具、偏好读写的具体函数名，以仓库现有实现为准。
- 情绪条、题材行的具体样式沿用梯队页现有组件与色板。
- 最小请求间隔、TTL 的具体值可在第 0 步观察后微调，需在客户端常量处注释依据。
