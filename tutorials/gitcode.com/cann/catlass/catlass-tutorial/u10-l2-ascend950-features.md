# Ascend950 特有能力

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 Ascend950 上**基于 Mutex 互斥锁的 BlockMmad**（`block_mmad_pingpong_mutex_tla.hpp`）是如何用「每个缓冲一份 MutexID」替代传统 HardEvent 事件同步的，以及它相对事件同步的优势。
- 掌握 Ascend950 **MX 量化模板**（`MmadMx` / `MmadA8W4Mx`）的组织方式，理解 MXFP8/MXFP4 微缩放量化、二级量化与 grouped（分组）形态在 kernel 内如何落地，并能对照 53/63/65/71 四个样例区分它们的协作路径。
- 理解 Ascend950 上 **EVG 后处理 + visitor kernel** 的标准用法（样例 64 的 7 种融合），并能说清楚样例 71 中**确定性调度（`ColumnBlockSwizzle`）**与**非确定性调度（`GemmGroupedAswtTailSplitSwizzle`）**在负载均衡与确定性上的取舍。

本讲承接 [u10-l1](u10-l1-a2-to-950-migration.md)（A2→950 迁移工作流）与 [u6-l4](u6-l4-evg-execution-extension.md)（EVG 执行模型），把视角从「迁移」转向「950 原生的新能力」。

## 2. 前置知识

在进入本讲前，请确保你已掌握以下概念（它们在前序讲义中已建立，本讲不重复展开）：

- **五层抽象与三层嵌套循环**：Device→Kernel→Block→Tile→Basic，Block 层即 K 维主循环（见 [u4-l1](u4-l1-block-mmad-mainloop.md)）。
- **多缓冲 Pingpong 与事件同步**：传统 `block_mmad_pingpong` 用 STAGES 片乒乓缓冲 + `SetFlag/WaitFlag`（HardEvent）在 MTE2/MTE1/M/FIX 四条 PIPE 间握手（见 [u4-l3](u4-l3-multibuffer-preload.md)）。本讲要讲的 Mutex 是它的 950 替代方案。
- **DispatchPolicy 是零开销策略标签**：作为 `BlockMmad` 首个模板参数决定实例化哪份主循环实现（见 [u4-l2](u4-l2-dispatch-policy.md)）。
- **量化矩阵乘三条路径**：权重 epilogue/prologue 反量化、FP8 cast、MX 微缩放（见 [u9-l2](u9-l2-quant-matmul.md)）。本讲的 MX 模板是其中 MX 路径的 950 深化。
- **TLA Tile 编程**：用 `MakeTensor`/`GetTile`/`TileView`/`MakeTensorLike` 描述「视图不搬运数据」（见 [u7-l2](u7-l2-tla-tensor-view.md) 与 [u7-l3](u7-l3-tla-matmul.md)）。本讲的 Mutex BlockMmad 与 MX kernel 全部基于 TLA 写法。
- **硬件差异**：Ascend950 相对 AtlasA2，L0C 翻倍（128KB→256KB）、引入 Mutex 互斥锁原语与原生 MX 指令（见 [u3-l3](u3-l3-arch-position.md)）。

两个本讲会用到的术语，先统一口径：

- **Mutex（互斥锁）**：Ascend950 提供的硬件同步原语 `AscendC::Mutex::Lock<PIPE>(id)` / `Unlock<PIPE>(id)`，用 0~27 的 MutexID 标识一把「锁」。生产者写缓冲前 Lock、写完 Unlock；消费者读缓冲前 Lock、读完 Unlock。同一把锁的 Lock/Unlock 配对即可保证对该缓冲的互斥访问。
- **MX（Microscaling）量化**：按 OCP MX 规范，每 32 个元素共享一个 `e8m0`（仅指数的 8 位浮点）缩放因子，称 per-32 微缩放。Ascend950 有原生 `MmadMx` 指令直接吃「数据 + 微缩放因子」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/catlass/gemm/dispatch_policy.hpp](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/include/catlass/gemm/dispatch_policy.hpp) | 策略标签总表，含 `MmadPingpongMutex`（行 317）、`MmadMx`（行 434）、`MmadA8W4Mx`（行 450） |
| [include/catlass/gemm/block/block_mmad_pingpong_mutex_tla.hpp](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/include/catlass/gemm/block/block_mmad_pingpong_mutex_tla.hpp) | 本讲主角之一：基于 Mutex 同步的 TLA 版 BlockMmad 主循环 |
| [include/catlass/gemm/kernel/mx_matmul_tla.hpp](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/include/catlass/gemm/kernel/mx_matmul_tla.hpp) | MX 矩乘 kernel：`MxMatmulTlaBase` 与 AIC/AIV 双特化的 `operator()` |
| [include/catlass/gemm/block/block_scheduler_aswt.hpp](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/include/catlass/gemm/block/block_scheduler_aswt.hpp) | `ColumnBlockSwizzle` 与 `GemmGroupedAswtTailSplitSwizzle`（滚动核分配 + 尾块拆分）调度器 |
| [examples/53_ascend950_fp8_mx_matmul/README.md](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/examples/53_ascend950_fp8_mx_matmul/README.md) | 950 原生 MX FP8 矩乘样例（基线） |
| [examples/63_ascend950_dual_level_quant_mx_batch_matmul/README.md](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/examples/63_ascend950_dual_level_quant_mx_batch_matmul/README.md) | 二级量化 + MX FP4 batch matmul（本次新迁入正式样例） |
| [examples/64_ascend950_matmul_evg/README.md](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/examples/64_ascend950_matmul_evg/README.md) | 950 EVG 后处理 7 种融合样例 |
| [examples/71_ascend950_fp8_mx_grouped_matmul_finalize_routing/...cpp](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/examples/71_ascend950_fp8_mx_grouped_matmul_finalize_routing/fp8_mx_grouped_matmul_finalize_routing.cpp) | grouped MX FP8 matmul + FinalizeRouting，确定性版组装（行 327-350） |

