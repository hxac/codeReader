# ZeRO-3 优化器：stage3 主循环与连续内存

> 对应源码版本：HEAD `84481c33`（本讲在 u5-l5 之后，聚焦 `DeepSpeedZeroOptimizer_Stage3` 这个"训练期大脑"）。

## 1. 本讲目标

学完本讲，你应该能够：

1. 按顺序列出 `DeepSpeedZeroOptimizer_Stage3.step()` 的完整阶段：预清理 → 溢出检查 → 全局范数 → 逐子组「缩放梯度 → 更新 fp32 分区 → 写回 ds_tensor」→ 收尾，并说清 reduce-scatter 参数梯度与更新 `ds_tensor` 的先后关系。
2. 解释 IPG（独立并行梯度）桶如何把反向梯度归约与计算重叠，`reduce_bucket_size`、`contiguous_gradients` 与 `reduce_scatter` 三个配置如何影响通信路径与显存。
3. 说明 partial offload（`offload_optimizer.ratio`）时子组设备串 `subgroup_to_device` 如何通过公共加速器 API `get_accelerator().device_name()` 构造，而不用任何私有属性。
4. 解读 `aliased_fp32_sub_groups`（权重分区即 master 副本）：fp32 模型（含 autocast）下 ZeRO-3 为什么可以少存一份权重，以及这给 `offload_states`/`reload_states` 带来的「必须随 lp_params 一起搬」约束。
5. 理解 `ContiguousMemoryAllocator` 如何用一块预分配平坦缓冲 + 地址簿 + 搬移式碎片整理来对抗显存碎片。
6. 精读本周期 #8841 的融合 CPU Adam 步：`_grad_scale_for_cpu_adam` 判定、`_deferred_grad_scale` 延迟补乘、以及双 pinned bf16 缓冲流水拷回 `_cpu_adam_step_and_copy_back`，并说明为什么它们与旧路径**逐位一致**。

## 2. 前置知识

本讲默认你已读过：

- **u5-l1**：ZeRO 三阶段的显存公式（混合精度 + Adam 下每卡模型状态约 16Ψ 字节；stage 3 把参数、梯度、优化器状态全部按数据并行度切分）。
- **u5-l2**：`zero.Init` 如何在模型构造期就把参数切成 `ds_tensor` 分区；参数本体只是 0 元素占位符。
- **u5-l5**：前向/反向 hook 如何按需 all-gather 参数（`ds_status` 三态轮转），参数协调器如何预取与释放。本讲的 `step()` 就发生在一次完整 backward 结束、所有梯度已落入本 rank 梯度分区之后。
- **u4-l1**：fp16 动态 loss scaling 的溢出检测与 scale 调整规则。stage3 与 stage1/2 共享这套机制，本讲的融合路径必须证明自己在溢出跳步时与旧路径一致。
- **u5-l4**：`DeepSpeedCPUAdam` 的「薄 Python 壳 + C++ SIMD 内核」结构与 pinned memory 的作用。本讲的 #8841 正是给这个内核加料。

补充几个本讲反复出现的术语：

| 术语 | 含义 |
| --- | --- |
| 子组（sub-group） | stage3 把本 rank 的全部参数分区按 `sub_group_size`（默认极大）分批，每批摊平成**一块**平坦缓冲、作为一个整体做优化器更新 |
| IPG 桶 | 反向传播中暂存"已就绪梯度"的连续缓冲，桶满立即归约，实现通信与计算重叠 |
| master 权重（hp） | fp32 主权重分区 `fp32_partitioned_groups_flat`；对应的低精度权重分区是 `fp16_partitioned_groups_flat`（历史命名，bf16 也走这里） |
| pinned memory | 页锁定主机内存，`non_blocking=True` 的 H2D/D2H 拷贝只有从 pinned 内存发出才是真异步 |
| 请求/累积窗口 | 两次 `step()` 之间的全部 micro-batch backward；本讲的"延迟梯度缩放"以窗口为单位记账 |

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [deepspeed/runtime/zero/stage3.py](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py) | 本讲主战场：`DeepSpeedZeroOptimizer_Stage3` 的 step 主循环、IPG 桶、子组设备分配、`offload_states`、融合 CPU Adam 步 |
| [deepspeed/runtime/zero/contiguous_memory_allocator.py](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/contiguous_memory_allocator.py) | 平坦缓冲碎片整理分配器（all-gather 的连续显存来源） |
| [deepspeed/runtime/zero/partitioned_param_profiler.py](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partitioned_param_profiler.py) | 参数 all-gather 事件的计时/计数器，实践任务用它观测通信 |
| [deepspeed/ops/adam/cpu_adam.py](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/ops/adam/cpu_adam.py) | `DeepSpeedCPUAdam`，#8841 给 `step()` 加了 `grad_scale` 与 `bf16_copy` 两个参数 |
| [csrc/adam/cpu_adam.cpp](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/csrc/adam/cpu_adam.cpp) / [csrc/adam/cpu_adam_impl.cpp](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/csrc/adam/cpu_adam_impl.cpp) | CPU Adam 内核入口与实现：`adam_update_scaled(_copy)`、能力探测、向量块对齐 |
| [tests/unit/v1/zero/test_zero.py](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/zero/test_zero.py) | 本周期新增的四个融合路径回归测试（GradScale/CopyBack/UnusedLayer/Fp16Overflow） |
| [tests/unit/v1/zero/test_offload_states.py](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/zero/test_offload_states.py) | `TestOffloadStatesZero3AliasedMaster`：aliased master 的搬运回归 |

## 4. 核心概念与源码讲解

### 4.1 stage3 step 主循环：从梯度分区到 ds_tensor 更新

#### 4.1.1 概念说明

`DeepSpeedZeroOptimizer_Stage3` 是 ZeRO-3 的"训练期大脑"。u5-l5 讲的参数协调器负责**前向/反向**把参数分区按需拼成完整参数；本讲的对象则在**一次累积窗口结束**时接管：把本 rank 持有的梯度分区变成参数更新。它管理的三块核心状态：

- `fp16_groups` / `fp16_partitioned_groups_flat`：本 rank 的低精度**权重**分区（摊平后每子组一块）；
- `fp32_partitioned_groups_flat`：fp32 **master 权重**分区（优化器真正更新的对象）；
- `grad_partitions_flat_buffers`：按累积 dtype 分桶的平坦**梯度**分区（一或多个 dtype 一块）。

step 要做的事本质上就是：`master += -lr * adam(master.grad)`，然后把 master 降精度写回权重分区，供下一个 forward 的 all-gather 使用。

#### 4.1.2 核心流程

`step()`（[stage3.py:3050-3138](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L3050-L3138)）的阶段顺序：

