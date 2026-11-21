# 首板「龙头」标签收紧 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把「龙头」从“任一行业或概念里涨幅第一”改成“某个概念里今天最先触板、且该概念涨停成员至少 3 只”，事件流不可用时一律待确认。

**Architecture:** `first_board_role.classify_roles` 的龙头判定改用触板时间（新增三个关键字入参），中军不变；`FirstBoardService` 新增一个读取事件流事实的方法并传入；前端只改龙头标签的悬停说明、规则页说明文字和类型。

**Tech Stack:** Python 3.11、Polars、pytest；React、TypeScript、vitest。

**Spec:** `docs/superpowers/specs/2026-11-20-first-board-leader-tighten-design.md`

## Global Constraints

- 龙头只看 `kind == "concept"` 的板块；行业不参与龙头，中军（行业和概念、流通市值或成交额前 N）一行不改。
- 三条同时满足才算该概念的龙头：板块涨停成员 ≥ 3 只；该股在其中；该股首次触板时间最早，时间相同并列都算。板块涨停成员 = 今日有触板记录且不在“最近状态为开板”集合里的成员。门槛 3 写成模块常量 `MIN_LIMIT_UP_MEMBERS = 3`，不进配置。
- 龙头为 `None`（待确认）的情形：事件流不可靠（今日无触板记录，或今日记录过 `seq` 缺口）；候选股自己没有触板记录；该股没有任何概念归属或概念成员名单缺失。`reason` 在这些情形下必须含“待确认”，因为 `role_gate` 会把它拼进阻断原因。
- 三态：`True`／`False`／`None`。确定且所有所属概念都不满足才是 `False`。
- 不新增 `FirstBoardConfig` 配置项；不改买入判断；龙头不得注入历史研究（沿用模块文档的约束）。
- 保持 CRLF：`first_board.py`、`first_board_role.py`、`test_first_board_service.py`、`api.ts`、`CandidateTable.tsx`、`RulesPanel.tsx`、`FirstBoard.test.tsx` 都是 CRLF；用脚本改文件时按 `\r\n` 读写。
- 后端测试命令：在 `backend/` 下用 `D:/stockSpace/tick-stock-panel/backend/.venv/Scripts/python.exe -m pytest ...`；前端命令只能用 `pnpm exec` 或 `pnpm build`（在 `frontend/` 下）。
- 开发环境：动 `backend/` 之前先查 3018 端口服务是否由带 `--reload` 的 dev.ps1 启动；是的话在旁边的 worktree 里开发（`git worktree add --detach ../tick-stock-panel-leader HEAD`），最后一次性拷回；不是（例如只有预览服务）则可直接在主仓库改。无论哪种，拷回或改完后都告诉用户可能需要重启服务。
- 不提交、不推送；用户要求提交时使用 `git commit -s`，且不带上工作区里原有的 `frontend/src/lib/queryKeys.ts` 修改和无关的未跟踪文档。
- **Ruling（规格没写到的后果）**：新口径下，尚未触板的候选股（状态为“临近涨停”）没有触板记录，龙头为 `None`。如果用户在规则里开启了“只观察龙头”，`role_gate` 会把所有“临近涨停”的候选按“待确认”阻断，也就没有买入提示。这个筛选目前是关闭的（`stock_roles` 为 `None`），本计划按规格实现，不做特殊处理，并在交付时明确告诉用户。代价：开启“只观察龙头”后买入提示几乎不会出现；以后若要让它可用，需要另议盘前口径。

## Review Focus

1. **候选股尚未触板 + 开启“只观察龙头”**：龙头待确认，候选保留在列表里但 `can_buy` 为 False，阻断原因含“待确认”，不能被当作“不是龙头”而隐藏。→ Task 2 `test_untouched_candidate_is_unknown_and_blocked_under_the_leader_filter`
2. **事件流有缺口或没数据**：所有候选的龙头都是 `None`，不是 `False`。→ Task 1 `test_unreliable_stream_or_untouched_candidate_is_unknown`；Task 2 `test_touch_facts_are_unreliable_with_gaps_or_no_touches`
3. **板块涨停成员正好 2 只与 3 只**：2 只不算板块效应，3 只算。→ Task 1 `test_two_limit_up_members_is_not_a_board_effect`
4. **最早触板的同伴已开板**：已开板的股票不计入涨停成员，也不能当“最先”的基准；候选股自己已开板则不是龙头。→ Task 1 `test_opened_members_are_not_limit_up_members`
5. **只有行业归属**：行业里再早也不是龙头，且为待确认而不是 `False`。→ Task 1 `test_industry_membership_alone_never_makes_a_leader`
6. **查看的不是今天的交易日**：事件流只保存当日，龙头待确认。→ Task 2 `test_touch_facts_are_unreliable_with_gaps_or_no_touches` 中的 `touch_times` 返回空字典用例

