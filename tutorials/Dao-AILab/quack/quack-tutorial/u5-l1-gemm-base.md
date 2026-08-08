# GemmBase 共享主循环与 epilogue 驱动

## 1. 本讲目标

本讲是 GEMM **设备侧（device-side）内核**系列的第一篇。前面 u4 系列讲的是「主机侧」——编译、计划缓存、公共 API；本讲起，我们钻进**真正在 GPU 上跑的内核代码**。

学完本讲，你应该能够：

1. 说清 `GemmBase` 这一层**共享了什么**、把什么**留给各 SM 子类**，以及为什么这样切分。
2. 读懂 GEMM 内核的 **mainloop 主循环**：A/B 数据如何经 TMA 从 gmem 搬到 smem、再搬到寄存器，被 MMA 指令累加进 accumulator；以及持久化内核里「加载 warp / MMA warp」的分工。
3. 读懂 `gemm_base.py` 的 **epilogue 驱动循环**：它如何把 accumulator 转成输出 D（和 aux 输出），并理解它依次调用的 `store_setup` / `store_convert` / `store_r2s` 三个 store 钩子构成的可插拔协议。
4. 解释 `NamedBarrierGemm` 这组命名屏障在「多 warp 组共享 SMEM」场景下的用途。

---

## 2. 前置知识

本讲是 **advanced** 难度，默认你已经掌握前三篇讲义建立的认知，这里只做极简回顾，不重复：

- **u4-l1（GEMM 编译与计划缓存）**：GEMM 主机侧分「编译—计划—启动」三层。编译期用符号张量把结构烘焙进 `.o` cubin，计划期定路由，调用期只换数据指针。本讲讲的就是这些 cubin **内部**的设备侧代码。
- **u3-l1（copy_utils）**：CuTe 里数据搬运由 **TiledCopy = CopyAtom + thr_layout + val_layout** 描述；GEMM 用 **TMA**（描述符驱动的整块拷贝，由 mbarrier 的 `complete_tx::bytes` 信用收尾）做 gmem↔smem 搬运；非整除边界用谓词处理。
- **u3-l5（异步流水线与同步原语）**：软件流水线用 `(index, phase)` 管理多级 smem 槽位，producer/consumer 经一对 mbarrier（empty/full）的「空满状态机」握手；`PipelineTmaAsync` 用事务屏障确认整块 TMA 到位。本讲的 AB pipeline、epi pipeline 全是这一套。

如果你对上面任何一条没有把握，请先回看对应讲义。本讲的关键术语：**mainloop（主循环）**、**accumulator（累加器）**、**epilogue（尾声/收尾）**、**warp group（线程束组）**、**named barrier（命名屏障）**、**TMEM（张量内存，SM100 专用）**。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `quack/gemm_base.py` | **主角**。定义 `GemmBase`（各 SM 共享的「非 mainloop」部件：epilogue 驱动、split-K 收尾、批次旋转、tile 调度参数构建）和 `GemmTmaBase`（SM90+ 共享的 TMA 加载原语 `load_tma` 与 pipeline 构造）。 |
| `quack/gemm_sm90.py` | `GemmSm90`：Hopper 的具体 mainloop（WGMMA）。本讲借用它说明 mainloop 的 warp 分工与数据流，是 u5-l2 的主角，这里只读结构。 |
| `quack/pipeline.py` | QuACK 对 cutlass pipeline 的封装（`PipelineTmaAsync`、`NamedBarrier` 等）。本讲主要引用其中的命名屏障与 TMA 流水线。 |
| `quack/epilogue/ops.py` | `EpiOp` / `TileStore` / `DStore` 等 epilogue 操作类，定义 `store_setup` / `store_convert` / `store_r2s` 钩子。 |

> 说明：mainloop 本身（`mma`、`mma_rs_interleaved` 及 warp 分工）**不在 `gemm_base.py`**，而在各 SM 子类（`gemm_sm90.py` / `gemm_sm100.py` / `gemm_sm120.py`）。本讲讲清这一**切分边界**，并用 SM90 做示例追踪。

---

## 4. 核心概念与源码讲解

### 4.1 GemmBase 公共结构

#### 4.1.1 概念说明

GEMM 内核跨三代硬件（Hopper SM90 / Blackwell SM100 / GeForce SM120），每一代的矩阵乘指令完全不同——SM90 用 **WGMMA**，SM100 用 **tcgen05 MMA**（累加器放进专用 TMEM），SM120 用 warp 级 MMA。所以「主循环怎么算」是硬件强相关的，**无法跨架构共享**。

但内核里有一大块逻辑是**与算子无关、各架构都一样**的：

- 把 accumulator（累加结果）转成输出、做 `α·D+β·C+bias`、激活、存回 gmem —— 这是 **epilogue**。
- 沿 K 维切分（split-K）时的部分和提交与最终归并协议。
- 批次张量从 caller 序 `(l, m, n)` 旋转到 kernel 序 `(m, n, l)`。
- 给持久化内核算 tile 调度参数。

