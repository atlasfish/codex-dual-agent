# v0.2.0 — 2026-11-16

- 从纯文本第二意见升级为受信任本地 Agent；默认 Shell、Skill、HTTP/REST 和搜索。
- Advisor 是软角色，不再承诺严格只读；禁用直接编辑入口而不是禁止所有 Shell。
- 两种 relay 协议的工具循环、预算、取消、重复调用保护；无自动副作用重放。
- 本地 Skill 发现/加载/脚本，默认搜索及可配置搜索脚本/SearXNG。
- 工具输出不长期落库，成功 job 清空重复 payload；非Gemini回合不自动打包改动文件。
- 加入Windows安装路径/PowerShell hooks及可移植快照；尚待Windows Desktop实测。
- 保留原有路由、共享历史和单次隔离补丁提案。完整GC/内容去重仍待实现。

# Changelog

## 0.1.0 — 2026-11-16

Initial implementation: strict prefix routing, independent detached Gemini jobs,
Chat Completions / Responses relay adapters, SQLite session ledger, immutable
selected-file snapshots, blind-first GPT opinion collection, original peer lookup,
bounded Stop continuation with turn aliasing, cancellation/deadlines, and one-use
isolated edit proposals. Includes tools-only MCP stdio, local plugin manifests,
installer, private-repository publish helper, tests and CI configuration.

Known limits: no native dual-bubble Desktop UI; no real Desktop/user-relay smoke
run yet; no direct Gemini edits to main workspace, native Windows support,
streaming provider output, search grounding, full MCP SDK interoperability audit,
or automatic long-term summarization. Publishing to GitHub is not implied by
this changelog or the existence of the local source archive.
