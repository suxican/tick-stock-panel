# 看板「实时热搜 / 最强风口 / 新闻」Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在看板页新增「实时热搜」「最强风口」「新闻」三个可摆放组件，数据来自猫爪。

**Architecture:** 组件注册进 `components/dashboard/registry.tsx`，每个组件自己用 `useQuery` 取数（参照 `BellwetherWidget`）。热搜沿用现有 `/api/meoz/hot-rank`；最强风口和新闻各新增 provider 方法、服务（60 秒缓存）和 `/api/meoz/*` 路由（加在 `meoz_reasons.py`）。

**Tech Stack:** FastAPI + polars 后端（pytest）；React + @tanstack/react-query + react-grid-layout 前端（vitest，`pnpm exec`）。

**Spec:** `docs/superpowers/specs/2026-11-20-dashboard-meoz-widgets-design.md`

## Global Constraints

- 前端命令一律 `pnpm exec ...`，**不得用 `npx`/`npm`**（会清空 `node_modules/.pnpm`）。工作目录 `frontend/`。
- 后端测试：`cd backend && .venv/Scripts/python.exe -m pytest <路径> -q`。后端测试不得用真实 Key、不得写真实 `data/`。
- 工作区是 CRLF（autocrlf=true）：用 Edit 工具改既有文件；脚本重写必须保留 `\r\n`。
- 不改 `DEFAULT_LAYOUT`；三个组件均单实例；不参与首板评分/主线评分；不落盘新闻正文。
- 涨跌幅内部统一小数制（0.1003 表示 10.03%）。`fengk_kp.pct_chg` 是百分数，在 provider 边界除以 100，与 `get_hot_stocks` 一致。
- 时间一律按北京时间（`CN_TZ`）转换，不用主机本地时间。
- 失败语义：来源失败显示「来源不可用」，不得显示成空榜；回看历史日时热搜/新闻显示「仅实时数据」，最强风口历史日失败不回退到实时数据。
- 轮询：盘中 60 秒；保留上一份有效数据；页面不可见时不轮询（`refetchIntervalInBackground: false`）。
- 项目规则（AGENTS.md）：**不自动提交/推送**。每个任务的最后一步是检查 `git diff` / `git status` 并汇报，由用户决定是否提交。

## Review Focus

1. 回看历史日：热搜/新闻不得展示今天的数据（Task 1、3 各有测试）。
2. 单个来源失败（503/502）与真正的空结果必须是两种界面（Task 1、2、3）。
3. 新闻 `url` 不是 `http(s)://` 开头（如 `javascript:`）时，标题只显示文字、不渲染成链接（Task 3）。
4. `fengk_kp` 行缺字段（`strength`/`signal_time` 为空、`selected_themes` 为 None、`symbol` 无法映射）时跳过或置空，不抛错（Task 2）。
5. 看板切换日期时最强风口的查询键包含日期，历史日不轮询，且失败时不显示上一个日期的数据（Task 2）。

---

### Task 1: 实时热搜组件（仅前端）

**Files:**
- Create: `frontend/src/components/dashboard/HotSearchWidget.tsx`
- Create: `frontend/src/components/dashboard/HotSearchWidget.test.tsx`
- Modify: `frontend/src/components/dashboard/layout.ts`（`WidgetType` 增加 `'hot-search'`）
- Modify: `frontend/src/components/dashboard/registry.tsx`（注册）
- Modify: `frontend/src/pages/Dashboard.tsx`（`PreviewSource` 增加 `'hot-search'`）

**Interfaces:**
- Consumes: `useMeozHotRank(enabled?: boolean)`、`HOT_STOCK_LABELS`（`@/lib/useMeozThemeAttribution`）；`MeozHotRank`/`MeozHotStockKind`（`@/lib/api`）；`SectionTitle`（`./shared`）；`WidgetCtx.isLatestDate / openStock / updateItemParams`。
- Produces: `export function HotSearchWidget(props: { kind?: string; isLatestDate: boolean; onKindChange: (kind: HotSearchKind) => void; onOpenStock: (symbol: string, name?: string, navList?: { symbol: string; name?: string }[]) => void }): JSX.Element`；`export type HotSearchKind = MeozHotStockKind | 'themes'`；`export function isHotSearchKind(v: unknown): v is HotSearchKind`。

