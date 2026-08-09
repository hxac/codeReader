# SM90（Hopper）GEMM

## 1. 本讲目标

本讲是「GEMM 设备侧内核」系列的第二篇，专门拆解 Hopper（SM90）上的矩阵乘内核 `GemmSm90`。读完本讲你应该能够：

- 说清 `GemmSm90` 的构造配置：tile 形状、cluster、`atom_layout`、warp group 划分、coop 与 pingpong 两种主循环模式的区别。
- 理解 Hopper 的 TMA 加载如何在 cluster 内做多播（multicast），以及 A/B 两个操作数分别沿哪个 cluster 维被共享。
- 读懂 WGMMA（warpgroup MMA）主循环：SS（A 来自 SMEM）与 RS（A 来自寄存器）两条路径，以及 `wait_group` 的节拍是如何保证数据依赖的。
- 理解 `sm90_utils.partition_for_epilogue` 如何为 epilogue 的子 tile 切分累加器/输出张量。
- 解释为什么 SM90 上动态持久化（dynamic persistent）默认关闭。

本讲承接 [u5-l1](u5-l1-gemm-base.md)（`GemmBase` 共享主循环与 epilogue 驱动）：`GemmSm90` 继承 `GemmTmaBase`，把架构无关的 epilogue 驱动、split-K 收尾、tile 调度参数都复用基类，自己只实现 Hopper 特有的 mainloop 与配置。

## 2. 前置知识

在进入源码前，先建立三个直觉。本讲不会从 CUDA 汇编讲起，但下面这些概念会反复出现。

**（1）Hopper 的三类内存与搬运单位。** 一张 H100（SM90）里，每个 SM 有：全局显存（GMEM/HBM）、共享内存（SMEM，每 SM 上限 228 KB）、寄存器（RMEM）。数据搬运的两条主力通道是 **TMA（Tensor Memory Access）** 与 **WGMMA（Warpgroup MMA）**：

- TMA 是「描述符驱动」的整块异步拷贝：CPU 侧把一个张量的形状/步长/swizzle 编码进一个 TMA descriptor，GPU 上一条指令就能把 GMEM 的一个 2D/3D 块搬进 SMEM，并由事务屏障（mbarrier）确认「字节数已到位」。TMA 还能做 **multicast**：一次拷贝同时送到 cluster 内多个 CTA 的 SMEM。
- WGMMA 是 Hopper 的矩阵乘指令：一条指令由一个 warpgroup（4 个 warp = 128 线程）发出，A 可以来自 SMEM（SS 模式）或寄存器（RS 模式），B 来自 SMEM，结果写进 warpgroup 的累加器寄存器。它是异步的——发射后不阻塞，用 `wait_group` 显式等结果。

**（2）cluster（线程块集群）。** SM90 起支持把最多 4 个 CTA 编成一个 cluster，cluster 内 CTA 共享分布式 SMEM，可经 SMEM 互访并做 TMA 多播。本讲里 `cluster_shape_mnk = (cluster_M, cluster_N, 1)`，cluster 维只在 M/N 上取值。

**（3）软件流水线 + warp specialization（warp 专精）。** GEMM 是「搬运」与「计算」的交替。Hopper 内核把这两件事交给不同 warp：一组 **producer warp** 用 TMA 把 A/B 灌进 SMEM 多级缓冲（stage），另一组 **consumer warp**（mma warpgroup）从 SMEM 取片段发 WGMMA，二者靠 mbarrier 的「空满状态机」握手反压。这正是一切 QuACK 流水线的基础（见 [u3-l5](u3-l5-pipeline-sync.md)）。

> 名词速查：CTA（线程块）、warp（32 线程）、warpgroup（4 warp=128 线程）、SMEM（共享内存）、RMEM（寄存器）、TMA、WGMMA、MMA atom（一条矩阵乘指令的原子）、atom_layout（多个 atom 沿 M/N 摆放）、tile（一个 CTA 负责的输出块）、stage（流水线级/缓冲份数）、cluster。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [quack/gemm_sm90.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py) | 本讲主角。`GemmSm90` 类：构造配置、`__call__` 主机编排、`kernel` 设备内核、`mma` / `mma_rs_interleaved` 两条主循环、pingpong 屏障、epilogue 分区辅助、stage/smem 布局计算。 |
| [quack/sm90_utils.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/sm90_utils.py) | Hopper 专属工具：`make_tiled_mma`（构造 WGMMA）、`gemm`（单次 WGMMA 封装）、`partition_fragment_ABC`（切 A/B/C 片段）、`partition_for_epilogue`、`canonical_a_load_s2r`（ldmatrix 加载）。 |
| [quack/gemm_base.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py) | `GemmTmaBase` / `GemmBase`。`GemmSm90` 复用其 `load_tma`、`make_ab_pipeline`、`epilogue`、`get_scheduler_arguments` 等架构无关逻辑。 |
| [quack/gemm_config.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_config.py) | SM90 的 autotune 配置空间 `_get_sm90_configs`：tile 形状、cluster 选项、`is_dynamic_persistent` 默认值。 |
| [quack/tile_scheduler.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/tile_scheduler.py) | `TileScheduler` 与 `PersistenceMode`（NONE/STATIC/DYNAMIC/CLC），决定 tile 如何分配给 CTA。 |

## 4. 核心概念与源码讲解

### 4.1 GemmSm90 类与配置

#### 4.1.1 概念说明

