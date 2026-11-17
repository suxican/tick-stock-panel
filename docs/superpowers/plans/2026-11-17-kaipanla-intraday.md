# 开盘啦盘中情绪/连板接入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把开盘啦盘中的涨停原因、题材集中度、情绪值补进现有「连板梯队」页，并按交易日留档，默认关闭。

**Architecture:** 后端照 cninfo 范式写一个不注册为数据源的 HTTP 客户端（`app/plugins/kaipanla`），照 zzshare_home 范式写带 TTL 合并与留档的服务（`app/services/kaipanla_intraday.py`），照 `app/custom/zzshare.py` 写扩展路由与定时采集（`app/custom/kaipanla.py`）。前端新建 `custom/kaipanla` 扩展只放设置开关，梯队页叠加情绪条、题材行与原因来源。

**Tech Stack:** Python 3.12 / FastAPI / httpx / pytest；React + TypeScript / @tanstack/react-query / vitest。

**Spec:** `docs/superpowers/specs/2026-11-16-kaipanla-intraday-design.md`（§10.5 的修正优先于 §5）

## Global Constraints

- 开关偏好键 `kaipanla_enabled`，默认 `False`；关闭时不发任何开盘啦请求。
- 不传 `Token`/`UserID`；公共参数 `PhoneOSNew=2`、`VerSion=5.23.0.1`、`apiv=w44`、持久化 `DeviceID`。
- 今日地址 `https://apphwhq.longhuvip.com/w1/api/index.php`，历史地址 `https://apphis.longhuvip.com/w1/api/index.php`。
- 客户端串行、最小请求间隔 1.0 秒、单请求超时 10 秒；服务 TTL 30 秒；前端过期警示阈值 120 秒；前端当日轮询 30 秒。
- 留档目录 `data/kaipanla/v1/<YYYY-MM-DD>/`：`observations.jsonl`、`close.json`（15:05 后定版，不覆写）、`expression.json`（取到后不覆写）；设备 ID 在 `data/kaipanla/device_id`。
- 涨跌幅、比率一律小数制（`10.03%` → `0.1003`），金额单位元，时间为北京时间 ISO 字符串（带 `+08:00`）。
- 厂商模型值字段加 `kpl_` 前缀；开盘啦数据不得进入回测、因子、策略、监控规则。
- 不修改 `/api/screener/limit-ladder` 返回结构。
- 与 spec 的两处取舍（计划作者的决定）：开关卡片放在 `settings.system.section` 插槽，而不是改核心数据源设置页（保持 L2、不碰核心页面）；开关读写放在扩展自己的路由里，不改 `app/api/settings.py`。
- 工作区 CRLF；前端只用 `pnpm exec`，不用 npx。后端改动在兄弟 worktree 中进行（改后端会打断正在运行的 dev 栈）。
- 后端测试：`backend/.venv/Scripts/python.exe -m pytest <path> -q`（在 `backend/` 下）；前端测试：`pnpm exec vitest run <path>`（在 `frontend/` 下）。

## Review Focus

1. 北交所 `920xxx`/`8xxxxx`/`4xxxxx`、科创 `688xxx`、创业板 `30xxxx` 代码 → 后缀分别为 `.BJ`/`.SH`/`.SZ`，梯队页能按 symbol 匹配上原因（Task 1 测试）。
2. 开盘前或无人涨停时各列表为空 → 快照计数为 0、`themes=[]`，不报错、不进入 stale（Task 2 测试）。
3. 首次请求即断网 → `/intraday` 返回 503，梯队页只显示一行灰色不可用提示，梯队本身照常（Task 3、Task 5 测试）。
4. 页面轮询与定时采集在 30 秒内同时触发 → 上游只请求一轮（Task 2 测试）。
5. 15:05 采集被重复触发或重启后再次触发 → `close.json` 不被覆写；历史日期无留档 → 404，情绪条静默隐藏（Task 2、Task 3、Task 5 测试）。

---

