"""插件配置数据模型。

将 AstrBot 传入的 dict 配置转为强类型 dataclass，
提供安全的默认值和类型校验。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ── 类型转换辅助 ──────────────────────────────────────────


def _get(data: Any, key: str, default: Any = None) -> Any:
    """兼容 dict 和 AstrBot 配置对象的取值。"""
    if isinstance(data, dict):
        return data.get(key, default)
    getter = getattr(data, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except TypeError:
            val = getter(key)
            return default if val is None else val
    return default


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in {"1", "true", "yes", "on"}:
            return True
        if low in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _str(value: Any, default: str) -> str:
    if value is None:
        return default
    return str(value)


# ── 配置 dataclass ────────────────────────────────────────


@dataclass
class ProviderConfig:
    enabled: bool = False
    api_key: str = ""
    base_url: str = ""
    model: str = ""


@dataclass
class RequestConfig:
    timeout: float = 120.0
    max_retry: int = 2


@dataclass
class ImageConfig:
    max_input_images: int = 3
    max_input_mb: int = 20
    allow_reply_image: bool = True


@dataclass
class ImageOutputConfig:
    """图片生成输出参数，对应 OpenRouter 的 image_config 字段。"""

    aspect_ratio: str = "default"
    image_size: str = "1K"

    def to_payload(self) -> dict[str, str]:
        """转为 API 请求中的 image_config 字典，default 值不发送。"""
        payload: dict[str, str] = {}
        if self.aspect_ratio and self.aspect_ratio != "default":
            payload["aspect_ratio"] = self.aspect_ratio
        if self.image_size and self.image_size != "default":
            payload["image_size"] = self.image_size
        return payload


@dataclass
class PluginConfig:
    fallback_to_event_images: bool = True
    default_image_config: ImageOutputConfig = field(default_factory=ImageOutputConfig)
    openrouter: ProviderConfig = field(
        default_factory=lambda: ProviderConfig(
            enabled=True,
            base_url="https://openrouter.ai/api/v1/chat/completions",
            model="google/gemini-3.1-flash-image-preview",
        )
    )
    newapi: ProviderConfig = field(default_factory=ProviderConfig)
    request: RequestConfig = field(default_factory=RequestConfig)
    image: ImageConfig = field(default_factory=ImageConfig)

    @classmethod
    def from_dict(cls, data: Any) -> PluginConfig:
        """从 AstrBot 的配置 dict 构造 PluginConfig。"""
        defaults = cls()

        ic_data = _as_dict(_get(data, "default_image_config", {}))
        or_data = _as_dict(_get(data, "openrouter", {}))
        na_data = _as_dict(_get(data, "newapi", {}))
        rq_data = _as_dict(_get(data, "request", {}))
        im_data = _as_dict(_get(data, "image", {}))

        return cls(
            fallback_to_event_images=_bool(
                _get(data, "fallback_to_event_images"),
                defaults.fallback_to_event_images,
            ),
            default_image_config=ImageOutputConfig(
                aspect_ratio=_str(
                    ic_data.get("aspect_ratio"),
                    defaults.default_image_config.aspect_ratio,
                ),
                image_size=_str(
                    ic_data.get("image_size"),
                    defaults.default_image_config.image_size,
                ),
            ),
            openrouter=ProviderConfig(
                enabled=_bool(or_data.get("enabled"), defaults.openrouter.enabled),
                api_key=_str(or_data.get("api_key"), defaults.openrouter.api_key),
                base_url=_str(or_data.get("base_url"), defaults.openrouter.base_url),
                model=_str(or_data.get("model"), defaults.openrouter.model),
            ),
            newapi=ProviderConfig(
                enabled=_bool(na_data.get("enabled"), defaults.newapi.enabled),
                api_key=_str(na_data.get("api_key"), defaults.newapi.api_key),
                base_url=_str(na_data.get("base_url"), defaults.newapi.base_url),
                model=_str(na_data.get("model"), defaults.newapi.model),
            ),
            request=RequestConfig(
                timeout=max(
                    1.0, _float(rq_data.get("timeout"), defaults.request.timeout)
                ),
                max_retry=max(
                    0, _int(rq_data.get("max_retry"), defaults.request.max_retry)
                ),
            ),
            image=ImageConfig(
                max_input_images=max(
                    1,
                    _int(
                        im_data.get("max_input_images"),
                        defaults.image.max_input_images,
                    ),
                ),
                max_input_mb=max(
                    1, _int(im_data.get("max_input_mb"), defaults.image.max_input_mb)
                ),
                allow_reply_image=_bool(
                    im_data.get("allow_reply_image"),
                    defaults.image.allow_reply_image,
                ),
            ),
        )
