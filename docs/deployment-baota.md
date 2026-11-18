# 宝塔面板部署指南（OpenCloudOS 9.4 · x86_64）

适用环境：OpenCloudOS 9.4（兼容 RHEL 9，使用 `dnf`）、宝塔面板（面板自带 Py3.7.16，**仅供宝塔自身使用，与本项目无关**）、4 核 / 3.6 GB 内存 / 60 GB 磁盘。

项目后端要求 Python ≥ 3.11，系统与宝塔自带的 Python 都不满足，且 3.6 GB 内存对"宝塔 + 前端构建"偏紧。因此推荐 **Docker 单容器部署 + 宝塔 Nginx 反向代理**：Python、依赖、前端产物都封装在镜像内，不污染系统环境，升级只需重建容器。

```
浏览器 ──HTTPS──> 宝塔 Nginx(80/443) ──> 127.0.0.1:3018 ──> Docker 容器(FastAPI + 前端静态页)
                                                              └─ ./data 持久化到宿主机
```

通用配置项见 [configuration.md](./configuration.md)，访问密码原理见 [deploy-password.md](./deploy-password.md)，本文只覆盖宝塔场景。

---

## 0. 部署前准备

| 项 | 说明 |
| --- | --- |
| 服务器登录 | 宝塔“终端”或 SSH，使用 root |
| 域名（可选） | 想用 HTTPS 就需要一个已解析到服务器公网 IP 的域名；无域名也可用 `http://IP:端口` |
| 云厂商安全组 | 腾讯云控制台 → 安全组放行 **80、443**（及宝塔面板端口）。**不要放行 3018**，应用只对本机开放，由 Nginx 代理 |
| 宝塔防火墙 | 宝塔 → 安全，同样只放行 80/443 |
| 外网访问 | 服务器需能访问 GitHub（拉代码）和行情/AI 数据源；Dockerfile 默认使用国内 npm/PyPI/apt 镜像 |

先确认 CPU 是否支持 AVX2（影响 Polars，老 VPS 常见）：

```bash
grep -o -w -E 'avx2|fma' /proc/cpuinfo | sort -u
```

输出 `avx2` 和 `fma` 两行则正常；缺任何一个，在第 3 步 `.env` 中设置 `BACKEND_EXTRAS=legacy-cpu`。

---

## 1. 增加 Swap（强烈建议，防止构建时 OOM）

前端 `vite build` 与 Python 依赖安装峰值内存较高，3.6 GB 机器上同时跑着宝塔很容易被 OOM 杀掉。先加 2 GB swap（已有 swap 可跳过）：

```bash
free -h                                   # 查看是否已有 Swap
fallocate -l 2G /swapfile && chmod 600 /swapfile
mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
free -h                                   # 确认 Swap 为 2G
```

---

## 2. 安装 Docker

### 方案 A：宝塔应用商店（推荐，最省事）

宝塔 → 左侧「Docker」→ 首次点击会提示安装 Docker 与 Docker Compose 管理器，按提示安装，等待完成。

### 方案 B：命令行

```bash
dnf install -y dnf-plugins-core
dnf config-manager --add-repo https://mirrors.aliyun.com/docker-ce/linux/centos/docker-ce.repo
dnf install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
systemctl enable --now docker
```

若 `dnf install` 提示 OpenCloudOS 版本不被识别，可把 repo 文件里的 `$releasever` 手工改成 `9`。

### 配置镜像加速（国内拉取 `python`、`node` 基础镜像必备）

```bash
mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<'EOF'
{
  "registry-mirrors": [
    "https://mirror.ccs.tencentyun.com"
  ],
  "log-driver": "json-file",
  "log-opts": { "max-size": "20m", "max-file": "3" }
}
EOF
systemctl restart docker
```

`mirror.ccs.tencentyun.com` 仅腾讯云内网可用（本服务器若为腾讯云则适用）；其他厂商请换成对应的加速地址。`log-opts` 用于限制容器日志体积，避免占满磁盘。

验证：

```bash
docker version
docker compose version     # 应输出 v2.x；若只有 docker-compose 命令，下文把 "docker compose" 换成 "docker-compose"
```

---

## 3. 获取代码与配置

