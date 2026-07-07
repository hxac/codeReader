# CUDA SRAM/寄存器复杂度与 swizzle/persist 策略

## 1. 本讲目标

本讲是「手写 CUDA 后端」系列的第二篇，承接 [u7-l1](u7-l1-cuda-fwd-kernel-architecture.md)。上一篇我们走通了 `csrc/cuffpa/` 前向 kernel 的宏观流水线（g2s → s2r → MMA → online softmax），本讲要回答一个更底层也更硬核的问题：

> 在大 head_dim（D 可达 512/1024）下，FFPA 是怎么把 SRAM（共享内存）压到 **O(1) in D**，同时把压力转移到寄存器，并保持寄存器只有 **O(d/4)** 的？

为了讲清这一点，本讲拆成三块互相关联的机制：

1. **swizzle 布局**：用 XOR 置换列地址来消除 SMEM bank 冲突，作为「padding 加宽行」的替代方案——二选一，由 `ENABLE_FFPA_SMEM_SWIZZLE_Q/K/V` 控制。
2. **launch_templates**：host 端把「tile 大小 / stage 深度 / pad 宽度 / persist 开关」在**编译期**装配成模板参数，并在编译期就算出每个 block 的 SRAM 上限。
3. **persist 策略**：Q/KV 在「g2s（驻留 SMEM）」与「s2r（驻留寄存器）」两条路径上的 SRAM/寄存器/IO 三方权衡，由 `ENABLE_FFPA_PERSIST_Q_G2S` / `PERSIST_KV_G2S` / `PERSIST_Q_S2R` / `PERSIST_V_S2R` 控制。

学完后你应该能：

- 说清 SMEM bank 冲突是怎么产生的，以及 `swizzle::permuted` 用一行 XOR 怎么消除它；
- 看懂 `launch_templates.cuh` 里那串 `getConfig*` 函数把 env 开关翻译成模板常量的全过程，并能手算一个 block 的 SRAM 用量；
- 对每个 `ENABLE_FFPA_PERSIST_*` / `ENABLE_FFPA_SMEM_SWIZZLE_*` 开关，说出它在 kernel 里改变了哪段代码的行为、又付出了什么代价；
- 从源码注释里印证「SRAM 与 D 无关、寄存器随 D 线性（1/4 折扣）」这一 Split-D 的核心复杂度结论。

## 2. 前置知识

本讲默认你已经读过 [u7-l1](u7-l1-cuda-fwd-kernel-architecture.md)，知道手写 CUDA 后端需要 `ENABLE_FFPA_CUDA_IMPL=1` 编译出 `ffpa_attn._C`，且只实现前向。这里再补几个本讲要用到的底层名词，全部用大白话解释。

**SRAM / SMEM（Shared Memory，共享内存）**
GPU 上每个 SM（流式多处理器）内部有一块极快但极小的存储，Ampere/Hopper 通常 164~228 KiB/SM。它是 block 内所有线程共享的「高速缓存」，也是 `cp.async`/TMA 把显存数据搬进来后落地的地方。FFPA 里 Q/K/V 的 tile 都暂存在这里。本讲的「SRAM」和「SMEM」是同一个东西。

**寄存器（Register）**
每个线程私有的最快存储，Hopper 每线程 255 个 32-bit 寄存器上限。MMA 指令的输入（`R_Q/R_K/R_V`）和输出累加器（`R_S/R_O/R_D`）都驻留在寄存器里。寄存器用爆了会 **spill（溢出）** 到本地内存（实际是显存），性能断崖式下降。

**SMEM bank 与 bank 冲突（bank conflict）**
SMEM 被切成 32 个 **bank**，每个 bank 宽 4 字节、独立带宽。一个 warp（32 线程）同时访问时，若多个线程落到**同一个 bank 的不同字**，就会串行化，这叫 bank 冲突。无冲突时一个 cycle 拿完全部 32 线程的数据；2 路冲突要 2 cycle，n 路要 n cycle。FFPA 用 `ldmatrix` 加载 MMA 所需 fragment，访问模式固定，必须让布局**bank-conflict-free**。

**消除 bank 冲突的两种办法**
- **padding（补齐）**：给每行末尾补几列空位，把对齐的访问错开。代价是**白吃 SRAM**（补的列不存有效数据）。
- **swizzle（搅动）**：不改行宽，而是用一条 XOR 公式把「逻辑列」重排到「物理列」，让相邻行落到不同 bank。代价是需要一条额外的地址计算（但 `__forceinline__` 且位运算，几乎免费）。

**MMA fragment 与 m16n8k16**
Ampere 的张量核指令 `mma.sync.m16n8k16` 一次算一个 16×8×16 的小块（M=16 行、N=8 列、K=16 归约）。它的输入输出都按固定规则**散布在 32 个线程的寄存器里**（fragment）。本讲只需要记住一点：每个线程只持有 MMA 结果的一小片，所以「一个 8 列宽的输出原子」摊到每线程只占 2~4 个寄存器——这正是「1/4 折扣」的物理来源。

**g2s / s2r**
搬运数据的两段路：`g2s` = global memory → SMEM（`cp.async` 或 TMA）；`s2r` = SMEM → register（`ldmatrix`）。**persist** 指让某份数据（Q 或 KV）只搬一次、之后所有 KV 循环都复用，不再每轮重搬。

