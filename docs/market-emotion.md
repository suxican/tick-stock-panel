# 市场情绪：功能、数据契约与迁移边界

市场情绪页面位于 `/market-emotion`。情绪指标、市场统计、直播与关联股票来自开盘啦；直播中的上证指数分时图与个股详情复用系统配置的数据源。不同来源分别标注，缺失数值保留为空。

## 1. can_main → can_new 迁移

本次功能来源为 `can_main` 的 `5f435ad3`，目标基线为 `can_new` 的 `ab41e68c`。迁移按功能调用链接入重构后的宿主，不合并整个旧分支。

- **L2 页面与路由接入**：`frontend/src/custom/kaipanla/extension.tsx` 由现有扩展加载器发现，注册菜单及路由。后端由 `app.custom.kaipanla` 的 `setup()` 注册独立路由。核心 `router.tsx`、`Layout.tsx`、后端 `main.py` 不需要增加专用分支。
- **L3 公共契约小增量**：统一 API 客户端增加两个请求方法和返回类型；集中查询键增加情绪、股票列表及可选的分钟源维度；共享分时图增加可选标注与缺口连线参数；指数卡片增加可选静态展示。既有调用方保留默认行为。
- **适配新版图表依赖**：情绪历史图使用原生 ECharts，随容器调整尺寸并在卸载时释放，不重新引入新版已移除的 `echarts-for-react`。
- **迁移范围**：仅接入本菜单使用的 11 个匿名数据集。不迁入旧分支的开盘啦扩展表预设、落盘、自动拉取任务或 AI 复盘摘要。

后续合并上游时重点复核 `api.ts`、`queryKeys.ts`、`EChartsIntraday.tsx` 和 `dashboard/widgets.tsx` 的增量接口，以及扩展 API 版本。

## 2. 页面行为

- 进入菜单默认查询**北京时间当天**，不受浏览器本地时区或“最近交易日”旧缓存影响。显式日期仅展示该日数据，日期不匹配时拒绝展示。
- “最新交易日”按钮不传日期，由情绪历史中的真实来源日期确定最近可用快照；假期和历史查询不把旧数据重新标记为当天。
- 数据分析包括核心指数、综合强度、涨跌停家数、连板高度、回撤家数、破板率、情绪历史、量能对比、涨跌分布、连板梯队、晋级与溢价、回撤名单、权重表现、大盘直播及缺口说明。
- 股票列表按需加载涨停榜和**今日首板破板**榜，本地分页。跌停和竞价名单保留不可用提示。
- 回撤、权重、涨停/破板以及直播中的有效股票可以打开共享个股详情。切股范围为完整当前榜单或当前消息的关联股票；切换日期或主标签清空预览。详情行情沿用共享组件日期规则，不把历史情绪日期冒充详情行情日期。
- 同日期刷新失败保留上次有效分项并明确提示；切换日期不沿用上一日期的数据。历史日不轮询；当天与最新日期发现按既有页面刷新条件每分钟更新。

## 3. HTTP 与数据来源

| 入口 | 用途 |
| --- | --- |
| `GET /api/market-recap/kaipanla/market-emotion?date=YYYY-MM-DD&force=true` | 读取指定日聚合；省略 `date` 时查来源最新日期，省略 `force` 时复用有效缓存 |
| `POST /api/market-recap/kaipanla/query/{dataset_id}` | 按日期获取涨停或首板破板名单；请求体包含 `date`、`parameters`、`force` |
| `GET /api/index/minute`、`GET /api/index/daily` | 复用系统指数数据，用于直播分时与前一实际交易日收盘基准 |

市场情绪聚合保留 `requested_date`、`date`、`previous_date`、`fetched_at`、`state`、`datasets`、`history` 和 `missing` 字段。分项失败独立隔离，整体状态区分 `ok`、`partial`、`empty`、`error`。客户端不接受登录凭据，公开名单入口仅开放本菜单使用的两类股票列表。

| 数据集 | 业务含义与约束 |
| --- | --- |
| `ext_kpl_emotion` | 来源综合强度、连板高度、回撤家数及真实日期历史；最多展示 60 个历史点 |
| `ext_kpl_indices` | 当日/历史指数快照，指数代码集合复用系统核心指数定义 |
| `ext_kpl_capacity` | 量能单位为万，显示亿元时除以 10000；预测文本与实际量能分别展示 |
| `ext_kpl_limit_counts` | 来源涨跌停家数；不与直播的全部/实际涨跌停统计混用 |
| `ext_kpl_limit_ladder_counts` | 首板、二板、三板、四板、五板及以上家数，保留来源合并分档 |
| `ext_kpl_limit_performance` | 二板、三板、最高板晋级率、破板率及昨日涨停表现，原始百分数口径 |
| `ext_kpl_withdrawal` | 大幅回撤股票；来源未定义入选阈值，不推导新筛选规则 |
| `ext_kpl_weight` | 权重板块上涨/下跌前三及来源龙头，不代表全板块排名 |
| `ext_kpl_live` | 同日直播、完整统计快照与明确关联股票；每条消息按北京时间戳再次核验日期 |
| `ext_kpl_limit_up` | 完整拉取一至五档涨停名单和全部分页 |
| `ext_kpl_broken_limits` | `PidType=1` 为今日首板破板；其他分档不能称为全板位今日炸板 |

