# 实施计划

## 1. 后端后台收尾

- [x] 增加有界 persistence/memory executor、固定分片锁和统一收尾调度函数。
- [x] 按“db Lambda 历史持久化 -> 用户归属补查 -> Mem0 写回”执行。
- [x] `invoke()` 捕获快照并调度后立即返回，让 SSE 正常 EOF。
- [x] submit、持久化、记忆失败均 best-effort 降级并完成 tracked task。

## 2. 前端非破坏性错误处理

- [x] 记录本轮是否已经收到正文。
- [x] 首增量前失败沿用整轮错误；部分正文后失败保留正文并追加提示。
- [x] AbortError 和会话切换不向新会话写入提示。

## 3. 文档与契约

- [x] 更新 README 的流尾、侧栏刷新与故障排查说明。
- [x] 更新 chat sessions 契约，明确 db Lambda 后台落库与最终一致性。
- [x] 更新 Mem0 契约为 AgentCore tracked async task。

## 4. 静态与本地验证

- [x] Python `py_compile`。
- [x] 后台调度/并发回归测试（3/3）。
- [x] 前端 `npm run build`。
- [x] 前端“零增量失败”和“部分增量后失败”浏览器探针。
- [x] `git diff --check` 与范围检查。

## 5. 真实 Runtime 验收

- [x] 从不使用 Data API 的 feature artifact 部署 Runtime v16（v15 验证逻辑，
      v16 追加日志断行，避免诊断前缀与正文粘连）。
- [x] 新建 web session，两轮最后正文到 EOF 分别 12ms、7ms。
- [x] 保持 `MEM0_ADD_TIMEOUT=60`；两轮 tracked task 分别在 8.71s、18.31s 后完成，
      SSE 均已先 EOF。服务本次未再次超时，未通过破坏生产端点强制制造 60s 故障。
- [x] 快速连续两轮，`/chat/.../messages` 返回 seq 0/1/2/3，角色
      user/assistant/user/assistant。
- [x] Runtime 两个 async task 均完成，无未捕获异常；自有日志不打印 prompt/reply。
      v16 复验 `[finalize]` 已独立成行，tracked task 5.38s 后完成。

## 6. Review gate / rollback

- [x] 对照 PRD 复核；timeout 保持 60 秒，未以缩短 timeout 掩盖问题。
- [x] tracked async task 在线上正常保持并完成，无需回滚。
