# Amazon Bedrock AgentCore 中文 Workshop

本项目是一个端到端的 demo，帮助你快速熟悉 **Amazon Bedrock AgentCore** 产品家族的核心能力：

| 组件 | 作用 | 本 demo 中的用途 |
|------|------|------------------|
| **Runtime** | 托管 Agent 的无服务器运行时（会话隔离、自动扩缩容） | 承载 Part 1 的数据摄入 Agent 和 Part 2 的分析 Agent |
| **Browser（内置工具）** | 云端托管的无头浏览器（CDP 协议，会话隔离） | 爬取 eBay 商品页面数据 |
| **Code Interpreter（内置工具）** | 安全沙箱内执行代码 | 执行选品经济测算（产品点公式）与数据分析 |
| **Gateway** | 把现有 API / Lambda 转换成 MCP 工具 | Part 2 中把数据查询 API 暴露给分析 Agent |
| **Identity** | Agent 的身份与凭证管理（OAuth / API Key） | Part 2 中 Web 门户的用户认证与 Agent 授权 |

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
│   │  │  + Redshift 入库工具    │  │      └─────────────────────┘               │
│   │  └────────────────────────┘  │                                            │
│   └──────────────┬───────────────┘                                            │
│                  │  redshift-data API                                         │
│                  ▼                                                            │
│   ┌──────────────────────────────┐                                            │
│   │  Redshift Serverless         │  候选档案表 + 判定记录表                     │
│   └──────────────────────────────┘                                            │
└────────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────── Part 2: Web Chat 分析门户（预留） ───────────────────────┐
│   Web 门户 ──▶ AgentCore Identity（认证）──▶ Runtime（分析 Agent）              │
│                     └─▶ Gateway（数据查询 MCP 工具）──▶ Redshift               │
│                     └─▶ Code Interpreter（图表 / 统计分析）                    │
└────────────────────────────────────────────────────────────────────────────────┘
```

## 设计要点：用 Skill 承载业务 SOP

Part 1 的核心思路是 **"Agent 框架不变，业务逻辑用 Skill 插拔"**：

- Agent 本体只包含通用能力：浏览器爬取、沙箱计算、数据入库。
- 具体业务流程（本期是跨境电商选品 SOP）转化为 Strands 的 **Skill**（`SKILL.md`），
  通过 `AgentSkills` 插件加载。
- 以后要新增其他场景的数据摄入 pipeline（如竞品监控、价格追踪），
  只需要新写一个 `SKILL.md` 放进 `skills/` 目录，无需改动 Agent 代码。

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
│   │       ├── redshift_tools.py    # Redshift Serverless 入库工具
│   │       └── browser_ext.py       # Browser 扩展（Web Bot Auth / Profile）
│   ├── infra/                       # 基础设施
│   │   ├── redshift-serverless.yaml # CloudFormation：Redshift Serverless
│   │   └── setup_tables.sql         # 建表 DDL
│   └── scripts/                     # 部署与测试脚本
│       ├── deploy.sh                # 一键部署（infra + agent）
│       ├── local_test.py            # 本地测试 Agent
│       └── invoke_example.sh        # 调用已部署 Agent 的示例
└── part2_chat_portal/               # Part 2：Web Chat 分析门户（预留）
    └── README.md
```

## 快速开始

1. 阅读并完成 [Part 1 部署运行指南](part1_data_ingestion/README.md)。
2. Part 2 需求确认后再开发，见 [占位说明](part2_chat_portal/README.md)。

## 前置条件

- AWS 账号，区域建议 `us-west-2`（AgentCore 目前在部分区域可用）
- 已在 Bedrock 控制台开通 OpenAI GPT-5.6 模型访问权限，并生成 Bedrock API key
  （Part 1 默认模型 `openai.gpt-5.6-terra` 走 bedrock-mantle 端点，
  `export AWS_BEARER_TOKEN_BEDROCK=<Bedrock API key>`；不设置则用
  IAM 角色自动铸 token）
- Python 3.10+，配置好 AWS 凭证（`aws configure`）
