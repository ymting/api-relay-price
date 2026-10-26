# 人类部署手册

本文是**给人看的**完整部署手册。只想快速跑起来，看 [`README.md`](README.md) 的「部署」一节就够了。

> 让 AI Agent 替你部署？把 [`AGENT-DEPLOY.md`](AGENT-DEPLOY.md) 丢给它，那份是机器可执行清单。
> 两份文档的硬约束完全一致，区别只在表达方式：这份讲「为什么」，那份讲「照做、验证、失败就停」。

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
| `*.versions/` | 配置的历史版本（也是还原前的自动归档，以及主文件损坏时的读取回退来源） | 不建议丢 |
| `.lock` | 实例锁，进程退出后自动失效 | 能 |

**只有 `sites.json` 和 `settings.json` 值得认真备份。** 其余都可再生。

### 2.2 端口

默认 `8765`。`local` 模式下如果端口被占会自动试 8766–8770；`server` 模式配合 `RELAY_STRICT_PORT=1` 时不会漂移，端口冲突直接失败（部署场景更希望明确报错，而不是悄悄换了端口让人找不到）。

### 2.3 镜像标签

```
ghcr.io/ymting/api-relay-price:1.0.1        正式版，与 Release 一致（以 Release 页最新为准）
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

没有终端（CI、面板、AI Agent 执行）时 `-it` 会直接卡住，改用 `-T` 从标准输入喂用户名和密码：

```bash
read -rsp '用户名: ' RELAY_USER; echo
read -rsp '密码（至少 8 位）: ' RELAY_PASS; echo
printf '%s\n%s\n' "$RELAY_USER" "$RELAY_PASS" | \
  sudo docker compose run --rm -T app python server.py --init-auth --auth-stdin
