# Public Group Feature — Operations Playbook

## 1. 概览
- **目标用户**：运营团队、客服与审核人员。
- **功能范围**：Telegram 公开交友群的创建审批、内容风控、奖励池管理、MiniApp 卡片维护、数据监控与活动策划。
- **系统依赖**：
  - Telegram Bot (`app.py`) 负责建群、命令交互与积分扣费。
  - Web Admin (`/admin/public-groups`) 提供审核与风控操作。
  - MiniApp API (`miniapp/main.py`) 向前端推送群卡片与状态。
  - Prometheus 指标：`public_group_operation_total`、`public_group_operation_seconds`。

## 2. 角色与职责
| 角色 | 职责 | 所需权限 |
| --- | --- | --- |
| **内容运营** | 设计群主题、编辑卡片信息、配置奖励池 | 后台登录、公开群菜单访问 |
| **审核员** | 审核新建群、处理举报、执行暂停/下架 | 后台登录、`FLAG_ENABLE_PUBLIC_GROUPS` |
| **客服** | 处理用户反馈、补发奖励、协调问题群移除 | 后台登录、基础查询 |
| **技术支持** | 监控指标、处理异常订单、维护脚本 | 服务器访问、日志读取、CI 权限 |

## 3. 操作流程
### 3.1 创建与上线
1. 运营在 Telegram 与 Bot 交互（`/group_create`）或后台录入现有群的邀请链接。
2. 系统进行风险评估与星星扣费，生成群卡片草稿。
3. 审核员在后台 `待审核` 列表检查：
   - 名称/简介是否合规；
   - 标签是否准确；
   - 邀请链接是否有效；
   - 风险评分与风险标记。
4. 确认无误后点击 **通过**；若需补充信息，可先 **暂停** 并联系运营修改。

### 3.2 入群奖励配置
- 奖励池字段：`entry_reward_enabled`、`entry_reward_points`、`entry_reward_pool`、`entry_reward_pool_max`。
- 建议策略：
  - `entry_reward_points` 控制在 5–20 星。
  - `entry_reward_pool_max` 取决于预算（默认 0 表示无限制）。
  - 确保池内余额 ≥ 单次奖励 × 50，避免频繁补充。
  - 当奖励池耗尽时系统自动停止发放，可在后台调整后再 **通过**。

### 3.3 置顶与露出
- 置顶步骤：后台点击 **通过 > 置顶**，系统扣除星星并设置 `pinned_until`。
- 推荐限制：同一时间置顶不超过 3 个群，确保 MiniApp 首屏内容新鲜。
- 到期后需人工解除或系统定时任务自动清理（可在 `services/public_group_service.py` 扩展）。

### 3.4 举报处理
1. 用户在 MiniApp 通过 **举报** 按钮提交原因。
2. 后台 `高风险` 列表出现新条目，查看 `risk_flags` 与举报内容。
3. 审核员可：
   - **暂停**：临时禁止曝光；
   - **下架**：彻底移除并记录原因；
   - **恢复审核**：待复核后重新上线。

### 3.5 用户收藏与追踪回访
- MiniApp 用户可将心仪群组加入收藏，接口如下：
  - `POST /v1/groups/public/{id}/bookmark`：收藏群组（幂等，首次返回 201）。
  - `DELETE /v1/groups/public/{id}/bookmark`：取消收藏（始终返回 `bookmarked=false`）。
  - `GET /v1/groups/public/bookmarks`：获取用户收藏列表（含 `is_bookmarked=true` 标识）。
- 列表与详情接口会携带 `is_bookmarked` 字段，方便前端同步显示收藏状态。
- 运营可通过收藏数据判断潜在热门群，结合活动模块推送回访或定向通知。

### 3.6 批量审核与活动批量管理
- **公开群批量动作**
  1. 进入 Web Admin `公开交友群` 页面，勾选待处理的群组（支持待审核、高风险、已暂停、已移除等分栏）。
  2. 在批量操作下拉选「通过 / 暂停 / 移除 / 恢复审核」，可加上备注方便客服追踪。
  3. 执行后会显示成功/失败摘要；成功项目会立即刷新状态，并写入审计日志。
- **活动批次调整**
  1. 前往 `公开交友群 → 自动化活动`，勾选多个活动。
  2. 选用批量按钮「暂停 / 启用」或填写奖励、限额、曝光位、高亮等字段后点击「套用更新」。
  3. 若部分活动更新失败（例如数值不合法），系统会在回传中列出错误明细，保留已成功的项目。
