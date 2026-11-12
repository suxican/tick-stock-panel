# data 目录数据说明与维护约定

本文说明运行时数据根目录中各文件夹保存什么、由谁更新、哪些功能读取，以及备份和重建时需要保留什么。`data/` 里的数据不进入 Git；说明文档保存在 `docs/`，随代码一起维护。

最近核对：**2026-11-08**。本次目录清单覆盖当前工作区的 **87 个一级目录**：46 个业务、缓存、工具或暂存目录，以及 41 个 `.test-*` 测试目录。日期分区、标的、任务 ID 和版本号用路径模式表示，不逐个列出历史日期或用户记录。目录存在不代表数据完整，空目录也不代表功能已运行。

## 1. 数据根目录与阅读方式

本文的 `data/` 指实际 `settings.data_dir`。源码运行默认使用项目根目录的 `data/`，可由 `DATA_DIR` 覆盖；桌面包、Docker 的默认位置和旧目录恢复规则见 [配置实现](../backend/app/config.py) 与 [部署指南](deployment.md)。不要只凭当前终端位置判断程序正在使用哪份数据。

路径中 `<date>` 表示日期，`<symbol>` 表示带交易所后缀的标的，`<id>` 表示对应业务标识。下文的“可重建”有前提：原始数据、历史定义、权限和上游接口仍然可用；不表示删除后必然自动恢复，也不保证重新计算与历史结果相同。

数据大致沿以下顺序流动：

```text
数据源 → 标准化同步 → 原始行情 / 基础资料 / 扩展数据
                         ↓
                 enriched 基础指标
                         ↓
           筛选 / 策略 / 回测 / 监控 / 市场研究

user_data / strategies / pools → 用户定义与配置 → 各业务模块
```

## 2. 行情与基础资料

### 2.1 标的资料与复权因子

| 目录 | 文件与内容 | 更新、读取与重建条件 |
| --- | --- | --- |
| `instruments/` | `instruments.parquet`：股票代码、名称、上市日、股本、最小报价单位及行情辅助字段。 | 标的同步更新，供搜索、行情、指标和策略读取；重新获取依赖数据源资料完整性。同步会保留既有非空股本等静态信息，不沿用过期涨跌停价。 |
| `instruments_etf/` | `instruments_etf.parquet`：ETF 标的维表。 | ETF 同步维护，供资产识别、ETF 图表与计算使用；应与股票维表分开。 |
| `instruments_index/` | `instruments_index.parquet`：指数标的维表。 | 指数同步维护，供指数图表和资产识别使用；读取兼容旧维表混有 ETF 的情况。 |
| `adj_factor/` | `all.parquet`：股票除权事件，主要为 `symbol`、`trade_date`、`ex_factor`，可带分红、送配股、除权前价格等字段。 | 因子同步按标的和日期合并，供日线及分钟复权使用；变更后需重算受影响 enriched。能否重拉取决于历史因子可得性。 |
| `adj_factor_etf/` | `all.parquet`：ETF 除权因子。 | 与股票因子隔离，供 ETF 复权；重建条件同上，缺少因子不能直接解释为没有除权事件。 |

实现入口：[repository.py](../backend/app/tickflow/repository.py)、[instrument_sync.py](../backend/app/services/instrument_sync.py)、[index_sync.py](../backend/app/services/index_sync.py)、[kline_sync.py](../backend/app/services/kline_sync.py)。

### 2.2 日线与分钟行情

| 目录 | 文件与内容 | 更新、读取与重建条件 |
| --- | --- | --- |
| `kline_daily/` | `date=<date>/*.parquet`：股票原始日线，开高低收、成交量、成交额等；通常为 `part.parquet`，流式历史同步也可能生成 `part-stream-<n>.parquet`。 | 历史同步、盘中持久化和盘后定版更新；是图表、指标和回测的基础。重拉要求上游仍提供对应历史。 |
| `kline_etf_daily/` | `date=<date>/part.parquet`：ETF 原始日线。 | ETF 行情同步维护，供 ETF 图表与回测使用；不可混入股票目录。 |
| `kline_index_daily/` | `date=<date>/part.parquet`：指数原始日线。 | 指数同步维护，供指数展示和市场环境计算；不是所有指数实时快照的完整存档。 |
| `kline_daily_enriched/` | `date=<date>/part.parquet`：股票前复权基础行情及原始价、换手率、连板递推状态、报价时间等窄表字段。 | 日线同步后的指标流水线、修复重算和当日实时更新维护；供筛选、策略、回测与监控读取。 |
| `kline_etf_enriched/` | 同样按日期存放 ETF 基础增强行情。 | ETF 指标流水线维护，服务 ETF 查询与计算。 |
| `kline_index_enriched/` | 同样按日期存放指数基础增强行情。 | 指数指标流水线维护，服务指数查询与计算。 |
| `kline_minute/` | `date=<date>/part.parquet`：股票分钟 OHLC、量额和可选分钟基准标记；另有 `.raw_basis` 迁移标记。 | 分钟补齐、全市场分钟落盘和盘后流程按配置与能力更新，按 `symbol + datetime` 合并；供分时图、分钟策略和回测读取。历史补拉受源窗口限制。 |
| `kline_etf_minute/` | `date=<date>/part.parquet`：ETF 分钟行情。 | ETF 分钟同步与读取独立分流；用于 ETF 分时和策略。资产类型须通过实际路由识别。 |

