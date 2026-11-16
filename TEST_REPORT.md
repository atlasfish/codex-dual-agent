# v0.2 本地测试记录

2026-11-16，Linux / Python 3.11；pytest：**123 passed**。
compileall 与配置/清单解析检查通过。测试不使用真实供应商账号或私人服务器。

新增覆盖：Chat Completions/Responses 工具循环、供应商附加字段/推理项回传、
重复调用 ID 拒绝、预算终止、真实 Shell 子进程、超时与取消、输出限额、
Skill发现/加载/脚本执行与环境继承、REST认证本地展开、禁止自动重定向、
搜索 HTML/JSON/自定义脚本适配、敏感环境值脱敏、直接编辑入口限制、
成功任务 payload 清理、只保存工具审计元数据、跨平台快照辅助函数与分页读取。
新增真实Worker → 本机HTTP中转 → Shell → 工具结果回传的端到端验证。
原有93项测试继续通过，含真实Hook/Worker、MCP stdio与本机HTTP联动。

未验证：真实 Codex Desktop、本地中转对 Gemini tool-call 的透传、私人 Skill
依赖与服务器、DuckDuckGo 公网可用性、Windows/macOS 实机。GitHub CI 状态单独
查看 Actions，不以本报告声称已通过。工具预算与软约束不是安全沙盒测试证明。