```bash
mkdir -p /www/wwwroot && cd /www/wwwroot
git clone -b can_new_cc2.0 https://github.com/suxican/tick-stock-panel.git
cd tick-stock-panel
# 本部署默认使用 can_new_cc2.0 分支；如需固定到某个版本：git checkout v0.3.35
```

**代码是从本机复制上去的（目录不是 git 仓库）：** 把它接入 git，之后即可 `git pull` 更新。先确认服务器能访问 GitHub（`curl -I -m 10 https://github.com`），仓库为私有时还需在服务器配置访问凭据。

```bash
git config --global --add safe.directory /www/wwwroot/tick-stock-panel   # 目录属主不是 root 时必需，否则报 dubious ownership
cd /www/wwwroot/tick-stock-panel
git init
git remote add origin https://github.com/suxican/tick-stock-panel.git
nohup git fetch --progress --depth 1 origin can_new_cc2.0 > /root/git-fetch.log 2>&1 &
```

- 用 `--depth 1` 只拉最新快照。国内服务器访问 GitHub 常时通时断、速度仅几十 KiB/s，完整历史（近两万个对象）几乎拉不完，中途断开还得重来。
- 放后台并把日志写到项目目录之外，避免宝塔网页终端断线中断下载。用 `pgrep -a git` 看进程是否还在，`du -sk .git` 看体积是否增长；进程消失后 `tail /root/git-fetch.log` 出现 `* branch can_new_cc2.0 -> FETCH_HEAD` 即成功。若日志出现 `Failed to connect` / `RPC failed`，重新执行一遍 `git fetch` 即可。

拉取完成后接入分支：

```bash
git checkout -b can_new_cc2.0
git reset origin/can_new_cc2.0        # 不带 --hard：只对齐 git 记录，不改动工作区文件
git branch --set-upstream-to=origin/can_new_cc2.0 can_new_cc2.0
git remote set-branches origin can_new_cc2.0   # 只跟踪该分支，否则 git pull 会连带拉取其他分支的完整历史，慢线路上几乎拉不完
git config core.autocrlf false
git status --short | head             # 查看与仓库的差异
```

**怎么看 `git status` 的差异：** 从 Windows 复制来的文件是 CRLF，仓库是 LF，`git status` 会显示上千个文件被修改，大多只是换行符，并非真实改动。用忽略行尾空白的方式判断真实差异：

```bash
git diff --ignore-space-at-eol --stat | tail -20
git diff --ignore-space-at-eol --diff-filter=M --stat | tail -5    # 只看被修改（非删除）的文件
```

- 没有新增行（只有 `deletions`）：差异只是仓库里有而服务器没复制的文件（截图、文档等），无需恢复。
- 出现真实内容差异：先把本机对应改动 `commit` 并 `push`，再让服务器拉取，否则覆盖会丢失。

确认无误后，备份代码，再**只**对齐被修改的文件（不恢复已删除的文件，也不使用 `git checkout -- .`）：

```bash
cd /www/wwwroot
tar -czf ~/tsp-code-backup-$(date +%F).tar.gz --exclude=tick-stock-panel/data --exclude='*.rar' tick-stock-panel
cd tick-stock-panel
git ls-files -m | xargs -r git checkout --
```

这只统一换行符和权限位，内容不变，容器无需重建。此后 `git pull` 即可，浅克隆下它只下载新增的提交。

`data/` 与 `.env` 不被 git 跟踪，以上操作不会影响它们。上传用的 `.rar` 压缩包与宝塔建站生成的 `index.html`、`404.html`、`.htaccess`、`.user.ini` 是未跟踪文件，可忽略；确认服务正常后可删除 `.rar` 释放磁盘。

服务器访问不了 GitHub（或反复失败）时，可在本机用 `git archive --format=tar.gz -o tsp-update.tar.gz HEAD` 打包（只含已提交的文件，不含 `data/`、`.env`），`scp` 到服务器后在项目目录 `tar -xzf` 覆盖；该方式不会删除已在本机移除的文件。

若 `git` 未安装：`dnf install -y git`。若 GitHub 访问不稳定，可在本机打包上传后解压到同一目录（不要上传 `node_modules`、`data`、`.git` 之外无关大目录）。

创建配置文件：

```bash
cp .env.example .env
chmod 600 .env
vi .env
```

