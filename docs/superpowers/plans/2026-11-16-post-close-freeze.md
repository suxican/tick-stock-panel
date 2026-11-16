# 纯风险闸门与盘后定版 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 博弈报告新增纯风险闸门 `market_gate`，环境结论改用它；定时盘后管道成功后自动生成当日博弈计划并存档环境结论。

**Spec:** `docs/superpowers/specs/2026-11-16-post-close-freeze-design.md`（规则、字段与边界以设计为准，本计划只列任务、接口与测试）。

## Global Constraints

- `total_cap`、候选、`RULE_VERSION`、既有报告数值不变。
- 旧报告无 `market_gate` 时视为"闸门未知"，不得当 0 或 1。
- 定版全程软失败；存档只追加不覆盖，损坏文件跳过不重写。
- 提交 `git commit -s`；工作区 CRLF；前端只用 `pnpm exec`。

## Review Focus

1. 当日已有用户手动报告：定版不重复生成。→ Task 4 `test_freeze_skips_generation_when_report_exists`。
2. 用户晚间多次保存相同预设：存档不产生重复版本。→ Task 3 `test_append_dedupes_identical_conclusion`。
3. 存档 JSON 损坏：不覆盖。→ Task 3 `test_append_skips_corrupt_archive`。
4. 生成抛异常或博弈服务未加载：仍写存档且不抛出。→ Task 4 `test_freeze_survives_generation_failure`。

---

### Task 1: 纯风险闸门

**Files:** `backend/app/services/market_game.py`、`backend/app/services/market_game_models.py`；Test `backend/tests/test_market_game.py`（或现有分配测试所在文件）

**Produces:** `allocation.market_gate: float | None`（报告），`Allocation.market_gate: Ratio | None = None`。

- [ ] 失败测试：正常无形态日 `total_cap == 0` 且 `market_gate == 1.0`；limited → 0.1；caution → 0.5；否决 / 退潮 / 冰点 / 不可用 → 0；旧报告 dict 无该字段时 `GameReport.model_validate` 通过且为 None。
- [ ] 实现 → `uv run pytest tests -q -k market_game` 通过。

### Task 2: 环境结论改用纯闸门

**Files:** `backend/app/services/environment_conclusion.py`；Test `backend/tests/test_environment_conclusion.py`

- [ ] 测试夹具 `_report` 写 `market_gate`；新增旧报告（只有 `total_cap`）→ `gate.cap_pct is None`、`gate.note` 存在、`effective_cap_pct` 取预设、`limited_by == "plan"`；`market_gate=1.0` 时生效为预设。
- [ ] 实现 → 测试通过。

### Task 3: 结论存档

**Files:** Create `backend/app/services/environment_conclusion_archive.py`；Modify `backend/app/services/environment_conclusion.py`（返回 `archive`）、`backend/app/api/settings.py`（保存预设后追加）；Test `backend/tests/test_environment_conclusion_archive.py`

**Produces:** `append_version(data_dir: Path, conclusion: dict, trigger: str) -> bool`、`load_day(data_dir: Path, day: str) -> dict | None`、`ARCHIVE_DIR = "environment_conclusion"`。

- [ ] 失败测试：首次写入；第二版追加；相同内容去重；`date` 为空不写；损坏文件不覆盖返回 False；`build_conclusion` 返回 `archive` 摘要；`update_env_matrix_plan` 后存在 `trigger == "preset"` 的版本。
- [ ] 实现 → 测试通过。

### Task 4: 盘后定版挂钩

**Files:** Create `backend/app/services/post_close_freeze.py`；Modify `backend/app/jobs/daily_pipeline.py`（`_scheduled_pipeline_task`）；Test `backend/tests/test_post_close_freeze.py`

**Produces:** `post_close_freeze(app_state) -> dict`，返回 `{"date", "generated": bool, "skipped_reason": str | None, "archived": bool}`。

- [ ] 失败测试：已有当日报告不生成；无报告时以最近报告 `requested_risk` 调用 `generate(None, risk)`；无任何报告用 None；`AnalysisBusyError` 跳过；生成抛异常或无服务仍存档且不抛；无环境历史时什么都不做。
- [ ] 实现并在 `_scheduled_pipeline_task` 实验室周期后调用（try/except 记日志）→ 测试通过。

### Task 5: 前端

**Files:** `frontend/src/lib/api.ts`、`frontend/src/pages/Regime.tsx`、`frontend/src/pages/Regime.test.tsx`

- [ ] 类型 `gate.cap_pct: number | null`、`gate.note?`、`archive`；卡片显示旧版闸门文案、"已存档 n 版 · 最新 HH:MM"、不同时"存档时 X%"。
- [ ] 测试 → 实现 → `pnpm build`；临时数据目录页面实测。

### Task 6: 文档、真实数据核对、提交

- [ ] 用现有真实报告的快照重算 `market_gate` 核对 11-10（只读）。
- [ ] 更新 `docs/market-game.md`、`docs/market-capital.md`、`docs/data-directory.md`；提交。
