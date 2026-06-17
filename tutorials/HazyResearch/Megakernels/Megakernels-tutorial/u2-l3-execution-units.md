# 数据搬运与执行单元

在 Megakernels 架构中，复杂的计算任务被分解为多个专门的执行单元，每个单元负责特定的职责。本讲义将深入介绍四个核心执行单元：Loader（加载器）、Storer（存储器）、Consumer（消费者）和 Launcher（启动器），以及它们之间的协作机制。

## 1. Loader 数据加载

### 1.1 概念说明

Loader（加载器）是负责将数据从全局内存（Global Memory, HBM）搬运到共享内存（Shared Memory）的执行单元。在 GPU 计算中，全局内存访问延迟高但容量大，而共享内存访问延迟低但容量小。Loader 的核心任务就是高效地管理这种数据搬运，确保计算单元能够及时获得所需数据。

在 Megakernels 架构中，Loader 由专门的 warp（warp 0）执行，独立于计算单元，从而实现计算与数据搬运的重叠（overlap）。

### 1.2 伪代码或流程

```python
# Loader 执行流程
for each instruction:
    1. 等待指令到达（通过信号量）
    2. 解析指令，确定需要加载的数据
    3. 检查目标页面是否就绪
    4. 发起异步加载操作（TMA/load_async）
    5. 标记页面完成，通知 Consumer
    6. 继续下一条指令
```

### 1.3 原理分析

Loader 的核心设计原理基于以下几个关键机制：

**流水线并行（Pipeline Parallelism）**：Loader 可以预先加载未来指令需要的数据，通过多级流水线（`INSTRUCTION_PIPELINE_STAGES`）实现指令级并行。当第 N 条指令在执行时，Loader 可以已经在为第 N+1、N+2 条指令准备数据。

**异步加载（Asynchronous Loading）**：使用 CUDA 的异步加载机制（如 `cp.async` 或 TMA），Loader 可以在不阻塞的情况下发起数据传输。这允许 Loader warp 在数据传输过程中继续处理其他工作。

**页面管理（Page Management）**：共享内存被划分为固定大小的页面（`PAGE_SIZE = 16384` 字节）。Loader 通过 `pid`（Physical Page ID）和 `lid`（Logical Page ID）的映射来管理这些页面的分配和释放。

**信号量同步（Semaphore Synchronization）**：Loader 使用信号量来通知 Consumer 数据已就绪。每个操作可以有多个信号量，用于精细控制不同数据流的同步。

### 1.4 代码实践

Loader 的实现通过 `MAKE_WORKER` 宏自动生成主循环框架：

```cpp
// include/loader.cuh
#pragma once
#include "kittens.cuh"
#include "util.cuh"

MAKE_WORKER(loader, TEVENT_LOADER_START, false)
```

这个宏展开后创建了 `loader::main_loop` 函数，它遍历所有指令并调用相应操作的 loader 实现。以 attention 操作为例：

```cpp
// demos/low-latency-llama/attention_partial.cu:298-305
struct loader {
    static __device__ void run(const globals &g, megakernel::state<config> &s) {
        auto laneid = kittens::warp::laneid();
        if (laneid >= 2 && laneid < config::NUM_PAGES) {
            int unused_page = s.pid(laneid);
            s.wait_page_ready(unused_page);
            s.finish_page(unused_page, config::NUM_CONSUMER_WARPS);
        }
    }
};
```

这段代码展示了 Loader 的一个关键职责：页面管理。某些 lanes 负责释放未使用的页面，确保页面资源得到有效利用。

对于需要实际加载数据的操作（如 MatVec），Loader 会执行更复杂的逻辑：

```cpp
// demos/low-latency-llama/rms_matvec_rope_append.cu:63-70
static __device__ inline void
load_iter(megakernel::state<Config> &s, const globals &g, parsed_instruction &inst,
          int iter, int col_idx, kittens::st_bf<16, 512> &weight_chunk,
          kittens::semaphore &sem) {
    auto block_idx = inst.start_block_idx + iter;
    kittens::tma::load_async<dim::ROW, cache_policy::EVICT_FIRST>(
        weight_chunk, g.qkv_weights,
        {inst.layer_idx, block_idx, col_idx}, sem);
}
```

这里使用了 Tensor Memory Accelerator (TMA) 进行异步加载，指定了缓存策略（`EVICT_FIRST`）以优化缓存利用率。

### 1.5 练习题

1. Loader 使用哪个 warp 执行？它如何与 Consumer warp 区分开？
2. 为什么 Loader 需要"预先"加载未来指令的数据？这与 `INSTRUCTION_PIPELINE_STAGES` 有何关系？
3. 在页面管理中，`pid`（Physical Page ID）和 `lid`（Logical Page ID）的区别是什么？
4. 异步加载（`load_async`）相比同步加载有什么优势？在什么情况下异步加载可能失败？

### 1.6 答案