---

### Task 1: `classify_roles` 的龙头改用触板时间

**Files:**
- Modify: `backend/app/services/first_board_role.py`
- Test: `backend/tests/test_first_board_role.py`

**Interfaces:**
- Produces: `classify_roles(current, groups, float_shares, symbols, *, core_top_n: int, touch: Mapping[str, datetime] | None = None, opened: Collection[str] = (), touch_reliable: bool = False) -> dict[str, dict]`
  - 返回值结构不变（`leader`、`core`、`matches`、`reason`）。龙头命中的 `matches` 项新增两个键：`limit_up_count: int | None`（该概念今日涨停成员数）和 `touch_at: str | None`（该股触板时间，`datetime.isoformat()`）；只有中军命中的项这两个键为 `None`。
  - 默认参数下（不传触板信息）龙头为 `None`，中军行为与现在完全一致。
- 常量：`MIN_LIMIT_UP_MEMBERS = 3`
- `touch` 的键为大写完整代码；函数内部对 `touch`、`opened` 的键做 `strip().upper()` 规范化，与现有 `_quote_map` 一致。

- [ ] **Step 1: 改写测试（`test_first_board_role.py`）**

给 `classify` 助手加 `**facts` 透传到 `classify_roles`。新增 `T0 = datetime(2026, 10, 16, 9, 31, 12, tzinfo=CN_TZ)` 和 `at(seconds)`。`concept(members=None)` 助手返回 `group(members, gid="概念A", kind="concept")`。

删除 `test_leader_is_first_by_change_and_ties_count_as_first` 与 `test_any_concept_hit_counts_and_matches_name_the_board`，把 `test_duplicate_or_missing_quotes_are_not_counted` 的断言改为 `["core"] is None`，其余用例（覆盖率、中军、无归属、`float_shares_map`、`role_gate`）原样保留，并新增：

```python
def test_leader_is_the_earliest_touch_in_a_concept_with_three_limit_up_members():
    touch = {SYMBOL: at(0), PEERS[0]: at(5), PEERS[1]: at(9)}
    role = classify(frame([.1] * 6), [concept()], touch=touch, touch_reliable=True)
    assert role["leader"] is True
    match = next(m for m in role["matches"] if "leader" in m["roles"])
    assert match["name"] == "概念A" and match["limit_up_count"] == 3 and match["touch_at"] == at(0).isoformat()
    later = {SYMBOL: at(7), PEERS[0]: at(5), PEERS[1]: at(9)}
    assert classify(frame([.1] * 6), [concept()], touch=later, touch_reliable=True)["leader"] is False

def test_two_limit_up_members_is_not_a_board_effect():
    touch = {SYMBOL: at(0), PEERS[0]: at(5)}
    assert classify(frame([.1] * 6), [concept()], touch=touch, touch_reliable=True)["leader"] is False

def test_simultaneous_touch_ties_count_as_leader():
    touch = {SYMBOL: at(3), PEERS[0]: at(3), PEERS[1]: at(9)}
    assert classify(frame([.1] * 6), [concept()], touch=touch, touch_reliable=True)["leader"] is True

def test_opened_members_are_not_limit_up_members():
    # 最早触板的同伴已开板: 剩下 SYMBOL、P2、P3 共 3 只, SYMBOL 最早 → 龙头。
    touch = {SYMBOL: at(5), PEERS[0]: at(0), PEERS[1]: at(6), PEERS[2]: at(7)}
    assert classify(frame([.1] * 6), [concept()], touch=touch, opened={PEERS[0]}, touch_reliable=True)["leader"] is True
    # 候选股自己已开板: 不是龙头。
    assert classify(frame([.1] * 6), [concept()], touch=touch, opened={SYMBOL}, touch_reliable=True)["leader"] is False

def test_industry_membership_alone_never_makes_a_leader():
    touch = {SYMBOL: at(0), PEERS[0]: at(5), PEERS[1]: at(9)}
    role = classify(frame([.1] * 6), [group()], touch=touch, touch_reliable=True)  # group() 默认 kind="industry"
    assert role["leader"] is None and "待确认" in role["reason"]

def test_unreliable_stream_or_untouched_candidate_is_unknown():
    touch = {SYMBOL: at(0), PEERS[0]: at(5), PEERS[1]: at(9)}
    assert classify(frame([.1] * 6), [concept()], touch=touch)["leader"] is None  # touch_reliable 缺省 False
    role = classify(frame([.1] * 6), [concept()], touch={PEERS[0]: at(5), PEERS[1]: at(9), PEERS[2]: at(11)}, touch_reliable=True)
    assert role["leader"] is None and "待确认" in role["reason"]

def test_leader_in_one_concept_is_enough_and_industry_still_counts_for_core():
    touch = {SYMBOL: at(0), PEERS[0]: at(5), PEERS[1]: at(9)}
    other = concept([SYMBOL, *[f"30000{i}.SZ" for i in range(1, 6)]])
    role = classify(frame([.1] * 6), [other, concept()], touch=touch, touch_reliable=True)
    assert role["leader"] is True
    assert classify(frame([.1] * 6, amounts=[9e9, 1e8, 1e8, 1e8, 1e8, 1e8]), [group()])["core"] is True
```

