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
- Memory（自建 mem0）: 不启用 AgentCore 托管 Memory（部署时 --disable-memory）。
                     长期记忆用自部署 mem0 记住用户分析偏好：会话首轮按用户
                     检索记忆注入 system prompt（跨会话生效），每轮结束把
                     对话交 mem0 提取写回。LLM/embedding 走 OpenAI 兼容端点，
                     向量存 Aurora pgvector（经 VPC 内记忆 Lambda 读写），
                     见 memory_backend.py。

多轮对话：Runtime 按 session 隔离 microVM，同一 session 的请求落在同一进程，
用模块级 dict 按 session_id 保存对话历史即可实现会话内多轮；microVM 回收
（idle 约 15 分钟）后内存历史丢失，因此每轮结束把消息落库 Aurora
（chat_messages 表），下次命中不到内存历史就从库里回灌（见下方持久化助手）。
"""

import asyncio
import json
import os
import urllib.parse
import urllib.request

import boto3
from botocore.config import Config as BotoConfig
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.models.openai import OpenAIModel
from strands.tools.mcp import MCPClient
from strands_tools.code_interpreter import AgentCoreCodeInterpreter
from mcp.client.streamable_http import streamablehttp_client

# 长期记忆（自建 mem0）。模块内部自带降级：mem0 未安装/未配置时 import 不抛错、
# recall/memorize 变成空操作，对话主链路零影响
import memory_backend

# ---- 配置（deploy.sh 注入） ----
REGION = os.environ.get("AWS_REGION", "us-west-2")
# 模型走任意 OpenAI 兼容的 Chat Completions 端点，三元组 env 注入：
# 全球区默认 bedrock-mantle（其 /chat/completions 已实测可用，key 用 Bedrock
# API key）；中国区没有 Bedrock，改 export 区内模型服务商的三元组即可
MODEL_ID = os.environ.get("MODEL_ID", "openai.gpt-5.6-terra")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://bedrock-mantle.us-east-1.api.aws/openai/v1")
# 兼容原部署习惯：AWS_BEARER_TOKEN_BEDROCK（Bedrock API key）可作为 key 来源
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY") or os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
# Gateway 的 MCP 端点 URL
GATEWAY_URL = os.environ.get("GATEWAY_URL", "")
# AgentCore Identity 的 OAuth2 credential provider 名（M2M 取 Gateway token）
IDENTITY_PROVIDER_NAME = os.environ.get("IDENTITY_PROVIDER_NAME", "")
# 本地调试兜底：不经 Identity，直连 Keycloak 换 M2M token
KC_TOKEN_URL = os.environ.get("KC_TOKEN_URL", "")
KC_CLIENT_ID = os.environ.get("KC_CLIENT_ID", "analysis-agent")
KC_CLIENT_SECRET = os.environ.get("KC_CLIENT_SECRET", "")
# 会话历史持久化：经 VPC 内数据面 Lambda（pg8000 直连 Aurora）读写——
# 中国区没有 RDS Data API，Runtime 自身也不挂 VPC，lambda.invoke 是两个分区
# 通用的形态。缺该变量就整体降级为纯内存历史（本地裸跑依赖这一点）
DB_LAMBDA_NAME = os.environ.get("DB_LAMBDA_NAME", "")

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


def _create_model() -> OpenAIModel:
    """标准 OpenAI 兼容 Chat Completions：端点 + key + 模型名三元组。

    统一 OpenAIModel（不再用 Responses API 专属的 OpenAIResponsesModel）——
    两个分区同一形态：全球区指 bedrock-mantle（Bedrock API key），中国区指
    区内模型服务商端点。IAM 铸 token 分支已删除（CN 无等价物），key 必填。
    """
    if not OPENAI_API_KEY:
        raise RuntimeError(
            "缺少模型 API key：请设置 OPENAI_API_KEY（或 AWS_BEARER_TOKEN_BEDROCK，"
            "使用 Bedrock API key 时），并确认 OPENAI_BASE_URL / MODEL_ID 与之配套"
        )
    # reasoning_effort：gpt-5.6-terra 经 /chat/completions 使用函数工具时必须
    # 显式置 none（端点 400 明示，实测）。自定义端点缺省不发该参数（严格实现
    # 可能拒绝未知参数），需要时用 MODEL_REASONING_EFFORT 显式指定
    params: dict = {}
    reasoning = os.environ.get("MODEL_REASONING_EFFORT")
    if reasoning:
        params["reasoning_effort"] = reasoning
    elif "bedrock-mantle" in OPENAI_BASE_URL:
        params["reasoning_effort"] = "none"
    return OpenAIModel(
        model_id=MODEL_ID,
        client_args={"api_key": OPENAI_API_KEY, "base_url": OPENAI_BASE_URL},
        params=params or None,
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


# 会话内多轮对话历史：session_id -> Strands messages（microVM 存活期间的快取）
_HISTORY: dict[str, list] = {}
# 写游标：session_id -> 已落库的消息条数（= 下一条的 seq）
_PERSISTED_SEQ: dict[str, int] = {}
# 长期记忆（自建 mem0，见 memory_backend.py）的会话级缓存：
# session_id -> 注入 system prompt 的记忆块（可为空串）。只在会话首轮 recall
# 一次并缓存整个会话——同一会话内 system prompt 逐字节不变，主模型的
# prompt cache 前缀才能持续命中；每轮 recall 会把缓存全部打穿。跨会话切换时
# 新会话首轮重新检索，用户的长期记忆自然延续。
_MEMORY_SECTION: dict[str, str] = {}
# session_id -> 会话归属用户（chat_sessions.user_name；None=查不到，不再重查）
_SESSION_USER: dict[str, str | None] = {}

# ---- 会话历史持久化（chat_messages 表，经 VPC 内数据面 Lambda） ----
# 全部 best-effort：读失败按空历史继续，写失败只打日志，绝不影响 SSE 流。

# 单条消息 JSON 上限：lambda.invoke 请求 6MB，300KB 留足余量；超了降级存裁剪版
_MAX_CONTENT_BYTES = 300 * 1024
# 数据面 Lambda 的读超时：Aurora 冷恢复的等待在 Lambda 内部（上限 240s），
# 回灌等不起那么久——宁可丢上下文不可挂请求，30 秒等不到就放弃
_DB_READ_TIMEOUT_S = 30

_db_lambda_cache = None


def _db_enabled() -> bool:
    """配了数据面 Lambda 才落库；本地 python main.py 裸跑时退化为纯内存历史。"""
    return bool(DB_LAMBDA_NAME)


def _db_lambda():
    global _db_lambda_cache
    if _db_lambda_cache is None:
        # 禁重试：SQL 层有 ON CONFLICT/主键幂等兜底，但重试只会放大尾延迟
        _db_lambda_cache = boto3.client(
            "lambda", region_name=REGION,
            config=BotoConfig(connect_timeout=5, read_timeout=_DB_READ_TIMEOUT_S,
                              retries={"max_attempts": 0}),
        )
    return _db_lambda_cache


def _p(name: str, value) -> tuple[str, str]:
    """SQL 命名参数（统一转字符串，SQL 里按需 cast——与 Data API 时代同语义）。"""
    return (name, "" if value is None else str(value))


def _db_execute(sql: str, parameters: list) -> list[dict]:
    """执行一条 SQL 并返回行（list[dict]，等价原 Data API formatRecordsAs=JSON）。

    经 lambda.invoke 调 VPC 内数据面 Lambda（pg8000 直连 Aurora，:name 占位符
    原样支持）；集群冷恢复的退避在 Lambda 内部完成，这里只管超时放弃。
    """
    resp = _db_lambda().invoke(
        FunctionName=DB_LAMBDA_NAME,
        Payload=json.dumps(
            # deadline_s < read_timeout(30s)：调用方放弃时 Lambda 必已先放弃
            # 连接、语句从未执行，保住「超时 = 零写入」的原子失败语义
            {"op": "execute", "args": {"sql": sql, "params": dict(parameters), "deadline_s": 25}},
            ensure_ascii=False,
        ).encode("utf-8"),
    )
    body = json.loads(resp["Payload"].read())
    if resp.get("FunctionError") or not isinstance(body, dict) or body.get("status") != "ok":
        raise RuntimeError(f"db lambda execute 失败: {body}")
    return body.get("rows") or []


def _display_text(message: dict) -> str | None:
    """抽取消息里的纯文本（门户回看用）。纯工具轮没有 text block，返回 None。"""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip() or None
    parts = [
        block["text"] for block in (content or [])
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    ]
    return "".join(parts).strip() or None


def _shrink(message: dict) -> dict:
    """超大消息降级：把 toolResult 内容、toolUse 入参、长文本都截断成占位文本，
    但保留 toolUse/toolResult 的配对结构与 toolUseId——拆掉配对会让回灌后的
    第一次调用直接被模型拒掉。"""
    shrunk = json.loads(json.dumps(message, ensure_ascii=False, default=str))
    for block in shrunk.get("content") or []:
        if not isinstance(block, dict):
            continue
        result = block.get("toolResult")
        if isinstance(result, dict):
            result["content"] = [
                {"text": json.dumps(item, ensure_ascii=False, default=str)[:4000]
                         + " …（原始结果过大，已截断）"}
                for item in (result.get("content") or [])
            ]
        use = block.get("toolUse")
        if isinstance(use, dict) and "input" in use:
            raw = json.dumps(use["input"], ensure_ascii=False, default=str)
            if len(raw) > 4000:
                use["input"] = {"truncated": raw[:4000] + " …（原始入参过大，已截断）"}
        if isinstance(block.get("text"), str) and len(block["text"]) > 20000:
            block["text"] = block["text"][:20000] + " …（内容过大已截断）"
    return shrunk


def _has_tool_use(message: dict) -> bool:
    """消息里是否有 toolUse 块（配对的 toolResult 在下一条消息里）。"""
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(block, dict) and "toolUse" in block for block in content)


def _complete_len(messages: list) -> int:
    """可安全回灌的长度：历史必须停在一轮问答的收尾（assistant 的纯文本回复）处。

    逐条落库中途失败会让库里停在一轮的中间——只写进了 user 提问、或者
    assistant 发了 toolUse 但配对的 toolResult 没落库。这种序列塞回模型会被判为
    非法消息序列、整个请求挂掉，那这个会话就再也聊不动了；宁可少回灌一轮上下文。
    """
    n = len(messages)
    while n:
        last = messages[n - 1]
        if last.get("role") == "assistant" and not _has_tool_use(last):
            break
        n -= 1
    return n


def _load_messages(session_id: str) -> list:
    """回灌会话历史：按 seq 取回完整 Strands messages（含工具轮）。"""
    if not _db_enabled():
        return []
    try:
        rows = _db_execute(
            "SELECT seq, content::text AS content FROM chat_messages "
            "WHERE session_id = :session_id ORDER BY seq",
            [_p("session_id", session_id)],
        )
        rows = [r for r in rows if r.get("content")]
        messages = [json.loads(r["content"]) for r in rows]
        keep = _complete_len(messages)
        if keep < len(messages):
            # 把不完整的尾巴一并从库里删掉：写游标是按回灌条数定的，留着这些行
            # 会让后面新消息的 seq 撞上它们（ON CONFLICT DO NOTHING 静默丢弃）。
            # 删不掉也不影响这轮对话，只是这个会话后续暂时写不进新消息
            try:
                _db_execute(
                    "DELETE FROM chat_messages "
                    "WHERE session_id = :session_id AND seq >= :seq::INT",
                    [_p("session_id", session_id), _p("seq", rows[keep]["seq"])],
                )
            except Exception as e:
                print(f"[history] 清理不完整尾巴失败: {type(e).__name__}: {e}")
            print(f"[history] 丢弃 {len(messages) - keep} 条不完整的尾部消息: {session_id}")
            messages = messages[:keep]
        if messages:
            print(f"[history] 已回灌 {len(messages)} 条历史消息: {session_id}")
        return messages
    except Exception as e:
        print(f"[history] 回灌失败（按空历史继续）: {type(e).__name__}: {e}")
        return []


def _persist_new(session_id: str, messages: list) -> None:
    """把游标之后的新消息逐条落库。

    某条写失败就地停下且不推进游标：下一轮从同一位置重试（唯一索引 +
    DO NOTHING 兜住重复），保证库里始终是一段连续的前缀，不出现 seq 空洞。
    """
    if not _db_enabled():
        return
    sql = (
        "INSERT INTO chat_messages (session_id, seq, role, content, display_text) "
        "VALUES (:session_id, :seq::INT, :role, :content::JSONB, "
        "        NULLIF(:display_text, '')) "
        "ON CONFLICT (session_id, seq) DO NOTHING"
    )
    for seq in range(_PERSISTED_SEQ.get(session_id, 0), len(messages)):
        message = messages[seq]
        try:
            content = json.dumps(message, ensure_ascii=False, default=str)
            if len(content.encode("utf-8")) > _MAX_CONTENT_BYTES:
                content = json.dumps(_shrink(message), ensure_ascii=False, default=str)
            if len(content.encode("utf-8")) > _MAX_CONTENT_BYTES:
                # 截断过还是超限（极端情况）。带工具块的消息不能降级成纯文本
                # ——那会拆散 toolUse/toolResult 配对，回灌时反而把会话搞死；
                # 这一轮干脆不落库，停在这里由下次回灌的尾部清理收拾
                if _has_tool_use(message) or any(
                    isinstance(b, dict) and "toolResult" in b
                    for b in (message.get("content") or [])
                ):
                    print(f"[history] 消息过大且含工具块，跳过本轮落库（seq={seq}）")
                    return
                content = json.dumps(
                    {"role": message.get("role") or "user",
                     "content": [{"text": (_display_text(message) or "")[:50_000]
                                          or "（内容过大已省略）"}]},
                    ensure_ascii=False,
                )
            _db_execute(sql, [
                _p("session_id", session_id),
                _p("seq", seq),
                _p("role", (message.get("role") or "user")[:16]),
                _p("content", content),
                _p("display_text", _display_text(message)),
            ])
            _PERSISTED_SEQ[session_id] = seq + 1
        except Exception as e:
            print(f"[history] 落库失败（seq={seq}，下轮重试）: {type(e).__name__}: {e}")
            return


def _memory_user(session_id: str) -> str | None:
    """session -> 用户：查 chat_sessions.user_name。

    该列由 tasks-api Lambda 从已验证的 Keycloak JWT 写入（身份可信）——
    Runtime 自身拿不到用户身份，长期记忆按用户隔离只能走这条反查路径。
    确认无归属（chat_test.sh 的 smoke-* 会话未注册、本地裸跑无库）缓存
    None 不再重查；查询异常（多为集群冷恢复）**不缓存**——流尾 memorize
    再调时集群多半已被本轮对话唤醒，可以补上。best-effort，失败不阻断对话。
    """
    if session_id in _SESSION_USER:
        return _SESSION_USER[session_id]
    if not _db_enabled():
        _SESSION_USER[session_id] = None
        return None
    try:
        rows = _db_execute(
            "SELECT user_name FROM chat_sessions WHERE session_id = :session_id",
            [_p("session_id", session_id)],
        )
    except Exception as e:
        print(f"[mem0] 查询会话归属失败（本轮跳过记忆，之后重试）: {type(e).__name__}: {e}")
        return None
    user = str(rows[0]["user_name"]) if rows and rows[0].get("user_name") else None
    _SESSION_USER[session_id] = user
    return user


def _render_memory_section(memories: list[str]) -> str:
    """把检索到的长期记忆渲染成 system prompt 追加块；无记忆返回空串。"""
    if not memories:
        return ""
    lines = "\n".join(f"- {m}" for m in memories)
    return (
        "\n\n## 用户长期记忆\n"
        "以下是该用户在过去会话中沉淀的分析偏好与事实（按相关度排序）。"
        "回答时自然运用，不要逐条复述，也不要向用户提及记忆系统本身：\n" + lines
    )


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

    # microVM 还活着就用内存历史；被回收（或换了实例）则从库里回灌，
    # 这样"15 分钟没动的会话继续追问"也能接上上下文
    if session_id not in _HISTORY:
        history = await asyncio.to_thread(_load_messages, session_id)
        _HISTORY[session_id] = history
        _PERSISTED_SEQ[session_id] = len(history)

    # 长期记忆：只在会话首轮检索一次（与取 Gateway token 并行，耗时被遮蔽）。
    # 管道 = 反查会话归属 + 语义检索，总预算约 2×MEM0_SEARCH_TIMEOUT；超时/
    # 无记忆缓存空串且不再重试——空也要缓存，保证本会话 system prompt 逐字节
    # 稳定、主模型 prompt cache 前缀不被打穿。非首轮零额外调用
    if session_id not in _MEMORY_SECTION:

        async def _recall_pipeline() -> list[str]:
            if not memory_backend.enabled():
                return []  # 记忆未配置/已熔断：连会话归属查询都省掉
            uid = await asyncio.to_thread(_memory_user, session_id)
            return await memory_backend.recall(uid, prompt)

        try:
            token, memories = await asyncio.gather(
                _gateway_token(),
                asyncio.wait_for(_recall_pipeline(), timeout=memory_backend.SEARCH_TIMEOUT_S * 2),
            )
        except asyncio.TimeoutError:
            # gather 里只有 wait_for 会抛 TimeoutError；token 任务此时已完成/
            # 将完成，重取一次命中 Identity token vault 缓存，代价可忽略
            print("[mem0] 会话首轮记忆管道超时（多为集群冷恢复），本会话无记忆注入")
            token, memories = await _gateway_token(), []
        _MEMORY_SECTION[session_id] = _render_memory_section(memories)
    else:
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
            # 记忆块拼在 SYSTEM_PROMPT 之后，且整个会话固定不变（见 _MEMORY_SECTION）；
            # 不碰 messages，_HISTORY 与 chat_messages 落库零污染
            system_prompt=SYSTEM_PROMPT + _MEMORY_SECTION.get(session_id, ""),
            messages=list(_HISTORY.get(session_id, [])),
        )
        async for event in agent.stream_async(prompt):
            if "data" in event:
                yield event["data"]
        _HISTORY[session_id] = agent.messages
        # 落库放在流结束之后：本身是 best-effort，慢一点也不占用回答时延
        await asyncio.to_thread(_persist_new, session_id, agent.messages)
        # 长期记忆写回：本轮问答交 mem0 做 LLM 事实提取（与既有记忆去重/合并）。
        # 必须 await——microVM 在响应结束后可能被冻结，fire-and-forget 会丢写；
        # 代价是 SSE 流关闭最坏再晚 MEM0_ADD_TIMEOUT（默认 20s，正文已全部
        # 送达，仅前端 busy 态解除变晚；连续失败有熔断兜底）。
        # 归属带补查：首轮 SELECT 可能跑在前端注册会话的 INSERT 之前（两者并发）
        # 而缓存下 None——流尾此刻行早已提交、集群也已被本轮唤醒，弹掉缓存
        # 重查一次，否则整个会话的记忆写入都会静默丢失。真正未注册的会话
        # （smoke-*）每轮只多付一次流后的 best-effort SELECT，不占回答时延。
        # 本会话新沉淀的事实对当前会话不注入（上下文在场），下个新会话生效
        if memory_backend.enabled():
            if _SESSION_USER.get(session_id) is None:
                _SESSION_USER.pop(session_id, None)
            user_id = await asyncio.to_thread(_memory_user, session_id)
            reply_text = _display_text(agent.messages[-1]) if agent.messages else None
            await memory_backend.memorize(user_id, prompt, reply_text or "")


if __name__ == "__main__":
    # 本地调试：python main.py 后另开终端
    # curl -N -X POST http://localhost:8080/invocations -d '{"prompt": "..."}'
    app.run()