1. Loader 使用 warp 0 执行。在 `megakernel.cuh:118-140` 中，通过 `kittens::warpid()` 判断：如果是 warp 0 则执行 `loader::main_loop`，如果是 warp 1-3 则执行 storer/launcher/controller，其他 warp（4-19）执行 consumer。

2. 预先加载是为了隐藏内存延迟。当 Consumer 在执行第 N 条指令时，Loader 可以已经在为第 N+1、N+2 条指令加载数据。`INSTRUCTION_PIPELINE_STAGES=2` 表示有 2 级指令流水线，允许同时处理 2 条指令的不同阶段。

3. `lid` 是逻辑页面 ID，表示页面操作的逻辑顺序；`pid` 是物理页面 ID，表示共享内存中实际的物理页面。Controller 通过 `release_lid` 函数将逻辑页面映射到物理页面，实现页面复用。

4. 异步加载的优势是可以与计算重叠，提高整体吞吐量。它可能在以下情况失败：如果目标共享内存区域已被占用且未完成，或者流水线深度不足以容纳额外的加载操作。

## 2. Storer 数据存储

### 2.1 概念说明

Storer（存储器）是负责将计算结果从共享内存写回全局内存的执行单元。与 Loader 相对，Storer 处理数据流的输出端，确保计算结果被正确地持久化到全局内存中，供后续操作使用。

Storer 由 warp 1 专门执行，与 Loader 形成数据搬运的对称两翼。

### 2.2 伪代码或流程

```python
# Storer 执行流程
for each instruction:
    1. 等待指令到达
    2. 等待 Consumer 完成计算（通过信号量）
    3. 从共享内存读取计算结果
    4. 发起异步存储操作到全局内存
    5. 更新全局屏障（Barrier）计数器
    6. 释放占用的页面
```

### 2.3 原理分析

Storer 的设计面临几个核心挑战：

**写后读（RAW）风险**：如果存储操作尚未完成，下一条指令可能读取到错误的全局内存数据。Storer 必须通过屏障（Barrier）机制确保存储完成后才允许后续操作继续。

**多 Consumer 同步**：多个 Consumer warp 可能同时完成计算，Storer 需要确保所有 warp 的结果都被正确收集和存储。

**部分结果处理**：在某些操作（如 attention reduction）中，Storer 可能需要存储部分结果到中间缓冲区，而不是最终输出。

**缓存一致性**：存储操作需要确保写回的数据对所有 SM 可见，特别是在多集群（Cluster）环境下。

### 2.4 代码实践

Storer 同样通过 `MAKE_WORKER` 宏生成框架：

```cpp
// include/storer.cuh
#pragma once
#include "kittens.cuh"
#include "util.cuh"

MAKE_WORKER(storer, TEVENT_STORER_START, false)
```

在 attention 操作中，Storer 负责存储 attention 输出和 LSE（Log-Sum-Exp）值：

```cpp
// demos/low-latency-llama/attention_partial.cu:549-673
struct storer {
    static inline __device__ void
    store_o_skip(const globals &g, megakernel::state<config> &s, int q_head_start_idx) {
        auto O_smem = get_O_smem(s);

        if (kittens::laneid() == 0) {
            kittens::wait(O_arrived(s), 0);
            s.record(megakernel::TEVENT_OUTPUT_READY);
        }
        kittens::warp::sync();

        kittens::rv_bf<globals::head_dim> O_bf;
        for (int head_offset = 0; head_offset < GQA_RATIO; head_offset++) {
            auto &smem_fl = O_smem[head_offset];
            auto &smem_bf = *reinterpret_cast<o_sv_bf *>(&smem_fl);

            kittens::warp::load(O_bf, smem_fl);
            kittens::warp::sync();
            kittens::warp::store(smem_bf, O_bf);
            kittens::warp::sync();
        }

        if (kittens::laneid() == 0) {
            for (int head_offset = 0; head_offset < GQA_RATIO; head_offset++) {
                auto &smem_bf = *reinterpret_cast<o_sv_bf *>(&O_smem[head_offset]);
                kittens::tma::store_async<cache_policy::EVICT_LAST>(
                    g.attn_out, smem_bf, {q_head_start_idx + head_offset});
            }
        }
    }

    static __device__ void run(const globals &g, megakernel::state<config> &s) {
        parsed_instruction inst{s};
        int laneid = kittens::warp::laneid();
        int q_head_start_idx = inst.kv_head_idx * GQA_RATIO;
        int q_head_vec_start_idx = q_head_start_idx % 16;

        auto skip_attn_reduction = g.skip_attn_reduction;

        if (skip_attn_reduction) {
            store_o_skip(g, s, q_head_start_idx);
        } else {
            store_o_no_skip(g, s, q_head_start_idx, inst);
        }

        // 存储 LSE 到全局内存
        if (laneid < GQA_RATIO && !skip_attn_reduction) {
            l_sv &L_smem = get_L_smem(s);
            kittens::wait(L_arrived(s), 0);

            float tmp;
            uint32_t src_ptr = static_cast<uint32_t>(
                __cvta_generic_to_shared(&L_smem.data[q_head_vec_start_idx + laneid]));
            float *dst_ptr = (float *)&g.attn_lse_intermediates.raw_ptr[
                (q_head_start_idx + laneid) * g.attn_lse_intermediates.cols() + inst.partial_idx];
            asm volatile("ld.shared.f32 %0, [%1];\n"
                         : "=f"(tmp)
                         : "r"(src_ptr));
            asm volatile("st.global.f32 [%0], %1;\n"
                         :
                         : "l"(dst_ptr), "f"(tmp));
        }

        kittens::tma::store_async_wait();
        if (laneid == 0) {
            s.record(123 + laneid);
            finish_QOL_page(s);
        }

        // 更新全局屏障
        if (laneid < GQA_RATIO) {
            if (laneid == 0) {
                s.record(megakernel::TEVENT_AT_GMEM_STORE);
            }

            if (skip_attn_reduction) {
                atomicAdd(&g.Bar[{inst.layer_idx, OPCODE_AttentionReduction - 1, 0}], 1);
            } else {
                atomicAdd(&g.Bar[{inst.layer_idx, opcode - 1, q_head_start_idx + laneid}], 1);
            }

            if (laneid == 0) {
                s.record(megakernel::TEVENT_DONE_GMEM_STORE);
            }
        }
    }
};
```

