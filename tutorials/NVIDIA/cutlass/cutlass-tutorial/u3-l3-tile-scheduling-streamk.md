# Tile Scheduling 与 Stream-K

## 1. 本讲目标

本讲承接 u2-l7（CUTLASS 3.x 通用模型）中「`TileScheduler` 决定 CTA↔tile 分配」那一句承诺，把调度器（tile scheduler）这块单独拆开讲透。读完本讲你应当能够：

- 说清 **tile scheduler 在 3.x 内核中扮演的角色**——它如何把输出 tile 分配给 CTA/cluster，并驱动持久化（persistent）内核的主循环。
- 理解 **持久化内核调度**：每个 CTA 不只算一个 tile，而是循环领取工作（`fetch_next_work`），以及 raster order 如何照顾 L2 局部性。
- 掌握 **Stream-K 分块策略**：为什么「尾部波（tail wave）」会浪费算力，Stream-K 又如何把浪费的 K 维工作量重新摊到所有 SM 上。
- 理解 **跨 CTA 归约（fixup）**：当一个输出 tile 被多个 CTA 协作计算时，部分累加器如何写入 workspace、如何用屏障同步、最终由谁完成 epilogue。

本讲只聚焦 SM90（Hopper）的调度器实现，但 SM100（Blackwell）的调度器建立在同一套抽象之上（见 `tile_scheduler.hpp` 中的 `TileSchedulerSelector`）。

## 2. 前置知识

在进入源码前，先用一张「思维快照」对齐几个概念（不展开，细节交给后续模块）。

### 2.1 从「一个 CTA 算一个 tile」说起

CUTLASS 3.x 把一次大 GEMM 的输出矩阵 \(M \times N\) 切成若干 **输出 tile**，每个 tile 的大小由 `TileShape`（CTA tile）决定。于是输出 tile 总数为：

\[
\text{output\_tiles} = \left\lceil \frac{M}{T_M} \right\rceil \times \left\lceil \frac{N}{T_N} \right\rceil \times L
\]

其中 \(T_M, T_N\) 是 CTA tile 的 M、N 维，\(L\) 是 batch 维。

最朴素的调度是「**一个 CTA 算一个完整的输出 tile**」——该 CTA 在内部把整个 K 维循环一遍，独立算出这块 tile 的结果。这正是 u1-l6 讲的 2.x `device::Gemm` 的做法（grid 按 \(\lceil M/T_M\rceil \times \lceil N/T_N\rceil\) 切分）。

### 2.2 wave（波）与「尾部波浪费」

GPU 有固定数量的 SM（Hopper H100 为 132 个，本讲举例时用 80 这个便于整除的数）。一个 **wave（波）** 指的是「一波就能同时驻留在 SM 上的 CTA 集合」，其大小约等于 SM 数（记为 `ctas_per_wave`）。

- 当 `output_tiles` 是 `ctas_per_wave` 的整数倍时，每个 wave 都满载，所有 SM 都在干活。
- 当不是整数倍时，**最后一个 wave（尾部波）只有少数 tile**，其余 SM 空转整个 wave 的时间。这就是 **wave quantization（波数量化）浪费**。

举例：`output_tiles = 90`，`ctas_per_wave = 80`。
- 数据并行调度：wave 0 有 80 个 CTA 满载；wave 1 只有 10 个 tile，**70 个 SM 空转**（尾部波里 87.5% 算力闲置）。

Stream-K 的全部动机就是消除这种尾部浪费。

### 2.3 持久化内核（persistent kernel）

3.x 内核默认是 **持久化**的：启动时只发射约等于 SM 数的 CTA（而不是 `output_tiles` 个），这些 CTA **常驻 SM**，算完一个 tile 后不退出，而是循环领取下一个 tile（`fetch_next_work`），直到所有 tile 算完。持久化降低了启动开销，也是 Stream-K 得以实现的前提。其主循环骨架在 4.2 节精读。

### 2.4 K 维可拆分性

一个输出 tile 的 K 维被切成 `k_tiles_per_output_tile` 个 K-tile 迭代。只要这个数足够大（CUTLASS 要求至少 `min_iters_per_sk_unit_ = 8`），就可以把 K 维拆给多个 CTA 分别算一部分，再把部分和加起来。这是 Stream-K 与 Split-K 的共同基础。

> 关键术语回顾（来自 u2-l7）：**collective / mainloop / epilogue / tile scheduler / dispatch policy**。本讲的主角就是其中的 `tile scheduler`。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `include/cutlass/gemm/kernel/tile_scheduler.hpp` | 调度器 **tag**（`PersistentScheduler`/`StreamKScheduler`…）与 `TileSchedulerSelector`，把 (tag, 架构) 映射到具体调度器类 |
| `include/cutlass/gemm/kernel/static_tile_scheduler.hpp` | `StaticPersistentTileScheduler`——CRTP 基类，实现「数据并行 + 持久化」调度 |
| `include/cutlass/gemm/kernel/sm90_tile_scheduler.hpp` | `PersistentTileSchedulerSm90`——SM90 默认（数据并行持久化）调度器，继承上面的基类 |
| `include/cutlass/gemm/kernel/sm90_tile_scheduler_stream_k.hpp` | `PersistentTileSchedulerSm90StreamK`——SM90 Stream-K 调度器，本讲核心 |
| `include/cutlass/gemm/kernel/tile_scheduler_params.h` | 调度器参数结构体，**包含 Stream-K 的全部判定数学**（`get_num_sk_tiles` 等） |
| `include/cutlass/gemm/kernel/sm90_gemm_warpspecialized_cooperative.hpp` | 一个真实的 SM90 warp-specialized 内核，展示调度器如何驱动 producer/consumer 主循环 |
| `examples/49_hopper_gemm_with_collective_builder/49_collective_builder.cu` | 官方示例，演示如何在 `GemmUniversal` 模板参数里切换 `StreamKScheduler` |

