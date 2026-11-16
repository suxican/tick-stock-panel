# 市场环境页「环境结论」实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用户为"环境分三档 × 资金档位"9 格预设仓位上限与打法；环境页每天定位当天格子，展示预设、与博弈页市场闸门叠加后的生效上限，以及该格历史证据。

**Architecture:** 校准归档摘要新增 `cells`；偏好新增 `env_matrix_plan`；新服务 `environment_conclusion.py` 汇合环境历史、偏好、当日博弈报告与最新校准归档；`GET /api/regime/conclusion` 薄层转发；环境页新增结论卡与 3×3 编辑弹窗。

**Tech Stack:** Python 3.11、FastAPI、Pydantic v2、Polars、pytest；React 18、TypeScript、TanStack Query、vitest。

**Spec:** `docs/superpowers/specs/2026-11-15-environment-conclusion-design.md`

## Global Constraints

- 未设置的格子不出现任何默认仓位数字；生效上限永不高于当日闸门。
- 预设 `cap_pct` 为 0–100 整数；博弈报告 `allocation.total_cap` 为小数，后端边界换算 `round(total_cap * 100)`。
- 打法固定列表：`打板`、`半路`、`低吸`、`趋势持股`、`空仓观望`；备注最长 60 字。
- 情绪三档映射复用 `market_game_calibration.MATRIX_SENTIMENT`，不建副本。
- 证据样本门槛复用 `market_game_credibility.MIN_DAYS`（30）。
- 文案不得出现"建议仓位""推荐打法"。
- 偏好接口路径随设置路由前缀：`/api/settings/preferences/env-matrix-plan`（设计稿写作 `/api/preferences/...`，以实际前缀为准）。
- 提交用 `git commit -s`；工作区 CRLF；前端只用 `pnpm exec`。

## Review Focus

1. 博弈报告目录有上百份含全市场快照的大文件：结论接口不得逐份全读。→ Task 3 只读 mtime 不早于环境日期的报告，测试 `test_gate_skips_reports_older_than_date`。
2. 同日多份报告：取 `created_at` 最新者。→ Task 3 `test_gate_picks_latest_same_day_report`。
3. 损坏或缺 `allocation` 的报告：跳过，不让接口 500。→ Task 3 `test_gate_ignores_broken_report`。
4. 偏好被手改成非法结构（非 dict、cap 为字符串）：读时清洗，不报错。→ Task 2 `test_get_plan_cleans_bad_stored_values`。
5. 编辑弹窗取消后再打开：显示已保存的值，而不是上次未保存的修改。→ Task 4 弹窗取消测试。

---

### Task 1: 校准归档每格统计

**Files:** Modify `backend/app/services/market_game_credibility.py`；Test `backend/tests/test_market_game_credibility.py`（不存在则新建）

**Produces:** `summarize(result)["cells"]: dict[str, dict[str, dict]]`，键为 `strong_expand` 等 9 格中在结果里出现过的格；每格 `all` / `train` / `test` 三段，各为 `{"n": int, "mean": float|None, "positive_rate": float|None, "p10": float|None}`；`n < MIN_DAYS` 时除 `n` 外为 None。数据取自 `groups` 中 `shadow:matrix:<格>`、`@train`、`@test` 的 `ew_1`。

- [ ] 写失败测试：构造含 `shadow:matrix:strong_expand`（n=40）及其 `@train`（n=10）/`@test`（n=30）的 `groups`，断言 `all`、`test` 有数值、`train` 只有 `n`；无矩阵分组时 `cells == {}`。
- [ ] 运行确认失败 → 实现 → `uv run pytest tests -q -k "credibility or calibration"` 通过。

### Task 2: 预设偏好读写与接口

**Files:** Modify `backend/app/services/preferences.py`、`backend/app/api/settings.py`；Test `backend/tests/test_env_matrix_plan.py`

**Produces:** `ENV_MATRIX_CELLS: tuple[str, ...]`（9 格，顺序为强/震荡/弱 × 放量/平量/缩量）、`ENV_MATRIX_STYLES: tuple[str, ...]`、`get_env_matrix_plan() -> dict[str, dict]`、`set_env_matrix_plan(plan: dict) -> dict`（整表替换，返回清洗后结果）；`GET/PUT /api/settings/preferences/env-matrix-plan`，PUT 体为 `{"plan": {...}}`，返回 `{"plan": {...}}`。

