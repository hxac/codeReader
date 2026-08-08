# tile_scheduler 持久化内核调度

## 1. 本讲目标

QuACK 的 GEMM 内核并不让「一个线程块只算一个输出 tile」，而是用一套**调度器（scheduler）**决定每个线程块该算哪些 tile。本讲聚焦 `quack/tile_scheduler.py`，学完后你应该能够：

- 说清楚**非持久化**与**持久化**内核在 tile 分配上的区别，以及为什么需要调度器。
- 看懂一个线性「工作索引（work index）」如何被**反线性化（delinearize）**成 `(pid_m, pid_n, split_idx, batch_idx)`，并理解 L2 swizzle（光栅化 + 蛇形）的作用。
- 区分四种持久化模式 `NONE / STATIC / DYNAMIC / CLC`，理解静态轮转、动态原子偷取、硬件 CLC 工作偷取三者的取舍。
- 读懂 `VarlenMTileScheduler` 如何在 M 维变长（每段序列长度不同）的情况下，用 `cu_seqlens` 做 warp 协作扫描，把 work index 映射到正确的段，并跳过空序列。

本讲是后续 GEMM 设备侧讲义（u5）的调度地基——所有 SM 的 GEMM 内核主循环都在调用这里定义的 `get_current_work` / `advance_to_next_work`。

## 2. 前置知识

阅读本讲前，请先具备以下认知（由前置讲义建立）：

- **CuTe-DSL 编程模型**（u1-l4）：`@cute.jit` / `@cute.kernel`、`const_expr` 编译期分支、`cutlass.range_constexpr` 编译期展开。调度器里几乎所有 `if` 都包在 `const_expr(...)` 里——因为持久化模式是编译期常量，不同模式会特化出不同 cubin。
- **TiledCopy 与 tile 概念**（u3-l1）：输出矩阵被切成一个个 `tile`（如 128×256），多个 tile 再聚合成一个 `cluster`（如 2×1）协同计算。
- **GEMM 基本结构**：`D = alpha * A @ B + beta * C`，其中 A 是 (M,K)、B 是 (K,N)、D 是 (M,N)。本讲反复出现 `pid_m`（M 方向的 tile 编号）、`pid_n`（N 方向的 tile 编号）。

几个本讲用到的术语：

- **grid / block / cluster**：grid 是一次 launch 的全部线程块；block 即 CTA（线程块）；cluster 是 SM90+ 引入的「线程块集群」，多个 CTA 共享分布式共享内存、可协作。
- **work index**：把所有待算的 tile 排成一维后得到的线性下标。调度器的核心职责就是 `work index → (pid_m, pid_n, split_idx, batch_idx)`。
- **cu_seqlens**：变长场景下「累计序列长度」数组，形如 `[0, len_0, len_0+len_1, ...]`，相邻两项之差就是某段序列的长度。来自 FlashAttention 系列约定。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [quack/tile_scheduler.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/tile_scheduler.py) | 调度器本体。定义 `PersistenceMode` 枚举、`TileScheduler` 基类（稠密矩形调度）、`TriangularTileScheduler`（三角矩阵调度）、`VarlenMTileScheduler`（变长 M 调度），以及 AllGather 相关参数。 |
| [quack/varlen_utils.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/varlen_utils.py) | 变长张量的「管理层」`VarlenManager`：根据 `cu_seqlens` 把每段序列在 A/B/D/索引张量上偏移到正确的子区域，并提供 `len_m` / `len_k` 查询。 |
| [quack/gemm_base.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py) | GEMM 各 SM 共享基类。`get_scheduler_class` 与 `get_scheduler_arguments` 在主机侧选定调度器类、构造参数、选定持久化模式。 |
| [quack/gemm_sm100.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm100.py) | Blackwell GEMM 内核。其持久化主循环是调度器最典型的「消费者」，演示 `get_current_work` / `advance_to_next_work` 如何驱动循环。 |
| [tests/test_linear_varlen_m.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_linear_varlen_m.py) | 变长 M 的端到端测试，含 `run_lowlevel_varlen_m_gemm`，是本讲代码实践的依据。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：

1. **TileScheduler 持久化调度**——调度器要解决的根本问题与基类骨架。
2. **动态 / 静态 / CLC 调度模式**——`PersistenceMode` 四种取值的机制与取舍。
3. **Varlen 变长调度**——变长 M 维下的网格过量分配与 `cu_seqlens` 扫描。

### 4.1 TileScheduler 持久化调度

#### 4.1.1 概念说明

先理解两种内核启动方式的区别。

**非持久化（non-persistent）**：让 grid 的大小等于输出 tile 的总数，每个 CTA 恰好算一个 tile，算完即退出。优点是简单；缺点是当 tile 数远超 GPU 上的 SM 数时，硬件要排队反复给同一批 SM 派发 CTA，**派发（launch / wave）开销**和**尾部负载不均**都会拖慢。

**持久化（persistent）**：launch 固定数量的 CTA（通常等于「一波能填满 SM 的 cluster 数」`max_active_clusters`），让它们**循环**把所有 tile 算完。这样：

- 每个持久化 CTA 自己维护一个游标，依次领任务，省去硬件反复派发。
- 可以让多个 tile 共享已经加载到 L2 / 寄存器 / SMEM 的数据（通过控制扫描顺序）。
- 在 tile 计算量不均（如变长序列）时，可用**工作偷取（work stealing）**让忙完的 CTA 去抢别人的活，天然负载均衡。

