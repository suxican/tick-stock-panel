# Windows 开发模式完整配置与启动指南

> 适用版本：Tick Stock Panel `v0.2.2`
> 适用终端：Windows PowerShell 5.1 或 PowerShell 7
> 默认后端端口：`3018`；默认前端端口：`3011`

本文用于在 Windows 上从零配置 Tick Stock Panel 源码开发环境。项目根目录自带的 `dev.ps1` 会管理 Python/Node 依赖，并同时启动 FastAPI 后端与 Vite 前端。

## 1. 启动后的组成

开发模式会运行两个本地服务：

| 服务 | 默认地址 | 用途 |
| --- | --- | --- |
| Vite 前端 | <http://localhost:3011> | 日常开发访问入口，支持前端热更新 |
| FastAPI 后端 | <http://localhost:3018> | REST API、SSE、后台任务与后端热重载 |
| 健康检查 | <http://localhost:3018/health> | 查看后端状态、版本和当前数据模式 |
| API 文档 | <http://localhost:3018/docs> | FastAPI 自动生成的接口文档 |

前端通过 Vite 代理把 `/api` 和 `/health` 请求转发到后端，因此开发时通常只需打开 `http://localhost:3011`。

## 2. 安装前准备

### 2.1 系统建议

- Windows 10 或 Windows 11 64 位。
- 建议至少 8 GB 内存；全量指标、回测和因子挖掘会使用更多内存。
- 项目和 `data/` 最好放在剩余空间充足的磁盘。
- 不建议把仓库放在自动同步或频繁扫描的目录中，以免 Parquet 文件被占用。

### 2.2 必需工具

| 工具 | 项目要求 | 建议 |
| --- | --- | --- |
| Git | 无特殊版本要求 | 使用当前稳定版 |
| Python | `>= 3.11` | Python 3.11 或 3.12 64 位 |
| Node.js | `>= 20` | 当前 LTS 64 位 |
| uv | 当前稳定版 | 管理后端虚拟环境与锁定依赖 |
| pnpm | 项目声明 `9.10.0` | 建议使用 pnpm 9 |

官方安装说明：

- Git：<https://git-scm.com/download/win>
- Python：<https://www.python.org/downloads/windows/>
- Node.js：<https://nodejs.org/en/download>
- uv：<https://docs.astral.sh/uv/getting-started/installation/>
- pnpm：<https://pnpm.io/installation>

## 3. 安装开发工具

以下命令都在 PowerShell 中执行。已有对应工具时可以跳过。

### 3.1 使用 winget 安装 Git、Python、Node.js 和 uv

```powershell
winget install --exact --id Git.Git
winget install --exact --id Python.Python.3.12
winget install --exact --id OpenJS.NodeJS.LTS
winget install --exact --id astral-sh.uv
```

安装完成后关闭并重新打开 PowerShell，让新的 `PATH` 生效。

如果系统没有 winget，可分别使用上一节的官方下载页面安装。Python 安装程序中建议勾选“Add Python to PATH”。

### 3.2 安装项目匹配的 pnpm

Node.js 安装完成后执行：

```powershell
npm install --global pnpm@9.10.0
```

### 3.3 检查工具链

```powershell
git --version
python --version
node --version
npm --version
uv --version
pnpm --version
```

至少确认：

- Python 输出为 `3.11.x`、`3.12.x` 或更高兼容版本。
- Node.js 输出为 `v20.x` 或更高版本。
- `uv` 和 `pnpm` 能直接执行，而不是提示“无法识别命令”。

若刚安装后仍找不到命令，先重新打开 PowerShell；仍无效时检查用户和系统 `PATH`。

## 4. 获取并进入项目

新安装：

```powershell
Set-Location E:\stockWorkSpace
git clone https://github.com/shy3130/tick-stock-panel.git
Set-Location .\tick-stock-panel
```

仓库已经存在时，直接进入项目根目录：

```powershell
Set-Location E:\stockWorkSpace\tick-stock-panel
```

确认当前位置正确：

