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
先在海外区（`us-west-2`）用等效架构验证，**代码层面的移植改动已全部完成**：
两个分区共用同一套代码，区域代码（`AWS_REGION`）是唯一开关。

**已完成、两分区通用的形态**：

- **数据面零 RDS Data API**（中国区没有该服务，已实证）：Part 1 走写库 Lambda；
  Part 2 的会话历史（Runtime）、任务表（tasks-api / runner）统一经通用数据面
  Lambda（`agentcore-workshop-db`，`op=execute` 协议、pg8000 直连），部署预检
  走写库 Lambda 的 `check_tables`。集群 `EnableHttpEndpoint` 与
  `AgentDataAccessPolicy` 已删除。
- **模型统一标准 OpenAI 兼容端点 + key**：两个 Agent 都用 Strands `OpenAIModel`
  （Chat Completions），`OPENAI_BASE_URL` / `OPENAI_API_KEY` / `MODEL_ID` 三元组
  env 注入。全球区默认 bedrock-mantle（其 `/chat/completions` 已实测可用，
  key 用 Bedrock API key）；中国区 export 区内模型服务商的三元组即可，
  IAM 铸 token 路径已移除。gpt-5.6-terra 在 `/chat/completions` 上用函数工具
  必须 `reasoning_effort=none`（端点硬约束），Agent 对默认 mantle 端点自动
  处理；自定义端点缺省不发该参数，可用 `MODEL_REASONING_EFFORT` 显式指定。
- **分区/域名全模板化**：CloudFormation 统一 `${AWS::Partition}` /
  `${AWS::URLSuffix}`（含 AWS 托管策略 ARN 与 VPC 端点服务名的
  `cn.com.amazonaws` 前缀 Condition）；部署脚本按区域推导
  `PARTITION` / `AWS_DOMAIN`，并在 cn-* 分支自动 export
  `BEDROCK_AGENTCORE_CP_ENDPOINT` / `DP_ENDPOINT` 覆盖 SDK 内置端点。
- **Gateway 零 VPC egress**：私网访问全部由 VPC 内 Lambda 承担，不依赖
  Gateway 的 VPC 出口能力。
- **认证自部署 Keycloak**（中国区没有 Cognito）：OIDC 全链路不依赖海外托管服务。
- **长期记忆自部署 mem0**：向量存 Aurora pgvector、经 VPC 内 Lambda 读写；
  提取 LLM 与 embedding 走 OpenAI 兼容端点（`MEM0_*` 环境变量），中国区换
  端点即可。换 embedding 模型若维度变化，需换 `MEM0_COLLECTION` 重建向量表。

**AgentCore 中国区上线后仍需处理的事项**：

| 事项 | 说明 |
|--------|------|
| Keycloak 基础镜像转存 | 镜像来自 `quay.io`，中国区构建前先转存到区内 ECR（`docker pull` → 区内 ECR push，deploy.sh 构建段配合改 build-arg） |
| 出网可达性实测 | Browser 出网爬 eBay 等境外站点在中国区的链路质量与反爬挑战需实测；Web Bot Auth（预览特性）若中国区首发不含需降级 |
| 上线验证清单 | Aurora PG 引擎小版本目录、starter-toolkit 对 aws-cn 的支持、Browser Live View（DCV 直播）是否随首发、`bedrock-agentcore.amazonaws.com` 服务 principal 写法——均以中国区实际发布为准逐项验证 |

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

- AWS 账号。区域是唯一开关：海外 `us-west-2`、中国区 `cn-northwest-1`
  （`AWS_REGION` 驱动分区/域名等全部差异）
- 模型（标准 OpenAI 兼容 Chat Completions，key 必填）：全球区在 Bedrock
  控制台开通 OpenAI GPT-5.6 并生成 Bedrock API key
  （`export AWS_BEARER_TOKEN_BEDROCK=<Bedrock API key>`，默认走
  bedrock-mantle 端点）；中国区 export
  `OPENAI_BASE_URL + OPENAI_API_KEY + MODEL_ID` 指区内 OpenAI 兼容服务
- Python 3.10+，配置好 AWS 凭证（`aws configure`）
