"""插件配置数据模型。

将 AstrBot 传入的 dict 配置转为强类型 dataclass，
提供安全的默认值和类型校验。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger


# ── 类型转换辅助 ──────────────────────────────────────────

_MISSING = object()
_DEFAULT_MODALITIES = ("image", "text")


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


def _str_list(value: Any, default: tuple[str, ...] | list[str]) -> list[str]:
    """将逗号分隔字符串、JSON 数组或 list/tuple 转为 list[str]。"""
    fallback = list(default)
    if isinstance(value, (list, tuple)):
        items = [str(item).strip() for item in value if str(item).strip()]
        return items or fallback
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return fallback
        # JSON 数组格式（如 '["image"]'）
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    return _str_list(parsed, default)
            except json.JSONDecodeError:
                pass
        # 逗号分隔格式，兼容中文逗号
        return [p.strip() for p in raw.replace("，", ",").split(",") if p.strip()] or fallback
    return fallback


def _str_set_lines(value: Any) -> set[str]:
    """将多行文本或序列转为去重的 set[str]，每行一个元素。"""
    if isinstance(value, (list, tuple, set, frozenset)):
        return {str(item).strip() for item in value if str(item).strip()}
    if isinstance(value, str):
        return {
            line.strip()
            for line in value.splitlines()
            if line.strip()
        }
    return set()


# ── 配置 dataclass ────────────────────────────────────────


@dataclass
class EndpointConfig:
    """单个 API 端点配置。"""

    name: str = ""
    enabled: bool = True
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
class QuotaConfig:
    """用户配额配置。"""

    enabled: bool = False
    daily_limit: int = 10
    reset_hour: int = 0
    whitelist: set[str] = field(default_factory=set)


@dataclass
class ImageOutputConfig:
    """图片生成输出参数，对应 OpenAI 兼容接口的 image_config 字段。"""

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
class ModelGroupConfig:
    """模型组配置。"""

    group_name: str = ""
    group_description: str = ""
    guide: str = ""
    support_img2img: bool = True
    support_txt2img: bool = False
    default_operation: str = "img2img"
    modalities: list[str] = field(default_factory=lambda: list(_DEFAULT_MODALITIES))
    output_config: ImageOutputConfig = field(default_factory=ImageOutputConfig)
    endpoints: list[EndpointConfig] = field(default_factory=list)


@dataclass
class PluginConfig:
    fallback_to_event_images: bool = True
    default_image_config: ImageOutputConfig = field(default_factory=ImageOutputConfig)
    model_groups: list[ModelGroupConfig] = field(default_factory=list)
    request: RequestConfig = field(default_factory=RequestConfig)
    image: ImageConfig = field(default_factory=ImageConfig)
    quota: QuotaConfig = field(default_factory=QuotaConfig)

    @classmethod
    def from_dict(cls, data: Any) -> PluginConfig:
        """从 AstrBot 的配置 dict 构造 PluginConfig。"""
        defaults = cls()

        ic_data = _as_dict(_get(data, "default_image_config", {}))
        rq_data = _as_dict(_get(data, "request", {}))
        im_data = _as_dict(_get(data, "image", {}))
        qt_data = _as_dict(_get(data, "quota", {}))

        default_image_config = ImageOutputConfig(
            aspect_ratio=_str(
                ic_data.get("aspect_ratio"),
                defaults.default_image_config.aspect_ratio,
            ),
            image_size=_str(
                ic_data.get("image_size"),
                defaults.default_image_config.image_size,
            ),
        )

        # 优先级：字段存在 → 使用新格式（即使为空也不回退）；字段不存在 → 旧格式迁移
        raw_groups = _get(data, "model_groups", _MISSING)
        if raw_groups is not _MISSING:
            model_groups = _parse_model_groups(raw_groups, default_image_config)
        else:
            model_groups = _migrate_legacy_config(data, default_image_config)

        return cls(
            fallback_to_event_images=_bool(
                _get(data, "fallback_to_event_images"),
                defaults.fallback_to_event_images,
            ),
            default_image_config=default_image_config,
            model_groups=model_groups,
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
            quota=QuotaConfig(
                enabled=_bool(qt_data.get("enabled"), defaults.quota.enabled),
                daily_limit=max(
                    1,
                    _int(qt_data.get("daily_limit"), defaults.quota.daily_limit),
                ),
                reset_hour=min(
                    23,
                    max(
                        0,
                        _int(qt_data.get("reset_hour"), defaults.quota.reset_hour),
                    ),
                ),
                whitelist=_str_set_lines(qt_data.get("whitelist")),
            ),
        )


# ── 模型组解析 ────────────────────────────────────────────


def _parse_model_groups(
    value: Any,
    default_output: ImageOutputConfig,
) -> list[ModelGroupConfig]:
    """解析 template_list 返回的模型组列表。"""
    if value is None:
        return []
    # 兼容 JSON 字符串（降级方案）
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("[gen_img] model_groups JSON 解析失败")
            return []
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []

    groups: list[ModelGroupConfig] = []
    seen_names: set[str] = set()

    for index, item in enumerate(value, start=1):
        group_data = _as_dict(item)
        if not group_data:
            continue

        group_name = _str(group_data.get("group_name"), "").strip()
        if not group_name:
            group_name = f"group-{index}"
            logger.warning(f"[gen_img] 模型组未命名，自动分配: {group_name}")

        if group_name in seen_names:
            logger.warning(f"[gen_img] 跳过重复的模型组名: {group_name}")
            continue
        seen_names.add(group_name)

        support_img2img = _bool(group_data.get("support_img2img"), True)
        support_txt2img = _bool(group_data.get("support_txt2img"), False)

        # 解析默认操作类型并校验一致性
        default_op = _str(group_data.get("default_operation"), "img2img").strip()
        if default_op not in {"img2img", "txt2img"}:
            default_op = "img2img"
        if default_op == "img2img" and not support_img2img and support_txt2img:
            default_op = "txt2img"
        elif default_op == "txt2img" and not support_txt2img and support_img2img:
            default_op = "img2img"

        # 解析输出参数覆盖
        ar_override = _str(group_data.get("aspect_ratio_override"), "inherit").strip()
        isz_override = _str(group_data.get("image_size_override"), "inherit").strip()

        groups.append(
            ModelGroupConfig(
                group_name=group_name,
                group_description=_str(
                    group_data.get("group_description"), ""
                ).strip(),
                guide=_str(group_data.get("guide"), "").strip(),
                support_img2img=support_img2img,
                support_txt2img=support_txt2img,
                default_operation=default_op,
                modalities=_str_list(
                    group_data.get("modalities"),
                    _DEFAULT_MODALITIES,
                ),
                output_config=ImageOutputConfig(
                    aspect_ratio=(
                        default_output.aspect_ratio
                        if ar_override == "inherit"
                        else ar_override
                    ),
                    image_size=(
                        default_output.image_size
                        if isz_override == "inherit"
                        else isz_override
                    ),
                ),
                endpoints=_parse_endpoints(group_data.get("endpoints", [])),
            )
        )

    return groups


def _auto_endpoint_name(seen: set[str], start: int = 1) -> tuple[str, int]:
    """生成不与已有名称冲突的自动端点名，返回 (名称, 下一起始序号)。"""
    idx = max(1, start)
    while f"endpoint-{idx}" in seen:
        idx += 1
    return f"endpoint-{idx}", idx + 1


def _parse_endpoints_text(raw: str) -> list[EndpointConfig]:
    """解析多行文本格式的端点列表。

    每行格式：
        名称 | base_url | api_key | model   （4 段）
        base_url | api_key | model           （3 段，自动编号）
    空行和 # 开头的行忽略。
    """
    endpoints: list[EndpointConfig] = []
    seen: set[str] = set()
    next_idx = 1

    for line_no, raw_line in enumerate(raw.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        parts = [p.strip() for p in line.split("|")]

        if len(parts) == 3:
            base_url, api_key, model = parts
            ep_name, next_idx = _auto_endpoint_name(seen, next_idx)
        elif len(parts) == 4:
            ep_name, base_url, api_key, model = parts
            if not ep_name:
                ep_name, next_idx = _auto_endpoint_name(seen, next_idx)
        else:
            # 日志仅记行号，不输出原文（含 api_key）
            logger.warning(
                f"[gen_img] endpoints 第 {line_no} 行格式错误"
                f"（期望 3 或 4 段，实际 {len(parts)} 段），已跳过"
            )
            continue

        if not base_url or not model:
            logger.warning(
                f"[gen_img] endpoints 第 {line_no} 行缺少地址或模型名，已跳过"
            )
            continue

        if ep_name in seen:
            logger.warning(f"[gen_img] 跳过重复的端点名: {ep_name}")
            continue
        seen.add(ep_name)

        endpoints.append(
            EndpointConfig(
                name=ep_name,
                enabled=True,
                api_key=api_key,
                base_url=base_url,
                model=model,
            )
        )

    return endpoints


def _parse_endpoints(value: Any) -> list[EndpointConfig]:
    """解析端点列表，兼容多行文本、list[dict] 和 JSON 字符串。"""
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        # 以 [ 或 { 开头的字符串优先尝试 JSON 解析（旧格式兼容）
        if raw[0] in "[{":
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("[gen_img] endpoints JSON 解析失败，回退为多行文本解析")
            else:
                return _parse_endpoints(parsed)
        # 非 JSON 字符串按多行文本格式解析
        return _parse_endpoints_text(raw)

    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []

    endpoints: list[EndpointConfig] = []
    seen: set[str] = set()
    next_idx = 1

    for item in value:
        ep_data = _as_dict(item)
        if not ep_data:
            continue

        ep_name = _str(ep_data.get("name"), "").strip()
        if not ep_name:
            ep_name, next_idx = _auto_endpoint_name(seen, next_idx)

        if ep_name in seen:
            logger.warning(f"[gen_img] 跳过重复的端点名: {ep_name}")
            continue
        seen.add(ep_name)

        endpoints.append(
            EndpointConfig(
                name=ep_name,
                enabled=_bool(ep_data.get("enabled"), True),
                api_key=_str(ep_data.get("api_key"), ""),
                base_url=_str(ep_data.get("base_url"), ""),
                model=_str(ep_data.get("model"), ""),
            )
        )

    return endpoints


# ── 旧配置迁移 ────────────────────────────────────────────


def _migrate_legacy_config(
    data: Any,
    default_output: ImageOutputConfig,
) -> list[ModelGroupConfig]:
    """将旧的 openrouter/newapi 双槽位配置迁移为单个默认模型组。"""
    legacy_defaults = {
        "openrouter": EndpointConfig(
            name="openrouter",
            enabled=True,
            base_url="https://openrouter.ai/api/v1/chat/completions",
            model="google/gemini-3.1-flash-image-preview",
        ),
        "newapi": EndpointConfig(name="newapi", enabled=False),
    }

    # 检测是否存在旧配置字段
    has_legacy = False
    for key in legacy_defaults:
        if _get(data, key, None) is not None:
            has_legacy = True
            break
    if not has_legacy:
        return []

    endpoints: list[EndpointConfig] = []
    for key, default_ep in legacy_defaults.items():
        raw = _get(data, key, None)
        if raw is None:
            continue
        ep_data = _as_dict(raw)
        endpoints.append(
            EndpointConfig(
                name=default_ep.name,
                enabled=_bool(ep_data.get("enabled"), default_ep.enabled),
                api_key=_str(ep_data.get("api_key"), default_ep.api_key),
                base_url=_str(ep_data.get("base_url"), default_ep.base_url),
                model=_str(ep_data.get("model"), default_ep.model),
            )
        )

    if not endpoints:
        return []

    logger.info("[gen_img] 检测到旧配置格式，已自动迁移为默认模型组")
    return [
        ModelGroupConfig(
            group_name="default",
            group_description="默认图片生成模型组（从旧配置迁移）",
            support_img2img=True,
            support_txt2img=False,
            default_operation="img2img",
            output_config=ImageOutputConfig(
                aspect_ratio=default_output.aspect_ratio,
                image_size=default_output.image_size,
            ),
            endpoints=endpoints,
        )
    ]
