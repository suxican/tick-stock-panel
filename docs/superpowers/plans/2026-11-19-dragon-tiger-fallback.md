# 龙虎榜双源实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal：** 龙虎榜接入能力路由与兜底链，fuyao 为主源、猫爪为兜底，同一交易日三张榜只来自一个源并标注来源。

**Architecture：**
- 注册可路由能力 `dragon_tiger`（`fallback: True`）。
- fuyao、猫爪插件各实现 `get_dragon_tiger(day) -> dict | None`，适配到现有前端契约。
- `services/dragon_tiger.py` 改为按日调用 `routing_health.call_with_fallback`，`None` 表示「该日无数据」，不计失败。
- 复盘页、AI 摘要、短线博弈机构披露都标注实际来源。

**Tech Stack：** Python / FastAPI / pytest（`python -m pytest`，用主检出的 `.venv`）；React + TypeScript + vitest（`pnpm exec`）。

**Spec：** `docs/superpowers/specs/2026-11-19-dragon-tiger-fallback-design.md`

## Global Constraints

- 在同级 worktree `D:\stockSpace\tsp-lhb`（分支 `feat/dragon-tiger-fallback`）开发；后端测试用 `D:/stockSpace/tick-stock-panel/backend/.venv/Scripts/python.exe -m pytest`。
- 金额单位为元，比率为小数，`thscode` 带后缀；猫爪 `pct_chg`、`net_rate` 是百分数，要除以 100。
- 猫爪原因中含「连续三个交易日」的为 `range_days=3`，其它为 1。
- 游资名归一化去掉：「股份有限公司」「有限责任公司」「有限公司」「证券营业部」「营业部」「分公司」「证券」。剔除游资名含「机构专用」「沪股通专用」「深股通专用」的条目。
- `hot_money_basis` 的值固定为 `"meoz_seat_match"`。
- 前端说明文案固定为「按猫爪游资名单匹配营业部，口径与 fuyao 不同」；未配置提示为「龙虎榜数据源未配置」。
- 默认兜底顺序 `DEFAULT_FALLBACK_ORDER["dragon_tiger"] = ["meoz", "fuyao"]`。
- 猫爪游资名单进程内缓存 24 小时（`_HOT_MONEY_TTL_S = 86400`）。
- 不改 `dragon_tiger(board_type, date)` 这个 fuyao 旧方法的签名与行为。
- 工作区为 CRLF；前端只用 `pnpm exec`；每个任务结束跑 ruff（改动文件不新增告警）和 `git diff --check`。

## Review Focus

1. **fuyao 已发布、猫爪未发布（或反过来）**：主源返回 `None` 时不换源，直接回退上一交易日 → Task 4 `test_primary_none_does_not_switch_and_falls_back_prev`。
2. **猫爪同一席位同时出现在 1 日榜和 3 日榜原因下**：游资汇总只计一次 → Task 2 `test_hot_money_counts_seat_once_across_ranges`。
3. **B 股（200xxx）、北交所（920xxx）代码**：猫爪适配器补后缀正确，不丢行 → Task 2 `test_symbols_get_suffix_for_b_share_and_bse`。
4. **旧缓存没有 `source` 字段**：按 fuyao 读取，主源已切到猫爪时显示为兜底以外的来源 → Task 4 `test_legacy_cache_reads_as_fuyao`。
5. **fuyao 对未来日期报 1003**：视为无数据而非失败，不触发熔断 → Task 3 `test_fuyao_out_of_range_returns_none`。

---

### Task 1：能力注册与路由偏好

