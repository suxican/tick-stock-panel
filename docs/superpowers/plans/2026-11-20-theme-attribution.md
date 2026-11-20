# 阶段 3 C 包：题材归因接入 实施计划（草案，待设计确认后执行）

设计见 `docs/superpowers/specs/2026-11-20-theme-attribution-design.md`。口径问题 1–6 确认前不动代码；下列以「建议答案」为前提，答案变化时对应任务调整。

TDD：每个任务先写失败测试再实现。命令：`cd backend && uv run pytest <路径> -q`、`uv run ruff check <文件>`、`cd frontend && pnpm exec vitest run <文件>`、`pnpm build`（用 `pnpm exec`，不用 npx）。

## 任务 1：provider 取数与标准化（`backend/app/plugins/meoz/provider.py`）
- 测试（假 client）：`get_theme_reasons` 合并 xgb/jygs，字段 `source/theme_id/name/reason`，某来源 502 时该来源标记失败而不是空；`get_hot_stocks("ths&normal")` live 以行序生成 rank、历史用返回 rank；`get_theme_hot` 解析 `updated_at`；空结果与 1002 返回空列表。
- 实现上述三个方法，复用 `_get_client`、重试 502（最多 2 次）。

## 任务 2：`services/meoz_theme_reasons.py`
- 测试：盘中不落盘且 60 秒缓存；15:30 后首次读取落盘、再次读取不请求；历史日 < 2026-01-05 返回空；未来日期 ValueError；插件不可用抛 `ReasonsUnavailableError`；部分来源失败时结果标注 `unavailable_sources`，不写留档（避免残缺定版）。

## 任务 3：`services/meoz_hot_rank.py`
- 测试：三榜单各自独立失败；每行带 `source`、`fetched_at`；`theme_hot_kp` 带 `available_at=updated_at`；未配置猫爪返回 `{available: false}`。

## 任务 4：路由（`backend/app/custom/meoz_reasons.py`）
- 测试：成功、无数据、422、503、502 响应契约，不泄露内部细节。

## 任务 5：前端数据层
- `api.ts` 类型与方法、`queryKeys.ts`（日期进键）、`useMeozThemeReasons`/`useMeozHotRank`；vitest：503/502 静默为空、定版后不再轮询、切换日期不串缓存。

## 任务 6：概念分析页归因面板
- 组件测试：加载、空、错、「来源不可用」与「无归因」文案区分；精确同名匹配出现提示，不匹配只在面板列出。

## 任务 7：首板人气补充
- `PriorityCell` 展开区增加东财人气/同花顺飙升行；测试：未上榜、不可用、就绪三态；断言优先分与候选判定输出不变（回归）。

## 任务 8：文档与验证
- 更新 `docs/data-directory.md`；运行受影响后端测试、`ruff check`、`pnpm build`、`git diff --check`；用临时数据目录做页面联调（见记忆「E2E preview with temp data dir」）。
