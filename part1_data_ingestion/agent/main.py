"""Part 1 数据摄入 Agent —— 部署到 Amazon Bedrock AgentCore Runtime。

架构说明（对应 AgentCore 各组件）：
- Runtime          : 本文件通过 BedrockAgentCoreApp 封装为 Runtime 入口，
                     由 `agentcore configure && agentcore launch` 部署。
- Browser 内置工具  : 云端托管浏览器，负责爬取电商页面（默认 eBay 英国站；
                     亚马逊被云端 IP 封锁，需代理，见 README）。启用了两项能力：
                     Web Bot Auth（请求签名，减少验证码，由自定义 browser 资源
                     承载）与 Browser Profile（跨会话保留 cookies/localStorage），
                     见 tools/browser_ext.py。
- Code Interpreter : AgentCoreCodeInterpreter —— 安全沙箱，负责选品经济测算。
- Skill 插件       : 业务 SOP（选品流程）以 SKILL.md 形式放在 skills/ 目录，
                     通过 Strands 的 AgentSkills 插件加载。新增业务场景只需
                     新增 skill 目录，Agent 代码零改动。
- 自定义工具       : tools/aurora_tools.py 调写库 Lambda（挂 Aurora 同 VPC，
                     pg8000 直连）把判定结果写入 Aurora Serverless
                     （PostgreSQL）。Runtime 自身无需挂 VPC，也不接触数据库
                     凭证；不用 RDS Data API 是为了中国区可移植，见该文件。

调用方式（部署后）：
    agentcore invoke '{"prompt": "从 eBay 英国站搜索 desk organizer 选品，处理前 3 个非广告商品"}'
"""

import asyncio
import os
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.agent.conversation_manager import SummarizingConversationManager
from strands.models.openai import OpenAIModel
from strands_tools.code_interpreter import AgentCoreCodeInterpreter

from tools.aurora_tools import save_candidate, save_verdict, set_current_run_id
from tools.browser_ext import ExtendedAgentCoreBrowser

# ---- 配置（可通过环境变量覆盖） ----
REGION = os.environ.get("AWS_REGION", "us-west-2")
MODEL_ID = os.environ.get("MODEL_ID", "openai.gpt-5.6-terra")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://bedrock-mantle.us-east-1.api.aws/openai/v1")
# 模型认证 key：优先 OPENAI_API_KEY，其次 AWS_BEARER_TOKEN_BEDROCK
#（Bedrock API key 的标准环境变量）。key 必填——IAM 铸 token 分支已删除
#（中国区无 Bedrock，统一为标准 OpenAI 兼容端点 + key 形态）
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY") or os.environ.get(
    "AWS_BEARER_TOKEN_BEDROCK"
)
# 自定义 browser（开启 Web Bot Auth）的 ID，由 deploy.sh 创建并注入；
# 不设置则退回默认系统浏览器（无请求签名）
BROWSER_IDENTIFIER = os.environ.get("BROWSER_IDENTIFIER", "aws.browser.v1")
# browser profile 的 ID，由 deploy.sh 创建并注入；不设置则不做跨会话状态复用
BROWSER_PROFILE_ID = os.environ.get("BROWSER_PROFILE_ID","data_default_profile")
SKILLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")

# ---- Runtime 入口：HTTP 服务封装（/invocations、/ping 由 SDK 自动提供） ----
app = BedrockAgentCoreApp()


def create_browser() -> ExtendedAgentCoreBrowser:
    """创建浏览器工具封装（底层是 AgentCore 托管的 Chromium + CDP，会话隔离）。

    identifier 指向开启了 Web Bot Auth 的自定义 browser（请求自动签名，
    减少验证码）；profile_id 让会话启动时恢复上次保存的 cookies/localStorage，
    结束时存回，跨会话免重复登录。
    """
    return ExtendedAgentCoreBrowser(
        region=REGION,
        identifier=BROWSER_IDENTIFIER,
        profile_id=BROWSER_PROFILE_ID,
    )


