# 多 GPU 训练：ZeRO 分片与 NCCL

## 1. 本讲目标

本讲讲解 llm.c 的 CUDA 主线 `train_gpt2.cu` 如何从「单卡训练」扩展到「多卡、多节点训练」。读完本讲，你应当能够：

1. 说清楚 `DataLoader` 的 `process_rank` / `num_processes` 如何把数据切成互不重叠的分片，让每张 GPU 看到不同的数据（数据并行）。
2. 理解 NCCL 的三种集合通信原语：`ncclAllReduce`、`ncclReduceScatter`、`ncclAllGather`，以及它们分别对应 DDP 与 ZeRO 的哪一步。
3. 解释 ZeRO Stage 1（优化器状态分片）为什么能在不改变数学结果的前提下降低单卡显存，并理解本仓库目前只支持 stage 0/1。
4. 推导「micro-batch 梯度累积」与 `total_batch_size` 的关系，看懂 `gpt2_backward_and_reduce` 如何把前向、反向、跨卡规约、梯度累积串成一个外层训练步。

本讲是「训练工程」单元的第四篇，承接 [u6-l1 混合精度](u6-l1-mixed-precision.md)（你已经知道 master weights 是 fp32、`m_memory`/`v_memory` 是优化器状态）与 [u5-l1 CUDA 主线架构](u5-l1-cuda-mainline-architecture.md)（你已经知道 `floatX`、`ParameterTensors`、一次性 `cudaMalloc` + 指针排布）。

## 2. 前置知识

在进入多 GPU 之前，先用三段话建立直觉。

**数据并行（Data Parallelism, DP）。** 最朴素的多卡做法是「每张卡复制一份完整的模型，喂不同的数据，各自算梯度，再把梯度平均」。这样每张卡看到的数据变多，等价于用更大的 batch 训练。llm.c 的多卡就是数据并行（而不是把一层层切到不同卡上的「模型并行」）。

**集合通信（Collective Communication）。** 「把梯度平均」需要一个让所有卡都参与的通信操作，称为**集合通信**。最常用的是 **all-reduce**：每张卡贡献自己的梯度，结束时每张卡都拿到「所有卡梯度的平均值」。NVIDIA 的 **NCCL** 库就是专门做这类 GPU-GPU 集合通信的，llm.c 通过 `ncclAllReduce` / `ncclReduceScatter` / `ncclAllGather` 调用它。

**ZeRO（Zero Redundancy Optimizer）。** 纯数据并行有个浪费：每张卡都完整保存了「参数 + 梯度 + 优化器状态（AdamW 的 m、v、master weights）」，这些是**冗余（redundancy）**的。ZeRO 的思路是「与其每张卡都冗余一份，不如把它们分片（shard）到各卡，需要时再通信凑齐」。分到什么程度分三个 stage：

| Stage | 分片对象 | llm.c 是否支持 |
|-------|---------|---------------|
| 0 | 不分片（纯 DDP） | ✅ |
| 1 | 优化器状态（m、v、master） | ✅ |
| 2 | 优化器状态 + 梯度 | ❌（源码明确标注 not yet supported） |
| 3 | 优化器状态 + 梯度 + 参数 | ❌（同上） |

这个表很关键——很多人以为 ZeRO 就等于 stage 3，但本讲会带你读源码确认：llm.c **当前只实现了 stage 0 和 stage 1**。

> 关键术语：`process`（进程，每张卡跑一个进程）、`rank`（进程编号，从 0 开始）、`P = num_processes`（进程总数）、`shard`（分片）、`all-reduce` / `reduce-scatter` / `all-gather`（三种集合通信）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [llmc/zero.cuh](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/zero.cuh) | 多 GPU 的「中枢」：`MultiGpuConfig` 结构体、NCCL 初始化、分片偏移计算、`multi_gpu_async_reduce_gradient` 通信函数、`set_zero_configs`。 |
| [train_gpt2.cu](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu) | CUDA 主线：`gpt2_backward_and_reduce`（反向 + 跨卡规约）、`gpt2_calculate_grad_norm`、`gpt2_update`（带 ZeRO 分片的 AdamW）、`main` 中的梯度累积主循环。 |
| [llmc/dataloader.h](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/dataloader.h) | 分布式数据加载：`process_rank` / `num_processes` 让每张卡读取不同的 token 分片。 |
| [Makefile](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile) | 自动探测 NCCL / MPI，加上 `-DMULTI_GPU` / `-DUSE_MPI` 编译宏。 |
| [scripts/multi_node/run_gpt2_124M_mpi.sh](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/scripts/multi_node/run_gpt2_124M_mpi.sh) | 真实多节点启动脚本，含 `mpirun` 与 `-z 1`（ZeRO-1）。 |

---

## 4. 核心概念与源码讲解

### 4.1 数据并行基础：DataLoader 如何给每张卡分不同数据

#### 4.1.1 概念说明

数据并行的第一步是「保证每张卡喂进不同的数据」。如果 8 张卡都看到同一批 token，那 8 张卡的梯度完全一样，等价于 1 张卡，多卡就白费了。

llm.c 的做法很直接：**把 `.bin` 文件里的 token 流看成一条连续的「长河」，每张卡按自己的 `rank` 跳到不同的河段去取水**。这一切只靠两个量：`process_rank`（我是第几号进程）和 `num_processes`（一共几个进程）。它们在 `main` 里从 `MultiGpuConfig` 透传给 `dataloader_init`：