`GemmBase` 把这些都收进来，类文档直白写着：

> [quack/gemm_base.py:73-74](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L73-L74) — `GemmBase`："Common non-mainloop pieces shared by GEMM architectures."（各 GEMM 架构共享的、**非主循环**的部件。）

于是设计成两层：

- `GemmBase` —— 共享的「非 mainloop」部件 + 一堆**可被覆盖的钩子**（默认是空操作）。
- `GemmSm90` / `GemmSm100` / `GemmSm120` —— 各自实现 mainloop，并按需覆盖钩子、混入 epilogue mixin（如 `GemmDefaultEpiMixin`、`ComposableEpiMixin`）。

这是一种典型的**「模板方法 + 钩子」**设计：驱动循环（`epilogue`）固定在基类，具体行为靠子类/mixin 覆盖钩子注入。下表归纳切分边界：

| 在 `GemmBase` / `GemmTmaBase`（共享） | 在各 SM 子类（不共享） |
|---|---|
| `epilogue` 驱动循环 | `mma` / `mma_rs_interleaved`（主循环核心） |
| `epilogue_split_k` / `split_k_partial_commit` | warp 角色分工（load warp / MMA warp / epi warp） |
| `load_tma`（TMA 加载原语） | 分区累加器、A/B 片段（`partition_fragment_ABC`） |
| `make_ab_pipeline` / `make_epi_pipeline` | pingpong 同步、寄存器配额（`setmaxregister_*`） |
| `rotate_batch_last` / `get_scheduler_arguments` | 具体 MMA 指令（wgmma / tcgen05） |
| epilogue 钩子默认实现（`epi_begin` / `epi_visit_subtile` / ...） | 各 epilogue mixin 覆盖钩子 |

#### 4.1.2 核心流程

从「一个 CTA 内部」看，`GemmBase` 参与的整体流程是：

```
持久化 while work_tile.is_valid_tile:          ← 各 SM 子类的 mainloop 外壳
   ├─ (load warp) load_tma(...)  把 A/B 经 TMA 填进 smem 多级槽   ← GemmTmaBase 共享
   ├─ (MMA warp) mma(...)        从 smem 取片段 → MMA 指令 → 累加进 acc  ← 子类
   └─ epilogue_split_k(...)      包一层 split-K 判定后调用 ↓       ← GemmBase 共享
         └─ epilogue(...)        acc → D/aux → gmem 的驱动循环     ← GemmBase 共享
              ├─ epi_setup_aux_out  / D 上下文   (store_setup 钩子)
              ├─ epi_begin                       (每 CTA tile 一次)
              └─ for 每个子 tile epi_idx:
                    ├─ load_acc_subtile          (取一子块累加器)
                    ├─ epi_begin_loop            (每子 tile 一次)
                    ├─ epi_visit_subtile         (epilogue 数学：α·D+β·C+bias)
                    ├─ epi_end_loop              (收尾，可改写 D)
                    ├─ store_convert             (转存储 dtype)
                    ├─ store_r2s                 (寄存器→smem)
                    └─ s2g TMA store             (smem→gmem)
```

关键观察：**mainloop 外壳和 `mma` 在子类，但每算完一个 tile 就调用的 epilogue 在基类**。子类的 mainloop 只需在 `acc` 凑齐后调用 `self.epilogue_split_k(...)`，epilogue 的所有复杂性都被基类吸收。

#### 4.1.3 源码精读

**(a) 共享的 constexpr 配置。** `GemmBase` 把一批影响编译产物的「开关」放在 `self` 上当 Constexpr。最关键的是 split-K：

[quack/gemm_base.py:84-99](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L84-L99) —— `split_k` 与 `split_k_mode`，注释写明：`split_k == 1` 会编译成**与非 split 内核逐位相同**的 cubin；非终止 split 不跑 epilogue，只把原始 f32 累加器片段倒进工作区，最后由终止 split 跑完整 epilogue（CUTLASS stream-K fixup 语义）。

```python
split_k = 1
split_k_mode = SplitKMode.SERIAL
```

另外 `b_transposed` / `a_transposed` / `cd_transposed` / `cd_packed`（[L107-L115](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L107-L115)）是一组**trace 期重排开关**，让 host 端省掉每次调用都要做的 `.mT` / `.view` 视图。

**(b) 默认为空的 epilogue 钩子。** 基类定义了一整套生命周期钩子，默认全部是 no-op，留给 mixin 覆盖。例如：

[quack/gemm_base.py:871-885](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L871-L885) —— `epi_begin` 默认返回空元组；`epi_begin_loop` / `epi_visit_subtile` / `epi_end_loop` / `epi_end` 同理（[L887-L939](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L887-L939)）。这种「基类空实现 + mixin 覆盖」正是可组合 epilogue 的地基（详见 u6-l1）。