这段代码展示了 Storer 的多个关键功能：
1. **条件存储**：根据 `skip_attn_reduction` 标志选择不同的存储路径
2. **数据类型转换**：从 float 转换为 bf16 进行存储
3. **屏障更新**：通过 `atomicAdd` 更新全局屏障计数器
4. **页面释放**：通过 `finish_QOL_page` 释放页面资源

### 2.5 练习题

1. Storer 使用哪个 warp 执行？它如何确保在 Consumer 完成之前不会开始存储？
2. 在上述代码中，`skip_attn_reduction` 的作用是什么？它如何影响存储行为？
3. 为什么要使用 `atomicAdd` 更新屏障计数器，而不是简单的赋值操作？
4. `tma::store_async_wait()` 的作用是什么？为什么在 `finish_QOL_page` 之前调用它？

### 2.6 答案

1. Storer 使用 warp 1 执行（在 `megakernel.cuh:127-129` 中）。它通过信号量（如 `O_arrived`、`L_arrived`）等待 Consumer 完成计算，这些信号量由 Consumer 通过 `arrive` 操作触发。

2. `skip_attn_reduction` 控制是否跳过 attention 的 reduction 步骤。当为 true 时，直接存储最终输出到 `g.attn_out`；当为 false 时，存储部分结果到 `g.attn_out_intermediates`，供后续 reduction 操作使用。

3. 使用 `atomicAdd` 是因为多个 SM 可能同时更新同一个屏障计数器。原子操作确保更新的正确性和可见性，避免竞态条件。

4. `tma::store_async_wait()` 等待所有异步存储操作完成。在 `finish_QOL_page` 之前调用确保存储已经刷到全局内存，避免其他指令读取到不完整的数据。

## 3. Consumer 消费者执行

### 3.1 概念说明

Consumer（消费者）是执行实际计算操作的执行单元，是整个系统的核心计算引擎。在 Megakernels 架构中，Consumer 不是单一的 warp，而是由多个 warp（默认 16 个，`NUM_CONSUMER_WARPS`）组成的并行计算集群，每个 warp 独立处理不同的数据分片。

Consumer 的"消费"体现在它"消费"Loader 准备的数据，"生产"Storer 需要存储的结果。

### 3.2 伪代码或流程

```python
# Consumer 执行流程（每个 warp 独立执行）
for each instruction:
    1. 等待指令到达
    2. 解析指令，确定本 warp 的任务分片
    3. 等待 Loader 准备好数据（通过信号量）
    4. 从共享内存加载输入数据到寄存器
    5. 执行计算操作（矩阵乘法、向量运算等）
    6. 将结果写回共享内存
    7. 触发信号量，通知 Storer 数据已就绪
    8. 继续下一条指令
```

### 3.3 原理分析

Consumer 的设计充分利用了 GPU 的并行计算能力：

**Warp 级并行**：多个 Consumer warp 并行执行，每个 warp 处理不同的数据子集。在 attention 操作中，不同的 warp 处理不同的 attention head 或 KV block。

**Tensor Core 利用**：Consumer 广泛使用 Tensor Core 进行矩阵乘累加运算（如 `warp::mma_AB`），这是现代 GPU 提供的专用硬件加速器，可以大幅提升矩阵运算性能。

**寄存器分片**：通过 `kittens::warpgroup::increase_registers`，每个 Consumer warp 可以使用更多寄存器（默认 104 个），容纳更多中间结果，减少对共享内存的访问。

**精细同步**：Consumer 使用 `warp::sync`、`arrive` 等原语进行 warp 内和 warp 间同步，确保数据依赖关系得到满足。

