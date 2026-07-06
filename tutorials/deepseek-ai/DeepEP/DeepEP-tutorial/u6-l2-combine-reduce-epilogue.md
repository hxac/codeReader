# Combine reduce epilogue 与 multiple reduction

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 DeepEP 的 combine 为什么被拆成「主 kernel + reduce epilogue」两个 GPU kernel，以及二者如何在不让 CPU 介入的情况下串接。
- 读懂 `combine_reduce_epilogue_impl` 内核：它如何从接收缓冲区里读出同一个 token 的多份副本、做加权归约、再写回 `combined_x`。
- 解释 `allow_multiple_reduction` 开关在「精度」与「传输量」之间的权衡，并能从缓冲区布局层面说出开关打开/关闭时布局维度的差别。
- 理解 0/1/2 个 bias 张量如何在 epilogue 里叠加到归约结果，以及 `combined_topk_weights` 是如何汇总输出的。

本讲只覆盖 **reduce epilogue** 与 **multiple reduction** 两个最小模块；combine 主 kernel 的反向路由细节已在 u6-l1 讲过，PTX/TMA 底层原语留待 u8-l1。

## 2. 前置知识

在进入本讲前，请确认你已经了解（这些都在前置讲义里建立过）：

- **combine 是 dispatch 的逆过程**：它不重新算路由，而是「重放」`EPHandle` 里 dispatch 写好的元数据（`recv_src_metadata`、`psum_num_recv_tokens_per_scaleup_rank` 等），把每个专家算出的输出按 `topk_weights` 加权送回原始 rank（见 u6-l1）。
- **两段式 kernel 拆分**：DeepEP 习惯把一次通信操作拆成「省 SM 的主 kernel」+「满 SM 的 epilogue」，二者靠 PDL（Programmatic Dependent Launch）在 GPU 侧串接（见 u5-l3 的 dispatch copy epilogue）。combine 沿用了同样的套路。
- **NCCL Gin 对称内存与 `get_sym_ptr`**：跨 rank 通信靠 NCCL Gin 后端暴露的对称窗口，每个 rank 都能用同一逻辑地址访问对端显存（见 u3-l4）。
- **`BufferLayout` / `TokenLayout`**：缓冲区被组织成「`num_ranks` × `num_max_tokens_per_rank`」的二维 token 阵列，每个 token 内部按 `[hidden | SF | metadata | mbarrier]` 分段对齐（见 u3-l2）。

一个一句话回顾：**combine 主 kernel 负责把数据搬到对端 buffer，reduce epilogue 负责把对端 buffer 里收到的一堆副本归约成最终输出 `combined_x`**。本讲聚焦后半句。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [csrc/kernels/elastic/combine.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/combine.hpp) | JIT 启动器：`launch_combine`（主 kernel）与 `launch_combine_reduce_epilogue`（归约 epilogue）的 host 代码、`CombineReduceEpilogueRuntime` 的模板实例化生成器。 |
| [deep_ep/include/deep_ep/impls/combine_reduce_epilogue.cuh](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine_reduce_epilogue.cuh) | reduce epilogue 的真正 GPU 内核 `combine_reduce_epilogue_impl`：读 buffer、去重、归约、写 `combined_x` 与权重。 |
| [deep_ep/include/deep_ep/impls/combine_utils.cuh](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine_utils.cuh) | 归约工具：`use_rank_layout`、`get_num_tokens_in_layout`、`compute_topk_slots`、以及核心的 `combine_reduce` 模板函数。 |
| [deep_ep/include/deep_ep/impls/combine.cuh](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine.cuh) | combine 主内核 `combine_impl`，本讲只引用它「源端局部归约」的分支，用于解释 multiple reduction 的第一级归约发生在哪里。 |
| [csrc/elastic/buffer.hpp](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp) | `ElasticBuffer::combine` 的 host 实现：bias 打包、依次调用主 kernel 与 epilogue、`get_combine_buffer_size` 的布局尺寸计算。 |
| [tests/elastic/test_ep.py](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py) | 端到端测试，含 `--allow-multiple-reduction` 开关与 bias 数量枚举，是本讲代码实践的入口。 |

调用方向（从上到下）：

```
ElasticBuffer.combine (buffer.hpp, host)
        │
        ├── launch_combine(...)            → combine_impl / hybrid_combine_impl  (主 kernel，推数据)
        │       └── 返回 reduce_buffer 指针
        │
        └── launch_combine_reduce_epilogue(...) → combine_reduce_epilogue_impl  (epilogue，归约)
```

## 4. 核心概念与源码讲解

### 4.1 Reduce epilogue 在 combine 链路中的位置：两段式架构

#### 4.1.1 概念说明

combine 要完成两件性质完全不同的事：

1. **把专家输出推回原始 rank**：这是一次跨 rank 的 all-to-all 通信，瓶颈在 NVLink/RDMA 带宽，理想做法是**用尽量少的 SM** 去跑（把 SM 让给旁边的计算内核，见 u2-l4 的通信-计算重叠）。
2. **把同一个 token 收到的多份副本加权归约成一行输出**：这是一次纯本地的逐元素累加，瓶颈在显存带宽与算力，理想做法是**用尽量多的 SM** 去跑。

