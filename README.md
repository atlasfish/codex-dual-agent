# Codex Dual Agent

**保留 Codex Desktop 的 GPT 原生工作流，通过本地中转连接一个独立的 Gemini 第二意见通道。**

`@both` 并行讨论 · `@gemini` 连续追问 · `@gpt` 执行与编辑 · 持久化共享历史 · 单次隔离编辑提案。

> **v0.1.0 / 本地可测试实现，不是已经在你的 Desktop 中验证过的正式发布。**
> 核心、HTTP 协议、真实 Hook/Worker 子进程和 MCP stdio 已有自动化测试；当前没有你的 Codex Desktop 实例或本地中转凭据，尚未完成这两部分的实机验收。具体结果见 [TEST_REPORT.md](TEST_REPORT.md)。
>
> Gemini 发言仍由 Codex 主 Agent 转发，并非两个原生模型气泡。`@gemini:edit` 目前生成隔离目录中的补丁，**不会直接改主工作区**。这两个边界是有意设计，不应宣传为完整 Desktop UI 扩展或 Gemini 原生编辑 Harness。

## 已实现的工作流

| 输入 | 行为 |
| --- | --- |
| `@gpt <问题>` | GPT 使用 Codex 原生工具，必要时读取 Gemini 历史。 |
| `@gemini <问题>` | Hook 启动 Gemini 请求；GPT 通过 MCP 获取并原文转发。 |
| `@both <问题>` | Hook 在返回之前提交独立 Worker；GPT 不等待 Gemini，可并行工作。读取 Gemini 当前答案前必须先保存 GPT 独立意见。 |
| `@gemini:edit --files src/a.ts -- <修改要求>` | 一次性授权精确文件集合；Gemini 生成替换内容，校验后输出隔离补丁与基线哈希。 |
| `@compare` | 读取双方历史原文，交给 GPT 对比；不会额外请求 Gemini。 |
| 无前缀 | 默认 GPT；首次显式使用上述前缀前，不记录普通会话。 |

前缀是本项目解析的**文字语法**，不是 Codex 内置命令。`--files ... --` 支持带空格路径的引号；路径相对当前工作区。默认没有 `--files` 就不会发送项目文件，也不会悄悄上传整个仓库。

```text
@both --files app/src/main/java/demo/MainViewModel.kt -- 独立评估状态管理方案，先不要修改代码。
@gemini 继续解释你的第二个建议，给一个更容易理解的例子。
@gpt 读取 Gemini 上一条完整意见，核对其假设后再决定是否采纳。
@gemini:edit --files src/router.ts -- 在不改变外部接口的前提下拆分状态机。
@gpt 审查刚才的隔离补丁；确认基线没有变化后，再按原生审批流程应用并测试。
@compare
```

## 环境要求

Python **3.11+**；macOS、Linux 或 WSL；安装依赖时可访问 Python 包源。运行时仅依赖 `httpx==0.28.1`，SQLite 使用标准库。Git 用于可选的版本上下文和补丁测试。

**原生 Windows 不在 V1 支持范围内**：安全快照依赖 POSIX `openat`/`O_NOFOLLOW`。在 WSL 内运行的程序无法直接把 Windows 原生 Desktop 的进程环境变成 WSL；这类组合需要额外配置并单独验证。

Codex 需要支持本地插件、MCP 和本文使用的 Hooks 事件。不要为了这个插件替换你现有的 GPT Provider 设置。

## 安装到本机

解压源码并进入目录，先看将修改哪些路径：

```bash
python3 scripts/install.py --dry-run
python3 scripts/install.py
```

安装脚本会创建私有 Python 虚拟环境、安装运行时，生成带**绝对解释器路径与 `-I` 隔离选项**的插件副本，并向本地个人 marketplace 添加一条记录；已有其他条目与用户配置保留。修改 marketplace 前会保存备份。不会自动替你信任 Hooks，也不会修改 `~/.codex/config.toml` 的主模型。

| 本地路径 | 内容 |
| --- | --- |
| `~/.local/share/codex-dual-agent/runtime` | Python 运行时及依赖 |
| `~/.codex/plugins/codex-dual-agent` | 安装后的插件副本 |
| `~/.config/codex-dual-agent/config.toml` | 你的插件配置，不是 Codex 主配置 |
| `~/.agents/plugins/marketplace.json` | 个人本地插件目录 |
| `~/.local/share/codex-dual-agent/state` | 会话库与隔离提案，含项目资料，不应上传 |

修改插件自己的 `config.toml`，最少填写：

```toml
base_url = "http://127.0.0.1:8045/v1"
model = "你的中转实际提供的Gemini模型ID"
protocol = "chat_completions" # 或 responses，按中转实际协议选择
allowed_roots = ["/Users/you/Projects/MyAndroidApp"]
```

`allowed_roots=[]` 会拒绝路由请求；不要图方便把整个用户目录加入列表。`base_url` 应为 API 根路径，程序自行追加 `chat/completions` 或 `responses`。

需要密钥时，可使用继承的 `DUAL_AGENT_API_KEY` 环境变量。GUI 启动的 Desktop 不一定继承终端环境，更推荐把密钥存入项目目录之外、属于自己的 `0600` 文件，并在配置中设置 `api_key_file` 的绝对路径。**不要把密钥放进聊天、仓库、插件清单或本文件。** 无鉴权的本地中转可留空。