**流水线执行**：Consumer 内部也实现多级流水线，可以在等待当前数据的同时，提前处理后续阶段的数据。

### 3.4 代码实践

Consumer 的框架由 `MAKE_WORKER` 宏生成，但最后一个参数为 `true`，表示这是 consumer：

```cpp
// include/consumer.cuh
#pragma once
#include "kittens.cuh"
#include "util.cuh"

MAKE_WORKER(consumer, TEVENT_CONSUMER_START, true)
```

在 attention 操作中，Consumer 实现了完整的 attention 计算：

```cpp
// demos/low-latency-llama/attention_partial.cu:387-547
struct consumer {
    static __device__ void run(const globals &g, megakernel::state<config> &s) {
        if (kittens::warpid() == 0) {
            // 设置
            parsed_instruction inst{s};
            int q_head_start_idx = inst.kv_head_idx * GQA_RATIO;

            // 等待前置操作完成
            if (kittens::laneid() == 0) {
                for (int head_offset = 0; head_offset < GQA_RATIO; head_offset++) {
                    while (*(volatile int *)&g.Bar[{inst.layer_idx,
                          OPCODE_RMS_QKV_MatVecRopeAppend - 1,
                          q_head_start_idx + head_offset}] < 4) {
                        __nanosleep(config::GMEM_SPIN_LOOP_SLEEP_NANOS);
                    }
                }
            }
            kittens::warp::sync();

            // 初始化寄存器
            int seq_len = g.pos_id + 1;
            int total_attn_blocks = (seq_len + LLAMA_1B_KV_BLOCK_SIZE - 1) /
                                    LLAMA_1B_KV_BLOCK_SIZE;
            int blocks_per_partial = (total_attn_blocks + inst.num_partials - 1) /
                                    inst.num_partials;
            int start_blk_idx = inst.partial_idx * blocks_per_partial;
            int end_blk_idx = min(start_blk_idx + blocks_per_partial, total_attn_blocks);
            float softmax_temp = g.attn_scale * 1.44269504089f;

            q_rt Q_reg;
            k_rt K_reg;
            v_rt V_reg;
            l_rv L_reg;
            o_rt O_reg;
            attn_fl_rt attn_fl_reg;
            attn_bf_rt attn_bf_reg;
            max_vec_rv max_vec_reg;
            norm_vec_rv norm_vec_reg;
            kittens::warp::neg_infty(max_vec_reg);
            kittens::warp::zero(norm_vec_reg);
            kittens::warp::zero(O_reg);

            // 加载 Q
            wait_QOL_page(s);
            q_st &Q_smem = get_Q_smem(s);
            load_Q_async(Q_smem, g.q_post_rope, q_head_start_idx);
            kittens::warp::load_async_wait();
            kittens::warp::load(Q_reg, Q_smem);

            // Attention 流水线
            for (int i = 0; i + start_blk_idx < end_blk_idx; ++i) {
                int stage = i % NUM_STAGES;
                kv_st &K_smem = get_K_smem(s, stage);
                kv_st &V_smem = get_V_smem(s, stage);

                // Q @ K.T
                kittens::warp::zero(attn_fl_reg);
                kittens::warp::wait(K_arrived(s, stage), (i / NUM_STAGES) % 2);
                kittens::warp::load(K_reg, K_smem);
                kittens::warp::mma_ABt(attn_fl_reg, Q_reg, K_reg, attn_fl_reg);
                kittens::warp::sync();
                kittens::warp::arrive(K_finished(s, stage));

                // Softmax 计算
                if ((i + start_blk_idx + 1) * LLAMA_1B_KV_BLOCK_SIZE > seq_len)
                    right_fill(attn_fl_reg, attn_fl_reg,
                               seq_len % LLAMA_1B_KV_BLOCK_SIZE, -999999999999.f);

                kittens::warp::row_max(max_vec_reg, attn_fl_reg, max_vec_reg);
                kittens::warp::mul(attn_fl_reg, attn_fl_reg, softmax_temp);
                kittens::warp::exp2(attn_fl_reg, attn_fl_reg);

                // A @ V
                kittens::warp::wait(V_arrived(s, stage), (i / NUM_STAGES) % 2);
                kittens::warp::load(V_reg, V_smem);
                kittens::warp::copy(attn_bf_reg, attn_fl_reg);
                kittens::warp::mma_AB(O_reg, attn_bf_reg, V_reg, O_reg);
                kittens::warp::sync();
                kittens::warp::arrive(V_finished(s, stage));

                // 更新归一化因子
                kittens::warp::row_sum(norm_vec_reg, attn_fl_reg, norm_vec_reg);
            }

            // 最终归一化
            if (start_blk_idx < end_blk_idx) {
                finish_KV_page(s);
                kittens::warp::div_row(O_reg, O_reg, norm_vec_reg);
                kittens::warp::log2(L_reg, norm_vec_reg);
                kittens::warp::add(L_reg, L_reg, scaled_max_vec_reg);
            }

            // 存储结果
            store_4_rows(O_smem, O_reg, q_head_local_idx);
            kittens::warp::sync();
            kittens::warp::arrive(O_arrived(s));
            kittens::warp::store(L_smem, L_reg);
            kittens::warp::sync();
            kittens::warp::arrive(L_arrived(s));
        }
    }
};
```

