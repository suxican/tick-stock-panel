# 首板临板焦点区 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 盘中在候选表上方用固定高度的大卡片展示“临近涨停”的候选（最多 3 张），显示能不能买、买多少、不能买差在哪，买入入口与表格口径一致。

**Architecture:** 纯函数 `pickImminent` 负责选卡和保持位置；`ImminentCards` 组件直接读快照里的候选（不经表格的筛选和冻结顺序）；把表格里的“模拟买入”按钮与建议金额抽成共用的 `BuyAction`，卡片和表格同用。纯前端。

**Tech Stack:** React 18、TypeScript、Tailwind、vitest（jsdom）。

**Spec:** `docs/superpowers/specs/2026-11-21-first-board-imminent-cards-design.md`

## Global Constraints

- 入选 = `row.state === 'approaching'` 且快照有效（与表格同一个 `current`：快照状态 ready 且无错误）；不另设阈值，不改后端和买入条件。
- 排序：`can_buy` 为真的在前；其次按 `distance_to_limit_pct` 从小到大（`null` 排最后）；再按 `${symbol}-${pattern}` 稳定排序。最多 3 张，超出时标题写 `另有 {n} 只，见下表`。
- 位置稳定：上一次已显示且仍入选的卡片保持原位（最多 3 张）；空位由排序最靠前的未显示候选补上；新出现的追加在后面；消失后又出现的视为新出现。
- 卡片不受表格筛选、排序冻结、星标置顶影响。
- 固定高度：快照有效时，卡片区内容区固定为 `h-64`（16rem），内容超出时在区内滚动（实测带评分的可提示卡片加按钮会超过 12rem，所以不用 min-h）；没有候选时在里面显示一行 `当前没有临近涨停的候选`；卡片横向排列，窄屏横向滚动而不是换行堆叠，保证高度恒定。快照无效（休市、过期、出错）时整块不渲染。
- 卡片内容：名称（点击打开预览）、代码、`RoleTags`、`MainlineTag`；现价、涨幅（`priceColorClass`）、距涨停；`PrioritySummary`；`AttentionSummary`；`Conclusion` 一句结论；可提示时 `BuyAction`。可提示卡片 `border-accent/50 bg-accent/5`，不能买的卡片 `border-border opacity-80`；结论文字负责说明原因，不只靠颜色。
- 买入按钮口径与表格完全一致：文字“模拟买入”，`disabled = !canOrder || !row.event_id || !row.suggested_amount`，点击调用 `onBuy(row.event_id, row.suggested_amount)`；建议金额文字 `{money(amount)} 元`，没有金额时 `需关联账户及可用额度`。两处必须是同一个组件。
- 区域标题：`临近涨停 {n} 只`（n 为入选总数），`<section aria-label="临近涨停">`；卡片根元素带 `data-imminent-card`。
- 保持 CRLF（`CandidateTable.tsx`、`FirstBoard.tsx`、`FirstBoard.test.tsx` 都是 CRLF；新文件也写成 CRLF）。
- 前端命令只能用 `pnpm exec` 或 `pnpm build`（在 `frontend/` 下），禁止 npx 或 npm。
- 不提交、不推送；用户要求提交时使用 `git commit -s`，且不带上工作区里原有的 `frontend/src/lib/queryKeys.ts` 修改和无关的未跟踪文档。

## Review Focus

1. **表格筛选后卡片仍在**：筛选清空表格时，快封板的候选仍显示在卡片里。→ Task 2 `is not affected by the table filters`
2. **刷新后卡片不换位**：距涨停的大小关系翻转时，已显示的卡片保持位置。→ Task 1 `keeps shown cards in place` 与 Task 2 `keeps the card order across refreshes`
3. **休市或行情过期**：整块不渲染，不残留旧卡片。→ Task 2 `is not rendered when the snapshot is not current`
4. **卡片与表格的按钮口径一致**：`canOrder` 为假时两处都禁用；缺 `event_id` 或金额时两处都禁用。→ Task 2 `shares the buy button rules with the table`
5. **没有候选时仍占固定高度**：卡片出现或消失不位移。→ Task 2 `reserves a fixed height and shows an empty line`
6. **距涨停缺失（null）**：排在最后，不报错。→ Task 1 `puts missing distances last`

---

### Task 1: 纯函数 `pickImminent`

