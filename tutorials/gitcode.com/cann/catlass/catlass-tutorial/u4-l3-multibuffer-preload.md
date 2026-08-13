# 多缓冲 Pingpong 与 Preload 流水

## 1. 本讲目标

本讲承接 u4-l2（DispatchPolicy 调度策略），下到 Block 层主循环的内部，回答两个工程问题：

- 既然昇腾 AICore 内部有多条独立的搬运/计算流水（PIPE），为什么有时它们仍然会「串起来排队跑」？怎么用**多缓冲（Multi Buffer / Pingpong）**让它们真正并行？
- 当数据搬运（GM→L1）成为主流水瓶颈时，怎么用**预加载（Preload）**把搬运指令提前一轮发出去、消除流水空泡？更进一步，**异步预加载（PreloadAsync）**又是如何用一个统一的延迟队列把跨块的搬运空泡也一并消除的？

学完本讲，你应当能够：

1. 说清 SingleBuffer 串行、Multi Buffer 并行的差异，并指出代码里 `STAGES` 与多缓冲数组如何实现乒乓。
2. 读懂 `block_mmad_preload.hpp` 中「跨块预加载」的逻辑，解释它如何消除相邻两个 C 基本块之间的 MTE2 空泡。
3. 读懂 `block_mmad_preload_async.hpp` 中的 `PRELOAD_STAGES` 延迟队列与 `Callback` 机制，理解「无需手动传下一块信息」的代价。

---

## 2. 前置知识

本讲默认你已掌握 u1-l2（存储层级 GM→L1→L0A/L0B→L0C→UB、各 PIPE 流水）、u4-l1（Block 层 k_tile 主循环与四类操作）、u4-l2（DispatchPolicy 标签特化与四种 GEMM 策略）。这里再强化两个关键概念。

### 2.1 PIPE 与 HardEvent 同步

一条 AICore 里有若干条**互不阻塞、可并行执行**的硬件流水（PIPE），本讲涉及四条：

| PIPE | 职责 | 典型 AscendC 接口 |
|------|------|-------------------|
| MTE2 | GM → L1 搬运 | `DataCopy`（`copyGmToL1A/B`） |
| MTE1 | L1 → L0A/L0B 搬运 | `LoadData`（`copyL1ToL0A/B`） |
| M    | L0A·L0B → L0C 计算 | `Mmad`（`tileMmad`） |
| FIX  | L0C → GM 搬出 | `Fixpipe`（`copyL0CToGm`） |

四条 PIPE 物理上并行，但它们共享同一块 L1/L0 缓冲。当「搬运」和「计算」要用同一块缓冲时，就必须**同步**：生产者写完通知消费者，消费者读完通知生产者「这块空了可以重用」。这个通知机制就是 **HardEvent**。

代码里成对出现的两个原语：

- `SetFlag<HardEvent::X_Y>(id)`：在 X 侧发信号（X「我做完了」）。
- `WaitFlag<HardEvent::X_Y>(id)`：在 Y 侧等信号（Y「我等 X 做完」）。

`X_Y` 表示「X 向 Y 发事件」。同一块缓冲上，两个方向的事件成对使用：`MTE2_MTE1`（搬运完通知计算可读）与 `MTE1_MTE2`（计算完通知搬运这块可重写）。这是本讲所有流水重叠的地基。

### 2.2 三种 GEMM 策略的递进

回顾 u4-l2：`MmadAtlasA2Pingpong`（基础乒乓）→ `MmadAtlasA2Preload`（加块间预加载与 ShuffleK）→ `MmadAtlasA2PreloadAsync`（异步 N-buffer + 块间预加载）→ `MmadAtlasA2PreloadAsyncWithCallback`（再加前后 Callback）。本讲按「多缓冲 → 预加载 → 异步与回调」的顺序拆解这条递进链。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [block_mmad_pingpong.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_pingpong.hpp) | 乒乓（Multi Buffer）主循环实现，是理解「块内预加载」与多缓冲数组的基准。 |
| [block_mmad_preload.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_preload.hpp) | `MmadAtlasA2Preload` 实现：在乒乓基础上加「跨块预加载」与 ShuffleK。 |
| [block_mmad_preload_async.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_preload_async.hpp) | `MmadAtlasA2PreloadAsync` 实现：用 `PRELOAD_STAGES` 延迟队列实现跨块预加载，并支持 `Callback`。 |
| [dispatch_policy.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/dispatch_policy.hpp) | 三种策略标签及其编译期参数（`STAGES`/`PRELOAD_STAGES`/`ENABLE_SHUFFLE_K` 等）。 |
| [callback.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/detail/callback.hpp) | `Callback` 轻量可调用对象，承载 mmad 完成后回调。 |
| [basic_matmul_preload.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul_preload.hpp) | 配套 Kernel：演示如何「手动算下一块偏移」喂给 Preload 的 blockMmad。 |
| [04_matmul_summary.md](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/2_Design/01_kernel_design/04_matmul_summary.md) | 设计文档，含「流水优化（Multi Buffer）」「流水优化（Preload）」两节的流水图与定性分析。 |