**编译期 vs 运行期开关**
FFPA 的 `ENABLE_FFPA_*` 大多是**运行期**开关（改了不用重编），但它们最终会变成 nvcc 的 `-D` 宏定义，在**编译期**冻结成模板常量（见本讲 4.3）。少数（`ENABLE_FFPA_ALL_STAGES/ALL_HEADDIM`）是纯构建期开关，改了必须重编。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [csrc/cuffpa/swizzle.cuh](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/swizzle.cuh) | 一个模板函数 `swizzle::permuted(i,j)`，用 XOR 给出 swizzled 列偏移；开头有大段 ASCII 图解释 bank 布局。 |
| [csrc/cuffpa/launch_templates.cuh](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh) | host 端启动器。一堆 `getConfig*` 把 env 宏翻译成模板常量，`getConfigQKVSmemMaxSize` 编译期算 SRAM 上限，`launch_ffpa_attn_fwd_template` 按 head_dim 选 kernel 并 launch。 |
| [csrc/cuffpa/warp.cuh](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/warp.cuh) | `reduce_sum`/`reduce_max`：基于 `__shfl_xor_sync` 的 warp 内归约，给 online softmax 的逐行 `m_i/l_i` 更新用。 |
| [csrc/cuffpa/ffpa_attn_fwd.cuh](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_fwd.cuh) | kernel 本体。SMEM 布局指针、`R_Q/R_K/R_V/R_S/R_O/R_D` 寄存器声明、persist 分支都在这里。本讲重点看它的「布局与寄存器」部分。 |
| [csrc/cuffpa/prefill.cuh](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/prefill.cuh) | `cp_async_qkv_g2s` 等 g2s 函数，实际调用 `swizzle::permuted` 决定 SMEM 落点。 |
| [docs/env.md](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/env.md) | 所有 `ENABLE_FFPA_*` 开关的人类可读文档。 |
| [env.py](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py) | 把环境变量解析成布尔，再在 `env_cuda_cflags()` 里转成 `-DENABLE_FFPA_*` 传给 nvcc。 |

---

## 4. 核心概念与源码讲解

### 4.1 SMEM bank 冲突与 swizzle 布局

#### 4.1.1 概念说明

`ldmatrix.x4` 是 MMA 取操作数的标准指令：它让一个 warp 的 32 个线程协作，从 SMEM 里把 4 个 8×8（fp16 下即 16 字节/线程）的 fragment 搬进寄存器。问题在于——`ldmatrix` 每个线程给出的地址很有规律，如果 Q/K/V 在 SMEM 里就按「自然行优先」存（第 i 行紧跟第 i-1 行），那么同一 warp 的线程会**整齐地访问同一组 bank**，瞬间触发 bank 冲突。

FFPA 给出两种解法，互斥二选一，由三个开关 `ENABLE_FFPA_SMEM_SWIZZLE_Q/K/V` 决定：

- 开 swizzle：`kPadQ/K/V = 0`，调用 `swizzle::permuted` 用 XOR 重排列偏移；
- 关 swizzle：`kPadQ/K/V = 8`，给每行补 8 个 fp16（16 字节）空位错开 bank。

`docs/env.md` 把这一点讲得很直白：