需要修改的关键项：

```ini
# 只监听本机，由宝塔 Nginx 对外提供服务（compose 的端口映射会读取 HOST）
HOST=127.0.0.1
PORT=3018

# TickFlow 行情 Key，留空 = Free 试用模式（仅历史日 K 单股）
TICKFLOW_API_KEY=

# 访问密码（公网部署必须设置，至少 6 位，用单引号包裹）
AUTH_PASSWORD='请换成强密码'

# AI 功能（可选）
AI_PROVIDER=openai_compat
AI_BASE_URL=https://api.deepseek.com/v1
AI_API_KEY=
AI_MODEL=deepseek-chat

# 老 CPU（第 0 步缺 avx2/fma）才设置；需要回测则写 "legacy-cpu backtest"
BACKEND_EXTRAS=

# docker-compose.yml 已强制 DATA_DIR=/app/data，此项保持默认即可
DATA_DIR=./data
```

说明：

- `HOST=127.0.0.1` 使端口映射成 `127.0.0.1:3018:3018`，公网无法绕过 Nginx 直接访问，也不会因 Docker 绕过防火墙而暴露。
- `AUTH_PASSWORD` 是**一次性初始化**：仅在尚未设密码时生效，之后改密码请在页面「设置 → 修改密码」。忘记密码的重置方法见 [deploy-password.md](./deploy-password.md)。
- 需要回测（vectorbt/numba）时设置 `BACKEND_EXTRAS=backtest`，但会明显增加镜像体积和构建内存，3.6 GB 机器请确保已加 swap。

---

## 4. 构建并启动

```bash
cd /www/wwwroot/tick-stock-panel
mkdir -p data
docker compose up --build -d
```

首次构建需下载基础镜像、Node/Python 依赖、Codex CLI，通常 5–15 分钟。观察进度与状态：

```bash
docker compose logs -f --tail=100       # Ctrl-C 退出日志跟随，不会停止服务
docker compose ps                        # STATUS 应为 Up
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:3018/   # 应返回 200（不要用 curl -I，HEAD 会返回 405）
```

构建期间若出现 `Killed` / `exit code 137`，是内存不足：确认第 1 步 swap 已生效，必要时暂时在宝塔里停掉不用的服务（如 MySQL、Redis）后重试。

构建时网络问题的处理：Dockerfile 默认 `USE_CN_MIRROR=1`（国内源）。若服务器在海外，改用官方源：

```bash
docker compose build --build-arg USE_CN_MIRROR=0 && docker compose up -d
```

**构建卡在 `uv sync`（Python 依赖下载）一小时以上：** 有两个原因叠加：

1. 默认的清华 PyPI 源在部分服务器上只有约 0.5 MB/s，先测速选最快的源。
2. **更关键：** `uv sync --frozen` 按 `backend/uv.lock` 里记录的地址下载 wheel，这些地址（1700 多处）指向 PyPI 官方文件服务器 `files.pythonhosted.org`，国内服务器实测仅约 20 KB/s，`--build-arg PYPI_INDEX` 等镜像参数对它们**无效**，大包（`pyarrow`、`polars`、`llvmlite`、`duckdb` 等）要下几个小时。`can_new_cc2.0` 分支自提交 `34288594` 起，Dockerfile 在 `USE_CN_MIRROR=1`（默认）时会按 `PYPI_FALLBACK` 把这些地址替换成镜像的同路径地址（`sha256` 不变，仍受校验），实测整步从数小时降到几分钟。因此**请确保服务器代码在该提交或之后**（`git log --oneline -1` 检查）。

怀疑镜像上同路径文件是否可用时，可对比同一个 wheel 的下载速度（`P` 取自 `uv.lock` 中任意 wheel 地址去掉域名的部分）：

```bash
P=packages/f3/27/99c42abe8e21b44f4917f62631f3aa31404882a2c41d8a4cd5c110e13d52/pyarrow-24.0.0-cp311-cp311-manylinux_2_28_x86_64.whl
curl -s -o /dev/null -m 20 -w "官方 %{http_code} %{speed_download} B/s\n" https://files.pythonhosted.org/$P
curl -s -o /dev/null -m 20 -w "腾讯云 %{http_code} %{speed_download} B/s\n" https://mirrors.cloud.tencent.com/pypi/$P
curl -s -o /dev/null -m 20 -w "阿里云 %{http_code} %{speed_download} B/s\n" https://mirrors.aliyun.com/pypi/$P
```