### Task 1: 开盘啦客户端与字段标准化

**Files:**
- Create: `backend/app/plugins/kaipanla/__init__.py`（模块说明：非官方接口、无 `plugin.yaml`、只供 `kaipanla_intraday` 调用）
- Create: `backend/app/plugins/kaipanla/client.py`
- Create: `backend/tests/fixtures/kaipanla/`（从 `C:\Users\suxic\AppData\Local\Temp\kpl_probe_out\` 复制并改名：`r2_limit_index_hq.json→limit_index.json`、`r2_limit_perf_hq_p1..p5.json→limit_up_p1..p5.json`、`r2_broken_hq_p1.json→broken_p1.json`、`r2_strong_hq.json→strong.json`、`r2_zdnum_hq.json→zdnum.json`、`x_expr_his_y.json→expression_2026-11-16.json`、`r1_expr_his.json→expression_1020.json`、`r1_expr_hq.json→expression_garbage.json`）
- Test: `backend/tests/test_kaipanla_client.py`

**Interfaces:**
- Produces:
  - `class KaipanlaError(RuntimeError)`
  - `def to_symbol(code: str) -> str`
  - `class KaipanlaClient(device_id: str, *, http: httpx.Client | None = None, min_interval: float = 1.0, timeout: float = 10.0)`，方法：
    - `limit_counts() -> list[int]`：`HomeDingPan/DailyLimitIndex`，5 个整数（一板…五板及以上）
    - `limit_up_stocks(pid: int) -> tuple[str, list[dict]]`：`HomeDingPan/DailyLimitPerformance`，`PidType=pid, Type=4, Index=0, Order=0, st=1000`；返回 `(交易日, 行)`
    - `first_board_broken() -> list[dict]`：`HomeDingPan/DailyLimitPerformance2`，`PidType=1, Type=5, Index=0, Order=1, st=1000`
    - `sentiment() -> dict`：`HomeDingPan/ChangeStatistics` → `{"day", "kpl_strong": int, "limit_up": int, "max_boards": int, "sharp_drawdown": int}`
    - `limit_totals() -> dict`：`HomeDingPan/MarketStockZDNum` → `{"limit_up": int, "limit_down": int}`
    - `expression(day: date) -> dict | None`：`apphis` `HisHomeDingPan/ZhangTingExpression`，`Day=YYYY-MM-DD`；`errcode=="1020"` 返回 `None`
  - 涨停行 dict 键：`symbol, name, boards, first_limit_at, reason, theme_code, plates(list[str]), seal_amount, max_seal, reseal(bool), label(str|None), price, change_pct`（索引：`[0][1][15][4][5][19][12]按「、」拆[6][7][16][18]空串→None[21][22]/100`）
  - 首板破板行 dict 键：`symbol, name, price, change_pct, plates, last_broken_at, max_seal`（索引 `[0][1][4][5]/100 [6] [13] [17]`）
  - expression dict 键：`limit_up, boards_2, boards_3, max_boards, kpl_promo_2, kpl_promo_3, kpl_promo_top, kpl_broken_rate, kpl_yest_limit_perf, kpl_yest_board_perf, kpl_yest_broken_perf, kpl_summary`（索引 0–11，百分数 /100）

- [ ] **Step 1: 写失败测试**（用 `httpx.MockTransport` 按 `a` 参数返回 fixture 内容）

```python
def test_to_symbol_suffixes():
    assert [to_symbol(c) for c in ("600881", "688227", "002451", "300824", "920118", "830799", "430047")] == \
        ["600881.SH", "688227.SH", "002451.SZ", "300824.SZ", "920118.BJ", "830799.BJ", "430047.BJ"]

