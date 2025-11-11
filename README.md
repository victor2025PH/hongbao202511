# 🧧 Telegram 红包 & 公开交友群系统（综合开发文档）

一个构建在 Telegram 生态之上的综合互动平台，包含红包玩法、资产中心、后台管理、充值支付与“基于星星积分驱动的公开交友群”功能。本文档将原有 README 与 `READMEV2.md` 信息融合，提供统一的技术说明、目录指引、接口能力与开发路线图。

---

## 🧠 项目综览
- **核心场景**：抢红包 / 排行榜 / 福利中心 / 充值 / 公开交友群。
- **覆盖终端**：Telegram Bot、Web Admin（FastAPI）、MiniApp（前端另仓）、外部监控与自检脚本。
- **技术栈**：Python 3.11、Aiogram 3.x、FastAPI、SQLAlchemy、Prometheus、NOWPayments。
- **多语言**：当前支持中文/英文，文案统一托管在 `core/i18n/messages/*`。
- **星星积分闭环**：用户可赚取“小星星”，并在公开群功能中消耗星星创建群、置顶曝光或获取入群奖励。

---

## 🏗️ 架构与组件
- **Bot 应用 (`app.py`)**：Telegram 机器人入口，注册消息路由、FSM、Profile 同步中间件，并装配红包、资产、公开群等业务逻辑。
- **配置中心 (`config/settings.py`)**：加载 `.env` 配置，提供 `Settings` 单例、命名空间（充值/NOWPayments/AI）与 `is_admin` 判定。
- **业务逻辑 (`routers/*.py` + `services/*.py`)**：拆分红包、充值、福利、余额等路由与服务；公开群将在此处新增模型与接口。
- **Web Admin (`web_admin/*`)**：FastAPI 管理后台，提供健康检查、Prometheus 指标、后台菜单、敏感操作审计、登录/2FA 等能力。
- **工具脚本 (`scripts/*`)**：环境变量校验、自检脚本（运行 `/healthz` 与回归测试）等。
- **测试 (`tests/*`)**：Pytest 回归用例，覆盖红包扣款、充值 fallback、审计日志等；后续会补充公开群测试。
- **文档与规范**：本 README 提供统一说明；品牌/提示词/风控策略等信息已整合自 `READMEV2.md`。

---

## 📂 最新目录结构与接入说明

| 路径 | 角色与职责 | 接入/使用要点 |
| --- | --- | --- |
| `app.py` | Telegram Bot 启动入口，初始化 `Dispatcher`、中间件、路由 | 运行 `python app.py`，需要先加载 `.env` 并确保数据库可达 |
| `config/load_env.py` | 稳健加载 `.env`（向上查找，兼容 BOM） | 在 `app.py` 首行调用，确保在导入模型前完成环境变量注入 |
| `config/settings.py` | 读取配置、封装 `Settings` 数据类，提供 `is_admin` | 所有组件通过 `from config.settings import settings` 使用 |
| `core/i18n/` | 多语言工具：`i18n.py` & YAML 文案 | 通过 `i18n.t("key", lang)` 获取多语言文本 |
| `core/middlewares/` | Aiogram 中间件：错误处理、节流、用户引导等 | 在 `app.py` 注册，需按需开启/配置 |
| `core/utils/keyboards.py` | Bot 内联键盘工具，兼容旧函数名 | 被红包、菜单等路由复用 |
| `models/db.py` | SQLAlchemy 引擎、Session、Base | `init_db()` 初始化，公开群模型需在此注册 |
| `models/*.py` | 用户、红包、账本、封面、公开群等 ORM 模型 | 公开群模型位于 `models/public_group.py`，关联成员与奖励表 |
| `routers/*.py` | Aiogram 路由：红包、充值、邀请、公开群、后台命令等 | `_register_routers` 自动 include；公开群命令在 `routers/public_group.py` |
| `services/*.py` | 业务服务层（充值、红包、邀请、导出、公开群、AI 等） | 公开群服务位于 `services/public_group_service.py`，提供创建/加入/置顶/列表 |
| `miniapp/main.py` | FastAPI MiniApp API，公开群 REST 接口 | `uvicorn miniapp.main:app --reload`；通过 Header `X-TG-USER-ID` 识别调用者 |
| `web_admin/main.py` | FastAPI 管理后台应用，含 `/healthz` `/readyz` `/metrics` | 以 `uvicorn web_admin.main:app` 启动；支持监控与审计 |
| `web_admin/services/audit_service.py` | 敏感操作审计，去重记录与查询 | `record_audit()` 在后台操作中调用，可扩展通知 |
| `scripts/check_env.py` | 检查 `.env.example` 与 `Settings` 要求的一致性 | 本地或 CI 执行 `python scripts/check_env.py` |
| `scripts/self_check.py` | 自检：自动以 SQLite 启动 `app.py`、访问 `/healthz`、执行 `check_env` 与回归测试 | `python scripts/self_check.py`，输出 JSON 结果 |
| `tests/test_regression_features.py` | 回归测试集 | 跑 `pytest -q tests/test_regression_features.py` 确保关键逻辑稳定 |
| `tests/test_public_group_service.py` | 公开群服务单元测试 | 覆盖创建、加入、置顶、风控；执行 `pytest tests/test_public_group_service.py` |
| `README.md`（本文） | 综合开发文档，整合背景、目录、接口、风控等 | 作为后续开发的统一参考 |
| `READMEV2.md` | 已整合入本文件，可按需保留历史版本参考 | 推荐以本 README 为准 |

