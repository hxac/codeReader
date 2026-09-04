# Kernel 2 架构：warp 专用化、共享存储与双流水线

## 1. 本讲目标

本讲是 Kernel 2（`_flash_kda_fwd_recurrence`）的**架构总览**：我们暂不深究每个相位算了什么（那是 u3-l4/u3-l5 的内容），而是回答三个结构性问题：

1. 一个 192 线程的 CTA 如何被切成 4 个 MMA warp + 1 个 LOAD warp + 1 个 STORE warp，各自跑哪段代码？
2. 约 98 KB 的共享内存（SharedStorageK2）是如何划分的？`state_acc`、多级 `input[]/output[]` 缓冲与 `state_fp32_buf` 的 union 复用依据是什么？
3. 两条流水线（`PipelineTmaAsync` 装载流 / `PipelineAsync` 存储流）是如何构造的？`transaction_bytes`、`num_consumers`、`producer_arv_count` 这些参数各自锁定什么行为？

学完本讲，你应该能独立画出 Kernel 2 的「warp 角色 × 代码区间 × 数据流」全景图，并能手工推算任意 `InputStages/OutputStages` 配置下的共享内存字节数。

## 2. 前置知识

本讲默认你已读过 u2-l8（workspace 契约）与 u2-l4（CuTe 布局）。以下概念用通俗语言再过一遍：

- **warp**：GPU 的最小执行单位，32 个线程总是锁步执行同一条指令。一个 CTA（线程块）由若干 warp 组成，Kernel 2 的 CTA 是 192 线程 = 6 个 warp。
- **warp 专用化（warp specialization）**：不再让所有 warp 执行同一份代码，而是给不同 warp 分配不同角色——有的专职搬数据（LOAD）、有的专职算（MMA）、有的专职写回（STORE）。这是 Hopper 上隐藏全局内存延迟的主流写法：搬数据和算数据在不同 warp 里**并行**进行。
- **多级缓冲（multi-stage buffer / stage 环形队列）**：LOAD warp 往 smem 搬第 t+2 块数据的同时，MMA warp 在算第 t 块、STORE warp 在写第 t-1 块的输出。为此 smem 里要备 `InputStages` 份输入缓冲和 `OutputStages` 份输出缓冲，像传送带一样轮转。
- **TMA（Tensor Memory Accelerator）**：SM90 引入的异步批量拷贝引擎。一条指令搬运一个 tile，由硬件完成，不占用线程的算力。TMA 拷贝的完成通过 **事务 barrier（transaction barrier）** 通知：预先声明「本 barrier 期待 N 字节」，TMA 每搬完一段就累加，攒满 N 字节 barrier 自动翻转。
- **CUTLASS Pipeline**：把「stage 环形队列 + empty/full barrier + 生产者/消费者角色」封装成 `cutlass::PipelineTmaAsync`（生产者是 TMA，完成信号由硬件给）和 `cutlass::PipelineAsync`（纯软件到达计数，完成信号由线程自己 arrive）。
- **proxy（代理）与围栏**：SM90 上普通指令（LDSM/STSM 等）走 generic proxy，TMA 走 async proxy。两个 proxy 看到的 smem 写入互不保证立即可见，跨越边界时需要 `fence_view_async_shared()` 打通可见性（u2-l6 已见过同款围栏）。
- **NamedBarrier**：`__syncthreads()` 的「只同步部分线程」版本——指定线程数与 barrier 编号，只有同一组线程参与。warp 专用化架构里**不能**随手用 `__syncthreads()`，原因见 4.1.2 的死锁论证。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注的行区间 |
| --- | --- | --- |
| `csrc/smxx/fwd_kernel2.cuh` | Kernel 2 全部实现：布局别名、共享存储结构、warp 分工、两条流水线、初始状态加载、MMA 主体、STORE 主体 | L9-L70（K2Layouts）、L72-L112（SharedStorageK2）、L169-L213（角色判定与 pipeline 构造）、L324-L838（三个角色的执行体） |
| `csrc/smxx/utils.cuh` | 公共工具：WarpRole 枚举、make_load_pipeline / make_store_pipeline 工厂 | L79-L84、L86-L116、L118-L143 |
| `csrc/smxx/fwd_launch.cu` | host 侧启动：stage 数常量、线程数、smem 尺寸、grid/block | L29-L31、L183-L216 |

永久链接使用的 HEAD 均为 `7afb9f4`。

## 4. 核心概念与源码讲解

本讲的三个最小模块：**warp 角色划分**、**SharedStorageK2 布局与 union**、**两条 pipeline 的构造**。

### 4.1 模块一：warp 角色划分

#### 4.1.1 概念说明

Kernel 2 的并行轴是 `(序列 seq_idx, 头 head_idx)`（grid 为 `(N, H)`，见 [csrc/smxx/fwd_launch.cu:L203-L204](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L203-L204)）。**一个 CTA 独占一个 (序列, 头)，在序列内部沿 tile 串行递推**——这是 K2 与 K1（海量 CTA 全并行）的本质区别。既然序列内是串行流水，单个 CTA 的吞吐就取决于流水线效率，于是作者把 CTA 切成三种角色：

- **4 个 MMA warp**（128 线程）：干所有的数学。每 warp 负责输出/状态矩阵的 2 个 16 列块（4 warp × 32 列 = 128 列 = D）。
- **1 个 LOAD warp**（32 线程）：专职发起 TMA 装载，把 v、beta 和 workspace 六个中间量搬进 smem。
- **1 个 STORE warp**（32 线程）：专职把输出 tile（和最终状态）写回 gmem。

为什么 LOAD/STORE 各只给 1 个 warp？因为 TMA 是硬件引擎，**发起**拷贝只需要极少的线程（实际只有 1 个 lane 在发指令），搬运本身不消耗 SM 算力；算力全留给 MMA。192 = 128 + 32 + 32 这个配比就是「计算:搬运 = 2:1:1 warp」的取舍。

#### 4.1.2 核心流程

CTA 内的分工判定与整体时间线：

