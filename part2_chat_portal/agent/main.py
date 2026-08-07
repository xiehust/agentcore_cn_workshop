"""Part 2 数据分析 Chat Agent —— 部署到 Amazon Bedrock AgentCore Runtime。

架构说明（对应 AgentCore 各组件）：
- Runtime          : 本文件通过 BedrockAgentCoreApp 封装为 Runtime 入口，
                     流式（SSE）返回；入站认证配置了 JWT authorizer，
                     只接受 Keycloak 签发、audience=analysis-runtime 的 token
                     （Web 门户用户登录后拿到的 access token）。
- Identity         : 出站认证。通过 OAuth2 credential provider（Keycloak
                     client credentials / M2M）为 Agent 换取访问 Gateway 的
                     token，token 缓存在 AgentCore 的 token vault。
- Gateway          : 把数据查询 Lambda 暴露成 MCP 工具（选品漏斗 / 类目分布 /
                     即席 SQL）。Lambda 挂在 Aurora 同 VPC 内直连集群——
                     Gateway 侧零 VPC egress 配置。
- Code Interpreter : 安全沙箱，对查询结果做统计分析 / 衍生计算。
- Memory（预留）    : 本期不启用 AgentCore Memory（部署时 --disable-memory）。
                     后续计划接入 AWS 自部署的 mem0 方案记住用户分析偏好，
                     接入点见 invoke() 里的标注。

多轮对话：Runtime 按 session 隔离 microVM，同一 session 的请求落在同一进程，
用模块级 dict 按 session_id 保存对话历史即可实现会话内多轮。
"""

import asyncio
import json
import os
import re
import urllib.parse
import urllib.request

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.models.openai_responses import OpenAIResponsesModel
from strands.tools.mcp import MCPClient
from strands_tools.code_interpreter import AgentCoreCodeInterpreter
from mcp.client.streamable_http import streamablehttp_client

# ---- 配置（deploy.sh 注入） ----
REGION = os.environ.get("AWS_REGION", "us-west-2")
MODEL_ID = os.environ.get("MODEL_ID", "openai.gpt-5.6-terra")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://bedrock-mantle.us-east-1.api.aws/openai/v1")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY") or os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
# Gateway 的 MCP 端点 URL
GATEWAY_URL = os.environ.get("GATEWAY_URL", "")
# AgentCore Identity 的 OAuth2 credential provider 名（M2M 取 Gateway token）
IDENTITY_PROVIDER_NAME = os.environ.get("IDENTITY_PROVIDER_NAME", "")
# 本地调试兜底：不经 Identity，直连 Keycloak 换 M2M token
KC_TOKEN_URL = os.environ.get("KC_TOKEN_URL", "")
KC_CLIENT_ID = os.environ.get("KC_CLIENT_ID", "analysis-agent")
KC_CLIENT_SECRET = os.environ.get("KC_CLIENT_SECRET", "")

app = BedrockAgentCoreApp()

SYSTEM_PROMPT = """你是跨境电商选品数据分析助手，运行在 Amazon Bedrock AgentCore Runtime 中，\
帮助用户分析 Part 1 数据摄入 pipeline 入库的选品数据（Aurora PostgreSQL 数仓）。

## 数据背景
选品 Agent 按 SOP 对电商平台（当前默认 eBay 英国站）商品逐个判定，结果两张表：
- selection_verdicts：每个被处理商品一条判定记录，verdict 取值 PASS（过初筛）/ 排除 / \
补采（数据不足）/ 采集（达标入候选池），含价格、月销、产品点（product_score）等字段。
- product_candidates：verdict=采集 的商品完整候选档案（中文命名、竞对最低价、预估售价/月销、\
变体明细 variants(JSONB)、认证、材质、AI 总结等）。

## 你的工具
1. get_selection_funnel —— 固定报表：选品漏斗（判定分布 + 候选入库数）。
2. get_category_distribution —— 固定报表：类目/赛道分布（以搜索关键词近似赛道，注意口径）。
3. run_sql_query —— 开放式即席分析：单条只读 SELECT/WITH。表结构见工具描述。
4. code_interpreter —— 沙箱执行 Python：对查询结果做统计、同环比、分布等衍生计算。

## 输出规范
- 全程中文，结论先行，再给支撑数据；金额单位注意站点（UK 站是英镑 £）。
- 数据结果用 Markdown 表格呈现；数据为空或口径受限时如实说明，绝不编造。
- 适合可视化的结果（漏斗、分布、排名、趋势），在文字结论之后追加一个 chart 代码块，\
门户会渲染成图表，格式（务必是合法 JSON）：
```chart
{"type": "bar", "title": "近30天选品漏斗", "data": [{"label": "处理总数", "value": 18}, {"label": "PASS", "value": 5}]}
```
- 即席分析尽量把计算下推到 SQL（聚合/过滤在库内完成），复杂统计再用 code_interpreter。
"""