这段代码展示了 Consumer 的完整计算流程：
1. **前置依赖检查**：通过全局屏障确保前置操作完成
2. **寄存器分配**：使用大量寄存器存储中间结果
3. **流水线加载**：使用异步加载预取 KV cache
4. **矩阵乘法**：使用 Tensor Core (`mma_ABt`, `mma_AB`) 加速矩阵运算
5. **在线 Softmax**：使用稳定的在线算法计算 softmax
6. **结果输出**：通过信号量通知 Storer 数据已就绪

### 3.5 练习题

1. Consumer 由多少个 warp 组成？这些 warp 如何分配不同的任务？
2. 在上述代码中，`GQA_RATIO` 的作用是什么？它如何影响 warp 的任务分配？
3. 为什么要使用 `neg_infty(max_vec_reg)` 初始化？这与 online softmax 的稳定性有何关系？
4. `arrive` 和 `wait` 信号量操作如何协调 Consumer 和 Storer 之间的同步？

### 3.6 答案

1. Consumer 由 16 个 warp 组成（`NUM_CONSUMER_WARPS=16`）。在 attention 操作中，不同的 warp 处理不同的 attention head（通过 `warpid()` 和 `kv_head_idx` 分配），或者同一个 head 的不同 KV block（通过 `partial_idx` 分配）。

2. `GQA_RATIO` 表示每个 KV head 对应的 Q head 数量（在 Llama 1B 中为 4）。这影响了 warp 如何从部分结果中提取和存储对应 Q head 的数据，以及如何更新全局屏障。

3. `neg_infty` 初始化用于在线 softmax 的数值稳定性。算法从负无穷开始，逐步更新最大值和指数和，避免下溢和上溢问题。每个迭代中，新的最大值是旧最大值和当前块最大值的较大值。

4. `arrive` 和 `wait` 实现生产者-消费者同步。Consumer 在数据就绪后调用 `arrive` 增加信号量计数；Storer 在使用数据前调用 `wait` 等待信号量达到期望值。这种机制确保 Storer 不会访问未完成的数据。

## 4. Launcher 启动器

### 4.1 概念说明

Launcher（启动器）是负责启动和协调 Tensor Core 操作的执行单元。在现代 GPU 中，Tensor Core 是专门用于矩阵运算的硬件单元，需要特定的启动和同步机制。Launcher 的职责包括：预取数据、等待依赖条件满足、启动 Tensor Core 操作、管理操作流水线。

Launcher 由 warp 2 专门执行，它作为计算单元的"指挥官"，确保计算资源得到充分利用。

### 4.2 伪代码或流程

```python
# Launcher 执行流程
for each instruction:
    1. 等待指令到达
    2. 检查全局内存依赖（通过 Barrier）
    3. 等待共享内存页面就绪
    4. 启动 Tensor Core 预取（expect）
    5. 启动异步矩阵操作（load_async）
    6. 管理流水线阶段
    7. 继续下一条指令
```

### 4.3 原理分析

Launcher 的设计针对 Tensor Core 的特性进行了优化：

**依赖管理**：Launcher 负责检查操作的前置依赖是否满足。这包括全局内存依赖（通过全局 Barrier）和共享内存依赖（通过页面信号量）。

**TMA 预取**：Launcher 使用 Tensor Memory Accelerator 的 `expect` 操作，预先告诉 TMA 硬件即将访问的数据位置，允许硬件提前准备地址转换和访问路径。

**流水线调度**：Launcher 维护多级流水线（`NUM_STAGES`），在不同的流水线阶段启动不同的操作，实现操作级并行。

**Blackwell 架构支持**：在 Blackwell 架构中，Launcher 还负责管理 Tensor Memory 的同步，通过 `wait_tensor_ready` 和 `tensor_finished` 信号量。

### 4.4 代码实践

Launcher 的框架同样由 `MAKE_WORKER` 宏生成：

```cpp
// include/launcher.cuh
#pragma once
#include "kittens.cuh"
#include "util.cuh"

MAKE_WORKER(launcher, TEVENT_LAUNCHER_START, false)
```

在 attention 操作中，Launcher 负责 KV cache 的加载调度：