- [ ] **Step 1: 写失败测试** `HotSearchWidget.test.tsx`（参照 `BellwetherWidget.test.tsx` 的 mock 与渲染方式，mock `@/lib/api` 的 `api.meozHotRank`），断言：
  - 默认显示「同花顺人气」榜，行内含排名、名称、涨幅（`pct_chg: 0.1` 显示 `+10.00%`），点击行调用 `onOpenStock('600000', '浦发银行', [...])`。
  - 点「东财人气」分段后调用 `onKindChange('dfcf_renqi')`；传入 `kind='themes'` 时显示板块名与排名，无涨幅列。
  - 当前榜 `available: false` → 文本含「来源不可用」且不含「暂无」；`available: true, rows: []` → 含「暂无数据」。
  - `isLatestDate={false}` → 含「热搜仅实时数据」，且 `api.meozHotRank` 未被调用（`enabled=false`）。
  - 非法 `kind='bogus'` 回退到 `ths_normal`。

- [ ] **Step 2: 运行确认失败**：`cd frontend && pnpm exec vitest run src/components/dashboard/HotSearchWidget.test.tsx`，预期：模块不存在而失败。

- [ ] **Step 3: 实现 `HotSearchWidget`**（`HotSearchWidget.tsx`）：`sectionCard` 风格与其他组件一致（`SectionTitle` + 右侧 hint 显示 `fetched_at` 的 HH:mm；板块热搜分段改显示行内 `available_at`）。分段为 4 项：`ths_normal / dfcf_renqi / ths_trend / themes`，标签取 `HOT_STOCK_LABELS`，`themes` 标「板块热搜」。窄宽度标题行 `flex-wrap`，列表容器 `min-h-0 overflow-y-auto`。涨幅用 `shared.tsx` 的 `fmtStockPct`/`pctClass`——先读其入参单位，保证小数 0.1 显示 `+10.00%`。

- [ ] **Step 4: 注册**：`layout.ts` 的 `WidgetType` 增加 `'hot-search'`；`registry.tsx` 在 `WIDGET_DEFS` 末尾（`ext-link` 之前）加 `{ id: 'hot-search', label: '实时热搜', icon: Flame, minW: 3, minH: 7, defW: 4, defH: 10, render: (ctx, item) => <HotSearchWidget kind={item.p?.kind} isLatestDate={ctx.isLatestDate} onKindChange={kind => ctx.updateItemParams(item.i, { kind })} onOpenStock={(s, n, nav) => ctx.openStock('hot-search', s, n, nav)} /> }`；`Dashboard.tsx` 的 `PreviewSource` 加 `'hot-search'`。

- [ ] **Step 5: 运行测试**：`pnpm exec vitest run src/components/dashboard`，预期全部 PASS（含已有 `layout.test.ts`、`useDashboardLayout.test.tsx`）；`pnpm exec tsc --noEmit` 无新增错误。

- [ ] **Step 6: 页面验证**：按「临时数据目录」约定启动预览（`DATA_DIR` 与 `TICKFLOW_ENV_FILE` 指向临时目录，不碰真实 `data/`），进入编辑态添加「实时热搜」，确认分段切换、点击打开个股、回看历史日提示；记录结果。未配置猫爪时确认显示「来源不可用」。

- [ ] **Step 7: 检查 `git diff` / `git status` 并汇报，不提交。**

---

### Task 2: 最强风口（后端 + 前端）

**Files:**
- Modify: `backend/app/plugins/meoz/provider.py`（新增 `get_strongest_vent`）
- Create: `backend/app/services/meoz_strongest_vent.py`
- Modify: `backend/app/custom/meoz_reasons.py`（新路由）
- Test: `backend/tests/test_meoz_strongest_vent.py`
- Modify: `frontend/src/lib/api.ts`、`frontend/src/lib/queryKeys.ts`、`frontend/src/lib/useMeozThemeAttribution.ts`（或新建 `frontend/src/lib/useMeozWidgets.ts`，二选一，倾向新建以免该文件变杂）
- Create: `frontend/src/components/dashboard/StrongestVentWidget.tsx` 及 `.test.tsx`
- Modify: `frontend/src/components/dashboard/layout.ts`、`registry.tsx`（`WidgetCtx` 增加 `tradeDate: string`）、`frontend/src/pages/Dashboard.tsx`
- Modify: `docs/猫爪数据对接/接口可用性与口径核对表.md`（`fengk_kp` 行补字段与 `tradedate` 能力）