def test_limit_up_rows_are_normalized():
    day, rows = client.limit_up_stocks(1)
    assert day == "2026-11-17" and len(rows) == 35
    first = rows[0]
    assert first["symbol"] == "002451.SZ" and first["boards"] == 1 and first["reason"] == "电气设备"
    assert first["theme_code"] == "801562" and first["plates"] == ["电气设备", "智能电网"]
    assert first["first_limit_at"] == "2026-11-17T09:25:00+08:00" and first["change_pct"] == pytest.approx(0.1003)
    assert first["label"] is None and first["reseal"] is False

def test_higher_board_rows_carry_board_count():
    assert {r["boards"] for r in client.limit_up_stocks(4)[1]} == {4}

def test_sentiment_and_totals():
    assert client.sentiment()["kpl_strong"] == 36 and client.limit_totals()["limit_down"] == 44

def test_expression_parses_history_and_maps_1020_to_none():
    exp = client.expression(date(2026, 11, 16))
    assert exp["boards_2"] == 8 and exp["kpl_promo_2"] == pytest.approx(0.266667, rel=1e-4)
    assert client_returning("expression_1020.json").expression(date(2026, 11, 17)) is None

def test_garbage_or_error_payload_raises():
    for fixture in ("expression_garbage.json",):  # hq 占位垃圾当作 limit_counts 返回
        with pytest.raises(KaipanlaError):
            client_returning(fixture).limit_counts()
    with pytest.raises(KaipanlaError):
        client_returning_status(500).sentiment()

def test_requests_are_spaced_and_carry_common_params(monkeypatch):
    # 连续两次调用之间 sleep >= min_interval; 请求带 DeviceID/PhoneOSNew=2/VerSion/apiv, 不带 Token/UserID
```

- [ ] **Step 2: 运行确认失败** — `backend/.venv/Scripts/python.exe -m pytest tests/test_kaipanla_client.py -q`，预期 ImportError。
- [ ] **Step 3: 实现 `client.py`**：GET/POST 与 spec 文档一致（`limit_*`/`sentiment` 用 GET，`limit_totals` 用 POST）；结构不符（非 dict、`errcode` 非 `"0"`、`info` 形状不对、数值字段不是数字）一律抛 `KaipanlaError`；`to_symbol` 规则：`60/68/90` 开头 → `.SH`，`4/8/92` 开头 → `.BJ`，其余 → `.SZ`。
- [ ] **Step 4: 运行确认通过** — 同上命令，全部 PASS。
- [ ] **Step 5: 提交** — `git add backend/app/plugins/kaipanla backend/tests/fixtures/kaipanla backend/tests/test_kaipanla_client.py && git commit -m "feat(kaipanla): 开盘啦非官方接口客户端与字段标准化"`

---

### Task 2: 盘中快照服务与留档

**Files:**
- Create: `backend/app/services/kaipanla_intraday.py`
- Test: `backend/tests/test_kaipanla_intraday.py`

**Interfaces:**
- Consumes: Task 1 的 `KaipanlaClient`、`KaipanlaError`
- Produces:
  - `class KaipanlaUnavailableError(RuntimeError)`
  - `def read_intraday(data_dir: Path, *, now: datetime | None = None, client: KaipanlaClient | None = None) -> dict` — 当日快照（TTL 30 秒合并；并发调用共享一轮）
  - `def read_day(data_dir: Path, day: date) -> dict | None` — 历史日：`close.json` 内容加 `expression`（有则附上），无 `close.json` 返回 `None`
  - `def capture(data_dir: Path, now: datetime, *, client: KaipanlaClient | None = None) -> None` — 定时入口
  - `def status() -> dict` — `{"last_success_at": str | None, "last_error": str | None}`
  - 快照结构：
    ```
    {"date", "fetched_at", "stale_seconds": int,
     "summary": {"kpl_strong", "limit_up", "limit_down", "max_boards", "sharp_drawdown",
                 "first_board_broken", "broken_rate"},
     "tiers": [{"boards", "count"}],        # 来自 limit_counts，boards 1..5（5 表示 5 板及以上）
     "stocks": [涨停行 dict, ...],            # PidType 1..5 合并
     "themes": [{"code", "name", "count", "symbols"}],  # 按 theme_code 聚合，name 取 reason，count 降序
     "prev_expression": {...} | None,       # 早于当日的最近一个 expression.json
     "missing": [str, ...]}
    ```
  - `broken_rate = first_board_broken / (limit_up + first_board_broken)`，分母为 0 时为 `None`

- [ ] **Step 1: 写失败测试**（用 `Mock(spec=KaipanlaClient)` 注入，`now` 固定为 `2026-11-17 10:30+08:00`）

```python
def test_snapshot_aggregates_themes_and_tiers(tmp_path):
    snap = read_intraday(tmp_path, now=NOW, client=fake_client())
    assert snap["summary"]["limit_up"] == 41 and snap["tiers"][0] == {"boards": 1, "count": 35}
    assert snap["themes"][0]["count"] >= snap["themes"][-1]["count"] and snap["missing"] == []
    assert snap["summary"]["broken_rate"] == pytest.approx(10 / (41 + 10))

