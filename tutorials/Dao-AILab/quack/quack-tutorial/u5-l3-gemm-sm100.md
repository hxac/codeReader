# SM100（Blackwell）GEMM 与 TMEM

## 1. 本讲目标

本讲聚焦 Blackwell（SM100）架构上的密集 GEMM 内核 `GemmSm100`，讲清它与上一讲 Hopper（SM90）内核的三处本质差异。读完本讲你应当能够：

- 说清 **`tcgen05.mma`** 指令的数据通路：A/B 从 SMEM 读入，累加器直接写进 **TMEM（张量内存）**，再由 epilogue 用 `tcgen05.ld` 取回寄存器。
- 理解 **2-CTA MMA 模式**：`use_2cta_instrs` 何时为真、`cta_group` 如何取值、为何 per-CTA 的 M tile 要折半。
- 读懂 **TMEM 累加器**的列寻址布局、分配（`TmemAllocator`）与回收机制，并能解释 TMEM 相比寄存器累加器的架构优势。
- 看懂 host 侧 `cta_tile_shape_m` 如何把「tile 折半」这个设备侧事实镜像到所有按 M tile 计数的主机缓冲。

本讲默认你已学过 **u5-l1**（`GemmBase` 共享主循环、epilogue 驱动、软件流水线、warp 分工）和 **u3-l5**（mbarrier / 异步流水线同步原语）。

## 2. 前置知识

### 2.1 从 Hopper 到 Blackwell：累加器搬家

Hopper（SM90）的 `wgmma` 把乘加结果累加进**通用寄存器**（register file）。这意味着 MMA 与 epilogue 抢同一份寄存器资源，必须靠 **pingpong**（两个 warp group 交替算相邻 tile）才能把 MMA 与 epilogue 重叠起来。

Blackwell（SM100）引入了 **TMEM（Tensor Memory）**——一块紧挨在张量核心旁、容量约 256 KB / SM 的专用存储。`tcgen05.mma` 直接把累加器写进 TMEM，而不是寄存器。这带来两个后果：

1. MMA warp 不再占用大块寄存器放累加器，寄存器压力骤降。
2. 累加器落在 TMEM 后，由**另一组 epilogue warp** 独立地用 `tcgen05.ld` 把它搬回寄存器再写回 GMEM。MMA 与 epilogue 的重叠从此由硬件 + warp 分工天然提供，不再需要 pingpong。

> 关键直觉：**TMEM 是 MMA 与 epilogue 之间的解耦缓冲**。理解了这一点，后面「SM100 没有 pingpong」「2-CTA」「tile 折半」都有了统一的解释。

### 2.2 术语速查

| 术语 | 含义 |
|---|---|
| **tcgen05** | Blackwell 第 5 代张量核心指令族（`.mma` / `.ld` / `.cp`）。 |
| **TMEM** | Tensor Memory，128 lanes × 512 columns 的 32 位单元阵列，**按列寻址**。 |
| **cta_group** | tcgen05 MMA 的协作组：`ONE`（单 CTA）或 `TWO`（2-CTA 协作）。 |
| **2-CTA MMA** | 一条 MMA 指令由**一对相邻 CTA** 协作完成，合力算一个更大的 tile。 |
| **DP（datapath lane）** | TMEM 的 128 条 lane，对应累加器的「行」。 |
| **Warp 分工** | 一个 CTA 内不同 warp 各司其职：加载、MMA、epilogue、调度。 |

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `quack/gemm_sm100.py` | `GemmSm100` 内核主体：host 侧 `__call__` 配置 + 设备侧 `kernel`（各 warp 主循环）+ `mma` mainloop + epilogue TMEM 取回。 |
| `quack/gemm_config.py` | `cta_tile_shape_m`（tile 折半的 host 镜像）、`_get_sm100_configs`（确认无 pingpong）、`blockscaled_config_ok`。 |
| `quack/spec/tmem.py` | TMEM 列寻址布局代数（`make_tmem_layout`、`TmemAcc`、`TmemStruct`）。 |
| `quack/sm100_utils.py` | quack 自有的 SM100 辅助（blockscaled tiled MMA 构造、gather 布局等）。 |