关键口径：

- `enriched` **不是完整技术指标归档**。MA、MACD 和多数信号在读取或计算时生成；重建需原始日线、复权因子、维表及历史股本，不能只凭一张当前维表保证历史结果相同。
- enriched 的 `open/high/low/close` 为前复权价，`raw_close/raw_high/raw_low` 为原始价；涨跌停判断不能混用。其换手率为百分数值，例如 `5` 表示 `5%`。
- 分钟 `datetime` 为北京时间墙钟时间，不含时区。旧分钟数据可能是前复权口径，不能将所有存量文件直接认定为原价；以迁移标记和 `minute_price_basis` 等元数据为准。分钟复权投影同时处理价格、成交额，成交量保持不变。
- 日期目录是实际交易日分区；不能用自然日数量推断缺失交易日。读取原始日线应覆盖分区内所有 Parquet 文件。

实现入口：[repository.py](../backend/app/tickflow/repository.py)、[kline_sync.py](../backend/app/services/kline_sync.py)、[pipeline.py](../backend/app/indicators/pipeline.py)、[minute_adjust.py](../backend/app/services/minute_adjust.py)。DuckDB 在这里使用内存数据库挂载 Parquet 视图，并不意味着根目录必有一份持久化行情 `.db` 文件。

### 2.3 财务、盘口与榜单

`financials/` 下每张表存于 `<table>/part.parquet`，保存多个报告期：

| 子目录 | 数据内容 | 主要读取用途 |
| --- | --- | --- |
| `metrics/` | 财务指标，如盈利、成长和估值相关指标。 | 财务分析、因子与筛选。 |
| `income/` | 利润表。 | 公司财务分析。 |
| `balance_sheet/` | 资产负债表。 | 资产与负债分析。 |
| `cash_flow/` | 现金流量表。 | 现金流分析。 |
| `shares/` | 带报告期、公告时间的历史股本。 | 历史换手率、规模等计算。 |

由 [financial_sync.py](../backend/app/services/financial_sync.py) 手工或按财务调度配置同步；首次/新增标的补历史，已有标的补最新报告并合并。当前自动周期主要更新 `metrics`，不能理解为全部五表实时刷新。财务报告期末 `period_end` 不等于信息公开日期；历史分析必须遵守公告可得性。删除后能否恢复多期历史取决于数据源。

| 目录 | 文件与内容 | 更新、读取与重建条件 |
| --- | --- | --- |
| `depth5/` | `date=<date>/part.parquet`：涨跌停候选的封板判定旁表，包括 `sealed_up/down`、买卖一档量、状态和采集时间。 | [depth_service.py](../backend/app/services/depth_service.py) 盘中维护内存，盘后定版、启动补跑或显式持久化时落盘；供连板梯队和总览读取。不是全市场完整五档逐笔历史；量单位为手，不能靠日 K 重建历史盘口。 |
| `dragon_tiger/` | `date=<date>.json`：Fuyao 龙虎榜综合、机构、游资榜及日期/状态。 | [dragon_tiger.py](../backend/app/services/dragon_tiger.py) 在复盘或 AI 上下文查询时按需拉取，历史日缓存，当日现取；未发布时可能返回前一交易日并标明回退。删除后依赖上游历史重取。 |
| `news_catalysts/` | `date=<date>.json`：Fuyao 异动解读中归属该交易日的 A 股个股记录（代码、异动标签、关键词、解读正文、异动与首次发布时间）及取得时间。 | [news_catalysts.py](../backend/app/services/news_catalysts.py) 在打开「资讯分析」时按需拉取该日及前 10 个交易日，历史日缓存，当日现取不落盘；空结果和被截断的响应不缓存。涨跌幅、涨停数和次日表现在读取时由本地日线补算，不在缓存内。删除后依赖上游历史重取。 |
| `news_announcements/` | `<symbol>.json`：巨潮资讯网该股已取得的全部公告（标题、发布时间、PDF 地址）及每个自然日的取得时间；`_org_ids.json` 为巨潮机构代码表副本。 | [news_announcements.py](../backend/app/services/news_announcements.py) 在「资讯分析 → 公告排雷」点击同步时，按自选股在后台拉取约 20 个交易日；读取只读本地文件，风险分类在读取时按标题规则计算。公告日次日 06:00 后取得的日期不再重拉；机构代码表 7 天内复用，过期且上游失败时沿用旧副本。删除后需重新同步。 |
| `auction_benchmark/` | `date=<date>.json`：Fuyao 竞价异动、短线风向标名单及概念信息。 | [auction_benchmark.py](../backend/app/services/auction_benchmark.py) 按需缓存历史日；当日/次日收益在读取时由本地日线补算，不是名单缓存里的固定收益记录。删除后需上游重取。 |

