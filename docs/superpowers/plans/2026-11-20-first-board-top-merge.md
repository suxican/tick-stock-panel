# 首板页顶部合并 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把首板页顶部合成一块：温度与时段、关键数字、昨日主线、必要告警常驻显示；市场环境、情绪阶段、今日梯队、日期行和数据覆盖收进“数据与环境”折叠区；“昨日首板反馈”折叠态不再重复显示 KPI。

**Architecture:** 纯前端，就地精简（spec 方案 A）。新增 `KeyNumbers` 组件显示 5 个关键数字；`TemperatureBadge` 收窄为只显示徽章和时段；`LiveStatus` 重排为“结论行 / 数字行 / 主线行 / 告警与状态行 / 折叠区”；`YesterdayFeedback` 删除折叠态的 KPI 行和注释行。折叠区用 `hidden` 属性隐藏但保留挂载，现有按文字断言的测试大多不用改。

**Tech Stack:** React 18、TypeScript、Tailwind、vitest（jsdom）。

**Spec:** `docs/superpowers/specs/2026-11-20-first-board-top-merge-design.md`

## Global Constraints

- 纯前端改动：不改后端，不改 `api.ts` 的类型，不改候选表、筛选和任何买入逻辑。
- 文案沿用：“昨日首板反馈”“展开分布／收起分布”“查看明细”、范围按钮“全部／主板／双创”、标签“市场环境：…”“情绪阶段（昨日）：…”“盘中梯队／今日梯队：…”“昨日主线：…”保持原样。
- 缺失值显示“—”，不显示 0；比例为小数制，显示时 ×100。
- 颜色：昨日首板均涨按红涨绿跌（`priceColorClass`），反馈过期或刷新失败时改用中性色（与 `YesterdayFeedback` 的 muted 规则一致）；告警用琥珀色（`border-amber-500/40`），并配图标和文字，不能只靠颜色。
- 折叠区默认收起，不记忆展开状态；使用 `aria-expanded` 和 `aria-controls`，隐藏用 `hidden` 属性、保留挂载。
- 修改已有文件时保持 CRLF（这些文件都是 CRLF）；新文件也写成 CRLF。
- 前端命令只能用 `pnpm exec` 或 `pnpm build`（在 `frontend/` 下），禁止用 npx 或 npm。
- 不提交、不推送；用户要求提交时使用 `git commit -s`，且不要带上工作区里原有的 `frontend/src/lib/queryKeys.ts` 修改。
- **Ruling（对 spec 第 3 节状态行的调整）**：spec 写的是收盘时显示“已收盘，显示 {交易日} 收盘数据”。但后端的 closed 状态也覆盖午休和盘前，此时写“已收盘”是错的；而交易日和“收盘”二字温度行已经显示了。所以 closed 时只显示后端 `message` 这一句，不再列出 `global_blocked_reasons`。代价：文案没有 spec 示例那么短。

## Review Focus

1. **市场环境不允许提示**（`environment.allowed === false`）：折叠区外必须有一条可见的琥珀色告警，写明环境名和“暂不允许提示”。→ Task 2 `shows the environment block outside the fold`
2. **实时行情受阻**（`data_status.realtime_status === 'blocked'`）：告警和“检查实时行情数据源”链接在折叠区外可见，收盘状态下也一样。→ Task 2 `keeps the realtime block visible when closed`
3. **旧后端快照**（没有 `market_ladder`、`temperature`、`mainlines`、`data_status`）：数字行显示“—”，没有主线行和徽章，页面不报错。→ Task 1 `shows placeholders for an older backend` 与 Task 2 `hides sentiment and mainline chips for snapshots from an older backend`（已有，继续通过）
4. **反馈过期或刷新失败**：昨日首板均涨不显示红绿。→ Task 1 `neutralizes the feedback mean when muted`
5. **收盘状态**：只显示一句后端 message，三条全局原因不出现在页面任何位置；但候选行的“受全局条件暂停”仍然出现。→ Task 2 `shows a single line when closed`

---

### Task 1: `KeyNumbers` 组件，收窄 `TemperatureBadge`

**Files:**
- Create: `frontend/src/custom/first-board/KeyNumbers.tsx`
- Create: `frontend/src/custom/first-board/KeyNumbers.test.tsx`
- Modify: `frontend/src/custom/first-board/TemperatureBadge.tsx`（删除封板率和均涨两项，只保留徽章和时段）
- Modify: `frontend/src/custom/first-board/TemperatureBadge.test.tsx`（删除对“封板率”“昨日首板均涨”的断言，其余断言保留）

