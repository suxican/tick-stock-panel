# 情绪 × 资金矩阵验证 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 离线校准新增 9 个"三档环境分 × 总量档位"格子与 6 对影子对比，在博弈页"规则可信度"卡中显示。

**Architecture:** 只改 `market_game_calibration.py`：`prepare_history` 透传 `state`，`_shadow_keys` 追加格键，`SHADOW_DEFINITIONS` 追加 6 对。可信度摘要与前端按定义通用渲染，不改。

**Tech Stack:** Python 3.11、Polars、pytest（`uv run`）。

**Spec:** `docs/superpowers/specs/2026-11-15-sentiment-capital-matrix-design.md`

## Global Constraints

- 原有规则分组、资金三组、`coverage`、`complete_inputs` 结果不变。
- `state` 或 `capital_volume` 为空（含旧 schema 缺列）时不产生矩阵键。
- 情绪三档映射：`strong`、`lean_strong` → `strong`；`range` → `range`；`lean_weak`、`weak` → `weak`。
- `card` 统一为 `'情绪×资金矩阵'`。
- 提交用 `git commit -s`；工作区 CRLF。

## Review Focus

1. `state` 出现未知值（如未来新增档位）：不应抛异常，也不应落格。→ Task 1 `test_matrix_shadow_skips_missing_or_unknown`。
2. 同一格出现在两对中：可信度摘要按定义各自取组，不应重复计天数。→ Task 1 断言 `summarize` 中两行的 `n` 均等于格子天数。

---

### Task 1: 矩阵影子分组

**Files:**
- Modify: `backend/app/services/market_game_calibration.py`
- Modify: `docs/market-game.md`、`docs/market-capital.md`
- Test: `backend/tests/test_market_game_calibration.py`

**Interfaces:**
- Produces: 常量 `MATRIX_SENTIMENT = {'strong': 'strong', 'lean_strong': 'strong', 'range': 'range', 'lean_weak': 'weak', 'weak': 'weak'}`；格键 `'shadow:matrix:' + tier + '_' + volume`。

6 对定义（group / control / label / definition）：

| group | control | label | definition |
| --- | --- | --- | --- |
| `shadow:matrix:strong_expand` | `shadow:matrix:strong_shrink` | 强势下放量 | 环境分强势或偏强且资金放量；对照组为同档资金缩量 |
| `shadow:matrix:range_expand` | `shadow:matrix:range_shrink` | 震荡下放量 | 环境分震荡且资金放量；对照组为同档资金缩量 |
| `shadow:matrix:weak_expand` | `shadow:matrix:weak_shrink` | 弱势下放量 | 环境分偏弱或弱势且资金放量；对照组为同档资金缩量 |
| `shadow:matrix:strong_expand` | `shadow:matrix:weak_expand` | 放量下情绪强 | 资金放量且环境分强势或偏强；对照组为资金放量且偏弱或弱势 |
| `shadow:matrix:strong_flat` | `shadow:matrix:weak_flat` | 平量下情绪强 | 资金平量且环境分强势或偏强；对照组为资金平量且偏弱或弱势 |
| `shadow:matrix:strong_shrink` | `shadow:matrix:weak_shrink` | 缩量下情绪强 | 资金缩量且环境分强势或偏强；对照组为资金缩量且偏弱或弱势 |

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.parametrize('state,volume,key', [
    ('strong', 'expand', 'shadow:matrix:strong_expand'), ('lean_strong', 'flat', 'shadow:matrix:strong_flat'),
    ('range', 'shrink', 'shadow:matrix:range_shrink'), ('lean_weak', 'expand', 'shadow:matrix:weak_expand'),
    ('weak', 'shrink', 'shadow:matrix:weak_shrink'),
])
def test_matrix_shadow_cell(state, volume, key):
    frame = _capital(history(10), 5, state=state, capital_volume=volume)
    assert key in _shadow(calibrate_history(frame))[5]

def test_matrix_shadow_skips_missing_or_unknown():
    for cols in ({'state': 'strong'}, {'capital_volume': 'expand'}, {'state': 'mystery', 'capital_volume': 'expand'}):
        keys = _shadow(calibrate_history(_capital(history(10), 5, **cols)))[5]
        assert not any(k.startswith('shadow:matrix') for k in keys)

def test_matrix_cell_shared_by_two_pairs_counts_once():
    # 用 summarize 校验 strong_expand 在两行中的 n 相同且等于格子天数
```

`history()` 无 `state` 列；`_capital` 会把非目标日设为 null。最后一个用例构造 60 天、分段日在中间，令若干天为 strong_expand / strong_shrink / weak_expand，调用 `market_game_credibility.summarize`，断言"强势下放量"与"放量下情绪强"两行的训练段 `n` 相同。同时更新 `test_shadow_groups_split_and_publish_fixed_definitions` 的期望集合，加入 6 个 group（`strong_expand` 出现两次，集合中计一次）。

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run pytest tests/test_market_game_calibration.py -q`
Expected: 新用例与被修改用例失败。

- [ ] **Step 3: 实现**（按 Interfaces 与上表）

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && uv run pytest tests -q -k "market_game" && uv run ruff check app/services/market_game_calibration.py tests/test_market_game_calibration.py`
Expected: 全部通过，Ruff 无告警。

- [ ] **Step 5: 真实数据校准并写文档**

Run: `cd backend && uv run --frozen python -m scripts.calibrate_market_game --start 2020-01-02 --split 2023-01-01 --archive --output ../tmp/matrix-calibration.json`
把 6 对的天数、训练→测试差值与结论写入 `docs/market-capital.md` 新节，`docs/market-game.md` 分组表追加 6 行。

- [ ] **Step 6: 提交**

```bash
git add backend/app/services/market_game_calibration.py backend/tests/test_market_game_calibration.py docs/market-capital.md docs/market-game.md docs/superpowers/specs/2026-11-15-sentiment-capital-matrix-design.md docs/superpowers/plans/2026-11-15-sentiment-capital-matrix.md
git commit -s -m "feat(market-game): 校准新增情绪×资金矩阵影子分组"
```