---

## 4. 核心概念与源码讲解

### 4.1 Multi Buffer 流水

#### 4.1.1 概念说明

「Multi Buffer（多缓冲）」要解决的问题，文档 [04_matmul_summary.md:283-291](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/2_Design/01_kernel_design/04_matmul_summary.md#L283-L291) 用一张图说明白了：

> 如果在 AIC 的 L1/L0A/L0B/L0C 上，每次载入数据 tile 块时都尽量填满所有空间，会导致各 PIPE 的流水**串行**，整体效率低下。

直观地讲：如果 L1 上只有一块缓冲，那么 MTE2（GM→L1）正在写这块缓冲时，MTE1（L1→L0）就必须干等；等 MTE2 写完、MTE1 开始读，MTE2 又反过来干等 MTE1 读完。于是「搬运」和「计算」两条本可并行的 PIPE 被迫排队，时间线是：

```
单缓冲：MTE2 [搬A0B0]        [搬A1B1]        ...
MTE1          [读A0B0]        [读A1B1]   ...   ← 干等
M                 [算]            [算]    ...
```

**优化思路**：给同一级缓冲配 **两块（乒乓，pingpong）**。当 MTE2 写第 0 块（ping）时，MTE1 读上一轮的第 1 块（pong）；下一轮反过来。于是搬运和计算可以同时进行：

```
双缓冲：MTE2 [搬A0] [搬A1] [搬A2] ...
MTE1        [读..] [读A0] [读A1] ...   ← 不再干等
M                  [算A0] [算A1] ...
```

文档把这叫 `流水优化（Multi Buffer）`，并指出它是「常规优化手段，所有 blockMmad 组件均使能」([04_matmul_summary.md:305-308](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/2_Design/01_kernel_design/04_matmul_summary.md#L305-L308))。

#### 4.1.2 核心流程

CATLASS 用一个编译期常量 `STAGES` 表示缓冲块数（乒乓时为 2），把 L1/L0A/L0B 的缓冲**做成数组**，用一个循环递增的索引 `l1ListId`/`l0AListId`/`l0BListId` 在数组里轮转。流程：

1. **构造时**：按 `STAGES` 把 L1/L0A/L0B 各切出 `STAGES` 块，给每块分配一个事件 id，并**预置初始 flag**，让第一轮搬运不必等待。
2. **主循环每轮**：
   - 当前轮读 `listId` 对应的缓冲，下一轮提前搬数据进 `listIdNext`（这是「块内预加载」，下一节细讲）。
   - 用 `SetFlag`/`WaitFlag` 在搬运与计算之间做「数据就绪」与「缓冲可重用」的握手。
3. **循环末尾**：`l1ListId = l1ListIdNext`，索引环形递增，进入下一轮。

#### 4.1.3 源码精读

**策略里的 `STAGES`。** `MmadAtlasA2Pingpong` 把缓冲块数写死为 2（乒乓）：

[dispatch_policy.hpp:31-35](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/dispatch_policy.hpp#L31-L35) —— `MmadAtlasA2Pingpong` 内 `static constexpr uint32_t STAGES = 2;`，这是乒乓的来源。

[block_mmad_pingpong.hpp:51-54](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_pingpong.hpp#L51-L54) —— `DispatchStagesGetter` 在偏特化里把 `STAGES` 取出为 2；对更通用的 `MmadPingpong`（行 56-66）则取 L1/L0 各自 stage 的**最小值**作为公共 `STAGES`。

**构造时切缓冲 + 预置初始 flag。**

[block_mmad_pingpong.hpp:150-168](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_pingpong.hpp#L150-L168) —— 循环 `STAGES` 次，每次给 L1A/L1B/L0A/L0B 各分配一块（`GetBufferByByte`），并给四块缓冲各发一个初始 `SetFlag`：
- `SetFlag<MTE1_MTE2>(l1AEventList[i])`：预置「这块 L1A 空闲可写」，于是主循环开头 `WaitFlag<MTE1_MTE2>` 不会被阻塞。
- `SetFlag<M_MTE1>(l0AEventList[i])`：预置「这块 L0A 空闲可写」，同理让首轮 `copyL1ToL0A` 前的 `WaitFlag<M_MTE1>` 放行。

这两组预置 flag 是多缓冲能跑起来的前提——没有它们，第一轮搬运就会因为等待「上一轮消费完」的死锁信号而卡住。

**多缓冲数组本身。**

[block_mmad_pingpong.hpp:366-389](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_pingpong.hpp#L366-L389) —— `l1ATensorList[STAGES]`、`l1BTensorList[STAGES]`、`l0ATensorList[STAGES]`、`l0BTensorList[STAGES]` 与对应的事件数组、当前块号 `l1ListId`/`l0AListId`/`l0BListId`。乒乓就是这些数组的长度为 2。

**握手与轮转。** 在主循环里，搬运（MTE2）和计算（M/MTE1）通过成对 flag 同步，例如：

[block_mmad_pingpong.hpp:276-286](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_pingpong.hpp#L276-L286) —— L1→L0A 前 `WaitFlag<M_MTE1>`（等上一轮 M 算完，L0A 块可重写）与首个 tile 的 `WaitFlag<MTE2_MTE1>`（等 GM→L1 把这块写完）；L1→L0A 完，在最后一个 tile `SetFlag<MTE1_MTE2>`（通知 GM→L1 这块可重写）。

[block_mmad_pingpong.hpp:343-344](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_pingpong.hpp#L343-L344) —— k 循环末尾 `l1ListId = l1ListIdNext`，实现乒乓轮转。

> 小结：Multi Buffer = 缓冲数组 + 轮转索引 + 成对 HardEvent 握手。`STAGES=2` 即乒乓，是所有 blockMmad 的默认底座。

#### 4.1.4 代码实践

**目标**：验证「乒乓 = STAGES=2 的多缓冲数组 + 初始 flag 预置」。

**步骤**：

1. 打开 [block_mmad_pingpong.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_pingpong.hpp)。
2. 找到构造函数（行 144-171），数一数循环体里 `GetBufferByByte` 与 `SetFlag` 各被调用了几次，确认它们都等于 `STAGES`。
3. 找到数据成员（行 366-389），确认四个 `*TensorList` 都是 `[STAGES]` 长度的数组。
4. 尝试把 `MmadAtlasA2Pingpong` 的 `STAGES` 在脑中改成 3（注意：仅作思考，不要改源码），回答：构造函数、容量断言（行 122 `(L1A_SIZE * STAGES + L1B_SIZE * STAGES) <= L1_SIZE`）、事件数组长度分别会受什么影响？

**需要观察的现象**：多缓冲数组长度、循环次数、容量断言都与 `STAGES` 线性相关——这就是「缓冲块数」这一参数贯通构造、断言、运行三处的体现。

**预期结果**：你能口述出「`STAGES` 加 1，L1/L0 的占用翻倍、事件数翻倍、但搬运与计算的重叠机会也增加」。

> 待本地验证：若有昇腾环境，可对比 `00_basic_matmul`（乒乓）与一个假设的「单缓冲」实现（不易构造，仅供思考）在仿真器下的 MTE2/MTE1 利用率差异。

#### 4.1.5 小练习与答案

**练习 1**：构造函数里为什么要 `SetFlag<MTE1_MTE2>` 和 `SetFlag<M_MTE1>`，却不 `SetFlag<MTE2_MTE1>`？

**参考答案**：因为主循环开头是搬运（MTE2 写 L1、MTE1 写 L0）等待「缓冲可写」的 `WaitFlag<MTE1_MTE2>`/`WaitFlag<M_MTE1>`，必须预置这两个方向的 flag 才能放行第一轮；而「数据就绪」方向 `MTE2_MTE1` 是第一轮搬运**之后**由 `SetFlag<MTE2_MTE1>` 自然产生的，不需要预置。

**练习 2**：`L0C` 为什么不是 `[STAGES]` 数组，而是单个 `l0CTensor`（行 169）？

**参考答案**：本模板里 L0C 用作累加器，一个 C 基本块在 K 维上**持续累加**到同一块 L0C，最后统一搬出；它不需要像 A/B 那样在「搬入」与「消费」之间乒乓，因此单块即可（L0C 的多缓冲只在与 `ENABLE_UNIT_FLAG`、写出并行相关的更高级策略里才出现，见 4.3）。

---

### 4.2 Preload 预加载

#### 4.2.1 概念说明

乒乓解决了「块内」搬运与计算的重叠，但仿真发现一个新的空泡，文档 [04_matmul_summary.md:314-318](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/2_Design/01_kernel_design/04_matmul_summary.md#L314-L318) 这样描述：

> MTE2 流水上，"当前 C 矩阵基本块计算的最后一个 A 矩阵（B 矩阵）的 tile 块"和"下一个 C 矩阵基本块计算的第一个 A 矩阵（B 矩阵）的 tile 块"之间加载的**空泡**。

也就是说：一个核会被 Kernel 层分配多个 C 基本块（SPMD 步长循环），每算完一个块（`blockMmad` 返回一次），紧接着算下一个块时，MTE2 要从零开始搬下一个块的第一片 A/B。这块「跨块衔接」处，MTE2 因为没有提前发指令而出现一段空闲——**跨块空泡**。

**优化思路（Preload）**：在算当前块时，**提前把下一个块的第一片 A/B 搬进来**（搬进乒乓的另一块缓冲）。文档给的伪代码 [04_matmul_summary.md:324-337](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/2_Design/01_kernel_design/04_matmul_summary.md#L324-L337)：

```cpp
for ... {
    copyGM2L1A;      // 搬入当前轮
    copyGM2L1B;
    preload_count++;
    if (preload_count == PRELOAD_STAGES) {
        copyL12L0A;  // 计算前 PRELOAD_STAGES 轮的数据
        copyL12L0B;
        Mmad;
    }
}
```

直觉上：把「读」和「算」错开一轮——读第 N 轮数据时，算第 N-1 轮数据。这样 MTE2 始终有活干，跨块衔接处不再空等。

#### 4.2.2 核心流程

CATLASS 的 `MmadAtlasA2Preload` 把 Preload 落在 Block 层，做法是**给 `blockMmad` 多塞一份「下一个 C 块」的信息**，让它在算当前块最后一轮 K 时，顺手把下一块的第一片 A/B 搬进来：

1. **Kernel 侧**：算当前块偏移的同时，**手动算出下一个块**（同核步长 `GetBlockNum()` 之后那个块）的 GM 偏移与真实形状，作为 `gmNextBlockA/B`、`actualShapeNext` 传进 `blockMmad`，并用 `hasNextBlock` 标志是否存在下一个块。
2. **Block 侧**：在 K 主循环里，区分三种搬运：
   - 首片：`isFirstBlock` 时搬当前块第一片（让首次进入循环有数据可算）；
   - 块内下一片：`shuffleKIdx != lastTileIdx` 时提前搬当前块下一片（块内预加载，乒乓也做）；
   - **跨块首片**：`shuffleKIdx == lastTileIdx && hasNextBlock` 时，搬**下一个块**的第一片（这是 Preload 相对乒乓多出来的关键一步）。
3. 索引 `l1ListId` 仍乒乓轮转，跨块首片正好落在「另一块」缓冲上，等下一个 `blockMmad` 调用进来时直接可用。

附带还有一个 `ENABLE_SHUFFLE_K`：让每个核从 K 方向不同的起始片开始搬，错开多核同址读取冲突（u4-l2 已讲过原理，本讲关注它在代码里的落地）。

#### 4.2.3 源码精读

**策略标签。** `MmadAtlasA2Preload` 在乒乓基础上多了 `ENABLE_SHUFFLE_K`，`STAGES` 仍为 2：

[dispatch_policy.hpp:59-64](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/dispatch_policy.hpp#L59-L64) —— `STAGES = 2`、`ENABLE_UNIT_FLAG`、`ENABLE_SHUFFLE_K` 三个编译期参数。

**`blockMmad` 签名多了「下一块」。**

[block_mmad_preload.hpp:134-141](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_preload.hpp#L134-L141) —— 相比乒乓，参数多了 `gmNextBlockA`、`gmNextBlockB`、`actualShapeNext`、`isFirstBlock`、`hasNextBlock`。这就是「需要 Kernel 手动传下一块信息」的接口体现。

**ShuffleK 落地。**

[block_mmad_preload.hpp:155-163](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_preload.hpp#L155-L163) —— `startTileIdx = GetBlockIdx()`，`firstTileIdx = startTileIdx % kTileCount`，每个核因此从不同的 K 片起搬，正是文档 [04_matmul_summary.md:429-435](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/2_Design/01_kernel_design/04_matmul_summary.md#L429-L435) 描述的错位。

**块内预加载（与乒乓相同的一步）。**

[block_mmad_preload.hpp:193-218](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_preload.hpp#L193-L218) —— `if (shuffleKIdx != lastTileIdx)`，提前把当前块下一片 A/B 搬进 `l1ListIdNext` 那块缓冲。

**跨块预加载（Preload 的核心一步）。**

[block_mmad_preload.hpp:219-242](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_preload.hpp#L219-L242) —— `if (shuffleKIdx == lastTileIdx && hasNextBlock)`，在算当前块**最后一片**K 的同时，把**下一个块的第一片** A/B（`gmNextBlockA/B`、`firstTileIdxNext`）搬进 `l1ListIdNext`。这一段代码是「跨块空泡」被消除的直接来源：下一个 `blockMmad` 进来时，它的首片数据已经在 L1 的另一块缓冲里等着了。

**Kernel 如何「手动算下一块」。**

[basic_matmul_preload.hpp:135-153](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul_preload.hpp#L135-L153) —— Kernel 用 `loopIdx + GetBlockNum()` 预测同核的下一个块坐标 `nextBlockIdCoord` 与真实形状 `nextActualBlockShape`，算出 `gmOffsetNextA/B`，最后把 `gmA[gmOffsetNextA]`、`gmB[gmOffsetNextB]` 一并传给 `blockMmad`（行 150-153）。文档 [04_matmul_summary.md:353](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/2_Design/01_kernel_design/04_matmul_summary.md#L353) 把它概括为「需要在 kernel 内手动计算传入下一块预载数据的信息」。

**样例里的启用。**

[optimized_matmul.cpp:74-86](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/06_optimized_matmul/optimized_matmul.cpp#L74-L86) —— `06_optimized_matmul` 同时开了 `ENABLE_UNIT_FLAG` 与 `ENABLE_SHUFFLE_K`，并用 `MmadAtlasA2Preload` 作为 DispatchPolicy。

#### 4.2.4 代码实践

**目标**：把 Preload「跨块首片提前搬」的时间线画出来，解释空泡为何被消除。这是本讲的主实践（源码阅读型）。

**步骤**：

1. 读文档 [04_matmul_summary.md 的「流水优化（Preload）」一节（行 311-357）](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/2_Design/01_kernel_design/04_matmul_summary.md#L311-L357)，重点看它对三种策略的对比（行 339-349）。
2. 对照 [block_mmad_preload.hpp:219-242](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_preload.hpp#L219-L242)（跨块首片搬运）与 [block_mmad_pingpong.hpp:224-252](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_pingpong.hpp#L224-L252)（乒乓只在 `kLoopIdx < kTileCount - 1` 时预加载、最后一片不预载）。
3. 在纸上为单个核处理两个相邻 C 块（C1、C2，各只需 2 次 K 分块）画两条时间线，MTE2 / MTE1 / M 三行：

   ```
   乒乓：MTE2 [A0 B0][A1 B1]_______[A2 B2][A3 B3]   ← C1最后一片后出现空泡_______
   MTE1           [读][读]          [读][读]
   M                  [算][算]          [算][算]

   Preload：MTE2 [A0 B0][A1 B1][A2 B2][A3 B3]...   ← C2首片(A2)在C1最后一片时提前搬，无空泡
   MTE1           [读][读 ][读][读 ]
   M                  [算][算][算][算]
   ```

   （A0/B0、A1/B1 属于 C1；A2/B2、A3/B3 属于 C2。`___` 表示 MTE2 空等。）

**需要观察的现象**：在乒乓时间线的 C1→C2 衔接处，MTE2 有一段空闲（因为下一块首片要等新的 `blockMmad` 调用进来才发指令）；Preload 时间线里这段被填上了「提前搬 C2 首片」。

**预期结果**：你能解释——`block_mmad_preload.hpp` 行 219-242 在 `hasNextBlock` 时把下一块首片搬进 `l1ListIdNext`，使得下一个 `blockMmad` 的首片搬运（行 172-188 的 `isFirstBlock` 分支）实际上**已经被上一轮提前完成**，MTE2 不再有空闲间隙，这正是文档行 347 所说「A3、B3 块的 GmToL1 搬运提前，减缓了 MTE2 上的搬运空泡」。

> 待本地验证：用 `--simulator`（u1-l4）跑 `06_optimized_matmul` 与 `00_basic_matmul`，对比仿真出的 MTE2 利用率。

#### 4.2.5 小练习与答案

**练习 1**：`blockMmad` 参数里 `actualShape` 与 `actualShapeNext` 分别表示什么？为什么 Preload 需要两个？

**参考答案**：`actualShape` 是**当前** C 基本块的真实尺寸（可能因触边裁剪小于 L1TileShape），用来决定当前块搬多少、算多少；`actualShapeNext` 是**同核下一个**块的真实尺寸，用来计算跨块预搬首片的真实长度 `kActualNext`（行 224-226）。乒乓没有「下一块」概念，所以只需一个。

**练习 2**：`isFirstBlock` 分支（行 172-188）既然有 `hasNextBlock` 已经提前搬了首片，为什么还要它？

**参考答案**：`hasNextBlock` 处理的是「上一个块预搬了本块首片」的情况，但**最开始的第一个块**没有「上一个块」替它预搬，必须由 `isFirstBlock` 分支自己把首片搬进来；同理 `hasNextBlock == false`（本核的最后一个块）时不再预搬下一块。二者共同保证首尾边界正确。

---

### 4.3 Async 与 Callback

#### 4.3.1 概念说明

`MmadAtlasA2Preload` 有一个工程负担：**Kernel 必须手动算下一块偏移**并多传四个参数（`gmNextBlockA/B`、`actualShapeNext`、`hasNextBlock`）。当策略变复杂（比如 GroupedMatmul、带量化的后处理），这块手算逻辑会越来越繁琐。

`MmadAtlasA2PreloadAsync` 用一个更优雅的机制——**延迟队列**——达成同样的「跨块预加载」，且 Kernel 不必算下一块。它的策略标签继承自 `MmadAtlasA2Async`（`ASYNC=true`，[dispatch_policy.hpp:94-105](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/dispatch_policy.hpp#L94-L105)），关键参数：

- `PRELOAD_STAGES`：提前几轮发搬运指令（即「计算比搬运滞后几轮」）。
- `L1_STAGES`/`L0A_STAGES`/`L0B_STAGES`/`L0C_STAGES`：各级缓冲**独立**配置块数，不再像乒乓那样共享一个 `STAGES`。

直觉：把整个 Block 主循环拆成「**发射 GM→L1**」和「**执行 L1→L0→Mmad→搬出**」两段。`operator()` 只负责发射搬运并把「这一轮要算什么」记进一个参数表；真正的计算被推迟 `PRELOAD_STAGES` 轮才执行。因为「算第 N 轮」与「搬第 N+PRELOAD_STAGES 轮」天然重叠，**跨块的搬运空泡被这个统一的延迟队列自然吸收**——块边界不再是特殊点，Kernel 自然不用为它做特殊处理。

更进一步，`MmadAtlasA2PreloadAsyncWithCallback`（承载于 `block_mmad_preload_async_with_callback.hpp`）允许在 blockMmad **计算前后**注入 `Callback`；而 `MmadAtlasA2PreloadAsync` 本身的 `operator()` 已经支持一个「mmad 计算完成后」的 `Callback`（文档 [04_matmul_summary.md:354-355](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/2_Design/01_kernel_design/04_matmul_summary.md#L354-L355)）。这个回调常用来挂「per-token 反量化」之类紧跟在 C 块搬出之后的后处理。

#### 4.3.2 核心流程

1. **各级缓冲独立 N-buffer**：构造时按 `L1_STAGES`/`L0A_STAGES`/`L0B_STAGES`/`L0C_STAGES` 分别切缓冲、发初始 flag。
2. **`operator()` 只发射 + 记账**：K 主循环每一轮——
   - 发 `copyGmToL1A/B`（GM→L1）；
   - 若 `preloadCount == PRELOAD_STAGES`，调用一次 `L1TileMmad(...)`，把 `PRELOAD_STAGES` 轮前搬进来的数据真正算掉；
   - 把「本轮搬进来的数据要怎么算」写进参数表 `l1TileMmadParamsList`；
   - `l1ListId` 在 `L1_STAGES` 块里轮转。
3. **`SynchronizeBlock()` 收尾**：循环结束后，参数表里还剩 `PRELOAD_STAGES` 轮「搬了没算」的数据，靠 `SynchronizeBlock()` 依次算完（析构函数会自动调用它）。
4. **`Callback`**：在 `L1TileMmad` 内，当某轮是 K 的最后一轮（`isKLoopLast`），搬出 L0C→GM 后调用 `params.callback()`。

#### 4.3.3 源码精读

**各级独立缓冲。**

[block_mmad_preload_async.hpp:64-68](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_preload_async.hpp#L64-L68) —— `PRELOAD_STAGES`/`L1_STAGES`/`L0A_STAGES`/`L0B_STAGES`/`L0C_STAGES` 各自从策略取出。

[block_mmad_preload_async.hpp:85-91](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_preload_async.hpp#L85-L91) —— 容量断言按各级独立 stage 校验，例如 `(L1A_TILE_SIZE + L1B_TILE_SIZE) * L1_STAGES <= L1_SIZE`、`L0C_TILE_SIZE * L0C_STAGES <= L0C_SIZE`。注意 `L0C_TILE_SIZE` 这里按 `L1TileShape::M*N` 算（行 79），因为整个 C 基本块的累加结果要放得下 L0C。

**`operator()` 只搬运 + 记账（无下一块参数）。**

[block_mmad_preload_async.hpp:133-139](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_preload_async.hpp#L133-L139) —— 签名只有当前块的 `gmBlockA/B/C` 与 `actualShape`，加一个可选 `Callback&&`。对比 [block_mmad_preload.hpp:134-141](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_preload.hpp#L134-L141)，**没有** `gmNextBlockA/B`、`actualShapeNext`、`hasNextBlock`——这是「无需手动算下一块」的直接证据。

[block_mmad_preload_async.hpp:150-201](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_preload_async.hpp#L150-L201) —— K 主循环：先发 `copyGmToL1A/B`（行 163-171），再在 `preloadCount == PRELOAD_STAGES` 时调 `L1TileMmad`（行 174-176），最后把本轮参数存进 `l1TileMmadParamsList`（行 178-199），`l1ListId` 在 `L1_STAGES` 内轮转（行 200）。整个 `operator()` 不做任何 Mmad，只发射与记账。

**`L1TileMmad` 是真正的「算」。** 它内部就是一份完整的 L1→L0→Mmad→（末轮）搬出流程（行 276-378），与乒乓主循环体同构，但被「延迟 `PRELOAD_STAGES` 轮」调用。

**收尾 `SynchronizeBlock`。**

[block_mmad_preload_async.hpp:204-212](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_preload_async.hpp#L204-L212) —— 循环里只把「搬进来」的算到「滞后 `PRELOAD_STAGES` 轮」，最后总有 `PRELOAD_STAGES` 轮留在参数表里没算，`while (preloadCount > 0)` 依次调 `L1TileMmad` 把它们算完。析构函数（行 114-131）开头先调 `SynchronizeBlock()`，保证搬出的数据都已计算完毕。

**Callback 触发点。**

[block_mmad_preload_async.hpp:361-377](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_preload_async.hpp#L361-L377) —— 在 `isKLoopLast` 分支里，`copyL0CToGm` 搬出 C 块后，`if (params.callback) params.callback();`。这个 `callback` 是在 K 最后一轮（行 189-193）连同 `gmBlockC`、`layoutCInGm` 一起存进参数表的。

**`Callback` 本体。**

[callback.hpp:22-41](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/detail/callback.hpp#L22-L41) —— `Callback` 是 `std::function<void()>` 的轻量替代：一个 `func` 指针 + 一个 `caller` 函数指针，`operator()` 在 `func` 非空时调用。它能携带带捕获的 lambda，但不持有捕获对象的生命周期。用 [callback.hpp:50-57](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/detail/callback.hpp#L50-L57) 的 `MakeCallback` 工厂构造。

#### 4.3.4 代码实践

**目标**：用一张「延迟队列」示意图，说清 PreloadAsync 为什么不用手动传下一块。

**步骤**：

1. 读 [block_mmad_preload_async.hpp:150-212](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_preload_async.hpp#L150-L212)，假设 `PRELOAD_STAGES = 1`、一个核处理两个块 C1（K 切 2 片）、C2（K 切 2 片）。
2. 画出每一轮 `operator()` 做了什么（设「搬」=GM→L1、「算」=L1TileMmad）：

   ```
   轮次（跨块连续编号）:   T0        T1        T2        T3        收尾
   搬入:                搬C1片0   搬C1片1   搬C2片0   搬C2片1   ——
   算(PRELOAD_STAGES=1): ——       算C1片0   算C1片1   算C2片0   算C2片1
   ```

3. 观察：T2 这一轮「搬 C2 片 0」和「算 C1 片 1」同时进行，跨块首片（C2 片 0）的搬运**自然**与上一块的计算重叠，没有任何特殊分支。

**需要观察的现象**：跨块衔接（T1→T2）处 MTE2 连续有搬运指令，不存在空泡；而且代码里完全没有「下一块」相关参数。

**预期结果**：你能解释——PreloadAsync 把「计算整体滞后 `PRELOAD_STAGES` 轮」，块边界不再是特殊点，跨块预加载由延迟队列**隐式**完成，因此 Kernel 无需（也无法）传下一块信息；代价是必须记得在所有块结束后调一次 `SynchronizeBlock()`（或依赖析构）把滞后的末尾算完。

> 待本地验证：对比 `MmadAtlasA2Preload`（[optimized_matmul.cpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/06_optimized_matmul/optimized_matmul.cpp)）与使用 `MmadAtlasA2PreloadAsync` 的样例 host 代码，确认后者组装 blockMmad 时不再出现 `gmNextBlockA` 之类参数。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `SynchronizeBlock()` 不能省？不调会怎样？

**参考答案**：`operator()` 只在 `preloadCount == PRELOAD_STAGES` 时才算，循环结束时参数表里仍剩 `PRELOAD_STAGES` 轮「搬了没算」的数据，对应 C 块最后几片 K 没被累加、也没被搬出。不调 `SynchronizeBlock()` 会导致结果不完整/未搬出；好在析构函数（行 117）会自动调一次。

**练习 2**：`PRELOAD_STAGES`、`L1_STAGES`、`L0C_STAGES` 三者必须满足什么容量与逻辑约束？至少说出两条。

**参考答案**：① 容量：`(L1A_TILE_SIZE+L1B_TILE_SIZE)*L1_STAGES ≤ L1_SIZE`、`L0C_TILE_SIZE*L0C_STAGES ≤ L0C_SIZE`（行 85-91）；② 逻辑：`PRELOAD_STAGES` 不能超过 `L1_STAGES`，否则要算的数据其所在 L1 块还没被「缓冲可重写」释放、或要被新一轮搬运覆盖，造成数据竞争；③ 若 `L0C_STAGES > 1` 则不能开 `ENABLE_UNIT_FLAG`（这是 `MmadAtlasA2SingleCoreSplitk` 里 `static_assert` 的同类约束，见 [dispatch_policy.hpp:50](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/dispatch_policy.hpp#L50) 的同款限制）。

**练习 3**：`Callback` 与直接在 Kernel 里写后处理相比，优势在哪？

**参考答案**：`Callback` 把「C 块搬出之后做什么」交给调用方注入（如 per-token 反量化），blockMmad 不必为每种后处理各写一份主循环；它是「在设备侧、紧跟计算流水」的钩子，能复用 blockMmad 已有的缓冲与同步状态，避免后处理另起一套搬运。

---

## 5. 综合实践

把三个最小模块串起来，完成一次「策略选型 + 流水分析」的小任务。

**背景**：假设你要为一个 `(M, N, K) = (4096, 4096, 4096)` 的 fp16 GEMM 选 Block 层策略，且数据搬运（MTE2）疑似是主流水瓶颈。

**任务**：

1. 打开 [04_matmul_summary.md 的「工程优化清单」](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/2_Design/01_kernel_design/04_matmul_summary.md#L276-L558)，定位「流水优化（Multi Buffer）」「流水优化（Preload）」两节。
2. 在三种策略中做选择并说明理由：
   - 若只是要让搬运与计算重叠、且不想改 Kernel → 选 `MmadAtlasA2Pingpong`（它是默认底座，无需手动算下一块）。
   - 若仿真发现相邻 C 块之间有 MTE2 空泡、且能接受改 Kernel 手算下一块 → 选 `MmadAtlasA2Preload`，参考 [basic_matmul_preload.hpp:135-153](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul_preload.hpp#L135-L153)。
   - 若还想要异步 N-buffer、不想手算下一块、或要在 C 块搬出后接后处理 → 选 `MmadAtlasA2PreloadAsync`（或 `WithCallback`），并记得 `SynchronizeBlock()`。
3. 选定 `MmadAtlasA2Preload` 后，画出单核处理两个相邻 C 块的 MTE2/MTE1/M 时间线（参照 4.2.4），用红笔标出「若无 Preload 会出现空泡」的位置，再标出 [block_mmad_preload.hpp:219-242](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_preload.hpp#L219-L242) 把它填上的那段搬运。

**验收标准**：你能对着自己画的时间线，指出「跨块空泡」的物理位置，并说出是哪几行代码把它消除的——这就把 Multi Buffer、Preload、跨块预加载三件事打通了。

> 待本地验证：在昇腾环境或 `--simulator` 下，分别用 `00_basic_matmul`（乒乓）、`06_optimized_matmul`（Preload）、以及一个 PreloadAsync 样例跑同一 shape，记录耗时与 MTE2 利用率，验证上面的定性分析。

---

## 6. 本讲小结

- **Multi Buffer（乒乓）** = 缓冲数组（`STAGES=2`）+ 轮转索引（`l1ListId` 等）+ 成对 HardEvent 握手；构造时预置初始 flag 让首轮放行，是所有 blockMmad 的默认底座，让 MTE2/MTE1/M 三条 PIPE 真正并行。
- **Preload（跨块预加载）** 针对「相邻 C 块之间 MTE2 空泡」：`blockMmad` 多收一份「下一块」信息，在算当前块最后一片 K 时（[block_mmad_preload.hpp:219-242](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_preload.hpp#L219-L242)）提前搬下一块首片；代价是 Kernel 要手算下一块偏移。
- **PreloadAsync（延迟队列）** 把「算」整体滞后 `PRELOAD_STAGES` 轮，`operator()` 只发射 GM→L1 + 记账，跨块预加载由延迟队列隐式完成，Kernel 不必传下一块；末尾靠 `SynchronizeBlock()` 收尾。
- **Callback** 是挂在「C 块搬出之后」的设备侧钩子，承载于轻量 `Callback` 对象，常用于 per-token 反量化等紧跟计算的后处理。
- 三种策略能力递进、复杂度递进：乒乓最省心、Preload 需改 Kernel、PreloadAsync 更通用但要管收尾与多级独立 stage 的容量约束。

---

## 7. 下一步学习建议

- **向下游（Tile 层）**：Block 层的 `copyGmToL1A`、`tileMmad` 等组件内部如何映射到 AscendC 的 `DataCopy`/`LoadData`/`Mmad`/`Fixpipe`，将在 U5（Tile 层与硬件指令）拆解，建议接着读 u5-l1、u5-l2。
- **向优化（理论模板与调优）**：本讲的 Preload/ShuffleK 属于「工程优化」，它们如何与「理论模板」（Common/SplitK）组合、以及如何据 shape 选模板，见 U8，尤其 u8-l1（理论模板总结）与 u8-l3（Padding 与 ShuffleK 读写带宽优化）。
- **向扩展（异步与回调的用武之地）**：`PreloadAsyncWithCallback` 与 per-token 反量化、GroupedMatmul 的组合，见 U6（Epilogue 后处理体系）与 U9（复杂算子场景）。
