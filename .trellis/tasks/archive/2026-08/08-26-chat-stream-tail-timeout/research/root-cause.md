# 根因与 SDK 研究

## 线上证据（2026-08-26，us-west-2）

- Runtime：`analysis_chat_agent-097iRB7dFw`，检查时版本 12、状态 READY。
- 线上 `MEM0_ADD_TIMEOUT=60`。
- 同一请求最后正文为 `01:07:24.693Z`，`memorize 超时（60s）` 为
  `01:08:25.442Z`，形成约 60.7 秒无 SSE 字节的静默。
- Runtime v12 的历史访问使用 `DB_LAMBDA_NAME=agentcore-workshop-db`；按用户要求
  保持这条 VPC 内 db Lambda 链路，不切换到 RDS Data API。

## 代码证据

- `invoke` 在 `agent.stream_async` 后依次 await `_persist_new` 与 `memorize`，
  generator 直到二者结束才 EOF。
- `streamChat` 一直等待 `reader.read()` 返回 done。
- `ChatView.send()` 对任何非 AbortError 都覆盖最后一条 assistant message。

## AgentCore SDK

- 官方长任务模式用 `add_async_task` 注册、`complete_async_task` 完成；SDK 据此让
  `/ping` 返回 `HealthyBusy`。
- 项目最低版本 `bedrock-agentcore>=1.1.0` 已包含这两个 API。
- StreamingResponse 只有在 async generator 迭代结束后才发送 EOF。

## v15/v16 线上验收

- v13 因只上传源码、未 vendoring 依赖而启动失败；v14 虽验证了 SSE，但误用了
  main 的 Data API 实现，消息未落库，均未作为最终版本。
- 按用户指示改用 `feat/cn-portability` 的 db Lambda 代码，基于原 vendored artifact
  发布 v15；Runtime 环境仍是 `DB_LAMBDA_NAME=agentcore-workshop-db`。
- 新会话连续两轮：最后正文到 EOF 12ms / 7ms。
- `/chat/.../messages` 最终返回 4 条，seq 0/1/2/3。
- CloudWatch 中两轮 tracked task 分别在 8.71s / 18.31s 后完成，证明慢后台阶段不再
  占用 SSE；本次 Mem0 服务恢复后未再次出现 60s 超时，未故意破坏生产端点制造故障。
- v16 在调度日志前显式断行，避免 Strands 最后一段正文与 `[finalize]` 粘在同一
  CloudWatch 事件；复验最后正文到 EOF 9ms、两条消息落库、task 5.38s 后完成。
