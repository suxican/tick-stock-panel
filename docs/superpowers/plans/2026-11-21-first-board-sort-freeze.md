# 首板候选表盘中排序冻结 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 候选表在数据实时刷新时保持行的先后顺序不变，用户操作或点“重排”时才按最新数据重排，并提示顺序是否有变化。

**Architecture:** 新增纯函数 `applyFrozenOrder`；`useCandidateView` 在排序结果之后套这一层，用“重置签名”判断何时重排；`CandidateTable` 在标题旁渲染提示条。纯前端，不改后端和排序规则本身。

**Tech Stack:** React 18、TypeScript、vitest（jsdom）。

**Spec:** `docs/superpowers/specs/2026-11-21-first-board-sort-freeze-design.md`

## Global Constraints

- 冻结的只是顺序：价格、涨幅、评分、状态、结论、可提示状态始终是最新的；状态分段计数（`stateCounts`、`stateTotal`）按实时数据算，不受冻结影响。
- 立即重排的时机仅限：排序列或方向变化；任一筛选变化（状态、可提示、主线、形态、重点关注、评分筛选、板块）；星标集合变化；点提示条；离开“今日盯盘”页签后再回来；首次载入。
- 新出现的行追加在末尾，消失的行直接去掉，其余行保持相对顺序；消失后又出现的行视为新出现。
- 提示条按钮文字固定：`排序有变化 · 点击重排`；有新提示时为 `排序有变化 · 新提示 {n} 只 · 点击重排`。顺序相同时不渲染。有新提示时用蓝色强调（`border-accent/50 bg-accent/10 text-foreground`），否则 `border-border text-secondary`；两种都有文字，不只靠颜色。
- “新提示”= 当前快照有效（`current`）且 `can_buy` 的行，其键不在“上次重排时已可提示的行”集合里（含新出现的可提示行）。
- 行的键沿用 `CandidateTable` 现有的 `${row.symbol}-${row.pattern}`，假定唯一。
- 股票预览弹窗的上一只、下一只使用显示出来的（冻结后的）顺序。
- 修改已有文件保持 CRLF（`CandidateTable.tsx`、`FirstBoard.tsx`、`FirstBoard.test.tsx` 都是 CRLF；新文件也写成 CRLF）。
- 前端命令只能用 `pnpm exec` 或 `pnpm build`（在 `frontend/` 下），禁止 npx 或 npm。
- 不提交、不推送；用户要求提交时使用 `git commit -s`，且不带上工作区里原有的 `frontend/src/lib/queryKeys.ts` 修改和无关的未跟踪文档。

## Review Focus

1. **筛选变化后不能留下陈旧的行**：筛选一变就重排，已不满足筛选的行必须消失。→ Task 2 `re-sorts immediately on sort, filter and star changes`
2. **行消失后又出现**：按新行追加到末尾，不回到原位。→ Task 1 `treats a row that disappeared and came back as new`
3. **首次数据到达前表为空**：之后第一批数据按实时排序显示，不是乱序。→ Task 1 `shows the live order when nothing was frozen yet`
4. **刷新失败时保留上一份数据**：行相同则不出现提示条。→ Task 2 `shows no hint when the order did not change`
5. **离开今日盯盘页签再回来**：回来时按最新数据排序，不沿用旧顺序。→ Task 2 `re-sorts when returning to the live tab`
6. **新提示被冻结压在下面**：提示条要显示数量，且行本身仍有可提示高亮。→ Task 2 `counts new buy hints without moving rows`

---

### Task 1: 纯函数 `applyFrozenOrder`

**Files:**
- Create: `frontend/src/custom/first-board/frozenOrder.ts`
- Create: `frontend/src/custom/first-board/frozenOrder.test.ts`