参考：某腾讯云服务器实测官方 19 KB/s、腾讯云 68.8 MB/s、阿里云 14.8 MB/s。依赖解析走的 PyPI 索引仍建议选最快的源，并用 `nohup` 放后台构建，避免宝塔网页终端断线中断：

```bash
for u in https://mirrors.cloud.tencent.com/pypi/simple https://pypi.tuna.tsinghua.edu.cn/simple https://mirrors.aliyun.com/pypi/simple; do echo $u; curl -o /dev/null -s -w "%{speed_download} B/s\n" -m 20 $u/numba/; done
```

```bash
cd /www/wwwroot/tick-stock-panel
nohup docker compose build --build-arg PYPI_INDEX=https://mirrors.cloud.tencent.com/pypi/simple --build-arg PYPI_FALLBACK=https://mirrors.aliyun.com/pypi/simple > build.log 2>&1 &
tail -f build.log      # Ctrl-C 只退出查看，不会中断构建；出现 naming to ... done 即完成
docker compose up -d
```

以腾讯云源为例；实测速度更快的源请自行替换。构建期间不要中断（构建里没有 uv 缓存，中断会丢失已下载内容）。

升级时若 `git pull` 无新提交（`Already up to date.`），无需重新构建。若曾在服务器上手工改过 `Dockerfile`，拉取新版本前先 `git checkout Dockerfile`，否则 `git pull` 会因本地有修改而冲突。

容器已设置 `restart: unless-stopped`，服务器重启后随 Docker 自动恢复；需确认 `systemctl is-enabled docker` 为 `enabled`。

---

## 5. 宝塔配置反向代理

### 5.1 创建站点

宝塔 → 「网站」→「添加站点」：

- 域名：填你的域名（如 `panel.example.com`）；无域名可填服务器 IP
- PHP 版本：选「纯静态」
- 数据库：不创建

### 5.2 申请 HTTPS（有域名时）

站点设置 → 「SSL」→ Let’s Encrypt → 勾选域名 → 申请 → 开启「强制 HTTPS」。（申请前确保域名已解析，且 80 端口已放行。）

### 5.3 配置反向代理

站点设置 → 「反向代理」→「添加反向代理」：

- 代理名称：`tsp`
- 目标 URL：`http://127.0.0.1:3018`
- 发送域名：`$host`

保存后，**不要直接用默认配置**，点击该代理的「配置文件」，把 `location /` 块替换为下面内容（关键是关闭缓冲以支持 SSE 实时推送、保留真实 IP、放宽超时）：

```nginx
location / {
    proxy_pass http://127.0.0.1:3018;
    proxy_http_version 1.1;

    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Connection "";

    # SSE（行情推送、回测/挖掘进度）必须关闭缓冲，否则前端收不到实时数据
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;

    # 回测结果、数据导入等可能有较大请求体
    client_max_body_size 100m;
}
```

要点说明：

- 项目大量使用 SSE（`text/event-stream`）。缺少 `proxy_buffering off` 会表现为"页面能打开但行情不刷新/进度条不动"。
- 后端只在直连来源是本机/内网（Nginx 与 Docker 网关都满足）时才采信 `X-Forwarded-For`，所以必须带上该头，访问密码的“首次设置仅限本机/内网”判断才能拿到真实客户端 IP。
- 若宝塔 Nginx 前面还有 CDN/WAF（腾讯云 EdgeOne 等），需确保 CDN 对 SSE 路径不缓存、不压缩缓冲。

### 5.4 无域名：仅用 IP 的临时访问

没有域名时无法申请 HTTPS，以下方案只适合临时测试，有域名后请改回 5.1–5.3。三种做法按安全性从高到低排列。

**通用前提：** 公网访问（方案 2、3）前，`.env` 必须已设置 `AUTH_PASSWORD`（至少 6 位），否则会提示“首次设置密码仅允许本机或内网访问”，且谁先打开页面谁就能抢设密码。修改后执行 `docker compose up -d --force-recreate` 生效。无 HTTPS 时密码和数据为明文传输。