**(c) 命名屏障注册表 `NamedBarrierGemm`。** 这是本讲的一个关键小部件：

[quack/gemm_base.py:34-46](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L34-L46) —— 一个 `IntEnum`，集中登记 CTA 内部「跨 warp 组」的命名屏障 ID：

```python
class NamedBarrierGemm(enum.IntEnum):
    Epilogue = enum.auto()      # 从 1 开始（barrier 0 留给 sync_threads()）
    EpilogueLoad = enum.auto()  # mainloop 加载 warp 通知 epilogue 加载 warp 可开始
    MmaWG0 = enum.auto()        # MMA warp group 0
    ...
```

SM90 的 `epilogue_barrier` 就是用它构造的：[quack/gemm_sm90.py:290-293](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L290-L293)。它的用途见 **4.1.4 实践**与 **4.3.3**。

#### 4.1.4 代码实践

**实践目标**：亲手划清「共享 vs 子类」的边界，理解 `GemmBase` 为何不含 mainloop。

**操作步骤**：
1. 打开 [quack/gemm_base.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py)，在 `class GemmBase` 与 `class GemmTmaBase` 内**搜索 `def mainloop`、`def mma`、`def gemm(`**。
2. 你会发现它们**不存在**于 `gemm_base.py`。
3. 再打开 [quack/gemm_sm90.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py)，搜索同样的方法名，确认 mainloop 住在子类。
4. 对照本讲 4.1.1 的切分表，给每个方法归类。

**需要观察的现象**：基类里能找到 `def epilogue`、`def epilogue_split_k`、`def load_tma`、`def make_ab_pipeline`，但找不到任何 `def mma` / `def mainloop`。

**预期结果**：你会直观体会到「算子硬件相关→不共享；数据搬运/收尾/调度→共享」这条切分原则。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `split_k == 1` 时编译产物与「根本不写 split-K」的内核逐位相同？

> **答案**：`split_k` 是 Constexpr，`epilogue_split_k` 里所有 `if const_expr(self.split_k > 1 ...)` 分支在 `split_k == 1` 时会在 trace 期被整体折叠删除，最终只剩一次裸的 `epilogue(...)` 调用，因此没有额外指令。这正是「Constexpr 分支只编入命中分支」的好处（见 u1-l4）。

**练习 2**：`NamedBarrierGemma.Epilogue` 为什么从 1 而不是 0 开始？

> **答案**：注释明确写「barrier 0 is reserved for `sync_threads()`」。PTX 的命名屏障（`bar.*`）0 号被全局线程同步占用，自定义屏障必须从 1 开始，用枚举集中管理避免 ID 冲突。

---

### 4.2 mainloop 主循环

#### 4.2.1 概念说明

**mainloop（主循环）**是 GEMM 内核的核心：它沿着收缩维 K 反复做「取一块 A、取一块 B、做一次矩阵乘、累加」。数学上：

\[
D_{M\times N} \;=\; \sum_{k=0}^{K/tile_k-1} A_{M\times tile_k}^{(k)} \cdot B_{tile_k\times N}^{(k)}
\]

每个 k-tile 的累加是：

\[
acc \;\leftarrow\; acc + A^{(k)} B^{(k)}
\]

mainloop 的难点不在数学，而在**让数据搬运与计算重叠**——这正是 u3-l5 的软件流水线。一个 CTA 内部把线程分成几组 warp，各司其职：

- **加载 warp（load warp）**：生产者。用 TMA 把 A、B 从 gmem 灌进 smem 的多级流水槽。
- **MMA warp**：消费者。等 smem 数据就绪，取片段进寄存器，发 MMA 指令累加进 `acc`。

这两组通过 **AB pipeline**（一对 empty/full mbarrier，见 u3-l5）握手：加载 warp 写满一槽通知 MMA warp，MMA warp 消费完通知加载 warp 可复用。

#### 4.2.2 核心流程

mainloop 的数据流（以 SM90 为例）：

```
gmem ──TMA──▶ smem(staged, 多级) ──ldmatrix/tile copy──▶ 寄存器(tCrA,tCrB) ──WGMMA──▶ acc(寄存器/tmem)
   ▲ producer(load warp)                          ▲ consumer(MMA warp)              │
   └──── AB pipeline (empty/full mbarrier 握手) ────┘                                 │
                                                                                       ▼
acc 凑齐后 ──▶ epilogue ──▶ gmem
```

mainloop 外壳（持久化）：

```
load warp:  while work_tile.is_valid_tile:        # 持久化调度循环
                算 copy_A/copy_B → load_tma(...) → advance scheduler
MMA warp:   while work_tile.is_valid_tile:
                mma(...)          # 累加 k_tile_cnt 次
                epilogue_split_k(...)   # 把 acc 交给 epilogue 驱动
                advance scheduler
```

注意：两组 warp **跑同一份 Python 源码**，靠 `if warp_idx >= ab_load_warp_id` 这样的**编译期分支**在 trace 期各自只编入自己的那一半——加载 warp 永远不执行 MMA 代码，反之亦然。