**Interfaces:**
- Produces: `export function applyFrozenOrder<T>(live: T[], previous: string[] | null, keyOf: (row: T) => string): { rows: T[]; keys: string[]; changed: boolean }`
  - `previous` 为 `null` 或 `[]`：`rows` 为 `live` 原顺序，`changed` 为 false。
  - 否则：`rows` = 按 `previous` 顺序保留仍在 `live` 中的行，再按 `live` 的顺序追加不在 `previous` 中的行；`keys` 为 `rows` 的键；`changed` 为 `keys` 与 `live` 的键序列是否不同。
  - 返回的 `rows` 元素是 `live` 里的最新对象（不是旧对象）。

- [ ] **Step 1: 写失败测试 `frozenOrder.test.ts`**（行用 `{ id: string; v: number }`，键为 `id`）

```ts
it('shows the live order when nothing was frozen yet', () => {
  // previous null 与 [] → rows 的 id 顺序等于 live，changed 为 false，keys 等于 live 的键
})
it('keeps the previous order but uses the latest row objects', () => {
  // previous ['a','b','c']；live 为 b(v=9)、a、c → rows id 为 ['a','b','c']，且 rows[1].v === 9；changed 为 true
})
it('appends new rows at the end in live order and drops missing ones', () => {
  // previous ['a','b','c']；live 为 d、a、c、e（b 消失）→ rows id 为 ['a','c','d','e']；keys 同序
})
it('treats a row that disappeared and came back as new', () => {
  // 第一步 previous ['a','b','c']，live 为 a、c → keys ['a','c']；第二步以该 keys 为 previous，live 为 b、a、c → rows id 为 ['a','c','b']
})
it('reports no change when the frozen order equals the live order', () => {
  // previous ['a','b']，live 为 a、b（值不同）→ changed 为 false
})
```

- [ ] **Step 2: 运行，确认失败**

Run（`frontend/` 下）：`pnpm exec vitest run src/custom/first-board/frozenOrder.test.ts`
Expected: FAIL，找不到 `./frozenOrder`

- [ ] **Step 3: 实现 `applyFrozenOrder`**（按上面的 Interfaces；文件开头一段注释写明用途：盘中刷新时保持行序、用户操作才重排）

- [ ] **Step 4: 运行，确认通过**

Run: `pnpm exec vitest run src/custom/first-board/frozenOrder.test.ts`
Expected: PASS

---

### Task 2: 接入 `useCandidateView`、提示条与页面测试

**Files:**
- Modify: `frontend/src/custom/first-board/CandidateTable.tsx`（`useCandidateView` 约 44–96 行；`CandidateTable` 标题行约 303 行）
- Modify: `frontend/src/custom/first-board/FirstBoard.tsx:69`（传入第四个参数）
- Test: `frontend/src/custom/first-board/FirstBoard.test.tsx`

**Interfaces:**
- Consumes: Task 1 的 `applyFrozenOrder`
- Produces: `useCandidateView(all, current, marks, active = true)`，在原有返回值基础上：
  - `rows`：显示用的行（冻结后的顺序）
  - `orderChanged: boolean`、`newBuyCount: number`、`reorder: () => void`
- 实现要点：
  - 现有 `useMemo` 先得到实时排序的行 `liveRows`（含星标置顶），再用一个 `useRef` 保存 `{ signature, keys, buyable }`，在第二个 `useMemo`（依赖 `liveRows` 与签名）里调用 `applyFrozenOrder`。
  - 重置签名 = 字符串化的 `[sort?.key, sort?.dir, filter, stateFilter, onlyActionable, onlyMainline, attentionFilter, priorityFilter, boardId, 被星标代码排序后拼接, 重排计数, active]`。签名变化时 `previous` 传 `null`，并把 `buyable` 重置为当前可提示行的键集合；`active` 为 false 时也走重置。
  - `newBuyCount` = `liveRows` 中 `current && row.can_buy` 且键不在 `buyable` 的行数。
  - `reorder` 递增一个计数状态，使签名变化。
- 提示条：放在 `CandidateTable` 标题“今日候选 N 条”之后，`orderChanged` 为 true 时渲染一个 `<button type="button">`，外层包 `aria-live="polite"`；点击调用 `view.reorder`。

