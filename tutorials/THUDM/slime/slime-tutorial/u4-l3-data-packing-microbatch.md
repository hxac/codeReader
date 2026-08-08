# 数据打包、微批调度与 loss mask

## 1. 本讲目标

上一讲（u4-l2）我们看清了 `train_one_step` 的六拍单步流程，并知道 slime 把 Megatron 的 `get_forward_backward_func()` 当作「执行器」，用 `forward_step` 闭包注入 RL 损失。但有一个关键问题被刻意跳过了：**那个 `forward_step` 闭包里，数据是怎么来的？** 一句话——它来自 `get_batch(data_iterator, ...)`，而 `data_iterator` 背后是一张在 rollout 阶段就算好的微批调度表。

本讲要回答的核心问题是：**rollout 产出的一堆变长 `Sample`，是如何被打包成 Megatron 能消费的张量微批的？**

学完本讲你应该能够：

1. 说出 slime 的「两阶段管线」：在 rollout 端用 `build_dp_schedule` 一次性算好调度表，在训练端用 `DataIterator` + `get_batch` 逐微批还原成张量，并理解为什么要把这两步分开。
2. 手算动态批（`--use-dynamic-batch-size`）如何用 first-fit 装箱把变长样本切成若干微批，以及 DP rank 之间如何分发。
3. 解释 `loss_mask` 为什么要在左边补 `prompt_length - 1` 个 0、右边补 1 个 0，才能和 next-token 预测对齐。
4. 描述开启 context parallel（CP）后，一条序列如何被「zigzag」切成两半，以及 THD 布局（`cu_seqlens` / `PackedSeqParams`）如何防止跨样本注意力泄漏。

## 2. 前置知识

阅读本讲前，你需要了解以下概念（不熟悉的话建议先看 u4-l2 与 u3-l1）：

- **微批（micro-batch, mbs）**：流水线并行中，一个训练 step 会被切成多个小批次依次过模型。Megatron 的 `forward_backward_func` 接收 `num_microbatches` 参数，逐个调用 `forward_step` 拉取数据。
- **数据并行（DP）、张量并行（TP）、流水线并行（PP）、上下文并行（CP）**：Megatron 的四类并行。本讲重点关注 DP（样本怎么分到不同 rank）和 CP（一条长序列怎么切到不同 rank）。
- **`Sample`**：slime 的核心数据载体（见 u3-l1）。它含 `tokens`（prompt+response 全序列）、`response_length`、`loss_mask`（仅覆盖 response，长度等于 `response_length`）等字段。本讲处理的正是 `list[Sample]` 到训练张量的转换。
- **next-token 预测**：因果语言模型中，位置 `i` 的 logit 预测的是位置 `i+1` 的 token。这个「左移一位」的特性是理解 loss mask 对齐的关键。
- **装箱问题（bin packing）**：把大小不一的物品装进容量有限的箱子，目标是用的箱子少/装的满。first-fit（首次适应）是最简单的贪心策略。

一个贯穿全讲的术语：**THD 布局**。`T` 表示打包后的序列长度（把多条样本拼成一条长序列），`H` 是注意力头数，`D` 是每个头的维度。`cu_seqlens` 是每条子序列结束位置的累计偏移量，FlashAttention 据此在一条长序列里独立地计算每条子序列的注意力，从而做到「打包但不串台」。

## 3. 本讲源码地图

本讲涉及的关键文件及其职责：

| 文件 | 职责 | 在哪一阶段运行 |
|------|------|----------------|
| `slime/utils/dp_schedule.py` | 纯 Python 的微批打包 + DP 分发调度（`build_dp_schedule`） | rollout 端，每轮一次 |
| `slime/utils/seqlen_balancing.py` | first-fit 装箱、Karmarkar-Karp 均衡、拆箱对齐等算法 | 被 `dp_schedule` 调用 |
| `slime/backends/megatron_utils/data.py` | `DataIterator`（按调度逐微批拉数）与 `get_batch`（张量化、CP、THD、loss mask） | 训练端，每微批一次 |
| `slime/backends/megatron_utils/cp_utils.py` | `slice_with_cp`（zigzag CP 切片）等 CP 工具 | 被 `get_batch` 调用 |
| `slime/ray/rollout.py` | `_convert_samples_to_train_data`（展平 Sample）与 `_split_train_data_by_dp`（调用调度并分箱） | rollout 端，衔接调度与训练 |
| `slime/backends/megatron_utils/model.py` | `forward_only` / `train_one_step` 内部调用 `get_batch` | 训练端，被 Megatron 引擎驱动 |
| `tests/test_dp_schedule.py` | `build_dp_schedule` 的 CPU 单元测试（不动 GPU/Megatron/Ray） | 测试 |

记住一条主线：**调度是「一次性、全局、纯 Python」地算好的；张量化是「每微批、每 rank、需 Megatron」地还原的。** 这一分工是本讲的灵魂。

## 4. 核心概念与源码讲解

### 4.1 两阶段管线总览：调度在 rollout 端算，张量在训练端拼

#### 4.1.1 概念说明

一个自然的疑问是：为什么不直接在训练工人里，拿到样本就当场决定怎么切微批？