```text
1. _pre_step()
   ├─ micro_step_id 归零
   ├─ _apply_all_deferred_grad_scales()   ← 融合路径的补乘，必须在溢出检查之前
   ├─ _offload_grad_written_ids = set()   ← 记录"本窗口写过哪些梯度"的集合清空
   └─ 清空 IPG 桶残留参数（reentrant checkpointing 防御）
2. _partition_all_parameters()            ← 把 step 间隙残留的完整参数重新切回分区
3. 溢出检查（update_scale=False，原始溢出在 Muon 之前判）
   → 无溢出则 _apply_muon_to_accumulated_grads + 各组范数合并成 scaled_global_grad_norm
4. _loss_scale_update_and_overflow_cleanup()
   → 溢出则清梯度、降 scale、直接 return（跳过整个更新）
5. 记录无缩放范数 self._global_grad_norm = norm / loss_scale
6. 逐子组循环（核心，见 6.）：
   for sub_group_id in fp16_groups:
       a. _prepare_sub_group        （NVMe 子组换入 / 非卸载路径准备 fp32 梯度）
       b. grad_scale = _grad_scale_for_cpu_adam(...)
          - None      → unscale_and_clip_grads（就地缩放旧路径）
          - != 1.0    → 记入 _deferred_grad_scale（融合路径不就地缩放）
       c. 更新：
          - 融合+拷回成立 → _cpu_adam_step_and_copy_back（一遍内核完成更新+bf16 写回）
          - 否则          → _optimizer_step（可带 grad_scale）
                            + _reassign_or_swap_out_partitioned_parameters
                            （把 fp32 master 降精度拷回权重分区 → ds_tensor 分区）
       d. _release_sub_group        （释放 fp32 梯度 / NVMe 子组换出）
7. _post_step()（offload 时 reset_cpu_buffers；收集持久参数；失效 hpZ 副本）
8. 检查 caching allocator 重试次数并告警
```

注意顺序要点：**梯度归约（reduce-scatter）不在 step 里发生**——它发生在 backward 的梯度 hook 中（见 4.2）；step 拿到的是已经 reduce-scatter 落位、且已跨 micro-batch 累积完的梯度分区。step 内部对每个子组先缩放、再更新 master、最后写回 `ds_tensor`。

#### 4.1.3 源码精读

step 主体与逐子组循环（引用处为当前 HEAD 真实行号）：

- [stage3.py:3088-3116](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L3088-L3116)：逐子组更新循环。`_prepare_sub_group` 之后，第 3096 行先问 `_grad_scale_for_cpu_adam`；为 `None` 走就地缩放 `unscale_and_clip_grads`，否则把因子交给内核或记入 `_deferred_grad_scale`；第 3102 行若同时满足拷回条件则走一遍式 `_cpu_adam_step_and_copy_back`，否则传统的 `_optimizer_step` + `_reassign_or_swap_out_partitioned_parameters`。
- [stage3.py:3027-3042](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L3027-L3042)：`_reassign_or_swap_out_partitioned_parameters` —— 把更新后的 fp32 master 拷回低精度权重分区并 `_unflatten_partitioned_parameters` 重新铺到各参数的 `ds_tensor`；pinned master 走 `_copy_pinned_master_to_device`（master 以自身 dtype 上设备、在设备上做 cast，这是 #5301 引入的省 CPU 端 pageable 临时拷贝的形式）。
- [stage3.py:2893-2898](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L2893-L2898)：`_unflatten_partitioned_parameters` —— 按子组把平坦缓冲切片赋回 `partitioned_param.data`，即"更新最终落到 ds_tensor 分区"的那一步。
- [stage3.py:2636-2661](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L2636-L2661)：`_pre_step`。注释明确说明 `_apply_all_deferred_grad_scales` 要放在溢出检查之前，这样被跳过的 step 也让梯度保持在"与就地缩放完全相同"的状态。
- [stage3.py:3082-3085](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L3082-L3085)：非卸载路径下梯度已在 epilogue 拷进分区缓冲，这里**一次性** `zero_grad(set_to_none=True)` 清模型梯度，而不是每个子组访问一遍全部参数（性能细节）。
- 溢出检查按 dtype 遍历所有梯度缓冲：[stage3.py:3243-3253](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L3243-L3253)（`for grad_buffer in self.grad_partitions_flat_buffers.values()`，fp32 保真参数有自己的缓冲，见 4.2.3）。这与 stage1/2 共享的 loss scaling 语义一致（u4-l1）。

#### 4.1.4 代码实践

1. **实践目标**：把 step() 的阶段清单落到真实行号上，形成一张可复查的地图。
2. **操作步骤**：
   - 打开 [stage3.py:3050](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L3050) 起，按 4.1.2 的 8 个阶段逐段找到对应方法与行号，抄成表格。
   - 用源码阅读型实践验证顺序断言：在 `_prepare_sub_group`、`unscale_and_clip_grads`、`_optimizer_step`、`_reassign_or_swap_out_partitioned_parameters` 四处各加一行 `print`（仅本地实验，勿提交），跑一个 2 层小模型的 ZeRO-3 训练，观察打印顺序。
3. **需要观察的现象**：每个子组固定按 `prepare → scale → step → reassign → release` 打印；卸载路径下 `unscale_and_clip_grads` 可能完全不再打印（被融合路径替代，见 4.6）。
4. **预期结果**：阶段顺序与本节流程图一致；若你在 DeepSpeedCPUAdam 卸载 + bf16 + pin_memory 的配置下运行，`_optimizer_step` 与 `_reassign...` 的打印会消失，取而代之的是 `_cpu_adam_step_and_copy_back`。
5. 本地无 GPU 时可参照 tests/unit/v1 的 CPU 多进程方式运行（见 4.6.4），实际输出**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 step 开头要先 `_apply_all_deferred_grad_scales()` 再做溢出检查，反过来行不行？

**答案**：不行。延迟补乘的目标是"让梯度在 step 结束后看起来和就地缩放路径完全一样"。就地路径里，无论后续是否溢出跳步，梯度都已被乘过因子；若把补乘放到溢出检查之后，被跳过的 step 会留下未缩放的梯度，而下一次 step 或任何 `safe_get_full_grad` 读取都会再次用到它，两条路径的梯度值就会分叉。源码注释（[stage3.py:2639-2640](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L2639-L2640)）明确写了这一点。

**练习 2**：`self._global_grad_norm = scaled_global_grad_norm / self.loss_scale`（[stage3.py:3075](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L3075)）为什么除以 loss_scale？

**答案**：fp16 训练中梯度全程带着 loss scale（u4-l1），范数是对"已放大"的梯度算的；除回得到真实范数，供日志与 `clip_grad` 语义使用。`_combined_grad_scale`（4.6.3）内部也做了同样的 `(total_norm / self.loss_scale + 1e-6) / self.clip_grad` 换算。

### 4.2 IPG 梯度桶：reduce_bucket、按 dtype 分桶与两条归约路径

#### 4.2.1 概念说明

ZeRO-3 反向时每个参数的梯度 hook 触发后，梯度并不立即通信，而是先进入 IPG（Independent Parallel Gradients）桶；桶满或跨进程组时立即 flush：对参数分区组做 **reduce-scatter**（默认）或 **all-reduce**（`reduce_scatter: false` 时先 all-reduce 再各取所需），每个 rank 拿到属于自己的那份梯度分区。这样把通信切成分桶批次，与剩余的反向计算重叠。`reduce_bucket_size`（默认 5e8 元素）决定桶容量——桶越大单次通信越大、launch 开销越低，但驻留显存越多；`contiguous_gradients`（默认 true）决定梯度是否先拷进桶的连续切片再归约。

#### 4.2.2 核心流程