- **活动资料导出**
  - 「导出已选」会下载当前勾选的活动清单；「导出全部」则输出所有活动的 CSV，包含奖励、限额、状态、开始/结束时间等字段，方便进行预算盘点或档案归档。

> **Google 凭证存放建议**
>
> - 将 `service_account.json` 复制到仓库根目录的 `secrets/` 资料夹（该目录已加入 `.gitignore` 不会被提交）。
> - 若凭证不在默认位置，可在环境变量中设置 `GOOGLE_SERVICE_ACCOUNT_PATH=/absolute/path/to/service_account.json`。
> - 建议同时保留一份加密或离线备份（例如密码保护的压缩包），避免遗失造成脚本无法写入 Google Sheet。

## 4. 日常维护清单
| 频率 | 项目 | 检查要点 |
| --- | --- | --- |
| 每日 | 审核待审核列表 | 新增群是否合规、奖励池余额是否足够 |
| 每日 | 监控指标 | `public_group_operation_total{status="unexpected"}` 需为零 |
| 每日 | 用户反馈 | 收集 Telegram、客服渠道问题并回访 |
| 每周 | 奖励池调整 | 统计新增/流失数据，平衡奖励成本与效果 |
| 每周 | 活动策划 | 更新置顶位内容，策划主题周或联动活动 |
| 每月 | 数据复盘 | 导出运营数据（待接入 BI），复盘关键词与风控命中 |

## 5. 风控策略参考
- **关键词过滤**：创建/编辑时过滤赌博、借贷、成人、灰产等高风险词。
- **频率限制**：
  - 单用户每日建群上限；
  - 置顶刷新间隔 ≥ 6 小时；
  - 入群奖励同一用户/同一群仅发放一次。
- **异常触发**：
  - `risk_score` ≥ 阈值（默认 `RISK_SCORE_THRESHOLD_REVIEW`）自动转为待审核；
  - 24 小时内举报量 ≥ 3，自动暂停并通知审核员；
  - 同一邀请链接重复出现在多个群，标记 `duplicate_invite_link`。

## 6. 数据与监控
- Prometheus 指标：
  - `public_group_operation_total{operation="create|join|pin|unpin", status}`；
  - `public_group_operation_seconds` 反映处理时延；
  - `app_uptime_seconds` 监控后台存活。
- 建议图表：
  - 每日入群人数 / 新建群数；
  - 举报趋势与处理效率；
  - 置顶曝光与点击率（MiniApp 侧需配合埋点）。
- 转化漏斗：
  - MiniApp 在曝光、点击、成功加入时调用 `POST /v1/groups/public/{id}/events`；
  - 后台管理员使用 `GET /v1/groups/public/stats/summary?period=7d` 获取曝光→点击→入群统计及热门群榜单；
  - 建议每周导出 JSON/CSV，结合 BI 工具观察留存、转化与投放效果。
- 仪表板入口：`Web Admin → 公开交友群 → 成效仪表板`，可快速查看：
  - 最近 7/14/30 日曝光、点击、入群趋势；
  - 新增群数量、状态分布；
  - 热门标签与热门群排行（含曝光/点击/入群指标）。
- 活动 Webhook：
  - 通过 `POST /v1/groups/public/activities/{activity_id}/webhooks` 配置第三方回调，事件触发时会附带 JSON 负载及 `X-Activity-Signature`（HMAC-SHA256）。
  - Webhook 的成败不影响业务流程，失败会记录在应用日志中；可用 `hard=true` 的 DELETE 请求彻底移除。
  - 若配置 `ACTIVITY_SLACK_WEBHOOK`（可复用 `REPORT_SLACK_WEBHOOK`），系统会同步推送 Slack 摘要，便于运营团队追踪活动成效。
  - 所有成功/失败事件会写入 `public_group_activity_conversion_logs`，后续仪表板与报表可据此呈现每日转换趋势与Webhook成功率。
- 活动洞察仪表板：`Web Admin → 公开交友群 → 活动洞察` 可查看每日转化趋势、热门活动排行榜与 Webhook/Slack 异常提醒。

## 7. 文案与卡片规范
- **标题**：20 字以内，突出主题 + 人群（例：“夜猫学习局”）。
- **简介**：50–100 字，说明群定位、活动频率与加群礼仪。
- **标签**：3–5 个，统一小写，覆盖场景（study、game、music）。
- **封面图**：使用品牌色 #FF675C/#FFF0E8，保持一致视觉识别。
- **MiniApp 卡片**：确保 CTA 明确（如“加入群聊”、“立即领取 10 星”）。