这两个目标互相冲突——一个想「省 SM」，一个想「满 SM」。DeepEP 的解法和 dispatch 完全一样（见 u5-l3）：**拆成两个独立的 kernel**。主 kernel `combine_impl` 只负责搬运，reduce epilogue `combine_reduce_epilogue_impl` 只负责归约。

#### 4.1.2 核心流程

host 侧（`ElasticBuffer::combine`）严格按顺序做三步：

1. 调 `launch_combine(...)`：主 kernel 把每个 rank 的专家输出写到对端 buffer，函数**返回 `reduce_buffer` 指针**——即「本 rank 这一次需要被归约的那段 buffer 起始地址」。
2. 分配输出张量 `combined_x`（以及可选的 `combined_topk_weights`）。
3. 调 `launch_combine_reduce_epilogue(...)`：epilogue 从 `reduce_buffer` 读、归约、写到 `combined_x`。

两个 kernel 之间**没有 CPU 同步**，靠 PDL 串接：epilogue 内核一开头就 `cudaGridDependencySynchronize()`，意思是「等上一个 kernel（主 kernel）写到 buffer 的数据全部对我可见」。这是 Hopper 引入的 grid 间依赖机制，省掉了一次 CPU launch + event 开销。

#### 4.1.3 源码精读

先看 host 侧如何把两个 kernel 串起来（注意返回值 `reduce_buffer` 被直接喂给 epilogue）：

