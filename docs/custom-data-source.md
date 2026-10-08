# 自定义数据源接入

本项目默认使用 TickFlow。自定义数据源是一个可选扩展: 外部 HTTP 服务负责取数和整理, 本项目只把返回结果映射成内部标准字段, 然后复用现有存储、指标、enriched、策略和前端展示逻辑。

## 支持范围

当前自定义源支持六类数据:

| 数据集 | 配置名 | 说明 |
| --- | --- | --- |
| 日K | `daily` | 批量返回一组股票在指定区间内的日K |
| 除权因子 | `adj_factor` | 批量返回一组股票的复权因子 |
| 实时行情 | `realtime` | 返回全市场快照,用于盘中 enriched 增量计算 |
| 分钟K | `minute` | 返回 1m 分钟K(需映射出 symbol / datetime / OHLC / 量额) |
| 全量分钟 | `full_minute` | 与 `minute` 同形;声明后可被路由为「全量分钟」生效源,内置服务盘中按当日窗口全市场批量落盘(仅修复轮语义,节奏下限 60s) |
| 财务数据 | `financial` | 一个配置覆盖全部财务表,请求时把表名作为参数传给上游;字段由数据源决定,仅需映射出 symbol |

深度盘口(depth5)暂无数据集契约,仍由 TickFlow 提供。

`full_minute` 声明式源只提供修复轮(当日窗口批量);廉价增量端点
(`get_intraday_latest`)是 Python 插件契约,见
[plugin-development.md](./plugin-development.md)。

## 配置位置

把 YAML 放到运行数据目录下:

```text
data/data_sources/*.yaml
```

Dev 模式下，默认位置是项目根目录的 `data/`；Docker 部署中，项目的 `data/` 会挂载为容器内的 `/app/data`。可通过 `DATA_DIR` 覆盖。

修改 YAML 后可在「设置 -> 数据源」点击「重新加载」,或调用:

```bash
curl -X POST http://127.0.0.1:3018/api/settings/data-sources/reload
```

## 最小 YAML

```yaml
name: mock_source
display_name: "Mock 自定义数据源"
auth:
  type: none

datasets:
  daily:
    url: http://127.0.0.1:3021/daily
    method: POST
    batch: 100
    rpm: 200
    response_path: data
    field_map:
      ts_code: symbol
      trade_date: date
      open: open
      high: high
      low: low
      close: close
      vol: volume
      amt: amount
    transforms:
      date: "parse_date(value, '%Y-%m-%d')"

  adj_factor:
    url: http://127.0.0.1:3021/adj_factor
    method: POST
    batch: 100
    rpm: 200
    response_path: data
    field_map:
      ts_code: symbol
      trade_date: trade_date
      factor: ex_factor
    transforms:
      trade_date: "parse_date(value, '%Y-%m-%d')"

  realtime:
    url: http://127.0.0.1:3021/realtime
    method: GET
    rpm: 60
    response_path: data
    field_map:
      ts_code: symbol
      name: name
      last: last_price
      pre_close: prev_close
      open: open
      high: high
      low: low
      vol: volume
      amt: amount
      pct: change_pct
      amount_change: change_amount
      amplitude: amplitude
      turnover: turnover_rate
```

## 字段契约

### daily 必填

| 内部字段 | 含义 |
| --- | --- |
| `symbol` | 标准代码,如 `000001.SZ` |
| `date` | 交易日 |
| `open` / `high` / `low` / `close` | 不复权 OHLC |
| `volume` | 成交量 |
| `amount` | 成交额 |

### adj_factor 必填

| 内部字段 | 含义 |
| --- | --- |
| `symbol` | 标准代码 |
| `trade_date` | 除权日期 |
| `ex_factor` | 复权因子 |

可选明细列 (提供即落库, 缺省为空): `dividend` 每股现金分红、`bonus` 每股送转比例、
`allot`/`allot_price` 配股比例与配股价、`prev_close` 除权前收盘 — 供等差显示投影
与全精度因子链重建。

### realtime 必填

| 内部字段 | 含义 |
| --- | --- |
| `symbol` | 标准代码 |
| `last_price` | 最新价 |
| `prev_close` | 昨收 |
| `open` / `high` / `low` | 当日 OHLC |
| `volume` | 成交量 |