def test_empty_lists_are_a_valid_snapshot(tmp_path):  # Review Focus 2
    snap = read_intraday(tmp_path, now=NOW, client=fake_client(empty=True))
    assert snap["summary"]["limit_up"] == 0 and snap["themes"] == [] and snap["stale_seconds"] == 0

def test_secondary_failure_goes_to_missing(tmp_path):
    snap = read_intraday(tmp_path, now=NOW, client=fake_client(fail={"sentiment"}))
    assert "kpl_strong" in snap["missing"] and snap["summary"]["kpl_strong"] is None

def test_core_failure_returns_previous_snapshot_as_stale(tmp_path):
    read_intraday(tmp_path, now=NOW, client=fake_client())
    snap = read_intraday(tmp_path, now=NOW + timedelta(seconds=95), client=fake_client(fail={"limit_up_stocks"}))
    assert snap["stale_seconds"] == 95

def test_never_succeeded_raises_unavailable(tmp_path):  # Review Focus 3
    with pytest.raises(KaipanlaUnavailableError):
        read_intraday(tmp_path, now=NOW, client=fake_client(fail={"limit_up_stocks"}))

def test_concurrent_reads_within_ttl_share_one_round(tmp_path):  # Review Focus 4
    client = fake_client(); [read_intraday(tmp_path, now=NOW, client=client) for _ in range(3)]
    assert client.limit_up_stocks.call_count == 5  # PidType 1..5 各一次

def test_capture_appends_observation_in_session_and_freezes_close(tmp_path):  # Review Focus 5
    capture(tmp_path, NOW, client=fake_client())
    assert len((tmp_path / "kaipanla/v1/2026-11-17/observations.jsonl").read_text().splitlines()) == 1
    capture(tmp_path, CLOSE_1505, client=fake_client()); first = close_json(tmp_path)
    capture(tmp_path, CLOSE_1505 + timedelta(minutes=1), client=fake_client(limit_up=99))
    assert close_json(tmp_path) == first

def test_capture_backfills_expression_once(tmp_path):
    # 已有 2026-11-16/close.json 无 expression.json; client.expression 首次返回 None, 第二次返回 dict
    # → 第一次 capture 不写文件; 第二次写入; 第三次不再调用 client.expression

def test_capture_skips_lunch_and_non_session(tmp_path):
    # 12:00 与 08:00 调用 capture: 不请求 limit_up_stocks, 不写 observations.jsonl

def test_read_day_returns_none_without_archive(tmp_path):
    assert read_day(tmp_path, date(2026, 11, 13)) is None

def test_device_id_is_persisted(tmp_path):
    # 未注入 client 时由服务创建; 两次创建读到同一个 data/kaipanla/device_id
