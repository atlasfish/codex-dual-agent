# Codex Dual Agent

保留 Codex Desktop 的 GPT 原生工作流，让 Gemini 通过本地中转成为**有 Shell、Skill、REST API 和联网搜索能力的独立协作 Agent**。

> **v0.2.0：权限模型发生变化。** Gemini 默认是“建议角色”，不是只读沙盒。它没有直接 `write_file` / `apply_patch` 工具，但可以运行 Shell 和 Skill 脚本，因此具有当前用户可用的文件写入、SSH 和网络能力。安装更新代表启用这个受信任的本地执行器；需关闭全部工具时设置 `tools_enabled = false`。
>
> 真实 Desktop、你的中转、私人服务器和公网搜索可用性仍需实机验收。不要把模拟协议测试当成生产兼容性证明。

## 交互

| 前缀 | 行为 |
| --- | --- |
| `@gpt` | GPT 原生执行、编辑，按需引用 Gemini 历史。 |
| `@gemini` | Gemini 独立使用工具并回答，GPT 原文转发。 |
| `@both` | 双路并发；GPT 保存独立意见后读取 Gemini 答案，再单独比较。 |
| `@gemini:edit --files src/a.ts -- ...` | 保留原有单次隔离补丁提案流程，不自动应用主工作区。 |
| `@compare` | 比较双方历史回答，不新增 Gemini 请求。 |

前缀是本项目的文字协议，不是 Desktop 原生模型切换 UI。没有两个独立原生聊天气泡。

```text
@gemini 读取我的 server-status Skill，执行其中的健康检查脚本，分析结果，不修改服务。
@gemini 检查当前模块的实现，需要哪些文件你自己查。
@both 调研这个依赖的最新文档，结合项目现状给出独立建议，暂不改代码。
@gpt 读取 Gemini 上一条意见，核实后按第二个方案实施。
```

默认无需手工列出 `--files`：Gemini 可以自主读文件或用 Shell 搜索代码。显式 `--files` 仍用于固定证据或补丁基线。动态工具读取的是当前文件，不保证与 GPT 看到的版本完全相同。

## Gemini 工具

- `shell`：运行本地命令、SSH、curl、项目检查和 Skill 脚本；继承运行进程的环境与本机认证能力。
- `read_file`、`list_skills`、`read_skill`：按需读文件，发现并加载本地 Skill。
- `http_request`、`web_search`：访问 HTTP(S)、本机/内网 REST API 和实时搜索。

默认建议角色允许临时文件、缓存与脚本正常产物；不主动编辑项目源码、部署、提交、删除或修改远程服务。任务确实授权的操作可以执行。这是**提示词约定**，没有命令黑名单或“绝对只读”保证。直接编辑入口被移除并不阻止 Shell 写文件。

`allowed_roots` 仅控制哪些项目可启动路由，不约束 Shell 的全部文件访问。`network_enabled=false` 只移除内建联网工具，不会阻止仍启用的 Shell 联网。

## 安装与升级

Python 3.11+；依赖 `httpx` 和 `PyYAML`。从仓库获取本版本后执行：

```bash
python scripts/install.py --dry-run
python scripts/install.py
```

安装器保留用户配置与其他 marketplace 条目，更新私有运行时和插件副本。重新启动 Desktop 并重新检查/信任 Hooks。不会替换你的 GPT Provider。

macOS/Linux/WSL 默认 `/bin/sh -c`。原生 Windows 已加入 `Scripts/python.exe`、PowerShell Hook override 与可移植快照路径；**本次本地验证环境是 Linux，Windows Desktop 的实际兼容性仍待验证**。

配置位置：`~/.config/codex-dual-agent/config.toml`，不是 Codex 自己的配置。最少配置：

```toml
base_url = "http://127.0.0.1:8317/v1"
model = "你的实际Gemini模型ID"
protocol = "chat_completions" # 或 responses
allowed_roots = ["C:/Users/you/Projects/MyApp"] # 替换为你的实际目录

tools_enabled = true
shell_enabled = true
skills_enabled = true
network_enabled = true
timeout_seconds = 300
skill_roots = []
```