## 4. 核心概念与源码讲解

### 4.1 tile scheduler 的角色：谁来分 tile？

#### 4.1.1 概念说明

在 3.x 的三段式模型（kernel 外壳 + collective mainloop + collective epilogue）里，内核外壳 `GemmUniversal` 自身不算乘加，它把「**哪个 CTA 算哪个（些）输出 tile、算 K 的哪一段、是否做 epilogue**」这三件事完全外包给 **tile scheduler**。可以这样理解调度器的接口契约：

- **输入**：CTA 在网格里的线性编号 `linear_idx`（由 `blockIdx` 得到）、问题形状、硬件信息。
- **输出**：一个 `WorkTileInfo`，描述本 CTA 这次要算的 `(M_idx, N_idx, K 起点, K tile 数, L_idx)` 等信息。
- **运行期行为**：提供 `initial_work_tile_info`（领第一份活）、`fetch_next_work`（干完领下一份）、`compute_epilogue`（这份活要不要写回 D）、`fixup`（要不要把部分和并入 workspace）等钩子。

不同调度器只要遵守同一套钩子接口，就可以无缝替换——这就是策略可插拔的关键。

#### 4.1.2 核心流程

调度器如何被「选中」是一个编译期的映射：

```
TileSchedulerTag（如 StreamKScheduler）
   + ArchTag（如 Sm90）
   + TileShape + ClusterShape
        │  TileSchedulerSelector 偏特化
        ▼
   具体调度器类（如 PersistentTileSchedulerSm90StreamK）
```

运行期，内核入口构造一个调度器对象，进入主循环：

```
scheduler(params)
work = scheduler.initial_work_tile_info()
while work.is_valid():
    k_count = get_work_k_tile_count(work)
    k_start = get_work_k_tile_start(work)
    # mainloop 跑 k_count 个 K-tile（producer 搬数据 / consumer 发 MMA）
    scheduler.fixup(work, accumulators)        # 若需要，归约跨 CTA 部分和
    if scheduler.compute_epilogue(work):
        epilogue.store(...)                     # 写回 D
    work = scheduler.fetch_next_work(work)      # 持久化：领下一份
```

#### 4.1.3 源码精读

调度器的「tag」是一组空结构体，仅用于编译期分派：