```

- [ ] **Step 2: 运行确认失败** — `... -m pytest tests/test_kaipanla_intraday.py -q`
- [ ] **Step 3: 实现**：合并请求与 TTL 照 `app/services/zzshare_home.py` 的 `_shared`；`close.json` 用 `app.services.fs_utils.atomic_write_text`，`observations.jsonl` 加锁追加；时段判断用 `app.market_time.in_continuous_session`；15:05 定版条件为 `now.time() >= time(15, 5)` 且 `close.json` 不存在；`capture` 每次先尝试补最近一个「有 `close.json`、无 `expression.json`、日期早于今天」的交易日；核心调用为 `limit_up_stocks`，其失败即整轮失败，其余调用失败记入 `missing`（`missing` 值用 summary 字段名）。
- [ ] **Step 4: 运行确认通过**
- [ ] **Step 5: 提交** — `git commit -m "feat(kaipanla): 盘中快照合并缓存与按日留档"`

---

### Task 3: 扩展路由、定时采集与开关

**Files:**
- Create: `backend/app/custom/kaipanla.py`
- Modify: `docs/data-directory.md`（在 `zzshare_radar/` 行后加 `kaipanla/` 一行：目录内容、何时写入、`close.json`/`expression.json` 不覆写、删除影响、非官方来源）
- Test: `backend/tests/test_kaipanla_routes.py`

**Interfaces:**
- Consumes: Task 2 的 `read_intraday`、`read_day`、`capture`、`status`、`KaipanlaUnavailableError`
- Produces（HTTP）:
  - `GET /api/kaipanla/status` → `{"enabled": bool, "last_success_at": str|null, "last_error": str|null}`
  - `PUT /api/kaipanla/enabled`，body `{"enabled": bool}` → `{"enabled": bool}`（`preferences.save({"kaipanla_enabled": ...})`）
  - `GET /api/kaipanla/intraday?date=YYYY-MM-DD`（可省略）→ 快照；未启用 409 `{"detail": "开盘啦数据未启用"}`；当日不可用 503 `{"detail": "开盘啦暂不可用：<原因>"}`；历史无档 404；日期晚于今天 422
  - `EXTENSION_ID = "kaipanla.intraday"`；`setup(registrar)` 调 `registrar.include_router(router)`

- [ ] **Step 1: 写失败测试**（照 `tests/test_zzshare_home_routes.py`：`FastAPI()` + `app.state.repo.store.data_dir=tmp_path` + `app.state.scheduler=Mock()`，monkeypatch 服务函数与 `preferences.load`）

```python
def test_disabled_returns_409_and_scheduler_does_not_request(...):
    # kaipanla_enabled=False: GET /intraday → 409; 调用注册的定时函数 → capture 未被调用
def test_today_uses_live_snapshot_and_history_reads_archive(...):
    # 不带 date → read_intraday 被调用; date=2026-11-16 → read_day 被调用, read_intraday 未调用
def test_unavailable_maps_to_503_and_missing_archive_to_404(...):  # Review Focus 3、5
def test_future_date_is_422(...):
def test_toggle_persists_preference(...):
    # PUT {"enabled": true} → preferences.save 收到 {"kaipanla_enabled": True}; GET /status 反映
def test_scheduler_job_registered_and_removed(...):
    # add_job 以 cron 注册, hour="9-15,21", minute="*/5", timezone="Asia/Shanghai", id="kaipanla_capture";
    # 生命周期结束 remove_job