```
blockIdx.x = seq_idx, blockIdx.y = head_idx        grid = (N, H)
block = 192 线程 = 6 warp

warp 0..3 (线程   0..127)  → MMA   : 状态加载后进入 t 循环，Phase1..Phase6
warp 4   (线程 128..159)  → LOAD  : 初始状态 TMA → t 循环预取 8 份输入
warp 5   (线程 160..191)  → STORE : t 循环写回 out → 最终状态 TMA

时间线（InputStages=3, OutputStages=2）:
t      :    0         1         2         3      ...
LOAD   : [装 s0]   [装 s1]   [装 s2]   [装 s0'] ...
MMA    :           [算 s0]   [算 s1]   [算 s2]  ...
STORE  :                     [写 o0]   [写 o1]  ...
```

三组角色靠两条 pipeline 传递数据（LOAD→MMA 装载流，MMA→STORE 存储流），MMA 是中间枢纽：装载流的消费者、存储流的生产者。

**为什么 MMA warp 之间还需要一个 NamedBarrier？** 输出路径（Phase1 双 GEMM）中每个 warp 读 `s_acc` 的**列**块 `warp_id*2`、`warp_id*2+1`（跨全部 128 行），而状态更新（Phase6）通过转置视图每个 warp 写 `s_acc` 的**行**块（跨全部 128 列）。因此「第 t+1 轮读取的状态」依赖「全部 4 个 warp 完成第 t 轮 Phase6 的写回」——这是一个跨 warp 的生产者-消费者依赖，必须显式同步。

**为什么不能用 `__syncthreads()` 做这个同步？** `__syncthreads()` 要求 CTA 内全部 192 线程到达。但三组角色进度天然错开：LOAD warp 会在 `producer_acquire` 处等待 stage 空闲（等 MMA release），STORE warp 会在 `consumer_wait` 处等待输出就绪（等 MMA commit）。若 MMA 在循环末尾改用 `__syncthreads()`，就会形成循环等待：LOAD/STORE 在 pipeline 上等 MMA，MMA 在 `__syncthreads()` 上等 LOAD/STORE——**死锁**。所以只能用 `NamedBarrier` 把同步范围收缩到 128 个 MMA 线程，让 LOAD/STORE 留在各自的 pipeline 等待点上。

#### 4.1.3 源码精读

角色枚举定义在 utils.cuh，四种取值：

```cpp
enum class WarpRole {
    MMA,
    LOAD_QKG,
    STORE,
    NonParticipant,
};
```

见 [csrc/smxx/utils.cuh:L79-L84](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L79-L84)。`NonParticipant` 是「本 warp 不参与该 pipeline」的显式标记（构造 pipeline 时会用到）。

CTA 内的角色判定是一段朴素的区间 if-else：

```cpp
int warp_id = threadIdx.x / kWarpSize;
WarpRole warp_role = WarpRole::NonParticipant;
if (warp_id < kComputeThreads / kWarpSize) {          // warp 0..3
    warp_role = WarpRole::MMA;
} else if (warp_id < kComputeThreads / kWarpSize + 1) { // warp 4
    warp_role = WarpRole::LOAD_QKG;
} else if (warp_id < kComputeThreads / kWarpSize + 2) { // warp 5
    warp_role = WarpRole::STORE;
}
```

见 [csrc/smxx/fwd_kernel2.cuh:L189-L197](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L189-L197)，其中 `kWarpSize = 32`、`kComputeThreads = 128` 定义在 [csrc/smxx/fwd_kernel2.cuh:L169-L170](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L169-L170)。线程总数由 host 侧给出：`constexpr int kK2Threads = 32 * 2 + 128;`（= 192），见 [csrc/smxx/fwd_launch.cu:L186](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L186)。

判定之后，整段 kernel 就是三个互斥的大 `if`，各自是独立的小程序：

| 角色 | warp / 线程 | 执行体行区间（fwd_kernel2.cuh） | 职责 |
| --- | --- | --- | --- |
| LOAD_QKG | warp 4 / 128..159 | [L324-L422](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L324-L422) | 每 stage 聚合发起 8 份 TMA 拷贝（v、beta、workspace 六量）；外加初始状态加载 [L241-L304](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L241-L304) 与最终 fp32 状态 TMA [L818-L834](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L818-L834) |
| MMA | warp 0..3 / 0..127 | [L426-L743](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L426-L743) | t 循环：等输入 → Phase1 双 GEMM → Phase2-5 输出路径 → Phase6 状态更新 → 同步释放 |
| STORE | warp 5 / 160..191 | [L746-L802](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L746-L802) | 每 stage 写回 out tile（整块 TMA / 尾块手写）+ bf16 最终状态 TMA |

MMA 主体开头的 NamedBarrier 与循环骨架：

```cpp
if (warp_role == WarpRole::MMA) {
    cutlass::arch::NamedBarrier compute_barrier(kComputeThreads, 0);
    ...
    for (int t = 0; t < t_tiles; ++t) {
        store_pipeline.producer_acquire(out_write);   // 等输出 stage 空闲
        load_pipeline.consumer_wait(load_read);       // 等输入 stage 就绪
        ...  // Phase 1..6
        compute_barrier.arrive_and_wait();            // 4 个 MMA warp 互相同步
        cutlass::arch::fence_view_async_shared();     // generic 写 → async proxy 可见
        store_pipeline.producer_commit(out_write);    // 输出就绪，通知 STORE
        load_pipeline.consumer_release(load_read);    // 输入 stage 释放，通知 LOAD
        ++load_read; ++out_write;
    }
}
```

分别见 [csrc/smxx/fwd_kernel2.cuh:L426-L431](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L426-L431)（NamedBarrier 构造与 pipeline 状态变量）、[csrc/smxx/fwd_kernel2.cuh:L434-L443](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L434-L443)（循环头的 acquire/wait）、[csrc/smxx/fwd_kernel2.cuh:L733-L741](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L733-L741)（循环尾的 barrier/围栏/commit/release）。`NamedBarrier(kComputeThreads, 0)` 的两个参数是「参与线程数 128」和「barrier 编号 0」——只同步 4 个 MMA warp，LOAD/STORE 不参与，这正是避免 4.1.2 中死锁的关键。

