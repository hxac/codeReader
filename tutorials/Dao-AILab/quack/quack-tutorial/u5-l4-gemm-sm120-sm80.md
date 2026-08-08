# SM120（GeForce）与 SM80 GEMM

## 1. 本讲目标

前面三讲（u5-l1 ~ u5-l3）讲完了 GEMM 设备侧的「通用骨架 `GemmBase`」「Hopper SM90 的 WGMMA」和「Blackwell 数据中心 SM100 的 tcgen05 + TMEM」。本讲把镜头转向另外两类 GPU：

- **SM120（Blackwell 消费级，GeForce RTX 50 系列）**：QuACK 在它上面跑的内核是 `GemmSm120`，用的是 **warp 级 `mma.sync` + `ldmatrix`**，而不是 SM90 的 WGMMA，更不是 SM100 的 tcgen05。
- **SM80（Ampere，A100 / RTX 30 系列）**：QuACK 保留了 `GemmSm80` 的设计骨架（cp.async + warp MMA、无 TMA、无 cluster），但**目前内核主体尚未实现**。

学完本讲你应该能够：

1. 说清 SM120 为什么用 warp MMA 而非 tcgen05，以及它和 SM90 共享了哪些代码（`GemmSm120` 直接继承 `GemmSm90`）。
2. 读懂 `GemmSm120` 的线程配置：pingpong 两个 warp 组交替算 tile、coop 八个 warp 合算一个 tile、外加一个 DMA warp 用 TMA 装载。
3. 理解 SM120 共享内存更紧张这一约束是如何被 `epi_c_stage_base = 2` 吸收的，以及 CLC（cluster launch control）动态持久化在 GeForce 上为何可用。
4. 对比 SM120 与 SM100 在 blockscaled GEMM 上的默认配置差异。
5. 识别 `GemmSm80` 的骨架设计意图，并知道它的 `__call__` 当前是 `NotImplementedError`（不编造它已经能跑的行为）。

## 2. 前置知识

- **warp 级 MMA 指令**：一条 `mma.sync` 指令由**一个 warp（32 线程）**协作完成一个小矩阵乘（atom，例如 16×8×16）。这跟 SM90 的 **WGMMA**（一个 warp group，128 线程，直接从 SMEM 读 A/B）和 SM100 的 **tcgen05.mma**（把累加器写进专用 TMEM）是三套不同的指令体系。
- **`ldmatrix`（LDSM）**：warp 级 MMA 的操作数**必须先在寄存器里**。`ldmatrix` 是把 SMEM 的一小块数据按 MMA fragment 的布局装载进寄存器（RMEM）的专用指令。所以 warp MMA 的主循环比 WGMMA 多一步「SMEM→RMEM」的显式拷贝。
- **pingpong / coop**：这是 SM90 就有的两种 warp 分工策略。coop 让所有 math warp 合力算一个 tile；pingpong 把 math warp 分成两组，交替算相邻的两个 tile，从而把「一个 tile 的 MMA」和「另一个 tile 的 epilogue」重叠起来。
- **持久化内核与 CLC**：复习 u3-l4。持久化内核里 CTA 不只算一个 tile，而是循环领多个 tile。SM100/SM120 的 Blackwell 硬件提供 **CLC（Cluster Launch Control）**，用一个硬件原子计数器做工作偷取（`try_cancel`），比 SM90 只能用的 GMEM 原子计数器更省。
- **blockscaled（块缩放量化）**：复习 u7-l1/u4-l2。MXFP8 / NVFP4 / MXFP4 等格式给每 16 或 32 个元素配一个 e8m0（或 e4m3）缩放因子（scale factor, SF），用专门的 block-scaled MMA 指令一次吃掉「值 + SF」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `quack/gemm_sm120.py` | SM120 内核 `GemmSm120`（继承 `GemmSm90`）：warp MMA 主循环、`ldmatrix` 装载、pingpong/coop、CLC、blockscaled、以及大量实测数据注释。本讲主角。 |
| `quack/gemm_sm80.py` | Ampere 内核 `GemmSm80`（继承 `GemmBase`）：cp.async + warp MMA 的**设计骨架**，`__call__` 当前 `raise NotImplementedError`。 |
| `quack/sm80_utils.py` | warp MMA 的操作数分区辅助 `partition_fragment_ABC`；名字带「sm80」但当前主要被 **SM120** 复用。 |
| `quack/gemm_config.py` | `GemmConfig` 配置空间、`_get_sm120_configs`、`blockscaled_default_config`、`_default_config_for_cap`。 |
| `quack/gemm.py` | `_compile_gemm` 里按 `device_capacity` 选内核类的分发表。 |
| `quack/gemm_default_epi.py` | 用 mixin 拼出 `GemmDefaultSm120` / `GemmDefaultSm80`，给裸内核挂上默认线性 epilogue。 |

## 4. 核心概念与源码讲解

### 4.1 架谱定位：SM120 / SM80 与已学的 SM90 / SM100 有何不同

#### 4.1.1 概念说明

把四个架构的 GEMM 指令体系摆在一起对比，是理解本讲最快的方式：