```powershell
Get-ChildItem README.md, dev.ps1, backend, frontend
git status --short --branch
```

根目录应能看到 `README.md`、`dev.ps1`、`backend/` 和 `frontend/`。

## 5. 创建并配置 `.env`

### 5.1 首次创建

不要覆盖已有的 `.env`。首次配置时执行：

```powershell
if (-not (Test-Path -LiteralPath .env)) {
    Copy-Item -LiteralPath .env.example -Destination .env
}
notepad .env
```

`.env` 包含数据源 Key、AI Key、访问密码和路径等敏感配置，已被 Git 忽略，不要手动强制提交。

### 5.2 仅本机开发的推荐配置

```ini
# ===== TickFlow =====
# 留空仍可使用免费历史日 K 路径；实时、分钟、盘口、财务等能力按实际权限开放。
TICKFLOW_API_KEY=

# ===== AI（可选） =====
# 不使用 AI 时保持 AI_API_KEY 为空，不影响其他功能。
AI_PROVIDER=openai_compat
AI_BASE_URL=https://api.deepseek.com/v1
AI_API_KEY=
AI_MODEL=deepseek-chat
AI_DAILY_TOKEN_BUDGET=500000

# ===== Server =====
# 仅本机开发建议绑定 127.0.0.1；需要局域网访问时再改为 0.0.0.0。
HOST=127.0.0.1
PORT=3018
LOG_LEVEL=INFO

# ===== Auth =====
# 仅本机使用可留空；局域网或公网访问建议预置至少 6 位密码。
AUTH_PASSWORD=''

# ===== Optional backend dependencies =====
# 普通机器留空；老 CPU 用 legacy-cpu；可选 vectorbt 用 backtest。
BACKEND_EXTRAS=

# ===== Data =====
DATA_DIR=./data
```

### 5.3 配置项说明

#### TickFlow

```ini
TICKFLOW_API_KEY=
```

- 留空：启用免费历史日 K 路径，可以体验基础选股和回测。
- 填写 Key：启动后根据实际账号权限检测实时、分钟 K、五档盘口和财务等能力。
- 保存或修改 Key 后，在页面中执行“重新检测”。

#### AI

```ini
AI_PROVIDER=openai_compat
AI_BASE_URL=https://api.deepseek.com/v1
AI_API_KEY=
AI_MODEL=deepseek-chat
```

- AI 完全可选，Key 为空时只关闭 AI 相关功能。
- `openai_compat` 可连接兼容 OpenAI API 的服务。
- 使用 Ollama 时按设置页提示配置 `AI_PROVIDER=ollama`、地址和模型。
- 密钥也可以在项目设置页管理；不要把真实 Key 写进文档或提交记录。

#### 监听地址与端口

```ini
HOST=127.0.0.1
PORT=3018
```

- `127.0.0.1`：只允许本机访问，适合开发，安全性更高。
- `0.0.0.0`：监听所有网卡，适合局域网联调；需要同时处理 Windows 防火墙和访问密码。
- `PORT` 是后端端口。前端开发端口由 `dev.ps1` 参数或 `FRONTEND_PORT` 环境变量控制，默认是 3011。

#### 访问密码

```ini
AUTH_PASSWORD='至少六位密码'
```

- 仅在系统尚未设置过密码时用于首次初始化。
- 初始化后只保存密码哈希；之后修改密码应使用页面设置。
- 已经初始化过密码时，修改 `.env` 中该值不会覆盖现有密码。

#### 可选依赖

```ini
BACKEND_EXTRAS=
```

可用值：

```ini
BACKEND_EXTRAS=legacy-cpu
BACKEND_EXTRAS=backtest
BACKEND_EXTRAS=legacy-cpu backtest
```

- 普通新电脑先留空。
- 老 CPU 出现 AVX2/FMA 或 `exit 132` 问题时使用 `legacy-cpu`。
- `backtest` 会安装可选的 vectorbt；项目主流程不要求必须安装它。
- 只要设置了 extras，一键脚本每次启动前都会重新执行后端依赖同步。

#### 数据目录

