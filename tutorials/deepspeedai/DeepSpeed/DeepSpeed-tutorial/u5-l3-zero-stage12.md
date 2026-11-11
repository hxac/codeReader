# ZeRO-1/2：梯度切分与 IPG 流水线

## 1. 本讲目标

本讲精读 ZeRO Stage 1/2 的核心实现 `DeepSpeedZeroOptimizer`（位于 `deepspeed/runtime/zero/stage_1_and_2.py`，约 4200 行）。读完本讲你应该能够：

1. 追踪反向传播中梯度的完整旅程：autograd hook → IPG（Independent Parallel Gradients，独立并行梯度）bucket → reduce-scatter / all-reduce → 本 rank 的梯度分区。
2. 解释 `reduce_bucket_size`、`contiguous_gradients`、`overlap_comm`、`reduce_scatter` 这四个配置如何共同决定通信的流水线深度与吞吐。
3. 按顺序定位 `step()` 中 flatten（扁平化）、reduce_scatter、optimizer step、allgather 的调用顺序。
4. 说明 Adagrad 初始化修复：为什么 `initialize_optimizer_states` 要用 `self.optimizer.param_groups`（而不是 fp32 分区张量）重建优化器。
5. 理解 CPU 卸载下从主机恢复梯度前先 `dest_buffer.zero_()` 的原因：共享 scratch 中残留的上一步 NaN 会被溢出检查读到（本周期该清零已变为无条件，覆盖全部 master 精度组合）。
6. 解读折叠专家（AutoEP+AutoTP folding，PR #8823）的梯度裁剪修复：`get_grad_norm_direct` 新增 `process_group` / `expert_parallel_group` 参数，`_is_folded_expert_group` 判定出的折叠组范数改为在 expert-DP 分区组与 EP owner 组上求和，且 `_average_expert_grad_norms` 不再对折叠组做专家范数平均。
7. 解读本周期 CPU 卸载梯度累积的两连修（#9018、#9044）：主机累计器从按参数惰性 dict 改为**按分区组**的 bit16 pinned 缓冲；紧凑分区（fp32 累积 + 16 位模型）在每个**请求边界**把运行和发布到 host 梯度，仅当 host dtype 能逐位恢复时才释放 GPU 分区（新配置 `retain_gpu_grad_partition_until_step`）；溢出检查改为对 cast 到 host dtype 后的值；`reset_cpu_buffers` 每步后清零 FP32 分区梯度，修复空闲参数复用上一窗口梯度的泄漏（#8991）。

## 2. 前置知识

### 2.1 ZeRO 三阶段的显存账本

回顾 u5-l1 建立的记号：设模型有 \(\Psi\) 个参数，混合精度训练 + Adam 优化器时每参数的显存开销是：

- 半精度（fp16/bf16）参数：\(2\Psi\) 字节
- 半精度梯度：\(2\Psi\) 字节
- fp32 主权重（master weights）：\(4\Psi\) 字节
- Adam 一阶/二阶矩（各 fp32）：\(8\Psi\) 字节

合计 \(16\Psi\)。朴素数据并行下每个 rank 都完整持有这 \(16\Psi\)。ZeRO 的两级切分：

- **Stage 1**（只切优化器状态）：\(2\Psi + 2\Psi + \dfrac{12\Psi}{N_d}\)
- **Stage 2**（再切梯度）：\(2\Psi + \dfrac{14\Psi}{N_d}\)

其中 \(N_d\) 是数据并行宽度。本讲的 `DeepSpeedZeroOptimizer` 同时承担 Stage 1 和 Stage 2：构造参数 `partition_grads=True` 即 Stage 2，`False` 即 Stage 1。参数始终不切（这是 Stage 3 的事，见 u5-l5）。

### 2.2 关键术语

- **IPG（独立并行梯度）**：不需要等所有梯度生成完毕，反向传播中每凑满一个 bucket 就立刻发起通信，让通信与剩余的反向计算重叠。
- **bucket（桶）**：一组梯度拼接成的通信单元。桶太小则通信次数多、延迟占比高；桶太大则等待凑桶的时间长、峰值显存高。
- **reduce-scatter**：一次集体通信同时完成「求和 + 按 rank 切分结果」，比 all-reduce + 本地切片少一次通信量，是 ZeRO-2 的默认路径。
- **expert data parallel（EDP）组 / expert parallel（EP）组**：MoE 场景下，同一个专家的副本 rank 组成 EDP 组，持有不同专家的 rank 组成 EP 组（详见 u3-l3、u6-l5）。
- **折叠（folding）**：AutoEP + AutoTP 联用时，把专家并行维「折叠」进张量并行维——TP 组内的各 rank 各持**不同**的路由专家。这是理解本讲 4.7 节修复的前提。
- **请求边界（request boundary）**：一次被标记为梯度累积边界的 backward（u2-l4 讲过的 `is_gradient_accumulation_boundary()`）。注意「边界 backward」不等于 `step()`：在 RLHF rollout 等场景下，一个优化器窗口内可能发生多次以请求边界结尾的 backward，`step()` 要到窗口末尾才执行——本周期两项修复（4.9 节）正是围绕「边界与 step 之间的显存驻留」展开的。
- **host 梯度分区**：`single_partition_of_fp32_groups[i].grad`——挂在 fp32 主权重分区张量上的 `.grad`。CPU 卸载时它位于主机（可 pinned）内存，是 CPU 端 Adam 实际消费的梯度。
- **紧凑梯度分区（compact gradient partition）**：`GradPartitionBufferGroup` 持有的、恰好 partition_size 大小的 GPU 端 fp32 扁平缓冲（见 4.3 节）。启用条件是「fp32 梯度累积 + 16 位模型」。

### 2.3 与前讲的衔接

u5-l2 讲了 `zero.Init` 如何让参数"从出生就切分"，那是 Stage 3 的入场券。本讲的 Stage 1/2 中参数是完整的，切分只发生在**优化器状态**（Stage 1/2 都有）与**梯度**（仅 Stage 2）上。u2-l4 讲过引擎侧 `engine.backward` / `engine.step()` 的边界判定，本讲深入 ZeRO 优化器在 `step()` 内部做了什么。u5-l1 已从配置面预告了本周期新增的 `retain_gpu_grad_partition_until_step`，本讲给出它的完整实现语义。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [deepspeed/runtime/zero/stage_1_and_2.py](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py) | 本讲主角：`IPGBucket`、`GradPartitionBufferGroup`、`DeepSpeedZeroOptimizer` 的全部逻辑 |
| [deepspeed/runtime/zero/config.py](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/config.py) | `DeepSpeedZeroConfig`：`reduce_bucket_size`、`contiguous_gradients`、`overlap_comm`、本周期新增 `retain_gpu_grad_partition_until_step` 等字段的定义与默认值 |
| [deepspeed/runtime/engine.py](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py) | 引擎侧把 `retain_gpu_grad_partition_until_step` 透传给 ZeRO-1/2 优化器的接线点 |
| [tests/unit/v1/zero/test_zero_offload_accum_precision.py](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/tests/unit/v1/zero/test_zero_offload_accum_precision.py) | 本周期新增：窗口求和精度矩阵、`safe_set_full_grad` 语义、`retain_gpu_grad_partition_until_step` 的驻留与结果一致性回归 |
| [tests/unit/v1/zero/test_zero2_gradient_safety.py](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/tests/unit/v1/zero/test_zero2_gradient_safety.py) | CPU 卸载梯度安全性：GAS=1 不分配主机累计缓冲、空闲参数不复用旧梯度（#8991 回归） |
| [tests/unit/v1/zero/test_zero2_offload_multi_backward.py](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/tests/unit/v1/zero/test_zero2_offload_multi_backward.py) | 多次 backward / 多请求边界：host 累计器请求边界测试、fp16 host 梯度溢出跳步测试、溢出后恢复 |
| [tests/unit/v1/moe/test_autoep_autotp_sharded_attention.py](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/tests/unit/v1/moe/test_autoep_autotp_sharded_attention.py) | 折叠专家梯度裁剪的回归测试，含 `TestShardedAttentionClippedUpdate` 对照断言 |
| [docs/code-docs/source/autoep.rst](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/docs/code-docs/source/autoep.rst) | 折叠下梯度裁剪契约的官方文档 |

## 4. 核心概念与源码讲解

### 4.1 IPGBucket：独立并行梯度流水线

#### 4.1.1 概念说明

朴素数据并行的做法是：反向传播全部结束后，把所有梯度 all-reduce。这在显存和时间上都很浪费——其实反向是逐层生成梯度的，先生成的梯度完全可以先通信。

ZeRO-1/2 把这件事做成了一条流水线：反向传播每产生一个参数的梯度，就尝试装进一个**桶**；桶满（累计元素数超过 `reduce_bucket_size`）就立刻发起一次通信。这就是 IPG——每个桶的通信与后续层的反向计算并行执行。

`IPGBucket` 是一个 dataclass，记录一个桶的运行时状态。注意构造函数里为**每种通信 dtype 各建一个桶**——因为同一个模型里可能混有 fp16 与 fp32 梯度（例如 autocast 场景），它们不能拼在同一个桶里。

#### 4.1.2 核心流程

```
反向传播（每参数梯度就绪时触发 grad_handling_hook）
  └─> process_gradients(param)
        └─> reduce_independent_p_g_buckets_and_remove_grads(param)
              ├─ 若 bucket.elements + param.numel() > reduce_bucket_size:
              │     reduce_ipg_grads()          ← 桶满，立刻发起通信
              │     overlap_comm 时切换双缓冲 index（0/1 互换）
              ├─ contiguous_gradients:
              │     把梯度 copy 进桶缓冲的窄视图，grad 指向桶内切片
              ├─ MoE 参数：bucket.has_moe_params = True
              │     （决定 average_tensor 用哪个进程组）
              └─ bucket.elements += param.numel
```

桶的通信由 `reduce_ipg_grads` 分派：

- `contiguous_gradients=True`（默认）：直接对桶缓冲的前 `bucket.elements` 个元素做 `average_tensor`；
- `contiguous_gradients=False`：退化为 `buffered_reduce_fallback`，逐个张量处理非连续梯度。

#### 4.1.3 源码精读

IPGBucket 的字段定义（含多流拷贝的同步注释）：

[deepspeed/runtime/zero/stage_1_and_2.py:L117-L138](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L117-L138)

上面这段定义了桶的缓冲 `buffer`、参数/梯度清单、累计元素数 `elements`、双缓冲下标 `index`，以及两组 CUDA event：`ready_events` / `reuse_events` 用于桶复用时的流同步；`copy_streams` 记录所有向该桶写过数据的流——在 torch.compile 下梯度 hook 可能跑在不同的 autograd 流上，reduce 前必须等所有生产者流完成（#8061 修复）。

