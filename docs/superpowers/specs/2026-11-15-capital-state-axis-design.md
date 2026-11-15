# 市场环境「资金状态轴」设计

状态：待审阅 · 2026-11-15

## 1. 意图

系统对市场环境的判断目前几乎全部来自情绪：5 档环境分（赚钱/投机/抗跌/趋势）、6 阶段情绪周期、博弈页的亏钱效应门。资金只有零散痕迹：`regime_history` 存了 `total_amount` 但不参与任何结论；成交活跃度与集中度只在博弈页的冻结快照里，没有逐日历史。

用户按"情绪 × 资金"两个因子判断环境和制定计划：情绪决定能不能做，资金决定做什么、做多大。本设计补上第二条轴：给每个交易日一个独立的资金状态，可回看、可校准，但暂不改变任何现有结论。

## 2. 目标、非目标与反目标

**目标**

- 每个交易日有资金状态：总量档位（放量/平量/缩量）、集中度档位（集中/中性/分散）、承接标记（放量滞涨/缩量上涨）。
- 全部由本地日线派生，历史可回算，不新增数据源。
- 市场环境页展示当日资金状态与走势。
- 资金档位进入现有离线校准，作为影子分组检验次日表现。

**非目标**

- 情绪 × 资金的环境矩阵与仓位结论（下一期，依赖本期校准结果）。
- 主线板块成交占比（概念成分是当前快照，早年归属失真）。
- 盘中资金暂定值；本期只在盘后管道产出。
- 融资余额、ETF 份额、龙虎榜等外部资金数据。
- 统一各页面的情绪口径。

**反目标**（出现即视为失败）

- 现有环境分 `score`、`state`、`phase` 的任何历史数值发生变化。
- 资金档位影响首板过滤、仓位闸门、回测环境过滤或挖掘。
- 成交额不可比（样本缺失、口径变化）时仍给出档位。
- 把成交额表述为"资金流入"或"增量资金"。成交额只说明交易活跃度。

## 3. 约束

- 遵循 `CONTRIBUTING.md`。本改动属 L3（直接修改核心 `regime_builder`），须保持聚焦并做兼容验证。
- 无未来函数：任一日的档位只使用当日及之前的数据；分位参考窗口不含当日。
- 单位：成交额为人民币元；占比为小数制（0–1）；倍数无单位；分位为 0–100。
- 窗口按 `regime_history` 中的实际交易日行计，不用自然日。
- 旧 `regime_history` 缺新列时可读，页面显示"未计算"，不报错、不补零。
- 前端命令使用 `pnpm` / `pnpm exec`。

## 4. 方案取舍

| | 做法 | 结论 |
|---|---|---|
| A | 只在校准脚本加资金影子分组 | 未选：页面不可见，日常用不上 |
| **B** | **`regime_history` 新增资金列与标签，环境页展示，同时进校准** | **用户选定** |
| C | 把量能并入环境分作为第 5 维 | 否决：改变所有下游历史结果，且两个因子混成一个数后无法交叉 |

B 内部的一个取舍：资金列放进 `regime_history`，而不是像主线那样另建目录。理由是环境页、校准脚本、首板上下文、交易日志上下文都已读这张表，后续接入零成本；成交额列在环境聚合时已经加载，新增聚合几乎不增加开销。代价是历史回填要走一次现有的环境全量重算。

## 5. 设计

### 5.1 模块

新增纯函数模块 `backend/app/services/market_capital.py`，结构仿照 `market_phase.py`：阈值常量集中在顶部，提供聚合表达式、单行整理函数和全序列标注函数。`regime_builder` 只做调用和持久化。

### 5.2 每日聚合列（在 `_aggregate_daily` 内，随批次计算）

有效成交额定义：`amount` 非空且大于 0（停牌零成交不计）。

| 列 | 含义 |
|---|---|
| `amount_valid_count` | 当日有效成交额的股票数 |
| `amount_top5_share` | 成交额最高的前 5% 股票（个数向上取整）成交额之和 ÷ 全部有效成交额；有效股票少于 100 只时为 null |

分母与现有 `total_amount` 使用同一股票集合。

### 5.3 序列派生列（全量历史重标，仿 `refresh_phase_labels`）

| 列 | 计算 |
|---|---|
| `amount_multiple` | 当日 `total_amount` ÷ 此前 20 个交易日中"可比日"成交额的中位数 |
| `amount_change_5d` | 近 5 日成交额均值 ÷ 再往前 5 日均值 − 1；10 日中任一日不可比则为 null |
| `amount_multiple_pctl` | `amount_multiple` 相对此前最多 252 个有效值的分位 |
| `top5_share_pctl` | `amount_top5_share` 相对此前最多 252 个有效值的分位 |
| `capital_volume` | 分位 ≥75 为 `expand` 放量，≤25 为 `shrink` 缩量，其余 `flat` 平量 |
| `capital_concentration` | 分位 ≥75 为 `concentrated` 集中，≤25 为 `dispersed` 分散，其余 `neutral` 中性 |
| `capital_flag` | `expand` 且 `median_pct ≤ 0` 为 `volume_stall` 放量滞涨；`shrink` 且 `median_pct > 0` 为 `shrink_advance` 缩量上涨；否则 null |

