"""AstrBot 图片生成插件入口。

通过 @llm_tool() 装饰器注册图片生成工具，
Agent 推理后调用，插件负责按模型组路由到具体端点。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import astrbot.api.message_components as Comp
from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core import AstrBotConfig
from astrbot.core.message.message_event_result import MessageChain

from .core.config import ModelGroupConfig, PluginConfig
from .core.image_extract import extract_image_refs_from_event, resolve_image_refs
from .core.provider import OpenAICompatibleProvider
from .core.quota import QuotaExhausted, QuotaManager
from .core.router import ProviderRouter


# 系统提示词注入块标记，用于去重检测
_SYSTEM_HINT_MARKER = "<!-- gen_img_hint -->"

# 模型组描述最大字符数（注入到系统提示词时截断）
_DESC_MAX_LEN = 50


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
                    request_timeout=self.config.request.timeout,
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

    def _new_deadline(self) -> float:
        """为一次图片生成流程创建 monotonic deadline。"""
        return asyncio.get_running_loop().time() + self.config.request.timeout

    # ── 图片解析辅助方法 ──

    async def _resolve_images(
        self,
        event: AstrMessageEvent,
        raw_image_urls: Any,
        deadline: float | None = None,
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

        # 计算图片下载可用的超时：取 deadline 剩余和配置超时的较小值
        if deadline is not None:
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            dl_timeout = min(self.config.request.timeout, remaining)
        else:
            dl_timeout = self.config.request.timeout

        # Agent 传了图片路径：直接解析
        if image_refs:
            logger.info(f"[gen_img] Agent 传入 {len(image_refs)} 张图片引用")
            return await resolve_image_refs(
                refs=image_refs,
                session=self.session,
                image_config=self.config.image,
                timeout=dl_timeout,
                deadline=deadline,
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
                    timeout=dl_timeout,
                    deadline=deadline,
                )

        return []

    def _build_system_hint(self) -> str:
        """构建注入到 system_prompt 的精简模型组提示（供 Agent 决策用）。

        与 _build_groups_overview() 不同：本方法输出更短、面向每轮预注入；
        _build_groups_overview() 是工具调用失败时的详细兜底文案。
        """
        lines: list[str] = [
            _SYSTEM_HINT_MARKER,
            "# 图片生成工具 gen_img — 可用模型组",
            "重要：model_group 名称必须从下列列表原样使用，不要猜测或改写。",
        ]

        for gname, rtg in self.runtime_groups.items():
            cfg = rtg.config
            ops = []
            if cfg.support_txt2img:
                ops.append("txt2img")
            if cfg.support_img2img:
                ops.append("img2img")

            entry = (
                f"- {gname} "
                f"[支持: {', '.join(ops)}; 默认: {cfg.default_operation}]"
            )
            # 描述：有内容则截断追加，无则省略
            desc = " ".join((cfg.group_description or "").split()).strip()
            if desc:
                if len(desc) > _DESC_MAX_LEN:
                    desc = desc[: _DESC_MAX_LEN - 1].rstrip() + "…"
                entry += f" {desc}"
            lines.append(entry)

        lines.append(
            "调用规则："
            "无参考图 → 优先选择支持 txt2img 的模型组；"
            "有参考图/基于原图修改 → 优先选择支持 img2img 的模型组。"
        )
        lines.append(
            "如需详细 prompt 写法，可只传 model_group 不传 prompt 以获取该组 guide。"
        )
        hint = "\n".join(lines)
        # 安全阀：极端情况下（模型组很多）截断到 800 字符
        if len(hint) > 800:
            hint = hint[:799].rstrip() + "…"
        return hint

    def _build_groups_overview(self) -> str:
        """构建模型组概览文本，帮助 Agent 选择 model_group 和 operation。"""
        lines = ["可用模型组（请通过 model_group 参数指定）："]

        for gname, rtg in self.runtime_groups.items():
            cfg = rtg.config
            ops: list[str] = []
            if cfg.support_txt2img:
                ops.append("txt2img")
            if cfg.support_img2img:
                ops.append("img2img")
            desc = cfg.group_description or "未配置描述"
            lines.append(
                f"  - {gname}: {desc} "
                f"[支持: {', '.join(ops)}; 默认: {cfg.default_operation}]"
            )

        lines.append("")
        lines.append("调用说明：")
        lines.append(
            "  - 纯文字描述生成图片 → operation=\"txt2img\"（无需 image_urls）"
        )
        lines.append(
            "  - 基于参考图片生成 → operation=\"img2img\""
            "（优先传 image_urls，未传时尝试读取消息附图）"
        )
        lines.append(
            "  - 不传 operation 时使用该模型组的默认操作，"
            "请注意默认操作是否符合用户意图"
        )
        lines.append(
            "  - 如果用户没有提供参考图片，应使用支持 txt2img 的模型组"
        )
        return "\n".join(lines)

    def _build_group_info(self, group_name: str, cfg: ModelGroupConfig) -> str:
        """构建单个模型组的操作信息摘要，仅列出实际支持的操作。"""
        ops: list[str] = []
        if cfg.support_txt2img:
            ops.append("txt2img")
        if cfg.support_img2img:
            ops.append("img2img")
        desc = cfg.group_description or "未配置描述"
        lines = [
            f"【模型组 '{group_name}' 信息】",
            f"  - 描述: {desc}",
            f"  - 支持操作: {', '.join(ops)}",
            f"  - 默认操作: {cfg.default_operation}",
        ]
        if cfg.support_txt2img:
            lines.append('  - 纯文本生图请传 operation="txt2img"（无需 image_urls）')
        if cfg.support_img2img:
            lines.append(
                "  - 基于参考图生图请传 operation=\"img2img\"，"
                "优先通过 image_urls 传入图片；未传时会尝试读取消息附图"
            )
        return "\n".join(lines)

    # ── LLM 请求前注入 ──

    @filter.on_llm_request()
    async def inject_tool_hint(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ):
        """在 LLM 请求前注入模型组概览，让 Agent 首次调用即可选对模型组。"""
        if not self.runtime_groups:
            return

        current = str(req.system_prompt or "")
        if _SYSTEM_HINT_MARKER in current:
            return

        hint = self._build_system_hint()
        req.system_prompt = (
            f"{current.rstrip()}\n\n{hint}" if current.strip() else hint
        )

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
        '''图片生成工具：基于提示词和可选的参考图片生成新图片。调用后工具会直接将图片发送给用户。请依据系统提示词中的模型组能力选择正确的 model_group，并结合用户是否提供参考图选择 operation（txt2img 或 img2img）。若未传 model_group 或传错，工具会返回可用模型组列表帮助恢复。不传 prompt 时返回所选模型组的 prompt 构建指南。禁止自行编造图片 URL。

        Args:
            model_group(string): 优先按系统提示词中的模型组名称原样填写；仅有一个模型组时可省略
            prompt(string): 图片生成指令，不传时返回所选模型组的 prompt 构建指南（guide）
            operation(string): 操作类型，img2img（需参考图）或 txt2img（纯文本），不传时使用模型组默认操作
            image_urls(array[string]): 参考图片的路径或 URL 列表，仅 img2img 模式需要
        '''
        try:
            return await self._do_gen_img(event, model_group, prompt,
                                          operation, image_urls)
        except Exception as exc:
            logger.error(f"[gen_img] tool 调用异常: {exc}", exc_info=True)
            return (
                f"图片生成过程中发生内部错误：{exc}\n"
                "禁止自行编造图片 URL 或假装图片已生成，"
                "请如实告知用户生成失败。"
            )

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
        group_name = str(model_group or "").strip()
        if not group_name:
            if len(runtime_groups) == 1:
                group_name = next(iter(runtime_groups))
            else:
                return self._build_groups_overview()

        rtg = runtime_groups.get(group_name)
        if rtg is None:
            return (
                f"模型组 '{group_name}' 不存在。\n"
                f"{self._build_groups_overview()}"
            )

        group_cfg = rtg.config
        prompt = str(prompt or "").strip()

        # ── 第二步：无 prompt → 返回 guide ──
        if not prompt:
            group_info = self._build_group_info(group_name, group_cfg)
            if group_cfg.guide:
                return (
                    f"{group_info}\n\n"
                    f"【prompt 构建指南】\n{group_cfg.guide}\n\n"
                    "请根据以上指南构建 prompt 后，"
                    "再次调用本工具并传入 prompt 和 operation 参数。"
                )
            return (
                f"{group_info}\n\n"
                f"模型组 '{group_name}' 没有配置 prompt 构建指南，"
                "请直接传入 prompt 和 operation 调用。"
            )

        # ── 第三步：解析 operation 并校验 ──
        operation = str(operation or "").strip()
        if not operation:
            operation = group_cfg.default_operation

        supported_ops: set[str] = set()
        if group_cfg.support_img2img:
            supported_ops.add("img2img")
        if group_cfg.support_txt2img:
            supported_ops.add("txt2img")

        if operation not in supported_ops:
            return (
                f"模型组 '{group_name}' 不支持操作 '{operation}'。\n"
                f"{self._build_group_info(group_name, group_cfg)}"
            )

        # ── 第四步：根据 operation 处理图片 ──
        deadline = self._new_deadline()
        images: list[tuple[str, str]] = []
        if operation == "img2img":
            images = await self._resolve_images(event, image_urls, deadline=deadline)
            if not images:
                hint = ""
                if group_cfg.support_txt2img:
                    hint = (
                        "\n如需从纯文本描述生成图片，请改为 "
                        'operation="txt2img" 并去掉 image_urls。'
                    )
                return (
                    "图生图操作需要参考图片。"
                    "请通过 image_urls 传入图片路径，"
                    f"或确保消息中附带了图片。{hint}"
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
            result = await rtg.router.generate(prompt, images, deadline=deadline)

            if result.error or not result.images:
                return (
                    (result.error or "图片生成失败，未返回图片结果。")
                    + "\n禁止自行编造图片 URL 或假装图片已生成，"
                    "请如实告知用户生成失败。"
                )

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
