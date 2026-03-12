"""AstrBot 图片生成插件入口。

通过 @llm_tool() 装饰器注册图片生成工具，
Agent 推理后调用，插件负责按模型组路由到具体端点。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import astrbot.api.message_components as Comp
from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core import AstrBotConfig
from astrbot.core.message.message_event_result import MessageChain

from .core.config import ModelGroupConfig, PluginConfig
from .core.image_extract import extract_image_refs_from_event, resolve_image_refs
from .core.provider import OpenAICompatibleProvider
from .core.quota import QuotaExhausted, QuotaManager
from .core.router import ProviderRouter


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
        self.quota_manager: QuotaManager | None = None

    async def initialize(self):
        """异步初始化：创建 HTTP 会话并装配模型组。"""
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

        if self.runtime_groups:
            names = ", ".join(self.runtime_groups.keys())
            logger.info(f"[gen_img] 插件初始化完成，可用模型组: {names}")
        else:
            logger.warning("[gen_img] 未发现可用模型组，图片生成工具当前不可用")

    async def terminate(self):
        """插件卸载：关闭配额管理器和 HTTP 会话。"""
        if self.quota_manager is not None:
            self.quota_manager.close()
            self.quota_manager = None
            logger.info("[gen_img] 配额管理器已关闭")

        if self.session is not None and not self.session.closed:
            await self.session.close()
            logger.info("[gen_img] HTTP 会话已关闭")

        self.runtime_groups = {}

    # ── 图片解析辅助方法 ──

    async def _resolve_images(
        self,
        event: AstrMessageEvent,
        raw_image_urls: Any,
    ) -> list[tuple[str, str]]:
        """提取并解析图片引用，仅在 img2img 时调用。"""
        if self.session is None:
            return []

        # 规范化 image_urls 参数：只接受字符串，过滤 None/dict 等非法项
        if not raw_image_urls:
            image_refs: list[str] = []
        elif isinstance(raw_image_urls, str):
            ref = raw_image_urls.strip()
            image_refs = [ref] if ref else []
        elif isinstance(raw_image_urls, (list, tuple)):
            image_refs = [
                str(url).strip()
                for url in raw_image_urls
                if isinstance(url, str) and url.strip()
            ]
        else:
            image_refs = []

        # Agent 传了图片路径：直接解析
        if image_refs:
            logger.info(f"[gen_img] Agent 传入 {len(image_refs)} 张图片引用")
            return await resolve_image_refs(
                refs=image_refs,
                session=self.session,
                image_config=self.config.image,
                timeout=self.config.request.timeout,
            )

        # 未传图片：fallback 从消息事件中提取
        if self.config.fallback_to_event_images:
            event_refs = extract_image_refs_from_event(
                event,
                allow_reply=self.config.image.allow_reply_image,
            )
            if event_refs:
                logger.info(
                    f"[gen_img] 从消息事件中提取到 {len(event_refs)} 张图片"
                )
                return await resolve_image_refs(
                    refs=event_refs,
                    session=self.session,
                    image_config=self.config.image,
                    timeout=self.config.request.timeout,
                )

        return []

    # ── LLM 工具 ──

    @llm_tool(name="gen_img")
    async def gen_img(
        self,
        event: AstrMessageEvent,
        model_group: str = "",
        prompt: str = "",
        operation: str = "",
        image_urls: list = None,
    ) -> str:
        '''图片生成工具：基于提示词和可选的参考图片生成新图片。使用流程：1) 传 model_group 不传 prompt 获取该模型组的 prompt 构建指南；2) 根据指南构建 prompt 后再次调用生成图片。多模型组时必须先指定 model_group。调用后工具会直接将图片发送给用户。

        Args:
            model_group(string): 模型组名称，多模型组时必须指定，单模型组时可省略
            prompt(string): 图片生成指令，不传时返回所选模型组的 prompt 构建指南
            operation(string): 操作类型，img2img 或 txt2img，不传时使用模型组默认操作
            image_urls(array[string]): 参考图片的路径或 URL 列表，仅 img2img 模式需要
        '''
        try:
            return await self._do_gen_img(event, model_group, prompt,
                                          operation, image_urls)
        except Exception as exc:
            logger.error(f"[gen_img] tool 调用异常: {exc}", exc_info=True)
            return f"图片生成过程中发生内部错误：{exc}"

    async def _do_gen_img(
        self,
        event: AstrMessageEvent,
        model_group: str,
        prompt: str,
        operation: str,
        image_urls: Any,
    ) -> str:
        """gen_img 工具的核心执行逻辑，与装饰器方法分离以保持异常边界清晰。"""
        if self.session is None or not self.runtime_groups:
            return "插件运行资源尚未就绪，请稍后再试。"

        runtime_groups = self.runtime_groups

        # ── 第一步：解析 model_group ──
        group_name = str(model_group).strip()
        if not group_name:
            if len(runtime_groups) == 1:
                group_name = next(iter(runtime_groups))
            else:
                available = "、".join(runtime_groups.keys())
                return (
                    f"有多个模型组可用，请通过 model_group 参数指定: {available}"
                )

        rtg = runtime_groups.get(group_name)
        if rtg is None:
            available = "、".join(runtime_groups.keys())
            return f"模型组 '{group_name}' 不存在。可用模型组: {available}"

        group_cfg = rtg.config
        prompt = str(prompt).strip()

        # ── 第二步：无 prompt → 返回 guide ──
        if not prompt:
            if group_cfg.guide:
                return (
                    f"【模型组 '{group_name}' 的 prompt 构建指南】\n\n"
                    f"{group_cfg.guide}\n\n"
                    "请根据以上指南构建 prompt 后，再次调用本工具并传入 prompt 参数。"
                )
            return (
                f"模型组 '{group_name}' 没有配置 prompt 构建指南，"
                "请直接传入 prompt 调用。"
            )

        # ── 第三步：解析 operation 并校验 ──
        operation = str(operation).strip()
        if not operation:
            operation = group_cfg.default_operation

        supported_ops: set[str] = set()
        if group_cfg.support_img2img:
            supported_ops.add("img2img")
        if group_cfg.support_txt2img:
            supported_ops.add("txt2img")

        if operation not in supported_ops:
            return (
                f"模型组 '{group_name}' 不支持操作 '{operation}'。"
                f"支持的操作: {', '.join(sorted(supported_ops))}"
            )

        # ── 第四步：根据 operation 处理图片 ──
        images: list[tuple[str, str]] = []
        if operation == "img2img":
            images = await self._resolve_images(event, image_urls)
            if not images:
                return (
                    "图生图操作需要参考图片。"
                    "请通过 image_urls 传入图片路径，或确保消息中附带了图片。"
                )
        # txt2img: 不提取图片，即使消息中有也忽略

        # ── 配额预扣（所有本地校验通过后、实际生成前）──
        quota_user_id: str | None = None
        quota_used = 0
        quota_limit = 0
        quota_date_key = ""
        if self.quota_manager is not None:
            quota_user_id = str(event.get_sender_id()).strip()
            try:
                quota_used, quota_limit, quota_date_key = (
                    await self.quota_manager.try_acquire(quota_user_id)
                )
            except QuotaExhausted as exc:
                return (
                    f"你今日的图片生成额度已用完（{exc.used}/{exc.limit} 次）。"
                    f"配额将在每天 {self.config.quota.reset_hour:02d}:00 重置。"
                )

        logger.info(
            f"[gen_img] tool 调用 group={group_name} operation={operation} "
            f"input_images={len(images)} prompt_len={len(prompt)}"
        )

        # ── 第五步：调用路由生成并发送 ──
        # 预扣后的所有操作包在 try/finally 中，失败时统一回退配额
        generation_ok = False
        try:
            result = await rtg.router.generate(prompt, images)

            if result.error or not result.images:
                return result.error or "图片生成失败，未返回图片结果。"

            chain: list = [Comp.Reply(id=event.message_obj.message_id)]
            chain.extend(Comp.Image.fromBase64(b64) for _, b64 in result.images)
            if result.text:
                chain.append(Comp.Plain(result.text))
            await event.send(MessageChain(chain=chain))
            generation_ok = True
        finally:
            if (
                not generation_ok
                and self.quota_manager is not None
                and quota_user_id is not None
            ):
                try:
                    await self.quota_manager.refund(
                        quota_user_id,
                        quota_date_key,
                    )
                except Exception as exc:
                    logger.error(
                        f"[gen_img] 配额回退失败 user={quota_user_id}: {exc}",
                        exc_info=True,
                    )

        # 构造配额信息（配额已在 try_acquire 中预扣）
        quota_note = ""
        if self.quota_manager is not None and quota_user_id is not None:
            if quota_limit < 0:
                quota_note = "（白名单用户，不受配额限制）"
            else:
                remaining = max(quota_limit - quota_used, 0)
                quota_note = f"（今日剩余额度: {remaining}/{quota_limit}）"

        detail = f"（模型组: {group_name}"
        if result.provider_used:
            detail += f", 提供商: {result.provider_used}"
        detail += "）"
        return (
            f"图片生成完成，已发送给用户{detail}。{quota_note}"
            "请直接继续回复用户，禁止再次调用 gen_img。"
        )
