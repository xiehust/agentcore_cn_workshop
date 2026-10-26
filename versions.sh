#!/usr/bin/env bash
# =============================================================================
# 依赖版本锁（单一事实源）
#
# 所有部署脚本的第三方依赖版本都集中在此。改版本只改这里，各脚本 source 本文件后
# 引用下面的变量，不要再在 pip 行里硬编码版本号。
#
# 被谁 source：
#   · agentcore_cn_hands_on/scripts/00_env.sh（→ 01/02/03 间接获得）
#   · part1_data_ingestion/scripts/deploy.sh
#   · part2_chat_portal/scripts/deploy.sh
#
# 幂等：可被重复 source；仅做 export，无副作用。
# =============================================================================

# ---- VPC 内 Lambda 的 PostgreSQL 直连依赖（pg8000 + 其 SCRAM 认证依赖）----
# scramp 1.5.0（2025-10-25 发布）删除了 pg8000 1.31.5 建 SSL 连接时调用的
# make_channel_binding，不锁会让 pip 拉到 1.5.0，每次连库抛 AttributeError 被
# handler 吞掉重试、表现为无日志的 300s Lambda 超时。三者成套锁定。
export PG8000_VERSION="1.31.5"
export SCRAMP_VERSION="1.4.17"
export DATEUTIL_SPEC="python-dateutil<3.0.0"
# 组合规格：各 pip 行统一用 `pip install -q $PGLIBS_SPEC -t <dir>`
export PGLIBS_SPEC="pg8000==${PG8000_VERSION} scramp==${SCRAMP_VERSION} ${DATEUTIL_SPEC}"

# ---- agentcore 工具链（00_env.sh 的 ensure_venv 建 venv 时装）----
# toolkit 的 CLI 在弃用流程中、行为随版本漂移（0.3.13 起默认 CodeBuild 容器部署，
# aws-cn 必挂）；uv 是 direct_code_deploy 打包的硬前置。两者都锁到端到端实测过的
# 版本，升级前先在一次完整建栈 + 02/03 里验证再改。
export TOOLKIT_VERSION="0.3.14"
export UV_VERSION="0.12.23"

# ---- Agent Runtime 依赖（part1/part2 的 agent/requirements.txt）----
# TODO：待从实测环境 `pip freeze` 导出精确版本后填入并把 requirements.txt 的
# >= 改成 ==。下列为占位，尚未启用（填好后由 requirements.txt 直接引用版本号，
# 或在此集中导出供脚本生成 requirements）。
# export STRANDS_AGENTS_VERSION=""
# export STRANDS_AGENTS_TOOLS_VERSION=""
# export BEDROCK_AGENTCORE_VERSION=""
# export BOTO3_VERSION=""
# export OPENAI_VERSION=""
# export MCP_VERSION=""
export MEM0AI_VERSION="2.0.20"   # 已在 part2 agent/requirements.txt 精确锁定