建议实时接口额外提供 `amount`、`change_pct`、`change_amount`、`amplitude`、`turnover_rate`、`name`。缺失时部分字段会由 pipeline 回算,但精度取决于可用输入。

`change_pct`、`amplitude`、`turnover_rate` 统一使用小数制,例如 `0.0366` 表示 `3.66%`。百分制单位必须在 realtime 数据集上**显式声明**,不做数值猜测(数值无法区分两种单位:`0.05` 既可能是 0.05% 也可能是 5%):

```yaml
datasets:
  realtime:
    url: https://api.example.com/snapshot
    pct_unit: percent   # 接口返回 3.66 表示 3.66%;小数制源声明 decimal 或省略
```

处理规则:

| 声明 | 行为 |
| --- | --- |
| `pct_unit: percent` | `change_pct` / `amplitude` / `turnover_rate` 无条件 `/100` |
| `pct_unit: decimal` | 三列原样透传 |
| 未声明 | `change_pct` 按截面中位数归一(A 股涨跌停 30% 上限使两种单位物理可分);`amplitude` / `turnover_rate` **置 `None`** 交由 pipeline 按价格与股本口径重算,并记录 WARNING |
| 列已配置 `transforms` | 视为用户已接管该列单位,原样透传 |

## 请求约定

- `daily` / `adj_factor` 会按 `batch` 切分 symbols。
- POST 请求会发送 JSON body: `symbols`、`start_time`、`end_time`。
- GET 请求会发送 query 参数: `symbols=000001.SZ,600000.SH`。
- `realtime` 必须是全市场快照接口,不支持逐个 symbol 拉实时行情。

可通过这些字段改参数名:

```yaml
symbols_param: symbols
start_param: start_time
end_param: end_time
```

分钟数据源如果需要区分资产类型或周期，可继续配置：

```yaml
asset_type_param: asset_type
freq_param: period
```

配置后，分钟请求会分别传入 `stock` / `etf` / `index` 和 `1m`；留空时不向上游发送这两个参数，以兼容已有数据源。

### 请求超时

每个数据集可单独配置请求超时（秒），默认 30：

```yaml
timeout: 60
```

留空或省略时用默认 30 秒，可配置范围为大于 0 且不超过 300 秒；该值对数据同步与「试拉测试」均生效。在设置页编辑数据源时可在「超时」输入框修改（与 批量 / RPM / 响应路径 同行）。「试拉测试」直接使用当前表单内容，新建数据源或尚未保存的修改也可测试。

## 鉴权

支持三种简单鉴权:

```yaml
auth:
  type: bearer
  token_env: MY_DATA_TOKEN
```

```yaml
auth:
  type: header
  header: X-Token
  token_env: MY_DATA_TOKEN
```

```yaml
auth:
  type: query
  param: token
  token_env: MY_DATA_TOKEN
```

Token 可以放在系统环境变量或项目 `.env` 中。

## 联调流程

1. 启动 mock 数据源:

```bash
cd docs/examples/custom-data-source
python mock_server.py
```

2. 复制示例配置:

```bash
mkdir -p data/data_sources
cp docs/examples/custom-data-source/mock_source.yaml data/data_sources/mock_source.yaml
```

3. 在「设置 -> 数据源」点击「重新加载」。

4. 使用「试拉测试」选择 `mock_source` 和 `daily` / `adj_factor` / `realtime`。

5. 保存数据源选择:

- 日K: `mock_source`
- 除权因子: `mock_source` (或保持默认 `tickflow`)
- 实时行情: `mock_source`

6. 触发同步或开启实时行情。

## 常见错误

| 现象 | 处理 |
| --- | --- |
| 列表里没有 custom 源 | 检查 YAML 是否放在 `data/data_sources/` 并点击重新加载 |
| errors 提示 missing mapped fields | `field_map` 没映射到必填内部字段 |
| 试拉 rows 为 0 | 检查 `response_path` 是否指向数组 |
| 日期列全为空 | 检查 `parse_date` 的格式是否和返回值一致 |
| 实时行情没刷新 | 确认实时数据源已保存为 custom,且返回全市场快照 |

