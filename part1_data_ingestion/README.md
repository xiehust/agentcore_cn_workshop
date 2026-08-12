# Part 1：选品数据摄入 Pipeline

一个部署在 **AgentCore Runtime** 的 Strands Agent，按"选品 SOP" Skill 的规则，
用 **AgentCore Browser** 爬取 eBay 商品数据（默认站点；亚马逊需配代理，见常见问题）、
用 **Code Interpreter** 做经济测算，把判定结果经**写库 Lambda**（挂在 Aurora 同一
VPC 内）写入 **Aurora Serverless v2（PostgreSQL）**（Agent 自身无需挂 VPC）。

```
调用方（CLI / Part 2 门户的 runner）
   │ InvokeAgentRuntime
   ▼
AgentCore Runtime（公网，不挂 VPC）── Strands Agent + 选品 SOP Skill
   ├──▶ AgentCore Browser ──▶ eBay 商品页
   ├──▶ Code Interpreter    （沙箱内跑经济测算）
   └──▶ lambda.invoke ──▶ 写库 Lambda（Aurora 同 VPC）── pg8000:5432 ──▶ Aurora
```

## 工作原理

1. 调用方发送任务指令（如"从 eBay 搜索 desk organizer 选品，处理前 2 个商品"）。
2. Runtime 中的 Agent 按站点匹配 Skill（默认 `product-selection-ebay`；
   明确指定亚马逊时用 `product-selection`），加载选品 SOP 规则。
3. Agent 用 browser 工具打开商品页面，提取价格、销量、品牌卖家等数据
   （eBay 无月销字段，按 Skill 定义的口径从购买记录推估）。
4. 通过 R1 硬门槛、市场结构、黑名单逐层过滤；经济测算在 code_interpreter 沙箱中执行。
5. 每个商品的判定（PASS/排除/补采/采集）经 `save_verdict` 入库；
   "采集"的商品再经 `save_candidate` 写入完整候选档案。

**扩展方式**：新增业务场景（竞品监控、价格追踪…）只需在 `agent/skills/` 下新增一个
含 `SKILL.md` 的目录，Agent 代码零改动。

## 前置条件

```bash
# 1. AWS 凭证已配置，区域 us-west-2，且已开通 Bedrock OpenAI GPT-5.6 模型访问
aws sts get-caller-identity

# 2. 模型认证：设置 AWS_BEARER_TOKEN_BEDROCK（Bedrock API key 的标准环境变量，
#    控制台 Bedrock -> API keys 生成）即可；也支持 OPENAI_API_KEY（优先级更高，
#    注意须是 Bedrock API key，不是 OpenAI 官网的 sk- key）。
#    两者都不设置时，Agent 用 Runtime IAM 角色自动铸 token（需要
#    bedrock-mantle:CreateInference 权限，deploy.sh 已附加）
export AWS_BEARER_TOKEN_BEDROCK=<Bedrock API key>

# 3. 安装部署工具链（本 demo 的脚本用 Python 版 CLI 验证过。官方另有 npm 版
#    @aws/agentcore，但子命令不同——它没有 configure，若装在 PATH 前面会遮蔽
#    Python 版，导致 deploy.sh 报 unknown command 'configure'；见常见问题）
pip install bedrock-agentcore-starter-toolkit

# 4.（本地测试需要）安装 Agent 依赖
pip install -r agent/requirements.txt
```

## 一键部署

```bash
bash scripts/deploy.sh
```

脚本依次完成：

