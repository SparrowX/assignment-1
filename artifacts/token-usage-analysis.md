# Django 上下文压缩对照实验

## 配置与结论

2026-11-17，以 `deepseek-flash` 运行 vendored `django__django-15368`，基础提交 `e972620ada4f`，相同 Agent 代码、任务、技能和 200 步上限。压缩组阈值为 6000，保留最近 1 步，摘要输出预算为 1200；基线不启用压缩。未修改 Agent 实现或本地任务测试。

两组生成的补丁均在新 Modal 环境中通过 `check-swebench`：FAIL_TO_PASS **1/1**，PASS_TO_PASS **29/29**，RESOLVED **yes**。压缩组发生 7 次压缩，显著减少活动上下文；包含摘要开销的总 tokens 减少 **38.69%**，但本次模型估算费用和时间跨度增加。

## 产物与复现

- 压缩补丁：`django__django-15368.patch`
- 压缩轨迹：`django__django-15368-trajectory.json`
- 基线补丁：`django__django-15368-baseline.patch`
- 基线轨迹：`django__django-15368-baseline-trajectory.json`
- 验收日志：`django__django-15368-check.log`、`django__django-15368-baseline-check.log`

```bash
COMPACT_THRESHOLD=6000 \
SWEBENCH_PATCH=artifacts/django__django-15368.patch \
SWEBENCH_TRAJECTORY=artifacts/django__django-15368-trajectory.json \
make run-swebench-agent INSTANCE=django__django-15368
make check-swebench INSTANCE=django__django-15368

COMPACT_THRESHOLD=0 \
SWEBENCH_PATCH=artifacts/django__django-15368-baseline.patch \
SWEBENCH_TRAJECTORY=artifacts/django__django-15368-baseline-trajectory.json \
make run-swebench-agent INSTANCE=django__django-15368
make check-swebench INSTANCE=django__django-15368 \
  SWEBENCH_PATCH=artifacts/django__django-15368-baseline.patch
```

命令前的赋值是传给 make 的环境变量。Makefile 将非零阈值转换为 Python 命令行参数 `--compact-threshold-tokens`；0 则省略参数，Python 默认 None，关闭压缩。

make 启动本地 Python；启动程序创建含指定 Django 版本的 Modal 环境并激活 testbed Python；本地 Agent 请求模型，将 execute 命令交给 Modal，追加工具结果并继续。invoke_skill 读取提交说明，send_message 标记完成；启动程序从远端读取 patch.txt，写入指定补丁文件。轨迹保存每次动作请求、响应及压缩事件。check-swebench 另建环境，应用补丁和评测测试，独立判定正确性。

## 用量统计

动作请求统计来自轨迹 `responses[*].usage`；摘要请求来自 `compactions[*].compaction_response.usage`。不按重复的 prompts 快照累加字符数，不将推理明细再次加到 completion_tokens 中。

| 指标 | 压缩组 | 无压缩基线 |
|---|---:|---:|
| 动作请求 / ReAct 步数 | 20 | 18 |
| 摘要请求 | 7 | 0 |
| 模型请求总数 | 27 | 18 |
| execute 工具调用 | 18 | 19 |
| invoke_skill 工具调用 | 1 | 1 |
| send_message 工具调用 | 1 | 1 |
| 工具调用总数 | 20 | 21 |
| 动作请求输入 tokens | 77,916 | 193,235 |
| 动作请求输出 tokens | 3,556 | 3,653 |
| 摘要请求输入 tokens | 32,130 | 0 |
| 摘要请求输出 tokens | 7,115 | 0 |
| 全部输入 tokens | 110,046 | 193,235 |
| 全部输出 tokens | 10,671 | 3,653 |
| 总 tokens | 120,717 | 196,888 |
| 输入缓存命中 tokens | 52,992 | 177,792 |
| 输入缓存未命中 tokens | 57,054 | 15,443 |
| 输入缓存命中比例 | 48.15% | 92.01% |
| 单次动作请求最大实际输入 | 6,053 | 18,215 |
| 最后一次动作请求实际输入 | 4,534 | 18,215 |

动作请求输入减少 59.68%；计入摘要后，全部输入减少 43.05%，总 tokens 减少 38.69%。摘要本身消耗 39,245 tokens，不能忽略。

## 活动上下文缩减

以下取自 compaction_events 的 estimated_tokens_before/after，均为同一种字符数粗估口径；step 表示已完成的动作步数，压缩发生在下一动作前。

