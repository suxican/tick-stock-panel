# 首板温度 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 首板快照新增只读的「今日首板温度」（进攻／试错／休息／待确认），后端计算，前端在盯盘状态区最上方显示，并把当时的档位写入买入候选信号的记录。

**Architecture:** 新增纯函数模块 `first_board_temperature.py`。数据充分性检查从 `_feedback_gate` 中抽成 `first_board_feedback.feedback_problem()`，两处共用。服务里有两个调用点：`process_snapshot` 在行循环前计算一次，写入每行的 `evidence.temperature`，信号随之带上；`snapshot()` 在读取时按最终状态和过期处理后的反馈重算一次，作为顶层 `temperature`。前端新增 `TemperatureBadge` 组件，由 `LiveStatus` 放在最前面渲染。

**Tech Stack:** Python 3.11、Polars、pytest、ruff；React 18、TypeScript、vitest（jsdom）、Tailwind。

**Spec:** `docs/superpowers/specs/2026-11-20-first-board-temperature-design.md`

## Global Constraints

- 只作显示：任何改动都不得改变 `can_buy`、`blocked_reasons`、`suggested_amount`、`global_blocked_reasons`、事件类型和去重。
- 阈值固定为常量，不进入 `FirstBoardConfig`：进攻 `S ≥ 0.70 且 F ≥ 0.02`；休息 `S < 0.60`，或 `S < 0.70 且 F < 0`；其余为试错。
- 数据充分性：触板（`limit_up + broken`）≥ 10；反馈的状态为 ready 或 partial，日期等于快照交易日，`small_sample` 为假，均值、上涨占比、覆盖率均为有限数，覆盖率 ≥ 0.8（`SECTOR_MIN_QUOTE_COVERAGE`）；盘中（status=ready）还要求 `quote_mode == "live"`。
- 缺失值输出 `None`，不得填 0。比例一律用小数制。
- 用户可见文案不得出现内部枚举名（如 `stale`、`attack`）。
- 修改已有文件时保持 CRLF 行尾（工作区设置了 autocrlf=true，`first_board*.py`、`LiveStatus.tsx`、`api.ts` 和测试文件都是 CRLF）。
- 后端改动在旁边的 worktree 中进行：`git worktree add --detach ../tick-stock-panel-temperature HEAD`。测试使用主仓库的 venv：在 worktree 的 `backend/` 下运行 `D:/stockSpace/tick-stock-panel/backend/.venv/Scripts/python.exe -m pytest ...`。用户的 dev.ps1（uvicorn --reload）会在后端文件变化时整套退出，所以后端文件在 Task 5 里一次性拷回。
- 前端直接在主仓库里修改（Vite 热更新是安全的）。前端命令只能用 `pnpm exec` 或 `pnpm build`，禁止用 npx 或 npm。
- 不提交、不推送。用户要求提交时，使用 `git commit -s`，并按 CONTRIBUTING §14.5 自检。不得改动工作区已有的 `frontend/src/lib/queryKeys.ts` 修改。

## Review Focus

1. **午间休市**（11:30–13:00 时 status 为 closed，交易日是今天）：应显示上午最后一次的读数，时段为 session，不能变成待确认。→ Task 2 `test_lunch_break_keeps_session_reading`
2. **次日开盘前**（交易日为上一交易日）：应显示上一交易日的收盘档位，时段为 close。→ Task 2 `test_next_morning_shows_previous_close`
3. **页面停留不刷新，盘中反馈已过期**：读取时反馈被置为 stale，温度应变为待确认，不能保留旧档位。→ Task 3 `test_temperature_turns_unknown_when_feedback_expires`
4. **没有炸板数据**（`broken` 为 None，所以 `seal_rate` 也为 None）：应显示待确认“封板率暂不可用”，不能按 0 或 100% 判档。→ Task 2 `test_unknown_cases` 中的 `seal_rate=None` 用例
5. **旧后端没有 `temperature` 字段，或版本未知**：不显示徽章，页面其余部分照常显示。→ Task 4 `renders nothing for old or unknown payloads`