## 💘 公开交友群功能（延伸自 Telegram 群组）
公开群功能完全基于 Telegram 已有能力扩展，并通过 MiniApp 展示：

- **群组载体**：所有公开群均为 Telegram 上的真实群组，由 Bot 创建或绑定，沿用 Telegram 权限、入群流程与聊天体验。
- **邀请链接卡片**：创建/绑定成功后，会将 Telegram 群的邀请链接、描述等信息整理成卡片数据，推送到 MiniApp。前端在群广场以卡片形式展示，用户点击卡片即可跳转 Telegram 加入。
- **不需自建群功能**：本项目不另起炉灶开发自有聊天室。所有群聊、成员管理、消息能力均依赖 Telegram 官方接口，只在后端记录元数据（邀请链接、标签、风控状态等）。
- **星星积分闭环**：创建公开群、置顶曝光消耗星星；新用户首次进入群可领取星星奖励。风控策略确保积分不会被滥用。
- **菜单触发**：
  - **Bot 端**：在 `/menu` 或主面板中新增“公开群”入口，可通过 `/groups` 查看列表、`/group_create` 创建（限管理员）、`/group_pin` 置顶。
  - **Web Admin**：侧边导航新增“公开交友群”菜单，供运营方审核群组、管理置顶、查看风控状态。
- **管理后台**：`/admin/public-groups` 提供风控面板，可查看待审核/高风险/已暂停/已移除群组，并支持单笔或批量通过、暂停、移除及导出活动策略。

---

## 📊 功能模块与开发重点
- **红包系统**：发放/抢红包、排行榜、资产扣减与记录。
- **充值与支付**：集成 NOWPayments，多币种支持、IPN 回调、失败重试。
- **后台管理**：账户登录、2FA、敏感操作二次确认与审计日志。
- **公开交友群（MVP）**：
  - 模型设计：群定义、成员/奖励/扣费等表。
  - 服务接口：创建群、加入、置顶/取消、列表、举报。
  - 风控策略：创建/置顶限额、奖励池、反刷、敏感词、举报裁决。
  - MiniApp 协同：群卡片数据、提示词优化、品牌视觉。
- **运营工具强化**：
  - Web Admin：公开群列表支援复选器与批量操作（approve/pause/remove/review），执行后写入审计日志。
  - 自动化活动：新增批量暂停/启用/参数调整与 CSV 导出，方便运营一次更新多场次。
  - 报表自动化：`scripts/activity_report_cron.py` 支援 Webhook/Slack 成功率字段、定时推送与 JSON 摘要。

---

<a id="env-vars"></a>
## ⚙️ 环境变量（摘自 `.env.example`）

