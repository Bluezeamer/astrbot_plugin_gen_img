"""提供商路由：重试 + 降级调度。

按优先级遍历提供商，每个内部重试 max_retry 次，
根据错误类型决定重试当前、降级到下一个、或直接终止。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from astrbot.api import logger

from .provider import OpenAICompatibleProvider, ProviderResult


@dataclass
class RouterResult:
    images: list[tuple[str, str]] = field(default_factory=list)
    text: str = ""
    provider_used: str = ""
    error: str = ""


class ProviderRouter:
    """多提供商路由调度器。"""

    def __init__(
        self,
        providers: list[OpenAICompatibleProvider],
        max_retry: int,
    ) -> None:
        self.providers = providers
        self.max_retry = max(0, max_retry)

    async def generate(
        self,
        prompt: str,
        images: list[tuple[str, str]],
    ) -> RouterResult:
        if not self.providers:
            return RouterResult(error="当前没有可用的图片生成提供商，请检查配置。")

        errors: list[str] = []
        last_text = ""
        attempts = self.max_retry + 1  # 总尝试次数 = 重试次数 + 1

        for provider in self.providers:
            logger.info(
                f"[gen_img] 路由 → {provider.name} "
                f"max_attempts={attempts} input_images={len(images)}"
            )

            for attempt in range(1, attempts + 1):
                logger.info(f"[gen_img] {provider.name} 第 {attempt}/{attempts} 次尝试")

                result = await provider.generate(prompt, images)
                last_text = result.text or last_text

                # 成功：有图片返回
                if result.images:
                    logger.info(
                        f"[gen_img] {provider.name} 成功 "
                        f"output_images={len(result.images)}"
                    )
                    return RouterResult(
                        images=result.images,
                        text=result.text,
                        provider_used=provider.name,
                    )

                # 可重试错误：在当前提供商内重试
                if result.retryable:
                    err = result.error or f"{provider.name} 临时错误"
                    if attempt < attempts:
                        delay = min(2.0, 0.5 * attempt)
                        logger.warning(
                            f"[gen_img] {provider.name} 可重试错误 "
                            f"attempt={attempt}/{attempts} delay={delay}s: {err}"
                        )
                        await asyncio.sleep(delay)
                        continue
                    # 重试耗尽，降级到下一个提供商
                    logger.warning(f"[gen_img] {provider.name} 重试耗尽: {err}")
                    errors.append(f"{provider.name}: {err}")
                    break

                # 应降级错误：跳到下一个提供商
                if result.should_fallback:
                    err = result.error or f"{provider.name} 需降级"
                    logger.warning(f"[gen_img] {provider.name} 降级: {err}")
                    errors.append(f"{provider.name}: {err}")
                    break

                # 不可重试/不降级错误：直接终止
                err = result.error or f"{provider.name} 调用失败"
                logger.error(f"[gen_img] {provider.name} 终止: {err}")
                return RouterResult(
                    text=result.text,
                    provider_used=provider.name,
                    error=err,
                )

        final_error = "；".join(errors) if errors else "所有提供商均未返回图片。"
        return RouterResult(text=last_text, error=final_error)