**Files:**
- Create: `frontend/src/custom/first-board/pickImminent.ts`
- Create: `frontend/src/custom/first-board/pickImminent.test.ts`

**Interfaces:**
- Produces: `export const MAX_IMMINENT_CARDS = 3`
- Produces: `export function pickImminent(rows: FirstBoardCandidate[], previous: string[], max = MAX_IMMINENT_CARDS): { cards: FirstBoardCandidate[]; keys: string[]; total: number }`
  - `total` = 入选总数；`cards` 为要显示的卡片（长度 ≤ `max`），`keys` 为其键 `${symbol}-${pattern}`；返回的卡片对象是 `rows` 里的最新对象。
  - 选法按 Global Constraints 的“位置稳定”和“排序”。

- [ ] **Step 1: 写失败测试 `pickImminent.test.ts`**（行用最小对象 `{ symbol, pattern: 'platform', state, can_buy, distance_to_limit_pct } as FirstBoardCandidate`）

```ts
it('picks only approaching rows, can-buy first and then the nearest to the limit', () => {
  // 行：A approaching 距 .02 can_buy false；B approaching .01 can_buy false；C sealed；D approaching .03 can_buy true
  // previous [] → cards 的 symbol 顺序 ['D','B','A']，total 3
})
it('puts missing distances last', () => {
  // E approaching null、F approaching .02（均不可买）→ ['F','E']
})
it('caps at three and reports the total', () => {
  // 5 只 approaching → cards 长度 3，total 5
})
it('keeps shown cards in place and fills free slots with the best remaining', () => {
  // previous ['A-platform','B-platform','C-platform']；此轮排序后 C 最近、其次 B、A，另有 D 排第 4
  // → cards 仍为 ['A','B','C']；
  // 再令 B 不再 approaching → cards 为 ['A','C','D']（A、C 保持原位，D 补位）
})
it('appends new cards and treats a card that disappeared and came back as new', () => {
  // previous ['A-platform']，A 消失一轮（keys 变为其他）后再出现且已有 2 张在显示 → 追加在这些卡片之后
})
```

- [ ] **Step 2: 运行，确认失败**

Run（`frontend/` 下）：`pnpm exec vitest run src/custom/first-board/pickImminent.test.ts`
Expected: FAIL，找不到 `./pickImminent`

- [ ] **Step 3: 实现 `pickImminent`**

- [ ] **Step 4: 运行，确认通过**

Run: `pnpm exec vitest run src/custom/first-board/pickImminent.test.ts`
Expected: PASS

---

### Task 2: `BuyAction` 共用组件、`ImminentCards` 与页面接入

**Files:**
- Modify: `frontend/src/custom/first-board/CandidateTable.tsx`（新增并导出 `BuyAction`；导出 `Conclusion`；表格行改用 `BuyAction`，约 298–302 行；`Conclusion` 约 189 行）
- Create: `frontend/src/custom/first-board/ImminentCards.tsx`
- Modify: `frontend/src/custom/first-board/FirstBoard.tsx`（在“昨日首板反馈”之后、`CandidateTable` 之前渲染）
- Test: `frontend/src/custom/first-board/FirstBoard.test.tsx`

**Interfaces:**
- Consumes: Task 1 的 `pickImminent`、`MAX_IMMINENT_CARDS`
- Produces:
  - `export function BuyAction({ row, canOrder, onBuy }: { row: FirstBoardCandidate; canOrder: boolean; onBuy: (eventId: string, amount: number) => void })`：渲染“模拟买入”按钮和下面的建议金额文字；表格行里用它替换原来的 `<> <button …/> <span …/> </>`，输出的 DOM 和文字保持不变。
  - `export function Conclusion(props: { row: FirstBoardCandidate; current: boolean; stockReasons: string[]; globalOnly: boolean })`：仅加 `export`。
  - `export function ImminentCards({ rows, current, globalReasons, canOrder, onOpen, onBuy }: { rows: FirstBoardCandidate[] | undefined; current: boolean; globalReasons: string[]; canOrder: boolean; onOpen: (symbol: string, name: string) => void; onBuy: (eventId: string, amount: number) => void }): JSX.Element | null`
    - 内部用 `useRef<string[]>` 保存上次显示的键，在 `useMemo(() => pickImminent(rows ?? [], ref.current), [rows, current])` 里调用并更新；`current` 为假时清空记录并返回 null。
    - `stockReasons` 与 `globalOnly` 的算法同 `CandidateRow`：`stockReasons = row.blocked_reasons.filter(r => !globalSet.has(r))`，`globalOnly = row.blocked_reasons.some(r => globalSet.has(r))`。