| 键名 | 示例值 / 默认值 | 说明 |
| --- | --- | --- |
| `BOT_TOKEN` | `123456:ABCDE-your-bot-token` | Telegram Bot Token（来自 @BotFather） |
| `DATABASE_URL` | `sqlite:///./data.sqlite` | SQLAlchemy 数据库连接串，建议生产使用 PostgreSQL |
| `ADMIN_IDS` / `SUPER_ADMINS` | `123,456` / 空 | 管理员与超管白名单（参与 `is_admin` 判定） |
| `ALLOW_RESET` | `false` | 是否允许余额清零类敏感操作 |
| `RECHARGE_PROVIDER` | `nowpayments` | 充值提供方（`mock` / `nowpayments` 等） |
| `NOWPAYMENTS_BASE_URL` | `https://api.nowpayments.io/v1` | NOWPayments API 根路径 |
| `NOWPAYMENTS_API_KEY` / `NOWPAYMENTS_IPN_SECRET` / `NOWPAYMENTS_IPN_URL` | 空 | 充值 API 通信凭证 |
| `NP_PAY_COIN_USDT` / `NP_PAY_COIN_TON` | `usdttrc20` / `ton` | NOWPayments 支付币种代码 |
| `ADMIN_WEB_USER` / `ADMIN_WEB_PASSWORD` | `admin` / 空 | Web Admin 登录凭据（建议改成散列版） |
| `ADMIN_SESSION_SECRET` | `change_me_at_least_32_chars` | FastAPI Session 密钥 |
| `ADMIN_TG_ID` / `TELEGRAM_BOT_TOKEN` | `123456789` / 同 `BOT_TOKEN` | 后台 OTP 推送目标 |
| `ADMIN_TOTP_SECRET` | 空 | 开启后台 TOTP 双因子 |
| `ADMIN_LOGIN_MAX_FAILED` / `ADMIN_LOGIN_LOCK_MIN` | `5` / `10` | 登录失败阈值与锁定时长 |
| `DEFAULT_LANG` / `FALLBACK_LANG` | `zh` / `en` | Bot 默认语言与回退语言 |
| `TZ` | `Asia/Manila` | 时区 |
| `AI_PROVIDER` / `OPENAI_MODEL` 等 | `openai` / `gpt-4o-mini` | AI 辅助功能配置（可选） |
| `HB_COVER_CHANNEL_ID` | `-1001234567890` | 红包封面素材频道 ID |
| `GOOGLE_SERVICE_ACCOUNT_PATH` | `secrets/service_account.json` | Google service account 凭证路径（未设置时依序尝试 `secrets/service_account.json` → `service_account.json`） |

更多键请参考 `.env.example`；新增键时需同步更新该文件与本节说明。

---

<a id="verify"></a>
## ✅ 初始化与验证流程

1. **准备配置与依赖**
   ```bash
   cp .env.example .env
   # 按需填写 BOT_TOKEN、DATABASE_URL、NOWPAYMENTS_* 等键值
   pip install -r requirements.txt
   ```
2. **初始化数据库（如首次部署或模型更新）**
   ```bash
   python -c "from models.db import init_db; init_db()"
   ```
3. **启动 Bot / Web Admin**
   ```bash
   python app.py
   uvicorn web_admin.main:app --host 0.0.0.0 --port 8000
   ```
4. **快速健康检查**
   ```bash
   curl http://127.0.0.1:8000/healthz
   curl http://127.0.0.1:8000/readyz
   curl http://127.0.0.1:8000/metrics
   ```
5. **测试与自检（可选）**
   ```bash
   pytest -q tests/test_regression_features.py
   python scripts/check_env.py
  python scripts/self_check.py  # 启动本地 self-check，JSON 中 ok=true 表示通过
   ```

---

## 🚀 容器化部署样板

> 适用于本地联调 / 预生产环境，以单一镜像运行 Bot、Web Admin 与 MiniApp API。生产部署请结合自身的日志、监控与密钥管理策略拓展。

1. **构建镜像**
   ```bash
   docker compose build  # 基于根目录 Dockerfile 构建 redpacket/app:latest
   ```
2. **准备环境变量**  
   默认读取 `.env`，如需与本地开发隔离，可复制为 `.env.docker` 并在 `docker-compose.yml` 中引用。务必填入：
   - `BOT_TOKEN`、`ADMIN_WEB_USER`、`ADMIN_WEB_PASSWORD`
   - `DATABASE_URL=postgresql+psycopg2://redpacket:redpacket@db:5432/redpacket`（或你自己的连接串）
   - `FLAG_ENABLE_PUBLIC_GROUPS=1`（如需启用公开群）