def create_agent(browser: ExtendedAgentCoreBrowser | None = None) -> Agent:
    """组装数据摄入 Agent。

    注意 Agent 本体只有"通用能力"（浏览器/沙箱/入库），业务流程全部来自 Skill。
    """
    browser = browser or create_browser()

    # AgentCore Code Interpreter：沙箱代码执行（经济测算用）
    code_interpreter = AgentCoreCodeInterpreter(region=REGION)

    # 模型统一走标准 OpenAI 兼容 Chat Completions：端点 + key + 模型名三元组。
    # 全球区默认 bedrock-mantle（/chat/completions 已实测可用，key 用 Bedrock
    # API key）；中国区无 Bedrock，export 区内模型服务商三元组即可。
    # IAM 铸 token 分支已删除（CN 无等价物），key 必填。
    if not OPENAI_API_KEY:
        raise RuntimeError(
            "缺少模型 API key：请设置 OPENAI_API_KEY（或 AWS_BEARER_TOKEN_BEDROCK，"
            "使用 Bedrock API key 时），并确认 OPENAI_BASE_URL / MODEL_ID 与之配套"
        )
    # reasoning_effort：gpt-5.6-terra 经 /chat/completions 使用函数工具时必须
    # 显式置 none（端点 400 明示，实测）。自定义端点缺省不发该参数（严格实现
    # 可能拒绝未知参数），需要时用 MODEL_REASONING_EFFORT 显式指定
    model_params: dict = {}
    reasoning = os.environ.get("MODEL_REASONING_EFFORT")
    if reasoning:
        model_params["reasoning_effort"] = reasoning
    elif "bedrock-mantle" in OPENAI_BASE_URL:
        model_params["reasoning_effort"] = "none"
    model = OpenAIModel(
        model_id=MODEL_ID,
        client_args={"api_key": OPENAI_API_KEY, "base_url": OPENAI_BASE_URL},
        params=model_params or None,
    )

    # AgentSkills 插件：加载 skills/ 目录下所有含 SKILL.md 的子目录。
    # 兼容说明：新版 strands 顶层导出 AgentSkills；旧版在 vended_plugins 下。
    try:
        from strands import AgentSkills
    except ImportError:
        from strands.vended_plugins.skills import AgentSkills

    skills_plugin = AgentSkills(skills=SKILLS_DIR)

    return Agent(
        model=model,
        # 长任务（逐个商品爬页）工具结果体积大，容易撑爆上下文窗口；
        # 溢出时把最旧的 50% 消息压缩成摘要，而非直接截断丢弃
        conversation_manager=SummarizingConversationManager(summary_ratio=0.5),
        plugins=[skills_plugin],
        tools=[
            browser.browser,                      # 浏览器工具
            code_interpreter.code_interpreter,    # 沙箱代码执行工具
            save_verdict,                         # 判定入库
            save_candidate,                       # 候选档案入库
        ],
        system_prompt=(
            "你是一个数据摄入（data ingestion）Agent，运行在 Amazon Bedrock "
            "AgentCore Runtime 中。你的通用能力：\n"
            "1. browser 工具：云端浏览器，用于爬取网页数据；\n"
            "2. code_interpreter 工具：安全沙箱，用于执行计算和数据处理；\n"
            "3. save_verdict / save_candidate 工具：把结果写入 Aurora 数仓。\n\n"
            "具体的业务流程（做什么、怎么判定、什么门槛）由已加载的 Skill 定义。"
            "收到任务后，先匹配最相关的 Skill 并严格按其规则执行。"
            "Skill 中的门槛与公式是硬性规则，不得放宽；抓不到的数据如实标注，"
            "绝不编造。全程用中文向用户汇报。"
        ),
    )


def _browser_session_event(browser: ExtendedAgentCoreBrowser) -> dict | None:
    """若云端浏览器会话已建立，返回一条 browser_session 事件，否则返回 None。

    浏览器会话是模型第一次调 browser 工具时才惰性创建的，所以只能在流式循环里
    轮询检查 _client_dict（见 tools/browser_ext.py 会话入册的说明）。
    browser_id / session_id 是门户生成 Live View presigned URL 的必要参数。
    """
    for client in list(getattr(browser, "_client_dict", {}).values()):
        browser_id = getattr(client, "identifier", None)
        session_id = getattr(client, "session_id", None)
        if browser_id and session_id:
            return {
                "event": "browser_session",
                "browser_id": browser_id,
                "session_id": session_id,
            }
    return None