> `ENABLE_FFPA_SMEM_SWIZZLE_Q`，默认 `True (1)`，`True`: bank-conflict-free Q SMEM via swizzle. `False`: bank-conflict-free via padding.
> 见 [docs/env.md:L47](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/env.md#L47)。

默认全开 swizzle，因为 swizzle 不浪费 SRAM，而 padding 要白吃 `kPad * 行数 * 字节数` 的共享内存——在大 D 场景 SRAM 本就吃紧，这点浪费很要命。

#### 4.1.2 核心流程

swizzle 的本质是一条**可逆的地址置换**：存储时用 `permuted(i,j)` 算出物理列，读取时用同一个 `permuted(i,j)` 还原——写入和读出对称，所以数据不会乱。

它的数学形式是一个 **XOR**。把每行切成宽 8（fp16）= 16 字节的「chunk」，chunk 在行内的位置记为 `j>>3`，行号记为 `i`。置换规则（对应 CuTe 硬件 `Swizzle<B,M,S>` 模式）是：

\[

\text{chunk}' = (\text{chunk}) \;\text{XOR}\; f(i)

\]

其中 `f(i)` 取 `i` 的若干低位比特。对最常用的 `kColStride=16`（SWIZZLE_32B），`f(i) = (i>>2) \& 1`，于是同一个物理位置在第 0~3 行存的是 chunk 0、第 4~7 行存的是 chunk 1、第 8~11 行又是 chunk 0……相邻的 4 行一组被「搅」到不同 bank。

伪代码（`kColStride=16` 分支）：

```text
permuted(i, j):
    chunk   = (j >> 3) & 1            # 0 或 1，因为只有 2 个 chunk
    bit     = (i >> 2) & 1            # 每 4 行翻转一次
    return ((chunk ^ bit) & 1) << 3   # 物理列偏移：0 或 8（fp16）
```

> 注意：`permuted` 只给「chunk 级」列偏移；chunk 内的 8 列细偏移（`j & 7`）由调用方保留。这样一条 XOR 就能把相邻行的同列访问错开到不同 bank，且写入读出对称。

#### 4.1.3 源码精读

`swizzle.cuh` 开头是一张巨大的 ASCII 图，逐行画出 SWIZZLE_32B 下「逻辑列 0~64 → 物理 bank」的映射，肉眼可见第 0~3 行和第 4~7 行的 0/8 是错开的：

见 [csrc/cuffpa/swizzle.cuh:L1-L50](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/swizzle.cuh#L1-L50)（图中 `row 0~3` 全是 `0`、`row 4~7` 全是 `8`，正是 `(i>>2)&1` 的翻转）。

核心函数是模板 `permuted<kColStride, kStep>`，用 `if constexpr` 为四种行宽各生成一条位运算：

[csrc/cuffpa/swizzle.cuh:L74-L101](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/swizzle.cuh#L74-L101)：定义 `permuted(i,j)`，注释里给出三种行宽对应的 CuTe 硬件 swizzle 模式（`SWIZZLE_32B/64B/128B`）。

其中三个关键分支：

- [csrc/cuffpa/swizzle.cuh:L89-L90](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/swizzle.cuh#L89-L90)：`kColStride==16` → `(((j>>3)^(i>>2))&1)<<3`，即 SWIZZLE_32B，对应行宽 16 fp16 = 32 字节。
- [csrc/cuffpa/swizzle.cuh:L91-L94](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/swizzle.cuh#L91-L94)：`kColStride==32` → 2 比特 XOR，SWIZZLE_64B。
- [csrc/cuffpa/swizzle.cuh:L95-L99](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/swizzle.cuh#L95-L99)：`kColStride==64` → 3 比特 XOR，SWIZZLE_128B。

注意注释点明这套置换**与 CuTe/TMA 的 `Swizzle<B,M,S>` 硬件模式逐位对齐**（[csrc/cuffpa/swizzle.cuh:L62-L73](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/swizzle.cuh#L62-L73)）。意义在于：`cp.async` 写入与 `ldmatrix` 读出用的是**同一个 swizzle 布局**，所以 swizzle 不会破坏数据，只是改变物理落点。

真正调用 `permuted` 的地方在 `prefill.cuh` 的 g2s 函数里，根据 `kPad==0`（swizzle）还是 `>0`（padding）二选一：

[csrc/cuffpa/prefill.cuh:L184-L186](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/prefill.cuh#L184-L186)：`cp async & apply swizzle or padding`——`kPad==0` 时调 `swizzle::permuted<kMmaAtomK>(...)`，否则用 padding 偏移。

#### 4.1.4 代码实践

**实践目标**：在不开 GPU 的情况下，把 `ENABLE_FFPA_SMEM_SWIZZLE_Q/K/V` 三个开关与 kernel 里的代码改动对应起来，并定量比较 swizzle vs padding 的 SRAM 代价。

**操作步骤**：

1. 打开 [docs/env.md:L45-L49](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/env.md#L45-L49)，确认三个 swizzle 开关默认都是 `True (1)`。
2. 打开 [launch_templates.cuh:L172-L197](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L172-L197)，看 `getConfigPadQ/K/V`：开 swizzle → `kPad=0`；关 swizzle → `kPad=8`。
3. 打开 [ffpa_attn_fwd.cuh:L151-L154](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_fwd.cuh#L151-L154)，看 `Q_tile_size = Br * (kMmaAtomK + kPadQ)`：padding 下每行多出 `kPadQ=8` 个 fp16 = 16 字节。

**需要观察的现象**：在 SRAM 公式 `getConfigQKVSmemMaxSize`（[launch_templates.cuh:L267-L272](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L267-L272)）里，`kPadQ/K/V` 直接乘进了 tile 体积。以 `Br=Bc=64`、`kStageQK=2`、`kStagePV=2`、fp16（2 字节）为例：

| 模式 | Q/K 每行字节 | 单 tile 体积 | Q+K+V 合计（约） |
|---|---|---|---|
| swizzle（kPad=0） | 16×2 = 32 B | 64×16×2 = 2048 B | (2+2+2)×2048 = 12 KiB |
| padding（kPad=8） | (16+8)×2 = 48 B | 64×24×2 = 3072 B | 6×3072 = 18 KiB |

也就是说**关掉 swizzle 改用 padding，SRAM 多吃约 50%**——这就是 swizzle 默认开启的硬理由。

**预期结果**：你能画出「env 开关 → `getConfigPad*` → `Q_tile_size` → SRAM 总量」这条链，并解释每个开关的代价是「bank-conflict-free 的两种实现里二选一」。

> 本地验证（可选）：若有 H100/A100 且已用 `ENABLE_FFPA_CUDA_IMPL=1` 编译，可分别以默认与 `ENABLE_FFPA_SMEM_SWIZZLE_Q=0 ENABLE_FFPA_SMEM_SWIZZLE_K=0 ENABLE_FFPA_SMEM_SWIZZLE_V=0`（注意需重编，因 `-D` 宏在编译期冻结）跑 `bench/bench_ffpa_fwd.py`，观察 SRAM 占用（`FFPA_PTXAS_VERBOSE=1`）与吞吐的变化。若无 GPU 则为「源码阅读型实践」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 swizzle 的写入和读出可以用同一个 `permuted`，而不会把数据读乱？

**答案**：因为 XOR 是**对合**运算（`a ^ b ^ b = a`）。写入时把逻辑列 `j` 落到物理列 `permuted(i,j)`；读出时对同一个物理列再套一次相同的置换逻辑（`ldmatrix` 的地址生成与 `cp.async` 对称），就还原出原始 `j`。swizzle 只改物理 bank 归属，不改数据内容。

**练习 2**：`kColStride=16` 时 `permuted` 只在 0 和 8 之间跳，为什么这就够消除冲突？

**答案**：`ldmatrix` 一个 warp 访问的是同一行的多个 8-fp16 chunk。冲突发生在「不同线程访问同 bank」。把相邻 4 行的 chunk 0/1 互换（`(i>>2)&1`），就保证相邻行落到不同 bank 组，2 路冲突被消除；又因为只有 2 个 chunk，1 比特 XOR 已足够把所有 chunk 错开。

---

### 4.2 launch_templates：编译期装配与 SRAM 上限计算

#### 4.2.1 概念说明

上一篇我们看到 kernel 模板有二十多个参数（`Br/Bc/stages/pad/persist/...`）。这些参数**不能在运行期改**——它们必须是编译期常量，才能让 nvcc 把循环展开、把 `if constexpr` 死分支剪掉、把 SRAM 大小静态算出来。`launch_templates.cuh` 的职责就是：

1. 读 env 宏（`#ifdef ENABLE_FFPA_*`）；
2. 按 `kHeadDim` 选 tile/stage；
3. 把这些组装成一堆 `constexpr`；
4. **编译期**算出每个 block 的最大 SRAM 用量；
5. 用这些常量实例化正确的 kernel 模板并 launch。

关键设计：**所有 `getConfig*` 都是 `constexpr` 函数**，结果在编译期就定死。这意味着开一个 env 开关 = 选一个不同的编译产物（见 4.3）。

#### 4.2.2 核心流程

`launch_ffpa_attn_fwd_template` 的装配流程（伪代码）：

```text
1. 读 env 宏 → kShareSmemQKV, kPrefetchQK/PV, kPersistQs2r/Qg2s, kRegPipeKV
2. 读 swizzle 宏 → kPadQ/K/V (0=swizzle, 8=padding)
3. 算 tile：Br = kMmaAtomM * kMmaTileSeqLenQ * kValTileSeqLenQ
            Bc = kMmaAtomN * kMmaTileSeqLenK * kValTileSeqLenK   (强制 Br==Bc)
4. 算 SRAM 上限 kQKVSmemMaxSize（编译期，见 4.2.3）
5. 按 head_dim 选 kernel：
     D <= 128 → small-d persistent kernel（需 ENABLE_FFPA_PERSIST_KV_G2S）
     D >  128 → large-d split-D kernel（默认）
6. cudaFuncSetAttribute(MaxDynamicSharedMemorySize, smem) + launch
```

注意第 5 步的「按 head_dim 分流」正是 [u7-l1](u7-l1-cuda-fwd-kernel-architecture.md) 讲的三套模板（split-D / persistent / decode 两阶段），本讲的 swizzle/persist 旋钮会同时影响 small-d 与 large-d 两条路径。

#### 4.2.3 源码精读

先看几个决定复杂度的常量：

[csrc/cuffpa/launch_templates.cuh:L7-L12](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L7-L12)：`kMaxDForSmallDKernel=128`、`kMaxDForOStoreFloat32=512`、`kMaxDForSmallBlockTile=256`。其中 `kMaxDForOStoreFloat32` 的注释直接点明寄存器取舍：

> Always use fp32 accumulators for O to reduce numerical instability for D up to 512; Use fp16/bf16 for D > 512 to save registers, since the larger D may cause register spilling when using fp32 accumulators.
> 见 [csrc/cuffpa/launch_templates.cuh:L8-L11](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L8-L11)。

这是「O(d/4) 寄存器」里那个 1/4 的开关——D>512 时把 O 累加器从 fp32（每 fragment 4 寄存器）降成 fp16（2 寄存器），寄存器占用直接砍半，见 `getConfigOStorageAccFloat32`（[launch_templates.cuh:L105-L113](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L105-L113)）。

再看 pad 与 swizzle 的翻译（4.1 已引用，这里看装配点）：

[csrc/cuffpa/launch_templates.cuh:L355-L357](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L355-L357)：`kPadQ/K/V = getConfigPadQ/K/V()` 把 env 开关变成 0 或 8。

SRAM 上限的计算是本模块最硬核的一段，`getConfigQKVSmemMaxSize` 用 `kStageQK/kStagePV`（流水线深度，每级一个缓冲）、`kPersistQg2s`（Q 是否独占一块常驻 SMEM）、`kShareSmemQKV`（V 是否复用 QK 的 SMEM）组装：

[csrc/cuffpa/launch_templates.cuh:L217-L284](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L217-L284)：编译期算 `kQKVSmemMaxSize`。关键分支：

- Q 是否独占整 D 的 SMEM：[L246-L249](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L246-L249)（`kPersistQg2s ? (kHeadDim/kMmaAtomK) : kStageQK` —— persist Q g2s 时 Q 占满整个 head_dim 切片）。
- V 是否复用 QK 的 SMEM：[L255-L259](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L255-L259)（`kShareSmemQKV && !kPersistQg2s` 时取 `max(QK, V)`，否则 Q+K+V 相加）。

而这段里有一行**直接印证寄存器复杂度**的注释：

[csrc/cuffpa/launch_templates.cuh:L260-L261](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L260-L261)：

> `R_D registers usage, s=2, d=64, 16 regs; d=128, 32 regs; d=256, 64 regs; d=512, 128 regs; d=1024, 256 regs;`

把数字摆出来：`regs = d / 4`（fp16 O 存储，`s=2`）。这正是 Split-D 的「O(d/4) 寄存器」结论的源码出处，详见 4.4。

最后是按 head_dim 选 kernel 的分流点：

[csrc/cuffpa/launch_templates.cuh:L517-L547](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L517-L547)：在 `ENABLE_FFPA_PERSIST_KV_G2S` 下，`kHeadDim <= 128` 走 small-d persistent kernel，否则走 large-d split-D kernel。

以及把 `kPersistQg2s` 与 large-D 的 s2r 互斥掉的「有效值」：

[csrc/cuffpa/launch_templates.cuh:L513-L515](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L513-L515)：

```cpp
constexpr int kEffShareSmemQKV_LargeD = (kPersistQg2s) ? 0 : kShareSmemQKV;
constexpr int kEffPersistQs2r_LargeD =
    (kPersistQg2s || kHeadDim > 256) ? 0 : kPersistQs2r;
```

意思是：large-D 路径下，一旦 Q 已 persist 到 SMEM（g2s），就**不能再 persist 到寄存器**（s2r），否则两份常驻会撑爆；且 D>256 时 s2r 一律关。这是 4.3 persist 三方权衡在代码里的硬约束。

#### 4.2.4 代码实践

**实践目标**：手算一个具体配置的 block SRAM 上限，验证「SRAM 与 D 无关」。

**操作步骤**：

1. 取 large-D 默认配置：`Br=Bc=64`、`kStageQK=kStagePV=2`、`kPadQ/K/V=0`（swizzle 开）、`kPersistQg2s=0`（D>320 时为 0）、`kShareSmemQKV=0`。
2. 代入 [getConfigQKVSmemMaxSize 的 large-D 分支](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L267-L272)：
   - `Q_smem_size = (kStageQK=2) * (64 * 16) * 2 字节 = 4096 B`
   - `K_smem_size = 2 * (64 * 16) * 2 = 4096 B`
   - `V_smem_size = (kStagePV=2) * (64 * 16) * 2 = 4096 B`
   - 合计 ≈ 12 KiB。
3. **关键观察**：这个 12 KiB 里**没有任何项依赖 `kHeadDim`**——tile 宽度恒为 `kMmaAtomK=16`，D 只作为内层循环步数（见 4.4），不进 SRAM。把 D 从 512 换成 1024，SRAM 仍是 12 KiB。

**需要观察的现象**：SRAM 公式里 `kHeadDim` 只在 `kPersistQg2s` 分支出现（Q 独占整 D 切片），其余分支 D 不出现。

**预期结果**：你能说清「默认 large-D 配置下，SRAM ≈ 常数（约 12 KiB 量级），与 D 无关」——这就是 O(1) SRAM 的来源。

#### 4.2.5 小练习与答案

**练习 1**：`getConfigQKVSmemMaxSize` 里为什么有一处 `* 2`（如 `K_smem_size = kStageQK * ... * 2`）？

**答案**：那个 `* 2` 是 fp16 的**字节数**（每个 fp16 占 2 字节）。整个 SRAM 公式算的是字节数，要传给 `cudaFuncSetAttribute(MaxDynamicSharedMemorySize)`。

**练习 2**：为什么 `Br` 必须等于 `Bc`？代码在哪强制？

**答案**：见 [launch_templates.cuh:L339-L340](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L339-L340) 的 `static_assert(Br == Bc, "Br must be equal Bc to avoid illegal memory access.")`。因为 Q/K/V 的 SMEM tile 共用同一套寻址与 swizzle 逻辑，行宽不一致会导致越界。

---

### 4.3 persist 策略：Q/KV 的 g2s 与 s2r 权衡

#### 4.3.1 概念说明

在线 softmax 的 KV 主循环里，每个 KV tile 都要用到 Q。Q 的搬运有三种粒度：

| 策略 | 含义 | 驻留位置 | 每轮 KV 是否重搬 |
|---|---|---|---|
| 都不 persist | Q 每 K 轮 g2s 一次 + s2r 一次 | 都不在 | 是（IO 最大） |
| `PERSIST_Q_G2S` | Q 整 D 切片 g2s **一次**，常驻 SMEM | SMEM | 只重 s2r |
| `PERSIST_Q_S2R` | Q g2s+s2r **一次**，常驻寄存器 | 寄存器 | 完全不重搬 |

三条路的代价是**SRAM、寄存器、IO 的三方权衡**：

- **PERSIST_Q_G2S**：省 g2s IO，但 Q 要在 SMEM 里占满整个 `kHeadDim/kMmaAtomK` 个切片（SRAM ∝ D）——所以只对 `D≤320` 开（[getConfigPersistQg2s](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L130-L141)），注释明说「more SRAM, but still keep register usage」。
- **PERSIST_Q_S2R**：连 s2r IO 都省，但要给 Q fragment 在寄存器里开 `kHeadDim/kMmaAtomK` 份（寄存器 ∝ D）——所以只对 `D<512` 开，且 large-D 路径在 D>256 时强制关（4.2.3 的 `kEffPersistQs2r_LargeD`），注释明说「more registers, but still keep O(1) SRAM」。

KV 也有对称的一组：`PERSIST_KV_G2S`（KV 常驻 SMEM，只对 `D≤256` 开，且**自动切换算法**——小 D 走 FA-2 attention 级 tiling，大 D 走 Split-D）和 `PERSIST_V_S2R`（仅 small-d kernel 用，更多寄存器）。这些对应关系在 `docs/env.md` 写得很清楚：

见 [docs/env.md:L51-L56](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/env.md#L51-L56)（Persistent g2s / s2r loads 一节）。

#### 4.3.2 核心流程

env 开关 → nvcc 宏 → 模板常量 → kernel 分支的链路：

```text
ENABLE_FFPA_PERSIST_Q_G2S=1   （环境变量，运行期读）
        │  env.py: enable_persist_q_g2s() → True
        │  env.py: env_cuda_cflags() 追加 "-DENABLE_FFPA_PERSIST_Q_G2S"
        ▼
#ifdef ENABLE_FFPA_PERSIST_Q_G2S   （编译期宏）
        │  getConfigPersistQg2s<kStageQK,kHeadDim>()
        ▼
kPersistQg2s = 0 或 1   （模板常量）
        │  传入 ffpa_attn_split_d_fwd_template<...>
        ▼
kernel 内 if constexpr (kPersistQg2s) { ... }   （死分支剪除）
```

要害：**env 变量虽是「运行期读」，但落到 `-D` 宏后就成了编译期常量**。所以「改 env 开关」对大部分 `ENABLE_FFPA_*` 而言**仍然需要重编**——`docs/env.md` 的 Key note #2 把这一点列为头号注意事项（见下文源码精读）。

#### 4.3.3 源码精读

先看 env.py 怎么把布尔变量变成 `-D` 宏：

[env.py:L289-L302](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L289-L302)：`env_cuda_cflags()` 里一连串 `if cls.enable_*(): extra_env_cflags.append("-DENABLE_FFPA_*")`。这就是「运行期开关 → 编译期宏」的转换点。

注意一个**依赖断言**：KV persist 必须连带 Q persist：

[env.py:L310-L312](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/env.py#L310-L312)：`assert cls.enable_persist_q_g2s()`（在 `enable_persist_kv_g2s()` 为真时）。逻辑上 KV 常驻 SMEM 的小-D 路径需要 Q 也常驻，否则每轮仍要重搬 Q，得不偿失。

再看 launch_templates 里各 persist 常量的「按 head_dim 自适应」：

[csrc/cuffpa/launch_templates.cuh:L130-L141](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L130-L141)：`getConfigPersistQg2s` —— `D<256 → 1`；`D≤320 且 stages<3 → 1`；否则 `0`。注释「Persist load Q g2s for headdim < 512, more SRAM, but still keep register usage」。

[csrc/cuffpa/launch_templates.cuh:L143-L152](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L143-L152)：`getConfigPersistQs2r` —— 纯开关，注释「more registers, but still keep O(1) SRAM」。

[csrc/cuffpa/launch_templates.cuh:L154-L161](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L154-L161)：`getConfigPersistVs2r` —— 仅 small-d kernel。

然后看 kernel 里这些常量怎么改变行为。在 `ffpa_attn_fwd.cuh` 里，`kPersistQg2s` 改变的是 **SMEM 布局**与**加载时机**：

[csrc/cuffpa/ffpa_attn_fwd.cuh:L155-L161](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_fwd.cuh#L155-L161)：`K_tile_smem` 的起点 = `Q_tile_smem + (kPersistQg2s ? (kHeadDim/kMmaAtomK)*Q_tile_size : kStageQK*Q_tile_size)`。即 persist Q g2s 时，Q 在 SMEM 里占满整个 head_dim 切片，K 紧跟其后。

[csrc/cuffpa/ffpa_attn_fwd.cuh:L169-L180](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_fwd.cuh#L169-L180)：`if constexpr (kPersistQg2s)` 在**进入 KV 主循环之前**就把 Q 的所有 D 切片 g2s 进来（「load Q g2s at very beginning」），之后循环里只 s2r。注释还点明把它放在寄存器初始化之前，是为了让 g2s 与初始化重叠。

而 `kPersistQs2r` 改变的是**寄存器声明**与**循环里的 s2r 频率**：

[csrc/cuffpa/ffpa_attn_fwd.cuh:L190-L192](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_fwd.cuh#L190-L192)：

```cpp
// Registers for S=Q@K^T/O=P@V, e.g, 64, !kPersistQs2r -> [1][4] 4 regs,
// kPersistQs2r -> [1][4*4] 16 regs.
uint32_t R_Q[kValTileSeqLenQ][(kPersistQs2r) ? (kHeadDim / kMmaAtomK) : 1][4];
```

这就是「persist Q s2r 用更多寄存器换更少 IO」的字面证据：开 s2r 后 `R_Q` 的第二维从 `1` 变成 `kHeadDim/kMmaAtomK`（D=64 时从 4 个寄存器涨到 16 个）。循环里也只 s2r 一次：

[csrc/cuffpa/ffpa_attn_fwd.cuh:L265-L269](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_fwd.cuh#L265-L269)：`if constexpr (kPersistQs2r)` 分支注释「We only load Q g2s and s2r once if kPersistQs2r is enabled」。

最后，`docs/env.md` 把所有 persist 开关的语义、默认值、适用 head_dim 范围列在两节里：

[docs/env.md:L51-L56](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/env.md#L51-L56)：Persistent g2s / s2r loads；以及关键的「构建期 vs 运行期」说明：

[docs/env.md:L66](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/env.md#L66)：Key note #2 —— 除 `ALL_STAGES/ALL_HEADDIM` 外的 `ENABLE_FFPA_*` 虽然运行期读，但改了要重编（因为落到 `-D` 宏）。

#### 4.3.4 代码实践

**实践目标**（即本讲指定的实践任务）：对照 `docs/env.md` 的 `ENABLE_FFPA_SMEM_SWIZZLE_Q/K/V` 与 `ENABLE_FFPA_PERSIST_Q_G2S` / `PERSIST_Q_S2R`，逐个解释开关在 kernel 里对应的行为与代价。

**操作步骤**：

1. 打开 [docs/env.md:L45-L56](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/env.md#L45-L56)，抄下五个开关的默认值与一句话描述。
2. 对每个开关，按下表填出「翻译点（env.py/launch_templates 里哪一行）」「kernel 行为变化（ffpa_attn_fwd.cuh 里哪个分支）」「代价」三列。参考答案见下方表格。

**参考答案表**：

| env 开关（默认） | 翻译点 | kernel 行为 | 代价 |
|---|---|---|---|
| `SMEM_SWIZZLE_Q=1` | [launch_templates.cuh:L172-L179](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L172-L179) → `kPadQ=0` | g2s/ldmatrix 用 `swizzle::permuted` 重排列，[prefill.cuh:L184-L186](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/prefill.cuh#L184-L186) | 每次访问多一条 XOR 位运算（几乎免费） |
| `SMEM_SWIZZLE_Q=0` | 同上 → `kPadQ=8` | 每行补 8 个 fp16 空位错开 bank | SRAM 每行多 16 字节（≈+50%，见 4.1.4） |
| `SMEM_SWIZZLE_K/V` | [launch_templates.cuh:L181-L197](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L181-L197) | 同 Q，作用于 K/V tile | 同 Q（padding 同样多吃 SRAM） |
| `PERSIST_Q_G2S=1` | [launch_templates.cuh:L130-L141](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L130-L141) → `kPersistQg2s` | Q 整 D 切片 g2s 一次常驻 SMEM，[ffpa_attn_fwd.cuh:L169-L180](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_fwd.cuh#L169-L180)；K_tile_smem 后移 | SRAM ∝ D（仅 D≤320 开），省 g2s IO |
| `PERSIST_Q_S2R=0` | [launch_templates.cuh:L143-L152](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L143-L152) → `kPersistQs2r` | 开启后 `R_Q` 第二维涨到 `kHeadDim/kMmaAtomK`，[ffpa_attn_fwd.cuh:L190-L192](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_fwd.cuh#L190-L192)；循环内只 s2r 一次 | 寄存器 ∝ D（D=64 时 4→16 regs），省 s2r IO，D>256 时被强制关 |

**需要观察的现象**：五个开关里，swizzle 系列是「SRAM vs 一条 XOR」的选择；persist 系列是「SRAM/寄存器 vs IO」的选择，且都**按 head_dim 自适应**（小 D 才开 g2s/s2r）。

**预期结果**：你能不查文档，仅凭源码说出每个开关改了哪段 kernel、付出什么代价。

#### 4.3.5 小练习与答案

**练习 1**：`PERSIST_Q_G2S` 和 `PERSIST_Q_S2R` 能同时在大 D 上开吗？为什么？

**答案**：不能。`PERSIST_Q_G2S` 让 Q 占满整 D 的 SRAM 切片（SRAM ∝ D），`PERSIST_Q_S2R` 让 Q 占满整 D 的寄存器 fragment（寄存器 ∝ D），两者同时开会双重吃紧。代码里 large-D 路径用 `kEffPersistQs2r_LargeD = (kPersistQg2s || kHeadDim > 256) ? 0 : kPersistQs2r`（[launch_templates.cuh:L514-L515](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L514-L515)）强制二者互斥且仅小 D 可开。

**练习 2**：为什么 `PERSIST_V_S2R` 默认开（`True`），而 `PERSIST_Q_S2R` 默认关（`False`）？

**答案**：V persist 只用于 small-d kernel（D≤128，见 [getConfigPersistVs2r 注释](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L154-L161)），D 很小，寄存器代价可控；而 Q persist 通用于更大 D，默认关闭以避免寄存器压力，让用户/自动调优按需打开。

---

### 4.4 Split-D 下 O(1) SRAM 与 O(d/4) 寄存器的来源

#### 4.4.1 概念说明

现在把前三节串起来，正面回答本讲的核心问题。Split-D（详见 [u4-l2](u4-l2-split-d-fine-grained-tiling.md) 的 Triton 版）在手写 CUDA 里的体现是：**head_dim 在两次矩阵乘里都不作为 SMEM 的一个完整维度加载，而是被切成宽 16（`kMmaAtomK`）的片段，作为内层循环迭代**。这带来两条硬核结论：

1. **SRAM 复杂度 O(1) in D**：每个 SMEM tile 永远只有 `[Br/Bc, 16]` 宽，D 只增加循环次数，不增加单 tile 体积。
2. **寄存器复杂度 O(d/4)**：输出累加器 `R_D` 必须在寄存器里持有整行 head_dim 的部分结果（因为 PV 的 D 是输出维，不能像 QK 的 D 那样分段累加），但摊到每线程只有 d/4 个寄存器（fp16 存储）或 d/2 个（fp32 存储）。

#### 4.4.2 核心流程

SRAM 为何 O(1)：SMEM tile 宽度恒为 16（`kMmaAtomK`），D 是外层循环。

```text
QK 阶段：for tile_K_d in 0 .. D/16:        # D 在这里，是循环次数
             load Q[Br,16], K[Bc,16] 到 SMEM  # SMEM tile 永远 16 宽
             mma 累加进同一块 R_S            # 归约维，可分段相加
PV 阶段：for each V-group (宽 8):           # D 在这里也是循环
             load V[Bc,8] 到 SMEM
             mma 累加进对应的 R_D 片段        # 输出维，各片段各存各的
```

关键：QK 的 D 是**归约维**（可分段相加进同一累加器 `R_S`，SRAM/寄存器都不随 D 涨）；PV 的 D 是**输出维**（每个 V-group 要独立的 `R_D` 片段，所以寄存器随 D 涨）。

寄存器为何 O(d/4)：`R_D` 形状是 `[kValTileSeqLenP=1][kValTileHeadDimV=D/8][2 或 4]`。每线程持有的寄存器数：

\[

\text{regs}(R_D) = \underbrace{1}_{kValTileSeqLenP} \times \underbrace{D/8}_{\text{V-group 数}} \times \underbrace{(2 \text{ 或 } 4)}_{\text{fp16/fp32 存储}}

\]

fp16 存储时 = `D/8 × 2 = D/4`；fp32 存储时 = `D/8 × 4 = D/2`。其中 `D/8` 来自 MMA m16n8k16 把 8 列宽的输出原子分发给线程、`(2 或 4)` 是每个原子的 fragment 寄存器数——这就是「1/4 折扣来自 MMA fragment 打排」。

#### 4.4.3 源码精读

SRAM O(1) 的直接证据是 kernel 顶部的文档注释：

[csrc/cuffpa/ffpa_attn_fwd.cuh:L9-L11](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_fwd.cuh#L9-L11)：

> The "large-d" variant tiles the head-dim axis into `kHeadDim / kMmaAtomK` inner steps so SMEM stays O(1) in D.

而 tile 宽度恒为 16 的证据：

[csrc/cuffpa/ffpa_attn_fwd.cuh:L151-L154](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_fwd.cuh#L151-L154)：`Q_tile_size = Br * (kMmaAtomK + kPadQ)`，行宽由 `kMmaAtomK=16` 决定，与 D 无关。

寄存器 O(d/4) 的直接证据，一是 `R_D` 声明：

[csrc/cuffpa/ffpa_attn_fwd.cuh:L204-L207](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_fwd.cuh#L204-L207)：

```cpp
uint32_t R_D[kValTileSeqLenP][kValTileHeadDimV]
            [(kOStorageAccFloat32) ? 4 : 2];
```

其中 `kValTileHeadDimV = kHeadDim / (kMmaAtomN * kMmaTileHeadDimV) = D/8`（见 [getConfigWarpTileHeadDimV](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L88-L94)）。二是 `launch_templates.cuh` 那行手算注释，把 `regs = D/4`（fp16 存储）逐 D 列出：

[csrc/cuffpa/launch_templates.cuh:L260-L261](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L260-L261)：`d=64→16, d=128→32, d=256→64, d=512→128, d=1024→256`，正好 `regs = D/4`。

而 `kOStorageAccFloat32` 由 `getConfigOStorageAccFloat32` 按 D 切换（[launch_templates.cuh:L105-L113](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L105-L113)）：`D≤512` 用 fp32（d/2 寄存器，更稳）；`D>512` 用 fp16（d/4 寄存器，省寄存器防 spill）。这解释了为什么 D=1024 时注释写 256 regs（d/4）而非 512 regs（d/2）——大 D 主动降精度保命。

最后，online softmax 的逐行 `m_i/l_i` 在寄存器里更新，跨 lane 的归约用 `warp.cuh` 的 shfl 原语：

[csrc/cuffpa/warp.cuh:L19-L35](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/warp.cuh#L19-L35)：`reduce_sum`/`reduce_max` 用 `__shfl_xor_sync` 做 warp 内树形归约。kernel 里 `lane_block_row_max_old/sum_old`（[ffpa_attn_fwd.cuh:L183-L188](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_fwd.cuh#L183-L188)）就是这些寄存器里的标量，重缩放时用 warp 归约对齐——它们数量固定（`[1][2]`），不随 D 增长，是 O(1) 的寄存器项。

#### 4.4.4 代码实践

**实践目标**：用源码里的常量，手动验证 `R_D` 寄存器数 = D/4（fp16 存储）。

**操作步骤**：

1. 从 [getConfigWarpTileHeadDimV](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L88-L94) 读出 `kValTileHeadDimV = kHeadDim / (kMmaAtomN=8 * kMmaTileHeadDimV=1) = D/8`。
2. 从 [R_D 声明](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/ffpa_attn_fwd.cuh#L204-L205) 读出形状 `[1][D/8][(kOStorageAccFloat32)?4:2]`。
3. 取 D=1024（此时 `kOStorageAccFloat32=0`，fp16 存储）：`1 × 128 × 2 = 256` 寄存器。
4. 对照 [注释](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L260-L261) 写的 `d=1024, 256 regs`。

**需要观察的现象**：手算的 256 与注释的 256 完全一致，且 `256 = 1024/4`。

**预期结果**：你能写出公式 `regs(R_D) = (D/8) × (fp16?2:fp32?4)`，并解释 D>512 时为何选 fp16（把 d/2 压到 d/4，避免 spill）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 QK 阶段的 D 不增加寄存器，而 PV 阶段的 D 增加？

**答案**：QK 中 D 是**归约维**，分段算出的部分 score 累加进同一块 `R_S`（`mma` 的累加器），寄存器数固定；PV 中 D 是**输出维**，每个 8 列宽的 V-group 产出独立的输出片段，必须有独立的 `R_D[...][D/8][...]`，所以寄存器随 D 线性增长。

**练习 2**：如果 D=1024 时仍用 fp32 存储 `R_D`，每线程要多少寄存器？会怎样？

**答案**：`D/8 × 4 = 512` 寄存器/线程，超过 Hopper 单线程 255 寄存器上限，必然大量 spill 到本地内存，性能骤降。所以代码在 `D>512` 时自动切到 fp16 存储（d/4=256 寄存器），用一点精度换不 spill。

---

## 5. 综合实践

把本讲三块机制串起来，完成一份「**给定 head_dim，反推默认 SRAM/寄存器占用与各 persist 开关实际取值**」的小报告。

**任务**：分别对 `D=128` 和 `D=512`，回答以下问题（全部基于源码，不靠记忆）：

1. 走 small-d 还是 large-d kernel？依据 [launch_templates.cuh:L517-L519](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L517-L519)。
2. `kPersistQg2s` 实际取值？依据 [getConfigPersistQg2s](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L130-L141)。
3. `kPersistQs2r` 在 large-d 有效值（`kEffPersistQs2r_LargeD`）？依据 [L514-L515](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L514-L515)。
4. `kPadQ/K/V`（默认 swizzle 开）？依据 [L172-L197](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L172-L197)。
5. `R_D` 寄存器数（fp32 还是 fp16 存储）？依据 [getConfigOStorageAccFloat32](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/csrc/cuffpa/launch_templates.cuh#L105-L113) 与 4.4 公式。

**参考答案要点**：

| D | kernel | kPersistQg2s | kEffPersistQs2r_LargeD | kPadQ/K/V | R_D 存储 | R_D 寄存器 |
|---|---|---|---|---|---|---|
| 128 | small-d（需 KV_G2S） | 1（D<256） | —（small-d 另算） | 0（swizzle） | fp32（D≤512） | D/2 = 64 |
| 512 | large-d split-D | 0（D>320） | 0（D>256 强制关） | 0（swizzle） | fp32（D≤512） | D/2 = 256 |

**进阶**：把 `ENABLE_FFPA_PERSIST_Q_S2R=1` 打开后重编，预测 D=128 与 D=512 下 `R_Q` 寄存器的变化（D=128：4→`128/16=8` 份×4=32 regs；D=512 在 large-d 被 `kEffPersistQs2r_LargeD` 强制关，无变化）。说明这正体现了「persist 是按 D 自适应的，大 D 下强行 persist 寄存器会 spill」。

## 6. 本讲小结

- **swizzle vs padding**：两者都为消除 SMEM bank 冲突，swizzle 用一条 XOR（`swizzle::permuted`）重排列、零 SRAM 浪费；padding 补 8 列、多吃约 50% SRAM。默认全开 swizzle，由 `ENABLE_FFPA_SMEM_SWIZZLE_Q/K/V` 控制。
- **launch_templates 是编译期装配器**：env 宏经 env.py 的 `-D` 落成 `constexpr`，`getConfig*` 按 head_dim 选 tile/stage/pad/persist，并在编译期算出 `kQKVSmemMaxSize`。改这些开关大多需重编。
- **persist 是 SRAM/寄存器/IO 三方权衡**：`PERSIST_Q_G2S`（Q 常驻 SMEM，D≤320）、`PERSIST_Q_S2R`（Q 常驻寄存器，D<512）、`PERSIST_KV_G2S`/`PERSIST_V_S2R`（小 D 专用），全部按 D 自适应，且 Q 的 g2s 与 s2r 在 large-d 互斥。
- **SRAM 复杂度 O(1) in D**：SMEM tile 宽恒为 `kMmaAtomK=16`，D 只是内层循环次数——QK 的 D 是归约维可分段累加，不涨 SRAM。
- **寄存器复杂度 O(d/4)**：PV 的 D 是输出维，每个 V-group 需独立 `R_D` 片段，每线程 `regs = D/8 × (2 或 4)`，fp16 存储即 D/4；D>512 自动降 fp16 存储防 spill。
- **数值稳定性与寄存器取舍**：D≤512 用 fp32 O 累加器（d/2 寄存器，更稳），D>512 用 fp16（d/4，省寄存器）——这是 `kMaxDForOStoreFloat32=512` 的设计意图。

## 7. 下一步学习建议

- 下一篇 **u7-l3 每个 head_dim 代码生成与 C++ pybind 分发**：本讲的 `kHeadDim` 模板参数会被 env.py 按 head_dim 拆成独立翻译单元（`generated/ffpa_attn_fwd_*.cu`），届时你会看到「为什么每个 D 要单独编一份」——正是因为 `getConfig*` 把 D 冻结成了编译期常量。
- 若想横向对照，回看 **u4-l2 Split-D（Triton 版）**：Triton 里的 `NUM_V_GROUPS`/`o_accs` 对应本讲的 V-group 与 `R_D`，两边是同一思想在不同抽象层的实现。
- 调优实战：参照 [docs/env.md:L70](https://github.com/xlite-dev/ffpa-attn/blob/882989c2a3aa465867de9ea2dbd19dbed08660d9/docs/env.md#L70) 的建议，开 `FFPA_PTXAS_VERBOSE=1` 编译，观察各 head_dim 翻译单元的实际寄存器/SMEM 占用，与本讲手算对照。
- 进阶可读 **u7-l4 env.py 构建配置** 与 **u7-l5 运行时 kernel 选择开关**，把「构建期 head_dim 集合 / 运行期 MMA 精度 / swizzle / persist / 流水线」五类开关的全景补齐。
