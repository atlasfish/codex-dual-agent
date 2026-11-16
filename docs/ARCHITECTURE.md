# v0.2 架构

```text
UserPromptSubmit -> 路由/会话/可选证据 -> SQLite job -> detached Worker
                                                     |
GPT 原生工具/独立意见                      Relay Agent loop
                                                     |
                                        工具请求 -> 本地 ToolRuntime
                                          shell / skills / HTTP / search
                                                     |
                                        回传 tool results -> 再次模型请求
                                                     |
record_gpt_opinion -> await_result <----------- 最终 Gemini 意见
```

同一 Gemini 任务内的工具按返回顺序执行，不并行执行可能有副作用的命令。
任务可与 GPT 并发。独立意见由应用顺序约束，不是同用户 Shell 无法绕过的隔离。
Chat Completions 保留原始 assistant/tool_calls（含供应商签名字段），回传 role=tool。
Responses 保留全部 output items，包括 reasoning，以 call_id 回传 function_call_output。
工具轮数用尽后给模型一次 tool_choice=none 的最终回答机会；再要求工具则报错。
没有自动重试、供应商切换、命令重放。

## 生命周期

queued -> running -> succeeded / failed / cancelled。SQLite claim 防止重复 Worker
领取同一任务；Hook 重投递不应造成重放。总任务期限、每命令超时、调用次数、
响应字节数与上下文预算各自约束。取消尽力杀本地进程组，不能回滚远程变更。

## 会话与存储

session_id + 工作区隔离历史，turn_token 选择当前会话，不是防同用户攻击的秘密。
GPT/Gemini opinion 与 host rendering 保留归属。Stop 最多请求一次继续执行。
动态工具内容只保存在本任务内存，不写完整审计日志。tool_events 记录名称、参数
哈希、耗时、状态、输出大小。成功后清空 jobs.payload，减少重复快照/历史存储。
原始会话、显式 snapshots 和 artifact 仍保存；全局容量与自动 GC 尚未实现。

## 权限模型

默认 trusted local Agent：Shell/Skill/network 可用，advisor 不主动改代码。
不暴露直接编辑函数；不以命令黑名单假装只读。allowed_roots 只控制路由入口。
现有 @gemini:edit 仍是校验基线的隔离提案通道，不自动应用到主工作区。
Skill 文件仅提供工作流，不自动继承宿主 MCP/OAuth/浏览器能力。

## 可选快照

日常无需列文件；模型自己查。--files 或配置的 changed-files 才预打包材料。
@gpt/@compare 不自动抓改动文件。快照只固定选中内容，不固定实时工具全视图。
POSIX 保留 hardened openat 检查，Windows 使用明确标记的 best-effort 读取检查。