```ini
DATA_DIR=./data
```

- 相对路径按项目根目录解析。
- 行情、财务、自选、策略、监控、回测结果和用户设置都保存在这里。
- 需要放到其他磁盘时，Windows 建议使用正斜杠绝对路径：

```ini
DATA_DIR=E:/TickStockData
```

- 迁移或清理前应停止服务并备份整个目录。

## 6. PowerShell 执行策略

先查看当前策略：

```powershell
Get-ExecutionPolicy -List
```

如果执行 `dev.ps1` 时提示禁止运行脚本，可选择其中一种方式。

只为当前一次启动临时放行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\dev.ps1
```

为当前 Windows 用户启用本地脚本：

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

不建议把策略设置为 `Unrestricted`。公司设备若受 `MachinePolicy` 或 `UserPolicy` 管理，应遵循组织策略，不要尝试绕过。

## 7. 一键启动

### 7.1 启动前检查端口

`dev.ps1` 会主动终止占用目标端口的进程树。运行前可先检查：

```powershell
Get-NetTCPConnection -State Listen -LocalPort 3018,3011 -ErrorAction SilentlyContinue |
    Select-Object LocalAddress, LocalPort, OwningProcess
```

如果端口上有需要保留的服务，不要直接运行默认命令，改用其他端口。

### 7.2 使用默认端口启动

在项目根目录执行：

```powershell
.\dev.ps1
```

第一次运行时脚本会：

1. 检查 `uv` 和 `pnpm`。
2. 读取根目录 `.env` 中的 `HOST`、`PORT` 和 `BACKEND_EXTRAS`。
3. 释放后端 3018 与前端 3011 端口。
4. 如果 `backend/.venv` 不存在，执行 `uv sync --frozen`。
5. 如果 `frontend/node_modules` 不存在，执行 `pnpm install`。
6. 启动 Uvicorn：`app.main:app`，开启后端热重载。
7. 启动 Vite，并把 API 请求代理到同一后端端口。
8. 在当前窗口持续输出带 `[backend]`、`[frontend]` 前缀的日志。

依赖首次下载可能需要数分钟。看到以下地址后即可访问：

```text
backend   http://localhost:3018
frontend  http://localhost:3011
```

### 7.3 使用自定义端口启动

命令行参数优先级最高：

```powershell
.\dev.ps1 -BackendPort 8000 -FrontendPort 5173
```

也可以仅对当前 PowerShell 会话设置：

```powershell
$env:BACKEND_PORT = '8000'
$env:FRONTEND_PORT = '5173'
.\dev.ps1
```

后端端口优先级为：

```text
-BackendPort 参数
  > BACKEND_PORT 环境变量
  > PORT 环境变量
  > .env 中的 PORT
  > 默认 3018
```

前端端口优先级为：

```text
-FrontendPort 参数
  > FRONTEND_PORT 环境变量
  > 默认 3011
