# 龙虎榜双源设计（fuyao 主源 + 猫爪兜底）

> 日期：2026-11-19
> 依据：`docs/猫爪数据对接/猫爪数据对接规划.md` 3.5 / 4.4 / 5.4 节；能力兜底链（`docs/superpowers/specs/2026-11-18-capability-fallback-chain-design.md`，已上线）。

## 1. 意图

龙虎榜目前只认 fuyao：`services/dragon_tiger.py` 写死调用 fuyao 插件。fuyao 失效时，以下三处都拿不到数据：
- 复盘页龙虎榜卡片；
- AI 复盘的龙虎榜摘要；
- 短线博弈的机构披露。

本次把龙虎榜接入能力路由与兜底链：fuyao 继续做主源，猫爪做兜底。同一个交易日的三张榜（总榜、机构榜、游资榜）只来自一个源，结果标明实际来源。

## 2. 目标、非目标、反目标

**目标**
- 新增可路由能力 `dragon_tiger`（`fallback: True`），默认主源 fuyao，默认兜底猫爪；设置页自动出现能力卡片（兜底顺序、重试主源）。
- 两个插件实现同一个单日三榜方法，返回现有前端契约（`DragonTigerPayload` 的三榜部分）。
- 猫爪适配器给出总榜、机构榜、近似游资榜。
- 复盘页、AI 摘要、短线博弈机构披露都标注实际来源。

**非目标**
- 兜底日的缓存在 fuyao 恢复后不自动改写（历史数据不可变）。
- 两源之间不做交叉校验或对账。
- 猫爪的游资名单不做每日归档。

**反目标**
- 同一天的三张榜来自不同的源。
- 「当日未发布」被记为源失败，导致每次盘中打开复盘页都触发熔断或换源。
- 猫爪近似游资榜不加任何说明，被当成 fuyao 口径展示。

## 3. 实测依据（2026-11-16 / 11-18 / 11-19）

| 项 | fuyao | 猫爪 |
|---|---|---|
| 总榜接口 | `dragon-tiger-list` board_type=all | `longhubang_stock`（历史服务，`tradedate=YYYYMMDD`） |
| 11-16 上榜个股 | 61 只（63 行，含 3 日榜 10 行） | 61 只，另多 2 只；同一只股票有几条上榜原因就出几行 |
| 净买额 | `net_value`（元） | `net_amount`（元），与 fuyao 逐笔一致 |
| 涨跌幅 / 净买占比 | 小数 | `pct_chg` / `net_rate` 为**百分数** |
| 1 日榜 / 3 日榜 | `range_days` | 无字段；原因含「连续三个交易日」即 3 日榜 |
| 人气排名、概念 | `hot_rank`、`concept_list` | 无 |
| 机构榜 | board_type=org，带 `org_net_value`、`org_net_rate`、`org_buy_num`、`org_sell_num` | 无此接口；`longhubang_seat` 席位明细里 `seat_name` 含「机构专用」的行可汇总 |
| 游资榜 | board_type=hot_money，按游资汇总并带关联个股 | 无此接口；`longhubang_hot_money` 有 121 个游资及其营业部简称（JSON 字符串），需与席位全称匹配。11-16 的试算：fuyao 19 个游资中 16 个匹配上，多数净额一致，「T王」不一致，3 个未匹配；名单中混有「机构专用」「沪股通专用」「深股通专用」 |
| 当日未发布（11-19 16:14） | 成功返回，0 条 | 0 行 |
| 未来日期 | `FuyaoError` code=1003 | 0 行 |
| 非交易日 | `FuyaoError` code=1002（文档） | 0 行 |

## 4. 方案比较

| 方案 | 结论 |
|---|---|
| **A. 注册为可路由能力并复用兜底链**：插件各自适配到同一契约，服务层按日调用 `call_with_fallback` | **采用** |
| B. 服务层写死「fuyao 失败就调猫爪」 | 否：违背规划第 5 节「业务层不写死数据源」，设置页也看不到兜底状态 |
| C. 三张榜分别兜底 | 否：同一天可能混用两个源，违反反目标 |

## 5. 设计

