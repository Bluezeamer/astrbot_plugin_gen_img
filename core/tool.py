"""LLM FunctionTool 定义：图生图工具。

注册为 AstrBot Agent 可调用的工具。
Agent 负责推理和构造 prompt，并主动传入图片引用（本地路径或 URL）。
本工具负责：
1. 接收 Agent 传入的图片路径/URL，或 fallback 从消息事件中提取
2. 调用提供商生成图片
3. 直接将结果图片发送给用户
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from pydantic import Field
from pydantic.dataclasses import dataclass

from .image_extract import extract_image_refs_from_event, resolve_image_refs

if TYPE_CHECKING:
    from ..main import Main

TOOL_NAME = "gen_img"


@dataclass
class GenImgTool(FunctionTool[AstrAgentContext]):
    plugin: Any = None
    name: str = TOOL_NAME
    # fmt: off
    description: str = (
        "图片生成工具：基于参考图片和提示词生成新图片。"
        "【图片来源】优先使用 image_urls 参数传入图片的本地路径或 URL。"
        "若未传入 image_urls，工具会尝试从当前消息的图片附件中自动提取。"
        "【调用前提】对于图生图操作，必须确保有可用的参考图片。"
        "用户仅用文字描述'上面那张图'等不算有效图片输入。"
        "【prompt 要求】必须是完整、清晰的图片编辑/生成指令，"
        "包含保留对象、修改内容、目标风格、色彩氛围等信息。"
        "【调用后】工具会直接将图片发送给用户。成功后，"
        "必须直接继续文字回复用户，严禁重复调用本工具。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "完整的图片生成指令。需明确描述：保留的主体、"
                        "需要修改的内容、目标风格、色彩和光影氛围。"
                        "即使基于预设模板，也必须填入完整的描述内容。"
                    ),
                },
                "image_urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "参考图片的路径或 URL 列表。支持本地文件路径、"
                        "HTTP/HTTPS URL。Agent 应主动从对话上下文中"
                        "获取图片路径后传入此参数。"
                    ),
                },
                "operation": {
                    "type": "string",
                    "description": (
                        "操作类型，默认 img2img。可选值：img2img。"
                        "后续版本将支持更多操作类型。"
                    ),
                    "default": "img2img",
                },
            },
            "required": ["prompt"],
        }
    )
    # fmt: on

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],  # type: ignore
        **kwargs,
    ) -> ToolExecResult:
        try:
            return await self._do_call(context, **kwargs)
        except Exception as exc:
            logger.error(f"[gen_img] tool 调用异常: {exc}", exc_info=True)
            return f"图片生成过程中发生内部错误：{exc}"

    async def _do_call(
        self,
        context: ContextWrapper[AstrAgentContext],  # type: ignore
        **kwargs,
    ) -> ToolExecResult:
        if self.plugin is None:
            return "插件尚未初始化完成，请稍后再试。"

        plugin: Main = self.plugin
        event: AstrMessageEvent = context.context.event  # type: ignore
        prompt = str(kwargs.get("prompt", "")).strip()
        operation = str(kwargs.get("operation", "img2img")).strip() or "img2img"

        if not prompt:
            return "prompt 不能为空，请提供明确的图片生成要求。"

        if operation not in {"img2img", "txt2img"}:
            return f"不支持的操作类型 '{operation}'，当前仅支持 img2img。"

        if plugin.session is None or plugin.router is None:
            return "插件运行资源尚未就绪，请稍后再试。"

        # ── 解析图片来源 ──
        raw_image_urls = kwargs.get("image_urls") or []
        if isinstance(raw_image_urls, str):
            raw_image_urls = [raw_image_urls]
        elif not isinstance(raw_image_urls, list):
            raw_image_urls = list(raw_image_urls)
        image_refs = [str(u).strip() for u in raw_image_urls if str(u).strip()]

        # Agent 传了图片路径：解析为 (mime, base64)
        if image_refs:
            logger.info(f"[gen_img] Agent 传入 {len(image_refs)} 张图片引用")
            images = await resolve_image_refs(
                refs=image_refs,
                session=plugin.session,
                image_config=plugin.config.image,
                timeout=plugin.config.request.timeout,
            )
        # Agent 未传图片：fallback 从消息事件中提取
        elif plugin.config.fallback_to_event_images:
            event_refs = extract_image_refs_from_event(
                event, allow_reply=plugin.config.image.allow_reply_image
            )
            if event_refs:
                logger.info(f"[gen_img] 从消息事件中提取到 {len(event_refs)} 张图片")
                images = await resolve_image_refs(
                    refs=event_refs,
                    session=plugin.session,
                    image_config=plugin.config.image,
                    timeout=plugin.config.request.timeout,
                )
            else:
                images = []
        else:
            images = []

        if operation == "img2img" and not images:
            return (
                "当前没有可用的参考图片。"
                "请通过 image_urls 传入图片路径，或确保消息中附带了图片。"
            )

        logger.info(
            f"[gen_img] tool 调用 operation={operation} "
            f"input_images={len(images)} prompt_len={len(prompt)}"
        )

        # 调用路由生成
        result = await plugin.router.generate(prompt, images)

        if result.error or not result.images:
            return result.error or "图片生成失败，未返回图片结果。"

        # 构建消息链并发送
        chain: list = [Comp.Reply(id=event.message_obj.message_id)]
        chain.extend(Comp.Image.fromBase64(b64) for _, b64 in result.images)
        if result.text:
            chain.append(Comp.Plain(result.text))
        await event.send(MessageChain(chain=chain))

        provider_info = f"（提供商：{result.provider_used}）" if result.provider_used else ""
        return (
            f"图片生成完成，已发送给用户{provider_info}。"
            "请直接继续回复用户，禁止再次调用 gen_img。"
        )