## 用 AI 生成映射配置

如果你的数据源 API 文档比较复杂,可以把 API 文档和返回示例丢给 AI,让它帮你生成 `field_map` 和 YAML 配置。

### 操作步骤

1. 从你的数据源获取 API 文档(接口地址、请求方式、返回字段说明)
2. 试拉一次,拿到返回的 JSON 示例
3. 把下面的 prompt 模板 + API 文档 + JSON 示例一起发给 AI
4. 把 AI 生成的 YAML 贴到 `data/data_sources/xxx.yaml`
5. 在设置页点「重新加载」,再「试拉测试」验证

### Prompt 模板

复制以下内容发给 AI(替换方括号部分):

```text
我在配置一个自定义数据源接入股票面板。请根据我的 API 文档和返回示例,生成 YAML 配置。

要求:
1. 输出标准 YAML 配置,包含 name / display_name / auth / datasets
2. 每个数据集的 field_map 把我的接口字段名映射到内部字段名
3. 日期类字段如果格式不是 YYYY-MM-DD, 加上 transforms 里的 parse_date
4. 只配置我能提供的接口, 不存在的数据集不要写

内部字段对照表:

日K (daily):
  symbol = 股票代码, 格式 000001.SZ / 600000.SH
  date = 交易日期
  open / high / low / close = OHLC
  volume = 成交量
  amount = 成交额

除权因子 (adj_factor):
  symbol = 股票代码
  trade_date = 除权日期
  ex_factor = 复权因子

实时行情 (realtime):
  symbol = 股票代码
  last_price = 最新价
  prev_close = 昨收价
  open / high / low = 当日 OHLC
  volume = 成交量
  amount = 成交额
  change_pct = 涨跌幅 (小数, 0.0366 = 3.66%)
  change_amount = 涨跌额
  amplitude = 振幅 (小数, 0.024 = 2.4%)
  turnover_rate = 换手率 (小数, 0.05 = 5%)
  # 上游若返回百分数值 (3.66 表示 3.66%), 在 realtime 数据集声明 pct_unit: percent,
  # 不要依赖数值自动识别; 逐列转换也可用 transforms: turnover_rate: "value / 100"

分钟K (minute) 与 全量分钟 (full_minute, 字段同 minute):
  symbol = 股票代码
  # datetime 必须是北京时间墙钟 (如 2026-08-28 09:35:00), 不要返回 UTC;
  # 入口守卫会自动纠偏 UTC 特征帧, 但契约仍要求源头写对
  datetime = 北京时间墙钟 (YYYY-MM-DD HH:MM:SS)
  open / high / low / close = OHLC
  volume = 成交量
  amount = 成交额

=== 我的 API 文档 ===
[把你的接口文档贴这里: URL / 请求方式 / 参数 / 返回字段说明]

=== 返回 JSON 示例 ===
[把试拉的 JSON 返回贴这里]
```

AI 会输出类似这样的结果:

```yaml
name: my_source
display_name: "我的数据源"
auth:
  type: bearer
  token_env: MY_API_TOKEN

datasets:
  daily:
    url: https://api.example.com/kline
    method: POST
    batch: 100
    rpm: 200
    response_path: data.list
    field_map:
      ts_code: symbol
      trade_date: date
      open: open
      vol: volume
    transforms:
      date: "parse_date(value, '%Y%m%d')"
```

把这段 YAML 保存为 `data/data_sources/my_source.yaml`,然后在设置页重新加载即可。

## 开盘啦匿名补充数据

开盘啦通过现有扩展数据链路补充情绪、涨停、板块、竞价、龙虎榜、题材和消息。现有 stocksdk/TickFlow 等数据源选择不变，日K、分钟K、复权、财务和实时行情仍由各自已配置的数据源提供。开盘啦补充表按日期保存，不直接替换本地市场统计，也不自动作为股票行情、策略因子或回测输入。

当前目录声明 **39 项匿名接口功能**。其中 **30 项创建为扩展数据预设，默认关闭定时拉取**；另外 **9 项需要股票、板块、题材或区间参数，只通过按需查询接口调用**。目录声明和解析测试不等于所有接口都已通过真实网络验证，实际可用性和更新时间以请求结果为准。

