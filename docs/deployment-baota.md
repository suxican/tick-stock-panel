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
git clone https://github.com/suxican/tick-stock-panel.git
cd tick-stock-panel
# 如需固定版本：git checkout v0.3.35
```

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
curl -I http://127.0.0.1:3018/           # 应返回 200
```

构建期间若出现 `Killed` / `exit code 137`，是内存不足：确认第 1 步 swap 已生效，必要时暂时在宝塔里停掉不用的服务（如 MySQL、Redis）后重试。

构建时网络问题的处理：Dockerfile 默认 `USE_CN_MIRROR=1`（国内源）。若服务器在海外，改用官方源：

```bash
docker compose build --build-arg USE_CN_MIRROR=0 && docker compose up -d
```

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

### 5.4 无域名的备选方案

不建站，直接在宝塔 → 「Docker」或命令行里把端口改为对外监听（**会失去 HTTPS，且暴露明文密码，不推荐**）：`.env` 中设 `HOST=0.0.0.0`，安全组放行 3018，再 `docker compose up -d`，用 `http://服务器IP:3018` 访问。务必已设置 `AUTH_PASSWORD`。

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

宝塔 → 「计划任务」→ 添加「Shell 脚本」，每天凌晨备份并保留 7 份：

```bash
#!/bin/bash
SRC=/www/wwwroot/tick-stock-panel
DST=/www/backup/tsp
mkdir -p "$DST"
tar -czf "$DST/tsp-data-$(date +%F).tar.gz" -C "$SRC" data .env
find "$DST" -name 'tsp-data-*.tar.gz' -mtime +7 -delete
```

磁盘只有 60 GB，行情数据会持续增长，建议定期查看：

```bash
du -sh /www/wwwroot/tick-stock-panel/data
docker system df
```

---

## 8. 升级

```bash
cd /www/wwwroot/tick-stock-panel
tar -czf ~/tsp-before-upgrade-$(date +%F).tar.gz data .env      # 先备份
git pull
docker compose up --build -d
docker image prune -f                                           # 清理旧镜像释放磁盘
```

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