顺带一提，kernel 签名上的 `__launch_bounds__(NumThreads)` 见 [csrc/smxx/fwd_kernel2.cuh:L133](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L133)（与 K1 固定 256 线程 8 CTA 的 occupancy 策略不同，K2 每线程寄存器预算更宽裕，靠大 smem 天然限制为每 SM 1~2 个 CTA）。

#### 4.1.4 代码实践

**实践：用源码区间验证角色划分**

1. **实践目标**：确认三组角色的线程区间与代码区间，画出角色图。
2. **操作步骤**：
   - 打开 `csrc/smxx/fwd_kernel2.cuh`，跳到 L189-L197，代入 `threadIdx.x = 137`（137/32 = warp 4）验证它被判为 `LOAD_QKG`；再代入 `threadIdx.x = 200`（warp 6）验证 `NonParticipant`（192 线程的 CTA 里 warp 6 不存在，这个分支只是防御式写法）。
   - 用编辑器折叠功能核对三个 `if` 体各自的行范围，与上表对号。
3. **需要观察的现象**：三个角色体之间没有互相嵌套；LOAD 与 STORE 体内部都出现 `lane_predicate`（`elect_one_sync()`，见 [csrc/smxx/fwd_kernel2.cuh:L237](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L237)）——即每个搬运 warp 里真正发指令的只有 1 个 lane。
4. **预期结果**：你能在纸上默画出「warp 0-3 MMA / warp 4 LOAD / warp 5 STORE」及其代码区间。本实践为源码阅读型，无需运行 GPU。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `kComputeThreads` 从 128 改成 160（5 个 MMA warp），Phase1 的列块划分 `warp_id * 2 + i`（每 warp 2 个 16 列块）还能覆盖 N=128 吗？

**答案**：不能。代码假设「warp 数 × 2 × 16 = D」，即 4 × 32 = 128。5 个 warp 需要 160 列，与 D=128 不符；改成每 warp 1.6 个列块无法整数化。要支持 5 warp 必须重写列块映射并让某些 warp 少处理一个块，破坏了「每 warp 恰好 2 块」的寄存器分块假设（`u_acc[2]`、`out_acc[2]` 等定长数组）。

**练习 2**：为什么 LOAD warp 发 TMA 用 `elect_one_sync()` 只让一个 lane 干活，而不是全 warp 32 个 lane 一起发？

**答案**：TMA 指令（如 `cp.async.bulk.tensor`）是单线程语义——一条指令描述一整块 tile 的搬运。多个 lane 重复发同一拷贝反而会造成重复搬运与事务字节重复计数，破坏 barrier 的字节数账本。所以约定「1 个 lane 发指令、事务字节数一次声明」，全 warp 只共享地址计算。

**练习 3**：论证：若把 L733 的 `compute_barrier.arrive_and_wait()` 换成 `__syncthreads()`，程序必然死锁。

**答案**：`__syncthreads()` 需要全部 192 线程到达同一屏障。取 t=0 时刻：MMA 在循环末尾等待全体；LOAD warp 已在 `producer_acquire` 上等待 stage 空闲（InputStages=3，最多装 3 个 stage 后阻塞，等待 MMA `consumer_release`）；STORE warp 在 `consumer_wait` 上等待第一个输出 commit（依赖 MMA 完成 t=0）。三方形成「MMA 等 LOAD/STORE 到 syncthreads、LOAD 等 MMA release、STORE 等 MMA commit」的环形等待，满足死锁条件。`NamedBarrier` 只汇聚 128 个 MMA 线程，不牵连另外两个 warp，故安全。

### 4.2 模块二：SharedStorageK2 布局与 union 复用

#### 4.2.1 概念说明

warp 专用化要求数据在 smem 里「各就各位」：MMA 要的输入放输入缓冲、写完的输出放输出缓冲、递推状态常驻不放。SharedStorageK2 就是这块约 98 KB smem 的**类型化蓝图**，它回答四个问题：

1. **常驻区**：`state_acc`（128×128 bf16 状态矩阵）贯穿整个 t 循环，每轮被读（Phase1）、被写（Phase6），绝不能被流水线缓冲覆盖——它被放在 union 之外。
2. **流水线区**：`input[InputStages]` 每份含 8 个成员（v、beta、k_decayed、q_decayed、k_restored、g_total、INV、Mqk——正好是 LOAD warp 每轮搬的全部家当）；`output[OutputStages]` 每份一个 out tile。
3. **union 复用**：fp32 状态转换缓冲 `state_fp32_buf` 只在「流水线启动前」（fp32 初始状态→bf16）和「流水线全部结束后」（bf16 最终状态→fp32）使用，与流水线缓冲的生命周期**不相交**，因此放进同一个 union 共享同一块内存。
4. **同步原语区**：两条 pipeline 各自的 barrier 数组 + 初始状态加载用的独立事务 barrier。

union 复用是本结构最精妙的一笔：fp32 状态路径需要 64 KB 的转换缓冲，但只在头尾各用一次；若单独分配，smem 总量要多出 64 KB，可能直接把 occupancy 压死或超出 227 KB 硬限。

#### 4.2.2 核心流程

先做字节对账（CHUNK=16, D=128，`cosize` 即 CuTe 布局的线性缓冲长度，u2-l4 已讲）：

| 成员 | 布局 | cosize（元素） | 字节 |
| --- | --- | --- | --- |
| `state_acc` | StateSmemLayout = 128×128 bf16 | 16384 | 32768 |
| `input.v` | VOLayout = 16×128 bf16 | 2048 | 4096 |
| `input.beta` | BetaSmemLayout = 32 bf16 | 32 | 64 |
| `input.k_decayed` / `q_decayed` / `k_restored` | MMALayout = 16×128 bf16 各一 | 2048 ×3 | 4096 ×3 |
| `input.g_total` | GTotalLayout = 128 fp32 | 128 | 512 |
| `input.INV` / `Mqk` | LMLayout = 16×16 bf16 各一 | 256 ×2 | 512 ×2 |
| **InputStorage 合计** | | | **17984** |
| `output.out` | VOLayout = 16×128 bf16 | 2048 | 4096 |
| `state_fp32_buf` | cosize(StateSmemLayout) × sizeof(float) | — | 65536 |

