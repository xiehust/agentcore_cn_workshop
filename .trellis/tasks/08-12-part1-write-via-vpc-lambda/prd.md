# Part 1 写库路径改造：Data API 换 VPC 写库 Lambda（中国区可移植）

## 背景

中国区（cn-north-1 / cn-northwest-1）没有 RDS Data API，且 AgentCore Gateway
不支持 VPC egress。AgentCore 中国区未来上线后本 workshop 要整体移植过去，
当前在海外区（us-west-2）用等效架构模拟。Part 1 的 Agent 写库与 deploy.sh
建表目前都走 Data API，是移植时的两个断点。

本任务把 Part 1 的数据面写路径改为中国区可用的形态：
Agent（公网 Runtime）→ boto3 lambda.invoke → 写库 Lambda（挂 Aurora VPC）
→ pg8000 直连集群。与 Part 2 已验证的「Gateway → Lambda(VPC) → Aurora」
共享同一设计哲学：计算留在公网，私网访问全部由 VPC 内 Lambda 承担。

## 需求

1. **Agent 行为不变**：`save_verdict` / `save_candidate` 的工具签名、参数
   语义、返回文案保持原样；Skill（SKILL.md）无需任何改动；模型不感知底层
   写路径的变化。
2. **run_id 注入机制保持**：仍用进程内 contextvar，由工具函数写入 Lambda
   payload；不进工具 schema、不暴露给模型。
3. **建表一并搬进 Lambda**：写库 Lambda 提供 `setup_tables` 操作，
   `setup_tables.sql` 打包进部署包；deploy.sh 建表步骤改为 invoke 该操作，
   幂等可重跑。Part 1 部署链路从此零 Data API 依赖。
4. **集群 Data API 保留**：`EnableHttpEndpoint` 不动。Part 2（会话落库、
   tasks-api）与 CLI 查数仓照常工作，其迁移是后续独立任务。
5. **Secrets Manager VPC 端点归属挪到 Part 1**：端点随 VPC 走（Part 1 的
   Aurora 栈），Part 2 analytics 栈删除该端点；已有环境的迁移顺序必须有
   文档说明与脚本预检（同一 VPC 同一服务的 Private DNS 端点只能有一个）。
6. **超时链覆盖 Aurora 冷恢复**：Min 0 ACU 暂停后恢复需 15 秒~1 分钟；
   Lambda 超时、handler 连接重试、Agent 侧 boto3 read_timeout 三层对齐，
   冷启动首次写库不得失败。
7. **Runtime 执行角色权限收敛**：从 rds-data 五个 Action + 读集群 Secret
   收敛为对写库 Lambda 的 `lambda:InvokeFunction`；Part 1 的 Runtime 不再
   接触数据库凭证。
8. **deploy.sh 幂等**，全新环境与已有环境重跑均成功；`local_test.py`
   本地跑 Agent 的路径继续可用。
9. **文档同步**：实验手册、两个 README、CLAUDE.md 的架构描述与命令段。

## 验收标准

- [x] 全新环境 `bash scripts/deploy.sh` 一次成功；建表经写库 Lambda 完成
      （已有环境实测通过；全新环境路径 = 同脚本预检跳过分支，逻辑已走查）
- [x] 已有环境按文档顺序重跑两个 deploy.sh 成功（端点归属迁移不报冲突；
      实测 Part 2 栈删端点 → Part 1 栈建新端点 → analytics Lambda 经新端点
      取凭证正常）
- [x] `agentcore invoke` 单商品端到端跑通，`selection_verdicts` 出现新行
      （eBay 实测 £3.26 商品判定 PASS，rds-data CLI 查到该行）
- [x] 冷库状态（集群暂停）下首次写库成功，不因超时链断裂而失败
      （实测集群 0 ACU 暂停后首写 17 秒成功）
- [x] Part 1 Agent 代码与 Runtime 执行角色不再引用
      `AURORA_CLUSTER_ARN` / `AURORA_SECRET_ARN` / rds-data 权限
      （角色现仅挂 agentcore-workshop-agent-writer-invoke；env 仅
      WRITER_LAMBDA_NAME）
- [x] Part 2 回归：`./scripts/chat_test.sh` 冒烟通过（端点挪走后 analytics
      Lambda 与会话落库不受影响）
- [x] 文档四处同步完成
- [x] 附加验证：setup_tables 幂等重放（17 条 DDL）；Part 1 deploy 重跑幂等
      （Lambda update 分支 + Browser/Profile 复用 + 策略重挂）；写库 Lambda
      直接写入测试行并清理

## 非目标

- Part 2 各 Data API 使用点（会话落库、tasks-api Lambda）的迁移
- Runtime 挂 VPC 直连方案（会牺牲公网出口的简单性，另行评估）
- 走 Gateway MCP 工具的方案（已评估否决：run_id 跨进程丢失、认证链过重）
- 真实中国区部署（AgentCore 尚未上线；端点服务名等差异移植时处理）
