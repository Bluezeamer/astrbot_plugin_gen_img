"""AstrBot 图片生成插件入口。

通过 LLM FunctionTool 机制注册图生图工具，
Agent 推理后调用，插件负责与 OpenRouter/NewAPI 交互。
"""

from __future__ import annotations

import aiohttp
from astrbot.api import logger
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core import AstrBotConfig

from .core.config import PluginConfig
from .core.provider import OpenAICompatibleProvider
from .core.router import ProviderRouter
from .core.tool import TOOL_NAME, GenImgTool


@register("astrbot_plugin_gen_img", "用户", "NanoBanana2 图生图插件", "0.1.0")
class Main(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.raw_config = config
        self.config = PluginConfig.from_dict(config)
        self.session: aiohttp.ClientSession | None = None
        self.router: ProviderRouter | None = None
        self._tool: GenImgTool | None = None

    async def initialize(self):
        """异步初始化：创建 HTTP 会话、装配提供商、注册 LLM 工具。"""
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.config.request.timeout),
            headers={"User-Agent": "astrbot-plugin-gen-img/0.1.0"},
        )

        # 按优先级装配提供商：openrouter（主） → newapi（备）
        providers: list[OpenAICompatibleProvider] = []

        if self.config.openrouter.enabled:
            providers.append(
                OpenAICompatibleProvider(
                    name="openrouter",
                    config=self.config.openrouter,
                    session=self.session,
                    request_config=self.config.request,
                    image_config=self.config.image,
                    output_config=self.config.default_image_config,
                )
            )

        if self.config.newapi.enabled:
            providers.append(
                OpenAICompatibleProvider(
                    name="newapi",
                    config=self.config.newapi,
                    session=self.session,
                    request_config=self.config.request,
                    image_config=self.config.image,
                    output_config=self.config.default_image_config,
                )
            )

        self.router = ProviderRouter(
            providers=providers,
            max_retry=self.config.request.max_retry,
        )

        # 注册 LLM 工具
        self._tool = GenImgTool(plugin=self)
        self.context.add_llm_tools(self._tool)

        names = ", ".join(p.name for p in providers) or "无"
        logger.info(f"[gen_img] 插件初始化完成，可用提供商: {names}")

    async def terminate(self):
        """插件卸载：关闭 HTTP 会话、注销 LLM 工具。"""
        if self.session is not None and not self.session.closed:
            await self.session.close()
            logger.info("[gen_img] HTTP 会话已关闭")

        tool_mgr = self.context.get_llm_tool_manager()
        if tool_mgr.get_func(TOOL_NAME):
            StarTools.unregister_llm_tool(TOOL_NAME)
            logger.info(f"[gen_img] 已注销 LLM 工具 {TOOL_NAME}")