三个交叉验证：

- **17984 = workspace 每 tile 13824 + v 4096 + beta 64**。K1 写入 workspace 的六量共 13824 字节（u2-l8），K2 的输入缓冲把这六量原样收下，另加本轮才需要的 v 与 beta——数字严丝合缝。
- **17984 同时是装载流的事务字节数** `kTmaTransactionBytes`（见 4.3.3）。
- **union 尺寸**：

\[
\text{union} = \max\big(\underbrace{17984\,S_{in}}_{\text{input 数组}} + \underbrace{4096\,S_{out}}_{\text{output 数组}},\ \underbrace{65536}_{\text{state\_fp32\_buf}}\big)
\]

代入基线 \((S_{in}, S_{out}) = (3, 2)\)：\(\max(62144,\ 65536) = 65536\)。总 smem ≈ 32768 + 65536 + pipeline/障碍字节 ≈ **98.3 KB**（与 u2-l5 说的「约 98 KB 动态 smem」一致）。

由此推出一个反直觉结论：**把 stage 数从 (3,2) 降到 (2,2)，union 仍是 65536**——因为 `state_fp32_buf` 把下限钳住了。只有流水线缓冲超过 64 KB（例如 (4,3) 时 84224 字节）union 才会被撑大。这个「钳制效应」是第 5 节综合实践的核心观察点。

生命周期图（union 安全性的依据）：

```
时间 ──────────────────────────────────────────────────▶
[fp32 初始状态: TMA→state_fp32_buf, 全线程转 bf16→state_acc]   ← 循环前
__syncthreads (L321)
[流水线主循环: input[]/output[] 轮转, t = 0..t_tiles-1]        ← union 的 A 面
__syncthreads (L809, 仅 fp32 输出路径)
[bf16 最终状态: state_acc 全线程转 fp32→state_fp32_buf, TMA 写回] ← union 的 B 面
```

#### 4.2.3 源码精读

结构定义全貌（节选）：

```cpp
template <class Layouts, int InputStages, int OutputStages>
struct SharedStorageK2 {
    ...
    alignas(128) cute::ArrayEngine<BF16, cute::cosize_v<StateSmemLayout>> state_acc;

    struct InputStorage {
        alignas(128) cute::ArrayEngine<BF16, cute::cosize_v<VOLayout>> v;
        alignas(128) cute::ArrayEngine<BF16, cute::cosize_v<BetaSmemLayout>> beta;
        alignas(128) cute::ArrayEngine<BF16, cute::cosize_v<MMALayout>> k_decayed;
        ...  // q_decayed / k_restored / g_total(fp32) / INV / Mqk
    };

    struct OutputStorage {
        alignas(128) cute::ArrayEngine<BF16, cute::cosize_v<VOLayout>> out;
    };

    union {
        struct {
            InputStorage input[InputStages];
            OutputStorage output[OutputStages];
        };
        alignas(128) char state_fp32_buf[cute::cosize_v<StateSmemLayout> * sizeof(float)];
    };

    typename cutlass::PipelineTmaAsync<InputStages>::SharedStorage load_pipeline;
    typename cutlass::PipelineAsync<OutputStages>::SharedStorage store_pipeline;
    alignas(16) cutlass::arch::ClusterTransactionBarrier state_acc_tma_barrier;
};
```

见 [csrc/smxx/fwd_kernel2.cuh:L72-L112](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L72-L112)。逐段说明：

- **L82** `state_acc`：32768 字节常驻区，独立于 union，理由见 4.2.1。
- **L84-L93** `InputStorage`：八字段全部 `alignas(128)`——每个字段起点都是 128 字节对齐，这是 TMA 拷贝对 smem 目标地址的对齐要求，也让每份 InputStorage 恰好 17984 字节无内部空洞（17984 = 128 × 140.5？不——17984/128 = 140.5，**不是** 128 的倍数！但因字段间 alignas 填充，v(4096)+beta(64)+k_decayed(4096)+q_decayed(4096)+k_restored(4096)+g_total(512)+INV(512)+Mqk(512) 各字段尺寸均为 128 的倍数或 64 字节（beta 需对齐填充 64 字节），数组元素间由编译器补齐到 128 的倍数，即每份实际占 18048 字节。**这里请以 `--ptxas-options=-v` 或第 5 节的手算+ncu 实测为准，标注：待本地验证**）。
- **L95-L97** `OutputStorage`：单个 out tile，4096 字节。
- **L101-L107** **匿名 union**：A 面是流水线缓冲数组，B 面是 65536 字节的 fp32 转换缓冲。注释原文写明设计意图："pipeline buffers share space with fp32 state conversion buffer. FP32 state load/store happens before/after the pipeline loop, so no overlap."
- **L109-L110** 两条 pipeline 的 SharedStorage：CUTLASS pipeline 自带的 barrier 数组（每个 stage 一对 empty/full barrier，各 8 字节量级）。
- **L111** `state_acc_tma_barrier`：初始状态加载专用的独立事务 barrier（不复用装载流的 barrier，因为初始状态加载发生在流水线构造之前/之外）。

union 安全性的代码证据链（fp32 输入路径在循环**前**）：

```cpp
} else if constexpr (HasStateIn && StateFP32) {
    ...  // TMA 载入 state_fp32_buf，然后：
    smem_cvt_fp32_to_bf16<...>(reinterpret_cast<float*>(shared_storage.state_fp32_buf),
                               shared_storage.state_acc.begin(), threadIdx.x);
    __syncthreads();   // 转换完成，此后 state_fp32_buf 不再被读
}
```

见 [csrc/smxx/fwd_kernel2.cuh:L267-L304](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L267-L304)。注意 TMA 写入的是 union 的 B 面，而此时流水线尚未启动（LOAD warp 的预取循环在 [L320-L423](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L320-L423)，之前有 [L321](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L321) 的 `__syncthreads()` 隔离），A 面还没有任何消费者。

fp32 输出路径在循环**后**（全部 warp 到齐之后才动 union 的 B 面）：

