# 首板页按时段调整默认布局 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 进入首板页时按北京时间给出默认布局：竞价前自动展开昨日首板的竞价明细并按竞价涨幅排序，收盘后默认打开“信号记录”；不覆盖用户的明确选择。

**Architecture:** 新增纯函数 `sceneAt` 判断时段；`FirstBoard` 在挂载时判断一次，用它决定默认标签，并把“竞价前”传给 `YesterdayFeedback`；`YesterdayFeedback` 据此设置展开、明细和排序的初始值，并且竞价前窗口内不读写保存的展开偏好。纯前端。

**Tech Stack:** React 18、TypeScript、vitest（jsdom）、`Intl.DateTimeFormat`。

**Spec:** `docs/superpowers/specs/2026-11-21-first-board-scene-layout-design.md`

## Global Constraints

- 时段按北京时间（`Asia/Shanghai`）、与浏览器所在时区无关：**竞价前** = 周一至周五 09:15 ≤ 时间 < 09:30；**收盘后** = 周一至周五 时间 ≥ 15:00；其余（周末、09:15 前、09:30–15:00）为**普通**，行为与现在完全一致。
- 时段只在页面挂载时判断一次，页面打开期间不因时间流逝而改变。
- 地址里已有有效的 `tab` 参数时一律按地址走；只有没有 `tab` 参数且为收盘后，默认标签才是 `events`（“信号记录”），否则仍为 `live`。
- 竞价前：`YesterdayFeedback` 的 `open`、`expanded` 初始为 true，`sortKey` 初始为 `auction_pct`（没有竞价数据时沿用现有回退到 `change`）；该窗口内“展开分布／收起分布”只改本次状态，**不写** `storage.firstBoardFeedbackExpanded`，初始值也不读它。窗口之外行为与现在完全一致。
- 用户手动选的个股排序始终优先；不新增设置项；不判断节假日；不改后端。
- 保持 CRLF（`FirstBoard.tsx`、`YesterdayFeedback.tsx`、`FirstBoard.test.tsx`、`YesterdayFeedback.test.tsx` 都是 CRLF；新文件也写成 CRLF）。
- 前端命令只能用 `pnpm exec` 或 `pnpm build`（在 `frontend/` 下），禁止 npx 或 npm。
- 不提交、不推送；用户要求提交时使用 `git commit -s`，且不带上工作区里原有的 `frontend/src/lib/queryKeys.ts` 修改和无关的未跟踪文档。

## Review Focus

1. **周末与边界时刻**：周六日 09:20、周一 09:14、09:30、14:59、15:00 的判断。→ Task 1 `classifies the boundaries and weekends`
2. **浏览器时区不是北京时间**：用 UTC 时间戳构造北京时间，结果与运行环境时区无关。→ Task 1 `ignores the machine time zone`
3. **竞价前不改写用户保存的偏好**：收起后 `localStorage` 里的展开偏好保持不变；窗口外照旧写入。→ Task 2 `does not read or write the saved preference in the pre-open window`
4. **地址里的 `tab=live` 在收盘后不被覆盖**。→ Task 3 `keeps an explicit tab after the close`
5. **没有竞价数据时**：竞价前仍展开，但排序回退到“涨幅”，不报错。→ Task 2 `falls back to change without auction data`
6. **窗口外与现在完全一致**：普通时段默认折叠、默认标签 live。→ Task 2 `keeps the saved preference outside the pre-open window` 与 Task 3 `defaults to live outside the after-close scene`

---

### Task 1: 时段纯函数 `sceneAt`

**Files:**
- Create: `frontend/src/custom/first-board/scene.ts`
- Create: `frontend/src/custom/first-board/scene.test.ts`

**Interfaces:**
- Produces: `export type Scene = 'preopen' | 'afterClose' | 'normal'`
- Produces: `export function sceneAt(now: number = Date.now()): Scene`（`now` 为毫秒时间戳）；用 `Intl.DateTimeFormat('en-US', { timeZone: 'Asia/Shanghai', weekday: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })` 取星期、时、分。

