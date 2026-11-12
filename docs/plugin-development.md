# 数据源插件开发指南

数据源插件是可选的行情数据来源(fuyao、akshare 等),作为独立模块放在
`backend/app/plugins/` 下。services 层(kline_sync / quote_service / financial_sync)
全部通过统一路由点分流:插件声明了某数据集就走插件,未声明自动回退 TickFlow。
因此**一个合格的插件只需要正确实现契约,不需要改动任何 service / API 代码**;
反过来,插件也必须遵守内部数据契约(单位、代码格式、复权口径),框架不会替你转换。

> 无代码接入(纯 HTTP YAML 配置)请看 [custom-data-source.md](./custom-data-source.md),
> 两种方式遵循同一套内部数据契约。

## 外部安装

现在可通过「设置 → 数据源 → 安装插件」上传独立源码 ZIP。外部包格式、版本管理、兼容范围和模板见 [外部数据源插件安装](external-data-source-packages.md)。现有内置目录方式继续兼容。

## 快速上手

一个插件 = 一个目录 + 一个 `plugin.yaml` 清单:

```
backend/app/plugins/<your_plugin>/
├── plugin.yaml          # 清单(必需)
├── provider.py          # Provider 实现(必需)
├── ...                  # client/桥接/依赖文件(按需)
```

### plugin.yaml 字段

```yaml
name: my_source                          # 唯一标识, 只允许 [a-z0-9_], 也是 provider name
display_name: "我的数据源"                 # 设置页显示名
runtime: none                            # 运行时类型: node | python | none
entry: app.plugins.my_source.provider:MyProvider   # provider 类的导入路径
check: app.plugins.my_source.bridge:availability   # 可用性检测函数(可选)
datasets: [realtime]                     # 支持: daily/adj_factor/minute/realtime/depth5/financial
api_key_env: MY_SOURCE_API_KEY           # (可选)声明后设置页提供 Key 输入框
hidden: false                            # (可选)true = 已加载但对设置页隐藏,不注册不展示
description: |
  ## 接入说明
  使用 **个人 API Key** 获取数据。
install_hint: "pip install xxx"          # 未装依赖时显示的安装提示
homepage: "https://example.com"          # (可选)官网/文档地址, 显示在插件详情中
homepage_label: "访问官网"                 # (可选)链接文字, 缺省为「显示名 官网」
billing: points                          # (可选)计费标识: "points" → 设置页能力芯片/适配行标注「积分」
billing_note: "消耗与频率/范围相关,间隔越短越高"  # (可选)计费提示: 设置页按警示框展示
usage_notes:                             # (可选)使用要点: 各能力规模/节奏, 详情卡以列表展示
  - "实时快照: **600 只/批**, 全市场一轮约 10 批"
  - "请先阅读 [接口文档](https://example.com/docs)。"
key_manage_url: "https://example.com/me" # (可选)Key/账务管理页, 显示在插件详情中
key_manage_label: "获取 Key / 查看用量"    # (可选)缺省为「管理 Key / 查看账户用量」
```

`homepage_label`、`key_manage_label` 为可选纯文本，最多 200 字符；不填或留空时使用默认文案，没有对应 URL 时不显示入口。外部安装包的 `homepage`、`key_manage_url` 必须使用 HTTPS。链接在「数据源」与「已安装扩展」详情中展示，不依赖 `api_key_env` 或 Key 配置状态。新增字段不改变插件 API 版本，旧清单继续兼容。

这两处详情及数据源安装预览的 `description`、详情中的 `usage_notes` 支持基础 Markdown：1–4 级标题、加粗、行内代码、无序/有序列表、引用、分隔线、表格和 HTTP/HTTPS 链接。YAML 多行正文可用 `|` 编写；名称、按钮与链接文案保持纯文本。当前不是完整 GFM，不支持图片、代码围栏或原始 HTML；原始 HTML 和不安全协议链接只显示为文字，不会执行。

只声明真实提供的数据集;未声明的数据集 `provider_has_dataset` 返回 False,自动回退
TickFlow。不要声明做不了的数据集(粒度含义见下文"能力声明的粒度")。

#### api_key_env(界面配置 API Key)

声明 `api_key_env` 的插件可以在设置页的数据源卡片中直接填写 Key, 对齐
TickFlow 的「先探后存」语义:

1. entry 模块需提供模块级 `probe_api_key(key) -> (ok, reason)`,
   后端用候选 Key 实探一次, **无效不落盘**
2. 有效则写入 `data/user_data/secrets.json` 的 `{name}_api_key` 字段
   (0600 权限, 优先级高于 `.env` / 环境变量)
3. 保存后自动重载数据源注册表, 插件即刻变为可切换
4. 插件取 Key 用 `secrets_store.get_env_backed_secret("{name}_api_key", api_key_env)`,
   保证 secrets.json 与 .env 两条配置路径一致

### runtime 字段说明

| runtime | 含义 | 典型场景 |
|---|---|---|
| `python` | 纯 Python 依赖, `pip install` | akshare、tushare |
| `none` | 无额外依赖 | 纯 HTTP API 源 |

内置插件的 `runtime` 决定依赖安装方式，实际就绪检测由 `check` 函数负责。外部源码包只检查依赖兼容性，不执行 pip/npm 安装。

### check 函数

插件自己负责检测依赖/Key 是否就绪。后端启动时会调用此函数:

```python
# app/plugins/my_source/provider.py (或 bridge.py)
def availability() -> tuple[bool, str]:
    """返回 (是否可用, 原因)。不抛异常。"""
    if not get_api_key():
        return False, "未配置 MY_SOURCE_API_KEY(可在设置页数据源卡片中直接填写)"
    return True, "ok"
```

- **可用** → 插件注册进路由表, 设置页可切换
- **不可用** → 设置页显示插件卡片但灰显, 展示原因/`install_hint`

## 内部数据契约(所有数据集必须遵守)

以下口径是全项目红线(详见 CONTRIBUTING §3)。金融数据错误往往不抛异常,而是生成
**看似合理的错误结果**——单位、代码格式、复权口径错了,页面照样能渲染,只是数字全错。
插件必须在 provider 内完成适配。

