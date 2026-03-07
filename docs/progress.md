# astrbot_plugin_gen_img — 开发进度

> 最后更新：2026-03-07 14:50

## 项目概况

AstrBot 插件，通过 LLM FunctionTool 机制注册图生图工具（`gen_img`），供 Agent 调用 nanobanana2（实际模型为 `google/gemini-3.1-flash-image-preview`）执行图片生成。API 走 OpenAI 兼容格式，支持 OpenRouter（主）→ NewAPI 中转（备用）自动降级。

- 设计文档/计划：`.claude/plans/iterative-popping-llama.md`
- 参考插件（仅本地参考，已 gitignore）：`astrbot_plugin_big_banana/`

## 技术栈

- **运行环境**：AstrBot v4.10+ 插件体系
- **语言**：Python 3.10+
- **异步 HTTP**：aiohttp >= 3.9.0
- **图片处理**：Pillow（可选，用于 GIF 转 PNG）
- **API 协议**：OpenAI Chat Completions 兼容（OpenRouter 扩展 `modalities` + `image_config`）

## 目录结构

```
astrbot_plugin_gen_img/
├── main.py                  # 插件入口：@register、初始化、工具注册、terminate
├── metadata.yaml            # 插件元数据（v0.1.0）
├── _conf_schema.json        # AstrBot 管理面板配置 schema
├── requirements.txt         # aiohttp>=3.9.0
├── .gitignore               # 忽略参考插件、__pycache__、temp
├── core/
│   ├── __init__.py
│   ├── config.py            # 配置 dataclass（PluginConfig, ProviderConfig, ImageOutputConfig 等）
│   ├── tool.py              # FunctionTool 定义（GenImgTool），Agent 调用入口
│   ├── image_extract.py     # 图片解析：本地路径/URL/data URI → (mime, base64)
│   ├── provider.py          # OpenAI 兼容请求构造 + 多格式响应解析
│   └── router.py            # 提供商路由：重试 + 降级调度
└── docs/
    └── progress.md          # 本文件
```

## 已完成

### 插件骨架
- `main.py`：`@register` 注册、`initialize()` 创建 aiohttp session + 装配 provider + 注册 LLM 工具、`terminate()` 关闭 session + 注销工具
- `metadata.yaml`：name/display_name/version/astrbot_version
- `_conf_schema.json`：`fallback_to_event_images`、`default_image_config`（aspect_ratio/image_size）、`openrouter`/`newapi` 提供商配置、`request`（timeout/max_retry）、`image`（max_input_images/max_input_mb/allow_reply_image）
- `core/config.py`：与 schema 对应的强类型 dataclass，`PluginConfig.from_dict()` 从 AstrBot 配置构造

### LLM Tool（`core/tool.py`）
- 工具名：`gen_img`，注册为 `FunctionTool[AstrAgentContext]`
- 参数：`prompt`（必填）、`image_urls`（可选，Agent 主动传入本地路径/URL）、`operation`（默认 img2img）
- 图片来源：优先 Agent 传入的 `image_urls` → fallback 从消息事件 Image 组件提取
- 成功后直接 `event.send()` 发图，Tool 返回确认文本防止 Agent 循环调用
- 顶层异常捕获，operation 参数校验

### 图片提取（`core/image_extract.py`）
- `resolve_image_refs()`：统一处理本地路径、HTTP URL、data URI
- `parse_data_uri()`：带 base64 合法性验证和大小校验（修复 Codex review critical）
- `download_image()`：带 Content-Length 预检和流式大小校验
- `read_local_image()`：本地文件读取 + 大小校验
- `extract_image_refs_from_event()`：从 message_obj.message 和引用消息中提取图片 URL
- MIME 魔数检测、GIF 第一帧转 PNG

### 提供商请求（`core/provider.py`）
- `OpenAICompatibleProvider`：统一的请求构造和响应解析
- 请求：`modalities: ["image", "text"]`、`image_config`（OpenRouter 特有）
- 响应解析覆盖四种格式：
  1. OpenRouter `message.images[]` 结构化数组
  2. markdown data URI
  3. markdown HTTP URL（下载后编码）
  4. 结构化 content part（image_url / b64_json）
- 响应图去重（seen set）
- 输出图下载独立限制 50MB（与输入解耦）
- 错误分类：可重试（408/5xx/超时/网络异常）、触发降级（429/无图/下载失败/401-404）、终止

### 提供商路由（`core/router.py`）
- `ProviderRouter`：按优先级遍历 provider，每个内部重试 max_retry 次
- 三条路径：可重试 → 重试当前 provider；触发降级 → 跳到下一个；终止 → 直接返回错误

## 关键决策

| 决策 | 原因 |
|------|------|
| Agent 主动传入图片路径而非插件自行提取 | 支持多轮对话场景，Agent 有上下文主动权 |
| 保留 fallback 从消息事件提取 | 兼容单轮直接发图的简单场景 |
| OpenRouter 响应走 `message.images[]` 优先 | 实测 OpenRouter 图片生成返回格式非标准 OpenAI |
| 401/403/404 也触发降级 | 两个提供商的 key/model 可能不同，一个失败不代表另一个也失败 |
| 输出图下载限制独立于输入（50MB vs 用户配置） | 生成图通常比输入大，避免误判为失败触发降级 |
| data URI 输入做完整解码+大小校验 | Codex review 发现原始实现绕过了 max_input_mb 限制 |

## 待完成

- [ ] 实际联调测试：配置 OpenRouter API Key 后在 AstrBot 中端到端验证
- [ ] NewAPI 中转联调：验证 `image_config` 在 NewAPI 上的行为（可能被忽略，需确认是否报 400）
- [ ] 考虑 Pillow 是否需要加入 `requirements.txt`（当前为可选，GIF 输入场景需要）
- [ ] 本地路径读取安全性：考虑是否需要添加目录白名单
- [ ] 后续扩展：文生图（txt2img）支持
- [ ] 日志脱敏：评估是否需要对路径/URL 前缀做进一步脱敏处理

## 架构速查

```
用户消息 (带图片)
    ↓
AstrBot LLM Agent 推理
    ↓
调用 gen_img Tool（prompt + image_urls）
    ↓
┌─ image_urls 有值？──→ resolve_image_refs() ──→ [(mime, b64), ...]
└─ 无 → fallback_to_event_images ──→ extract_image_refs_from_event()
                                         ↓
                                   resolve_image_refs()
    ↓
ProviderRouter.generate()
    ↓
┌─ OpenRouter (主) ──→ 成功？返回
└─ 失败/降级 ──→ NewAPI (备) ──→ 成功？返回
                                └─ 全部失败 → 错误文本
    ↓
event.send(MessageChain) → 直接发图给用户
Tool 返回确认文本 → Agent 继续文字回复
```

## Changelog

### 2026-03-07 14:50
本轮完成：从零搭建完整插件，含配置/工具/图片提取/提供商请求/路由降级全链路，通过 Codex review 并修复所有 critical/warning 问题
主体更新：新建文档（项目概况、技术栈、目录结构、已完成、关键决策、待完成、架构速查）
下一步：配置 OpenRouter API Key 进行端到端联调测试