答案是**信息不对称**。决定「哪些样本放进同一个微批」需要看到**全部**样本的长度，才能做全局均衡；而每个训练工人（DP rank）只能看到分给自己的那一份。如果让每个 rank 各自为政地切，就无法保证一个关键不变量——**所有 DP rank 必须跑相同数量的微批**（否则 PP 同步会卡死）。

因此 slime 采用两阶段管线：

1. **调度阶段（rollout 端，单进程，每轮一次）**：`RolloutManager` 持有全部样本与长度，调用 `build_dp_schedule` 算出 `partitions`（每个 rank 拿哪些全局样本下标）和 `micro_batch_indices`（每个 rank 内部，哪些样本组成一个微批）。然后把每个 rank 的切片打包成 `rollout_data` 字典，经 Ray object store 投递。
2. **张量化阶段（训练端，每 rank、每微批）**：训练工人收到自己的 `rollout_data` 后，用 `DataIterator` 按 `micro_batch_indices` 逐个吐出微批，再用 `get_batch` 把变长张量拼成 Megatron 能吃的 `[1, T_padded]` 张量 + `PackedSeqParams`。

这种分离带来一个工程红利：调度逻辑完全不依赖 Ray / Megatron / GPU，可以在纯 CPU 上单测——这正是 `tests/test_dp_schedule.py` 存在的原因。

#### 4.1.2 核心流程

```
rollout 端（RolloutManager，单进程）:
  list[Sample]
    │  _convert_samples_to_train_data        （展平成 dict: tokens/loss_masks/...）
    ▼
  train_data: dict[str, list]                 （每条样本一个张量/标量）
    │  _split_train_data_by_dp
    │    └─ build_dp_schedule(...)            （纯 Python，全局均衡）
    ▼
  partitions[r], micro_batch_indices[r]       （每个 rank 的调度表）
    │  按 partition 切片 + ray.put(Box)
    ▼
  rollout_data (per rank)  ─────► Ray object store ─────► 训练工人

训练端（每个 MegatronTrainRayActor，每 rank）:
  rollout_data
    │  get_data_iterator → list[DataIterator] （每个 VPP stage 一个）
    ▼
  DataIterator.get_next(keys)                 （按 micro_batch_indices 吐一个微批）
    │  get_batch                              （CP 切片 + THD 打包 + loss mask 对齐 + pad）
    ▼
  tokens[1, T_padded] + PackedSeqParams + full_loss_masks[1, T_padded]
    │  forward_step 闭包                      （喂给 Megatron pipeline 引擎）
    ▼
  output_tensor → loss_function
```

注意 `get_data_iterator` 会为每个 VPP（虚拟流水线）stage 各建一个 `DataIterator`，但它们**共享同一份** `micro_batch_indices`（VPP 只是让多个 stage 交错消费同一批微批）。

#### 4.1.3 源码精读

调度阶段在 rollout.py 中触发。`generate` 完成后，样本先被展平为训练字典，再被分箱：

- [slime/ray/rollout.py:566-567](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L566-L567) —— `generate` 把 `data` 经 `_convert_samples_to_train_data` 转成训练字典后，交给 `_split_train_data_by_dp`。
- [slime/ray/rollout.py:847-853](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L847-L853) —— `_split_train_data_by_dp` 调用 `build_dp_schedule`，传入每条样本的 `total_lengths` 与 `rollout_ids`，拿回四元组 `(partitions, micro_batch_indices, num_microbatches, global_batch_sizes)`。
- [slime/ray/rollout.py:855-895](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L855-L895) —— 按 `partitions[r]` 切出该 rank 的样本子集，连同 `num_microbatches`、`micro_batch_indices[r]` 装进 `rollout_data`，再 `ray.put` 成 `Box` 投递。

张量化阶段在训练工人中触发。`train_actor`（actor/critic）都先建迭代器：

