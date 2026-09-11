"""AgentCore Browser 扩展：Web Bot Auth（自定义 browser）+ Browser Profile。

- Web Bot Auth：减少验证码。签名能力在 browser 资源上开启（部署脚本用
  `create-browser --browser-signing '{"enabled":true}'` 创建），Agent 侧只需
  用该自定义 browser 的 identifier 开会话，请求签名全自动，无需代码配合。
- Browser Profile：跨会话保留 cookies / localStorage（免重复登录、降低反爬
  摩擦）。官方 SDK 的 BrowserClient.start 尚未支持 profileConfiguration
  参数，这里子类化补上，并在会话停止前把状态存回 profile。

上游已知问题：strands-agents-tools 0.8.x 的 AgentCoreBrowser 创建会话后
没有把 client 记入 _client_dict，导致 close/清理时无法真正停止云端会话
（只能等空闲超时），profile 也就没有保存时机。ExtendedAgentCoreBrowser
一并补上。上游修复后本模块可回退为直接使用 AgentCoreBrowser。
"""

import logging
import uuid
from typing import Any, Dict, Optional

from bedrock_agentcore.tools.browser_client import BrowserClient
from playwright.async_api import Browser as PlaywrightBrowser
from strands_tools.browser import AgentCoreBrowser
from strands_tools.browser.models import EvaluateAction, GetTextAction

logger = logging.getLogger(__name__)

# 上游 0.8.x 的 get_text / evaluate 不截断返回内容（get_html 反而截到 1000），
# 电商搜索/详情页整页文本可达几百 KB，一条工具结果就能撑爆模型上下文窗口，
# 且随后的对话摘要自救也会因同一条巨型消息再次溢出，最终 SSE 断流
#（IncompleteRead）。在子类统一钳制这两个无界 action 的返回大小。
MAX_TOOL_RESULT_CHARS = 25_000


def _clamp_result(result: Dict[str, Any], limit: int = MAX_TOOL_RESULT_CHARS) -> Dict[str, Any]:
    for block in result.get("content") or []:
        text = block.get("text")
        if isinstance(text, str) and len(text) > limit:
            block["text"] = (
                text[:limit]
                + f"\n\n[输出过长已截断：原始 {len(text)} 字符，仅保留前 {limit} 字符。"
                "请改用更精确的 CSS 选择器分块提取，或用 evaluate 在页面内先筛出"
                "所需字段再返回精简结果。]"
            )
            logger.info("浏览器工具结果已截断: %d -> %d 字符", len(text), limit)
    return result

# 会话默认分辨率：与门户 Live View 组件的 remoteWidth/remoteHeight 常量对齐，
# 不一致会导致直播画面被裁切或出现黑边。
DEFAULT_VIEWPORT: Dict[str, int] = {"width": 1600, "height": 900}


class ProfileBrowserClient(BrowserClient):
    """支持 browser profile 的会话客户端。"""

    def __init__(self, region: str, profile_id: Optional[str] = None):
        super().__init__(region)
        self.profile_id = profile_id

    def start(
        self,
        identifier: Optional[str] = None,
        name: Optional[str] = None,
        session_timeout_seconds: Optional[int] = 3600,
        viewport: Optional[Dict[str, int]] = None,
    ) -> str:
        # 调用方不指定时固定 1600x900，保证 Live View 画面尺寸可预期
        viewport = viewport or DEFAULT_VIEWPORT

        if not self.profile_id:
            return super().start(
                identifier=identifier,
                name=name,
                session_timeout_seconds=session_timeout_seconds,
                viewport=viewport,
            )

        # 复刻父类请求，追加 profileConfiguration：会话启动时恢复
        # profile 中保存的 cookies / localStorage
        request_params = {
            "browserIdentifier": identifier,
            "name": name or f"browser-session-{uuid.uuid4().hex[:8]}",
            "sessionTimeoutSeconds": session_timeout_seconds,
            "profileConfiguration": {"profileIdentifier": self.profile_id},
        }
        if viewport is not None:
            request_params["viewPort"] = viewport

        response = self.data_plane_client.start_browser_session(**request_params)
        self.identifier = response["browserIdentifier"]
        self.session_id = response["sessionId"]
        logger.info(
            "浏览器会话已启动（携带 profile %s）: %s", self.profile_id, self.session_id
        )
        return self.session_id

    def stop(self) -> bool:
        # 停止前把会话状态存回 profile，供下次会话复用；保存失败不阻断停止
        if self.profile_id and self.session_id and self.identifier:
            try:
                self.data_plane_client.save_browser_session_profile(
                    profileIdentifier=self.profile_id,
                    browserIdentifier=self.identifier,
                    sessionId=self.session_id,
                )
                logger.info("会话状态已存回 profile: %s", self.profile_id)
            except Exception as e:
                logger.warning("保存 browser profile 失败（已忽略）: %s", e)
        return super().stop()


class ExtendedAgentCoreBrowser(AgentCoreBrowser):
    """AgentCoreBrowser + profile 支持 + 会话可被真正停止（见模块 docstring）。"""

    def __init__(
        self,
        region: Optional[str] = None,
        identifier: Optional[str] = None,
        session_timeout: int = 3600,
        profile_id: Optional[str] = None,
    ):
        super().__init__(
            region=region, identifier=identifier, session_timeout=session_timeout
        )
        self.profile_id = profile_id

    # get_text / evaluate 是仅有的两个无界大输出 action（见模块顶部说明）；
    # 上游按 `self.get_text(action)` 实例方法分发，子类覆写即可全量拦截
    def get_text(self, action: GetTextAction) -> Dict[str, Any]:
        return _clamp_result(super().get_text(action))

    def evaluate(self, action: EvaluateAction) -> Dict[str, Any]:
        return _clamp_result(super().evaluate(action))

    async def create_browser_session(self) -> PlaywrightBrowser:
        if not self._playwright:
            raise RuntimeError("Playwright not initialized")

        session_client = ProfileBrowserClient(
            region=self.region, profile_id=self.profile_id
        )
        session_id = session_client.start(
            identifier=self.identifier,
            session_timeout_seconds=self.session_timeout,
        )
        # 记入 _client_dict，close/清理时 close_platform 才会停止云端会话
        # 并触发 ProfileBrowserClient.stop 里的 profile 保存
        self._client_dict[session_id] = session_client

        cdp_url, cdp_headers = session_client.generate_ws_headers()
        return await self._playwright.chromium.connect_over_cdp(
            endpoint_url=cdp_url, headers=cdp_headers
        )