- [ ] **Step 1: 改写已有用例并新增页面级用例（`FirstBoard.test.tsx`）**

先改已有用例 `preserves sorting across filters, an empty result and a refreshed snapshot`（约 881 行）：刷新后的断言 `expect(candidateNames()).toEqual(['A', 'E', 'C'])` 改为 `['C', 'A', 'E']`（冻结：C、A 保持原序，新行 E 追加），随后 `await click('排序有变化 · 点击重排')` 再断言 `['A', 'E', 'C']`；其后的断言保持不变。

新增用例（行用 `candidate('A', { change_pct: .09, can_buy: false })` 等，不带评分，所以默认按涨幅降序；`firstBoardSnapshot` 先后 mock 两份数据，刷新方式同上面的已有用例）：

```tsx
it('keeps the row order after a refresh, updates the values and offers a re-sort', async () => {
  // 初始 A .09、B .07、C .05 → ['A','B','C']，无提示条
  // 刷新后 B .10、A .09、C .05 → candidateNames 仍为 ['A','B','C']；B 行文字含 '+10.00%'；
  // 提示条按钮文字为 '排序有变化 · 点击重排'；点击后顺序为 ['B','A','C']，提示条消失
})
it('appends new rows and drops missing ones without moving the others', async () => {
  // 初始 A .09、B .07、C .05；刷新为 A .09、C .05、D .20（B 消失）→ ['A','C','D']；提示条出现（实时序为 D,A,C）
})
it('shows no hint when the order did not change', async () => {
  // 刷新为同样的顺序、不同价格 → 无提示条按钮；刷新请求失败（firstBoardRefresh reject）后保留旧数据，同样无提示条
})
it('counts new buy hints without moving rows', async () => {
  // 初始 A .09(can_buy false)、B .07(false)；刷新为 B .10(can_buy true)、A .09(false)
  // → 顺序仍为 ['A','B']；按钮文字含 '新提示 1 只'，class 含 'accent'；B 行带可提示高亮（行 class 含 'bg-accent/5'）
})
it('re-sorts immediately on sort, filter and star changes', async () => {
  // 先制造“冻结且有变化”的状态；点表头 '现价' 排序 → 立即按新规则排序且提示条消失
  // 再制造变化；切换 '只看可提示候选' → 立即重排，且不满足筛选的行不在表中
  // 再制造变化；点某行的星标 → 该行立即置顶
})
it('re-sorts when returning to the live tab', async () => {
  // 制造冻结且有变化；点 '信号记录' 再点 '今日盯盘' → 顺序为最新实时序，无提示条
})
it('uses the displayed order for stock preview navigation', async () => {
  // 制造冻结且有变化；点开第一行股票 → 弹窗的 data-nav-list 的代码顺序等于当前显示顺序（不是实时序）
})
```

- [ ] **Step 2: 运行，确认失败**

Run: `pnpm exec vitest run src/custom/first-board/FirstBoard.test.tsx`
Expected: 新增用例和改写的用例 FAIL；其余 PASS

- [ ] **Step 3: 按 Interfaces 实现；`FirstBoard.tsx` 传入 `tab === 'live'`**

- [ ] **Step 4: 运行首板前端全部测试并构建**

Run: `pnpm exec vitest run src/custom/first-board` 然后 `pnpm build`
Expected: 全部 PASS，构建成功。若其他已有用例因为“刷新后顺序不再自动变化”而失败，逐个判断：属于冻结的预期后果的，改成先点提示条或先触发重排再断言，不能删除断言；每处改动都要在汇报里列出。

---

### Task 3: 验证与汇报

- [ ] **Step 1**：`git diff --check`（CRLF 引起的“尾随空白”提示属于仓库现状，忽略）。
- [ ] **Step 2: 浏览器检查**：休市时数据不变，页面应无提示条，表格与之前一致；截图。盘中刷新下的行为无法在休市时看到。
- [ ] **Step 3: 汇报**：说明改了什么、改动了哪些已有测试断言（逐条列出）、验证了什么；明确写出盘中实际表现未实测；不提交。