每种通信 dtype 建一个桶：

[deepspeed/runtime/zero/stage_1_and_2.py:L610-L618](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L610-L618)

autocast 初始化时会收集所有参数可能出现的梯度 dtype 集合 `comm_dtypes`；否则就是单一的 `communication_data_type`。随后第 618 行为每个 dtype 建一个空 `IPGBucket`。

梯度 hook 的注册入口：

[deepspeed/runtime/zero/stage_1_and_2.py:L1469-L1490](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L1469-L1490)

每个 `requires_grad` 的参数注册一个 `grad_handling_hook`，hook 内调用 `process_gradients(param, i)`，并统计已触发的 hook 数，全部就绪时自动运行 epilogue（收尾）。

桶满判定与双缓冲切换：

[deepspeed/runtime/zero/stage_1_and_2.py:L1521-L1535](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L1521-L1535)

当装入本参数会超出 `reduce_bucket_size` 时，先 `reduce_ipg_grads` 把当前桶发出去；`contiguous_gradients` 且 `overlap_comm` 时把 `bucket.index` 在 0/1 间切换——旧缓冲还在被通信流读，新梯度写另一个缓冲，这正是"通信与反向重叠"的物理基础。

连续拷贝进桶（防碎片的关键）：

[deepspeed/runtime/zero/stage_1_and_2.py:L1548-L1575](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L1548-L1575)

梯度被 `copy_` 进桶缓冲的 `narrow` 视图，然后 `grad_reduc.data` 改指向这个视图——后续清理梯度时只需把指针置空，桶缓冲统一管理，避免反复分配导致显存碎片。超过桶大小的"巨型参数"单独走 `extra_large_param_to_reduce` 旁路。

桶的两种归宿（通信分派）：

[deepspeed/runtime/zero/stage_1_and_2.py:L2282-L2314](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L2282-L2314)

`contiguous_gradients=True` 时直接 `average_tensor(bucket.buffer[bucket.index].narrow(0, 0, bucket.elements), ...)`；否则走 `buffered_reduce_fallback` 逐张量兜底（若桶内含紧凑分区组则用保留 fp32 精度的变体 `_buffered_reduce_fallback_preserving_fp32`）。

通信完成后的分区记账与桶清空：

[deepspeed/runtime/zero/stage_1_and_2.py:L2326-L2358](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L2326-L2358)

对桶内每个参数：不属于本分区的梯度立刻清除（ZeRO-2 不留全量梯度）；属于本分区且 `contiguous_gradients` 时调用 `copy_grads_in_partition`。最后记录桶复用 event 并 `bucket.clear()`。

配置默认值（桶大小与开关）：