**Files:**
- Modify: `backend/app/data_providers/capabilities.py`（注册表条目、`DEFAULT_FALLBACK_ORDER`、`_current_routing`）
- Modify: `backend/app/services/preferences.py`（`get_dragon_tiger_data_provider`、`_FALLBACK_PRIMARY_GETTERS`）
- Modify: `backend/app/api/settings.py`（`DataProvidersIn` 两字段；`get_preferences` / `get_capability_matrix` / `update_data_providers` 三处字典；`get_capability_matrix` 的 `fallbacks` 加 `dragon_tiger`）
- Modify: `backend/app/plugins/fuyao/plugin.yaml`、`backend/app/plugins/meoz/plugin.yaml`（`datasets` 加 `dragon_tiger`）
- Modify: `frontend/src/lib/api.ts`（`ProviderField`、`FallbackField`）、`frontend/src/lib/provider-routing.ts`（`DEFAULT_ROUTING`）、`frontend/src/pages/settings/DataSources.tsx`（`DATASET_LABEL`、`CAP_ICON` 用 `Trophy`）
- Test: `backend/tests/test_capability_matrix.py`（`DEFAULT_CURRENT`、注册表 id 集合、getter 注入用例补字段）、`backend/tests/test_dragon_tiger_routing.py`（新）

**Interfaces:**
- Produces：
  - `preferences.get_dragon_tiger_data_provider() -> str`：存储值已注册且声明 `dragon_tiger` 时返回该值，否则返回 `"fuyao"`；
  - `preferences.get_capability_fallbacks("dragon_tiger") -> list[str]`；
  - 偏好键 `dragon_tiger_data_provider`、`dragon_tiger_data_provider_fallbacks`。

- [ ] **Step 1：写失败测试**

```python
def test_registry_has_dragon_tiger_fallback_capability():
    cap = next(c for c in CAPABILITY_REGISTRY if c["id"] == "dragon_tiger")
    assert (cap["field"], cap["default"], cap["tf_tier"], cap.get("fallback")) == \
        ("dragon_tiger_data_provider", "fuyao", None, True)

def test_getter_defaults_and_validates(monkeypatch):     # 未存储→fuyao; 存 meoz 且声明→meoz; 存 tdxquant(未声明)→fuyao
def test_default_fallbacks_is_meoz(monkeypatch):         # 主源 fuyao, 两源声明 → ["meoz"]
def test_api_saves_dragon_tiger_route_and_fallbacks(monkeypatch)
def test_matrix_row_lists_both_plugins(monkeypatch):     # 两插件声明 dragon_tiger → candidates 含 fuyao, meoz; fallbacks == ["meoz"]
```
并更新 `test_capability_matrix.py` 中的 `DEFAULT_CURRENT`（加 `"dragon_tiger_data_provider": "fuyao"`）、注册表 id 集合、`getter_values` / `getter_names`。

- [ ] **Step 2：** `python -m pytest tests/test_dragon_tiger_routing.py tests/test_capability_matrix.py -q`，预期 FAIL。
- [ ] **Step 3：** 实现。getter 参照 `get_overseas_data_provider`：用 `custom.names()` 加 `provider_has_dataset(name, "dragon_tiger")` 校验。
- [ ] **Step 4：** 跑上述测试，以及 `tests/test_fallback_preferences.py tests/test_fallback_matrix.py tests/test_overseas_routing.py`，预期 PASS。前端跑 `pnpm exec vitest run src/lib/provider-routing.test.ts src/pages/settings`，预期 PASS。
- [ ] **Step 5：** 提交 `feat(dragon-tiger): 注册可路由的龙虎榜能力`。

---

### Task 2：猫爪适配器 `MeozProvider.get_dragon_tiger`

**Files:**
- Modify: `backend/app/plugins/meoz/provider.py`
- Test: `backend/tests/test_meoz_dragon_tiger.py`（新）

**Interfaces:**
- Produces：`MeozProvider.get_dragon_tiger(day: date) -> dict | None`，返回结构见 spec 5.2；猫爪结果额外带 `"hot_money_basis": "meoz_seat_match"`。
- 内部：
  - `_lhb_range_days(reason: str) -> int`；
  - `_seat_key(name: str) -> str`（归一化）；
  - `self._hot_money_alias() -> dict[str, str]`（归一化营业部 → 游资名，24 小时缓存，持 `self._lock`）。

假客户端按 `apiname` 返回下列行（用 11-16 实测形状）：
- `longhubang_stock`：`tradedate, symbol, name, pct_chg, amount, lhb_buy, lhb_sell, net_amount, net_rate, reason`；
- `longhubang_seat`：`tradedate, symbol, name, reason, side, seat_name, buy, sell, net_buy`；
- `longhubang_hot_money`：`name, description, seats`（`seats` 为 JSON 字符串）。