```bash
~/.local/share/codex-dual-agent/runtime/bin/python -I -m dual_agent doctor
```

`doctor` 只验证配置和本地依赖，不发模型请求；`ready=true` 不代表中转或 Desktop 已实测成功。

随后在 Codex Desktop 的本地插件来源中安装启用该插件，或使用安装脚本输出的命令（marketplace 名称以脚本输出为准）：

```bash
codex plugin add codex-dual-agent@dual-agent-personal
```

重新启动 Desktop，在 Hooks 设置中检查并信任这些脚本。以 `@gemini 请用一句话介绍自己的任务` 开始，再按照 [实机验收清单](docs/SMOKE_TEST.md) 测试。若 `@` 被界面提及菜单消费，先确认最终提交文本确实包含字面前缀；本插件不能修改 Desktop 输入框。

## 上下文、并发与权限

每轮以 **Codex session_id + 规范化工作区路径** 隔离，另有不可预测的 `turn_token`。SQLite 保存原始发言、任务状态、上下文快照和补丁授权。后续 `@gpt` 可用 `fetch_peer_opinion` 找到 Gemini 指定轮次的原文；重启后仍可恢复已经保存的内容。

这并非共享两个模型的全部隐藏状态。Gemini 收到的是本插件从激活之后保存的可观察发言，以及显式选择的文件。不会解析 Codex 内部 transcript，不会导出隐藏推理，也没有自动导入插件启用前的会话。超出上下文预算时发送最近的消息后缀；数据库保留原文，V1 **没有自动长期摘要**。

`@both` 的 Gemini 输入在提交时冻结，不包含 GPT 同轮新产生的结论。GPT 在读取 Gemini 结果前先提交不可改写的独立意见。原生 GPT 可能仍看到自己的既有上下文与额外工具证据，因此这是**共享证据快照上的独立协作**，不是两边完整上下文完全一致。

Gemini HTTP 请求不包含工具声明；Gemini 不能直接访问文件、终端或网络搜索。主进程只发送受限快照。敏感路径、越界路径、符号链接、硬链接、非文本和超大文件会被拒绝。可在工作区添加 `.dual-agentignore` 排除模式；它是简单 `fnmatch` 排除规则，不是完整 Git ignore 语法。

编辑权限仅由显式 `@gemini:edit --files ... --` 创建，绑定任务、快照、目标路径、期限和单次使用。生成物保存在 `state/artifacts/`，包括 `tree/`、`proposal.patch`、`manifest.json` 和原始 Gemini 输出。只支持已选中文本文件的替换，不支持新增、删除或重命名。**隔离目录不是 Git worktree**。应用到主工作区必须是后续明确的 GPT 任务，先核对基线哈希，再执行正常审查、审批和测试。

取消会尽力关闭 HTTP 请求并拒收迟到结果，但无法保证远端供应商取消计费。超时或进程崩溃后不会自动重试模型请求，以免重复扣费；需要用户重新发起一轮。

## MCP 工具

`get_context`、`get_job`、`await_result`、`record_gpt_opinion`、`fetch_peer_opinion`、`compare_opinions`、`cancel_job`。

MCP 不能创建额外 Gemini 请求、改变中转地址、授予写权限或写主工作区。`await_result` 每次等待不超过 25 秒；任务可在其总期限内继续等待。MCP 消费端取消一次等待，不等于取消持久化任务。

当前为一个小型、经过协议测试的 **tools-only stdio JSON-RPC 实现**，不是 FastMCP，也不是完整 MCP 平台。支持版本协商、初始化、工具调用、ping 和取消通知；不提供 HTTP/SSE、资源、采样或恢复流式输出。

## 开发与测试

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
python -m pytest -q
python -m compileall -q dual_agent scripts
```

测试不需要真实模型密钥；使用 MockTransport 或仅监听本机的测试 HTTP 服务。真实子进程集成覆盖 Hook → detached Worker → SQLite → MCP，以及并发、取消与重试幂等性。GitHub Actions 配置已提供，但“配置存在”不等于已经在你的账号执行通过。

## 获取源码与发布

项目仓库：`atlasfish/codex-dual-agent`。仓库已由作者创建；日常安装请克隆源码，再执行上面的安装步骤：

```bash
git clone https://github.com/atlasfish/codex-dual-agent.git
cd codex-dual-agent
python3 scripts/install.py --dry-run
python3 scripts/install.py
```

`scripts/publish.py` 保留用于向**另一个尚不存在的新私有仓库**发布副本，不用于更新当前已有仓库。它使用本机 GitHub CLI 登录状态，核对账号、运行测试后再建库和 push；遇到同名远程仓库、已有本地 remote 或非预期目录会停止，不覆盖、不强推。源码不包含 GitHub 凭据。

更新当前仓库请使用常规 Git 分支、提交和推送流程。GitHub Actions 的实际结果以仓库 Actions 页面为准，不以本地测试报告代替。

## 文档

[架构与状态机](docs/ARCHITECTURE.md) · [官方接口与兼容边界](docs/COMPATIBILITY.md) · [Desktop 实机验收](docs/SMOKE_TEST.md) · [安全边界](SECURITY.md) · [测试报告](TEST_REPORT.md)

本项目是独立社区代码，不代表 OpenAI 或 Google 官方支持。MIT License。