- `FirstBoard.tsx` 传参：`rows={data?.rows}`、`current={attentionCurrent}`、`globalReasons={data?.global_blocked_reasons ?? noReasons}`、`canOrder={liveUsable && !order.isPending}`（与表格同值）、`onOpen={openStock}`、`onBuy` 与表格同一个函数。

- [ ] **Step 1: 写失败测试（`FirstBoard.test.tsx` 末尾追加）**

助手：`const cards = () => [...host.querySelectorAll('section[aria-label="临近涨停"] [data-imminent-card]')]`，`const cardNames = () => cards().map(card => card.querySelector('button')!.firstChild!.textContent)`。行用 `candidate(name, { state, can_buy, distance_to_limit_pct, ... })`。

```tsx
it('shows only approaching candidates, can-buy first, capped at three', async () => {
  // A approaching .02 不可买；B approaching .01 不可买；C sealed；D approaching .03 可买；E approaching .005 不可买；F approaching .04 不可买
  // → cardNames ['D','E','B']；区域标题含 '临近涨停 5 只' 和 '另有 2 只，见下表'
})
it('is not affected by the table filters', async () => {
  // 一只 approaching 的候选，勾选 '只看主线'（该候选无主线）→ 表格为空，卡片仍在
})
it('keeps the card order across refreshes', async () => {
  // 初始 A .01、B .02 → ['A','B']；刷新为 A .03、B .01（B 更近）→ 仍为 ['A','B']；A 变为 sealed → ['B']
})
it('is not rendered when the snapshot is not current', async () => {
  // status 'closed' 和 status 'stale' 两种 → 页面没有 section[aria-label="临近涨停"]
})
it('reserves a fixed height and shows an empty line', async () => {
  // 只有 sealed 候选 → section 存在，含 '当前没有临近涨停的候选'，且内容区 class 含 'min-h-48'
  // 有卡片时同一个内容区 class 仍含 'min-h-48'
})
it('shares the buy button rules with the table', async () => {
  // 可提示候选（event_id、suggested_amount 齐全）：点卡片里的 '模拟买入' → firstBoardPaperOrder 以 { event_id, side: 'buy', amount: 25000 } 被调用一次
  // capabilityState.available = false（canOrder 为假）→ 卡片和表格里的 '模拟买入' 都 disabled
  // 缺 event_id → 两处都 disabled
})
it('explains a blocked card in one line and offers no buy button', async () => {
  // 不可买候选 blocked_reasons ['行业同行未跟涨'] → 卡片文字含该原因，卡片里没有 '模拟买入' 按钮
})
```

- [ ] **Step 2: 运行，确认失败**

Run: `pnpm exec vitest run src/custom/first-board/FirstBoard.test.tsx`
Expected: 新增用例 FAIL，其余 PASS

- [ ] **Step 3: 实现 `BuyAction`、导出 `Conclusion`、新建 `ImminentCards.tsx`，并在 `FirstBoard.tsx` 接入**

- [ ] **Step 4: 运行首板前端全部测试并构建**

Run: `pnpm exec vitest run src/custom/first-board` 然后 `pnpm build`
Expected: 全部 PASS，构建成功。许多已有用例用 `button('模拟买入')` 取第一个匹配的按钮，卡片出现后第一个会是卡片里的按钮；两处口径相同，预期这些用例继续通过。若有用例因“页面上多了一份名称或按钮”而失败，逐个判断并把查询限定到表格（`candidateRows()`）或卡片区，不得删除断言；每处改动在汇报里列出。

---

### Task 3: 验证与汇报

- [ ] **Step 1**：`pnpm exec vitest run`（整个前端套件）应全部通过；`git diff --check`（CRLF 引起的“尾随空白”提示属于仓库现状，忽略）。
- [ ] **Step 2: 浏览器检查**：休市时快照无效，页面不应出现“临近涨停”区块，表格与之前一致；截图。盘中的卡片外观与刷新行为无法在休市时看到。
- [ ] **Step 3: 汇报**：说明改了什么、已有测试中改动了哪些断言（逐条列出）、验证了什么；明确写出盘中表现和卡片的实际高度未实测；不提交。