---

### Task 1: 抽出反馈充分性检查 `feedback_problem`

**Files:**
- Modify: `backend/app/services/first_board_feedback.py`（新增函数，放在 `expire_feedback` 之前）
- Modify: `backend/app/services/first_board.py:181-200`（`_feedback_gate` 改为调用新函数）
- Test: `backend/tests/test_first_board_feedback.py`（已存在，在末尾追加）

**Interfaces:**
- Produces: `feedback_problem(feedback: dict | None, as_of: str | None, *, require_live: bool) -> str | None`。数据可用时返回 None，否则返回一条中文原因。

- [ ] **Step 1: 写失败测试**

```python
import pytest
from app.services.first_board_feedback import feedback_problem

DAY = "2026-10-16"

def _feedback(**summary_overrides):
    summary = {"sample_count": 12, "coverage": 1., "mean_change_pct": .02, "up_ratio": .6, "small_sample": False}
    summary.update(summary_overrides)
    return {"status": "ready", "quote_mode": "live", "as_of": DAY, "groups": {"all": summary}}

def test_feedback_problem_accepts_measured_live_sample():
    assert feedback_problem(_feedback(), DAY, require_live=True) is None
    assert feedback_problem({**_feedback(), "status": "partial"}, DAY, require_live=True) is None
    assert feedback_problem({**_feedback(), "quote_mode": "snapshot"}, DAY, require_live=False) is None

@pytest.mark.parametrize(("feedback", "require_live", "reason"), [
    (None, False, "昨日首板反馈未就绪"),
    ({**_feedback(), "status": "stale"}, False, "昨日首板反馈未就绪"),
    ({**_feedback(), "quote_mode": "snapshot"}, True, "昨日首板反馈不是盘中实测"),
    ({**_feedback(), "as_of": "2026-10-15"}, False, "昨日首板反馈不是当日数据"),
    (_feedback(small_sample=True), False, "昨日首板样本少于 10 只"),
    (_feedback(mean_change_pct=None), False, "昨日首板反馈缺少有效统计"),
    (_feedback(coverage=.79), False, "昨日首板行情覆盖不足 80%"),
])
def test_feedback_problem_names_the_missing_piece(feedback, require_live, reason):
    assert feedback_problem(feedback, DAY, require_live=require_live) == reason
```

- [ ] **Step 2: 运行，确认失败**

Run（worktree 的 `backend/` 下）：`D:/stockSpace/tick-stock-panel/backend/.venv/Scripts/python.exe -m pytest tests/test_first_board_feedback.py -k feedback_problem -q`
Expected: FAIL，报 `ImportError: cannot import name 'feedback_problem'`

- [ ] **Step 3: 在 `first_board_feedback.py` 实现 `feedback_problem`，并让 `_feedback_gate` 调用它**

按上面参数表的顺序逐项检查，返回第一个不满足项的原因：状态 → 是否实测（仅 `require_live` 时检查）→ 日期 → `small_sample is not False` → 均值、上涨占比、覆盖率是否为有限数（排除 bool）→ 覆盖率 < `SECTOR_MIN_QUOTE_COVERAGE`（从 `first_board_context` 导入）。
`_feedback_gate` 原来那串条件改为 `if feedback_problem(feedback, str(as_of), require_live=True):`，原因文案保持原样“昨日首板反馈样本不足或行情覆盖不足, 无法确认赚钱效应”，其余逻辑不动。

- [ ] **Step 4: 运行新测试和已有门控测试，确认通过**

Run: `D:/stockSpace/tick-stock-panel/backend/.venv/Scripts/python.exe -m pytest tests/test_first_board_feedback.py tests/test_first_board_service.py -k "feedback" -q`
Expected: PASS（包括已有的 `test_feedback_gate_fails_closed_without_same_day_live_sample` 和 `test_feedback_gate_is_optional_and_needs_measured_feedback`）

---

### Task 2: 温度纯函数模块

**Files:**
- Create: `backend/app/services/first_board_temperature.py`
- Test: `backend/tests/test_first_board_temperature.py`