**Interfaces:**
- Produces（后端）：
  - `MeozProvider.get_strongest_vent(self, day: date | None = None) -> list[dict]`，每行 `{symbol: str, name: str, strength: int, pct_chg: float | None, main_net_amount: float | None, main_buy_amount: float | None, main_sell_amount: float | None, signal_at: str | None, themes: list[str]}`；`day` 非空时传 `{"tradedate": _ymd(day)}`，空则 `{}`；`pct_chg` 除以 100；`signal_at` 为北京时间 ISO（秒）；`themes` 按「、」拆分并去空；`symbol` 经 `_full_symbol(row.get("symbol"), self._universe_map())`，映射不到或 `strength` 为空的行跳过。
  - `app.services.meoz_strongest_vent.read(day: date | None) -> dict` 返回 `{fetched_at: str, trade_date: str | None, rows: list[dict]}`；`class StrongestVentUnavailableError(RuntimeError)`；`day` 在未来 → `ValueError`；实时缓存 60 秒（`LIVE_TTL_S = 60.0`），历史日进程内缓存最多 16 个日期（成功且非空才缓存）；`reset_for_tests()`。
  - 路由 `GET /api/meoz/strongest-vent?date=YYYY-MM-DD`（可选）：`ValueError`→422，`StrongestVentUnavailableError`→503，其他异常→502「猫爪最强风口暂时不可用，请稍后重试」。
- Produces（前端）：`MeozStrongestVentRow`、`MeozStrongestVent` 类型；`api.meozStrongestVent(date?: string)`；`QK.meozStrongestVent(date: string)`；`useMeozStrongestVent(date: string | undefined, live: boolean)`；`StrongestVentWidget(props: { tradeDate: string; isLatestDate: boolean; onOpenStock: (symbol, name?, navList?) => void })`；`WidgetCtx.tradeDate`（取 `currentDate`，见 `Dashboard.tsx`）。

- [ ] **Step 1: 写后端失败测试** `test_meoz_strongest_vent.py`（沿用 `test_meoz_theme_attribution.py` 的 `FakeClient`/`SimpleNamespace` 风格）：
  - provider：样本行 `{symbol:'000530', name:'冰山冷热', strength:'4893', pct_chg:'10.03', main_net_amount:'967921794', main_buy_amount:'1598063994', main_sell_amount:'-630142200', signal_time:'1795138380', selected_themes:'PCB、芯片'}` → `pct_chg == pytest.approx(0.1003)`、`themes == ['PCB','芯片']`、`signal_at` 以 `+08:00` 结尾；`selected_themes=None` → `themes == []`；`strength` 为空或 `symbol` 无法映射的行被跳过；传 `day` 时 `FakeClient` 收到 `{"tradedate": "YYYYMMDD"}`（格式以 `_ymd` 为准）。
  - service：实时命中缓存（第二次调用不再请求 provider）；历史日非空才缓存；未来日期抛 `ValueError`；`get_provider` 抛 `ValueError` → `StrongestVentUnavailableError`；provider 缺少 `get_strongest_vent` → 同样不可用。
  - 路由：`TestClient` 验证 422 / 503 / 502 映射（参照 `test_routes_map_errors`）。

- [ ] **Step 2: 运行确认失败**：`cd backend && .venv/Scripts/python.exe -m pytest tests/test_meoz_strongest_vent.py -q`，预期失败（方法/模块不存在）。

- [ ] **Step 3: 实现** provider 方法、服务、路由。provider 的 `strength` 用 `_to_int`，金额用 `_to_float`；服务结构照抄 `meoz_hot_rank.py`（锁 + `time_mod.monotonic()` 缓存）。

- [ ] **Step 4: 运行后端测试通过**：同 Step 2 命令，预期全部 PASS；再跑 `tests/test_meoz_theme_attribution.py` 确认未破坏。

- [ ] **Step 5: 写前端失败测试** `StrongestVentWidget.test.tsx`（mock `api.meozStrongestVent`）：
  - 实时：渲染排名、名称、涨幅（`0.1003` → `+10.03%`）、强度、主力净额（`967921794` 显示 `9.68亿` 或 `fmtBigNum` 的等价输出）、题材标签；`themes: []` 的行不渲染标签容器；点击行调用 `onOpenStock`。
  - 历史日：`isLatestDate=false` 时请求带 `tradeDate`、标题含「回看 2026-11-19」；请求失败 → 「来源不可用」且不显示实时数据；`rows: []` → 「该日无数据」；实时空 → 「暂无信号」。
  - 实时模式下 `refetchInterval` 为 60000，历史日为 `false`（通过 hook 的选项断言）。

