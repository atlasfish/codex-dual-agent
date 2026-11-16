# 架构与状态机

## 数据通路

```text
用户字面前缀
  -> UserPromptSubmit Hook
     -> 确定性解析 + 工作区允许列表 + 受限文件快照
     -> SQLite 事务：用户消息 / turn / frozen payload / job / 单次 grant
     -> 启动独立 Worker（不等待模型响应）
     -> 向 Codex 注入 mode / turn_token / job_id / snapshot_id

GPT 原生工具与推理               Gemini Worker
  -> get_context                 -> SQLite 原子 claim
  -> 独立形成意见                 -> 本地中转 HTTP 请求（无工具）
  -> record_gpt_opinion           -> 验证响应 / 可选生成隔离提案
  -> await_result <------------- SQLite 持久化结果
  -> 展示两份原文 + 单独综合
  -> Stop 保存 host rendering / GPT 原始意见
```

没有额外常驻 HTTP 控制服务，也没有开放本机未鉴权端口。Hook、MCP 和 Worker 通过私有目录中的 SQLite 通信。每个任务一个短生命周期 Worker；进程数受运行任务数量约束，重复投递通过原子 claim 抑制重复请求。

## 任务状态

`queued -> running -> succeeded | failed | cancelled`。到期的 queued/running 任务在读取时转 failed。不会自动重试、不做自动供应商降级；网络失败可能已经产生费用，不能当成“请求肯定没发送”。任务取消会尽力停止本地 HTTP 等待，终态后拒收迟到结果。

后台请求与主 GPT 调用可以并发，但不承诺 GPU 同时开始生成，也不承诺两边延迟相同。Hook 需要先冻结必要证据，这段小量本地工作仍在主轮开始前发生。`get_job` 只显示状态，不泄露 Gemini 当前回答。`@both` 的 `await_result` 有数据库级先决条件：同轮 GPT 独立意见已经保存。

## 会话身份与共享内容

会话主键基于原生 session_id 与 real workspace path；同仓库多个聊天不会混用历史。每轮保留原生 turn_id，工具使用随机 turn_token。用户问题、GPT opinion、Gemini opinion、host_rendering 分开保存。纯 Gemini 转发不会被伪装为 GPT 原始意见，GPT 最终综合也不会覆盖独立草稿。

历史冻结在任务创建时。Gemini 得到之前已完成的发言与当前用户问题，不会得到正在生成的同轮 GPT 答案。历史按字符预算取最近后缀；原文仍保留在库中供精确检索。这不是两模型内部推理状态同步。

`Stop` 一次兜底可能让 Codex 生成新的 continuation turn_id。因此数据库维护 host-turn alias：带内部续跑标记且符合预期的下一轮仍对应原任务，不新增用户消息、不重复请求、不追加授权。最多阻止一次 Stop；仍未满足协议则发警告，不无限续跑。

## 快照

只取显式文件，除非用户在配置中主动开启 `auto_changed_files`。每个条目含路径、UTF-8 字节的 SHA-256 与冻结内容。读取时逐层拒绝符号链接，并拒绝硬链接与非普通文件；最后再次核对已选文件。快照是有界的最佳努力一致视图，不是整个文件系统的原子事务。

可选 evidence diff 比较一个固定 Git HEAD 与已冻结的文件内容，不读取正在变化的工作树来生成差异。大型或无法读取的基线会被省略并附警告。evidence diff 只供分析，可能被截断；它不是可以直接 apply 的补丁。编辑提案的 `proposal.patch` 则单独从完整冻结内容生成。

## 一次性编辑提案

授权只来自实际 UserPromptSubmit 的 `@gemini:edit`，不能由 MCP 参数或 Gemini 回复自授。授权绑定 session/turn/job/snapshot、精确路径、到期时间，并事务性消费一次。生成模型只返回严格 JSON replacement；路径、基线哈希、大小和 UTF-8 检查通过后才在私有 staging tree 内落盘。

主工作区完全不由这个编辑通道改动。提案应用留给后续明确的 GPT 原生任务：检查 manifest、复算当前文件哈希、审阅 diff、正常审批、测试。该 V1 没有“永久 Gemini 可写模式”、任意命令执行器或自动 cherry-pick。

## 重要设计选择

- Python + SQLite，不引入通用多 Agent 平台；唯一运行时第三方依赖为 httpx。
- Provider 适配独立于 Codex 主 Provider；当前支持中转的 Chat Completions / Responses 文本协议。
- 自实现最小 MCP stdio 工具服务，严格验证参数；不伪称完整 SDK 等价实现。
- 安装后解释器使用绝对路径与 Python `-I`，防止目标项目的同名模块/PYTHONPATH 污染运行时。
- 日志不记录原始 HTTP 错误体或鉴权头；会话库本身仍包含敏感项目数据，必须按私密资料管理。