```cpp
// demos/low-latency-llama/attention_partial.cu:307-385
struct launcher {
    static __device__ void wait_for_kv(const globals &g, megakernel::state<config> &s,
                                       parsed_instruction &inst) {
        s.record(megakernel::TEVENT_AT_GMEM_WAIT);

        // 等待前置操作完成（每个 head 16 dims，所以同一 head 上 4 个操作）
        while (*(volatile int *)&g.Bar[{inst.layer_idx,
              OPCODE_RMS_QKV_MatVecRopeAppend - 1,
              LLAMA_1B_NUM_ATTENTION_HEADS + inst.kv_head_idx}] < 4) {
            __nanosleep(config::GMEM_SPIN_LOOP_SLEEP_NANOS);
        }

        while (*(volatile int *)&g.Bar[{inst.layer_idx,
              OPCODE_RMS_QKV_MatVecRopeAppend - 1,
              LLAMA_1B_NUM_ATTENTION_HEADS + LLAMA_1B_NUM_KV_HEADS + inst.kv_head_idx}] < 4) {
            __nanosleep(config::GMEM_SPIN_LOOP_SLEEP_NANOS);
        }

        s.record(megakernel::TEVENT_DONE_GMEM_WAIT);
    }

    static __device__ void run(const globals &g, megakernel::state<config> &s) {
        if (kittens::warp::laneid() == 0) {
#ifdef KITTENS_BLACKWELL
            s.wait_tensor_ready();
            arrive(s.tensor_finished, config::NUM_CONSUMER_WARPS);
#endif

            // 设置
            parsed_instruction inst{s};
            int seq_len = g.pos_id + 1;
            int total_attn_blocks = (seq_len + LLAMA_1B_KV_BLOCK_SIZE - 1) /
                                    LLAMA_1B_KV_BLOCK_SIZE;
            int blocks_per_partial = (total_attn_blocks + inst.num_partials - 1) /
                                    inst.num_partials;
            int start_blk_idx = inst.partial_idx * blocks_per_partial;
            int end_blk_idx = min(start_blk_idx + blocks_per_partial, total_attn_blocks);

            // 等待 KV 页面
            wait_KV_page(s);

            if (start_blk_idx >= end_blk_idx)
                finish_KV_page(s);

            // 运行流水线！
            for (int i = 0; i + start_blk_idx < end_blk_idx; ++i) {
                auto cur_blk_idx = start_blk_idx + i;
                int stage = cur_blk_idx % NUM_STAGES;
                kv_st &K_smem = get_K_smem(s, stage);
                kv_st &V_smem = get_V_smem(s, stage);

                if (i >= NUM_STAGES) {
                    kittens::wait(K_finished(s, stage), (i / NUM_STAGES - 1) % 2);
                    kittens::wait(V_finished(s, stage), (i / NUM_STAGES - 1) % 2);
                }

                if (cur_blk_idx == end_blk_idx - 1 && inst.partial_idx == inst.num_partials - 1) {
                    wait_for_kv(g, s, inst);
                }

                kittens::tma::expect(K_arrived(s, stage), K_smem);
                kittens::tma::load_async<dim::DEPTH, cache_policy::EVICT_FIRST>(
                    K_smem, g.k_cache,
                    {inst.layer_idx, cur_blk_idx, inst.kv_head_idx, 0},
                    K_arrived(s, stage));
                kittens::tma::expect(V_arrived(s, stage), V_smem);
                kittens::tma::load_async<dim::DEPTH, cache_policy::EVICT_FIRST>(
                    V_smem, g.v_cache,
                    {inst.layer_idx, cur_blk_idx, inst.kv_head_idx, 0},
                    V_arrived(s, stage));
            }
        }
    }
};
```

这段代码展示了 Launcher 的核心功能：
1. **依赖检查**：通过全局屏障等待前置操作的 QKV 投影完成
2. **流水线管理**：使用模运算 (`stage = cur_blk_idx % NUM_STAGES`) 在多个流水线阶段间轮转
3. **TMA 预取**：使用 `tma::expect` 预先声明将要访问的共享内存位置
4. **异步加载**：使用 `tma::load_async` 启动 KV cache 的异步加载
5. **流水线同步**：通过 `K_finished` 和 `V_finished` 信号量确保流水线阶段可用

### 4.5 练习题

1. Launcher 使用哪个 warp 执行？它与其他执行单元的 warp 分配有何不同？
2. 在上述代码中，`NUM_STAGES` 的作用是什么？它与流水线深度的关系是什么？
3. 为什么要使用 `tma::expect` 而不是直接 `load_async`？`expect` 带来什么性能优势？
4. Launcher 如何处理最后一块 KV 数据的依赖等待？这与前面的块有何不同？

### 4.6 答案

1. Launcher 使用 warp 2 执行（在 `megakernel.cuh:130-132` 中）。与其他单元类似，它也是专用的单个 warp，但职责更偏向于调度和启动，而非实际的数据搬运或计算。

2. `NUM_STAGES`（值为 3）定义了流水线的阶段数。通过 `stage = cur_blk_idx % NUM_STAGES`，Launcher 循环使用不同的阶段，实现操作的重叠。流水线深度决定了可以同时进行的加载数量，影响内存带宽利用率和延迟隐藏。

