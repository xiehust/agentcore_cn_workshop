# 技术设计：Part 1 写库路径改造

## 架构变化

```
现状：
  Runtime(公网) ── boto3 rds-data(HTTPS) ──▶ Aurora        # Agent 写库
  deploy.sh ──── aws rds-data CLI ─────────▶ Aurora        # 建表

改后：
  Runtime(公网) ── boto3 lambda.invoke ──▶ 写库 Lambda(VPC) ── pg8000:5432 ──▶ Aurora
  deploy.sh ────── aws lambda invoke ────▶ 同一 Lambda（op=setup_tables）
```

与 Part 2 的 analytics Lambda 完全同构（VPC 私有子网 + LambdaClientSecurityGroup
+ Secrets Manager 接口端点取凭证），仅调用方从 Gateway 换成 Agent 进程 / CLI。

## 新组件

### 写库 Lambda：`part1_data_ingestion/infra/writer-lambda/handler.py`

- 依赖 pg8000（deploy.sh 打包时 `pip install pg8000 -t`，同 analytics 模式）；
  `setup_tables.sql` 一并拷进 zip。
- 连接管理照抄 analytics handler：模块级连接复用、Secrets Manager 取凭证、
  SSL 不校验（demo）、冷恢复循环重连——deadline 从 50s 提到 **240s**
  （原 `_execute` 在 Agent 进程里等最长 3 分钟，这个职责移进 Lambda）。
- 请求协议（Agent 与 deploy.sh 共用）：

  ```json
  {"op": "save_verdict" | "save_candidate" | "setup_tables",
   "args": {…工具入参原样…},
   "run_id": "uuid 或 null"}
  ```

  响应：`{"status": "ok", "detail": …}` / `{"status": "error", "error": "…"}`。
  handler 内部完成 verdict 枚举校验、variants JSON 校验降级（从
  aurora_tools.py 平移，工具层只留薄壳）。
- SQL 参数化用 pg8000.native 的 `:name` 命名参数，与现有 SQL 风格一致；
  但值直接传原生类型（float/int/None/str），删掉 Data API 时代的
  `stringValue` + `NULLIF(...)::cast` 迂回写法，SQL 相应简化。
- `setup_tables`：按现 deploy.sh 的同款拆分逻辑（去 `--` 注释、按分号拆句）
  逐条执行；SQL 全部是 `CREATE TABLE IF NOT EXISTS` 系幂等语句
  （现文件无 dollar-quoted 块，分号拆分安全——新增语句时须保持此约束）。

### 基础设施（改 `aurora-serverless.yaml`，不新增栈）

新增资源：
- `SecretsEndpointSecurityGroup` + `SecretsManagerVpcEndpoint`
  （从 part2-analytics.yaml **平移**，ingress 允许 LambdaClientSecurityGroup 443）
- `WriterLambdaRole`：AWSLambdaVPCAccessExecutionRole + 读集群 Secret
  （同 AnalyticsLambdaRole 模式）
- 参数 `WriterFunctionName`（default `agentcore-workshop-writer`）；函数
  本体由 deploy.sh 创建（栈只管角色，与 Part 2 惯例一致）

修改资源（实施中修订）：
- ~~`AgentDataAccessPolicy` 内容替换为 lambda:InvokeFunction~~ **不可行**：
  该托管策略被 Part 2 共享（part2 deploy.sh 附加给 Part 2 Runtime 角色；
  part2-tasks.yaml 的 tasks-api / runner 两个 Lambda 角色把它写进
  ManagedPolicyArns），内容更新即时生效会抽掉 Part 2 全部 rds-data 权限，
  违反需求 4。
- 改为：`AgentDataAccessPolicy` 名字与内容原样保留（注释标明现在只服务
  Part 2，Part 2 迁完才能删）；**新增** `AgentWriterInvokePolicy`
  （`agentcore-workshop-agent-writer-invoke`，仅 lambda:InvokeFunction）。
  Part 1 deploy.sh 给 Runtime 角色挂新策略并主动 detach 旧的 Data API
  策略（NoSuchEntity 忽略），老环境重跑即收敛，需求 7 照样达成。