#### 4.2.3 源码精读

**(a) warp 角色分工（子类，但模式各架构一致）。** SM90 用 `ab_load_warp_id` 把线程劈成两半：

[quack/gemm_sm90.py:294-295](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L294-L295) —— `ab_load_warp_id = mma_warp_groups * 4`（MMA warp 组之后的第一个 warp 就是加载 warp）。

[quack/gemm_sm90.py:1032-1037](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L1032-L1037) —— 加载 warp 分支：`if warp_idx >= self.ab_load_warp_id and warp_idx < ab_load_warp_id + num_ab_load_warps:` 进入生产者循环；下面 [L1180](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L1180) 的 `if warp_idx < self.ab_load_warp_id:` 才是 MMA 消费者。加载分支还会 `setmaxregister_decrease`（[L1033](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L1033)），MMA 分支 `setmaxregister_increase`（[L1181](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L1181)）——把寄存器配额倾斜给计算密集的 MMA warp。

**(b) 共享的 TMA 加载原语 `load_tma`。** 加载 warp 循环体里真正搬数据的，是 `GemmTmaBase` 提供的共享方法：

[quack/gemm_base.py:1009-1038](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L1009-L1038) —— `load_tma`：

```python
peek_empty_status = Boolean(True)
if 0 < k_tile_cnt:
    peek_empty_status = pipeline.producer_try_acquire(producer_state)  # 预取下一槽空不空
for k_tile in cutlass.range(k_tile_cnt, unroll=1):
    pipeline.producer_acquire(producer_state, peek_empty_status)       # 等 AB 槽空 + 上膛事务屏障
    tma_bar_ptr = pipeline.producer_get_barrier(producer_state)
    smem_idx = producer_state.index
    for copy_fn in copy_fns:                                            # 把 A、B（可能还有 aux）TMA 装进 smem[smem_idx]
        if const_expr(copy_fn is not None):
            copy_fn(k_tile_start + k_tile, smem_idx, tma_bar_ptr=tma_bar_ptr)
    pipeline.producer_commit(producer_state)                           # TMA pipeline 这里是 NOP
    producer_state.advance()
```

要点对照 u3-l5：`producer_acquire` 既「等数据槽空」又「上膛事务屏障的信用」，`copy_fn` 内的 TMA 指令发射时扣信用，consumer（MMA warp）侧 `consumer_wait` 等信用扣完即代表整块到位。

**(c) 加载 warp 在 mainloop 里怎么调它。**

[quack/gemm_sm90.py:1149-1155](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L1149-L1155) —— 把 `[copy_A, copy_B, copy_AuxA]` 三个拷贝函数连同 K 方向 tile 计数交给 `load_tma`：

```python
ab_producer_state = self.load_tma(
    ab_pipeline, ab_producer_state, [copy_A, copy_B, copy_AuxA],
    k_tile_cnt, k_tile_start=k_tile_start,
)
```

其中 `copy_A` / `copy_B` 是用 `copy_utils.tma_get_block_copy_fn` 基于 TMA atom 造的整块拷贝（[L1090-L1142](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L1090-L1142)）。加载 warp 跑完最后一个 tile 后还要 `ab_pipeline.producer_tail(...)`（[L1175-L1176](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L1175-L1176)）把残留的未消费槽排空，防止 CTA 退出使 smem barrier 失效。

**(d) MMA warp 的累加与 epilogue 衔接。**

[quack/gemm_sm90.py:1306-1314](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L1306-L1314) —— `self.mma(ab_pipeline, ab_read_state, mma_fn, acc, ...)` 把 K 方向所有 tile 累加进 `acc`。累加完即 epilogue：

[quack/gemm_sm90.py:1338-1341](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L1338-L1341) —— 注释 `# EPILOGUE`，随后构造 `epi_fn`（把除 `load_acc_subtile` 外的参数都绑定的偏函数，[L1400-L1426](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L1400-L1426)），交给 `self.epilogue_split_k(...)`（[L1427](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L1427)）。这正印证 4.1.2 的流程：**子类只管把 `acc` 凑齐，收尾全交给基类**。

**(e) AB pipeline 在哪造。** `make_ab_pipeline`（[gemm_base.py:1196-1247](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L1196-L1247)）根据 `gather_A` 选 `PipelineTmaAsync` 或 `PipelineTmaCpAsync`，注入 producer/consumer 到达计数与 `tx_count`（每块 TMA 字节数）。这是 u3-l5 的 `PipelineTmaAsync` 在 GEMM 里的具体化。

#### 4.2.4 代码实践

**实践目标**：跟踪一次完整的「gmem → smem → 寄存器 → acc」数据流，理解 producer/consumer 握手。