**Interfaces:**
- Produces: `export function KeyNumbers({ ladder, feedback, muted }: { ladder?: FirstBoardSnapshot['market_ladder']; feedback?: FirstBoardFeedback; muted: boolean }): JSX.Element`
  - 根元素为 `<dl aria-label="关键数字">`，每项一对 `dt` 和 `dd`，按以下顺序：
    | dt | dd |
    |---|---|
    | 封板率 | `Math.round(ladder.seal_rate*100)%` |
    | 昨日首板均涨 | `fmtPct(feedback.groups.all.mean_change_pct)` |
    | 晋级二板 | `${limit_up_count} / ${sample_count}`（取自 `groups.all`） |
    | 涨停:跌停 | `${limit_up}:${limit_down}` |
    | 高度 | `${max_consecutive} 板` |
  - 来源缺失时 dd 为“—”。“晋级二板”在 `sample_count` 为 0 或没有反馈时为“—”；“涨停:跌停”任一侧为 null 时为“—”。
  - “昨日首板均涨”的 dd：`muted` 为 false 时使用 `priceColorClass(value)`，否则使用 `text-foreground`。

- [ ] **Step 1: 写失败测试 `KeyNumbers.test.tsx`**（用 `renderToStaticMarkup`，写法同 `TemperatureBadge.test.tsx`）

```tsx
// value(host, label) = 找到 textContent === label 的 dt，取其 nextElementSibling
const ladder = { as_of: '2026-11-20', observed_at: '2026-11-20T15:00:00+08:00', live: false, sealed_corrected: true,
  limit_up: 30, broken: 14, limit_down: 61, max_consecutive: 4, first_board: 26, ge2_count: 4, seal_rate: .6818, promo_rate: .14, promo_pool: 28 }
// feedback: groups.all = { sample_count: 19, limit_up_count: 1, mean_change_pct: -.0448, ... 其余字段照 FirstBoardFeedbackSummary 填 }

it('shows the five numbers in order', () => {
  // dt 依次为 ['封板率','昨日首板均涨','晋级二板','涨停:跌停','高度']
  // dd 依次为 ['68%','-4.48%','1 / 19','30:61','4 板']
  // '昨日首板均涨' 的 dd class 含 'text-bear'
})
it('neutralizes the feedback mean when muted', () => {
  // muted=true → '昨日首板均涨' 的 dd class 不含 'text-bear'，含 'text-foreground'
})
it('shows placeholders for an older backend', () => {
  // ladder=undefined, feedback=undefined → 五个 dd 都是 '—'
  // ladder 的 limit_down=null → '涨停:跌停' 为 '—'；seal_rate=null → '封板率' 为 '—'
})
```

- [ ] **Step 2: 运行，确认失败**

Run: `pnpm exec vitest run src/custom/first-board/KeyNumbers.test.tsx`
Expected: FAIL，提示找不到 `./KeyNumbers`

- [ ] **Step 3: 实现 `KeyNumbers.tsx`；同时把 `TemperatureBadge.tsx` 收窄为“徽章 + 时段”，并同步删掉其测试里的两条数字断言**

`TemperatureBadge` 的 `title`、颜色、版本判断、`data-first-board-temperature` 属性都不变。

- [ ] **Step 4: 运行，确认通过**

Run: `pnpm exec vitest run src/custom/first-board/KeyNumbers.test.tsx src/custom/first-board/TemperatureBadge.test.tsx`
Expected: PASS

---

### Task 2: 重排 `LiveStatus`

**Files:**
- Modify: `frontend/src/custom/first-board/LiveStatus.tsx`
- Modify: `frontend/src/custom/first-board/FirstBoard.tsx:137`（向 `LiveStatus` 传 `muted`，取值为 `snapshot.isError || refresh.isError || data.yesterday_feedback?.status === 'stale'`）
- Test: `frontend/src/custom/first-board/FirstBoard.test.tsx`

**Interfaces:**
- Consumes: Task 1 的 `KeyNumbers`、收窄后的 `TemperatureBadge`
- Produces: `LiveStatus({ data, retained, muted }: { data: FirstBoardSnapshot; retained: boolean; muted: boolean })`

