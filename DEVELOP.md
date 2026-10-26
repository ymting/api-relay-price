# 开发与发布

面向改这个仓库的人。只想部署和使用，看 [`README.md`](README.md)、[`DEPLOY.md`](DEPLOY.md)、[`USER-GUIDE.md`](USER-GUIDE.md)。

## 1. 本地跑起来

```powershell
$env:PYTHONUTF8 = "1"
python -X utf8 server.py --no-schedule     # 开发用，默认端口 8765
```

只依赖 Python 3.10+ 标准库，没有构建步骤。改前端就是改 `中转站比价.html`（样式与脚本都内联在里面），改完 Ctrl+F5。

`python -X utf8 fetch_prices.py --only <site-id>` 会真实联网并写快照，别随手跑。

## 2. 测试

```powershell
$env:PYTHONUTF8 = "1"                      # Windows 子进程也统一 UTF-8
python -X utf8 -m unittest discover -s tests -v
node tests/test_stage_a_ui.js              # 计价与页面
node tests/test_site_framework_ui.js       # 站点配置
python -X utf8 tests/check_page_syntax.py  # 从 HTML 抽取内联脚本做语法检查
python -X utf8 tests/check_stage_d_browser.py   # 需本机 Edge
python -X utf8 tests/check_key_ccs_browser.py   # 创建/导入交互，隔离远端与系统唤起
python -X utf8 diagnose_account_auth.py    # 只读比对账户鉴权形式，结果写 logs/
python -X utf8 tests/smoke_deploy.py --mode process   # 部署自检；有 Docker 用 --mode docker
```

约定：

- 合成响应、mock、临时目录；**不使用真实凭据**。
- 改动涉及凭据隔离、分页不完整、快照过期、写入结果不明时，必须补回归。
- UI 改动额外跑 Edge 合成浏览器检查。
- 本地 fixture 的结论和真实站点验证要**分开报告**，不能拿一次只读测试推定全部完成。

最近一轮（2026-10-19）后端 351 项（含 Key 交互 22 项专项）、Node 页面 52 项、Edge 46+21 项通过；均为合成接口，没有真实 Key 写入或 CCS 导入。

## 3. 目录与文件

| 路径 | 作用 |
| --- | --- |
| `启动服务.bat` | 主入口：启动本地服务 + 打开浏览器 |
| `server.py` | 本地服务：配置读写、手动采集、每日定时、账户快照、数据接口（只监听 127.0.0.1） |
| `fetch_prices.py` | 采集与归一化核心；命令行入口，取数走 `integrations` 统一路由 |
| `integrations/` | 接口接入层，见下节 |
| `中转站比价.html` | 前端页面（服务模式下能力完整，双击打开时只读快照） |
| `sites.json` / `settings.json` | 站点清单（含凭据）与偏好设置，**都不进版本库**；每次保存把上一版归档到 `.versions/`（保留最近 30 份 ∪ 30 天，同样含凭据） |
| `data/prices.json`、`prices.js`、`index.json` | 本地数据快照，网页只读这三个 |
| `data/accounts.json` | 各站余额 / 用量 / Key 快照 |
| `data/health.json` | 显式更新后的模型分组健康统计 |
| `data/write_operations.json` | Key 创建/修改/删除记录（**不含明文**），重启后据此对账 |
| `data/evidence_matrix.jsonl` | 「站点 × 功能」调用证据（提供者/模式/触发源/成败/耗时，**不含凭据**） |
| `tests/` | A–E 各阶段合成专项测试 |
| `bump_version.py` | 版本号维护：`--dev` 递增序号、`--release` 去后缀定版（`VERSION` 是唯一来源） |
| `diagnose_account_auth.py` | 只读比对账户鉴权形式，结果写 `logs/`，不发写请求 |
| `Dockerfile`、`compose.example.yaml`、`deploy/` | 容器镜像、Compose 样例、systemd unit、Caddy 反代样例、部署冒烟脚本 |
| `DEPLOY.md` / `AGENT-DEPLOY.md` | 人类部署手册 / Agent 可执行部署指引 |
| `USER-GUIDE.md` | 功能使用详解 |
| `docs/`、`memory/` | 设计文档与项目记忆，本机目录，已在 `.gitignore` 排除 |

`index.json` 是轻量索引（压缩后约 8KB），首屏秒开；某模型的完整报价在点开时才按需请求。`采集价格.bat` 是命令行采集入口，加 `--debug` 会多打印每站逐功能诊断（提供者、端点、状态），只输出到控制台、不写快照。

## 4. 接口接入层现状（2026-10-15）

