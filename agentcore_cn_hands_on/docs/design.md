# AgentCore CN Hands-on：VS Code IDE Server + 分步部署方案设计

日期：2026-08-29

## 目标

1. 从 `openclaw-saas-gcr/cloudformation/cloudlab-template-china.yaml` 中抽离 IDE 部分，
   构建一个**独立的 VS Code（code-server）IDE Server** CloudFormation 模板——去掉全部
   EKS/OpenClaw 相关资源，只保留 IDE 所需（VPC + EC2 + ALB + Secrets 密码 + SSM 引导）。
2. IDE 实例的 terminal 里可以直接运行 `agentcore_cn_workshop` 的两个场景
   （场景 1：数据摄入 Pipeline；场景 2：Web Chat 分析门户）。
3. 拆分为三个分步脚本：`01_deploy_base.sh`（基础环境）、`02_run_scenario1.sh`（场景 1）、
   `03_run_scenario2.sh`（场景 2），另附 `99_cleanup.sh`（资源清理）。
4. IDE 实例 IAM 角色遵循**最小权限原则**：按 workshop 实际触达的服务/资源名前缀收敛
   （`agentcore-workshop-*`、`AmazonBedrockAgentCoreSDK*`、`bedrock-agentcore-*` 等），
   不再给 `eks:*`、`sqs:*`、`wafv2:*` 等 OpenClaw 遗留权限。
5. 输出完整的《运行手册》，指导客户一步步完成部署。

## 架构决策

### IDE Server（ide/vscode-ide-server.yaml）

- 独立小 VPC（10.40.0.0/16，2 个公有子网跨 2 AZ——ALB 最低要求），**无 NAT**
  （实例在公有子网直接出网，省成本）。AZ 用 `Fn::GetAZs` 自动选择，全分区通用。
- EC2（默认 t3.large / 50GB gp3 加密盘）运行 code-server，监听 8889；
  Internet-facing ALB 监听 8889 转发 8889（入口用非标准端口：中国区未备案账号
  会被封 80/8080/443，非标准端口不受限；没有 CloudFront + 备案限制，沿用
  openclaw 中国区模板的 ALB 直出形态）。`AllowedCidr` 参数可收敛来源 IP。
- 登录密码由 Secrets Manager 生成（32 位），bootstrap 时取出做 argon2 哈希写入
  code-server 配置——沿用 openclaw 模板的成熟做法。
- 引导链：SSM Document（安装工具 + 拉取 workshop 代码 + 配置 code-server）
  ← Lambda 自定义资源触发（等 SSM Agent 上线后 send-command 并等待完成）。
- 工具集：git / docker / python3.11 / nodejs20+npm / jq / zip / argon2 / AWS CLI v2 /
  code-server（版本参数化；中国区可用 `CodeServerRpmUrl` 覆盖为区内可达镜像）。
- workshop 代码分发：`deploy-ide.sh` 在操作员机器上把 `agentcore_cn_workshop` +
  `agentcore_cn_hands_on` 打成 tar.gz 传 S3，模板参数 `WorkshopAssetsS3Uri` 传入，
  bootstrap 下载解压到 `/home/ec2-user/environment/`（中国区 GitHub 不可靠，
  git clone 仅作为备选写进手册）。
- 分区全模板化：`${AWS::Partition}` / `${AWS::URLSuffix}`，中国区/全球区同一模板。

### 最小权限（IdeRole）

拆成 3 个客户托管策略（避开单策略 6144 字符限制）：

