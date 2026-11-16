# 本地测试报告

执行日期：2026-11-16。版本：0.1.0。

## 结果

**93 passed，0 failed，0 errors，0 skipped。** 本次上传前重新运行完整 pytest，耗时约 7.94 秒。

- Python：3.13.5。
- 平台：Linux / x86_64。
- 命令：`python -m pytest -q`。
- `python -m compileall -q dual_agent scripts`：通过。
- Wheel：本地无网络构建成功（`--no-deps --no-build-isolation`）。
- Wheel 安装：安装到独立虚拟环境，切换到 `/tmp` 后以 Python `-I` 成功导入，版本为 0.1.0；该验证环境复用预装第三方依赖。
- 安装器与 GitHub 发布脚本：dry-run 通过；没有实际执行 GitHub 创建、push 或更改用户 Desktop。

## 测试模块

| 模块 | 用例数 |
| --- | ---: |
| `tests.test_install_publish` | 6 |
| `tests.test_ledger_hooks` | 13 |
| `tests.test_mcp_e2e` | 3 |
| `tests.test_provider_worker_edits` | 30 |
| `tests.test_routing_context` | 41 |

## 已验证的关键链路

真实子进程集成：启动本机 HTTP 测试服务，再启动 Hook 进程。HTTP 响应被测试屏障暂时挂起时，Hook 已返回，Worker 独立运行；随后经真实 MCP stdio 子进程取回结果。验证请求只发生一次，当前 GPT 草稿未进入 Gemini 输入。

其他覆盖：前缀语法、会话隔离、历史持久化、冻结快照、符号链接/硬链接/路径越界拒绝、受限文件预算、双路独立意见门控、Hook 重投递、Stop 一次续跑和 turn alias、任务期限、取消与迟到结果、两种中转 JSON 格式、错误脱敏、MCP 解析/初始化/参数/取消、单次编辑授权、基线校验，以及空格/中文路径和缺失末尾换行的补丁 `git apply --check`。

安装器测试验证绝对解释器路径、`-I` 隔离选项、marketplace 保留与备份、同名冲突拒绝、清单 JSON 解析。

## 明确未验证

- 用户实际 Codex Desktop 的插件加载、Hook 触发、原文渲染与原生工具审批。
- 用户本地中转、真实 Gemini 模型、真实供应商费用与远端取消行为。
- GitHub Actions 实际执行结果；以仓库 Actions 页面为准。仓库由用户创建，代码提交通过 GitHub 连接完成。
- macOS / Python 3.11 的实机运行；CI 矩阵已配置，但本次只运行上述 Linux / Python 3.13 环境。
- 官方 MCP SDK 独立客户端互操作与第三方安全审计。

## 发布状态

仓库 `atlasfish/codex-dual-agent` 已由用户创建。本报告随 V1 源码通过 GitHub 连接提交；完整文件树在发布时另行核对。未改动用户的其他仓库，未上传真实配置、API key、SQLite 数据库或会话资料。

本机 `scripts/publish.py` 保留为向其他新私有仓库发布副本的辅助脚本，不适用于更新这个已经存在的仓库。远端 CI 结果不包含在上述本地 93 项测试结论中。