- [ ] **Step 6: 运行确认失败**：`pnpm exec vitest run src/components/dashboard/StrongestVentWidget.test.tsx`。

- [ ] **Step 7: 实现前端**：`api.ts` 加类型与 `meozStrongestVent: (date?: string) => request<MeozStrongestVent>(date ? \`/api/meoz/strongest-vent?date=${date}\` : '/api/meoz/strongest-vent', { quiet: true, timeoutMs: 60_000 })`；`queryKeys.ts` 加键；hook 用 `quiet`（把 422/502/503 变成 `null`，同 `useMeozThemeAttribution.ts`），`placeholderData` 不跨日期保留（日期变化时不显示上一个日期的数据）。组件布局按设计文档 6.3：主行（排名、名称、涨幅、强度、主力净额）+ 题材标签行，买入/卖出额放悬停 `title`。`Dashboard.tsx` 的 `widgetCtx` 增加 `tradeDate: currentDate`，`PreviewSource` 加 `'strongest-vent'`；`registry.tsx` 注册 `{ id: 'strongest-vent', label: '最强风口', icon: Compass, minW: 3, minH: 7, defW: 4, defH: 9 }`，`layout.ts` 增加类型。

- [ ] **Step 8: 运行前端测试与类型检查**：`pnpm exec vitest run src/components/dashboard src/pages/Dashboard.test.tsx src/pages/Dashboard.status.test.tsx` 全 PASS；`pnpm exec tsc --noEmit` 无新增错误。

- [ ] **Step 9: 更新核对表**：把 `fengk_kp` 一行补上字段、`tradedate` 有效/`date` 无效、百分数口径；历史最远日期用一次真实只读探测确认（需用户已授权 Key 的前提下，本机脚本，不入库），记录到该行。

- [ ] **Step 10: 页面验证**（临时数据目录）：实时与回看历史日各验证一次；未配置猫爪时显示「来源不可用」。

- [ ] **Step 11: 检查 `git diff` / `git status` 并汇报，不提交。**

---

### Task 3: 新闻（后端 + 前端）

**Files:**
- Modify: `backend/app/plugins/meoz/provider.py`（新增 `get_news`）
- Create: `backend/app/services/meoz_news.py`
- Modify: `backend/app/custom/meoz_reasons.py`
- Test: `backend/tests/test_meoz_news.py`
- Modify: `frontend/src/lib/api.ts`、`queryKeys.ts`、Task 2 中选定的 hook 文件
- Create: `frontend/src/components/dashboard/NewsWidget.tsx` 及 `.test.tsx`
- Modify: `layout.ts`、`registry.tsx`、`Dashboard.tsx`（`PreviewSource` 无需改，新闻不开个股）
- Modify: `docs/猫爪数据对接/接口可用性与口径核对表.md`（新增 `news` 行）

**Interfaces:**
- Produces（后端）：
  - `MeozProvider.get_news(self, limit: int = 50) -> list[dict]`：调用 `_query_retry("news", {"limit": limit})`；每行只保留 `{id: str, published_at: str | None, display_at: str, time_label: str | None, source: str, source_name: str, title: str, summary: str | None, url: str | None, item_type: str | None}`；`id` 取 `source_id` 的字符串并与 `source` 拼接；`display_at` 无时区，按北京时间补 `+08:00`；标题为空或 `display_at` 无法解析的行跳过；不保留 `image`。
  - `app.services.meoz_news.read(limit: int = 50) -> dict`：`{fetched_at: str, rows: list[dict], sources: list[{source: str, source_name: str}]}`；`rows` 按 `display_at` 倒序（同时刻保持上游顺序，用稳定排序）；`limit` 限制在 1–100（超出抛 `ValueError`）；缓存 60 秒；`NewsUnavailableError`；`reset_for_tests()`。
  - 路由 `GET /api/meoz/news?limit=50`：422 / 503 / 502 映射同 Task 2。