## 3. 扩展数据与页面

| 目录 | 文件与内容 | 更新、读取与保留要求 |
| --- | --- | --- |
| `ext_data/` | `<id>/config.json` 保存字段和拉取定义，`part.parquet` 保存快照，`timeseries/date=<date>/part.parquet` 保存时序；`.builtin_presets_seeded` 为预置初始化标记。 | 手工导入、开放接口和配置的定时拉取维护；供扩展页面、列表及因子读取。写入按 `symbol`（没有该列时取首列）及可选时间列合并再替换文件，不应理解为每次删除所有旧键。定义及不可重取的手工数据需备份。 |
| `instruments_ext/` | 旧版扩展快照的兼容目录，历史布局随旧数据而定。 | 当前预建并挂载兼容视图，旧路径还用于部分统计回退；当前扩展主写入位于 `ext_data/<id>/`。发现旧数据应先确认迁移情况。 |
| `kline_ext/` | 旧版扩展时序的兼容目录。 | 与上一项类似；不能仅因当前版本不主要写入，就认定旧文件无用。 |
| `data_sources/` | `<name>.yaml` 或 `.yml`：自定义 HTTP 数据源定义，包括数据集、接口、字段映射等。 | 设置保存或导入后由数据源加载器读取；删除会使该自定义源不可用。当前目录为空，配置需要备份。 |
| `ext_pages/` | `<id>/page.json` 元数据和 `bundle.js` 页面产物。 | 上传或导入写入、试载回填状态，前端启动动态加载；删除丢失页面及启用状态，需原包或备份恢复。当前目录为空。 |
| `zzshare_home/` | `v1/<date>/close.json` 正式收盘榜、`observations.json` 本地实际观测（盘中及当日收盘），含采集时间和排行。 | 查询按需采集；扩展在连续交易时段周期采集、收盘后采集。历史 `as_of` 回看只读已收集记录；删掉盘中观测后不能用收盘榜还原。 |
| `zzshare_radar/` | `v1/<date>/report.json` 题材强度榜及 `members/<hash>.json` 当日成分。 | 显式同步结合 ZZShare 成分和本地相邻交易日日线计算，读取接口只读；完整计算后替换榜单。重建需要同日成分、日历和行情。 |

当前 `ext_data` 中的 `ext_gn_ths/`、`ext_hy_ths/` 分别承载概念和行业扩展数据，`etf_broad_flow_daily/` 承载 ETF 宽基资金流日序列，`zzshare_limit_reasons/` 承载涨停归因。各自字段映射与时序配置以对应定义为准，不是一套通用行情 schema。鉴权 Key 由 `user_data/secrets.json` 管理；接口地址和拉取配置也可能含敏感信息，说明中不抄录配置值。

实现入口：[ext_data.py](../backend/app/services/ext_data.py)、[ext_pull.py](../backend/app/services/ext_pull.py)、[旧路径兼容](../backend/app/api/ext_data.py)、[数据源加载器](../backend/app/data_providers/custom/loader.py)、[ext_pages.py](../backend/app/services/ext_pages.py)、[zzshare_home.py](../backend/app/services/zzshare_home.py)、[采集调度](../backend/app/custom/zzshare.py)、[zzshare_radar.py](../backend/app/services/zzshare_radar.py)。

## 4. 业务结果与研究记录

