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

AgentCore 已在宁夏区（`cn-northwest-1`）上线，本 workshop 已在宁夏区完成
**全量端到端实测**（2026-09）：Runtime / Browser / Gateway / Identity /
Code Interpreter 可用，托管 Memory 暂未上线（本仓库长期记忆用自建 mem0，
不受影响）。两个分区共用同一套代码，**区域代码（`AWS_REGION`）是唯一开关**。

移植的核心形态：数据面零 RDS Data API（统一 VPC 内 Lambda）、模型统一
OpenAI 兼容三元组（中国区指区内模型网关）、`direct_code_deploy` 部署
（aws-cn 无 CodeBuild ARM_CONTAINER）、自部署 Keycloak（无 Cognito）与
自建 mem0。分区差异明细、宁夏区实测修复清单（接口端点服务名、IAM
principal、IMDSv2、模型工具 schema 净化等）与实测结论见
**[docs/中国区移植说明.md](docs/中国区移植说明.md)**。

## 目录结构

```
.
├── README.md                        # 本文件
├── 实验手册.md                       # 动手主线：两个实验的部署与上手（从这里开始）
├── docs/                            # 设计类文档
│   ├── 架构与设计.md                 # 架构详解、Skill 机制、认证链、模型接入设计
│   ├── 中国区移植说明.md             # 分区差异、宁夏区实测修复清单与结论
│   └── images/                      # 文档截图
├── agentcore_cn_hands_on/           # 现场活动交付包（浏览器 IDE + 一键建栈 + 学员手册）
│   ├── 中国区实验手册.md             # 学员视角手册（宁夏区，模型网关信息现场发放）
│   ├── 运行手册.md                   # 操作员视角 CloudLab 部署手册
│   ├── cloudformation/              # CloudLab 单栈模板（IDE + 数仓 + 清理钩子）
│   ├── ide/                         # 部署/销毁/资产发布脚本
│   └── scripts/                     # IDE 内分步执行脚本
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
参加现场活动（预置 CloudLab 环境）直接看
[学员版中国区实验手册](agentcore_cn_hands_on/中国区实验手册.md)。
想理解实现再读 [docs/架构与设计.md](docs/架构与设计.md)，以及各部分 README：

1. [Part 1 部署运行指南](part1_data_ingestion/README.md)
2. [Part 2 Web Chat 分析门户](part2_chat_portal/README.md)（依赖 Part 1 的数仓）

## 前置条件

- AWS 账号。区域是唯一开关：中国区 `cn-northwest-1`（宁夏，已全量实测）、
  海外 `us-west-2`（`AWS_REGION` 驱动分区/域名等全部差异）
- 模型（标准 OpenAI 兼容 Chat Completions，key 必填）：中国区 export
  `OPENAI_BASE_URL + OPENAI_API_KEY + MODEL_ID` 指区内 OpenAI 兼容模型网关
  （需支持工具调用；启用长期记忆另需 embedding 模型）；全球区在 Bedrock
  控制台开通 OpenAI GPT-5.6 并生成 Bedrock API key
  （`export AWS_BEARER_TOKEN_BEDROCK=<Bedrock API key>`，默认走
  bedrock-mantle 端点）
- Python 3.10+，配置好 AWS 凭证（`aws configure`）；
  `pip install "bedrock-agentcore-starter-toolkit==0.3.14" "uv==0.12.23"`
  （端到端实测锁定的版本组合）

## 成本估算（宁夏区，粗略）

按需计费的常驻部分：Aurora Serverless v2（Min 0.5 ACU warm standby）+
Keycloak Fargate（0.25 vCPU）+ 若干 Lambda/API Gateway（调用量极小），
合计约 **¥25~40/天**；走 CloudLab 交付包另加 IDE EC2 t3.large + ALB/NLB，
合计约 **¥60~90/天**。模型调用费用取决于所接网关的计费方式。实验结束按
手册「资源清理」一键销毁即不再产生费用。