| 架构 | 代表卡 | MMA 指令体系 | 操作数来源 | 累加器位置 | 线程块簇（cluster） |
|------|--------|-------------|-----------|-----------|---------------------|
| SM80 | A100 / RTX 30 | warp `mma.sync`（m16n8k16） | 寄存器（需 `ldmatrix`） | 寄存器 | 无 |
| SM90 | H100 | **WGMMA**（warp group，128 线程） | A/B 直接从 SMEM 读 | 寄存器 | 有（多播 A/B） |
| SM100 | B200 / B300 | **tcgen05.mma**（2-CTA） | SMEM | **TMEM（专用张量内存）** | 有 |
| SM120 | RTX 50 | warp `mma.sync`（m16n8k16 / m16n8k32） | 寄存器（需 `ldmatrix`） | 寄存器 | 形式上接受，实际单 CTA |

关键结论：

- **SM120 在指令层面「退化」回了 SM80 那一代的 warp MMA**——但用上了 Blackwell 新增的 **block-scaled MMA 变体（`kind::mxf8f6f4`）**，使 fp8 吞吐翻倍。GeForce 消费级芯片**没有 SM100 的 TMEM / tcgen05 数据通路**，所以只能走 warp MMA。
- **SM120 在工程上却继承自 `GemmSm90`**，大量复用 Hopper 的主机侧编排与 epilogue 流程——因为它的**数据装载仍用 TMA**（ unlike SM80 的 cp.async）。这是 SM120 最有趣的地方：它是「SM90 的 TMA 装载流水线 + SM80 风格的 warp MMA 计算」的混血。

#### 4.1.2 核心流程

```
SM120 单个 CTA 的内部结构（pingpong 为例）：
┌─────────────────────────────────────────────┐
│  Warp Group 0 (4 warps, MMA)  算 tile A      │
│  Warp Group 1 (4 warps, MMA)  算 tile B（交替）│
│  DMA Warp Group (128 线程, 其中 1 warp 真干活) │
│      └─ TMA 把 A/B/SF 灌进 SMEM 多级缓冲       │
└─────────────────────────────────────────────┘
WG0: ldmatrix 取 A/B 进寄存器 → mma.sync 累加 → epilogue 存回
WG1: 与 WG0 错相，做相邻 tile，二者用 pingpong barrier 握手
```

而 SM80 的设想结构（尚未实现）则是：**所有线程都参与** cp.async 装载、MMA 和 epilogue，没有专门的 DMA warp，也没有 TMA。

#### 4.1.3 源码精读

`GemmSm120` 的继承关系和架构标记：