```c
dataloader_init(&train_loader, train_data_pattern, B, T,
                multi_gpu_config.process_rank,    // 我是第几号
                multi_gpu_config.num_processes,   // 一共几号
                permute_train_loader);
```

对应 [train_gpt2.cu:L1604](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1604)。

#### 4.1.2 核心流程

设 `P = num_processes`、当前进程 `r = process_rank`，一个 micro-batch 要 `B*T` 个 token。`DataLoader` 把 token 流按下面的方式切片（字节为单位，每个 token 是 `uint16_t` = 2 字节）：

```
一个「全局样本 idx」横跨 P 张卡，共占 P*B*T 个 token：
+--------- sample idx=0 ---------+--------- sample idx=1 ---------+ ...
| rank0 的 B*T | rank1 的 B*T | ... | rank0 的 B*T | rank1 的 B*T | ...
+--------------------------------+--------------------------------+
```

也就是说，对同一个样本编号 `idx`，rank 0 取第 0 个 `B*T` 块、rank 1 取第 1 个 `B*T` 块……各卡互不重叠。换算成 `.bin` 文件的**字节偏移**（注意头是 1024 字节）：

\[
\text{offset}(\text{idx}, r) = 1024 \;+\; \text{idx}\cdot(P\cdot B\cdot T\cdot 2) \;+\; r\cdot(B\cdot T\cdot 2)
\]

从该偏移读 `B*T+1` 个 `uint16_t` token，前 `B*T` 个当 `inputs`、后 `B*T` 个当 `targets`（错位一位，下一个 token 预测）。这套「错位一位」逻辑与单卡 [u1-l4 数据管线](u1-l4-data-and-tokenizer.md) 完全一致，多卡只是多了一个 `r` 的偏移。

#### 4.1.3 源码精读

