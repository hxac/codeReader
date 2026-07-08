# 静态配置 config.h 与 traits.h（GMMA）

## 1. 本讲目标

上一篇（u3-l1）我们从理论推导得出：MLA 解码在 \(h_q\cdot s_q \approx 128\) 时落进 compute-bound 区间，必须想办法「喂饱 Tensor Core」。从本篇开始，我们真正进入 SM90 dense decode kernel 的源码。

本篇只读两个小文件——`config.h` 和 `traits.h`，它们是整个 kernel 的「静态蓝图」：把 tile 大小、head 维度、要用哪几条 WGMMA 指令、shared memory 怎么摆、用什么 barrier 同步，全部在**编译期**钉死。读懂它们，下一篇（u3-l3）讲 seesaw 调度与 TMA 流水时你才不会迷路。

学完本篇你应当能：

- 说出 `config.h` 里 `BLOCK_SIZE_M / PAGE_BLOCK_SIZE / HEAD_DIM_K / HEAD_DIM_V` 各自的几何含义与取值依据。
- 理解 Hopper 上 **GMMA（WGMMA）** 中 `ss` 与 `rs` 两种操作数来源的区别，并解释 `traits.h` 为什么为 QK 与 PV 各定义两套 `TiledMMA`。
- 读懂 `SharedMemoryPlan`（shared memory 摆放）和 `NamedBarriers`（两个 warpgroup 之间点名同步）的设计。

## 2. 前置知识

### 2.1 WGMMA / GMMA：Hopper 的矩阵乘指令

在 Ampere 及更早的 GPU 上，做矩阵乘用的是 `mma`（warp-level matrix multiply-accumulate），指令很小（如 `m16n8k16`），需要程序员手工用 `ldmatrix` 把数据搬到寄存器再发射。Hopper（SM90，对应 `sm_90a`）引入了 **WGMMA**（WarpGroup MMA，CUTLASS 里叫 **GMMA**），它有几个关键变化：

- 操作单位从 **warp（32 线程）** 升级到 **warpgroup（4 个 warp = 128 线程）**。
- 指令是**异步**的：发射后 CUDA Core 可以干别的活，等结果要用时再 `wait`。
- 操作数 A、B 可以直接来自 **shared memory**（通过 matrix descriptor 描述），不必先 `ldmatrix` 搬到寄存器。

根据操作数从哪里来，GMMA 分成几类，本篇只需要区分两种：

| 名称 | A 操作数来源 | B 操作数来源 | 说明 |
|------|------------|------------|------|
| `ss`（shared-shared） | shared memory | shared memory | 两个操作数都在 smem |
| `rs`（register-shared） | 寄存器 register | shared memory | A 在寄存器，B 在 smem |

> 一个直觉：`rs` 的 A 是「我自己寄存器里现成的东西」（比如刚算出来的 P），`ss` 的 A 是「shared memory 里大家共享的东西」。本篇 PV 矩阵乘的两种 `TiledMMA`，正是为了区分「自己的 P」和「别人写的 P」。

每个 `TiledMMA` 描述的是一次完整的逻辑矩阵乘：M×K 的 A 乘以 K×N 的 B 得到 M×N 的 C，靠 `GMMA::ss_op_selector` / `GMMA::rs_op_selector` 这类「选择器」根据目标形状和数据类型挑出底层真正要用哪几条 WGMMA 指令。

### 2.2 TMA：Tensor Memory Accelerator

TMA 是 Hopper 上**异步搬运一块多维张量**的硬件单元，由一个线程发起 `copy`，数据从 global memory 直达 shared memory，搬运完成时通过 **mbarrier**（memory barrier，这里用 `ClusterTransactionBarrier`）通知等待的线程。本篇 `SharedMemoryPlan` 里那一排 `barriers_K0[9]` 就是 TMA 的完成信号。

### 2.3 为什么要把输出 O 拆开：seesaw 的动机

soft attention 的输出矩阵 O 需要**常驻在寄存器里**反复累加（FlashAttention 的 online softmax 思路）。但一篇 kernel 要处理的输出是 \(64\times512\)，全 float32：

\[
64 \times 512 = 32768 \quad \text{个 32-bit 寄存器}
\]

博客原文指出，一个 SM 只有约 65536 个 32-bit 寄存器，**只能放下一份完整的 O**。FlashAttention-3 的「双 O、ping-pong 交替」玩不转了（放不下两份 O）。于是作者把 O 沿列**竖切**成 \(O_L\)（\(64\times256\)）和 \(O_R\)（\(64\times256\)），分别交给 warpgroup 0 和 warpgroup 1，再设计一套数学上等价、但能让两个 warpgroup 交错的「seesaw（跷跷板）」调度。