### 代码格式

- symbol 统一带交易所后缀: `600519.SH` / `000001.SZ` / `300750.SZ`; ETF、指数同格式。
- 接口返回裸代码(如 `600519`)或异构格式时,在 client 层实测一页并归一,不要直接透传。

### 单位制

| 字段 | 契约 | 说明 |
| --- | --- | --- |
| `change_pct` | **小数制**, `0.0366` = 3.66% | 接口给百分数(3.66)时必须在 provider 内显式 /100 |
| `turnover_rate`(realtime 入口) | **小数制**, `0.05` = 5% | 下游 enriched 管道统一转百分数值存储 |
| `volume` | **手**, `436231` = 43,623,100 股 | 日K 与实时快照均以手计(1手=100股), 股票/ETF/指数一致(与上游 TickFlow 口径一致, 可用 amount÷volume÷100≈当日均价自验); 接口给股时必须在 provider 内显式 /100(参考 fuyao) |
| `amount` / `turnover` | 元 | |
| 日K OHLC | **不复权原始价** | 复权由 adj_factor + enriched 管道处理, provider 不得自行复权 |

### 缺字段与空数据

- 接口不提供的字段返回 `None`,禁止"数值小于 1 就乘 100"之类启发式补全——那会掩盖
  真实的数据错误。
- 可推导字段按固定口径推导: `change_pct = change_amount / prev_close`(小数制,不乘 100)。
- 接口结构整体变化(如所有行都识别不出 symbol)要打明确告警日志,不要静默返回空数据。

## 能力声明的粒度(重要)

`datasets` 声明是**数据集级**的,不是资产类型级的:声明了 `realtime`,整个全市场实时
轮询周期(含指数与 ETF 部分)就全部路由给插件。若你的快照只覆盖 A 股股票:

- 指数行情自动降级为日线推导值(非实时),不报错;
- ETF 实时计数为 0。

这是当前框架的设计行为。要么在数据里尽量覆盖指数/ETF,要么接受降级并在
`description` 里向用户说明覆盖范围。

## Provider 接口契约

Provider 是普通 Python 类(无需继承基类),方法签名对齐 `GenericHTTPProvider`,
services 层零改动即可路由。只实现已声明数据集对应的方法,其余可缺省。

```python
class MyProvider:
    name = "my_source"
    builtin = True  # 标记为内置(不可被用户编辑/删除)

    def __init__(self):
        self.config = MyConfig()  # 需有 .datasets 属性(dict, key 是数据集名)

    def close(self) -> None:
        """清理资源(load_all 重建注册表时会调)。"""

    def get_daily(self, symbols, start_time, end_time, asset_type="stock",
                  on_chunk_done=None) -> pl.DataFrame:
        """日K: [symbol, date, open, high, low, close, volume, amount]; 不复权"""

    def iter_daily(self, symbols, start_time, end_time, asset_type="stock",
                   on_chunk_done=None) -> Iterator[pl.DataFrame]:
        """(可选)有界分批返回与 get_daily 同形的日K; 全市场历史同步优先消费。"""

    def get_adj_factors(self, symbols, start_time, end_time, asset_type="stock",
                        on_chunk_done=None) -> pl.DataFrame:
        """除权因子: [symbol, trade_date, ex_factor]
        (+可选明细列 dividend/bonus/allot/allot_price/prev_close, 均可空 —
        提供时用于等差显示投影与全精度因子链重建)"""

    def get_minute(self, symbols, start_time, end_time, asset_type="stock",
                   on_chunk_done=None, freq="1m") -> pl.DataFrame:
        """分钟K: [symbol, datetime(北京墙钟), open, high, low, close, volume, amount(元, 可空)]"""

    def iter_minute(self, symbols, start_time, end_time, asset_type="stock",
                    freq="1m", on_chunk_done=None) -> Iterator[pl.DataFrame]:
        """(可选)有界分批返回与 get_minute 同形的分钟K;历史落盘优先消费。"""

    def get_intraday_batch(self, symbols, count=300, asset_type="stock") -> pl.DataFrame:
        """(声明 full_minute 数据集时实现) 全量分钟修复轮: 给定标的当日 1 分钟K,
        canonical 8 列同 get_minute; 内部自行分块/限速。"""

    def get_intraday_latest(self, symbols=None, count=3) -> pl.DataFrame:
        """(可选, full_minute 稳态增量轮) 尽量单请求返回全市场每只最新 count 根;
        未实现则服务降级为仅修复轮 (节奏下限 60s)。"""

    def get_realtime(self) -> list[dict]:
        """全市场实时快照 → list[dict]。失败软返回 [], 不抛异常(不阻断轮询线程)。"""

    def get_realtime_symbols(self, symbols: list[str], *, asset_type="stock") -> list[dict] | None:
        """(可选)补拉全市场快照未覆盖的自选/详情股票或 ETF，字段同 realtime。
        未实现时不补拉；失败返回 None，成功无数据返回 []，不得内部静默跨源。"""

    def get_realtime_indices(self, symbols: list[str]) -> list[dict] | None:
        """(可选)指数实时快照 → list[dict], 行字段与 get_realtime 一致。
        A 股快照普遍不含指数(fuyao 的指数在独立端点); 声明 realtime 的源
        强烈建议实现本方法, 否则指数行情冻结在本地日K兜底。失败返回 None,
        成功但无数据返回 []。"""

    # symbols now includes saved watchlist indices and visible detail leases,
    # in addition to the core indices, benchmarks and enabled monitoring rules.

    def get_depth_batch(self, symbols: list[str]) -> dict[str, dict]:
        """(声明 depth5 时实现)五档盘口, 返回以 symbol 为键的标准盘口字典。"""

    def get_financials(self, table, symbols, latest_only=False) -> pl.DataFrame:
        """财务数据(声明 financial 数据集时实现, table 见 financial_sync 调用)。"""

    def get_instruments(self, asset_type="stock") -> list[dict]:
        """(可选)标的维表: 返回 tickflow Instrument 形状的行, 供 instrument_sync 复用 flatten"""

    def test_dataset(self, dataset: str, symbols=None) -> dict:
        """(强烈建议)设置页"试拉"按钮。
        返回 {provider, dataset, rows, columns, preview, error?}; 未支持的数据集
        返回 error 字段说明会回退 TickFlow。"""
```

