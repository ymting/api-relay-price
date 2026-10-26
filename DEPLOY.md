# 部署手册

本文是完整部署手册。只想快速跑起来，看 README 的「部署」一节就够了。

> **最容易踩的坑先说**：容器化部署时如果开了访问保护（`RELAY_AUTH_DEFAULT_ENABLED=1`）却没有 `auth.json`，服务会**直接退出、退出码 1**，容器一直重启。必须先执行一次 `--init-auth` 生成密码，或者首次先把访问保护关掉。

## 一、三种方式怎么选

| 方式 | 适合 | 需要 | 状态目录 |
| --- | --- | --- | --- |
| Docker Compose | 有 Docker 的 Linux 服务器，推荐 | Docker 24+ / Compose v2 | 卷 `/var/lib/relay-price` |
| systemd + 源码 | 不想装 Docker，Debian/Ubuntu | Python 3.10+ | `/var/lib/relay-price` |
| Windows 本地 | 自己的电脑，看看数据 | Python 3.10+ | 项目目录本身 |

三种方式的数据完全一样：一个状态目录里放着站点配置、偏好设置、价格快照、账户缓存、日志和认证文件。换方式只要能把这个目录搬过去就能接着用。

## 二、通用概念

### 2.1 状态目录

| 文件/目录 | 内容 | 能否丢 |
| --- | --- | --- |
| `sites.json` | 站点清单与凭据 | **不能**，丢了要重新配 |
| `settings.json` | 定时、节流、代理等偏好 | 不能，重配麻烦 |
| `auth.json` | 访问保护的密码哈希 | 能，重跑 `--init-auth` |
| `data/` | 价格快照、账户与用量缓存 | 能，重新采集即可 |
| `logs/` | 运行日志 | 能 |
| `*.versions/` | 配置的历史版本 | 能 |
| `.lock` | 实例锁，进程退出后自动失效 | 能 |

**只有 `sites.json` 和 `settings.json` 值得认真备份。** 其余都可再生。

### 2.2 端口

默认 `8765`。`local` 模式下如果端口被占会自动试 8766–8770；`server` 模式配合 `RELAY_STRICT_PORT=1` 时不会漂移，端口冲突直接失败（部署场景更希望明确报错，而不是悄悄换了端口让人找不到）。

### 2.3 镜像标签

```
ghcr.io/ymting/api-relay-price:1.0.0        正式版，与 Release 一致（当前）
ghcr.io/ymting/api-relay-price:latest       最新正式版
ghcr.io/ymting/api-relay-price:dev-latest   dev 分支最新构建，功能最新但不保证稳定
ghcr.io/ymting/api-relay-price:sha-xxxxxxx  按提交，用于精确定位
```

生产固定版本号或 digest；想尝鲜试 `dev-latest`。

### 2.4 启动退出码

| 码 | 含义 | 处理 |
| --- | --- | --- |
| `0` | 正常退出 | — |
| `1` | 配置错误，或认证已开启但没有密码 | 看控制台提示；认证问题跑 `--init-auth` |
| `2` | 状态目录已被另一个实例锁定 | 关掉旧进程/容器；systemd 侧已配 `RestartPreventExitStatus=2` 避免无限重启 |

## 三、方式 A：Docker Compose

### 3.1 准备宿主机

```bash
# 目录属主必须与镜像内的 UID/GID 一致（10001），否则启动即报权限错误
sudo install -d -m 0700 -o 10001 -g 10001 /var/lib/relay-price

sudo mkdir -p /etc/relay-price
sudo cp compose.example.yaml /etc/relay-price/compose.yaml
```

编辑 `/etc/relay-price/compose.yaml`，把 `RELAY_ALLOWED_ORIGINS` 和 `RELAY_ALLOWED_HOSTS` 改成你自己的域名。

### 3.2 初始化访问保护（**必做**）

这一步会写 `auth.json`。交互式，需要 `-it`：

```bash
cd /etc/relay-price
sudo docker compose run --rm -it app python server.py --init-auth
```

按提示选择是否开启访问保护、设置用户名和至少 8 位密码。**选「不开启」也会写入 `auth.json`**，效果等同 `RELAY_AUTH_DEFAULT_ENABLED=0`。

跳过这步直接 `up -d`，容器会反复重启，日志里是：

```
认证已开启但没有有效密码，请先执行：python server.py --init-auth
```

### 3.3 启动

```bash
sudo docker compose up -d
sudo docker compose ps          # STATUS 应显示 healthy
curl -s http://127.0.0.1:8765/healthz
```

正常返回：

```json
{"ok": true, "version": "1.0.0"}
```

`ok` 为 false 时返回 503，说明状态目录不可写或磁盘满了。

### 3.4 首次上线先关定时

