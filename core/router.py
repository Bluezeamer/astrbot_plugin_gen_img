"""提供商路由：重试 + 降级调度。

按优先级遍历提供商，每个内部重试 max_retry 次，
根据错误类型决定重试当前、降级到下一个、或直接终止。
通过 deadline 机制确保总耗时不超过外层工具超时预算。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from astrbot.api import logger

from .provider import OpenAICompatibleProvider, ProviderResult

# 单次尝试至少需要的秒数，低于此值直接跳过
_MIN_ATTEMPT_SECONDS = 2.0


@dataclass
class RouterResult:
    images: list[tuple[str, str]] = field(default_factory=list)
    text: str = ""
    provider_used: str = ""
    error: str = ""
    providers_tried: int = 0


class ProviderRouter:
    """多提供商路由调度器。"""

    def __init__(
        self,
        providers: list[OpenAICompatibleProvider],
        max_retry: int,
        request_timeout: float,
    ) -> None:
        self.providers = providers
        self.max_retry = max(0, max_retry)
        self.request_timeout = max(1.0, request_timeout)

    # ── 时间预算辅助 ──────────────────────────────────────

    @staticmethod
    def _remaining(deadline: float) -> float:
        return max(0.0, deadline - asyncio.get_running_loop().time())

    # ── 主入口 ────────────────────────────────────────────

    async def generate(
        self,
        prompt: str,
        images: list[tuple[str, str]],
        deadline: float | None = None,
        start_index: int = 0,
    ) -> RouterResult:
        if not self.providers:
            return RouterResult(error="当前没有可用的图片生成提供商，请检查配置。")

        if deadline is None:
            deadline = asyncio.get_running_loop().time() + self.request_timeout

        start_index = max(0, min(start_index, len(self.providers)))
        active_providers = self.providers[start_index:]

        errors: list[str] = []
        last_text = ""
        attempts = self.max_retry + 1  # 总尝试次数 = 重试次数 + 1
        budget_exhausted = False
        providers_tried = 0

        for provider in active_providers:
            remaining = self._remaining(deadline)
            if remaining < _MIN_ATTEMPT_SECONDS:
                budget_exhausted = True
                break

            providers_tried += 1
            logger.info(
                f"[gen_img] 路由 → {provider.name} "
                f"max_attempts={attempts} input_images={len(images)} "
                f"remaining={remaining:.1f}s"
            )

            for attempt in range(1, attempts + 1):
                remaining = self._remaining(deadline)
                if remaining < _MIN_ATTEMPT_SECONDS:
                    logger.warning(
                        f"[gen_img] {provider.name} 跳过：剩余时间 {remaining:.1f}s 不足"
                    )
                    budget_exhausted = True
                    break

                logger.info(
                    f"[gen_img] {provider.name} 第 {attempt}/{attempts} 次尝试 "
                    f"budget={remaining:.1f}s"
                )

                result = await provider.generate(prompt, images, deadline=deadline)
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
                        providers_tried=providers_tried,
                    )

                # 可重试错误：在当前提供商内重试
                if result.retryable:
                    err = result.error or f"{provider.name} 临时错误"
                    if attempt < attempts:
                        delay = min(2.0, 0.5 * attempt)
                        if self._remaining(deadline) <= delay + _MIN_ATTEMPT_SECONDS:
                            logger.warning(
                                f"[gen_img] {provider.name} 剩余时间不足，停止重试: {err}"
                            )
                            errors.append(err)
                            budget_exhausted = True
                            break
                        logger.warning(
                            f"[gen_img] {provider.name} 可重试错误 "
                            f"attempt={attempt}/{attempts} delay={delay}s: {err}"
                        )
                        await asyncio.sleep(delay)
                        continue
                    # 重试耗尽，降级到下一个提供商
                    logger.warning(f"[gen_img] {provider.name} 重试耗尽: {err}")
                    errors.append(err)
                    break

                # 应降级错误：跳到下一个提供商
                if result.should_fallback:
                    err = result.error or f"{provider.name} 需降级"
                    logger.warning(f"[gen_img] {provider.name} 降级: {err}")
                    errors.append(err)
                    break

                # 不可重试/不降级错误：直接终止
                err = result.error or f"{provider.name} 调用失败"
                logger.error(f"[gen_img] {provider.name} 终止: {err}")
                return RouterResult(
                    text=result.text,
                    provider_used=provider.name,
                    error=err,
                    providers_tried=providers_tried,
                )

            if budget_exhausted:
                break

        if budget_exhausted:
            errors.append("内部时间预算已耗尽")
        final_error = "；".join(dict.fromkeys(errors)) if errors else "所有提供商均未返回图片。"
        return RouterResult(text=last_text, error=final_error, providers_tried=providers_tried)