`get_depth_batch` 返回结构如下。价格和数量数组均按一档到五档排列;数量单位为“手”,
`timestamp` 为毫秒 Unix 时间戳。服务层按 capability 的 `batch` / `rpm` 统一分片限速,
provider 不应自行切换或回退到其他数据源。

```python
{
    "600519.SH": {
        "bid_prices": [1500.0, 1499.9, 1499.8, 1499.7, 1499.6],
        "bid_volumes": [10, 20, 30, 40, 50],
        "ask_prices": [1500.1, 1500.2, 1500.3, 1500.4, 1500.5],
        "ask_volumes": [12, 22, 32, 42, 52],
        "timestamp": 1788505200000,
    },
}
```

### 历史分钟请求与流式接口

核心把完整的 `symbols`、时间窗、`asset_type` 和 `freq` 一次交给选中的源，
不再按固定 50 只拆分历史分钟请求。标的分批、时间分段、分页、限频、重试和
同源端点降级均由 Provider 管理；不得静默改用另一个数据源。

提供 `iter_minute` 时，历史落盘逐帧校验并写入，不在核心攒齐全市场结果。
每帧至少包含 `symbol/datetime/open/high/low/close/volume`，`amount` 可缺省或为空；
标的和时间不可为空，合法的休市、停牌、无数据批次可以返回空帧。
源应限制输出帧的行数，避免先累积全市场历史再一次 `yield`；请求批次和输出帧
不要求一一对应，可在源内合并小结果，减少反复合并同一日分区的写盘成本。

`on_chunk_done(cur, total)` 仍为两参，计量单位由源决定（如请求数或标的数），
需覆盖空批次；等待限流期间可重复报告当前进度作为心跳，回调抛出的异常必须
原样传递以支持取消。不要把未知的总量或失败批次伪装为已成功完成。
消费者提前退出、写盘失败或取消时会关闭迭代器；源在 `finally` 中释放临时资源。
注册源重载或更换 Key 时，在途迭代持有旧实例，结束后才关闭旧连接。

未实现 `iter_minute` 的旧插件继续调用 `get_minute`：仍一次收到完整请求，
宿主不会在兼容层偷偷拆批；返回结果作为一帧保存。这能兼容旧包和配置，但
无法保证旧插件内部的内存峰值；全市场深历史源建议补充流式接口。
原有 `get_minute` 返回 DataFrame 的调用方式保持可用。
YAML 源的 `batch/rpm/timeout` 继续有效，由源自身执行；无须迁移配置。
盘中 `full_minute` 的独立权限、请求池和刷新调度沿用原契约。

### get_minute / iter_minute 的 datetime 时区契约

`datetime` 必须是**北京时间墙钟**（naive，如 `2026-08-28 09:35:00`），与日K的
`date` 语义对齐；不要返回 UTC 或带时区的时间。前端分时图按交易时段时轴
（09:30–11:30 / 13:00–15:00）映射每根K线，UTC 口径的帧会导致全部点位落在时轴外、
分时图空白。

`amount` 单位为元；数据源无法提供可靠的分钟成交额时应返回 `null`，不得伪造。
成交额缺失后无法继续计算累计成交均价，前端会停止绘制后续均价线并显示 `—`。

分钟帧可附带两个逐行字符串字段：`minute_price_basis` 表示价格口径（`raw` 为原价，
`forward` 为前复权；缺失／`null` 为未知），`minute_time_grid=auction_end` 表示独立
09:30 开盘点加 09:31–11:30、13:01–15:00 的 241 点网格。仅在源接口有明确保证时
声明；它们不是能力开关，不能由当前路由反推旧数据来源。未声明网格的 240 根分钟仍
按开盘标签验证。首板要求某股票当日已完成分钟全部有原价证据且网格完整，未知或
混合记录不会被新增记录认证；分区合并保留可选列，复权投影改变价格时同步改为
`forward`。量使用手、额使用元，时间戳严格早于当前分钟才参与首板计算。

入口守卫（`kline_sync._enforce_minute_beijing_wallclock`）对所有分钟源强制归一：
带时区 → 自动换算成北京墙钟；naive 但整体呈 UTC 特征（如 01:30）→ 自动 +8 纠偏并
记日志；完全无法识别的口径 → 拒收，不静默跨源。契约仍要求源头写对，守卫只是兜底。

可选类属性 `minute_history_days = 5` 声明 1 分钟历史深度（交易日）；未声明视为
深历史（TickFlow 基准）。浅源（如免费分时仅保留最近 5 个交易日的源）声明后，
个股分时档位自动收窄为可行选项并默认 5 日，深源默认 20 日。

> **全量分钟 (full_minute) 数据集契约**:声明 `full_minute` 数据集并把
> `full_minute_data_provider` 路由到你的源,即接入「全量分钟」能力(盘中全市场
> 当日分钟K增量落盘,由内置服务 `minute_refresh` 调度,与 TickFlow Expert 同一
> 能力键 `intraday.universe`)。需实现:
>
> - `get_intraday_batch(symbols, count=300, asset_type="stock") -> pl.DataFrame`
>   — **必须**(或已有 `get_minute` 自动回退,但强烈建议实现批量端点)。
>   返回给定标的当日 1 分钟K,canonical 8 列
>   `[symbol, datetime(北京墙钟 naive), open, high, low, close, volume, amount]`,
>   内部自行分块/限速。服务在冷启动、覆盖断档、连续空轮时调用(修复轮)。
> - `get_intraday_latest(symbols=None, count=3) -> pl.DataFrame` — **可选**,
>   稳态增量轮专用:尽量单请求返回全市场每只标的最新 `count` 根。未实现时
>   服务自动降级为仅修复轮,节奏下限抬到 60s(全天批量打不住 6s 节奏)。
> - **价格基准契约**:两个方法都必须返回**原始价(不复权)**。本地分钟库以 raw
>   基准落盘 (`.raw_basis` 标记),读取侧统一做前复权投影;返回已复权价格会与
>   本地存储混基准,产生静默的错误价格。服务状态面板的 `minute_basis_raw`
>   字段会暴露本地库的基准迁移状态。
>
> 两个方法的返回帧都过 `_enforce_minute_beijing_wallclock` 时区守卫(与
> `get_minute` 同纪律);失败抛异常或返回空 df 均按空轮处理,连续空轮触发
> 修复轮自愈。声明方式:插件在 `plugin.yaml` 的 `datasets:` 列表加入
> `full_minute`。YAML 声明式源同样支持(数据集配置与 `minute` 同形,仅提供
> 修复轮语义,见 [custom-data-source.md](./custom-data-source.md))。