```cpp
if constexpr (HasStateOut && StateFP32) {
    __syncthreads();  // all warps sync — pipeline smem now free   ← 注释原文
    smem_cvt_bf16_to_fp32<...>(shared_storage.state_acc.begin(),
                               reinterpret_cast<float*>(shared_storage.state_fp32_buf), threadIdx.x);
    cutlass::arch::fence_view_async_shared();
    __syncthreads();  // conversion complete
    ...  // STORE warp 发 TMA
}
```

见 [csrc/smxx/fwd_kernel2.cuh:L804-L835](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L804-L835)。这里 `__syncthreads()` 此刻是安全的：所有角色都已退出各自的 t 循环（STORE warp 的输出循环在 [L750-L784](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L750-L784) 内完成，LOAD warp 已 `producer_tail`），不再有 pipeline 等待点。

最后，host 侧把 `sizeof(SharedStorageK2T)` 作为动态 smem 尺寸传入并 opt-in 上限，见 [csrc/smxx/fwd_launch.cu:L187-L201](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L187-L201)：

```cpp
using SharedStorageK2T = SharedStorageK2<K2L, kInputStages, kOutputStages>;
int smem_size_k2 = sizeof(SharedStorageK2T);
...
cudaFuncSetAttribute(kernel2, cudaFuncAttributeMaxDynamicSharedMemorySize, smem_size_k2);
```

kernel 侧用 `extern __shared__` 接收并重解释为该类型，见 [csrc/smxx/fwd_kernel2.cuh:L184-L186](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L184-L186)。注意 `SharedStorageK2` 的模板参数只有 `Layouts/InputStages/OutputStages`，**不含 StateFP32**——所以 `state_fp32_buf` 对全部 14 份 kernel 实例都占着 64 KB，这是 union 钳制效应的根源。

#### 4.2.4 代码实践

**实践：手算三种 stage 配置的 smem 布局**

1. **实践目标**：用 4.2.2 的公式独立推算 (3,2)、(2,2)、(4,3) 三种配置下 union 与总 smem，预测哪两种的字节数相同。
2. **操作步骤**：
   - 按公式 \(\text{union} = \max(17984 S_{in} + 4096 S_{out}, 65536)\) 分别代入 (3,2)、(2,2)、(4,3)；
   - 总量按 `32768 (state_acc) + union + 约 0.2 KB (pipeline/障碍，待本地验证)` 估算；
   - 把结果写成表格，留到第 5 节综合实践与实测对账。
3. **需要观察的现象**：(2,2) 与 (3,2) 的 union 同为 65536（被 `state_fp32_buf` 钳制）；(4,3) 的 union 增至 84224，总量增加约 18.7 KB。
4. **预期结果**：三行表格；结论「减少 stage 数不一定省 smem」。若你的手算与后续 ptxas/ncu 实测不符，优先怀疑 InputStorage 的 alignas 填充（4.2.3 提到的 18048 疑点），标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `state_acc` 不能也放进 union 与 `state_fp32_buf` 复用？两者尺寸恰好都是「状态矩阵」量级。

**答案**：生命周期不相交才能 union。`state_acc` 从初始状态加载一直活到最终状态写回，贯穿整个流水线主循环；而流水线缓冲（union 现在的 A 面）也在主循环期间活跃。若 `state_acc` 与 `state_fp32_buf` 同 union，则 A 面（流水线缓冲）与 B 面（两个状态缓冲）都必须与 `state_acc` 错峰，但 A 面与 `state_acc` 恰恰同峰（都被主循环使用），必然互相踩踏。`state_fp32_buf` 只在头尾使用，是唯一与 A 面生命周期错开的候选。

**练习 2**：`state_fp32_buf` 的尺寸为什么写成 `cosize_v<StateSmemLayout> * sizeof(float)`？

**答案**：fp32 转换缓冲要装下与 bf16 状态矩阵**同元素数**（16384 个）的 fp32 数据，即 16384 × 4 = 65536 字节。借用 bf16 布局的 cosize 表示元素个数、再乘 sizeof(float) 换算字节，避免重复定义一个 128×128 的 fp32 形状常量。

**练习 3**：若把 `kInputStages` 提到 8、`kOutputStages` 提到 5，smem 总量约多少？还能在 SM90 上启动吗？

**答案**：union = max(8×17984 + 5×4096, 65536) = max(164352, 65536) = 164352；总量 ≈ 32768 + 164352 + ε ≈ 197 KB，仍低于 SM90 单 CTA 227 KB 上限，`cudaFuncSetAttribute` 可以成功——但每 SM 只能驻留 1 个 CTA 且 smem 接近打满，寄存器压力与 L2 局部性未必划算。这解释了作者选 (3,2) 的保守取向：够深的预取 + 克制的 smem。

### 4.3 模块三：两条 pipeline 的构造

#### 4.3.1 概念说明

warp 专用化后，「LOAD 装完了没有」「MMA 算完了没有」这类握手不能靠轮询标志位手写，CUTLASS 提供了两类流水线原语，本 kernel 各用一条：

- **装载流 `cutlass::PipelineTmaAsync<InputStages>`**：连接 LOAD warp（生产者）与 4 个 MMA warp（消费者）。它的「数据就绪」信号**由 TMA 硬件给出**——生产者只负责 `arrive_and_expect_tx(N)`（声明期待 N 字节），TMA 每完成一段拷贝就向事务 barrier 记账，攒满 N 字节 barrier 翻转、消费者醒来。所以它的关键参数是 `transaction_bytes`。
- **存储流 `cutlass::PipelineAsync<OutputStages>`**：连接 4 个 MMA warp（生产者）与 STORE warp（消费者）。这里没有 TMA 参与握手（TMA 只在消费者侧执行写回），「输出写完」必须由 MMA 线程自己 arrive 计数，所以它的关键参数是**到达计数**：`producer_arv_count`（多少生产者线程 arrive 算一次写完）与 `consumer_arv_count`（多少消费者线程 arrive 算一次释放）。

两条 pipeline 方向相反的角色分配是本架构的对称美：MMA 在装载流是消费者、在存储流是生产者，是整条流水线的中枢。

#### 4.3.2 核心流程

装载流的每 stage 生命周期：