```

环境变量只对当前终端临时覆盖时，关闭 PowerShell 后会恢复。

## 8. 验证启动结果

### 8.1 验证后端

新开一个 PowerShell 窗口执行：

```powershell
Invoke-RestMethod http://127.0.0.1:3018/health | ConvertTo-Json
```

正常结果类似：

```json
{
  "status": "ok",
  "version": "0.2.2",
  "mode": "none"
}
```

`mode` 会随 TickFlow Key 和账号状态变化，不要求必须是某个固定值。

### 8.2 验证前端

浏览器打开：

```text
http://127.0.0.1:3011
```

正常情况会进入首次配置向导、登录页或主界面。浏览器直接访问后端接口文档：

```text
http://127.0.0.1:3018/docs
```

### 8.3 查看日志

- 一键启动终端：同时显示前后端实时日志。
- 后端持久日志：`data/backend.log`。
- 前端错误：浏览器开发者工具的 Console 和 Network。

如果后端显示 ready 但部分页面暂时为空，可能是 enriched 指标仍在后台预热。

## 9. 首次进入后的初始化

推荐按以下顺序操作：

1. 完成首次使用声明与向导。
2. 在“设置 → 数据源”保存 TickFlow Key；没有 Key 可以暂时跳过。
3. 点击“重新检测”，确认能力路由矩阵中的日 K、实时、分钟、盘口和财务状态。
4. 打开“数据”页，先同步个股维表。
5. 执行盘后管道或立即同步，准备 A 股日 K、除权因子和 enriched 数据。
6. 等待任务完成后，在“自选”页添加一个标的。
7. 在“策略”页运行内置策略，确认数据读取和指标计算正常。
8. 在“回测”页选择策略和区间，验证回测链路。
9. 需要 ETF、指数、分钟 K 或财务功能时，再开启相应拉取配置并同步。
10. 需要实时通知时，再配置实时范围、监控规则、语音、飞书或企业微信。

首次数据同步完成前，策略、回测、市场环境和分析页面显示空数据通常是预期行为。

## 10. 手动分别启动前后端

当一键脚本失败或需要分别观察日志时，使用两个 PowerShell 窗口。

### 10.1 后端窗口

```powershell
Set-Location E:\stockWorkSpace\tick-stock-panel\backend
uv sync --frozen
$env:PYTHONUNBUFFERED = '1'
.\.venv\Scripts\python.exe -m uvicorn app.main:app `
    --env-file ..\.env `
    --reload `
    --host 127.0.0.1 `
    --port 3018
```

需要 extras 时先执行其中一条：

```powershell
uv sync --frozen --extra backtest
uv sync --frozen --extra legacy-cpu
uv sync --frozen --extra legacy-cpu --extra backtest
```

### 10.2 前端窗口

```powershell
Set-Location E:\stockWorkSpace\tick-stock-panel\frontend
pnpm install --frozen-lockfile
$env:BACKEND_HOST = '127.0.0.1'
$env:BACKEND_PORT = '3018'
pnpm dev --host 127.0.0.1 --port 3011
```

如果后端使用了其他端口，前端窗口中的 `BACKEND_PORT` 必须保持一致。

## 11. 停止、重启和更新

### 11.1 停止

一键启动时，在启动窗口按：

```text
Ctrl+C
```

脚本会关闭后端、前端及其子进程。手动分别启动时，需要在两个窗口各按一次 `Ctrl+C`。

### 11.2 重新启动

正常情况下重新执行：

```powershell
.\dev.ps1
```

前后端源码修改通常会热重载；修改 `.env`、依赖或启动参数后应完整停止并重启。

### 11.3 更新代码后的依赖同步

`dev.ps1` 只在本地依赖目录不存在时自动安装普通依赖。拉取新代码后，如果锁文件或依赖清单有变化，建议显式同步：

```powershell
Set-Location E:\stockWorkSpace\tick-stock-panel
git status --short
git pull --ff-only

Set-Location .\backend
uv sync --frozen

Set-Location ..\frontend
pnpm install --frozen-lockfile

Set-Location ..
.\dev.ps1
```

执行 `git pull` 前先确认没有需要提交或保留的本地修改。不要用 `git clean -fdx` 清理冲突，它会删除 `.env`、`data/` 和本地依赖。

## 12. 常见问题

### 12.1 `uv` 无法识别

```powershell
Get-Command uv -ErrorAction SilentlyContinue
```

如果没有结果：

1. 关闭并重新打开 PowerShell。
2. 重新执行 `winget install --exact --id astral-sh.uv`。
3. 检查用户 `PATH` 是否包含 uv 的安装目录。

### 12.2 `pnpm` 无法识别

```powershell
node --version
npm --version
npm install --global pnpm@9.10.0
pnpm --version
```

如果 npm 的全局目录不在 `PATH`，重新安装 Node.js LTS 或修复 npm 全局路径。

### 12.3 PowerShell 提示脚本被禁止

优先使用一次性启动：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\dev.ps1
```

长期开发可设置当前用户为 `RemoteSigned`。如果 `MachinePolicy` 有值，应联系设备管理员。

### 12.4 3018 或 3011 端口被占用

查看占用：