3. `tma::expect` 预先告诉 TMA 硬件即将访问的共享内存地址，允许硬件提前准备地址转换、缓存行分配等。相比直接 `load_async`，`expect` 可以减少启动延迟，提高 TMA 传输效率。

4. 对于最后一块（`cur_blk_idx == end_blk_idx - 1`）且最后一个 partial（`partial_idx == num_partials - 1`），Launcher 调用 `wait_for_kv` 确保全局内存中的 KV 数据已由前置操作完全写入。前面的块可以假设数据已就绪（因为流水线保证），但最后一块需要额外的全局内存同步。

## 5. 单元间协作机制

### 5.1 概念说明

Megakernels 的强大之处在于四个执行单元的高效协作。这些单元通过精心设计的同步机制实现数据流和控制流的协调，形成完整的计算流水线。单元间协作的核心是信号量（Semaphore）和屏障（Barrier）机制，以及指令流水线的多级并行。

### 5.2 伪代码或流程

```python
# 单元间协作的总体流程
# Controller (warp 3)
for instruction in instructions:
    1. 获取指令
    2. 分配物理页面
    3. 初始化信号量
    4. 触发 instruction_arrived 信号量

# 并行执行 (warp 0-2, 4-19)
for instruction in instructions:
    # Loader (warp 0)
    wait(instruction_arrived)
    load_data_from_global_memory()
    trigger(data_ready_semaphores)

    # Launcher (warp 2)
    wait(instruction_arrived)
    wait_for_dependencies()
    start_tensor_core_operations()

    # Consumer (warp 4-19)
    wait(instruction_arrived)
    wait(data_ready_semaphores)
    perform_computations()
    trigger(result_ready_semaphores)

    # Storer (warp 1)
    wait(instruction_arrived)
    wait(result_ready_semaphores)
    store_results_to_global_memory()
    update_global_barriers()
    release_pages()

    # 所有单元
    trigger(instruction_finished)
```

### 5.3 原理分析

单元间协作基于以下几个核心原理：

**指令级并行（Instruction-Level Parallelism）**：通过 `INSTRUCTION_PIPELINE_STAGES=2`，系统可以同时处理两条指令的不同阶段。当第 N 条指令在 Consumer 中执行计算时，第 N+1 条指令可能已经在 Loader 中加载数据。

**数据流并行ism**：每个操作定义自己的信号量集合，用于精细控制数据流的不同阶段。例如，attention 操作有 `Q_arrived`、`K_arrived`、`V_arrived`、`O_arrived`、`L_arrived` 等多个信号量。

**全局屏障同步**：全局 Barrier 用于跨 SM 的同步，确保一个 SM 的操作完成后，其他 SM 才能开始依赖的操作。这对于多 SM 协作至关重要（如 attention reduction）。

**环形缓冲区**：指令状态通过环形缓冲区管理，`instruction_ring` 在 `INSTRUCTION_PIPELINE_STAGES` 个阶段间轮转，实现指令流水线的循环利用。

**页面生命周期管理**：每个页面有明确的生命周期：分配 → 加载 → 计算 → 存储 → 释放。Controller 通过 `release_lid` 函数决定页面复用策略。

### 5.4 代码实践

协作机制的核心在 `megakernel.cuh` 的主函数中体现：

```cpp
// include/megakernel.cuh:118-140
if (kittens::warpid() < config::NUM_CONSUMER_WARPS) {
    kittens::warpgroup::increase_registers<config::CONSUMER_REGISTERS>();
    ::megakernel::consumer::main_loop<config, globals, ops...>(g, mks);
} else {
    kittens::warpgroup::decrease_registers<config::NON_CONSUMER_REGISTERS>();
    switch (kittens::warpgroup::warpid()) {
    case 0:
        ::megakernel::loader::main_loop<config, globals, ops...>(g, mks);
        break;
    case 1:
        ::megakernel::storer::main_loop<config, globals, ops...>(g, mks);
        break;
    case 2:
        ::megakernel::launcher::main_loop<config, globals, ops...>(g, mks);
        break;
    case 3:
        ::megakernel::controller::main_loop<config, globals, ops...>(g, mks);
        break;
    default:
        asm volatile("trap;");
    }
}
```

每个执行单元的 `main_loop` 由 `MAKE_WORKER` 宏生成：