样例里的 `command` 带了 `--no-schedule`，目的是先不自动采集。核对完页面上的定时设置后，把整个 `command` 行删掉再 `up -d`，定时才会生效。

### 3.5 升级

```bash
cd /etc/relay-price
sudo sed -i 's|api-relay-price:1\.0\.0|api-relay-price:1.0.1|' compose.yaml
sudo docker compose pull
sudo docker compose up -d
```

服务收到 `SIGTERM` 后会把在途任务收尾，`stop_grace_period: 60s` 就是给它的时间，别调小。

### 3.6 反向代理

`compose.example.yaml` 只把端口发布到 `127.0.0.1`，公网入口交给宿主上的 Caddy：

```caddyfile
relay.example.com {
    @internal_health path /healthz /healthz/*
    handle @internal_health {
        respond "Not Found" 404
    }
    handle {
        reverse_proxy 127.0.0.1:8765
    }
}
```

Nginx 等价配置：

```nginx
server {
    listen 443 ssl http2;
    server_name relay.example.com;

    location = /healthz { return 404; }

    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }
}
```

反代后 `RELAY_ALLOWED_ORIGINS` 必须是 `https://你的域名`（带协议、不带结尾斜杠），`RELAY_ALLOWED_HOSTS` 里要有裸域名。

如果还想按真实客户端 IP 记日志或限流，把 `RELAY_TRUSTED_PROXIES` 设成**反代到容器的源地址**（通常是 docker 网关 `172.17.0.1`）。该字段只接受精确 IP，不支持网段。

## 四、方式 B：systemd + 源码

```bash
sudo apt install -y python3          # 需要 3.10+
sudo install -d -m 0700 -o relay -g relay /var/lib/relay-price
sudo install -d -m 0755 /opt/relay-price/current

# 取源码（二选一）
git clone https://github.com/ymting/api-relay-price.git /opt/relay-price/current
# 或从 Release 下载 tarball 解压到该目录

sudo cp /opt/relay-price/current/deploy/relay-price.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now relay-price
```

`deploy/relay-price.service` 里已经把 `ProtectSystem=strict`、`PrivateTmp=true`、`ReadWritePaths=/var/lib/relay-price` 都设好了。首次同样要先初始化认证：

```bash
sudo -u relay RELAY_MODE=server RELAY_STATE_DIR=/var/lib/relay-price \
  RELAY_ALLOWED_ORIGINS=https://relay.example.com \
  RELAY_ALLOWED_HOSTS=relay.example.com \
  /usr/bin/python3 /opt/relay-price/current/server.py --init-auth
sudo systemctl restart relay-price
```

**注意**：`--init-auth` 只写认证文件、不监听端口，但它同样会取实例锁，所以要先 `systemctl stop relay-price` 再跑，跑完再启动。

## 五、方式 C：Windows 本地

```powershell
# 双击「启动服务.bat」即可，会自动选端口并打开浏览器
# 或手动：
python -X utf8 server.py --open
```

本地模式（默认）只监听 `127.0.0.1`，不接受外部来源，访问保护默认关闭。数据就放在项目目录下。

不需要 Docker、不需要 Node、不需要浏览器驱动 —— 只要 Python 3.10+，全程标准库。

## 六、环境变量全表

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `RELAY_MODE` | `local` | `local` 或 `server`。server 模式强制要求下面三项 |
| `RELAY_HOST` | `127.0.0.1` | 监听地址，容器内用 `0.0.0.0` |
| `RELAY_PORT` | `8765` | 监听端口，也可用 `--port` 覆盖 |
| `RELAY_STRICT_PORT` | `true` | 端口被占时直接失败，不自动漂移 |
| `RELAY_STATE_DIR` | — | 状态目录，**server 模式必填** |
| `RELAY_ALLOWED_ORIGINS` | — | 允许的来源，**server 模式必填**，如 `https://relay.example.com` |
| `RELAY_ALLOWED_HOSTS` | — | 允许的 Host，**server 模式必填**，如 `relay.example.com,127.0.0.1:8765` |
| `RELAY_TRUSTED_PROXIES` | 空 | 可信反代源 IP，逗号分隔，精确匹配 |
| `RELAY_AUTH_DEFAULT_ENABLED` | server 下 `true` | 是否默认开启登录保护 |
| `RELAY_VERSION_KEEP` | `30` | `*.versions/` 保留几个版本 |
| `RELAY_VERSION_KEEP_DAYS` | `30` | `*.versions/` 保留多少天 |
| `RELAY_LOCAL_ALLOW_NULL_ORIGIN` | `false` | 仅 local 模式，允许 `file://` 页面直连（默认禁止） |

`local` 模式不接受 `RELAY_ALLOWED_ORIGINS`、`RELAY_ALLOWED_HOSTS`、`RELAY_STRICT_PORT`，设了会直接报错 —— 这是为了防止本机模式被误配成对外开放。