| 策略 | 覆盖 | 收敛方式 |
|------|------|---------|
| infra | CloudFormation / EC2-VPC / RDS / SecretsManager / Logs / Scheduler | CFN 栈限定 `stack/agentcore-workshop-*`；Scheduler 限定 `agentcore-workshop-ingestion`；EC2/RDS 按动作枚举（资源 ARN 不可预知，资源留 `*` 但动作最小化）|
| iam | 角色/策略管理 + PassRole | 角色限定 `agentcore-workshop-*`、`AmazonBedrockAgentCoreSDK*`；策略限定 `agentcore-workshop-*`；PassRole 附 `iam:PassedToService` 条件 |
| app | Lambda / ECR / ECS / Cloud Map / API Gateway / bedrock-agentcore / CodeBuild / S3 / Bedrock | Lambda 限定 `function:agentcore-workshop-*`；ECR 限定 `agentcore-workshop*` 与 `bedrock-agentcore-*` 仓库；agentcore 限定本账号 + `:aws:` 区域资源；S3/CodeBuild 限定 `bedrock-agentcore-*`（starter toolkit 产物）|

明确移除（openclaw 遗留、workshop 用不到）：`eks:*`、`sqs:*`、`wafv2/waf-regional:*`、
`elasticfilesystem:*`、`elasticloadbalancing:*`（workshop 不建 ALB）、`kms:*`、
`pricing:*`、`events:*`、`autoscaling:*`、EC2 实例管理（RunInstances 等）。

### 分步脚本（scripts/，在 IDE terminal 内运行）

原 workshop 的 part1 `deploy.sh` 一次跑完 6 步（含基础设施），part2 `deploy.sh` 跑 10 步。
拆分原则：**基础环境与场景解耦；场景 2 复用原脚本**（其 10 步内部强耦合，重抄 780 行
只会引入漂移）：

- `00_env.sh`：公共库（被 source）。区域/分区推导、workshop 目录定位、venv 激活、
  agentcore CLI 检查、模型三元组校验函数（fail-fast 逻辑与原 deploy.sh 一致）。
- `01_deploy_base.sh`：基础环境 = part1 deploy.sh 的第 1-3 步抽离
  （Aurora Serverless v2 + VPC 栈、写库 Lambda、建表）+ Python venv/starter-toolkit
  安装 + 工具自检。**不需要模型 key**。
- `02_run_scenario1.sh`：场景 1 = part1 deploy.sh 的第 4-6 步抽离
  （自定义 Browser + Profile、`agentcore configure/launch`、Runtime 角色策略附加）
  + 冒烟调用（`agentcore invoke`，可 `SKIP_SMOKE=1` 跳过）。需要模型 key。
- `03_run_scenario2.sh`：场景 2 = 预检（docker/Part1 完成度）后**调用原
  `part2_chat_portal/scripts/deploy.sh`**（幂等、已验证），再跑 `chat_test.sh` 冒烟
  + 打印前端启动/端口转发指引。需要模型 key。
- `99_cleanup.sh`：按实验手册清理章节逆序自动化（调度→Lambda→AgentCore 资源→
  4 个 CFN 栈→ECR 仓库→日志组）。IDE 栈本身由 `ide/destroy-ide.sh` 清理。

所有脚本幂等可重跑；`AWS_REGION` 是唯一开关（cn-* 自动切分区/域名/SDK 端点覆盖）。

### 前端访问（场景 2）

Keycloak PKCE 要求安全上下文（localhost 或 HTTPS），而 IDE ALB 是 HTTP——前端**不能**
经 ALB 访问。手册主推：客户笔记本上 `aws ssm start-session` 端口转发 5173 到 IDE 实例，
浏览器开 `http://localhost:5173`（localhost 属安全上下文，且与 Keycloak realm 的
redirect_uri / Tasks API CORS 配置一致）。备选：把 `web/.env.local` 拷到本机跑前端。

### 修订（2026-08-29 下午）：合并为 CloudLab 单栈

应用户要求，把「IDE 栈」与「数仓 Aurora 栈」合并为一个栈
`cloudformation/cloudlab-template-china.yaml`（目录形态对齐 openclaw workshop），
默认栈名 `agentcore-workshop-base`：

- 数仓资源（私有 VPC/Aurora/Secrets 端点/写库角色/调用策略）从 part1 的
  `aurora-serverless.yaml` 原样并入，**输出键保持一致**；
