# 首板页“昨日主线”降为纯展示并加风险提示 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 昨日主线在首板页只作展示：标签和徽章改成中性灰，属于主线的候选在悬停说明和依据区带上同一句风险提示，移除“只看主线”筛选，规则页开关旁加说明。

**Architecture:** 新增一个共享的风险提示常量文件，`MainlineTag`、依据区、组头、顶部主线行、规则页都引用它；样式改为中性；删除 `onlyMainline` 的状态和 UI。纯前端，不改后端。

**Tech Stack:** React 18、TypeScript、vitest（jsdom）。

**Spec:** `docs/superpowers/specs/2026-11-22-first-board-mainline-display-only-design.md`

## Global Constraints

- 风险提示句（逐字，各处共用同一个常量）：`昨日主线内的首板，历史回测次日平均表现低于主线外（2018 年起，换多种口径结论一致），仅供参考。` 不加具体数字。常量放在新文件 `frontend/src/custom/first-board/mainlineNote.ts`，导出 `MAINLINE_RISK_NOTE`。
- “主线”标签（`MainlineTag`）：样式改为中性灰描边（`border-border bg-elevated text-secondary`，保留 `ml-1 inline-flex shrink-0 items-center rounded border px-1 text-[10px] leading-4`），文字仍是“主线”；`title` 为原有主线名称说明，后面换行追加 `MAINLINE_RISK_NOTE`（`\n` 分隔）。仍只在 `row.mainline?.hit === true` 且有匹配项时渲染。
- 依据展开区：原有 `{row.mainline && <li>{row.mainline.reason}</li>}` 保留；在其后，当 `row.mainline?.hit === true` 时新增一行 `<li>`，文字 `风险提示：` + `MAINLINE_RISK_NOTE`，颜色 `text-amber-700 dark:text-amber-400`（不仅靠颜色，文字以“风险提示：”开头）。
- 移除“只看主线”：删除 `onlyMainline`/`setOnlyMainline` 状态、过滤条件、重置签名里的项、返回值、筛选 chip、`emptyMessage` 里对应的分支。`useCandidateView` 其余行为不变。
- 题材分组组头的“昨日主线第 N”徽章：样式改为 `border-border bg-elevated text-secondary`（原为 `border-accent/40 bg-accent/10 text-foreground`），文字不变；分组归属规则（`themeGroups.ts`）一行不改。
- 顶部主线行（`LiveStatus.tsx`）：文字不变；该元素 `title` 的末尾换行追加 `MAINLINE_RISK_NOTE`（原有按行列出名次的说明保留）。
- 规则页（`RulesPanel.tsx`）：开关“只提示主线内个股”保留（字段 `require_mainline` 不变）；在它所在的说明段落（`!!draft.allowed_market_phases?.length || draft.require_mainline` 时显示的那段）后追加同样的 `MAINLINE_RISK_NOTE`，或在开关下新增一段仅当 `draft.require_mainline` 为真时显示的 `text-xs text-secondary` 说明，二选一，由实现者决定，但文案必须包含该常量全文。
- 不改后端；不改 `can_buy`、阻断原因、建议金额；不新增配置。
- 保持 CRLF（`CandidateTable.tsx`、`LiveStatus.tsx`、`RulesPanel.tsx`、`FirstBoard.test.tsx` 都是 CRLF；新文件也写成 CRLF）。
- 前端命令只能用 `pnpm exec` 或 `pnpm build`（在 `frontend/` 下），禁止 npx 或 npm。
- 不提交、不推送；用户要求提交时使用 `git commit -s`，且不带上工作区里原有的 `frontend/src/lib/queryKeys.ts` 修改和无关的未跟踪文档。

## Review Focus

1. **只在命中主线时出现风险提示**：未命中、主线数据缺失（`hit` 为 null/false）的候选，标签、依据区风险行都不出现。→ Task 1 `shows the risk note only for mainline hits`
2. **“只看主线”确实没了，且不影响其他筛选**：筛选区没有该 chip；原来依赖它的用例改用别的筛选后行为不变。→ Task 1 `no longer offers the mainline filter`；已有用例改用 `只看可提示候选`
3. **风险提示不改变买入**：命中主线与否，`can_buy`、阻断原因、建议金额、买入按钮都一致。→ Task 1 `keeps buying unaffected by the mainline risk note`
4. **规则页开关仍可保存**：`require_mainline` 勾选后保存的值不变，提示句出现。→ Task 1 改写已有用例 `saves the sentiment phase and mainline gates…` 并断言提示句
5. **临近涨停卡片**：卡片里的主线标签同样是中性样式并带提示（共用组件）。→ Task 1 `shows the neutral mainline tag on imminent cards`

---

### Task 1: 实现与测试