[deepspeed/runtime/zero/config.py:L102-L138](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/config.py#L102-L138)

`contiguous_gradients` 默认 True、`reduce_scatter` 默认 True、`reduce_bucket_size` / `allgather_bucket_size` 默认 5e8（元素数）、`overlap_comm` 默认 None（动态默认：见 [deepspeed/runtime/zero/config.py:L394-L396](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/config.py#L394-L396)——仅 Stage 3 默认开启，Stage 1/2 默认关闭）。

#### 4.1.4 代码实践

1. **实践目标**：直观感受 `reduce_bucket_size` 与 `contiguous_gradients`/`overlap_comm` 组合对每步耗时的影响。
2. **操作步骤**：
   - 准备一个两卡 ZeRO-2 训练脚本（可仿照 `tests/unit/simple_model.py` 的 `SimpleModel`，用 `deepspeed --num_gpus 2` 启动，配置 `zero_optimization.stage=2`、`gradient_clipping=1.0`）。
   - 分别设置 `reduce_bucket_size` 为 \(5\times10^5\)、\(5\times10^6\)、\(5\times10^8\) 各跑 20 步，记录每步平均耗时（`torch.cuda.synchronize()` + 计时）。
   - 再固定一个桶大小，对比 `contiguous_gradients: true/false` × `overlap_comm: true/false` 的四种组合。
3. **需要观察的现象**：桶很小时每步总耗时上升（通信次数多、launch 开销占比大）；桶极大时反向后期的梯度被迫攒着等桶满，重叠机会减少；`contiguous_gradients=false` 时峰值显存通常更高（碎片）。
4. **预期结果**：存在一个中等的桶大小使每步耗时最低；`overlap_comm=true` 时桶的切换使总耗时对桶大小更不敏感。（具体数值依赖硬件，**待本地验证**。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ipg_buckets` 是一个以 dtype 为 key 的字典，而不是单个桶？
**答案**：同一模型可能同时存在 fp16 与 fp32 梯度（如 autocast、部分参数强制 fp32），不同 dtype 的张量无法拼进同一块连续缓冲，也无法在一次通信中混用，所以每种通信 dtype 独立成桶（[stage_1_and_2.py:L610-L618](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L610-L618)）。

**练习 2**：`bucket.index = 1 - bucket.index` 这行在什么条件下执行、解决什么问题？
**答案**：仅在 `contiguous_gradients and overlap_comm` 时执行（[stage_1_and_2.py:L1532-L1534](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L1532-L1534)）。旧桶缓冲正被通信流读取，如果立刻往里写新梯度会数据竞争；双缓冲让下一批梯度写另一块缓冲，读写并行。

**练习 3**：`copy_streams` 字段解决的是什么场景的 bug？
**答案**：torch.compile 下梯度 hook 可能运行在多条 autograd 流上，多条流都向同一个桶拷贝过数据；reduce 流如果只等待当前流，可能读到其他流尚未写完的桶内容（#8061），所以桶记录所有生产者流（[stage_1_and_2.py:L1569-L1572](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L1569-L1572)），`average_tensor` 逐一等待（[stage_1_and_2.py:L1707-L1719](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L1707-L1719)）。

### 4.2 average_tensor：reduce-scatter 与两种梯度平均模式

#### 4.2.1 概念说明

桶满后真正执行通信的是 `average_tensor`。它有两个职责：选流与同步（overlap 时在 `reduction_stream` 上执行）、以及把桶内容平均并分发。分发有两种模式，对应配置 `reduce_scatter`：

- **all-reduce 模式**（`reduce_scatter=false`）：整个桶做一次带预除的 all-reduce，每个 rank 拿到全量平均梯度，然后各自抽取属于自己的分区。
- **reduce-scatter 模式**（`reduce_scatter=true`，默认）：每个 rank 只收到属于自己的那一片平均梯度，通信量是 all-reduce 的 \((N_d-1)/N_d\)，且天然实现 ZeRO-2 的"梯度分区"。

MoE 参数有特殊处理：桶内若含 MoE 参数（`bucket.has_moe_params`），该参数的进程组从 `dp_process_group` 换成它所属专家组的 expert data parallel 组。

#### 4.2.2 核心流程

```
average_tensor(tensor, comm_dtype)
  ├─ 选流：overlap → reduction_stream（并等待所有生产者流/事件）
  ├─ reduce_scatter=false:
  │     gradient_reduction_w_predivide(tensor)   ← all-reduce + 预除
  │     _add_full_reduction_to_compact_grad_partition(...)
  │     return
  └─ reduce_scatter=true:
        遍历桶内参数，按 partition_ids 计算每个梯度切片的
        (目标 rank, 源偏移, 目标偏移, 长度) → rank_and_offsets
        按 rank 分组拼接 → 一次 reduce_scatter 完成平均+分区
```

范数相关的数学：全局 L2 范数按"先局部平方和、再跨组求和、最后开方"计算：

\[
\|g\|_{\text{global}} = \Big(\sum_{r \in G_{\text{dp}}} \sum_{i \in \text{rank } r} \|g_i\|_2^2\Big)^{1/2}
\]

这依赖每个元素恰好被一个 rank 持有一份分区——这正是 4.7 节折叠修复要维护的不变量。

#### 4.2.3 源码精读

流选择与多生产者等待：

[deepspeed/runtime/zero/stage_1_and_2.py:L1697-L1728](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L1697-L1728)

非 reduce-scatter 路径在这里提前返回（第 1725-1728 行），走 `gradient_reduction_w_predivide` 的 all-reduce。

reduce-scatter 的切片规划：

[deepspeed/runtime/zero/stage_1_and_2.py:L1730-L1787](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L1730-L1787)

注意第 1746-1748 行：桶里有 MoE 参数时，MoE 参数用其专家组的 expert-DP 组、稠密参数仍用 DP 组——一次 reduce_scatter 只能在一个组上做，所以这里按参数逐个确定组并规划切片。

#### 4.2.4 代码实践

1. **实践目标**：验证 reduce_scatter 与 all-reduce 两条路径的数值等价性。
2. **操作步骤**：写一个 2 进程的最小脚本（可用 torchrun 启动），分别构造 `DeepSpeedZeroOptimizer` 或直接用引擎配置 `reduce_scatter: true/false` 跑几步，打印每步 loss。
3. **需要观察的现象**：两条路径 loss 序列应逐位一致（同一随机种子下）；reduce_scatter 路径在 rank 上的梯度分区张量形状约为全量的一半。
4. **预期结果**：数值一致；reduce_scatter 模式峰值显存略低。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 MoE 参数不能与稠密参数共用一次 reduce_scatter？
**答案**：稠密参数的分区组是 `dp_process_group`，MoE 参数的分区组是其 `group_name` 对应的 expert data parallel 组（[stage_1_and_2.py:L1744-L1748](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L1744-L1748)）。两者组成员不同，一次集体通信只能在同一个组上执行，所以必须分别规划。

**练习 2**：`_configure_moe_settings` 对 Stage 2 有什么硬性要求？
**答案**：要求 `contiguous_gradients=True` 且 `reduce_scatter=True`，未标记 `'moe': True` 的参数组直接断言失败；同时把 MoE 参数组的 `real_dp_process_group[i]` 替换为 expert-DP 组（[stage_1_and_2.py:L876-L901](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L876-L901)）。

### 4.3 GradPartitionBufferGroup：紧凑梯度分区缓冲

#### 4.3.1 概念说明

ZeRO-2 中每个 rank 最终只需要持有自己那份梯度分区。如果 `gradient_accumulation_dtype` 是 fp32 且与参数 dtype 不同（最典型的：fp16/bf16 训练 + fp32 梯度累积），梯度分区可以做成一个**恰好 partition_size 大小的紧凑 fp32 缓冲**，reduce-scatter 的结果直接落进对应切片，不需要任何中间拷贝。`GradPartitionBufferGroup` 就是这个缓冲：它是一个 `list` 子类——列表元素是每个参数对应的缓冲视图（保持与参数一一对应，便于按参数求范数、清零），同时持有 `flat_partition`（底层一维大张量）。

本周期（#9044）给它加了一个新维度：CPU 卸载时，这块 GPU 缓冲在**请求边界**可以被释放、把运行和托管给 host 梯度，下次 backward 再按需重建恢复（详见 4.9 节）。因此 `_get_compact_grad_partition_buffer` 现在带着"若本组曾被释放则先恢复"的钩子。

#### 4.3.2 核心流程

```
_use_compact_grad_partition_buffer(i)?
   条件（按判定顺序）:
     1. 该组不是 Muon 组          ← 本周期（#9044）把此判定提到最前
     2. gradient_accumulation_dtype == fp32 且 != 参数 dtype
   → True

是 → _get_compact_grad_partition_buffer(i):
      已有且 flat_partition 存在 → 直接复用
      否则 _build_compact_grad_partition_buffer(i):
            flat_partition = zeros(partition_size, dtype=fp32)
            按参数在分区中的 (dest_offset, num_elements) 切窄视图，
            视图形状还原为参数形状；空隙标记 _zero_padding=True
      若 CPU 卸载且 i ∈ _host_owned_compact_partitions:
            _restore_compact_grad_partition(i, grad_group)   ← 从 host 恢复
      → GradPartitionBufferGroup(views, flat_partition)
否 → 传统路径: averaged_gradients[i] = get_flat_partition(...)
```

#### 4.3.3 源码精读

启用条件（Muon 判定已提前到 dtype 判定之前）：

[deepspeed/runtime/zero/stage_1_and_2.py:L1299-L1306](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L1299-L1306)

两个守护子句逻辑上本就等价（先后返回 False 不改变结果），重排的意义是把契约写明白：**Muon 组永远走主机累计器路径**，不进紧凑分区——Muon 在分区填充阶段就把梯度折叠进 momentum（见 L698-L700 的注释），紧凑分区的边界发布/释放协议对它不适用。

缓冲构建与 padding 标记：

[deepspeed/runtime/zero/stage_1_and_2.py:L1316-L1349](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L1316-L1349)

取缓冲（含被释放后从 host 恢复的钩子）：

[deepspeed/runtime/zero/stage_1_and_2.py:L1351-L1360](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L1351-L1360)

视图组类定义（含跨设备迁移时重建视图）：

[deepspeed/runtime/zero/stage_1_and_2.py:L141-L155](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L141-L155)

`_view_specs` 缓存每个视图相对 `flat_partition` 的偏移与形状；`move_flat_partition` 把底层数据搬到别的设备后能按 spec 重建全部视图——这对 CPU 卸载（GPU↔CPU 搬运）至关重要。

`_zero_padding` 标记的用途：求全局范数时跳过 padding 视图（见 4.7 节 [stage_1_and_2.py:L2756](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L2756) 的 `param_gradients` 生成器），避免对齐填充的零被当成参数梯度重复计数。

#### 4.3.4 代码实践

1. **实践目标**：确认紧凑缓冲路径被激活并理解其形状。
2. **操作步骤**：在 bf16 + `data_types.grad_accum_dtype: fp32` 的 ZeRO-2 配置下训练，在 `step()` 前打印 `type(engine.optimizer.averaged_gradients.get(i))`、`flat_partition.numel()` 与 `engine.optimizer.partition_size[i]`。
3. **需要观察的现象**：`averaged_gradients[i]` 是 `GradPartitionBufferGroup` 实例；`flat_partition.numel() == partition_size[i]`（总参数量 / DP 宽度，向上对齐）。
4. **预期结果**：两者相等；元素视图数与本分区参数数一致。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `GradPartitionBufferGroup` 要继承 `list` 而不是只保存一个大张量？
**答案**：下游代码（范数计算、逐参数清理、`copy_grads_in_partition`）都以"参数 → 梯度张量"的粒度工作，列表视图让这些代码无需改动；而 `flat_partition` 又让通信可以直接整块进行，两种视角同时保留。

**练习 2**：`_zero_padding` 视图如果不被范数计算跳过，会导致什么错误？
**答案**：padding 全是零，`vector_norm(0)=0`，平方求和时结果不变——但如果未来 padding 复用为 scratch（写入过非零数据），就会把垃圾值计入全局范数；同时跳过它也是防御性约定，保证"每参数恰好计一次"。

### 4.4 step / zero_grad：一次优化的完整时序

#### 4.4.1 概念说明

`step()` 是所有链条的汇合点。它的时序是：**溢出检查 → 全局范数 → （溢出则清空梯度、清理卸载簿记与驻留的紧凑分区后直接返回）→ unscale+clip → 优化器更新本 rank 的 fp32 分区 → fp32 分区降精度写回 bit16 分区 → all-gather 全量参数**。`zero_grad()` 则负责把模型参数上的 `grad`/`grad_accum` 指针清空——注意 ZeRO-2 下大多数参数的梯度在 reduce 后就被丢弃了，只有本分区参数的梯度活到 step。

本周期（#9044）在溢出跳步路径上补了一个清理动作：若紧凑分区仍驻留 GPU（例如 `retain_gpu_grad_partition_until_step=true`，或 host dtype 装不下运行和），它还保有本窗口的 inf/NaN 和，必须一并清掉，否则下个窗口会把它当成"已累积的和"继续用。

#### 4.4.2 核心流程

```
step()
  ├─ 清空本窗口卸载簿记（_offload_accumulated/_partition_only/_unpublished）
  ├─ 溢出检查 check_overflow（读各分区的 inf/nan 标记）
  ├─ scaled_global_norm()
  │     ├─ cpu_offload → complete_grad_norm_calculation_for_cpu_offload
  │     └─ 否则 → get_grad_norm_direct(averaged_gradients[i], params_in_partition[i])
  ├─ overflow? → zero_grad(set_to_none=True)
  │             + cpu_offload: reset_cpu_buffers()
  │               并把所有 averaged_gradients（含驻留紧凑分区）置 None
  │             + 直接 return
  ├─ 对每个参数组 i：
  │     ├─ flatten：averaged_gradients[i] → single_grad_partition（转 fp32）
  │     ├─ unscale_and_clip_grads([single_grad_partition], scaled_global_grad_norm)
  │     │     combined_scale = clip * loss_scale
  │     │     clip = clamp((norm/loss_scale + 1e-6)/clip_grad, min=1)
  │     ├─ _optimizer_step(i)          ← 基础优化器只更新本 rank 分区
  │     └─ bit16_partitions[partition_id].copy_(fp32_partition)  ← 降精度写回
  └─ all_gather_dp_groups(...)          ← 收回全量参数，供下一轮 forward
```

裁剪系数的数学：

\[
c = \frac{\|g\|_{\text{global}} / s + \epsilon}{\tau}, \quad c \leftarrow \max(c, 1), \quad g \leftarrow \frac{g}{c \cdot s}
\]

其中 \(s\) 是 loss scale，\(\tau\) 是 `gradient_clipping` 阈值，\(\epsilon=10^{-6}\)。\(c \geq 1\) 保证范数小于阈值时不缩放。

#### 4.4.3 源码精读

step 开头清空本窗口的卸载簿记集合（`_offload_unpublished_param_ids` 是本周期新增的第三个）：

[deepspeed/runtime/zero/stage_1_and_2.py:L3116-L3125](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L3116-L3125)

step 的溢出与范数阶段：

[deepspeed/runtime/zero/stage_1_and_2.py:L3130-L3153](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L3130-L3153)

溢出跳步路径（含紧凑分区清理）：

[deepspeed/runtime/zero/stage_1_and_2.py:L3155-L3176](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L3155-L3176)

第 3159 行 `zero_grad(set_to_none=True)` 后**直接返回**——本步不做任何参数更新，这是 fp16 动态 loss scaling 的"跳步"语义（对照 u4-l1）。第 3161-3165 行：CPU 卸载时先 `reset_cpu_buffers()`（4.10 节），再把所有 `averaged_gradients` 置 None——注释写明"A resident compact partition still holds the skipped window's inf/NaN sum"。

无卸载路径的 flatten → clip → step → 写回（调用顺序清晰可见）：

[deepspeed/runtime/zero/stage_1_and_2.py:L3210-L3254](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L3210-L3254)

第 3226 行 `single_grad_partition = flat_grad_partition.to(...)` 就是"flatten+升精度"，第 3241 行 unscale+clip，第 3247 行 `_optimizer_step(i)`，第 3253 行把更新后的 fp32 分区拷回 bit16 分区。CPU 卸载分支（L3186-L3208）直接用 host 梯度分区作为 `single_grad_partition`，无需搬运。

最后一步 all-gather 收回全量参数：

[deepspeed/runtime/zero/stage_1_and_2.py:L3260-L3268](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L3260-L3268)

`unscale_and_clip_grads` 的缩放公式实现：

[deepspeed/runtime/zero/stage_1_and_2.py:L3301-L3324](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L3301-L3324)

`zero_grad`（含 `grad_accum` 指针清理）：

[deepspeed/runtime/zero/stage_1_and_2.py:L2689-L2704](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L2689-L2704)

#### 4.4.4 代码实践

1. **实践目标**：用引擎计时器分解一次 step 的时间构成。
2. **操作步骤**：配置 `"tensorboard": {"enabled": true}`（或 `"monitor_enabled": true`），ZeRO-2 训练若干步后查看 `optimizer/gradient` 、`optimizer/step`、`optimizer/allgather` 三个计时器（`OPTIMIZER_GRADIENTS_TIMER` / `OPTIMIZER_STEP_TIMER` / `OPTIMIZER_ALLGATHER_TIMER`，见 [stage_1_and_2.py:L3184](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L3184)、[L3246](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L3246)、[L3260](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L3260)）。
3. **需要观察的现象**：大模型下 allgather 计时器占比显著（Stage 1/2 的参数仍需全量同步）；offload 时 step 计时器含 CPU 优化器时间。
4. **预期结果**：得到三段耗时的比例表。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：溢出时 step 做了什么？为什么不做参数更新？
**答案**：检测到 inf/nan 后把 `grad`/`grad_accum` 全部置 None、CPU 卸载时调 `reset_cpu_buffers()` 并清掉驻留的紧凑分区、释放缓冲、计时器空转后 return（[stage_1_and_2.py:L3155-L3176](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L3155-L3176)）。因为含溢出的梯度算出的范数与更新都不可信，跳过本步并等 loss scale 降档（u4-l1）。

**练习 2**：`zero_grad(set_to_none=False)` 与 `set_to_none=True` 的区别？
**答案**：`set_to_none=True`（默认）把 `p.grad` 与 `p.grad_accum` 指针直接置 None，速度快且释放引用；`False` 则原地 `zero_()` 保留存储（[stage_1_and_2.py:L2698-L2704](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L2698-L2704)）。

### 4.5 Adagrad 的 param_groups 重建

#### 4.5.1 概念说明

大多数优化器（Adam 等）在**第一次 step 时**才惰性创建状态（momentum 等）。但 `torch.optim.Adagrad` 例外：它在**构造时**就为传入的参数初始化 `state`（初始梯度平方和等）。`DeepSpeedZeroOptimizer` 包装基础优化器时，传给基础优化器的是 fp32 分区张量视图；随后 `initialize_optimizer_states` 会给 `single_partition_of_fp32_groups` 挂上 `.grad` 并重建梯度分区。如果此时直接沿用构造期基于旧张量建好的 Adagrad 状态，状态张量会指错设备/张量。

修复方式：如果基础优化器是 Adagrad，就用**它自己的 `param_groups`**（而非 fp32 分区张量清单 `single_partition_of_fp32_groups`）连同默认超参重建一个全新的 Adagrad。用 `param_groups` 的关键在于它保留了每个参数组各自的选项（lr、eps 等）与参数身份，重建后状态会在正确的参数（也就是 fp32 分区视图本身）上重新初始化。

#### 4.5.2 核心流程

```
initialize_optimizer_states()
  ├─ 为每组 i 构造 single_grad_partition（partition_size 大小的零张量）
  │    挂到 single_partition_of_fp32_groups[i].grad（可 pin memory）
  └─ if isinstance(optimizer, torch.optim.Adagrad):
        optimizer = torch.optim.Adagrad(optimizer.param_groups, **optimizer.defaults)
        ← 用参数组（保留每参数组选项）而非分区张量列表重建
```

#### 4.5.3 源码精读

[deepspeed/runtime/zero/stage_1_and_2.py:L948-L967](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L948-L967)

第 957-961 行的注释与重建明确解释了差异："State initialization for the Adagrad optimizer occurs at construction as opposed to other optimizers which do lazy initialization of the state at the first call to step"。另外注意第 950-955 行：初始梯度分区在 `self.device`（参数所在设备）上创建——Adagrad 构造期初始化要求梯度与参数同设备，CPU/卸载参数不能默认放到当前加速器设备。

#### 4.5.4 代码实践

1. **实践目标**：验证 Adagrad 路径可正常工作。
2. **操作步骤**：写一个单进程 ZeRO-2 配置，optimizer 用 `torch.optim.Adagrad`（引擎配置 `"optimizer": {"type": "Adagrad", ...}`），跑几步并打印 loss。可参考 `tests/unit/runtime/zero/test_zero_adagrad.py` 的断言方式。
3. **需要观察的现象**：训练正常推进、无设备错位报错；`engine.optimizer.optimizer` 是重建后的 `torch.optim.Adagrad` 实例，其 `state` 的 key 指向 fp32 分区张量。
4. **预期结果**：loss 下降，Adagrad 状态在 fp32 分区上。**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：为什么重建时传 `self.optimizer.param_groups` 而不是 `single_partition_of_fp32_groups`？
**答案**：`param_groups` 里的 `params` 本来就是 fp32 分区张量（构造包装器时已传入），且每组携带各自的超参（lr/eps/weight_decay 等）与元数据（如 MoE 组的 `name`、`moe` 标记）；直接传分区列表会丢失这些每组选项。

**练习 2**：Adam 需要同样的重建吗？
**答案**：不需要。Adam 的状态在首次 `step()` 惰性创建，那时 `.grad` 与参数布局都已就绪；只有构造期就初始化状态的 Adagrad 需要（[stage_1_and_2.py:L957-L961](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L957-L961)）。

### 4.6 从主机恢复梯度前的 scratch 清零

#### 4.6.1 概念说明

CPU 卸载 + 梯度累积场景下，非边界 backward 的梯度累积发生在主机上；每个参数只把自己**属于本 rank 分区的那一段**（`num_elements` 长度的切片）存进主机累计器（4.8 节）。在梯度累积边界，`_restore_cpu_offload_grad_to_gpu` 把累积梯度搬回 GPU 供范数计算与溢出检查使用。

问题在于搬运目的地 `temp_grad_buffer_for_gpu_offload` 是**所有参数共享的一块 scratch**。恢复时只写入 `num_elements` 长度的分区切片，scratch 里剩下的区域是**上一次某个参数留下的残影**——如果上一步发生过溢出，那里可能还是 NaN。溢出检查读的是"恢复出来的完整梯度"，就会把残留 NaN 误判为本步溢出，造成永远跳步的死循环。

修复：恢复前先把整个 scratch 清零（`dest_buffer.zero_()`），再只把有效切片拷进去。这个清零最初（#8728）只加在低精度 master 分支上；本周期（#9018）主机累计器改为按分区组的切片后，**fp32 master 同样只恢复分区切片**，于是清零变为无条件——对所有 master 精度组合一视同仁。

#### 4.6.2 核心流程

```
_restore_cpu_offload_grad_to_gpu(param)
  dest_buffer = temp_grad_buffer_for_gpu_offload[:param.numel()]   ← 共享 scratch
  dest_buffer.zero_()                ← 无条件清零（本周期起覆盖全部精度组合）
  dest_buffer[source_offset : source_offset+num_elements].copy_(
      _get_cpu_offload_accumulator_slice(param))   ← 只拷本 rank 的分区切片
  restored = dest_buffer.view_as(param).clone()   # 克隆，scratch 可复用
```

#### 4.6.3 源码精读

[deepspeed/runtime/zero/stage_1_and_2.py:L1151-L1167](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L1151-L1167)

第 1158 行的注释直指要害："The overflow scan reads the whole tensor, so scratch data outside this rank's fragment must be cleared"。第 1160 行从 `_get_cpu_offload_accumulator_slice` 取源切片——低精度 master 直接用分区梯度切片，fp32 master 用按分区组的主机累计器切片（4.8 节）。第 1163 行的 `clone()` 同样重要：不克隆的话下一个参数复用 scratch 会把本参数的梯度覆盖掉。

主机累积如何发生（重写后的唯一累积入口）：

[deepspeed/runtime/zero/stage_1_and_2.py:L1911-L1937](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L1911-L1937)

`async_accumulate_grad_in_cpu_via_gpu`：取本参数的分区切片 `grad_accum` 与主机累计切片 `accumulated`；若本参数本窗口已有累积（在 `_offload_accumulated_param_ids` 里），把旧和（或 `_offload_partition_only_param_ids` 场景下的分区梯度）经 GPU scratch 中转后 `add_` 到 `grad_accum`；最后把新和写回主机切片。约十个 dtype/精度分支被压缩成了这一个统一流程（对照 4.8 节）。

边界时两条路径的分派（紧凑分区组走 `_offload_compact_grad_partitions`，其余走逐参数发布）：

[deepspeed/runtime/zero/stage_1_and_2.py:L2225-L2257](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L2225-L2257)

低精度 master 开关的配置与校验入口：

[deepspeed/runtime/zero/stage_1_and_2.py:L333-L346](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L333-L346)

低精度 master 要求 ZeRO-Offload + DeepSpeedCPUAdam（`_enforce_cpu_offload` 校验），最终得到 `master_weights_and_grads_dtype`；不等于 fp32 即 `low_precision_master_weights_and_grads=True`。

#### 4.6.4 代码实践

1. **实践目标**：复现"共享 scratch 残留 NaN 导致误报溢出"的病态，并验证清零修复。
2. **操作步骤**：
   - 配置 ZeRO-2 + `offload_optimizer.device=cpu` + `bf16.enabled=true`（或 fp16 对应开关），fp16 场景下开 `loss_scale` 动态调整。
   - 人为制造一次溢出：给模型某一层乘一个巨大常数（如 `1e5` 连乘若干次）或把某参数梯度 hook 里写入 inf，跑一步让 loss scale 降档。
   - 恢复正常输入继续训练，观察每步 `engine.optimizer.overflow` 与 loss scale。
   - 更可靠的验证是运行现成回归：`pytest "tests/unit/v1/zero/test_zero2_offload_multi_backward.py::TestZero2OffloadOverflowRecovery::test_finite_step_after_overflow" -v`。该测试用两个参数组（12 与 8 个元素）先制造一次真实的 NaN 溢出步，再要求随后两个正常步都必须更新两个组的参数（[L357-L412](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/tests/unit/v1/zero/test_zero2_offload_multi_backward.py#L357-L412)）；当前按 precision（bf16/fp16）× master（低精度/fp32）× 累积 dtype（默认/fp32）共八种组合参数化，#8728 的提交信息说明其最初覆盖 bf16/fp16 × 低精度/fp32 master 四种组合。
3. **需要观察的现象**：修复后，溢出后的下一步在正常输入下 `overflow=False`、参数恢复更新；若把 [stage_1_and_2.py:L1159](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L1159) 的 `dest_buffer.zero_()` 临时注释掉（仅本地实验，勿提交），则可能观察到连续误报溢出。
4. **预期结果**：scratch 清零后无误报。**待本地验证**（需要能触发真实溢出的环境；CPU 双进程可用 `LOCAL_SIZE=2` 跑上述测试）。

#### 4.6.5 小练习与答案

**练习 1**：为什么现在**所有** master 精度组合恢复时都只拷 `num_elements` 切片、都需要清零？
**答案**：#9018 之后 fp32 master 的主机累计器也是按分区组布局的缓冲，参数只取自己的 `dest_offset` 切片（`_get_cpu_offload_accumulator_slice`，[stage_1_and_2.py:L1939-L1954](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L1939-L1954)）；低精度 master 本来就用分区梯度切片。于是 scratch 的"无主区域"对两种精度都存在，清零必须无条件（[stage_1_and_2.py:L1158-L1161](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L1158-L1161)）。

**练习 2**：`restored = dest_buffer.view_as(param).clone()` 里的 `clone()` 为什么必不可少？
**答案**：`temp_grad_buffer_for_gpu_offload` 是所有参数轮流复用的共享缓冲，不克隆的话下一个参数恢复时会直接改写本参数刚恢复的梯度。

### 4.7 折叠专家的梯度范数组（PR #8823）

#### 4.7.1 概念说明

先回顾旧世界（原生 MoE / ETP）的范数计算。`scaled_global_norm` 对每个参数组调 `get_grad_norm_direct`，其中做两级归约：

1. 在 DP（或 expert-DP）组上 all-reduce SUM——因为 ZeRO-2 把梯度切到各 rank，每个 rank 只算得到局部平方和；
2. 在模型并行组上过滤：只有 `model_parallel_rank == 0`（或 `is_model_parallel_parameter`）的参数计入——因为 TP 复制的参数每个 TP rank 都有相同梯度，全算会重复计数。

算完后 `_average_expert_grad_norms` 再对 MoE 参数组做一次"除以 expert-DP world_size 再 all-reduce"的平均。

**折叠改变了前提**。AutoEP + AutoTP folding 把专家维折叠进 TP 维：TP 组内的 peers 各持有**不同的路由专家**（参数带 `ds_zero_placement_family = "autoep_expert"` 标记，见 [deepspeed/module_inject/auto_ep_layer.py:L679](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/module_inject/auto_ep_layer.py#L679)）。此时：

- 旧路径过滤到 TP rank 0 会**漏掉**非 0 TP rank 持有的专家——这些专家不是复制，是独占的属主；
- 专家范数平均也不再成立——折叠组里每个 rank 的范数贡献互不相同，取平均等于丢掉大部分专家的能量。

修复后的正确语义（与 [docs/code-docs/source/autoep.rst 的"Gradient clipping under folding"契约](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/docs/code-docs/source/autoep.rst#L609-L618)一致）：折叠专家组的平方范数先在 **expert-DP 分区组**上求和、再在 **EP owner 组**上求和，每个专家属主恰好贡献一次（包括 TP rank 非 0 的属主），且**不再做专家范数平均、不再做 TP 求和**。稠密组与原生 MoE 组保持原路径，两者可在同一优化器中共存。

#### 4.7.2 核心流程

```
scaled_global_norm()
  for 每个参数组 i:
    folded = _is_folded_expert_group(i)
           = autoep_folding_spec 已配置
             且 has_moe_layers
             且 is_moe_param_group[i]
             且组内所有参数 ds_zero_placement_family == "autoep_expert"
    if folded:
        group_name = optimizer.param_groups[i]["name"]
        norm = get_grad_norm_direct(...,
                 process_group=real_dp_process_group[i],   ← expert-DP 分区组
                 expert_parallel_group=ep_process_group[group_name]) ← EP owner 组
    else:
        norm = get_grad_norm_direct(...)                   ← 原路径

get_grad_norm_direct(gradients, params, norm_type, process_group, expert_parallel_group)
  局部: total = Σ_i ‖g_i‖²（折叠组不过滤 TP rank 0，全部计入）
  第 1 级: all_reduce SUM @ process_group（默认 dp_process_group）
  第 2 级: expert_parallel_group 为 None → _model_parallel_all_reduce（原路径）
           否则 → all_reduce SUM @ expert_parallel_group（折叠新路径）

_average_expert_grad_norms(norm_groups)
  仅对 is_moe_param_group[i] 且【非折叠组】做平均
```

折叠组的范数数学（每个属主恰好一次）：

\[
\|g_E\|^2 = \sum_{o \in G_{\text{EP}}} \; \sum_{r \in G_{\text{EDP}}(o)} \; \sum_{i \in \text{partition}(r)} \|g_i\|_2^2
\]

#### 4.7.3 源码精读

折叠组判定：

[deepspeed/runtime/zero/stage_1_and_2.py:L3033-L3037](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L3033-L3037)

四个条件缺一不可：folding spec 存在、模型有 MoE 层、该参数组标记为 MoE 组、组内**所有**参数都属于 `autoep_expert` 放置族（混合组走原路径）。

按组选择归约组：

[deepspeed/runtime/zero/stage_1_and_2.py:L3039-L3054](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L3039-L3054)

`scaled_global_norm` 里折叠组从 `optimizer.param_groups[i]["name"]` 找到专家组名，进而取 `ep_process_group[group_name]`（EP owner 组）与 `real_dp_process_group[i]`（对 MoE 组它已在 `_configure_moe_settings` 中被替换为 expert-DP 组）。

`get_grad_norm_direct` 的新签名与两级归约：

[deepspeed/runtime/zero/stage_1_and_2.py:L2724-L2786](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L2724-L2786)

三个关键点：第 2741-2742 行 `process_group` 缺省回落到 DP 组；第 2761-2762 行 `expert_parallel_group is not None` 时**跳过 TP rank 0 过滤**（折叠组内每个 rank 的专家都要计入）；第 2775-2780 行第二级归约——折叠组在 EP owner 组上 SUM，代码注释写明原因："Folded EP spans TP peers: each owns different experts. Sum their partitions over expert DP and EP instead of filtering to TP rank zero."

专家范数平均对折叠组的豁免：

[deepspeed/runtime/zero/stage_1_and_2.py:L3292-L3299](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L3292-L3299)

条件从 `is_moe_param_group[i]` 变为 `is_moe_param_group[i] and not self._is_folded_expert_group(i)`。

折叠下梯度本身的 TP 校正入口（与范数修复配套的既有机制）：

[deepspeed/runtime/zero/stage_1_and_2.py:L1254-L1272](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L1254-L1272)

`configure_autoep_folding_tp_gradient_reduction` 记录 folding spec 与 TP 组；`_maybe_reduce_autoep_folding_tp_gradient` 在每个梯度进桶前对其做折叠校正（`apply_folding_correction_to_grad_buffer`）。

契约文档：

[docs/code-docs/source/autoep.rst:L609-L618](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/docs/code-docs/source/autoep.rst#L609-L618)

明确写道：routed-expert 范数按 EDP 再 EP 两级求平方和贡献；每个专家属主都参与，包括 TP rank 非 0 的属主；这个专家总量**不再**做 TP 求和；稠密参数与原生 MoE 组保持原有归约。

回归测试（对照断言的核心）：

[tests/unit/v1/moe/test_autoep_autotp_sharded_attention.py:L548-L559](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/tests/unit/v1/moe/test_autoep_autotp_sharded_attention.py#L548-L559)

`TestShardedAttentionClippedUpdate.test_sgd_update_matches_reference` 通过 `_clipped_step_against_reference` 把折叠切分下的**实际 SGD 更新**与未切分 ZeRO-0 参考实现逐参数对照——只对范数是不够的，某个参数族可能逃逸裁剪或被裁两次，所以直接断言更新量。

混合组共存的直接验证：

[tests/unit/v1/moe/test_autoep_autotp_sharded_attention.py:L360-L438](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/tests/unit/v1/moe/test_autoep_autotp_sharded_attention.py#L360-L438)

`TestMixedExpertClipping` 在同一个 `DeepSpeedZeroOptimizer` 里放一个原生 ETP 专家组和一个折叠专家组，手工构造梯度（原生组范数 5、折叠专家在 TP rank 1 贡献 12），断言全局范数为 13（折叠时）或 5（不折叠时），并断言裁剪后的参数更新量精确等于 \(-\eta \cdot \frac{\tau}{\|g\|+\epsilon} \cdot g\)。这个测试是理解两种范数路径共存的最好教材。

#### 4.7.4 代码实践

1. **实践目标**：通过阅读测试理解"折叠专家组为何不能走专家范数平均"。
2. **操作步骤**：
   - 精读 [tests/unit/v1/moe/test_autoep_autotp_sharded_attention.py:L360-L438](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/tests/unit/v1/moe/test_autoep_autotp_sharded_attention.py#L360-L438)：注意第 384-392 行——折叠专家参数显式设置 `ds_zero_placement_family = "autoep_expert"`，且两个专家组的 `ep_groups` / `edp_groups` 映射不同（折叠组的 EP 组是 **TP 组**）。
   - 手工推演期望范数：`expected_norm = 13.0 if folded else 5.0`（第 393-394 行）。若折叠组错误地走了"过滤 TP rank 0"，TP rank 1 持有的折叠专家（贡献 12² =144）会丢失，范数将变成 5 而非 13；若错误地做了专家平均，范数会偏离 13。
   - 有加速器时运行：`pytest tests/unit/v1/moe/test_autoep_autotp_sharded_attention.py::TestMixedExpertClipping -v`（CPU 环境会 skip，见第 366-367 行）。
3. **需要观察的现象**：测试通过；把断言中的 `expected_norm` 改成 5.0（折叠分支）可以观察到它失败——这就是该修复所守护的行为。
4. **预期结果**：折叠与不折叠两个分支的范数分别为 13 与 5，裁剪系数 `threshold/(expected_norm+1e-6)` 精确作用于两组参数。CPU 环境下只能完成阅读部分，**运行部分待本地验证**。

#### 4.7.5 小练习与答案

**练习 1**：折叠后 TP peers 各持不同专家，为什么旧的"过滤到 TP rank 0"会出错？
**答案**：旧过滤的前提是"TP 组内各 rank 持有**相同**参数的复制，只需计一次"。折叠后 TP 组内各 rank 的专家参数**互不相同**（各是不同专家的属主），过滤到 rank 0 会把其余 rank 独占的专家整个从范数里丢掉（[stage_1_and_2.py:L2761-L2762](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L2761-L2762) 用 `expert_parallel_group is not None` 跳过过滤）。

**练习 2**：`_average_expert_grad_norms` 为什么对折叠组要跳过？
**答案**：折叠组的范数已经通过 EDP 组 + EP owner 组的两级 SUM 凑齐了所有属主的贡献（每个专家恰好一次）；再做除以 EDP world_size 的平均会把它缩小，导致裁剪系数偏大（裁不够）。豁免逻辑见 [stage_1_and_2.py:L3294](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L3294)。

**练习 3**：一个参数组里既有折叠专家又有稠密参数，`_is_folded_expert_group` 返回什么？走哪条路径？
**答案**：返回 False——判定要求组内**所有**参数的 `ds_zero_placement_family` 都是 `autoep_expert`（[stage_1_and_2.py:L3033-L3037](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L3033-L3037)）。混合组走原路径；实践中折叠专家总是单独成组（AutoEP 注入时按组划分），`TestMixedExpertClipping` 里原生组与折叠组也是两个独立的 param group。

### 4.8 按分区组的主机梯度累计器（本周期修复，PR #9018）

#### 4.8.1 概念说明

CPU 卸载 + 多次 backward（GAS>1 或非边界 backward）时，梯度运行和要存放在主机上。**旧实现**把 `accumulated_grads_in_cpu` 做成按参数 id 的惰性 dict：fp32 master 给每个参数分配**整参数大小**的缓冲；低精度 master 则在两处代码里分别创建指向分区梯度的别名视图。累积、拷回、恢复三件事各自分支于"手里拿的是哪种缓冲"——四个函数里散布着约十个分支；pinned 缓冲从不解 pin；低精度恢复还会在共享 scratch 里留下残值（4.6 节的病根之一）。

**新实现**把数据结构换成了与 FP32 分区同构的布局：`accumulated_grads_in_cpu` 是一个长度等于参数组数的列表，每组一个 `partition_size` 大小、bit16 dtype 的缓冲（`cpu_offload_pin_memory` 时 pinned）；每个参数按自己的 `dest_offset` 取**固定切片**，与它在 FP32 分区中的位置一一对应。三个直接收益：

1. **分支消失**：`_get_cpu_offload_accumulator_slice()` 成为取缓冲的唯一入口，低精度 master 依旧直接用分区梯度（该入口里一个 if 处理），其余代码不再关心精度组合；
2. **GAS=1 零开销**：缓冲只在**首个非边界累积**时分配——边界式训练（每个 backward 都是边界、立即发布到分区）全程不 pin 任何额外主机内存；
3. **生命周期完整**：`destroy()` 里与其它卸载缓冲一起统一 unpin。

顺带清理：无调用方的 `set_norm_for_param_grad` 被删除；范数改由边界发布路径（4.9 节）与 `set_norm_for_param_grad_in_gpu` 计算。

#### 4.8.2 核心流程

```
__init__ (cpu_offload):
  accumulated_grads_in_cpu = [None] * len(partition_size)   ← 惰性，逐组分配
  _offload_accumulated_param_ids   = set()  # 本窗口累积过的参数
  _offload_partition_only_param_ids = set() # 首用即边界：和只在分区里
  _offload_unpublished_param_ids    = set() # 非边界 backward 更新过 host 和
  _host_owned_compact_partitions    = set() # 已释放的紧凑分区（4.9 节）

非边界 backward 对参数 p:
  accumulated = _get_cpu_offload_accumulator_slice(p)
      低精度 master → single_partition_of_fp32_groups[g].grad 的切片
      fp32 master   → accumulated_grads_in_cpu[g]（None 则此刻分配/pin）
  旧和 + 新梯度 → 写回 accumulated

首个非边界累积 → 才发生分配（GAS=1 永不分配）
destroy() → 逐组 unpin
```

#### 4.8.3 源码精读

初始化（数据结构与四个簿记集合，注释写明惰性分配的动机）：

[deepspeed/runtime/zero/stage_1_and_2.py:L648-L672](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L648-L672)

唯一的取缓冲入口：

[deepspeed/runtime/zero/stage_1_and_2.py:L1939-L1954](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L1939-L1954)

注意 `dtype=self.bit16_groups_flat[group_idx].dtype`——累计器用**模型精度**（bit16），不是 fp32；fp32 精度的累积由紧凑分区路径承担（4.9 节），主机累计器只服务默认的 bit16 累积与 Muon 组。

统一后的累积流程：

[deepspeed/runtime/zero/stage_1_and_2.py:L1911-L1937](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L1911-L1937)

destroy 时的统一解 pin：

[deepspeed/runtime/zero/stage_1_and_2.py:L799-L801](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L799-L801)

GAS=1 零分配的资源回归测试：

[tests/unit/v1/zero/test_zero2_gradient_safety.py:L170-L195](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/tests/unit/v1/zero/test_zero2_gradient_safety.py#L170-L195)

`test_single_boundary_offload_avoids_auxiliary_storage_and_copy` monkeypatch 了 `torch.Tensor.copy_` 来计数：每次边界 backward 只允许**一次**拷贝（写进 fp32 分区梯度），并直接断言 `opt.accumulated_grads_in_cpu[0] is None`——主机累计器从未被分配；随后与 `torch.optim.Adam` 参考实现对照权重。

#### 4.8.4 代码实践

1. **实践目标**：验证「GAS=1 不分配主机累计缓冲」与「非边界累积才分配」。
2. **操作步骤**：
   - 运行 `pytest tests/unit/v1/zero/test_zero2_gradient_safety.py::test_single_boundary_offload_avoids_auxiliary_storage_and_copy -v`（fp32/bf16 master 两个参数化分支）。
   - 再写一个小脚本（或改用 `tests/unit/v1/zero/test_zero2_offload_multi_backward.py` 的 `test_multiple_request_boundaries_host_accumulator`，[L299-L306](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/tests/unit/v1/zero/test_zero2_offload_multi_backward.py#L299-L306)）：ZeRO-2 + CPU 卸载，先做一次边界 backward 打印 `engine.optimizer.accumulated_grads_in_cpu`（应全为 None），再做一次非边界 backward 后打印（对应组应变为张量）。
3. **需要观察的现象**：边界-only 场景列表始终是 `[None, ...]`；发生非边界累积后对应组出现 `partition_size` 大小、dtype 等于模型 dtype 的（pinned）张量。
4. **预期结果**：与上述一致；MUON 组即使 fp32 累积也走这条主机累计器路径（`_use_compact_grad_partition_buffer` 的第一道守护，[L1299-L1306](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L1299-L1306)）。**待本地验证**。

#### 4.8.5 小练习与答案

**练习 1**：为什么累计器用 bit16 dtype 而不是 fp32？
**答案**：fp32 精度累积的场景已经由**紧凑分区**（GPU 端 fp32 缓冲，4.9 节）承担；主机累计器只覆盖默认的 bit16 梯度累积（此时 `gradient_accumulation_dtype` 本来就是 bit16）和 Muon 组。各归其位后，`_use_compact_grad_partition_buffer` 与 `_get_cpu_offload_accumulator_slice` 是互补的两条路径，不会同时服务同一参数组。

**练习 2**：旧实现的「两处别名视图」指什么？为什么是坏味道？
**答案**：低精度 master 的 CPU 累积缓冲是**别名**——直接指向 `single_partition_of_fp32_groups[i].grad` 的切片，且在 `async_accumulate_grad_in_cpu_via_gpu` 与 `copy_grads_in_partition` 两处分别创建（旧代码 L2158-L2202 一带）。别名意味着"同一个物理存储有两个身份"，累积/恢复代码必须处处区分手里是_owned buffer_还是_alias_，这正是十个分支的来源；#9018 把这个区分收敛进 `_get_cpu_offload_accumulator_slice` 的一个 if。

**练习 3**：`_offload_partition_only_param_ids` 集合的语义是什么？
**答案**：记录"本窗口首用即边界"的参数——它的运行和只存在于 fp32 分区梯度里（边界 backward 已发布），主机累计器里没有它的旧和；后续若再来一次非边界 backward，累积时要从分区梯度取旧和而不是从主机切片取（[stage_1_and_2.py:L1928-L1932](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L1928-L1932)）。

### 4.9 紧凑分区的边界发布与释放（本周期修复，PR #9044）

#### 4.9.1 概念说明

「紧凑梯度分区」（4.3 节）是 GPU 上一块 `partition_size` 大小的 fp32 缓冲，服务「fp32 梯度累积 + 16 位模型」的组合。#9044 之前，这块缓冲一旦建起就驻留到 `step()`——即使两次 backward 之间（请求间）它完全闲置。对于 ZeRO-2 + CPU 卸载的 RLHF 类负载（一个优化器窗口内多次"请求边界 backward"），这等于每个 rank 白白多占一块 fp32 分区。

新协议把它变成**按需驻留**：

1. **发布**：每个请求边界，`_offload_compact_grad_partitions` 把本窗口每个参与累积的参数的运行和从紧凑分区拷进 host 梯度（`single_partition_of_fp32_groups[g].grad` 的对应切片），顺带记录逐参数范数、做 host dtype 溢出检查（4.10 节）。
2. **释放**：仅当 host 梯度能**逐位恢复**设备上的运行和时（`_host_grad_holds_accum_exactly`：同 dtype，或 fp32 host 持有 bf16/fp16 值），才把 `averaged_gradients[g]` 置 None、登记进 `_host_owned_compact_partitions`。bf16 master + fp32 累积（host 是 bf16，装不下 fp32 和）不支持释放——分区驻留到 `step()`。
3. **恢复**：窗口内下一次 backward 需要写紧凑分区时，`_get_compact_grad_partition_buffer` 发现该组被释放过，先重建全零缓冲再从 host 梯度把运行和拷回来（`_restore_compact_grad_partition`）。
4. **兜底**：非边界 backward 只更新了 host 累计器的参数（`_offload_unpublished_param_ids`），若之后再也没有 backward 触碰它，它的和就到不了 fp32 分区——`_publish_unpublished_offload_grads` 在每个边界把这类"未发布"的 host 和补发布进分区，否则它会以零梯度到达 `step()`、`safe_get_full_grad` 与范数计算。
5. **跳步清理**：溢出跳步时驻留的紧凑分区（可能仍持本窗口的 inf/NaN 和）被一并置 None（4.4 节）。

配套新配置 `zero_optimization.retain_gpu_grad_partition_until_step`（默认 `false`）：置 `true` 禁止释放，分区驻留到 `step()`——请求间显存多一块 fp32 分区，换来省去每请求的主机拷贝与下次 backward 的恢复（PR 记录 Qwen3-4B 单卡 stage 2：释放路径 backward 474→733ms，开启 retain 后回到约 455ms，且 step 时间不变）。

#### 4.9.2 核心流程

```
ipg_epilogue（每次 backward 收尾）
  └─ 若本次是边界 backward（非 cpu_offload 分支之外）:
        _publish_unpublished_offload_grads()   ← 兜底非边界留下的 host 和
        _offload_compact_grad_partitions()     ← 发布 + 视条件释放

_offload_compact_grad_partitions():
  for 每个使用紧凑分区的组 g:
    for 本窗口累积过的参数 p:
      source = 紧凑分区中 p 的切片
      norm_for_param_grads[p] = ‖source‖₂
      host_value = source.to(host_grad.dtype)     ← 先 cast
      update_offload_overflow_tracker(host_value) ← 检查 cast 后的值
      host_grad[dest_offset : +num_elements].copy_(host_value)
    可释放? = _host_grad_holds_accum_exactly(host_dtype, fp32)
             且 offload gradient safety 启用（非 ZenFlow）
             且 not retain_gpu_grad_partition_until_step
    是 → averaged_gradients[g] = None; _host_owned_compact_partitions.add(g)

下一次 backward 写紧凑分区:
  _get_compact_grad_partition_buffer(g)
    └─ g ∈ _host_owned_compact_partitions → _restore_compact_grad_partition(g)
           （重建缓冲，从 host_grad 逐参数切片拷回运行和）
```

#### 4.9.3 源码精读

epilogue 的边界钩子（两次调用的入口）：

[deepspeed/runtime/zero/stage_1_and_2.py:L1072-L1074](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L1072-L1074)

unmanaged 模式（手动 `engine.backward`）的对应入口在 `_finalize_cpu_offload_gradient_accumulation`（[L1109-L1124](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L1109-L1124)）。

发布与释放的主体：

[deepspeed/runtime/zero/stage_1_and_2.py:L1362-L1402](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L1362-L1402)

四个关键点：L1384 逐参数范数在发布时顺便记录（供 `complete_grad_norm_calculation_for_cpu_offload` 使用）；L1385-L1388 溢出检查对 **cast 到 host dtype 后的值**做（原因见 4.10 节）；L1389 发布拷贝；L1392-L1402 释放的三道门（可逐位恢复 / 非 ZenFlow 的梯度安全 / 未开 retain）。

可逐位恢复判定：

[deepspeed/runtime/zero/stage_1_and_2.py:L1308-L1314](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L1308-L1314)

同 dtype（fp32 host + fp32 累积），或 fp32 host 持有 bf16/fp16 值（此时 host 是"加宽"存储，恢复无损）。bf16 host + fp32 累积不满足——释放后下次恢复会丢位，所以分区必须驻留。

恢复路径：

[deepspeed/runtime/zero/stage_1_and_2.py:L1404-L1417](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L1404-L1417)

注意 L1407 的 `_wait_for_offload_copies()`——恢复可能与填 host 梯度的边界拷贝跑在不同 stream 上，必须先等它完成。

取缓冲时的恢复钩子：

[deepspeed/runtime/zero/stage_1_and_2.py:L1351-L1360](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L1351-L1360)

逐参数路径的记账变化（紧凑组的边界拷贝改由 `_offload_compact_grad_partitions` 按组执行）：

[deepspeed/runtime/zero/stage_1_and_2.py:L2228-L2233](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L2228-L2233)

未发布梯度的兜底：

[deepspeed/runtime/zero/stage_1_and_2.py:L1126-L1149](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L1126-L1149)

`_publish_unpublished_offload_grads` 的 docstring 把问题说得很清楚："A parameter whose last contribution this window came from a non-boundary backward would otherwise reach step(), safe_get_full_grad() and the grad norm as zero."

新配置字段（默认 false，docstring 完整描述了语义与代价）：

[deepspeed/runtime/zero/config.py:L150-L159](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/config.py#L150-L159)

引擎侧接线（读取器 + 构造参数透传）：

[deepspeed/runtime/engine.py:L1645-L1646](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L1645-L1646)、[deepspeed/runtime/engine.py:L2736-L2740](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/engine.py#L2736-L2740)

保留/释放两模式的回归测试（三次边界 backward + 一次 step，断言驻留模式差异与最终权重一致）：

[tests/unit/v1/zero/test_zero_offload_accum_precision.py:L171-L210](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/tests/unit/v1/zero/test_zero_offload_accum_precision.py#L171-L210)

`_retain_option_run` 每个边界后记录 `engine.optimizer.averaged_gradients.get(0) is not None`（驻留标志）；`TestZeroOffloadReleaseOption` 断言释放模式三个边界全是 False、retain 模式全是 True，且两种模式的窗口梯度与最终权重完全一致——释放只是显存调度，不改变训练结果。

#### 4.9.4 代码实践

1. **实践目标**：用 `torch.cuda.memory_allocated` 实测请求边界后紧凑分区被释放、`retain_gpu_grad_partition_until_step=true` 时驻留，且两种设置最终权重一致。
2. **操作步骤**：
   - 参照 [tests/unit/v1/zero/test_zero_offload_accum_precision.py:L171-L210](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/tests/unit/v1/zero/test_zero_offload_accum_precision.py#L171-L210) 的 `_retain_option_run`：bf16 模型 + ZeRO-2 + `offload_optimizer.device=cpu` + `data_types.grad_accum_dtype=fp32`，配置 `train_batch_size` 使多次 backward 共享一个优化器窗口；每个边界 backward 后打印 `torch.cuda.memory_allocated()` 与 `engine.optimizer.averaged_gradients.get(0) is not None`。
   - 分别以 `retain_gpu_grad_partition_until_step=false/true` 各跑一轮，比较边界后显存差（应约等于一块 fp32 分区 = 参数量×4/N_d 字节）与每步 backward 耗时；最后比较两轮的 `engine.module` 权重。
3. **需要观察的现象**：false 时边界后 `averaged_gradients` 为 None、显存回落一块分区；true 时始终驻留、backward 省去主机拷贝/恢复；两轮权重逐位一致（bf16 master + fp32 累积的 unsupported 组合则两边都驻留）。
4. **预期结果**：显存差 ≈ 模型参数量×4 字节/N_d（PR 记录 B200 上 134M 参数合成模型差 512MB、Qwen3-4B 单卡差约 15.3GB）。**待本地验证**。

#### 4.9.5 小练习与答案

**练习 1**：为什么 bf16 master + fp32 累积不能释放紧凑分区？
**答案**：host 梯度是 bf16，无法逐位保存 fp32 的运行和（`_host_grad_holds_accum_exactly` 返回 False，[L1308-L1314](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L1308-L1314)）；释放后恢复会把 fp32 和舍入到 bf16 再舍入回来，窗口和会漂移。分区只能驻留到 `step()`（该组合整体被 PR 标记为 unsupported）。

**练习 2**：`_publish_unpublished_offload_grads` 防的是什么场景的 bug？
**答案**：参数最后一次梯度贡献来自非边界 backward（和只更新到了 host 累计器，`_offload_unpublished_param_ids` 记录了它），而随后的边界 backward 没有再触碰它。没有兜底的话，这个参数会以零梯度到达 `step()` 与范数计算（[L1136-L1149](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L1136-L1149) 的 docstring）。

**练习 3**：为什么释放门要排除 ZenFlow（`_offload_gradient_safety_enabled` 为 False 时 `continue`）？
**答案**：ZenFlow 有自己的卸载拷贝编排（stall-free 调度），紧凑分区的搬运顺序由它接管；注释写明"Otherwise the device buffer stays resident until step(), and ZenFlow orders its own copies"（[L1392-L1397](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L1392-L1397)，该标志在 [L234](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L234) 定义为 `cpu_offload and not zenflow`）。

### 4.10 空闲梯度清零与 host dtype 溢出检查（#8991 / #9044）

#### 4.10.1 概念说明

**空闲梯度泄漏（#8991）**。CPU 卸载下 Adam 消费的是 host 梯度分区（`fp32_partition.grad`）。旧版 `reset_cpu_buffers` 只清范数表和溢出标志、**不清零 FP32 分区梯度**。于是：一个可训练参数在窗口 W 有梯度，在窗口 W+1 整个窗口都没收到梯度（例如 LoRA 场景只训部分参数、或数据依赖使某些 head 空转）——它在上一个窗口的梯度切片原样留在 host 梯度分区里，Adam 在 W+1 又把它应用了一遍。对照 `torch.optim.Adam`（没梯度就不更新）即可看出这是正确性 bug。修复：`reset_cpu_buffers` 在每次 step 后、溢出后与 reset 时，把每个 FP32 分区梯度整个 `zero_()`——空闲参数到达 Adam 的梯度恒为零，"无梯度 = 无更新"。

**host dtype 溢出检查（#9044）**。fp16 master + fp32 累积的组合里，窗口和是 fp32 的：单次 micro-step 梯度有限（如 40000），两次累加成 80000——fp32 里有限，但 cast 到 fp16 host 梯度（上限 65504）就变成 inf。旧检查直接查 fp32 源张量，查不到这个"将要发生"的溢出；inf 静默进入 host 梯度，污染 Adam 状态。修复：发布时先 `source.to(host_grad.dtype)` 再做 `_has_inf_or_nan` 检查（4.9 节 L1385-L1388）——检查"优化器实际将读到的值"，fp32 和超范围会被正确捕获并跳步。

#### 4.10.2 核心流程

```
正常 step 尾部 / 溢出跳步 / 手动 reset:
  reset_cpu_buffers():
    _wait_for_offload_copies()          ← 先等在途主机拷贝
    norm_for_param_grads = {}
    local_overflow = False
    _host_owned_compact_partitions.clear()
    for fp32_partition in single_partition_of_fp32_groups:
        fp32_partition.grad.zero_()     ← 本周期新增：清零 host 梯度分区

发布紧凑分区时（_offload_compact_grad_partitions 内）:
  host_value = source.to(host_grad.dtype)   ← cast 到 host 精度
  update_offload_overflow_tracker(host_value) ← 检查 cast 后的值（inf 即置 local_overflow）
  → has_overflow() 会把 local_overflow 经 DP/MP 组 MAX-allreduce 汇聚成跳步决定
```

#### 4.10.3 源码精读

`reset_cpu_buffers` 的清零（注释即修复理由）：

[deepspeed/runtime/zero/stage_1_and_2.py:L3008-L3016](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L3008-L3016)

"Adam consumes the partition gradient; a parameter idle in the next window must not reuse it." 调用点有三处：正常 step 尾部（[L3257-L3258](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L3257-L3258)）、溢出跳步（[L3161-L3162](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L3161-L3162)）以及优化器重置路径。

host dtype 溢出检查的落点（在 4.9 节的发布循环内）：

[deepspeed/runtime/zero/stage_1_and_2.py:L1384-L1389](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L1384-L1389)

注释写明动机："Check the value the optimizer will read: an fp32 sum can pass here and become inf in an fp16 host gradient."

空闲参数回归测试（与 torch.optim.Adam 逐位对照）：

[tests/unit/v1/zero/test_zero2_gradient_safety.py:L499-L517](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/tests/unit/v1/zero/test_zero2_gradient_safety.py#L499-L517)

`test_idle_parameter_window_does_not_reuse_previous_gradient`：窗口 1 给参数梯度 2 并 step；窗口 2 完全不给梯度再 step——断言 master 与"参考 Adam 以零梯度 step"的结果逐位相等。提交信息记录：去掉 `reset_cpu_buffers` 的清零后该测试（及邻居测试）全部失败。

空闲参数与活跃邻居共享一块分区的版本：

[tests/unit/v1/zero/test_zero2_gradient_safety.py:L521-L564](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/tests/unit/v1/zero/test_zero2_gradient_safety.py#L521-L564)

`test_idle_parameter_stays_zero_beside_active_neighbor`：两个参数写同一组缓冲的相邻切片，第二个窗口只有第一个参数有梯度——断言空闲邻居的切片以**零**到达 Adam，而不是它上一窗口的和。

fp16 host 梯度溢出跳步测试：

[tests/unit/v1/zero/test_zero2_offload_multi_backward.py:L313-L352](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/tests/unit/v1/zero/test_zero2_offload_multi_backward.py#L313-L352)

`test_fp16_host_gradient_overflow_skips_step`：两次 backward 各贡献 40000/元素（fp16 内有限），窗口和 80000 超 fp16 上限；参数化覆盖默认累积与 fp32 累积。断言 step 后参数与初始值完全一致（"overflowed sum must skip the update"）。提交信息记录：恢复旧检查（查 fp32 源）后 fp32 累积分支失败。

#### 4.10.4 代码实践

1. **实践目标**：用测试复现两个 bug 的"修复前"行为，理解清零与 cast 检查为何是正确性修复。
2. **操作步骤**：
   - 运行 `pytest tests/unit/v1/zero/test_zero2_gradient_safety.py::test_idle_parameter_window_does_not_reuse_previous_gradient tests/unit/v1/zero/test_zero2_gradient_safety.py::test_idle_parameter_stays_zero_beside_active_neighbor -v`（fp32/bf16 master 共四个分支，CPU 可跑）。
   - 本地实验（勿提交）：注释掉 [L3014-L3016](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L3014-L3016) 的清零，重跑上述测试观察失败信息；恢复后还原。
   - 有 fp16 环境时运行 `pytest "tests/unit/v1/zero/test_zero2_offload_multi_backward.py::TestZero2OffloadOverflowRecovery::test_fp16_host_gradient_overflow_skips_step" -v`；再本地实验把 [L1387-L1388](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L1387-L1388) 的 `host_value` 换回 `source`，观察 fp32 累积分支是否漏掉溢出。
3. **需要观察的现象**：清零被移除后空闲参数用例失败（master 与零梯度参考不一致）；cast 检查被移除后 fp32 累积 + fp16 master 的溢出和被静默应用（参数改变）。
4. **预期结果**：两处修复各守护一个不可退化的契约。**运行部分待本地验证**。

#### 4.10.5 小练习与答案

**练习 1**：为什么空闲参数 bug 只在 CPU 卸载路径出现？
**答案**：非卸载路径每次 step 用 `averaged_gradients` 重新 flatten 出 `single_grad_partition`（L3215-L3234），窗口内没梯度的参数在 flatten 时自然取到零；CPU 卸载路径 Adam 直接消费持久存在的 `fp32_partition.grad`，上窗口内容会跨窗口存活——所以必须显式清零（[L3008-L3016](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L3008-L3016)）。

**练习 2**：溢出检查为什么要"先 cast 再检查"，而不是检查源、检查目标两次？
**答案**：只需检查优化器真正读到的值。host 梯度就是 Adam 的输入，cast 之后的值若有 inf/nan，无论源值多"干净"这一步都不可信；只查源（旧代码）会漏掉"fp32 有限、fp16 溢出"的窗口和，只查目标不做源检查则丢掉发布前的早期发现。一次 cast + 一次检查同时覆盖两者（[L1385-L1388](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L1385-L1388)）。

**练习 3**：`local_overflow` 置位后如何变成全组的跳步决定？
**答案**：`has_overflow()` 把 `local_overflow` 经 DP 组与模型并行组的 MAX-allreduce 汇聚（[L3345](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/deepspeed/runtime/zero/stage_1_and_2.py#L3345) 起的 `has_overflow` 实现），任何一个 rank 的 host 梯度出现 inf/nan 都会让全组跳步——这保证各 rank 对"本步是否更新"达成一致（u4-l1 讲过跳步必须全组同步的原因）。

## 5. 综合实践

把本讲的知识串成一条完整的"梯度生命周期追踪"，并以本周期的两项修复做对照实验：

1. **准备**：两卡（或单卡模拟读码）环境，SimpleModel 或小型 Transformer，ZeRO-2 配置：`"zero_optimization": {"stage": 2, "contiguous_gradients": true, "reduce_bucket_size": 5e5, "overlap_comm": false, "reduce_scatter": true}`，开 `"gradient_clipping": 1.0`。
2. **追踪链路**（读码 + 断点/日志）：在 `grad_handling_hook` → `process_gradients` → `reduce_independent_p_g_buckets_and_remove_grads` → `reduce_ipg_grads` → `average_tensor` → `copy_grads_in_partition` 这条链上各加一行 `logger.info`（本地实验，勿提交），打印参数名与 `bucket.elements`，观察桶的填充与冲刷节奏。
3. **bucket 扫描**：按 4.1.4 的方法扫 3 个 `reduce_bucket_size`，记录每步耗时，解释你观察到的最优点。
4. **范数验证**：对无 MoE 的稠密模型，手工计算 \(\|g\|_{global}\)（gather 全量梯度后 `torch.linalg.vector_norm`），与 `engine.optimizer._global_grad_norm` 对比，验证"局部平方和 + 组内 SUM + 开方"的实现。
5. **裁剪验证**：把 `gradient_clipping` 设为一个小于当前范数的值，验证参数更新量满足 \( \Delta w = -\eta \cdot g \cdot \frac{\tau}{\|g\| + 10^{-6}} \)。
6. **紧凑分区驻留实验**（4.9.4）：CPU 卸载 + bf16 模型 + `grad_accum_dtype=fp32`，多次边界 backward 共享一个窗口，对照 `retain_gpu_grad_partition_until_step` 两种取值的边界后显存、backward 耗时与最终权重；并人为构造 fp32 溢出（梯度乘 40000 两次）验证 host dtype 溢出检查会跳步（对照 [test_fp16_host_gradient_overflow_skips_step](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/tests/unit/v1/zero/test_zero2_offload_multi_backward.py#L313-L352) 的构造方式）。
7. **空闲参数实验**（4.10.4）：参照 [test_idle_parameter_window_does_not_reuse_previous_gradient](https://github.com/deepspeedai/DeepSpeed/blob/b9100c541ac1b8e25148bc8000d1ad45c4c095db/tests/unit/v1/zero/test_zero2_gradient_safety.py#L499-L517)，写一个"窗口 1 有梯度、窗口 2 无梯度"的两步训练，与 `torch.optim.Adam` 的零梯度更新对照，解释 `reset_cpu_buffers` 清零为何是正确性修复。
8. **（进阶，需加速器）折叠范数**：运行 `TestMixedExpertClippedUpdate` 与 `TestShardedAttentionClippedUpdate`，对照 4.7 节解释每个断言。

产出：一份含梯度链路日志摘录、bucket 耗时对照表、范数手工验证结果、紧凑分区驻留对照表、空闲参数对照结果的实验记录。

## 6. 本讲小结

- ZeRO-1/2 的核心是 **IPG 流水线**：梯度就绪即入桶，桶满即通信（`reduce_ipg_grads`），`overlap_comm` 用双缓冲（`bucket.index` 0/1 切换）让通信与反向重叠；桶大小在"通信次数"与"凑桶延迟"间权衡。
- `average_tensor` 有两种模式：`reduce_scatter=true`（默认，一次通信同时完成平均与分区）与 all-reduce + 预除模式；MoE 参数按其 expert-DP 组单独规划切片。
- ZeRO-2 的梯度分区可用 `GradPartitionBufferGroup` 紧凑表示：一个 `partition_size` 大小的 fp32 扁平缓冲 + 每参数视图列表，`_zero_padding` 标记让范数计算跳过对齐填充；Muon 组被排除在外（判定已提前到 dtype 判定之前）。
- `step()` 的固定时序：溢出检查 → 全局范数 → flatten/unscale/clip → 优化器更新 fp32 分区 → 降精度写回 bit16 分区 → all-gather 全量参数；溢出步清空梯度、清理卸载簿记与**驻留的紧凑分区**后直接返回。
- Adagrad 因**构造期初始化状态**而需要用 `optimizer.param_groups` 重建；从主机恢复梯度前必须 `dest_buffer.zero_()` 清掉共享 scratch 的残影——本周期起该清零无条件化（fp32 master 也只恢复分区切片）。
- 折叠专家修复（#8823）：`get_grad_norm_direct` 参数化归约组，折叠专家组（`ds_zero_placement_family` 全为 `autoep_expert`）的范数在 expert-DP 分区组与 EP owner 组上级联求和、跳过 TP rank 0 过滤、豁免专家范数平均；稠密组与原生 MoE 组保持原路径并可共存。
- 本周期 CPU 卸载两连修（#9018/#9044）：主机累计器改为**按分区组**的 bit16 pinned 缓冲（`_get_cpu_offload_accumulator_slice` 唯一入口，GAS=1 零分配，destroy 统一 unpin）；紧凑分区在每个**请求边界**把运行和发布到 host 梯度，仅当 host dtype 可逐位恢复（`_host_grad_holds_accum_exactly`）且未开 `retain_gpu_grad_partition_until_step` 时释放 GPU 分区、下次 backward 从 host 恢复；`_publish_unpublished_offload_grads` 兜底非边界 backward 只留在 host 的梯度。
- 空闲梯度清零（#8991）与 host dtype 溢出检查（#9044）：`reset_cpu_buffers` 每步后清零 FP32 分区梯度，空闲参数不再复用上一窗口的和；溢出检查改对 cast 到 host dtype 后的值做，fp32 窗口和超 fp16 范围会被跳步而非静默污染 Adam。

## 7. 下一步学习建议

- **u5-l4（ZeRO-Offload）**：本讲的 `cpu_offload` 分支（host 梯度分区、`DeepSpeedCPUAdam`、pinned memory 管理）将在那里展开，包括 `DeepSpeedCPUOptimizer` 基类对 fp32 状态精度的保护——与本讲的按分区组累计器直接衔接。
- **u5-l5（ZeRO-3 参数卸载）**：Stage 3 把参数也切分后，本讲的 all-gather 收尾演变为按需 all-gather + 参数协调器；销毁链（`destroy()` 解 pin、`_zero_optimizer` 反向引用清理）与本讲 4.8 节的 unpin 同属一条生命周期主线（#9034）。
- **u6-l6（AutoEP）**：本讲 4.7 节的折叠只是裁剪侧的适配；folding 的完整机制（注入、与 ZeRO 卸载的兼容矩阵、空专家梯度清零）在 AutoEP 一讲展开，配套契约文档 `docs/code-docs/source/autoep.rst` 值得通读。
- 源码阅读顺序建议：重读 `stage_1_and_2.py` 的 `__init__`（L170-L760）补全分区元数据（`grad_start_offset`、`param_to_partition_ids` 等）与四个卸载簿记集合的构建，再按 4.9 节的流程图把 `_offload_compact_grad_partitions` → `_restore_compact_grad_partition` → `reset_cpu_buffers` 三个函数连起来读一遍，最后对照 ZeRO 论文第 3 节验证显存公式。
