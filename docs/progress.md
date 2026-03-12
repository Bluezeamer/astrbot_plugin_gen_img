# astrbot_plugin_gen_img — 开发进度

> 最后更新：2026-03-12 07:11

## 项目概况

AstrBot 插件，通过 `@llm_tool()` 装饰器注册图片生成工具（`gen_img`），供 Agent 调用。支持动态模型组配置，每个模型组可挂载多个 OpenAI 兼容端点并按顺序降级路由。支持图生图（img2img）和文生图（txt2img），提供渐进式 guide 加载机制教 Agent 针对不同模型构建最优 prompt。

- 设计文档/计划：`.claude/plans/piped-greeting-seahorse.md`
- 参考插件（仅本地参考，已 gitignore）：`astrbot_plugin_big_banana/`

## 技术栈

- **运行环境**：AstrBot v4.10+ 插件体系
- **语言**：Python 3.10+
- **异步 HTTP**：aiohttp >= 3.9.0
- **图片处理**：Pillow（可选，用于 GIF 转 PNG）
- **API 协议**：OpenAI Chat Completions 兼容（支持 OpenRouter、NewAPI、火山引擎等）
- **配置系统**：AstrBot `template_list`（模型组层）+ 多行文本（端点层）
- **配额存储**：sqlite3（Python 标准库，零额外依赖）

## 目录结构

```
astrbot_plugin_gen_img/
├── main.py                  # 插件入口：RuntimeModelGroup 装配、@llm_tool 注册、多 router 映射
├── metadata.yaml            # 插件元数据（v0.2.0）
├── _conf_schema.json        # AstrBot 管理面板配置 schema（模型组 template_list + 端点多行文本）
├── README.md                # 项目说明文档
├── requirements.txt         # aiohttp>=3.9.0
├── .gitignore               # 忽略参考插件、__pycache__、temp
├── core/
│   ├── __init__.py
│   ├── config.py            # 配置 dataclass（PluginConfig, EndpointConfig, ModelGroupConfig, QuotaConfig 等）+ 旧配置迁移
│   ├── quota.py             # 用户配额管理：SQLite + threading.Lock，先扣后退模式
│   ├── image_extract.py     # 图片解析：本地路径/URL/data URI → (mime, base64)
│   ├── provider.py          # OpenAI 兼容请求构造 + 多格式响应解析
│   └── router.py            # 端点路由：重试 + 降级调度
└── docs/
    └── progress.md          # 本文件
```

## 已完成

### 动态模型组架构（v0.2.0 重构）
- `_conf_schema.json`：从固定 `openrouter`/`newapi` 双槽位改为 `model_groups`（template_list），模板 `openai_compatible` 含 group_name/group_description/guide/support_img2img/support_txt2img/default_operation/aspect_ratio_override/image_size_override，`endpoints` 为多行文本（`type: "text"`）
- `core/config.py`：`ProviderConfig` → `EndpointConfig`（新增 name 字段），新增 `ModelGroupConfig`，`PluginConfig` 移除 openrouter/newapi 改为 `model_groups: list[ModelGroupConfig]`，新增 `_parse_model_groups`/`_parse_endpoints`/`_migrate_legacy_config`，`from_dict` 用 `_MISSING` 哨兵区分"字段不存在"和"字段为空"
- `core/provider.py`：构造函数签名 `ProviderConfig` → `EndpointConfig`，移除未使用的 `image_config` 参数
- `main.py`：新增 `RuntimeModelGroup` dataclass，`self.router` → `self.runtime_groups: dict[str, RuntimeModelGroup]`，遍历 model_groups 构建多 router，provider name 格式 `{group_name}/{ep_name}`
- 工具通过 `@llm_tool(name="gen_img")` 装饰器注册（取代旧的 `FunctionTool` 子类 + `add_llm_tools()`），支持插件粒度权限控制。两阶段调用（无 prompt → 返回 guide，有 prompt → 执行生成）、operation 校验、`_resolve_images` 图片解析、配额检查均在 `main.py` 内实现
- `metadata.yaml`：版本 0.1.0 → 0.2.0
- `README.md`：完整更新适配新架构

