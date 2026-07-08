# Crossover 与 DSM / CTA cluster

## 1. 本讲目标

上一讲（u5-l2）我们用时钟周期证明：FP8 sparse 解码 kernel 是 **dequantization-bound**——反量化（约 50 cycle/token）比 64 头 MMA（约 34 cycle/token）更慢，Tensor Core 被迫空等。本讲讲解 FlashMLA 用来破局的核心技术 **crossover**，学完后你应当：

- 理解 **MQA 下两个 CTA 共享同一份 KV** 这一关键事实，以及它为什么让「各反量化一半」成为可能；
- 掌握 Hopper 的 **Distributed Shared Memory（DSM）** 与 **CTA cluster** 这一组硬件原语；
- 看懂 **cluster transaction barrier**（`st.async` + `mbarrier::complete_tx::bytes`）的同步语义；
- 能够跟踪 producer warpgroup「加载一半 → 反量化 → 本地写 + DSM 写给对端 → barrier 同步」的完整流水，并分析它对寄存器与 shared memory 压力的影响。

## 2. 前置知识

阅读本讲前，请先建立以下直觉（均来自 u5-l1、u5-l2）：

- **MQA 解码形态**：MLA 解码阶段表现为 Multi-Query Attention——128 个 query 头、1 个 key 头，`head_dim_k=576`、`head_dim_v=512`，K/V 同源。
- **FP8 KV cache 布局**：每 token 前 512 维 NoPE 做 tile 级 FP8 量化（配 scale），后 64 维 RoPE 保留 bf16 不量化。
- **反量化瓶颈**：H800 无法直接 fp8→bf16，反量化要走 `fp8→half→fp32→bf16→×scale` 四步链，单 token 约 50 cycle，反量化跑在 CUDA Core、MMA 跑在 Tensor Core，两者有数据依赖，故 `max(50,34)=50`，Tensor Core 每 token 空等约 16 cycle。
- **CTA 与 SM 的映射**：每个 CTA 跑在一个 SM 上，每个 SM 只映射一个 CTA；一个 CTA 负责 64 个 query 头。

还需要两个 CUDA 硬件术语（本讲会展开）：

- **CTA cluster（线程块集群）**：Hopper 起允许把多个 CTA 编为一个 cluster，cluster 内的 CTA 可以彼此直接访问对方的 shared memory。
- **Distributed Shared Memory（DSM）**：cluster 内跨 CTA 的 shared memory 逻辑视图，访问对端 smem 用一条特殊的 `st.async` 异步指令完成。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `docs/20250929-hopper-fp8-sparse-deep-dive.md` | 官方深度博客，crossover / DSM / cluster barrier 的设计动机与性能数据来源 |
| `csrc/sm90/decode/sparse_fp8/config.h` | 静态配置：`CLUSTER_SIZE`、`SharedMemoryPlan`（含三个 K barrier）、cluster 同步函数 |
| `csrc/sm90/decode/sparse_fp8/components/dequant.h` | FP8→bf16 反量化指令序列与 `load_128b_from_gmem` 带缓存的宽加载 |
| `csrc/sm90/decode/sparse_fp8/components/helpers.h` | DSM 原语：`st_async_128b`、`get_peer_addr`、`PEER_ADDR_MASK` |
| `csrc/sm90/decode/sparse_fp8/splitkv_mla.cuh` | 主 kernel：三个 warpgroup 分工、producer 的 crossover 流水、cluster 启动 |
| `csrc/kerutils/include/kerutils/device/sm90/intrinsics.cuh` | `barrier.cluster.arrive/wait` 等 cluster 屏障内联 PTX |
| `csrc/defines.h` | `transac_bar_t = cutlass::arch::ClusterTransactionBarrier` 类型别名 |

## 4. 核心概念与源码讲解

### 4.1 MQA 共享 KV 的机会：为何可以「各做一半」

#### 4.1.1 概念说明

破局的关键不在反量化本身，而在一句朴素的事实：**MQA 下，同一个 query token 的所有 query 头，注意到的都是同一个 key 头**。这意味着如果两个 CTA 分别处理不同的 query 头集合，它们需要用到的 K/V 是**完全相同**的。

DeepSeek-V3.2 共 128 个 query 头，每个 CTA 负责 64 个，于是天然需要恰好 2 个 CTA 来覆盖全部 128 个头。如果这 2 个 CTA 能「合伙」反量化同一份 KV——各做一半再交换——每个 CTA 的反量化工作量就减半。作者把这个想法命名为 **crossover**，灵感来自减数分裂中的「染色体交叉互换」（Chromosomal crossover）。

#### 4.1.2 核心流程