[launch_combine 末尾返回 reduce_buffer：csrc/kernels/elastic/combine.hpp:181-192](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/combine.hpp#L181-L192) — 直接模式返回整段 `buffer`；hybrid 模式需要跳过 scaleup 段，用 `BufferLayout` 算出 scaleup buffer 的末尾指针作为真正的 reduce 起点。注释里 `is_scaleup_buffer_rank_layout` 正是 multiple reduction 决定的布局维度（见 4.3）。

[ElasticBuffer::combine 依次调用主 kernel 与 epilogue：csrc/elastic/buffer.hpp:1285-1329](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L1285-L1329) — 先 `launch_combine(...)` 拿到 `reduce_buffer`，分配输出张量，再把 `reduce_buffer` 传给 `launch_combine_reduce_epilogue`。中间的 `stream_control_before_epilogue` 处理可选的「计算先跑、epilogue 等它」的事件依赖（见 u2-l4）。

epilogue 的启动配置里第六个参数 `true` 就是 PDL 开关：

[launch_combine_reduce_epilogue 的 LaunchArgs：csrc/kernels/elastic/combine.hpp:282](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/combine.hpp#L282) — `LaunchArgs(num_sms, num_threads, num_smem_bytes, /*cluster_dim=*/1, /*cooperative=*/false, /*pdl_enabled=*/true)`。对比主 kernel 的 `LaunchArgs(..., 2 - (num_sms % 2), true)`（[combine.hpp:174](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/combine.hpp#L174)）：主 kernel 用 cluster + cooperative launch 做 grid 级同步，epilogue 则开 PDL 以便和主 kernel 衔接、并铺满所有 SM。

epilogue 内核第一件事就是等主 kernel 的数据：

[PDL 同步屏障：deep_ep/include/deep_ep/impls/combine_reduce_epilogue.cuh:57-59](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine_reduce_epilogue.cuh#L57-L59) — `cudaGridDependencySynchronize()` 阻塞到主 kernel 写入可见；注释特意提醒「PDL is used, please do not use `__ldg`」，因为 PDL 路径下的可见性保证与普通 `__ldg` 缓存语义不完全相同。

> 关于 `num_warps`：epilogue 用「最大化共享内存」策略决定 warp 数——`num_warps = min(num_smem_bytes / token_bytes, 32)`（[combine.hpp:264](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/kernels/elastic/combine.hpp#L264)）。每个 warp 在共享内存里有一个独立的 TMA 暂存区（`tma_buffer`），用来收集中间归约结果。

#### 4.1.4 代码实践

**实践目标**：直观看到 combine 被拆成两个 kernel，并确认它们是 PDL 串联、而非 CPU 事件串联。

**操作步骤**：

1. 设置调试变量观察内核名：`export EP_JIT_DEBUG=1`。
2. 在单机 8 卡上跑一次 combine（命令见 4.3.4），在日志里应能看到 `combine` 与 `combine_reduce_epilogue` 两个不同的内核被 JIT 编译并启动。
3. 对照本节源码，确认 `combine_reduce_epilogue_impl` 的第一行有效操作是 `cudaGridDependencySynchronize()`，而 host 侧两次 launch 之间没有 `cudaStreamSynchronize` 或 `event.wait`。

**需要观察的现象**：日志里出现两个内核名；host 调用顺序是「主 kernel → 分配张量 → epilogue」三步连续下发到同一条流上。

**预期结果**：两次 launch 之间无 CPU 阻塞，epilogue 靠 PDL 等主 kernel。若无法在本地运行，明确记为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 epilogue 不直接复用主 kernel 的输出，而要主 kernel 先写到 buffer、epilogue 再读？

**参考答案**：主 kernel 的首要目标是「省 SM 做通信」，它的输出是按通信友好的方式（按源 rank 或 topk 槽）排布在 buffer 里的多份副本，而不是用户想要的 `[num_combined_tokens, hidden]`。归约需要满 SM 与算力，目标与主 kernel 冲突，因此拆开：主 kernel 只管搬，epilogue 只管算与重排。

**练习 2**：如果把 epilogue 的 `pdl_enabled` 改成 `false` 会发生什么？

**参考答案**：`cudaGridDependencySynchronize()` 失去意义（没有上游 grid 依赖可等），epilogue 可能在主 kernel 还没把数据写完时就开读，读到不完整或未对齐可见的数据。要补救就必须在 host 侧插一个 event/synchronize，退化回 CPU 串联，增加延迟。

---

### 4.2 归约核心 combine_reduce：去重、累加与 TMA 写回

#### 4.2.1 概念说明

reduce epilogue 的核心任务是：**对每一个输出 token，从 `reduce_buffer` 里读出它的若干份副本，加权累加成一行 `combined_x`**。

为什么一个 token 会有多份副本？回想 MoE：一个 token 被 top-k（比如 k=6）个专家处理，这 6 个专家可能分散在不同 rank 上。combine 时这 6 份输出都要送回原始 rank，于是 `reduce_buffer` 里就攒了最多 k 份（或按 rank 去重后的若干份）同一个 token 的数据。epilogue 要把它们加起来。

这里的关键技巧是 **warp 内去重（deduplicate）**：32 个 lane 各自持有一个 top-k 选择（`lane_idx < kNumTopk`），但其中可能有多个 lane 指向**同一个源 rank**。直接让每个 lane 都去读一份会重复读、重复加。去重保证「同一个源 rank 只有一个 lane（master lane）真正去读和累加」。

#### 4.2.2 核心流程

epilogue 内核对每个 `token_idx`（由 SM 与 warp 协同跨步覆盖 `num_combined_tokens`）做：

1. **预处理索引**：每个 lane 从 `combined_topk_idx` 读出自己的 top-k 专家号，换算成「该专家所在的源 rank 号」`stored_dst_rank_idx`。
2. **去重**：用 `ptx::match` + `get_master_lane_idx` 选出每个 rank 的 master lane，构造 `reduce_valid_mask`（每个有效且唯一的源 rank 对应一个 bit）。
3. **算槽位**：`compute_topk_slots` 把 mask 里的有效 bit 压到数组 `topk_slot_idx[]` 前面，得到「需要读哪几个源 rank 的 buffer 槽」。
4. **归约**：调 `combine_reduce`，循环遍历 `topk_slot_idx`，从 `comm_buffer.get_rank_buffer(slot).get_token_buffer(token_idx)` 读出每份副本，累加进共享内存的 `tma_buffer`（这一步同时叠加 bias，见 4.4）。
5. **写回**：`elect_one_sync` 选出 warp 中唯一的 lane，用 `tma_store_1d` 把 `tma_buffer` 一次写到 `combined_x[token_idx]`。

伪代码：

```
for token_idx in (warp 跨步覆盖 num_combined_tokens):
    # 1. 每个 lane 算自己的源 rank
    stored_dst_rank_idx[lane] = expert_to_rank(combined_topk_idx[token_idx, lane])

    # 2. 同 rank 去重，留下每个 rank 的 master lane
    mask = deduplicate(stored_dst_rank_idx) and (stored_dst_rank_idx >= 0)

    # 3. 把有效源 rank 压成连续数组
    topk_slot_idx[] = compute_topk_slots(mask)

    # 4. 把这些源 rank 的副本累加进 smem（含 bias）
    combine_reduce(tma_buffer, 读多份副本, bias_0, bias_1)

    # 5. TMA 一次写回 combined_x[token_idx]
    tma_store_1d(output_buffer[token_idx], tma_buffer)
```

#### 4.2.3 源码精读

主循环与索引预处理（每个 lane 算源 rank 号）：

[读 top-k 并换算源 rank：combine_reduce_epilogue.cuh:62-71](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine_reduce_epilogue.cuh#L62-L71) — `global_warp_idx = warp_idx * kNumSMs + sm_idx` 的跨步方式刻意「先跨 SM 分配任务」，保证最后一波（last wave）均匀落在每个 SM 上，避免拖尾。`stored_dst_rank_idx` 由专家号除以「每 rank 专家数」得到，-1 表示该 top-k 槽无效（被 mask 掉的专家）。

去重与槽位计算（本模块最精巧的一段）：

[去重键三分支 + compute_topk_slots：combine_reduce_epilogue.cuh:74-95](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine_reduce_epilogue.cuh#L74-L95) — `should_deduplicate` 与 `deduplicate_key` 共同决定「按什么去重」，4.3 会专门讲这三个分支。`ptx::deduplicate(key, lane_idx)` 的实现是 `get_master_lane_idx(match(key)) == lane_idx`（[ptx.cuh:410-412](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/ptx.cuh#L410-L412)）：`match` 用 `__match_any_sync` 找出所有 key 相同的 lane，`get_master_lane_idx` 取其中最大 lane 号作 master，只有 master lane 的 `deduplicate` 返回 true，于是同一个源 rank 只剩一个 lane 去读。

> `compute_topk_slots`（[combine_utils.cuh:41-53](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine_utils.cuh#L41-L53)）用 `__ffs(mask)-1` 反复抽出最低有效 bit，把稀疏的 32-bit mask 压成紧凑的 `topk_slot_idx[]` 数组，-1 填充无效位。

核心归约函数 `combine_reduce` 的两条路径：

[combine_reduce 主体：combine_utils.cuh:55-169](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine_utils.cuh#L55-L169) — 分「快路径 `enable_hadd_bypass`」与「慢路径」。快路径成立条件是 `bias 全空 且 有效 top-k ≤ 2`（[combine_utils.cuh:68-70](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine_utils.cuh#L68-L70)）：此时最多加两份，可以直接用 BF16 原生加法（`__hadd2_rn`，注释说「casting is slow」），不必升到 FP32。慢路径则用 FP32 累加器 `float2 reduced[]`，因为要叠加 bias、且加的份数可能多于 2，FP32 能避免 BF16 多次相加的精度损失。

读源副本用的是带谓词的加载：

[ldg_with_gez_pred：ptx.cuh:167-178](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/ptx.cuh#L167-L178) — 内联 PTX 用 `setp.ge.s32` 判断 `value >= 0`，只有 slot 合法时才真正发起 `ld.global.nc.v4.s32`（否则返回全零），避免对 -1 槽位的越界读。`.nc`（non-coherent / read-only）配合 `L1::no_allocate` 是只读流的缓存策略。

累加原语把 BF16 直接喂进 FP32 累加器：

[accumulate：ptx.cuh:436-445](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/common/ptx.cuh#L436-L445) — SM100+ 用 `add.rn.f32.bf16` 一条指令完成「BF16 → FP32 + 加」，省掉显式类型转换；旧架构退化为 `__bfloat1622float2` 后相加。

最后用 TMA 把共享内存里的结果一次写回全局：

[TMA 写回 combined_x：combine_reduce_epilogue.cuh:119-124](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine_reduce_epilogue.cuh#L119-L124) — `elect_one_sync` 选 warp 中一个 lane 发起 `tma_store_1d`，把整个 hidden 维度从 `tma_buffer` 拷到 `output_buffer[token_idx]`，`tma_store_commit` 提交。

#### 4.2.4 代码实践

**实践目标**：理解 `enable_hadd_bypass` 快路径的触发条件，以及它为何能省掉 FP32 转换。

**操作步骤**：

1. 打开 [combine_utils.cuh:68-110](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine_utils.cuh#L68-L110)，找到 `enable_hadd_bypass` 的两个条件。
2. 跟踪 `topk_slot_idx[2] < 0` 这个判断：它说明当只有 1~2 个有效源 rank 时，加法次数 ≤ 1，BF16 一次相加的精度足够，不需要 FP32 累加器。
3. 思考：为什么带 bias 时（即便只有 1 个源 rank）也强制走慢路径？

**需要观察的现象**：快路径只读 `slot_0` 与 `slot_1` 两个源，慢路径用循环读 `kNumValidTopk` 个源。

**预期结果**：快路径用 `nv_bfloat162` 原生 `+=`，慢路径用 `float2 reduced[]` 累加后再 `__float22bfloat162_rn` 回写。带 bias 时多了一次 bias 与结果的相加，FP32 累加器能避免「BF16 + BF16 + BF16」的中间舍入误差，所以即便源很少也走慢路径。

#### 4.2.5 小练习与答案

**练习 1**：`compute_topk_slots` 里为什么对 `lowest_idx >= 0` 还要 `fetched`，而不是直接存 `lowest_idx`？

**参考答案**：`lowest_idx` 是「warp 内第几个有效 lane」，而真正要读的 buffer 槽号是「该 lane 持有的源 rank/topk 号」。`fetch_func` 把 lane 序号换算成真正的槽号（direct 模式可能还要做 `ptx::exchange` 把 master lane 的 rank 广播给同组 lane，见 [combine_reduce_epilogue.cuh:92-94](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine_reduce_epilogue.cuh#L92-L94)）。无效位（mask 耗尽）才存 -1。

**练习 2**：归约时为什么把中间结果先放在共享内存的 `tma_buffer`，而不是直接写到 `combined_x`？

**参考答案**：归约要跨多份副本累加，每份是一个 hidden 向量；先在共享内存里累加好，最后用一次 TMA store 写回全局，比「每加一份就写一次全局内存」省得多——全局内存写是归约里最贵的操作，TMA 又能用一条指令搬整个 hidden 段。

---

### 4.3 Multiple reduction：两级归约与缓冲区布局

#### 4.3.1 概念说明

`allow_multiple_reduction` 是 `ElasticBuffer` 构造时的一个开关（见 u2-l2），它回答的问题是：**为了把一个 token 的多份专家输出合并，我们是「全部传回来再一次归约」，还是「先在源端按 rank 分组局部归约、再传回来归约」？**

两种策略各有取舍：

| 策略 | 开关值 | 传输量 | 归约次数（精度） |
| --- | --- | --- | --- |
| 单次归约（single） | `allow_multiple_reduction=False` | 大：每份 topk 独立传输 | 1 次，最精确 |
| 多次归约（multiple） | `allow_multiple_reduction=True` | 小：同 rank 先合并 | ≥2 次，多一次舍入 |

直觉上：设一个 token 被分给了 \(k\) 个专家，这 \(k\) 个专家散布在 \(R\) 个 rank 上（\(R \le k\)）。

- 单次归约：\(k\) 份输出全部传回原 rank，原 rank 一次性加 \(k\) 个数。
- 多次归约：每个源 rank 先把自己内部那几份加好（局部归约），只传 \(R\) 份部分和回原 rank，原 rank 再加 \(R\) 个数。

所以多次归约把每 token 的传输份数从 \(k\) 降到 \(\min(R, k)\)，代价是多了一次（局部）舍入。在 hybrid 多节点场景下更激进：**先用便宜的 NVLink（scaleup）在节点内归约，再用昂贵的 RDMA（scaleout）只传节点级的部分和**——这就是 topic 里说的「先 scaleup 内归约再全局」。

为什么开关默认是 `True`？因为 MoE 里 \(k\) 通常不大（如 6），而 rank 数可能很多，省下的跨节点带宽远比一次 BF16 舍入值钱。

#### 4.3.2 核心流程

这个开关在三个地方生效，层层相连：

1. **缓冲区布局维度**（`get_combine_buffer_size`）：开关打开时，接收缓冲区按「源 rank」分槽，维度是 \(\min(R, k)\) 而非 \(k\)；关闭时按「topk 槽」分槽，维度是 \(k\)。
2. **主 kernel 的源端局部归约**（`combine_impl`）：开关打开时，主 kernel 在**发送之前**就把同 rank 的多份 topk 加好再发（少传）；关闭时每份 topk 单独发。
3. **epilogue 的去重键**（`combine_reduce_epilogue_impl`）：决定「按什么去重」——是按完整 rank 还是按 scale-rank。

两个编译期辅助函数把开关翻译成布局维度：

[use_rank_layout 与 get_num_tokens_in_layout：combine_utils.cuh:8-18](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine_utils.cuh#L8-L18) —
- `use_rank_layout = allow_multiple_reduction && (num_ranks <= num_topk)`：只有「允许多次归约」且「rank 数不超过 topk」时，才按 rank 排布（否则 rank 太多、按 rank 排布反而比按 topk 还大，得不偿失）。
- `get_num_tokens_in_layout = use_rank_layout ? num_ranks : num_topk`：每个 token 区段里要预留几份副本的槽。

#### 4.3.3 源码精读

**(a) 缓冲区布局尺寸**

[get_combine_buffer_size：csrc/elastic/buffer.hpp:616-650](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L616-L650) — 直接模式的接收布局维度 `num_tokens_in_layout` 与 hybrid 模式的 `num_tokens_in_scaleup_layout`/`num_tokens_in_scaleout_layout`，都随开关变化（[buffer.hpp:625](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L625)、[buffer.hpp:636-637](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L636-L637)）。关掉开关时，send buffer 还要按 `num_topk` 放大（[buffer.hpp:630](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L630) 注释明确说「按 do_expand=True 的最坏情况算」）。

**(b) 主 kernel 的源端局部归约**

[combine_impl 的三种情况分支：deep_ep/include/deep_ep/impls/combine.cuh:121-144](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine.cuh#L121-L144) — 这是 multiple reduction「第一级归约」发生的地方。关键判断：

```
no_local_reduce = (非 expand) 或 (multiple_reduction 且 该 token 只有 1 个有效 topk)
```

- 若 `no_local_reduce`：不需要源端归约，直接 TMA load + store 一份。
- 若 `kAllowMultipleReduction` 且需要归约：进入 [combine.cuh:144](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine.cuh#L144) 起的 local reduction 分支，**复用本讲的 `combine_reduce` 工具函数**在源端先把同 rank 的几份加好，再把单个部分和发出去。
- 否则（expand 且关掉 multiple reduction）：每份 topk 单独发送。

注意主 kernel 与 epilogue 共享同一个 `combine_reduce`——一个在源端做「局部预归约」，一个在接收端做「最终归约」。

**(c) epilogue 的去重键三分支**

[去重键选择：combine_reduce_epilogue.cuh:74-88](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine_reduce_epilogue.cuh#L74-L88) — 这三分支把开关、布局、拓扑耦合在一起：

| 条件 | 去重键 | 含义 |
| --- | --- | --- |
| `kUseExpandedLayout && !kAllowMultipleReduction` | 不去重 | expand + 单次归约：每份 topk 都是独立的、从未被归约过，逐份读即可 |
| hybrid + 非 expand + 非 multiple | 按完整 rank（`expert / kNumExpertsPerRank`） | 数据来自完整 rank，按 rank 去重 |
| 其它（含 direct、含 hybrid+multiple） | 按 `stored_dst_rank_idx` | direct 下就是 rank；hybrid+multiple 下是「scale-rank」（已被源端预归约的那个维度） |

第三行的 `stored_dst_rank_idx` 在 hybrid+multiple 下其实指向 scaleout 维——因为节点内已经在 scaleup 阶段归约过了，epilogue 只需再跨 scaleout rank 归约。这正是「先 scaleup 内归约再全局」在 epilogue 侧的体现。

> 关键不变式：**去重键必须与缓冲区实际排布维度一致**。`use_rank_layout` 为真时 buffer 按 rank 排（`comm_buffer.get_rank_buffer(slot_idx)` 的 slot 就是 rank 号），去重键也得是 rank；否则 buffer 按 topk 排，去重键用 lane 序号。`combine_reduce_epilogue.cuh:103-105` 的 `get_rank_buffer(slot_idx)` 与 [combine_reduce_epilogue.cuh:133-134](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine_reduce_epilogue.cuh#L133-L134) 的 `kUseRankLayout ? stored_dst_rank_idx : master_lane_idx` 共同维持这一不变式。

#### 4.3.4 代码实践

**实践目标**：用 `--allow-multiple-reduction 0/1` 分别跑 combine，对照代码体会传输量与归约次数的权衡，并验证两种模式都正确（与 NCCL 参考实现逐位一致）。

**操作步骤**：

1. 单机 8 卡跑两次（开关分别为 0 和 1）：

   ```bash
   # 单次归约（更精确、传输更多）
   torchrun --nproc_per_node=8 tests/elastic/test_ep.py --allow-multiple-reduction 0

   # 多次归约（节点内先归约、传输更少）
   torchrun --nproc_per_node=8 tests/elastic/test_ep.py --allow-multiple-reduction 1
   ```

   （`test_ep.py` 默认 `--allow-multiple-reduction 1`，见 [test_ep.py:585](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L585)。若无可用的 8 卡 Hopper 环境，记为「待本地验证」。）

2. 关注测试里 `combine_recipe` 与 `reduced_combine_recipe` 的取值：[test_ep.py:114-124](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L114-L124)。开关为 1 且 hybrid 多节点时，参考实现用 `(True, True)`（两级归约：先 scaleup 内、再全局）；否则用 `(True, False)`（仅 rank 内归约后全局）。开关为 0 时参考实现 `(False, False)` 表示完全单次归约。
3. 对照 [buffer.hpp:616-650](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L616-L650) 解释：开关从 1 变 0 时，接收布局维度从 `min(num_ranks, num_topk)` 变成 `num_topk`，send buffer 还要多乘一个 `num_topk` 因子——这就是「传输更多」的布局证据。

**需要观察的现象**：两次运行都应通过 `torch.equal(combined_x, ref_combined_y)` 的逐位正确性断言；开关为 0 时（若跑性能测试）combine 涉及的字节数更大。

**预期结果**：两种模式数值都正确（差别在舍入路径，参考实现已经分别匹配）。开关为 0 的缓冲区布局更大、传输份数更多；开关为 1 在多节点下用两级归约省跨节点带宽。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `use_rank_layout` 还要附加 `num_ranks <= num_topk` 这个条件，而不是只要开关打开就按 rank 排？

**参考答案**：按 rank 排布需要 `num_ranks` 个槽，按 topk 排布需要 `num_topk` 个槽。如果 `num_ranks > num_topk`（rank 比 topk 还多），按 rank 排反而比按 topk 排占更多缓冲区、传更多数据，完全失去了 multiple reduction 省 流量的意义。所以只有 `num_ranks <= num_topk` 时才值得按 rank 排，否则退回按 topk 排。

**练习 2**：在单机 8 卡（`num_scaleout_ranks == 1`）上，开关 0/1 的差别大吗？为什么？

**参考答案**：差别较小但仍存在。单机没有跨节点 RDMA，省流量的收益主要来自「同 rank 的多份 topk 合并」。由于 `num_ranks=8` 通常大于典型 `num_topk=6`，`use_rank_layout` 为假，布局仍按 topk 排，主 kernel 的源端局部归约也不会触发大规模合并；真正的大收益出现在多节点 hybrid 场景，那里 scaleup 阶段能把节点内多份归约成一份，再省下昂贵的 scaleout 传输。

---

### 4.4 bias 叠加与 topk_weights 输出

#### 4.4.1 概念说明

reduce epilogue 除了归约，还承担两件「顺带」的事：

1. **叠加 bias**：调用方可传入 0、1 或 2 个形状为 `[num_combined_tokens, hidden]` 的 BF16 bias 张量，epilogue 在归约结果上加上它们（典型用途是 MoE 专家里的 final bias，或两段式 bias 的拆分）。`combine` 接口用 `bias` 参数接受单个张量或一个二元组（见 [elastic.py:1036-1044](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/buffers/elastic.py#L1036-L1044) 的 `_unpack_bias`）。
2. **输出 `combined_topk_weights`**：归约后的权重。每份副本自带一份 `topk_weights`，epilogue 从中读出对应源 rank 的那份，写到 `[num_combined_tokens, num_topk]`。

#### 4.4.2 核心流程

bias 流程：

```
host: bias (Tensor 或 2元组) → _unpack_bias → bias_0, bias_1 (可选指针)
       ↓ 断言形状 == [num_combined_tokens, hidden] 且为 BF16
epilogue: combine_reduce(..., bias_0, bias_1)
       → 若任一 bias 非空，走 FP32 累加路径：先 add_bias(bias_0)，再 add_bias(bias_1)，再归约多份副本
```

权重输出流程：

```
epilogue: 对每个 token，每个 lane (< num_topk) 持有一个源 rank
       → 用 match(stored_dst_rank_idx) 找到同 rank 的 master lane
       → 从 comm_buffer 的 master rank 槽读出该 lane 对应的 weight
       → 写到 combined_topk_weights[token_idx, lane]
```

#### 4.4.3 源码精读

host 侧 bias 打包与形状校验：

[bias 打包与断言：csrc/elastic/buffer.hpp:1237-1247](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L1237-L1247) — 把两个 `std::optional<Tensor>` 装进 `bias_ptrs[2]`，没有就是 `nullptr`。每个 bias 都断言 `dim()==2`、`BF16`、`size(0)==num_combined_tokens`、`size(1)==hidden`。

bias 在 `combine_reduce` 里叠加：

[bias 累加路径：combine_utils.cuh:111-131](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine_utils.cuh#L111-L131) — 进入慢路径后，先用 `add_bias` lambda 把 bias_0、bias_1 读进同一个 FP32 累加器 `reduced[]`（[combine_utils.cuh:129-130](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine_utils.cuh#L129-L130) 的三元判断 `bias != nullptr ? add_bias(...) : void()`），再继续累加多份副本。这就是「带 bias 强制走慢路径」的原因——需要统一的 FP32 累加器把 bias 与多份副本加在一起。

epilogue 把 bias 指针传进 `combine_reduce`：

[combine_reduce 调用：combine_reduce_epilogue.cuh:111-114](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine_reduce_epilogue.cuh#L111-L114) — `bias_0 == nullptr ? nullptr : ...` 在内核里再次判空，把对应的 buffer 指针传下去。

权重输出（注意它从 buffer 读、而非从输入 `topk_weights` 读）：

[combined_topk_weights 写回：combine_reduce_epilogue.cuh:128-141](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine_reduce_epilogue.cuh#L128-L141) — `master_lane_idx = get_master_lane_idx(match(stored_dst_rank_idx))`：用 `match` 找出与当前 lane 同源 rank 的所有 lane，取 master；然后从 `comm_buffer` 的对应槽（`kUseRankLayout ? stored_dst_rank_idx : master_lane_idx`）读出该 token 在该 rank 的 `topk_weights`。无效 lane（`stored_dst_rank_idx < 0`）写 0。这一步保证了「无论该 token 被哪几个 rank 处理过，每个 topk 槽都能拿到正确的权重」。

#### 4.4.4 代码实践

**实践目标**：验证 epilogue 能正确处理 0/1/2 个 bias，并理解 bias 数量对归约路径的影响。

**操作步骤**：

1. 看 `test_ep.py` 如何枚举 bias 数量：[test_ep.py:26](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L26) 的 `for num_bias in (0, 1, 2)`，以及 [test_ep.py:96-99](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L96-L99) 构造单个或两个随机 bias 张量。
2. 跑一次测试，确认三种 `num_bias` 都通过 `torch.equal(combined_x, ref_combined_y)`（参考实现 `ref_combine` 也接受同样的 bias，见 [test_ep.py:129-140](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/tests/elastic/test_ep.py#L129-L140)）。
3. 对照 [combine_utils.cuh:68-70](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine_utils.cuh#L68-L70) 回答：bias 数量为 1 或 2 时，`enable_hadd_bypass` 还可能成立吗？

**需要观察的现象**：三种 bias 数量下数值都正确。

**预期结果**：只要任意一个 bias 非空，`enable_hadd_bypass` 的第一个条件 `(bias_0 == nullptr and bias_1 == nullptr)` 就为假，必然走 FP32 慢路径。所以 num_bias=0 才有机会走快路径（且还需有效 topk ≤ 2）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 bias 要在「读多份副本之前」就加进累加器，而不是先归约完再加 bias？

**参考答案**：顺序在数学上可交换，但放进同一个 FP32 累加器一起加，比「BF16 归约 → 写 smem → 再读出来加 bias → 再写」省一次往返。更重要的是精度：bias 是 BF16，归约结果也接近 BF16 量级，全部在 FP32 累加器里相加后再一次性转回 BF16，舍入误差最小。

**练习 2**：`combined_topk_weights` 为什么用 `match` 找 master lane，而不是每个 lane 直接读自己那份？

**参考答案**：因为 buffer 是按 rank（或按去重后的 scale-rank）排布的，同一个源 rank 的多个 topk 槽共用同一份 buffer 数据，只有 master lane 知道该 rank 的 buffer 槽号。普通 lane 需要通过 `match` + master lane 的协调才能定位到正确的源数据。

---

## 5. 综合实践

把本讲知识串起来：**手动追踪一个 token 在 combine 全流程里的「份数」变化，并解释 `allow_multiple_reduction` 如何改变这条路径。**

任务步骤：

1. 设定场景：`num_topk=6`，`num_experts=256`，单机 8 卡（`num_ranks=8`，`num_scaleout_ranks=1`）。某个 token 被路由到 6 个专家，假设它们分别落在 rank `[0, 0, 1, 2, 2, 3]`（即 rank0 有 2 个、rank2 有 2 个、rank1/rank3 各 1 个）。
2. **关闭 multiple reduction（`--allow-multiple-reduction 0`）**：
   - 对照 [combine.cuh:121-144](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine.cuh#L121-L144)：主 kernel 走哪个分支？每个 rank 各发几份？总共几份到原 rank？
   - 对照 [combine_reduce_epilogue.cuh:74-88](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine_reduce_epilogue.cuh#L74-L88)：epilogue 去重键是哪个分支？最终加几次？
3. **打开 multiple reduction（`--allow-multiple-reduction 1`）**：
   - 主 kernel 是否触发源端 local reduction（[combine.cuh:144](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine.cuh#L144)）？rank0 的 2 份、rank2 的 2 份是否先在源端相加？
   - 此时缓冲区布局维度是多少（用 [combine_utils.cuh:8-18](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine_utils.cuh#L8-L18) 与 [buffer.hpp:625](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/csrc/elastic/buffer.hpp#L625) 算）？注意 `num_ranks=8 > num_topk=6`，`use_rank_layout` 是真还是假？
4. 用一张表总结两种模式下「传输份数 / 归约次数 / 布局维度」的差别。
5. （可选）跑 `torchrun --nproc_per_node=8 tests/elastic/test_ep.py --allow-multiple-reduction 0` 与 `=1`，确认两种模式都通过正确性断言；若没有 8 卡环境，记为「待本地验证」并只做静态分析。

**参考结论**：关闭时主 kernel 每份 topk 单独发（共 6 份），epilogue 按完整 rank 去重后实际读 4 个源 rank（0,1,2,3）、单次归约；打开时由于 `num_ranks(8) > num_topk(6)`，`use_rank_layout` 为假，布局仍按 topk 排（6 槽），但主 kernel 会在源端把同 rank 的多份（rank0 的 2 份、rank2 的 2 份）预归约后再发，从而减少实际传输的数据量，epilogue 再做最终归约（多次归约、多一次舍入）。

## 6. 本讲小结

- combine 被拆成「省 SM 的主 kernel」+「满 SM 的 reduce epilogue」两个 GPU kernel，host 依次下发、靠 PDL（`cudaGridDependencySynchronize`）在 GPU 侧串接，无需 CPU 同步。
- reduce epilogue 的核心 `combine_reduce` 对每个 token 从接收缓冲区读出多份副本、加权累加：先用 `match`/`deduplicate` 按 rank 去重（每个源 rank 只一个 master lane 读），再用 `compute_topk_slots` 压成紧凑槽位数组，FP32 累加后用 TMA 一次写回 `combined_x`。
- `enable_hadd_bypass` 是无 bias 且有效 topk ≤ 2 时的 BF16 快路径，省掉 FP32 转换；其余情况走 FP32 累加器慢路径。
- `allow_multiple_reduction` 控制「单次精确归约（传输多）」vs「多次归约（先在源端/节点内归约、传输少、多一次舍入）」的权衡；它通过 `use_rank_layout`/`get_num_tokens_in_layout` 决定缓冲区维度，通过主 kernel 的 `no_local_reduce` 分支决定是否源端预归约，通过 epilogue 的去重键三分支决定按什么合并。
- 0/1/2 个 bias 在 host 侧打包成两个可选指针，在 `combine_reduce` 慢路径里与多份副本一起进入同一个 FP32 累加器；`combined_topk_weights` 用 `match` 找 master lane 从 buffer 读出对应源 rank 的权重。
- 主 kernel 与 epilogue 共享同一个 `combine_reduce` 工具函数——前者做源端「局部预归约」，后者做接收端「最终归约」。

## 7. 下一步学习建议

- **u6-l3 确定性排序**：本讲的 epilogue 输出顺序依赖多 rank 数据到达顺序，可能不确定。下一讲讲 `EPHandle.deterministic_sort` 如何在 event 等待之后对结果重排，保证多次 combine 位序一致，与本讲的 `register_hook_after_wait` 直接相关。
- **u8-l1 PTX 原语**：本讲多次用到 `match`/`deduplicate`/`accumulate`/`ldg_with_gez_pred`/`tma_store`，下一讲会系统讲这些 PTX 原语的语义与内存序，特别是 `fence.proxy.async.shared::cta` 在 TMA 与 mbarrier 之间的作用。
- **延伸阅读**：对照 [combine.cuh](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/combine.cuh) 的主 kernel 与 [hybrid_combine.cuh](https://github.com/deepseek-ai/DeepEP/blob/099d5f2bad488b9c534ea785062b12f2e91d1d41/deep_ep/include/deep_ep/impls/hybrid_combine.cuh)，理解 hybrid 模式下 scaleup/forward 两类 warp 如何配合，与本讲的「两级归约」对应。