```cpp
// include/util.cuh:260-304
#define MAKE_WORKER(name, start_event, is_consumer)                            \
    namespace megakernel {                                                     \
    namespace name {                                                           \
                                                                               \
    template <typename config, typename globals> struct name##_op_dispatcher { \
        template <typename op> struct dispatcher {                             \
            __device__ static inline void                                      \
            run(const globals &g, ::megakernel::state<config> &mks) {         \
                op::name::run(g, mks);                                         \
            }                                                                  \
        };                                                                     \
    };                                                                         \
                                                                               \
    template <typename config, typename globals, typename... ops>              \
    __device__ void main_loop(const globals &g,                                \
                              ::megakernel::state<config> &mks) {              \
        MK_DEBUG_PRINT_START(#name);                                           \
        int num_iters = g.instructions.rows();                                 \
        for (mks.instruction_index = 0, mks.instruction_ring = 0;              \
             mks.instruction_index < num_iters; mks.next_instruction()) {      \
            mks.await_instruction();                                           \
            if (kittens::laneid() == 0) {                                               \
                if (is_consumer) {                                             \
                    mks.record(start_event + 2 * kittens::warpid());                    \
                } else {                                                       \
                    mks.record(start_event);                                   \
                }                                                              \
            }                                                                  \
            dispatch_op<name##_op_dispatcher<config, globals>::dispatcher,     \
                        ops...>::template run<void, config, globals,           \
                                              ::megakernel::state<config>>(    \
                mks.instruction()[0], g, mks);                                 \
            if (kittens::laneid() == 0) {                                               \
                if (is_consumer) {                                             \
                    mks.record(start_event + 2 * kittens::warpid() + 1);                \
                } else {                                                       \
                    mks.record(start_event + 1);                               \
                }                                                              \
            }                                                                  \
        }                                                                      \
        __syncwarp();                                                          \
        MK_DEBUG_PRINT_END(#name);                                             \
    }                                                                          \
    }                                                                          \
    }
```

这个宏实现了通用的主循环框架，包括：
1. **指令迭代**：遍历所有指令
2. **指令等待**：通过 `await_instruction` 等待指令就绪
3. **时间记录**：记录执行时间用于性能分析
4. **操作分发**：根据指令 opcode 分发到具体操作的处理函数
5. **指令推进**：通过 `next_instruction` 推进到下一条指令

具体的同步原语在 `state<config>` 中定义：

```cpp
// include/util.cuh:122-140
__device__ inline void await_instruction() {
    kittens::wait(instruction_arrived[instruction_ring],
         (instruction_index / config::INSTRUCTION_PIPELINE_STAGES) & 1);
    pid_order_shared_addr =
        static_cast<uint32_t>(__cvta_generic_to_shared(&(pid_order()[0])));
}
__device__ inline void next_instruction() {
    __syncwarp();
    if (kittens::laneid() == 0) {
#ifdef MK_DEBUG
        printf("Thread %d: arriving at instruction finished %d\n",
               threadIdx.x, instruction_ring);
#endif
        kittens::arrive(instruction_finished[instruction_ring]);
    }
    instruction_index++;
    instruction_ring =
        ring_advance<config::INSTRUCTION_PIPELINE_STAGES>(instruction_ring);
}
```

### 5.5 练习题

1. 在单元间协作中，Controller 的角色是什么？它如何确保其他单元不会开始执行未准备好的指令？
2. `instruction_ring` 的作用是什么？为什么要使用环形缓冲区而不是直接递增索引？
3. Consumer 使用 `increase_registers` 而其他单元使用 `decrease_registers` 的原因是什么？这如何影响性能？
4. 在 `MAKE_WORKER` 宏中，`is_consumer` 参数如何影响时间记录？为什么 Consumer 的时间记录公式不同？

### 5.6 答案

1. Controller 负责指令的调度和资源分配。它在其他单元之前执行，确保指令已加载、页面已分配、信号量已初始化。只有当 Controller 触发 `instruction_arrived` 信号量后，其他单元才开始执行该指令。

2. `instruction_ring` 在 `INSTRUCTION_PIPELINE_STAGES` 个阶段间循环，实现指令流水线的循环利用。环形缓冲区允许同时处理多条指令，不同的指令占用不同的流水线阶段，避免资源冲突。

3. Consumer 执行大量计算，需要更多寄存器存储中间结果（104 个）。Loader/Storer/Launcher 执行简单的数据搬运，不需要那么多寄存器（64 个），减少寄存器压力可以增加可驻留的 warp 数量。

4. 对于 Consumer，`is_consumer=true`，时间记录为 `start_event + 2 * warpid() (+ 1)`，因为多个 Consumer warp 并行执行，需要不同的时间槽。对于其他单元（单个 warp），时间记录为 `start_event (+ 1)`，只需要一个时间槽。

## 总结

本讲义介绍了 Megakernels 架构中的四个核心执行单元及其协作机制：

- **Loader**：负责将数据从全局内存搬运到共享内存，使用异步加载和流水线技术隐藏内存延迟
- **Storer**：负责将计算结果从共享内存写回全局内存，通过屏障机制确保数据一致性
- **Consumer**：执行实际的计算操作，使用 Tensor Core 和寄存器分片技术提高计算效率
- **Launcher**：启动和协调 Tensor Core 操作，管理依赖关系和流水线调度

这些单元通过精心设计的信号量和屏障机制实现高效协作，形成完整的计算流水线。通过指令级并行、数据流并行和流水线重叠，Megakernels 架构充分利用了 GPU 的计算能力和内存带宽，实现了卓越的性能。

理解这些执行单元的职责和协作机制，是掌握 Megakernels 架构的关键，也是进一步优化和扩展系统的基础。