```text
backward 中某参数梯度就绪
  → reduce_independent_p_g_buckets_and_remove_grads(param)
      ├─ 进程组变了 → 先 flush 旧桶
      ├─ 桶容量不够且桶非空 → flush
      └─ __add_grad_to_ipg_bucket(param)
           contiguous_gradients 时把 param.grad 拷进 bucket.buffer 的尾部切片
flush（__reduce_and_partition_ipg_grads，在专用 reduce_and_partition_stream 上）
  ├─ contiguous_gradients 且 未超容量 且 reduce_scatter=False
  │     → __avg_scatter_contiguous_grads(整块桶缓冲)     ← all-reduce 路径
  ├─ 否则 → __avg_scatter_grads(逐参数)                 ← reduce-scatter 路径
  └─ partition_grads(...)：结果累进 grad_partitions_flat_buffers 中该参数的切片
       卸载时在累积边界把梯度拷去 CPU 的 fp32 梯度缓冲
```

桶本身是按**通信 dtype** 分开的一组（`ipg_buckets: Dict[torch.dtype, IPGBucketZ3]`）：fp32 保真参数（`data_types.keep_in_fp32_modules` 点名的，见 u5-l9）用 fp32 桶，其余用训练 dtype 的桶——把保真参数的梯度舍到 bf16 再归约会让"保真"失去意义。

#### 4.2.3 源码精读

- [stage3.py:122-141](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L122-L141)：`IPGBucketZ3` 数据类——buffer/buffer_meta/params/elements/process_group 五个字段，`clear_params` 保留 buffer 只清参数（buffer 复用，避免反复分配）。
- [stage3.py:804-814](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L804-L814)：桶缓冲分配。纯 fp32 保真参数的桶可按参数总量缩小容量；混合桶保持 `reduce_bucket_size`，注释解释了原因：backward 中途抛异常会把梯度留在桶里直到 step()，重试的 backward 会把同一参数再次入桶，按参数总量定容会让参数被归约两次。
- [stage3.py:819-835](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L819-L835)：**每个累积 dtype 一块平坦梯度缓冲**（`grad_partitions_flat_buffers`），fp32 保真参数在 fp32 缓冲中累积；每块缓冲可 pin（卸载时）。
- [stage3.py:1644-1654](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L1644-L1654)：`get_param_comm_dtype` 与 `_grad_accumulation_dtype`——保真参数强制 fp32 通信与 fp32 累积。
- [stage3.py:1662-1681](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L1662-L1681)：入桶与 flush 触发条件（跨组、超容量）。
- [stage3.py:1764-1791](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L1764-L1791)：flush 的两条归约路径（第 1768-1774 行的 if/else），随后 `partition_grads` 落位；`param_reduce_events` 队列限制在途归约事件数以形成反压。
- [stage3.py:2106-2154](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L2106-L2154)：`partition_grads` —— micro_step 0 用 `copy_`（不累积），否则 `add_`；目标是 CPU 缓冲时先搬到源设备做加法再拷回（CPU 上直接 add 很慢）；累积边界处调 `_offload_grad_partition_at_boundary`（见 4.6.4 的 `_offload_grad_written_ids`）。
- allgather 侧的 bucket（参数取回用）在 u5-l5 已讲（`prefetch_bucket_size` 等），本讲不重复。

#### 4.2.4 代码实践

1. **实践目标**：感受 `reduce_bucket_size` 对 step 时间与通信批次的影响。
2. **操作步骤**：
   - 写一个 4 层 `SimpleModel`（`tests/unit/simple_model.py`）的 ZeRO-3 双进程训练脚本，配置 `zero_optimization.stage=3`；用 `deepspeed.initialize` 返回的 engine 外层计时 `engine.backward + engine.step`。
   - 分别设 `reduce_bucket_size` 为 `1e4`（小桶、频繁 flush）、`5e8`（默认）、`1e10`（近似一桶到底），各跑 10 步取平均；如有条件打开 `"wall_clock_breakdown": true` 让 DeepSpeed timers 打印 `optimizer` 等分项。
   - 无分布式环境时的替代：源码阅读——数一数 [stage3.py:1674-1676](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L1674-L1676) 的 flush 条件在 `ds_numel > bucket容量` 时对每步反向触发多少次，推导桶大小与 flush 次数的关系。