- Produces（前端）：`MeozNewsRow`、`MeozNews` 类型；`api.meozNews(limit?: number)`；`QK.meozNews`；`useMeozNews(enabled: boolean)`；`NewsWidget(props: { source?: string; isLatestDate: boolean; onSourceChange: (source: string) => void })`，`source` 取值 `'all'` 或某个来源 `source` 字段值。

- [ ] **Step 1: 写后端失败测试** `test_meoz_news.py`：
  - provider：样本 `{published_at:None, display_at:'2026-11-20T21:01:05', time_label:'21:01', source:'yicai_home', source_name:'第一财经头条', title:'逼近20万亿元！详解前10月全国财政数据', summary:None, url:'https://www.yicai.com/news/103448373.html', image:None, rank:1, source_id:'103448373', item_type:'link', updated_at:'2026-11-20T21:01:05'}` → 输出含 `display_at` 以 `+08:00` 结尾、无 `image` 键、`id == 'yicai_home:103448373'`；`title` 为空的行被跳过；`display_at` 异常的行被跳过；调用参数含 `{"limit": 50}`。
  - service：多来源按 `display_at` 倒序、同时刻保持原序；`sources` 去重且保持首次出现顺序；缓存命中；`limit=0` 或 `101` → `ValueError`；插件缺失 → `NewsUnavailableError`。
  - 路由：422 / 503 / 502。

- [ ] **Step 2: 运行确认失败**：`cd backend && .venv/Scripts/python.exe -m pytest tests/test_meoz_news.py -q`。

- [ ] **Step 3: 实现** provider 方法、服务、路由（结构同 Task 2）。**先核对时区**：用一次只读真实探测（本机脚本，不入库）对照若干行的 `time_label` 与 `display_at`，确认 `display_at` 就是北京时间，结果写进核对表 `news` 行；若不是，按实际口径调整并同步修改测试。

- [ ] **Step 4: 运行后端测试通过**（同 Step 2 命令）。

- [ ] **Step 5: 写前端失败测试** `NewsWidget.test.tsx`（mock `api.meozNews`）：
  - 渲染时间标签、来源名、标题；标题为链接 `target="_blank"` 且 `rel="noopener noreferrer"`。
  - `url` 为 `javascript:alert(1)` 或空 → 标题是纯文本，无 `<a>`。
  - 来源分段由返回的 `sources` 动态生成（含「全部」）；选「财联社头条」后只显示该来源，并调用 `onSourceChange('cls_depth_1000')`；传入的 `source` 不在 `sources` 中时回退为 `all`。
  - 有 `summary` 的行有展开箭头，点击展开显示摘要；无 `summary` 的行没有箭头。
  - `isLatestDate={false}` → 「新闻仅实时数据」且 `api.meozNews` 未调用；请求失败 → 「来源不可用」；`rows: []` → 「暂无新闻」。

- [ ] **Step 6: 运行确认失败**：`pnpm exec vitest run src/components/dashboard/NewsWidget.test.tsx`。

- [ ] **Step 7: 实现前端**：`api.meozNews` 请求 `/api/meoz/news?limit=50`；hook `useMeozNews(enabled)` 60 秒轮询、`quiet`、保留上一份数据；组件按设计文档 6.3，链接判断用 `/^https?:\/\//i`，列表容器 `min-h-0 overflow-y-auto`，标题单行截断、悬停显示完整标题。`registry.tsx` 注册 `{ id: 'news', label: '新闻', icon: Newspaper, minW: 3, minH: 8, defW: 4, defH: 12 }`（`Newspaper` 从 `lucide-react` 导入），`layout.ts` 增加类型。

- [ ] **Step 8: 运行前端测试与类型检查**：`pnpm exec vitest run src/components/dashboard src/pages/Dashboard.test.tsx src/pages/Dashboard.status.test.tsx` 全 PASS；`pnpm exec tsc --noEmit` 无新增错误。

- [ ] **Step 9: 页面验证**（临时数据目录）：来源筛选、展开摘要、点击标题新标签打开、回看历史日提示、窄宽度不溢出；三个组件同时在看板上时确认后端日志无 429。

- [ ] **Step 10: 全量验证**：按 CONTRIBUTING §9 选择后端与前端对应的测试集运行（先用 `Grep "^## 9\."` 定位该章节再读），汇报通过/未运行项。

- [ ] **Step 11: 检查 `git diff` / `git status` 并汇报，不提交。**
