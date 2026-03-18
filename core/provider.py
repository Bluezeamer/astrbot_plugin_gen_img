"""OpenAI 兼容提供商：请求构造与响应解析。

支持任意 OpenAI 兼容 /v1/chat/completions 端点。
响应解析覆盖四种图片返回格式：
1. OpenRouter message.images[] 结构化数组（优先）
2. markdown data URI（base64 内联）
3. markdown HTTP URL（需下载）
4. 结构化 content part（image_url / b64_json）
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from astrbot.api import logger

from .config import EndpointConfig, ImageOutputConfig, RequestConfig
from .image_extract import download_image, encode_image

# 匹配 data:image/...;base64,... 格式
_DATA_URI_RE = re.compile(
    r"data:(image/[a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=\s]+)", re.IGNORECASE
)
# 匹配 markdown 图片 ![...](...)
_MD_IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
# 判断 HTTP URL
_HTTP_RE = re.compile(r"^https?://", re.IGNORECASE)

# 输出图片下载限制（生成图通常比输入大）
_OUTPUT_MAX_MB = 50
_RETRYABLE_STATUS = {408, 500, 502, 503, 504}
_NO_RETRY_STATUS = {400, 401, 403, 404, 422}
# CancelledError 判定容差：deadline 剩余低于此值视为预算耗尽而非外部取消。
# 注意：Python 3.10 无 asyncio.timeout()，无法建立独立的内部取消源，
# 因此用 deadline 余量启发式区分内部/外部取消。由于内部 deadline（默认 50s）
# 远早于 AstrBot 硬超时（60s），误判窗口约 0.5s，实际风险极低。
# 当项目最低版本升至 3.11 后，可改用 asyncio.timeout() 精确区分。
_CANCEL_GRACE_SECONDS = 0.5


def _is_json(text: str) -> bool:
    """快速判断字符串是否为有效 JSON（用于文本清理决策）。"""
    try:
        json.loads(text)
        return True
    except (json.JSONDecodeError, ValueError):
        return False


@dataclass
class ProviderResult:
    images: list[tuple[str, str]] = field(default_factory=list)
    text: str = ""
    error: str = ""
    status_code: int = 0
    retryable: bool = False
    should_fallback: bool = False


class OpenAICompatibleProvider:
    """统一的 OpenAI 兼容 API 提供商。"""

    def __init__(
        self,
        name: str,
        config: EndpointConfig,
        session: aiohttp.ClientSession,
        request_config: RequestConfig,
        modalities: list[str] | tuple[str, ...],
        output_config: ImageOutputConfig,
    ) -> None:
        self.name = name
        self.config = config
        self.session = session
        self.request_config = request_config
        self.modalities = list(modalities) or ["image", "text"]
        self.output_config = output_config

    async def generate(
        self,
        prompt: str,
        images: list[tuple[str, str]],
        deadline: float | None = None,
    ) -> ProviderResult:
        """发送图片生成请求并解析响应。"""
        err = self._check_config()
        if err:
            return err

        request_timeout = self._effective_timeout(deadline)
        if request_timeout <= 0:
            return ProviderResult(
                error=f"{self.name} 剩余时间不足，跳过请求", retryable=True
            )

        payload = self._build_payload(prompt, images)
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=request_timeout)

        try:
            async with self.session.post(
                self.config.base_url,
                json=payload,
                headers=headers,
                timeout=timeout,
            ) as resp:
                raw = await resp.text()
                status = resp.status
        except asyncio.CancelledError:
            if self._deadline_exceeded(deadline):
                return ProviderResult(
                    error=f"{self.name} 请求被取消（时间预算耗尽）", retryable=True
                )
            raise
        except asyncio.TimeoutError:
            return ProviderResult(error=f"{self.name} 请求超时", retryable=True)
        except aiohttp.ClientError as exc:
            return ProviderResult(error=f"{self.name} 网络错误: {exc}", retryable=True)

        if status < 200 or status >= 300:
            return self._classify_error(status, raw)

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return ProviderResult(
                error=f"{self.name} 响应非 JSON",
                status_code=status,
                should_fallback=True,
            )

        try:
            return await self._parse_response(data, status, deadline=deadline)
        except asyncio.CancelledError:
            if self._deadline_exceeded(deadline):
                return ProviderResult(
                    error=f"{self.name} 响应解析被取消（时间预算耗尽）",
                    status_code=status,
                    retryable=True,
                )
            raise

    # ── 时间预算辅助 ──────────────────────────────────────

    def _effective_timeout(self, deadline: float | None) -> float:
        """根据 deadline 计算当前请求可用的超时秒数。"""
        base = max(0.0, self.request_config.timeout)
        if deadline is None:
            return base
        remaining = deadline - asyncio.get_running_loop().time()
        return max(0.0, min(base, remaining))

    def _deadline_exceeded(self, deadline: float | None) -> bool:
        """判断是否已接近或超过内部 deadline（容差 _CANCEL_GRACE_SECONDS）。"""
        if deadline is None:
            return False
        return self._effective_timeout(deadline) <= _CANCEL_GRACE_SECONDS

    # ── 请求构造 ──────────────────────────────────────────

    def _build_payload(
        self,
        prompt: str,
        images: list[tuple[str, str]],
    ) -> dict[str, Any]:
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for mime, b64 in images:
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            })
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [{"role": "user", "content": content}],
            "modalities": list(self.modalities),
            "stream": False,
        }
        # 添加 image_config（部分 OpenAI 兼容端点可能忽略此字段）
        image_payload = self.output_config.to_payload()
        if image_payload:
            payload["image_config"] = image_payload
        return payload

    # ── 前置校验 ──────────────────────────────────────────

    def _check_config(self) -> ProviderResult | None:
        if not self.config.enabled:
            return ProviderResult(error=f"{self.name} 已禁用", should_fallback=True)
        if not self.config.api_key:
            return ProviderResult(error=f"{self.name} 缺少 API Key", should_fallback=True)
        if not self.config.base_url:
            return ProviderResult(error=f"{self.name} 缺少 base_url", should_fallback=True)
        if not self.config.model:
            return ProviderResult(error=f"{self.name} 缺少 model", should_fallback=True)
        return None

    # ── 错误分类 ──────────────────────────────────────────

    def _classify_error(self, status: int, raw: str) -> ProviderResult:
        msg = self._extract_error_msg(raw)
        logger.warning(f"[gen_img] {self.name} HTTP {status}: {msg}")

        if status == 429:
            return ProviderResult(
                error=f"{self.name} 限流: {msg}",
                status_code=status,
                should_fallback=True,
            )
        if status in _RETRYABLE_STATUS:
            return ProviderResult(
                error=f"{self.name} 服务暂不可用({status}): {msg}",
                status_code=status,
                retryable=True,
            )
        if status in _NO_RETRY_STATUS:
            # 配置/鉴权类错误也触发降级，让备用提供商有机会尝试
            return ProviderResult(
                error=f"{self.name} 请求失败({status}): {msg}",
                status_code=status,
                should_fallback=True,
            )
        return ProviderResult(
            error=f"{self.name} 未知错误({status}): {msg}",
            status_code=status,
            should_fallback=True,
        )

    def _extract_error_msg(self, raw: str) -> str:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return raw[:200].strip() or "请求失败"
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                return str(err.get("message") or err.get("type") or err.get("code") or data)
            if isinstance(err, str):
                return err
            if isinstance(data.get("message"), str):
                return data["message"]
        return raw[:200].strip() or "请求失败"

    # ── 响应解析 ──────────────────────────────────────────

    async def _parse_response(
        self,
        data: dict,
        status: int,
        deadline: float | None = None,
    ) -> ProviderResult:
        """解析 Chat Completions 响应，提取图片。

        兼容两种顶层格式：
        - Chat Completions: {choices: [{message: {content: ...}}]}
        - Images Generations: {data: [{url: ...} | {b64_json: ...}]}
        """
        images: list[tuple[str, str]] = []
        texts: list[str] = []
        download_failed = False
        seen: set[str] = set()  # 候选去重

        # ── 顶层 data[] 格式（/v1/images/generations 风格）──
        top_data = data.get("data")
        if isinstance(top_data, list) and top_data:
            for item in top_data:
                if not isinstance(item, dict):
                    continue
                # url 格式
                url = item.get("url", "")
                if isinstance(url, str) and url:
                    failed = await self._consume_candidate(url, images, deadline=deadline)
                    download_failed = download_failed or failed
                # b64_json 格式
                b64 = item.get("b64_json") or item.get("base64")
                if isinstance(b64, str) and b64:
                    mime = str(item.get("mime_type") or item.get("mime") or "image/png")
                    self._try_append_b64(mime, b64, images)
                # revised_prompt 作为文本
                rp = item.get("revised_prompt")
                if isinstance(rp, str) and rp.strip():
                    texts.append(rp.strip())
            if images:
                return ProviderResult(
                    images=images, text="\n".join(texts).strip(), status_code=status
                )
            if download_failed:
                return ProviderResult(
                    text="\n".join(texts).strip(),
                    error=f"{self.name} data[] 中的图片 URL 下载失败",
                    status_code=status,
                    should_fallback=True,
                )

        # ── Chat Completions 格式 ──
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return ProviderResult(
                error=f"{self.name} 响应中无 choices 或 data",
                status_code=status,
                should_fallback=True,
            )

        for choice in choices:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message", {})
            if not isinstance(message, dict):
                continue

            # ── OpenRouter 格式：message.images[] ──
            msg_images = message.get("images")
            if isinstance(msg_images, list):
                for img_item in msg_images:
                    if not isinstance(img_item, dict):
                        continue
                    img_url_obj = img_item.get("image_url")
                    if isinstance(img_url_obj, dict):
                        url = img_url_obj.get("url", "")
                    elif isinstance(img_url_obj, str):
                        url = img_url_obj
                    else:
                        url = img_item.get("url", "")
                    if url:
                        failed = await self._consume_candidate(url, images, deadline=deadline)
                        download_failed = download_failed or failed

            content = message.get("content")

            # ── 情况 1：content 是字符串 ──
            if isinstance(content, str):
                failed = await self._extract_from_text(content, images, seen, deadline=deadline)
                download_failed = download_failed or failed
                # 清理 text：去掉图片相关内容
                clean = _MD_IMG_RE.sub("", content)
                clean = _DATA_URI_RE.sub("", clean)
                # 如果整段 content 是 JSON 或裸 URL（图片载体），不保留为文本
                stripped = clean.strip()
                if stripped and not (
                    (stripped[0] in "[{" and _is_json(stripped))
                    or _HTTP_RE.match(stripped)
                ):
                    texts.append(stripped)

            # ── 情况 2：content 是结构化列表 ──
            elif isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    ptype = part.get("type", "")

                    if ptype == "text" and isinstance(part.get("text"), str):
                        text_val = part["text"]
                        failed = await self._extract_from_text(text_val, images, seen, deadline=deadline)
                        download_failed = download_failed or failed
                        clean = _MD_IMG_RE.sub("", text_val)
                        clean = _DATA_URI_RE.sub("", clean).strip()
                        if clean:
                            texts.append(clean)

                    elif ptype == "image_url":
                        img_url = part.get("image_url", {})
                        if isinstance(img_url, dict):
                            url = img_url.get("url", "")
                        elif isinstance(img_url, str):
                            url = img_url
                        else:
                            url = ""
                        if url:
                            failed = await self._consume_candidate(url, images, deadline=deadline)
                            download_failed = download_failed or failed

                    # 有些 API 直接返回 b64_json
                    b64 = part.get("b64_json") or part.get("base64")
                    if isinstance(b64, str):
                        mime = str(part.get("mime_type") or part.get("mime") or "image/png")
                        self._try_append_b64(mime, b64, images)

        text = "\n".join(texts).strip()
        if images:
            return ProviderResult(images=images, text=text, status_code=status)
        if download_failed:
            return ProviderResult(
                text=text,
                error=f"{self.name} 返回了图片 URL 但下载失败",
                status_code=status,
                should_fallback=True,
            )
        return ProviderResult(
            text=text,
            error=f"{self.name} 响应中未找到图片",
            status_code=status,
            should_fallback=True,
        )

    async def _extract_from_text(
        self,
        text: str,
        images: list[tuple[str, str]],
        seen: set[str],
        deadline: float | None = None,
    ) -> bool:
        """从文本中提取图片，返回是否有下载失败。

        按优先级处理：markdown 图片 → data URI → JSON 字符串 → 裸 HTTP URL。
        """
        download_failed = False

        # markdown 图片标记（可能包含 data URI 或 HTTP URL）
        for match in _MD_IMG_RE.finditer(text):
            candidate = match.group(1).strip().strip("<>")
            if candidate and candidate not in seen:
                seen.add(candidate)
                failed = await self._consume_candidate(candidate, images, deadline=deadline)
                download_failed = download_failed or failed

        # 非 markdown 包裹的 data URI（避免与上面重复）
        for match in _DATA_URI_RE.finditer(text):
            full_uri = match.group(0)
            if full_uri in seen:
                continue
            seen.add(full_uri)
            mime = match.group(1)
            b64 = re.sub(r"\s+", "", match.group(2))
            self._try_append_b64(mime, b64, images)

        # JSON 字符串格式：content 可能是 JSON 编码的 {url:...} 或 [{url:...}]
        stripped = text.strip()
        if not images and stripped and stripped[0] in "[{":
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                pass
            else:
                items = parsed if isinstance(parsed, list) else [parsed]
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    url = item.get("url", "")
                    if isinstance(url, str) and url and url not in seen:
                        seen.add(url)
                        failed = await self._consume_candidate(url, images, deadline=deadline)
                        download_failed = download_failed or failed
                    b64 = item.get("b64_json") or item.get("base64")
                    if isinstance(b64, str) and b64:
                        mime = str(item.get("mime_type") or item.get("mime") or "image/png")
                        self._try_append_b64(mime, b64, images)

        # 裸 HTTP URL（整段 content 就是一个 URL，没有 markdown 包裹）
        if not images and _HTTP_RE.match(stripped):
            candidate = stripped.split()[0]  # 取第一个 token 防止尾随文字
            if candidate not in seen:
                seen.add(candidate)
                failed = await self._consume_candidate(candidate, images, deadline=deadline)
                download_failed = download_failed or failed

        return download_failed

    async def _consume_candidate(
        self,
        candidate: str,
        images: list[tuple[str, str]],
        deadline: float | None = None,
    ) -> bool:
        """处理单个图片候选（data URI 或 HTTP URL），返回是否失败。"""
        if candidate.startswith("data:image/"):
            m = _DATA_URI_RE.search(candidate)
            if m:
                mime = m.group(1)
                b64 = re.sub(r"\s+", "", m.group(2))
                self._try_append_b64(mime, b64, images)
            return False

        if _HTTP_RE.match(candidate):
            dl_timeout = min(self._effective_timeout(deadline), 60.0)
            if dl_timeout <= 0:
                logger.warning(f"[gen_img] {self.name} 结果图下载预算耗尽，跳过: {candidate[:80]}")
                return True
            try:
                img = await download_image(
                    session=self.session,
                    url=candidate,
                    max_mb=_OUTPUT_MAX_MB,
                    timeout=dl_timeout,
                )
                images.append(img)
                return False
            except Exception as exc:
                logger.warning(f"[gen_img] {self.name} 下载结果图失败: {candidate[:80]} {exc}")
                return True

        return False

    def _try_append_b64(
        self, mime: str, b64_data: str, images: list[tuple[str, str]]
    ) -> None:
        """尝试将 base64 数据追加到结果列表。"""
        clean = re.sub(r"\s+", "", b64_data)
        try:
            raw = base64.b64decode(clean, validate=True)
        except (ValueError, TypeError):
            return
        final_mime, final_b64 = encode_image(raw, mime)
        images.append((final_mime, final_b64))