## 8. 自动化活动模块
- **业务场景**：配置特定时段的入群加码奖励、MiniApp 置顶曝光、节庆主题周等。
- **入口**：`Web Admin → 公开交友群 → 自动化活动`。
- **核心字段**：
  - `活动名称/简介`：明确主题与规则；
  - `开始/结束时间`：可选，留空表示随时生效或永久；
  - `基础奖励 / 额外奖励`：基础奖励维持既有入群星数，额外奖励为活动加码；
  - `每日 / 总上限`：防止预算溢出；
  - `额外曝光位`：MiniApp 卡片置顶数量；
  - `启用曝光`：是否在 MiniApp 重点位置露出。
- **操作流程**：
  1. 在创建表单填写活动参数，可先保持草稿（默认立即上线，如需预存可随后暂停）。
  2. 通过活动列表查看状态（draft/active/paused/ended），随时暂停或恢复。
  3. 与仪表板结合，观察活动期间曝光/点击/入群变化，评估成效。
- **前台提示设置**：
  - 「前台标题 / 副标题」会同步呈现在 MiniApp 活动卡片与机器人 `/groups` 提示。
  - 「按钮文字 / 链接」可引导用户快速加入指定群或活动说明页，未填写时沿用默认处理。
  - 「展示优先级」数字越小越靠前，可依活动节奏安排（默认 100）。
  - 建议搭配角标（例如「⚡ 限时加码」）强化视觉辨识度。
  - MiniApp 可调用 `GET /v1/groups/public/activities/{activity_id}` 展示弹窗详情；Bot `/groups` 指令已整合同一数据，会提示剩余额度、资格状态与 CTA。
- **与转化统计协作**：
  - Auto 活动产生的加码会记录在 `public_group_activity_logs` 中；
  - 搭配 `GET /v1/groups/public/stats/summary` 或仪表板查看影响；
  - 建议每周回顾活动阻塞指标（Daily cap、Total cap、热度）。
- **绩效报表**：
  - 入口：`Web Admin → 公开交友群 → 自动化活动 → 查看报表`；
  - 可按日期范围查看每天发放次数与奖励星数，并支持 CSV 导出；
  - 建议每周复盘，评估活动 ROI 与预算消耗。
  - 若需自动化周/月报，可在服务器排程执行 `python scripts/activity_report_cron.py --days 1 --output-dir reports --include-webhooks --slack-summary --json`，CSV 将附带 `conversions`、Webhook 成功率、失败次数与 Slack 告警栏位。
  - 透过 `--no-include-webhooks` 可仅输出发放/星数摘要；`REPORT_SLACK_WEBHOOK` 设定后脚本会推送含热门活动 Top3 的 Slack 摘要。
  - 如需快速调整多场活动，可先在页面勾选并套用批量更新，再以导出功能下载校对用 CSV，避免逐笔修改造成落差。

## 9. FAQ 模板（可给客服使用）
| 问题 | 回答 |
| --- | --- |
| 为什么无法领取入群奖励？ | 确认是否曾加入过该群，奖励每人仅限一次；若奖励池耗尽，请等待运营补充。 |
| 群违规该如何处理？ | 在 MiniApp 点击举报，或提供截图给客服；我们会在 24 小时内处理。 |
| 如何成为官方合作群？ | 提交申请群表格（运营提供），满足质量标准后可获得置顶和额外曝光。 |
| 入群链接失效 | 在 Web Admin 重新编辑群资料，上传最新邀请链接。 |

## 10. 运维与应急
- 若 Bot 掉线：
  - 检查 `docker compose ps` 状态；
  - 查看 `docker compose logs bot`；
  - 重新执行 `docker compose restart bot`。
- 若后台访问异常：
  - 检查 `/healthz`、`/readyz`；
  - 查看 `web_admin` 服务日志；
  - 运行 `python scripts/self_check.py` 做快速诊断。
- 若数据库异常：
  - 使用 `docker compose exec db psql -U redpacket -d redpacket` 排查；
  - 定期备份 `db_data` 卷，防止误操作。

## 11. 后续规划
- 接入自动化审核策略（敏感词库、机器学习评分）。
- 完善运营数据导出接口，与现有导出服务打通。
- 制定公开群 KPI（每日新增、活跃率、举报响应时间）。
- 提供标准活动模板（节日活动、合作宣发素材）。

> 本手册建议随业务迭代持续更新，并与 README 及产品规划保持一致。建议所有运营人员完成培训后才能执行后台操作。