| 目录 | 文件与内容 | 更新、读取与重建条件 |
| --- | --- | --- |
| `backtest_results/` | `run_id=<id>.parquet`：旧基础回测的 `stats_json`、交易数等简要汇总，不是完整交易明细。 | [基础回测服务](../backend/app/services/backtest.py) 写入，其 `get_result()` 当前未实现读取。重跑可产生新汇总，不能保证复现旧运行。 |
| `backtest_runs/` | `<run_id>.json`：正式回测的输入指纹、定义/代码版本、时间因果检查等证据；写入期间可短暂出现锁文件。 | [evidence.py](../backend/app/backtest/evidence.py) 保存且禁止同编号覆盖，正式回测 worker 生成。原运行证据不可当普通缓存删除。 |
| `job_store/` | `<job_id>.json`：同步、修复等任务的状态、时间、进度日志和结果摘要。 | [pipeline_jobs.py](../backend/app/services/pipeline_jobs.py) 在创建、启动和终态落盘；过程进度主要在内存。重启将遗留待执行/运行任务标为中断失败；默认保留最多 50 条，新建时淘汰旧记录。 |
| `pools/` | `<pool_id>.parquet`：标的池成员及 `as_of` 快照日期。 | [pools.py](../backend/app/tickflow/pools.py) 缺缓存或显式刷新时拉取；自选池另读 `user_data/watchlist.parquet`。重拉得到新成分，不能当作历史成分库。 |
| `mainline_history/` | `part.parquet`：按日期、概念/行业聚合的涨停梯队、主线分与排名，保留排名靠前的主线。 | [market_mainline.py](../backend/app/services/market_mainline.py) 由启用的盘后市场环境管道增量计算，环境接口也可回填。重算依赖 enriched 和成分；用当前成分快照回看历史，不保证还原旧归属。 |
| `regime_history/` | `part.parquet`：市场宽度、涨跌停、赚钱/投机/抗跌/趋势评分、市场状态与阶段。 | [regime_builder.py](../backend/app/services/regime_builder.py) 计算，供环境页、回测过滤和研究读取。与主线共用默认关闭的 `pipeline_regime_enabled` 管道开关；缺输入时不能完整回算。 |
| `paper/` | `accounts/<account_id>/` 下保存 `account.json`、`orders/`、`fills.jsonl`、`positions.json`、`nav/`、`auto_rules/`。 | [paper.py](../backend/app/strategy/paper.py) 与 [paper_auto.py](../backend/app/strategy/paper_auto.py) 在模拟账户操作、撮合、结算和自动执行时更新。成交日志是追加式事实源；账户历史不能由行情恢复。旧单账户布局按条件迁移到 `accounts/default/`。 |
| `etf_observation/` | `shares.csv`、`nav.csv`、`daily.csv`、`summary.csv`、`reconciliation.csv`、`manifest.json` 及拆分校验材料；`raw/` 存 JSON/Gzip/HTML 原始响应，失败产物可进入 `incomplete/`。 | [ETF 观察采集与发布](../backend/app/custom/etf_observation_data/ledger.py) 保存份额、未复权单位净值和申赎估算证据。页面通过 [read_model.py](../backend/app/custom/etf_observation_data/read_model.py) 读取发布到 `ext_data` 的时序表，不直接读这里的 CSV。2026-11-04 及以前为历史回补；之后由 [daily.py](../backend/app/custom/etf_observation_data/daily.py) 每天 08:30、21:30（北京时间）定时追加：份额与交叉核对净值都齐备的交易日才发布，已发布日期只核对不改写，超出各基金口径核对期的行只填场内等值。定时任务只延续已有发布，不会在没有发布的机器上自动从头回补；内置交易日历到期后停止追加。发布前的数据备份可放在同级 `etf_observation_backup_*/`，确认无误后可删除。原始响应建议保留，历史回补日期不代表当时盘中已公开。 |
| `research/` | 挖掘、守护运行以及本地专项研究输出，子目录见下表。 | 正式运行由任务系统更新；本地研究输出随相应脚本产生。新运行不能替代旧参数、事件和版本证据。 |
| `market_game_validation/` | 本地行情联调、覆盖审计和收益验证脚本及 JSON/Markdown 报告。 | 当前应用未找到固定读写入口，属于手工验证留存；正式博弈报告位于 `user_data/market_game/`。不能承诺清理后自动恢复。 |
| `cache/` | `fuyao/<type>__<release>.parquet` 为整库下载缓存；另含 ETF 页面和测试验证产物。 | [Fuyao provider](../backend/app/plugins/fuyao/provider.py) 下载并复用发布版本，清理同类型旧缓存。删除后可能需联网重下，并失去离线回退；本地测试子目录没有统一运行时保留契约。 |
| `ai_cache/` | 预留目录，当前未找到生产业务结果写入。 | `DataStore` 预建，数据管理统计/清理；不能按名字认定这里保存了 AI 分析结果。若有本地文件，先辨明来源。 |
| `screener_results/` | 预留目录，当前仅找到初始化、统计、清理引用。 | 不能把它当成当前筛选结果的实际持久化位置。空目录可重建，非空时需确认文件来源。 |

`cache/` 的本地验证子目录包括 `etf_observation_ui_v1/`、`etf_observation_test_runs_*/`、`pytest_etf_observation_*/` 和 `pytest_etf_page_*/`。它们与 Fuyao 发布缓存不同，清理前先确认相关测试已结束且不再需要保留验证证据。

### research 子目录