**Interfaces:**
- Consumes: Task 1 的 `feedback_problem`
- Produces:
  - 常量：`TEMPERATURE_VERSION = "temperature-v1"`、`MIN_TOUCHES = 10`
  - 函数：`first_board_temperature(ladder: dict | None, feedback: dict | None, *, status: str, as_of: str | None, now: datetime) -> dict`
  - 返回的键（固定，缺失为 None）：`version, tier, label, seal_rate, touch_count, feedback_mean, feedback_sample, stage, as_of, observed_at, reason`
    - `tier` ∈ `attack | probe | rest | unknown`；`label` 依次为 `进攻 | 试错 | 休息 | 待确认`
    - `stage` ∈ `early | session | close | None`；`observed_at` 取自 `ladder["observed_at"]`
    - `seal_rate` 取自 `ladder["seal_rate"]`；`touch_count = limit_up + broken`，任一缺失则为 None
    - `feedback_mean`、`feedback_sample` 分别取自 `feedback["groups"]["all"]` 的 `mean_change_pct`、`sample_count`

**判定顺序**（遇到第一个不满足项就返回 unknown；原因文案即下列引号中的原文）：
1. `status` 不是 ready 也不是 closed：waiting →“等待有效行情”，stale →“行情已过期”，error →“首板数据暂不可用”，disabled →“自动盯盘已暂停”，其他 →“暂无可用行情”。此时 stage 为 None。
2. 时段：status 为 ready 时，北京时间 < 10:00 为 early，否则为 session。status 为 closed 时，若 `as_of` 早于 `now.date()`，或 `now.time() ≥ 14:57`，为 close；否则（午间、当日盘前）为 session。
3. `ladder` 缺失，或 `ladder["as_of"] != as_of` →“缺少当日涨停梯队”
4. `seal_rate` 为 None →“封板率暂不可用（缺少炸板数据）”
5. `touch_count` 为 None 或小于 10 →“触板仅 {n} 家，封板率尚不稳定”（n 为 None 时写“触板家数未知，封板率尚不稳定”）
6. `feedback_problem(feedback, as_of, require_live=(status == "ready"))` 返回非 None → 原样作为原因
7. 按 Global Constraints 的阈值判档。原因写为“封板率 {S:.0%}，昨日首板今日均涨 {F:+.2%}”

- [ ] **Step 1: 写失败测试**