**方案 1：SSH 隧道（最安全，不开放任何端口）**

在**自己电脑**的终端执行并保持窗口不关：

```bash
ssh -L 3018:127.0.0.1:3018 root@服务器公网IP
```

浏览器打开 `http://127.0.0.1:3018`。后端会把你视为本机，首次设置密码不受限制。仅适合单人使用。

**方案 2：宝塔 Nginx 用 IP + 端口代理（推荐，可多人访问）**

保持 `.env` 中 `HOST=127.0.0.1`，由 Nginx 对外开放一个端口（下例用 8088，避免与宝塔面板端口冲突）：

1. 宝塔 → 「网站」→「添加站点」：域名填 `服务器公网IP:8088`，选「纯静态」，不创建数据库。
2. 云安全组与宝塔「安全」页都放行 `8088`，**不放行 3018**。
3. 站点设置 → 「反向代理」→ 添加，目标 URL `http://127.0.0.1:3018`，再按 5.3 把 `location /` 替换为含 `proxy_buffering off` 与 `X-Forwarded-For` 的配置并保存。
4. 服务器上验证代理链路，返回 `200` 即正常：

   ```bash
   curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8088/
   ```

5. 浏览器访问 `http://服务器公网IP:8088`。

**方案 3：直接暴露 3018（最简单，不推荐）**

```bash
cd /www/wwwroot/tick-stock-panel
sed -i 's/^HOST=.*/HOST=0.0.0.0/' .env
docker compose up -d --force-recreate
```

安全组放行 3018，访问 `http://服务器公网IP:3018`。用完后把 `HOST` 改回 `127.0.0.1`、撤销放行并重建容器。

> 验证容器时不要用 `curl -I`：它发 HEAD 请求，根路径只允许 GET，会返回 `405 Method Not Allowed`，并非故障。请用 `curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:3018/`。

---

## 6. 首次访问与验证

1. 浏览器打开 `https://你的域名`，应出现登录页；输入第 3 步设置的 `AUTH_PASSWORD`。
2. 若提示“首次设置密码仅允许本机或内网访问”：说明 `AUTH_PASSWORD` 未生效（或密码不足 6 位 / `.env` 修改后未重启）。处理：

   ```bash
   docker compose logs --tail=100 | grep -i -E "auth|password"
   docker compose up -d --force-recreate     # 修改 .env 后重建使其生效
   ```

   或改用 SSH 转发设置密码：`ssh -L 3018:127.0.0.1:3018 root@服务器IP`，再访问本机 `http://127.0.0.1:3018`。
3. 登录后进入「设置」填写行情源 / AI 的 Key（保存在 `data/user_data/secrets.json`）。
4. 确认实时推送：打开行情页，观察数据是否自动刷新；若不刷新，回查 5.3 的 `proxy_buffering off`。
5. 健康检查：

   ```bash
   curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:3018/
   docker stats --no-stream         # 稳定运行内存一般应明显低于 2 GB
   ```

---

## 7. 数据、备份与计划任务

所有用户数据（行情 K 线、自选、策略、回测、密钥）都在宿主机 `/www/wwwroot/tick-stock-panel/data`，容器重建不会丢失。**`data/user_data/secrets.json` 含 Key，备份文件不要公开分享。**

`data/` 体积很大（实测约 5.5 GB，主要是 `kline_*` 行情、`research`、`integrity_backups`、`cache`，以及 `user_data` 下的 `first_board` 等运行数据），整体打包每天要占几 GB，60 GB 磁盘撑不住。每日备份只保留**丢了找不回来**的配置与密钥（`secrets.json`、`auth.json`、`preferences.json`、策略与监控配置等，通常只有几 MB），行情、缓存可重新拉取，不备份。

宝塔 → 「计划任务」→ 添加「Shell 脚本」，每天凌晨备份并保留 7 份：

```bash
#!/bin/bash
SRC=/www/wwwroot/tick-stock-panel
DST=/www/backup/tsp
mkdir -p "$DST"
tar -czf "$DST/tsp-config-$(date +%F).tar.gz" -C "$SRC" \
  --exclude=data/user_data/first_board \
  --exclude=data/user_data/market_game \
  --exclude=data/user_data/position_t \
  --exclude=data/user_data/alerts.jsonl \
  data/user_data .env
find "$DST" -name 'tsp-config-*.tar.gz' -mtime +7 -delete
```