（第三个 `other` 概念只有 SYMBOL 一只有行情的成员，覆盖率不足，用来确认“任一概念满足即可”不受另一概念干扰；实现者可调整成员构造，但保留该断言。）

- [ ] **Step 2: 运行，确认失败**

Run: `python -m pytest tests/test_first_board_role.py -q`（用 Global Constraints 里的解释器）
Expected: 新增用例 FAIL（`classify_roles` 不接受 `touch` 等关键字参数）

- [ ] **Step 3: 在 `first_board_role.py` 实现**

按 Global Constraints 的三条判定改写龙头；`change_rank` 等排名字段继续计算（中军与展示仍用），但龙头不再依据它。更新模块 docstring 里“龙头 = 板块内当日涨幅排名第一”的描述。无触板信息、事件流不可靠、候选无触板记录、无概念归属时的 `reason` 文案按“待确认”约束自拟，且要能区分是“数据不足”还是“确实不是”。

- [ ] **Step 4: 运行，确认通过**

Run: `python -m pytest tests/test_first_board_role.py -q`
Expected: 全部 PASS；再跑 `ruff check app/services/first_board_role.py tests/test_first_board_role.py`，本次改动无新增告警

---

### Task 2: 服务读取事件流事实并传入，改写服务测试

**Files:**
- Modify: `backend/app/services/first_board.py`（新增方法；`classify_roles(...)` 调用处，约 878–882 行）
- Test: `backend/tests/test_first_board_service.py`

**Interfaces:**
- Consumes: Task 1 的 `classify_roles(..., touch=, opened=, touch_reliable=)`
- Produces: `FirstBoardService._limit_touch_facts(self, now: datetime, day: date) -> tuple[dict[str, datetime], frozenset[str], bool]`
  - 返回 `(触板时间, 最近状态为开板的股票, 是否可靠)`。
  - 可靠 = `stream.touch_times(day)` 非空，且 `stream.status()["seq_gaps"] == 0`。
  - 触板时间取 `stream.touch_times(day)` 中每项的 `.at`；开板集合取 `stream.limit_state(now, day=day)` 中 `status == "opened"` 的股票。
  - 读取失败（任何异常）返回 `({}, frozenset(), False)` 并记一条 warning 日志，不得抛出。读取方式照抄 `_exchange_limit_states`（`get_stream(self.repo.store.data_dir)`）。

- [ ] **Step 1: 写失败测试并改写已有角色测试**

先新增助手，放在 `_role_service` 旁：

```python
def _touch_facts(service, monkeypatch, *, candidate_first, reliable=True, candidate_touched=True):
    base = datetime(2026, 10, 16, 9, 31, 0, tzinfo=CN_TZ)
    touch = {peer: base + timedelta(seconds=10 + i) for i, peer in enumerate(ROLE_PEERS[:3])}
    if candidate_touched:
        touch[SYMBOL] = base if candidate_first else base + timedelta(seconds=30)
    monkeypatch.setattr(service, "_limit_touch_facts", lambda now, day: (touch, frozenset(), reliable))
```

`_role_service` 里的概念成员保持不变（`kind: "concept"`，6 个成员）。把下列已有用例改为使用 `_touch_facts`（不再依赖 `_role_quotes` 的涨幅参数；`_role_quotes` 可保留但参数不再决定龙头）：

- `test_all_roles_keep_existing_behaviour_but_label_the_role`：`candidate_first=False` → `leader is False`，`can_buy is True`
- `test_leader_filter_allows_sector_leader`：`candidate_first=True` → `can_buy is True`，`leader is True`，`matches[0]["name"] == "概念A"`，发布 1 条事件
- `test_non_leader_is_hidden_without_events`：`candidate_first=False` → `rows == []`，`role_hidden_count == 1`，无事件
- `test_core_or_leader_any_selected_role_matches`：第一段用 `candidate_first=False`（龙头 False、中军待确认 → 保留并阻断，原因含“待确认”），第二段断言不变
- `test_alerted_stock_that_loses_leadership_stays_visible_and_blocked`：先 `candidate_first=True` 处理一次，再 `candidate_first=False` 处理一次，断言不变
- `test_closed_session_labels_roles_from_latest_displayed_quotes`：用 `candidate_first=True`，仍断言 `leader is True`、`can_buy is False`、无事件