| 子目录模式 | 内容、更新与读取 |
| --- | --- |
| `mining/runs/<run_id>/` | 正式挖掘任务：`manifest.json` 请求、指纹、状态和产物登记，`summary.json` 汇总，`events.jsonl` 有界事件，以及按需生成的因子、相关性、候选和折叠验证 Parquet。由 [mining_jobs.py](../backend/app/services/mining_jobs.py) 保存，研究页面读取。 |
| `guardian/runs/<run_id>/` | 正式策略守护任务输出，复用运行仓库并通过命名空间隔离；与每策略的长期基线档案分开。 |
| `first_board_daily_baseline/<run>/` | 本地首板日线基线研究留存，未找到以此目录名固定读写的应用入口。 |
| `first_board_ideal/` | 本地首板理想成交研究 JSON/TXT 等输出；具体输出位置由研究命令选择，不是核心服务固定存储契约。 |
| `huichun/<batch>/` | 历史回春数据修复、因子重建、回测及验收研究材料；当前回春服务状态在 `user_data/huichun/`。 |
| `huichun_a0_<date>/<run>/` | 本地回春 A0 专项审计、脚本、输出与报告；未找到该名称的固定应用读写契约。 |

## 5. 用户配置与策略

| 目录 | 文件与内容 | 更新、读取与删除影响 |
| --- | --- | --- |
| `strategies/` | `custom/`、`ai/`、`composite/` 下的 Python 策略源码；本次实际存在 `custom/`。 | 编辑、导入、AI 生成或组合发布写入，启动和回测 worker 加载；删除会使策略、监控、回测引用失效。参数覆盖另存于 `user_data/strategy_overrides/`。 |
| `analysis_menus/` | `<menu_id>.json`：声明式分析菜单、模板、数据源绑定、列配置、排序与可见性。 | [analysis.py](../backend/app/api/analysis.py) 在新增、编辑、重排时写入，分析菜单接口读取；删除丢失用户展示设置，不能从行情重建。 |
| `user_data/` | 用户配置、凭据、定义、记录及部分缓存的集合。 | 各功能服务维护，不能整体当作缓存。下面按主要子目录和根文件说明。 |

策略入口：[strategy.py](../backend/app/api/strategy.py)、[启动加载](../backend/app/main.py)、[回测 worker](../backend/app/backtest/worker.py)。

### user_data 子目录

| 子目录 | 数据、读写来源与保留要求 |
| --- | --- |
| `custom_factors/` | `<id>.json` 自定义和复合因子定义，编辑/导入时由 [factors/store.py](../backend/app/factors/store.py) 保存，注册和计算时读取；属于用户定义。 |
| `custom_signals/` | `<id>.json` 日线/分钟信号定义及可能存在的初始化标记，由 [custom_signals.py](../backend/app/strategy/custom_signals.py) 维护；删除会影响策略和监控引用。 |
| `extensions/` | 当前 `paper_execution/<account_id>.json` 为模拟执行扩展的账户配置/运行状态，由 [扩展 service.py](../backend/app/custom/paper_execution/service.py) 管理。成交台账仍在 `paper/accounts/`，这里也不是后端扩展安装包目录。 |
| `first_board/` | 首板参数 `config.json`、按日 `events/`、前向观测 `observations/`、雷达 `radar/v1/hot/`、交易验证 `trade-validation/` 及按需 `research/`。由 [first_board.py](../backend/app/services/first_board.py)、[观测服务](../backend/app/services/first_board_observations.py)、[雷达服务](../backend/app/services/first_board_radar.py) 更新；盘中样本不会事后补写错过时点，不能从日线完整还原。 |
| `holding_analysis/` | `state.json`：持仓研究画像、报告和采纳计划，由 [holding_analysis_store.py](../backend/app/services/holding_analysis_store.py) 保存；最多保留 100 条报告，计划内保留采纳证据。此存储不直接修改实际库存或成交记录。 |
| `huichun/` | 参数及版本、`snapshot.json` 任务状态、扫描/研究结果、`tracking.json` 跟踪与 `market_calendar.json` 日历等，由 [huichun_mode.py](../backend/app/services/huichun_mode.py) 随扫描、研究、用户操作更新；配置及跟踪不可从行情恢复。 |
| `lots/` | `lot_*.json` 买入批次、止盈止损与到期提醒输入，由 [lots.py](../backend/app/strategy/lots.py) 保存并派生监控规则；不是完整会计台账。 |
| `market_game/` | `mg_<id>.json` 不可覆盖的博弈报告和当时快照，另有评估、解释、资金及模型旁文件，由 [market_game_reports.py](../backend/app/services/market_game_reports.py) 保存。无自动保留上限，删除会损失预测验真证据。`calibration/cal_<时间>_<随机>.json` 是离线校准 `--archive` 追加的规则可信度摘要，由 [market_game_credibility.py](../backend/app/services/market_game_credibility.py) 写入、页面只读最新一份；可重新运行校准生成，删除只会让页面显示“尚无校准归档”。 |
| `monitor_rules/` | `<rule_id>.json` 价格、日期、信号监控规则，由 [monitor_rules.py](../backend/app/strategy/monitor_rules.py) 在用户编辑或规则派生时更新；删除改变提醒行为。 |
| `position_t/` | `ledger.sqlite3` 做 T 的账户、持仓、信号、计划、执行、订单、通知和事件记录，由 [position_t_store.py](../backend/app/services/position_t_store.py) 事务更新；操作台账不可从行情重建。 |
| `removed_integrations/` | 本地按集成名与批次留存的移除材料；当前未找到统一读写或自动恢复契约。保留到确认不再需要，不能仅凭名称承诺可一键恢复。 |
| `strategy_guardian/` | `<strategy_id>.json` 策略基线、复验历史与状态，`combinations.json` 组合建议，由 [guardian_store.py](../backend/app/services/guardian_store.py) 维护；删除丢失守护历史，不等于删除策略源码。 |
| `strategy_overrides/` | `<strategy_id>.json` 策略参数覆盖，由 [strategy/config.py](../backend/app/strategy/config.py) 保存、运行时合并；删除会回退默认参数并改变运行行为。 |