[quack/gemm_sm120.py:213-246](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm120.py#L213-L246) —— `class GemmSm120(GemmSm90)`，`arch = 120`。注意它**继承 `GemmSm90`**，因此 `__call__`、`_setup_attributes`、`make_ab_pipeline`、`epilogue` 等都直接沿用 Hopper 的实现（文件末尾注释明说「inherited from GemmSm90」）。

[quack/gemm_sm80.py:20-29](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm80.py#L20-L29) —— `class GemmSm80(GemmBase)`，`arch = 80`，`_supported_archs = (80, 86, 87, 89)`。它继承的是**基类 `GemmBase`**（而不是 `GemmSm90`），因为 Ampere 没有 TMA、没有 cluster，逻辑上离 SM90 最远。

主机侧的分发表（决定哪张卡用哪个类）：

[quack/gemm.py:93-100](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L93-L100) —— `device_capacity`（即 SM 主版本号）映射到 `GemmDefaultSmXX`（带默认 epilogue 的组合类）。

#### 4.1.4 代码实践

**实践目标**：用三行表格固化「四架构指令体系」的对照。

**操作步骤**：
1. 打开 `quack/gemm_sm120.py` 顶部注释（L4-L7）和类 docstring（L213-L244），找到它对「warp-level MMA vs WGMMA」的明确陈述。
2. 回顾 u5-l2 讲义里 SM90 用 WGMMA、u5-l3 里 SM100 用 tcgen05 的结论。
3. 在本讲 4.1.1 的表格里，用自己的话补上每个架构「操作数来源」一列。

**预期结果**：你能不看讲义，回答「为什么 SM120 不用 tcgen05」——因为 GeForce 消费级芯片没有 TMEM/tcgen05 数据通路。

#### 4.1.5 小练习与答案

**练习 1**：`GemmSm120` 继承 `GemmSm90`，但 SM80 同样是 warp MMA，为什么 `GemmSm80` 不也继承 `GemmSm90`？

**参考答案**：因为 SM120 和 SM90 一样**用 TMA 装载**（`__call__`、AB pipeline、epilogue 流程都能直接复用），只是把计算指令从 WGMMA 换成 warp MMA；而 SM80 **没有 TMA**、**没有 cluster**，连装载机制都不同，复用 SM90 的 TMA 流水线没有意义，所以它继承更基础的 `GemmBase`。

**练习 2**：SM120 的 MMA 指令宽度（`mma_inst_mnk`）是多少？

**参考答案**：16 位操作数用 `(16, 8, 16)`，8 位用 `(16, 8, 32)`（见 [quack/gemm_sm120.py:364](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm120.py#L364)）。packed fp4 的 block-scaled 专用原子则为 `(16, 8, 64)`。

---

### 4.2 GemmSm120 主循环：warp MMA + ldmatrix（pingpong 与 coop）

这是本讲最核心的模块，对应「GemmSm120 warp MMA pingpong」。

#### 4.2.1 概念说明

warp 级 MMA 与 WGMMA 最大的工程差异是：**warp MMA 的操作数必须先在寄存器里**。于是主循环多了一个「produce」步骤——用 `ldmatrix` 把 SMEM 里的 A/B 块搬进 RMEM。这份搬运在 QuACK 里被抽象成一个 `copy_block(stage_idx, b, k_tile)` 接缝（seam）：

- 默认情况下，`copy_block` 就是 `canonical_a_load`——一次 `ldmatrix` 的 SMEM→RMEM 拷贝。
- 当 A 操作数带变换（如 W4 反量化、dropout）时，`copy_block` 被换成变换自己的 produce（解码、解包等），但**接缝形状不变**。这就是为什么 A 侧变换能无缝插进 SM120 主循环。

线程配置分两种：

- **coop（非 pingpong）**：1 组共 8 个 math warp，atom 布局 `(4, 2, 1)`（4 warp 沿 M，2 warp 沿 N），合力算一个 tile。
- **pingpong**：2 个 warp 组，每组 atom 布局 `(2, 2, 1)`（4 warp），交替算相邻的两个 tile，用 pingpong barrier 把 WG0 的 MMA 与 WG1 的 epilogue 重叠。
- 两种模式都**额外配一个 DMA warp 组**（128 线程的粒度，便于 `setmaxnreg.dec.sync`），其中只有 1 个 warp 真正发 TMA（`num_ab_load_warps = 1`）。

#### 4.2.2 核心流程

`mma` 主循环的 produce 节奏（与 CUTLASS SM120 collective、`GemmSm90.mma_rs_interleaved` 一致）：

```
装载第 0 个 k-block（slot 0）
for k_tile in [0, k_tile_cnt-1):          # 动态循环
    for k in range_constexpr(num_k_blocks): # 编译期展开
        if k 是该 tile 最后一个 block:
            fence + sync_warp + release 当前 stage
            advance 到下一个 stage，wait 它 full
        预取下一个 k-block（下一个 slot）
        ldmatrix A、ldmatrix B 进寄存器
        cute.gemm(tiled_mma, acc, tCrA, tCrB, acc)   # mma.sync
# 最后一个 k-tile 单独 hoist 出来（含边界/ragged-K 处理）
```

关键点：warp `mma.sync` 是**同步**指令，**不需要** WGMMA 那套 commit-group/wait 纪律，所以 produce 接缝的契约只剩下「调度」本身——先 produce block k+1，再算 block k。

#### 4.2.3 源码精读

线程配置的选择——pingpong / W4 / 普通三条路径：

[quack/gemm_sm120.py:356-393](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm120.py#L356-L393) —— 注意 `atom_layout_mnk` 三种取值；`num_mma_warps = prod(atom) * (1 or 2)`；`threads_per_cta = (mma_warp_groups + 1) * 128`（「+1」是 DMA warp 组，为了寄存器配置同步必须按 128 对齐）。

DMA 装载 warp：用 TMA 把 A/B/SF 灌进 SMEM，并跑持久化调度循环：

[quack/gemm_sm120.py:924-957](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm120.py#L924-L957) —— `setmaxregister_decrease`（装载 warp 用更少寄存器）、PDL 等待、构造 TMA 拷贝函数、初始化 tile scheduler。注意 SM120 这里完全复用 SM90 的 TMA 路径，`load_tma` 即来自父类。

MMA warp 的寄存器配置与 fragment 分区：

[quack/gemm_sm120.py:1109-1151](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm120.py#L1109-L1151) —— `setmaxregister_increase`（MMA warp 要更多寄存器），用 `sm80_utils.partition_fragment_ABC` 切出累加器 `acc` 和 A/B 的 SMEM/RMEM 视图。

warp MMA 主循环本体：

[quack/gemm_sm120.py:1644-1741](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm120.py#L1644-L1741) —— `mma` 方法。L1695 `copy_block(stage, 0, kt)` 装载第一个 k-block，L1696 `load_sB` 用 `ldmatrix` 取 B；L1704 起的双层循环里，L1721 预取下一 block、L1740 `cute.gemm(...)` 发 `mma.sync`。

A 的 produce 接缝（默认就是 `ldmatrix`）：

[quack/gemm_sm120.py:713-753](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm120.py#L713-L753) —— `canonical_a_load`：按 dtype/主序选不同的 LDSM 原子（16 位、k-major 8 位、M-major 8 位的转置变体、亚字节 padded 变体）。

#### 4.2.4 代码实践

**实践目标**：在源码里把「pingpong 两个 WG 如何交替」的握手点找出来。

**操作步骤**：
1. 读 [quack/gemm_sm120.py:1270-1301](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm120.py#L1270-L1301)：WG0 启动前先 arrive 两个 `mma`/`epi` 信号。
2. 读 [quack/gemm_sm120.py:1339-1393](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm120.py#L1339-L1393)：主循环里 `pingpong_barrier_sync(warp_group_idx, "mma")` 等对方、算完自己的 MMA 后 `pingpong_barrier_arrive(1 - warp_group_idx, "mma")` 唤醒对方。
3. 跟踪 WG1（`warp_idx >= 4`）如何把自己的 pipeline 状态「快进」过一个 tile（L1292-L1301），从而和 WG0 错开一格。

**预期结果**：你能用一句话说清——pingpong 靠一组命名 barrier，让两个 WG 各算相隔一个的 tile，从而把 WG0 的 epilogue 与 WG1 的 MMA 时间重叠。

#### 4.2.5 小练习与答案

**练习 1**：为什么 warp MMA 主循环里，在「该 tile 最后一个 k-block」处要 `fence_view_async_shared()` + `sync_warp()` 才能 release 当前 stage？

**参考答案**：TMA 通过 **async proxy** 写 SMEM，而 `ldmatrix` 通过 **generic proxy** 读 SMEM。在释放空屏障（让 producer 写下一 stage）之前必须加 fence，确保 producer 接下来的 async-proxy 写不会和当前 warp 正在进行的 ldmatrix 读竞争；`sync_warp` 是因为只有一个 lane 去信号 empty 屏障。见 [quack/gemm_sm120.py:1707-1714](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm120.py#L1707-L1714)。

**练习 2**：coop 模式下 `atom_layout_mnk = (4, 2, 1)`，pingpong 模式下 `(2, 2, 1)`，请算出各自的 math warp 总数。

**参考答案**：coop：`prod((4,2,1)) * 1 = 8` 个 warp（1 组）；pingpong：`prod((2,2,1)) * 2 = 8` 个 warp（2 组，每组 4）。两种模式 math warp 总数都是 8，区别在分组方式。

---

### 4.3 SMEM 约束与 CLC 动态调度

本模块对应「CLC 调度与 SMEM 约束」。

#### 4.3.1 概念说明

**SMEM 约束**。RTX 50（SM120）这类 GeForce 消费级芯片，每个 CTA 可用的共享内存比数据中心卡（SM100）更紧张。AB pipeline 的多级缓冲、epilogue 的 C/D 暂存都挤在同一块 SMEM 里。CUTLASS 的 `sm120_builder` 因此定了一条策略：

\[ \text{StagesC} = \text{StagesD} = \min(\text{EpiTiles},\, 2) \]

即 epilogue 的 C 缓冲级数最多 2——「smaller stage counts in order to fit within the limited shared memory capacity」。QuACK 用 `epi_c_stage_base = 2` 镜像了这条策略。SMEM 总量在运行时由 `get_smem_capacity_in_bytes("sm_120")` 查询（不写死，留待本地验证具体 KiB 数）。

**CLC 动态持久化**。复习 u3-l4：持久化内核需要把 tile 分配给 CTA。SM90 没有 CLC 硬件，动态分配只能靠 GMEM 原子计数器，开销大，所以 SM90 默认关动态持久化。而 Blackwell（含 GeForce 的 `sm_120a/121a`）有 CLC 硬件——`CUTLASS_ARCH_CLC_ENABLED` 覆盖了 GeForce 部分——用硬件 `try_cancel` 做工作偷取，开销小得多。所以 SM120 的配置默认 `is_dynamic_persistent=True`，并允许 `use_clc_persistence=True`。

#### 4.3.2 核心流程

SM120 的持久化调度有两种 tile 消费节奏：

```
① pingpong + 静态调度（pingpong_sched_skip=True）：
   每个 WG 只消费自己的隔行 slot（advance_count=2），
   producer 在循环末尾手写一条 invalid 记录给尾部 WG。

② CLC / varlen_k / split_k（one-at-a-time）：
   两个 WG 都读每一个 CLC 响应（slot 里是硬件响应，
   不能手写、不能隔行跳），各取一个 tile 再前进。
```

为什么 CLC 下不能用隔行 skip？因为 CLC 的 slot 里装的是**硬件响应记录**，手写一条假记录会被误解码；而且尾部 WG 在 tail slot 没有对应的 producer。所以 CLC pingpong 退化为「一次消费一个」。

#### 4.3.3 源码精读

SMEM 约束的设计响应：

[quack/gemm_sm120.py:246-263](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm120.py#L246-L263) —— `arch = 120`，`epi_c_stage_base = 2`。注释详细解释了 CUTLASS 的 StagesC 策略，并给出 RTX 5090 上的实测数据（例如 bf16 128×128pp + f32 C 从 201→239 TF、fp8 128×128pp 从 525→637 TF）。

SMEM 容量与占用率：

[quack/gemm_sm120.py:402-403](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm120.py#L402-L403) —— `self.occupancy = 1`、`self.smem_capacity = get_smem_capacity_in_bytes(f"sm_{self.arch}")`。占用率固定为 1（一个 SM 只驻留一个 CTA），因为单 CTA 的 SMEM/寄存器开销已经很大。

CLC 持久化的开关与约束：

[quack/gemm_sm120.py:280-298](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm120.py#L280-L298) —— `use_clc_persistence` 默认 `False`，开它要求 `is_persistent=True`。注释指出 CLC 在 `sm_120a/121a` 上与 SM100 一样可用，且「调度 warp 在这里兼任 load warp，所以不需要节流屏障」。

内核里 CLC 尾部退役门控：

[quack/gemm_sm120.py:841-870](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm120.py#L841-L870) —— `pingpong_sched_skip` 的判定（CLC 下为 False），以及 CLC 需要在调度 scratch 末尾多留 6 个 Int32（退役 drain 的私有响应槽 + mbarrier）。L1101-L1104 的 `cancel_pending_tail()` 负责「幻影退役」——把 CLC 多发的 padding tail 取消掉。

#### 4.3.4 代码实践

**实践目标**：看清 CLC pingpong 与静态 pingpong 在 tile 消费节奏上的分叉点。

**操作步骤**：
1. 读 [quack/gemm_sm120.py:841-843](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm120.py#L841-L843)：`pingpong_sched_skip` 仅在「静态调度、无 varlen_k、split_k==1、无 CLC」时为 True。
2. 读 [quack/gemm_sm120.py:1513-1548](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm120.py#L1513-L1548)：主循环末尾，skip 模式用 `advance_count=self.mma_warp_groups`（隔行跳），否则「读并丢弃对端 slot，再前进」（一次一个）。

**预期结果**：待本地验证——在 RTX 50 上分别用 `is_dynamic_persistent=True/False` 编译，用 `cute.printf` 打印每个 WG 领到的 `tile_idx`，应能看到 skip 模式下两个 WG 取相隔一格的 tile，CLC 模式下取相邻 tile。

#### 4.3.5 小练习与答案

**练习 1**：SM120 把 `epi_c_stage_base` 设成 2，省下的 SMEM 用来换什么？

**参考答案**：换 AB pipeline 的级数（stage 数）。如果像 SM90 那样给 epilogue 预留 4 个 C-stage，C/D 的 SMEM 占用会吃掉整整一个 AB stage；把 C-stage 压到 2，AB 流水线就能多保留一级，整体吞吐更高（注释里有实测对照）。

**练习 2**：为什么 SM120 默认开动态持久化（`is_dynamic_persistent=True`），而 SM90 默认关？

**参考答案**：SM120（Blackwell GeForce）有 CLC 硬件，动态工作偷取代价低；SM90（Hopper）没有 CLC，动态只能用 GMEM 原子计数器，开销高于静态光栅化带来的负载均衡收益，所以默认关（仅 varlen 例外）。

---

### 4.4 blockscaled：warp MMA（`kind::mxf8f6f4`）而非 tcgen05 + 默认配置差异

本模块直接服务于本讲的代码实践任务。

#### 4.4.1 概念说明

**为什么 SM120 用 warp MMA 而非 tcgen05**。根本原因是**芯片面积**：GeForce 消费级 SM120 砍掉了数据中心 SM100 的 TMEM（张量内存）和 tcgen05.mma 数据通路。但 Blackwell 给 warp MMA 家族新增了一条 **block-scaled 变体** `mma.sync.kind::mxf8f6f4`——它一次吃掉「值 + ue8m0 缩放因子」，吞吐是 Ada 时代普通 fp8 `mma.sync`（`MmaFP8Op`）的 **2 倍**。

QuACK 的巧思在于：即使是**普通（非量化的）fp8** GEMM，也让它 ride 这条 block-scaled 指令，只是把缩放因子设成**常数 1.0（ue8m0 的 0x7F）**——所谓「unit scale fast path」。这样普通 fp8 也能享受 2× 吞吐，代价只是每个 (m-atom, k-atom) 多一个常数 SF 字节片段、无需额外装载。

实测（RTX 5090，注释里写明）：block-scaled 指令 1005 TFLOPS，Ada 指令 507 TFLOPS，且累加器精度特征一致（~21-22 位截断 f32）。

**SM120 vs SM100 的 blockscaled 默认配置差异**：

| 维度 | SM100（数据中心） | SM120（GeForce） |
|------|------------------|------------------|
| MMA 指令 | tcgen05.mma（2-CTA） | warp `kind::mxf8f6f4` |
| 默认 tile（blockscaled） | 大形状 (256,256) cluster (2,1) | 固定 **(128,128) pingpong** |
| pingpong | 无 | 有 |
| cluster | (2,1) 多播 | (1,1) 单 CTA |
| tile_M/N 允许值 | tile_n 须 64 整除，∈[64,256] | 严格 ∈ {128, 256} |
| 动态持久化 | True（CLC） | True（CLC） |

SM120 之所以固定 (128,128) pingpong、单 CTA：warp MMA 的 SF smem 布局与 fragment 分区辅助只支持**整个 128 tile** 的粒度，且没有 cluster 多播可用。

#### 4.4.2 核心流程

`_setup_tiled_mma` 的 MMA 选路（简化）：

```
if blockscaled（真实 SF 操作数）:
    要求 sm_120a/f 编译目标、f32 累加、e8m0 SF、sf_vec_size∈{16,32}
    同 dtype fp4  → kind::mxf4 / mxf4nvf4（inst K 64）
    其他合法组合  → kind::mxf8f6f4（独立 a/b dtype 限定符）
elif 普通路径（unit scale）:
    若 dtype∈MXF8F6F4 且 tile_M%128==0 且 tile_K%128==0 且 sm_120a/f:
        ride kind::mxf8f6f4 + 常数 0x7F SF（2× 吞吐）
    else:
        退化到 Ada MmaFP8Op（仅同 dtype fp8，H100 CI 代理路径）
```

#### 4.4.3 源码精读

`_setup_tiled_mma` 的选路逻辑与详尽注释：

[quack/gemm_sm120.py:519-545](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm120.py#L519-L545) —— docstring 解释了「2× 吞吐是 `.block_scale` 变体独占」「unit-SF 不需装载」「H100 CI 代理腿走 MmaFP8Op 回退」。

具体的 op 构造（按编译期分支）：

[quack/gemm_sm120.py:663-682](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm120.py#L663-L682) —— 16 位走 `MmaF16BF16Op`；同 dtype fp4 走 `MmaMXF4NVF4Op`/`MmaMXF4Op`；混合/同 dtype fp6 走 `MmaMXF8F6F4OpFull`；同 dtype fp8 走 `MmaMXF8Op`；混合 fp8 回退走 `MmaFP8MixedOp`；否则 `MmaFP8Op`。

unit-scale 常数 SF 片段的填充：

[quack/gemm_sm120.py:1203-1251](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm120.py#L1203-L1251) —— 用任意指针造 dummy SF 张量，分区出片段后 `cute.recast_tensor(...).fill(127)`（0x7F = ue8m0 的 1.0），永不重载。

SM120 的 blockscaled 合法性约束（配置层）：

[quack/gemm_config.py:75-83](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_config.py#L75-L83) —— `blockscaled_config_ok` 的 SM120 分支：`tile_m/tile_n ∈ {128,256}`、`tile_k is None`、`not swap_ab`。

blockscaled 默认配置（SM120 vs SM100 的分流）：

[quack/gemm_config.py:288-314](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_config.py#L288-L314) —— `device_capacity == 12` 直接返回 `(128,128) pingpong, cluster (1,1)`；SM100 则按 m/n 尺寸在 (256,256)/(256,128)/(128,128) 间选，cluster (2,1)，无 pingpong。注释记录了 RTX 5090 上的实测取舍。

#### 4.4.4 代码实践（本讲主任务）

**实践目标**：比较 SM120 与 SM100 在 blockscaled GEMM 上的默认配置差异，并解释 SM120 为何用 warp MMA 而非 tcgen05。

**操作步骤**：
1. 读 [quack/gemm_config.py:306-314](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_config.py#L306-L314)：确认 SM120 分支返回 `(128,128) pingpong`。
2. 读同函数 SM100 分支（L308-L313）：大形状 (256,256) cluster (2,1)。
3. 读 [quack/gemm_config.py:71-95](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_config.py#L71-L95）：对比两架构的 `blockscaled_config_ok` 约束差异（SM120 tile 严格 128/256；SM100 受 SF tmem 64-N 颗粒约束）。
4. 回顾 u5-l3 讲义：SM100 的 tcgen05 把累加器写进 TMEM。

**需要观察的现象 / 预期结论**：
- SM120 没有 cluster（`(1,1)`），因为 GeForce 单 CTA；SM100 用 `(2,1)` 多播。
- SM120 用 pingpong 重叠 MMA/epilogue；SM100 不需要 pingpong——tcgen05 把累加器放进 TMEM，MMA 与 epilogue 由硬件原生并行（见 u5-l3）。
- SM120 用 warp `kind::mxf8f6f4` 而非 tcgen05，因为 **GeForce SM120 没有 TMEM/tcgen05 数据通路**；但 Blackwell 仍给了 warp MMA 一条 block-scaled 2× 变体，所以量化吞吐不输。

> 待本地验证：若手头有 RTX 50（SM120），可跑 `tests/test_gemm_functional.py` 中 blockscaled 用例，确认默认走 `(128,128) pingpong`。

#### 4.4.5 小练习与答案

**练习 1**：普通（非量化）fp8 GEMM 在 SM120 上为什么也能跑出 ~2× Ada 的吞吐？

**参考答案**：因为它 ride 了 block-scaled 指令 `kind::mxf8f6f4`，把缩放因子设成常数 1.0（ue8m0 0x7F），享受 `.block_scale` 变体的 2× 数据通路，而无需真实 SF 装载（见 [quack/gemm_sm120.py:1203-1251](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm120.py#L1203-L1251)）。

**练习 2**：`blockscaled_config_ok` 里，SM100 要求 `tile_n % 64 == 0` 而 SM120 要求 `tile_n ∈ {128,256}`，为什么 SM120 更严？

**参考答案**：SM100 的约束源于 SF tmem 数据通路 64-N 颗粒；SM120 的 SF smem 布局与 fragment 分区辅助（`partition_fragment_SFA/SFB`）只能处理**整个 128 宽**的 tile（与 CUTLASS SM120 示例同），所以 tile_M/N 只能取 128 或 256。

---

### 4.5 GemmSm80：cp.async + warp MMA 的设计骨架（当前未实现）

本模块对应「SM80 基础 cp.async 路径」，并**如实说明其实现状态**。

#### 4.5.1 概念说明

`GemmSm80` 是 QuACK 为 Ampere（SM80/86/87/89，A100 / RTX 30）准备的 GEMM 类。它的**设计意图**很清晰：

- **无 TMA**：A100 没有 TMA 引擎，全局→共享内存的数据搬运用 **cp.async**（每线程级的异步拷贝），由 `commit/wait_group` 收尾（复习 u3-l1）。
- **无 cluster**：Ampere 没有线程块簇，`cluster_shape_mnk` 强制为 `(1,1,1)`，每个 CTA 独立。
- **warp MMA**：用 `MmaF16BF16Op`（m16n8k16），与 SM120 同属 warp MMA 体系，但**没有 Blackwell 的 block-scaled 变体**。
- **全员参与**：所有 CTA 线程都参与 cp.async 装载、MMA、epilogue（没有 SM120 那种专门 DMA warp）。

**重要事实**：当前 HEAD 下，`GemmSm80` 只实现了配置层（`__init__`、`_setup_tiled_mma`、`_smem_capacity_for_arch`），**内核主体 `__call__` 是 `raise NotImplementedError("Gemm Sm80 is not implemented yet")`**。也就是说，QuACK 目前并不在 SM80 上实际跑 GEMM；这个类是留给未来补全或作为教学对照的骨架。本节 therefore 以「源码阅读型实践」为主。

#### 4.5.2 核心流程（设计意图，非已实现）

按 `__init__` 的配置逻辑，设想的 SM80 GEMM 应是：

```
1. 配置：num_warps∈{4,8}，atom_layout=(2, num_warps//2, 1)，无 cluster
2. SMEM 容量：_smem_capacity_for_arch(arch) 查询；按 AB 占用决定 occupancy(1 或 2)
3. mainloop（设想）：
   cp.async 把 A/B tile 灌进多级 SMEM 缓冲
   wait_group 等数据到位
   ldmatrix 取进寄存器
   mma.sync 累加
4. epilogue：复用 GemmBase 的可组合 epilogue 钩子（per-thread store 回 GMEM）
```

#### 4.5.3 源码精读

类定义与架构约束：

[quack/gemm_sm80.py:20-29](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm80.py#L20-L29) —— docstring 直言「SM80 has no TMA」「epilogue 复用 SM90/SM120 的标准钩子」；`_supported_archs = (80, 86, 87, 89)`。

配置：无 pingpong、无 cluster、warp MMA：

[quack/gemm_sm80.py:52-88](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm80.py#L52-L88) —— `assert not pingpong`、`assert cluster_shape_mnk == (1,1,1)`；`mma_inst_mnk = (16,8,16)`；`num_warps` 默认按 tile 尺寸选 4 或 8；`atom_layout_mnk = (2, num_warps//2, 1)`。

SMEM 容量与 occupancy：

[quack/gemm_sm80.py:90-99](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm80.py#L90-L99) —— 按 `3 * ab_bytes_per_stage` 是否超 SMEM 预算来把默认 occupancy 从 2 降到 1。注意 SM80 不像 SM120 那样把 occupancy 写死成 1。

MMA 与 tile K 推导：

[quack/gemm_sm80.py:125-135](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm80.py#L125-L135) —— `_setup_tiled_mma` 用 `warp.MmaF16BF16Op` 造 tiled MMA；tile_k 默认 `4 * mma_inst_k = 4*16 = 64`。

**未实现的内核主体**：

[quack/gemm_sm80.py:137-153](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm80.py#L137-L153) —— `@cute.jit def __call__(...)` 直接 `raise NotImplementedError("Gemm Sm80 is not implemented yet")`。这是当前的真实状态。

被 SM120 复用的 warp MMA 分区辅助：

[quack/sm80_utils.py:6-27](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/sm80_utils.py#L6-L27) —— `partition_fragment_ABC`：从 `thr_mma` 切出累加器 `acc` 与 A/B 的 SMEM/RMEM 视图，支持 `swap_AB`。注意它叫 `sm80_utils`，但**当前主要使用者是 SM120**（见 [quack/gemm_sm120.py:1149](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm120.py#L1149)），SM80 自己还没用到。

#### 4.5.4 代码实践（源码阅读型）

**实践目标**：确认 `GemmSm80` 当前的实现边界，避免误以为它已经能跑。

**操作步骤**：
1. 读 [quack/gemm_sm80.py:137-153](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm80.py#L137-L153)，确认 `__call__` 抛 `NotImplementedError`。
2. 用 `git log --oneline -- quack/gemm_sm80.py` 查看该文件最近的提交，判断 SM80 是活跃开发还是长期搁置（命令本身只读，安全）。
3. 读 [quack/gemm.py:93-100](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L93-L100)：`device_capacity==8` 仍会分派到 `GemmDefaultSm80`——若真在 A100 上调用 `quack.gemm`，会在运行期撞上这个 `NotImplementedError`。

**预期结果 / 待本地验证**：你得出结论——SM80 路径目前是骨架，配置层已就绪、内核主体待补；不要在文档或测试中声称 QuACK 已支持 Ampere GEMM。

#### 4.5.5 小练习与答案

**练习 1**：`GemmSm80` 与 `GemmSm120` 同为 warp MMA，为什么 SM120 能跑 blockscaled 而 SM80（设计上）不能？

**参考答案**：blockscaled 走的是 Blackwell 新增的 `kind::mxf8f6f4` 等 block-scaled 指令，仅 `sm_120a/f` 及更新支持；Ampere（SM80）的 warp MMA 只有普通的 `mma.sync`，没有 block-scaled 变体，也没有 SF 数据通路。

**练习 2**：SM80 的 occupancy 选择（[quack/gemm_sm80.py:90-99](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm80.py#L90-L99)）和 SM120（写死 1）有何不同？

**参考答案**：SM80 会估算 `3 * ab_bytes_per_stage` 是否超出 `smem_capacity / default_occupancy`，若超就把 occupancy 从默认（8 warp 时 1，4 warp 时 2）降级；SM120 则直接 `occupancy = 1`，因为单 CTA 的 SMEM/寄存器开销已经占满一个 SM。

---

## 5. 综合实践

**任务**：为 SM120 画一张「从用户调用到 `mma.sync` 指令」的完整数据通路图，并标注每一段复用的是哪一代架构的机制。

**操作步骤**：
1. 从 `quack/gemm.py` 的 `_compile_gemm`（[L93-L100](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L93-L100)）出发，确认 `device_capacity==12 → GemmDefaultSm120`。
2. 进入 `GemmSm120`，标注：
   - **装载段**（TMA → SMEM 多级缓冲、持久化调度、CLC）——复用自 **SM90**（继承 `GemmSm90`）。
   - **计算段**（`ldmatrix` SMEM→RMEM、warp `mma.sync`、pingpong barrier）——**SM80 风格的 warp MMA**，但用 Blackwell 的 block-scaled 变体。
   - **epilogue 段**（r2s → SMEM → TMA store）——复用自 **SM90/GemmBase**。
3. 在图上用颜色/标注区分「SM90 血统」「SM80 血统」「Blackwell 专属（CLC、kind::mxf8f6f4）」。
4. 写一段话总结：SM120 为何被称作「SM90 的 TMA 流水线 + SM80 风格 warp MMA 的混血」。

**预期结果**：你得到一张清晰的三段通路图，能指出 SM120 真正「自己写」的只有计算段（`mma` 方法、`canonical_a_load`、`_setup_tiled_mma`、pingpong/coop 配置），其余大量继承自 SM90。

## 6. 本讲小结

- **SM120 是混血**：继承 `GemmSm90`，复用 Hopper 的 TMA 装载流水线与 epilogue 流程，但把计算从 WGMMA 换成 warp 级 `mma.sync` + `ldmatrix`。
- **不用 tcgen05 的根因**：GeForce 消费级 SM120 没有数据中心 SM100 的 TMEM/tcgen05 数据通路；但 Blackwell 给了 warp MMA 一条 block-scaled 2× 变体 `kind::mxf8f6f4`，普通 fp8 用常数 unit scale 也能享受。
- **线程配置**：pingpong 两个 `(2,2,1)` warp 组交替算 tile；coop 一个 `(4,2,1)` 八 warp 组合算；二者都配一个 128 线程的 DMA warp 组（实际 1 warp 发 TMA），CTA 总 384 线程。
- **SMEM 约束**：GeForce 每 CTA 共享内存更紧张，`epi_c_stage_base = 2` 镜像 CUTLASS 的 StagesC 策略，把空间让给 AB 流水线。
- **CLC 动态调度**：Blackwell（含 GeForce）有 CLC 硬件，SM120 默认 `is_dynamic_persistent=True`；CLC pingpong 退化为「一次消费一个」，因为 slot 里是硬件响应、不能隔行跳。
- **blockscaled 默认差异**：SM120 固定 (128,128) pingpong、单 CTA；SM100 按形状选 (256,256)/(256,128)/(128,128)、cluster (2,1)、无 pingpong。
- **SM80 是骨架**：`GemmSm80` 配置层就绪（cp.async + warp MMA、无 TMA/cluster 的设计意图清晰），但 `__call__` 当前 `raise NotImplementedError`，QuACK 暂未实际支持 Ampere GEMM。

## 7. 下一步学习建议

- **补全 blockscaled 全貌**：本讲只讲了 SM120 侧的 block-scaled MMA 选路。建议接着读 u7-l1（`blockscaled/` 的 `BlockScaledOperand` / 格式）和 u7-l2（量化输出 SFD），把 SF 的主机侧容器与设备侧 fragment 串起来。
- **深入 A 侧变换接缝**：本讲提到 `copy_block` 接缝可被 W4 反量化/dropout 替换。读 u7-l3（`operand_transform/`）理解 `transform_a.make_copy_block` 如何挂在 SM120 的 warp MMA 主循环上。
- **对比 SM90 的 pingpong**：本讲的 pingpong 是 warp 粒度，建议回看 u5-l2 的 WGMMA pingpong，体会「warp 组交替」在两代架构下的同与不同。
- **关注 SM80 进展**：若你关心 Ampere 支持，可用 `git log -- quack/gemm_sm80.py` 跟踪 `__call__` 何时落地；在那之前不要在生产中依赖 SM120 以外的 GeForce 老卡跑 QuACK GEMM。