新增输出：`WriterLambdaRoleArn`、`WriterFunctionName`、
`AgentWriterInvokePolicyArn`。

### Part 2 栈瘦身（`part2-analytics.yaml`）

删除 `SecretsEndpointSecurityGroup` 与 `SecretsManagerVpcEndpoint` 两个资源
（端点归 VPC 拥有者 Part 1）。analytics Lambda 继续挂
LambdaClientSecurityGroup，走 Part 1 的新端点，代码零改动。

## 端点归属迁移（一次性）

同一 VPC 对同一服务只允许一个 Private DNS 端点，因此**已有环境必须先删后建**：

1. 先跑 `part2_chat_portal/scripts/deploy.sh`（栈更新删除旧端点）
2. 再跑 `part1_data_ingestion/scripts/deploy.sh`（栈更新创建新端点）

全新环境天然 Part 1 先行，无此问题。Part 1 deploy.sh 加**预检**：栈部署前
查询 VPC 内是否存在不属于本栈的 secretsmanager 接口端点，命中则报错并提示
先升级 Part 2——把迁移顺序错误从「CFN 栈 CREATE_FAILED 回滚」变成开跑前的
明确提示。

窗口期说明：两次部署之间 analytics Lambda 取凭证会失败（端点不存在），
已缓存凭证的热实例不受影响；workshop 环境可接受，文档标注即可。

## Agent 侧改动（`agent/tools/aurora_tools.py`)

- 工具签名、docstring、返回文案、contextvar 全部不动。
- `_execute` 替换为 `_invoke_writer(op, args)`：boto3 lambda client，
  `Config(connect_timeout=10, read_timeout=310, retries={"max_attempts": 0})`
  ——重试与冷恢复等待已下沉到 Lambda 内，Agent 侧不再自旋。
- 环境变量：`WRITER_LAMBDA_NAME`（default `agentcore-workshop-writer`）替代
  `AURORA_CLUSTER_ARN` / `AURORA_SECRET_ARN` / `AURORA_DATABASE`。
- `run_id` 由 `_invoke_writer` 从 contextvar 读出放进 payload 顶层。

## 超时链

| 层 | 值 | 依据 |
| --- | --- | --- |
| handler 连接重试 deadline | 240s | 冷恢复 15s~1min，留裕量 |
| Lambda timeout | 300s | 覆盖连接重试 + 语句执行 |
| Agent boto3 read_timeout | 310s | 略大于 Lambda timeout，错误来源清晰 |

## deploy.sh 步骤重排（Part 1）

```
[1/6] CFN 栈（含端点预检）
[2/6] 打包并创建/更新写库 Lambda（pg8000 + handler + setup_tables.sql；
      VPC 配置：私有子网 + LambdaClientSecurityGroup；timeout 300）
[3/6] aws lambda invoke op=setup_tables 建表（替代 rds-data 循环）
[4/6] Browser / Profile（不变）
[5/6] agentcore launch：env 改传 WRITER_LAMBDA_NAME，去掉 AURORA_*
[6/6] 附加策略到 Runtime 角色（policy 内容已变，流程不变）
```

注意 [2] 必须在 [3] 之前（建表要用 Lambda），且 Lambda 创建后首次 invoke
可能撞 ENI 就绪延迟，invoke 带一轮重试。

## 回滚

集群 Data API 保留（需求 4），回滚 = 还原 `aurora_tools.py` + deploy.sh +
栈模板重新部署，数据无迁移。端点归属是唯一单向变化：回滚 Part 1 栈会删端点，
需同时还原 part2-analytics.yaml 并重跑 Part 2 deploy。

## 中国区移植时的已知差异（不在本次范围）

- 接口端点服务名前缀：`cn.com.amazonaws.<region>.secretsmanager`
- ARN 分区 `arn:aws-cn:`（模板内 `!Sub` 已用 `${AWS::Partition}` 的要检查，
  现模板硬编码 `arn:aws:` 的位置移植时统一替换）
- Part 2 的 Data API 使用点届时需按同款模式迁移（后续任务）
