# 首板候选表合并价格列并补上成交额、流通市值 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 候选表把“现价”和“涨幅”合成一列（保留各自的排序按钮），新增“成交额 / 流通市值”一列（两个排序按钮），列数由 11 变 10；后端每行新增只作展示的 `float_cap`。

**Architecture:** 后端在行组装处新增 `float_cap = 现价 × 流通股本`；前端把“排序用的列配置”和“表头显示的列配置”分开：排序仍按单个字段（`price`、`change_pct`、`amount`、`float_cap`…），表头按显示列渲染，一个显示列里可以有多个排序按钮。

**Tech Stack:** Python 3.11、pytest；React 18、TypeScript、vitest。

**Spec:** `docs/superpowers/specs/2026-11-21-first-board-size-columns-design.md`

## Global Constraints

- 后端 `float_cap`：元，`现价 × 流通股本`；价格或流通股本缺失、非有限数或非正时为 `null`；只作展示，不得影响 `can_buy`、阻断原因、建议金额、事件和去重。流通股本取 `FirstBoardService._float_shares`（龙头、中军判定在用的同一份，单位股）。
- 成交额沿用 `row.evidence.amount`（元），不新增后端字段；前端显示用 `fmtBigNum`，缺失（非有限数）显示“—”，不显示 0。
- 显示列（共 11 列，顺序固定）：`股票 / 行业`、`形态`、`状态`、`现价 / 涨幅`、`距涨停`、`成交额 / 流通市值`、`首板优先分`、`板块联动`、`结论`、`模拟操作`、`依据`。现有 11 列把“现价”“涨幅”合成一列（−1），再新增“成交额 / 流通市值”（+1），所以**总列数不变，仍是 11 列**。证据展开行等使用总列数的地方（`colSpan`）用显示列数。
- 合并单元格：`现价 / 涨幅` 单元格是一个 `<td>`，里面两个块：第一块现价（`fmtPrice`，着色沿用 `priceColorClass(row.price ? row.change_pct : null)`），第二块涨幅（`fmtPct`，`priceColorClass(row.change_pct)`）。价格缺失时现价块显示“—”且中性色，涨幅块仍按涨幅着色（沿用已有行为）。
- 合并单元格：`成交额 / 流通市值` 单元格同样两个块：上成交额（`title="成交额"`），下流通市值（`title="流通市值"`，次要文字色 `text-secondary`）。
- 排序：保留现有排序键 `price`、`change_pct`、`distance_to_limit_pct`、`priority_score`、`attention_context`、`pattern`、`state`；新增排序键 `amount`（取 `row.evidence.amount`）和 `float_cap`（取 `row.float_cap`），缺失值排在最后（沿用 `useTableSort` 规则）。表头排序按钮的可访问名称：`按现价排序`、`按涨幅排序`、`按成交额排序`、`按流通市值排序`，按钮文字分别为“现价”“涨幅”“成交额”“流通市值”。
- 合并列的 `<th>`：`aria-sort` 取该列所含排序键里当前生效的那一个的方向（都不生效为 `none`）；表头不再另写“现价 / 涨幅”的整体标题，只放这两个按钮并用 `/` 分隔。
- 保持 CRLF（`CandidateTable.tsx`、`api.ts`、`first_board.py`、`test_first_board_service.py`、`FirstBoard.test.tsx` 都是 CRLF）。
- 后端测试命令：在 `backend/` 下用 `D:/stockSpace/tick-stock-panel/backend/.venv/Scripts/python.exe -m pytest ...`；前端命令只能用 `pnpm exec` 或 `pnpm build`（在 `frontend/` 下）。
- 开发环境：动 `backend/` 之前先查 3018 端口服务是否由带 `--reload` 的 dev.ps1 启动（`Get-CimInstance Win32_Process` 看命令行）；是的话在旁边的 worktree 里开发，最后一次性拷回；不是（只有预览服务）则可直接在主仓库改。无论哪种，改完告诉用户可能需要重启后端。
- 不提交、不推送；用户要求提交时使用 `git commit -s`，且不带上工作区里原有的 `frontend/src/lib/queryKeys.ts` 修改和无关的未跟踪文档。

## Review Focus

1. **流通股本缺失或价格缺失**：`float_cap` 为 `null`，界面显示“—”，不是 0。→ Task 1 `float_cap is null without shares or price`；Task 2 `shows placeholders for missing amount and market cap`
2. **合并后丢失排序能力**：四个排序按钮都在，点击按各自字段排序，`aria-sort` 反映生效的那个。→ Task 2 `keeps four independent sort buttons`
3. **价格缺失但涨幅为正**：现价块中性色、涨幅块仍着色（已有行为不变）。→ Task 2 改写的两个已有着色用例
4. **证据展开行的跨列**：展开依据后展开行仍横跨整行。→ Task 2 已有展开用例继续通过（`colSpan` 随显示列数）
5. **`float_cap` 不影响买入**：加入前后 `can_buy`、阻断原因、建议金额一致。→ Task 1 `float_cap never changes buying`