以上为当前 13 个子目录。代码还支持按需创建 `position_policies/` 保存自定义仓位策略源码，见 [position_policies.py](../backend/app/backtest/position_policies.py)；启用其他功能后也可能有新增目录，需按第 11 节更新说明。

### user_data 根文件

| 文件或类别 | 内容与维护方式 |
| --- | --- |
| `preferences.json`、`secrets.json`、`auth.json`、`api_tokens.json` | 偏好、数据源/AI 等凭据、登录配置和开放接口访问凭证。通过相应设置功能维护，需私密备份。 |
| `watchlist.parquet`、`watchlist_groups.json` | 自选标的和分组，由自选管理操作更新，不等于 `pools/` 的上游标的池缓存。 |
| `alerts.jsonl`、`assistant_sessions.json` | 告警历史与助手会话，随事件/会话更新；包含历史上下文，不能由行情自动还原。 |
| `research_candidates.json` | 挖掘候选及其研究状态，不等于已经发布的策略。 |
| 策略结果/计时缓存、任务注册表 | 各服务用于复用结果、记录耗时和调度任务的状态；应由对应功能管理，不与用户定义一并清理。 |
| `integrity_report.json`、`daily_integrity_state.json` | 自检报告和跨重启保留的异常/待重算状态；与 `integrity_backups/`、修复流程配套，不应手工删除以强行放行异常数据。 |
| `ai_market_recaps.json` | AI 复盘归档(最多 20 篇)：正文、数据快照(含剧本核验、持仓联动)和结构化明日剧本 `plan`，由 [market_recap_reports.py](../backend/app/services/market_recap_reports.py) 在生成复盘后写入。下一篇复盘读取上一篇的剧本做系统核验、读取历史核验算命中率；删除后历史复盘与命中率统计丢失，不能从行情重建。 |
| `market_recap_intraday.json` | 10:00 盘中剧本核验结果(最多 20 条)：开盘半小时成交额与倾向情景，由 [market_recap_intraday.py](../backend/app/services/market_recap_intraday.py) 在定时任务(偏好 `intraday_plan_check_enabled`)或复盘页手动核验时写入，复盘页读取当日最新一条。删除只影响页面显示历史结果，可再次核验生成。 |

上述文件与备份范围的对应关系见 [config_backup.py](../backend/app/services/config_backup.py)；自检状态说明见 [配置说明](configuration.md)。

## 6. 缓存、日志与修复暂存

| 目录 | 文件与内容 | 生命周期与清理后果 |
| --- | --- | --- |
| `.backtest_matrix_cache/` | `v<version>-<fingerprint>/manifest.json`、`matrix.bin` 以及根下 `.axes-v<version>-<fingerprint>.json`：回测矩阵和日期/标的轴索引。 | 回测或预热生成，按数据代次、覆盖范围和字段校验；有容量/代次清理并保护使用中的矩阵。无任务占用后可重建，首次回测会变慢。 |
| `integrity_backups/` | `<batch>/<table>/date=<date>/*.parquet`：自检修补或精准日线替换前的原文件。 | 修补前保存；无已确认的自动保留期清理。删除后失去回退材料，修复后的数据不能还原这些旧文件。 |
| `.daily_repair_staging/` | `<batch>/<daily_table>/date=<date>/part.parquet`：精准修补取得的新日线。 | 全部校验通过后才改正式数据，退出时清理当次暂存；当前为空。活跃修补期间不可删。 |
| `.staging/` | `enriched_rebuild/<id>/batch-<n>.parquet`：全量 enriched 重建的分批结果。 | 合并到正式日期分区后清理，成功/失败/取消均有清理路径；下次重建会清理超过 24 小时的遗留暂存。不要干扰活跃批次。 |
| `logs/` | 后端、桌面和启动器日志；可有 `backend.log`、前后端 `out/err` 重定向日志。 | 后端主日志按 10 MB 轮转、保留 3 个轮转文件；其他输出是否轮转由启动方式决定。清理只损失排障历史，应先停止相应写入。 |
| `.codex-uv-cache/` | 当前包含 `interpreter-v4/`、`sdists-v9/`、锁与缓存标记，为本地 uv 工具缓存结构。 | 未在项目源码中找到这个具体目录名的固定创建入口；不属于业务数据契约。无工具任务占用时可按工具缓存处理，重建可能需要网络。 |