```

用 `read -s` 而不是把密码写进命令行，密码才不会留在 shell 历史里。`--auth-stdin` 一定是「开启」，
没有「不开启」这个选项 —— 服务器模式下不开认证本来就起不来。

按提示选择是否开启访问保护、设置用户名和至少 8 位密码。**选「不开启」也会写入 `auth.json`**，效果等同 `RELAY_AUTH_DEFAULT_ENABLED=0`。

> 从 1.0.2-4 起，**服务器模式下没开访问保护会直接拒绝启动**（退出码 1），提示里带着该执行的命令。所以这一步不是「建议」，是必须走完的：要么在这里开，要么确认反代已做鉴权并显式设置 `RELAY_ALLOW_UNAUTHENTICATED_REMOTE=1`。

服务已经跑起来之后想开或改密码，不用再回命令行：「系统设置 → 访问保护」可以直接开启、改用户名、改密码、关闭（都需要当前密码）。忘记密码只能停服务重跑 `--init-auth`，它会覆盖 `auth.json`。

跳过这步直接 `up -d`，容器会反复重启，日志里是：

```
认证已开启但没有有效密码，请先执行：python server.py --init-auth
```

**没开访问保护会怎样**

| 模式 | 表现 |
| --- | --- |
| `server` | **拒绝启动**，退出码 1，控制台直接告诉你该执行哪条命令 |
| `local` | 只监听 `127.0.0.1`，外部本来就访问不到；若手动把监听地址改成 `0.0.0.0`，同样拒绝启动 |
| 被反代/内网穿透包出去且没开认证 | 来自回环之外的请求一律返回 403，提示里带开启方法 |

确实打算让前面的反向代理负责鉴权，才设 `RELAY_ALLOW_UNAUTHENTICATED_REMOTE=1` —— 前提是那一层真的开着。

**认证开启后的机制**（排障时用得上）

`/login`、`/login.js`、`/healthz`、`/api/ping`、`/api/auth/challenge`、`/api/auth/login` 是匿名入口；其余业务接口与 `/data/*` 未登录返回 401，`/` 跳转 `/login`。
会话 Cookie 为 `relay_session`（`HttpOnly; SameSite=Strict`），全部来源都是 https 时才加 `Secure`，空闲 30 分钟或满 12 小时失效。写请求除 Origin 外还要带 `X-Relay-CSRF`。
口令用 scrypt 派生后存 `auth.json`；登录要带一次性挑战，失败按来源 / 账号 / 全局三层限速并逐次退避，成功和失败都写进 `logs/`（失败行只有来源和用户名，不含密码）。

`RELAY_ALLOWED_ORIGINS` 必须与访问协议一致：用 `http` 访问却把来源写成 `https://`（或反之），登录后会立刻被打回 `/login`。

**服务已经在跑时怎么开关**

浏览器打开服务 → 「系统设置 → 访问保护」，填用户名和新密码即可开启；改密码、关闭也在这里（都需要当前密码）。忘记密码只能停服务重跑 `--init-auth`。

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
sudo sed -i 's|api-relay-price:.*|api-relay-price:1.0.2|' compose.yaml   # 换成目标版本号
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
| `RELAY_ALLOW_UNAUTHENTICATED_REMOTE` | `false` | 未开访问保护时仍允许对外服务。仅当反向代理已做鉴权时才打开；打开后 server 模式不再拒绝启动、也不再拦截外部来源 |
| `RELAY_VERSION_KEEP` | `30` | `*.versions/` 保留几个版本 |
| `RELAY_VERSION_KEEP_DAYS` | `30` | `*.versions/` 保留多少天 |
| `RELAY_LOCAL_ALLOW_NULL_ORIGIN` | `false` | 仅 local 模式，允许 `file://` 页面直连（默认禁止） |

`local` 模式不接受 `RELAY_ALLOWED_ORIGINS`、`RELAY_ALLOWED_HOSTS`、`RELAY_STRICT_PORT`，设了会直接报错 —— 这是为了防止本机模式被误配成对外开放。

## 七、备份与迁移

### 7.1 网页里导出与还原（推荐）

「系统设置 → 备份还原」提供一键备份，Docker、systemd、Windows 三种方式都一样：

1. **导出**：勾选「包含站点凭据」（默认勾上）后点「导出配置文件」，浏览器下载 `relay-price-config-<时间戳>.json`。文件里含各站点的 Cookie / 密码 / 管理令牌 / 模型 Key 明文，所以会先弹一次确认 —— 别把它丢进群聊或公开网盘。已开启访问保护时可以额外勾「包含访问保护」，连用户名和密码哈希一起带走。
2. **还原**：在新机器上装好服务、打开页面、进同一个页签，选文件 → 预检（显示站点数、导出时间与版本）→ 确认还原。

几条硬规则：

- 还原是**整体替换**，不是合并：备份里没有的站点会被删除。
- 还原前当前配置自动归档到 `sites.json.versions/`、`settings.json.versions/`（保留最近 30 份 ∪ 30 天，可用 `RELAY_VERSION_KEEP` / `RELAY_VERSION_KEEP_DAYS` 调），还原错了能翻回去。
- 备份不合法（格式/版本不对、站点超限、字段非法）整份拒绝，不会只还原一半；写入中途失败会回滚到导入前状态。
- 换了访问保护密码后旧会话立即作废，需要重新登录。
- 备份文件里若带站点专用代理，注意新机器能否用同样的地址。

### 7.2 命令行备份

网页进不去（比如容器起不来）时用这条：

```bash
# 停服务保证文件一致（也可以不停，但要接受瞬时不一致）
docker compose stop app

sudo tar czf relay-price-backup-$(date +%F).tar.gz \
  -C /var/lib/relay-price sites.json settings.json auth.json

docker compose start app
```

`data/` 和 `logs/` 不用备 —— 价格快照能重新采集，日志只是排障用。

迁移到新机器：按上面的方式装好，把这三个文件放进新机器的状态目录，`chown 10001:10001`，启动即可。`sites.json` 里若有站点专用代理，注意新机器是否能用同样的地址。

## 八、健康检查、停机与部署自检

### 8.1 健康检查

`GET /healthz` 只探测状态目录可写，不读快照、不发站点请求，返回 `{"ok": ..., "version": ...}`；目录不可写时返回 503。`compose.example.yaml` 的 healthcheck 已显式禁用代理，避免环境变量把本机探活转发出去。

### 8.2 停机

收到 `SIGTERM` / `SIGINT` 后先停止任务与调度，在 **45 秒预算**内收尾；超时保留 journal 中未确认的写操作，**不会重放**。容器 `stop_grace_period` 设 60 秒、systemd `TimeoutStopSec` 不低于 45 秒，别调小 —— 砍掉会丢正在写的状态。

### 8.3 部署自检

```powershell
$env:PYTHONUTF8 = "1"
python -X utf8 tests/smoke_deploy.py --mode process   # 本机 / systemd
python -X utf8 tests/smoke_deploy.py --mode docker    # 需要 Docker
```

冒烟只用合成配置（空站点、调度关闭），不访问任何站点。纯 Compose 部署的服务器上没有源码副本，跳过这步，用 8.1 的 `healthz` 加页面四步验收即可。

## 九、排障

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 启动报「访问保护未开启」并以退出码 1 退出 | 服务器模式没有开启登录保护 | 按提示执行 `--init-auth`；若反代已做鉴权，设 `RELAY_ALLOW_UNAUTHENTICATED_REMOTE=1` |
| 页面访问返回「当前未开启访问保护，已拒绝来自外部地址」 | local 模式被反代/穿透暴露且未开认证 | 开启访问保护，或确认真实链路后配置 `RELAY_ALLOW_UNAUTHENTICATED_REMOTE=1` |
| 容器反复重启，日志说「认证已开启但没有有效密码」 | 没跑 `--init-auth` | `docker compose run --rm -it app python server.py --init-auth` |
| 启动报权限错误 | 宿主目录属主不是 10001 | `sudo chown -R 10001:10001 /var/lib/relay-price` |
| `healthz` 返回 503 | 状态目录不可写 | 检查挂载、磁盘空间、SELinux/AppArmor |
| 页面 403 | `RELAY_ALLOWED_HOSTS` 没有当前域名 | 加上域名并重启 |
| 登录后马上又跳回 `/login` | 用的 `http` 但 `RELAY_ALLOWED_ORIGINS` 写的是 `https`（或反之） | 两边协议必须一致 |
| 拉镜像 401 | 镜像需要登录（现已公开，正常应可匿名拉） | `docker login ghcr.io` |
| 端口起不来，退出码 2 | 状态目录被另一个进程锁定 | `docker compose down` 或杀掉旧进程；`.lock` 里的 PID 已失效时会自动接管 |
| 反代后拿到的客户端 IP 都是网关地址 | 没配 `RELAY_TRUSTED_PROXIES` | 设成反代源 IP（精确值） |

## 十、交给 AI Agent 跑

想让 Agent 代劳的话，把 [`AGENT-DEPLOY.md`](AGENT-DEPLOY.md) 丢给它。那份文档把本文压缩成了可执行流程：

前置检查 → 路径选择 → 无 TTY 的 `--init-auth` 写法 → 验收断言 → 失败判定表 → 回滚 → 交付汇报模板，
并且明确写了「验证不通过就停下报错，不许跳步、不许自创参数」。

人类这边只需要准备三样东西：**域名**、**访问保护账号密码**、**站点凭据**。
前两个可以直接给 Agent；最后一个建议自己在页面上填，不必经过对话。