> 这就是为什么本篇你会看到 `HEAD_DIM_V/2` 反复出现：PV 矩阵乘只算输出的一半，每个 warpgroup 只「拥有」一半 O。seesaw 的 11 步细节留到 u3-l3，本篇只把它作为理解 `TiledMMA_PV_LocalP/RemoteP` 的背景。

### 2.4 两个同步原语对照

| 原语 | 类型 | 本篇出现处 | 用途 |
|------|------|-----------|------|
| `TMABarrier`（`ClusterTransactionBarrier`） | mbarrier | `barriers_K0/K1/barrier_Q` | 等 TMA 异步搬运完成 |
| `NamedBarrier::arrive_and_wait` | 命名 barrier | `NamedBarriers` 枚举 | 两个 warpgroup 之间互相点名等 |

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [config.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/config.h) | 整个 kernel 的静态 tile 配置常量，放进 `Config` 命名空间 |
| [traits.h](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/traits.h) | `Traits` 模板：把 config 常量、各路 GMMA `TiledMMA`、smem 布局、`SharedMemoryPlan`、`NamedBarriers` 全部打包 |
| [splitkv_mla.cuh](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh) | 真正消费 `Traits` 的 kernel 实现，本篇只引用其中「怎么用 LocalP / RemoteP」的关键段落作为佐证 |

一句话：`config.h` 给「砖头尺寸」，`traits.h` 把砖头砌成「图纸」，`splitkv_mla.cuh` 按图纸盖房子。

## 4. 核心概念与源码讲解

### 4.1 Config 常量：kernel 的静态尺寸

#### 4.1.1 概念说明

`config.h` 是全文件最短、却最关键的头文件。它把 kernel 的 tile 大小和 MLA 的 head 维度写成一组 `static constexpr` 常量，集中在 `Config` 命名空间里。这些值是**编译期常量**——因为 GMMA 指令选择、shared memory swizzle 布局、TMA 描述符形状全部依赖它们，必须编译期定死，不能运行时变。

为什么要做成常量？因为 Hopper 的一次 WGMMA 只支持特定形状（如 M=64 固定，N∈{8…256}，K 取 16/32…），CUTLASS 的 `op_selector` 要在编译期挑出最优指令组合；smem 的 `SW128` swizzle 也是按具体形状生成的。把这些维度写成模板参数 / `constexpr`，编译器才能为这一种尺寸「量身定制」出最高效的指令序列。

#### 4.1.2 核心流程

四个常量的含义：

| 常量 | 值 | 几何含义 | 作用 |
|------|----|---------|------|
| `BLOCK_SIZE_M` | 64 | Q 一块的「行数」（M 维，query 维） | 一次 GEMM 处理 64 个 query 向量（注：MLA 下 query 维其实是「query 头」） |
| `PAGE_BLOCK_SIZE` | 64 | KV cache 一个 page 的 token 数（K/N 分块的块大小） | Paged KV cache 一页 = 64 token；QK 的 N 维、PV 的 K 维都吃这个 |
| `HEAD_DIM_K` | 576 | K 的 head 维（含 64 维 RoPE） | MLA decode 的 \(d_k\)，见 u1-l1 |
| `HEAD_DIM_V` | 512 | V 的 head 维（输出 O 的列数） | MLA decode 的 \(d_v\)，\(d_k > d_v\) 是 MLA 非对称约束 |

注意一个贯穿全篇的不等式：

\[
\text{HEAD\_DIM\_K} = 576 = 9 \times 64, \qquad \text{HEAD\_DIM\_V} = 512 = 8 \times 64
\]

这两个「能被 64 整除」是后面所有分块的基石：`HEAD_DIM_K/64 = 9` 正是 TMA 把一个 K 块拆成 9 份细粒度拷贝的由来，也是 `barriers_K0[9]` 数组长度的由来。

#### 4.1.3 源码精读

整个 `config.h` 只有一个 `Config` 命名空间，4 行有效定义：