2026-10-01 匿名联调以 2026-09-30 历史日期及当前快照逐项查询，39 项中 35 项返回数据、3 项返回空数据、游资席位信息返回上游错误码 `9999`。这些结果只代表本次参数和时点；题材详情另外使用题材搜索返回的有效 ID 核验。板块排名和游资动向存在返回空列表的情况，不保证每次请求都有数据。

| 类别 | 功能 |
| --- | --- |
| 市场概览 | 市场情绪、市场量能、核心指数快照 |
| 涨停分析 | 涨跌停总数、连板数量统计、涨停表现、涨停梯队、涨停板列表、破板个股、涨停原因、盘面亮点、大幅回撤 |
| 板块数据 | 板块涨停历史、板块强度、行业涨幅、地区涨幅、权重表现 |
| 龙虎榜 | 龙虎榜动向、龙虎榜列表、龙虎榜详细信息、游资动向、游资席位信息 |
| 竞价数据 | 竞价总体信息、竞价数量统计、尾盘抢筹 |
| 股票数据 | 股票所属板块、股票所属板块及龙头、百日新高、板块区间统计、股票区间统计、复盘榜 |
| 风口概念 | 股票风口明细、概念风口 |
| 题材数据 | 题材详情、题材库搜索 |
| 其他 | 大盘直播、历史新高趋势、法定节假日参考、最新消息 |

明确需要登录的 **板块竞价、板块内股票竞价、最强风口**不接入。**竞价列表 `MorningBiddingList`** 的认证参数说明存在歧义，在确认匿名访问条件前也不接入。请求不使用 `Token`、`UserID`、Cookie 或其他登录凭据，按需查询接口会拒绝这些参数。

「最新消息」只读取最新一页 20 条消息，不扫描全部历史消息归档。

### 获取和更新

复盘页的「开盘啦补充数据」卡片可点击「获取补充数据」，一次手动刷新以下 8 张主要表：

| 补充内容 | 扩展表 ID |
| --- | --- |
| 市场情绪 | `ext_kpl_emotion` |
| 市场量能 | `ext_kpl_capacity` |
| 涨停表现 | `ext_kpl_limit_performance` |
| 连板梯队 | `ext_kpl_limit_ladder` |
| 涨停原因 | `ext_kpl_limit_reasons` |
| 板块强度 | `ext_kpl_sector_strength` |
| 竞价总览 | `ext_kpl_auction_summary` |
| 大盘直播 | `ext_kpl_live` |

页面每 **60 秒**读取已保存的同日快照，这个页面轮询不会请求开盘啦。手动获取会请求上游并写入快照；切换历史日期后只显示所选日期的数据，不拿其他日期的结果补空。市场总览和复盘生成流程复用同一份补充摘要，第三方观点与本地统计分别注明来源。

需要持续取数时，可在现有扩展数据拉取设置中启用相应预设。核心指数、连板统计、涨停梯队、涨停和破板名单、板块强度及行业排行、大盘直播的预设周期为 **1 分钟**；情绪、量能、涨停表现、竞价总览等预设周期为 **5 分钟**。龙虎榜类为 30 分钟，历史趋势与节假日参考为 1440 分钟。启用后沿用扩展数据调度器，可设置拉取间隔和北京时间时间窗口；默认预设关闭，已有同 ID 配置不会被覆盖。

这些周期是请求计划，不代表供应商会按同样频率更新，也不保证秒级行情或覆盖全部市场数据。接口只提供当前快照时不接受历史日期；文档只声明历史接口的功能不会猜测其他实时端点。

### API

开盘啦接口挂在复盘 API 下：

| 方法和路径 | 行为 |
| --- | --- |
| `GET /api/market-recap/kaipanla/catalog` | 返回 39 项目录、必需查询参数、历史查询支持、预设周期和排除清单，不触发取数 |
| `GET /api/market-recap/kaipanla/context?date=YYYY-MM-DD` | 读取目标日已保存的 8 表摘要，不请求上游 |
| `POST /api/market-recap/kaipanla/refresh` | 手动请求并保存 8 表完整快照，返回各表成功、空数据或失败状态 |
| `POST /api/market-recap/kaipanla/query/{dataset_id}` | 查询目录中的单项功能，返回数据及日期归属信息，不写入扩展表 |