**`<section aria-label="盯盘状态">` 内的结构（从上到下）**
1. **结论行**：左边是 `<TemperatureBadge value={data.temperature} />`；右边是折叠开关 `<button aria-expanded aria-controls>`，文字为“数据与环境”，后接现有数据覆盖摘要（图标 + “数据覆盖 {summary.label}” + `retained` 时的“（上次摘要）”）和上下箭头。没有 `data_status` 时只显示“数据与环境”。
2. **数字行**：`<KeyNumbers ladder={data.market_ladder} feedback={data.yesterday_feedback} muted={muted} />`
3. **主线行**：仅在 `data.mainlines` 存在时显示 `<p title={原主线 chip 的 title}>昨日主线：{原取名逻辑}</p>`；取名逻辑为概念优先、取前 3 个、用“、”连接，没有条目时显示“待确认”。
4. **常驻告警**（每条都是琥珀色描边、带图标的一行）：
   - `!env.allowed`：`市场环境：{env.label || '待确认'}{日期} · 暂不允许提示`，`title={env.reason}`
   - `data_status.realtime_status === 'blocked'`：`实时行情受阻` + `realtime_reason` + `<Link to="/settings?tab=data-sources">检查实时行情数据源</Link>`
   - 盘中封单未开启：原 Link 原样搬过来
5. **状态行**：
   - status 为 `ready`：只在有全局原因时列出原因（去重，并去掉与 message 相同的项，同现状）
   - status 为 `closed`：只显示 `data.message` 一句，不列原因
   - 其他状态：显示 message 和原因列表（同现状）
6. **折叠区** `<div id hidden={!open}>`：
   - 实时行情标签（仅在不是 blocked 时显示）
   - 市场环境标签（文案和 sr-only 文字不变）
   - 情绪阶段标签
   - 今日／盘中梯队标签
   - 日期行“交易日期 … · 快照 … · 规则版本 …”
   - 现有 `<DataStatus>`
   - 所有标签的文字和悬停说明都沿用现状，只是搬了位置。

- [ ] **Step 1: 改写和新增测试（`FirstBoard.test.tsx`）**

```tsx
it('shows the environment block outside the fold', async () => {
  calls.firstBoardSnapshot.mockResolvedValue({ ...snapshot(), environment: { label: '偏弱', allowed: false, reason: '参考交易日市场环境不在允许范围' } })
  await render()
  const status = host.querySelector('section[aria-label="盯盘状态"]')!
  const alert = [...status.querySelectorAll<HTMLElement>('[data-first-board-alert]')].find(node => node.textContent?.includes('暂不允许提示'))!
  expect(alert.closest('[hidden]')).toBeNull()
  expect(alert.textContent).toContain('市场环境：偏弱')
})
it('keeps the realtime block visible when closed', async () => {
  // snapshot 的 status 为 'closed'，data_status 用 'keeps data coverage…' 用例里同样的结构，但 realtime_status: 'blocked'、realtime_reason: '全局实时行情未启用'
  // → [data-first-board-alert] 中有一条包含 '全局实时行情未启用'，不在 [hidden] 内，且含 href 为 '/settings?tab=data-sources' 的链接
})
it('shows a single line when closed', async () => {
  // status: 'closed'，message: '当前为非交易时段, 显示最近行情观察结果, 不触发买卖提示。'，global_blocked_reasons: ['当前不在有效盘中时段']，rows 中一行的 blocked_reasons 只含该全局原因
  // → 盯盘状态区 textContent 包含该 message，但 host.textContent 不包含 '当前不在有效盘中时段'；候选行包含 '受全局条件暂停'
})
it('folds environment, phase, ladder and dates until expanded', async () => {
  // 用 'shows the live ladder…' 用例里的 environment 和 market_ladder
  // → '市场环境：强势'、'情绪阶段（昨日）：修复'、'交易日期' 所在元素的 closest('[hidden]') 不为 null
  // → 点击 '数据与环境' 按钮后 aria-expanded='true'，上述元素不再位于 [hidden] 内
  // → 数字行 dl[aria-label="关键数字"] 不在 [hidden] 内
})
```

同时修改已有用例 `shows global blocking reasons once in the status gate…`：把 `status: 'closed'` 和 message 改成 `status: 'stale', message: '缺少当日新鲜行情, 买入提示已暂停。'`，其余断言不变（stale 状态仍会列出原因）。