清洗规则：丢弃未知格键；`cap_pct` 必须是 0–100 的整数（bool 不算），否则该格丢弃；`styles` 只保留列表内值并去重保序；`note` 转字符串截断到 60 字。

- [ ] 写失败测试：往返；未知格键/未知打法/越界或非整数 cap/超长备注被清洗；空表可写；`test_get_plan_cleans_bad_stored_values`（直接写入非法 JSON 结构后读取）；API PUT 后 GET 一致。
- [ ] 运行确认失败 → 实现 → 测试通过 + Ruff。

### Task 3: 结论服务与接口

**Files:** Create `backend/app/services/environment_conclusion.py`；Modify `backend/app/api/regime.py`；Test `backend/tests/test_environment_conclusion.py`

**Consumes:** Task 1 `cells`、Task 2 `get_env_matrix_plan`、`regime_builder.load_regime_history`、`market_game_credibility.CalibrationArchive(data_dir).latest()`。

**Produces:** `build_conclusion(data_dir: Path, *, plan: dict | None = None) -> dict`（`plan` 为 None 时读偏好），返回设计 5.4 的结构；`GET /api/regime/conclusion`。

规则：取 `regime_history` 最新一行；格子按 `MATRIX_SENTIMENT` 与 `capital_volume`；缺失原因文案"环境分缺失"/"资金档位未计算"/"暂无环境历史"。闸门：扫描 `<data_dir>/user_data/market_game/mg_<32hex>.json`，只读 mtime 不早于环境日期北京时间 00:00 的文件，`report.as_of == date` 者中取 `created_at` 最大，读取失败或缺 `allocation.total_cap` 跳过。`effective_cap_pct`、`limited_by` 按设计。

- [ ] 写失败测试：9 格定位（参数化）；缺 state / 缺 capital_volume / 空历史；取小值、只有预设、只有闸门、相等；`0.5 → 50`；`test_gate_picks_latest_same_day_report`；`test_gate_skips_reports_older_than_date`（旧 mtime 的同日报告不读）；`test_gate_ignores_broken_report`；无归档或归档无 `cells` 时 `evidence` 为 None；API 空数据目录返回 200。
- [ ] 运行确认失败 → 实现 → `uv run pytest tests -q -k "conclusion or env_matrix or credibility or regime"` 通过 + Ruff。

### Task 4: 环境页结论卡与编辑弹窗

**Files:** Modify `frontend/src/lib/api.ts`、`frontend/src/lib/queryKeys.ts`、`frontend/src/pages/Regime.tsx`、`frontend/src/pages/Regime.css`、`frontend/src/pages/Regime.test.tsx`

**Produces:** 类型 `EnvConclusion`、`EnvMatrixPlan`；`api.regimeConclusion()`、`api.envMatrixPlan()`、`api.envMatrixPlanUpdate(plan)`；查询键 `QK.regimeConclusion = ['regime-conclusion']`、`QK.envMatrixPlan = ['env-matrix-plan']`，并把 `'regime-conclusion'` 加入 `DAILY_DATA_QUERY_PREFIXES`。保存预设成功后让两键失效。

卡片 `aria-label="环境结论"`，文案按设计 5.5；弹窗 `ariaLabel="编辑环境预设"`，3×3 网格，每格含 `aria-label="<强·放量>仓位上限"` 的数字输入、5 个打法复选框、备注输入，并显示该格历史天数（来自结论接口不提供时省略——本期从 `api.regimeHistory` 已有行统计格子天数）。

- [ ] 写失败测试：已设置（显示"仓位上限 50%"与"被市场闸门 50% 限制"）；未设置（显示"未设置"且不含百分数）；无格子（显示缺失原因）；证据不足（显示"样本不足"）；弹窗保存调用 `envMatrixPlanUpdate` 且载荷正确；取消后再打开显示原值。
- [ ] 运行确认失败 → 实现 → `pnpm exec vitest run src/pages/Regime.test.tsx` 通过 + `pnpm build`。

### Task 5: 实测、文档、提交

- [ ] 临时数据目录启动预览，合成环境历史 + 一份同日博弈报告 + 一份含 `cells` 的校准归档；检查已设置/未设置、闸门限制、窄屏与深浅主题。
- [ ] 真实数据跑一次校准归档，使 `cells` 可用。
- [ ] `docs/market-capital.md` 增"环境结论"一节；`docs/data-directory.md` 中 `preferences.json` 说明补 `env_matrix_plan`。
- [ ] `git diff --check`，提交。
