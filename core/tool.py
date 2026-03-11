"""LLM FunctionTool 定义：图片生成工具。

注册为 AstrBot Agent 可调用的工具。
Agent 负责选择模型组、构造 prompt，并按需传入图片引用。
本工具负责：
1. 两阶段调用：无 prompt 返回 guide，有 prompt 执行生成
2. 根据模型组配置选择对应的运行时路由
3. 在 img2img 模式下解析图片引用
4. 调用提供商生成图片并直接发送给用户
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
    description: str = ""
    parameters: dict = Field(default_factory=dict)

    def __post_init__(self) -> None:
        self._rebuild_metadata()

    def _rebuild_metadata(self) -> None:
        """根据当前可用模型组动态生成 description 和 parameters。"""
        runtime_groups = getattr(self.plugin, "runtime_groups", None) or {}
        group_names = list(runtime_groups.keys())

        # ── 构建 description ──
        group_lines: list[str] = []
        has_guide = False
        for gname, rtg in runtime_groups.items():
            cfg = rtg.config
            ops = []
            if cfg.support_img2img:
                ops.append("图生图")
            if cfg.support_txt2img:
                ops.append("文生图")
            ops_str = "/".join(ops) or "未知"
            desc = cfg.group_description or "未填写说明"
            group_lines.append(f"  - {gname}（{ops_str}）: {desc}")
            if cfg.guide:
                has_guide = True

        groups_desc = "\n".join(group_lines) if group_lines else "  当前没有可用模型组。"

        desc_parts = [
            "图片生成工具：基于提示词和可选的参考图片生成新图片。",
            f"可用模型组：\n{groups_desc}",
        ]
        if has_guide:
            desc_parts.append(
                "首次使用某模型组时，可只传 model_group 不传 prompt，"
                "获取该模型的 prompt 构建指南。"
            )
        desc_parts.extend([
            "【图片来源】img2img 时优先使用 image_urls 参数传入图片，"
            "未传入则自动从消息中提取。",
            "【调用后】工具会直接将图片发送给用户，成功后禁止重复调用。",
        ])
        self.description = "\n".join(desc_parts)

        # ── 构建 parameters ──
        single_group = len(group_names) == 1
        properties: dict[str, Any] = {}
        required: list[str] = []

        if single_group:
            properties["model_group"] = {
                "type": "string",
                "description": (
                    f"模型组名称。当前仅有一个模型组，可省略。默认: {group_names[0]}"
                ),
                "enum": group_names,
                "default": group_names[0],
            }
        elif group_names:
            properties["model_group"] = {
                "type": "string",
                "description": "模型组名称，从可用模型组中选择。",
                "enum": group_names,
            }
            required.append("model_group")
        else:
            properties["model_group"] = {
                "type": "string",
                "description": "模型组名称（当前无可用模型组）。",
            }
            required.append("model_group")

        properties["prompt"] = {
            "type": "string",
            "description": (
                "图片生成指令。不传此参数时返回所选模型组的 prompt 构建指南。"
                "传入时需明确描述保留主体、修改内容、目标风格、色彩和光影氛围。"
            ),
        }

        properties["image_urls"] = {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "参考图片的路径或 URL 列表。仅在 img2img 模式下需要。"
                "支持本地文件路径和 HTTP/HTTPS URL。"
            ),
        }

        # 收集所有模型组支持的操作类型
        all_ops: set[str] = set()
        for rtg in runtime_groups.values():
            if rtg.config.support_img2img:
                all_ops.add("img2img")
            if rtg.config.support_txt2img:
                all_ops.add("txt2img")

        properties["operation"] = {
            "type": "string",
            "description": (
                "操作类型。不传时使用所选模型组的默认操作。"
            ),
            "enum": sorted(all_ops) if all_ops else ["img2img"],
        }

        self.parameters = {
            "type": "object",
            "properties": properties,
            "required": required,
        }

    # ── 调用入口 ──

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

        if plugin.session is None or not plugin.runtime_groups:
            return "插件运行资源尚未就绪，请稍后再试。"

        runtime_groups = plugin.runtime_groups

        # ── 第一步：解析 model_group ──
        group_name = str(kwargs.get("model_group", "")).strip()
        if not group_name:
            if len(runtime_groups) == 1:
                group_name = next(iter(runtime_groups))
            else:
                available = "、".join(runtime_groups.keys())
                return f"有多个模型组可用，请通过 model_group 参数指定: {available}"

        rtg = runtime_groups.get(group_name)
        if rtg is None:
            available = "、".join(runtime_groups.keys())
            return f"模型组 '{group_name}' 不存在。可用模型组: {available}"

        group_cfg = rtg.config
        prompt = str(kwargs.get("prompt", "")).strip()

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
        operation = str(kwargs.get("operation", "")).strip()
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
            images = await self._resolve_images(
                plugin, event, kwargs.get("image_urls")
            )
            if not images:
                return (
                    "图生图操作需要参考图片。"
                    "请通过 image_urls 传入图片路径，或确保消息中附带了图片。"
                )
        # txt2img: 不提取图片，即使消息中有也忽略

        logger.info(
            f"[gen_img] tool 调用 group={group_name} operation={operation} "
            f"input_images={len(images)} prompt_len={len(prompt)}"
        )

        # ── 第五步：调用路由生成 ──
        result = await rtg.router.generate(prompt, images)

        if result.error or not result.images:
            return result.error or "图片生成失败，未返回图片结果。"

        # 构建消息链并发送
        chain: list = [Comp.Reply(id=event.message_obj.message_id)]
        chain.extend(Comp.Image.fromBase64(b64) for _, b64 in result.images)
        if result.text:
            chain.append(Comp.Plain(result.text))
        await event.send(MessageChain(chain=chain))

        detail = f"（模型组: {group_name}"
        if result.provider_used:
            detail += f", 提供商: {result.provider_used}"
        detail += "）"
        return (
            f"图片生成完成，已发送给用户{detail}。"
            "请直接继续回复用户，禁止再次调用 gen_img。"
        )

    # ── 图片解析 ──

    async def _resolve_images(
        self,
        plugin: Main,
        event: AstrMessageEvent,
        raw_image_urls: Any,
    ) -> list[tuple[str, str]]:
        """提取并解析图片引用，仅在 img2img 时调用。"""
        if plugin.session is None:
            return []

        # 规范化 image_urls 参数：只接受字符串，过滤 None/dict 等非法项
        if not raw_image_urls:
            image_refs: list[str] = []
        elif isinstance(raw_image_urls, str):
            ref = raw_image_urls.strip()
            image_refs = [ref] if ref else []
        elif isinstance(raw_image_urls, (list, tuple)):
            image_refs = [
                str(u).strip()
                for u in raw_image_urls
                if isinstance(u, str) and u.strip()
            ]
        else:
            image_refs = []

        # Agent 传了图片路径：直接解析
        if image_refs:
            logger.info(f"[gen_img] Agent 传入 {len(image_refs)} 张图片引用")
            return await resolve_image_refs(
                refs=image_refs,
                session=plugin.session,
                image_config=plugin.config.image,
                timeout=plugin.config.request.timeout,
            )

        # 未传图片：fallback 从消息事件中提取
        if plugin.config.fallback_to_event_images:
            event_refs = extract_image_refs_from_event(
                event, allow_reply=plugin.config.image.allow_reply_image
            )
            if event_refs:
                logger.info(f"[gen_img] 从消息事件中提取到 {len(event_refs)} 张图片")
                return await resolve_image_refs(
                    refs=event_refs,
                    session=plugin.session,
                    image_config=plugin.config.image,
                    timeout=plugin.config.request.timeout,
                )

        return []