规则细节：

- **可比日**：该日 `total_amount > 0`，且 `amount_valid_count` 与当日相差不超过 5%。此前 20 日中可比日少于 15 个时，`amount_multiple` 为 null。
- **分位**：至少 60 个参考样本才计算，并列取中位秩，与博弈页心理画像和亏钱效应门的做法一致。
- 任一输入为 null，则对应档位为 null；`capital_flag` 依赖的档位或 `median_pct` 为 null 时为 null。
- 用滚动分位而非固定阈值，是因为成交额水位和集中度随年份有结构性漂移。

### 5.4 持久化与回填

- 新增 `refresh_capital_labels(data_dir, *, start=None)`，在现有三处 `refresh_phase_labels` 调用点（`api/regime.py` 重算、`regime_builder.compute_regime_incremental`、`repair_daily`）之后调用。写入使用 `atomic_write_parquet`。
- 5.2 的两列对存量历史为 null，需用户执行一次环境全量重算（`POST /api/regime/recompute`，环境页已有入口）。重算前派生列全部为 null。
- `upsert_regime_history` 已能给旧行补缺失列，无需迁移脚本。注意它以新行的列为准对齐旧行，不在新行里的派生列（`phase` 与本设计的派生列）会被丢弃，因此每次 upsert 之后必须对全量序列重标，不能只标新增日期。

### 5.5 接口与前端

`GET /api/regime/history` 和 `/latest` 按列透传，后端接口无需新增。前端在 `frontend/src/lib/api.ts` 的 `RegimeRow` 增加上述可选字段。

市场环境页改动两处：

1. "当前阶段"卡片旁增加一张"资金"卡：

   ```
   资金  2026-11-13
   放量 · 集中          ⚠ 放量滞涨
   成交额 1.32 倍（20日中位） · 5日 +8.4%
   前5%个股占成交 41.2%
   ```

   档位为 null 时显示"未计算"，并提示需重算环境历史；不显示 0。

2. "环境综合分趋势"图的可叠加图例增加"成交额分位"一项（`amount_multiple_pctl`，0–100），默认不显示。用分位而不是倍数，是因为该图左轴固定为 0–100、右轴为家数，倍数（约 0.5–2）放不进现有两根轴。

### 5.6 校准

`market_game_calibration.py`：

- `prepare_history` 把 `capital_volume`、`capital_concentration`、`capital_flag`、`median_pct` 作为影子输入透传，不进入 `_metrics`，因此不改变 `complete_inputs` 与 `coverage`，原有分组结果不变。
- `_shadow_keys` 与 `SHADOW_DEFINITIONS` 新增三组，不限趋势：

| 分组 | 对照组 |
|---|---|
| `shadow:capital_expand` | `shadow:capital_shrink` |
| `shadow:capital_concentrated` | `shadow:capital_dispersed` |
| `shadow:capital_volume_stall` | `shadow:capital_expand_advance`（放量且中位涨跌 > 0） |

博弈页"规则可信度"卡按归档通用渲染影子行，无需改前端。新增分组加剧多重比较风险，文档须注明。

### 5.7 文档

- 新增 `docs/market-capital.md`：口径、阈值、可比性规则、边界。
- 更新 `docs/data-directory.md` 中 `regime_history/` 一行。
- 更新 `docs/market-game.md` 影子分组表。

## 6. 验证

- `backend/tests/test_market_capital.py`：前 5% 占比（含取整与不足 100 只）、可比日过滤、倍数、5 日变化、分位（样本不足为 null、并列取中位秩）、档位、承接标记、任一输入缺失时为 null、当日不进入自身参考窗口。
- `test_regime_builder.py`：新列写入；旧 schema 行可读；`score`/`state`/`phase` 在加入资金列前后逐行相同。
- `test_market_game_calibration.py`：新影子分组的归属；原有分组结果不变。
- 前端：`Regime.test.tsx` 覆盖有值、未计算两种状态；`pnpm build`。
- 实际数据：全量重算后抽查若干已知放量日（如 2024-09-30、2024-10-08）应为放量档；运行一次校准并记录三组结果。
- `uv run ruff check`、`git diff --check`。

## 7. 以下为我自行决定、未经用户确认的事项

- 集中度指标用"前 5% 个股成交占比"，不沿用博弈页的平方和指数；并增加"中性"档，而非只分集中/分散。
- 总量基准为 20 日中位数，可比日至少 15 个。
- 承接标记按资金轴自身的档位定义，与校准脚本现有的 `shadow:volume_stall`（要求趋势上行、成交额 ≥ 前 5 日均值 1.2 倍）不是同一口径；两者并存。
- 分位阈值 75/25、最少 60 个样本。这些是研究初始值，未经校准。

## 8. 留给实现者

- 前 5% 占比的 Polars 聚合写法，须保持向量化。
- `refresh_capital_labels` 与 `refresh_phase_labels` 是否合并为一次写盘。
- 校准归档是否需要提升版本号；确认新分组不破坏旧归档的读取。
- 环境全量重算在真实数据上的耗时，若明显变慢需报告。
- 资金卡的具体样式，沿用环境页现有卡片类名。