- part2 的 `deploy.sh` 打了一行补丁：`AURORA_STACK` 支持环境变量覆盖
  （由 `00_env.sh` export 指向单栈）——这是对 workshop 仓库唯一的改动；
- `01_deploy_base.sh` 不再部署 CFN，只做 venv + 写库 Lambda + 建表；
- **一键销毁**：栈内 `CleanupOnDelete` 自定义资源（Lambda，挂同一组最小权限
  托管策略）在删栈时先清扫步骤脚本创建的栈外资源（调度、6 个 Lambda、
  AgentCore Runtime/Browser/Gateway/Identity、part2 三个子栈、ECR、SDK 角色、
  残留 ENI）。关键时序：自定义资源 DependsOn 数仓 VPC 面资源与三个托管策略
  （删除时先跑清扫、后删网络与策略）；且必须**等子栈删完才返回**——子栈删除
  使用清理 Lambda 的会话凭证，提前返回会导致策略被删、子栈 DELETE_FAILED；
- `99_cleanup.sh` 降级为「只重置两个场景、保留基础栈」的练习复位工具。

### 修订（2026-08-30）：门户公网化的三种形态

场景 2 前端的 PKCE 登录要求安全上下文（HTTPS 或 localhost）。在端口转发之外，
模板提供两个公网化开关（可同时开）：

- `ENABLE_PUBLIC_PORTAL=1`（HTTPS）：deploy-ide.sh 自签证书导入 ACM →
  现有 ALB 加 8443 HTTPS 监听器 → 实例 5173。浏览器一次证书告警。
- `ENABLE_HTTP_PORTAL=1`（纯 HTTP，中国区最顺滑）：新建 NLB TCP:5173 直通
  实例 5173，免备案（非标准端口）、免告警。配套三处前端/realm 改动：
  1. `auth.ts`：`pkceMethod: window.isSecureContext ? "S256" : false`——
     非安全上下文没有 `crypto.subtle`，退回无 PKCE 的授权码流程；
  2. realm-template.json：移除 `pkce.code.challenge.method=S256` 强制
     （安全上下文下前端仍主动 S256；已部署过 Keycloak 需 KC_REDEPLOY=1）；
  3. `insecure-context-polyfill.ts`（main.tsx 首个 import）：非安全上下文垫
     `crypto.randomUUID`（session id 生成用）。
  ⚠️ HTTP 形态 token 明文传输，仅限 workshop 演示。
  注意 NLB 保留源 IP 时实例自访会 hairpin 失败——验证要从外部网络发起。

端口相关的分区事实：中国区未备案账号被封 80/8080/443，IDE 入口（8889）、
HTTPS 门户（8443）、HTTP 门户（5173）全部使用非标准端口规避；中国区
CloudFront 必须绑定已备案域名，故不采用 CloudFront 方案。

### 测试计划（当前账号，us-west-2）

1. `deploy-ide.sh` 部署 IDE 栈 → ALB URL 可达、密码可取、code-server 登录页正常。
2. 经 SSM 在实例内以 ec2-user 依次跑 01 → 02 → 03，用实例 profile（即最小权限角色）
   验证权限收敛正确，出现 AccessDenied 按最小增量补。
3. 模型 key（`AWS_BEARER_TOKEN_BEDROCK`）为长期凭证，自动化会话不代为生成，
   到达该门槛时向用户请求提供；02/03 的部署段与 key 无关部分先行验证。

### 中国区（宁夏 cn-northwest-1）资产分发与实测决策（2026-09）

- **资产区内化**：中国区建栈从全球区 CloudFront 拉资产跨境慢且不稳，叠加
  bootstrap waiter（约 13 分钟）极易超时回滚。改为区内 S3 + 中国区 CloudFront
  分发（备案别名域，**中国区 CloudFront 默认域名 `*.cloudfront.cn` 被政策
  禁止直接服务**，403）。HTTPS 证书过期时走 HTTP（资产为公开 workshop 代码，
  可接受）。`publish-assets.sh` 增加 cn 通道；`deploy-ide.sh` 内置 cn 默认
  `ASSET_BASE_URL` 并透传模板。