| 已完成步数 | 压缩前粗估 | 压缩后粗估 | 缩减比例 |
|---|---:|---:|---:|
| 2 | 4,472 | 3,872 | 13.4% |
| 3 | 6,219 | 3,250 | 47.7% |
| 4 | 4,824 | 2,515 | 47.9% |
| 6 | 5,146 | 1,158 | 77.5% |
| 10 | 5,036 | 2,164 | 57.0% |
| 14 | 6,211 | 3,225 | 48.1% |
| 16 | 5,501 | 1,642 | 70.2% |

7 次摘要的 finish_reason 均为 stop。系统提示、原始任务和最近完整动作/工具结果保留，较早历史替换为摘要。

部分“压缩前粗估”低于 6000 并非阈值失效：触发检查使用上次 API 实际 prompt_tokens 加新增消息粗估；事件记录使用纯字符数粗估，两者口径不同。6000 是估算触发阈值，不是模型输入的硬上限。

## 执行时间与调用次数

轨迹没有记录完整命令起止时间或逐请求耗时。以下只计算首末动作响应 created 时间戳跨度，不能当作精确端到端耗时；不包含完整环境启动、首尾请求处理、清理和独立验收时间。

| 指标（UTC） | 压缩组 | 基线 |
|---|---|---|
| 首个动作响应 created | 2026-11-17 07:19:42 | 2026-11-17 07:14:50 |
| 最后动作响应 created | 2026-11-17 07:21:04 | 2026-11-17 07:15:32 |
| 首末跨度 | 82 秒 | 42 秒 |

压缩组模型调用增加 50%（27 对 18），其中 7 次摘要需要串行等待完成，输出总量也增加。动作请求多 2 次，但工具调用少 1 次，因为基线某些响应包含多个工具调用。压缩组存在重新读取代码的行为；基线也有重复查找和一次错误目录调用。不能仅凭一对随机样本将额外动作或全部时间差归因于摘要丢失信息。缓存未命中增加可能影响延迟，但未采集逐请求延迟，不能量化贡献。

首次压缩组尝试在远端控制服务就绪阶段停滞，尚未发起模型动作请求。终止该环境后，以原配置重试成功。该启动失败未计入以上模型轨迹比较，可能产生额外 Modal 费用。两组不是严格隔离、重复多次的性能基准。

## 模型费用估算

依据 2026-11-17 查阅的 [DeepSeek 官方定价](https://api-docs.deepseek.com/quick_start/pricing/)，deepseek-flash 每百万 tokens 高峰价格：缓存命中输入 $0.006、未命中输入 $0.30、输出 $1.20。响应时间戳位于周二 UTC 07:14–07:21，处于官方公布的工作日 06:00–10:00 高峰窗口。

```text
费用 = (缓存命中输入 × 0.006 + 未命中输入 × 0.30 + 输出 × 1.20) / 1,000,000
压缩组 = (52,992 × 0.006 + 57,054 × 0.30 + 10,671 × 1.20) / 1,000,000
       = $0.030239352
基线   = (177,792 × 0.006 + 15,443 × 0.30 + 3,653 × 1.20) / 1,000,000
       = $0.010083252
```

压缩组模型估算费用约为基线的 3.00 倍，而非随总 tokens 下降。原因是基线的长前缀高度命中缓存；替换历史为摘要后，缓存未命中输入从 15,443 增至 57,054，且增加摘要输出。缓存命中机制参见 [官方上下文缓存说明](https://api-docs.deepseek.com/guides/kv_cache/)。

以上是公开价格乘以 usage 的估算，不是账单实扣；未包含账户优惠、赠送余额、Modal 环境运行费、评测环境和失败启动成本，不能据此声称整个实验的实际账单已确定。

## 取舍与观察

- 压缩有效限制了活动上下文，并且这次保留了足够信息，使最终修复通过独立评测。
- 更小的上下文不等于更少步骤、更短延迟或更低费用：摘要调用、生成开销及缓存失效需要一并计算。
- 本任务基线最终输入仅 18,215 tokens，仍可完整保留历史；6000 阈值用于验证压缩机制，不是本模型费用或速度的最优阈值。
- 更长任务接近上下文容量时，压缩有维持任务可运行性的价值；本次短任务中，其费用与时间代价更明显。
- 每组只有一次成功样本，且动作轨迹不同；结果说明本次观察，不能证明所有任务中压缩都更慢或更贵。若以降低费用为目标，应另做多次运行及阈值对照，再比较成功率、真实账单和完整耗时。
