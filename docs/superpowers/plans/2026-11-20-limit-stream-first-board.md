# 首板接入异动订阅（炸板、回封判定）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用猫爪异动订阅的交易所事件，给首板每只候选补上「确认封死 / 触板中 / 已开板」证据与文案，不改首板判定。

**Architecture:** `LimitStream` 新增只读的 `limit_state()`，把事件 1、3、4 归并成每只股票的状态；新增纯函数模块把「行情状态 × 流状态 × 订阅健康」合成 `exchange_limit` 证据；`FirstBoardService` 在逐行评估处调用并追加文案。任何异常或订阅不可用都回退为现状。

**Tech Stack:** Python、polars、pytest；现有 `backend/app/services/meoz_limit_stream.py`、`first_board.py`。

**Spec:** `docs/superpowers/specs/2026-11-20-limit-stream-first-board-design.md`

## Global Constraints

- 不改 `state`、`can_buy`、买入门槛、风险预算（spec §1）。
- 只使用事件码 1、3、4；2、5–12 本期不用（spec §2）。
- 事件 3 = 触板后封板满 60 秒的确认；`touching` 的有效期 60 秒，超时且无事件 3 视为 `none`（spec §3.1）。
- 状态排序按交易所时间，同一时刻事件 4 优先于事件 1（spec §3.1）。
- 开板次数优先用涨停池 `open_times`，事件 4 的计数只作补充（spec §5）。
- 降级：订阅 `state != "connected"` 或读取异常 → `status="unknown"`；`seq_gaps > 0` → 仍展示事实，`degraded=True`（spec §3.3）。
- 不新增 WebSocket 连接；`data/` 不提交；Key、票据、连接地址不进日志。
- 工作区是 CRLF（autocrlf=true），脚本改写文件须保留 `\r\n`。
- 项目约定：不自动提交、推送、合并；完成后 `git diff` / `git status` 复核。本计划不含 commit 步骤，提交只在用户明确要求时做。
- 测试从 `backend` 目录运行：`python -m pytest <path> -q`。

## Review Focus

1. 事件乱序、同一毫秒的 4 与 1：期望按规则取 4，不抛异常（Task 2）。
2. 重启后从当日 jsonl 恢复的事件：期望状态可用，但订阅未连接时证据为 `unknown`（Task 2、4）。
3. 昨日事件不得影响今日：`limit_state(day=昨日)` 在今日内存为空时返回空（Task 2）。
4. 行情为 sealed 而流里没有该股事件或最近事件为 4（事件会丢，召回约 50–70%）：期望不标冲突，只保持现状（Task 3）。
5. `get_stream` 抛异常或首板评估时订阅整体不可用：期望首板列表、事件、`can_buy` 与改动前逐项相同（Task 4）。

---

### Task 1: 核对事件码（前置闸门，不写业务代码）

**Files:**
- Create: `tmp/limit_event_codes_check.py`（一次性脚本，不提交）
- Modify: `docs/superpowers/specs/2026-11-20-limit-stream-first-board-design.md`（第 2 节追加「批量核对结果」）

**Interfaces:**
- Produces: 对事件 1、3、4 含义的确认结论；Task 2 的夹具序列。

- [ ] **Step 1: 编写脚本**：读取 `data/meoz_limit_events/v1/date=2026-11-19.jsonl` 与 `date=2026-11-20.jsonl`，以及 `data/market_reference/v1/limit_events/date=<同日>.parquet` 的 `limit_detail`。对每只股票、每个 `ub` 时刻，查找 ±5 秒内是否有事件 4；对每个 `u` 时刻查找 ±5 秒内是否有事件 1；对每个事件 3 检查前 55–65 秒内是否有事件 1。输出三类命中率与未命中清单。
- [ ] **Step 2: 运行**：`python tmp/limit_event_codes_check.py`。期望：事件 4 对 `ub` 的命中率与「未确认封死就开板」的差额一致（未命中项全部能在时间线里找到「`u` 后不足 60 秒即 `ub`」）。
- [ ] **Step 3: 判定**：命中率或解释不符时**停止**，把结果贴给用户，修订 spec 第 2 节后再继续；符合则把统计结果（样本数、命中率、未命中原因）追加到 spec 第 2 节。

---

### Task 2: `LimitStream.limit_state()` 与涨停池开板次数

**Files:**
- Modify: `backend/app/services/meoz_limit_stream.py`（`LimitStream`、`_default_pool`、`poll_pool`）
- Test: `backend/tests/test_meoz_limit_stream.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class LimitState:
      status: Literal["sealed_confirmed", "touching", "opened", "none"]
      at: datetime | None          # 决定 status 的最近事件的交易所时间
      touch_at: datetime | None    # 沿用 touch_times 的首次触板时间
      confirmed_opens: int         # 事件 4 的次数
      pool_open_times: int | None  # 涨停池 open_times, 无则 None

  LimitStream.limit_state(self, now: datetime | None = None, day: date | None = None) -> dict[str, LimitState]
  ```
  `now` 缺省取 `self._now()`；`day` 非当前内存日返回 `{}`。