**Files:**
- Create: `frontend/src/custom/first-board/mainlineNote.ts`
- Modify: `frontend/src/custom/first-board/CandidateTable.tsx`（`onlyMainline` 相关约 64、99、110、113、135、161–162、190 行；`MainlineTag` 约 287–291 行；依据区列表约 `row.mainline` 那一行；组头徽章约 313 行）
- Modify: `frontend/src/custom/first-board/LiveStatus.tsx`（主线行的 `title`，约 66–68 行）
- Modify: `frontend/src/custom/first-board/RulesPanel.tsx`（约 87、103 行）
- Test: `frontend/src/custom/first-board/FirstBoard.test.tsx`

**Interfaces:**
- Produces: `export const MAINLINE_RISK_NOTE: string`（见 Global Constraints，逐字）。

- [ ] **Step 1: 改写已有用例并新增用例（`FirstBoard.test.tsx`）**

先改已有用例：
- `shows the reference-day sentiment phase and mainlines, and filters candidates by mainline`（约 1165–1190 行）：改名为 `shows the reference-day sentiment phase and mainlines`，保留顶部“昨日主线：机器人”、主线标签只有 1 个、标签 `title` 以原说明开头（`toBe` 改为 `toContain('2026-09-29 主线：机器人（第 1）')`）的断言；删掉末尾 `toggleExact('只看主线')` 及其后对表格内容的断言。
- 约 1740 行 `is not affected by the table filters`（临近涨停卡片用例）：把 `toggleExact('只看主线')` 改成 `toggleExact('只看可提示候选')`（该用例的候选 `can_buy` 为 false，所以表格清空而卡片仍在）。
- 约 1255 行规则页用例：保存断言不变，另外断言页面文字含 `MAINLINE_RISK_NOTE`（从 `./mainlineNote` 导入）。

新增用例（命中主线的候选用 `mainline: { hit: true, date: '2026-09-29', reason: '', matches: [{ kind: 'concept', name: '机器人', rank: 1, limit_up_count: 9, max_boards: 4 }] }`）：

```tsx
it('shows the risk note only for mainline hits', async () => {
  // 三行：A 命中主线；B hit false；C hit null（数据缺失）；用 state 'watch' 避免卡片干扰
  // A 行：标签文字 '主线'，title 含 '2026-09-29 主线：机器人（第 1）' 与 MAINLINE_RISK_NOTE，class 含 'text-secondary' 且不含 'accent'
  // 展开依据后：A 的依据区有以 '风险提示：' 开头的行，全文含 MAINLINE_RISK_NOTE，B、C 的依据区没有 '风险提示：'
})
it('no longer offers the mainline filter', async () => {
  // 渲染后筛选区没有文字为 '只看主线' 的 label；其他 chip（'只看可提示候选'、'按题材分组'）仍在
})
it('keeps buying unaffected by the mainline risk note', async () => {
  // 两个候选完全相同，只有 mainline 不同（一个命中主线，一个 hit false），can_buy true、event_id/suggested_amount 齐全
  // 两行的 '模拟买入' 按钮都可用、建议金额文字相同、行结论文字相同
})
it('shows the neutral mainline tag on imminent cards', async () => {
  // approaching 且命中主线的候选 → 卡片里的标签 class 不含 'accent'，title 含 MAINLINE_RISK_NOTE
})
it('shows the note on the top mainline line and on the group badge', async () => {
  // 快照带 mainlines（同已有主线用例）：顶部主线行元素的 title 含 MAINLINE_RISK_NOTE
  // 开启 '按题材分组' 且候选命中主线：组头的 '昨日主线第 1' 徽章 class 不含 'accent'
})
```

- [ ] **Step 2: 运行，确认失败**

Run（`frontend/` 下）：`pnpm exec vitest run src/custom/first-board/FirstBoard.test.tsx`
Expected: 新增和改写的用例 FAIL（缺 `./mainlineNote`、仍有“只看主线”、标签仍是蓝色等），其余 PASS

- [ ] **Step 3: 按 Global Constraints 实现**（先建 `mainlineNote.ts`，再改各文件）

- [ ] **Step 4: 运行首板前端全部测试并构建**

Run: `pnpm exec vitest run src/custom/first-board` 然后 `pnpm build`
Expected: 全部 PASS，构建成功。已有用例若因“主线标签样式”或“筛选 chip 个数”而失败，逐个判断并调整，不得删除断言；每处改动在汇报里列出。

---

### Task 2: 验证与汇报

- [ ] **Step 1**：整个前端套件 `pnpm exec vitest run` 全部通过；`git diff --check`（CRLF 引起的“尾随空白”提示属于仓库现状，忽略）。
- [ ] **Step 2: 浏览器检查**：休市时候选里有“主线”标签（易天股份、华微电子、高乐股份）：应为灰色，悬停显示风险提示；展开依据应有琥珀色“风险提示：”一行；筛选区没有“只看主线”；规则页勾选“只提示主线内个股”时能看到提示句；截图。
- [ ] **Step 3: 汇报**：说明改了什么、已有测试中改动了哪些断言（逐条列出）、验证了什么；明确说明风险提示依据的是离线回测，有局限；不提交。