def _create_model() -> OpenAIResponsesModel:
    """GPT-5.6 Terra 走 Bedrock 的 OpenAI 兼容端点（bedrock-mantle）。
    优先显式 key（OPENAI_API_KEY / AWS_BEARER_TOKEN_BEDROCK，须是 Bedrock API
    key）；未设置则用 Runtime IAM 角色自动铸 bearer token。"""
    if OPENAI_API_KEY:
        return OpenAIResponsesModel(
            model_id=MODEL_ID,
            client_args={"api_key": OPENAI_API_KEY, "base_url": OPENAI_BASE_URL},
        )
    m = re.match(r"https://bedrock-mantle\.([a-z0-9-]+)\.api\.aws", OPENAI_BASE_URL)
    return OpenAIResponsesModel(
        model_id=MODEL_ID,
        bedrock_mantle_config={"region": m.group(1) if m else REGION},
    )


async def _gateway_token() -> str:
    """换取访问 Gateway 的 M2M token。

    优先走 AgentCore Identity（OAuth2 credential provider，token 缓存在
    token vault，Runtime 内自动带 workload 身份）；本地调试时可以用
    KC_TOKEN_URL + KC_CLIENT_SECRET 直连 Keycloak。
    """
    if IDENTITY_PROVIDER_NAME:
        from bedrock_agentcore.identity.auth import requires_access_token

        @requires_access_token(
            provider_name=IDENTITY_PROVIDER_NAME, scopes=[], auth_flow="M2M"
        )
        async def _fetch(*, access_token: str) -> str:
            return access_token

        return await _fetch()

    if KC_TOKEN_URL and KC_CLIENT_SECRET:
        def _password_grant() -> str:
            body = urllib.parse.urlencode({
                "grant_type": "client_credentials",
                "client_id": KC_CLIENT_ID,
                "client_secret": KC_CLIENT_SECRET,
            }).encode()
            req = urllib.request.Request(
                KC_TOKEN_URL, data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())["access_token"]

        return await asyncio.to_thread(_password_grant)

    raise RuntimeError(
        "未配置 Gateway 认证：需要 IDENTITY_PROVIDER_NAME（推荐）"
        "或 KC_TOKEN_URL + KC_CLIENT_SECRET（本地调试）"
    )


# 会话内多轮对话历史：session_id -> Strands messages
_HISTORY: dict[str, list] = {}
# [Memory 预留] 后续接入 mem0（AWS 自部署）时：invoke 开始前按用户检索
# 长期记忆注入上下文，结束后把本轮对话写回 mem0。本期不实现。


@app.entrypoint
async def invoke(payload: dict, context=None):
    """Runtime 调用入口（流式）。

    payload 约定:
        prompt: 用户的分析请求，例如 "看下最近 30 天的选品漏斗"
    """
    prompt = payload.get("prompt")
    if not prompt:
        yield "缺少 prompt 字段，请提供分析请求"
        return

    session_id = getattr(context, "session_id", None) or "local"

    token = await _gateway_token()
    mcp_client = MCPClient(lambda: streamablehttp_client(
        GATEWAY_URL, headers={"Authorization": f"Bearer {token}"}
    ))
    code_interpreter = AgentCoreCodeInterpreter(region=REGION)

    # MCP 连接的生命周期覆盖整个流式过程（工具调用发生在流式中途）
    with mcp_client:
        gateway_tools = mcp_client.list_tools_sync()
        agent = Agent(
            model=_create_model(),
            tools=[*gateway_tools, code_interpreter.code_interpreter],
            system_prompt=SYSTEM_PROMPT,
            messages=list(_HISTORY.get(session_id, [])),
        )
        async for event in agent.stream_async(prompt):
            if "data" in event:
                yield event["data"]
        _HISTORY[session_id] = agent.messages


if __name__ == "__main__":
    # 本地调试：python main.py 后另开终端
    # curl -N -X POST http://localhost:8080/invocations -d '{"prompt": "..."}'
    app.run()
