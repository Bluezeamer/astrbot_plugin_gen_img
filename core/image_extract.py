"""图片提取与规范化。

负责：
- 统一处理 Agent 传入的图片引用（本地路径 / HTTP URL / data URI）
- 从消息事件中 fallback 提取 Image 组件
- 下载图片并转为 (mime, base64_str)
- MIME 检测、大小校验、GIF 转 PNG
"""

from __future__ import annotations

import asyncio
import base64
import io
from pathlib import Path
from typing import Any

import aiohttp
import astrbot.api.message_components as Comp
from astrbot.api import logger

from .config import ImageConfig

try:
    from PIL import Image as PILImage
except ImportError:
    PILImage = None  # type: ignore[assignment,misc]

_DOWNLOAD_CHUNK = 64 * 1024  # 64KB


# ── MIME 检测 ─────────────────────────────────────────────


def detect_mime(data: bytes, fallback: str | None = None) -> str:
    """通过文件魔数检测图片 MIME 类型。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in {b"GIF87a", b"GIF89a"}:
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    if fallback:
        clean = fallback.split(";", 1)[0].strip().lower()
        if clean.startswith("image/"):
            return clean
    raise ValueError("无法识别图片 MIME 类型")


# ── 图片规范化 ────────────────────────────────────────────


def _gif_to_png(data: bytes) -> bytes:
    """将 GIF 第一帧转换为 PNG。"""
    if PILImage is None:
        raise ValueError("检测到 GIF，但缺少 Pillow 库，无法提取第一帧")
    with PILImage.open(io.BytesIO(data)) as img:
        img.seek(0)
        frame = img.convert("RGBA")
        buf = io.BytesIO()
        frame.save(buf, format="PNG")
        return buf.getvalue()


def encode_image(data: bytes, content_type: str | None = None) -> tuple[str, str]:
    """将图片 bytes 转为 (mime, base64_str)，GIF 自动转 PNG。"""
    mime = detect_mime(data, content_type)
    if mime == "image/gif":
        data = _gif_to_png(data)
        mime = "image/png"
    return mime, base64.b64encode(data).decode("utf-8")


# ── 下载 ──────────────────────────────────────────────────


async def download_image(
    session: aiohttp.ClientSession,
    url: str,
    max_mb: int,
    timeout: float,
) -> tuple[str, str]:
    """下载图片并返回 (mime, base64_str)。"""
    max_bytes = max_mb * 1024 * 1024
    tc = aiohttp.ClientTimeout(total=timeout)

    async with session.get(url, timeout=tc) as resp:
        if resp.status != 200:
            raise ValueError(f"下载失败 HTTP {resp.status}")

        cl = resp.headers.get("Content-Length")
        if cl:
            try:
                declared_size = int(cl)
            except (ValueError, TypeError):
                declared_size = 0
            if declared_size > max_bytes:
                raise ValueError(f"图片过大：{declared_size} bytes > {max_bytes} bytes")

        content_type = resp.headers.get("Content-Type")
        chunks: list[bytes] = []
        total = 0
        async for chunk in resp.content.iter_chunked(_DOWNLOAD_CHUNK):
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"图片过大：已读取 {total} bytes > {max_bytes} bytes")
            chunks.append(chunk)

    return encode_image(b"".join(chunks), content_type)


# ── 本地文件读取 ──────────────────────────────────────────


def read_local_image(path_str: str, max_mb: int) -> tuple[str, str]:
    """读取本地图片文件并返回 (mime, base64_str)。"""
    path = Path(path_str)
    if not path.exists() or not path.is_file():
        raise ValueError(f"本地图片不存在：{path}")
    max_bytes = max_mb * 1024 * 1024
    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(f"图片过大：{size} bytes > {max_bytes} bytes")
    return encode_image(path.read_bytes())


# ── 解析 data URI ─────────────────────────────────────────


def parse_data_uri(uri: str, max_mb: int = 20) -> tuple[str, str]:
    """解析 data:image/xxx;base64,... 格式，返回 (mime, base64_str)。

    会校验 base64 合法性和解码后大小。
    """
    header, _, b64_data = uri.partition(",")
    if not b64_data:
        raise ValueError("data URI 格式无效")
    mime = header.split(";", 1)[0].split(":", 1)[-1].strip()
    if not mime.startswith("image/"):
        raise ValueError(f"data URI 非图片类型：{mime}")
    b64_clean = b64_data.strip()
    # 校验 base64 合法性和大小
    try:
        raw = base64.b64decode(b64_clean, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"data URI base64 解码失败: {exc}") from exc
    max_bytes = max_mb * 1024 * 1024
    if len(raw) > max_bytes:
        raise ValueError(f"data URI 图片过大：{len(raw)} bytes > {max_bytes} bytes")
    # 重新编码以确保规范化（同时处理 GIF 转 PNG）
    return encode_image(raw, mime)


# ── 统一解析图片引用 ──────────────────────────────────────


async def resolve_image_refs(
    refs: list[str],
    session: aiohttp.ClientSession,
    image_config: ImageConfig,
    timeout: float,
    deadline: float | None = None,
) -> list[tuple[str, str]]:
    """统一处理 Agent 传入的图片引用列表。

    支持三种格式：
    - 本地文件路径（如 /tmp/img.png）
    - HTTP/HTTPS URL
    - data:image/... URI

    当提供 deadline 时，每次 HTTP 下载前重算剩余时间，防止多张图片
    串行下载耗尽时间预算。timeout 仅作为单次下载的上限。

    返回 [(mime, base64_str), ...]
    """
    results: list[tuple[str, str]] = []
    seen: set[str] = set()

    for ref in refs:
        ref = str(ref).strip()
        if not ref or ref in seen:
            continue
        seen.add(ref)

        if len(results) >= image_config.max_input_images:
            break

        try:
            if ref.startswith("data:image/"):
                results.append(parse_data_uri(ref, max_mb=image_config.max_input_mb))
            elif ref.startswith("http://") or ref.startswith("https://"):
                # 每次下载前重算剩余时间
                dl_timeout = min(timeout, 60.0)
                if deadline is not None:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        logger.warning(
                            f"[gen_img] 输入图下载预算耗尽，跳过剩余 ref={ref[:80]}"
                        )
                        break
                    dl_timeout = min(dl_timeout, remaining)
                results.append(
                    await download_image(
                        session=session,
                        url=ref,
                        max_mb=image_config.max_input_mb,
                        timeout=dl_timeout,
                    )
                )
            else:
                # 视为本地路径
                results.append(read_local_image(ref, image_config.max_input_mb))
        except Exception as exc:
            logger.warning(f"[gen_img] 跳过图片引用 ref={ref[:80]} err={exc}")
            continue

    return results


# ── 从消息组件收集图片 URL ────────────────────────────────


def _collect_urls(components: Any, include_reply: bool) -> list[str]:
    """从消息组件列表中收集去重的图片 URL/路径。"""
    urls: list[str] = []
    seen: set[str] = set()

    items = components if isinstance(components, list) else []

    for comp in items:
        if isinstance(comp, Comp.Image):
            url = str(getattr(comp, "url", "") or getattr(comp, "file", "") or "").strip()
            if url and url not in seen:
                seen.add(url)
                urls.append(url)

        if include_reply and isinstance(comp, Comp.Reply):
            chain = getattr(comp, "chain", None)
            if isinstance(chain, list):
                for sub in chain:
                    if isinstance(sub, Comp.Image):
                        url = str(
                            getattr(sub, "url", "") or getattr(sub, "file", "") or ""
                        ).strip()
                        if url and url not in seen:
                            seen.add(url)
                            urls.append(url)

    return urls


def extract_image_refs_from_event(event: Any, allow_reply: bool = True) -> list[str]:
    """从消息事件中提取图片引用列表（URL 或本地路径）。"""
    message_obj = getattr(event, "message_obj", None)
    components = getattr(message_obj, "message", None)

    urls = _collect_urls(components, allow_reply)

    # 兜底：尝试 event.get_messages()
    if not urls and hasattr(event, "get_messages"):
        try:
            urls = _collect_urls(event.get_messages(), allow_reply)
        except Exception as exc:
            logger.warning(f"[gen_img] 从 get_messages() 提取图片失败: {exc}")

    return urls