3. **启动服务栈**
   ```bash
   docker compose up -d bot web_admin miniapp_api db redis
   ```
   - `bot`：运行 `python app.py`
   - `web_admin`：通过 `uvicorn web_admin.main:app` 暴露后台（默认端口 8000）
   - `miniapp_api`：通过 `uvicorn miniapp.main:app` 暴露 MiniApp REST（默认端口 8080）
   - `db` / `redis`：可按需保留或移除（如接入托管数据库）
4. **健康检查**
   ```bash
   curl http://127.0.0.1:8000/healthz
   curl http://127.0.0.1:8080/healthz
   ```
5. **日志与维护**
   ```bash
   docker compose logs -f bot
   docker compose exec db psql -U redpacket -d redpacket
   docker compose down    # 关闭所有容器
   ```

---

## 🧪 CI 集成

- 仓库新增 `.github/workflows/ci.yml`，使用 GitHub Actions 在 `push`/`pull_request` 时执行：
  1. 安装依赖（Ubuntu + Python 3.11）；
  2. 运行 `python scripts/self_check.py`，自动完成 `/healthz`、`scripts/check_env.py` 与核心测试集。
- 可在 Fork 或私有仓库中直接启用；如需加速，可结合缓存或替换为内部 CI 工具，只需执行同一脚本即可。
- 如果在自有流水线中使用，建议预先设置：
  ```bash
  export DATABASE_URL=sqlite:///./ci.sqlite
  python scripts/self_check.py
  ```
  避免 CI 不必要地连接外部数据库。

---

## 🌐 多语言与提示词治理
- 文案位于 `core/i18n/messages/*.yml`，通过 `i18n.t(key, lang)` 调用。
- 提示词优化器（Prompt Optimizer）支持多语言、A/B、合规词校验，输出到 `i18n_messages` 表供 Bot/MiniApp 统一读取。
- 新增语言或更新文案时，保持黑名单词过滤（如 bet / profit / cash 等）。

---

## 🧩 Feature Flags
- 默认读取 `config/feature_flags.py` 中的 `flags`；亦可在 `.env` 以 `FLAG_ENABLE_XXX=true/false` 覆盖。
- 典型开关：`ENABLE_WELFARE`、`ENABLE_INVITE`、`ENABLE_RANK_GLOBAL`、`ENABLE_PUBLIC_GROUPS`（将在 T6 引入）。
- 更新 Feature Flag 需同步维护 README 与 `.env.example`。

---

## 🔐 后台登录、二次确认与审计
- 登录入口：`http://<host>:8000/admin/login`
- 支持账号密码 + OTP/TOTP 双因子；超过失败次数自动锁定。
- 余额清零、导出等敏感操作需二次确认并记录审计日志（`web_admin/services/audit_service.py`）。
- 审计日志支持去重、查询与序列化，后续可扩展 Slack/Email 通知。
- 批量操作（群组/活动）会将目标、操作人、修改字段写入 `audit_log`，可搭配数据库或 BI 工具追踪。

---

## 📈 观测与监控
- `GET /healthz`：存活检测。
- `GET /readyz`：静态资源、模板路径、数据库连通性检查。
- `GET /metrics`：Prometheus 指标（应用运行时长、业务计数等），核心指标包含：
  - `app_uptime_seconds`、`app_info`：基础运行信息。
  - `public_group_operation_total` / `public_group_operation_seconds`：`operation=create|join|pin|unpin`，`status` 标记成功/复核/错误等，便于统计公开群创建、加入与置顶的情况。
  - `hongbao_operation_total` / `hongbao_operation_seconds`：`operation=send|grab`，追踪红包发送与抢红包成功/失败、重复操作等状态。
  - `activity_conversion_total` / `activity_conversion_points`：记录自动化活动的入群加码触发次数与累计星数，并区分 `status=success|partial|failed`。