定义 5 个调度器标签，`StreamKScheduler` 即其中之一：
[include/cutlass/gemm/kernel/tile_scheduler.hpp:L49-L58](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/tile_scheduler.hpp#L49-L58)

`TileSchedulerSelector` 的主模板对未匹配的 (tag, 架构) 组合直接 `static_assert` 报错，避免误用；这是「一个标签一个调度器」的编译期查表：

[include/cutlass/gemm/kernel/tile_scheduler.hpp:L78-L89](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/tile_scheduler.hpp#L78-L89)

默认（`void`）与 `PersistentScheduler` 都映射到 `PersistentTileSchedulerSm90`（数据并行持久化）：

[include/cutlass/gemm/kernel/tile_scheduler.hpp:L91-L105](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/tile_scheduler.hpp#L91-L105)

而 `StreamKScheduler` + `Sm90` 专门映射到带模板参数的 `PersistentTileSchedulerSm90StreamK<TileShape, ClusterShape>`：

[include/cutlass/gemm/kernel/tile_scheduler.hpp:L130-L143](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/tile_scheduler.hpp#L130-L143)

注意 Stream-K 调度器需要知道 `TileShape` 与 `ClusterShape`（要算 K-tile 数、要做 cluster 内的 K 局部性），所以它是类模板；而数据并行调度器不需要，是个普通类。

#### 4.1.4 代码实践（源码阅读型）

**目标**：建立「tag → 调度器类」映射的全局观。

**步骤**：
1. 打开 `include/cutlass/gemm/kernel/tile_scheduler.hpp`。
2. 找到所有 `TileSchedulerSelector<...>` 的偏特化，数一数 SM90 与 SM100 各支持哪几种 tag。
3. 回答：在 SM120（`arch::Sm120`）上，`StreamKScheduler` 映射到哪个类？`GroupScheduler` 映射到哪个？

**预期结果**：SM120 的 `StreamKScheduler` → `PersistentTileSchedulerSm100StreamK`（见该文件 Sm120 段），`GroupScheduler` → `PersistentTileSchedulerSm90Group`。这说明调度器可跨架构复用，命名虽带 Sm90/Sm100，但实际由 `Selector` 决定。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `PersistentTileSchedulerSm90StreamK` 是类模板（带 `TileShape`/`ClusterShape`），而 `PersistentTileSchedulerSm90` 不是？

**参考答案**：数据并行调度器只需把 `linear_idx → (M,N,L)`，不需要知道 K 维细节；而 Stream-K 调度器要在运行期计算「每个 stream-K 单元算多少个 K-tile」、判断 cluster 内能否 multicast 复用操作数等，这些都依赖具体的 tile/cluster 形状，因此必须把形状作为模板参数固化进类型。

---

### 4.2 持久化内核调度与 raster order

#### 4.2.1 概念说明

「持久化（persistent）」是 3.x 调度器的底座：内核启动的 CTA 数 ≈ SM 数，每个 CTA 算完一个 tile 后通过 `advance_to_next_work` 把自己的线性游标 **加上整个 grid 的大小**，从而跳到「属于自己但晚若干波才该算」的下一个 tile，循环往复直到游标越界（`is_valid()` 返回 false）。

`StaticPersistentTileScheduler` 是 CRTP 基类（`Subclass` 提供 swizzle/raster 细节），实现了数据并行持久化的通用逻辑。SM90 的 `PersistentTileSchedulerSm90` 继承它并填上 `get_work_idx_m_and_n`。

**raster order（扫描顺序）** 决定输出 tile 被分配的先后。它影响 L2 cache 局部性：相邻 CTA 尽量复用 A 或 B 的同一块。CUTLASS 的启发式是「**N 维 tile 多就沿 M 扫描，否则沿 N 扫描**」（见 4.2.3 的 `get_rasterization_order`）。

#### 4.2.2 核心流程

数据并行持久化调度的运行期逻辑：

```
# 构造（每个 CTA 执行一次）
current_work_linear_idx_ = 把 blockIdx 映射成一个线性编号
total_grid_size_        = gridDim.x * gridDim.y * gridDim.z   # 持久化 CTA 总数

# 取活
get_current_work_for_linear_idx(idx):
    if idx >= blocks_per_problem_:        # 越界 → 无效
        return invalid
    (l, remainder) = divmod_batch(idx)    # 拆出 batch
    (m, n)         = get_work_idx_m_and_n(remainder, ...)   # 加 swizzle 的反变换
    return WorkTileInfo{m, n, l}

# 持久化前进
advance_to_next_work():
    current_work_linear_idx_ += total_grid_size_   # 关键：跨波领活
```

注意 `WorkTileInfo`（数据并行版）只有 `(M_idx, N_idx, L_idx)`，**没有 K 拆分信息**——因为每个 CTA 把整个 K 维算完。

#### 4.2.3 源码精读

CRTP 基类定义，子类 `Subclass` 提供具体的反 swizzle 变换：

[include/cutlass/gemm/kernel/static_tile_scheduler.hpp:L47-L48](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/static_tile_scheduler.hpp#L47-L48)

数据并行版的 `WorkTileInfo`：只有 M/N/L 三个坐标加一个有效性标记，没有 K 拆分、没有「是否最终分片」的概念：

[include/cutlass/gemm/kernel/static_tile_scheduler.hpp:L55-L84](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/static_tile_scheduler.hpp#L55-L84)

注意其中 `is_final_split` 恒为 `true`、`reduction_subtile_idx()` 恒为 `-1`——这正反映了「数据并行下每个 tile 只由一个 CTA 独立算完，不存在拆分与归约」。

构造函数把 `blockIdx` 折算成线性游标，并记录持久化 grid 总大小：

[include/cutlass/gemm/kernel/static_tile_scheduler.hpp:L136-L151](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/static_tile_scheduler.hpp#L136-L151)

持久化循环的灵魂——一行：游标加上整个 grid 大小，于是 CTA \(i\) 下一轮去算 CTA \(i + \text{grid\_size}\) 的活：

[include/cutlass/gemm/kernel/static_tile_scheduler.hpp:L190-L194](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/static_tile_scheduler.hpp#L190-L194)

数据并行下每个 tile 都独立完成，所以 `compute_epilogue` 恒真、`fixup` 是空操作、`continue_current_work` 恒假、不需要单独归约：

[include/cutlass/gemm/kernel/static_tile_scheduler.hpp:L402-L435](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/static_tile_scheduler.hpp#L402-L435)

而 `get_work_k_tile_count` 返回整段 K 迭代空间（确认「一个 CTA 算完整 K」）：

[include/cutlass/gemm/kernel/static_tile_scheduler.hpp:L445-L452](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/static_tile_scheduler.hpp#L445-L452)

SM90 默认调度器声明为持久化且非动态，且明确声明 **不需要任何 workspace**（`get_workspace_size` 返回 0），这是它和 Stream-K 的一大区别：

[include/cutlass/gemm/kernel/sm90_tile_scheduler.hpp:L40-L51](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm90_tile_scheduler.hpp#L40-L51)

[include/cutlass/gemm/kernel/sm90_tile_scheduler.hpp:L137-L149](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm90_tile_scheduler.hpp#L137-L149)

raster order 的 L2 启发式——`tiles_n > tiles_m` 时沿 M 扫（否则沿 N）：

[include/cutlass/gemm/kernel/tile_scheduler_params.h:L329-L354](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/tile_scheduler_params.h#L329-L354)

内核里调度器如何驱动 producer（搬数据）与 consumer（发 MMA）的循环。下面是 producer warp group 的循环，注意 `while (work_tile_info.is_valid())` 与末尾的 `fetch_next_work`：

[include/cutlass/gemm/kernel/sm90_gemm_warpspecialized_cooperative.hpp:L358-L370](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm90_gemm_warpspecialized_cooperative.hpp#L358-L370)

每个 CTA 从 `WorkTileInfo` 取出本份活的 K-tile 数与起点，喂给 mainloop；这正是调度器与 mainloop 的衔接点：

[include/cutlass/gemm/kernel/sm90_gemm_warpspecialized_cooperative.hpp:L382-L384](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm90_gemm_warpspecialized_cooperative.hpp#L382-L384)

#### 4.2.4 代码实践（源码阅读型）

**目标**：亲眼看到「持久化」就是「游标 += grid_size」。

**步骤**：
1. 打开 `static_tile_scheduler.hpp`，定位 `advance_to_next_work`（L190 附近）。
2. 再看构造函数里 `total_grid_size_` 的赋值（L147）。
3. 跟踪 `fetch_next_work`（L264 附近）如何先调 `continue_current_work`，再 `advance_to_next_work`，最后 `get_current_work`。
4. 思考：如果 `grid_size` 等于 `blocks_per_problem_`（即不持久化、一个 CTA 一个 tile），这个循环退化为「算一次就退出」。

**预期结果**：你会确认持久化的本质就是「启动 CTA 数 < tile 数时，每个 CTA 用 `+= grid_size` 跨波领取多个 tile」。

#### 4.2.5 小练习与答案

**练习 1**：为什么数据并行调度器的 `get_workspace_size` 返回 0，而 Stream-K 需要一块 workspace？

**参考答案**：数据并行下每个输出 tile 由唯一一个 CTA 独立算完，没有跨 CTA 的部分和需要保存。Stream-K 下一个 tile 可能被多个 CTA 协作计算，各自只能算出 K 的一段对应的部分累加器，必须把这些部分和暂存到全局 workspace，最后再归约——因此需要 workspace（存放部分累加器与同步屏障）。

**练习 2**：`get_rasterization_order` 在 `tiles_n > tiles_m` 时返回 `AlongM`，直觉上似乎「反了」。请解释为什么这其实有利于 L2。

**参考答案**：沿 M 扫描时，相邻的 CTA 共享同一列 B（同一 N 坐标、不同 M 坐标），B 的这一块在 L2 里被多个 CTA 复用；当 N 维 tile 数较多时，沿 M 扫描能让更多 CTA 命中 L2 中已缓存的 B 块，从而减少 gmem 重读。选择「沿较短维跨步、沿较长维连续」是常见的 L2 局部性策略。

---

### 4.3 Stream-K 分块策略：消除尾部波浪费

#### 4.3.1 概念说明

Stream-K 的核心思想可以一句话概括：**别让尾部波里的 SM 闲着——把那些「凑不满一波」的 tile 的 K 维工作量，均匀摊到所有 stream-K 单元上。**

它和数据并行、Split-K 的关系：

| 策略 | 谁算一个 tile | K 维是否拆分 | 何时用 |
|------|--------------|-------------|--------|
| 数据并行 (DataParallel) | 单个 CTA 独立算完整 tile | 否 | tile 数是 SM 数整数倍、无尾部浪费 |
| Split-K | `splits` 个 CTA 各算 K 的一段，再全局归约 | 是（均匀拆 K） | tile 数 < SM 数（欠占用），靠拆 K 增加并行 |
| Stream-K | 若干 CTA 各算「跨 tile 边界的连续 K 区间」，再归约 | 是（连续摊 K） | 有尾部波浪费、且 K 足够大 |

Stream-K 与 Split-K 都拆 K、都需要归约，区别在于 **Split-K 把每个 tile 的 K 均匀切成 `splits` 段**（归约量与 tile 数成正比，开销大），而 **Stream-K 只对「尾部波」那部分 tile 拆 K**，其余满波 tile 仍走数据并行（无需归约），从而在「消除尾部浪费」与「少做归约」之间取平衡。

#### 4.3.2 核心流程

Stream-K 的判定与分配由主机端 `stream_k_heuristic → select_decomposition_mode` 完成，关键量定义如下：

\[
\begin{aligned}
\text{full\_waves} &= \left\lfloor \frac{\text{output\_tiles}}{\text{ctas\_per\_wave}} \right\rfloor \\
\text{total\_waves} &= \left\lceil \frac{\text{output\_tiles}}{\text{ctas\_per\_wave}} \right\rceil \\
\text{dp\_waves} &= \max(\text{full\_waves} - 1,\ 0) \\
\text{dp\_tiles} &= \text{dp\_waves} \times \text{ctas\_per\_wave} \\
\text{sk\_tiles} &= \text{output\_tiles} - \text{dp\_tiles}
\end{aligned}
\]

解读：
- 若 `full_waves == total_waves`（无量化）→ `sk_tiles = 0`，纯数据并行。
- 否则把 **最后两波** 的 tile 划为 stream-K（`sk_tiles`），前面满波走数据并行（`dp_tiles`）。
- **启发式闸门**：若尾部 tile 数超过半波（`2 * tail_tiles >= ctas_per_wave`），说明尾部本来就够满，不值得拆，直接返回 0 走数据并行。
- **最小迭代闸门**：若 `k_tiles_per_output_tile <= 8`（`min_iters_per_sk_unit_`），K 太短拆不动，也返回 0。

随后 `sk_tiles` 由 `sk_units`（约等于 SM 数）个 stream-K 单元分担。每个单元算一段 **连续的 K-tile 区间**，这段区间可能 **跨越输出 tile 边界**——于是产生了「多个单元协作算同一 tile」的情况，需要 4.4 节的 fixup。

判定最终的分解模式（仍可能在 Stream-K 与 Split-K 间切换）：当 `sk_units` 是 `sk_tiles` 的整数倍时退化为 Split-K（更规整、归约更简单），否则保持 Stream-K。

运行期，Stream-K 的 `WorkTileInfo` 比数据并行多了 `K_idx`（本单元在该 tile 内的 K 起点）、`k_tile_count`（本次算几个 K-tile）、`k_tile_remaining`（本单元整体还剩多少 K），并且 **一个单元可能连续算多个输出 tile**（`continue_current_work` 会返回 true 并把工作推进到下一个 tile）。

#### 4.3.3 源码精读

**get_num_sk_tiles**——Stream-K 的核心公式。逐行对应 4.3.2 的数学：

[include/cutlass/gemm/kernel/tile_scheduler_params.h:L1063-L1112](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/tile_scheduler_params.h#L1063-L1112)

注意三道闸门：纯数据并行/SplitK 模式直接返回 0（L1077-1080）；无量化或 K 太短返回 0（L1090-1094）；启发式「尾部过半」返回 0（L1099-1106）。最后把 `sk_tiles` 向下对齐到 cluster_size 的倍数（L1109），保证 cluster 内可整除。

`get_num_sk_units`——决定用多少个 stream-K 单元来覆盖 `sk_tiles`：

[include/cutlass/gemm/kernel/tile_scheduler_params.h:L1114-L1139](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/tile_scheduler_params.h#L1114-L1139)

其逻辑是：总 K-迭代数 `k_tiles_per_output_tile * sk_tiles` 除以最小迭代数 8，得到「理论需要的最小单元数」，再与「单波 CTA 数」取小，并按 cluster 取整。

判定分解模式的总入口 `select_decomposition_mode`，能看到 SplitK 短路、DataParallel 短路，以及 StreamK/SplitK 的最终抉择：

[include/cutlass/gemm/kernel/tile_scheduler_params.h:L798-L833](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/tile_scheduler_params.h#L798-L833)

「若 sk_units 是 sk_tiles 的整数倍，退化为 SplitK；否则是 Stream-K」的关键判断：

[include/cutlass/gemm/kernel/tile_scheduler_params.h:L911-L922](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/tile_scheduler_params.h#L911-L922)

最小迭代数常量 `min_iters_per_sk_unit_ = 8`（K 太短不拆）与最大分组数 `max_sk_groups_ = 8`（用于 L2 局部性分组）：

[include/cutlass/gemm/kernel/tile_scheduler_params.h:L466-L470](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/tile_scheduler_params.h#L466-L470)

Stream-K 的 `WorkTileInfo`——多了 K 拆分信息与「是否单独归约单元」标记：

[include/cutlass/gemm/kernel/sm90_tile_scheduler_stream_k.hpp:L91-L154](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm90_tile_scheduler_stream_k.hpp#L91-L154)

注意 `is_final_split` 判断 `(K_idx + k_tile_count) == k_tiles_per_output_tile`——只有「算到 tile 的 K 末端」的那个单元才算最终分片、才有资格做 epilogue。

Stream-K 的 `Arguments`——用户可调的旋钮：`splits`（>1 强制 Split-K）、`max_swizzle_size`、`raster_order`、`reduction_mode`（确定性/非确定性归约）、`decomposition_mode`（DataParallel/Heuristic/StreamK/SplitK）：

[include/cutlass/gemm/kernel/sm90_tile_scheduler_stream_k.hpp:L156-L202](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm90_tile_scheduler_stream_k.hpp#L156-L202)

`assign_work`——运行期把 `linear_idx` 翻译成具体工作。三种分支：单独归约单元、数据并行单元、stream-K 单元：

[include/cutlass/gemm/kernel/sm90_tile_scheduler_stream_k.hpp:L1002-L1040](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm90_tile_scheduler_stream_k.hpp#L1002-L1040)

其中数据并行分支（`linear_idx >= sk_units_` 且非 split）直接把整段 K 赋给该单元：

[include/cutlass/gemm/kernel/sm90_tile_scheduler_stream_k.hpp:L1027-L1032](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm90_tile_scheduler_stream_k.hpp#L1027-L1032)

#### 4.3.4 代码实践（计算型）

**目标**：手算一遍 `get_num_sk_tiles`，体会尾部波浪费与 Stream-K 的关系。

**问题设定**：假设 `ctas_per_wave = 80`，`k_tiles_per_output_tile = 16`，cluster_size = 1。分别对 `output_tiles = {640, 660, 700}` 计算：
1. 数据并行调度下，尾部波有多少 SM 空转？
2. `get_num_sk_tiles` 返回多少（启发式模式 `Heuristic`）？

**计算步骤（以 660 为例）**：
- `full_waves = 660/80 = 8`，`total_waves = ceil(660/80) = 9`。存在量化。
- 数据并行尾部：第 9 波只有 `660 - 8*80 = 20` 个 tile，**60 个 SM 空转**（尾部波 75% 闲置）。
- Stream-K：`dp_waves = 8-1 = 7`，`dp_tiles = 7*80 = 560`，`sk_tiles = 660-560 = 100`。
- 启发式闸门：尾部 `tail_tiles = 20`，`2*20=40 < 80`，不触发「过半」→ 返回非 0。
- K 闸门：`16 > 8`，通过。
- 对齐 cluster（=1）后 `sk_tiles = 100`。

**对比**：
- `output_tiles = 640`：`full_waves = total_waves = 8`，无量化 → `sk_tiles = 0`（纯数据并行，无浪费）。
- `output_tiles = 700`：尾部 = `700 - 8*80 = 60`，`2*60=120 >= 80` → 启发式返回 0（尾部够满，不值得拆）。

**结论（即练习任务答案）**：Stream-K 收益最大的形状是 **「输出 tile 数略大于 SM 数的整数倍，使尾部波很小」**（如 660：尾部仅 20，60 个 SM 闲置），**且 K 维足够长**（`k_tiles_per_output_tile > 8`）。当无量化（640）或尾部过满（700）时，Stream-K 自动退化为数据并行。

> 待本地验证：上述数值是按源码公式手算的结果；若要在 GPU 上确认，可参见 4.4.4 的实践，用 `cutlass_profiler` 或在调试输出里打印 `sk_tiles_`/`sk_units_`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 Stream-K 只拆「最后两波」的 tile，而不是拆所有 tile？

**参考答案**：拆 K 必然引入跨 CTA 归约（写部分和、屏障同步、再读回相加），这是额外开销。满波里的 tile 用数据并行算「免费」（无需归约）。只把「否则会空转 SM」的尾部 tile 拆开，就能用最小的归约代价换取消除尾部浪费——这是 Stream-K 相比「全部 Split-K」的关键优势。

**练习 2**：`select_decomposition_mode` 在什么条件下会把一个本可 Stream-K 的问题退化为 Split-K？

**参考答案**：当 `sk_units` 是 `sk_tiles` 的整数倍且 `sk_tiles < sk_units` 时（见 L912-917）。此时每个 stream-K tile 恰好被整数个单元均分，K 拆分完全规整、不跨 tile 边界，归约更简单，因此选 SplitK 更划算。

---

### 4.4 跨 CTA 归约：fixup 与 workspace

#### 4.4.1 概念说明

当一个输出 tile 被 `p` 个 stream-K 单元协作计算（每个单元算 K 的一段）时，没有任何一个单元单独持有该 tile 的完整结果。需要一套机制把 `p` 份部分累加器（partial accumulator）合并成最终值，再由其中一个单元跑 epilogue 写回 D。这套机制就是 **fixup**。

fixup 的设计要点：
- **workspace**：一块全局内存，存放各单元的部分累加器，外加一个 **lock/barrier workspace** 做同步计数。
- **角色分工**：
  - **非最终单元**（不是 `is_final_split`）：把自己的累加器写入 workspace，并给对应 tile 的屏障 `arrive_inc`（报到 +N）。
  - **最终单元**（`is_final_split`）：等待屏障达到「peer 数」（`wait_eq`），意味着所有协作者都已写入；再把自己的累加器与 workspace 里其他 peer 的部分逐元素相加（`load_add`），最后跑 epilogue。
- **BlockStripedReduce**：归约的具体布局策略，让同一 warp group 内线程把数据条带化存放，避免写冲突。
- **确定性 vs 非确定性归约**（`ReductionMode`）：浮点加法不满足结合律，不同到达顺序可能产生微小差异；`Deterministic` 模式强制按 K 顺序归约以保证可复现。

#### 4.4.2 核心流程

```
对一个被拆分的 tile（requires_fixup == true）：

if 本单元是 is_final_split:           # 我负责收尾 + epilogue
    wait_eq(lock, peer 数)             # 等所有 peer 写完
    load_add(accum, workspace)         # 把别人的部分加到我的累加器
    compute_epilogue == true           # 跑 epilogue 写 D
else:                                  # 我只是贡献一段部分和
    若我是该 tile 的第一个贡献者: store
    否则: wait → reduce（读到现有值并相加）→ 写回
    arrive_inc(lock, k_tile_count)     # 报到，告诉最终单元我写完了
```

`requires_fixup` 的判定很简单：本单元本次的 `k_tile_count` 是否等于「完整 tile 的 K 数」。相等→独立算完→无需 fixup；不等→是部分拆分→需要 fixup。

#### 4.4.3 源码精读

`requires_fixup`：仅当本次工作覆盖的 K-tile 数不等于完整 tile 的 K 数时才需要归约：

[include/cutlass/gemm/kernel/sm90_tile_scheduler_stream_k.hpp:L382-L388](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm90_tile_scheduler_stream_k.hpp#L382-L388)

`compute_epilogue`：只有「最终分片」或「单独归约单元」才执行 epilogue——这是 fixup 之所以能工作的前提（只有一个单元写 D）：

[include/cutlass/gemm/kernel/sm90_tile_scheduler_stream_k.hpp:L583-L593](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm90_tile_scheduler_stream_k.hpp#L583-L593)

`fixup` 的三分支结构：单独归约单元 / 非最终贡献者 / 最终单元。最终单元那段（`else` 分支）正是 `wait_eq` 后 `load_add`：

[include/cutlass/gemm/kernel/sm90_tile_scheduler_stream_k.hpp:L544-L552](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm90_tile_scheduler_stream_k.hpp#L544-L552)

非最终贡献者分支：第一个 peer 用 `store` 初始化 workspace，后续 peer 用 `reduce`（读-加-写），最后 `arrive_inc` 报到。确定性模式用 `wait_eq`（严格顺序），非确定模式用 `wait_lt`（只要首个 peer 写过即可）：

[include/cutlass/gemm/kernel/sm90_tile_scheduler_stream_k.hpp:L508-L543](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm90_tile_scheduler_stream_k.hpp#L508-L543)

workspace 大小计算：屏障 workspace 按「tile 数 × warp group 数」算，归约 workspace 按「tile 数 × tile 面积 × 累加器位宽」算：

[include/cutlass/gemm/kernel/tile_scheduler_params.h:L1141-L1156](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/tile_scheduler_params.h#L1141-L1156)

内核里调用 fixup 的位置——在 consumer warp group 算完 MMA、调用 epilogue 之前：

[include/cutlass/gemm/kernel/sm90_gemm_warpspecialized_cooperative.hpp:L475-L479](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/kernel/sm90_gemm_warpspecialized_cooperative.hpp#L475-L479)

#### 4.4.4 代码实践（源码阅读 + 可选运行）

**目标**：看清 fixup 的「写-等-加」三段，并对比数据并行与 Stream-K 在 workspace 上的差异。

**步骤（源码阅读）**：
1. 在 `sm90_tile_scheduler_stream_k.hpp` 打开 `fixup`（L404 起），找到三段：
   - 单独归约单元段（`is_reduction_unit()`，L501-507）：`wait_eq` 等全部 peer 写完，再 `separate_reduction` 把所有 peer 的部分相加。
   - 非最终贡献者段（L508-543）：`store`/`reduce` + `arrive_inc`。
   - 最终单元段（L544-551）：`wait_eq` + `load_add`。
2. 对照数据并行 `static_tile_scheduler.hpp` 的 `fixup`（L416-426）——它是空函数体，再次印证「数据并行无需归约」。

**步骤（可选运行，需 SM90 GPU）**：
1. 编译 example 49：`cmake .. -DCUTLASS_NVCC_ARCHS=90a`，`make 49_hopper_gemm_with_collective_builder -j`。
2. 用一个会触发尾部浪费的形状运行，例如 `--m=5120 --n=5120 --k=4096`（调整 M/N 使 tile 数略大于 SM 数整数倍），观察 Stream-K 变体与普通 cooperative 变体的耗时差异。
3. （进阶）在 `select_decomposition_mode` 末尾临时加一条主机端 `printf` 打印 `sk_tiles`/`sk_units`/`heuristic_mode`，验证 4.3.4 的手算结论。**注意：这属于修改源码用于学习，验证后请还原。**

**预期结果**：对「尾部波很小」的形状，Stream-K 变体应明显快于数据并行 cooperative 变体；对 tile 数恰为 SM 数整数倍的形状，两者接近（Stream-K 退化）。

> 待本地验证：具体加速比依赖 GPU 型号、tile/cluster 形状与 K 大小；若无 SM90 环境，请完成源码阅读部分即可。

#### 4.4.5 小练习与答案

**练习 1**：为什么 fixup 里最终单元用 `wait_eq(lock, K_idx)`（等到等于自己的 K 起点），而不是等到 peer 数？

**参考答案**：在「链式归约」（非单独归约）模式下，各 peer 按到达顺序依次把自己的 `k_tile_count` 累加进 lock。最终单元的 `K_idx` 恰好等于「在它之前所有 peer 报到的 k_tile_count 之和」，所以 `wait_eq(lock, K_idx)` 表示「我之前的所有 peer 都已写完」。这是一种用累加计数代替「peer 个数」的同步技巧，天然支持各 peer 贡献不等长的情况。

**练习 2**：`ReductionMode::Deterministic` 与非确定模式在 fixup 里的唯一区别体现在哪一行？为什么这能保证确定性？

**参考答案**：区别在 L518-529 的等待条件——确定性用 `wait_eq(..., work_tile_info.K_idx)`（必须严格等到此前所有按序 peer 写完），非确定用 `wait_lt(..., 1)`（只要首个 peer 写过即可，后续 peer 可乱序相加）。确定性模式强制按 K 顺序累加，浮点加法顺序固定，因此结果可复现；非确定模式允许乱序归约，更快但结果可能有微小差异。

---

## 5. 综合实践

**任务**：用 example 49 作为脚手架，对比「数据并行」与「Stream-K」两种调度，并用本讲学到的公式预测哪个更快。

**操作步骤**：

1. **阅读示例结构**：打开 `examples/49_hopper_gemm_with_collective_builder/49_collective_builder.cu`。
   - 看 `ExampleRunner` 模板的 `TileSchedulerType` 默认值是 `PersistentScheduler`（数据并行）：

     [examples/49_hopper_gemm_with_collective_builder/49_collective_builder.cu:L258](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/49_hopper_gemm_with_collective_builder/49_collective_builder.cu#L258)

   - 看 `GemmKernel` 如何把 `TileSchedulerType` 作为 `GemmUniversal` 的第 4 个模板参数注入：

     [examples/49_hopper_gemm_with_collective_builder/49_collective_builder.cu:L330-L335](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/49_hopper_gemm_with_collective_builder/49_collective_builder.cu#L330-L335)

   - 看 `main` 里如何用一个 `ExampleRunner<..., StreamKScheduler>` 实例跑 Stream-K 变体（cooperative schedule）：

     [examples/49_hopper_gemm_with_collective_builder/49_collective_builder.cu:L635-L644](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/49_hopper_gemm_with_collective_builder/49_collective_builder.cu#L635-L644)

2. **预测**：选定一个 tile 形状（示例用 `Shape<_128,_128,_64>`，cluster `Shape<_2,_1,_1>`，故 tile 面积 128×128）。对问题 `M=N=4096, K=2048`：
   - tile 数 ≈ `(4096/128) × (4096/128) = 32 × 32 = 1024`。
   - 假设 H100 有 132 SM、cluster 2×1，估算 `ctas_per_wave`。
   - 用 4.3.4 的公式判断 `sk_tiles` 是否为 0，预测 Stream-K 是否会被启用。
3. **验证**（可选，需 SM90）：编译运行，观察两个变体的 `Passed.` 与耗时；调整 M/N 找到「Stream-K 显著更快」的形状，验证你的预测。
4. **反思**：解释为什么 example 49 把 Stream-K 与 **cooperative** schedule（而非 pingpong）搭配——提示：cooperative 用多个 warp group 并行发 MMA，更适合在 stream-K 的「每个单元算较长 K 段」场景下摊开计算。

**预期收获**：把「调度器 tag → `GemmUniversal` 模板参数 → 运行期 `WorkTileInfo`/`fixup`」这条链路在真实示例里走通一遍，建立从高层 API 到调度器源码的完整心智模型。

> 待本地验证：步骤 2 的 tile/SM 估算与步骤 3 的实测耗时依赖具体 GPU；若无可运行环境，请聚焦步骤 1 与 4 的源码理解。

## 6. 本讲小结

- **tile scheduler 是可插拔策略**：3.x 内核把「谁算哪个 tile、算 K 的哪段、是否写 D」外包给调度器，靠 `TileSchedulerSelector` 在编译期按 (tag, 架构) 选类，运行期用 `initial_work_tile_info`/`fetch_next_work`/`compute_epilogue`/`fixup` 一套钩子驱动内核。
- **持久化是底座**：启动 CTA 数 ≈ SM 数，每个 CTA 用 `advance_to_next_work`（游标 += grid_size）跨波领活；raster order 照顾 L2 局部性。数据并行调度器 `WorkTileInfo` 无 K 拆分、`fixup` 为空、不需要 workspace。
- **Stream-K 消除尾部波浪费**：当 `output_tiles` 不是 `ctas_per_wave` 整数倍且尾部不满半波、K 又足够长时，把最后两波 tile 的 K 维工作量连续摊到 `sk_units` 个单元上，让原本空转的 SM 干活；否则退化为数据并行或 Split-K。
- **核心公式在 `get_num_sk_tiles`**：`sk_tiles = output_tiles − dp_waves×ctas_per_wave`，受「无量化」「K 太短（≤8）」「尾部过半」三道闸门控制。
- **跨 CTA 归约靠 fixup + workspace**：被拆分的 tile 由多个单元贡献部分累加器，非最终单元 `store`/`reduce`+`arrive_inc`，最终单元 `wait_eq`+`load_add` 后独占 epilogue；`Deterministic` 模式按序归约保证可复现。
- **代价**：Stream-K 用额外的全局 workspace 与屏障同步换取尾部利用率，因此只在「尾部浪费大、K 足够长」时才划算——这正是启发式存在的意义。

## 7. 下一步学习建议

- **u3-l1（异步流水线）**：本讲多次提到 producer/consumer warp group，但把「搬算重叠」的同步原语（`PipelineTmaAsync`、mbarrier）当黑盒。下一站应深入 `include/cutlass/pipeline/`，理解 `PipelineState` 的 `(index, phase)` 与双屏障模型。
- **u3-l2（TMA）**：Stream-K 的数据搬运依赖 TMA bulk copy，且 cluster 内 multicast 复用操作数（4.3 节提到的 cluster 局部性）正是 TMA 的能力。建议读 `include/cute/arch/copy_sm90_tma.hpp` 与 `copy_traits_sm90_tma.hpp`。
- **Blackwell 调度器**：本讲聚焦 SM90；SM100 的 `PersistentTileSchedulerSm100StreamKParams`（`tile_scheduler_params.h` 后段）建立在 SM90 实现之上，但 grid 形状计算与 CLC（cluster launch control）查询有差异，可在学完 u3-l7（Blackwell SM100 collective）后对比阅读。
- **Grouped GEMM 调度**：`tile_scheduler.hpp` 里的 `GroupScheduler` → `PersistentTileSchedulerSm90Group` 处理「多个不同形状矩阵」的调度，是 u3-l4（Grouped GEMM）的前置，可作为延伸阅读。