调度器就是这个「决定每个 CTA 该算哪个 tile」的组件。它的接口极简：主机侧算出 grid 大小（`get_grid_shape`），设备侧把一个线性 work index 翻译成 tile 坐标（`_delinearize_work_idx`），并在持久化循环里反复 `get_current_work` → 算 → `advance_to_next_work`。

#### 4.1.2 核心流程

一个 tile 的坐标由四元组刻画，封装在 `WorkTileInfo` 中（`tile_idx` 即 `(pid_m, pid_n, split_idx, batch_idx)`）：

```
work_idx (标量)
   │  _delinearize_work_idx
   ▼
(pid_m, pid_n, split_idx, batch_idx, is_valid)
```

稠密矩形调度（无 split-K）的线性化规则是「先 batch，再 (M,N) 平面」：

\[
\text{work\_idx} = \text{batch} \cdot \underbrace{(\text{ncluster}_m \cdot \text{ncluster}_n)}_{\text{num\_clusters\_per\_problem}} + \text{cluster\_id\_in\_problem}
\]

反解就是先 `divmod(work_idx, num_clusters_per_problem)` 得到 `(batch, cluster_id_in_problem)`，再把 `cluster_id_in_problem` 经 **L2 swizzle** 翻成 `(cid_m, cid_n)`，最后乘以 cluster 形状得到 CTA 级别的 `pid_m, pid_n`。Split-K 时把工作空间再乘以 `num_split_k`，让 split 索引成为最快变化分量（同一 tile 的各 split 时间相邻，便于 L2 复用与信号量同步）。

**L2 swizzle**（光栅化 + 蛇形）的动机：朴素地按行/列扫描输出 tile 会让某一块 B（或 A）反复被驱逐出 L2。QuACK 把相邻的 `group_size` 个 tile 聚成一个 group，group 内沿「快维」扫描，group 之间走**蛇形（serpentine / boustrophedon）**——偶数 group 正向、奇数 group 反向，使相邻 group 在物理上首尾相接，从而同一个 B 分块能在两个相邻 group 间命中 L2。

#### 4.1.3 源码精读

调度结果的载体 `WorkTileInfo`，它特意被设计成「对 split-K 的泛化」——`split_idx` 在 `num_split_k==1` 时为静态 `None`，保持非 split 路径与 DSL 原版一致：