## 4. 日期、单位与缺口

数据日期、来源时间和采集时间分别保留。`fetched_at` 只是请求完成时间；`date_origin=request_parameter` 表示来源未直接返回日期。情绪多日记录保留各行日期，拒绝日期冲突并排除目标日之后的数据。前日比较取来源上一记录日，不直接减一个自然日。

开盘啦百分比使用百分数值，例如 `3.66` 显示 `3.66%`。金额按字段声明单位转换，市场量能的“万”除以 10000 转为亿，分时成交额的“元”除以 100000000 转为亿。缺失、零、失败分别处理，不按数值大小猜单位。

直播涨跌分布必须具有目标日时间戳、21 个分档及完整非负整数统计。分档边界和市场覆盖范围未由来源明确，不能把 `0` 档改称平盘或把 `±10` 档改称超过 10%。缺少独立封板率、四板独立晋级率、连板强度、活跃股走势、量能分钟曲线、全板位炸板及跌停名单时保留说明，不使用其他字段冒充。

## 5. 分时标注与缓存

直播分时按能力矩阵的 `minute.usable` 门控，明确显示有效分钟源。分钟结果必须匹配上证代码和所选日期，缺口不插值。昨收从此前 60 个自然日范围内的真实日 K 记录选取；缺少昨收时只显示点位。

直播强弱标注从同日评论的明确板块、指数或短线情绪原文提取，每条最多两个。走强/修复用红色、走弱/回落用绿色，其余用灰点；预测、条件、否定、历史回顾和方向冲突不作强弱判断。标注只放在同分钟真实价格上，点击定位原消息；来源文字作为普通文本处理。

- 后端使用有界内存缓存：当天 60 秒、历史 3600 秒；键包含数据集、日期、参数和历史模式，相同键请求并发合并。
- 前端情绪、名单按日期及类型隔离；分钟键包含可选 provider，旧的日期前缀仍能精确使其失效。
- 页面数据不加入本地持久化查询白名单，不改变系统行情 SSE 范围；没有新增文件数据、配置迁移或后台任务。
- 共享分时图的 `annotations`、`onAnnotationClick`、`connectNulls` 均为可选；未传入的既有页面保留原行为。

## 6. 验证范围

后端定向回归覆盖来源日期、假期、单位、分页、历史缓存隔离、并发、分项失败及扩展路由注册；前端覆盖日期选择与刷新、名单/消息分页、个股导航、能力门控、分钟源切换、标注安全和原生图表生命周期。构建使用 `pnpm build`。

本次迁移验证：后端下列 5 个测试文件共 113 项通过；前端菜单、共享组件/API 和现有页面兼容性共 181 项通过；`pnpm build`、新增后端文件 Ruff 检查和 `git diff --check` 通过。可复现命令：

```bash
# backend/
uv run --frozen --extra dev pytest tests/test_market_emotion.py tests/test_kaipanla_history.py tests/test_kaipanla_catalog.py tests/test_kaipanla_pull.py tests/test_extensions.py -q
uv run --frozen --extra dev ruff check app/custom/kaipanla.py app/data_providers/kaipanla.py app/data_providers/kaipanla_catalog.py app/services/market_emotion.py tests/test_market_emotion.py tests/test_kaipanla_history.py tests/test_kaipanla_catalog.py tests/test_kaipanla_pull.py

# frontend/（三组分别运行）
pnpm exec vitest run src/custom/kaipanla
pnpm exec vitest run src/lib/marketEmotionApi.test.ts src/components/dashboard/IndexTicker.test.tsx src/components/EChartsIntraday.test.tsx
pnpm exec vitest run src/extensions/runtime.test.ts src/lib/navOrder.test.ts src/pages/Indices.test.tsx src/components/dashboard/useDashboardLayout.test.tsx
pnpm build
```

2026-10-21 本地联调使用独立临时数据目录和构建产物，关闭后台调度；实际匿名接口返回当日 `partial`、前一来源日期 `2026-10-20`，可读取情绪、4 个指数、60 日历史、47 条直播、50 条涨停和 17 条首板破板记录。当天量能、涨跌停总数、涨停表现、回撤和权重分项不可用；这 5 项的请求 URL、参数及归一化契约与 `can_main` 一致。浏览器已检查当天默认、历史切换时清空旧数据、名单和深浅主题。隔离环境未配置分钟源，分时图按能力矩阵显示不可用；真实分钟行情和个股详情完整取数未在该环境验证。

固定测试样本证明解析与交互契约，不保证第三方匿名接口始终可用。运行时仍按分项状态显示缺失与错误；接口发布延迟和供应商结构变化需要以真实请求结果复核。