```
LOAD warp (leader lane):
  producer_acquire(state)         → 等 empty barrier（该 stage 的旧数据已被消费）
  producer_get_barrier(state)     → 取该 stage 的事务 barrier
  arrive_and_expect_tx(17984)     → 声明本 stage 期待 17984 字节
  发起 8 份 TMA copy              → 硬件异步搬 17984 字节
  ++state

TMA 硬件: 每段拷贝完成 → 向事务 barrier 记账 → 攒满 17984 字节 → full barrier 翻转

MMA warps (128 线程):
  consumer_wait(state)            → 等 full barrier（数据就绪）
  ... 计算 ...
  consumer_release(state)         → 128 线程 collective arrive → empty barrier 翻转
  ++state
```

存储流的每 stage 生命周期：

```
MMA warps (128 线程):
  producer_acquire(state)         → 等 empty（STORE 已释放该输出 stage）
  ... STSM 写 output[state].out ...
  producer_commit(state)          → 128 线程各 arrive 一次（producer_arv_count=128）
  ++state

STORE warp (leader lane):
  consumer_wait(state)            → 等 full（128 次 arrive 攒齐）
  TMA store / 手写尾块
  consumer_release(state)         → 1 线程 arrive（consumer_arv_count=1）
  ++state
```

注意两个不对称：装载流的完成由**硬件字节计数**触发，存储流的完成由**软件线程计数**触发；装载流的消费者有 128 个线程，存储流的消费者只有 1 个 leader lane。

#### 4.3.3 源码精读

**事务字节数的编译期公式**（装载流的总账）：

```cpp
constexpr uint32_t kTmaTransactionBytes =
    uint32_t(cute::cosize_v<VOLayout>) * uint32_t(sizeof(BF16)) +        // v
    uint32_t(32) * uint32_t(sizeof(BF16)) +                              // beta
    uint32_t(cute::cosize_v<MMALayout>) * uint32_t(sizeof(BF16)) * 3 +   // kd/qd/kr
    uint32_t(cute::cosize_v<GTotalLayout>) * uint32_t(sizeof(float)) +   // g_total
    uint32_t(cute::cosize_v<LMLayout>) * uint32_t(sizeof(BF16)) * 2 +    // INV, Mqk
    0u;
```

见 [csrc/smxx/fwd_kernel2.cuh:L172-L181](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L172-L181)。代入 4.2.2 的 cosize：4096 + 64 + 12288 + 512 + 1024 = **17984**，与 InputStorage 合计逐字节一致——这不是巧合，而是「一个 stage 的缓冲大小 = 一个 stage 的事务字节数」的必然（`TMA_DISABLE_ALL` 消融时整个表达式塌缩为 `0u`）。

**kernel 内构造两条 pipeline**：

```cpp
LoadPipeline load_pipeline = make_load_pipeline<InputStages>(
    shared_storage.load_pipeline,
    kTmaTransactionBytes,
    warp_role, 1, kComputeThreads          // (role, num_producers=1, num_consumers=128)
);
StorePipeline store_pipeline = make_store_pipeline<OutputStages>(
    shared_storage.store_pipeline,
    warp_role, kComputeThreads, 1          // (role, num_producers=128, num_consumers=1)
);
```

见 [csrc/smxx/fwd_kernel2.cuh:L199-L213](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L199-L213)。实参顺序里藏着角色映射：装载流的 num_producers=1（LOAD warp 的 leader lane 一个线程负责 arrive），num_consumers=128（4 个 MMA warp 集体消费）；存储流正好倒过来。

**工厂函数之一：make_load_pipeline**（utils.cuh）：

```cpp
auto role = Pipeline::ThreadCategory::NonParticipant;
bool is_leader = false;
if (warp_role == WarpRole::LOAD_QKG) {
    role = Pipeline::ThreadCategory::Producer;
    is_leader = cute::elect_one_sync();     // 只有 leader lane 负责 expect_tx
} else if (warp_role == WarpRole::MMA) {
    role = Pipeline::ThreadCategory::Consumer;
}

params.transaction_bytes = transaction_bytes;
params.role = role;
params.is_leader = is_leader;
params.num_consumers = num_consumers;
params.num_producers = num_producers;

Pipeline pipeline(storage, params, Shape<_1,_1>{});
cutlass::pipeline_init_wait(1);
return pipeline;
```

见 [csrc/smxx/utils.cuh:L86-L116](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L86-L116)（params 填充在 [L107-L111](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L107-L111)）。要点：

- `transaction_bytes` 告诉 pipeline：事务 barrier 每轮期待多少字节，TMA 完成的记账以此为准；
- `is_leader` 只在 LOAD warp 内由 `elect_one_sync()` 选出 1 个 lane——后续 `producer_acquire` 内部的 `arrive_and_expect_tx` 只由 leader 执行（呼应 4.1 的单发模式）；
- `num_consumers=128` 决定 `consumer_release` 需要 128 个 MMA 线程集体到达才翻转 empty barrier；
- `Shape<_1,_1>` 表示不做 cluster 维度扩展（单 CTA 语义）；
- `pipeline_init_wait(1)` 等待 barrier 初始化在全体线程间可见（与 K1 里 `fence_barrier_init` 的动机同类）。

**工厂函数之二：make_store_pipeline**：

```cpp
params.role = role;
params.producer_arv_count = num_producers;   // = 128
params.consumer_arv_count = num_consumers;   // = 1
```

见 [csrc/smxx/utils.cuh:L118-L143](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L118-L143)。注意参数名换了：`PipelineAsync` 没有 TMA，没有 transaction_bytes，只有**纯到达计数**——producer 侧 128 线程每人 arrive 一次攒齐 `producer_arv_count=128` 才宣告「输出写完」；consumer 侧 STORE warp 的 1 个线程 arrive 即算「输出 stage 已释放」。STORE warp 侧对应代码在 [csrc/smxx/fwd_kernel2.cuh:L781-L783](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L781-L783)（`tma_store_wait<0>()` 后 `consumer_release`）。

**生产者侧（LOAD warp）如何使用装载流**：