```

- [ ] **Step 2: 运行确认失败**
- [ ] **Step 3: 实现**：生命周期照 `app/custom/zzshare.py::_lifespan`；定时函数 `_capture(data_dir)` 先查开关，再查 `is_trading_day(now) is not False`，然后调 `capture(data_dir, cn_now())`；路由同步函数里调用服务（与 zzshare 一致）。
- [ ] **Step 4: 运行确认通过**，并跑 `-m pytest tests/test_kaipanla_client.py tests/test_kaipanla_intraday.py tests/test_kaipanla_routes.py -q`
- [ ] **Step 5: 提交** — `git commit -m "feat(kaipanla): 扩展路由、定时采集与启用开关"`

---

### Task 4: 前端 API、查询 hook 与设置开关

**Files:**
- Modify: `frontend/src/lib/api.ts`（类型 + 方法）
- Modify: `frontend/src/lib/queryKeys.ts`
- Create: `frontend/src/lib/useKaipanla.ts`
- Create: `frontend/src/custom/kaipanla/extension.tsx`、`frontend/src/custom/kaipanla/KaipanlaSettings.tsx`
- Test: `frontend/src/custom/kaipanla/KaipanlaSettings.test.tsx`、`frontend/src/lib/useKaipanla.test.tsx`

**Interfaces:**
- Consumes: Task 3 的 HTTP 接口
- Produces:
  - 类型 `KplStock`、`KplTheme`、`KplSummary`、`KplExpression`、`KplSnapshot`（字段与 Task 2 快照结构一致）、`KplStatus`
  - `api.kaipanlaStatus(): Promise<KplStatus>`、`api.kaipanlaSetEnabled(enabled: boolean): Promise<{ enabled: boolean }>`、`api.kaipanlaIntraday(date?: string): Promise<KplSnapshot>`（均 `quiet: true`）
  - `QK.kaipanlaStatus = ['kaipanla', 'status']`、`QK.kaipanla(date?: string) = ['kaipanla', 'intraday', date ?? 'today']`
  - `useKaipanlaStatus()`；`useKaipanlaIntraday(date: string | undefined, enabled: boolean)`：`enabled=false` 不发请求；`date` 为空或等于 `cnToday()` 时 `refetchInterval: 30_000`，否则不轮询；404 时返回 `data=undefined` 且不重试
  - 扩展：`id: 'official.kaipanla'`，slot `settings.system.section` → `KaipanlaSettings`

- [ ] **Step 1: 写失败测试**

```tsx
it('设置卡片显示风险说明并切换开关', async () => {
  // mock api.kaipanlaStatus → {enabled:false,...}; 渲染 KaipanlaSettings
  // 断言文案「非官方接口，仅供个人研究；App 升级可能失效」; 点击开关 → api.kaipanlaSetEnabled(true)
})
it('显示最近成功时间与最近错误', async () => { /* status 带 last_error → 文案可见 */ })
it('未启用时不请求快照；当日轮询、历史不轮询', () => { /* useKaipanlaIntraday 的 query options */ })
```

- [ ] **Step 2: 运行确认失败** — `pnpm exec vitest run src/custom/kaipanla src/lib/useKaipanla.test.tsx`
- [ ] **Step 3: 实现**：设置卡片样式沿用设置页现有 section 组件；开关切换后失效 `['kaipanla']` 前缀查询。
- [ ] **Step 4: 运行确认通过**
- [ ] **Step 5: 提交** — `git commit -m "feat(kaipanla): 前端接口、查询 hook 与设置开关"`

---

### Task 5: 连板梯队页叠加情绪条、题材行与原因来源

**Files:**
- Create: `frontend/src/custom/limit-ladder/kaipanlaReasons.ts`
- Create: `frontend/src/custom/limit-ladder/KaipanlaStrip.tsx`
- Modify: `frontend/src/custom/limit-ladder/Page.tsx`（`StockCard` 原因渲染约 227、400 行；`TierGroup` 约 997、1163 行；数据查询约 1571 行；总览条下方约 1794 行插入情绪条）
- Test: `frontend/src/custom/limit-ladder/kaipanlaReasons.test.ts`、`frontend/src/custom/limit-ladder/KaipanlaStrip.test.tsx`

**Interfaces:**
- Consumes: Task 4 的 `useKaipanlaStatus`、`useKaipanlaIntraday`、`KplSnapshot`
- Produces:
  - `type LadderReason = { text: string; source: 'kpl' | 'zzshare'; alt?: string }`
  - `mergeReasons(zz: ReadonlyMap<string, string> | undefined, kpl: KplStock[] | undefined): Map<string, LadderReason>` — 有 zzshare 原因时用 zzshare，并把开盘啦原因放进 `alt`；只有开盘啦原因时用开盘啦
  - `KaipanlaStrip({ snapshot, error, onThemeSelect, selectedTheme })`
  - `TierGroup`/`StockCard` 的 `reasons`/`reason` 改为 `LadderReason`

- [ ] **Step 1: 写失败测试**

```ts
it('盘中只有开盘啦原因时显示开盘啦', () => {
  expect(mergeReasons(undefined, [stock('002451.SZ', '电气设备')]).get('002451.SZ'))
    .toEqual({ text: '电气设备', source: 'kpl' })
})
it('盘后两者都有时以 zzshare 为准, 开盘啦放 alt', () => {
  expect(mergeReasons(new Map([['002451.SZ', '智能电网订单']]), [stock('002451.SZ', '电气设备')]).get('002451.SZ'))
    .toEqual({ text: '智能电网订单', source: 'zzshare', alt: '电气设备' })
})
```

```tsx
it('情绪条显示 summary, 缺失项隐藏', () => { /* missing:['kpl_strong'] → 无「情绪」字样; 有「涨停 41」「最高 4 板」 */ })
it('stale_seconds>120 显示「数据已过期」', () => {})
it('不可用时一行灰色提示', () => { /* error='开盘啦暂不可用：超时' → 文案可见 */ })  // Review Focus 3
it('题材行最多 8 个, 点击回调 code, 再点取消', () => {})
it('有 prev_expression 时显示「昨日二板晋级 26.7%」', () => {})
```

- [ ] **Step 2: 运行确认失败** — `pnpm exec vitest run src/custom/limit-ladder`
- [ ] **Step 3: 实现**：
  - 情绪条一行：`开盘啦 情绪 {kpl_strong} · 涨停 {limit_up} · 跌停 {limit_down} · 最高 {max_boards} 板 · 破板率 {fmtPct(broken_rate,1)} · 昨日二板晋级 {fmtPct(kpl_promo_2,1)}`，右侧 `HH:mm 更新`；只在 `direction==='up'` 且开关开启时渲染；404（历史无档）不渲染。
  - 题材行：`themes` 前 8 个 `名称 数量`，选中后把 `sortedTiers` 每档的 `stocks` 过滤为 `symbols` 内的股票（与现有 `selectedTag` 独立，可叠加）。
  - 原因来源：`StockCard` 的「涨停归因」后加小字 `开` 或 `zz`（`title` 为「开盘啦（盘中）」/「ZZShare（盘后）」），`alt` 存在时展开区多一行「开盘啦：{alt}」。
  - 封单：开盘啦 `seal_amount`/`max_seal` 只放展开区，不动 `SealedBadge`。
- [ ] **Step 4: 运行确认通过**，并跑 `pnpm exec vitest run src/custom/limit-ladder src/custom/kaipanla src/lib` 与 `pnpm exec tsc --noEmit`
- [ ] **Step 5: 提交** — `git commit -m "feat(limit-ladder): 叠加开盘啦情绪条、题材行与原因来源"`

---

### Task 6: 整体验证

- [ ] **Step 1:** 后端全量：`backend/.venv/Scripts/python.exe -m pytest -q`，预期无新增失败（与基线对比）。
- [ ] **Step 2:** 前端全量：`pnpm exec vitest run` 与 `pnpm exec tsc --noEmit`。
- [ ] **Step 3:** 用临时数据目录（`DATA_DIR` + `TICKFLOW_ENV_FILE`）启动预览，交易时段打开 `/limit-ladder`：开关关闭时无情绪条且无开盘啦请求；开启后情绪条与原因出现，30 秒后更新时间变化；断网后显示过期/不可用提示而梯队照常。
- [ ] **Step 4:** 收盘后确认 `data/kaipanla/v1/<当日>/close.json` 生成；次日确认前一日 `expression.json` 补齐、梯队页显示昨日晋级率。
- [ ] **Step 5:** 截图与结论写入 PR 说明。