- **大文件工具资产**（awscli zip / code-server rpm / Keycloak arm64 镜像 tar，
  共约 420MB）不随每次发布：维护者在 jumphost 下载（github/quay 出口快），
  用本地生成的 S3 presigned PUT URL 直传宁夏桶（us-west-2 → 宁夏实测
  10~25MB/s）。
- **Keycloak 基础镜像**：bootstrap cn 分支预载镜像 tar（`docker load`），
  场景 2 `docker build` 命中本地 tag 后不再访问 quay.io；预载失败仅告警，
  回退直拉。
- **包管理镜像源**：bootstrap cn 分支写 `/etc/pip.conf`（TUNA）与
  `~ec2-user/.npmrc`（npmmirror），覆盖建栈与场景脚本的全部 pip/npm 安装。
- **实测修正的分区差异**：
  - 接口端点服务名**按服务不一致**：secretsmanager 在 cn-north-1 /
    cn-northwest-1 均为 `com.amazonaws.<region>.secretsmanager`（无 `cn.`
    前缀），而 execute-api / lambda 带 `cn.` 前缀——原「中国区统一加前缀」
    假设导致建栈 400，已按服务写死。
  - IAM principal：`lambda.${AWS::URLSuffix}` 在 cn 展开为 `.com.cn` 不被
    接受，统一硬编码 `lambda.amazonaws.com`（EC2 仍走 URLSuffix，cn 确为
    `ec2.amazonaws.com.cn`）；`iam:PassedToService` 双写两种形态兜底。
  - 测试账号开启账户级 IMDSv2 强制（httpTokensEnforced），IDE 实例经最小
    LaunchTemplate 注入 `HttpTokens=required`（两分区通用的安全默认）。
  - 宁夏区实测可用：Aurora PG 17.10 + db.serverless + 托管主密码、NLB 挂
    安全组、EventBridge Scheduler、AgentCore Runtime/Browser/Gateway/
    Identity/Code Interpreter（托管 Memory 未上线，本项目用自建 mem0
    不受影响；原生 Bedrock 端点不存在，模型必须走 OpenAI 兼容网关）。

### Keycloak 建栈即就绪（消除学员路径的 ecs:UpdateService，2026-10）

受管账号 SCP 显式 Deny `ecs:UpdateService`（他账号实测，豁免只给建栈者），
原「服务 0 副本建栈、03 推完镜像拉到 1 副本」的形态在学员路径上必炸。改为：

- realm 的 4 个动态值（analysis-agent client secret + 三个门户用户密码）
  从 deploy.sh 运行期生成改为**栈管 Secret**（GenerateSecretString）——
  删栈自动清理、天然幂等、IDE 角色只需 4 个精确 ARN 的 GetSecretValue；
- IDE bootstrap 新增 [4.8/6]：读 Secret → sed 渲染 realm →
  docker build（基镜像 cn 已预载）→ push ECR → **预写
  part2 的 .deploy/secrets.env**（与 realm 烧入值同源，600 权限）——
  deploy.sh「secrets.env 已存在则复用」与手册 grep 密码命令零改动；
- `KcService` DesiredCount 0→1 并 `DependsOn` bootstrap（CFN 对 ECS
  服务创建会等稳定，镜像先行入仓是硬前置，故本段失败即建栈失败）；
- deploy.sh：ECR 已有 latest 镜像则跳过渲染/构建/推送；服务
  runningCount≥1 则跳过 `ecs update-service`（只保留 discovery 在线等待）。
  `KC_REDEPLOY=1`（换 realm）降级为维护操作，需 ecs 权限/豁免身份。

Tasks API 的 JWT authorizer 仍留在 03（create-authorizer 时 API Gateway
会实际校验 issuer discovery，Keycloak 任务在建栈末段才在线，无法前置）。