```python
from datetime import datetime
import pytest
from app.market_time import CN_TZ
from app.services.first_board_temperature import first_board_temperature

DAY = "2026-10-16"
AT = datetime(2026, 10, 16, 10, 30, tzinfo=CN_TZ)

def ladder(seal=.8, up=8, broken=2, as_of=DAY):
    return {"as_of": as_of, "observed_at": AT.isoformat(), "seal_rate": seal, "limit_up": up, "broken": broken}

def feedback(mean=.03, *, mode="live", **summary):
    base = {"sample_count": 12, "coverage": 1., "mean_change_pct": mean, "up_ratio": .6, "small_sample": False, **summary}
    return {"status": "ready", "quote_mode": mode, "as_of": DAY, "groups": {"all": base}}

def temp(lad=None, fb=None, *, status="ready", now=AT, as_of=DAY):
    return first_board_temperature(ladder() if lad is None else lad, feedback() if fb is None else fb,
                                   status=status, as_of=as_of, now=now)

@pytest.mark.parametrize(("seal", "mean", "tier"), [
    (.70, .02, "attack"), (.80, .03, "attack"), (.6999, .02, "probe"), (.70, .0199, "probe"),
    (.70, -.01, "probe"), (.60, 0., "probe"), (.60, -.0001, "rest"), (.5999, .05, "rest"),
])
def test_tier_boundaries(seal, mean, tier):
    result = temp(ladder(seal=seal), feedback(mean))
    assert result["tier"] == tier and result["version"] == "temperature-v1"
    assert result["label"] == {"attack": "进攻", "probe": "试错", "rest": "休息"}[tier]

@pytest.mark.parametrize(("kwargs", "reason"), [
    ({"status": "stale"}, "行情已过期"), ({"status": "waiting"}, "等待有效行情"),
    ({"status": "disabled"}, "自动盯盘已暂停"), ({"status": "error"}, "首板数据暂不可用"),
    ({"lad": {}}, "缺少当日涨停梯队"), ({"lad": ladder(as_of="2026-10-15")}, "缺少当日涨停梯队"),
    ({"lad": ladder(seal=None, broken=None)}, "封板率暂不可用（缺少炸板数据）"),
    ({"lad": ladder(up=7, broken=2)}, "触板仅 9 家，封板率尚不稳定"),
    ({"fb": feedback(small_sample=True)}, "昨日首板样本少于 10 只"),
    ({"fb": feedback(coverage=.79)}, "昨日首板行情覆盖不足 80%"),
    ({"fb": feedback(mode="snapshot")}, "昨日首板反馈不是盘中实测"),
])
def test_unknown_cases(kwargs, reason):
    result = temp(**kwargs)
    assert result["tier"] == "unknown" and result["label"] == "待确认" and result["reason"] == reason

def test_unknown_keeps_available_numbers_and_nulls_missing_ones():
    result = temp(ladder(seal=None, broken=None))
    assert result["seal_rate"] is None and result["touch_count"] is None
    assert result["feedback_mean"] == .03 and result["feedback_sample"] == 12

def test_ten_touches_is_enough():
    assert temp(ladder(up=8, broken=2))["tier"] == "attack"

def test_early_and_session_stage():
    assert temp(now=AT.replace(hour=9, minute=59, second=59))["stage"] == "early"
    assert temp(now=AT.replace(hour=10, minute=0))["stage"] == "session"

def test_closed_after_session_is_close_and_accepts_snapshot_feedback():
    result = temp(fb=feedback(mode="snapshot"), status="closed", now=AT.replace(hour=15, minute=30))
    assert result["stage"] == "close" and result["tier"] == "attack"

def test_lunch_break_keeps_session_reading():
    result = temp(fb=feedback(mode="snapshot"), status="closed", now=AT.replace(hour=12))
    assert result["stage"] == "session" and result["tier"] == "attack"

def test_next_morning_shows_previous_close():
    result = temp(fb=feedback(mode="snapshot"), status="closed", now=datetime(2026, 10, 19, 9, 0, tzinfo=CN_TZ))
    assert result["stage"] == "close" and result["as_of"] == DAY and result["tier"] == "attack"
```

- [ ] **Step 2: 运行，确认失败**

Run: `D:/stockSpace/tick-stock-panel/backend/.venv/Scripts/python.exe -m pytest tests/test_first_board_temperature.py -q`
Expected: FAIL，报 `ModuleNotFoundError: app.services.first_board_temperature`

- [ ] **Step 3: 按上面的判定顺序实现 `first_board_temperature`**

数值字段用与 `_feedback_gate` 相同的有限数判断：排除 bool，用 `math.isfinite`。模块开头的 docstring 说明这是一个只作显示的档位，并注明回测依据见上面的 spec 路径。

- [ ] **Step 4: 运行，确认通过**

Run: `D:/stockSpace/tick-stock-panel/backend/.venv/Scripts/python.exe -m pytest tests/test_first_board_temperature.py -q`
Expected: PASS

---

### Task 3: 接入首板服务（信号记录 + 读取时重算）

**Files:**
- Modify: `backend/app/services/first_board.py`
  - 约 908 行 `ladder.update(...)` 之后：把原来位于约 1068 行的 `status = (...)` 表达式原样上移到这里，紧接着计算 `temperature = first_board_temperature(ladder, feedback, status=status, as_of=str(as_of), now=now)`。原位置只保留 `message` 字典，继续使用这个 `status`。
  - 行循环中、紧挨 `feedback_gate` 的写入处（约 949 行）：`row["evidence"]["temperature"] = dict(temperature)`
  - `snapshot()` 中，状态更新那段 if/elif（约 1158–1167 行）之后、`build_data_status` 之前：`result["temperature"] = first_board_temperature(result.get("market_ladder"), result.get("yesterday_feedback"), status=result["status"], as_of=result.get("as_of"), now=now)`