**操作步骤**（源码阅读型实践）：
1. 在 [gemm_sm90.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py) 定位 `ab_load_warp_id`（L295）、加载分支（L1032）、`load_tma` 调用（L1149）。
2. 跳到 [gemm_base.py 的 `load_tma`](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L1009-L1038)，画出 producer 侧：`try_acquire`（预取）→ `acquire`（等空+上膛）→ `copy_fn`（TMA 搬运）→ `commit`（NOP）→ `advance`。
3. 回到 SM90 MMA 分支（L1180 起），定位 `self.mma(...)`（L1306），这是 consumer 侧：内部 `consumer_wait` 等 smem 满后取片段、发 WGMMA。
4. 在一张纸上写出两个 warp 组的时序：加载 warp 写满第 0 槽→通知→MMA warp 消费第 0 槽的同时加载 warp 写第 1 槽……

**需要观察的现象**：`load_tma` 用的是 `cutlass.range(..., unroll=1)`（**运行期**循环，不展开），而 SM90 mainloop 外层 `while work_tile.is_valid_tile` 也是运行期循环。

**预期结果**：你能讲清「为什么加载 warp 和 MMA warp 可以并行——因为 AB pipeline 的 empty/full mbarrier 提供反压，加载不会跑过 MMA 太多（槽数有限）」。

#### 4.2.5 小练习与答案

**练习 1**：`load_tma` 里 `producer_commit` 注释说是「NOP for TMA pipelines」，为什么 TMA 流水线的 commit 是空操作？

> **答案**：TMA 的数据就绪由**事务屏障的信用机制**判定——`producer_acquire` 已经 `arrive_and_expect_tx` 上膛并预设了字节数，TMA 硬件搬运完成时会自动扣信用，consumer_wait 等信用扣完即可。所以不需要再额外 arrive，commit 自然是 NOP。这和 cp.async 流水线（要显式 `cp_async_commit`）不同（见 u3-l5）。

**练习 2**：为什么加载 warp 要 `setmaxregister_decrease`、MMA warp 要 `setmaxregister_increase`？

> **答案**：同一 CTA 的寄存器总量固定。加载 warp 几乎不碰寄存器（TMA 是描述符驱动、不经过寄存器），把配额让出来；MMA warp 要持有累加器 `acc` 和 A/B 片段，寄存器需求大。`setmaxregister_*` 在编译期重新分配寄存器上限，让计算 warp 拿到更多寄存器、减少溢出（spill）。

---

### 4.3 epilogue 驱动与 store 钩子

#### 4.3.1 概念说明

`acc` 累加完后，要把 fp32 累加结果变成最终输出：应用 \(D=\alpha\cdot acc+\beta\cdot C+bias\)、激活函数、可能的量化，再按目标 dtype 存回 gmem。这段逻辑叫 **epilogue**（尾声）。

epilogue 的复杂度远超想象：要支持 bias（行/列向量广播）、C（残差加项）、激活、gated MLP 的拼接、量化输出、行/列归约输出（如 LSE）、rotary……如果每种组合都手写一遍驱动循环，会爆炸。

QuACK 的解法是**驱动循环固定 + 可插拔 op**：

- 驱动循环（`GemmBase.epilogue`）**只编排时序**：取一子块 acc、调数学钩子、转 dtype、寄存器→smem、smem→gmem。
- 具体行为封装成 `EpiOp`（[epilogue/ops.py](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py)）对象，每个 op 提供一组**钩子**，驱动循环统一调用。

本讲聚焦「**store 钩子**」——把结果存出去的三件套：

| 钩子 | 调用频率 | 职责 |
|------|---------|------|
| `store_setup` | 每 CTA tile 一次 | 构造 r2s 的 TiledCopy、分区 smem、造 gmem 拷贝函数、算 store 谓词 |
| `store_convert` | 每子 tile 一次 | 把累加器片段从 acc_dtype 转成存储 dtype（含舍入、gated 重排） |
| `store_r2s` | 每子 tile 一次 | 把转换后的片段从寄存器拷进 smem 暂存槽 |

> 待本地验证：本讲引用的钩子调用时序来自静态阅读；若你想确认某钩子是否在某 config 下被编译剔除，可用 `cute.printf` 在钩子里打点实测。

#### 4.3.2 核心流程

epilogue 驱动循环的骨架（精简自 [gemm_base.py 的 `epilogue`](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L250-L514)）：

```
1. 组装 store_ctxs（每个要存的输出一个 6-元组）
     = epi_setup_aux_out(...) 调各 aux TileStore 的 store_setup
     + 若有 D：在最前面插 (DStore(), quant, copy_r2s, tRS_sD, copy_D, None)
2. epi_begin(...)                         # 每 CTA tile 一次性设置
3. (可选) acc 预扫描 epi_prepass_*         # 需要全 tile 归约的 op（如 QK-norm）
4. (可选) 内联预取 C 进 smem 多级
5. for epi_idx in range_constexpr(epi_tile_num):   # 逐子 tile
      load_acc_subtile(tRS_rD, epi_coord)          # 取一子块 acc
      (若有 C) wait + smem→register 拷 C
      epi_begin_loop(...)
      epi_visit_subtile(...)  → tRS_rAuxOuts       # epilogue 数学
      epi_end_loop(...)        # 可原地改写 D
      for 每个输出:
          (若 quant) quant.quantize(...)
          op.store_convert(...)                     # store_convert 钩子
      acquire epi store 槽 / arrive_and_wait
      for 每个输出:
          op.store_r2s(...)                         # store_r2s 钩子
      (若 TMA store) fence + arrive + copy_out + producer_commit
      (否则) arrive → copy_out → arrive
6. epi_end(...)
```