3. **需要观察的现象**：桶过小时每步时间上升（通信 launch 次数多）；桶过大时峰值显存上升（桶缓冲 + 在途分区）。参数 all-gather 计数可用 [partitioned_param_profiler.py:54-56](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/partitioned_param_profiler.py#L54-L56) 的 `log_events`（`count`/`numel`）观测。
4. **预期结果**：一条"桶增大→时间先降后平、显存单调升"的曲线；最优点因模型与互联带宽而异。
5. 具体数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`reduce_scatter=True`（默认）与 `False` 的通信量差多少？

**答案**：设数据并行度为 \(N\)、桶内元素 \(B\)。reduce-scatter 路径每 rank 收发约 \(2B\cdot\frac{N-1}{N}\) 个元素（环状 reduce-scatter），最终每 rank 只持有 \(B/N\) 的本区梯度；all-reduce 路径等价于 reduce-scatter + all-gather，收发翻倍但每 rank 拿到完整 \(B\)（随后本地切片，见 `__avg_scatter_contiguous_grads`）。所以 `reduce_scatter=False` 通信量约为两倍，但实现上直接对整块桶缓冲操作，某些小桶/特定后端场景更简单。

**练习 2**：为什么混合 dtype 时桶必须按 dtype 分开，而 stage1/2（u5-l3）历史上只有一个 IPG 桶？

**答案**：一块桶缓冲只有一种 dtype。stage3 在 #8843 引入"点名参数留在 fp32"后，同一模型里同时存在 fp32 与 bf16 分区，梯度若共用一个 bf16 桶会在拷入桶时被舍入。按 `get_param_comm_dtype` 分桶后，fp32 梯度全程 fp32 归约与累积（[stage3.py:3243-3245](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L3243-L3245) 的溢出检查也因此要遍历所有 dtype 的缓冲）。

### 4.3 partial offload：subgroup_to_device 与公共 accelerator API

#### 4.3.1 概念说明

ZeRO-3 允许只把一部分子组卸载到 CPU：`offload_optimizer.ratio`（引擎侧叫 `offload_ratio`，默认 0.0=全 GPU）。每个子组据此决定自己的"驻地"：驻地是 CPU 的子组，其 fp32 master、梯度、Adam 状态都躺在主机内存，step 由 `DeepSpeedCPUAdam` 执行；驻地是加速器的子组由 `backup_optimizer`（GPU 上的备用优化器）执行。这叫 partial offload——用显存换主机带宽，在两者之间取折中。

关键工程点：设备字符串的构造。stage3 不写死 `"cuda"`，也不用任何后端私有属性（如 `_name`），而是调用公共加速器抽象 `get_accelerator().device_name()`（u3-l1 讲过的端口-适配器模式），因此在 HPU/XPU/NPU 等后端上无需修改。

#### 4.3.2 核心流程

```text
_create_fp32_partitions()（__init__ 期）
  if offload_optimizer:
      sub_group_size = 子组总数
      for i in range(sub_group_size):
          if i >= int((1 - partial_offload) * sub_group_size):
              subgroup_to_device[i] = 'cpu'                          ← 尾部一截去 CPU
          else:
              subgroup_to_device[i] = get_accelerator().device_name() ← 其余留在加速器
```

即：**前 `⌈ratio × 总数⌉` 个子组在加速器、尾部其余在 CPU**（ratio 是留在 GPU 的比例，`ratio=1.0` 全在 GPU、`0.0` 全在 CPU——对照 4.6.4 测试的参数化 `ratio 1.0/0.5` 理解）。

step 时 `_optimizer_step` 查表：CPU 子组把平坦 master 塞进 `self.optimizer`（DeepSpeedCPUAdam）的 param_groups 再 `step()`，GPU 子组走 `backup_optimizer`（[stage3.py:1311-1321](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L1311-L1321)）。

#### 4.3.3 源码精读

- [stage3.py:1144-1151](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L1144-L1151)：`subgroup_to_device` 的构造循环——`partial_offload` 分界 + `get_accelerator().device_name()` 公共 API。
- [stage3.py:1200-1208](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L1200-L1208)：卸载子组的 master 创建——低精度分区 `.to(设备, master_dtype)` 后 clone，pin_memory 开启且 master 在 CPU 时再 pin（注释：master 每步都要拷回设备，只有 pinned 才快，对应 `_copy_pinned_master_to_device`）。
- [stage3.py:1405-1412](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L1405-L1412)：卸载子组的梯度缓冲——`torch.zeros(..., device=self.device)` 后 pin 再 `.to(subgroup_to_device[i])`，即"在加速器上造、pin 到主机、落在子组驻地"。
- [stage3.py:1295-1321](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L1295-L1321)：`_optimizer_step` 的设备分派；注意 `grad_scale` 参数直传 `optimizer.step(grad_scale=...)`（第 1318 行），这是 #8841 的接口。
- [stage3.py:349](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L349)：`self.deepspeed_adam_offload = (self.offload_optimizer and type(init_optimizer) == DeepSpeedCPUAdam)`——融合路径的前提之一。

#### 4.3.4 代码实践

1. **实践目标**：直观看到 `ratio` 如何切分子组驻地。
2. **操作步骤**：写一个 ZeRO-3 + `offload_optimizer: {device: cpu, pin_memory: true, ratio: 0.5}` 的小配置，`sub_group_size` 设小（如 20000，参考 [tests/unit/v1/zero/test_zero.py:2132-2140](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/zero/test_zero.py#L2132-L2140)）让模型切出多个子组；初始化后打印 `engine.optimizer.subgroup_to_device`。
3. **需要观察的现象**：字典前一半值为加速器名（如 `cuda:0`），后一半为 `'cpu'`；`ratio=1.0` 时全部为加速器名、`ratio=0.0` 时全部为 `'cpu'`。
4. **预期结果**：与 4.3.2 的公式一致。打印键值时顺带对照 [stage3.py:1148](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L1148) 的 `int((1 - partial_offload) * sub_group_size)` 边界。
5. **待本地验证**（CPU 后端下加速器名是 `cpu`，此时全在主机、观察不到分界——需在 GPU 环境验证）。

#### 4.3.5 小练习与答案

**练习**：为什么 `_grad_scale_for_cpu_adam` 只对 `subgroup_to_device[sub_group_id] == 'cpu'` 的子组返回因子，GPU 子组仍走就地缩放？

**答案**：GPU 子组的梯度在加速器上，`grad.mul_(1/combined_scale)` 是设备端 kernel，与后续 `backup_optimizer.step()` 在同一设备上由流序保证先后，代价只是一遍设备读写，且 GPU 子组无法使用 CPU Adam 内核的融合加载缩放。融合收益只存在于"梯度在主机内存、被 CPU 内核逐段读入"的场景——省掉的是一次对**主机内存**梯度的完整读改写遍历（PCIe/CPU 内存带宽远比设备显存宝贵）。

### 4.4 「权重分区即 master 副本」：aliased_fp32_sub_groups 与 offload_states

#### 4.4.1 概念说明

非卸载、非交换（NVMe）路径下，stage3 默认给每个子组建两份：低精度权重分区 + fp32 master 分区。但当**权重本身就是 master dtype**（fp32 模型，含开 `torch_autocast` 的场景）且已在加速器上时，这两份完全相同——再 clone 一份纯属浪费。此时 stage3 让 `fp32_partitioned_groups_flat[i]` **直接指向** `fp16_partitioned_groups_flat[i]`（同一块存储），并把 `i` 记入 `aliased_fp32_sub_groups`。更新就地发生在权重分区上，`_reassign_or_swap_out_partitioned_parameters` 里的 `dst.copy_(src)` 因为 src/dst 同源而不产生任何拷贝（[stage3.py:3030-3031](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L3030-L3031) 注释）。

代价出现在**手动搬运状态**时：`offload_states(include=[...])` 允许把 hp_params（master）、lp_params（权重）、lp_grads（梯度）、optim_states（Adam 状态）、contiguous_grad_buffer（IPG 桶）分批搬到 CPU。aliased 子组没有自己的存储，单独搬 hp_params 对它什么都不做——若不提醒，调用方会以为省了显存。所以：

- 搬 hp_params 时只处理 `_fp32_partitions_with_own_storage()`（非 aliased 的），且若 lp_params 留在设备则 `warning_once` 告警（[stage3.py:4132-4138](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L4132-L4138)）；
- 搬 lp_params 时整块 `lp_param_buffer`（所有 ds_tensor 的底座）一起走，搬完后 `_point_aliased_fp32_partitions_at_lp_buffer` 把每个 aliased master 的 `.data` 重新 narrow 到新缓冲的对应切片（[stage3.py:4093-4104](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L4093-L4104)）；`reload_states` 回程同理。

另外，#8843 之后多 dtype 分区（fp32 保真参数混在 bf16 模型里）会让 lp_params/lp_grads 的整块搬运失去"单一 dtype 平坦缓冲"前提，此时 `offload_states` 直接 `NotImplementedError` 拒绝（[stage3.py:4121-4126](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L4121-L4126)；详见 u5-l9）。

#### 4.4.2 核心流程

```text
__init__/_create_fp32_partitions：
  权重 dtype == master dtype 且在加速器上且无卸载
      → fp32_partitioned_groups_flat[i] = fp16_partitioned_groups_flat[i]（别名）
      → aliased_fp32_sub_groups.add(i)

offload_states(include=[hp_params])：
  只搬非 aliased 的 master；aliased 存在且 lp_params 不动 → warning_once
offload_states(include=[lp_params])：
  整块 lp_param_buffer → CPU（可 pin）
  所有 ds_tensor.data 重新 narrow 到新缓冲
  _point_aliased_fp32_partitions_at_lp_buffer()  ← aliased master 跟着权重走
reload_states：对称地搬回并再次重挂指针
```

#### 4.4.3 源码精读

- [stage3.py:1210-1217](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L1210-L1217)：别名判定分支（dtype 相同 + 设备相同 + 无卸载）。
- [stage3.py:4085-4091](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L4085-L4091)：`_fp32_partitions_with_own_storage` —— 过滤掉 aliased，docstring 点明"把它与 lp_params 分开搬会把两份副本拆散"。
- [stage3.py:4129-4152](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L4129-L4152)：hp_params 搬运（只搬有独立存储的；pinned 路径复用 `hp_params_pin_buffers`）。
- [stage3.py:4155-4172](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L4155-L4172)：lp_params 搬运 + `_point_aliased_fp32_partitions_at_lp_buffer`（第 4171 行）。
- [stage3.py:4223-4233](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L4223-L4233)：reload 的 lp_params 回程（`_set_fp16_partitioned_groups_flat` 重建权重分区视图后再次重挂 aliased master）。
- [stage3.py:4192-4198](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L4192-L4198)：IPG 桶缓冲的搬运用 meta tensor 记形状、丢数据（桶数据是临态，不需要保真）。

#### 4.4.4 代码实践（对应总实践任务第二项）

1. **实践目标**：验证 aliased 子组只随 lp_params 移动、单独搬 hp_params 不省显存。
2. **操作步骤**：参照 [tests/unit/v1/zero/test_offload_states.py:423-509](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/zero/test_offload_states.py#L423-L509) 的 `TestOffloadStatesZero3AliasedMaster`：
   - 用 fp32 `SimpleModel`（不开任何混合精度）建 ZeRO-3 engine（无 optimizer offload）；
   - 断言 `optimizer.fp32_partitioned_groups_flat[i] is optimizer.fp16_partitioned_groups_flat[i]` 且所有 `p.ds_tensor` 的存储指针等于 `lp_param_buffer` 的存储指针（`one_copy` 辅助函数，[test_offload_states.py:464-470](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/zero/test_offload_states.py#L464-L470)）；
   - 训一步后分别调用 `engine.offload_states(include=[OffloadStateTypeEnum.hp_params])` 与 `include=[lp_params]`，前后用 `get_accelerator().memory_allocated()` 记录设备显存差值，并观察第一次调用触发的 `warning_once` 日志。
3. **需要观察的现象**：hp_params 一轮显存几乎不降（master 无独立存储）、日志出现"the fp32 master copy of N sub-group(s) is the weight partition itself..."；lp_params 一轮释放的显存 ≥ 权重字节数，且回程后 `one_copy` 仍为真、权重值不变。
4. **预期结果**：与测试断言一致——多轮混合搬运（lp→hp→lp→hp+lp→reload）后训练结果与从未搬运的参考 engine 逐位相等（[test_offload_states.py:504-507](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/zero/test_offload_states.py#L504-L507)）。
5. **待本地验证**（需能跑双进程的分布式环境；CPU 后端下显存断言退化为内存口径）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `_point_aliased_fp32_partitions_at_lp_buffer` 里要同时更新 `fp16_partitioned_groups_flat[i]`？

**答案**：lp_params 搬运后第 4170 行 `self.fp16_partitioned_groups_flat.clear()` 清空了权重分区视图表；aliased 子组的"权重分区"和"master"本是同一个 tensor 对象，重挂时必须两边一起恢复，否则下次 `_reassign_or_swap_out_partitioned_parameters` 会对着空表走错分支。

**练习 2**：autocast 场景（fp32 权重 + `torch_autocast` bf16 计算）为什么也算"权重即 master"？

**答案**：autocast 只在前向把激活转成低精度，参数本体保持 fp32；优化器更新与存储的 dtype 判定看的是参数 dtype 本身。`_create_fp32_partitions` 的判定条件（dtype 相同 + 设备相同）自然覆盖该场景，测试也以 `"autocast"` 参数化覆盖（[test_offload_states.py:422-424](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/zero/test_offload_states.py#L422-L424)）。

### 4.5 contiguous 分配器：一块缓冲对抗碎片

#### 4.5.1 概念说明

ZeRO-3 每个 forward 都要 all-gather 出"当前层完整参数"，用完即弃；不同层的参数形状各异，"申请-释放-再申请"如果直接走 PyTorch caching allocator，会在显存里留下大量小空洞（碎片），最终一次大申请失败触发 `empty_cache` 级别的全量整理——代价极高。`ContiguousMemoryAllocator` 的解法：**一次性预分配一块平坦大缓冲，之后所有 all-gather 临时张量都从这块缓冲里切**，自己维护地址簿，碎片化到放不下时执行一次"整体向头部搬移"的碎片整理。

#### 4.5.2 核心流程

```text
allocate_tensor(size)：
  free 不足 → assert 失败
  largest_contiguous < size → _defragment_memory() + _reset_param_data()
  在最小的能容纳的空闲块里切出 tensor（best-fit）
release_tensor(tensor)：
  归还地址区间 → 与前后空闲块合并（_consolidate_address）
_defragment_memory()：
  把所有已分配块依次向最靠前的空闲区搬移（memmove 语义）
  → _replace_old_address_with_new 更新地址簿
_reset_param_data()：
  所有引用该缓冲切片的 param.data 重新指向搬移后的位置
```

#### 4.5.3 源码精读

- [contiguous_memory_allocator.py:16-46](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/contiguous_memory_allocator.py#L16-L46)：构造——一块 `torch.zeros(size)` 大缓冲 + `contiguous_sizes`（地址→连续空闲大小）、`tensor_addresses/tensor_sizes/tensor_ids/tensor_map` 四张地址簿 + `id_to_params`（缓冲块↔参数的反向索引，碎片整理后重挂用）。
- [contiguous_memory_allocator.py:51-78](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/contiguous_memory_allocator.py#L51-L78)：`allocate_tensor` —— 容量断言、按需碎片整理、best-fit 选址、记账一致性断言（`total_free + size == free_before`）。
- [contiguous_memory_allocator.py:97-118](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/contiguous_memory_allocator.py#L97-L118)：`release_tensor(_with_id)` 归还与合并。
- [contiguous_memory_allocator.py:179-229](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/contiguous_memory_allocator.py#L179-L229)：`_defragment_memory` —— 经典的"顺序向前压缩"；空块比目标小则分多段拷贝（211-220 行的 while 循环）。
- [contiguous_memory_allocator.py:138-141](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/contiguous_memory_allocator.py#L138-L141)：`_reset_param_data` —— 搬移后把每个 param.data 重新 narrow 到新地址，保证参数视图不失联。
- [contiguous_memory_allocator.py:121-131](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/contiguous_memory_allocator.py#L121-L131)：`print_allocation` —— 用字符画打印缓冲占用图（`.`=空闲 `|`=占用），调试碎片问题的利器。

#### 4.5.4 代码实践

1. **实践目标**：亲手制造一次碎片整理。
2. **操作步骤**（纯 CPU、无需分布式，示例代码）：

```python
from deepspeed.runtime.zero.contiguous_memory_allocator import ContiguousMemoryAllocator

alloc = ContiguousMemoryAllocator(size=1000, dtype=torch.float32, device="cpu")
a = alloc.allocate_tensor(400)   # [0,400)
b = alloc.allocate_tensor(400)   # [400,800)
alloc.release_tensor(a)          # 空闲: [0,400) 与 [800,1000)，largest_contiguous=400
alloc.print_allocation()
c = alloc.allocate_tensor(500)   # 放不下任何整块 → 触发 _defragment_memory
alloc.print_allocation()
```

   （标注：示例代码，本讲撰写，非项目原有；仅调用项目公开类。）
3. **需要观察的现象**：第三次分配前打印"Needs defragmentation to allocate. Before Defragmentation:"及占用图；b 被搬到 `[0,400)`、c 落在 `[400,900)`；`tensor.data` 内容在搬移后不变（memmove 语义）。
4. **预期结果**：`largest_contiguous` 从 400 恢复到接近总空闲量，分配成功。
5. 可在本地直接运行验证（该类不依赖分布式初始化时 `dist.get_rank()` 可用；若报错需先 `torch.distributed.init_process_group` 单进程初始化——**待本地验证**）。

#### 4.5.5 小练习与答案

**练习**：`_get_new_tensor_address` 用的是 best-fit（选**能容纳的最小**空闲块）而不是 first-fit。为什么？

**答案**：first-fit 会不断消耗头部大块，把大块切碎后，下一次大 all-gather（比如 embedding 层的巨大参数）就放不下、被迫频繁碎片整理（整体搬移是全缓冲量级的拷贝）。best-fit 优先消耗零碎小块，保住大块给大请求，把整理次数压到最低——这是显存分配器的常见取舍（伙伴系统/best-fit 的动机相同）。

### 4.6 融合 CPU Adam 步（#8841）：_grad_scale_for_cpu_adam 与延迟梯度缩放

#### 4.6.1 概念说明

本周期（`84679e9d → 84481c33`，PR #8841 "Fused CPU Adam step"）之前，ZeRO-3 + CPU 卸载的每个 step 对每个 CPU 子组做三遍主机内存遍历：

1. `unscale_and_clip_grads`：`grad.mul_(1/combined_scale)` —— 对整块 fp32 梯度分区的**读改写**（即使因子是 1 也照做）；
2. `DeepSpeedCPUAdam.step()`：内核逐段读梯度、更新 master；
3. `_reassign_or_swap_out_partitioned_parameters`：把 fp32 master 整块读出、经一个 fp32 设备临时中转、cast 成 bf16 写进权重分区——拷贝字节数是 bf16 所需的**两倍**。

#8841 把 1 和 3 都折进 2 的内核遍历：

- **grad_scale**：内核在**加载每条梯度时**乘上因子（与先 `grad.mul_(f)` 再 step 的两遍式逐位一致——同一个 float 乘法，只是位置不同），梯度本身留在原地不动；
- **bf16_copy**：同一次内核遍历把更新后的 fp32 参数按 round-to-nearest-even 写出 bf16 副本，通过分片回调交给 stage3 流水拷回设备。

关键难点：梯度不就地缩放后，**内存里的梯度值与旧路径不同步了**。旧路径里，一个"本窗口没被 backward 覆写的参数"（比如只在第 0 步用过的层、或某轮没被路由到的专家）的梯度会在每一步被乘上每一步的因子；下一次 step 或任何梯度读取还会再用它。融合路径必须补上这些乘法，且只补"未被覆写"的那些——这就是 `_deferred_grad_scale` + `_offload_grad_written_ids` 的延迟补乘机制。

#### 4.6.2 核心流程

```text
step() 逐子组：
  factor = _grad_scale_for_cpu_adam(sub_group_id, total_norm)
      判定（全部满足才非 None）：
        deepspeed_adam_offload（DeepSpeedCPUAdam 卸载）
        ∧ subgroup_to_device[i] == 'cpu'
        ∧ 无 torch GradScaler / 无 ZenFlow / 无 NVMe swap / 无 Muon
        ∧ fp32 梯度 ∧ fp32 master ∧ 都在 CPU
        ∧ optimizer.grad_scale_supported()（除 RVV 内核外全部支持）
      factor = 1 / _combined_grad_scale(total_norm)
  factor 为 None → 旧就地路径
  factor != 1.0 → _deferred_grad_scale[i] = factor（不写梯度）

补乘时机（三处）：
  a. 下一次 _pre_step（_apply_all_deferred_grad_scales，溢出检查之前）
  b. 所有梯度读取入口：get_fp32_grad_partitions / _get_fp32_grad_state_partition（safe_get_full_grad 底层）
  c. （见 3501 行附近的其它入口）
  补乘对象 = 子组内 ds_id ∉ _offload_grad_written_ids 的参数
  （_offload_grad_written_ids 在 _offload_grad_partition_at_boundary 里
   随每个被本窗口覆写的参数梯度登记，_pre_step 清空）
```

为什么"未被覆写"要补、"被覆写"不补：被覆写的参数梯度是新值，天然不带旧因子；未被覆写的参数梯度在旧路径里已经带上了**历次**因子（每步乘一次），融合路径在每步结束只欠**当步**一次，补上后两者相等。

#### 4.6.3 源码精读

- [stage3.py:3167-3175](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L3167-L3175)：`_combined_grad_scale` —— `loss_scale × clamp((norm/scale + 1e-6)/clip_grad, min=1.0)`；被裁剪时 clip>1，因子<1。
- [stage3.py:3177-3180](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L3177-L3180)：旧就地路径 `unscale_and_clip_grads` —— 一行 `grad.mul_(1./combined_scale)`，一遍完整读改写。
- [stage3.py:3182-3202](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L3182-L3202)：`_grad_scale_for_cpu_adam` 的完整判定链与返回值，docstring 逐条说明等价性与适用边界。
- [stage3.py:3204-3216](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L3204-L3216)：`_apply_deferred_grad_scale` —— `pop` 出因子，跳过 `ds_id in _offload_grad_written_ids` 的参数，对每个欠账参数的梯度切片 `mul_(factor)`。
- [stage3.py:1526-1528](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L1526-L1528)：`_offload_grad_written_ids.add(param.ds_id)` 的登记点（梯度写入 CPU fp32 缓冲时）。
- [stage3.py:3311-3312](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L3311-L3312) 与 [stage3.py:3346-3347](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L3346-L3347)：读取入口的补乘（`get_fp32_grad_partitions` / `_get_fp32_grad_state_partition`，后者是 `safe_get_full_grad` 的底层）。
- [stage3.py:259-264](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L259-L264)：两个记账结构的初始化（`_deferred_grad_scale` 字典、`_offload_grad_written_ids` 集合）。
- 内核侧（u5-l4 的延伸）：[cpu_adam.py:117-196](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/ops/adam/cpu_adam.py#L117-L196) 的 `step(grad_scale=...)` 分派到 `adam_update_scaled`；[cpu_adam_impl.cpp:299-307](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/csrc/adam/cpu_adam_impl.cpp#L299-L307) 的 `ds_adam_grad_scale_supported`（仅 RVV 内核不支持）；pybind 入口见 [cpu_adam.cpp:12-28](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/csrc/adam/cpu_adam.cpp#L12-L28)。

#### 4.6.4 代码实践（对应总实践任务第三项之一）

1. **实践目标**：验证融合缩放与就地缩放逐位一致，以及"只在第 0 步用到的层"也能对上。
2. **操作步骤**：运行本周期新增的回归（可在 CPU 后端、双进程下跑，参考 [tests/unit/v1/zero/test_zero.py:2110-2172](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/zero/test_zero.py#L2110-L2172) 的 `_train_zero3_cpu_offload` helper）：

```bash
pytest tests/unit/v1/zero/test_zero.py::TestZero3CpuAdamGradScale -v
pytest tests/unit/v1/zero/test_zero.py::TestZero3CpuAdamUnusedLayer -v
```

   - `TestZero3CpuAdamGradScale`（[test_zero.py:2199-2221](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/zero/test_zero.py#L2199-L2221)）：参数化 `pin_memory × clip(1e-3, 1e6) × ratio(1.0, 0.5)`。用 `_scale_gradients_in_place`（把 `_grad_scale_for_cpu_adam` 打成恒 None，[test_zero.py:2175-2177](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/zero/test_zero.py#L2175-L2177)）跑出参考，与融合路径比参数和最末步梯度，`torch.equal` 逐位断言；并断言 clip=1e-3 时所有因子 <1、clip=1e6 时全为 1。
   - `TestZero3CpuAdamUnusedLayer`（[test_zero.py:2257-2279](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/zero/test_zero.py#L2257-L2279)）：`_SometimesUsedLayerModel` 的 `extra` 层只在 step 0 前向（[test_zero.py:2092-2107](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/zero/test_zero.py#L2092-L2107)），此后梯度不再被覆写——正是延迟补乘要覆盖的场景。
3. **需要观察的现象**：两组测试全绿；若手动打印 `_record_grad_scales` 捕获的因子（[test_zero.py:2180-2192](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/zero/test_zero.py#L2180-L2192)），可见 clip=1e-3 时因子略小于 1。
4. **预期结果**：`assert torch.equal(a, b)` 全部通过——参数与梯度两条路径逐位一致。
5. 在本仓库当前环境**待本地验证**。

#### 4.6.5 小练习与答案

**练习 1**：为什么判定链要排除 torch GradScaler、ZenFlow、NVMe swap 和 Muon？

**答案**：torch GradScaler 有自己的 unscale 时机与断言（二次 unscale 会报错），让内核代乘会破坏其不变量；ZenFlow 的选择性优化器走独立的 `zenflow_cpu_optimizer_step` 路径；NVMe swap 的梯度/状态由 swapper 管理，何时在内存里不确定；Muon 的更新是 Newton-Schulz 正交化（更新量对 loss scale 不齐次，见 `_muon_update_lacks_loss_scale`，[stage3.py:2663-2671](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L2663-L2671)），不存在"梯度乘因子"语义。融合路径只在能**证明**等价的子集上启用。

**练习 2**：`grad_scale_supported()` 为什么除 RVV（RISC-V 向量)内核外全支持？

**答案**：见 [cpu_adam_impl.cpp:299-306](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/csrc/adam/cpu_adam_impl.cpp#L299-L306) 的注释——RVV 的 `Step_RVV` 实现没有接收/应用 `_grad_scale` 参数，属于内核能力缺口而非设计选择；AVX512/AVX2/NEON/标量内核都在加载梯度时执行同一个 float 乘法。

### 4.7 双缓冲 pinned bf16 拷回：_cpu_adam_step_and_copy_back

#### 4.7.1 概念说明

缩放折进内核解决了"一遍多余的主机读改写"；另一半收益是拷回。旧路径 `_copy_pinned_master_to_device` 把 fp32 master 整块送上设备再在设备上 cast，拷贝字节数是 bf16 目标的 2 倍，还要在设备上开一块整子组大小的 fp32 临时区。融合拷回改为：**CPU Adam 内核一边更新参数、一边把 bf16 副本写进 pinned 主机缓冲**，stage3 用两块轮换的 pinned bf16 缓冲把"内核写一片"与"上一片异步 H2D 拷贝"流水起来——CPU 计算 PCIe 拷贝，互不等待。

正确性细节有三：

1. **分片对齐**：分片尺寸经 `aligned_piece_numel` 向下取整到内核最宽向量块（AVX512 下 `SIMD_WIDTH*8 = 128` 个 float）的整数倍，保证每个元素走的代码路径与整段调用完全相同、舍入逐位一致（128 的余数尾巴单独成片）。
2. **缓冲复用同步**：重写某块缓冲前，必须等"两次之前从这块发出的拷贝"完成（同块缓冲最多有一份在途拷贝 + 一份待发）。
3. **返回前等最后一片**：下一个 forward 的参数 all-gather 跑在与本拷贝不同的流上、不会等待它，所以函数返回前必须 `event.synchronize()` 保证权重分区已就位。

#### 4.7.2 核心流程

```text
step() 中：
  grad_scale 非None ∧ _can_copy_back_from_cpu_adam(i)
      （bf16 设备分区 ∧ offload pin_memory ∧ optimizer.copy_supported()）
  → _cpu_adam_step_and_copy_back(i, grad_scale)
      buffers = 两块 pinned bf16（每块 2**26 元素，惰性创建）
      piece_id = 0
      before_piece(param, start, count):
          若 copy_events[piece_id % 2] 存在 → synchronize（等两次前的拷贝）
          返回 buffers[piece_id % 2][:count]
      after_piece(param, start, count, out):
          dst[start:start+count].copy_(out, non_blocking=True)  ← 异步 H2D
          记录 event 到 copy_events[piece_id % 2]；piece_id += 1
      optimizer.step(grad_scale=..., bf16_copy=(piece_numel, before, after))
      最后：synchronize 最后一片的事件 → _unflatten_partitioned_parameters
```

2^26（67,108,864）个 bf16 元素 = 每块 128 MiB；取 2 的幂是为了 pinned 分配器不做向上取整（[stage3.py:254-258](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L254-L258) 注释）。

#### 4.7.3 源码精读

- [stage3.py:2973-2982](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L2973-L2982)：`_can_copy_back_from_cpu_adam` —— 仅在 `_grad_scale_for_cpu_adam` 已返回因子时询问；bf16 设备分区 + pin_memory + `copy_supported()`。
- [stage3.py:2984-3025](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L2984-L3025)：`_cpu_adam_step_and_copy_back` 全文——`before_piece` 的"两次前事件同步"（3002-3007 行）、`after_piece` 的异步拷贝与事件记录（3009-3015 行）、返回前的最后事件同步（3020-3024 行，注释点明 all-gather 不等本流）。docstring（2986-2989 行）对比了被替代的两步式：读 fp32 master + 双倍字节 + 整子组 fp32 设备临时区，并声明"same values, same rounding"。
- [stage3.py:2960-2971](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L2960-L2971)：旧路径 `_copy_pinned_master_to_device`（master 以自身 dtype 上设备后 cast + 事件同步），与融合路径形成对照。
- [cpu_adam.py:133-142](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/ops/adam/cpu_adam.py#L133-L142)（bf16_copy 协议文档）与 [cpu_adam.py:214-228](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/ops/adam/cpu_adam.py#L214-L228)（`_step_in_pieces_with_copy`：`aligned_piece_numel` 对齐 + 逐片调 `adam_update_scaled_copy`）。
- [cpu_adam.py:207-212](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/ops/adam/cpu_adam.py#L207-L212)：`aligned_piece_numel` —— 向下取整到向量块整数倍（至少一块）。
- [cpu_adam_impl.cpp:309-320](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/csrc/adam/cpu_adam_impl.cpp#L309-L320)：`ds_adam_block_numel` —— 注释解释为什么分片起点必须是该块数的倍数（尾部元素走窄向量/标量代码，float 舍入可能不同）。
- [cpu_adam_impl.cpp:282-297](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/csrc/adam/cpu_adam_impl.cpp#L282-L297)：`ds_adam_copy_supported` —— AVX512/NEON/标量返回 true，AVX2/SVE/RVV 返回 false（向量循环缺 bf16 store）。
- [stage3.py:584-585](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage3.py#L584-L585)：`destroy()` 的 `_unpin_offload_buffers` 也 unpin 这两块拷回缓冲——u2-l3 讲的销毁引用链在这里的落点。

#### 4.7.4 代码实践（对应总实践任务第三项之二）

1. **实践目标**：验证小分片反复复用两块缓冲时，融合拷回与两步式逐位一致；并观察分片回调的节奏。
2. **操作步骤**：
   - 运行 `pytest tests/unit/v1/zero/test_zero.py::TestZero3CpuAdamCopyBack -v`（[test_zero.py:2226-2252](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/zero/test_zero.py#L2226-L2252)）：helper 传入 `piece_numel=1000`（远小于子组、**不是** 128 的倍数 → 正好覆盖对齐取整与尾巴片逻辑），并用 `record` 补丁确认融合路径确实被走到。
   - fp16 溢出跳步等价性：`pytest tests/unit/v1/zero/test_zero.py::TestZero3CpuAdamFp16Overflow -v`（[test_zero.py:2283-2305](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/zero/test_zero.py#L2283-L2305)）：第 0 步 loss×inf → 两路径都 skipped_steps==1、loss_scale 同步下降且低于 2^8、后续参数逐位一致。
   - 进阶（源码实验，勿提交）：把 `_cpu_adam_step_and_copy_back` 里 `after_piece` 的 `event.record()` 换成打印 `piece_id` 与 `count`，观察分片序列如何交替使用 `buffers[0]`/`buffers[1]`，以及 1000 元素请求被 `aligned_piece_numel` 实际切成的片宽（应为 128 的倍数如 896，余数为尾片）。
3. **需要观察的现象**：分片 id 严格 0,1,2,... 交替缓冲；同一缓冲的两次写入之间恰有一个已记录的拷贝事件；测试全绿。
4. **预期结果**：`torch.equal` 断言通过——bf16 副本等于对整段更新结果做 `param.to(torch.bfloat16)`（round-to-nearest-even）。
5. **待本地验证**（CopyBack 测试在 CPU 后端会跳过——分区从不上设备，见 [test_zero.py:2234-2235](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/zero/test_zero.py#L2234-L2235) 的 skip 条件，需要真实加速器）。

#### 4.7.5 小练习与答案

**练习 1**：为什么是两块缓冲轮换，一块或三块行不行？

**答案**：一块无法流水——内核写下一片前必须等上一片的 H2D 完成，CPU 与 PCIe 完全串行。两块已足够让"内核写第 k 片"与"拷贝第 k-1 片"并行。三块能容忍拷贝偶尔慢于内核而不阻塞，但每块 128 MiB pinned 主机内存很贵，且 PCIe 带宽通常是瓶颈（内核几乎总能跑赢拷贝），两块已达到饱和——这是工程上的性价比选择。

**练习 2**：`before_piece` 里 `event.synchronize()` 等的是"两块之前的同位事件"（`copy_events[piece_id % 2]`），为什么不等等一块之前（`piece_id-1`）的？

**答案**：`dst[start:...].copy_(out, non_blocking=True)` 只与**同一目标缓冲区**的其它拷贝有写冲突；相邻片（piece_id-1）写的是 dst 的不同区间且来自另一块源缓冲，流内顺序已保证。真正不能重用的是**源** pinned 缓冲：它马上要被内核覆写，所以必须等到"从它发出的上一次拷贝"（即两次前、同侧的那次）完成为止。

## 5. 综合实践

**任务：给融合 CPU Adam 步做一次"等价性 + 性能"双维度的本地验收。**

写一个脚本（或直接复用测试 helper），对同一个 4 层 `SimpleModel`（`hidden_dim=256`，`sub_group_size=20000` 强制多子组）在 ZeRO-3 + `offload_optimizer: {device: cpu, pin_memory: true, ratio: 0.5}` + bf16 下训练 3 步，完成三组对照：

1. **等价性**：用 `_scale_gradients_in_place` 补丁跑参考路径，用 `_record_grad_scales` 补丁跑融合路径，比较每步后 `GatheredParameters` 下的参数与 `safe_get_full_grad` 的梯度，断言 `torch.equal`（对照 [test_zero.py:2199-2221](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/zero/test_zero.py#L2199-L2221) 的写法）。
2. **覆盖面**：加一个"只在第 0 步用到的层"（仿 `_SometimesUsedLayerModel`），验证延迟补乘让最末步梯度仍逐位一致；再设 `gradient_clipping: 1e-3` 观察记录到的因子是否全部 <1。
3. **性能与桶**：把同一脚本的 `reduce_bucket_size` 在 `{1e4, 5e8}` 两档间切换，外层计时 `backward+step`；如有条件加 `"wall_clock_breakdown": true` 并对比 `optimizer` 计时项；同时对比开/关融合路径（用补丁强制关闭）时该计时项的差异——融合收益体现在 CPU 卸载子组的 step 用时上。

预期：等价性断言全部通过；融合路径的 optimizer step 计时下降（省一遍主机梯度遍历 + 拷回字节数减半）；桶大小主要影响 backward 阶段而非 step 阶段。所有数值**待本地验证**（双进程 CPU 即可跑等价性；性能对比在真实加速器 + PCIe 环境下更有意义）。

## 6. 本讲小结

- `step()` 的骨架是"预清理（含延迟补乘）→ 溢出检查 → 全局范数 → 逐子组：准备 → 缩放 → 更新 master → 写回 ds_tensor → 释放"；梯度 reduce-scatter 发生在 backward 的 IPG 桶 flush 中，step 只消费已落位的梯度分区。
- IPG 桶按通信 dtype 分桶（fp32 保真参数独立成桶），`reduce_bucket_size`/`contiguous_gradients`/`reduce_scatter` 共同决定归约的批次大小与路径；每累积 dtype 一块平坦梯度缓冲。
- partial offload 用 `subgroup_to_device` 字符串表给每个子组定驻地，设备名一律走 `get_accelerator().device_name()` 公共 API；CPU 子组由 DeepSpeedCPUAdam 更新，GPU 子组由 backup_optimizer 更新。
- fp32 模型（含 autocast）下 `aliased_fp32_sub_groups` 让权重分区兼任 master，省一整份显存；代价是 `offload_states` 里这些子组只能随 lp_params 整体搬运，单独搬 hp_params 不省显存并触发 `warning_once`。
- `ContiguousMemoryAllocator` 用单块预分配缓冲 + 地址簿 + 搬移式碎片整理，消除 all-gather 临时张量对 caching allocator 的碎片压力。
- #8841 的融合 CPU Adam 步：`grad_scale` 让内核在加载梯度时应用缩放因子（`_grad_scale_for_cpu_adam` 判定适用条件，`_deferred_grad_scale`/`_offload_grad_written_ids` 负责延迟补乘保证逐位一致），`bf16_copy` + 双 pinned 缓冲把 bf16 拷回折进同一遍内核并流水化——四个新回归测试（GradScale/CopyBack/UnusedLayer/Fp16Overflow）把这组等价性契约固定下来。

## 7. 下一步学习建议

- **u5-l7（ZeRO-Infinity）**：把本讲的 CPU 卸载推向 NVMe——`_swappable_optimizer_subgroup` 分支与 `optimizer_swapper` 的换入换出在本讲已多次出现，下一讲正面讲解。
- **u5-l9（ZeRO-3 混合 dtype）**：本讲反复出现的"fp32 保真参数"完整机制（`_split_by_partition_dtype`、按 dtype 的梯度缓冲与桶、offload_states 的多 dtype 拒绝矩阵）在那一讲系统展开。
- **源码延伸**：对照阅读 [deepspeed/runtime/zero/stage_1_and_2.py](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/deepspeed/runtime/zero/stage_1_and_2.py) 的 step（u5-l3），思考为什么 stage1/2 的梯度分区布局是"每子组一块"而 stage3 是"每累积 dtype 一块"；再读 [tests/unit/v1/zero/test_zero.py](https://github.com/deepspeedai/DeepSpeed/blob/84481c333875abc8a47f1b0d07fdadddb8d70fd8/tests/unit/v1/zero/test_zero.py) 的 `TestDestroyReleasesEngine`（u2-l3 已讲），看本讲的 `_unpin_offload_buffers` 如何被销毁链覆盖。