- 运营转化分析：通过 `GET /v1/groups/public/stats/summary` 可取得曝光 / 点击 / 入群汇总，以及热门群排行榜（需管理员权限）。
- 建议集成 Prometheus + Grafana，并结合上述指标配置报警（如公开群创建失败率、红包重复抢占异常等）。
- 活动报表自动化：`python scripts/activity_report_cron.py --days 1 --output-dir reports --json`
  - 默认统计“昨日”区间，生成 `reports/activity_report_<日期>.csv` 与摘要 JSON，CSV 包含 `conversions/webhook_success_rate/webhook_failures/slack_failures` 等列。
  - 支持 `REPORT_OUTPUT_DIR`（自定义输出目录）、`REPORT_SLACK_WEBHOOK`（推送 Slack 摘要）与 `--include-webhooks/--no-include-webhooks`、`--slack-summary` 参数，便于依场景输出详略不同的报表。
  - 可交由 cron / Windows Task Scheduler 定时执行，搭配 CI 产出周期性运营周报，并结合 Prometheus 指标校验 Webhook/Slack 成功率。
- 活动 Webhook / Slack 通知：当自动化活动触发入群加码时，会以 Webhook 与（可选）Slack 通知广播转化结果；可通过 `ACTIVITY_SLACK_WEBHOOK` 或复用 `REPORT_SLACK_WEBHOOK` 指定通知通道，Webhook 请求附带 `X-Activity-Signature`（HMAC-SHA256）供验签。

---

## 🧾 接口与数据模型（公开群相关摘要）
- **主要 REST 接口**（已在 `miniapp/main.py` 实现，调用需携带 `X-TG-USER-ID` Header）：
  - `GET /v1/groups/public`：群列表（支持分页 `limit`、名称搜索 `q`、多标签筛选 `tags`、排序 `sort=default|new|members|reward`；管理员可启用 `include_review` 查看待复核群）。
  - `GET /v1/groups/public/{id}`：群详情。
  - `GET /v1/groups/public/bookmarks`：当前用户收藏的公开群列表（按收藏时间倒序）。
  - `POST /v1/groups/public`：创建群（返回群信息与风控评分）。
  - `POST /v1/groups/public/{id}/join`：加入群并发放入群奖励。
  - `POST /v1/groups/public/{id}/events`：记录 MiniApp 端曝光 / 点击 / 入群事件，并可附加上下文字段，供后续转化分析。
  - `POST /v1/groups/public/{id}/pin` / `POST /v1/groups/public/{id}/unpin`：置顶 / 取消置顶（管理员）。
  - `POST /v1/groups/public/{id}/bookmark` / `DELETE /v1/groups/public/{id}/bookmark`：收藏 / 取消收藏公开群，响应体返回 `bookmarked` 状态，接口具备幂等性。
- `GET /v1/groups/public/activities`：MiniApp 获取活动卡片列表，附带 `front_card`、倒数文字、`has_participated` 等提示资讯（Bot 也共用此摘要）。
- `GET /v1/groups/public/activities/{id}`：MiniApp / Bot 取得单一活动详页数据，包含规则列表、剩余额度、`eligible`、CTA。
- `GET /v1/groups/public/activities/{id}/webhooks`：管理员查看指定活动的 Webhook 配置（仅返回 `is_active=true` 的项目）。
- `POST /v1/groups/public/activities/{id}/webhooks`：管理员创建 / 更新活动 Webhook（重复 URL 自动覆盖），支持设置 `secret` 与启停状态。
- `DELETE /v1/groups/public/activities/webhooks/{webhook_id}?hard=false`：管理员禁用（或加 `hard=true` 直接移除）指定 Webhook。
- `GET /admin/public-groups/activities/insights`：公开群“活动洞察”仪表板，聚合展示活动转化趋势、奖励星数与 Webhook/Slack 健康度。
  - `GET /v1/groups/public/{id}/invite_link`：获取 Telegram 邀请链接。
  - `PATCH /v1/groups/public/{id}`：编辑描述、标签、奖励参数、封面等。
  - `POST /v1/groups/public/{id}/report`：举报群，后台记录以供人工复核。
  - `GET /v1/groups/public/stats/summary`：管理员查询曝光→点击→入群漏斗统计与热门群榜单。
