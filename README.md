# Amazon Bedrock AgentCore 中文 Workshop

本项目是一个端到端的 demo，帮助你快速熟悉 **Amazon Bedrock AgentCore** 产品家族的核心能力：

| 组件 | 作用 | 本 demo 中的用途 |
|------|------|------------------|
| **Runtime** | 托管 Agent 的无服务器运行时（会话隔离、自动扩缩容） | 承载 Part 1 的数据摄入 Agent 和 Part 2 的分析 Agent |
| **Browser（内置工具）** | 云端托管的无头浏览器（CDP 协议，会话隔离） | 爬取 eBay 商品页面数据 |
| **Code Interpreter（内置工具）** | 安全沙箱内执行代码 | 执行选品经济测算（产品点公式）与数据分析 |
| **Gateway** | 把现有 API / Lambda 转换成 MCP 工具 | Part 2 中把数据查询 Lambda（Aurora 同 VPC 直连）暴露给分析 Agent |
| **Identity** | Agent 的身份与凭证管理（OAuth / API Key） | Part 2 中门户用户认证（Keycloak JWT 入站）与 Agent 访问 Gateway 的 M2M 授权 |

## 整体架构

```
┌─────────────────────── Part 1: 数据摄入 Pipeline（本期） ───────────────────────┐
│                                                                                │
│   调用方（CLI / 定时任务）                                                       │
│        │  InvokeAgentRuntime                                                   │
│        ▼                                                                       │
│   ┌──────────────────────────────┐                                            │
│   │  AgentCore Runtime           │      ┌─────────────────────┐               │
│   │  ┌────────────────────────┐  │─────▶│ AgentCore Browser   │──▶ eBay 网站   │
│   │  │ Strands Agent          │  │      └─────────────────────┘               │
│   │  │  + AgentSkills 插件     │  │      ┌─────────────────────┐               │
│   │  │    (选品 SOP Skill)     │  │─────▶│ Code Interpreter    │ 产品点测算     │
│   │  │  + Aurora 入库工具      │  │      └─────────────────────┘               │
│   │  └────────────────────────┘  │                                            │
│   └──────────────┬───────────────┘                                            │
│                  │  lambda.invoke（Runtime 不挂 VPC）                           │
│                  ▼                                                            │
│   ┌──────────────────────────────┐                                            │
│   │  写库 Lambda（Aurora 同 VPC）  │  pg8000 直连 5432；不用 RDS Data API        │
│   └──────────────┬───────────────┘ （中国区没有该服务，为移植提前换形态）        │
│                  ▼                                                            │
│   ┌──────────────────────────────┐                                            │
│   │  Aurora Serverless v2 (PG)   │  候选档案表 + 判定记录表（自建 VPC 内）       │
│   └──────────────────────────────┘                                            │
└────────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────── Part 2: Web Chat 分析门户 ──────────────────────────────┐
│   浏览器（React SPA）─① OIDC 登录（PKCE）─▶ Keycloak（自部署，ECS Fargate）     │
│        │  ② 用户 token（SSE 流式调用）                                          │
│        ▼                                                                       │
│   Runtime（分析 Agent，JWT 入站认证）                                           │
│        ├─▶ Identity ─③ M2M token─▶ Gateway（MCP 工具，JWT 认证）               │
│        │                              └─▶ Lambda(VPC) ─▶ Aurora(同 VPC 直连)   │
│        │                                  （Gateway 侧零 VPC egress）           │
│        └─▶ Code Interpreter（统计分析）                                        │
│        └─▶ mem0 长期记忆（自部署）─▶ 记忆 Lambda(VPC) ─▶ Aurora pgvector       │
└────────────────────────────────────────────────────────────────────────────────┘
```

## 设计要点：用 Skill 承载业务 SOP

Part 1 的核心思路是 **"Agent 框架不变，业务逻辑用 Skill 插拔"**：

- Agent 本体只包含通用能力：浏览器爬取、沙箱计算、数据入库。
- 具体业务流程（本期是跨境电商选品 SOP）转化为 Strands 的 **Skill**（`SKILL.md`），
  通过 `AgentSkills` 插件加载。
- 以后要新增其他场景的数据摄入 pipeline（如竞品监控、价格追踪），
  只需要新写一个 `SKILL.md` 放进 `skills/` 目录，无需改动 Agent 代码。

## 中国区移植说明

AgentCore 在中国区（`cn-north-1` / `cn-northwest-1`）尚未上线。本 workshop
先在海外区（`us-west-2`）用等效架构验证，**架构层面已提前对齐中国区可用的形态**，
上线后按下面的清单移植即可。

**已就绪、无需改动的设计**：

- **数据面不依赖 RDS Data API**（中国区没有该服务）：Part 1 的 Agent 写库与
  建表都走「lambda.invoke → VPC 内写库 Lambda → pg8000 直连」，两边通用。
- **Gateway 零 VPC egress**：私网访问全部由 VPC 内 Lambda 承担，不依赖
  Gateway 的 VPC 出口能力。