- [ ] **Step 1: 写失败测试 `scene.test.ts`**（北京时间用 `Date.UTC(年, 月-1, 日, 时-8, 分)` 构造；2026-11-23 是周一，2026-11-21 是周六）

```ts
const beijing = (day: number, hour: number, minute = 0) => Date.UTC(2026, 10, day, hour - 8, minute)

it('classifies the boundaries and weekends', () => {
  // 周一：09:14 → normal；09:15 → preopen；09:29 → preopen；09:30 → normal；14:59 → normal；15:00 → afterClose；23:59 → afterClose
  // 周六 09:20 → normal；周六 16:00 → normal
})
it('ignores the machine time zone', () => {
  // 同一个 UTC 时间戳 beijing(23, 9, 20) 在 process.env.TZ 为 'America/Los_Angeles' 与 'Asia/Tokyo' 下都得到 'preopen'
  // （在测试里用 vi.stubEnv('TZ', …) 无效时，可只断言 sceneAt 的结果不依赖 new Date().getHours()：构造一个本地小时不同于北京小时的时间戳，断言结果按北京时间判断）
})
it('defaults to the current time', () => {
  // vi.useFakeTimers(); vi.setSystemTime(beijing(23, 15, 5)) → sceneAt() 为 'afterClose'；用完 vi.useRealTimers()
})
```

- [ ] **Step 2: 运行，确认失败**

Run（`frontend/` 下）：`pnpm exec vitest run src/custom/first-board/scene.test.ts`
Expected: FAIL，找不到 `./scene`

- [ ] **Step 3: 实现 `sceneAt`**（文件开头一段注释写明用途和时段定义）

- [ ] **Step 4: 运行，确认通过**

Run: `pnpm exec vitest run src/custom/first-board/scene.test.ts`
Expected: PASS

---

### Task 2: `YesterdayFeedback` 的竞价前默认

**Files:**
- Modify: `frontend/src/custom/first-board/YesterdayFeedback.tsx`（props 与 `useState` 初始值，约 103–114 行；`toggleOpen`，约 137–141 行）
- Test: `frontend/src/custom/first-board/YesterdayFeedback.test.tsx`

**Interfaces:**
- Produces: `YesterdayFeedback` 新增可选 prop `preopen?: boolean`（缺省 false）。为 true 时：
  - `open` 初始 true（不读保存的偏好）、`expanded` 初始 true、`sortKey` 初始 `'auction_pct'`；
  - `toggleOpen` 只 `setOpen`，不调用 `storage.firstBoardFeedbackExpanded.set`。
  - 为 false 时与现在完全一致。

- [ ] **Step 1: 写失败测试（`YesterdayFeedback.test.tsx` 末尾追加；`render` 助手需要增加可选参数 `preopen` 并传给组件）**

```tsx
it('opens the auction detail sorted by auction change in the pre-open window', async () => {
  // data：feedback() 加 auction: { source: 'meoz', fetched_at: … }，两只个股带 auction.pct_chg（A 的 .05、B 的 .08）且 change_pct 的大小关系相反
  // render(data, false, true) → '展开分布' 变为 '收起分布'（aria-expanded true），页面有 table[aria-label="昨日首板个股表现"]，
  // 按钮 '竞价涨幅' aria-pressed 为 true，表中第一行是 B
})
it('falls back to change without auction data', async () => {
  // data 不带 auction，render(data, false, true) → 仍展开并显示个股表；'涨幅' 按钮 aria-pressed 为 true；没有 '竞价涨幅' 按钮
})
it('does not read or write the saved preference in the pre-open window', async () => {
  // localStorage 先写 'first-board-feedback-expanded' = 'false'；render(..., preopen=true) → 仍展开（不读偏好）
  // 点 '收起分布' → 收起；localStorage 里该键仍为 'false'（不改写）
})
it('keeps the saved preference outside the pre-open window', async () => {
  // preopen 缺省：默认折叠；点 '展开分布' 后 localStorage 该键为 'true'（原有行为不变）
})
```

- [ ] **Step 2: 运行，确认失败**