### 5.1 能力注册
- `CAPABILITY_REGISTRY` 新增：`id=dragon_tiger`，label「龙虎榜」，desc「上榜个股、机构与游资席位」，field `dragon_tiger_data_provider`，default `fuyao`，`tf_tier=None`，`fallback=True`。
- `DEFAULT_FALLBACK_ORDER["dragon_tiger"] = ["meoz", "fuyao"]`。
- `fuyao` 与 `meoz` 的 `plugin.yaml` 都在 `datasets` 中声明 `dragon_tiger`。
- preferences：新增 `get_dragon_tiger_data_provider()`（未注册或未声明 `dragon_tiger` 的值回退 fuyao）。另外三处要同步加上：
  - `_FALLBACK_PRIMARY_GETTERS`；
  - `capabilities._current_routing`；
  - settings API 的三处显式 getter 字典，以及 `DataProvidersIn`（含 `dragon_tiger_data_provider_fallbacks`）。
- 前端加入 `ProviderField`、`FallbackField`、`DEFAULT_ROUTING`、`DATASET_LABEL`。

### 5.2 插件契约
```text
get_dragon_tiger(day: date) -> dict | None
  dict: {"trade_date": "YYYY-MM-DD",
         "all":       {"trade_date", "stock_count", "count", "stock_items": [...]},
         "org":       {"trade_date", "stock_count", "count", "stock_items": [...]},
         "hot_money": {"trade_date", "count", "hot_money_items": [...]}}
  None: 该日无数据（尚未发布、非交易日、未来日期），不是源失效
  抛异常: 源失效（网络、认证、额度、格式）
```
- 字段口径沿用现有 `DragonTigerStockItem` / `DragonTigerHotMoney`：金额单位为元，比率为小数，`thscode` 带后缀。
- 服务层调用 `call_with_fallback("dragon_tiger", "get_dragon_tiger", day, validate=...)`。`validate` 接受 `None` 或含三榜的 dict，因此 `None` 不计失败、不换源。

### 5.3 fuyao 适配器
- 把现在服务里的「三榜一次取齐」移到 `FuyaoProvider.get_dragon_tiger`：三次调用 `dragon_tiger_list(board, iso)`。
- 总榜 `stock_items` 为空 → 返回 `None`。
- `FuyaoError` 的 code 为 1002 或 1003 → 返回 `None`；其它 `FuyaoError` 照常抛出。
- 三榜中任一榜失败，整日失败（与现状一致）。
- 保留原有的 `dragon_tiger(board_type, date)` 方法，避免影响其它调用方。

### 5.4 猫爪适配器 `MeozProvider.get_dragon_tiger`
- 当天调用 `longhubang_stock`、`longhubang_seat` 各一次（历史服务）；`longhubang_hot_money` 名单在进程内缓存 24 小时。
- 个股榜为 0 行 → 返回 `None`。
- 行的 `tradedate` 与请求日不一致 → 抛错（与现有参考数据同口径）。
- 代码后缀：先用 `_full_symbol(symbol, universe)`，再退回 `dragon_tiger.derive_thscode`（覆盖 B 股），都无法识别则丢弃并告警。
- **总榜**：
  - 按 `(symbol, range_days)` 去重。原因含「连续三个交易日」的为 `range_days=3`，否则为 1。
  - 同键多行（多条上榜原因）取第一行；它们的金额相同。
  - 字段映射：`net_value=net_amount`，`net_rate=net_rate/100`，`buy_value=lhb_buy`，`sell_value=lhb_sell`，`change=pct_chg/100`，`ticker` 为 6 位代码，`name`；`hot_rank`、`concept_list` 为 `None`；`limit_reason` 为该键下所有原因去重后用「；」连接。
  - `stock_count` = 去重后的代码数，`count` = 去重后的行数。
- **机构榜**：
  - 席位行按 `(symbol, range_days, seat_name, side)` 去重，`range_days` 由该行原因按上面同样的规则判定。
  - `seat_name` 含「机构专用」的行，按 `(symbol, range_days)` 汇总：`org_net_value` 为 `net_buy` 之和；`org_buy_num` 为 `side=0` 的行数，`org_sell_num` 为 `side=1` 的行数。
  - `org_net_rate = org_net_value / amount`（`amount` 取自个股榜同键）。
  - 其余字段从总榜同键复制。
  - 只列有机构席位的个股。
- **游资榜**：
  - 席位名归一化：去掉「股份有限公司」「有限责任公司」「有限公司」「证券营业部」「营业部」「分公司」「证券」。名单里的营业部简称做同样处理，建立「归一化名 → 游资名」的映射。
  - 剔除游资名含「机构专用」「沪股通专用」「深股通专用」的条目。
  - 席位行按 `(symbol, seat_name)` 去重：同一席位在同一只股票上的 1 日榜和 3 日榜记录只计一次，取 `range_days=1` 那条。
  - 汇总：游资的 `buying` = 匹配到的席位行 `net_buy` 之和；`rows` = 关联个股，字段从总榜复制，并加上 `hot_money_item_net_value`（该游资在该股的净买入）。
  - 按 `buying` 降序排列。