[quack/tile_scheduler.py:L87-L96](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/tile_scheduler.py#L87-L96) — `WorkTileInfo` 的注释说明 `tile_idx` 即 `(pid_m, pid_n, split_idx, batch_idx)`。

主机侧把「问题形状 + 调度偏好」烘焙成设备可用的 `Params`。注意 `group_size`、`num_groups_regular`、`num_clusters_in_group` 等 swizzle 几何都在主机侧一次性算好，并封装成 `FastDivmod`（魔法数除法，见 u3-l3 提到的设备侧高效除法）下发：

[quack/tile_scheduler.py:L316-L329](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/tile_scheduler.py#L316-L329) — 计算 `group_size`、尾段 `group_size_tail`、`num_groups_regular` 与 `num_clusters_in_group = group_size * ncluster_slow`，把 swizzle 几何预算好。

主机侧确定 grid 大小的 `get_grid_shape`。注意它对**持久化与否**有两条截然不同的路径：非持久化时 grid 直接覆盖全部 cluster；持久化时 grid 被「夹」到一波能跑满 SM 的 cluster 数：

[quack/tile_scheduler.py:L468-L491](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/tile_scheduler.py#L468-L491) — `persistence_mode` 为 `NONE/CLC` 时返回覆盖全部 cluster 的 grid；否则用 `min(num_ctas_in_problem, max_active_clusters * num_ctas_per_cluster)` 得到持久化 CTA 数，grid 的 z 维 = `cluster_l * num_persistent_clusters`。

把线性 `cluster_id_in_problem` 翻成 `(cid_m, cid_n)` 的 swizzle 核心代码：

[quack/tile_scheduler.py:L499-L522](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/tile_scheduler.py#L499-L522) — 先 `divmod` 出 `group_id` 与 `id_in_group`；组内再 `divmod(group_size_fdd)` 得到「慢维 / 组内快维」；奇数 group 把慢维翻转实现蛇形；最后按 `raster_order`（AlongM/AlongN）交换快慢维。注释 `# CTA Swizzle to promote L2 data reuse` 点明其目的。

完整反线性化 `TileScheduler._delinearize_work_idx`，把 work index 拆成 tile 坐标：

[quack/tile_scheduler.py:L562-L634](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/tile_scheduler.py#L562-L634) — `is_valid` 时先按模式取出 `cluster_id_in_problem` 与 `bidz_`（NONE/CLC 直接用 block 坐标；STATIC/DYNAMIC 用 `divmod(work_idx, num_clusters_per_problem_fdd)`，见 L571-L584）；随后调用 swizzle、cluster→CTA，得到 `(pid_m, pid_n, split_idx, batch_idx)`。Split-K 分支把 `work_idx_tile = work_idx // num_split_k` 单独拆出，保证 split 时间相邻。

主机侧如何「选定调度器类」——稠密用 `TileScheduler`，变长 M 用 `VarlenMTileScheduler`：

[quack/gemm_base.py:L759-L761](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L759-L761) — `get_scheduler_class` 据 `varlen_m` 在两类调度器间二选一。

#### 4.1.4 代码实践

**实践目标**：在源码层面跟踪一次「work index → tile 坐标」的完整解码，亲手算一个小例子，确认你理解了线性化与 swizzle。

**操作步骤**（源码阅读型）：

1. 打开 `quack/tile_scheduler.py`，定位 `Params.create`（[L272-L349](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/tile_scheduler.py#L272-L349)）。
2. 假设一个稠密 GEMM：M 方向有 4 个 cluster（`ncluster_m=4`），N 方向有 4 个 cluster（`ncluster_n=4`），`cluster_shape_mnk=(1,1,1)`，`group_size=2`，`raster_order=AlongM`，单 batch、无 split-K。
3. 手算 `num_clusters_per_problem = 4*4 = 16`，`num_clusters_in_group = group_size * ncluster_slow = 2 * 4 = 8`，`num_groups_regular = ncluster_fast // group_size = 4 // 2 = 2`。
4. 取 `work_idx = 10`，按 `_delinearize_work_idx`（STATIC/DYNAMIC 分支，无 split-K）反解：`divmod(10, 16) → (batch=0, cluster_id_in_problem=10)`。再进 `_swizzle_cta(10)`：`divmod(10, 8) → group_id=1, id_in_group=2`；`group_id=1` 是奇数 → 蛇形翻转慢维。

**需要观察的现象**：

- `group_id=1 < num_groups_regular=2`，走规则组分支：`divmod(2, group_size=2) → cid_slow=1, cid_fast_in_group=0`。
- 蛇形：`ncluster_slow = ncluster_n = 4`（AlongM 时慢维是 N），翻转后 `cid_slow = 4-1-1 = 2`。
- `cid_fast = group_id * group_size + cid_fast_in_group = 1*2 + 0 = 2`；AlongM → `cid_m=2, cid_n=2`。

**预期结果**：`work_idx=10` 解码到 `(pid_m=2, pid_n=2)`，即输出平面正中央那个 tile；而朴素行优先光栅化下 `work_idx=10` 本应是 `(2, 2)`…… 这里恰好相同是个巧合，换 `work_idx=9`（规则组 `divmod(9,8)→(1,1)` → `divmod(1,2)→(cid_slow=0,cid_fast_in_group=1)` → 翻转 `cid_slow=3`，`cid_fast=3` → `(3,3)`）就能看到蛇形带来的「跳跃」。

> 待本地验证：上面 `work_idx=9/10` 的手算，建议你在读懂 `_swizzle_cta` 后再核一遍。重点是体会「group 内连续、group 间蛇形」的扫描轨迹，而非死记具体坐标。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `num_clusters_in_group` 要定义成 `group_size * ncluster_slow`，而不是 `group_size * ncluster_fast`？

> **答案**：group 沿「快维」方向聚拢 `group_size` 个 tile，因此一个 group 在快维上跨 `group_size`、在慢维上跨全部 `ncluster_slow`，覆盖的 tile 总数正是 `group_size * ncluster_slow`。用它做 `divmod` 才能把线性 id 正确切成「第几个 group」和「group 内第几个」。

**练习 2**：`grid_may_exceed_work: bool = False`（[L254](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/tile_scheduler.py#L254)）这个类属性的含义是什么？哪类调度器会改它？

> **答案**：它表示「launch 的 grid 可能比真实工作量大（存在 padding 工作索引）」。只有变长调度器 `VarlenMTileScheduler`（4.3 节）会把它设为 `True`，因为变长场景下 tile 总数在 launch 时未知，只能按上界过量分配。对精确 grid 的调度器，退役排空（`cancel_pending_tail`）是死代码。

### 4.2 动态 / 静态 / CLC 调度模式

#### 4.2.1 概念说明

持久化内核需要回答一个关键问题：**下一个该算哪个 tile？** QuACK 用 `PersistenceMode` 枚举给出四种答案，分别对应不同的硬件特性与负载特征：

| 模式 | 含义 | 「下一个 work index」从哪来 | 典型场景 |
|------|------|---------------------------|---------|
| `NONE` | 非持久化，每个 CTA 一个 tile | `blockIdx`（硬件派发） | 小问题、SM80 部分路径 |
| `STATIC` | 持久化 + 轮转 | `work_idx += num_persistent_clusters`（寄存器递推） | 稠密、负载均匀 |
| `DYNAMIC` | 持久化 + 原子工作偷取 | 全局原子计数器 `tile_count_semaphore` | 变长、负载不均（SM90） |
| `CLC` | 硬件工作偷取 | SM100+ 的 Cluster Launch Control `try_cancel` | Blackwell 持久化默认 |

**STATIC vs DYNAMIC 的本质差别**：STATIC 在编译期就知道总 tile 数，每个持久化 CTA 用 `idx += stride` 轮转，游标是寄存器里的递推，可被流水线提前计算；但负载完全静态——若某些 tile 比 others 贵（变长序列），先算完的 CTA 只能空转。DYNAMIC 改用一个全局原子计数器：算完一个 tile 就 `atomic_inc` 抢下一个，谁先空谁先抢，天然均衡，代价是一次跨 SM 的原子操作 + 它带来的串行化。变长 M 的负载极度不均，正是 DYNAMIC 的主场。

**CLC（Cluster Launch Control）** 是 SM100 引入的硬件特性：硬件本身维护一个待派发 cluster 池，持久化 cluster 退役前发 `try_cancel` 请求「偷走」池中尚未启动的 cluster 的工作，响应由硬件**多播**进 cluster 内每个 CTA 的 SMEM。它把 DYNAMIC 的「软件原子计数器」换成「硬件池」，省掉全局原子的串行化，是 Blackwell 持久化的默认路径。

#### 4.2.2 核心流程

模式选择发生在主机侧 `gemm_base.GemmBase.get_scheduler_arguments`，规则清晰可记：

```
若 not is_persistent           → NONE
否则若 arch >= 100 且 use_clc  → CLC
否则若有 tile_count_semaphore  → DYNAMIC   # 仅 SM8x/SM90 动态路径用信号量
否则                           → STATIC
```

设备侧，work index 的**来源**由 `_fetch_next_work_idx` 按模式分派；work index 的**去向**（写回 SMEM 供 cluster 内其它 CTA 读）由 `write_work_tile_to_smem` 与 `advance_to_next_work` 编排。NONE/CLC 模式下 work index 直接来自 block 坐标（不需要软件递推）；STATIC/DYNAMIC 模式下 work index 是持久化计数器，需要 scheduler warp 单独推进并经 SMEM 广播。

CLC 的 producer/consumer 分工值得单独记：

- **Producer**（cluster 中 CTA 0 的 scheduler warp）：等所有 consumer 释放 stage → 给每个 CTA 的 full barrier 上膛 `expect_tx(16)` → 发一次多播 `issue_clc_query`。
- **Consumer**（cluster 中每个 CTA 的每个 consumer warp）：`consumer_wait` 等硬件把 16 字节响应多播进本 CTA 的 SMEM → 解码 `clc_response` → **本地**做 swizzle（不再由 scheduler warp 解码一次）。
- **退役排空** `cancel_pending_tail`：当 cluster 在一个「解码出的幻影 tile」（padding）上退役时，串行发 `try_cancel` 排空尾部 padding。

#### 4.2.3 源码精读

`PersistenceMode` 枚举，注意每个值注释里的语义：

[quack/tile_scheduler.py:L31-L39](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/tile_scheduler.py#L31-L39) — `NONE/STATIC/DYNAMIC/CLC` 四值；`CLC` 注释说明它依赖硬件多播 `try_cancel` 响应，work index 来自被取消 cluster 的 x 坐标而非 z 维持久化计数器。

主机侧的模式选择逻辑：

[quack/gemm_base.py:L776-L784](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L776-L784) — 四分支选定 `persistence_mode`：非持久化→`NONE`；SM100+ 且开 CLC→`CLC`；有信号量→`DYNAMIC`；否则→`STATIC`。

设备侧取「下一个 work index」的分派——这是 STATIC 与 DYNAMIC 差别的核心：

[quack/tile_scheduler.py:L895-L916](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/tile_scheduler.py#L895-L916) — `STATIC`：`return self._current_work_idx + num_persistent_clusters`（纯寄存器递推，可提前算）；`DYNAMIC`：lane 0 对 `tile_count_semaphore` 做带模上界的 `atomicrmw INC`（稠密，知道总量）或 `atomic_add(1)`（变长 M，总量未知），再 `shuffle_sync` 广播给全 warp。注释指出变长 M 时 `problem_shape_ncluster_mnl[0] is None`，故用 `atomic_add` 而非 `atomic_inc`，且内核结束时须把信号量复位为 0。

持久化「主循环消费调度器」的典型写法（Blackwell GEMM）：

[quack/gemm_sm100.py:L1312-L1313](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm100.py#L1312-L1313) 与 [quack/gemm_sm100.py:L1505-L1506](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm100.py#L1505-L1506) — `while work_tile.is_valid_tile:` 内部先算当前 tile，末尾 `tile_scheduler.advance_to_next_work()` 再 `work_tile = tile_scheduler.get_current_work()` 推进。`is_valid_tile` 为假即退出循环——这就是「持久化 CTA 循环到活干完」的统一抽象。

`get_current_work` 对 NONE 与持久化的区分读取：

[quack/tile_scheduler.py:L669-L692](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/tile_scheduler.py#L669-L692) — `NONE` 模式直接用初始解码（无推进）；持久化模式则 `consumer_wait` 等 scheduler warp 把下一个 tile 坐标写进 `sched_smem`，读取 4 个 Int32（含 `is_valid`），并在 cluster>1 时 `fence_view_async_shared` 防 async 代理与 generic 代理读竞态。

CLC 的 producer 端（仅 cluster 内 CTA 0 的 scheduler warp 调用）：

[quack/tile_scheduler.py:L749-L767](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/tile_scheduler.py#L749-L767) — `acquire` → 用 `mbarrier_arrive_and_expect_tx(mbar, 16, lane)` 给每个 CTA 的 full bar 上膛 → `elect_one` 发一次 `issue_clc_query(multicast=True)`。响应由硬件直接多播进各 CTA 的 SMEM，无需软件 STAS 重广播。

CLC 退役排空 `cancel_pending_tail` 的门控——只有在 CLC 且 grid 过量时才执行，且只有「解码出的幻影」退役才给预算：

[quack/tile_scheduler.py:L843-L855](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/tile_scheduler.py#L843-L855) — `const_expr(persistence_mode == CLC and grid_may_exceed_work)` 才进入；预算 `budget = min(CLC_DRAIN_MAX_CANCELS, grid_total - current_work_idx)`，但若 `_phantom_retire == 0` 则预算清零。注释详述了 2026 年 7 月那次「 invalild 响应触发排空 → 误取消真实 cluster」的静默损坏 bug，是理解为何要「幻影门控」的关键。

#### 4.2.4 代码实践

**实践目标**：对比 `is_dynamic_persistent=True/False`（即 STATIC vs DYNAMIC）在 tile 分配上的差别，并观察动态路径对信号量的依赖。

**操作步骤**（源码阅读 + 本地运行型）：

1. 打开测试 `tests/test_gemm_split_k.py` 或 `tests/test_linear_varlen_m.py`，找到构造 `tile_count_semaphore` 的位置：

   [tests/test_linear_varlen_m.py:L71-L76](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_linear_varlen_m.py#L71-L76) — 仅当 `dynamic_persistent and device_capacity == 9`（SM90）才分配 `torch.zeros(1, int32)` 作为信号量。这正是 4.2.3 中 `scheduler_uses_semaphore = is_dynamic_persistent and device_capacity[0] == 9` 的来源。

2. 读 `run_lowlevel_varlen_m_gemm`：

   [tests/test_linear_varlen_m.py:L77-L92](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_linear_varlen_m.py#L77-L92) — 把 `persistent=True` 与 `is_dynamic_persistent=dynamic_persistent` 透传给 `quack.gemm`。

3. 若本机有 SM90 GPU，运行：
   ```bash
   pytest tests/test_linear_varlen_m.py -x -k "dynamic_persistent" -p no:randomly
   ```
   分别对 `dynamic_persistent=False` 与 `True` 各跑一次（`-k` 过滤参数化用例）。

**需要观察的现象**：

- `dynamic_persistent=False`（STATIC）：tile 分配是确定性的轮转，`work_idx` 是 `+= stride` 递推，不需要信号量，`tile_count_semaphore=None`。
- `dynamic_persistent=True`（DYNAMIC，SM90）：进入 `_fetch_next_work_idx` 的 DYNAMIC 分支，每个 CTA 算完一个 tile 就对 `tile_count_semaphore` 做一次原子自增「抢号」。若负载不均（变长序列），先空的 CTA 会抢到更多号，从而负载均衡。
- 两条路径的**数值结果都应与参考一致**（测试用 `(out - ref).abs().max()` 校验）——调度顺序不同不影响 GEMM 的数值正确性。

**预期结果**：两种模式都通过数值校验。> 待本地验证：若你手上不是 SM90，DYNAMIC 路径不会被触发（信号量为 `None`，模式会回退）；可在 SM100 上观察 CLC 路径取而代之。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `cancel_pending_tail` 必须在「解码出的幻影（decoded phantom）」上退役才执行，而不能在「invalid 响应」退役时执行？

> **答案**：CLC 的 `try_cancel` 在 GPU 竞争下会**伪失败（spurious invalid）**，远在池子空之前就返回 invalid。若在 invalid 退役时排空，会误以为池子空了而取消大量**真实**待派发 cluster（2026 年 7 月的静默损坏正是此因）。而「解码出的幻影」=一次**有效**授予但 work index 落在 padding 区，授予顺序的单调性保证了此时真实工作必已全部离开池子，排空才安全。

**练习 2**：STATIC 模式下 `work_idx` 是寄存器递推、可提前一拍计算；DYNAMIC 模式下却做不到。为什么 CLC 引入后，变长 M 的 scan 缓存（4.3 节）变得尤其重要？

> **答案**：STATIC 的 `idx += stride` 在 SASS 调度器眼里是已知量，能和循环体的 stall 重叠隐藏。CLC 的偷取响应要等 `mbarrier` 消费 + `fence.proxy.async` 之后才存在，无法提前提升，于是每次偷取都要在 fetch 处现场做一次 cu_seqlens 扫描（约 800–1100 周期），在 tcgen05 issue 流里砸出 per-tile 气泡（源码注释实测 3–9% e2e 损失）。scan 缓存让「落在同一段内的偷取」免去扫描，把 CLC 偷取的解码成本拉回到稠密调度的水平。

### 4.3 Varlen 变长调度

#### 4.3.1 概念说明

变长（variable-length，varlen）场景指 batch 的每一「段」是一个长度不同的序列——典型例子是 packed attention / packed linear：把一个 mini-batch 里所有序列的 token 拼成一根无 padding 的长向量（M 维），用 `cu_seqlens` 标记每段边界，从而省掉 padding 的无效计算。

难点在于：**launch 时根本不知道一共有多少个输出 tile**。每段长度 `len_i` 决定它在 M 方向占 `ceil(len_i / tile_M)` 个 tile，但 `len_i` 在 host 上虽然可算，把这些 tile 摊平成一维后，设备侧反解 work index 时却需要扫描 `cu_seqlens` 才能知道「这个 work index 属于哪一段」。更麻烦的是，若用 CLC 过量分配 grid（4.3.2 的上界），还会存在 padding tile，必须能识别并丢弃。

`VarlenMTileScheduler` 解决三件事：

1. **网格过量分配**：用 `total_m` 与段数 `L` 算一个 tile 总数的**紧上界**，按它 launch；多余的 CTA 会解码出 invalid tile 并退出（配合 `cancel_pending_tail` 排空）。
2. **work index → (batch_idx, cluster_id_in_problem)**：用 warp 协作扫描 `cu_seqlens`，找到 work index 落在哪一段、以及在该段内的偏移。
3. **跳过空序列**：长度为 0 的段 `ceil(0/tile_M)=0`，在扫描中自然贡献 0 个 tile，从而被「跨过」。

`VarlenManager`（`varlen_utils.py`）则负责另一面：拿到 `batch_idx` 后，把每段在 A/B/D/索引张量上偏移到正确的子区域，并屏蔽越界行（ragged tensor）。

#### 4.3.2 核心流程

**网格上界的推导**。设 `block_size = tile_M * cluster_M`，段数为 `num_batch`，总长度 `total_m = Σ len_i`。每段占 `ceil(len_i / block_size)` 个 M-cluster。由于 `ceil(a/B) = ⌊(a + B - 1)/B⌋`，且

\[
\sum_{i} \left\lceil \frac{\text{len}_i}{B} \right\rceil \le \sum_i \frac{\text{len}_i + B - 1}{B} = \frac{\text{total\_m} + \text{num\_batch}\cdot(B-1)}{B},
\]

即

\[
\text{total\_clusters\_m\_max} = \left\lfloor \frac{\text{total\_m} + \text{num\_batch}\cdot(B-1)}{B} \right\rfloor.
\]

这个上界在每段 `len_i ≡ 1 (mod B)` 时取到（每段都浪费 `B-1` 个元素），是仅凭 `(total_m, num_batch)` 能给出的最紧上界。源码注释明确：「no smaller grid is safe without per-batch seqlens」。

**work index 反解（扫描）**。把所有段在 M 方向的 tile 数前缀和想象成一堵「刻度尺」：`problems_end_tile` 累计到 `next_tile_idx` 所在的刻度，就找到了对应的 `batch_idx`。由于段数可能很多，扫描用 **warp 协作**：32 个 lane 各算一段的 tile 数，再做 warp 内前缀和（`warp_prefix_sum`），一次推进 31 段，再用 `vote_ballot_sync + popc` 在 warp 内二分定位到具体段。这就是 `_delinearize_work_idx` 里 `while problems_end_tile <= next_tile_idx` 循环的实质。

**跳过空序列**：在 `_get_num_m_blocks` 中，`seqlen = next_cu_seqlen - cur_cu_seqlen` 若为 0，则 `ceil_div(0, block_size) = 0`，该段贡献 0 个 tile，扫描的累加器原地不动，自然跳过。

**扫描缓存**：CLC 下每次偷取都会触发解码，若每次都重新扫描 `cu_seqlens`，长串依赖链（gmem 加载 + warp 前缀和 + ballot + shuffle）会成为 per-tile 气泡。`VarlenMTileScheduler` 缓存上一次解码所在的「段窗口」`[_num_work_idx_before_cur_batch, _cur_batch_end)`，若新 work index 仍落在窗口内，直接复用，免去扫描。

#### 4.3.3 源码精读

`VarlenMTileScheduler` 标记自己会过量分配 grid：

[quack/tile_scheduler.py:L1257-L1258](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/tile_scheduler.py#L1257-L1258) — `grid_may_exceed_work: bool = True`，是唯一打开此标志的调度器，使 `cancel_pending_tail` 在 CLC+varlen 下生效。

网格上界计算：

[quack/tile_scheduler.py:L1359-L1377](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/tile_scheduler.py#L1359-L1377) — `total_clusters_m_max = (total_m + num_batch * (block_size - 1)) // block_size`，注释说明该紧上界由「每段 ≡1 (mod block)」这种对抗性长度达到。NONE/CLC 时按此上界 launch，多余 CTA 解码出 invalid；持久化时用 `min(max_active_clusters, total_clusters_max)`。

warp 协作扫描的一段计算 `_get_num_m_blocks`——这里是「跳过空序列」发生的地方：

[quack/tile_scheduler.py:L1420-L1435](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/tile_scheduler.py#L1420-L1435) — 每个 lane 取一段，`cur_cu_seqlen = cu_seqlens_m[batch_idx]`（越界段取 0），`seqlen = next - cur`，返回 `ceil_div(seqlen, block_size)`；`batch_idx >= num_batch` 或 lane 末尾返回 0。`seqlen=0` 段返回 0，扫描累加器不变 → 跳过空序列。

完整反线性化 `VarlenMTileScheduler._delinearize_work_idx`，含扫描主循环与缓存：

[quack/tile_scheduler.py:L1464-L1525](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/tile_scheduler.py#L1464-L1525) — `need_scan` 判定是否需要扫描（work index 落在缓存窗口外）；扫描循环里用 `_get_num_m_blocks` + `warp_prefix_sum` + `shuffle_sync` 累计 `problems_end_tile`，直到覆盖 `next_tile_idx`；随后 `vote_ballot_sync + popc` 在 warp 内定位段 `batch_idx`，并算出该段 tile 起点 `num_work_idx_before_cur_batch` 与窗口结束 `cur_batch_end`，写回缓存字段。注释（L1465-L1478）详述了为何 CLC 下这套缓存能省 3–9% e2e。

变长张量管理层 `VarlenArguments` 与段长度查询：

[quack/varlen_utils.py:L15-L25](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/varlen_utils.py#L15-L25) — `VarlenArguments` 携带 `cu_seqlens_m/k`、可选 `mAIdx`（gather）、可选 `mCuTilesM`（per-sequence M-tile 前缀，供需要按段索引的 epilogue 使用）。

[quack/varlen_utils.py:L95-L99](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/varlen_utils.py#L95-L99) — `len_m(batch_idx) = cu_seqlens_m[b+1] - cu_seqlens_m[b]`：变长时从累计序列长度差读段长，否则用静态 `_len_m_static`。这是调度器与 GEMM 主循环拿到 `batch_idx` 后查询「这段有多长」的统一入口。

#### 4.3.4 代码实践

**实践目标**：用一组具体的 `cu_seqlens` 手算变长调度的网格上界与 work index 反解，亲眼看到「空序列被跳过」。

**操作步骤**（手算 + 源码对照型）：

1. 设 `tile_M = 128, cluster_M = 1`，故 `block_size = 128`。三段序列长度 `len = [256, 0, 96]`（第 2 段为空），`total_m = 352`，`num_batch = 3`，`cu_seqlens_m = [0, 256, 256, 352]`。
2. 按上界公式算网格：
   \[
   \text{total\_clusters\_m\_max} = \lfloor (352 + 3 \times 127) / 128 \rfloor = \lfloor 733 / 128 \rfloor = 5.
   \]
3. 算各段真实 tile 数：`ceil(256/128)=2`，`ceil(0/128)=0`，`ceil(96/128)=1`，合计真实 M-cluster = 3（记它们的 work index 区间：段 0 → [0,2)，段 2 → [2,3)）。注意段 1 贡献 0，被跳过。
4. 设 `ncluster_n = 1`，于是真实 tile 总数 = 3，但 grid 按 5 过量分配。work index `0,1` 落在段 0；`2` 落在段 2；`3,4` 越界 → invalid（持久化下由 `cancel_pending_tail` 排空）。
5. 对照源码 `_get_num_m_blocks`（[L1420-L1435](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/tile_scheduler.py#L1420-L1435)）确认段 1 的 `seqlen = 256 - 256 = 0` → 返回 0，扫描累加器不动，等价于「跨过」空段。

**需要观察的现象**：

- 网格上界 5 ≥ 真实 3，且无更小值能覆盖所有「每段 ≡1 (mod 128)」的最坏情形。
- `cu_seqlens` 里相邻相等的两项（`[256, 256]`）即表示空段，对应 tile 数为 0。
- 真实可运行验证：跑 `pytest tests/test_linear_varlen_m.py -x -k "gather_A0 or gather_AFalse"`（见 [L126-L156](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/tests/test_linear_varlen_m.py#L126-L156)），它会随机生成 `seq_lens ∈ [96, 320)` 的若干段，与 PyTorch 参考做数值比对。注意测试里段长下限 96 < tile_M 128，恰好会触发「一段不足一个 tile」的边界。

**预期结果**：手算的 tile 分配与「`cu_seqlens` 相邻相等 → 空段跳过」一致；测试数值校验通过。> 待本地验证：手算例子可在脑中/纸上完成；可运行部分需 SM90/SM100 GPU。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `VarlenMTileScheduler` 在 NONE/CLC 模式下也敢按上界过量分配 grid，而稠密 `TileScheduler` 不行？

> **答案**：稠密调度器的 tile 总数在编译期精确已知，grid 就等于 tile 总数，每个 work index 都有效。变长调度器 launch 时只知道 `(total_m, num_batch)`，无法知道确切 tile 数（取决于各段长度对 `block_size` 取整的浪费），只能按紧上界分配，必然产生 padding work index。它靠 `grid_may_exceed_work=True` + `_delinearize_work_idx` 把越界 work index 标记为 `is_valid=False`（CLC 下再由 `cancel_pending_tail` 排空尾部）来消化这些 padding，稠密调度器没有这套机制。

**练习 2**：`VarlenManager.len_m(batch_idx)`（[varlen_utils.py:L95-L99](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/varlen_utils.py#L95-L99)）和 `VarlenMTileScheduler._get_num_m_blocks`（[tile_scheduler.py:L1420-L1435](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/tile_scheduler.py#L1420-L1435)）都在读 `cu_seqlens`，它们的分工是什么？

> **答案**：`_get_num_m_blocks` 是**调度器**侧的——它需要「这段有几个 M-cluster」来把一维 work index 映射到正确的段（扫描刻度尺）。`VarlenManager.len_m` 是**内核主体**侧的——调度器给出 `batch_idx` 后，GEMM 主循环用它查「这段的真实 M 长度」，进而偏移 A/D 张量（`offset_batch_A` / `offset_batch_epi`）、屏蔽越界行、决定 K 维循环长度。一个负责「算哪个 tile」，一个负责「这个 tile 的数据在哪、算多少」。

## 5. 综合实践

把三个最小模块串起来，做一次「调度器全链路追踪」：

**任务**：选定一个稠密 GEMM 配置（如 M=N=K=1024，bf16，`tile=(128,128)`，`cluster=(2,1)`），在源码里完成下表，并与变长配置对比。

| 维度 | 稠密 STATIC | 稠密 DYNAMIC(SM90) | 变长 M (CLC) |
|------|------------|-------------------|--------------|
| 调度器类 | `TileScheduler` | `TileScheduler` | `VarlenMTileScheduler` |
| `PersistenceMode` | ? | ? | ? |
| grid 如何确定（`get_grid_shape`） | ? | ? | ? |
| 「下一个 work index」来源 | ? | ? | ? |
| work index → tile 坐标的核心函数 | ? | ? | ? |
| 是否可能 launch 过量 grid | ? | ? | ? |

**步骤**：

1. 在 `gemm_base.get_scheduler_arguments`（[L776-L784](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L776-L784)）填出三列的 `PersistenceMode`。
2. 在 `TileScheduler.get_grid_shape`（[L468-L491](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/tile_scheduler.py#L468-L491)）与 `VarlenMTileScheduler.get_grid_shape`（[L1359-L1377](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/tile_scheduler.py#L1359-L1377)）里填出 grid 算法。
3. 在 `_fetch_next_work_idx`（[L895-L916](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/tile_scheduler.py#L895-L916)）与 `_get_current_work_clc`（[L702-L747](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/tile_scheduler.py#L702-L747)）里填出 work index 来源。
4. 在 `_delinearize_work_idx`（稠密 [L562-L634](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/tile_scheduler.py#L562-L634)、变长 [L1464-L1525](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/tile_scheduler.py#L1464-L1525)）里填出反线性化函数。
5. 用 `grid_may_exceed_work`（[L254](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/tile_scheduler.py#L254) 与 [L1258](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/tile_scheduler.py#L1258)）回答最后一行。

**预期**：填完这张表，你就能对任意一个 GEMM launch 说出「它用了哪个调度器、grid 多大、每个 CTA 怎么领下一个 tile、过量 grid 怎么消化」——这正是读懂后续 GEMM 设备侧讲义（u5）主循环的钥匙。

## 6. 本讲小结

- **持久化内核**用固定数量的 CTA 循环算完所有 tile，调度器的职责是把线性 work index 翻译成 `(pid_m, pid_n, split_idx, batch_idx)`；主机侧 `get_grid_shape` 定 grid，设备侧 `_delinearize_work_idx` 定坐标。
- **L2 swizzle**（光栅化 + group + 蛇形）通过控制 tile 扫描顺序提升 L2 复用，几何在主机侧一次性算好并封装成 `FastDivmod` 下发。
- **四种持久化模式**：`NONE`（一 CTA 一 tile）、`STATIC`（寄存器轮转 `idx += stride`）、`DYNAMIC`（全局原子计数器工作偷取，SM90 变长主场）、`CLC`（SM100 硬件 `try_cancel` 多播工作偷取，Blackwell 默认）。
- 模式选择在 `gemm_base.get_scheduler_arguments`：`not persistent → NONE`、`arch≥100 且开 CLC → CLC`、`有信号量 → DYNAMIC`、`否则 STATIC`；SM8x/SM90 动态路径才用 `tile_count_semaphore`。
- **变长 M 调度** `VarlenMTileScheduler` 因 launch 时不知 tile 总数，按紧上界 `(total_m + num_batch*(block-1))//block` 过量分配 grid，并用 warp 协作扫描 `cu_seqlens` 把 work index 映射到正确的段；空序列因 `ceil(0/block)=0` 被自然跳过。
- CLC 下变长 scan 缓存（`[_num_work_idx_before_cur_batch, _cur_batch_end)` 窗口）把同段内偷取的解码成本拉回稠密水平，实测省 3–9% e2e；`cancel_pending_tail` 仅在「解码幻影」退役时排空，以防误取消真实 cluster。

## 7. 下一步学习建议

- **进入 GEMM 设备侧**：本讲只讲「算哪个 tile」。下一单元 u5-l1（`GemmBase` 共享主循环）会展示主循环拿到 `tile_coord_mnkl` 后如何驱动 A/B 加载、MMA 累加与 epilogue，把调度器与计算流水线缝合起来。
- **同步原语**：本讲的持久化推进、CLC 多播、`cancel_pending_tail` 都依赖 mbarrier / named barrier。建议结合 u3-l5（异步流水线与同步原语）理解 `consumer_wait` / `arrive_and_expect_tx` / `fence_view_async_shared` 背后的协作语义。
- **继续阅读源码**：`cancel_pending_tail`（[tile_scheduler.py:L780-L883](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/tile_scheduler.py#L780-L883)）的注释是一份珍贵的「踩坑史」，记录了 CLC 伪失败导致的静默损坏与各种被测量否决的优化尝试，是学习「性能调优如何用数据驱动决策」的绝佳案例。
- **Split-K**：本讲多次提到 `num_split_k`。若想理解 split index 如何参与工作空间线性化、以及三种合并模式，可预习 u8-l3（Split-K 归约）。
