# 给 AI Agent 的部署指引

> 这份文档写给**执行部署的 AI Agent**。人类读者请走 [`DEPLOY.md`](DEPLOY.md)。
>
> 目标：在一台干净的 Linux 服务器上，把服务跑到「能登录、能采集、重启不丢状态」。
> 铁律：每一步的验证命令必须看到预期输出才继续；**失败就停下来报错**，不要跳步、不要自己发明参数。

---

## 0. 动手前先问人类三个问题

| 问题 | 为什么必须先问 | 没答案时怎么办 |
| --- | --- | --- |
| 用哪个域名？ | `RELAY_ALLOWED_ORIGINS` / `RELAY_ALLOWED_HOSTS` 必须写真实域名，写错就登录后立刻被打回 | 没有域名就只在本机/内网跑，**不要对外暴露** |
| 服务器上有 Docker 吗？ | 有则走路径 A，没有则走路径 B（systemd） | 自己 `docker --version` 探，探不到就走 B |
| 站点凭据由谁填？ | 凭据只能进状态目录里的 `sites.json`，**不要出现在命令行、compose 文件或对话里** | 默认由人类在页面上填，Agent 只负责把服务跑起来 |

访问保护的账号密码需要人类给。人类没给就自己生成一个随机强密码，**在交付汇报里明确告诉他**，并提醒他首次登录后改掉。

---

## 1. 前置检查

```bash
python3 --version                            # 路径 B 需要 3.10+
docker --version && docker compose version   # 路径 A 需要 Docker 24+ / Compose v2
ss -ltnp | grep 8765                         # 应为空，否则端口被占
df -h /var/lib                               # 状态目录所在分区要有空间
```

任何一项不满足就停下，把原始输出贴给人类，不要自己装 Docker 或升级系统。

---

## 2. 路径 A：Docker Compose（有 Docker 时首选）

```bash
# 1) 状态目录。属主必须与镜像内 UID/GID 一致（10001），否则启动即报权限错误
sudo install -d -m 0700 -o 10001 -g 10001 /var/lib/relay-price

# 2) 取 compose 配置
sudo mkdir -p /etc/relay-price
sudo curl -fsSL https://raw.githubusercontent.com/ymting/api-relay-price/main/compose.example.yaml \
  -o /etc/relay-price/compose.yaml
sudo sed -i 's/relay\.example\.com/<实际域名>/g' /etc/relay-price/compose.yaml

# 3) 锁定版本号（生产不要用 latest / dev-latest）
#    把 image 行改成具体版本，例如 ghcr.io/ymting/api-relay-price:1.0.1

# 4) 初始化访问保护（必做，否则容器反复重启）
cd /etc/relay-price
RELAY_USER='admin'
RELAY_PASS="$(openssl rand -base64 18)"       # 人类没给密码就自己生成，交付时告诉他
printf '%s\n%s\n' "$RELAY_USER" "$RELAY_PASS" | \
  sudo docker compose run --rm -T app python server.py --init-auth --auth-stdin
echo "访问保护账号：$RELAY_USER ／ 密码：$RELAY_PASS"

# 5) 启动并验证
sudo docker compose up -d
sudo docker compose ps                                        # STATUS 应为 healthy
curl --noproxy '*' -fsS http://127.0.0.1:8765/healthz          # {"ok": true, "version": ...}
```

要点：

- **有 TTY** 时才用 `-it` 交互式初始化；Agent 环境通常没有 TTY，`-it` 会直接卡死，必须用上面 `-T` + `--auth-stdin` 的写法。
- `compose.example.yaml` 默认按系统设置运行自动采集；新配置默认每天 03:00。首次启动后核对时区、自动采集开关与时间；如需验收期间暂时关闭，在系统设置中关闭并保存。
- 业务端口只发布到 `127.0.0.1`，公网入口交给宿主 Caddy / Nginx 反代（配置见 `DEPLOY.md` 3.6）。

---

## 3. 路径 B：systemd + 源码（没有 Docker）

```bash
sudo apt install -y python3
sudo install -d -m 0700 -o relay -g relay /var/lib/relay-price
sudo install -d -m 0755 /opt/relay-price/current
sudo git clone https://github.com/ymting/api-relay-price.git /opt/relay-price/current

sudo cp /opt/relay-price/current/deploy/relay-price.service /etc/systemd/system/
sudo systemctl daemon-reload

# 首次初始化认证：--init-auth 也要取实例锁，所以此时服务必须是停止状态
sudo -u relay env RELAY_MODE=server RELAY_STATE_DIR=/var/lib/relay-price \
  RELAY_ALLOWED_ORIGINS=https://<实际域名> RELAY_ALLOWED_HOSTS=<实际域名> \
  /usr/bin/python3 /opt/relay-price/current/server.py --init-auth

sudo systemctl enable --now relay-price
systemctl is-active relay-price                                # active
curl --noproxy '*' -fsS http://127.0.0.1:8765/healthz          # {"ok": true, ...}
```

`deploy/relay-price.service` 里已经写好 `ProtectSystem=strict`、`PrivateTmp=true`、`ReadWritePaths=/var/lib/relay-price`、`RestartPreventExitStatus=2`，不要删。

---

## 4. 路径 C：Windows 本机

```powershell
# 双击「启动服务.bat」，或手动：
python -X utf8 server.py --open
```

本地模式（默认）只监听 `127.0.0.1`，认证默认关闭，数据就放在项目目录下。这条路径**不要**加 `RELAY_MODE=server`，也不要对公网开端口。

---

## 5. 硬约束（违反即视为部署失败）