- **认证自部署 Keycloak**（中国区没有 Cognito）：OIDC 全链路不依赖海外托管服务。
- **长期记忆自部署 mem0**（不用 AgentCore 托管 Memory）：向量存 Aurora
  pgvector、经 VPC 内 Lambda 读写（与写库/分析 Lambda 同构）；mem0 的事实
  提取 LLM 与 embedding 本来就走 OpenAI 兼容端点（`MEM0_*` 环境变量），
  中国区只需把端点指向区内模型服务商，向量面零改动。唯一注意点：换
  embedding 模型时若维度变化，需换 `MEM0_COLLECTION` 重建向量表。

**移植时需要处理的差异**（AgentCore 中国区上线后）：

| 差异点 | 说明 |
|--------|------|
| Part 2 残留的 Data API 调用 | 会话历史落库/回灌（`part2_chat_portal/agent/main.py`）、tasks-api / runner 两个 Lambda、部署脚本的建表预检，共 5 个文件。按 Part 1 写库 Lambda 的同款模式迁移；迁完可关集群 `EnableHttpEndpoint`、删 `AgentDataAccessPolicy` |
| ARN 分区前缀 | 模板与脚本里约 30 处硬编码 `arn:aws:`，中国区是 `arn:aws-cn:`。CloudFormation 内建议统一改 `${AWS::Partition}`（含 `AWSLambdaVPCAccessExecutionRole` 这类 AWS 托管策略 ARN），脚本内拼接同理 |
| 区域与域名 | 默认区域 `us-west-2` 散在两个 deploy.sh 与 agent 代码默认值里；API Gateway 等服务域名后缀变为 `.amazonaws.com.cn` |
| VPC 接口端点服务名 | `com.amazonaws.<region>.secretsmanager` → 中国区前缀不同：`cn.com.amazonaws.<region>.secretsmanager` |
| 模型 | 中国区没有 Bedrock。改用任意 OpenAI 兼容的 Chat Completions 模型服务即可：模型类从 `OpenAIResponsesModel` 换成 Strands 的 `OpenAIModel`（Chat Completions），`OPENAI_BASE_URL` / `MODEL_ID` / `OPENAI_API_KEY` 本来就走环境变量，指向区内模型服务商的兼容端点即可 |
| 镜像与外网可达性 | Keycloak 基础镜像来自 `quay.io`，中国区构建前先转存到区内 ECR；Browser 出网爬 eBay 等境外站点的网络可达性需实测 |

## 目录结构

```
.
├── README.md                        # 本文件
├── part1_data_ingestion/            # Part 1：数据摄入 pipeline
│   ├── README.md                    # Part 1 部署运行指南
│   ├── agent/                       # Agent 代码（部署到 AgentCore Runtime）
│   │   ├── main.py                  # 入口：BedrockAgentCoreApp + Strands Agent
│   │   ├── requirements.txt         # 依赖
│   │   ├── skills/                  # Skill 目录（业务 SOP 插拔点）
│   │   │   ├── product-selection-ebay/  # 选品 Skill —— eBay（默认站点）
│   │   │   │   └── SKILL.md
│   │   │   └── product-selection/   # 选品 Skill —— 亚马逊（需代理）
│   │   │       └── SKILL.md
│   │   └── tools/                   # 自定义工具
│   │       ├── aurora_tools.py      # Aurora 入库工具（invoke 写库 Lambda）
│   │       └── browser_ext.py       # Browser 扩展（Web Bot Auth / Profile）
│   ├── infra/                       # 基础设施
│   │   ├── aurora-serverless.yaml   # CloudFormation：Aurora Serverless v2 + VPC
│   │   ├── writer-lambda/           # 写库 Lambda（VPC 内 pg8000 直连，含建表 op）
│   │   │   └── handler.py
│   │   └── setup_tables.sql         # 建表 DDL（打包进写库 Lambda 执行）
│   └── scripts/                     # 部署与测试脚本
│       ├── deploy.sh                # 一键部署（infra + agent）
│       ├── local_test.py            # 本地测试 Agent
│       └── invoke_example.sh        # 调用已部署 Agent 的示例
└── part2_chat_portal/               # Part 2：Web Chat 分析门户
    ├── README.md                    # Part 2 部署运行指南
    ├── agent/                       # 分析 Agent（流式 + Identity M2M + Gateway 工具）
    ├── infra/                       # Keycloak 镜像 / 数据查询 Lambda / CloudFormation
    ├── scripts/                     # 一键部署与冒烟测试脚本
    └── web/                         # 前端 SPA（Vite + React，本地 dev server）
```

## 快速开始

推荐从 [实验手册](实验手册.md) 开始：两个实验的部署与上手操作一份文档走完。
需要更深入的细节再看各部分的 README：

1. [Part 1 部署运行指南](part1_data_ingestion/README.md)
2. [Part 2 Web Chat 分析门户](part2_chat_portal/README.md)（依赖 Part 1 的数仓）

## 前置条件

- AWS 账号，区域建议 `us-west-2`（AgentCore 目前在部分区域可用）
- 已在 Bedrock 控制台开通 OpenAI GPT-5.6 模型访问权限，并生成 Bedrock API key
  （Part 1 默认模型 `openai.gpt-5.6-terra` 走 bedrock-mantle 端点，
  `export AWS_BEARER_TOKEN_BEDROCK=<Bedrock API key>`；不设置则用
  IAM 角色自动铸 token）
- Python 3.10+，配置好 AWS 凭证（`aws configure`）