```powershell
Get-NetTCPConnection -State Listen -LocalPort 3018,3011 -ErrorAction SilentlyContinue |
    Select-Object LocalPort, OwningProcess
```

查看对应进程：

```powershell
Get-Process -Id <PID>
```

需要保留该进程时，使用 `-BackendPort` 和 `-FrontendPort` 改端口。不要让一键脚本自动结束重要服务。

### 12.5 `uv sync --frozen` 下载失败

- 检查是否能访问 Python 包索引。
- 检查杀毒软件、代理或公司网络是否拦截 `uv`。
- 确认系统时间与证书正常。
- 保留 `backend/uv.lock`，不要为了绕过错误随意删除锁文件。

### 12.6 Polars 报 AVX2/FMA 或进程异常退出

编辑 `.env`：

```ini
BACKEND_EXTRAS=legacy-cpu
```

然后重新运行 `dev.ps1`。不要设置跳过 CPU 检测的变量，这只会隐藏提示，无法让 CPU 执行不支持的指令。

### 12.7 浏览器打开 3018 没有前端页面

这是开发模式的正常差异：前端开发服务器在 3011。请打开：

```text
http://127.0.0.1:3011
```

3018 主要是后端和 API。只有已经构建 `frontend/dist` 时，后端才可能直接托管前端静态文件。

### 12.8 页面打开但没有策略结果

依次确认：

1. `/health` 正常。
2. 设置页能力矩阵中的日 K 为可用。
3. 数据页已经同步维表和日 K。
4. enriched 任务已经完成，后台预热结束。
5. 所选日期位于本地数据范围内。

### 12.9 访问时跳到登录页

- 已设置密码：使用正确密码登录。
- 尚未设置密码：从本机 `127.0.0.1` 访问并完成初始化。
- 使用 `0.0.0.0` 只是监听方式，浏览器仍应打开 `127.0.0.1`、`localhost` 或机器实际 IP。

### 12.10 局域网设备无法访问

1. `.env` 设置 `HOST=0.0.0.0`。
2. 重启 `dev.ps1`。
3. 查看本机 IPv4 地址：

   ```powershell
   Get-NetIPAddress -AddressFamily IPv4 |
       Where-Object IPAddress -NotLike '127.*' |
       Select-Object IPAddress, InterfaceAlias
   ```

4. 在其他设备打开 `http://本机IP:3011`。
5. 允许 Windows 防火墙中的 Node.js 和 Python 入站访问。
6. 设置访问密码，不要在不可信网络裸露开发服务。

## 13. 开发验证命令

后端定向测试：

```powershell
Set-Location backend
uv sync --extra dev
uv run pytest tests\path\to\test_x.py -q
uv run ruff check app\path.py tests\path.py
```

前端生产构建：

```powershell
Set-Location frontend
pnpm build
```

仓库检查：

```powershell
Set-Location E:\stockWorkSpace\tick-stock-panel
git diff --check
git status --short --branch
```

代码改动的完整最低验证范围以根目录 `CONTRIBUTING.md` 为准。

## 14. 数据备份

开发环境中至少备份：

```text
data/
.env
额外安装的数据源插件或自定义部署配置
```

建议先停止 `dev.ps1`，再复制数据：

```powershell
Copy-Item -LiteralPath .\data -Destination E:\Backup\tick-stock-panel-data -Recurse
Copy-Item -LiteralPath .\.env -Destination E:\Backup\tick-stock-panel.env
```

目标备份目录应根据实际情况修改，并避免把包含密钥的 `.env` 放到公共网盘或 Git 仓库。

## 15. 最短启动清单

已经安装全部工具时，只需：

```powershell
Set-Location E:\stockWorkSpace\tick-stock-panel

if (-not (Test-Path -LiteralPath .env)) {
    Copy-Item -LiteralPath .env.example -Destination .env
}

notepad .env
.\dev.ps1
```

然后打开：

```text
http://127.0.0.1:3011
```

首次进入后执行“能力重新检测 → 数据同步 → 内置策略扫描”，即可验证完整开发链路。