排除的几个目录是运行时积累的数据，如其中有不想重新生成的记录（例如 `position_t`），去掉对应的 `--exclude` 或单独备份。首次执行后用 `ls -lh /www/backup/tsp` 确认压缩包大小合理。

磁盘只有 60 GB，行情数据会持续增长，建议定期查看各目录占用与 Docker 占用：

```bash
du -sh /www/wwwroot/tick-stock-panel/data/* | sort -h | tail -10
docker system df
```

`data/cache`、`data/integrity_backups` 通常可以清理以释放空间，清理前先确认其用途，不要整体删除 `data/`。

---

## 8. 升级

**请分步执行，不要把多行命令一次性粘贴进终端**：多行粘贴会被逐条排队执行，`git pull` 卡住时按 `Ctrl-C` 只会取消当前这一条，后面排队的构建、重启仍会执行，还可能因按键混入而把命令打乱。

**第 1 步：备份配置（可选）并确认分支**

```bash
cd /www/wwwroot/tick-stock-panel
tar -czf ~/tsp-before-upgrade-$(date +%F).tar.gz --exclude=data/user_data/first_board --exclude=data/user_data/market_game --exclude=data/user_data/position_t --exclude=data/user_data/alerts.jsonl data/user_data .env   # 只备份配置与密钥；排除的是体积大的运行数据目录（可按需调整）
git branch                                                      # 应显示 * can_new_cc2.0
```

**第 2 步：拉取代码**（服务器访问 GitHub 时通时断，加超时；失败或超时就隔一会儿重试，不要往下走）

```bash
timeout 600 git pull
```

输出 `Already up to date.` 表示没有新提交，无需重建；出现文件变更才继续。

**第 3 步：重建并启动**（用 `&&` 串联，任何一步失败即停止）

```bash
docker compose build --build-arg PYPI_INDEX=https://mirrors.cloud.tencent.com/pypi/simple --build-arg PYPI_FALLBACK=https://mirrors.aliyun.com/pypi/simple && docker compose up -d
```

**第 4 步：确认服务正常，再清理旧镜像**

```bash
docker compose ps
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:3018/     # 应返回 200
docker image prune -f                                               # 清理无标签旧镜像释放磁盘
```

- 服务器只能拉到本机**已 commit 并 push** 的内容；本机未提交的修改不会同步。
- 只改代码时依赖层有缓存，构建较快；改了 `backend/pyproject.toml`、`backend/uv.lock` 或 `frontend/package.json` 时依赖会重新下载，建议用 `nohup` 放后台（见第 4 节）。
- 前端无需在服务器单独构建，Dockerfile 会重新打包前端并写入镜像；强制刷新浏览器（`Ctrl+F5`）才能看到新页面。
- `data/` 不在 git 中，更新代码不会影响它；`.env` 中新增配置项需在服务器手动修改，并执行 `docker compose up -d --force-recreate`。
- 需要把本机的数据带到服务器时，只拷贝确需同步的子目录，不要整体覆盖 `data/`（尤其 `data/user_data/secrets.json` 与 `auth.json`），拷贝前先 `docker compose stop`。

注意：

- **不要**执行 `git clean -fdx`、`git reset --hard`，也不要删除整个目录重新 clone——会清掉 `data/`。`git pull` 冲突时先 `git stash`。
- 若你对代码有本地修改，升级前参考 [secondary-development.md](./secondary-development.md)。
- 回滚：`git checkout <旧版本 tag>` 后再 `docker compose up --build -d`。

---

## 9. 常用运维命令

```bash
cd /www/wwwroot/tick-stock-panel
docker compose ps                         # 状态
docker compose logs -f --tail=200         # 实时日志
docker compose restart                    # 重启
docker compose down                       # 停止并移除容器（data 保留）
docker compose up -d --force-recreate     # 修改 .env 后让配置生效
docker exec -it TickFlow_Stock_Panel bash # 进入容器
```

---

## 10. 常见问题