- Test: `backend/tests/test_first_board_service.py`（在末尾追加）

**Interfaces:**
- Consumes: Task 2 的 `first_board_temperature`
- Produces: `snapshot()["temperature"]`（结构同 Task 2）；每行和每条事件的 `evidence["temperature"]`

- [ ] **Step 1: 写失败测试（沿用现有的 `service`、`quotes`、`NOW` 夹具）**

```python
def _live_feedback(mean, mode="live", day=NOW.date()):
    summary = {"sample_count": 12, "coverage": 1., "mean_change_pct": mean, "up_ratio": .6, "small_sample": False}
    return {"status": "ready", "quote_mode": mode, "as_of": str(day), "observed_at": NOW.isoformat(),
            "groups": {"all": summary}, "rows": []}

def _fix_inputs(service, monkeypatch, *, seal, mean, mode="live"):
    monkeypatch.setattr(fb, "live_ladder", lambda *a, **k: {"limit_up": 8, "broken": 2, "seal_rate": seal})
    monkeypatch.setattr(service._feedback, "evaluate", lambda *a, **k: _live_feedback(mean, mode))

def test_temperature_is_display_only_and_recorded_on_signals(service, monkeypatch):
    _fix_inputs(service, monkeypatch, seal=.5, mean=-.03)
    service.process_snapshot(quotes(), NOW.date(), now=NOW)
    rest = service.snapshot()
    assert rest["temperature"]["tier"] == "rest" and rest["temperature"]["stage"] == "session"
    rest_row = rest["rows"][0]
    assert rest_row["evidence"]["temperature"]["tier"] == "rest"
    assert service.events(NOW.date())[0]["evidence"]["temperature"]["tier"] == "rest"
    _fix_inputs(service, monkeypatch, seal=.8, mean=.03)
    service.process_snapshot(quotes(), NOW.date(), now=NOW)
    attack = service.snapshot()
    assert attack["temperature"]["tier"] == "attack"
    attack_row = attack["rows"][0]
    for key in ("can_buy", "blocked_reasons", "suggested_amount", "event_id"):
        assert attack_row[key] == rest_row[key]
    assert attack["global_blocked_reasons"] == rest["global_blocked_reasons"]

def test_temperature_turns_unknown_when_feedback_expires(service, monkeypatch):
    _fix_inputs(service, monkeypatch, seal=.8, mean=.03)
    service.process_snapshot(quotes(), NOW.date(), now=NOW)
    monkeypatch.setattr(fb, "_now", lambda: NOW + timedelta(seconds=31))
    assert service.snapshot()["temperature"]["tier"] == "unknown"

def test_closed_snapshot_reports_close_temperature(service, monkeypatch):
    closed_at = NOW.replace(hour=16)
    monkeypatch.setattr(fb, "_now", lambda: closed_at)
    _fix_inputs(service, monkeypatch, seal=.5, mean=-.03, mode="snapshot")
    service.process_snapshot(quotes(now=closed_at), NOW.date(), now=closed_at)
    result = service.snapshot()
    assert result["status"] == "closed"
    assert result["temperature"]["stage"] == "close" and result["temperature"]["tier"] == "rest"

def test_empty_snapshot_has_unknown_temperature(service):
    assert service.snapshot()["temperature"]["tier"] == "unknown"
```

- [ ] **Step 2: 运行，确认失败**

Run: `D:/stockSpace/tick-stock-panel/backend/.venv/Scripts/python.exe -m pytest tests/test_first_board_service.py -k temperature -q`
Expected: FAIL，报 `KeyError: 'temperature'`

- [ ] **Step 3: 按 Files 中列出的三处修改接入**

- [ ] **Step 4: 运行首板相关的全部测试，确认通过且没有回归**

Run: `D:/stockSpace/tick-stock-panel/backend/.venv/Scripts/python.exe -m pytest tests/test_first_board_service.py tests/test_first_board_temperature.py tests/test_first_board_feedback.py tests/test_first_board_exchange_limit.py -q`
Expected: 全部 PASS

