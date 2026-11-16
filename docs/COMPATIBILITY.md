# 接口与兼容性（2026-11-16）

## 依据

- Hooks / commandWindows: https://learn.chatgpt.com/docs/hooks
- Function calling: https://developers.openai.com/api/docs/guides/function-calling
- Skill 包与脚本: https://developers.openai.com/plugins/build/skills
- MCP stdio: https://modelcontextprotocol.io/specification/2025-06-18/basic/transports

引用文档不等于你的 Desktop 版本已验收。

## 中转

支持 OpenAI-compatible Chat Completions 和 Responses 的文本+function-call循环。
中转必须透传工具定义、调用、ID 和工具结果。Gemini 原生 generateContent 协议不在
此适配器范围。保留返回的供应商附加字段（如 thought_signature），Responses 回传
reasoning items 并请求 encrypted_content。具体中转可能不支持所有字段，需实测。
不支持流式 token、多模态、自动协议降级或原生供应商 grounding 的隐式假设。
搜索由本地工具显式完成，不因模型名字包含 Gemini 就宣称自带实时搜索。

## Skill / Shell

发现本地 .agents/skills、.codex/skills 与 skill_roots。读取标准 YAML frontmatter
和 SKILL.md，脚本经本机 Shell 执行。不能凭文档复制宿主连接器、OAuth 或 MCP。
默认 POSIX /bin/sh 或 Windows PowerShell，可配置 shell_command。
无 PTY/交互登录输入；必须预配置认证。脚本依赖与网络连通性由实际本机提供。
Windows 安装路径和 commandWindows 已实现，本次没有 Windows/GUI 实机验证。

## Hooks / MCP

保留 UserPromptSubmit / Stop / Interrupt / PreToolUse。公开路由仍是文本语义，
不能重写 Desktop 原生模型流或保证两个原生聊天气泡。
PreToolUse 只限制直接编辑入口，不再 blanket-deny shell。
MCP 仍为七工具 stdio 子集，支持 2025-06-18 / 2024-11-05，不冒充完整平台。
安装后须重新检查信任 Hooks；升级不会替你操作 Desktop 设置。

## 默认搜索

DuckDuckGo HTML 是 best-effort 无 key 适配器，不是官方稳定搜索 API。
验证码、限流、HTML 变化都会显式失败。可配置 SearXNG JSON 或本机搜索脚本。
公网搜索及私人 REST/SSH 尚未在用户网络验证。