| 现象 | 原因与处理 |
| --- | --- |
| 构建中途 `Killed` / 137 | 内存不足。确认 swap；临时停掉宝塔里不用的服务；`BACKEND_EXTRAS` 不要带 `backtest` |
| 拉取基础镜像超时 | 检查 `/etc/docker/daemon.json` 镜像加速；`docker pull python:3.11-slim` 单独验证 |
| `exit 132` / 日志提示 avx2 缺失 | CPU 太老，`.env` 设 `BACKEND_EXTRAS=legacy-cpu`，再 `docker compose up --build -d` |
| 页面 502 | 容器没起来或端口不对：`docker compose ps`、`curl http://127.0.0.1:3018/`；确认反代目标是 `127.0.0.1:3018` |
| 页面能开但行情不刷新 | Nginx 缺 `proxy_buffering off`，或 CDN 缓冲了 SSE；按 5.3 修正后在宝塔重载 Nginx |
| 提示首次设置密码仅限本机 | 见第 6 步第 2 点；确认反代带了 `X-Forwarded-For` |
| `${HOME}/.codex` 相关挂载报错 | compose 会挂载主机的 `~/.codex`（Codex 登录态，不用 AI Codex 功能可忽略）。目录不存在时 Docker 会自动创建；若 `HOME` 为空，在 `.env` 加 `CODEX_HOME_HOST=/root/.codex` |
| 容器内访问宿主机上的自定义数据源 | compose 已配置 `host.docker.internal`，数据源地址写 `http://host.docker.internal:端口` |
| 磁盘占满 | `docker system prune -a`（会删未使用镜像，谨慎）；清理 `data` 中不需要的历史行情；确认 daemon.json 已限制日志大小 |
| 容器访问外网超时（同步涨停归因、K 线拉取报 `timed out`），宿主机 `curl` 同一地址正常 | 容器出站被拦：先查 `iptables -t nat -S POSTROUTING` 是否有 Docker 网段的 `MASQUERADE` 规则；没有则 `systemctl restart docker && docker compose up -d` 让 Docker 重建规则（`firewalld` 启用时，修改或重载防火墙后也需要重启 Docker）。验证：`docker exec TickFlow_Stock_Panel /app/.venv/bin/python -c "import socket;socket.create_connection(('api.zizizaizai.com',443),timeout=8);print('tcp ok')"` |
| 修改防火墙后 Docker 启动失败，日志含 `ZONE_CONFLICT: 'docker0' already bound to 'trusted'` | **不要**把 `docker0` 手动加进 `trusted` 区，它由 Docker 自己管理（`docker` 区）。恢复：`systemctl stop docker docker.socket`，`firewall-cmd --permanent --zone=trusted --remove-interface=docker0`，`firewall-cmd --reload`，`systemctl reset-failed docker docker.socket`，`systemctl start docker` |
| Docker 启动失败，日志含 `networks have same bridge name` / 容器报 `network ... not found` | 网络状态库损坏。`systemctl stop docker docker.socket`，把 `/var/lib/docker/network/files/local-kv.db` 移走备份（只含网络配置，不含镜像、容器、卷），再 `systemctl start docker`，最后 `docker compose up -d` 重建网络 |
| `docker rm -f` 报 `tried to kill container, but did not receive an exit event` | 容器残留记录。`docker update --restart=no 容器名`，`systemctl restart containerd && systemctl restart docker`，再 `docker rm -f 容器名`，最后 `docker compose up -d`；镜像与 `data/` 不受影响 |
| 多容器/多进程扩容 | 不支持：API 必须保持单进程，且不能多个容器写同一 `data` 目录（见 [deployment.md](./deployment.md) 的“多窗口与多人共享访问”） |

---

## 11. 部署清单（逐项勾选）

- [ ] 安全组 / 宝塔防火墙只放行 80、443（及宝塔面板端口），**未放行 3018**
- [ ] 已创建 2 GB swap
- [ ] Docker 与 Compose 正常，镜像加速已配置
- [ ] `.env`：`HOST=127.0.0.1`、已设置 `AUTH_PASSWORD`、权限 600
- [ ] `docker compose ps` 为 Up，`curl 127.0.0.1:3018` 返回 200
- [ ] Nginx 反代含 `proxy_buffering off`、`X-Forwarded-For`，HTTPS 已启用
- [ ] 登录成功、行情实时刷新正常
- [ ] 已添加每日数据备份计划任务