- `GET /admin/public-groups/dashboard`：Web Admin 成效仪表板（需登录），可视化查看曝光/点击/入群趋势、新建群数量、热门标签与热门群排行。
- `GET /admin/public-groups/activities`：自动化活动配置页面，新建 / 暂停入群加码、MiniApp 曝光等活动，可自订活动卡片标题、副标题、CTA 按钮、角标与展示优先级，让 MiniApp/Bot 前台提示更聚焦；页面右上角提供批量工具列与 CSV 导出。
- `GET /admin/public-groups/activities/report`：活动绩效报表（支持日期筛选、异常过滤与 CSV 导出），统计每日发放次数、星数、转化人数、Webhook 成功率与 Slack 失败次数。
- `GET/POST /admin/public-groups/activities`：Web Admin 自动化活动配置（创建 / 暂停入群奖励、额外曝光位与加码规则）。
- `POST /admin/public-groups/bulk/status`：批量审核/暂停/下架公开群。
- `POST /admin/public-groups/activities/bulk`：批量暂停/启用活动，或一次调整奖励、限额、曝光位与高亮设定。
- `GET /admin/public-groups/activities/export`：将所选活动（或全部）导出为 CSV，方便制订预算或跨团队同步。
- **核心表结构（规划中）**：
  - `public_groups`：群主、Telegram Chat ID、名称、简介、标签、语言、奖励开关、置顶状态等。
  - `group_cost_ledger`：群创建/置顶的星星扣费记录。
  - `group_entry_rewards`：入群奖励明细与去重。
- **风控策略**：
  - 创建需余额 ≥ 阈值，每日创建上限，敏感词过滤。
  - 置顶次数与时长限制，扣费失败回滚。
  - 入群奖励去重、冷却、奖励池上限、反刷判断（IP/设备/短期频率）。
  - 举报闭环：自动降权/下架 + 人工复核，记录审计。

---

## 🛠️ 开发范围与阶段性里程碑
- **M1（当前迭代）**：公开群模型/服务/路由、基本列表与创建、Telegram 建群回调、MiniApp 卡片数据。
- **M2**：入群奖励发放、奖励池管理、风控限频与举报系统。
- **M3**：置顶续费、群广场排行榜、数据面板、提示词优化器接入。
- **M4**：小游戏整合、更多语言支持、监控指标扩展。
- **M5**：运营工具（批量审核、活动脚本）、国际化与品牌规范落地。

---

## 📚 测试策略
- **单元/集成测试**：使用 `pytest`，对红包扣款、充值 fallback、审计、公开群流程进行覆盖。
- **自检脚本**：`python scripts/self_check.py` 会自动启动应用（SQLite）、运行 `/healthz`、`scripts/check_env.py` 与关键测试，返回 JSON 结果，推荐作为上线前 Smoke。
- **性能与并发**：公开群流程需验证高并发建群/加入场景下的幂等与锁定机制。

---

## 📌 其它提示
- 新增或修改模型后，请运行 `init_db()` 或编写迁移脚本保障生产可用。
- 敏感操作务必伴随审计记录，便于追溯。
- 文档更新需与代码同步，特别是环境变量、目录结构与接口说明。
- 公开群运营材料详见 `docs/public_group_ops.md`，涵盖审核流程、奖励策略与客服 FAQ。
- MiniApp 若要追踪转化，请在曝光/点击时调用 `POST /v1/groups/public/{id}/events`，再由后台使用 `GET /v1/groups/public/stats/summary` 获取漏斗数据。
- Web Admin 「公开交友群」页面已新增「成效仪表板」入口，方便运营即时掌握曝光、点击、入群与热门标签／热门群排名。
- 自动化活动模块现已支持配置「额外奖励星数」「MiniApp 置顶曝光位」等活动，可于 Web Admin → 公开交友群 → 自动化活动 页面管理。
- 活动绩效报表（Web Admin → 公开交友群 → 自动化活动 → 查看报表）可导出 CSV，便于追踪每日发放次数与加码星数。
- 活动洞察仪表板（Web Admin → 公开交友群 → 活动洞察）提供活动转化、奖励星数、Webhook/Slack 成功率与异常提醒。
- Telegram Bot `/groups` 指令已接轨新的活动详页资料：会显示剩余额度、资格状态、CTA 链接，若用户已参与或名额用尽会主动提示。

本 README 将持续迭代，作为红包系统与公开交友群功能的统一开发入口。如需查阅历史版本，可参考 `READMEV2.md`（已整合入本文）。