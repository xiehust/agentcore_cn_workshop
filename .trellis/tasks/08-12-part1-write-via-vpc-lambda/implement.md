# 执行计划：Part 1 写库路径改造

依赖顺序：基础设施模板 → Lambda handler → Agent 工具 → 部署脚本 → 验证 → 文档。

## 1. 基础设施模板

- [ ] `part1_data_ingestion/infra/aurora-serverless.yaml`
  - 新增 `SecretsEndpointSecurityGroup`、`SecretsManagerVpcEndpoint`
    （自 part2-analytics.yaml 平移）
  - 新增 `WriterLambdaRole`（VPCAccessExecution + 读集群 Secret）
  - 新增参数 `WriterFunctionName`（default `agentcore-workshop-writer`）
  - `AgentDataAccessPolicy` 内容替换为 `lambda:InvokeFunction`
    （**ManagedPolicyName 保持不变**，防 replacement 失败）
  - 新增输出 `WriterLambdaRoleArn`、`WriterFunctionName`
- [ ] `part2_chat_portal/infra/part2-analytics.yaml`
  - 删除 `SecretsEndpointSecurityGroup`、`SecretsManagerVpcEndpoint`
  - 模板头部说明改为「端点由 Part 1 Aurora 栈提供」

## 2. 写库 Lambda

- [ ] 新建 `part1_data_ingestion/infra/writer-lambda/handler.py`
  - 连接/凭证/冷恢复重连照抄 analytics handler，deadline 240s
  - `op=save_verdict`：verdict 枚举校验 + 参数化 INSERT（原生类型，去 NULLIF cast）
  - `op=save_candidate`：variants JSON 校验降级 + 参数化 INSERT
    （estimated_price / estimated_monthly_sales 的派生计算保留在 SQL）
  - `op=setup_tables`：读打包的 setup_tables.sql，去注释按分号拆句逐条执行
  - 统一响应 `{"status": "ok"|"error", ...}`

## 3. Agent 工具层

- [ ] `part1_data_ingestion/agent/tools/aurora_tools.py`
  - `_execute`/`_p` 替换为 `_invoke_writer(op, args)`
    （boto3 lambda client，read_timeout=310，不重试）
  - 环境变量换为 `WRITER_LAMBDA_NAME`；工具签名/返回文案/contextvar 不动
  - 模块 docstring 更新（设计说明段）
- [ ] 检查 `scripts/local_test.py` 是否引用 AURORA_* env，改为可选的
  `WRITER_LAMBDA_NAME`

## 4. 部署脚本

- [ ] `part1_data_ingestion/scripts/deploy.sh`
  - 栈部署前加端点预检（VPC 内已存在非本栈的 secretsmanager 端点 →
    报错提示先跑 Part 2 deploy）
  - 新增步骤：打包（pip install pg8000 -t + handler + setup_tables.sql）
    并 create/update 写库 Lambda（VPC 子网 + LambdaClientSecurityGroup，
    timeout 300，env DB_HOST/DB_NAME/DB_SECRET_ARN）
  - 建表步骤改为 `aws lambda invoke` op=setup_tables（带 ENI 就绪重试），
    删除 rds-data 循环
  - `agentcore launch` env：去 AURORA_*，加 WRITER_LAMBDA_NAME
  - 步骤编号与首尾提示语同步更新
- [ ] `scripts/invoke_example.sh` 若引用 AURORA_* 一并检查

## 5. 验证（全部通过才算完成）

- [ ] 已有环境迁移顺序：先 `part2 deploy.sh` 再 `part1 deploy.sh`，两次均成功
- [ ] `aws lambda invoke` op=setup_tables 幂等（重跑不报错）
- [ ] 冷库验证：集群暂停状态下直接 invoke op=save_verdict 写测试行成功
      （之后删除测试行）
- [ ] 端到端：`agentcore invoke` 单商品，rds-data CLI 查到新 verdict 行
- [ ] `python scripts/local_test.py` 路径可用
- [ ] Part 2 回归：`./scripts/chat_test.sh '看下最近30天的选品漏斗'` 通过
      （验证 analytics Lambda 经新端点取凭证正常）
- [ ] deploy.sh 重跑一遍全程幂等

## 6. 文档同步

- [ ] `part1_data_ingestion/README.md`：架构图、目录树、FAQ（Data API 相关
      条目改写：为什么 Agent 写库走 Lambda、中国区移植说明）
- [ ] `part2_chat_portal/README.md`：端点归属变化一句话说明
- [ ] `实验手册.md`：Part 1 架构段与部署说明、迁移顺序注意事项
- [ ] `CLAUDE.md`：架构段（Part 1 数据流）、常用命令段如受影响则更新

## 7. 收尾

- [ ] spec 更新（若沉淀出可复用约定：端点归属原则、超时链对齐模式）
- [ ] 中文 commit（按仓库惯例分层提交：infra / agent+脚本 / 文档）

## 回滚点

- 步骤 1 后未动代码：还原两个模板重新 deploy 即可
- 步骤 5 验证失败：还原 aurora_tools.py + deploy.sh（集群 Data API 未关，
  旧路径立即可用），栈模板按「回滚」节顺序还原
