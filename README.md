# astrbot_plugin_gen_img

AstrBot 图片生成插件 — 通过 LLM Agent 工具调用实现图片生成/编辑，支持动态模型组、多端点降级路由、文生图与图生图。

## 工作原理

本插件注册为 AstrBot 的 **LLM FunctionTool**（工具名：`gen_img`）。Agent 根据用户意图自动选择合适的模型组并调用：

```
用户: "把这张图变成水彩画" + 附图
  → Agent 推理，选择模型组 style_transfer
  → 首次可不传 prompt，获取该模型组的 prompt 构建指南（guide）
  → Agent 按指南构造 prompt
  → 调用 gen_img(model_group="style_transfer", prompt=..., image_urls=[...])
  → 插件在模型组内按端点顺序尝试，第一个失败自动降级到下一个
  → 直接发送结果图给用户

用户: "画一只赛博朋克风的猫"
  → Agent 选择支持 txt2img 的模型组
  → 调用 gen_img(model_group="txt2img", prompt=..., operation="txt2img")
  → 无需参考图，直接生成
```

## 安装

将本仓库克隆到 AstrBot 的插件目录：

```bash
cd <astrbot>/data/plugins
git clone <repo_url> astrbot_plugin_gen_img
```

重启 AstrBot 后，在管理面板中配置模型组和端点。

## 配置说明

所有配置通过 AstrBot 管理面板完成。

### 模型组（model_groups）

插件支持配置多个**模型组**，每个模型组代表一种图片生成能力。在管理面板中通过模板列表添加。

每个模型组包含：

| 字段 | 说明 |
|------|------|
| `group_name` | 唯一标识，Agent 通过此名称选择模型组（如 `style_transfer`、`txt2img`） |
| `group_description` | 简短描述，显示在 Agent 工具说明中 |
| `guide` | Prompt 构建指南，教 Agent 如何为此模型写出最优 prompt。留空表示无需额外指导 |
| `support_img2img` | 是否支持图生图 |
| `support_txt2img` | 是否支持文生图 |
| `default_operation` | Agent 未指定 operation 时的默认值 |
| `aspect_ratio_override` | 宽高比覆盖，`inherit` 表示继承全局默认值 |
| `image_size_override` | 分辨率覆盖，`inherit` 表示继承全局默认值 |

### 端点（endpoints）

每个模型组内可配置多个 API 端点，按顺序降级：

| 字段 | 说明 |
|------|------|
| `name` | 端点名称，用于日志显示（如 `openrouter`、`newapi-backup`） |
| `enabled` | 是否启用 |
| `api_key` | API Key（无需 Bearer 前缀） |
| `base_url` | Chat Completions 端点地址（如 `https://openrouter.ai/api/v1/chat/completions`） |
| `model` | 模型标识（如 `google/gemini-3.1-flash-image-preview`） |

### 全局默认参数

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `default_image_config.aspect_ratio` | 生成图宽高比（`1:1`、`16:9` 等，`default` 不指定） | `default` |
| `default_image_config.image_size` | 生成图分辨率（`1K`、`2K`，`default` 不指定） | `1K` |

### 其他配置

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `fallback_to_event_images` | Agent 未传图片时从消息中自动提取 | `true` |
| `request.timeout` | 请求超时秒数 | `120` |
| `request.max_retry` | 每个端点的重试次数 | `2` |
| `image.max_input_images` | 最大输入图片数 | `3` |
| `image.max_input_mb` | 单张图片大小限制（MB） | `20` |
| `image.allow_reply_image` | 是否读取引用消息中的图片 | `true` |

## 降级策略

每个模型组内的端点按配置顺序依次尝试。端点内部支持重试，跨端点支持自动降级：

- **重试**（当前端点内）：超时、408、5xx、网络异常
- **降级**（切换到下一个端点）：429 限流、响应无图、图片下载失败、401/403/404
- **终止**（返回错误）：所有端点均失败

## 工具参数

Agent 调用 `gen_img` 时可传入以下参数：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `model_group` | string | 多组时必填 | 目标模型组名称。仅有一个模型组时可省略 |
| `prompt` | string | 否 | 图片生成指令。不传时返回所选模型组的 guide |
| `image_urls` | array[string] | 否 | 参考图片路径/URL 列表（本地路径、HTTP URL 均可） |
| `operation` | string | 否 | 操作类型（`img2img` / `txt2img`），不传时使用模型组默认值 |

### 两阶段调用

1. **获取指南**：只传 `model_group`，不传 `prompt` → 返回该模型组的 prompt 构建指南
2. **执行生成**：传入 `model_group` + `prompt` → 调用 API 生成图片并发送给用户

对于没有配置 guide 或 description 已足够直观的模型组，Agent 可跳过第一阶段直接生成。

## 旧配置兼容

从 v0.1.x 升级时，插件会自动识别旧的 `openrouter`/`newapi` 配置并迁移为一个名为 `default` 的模型组。建议升级后在管理面板中重新配置模型组。

## 依赖

- AstrBot >= 4.10
- aiohttp >= 3.9.0
- Pillow（可选，GIF 输入时需要）