再新增：

```python
def test_untouched_candidate_is_unknown_and_blocked_under_the_leader_filter(service, monkeypatch):
    _role_service(service, monkeypatch, ["leader"])
    _touch_facts(service, monkeypatch, candidate_first=False, candidate_touched=False)
    service.process_snapshot(_role_quotes(.01), NOW.date(), now=NOW)
    snapshot = service.snapshot()
    row = snapshot["rows"][0]
    assert row["stock_role"]["leader"] is None and row["can_buy"] is False
    assert any("待确认" in reason for reason in row["blocked_reasons"])
    assert snapshot["coverage"]["role_hidden_count"] == 0

def test_touch_facts_are_unreliable_with_gaps_or_no_touches(service, monkeypatch):
    from app.services import meoz_limit_stream as stream_module
    touch = {SYMBOL: SimpleNamespace(at=NOW)}

    def fake(gaps=0, touches=touch, opened=()):
        states = {symbol: SimpleNamespace(status="opened") for symbol in opened}
        return SimpleNamespace(touch_times=lambda day: touches, limit_state=lambda now, day: states,
                               status=lambda: {"seq_gaps": gaps})
    monkeypatch.setattr(stream_module, "get_stream", lambda data_dir: fake())
    assert service._limit_touch_facts(NOW, NOW.date()) == ({SYMBOL: NOW}, frozenset(), True)
    monkeypatch.setattr(stream_module, "get_stream", lambda data_dir: fake(opened=("600001.SH",)))
    assert service._limit_touch_facts(NOW, NOW.date())[1] == frozenset({"600001.SH"})
    monkeypatch.setattr(stream_module, "get_stream", lambda data_dir: fake(gaps=1))
    assert service._limit_touch_facts(NOW, NOW.date())[2] is False
    monkeypatch.setattr(stream_module, "get_stream", lambda data_dir: fake(touches={}))  # 非当日或没有触板
    assert service._limit_touch_facts(NOW, NOW.date()) == ({}, frozenset(), False)
    monkeypatch.setattr(stream_module, "get_stream", lambda data_dir: (_ for _ in ()).throw(RuntimeError("x")))
    assert service._limit_touch_facts(NOW, NOW.date()) == ({}, frozenset(), False)

def test_leader_label_never_changes_buying(service, monkeypatch):
    _role_service(service, monkeypatch, None)
    _touch_facts(service, monkeypatch, candidate_first=True)
    service.process_snapshot(_role_quotes(.01), NOW.date(), now=NOW)
    first = service.snapshot()["rows"][0]
    _touch_facts(service, monkeypatch, candidate_first=False, reliable=False)
    service.process_snapshot(_role_quotes(.01), NOW.date(), now=NOW)
    second = service.snapshot()["rows"][0]
    for key in ("can_buy", "blocked_reasons", "suggested_amount", "event_id"):
        assert first[key] == second[key]
```

- [ ] **Step 2: 运行，确认失败**

Run: `python -m pytest tests/test_first_board_service.py -k "role or leader or touch" -q`
Expected: FAIL（`_limit_touch_facts` 不存在、`monkeypatch.setattr` 报属性不存在）

- [ ] **Step 3: 实现 `_limit_touch_facts`，并在 `classify_roles` 调用处传入**

调用处用 `self._limit_touch_facts(now, as_of)` 取一次，传 `touch=`、`opened=`、`touch_reliable=`；紧邻现有的 `try`，不要放进行循环里。

- [ ] **Step 4: 运行首板相关全部后端测试和 ruff**

Run: `python -m pytest tests -k "first_board" -q`
Expected: 全部 PASS
Run: `ruff check app/services/first_board.py app/services/first_board_role.py tests/test_first_board_service.py tests/test_first_board_role.py`
Expected: 只剩改动前就存在的 I001、BLE001，无新增告警

---

### Task 3: 前端悬停说明、规则页文字与类型