## 4. 核心概念与源码讲解

### 4.1 tcgen05.mma 指令模型与 warp 分工

#### 4.1.1 概念说明

`GemmSm100` 的模块级文档把 Blackwell GEMM 的执行模型讲得很直白。一条 `tcgen05.mma` 指令做三件事：

- 从 SMEM 读矩阵 A；
- 从 SMEM 读矩阵 B；
- 把乘加结果**写进 TMEM**（不是寄存器）。

随后累加器必须先由 `tcgen05.ld` 加载到寄存器（RMEM），才能写回 GMEM。这套「SMEM→MMA→TMEM→RMEM→GMEM」的数据流，正是 TMEM 解耦 MMA 与 epilogue 的物理基础。

内核整体采用 **warp specialization**（warp 分工）：同一个 CTA 里的不同 warp 被静态绑定到不同职责，靠流水线 mbarrier 握手。

#### 4.1.2 核心流程

一个 CTA 内的 warp 角色与流水线（producer → consumer）：

```
DMA / AB-load warp : TMA 把 A/B 灌进 SMEM     ──ab_pipeline──▶ MMA warp
MMA warp           : 发 tcgen05.mma, 写 TMEM   ──acc_pipeline──▶ Epilogue warp
Epilogue warp      : tcgen05.ld 取 TMEM→寄存器→SMEM→TMA 存 GMEM
Scheduler warp     : CLC 硬件调度, 分发 tile
```

三条流水线把「加载 / 计算 / 收尾」三段重叠成软件流水线：MMA 在算第 `k` 块时，加载 warp 已在搬第 `k+1` 块的 A/B，epilogue warp 在收尾上一个完整 tile。这与 Hopper 相同；不同之处只在**累加器落在 TMEM**，以及 epilogue 因此可以由独立 warp 组承担。

#### 4.1.3 源码精读

模块文档对 tcgen05 数据通路的说明：