把一个 KV block（`TOPK_BLOCK_SIZE = 64` 个 token）拆成两半：

1. cluster 内 CTA 0（`idx_in_cluster = 0`）负责 token `[0, 32)`；
2. CTA 1（`idx_in_cluster = 1`）负责 token `[32, 64)`；
3. 各自反量化自己那一半，写入自己的 smem；
4. 同时把反量化结果通过 DSM 发给对端 smem；
5. 同步后，两个 CTA 的 smem 里都拥有完整 64 个 token 的反量化结果。

从周期角度看，每个 CTA 的反量化工作量从 64 个 token 砍到 32 个，相当于把上一讲的 50 cycle 降到约 25 cycle，**低于 MMA 的 34 cycle**，瓶颈从 dequant 翻转到 MMA，Tensor Core 不再空等。

#### 4.1.3 源码精读

crossover 是否开启，由 query 头数决定。在 [config.h:20-21](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/config.h#L20-L21)：

```cpp
static constexpr int NUM_M_BLOCKS = NUM_HEADS / 64;
static constexpr int CLUSTER_SIZE = NUM_M_BLOCKS;
```

即 `NUM_HEADS=128 ⇒ CLUSTER_SIZE=2`（开 crossover），`NUM_HEADS=64 ⇒ CLUSTER_SIZE=1`（不开，单 CTA 自己反量化全部）。kernel 启动时 cluster 维度直接取这个常量，见 [splitkv_mla.cuh:769-778](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/splitkv_mla.cuh#L769-L778)（`dim3(CLUSTER_SIZE, 1, 1)`）与 `__launch_bounds__` 第三参数 [splitkv_mla.cuh:681](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/splitkv_mla.cuh#L681)。

每个 CTA 在 cluster 中的编号在 [splitkv_mla.cuh:93](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/splitkv_mla.cuh#L93) 由 `head_block_idx`（即 blockIdx.x）算出：

```cpp
const int idx_in_cluster = CLUSTER_SIZE == 1 ? 0 : head_block_idx % 2;
```

producer warpgroup 用 `idx_in_cluster` 把自己负责的 token 区段错开半块，见 [splitkv_mla.cuh:475](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/splitkv_mla.cuh#L475)：

```cpp
nxt_token_indexs[round] = __ldg(gIndices + args.start_block_idx*TOPK_BLOCK_SIZE
                              + idx_in_cluster*(TOPK_BLOCK_SIZE/2)   // ← 错开 32 个 token
                              + round*NUM_TOKENS_PER_ROUND + my_token_idx_base);
```

`NUM_TOKENS_PER_ROUND = 32` 与 `CLUSTER_SIZE==2` 时 `NUM_TOKENS_PER_THREAD = 1` 的搭配，注释写得很直白——「head 是 128 时每个 CTA 反量化 32 个 token（1 轮）；head 是 64 时反量化 64 个 token（2 轮）」，见 [splitkv_mla.cuh:458-460](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/splitkv_mla.cuh#L458-L460)。

#### 4.1.4 代码实践

**实践目标**：确认「crossover 只在 128 头时开启」这一结论在编译期被钉死。

1. 打开 `csrc/sm90/decode/sparse_fp8/instantiations/` 目录，列出 4 个实例化文件；
2. 阅读 [v32_persistent_h128.cu:5](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/instantiations/v32_persistent_h128.cu#L5)，它实例化的是 `ModelType::V32, 128`；
3. 对照 config.h 的 `CLUSTER_SIZE = NUM_HEADS/64`，填表：

| 实例化文件 | NUM_HEADS | CLUSTER_SIZE | crossover? |
| --- | --- | --- | --- |
| `v32_persistent_h128.cu` | 128 | 2 | 是 |
| `v32_persistent_h64.cu` | 64 | 1 | 否 |
| `model1_persistent_h128.cu` | 128 | 2 | 是 |
| `model1_persistent_h64.cu` | 64 | 1 | 否 |

**需要观察的现象**：h128 与 h64 共用同一份 `splitkv_mla.cuh`，区别完全由模板参数 + `if constexpr (CLUSTER_SIZE == 2)` 编译期分支决定。

**预期结果**：4 个文件两两配对，只有 h128 走 crossover 路径。若你看到 h64 也启用 cluster，说明理解有误。

#### 4.1.5 小练习与答案

**练习 1**：如果某天 DeepSeek 把 query 头数从 128 改成 192，crossover 还能成立吗？

**参考答案**：`CLUSTER_SIZE = 192/64 = 3`。Hopper 的 cluster 最大支持 8 个 CTA（且受 GPC 边界限制，非幂次 cluster 需谨慎），数量上可行；但「各做一半」的对称交换会被打破（3 份不好均分），需要重新设计交换拓扑。本讲的 size=2 是最简单的成对交换。

**练习 2**：为什么 dense 解码 kernel（u3）没有用 crossover？

**参考答案**：dense 解码的 KV 是 bf16，不需要反量化，根本不存在 dequantization-bound，也就没有「分享反量化成果」的动机。crossover 是 FP8 场景专属。

---

### 4.2 DSM 与 CTA cluster：跨 CTA 共享显存的硬件基础

#### 4.2.1 概念说明

「各做一半」的算盘打得再好，也得有办法把数据在两个 CTA 之间搬运。在 Hopper 之前，CTA 之间交换数据只能走 global memory 或 L2 cache，延迟高、带宽低，得不偿失。Hopper 引入了两样东西让这件事变得划算：

- **CTA cluster**：把最多 8 个 CTA 编为一个集群，由硬件保证它们被调度到同一个 GPC（GPU Processing Cluster）上、物理上彼此邻近；
- **Distributed Shared Memory（DSM）**：cluster 内每个 CTA 看到的不只是自己的 smem，还有一个**跨 CTA 的逻辑地址空间**——访问对端 CTA 的 smem 只需把本地 smem 地址的最高位翻转，再用一条 `st.async` 指令即可，数据不必绕道 global memory。

#### 4.2.2 核心流程

DSM 写入的关键是地址换算与异步存储指令：

1. **地址换算**：对端 smem 地址 = 本地 smem 地址 `XOR` 一个掩码（`PEER_ADDR_MASK = 1 << 24`）。这个位翻转是 Hopper DSM 寻址的硬件约定。
2. **异步存储**：用 `st.async.shared::cluster.mbarrier::complete_tx::bytes` 指令，把 128 位数据写入对端 smem，**同时**累加完成字节数到对端的一个 mbarrier 上。指令一经发射即返回，不阻塞生产者。
3. **同步**：消费端在 mbarrier 上等待「字节凑齐」（详见 4.3）。

整条交换路径都停留在片上 smem 与集群互联网络，**不经过 global memory**，这是 DSM 比传统方案快的根本原因。

#### 4.2.3 源码精读

DSM 地址换算在 [helpers.h:102-107](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/components/helpers.h#L102-L107)：

```cpp
static constexpr int PEER_ADDR_MASK = 16777216; // peer_addr = my_addr ^ PEER_ADDR_MASK.
template<typename T>
T* get_peer_addr(T* p) {
    return (T*)((int64_t)(p) ^ PEER_ADDR_MASK);
}
```

> 注：`16777216 == 1<<24`。Hopper DSM 中，cluster 内 CTA 的 smem 地址由若干高位区分，翻转该位即指向「对端」CTA 同偏移处。`CLUSTER_SIZE=2` 时只有唯一对端，故一个 XOR 足矣。

128 位异步存储原语 `st_async_128b` 在 [helpers.h:77-88](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/components/helpers.h#L77-L88)，核心是一条内联 PTX：

```cpp
asm volatile (
    "st.async.weak.shared::cluster.mbarrier::complete_tx::bytes.v2.s64 [%0], {%1, %2}, [%3]; \n"
    : : "r"(dst_addr), "l"(data_long2.x), "l"(data_long2.y), "r"(mbar_addr));
```

- `st.async.weak.shared::cluster`：集群内异步 shared store（弱顺序，性能优先）；
- `mbarrier::complete_tx::bytes`：写入完成后**自动**把字节数计入目标 mbarrier 的 transaction 计数；
- `.v2.s64`：一次写 128 位（两个 64 位）。

> 该函数还在 [helpers.h:90-100](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/components/helpers.h#L90-L100) 提供了对应的 `cp.async.bulk.shared::cluster` 版本（按块拷贝），本 kernel 的反量化路径用的是逐 128 位的 `st_async_128b`。

#### 4.2.4 代码实践

**实践目标**：在纸上把 DSM 地址换算跑一遍，建立「翻转一位 = 跳到对端」的直觉。

1. 假设 CTA 0 某个 smem 变量的地址为 `0x????_????_0030`；
2. 套用 `get_peer_addr`，对端地址为 `0x????_????_0100_0030`（第 24 位翻转）；
3. 在 producer 代码里找到对端 smem 写入点的构造 [splitkv_mla.cuh:513](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/splitkv_mla.cuh#L513)（`sK_nope_peer_base = get_peer_addr(sK_nope_base)`），确认本地 slot 与对端 slot 偏移完全相同。

**需要观察的现象**：CTA 0 写自己 smem 的 slot `[0:32]`，同时把**同样的数据**写给对端 smem 的 slot `[0:32]`；CTA 1 写自己 smem 的 slot `[32:64]`，同时写给对端 slot `[32:64]`。两边各自只算一半，但交换后都拿到全部 64 个 slot。

**预期结果**：交换完成后，CTA 0 的 smem = slot[0:32]（本地）∪ slot[32:64]（来自 CTA1 的 DSM），CTA 1 对称。两份完整数据，各自只付出一半反量化代价。若无法在本机运行，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `st.async` 用 `.weak`（弱顺序）而非强顺序？

**参考答案**：弱顺序省去不必要的排序屏障，延迟更低；数据的可见性由目标 mbarrier 的 transaction 计数严格保证，不需要 store 指令自身做强排序。性能优先的选择。

**练习 2**：DSM 写入是否经过 L2 cache？

**参考答案**：不经过。DSM 走的是集群内 SM 间的专用互联（cluster fabric），数据停留在 shared memory 层面，既不污染也不依赖 L2。这也是它比「走 global memory 交换」快的根本原因。

---

### 4.3 cluster transaction barrier：异步交换的同步原语

#### 4.3.1 概念说明

异步写入了数据，消费端怎么知道「对端那一半到齐了」？普通的 mbarrier 只能数「到达次数」，数不了「字节数」。Hopper 提供了 **cluster transaction barrier（事务屏障）**：它除了到达计数，还有一路 **transaction byte count**（期望收到的字节数）。生产者每写一笔 `st.async` 都会扣减字节数；当「到达次数」与「字节数」同时满足，屏障翻转，消费端的 `wait` 才返回。

这套机制让「异步、批量、跨 CTA」的数据交换拥有了精确的完成语义——这正是 crossover 交换对端半块所需要的。

#### 4.3.2 核心流程

crossover 用到三个屏障（都在 `SharedMemoryPlan` 里，每个 K buffer 一份，`NUM_K_BUFS=2`）：

| 屏障 | 角色 | 初始值 | 谁到达 | 谁等待 |
| --- | --- | --- | --- | --- |
| `bar_k_local_ready` | 本地半块就绪 | 128（CLUSTER==2）/ 128（==1） | 本 CTA producer 全 128 线程各到达一次 | 本 CTA 消费者 wg0 |
| `bar_k_remote_ready` | 对端半块就绪（事务屏障） | 1 | 本 CTA 代表线程 `arrive_and_expect_tx(N)` + 对端 producer 的 `st.async` 累计 N 字节 | 本 CTA 消费者 wg0 |
| `bar_k_avail` | K buffer 可覆写 | 4（==2）/ 256（==1） | 消费者 wg0/wg1 读完后到达 | 本 CTA producer |

一次交换的时序（设 CTA A 与 CTA B 互为对端）：

1. **消费端预登记**：A 的代表线程对**自己**的 `bar_k_remote_ready[A]` 执行 `arrive_and_expect_tx(N)`——声明「我等 N 字节到达 + 1 次到达」；
2. **生产端 DSM 写**：B 的 producer 把 B 那一半反量化结果用 `st.async` 写进 A 的 smem，每笔同时累计字节数到 `bar_k_remote_ready[A]`；
3. **字节数凑齐**：当 N 字节写完，transaction 计数归零；加上步骤 1 的那 1 次到达，屏障翻转；
4. **消费端放行**：A 的 wg0 在 `bar_k_local_ready[A]`（本地半块好）与 `bar_k_remote_ready[A]`（对端半块好）上都 `wait` 通过后，开始 WGMMA。

其中 transaction 字节数为半块大小：`(TOPK_BLOCK_SIZE/2) × (HEAD_DIM_NOPE + HEAD_DIM_ROPE) × sizeof(bf16)`，即 32 个 token × 576 维 × 2 字节。

#### 4.3.3 源码精读

屏障类型与声明：`transac_bar_t` 在 [defines.h:8](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/defines.h#L8) 定义为 `cutlass::arch::ClusterTransactionBarrier`；`SharedMemoryPlan` 在 [config.h:102](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/config.h#L102) 持有这三个屏障数组。

初始化区分 cluster 与否，在 [splitkv_mla.cuh:116-133](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/splitkv_mla.cuh#L116-L133)：

```cpp
if constexpr (CLUSTER_SIZE == 2) {
    for (int i = 0; i < NUM_K_BUFS; ++i) {
        plan.bar_k_local_ready[i].init(128);   // 本地 producer 128 线程到达
        plan.bar_k_remote_ready[i].init(1);    // 事务屏障：1 次到达 + N 字节
        plan.bar_k_avail[i].init(4);
    }
} else {
    for (int i = 0; i < NUM_K_BUFS; ++i) {
        plan.bar_k_local_ready[i].init(128);
        plan.bar_k_avail[i].init(256);          // 单 CTA 路径无 remote_ready
    }
}
```

消费端预登记（在 producer 里、写之前由代表线程执行），见 [splitkv_mla.cuh:569-571](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/splitkv_mla.cuh#L569-L571)：

```cpp
if (CLUSTER_SIZE == 2 && round == 0 && idx_in_warpgroup == 0) {
    plan.bar_k_remote_ready[buf_idx].arrive_and_expect_tx(
        (TOPK_BLOCK_SIZE/2)*(HEAD_DIM_NOPE+HEAD_DIM_ROPE)*sizeof(bf16));
}
```

消费端（wg0）等待本地 + 对端两块都就绪，见 [splitkv_mla.cuh:225-228](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/splitkv_mla.cuh#L225-L228)：

```cpp
plan.bar_k_local_ready[buf_idx].wait(bar_phase_k>>buf_idx&1);
if constexpr (CLUSTER_SIZE == 2) {
    plan.bar_k_remote_ready[buf_idx].wait(bar_phase_k>>buf_idx&1);   // 仅 crossover 路径
}
```

> `bar_phase_k` 是一个手动维护的相位位（注释明确「不要用数组，避免落本地内存」），让 `wait` 在 ping-pong 的两个 buffer 之间正确翻转期望相位。

此外，每个 batch 循环结尾有一次 cluster 级全同步 `sync_all_threads_in_cluster()`（[splitkv_mla.cuh:669](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/splitkv_mla.cuh#L669)），其实现见 [config.h:144-152](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/config.h#L144-L152)：cluster 模式下用 `barrier.cluster.arrive/wait`（[intrinsics.cuh:57-65](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/kerutils/include/kerutils/device/sm90/intrinsics.cuh#L57-L65)），单 CTA 模式退化为 `__syncthreads`。

#### 4.3.4 代码实践

**实践目标**：把「事务屏障 = 到达计数 + 字节计数」这条规则在脑中跑通一次。

1. 假设半块需要传输 `N = 32 × 576 × 2 = 36864` 字节；
2. A 的代表线程执行 `arrive_and_expect_tx(36864)`：到达计数 `1→0`（满足），字节计数设为 36864；
3. B 的 producer 用 288 次 `st.async`（每次 128 字节）把这 36864 字节写进 A 的 smem，每次扣减字节计数；
4. 全部写完后字节计数归零——A 的 `bar_k_remote_ready[A].wait()` 放行。

**需要观察的现象**：到达计数与字节计数是**与**关系，两者都满足才翻转。即便 B 的 producer 全部写完，若 A 忘了 `arrive_and_expect_tx`（到达计数没满足），屏障也不会翻转，反之亦然。

**预期结果**：你能解释为什么 `bar_k_remote_ready.init(1)` 只需要「1 次到达」——因为那 1 次到达由 A 自己的代表线程在 `arrive_and_expect_tx` 里完成，剩下的纯粹靠字节数。若本机无法编译，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：如果 producer 漏发了一笔 `st.async`（实际写了 N-128 字节），会发生什么？

**参考答案**：字节计数停在 128，永远不归零，`bar_k_remote_ready.wait()` 死锁——consumer wg0 永远等不到对端半块。这是 transaction barrier 的典型死锁陷阱，字节预算必须与实际写入精确匹配。

**练习 2**：为什么单 CTA 路径（CLUSTER==1）不需要 `bar_k_remote_ready`？

**参考答案**：没有对端，所有 token 都自己反量化、写本地 smem，只需 `bar_k_local_ready`（producer→consumer 的本地就绪信号）即可，故 init 分支里 `else` 没有它。

---

### 4.4 各反量化一半并互换：producer 的完整流水

#### 4.4.1 概念说明

前面三模块分别是「动机」「硬件」「同步」。本模块把它们缝合成 producer warpgroup 的真实代码路径。回顾 kernel 的三个 warpgroup 分工：

- **wg0**（消费者，QK + online softmax）：寄存器最多（`warpgroup_reg_alloc<192>`）；
- **wg1**（消费者，PV）：中等寄存器（`warpgroup_reg_dealloc<160>`）；
- **wg2**（producer，反量化 + DSM 交换）：寄存器最少（`warpgroup_reg_dealloc<152>`）。

producer 每处理一个 KV block，做四件事：① 等该 buffer 可覆写（`bar_k_avail`）；② 从 gmem 宽加载自己那一半 token 的 fp8 KV；③ 在 CUDA Core 上反量化成 bf16；④ **同一份数据**既本地 store 进自己 smem，又 `st.async` 进对端 smem。最后到达 `bar_k_local_ready` 通知本地消费者。

注意第 ④ 步的关键：**反量化只做一次**，结果同时落本地与对端——这正是「工作量减半、覆盖不减半」的实现方式。

#### 4.4.2 核心流程

producer 处理一个 block（`process_one_block`）的伪代码：

```
wait bar_k_avail[buf]              # 该 K buffer 消费者已读完
if CLUSTER_SIZE==2 and round==0 and tid==0:
    bar_k_remote_ready[buf].arrive_and_expect_tx(N)   # 预登记收 N 字节

for t in my_half_tokens:           # 本 CTA 负责 32 个 token（idx_in_cluster 决定区间）
    # ---- NoPE 段（512 维 fp8，分 8 个 64 维子块）----
    for dim in 0..HEAD_DIM_NOPE/64:
        fp8x16 = load_128b_from_gmem(gK_nope + dim*64)   # 宽加载 + 缓存 hint
        scale  = scales[V32? dim/2 : dim]
        bf16x8 = cvt_fp8x8_bf16x8(fp8x16.lo, scale)      # 反量化（一次！）
        *(int128*)(sK_local + off)        = bf16x8        # 写本地 smem
        if CLUSTER_SIZE==2:
            st_async_128b(sK_peer + off, bf16x8, peer_bar)  # 同步写对端 smem
        (lo 与 hi 各一次)
    # ---- RoPE 段（64 维 bf16，直接搬，不量化）----
    for dim in 0..HEAD_DIM_ROPE/32:
        bf16x8 = load_128b_from_gmem(gK_rope + dim*32)
        *(int128*)(sK_local + off) = bf16x8
        if CLUSTER_SIZE==2: st_async_128b(sK_peer + off, bf16x8, peer_bar)

fence_view_async_shared()          # 让 async store 对后续同步可见
bar_k_local_ready[buf].arrive()    # 通知本地消费者：我这半好了
```

对端的 `bar_k_remote_ready` 则由对端 CTA 的 producer 在写 DSM 时累计字节、自动翻转。

#### 4.4.3 源码精读

producer 入口在 [splitkv_mla.cuh:454-456](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/splitkv_mla.cuh#L454-L456)（`else { // Producer warpgroup`）。对端屏障地址在循环外取一次，[splitkv_mla.cuh:507](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/splitkv_mla.cuh#L507)：

```cpp
transac_bar_t* peer_bar_k_remote_ready = get_peer_addr(&(plan.bar_k_remote_ready[buf_idx]));
```

反量化 + 双写的核心是 `dequant_and_save_bf16x8` lambda，[splitkv_mla.cuh:586-597](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/splitkv_mla.cuh#L586-L597)：

```cpp
auto dequant_and_save_bf16x8 = [&](const fp8x8 &data, int offset) {
    int smem_offset = (dim_idx*64 + offset) * TOPK_BLOCK_SIZE;
    bf16x8 cur_bf16x8 = cvt_fp8x8_bf16x8(data, __bfloat162bfloat162(*(__nv_bfloat16*)(&scale)));
    *(__int128_t*)(sK_nope_base + smem_offset) = *(__int128_t*)&cur_bf16x8;   // 本地写
    if constexpr (CLUSTER_SIZE == 2) {
        st_async_128b(sK_nope_peer_base + smem_offset, cur_bf16x8, peer_bar_k_remote_ready);  // DSM 写对端
    }
};
```

反量化指令本身在 [dequant.h:20-34](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/components/dequant.h#L20-L34)（`cvt_fp8x8_bf16x8`），即上一讲分析的四步链的紧凑实现。宽加载用 `load_128b_from_gmem`（[dequant.h:49-86](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/components/dequant.h#L49-L86)），带 `EVICT_LAST`/`L2::B256` 等缓存策略。

RoPE 段同样的双写模式见 [splitkv_mla.cuh:609-623](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/splitkv_mla.cuh#L609-L623)。循环结束后 `fence_view_async_shared()`（[splitkv_mla.cuh:626](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/splitkv_mla.cuh#L626)）确保 async 写入对后续 `bar_k_local_ready.arrive()` 可见，最后到达本地屏障 [splitkv_mla.cuh:645](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/splitkv_mla.cuh#L645)。

> **关于无效 token**：当 `token_index == -1`（无效索引），代码把 scale 置零、把加载结果清零（[splitkv_mla.cuh:577-595](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/splitkv_mla.cuh#L577-L595)），保证写入 smem（含对端）的是合法的零值，避免 DSM 交换出未初始化数据。

#### 4.4.4 代码实践

**实践目标**：用文字 + ASCII 示意图说清两个 CTA 的 crossover 交换，并分析对寄存器与 shared memory 压力的影响。

**操作步骤**：

1. 阅读官方博客对 crossover 的四步描述 [docs/20250929-hopper-fp8-sparse-deep-dive.md:39-45](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/docs/20250929-hopper-fp8-sparse-deep-dive.md#L39-L45)；
2. 对照本模块源码，画出下面的示意图并补全每一步对应的源码行号。

```
           Cluster (size=2) ── 跨 2 个 SM，共享同一份 KV（因 MQA）
 ┌──────────────────────────────┐         ┌──────────────────────────────┐
 │   CTA 0   idx_in_cluster=0   │         │   CTA 1   idx_in_cluster=1   │
 │   负责 query 头 [0,64)        │         │   负责 query 头 [64,128)      │
 ├──────────────────────────────┤         ├──────────────────────────────┤
 │ ① 加载 token[0:32] 的 fp8 KV  │         │ ① 加载 token[32:64] 的 fp8 KV │
 │ ② 反量化→bf16（只算一半）     │         │ ② 反量化→bf16（只算一半）     │
 │ ③a 本地 store → smem[0:32]    │         │ ③a 本地 store → smem[32:64]   │
 │ ③b st.async ───── DSM ──────────────────► smem[0:32] of CTA1           │
 │                              │ ◄────────── DSM ───── st.async ③b       │
 │                              │  smem[32:64] of CTA0                    │
 │ ④ 等 bar_k_local_ready        │         │ ④ 等 bar_k_local_ready        │
 │     + bar_k_remote_ready      │         │     + bar_k_remote_ready      │
 │   ⇒ smem 拥有完整 64 token    │         │   ⇒ smem 拥有完整 64 token    │
 │ ⑤ WGMMA QK/PV（Tensor Core）  │         │ ⑤ WGMMA QK/PV（Tensor Core）  │
 └──────────────────────────────┘         └──────────────────────────────┘
```

**需要观察/分析的现象**：

- **寄存器压力**：crossover **不增加**每线程寄存器负担。三 warpgroup 的寄存器预算是固定的（wg0 192 / wg1 160 / producer 152，见 [splitkv_mla.cuh:187](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/splitkv_mla.cuh#L187)、[367](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/splitkv_mla.cuh#L367)、[456](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/splitkv_mla.cuh#L456)）。producer 持有的 `bf16x8`、scale 等中间量与 cluster 大小无关；`if constexpr` 让 crossover 分支在 h64 时根本不编译。
- **shared memory 压力**：crossover **不减少** smem——每个 CTA 的 K buffer 仍是**完整 64 token × 576 维**（`SmemLayoutK`，见 [config.h:82](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/config.h#L82)），交换后两 CTA 各持一份完整副本。额外开销仅是 `NUM_K_BUFS` 个 `bar_k_remote_ready` 事务屏障占用的小块 smem。换言之，crossover 用「多占一个 SM + DSM 带宽 + 屏障延迟」换「反量化算力减半」，**换的是算力，不是显存**。
- **代价**：cluster 占用 2 个 SM（[splitkv_mla.cuh:772](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/splitkv_mla.cuh#L772) cluster 维度为 2），且每个 batch 末尾有一次 cluster 全同步（[splitkv_mla.cuh:669](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/splitkv_mla.cuh#L669)）。

**预期结果**：你能用一句话总结——crossover 是「以 2 倍 SM 资源 + DSM 同步开销，把单 CTA 的反量化算力瓶颈转移到 MMA」的工程取舍；它不省 smem、不省寄存器，省的是 dequant 的 cycle。若无 GPU，标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：crossover 交换后，CTA 0 和 CTA 1 的 smem 内容是否完全相同？

**参考答案**：K/V 的 smem 内容相同（都拥有完整 64 token 的反量化结果），因为 MQA 下两 CTA 处理的本来就是同一份 KV。不同的只是各自的 Q（各自 64 个头）和输出 O。这正是 MQA 让 crossover 成立的前提。

**练习 2**：为什么本地写用普通 `*(__int128_t*) = ...`，而对端写必须用 `st_async_128b`？

**参考答案**：本地 smem 写在同一 CTA 内，用普通同步 store 即可，靠 `bar_k_local_ready` 的到达计数同步；对端 smem 在另一 CTA，必须用集群异步 store 才能跨 SM 寻址，且需要把完成字节数累计到对端事务屏障（`bar_k_remote_ready`），所以非 `st.async ... complete_tx::bytes` 不可。

**练习 3**：若把 `NUM_K_BUFS` 从 2 改成 1，crossover 还能正确工作吗？

**参考答案**：原则上仍可工作（屏障逻辑与 buffer 数解耦），但失去 ping-pong 双缓冲——producer 写 buffer 时消费者无法同时读上一个 buffer，反量化与 MMA 无法重叠，性能会显著下降。`NUM_K_BUFS=2`（[config.h:34](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/config.h#L34)）是流水化的关键。

## 5. 综合实践

把本讲四模块串起来，完成一次「crossover 全链路走查」：

1. **定位开关**：从 `setup.py` 的源文件列表找到 `csrc/sm90/decode/sparse_fp8/instantiations/v32_persistent_h128.cu`，确认它实例化 `ModelType::V32, 128`，进而由 `CLUSTER_SIZE = 128/64 = 2` 判定 crossover 启用；
2. **跟踪一次 DSM 写**：在 `splitkv_mla.cuh` 的 producer 分支里，从 `get_peer_addr`（[L507](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/splitkv_mla.cuh#L507)）出发，跟踪一个 `dequant_and_save_bf16x8` 调用（[L586-597](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/splitkv_mla.cuh#L586-L597)），指出本地写与对端写分别落到哪两个地址、对端写的字节如何累计到 `peer_bar_k_remote_ready`；
3. **跟踪一次同步**：找到消费端 wg0 的 `wait`（[L225-228](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/sparse_fp8/splitkv_mla.cuh#L225-L228)），解释它为何需要同时等 `bar_k_local_ready` 与 `bar_k_remote_ready` 才能开始 QK 的 WGMMA；
4. **量化收益**：用 u5-l2 的周期模型（dequant 50 cycle/token，MMA 34 cycle/token），说明 crossover 把单 CTA 反量化降到约 25 cycle/token，从而让 `max(25,34)=34`，瓶颈回到 MMA。对照博客性能数据（410 TFLOPS vs 无 crossover 的 250 TFLOPS，[docs:48](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/docs/20250929-hopper-fp8-sparse-deep-dive.md#L48)）印证收益。

输出：一张「CTA 0 / CTA 1 × 加载 / 反量化 / 本地写 / DSM 写 / 屏障 / WGMMA」的时序对照表，并在表下用 2-3 句说明 crossover 的寄存器与 smem 取舍。

## 6. 本讲小结

- **crossover 的动机**是 MQA——同一 query token 的所有头共享同一份 KV，于是 2 个 CTA 可以各反量化一半再交换，把 dequantization-bound 翻转为 MMA-bound；
- **crossover 仅在 `NUM_HEADS=128`（`CLUSTER_SIZE=2`）时启用**，由 `CLUSTER_SIZE = NUM_HEADS/64` 与一串 `if constexpr (CLUSTER_SIZE == 2)` 编译期分支控制；
- **DSM + CTA cluster** 提供跨 CTA 共享 smem 的硬件通道：对端地址用 `PEER_ADDR_MASK`（`1<<24`）异或得到，数据用 `st.async.shared::cluster.mbarrier::complete_tx::bytes` 异步写入；
- **cluster transaction barrier** = 到达计数 + 字节计数，消费端 `arrive_and_expect_tx(N)` 预登记、生产端 `st.async` 自动扣字节，两者满足才放行，精确刻画了异步批量交换的完成语义；
- producer 对**同一份反量化结果**做「本地 store + DSM st.async」双写，反量化只算一次，工作量减半而覆盖不减半；
- crossover **不省 smem、不省寄存器**，省的是 dequant 的 cycle——代价是多占一个 SM 与 DSM/屏障同步开销。

## 7. 下一步学习建议

- 本讲聚焦 SM90（Hopper）FP8 sparse 解码的 crossover。下一讲 **u5-l4（Sparse decode 接口与 DecodeFeatures 派发）** 会从接口层说明 sparse decode 如何按 SM90/SM100、head64/head64x2/head128 选择实现，把本讲的 kernel 与上层派发框架对接；
- 若想横向对比，可先读 **u4-l2（Combine kernel）** 理解 crossover 主 kernel 写出的 accumulate 缓冲如何被 combine 归并；
- 对 DSM/cluster 想深入硬件细节，推荐 NVIDIA 博客 *Hopper Architecture In-Depth*（博客 [L37](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/docs/20250929-hopper-fp8-sparse-deep-dive.md#L37) 有链接）与 PTX 手册的 `st.async`、`mbarrier` 章节；
- 性能复盘可对照 **u8-l3（Benchmark 与性能调优）**，把本讲的 410 TFLOPS 数据放进多实现对比框架中理解。