> 本次更新（`53a42be2 → 4fab1d09`）把 63/65/71 三个样例从 `experimental/` 提升为 `examples/` 正式样例，并补齐了 `gen_data*.py`、`tests/test_example.py` 测试用例与 `01_example_design.md` 的样例链接。它们对应的 include 模板头文件（`block_epilogue_dual_level_quant_mx.hpp`、`dual_level_quant_mx_batched_matmul_tla.hpp` 等）本讲只点到为止，重点放在 950 的三类共性能力上。

## 4. 核心概念与源码讲解

### 4.1 Mutex 同步 BlockMmad

#### 4.1.1 概念说明

回顾 [u4-l3](u4-l3-multibuffer-preload.md)：传统 `block_mmad_pingpong` 在 L1/L0 多缓冲之间，靠 **HardEvent**（`SetFlag<PIPE_A, PIPE_B>` / `WaitFlag<...>`）来同步——它本质是「成对 PIPE 之间」的事件计数。这种方式有一个特点：**同步粒度绑在 PIPE 对上**，同一对 PIPE 之间所有缓冲共享一套 flag，调度上较粗。

Ascend950 引入了硬件级 **Mutex 互斥锁原语**，思路完全不同：给**每一片缓冲单独分配一把锁（一个 MutexID）**，谁要访问这片缓冲（无论读还是写），就先 `Lock` 这把锁、用完 `Unlock`。于是同步从「PIPE 对」细化到了「单个缓冲片」，互不冲突的缓冲可以真正并行推进。

`MmadPingpongMutex` 就是把这套 Mutex 同步搬进 Block 层主循环的 950 专用策略标签，配套实现是 `block_mmad_pingpong_mutex_tla.hpp` 中的 `BlockMmadTla` 偏特化。

#### 4.1.2 核心流程

Mutex 版主循环的执行过程（与 [u4-l1](u4-l1-block-mmad-mainloop.md) 的四类操作一一对应）：

1. **构造期分配缓冲与 MutexID**：为 L1A/L1B/L0A/L0B/L0C（可选 bias）每个 stage 各开一片缓冲，并按「连续偏移」给每片分配一个 MutexID。
2. **GM→L1（MTE2）**：`Mutex::Lock<PIPE_MTE2>(l1AMutexList[id])` → `copyGmToL1A` → `Unlock`。
3. **L1→L0A/L0B（MTE1）**：对源 L1 片与目的 L0 片各加一把锁 → `copyL1ToL0A` → 双 Unlock。
4. **TileMmad 计算（M）**：对参与计算的 L0A/L0B/L0C 片分别加锁 → `tileMmad` → 解锁。
5. **L0C→GM（FIX）**：`Mutex::Lock<PIPE_FIX>(l0CMutexList[id])` → `copyL0CToDst` → `Unlock`。

关键点：第 3、4 步**对同一片缓冲要同时持有「生产者锁」和「消费者锁」**——例如 L1→L0A 时既 Lock 源 L1A 片（防止 MTE2 还在写）、又 Lock 目的 L0A 片（防止上一轮 M 还在读）。这就是 Mutex「按缓冲片互斥」的写法。

#### 4.1.3 源码精读

**(1) 策略标签：仅限 Ascend950。** `MmadPingpongMutex` 继承自 `MmadBase<ArchTag, false>`，并在编译期用 `static_assert` 把架构锁死为 950——Mutex 是 950 才有的硬件原语：