两个关键字节数在 `dataloader_init` 里算好，存进结构体（[llmc/dataloader.h:L156-L157](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/dataloader.h#L156-L157)）：

```c
loader->total_batch_size_bytes = ((loader->num_processes * (loader->B * loader->T)) * sizeof(uint16_t));
loader->local_batch_offset_bytes = loader->process_rank * loader->B * loader->T * sizeof(uint16_t);
```

- `total_batch_size_bytes`：一个全局样本跨所有进程的总字节数（`P*B*T*2`）。
- `local_batch_offset_bytes`：当前进程在自己样本内的起始字节偏移（`r*B*T*2`）。

真正读数据在 `dataloader_load_batch`（[llmc/dataloader.h:L203-L219](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/dataloader.h#L203-L219)）：

```c
size_t global_batch_offset_bytes = idx * loader->total_batch_size_bytes;
int64_t current_offset = loader->header_bytes + global_batch_offset_bytes + loader->local_batch_offset_bytes;
fseekCheck(loader->tokens_file, (int) current_offset, SEEK_SET);
freadCheck(loader->buffer, sizeof(uint16_t), B*T+1, loader->tokens_file);
for (int i = 0; i < B*T; i++) {
    loader->inputs[i]  = (int)loader->buffer[i];
    loader->targets[i] = (int)loader->buffer[i+1];   // 错位一位
}
```

中文说明：`global_batch_offset_bytes` 把读指针跳到第 `idx` 个全局样本的起点，`local_batch_offset_bytes` 再加上「我这张卡」在该样本内的偏移，最后读 `B*T+1` 个 token 并错位拆成 `inputs`/`targets`。每个 rank 的 `local_batch_offset_bytes` 不同，于是各卡读到不同数据。

注意 `DataLoader` 结构体本身就把分布式字段放在最前面（[llmc/dataloader.h:L32-L33](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/dataloader.h#L32-L33)），可见「分布式」是它的一等公民。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：确认 `P=2, B=4, T=2` 时两卡不会读到重叠 token。
2. **步骤**：假设 `.bin` 头后是一串 token id `[0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,...]`（每行 B*T=8 个 token 为一个卡内 batch）。
   - 手算 `total_batch_size_bytes = 2*4*2*2 = 32` 字节 = 16 个 token。
   - 手算 rank 0 的 `local_batch_offset_bytes = 0`，rank 1 的 `= 4*2*2 = 16` 字节 = 8 个 token。
   - 对 `idx=0`：rank 0 从字节偏移 `1024+0+0` 读 token `[0..7]`；rank 1 从 `1024+0+16` 读 token `[8..15]`。
3. **观察现象**：两卡的 `inputs` 拼起来正好覆盖 `[0..15]` 共 16 个 token，无重叠、无遗漏。
4. **预期结果**：每张卡的 `inputs` 各 8 个 token 且互不相同。若你修改任一卡的 `process_rank`，它的取数起点会随之平移 `B*T*2` 字节。**待本地验证**（需要真实 `.bin` 与多进程环境；单进程下 `num_processes=1`，两「卡」退化为同一段数据）。

#### 4.1.5 小练习与答案

**练习 1**：如果 `num_processes=1`，`local_batch_offset_bytes` 是多少？这说明什么？
**答**：`process_rank=0`，所以 `local_batch_offset_bytes=0`，且 `total_batch_size_bytes = B*T*2`。这说明单卡时退化为普通的、无分片的 DataLoader。

**练习 2**：为什么 `freadCheck` 读 `B*T+1` 个 token，而 `inputs`/`targets` 只各存 `B*T` 个？
**答**：因为 `targets[i] = buffer[i+1]`，要给最后一个位置 `i=B*T-1` 取到 `buffer[B*T]`，所以必须多读 1 个 token；这 1 个 token 就是最后一个位置的「下一个 token 预测」目标。

---

### 4.2 NCCL 集合通信：all-reduce 与 reduce-scatter

#### 4.2.1 概念说明

数据并行要求「把各卡梯度合并」。NCCL 提供三种本讲会遇到的集合通信原语，先用一个「P 个同学各有一摞扑克牌」的比喻建立直觉：

- **`ncclAllReduce`**（全归约）：每个同学把自己的牌求和（或求平均），结束后**每个人都拿到同一份完整的平均值**。对应 DDP / ZeRO stage 0。
- **`ncclReduceScatter`**（归约 + 散射）：把牌求平均后**只平均地切开**，每个同学只拿回其中的 `1/P`（自己那一段）。对应 ZeRO stage 1 的梯度合并。
- **`ncclAllGather`**（全收集）：每个同学贡献自己的一段，结束后**每个人都拿到拼好的完整一摞**。对应 ZeRO stage 1 在 AdamW 更新后把参数重新散播给所有卡。

llm.c 把这些原语包在 `llmc/zero.cuh` 里，并用 `ncclCheck` 宏做错误检查（[llmc/zero.cuh:L36-L42](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/zero.cuh#L36-L42)），出错会打印文件名行号并 `exit`，与 [u5-l2](u5-l2-cuda-utility-layer.md) 讲过的 `cudaCheck` 一脉相承。

一个工程细节：NCCL 操作的数据类型要和 `PRECISION` 对齐，仓库用一个全局常量 `ncclFloatX` 跟随精度宏切换（[llmc/zero.cuh:L28-L34](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/zero.cuh#L28-L34)）——BF16 对应 `ncclBfloat16`、FP16 对应 `ncclHalf`、FP32 对应 `ncclFloat`，与 `floatX` 同源。

#### 4.2.2 核心流程

跨卡梯度合并的核心函数是 `multi_gpu_async_reduce_gradient`（[llmc/zero.cuh:L513-L553](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/zero.cuh#L513-L553)）。它的伪代码：

```
multi_gpu_async_reduce_gradient(pointers[N], sizes[N], config, compute_stream):
    if num_processes == 1: return            # 单卡无事可做
    # 让 nccl_stream 等 compute_stream 把梯度算完
    record(compute_nccl_sync 事件 on compute_stream)
    nccl_stream.wait(compute_nccl_sync 事件)
    ncclGroupStart()                          # 把 N 个集合调用合并成一个 kernel
    for i in 0..N:
        if zero_stage == 0:
            ncclAllReduce(pointers[i], pointers[i], sizes[i], Avg)   # DDP
        elif zero_stage == 1:
            shard_size  = sizes[i] / P
            shard_off   = shard_size * rank
            ncclReduceScatter(pointers[i], pointers[i] + shard_off, shard_size, Avg)  # ZeRO-1
    ncclGroupEnd()
```

三件事值得注意：

1. **异步与流同步**：NCCL 跑在独立的 `nccl_stream` 上，靠 `compute_nccl_sync` 这个 **event**（不是 `cudaDeviceSynchronize`）和计算流建立依赖——「算完梯度再规约」，同时不阻塞 host。这套 event 机制在 [u5-l2 多 stream 调度](u5-l2-cuda-utility-layer.md) 已讲过。
2. **`ncclGroupStart/End`**：把循环里几十个 `ncclAllReduce`/`ncclReduceScatter` 合并成**一个** NCCL kernel 提交，显著减少 launch 开销。
3. **reduce-scatter 的就地写入**：ZeRO-1 下，输出指针是 `pointers[i] + shard_off`，即「在自己那份梯度缓冲的对应位置就地写入本卡分片」。规约结束后，`grads_memory` 里**只有本卡负责的那一段是有效平均值，其余段是过期数据**——这点会在 4.4 节的 grad norm 计算中再次出现。

#### 4.2.3 源码精读

DDP（stage 0）分支，in-place all-reduce、用 `ncclAvg`（[llmc/zero.cuh:L532-L538](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/zero.cuh#L532-L538)）：

```c
if(config->zero_stage == 0) {
    ncclCheck(ncclAllReduce(
            pointers[i], pointers[i],     // in-place：输入输出同址
            pointers_sizes[i],
            ncclFloatX, ncclAvg,          // 取平均
            config->nccl_comm, config->nccl_stream
    ));
}
```

ZeRO-1（stage 1）分支，reduce-scatter 到本卡分片（[llmc/zero.cuh:L539-L549](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/zero.cuh#L539-L549)）：

```c
else if(config->zero_stage == 1) {
    assert(pointers_sizes[i] % config->num_processes == 0);
    size_t shard_size = pointers_sizes[i] / config->num_processes;
    ptrdiff_t shard_offset = (ptrdiff_t)shard_size * config->process_rank;
    ncclCheck(ncclReduceScatter(
            pointers[i], pointers[i] + shard_offset,   // 输出落在自己那段
            shard_size,
            ncclFloatX, ncclAvg,
            config->nccl_comm, config->nccl_stream
    ));
}
```

`ncclGroupStart/End` 包住整个循环（[llmc/zero.cuh:L530-L551](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/zero.cuh#L530-L551)）。函数顶部的「单卡早退」也很关键（[llmc/zero.cuh:L517-L519](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/zero.cuh#L517-L519)），保证单卡时不触发任何 NCCL 调用。

> 关于通信量：理论上 all-reduce ≈ reduce-scatter + all-gather，所以 ZeRO-1 的通信量与纯 DDP **基本相同**。ZeRO-1 的收益纯粹来自省显存（见 4.3），不是省通信。能省通信的是 stage 2/3，但本仓库尚未实现。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：理解 event 同步与 group 合并。
2. **步骤**：打开 [llmc/zero.cuh:L521-L552](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/zero.cuh#L521-L552)。
3. 观察 `cudaEventRecord` + `cudaStreamWaitEvent` 这两行：它们没有同步 host，只是让 GPU 内部两条流排队等待。
4. 把 `ncclGroupStart/End` 注释掉**想象**一下后果（不要真改源码）：N 个张量会变成 N 次独立 NCCL kernel，launch 开销翻几十倍。
5. **预期结果**：能口述「event 建立流间依赖、group 聚合集合调用」两件事的作用。**待本地验证**真实多卡下的 nsys/ncu 时间线。

#### 4.2.5 小练习与答案

**练习 1**：`ncclAllReduce` 用 `ncclAvg` 而不是 `ncclSum`，为什么？
**答**：数据并行下，每张卡的梯度是该卡 `B*T` 个 token 上的平均；要把 `P` 张卡合并成「全体 `P*B*T` 个 token 的平均梯度」，就是对 P 份平均再取平均，故用 `ncclAvg`。若用 `ncclSum` 会得到 P 倍梯度，需在别处再除 P。

**练习 2**：reduce-scatter 之后，`grads_memory` 的非本卡区段为什么「过期」？
**答**：reduce-scatter 的输出只写到了 `pointers[i] + shard_offset` 起的 `shard_size` 个元素；其余位置保留的是反向算出的、未经跨卡平均的旧梯度，不能直接用于更新或算 grad norm。

---

### 4.3 ZeRO Stage 1：优化器状态分片与显存收益

#### 4.3.1 概念说明

现在进入本讲最有「显存账」可算的部分。回顾纯数据并行（stage 0）下，每张卡要完整保存：

- **参数** `params_memory`（`floatX`，BF16 下 2 字节/个）
- **梯度** `grads_memory`（`floatX`，2 字节/个）
- **优化器状态** `m_memory` + `v_memory`（fp32，各 4 字节/个）+ `master_weights`（fp32，4 字节/个，见 [u6-l1](u6-l1-mixed-precision.md)）

设参数总数为 `N`，BF16 + master weights 时，每卡显存约为：

\[
\underbrace{2N}_{\text{params}} + \underbrace{2N}_{\text{grads}} + \underbrace{4N}_{m} + \underbrace{4N}_{v} + \underbrace{4N}_{\text{master}} = 16N \text{ 字节}
\]

其中**参数和梯度是前向/反向真正需要的**（不能轻易丢），但**优化器状态（m、v、master）只有 AdamW 更新那一步用到**——ZeRO-1 的洞察就是：把优化器状态切成 P 份，每卡只留自己那 `1/P`，更新时各自更新自己的片段，再通过 `all-gather` 把新参数散播给所有人。**数学结果与 stage 0 完全一致**，但每卡优化器状态从 `12N` 字节降到 `12N/P` 字节。

#### 4.3.2 核心流程

ZeRO-1 一个外层训练步的「显存视角」：

```
每卡持有：完整参数 N + 完整梯度缓冲 N + 优化器状态 N/P（只在自己的分片上）
   │
   │ 反向传播：每卡在自己的 B*T 数据上算完整梯度 → grads_memory（仍 N 大小）
   │
   ▼ multi_gpu_async_reduce_gradient (zero_stage==1)
reduce-scatter：每卡得到「平均梯度的本卡分片」（其余段过期）
   │
   ▼ gpt2_update
AdamW 只更新本卡分片（shard.size = N/P 个参数），
   只读写本卡分片的 m/v/master（已按 N/P 分配）
   │
   ▼ all-gather
把更新后的本卡参数分片散播给所有卡 → 每卡又重新持有完整的新参数 N
```

关键工程点：**优化器状态在分配时就只分 `N/P`**，不是分配完整再丢弃。来看分配处（[train_gpt2.cu:L397-L408](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L397-L408)）：

```c
size_t shard_num_parameters = multi_gpu_config.shard_num_parameters; // stage1 时 = N/P
cudaCheck(cudaMallocConditionallyManaged((void**)&model->m_memory, shard_num_parameters * sizeof(float)));
cudaCheck(cudaMallocConditionallyManaged((void**)&model->v_memory, shard_num_parameters * sizeof(float)));
if (model->master_weights != nullptr ... )
    cudaCheck(cudaMallocConditionallyManaged((void**)&model->master_weights, shard_num_parameters * sizeof(float)));
```

`shard_num_parameters` 在 `set_zero_configs` 里定（stage 0 时等于 N，stage 1 时等于 N/P）。

#### 4.3.3 源码精读

先看 `set_zero_configs`——它决定了 stage 2/3 会被静默降级为 stage 0（[llmc/zero.cuh:L558-L579](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/zero.cuh#L558-L579)）：

```c
void set_zero_configs(MultiGpuConfig* config, int zero_stage, size_t total_parameters) {
    config->zero_stage = 0;
    config->shard_num_parameters = total_parameters;
    if (zero_stage == 0) { ... }                          // 不分片
    else if (zero_stage == 1) {
        if (total_parameters % config->num_processes != 0) {
            // 不能均分 → 降级为 0
            config->zero_stage = 0;
        } else {
            config->zero_stage = 1;
            config->shard_num_parameters = total_parameters / config->num_processes;
        }
    } else {
        printf0("| ... Zero Stage2 and Stage3 are not yet supported  |\n");
        config->zero_stage = 0;                            // 2/3 强制降级为 0
    }
}
```

中文说明：传入 `-z 2` 或 `-z 3` 不会报错，而是打印一行「not yet supported」并退回 stage 0；传 `-z 1` 但参数数不能被 `num_processes` 整除时也退回 stage 0。所以**真正生效的 ZeRO 只有 stage 1，且要求 `N % P == 0`**。

接着看 `multi_gpu_get_shard_offset`——给定一个张量，算「本卡该负责哪一段」（[llmc/zero.cuh:L496-L507](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/zero.cuh#L496-L507)）：

```c
ShardInfo multi_gpu_get_shard_offset(size_t elements, const MultiGpuConfig* config, int shard_at_stage) {
    const int nproc = config->num_processes;
    if(config->zero_stage >= shard_at_stage) {
        return {(ptrdiff_t)(config->process_rank * (elements / nproc)), elements / nproc};
    } else {
        return {0, elements};   // 不分片：偏移 0、全长
    }
}
```

`gpt2_update` 用它决定 AdamW 要更新哪段参数、读写哪段优化器状态（[train_gpt2.cu:L1071-L1086](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1071-L1086)）：

```c
ShardInfo tensor = gpt2_get_tensor_at_layer(model, 0, i);
ShardInfo shard = multi_gpu_get_shard_offset(tensor.size, multi_gpu_config, 1);
ptrdiff_t local_offset_full    = tensor.offset + shard.offset;              // 参数/梯度：完整布局里的本卡段
ptrdiff_t local_offset_partial = tensor.offset / multi_gpu_config->num_processes;
...
ptrdiff_t opt_state_offset = multi_gpu_config->zero_stage < 1 ? local_offset_full : local_offset_partial;
float* m_ptr      = model->m_memory + opt_state_offset;     // m/v/master 索引进 N/P 大小的缓冲
```

中文说明：参数和梯度用「完整布局里的本卡段」寻址（因为它们仍是 N 大小的全缓冲），而优化器状态用「缩了 P 倍的局部偏移」寻址（因为 m/v/master 缓冲只有 N/P 大）。`adamw_update` kernel 只处理 `shard.size` 个元素。

更新完本卡分片后，用 `ncclAllGather` 把新参数散播给所有卡（[train_gpt2.cu:L1108-L1120](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1108-L1120)）：

```c
if (multi_gpu_config->zero_stage == 1) {
    ncclCheck(ncclGroupStart());
    for(int l = 0; l < num_layers; ++l) {
        // 每卡贡献自己更新好的分片，所有人收集成完整参数
        ncclCheck(ncclAllGather(param_ptr + l * tensor.size,
                                (floatX*) model->params_memory + tensor.offset + l * tensor.size,
                                shard.size, ncclFloatX,
                                multi_gpu_config->nccl_comm, multi_gpu_config->nccl_stream));
    }
    ncclCheck(ncclGroupEnd());
}
```

#### 4.3.4 代码实践（显存账）

1. **目标**：定量算出 ZeRO-1 在 8 卡 BF16 + master weights 下的显存收益。
2. **步骤**：
   - stage 0 每卡：`16N` 字节（见 4.3.1 推导）。
   - stage 1 每卡：参数 `2N` + 梯度缓冲 `2N` + `m` `4N/P` + `v` `4N/P` + master `4N/P` = `4N + 12N/P`。
   - 代入 `P=8`：stage 1 = `4N + 1.5N = 5.5N`。
3. **观察现象**：`16N / 5.5N ≈ 2.9`，即接近 3 倍显存节约。
4. **预期结果**：你会得出「ZeRO-1 在 8 卡下把每卡训练显存压到约原来的 1/3」的结论；卡数越多，`12N/P` 项越小，收益越接近 `4N` 这个下限（参数+梯度无法再省）。注意：本结论只针对「优化器状态」这项，**梯度和参数在 stage 1 仍是完整的**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 ZeRO-1 的梯度缓冲（`grads_memory`）仍是 `N` 大小，而不是 `N/P`？
**答**：因为反向传播在每卡上对完整参数计算梯度，需要完整大小的缓冲来 `+=` 累积；reduce-scatter 只是「在完整缓冲里就地写出本卡分片」，并未缩小缓冲。梯度缓冲的缩小要等 stage 2（未实现）。

**练习 2**：若 `total_parameters` 不能被 `num_processes` 整除，传 `-z 1` 会怎样？
**答**：`set_zero_configs` 检测到 `total_parameters % num_processes != 0`，打印「Can't equally partition parameters」并把 `zero_stage` 强制改为 0，退回纯 DDP。

**练习 3**：ZeRO-1 的 AdamW 更新完成后，为什么还要 `ncclAllGather`？
**答**：每卡只更新了自己负责的参数分片，其他分片还是旧值；为了让下一步前向时每卡都持有「完整且一致的新参数」，必须 all-gather 把各卡的新分片拼回完整参数。

---

### 4.4 梯度累积与全局 batch：把通信、分片串进训练主循环

#### 4.4.1 概念说明

到这里，前三个模块分别讲了「数据怎么分」「梯度怎么合并」「优化器状态怎么省」。本模块把它们**按时间顺序**串成一个外层训练步，并解释两个常被混淆的概念：

- **micro-batch（小批）**：一次前向/反向实际喂进模型的 `B*T` 个 token。受显存限制，`B` 不能任意大。
- **全局 batch（total batch）**：你「希望」每步更新看到的 token 总数，可以远大于显存能装下的 micro-batch。

当显存装不下全局 batch 时，就**把全局 batch 拆成若干个 micro-batch，依次做前向/反向、梯度先在本地累加，最后一次性更新**——这就是**梯度累积（gradient accumulation）**。llm.c 把三个维度乘起来定义全局 batch：

\[
\text{total\_batch\_size} = \underbrace{\text{grad\_accum\_steps}}_{\text{累积步数}} \times \underbrace{B \times T}_{\text{单卡 micro-batch}} \times \underbrace{P}_{\text{进程数}}
\]

#### 4.4.2 核心流程

一个外层训练步（处理 `total_batch_size` 个 token）的时间线：

```
for micro_step in [0 .. grad_accum_steps):
    dataloader_next_batch()                     # 每卡取自己那份数据
    gpt2_forward()                              # 前向
    gpt2_backward_and_reduce(grad_accum_steps, micro_step)
        │
        ├─ 若 micro_step==0：清零 losses 与 grads（因为要 += 累积）
        ├─ dloss = 1 / (B*T*grad_accum_steps)   # 关键缩放，保证累积后是「全体均值」
        ├─ 反向逐层算梯度，写入 grads_memory（用 +=）
        └─ 若 last_step（最后一个小批）：
              ├─ 跨卡 all-reduce / reduce-scatter 梯度   ← 通信只在这一步发生
              └─ 跨卡平均 loss
# 累积结束后：
grad_norm = gpt2_calculate_grad_norm()          # 注意 ZeRO-1 下只算本卡分片
gpt2_update(...)                                # AdamW（ZeRO-1 下只更新本卡分片 + all-gather）
```

两个关键不变量：

1. **跨卡通信只发生在最后一个 micro-step**（`last_step`）。前几个 micro-step 的梯度只是在本卡 `+=` 累积，不通信——因为必须等所有 micro-step 的梯度加完，再跨卡平均，结果才正确。
2. **`dloss = 1/(B*T*grad_accum_steps)`** 保证累积 + 平均后，梯度恰为「`total_batch_size` 个 token 上的均值」。这个缩放因子藏在反向的入口（[train_gpt2.cu:L819](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L819)）。

简单推导：micro-step `m`、进程 `r` 的梯度为 \(g_{m,r}=\frac{1}{B\cdot T\cdot A}\frac{\partial L_{m,r}}{\partial\theta}\)（`A=grad_accum_steps`）。本卡累积 \(G_r=\sum_m g_{m,r}\)；跨 `P` 卡 all-reduce 取平均：

\[
G = \frac{1}{P}\sum_r G_r = \frac{1}{P\cdot B\cdot T\cdot A}\sum_{m,r}\frac{\partial L_{m,r}}{\partial\theta}
= \frac{1}{\text{total\_batch\_size}}\sum_{\text{所有 token}}\frac{\partial L}{\partial\theta}
\]

正是全体 token 上的均值梯度，与 `total_batch_size` 的定义一致。

#### 4.4.3 源码精读

先看 `main` 如何从命令行 `-d`（total batch size）算出 `grad_accum_steps`（[train_gpt2.cu:L1512-L1519](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1512-L1519)）：

```c
int tokens_per_fwdbwd = B * T * multi_gpu_config.num_processes; // 一个 micro-batch 跨所有卡的 token 数
if (total_batch_size == -1) { total_batch_size = tokens_per_fwdbwd; } // 默认不累积
assert(total_batch_size % tokens_per_fwdbwd == 0);
int grad_accum_steps = total_batch_size / tokens_per_fwdbwd;
```

中文说明：默认 `total_batch_size = B*T*P`，即 `grad_accum_steps=1`（无累积）；用户用 `-d` 指定更大的值，例如 `-d 2097152`（约 2M token），就会算出多步累积。注意 `total_batch_size` 必须能被 `tokens_per_fwdbwd` 整除，否则 `assert` 失败。

训练主循环就是一个 `for` 套住前向+反向（[train_gpt2.cu:L1826-L1836](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1826-L1836)）：

```c
// gradient and loss accumulation loop over micro-batches
for (int micro_step = 0; micro_step < grad_accum_steps; micro_step++) {
    dataloader_next_batch(&train_loader);
    gpt2_forward(&model, train_loader.inputs, B, T);
    gpt2_backward_and_reduce(&model, train_loader.inputs, train_loader.targets,
                             grad_accum_steps, micro_step);
}
```

进入 `gpt2_backward_and_reduce`（[train_gpt2.cu:L788-L802](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L788-L802)）：第一个 micro-step 清零、判定是否最后一步：

```c
bool last_step = micro_step == grad_accum_steps - 1;
if (micro_step == 0) {
    cudaCheck(cudaMemsetAsync(model->acts.losses, 0, ... ));   // 损失累积缓冲清零
    cudaCheck(cudaMemsetAsync(model->grads_memory, 0, ... ));  // 梯度累积缓冲清零
}
```

逐层反向里，**仅当 `last_step` 才触发跨卡规约**——对每层的 12 个参数梯度张量调用 `multi_gpu_async_reduce_gradient`（[train_gpt2.cu:L929-L947](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L929-L947)）：

```c
if(last_step) {
    floatX* const pointers[] = { dl_ln1w, dl_ln1b, dl_qkvw, dl_qkvb, dl_attprojw, dl_attprojb,
                                 dl_ln2w, dl_ln2b, dl_fcw, dl_fcb, dl_fcprojw, dl_fcprojb };
    const size_t nelem[] = { C,C, 3*C*C,3*C, C*C,C, C,C, 4*C*C,4*C, C*4*C,C };
    multi_gpu_async_reduce_gradient(pointers, nelem, &multi_gpu_config, main_stream);
}
```

`encoder_backward` 之后的非分层参数（`wte`/`wpe`/`lnfw`/`lnfb`）和 loss 也在 `last_step` 规约（[train_gpt2.cu:L952-L965](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L952-L965)）。注意 loss 的跨卡平均用的是 `ncclFloat`（loss 是 fp32 标量）：

```c
global_sum_deterministic(model->accumulated_mean_loss, acts.losses, B*T, main_stream);
#if MULTI_GPU
ncclCheck(ncclAllReduce(model->accumulated_mean_loss, model->accumulated_mean_loss,
                        sizeof(float), ncclFloat, ncclAvg, multi_gpu_config.nccl_comm, main_stream));
#endif
```

只有 `last_step` 才有意义明确的 `mean_loss`，否则置 `-1`（[train_gpt2.cu:L967-L972](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L967-L972)）——这就是为什么日志只在每步（外层）打印一次 loss。

最后看一个 ZeRO-1 的精妙之处：`gpt2_calculate_grad_norm` 在 stage 1 下**只对本卡分片求平方和，再跨卡相加**，而不能像 DDP 那样对全缓冲求（[train_gpt2.cu:L1002-L1023](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1002-L1023)）。源码注释把原因说得很清楚：

```c
if (multi_gpu_config->zero_stage == 1) {
    // because of the ncclReduceScatter() in backward,
    // grads_memory only contains the averaged gradients at the local shards,
    // so we only calculate the grad norm at the grads_memory belonging to the local shards
    for (int i = 0; i < NUM_PARAMETER_TENSORS; i++) {
        ShardInfo tensor = gpt2_get_tensor_at_layer(model, 0, i);
        ShardInfo shard = multi_gpu_get_shard_offset(tensor.size, multi_gpu_config, 1);
        ...
    }
    ...
    ncclCheck(ncclAllReduce(grad_norm_squared, ..., ncclSum, ...)); // 把各卡分片的平方和再加起来
}
```

中文说明：reduce-scatter 之后非本卡段是过期的，所以求全局梯度范数只能先把每卡「本卡分片的平方和」算出来，再用一次 `ncclAllReduce(ncclSum)` 把这些部分和加成全局值。这正是 4.2 里「reduce-scatter 后非本卡段过期」的直接后果。

#### 4.4.4 代码实践（命令行参数追踪）

1. **目标**：读懂命令行如何控制数据并行 + 梯度累积 + ZeRO。
2. **步骤**：打开 [scripts/multi_node/run_gpt2_124M_mpi.sh](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/scripts/multi_node/run_gpt2_124M_mpi.sh)，定位关键参数（[第 31-49 行](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/scripts/multi_node/run_gpt2_124M_mpi.sh#L31-L49)）：
   - `mpirun -np 16 --host h1:8,h2:8` → 2 节点共 16 进程（`P=16`）。
   - `-b 64 -t 1024` → 每 micro-batch 单卡 `64*1024 = 65536` token。
   - `-d 2097152` → 全局 batch 约 2M token。
   - `-z 1` → 开启 ZeRO-1。
   - `-pi "mpi"` → 用 MPI 交换 NCCL 唯一 ID（见 [u6-l5 多节点](u6-l5-multi-node.md)）。
3. **手算验证**：
   - `tokens_per_fwdbwd = 64*1024*16 = 1048576`。
   - `grad_accum_steps = 2097152 / 1048576 = 2`。
   - 即每个外层步做 2 次 micro-batch 前向/反向，第 2 次才跨 16 卡规约。
4. **观察现象**：脚本注释里 `# 16 进程 / ZeRO-1 / 2 步累积` 三件事互不冲突，可以同时开启。
5. **预期结果**：能口述「`-d` 越大、`P` 越小，累积步数越多；跨卡通信每 `grad_accum_steps` 个 micro-batch 只发生一次」。**待本地验证**（需要真实多 GPU 集群）。

#### 4.4.5 小练习与答案

**练习 1**：如果 `total_batch_size` 不能被 `B*T*P` 整除会怎样？
**答**：`assert(total_batch_size % tokens_per_fwdbwd == 0)` 失败，程序退出。因为 `grad_accum_steps` 必须是整数，否则无法均分累积步数。

**练习 2**：为什么跨卡梯度规约放在 `last_step`，而不是每个 micro-step 都规约？
**答**：每个 micro-step 各卡只持有一部分梯度的累加；必须等所有 micro-step 的梯度在本卡加完，再做一次跨卡平均，才等价于「全体 token 上的均值梯度」。每个 micro-step 都规约会多花 `grad_accum_steps` 倍通信量且结果相同。

**练习 3**：`mean_loss` 为什么在非 `last_step` 设为 `-1`？
**答**：只有最后一个 micro-step 才会把累积的 loss 跨卡平均并拷回 host；中间步的 loss 不完整，置 `-1` 作为「尚无意义」的哨兵，避免日志误用。

---

## 5. 综合实践

本综合实践把本讲三个模块串起来，回答两个核心问题（也是本讲的实践任务）。

### 任务：追踪一个分布式训练步，并解释 ZeRO-1 的显存收益

**背景**：假设你在 4 卡机器上跑 `train_gpt2cu`，BF16 + master weights 开启，GPT-2 124M（约 124M 参数，`N≈1.24e8`）。

**第一问：DataLoader 如何保证 4 卡看到不同数据？**

1. 读 [llmc/dataloader.h:L156-L157](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/dataloader.h#L156-L157) 与 [L203-L219](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/dataloader.h#L203-L219)。
2. 用你自己的话写一段：rank `r` 在样本 `idx` 处的字节偏移公式，并解释为什么 4 个 rank 的偏移互不重叠。
3. **要点**：`local_batch_offset_bytes = r*B*T*2` 让每卡错开一个 `B*T` 的窗口；`total_batch_size_bytes = P*B*T*2` 让下一个样本跳过所有卡的总消耗。本质上是对 token 流做「每 P 段分给 P 张卡」的均匀切片。

**第二问：`-z 1`（ZeRO-1）为什么能降低单卡显存？**

1. 读 [train_gpt2.cu:L397-L408](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L397-L408)：确认 `m_memory`/`v_memory`/`master_weights` 都按 `shard_num_parameters = N/P` 分配。
2. 读 [llmc/zero.cuh:L565-L574](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/zero.cuh#L565-L574)：确认 stage 1 下 `shard_num_parameters = total_parameters / num_processes`。
3. 用 4.3.4 的公式定量计算 `P=4` 时每卡显存：stage 0 = `16N`；stage 1 = `4N + 12N/4 = 7N`；节约约 56%。
4. **要点**：ZeRO-1 **只分片优化器状态**（m、v、master），参数和梯度仍是完整的；数学上靠 reduce-scatter（合并梯度）+ all-gather（散播新参数）保证与 DDP 等价。

**第三问（进阶）：把通信时机画出来。**

画一条时间线，标注：micro-batch 0（反向、本地 `+=`、无通信）→ micro-batch 1…→ 最后一个 micro-batch（反向、`multi_gpu_async_reduce_gradient` 触发跨卡 reduce-scatter/all-reduce）→ `gpt2_calculate_grad_norm`（stage 1 下再来一次 all-reduce 求全局范数）→ `gpt2_update`（stage 1 下 all-gather 散播新参数）。检查你画的时间线是否与 [train_gpt2.cu:L1829-L1852](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1829-L1852) 一致。

> 若无多卡环境，本实践以「源码阅读 + 手算」为主；结论性的显存数字与通信时机可在单卡 `num_processes=1` 下编译运行确认程序不报错（此时所有 NCCL 路径走「早退」分支）。

## 6. 本讲小结

- llm.c 的多 GPU 是**数据并行**：`DataLoader` 用 `process_rank`/`num_processes` 把 token 流切成互不重叠的分片，每卡喂不同数据。
- 跨卡梯度合并靠 NCCL：stage 0 用 `ncclAllReduce`（每卡拿到完整平均梯度），stage 1 用 `ncclReduceScatter`（每卡只拿本卡分片）；两者都被 `multi_gpu_async_reduce_gradient` 用 `ncclGroupStart/End` 合并提交，并用 event 做异步流同步。
- **ZeRO 当前只支持 stage 0 和 stage 1**：`set_zero_configs` 会把 stage 2/3 或不可整除的 stage 1 静默降级为 0。stage 1 只分片优化器状态（`m`/`v`/`master` 按 `N/P` 分配），参数和梯度仍完整。
- ZeRO-1 的数学等价性靠「reduce-scatter 合并梯度 + 各卡更新本卡分片 + all-gather 散播新参数」三步保证；通信量与 DDP 相当，收益纯来自显存。
- 梯度累积用 `total_batch_size = grad_accum_steps * B * T * P` 把全局 batch 拆成多个 micro-batch；`dloss = 1/(B*T*grad_accum_steps)` 保证累积 + 平均后恰为全体均值梯度；跨卡通信只在最后一个 micro-step 发生。
- ZeRO-1 下 `grads_memory` 非本卡段在 reduce-scatter 后过期，所以 `gpt2_calculate_grad_norm` 只算本卡分片的平方和、再跨卡 `ncclSum`。

## 7. 下一步学习建议

- 阅读 [u6-l5 多节点训练：MPI / TCP / FS 三种初始化](u6-l5-multi-node.md)，看 `multi_gpu_config_init` 如何用三种方式交换 NCCL 唯一 ID，把本讲的「单机多卡」扩展到「多机多卡」。
- 想理解 ZeRO-1 更底层的通信实现，可对照 NCCL 官方文档看 ring all-reduce 与 reduce-scatter 的算法复杂度，体会「为什么 ZeRO-1 通信量与 DDP 相当」。
- 建议继续阅读 [llmc/zero.cuh](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/zero.cuh) 全文（尤其是 `multi_gpu_barrier`、`multi_gpu_cpu_float_sum` 这两个辅助集合操作），它们在验证 loss 聚合（[train_gpt2.cu:L1727](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1727)）中被用到。
- 若你对「为什么 stage 2/3 能进一步省通信和显存」感兴趣，可阅读 ZeRO 原论文（arXiv:1910.02054），再回过头体会本仓库「只做到 stage 1」的工程取舍。