- [ ] **Step 1：写失败测试**

```python
def test_all_board_converts_percent_and_dedupes_reasons():
    # 002086 三条原因同金额 → 1 行 range_days=1; net_rate 0.79 → 0.0079; change -9.9585 → -0.099585
    # limit_reason 为三条原因以「；」连接
def test_three_day_reason_becomes_range_days_3():
    # 002148: 两条 1 日原因 + 一条「连续三个交易日…」→ 两行, range_days {1, 3}, 净额各自保留
def test_org_board_aggregates_institution_seats():
    # 两个「机构专用」买 (side 0) + 一个卖 (side 1) → org_buy_num 2, org_sell_num 1,
    # org_net_value = 三者 net_buy 之和, org_net_rate = org_net_value / amount
def test_hot_money_matches_normalized_seat_names_and_excludes_channels():
    # 名单「华泰证券南京六合雄州西路」匹配席位「华泰证券股份有限公司南京六合雄州西路证券营业部」
    # 名单中的「机构专用」「沪股通专用」不产生游资条目
def test_hot_money_counts_seat_once_across_ranges():
    # 同一席位在同一股票的 1 日与 3 日原因下各一行 → buying 只计一次
def test_symbols_get_suffix_for_b_share_and_bse():
    # 200056 → 200056.SZ, 920021 → 920021.BJ, 600715 → 600715.SH
def test_empty_stock_rows_return_none():
def test_trade_date_mismatch_raises():
def test_hot_money_list_cached_between_days():   # 连续两天只调用一次 longhubang_hot_money
def test_stock_counts():                          # stock_count = 去重代码数, count = 去重行数
```

- [ ] **Step 2：** `python -m pytest tests/test_meoz_dragon_tiger.py -q`，预期 FAIL。
- [ ] **Step 3：** 实现。代码后缀先用 `_full_symbol(symbol, self._universe_map())`，再退回 `app.services.dragon_tiger.derive_thscode`；该函数在方法内 import，避免插件在模块级依赖服务层。交易日校验复用 `_same_day(rows, "tradedate", ymd, api)`。
- [ ] **Step 4：** 跑 `tests/test_meoz_dragon_tiger.py tests/test_meoz_provider.py`，预期 PASS。
- [ ] **Step 5：** 提交 `feat(meoz): 龙虎榜三榜适配 (个股榜、机构席位汇总、游资名单匹配)`。

---

### Task 3：fuyao 适配器 `FuyaoProvider.get_dragon_tiger`

**Files:**
- Modify: `backend/app/plugins/fuyao/provider.py`
- Test: `backend/tests/test_fuyao_dragon_tiger.py`（新）

**Interfaces:**
- Produces：`FuyaoProvider.get_dragon_tiger(day: date) -> dict | None`。内部三次调用 `self._get_client().dragon_tiger_list(board, day.isoformat())`，`board` 依次为 all、org、hot_money。每榜输出 `{"trade_date", "stock_count", "count", "stock_items" | "hot_money_items"}`，**不在插件里做 thscode 归一化**（服务层统一做）。

- [ ] **Step 1：写失败测试**

```python
def test_fuyao_returns_three_boards():
def test_fuyao_empty_all_board_returns_none():          # all 榜 stock_items=[] → None
def test_fuyao_non_trading_day_returns_none():          # FuyaoError(code=1002) → None
def test_fuyao_out_of_range_returns_none():             # FuyaoError(code=1003) → None
def test_fuyao_other_error_raises():                    # FuyaoError(code=5000) → 抛出
def test_fuyao_org_failure_fails_whole_day():           # org 榜抛 5000 → 抛出 (不返回半套)
```

- [ ] **Step 2：** 跑测试，预期 FAIL。
- [ ] **Step 3：** 实现。code 比较用 `str(e.code) in {"1002", "1003"}`。
- [ ] **Step 4：** 跑 `tests/test_fuyao_dragon_tiger.py` 与 `python -m pytest -q -k fuyao`，预期 PASS。
- [ ] **Step 5：** 提交 `feat(fuyao): 龙虎榜单日三榜接口, 无数据与失败分开`。