两个协议都需要中转正确透传 **function/tool calls**，而不只是纯文本聊天。Chat Completions 保留 tool_call_id 和供应商附加字段；Responses 回传 function_call_output 及 reasoning items。不支持工具的中转会显式报错；不会偷偷降级为“假执行”。

密钥仍从 `DUAL_AGENT_API_KEY` 或 `api_key_file` 读取。GUI 进程不一定继承终端环境；SSH、REST 和脚本所需凭据必须在实际 Worker 进程环境中可用。无需把凭据贴入对话。HTTP headers 支持 `Bearer ${SERVER_TOKEN}` 一类本地环境变量展开。

## Skill 发现与脚本

自动扫描工作区和用户目录中的 `.agents/skills`、`.codex/skills`。通过 `skill_roots` 添加其他安装目录（包括具体插件的 skills 目录）。先给 Gemini 名称/描述索引，需要时读完整 SKILL.md，再按其中说明运行脚本。

```toml
skill_roots = ["C:/Users/you/my-skills", "C:/Users/you/.codex/plugins/my-plugin/skills"]
# 可选覆盖默认 shell：
# shell_command = ["pwsh", "-NoProfile", "-NonInteractive", "-Command"]
```

Skill 是指令和资源，不是自动复制宿主权限。依赖 Codex 专属 MCP、连接器 OAuth、浏览器会话或系统内置工具的 Skill **不能仅凭文件自动继承这些能力**；应改用本机已有 CLI/脚本/REST，或另外接入对应工具。Shell 非交互式，不提供终端登录对话；先在本机配置认证。

## 默认联网与搜索

联网工具默认启用。默认搜索使用无需 API key 的 DuckDuckGo HTML 适配器，**可能被限流、验证码或页面变化阻断**；失败会返回错误，模型不得伪造搜索结果。未在你的网络实测。

可改用自己的 SearXNG：

```toml
search_backend = "searxng"
search_url = "http://127.0.0.1:8080/search"
```

或者复用自己的搜索脚本/Skill：

```toml
search_backend = "command"
search_command = ["python", "C:/Users/you/skills/search/scripts/search.py", "{query}"]
```

脚本输出 JSON：`{"results":[{"title":"标题","url":"https://...","snippet":"摘要"}]}`。参数通过 argv 传递，不把查询拼入 Shell。也可让 Gemini 直接读搜索 Skill 并运行其中脚本。

## 并发、数据与存储

保留按会话/工作区隔离的 SQLite 历史、原文转发、先记录 GPT 独立意见再看 Gemini 的顺序约束。开放 Shell 后，模型理论上能绕过应用接口读取本机状态；独立性也不再是针对恶意同用户进程的硬保证。

工具中间结果仅用于当前 Worker 的模型循环，**不作为完整日志写入 SQLite**。持久化审计只记录工具名、参数哈希、状态、时间和输出字节数，不保存原始命令、鉴权头或网页正文。成功任务清除重复的 `jobs.payload`。`@gpt` / `@compare` 不再自动抓取改动文件。

对话原文、显式快照、补丁产物仍会增长。**本版本没有完成全局容量配额、历史快照去重或自动 GC**；不能声称已经彻底解决数据库增长。工具看到的内容会发送至你的中转/模型供应商；环境变量与密钥不会自动整体打包，但脚本自行输出秘密仍有泄露风险。

取消会尽力结束本地命令/请求；已发生的远程变更不能回滚，供应商计费也不能保证停止。失败不会自动重复执行命令或 API。

## 开发

```bash
python -m pip install -e '.[test]'
python -m pytest -q
python -m compileall -q dual_agent scripts
```

测试使用临时目录、模拟或本机 HTTP，不连接真实模型和私人服务器。说明见 [TEST_REPORT.md](TEST_REPORT.md)、[安全边界](SECURITY.md)、[实机验收](docs/SMOKE_TEST.md)。

仓库：`atlasfish/codex-dual-agent`。`scripts/publish.py` 仅用于创建另一个新私有仓库，不用于更新本仓库。MIT License；独立社区项目。