### 端点配置文本化
- `_conf_schema.json`：endpoints 从嵌套 `template_list` 改为 `type: "text"` 大文本框，default 含注释示例
- `core/config.py`：新增 `_auto_endpoint_name` 和 `_parse_endpoints_text` 函数，`_parse_endpoints` 改为先判断 JSON 再 fallback 多行文本，旧 `list[dict]` 路径保持兼容
- `README.md`：端点配置说明从表格改为多行文本格式说明

### modalities 配置化 + 响应解析增强
- `_conf_schema.json`：模型组新增 `modalities` 字段（string，默认 `image,text`），纯出图模型设为 `image`
- `core/config.py`：新增 `_DEFAULT_MODALITIES` 常量、`_str_list()` 辅助函数（支持逗号分隔/JSON 数组/中文逗号），`ModelGroupConfig` 新增 `modalities` 字段，`_parse_model_groups` 解析 modalities
- `core/provider.py`：`__init__` 新增 `modalities` 参数，`_build_payload` 使用配置值替代硬编码 `["image","text"]`；`_parse_response` 新增顶层 `data[]` 格式解析（兼容 `/v1/images/generations` 风格）；`_extract_from_text` 新增 JSON 字符串和裸 HTTP URL 解析；`image_url` 兼容字符串值；文本清理覆盖新格式（`_is_json` 辅助函数）
- `main.py`：传递 `group_cfg.modalities` 给 provider
- `README.md`：模型组表格新增 `modalities` 字段说明

### 用户配额系统
- `core/quota.py`（新建）：`QuotaManager` 类，SQLite 单表 `usage(user_id, date_key, count)`，`threading.Lock` 串行化，异步接口 `asyncio.to_thread`。采用"先扣后退"模式（`try_acquire` 原子检查+扣减 → `refund` 按 date_key 精确回退），防止并发绕过。`QuotaExhausted` 异常携带 used/limit。`_ensure_open()` 保证关闭后安全拒绝请求
- `core/config.py`：新增 `QuotaConfig` dataclass（enabled/daily_limit/reset_hour/whitelist）、`_str_set_lines()` 多行文本→set 辅助函数、`PluginConfig.quota` 字段、`from_dict` 解析 quota 配置
- `_conf_schema.json`：新增 `quota` 配置区块（object 类型，含 enabled/daily_limit/reset_hour/whitelist 四项）
- `main.py`：`initialize()` 中按 `quota.enabled` 创建 `QuotaManager`（try/except 降级），`terminate()` 中关闭。数据库路径 `StarTools.get_data_dir() / "quota.sqlite3"`
- `main.py`：`gen_img()` 方法中图片校验后、生成前调用 `try_acquire`，整个生成+发送流程包在 `try/finally` 中统一 refund。成功时返回剩余额度信息
- `README.md`：新增"用户配额（quota）"配置说明文档

### 基础设施（v0.1.0）
- 图片提取（`core/image_extract.py`）：本地路径/URL/data URI 统一解析，MIME 魔数检测，GIF 转 PNG，大小校验
- 提供商请求（`core/provider.py`）：四层响应解析（images[] / markdown data URI / markdown URL / 结构化 content part），输出图下载限制 50MB
- 端点路由（`core/router.py`）：按优先级遍历 provider，每个内部重试 max_retry 次，三条路径（重试/降级/终止）

## 关键决策