### 异常语义

| 方法 | 失败行为 |
| --- | --- |
| `get_realtime` | **软失败**: 返回 `[]` + warning 日志, 保证轮询线程不中断 |
| `get_realtime_indices` | **软失败**: 返回 `None` + warning 日志, 保留上轮有效缓存; 成功无数据返回 `[]` |
| `get_depth_batch` | 单批异常由服务隔离并保留其他批次; 不跨数据源回退 |
| `get_minute` | `ProviderRequestError` 明确上报失败，不跨源；旧非流式调用的普通异常保留告警与空帧语义 |
| `iter_minute` | 取数或数据契约错误中止本次任务，不跨源；已保存帧保留并刷新查询视图，后续可重新补齐。取消异常原样透传 |
| `get_daily` / `get_adj_factors` / `get_financials` | 异常由上层同步流程捕获记录; 无数据返回空 DataFrame |
| `iter_daily` | 可选; 每批必须符合 `get_daily` 契约。流正常结束后才提交 staging; 未捕获异常会丢弃 staging。provider 内已定义的单标的软失败语义保持不变 |

`iter_daily` 用于避免大范围日K同步在 provider 内累积完整 DataFrame。实现该方法后,
`kline_sync` 会优先消费它; 未实现的 provider 继续调用 `get_daily`,保持兼容。批次大小应有
明确上界,不得先把全部结果放入列表再 `concat`。`on_chunk_done(cur, total)` 必须覆盖空批次,
确保最终 `cur == total`。

### get_realtime 行字段

自选和当前详情的行情需求由同一个 `QuoteService` 合并去重，页面不自行启动行情轮询。
TickFlow 全市场快照未覆盖的自选/详情标的按码补拉（包括未开启全量 ETF 拉取时的少量 ETF）；
指数通过既有 `get_realtime_indices` 补充。自定义源未提供某个标的时保留有效旧数据并显示日期/过期状态，
不把历史日K当实时快照，不为补齐行情静默切换数据源。显式选择的实时源未加载或未声明 `realtime` 时同样不回退。

详情使用 `PUT /api/intraday/subscriptions/{client_id}` 登记 `{ "symbols": ["000985.SH"] }`，
每个租约最多 64 只、90 秒有效；`DELETE` 释放，页面约每 20 秒续租，隐藏窗口停止续租。
接口只登记需求，不立即取数，不改变行情总开关，也不写入用户配置；服务端重启后页面续租恢复。
多个窗口的重叠标的由同一个行情工作线程按集合拉取，手动刷新与后台轮询复用同轮结果。
该去重范围为单个后端进程（默认部署）；多个后端进程/副本需要独立的集中行情工作进程。

自选行和日K详情新增可选的 `quote_freshness` 对象，包含 `state`、`message`、`data_date`、
`quote_ts`（毫秒，可空）、`checked_at`、`max_age_ms`。状态包括 `live`、`delayed`、`stale`、
`historical`、`unknown`、`missing`、`paused`、`disabled`。日期和时间按标的判断，不用资产大类日期替代；
未提供时间戳的旧数据保持可读，标记为时间未知，不伪造实时性。休市、盘前、午休和收盘按北京时间边界判断。
原响应字段、历史 Parquet 和用户配置保持兼容；提示信息不落盘。

| 字段 | 必需 | 契约 |
| --- | --- | --- |
| `symbol` | ✅ | 标准代码带后缀 |
| `last_price` | ✅ | 最新价 |
| `prev_close` | ✅ | 昨收, 涨跌幅推导基准 |
| `open` / `high` / `low` | ✅ | 当日 OHLC |
| `volume` | ✅ | **手**(1手=100股) |
| `amount` | 建议 | 成交额(元) |
| `change_pct` | 建议 | **小数制**; 缺失时下游按 change_amount/prev_close 推导 |
| `change_amount` | 建议 | 涨跌额(元) |
| `timestamp` | 建议 | 毫秒; 优先用服务端时间(行情归属), 缺失退本地时间 |
| `name` | 可选 | 快照无名称时置 None, 下游用标的维表关联 |
| `amplitude` / `turnover_rate` / `session` | 可选 | 缺失置 None, 不启发式伪造; turnover_rate 入口为小数制 |

### 同花顺指数数据集 `ths_index`

指数页的同花顺全A（沪深）使用 `883421.TI`。此能力通过
`ths_index_data_provider` 独立路由，默认 `fuyao`，可用性由能力矩阵决定。
Python 插件声明 `datasets: [ths_index]` 后需实现以下方法。通用 YAML 尚不支持此数据集。

- `get_ths_index_quotes() -> list[dict]` 返回指定指数的 `symbol`、
  `last_price`、`prev_close`、小数制 `change_pct` 和毫秒 `snapshot_time`。
  `snapshot_time` 是服务端取得快照的时间，不能推断为成交时间或交易日。
- `get_ths_index_daily(symbol: str, start: date, end: date) -> pl.DataFrame`
  返回标准日 K 列，日期为北京交易日且含首尾日期，成交量使用手、成交额使用元。
  服务复用通用指标计算，在内存中缓存结果。