---

### Task 4：服务层改走兜底链

**Files:**
- Modify: `backend/app/services/dragon_tiger.py`
- Modify: `backend/tests/test_dragon_tiger.py`（打桩方式改为兜底链；保留原有断言意图）
- Modify: `backend/app/api/market_recap.py`（docstring）

**Interfaces:**
- Consumes：`routing_health.call_with_fallback`、`routing_health.effective_chain`、`routing_health._primary`、`routing_health._display`；Task 2、3 的 `get_dragon_tiger`。
- Produces：`get_dragon_tiger(data_dir, target) -> dict`。返回体新增 `source`、`source_display`、`degraded`、`fallback_reason`、`hot_money_basis`；`state` 取值不变。
- 内部：`_fetch_day(day: date) -> tuple[dict | None, FallbackResult]`，同时用于目标日和上一交易日。

流程（测试不能唯一确定，因此写出）：

```text
trade_date = 解析 (target 为 None 且本地无分区 → cn_today())
if trade_date < today 且缓存命中: return _with_routing(cached)          # 缓存无 source → "fuyao"
if not routing_health.effective_chain("dragon_tiger"): return {"state": "source_unavailable"}
payload, r = _fetch_day(trade_date)
if payload: 历史日落缓存 (带 source / hot_money_basis); return {..., state: ok}
prev = 上一交易日; 若有: 先查缓存, 再 _fetch_day(prev); 有数据 → state fallback_prev
否则 → {"state": "no_data", "message": r.reason or "该日暂无龙虎榜数据", ...}
```

`_with_routing(payload)`：
- 补齐 `source`（缺省为 `"fuyao"`）和 `source_display`；
- `degraded` = 本次实际来源 ≠ 当前主源；
- `fallback_reason`：只在本次现拉且降级时填写；读缓存时为 `None`。

`validate`：`lambda d: d is None or (isinstance(d, dict) and isinstance(d.get("all"), dict))`。
- 现拉结果仍过一遍 `normalize_payload`；
- `build_recap_context` 首行改为 `(数据日期: {trade_date}；来源: {source_display})`，`hot_money_basis` 存在时在「活跃游资」后注明「(按猫爪游资名单匹配)」。

- [ ] **Step 1：写失败测试**（`tests/test_dragon_tiger.py`。`_FakeProvider` 改为实现 `get_dragon_tiger(day)`，用 monkeypatch 替换 `routing_health` 的 `_primary`、`_fallbacks`、`_candidate_names`、`resolve_provider`；原有用例全部改用该夹具）

```python
def test_primary_ok_marks_source_not_degraded():
def test_primary_error_falls_back_to_meoz_and_marks_degraded():
def test_primary_none_does_not_switch_and_falls_back_prev():   # fuyao 返回 None, meoz 不应被调用
def test_all_fail_returns_no_data():
def test_empty_chain_returns_source_unavailable():
def test_cache_stores_source_and_basis():
def test_legacy_cache_reads_as_fuyao():                         # 旧缓存无 source → source "fuyao"
def test_cached_meoz_day_marked_degraded_when_primary_fuyao():
def test_recap_context_names_source_and_hot_money_basis():
```

- [ ] **Step 2：** 跑 `tests/test_dragon_tiger.py -q`，预期新用例 FAIL。
- [ ] **Step 3：** 实现。删除 `_provider()`、对 `FuyaoError` 的依赖和 `_fetch_boards`/`_boards_of`（逻辑已移到插件）；保留 `derive_thscode`、`normalize_*`、`resolve_trade_date`、`_local_trading_days`（其它服务在用）。
- [ ] **Step 4：** 跑 `tests/test_dragon_tiger.py` 与 `python -m pytest -q -k "dragon or recap or disclos"`，预期 PASS。
- [ ] **Step 5：** 提交 `feat(dragon-tiger): 服务层按日走兜底链, 结果标注来源`。

---

### Task 5：短线博弈机构披露来源