| 决策 | 原因 |
|------|------|
| 单 Tool + model_group 参数（方案 A） | 比多 Tool 简单，靠 description 引导 Agent 选择 |
| @llm_tool 装饰器注册（取代 FunctionTool 子类） | 解决 add_llm_tools() 在 AstrBot 权限过滤中 handler_module_path 不匹配导致的插件粒度控制失效问题 |
| guide 渐进式加载（给 Agent 看） | 不污染系统提示词，不拼入最终 API 请求，按需展开 |
| endpoints 多行文本格式 | 嵌套 template_list 在 WebUI 不可用，改为 `名称\|地址\|密钥\|模型` 多行文本，编辑直观 |
| 旧配置自动迁移 | from_dict 识别 openrouter/newapi 合成为 default 模型组，平滑升级 |
| `_MISSING` 哨兵区分字段缺失 | 避免 model_groups=[] 时误回退旧格式 |
| txt2img 跳过图片提取 | 即使消息中有图也忽略，防止语义混淆 |
| 火山引擎走 OpenAI Chat Completions 兼容 | 无需特殊适配器，配置 base_url 和 model 即可接入 |
| Agent 主动传入图片路径 + fallback 消息提取 | 支持多轮对话 + 兼容单轮简单场景 |
| 401/403/404 也触发降级 | 两端点 key/model 可能不同，一个失败不代表另一个也失败 |
| modalities 模型组级配置 | SeedDream 等 image-only 模型需要 `["image"]`，Gemini 等需要 `["image","text"]`，不可硬编码 |
| 响应解析兼容 data[] 格式 | NewAPI 中转可能返回 `/v1/images/generations` 风格响应，需兼容顶层 `data[{url/b64_json}]` |
| 配额"先扣后退"模式 | check→生成→consume 非原子会被并发绕过，改为 try_acquire 原子扣减 + 失败 refund |
| 配额 date_key 精确回退 | try_acquire 返回 date_key，refund 按此回退，防止跨 reset_hour 边界退错周期 |
| QuotaManager 初始化失败降级 | SQLite 打不开不应阻塞插件启动，降级为不限额并记录 warning |
| 工具返回文本注入运行时模型组信息 | 静态 docstring 无法包含动态模型组列表，改为在关键决策点（多组未指定、guide 返回、操作不支持等）动态注入模型组名称/操作类型/描述等信息 |
| 所有失败路径附加"禁止编造图片"约束 | 防止 Agent 生成失败后幻觉出虚假图片 URL |

## 待完成

- [ ] 端到端联调：SeedDream 模型组设置 `modalities: image` 后重新验证（OpenRouter + NewAPI）
- [x] ~~验证嵌套 template_list 在 AstrBot WebUI 中的渲染效果~~ → 已确认不支持，改为多行文本方案
- [x] ~~modalities 硬编码导致 image-only 模型 404~~ → 已改为模型组级配置
- [ ] NewAPI 中转联调：确认 SeedDream 响应格式是否被 data[] / 裸 URL 解析覆盖
- [ ] 考虑 Pillow 是否需要加入 `requirements.txt`
- [ ] 本地路径读取安全性：考虑目录白名单
- [ ] 日志脱敏：评估路径/URL 前缀脱敏需求
- [ ] 端到端联调验证插件粒度权限控制：在 AstrBot 管理面板只允许本插件，确认 gen_img 可正常调用

## 架构速查

```
用户消息
    ↓
AstrBot LLM Agent 推理
    ↓
查看 gen_img @llm_tool description → 了解工具用途
    ↓
┌─ 首次使用？不传 prompt ──→ 返回该模型组的 guide（prompt 构建指南）
│                              ↓
│                         Agent 阅读 guide，学会如何写 prompt
│                              ↓
└─ 调用 gen_img(model_group=..., prompt=..., operation=..., image_urls=...)
    ↓
┌─ img2img ──→ resolve_image_refs() ──→ [(mime, b64), ...]
└─ txt2img ──→ images = []（跳过图片提取）
    ↓
quota_manager.try_acquire(user_id) ──→ 配额不足？返回错误文本
    ↓（配额已预扣）
RuntimeModelGroup.router.generate()
    ↓
┌─ endpoint 1 (主) ──→ 成功？返回
└─ 失败/降级 ──→ endpoint 2 (备) ──→ ...
                                    └─ 全部失败 → 错误文本
    ↓
event.send(MessageChain) → 直接发图给用户
    ↓（失败则 quota_manager.refund(user_id, date_key)）
Tool 返回确认文本 + 剩余额度 → Agent 继续文字回复
```

