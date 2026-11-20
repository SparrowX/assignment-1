# AI 使用说明

我全程监督 Codex 完成本作业的方案设计和编码。我与 Codex 逐项讨论作业要求和实现方案，决定采用哪些修改，并逐行审阅了 Codex 编写的代码。

- **OpenAI Codex**：根据我们的讨论设计并实现 Agent 的提示词构造、工具调用、上下文压缩和棋局搜索工具。我也使用 Codex 运行测试、排查失败原因、核对执行轨迹并整理实验结果。
- **DeepSeek**：作业中的 Agent 在运行时调用了 DeepSeek 模型。CodeAgent 使用 `deepseek-flash` 生成修复任务中的工具调用和上下文摘要；ChessAgent 使用 DeepSeek 选择走法并生成在 Modal 中执行的 Python 搜索代码。在合法走法列表的对照实验中，我使用了 `deepseek-flash` 和 `deepseek-v4-pro`。