- [ ] **Step 2: 运行，确认新用例失败**

Run: `pnpm exec vitest run src/custom/first-board/FirstBoard.test.tsx`
Expected: 四个新用例 FAIL（找不到 `[data-first-board-alert]` 或“数据与环境”按钮）；改写过的已有用例 PASS

- [ ] **Step 3: 按上面的结构改写 `LiveStatus.tsx`；在 `FirstBoard.tsx` 中传入 `muted`**

告警元素加上 `data-first-board-alert` 属性。

- [ ] **Step 4: 运行首板前端全部测试，确认通过**

Run: `pnpm exec vitest run src/custom/first-board`
Expected: 全部 PASS。若已有用例因为“元素被搬进折叠区”而失败，只能改为先点“数据与环境”再断言，不能删断言；每处改动都要在汇报中列出。

---

### Task 3: “昨日首板反馈”折叠态去掉 KPI

**Files:**
- Modify: `frontend/src/custom/first-board/YesterdayFeedback.tsx`（删掉 `!open &&` 的 KPI `<dl>` 和 notes 行；删掉随之不再使用的 `Kpi` 组件和 `opening` 变量；展开态不变）
- Test: `frontend/src/custom/first-board/YesterdayFeedback.test.tsx`、`frontend/src/custom/first-board/FirstBoard.test.tsx`

- [ ] **Step 1: 修改测试**
  - `YesterdayFeedback.test.tsx`，`collapses to key indicators…`：把收起时的 4 条 metric 断言（原第 53–56 行）改为 `expect(metric('平均溢价')).toBeUndefined()` 和 `expect(metric('当前二板')).toBeUndefined()`；用例名改成 `collapses to the header by default and remembers the expanded distribution`。
  - `keeps missing values distinct from zero…` 和 `retains values with a refresh failure message…`：在第一次 `metric(...)` 断言前插入 `await click('展开分布')`。`retains…` 里第二次 `render` 之后也要先展开（同一个 root 重新渲染会保留 open 状态的，就不用再点）。
  - `FirstBoard.test.tsx`，`keeps the market temperature collapsed by default…`：把第 929 行改为 `expect([...feedback.querySelectorAll('dt')].find(item => item.textContent === '平均溢价')).toBeUndefined()`。
  - `marks yesterday feedback as a retained snapshot…`：在 `await click('刷新')` 之后、查找 `平均溢价` 之前插入 `await click('展开分布')`。

- [ ] **Step 2: 运行，确认失败**

Run: `pnpm exec vitest run src/custom/first-board/YesterdayFeedback.test.tsx`
Expected: `collapses to the header…` FAIL，原因是收起时仍找到了“平均溢价”

- [ ] **Step 3: 删除折叠态的 KPI 行和 notes 行**

- [ ] **Step 4: 运行首板前端全部测试**

Run: `pnpm exec vitest run src/custom/first-board`
Expected: 全部 PASS

---

### Task 4: 构建与浏览器检查

- [ ] **Step 1: 构建**

Run: `pnpm build`
Expected: 成功，没有类型错误（体积告警属于已有情况）

- [ ] **Step 2: 浏览器检查（当前为休市时段）**

打开 `/first-board`。
- 第一屏应为：`首板温度 · 休息　11/20 收盘` 加右侧“数据与环境”，下一行是 `封板率 68%　昨日首板均涨 -4.48%　晋级二板 1/19　涨停:跌停 30:61　高度 4 板`，再下一行是 `昨日主线：先进封装、光刻胶、算力租赁`，最后是一句后端 message。
- 页面上看不到“当前不在有效盘中时段”。
- 展开“数据与环境”后能看到环境、阶段、梯队标签和日期行。
- “昨日首板反馈”折叠态只剩标题行。
- 再用 `resize_window` 的 mobile 尺寸检查一次换行和截断，检查完恢复为 desktop。
- 截一张图作为证据。

- [ ] **Step 3: 汇报**

用 `git status` 和 `git diff --stat` 确认只改了本计划列出的文件（原有的 `queryKeys.ts` 修改和其他未跟踪文档不动）。说明改了什么、哪些已有测试断言被改动（逐条列出）、哪些未验证（盘中时段与实时刷新）。不提交。