| 步骤 | 内容 | 说明 |
|------|------|------|
| 1 | CloudFormation 部署 Aurora Serverless v2 | 自建 VPC + 私有子网 + Aurora（Min 0 ACU 自动暂停）+ Secrets Manager 接口端点 + 写库 Lambda 角色 + IAM 策略。VPC 与端点由本栈拥有，Part 2 的分析 Lambda 共用 |
| 2 | 打包并部署写库 Lambda | 挂私有子网 + client 安全组，pg8000 直连集群 5432；zip 里含 `handler.py`、`setup_tables.sql` 与 pg8000 依赖；超时 300 秒（覆盖集群冷恢复） |
| 3 | 建表 | `aws lambda invoke` 触发写库 Lambda 的 `setup_tables`：业务表 `selection_verdicts`（判定记录）、`product_candidates`（候选档案），以及 Part 2 门户用的五张表（任务管理三表 + 会话历史两表，见下节）。DDL 逐条幂等执行 |
| 4 | 创建自定义 Browser 与 Browser Profile | browser 开启 Web Bot Auth（请求签名，减少验证码，预览功能）；profile 跨会话保留 cookies/localStorage。均按名字幂等，重复执行会复用 |
| 5 | `agentcore configure && agentcore launch` | direct_code_deploy 直接部署代码到 Runtime（无需 Docker），并注入 `WRITER_LAMBDA_NAME`、browser/profile ID 等环境变量 |
| 6 | 附加写库/浏览器访问策略到 Runtime 执行角色 | 写数仓只需对写库 Lambda 的 `lambda:InvokeFunction`（Runtime 不再接触数据库凭证）+ 自定义 browser 与 profile 的会话/保存权限 |

> 也可以分步手动执行，每个步骤的命令都在 `deploy.sh` 中有注释。

> **已有环境升级注意**：Secrets Manager 接口端点的归属从 Part 2 的 analytics 栈
> 挪到了本部分的 Aurora 栈（端点随 VPC 走）。同一 VPC 对同一服务只允许一个
> Private DNS 端点，所以必须**先跑 `part2_chat_portal/scripts/deploy.sh`**（更新
> Part 2 栈、删掉旧端点），**再跑本部分的 `scripts/deploy.sh`**（建新端点）。
> 顺序错了本脚本会在部署栈前预检报错并提示。两次部署之间的窗口期内 Part 2 的
> 分析 Lambda 冷启动取不到凭证（热实例已缓存凭证，不受影响），跑完即恢复。
> 全新环境天然是 Part 1 先行，无此问题。

## 调用测试

```bash
cd agent
agentcore invoke '{"prompt": "从 eBay 英国站搜索 desk organizer 选品，处理前 2 个非广告商品，按选品 SOP 完成判定并入库"}'
```

更多调用示例见 `scripts/invoke_example.sh`（类目浏览、指定 URL 判定、亚马逊站点）。

### payload 的 `run_id` 扩展（供 Part 2 门户调用）

Part 2 门户的「数据摄入任务管理」模块也是调这个 Runtime（由它的 runner Lambda
用 IAM 角色 SigV4 代签，Runtime 的认证方式不变），只是 payload 多带一个可选的
`run_id`：

```bash
agentcore invoke '{"prompt": "...", "run_id": "<uuid>"}'
```

带 `run_id` 时多做两件事：`save_verdict` / `save_candidate` 入库的记录会写上
`run_id`（门户按它过滤出「本次运行」的结果明细）；输出流里除文本增量外还混有
两种结构化事件（dict，Runtime 序列化为 JSON），供门户还原进度时间线与
Browser Live View 入口：

```json
{"event": "tool_use",        "name": "browser", "tool_use_id": "..."}
{"event": "browser_session", "browser_id": "...", "session_id": "..."}
```

不带 `run_id` 时行为与原先完全一致——`agentcore invoke`、`local_test.py`
的输出和入库结果都不变，`run_id` 列留空。