扶摇适配器分别调用 `/api/a-share-index/prices/snapshot` 与
`/api/a-share-index/prices/historical`，仅查询 `883421.TI`。查询凭据复用现有扶摇插件，
指数报价与日 K 不写入股票仓库。页面每 10 秒刷新报价，服务端合并重复请求，
源不可用时显示原因。该指数接口仅提供日 K 与快照。参见
[扶摇指数接口文档](https://fuyao.aicubes.cn/docs/api-reference/a-share-index/)。

### 海外观察数据集 `overseas`

海外观察使用独立数据集和路由，不混入 A 股 `realtime`、国内指数缓存或本地日线仓库。
目前仅支持 Python 插件；通用 YAML 数据源尚未实现此数据集的字段映射，声明该能力会被拒绝。插件只在真实提供此能力时声明 `datasets: [overseas]`，并实现：

```python
def get_overseas_quotes(self) -> dict:
    # source 为插件名，fetched_at 为带时区的本次采集时间。
    return {"source": self.name, "fetched_at": "2026-09-30T07:00:00+00:00", "quotes": []}
```

`quotes` 的四个固定标的是纳斯达克综合指数 `^IXIC`、韩国 KOSPI `^KS11`、
三星电子 `005930.KS`、SK 海力士 `000660.KS`。每个标的始终返回一行，失败也应保留
标识和原因，禁止用零涨跌幅代替缺失值：

| 字段 | 契约 |
| --- | --- |
| `symbol` / `name` | 固定标识和中文显示名；不得用纳斯达克 100、ETF 或存托凭证暗中代替 |
| `market` / `currency` | `US` 或 `KR`；本币报价为 `USD` 或 `KRW`，不做汇率换算 |
| `state` | provider 返回 `ready`、`unavailable` 或 `unverified`；消费服务可再标记 `stale` |
| `price` / `previous_close` | 正数或 `null`；最新常规交易报价、其相邻上一交易日的同口径收盘价 |
| `change_pct` | **小数制**，`price / previous_close - 1`；无法核验时为 `null` |
| `session_date` | 报价所属交易所本地日期，不能直接使用北京时间日期 |
| `observed_at` | 带时区的报价时间；不能用抓取时间代替缺失的报价时间 |
| `available_at` | 本系统本次实际取得报价的时间；不是历史最早公开时间 |
| `phase` | `intraday`、`closed`、`unknown`；无法核验交易时段时为 `unknown` |
| `source_url` / `reason` | 固定行情页面和可读状态原因；不得泄漏响应原文、密钥或底层异常 |

指数页复用同一 `overseas` 路由。插件可分别实现以下可选方法。缺少某个方法时，仅对应的指数报价或日 K 显示为空。

- `get_overseas_index_quotes() -> list[dict]` 返回上述报价结构的列表，覆盖 `^DJI`、`^GSPC`、`^IXIC`、`^N225`、`^KS11` 中支持的指数。日本市场使用 `JP`、`JPY` 和 `Asia/Tokyo`。指数页 API 将已核验的小数涨跌幅转换为百分数。
- `get_overseas_index_daily(symbol: str, start: date, end: date) -> list[dict]` 按当地交易日升序返回含首尾日期的日线，每行含 `symbol`、ISO 格式 `date`、`open/high/low/close` 及可空的 `volume`。缺少 OHLC 的日期跳过。

内置 `yahoo_overseas` 使用项目已有 `httpx`，无需额外依赖或 API Key。
注册、构造、`availability()`、`validate()` 均不联网；显式采集时最多四个并发请求，
每个请求超时 8 秒，不重试、不跟随跳转、不使用账户凭据或浏览器伪装。HTTP 429、
网络错误、空数据按标的隔离，采集结束关闭客户端，不静默换源。

适配器使用 Yahoo chart 的 `regularMarketPrice`、`regularMarketTime`，
按 `America/New_York`（自动处理夏令时）和 `Asia/Seoul` 归属交易日。
`chartPreviousClose` 可能是查询区间的起点基准，**不得直接作为上一交易日收盘价**；
适配器改用返回日线的相邻两个交易日，并检查日期、OHLC、空值和数组长度。
最新报价须处于当日日线高低价之间，仅容许源声明价格精度的半单位舍入差；
报价与日线尺度不一致时，适配器保留部分报价但不计算涨跌幅；消费服务会清空未核验行的数值，只展示原因，不猜测倍数或自行换算。
日线时间只用于识别交易日，不代表该日收盘价何时可得。

韩国个股请求除权事件并核对相邻两日 `adjclose / close` 的口径是否一致。
比较窗口跨分红、拆股等事件，事件字段异常，或缺少除权校验数据时，保留有效报价，
将涨跌幅标为不可核验。接口未返回事件仅表示本次返回中没有事件记录，不代表完整公告核验。
本模块不修复或猜测公司行动。

数据源的 `currentTradingPeriod` 可能已指向下一交易日，因此只有它与报价所属日期一致时
才判断盘中/收盘；只有接近已结束时段末端的报价才能标为收盘。未取得独立海外交易日历，
不根据周一至周五推定开市；消费服务还需校验报价新鲜度与分析截止时间。
后取得的行情不得回填历史计划，延迟时长无法核验时必须明确显示，不能宣称实时行情。

该公开 chart 端点并非有可用性保证的开发者接口。实现依据为
[yfinance 维护者的 chart 调用](https://github.com/ranaroussi/yfinance/blob/main/yfinance/scrapers/history.py)
及[相邻日线前收口径](https://github.com/ranaroussi/yfinance/blob/main/yfinance/scrapers/quote.py)。
旧版连接核验曾遇到 HTTP 429；本次迁移使用合成固定样本验证，未重新确认实盘源可用性。

### config.datasets 的作用

`provider_has_dataset(name, dataset)` 通过 `dataset in provider.config.datasets` 判断。
这是 services 层路由的关键: 各能力按自己的路由契约处理未声明或缺失情况。
独立的 `overseas` 能力不支持 TickFlow；无有效配置时选默认 `yahoo_overseas`，
选定插件采集失败后不再尝试其他来源。

```python
class MyConfig:
    datasets = {"daily": ..., "realtime": ...}  # key 是数据集名, value 任意
```

## 限频与性能

- realtime 默认 6s 轮询一轮。优先确认服务端单次 limit 上限: fuyao 实测单页
  limit=6000 可一次拉完全市场(~5600 只), 1 请求/轮; 若服务端强制小页, 必须做
  页间隔/自限速(参考 fuyao 的 0.15s 页间隔兜底), 并建议用户把轮询间隔调大(15-30s)。
- 分页必须有页数上限(防 count 异常导致死循环)和空页终止条件。
- 拉取由 fetch 锁串行化, 慢不会并发重叠; 实际刷新周期 = 轮询间隔 + 拉取耗时,
  串行分页的全量快照本身就需要数秒, 不要按"6s 内必须完成"设计。

## 测试要求

插件 PR 必须带契约测试(CONTRIBUTING §9), **不依赖真实网络与 API Key**——用假
Client/桥接注入。以 `backend/tests/test_fuyao_provider.py` 为范本, 至少覆盖:

1. 字段映射与单位转换: 百分数→小数制、volume 股→手、*ms 零点戳时区换算、缺失字段按口径推导、缺失字段置 None 不伪造
2. 接口响应结构变体: 实测结构 vs 官方文档示例双兼容(供应商文档与实际不一致是常态)
3. 分页: 多页合并、空页终止、页数上限
4. 软失败: 接口报错返回 []; 整页 schema 变化有告警而非静默空数据
5. 能力声明: 未声明数据集 `provider_has_dataset` 为 False
6. Key 语义: 先探后存(无效不落盘)、secrets.json > .env 优先级、availability 两态
7. loader 集成: 清单解析后正确注册(或 hidden 时正确跳过)

```bash
cd backend && uv run --extra dev python -m pytest tests/test_<your_plugin>_provider.py -q
uv run --extra dev python -m ruff check app/plugins/<your_plugin>/ tests/test_<your_plugin>_provider.py
```

## 现有插件参考

- **zzshare（自在量化）**。在「设置 → 数据源 → zzshare」试拉 A 股股票日 K。试拉只预览结果。需要用于日 K 同步时，单独切换日 K 数据源。插件使用公开 HTTP 接口，默认匿名访问，可通过环境变量 `ZZSHARE_TOKEN` 提供自己的凭据。
  - 插件只声明 `daily`，返回未复权 OHLC，成交量由股转换为手，成交额为元。指数、ETF 走系统现有日 K 回退流程。上游 `factor` 不写入本地除权事件表。
  - 卡片上的涨停归因同步独立于日 K 选择。在「设置 → 数据源 → zzshare → 查看操作」选定已收盘交易日，点击「同步涨停归因」，再点「查看涨停归因」展开股票完整原因。首次成功同步后才创建 `zzshare_limit_reasons`，在「数据」页的「数据画像」中显示为「ZZShare 涨停归因」卡片；顶部「扩展数据」按钮用于新建表。一只股票一行，同股保留不同的原因文本。原始记录数含重复项，不可用作涨停家数。采集时间表示本地取数时刻，不能作为原因的历史发布时间。
  - 「数据 → 数据画像 → ZZShare 涨停归因」卡片也可直接同步和查看指定交易日的归因，复用同一专用同步接口。这张表由 ZZShare 管理，不需要配置通用拉取 URL；请勿将其改成普通 HTTP 拉取表。
  - `POST /api/zzshare/limit-reasons/sync` 接受 `{"date":"YYYY-MM-DD"}`。首次成功写入该日快照，同日重复同步读取已有分区。无数据时可稍后重试。历史查询请指定分区日期。[上游接口文档](https://github.com/zzquant/zzshare)
  - **个股详情**：在图表底部的「当日涨停归因」查看来源、交易日和完整原因。点击日 K 的另一根蜡烛会切换归因日期；切到多日分时后按图上最近有记录的交易日展示。
  - **连板梯队 → 涨停**：选择已同步归因的日期，点击股票下方的「涨停归因」展开全文，或点击股票打开以该交易日为截止日的日 K 详情。梯队和详情按同日、同代码读取本地快照，不会自动同步或借用其他日期；没有归因记录不改变板数、名单或计数。
  - **侧栏 → 题材雷达**（`/zzshare-radar`）：这是原生页面。若菜单被隐藏，在「设置 → 页面管理 → 菜单与页面」启用。雷达复用 ZZShare 插件，无需切换主行情源或新建扩展表。
    - **今日强势题材**展示[自在量化首页](https://quant.zizizaizai.com/home)返回的席位顺序、强度和核心股。现行规则从策略池取前两席，再从全部候选中补一席。交易日盘中取官网最新榜，收盘榜按交易日查询。排名变化以同一题材 ID 在上一交易日官网收盘榜中的位置为基准。连续上榜按相邻交易日的官网前三计算，盘中计入当前观测的一天。历史记录不足时只展示能核验的天数下限。
    - **历史时点回看**支持查看本机当时实际保存的榜单。后端调度器在交易时段每 5 分钟采集一次，15:05 再尝试取正式收盘榜。指定截止时刻只读取该日当时或更早已采集的快照，连同当时已知的排名变化一起展示。安装前、停机期间等缺少观测的时段可查看当日收盘榜，盘中回看须以实际快照为准。相同内容去重保存。官网行情时点、官网计算时间和本机采集时间分别显示。
    - 日榜及实际观测保存在数据目录 `zzshare_home/v1/<交易日>/`。排名变化为上个交易日名次减当前名次，正值表示上升。连续天数最多向前核验 10 个相邻交易日，遇未发布或样本不足时显示已证实的下限。官网正式公布的零席、单席或两席均按实际发布的席位计算。
    - **本地板块强度榜**：选择日期后点击「计算题材排名」，从当日板块榜取候选，读取 `plates_stocks` 的同日成分与本地行情计算。首次可能需要数分钟，页面显示进度。点击前三卡片或「全部入围题材」里的题材名称，即可查看计算该榜时保存的股票池，在池内搜索、排序或打开个股详情。池内行情取所选交易日的本地数据，可能晚于榜单计算时刻；没有当日报价的成员仍列出。板块和全市场等权分时使用该日已有分钟数据，缺少分钟数据不影响查看名单。
    - **股票池接口**：`GET /api/zzshare/themes/{plate_code}/members?date=YYYY-MM-DD` 返回按交易日归档的成分及本地行情；同路径下的 `intraday` 返回分时对比。两者均只读，不需要重新向供应商拉取成分。历史日期未保存成分时不会用今天的股票池补足。
    - **官网榜接口**为 `GET /api/zzshare/home-topics?date=YYYY-MM-DD`。历史时点加 `as_of`，例如 `2026-11-04T10:30:00%2B08:00`。默认响应中的 `points` 提供该日可回看的本地采集时刻。旧版 `/themes` 接口和本地板块快照继续沿用。
    - **评分**：有效成分至少 5 只且占完整成分至少 80%，平均涨幅为正且至少一半有效成分上涨方可入围。以有效成分为分母，均涨幅占 40 分（涨 5% 满分），上涨比例占 20 分，涨幅严格超过 5% 的比例占 20 分（占 30% 满分），涨停比例占 20 分（占 10% 满分）；各项线性计分并封顶。涨跌停用系统行情引擎写入的同日标记。龙头为涨幅第一，中军为上涨股中成交额最大者，两者可为同一股。缺失、停牌等无有效交易的股票不填零，只影响覆盖率。
    - **市场情绪 / 个股热榜**：分别展示指定日分时模型点位、截至该日近 30 个自然日的情绪日 K，以及同花顺热榜。日 K 盘中仍会变化。热榜的榜单归属日与源更新时间分开展示；附带行情的更新时间跨日时不展示价格和涨幅。采集时刻不代表行情发生时刻。
    - **盘后龙虎榜**：读取所选日名单，按需展开买卖席位或截至该日的个股上榜历史。当天盘中显示待披露，不代入前日数据。雷达中的股票可打开对应交易日的个股详情。
    - **服务接口**：`GET /api/zzshare/themes?date=YYYY-MM-DD` 只读；`POST /api/zzshare/themes/sync` 接受 `{"date":"YYYY-MM-DD"}`，后台更新按日快照。候选成分与完成的榜单保存在数据目录的 `zzshare_radar/v1/<交易日>/`。研究查询使用 `GET /api/zzshare/research/<kind>?date=YYYY-MM-DD`；支持 `plates_list`、`plates_rank`（可加 `plate_type=14|15|17`，分别为行业、概念、题材）、`market_sentiment`、`sentiment_trend`、`ths_hot_top`、`lhb_list`、`lhb_detail` 和 `lhb_stock_history`（后两者须加 `code`）。插件另有 `sentiment_daily`（`v3/sentiment/data`，涨跌停、炸板、面/大面/天地板等盘后计数）和 `effect_daily`（`v3/api/sentiment/market/hot/day`，赚钱/亏钱效应）两个区间数据集，只供「超短线博弈」通过 `zzshare_research.fetch_range` 归档使用，不经过上述研究路由和缓存；详见 [超短线博弈](market-game.md#130-短线情绪与规则调整)。各资源在后端按参数合并并缓存请求；有同日旧数据才在上游故障时返回旧数据并标明延迟。

- **`backend/app/plugins/fuyao/`** — 同花顺官方 REST 数据源(runtime: none, 纯 HTTP 零依赖)
  - 提供 `realtime`(A 股全市场快照, 分页拉取)、`daily`(股票原始日K: ≤10 只优先批量历史接口; 更大范围近端走 daily-k-10d dump, 深窗口走 daily-k 10 年全量 dump(缓存复用、10d 补尾); dump 缺口或不可用时走批量接口, 每组最多10只、按10年自动分片; 漏组或批量失败回退旧单只接口)、`adj_factor`(事件 dump + 前收盘价从本地日K dump 一次取齐、缺价标的回退单标的接口, 按交易所公式推导单事件比值, 涨跌停自检; 全市场配价从逐标的 ~13 分钟降为秒级); Key 在设置页卡片直接配置(先探后存), 或 `.env` 配 `FUYAO_API_KEY`
  - `client.py` — httpx 客户端(X-api-key 认证 + 统一信封解包 + 分页 + 页间隔限频 + 单只/批量日K + dump 预签名下载, S3 下载不带 Key 头)
  - `provider.py` — Provider 实现(实测/文档双字段名映射、百分数→小数制、volume 股→手、上海零点戳 +8h 时区、dump 按 release 版本缓存、软失败、Key 探测)
  - `tests/test_fuyao_provider.py`、`tests/test_fuyao_historical_batch.py` — Provider 与批量历史契约测试, 是新插件的测试范本
- **`backend/app/plugins/tdxquant/`** — 通达信量化数据服务(runtime: python; `tdxaidata` 已是后端默认依赖, ctypes 包装的私有 TCP 协议, wheel 自带跨平台原生库)
  - 提供 `realtime`(五市场全 A 股票池枚举 + 600 只/批快照, 批间失败隔离)、`depth5`(快照自带买卖五档数组, 手口径透传)、`daily`(count 模式单标的拉取, 原始价, volume 股→手, amount 万元→元, `iter_daily` 逐标的流式)、`adj_factor`(事件接口每10股口径 ÷10 转每股 + 本地K线一次配价, 交易所公式推导单事件比值, 涨跌停自检)、`minute`(1m count 模式单标的, 240 根/日, 深度约 40 交易日, datetime=Date+Time 拼北京墙钟 naive, 与日K同口径 股→手/万元→元)、`full_minute`(修复轮 `get_intraday_batch` 当日窗口 + 稳态增量 `get_intraday_latest` 每股最新 N 根; 无批量端点, 全市场一轮 10 分钟量级); 指数快照直接支持(上证指数走 TDX 999999.SH 别名, `get_realtime_indices` 映射); 快照无名称列由维表关联; 未声明 financial → 回退 TickFlow; 清单声明 `billing: points` + `key_manage_url`(商城积分明细入口)
  - `client.py` — 原生层封装(lib 私有物化 + token 注入双 ini + `TDX_AI_DATA_LIB` 指向、空闲首连 "Invalid server response" 重试包装、股票池 12h 缓存)
  - `provider.py` — Provider 实现(快照昨收=LastClose/高低=Max/Min 字段映射、涨跌幅 (Now-LastClose)/LastClose 小数制、RefreshTime 北京墙钟→epoch ms、软失败)
  - `probe.py` — 候选 Key 探测的独立子进程(原生 DLL 每进程单 Key): 分阶段进度留痕定位原生层死点; 服务端明确拒绝凭据或额度时不保存。因 TDX 会话按 IP 复用(错误 Key 在活跃会话下也能拉数), 通过探测仅代表格式与连通性检查通过, 不保证所有数据接口均有权限。

TdxQuant 在 SDK 清理响应前检查服务端错误, 保留错误原因并脱敏; 明确的凭据/额度拒绝不重试, 实时分钟轮立即熔断。历史分钟请求失败会通过 `app.data_providers.base.ProviderRequestError` 传递到任务层并标记失败, 不转换成成功的 0 行或跨源重试; 其他插件也可用该异常显式声明请求失败。带时区的分钟请求窗口在插件边界归一为北京墙钟, 再与原生分钟列比较。正常停牌、休市或窗口内无数据的空结果保留原语义。

Fuyao 股票批量历史日K固定请求 `interval=1d&adjust=none`，并核对响应口径、标的分组、必要字段与日期范围。按 `thscode` 对齐分组，合法空组不重复补查；漏组或批量端点无权限、限频、不可用时回退原单只接口，仍使用原价。单只接口明确返回 `code=1002` 且消息为当前代码的 `Unknown thscode: <symbol>` 时，仅跳过该源未覆盖的标的，保留其本地历史，由核心原有 `zero_row_out` 回报本轮 0 行；同一补查会话的后续时间片不再重复请求该代码。其他参数错误、权限、限频和网络失败仍抛 `ProviderRequestError`，现有流式同步丢弃本轮暂存数据，保留正式分区；设置页试拉显示错误。仅升级股票日K，指数/ETF 路由、除权因子计算、Key 优先级、配置和 Parquet schema 均不变；无新增缓存，沿用现有整包缓存及仓库发布、视图刷新链路。批量端点文档见 [Fuyao 批量历史 K 线](https://fuyao.aicubes.cn/docs/api-reference/prices/#historical-batch)。

除权事件也在原生 `get_cw_data` 响应上执行上述检查：SDK 1.2.2 的 `get_divid_factors` 会将服务端失败与正常无事件都转换为空 DataFrame，不能用 DataFrame 是否为空判断请求成功。插件按 SDK 的 `Date/Type/Value` 契约解析事件，保留每 10 股单位及闭区间日期筛选；正常无事件返回空列表，事件或配价请求失败则中止本轮、保留原有因子文件，并向任务层及数据源试拉返回明确错误。

- **`backend/app/plugins/meoz/`** — [猫爪数据](https://meoz.cn) / [猫头鹰数据](https://t.meoz.cn) HTTP API（runtime: none，两站共用 `MEOZ_API_KEY`，设置页先探后存）
  - 请求为 `POST {apikey, apiname, fields, params}`，响应 `data.fields` + `data.items` 二维数组；日内服务 `sz/sh.meoz.cn:6688`，历史服务 `hist/hist-vip.meoz.cn:6688`，连接失败只在同服务节点间切换。单次最多 6000 行且服务端静默截断，客户端把达到上限的结果当作截断（`MeozTruncatedError`），请求按“代码数 × 交易日 ≤ 5000 行”切分；全局约 3 次/秒节流，HTTP 429 按 `Retry-After` 重试。
  - `realtime`：股票用 `daily` 最新交易日盘中快照（全市场 1 个请求，超限时按 `stockbasic` 股票池分组；`stockbasic` 实测 `exchange` 恒为空，按交易所过滤无结果，股票池全量查询、超限时按上市板块分段）；ETF 由 `get_realtime_symbols` 走 `etf_daily`，指数由 `get_realtime_indices` 走 `indexdaily`。`pct_chg` 百分数 ÷100，`vol` 文档为手直接透传；`pre_close` 为前复权可比昨收（除权日即参考价）。快照交易日早于今日时时间戳记为该日 15:00，交给下游按日期过滤。指数成交量单位官方未确认，置 `None`。未用 `screening`（文档登记仅专业版/旗舰版）。
  - `daily`：股票 `daily`、ETF `etf_daily` 原始价，收盘前丢弃最新交易日的盘中行；指数日K成交量单位未确认，空手而归由上层回退。
  - `adj_factor`：`adj_factor` 是累计复权因子，相邻交易日比值即单事件 `ex_factor`（与除权除息明细示例 `dr` 同向）。标的较多时先比较窗口前一交易日与末日的全市场因子，只为有变化的标的拉序列；比值小于 1 说明口径与契约相反，抛 `ProviderRequestError` 整轮不写入。ETF 无因子接口，返回空并由核心回退。
  - `minute` / `full_minute`：猫头鹰 `minute` / `etf_minute`。`trademin` 为收盘桶（0930 含集合竞价、0931–1130、1301–1500 共 241 根），分钟时间取 `trademin`（实测日内服务的 `time` 是桶内最后一笔时刻，如 09:25:01），声明 `minute_time_grid=auction_end`；`vol` 为相邻累计量差分，按 `vwap` 公式口径为手。当日行走日内服务并声明 `minute_price_basis=raw`（接口无复权参数，由实时累计行情差分生成，与 TickFlow 日内同理）；历史沉淀分钟官方未写明价格口径，不声明。历史分钟按“交易日 × 24 只”请求；全量分钟修复轮 24 只/请求，稳态增量轮按交易分钟不传代码取全市场，每根 1 个请求。指数分钟成交量单位未确认，不提供。
  - 未声明 `depth5`（分笔快照委托量单位未写明）与 `financial`，按能力路由使用其他源。测试见 `tests/test_meoz_provider.py`（假客户端与 `httpx.MockTransport`，不访问网络）。

## 路由机制(无需关心, 仅参考)

后端启动时, `loader.py` 扫描 `plugins/` 目录:
1. 读每个子目录的 `plugin.yaml`
2. `hidden: true` → 跳过(不注册不展示); 否则调 `check` 函数检测可用性
3. 可用 → 动态 import `entry` 指向的 Provider 类 → 注册进 `_PROVIDERS`
4. 不可用 → 记录状态, 设置页显示但不可切换

注册后, 插件和用户 YAML 自定义源走**完全相同的路由路径**(services 层的
`provider_has_dataset` / `get_provider` 调用), 无需额外集成代码。