`GemmSm90` 是一个**持久化（persistent）** GEMM 内核：每个 CTA 不只算一个输出 tile，而是被调度器反复派发多个 tile，直到所有 tile 算完。它把 Hopper 的硬件特性——TMA、WGMMA、cluster、warp specialization——组装成一条软件流水线。

构造时需要回答几个关键问题，这些问题的答案就是本节要读的配置逻辑：

1. **tile 多大？** 一个 CTA 一次 MMA 覆盖多大的 `(M, N)` 输出块？
2. **多少个 warpgroup 做 MMA？它们如何摆放？** 这由 `atom_layout_mnk` 决定。
3. **coop 还是 pingpong？** 所有 MMA warpgroup 合力算一个 tile（cooperative），还是两个 warpgroup 交替算两个 tile（pingpong）？
4. **寄存器预算如何分配？** Hopper 用 `setmaxreg` 指令给不同 warp 组分配不同寄存器上限——producer warp 少寄存器、consumer warp 多寄存器。

#### 4.1.2 核心流程

构造阶段（`__init__`）只做「几何与策略」的静态配置，不碰具体张量。流程可概括为：

```text
__init__:
  记录 acc_dtype / pingpong / is_persistent / use_clc_persistence / split_k
  校验 tile_M, tile_N（按是否 pingpong 有不同约束）
  ── 推导 atom_layout_mnk：(atom_M, atom_N, 1)
        coop:  沿 M 拆 (atom_M = tile_M//64 或 2)，atom_N=1；或 tile_M=320/192 时沿 N 拆
        pingpong: 固定 (1,1)
  ── mma_warp_groups = prod(atom_layout) * (1 if coop else 2)
  ── 线程数 / 寄存器预算 / epilogue warp 数 / 屏障 id
  ── transform_a（若指定，强制 RS 主循环）
```

理解 `atom_layout` 与 `mma_warp_groups` 是理解后面一切 warp 划分的基础，所以先讲清楚它。

