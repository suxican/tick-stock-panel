# 纯风险闸门与盘后定版 设计

状态：已获用户批准 · 2026-11-16

## 1. 意图

环境结论（`docs/superpowers/specs/2026-11-15-environment-conclusion-design.md`）把用户 9 格预设与博弈页"市场闸门"取小值。上线后发现所用的 `allocation.total_cap` 不是纯风险闸门：当天不符合博弈页两种入场形态（恐慌修复、趋势回踩）时它直接为 0。真实报告 11-10 处于修复、无任何否决，闸门仍为 0%。形态少见，因此大多数日子会把用户上限错误地压成 0。

本设计先修正闸门口径，再让每天收盘后自动生成博弈计划并把当天的环境结论存档，使结论每天完整、事后可复盘。

## 2. 目标、非目标与反目标

**目标**

- 博弈报告新增只反映风险的 `allocation.market_gate`，环境结论改用它。
- 定时盘后管道成功后自动生成当日博弈计划，并存档当天的环境结论。
- 用户保存预设时，为最新交易日追加一版存档；存档只追加不覆盖。
- 环境页显示存档状态。

**非目标**

- 复盘页核对实际持仓与存档上限（下一期）。
- 改动 `total_cap`、候选、规则版本或任何既有报告数值。
- 手动"立即运行"的管道触发定版（与实验室周期一致，只挂定时管道）。

**反目标**

- 旧报告（无 `market_gate`）被当作 0% 或 100% 闸门。
- 定版失败导致盘后管道失败。
- 存档被覆盖或删除。

## 3. 方案（已选）

| 选择 | 做法 | 否决的替代 |
| --- | --- | --- |
| 闸门 | 报告新增 `market_gate` 字段，在 `market_game.py` 与 `total_cap` 同处计算 | 环境结论自行从报告字段重算（复制规则）；取消闸门（丢掉唯一两段一致的风控） |
| 触发 | 挂在定时盘后管道成功之后，与实验室周期同一入口 | 固定时间单独定时（数据可能未就绪） |
| 存档 | 每个交易日一份文件，内含多版记录，只追加 | 每日一份、首次写入即冻结（用户晚间改预设后无法反映） |

## 4. 设计

### 4.1 纯风险闸门

在 `market_game.py` 计算 `market_gate`（小数，0–1）：

1. 初值 1.0。
2. 短期指数收益缺失，或 `quality == "limited"`：取 `min(·, 0.1)`。
3. 亏钱效应门 `caution`：乘 0.5。
4. `quality == "unavailable"`、存在 `risk_vetoes`（含亏钱效应门否决）、或周期为退潮 / 冰点：0。

不考虑入场形态、`trend_cap`、用户 `total_cap` 参数。`Allocation` 模型新增 `market_gate: Ratio | None = None`；旧报告读取为 None。`total_cap` 与规则版本 `1.4.0` 不变。

### 4.2 环境结论

- `_gate` 改读 `allocation.market_gate`。当日报告存在但该字段为 None（旧报告）时，`gate = {"cap_pct": None, "report_id", "as_of", "note": "当日计划为旧版，未含风险闸门"}`，按无闸门处理生效上限（`limited_by="plan"`）。
- 返回新增 `archive`：`{"versions": int, "latest_at": str, "latest_effective_cap_pct": int | None} | None`。

### 4.3 结论存档

- 目录 `<data_dir>/user_data/environment_conclusion/<YYYY-MM-DD>.json`，内容 `{"date": ..., "versions": [ {recorded_at, trigger, conclusion} ]}`；`trigger` 为 `"pipeline"` 或 `"preset"`。
- `append_version(data_dir, conclusion, trigger)`：只在 `conclusion.date` 非空时写；读旧文件追加后原子写回；与上一版的 `cell`、`plan`、`gate`、`effective_cap_pct` 都相同时不追加。文件损坏时不覆盖，记录警告并跳过。
- `PUT /api/settings/preferences/env-matrix-plan` 保存成功后追加一版（`trigger="preset"`）；失败不影响保存结果。

### 4.4 盘后定版

`daily_pipeline._scheduled_pipeline_task` 在实验室周期之后调用 `post_close_freeze(app_state)`（新模块 `app/services/post_close_freeze.py`），软失败只记日志：

1. 取 `regime_history` 最新日期 D。
2. 若博弈服务存在，且没有 `as_of == D` 且含 `market_gate` 的报告（旧版报告不算），用最近一份报告的 `requested_risk`（无则默认）以"最新盘后"模式生成；`AnalysisBusyError` 视为已在生成，跳过。生成结果的 `as_of` 不等于 D 时只记录，不重试。
3. `append_version(..., trigger="pipeline")`。

### 4.5 前端

环境结论卡：

- 闸门为旧版时显示"当日计划为旧版，未含风险闸门"。
- 底部显示"已存档 n 版 · 最新 HH:MM"；最新存档的生效上限与当前不同时加"存档时 X%"。无存档不显示。

### 4.6 文档

更新 `docs/market-game.md`（新字段与口径）、`docs/market-capital.md`（环境结论闸门与定版）、`docs/data-directory.md`（新目录）。

## 5. 验证

- `market_gate`：正常日 1.0；无入场形态、无否决时仍为 1.0（`total_cap` 为 0）；限制数据 0.1；caution 0.5；否决、退潮、冰点、不可用为 0；旧报告模型校验通过。
- 环境结论：使用 `market_gate`；旧报告时 `gate.cap_pct is None` 且生效上限取预设；`archive` 字段。
- 存档：追加、去重、损坏不覆盖、日期为空不写、保存预设后追加。
- 定版：已有当日报告不生成；无报告时用最近 `requested_risk` 生成；忙时跳过；生成异常不抛出；最后写入存档。
- 前端：旧版闸门文案、存档状态；`pnpm build`。
- 真实数据：对现有报告重放一次 `market_gate`，核对 11-10 不再是 0。