[quack/gemm_sm100.py:102-107](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm100.py#L102-L107) — 说明 SM100 的 `tcgen05.mma` 读 A、B 自 SMEM，写累加器到 TMEM，且累加器须先加载到寄存器才能写回 GMEM。

类文档详细列出了每个 warp 的逐 tile / 逐 k 时间线。其中 MMA warp 与 epilogue warp 的职责：

[quack/gemm_sm100.py:153-158](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm100.py#L153-L158) — MMA warp 在 k 循环里 `ab full wait → tcgen05.mma → commit → ab.release`，tile 末尾 `acc.commit`；epilogue warp 则 `acc.wait → tmem load → smem → TMA store`。

主机侧 `__init__` 把 warp id 固化下来（编译期常量），这是 warp 分工的「地址表」：

[quack/gemm_sm100.py:295-304](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm100.py#L295-L304) — 固定 epilogue / MMA / AB-load / epi-load / scheduler 各 warp 的 id，gather_A 时还多一个 A-index 预取 warp。

设备侧 `kernel` 入口用 `warp_idx` 把每个 warp 路由到对应分支：

[quack/gemm_sm100.py:1145-1148](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm100.py#L1145-L1148) — 计算 `warp_idx` 并由 AB-load warp 预取所有 TMA descriptor。

#### 4.1.4 代码实践

**实践目标**：建立「warp 角色 → 数据通路」的映射，确认 tcgen05 把累加器写进 TMEM 而非寄存器。

**操作步骤**：

1. 打开 `quack/gemm_sm100.py`，阅读 102–118 行的模块文档。
2. 跳到 153–158 行的「per-role timeline」，把每行的 `tcgen05.mma` / `acc.commit` / `tmem load` 三个动作画成时间轴。
3. 用 `grep -n "acc_pipeline" quack/gemm_sm100.py` 找出 MMA warp 是 producer、epilogue warp 是 consumer 的成对调用。

**需要观察的现象**：你会看到 MMA warp 调 `acc_pipeline.producer_commit`（写完 TMEM 通知），epilogue warp 调 `acc_pipeline.consumer_wait`（等 TMEM 就绪）——这正是「TMEM 是两组 warp 间的解耦缓冲」的证据。

**预期结果**：能画出 ab_pipeline（load→MMA）与 acc_pipeline（MMA→epilogue）两条流水线，并指出累加器经 TMEM 中转。

#### 4.1.5 小练习与答案

**练习 1**：Hopper（SM90）靠 pingpong 重叠 MMA 与 epilogue，Blackwell 为什么不需要？

**参考答案**：Blackwell 的 `tcgen05.mma` 把累加器写进独立的 TMEM，epilogue 由另一组 warp 用 `tcgen05.ld` 异步取回。MMA 与 epilogue 因此天然并行（由硬件 + warp 分工保证），不需要再用两个 warp group 交替算相邻 tile 的 pingpong 技巧。这也是 `_get_sm100_configs` 里 `pingpong=False`、注释「There's no pingpong on Sm100」的原因。

---

### 4.2 TMEM 累加器：列寻址、分配与回收

#### 4.2.1 概念说明

TMEM 是一块**按列寻址**的存储：128 条 datapath lane（DP）× 512 列，每列 32 位。每个累加器元素的位置由 `(dp, col)` 决定——DP 对应累加器的「行方向」，列对应「列方向」。这与 SMEM（按字节线性寻址）截然不同。

`quack/spec/tmem.py` 用一段模块文档点出了 TMEM 与 SMEM `@cute.struct` 的类比与差异：字段足印用列数计量，每个字段横跨全部 128 条 lane，列偏移叠加到一个 32 位类型的 TMEM 基址指针上；字段布局来自 `tiled_mma`（而非 dtype+size）。

#### 4.2.2 核心流程

TMEM 的生命周期由 `TmemAllocator` 管理，呈「分配—使用—回收」三段：

1. **分配**：epilogue warp（`allocator_warp_id`）调 `tmem.allocate(num_cols)` 申请若干列，硬件返回一个基址指针。
2. **使用**：MMA warp 与 epilogue warp 通过 `tmem.retrieve_ptr(acc_dtype)` 各自拿到同一块累加器的视图；MMA 写、epilogue 读。
3. **回收**：epilogue warp 调 `tmem.relinquish_alloc_permit()` + `tmem.free(ptr)` 释放，供下一个 tile 复用。

列数 `num_tmem_alloc_cols` 在 host 侧由累加器片段的 `partition_shape_C` 推出，并上取整到 2 的幂，且不能超过硬件上限 `get_max_tmem_alloc_cols("sm_100")`。

#### 4.2.3 源码精读

TMEM 列寻址模型与 DP 步长公式：

[quack/spec/tmem.py:18-25](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/tmem.py#L18-L25) — 说明 TMEM 是 128 lane × 512 column 的 32 位单元、按列寻址，字段足印用列数计量。

[quack/spec/tmem.py:28-32](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/tmem.py#L28-L32) — `_tmem_dp_stride`：DP 方向的元素步长是 \((1 \ll 16) \times (32 / \text{bits}(T))\)，体现了「32 位单元按 dtype 缩放」的列寻址规则。

TMEM 布局只显式表示物理 M=64 与 M=128（与 2-CTA 模式直接相关）：

[quack/spec/tmem.py:55-66](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/tmem.py#L55-L66) — `make_tmem_layout` 文档：M=128 用全部 DP lane 线性排布；M=64 用半子分区，行按 `(16,4)` 分组映射到 DP `[0:16], [32:48], [64:80], [96:112]`。

`TmemStruct` 把多个命名 TMEM 区域背靠背打包，并做容量校验：

[quack/spec/tmem.py:245-266](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/spec/tmem.py#L245-L266) — 各字段列数累加、上取整到 2 的幂，并断言不超过 `get_max_tmem_alloc_cols("sm_100")`。

设备侧的分配—使用—回收三段（epilogue warp 负责 allocate/free，MMA warp 只 retrieve）：

[quack/gemm_sm100.py:1199-1203](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm100.py#L1199-L1203) — 构造 `TmemAllocator`，`is_two_cta=use_2cta_instrs`、`allocator_warp_id=epilog_warp_id[0]`。

[quack/gemm_sm100.py:1704-1705](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm100.py#L1704-L1705) — MMA warp `tmem.wait_for_alloc()` 后 `retrieve_ptr(acc_dtype)` 得到 TMEM 累加器基址。

[quack/gemm_sm100.py:1862-1863](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm100.py#L1862-L1863) — epilogue warp 执行 `tmem.allocate(num_tmem_alloc_cols)` 与 `wait_for_alloc()`。

[quack/gemm_sm100.py:2028-2030](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm100.py#L2028-L2030) — epilogue 收尾后 `relinquish_alloc_permit()` + `tmem.free(acc_tmem_ptr)` 回收 TMEM。

host 侧列数推导：

[quack/gemm_sm100.py:2815-2837](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm100.py#L2815-L2837) — `_compute_num_tmem_alloc_cols`：用 `partition_shape_C` 造累加器片段，调 `get_num_tmem_alloc_cols` 得列数。

#### 4.2.4 代码实践

**实践目标**：理解 TMEM 累加器相比寄存器累加器的优势，以及列数如何受 tile 形状约束。

**操作步骤**：

1. 在 `gemm_sm100.py` 用 `grep -n "num_acc_stage" quack/gemm_sm100.py` 找到累加器级数的设定。
2. 阅读 2722–2727 行 `_compute_stages` 里 `num_acc_stage` 的默认值：tile_n 较大时为 1，否则为 2。
3. 思考：若是寄存器累加器，能否轻松支持「2 级累加器 + 独立 epilogue warp 组」。

**需要观察的现象**：TMEM 累加器可以多级缓冲（`num_acc_stage=2`），让 MMA 写下一个 tile 的同时 epilogue 排空当前 tile；而寄存器累加器受寄存器压力限制，很难做到大 tile 双缓冲。

**预期结果**：能口述「TMEM 把累加器从寄存器堆搬走，既释放寄存器又支持多级缓冲与 MMA/epilogue 并行」这条主线。若需精确数值，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：TMEM 地址是 `(dp << 16) | col`（32 位字）。对一个 bf16（16 位）累加器视图，DP 方向的元素步长是多少？

**参考答案**：由 `_tmem_dp_stride`，步长 \(= (1 \ll 16) \times (32 / 16) = 2^{17}\) 个元素。即相邻 DP lane 上的同列元素相隔 \(2^{17}\) 个元素（因为 32 位字容纳 2 个 bf16，`tmem_ptr<T>` 已施加子字缩放）。

**练习 2**：为什么 TMEM 分配由 epilogue warp 而非 MMA warp 执行？

**参考答案**：`TmemAllocator` 的 `allocator_warp_id` 固定为首个 epilogue warp。分配与回收是成对的资源生命周期管理，而 epilogue warp 是每个 tile 里**最后**读取 TMEM 的一方（读完后才能释放），由它持有分配器最自然；MMA warp 只是 `retrieve_ptr` 的消费者，不主导生命周期。这样还能让分配/释放与 epilogue 排空时序对齐。

---

### 4.3 2-CTA MMA 模式

#### 4.3.1 概念说明

Blackwell 的 `tcgen05.mma` 支持 **2-CTA 协作模式**：一条 MMA 指令由**一对相邻 CTA**（cluster 内 M 方向上的邻居）合力完成，共同算一个更大的 tile。这是 Blackwell 在不增加单 CTA 寄存器压力的前提下、提升单条指令吞吐的关键机制。

是否启用 2-CTA 由 `use_2cta_instrs` 这个**编译期布尔**决定，它在 host 侧 `__init__` 里根据 cluster 与 tile 形状算出，并映射为 `tcgen05.CtaGroup.TWO` / `ONE` 传给 `tiled_mma`。

#### 4.3.2 核心流程

启用 2-CTA 的两个充要条件：

\[
\text{use\_2cta\_instrs} = (\text{cluster\_M} \bmod 2 = 0) \;\land\; (\text{mma\_tiler\_M} \in \text{valid\_2cta\_M})
\]

其中 `valid_2cta_M = (128, 256)`（dense）或 `(256,)`（blockscaled）。

一旦启用：

- `cta_group = tcgen05.CtaGroup.TWO`，`tiled_mma.thr_id.shape` 的 size 变为 **2**。
- cluster 内一对 CTA 中，`bidx % 2 == 0` 的是 **leader CTA**，负责真正下发 MMA 指令；另一个是 peer。
- per-CTA 的 M tile 折半（见 4.4）。

设备侧通过 `use_2cta_instrs = (cute.size(tiled_mma.thr_id.shape) == 2)` 重新派生这个布尔值——它直接来自 `tiled_mma` 的线程布局，避免再传一个参数。

#### 4.3.3 源码精读

`use_2cta_instrs` 与 `cta_group` 的设定：

[quack/gemm_sm100.py:258-259](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm100.py#L258-L259) — `valid_2cta_m = (128, 256) if not self.blockscaled else (256,)`；`use_2cta_instrs = cluster_shape_mnk[0] % 2 == 0 and mma_tiler_mnk[0] in valid_2cta_m`。

[quack/gemm_sm100.py:290](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm100.py#L290) — `cta_group = tcgen05.CtaGroup.TWO if self.use_2cta_instrs else tcgen05.CtaGroup.ONE`。

构造 `tiled_mma` 时把 `cta_group` 传进去（dense 路径）：

[quack/gemm_sm100.py:386-395](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm100.py#L386-L395) — `make_trivial_tiled_mma(..., self.cta_group, self.mma_inst_shape_mnk[:2])`，cta_group 决定了 MMA 是单 CTA 还是 2-CTA 变体。

设备侧派生 leader 身份：

[quack/gemm_sm100.py:1160-1166](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm100.py#L1160-L1166) — `use_2cta_instrs = cute.size(tiled_mma.thr_id.shape) == 2`；`mma_tile_coord_v = bidx % cute.size(tiled_mma.thr_id.shape)`；`is_leader_cta = mma_tile_coord_v == 0`。

MMA mainloop 里**只有 leader CTA 下发 MMA 指令**：

[quack/gemm_sm100.py:2244-2247](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm100.py#L2244-L2247) — leader 先 `acc_pipeline.producer_acquire`，再 `tiled_mma.set(tcgen05.Field.ACCUMULATE, False)` 重置累加标志。

[quack/gemm_sm100.py:2264-2266](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm100.py#L2264-L2266) — `if is_leader_cta:` 守卫整个 `consumer_wait → cute.gemm → release` 块；peer CTA 只参与流水线 arrive，不下发指令。

2-CTA 时 epilogue 的 warp 形状也要调整（per-CTA M 变小）：

[quack/gemm_sm100.py:347-354](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm100.py#L347-L354) — 当 per-CTA M tile 为 64 且启用 2-CTA 时，epilogue 用 `(warp_m, warp_n) = (2, 2)`，否则 `(4, 1)`。

`acc_pipeline` 在 2-CTA 下把两个 CTA 的 epilogue arrive 都路由到 leader 的屏障：

[quack/gemm_sm100.py:2560-2577](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm100.py#L2560-L2577) — `num_acc_consumer_threads` 在 2-CTA 时翻倍（`* 2`），`ctas_routed=2`，注释指出两个 CTA 的 epi warp 都把 arrive 路由到 leader 的屏障。

#### 4.3.4 代码实践

**实践目标**：验证「leader 下发、peer 参与」的 2-CTA 协作结构，理解 `is_leader_cta` 守卫的作用。

**操作步骤**：

1. 打开 `quack/gemm_sm100.py` 的 `mma` 方法（2208–2329 行）。
2. 找到所有 `if is_leader_cta:` 守卫，列出 leader 独占执行的动作（`consumer_wait`、`cute.gemm`、`consumer_release`、`producer_commit`）。
3. 找到 `if not is_leader_cta:` 分支（仅 gather_A + 2-CTA + 非 TMA 路径），看 peer CTA 如何用 `mbarrier_arrive_release_cluster` 把自己的 cp.async 写释放给 leader 的 MMA（cluster scope）。

**需要观察的现象**：peer CTA 不发 `cute.gemm`，但仍参与 `ab_consumer_state.advance()` 与流水线 arrive；2-CTA 是「协作」而非「独立」。

**预期结果**：能解释为何一条 2-CTA MMA 指令只需 leader 下发——因为它在硬件层面会读 pair 中两个 CTA 的 SMEM（经 DSMEM），故双方都要把数据可见性释放到 cluster scope。

#### 4.3.5 小练习与答案

**练习 1**：给定 `cluster_shape_mn=(2,1)`、`mma_tiler_mnk=(128,128)`、dense（非 blockscaled）。`use_2cta_instrs` 为真吗？`cta_group` 是什么？

**参考答案**：cluster_M=2（偶），mma_tiler_M=128 ∈ (128,256)，故 `use_2cta_instrs=True`，`cta_group=tcgen05.CtaGroup.TWO`。

**练习 2**：把上面改成 `cluster_shape_mn=(1,2)`，结论如何？为什么？

**参考答案**：cluster_M=1（奇），不满足 `cluster_M % 2 == 0`，故 `use_2cta_instrs=False`，`cta_group=ONE`。2-CTA 协作发生在 cluster 的 **M 维**，(1,2) 的多播在 N 维，没有 M 维邻居可配对。

---

### 4.4 cluster MMA 与 tile 折半（host 侧镜像）

#### 4.4.1 概念说明

2-CTA MMA 把一个 `mma_tiler_M` 大小的 tile **分摊到两个 CTA**，于是每个 CTA 实际只负责 `mma_tiler_M / 2` 行的输出。这个「per-CTA M tile」记为 `cta_tile_shape_m`，它是一个被**全项目多方**共享的几何量：

- tile scheduler 按它分配输出 tile；
- OOB（越界）限制按它计算；
- reduce-sink 的 partial 槽位按它计数；
- 任何按 M tile 大小的 host 侧缓冲（如 split-K partials）都必须用它。

因此 quack 把这个折半规则**镜像成 host 侧纯函数** `cta_tile_shape_m`，确保设备侧与主机侧口径一致。

#### 4.4.2 核心流程

设备侧的折半由布局代数自动完成：

\[
\text{cta\_tile\_shape\_M} = \text{mma\_tiler\_M} \;/\; \text{cute.size}(\text{tiled\_mma.thr\_id.shape})
\]

2-CTA 时分母为 2，于是 `mma_tiler_M ∈ {128, 256}` 折半成 `{64, 128}`。注意 `make_tmem_layout` 只显式表示物理 M=64 与 M=128——这正对应 2-CTA 折半后的两种 per-CTA 形态。

host 侧 `cta_tile_shape_m` 用同样判据：仅当 `device_capacity ∈ {10,11}`（Blackwell）、cluster_M 偶、tile_M ∈ valid_2cta_M 时返回 `tile_M // 2`，否则原样返回。

#### 4.4.3 源码精读

设备侧 per-CTA tile 的推导：

[quack/gemm_sm100.py:445-449](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm100.py#L445-L449) — `cta_tile_shape_mnk = (mma_tiler[0] // cute.size(tiled_mma.thr_id.shape), mma_tiler[1], mma_tiler[2])`。2-CTA 时 `thr_id.shape` size=2，M 折半。

host 侧镜像函数：

[quack/gemm_config.py:57-68](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_config.py#L57-L68) — `cta_tile_shape_m`：注释明说「mirrors `GemmSm100.use_2cta_instrs`」，并要求「host-side buffers sized per M tile must use it too」；判据与设备侧完全一致。

SM100 配置空间确认无 pingpong（与 4.1 呼应）：

[quack/gemm_config.py:212-214](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_config.py#L212-L214) — `_get_sm100_configs` 用 `partial(GemmConfig, pingpong=False, device_capacity=10)`，旁注「There's no pingpong on Sm100」。

blockscaled 的 64-N 颗粒约束（与 tile 折半并列的另一条 SM100 硬约束）：

[quack/gemm_config.py:71-95](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_config.py#L71-L95) — `blockscaled_config_ok`：tile_m∈{128,256}、tile_n 为 64 的倍数且在 [64,256]、cluster 各维 ≤4，是 SM100 blockscaled 合法性的唯一真值来源。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：结合 `cta_tile_shape_m` 解释「cluster_M 偶且 tile_m∈{128,256} 时 per-CTA M tile 折半」，并说明 TMEM 累加器相比寄存器累加器的优势。

**操作步骤**：

1. 打开 `quack/gemm_config.py`，阅读 `cta_tile_shape_m`（57–68 行）及其 docstring。
2. 手算下表，逐行填入 `use_2cta_instrs`、`cta_group`、`cta_tile_shape_m`：

   | tile_m | cluster_m | blockscaled | use_2cta? | cta_group | cta_tile_shape_m |
   |--------|-----------|-------------|-----------|-----------|------------------|
   | 128    | 2         | False       | ?         | ?         | ?                |
   | 256    | 2         | False       | ?         | ?         | ?                |
   | 128    | 1         | False       | ?         | ?         | ?                |
   | 256    | 2         | True        | ?         | ?         | ?                |
   | 128    | 2         | True        | ?         | ?         | ?                |

3. 打开 `quack/gemm_sm100.py:445-449`，确认设备侧 `mma_tiler[0] // cute.size(tiled_mma.thr_id.shape)` 与上表口径一致。
4. 写一段话回答：**为何要把折半规则同时写在设备侧和 host 侧两处？** 提示看 `cta_tile_shape_m` 的 docstring（「Tile schedulers, OOB limits, and reduce-sink partial slots all count M in this unit」）。

**需要观察的现象 / 预期结果**：

填表答案：

| tile_m | cluster_m | blockscaled | use_2cta? | cta_group | cta_tile_shape_m |
|--------|-----------|-------------|-----------|-----------|------------------|
| 128    | 2         | False       | **True**  | TWO       | **64**           |
| 256    | 2         | False       | **True**  | TWO       | **128**          |
| 128    | 1         | False       | False     | ONE       | 128              |
| 256    | 2         | True        | **True**  | TWO       | **128**          |
| 128    | 2         | True        | False     | ONE       | 128              |

（最后一行：blockscaled 时 `valid_2cta_m=(256,)`，128 不在其中，故不启用 2-CTA。）

**TMEM 相比寄存器累加器的优势**（实践要求回答的另一半）：

- **释放寄存器**：tcgen05 MMA 写 TMEM，不占用通用寄存器堆放累加器，大 tile（如 256×256）才可行；若放寄存器会撑爆 256 regs/warp 上限。
- **天然并行**：累加器落 TMEM 后由独立 epilogue warp 组异步取回，MMA 与 epilogue 重叠由硬件提供，免去 Hopper 的 pingpong。
- **多级缓冲**：TMEM 可轻松支持 `num_acc_stage=2` 双缓冲，让下一个 tile 的 MMA 与当前 tile 的 epilogue 排空并行；寄存器累加器难以承受双份大 tile 的寄存器开销。

> 若你在本机有 B200/B300，可进一步用 `QUACK_CACHE_ENABLED=0` 跑 `pytest tests/test_gemm_functional.py -x -k "bfloat16"` 验证编译产物对 2-CTA / 非 2-CTA 配置确实特化出不同 cubin；否则标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `cta_tile_shape_m` 是纯 host 函数、且 docstring 强调「keep in sync」？

**参考答案**：因为它必须与设备侧 `GemmSm100.use_2cta_instrs` 的判据**逐字一致**——任何按 per-CTA M tile 大小分配的主机缓冲（split-K partials、reduce-sink 槽位、OOB 限制）都依赖它。一旦两边判据漂移，主机缓冲就会按错误的 M 单位计数，导致越界或结果错乱。写成纯函数并标注「keep in sync」是为了让任何修改 `use_2cta_instrs` 的人同时想到改这里。

**练习 2**：`make_tmem_layout` 只显式支持物理 M=64 与 M=128。这与 2-CTA 有何关系？

**参考答案**：2-CTA 把 `mma_tiler_M ∈ {128,256}` 折半成 per-CTA `{64,128}`，正好对应 `make_tmem_layout` 支持的两种物理 M。M=128 用全部 DP lane 线性排布；M=64 用半子分区（`(16,4)` 分组映射到 4 段 DP）。换言之，TMEM 布局代数就是为 2-CTA 折半后的两种 per-CTA 形态量身设计的。

---

## 5. 综合实践

把本讲四条主线串起来，完成一次「配置 → 设备侧派生 → TMEM 生命周期」的端到端追踪。

**任务**：给定一个 SM100 dense GEMM 配置 `GemmConfig(tile_m=256, tile_n=128, cluster_m=2, cluster_n=1, pingpong=False, device_capacity=10)`，回答下列问题并给出源码依据：

1. `use_2cta_instrs` 与 `cta_group` 各是什么？依据 [gemm_sm100.py:258-259](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm100.py#L258-L259) 与 [gemm_sm100.py:290](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm100.py#L290)。
2. 设备侧 `cta_tile_shape_mnk[0]` 等于多少？依据 [gemm_sm100.py:445-449](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm100.py#L445-L449)。
3. host 侧 `cta_tile_shape_m(256, 2, 10, blockscaled=False)` 返回什么？依据 [gemm_config.py:57-68](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_config.py#L57-L68)。它是否与第 2 问一致？
4. `is_leader_cta` 如何决定？MMA 指令由哪个 CTA 下发？依据 [gemm_sm100.py:1160-1166](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm100.py#L1160-L1166) 与 [gemm_sm100.py:2264-2266](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm100.py#L2264-L2266)。
5. 画出这个 tile 的累加器数据通路，标出 TMEM 在其中的位置（参考 [gemm_sm100.py:102-107](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm100.py#L102-L107)）。

**参考结论**：(1) True / TWO。(2) 128（256 // 2）。(3) 128，一致。(4) `bidx % 2 == 0` 为 leader；仅 leader 下发 `cute.gemm`。(5) `SMEM(A,B) → tcgen05.mma → TMEM(acc) → tcgen05.ld → RMEM → SMEM(sD) → TMA → GMEM`，TMEM 是 MMA 与 epilogue 之间的解耦缓冲。

## 6. 本讲小结

- Blackwell 的 `tcgen05.mma` 把累加器写进 **TMEM**（而非寄存器），由独立 epilogue warp 用 `tcgen05.ld` 取回——这是 SM100 区别于 SM90 的核心。
- TMEM 是 128 lane × 512 column 的**按列寻址**存储，由 `TmemAllocator` 在 epilogue warp 上分配/回收，列数由 `partition_shape_C` 推出。
- **2-CTA MMA**：cluster_M 为偶且 `mma_tiler_M ∈ {128,256}`（blockscaled 仅 256）时启用，`cta_group=TWO`，一对 CTA 协作算一个 tile，仅 leader 下发指令。
- tile 折半：2-CTA 时 per-CTA M = `mma_tiler_M / 2`，设备侧由 `thr_id.shape` 自动推导，host 侧由 `cta_tile_shape_m` 镜像，所有按 M tile 计数的主机缓冲都必须用它。
- SM100 没有 pingpong：TMEM 解耦使 MMA 与 epilogue 由硬件 + warp 分工天然重叠，转而调优 `use_clc` / `use_tma_gather`。
- TMEM 相比寄存器累加器的三大优势：释放寄存器、天然并行、支持多级缓冲。

## 7. 下一步学习建议

- **u5-l4（SM120/SM80）**：看 Blackwell 消费级（RTX 50）为何改用 **warp MMA**（无 tcgen05 / 无 TMEM），对照理解 TMEM 是数据中心 Blackwell 的专属。
- **u5-l5（spec 层）**：深入 `spec/tma.py`、`spec/mma.py`、`spec/tmem.py`、`spec/tensor_spec.py`，把本讲提到的 `BoundMMASm100`、`TmemAcc`、`TmemOperandA` 串成完整的描述符抽象。
- **u7-l1（blockscaled）**：本讲多次提到 blockscaled 的 SF（scale factor）也驻留 TMEM，并与累加器重叠（`overlap_accum_sf`）；下一阶段可读 `blockscaled/operand.py` 看 MXFP8/NVFP4 如何与 2-CTA MMA 组合。
- 进阶阅读：`quack/gemm_sm100.py` 的 `mma` 方法（2208–2329 行）完整呈现了 gather_A + 2-CTA 时 peer CTA 经 cluster-scope mbarrier 释放 SMEM 可见性的细节，值得逐行精读。
