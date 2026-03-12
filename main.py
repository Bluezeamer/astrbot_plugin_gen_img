"""AstrBot 图片生成插件入口。

通过 LLM FunctionTool 机制注册图片生成工具，
Agent 推理后调用，插件负责按模型组路由到具体端点。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import aiohttp
from astrbot.api import logger
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core import AstrBotConfig

from .core.config import ModelGroupConfig, PluginConfig
from .core.provider import OpenAICompatibleProvider
from .core.quota import QuotaManager
from .core.router import ProviderRouter
from .core.tool import TOOL_NAME, GenImgTool


@dataclass
class RuntimeModelGroup:
    """模型组运行时实例。"""

    config: ModelGroupConfig
    router: ProviderRouter


@register("astrbot_plugin_gen_img", "用户", "动态模型组图片生成插件", "0.2.0")
class Main(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.raw_config = config
        self.config = PluginConfig.from_dict(config)
        self.session: aiohttp.ClientSession | None = None
        self.runtime_groups: dict[str, RuntimeModelGroup] = {}
        self._tool: GenImgTool | None = None
        self.quota_manager: QuotaManager | None = None

    async def initialize(self):
        """异步初始化：创建 HTTP 会话、装配模型组、注册 LLM 工具。"""
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.config.request.timeout),
            headers={"User-Agent": "astrbot-plugin-gen-img/0.2.0"},
        )

        self.runtime_groups = {}

        # 初始化配额管理器
        if self.config.quota.enabled:
            try:
                quota_db = (
                    Path(StarTools.get_data_dir("astrbot_plugin_gen_img"))
                    / "quota.sqlite3"
                )
                self.quota_manager = QuotaManager(
                    db_path=quota_db,
                    daily_limit=self.config.quota.daily_limit,
                    reset_hour=self.config.quota.reset_hour,
                    whitelist=self.config.quota.whitelist,
                )
                logger.info(
                    f"[gen_img] 用户配额已启用: "
                    f"limit={self.config.quota.daily_limit}, "
                    f"reset={self.config.quota.reset_hour:02d}:00"
                )
            except Exception as exc:
                logger.warning(
                    f"[gen_img] 配额管理器初始化失败，已禁用配额功能: {exc}",
                    exc_info=True,
                )
                self.quota_manager = None
        else:
            self.quota_manager = None

        for group_cfg in self.config.model_groups:
            group_name = group_cfg.group_name
            if not group_name:
                logger.warning("[gen_img] 跳过未命名模型组")
                continue

            if group_name in self.runtime_groups:
                logger.warning(f"[gen_img] 跳过重复模型组: {group_name}")
                continue

            if not (group_cfg.support_img2img or group_cfg.support_txt2img):
                logger.warning(
                    f"[gen_img] 跳过模型组 {group_name}: 未启用任何操作类型"
                )
                continue

            # 按顺序构建端点 Provider 列表
            providers: list[OpenAICompatibleProvider] = []
            for ep in group_cfg.endpoints:
                if not ep.enabled:
                    continue
                ep_name = ep.name or "unnamed"
                providers.append(
                    OpenAICompatibleProvider(
                        name=f"{group_name}/{ep_name}",
                        config=ep,
                        session=self.session,
                        request_config=self.config.request,
                        modalities=group_cfg.modalities,
                        output_config=group_cfg.output_config,
                    )
                )

            if not providers:
                logger.warning(f"[gen_img] 跳过模型组 {group_name}: 无启用端点")
                continue

            self.runtime_groups[group_name] = RuntimeModelGroup(
                config=group_cfg,
                router=ProviderRouter(
                    providers=providers,
                    max_retry=self.config.request.max_retry,
                ),
            )
            ep_names = ", ".join(p.name for p in providers)
            logger.info(f"[gen_img] 已装配模型组 {group_name}: {ep_names}")

        # 仅在有可用模型组时注册 LLM 工具
        if self.runtime_groups:
            self._tool = GenImgTool(plugin=self, quota_manager=self.quota_manager)
            self.context.add_llm_tools(self._tool)

            names = ", ".join(self.runtime_groups.keys())
            logger.info(f"[gen_img] 插件初始化完成，可用模型组: {names}")
        else:
            self._tool = None
            logger.warning("[gen_img] 未发现可用模型组，跳过 LLM 工具注册")

    async def terminate(self):
        """插件卸载：关闭 HTTP 会话、注销 LLM 工具。"""
        if self.quota_manager is not None:
            self.quota_manager.close()
            self.quota_manager = None
            logger.info("[gen_img] 配额管理器已关闭")

        if self.session is not None and not self.session.closed:
            await self.session.close()
            logger.info("[gen_img] HTTP 会话已关闭")

        tool_mgr = self.context.get_llm_tool_manager()
        if tool_mgr.get_func(TOOL_NAME):
            StarTools.unregister_llm_tool(TOOL_NAME)
            logger.info(f"[gen_img] 已注销 LLM 工具 {TOOL_NAME}")

        self.runtime_groups = {}
        self._tool = None