```cpp
for (int t = 0; t < t_tiles; ++t) {
    load_pipeline.producer_acquire(load_write);
    LoadBarrierType* tma_barrier = load_pipeline.producer_get_barrier(load_write);
    int stage = load_write.index();
    int ws_idx = head_idx * total_tiles + tile_base + t;
    // ... 8 份 cute::copy(tma_xxx.with(*tma_barrier), ...) 逐一登记 ...
    ++load_write;
}
load_pipeline.producer_tail(load_write);
```

见 [csrc/smxx/fwd_kernel2.cuh:L346-L351](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L346-L351)（循环头）与 [csrc/smxx/fwd_kernel2.cuh:L419-L421](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L419-L421)（推进与收尾）。`tma_barrier` 通过 `.with(*tma_barrier)` 绑进每份拷贝，8 份拷贝**共享同一个事务 barrier**——这正是「一次声明 17984 字节、多份拷贝共同记账」的完成语义。`load_write` 是 `make_producer_start_state` 产生的流水线状态（stage 环形游标），`producer_tail` 在循环后做生产者侧的优雅退出。

消费者侧（MMA）与存储流生产者侧的对接已在 4.1.3 引用（[L434-L443](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L434-L443)、[L733-L741](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L733-L741)）：`consumer_wait(load_read)` 等数据、`producer_commit(out_write)` 交输出、`consumer_release(load_read)` 还输入 stage。`fence_view_async_shared()` 夹在 NamedBarrier 与 commit 之间，把 MMA 的 STSM 写（generic proxy）对 STORE warp 的 TMA 读（async proxy）打通可见。

**STORE warp 消费存储流**：

```cpp
for (int t = 0; t < t_tiles; ++t) {
    store_pipeline.consumer_wait(out_read);
    int stage = out_read.index();
    int actual_len = min(CHUNK, seq_len - t * CHUNK);
    if (actual_len < CHUNK) { /* 单线程逐元素写尾块 */ }
    else { /* TMA 整块 store + tma_store_arrive */ }
    tma_store_wait<0>();
    store_pipeline.consumer_release(out_read);
    ++out_read;
}
```

见 [csrc/smxx/fwd_kernel2.cuh:L750-L784](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L750-L784)。尾块分支的完整分析留给 u3-l7，这里只需看到它是流水线消费者循环里的一次双分支。

#### 4.3.4 代码实践

**实践：追踪一个 stage 的完整旅程**

1. **实践目标**：沿代码走一遍 stage 0 从「被装载」到「被释放」的全部状态迁移。
2. **操作步骤**：
   - 在 [L347](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L347)（acquire）、[L349](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L349)（get_barrier）、[L358](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L358)（第一份 copy）做记号；再在 [L437](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L437)（consumer_wait）、[L738](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L738)（consumer_release）做记号；
   - 写一段 10 行左右的伪代码，只含这 5 个锚点与 `++load_write`，标注每步之后该 stage 的 empty/full 状态。
3. **需要观察的现象**：`load_write` 与 `load_read` 两个游标各自独立推进，靠 InputStages=3 个 buffer 的环形缓冲解耦；若把伪代码里 release 延后一轮，LOAD 最多能领先 MMA 3 个 stage。
4. **预期结果**：伪代码与 4.3.2 的流程图一致。本实践为源码阅读型，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `kTmaTransactionBytes` 里的 `* 2`（INV、Mqk 两项）误写成 `* 1`，会发生什么？

**答案**：事务 barrier 每轮只期待 17984 − 512 = 17472 字节。TMA 实际搬 17984 字节，记账会提前攒满期待值（硬件按 arrive 顺序记账），full barrier 可能在 Mqk 拷贝尚未完成时就翻转——MMA 读到 Mqk 的陈旧/未定义数据，产出错误结果；或者（取决于记账顺序）出现字节账目错位、后续 stage 的 barrier 语义紊乱。总之是静默的数据竞争，编译期无法捕获——这正是事务字节数必须与缓冲清单严格对账的原因（4.3.3 的 17984 对账不是装饰）。

**练习 2**：存储流的 `producer_arv_count=128`、`consumer_arv_count=1`，为什么不对称？

**答案**：生产者侧是 4 个 MMA warp 共 128 线程合作写一份输出（每个 warp 写自己的 2 个 16 列块），必须 128 线程全部 arrive 才能确认「整份输出写完」，所以计 128。消费者侧 STORE warp 内部只有 leader lane 一个线程执行 `consumer_wait/release`（TMA 单线程语义），所以计 1。数值恰好与线程职责对齐：谁真正参与握手，谁就计数。

**练习 3**：`PipelineTmaAsync` 与 `PipelineAsync` 的本质区别是什么？为什么装载流必须用前者？

**答案**：`PipelineTmaAsync` 的 full barrier 是**事务 barrier**，由 TMA 硬件的字节记账驱动翻转——线程发出拷贝后即可离开，数据就绪信号与任何线程的执行无关；`PipelineAsync` 的 full/empty 都靠线程自身 arrive 计数翻转。装载流的生产者数据由 TMA 异象搬运，没有任何线程能在「搬运完成」时刻替它 arrive，所以必须用事务 barrier 让硬件报信。存储流的「输出写完」由 MMA 线程自己的 STSM 构成，天然有线程可以 arrive，用纯软件的 `PipelineAsync` 即可（也更轻量，不占事务 barrier 资源）。

## 5. 综合实践

**综合实践：InputStages/OutputStages 消融实验——smem 与性能的三点测量**

本实践把 4.2 的手算公式与 4.3 的流水线语义放到真实硬件上验证，并揭示 union 钳制效应。

1. **实践目标**：
   - 记录 (3,2)（基线）、(2,2)、(4,3) 三种配置下 K2 的 smem 字节数与端到端性能；
   - 验证「(2,2) 与 (3,2) 的 smem 相同」（union 被 `state_fp32_buf` 钳制）；
   - 体会 stage 深度对性能的影响方向。

