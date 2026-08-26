# 数据库访问约定

> 共享数仓（Aurora Serverless v2 / PostgreSQL）的访问路径、超时与建表约定。
> 表结构的唯一来源是 `part1_data_ingestion/infra/setup_tables.sql`。

---

## 两条访问路径怎么选

| 路径 | 用在哪 | 说明 |
| --- | --- | --- |
| VPC 内 Lambda + pg8000 直连 5432 | **新代码默认选这条**。Part 1 写库 Lambda（`infra/writer-lambda/`）、Part 2 analytics Lambda | 挂 Aurora 栈的私有子网 + `LambdaClientSecurityGroup`；凭证从 Secrets Manager 取（VPC 接口端点，子网无 IGW/NAT） |
| RDS Data API（HTTPS） | 存量：Part 2 Runtime 的会话落库、tasks-api / runner Lambda、命令行查数仓 | **中国区没有 Data API**，本 workshop 后续要整体移植，这些点是待迁移的技术债 |

托管服务（Runtime / Gateway）都不挂 VPC：计算留在公网，私网访问全部由 VPC 内的
Lambda 承担，公网侧只做一次 `lambda.invoke`（Part 1）或 Gateway MCP 工具调用
（Part 2）。

派生的 IAM 约定：调用方角色只要 `lambda:InvokeFunction`，不再持有数据库凭证。
Part 1 Runtime 挂 `AgentWriterInvokePolicyArn`，Data API 那份托管策略
（`AgentDataAccessPolicyArn`）如今只服务 Part 2，Part 2 迁完才能删。

---

## Secrets Manager 接口端点的归属

**端点归 VPC 拥有者的栈**（= Part 1 的 `agentcore-workshop-aurora`），使用方只挂
`LambdaClientSecurityGroup`。原因：同一 VPC 对同一服务只允许一个 Private DNS
端点，若各使用方各建一个，第二个栈必然 CREATE_FAILED。

挪动端点归属属于单向变化，已有环境必须**先删后建**（先跑放弃端点的那个栈的
deploy，再跑接管端点的），并在接管方的脚本里加预检（查 VPC 内是否存在不带本栈
`aws:cloudformation:stack-name` 标签的同服务端点），把顺序错误从 CFN 回滚变成
开跑前的明确提示。

---

## 超时链对齐

Aurora `MinCapacity=0.5` 保持 warm standby，不因空闲自动暂停。连接重试仍只在
一处自旋，外层逐层放宽，以覆盖维护或瞬时故障并让超时报错指向真正原因：

| 层 | 值（Part 1 写库路径） |
| --- | --- |
| handler 内连接重试 deadline | 240s |
| Lambda timeout | 300s |
| 调用方 boto3 `read_timeout` | 310s，且 `retries={"max_attempts": 0}` |

调用方禁用 boto3 重试：等待已下沉到 Lambda，重试只会重复写入。
走 Data API 的存量代码用另一套：`DatabaseResumingException` 退避重试，deadline ≈50s。

---

## 参数化与类型

- pg8000 路径用 `pg8000.native` 的 `:name` 命名参数，值传原生 Python 类型
  （`str/int/float/None`），不要拼字符串。
- 只在 SQL 语义需要时才写 `CAST(...)`：`ROUND(numeric, int)`（float8 没有这个
  重载）、整数除法、写 JSONB 列。其余靠 Postgres 的赋值转换。
- 入参归一化（列长度截断、枚举校验、JSON 合法性降级包装）统一放在 Lambda
  handler 里，工具/调用层只留薄壳——模型给的值类型和长度都不可信。

---

## 建表 / DDL

- 建表由 Part 1 的 deploy.sh 触发（`aws lambda invoke '{"op":"setup_tables"}'`），
  `setup_tables.sql` 随部署包打进写库 Lambda。Part 2 只预检表是否存在，不建表。
- DDL 全部写成幂等语句（`CREATE TABLE / CREATE INDEX ... IF NOT EXISTS`、
  `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`），deploy 可反复重跑。
- **不得引入 dollar-quoted 块（`$$ ... $$`）**：执行前按分号拆句，函数体会被切碎。
- 给已有表加列一律可空，保证旧数据与不传该字段的调用方不受影响。