- [slime/backends/megatron_utils/actor.py:418-422](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/actor.py#L418-L422) —— `train_actor` 调 `get_data_iterator(rollout_data)` 得到迭代器，并取出 `num_microbatches`、`global_batch_sizes`（critic 路径同理，见 L390-394）。
- [slime/backends/megatron_utils/model.py:412-417](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L412-L417) —— `forward_only` 内的 `forward_step` 闭包调 `get_batch(data_iterator, batch_keys, ...)` 拿到一个微批；训练路径在 [model.py:577-600](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/model.py#L577-L600) 同样调用。

#### 4.1.4 代码实践

**实践目标**：建立「调度在 rollout 端、张量化在训练端」的肌肉记忆。

**操作步骤**：

1. 打开 `slime/ray/rollout.py`，定位 `_split_train_data_by_dp`（L831），确认它返回 `rollout_data_refs`（一组 Ray `Box`）。
2. 打开 `slime/backends/megatron_utils/actor.py`，定位 `train_actor`（L418），确认它接收的 `rollout_data` 已是某 rank 的切片。
3. 在 `model.py` 中搜索 `get_batch(`，确认它只出现在 `forward_only`（L412）与 `train_one_step`（L577）的 `forward_step` 闭包内。

**需要观察的现象**：`build_dp_schedule` 与 `get_batch` 永远不会出现在同一个进程里——前者在 rollout 端单进程调用，后者在每个训练工人、每个微批调用。

**预期结果**：你会看到调度表（`micro_batch_indices`）是「跨进程、跨 rank」流动的媒介，而 `get_batch` 是纯函数式的张量化器。

#### 4.1.5 小练习与答案

**练习 1**：为什么不能让每个 DP rank 各自决定自己拿到的样本怎么切微批？

**答案**：因为流水线并行要求所有 DP rank 在每个训练 step 跑**相同数量**的微批（否则 PP 的前向/后向同步会失配）。各自为政无法保证这个不变量，所以必须由一个能看到全局样本的中心点统一调度。

**练习 2**：`build_dp_schedule` 被设计成「不 import Ray / sglang / megatron」。这种隔离带来了什么好处？

**答案**：它可以在纯 CPU、无 GPU 的 CI 环境里用单元测试直接覆盖（见 `tests/test_dp_schedule.py`），不依赖任何重型 CUDA 库或分布式运行时，回归成本极低。

---

### 4.2 dp_schedule：纯 Python 的「先打包再分发」微批调度

#### 4.2.1 概念说明

`build_dp_schedule` 是调度的「大脑」，定义在 `slime/utils/dp_schedule.py`。它的设计哲学（写在文件开头的 docstring 里）是 **pack first, distribute second（先打包，再分发）**：

1. **先打包**：把一个训练 step 内的样本，按长度塞进若干「微批箱子」（mbs）。动态批用 first-fit 装箱（箱子有容量上限 `max_tokens_per_gpu * cp_size`），静态批则按固定 `micro_batch_size` 等分。
2. **对齐微批数**：把微批数 `K` 调整为 `dp_size`（VPP 时还要乘 `mb_group`）的倍数，确保每个 rank 分到相同数量微批。
3. **再分发**：把 `K` 个微批分给 `dp_size` 个 rank，每个 rank 拿 `K // dp_size` 个。分发方式有两种：跨步轮转（strided round-robin）或基于 FLOPs 的 Karmarkar-Karp 均衡。

这里有一个关键的 RL 语义约束：**同一个 rollout 的所有样本必须留在同一个 step**。因为 GRPO 等算法的优势是「组内相对」算出来的，per-rollout 的损失归约器要求同组样本可见。在 compaction / subagent 场景下，一次 rollout 可能产出多条训练样本，它们必须整体进同一个 step。

#### 4.2.2 核心流程

`build_dp_schedule(args, train_parallel_config, total_lengths, *, global_batch_size, rollout_indices)` 的主流程：

```
1. 按 rollout_id 把样本分组（rollout_id_to_samples）。
2. num_steps = len(rollout_ids) // global_batch_size；每个 step 取 global_batch_size 个 rollout。
   （不足一个 step 的尾部 rollout 直接丢弃——这是「丢弃尾部」的由来。）
3. for 每个 step:
   a. 收集该 step 所有样本的下标与长度 step_lengths。
   b. _pack_step_into_mbs(step_lengths):  把样本装箱成 K 个 mbs
        - 动态: first_fit_pack(step_lengths, max_per_bin)   # max_per_bin = max_tokens_per_gpu * cp_size
        - 动态+FLOPs: get_seqlen_balanced_partitions(workloads, num_mbs)  # 按 FLOPs KK 均衡
        - 静态: 按 micro_batch_size 等分
   c. 对齐 K 到 align_to = dp_size * (mb_group if vpp>1 else 1) 的倍数:
        - 动态: expand_bins_by_splitting  (拆分最大的多样本箱子补足)
        - 静态: 若不可整除则直接报错
   d. num_mbs_per_rank = K // dp_size；分发:
        - balance_data: 按 mbs 的 FLOPs 权重用 KK 把 mbs 均衡分给 rank
        - 否则:        strided round-robin, rank r 拿 mbs[r, r+dp, r+2dp, ...]
   e. 把 (全局样本下标) 写进 partitions[r]，把 (rank 内局部下标) 写进 micro_batch_indices[r]。
4. 返回 (partitions, micro_batch_indices, num_microbatches, global_batch_sizes)。
```

它保证四条不变量（见文件头 docstring 与单测 `assert_invariants`）：

- 每个 DP rank 每个 step 跑**相同**的 `num_microbatches`（PP 同步要求）。
- 动态路径下（且不开 `balance_by_flops`），每个 mbs 的 token 数 `≤ max_tokens_per_gpu * cp_size`；**唯一例外**是单条样本本身就超 cap 时，它独占一个 mbs（允许超 cap）。
- 各 rank 样本下标的并集 = 丢弃尾部后的全部样本（每个样本恰好放一次）。
- 把某 rank 的 `micro_batch_indices` 摊平，恰好等于 `range(len(partitions[r]))`（该 rank 的样本被它的 mbs 调度恰好铺满一次）。

#### 4.2.3 源码精读

装箱与对齐的核心循环：

- [slime/utils/dp_schedule.py:146-189](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/dp_schedule.py#L146-L189) —— 对每个 step：先 `_pack_step_into_mbs` 装箱（L158），再算对齐目标 `target_K` 并补足（L168-185），最后 `num_mbs_per_rank = K // dp_size`。
- [slime/utils/dp_schedule.py:191-207](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/dp_schedule.py#L191-L207) —— 分发：`balance_data` 时按 FLOPs 权重 KK 分配，否则 strided round-robin；最后写 `partitions[r]` 与 `micro_batch_indices[r]`。

装箱策略函数 `_pack_step_into_mbs`：

- [slime/utils/dp_schedule.py:55-79](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/dp_schedule.py#L55-L79) —— 动态走 `first_fit_pack`（L76），动态+FLOPs 走 `get_seqlen_balanced_partitions`（L75，注意它**不**强制 token cap，可能 OOM），静态走固定等分（L79）。

底层算法（来自 verl，纯 Python）：

- [slime/utils/seqlen_balancing.py:180-198](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/seqlen_balancing.py#L180-L198) —— `first_fit_pack`：first-fit 装箱；放不进任何已有箱就开新箱，单条超 cap 的样本自然独占一箱。
- [slime/utils/seqlen_balancing.py:218-229](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/seqlen_balancing.py#L218-L229) —— `expand_bins_by_splitting`：反复把「最大的多样本箱」按 LPT 启发式拆成两半，凑到目标箱数，且保证拆出的子箱 token 数不超过原箱。
- [slime/utils/seqlen_balancing.py:146-177](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/seqlen_balancing.py#L146-L177) —— `get_seqlen_balanced_partitions`：Karmarkar-Karp 最大差分法（L176），让各分区总和尽量均衡。

参数定义（命令行）：

- [slime/utils/arguments.py:735-755](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L735-L755) —— `--use-dynamic-batch-size`（按最大 token 数自适应微批大小）与 `--max-tokens-per-gpu`（动态批的容量上限；开 CP 时应约为 `max_response_len // cp_size`）。help 文本里直接给了官方示例：3 条长度 100/200/300 的样本、`max_tokens_per_gpu=300`，会切成 2 个微批 `[100,200]` 与 `[300]`。

#### 4.2.4 代码实践

**实践目标**：在纯 CPU 上亲手跑通动态装箱与调度，验证「3 条样本切 2 个微批」。

**操作步骤**：

1. 在仓库根目录（已 `pip install -e .` 的环境，或保证 `slime` 可 import）写一个临时脚本 `pack_demo.py`：

   ```python
   # 示例代码：非项目原有文件，仅用于演示
   from slime.utils.seqlen_balancing import first_fit_pack
   from slime.utils.dp_schedule import build_dp_schedule
   from types import SimpleNamespace

   # 1) 直接体验 first-fit 装箱（纯标准库依赖，无需 torch/GPU）
   print(first_fit_pack([100, 200, 300], 300))   # 期望: [[0, 1], [2]]

   # 2) 用 build_dp_schedule 跑一遍动态调度
   args = SimpleNamespace(
       micro_batch_size=1, use_dynamic_batch_size=True,
       max_tokens_per_gpu=300, balance_data=False, balance_by_flops=False,
       hidden_size=16, num_attention_heads=2, num_query_groups=2,
       vocab_size=32, ffn_hidden_size=64, num_experts=None,
       num_layers=2, kv_channels=8,
   )
   tp = {"dp_size": 1, "cp_size": 1, "vpp_size": 1, "microbatch_group_size_per_vp_stage": 1}
   parts, mbi, nmb, gbs = build_dp_schedule(
       args, tp, [100, 200, 300], global_batch_size=3, rollout_indices=[0, 1, 2]
   )
   print("partitions:", parts)        # [[0, 1, 2]]
   print("micro_batch_indices:", mbi) # [[[0, 1], [2]]]  —— 两个微批
   print("num_microbatches:", nmb)    # [2]
   ```

2. 运行 `python pack_demo.py`。

**需要观察的现象**：`first_fit_pack([100,200,300], 300)` 返回 `[[0,1],[2]]`；`build_dp_schedule` 给出两个微批，第一个含样本 0、1（合计 300 token，正好顶满），第二个含样本 2。

**预期结果**：与 arguments.py 的 help 文本示例完全一致。随后可运行现成单测加强印象：`pytest tests/test_dp_schedule.py -m unit`（全 CPU、无 GPU）。

**待本地验证**：若 `import slime` 报错，说明环境未装好，请先按 u1-l3 完成安装。

#### 4.2.5 小练习与答案

**练习 1**：若 `total_lengths = [15, 3, 3, 3, 3, 3, 3, 3]`、`max_tokens_per_gpu=10`、`dp_size=2`，长度 15 的样本会怎样？参考 [tests/test_dp_schedule.py:193-224](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/tests/test_dp_schedule.py#L193-L224)。

**答案**：长度 15 > cap 10，`first_fit_pack` 放不进任何箱，于是独占一个 mbs（且这是唯一允许超 cap 的箱子）。其余 7 条长度 3 的样本正常装箱。这正是「超长样本独占一箱」不变量的体现。

**练习 2**：动态路径下，如果装箱得到的微批数 `K` 不是 `dp_size` 的倍数，slime 怎么处理？静态路径呢？

**答案**：动态路径调用 `expand_bins_by_splitting`，把最大的多样本箱拆成两半来凑足 `target_K`（L168-176）；静态路径无法安全拆分固定大小的箱，直接 `raise AssertionError`，要求用户调整配置使 `step_size % (dp_size * micro_batch_size * mb_group) == 0`（L177-185）。

---

### 4.3 DataIterator：按既定调度逐微批拉取

#### 4.3.1 概念说明

`DataIterator` 是一个极薄的迭代器，定义在 `slime/backends/megatron_utils/data.py`。它不做任何计算，只做一件事：**按预先算好的 `micro_batch_indices`，每次 `get_next()` 吐出一个微批对应的字段。**

它的存在是为了适配 Megatron 流水线引擎的拉取式接口。Megatron 的 `forward_backward_func` 会反复调用 `forward_step(data_iterator, model)`，每次消费一个微批；`forward_step` 再调 `data_iterator.get_next(keys)` 拿数据。所以 `DataIterator` 就是「调度表」与「Megatron 引擎」之间的读卡器。

#### 4.3.2 核心流程

```
DataIterator(rollout_data, micro_batch_indices):
    self.rollout_data = rollout_data       # 该 rank 的全部字段（dict[str, list]）
    self.micro_batch_indices = [...]        # 每个元素是一个微批：该微批含哪些「局部样本下标」
    self.offset = 0

get_next(keys):
    indices = micro_batch_indices[offset]   # 当前微批的局部下标列表
    for key in keys:
        batch[key] = [rollout_data[key][i] for i in indices]   # 按下标挑出这些样本的字段
    offset += 1
    return batch
```

注意「局部下标」是相对该 rank 自己的 `rollout_data`（即 `partitions[r]` 切片后的列表）而言的，所以下标从 0 开始连续。这也呼应了上一节的不变量：摊平 `micro_batch_indices[r]` 恰好等于 `range(len(partitions[r]))`。

#### 4.3.3 源码精读

- [slime/backends/megatron_utils/data.py:201-238](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/data.py#L201-L238) —— `DataIterator` 全貌：`get_next`（L219）按下标挑字段，`offset` 自增；`reset`（L235）把 `offset` 归零。
- [slime/backends/megatron_utils/data.py:241-245](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/data.py#L241-L245) —— `get_data_iterator`：按 VPP stage 数量各建一个 `DataIterator`，但**共享同一份** `micro_batch_indices`（VPP 让多 stage 交错消费同一批微批）。

`get_next` 返回的 `batch["tokens"]` 此时还是一个 **list**（每个元素是一条样本的 1D token 张量），尚未拼成 Megatron 要的二维张量——拼接是下一节 `get_batch` 的职责。

#### 4.3.4 代码实践

**实践目标**：手工构造 `DataIterator`，体会它「只按表拉数、不做计算」的薄度。

**操作步骤**：写一个临时脚本（示例代码）：

```python
# 示例代码：仅演示 DataIterator 的纯逻辑（字段用普通 list 代替张量）
from slime.backends.megatron_utils.data import DataIterator

rollout_data = {
    "tokens": [["a1", "a2"], ["b1"], ["c1", "c2", "c3"]],  # 3 条样本，模拟变长
    "response_lengths": [2, 1, 3],
}
# 假设调度表说：微批0 含样本0、2；微批1 含样本1
micro_batch_indices = [[0, 2], [1]]

it = DataIterator(rollout_data, micro_batch_indices)
print(it.get_next(["tokens", "response_lengths"]))  # 第 1 个微批
print(it.get_next(["tokens", "response_lengths"]))  # 第 2 个微批
```

**需要观察的现象**：第一次返回 `{'tokens': [['a1','a2'], ['c1','c2','c3']], 'response_lengths': [2, 3]}`，第二次返回 `{'tokens': [['b1']], 'response_lengths': [1]}`。

**预期结果**：`get_next` 严格按 `micro_batch_indices` 的每个子列表挑样本，顺序与下标完全对应；连续调用会自动推进 `offset`。

#### 4.3.5 小练习与答案

**练习 1**：`get_data_iterator` 为什么要为每个 VPP stage 各建一个 `DataIterator`，而不是共享一个？

**答案**：VPP（虚拟流水线）让多个 stage **交错**消费微批，每个 stage 各自维护独立的 `offset` 游标；若共享一个迭代器，多个 stage 会互相干扰 `offset`。它们共享的是底层 `micro_batch_indices` 调度表（内容相同），但游标各自独立。

**练习 2**：`get_next` 调用次数超过了 `micro_batch_indices` 的长度会怎样？

**答案**：`self.micro_batch_indices[self.offset]` 会抛 `IndexError`。这正是为什么调度阶段必须保证「每个 rank 的微批数 = `num_microbatches`」，而 Megatron 引擎也按这个数调用 `forward_step`——两边必须严丝合缝。

---

### 4.4 get_batch：变长序列变 Megatron 张量（CP / THD / loss mask）

#### 4.4.1 概念说明

`get_batch` 是本讲的「重头戏」，定义在 `slime/backends/megatron_utils/data.py`。它接收 `DataIterator` 吐出的一个微批（一组变长的 1D token 张量 + loss_mask 等），输出 Megatron 模型 `forward` 所需的：

- `tokens`：形状 `[1, T_padded]` 的拼接张量（多条样本首尾相接，必要时补 pad）。
- `packed_seq_params`：THD 布局的 `PackedSeqParams`，告诉 FlashAttention 每条子序列的边界（`cu_seqlens`）。
- `full_loss_masks`：与 `tokens` 同形的损失掩码，已完成 next-token 对齐。
- `unconcat_tokens`：未做 CP 拼接的原始 token 列表（算 logprob 时需要完整序列）。

它要同时解决三个难题：

1. **打包（packing）**：把多条变长样本拼进一个稠密张量，省去按最长样本补齐的浪费；但要用 `cu_seqlens` 防止跨样本注意力。这就是 THD 布局。
2. **context parallel 切片**：开启 CP 时，每条序列要被「zigzag」切成两块分给不同 CP rank；slime 支持两种模式——逐条切片（zigzag，默认）和全局拼接后一次切片（`allgather_cp` / DSA 模式）。
3. **loss mask 对齐**：原始 `loss_mask` 只覆盖 response（长度 = `response_length`）。要让它和「每个位置预测下一个 token」的 logit 对齐，得左移一位——左边补 `prompt_length - 1` 个 0、右边补 1 个 0。

#### 4.4.2 核心流程

```
get_batch(data_iterator, keys, pad_multiplier=128, allgather_cp=False):
    batch = data_iterator.get_next(keys)            # tokens: list[1D], loss_masks: list[1D]...
    pad_size = tp_size * pad_multiplier              # 对齐粒度，减少显存碎片
    batch["unconcat_tokens"] = tokens               # 保留未切片原始序列（logprob 用）

    # —— tokens 的 CP 处理 ——
    if allgather_cp:                                 # DSA 模式：先全局拼接，再一次性切
        tokens = torch.cat(tokens)
        按全局粒度 pad 到 cp_size*pad_size 的倍数
        tokens = tokens.chunk(cp_size)[cp_rank]      # 每个 rank 拿等长的一整块
        cu_seqlens 记录原始序列边界
    else:                                            # zigzag 模式：逐条切片
        tokens = [slice_with_cp(t, 0) for t in tokens]   # 每条切成 head+tail 两块拼接
        tokens = torch.cat(tokens)
        pad 到 pad_size 的倍数
        cu_seqlens = torch.tensor(cu_seqlens) * cp_size

    # —— THD 布局 ——
    max_seqlen = (cu_seqlens[1:] - cu_seqlens[:-1]).max()
    packed_seq_params = PackedSeqParams(cu_seqlens_q=cu_seqlens, cu_seqlens_kv=cu_seqlens,
                                        max_seqlen_q=max_seqlen, qkv_format="thd")
    tokens = tokens.unsqueeze(0)                     # [1, T_padded]

    # —— loss mask 对齐 ——
    for (loss_mask, total_length, response_length) in zip(...):
        prompt_length = total_length - response_length
        loss_mask = F.pad(loss_mask, (prompt_length - 1, 1), value=0)   # 左移一位对齐 logit
        （非 allgather_cp 时再 slice_with_cp 跟随 CP 切片）
    loss_masks = torch.cat(...) → pad → unsqueeze(0)
    assert loss_masks.shape == tokens.shape
    batch["full_loss_masks"] = loss_masks
```

**loss mask 对齐的数学**：设 `total_length = T`、`response_length = R`、`prompt_length = T - R`。原始 `loss_mask` 长度为 `R`（只标 response）。因果 LM 中位置 `i` 的 logit 预测位置 `i+1` 的 token，所以「监督第 `k` 个 response token」用的是位置 `prompt_length - 1 + k` 的 logit（因为第 0 个 response token 位于位置 `prompt_length`）。于是把 `R` 个 1 对齐到 logit 轴，需要左边补 `prompt_length - 1` 个 0、右边补 1 个 0（末位 logit 预测序列之外，不监督）。补完后长度恰为 `T`：

\[ (prompt\_length - 1) + R + 1 = (T - R - 1) + R + 1 = T \]

**zigzag CP 的切法**（`slice_with_cp`，cp_size=2 为例）：对长度 `L` 的序列，`chunk_size = ceil(L/4)`，补齐到 `4*chunk_size` 后分成 4 等段；rank 0 取**第 1 段 + 第 4 段**（头尾），rank 1 取**第 2 段 + 第 3 段**（中间两段）。每个 rank 持有约 `L/2` 个 token，分成「头块 + 尾块」两段。这种交错（而非朴素的前半/后半）是为了均衡注意力计算量：因果注意力中头部位置 attend 的少、尾部 attend 的多，头尾配对能让各 rank 算力相当。

#### 4.4.3 源码精读

`get_batch` 主体与 CP/THD 处理：

- [slime/backends/megatron_utils/data.py:28-63](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/data.py#L28-L63) —— 函数签名与 docstring，明确「生成一个 CP-ready 的微批」。`pad_size = tp_size * pad_multiplier`（L61），`unconcat_tokens` 保留原始序列（L64）。
- [slime/backends/megatron_utils/data.py:69-104](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/data.py#L69-L104) —— 两种 CP 模式：`allgather_cp` 全局拼接后 `chunk(cp_size)[cp_rank]`（L76-87）；否则逐条 `slice_with_cp`（L89），最后 `cu_seqlens * cp_size`（L104，把局部边界换算成 Megatron CP-THD 约定的全局单位）。
- [slime/backends/megatron_utils/data.py:106-118](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/data.py#L106-L118) —— 构造 `PackedSeqParams`（`qkv_format="thd"`），`tokens.unsqueeze(0)` 成 `[1, T_padded]`。

`slice_with_cp` 的 zigzag 切片实现：

- [slime/backends/megatron_utils/cp_utils.py:287-317](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/cp_utils.py#L287-L317) —— `chunk_size = ceil(token_len / (2*cp_size))`（L308），补齐后 rank r 取 `[r*chunk:(r+1)*chunk]` 与 `[(2*cp-r-1)*chunk:(2*cp-r)*chunk]` 两段拼接（L315-317）。`cp_size==1` 时直接原样返回（L304-305）。

loss mask 对齐：

- [slime/backends/megatron_utils/data.py:120-148](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/data.py#L120-L148) —— 核心两行：`prompt_length = total_length - response_length`（L128），`F.pad(loss_mask, (prompt_length - 1, 1), value=0)`（L130）做左移对齐；随后按 CP 模式跟随切片（L131-145），最后 `assert loss_masks.shape == tokens.shape`（L147）。

`loss_mask` 的源头（长度为何等于 `response_length`）：

- [slime/ray/rollout.py:748-760](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L748-L760) —— `_convert_samples_to_train_data` 里断言 `len(sample.loss_mask) == sample.response_length`，未提供时默认全 1（区分「模型生成的 token」（1）与「工具/环境注入的 token」（0），见 u3-l1）。

#### 4.4.4 代码实践

**实践目标**：手算一条样本的 loss mask 对齐，并手算 CP=2 的 zigzag 切片。

**操作步骤（loss mask）**：给定一条样本：prompt 3 个 token、response 4 个 token，即 `total_length=7`、`response_length=4`、`prompt_length=3`，原始 `loss_mask = [1,1,1,1]`。

1. 套用 `F.pad(loss_mask, (prompt_length - 1, 1), value=0)`，即 `F.pad([1,1,1,1], (2, 1), value=0)`。
2. 左补 2 个 0、右补 1 个 0，得到 `[0, 0, 1, 1, 1, 1, 0]`，长度 7。

**需要观察的现象**：对齐后的 mask 中，前 2 位（prompt 内部 logit）为 0、接下来 4 位（监督 4 个 response token 的 logit）为 1、末位（预测序列之外）为 0。位置 2 的 logit（即第 3 个 prompt token 产生的 logit）正好监督位置 3 的第一个 response token——这正是 next-token 预测的语义。

**操作步骤（CP=2 zigzag）**：给定一条 `L=100` 的序列，`cp_size=2`。

1. `chunk_size = ceil(100/4) = 25`，无需补齐（100 是 4 的倍数）。
2. rank 0 取 `tokens[0:25]`（第 1 段）与 `tokens[75:100]`（第 4 段），拼接成 50 个 token。
3. rank 1 取 `tokens[25:50]`（第 2 段）与 `tokens[50:75]`（第 3 段），拼接成 50 个 token。

**需要观察的现象**：每个 rank 持有 `L/cp_size = 50` 个 token，且各拿「头+尾」或「中间两段」——这就是 zigzag（Z 字形）交错，保证各 rank 注意力算力均衡。

**预期结果**：理解为何开 CP 后 `--max-tokens-per-gpu` 应设为约 `max_response_len // cp_size`（见 [arguments.py:752-753](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/utils/arguments.py#L752-L753)）——因为每个 rank 实际只处理原序列的 `1/cp_size`。

**待本地验证**：如要真机观察 `cu_seqlens` 的具体数值，需在多卡 + Megatron + CP=2 的环境运行；纯 CPU 无法初始化 `mpu` 的 CP 通信组。

#### 4.4.5 小练习与答案

**练习 1**：若 `prompt_length=1`（几乎没有 prompt，极端情况），`F.pad(loss_mask, (prompt_length - 1, 1))` 会变成什么？是否有隐患？

**答案**：`(prompt_length - 1)` = 0，即左补 0 个、右补 1 个，结果是 `[loss_mask..., 0]`，长度 = `response_length + 1 = total_length`。形状仍正确，但此时第 0 个位置（也是 prompt 唯一 token 的 logit）就直接监督第一个 response token。一般不会出问题，因为 `prompt_length` 至少为 1（必有 prompt）。

**练习 2**：`get_batch` 末尾有 `assert loss_masks.shape == tokens.shape`。如果这个断言失败，最可能的原因是什么？

**答案**：最可能是某条样本的 `loss_mask` 长度与 `response_length` 不一致（源头 `_convert_samples_to_train_data` 有对应断言保护，见 rollout.py L754-756），或 CP 切片/pad 步骤对 tokens 与 loss_masks 走了不一致的分支。两者必须在同一个 CP 模式（`allgather_cp` 开或关）下做完全对称的切片与补齐，断言正是用来兜住这种不对称。

**练习 3**：`allgather_cp`（DSA 模式）与默认 zigzag 模式，对 `tokens` 的切法最大区别是什么？

**答案**：zigzag 模式**逐条**序列切成「头块+尾块」再拼接（每条序列内部跨段），适合普通注意力模型；`allgather_cp` 先把**所有**序列全局拼接成一条流、统一补齐后再 `chunk(cp_size)` 等分，每个 rank 拿一整块连续流——这是为 DSA（DeepSeek-style Attention）等特定模型设计的，且仅支持特定模型结构（见 [arguments.py:20-39](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/arguments.py#L20-L39) 的校验）。

---

## 5. 综合实践

把本讲三个模块串起来。给定 `dp_size=2`、`cp_size=1`、`max_tokens_per_gpu=300`，3 条样本 `total_lengths = [100, 200, 300]`，`rollout_indices = [0, 1, 2]`（每条样本各属一个 rollout），`global_batch_size=3`（一个 step）。

**任务 A：算调度表**。

1. 装箱：`first_fit_pack([100,200,300], 300)` → `[[0,1],[2]]`，即 mbs0={样本0,1}（合计 300）、mbs1={样本2}（300）。`K=2`，已是 `dp_size=2` 的倍数，无需对齐拆分。
2. 分发（默认 strided round-robin，`dp_size=2`）：rank 0 拿 mbs0，rank 1 拿 mbs1。即 `partitions = [[0,1],[2]]`，`micro_batch_indices = [[[0,1]], [[0]]]`，`num_microbatches = [1]`。
3. 每个 rank 跑 1 个微批——满足 PP 同步不变量。

**任务 B：还原一个微批的张量**（以 rank 0 的 mbs0 为例，`cp_size=1`）。

1. `DataIterator.get_next(["tokens","loss_masks","total_lengths","response_lengths"])` 返回样本 0、1 的字段。
2. `get_batch` 把两条 token 序列 `cat` 成一条 `[1, T_padded]`（T=300，再按 `pad_size` 对齐补齐），并构造 `cu_seqlens = [0, len0, T_padded]`、`qkv_format="thd"` 的 `PackedSeqParams`——这样 FlashAttention 在样本 0、1 之间不会串台。
3. 对每条样本的 `loss_mask` 做 `(prompt_length-1, 1)` 左移对齐，cat 后与 tokens 同形。
4. CP=1 时 `slice_with_cp` 原样返回，无切分。

**任务 C：开启 CP=2 后**，rank 0 的 mbs0 里每条序列（如样本 0 的 100 token）会被 `slice_with_cp` 切成 `tokens[0:25]+tokens[75:100]`（25+25=50 个 token），另一 CP rank 拿中间两段。THD 的 `cu_seqlens` 也相应按 `*cp_size` 换算。此时 `max_tokens_per_gpu` 应下调到约 `max_response_len // 2`，否则每个 rank 仍按全长装箱会超 cap。

**交付物**：把 A、B、C 的结果写成一张表，列出「样本 → 所属 mbs → 所属 rank → 每条序列在各 CP rank 上的切片区间」。如果能用 `pytest tests/test_dp_schedule.py -m unit` 跑通并对照你手算的 `partitions`/`micro_batch_indices`，就算完成。

## 6. 本讲小结

- slime 的数据打包是**两阶段管线**：rollout 端用 `build_dp_schedule` 一次性算好全局调度表，训练端用 `DataIterator` + `get_batch` 逐微批还原张量；这种分离既保证了「所有 rank 微批数相同」的 PP 不变量，又让调度逻辑可在纯 CPU 单测。
- `build_dp_schedule` 走 **pack first, distribute second**：先 first-fit 装箱（或 FLOPs 均衡），再把微批数对齐到 `dp_size`（动态拆箱补足、静态不可整除则报错），最后按 strided round-robin 或 KK 均衡分发给各 rank；同一 rollout 的样本必留同一 step。
- `DataIterator` 极薄：只按 `micro_batch_indices` 的局部下标挑字段、维护一个 `offset` 游标，不做任何计算。
- `get_batch` 解决三件事：THD 打包（`cu_seqlens`/`PackedSeqParams` 防跨样本注意力）、CP 切片（zigzag 逐条切 或 `allgather_cp` 全局切）、loss mask 对齐（左补 `prompt_length-1`、右补 1，与 next-token 预测的 logit 轴对齐）。
- 开 CP 后每条序列约按 `1/cp_size` 分摊到各 rank，故 `--max-tokens-per-gpu` 应设为约 `max_response_len // cp_size`。

## 7. 下一步学习建议

本讲把「数据怎么进模型」讲透了，接下来建议：

1. **u4-l4（RL 损失与优势估计）**：`get_batch` 产出的 `full_loss_masks` 与 `tokens` 会进入 `loss.py` 的 `policy_loss_function` 与 `compute_advantages_and_returns`，看看 loss mask 如何参与 per-token / per-rollout 的归约，以及 `get_sum_of_sample_mean` 如何在 CP 下算出正确的样本均值。
2. **阅读 `cp_utils.py` 的 `get_sum_of_sample_mean`**：它会用 `slice_log_prob_with_cp` / `get_logits_and_tokens_offset_with_cp` 把 CP 切片后的 logprob 还原成完整 response 上的均值，是理解「CP 下损失正确性」的关键。
3. **对照 `tests/test_dp_schedule.py` 的全部用例**：尤其 `test_dynamic_oversized_sample_lands_alone`、`test_rollout_grouping_keeps_samples_together`、`test_trims_trailing_rollouts_that_dont_fill_a_step`，它们把本讲的不变量固化成了可执行断言，是最佳复习材料。