注意第 1 步：**D 的 host 管线是内核自建的**（不在 `_epi_ops` 里），所以驱动循环**直接拼** D 的上下文；aux 输出（如 postact）才走 `epi_setup_aux_out` 调它们的 `store_setup`。但**设备侧**两者的 `store_convert` / `store_r2s` 钩子完全一致——这正是 `DStore` 类存在的意义（见 4.3.3）。

`epi_tile_num` 是一个 CTA tile 被切成多少个「子 tile」来分批处理（受寄存器/smem 限制，不能一次吃下整个 `cta_tile_m × cta_tile_n`）。循环用 `range_constexpr`（**编译期展开**），因为 `store_ctxs` 是静态 Python 元组、staged 循环变量无法索引它。

#### 4.3.3 源码精读

**(a) 组装 store 上下文（store_setup 钩子的调用点）。**

[quack/gemm_base.py:296-308](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L296-L308) —— `store_ctxs` 由 aux 输出（`epi_setup_aux_out`）和 D 拼成。注释（[L287-L295](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L287-L295)）说明每个 6-元组 `(op, quant, tiled_copy, tRS_s, copy_fn, store_pred)` 的含义：

```python
store_ctxs = self.epi_setup_aux_out(params, epi_smem_tensors, ...)   # 调 aux op 的 store_setup
if const_expr(has_D):
    store_ctxs = (
        (DStore(), self._epi_store_quant("D"), tiled_copy_r2s, tRS_sD, copy_D, None),
    ) + store_ctxs
```

默认 `epi_setup_aux_out` 返回空元组（[gemm_base.py:981-997](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L981-L997)）；`ComposableEpiMixin` 覆盖它，遍历 `_epi_ops` 里每个 `TileStore` 调其 `store_setup`。

**(b) store_setup 钩子（aux 输出）。** `TileStore.store_setup` 负责一次性准备：

[quack/epilogue/ops.py:1043-1074](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1043-L1074) —— 构造 `tiled_copy_aux_r2s`、`partition_D` 切出该 op 的 smem 区、用 `epilog_gmem_copy_and_partition` 造 gmem 拷贝函数、算 store 谓词，返回 `(tiled_copy, tRS_sAux, copy_fn, pred)`。

**(c) store_convert 与 store_r2s 钩子。** 主循环里逐子 tile 调用：

[quack/gemm_base.py:439-477](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L439-L477) —— 先（可选）量化、再 `store_convert`、最后 `store_r2s`：

```python
for i in cutlass.range_constexpr(len(store_ctxs)):
    op, quant, _, _, _, _ = store_ctxs[i]
    if const_expr(quant is not None):
        quant.quantize(self, epi_loop_tensors[quant.name], store_frags[i])
    store_frags_out.append(op.store_convert(self, store_frags[i], ...))   # ← store_convert
...
for i in cutlass.range_constexpr(len(store_ctxs)):
    op, _, tiled_copy_st, tRS_s, _, _ = store_ctxs[i]
    op.store_r2s(self, tiled_copy_st, store_frags_out[i], tRS_s[None, None, None, epi_buffer], tidx)  # ← store_r2s
```

具体实现：

- `TileStore.store_convert`（[ops.py:1077-1102](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1077-L1102)）：含逐 op 舍入模式、gated STSM 寄存器重排，核心是 `tRS_rAuxOut.to(dtype)`。
- `TileStore.store_r2s`（[ops.py:1105-1108](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1105-L1108)）：`cute.copy(tiled_copy, frag.contiguous(), tRS_s_stage)`。
- `DStore.store_convert` / `store_r2s`（[ops.py:1130-1158](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1130-L1158)）：D 专用，含 SM90 的 fp32 pair-XOR `STS.32` 路径（`epi_r2s_pair_xor`）。

`DStore` 的类文档（[ops.py:1111-1124](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1111-L1124)）说清了为何要它：D 的 host 管线（TMA atom、smem 布局、split-K 工作区重指向）必须内核自建，所以 D 不进 `_epi_ops`、没有 host 钩子；但设备侧 store 路径**必须**和 TileStore 走同一套 `store_convert` / `store_r2s`，这样量化、舍入、gated 重排对 D 和 aux 输出行为一致。

