# astrbot_plugin_gen_img

AstrBot 图片生成插件 — 通过 LLM Agent 工具调用实现图生图，支持 OpenRouter / NewAPI 中转自动降级。

## 工作原理

本插件不提供用户命令，而是注册为 AstrBot 的 **LLM FunctionTool**（工具名：`gen_img`）。当用户发送包含图片的消息并表达编辑意图时，AstrBot 的 LLM Agent 会自动推理并调用本工具：

```
用户发图 + "把这张图变成水彩画"
    → Agent 推理，构造 prompt
    → 调用 gen_img(prompt=..., image_urls=[图片路径])
    → 插件调用 OpenRouter/NewAPI API 生成图片
    → 直接发送结果图给用户
```

## 安装

将本仓库克隆到 AstrBot 的插件目录即可：

```bash
cd <astrbot>/data/plugins
git clone <repo_url> astrbot_plugin_gen_img
```

重启 AstrBot 后，在管理面板中配置 API Key。

## 配置说明

所有配置通过 AstrBot 管理面板完成，配置项如下：

### 提供商配置

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `openrouter.enabled` | 启用 OpenRouter（主提供商） | `true` |
| `openrouter.api_key` | OpenRouter API Key（无需 Bearer 前缀） | 空 |
| `openrouter.model` | 模型标识 | `google/gemini-3.1-flash-image-preview` |
| `newapi.enabled` | 启用 NewAPI 中转（备用提供商） | `false` |
| `newapi.api_key` | NewAPI API Key | 空 |
| `newapi.base_url` | 端点地址（如 `http://host/v1/chat/completions`） | 空 |
| `newapi.model` | 模型名（如 `gemini-3.1-flash-image-preview-2k`） | 空 |

### 图片生成参数

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `default_image_config.aspect_ratio` | 生成图宽高比（`1:1`、`16:9` 等，`default` 不指定） | `default` |
| `default_image_config.image_size` | 生成图分辨率（`1K`、`2K`，`default` 不指定） | `1K` |

### 其他配置

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `fallback_to_event_images` | Agent 未传图片时从消息中自动提取 | `true` |
| `request.timeout` | 请求超时秒数 | `120` |
| `request.max_retry` | 每个提供商的重试次数 | `2` |
| `image.max_input_images` | 最大输入图片数 | `3` |
| `image.max_input_mb` | 单张图片大小限制（MB） | `20` |
| `image.allow_reply_image` | 是否读取引用消息中的图片 | `true` |

## 降级策略

插件按 **OpenRouter → NewAPI** 的优先级尝试生成。每个提供商内部支持重试，跨提供商支持自动降级：

- **重试**（当前提供商内）：超时、408、5xx、网络异常
- **降级**（切换到备用提供商）：429 限流、响应无图、图片下载失败、401/403/404
- **终止**（直接返回错误）：所有提供商均失败

## 工具参数

Agent 调用 `gen_img` 时可传入以下参数：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `prompt` | string | 是 | 图片生成指令 |
| `image_urls` | array[string] | 否 | 参考图片路径/URL 列表（本地路径、HTTP URL 均可） |
| `operation` | string | 否 | 操作类型，默认 `img2img` |

## 依赖

- AstrBot >= 4.10
- aiohttp >= 3.9.0
- Pillow（可选，GIF 输入时需要）