1. **不要把业务端口发布到 `0.0.0.0`**。只到回环，公网入口交给反代；要对外必须同时配 `RELAY_ALLOWED_ORIGINS` 与 `RELAY_ALLOWED_HOSTS`，且协议/域名完全一致。
2. **不要跳过 `--init-auth`**。`server` 模式没开访问保护会以退出码 1 拒绝启动；开了却没有 `auth.json` 同样退出 1，表现就是容器重启循环。
3. **不要为了让服务起来就设 `RELAY_ALLOW_UNAUTHENTICATED_REMOTE=1`**。它同时关掉启动拦截和外部来源拦截，只在前方确实有做鉴权的反代/网关时才成立。
4. **不要把 `stop_grace_period` / `TimeoutStopSec` 调到 45s 以下**。优雅停机要这段时间把状态写完，砍了会丢数据。
5. **生产不要用 `:latest` / `:dev-latest`**。锁版本号或 digest。
6. **不要编辑容器内的 `sites.json`**。它是状态目录文件，改宿主上那一份。
7. **不要把凭据写进 compose、命令行或 Git**。凭据只放 `sites.json`（已被 `.gitignore` 排除）。
8. **不要 `docker compose down -v`**。卷挂的是宿主目录，但误删仍可能带走状态。

---

## 6. 环境变量（server 模式必填项）

| 变量 | 示例 | 说明 |
| --- | --- | --- |
| `RELAY_MODE` | `server` | `local`（默认）/ `server` |
| `RELAY_STATE_DIR` | `/var/lib/relay-price` | 配置、快照、日志、认证、实例锁的唯一目录 |
| `RELAY_ALLOWED_ORIGINS` | `https://relay.example.com` | 精确来源，不接受通配符或 `null` |
| `RELAY_ALLOWED_HOSTS` | `relay.example.com,127.0.0.1:8765` | 精确 Host（含端口） |
| `RELAY_HOST` / `RELAY_PORT` | `0.0.0.0` / `8765` | 容器内监听地址与端口 |
| `RELAY_AUTH_DEFAULT_ENABLED` | `1` | 仅首次没有 `auth.json` 时生效 |

可选：`RELAY_TRUSTED_PROXIES`（只有这些 TCP 对端可以提供 `X-Forwarded-For`，精确 IP，不支持网段）、`RELAY_STRICT_PORT`（默认 1）、`RELAY_VERSION_KEEP` / `RELAY_VERSION_KEEP_DAYS`（默认 30 / 30）。

`local` 模式不接受 `RELAY_ALLOWED_ORIGINS`、`RELAY_ALLOWED_HOSTS`、`RELAY_STRICT_PORT`，设了会直接报错。

---

## 7. 验收清单（全过才算完成）

```bash
curl --noproxy '*' -fsS http://127.0.0.1:8765/healthz                      # ok=true，version 与预期一致
sudo stat -c '%U:%G %a' /var/lib/relay-price                              # 10001:10001 700（Docker）
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/login      # 200
sudo docker compose logs --tail=50 app                                     # 无 Traceback
```

浏览器再确认四件事：**能登录 → 站点列表非空 → 手动采集一次有数据 → 刷新页面数据还在**。

最后，如果服务器上有源码副本，再跑一次部署自检（只用合成配置，不访问任何站点）：

```bash
python -X utf8 tests/smoke_deploy.py --mode docker    # systemd / 本机用 --mode process
```

---

## 8. 出问题时的判定路径

| 现象 | 判定 | 动作 |
| --- | --- | --- |
| 退出码 1，日志「认证已开启但没有有效密码」 | 没跑 `--init-auth` | 回到路径 A 第 4 步 |
| 退出码 1，日志「访问保护未开启」 | `server` 模式没开认证 | 跑 `--init-auth`；**不要**用 `RELAY_ALLOW_UNAUTHENTICATED_REMOTE` 绕 |
| 退出码 2 | 状态目录被另一个实例锁住 | `docker compose down` 或杀掉旧进程；`.lock` 里 PID 已失效会自动接管 |
| 启动报权限错误 | 宿主目录属主不是 10001 | `sudo chown -R 10001:10001 /var/lib/relay-price` |
| `/healthz` 503 | 状态目录不可写 | 查挂载、磁盘、SELinux/AppArmor |
| 页面 403 | `RELAY_ALLOWED_HOSTS` 缺当前域名 | 补上并重启 |
| 登录后立刻跳回 `/login` | 协议不一致（用 http 却把来源写成 https） | 两边改成一致 |
| 拉镜像 401 | 本地残留旧 GHCR 凭据 | `docker logout ghcr.io` 后重试 |

仍无解就收集这三样贴给人类，不要自己改镜像或改代码：

```bash
sudo docker compose ps
sudo docker compose logs --tail=200 app
sudo docker compose config        # 确认环境变量真的生效
```

---

## 9. 回滚

```bash
cd /etc/relay-price
sudo sed -i 's|api-relay-price:.*|api-relay-price:<上一个版本号>|' compose.yaml
sudo docker compose pull && sudo docker compose up -d
```

状态目录不动，配置和快照都还在。真要干净卸载：`docker compose down`，再手工删 `/etc/relay-price` 与 `/var/lib/relay-price`（**删前先备份 `sites.json` 和 `settings.json`**）。

---

## 10. 交付汇报模板

部署完成后按这个格式回话，不要只说「已完成」：

```
部署方式：Docker Compose / systemd / Windows 本机
版本：1.0.x（镜像 digest 前 12 位）
访问地址：https://relay.example.com
状态目录：/var/lib/relay-price
访问保护：已开启，账号 admin（密码见下，请首次登录后修改）
验收：healthz ok / login 200 / 站点列表非空 / 手动采集成功 / 刷新后数据保留
定时任务：如实填写开关、时区与计划时间；未验证实际执行时须注明
待你操作：站点凭据需要在「系统设置 → 站点设置」里逐站填写
```