- Consumes: 现有 `self._events`、`self._touch`、`self._lock`、`self._roll`。

- [ ] **Step 1: 写失败测试**（复用文件内 `make`、`row`、`push`、`_ms`、`NOW=10:00:05`），用例名与断言：
  - `test_limit_state_confirmed_seal`：事件 1@09:40:00、3@09:41:00 → `status == "sealed_confirmed"`，`at` 为 09:41:00。
  - `test_limit_state_touching_expires`：仅事件 1@09:59:30 → `touching`；`now` 取 10:01:00 时 → `none`。
  - `test_limit_state_opened_counts_confirmed_opens`：序列 1,3,4,1,3,4（600360 的真实节奏）→ `opened`、`confirmed_opens == 2`。
  - `test_limit_state_same_millisecond_prefers_open`：事件 1 与 4 同一毫秒、乱序到达 → `opened`。
  - `test_limit_state_wrong_day_empty`：`limit_state(day=昨日)` → `{}`。
  - `test_limit_state_restored_from_archive`：写入当日 jsonl 后新建实例，不连接，`limit_state()` 仍能给出状态。
  - `test_limit_state_ignores_unverified_events`：仅事件 2、5、6、7 → 该股不出现在结果里。
  - `test_poll_pool_records_open_times`：注入返回 `("600360.SH", "09:32:36", 4)` 三元组的 `pool`，`pool_open_times == 4`；同时二元组 `("X", "09:30:00")` 仍被接受且 `pool_open_times is None`。
- [ ] **Step 2: 运行并确认失败**：`python -m pytest tests/test_meoz_limit_stream.py -q`，期望新用例 FAIL（方法不存在）。
- [ ] **Step 3: 实现**：新增 `LimitState` 与 `limit_state`，按交易所时间排序、同时刻 4 优先；只取 `event in {1,3,4}`；`touching` 以 `now - at <= 60s` 判定。`poll_pool` 兼容 2 元组与 3 元组，开板次数存入 `self._pool_opens: dict[str, int]`，`_roll` 换日时清空；`_default_pool` 返回 `(symbol, first_time, open_times)`，`open_times` 缺失为 `None`。
- [ ] **Step 4: 运行并确认通过**：同上命令，全部 PASS，且文件内原有用例不受影响。

---

### Task 3: 证据合成纯函数

**Files:**
- Create: `backend/app/services/first_board_exchange_limit.py`
- Test: `backend/tests/test_first_board_exchange_limit.py`

**Interfaces:**
- Consumes: `LimitState`（Task 2）；`LimitStream.status()` 返回的字典（键 `state`、`seq_gaps`）。
- Produces:
  ```python
  def exchange_limit_evidence(quote_state: str, limit: LimitState | None,
                              stream_status: dict | None, *, now: datetime) -> dict
  ```
  返回 `{"status": "sealed_confirmed|touching|opened|none|unknown", "at": iso|None, "confirmed_opens": int, "open_times": int|None, "conflict": bool, "degraded": bool, "note": str|None}`。`note` 为可直接追加到 `row["reasons"]` 的中文短句，无需追加时为 `None`。

规则（spec §3.2、§3.3）：

| `quote_state` | 流状态 | 结果 |
|---|---|---|
| `sealed` | `sealed_confirmed` | `note`「交易所确认封板 HH:MM:SS」；有开板则附「已开板 n 次后回封」 |
| `sealed` | `touching` | `note`「刚触及涨停，封板未满 60 秒」 |
| `sealed` | `opened`/`none`/无 | `conflict=False`、`note=None`（Task 1 核对：事件召回约 50–70%，缺失不能当作矛盾；2026-11-20 ruling） |
| `broken` | `opened` | `note`「交易所开板 HH:MM:SS，已开板 n 次，尚未回封」 |
| `broken` | `sealed_confirmed` | `conflict=True`，`note=None` |
| 其它 | 任意 | `note=None` |

`n` 取 `open_times`，缺失时取 `confirmed_opens`。`stream_status` 为 `None` 或 `state != "connected"` → `status="unknown"`、`note=None`；`seq_gaps > 0` → `degraded=True`。

- [ ] **Step 1: 写失败测试**（每条规则一个用例，断言 `status`、`conflict`、`degraded`、`note` 的精确文案），额外：
  - `test_sealed_without_stream_event_is_not_conflict`：`limit=None`、已连接 → `status="none"`、`conflict=False`。
  - `test_sealed_with_stale_opened_has_no_note_or_conflict`：行情 `sealed`、流 `opened` → `conflict=False`、`note=None`。
  - `test_not_connected_is_unknown`：`state="idle"` → `unknown`，且不抛异常。
  - `test_seq_gap_keeps_facts_but_degraded`：`seq_gaps=2` → `status` 仍为流状态，`degraded=True`。
  - `test_open_times_preferred_over_confirmed_opens`：`pool_open_times=3`、`confirmed_opens=1` → 文案为「已开板 3 次」。