已完成 **阶段 A（A1–A4）、B 统一契约及错误分类、C 平台提供者、D 路由器/兼容包装与配置迁移**；
**E（全功能验收与兼容退出）进行中**：F01–F12 fixture 与退出闸门已落地，实站验收与逐功能退出兼容尚未执行。

生产入口已切到统一服务，旧 Cookie → 账号密码链路退化为**可选注册**的兼容提供者（可在「系统设置 → 接口接入与兼容策略」整体关闭）。

模块：

- `contracts`：契约与错误分类
- `providers`：两平台接口
- `sub2api_views`：支付／兑换与 V1/V2 监控的脱敏投影
- `router`：模式与回退
- `compat`：旧 Cookie／账号密码链路，可整体停用
- `service`：结果投影
- `configuration`：配置合并与原子轮换
- `config_backup`：配置备份导出与整体还原
- `journal`：写操作记录
- `ccs`：CC Switch 链接编码
- `pricing`：价格解析

调用链：账户刷新、Key 增删改查明文、CCS 导出、模型健康、价格采集、CLI 与定时任务都经同一路由取结果。

口径要点：

- New API 分档表达式按短上下文选档并保留表达式；无法解释时显示未知。
- Sub2API token 单价从 USD/token 换算为 USD/百万 token，个人倍率优先；按次价格保持 USD/次。异常低价拒绝入库；未确认的分时、生图或分组倍率不参与最低价排名。
- New API 充值用 `/api/user/topup/self`；Sub2API 用支付订单与兑换历史。充值、Key 列表、创建前后回查和 CCS 选 Key 共用分页读取，处理总数、去重、重复页、变化总数及安全上限；不完整列表不触发自动新建。
- 汇总金额只纳入**单位已确认**的钱包余额；只有旧口径（`quota_usd`）的站点不进跨站合计，卡片仍显示并标注历史数据。
- New API 钱包按本站 `/api/status` 的 `quota_per_unit` 换算成系统美元；分叉显式返回币种时尊重该币种；缺失或无效换算比例显示未知，不固定套 500000。
- New API 默认只发 Bearer 管理令牌。旧站明确返回「未提供 New-Api-User」且填了预期账户 ID 时，只对账户资料读取补该头再验证一次；回包 ID 一致后本次会话后续请求才沿用。普通权限拒绝、无效令牌、人机验证不重试、不切 Cookie，Key 写请求也不重放。

每次采集/刷新/Key 操作都会在 `data/evidence_matrix.jsonl` 记一行「站点 × 功能」证据（不含凭据）。`GET /api/compat/retirement` 会回答每个站点每个功能**能不能停用旧链路**、还差什么。只读接口，不发远端请求。

## 5. 版本号与分支

`main` 只保留已发布的正式版本，日常改动全部在 `dev` 上。

| 分支 | `VERSION` 形态 | 镜像标签 | 何时构建镜像 |
| --- | --- | --- | --- |
| `dev` | `1.0.2-1`、`1.0.2-2`……（生产 patch +1，末尾 `-n` 递增） | `dev-latest`、版本号、`sha-xxxxxxx` | 每次推送 `dev` |
| `main` | `1.0.2`（与发布标签一致） | `latest`、版本号、`sha-xxxxxxx` | 只在发布 Release 时 |

日常开发：

```powershell
python bump_version.py --dev     # 1.0.2-1 → 1.0.2-2，提交前跑一次
git add -A
git commit -m "说明本次修改"
git push
```

推 `dev` 后 CI 跑完整回归，镜像同时产出 `dev-latest`。

正式发版：

```powershell
python bump_version.py --release  # 1.0.2-4 → 1.0.2
git commit -am "发版 1.0.2"
git checkout main
git merge --ff-only dev           # main 只接受来自 dev 的快进合并
git push
gh release create v1.0.2 --target main --title "v1.0.2" --notes "变更说明"

git checkout dev
python bump_version.py --dev      # 1.0.2 → 1.0.3-1，开下一轮
git commit -am "开始 1.0.3 开发"
git push
```

版本号由 CI 把关：`dev` 的 `VERSION` 必须是 `X.Y.Z-n`；Release 标签必须与 `VERSION` 完全一致且不带后缀，标签所在提交必须在 `main` 上。

## 6. 什么进版本库

进：源码、启动脚本、站点配置模板（`sites.example.json`）、测试、文档。

不进：`sites.json` / `settings.json` 及其备份与 `.versions/`、`auth.json`、`data/`、`logs/`、`.env`、`_` 前缀本机自检脚本、`docs/`、`memory/`。

被排除的这些 Git 恢复不了，需要单独备份。
