# 首板候选表按题材分组 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 候选表新增“按题材分组”视图：每只候选归属一个题材，同题材聚在一起，组头给出候选数、昨日主线、同行均涨、上涨占比和领涨股。

**Architecture:** 纯函数 `groupCandidatesByTheme` 对已冻结排序后的显示行分组；`useCandidateView` 在冻结之后套这一层并保存开关；`CandidateTable` 在表体里渲染组头行并支持折叠。纯前端，不改后端。

**Tech Stack:** React 18、TypeScript、Tailwind、vitest（jsdom）。

**Spec:** `docs/superpowers/specs/2026-11-21-first-board-theme-grouping-design.md`

## Global Constraints

- 开关“按题材分组”默认关闭，状态保存在 `localStorage` 键 `first-board-group-by-theme`（在 `lib/storage.ts` 里新增 `firstBoardGroupByTheme: kv<boolean>('first-board-group-by-theme')`）；关闭时表格 DOM 与现在完全一致。
- 候选的可选题材 = `attention_context.groups` 里 `kind === 'concept'` 的板块（不要求联动数据有效，名称和成分数是静态信息）∪ `mainline.matches` 里 `kind === 'concept'` 的项；按**概念名称**识别。
- 归属规则，按顺序取第一个分出胜负的：① 当前显示的候选里共享该概念的**不同股票**更多者（同一只股票的多个形态只算一次）；② 命中昨日主线者，主线名次靠前者优先；③ 概念成分数（`member_count`）更少者，缺失视为最多；④ 概念名称 `localeCompare`。没有任何概念则归到行业（`row.sector`，键 `industry:{名称}`），没有行业则归到“未归类”（键 `none`）。归属规则不得使用盘中会波动的数字。
- 组的先后按各组第一行在传入顺序里的位置；组内顺序保持传入顺序。
- 组头统计（同行均涨 `mean_change_pct`、上涨占比 `up_ratio`、领涨股 `leader`）只对概念组显示，取自该组里第一只拥有该概念联动数据的候选，且要求该候选的联动上下文有效（`hasCurrentAttention(context, current)`）；否则 `stats` 为 `null`，界面显示“—”，不编造；行业组和“未归类”组不显示统计。
- 组头文字：`{题材名}`、`候选 {n} 只`、昨日主线时 `昨日主线第 {k}`、`同行均涨 {fmtPct}`、`上涨占比 {pct(up_ratio)}`、`领涨 {名称}`（`at_limit === true` 追加“（涨停）”，`false` 追加“（已开板）”，`null` 不追加）。
- 组头是 `<tr data-theme-header>`，内含 `colSpan={candidateColumns.length}` 的单元格和一个折叠按钮 `aria-expanded`、`aria-label` 为 `折叠题材 {名称}`／`展开题材 {名称}`；折叠状态只在本次访问内有效，默认全部展开；折叠的组不渲染其数据行，但这些行仍计入“今日候选 N 条”和预览翻页顺序。
- `useCandidateView` 返回的 `rows` 在分组打开时为分组后的顺序（`groups.flatMap(g => g.rows)`），关闭时为现在的顺序；新增返回 `groupByTheme: boolean`、`setGroupByTheme(value: boolean)`、`groups: ThemeGroup[] | null`。
- 保持 CRLF（`CandidateTable.tsx`、`FirstBoard.test.tsx`、`storage.ts` 都是 CRLF；新文件也写成 CRLF）。
- 前端命令只能用 `pnpm exec` 或 `pnpm build`（在 `frontend/` 下），禁止 npx 或 npm。
- 不提交、不推送；用户要求提交时使用 `git commit -s`，且不带上工作区里原有的 `frontend/src/lib/queryKeys.ts` 修改和无关的未跟踪文档。

## Review Focus

1. **同一只股票的两个形态**（同代码、不同 `pattern`）：共享计数只算一次，两行归到同一组。→ Task 1 `counts a stock once even with several patterns`
2. **联动数据缺失或已过期**：组头统计显示“—”，不报错，不出现 0。→ Task 1 `has no stats without current attention data`；Task 2 页面用例
3. **冻结顺序不被分组打乱**：组内保持冻结的显示顺序；刷新后组内、组间不换位。→ Task 2 `keeps the frozen order inside and between groups`
4. **折叠的组**：行不渲染，但计数与预览翻页不变。→ Task 2 `collapses a group without changing the count or navigation`
5. **没有概念、没有行业**：归到“未归类”，不丢行。→ Task 1 `falls back to the industry and then to the unclassified group`
6. **昨日主线概念不在联动板块里**：仍能成为候选的可选题材。→ Task 1 `treats a mainline concept as a candidate theme`