`context` 和 `refresh` 未指定日期时使用本地最新交易日。`refresh` 请求体可为 `{}` 或 `{"date":"2026-09-30"}`；`query` 未指定日期时使用当前北京时间日期。未来日期会被拒绝。

以下 9 项必须带业务参数，不创建默认拉取预设：

| 数据集 ID | 必需参数 |
| --- | --- |
| `ext_kpl_plate_history` | `ZSCode`：板块代码 |
| `ext_kpl_lhb_details` | `StockID`：股票代码 |
| `ext_kpl_hot_money_seats` | `GID`：游资组编号 |
| `ext_kpl_stock_plates` | `StockID`：股票代码 |
| `ext_kpl_stock_plates_v2` | `StockID`：股票代码 |
| `ext_kpl_range_plates` | `DStart`、`DEnd`：区间起止日期 |
| `ext_kpl_range_stocks` | `DStart`、`DEnd`：区间起止日期 |
| `ext_kpl_theme_details` | `ID`：题材编号 |
| `ext_kpl_theme_search` | `key`：搜索词 |

例如查询股票当前所属板块：

```http
POST /api/market-recap/kaipanla/query/ext_kpl_stock_plates_v2
Content-Type: application/json

{"parameters":{"StockID":"002726"}}
```

`query` 返回 `requested_date`、`data_date`、`date_origin`、`fetched_at`、`state` 和 `rows`。`date_origin=response` 表示响应中有可核验日期；`request_parameter` 表示仅依据接口日期参数归属；`range_parameters` 表示依据区间参数；`observation` 表示当前观察数据未提供可核验业务日期。`fetched_at` 是本系统获取时间，不代表报价更新时间或信息最早公开时间。

### 日期、快照和单位边界

- 校验响应包及业务行的 `Date/date/Day/day`，以及表示日期的 `Time` 字符串；日期格式非法、别名冲突或与请求不符时拒绝发布。市场情绪的多日列表只提取目标日，历史趋势和板块历史保留各自业务日期，显式历史查询排除晚于目标日的记录。
- 开盘啦 `Index` 是记录偏移，按实际原始记录数推进；百日新高按原始板块组数计数。涨停和破板列表默认汇总 `PidType=1..5`，单项查询也可显式选择 `PidType`。全部分页成功后才保存，重复页、异常空页或达到安全上限均拒绝发布不完整榜单。
- 完整快照按 `data/ext_data/{id}/timeseries/date=YYYY-MM-DD/part.parquet` 原子替换。网络、日期或解析失败时保留该表上一份完整快照；单表失败不影响其他表。成功返回空数据时以空快照替换该日旧名单，避免退出涨停、破板或新高榜的股票继续残留。
- 百分数保留供应商百分数口径，例如 `10.03` 表示 `10.03%`，不注入系统实时行情的小数制字段。市场量能保留文档标注的「万」单位及预测成交文本；只有文档明确标注元的金额才注明元。龙虎榜动向中单位有歧义的 `Money` 保存为 `money_raw`，不擅自换算成元。
- 涨停表现数组的 `[0]`、`[3]` 在文档中标为总家数、最高板数，但本次分别返回 `40`、`2`，与同日情绪的 `52` 家及梯队最高 `7` 板冲突。保留这两个原值并标注口径待核，从复盘展示摘要与 AI 输入中排除，避免当作已核验的总数或高度。
- 指数请求使用系统核心四只：上证指数、深证成指、创业板指、**科创综指 `000680.SH`**。不以文档示例中的科创50 `000688.SH` 替代科创综指。开盘啦法定节假日清单只供参考，不替代系统实际交易日序列。
- 事后查询的龙虎榜、涨停原因、题材和直播内容不能证明目标日盘中已经公开。预设采用市场级补充表，不自动连接股票因子或历史回测；复盘摘要保留获取时间，并提示事后取数边界。第三方文本作为引用材料处理，其中的操作指令不执行。