@app.entrypoint
async def invoke(payload: dict):
    """Runtime 调用入口。

    payload 约定:
        prompt: 任务指令，例如
                "从 eBay 英国站搜索 desk organizer 选品，处理前 3 个非广告商品"
        run_id: 可选。Part 2 门户任务管理模块传入的运行 ID，用于
                ① 入库记录打上 run_id（见 tools/aurora_tools.py）；
                ② 额外输出结构化进度事件供 runner Lambda 落库。
                命令行调用（agentcore invoke / local_test）不传，行为与原先一致。

    以流式方式返回 Agent 的执行过程（AgentCore Runtime 支持 SSE 流式响应）。
    带 run_id 时流里混有两种结构化事件（dict，Runtime 会序列化为 JSON）：
        {"event": "tool_use", "name": ..., "tool_use_id": ...}
        {"event": "browser_session", "browser_id": ..., "session_id": ...}
    纯文本增量仍是裸字符串，两者的契约与 runner Lambda 约定，不要随意改动。
    """
    prompt = payload.get("prompt")
    if not prompt:
        yield {"error": "缺少 prompt 字段，请提供任务指令"}
        return

    # 运行 ID 写入 contextvar，入库工具据此给记录打标（无则写 NULL）
    run_id = payload.get("run_id") or None
    set_current_run_id(run_id)

    # 每次调用新建 Agent 实例，避免跨请求共享会话状态
    #（Runtime 本身按 session 隔离 microVM，这里再保证进程内无状态）
    browser = create_browser()
    agent = create_agent(browser)

    seen_tool_use_ids: set[str] = set()
    browser_session_reported = False
    next_event: asyncio.Task | None = None

    try:
        stream = agent.stream_async(prompt).__aiter__()
        while True:
            if next_event is None:
                next_event = asyncio.ensure_future(stream.__anext__())

            # 浏览器会话是模型第一次调 browser 工具时才惰性创建的，而流事件可能
            # 被上游整体缓冲到运行末尾才集中到达（实测出现过全程静默、事件挤在
            # 最后 1 秒的情况）；若上报只挂在流事件上，门户的 Live View 要到
            # 运行结束前才可见。这里在等流事件的间隙每秒独立轮询一次，
            # 会话一建立立刻上报，与流事件是否到达解耦。
            if run_id and not browser_session_reported:
                done, _ = await asyncio.wait({next_event}, timeout=1.0)
                session_event = _browser_session_event(browser)
                if session_event:
                    browser_session_reported = True
                    yield session_event
                if not done:
                    continue

            try:
                event = await next_event
            except StopAsyncIteration:
                next_event = None
                break
            next_event = None

            # 结构化进度事件只在门户任务模式（有 run_id）下发，避免影响 CLI 输出
            if run_id:
                tool_use = event.get("current_tool_use") or {}
                tool_use_id = tool_use.get("toolUseId")
                # 同一次工具调用会持续推 input 增量，按 toolUseId 去重只发一次
                if tool_use_id and tool_use_id not in seen_tool_use_ids:
                    seen_tool_use_ids.add(tool_use_id)
                    yield {
                        "event": "tool_use",
                        "name": tool_use.get("name", ""),
                        "tool_use_id": tool_use_id,
                    }

            # 只转发文本增量事件，减小响应体积；需要完整事件可直接 yield event
            if "data" in event:
                yield event["data"]
    finally:
        # 生成器被提前关闭（调用方断流）时，别让挂着的 __anext__ 任务泄漏
        if next_event is not None and not next_event.done():
            next_event.cancel()
        # 停止云端浏览器会话；配置了 BROWSER_PROFILE_ID 时会先把会话状态
        # 存回 profile（见 tools/browser_ext.py），不能依赖模型主动 close
        browser._cleanup()


if __name__ == "__main__":
    # 本地调试：python main.py 后另开终端
    # curl -X POST http://localhost:8080/invocations -d '{"prompt": "..."}'
    app.run()