---

### Task 1: 纯函数 `groupCandidatesByTheme`

**Files:**
- Create: `frontend/src/custom/first-board/themeGroups.ts`
- Create: `frontend/src/custom/first-board/themeGroups.test.ts`

**Interfaces:**
- Consumes: `FirstBoardCandidate`、`hasCurrentAttention`（来自 `./AttentionCell`）
- Produces:
  ```ts
  export interface ThemeGroup {
    key: string                       // 'concept:名称' | 'industry:名称' | 'none'
    name: string                      // 概念名／行业名／'未归类'
    kind: 'concept' | 'industry' | 'none'
    mainlineRank: number | null       // 命中昨日主线时的名次
    rows: FirstBoardCandidate[]
    stats: { meanChangePct: number | null; upRatio: number | null; leader: { name: string; atLimit: boolean | null } | null } | null
  }
  export function groupCandidatesByTheme(rows: FirstBoardCandidate[], current: boolean): ThemeGroup[]
  ```
  - `leader.name` 取 `group.leader.name || group.leader.symbol`。

- [ ] **Step 1: 写失败测试 `themeGroups.test.ts`**（行用最小对象：`{ symbol, pattern, sector, attention_context: { schema_version: 1, status: 'ready', groups: [...] }, mainline: { hit, date, matches, reason } } as unknown as FirstBoardCandidate`；概念板块只需 `id、kind、name、member_count、mean_change_pct、up_ratio、leader`）

```ts
it('puts stocks that share a concept into one group, first appearance order', () => {
  // A 有概念 X(成分 50) 与 Y(成分 5)，B 有 X，C 有 Z；传入顺序 C、A、B
  // → 组顺序 [Z(C)]、[X(A,B)]；A 不因 Y 成分更少而归到 Y（共享计数优先）
})
it('counts a stock once even with several patterns', () => {
  // A 的 platform、trend 两行都有概念 X，B 只有概念 Y（成分更少）→ X 与 Y 共享数都是 1，Y 成分更少… 构造使得若按行计数则 X 为 2 而按股票计数为 1，断言按股票计数后 A 两行归到同一组
})
it('breaks ties with the mainline and then with fewer members and then by name', () => {
  // 无共享：A 有 P(主线第 2) 与 Q → 归 P，mainlineRank 为 2；
  // B 有 R(成分 30) 与 S(成分 10) → 归 S；C 的两个概念成分都缺失 → 按名称较小者
})
it('treats a mainline concept as a candidate theme', () => {
  // A 的联动板块里没有概念 M，但 mainline.matches 含概念 M（第 1）→ 归 M，mainlineRank 为 1
})
it('falls back to the industry and then to the unclassified group', () => {
  // A 无概念、sector 为 '元件-被动元件' → kind 'industry'、key 'industry:元件-被动元件'；B 无概念无行业 → key 'none'、name '未归类'；两者 stats 都为 null
})
it('keeps the incoming order inside a group', () => {
  // X 组含 B、A（传入顺序 B 在 A 前）→ rows 为 [B, A]
})
it('has stats from the first member with current attention data', () => {
  // X 组两行：第一行联动 status 'stale'（无效）、第二行有效 → stats 取第二行：meanChangePct、upRatio、leader { name, atLimit }
})
it('has no stats without current attention data', () => {
  // current=false 或所有行联动无效 → 概念组 stats 为 null
})
```

- [ ] **Step 2: 运行，确认失败**

Run（`frontend/` 下）：`pnpm exec vitest run src/custom/first-board/themeGroups.test.ts`
Expected: FAIL，找不到 `./themeGroups`

- [ ] **Step 3: 实现 `groupCandidatesByTheme`**（文件开头一段注释写明用途与归属规则）

- [ ] **Step 4: 运行，确认通过**

Run: `pnpm exec vitest run src/custom/first-board/themeGroups.test.ts`
Expected: PASS

---

### Task 2: 开关、分组渲染与页面测试

**Files:**
- Modify: `frontend/src/lib/storage.ts`（新增 `firstBoardGroupByTheme`，放在 `firstBoardFeedbackExpanded` 旁边）
- Modify: `frontend/src/custom/first-board/CandidateTable.tsx`（`useCandidateView` 约 44–125 行；`CandidateFilters` 约 128 行起；`CandidateTable` 的表体）
- Test: `frontend/src/custom/first-board/FirstBoard.test.tsx`