**Files:**
- Modify: `backend/app/services/market_game_disclosures.py`
- Test: 在 `backend/tests/` 中找到 `fetch_institutional_disclosures` 的现有测试文件（`grep -rln fetch_institutional_disclosures backend/tests`），追加用例

- [ ] **Step 1：写失败测试**：`payload["source"] == "meoz"` 时 `result["source"] == "meoz/dragon_tiger/org"`；`payload` 没有 `source` 时为 `"fuyao/dragon_tiger/org"`。
- [ ] **Step 2：** 跑，预期 FAIL。
- [ ] **Step 3：** 实现：`result["source"]` 在拿到 `payload` 后按实际来源改写；模块 docstring 去掉 fuyao 字样。
- [ ] **Step 4：** 跑 `python -m pytest -q -k "disclos or market_game"`，预期 PASS。
- [ ] **Step 5：** 提交 `fix(market-game): 机构披露来源随龙虎榜实际来源`。

---

### Task 6：复盘页龙虎榜卡片

**Files:**
- Modify: `frontend/src/lib/api.ts`（`DragonTigerPayload` 加 `source?`、`source_display?`、`degraded?`、`fallback_reason?`、`hot_money_basis?`）
- Modify: `frontend/src/custom/review/Page.tsx`（标题来源标注；游资页签说明行；未配置文案；把文件内「fuyao 专有」注释改成通用说法）
- Test: `frontend/src/custom/review/DragonTiger.source.test.tsx`（新，按 `DragonTiger.nullThscode.test.tsx` 的渲染方式）

- [ ] **Step 1：写失败测试**

```tsx
it('shows the source name next to the title', ...)                       // source_display '猫爪数据' 出现在头部
it('marks a fallback source and explains why on hover', ...)             // degraded → 文本含「（兜底）」, title 含 fallback_reason
it('explains the meoz hot money basis on the hot money tab', ...)        // 展开→切到游资页签→含「按猫爪游资名单匹配营业部，口径与 fuyao 不同」
it('uses a source-neutral message when no source is configured', ...)    // state source_unavailable → 含「龙虎榜数据源未配置」, 不含「fuyao」
```

- [ ] **Step 2：** `pnpm exec vitest run src/custom/review`，预期新用例 FAIL。
- [ ] **Step 3：** 实现。来源标注放在「龙虎榜」标题后，用 `text-[10px] text-muted`，兜底时用 `text-warning`；说明行放在 `_DtSeatList` 的调用处上方。
- [ ] **Step 4：** 跑 `pnpm exec vitest run src/custom/review src/pages/settings` 与 `pnpm build`，预期 PASS。
- [ ] **Step 5：** 提交 `feat(review): 龙虎榜卡片标注来源与猫爪游资口径`。

---

### Task 7：文档与整体验证

**Files:**
- Modify: `docs/plugin-development.md`：新增「龙虎榜数据集 `dragon_tiger`」小节，写明 `get_dragon_tiger(day) -> dict | None` 契约、`None` 语义、单位、`hot_money_basis`。
- Modify: `CONTRIBUTING.md` §4 兜底链条目（中英文）：「目前为 `depth5`、`overseas`」改为「目前为 `depth5`、`overseas`、`dragon_tiger`」，并注明「插件返回 `None` 表示该日无数据，不计失败、不换源」。

- [ ] **Step 1：** 改文档。
- [ ] **Step 2：** 后端全量 `python -m pytest -q`（后台跑）。与上一轮的基线失败清单对比；新增失败要在 `cfcaf79b` 上重跑确认，排除不稳定用例。
- [ ] **Step 3：** ruff（改动文件告警数不增加）、`git diff --check`、`pnpm build`。
- [ ] **Step 4：真实数据冒烟（只读，约 6 次猫爪额度）**：
  1. 用真实 Key 直接调用 `MeozProvider().get_dragon_tiger(date(2026, 11, 18))` 和 `FuyaoProvider().get_dragon_tiger(...)`；
  2. 对比两边总榜代码集合、净额，以及机构榜、游资榜的条目数；
  3. 结果写进交付说明。不写入真实 `data/`。
- [ ] **Step 5：** 提交 `docs: 龙虎榜数据集契约与兜底链能力清单`。