实现入口：[engine.py](../backend/app/backtest/engine.py)、[matrix.py](../backend/app/backtest/matrix.py)、[integrity_check.py](../backend/app/services/integrity_check.py)、[repair_daily.py](../backend/app/services/repair_daily.py)、[pipeline.py](../backend/app/indicators/pipeline.py)、[logfile.py](../backend/app/logfile.py)。`cache/` 内既有数据源缓存也有本地测试产物，见第 4 节，不能按目录名一概清空。

## 7. 本地测试目录

下列 41 个目录共享同一存储性质：内部为 `test_<case><n>/` 等测试工作目录，包含测试生成的 `data/`、日历 JSON、Parquet、策略及配置样本。它们是本机测试产物，**不是生产数据目录**；未找到这些顶层目录名的固定应用创建入口，不据名称推断具体执行命令。

确认测试进程已结束、失败样本和审查证据不再需要后，可决定是否清理。测试可以重新生成所需样本，但不保证重现这次运行的现场。

| 目录（均为测试临时数据与配置样本） |
| --- |
| `.test-a0-backtest-regression/` |
| `.test-a0-score-after/` |
| `.test-huichun-backtest/` |
| `.test-huichun-backtest-final/` |
| `.test-huichun-backtest-original/` |
| `.test-huichun-review-score/` |
| `.test-huichun-adapter/` |
| `.test-huichun-contracts/` |
| `.test-huichun-registration/` |
| `.test-huichun-mon-contract/` |
| `.test-huichun-reader/` |
| `.test-huichun-reader-baseline/` |
| `.test-huichun-reader-baseline-all/` |
| `.test-huichun-reader-failure/` |
| `.test-huichun-reader-isolated-00/` |
| `.test-huichun-reader-isolated-01/` |
| `.test-huichun-reader-isolated-02/` |
| `.test-huichun-reader-isolated-03/` |
| `.test-huichun-reader-isolated-04/` |
| `.test-huichun-reader-isolated-05/` |
| `.test-huichun-reader-isolated-06/` |
| `.test-huichun-reader-isolated-07/` |
| `.test-huichun-reader-isolated-08/` |
| `.test-huichun-reader-isolated-09/` |
| `.test-huichun-reader-isolated-10/` |
| `.test-huichun-reader-isolated-11/` |
| `.test-huichun-reader-isolated-12/` |
| `.test-huichun-reader-isolated-13/` |
| `.test-huichun-reader-isolated-14/` |
| `.test-huichun-reader-monitor/` |
| `.test-huichun-reader-pure/` |
| `.test-huichun-reader-research/` |
| `.test-huichun-reader-research-unit/` |
| `.test-huichun-reader-runs/` |
| `.test-huichun-reader-single/` |
| `.test-huichun-reader-stale-head/` |
| `.test-huichun-reader-stale-isolated/` |
| `.test-huichun-final-base/` |
| `.test-huichun-regression/` |
| `.test-huichun-review/` |
| `.test-huichun-review-fixed/` |

清单不表示相关测试已经通过。维护时可从 [test_huichun_data.py](../backend/tests/test_huichun_data.py) 等 `tmp_path` 用例核对生成方式；不将测试样本复制进正式行情分区。

## 8. 根目录中的辅助文件

这些文件不是文件夹，但会影响数据一致性，迁移时应一并关注。

| 文件模式 | 保存内容、更新来源与注意事项 |
| --- | --- |
| `capabilities.json` | TickFlow 能力探测缓存及版本信息，由 [policy.py](../backend/app/tickflow/policy.py) 维护；不是所有自定义数据源的能力路由总表。失效后需要重新探测。 |
| `.matrix_generation_<asset>.json` | 股票、ETF、指数 enriched 数据的发布状态和代次，用于识别旧缓存、发布中或失败状态；由 [enriched_generation.py](../backend/app/enriched_generation.py) 维护，不应手工改成“就绪”。 |
| `.matrix_generation_<asset>.lock` | enriched 发布的跨进程互斥锁文件。文件存在不等于锁正被持有，不要靠删除文件来解除运行中的锁。 |
| `.mining_process.lock` | 挖掘任务的跨进程锁及持有者诊断信息，由 [mining_process_lock.py](../backend/app/services/mining_process_lock.py) 维护。 |
| `.integrity_repair_backoff.json` | 按异常起始日期记录自动修复尝试时间，当前冷却期为 6 小时；手工修补不走此退避限制，见 [data_integrity.py](../backend/app/services/data_integrity.py)。 |
| `..matrix_generation_<asset>.json.<id>.tmp`、其他 `*.tmp` | 原子写入过程的中间文件；可能是写入中，也可能是中断遗留，不能作为正式数据读取。 |
| `backend.log` | 根目录下的后端输出文件；当前标准日志目录是 `logs/`。其实际生成方式取决于启动或重定向命令，不能仅凭文件名判断来源。 |