---

### Task 1: 后端新增 `float_cap`

**Files:**
- Modify: `backend/app/services/first_board.py`（行组装处，约 960–965 行：`row.update(quote_time=…, sector=…, event_id=None, …)` 之后）
- Test: `backend/tests/test_first_board_service.py`
- Modify: `frontend/src/lib/api.ts`（`FirstBoardCandidate` 增加 `float_cap?: number | null`，注释“流通市值（元）= 现价 × 流通股本，缺失为 null；旧后端缺省”）

**Interfaces:**
- Produces: 快照每行的 `float_cap: float | None`。

- [ ] **Step 1: 写失败测试（沿用 `service`、`quotes`、`SYMBOL`、`NOW` 夹具；通过 `service._float_shares` 注入流通股本）**

```python
def test_float_cap_is_price_times_float_shares(service):
    service._float_shares = {SYMBOL: 1_000_000_000.0}
    service.process_snapshot(quotes(), NOW.date(), now=NOW)
    row = service.snapshot()["rows"][0]
    assert row["float_cap"] == pytest.approx(10.8 * 1_000_000_000.0)

def test_float_cap_is_null_without_shares_or_price(service):
    service._float_shares = {}
    service.process_snapshot(quotes(), NOW.date(), now=NOW)
    assert service.snapshot()["rows"][0]["float_cap"] is None
    service._float_shares = {SYMBOL: 1_000_000_000.0}
    service.process_snapshot(quotes(raw_close=None, close=None), NOW.date(), now=NOW)
    assert service.snapshot()["rows"][0]["float_cap"] is None
    service._float_shares = {SYMBOL: -5.0}
    service.process_snapshot(quotes(), NOW.date(), now=NOW)
    assert service.snapshot()["rows"][0]["float_cap"] is None

def test_float_cap_never_changes_buying(service):
    service._float_shares = {}
    service.process_snapshot(quotes(), NOW.date(), now=NOW)
    without = service.snapshot()["rows"][0]
    service._float_shares = {SYMBOL: 1_000_000_000.0}
    service.process_snapshot(quotes(), NOW.date(), now=NOW)
    with_cap = service.snapshot()["rows"][0]
    for key in ("can_buy", "blocked_reasons", "suggested_amount", "event_id"):
        assert with_cap[key] == without[key]
```

（`process_snapshot` 里 `_prepare` 已被夹具替换，注入的 `_float_shares` 会保留；若 `_prepare` 之后被重置，改为在夹具的 `prepare` 里读取一个由测试设置的变量。）

- [ ] **Step 2: 运行，确认失败**

Run（`backend/` 下）：`D:/stockSpace/tick-stock-panel/backend/.venv/Scripts/python.exe -m pytest tests/test_first_board_service.py -k float_cap -q`
Expected: FAIL（`KeyError: 'float_cap'`）

- [ ] **Step 3: 在 `first_board.py` 的行循环里设置 `row["float_cap"]`**

用 `math.isfinite` 校验价格和流通股本均为有限正数，否则 `None`；写在 `row.update(...)` 之后、任何判定之前，保持单独一行逻辑，不参与后续判断。

- [ ] **Step 4: 运行首板相关全部后端测试与 ruff**

Run: `D:/stockSpace/tick-stock-panel/backend/.venv/Scripts/python.exe -m pytest tests -k first_board -q`
Expected: 全部 PASS
Run: `…/python.exe -m ruff check app/services/first_board.py tests/test_first_board_service.py`
Expected: 只剩改动前就存在的 I001、BLE001

---

### Task 2: 前端列合并、新列与排序

**Files:**
- Modify: `frontend/src/custom/first-board/CandidateTable.tsx`（`candidateColumns`/`sortableColumns` 约 15–19 行；`candidateSortValue` 约 34–44 行；`CandidateHeader` 约 188–201 行；`CandidateRow` 里价格与涨幅两个 `<td>` 约 337–338 行；所有使用 `candidateColumns.length` 的 `colSpan`）
- Test: `frontend/src/custom/first-board/FirstBoard.test.tsx`

