# 棋局观察信息 A/B 实验：Flash 与 Pro

## 实验设置

2026-11-17，按自学要求使用 `deepseek-flash` 和 `deepseek-v4-pro`，替代原作业的 DeepSeek 与 GPT-OSS。此报告不代表完成了原版指定的 GPT-OSS 实验。

每个模型分别运行有、无合法走法列表（`legal_moves`）两组。两组均提供棋盘、FEN、状态及近期历史；提示词分别要求从列表选择或自行推断合法走法。服务器始终校验走法。

四组均关闭思考（`thinking.type=disabled`），轨迹中的 reasoning_tokens 合计均为 0；使用同一份 `fix.patch`、200 步上限、1800 秒环境上限，未开启压缩，只注册 `play_move`。每组使用独立环境，从初始棋局开始；四组并行运行，每组仅一个完成样本。

## 结果

非法率 = 被服务器判定非法的 play_move 次数 / 所有 play_move 调用次数。网络错误单独统计。

| 模型 | 合法走法列表 | play_move 次数 | 非法次数 | 非法率 | 网络错误 | game_over | 结果 |
|---|---|---:|---:|---:|---:|---|---|
| Flash | 不提供 | 72 | 14 | 19.44% | 0 | true | 黑棋胜 |
| Flash | 提供 | 17 | 0 | 0.00% | 0 | true | 黑棋胜 |
| Pro | 不提供 | 21 | 3 | 14.29% | 1 | true | 黑棋胜 |
| Pro | 提供 | 58 | 2 | 3.45% | 0 | true | 黑棋胜 |

提供列表后，本次 Flash 非法率下降 19.44 个百分点，Pro 下降 10.84 个百分点。两者均显示列表有帮助，但 Pro 在有列表时仍提交了非法走法，说明提供选项不能保证模型遵守。

## 轨迹证据

以下编号是模型动作请求的步骤编号，不是棋谱回合数。

- Flash 无列表：第 30、31、45、58、60、61、63、64、65、67、68、69、70、71 步非法。例如第 30 步 `c7b8b` 被拒绝，下一步改成 `c7b8q` 仍被拒绝。
- Flash 有列表：没有非法调用。
- Pro 无列表：第 14、17、18 步非法，分别为 `c1b2`、`c1b2`、`c1d2`；第 13 步为连接断开错误，未算入非法次数。
- Pro 有列表：第 18 步 `e2c4`、第 20 步 `f3d3` 非法。

统计逐轮关联 assistant 工具调用与下一次请求中新增的 tool 观察，避免重复统计累计 prompt，也不假定调用 ID 全局唯一。最后一次工具观察不会进入新的模型请求，因此用对应 result 文件的终局状态和 human_move 核对最终调用成功。

## 用量与时间参考

以下总 tokens 包含全部已记录请求的输入和输出。时间仅为首末模型响应 created 时间戳跨度，不是完整运行耗时。

| 模型 | 合法走法列表 | 总 tokens | 首末时间戳跨度 |
|---|---|---:|---:|
| Flash | 不提供 | 884,074 | 119 秒 |
| Flash | 提供 | 74,703 | 22 秒 |
| Pro | 不提供 | 87,208 | 101 秒 |
| Pro | 提供 | 708,458 | 104 秒 |

Flash 有列表组第一次启动在上传补丁时因连接超时失败，未发起模型请求；原配置重试后完成。以上只统计完成轨迹，未将该启动失败混入模型指标。Pro 无列表组的时间包含网络错误及恢复，不能直接当作纯模型速度比较。

## 结论与限制

合法列表把“判断是否合法”和“选择哪步更好”拆开，让模型可以专注选择。本次两个模型的非法率均下降，但四组都输了，不能据此说列表提高了获胜率。非法更少也不必然导致总用量更少：Pro 有列表组棋局更长，累计 tokens 反而更多。

每组仅一次样本，局面和对局长度不同；并行运行、网络波动和随机生成也会影响结果。这些数据支持本次观察，不足以证明模型整体棋力排名或普遍性能差异。

## 产物与复现

| 条件 | 轨迹 | 结果 |
|---|---|---|
| Flash 无列表 | `part3-no-legal-moves-flash.json` | `part3-no-legal-moves-flash-result.json` |
| Flash 有列表 | `part3-legal-moves-flash.json` | `part3-legal-moves-flash-result.json` |
| Pro 无列表 | `part3-no-legal-moves-pro.json` | `part3-no-legal-moves-pro-result.json` |
| Pro 有列表 | `part3-legal-moves-pro.json` | `part3-legal-moves-pro-result.json` |

```bash
make run-obs-experiment-no-legal-moves MODEL=deepseek-flash MODEL_TAG=flash
make run-obs-experiment-legal-moves MODEL=deepseek-flash MODEL_TAG=flash
make run-obs-experiment-no-legal-moves MODEL=deepseek-v4-pro MODEL_TAG=pro
make run-obs-experiment-legal-moves MODEL=deepseek-v4-pro MODEL_TAG=pro
```