> [dispatch_policy.hpp:317-327](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/include/catlass/gemm/dispatch_policy.hpp#L317-L327) —— `MmadPingpongMutex` 定义，第 318 行断言「仅支持 Ascend950」。它与 `MmadPingpong`（行 302）参数表完全对应（同样有 L1A/L1B/L0A/L0B/L0C STAGES、ENABLE_UNIT_FLAG、USE_HF32_MODE、ENABLE_L1_RESIDENT），只是同步机制不同。

**(2) MutexID 分配：连续偏移，上限 28 把。** 构造期把 L1A→L1B→L0A→L0B→L0C→bias 的 MutexID 连续编号，并用 `static_assert` 卡死总数不超过 28（硬件支持 0~27）：

> [block_mmad_pingpong_mutex_tla.hpp:133-141](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/include/catlass/gemm/block/block_mmad_pingpong_mutex_tla.hpp#L133-L141) —— MutexID 偏移计算与 `TOTAL_MUTEX_IDS <= 28` 断言。注意 `L1A_MUTEX_OFFSET=0` 起，后续每段累加上一段的 STAGES，bias 视 `HAS_BIAS` 决定是否占两把（L1/L0 各一）。

**(3) 构造函数：给每片缓冲绑 MutexID。** 下面这段在 AIC 侧为 L1A 的每个 stage 分配缓冲地址与 MutexID，L1B/L0A/L0B/L0C 同理：

> [block_mmad_pingpong_mutex_tla.hpp:183-194](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/include/catlass/gemm/block/block_mmad_pingpong_mutex_tla.hpp#L183-L194) —— `l1ATensorList[i]` 拿到 L1 偏移，`l1AMutexList[i] = L1A_MUTEX_OFFSET + i` 拿到对应的锁编号。

**(4) GM→L1 搬运：Lock-copy-Unlock 三明治。** 以矩阵 A 首片搬运为例（非 L1 常驻模式）：

> [block_mmad_pingpong_mutex_tla.hpp:295-298](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/include/catlass/gemm/block/block_mmad_pingpong_mutex_tla.hpp#L295-L298) —— `Lock<PIPE_MTE2>` → `copyGmToL1A` → `Unlock<PIPE_MTE2>`，锁的是这片 L1A 缓冲，保护它不被同时访问。

**(5) L1→L0 搬运：同时锁源片与目的片。** 这是 Mutex「按缓冲片」特性的精髓——MTE1 从 L1A 读、写 L0A，于是两片都上锁：

> [block_mmad_pingpong_mutex_tla.hpp:418-422](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/include/catlass/gemm/block/block_mmad_pingpong_mutex_tla.hpp#L418-L422) —— `Lock<PIPE_MTE1>(l1AMutexList[...])` 锁源 L1A、`Lock<PIPE_MTE1>(l0AMutexList[...])` 锁目的 L0A，搬完按相反顺序双 Unlock。

**(6) 计算：对参与运算的 L0 片逐一上锁。** Mmad 同时读 L0A、L0B，写 L0C（unitFlag 关时）：

> [block_mmad_pingpong_mutex_tla.hpp:474-500](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/include/catlass/gemm/block/block_mmad_pingpong_mutex_tla.hpp#L474-L500) —— `Lock<PIPE_M>` 分别锁 L0A/L0B（开 unitFlag 时不锁 L0C，因为 unitFlag 让 Mmad 与 L0C→GM 随路并行，无需互斥；见第 476-478 行的 `if constexpr (!ENABLE_UNIT_FLAG)`），调用 `tileMmad` 后解锁。

**(7) L1 常驻模式（ENABLE_L1_RESIDENT）的数据复用。** 这也是 Mutex 版的特色能力：若连续两次 `blockMmad` 调用读的是同一片 GM 数据（指针 + 坐标都没变），就跳过 GM→L1 搬运，直接复用 L1 里已有的内容：

> [block_mmad_pingpong_mutex_tla.hpp:281-293](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/include/catlass/gemm/block/block_mmad_pingpong_mutex_tla.hpp#L281-L293) —— 比较 `lastAddrA`/`lastCoordA` 与当前 GM tile，命中则跳过 `copyGmToL1A`。`RestoreStatus()`（行 153-164）在连续调用之间重置这些「上次状态」。

> **Mutex 相对 HardEvent 的优势小结**：① 粒度细到「单缓冲片」，互不冲突的缓冲可并行；② 天然支持「同一缓冲被多个 PIPE 交替读写」的复杂场景（grouped/MX 融合常需要）；③ 配合 L1 常驻模式实现跨块数据复用，减少重复 GM 读取。

#### 4.1.4 代码实践

**实践目标**：在源码层面确认「Mutex 同步如何替代事件同步」，并量化每片缓冲各占几把锁。

**操作步骤**（源码阅读型，无需 NPU）：

1. 打开 [block_mmad_pingpong_mutex_tla.hpp](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/include/catlass/gemm/block/block_mmad_pingpong_mutex_tla.hpp)，对比 [u4-l3](u4-l3-multibuffer-preload.md) 讲过的传统 `block_mmad_pingpong.hpp`（用 `SetFlag/WaitFlag`）。
2. 在本文件里统计 `AscendC::Mutex::Lock` / `Unlock` 出现的位置，把它们按 PIPE（MTE2/MTE1/M/FIX）分类，填一张表。
3. 对照行 133-141 的偏移公式，假设 `L1A_STAGES=2, L1B_STAGES=2, L0A_STAGES=2, L0B_STAGES=2, L0C_STAGES=1, HAS_BIAS=false`，手算 `TOTAL_MUTEX_IDS` 的值。

**需要观察的现象**：每一类搬运/计算操作都被「Lock … 操作 … Unlock」包裹；L1→L0 搬运会出现「双 Lock / 双 Unlock」；`Lock` 的 PIPE 标签与该操作的 PIPE 一致（MTE2 搬运锁 MTE2、Mmad 锁 M）。

**预期结果**：上述参数下，`L1A=0~1, L1B=2~3, L0A=4~5, L0B=6~7, L0C=8`，`TOTAL_MUTEX_IDS = 9`，远小于上限 28。

#### 4.1.5 小练习与答案

**练习 1**：为什么行 108 断言「`L0C_STAGES` 必须为 1 当 `ENABLE_UNIT_FLAG` 为真」？

> **答案**：unitFlag 的作用是让 Mmad 与 L0C→GM（Fixpipe）**随路并行**——计算结果通过 unitFlag 直接驱动搬出，L0C 不再需要显式的多缓冲互斥。若 `L0C_STAGES>1`，多片 L0C 会破坏这种随路并行的单缓冲假设，因此编译期直接禁止。

**练习 2**：`ENABLE_L1_RESIDENT=true` 时，`RestoreStatus()` 为什么必须在「连续两次 blockMmad 调用之间」插入？

> **答案**：常驻模式靠 `lastAddrA/B`、`lastCoordA/B` 记录「上次搬了哪片 GM 数据」来判定是否跳过搬运。若不重置，第二次调用会把第一次调用的尾部状态误判为「已搬入」，从而错误地跳过本该执行的 GM→L1 搬运。`RestoreStatus()` 把这些记录清零，让每次 blockMmad 都从干净的初始态开始。

---

### 4.2 MX 量化模板（含 grouped）

#### 4.2.1 概念说明

MX（Microscaling）量化的核心是「**每 32 个元素共享一个 e8m0 缩放因子**」（per-32 微缩放）。相比 per-tensor（整张图一个 scale）或 per-channel/per-token，MX 在精度与开销间取了折中：粒度足够细以保留精度，scale 体量又足够小（每 32 元素 1 字节）几乎不增加读取带宽。

Ascend950 的关键升级是提供了**原生 MX 指令**：`MmadMx` 直接吃「数据 + 微缩放因子」，无需在 AIV 侧先把 MX 反量化回 fp16 再做普通 matmul。CATLASS 对应的策略标签是 `MmadMx`，对应 kernel 是 `MxMatmulTla`/`MxMatmulTlaBase`。

MX 在 CATLASS 里有三种深化形态，本讲都覆盖：

| 形态 | 含义 | 代表样例 |
| --- | --- | --- |
| MXFP8 | A/B 为 fp8（e4m3/e5m2），per-32 e8m0 scale | 53 |
| **二级量化（dual-level）MXFP4** | 在 per-32 e8m0（LEVEL1）外再加一级 per-512 fp32（LEVEL0），挽救 FP4 的窄表示 | **63**（本次新迁入） |
| grouped MX | 多组变长矩阵的 MX 分组乘，常与 SwiGLU/FinalizeRouting 融合 | **65、71**（本次新迁入） |

此外还有 `MmadA8W4Mx`（激活 fp8/权重量化 int4 的 MX 变体），结构类似，本讲点到为止。

#### 4.2.2 核心流程

**MX 矩乘 kernel 的 AIC 主循环**（`MxMatmulTlaBase::operator()<AIC>`）和普通 matmul 的 SPMD 循环几乎同构，区别在于：

1. **多搬两份 scale**：除了 A/B/C，还要给 `gmMxScaleA`、`gmMxScaleB` 各建一个 GM Tensor，并按 MX 布局 `MakeMxScaleLayout` 构造 layout。
2. **blockMmad 多收 scale 参数**：调用 `blockMmad(tensorA, tensorB, tensorC, blockShape, tensorMxScaleA, tensorMxScaleB[, bias])`，scale 作为额外 tile 传入。
3. **尾波负载均衡（仅 FP8）**：当 A/B 都是 fp8 时，若当前核分到的块数较少（`endBlockIdx_ + 1 <= blockNum / 2`），调用 `UpdateTailTile()` 把尾块拆细，提升多核利用率。

**二级量化的协作路径**（样例 63）走的是「AIV 预量化 → workspace → SyncAll → AIC MX matmul」：

```
AIV: fp16 A/B  ──全量量化(LEVEL0 per-512 fp32 + LEVEL1 per-32 e8m0)──▶  workspace (fp4 + scale)
                                                              │
                                              AscendC::SyncAll<false>() 通知 AIC
                                                              ▼
AIC: 从 workspace 读 fp4 + e8m0 scale ──MmadMx──▶ bf16 C
```

**grouped MX**（样例 65/71）则在 MX 基础上叠加分组调度与后处理融合：65 融合 SwiGLU + 在线 MX 量化；71 融合 FinalizeRouting（Scatter Add 聚合 + 共享专家加权），后处理在 AIV 侧完成。

#### 4.2.3 源码精读

**(1) `MmadMx` 策略标签。** 它在 `MmadBase` 之上多了 `L1_SCALE_FACTOR_K`——「GM→L1 一次驻留的 L1 K 条带个数」，默认 16，即 16 个 L1 K 条带共用一次 scale 搬运：

> [dispatch_policy.hpp:434-444](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/include/catlass/gemm/dispatch_policy.hpp#L434-L444) —— `MmadMx` 定义；行 442-443 注释解释 `L1_SCALE_FACTOR_K`。`MmadA8W4Mx`（行 450）与之同构，仅默认 `L1B_STAGES=1`。

**(2) MX kernel 的 AIC SPMD 主循环。** 与 [u2-l4](u2-l4-kernel-basic-matmul.md) 的 BasicMatmul 结构一致：`GetBlockIdx` 起步、`BlockScheduler` 算 `coreLoops`、循环内取 tile 再调 `blockMmad`：

> [mx_matmul_tla.hpp:176-191](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/include/catlass/gemm/kernel/mx_matmul_tla.hpp#L176-L191) —— AIC `operator()` 入口：构造 `BlockScheduler`，行 180-188 是 FP8 专属的尾波负载均衡判定。

> [mx_matmul_tla.hpp:225-263](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/include/catlass/gemm/kernel/mx_matmul_tla.hpp#L225-L263) —— SPMD 主循环：`UpdateMNTileIdx`/`UpdateBlockShape` 算当前块坐标与尺寸，`GetTile` 取 A/B/**两份 scale**/C 的 block 视图，最后调 `blockMmad`（行 255-256 无 bias 版、行 259-261 带 bias 版）。注意 scale 的 K 维用 `CeilDiv<MX_SCALE_GROUP_NUM>(blockShape.k())`，即每 32 元素一个 scale。

**(3) 二级量化样例 63 的组件选型。** 本次新迁入的 63 把 LEVEL0/LEVEL1 两级 block size固化为常量，走单 kernel 路径：

> [63 README:54-68](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/examples/63_ascend950_dual_level_quant_mx_batch_matmul/README.md#L54-L68) —— 组件表：`Kernel=DualLevelQuantMxBatchedMatmulTla`、`DispatchPolicy=MmadMx<Ascend950, true, 16>`、`ElementA/B=float4_e2m1x2_t`（FP4）、`ElementMxScale=float8_e8m0_t`、`ElementC=bfloat16_t`；两级 block size `LEVEL0_BLOCK_SIZE=512`、`LEVEL1_BLOCK_SIZE=32` 见行 50。

> [63 README:3](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/examples/63_ascend950_dual_level_quant_mx_batch_matmul/README.md#L3) —— 单 kernel 路径说明：AIV 全量量化到 workspace，`AscendC::SyncAll<false>()` 通知 AIC 做 MX FP4 matmul；行 50 给出两级 block size，行 72 提示多流须开 batchmode 否则 `SyncAll` 可能死锁。

**(4) grouped MX 样例 71 的组装。** 这是本讲最复杂的一个组装链，把 MX tile copy、MX block mmad、FinalizeRouting epilogue 与分组调度串起来：

> [71 cpp:327-350](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/examples/71_ascend950_fp8_mx_grouped_matmul_finalize_routing/fp8_mx_grouped_matmul_finalize_routing.cpp#L327-L350) —— 组装链：`DispatchPolicy=MmadMx<Ascend950, true>`（行 329）→ `TileCopy=PackedMxTileCopyTla`（行 338，打包式 MX tile 搬运）→ `BlockMmad=BlockMmadMxFinalizeRoutingTla`（行 341）→ `BlockEpilogue=BlockEpilogueFinalizeRouting`（行 345，AIV 侧 FinalizeRouting）→ `BlockScheduler=ColumnBlockSwizzle`（行 347，确定性调度）→ `Kernel=GroupedMxMatmulFinalizeRoutingTla`（行 348）。注意行 333-334 用 `MakeMxScaleLayout` 分别给 A（不转置）/B（转置）构造 scale 布局。

> [71 README:66-71](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/examples/71_ascend950_fp8_mx_grouped_matmul_finalize_routing/README.md#L66-L71) —— 计算语义：`C = (MxScaleA·A) @ (MxScaleB·B) + Bias`，再 `out[rowIndex[p], :] += logit[p] * C[p, :]`（Scatter Add），可选共享专家加权。AIC 算完写 GM workspace，AIV 从 workspace 读出做后处理。

#### 4.2.4 代码实践

**实践目标**：对比「950 原生 MX FP8（样例 53）」与「A2 版 FP8（样例 29）」，理解 MX 原生指令带来的路径差异。

**操作步骤**：

1. 打开 [53 README](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/examples/53_ascend950_fp8_mx_matmul/README.md) 与 `examples/29_a2_fp8_e4m3_matmul/`（A2 版 FP8）。
2. 在 53 的 `fp8_mx_matmul.cpp` 里找到 `MmadMx`、`PackedMxTileCopyTla`、`MakeMxScaleLayout` 的组装点，记录 scale 的数据类型与布局构造方式。
3. 对照 29（A2）：A2 没有 `MmadMx` 原生指令，FP8 走的是 [u9-l2](u9-l2-quant-matmul.md) 讲过的「AIV prologue cast fp8→fp16 再做 fp16 matmul」路径。
4. 列一张对比表：路径（原生 MX vs cast）、是否需要 AIV 预处理、scale 形态（per-32 e8m0 vs 无）、kernel 类型。

**需要观察的现象**：53 的 AIC 主循环里 `blockMmad` 直接吃 `tensorMxScaleA/B`；而 29 需要在搬入前先把 fp8 转成 fp16。

**预期结果**：53 路径更短（少一次 AIV cast）、scale 显式参与计算；29 路径多了 cast 但不依赖 950 的 MX 硬件。两样例精度都应输出 `Compare success.`（待本地验证：需 950 环境 `bash scripts/build.sh 53_ascend950_fp8_mx_matmul -DCATLASS_ARCH=3510`）。

#### 4.2.5 小练习与答案

**练习 1**：样例 63 为什么要做「二级量化」？只用一级 per-32 e8m0 不行吗？

> **答案**：63 的输入是 **FP4**（`float4_e2m1x2_t`），FP4 的表示范围极窄（仅 2 位指数）。单级 per-32 e8m0 的动态范围不足以覆盖某些数据分布，会导致精度损失。再加一级 per-512 的 fp32 scale（LEVEL0）做粗粒度标定、per-32 e8m0（LEVEL1）做细粒度标定，层次化地扩展了有效动态范围，从而在 FP4 的窄表示下仍保住精度。

**练习 2**：`MmadMx` 的 `L1_SCALE_FACTOR_K=16` 调大或调小分别有什么影响？

> **答案**：`L1_SCALE_FACTOR_K` 是「GM→L1 一次驻留覆盖的 L1 K 条带数」。调大 → scale 搬运次数减少（省带宽），但 L1 需要驻留更多 scale 占用 L1 空间，可能与数据 tile 争抢 L1 容量；调小 → scale 搬运更频繁但 L1 占用更省。它受 L1 容量约束，需在带宽与容量间权衡。

---

### 4.3 EVG 后处理与调度策略

#### 4.3.1 概念说明

EVG（Epilogue Visitor Graph）是 CATLASS 的**声明式后处理框架**（见 [u6-l3](u6-l3-evg-framework.md)/[u6-l4](u6-l4-evg-execution-extension.md)）：用 Visitor 节点（AccLoad/AuxLoad/Compute/Store）以图的方式描述 `D = activation(A×B + bias)` 这类后处理，编译期生成 AIV 侧的执行代码。Ascend950 上 EVG 的标准载体是 **64 号样例集**——7 个可执行文件覆盖了最常见的融合模式。

本模块的另一半是**调度策略**。当算子复杂到 grouped（分组）+ 融合后处理时，**「怎么把基本块分给各核」**（调度）会直接影响多核利用率与结果确定性。样例 71 同时提供了两套调度，是理解「确定性 vs 非确定性」取舍的最佳教材：

- **确定性版**：`ColumnBlockSwizzle`——按列分块，固定的核→块映射，同一输入每次运行结果位级一致。
- **非确定性版**：`GemmGroupedAswtTailSplitSwizzle`——滚动核分配 + 窗口调度 + 尾块拆分，多核利用率更高，但因尾块拆分与归约顺序不固定，浮点累加顺序可能不同→结果非位级确定。

#### 4.3.2 核心流程

**EVG 后处理的标准组装**（样例 64）：

1. AIC 用 BlockMmad 算出 `A×B`，结果经 L0C 写到 **GM workspace**（默认）或 **L0C→UB workspace**（`add_ub` 变体）。
2. AIV 侧的 visitor kernel 按 EVG 图读 workspace、做激活/加法/广播、写回 GM。
3. 图的组织方式：线性链（`D = f(A×B)`）用 `TreeVisitor`；多节点有向图（如 `Tanh` 需要中间节点）用 `TopologicalVisitor`。

**grouped MX + FinalizeRouting 的两套调度**（样例 71）：

```
确定性版 (ColumnBlockSwizzle):
  核 i  ──固定映射──▶  第 i 列块；AIV 按 N 维切分后处理；无尾块拆分
  → 结果位级确定，但尾波核可能空转

非确定性版 (GemmGroupedAswtTailSplitSwizzle):
  startBlockIdx_ 跨 group 滚动 ──▶ 核任务窗口滑动；满足条件时 UpdateTailTile 拆尾块
  → 多核利用率高、尾块负载均衡，但浮点累加顺序非固定 → 非位级确定
```

取舍原则：**推理场景对确定性无要求时选非确定性版换吞吐；需要可复现/对齐 golden 时选确定性版**。

#### 4.3.3 源码精读

**(1) EVG 样例集 64 的 7 种融合。** 一张表说清场景、图组织与数据通路：

> [64 README:5-13](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/examples/64_ascend950_matmul_evg/README.md#L5-L13) —— 7 个可执行文件：`add/leaky_relu/sigmoid/silu`（TreeVisitor，GM workspace）、`tanh`（TopologicalVisitor，GM workspace）、`bias`（TreeVisitor + RowBroadcast）、`add_ub`（TreeVisitor，**L0C→UB workspace**）。其中 `add_ub` 走的是 [u6-l4](u6-l4-evg-execution-extension.md) 讲的 UB workspace 路径，省一次 GM 往返。

**(2) 样例 71 两套调度的组装对比。** 确定性版的组装见 [4.2.3 (4)](#423-源码精读)（行 347 `ColumnBlockSwizzle`、行 345 `BlockEpilogueFinalizeRouting`）。非确定性版把调度器与 epilogue 都换成 NoDeter 变体：

> [71 no_deter cpp:351-355](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/examples/71_ascend950_fp8_mx_grouped_matmul_finalize_routing/fp8_mx_grouped_matmul_finalize_routing_no_deter.cpp#L351-L355) —— `BlockEpilogue=BlockEpilogueFinalizeRoutingNoDeter`（AIV 按 M 维切分）、`BlockScheduler=GemmGroupedAswtTailSplitSwizzle<>`、`Kernel=GroupedMxMatmulFinalizeRoutingNoDeterTla`。

> [71 README:95-104](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/examples/71_ascend950_fp8_mx_grouped_matmul_finalize_routing/README.md#L95-L104) —— 确定性/非确定性对照表与说明：非确定性版 `startBlockIdx_` 跨 group 滚动提升多核利用率与尾块均衡；两版输入参数、数据格式、精度逻辑完全一致，仅调度与 AIV 切分方向不同。

**(3) 非确定性调度的核心：滚动核分配 + 尾块拆分。** `GemmGroupedAswtTailSplitSwizzle` 继承自 `BlockSchedulerAswt`，关键状态是 `startBlockIdx_`/`endBlockIdx_`（核任务窗口）与 `UpdateTailTile()`：

> [block_scheduler_aswt.hpp:99-107](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/include/catlass/gemm/block/block_scheduler_aswt.hpp#L99-L107) —— 滚动核分配：`startBlockIdx_` 在 `endBlockIdx_` 之后接续，窗口跨 group 滑动，`round_` 据此增减，让核不会在 group 边界空转。

> [block_scheduler_aswt.hpp:109-116](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/include/catlass/gemm/block/block_scheduler_aswt.hpp#L109-L116) —— `UpdateTailTile()`：当剩余 tile 还能再拆（`remainTile > 1`）时，把尾波 tile 进一步切分给多核，提升尾波负载均衡。这正是 [4.2.3 (2)](#423-源码精读) `mx_matmul_tla.hpp` 行 185-188 在 FP8 场景调用的同一个接口。

> [71 README:91](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/examples/71_ascend950_fp8_mx_grouped_matmul_finalize_routing/README.md#L91) —— 「AIC/AIV 通过 CrossCore Flag 实现 tile 粒度的流水线化交替执行，避免全局同步」——这是 71 用 tile 级 CrossCore 同步（而非 `SyncAll` 全局栅栏）做 AIC/AIV 交替的关键，与 63 的 `SyncAll` 全局同步形成对比。

#### 4.3.4 代码实践

**实践目标**：对照样例 71 的确定性/非确定性两版，说清两种调度在负载均衡与确定性上的取舍。

**操作步骤**（源码阅读 + 可选运行）：

1. 打开 [71 README 的对照表](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/examples/71_ascend950_fp8_mx_grouped_matmul_finalize_routing/README.md#L95-L101)，逐行理解「Kernel / BlockScheduler / BlockEpilogue / 尾块处理」四列差异。
2. 打开 [block_scheduler_aswt.hpp](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/include/catlass/gemm/block/block_scheduler_aswt.hpp)，找到 `startBlockIdx_`/`endBlockIdx_` 的滚动更新（行 99-100）与 `UpdateTailTile()`（行 110），解释它们如何提升多核利用率。
3. （可选，需 950 环境）按 README 分别编译两版：
   ```bash
   bash scripts/build.sh 71_ascend950_fp8_mx_grouped_matmul_finalize_routing -DCATLASS_ARCH=3510
   bash scripts/build.sh 71_ascend950_fp8_mx_grouped_matmul_finalize_routing_no_deter -DCATLASS_ARCH=3510
   ```
   用同一组参数运行，确认两者都输出 `Compare success.`（精度逻辑一致）。

**需要观察的现象**：两版的 `Arguments` 构造与精度计算完全相同；唯一差异在 `BlockScheduler` 与 `BlockEpilogue` 的类型别名上。`UpdateTailTile` 只在满足条件时才启用尾块拆分。

**预期结果**：非确定性版因滚动核分配 + 尾块拆分，在 group 数较多、尾波不均衡的场景下多核利用率更高；确定性版结果位级可复现。两者精度均通过对比（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：样例 64 的 `add_ub` 相比 `add` 为什么能省一次 GM 往返？

> **答案**：`add` 走 GM workspace 路径——AIC 把 `A×B` 从 L0C 经 Fixpipe 写到 GM workspace，AIV 再从 GM 读回做加法。`add_ub` 走 L0C→UB workspace 路径——结果从 L0C 直接搬到 UB（AIV 的本地存储），AIV 直接在 UB 上做加法，省掉了「L0C→GM→(AIV 读) GM」这一段 GM 带宽。代价是 UB 容量有限，只适合较小的 tile。

**练习 2**：如果业务要求「同一输入每次推理结果完全一致」，样例 71 该选哪个版本？为什么非确定性版做不到？

> **答案**：选**确定性版**（`ColumnBlockSwizzle`）。非确定性版的 `GemmGroupedAswtTailSplitSwizzle` 会做尾块拆分（`UpdateTailTile`）与滚动核分配，导致同一逻辑结果被不同核以不同顺序累加；浮点加法不满足结合律，累加顺序不同就会产生微小的位级差异，因此无法保证位级确定（精度仍在容差内）。

---

## 5. 综合实践

**任务**：以样例 71 为对象，画出一条从「Host 组装」到「AIC MX matmul + AIV FinalizeRouting」的完整数据流，并标注本讲三类能力各自的落点。

**要求**：

1. **追踪组装链**：从 [71 cpp:327-350](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/examples/71_ascend950_fp8_mx_grouped_matmul_finalize_routing/fp8_mx_grouped_matmul_finalize_routing.cpp#L327-L350) 出发，写出 `DispatchPolicy → TileCopy → BlockMmad → BlockEpilogue → BlockScheduler → Kernel → DeviceGemm` 七级 `using` 别名，并在每一级旁标注它体现了哪类 950 能力（Mutex / MX / EVG+调度）。
2. **画数据流图**：标出 GM（A/B/scale/bias/logit/rowIndex/out/workspace）、AIC（L1/L0/MmadMx）、AIV（UB/FinalizeRouting）三者的关系，重点画出「AIC 写 workspace → CrossCore Flag → AIV 读 workspace」这条 tile 级流水（参考 [71 README:91](https://github.com/gitcode.com/cann/catlass/blob/4fab1d0953b846f4876aa04cf07d1ecbd1110fad/examples/71_ascend950_fp8_mx_grouped_matmul_finalize_routing/README.md#L91)）。
3. **调度取舍分析**：假设你的业务是「离线 golden 对齐」（要求位级确定）vs「在线高吞吐推理」（不要求确定），分别说明该选 71 的哪个版本，并引用 `UpdateTailTile`/`startBlockIdx_` 的行为作为依据。
4. **（可选）扩展阅读**：对照样例 63 的 `SyncAll<false>()` 全局同步与样例 71 的 CrossCore Flag tile 级同步，写一段话说明为什么 71 能「避免全局同步」而 63 不能（提示：63 的 AIV 预量化必须先于 AIC matmul 全部完成，存在全局依赖；71 的 AIC/AIV 是 tile 粒度交替）。

**预期产出**：一张组装链表 + 一张数据流图 + 一段调度取舍说明。这道题把本讲的 Mutex（隐含在 BlockMmad 的同步基座里）、MX（`MmadMx` + scale）、EVG+调度（FinalizeRouting epilogue + 两套 swizzle）三类能力串了起来。

## 6. 本讲小结

- Ascend950 用**硬件 Mutex 互斥锁**替代传统 HardEvent：给每片缓冲（L1A/L1B/L0A/L0B/L0C/bias 的每个 stage）分配独立 MutexID，Lock/Unlock 配对实现按缓冲片的细粒度互斥，互不冲突的缓冲可并行；配套 `ENABLE_L1_RESIDENT` 支持跨块数据复用。
- `MmadPingpongMutex` 是 950 专属策略标签（`static_assert` 锁架构），实现是 `block_mmad_pingpong_mutex_tla.hpp` 的 TLA 版 `BlockMmadTla` 偏特化；MutexID 连续编号、上限 28。
- Ascend950 提供**原生 MX 指令**，对应 `MmadMx` 策略与 `MxMatmulTla` kernel：AIC 主循环结构与普通 matmul 同构，只是多搬两份 per-32 e8m0 scale 并把它们传入 `blockMmad`；FP8 场景还支持尾波 `UpdateTailTile` 负载均衡。
- MX 有三种深化形态：MXFP8（53）、二级量化 MXFP4（63，LEVEL0 per-512 fp32 + LEVEL1 per-32 e8m0，走 AIV 预量化→SyncAll→AIC matmul）、grouped MX（65/71，融合 SwiGLU/FinalizeRouting）。
- EVG 后处理在 950 上的标准载体是样例集 64（7 种融合，分 GM workspace 与 L0C→UB workspace 两条通路）；grouped 场景的调度策略决定多核利用率与确定性。
- 样例 71 提供**确定性（`ColumnBlockSwizzle`）**与**非确定性（`GemmGroupedAswtTailSplitSwizzle`，滚动核分配 + 尾块拆分）**两套调度：前者位级可复现，后者多核利用率更高；两版精度逻辑一致，仅调度与 AIV 切分方向不同。

## 7. 下一步学习建议

- **深入 Mutex 同步**：对比 `block_mmad_pingpong.hpp`（HardEvent）与 `block_mmad_pingpong_mutex_tla.hpp`（Mutex）两份主循环，体会同步粒度差异；进而阅读 `block_mmad_pingpong_mutex_tla.hpp` 中 `ENABLE_L1_RESIDENT` 在 grouped 场景（连续 blockMmad 调用）的复用收益。
- **MX 全家桶**：依次运行 53（MXFP8 基线）→ 58（MX batch）→ 63（二级量化 MXFP4）→ 65（grouped + SwiGLU + 在线 MX 量化）→ 71（grouped + FinalizeRouting），画出它们在「AIV 预处理 / AIC matmul / AIV 后处理」三段上的分工演进。
- **调度策略专题**：阅读 `block_scheduler_aswt.hpp` 与 `block_swizzle.hpp`，系统对比 `GemmIdentityBlockSwizzle`、`ColumnBlockSwizzle`、`GemmGroupedAswtTailSplitSwizzle` 三类调度器的分核映射，理解 [u4-l4](u4-l4-block-scheduler-swizzle.md) 的 Swizzle 在 950 上如何演化为 ASWT（滚动窗口）调度。
- **EVG 扩展**：若要自定义后处理，回到 [u6-l4](u6-l4-evg-execution-extension.md) 的 EVG 扩展机制，参考样例 64 的 `tanh`（TopologicalVisitor）尝试拼一个多节点 EVG 图。