**Interfaces:**
- Consumes: Task 1 的 `float_cap`（`FirstBoardCandidate['float_cap']`）
- 实现要点：
  - `candidateColumns` 继续作为“排序用的列配置”（`sortRows(selected, candidateColumns)` 按排序键找列），在 `distance_to_limit_pct` 之后新增 `['amount', '成交额']`、`['float_cap', '流通市值']`，并加入 `sortableColumns`。
  - 新增 `displayColumns: { id: string; label: string; sorts: string[] }[]`（11 项，顺序见 Global Constraints）：`price_change`（label `现价 / 涨幅`，sorts `['price','change_pct']`）、`size`（label `成交额 / 流通市值`，sorts `['amount','float_cap']`）；其余列 `sorts` 为各自的排序键（不可排序的为空数组，如 `symbol`、`conclusion`、`actions`、`details`）。表头和 `colSpan` 都用 `displayColumns`。
  - `candidateSortValue`：`amount` 取 `row.evidence.amount`（有限数否则 `null`）；`float_cap` 沿用通用的数值字段分支。
  - 表头里每个排序键一个按钮（文字用 `candidateColumns` 里该键的 label，两个按钮之间用 `/` 文本分隔），按钮属性沿用现有（`aria-label=按{label}排序`、`title`、激活样式、`toggleSort(key)`）；`<th>` 的 `aria-sort` 按 Global Constraints。
  - `CandidateRow`：把价格与涨幅两个 `<td>` 换成一个合并 `<td>`（见 Global Constraints），并在“距涨停”之后新增 `成交额 / 流通市值` 的 `<td>`。

- [ ] **Step 1: 改写已有用例并新增用例（`FirstBoard.test.tsx`）**

改写（合并后价格和涨幅在同一个单元格 `cells[3]` 的两个子块里，`cells[4]` 变为距涨停）：

- `colors candidate price and change consistently for change %s`：`const [priceBlock, changeBlock] = cells[3].children`；断言 `priceBlock`、`changeBlock` 都带 `color` 的 class，`changeBlock.textContent` 等于 `formatted`；`cells[0]` 的两条不含“创”“科”保持。
- `keeps a missing price neutral even when change is positive`：`priceBlock.textContent` 为 `—` 且含 `text-muted`；`changeBlock` 含 `text-bull`。
- 其余使用 `children[2]`（状态单元格）的断言不受影响。

新增：

```tsx
it('keeps four independent sort buttons in the merged headers', async () => {
  // 价格 3 行：价格 30/20/10、涨幅 .05/.09/.07、成交额 3e8/1e8/2e8（evidence.amount）、float_cap 5e9/9e9/null
  // 表头中存在按钮 aria-label 为 '按现价排序'、'按涨幅排序'、'按成交额排序'、'按流通市值排序'
  // 点 '按成交额排序' 一次 → 升序（1e8,2e8,3e8 对应的名称顺序）；其 th 的 aria-sort 为 'ascending'
  // 点 '按流通市值排序' → 升序且缺失值排最后；点 '按现价排序' 仍可用
  // 同一个 th 里的两个按钮：生效的是哪个，th 的 aria-sort 就反映哪个的方向
})
it('shows the merged price and change cell and the size cell', async () => {
  // 一行：price 18、change_pct .2、evidence.amount 123_000_000、float_cap 3_560_000_000
  // 合并单元格第一块 '18.00'、第二块 '+20.00%'；size 单元格第一块 '1.23亿'（title '成交额'）、第二块 '35.60亿'（title '流通市值'）
})
it('shows placeholders for missing amount and market cap', async () => {
  // evidence 里没有 amount、float_cap 为 null（以及 float_cap 缺省的旧后端）→ 两块都是 '—'，不含 '0'
})
it('expands the evidence row across all display columns', async () => {
  // 展开依据后 tr[data-evidence-row] 的单元格 colSpan 等于表头 th 的个数（11）
})
```

- [ ] **Step 2: 运行，确认失败**

Run（`frontend/` 下）：`pnpm exec vitest run src/custom/first-board/FirstBoard.test.tsx`
Expected: 新增和改写的用例 FAIL，其余 PASS

- [ ] **Step 3: 按 Interfaces 实现列配置、表头、单元格和排序值**

- [ ] **Step 4: 运行首板前端全部测试并构建**

Run: `pnpm exec vitest run src/custom/first-board` 然后 `pnpm build`
Expected: 全部 PASS，构建成功。已有排序用例（`sortBy('现价')`、`sortButton('涨幅')` 等）应继续通过；若有用例因“现价和涨幅现在在同一个 th”而对 `aria-sort` 的断言失败，按 Global Constraints 的 `aria-sort` 规则调整断言并在汇报里逐条列出，不得删除断言。

---

### Task 3: 验证与汇报

- [ ] **Step 1**：整个前端套件 `pnpm exec vitest run` 全部通过；后端 `pytest tests -k first_board` 全部通过；`git diff --check`（CRLF 引起的“尾随空白”提示属于仓库现状，忽略）。
- [ ] **Step 2: 重启后端并联调**：`/api/first-board/snapshot` 的每行应有 `float_cap`（休市时有数值或 `null`）。浏览器看候选表：表头有四个排序按钮，成交额和流通市值两行显示；截图；检查窄屏下横向滚动是否正常。
- [ ] **Step 3: 汇报**：说明改了什么、已有测试中改了哪些断言（逐条列出）、验证了什么；说明流通市值取决于数据源是否有流通股本；不提交。
