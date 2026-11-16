# Codex Desktop 实机验收清单

以下是**待用户环境执行**的验收流程，不是已经完成的测试结果。准备一个允许列表中的小型临时 Git 仓库，不用生产密钥文件或业务敏感数据试运行。

## 1. 安装和连接

运行安装器，填真实 Gemini model ID、API 根地址、allowed_roots 和可选 keyfile。运行 doctor，确保 ready=true。安装/启用本地插件，审阅并信任 Hooks，重启 Desktop。确认 MCP 工具列表含双模型工具。

提交 `@gemini 请只回复一句简短的测试问候`。中转应只有一次 Gemini 请求；Desktop 显示 Gemini 标题和回复。检查中转所收 payload 不含鉴权密钥、整个仓库或 Codex 内部推理。

## 2. 双路并发与独立性

建立 `sample.txt`，提交 `@both --files sample.txt -- 独立评价文件内容，不要修改文件`。观察中转请求时间：Gemini 请求应在 GPT 独立分析期间存在，而不是等 GPT 最终答案后才启动。GPT 必须先调用 record_gpt_opinion，再读取 Gemini 结果。输出分为两个原始意见和单独的比较。

让测试中转故意延迟。主 GPT 应仍能工作；await_result 可以重复等待，但不得产生新模型请求。最终记录不能把双模型综合文本冒充成某一方独立原文。

## 3. 连续对话与恢复

连续两次 @gemini 追问，确认第二次请求含之前 Gemini 的原始回答。然后 @gpt 指定引用上一条 Gemini，确认来源消息准确。关闭/重启 Desktop，在同一原生会话中继续验证。另开同工作区新会话，不应自动看到另一会话的历史。

## 4. 只读与编辑提案

在 @gemini 里要求直接执行 shell 或改文件，Gemini 不应获得执行工具，主工作区哈希不变。随后提交 `@gemini:edit --files sample.txt -- 重新组织句子`：应生成 staging 目录、proposal.patch、manifest.json，主文件仍不变。

在生成提案后手动修改 sample.txt，再让 @gpt 审查应用：应发现 base_hashes 不匹配并停止强行套用。重新生成或手工解决；不要以 --force 绕过。

## 5. 中断与故障

在延迟请求时按停止；任务应取消，迟到结果不能替换终态。用错误 model ID、短超时、过期密钥分别验证失败提示。不应悄悄把 GPT 的回答当成 Gemini 回答，也不应重复请求计费。

若 Stop 请求继续执行，确认最多一次兜底，内部续跑不会新增 Gemini 请求；若 Desktop 字段与当前契约不符，保存脱敏 Hook 字段与版本后排查，不能伪装已实现双路。