**Hopper WGMMA 的物理约束：** 一条 WGMMA 的物理 M 维固定为 64（见 [quack/sm90_utils.py:56](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/sm90_utils.py#L56)）。当 tile_M 比单个 atom 大时，内核把多个 atom 沿 M 或 N 摆开（`atom_layout_mnk`），每个 atom 对应一个 warpgroup。因此 `mma_warp_groups`（MMA warpgroup 个数）只能取 1/2/3。

- **coop（cooperative）模式：** `mma_warp_groups` 个 warpgroup 合力算**同一个** tile。`tile_M=256` 时 `atom_M=2`，即两个 warpgroup 各算一半 M 行；`tile_M=192` 且 `tile_N<=128` 时 `atom_M=3`（三个 warpgroup 各算 64 行）。
- **pingpong 模式：** 固定 `atom_layout=(1,1,1)`，但 `mma_warp_groups=2`，两个 warpgroup **交替算相邻的两个 tile**——一个算 tile 时，另一个写上一个 tile 的 epilogue，二者靠 pingpong 屏障交接。

#### 4.1.3 源码精读

类声明与架构标记：

[quack/gemm_sm90.py:75](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L75) —— `GemmSm90` 继承 `GemmTmaBase`，所有 TMA 流水线与 epilogue 驱动复用基类。

[quack/gemm_sm90.py:141-148](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L141-L148) —— `arch = 90` 是 SM 选类与配置过滤的总开关；`epi_c_stage_base = 4` 是 SM90 的 C-load 流水线深度基准（注释说明 SM120 会覆写为 2，因为其 SMEM 预算更紧）。

构造签名浓缩了所有可调旋钮：

[quack/gemm_sm90.py:152-169](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L152-L169) —— 关键参数：`tile_shape_mnk`、`cluster_shape_mnk`、`pingpong`、`is_persistent`、`fp8_fast_accum`、`gather_A`、`use_clc_persistence`、`mma_is_rs`、`transform_a`。注意 `use_clc_persistence` 在 SM90 上被断言禁用：

[quack/gemm_sm90.py:192-197](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L192-L197) —— CLC（Cooperative Launch Control，SM100 才有的硬件持久化机制）在 SM90 上不可用（`assert self.arch == 100`）；pingpong 必须搭配 persistent 调度。

**atom_layout 推导**（本节最核心的一段）：

[quack/gemm_sm90.py:256-272](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L256-L272) —— coop 模式下沿 M 拆（`atom_M = tile_M//64`，但 `tile_M>=256` 时取 2，避免 atom 过多）；`tile_M=320`（不能被 64 整除成偶数）改沿 N 拆成 `(1,2)`；pingpong 固定 `(1,1,1)`。

**warp group 数与线程布局：**

[quack/gemm_sm90.py:282-296](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L282-L296) —— 几个决定 warp 划分的等式：

- `mma_warp_groups = prod(atom_layout) * (1 if coop else 2)`，并断言只能是 1/2/3；
- `threads_per_cta = (mma_warp_groups + 1) * 128`——多出的那 `+1` 个 warpgroup 是 producer（AB-load warp + scheduler warp）；
- `num_ab_load_warps = 1`（普通情况只有 1 个 TMA load warp；`gather_A` 时用 4 个 cp.async warp）；
- `ab_load_warp_id = mma_warp_groups * 4`——producer warp 的起始 warp 号，正好排在所有 MMA warp 之后。

**寄存器预算：**

[quack/gemm_sm90.py:297-325](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L297-L325) —— 先按 `prod(tile_MN) / (prod(atom) * 128)` 估算每线程寄存器数（累加器占用），再选 `(num_regs_load, num_regs_mma)`：producer warp 少寄存器、MMA warp 多寄存器。这些值稍后在 kernel 里通过 `setmaxregister_decrease` / `setmaxregister_increase` 真正生效（见 4.3）。

#### 4.1.4 代码实践

> 实践目标：亲手用具体 tile 形状推一遍 `atom_layout` 与 `mma_warp_groups`，验证你对 coop/pingpong 的理解。

1. 打开 [quack/gemm_sm90.py:256-285](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L256-L285)。
2. 对下表每一行，按源码逻辑写出 `atom_layout_mnk`、`mma_warp_groups`、`threads_per_cta`、`ab_load_warp_id`：

| 模式 | tile_M | tile_N | atom_layout_mnk | mma_warp_groups | threads_per_cta |
| --- | --- | --- | --- | --- | --- |
| coop | 256 | 128 | ? | ? | ? |
| coop | 192 | 128 | ? | ? | ? |
| coop | 320 | 160 | ? | ? | ? |
| pingpong | 128 | 128 | ? | ? | ? |

3. **预期结果（待本地验证）**：`256×128` → `(2,1,1)`、2 个 WG、`3*128=384` 线程；`192×128` → `(3,1,1)`、3 个 WG、512 线程；`320×160` → `(1,2,1)`、2 个 WG、384 线程；pingpong `128×128` → `(1,1,1)`、2 个 WG、384 线程。
4. 观察：pingpong 下 `atom_layout` 永远是 `(1,1,1)`，但 `mma_warp_groups` 仍是 2——这正是「两个 WG 各自独立算一个完整 tile」的体现。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `tile_M=320` 不能像 `tile_M=256` 那样沿 M 拆 atom？
**答**：因为一条 WGMMA 的物理 M 维是 64，`320/64=5` 不是 2（`atom_M` 想要的偶数），而 `atom_layout_m` 被限制在 `{1,2,3}`，5 个 atom 会让 warpgroup 数超标，所以改沿 N 拆成 `(1,2)`（见源码第 257-258 行的 `tile_M == 320` 分支）。

**练习 2**：`threads_per_cta = (mma_warp_groups + 1) * 128` 里那个 `+1` 是什么？
**答**：它是 producer warpgroup——包含 AB-load warp（与可选的 scheduler warp、C-load warp）。`mma_warp_groups` 个 warpgroup 全部用于 WGMMA 与 epilogue，多出的一个专门做 TMA 搬运与调度。

---

### 4.2 TMA 加载与 cluster 多播

#### 4.2.1 概念说明

GEMM 的「输入侧」要做的事：把 A、B 两个操作数从 GMEM 搬进 SMEM 的多级缓冲（AB pipeline 的 stage），供 MMA warp 读取。Hopper 上这件事由 **producer warp** 用 **TMA** 完成。

cluster 的价值在这一步才真正体现：当 cluster 沿 M 或 N 维配对多个 CTA 时，这些 CTA 会**共享其中一个操作数**——它们需要的是同一个 SMEM 块。TMA multicast 让这份共享数据只从 L2/DRAM 读**一次**，然后同时送到所有 peer CTA 的 SMEM，直接砍掉一半的 GMEM 带宽。

- cluster=(1,2)：两个 CTA 沿 **N** 配对，算 `(m, n0)` 与 `(m, n1)`——**同一个 M 行**，共享 **A**。
- cluster=(2,1)：两个 CTA 沿 **M** 配对，算 `(m0, n)` 与 `(m1, n)`——**同一个 N 列**，共享 **B**。

#### 4.2.2 核心流程

```text
producer warp（ab_load_warp_id 起）的持久化循环：
  while 还有 work_tile:
      tile_coord = scheduler 解码 (pid_m, pid_n, split_idx, batch_idx)
      gA = local_tile(A, (tile_M, tile_K), (pid_m, None))   # 本 tile 的 A 块
      gB = local_tile(B, (tile_N, tile_K), (pid_n, None))   # 本 tile 的 B 块
      copy_A = tma_get_block_copy_fn(tma_atom_a, gA, sA, multicast={cluster, dim="M"})
      copy_B = tma_get_block_copy_fn(tma_atom_b, gB, sB, multicast={cluster, dim="N"})
      load_tma(ab_pipeline, [copy_A, copy_B, copy_AuxA], k_tile_cnt)  # 逐 k-tile 灌 stage
      scheduler.advance_to_next_work()
```

`load_tma`（基类实现）是一个标准的软件流水线 producer 循环：对每个 k-tile，`producer_acquire`（等 SMEM 缓冲空 + 给事务屏障上膛）→ 发 TMA（`complete_tx::bytes` 信用由 mbarrier 自动记账）→ `producer_commit` → `advance` 推进 stage 索引。

#### 4.2.3 源码精读

**多播计数：**

[quack/gemm_sm90.py:274-279](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L274-L279) —— `num_mcast_ctas_a = cluster_shape_mnk[1]`（cluster 的 N 维），`num_mcast_ctas_b = cluster_shape_mnk[0]`（cluster 的 M 维）。这印证了上面的结论：A 沿 N 维 cluster 多播、B 沿 M 维 cluster 多播。

**TMA 多播方向：**

[quack/gemm_sm90.py:1045-1052](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L1045-L1052) —— `a_tma_multicast` 用 `multicast_dim="M"`（A 跨 N-peer 共享），`b_tma_multicast` 用 `multicast_dim="N"`。注释点出一个关键细节：multicast 的坐标必须由 multicast 掩码固定——A 在 N-peer 间是同一个 M 坐标，B 在 M-peer 间是同一个 N 坐标。

**load_tma 调用：**

[quack/gemm_sm90.py:1148-1155](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L1148-L1155) —— 把 `[copy_A, copy_B, copy_AuxA]` 三个 copy 函数交给基类 `load_tma`，`k_tile_start` 支持 split-K 的 K 段偏移。

**load_tma 本身（基类）：**

[quack/gemm_base.py:1010-1038](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L1010-L1038) —— `producer_try_acquire` 是「窥探」优化：在循环顶部先尝试获取下一级缓冲，减少 acquire 的停顿；每个 k-tile 把所有 copy_fn 都灌进同一个 stage。

**AB pipeline 构造（含多播到达计数）：**

[quack/gemm_base.py:1216-1247](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L1216-L1247) —— `consumer_arrive_cnt = mcast_size * (tiled_mma.size / WARP_SIZE)`，其中 `mcast_size = num_mcast_ctas_a + num_mcast_ctas_b - 1`。也就是说，每个 MMA warp 在**每个多播 peer CTA** 都要「到达」一次来释放该 stage——这正是 multicast 屏障的到达计数规则（数据被所有 peer 读完后才能回收）。pipeline 类型是 `PipelineTmaAsync`（`gather_A` 时换 `PipelineTmaCpAsync`）。

> 小贴士：`PipelineTmaAsync` 的「事务屏障 + `complete_tx::bytes` 信用」机制在 [u3-l5](u3-l5-pipeline-sync.md) 有详细讲解，本讲直接复用结论：producer 不必显式确认，mbarrier 靠 TMA 报告的字节数自动判定数据就绪。

#### 4.2.4 代码实践

> 实践目标：跟踪 cluster=(1,2) 与 (2,1) 下，A/B 谁被多播、谁各自加载。

1. 阅读 [quack/gemm_sm90.py:274-279](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L274-L279) 与 [quack/gemm_sm90.py:1045-1052](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L1045-L1052)。
2. 填表（待本地验证你的推断）：

| cluster_shape_mnk | num_mcast_ctas_a | num_mcast_ctas_b | A 是否多播 | B 是否多播 |
| --- | --- | --- | --- | --- |
| (1, 2, 1) | ? | ? | ? | ? |
| (2, 1, 1) | ? | ? | ? | ? |

3. **预期结果**：`(1,2,1)` → A 多播到 2 CTA、B 不多播；`(2,1,1)` → B 多播到 2 CTA、A 不多播。
4. 思考：为什么 SM90 配置空间 [quack/gemm_config.py:135](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_config.py#L135) 只给 `cluster=[(1,2),(2,1)]` 而不给 `(2,2)`？因为 cluster 总大小受硬件上限 4 约束（`(2,2)` 是 4，理论可行，但 `(1,2)/(2,1)` 已能让 A 或 B 二选一多播，简单且收益明确；autotune 在这两者间择优即可）。

#### 4.2.5 小练习与答案

**练习 1**：若把 cluster 设成 `(1,1,1)`，`mcast_size` 是多少？AB pipeline 的 consumer 到达计数会变成什么？
**答**：`mcast_size = 1+1-1 = 1`，`consumer_arrive_cnt = 1 * (tiled_mma.size/32)`，即每个 MMA warp 在本 CTA 到达一次即可——退化为普通单 CTA 流水线，没有多播。

**练习 2**：为什么 A 的多播维度是 `"M"` 却由 `cluster_shape_mnk[1]`（N 维）决定多播 CTA 数？
**答**：`"M"` 描述的是「TMA 在 cluster 的 M 方向上广播」（同一份 A 数据送到各 CTA）；而这些 CTA 是沿 cluster 的 N 维排列的（算不同 N 列、同一 M 行），所以参与多播的 CTA 数 = cluster 的 N 维大小 = `cluster_shape_mnk[1]`。两个维度描述的是不同的事。

---

### 4.3 WGMMA 主循环

#### 4.3.1 概念说明

「计算侧」由 **consumer warp**（MMA warpgroup）完成：从 SMEM 取 A/B 片段，发 WGMMA 累加进寄存器累加器 `acc`，一个 tile 的所有 k-tile 累加完后交给 epilogue。

`GemmSm90` 支持两条主循环路径，由 `mma_is_rs` 开关选择：

- **SS 路径（默认）：** A 直接从 SMEM 喂给 WGMMA（WGMMA 的 A 操作数源是 SMEM）。每个 k-tile 一发 `gemm`，靠 `wait_group` 等前面若干组 WGMMA 完成。
- **RS 路径：** A 先用 `ldmatrix` 从 SMEM 装进寄存器，再喂给 WGMMA（A 源是 RMEM）。这是 CUTLASS `rs_warpspecialized` 风格——关键巧思是把「装 block k+1」夹在「WGMMA(k)」和「WGMMA(k+1)」之间，用更深的 `wait_group` 保证寄存器读后写依赖。RS 路径在启用 `transform_a`（如反量化）时强制开启，因为变换需要自己控制 A 的 s2r 装载。

> 为什么需要 `wait_group`？WGMMA 是异步指令，发射后立即返回。连续发射多条 WGMMA 时，它们被编进 commit group；`wait_group(N)` 表示「等到只剩 N 个未完成组」。这是用 WGMMA 的「组」机制来管理寄存器/SMEM 的读后写依赖——producer 改写某个 stage 的 SMEM 前，必须保证 MMA 已经读完它。

#### 4.3.2 核心流程

**SS 主循环**（`mma`）伪代码：

```text
prologue:  发射前 k_pipe_mmas 个 WGMMA（zero_init=True 只在第一个）
mainloop:  for k in [prologue, k_tile_cnt):
             consumer_wait   # 等 AB pipeline 的 stage 数据就绪
             gemm(stage)     # WGMMA(k)
             wait_group(k_pipe_mmas)   # 只保留 k_pipe_mmas 个未完成组
             consumer_release          # 释放上一个 stage 给 producer
             advance
wait_group(0)  # 排空所有 WGMMA
```

**RS 主循环**（`mma_rs_interleaved`）伪代码（一个 tile 内有 `mma_k` 个 k16 块）：

```text
首 tile:  copy_block(stage, 0..mma_k-1) 与 wgmma_block(stage, 0..mma_k-1) 交叉
steady:   每个 tile：
            copy_block(下一 stage, 0)   # 预装下一 tile 的 slot 0
            for k in 0..mma_k-1:
                copy_block(本 stage, k+1)        # 装本 tile 的 block k+1
                wgmma_block(本 stage, k)         # 算 block k
                wait_group(mma_k - 2)            # 最深安全等待
                if k == mma_k-3: release(stage)  # 该 stage 的 WGMMA 都退休了
wait_group(0); release 最后 stage
```

`wait_group(mma_k - 2)` 的含义：装 block k+1 的寄存器，与读到它的 WGMMA(k+1) 之间，最多隔 `mma_k-2` 个组——保证覆盖（详见源码长注释）。

#### 4.3.3 源码精读

**WGMMA 的最底层封装**（一条/一组 WGMMA 指令）：

[quack/sm90_utils.py:151-176](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/sm90_utils.py#L151-L176) —— `@cute.jit def gemm`：`warpgroup.fence()` → 新建 mma_atom 并按 `zero_init` 设置 `ACCUMULATE`（首条不累加、清零，其余累加）→ `cutlass.range_constexpr` 展开所有 k → `cute.gemm` 发指令 → `commit_group` → `wait_group(wg_wait)`。注意它支持 `swap_AB`：把 A/B 对调后递归调用自己。

**构造 TiledMma（WGMMA 描述符）：**

[quack/sm90_utils.py:99-128](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/sm90_utils.py#L99-L128)：`make_tiled_mma` 把 dtype、主序、`atom_layout_mnk`、`tiler_mn=(64, tiler_n)`、`a_source`（SMEM 或 RMEM）编码成一个 `TiledMma`。`tiler_mn=(64, ...)` 正是 Hopper WGMMA 物理固定 M=64 的体现。

**_setup_tiled_mma（在 kernel 里真正组装 MMA）：**

[quack/gemm_sm90.py:379-405](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L379-L405) —— `make_trivial_tiled_mma(...)` 建出 MMA；当 `atom_layout_mnk[1] > 1`（沿 N 拆两个 WG）时，还要对 N 维做 `permutation_mnk`，让两个 WG 的累加器在 epilogue 的 SMEM 里相邻排列（注释解释：不重排的话 WG0/WG1 的累加器会分得很远）。

**MMA warp 的寄存器上限与片段切分：**

[quack/gemm_sm90.py:1180-1202](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L1180-L1202) —— `setmaxregister_increase(num_regs_mma)`（consumer 拿更多寄存器）；`is_tma_warp` 选出负责发 TMA store 的 warp；`thr_mma = tiled_mma.get_slice(...)` 按 warpgroup 取片段；`partition_fragment_ABC`（[quack/sm90_utils.py:219-247](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/sm90_utils.py#L219-L247)）建立累加器 `acc` 与 A/B 片段 `tCrA/tCrB`。

**主循环分发（SS vs RS）：**

[quack/gemm_sm90.py:1305-1328](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L1305-L1328) —— `not mma_is_rs` 走 `self.mma(...)`（SS），否则走 `self.mma_rs_interleaved(...)`（RS）。

**SS 主循环实现：**

[quack/gemm_sm90.py:1611-1676](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L1611-L1676) —— 注意几个要点：
- `mma_fn = partial(quack_sm90_utils.gemm_w_idx, tiled_mma, acc, tCrA, tCrB)`（在 [quack/gemm_sm90.py:1211](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L1211) 绑定），用 `A_idx/B_idx` 指明读哪个 stage；
- `peek_ab_full_status`（`consumer_try_wait`）是窥探优化，减少 wait 停顿；
- 主循环里 `wait_group(k_pipe_mmas)` 后才 `consumer_release`——保证 stage 的数据已被 WGMMA 读走才还给 producer；
- `fp8_slow_accum` 分支用 `acc_slow`（f32）做软件累加，规避 fp8 MMA 的精度损失。

**RS 主循环实现：**

[quack/gemm_sm90.py:1697-1829](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L1697-L1829) —— 头部那段长注释（[quack/gemm_sm90.py:1712-1737](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L1712-L1737)）是理解 RS 路径的最佳材料：它解释了「装 block k+1 夹在 WGMMA(k)/(k+1) 之间、每块一个 commit group、`wait_group(mma_k-2)`」的设计，以及 slot 0 在下一个 tile 的最后一块被重装的安全论证。

**canonical A 装载（RS 的 produce 接缝）：**

[quack/sm90_utils.py:250-287](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/sm90_utils.py#L250-L287) —— `canonical_a_load_s2r` 用 ldmatrix（16-bit）做 SMEM→RMEM 装载，返回 `copy_block(stage, b)`。`position_independent` 把 swizzle 吸收进指针，让 per-block 地址是线性 IMAD 链（ptxas 能 hoist），而非每条 LDSM 一次 `SHF+LOP3 XOR`——一个性能微优化。SM90 在 [quack/gemm_sm90.py:1499-1506](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L1499-L1506) 用 `position_independent=True` 调用它。

#### 4.3.4 代码实践

> 实践目标：对比 SS 与 RS 主循环的 `wait_group` 节拍，理解异步依赖管理。

1. 打开 SS 主循环 [quack/gemm_sm90.py:1646-1676](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L1646-L1676)。
2. 回答：SS 路径里 `wait_group` 的参数是 `k_pipe_mmas`（=1），而 RS 路径 [quack/gemm_sm90.py:1792](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L1792) 是 `mma_k - 2`。为什么 RS 要等得更深？
3. **预期理解（待本地验证）**：SS 里 A 在 SMEM，stage 的回收只依赖「WGMMA 读完 SMEM」——`wait_group(1)` 就能保证上一个 stage 退休。RS 里 A 在寄存器，装 block k+1 会**覆盖**正在被未完成 WGMMA 读的寄存器，必须等到「只剩 `mma_k-2` 个未完成组」，才能确保被覆盖的寄存器已无人再读。所以等待深度反映的是「数据载体从 SMEM 变成寄存器」后更紧的读后写依赖。
4. 观察 [quack/gemm_sm90.py:206-218](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L206-L218)：`transform_a is not None` 会强制 `mma_is_rs=True`，因为变换（反量化等）要自己掌控 A 的 s2r 装载；并且 RS 暂只支持 16-bit A。

#### 4.3.5 小练习与答案

**练习 1**：SS 主循环里 `zero_init` 在第一次 WGMMA 是 `True`，之后是 `False`，为什么？
**答**：`zero_init=True` 让 mma_atom 的 `ACCUMULATE` 关闭，相当于 `acc = A@B`（清零后写入）；之后 `ACCUMULATE=True` 是 `acc += A@B`。这保证一个 tile 的累加从零开始、逐 k-tile 累加（见 [quack/sm90_utils.py:168-172](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/sm90_utils.py#L168-L172)）。

**练习 2**：`mma_fn` 用 `A_idx=ab_read_state.index, B_idx=ab_read_state.index` 指定 stage，这和 `load_tma` 的 `smem_idx = producer_state.index` 是同一个索引吗？
**答**：是同一套 stage 索引，但分属 producer/consumer 两端。producer 写 `producer_state.index` 号 stage，consumer 读 `ab_read_state.index` 号 stage，二者经 pipeline 的 `(index, phase)` 状态机按 `//stages` 对齐（见 [u3-l5](u3-l5-pipeline-sync.md) 的 PipelineState）。

---

### 4.4 partition_for_epilogue 与 epilogue 分区

#### 4.4.1 概念说明

一个 tile 的 WGMMA 算完后，累加器 `acc`（在寄存器里）要经过 epilogue：可能加 `beta*C`、加 bias、做激活，最后转成输出 dtype、存回 SMEM 再用 TMA 存到 GMEM。整个 epilogue 是按 **子 tile（epi_tile）** 粒度推进的——一次处理 `acc` 的一小块。

`partition_for_epilogue` 解决的问题是：给定一个累加器/输出张量 `cT` 和 epilogue 的子 tile 形状 `epi_tile`，以及一个 tiled copy（描述「线程如何搬运这块」），把 `cT` 按 `epi_tile` 切成网格，再按线程切分，得到每个线程负责的寄存器/SMEM 片段。它是 epilogue 把「全局张量」和「线程私有片段」对应起来的桥梁。

SM90 的 epilogue 存储用 **`StMatrix`** 指令（warpgroup 协同的 SMEM store），把寄存器累加器转成 SMEM 里的输出布局，再由 TMA 存走。

#### 4.4.2 核心流程

```text
epilogue（每个输出 tile，基类 epilogue 驱动，按 epi 子 tile 循环）：
  1. store_setup:   epi_retile_acc(acc) → 把累加器重排成 epi 子 tile 视图
  2. 对每个 epi 子 tile:
       store_convert: load_acc_subtile → 取出该子 tile 的累加器到 tRS_rD
                      （此处叠加 alpha/beta*C/bias/激活等 EpiOp）
       store_r2s:     用 StMatrix 把 tRS_rD 写进 sD（SMEM）
       TMA store:     把 sD 的子 tile 异步存到 GMEM
```

`partition_for_epilogue` 用在「把输出张量 `mD`（或 C）按 epi_tile 切分并按线程分区」，得到 TMA store / SMEM load 需要的坐标片段。

#### 4.4.3 源码精读

**partition_for_epilogue：**

[quack/sm90_utils.py:131-148](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/sm90_utils.py#L131-L148) —— 三步：`thr_copy = tiled_copy.get_slice(tidx)` 取本线程的 copy 视图；`cT_epi = cute.flat_divide(cT, epi_tile)` 把张量按 epi_tile 切成 `(CPY_M, CPY_N, EPI_M, EPI_N)` 形式的网格；按 `reference_src` 选择 `partition_S`（按源布局分区，用于读）或 `partition_D`（按目的布局分区，用于写）。注释点明：当 `atom_layout_n>1`（两个 WG 沿 N 拆）时，N 维已被 `_setup_tiled_mma` 重排，使两 WG 的累加器在 epi SMEM 里相邻——`partition_for_epilogue` 与这套重排配套。

**累加器重排成 epi 子 tile：**

[quack/gemm_sm90.py:1831-1845](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L1831-L1845) —— `epi_retile_acc`：先用 `reshape_acc_to_frgA` 把 `acc` 整理成 `((2,2,2), MMA_M, MMA_N)`，再用 `flat_divide` 按 `epi_tile_shape` 切出 `(MMA_M/epi_M, MMA_N/epi_N, epi_M, epi_N)`，最后 `group_modes` + `retile` 得到每个线程的子 tile 累加器视图。

**StMatrix store atom：**

[quack/gemm_sm90.py:1865-1899](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L1865-L1899) —— `epilog_smem_copy_atom` 用 `StMatrix8x8x16bOp`（`num_matrices` 按 `epi_tile[1]%16` 选 4 或 2）构造 store atom；`epilog_smem_store_and_partition` 把它包成 `tiled_copy_r2s`，并 `partition_D(sD)` 得到 SMEM 写入片段、`partition_S` 得到寄存器片段 `tRS_rD`。`epi_r2s_pair_xor`（[quack/gemm_sm90.py:1847-1863](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L1847-L1863)）是 32-bit n-major 输出时的一个 bank-conflict 优化（pair-XOR 拆 STS）。

**epilogue 驱动本体（基类）：**

[quack/gemm_base.py:251-330](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L251-L330) —— 这是 u5-l1 讲过的 epilogue 驱动循环在 SM90 上的具体调用：D（`DStore`）与 aux 输出（`TileStore`）共享同一套 store 钩子（`store_setup/store_convert/store_r2s`）。SM90 把上面构造好的 `tiled_copy_r2s`、`tRS_rD`、`copy_D` 等通过 `epi_fn` 传给它（见 [quack/gemm_sm90.py:1400-1426](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L1400-L1426)）。

#### 4.4.4 代码实践

> 实践目标：跟踪一个累加器 tile 如何被切成 epi 子 tile 并存回 GMEM。

1. 从 [quack/gemm_sm90.py:1381-1386](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L1381-L1386) 的 `epilog_smem_store_and_partition` 与 `epi_retile_acc` 开始。
2. 读 [quack/sm90_utils.py:131-148](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/sm90_utils.py#L131-L148)，回答：`partition_for_epilogue` 的 `reference_src` 参数什么时候用 `partition_S`、什么时候用 `partition_D`？
3. **预期理解**：读源（把张量搬进寄存器，如加载 C 做加法）用 `partition_S`（按源布局切）；写目的（把累加器存进 SMEM/GMEM）用 `partition_D`。`partition_for_epilogue` 用 `reference_src` 告诉它这次切分是为读还是为写，从而选对线程—元素映射。
4. （可选，需 SM90 GPU）运行一次真实 GEMM 并在 epilogue 处加 `cute.printf` 打印 `epi_coord` 与 `tRS_rD` 形状，观察子 tile 循环顺序——**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`epi_tile` 比 `cta_tile` 小，为什么不直接一次性存整个 tile？
**答**：寄存器容量有限——整个 tile 的累加器可能占满寄存器，但转成输出 dtype 并做 StMatrix store 需要按小块（`StMatrix8x8x16bOp` 的粒度）搬运；按 epi 子 tile 循环还能让 TMA store 与下一子 tile 的计算重叠。

**练习 2**：`_compute_tile_shape_or_override` 里（[quack/gemm_sm90.py:2117-2126](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L2117-L2126)），为什么 `atom_layout_n=2`（沿 N 拆两 WG）时要把 `epi_tile_m` 设成 64 而非 128？
**答**：注释说得很清楚——累加器在寄存器里是「先沿 N 后沿 M」迭代，而 epilogue 默认「先沿 M 后沿 N」。若 `epi_tile_m=128`，epilogue 会先走完整个 M 再换 N，与累加器的 N-major 迭代顺序冲突；设成 64 让两者在小块内对齐，避免改写整个 epilogue。

---

## 5. 综合实践

> 本任务把本讲四个模块串起来：对比 SM90 的 **coop 与 pingpong**，解释 **cluster=(1,2)/(2,1)** 如何影响 tile 分配与多播，并指出 **dynamic persistent 在 SM90 默认关闭** 的原因。

**步骤 1：跑一次真实 SM90 GEMM（若你有 H100）。**

```bash
pytest tests/test_gemm_functional.py -x        # 需要 SM90+
```

该测试（[tests/test_gemm_functional.py:47-52](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_gemm_functional.py#L47-L52)）用 `torch.ops.quack.gemm` 与 Python 封装 `gemm` 做数值比对，验证 functional parity。若无 GPU，跳过运行、只做下面的源码阅读。

**步骤 2：coop vs pingpong。** 读 [quack/gemm_sm90.py:256-296](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L256-L296) 与 pingpong 屏障 [quack/gemm_sm90.py:1919-1933](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L1919-L1933)，写出两种模式的差异：

| 维度 | coop | pingpong |
| --- | --- | --- |
| atom_layout | 沿 M/N 拆 | 固定 (1,1,1) |
| mma_warp_groups | prod(atom_layout) | 2（固定） |
| tile 归属 | 所有 WG 合力算 1 个 tile | 2 WG 交替算相邻 2 个 tile |
| epilogue SMEM 复用 | 单 tile | 两 WG 的输出轮流复用同一 SMEM（需屏障交接） |

pingpong 的好处：一个 WG 算 MMA 时，另一个 WG 写上一个 tile 的 epilogue，**MMA 与 epilogue 在时间上重叠**，掩盖 epilogue 延迟（见 [quack/gemm_sm90.py:1442-1448](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L1442-L1448) 的注释——pingpong 下两 WG 写不同 tile 到同一 SMEM，故要等 SMEM 读完后才通知下一 WG）。

**步骤 3：cluster=(1,2)/(2,1) 的 tile 分配。** 结合 4.2，写出：

- `(1,2)`：cluster 沿 N 配对，两 CTA 算同一 M 行的两个 N 列，共享 A（A 多播），各算各的 B。
- `(2,1)`：cluster 沿 M 配对，两 CTA 算同一 N 列的两个 M 行，共享 B（B 多播），各算各的 A。

二者都是 2-CTA cluster（总大小 2 ≤ 4），autotune 在它们之间择优（[quack/gemm_config.py:135](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_config.py#L135)）。

**步骤 4：为什么 SM90 默认关闭 dynamic persistent。** 读三处：

1. 配置默认值 [quack/gemm_config.py:152](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_config.py#L152)：`is_dynamic_persistent=False, # default to not use dynamic persistent on SM90`。
2. SM90 dynamic persistent 的代价 [quack/gemm.py:860-863](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm.py#L860-L863)：它要求在 **GMEM 里放一个 `tile_count_semaphore`**（原子计数器做工作偷取）。
3. 调度模式选择 [quack/gemm_base.py:776-784](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L776-L784)：SM90（arch=90）用不了 CLC（CLC 是 `arch>=100` 的硬件机制，[quack/gemm_sm90.py:193-194](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L193-L194) 断言），所以 SM90 唯一的「动态」选项是 `PersistenceMode.DYNAMIC`——基于 GMEM 原子计数器的工作偷取。

**结论（待本地验证你的表述）**：Hopper 没有硬件 CLC，动态持久化只能靠 GMEM 原子计数器做工作偷取，这带来原子竞争与额外 GMEM 信号开销；而稠密 GEMM 的静态光栅化调度（`PersistenceMode.STATIC`，带 L2 swizzle）本身负载均衡良好，工作偷取的收益盖不过原子开销，故默认关闭。动态持久化只在变长序列（varlen，M 因 batch 而异、负载天然不均）时才值得开启——那时 SM90 才用 `DYNAMIC` 调度。

**步骤 5（自检）。** 用一句话回答：若把同一个 `(M,N,K)` 的 GEMM 分别用 coop `tile=(256,128)` 与 pingpong `tile=(128,128)` 跑，谁更可能赢？为什么？——**预期**：无定论，取决于形状与 K 长度（K 长 → pingpong 的 epilogue 重叠收益大；K 短、tile 大 → coop 的算术强度高）。这正是 QuACK 用 autotune 在配置空间里搜索的原因。

## 6. 本讲小结

- `GemmSm90` 是 Hopper 持久化 GEMM 内核，继承 `GemmTmaBase`，复用基类的 epilogue 驱动、split-K、tile 调度，自己实现 TMA/WGMMA mainloop 与配置。
- 配置核心是 `atom_layout_mnk`：coop 模式沿 M（或 N）拆多个 WGMMA atom、`mma_warp_groups∈{1,2,3}`；pingpong 固定 `(1,1,1)`、两个 WG 交替算相邻 tile。`threads_per_cta = (mma_warp_groups+1)*128`，多出的一个 WG 是 producer。
- 输入侧由 producer warp 用 TMA 加载 A/B 进 AB pipeline 多级缓冲；cluster=(1,2) 多播 A、(2,1) 多播 B，靠事务屏障的 `complete_tx::bytes` 信用与按多播 peer 数放大的 consumer 到达计数管理同步。
- 计算侧有 SS 与 RS 两条 WGMMA 主循环：SS 的 A 来自 SMEM、`wait_group(1)`；RS 的 A 来自寄存器、把装载夹在 WGMMA 之间、`wait_group(mma_k-2)`。`transform_a` 强制走 RS。
- `partition_for_epilogue` 用 `flat_divide` + `partition_S/D` 把张量按 epi 子 tile 切分并按线程分区；epilogue 用 `StMatrix` 把累加器存进 SMEM 再由 TMA 存走，整个驱动循环在基类。
- SM90 默认关闭 dynamic persistent：Hopper 无硬件 CLC，动态只能靠 GMEM 原子计数器工作偷取，开销高于静态光栅化调度的负载均衡收益；varlen 场景例外。

## 7. 下一步学习建议

- **横向对比 Blackwell：** 读 [u5-l3](u5-l3-gemm-sm100.md)（SM100）与 `quack/gemm_sm100.py`，重点看 tcgen05 MMA、TMEM 累加器、2-CTA 模式与 CLC 持久化——理解为什么 SM100 没有 pingpong（硬件原生提供 MMA↔epilogue 重叠）。
- **纵向深挖调度：** 读 [u3-l4](u3-l4-tile-scheduler.md) 与 `quack/tile_scheduler.py`，把本讲的 `PersistenceMode` 四模式（NONE/STATIC/DYNAMIC/CLC）与 L2 swizzle 的几何彻底搞懂。
- **流水线细节：** 读 [u3-l5](u3-l5-pipeline-sync.md) 与 `quack/pipeline.py`，确认 `PipelineTmaAsync` 的事务屏障握手与本讲的 AB pipeline 完全对应。
- **epilogue 全貌：** 本讲只用了最基础的 DStore epilogue；读 [u6](u6-l1-epi-mixin-lifecycle.md) 系列理解可组合 epilogue（rotary、量化输出等）如何在同一套 store 钩子上扩展。