**Files:**
- Modify: `frontend/src/lib/api.ts`（`FirstBoardRoleMatch` 加 `limit_up_count?: number | null`、`touch_at?: string | null`；`core_top_n` 附近的注释之外，如有“龙头为涨幅第一”的注释一并改正）
- Modify: `frontend/src/custom/first-board/CandidateTable.tsx`（`RoleTags` 的 `title`）
- Modify: `frontend/src/custom/first-board/RulesPanel.tsx`（第 112 行的说明文字）
- Test: `frontend/src/custom/first-board/FirstBoard.test.tsx`

**Interfaces:**
- Consumes: Task 1 输出的 `matches[].limit_up_count`、`matches[].touch_at`
- 龙头标签的 `title`：
  - 有 `touch_at` 的命中项，按 `limit_up_count` 从大到小取前 3 个，每项写成 `{板块名}（今日 {n} 只涨停，{HH:MM:SS} 最先触板）`，用“；”连接，整体前缀 `龙头：`。时间用北京时间的时分秒（`stamp(touch_at).slice(-8)`）。
  - 没有 `touch_at` 的（旧后端）保持现在的格式 `龙头：{名称、名称}`。
  - 中军标签不变。
- 规则页说明改为：`按当前行业与概念归属盘中判定，任一板块命中即算：龙头为概念内今日最先触板（该概念至少 3 只涨停，需要交易所事件流，无数据时待确认），中军为行业或概念内流通市值或成交额进入前 {core_top_n}。不属于所选角色的个股不列出、不提示；历史验证不包含此过滤。`

- [ ] **Step 1: 写失败测试（`FirstBoard.test.tsx`）**

在现有 `labels matched roles…` 用例旁新增：

```tsx
it('explains the leader tag with the board, its limit-up count and the first touch time', async () => {
  const base = snapshot()
  const match = (name: string, count: number, at: string) => ({ id: name, kind: 'concept' as const, name, change_rank: 1, cap_rank: null, amount_rank: null,
    roles: ['leader' as const], limit_up_count: count, touch_at: at })
  calls.firstBoardSnapshot.mockResolvedValue({ ...base, rows: base.rows.map(row => ({ ...row, stock_role: { leader: true, core: null, reason: '', matches: [
    match('小概念', 3, '2026-09-30T09:40:05+08:00'), match('先进封装', 8, '2026-09-30T09:31:12+08:00'),
    match('概念三', 5, '2026-09-30T09:35:00+08:00'), match('概念四', 4, '2026-09-30T09:36:00+08:00')] } })) })
  await render()
  const tag = [...host.querySelectorAll('span')].find(item => item.textContent === '龙头')!
  expect(tag.title).toBe('龙头：先进封装（今日 8 只涨停，09:31:12 最先触板）；概念三（今日 5 只涨停，09:35:00 最先触板）；概念四（今日 4 只涨停，09:36:00 最先触板）')
})
```

并在已有的规则页测试里（搜索 `toggleExact('龙头')` 附近）加一句：开启角色筛选后页面文字包含 `龙头为概念内今日最先触板`。已有的 `labels matched roles…` 用例（无 `touch_at`，期望 `龙头：概念A`）必须原样保持通过。

- [ ] **Step 2: 运行，确认失败**

Run: `pnpm exec vitest run src/custom/first-board/FirstBoard.test.tsx`（在 `frontend/` 下）
Expected: 两个新增断言 FAIL，其余 PASS

- [ ] **Step 3: 实现**

- [ ] **Step 4: 运行首板前端全部测试并构建**

Run: `pnpm exec vitest run src/custom/first-board` 然后 `pnpm build`
Expected: 全部 PASS，构建成功

---

### Task 4: 整体验证与汇报

- [ ] **Step 1: 如用了 worktree，一次性拷回后端文件**（`first_board.py`、`first_board_role.py`、两个测试文件；新文件按 CRLF）；确认没有正在运行的流水线，避开北京时间 08:30、15:35、21:30 前后。拷回后在主仓库重跑 `python -m pytest tests -k "first_board" -q`，应全部 PASS。

- [ ] **Step 2: 空白字符检查**：`git diff --check`（CRLF 引起的“尾随空白”提示属于仓库现状，忽略）。

- [ ] **Step 3: 联调**：重启后端后访问 `/api/first-board/snapshot`。休市且事件流未连上时，每只候选的 `stock_role.leader` 应为 `null`（不是 `true` 或 `false`），`reason` 含“待确认”；页面上不应出现“龙头”标签，中军标签照常。截一张页面截图。

- [ ] **Step 4: 汇报**：说明改了什么、验证了什么；明确写出：盘中龙头是否合理要等交易日用真实事件流观察，未实测；“只观察龙头”筛选开启后“临近涨停”候选会因无触板记录全部待确认（见 Global Constraints 里的 Ruling）；`git status` 中本计划之外的文件不动。不提交。
