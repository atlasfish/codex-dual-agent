# 接口依据与兼容性

文档核对日期：**2026-11-16**。这是实现依据，不代表所有已发行 Desktop 版本都支持这些接口，也不代表本项目已经通过 Desktop 实机测试。

## 官方资料

- Codex Hooks：<https://learn.chatgpt.com/docs/hooks>
- OpenAI 插件构建与清单：<https://developers.openai.com/plugins/build/plugins>
- Codex MCP：<https://developers.openai.com/codex/mcp>
- MCP 2025-06-18 stdio transport：<https://modelcontextprotocol.io/specification/2025-06-18/basic/transports>
- MCP 2025-06-18 lifecycle：<https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle>
- MCP 2025-06-18 tools：<https://modelcontextprotocol.io/specification/2025-06-18/server/tools>

## 使用的 Hooks 契约

| 事件 | 使用字段 | 输出/用途 |
| --- | --- | --- |
| UserPromptSubmit | hook_event_name, session_id, turn_id, cwd, prompt | hookSpecificOutput.additionalContext 注入路由元数据；错误时 decision=block。 |
| Stop | 同一身份字段、last_assistant_message、stop_hook_active | 保存原文；最多一次 decision=block 触发补收结果。 |
| Interrupt | 同一身份字段 | 尽力取消本轮任务；不把部分助手草稿记为最终观点。 |
| PreToolUse | 同一身份字段、tool_name | Gemini-only 回合拒绝一组已知 shell/edit 工具；不是覆盖所有工具的完整权限系统。 |

缺少关键身份字段不能由项目路径或“最近一次任务”猜测代替。插件不依赖 transcript_path 内部 JSON 格式。Hook 能解析文本并影响行为，不等于能够替换 Desktop 主模型、注入原生 Gemini 气泡、拦截所有 UI 路径或保证主模型逐字照抄。

`@gemini` 原文在数据库和 MCP 结果中保真；显示层依赖 GPT 转发，并由 Stop 做一次检查与纠正。超过纠正上限只警告，不应声称绝对可保证 UI 原文。

## 清单与安装

仓库同时提供 portable `plugin.json` / `mcp.json` 和旧式 `.codex-plugin/plugin.json` / `.mcp.json`。Hooks 放在 `hooks/hooks.json`。安装器把用户本机绝对解释器路径写入插件副本，不修改仓库模板，也不依赖 Desktop 的 shell PATH。

本地 marketplace 配置会保留已有其他条目并备份。仓库自带 `.agents/plugins/marketplace.json` 可供开发参考，但直接通过 Git marketplace 安装模板前仍需要安装 Python 运行时；推荐使用 `scripts/install.py`。

**带 Hooks 的插件属于本地/手动安装路径，不以公开目录可发布为既成事实。** 安装后需要用户检查并信任 Hooks；更新后可能需要重新检查。

## MCP 子集

支持 `2025-06-18` 和 `2024-11-05` 的 tools-only stdio：initialize、notifications/initialized、ping、tools/list、tools/call、notifications/cancelled。只宣告 tools 能力；JSON-RPC 按行，stdout 只输出协议消息。

自实现服务不提供 HTTP/SSE、resources、prompts、sampling、elicitation、OAuth 或工具输出流。测试包含初始化、错误输入、取消等待、并发 ping，以及实际 stdio 子进程调用。尚未使用独立官方 SDK 客户端做互操作测试。

## 中转适配边界

只实现 OpenAI-compatible 的 Chat Completions 或 Responses JSON 文本请求。不是 Gemini 原生 generateContent 接口，不支持供应商专有思考参数、文件上传、多模态、流式 token、实时搜索工具或自动切换 API 协议。

连接错误、429/5xx、不兼容 JSON、截断、工具请求和超时都显式失败，不自动重试、不隐式换模型。部分中转对 `max_tokens`、`store` 或消息角色有额外要求，需要在实际中转环境验收并按需扩展适配层。