- 返回 dict 中额外带上 `hot_money_basis: "meoz_seat_match"`，供服务层和前端标注。

### 5.5 服务层 `services/dragon_tiger.py`
- `_provider()` 和写死的 `FuyaoError` 移除，改为按日调用兜底链。
- 流程：
  1. 解析目标交易日（逻辑不变）。目标为 `None` 且本地没有任何日K分区时，用北京时间今天。
  2. 历史日且缓存命中，直接返回。
  3. 调用兜底链：
     - 某个源返回数据 → `state=ok`；
     - 所有源都返回 `None`，或全链失败 → 尝试上一交易日（先查缓存，再调兜底链），成功则 `state=fallback_prev`；
     - 上一交易日也没有 → `no_data`。
  4. 生效链为空（没有任何已就绪的源）→ `source_unavailable`。
- 返回体新增：
  - `source`：实际来源，例如 `fuyao` / `meoz`；
  - `source_display`；
  - `degraded`：实际来源不是当前主源；
  - `fallback_reason`；
  - `hot_money_basis`：猫爪时为 `meoz_seat_match`，fuyao 时为空。
- 缓存：写入时带 `source` 和 `hot_money_basis`。读取没有 `source` 字段的旧缓存时，按 `fuyao` 处理。读缓存时，`degraded` 按「缓存来源 ≠ 当前主源」实时计算。
- 历史日才落缓存，与现状一致。当日的 `None` 不落缓存。
- `build_recap_context` 的首行改为「(数据日期: X；来源: 显示名)」。猫爪游资榜那一行加注「按猫爪游资名单匹配」。

### 5.6 下游
- `market_game_disclosures.fetch_institutional_disclosures`：`source` 改为 `f"{payload['source']}/dragon_tiger/org"`，不再写死 fuyao。旧缓存没有来源时仍为 `fuyao/...`。
- `api/market_recap.py` 的 docstring 去掉「fuyao 专有」。

### 5.7 前端（复盘页龙虎榜卡片 `custom/review/Page.tsx`）
- 标题旁显示来源名，`degraded` 时显示「（兜底）」，悬停显示 `fallback_reason`。
- 游资页签：`hot_money_basis === "meoz_seat_match"` 时，在列表上方加一行灰字「按猫爪游资名单匹配营业部，口径与 fuyao 不同」。
- 「需要 fuyao 数据源」的降级提示改为「龙虎榜数据源未配置」，链接到数据源设置。
- `hot_rank`、`concept_list` 为空时不显示对应徽标，现有逻辑已支持。
- `DragonTigerPayload` 类型新增 `source`、`source_display`、`degraded`、`fallback_reason`、`hot_money_basis`。

## 6. 测试
- **猫爪适配器**（mock 客户端，用 11-16 的真实形状构造样本）：
  - 百分数 → 小数；
  - 按原因去重，3 日榜识别；
  - 机构汇总（家数、净额、净买占比）；
  - 游资匹配：剔除「机构专用」「沪股通专用」等；同席位跨 1 日 / 3 日榜不重复计；
  - B 股与北交所代码后缀；
  - 0 行返回 `None`；交易日不符时抛错。
- **fuyao 适配器**：
  - 三榜聚合；
  - 0 条返回 `None`；
  - code 1002 / 1003 返回 `None`；其它错误抛出。
- **服务层**：
  - 主源成功；
  - 主源抛错时由兜底源接管，标 `degraded`；
  - 主源返回 `None` 时不换源、回退上一交易日；
  - 全链失败 → `no_data`；
  - 链为空 → `source_unavailable`；
  - 旧缓存（无 `source`）按 fuyao 读取；
  - 缓存来源与当前主源不同时标 `degraded`。
- **现有 `test_dragon_tiger.py`**：改为通过兜底链打桩，原有交易日、缓存、`fallback_prev` 断言保留。
- **机构披露**：`source` 随实际来源变化。
- **前端**：来源与兜底标注；猫爪游资口径说明；未配置提示文案。

## 7. 我替用户做的决定
1. `None` 表示「该日无数据」，不计失败、不换源。如果 fuyao 已发布而猫爪未发布（或反过来），不会跨源去拿。
2. 游资名单在进程内缓存 24 小时，不落盘。
3. 游资匹配规则就是 5.4 那组归一化与剔除名单。匹配不上的游资不显示，不做模糊匹配。
4. 默认兜底顺序为 `["meoz", "fuyao"]`（主源为 fuyao 时即 `["meoz"]`）。
