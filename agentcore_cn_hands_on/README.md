# agentcore_cn_hands_on

AgentCore Workshop 的**动手实验交付包**：一个 CloudLab 单栈（浏览器版
VS Code IDE Server + workshop 数仓基础环境，删栈一键全部销毁）+ 三个分步脚本，
让客户在自己账号里按「基础环境 → 场景 1 → 场景 2」逐步完成
`agentcore_cn_workshop` 的两个实验。IDE 实例 IAM 角色按最小权限收敛。

实验背景与场景说明见 [实验介绍.md](实验介绍.md)，动手操作从 [运行手册.md](运行手册.md) 开始。

## 目录

```
agentcore_cn_hands_on/
├── README.md                    # 本文件
├── 实验介绍.md                  # 实验背景与场景说明（Workshop 门户引导文案）
├── 场景1介绍.md                 # 场景 1 动手实验描述（Skill 心智模型 + 决策流水线流程图）
├── 场景2介绍.md                 # 场景 2 场景介绍（心智模型 + 架构 + 认证链时序图 + 设计点）
├── 场景2操作手册.md             # 场景 2 操作手册（IDE 内分步操作 + 记忆/任务演示 + 排障）
├── 运行手册.md                  # 客户逐步操作手册（从这里开始）
├── docs/design.md               # 方案设计与决策记录
├── cloudformation/
│   └── cloudlab-template-china.yaml  # 单栈：IDE(VPC+EC2+ALB+最小权限角色)
│                                     #  + 数仓(私有VPC+Aurora+Secrets端点)
│                                     #  + 删除清理钩子（一键销毁）
├── ide/
│   ├── deploy-ide.sh            # 操作员机器：打包代码上 S3 + 部署单栈
│   └── destroy-ide.sh           # 一键销毁（删单栈 + 资产桶）
└── scripts/                     # IDE Terminal 内执行
    ├── 00_env.sh                # 公共库（区域/分区推导、venv、模型 key 校验）
    ├── 01_deploy_base.sh        # 1/ 部署基础环境（写库 Lambda + 建表）
    ├── 02_run_scenario1.sh      # 2/ 运行场景 1（数据摄入 Agent + 冒烟调用）
    ├── 03_run_scenario2.sh      # 3/ 运行场景 2（Web Chat 分析门户 + 冒烟）
    └── 99_cleanup.sh            # 仅重置两个场景（保留基础栈）
```

## 快速开始

```bash
# 操作员机器（全球区）
cd ide && AWS_REGION=us-west-2 bash deploy-ide.sh
# 操作员机器（中国区宁夏，资产区内 CDN 默认值已内置）
cd ide && AWS_REGION=cn-northwest-1 bash deploy-ide.sh

# 之后全部在 IDE Terminal 内：
bash ~/environment/agentcore_cn_workshop/scripts/01_deploy_base.sh
export AWS_BEARER_TOKEN_BEDROCK=<Bedrock API key>   # 全球区
# 中国区改 OpenAI 兼容三元组：
# export OPENAI_BASE_URL=<模型网关>/v1 OPENAI_API_KEY=<key> MODEL_ID=<模型名>
#（推荐改填 workshop 根的 .env 文件——模板 .env.example，脚本自动加载，免逐 Terminal export）
bash ~/environment/agentcore_cn_workshop/scripts/02_run_scenario1.sh
bash ~/environment/agentcore_cn_workshop/scripts/03_run_scenario2.sh
```

学员视角的完整实验手册（中国区 · 宁夏）见 [中国区实验手册.md](中国区实验手册.md)。

## 来源说明

- IDE 形态（code-server + ALB + SSM 引导 + Secrets 密码）抽离自
  `openclaw-saas-gcr/cloudformation/cloudlab-template-china.yaml`，
  去掉了全部 EKS/OpenClaw 资源；IAM 从 `eks:*`/`sqs:*` 级别的宽权限收敛为
  仅覆盖 workshop 部署面的最小权限（详见运行手册第 7 章）。
- 场景脚本抽离/复用自 `agentcore_cn_workshop` 的 part1/part2 部署脚本，
  保持幂等与中国区分区自适应（`AWS_REGION` 是唯一开关）。
