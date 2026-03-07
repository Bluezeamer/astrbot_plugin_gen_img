"""OpenAI 兼容提供商：请求构造与响应解析。

支持 OpenRouter 和 NewAPI 中转的 /v1/chat/completions 端点。
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

from .config import ImageConfig, ImageOutputConfig, ProviderConfig, RequestConfig
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
        config: ProviderConfig,
        session: aiohttp.ClientSession,
        request_config: RequestConfig,
        image_config: ImageConfig,
        output_config: ImageOutputConfig,
    ) -> None:
        self.name = name
        self.config = config
        self.session = session
        self.request_config = request_config
        self.image_config = image_config
        self.output_config = output_config

    async def generate(
        self,
        prompt: str,
        images: list[tuple[str, str]],
    ) -> ProviderResult:
        """发送图片生成请求并解析响应。"""
        err = self._check_config()
        if err:
            return err

        payload = self._build_payload(prompt, images)
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=self.request_config.timeout)

        try:
            async with self.session.post(
                self.config.base_url,
                json=payload,
                headers=headers,
                timeout=timeout,
            ) as resp:
                raw = await resp.text()
                status = resp.status
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

        return await self._parse_response(data, status)

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
            "modalities": ["image", "text"],
            "stream": False,
        }
        # 添加 image_config（OpenRouter 特有，NewAPI 中转通常忽略）
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

    async def _parse_response(self, data: dict, status: int) -> ProviderResult:
        """解析 Chat Completions 响应，提取图片。"""
        images: list[tuple[str, str]] = []
        texts: list[str] = []
        download_failed = False
        seen: set[str] = set()  # 候选去重

        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return ProviderResult(
                error=f"{self.name} 响应中无 choices",
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
                        failed = await self._consume_candidate(url, images)
                        download_failed = download_failed or failed

            content = message.get("content")

            # ── 情况 1：content 是字符串 ──
            if isinstance(content, str):
                failed = await self._extract_from_text(content, images, seen)
                download_failed = download_failed or failed
                # 清理 text：去掉 markdown 图片标记和裸 data URI
                clean = _MD_IMG_RE.sub("", content)
                clean = _DATA_URI_RE.sub("", clean).strip()
                if clean:
                    texts.append(clean)

            # ── 情况 2：content 是结构化列表 ──
            elif isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    ptype = part.get("type", "")

                    if ptype == "text" and isinstance(part.get("text"), str):
                        text_val = part["text"]
                        failed = await self._extract_from_text(text_val, images, seen)
                        download_failed = download_failed or failed
                        clean = _MD_IMG_RE.sub("", text_val)
                        clean = _DATA_URI_RE.sub("", clean).strip()
                        if clean:
                            texts.append(clean)

                    elif ptype == "image_url":
                        img_url = part.get("image_url", {})
                        url = img_url.get("url", "") if isinstance(img_url, dict) else ""
                        if url:
                            failed = await self._consume_candidate(url, images)
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
        self, text: str, images: list[tuple[str, str]], seen: set[str]
    ) -> bool:
        """从文本中提取 data URI 和 markdown 图片链接，返回是否有下载失败。"""
        download_failed = False
        # markdown 图片标记（可能包含 data URI 或 HTTP URL）
        for match in _MD_IMG_RE.finditer(text):
            candidate = match.group(1).strip().strip("<>")
            if candidate and candidate not in seen:
                seen.add(candidate)
                failed = await self._consume_candidate(candidate, images)
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
        return download_failed

    async def _consume_candidate(
        self, candidate: str, images: list[tuple[str, str]]
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
            try:
                img = await download_image(
                    session=self.session,
                    url=candidate,
                    max_mb=_OUTPUT_MAX_MB,
                    timeout=min(self.request_config.timeout, 60.0),
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