[config.h:5-9](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/config.h#L5-L9) 把 `BLOCK_SIZE_M / PAGE_BLOCK_SIZE / HEAD_DIM_K / HEAD_DIM_V` 钉死为 `static constexpr int`。这四行决定了 kernel 的所有 tile 形状，后续 `Traits` 会逐个引用它们。

kernel 启动前还会用 `FLASH_ASSERT` 做一次防御性核对：[splitkv_mla.cuh:1276-1278](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L1276-L1278) 里 `run_flash_splitkv_mla_kernel` 断言 `params.d == Config::HEAD_DIM_K` 且 `params.d_v == Config::HEAD_DIM_V`——即运行时传进来的 head 维度必须和编译期常量一致，否则直接退出。这就是为什么 u2-l3 要用 `DISPATCH_HEAD_DIM` 把运行时的 head_dim 转成编译期常量，再实例化对应 `Traits` 的 kernel。

#### 4.1.4 代码实践

**目标**：亲手验证 `HEAD_DIM_K/64` 与 TMA 分块数、barrier 数量的一致性。

**操作步骤**：

1. 打开 `traits.h`，找到 `SharedMemoryPlan`（4.3 节会精读）。
2. 数一数 `barriers_K0` 这个数组有多大（答案：`HEAD_DIM_K/64`）。
3. 打开 `splitkv_mla.cuh`，看 `launch_kv_tiles_copy_tma<0, 9>(...)` 这种调用（如 [splitkv_mla.cuh:1082](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L1082)），模板参数 `0, 9` 表示「从第 0 个 64×64 子块拷到第 9 个」。

**需要观察的现象**：

- `barriers_K0` 数组长度 = `HEAD_DIM_K/64` = \(576/64 = 9\)。
- TMA 拷贝函数的 `START..END` 范围上限正好是 9，对应博客里「一个 \(64\times576\) 的 K 块拆成 9 次 TMA copy」。

**预期结果**：9 这个数字在 config（576÷64）、barrier 数组、TMA 调用模板参数三处完全自洽。这不是巧合，而是「能被 64 整除」的 head 维度让整条流水天然对齐。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `HEAD_DIM_K` 改成 640（即 10×64），需要同步改动哪些地方？

> **答案**：至少要改 `config.h` 的常量、`barriers_K0/K1` 数组长度（自动变成 `640/64=10`，因为数组长度本身写的是 `HEAD_DIM_K/64`）、`launch_kv_tiles_copy_tma` 的 `END` 上限、`warpgroup_cooperative_qkt_gemm` 里把 sQ/sKV 切成 9 块（`(_, _, _0{}, _)` 得到第 3 维大小）的硬编码假设，以及 `rQ8` 相关的「第 8 块单独放寄存器」逻辑。这正是 GMMA kernel 改一个维度要动一片的原因。

**练习 2**：为什么 `BLOCK_SIZE_M` 恰好取 64？

> **答案**：因为 WGMMA 的 M 维固定是 64（一条 `wgmma.mma_async` 处理 64 行）。把 Q 块也定成 64，一次 GEMM 就能把整块 Q 喂给一条 WGMMA，无需在 M 维上拼接，指令利用率最高。

---

### 4.2 GMMA TiledMMA 选择：QK 用 ss/rQ，PV 用 LocalP/RemoteP

#### 4.2.1 概念说明

`Traits` 模板里一口气定义了**四个** `TiledMMA`：

| 名字 | op_selector | 用在哪 | A 来源 | A=B 是谁 |
|------|-------------|--------|--------|---------|
| `TiledMMA_QK_sQ` | `ss` | \(P = Q K^\top\)，Q 在 smem 时 | smem | A=Q, B=K |
| `TiledMMA_QK_rQ` | `rs` | \(P = Q K^\top\)，Q 在寄存器时 | 寄存器 | A=Q, B=K |
| `TiledMMA_PV_LocalP` | `rs` | \(O \mathrel{+}= P V\)，P 在自己寄存器 | 寄存器 | A=P, B=V |
| `TiledMMA_PV_RemoteP` | `ss` | \(O \mathrel{+}= P V\)，P 在 smem（别人写的） | smem | A=P, B=V |

核心直觉：

- **QK 的两种**：一个 K 块 \(64\times576\) 太大，Q 也大，受寄存器限制，Q 的「最后一块」（第 8 个 \(64\times64\) 子块）被单独存到寄存器 `rQ8` 里腾出 smem 给 P。所以 Q 的前 8 块走 `sQ`（smem，`ss`），第 9 块走 `rQ8`（寄存器，`rs`）。
- **PV 的两种**：这是本篇重头戏，直接对应 seesaw 调度。`LocalP` = 用「我自己刚算出来的 P」（在寄存器，`rs`）；`RemoteP` = 用「另一个 warpgroup 算出来、写到 smem 里的 P」（在 smem，`ss`）。

`op_selector` 的参数顺序是 `<InputT_A, InputT_B, OutT, Shape<M,N,K>, MajorA, MajorB>`。其中 `GMMA::Major::K` / `GMMA::Major::MN` 告诉描述符：该操作数的 **K 维** 还是 **M/N 维** 是连续排布的，这决定 TMA/GMMA 怎么读 smem。对初学者，只需记住：Major 是「这个矩阵在 smem 里按哪个维度连续」的开关，由 smem layout 反推出来。

#### 4.2.2 核心流程

把四套 `TiledMMA` 放回 attention 两次矩阵乘里看：

```
QK 阶段： P[m,n] = Q[m,k] · K[n,k]^T        形状 (64, 64, 576)
          ├── 前 8 个 64×64 子块：A=Q 在 smem → TiledMMA_QK_sQ (ss)
          └── 第 9 个 64×64 子块：A=Q 在寄存器 → TiledMMA_QK_rQ (rs)

PV 阶段： O_half[m,n] = P[m,k] · V[n,k]^T    形状 (64, 256, 64)
          ├── P 是「我自己算的」→ 寄存器 → TiledMMA_PV_LocalP  (rs)
          └── P 是「对方算的」  → smem    → TiledMMA_PV_RemoteP (ss)
```

注意 PV 的 N 维是 `HEAD_DIM_V/2 = 256` 而不是 512：因为 O 被竖切成两半，每个 warpgroup 只算自己那 256 列。这与 2.3 节 seesaw 动机直接对应。

`LocalP` / `RemoteP` 怎么落到 seesaw 上，看下面这张对应表（seesaw 步骤序号取自博客，`[0]`/`[1]` 是 warpgroup 编号）：

| seesaw 步骤 | 运算 | 用到的 P | P 在哪 | 选哪套 TiledMMA |
|------------|------|---------|--------|----------------|
| `[5]` wg0: \(o_L \mathrel{+}= p_0 V_{0L}\) | wg0 用**自己的** \(p_0\) | wg0 寄存器 | **LocalP (rs)** |
| `[8]` wg1: \(o_R \mathrel{+}= p_1 V_{1R}\) | wg1 用**自己的** \(p_1\) | wg1 寄存器 | **LocalP (rs)** |
| `[10]` wg1: \(o_R \mathrel{+}= p_0 V_{0R}\) | wg1 借用 wg0 的 \(p_0\) | smem（wg0 写入） | **RemoteP (ss)** |
| `[11]` wg0: \(o_L \mathrel{+}= p_1 V_{1L}\) | wg0 借用 wg1 的 \(p_1\) | smem（wg1 写入） | **RemoteP (ss)** |

也就是说：**一个 warpgroup 算自己的 P 时，P 在寄存器，走 LocalP；要用对方 warpgroup 的 P 时，对方先用 `stmatrix` 把 P 写进 smem，自己再走 RemoteP 读出来。** 这正是本篇练习任务要解释的关系。

#### 4.2.3 源码精读

**QK 的两套（Q 在 smem / 在寄存器）**：

[traits.h:26-34](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/traits.h#L26-L34) 用 `GMMA::ss_op_selector` 和 `GMMA::rs_op_selector` 各定义一个，形状都是 `(BLOCK_SIZE_M=64, PAGE_BLOCK_SIZE=64, HEAD_DIM_K=576)`，两个操作数都 `Major::K`。唯一区别是 A（Q）从 smem 来还是寄存器来。

在 kernel 里它们是这样切换的：[splitkv_mla.cuh:207-211](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L207-L211) 同时构造 `tiled_mma_sQ`（ss）和 `tiled_mma_rQ`（rs），然后宏 `QKT_GEMM_ONE_TILE` 在 [splitkv_mla.cuh:213-218](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L213-L218) 里分支：第 0~7 块用 `qkt_gemm_one_tile_sQ`，第 8 块用 `qkt_gemm_one_tile_rQ`（A 换成寄存器里的 `rQ8`）。

**PV 的两套（LocalP / RemoteP）**：

[traits.h:36-44](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/traits.h#L36-L44) 是本篇核心。两者形状都是 `(BLOCK_SIZE_M=64, HEAD_DIM_V/2=256, PAGE_BLOCK_SIZE=64)`，Major 是 `K, MN`（A=P 按 K 维连续，B=V 按 MN 维连续）。差别只在 A 是 `rs`（寄存器，LocalP）还是 `ss`（smem，RemoteP）。

它们在 kernel 里被两个函数分别消费：

- [splitkv_mla.cuh:284-298](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L284-L298) `warpgroup_cooperative_pv_gemm_localP`：A 是寄存器里的 `rP`（重排成 `rP_retiled`），用 `TiledMMA_PV_LocalP`，对应「自己的 P」。
- [splitkv_mla.cuh:302-319](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L302-L319) `warpgroup_cooperative_pv_gemm_remoteP`：A 是 smem 里的 `sP`，用 `TiledMMA_PV_RemoteP`，对应「对方的 P」。

把寄存器 P 写进 smem（让另一个 warpgroup 能 RemoteP 读）的动作在 [splitkv_mla.cuh:484-497](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L484-L497) `save_rPb_to_sP`，用的是 `SM90_U32x4_STSM_N`（stmatrix 指令）；反向读回来的 [splitkv_mla.cuh:506-519](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L506-L519) `retrieve_rP_from_sP` 用 `SM75_U32x4_LDSM_N`（ldmatrix）。`stmatrix`/`ldmatrix` 这对指令正是「寄存器 ⇄ smem 的矩阵搬运工」，是 LocalP 与 RemoteP 之间的桥梁。

#### 4.2.4 代码实践（本讲指定任务）

**目标**：在 `traits.h` 里找到 PV 的 `LocalP`（rs）和 `RemoteP`（ss）两个 `TiledMMA`，说清它们如何对应 seesaw 里两个 warpgroup 对 P 的「本地/远程」访问。

**操作步骤**：

1. 打开 [traits.h:36-44](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/traits.h#L36-L44)，对照两段定义。注意：
   - `TiledMMA_PV_LocalP` 用 `rs_op_selector` → A=P 在**寄存器**。
   - `TiledMMA_PV_RemoteP` 用 `ss_op_selector` → A=P 在 **shared memory**。
   - 两者 N 维都是 `HEAD_DIM_V/2 = 256`，因为每个 warpgroup 只拥有 O 的一半列。
2. 打开博客 [docs/20250422-new-kernel-deep-dive.md](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/docs/20250422-new-kernel-deep-dive.md) 的 11 步 seesaw（第 27–40 行），找到步骤 `[5]/[8]`（各自用自己的 P）和步骤 `[10]/[11]`（借用对方的 P）。
3. 在 `splitkv_mla.cuh` 里找两个 `wg0_subroutine` / `wg1_subroutine`，确认：
   - 自己用自己 P 的 GEMM（如 wg0 的 `rO0 += rPb @ sV0L`，[splitkv_mla.cuh:786](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L786)）调 `warpgroup_cooperative_pv_gemm_localP` → **LocalP**。
   - 用对方 P 的 GEMM（如 wg0 的 `rO0 += sP1 @ sV1L`，[splitkv_mla.cuh:810](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L810)）调 `warpgroup_cooperative_pv_gemm_remoteP` → **RemoteP**。

**需要观察的现象**：

- `LocalP` 一律搭配「寄存器里的 rP/rPb」；`RemoteP` 一律搭配「smem 里的 sP0/sP1」。
- 在调用 `remoteP` 之前，必定先有一处 `save_rPb_to_sP`（stmatrix）把对方的 P 落到 smem，并用 `NamedBarrier` 通知（`sP0Ready`）。

**预期结果**：你能画出下面这条「P 的所有权流转」链——

```
wg0 算出 p0(寄存器) ──LocalP(rs)──► wg0 自己更新 o_L ──[stmatrix]──► sP0(smem)
                                                                    │
wg1 想用 p0 时 ──RemoteP(ss)── 读 sP0 ◄────────────────────────────┘
```

对称地，wg1→wg0 走 sP1。本篇不展开 seesaw 的 rescale 数学（留 u3-l3），只确认「LocalP = 自己的寄存器 P / rs，RemoteP = 别人的 smem P / ss」这层硬件映射。

#### 4.2.5 小练习与答案

**练习 1**：为什么 QK 阶段也要准备 `sQ`（ss）和 `rQ`（rs）两套，而不是只用其中一套？

> **答案**：因为 Q 块 \(64\times576\) 在 smem 里占了很大空间，而主循环还要给 `sP`（写到 smem 的 P）腾位置。把 Q 的第 8 个 \(64\times64\) 子块挪到寄存器 `rQ8`，正好让 `sP1` 可以和 Q 的第 8 块 smem 区域重叠复用（见 4.3 节 `sP1 = ...(_8{})`）。所以「Q 在 smem」用于前 8 块，「Q 在寄存器」用于第 9 块，两套 `TiledMMA` 各司其职。

**练习 2**：PV 的 `TiledMMA` 里 Major 为什么是 `K, MN` 而不是像 QK 那样 `K, K`？

> **答案**：因为 MLA 里 V 和 K 共享同一块 smem（只是「换个名字」），V 是 K 的转置视图（见 4.3 节 `SmemLayoutV` 是 `SmemLayoutK` 的转置）。转置后 V 的 M/N 维（256）变成连续维，所以 B 操作数按 MN-major 读取，Major 自然是 `MN`。这也是 `SmemLayoutV` 用 `composition(..., GenRowMajor{})` 构造的原因。

---

### 4.3 SharedMemoryPlan 与 NamedBarriers：smem 布局与同步

#### 4.3.1 概念说明

光有 `TiledMMA` 还不够，还得告诉 GPU「shared memory 里摆哪些张量、怎么 swizzle、用哪些 barrier 同步」。这部分由 `Traits` 里的 `SmemLayout*`、`SharedMemoryPlan` 和 `NamedBarriers` 三件套完成。

- **SmemLayout**：每个 smem 张量用 `GMMA::Layout_K_SW128_Atom` 这个「swizzle 原子」铺到目标形状。`SW128` 是一种 swizzle 模式，通过重排列地址避免 GMMA 访问 smem 时的 bank conflict。
- **SharedMemoryPlan**：一个 POD 结构，把所有 smem 数组（Q、双缓冲的 K0/K1、P、各种标量缓冲）和 TMA barrier 集中声明，一次性映射到 kernel 的 dynamic shared memory（`extern __shared__ char wksp_buf[]`）。
- **NamedBarriers**：一组枚举值，给两个 warpgroup 之间的「点名同步」起名字。Hopper 的 `NamedBarrier::arrive_and_wait(num_threads, barrier_id)` 让指定数量的线程在某个编号的 barrier 上汇合。

#### 4.3.2 核心流程

smem 里要同时放下（参考 [traits.h:71-83](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/traits.h#L71-L83)）：

| smem 张量 | 形状 | 字节（bf16=2B, float=4B） | 用途 |
|-----------|------|--------------------------|------|
| `smem_sQ` | \(64\times576\) | 36864×2 ≈ 72 KiB | Q |
| `smem_sK0` | \(64\times576\) | ≈ 72 KiB | K 块 0（双缓冲之一） |
| `smem_sK1` | \(64\times576\) | ≈ 72 KiB | K 块 1（双缓冲之二） |
| `smem_sP0` | \(64\times64\) | 4096×2 = 8 KiB | wg0 写入的 P（给 wg1 RemoteP 读） |
| `smem_sM` | 64 | 64×4 = 256 B | 全局 running max \(m\) |
| `sL_reduction_wksp` | 128 | 128×4 = 512 B | 跨 warpgroup 归约 rL |
| `smem_sScale0/1` | 各 64 | 各 256 B | rescale 因子 |
| `barriers_K0[9]` | — | — | K0 的 9 个 TMA 完成信号 |
| `barriers_K1[9]` | — | — | K1 的 9 个 TMA 完成信号 |
| `barrier_Q` | 1 | — | Q 的 TMA 完成信号 |

注意几个**复用**（这是节省 smem 的关键，初读很容易看漏）：

- `sP1`（wg1 写给 wg0 的 P）**复用 sQ 的第 8 个 64×64 子块**（[splitkv_mla.cuh:986](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L986)）。这正是 4.2 练习 1 提到「把 Q 第 8 块挪进寄存器 rQ8」换来的空间。
- `sO`（写回前的输出暂存）**复用 sK0/sK1**（[splitkv_mla.cuh:991](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L991)），因为 store 阶段 K 已经用完了。

`NamedBarriers` 共 5 个名字（[traits.h:101-107](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/traits.h#L101-L107)），是两个 warpgroup 之间的「接力棒」：

| 名字 | 谁发出（arrive） | 谁等（wait） | 含义 |
|------|----------------|-------------|------|
| `sScale0Ready` | wg0 | wg1 | wg0 的 rescale 因子 `sScale0` 写好了 |
| `sScale1Ready` | wg1 | wg0 | wg1 的 `sScale1` 写好了 |
| `sP0Ready` | wg0 | wg1 | wg0 的 P 已 stmatrix 到 smem（RemoteP 可读） |
| `rO1sP0sV0RIssued` | wg1 | wg0 | wg1 已发射 `rO1 += sP0 @ sV0R`（可以复用 sV0） |
| `sMInitialized` | （初始化阶段） | wg0 | running max `sM` 已初始化好 |

> 区分两类 barrier：`barriers_K0/K1/barrier_Q`（`TMABarrier`）是**硬件异步搬运的完成信号**，由 TMA 引擎 arrive、线程 wait；`NamedBarrier` 是**线程之间的点名汇合**，由某个 warpgroup 的线程主动 arrive_and_wait。两者解决的是完全不同的同步问题。

#### 4.3.3 源码精读

**SmemLayout 系列**：[traits.h:46-69](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/traits.h#L46-L69) 定义 Q/K/P 的 smem 布局，都用 `GMMA::Layout_K_SW128_Atom`（K 维、SW128 swizzle）。值得单独看的是 `SmemLayoutV`：

[traits.h:56-59](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/traits.h#L56-L59) 用 `composition(SmemLayoutK, make_layout(Shape<HEAD_DIM_V, PAGE_BLOCK_SIZE>, GenRowMajor{}))` 构造 V——**V 不是新分配内存，而是 K 的同一片 smem 换个转置视图**（注释明确写 `A transposed version of SmemLayoutK`）。这呼应了 MLA 里「K 和 V 同源，只是不同名字」（博客原话），也解释了 4.2 练习 2 里 V 的 Major 是 `MN` 的原因。

`rP0Layout`（[traits.h:66-69](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/traits.h#L66-L69)）是 P 在寄存器里的排布，由 `partition_fragment_C(TiledMMA_QK_sQ, ...)` 推出，即「QK 累加器（=P）在寄存器里的形状」——`((2,2,8),1,1)`，每线程 32 个 float，128 线程凑成 \(64\times64\) 的 P。

**SharedMemoryPlan**：[traits.h:71-83](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/traits.h#L71-L83) 把上面表格里所有 smem 数组 + 19 个 TMA barrier 装进一个结构。`cute::array_aligned` 保证每个数组按访问对齐。这个结构的 `sizeof` 就是 kernel 启动时要申请的 dynamic shared memory 大小（见 [splitkv_mla.cuh:1335-1336](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L1335-L1336)，用 `cudaFuncSetAttribute` 把上限抬到这个值）。

**NamedBarriers**：[traits.h:101-107](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/traits.h#L101-L107) 定义 5 个枚举。kernel 里以 `NamedBarrier::arrive_and_wait(T::NUM_THREADS, NamedBarriers::sP0Ready)` 这种形式使用（如 [splitkv_mla.cuh:800](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L800)、[splitkv_mla.cuh:913](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L913)）。注意 arrive 的线程数：跨两个 warpgroup 同步时传 `NUM_THREADS=256`，只在一个 warpgroup 内同步时传 `128`（如初始化阶段 [splitkv_mla.cuh:1125](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L1125) 的 `NamedBarrier::arrive_and_wait(128, ...)`）。

#### 4.3.4 代码实践

**目标**：估算这个 kernel 的 shared memory 占用，理解为什么需要 `cudaFuncSetAttribute` 抬高上限。

**操作步骤**：

1. 按 4.3.2 的表格，把每个 `cute::array_aligned` 的字节数加起来（bf16 张量按 2 字节/元素，float 张量按 4 字节/元素，`TMABarrier` 每个按 8 字节估）。
2. 算出 `sizeof(SharedMemoryPlan)` 的近似值。
3. 打开 [splitkv_mla.cuh:1335-1336](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L1335-L1336)，确认 kernel 用 `cudaFuncSetAttribute(..., cudaFuncAttributeMaxDynamicSharedMemorySize, smem_size)` 把单 block 可用 smem 提到这个值。

**需要观察的现象**：

- 仅 `smem_sQ + smem_sK0 + smem_sK1` 三块 bf16 数据就约 \(3 \times 64 \times 576 \times 2 \approx 216\) KiB，远超 Hopper 默认的 48 KiB dynamic shared memory 上限。
- 因此必须显式 `cudaFuncSetAttribute` 抬高到接近 SM 的 228 KiB 上限。

**预期结果**：你理解了「为什么 kernel 启动前一定要调 `cudaFuncSetAttribute`」——否则 launch 会因为 smem 超限失败。这也解释了为什么 seesaw 要绞尽脑汁用 sP1/sO 的 smem 复用：smem 极其紧张，每省一块都值。精确字节数「待本地验证」（取决于 `TMABarrier`、对齐 padding 的实际大小）。

#### 4.3.5 小练习与答案

**练习 1**：`barriers_K0` 为什么是 9 个而不是 1 个？

> **答案**：一个 K 块 \(64\times576\) 被拆成 9 个 \(64\times64\) 子块逐个 TMA 拷贝，每个子块一个 barrier。这样第 0 个子块一到，GEMM 就能先算它，不必等整个 \(64\times576\) 全到——这是博客说的「fine-grained TMA copy - GEMM pipelining」掩盖访存延迟的关键。

**练习 2**：`NamedBarrier` 和 `TMABarrier` 能互换吗？

> **答案**：不能。`TMABarrier`（mbarrier）由 TMA 硬件在数据搬完时自动 arrive，专门表达「异步搬运完成」；`NamedBarrier` 由线程主动 arrive_and_wait，表达「某些线程互相等」。两者信号源不同，互换会破坏正确性。

---

## 5. 综合实践

把三个模块串起来，做一次「读图填表」的源码阅读型综合练习（无需 GPU）：

1. 打开 `traits.h`，从上到下把 `Traits` 结构里的所有成员填进下表的「类别」列：
   - 类别分为：**Config 常量**、**GMMA TiledMMA**、**SmemLayout**、**SharedMemoryPlan 成员**、**NamedBarrier**。
2. 对每个 `TiledMMA`，标注它用的是 `ss` 还是 `rs`、A/B 分别是谁、N 维多大（注意 PV 的 N=`HEAD_DIM_V/2`）。
3. 对照博客的 seesaw 11 步，给每个 PV 的 `TiledMMA` 标注它对应 seesaw 里的哪几步（`[5]/[8]` → LocalP，`[10]/[11]` → RemoteP）。
4. 在 `SharedMemoryPlan` 里找出两处「smem 复用」：`sP1` 复用谁的 smem？`sO` 复用谁的 smem？（提示：回到 [splitkv_mla.cuh:986](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L986) 和 [splitkv_mla.cuh:991](https://github.com/deepseek-ai/FlashMLA/blob/9241ae3ef9bac614dd25e45e507e089f888280e0/csrc/sm90/decode/dense/splitkv_mla.cuh#L991)）

**交付物**：一张填满的「成员 → 类别 → ss/rs → seesaw 步骤」对照表。完成后你应当能脱口而出：「LocalP 是 rs、用自己的寄存器 P；RemoteP 是 ss、用对方 stmatrix 写进 smem 的 P；这两者靠 5 个 NamedBarrier 接力，靠 19 个 TMA barrier 等数据。」

## 6. 本讲小结

- `config.h` 用 4 个 `constexpr`（`BLOCK_SIZE_M=64`、`PAGE_BLOCK_SIZE=64`、`HEAD_DIM_K=576`、`HEAD_DIM_V=512`）钉死 kernel 的 tile 尺寸；`576÷64=9` 这个整除关系是 TMA 分块、barrier 数量自洽的根。
- `traits.h` 为 QK 定义两套 `TiledMMA`（`sQ` 用 ss、`rQ` 用 rs，对应 Q 在 smem / 在寄存器），为 PV 定义两套（`LocalP` 用 rs、`RemoteP` 用 ss）。
- **核心映射**：PV 的 `LocalP`（rs）= 用自己寄存器里的 P；`RemoteP`（ss）= 用对方 warpgroup 经 `stmatrix` 写进 smem 的 P；二者靠 `ldmatrix`/`stmatrix` 与 NamedBarrier 串联。
- `SmemLayout*` 全用 `SW128` swizzle 避免 bank conflict；`SmemLayoutV` 是 `SmemLayoutK` 的转置视图（K/V 同源）。
- `SharedMemoryPlan` 集中声明所有 smem 数组 + 19 个 TMA barrier，并通过 `sP1` 复用 sQ、`sO` 复用 sK 来缓解 smem 紧张；启动时需 `cudaFuncSetAttribute` 抬高 smem 上限。
- `NamedBarriers` 的 5 个枚举是两个 warpgroup 之间的点名接力棒，与「等 TMA 完成」的 `TMABarrier` 各司其职、不可互换。

## 7. 下一步学习建议

本篇把 seesaw 调度的「静态骨架」（config + traits）摆好了，但**没讲 11 步的 rescale 数学**，也没讲两个 warpgroup 如何在时间上交错重叠 CUDA Core 与 Tensor Core。下一篇 **u3-l3「Seesaw 调度与 TMA 流水」** 会带着本篇的四个 `TiledMMA` 和五个 `NamedBarrier`，深入 `splitkv_mla.cuh` 的 `wg0_subroutine` / `wg1_subroutine` 主循环，把 seesaw 的 11 步、online softmax 的 rescale、细粒度 TMA 流水与 `EVICT_FIRST` cache hint 一一对上号。建议在进入 u3-l3 前，先把本篇综合实践的对照表做完——那张表就是下一篇的导航图。

如果对 GMMA `op_selector` 的 Major/swizzle 细节仍觉模糊，可回头读 CUTLASS 里 `cute/arch/cluster_sm90.hpp` 与 `GMMA` 命名空间的文档，不过对理解 FlashMLA 而言，掌握「ss=都在smem、rs=A在寄存器」这一层已足够。
