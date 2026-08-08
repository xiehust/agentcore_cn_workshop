# Part 1：选品数据摄入 Pipeline

一个部署在 **AgentCore Runtime** 的 Strands Agent，按"选品 SOP" Skill 的规则，
用 **AgentCore Browser** 爬取 eBay 商品数据（默认站点；亚马逊需配代理，见常见问题）、
用 **Code Interpreter** 做经济测算，把判定结果经 RDS Data API 写入
**Aurora Serverless v2（PostgreSQL）**（Agent 无需挂 VPC）。

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
| 1 | CloudFormation 部署 Aurora Serverless v2 | 自建 VPC + 私有子网 + Aurora（Data API，Min 0 ACU 自动暂停）+ IAM 策略。VPC 留给 Part 2 的 Lambda 直连验证 |
| 2 | 建表 | 业务表 `selection_verdicts`（判定记录）、`product_candidates`（候选档案），以及 Part 2 任务管理模块用的三张表（见下节）。DDL 逐条幂等执行 |
| 3 | 创建自定义 Browser 与 Browser Profile | browser 开启 Web Bot Auth（请求签名，减少验证码，预览功能）；profile 跨会话保留 cookies/localStorage。均按名字幂等，重复执行会复用 |
| 4 | `agentcore configure && agentcore launch` | direct_code_deploy 直接部署代码到 Runtime（无需 Docker），并注入 browser/profile ID 等环境变量 |
| 5 | 附加数仓/浏览器访问策略到 Runtime 执行角色 | rds-data 入库权限 + Secret 读取 + 自定义 browser 与 profile 的会话/保存权限 |

> 也可以分步手动执行，每个步骤的命令都在 `deploy.sh` 中有注释。

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

## 本地调试（不部署 Runtime）

```bash
cd agent
python ../scripts/local_test.py "从 eBay 英国站搜索 desk organizer，对第 1 个非广告商品做选品判定并入库"
```

Agent 本体跑在本机，但 Browser / Code Interpreter / Aurora 仍是云端服务
（本机凭证需要 `bedrock-agentcore:*`、`rds-data:*`、数仓 Secret 读取及
`bedrock:InvokeModel` 权限；模型调用默认用本机 AWS 凭证自动铸 token，
也可 `export OPENAI_API_KEY=<Bedrock API key>` 显式指定）。

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
| `agent/tools/aurora_tools.py` | save_verdict / save_candidate 入库工具（RDS Data API） |
| `agent/tools/browser_ext.py` | Browser 扩展：Web Bot Auth 自定义 browser 接入 + profile 恢复/保存 |
| `infra/aurora-serverless.yaml` | Aurora Serverless v2 + VPC CloudFormation 模板 |
| `infra/setup_tables.sql` | 表结构 DDL：两张业务表 + Part 2 任务管理的三张表 |
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
- **Browser Profile 何时保存**：每次调用结束时（`invoke` 的 finally）自动把
  会话 cookies/localStorage 存回 profile，下次会话启动时恢复。不想复用状态
  就部署时不传 `BROWSER_PROFILE_ID`。
- **deploy.sh 第 4 步报 `unknown command 'configure'`**：本机装了 npm 版 AgentCore
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