Run: `pnpm exec vitest run src/custom/first-board/YesterdayFeedback.test.tsx`
Expected: 前三个新用例 FAIL；第四个与已有用例同类，PASS

- [ ] **Step 3: 实现**

- [ ] **Step 4: 运行，确认通过**

Run: `pnpm exec vitest run src/custom/first-board/YesterdayFeedback.test.tsx`
Expected: 全部 PASS

---

### Task 3: `FirstBoard` 接入时段

**Files:**
- Modify: `frontend/src/custom/first-board/FirstBoard.tsx`（第 49 行的 `tab` 推导；约 139 行的 `<YesterdayFeedback>`）
- Test: `frontend/src/custom/first-board/FirstBoard.test.tsx`

**Interfaces:**
- Consumes: Task 1 的 `sceneAt`；Task 2 的 `preopen` prop
- 实现：`const [scene] = useState(() => sceneAt())`；`tab` 为地址里的有效 `tab` 参数，否则 `scene === 'afterClose' ? 'events' : 'live'`；`<YesterdayFeedback preopen={scene === 'preopen'} …>`。

- [ ] **Step 1: 写失败测试（`FirstBoard.test.tsx`）**

测试文件顶部加时段桩，与已有的 `vi.mock('@/lib/api', …)` 并列（`vi.hoisted` 状态默认 `'normal'`，每个用例的 `beforeEach` 里复位）：

```tsx
const sceneState = vi.hoisted(() => ({ scene: 'normal' as 'normal' | 'preopen' | 'afterClose' }))
vi.mock('./scene', async importOriginal => ({ ...await importOriginal<typeof import('./scene')>(), sceneAt: () => sceneState.scene }))
```

再增加助手 `renderAt(path: string)`（同 `render`，但 `initialEntries` 用传入的完整路径），并新增用例：

```tsx
it('defaults to the signal record after the close when the address has no tab', async () => {
  sceneState.scene = 'afterClose'
  await renderAt('/first-board')
  // 标签 '信号记录' 的 aria-selected 为 'true'
})
it('keeps an explicit tab after the close', async () => {
  sceneState.scene = 'afterClose'
  await renderAt('/first-board?tab=live')
  // '今日盯盘' 的 aria-selected 为 'true'
})
it('defaults to live outside the after-close scene', async () => {
  for (const scene of ['normal', 'preopen'] as const) { sceneState.scene = scene; … renderAt('/first-board') → '今日盯盘' 被选中 }
})
it('opens the auction detail in the pre-open scene', async () => {
  sceneState.scene = 'preopen'
  // 快照的 yesterday_feedback 带竞价数据；renderAt('/first-board') → 反馈面板展开（按钮 '收起分布'），竞价涨幅排序按钮 aria-pressed 为 true
})
```

- [ ] **Step 2: 运行，确认失败**

Run: `pnpm exec vitest run src/custom/first-board/FirstBoard.test.tsx`
Expected: 新增用例中依赖时段的 FAIL（收盘后默认标签、竞价前展开）；其余 PASS

- [ ] **Step 3: 实现**

- [ ] **Step 4: 运行首板前端全部测试并构建**

Run: `pnpm exec vitest run src/custom/first-board` 然后 `pnpm build`
Expected: 全部 PASS，构建成功。已有用例都用 `?tab=…` 且时段桩默认 `normal`，不应受影响；若有失败逐个判断，不得删除断言。

---

### Task 4: 验证与汇报

- [ ] **Step 1**：`pnpm exec vitest run`（整个前端套件）全部通过；`git diff --check`（CRLF 引起的“尾随空白”提示属于仓库现状，忽略）。
- [ ] **Step 2: 浏览器检查**：当前时间若在“普通”时段，页面应与之前完全一致（默认今日盯盘、昨日首板反馈折叠）；若恰好在收盘后的工作日，应默认打开“信号记录”。截图。
- [ ] **Step 3: 汇报**：说明改了什么、验证了什么；明确写出竞价前窗口的实际表现和收盘后默认标签只靠测试验证（未在对应时段实测）；不提交。
