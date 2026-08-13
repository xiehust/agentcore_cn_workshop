# Journal - river (Part 1)

> AI development session journal
> Started: 2026-08-07

---



## Session 1: 门户新增数据摄入任务管理模块（并行/调度/进度日志/Live View/结果明细）

**Date**: 2026-08-08
**Task**: 门户新增数据摄入任务管理模块（并行/调度/进度日志/Live View/结果明细）
**Branch**: `main`

### Summary

在 Part 2 门户落地任务管理模块：Aurora 新增任务三表与业务表 run_id 列；Part 1 agent 向后兼容支持 run_id 与结构化进度事件；新增 Tasks HTTP API（Keycloak JWT）+ tasks-api/runner 两个 Lambda + EventBridge Scheduler 定期调度；前端新增任务视图与 DCV Live View（presigned URL 300s 刷新重连）。已真实部署并通过 E2E（并行 run、调度触发/暂停恢复、DCV 握手截图、统计与明细、401、幂等重跑）。质检修复 3 个缺陷（暂停任务手动运行卡排队、Lambda 超时悬挂、500 泄漏）。规范沉淀至 spec/backend/ingestion-tasks-module.md 与 spec/frontend/live-view-and-polling.md。注意：npm 版 agentcore CLI 会遮蔽 .venv 的 Python 版，deploy 需前置 PATH。

### Git Commits

| Hash | Message |
|------|---------|
| `b60c2b6` | (see git log) |
| `311fcef` | (see git log) |
| `8c86623` | (see git log) |
| `708c3a2` | (see git log) |

### Status

[OK] **Completed**


## Session 2: 门户分析会话与历史功能落地

**Date**: 2026-08-11
**Task**: 门户分析会话与历史功能落地
**Branch**: `main`

### Summary

会话持久化全链路：Aurora chat 两表、tasks-api 扩展 /chat 路由组（JWT 用户归属+软删+游标分页）、Runtime 历史回灌与增量落库（best-effort 不阻断 SSE）、门户侧栏历史会话（恢复/重命名/删除）。E2E 实测强停 microVM 后上下文经回灌延续；文档四处同步并新增 chat-sessions-module 后端契约 spec。坑：JWT 认证的 Runtime 停会话须带 bearer 直调数据面，SigV4 报 Authorization method mismatch。

### Git Commits

| Hash | Message |
|------|---------|
| `9f019fa` | (see git log) |
| `990a77c` | (see git log) |
| `061c6e1` | (see git log) |
| `9e03d6d` | (see git log) |

### Status

[OK] **Completed**


## Session 3: Part 1 写库路径改造：Data API 换 VPC 写库 Lambda

**Date**: 2026-08-12
**Task**: Part 1 写库路径改造：Data API 换 VPC 写库 Lambda
**Branch**: `main`

### Summary

为中国区移植做准备（cn 区无 RDS Data API），Part 1 的 Agent 写库与建表全部改经 VPC 内写库 Lambda（pg8000 直连），模型与 Skill 零感知。Secrets Manager 端点归属从 Part 2 栈挪到 Part 1 栈（VPC 拥有者），deploy.sh 带归属预检；AgentDataAccessPolicy 因被 Part 2 共享而原样保留，新增独立 invoke 策略换挂。线上验收七项全过：冷库首写 17s、端到端 eBay 实测入库、Part 2 回归通过、两侧 deploy 重跑幂等。注意事项：跑 deploy 脚本须把仓库根 .venv/bin 前置 PATH（npm 同名 agentcore 遮蔽）。

### Git Commits

| Hash | Message |
|------|---------|
| `76fe25c` | (see git log) |
| `14e1321` | (see git log) |
| `17977b3` | (see git log) |

### Status

[OK] **Completed**


## Session 4: Add eBay US ingestion skill

**Date**: 2026-08-13
**Task**: Add eBay US ingestion skill
**Branch**: `main`

### Summary

Added a dedicated eBay US Part 1 selection skill using ebay.com, USD, site=US, fixed demo exchange rate 7, US compliance guidance, and documented invocation examples. Static validation passed; live AgentCore Browser-to-Aurora E2E was not run because the local environment lacks strands and deployed runtime verification was outside this task.

### Git Commits

| Hash | Message |
|------|---------|
| `bb8295a` | (see git log) |

### Status

[OK] **Completed**


## Session 5: Add extensible country selection to ingestion tasks

**Date**: 2026-08-13
**Task**: Add extensible country selection to ingestion tasks
**Branch**: `main`

### Summary

Added a UK-defaulted editable country/marketplace combobox with UK/US suggestions and custom values; persisted country on ingestion_tasks with an idempotent compatibility migration; propagated country through task API responses; added generic Skill-routing instructions in runner without country mappings; added 9 backend tests, docs, and spec updates. Unit tests, Python checks, frontend build, static extensibility assertions, and desktop/mobile browser QA passed. Aurora migration and live AgentCore E2E were not run.

### Git Commits

| Hash | Message |
|------|---------|
| `ec5184e` | (see git log) |

### Status

[OK] **Completed**