**(d) smem→gmem 的 TMA store。** r2s 之后是真正的写出（[gemm_base.py:478-501](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L478-L501)）：TMA 路径先 `fence_view_async_shared` + `arrive_and_wait`，再由 `is_tma_warp` 发 `copy_out`（`cp.async.bulk`）并 `producer_commit`。每个输出的 `copy_fn` 由 `epilog_gmem_copy_and_partition`（[gemm_base.py:1164-1194](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L1164-L1194)）造。

**(e) NamedBarrierGemm 的用途（呼应 4.1）。** epilogue 循环里反复出现 `epilogue_barrier.arrive_and_wait()`（如 [L368](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L368)、[L459](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L459)、[L501](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L501)）。`epilogue_barrier` 是 `pipeline.NamedBarrier`（[pipeline.py:186-213](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/pipeline.py#L186-L213)），用 `NamedBarrierGemm.Epilogue` 这个 ID 构造。

它的用途是**在同一 CTA 的多组 warp 之间做命名会合**。epilogue 里所有 `num_epi_warps` 个线程共享同一块 epi smem（r2s 写、s2g 读），写入与 TMA 读出之间必须同步：`arrive_and_wait` 让所有 epi 线程到齐、确保 smem 内容就绪后再继续。`NamedBarrierGemm` 枚举就是给这些会合点统一编号，避免不同会合撞同一个屏障 ID。比如 `EpilogueLoad`（[L37-L38](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L37-L38)）专门让 mainloop 加载 warp 通知「epilogue 的 C 加载 warp 现在可以开始加载了」——避免过早加载 C 干扰 A/B 的加载。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：定位 epilogue 驱动循环，列出它依次调用的 store 钩子，理解 `NamedBarrierGemm` 的用途。

**操作步骤**（源码阅读型实践）：
1. 打开 [gemm_base.py 的 `epilogue`](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L250-L514)。
2. 在主循环 `for epi_idx in cutlass.range_constexpr(epi_tile_num)`（[L370](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L370)）内，按出现顺序记下每个钩子调用及其行号。
3. 对照下表填写「驱动调谁 → 实现在哪」。

**需要观察的现象 / 预期结果**：你应得到这样一张时序表：

| 顺序 | 驱动循环调用 | 行号 | 实现位置 | 频率 |
|------|------------|------|---------|------|
| 0 | `epi_setup_aux_out` → 各 op `store_setup` | L296 | [ops.py:1043](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1043) | 每 CTA tile |
| 1 | `epi_begin` | L319 | [gemm_base.py:871](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L871) | 每 CTA tile |
| 2 | `load_acc_subtile` | L375 | [gemm_base.py:859](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L859) | 每子 tile |
| 3 | `epi_begin_loop` | L396 | [gemm_base.py:887](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L887) | 每子 tile |
| 4 | `epi_visit_subtile`（数学） | L415 | [gemm_base.py:892](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L892) | 每子 tile |
| 5 | `epi_end_loop` | L419 | [gemm_base.py:911](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L911) | 每子 tile |
| 6 | `op.store_convert` | L439 | [ops.py:1077/1130](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1130) | 每子 tile×每输出 |
| 7 | `op.store_r2s` | L467 | [ops.py:1105/1149](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/epilogue/ops.py#L1149) | 每子 tile×每输出 |
| 8 | s2g `copy_out`（TMA） | L478 | [gemm_base.py:1164](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L1164) | 每子 tile×每输出 |
| 9 | `epi_end` | L503 | [gemm_base.py:927](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L927) | 每 CTA tile |

4. 回答：`NamedBarrierGemm` 共定义了 8 个屏障（[L34-L46](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L34-L46)）。用一句话说清它的用途。

> **参考答案**：它是 CTA 内「跨 warp 组共享 SMEM」的命名会合点 ID 注册表，集中编号避免碰撞（且 0 号留给 `sync_threads()`），让加载 warp / MMA warp / epilogue warp 在读写同一块 smem 的关键节点（如「C 可以开始加载」「smem 内容读完了」）按名同步。

#### 4.3.5 小练习与答案

**练习 1**：为什么 D 不放进 `_epi_ops`、却仍实现了 `store_convert` / `store_r2s`？

> **答案**：D 的 host 管线（TMA atom、staged smem 布局、split-K 工作区重指向、`add_to_output`）必须由内核主流程自建，无法像 aux 输出那样被通用 op 描述，所以 D 不进 `_epi_ops`、没有 host 钩子。但设备侧 store 路径（转 dtype、寄存器→smem、量化/舍入/gated 重排）逻辑与 aux 输出一致，于是抽成 `DStore` 类、与 `TileStore` 共享同一对 `store_convert` / `store_r2s` 钩子，让驱动循环对 D 和 aux **一视同仁**地循环。

**练习 2**：epilogue 主循环用 `range_constexpr`（编译期展开），而 `load_tma` 用 `range(unroll=1)`（运行期），为什么 epilogue 这里必须编译期展开？

> **答案**：循环体里要用循环变量 `i` 去**索引静态 Python 元组** `store_ctxs`（`store_ctxs[i]`）。DSL 里 staged（运行期）循环变量无法索引编译期 Python 元组——元组元素类型在编译期才确定，必须编译期展开才能拿到每个 op 的具体类型。K 方向 tile 数 `k_tile_cnt` 是运行期值，没有这个问题，故 `load_tma` 可用运行期循环。

---

## 5. 综合实践

**任务**：画一张「一个 CTA 处理一个输出 tile」的完整时序图，把 mainloop 和 epilogue 串起来。

**要求**：
1. 纵向画两组 warp：「load warp」和「MMA warp」（SM90 用 `ab_load_warp_id` 分界，[gemm_sm90.py:294-295](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L294-L295)）。
2. 在 load warp 行标注：`producer_try_acquire`（预取）→ `producer_acquire`（等空+上膛）→ `copy_A/copy_B`（TMA）→ `producer_commit`(NOP) → `advance`，引用 [load_tma](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L1009-L1038)。
3. 在 MMA warp 行标注：`consumer_wait`（等 smem 满）→ 取片段 → WGMMA → 累加进 `acc`（`self.mma`，[L1306](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_sm90.py#L1306)）→ `epilogue_split_k` → `epilogue`。
4. 在两组之间画 AB pipeline 的 empty/full 握手箭头。
5. 在 epilogue 段，按 4.3.4 的时序表，标注 `store_setup → store_convert → store_r2s → s2g`，并标出 `epilogue_barrier.arrive_and_wait()` 出现的同步点。
6. 在图边用一句话注明：哪些是 `GemmBase` 共享的、哪些是 `GemmSm90` 独有的。

**验收**：你能指着图讲清「数据从 gmem 到最终写出 gmem 经过了几道屏障、几次 dtype 转换、谁在等谁」。

> 待本地验证：若有 GPU，可用 `cute.printf` 在 `load_acc_subtile`、`epi_visit_subtile`、`store_r2s` 各打一行带 `epi_idx` / `tile_coord_mnkl` 的日志（注意要写在临时文件里以满足源码落盘要求，见 u1-l4），实跑一次小 GEMM 核对时序。

---

## 6. 本讲小结

- `GemmBase` 收容各架构共享的「**非 mainloop**」部件：epilogue 驱动、split-K 收尾协议、批次旋转、tile 调度参数；mainloop（`mma` 及 warp 分工）因算子硬件相关而留在各 SM 子类。
- mainloop 是「**加载 warp 生产、MMA warp消费**」的软件流水线：加载 warp 用共享原语 `load_tma` 把 A/B 经 TMA 灌进 smem 多级槽，MMA warp `consumer_wait` 后取片段发 MMA 指令累加进 `acc`，二者经 AB pipeline 的 empty/full mbarrier 握手。
- `acc` 凑齐后，子类只需调 `self.epilogue_split_k(...)`，所有收尾复杂性（α·D+β·C+bias、激活、量化、存储）由 `GemmBase.epilogue` 驱动循环吸收。
- epilogue 用「**固定驱动 + 可插拔 EpiOp**」：`store_setup`（每 tile）→ `store_convert`（每子 tile）→ `store_r2s`（每子 tile）三件套构成 store 钩子协议；D（`DStore`）与 aux（`TileStore`）共享同一套设备侧钩子。
- `NamedBarrierGemm` 是 CTA 内跨 warp 组共享 SMEM 的命名会合点注册表，集中编号（0 号留给 `sync_threads()`），支撑 epilogue 里反复出现的 `epilogue_barrier.arrive_and_wait()` 同步。
- split-K 是 Constexpr，`split_k==1` 时所有 split-K 分支在 trace 期折叠，产物与非 split 内核逐位相同。

---

## 7. 下一步学习建议

- **u5-l2（SM90 GEMM）**：深入 `GemmSm90.mma`，看 WGMMA 指令、cluster 协作与 pingpong 主循环——本讲只点了结构，那是 mainloop 的真身。
- **u5-l3（SM100 GEMM 与 TMEM）**：看 Blackwell 如何把累加器放进 TMEM、用 2-CTA tcgen05 MMA，体会 mainloop 为何不能跨架构共享。
- **u6-l1（ComposableEpiMixin 与 EpiOp 生命周期）**：本讲把 `epi_begin` / `epi_visit_subtile` 等钩子当成黑盒，u6 系列会打开它们，讲清可组合 epilogue 如何用 `EpiOp` 词汇表实现 `α·D+β·C+bias` 及更复杂的融合。
- 建议同时回读 [gemm_base.py 的 `epilogue_split_k`](https://github.com/Dao-AILab/quack/blob/60d88082272a256fa9b3b2ab631c82cfa78337c6/quack/gemm_base.py#L642-L757) 与 `split_k_partial_commit`，为 u8-l3（Split-K 归约）打底。