配套的表结构也在 `infra/setup_tables.sql` 里（随本部分的 deploy.sh 步骤 2 建）：
任务管理三张表 `ingestion_tasks`（任务定义）、`ingestion_runs`（每次运行）、
`ingestion_run_events`（进度与日志事件），以及两张业务表新增的可空 `run_id` 列
（可空 = 旧数据和命令行调用不受影响）。模块本身的部署与用法见
[Part 2 说明](../part2_chat_portal/README.md#数据摄入任务管理模块)。

## Part 2 门户的会话历史两表

`infra/setup_tables.sql` 里还有两张只服务 Part 2 门户的表（表 6、表 7）——建在
共享数仓，建表动作归本部分的 deploy.sh，Part 1 自身不读写它们：

- `chat_sessions`：门户「分析对话」的会话元数据（标题、归属用户、软删状态），
  由 Part 2 的 tasks-api Lambda 按 Keycloak 用户名注册与维护；
- `chat_messages`：会话消息，存完整的 Strands message JSON（含工具轮）加一份
  给前端回看用的 `display_text`，`(session_id, seq)` 唯一。Part 2 的 Runtime
  在每轮对话结束后增量落库，会话沙箱被回收后按 `seq` 回灌恢复上下文。

Part 2 的 deploy.sh 开头会预检这两张表，缺表直接失败并提示回来重跑本部分的
deploy.sh。

## 验证入库结果

```bash
# 查最近 10 条判定记录
# 集群/Secret ARN 从 CloudFormation 输出获取
CLUSTER_ARN=$(aws cloudformation describe-stacks --stack-name agentcore-workshop-aurora \
  --region us-west-2 --query "Stacks[0].Outputs[?OutputKey=='ClusterArn'].OutputValue" --output text)
SECRET_ARN=$(aws cloudformation describe-stacks --stack-name agentcore-workshop-aurora \
  --region us-west-2 --query "Stacks[0].Outputs[?OutputKey=='SecretArn'].OutputValue" --output text)
# rds-data 同步返回结果，无需轮询
aws rds-data execute-statement --resource-arn "$CLUSTER_ARN" --secret-arn "$SECRET_ARN" \
  --database product_hunting --region us-west-2 \
  --sql "SELECT verdict, reason, product_title, product_score FROM selection_verdicts ORDER BY created_at DESC LIMIT 10"

# 查候选档案
# ... --sql "SELECT product_name_cn, product_score, ai_summary FROM product_candidates ORDER BY created_at DESC" ...
```

也可以在 RDS 控制台的 Query Editor（Data API）里直接查询。

> 写入已经不走 Data API 了（见常见问题），但集群的 Data API 仍然开着：命令行
> 查数仓、RDS 控制台 Query Editor、以及 Part 2 的会话落库与 tasks-api 都还在用。

## 本地调试（不部署 Runtime）

```bash
cd agent
python ../scripts/local_test.py "从 eBay 英国站搜索 desk organizer，对第 1 个非广告商品做选品判定并入库"
```

Agent 本体跑在本机，但 Browser / Code Interpreter / 写库 Lambda 仍是云端服务
（本机凭证需要 `bedrock-agentcore:*`、对写库 Lambda 的 `lambda:InvokeFunction`
及 `bedrock:InvokeModel` 权限；数据库凭证本机拿不到也不需要。模型调用默认用
本机 AWS 凭证自动铸 token，也可 `export OPENAI_API_KEY=<Bedrock API key>`
显式指定）。

另一种本地方式是 Runtime 本地容器模拟：

```bash
cd agent
agentcore launch --local     # 本机起容器模拟 Runtime
agentcore invoke -l '{"prompt": "..."}'
```

## 文件说明

| 文件 | 作用 |
|------|------|
| `agent/main.py` | Runtime 入口：BedrockAgentCoreApp + Strands Agent 组装 |
| `agent/skills/product-selection-ebay/SKILL.md` | 选品 SOP Skill —— eBay 英国站（默认站点） |
| `agent/skills/product-selection/SKILL.md` | 选品 SOP Skill —— 亚马逊英国站（需代理） |
| `agent/tools/aurora_tools.py` | save_verdict / save_candidate 入库工具（薄壳，调写库 Lambda） |
| `agent/tools/browser_ext.py` | Browser 扩展：Web Bot Auth 自定义 browser 接入 + profile 恢复/保存 |
| `infra/aurora-serverless.yaml` | Aurora Serverless v2 + VPC + Secrets 端点 + IAM CloudFormation 模板 |
| `infra/writer-lambda/handler.py` | 写库 Lambda：VPC 内 pg8000 直连 Aurora，实现 save_verdict / save_candidate / setup_tables |
| `infra/setup_tables.sql` | 表结构 DDL：两张业务表 + Part 2 任务管理三表 + Part 2 会话历史两表 |
| `scripts/deploy.sh` | 一键部署 |
| `scripts/local_test.py` | 本地跑 Agent |
| `scripts/invoke_example.sh` | 调用示例 |

## 常见问题

- **为什么默认站点是 eBay 而不是亚马逊**：亚马逊（.com/.co.uk）在边缘层
  对云端数据中心 IP 段整体硬封锁（直接 503，连验证码都不给），换会话、
  重试均无解。eBay 等使用主流反爬厂商的站点可正常访问：AgentCore Browser
  是真实 Chromium，且已启用 Web Bot Auth（对每个 HTTP 请求做 IETF 标准
  签名，Cloudflare / Akamai / HUMAN / DataDome / F5 等厂商可验证放行，
  实测 eBay 的反爬挑战约 8 秒自动放行）和 Browser Profile（跨会话复用
  cookies）。要爬亚马逊需接入住宅代理（`start_browser_session` 原生支持
  `proxy_configuration`）或改用官方 SP-API。Skill 已约定"抓不到就标补采，
  不编造数据"。
- **为什么入库要绕一个写库 Lambda，不直接用 RDS Data API**：Data API 是
  HTTPS 接口，Agent 不挂 VPC 就能写库，本来最省事——但**中国区
  （cn-north-1 / cn-northwest-1）没有这个服务**。本 workshop 后续要整体移植到
  中国区，所以数据面提前换成两边都可用的形态：Agent 仍留在公网（不挂 VPC），
  私网访问全部由挂在 Aurora 同 VPC 的 Lambda 承担，Agent 只做一次
  `lambda.invoke`。这和 Part 2 的「Gateway → Lambda(VPC) → Aurora」是同一个
  设计哲学（AgentCore Gateway 也不支持 VPC egress），只是调用方不同。
  顺带收敛了权限：Runtime 执行角色从「rds-data 五个 Action + 读集群 Secret」
  变成只有对写库 Lambda 的 `lambda:InvokeFunction`，Agent 不再接触数据库凭证。
  移植到中国区时要改的已知差异：接口端点服务名前缀变
  `cn.com.amazonaws.<region>.secretsmanager`，ARN 分区变 `arn:aws-cn:`。
  Part 2 各处的 Data API 调用是后续独立任务。
- **建表为什么也走 Lambda**：`setup_tables.sql` 随部署包打进写库 Lambda，
  deploy.sh 用 `aws lambda invoke '{"op": "setup_tables"}'` 建表，本地无需
  数据库客户端也无需 Data API，部署链路同样零 Data API 依赖。DDL 全是
  `IF NOT EXISTS` 系语句，重跑安全。**新增 DDL 时不要引入 dollar-quoted 块
  （`$$ ... $$`）**——handler 按分号拆句执行，函数体会被切碎。
- **冷启动首次写库会不会超时**：集群 Min 0 ACU 空闲自动暂停，冷恢复约
  15 秒~1 分钟。超时链是对齐好的：handler 内连接重试上限 240 秒 <
  Lambda 超时 300 秒 < Agent 侧 boto3 `read_timeout` 310 秒，且 Agent 侧关掉
  了 boto3 重试（等待只在一处自旋，也避免超时后重复写入）。
- **Browser Profile 何时保存**：每次调用结束时（`invoke` 的 finally）自动把
  会话 cookies/localStorage 存回 profile，下次会话启动时恢复。不想复用状态
  就部署时不传 `BROWSER_PROFILE_ID`。
- **deploy.sh 第 5 步报 `unknown command 'configure'`**：本机装了 npm 版 AgentCore
  CLI（`@aws/agentcore`），在 `PATH` 里遮蔽了脚本要用的 Python 版 `agentcore`。
  把 Python 版所在目录前置即可，例如
  `PATH="<仓库路径>/.venv/bin:$PATH" bash scripts/deploy.sh`。
- **调用超时**：完整处理一个商品（详情页 + 竞品调研 + 测算 + 入库）约 2~5 分钟，
  建议每次调用处理 1~3 个商品；批量场景用 EventBridge 定时分批触发。
- **想换模型**：部署前 `export MODEL_ID=<模型ID>` 再跑 deploy.sh（或部署时加
  `--env MODEL_ID=<模型ID>`），仅限 bedrock-mantle（OpenAI 兼容）端点上的模型；
  换端点区域用 `OPENAI_BASE_URL` 覆盖（默认 us-east-1）。换用 Converse API
  模型（如 Claude）需把 `agent/main.py` 里的 `OpenAIResponsesModel` 换回
  `BedrockModel`。