2. **操作步骤**：
   - **基线测量**：确认已按 u1-l3 完成安装；跑一次基准留底：
     ```bash
     python benchmarks/bench_fwd.py --mode fixed --iters 100 --repeats 3 2>&1 | tee bench_32.log
     ```
     （bench_fn 用 cuda.Event 计时并输出 mean/min/max，见 [benchmarks/bench_fwd.py:L8-L30](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/benchmarks/bench_fwd.py#L8-L30)；参数定义在 [L148-L154](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/benchmarks/bench_fwd.py#L148-L154)。需要 `pip install flash-linear-attention` 提供 fla 对照。）
   - **改配置重编译**：编辑 [csrc/smxx/fwd_launch.cu:L29-L30](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L29-L30) 的 `kInputStages/kOutputStages` 为 `(2,2)`，重装：
     ```bash
     pip install --no-build-isolation -v . 2>&1 | tee build_22.log
     ```
     再改为 `(4,3)` 重复。（**实验后务必 `git checkout csrc/smxx/fwd_launch.cu` 恢复源码并重装基线**。）
   - **收集 smem 证据**（三条互补渠道）：
     1. `--ptxas-options=-v` 已在 [setup.py:L79](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py#L79) 默认开启，build log 里搜索 `_flash_kda_fwd_recurrence` 可读出各实例的寄存器数与静态 smem。**注意**：K2 的 smem 走 `extern __shared__` 动态分配，ptxas 报告的 4xx 字节只是静态部分，不随 stage 数显著变化——这是 ptxas 观察法的盲区；
     2. 临时插桩：在 [fwd_launch.cu:L188](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L188) 后加一行 `printf("[K2] smem_size_k2 = %d\n", smem_size_k2);`（host 侧合法，实验后撤掉），每次安装后随便跑一次 `pytest tests/test_fwd.py -x -k varlen` 触发打印；
     3. ncu 复核：`ncu --metrics launch__dynamic_shared_memory_per_block python -c "..."`（ncu 用法详见 u3-l10 与 `benchmarks/ncu.sh` 的 `-k` 正则过滤）。
   - **性能测量**：每种配置各跑一次第 1 步的 bench 命令，另跑 `--mode varlen` 一组（varlen 的尾块路径会放大 stage 深度的敏感性）。
   - **正确性回归**：每种配置跑 `pytest tests/test_fwd.py -x`，确认 bit-exact 仍通过（stage 数不应影响数值，只影响调度）。
3. **需要观察的现象**：
   - smem：(3,2) ≈ (2,2) < (4,3)，差值约 18.7 KB（84224 − 65536，再考虑 4.2.3 提到的 alignas 填充，实测差值可能略有出入——如实记录）；
   - 寄存器：ptxas -v 中三种配置应基本持平（stage 数只改 smem，不改寄存器分块）；
   - 性能：三种配置的 mean/min 差异（方向待本地验证：(2,2) 预取深度浅一级，长序列上可能略慢；(4,3) smem 变大但 occupancy 已是每 SM 1 个 CTA，可能持平）。
4. **预期结果**：一张三行对照表（配置 | smem 字节 | 寄存器 | fixed mean ms | varlen mean ms | 测试是否通过），外加两三句结论：union 钳制是否成立、stage 深度在你的硬件上的收益方向。若某项实测与手算不符，把差异写进实验记录（优先怀疑 alignas 填充与 pipeline SharedStorage 的精确字节数）。
5. 本实践涉及改源码与重编译，请在 git 干净的工作区进行，实验结束恢复现场。

## 6. 本讲小结

- Kernel 2 的 CTA 是 192 线程 = **4 个 MMA warp + 1 个 LOAD warp + 1 个 STORE warp**（[L189-L197](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L189-L197)），三个角色各跑互斥的代码区间；MMA warp 之间用 `NamedBarrier(128, 0)` 同步状态更新（[L427](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L427)、[L733](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L733)），不能用 `__syncthreads()`（会与 LOAD/STORE 的 pipeline 等待点死锁）。
- SharedStorageK2 分四区：常驻 `state_acc`(32 KB)、流水线 union 区（`input[3]`+`output[2]` 共 62144 字节，与 64 KB 的 `state_fp32_buf` 取 max=65536）、两条 pipeline 的 barrier 存储、初始状态专用事务 barrier；union 复用安全的前提是 fp32 状态转换严格发生在主循环前/后，由两处 `__syncthreads` 隔离。
- 一个 stage 的 InputStorage = **17984 字节** = workspace 六量 13824 + v 4096 + beta 64，同时就是装载流的事务字节数 `kTmaTransactionBytes`——缓冲清单与字节账目严格对账。
- 装载流用 `PipelineTmaAsync`（完成信号由 TMA 硬件字节记账给出，`num_producers=1` 的 leader lane 声明期待、`num_consumers=128` 集体释放）；存储流用 `PipelineAsync`（纯软件到达计数，`producer_arv_count=128`/`consumer_arv_count=1`）；MMA 是两条流的中枢：装载流的消费者、存储流的生产者。
- **union 钳制效应**：union 尺寸 = max(17984·S_in + 4096·S_out, 65536)，所以 (2,2) 与 (3,2) 的 smem 相同，(4,3) 才会撑大约 18.7 KB——减少 stage 数不一定省内存。

## 7. 下一步学习建议

本讲只搭了 Kernel 2 的「骨架」：谁在跑、数据放哪、如何握手。接下来按顺序填肉：

- **u3-l3（LOAD warp）**：细读 8 份 TMA 拷贝的清单与 1D beta 的 `& ~7` 对齐技巧，验证事务字节预算。
- **u3-l4（MMA 相位 1-5）**：本讲跳过的输出路径计算细节——双 GEMM、delta 修正、MOVM_T 寄存器转置、输出累加。
- **u3-l5（MMA 相位 6）**：状态更新的转置视图（`TransposedStateSmemLayout`/LDSM_T/STSM_T）与 PREFETCH 预取环——你会重新遇到本讲 4.1.2 埋下的「跨 warp 行/列依赖」问题。
- **u3-l7（STORE warp 与尾块）**：`actual_len < CHUNK` 时的逐元素回退分支为何是 varlen 正确性的关键。
- 若想先横向对照另一种 warp 专用化风格，可回头看 u2-l6 的 K1「单发 TMA + 全块 wait」模式，体会「海量 CTA 靠并行度隐藏延迟」与「单 CTA 流水线靠深度隐藏延迟」两种设计哲学。