- [ ] **Step 5: ruff 检查本次改动的文件**

Run: `D:/stockSpace/tick-stock-panel/backend/.venv/Scripts/python.exe -m ruff check app/services/first_board_temperature.py app/services/first_board_feedback.py app/services/first_board.py tests/test_first_board_temperature.py tests/test_first_board_feedback.py tests/test_first_board_service.py`
Expected: 本次新增的代码没有告警（已有告警不在本次处理范围）

---

### Task 4: 前端类型、温度徽章、接入 LiveStatus

**Files:**
- Modify: `frontend/src/lib/api.ts`（在 `FirstBoardSnapshot` 之前加类型；快照里加 `temperature?: FirstBoardTemperature`，注释写“旧后端缺省”）
- Create: `frontend/src/custom/first-board/TemperatureBadge.tsx`
- Create: `frontend/src/custom/first-board/TemperatureBadge.test.tsx`
- Modify: `frontend/src/custom/first-board/LiveStatus.tsx`（作为 `<section aria-label="盯盘状态">` 的第一个子元素渲染）
- Modify: `frontend/src/custom/first-board/FirstBoard.test.tsx`（追加一个用例）

**Interfaces:**
- Consumes: 后端的 `temperature` 结构（Task 2）
- Produces:
  ```ts
  export type FirstBoardTemperatureTier = 'attack' | 'probe' | 'rest' | 'unknown'
  export interface FirstBoardTemperature {
    version: string; tier: FirstBoardTemperatureTier; label: string
    seal_rate: number | null; touch_count: number | null; feedback_mean: number | null; feedback_sample: number | null
    stage: 'early' | 'session' | 'close' | null; as_of: string | null; observed_at: string | null; reason: string
  }
  export function TemperatureBadge({ value }: { value?: FirstBoardTemperature }): JSX.Element | null
  ```

**显示文案（固定）**
- 外层是 `<div data-first-board-temperature title={说明}>`，里面依次是：
  - 徽章 `首板温度 · {label}`
  - `封板率 {Math.round(seal_rate*100)}%`（为 null 时显示 `封板率 —`）
  - `昨日首板均涨 {fmtPct(feedback_mean)}`，颜色用 `priceColorClass`
  - 时段：early 为 `早盘初判`；session 为 `盘中`；close 为 `{MM/DD} 收盘`（取自 `as_of`，如 `11/20 收盘`）；null 时不显示
- 徽章颜色：
  - attack：`positiveBadgeClass`（shared.tsx，蓝色）
  - probe：`border-border text-secondary`
  - rest：`border-amber-500/50 bg-amber-500/10 text-amber-800 dark:text-amber-300`
  - unknown：`border-border text-muted`
- `title` 说明按行拼接（`\n`）：
  1. `进攻：封板率 ≥70% 且昨日首板均涨 ≥2%；休息：封板率 <60%，或封板率 <70% 且昨日首板均涨 <0；其余为试错。`
  2. `只作显示，不影响买入提示与建议金额。`
  3. `赚钱效应仍靠选股，进攻档不代表可以无差别打板。`
  4. stage 为 early 时追加 `早盘参考性较弱。`
  5. tier 为 unknown 时追加 `待确认原因：{reason}`
- `value` 缺失或 `version !== 'temperature-v1'` 时返回 null。

- [ ] **Step 1: 写失败测试 `TemperatureBadge.test.tsx`**（沿用 OpeningDistribution.test.tsx 的 `renderToStaticMarkup` 写法）