## 9. 按需出现的其他目录

以下目录在当前清单中未出现，但已有实现；新安装、功能启用或升级后可能产生。

| 目录模式 | 用途与维护边界 |
| --- | --- |
| `extensions/<id>/versions/<version>/` | 外部后端扩展包及配套页面产物；由 [ExtensionPackageStore](../backend/app/extensions/packages.py) 安装和管理。与 `user_data/extensions/` 的扩展业务状态不同，删除会影响扩展加载。 |
| `upgrade_guard/live/`、`upgrade_guard/reports/` | 安装包升级保护的程序修改备份和版本差异报告；由 [upgrade_guard.py](../backend/app/services/upgrade_guard.py) 维护。源码环境无发布清单时停用，不能当作普通缓存清理。 |
| `upgrade_backups/data-dir-v1/<id>/` | 旧数据目录恢复时的配置备份与完成标记；由 [data_dir_upgrade.py](../backend/app/services/data_dir_upgrade.py) 维护。可能含凭据；删除完成标记可能影响后续恢复判定。 |

此表不是所有插件可能创建的路径白名单。出现新目录时先查写入者，再补充对应分类；未能确认来源的目录标为“来源待核实”，不要按名字推断为可删除缓存。

## 10. 备份与清理原则

1. **迁移优先保留完整数据根目录。** 停止写入任务后复制，避免行情分区、发布代次、配置和业务记录来自不同时间点。配置导出只覆盖其声明的范围，不等于完整 `data/` 备份，见 [config_backup.py](../backend/app/services/config_backup.py)。
2. **优先备份用户定义与历史证据。** `user_data/`、`strategies/`、`pools/`、`paper/`、安装的插件和页面、研究与回测档案不能从行情下载接口恢复。
3. **行情可以重拉，但历史不一定还能补齐。** 原始分钟、盘口、旧数据源快照、历史成分和公告可得性都可能受上游窗口或权限限制；重算 enriched 前须确认原始行情、复权因子及历史股本齐全。
4. **清理由对应功能入口处理。** 手工删除 Parquet 不会自动维护所有内存缓存、发布代次和衍生结果。自检修补应走“数据 → 自检/修补”，不要靠清空目录解决状态不一致。
5. **临时文件先确认无人使用。** 任务取消、失败或进程退出不一定说明每个残留目录都可丢弃；检查修复备份与诊断证据后，再决定是否清理。本文不执行删除操作。

凭据、日志、账户状态、用户策略、备份及真实数据样本均不应提交到 Git 或直接公开分享。

## 11. 持续更新规则

本文件采用**与代码变更同步维护**的方式，不依赖定时扫描自动推断业务语义。要求已写入 [贡献规范第 8 节](../CONTRIBUTING.md)：涉及目录新增、重命名、删除，或文件结构、数据口径、更新触发和保留策略变化时，在同一变更中更新本文。

维护步骤：

1. 枚举实际数据根目录的一级目录，包含隐藏目录；日期、任务、账户、插件等动态子目录按模式描述。
2. 追踪写入函数、读取者与相关测试，核实格式、关键字段、单位和更新时机；目录存在只作为盘点证据。
3. 更新相应表格的用途、文件结构、触发方式、消费者和重建条件，并维护源码链接。
4. 将新出现的测试、工具或临时目录与正式契约分开；不把个人配置值、实际持仓或凭据写进说明。
5. 检查目录覆盖和文档链接，更新“最近核对”日期及下方维护记录。数据量、文件数和磁盘大小属于实时状态，不作为固定契约写入本文。

在项目根目录运行以下 PowerShell 片段，可只读检查一级目录名是否已出现在说明中；无输出表示名称覆盖，没有证明文件内容或业务口径正确：

```powershell
$dataDoc = Get-Content -LiteralPath ./docs/data-directory.md -Raw -Encoding utf8
Get-ChildItem -LiteralPath ./data -Directory -Force |
    Where-Object { -not $dataDoc.Contains(('`' + $_.Name + '/`')) } |
    Select-Object -ExpandProperty Name
```

若实际使用其他 `DATA_DIR`，替换命令中的 `./data`。动态分区不逐项进入清单；新增目录应有独立说明，或明确归入有用途说明的模式组。

| 核对日期 | 维护内容 |
| --- | --- |
| 2026-11-08 | 建立目录说明，区分正式存储、衍生结果、用户定义、工具与测试产物，并加入同步维护要求。 |