## Changelog

### 2026-03-07 14:50
本轮完成：从零搭建完整插件，含配置/工具/图片提取/提供商请求/路由降级全链路，通过 Codex review 并修复所有 critical/warning 问题
主体更新：新建文档（项目概况、技术栈、目录结构、已完成、关键决策、待完成、架构速查）
下一步：配置 OpenRouter API Key 进行端到端联调测试

### 2026-03-11 08:54
本轮完成：重构为动态模型组架构，6 文件 +676/-238 行。支持多模型组并列、组内端点降级、文生图、渐进式 guide、旧配置自动迁移。修复 Codex review 发现的 _MISSING 哨兵缺失和 _resolve_images 输入归一化问题
主体更新：项目概况、技术栈、目录结构、已完成（新增 v0.2.0 重构区块）、关键决策、待完成、架构速查
下一步：端到端联调测试 + 验证嵌套 template_list 在 WebUI 的渲染效果

### 2026-03-11 09:43
本轮完成：确认 AstrBot WebUI 不支持嵌套 template_list 渲染，将 endpoints 从嵌套 template_list 改为多行文本方案（每行 `名称|地址|密钥|模型`）。涉及 3 文件：_conf_schema.json、core/config.py、README.md。Codex review 无 critical 问题
主体更新：技术栈、目录结构、已完成（v0.2.0 描述修正 + 新增端点配置文本化区块）、关键决策、待完成
下一步：端到端联调测试

### 2026-03-11 23:54
本轮完成：首次端到端联调 SeedDream 4.5，发现并修复两个问题——modalities 硬编码导致 OpenRouter 404、响应解析不覆盖 data[]/裸 URL/JSON 字符串格式。涉及 5 文件，Codex review 无 critical
主体更新：已完成（新增 modalities + 响应解析区块）、关键决策（+2）、待完成
下一步：SeedDream 模型组设 `modalities: image` 后重新联调验证

### 2026-03-12 01:54
本轮完成：实现用户配额系统，新建 `core/quota.py` + 修改 5 文件 +180 行。SQLite "先扣后退"模式，Codex 三轮 review 修复全部 critical/warning（Pydantic 类型、并发绕过、date_key 跨日、异常路径漏退、初始化降级）
主体更新：技术栈（+配额存储）、目录结构（+quota.py）、已完成（+用户配额系统）、关键决策（+4）、待完成（+配额联调）、架构速查（+配额环节）
下一步：提交 git + 配额功能端到端联调验证

### 2026-03-12 15:00
本轮完成：工具注册机制从 `FunctionTool` 子类 + `add_llm_tools()` 迁移到 `@llm_tool()` 装饰器方式，修复 AstrBot 插件粒度权限过滤无法识别工具归属的问题。删除 `core/tool.py`，逻辑迁入 `main.py`（4 文件 +253/-394 行）。Codex review 通过
主体更新：项目概况（注册方式描述）、目录结构（移除 tool.py）、已完成（动态模型组架构/配额系统描述更新）、关键决策（+@llm_tool 迁移）、待完成（+权限控制联调）
下一步：提交 git + 端到端联调验证插件粒度权限控制

### 2026-03-12 07:11
本轮完成：增强工具提示词引导，解决 Agent 多模型组盲选、默认 operation 误判、失败后幻觉图片三个实际问题。新增 `_build_groups_overview()`/`_build_group_info()` 辅助方法，优化全部返回路径的提示文本，`None` 参数防御性处理。Codex review 修复条件化输出等问题
主体更新：关键决策（+2：运行时信息注入、禁止编造约束）
下一步：部署验证提示词引导效果 + 端到端联调