```tsx
const base: FirstBoardTemperature = { version: 'temperature-v1', tier: 'attack', label: '进攻', seal_rate: .72, touch_count: 40,
  feedback_mean: .025, feedback_sample: 20, stage: 'session', as_of: '2026-11-20', observed_at: '2026-11-20T10:30:00+08:00', reason: '' }
// render(value) 返回 host；node = host.querySelector('[data-first-board-temperature]')

it('shows tier, both inputs and stage', () => {
  // textContent 包含 '首板温度 · 进攻'、'封板率 72%'、'昨日首板均涨 +2.50%'、'盘中'
  // 徽章 class 包含 'blue'；title 包含 '只作显示，不影响买入提示与建议金额。'
})
it('marks rest in amber with the close date', () => {
  // { tier: 'rest', label: '休息', seal_rate: .68, feedback_mean: -.0448, stage: 'close' }
  // → 包含 '首板温度 · 休息'、'昨日首板均涨 -4.48%'、'11/20 收盘'；徽章 class 包含 'amber'
})
it('flags the early reading', () => {
  // stage: 'early' → 包含 '早盘初判'；title 包含 '早盘参考性较弱。'
})
it('explains unknown and shows placeholders', () => {
  // { tier: 'unknown', label: '待确认', seal_rate: null, reason: '封板率暂不可用（缺少炸板数据）' }
  // → 包含 '首板温度 · 待确认'、'封板率 —'；title 包含 '待确认原因：封板率暂不可用（缺少炸板数据）'
})
it('renders nothing for old or unknown payloads', () => {
  // value 为 undefined，以及 { ...base, version: 'temperature-v9' } → host 为空
})
```

- [ ] **Step 2: 运行，确认失败**

Run（`frontend/` 下）：`pnpm exec vitest run src/custom/first-board/TemperatureBadge.test.tsx`
Expected: FAIL，提示找不到 `./TemperatureBadge`

- [ ] **Step 3: 加 api.ts 类型，实现 `TemperatureBadge.tsx`，并在 `LiveStatus` 中作为 section 的第一个子元素渲染 `<TemperatureBadge value={data.temperature} />`**

- [ ] **Step 4: 在 `FirstBoard.test.tsx` 末尾追加页面级用例**

```tsx
it('puts the temperature first in the live status and hides it for old backends', async () => {
  // 快照带 temperature（tier 'rest'）：section[aria-label="盯盘状态"] 的 firstElementChild 带 data-first-board-temperature 属性
  // 快照 snapshot() 不带 temperature：页面中不存在 [data-first-board-temperature]，而 '市场环境' 标签仍然显示
})
```

- [ ] **Step 5: 运行首板前端测试并构建**

Run: `pnpm exec vitest run src/custom/first-board` 然后 `pnpm build`
Expected: 测试全部 PASS；构建成功，没有类型错误

---

### Task 5: 拷回后端、整体验证

**Files:** 只涉及从 worktree 拷回 Task 1–3 的后端文件，不新增代码。

- [ ] **Step 1: 确认拷回的时机**

先确认没有正在运行的数据流水线，并避开北京时间 08:30、15:35、21:30 前后。然后一次性把 worktree 中的 `backend/app/services/first_board.py`、`first_board_feedback.py`、`first_board_temperature.py` 和三个测试文件拷回主仓库。拷回后告诉用户：可能需要重启 dev.ps1。

- [ ] **Step 2: 在主仓库重跑后端测试**

Run（`backend/` 下）：`uv run pytest tests/test_first_board_service.py tests/test_first_board_temperature.py tests/test_first_board_feedback.py -q`
Expected: 全部 PASS

- [ ] **Step 3: 检查空白字符问题**

Run: `git diff --check`
Expected: 无输出

- [ ] **Step 4: 浏览器检查（休市时）**

dev 服务恢复后打开 `/first-board`。盯盘状态区第一行应为 `首板温度 · 休息　封板率 68% · 昨日首板均涨 -4.48%　11/20 收盘`（11/20 的实际数据）。悬停能看到规则和“只作显示”的说明。盘中表现要到交易日实测，验证报告里要注明这一点。

- [ ] **Step 5: 汇报**

`git status` 和 `git diff --stat` 中应只有本计划涉及的文件，再加上原有的 `queryKeys.ts` 修改与几个未跟踪文档。说明改了什么、验证了什么、哪些未验证（盘中时段）。不提交，等用户决定。删除 worktree 前先查看 memory `worktree-removal-long-paths`。