- [ ] **Step 2: 运行并确认失败**：`python -m pytest tests/test_first_board_exchange_limit.py -q`，期望 FAIL（模块不存在）。
- [ ] **Step 3: 实现** `exchange_limit_evidence`，纯函数、无 IO、不读时钟（时间由 `now` 传入）。
- [ ] **Step 4: 运行并确认通过**：同上命令，全部 PASS。

---

### Task 4: 接入 `FirstBoardService`

**Files:**
- Modify: `backend/app/services/first_board.py`（新增 `_exchange_limit_states()`，约 `:722` 旁；逐行评估处约 `:949-953` 之后）
- Test: `backend/tests/test_first_board_service.py`

**Interfaces:**
- Consumes: `get_stream(data_dir).limit_state(now)`、`.status()`（Task 2）；`exchange_limit_evidence`（Task 3）。
- Produces: 每行新增 `row["exchange_limit"]` 与 `row["evidence"]["exchange_limit"]`；`note` 非空时追加到 `row["reasons"]`。`_exchange_limit_states(self, now: datetime) -> tuple[dict[str, LimitState], dict | None]`，失败返回 `({}, None)`，写法与 `_exchange_touch()` 一致（`logger.warning` + 空结果）。每次评估只取一次，供所有行共享。

- [ ] **Step 1: 写失败测试**（沿用该文件现有的服务夹具与行情构造，monkeypatch `_exchange_limit_states`）：
  - `test_broken_row_gets_exchange_open_note`：行情 `broken` + 流 `opened` → `row["exchange_limit"]["status"] == "opened"`，`reasons` 含「交易所开板」。
  - `test_sealed_row_after_reseal_note`：行情 `sealed` + `sealed_confirmed` 且 `open_times=2` → `reasons` 含「已开板 2 次后回封」。
  - `test_exchange_limit_never_changes_state_or_can_buy`：同一输入分别在有流证据与 `({}, None)` 下评估，`state`、`can_buy`、`blocked_reasons`、`event_id`、`suggested_amount` 逐项相等。
  - `test_exchange_limit_failure_falls_back`：`_exchange_limit_states` 内部 `get_stream` 抛异常 → 评估正常完成，`row["exchange_limit"]["status"] == "unknown"`，其余字段与基线相同。
  - `test_restored_events_unknown_when_not_connected`：流有恢复的事件但 `status()["state"]="idle"` → `unknown`。
- [ ] **Step 2: 运行并确认失败**：`python -m pytest tests/test_first_board_service.py -q -k exchange_limit`，期望 FAIL。
- [ ] **Step 3: 实现**：评估入口取一次 `_exchange_limit_states(now)`；逐行在 `seal_quality` 之后调用 `exchange_limit_evidence(row["state"], limits.get(symbol), status, now=now)`，写入 `row` 与 `row["evidence"]`，`note` 追加到 `row["reasons"]`（不重复追加）。不得读取该结果参与 `blocked`、`can_buy`、`shadow`。
- [ ] **Step 4: 回归**：`python -m pytest tests/test_first_board_service.py tests/test_first_board_attention_integration.py tests/test_meoz_limit_stream.py tests/test_first_board_exchange_limit.py -q`，期望全部 PASS，原有用例零改动。

---

### Task 5: 回放对比报告与文档收尾

**Files:**
- Create: `tmp/limit_state_replay.py`（一次性脚本，不提交）
- Modify: `docs/superpowers/specs/2026-11-20-limit-stream-first-board-design.md`（追加「回放结果」，状态改为已实施）
- Modify: `docs/猫爪数据对接/猫爪数据对接规划.md`（第 11.5 节「实施进度」追加一行 4d 首板接入及未做的盘中联调）

- [ ] **Step 1: 编写回放脚本**：对 11-19、11-20 每个有事件的股票，用 `LimitStream` 实例加载当日 jsonl（`now` 取收盘后），输出 `confirmed_opens` 与涨停池 `open_times` 的差额分布，并列出差额 > 0 的股票。
- [ ] **Step 2: 运行**：`python tmp/limit_state_replay.py`。期望：`confirmed_opens <= open_times` 恒成立；差额全部可由「未确认封死就开板」解释（抽查 3 只，对照 `limit_detail`）。出现 `confirmed_opens > open_times` 则停止并汇报。
- [ ] **Step 3: 写文档**：把分布、抽查结论、样本只有两天写进 spec；规划文档注明「盘中联调尚未做，需下一交易日 09:20 后观察一只已开板个股」。
- [ ] **Step 4: 收尾复核**：`git status`、`git diff --stat`，确认只有预期文件被改，`tmp/` 与 `data/` 未被纳入；汇报改动、验证结果与剩余风险。

---

## Self-Review

- **Spec 覆盖：** §2 事件码核对 → Task 1；§3.1 → Task 2；§3.2 规则表 → Task 3、4；§3.3 降级 → Task 3、4；§4 验证 1–4 → Task 1/2/3/4/5，验证 5（盘中联调）明确留作未做。
- **类型一致：** `LimitState`、`limit_state()`、`exchange_limit_evidence()`、`_exchange_limit_states()` 的名称与签名在各任务中一致。
- **比例：** 5 个任务，只给签名、断言和规则表，无函数体。
