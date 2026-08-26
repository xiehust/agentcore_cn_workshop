# 修复 Chat Portal 流结束后超时与 network error

## Goal

让 Chat Portal 在正文流式输出完成后及时、干净地结束响应，同时继续可靠执行
会话历史持久化和 Mem0 长期记忆写回；即使连接异常，前端也不得抹掉已经收到的正文。

## Requirements

- Runtime 的 SSE 响应结束不得等待历史落库或 Mem0 写回；流尾后台工作必须使用
  AgentCore Runtime 官方异步任务追踪机制，保证 microVM 在工作完成前保持
  `HealthyBusy`，不能使用无人追踪的 `asyncio.create_task` 或普通 fire-and-forget。
- 后台收尾仍按“先经 db Lambda 持久化完整 Strands messages，再做长期记忆提取”
  的顺序执行；不得改回 RDS Data API。同一会话并发收尾不得让
  `_PERSISTED_SEQ` 回退、产生 seq 空洞或破坏 toolUse/toolResult 配对。
- Mem0 仍是 best-effort 增强能力：超时、提取失败、线程池饱和时只记录日志并沿用
  现有熔断语义，不影响回答完成；本任务不以缩短 `MEM0_ADD_TIMEOUT` 掩盖问题。
- 前端只有在尚未收到任何助手正文时，才把传输异常渲染为整轮失败；已经收到正文时
  必须保留正文，并明确提示连接中断、内容可能不完整。
- 保持现有请求体、JWT/session header、SSE 文本增量格式、`/chat` REST API、
  Aurora schema 和 Lambda RPC 不变。
- 新增的后台收尾必须有可检索的开始、完成、失败日志，且日志不得包含 prompt、回复
  正文、token 或 API key。

## Acceptance Criteria

- [x] 新建 Runtime session 发起分析请求后，最后一个正文增量到 SSE 正常 EOF 的间隔
      小于 5 秒，前端输入框随即解锁，不出现 `network error`。
- [x] `MEM0_ADD_TIMEOUT` 保持线上 60 秒；本次服务恢复后未再次真实超时，但线上
      tracked task 在 SSE EOF 后继续运行 8.71/18.31 秒并完成，配合慢记忆单测证明
      两者互不阻塞；未故意破坏生产记忆端点制造超时。
- [x] 正常回答最终通过 db Lambda 写入 `chat_messages`；页面刷新或重新打开历史会话
      后能看到完整问答。
- [x] 同一 session 在上一轮 Mem0 尚未完成时立即发送下一轮，两轮消息最终按连续 seq
      落库，后一次落库不会被前一次游标覆盖。
- [x] 模拟“收到至少一个增量后 reader 抛错”，界面保留已收到的正文并追加非破坏性
      中断提示；模拟“首个增量前失败”，仍显示原有整轮失败提示。
- [x] `npm run build`、Python 语法检查和新增/相关测试通过。
- [x] README 与 Trellis 后端契约更新为后台收尾语义，不再声称 Mem0 会延迟 SSE 关闭。

## Notes

### Out of scope

- 更换或修复当前 Mem0 LLM/embedding 服务商。
- 调整 `MEM0_ADD_TIMEOUT`、记忆提取提示词或向量表结构。
- 改造为 AG-UI/WebSocket 或重新设计整套流协议。