## 七、备份与迁移

```bash
# 停服务保证文件一致（也可以不停，但要接受瞬时不一致）
docker compose stop app

sudo tar czf relay-price-backup-$(date +%F).tar.gz \
  -C /var/lib/relay-price sites.json settings.json auth.json

docker compose start app
```

`data/` 和 `logs/` 不用备 —— 价格快照能重新采集，日志只是排障用。

迁移到新机器：按上面的方式装好，把这三个文件放进新机器的状态目录，`chown 10001:10001`，启动即可。`sites.json` 里若有站点专用代理，注意新机器是否能用同样的地址。

## 八、排障

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 容器反复重启，日志说「认证已开启但没有有效密码」 | 没跑 `--init-auth` | `docker compose run --rm -it app python server.py --init-auth` |
| 启动报权限错误 | 宿主目录属主不是 10001 | `sudo chown -R 10001:10001 /var/lib/relay-price` |
| `healthz` 返回 503 | 状态目录不可写 | 检查挂载、磁盘空间、SELinux/AppArmor |
| 页面 403 | `RELAY_ALLOWED_HOSTS` 没有当前域名 | 加上域名并重启 |
| 登录后马上又跳回 `/login` | 用的 `http` 但 `RELAY_ALLOWED_ORIGINS` 写的是 `https`（或反之） | 两边协议必须一致 |
| 拉镜像 401 | 镜像需要登录（现已公开，正常应可匿名拉） | `docker login ghcr.io` |
| 端口起不来，退出码 2 | 状态目录被另一个进程锁定 | `docker compose down` 或杀掉旧进程；`.lock` 里的 PID 已失效时会自动接管 |
| 反代后拿到的客户端 IP 都是网关地址 | 没配 `RELAY_TRUSTED_PROXIES` | 设成反代源 IP（精确值） |

## 九、给 AI Agent 的部署指令

这一节是机器可执行版。Agent 按顺序执行，**每一步失败就停下报错，不要跳步、不要自己发明参数**。

### 9.1 前置检查

```bash
python3 --version            # 需要 3.10+（源码方式）
docker --version && docker compose version   # 需要 Docker 24+ / Compose v2（容器方式）
ss -ltnp | grep 8765         # 确认 8765 没被占用
```

### 9.2 Docker Compose 路径（推荐）

```bash
# 1. 目录（属主必须是 10001）
sudo install -d -m 0700 -o 10001 -g 10001 /var/lib/relay-price

# 2. 配置
sudo mkdir -p /etc/relay-price
sudo curl -fsSL https://raw.githubusercontent.com/ymting/api-relay-price/main/compose.example.yaml \
  -o /etc/relay-price/compose.yaml
# 或从源码仓库复制
sudo sed -i 's/relay\.example\.com/<实际域名>/g' /etc/relay-price/compose.yaml

# 3. 初始化认证（交互，必须 -it；无 TTY 时先选「不开启」再在页面里配）
cd /etc/relay-price && sudo docker compose run --rm -it app python server.py --init-auth

# 4. 启动与验证
sudo docker compose up -d
sudo docker compose ps
curl -fsS http://127.0.0.1:8765/healthz   # 期望 {"ok": true, "version": ...}
```

### 9.3 硬约束（不要违反）

1. **不要把端口发布成 `0.0.0.0`**。业务端口只到回环，公网入口交给反代。要对外就必须同时配好 `RELAY_ALLOWED_ORIGINS` 与 `RELAY_ALLOWED_HOSTS`。
2. **不要跳过 `--init-auth`**。开了访问保护却没有密码，服务会以退出码 1 退出。
3. **不要改小 `stop_grace_period`**。收尾写盘需要时间，砍掉会丢正在写的状态。
4. **不要用 `:latest` 或 `:dev-latest` 做生产版本**。生产锁版本号或 digest。
5. **不要直接编辑容器内的 `sites.json`**。它是状态目录里的文件，改宿主上的那一份。
6. **不要把凭据写进 compose 文件或提交到 Git**。凭据只放 `sites.json`，该文件已被 `.gitignore` 排除。

### 9.4 验收清单

```bash
curl -fsS http://127.0.0.1:8765/healthz                 # ok=true，version 与预期一致
sudo docker compose logs --tail=50 app                   # 无 Traceback
sudo stat -c '%U:%G %a' /var/lib/relay-price             # 10001:10001 700
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/login   # 200
```

浏览器打开站点，确认：能登录 → 站点列表非空 → 手动采集一次有数据 → 页面刷新后数据还在。

### 9.5 出问题时

按第九节的表格逐一对照。仍无解时收集：

```bash
sudo docker compose ps
sudo docker compose logs --tail=200 app
sudo docker compose config          # 确认环境变量真的生效了
```