**Interfaces:**
- Consumes: Task 1 的 `groupCandidatesByTheme`、`ThemeGroup`
- Produces: `useCandidateView` 的返回值新增 `groupByTheme`、`setGroupByTheme`、`groups`；`rows` 的含义见 Global Constraints。
- 实现要点：
  - `useCandidateView` 里在冻结之后：`const groups = useMemo(() => groupByTheme ? groupCandidatesByTheme(shown.rows, current) : null, [...])`，`rows = groups ? groups.flatMap(g => g.rows) : shown.rows`。开关用 `useState(() => storage.firstBoardGroupByTheme.get(false) === true)`，`setGroupByTheme` 同时写入存储。
  - `CandidateFilters` 在“只看主线”之后增加一个同样样式的复选 chip，文字“按题材分组”。
  - `CandidateTable` 内保存折叠状态 `useState<ReadonlySet<string>>`（元素是 `ThemeGroup.key`）；表体在分组打开时按组渲染：组头行 + （未折叠时）该组的 `CandidateRow`；`key` 沿用 `${symbol}-${pattern}`。
  - 星标置顶（`markedFirst`）在冻结之前已应用，分组只重排“显示顺序里的相对位置”，不再单独处理星标。

- [ ] **Step 1: 写失败测试（`FirstBoard.test.tsx` 末尾追加）**

助手：`const headers = () => [...host.querySelectorAll('tr[data-theme-header]')]`；构造候选用 `candidate(name, { attention_context: attention({ groups: [...] }), mainline: {...} })`（`attention` 助手在文件中已有，约 380 行；它的 `groups` 项需要 `id、kind、name、member_count、mean_change_pct、up_ratio、leader` 等字段，沿用该助手里的字段形状）。

```tsx
it('does not group by default and remembers the switch', async () => {
  // 默认没有 tr[data-theme-header]；勾选 '按题材分组' → 出现组头，localStorage 键 'first-board-group-by-theme' 为 'true'
  // 卸载后重新渲染 → 开关已勾选且仍分组
})
it('groups candidates that share a theme and shows the group stats', async () => {
  // A、B 共享概念 '先进封装'（A、B 联动有效，mean_change_pct .031、up_ratio .68、leader 名 '华微电子' at_limit true），C 无共享且无概念有行业
  // → 两个组头：'先进封装' 的组头文字含 '候选 2 只'、'同行均涨 +3.10%'、'上涨占比 68.00%'、'领涨 华微电子（涨停）'；候选表行顺序为该组两行在前或按组首位置排列
  // 昨日主线命中时组头含 '昨日主线第 1'
})
it('shows placeholders when the attention data is not current', async () => {
  // 联动数据 status 'stale'（或快照不是 ready）→ 组头统计显示 '—'，不含 '0.00%'
})
it('keeps the frozen order inside and between groups', async () => {
  // 开启分组；先后两个快照使实时排序反转 → 组间、组内的显示顺序都不变；点 '排序有变化 · 点击重排' 后才变化
})
it('collapses a group without changing the count or navigation', async () => {
  // 点某组头的折叠按钮 → aria-expanded 为 'false'，该组的行不在表格里；'今日候选 N 条' 的 N 不变
  // 打开其他组里某只股票的预览 → data-nav-list 仍含被折叠组里的股票，且顺序是分组后的顺序
  // 再点一次 → 行恢复
})
```

- [ ] **Step 2: 运行，确认失败**

Run: `pnpm exec vitest run src/custom/first-board/FirstBoard.test.tsx`
Expected: 新增用例 FAIL，其余 PASS

- [ ] **Step 3: 实现（storage、hook、筛选 chip、表体渲染）**

- [ ] **Step 4: 运行首板前端全部测试并构建**

Run: `pnpm exec vitest run src/custom/first-board` 然后 `pnpm build`
Expected: 全部 PASS，构建成功；已有用例不开启分组，不应受影响，若有失败逐个判断，不得删除断言。

---

### Task 3: 验证与汇报

- [ ] **Step 1**：`pnpm exec vitest run`（整个前端套件）全部通过；`git diff --check`（CRLF 引起的“尾随空白”提示属于仓库现状，忽略）。
- [ ] **Step 2: 浏览器检查**：休市时数据不变，可直接看效果：勾选“按题材分组”，应出现组头（先进封装等）；折叠、展开、刷新页面后开关仍勾选；截图。
- [ ] **Step 3: 汇报**：说明改了什么、验证了什么、盘中刷新下的表现靠测试验证；不提交。
