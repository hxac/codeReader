# Epilogue 与 EVT 访客树

## 1. 本讲目标

在 u2-l7/u2-l8 里我们建立了 CUTLASS 3.x 的「内核 + 主循环（collective MMA）+ epilogue」三段式模型，并在 u2-l9 用 example 49 跑通了 Hopper warp-specialized GEMM。但当时我们把 epilogue 当成一个黑盒：它接收主循环的累加器（accumulator），输出最终矩阵 D，中间到底发生了什么并没有展开。

本讲就打开这个黑盒。读完本讲你应当能够：

- 说清 **epilogue collective** 的内部结构：它如何被拆成 `load`（producer）和 `store`（consumer）两段、`FusionCallbacks` 在其中扮演什么角色、`EpilogueTile` 又是用来做什么的。
- 理解 **EVT（Epilogue Visitor Tree，epilogue 访客树）** 的组合模型：树由哪些节点构成、`visit()` 是如何自顶向下递归求值的。
- 区分 **「声明式 Fusion 标签」**（`operations.hpp` 里的 `LinearCombination`/`LinCombEltAct` 等）与 **「树的具体实现」**（`Sm90EVT<...>`），并知道 `CollectiveBuilder` 如何把前者翻译成后者。
- 能在源码里找到一个「LinearCombination + ReLU」组合，画出对应的访客树并解释数据流向，进而能动手搭出自己的融合 epilogue。

## 2. 前置知识

本讲假设你已经掌握以下概念（来自前置讲义）：

- **GEMM 三段式与 collective**（u2-l7、u2-l8）：内核外壳 + 主循环 + epilogue，主循环产出累加器 `acc`。
- **warp specialization 的 producer/consumer**（u2-l8、u2-l9）：Hopper 内核把「搬数据」和「算」分流给不同的 warp/warp group，靠异步流水线同步。
- **CuTe 的 Array/Tensor/recast**（u2-l2、u2-l3）：epilogue 大量使用 `Array<Element, N>` 片段（fragment）和 `recast` 做向量化。
- **C++ 模板元编程基础**：偏特化、`tuple`、`template template parameter`（模板模板参数）、`enable_if`。EVT 是重度模板元代码，但本讲只讲「怎么用、怎么读」，不要求你能手写最底层的元函数。

两个本讲会用到的术语：

- **fragment（片段）**：每个线程在寄存器里持有的一小块数据，通常是 `Array<T, FragmentSize>`。epilogue 的核心运算都发生在寄存器片段上。
- **accumulator（累加器，acc）**：主循环 wgmma 算完后留在寄存器里的 `D = A·B` 结果片段，是 epilogue 的唯一「真正的输入」。源/C（`ElementC`）和 α/β 都是 epilogue 自己再去搬的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`include/cutlass/epilogue/collective/sm90_epilogue_tma_warpspecialized.hpp`](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/collective/sm90_epilogue_tma_warpspecialized.hpp) | SM90 epilogue collective 的本体。定义 `load()`/`store()` 两段、`EpilogueTile` 子分块、共享内存与 TMA 描述符，并在固定入口点调用 `FusionCallbacks`。 |
| [`include/cutlass/epilogue/fusion/sm90_visitor_tma_warpspecialized.hpp`](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_visitor_tma_warpspecialized.hpp) | EVT 的「骨架」：回调包装器（`ProducerLoadCallbacksImpl`/`ConsumerStoreCallbacksImpl`）、参数包（`ProducerLoadArgs`/`ConsumerStoreArgs`）、多 op 聚合基类 `Sm90VisitorImplBase`/`Sm90VisitorImpl`，以及三种组合原语 `Sm90TreeVisitor`/`Sm90SplitTreeVisitor`/`Sm90TopologicalVisitor`。 |
| [`include/cutlass/epilogue/fusion/sm90_callbacks_tma_warpspecialized.hpp`](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_callbacks_tma_warpspecialized.hpp) | EVT 的「菜谱库」：定义面向用户的别名 `Sm90EVT`，以及 `Sm90LinearCombination`、`Sm90LinCombEltAct` 等预置树；并用 `FusionCallbacks<策略, 标签, ...>` 偏特化把声明式标签翻译成树。 |
| [`include/cutlass/epilogue/fusion/operations.hpp`](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/operations.hpp) | 声明式融合操作标签层：`FusionOperation` 基类与 `LinearCombination`/`LinCombEltAct`/`LinCombPerRowBiasEltAct` 等标签，只描述「要什么」不描述「怎么做」。 |
| [`include/cutlass/epilogue/fusion/sm90_visitor_compute_tma_warpspecialized.hpp`](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_visitor_compute_tma_warpspecialized.hpp) | 计算节点 `Sm90Compute<ComputeFn, ...>`：N 元逐元素运算，是树的「内部节点」。 |
| [`include/cutlass/epilogue/fusion/sm90_visitor_load_tma_warpspecialized.hpp`](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_visitor_load_tma_warpspecialized.hpp) / [`.../sm90_visitor_store_tma_warpspecialized.hpp`](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_visitor_store_tma_warpspecialized.hpp) | 叶子节点：`Sm90AccFetch`（取 acc）、`Sm90SrcFetch`（取 C）、`Sm90ScalarBroadcast`（α/β 标量）、`Sm90RowBroadcast`/`Sm90ColBroadcast`（按行/列 bias），以及 `Sm90AuxLoad`/`Sm90AuxStore`（辅助张量读写）。 |
| [`include/cutlass/epilogue/thread/activation.h`](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/thread/activation.h) | 激活函子 `ReLu`/`Sigmoid`/`TanH`/`LeakyReLU` 等，可作为 `Sm90Compute` 的 `ComputeFn`。 |
| [`examples/49_hopper_gemm_with_collective_builder/49_collective_builder.cu`](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/49_hopper_gemm_with_collective_builder/49_collective_builder.cu) | 官方示例：同时演示「自定义 EVT 树」和「声明式标签」两条路径如何喂给 `CollectiveBuilder`。 |
| [`examples/113_hopper_gemm_activation_fusion/sm90_visitor_gated_act.hpp`](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/113_hopper_gemm_activation_fusion/sm90_visitor_gated_act.hpp) | 进阶示例：用户自己手写一个嵌套 `Sm90EVT` 树实现 gated activation。 |

> 记忆口诀：**collective 是「舞台」（搬数据 + 调入口点），visitor 是「剧本」（每个节点算什么），operations 是「菜单」（点哪个菜名）**。

---

## 4. 核心概念与源码讲解

### 4.1 Epilogue Collective 概览

#### 4.1.1 概念说明

epilogue（收尾）是 GEMM 内核里「把累加器变成最终输出 D」的那一段。最朴素的需求是：

\[
D = \alpha\cdot \text{acc} + \beta\cdot C
\]

但实际业务里，我们几乎从不需要「朴素」的 epilogue——训练时要加 bias、做激活（ReLU/GELU）、做残差相加（residual）、把 FP32 缩放回 FP8……如果每一步都单独启动一个内核读写显存，带宽会立刻成为瓶颈。所以现代 GEMM 库都追求 **epilogue 融合（fusion）**：把这些后处理「融合」进写回 D 的那一个内核里，让中间结果尽量留在寄存器/共享内存。

CUTLASS 3.x 把这件事拆成两层：

- **`CollectiveEpilogue`**：固定的「舞台调度」。它负责把 C 从显存搬进共享内存、把算好的 D 从寄存器写回显存、管理 TMA 异步流水线。它**不算数学**——具体的 α·acc+β·C、激活、bias 全部委托给一个叫 `FusionCallbacks` 的部件。
- **`FusionCallbacks`（EVT 树）**：可替换的「剧本」。它接收累加器片段和（可选的）C 片段，吐出 D 片段。本讲的主角 EVT，就是构造 `FusionCallbacks` 的方式。

这就是 CUTLASS 3.x epilogue 的核心设计思想：**「搬运调度」与「数学计算」彻底解耦**。你换融合公式时，只动 `FusionCallbacks`，不动 collective 的一行代码。

#### 4.1.2 核心流程

SM90 的 `CollectiveEpilogue` 沿用 warp specialization，分两段（对应 u2-l9 讲过的 producer/consumer）：

```text
producer warp (DMA warp) —— 调用 collective.load()
  for 每个 EpilogueTile 子块:
      producer_acquire(缓冲锁)
      fusion_callbacks.get_producer_load_callbacks().step(...)   # 在此发辅助 TMA 载入(Aux)
      若需要 C:  TMA load  C: gmem -> smem
      producer_commit
  return 流水线状态

consumer warp group (math warp group) —— 调用 collective.store()
  for 每个 EpilogueTile 子块:
      consumer_wait(等 C 到达 smem)
      若需要 C:  smem -> 寄存器 tSR_rC
      cst_callbacks.previsit(...)                                # smem 广播(如 per-row bias)
      tRS_rCompute_frg = cst_callbacks.visit(tRS_rAcc_frg, ...)  # ★ 数学就在这里发生
      cst_callbacks.reduce(...)                                  # 跨线程归约(如 bias 的 dBias)
      rD = NumericArrayConverter(tRS_rCompute)                   # 转成输出类型
      reg -> smem  tRS_rD
      cst_callbacks.postreduce(...)                              # smem 辅助写
      TMA store D: smem -> gmem
      cst_callbacks.tma_store(...)                               # 辅助 TMA 写(Aux)
```

几个关键点：

1. **唯一的数学入口是 `cst_callbacks.visit()`**：它吃进累加器片段 `tRS_rAcc_frg`，吐出输出片段 `tRS_rCompute_frg`。α/β/bias/激活全都封装在这个 `visit` 里。
2. **`EpilogueTile` 是「子分块」**：CTA 算的输出块大小是 `CtaTileMNK`（如 128×128），但写回时可以拆成更小的 `EpilogueTile`（如 64×64）逐块处理，目的是控制寄存器/共享内存占用，并和 TMA 拷贝粒度对齐。两者必须满足整除关系。
3. **`FusionCallbacks` 决定要不要搬 C**：通过 `is_C_load_needed()` / `is_producer_load_needed()` 两个查询，树可以告诉 collective「我这个融合不需要 C（比如纯 `α·acc`），就别浪费带宽搬 C 了」。

#### 4.1.3 源码精读

`CollectiveEpilogue` 的模板参数里，`FusionCallbacks_` 就是我们要替换的 EVT 树类型：

[include/cutlass/epilogue/collective/sm90_epilogue_tma_warpspecialized.hpp:61-100](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/collective/sm90_epilogue_tma_warpspecialized.hpp#L61-L100) — `class CollectiveEpilogue<Sm90TmaWarpSpecialized<...>, ..., FusionCallbacks_, ...>` 的偏特化，把第 8 个模板参数 `FusionCallbacks_` 起别名 `using FusionCallbacks = FusionCallbacks_;`。

collective 把 `FusionCallbacks` 作为成员持有：

[include/cutlass/epilogue/collective/sm90_epilogue_tma_warpspecialized.hpp:950-953](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/collective/sm90_epilogue_tma_warpspecialized.hpp#L950-L953) — `Params const& params; FusionCallbacks fusion_callbacks; int issued_stores = 0;`，说明融合逻辑是 collective 的一个成员子对象。

producer 段 `load()` 在循环里把控制权交给融合回调：

[include/cutlass/epilogue/collective/sm90_epilogue_tma_warpspecialized.hpp:472-504](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/collective/sm90_epilogue_tma_warpspecialized.hpp#L472-L504) — 先 `fusion_callbacks.get_producer_load_callbacks(pld_args)` 拿到 `pld_callbacks`，再在每个子块里调 `pld_callbacks.begin()/step()/end()`；`step()` 内部既触发 Aux 的 TMA 载入，也发出 C 的 TMA 载入。

consumer 段 `store()` 是数学发生的地方，看 `visit` 的两个调用点：

[include/cutlass/epilogue/collective/sm90_epilogue_tma_warpspecialized.hpp:698-706](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/collective/sm90_epilogue_tma_warpspecialized.hpp#L698-L706) — 构造 `ConsumerStoreArgs`（携带坐标张量、残差、源片段 `tCrC` 等），调 `fusion_callbacks.get_consumer_store_callbacks<RefSrc>(cst_args)` 得到 `cst_callbacks`，并据此推断输出寄存器的元素类型。

[include/cutlass/epilogue/collective/sm90_epilogue_tma_warpspecialized.hpp:840-856](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/collective/sm90_epilogue_tma_warpspecialized.hpp#L840-L856) — `tRS_rCompute_frg(...) = cst_callbacks.visit(tRS_rAcc_frg_mn(...), epi_v, epi_m, epi_n);`，**累加器片段 `tRS_rAcc_frg` 进，输出片段 `tRS_rCompute_frg` 出**。这就是整段 epilogue 唯一的「数学」调用。

`EpilogueTile` 的整除约束（解释为什么子分块不能随便取）：

[include/cutlass/epilogue/collective/sm90_epilogue_tma_warpspecialized.hpp:129-130](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/collective/sm90_epilogue_tma_warpspecialized.hpp#L129-L130) — `EPI_TILE_M must divide CTA_M`、`EPI_TILE_N must divide CTA_N` 的 `static_assert`。

#### 4.1.4 代码实践

**实践目标**：在不编译的前提下，验证「collective 不关心融合公式，只调入口点」这一论断。

**操作步骤**：

1. 打开 [`sm90_epilogue_tma_warpspecialized.hpp`](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/collective/sm90_epilogue_tma_warpspecialized.hpp)。
2. 在 `store()` 方法（L527 起）里搜索所有 `cst_callbacks.` 出现的位置。
3. 列出 collective 对 `FusionCallbacks` 调用的全部「入口点方法」。

**需要观察的现象**：你会看到一组固定的方法名：`begin`、`begin_sync_needed`、`begin_loop`、`previsit`、`visit`、`reduce`、`postreduce`、`tma_store`、`end_loop`、`end`。

**预期结果**：约 9~10 个入口点，且 **只有 `visit` 的返回值被赋给输出片段**，其余入口点返回 `void`（仅用于副作用：发 TMA、做归约、广播）。这印证了「数学只在 `visit` 里」。

**结论性结果**：待本地验证（你只需在编辑器里数一下即可）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `FusionCallbacks` 换成「不需要 C」的树（比如纯 `α·acc`），collective 会浪费带宽去搬 C 吗？
**答案**：不会。collective 通过 `is_C_load_needed()` 查询树（[L473](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/collective/sm90_epilogue_tma_warpspecialized.hpp#L473)、[L701](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/collective/sm90_epilogue_tma_warpspecialized.hpp#L701)），只有当树里存在读取 C 的节点（`Sm90SrcFetch`）时才搬 C。这是「数学与调度解耦」带来的直接收益。

**练习 2**：`EpilogueTile` 和 `CtaTileMNK` 是什么关系？
**答案**：`CtaTileMNK = (CTA_M, CTA_N, CTA_K)` 是一个 CTA 负责的输出块大小；`EpilogueTile = (EPI_M, EPI_N)` 是 epilogue 写回时的子分块，必须整除 `CTA_M/CTA_N`。把大块切成小块写回，是为了控制寄存器压力并与 TMA 粒度对齐。

---

### 4.2 EVT 访客树模型

#### 4.2.1 概念说明

`FusionCallbacks` 的具体形态可以任意复杂。CUTLASS 3.x 给出的统一构造方式是 **EVT（Epilogue Visitor Tree，epilogue 访客树）**：把融合公式表达成一棵「表达式树」，每个节点是一个小的、可复用的「访问者（visitor）」，整棵树的 `visit()` 递归求值就等价于计算融合公式。

一棵 EVT 由三类节点组成：

- **叶子节点（leaf，零元/nullary）**：不依赖子节点，直接「产出」一个片段。例如：
  - `Sm90AccFetch`：产出主循环的累加器 `acc`。
  - `Sm90SrcFetch<C>`：产出源矩阵 C 的片段。
  - `Sm90ScalarBroadcast<S>`：产出标量广播（α、β），支持指针 + 步长（按 batch 变化）。
  - `Sm90RowBroadcast` / `Sm90ColBroadcast`：按行 / 按列广播一个向量（bias）。
- **内部/计算节点（compute，N 元）**：吃进 N 个子节点的输出，做一个逐元素运算，产出新片段。即 `Sm90Compute<ComputeFn, ...>`，其中 `ComputeFn` 是函子，如 `cutlass::multiplies`（逐元素乘）、`cutlass::homogeneous_multiply_add`（`a*b+c`）、或激活函子 `ReLu`。
- **组合原语**：把节点拼成树或 DAG，下面 4.2.2 详述。

例如 `D = α·acc + β·C` 可以写成：

```text
        multiply_add (根, 三元)
       /            |          \
  ScalarBroadcast  SrcFetch    multiplies (二元)
     (β)            (C)        /          \
                          ScalarBroadcast  AccFetch
                              (α)           (acc)
```

每个内部节点的 `visit()` 先递归调用各子树的 `visit()` 拿到子结果，再用自己的 `ComputeFn` 合并。

#### 4.2.2 核心流程

EVT 的拼装靠 `sm90_visitor_tma_warpspecialized.hpp` 里的一组组合原语。先看「聚合基类」，再看「树/DAG 原语」。

**(a) 聚合多个 op**：一棵树是「多个节点 op 的集合」。`Sm90VisitorImplBase<Ops...>` 把 N 个 op 聚成一个对象，统一管理它们的 `Arguments/Params/SharedStorage` 元组、统一转发 host 端的 `to_underlying_arguments/can_implement/get_workspace_size/initialize_workspace`，并按每个 op 的需求切分 workspace：

[include/cutlass/epilogue/fusion/sm90_visitor_tma_warpspecialized.hpp:482-584](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_visitor_tma_warpspecialized.hpp#L482-L584) — 注意 `using Arguments = tuple<typename Ops::Arguments...>;`、`using Params = tuple<typename Ops::Params...>;`，所有 host 函数都用 `transform_apply` 逐 op 处理并在它们之间累加 workspace 偏移。

`Sm90VisitorImpl<Ops...>` 在此基础上提供 **device 端回调工厂** 和 **运行期查询**：`is_producer_load_needed` / `is_C_load_needed` 对所有 op 做「或」聚合；`get_producer_load_callbacks` / `get_consumer_store_callbacks` 把每个 op 各自的回调打包成一个 `ProducerLoadCallbacksImpl<tuple>` / `ConsumerStoreCallbacksImpl<tuple>`：

[include/cutlass/epilogue/fusion/sm90_visitor_tma_warpspecialized.hpp:636-669](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_visitor_tma_warpspecialized.hpp#L636-L669) — `transform_apply(ops, ...)` 对每个 op 调一次工厂，再 `make_tuple` 包起来。

**(b) 回调包装器广播入口点**：`ProducerLoadCallbacksImpl<CallbacksTuple>` 和 `ConsumerStoreCallbacksImpl<CallbacksTuple>` 各持有一个「每节点回调」的 `tuple`，每个入口点（`begin/step/.../visit/reduce/...`）都用 `for_each` 把调用广播给**每一个**节点回调：

[include/cutlass/epilogue/fusion/sm90_visitor_tma_warpspecialized.hpp:180-305](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_visitor_tma_warpspecialized.hpp#L180-L305) — 例如 `previsit` 对 `callbacks_tuple` 里每个回调都调一次 `callbacks.previsit(...)`。注意 `visit` 在基类里是 `= delete`（L227-231），**必须由具体的组合原语重写**——这正是「树」语义注入的地方。

**(c) 树原语 `Sm90TreeVisitor`**：这是 EVT 的核心。它继承 `Sm90VisitorImpl<ChildOps..., NodeOp>`（**子节点在前、节点 op 在最后**），并重写 `get_consumer_store_callbacks` 返回一个自定义的 `ConsumerStoreCallbacks`，其 `visit()` 实现「先递归求所有子树，再把子结果喂给节点 op」：

[include/cutlass/epilogue/fusion/sm90_visitor_tma_warpspecialized.hpp:710-760](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_visitor_tma_warpspecialized.hpp#L710-L760) — 关键是 L736-747 的 `visit`：用 `tapply` 对前 `R-1` 个子回调分别调 `visit(frg_acc, epi_v, epi_m, epi_n)`（子节点必须是零元，注释 L740 写明），收集成 `frg_inputs...`，再对最后一个（节点 op）调 `get<Rm1>(callbacks_tuple).visit(frg_acc, epi_v, epi_m, epi_n, frg_inputs...)`。这就是「树」的递归求值。

> **反直觉点**：注意子节点在前、节点 op 在后（`<ChildOps..., NodeOp>`），但「节点 op 是树的根」。模板参数的物理排列顺序 ≠ 树的逻辑根位置。

**(d) DAG 原语**：当一棵树不够用（比如同一个中间结果要喂给多个输出，或者有共享子图）时，还有两个原语：

- `Sm90SplitTreeVisitor<InputTree, OutputTree, AuxOutTrees...>`：先算公共输入树，结果当作新的 `frg_acc` 喂给输出树和若干辅助输出树（[L770-809](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_visitor_tma_warpspecialized.hpp#L770-L809)）。对应「一个输入 → 多个输出」的常见 DAG。
- `Sm90TopologicalVisitor<ElementCompute, EdgeTuple, Ops...>`：最通用的 DAG，节点按拓扑序排列，`EdgeTuple` 显式给出每个节点的「孩子下标」（[L812-885](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_visitor_tma_warpspecialized.hpp#L812-L885)）。所有中间结果统一转成 `ElementCompute` 类型。

求值过程的「代数」可以这么写：对一个树节点 \( v \)（ComputeFn 为 \( f_v \)、孩子为 \( c_1,\dots,c_n \)），其输出为

\[
\text{out}(v) = f_v\bigl(\text{out}(c_1), \text{out}(c_2), \dots, \text{out}(c_n)\bigr)
\]

叶子节点的 \(\text{out}\) 直接取自硬件数据（acc / C / 标量 / bias 向量）。整棵树的根的输出就是 `visit()` 的返回值，被 collective 写到输出片段。

#### 4.2.3 源码精读

面向用户的别名 `Sm90EVT`，正是 `Sm90TreeVisitor` 的简写：

[include/cutlass/epilogue/fusion/sm90_callbacks_tma_warpspecialized.hpp:57-58](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_callbacks_tma_warpspecialized.hpp#L57-L58) — `template <class NodeOp, class... ChildOps> using Sm90EVT = Sm90TreeVisitor<NodeOp, ChildOps...>;`。所以**写 EVT 树就是写 `Sm90EVT<节点op, 孩子1, 孩子2, ...>`**。

最简单的融合 `D = α·acc` 的整棵树（这是菜谱库里 `ScaledAcc` 的实现）：

[include/cutlass/epilogue/fusion/sm90_callbacks_tma_warpspecialized.hpp:79-87](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_callbacks_tma_warpspecialized.hpp#L79-L87) — `Sm90EVT<Sm90Compute<multiplies, ElementOutput, ElementCompute, RoundStyle>, Sm90ScalarBroadcast<...>, Sm90AccFetch>`，即「`multiplies(α, acc)`」：节点 op 是 `multiplies`，两个叶子是 α 标量和 acc。

计算节点 `Sm90Compute` 的定义与「可带超参的激活」支持：

[include/cutlass/epilogue/fusion/sm90_visitor_compute_tma_warpspecialized.hpp:84-109](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_visitor_compute_tma_warpspecialized.hpp#L84-L109) — `Sm90Compute<template<class> class ComputeFn, ElementOutput, ElementCompute, RoundStyle>`。`ComputeFn` 是**模板模板参数**（所以 `ReLu`、`multiplies` 这种 `template<class> class` 函子才能传进去）；它的 `Arguments` 自动从 `ComputeFn::Arguments` 推导（L95-104），这是激活函子（如带 leaky 系数的 `LeakyReLU`）能携带超参的机制。

零元叶子节点 `Sm90AccFetch`（产出 acc 片段）：

[include/cutlass/epilogue/fusion/sm90_visitor_load_tma_warpspecialized.hpp:62-85](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_visitor_load_tma_warpspecialized.hpp#L62-L85) — `struct Sm90AccFetch : Sm90VisitorImpl<> {};`，继承空 op 列表，是个纯叶子。

#### 4.2.4 代码实践

**实践目标**：把 `D = α·acc + β·C` 的公式「翻译」成一棵 `Sm90EVT` 类型。

**操作步骤**：

1. 阅读 [`sm90_callbacks_tma_warpspecialized.hpp:174-190`](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_callbacks_tma_warpspecialized.hpp#L174-L190) 里官方的 `Sm90LinearCombination` 定义。
2. 对照 4.2.1 里画的那棵树，把每个数学运算映射到一个 `Sm90Compute`、每个操作数映射到一个叶子。
3. 自己在纸上写出「`D = sigmoid(α·acc)`」（注意：sigmoid 是一元激活）对应的 `Sm90EVT` 类型。

**需要观察的现象**：你应该发现 `Sm90LinearCombination` 用了**嵌套** `Sm90EVT`——外层 `multiply_add` 是三元（β, C, α·acc），其中一个孩子本身就是一棵 `Sm90EVT<multiplies, α, acc>` 子树。

**预期结果**：`D = sigmoid(α·acc)` 对应

```cpp
Sm90EVT<
  Sm90Compute<sigmoid, ElementOutput, ElementCompute, RoundStyle>,   // 一元激活
  Sm90EVT<Sm90Compute<multiplies, ElementCompute, ElementCompute, RoundStyle>,
          Sm90ScalarBroadcast<ElementScalar>,   // α
          Sm90AccFetch                          // acc
  >
>;
```

**结论性结果**：待本地验证（与同伴交叉检查你的类型写法；sigmoid 函子可取自 `cutlass/epilogue/thread/activation.h` 或 `cute/numeric/math.hpp`）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ConsumerStoreCallbacksImpl` 基类里把 `visit` 标成 `= delete`？
**答案**：因为「如何把多个子节点的结果合并」是树/DAG 的语义，只有具体的组合原语（`Sm90TreeVisitor` 等）知道子节点个数和合并方式。基类只负责「把入口点广播给每个节点」，无法定义统一的 `visit`，故留空 (`= delete`) 强制子类重写。

**练习 2**：`Sm90EVT<NodeOp, ChildOps...>` 的模板参数里，`NodeOp` 是树的根还是叶？
**答案**：是**根**（节点 op）。物理排列上是 `<NodeOp, ChildOps...>`（第一个是根），但在继承的 `Sm90VisitorImpl<ChildOps..., NodeOp>` 里被挪到了最后，因为聚合基类按「顺序持有 op」、而 `Sm90TreeVisitor::visit` 用 `get<Rm1>` 取最后一个当作根。

---

### 4.3 Fusion visitor 与 callback

#### 4.3.1 概念说明

4.2 讲了「树怎么求值」，本模块讲两件事：

1. **一个独立的节点 op 如何「插」进 collective 的入口点**——即每个 op 要实现哪些方法、callback 在不同入口点做什么。
2. **声明式标签如何被翻译成树**——即 `operations.hpp` 的标签（菜单）和 `callbacks.hpp` 的 `FusionCallbacks<...>` 偏特化（厨房）之间的关系，以及 `CollectiveBuilder` 怎么串起来。

先说节点 op 的契约。一个可参与 EVT 的节点 op 通常需要提供：

| 成员 | 作用 | host/device |
| --- | --- | --- |
| `Arguments` | host 端用户填的参数（如标量值、指针、步长） | host |
| `Params` | device 端用的参数（由 `Arguments` 转换而来） | both |
| `SharedStorage` | 该节点需要的共享内存（如 bias 的 smem 缓冲） | both |
| `to_underlying_arguments` | `Arguments → Params`，并切分 workspace | host |
| `can_implement` / `get_workspace_size` / `initialize_workspace` | 可行性检查与 workspace 初始化 | host |
| `is_producer_load_needed` / `is_C_load_needed` | 告诉 collective 是否要发 TMA / 搬 C | device |
| `get_producer_load_callbacks` | 返回该节点在 producer 段的回调 | device |
| `get_consumer_store_callbacks` | 返回该节点在 consumer 段的回调（含 `visit`） | device |

不同节点按「职责」分到三个文件：

- **compute**（`sm90_visitor_compute_*.hpp`）：`Sm90Compute`。`is_*_load_needed` 都返回 false，producer 回调为空，只在 consumer 的 `visit` 里做计算。
- **load**（`sm90_visitor_load_*.hpp`）：`Sm90AccFetch`/`Sm90SrcFetch`/`Sm90ScalarBroadcast`/`Sm90RowBroadcast`/`Sm90ColBroadcast`/`Sm90AuxLoad`。它们要发 TMA 或读寄存器，因此 `get_producer_load_callbacks` 与 `previsit` 里有实际动作。
- **store**（`sm90_visitor_store_*.hpp`）：`Sm90AuxStore`。它把额外结果（如 layernorm 的均值/方差、aux 输出）写回显存，因此动用 `reduce`/`postreduce`/`tma_store` 入口点。

再说「标签 → 树」的翻译。`operations.hpp` 里的 `LinearCombination`、`LinCombEltAct` 等都是 **纯描述性标签**——它们继承 `FusionOperation`，只是用一组 `static constexpr` 元数据声明「我支持源 C / 支持激活 / 支持逐行 bias / ……」，**没有任何实现代码**。真正实现这些公式的是 `callbacks.hpp` 里的偏特化：

```cpp
template <class DispatchPolicy, ..., class FusionOp, ...>
struct FusionCallbacks<DispatchPolicy, FusionOp, ...> : <某棵 Sm90EVT 树> { ... };
```

即「给定调度策略 + 一个标签，得到对应的 EVT 树类型」。`EpilogueCollectiveBuilder` 拿到你传的标签，实例化 `FusionCallbacks<策略, 标签, ...>` 就完成了翻译。

最后，用户填的 `Arguments` 是**扁平命名**的字段（`alpha`、`beta`、`alpha_ptr`、`dAlpha`……），而树的 `Impl::Arguments` 是**嵌套 tuple**（按树的结构组织）。每个 `FusionCallbacks` 偏特化里都提供一个 `operator Impl::Arguments() const`，把扁平字段重新打包成树的嵌套结构。这是 EVT 对用户友好的关键：**你按名字填字段，库帮你映射到树**。

#### 4.3.2 核心流程

标签到执行的完整链路：

```text
用户侧
  Gemm = ... CollectiveBuilder<..., FusionOp=LinCombEltAct<ReLu,...> >::CollectiveOp ...
        │
        ▼ (builder 实例化)
FusionCallbacks<Sm90TmaWarpSpecialized<...>, LinCombEltAct<ReLu,...>, CtaTile, EpiTile>
        │  (callbacks.hpp 偏特化)
        ▼
Sm90LinCombEltAct<ReLu,...> = Sm90EVT<Sm90Compute<ReLu,...>, Sm90LinearCombination<...>>
        │  (运行期)
        ▼
collective.store() → cst_callbacks.visit(acc) → 树递归求值 → 输出片段
```

`ConsumerStoreArgs` 是 collective 传给每个节点回调的「上下文背包」，里面装了节点可能用到的几乎所有东西：

[include/cutlass/epilogue/fusion/sm90_visitor_tma_warpspecialized.hpp:417-457](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_visitor_tma_warpspecialized.hpp#L417-L457) — 含 `problem_shape_mnkl`、`tile_coord_mnkl`、`tiled_mma`、`epi_tile`、坐标张量 `cD`、残差 `residue_cD`、线程局部坐标 `tCcD` / `residue_tCcD`、**源片段引用 `tCrC`**（C 的寄存器视图）、`thread_idx`。bias/aux 这类需要按坐标取数的节点就靠这些张量定位「当前线程该读哪个元素」。

扁平 `Arguments` → 嵌套树 `Arguments` 的映射示例（`LinearCombination`）：

[include/cutlass/epilogue/fusion/sm90_callbacks_tma_warpspecialized.hpp:216-240](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_callbacks_tma_warpspecialized.hpp#L216-L240) — 用户只填 `alpha/beta/alpha_ptr/beta_ptr/dAlpha/dBeta`，`operator Impl::Arguments()` 把它们重组成 `{ {beta,beta_ptr,dBeta}, /*C*/ {}, { {alpha,alpha_ptr,dAlpha}, /*acc*/{}, /*multiplies*/{} }, /*multiply_add*/{} }`——这个花括号嵌套结构和 `Sm90LinearCombination` 的树结构**一一对应**（注意注释里 `// ternary op`、`// binary op` 标明了每层对应树的哪个节点）。

#### 4.3.3 源码精读

声明式标签基类与一个典型标签：

[include/cutlass/epilogue/fusion/operations.hpp:52-91](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/operations.hpp#L52-L91) — `struct FusionOperation { ... };` 全是元数据字段（`IsSourceSupported`、`IsEltActSupported`、`IsPerRowBiasSupported`……）。

[include/cutlass/epilogue/fusion/operations.hpp:122-135](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/operations.hpp#L122-L135) — `LinCombEltAct<ActivationFn, ...>` 继承 `LinearCombination` 并加 `using ActivationFn = ...; static constexpr bool IsEltActSupported = true;`，仅此而已——它只是「声明我要线性组合 + 激活」，不含任何 `visit` 实现。

「标签 → 树」的翻译（`LinCombEltAct` 的偏特化）：

[include/cutlass/epilogue/fusion/sm90_callbacks_tma_warpspecialized.hpp:345-399](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_callbacks_tma_warpspecialized.hpp#L345-L399) — `FusionCallbacks<Sm90TmaWarpSpecialized<...>, fusion::LinCombEltAct<...>, ...>` 继承自 `Sm90LinCombEltAct<...>`（即一棵具体的 EVT 树），并提供扁平 `Arguments` + `operator Impl::Arguments()`。注意 L381-382 把激活函子可能携带的超参（`ActivationArguments`）也单独暴露给用户。

`Sm90LinCombEltAct` 这棵树本身：

[include/cutlass/epilogue/fusion/sm90_callbacks_tma_warpspecialized.hpp:340-343](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_callbacks_tma_warpspecialized.hpp#L340-L343) — `Sm90EVT<Sm90Compute<ActivationFn,...>, Sm90LinearCombination<...>>`，即「在一棵 `LinearCombination` 树之上，再套一个激活节点」。这就是「LinearCombination + 激活」的标准写法。

激活函子来自 `activation.h`（可作为 `ComputeFn`）：

[include/cutlass/epilogue/thread/activation.h:144-145](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/thread/activation.h#L144-L145) — `template <typename T> struct ReLu { ... };`，是 `template<class> class` 形式，正好能传给 `Sm90Compute<ReLu, ...>`。

#### 4.3.4 代码实践

**实践目标**：跟踪一条「标签 → 树 → 入口点」的完整链路，确认每一步在源码里的位置。

**操作步骤**：

1. 从 [`operations.hpp:122`](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/operations.hpp#L122) 的 `LinCombEltAct` 标签出发。
2. 跳到 [`callbacks.hpp:360`](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_callbacks_tma_warpspecialized.hpp#L360) 的 `FusionCallbacks<..., LinCombEltAct<...>, ...>` 偏特化，确认它继承 `Sm90LinCombEltAct`。
3. 再跳到 [`callbacks.hpp:340`](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_callbacks_tma_warpspecialized.hpp#L340) 的 `Sm90LinCombEltAct`，看到它 = `Sm90EVT<Sm90Compute<ActivationFn,...>, Sm90LinearCombination<...>>`。
4. 最后回到 [`visitor_*.hpp:710`](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_visitor_tma_warpspecialized.hpp#L710) 的 `Sm90TreeVisitor::visit`，确认求值时「先算 `LinearCombination` 子树，再把结果喂给 `ActivationFn`」。

**需要观察的现象**：四跳之后，你应当能说清「我传一个 `LinCombEltAct<ReLu>` 标签，最终在 GPU 上执行的 `visit` 是怎么递归调用的」。

**预期结果**：链路完整闭合——标签 → 偏特化 → 树类型 → `visit` 递归。

**结论性结果**：待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`operations.hpp` 里的 `LinearCombination` 标签和 `callbacks.hpp` 里的 `Sm90LinearCombination` 是同一个东西吗？
**答案**：不是。前者是**声明式标签**（`fusion::LinearCombination<...>`，只含元数据，无实现）；后者是**具体 EVT 树类型**（`Sm90EVT<...>` 的别名，含 `visit` 实现）。二者靠 `FusionCallbacks<策略, 标签, ...>` 偏特化连接。

**练习 2**：用户填的 `Arguments::alpha` 是个标量，它是怎么变成「每个线程在每个子块都能取到」的广播值的？
**答案**：`Sm90ScalarBroadcast` 节点在 host 端把标量/指针/步长存进 `Params`，device 端在 `visit` 里让每个线程都读同一个标量（或按 `Stride` 与坐标算出当前 batch 的值）。collective 不参与这个过程——它只负责调 `visit`。

---

### 4.4 常用 epilogue 融合模式

#### 4.4.1 概念说明

掌握了树模型与翻译机制后，我们来看「菜谱库」里最常用的几道菜，以及实际写代码时的两条路径。

**预置融合模式（按公式复杂度递增）**：

| 标签（operations.hpp） | 公式 | 对应树（callbacks.hpp） |
| --- | --- | --- |
| `ScaledAcc` | \(D = \alpha\cdot\text{acc}\) | `Sm90EVT<multiplies, α, acc>` |
| `LinearCombination` | \(D = \alpha\cdot\text{acc}+\beta\cdot C\) | `Sm90LinearCombination` |
| `LinCombEltAct<Act>` | \(D = \text{Act}(\alpha\cdot\text{acc}+\beta\cdot C)\) | `Sm90LinCombEltAct` |
| `LinCombPerRowBiasEltAct<Act>` | \(D = \text{Act}(\alpha\cdot\text{acc}+\beta\cdot C + b_m)\) | `Sm90LinCombPerRowBiasEltAct`（加 `Sm90RowBroadcast` 叶子） |
| `LinCombPerRowBiasEltActAux<Act>` | 同上，且把激活前/后的中间结果额外写回一个 Aux 张量 | 加 `Sm90AuxStore` |
| `LinCombDeEltAct<Act>` | \(D = \text{Act}'(\alpha\cdot\text{acc}+\beta\cdot C, Z)\)（反向激活，含 Aux 输入） | 加 `Sm90AuxLoad` |

注意右列的「树」本身就是用更小的子树组合出来的，例如 `LinCombPerRowBiasEltAct` 只是在 `LinCombEltAct` 的基础上把根的 `multiply_add` 换成四元、再加一个 `RowBroadcast` 叶子。

**两条使用路径**：

1. **声明式（推荐）**：把上表的某个**标签**作为最后一个模板参数传给 `cutlass::epilogue::collective::CollectiveBuilder`。builder 会自动推断 stage 数、copy atom/layout 等繁琐细节，还会用标签的元数据做静态查询。这是 90% 场景下的做法。
2. **自定义树**：直接构造一个 `Sm90EVT<...>`（或 `Sm90SplitTreeVisitor`/`Sm90TopologicalVisitor`）类型，传给 builder。代价是你得自己保证树的正确性（尤其是 `Arguments` 的嵌套结构要和树匹配），收益是能表达菜谱库里没有的公式。

> 经验法则：先查 [`operations.hpp`](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/operations.hpp) 里有没有现成标签；没有再考虑自定义树，并参考 [`callbacks.hpp`](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_callbacks_tma_warpspecialized.hpp) 里现有标签是怎么搭的。

#### 4.4.2 核心流程

以 example 49 为例，它**同时**演示了两条路径，并用一个编译期开关 `UseCustomEVT` 切换：

```text
using CustomEVT        = Sm90EVT<...>;                                  // 自定义树
using DefaultOperation = fusion::LinearCombination<...>;              // 声明式标签

using CollectiveEpilogue = CollectiveBuilder< ...,
    cute::conditional_t<UseCustomEVT, CustomEVT, DefaultOperation>     // 二选一
  >::CollectiveOp;
```

无论走哪条，builder 最终都得到一个 `CollectiveEpilogue`，其 `FusionCallbacks` 成员类型就是那棵树（或标签翻译出的树）。后续 `GemmUniversal` / `GemmUniversalAdapter` 的组装与 u2-l9 完全一致。

#### 4.4.3 源码精读

example 49 的「自定义 EVT 树」与「标签」并排写法：

[examples/49_hopper_gemm_with_collective_builder/49_collective_builder.cu:291-305](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/49_hopper_gemm_with_collective_builder/49_collective_builder.cu#L291-L305) — `CustomEVT` 用 `Sm90Compute<homogeneous_multiply_add>`（`a*b+c`）做根、`Sm90ScalarBroadcast`（β）、`Sm90SrcFetch`（C）、以及一棵 `Sm90EVT<Sm90Compute<multiplies>, α, acc>` 子树，完整表达 `α·acc + β·C`；`DefaultOperation` 则直接用 `fusion::LinearCombination<...>` 标签。注释 L289-290 还指引读者去 `callbacks.hpp` 看更复杂的例子。

builder 的最后一个模板参数就是 FusionOp（或自定义树）：

[examples/49_hopper_gemm_with_collective_builder/49_collective_builder.cu:307-316](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/49_hopper_gemm_with_collective_builder/49_collective_builder.cu#L307-L316) — `CollectiveBuilder<..., cute::conditional_t<UseCustomEVT, CustomEVT, DefaultOperation>>::CollectiveOp`。注意 L283-286 的断言：**自定义 EVT 目前仅被 TMA warp-specialized epilogue 支持**。

进阶：用户自己手写嵌套 EVT（example 113 的 gated activation）：

[examples/113_hopper_gemm_activation_fusion/sm90_visitor_gated_act.hpp:109-122](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/113_hopper_gemm_activation_fusion/sm90_visitor_gated_act.hpp#L109-L122) — `ComputeEVT = Sm90EVT<ComputeOp, Sm90AccFetch>`、`StoreEVT = Sm90EVT<StoreOp, ...>`、`Impl = Sm90EVT<StoreEVT, ComputeEVT>`，即「树里套树」：先算激活子树，再喂给存储子树。这正是 EVT 表达力的体现——当预置标签不够时，你可以用 `Sm90EVT` 自由组合。

预置菜谱 `Sm90LinearCombination`（用来对照自定义写法）：

[include/cutlass/epilogue/fusion/sm90_callbacks_tma_warpspecialized.hpp:174-190](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_callbacks_tma_warpspecialized.hpp#L174-L190) — 与 example 49 的 `CustomEVT` 几乎逐行一致，区别仅在于菜谱版多了一些类型推导与 `get_unpacked_element_type` 处理。这说明**自定义 EVT 与预置菜谱本质同构**，菜谱只是「官方帮你写好并测过」的版本。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：把 example 49 的 epilogue 改成 **「LinearCombination + ReLU」融合**（即 \(D=\text{ReLU}(\alpha\cdot\text{acc}+\beta\cdot C)\)），用声明式标签实现，编译运行并验证。

**操作步骤**：

1. 复制 example 49 到自己的实验目录（不要改原始示例）。
2. 找到 [`49_collective_builder.cu:305`](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/49_hopper_gemm_with_collective_builder/49_collective_builder.cu#L305) 的 `DefaultOperation`，把
   ```cpp
   using DefaultOperation = cutlass::epilogue::fusion::LinearCombination<ElementD, ElementCompute, ElementC, ElementScalar, RoundStyle>;
   ```
   改为
   ```cpp
   using DefaultOperation = cutlass::epilogue::fusion::LinCombEltAct<cutlass::epilogue::thread::ReLu, ElementD, ElementCompute, ElementC, ElementScalar, RoundStyle>;
   ```
   并确保 `UseCustomEVT = false`（走标签路径）。
3. 确认文件顶部已 `#include "cutlass/epilogue/thread/activation.h"`（若没有则加上）。
4. 用 Hopper 架构编译：
   ```bash
   cmake -B build -DCUTLASS_NVCC_ARCHS=90a
   cmake --build build --target 49_hopper_gemm_with_collective_builder -j
   ```
5. 运行 `./build/examples/49_hopper_gemm_with_collective_builder/49_hopper_gemm_with_collective_builder`。

**需要观察的现象**：

- 编译通过（标签路径下，builder 自动选好 stage/copy atom，无需手调）。
- 运行后程序打印 `Passed`（示例自带与参考实现的逐元素比对）。
- 把 α 设为正、把部分 C 设为负的大值时，输出 D 的负数位置应被 ReLU 截断为 0。

**预期结果**：D 中所有 \( \alpha\cdot\text{acc}+\beta\cdot C < 0\) 的元素都变成 0，其余等于原线性组合值；自检 `Passed`。

**结论性结果**：待本地验证（本机若无 Hopper GPU，可在编译期用 `nvcc -c` 仅做语法/模板实例化检查，确认 `LinCombEltAct<ReLu,...>` 能被 `CollectiveBuilder` 接受；但运行结果需 SM90a 硬件）。

> **没有 Hopper 硬件的替代实践（源码阅读型）**：按 4.3.4 的四跳链路，在纸上画出 `LinCombEltAct<ReLu>` 对应的完整访客树（见第 5 节综合实践），并标注每个节点落在 `producer load` 还是 `consumer store` 入口点。

#### 4.4.5 小练习与答案

**练习 1**：若要做 \(D=\text{ReLU}(\alpha\cdot\text{acc}+\beta\cdot C + b_m)\)（多了逐行 bias），该用哪个标签？
**答案**：`fusion::LinCombPerRowBiasEltAct<ReLu, ...>`（见 [`operations.hpp:185-201`](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/operations.hpp#L185-L201)）。它在 `LinCombEltAct` 基础上声明 `IsPerRowBiasSupported=true`，对应树多一个 `Sm90RowBroadcast` 叶子喂给根的 `multiply_add`。

**练习 2**：为什么 `Sm90AuxStore` 节点要使用 `reduce`/`postreduce`/`tma_store` 入口点，而不是 `visit`？
**答案**：`visit` 的语义是「给定输入片段，返回一个输出片段」，是纯寄存器内的逐元素运算；而 Aux 写回需要把寄存器数据搬到 smem 再用 TMA 写显存（甚至需要跨线程归约，如算 dBias），这些副作用正好对应 collective 提供的 `reduce`（smem 归约）/`postreduce`（smem 写）/`tma_store`（TMA 写）入口点。把这些副作用与纯计算的 `visit` 分开，是为了让 collective 的流水线编排能正确插入门栏与异步等待。

---

## 5. 综合实践

把本讲知识串起来，完成一个 **「画出一棵真实 EVT 并解释数据流」** 的小任务。这是本讲规格里指定的实践，也是检验你是否真正理解 EVT 的最好方式。

**任务**：针对融合公式

\[
D = \text{ReLU}\bigl(\alpha\cdot\text{acc} + \beta\cdot C\bigr)
\]

完成下列各步。

**第 1 步：定位官方实现**。打开 [`sm90_callbacks_tma_warpspecialized.hpp:340-343`](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_callbacks_tma_warpspecialized.hpp#L340-L343) 的 `Sm90LinCombEltAct` 与 [`operations.hpp:122-135`](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/operations.hpp#L122-L135) 的 `LinCombEltAct` 标签，确认二者对应。

**第 2 步：展开整棵树**。结合 [`Sm90LinearCombination`](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_callbacks_tma_warpspecialized.hpp#L174-L190) 的定义，画出 `Sm90LinCombEltAct<ReLu, ...>` 的完整访客树：

```text
                         Sm90Compute<ReLu>              (根, 一元: 激活)            [consumer.visit]
                                  │
                                  ▼ 返回激活后片段 → 写入 tRS_rCompute_frg → smem → TMA → D
                                  │
                         Sm90LinearCombination           (子树根, 三元: β·C + (α·acc)) [consumer.visit]
                       /              |                \
            Sm90ScalarBroadcast   Sm90SrcFetch        Sm90EVT<Sm90Compute<multiplies>>   (二元: α·acc)
                 (β)                 (C)                /                        \
                                               Sm90ScalarBroadcast          Sm90AccFetch
                                                   (α)                      (acc ← 主循环 wgmma)
```

**第 3 步：标注数据流与入口点**。在树上标注：

- **`acc` 从哪里来**：来自主循环的 wgmma 累加器，由 `Sm90AccFetch` 在 `consumer.visit` 取出。
- **`α/β` 从哪里来**：host 端用户填的标量（或指针），由 `Sm90ScalarBroadcast` 在 `consumer.visit` 广播；若用了指针，则在 `consumer.previsit`/`begin` 里可能先做 gmem→rmem 的广播拷贝。
- **`C` 从哪里来**：collective 的 producer 段 `load()` 用 TMA 把 C 从 gmem 搬到 smem（[L497-501](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/collective/sm90_epilogue_tma_warpspecialized.hpp#L497-L501)），consumer 段再 smem→寄存器（[L803](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/collective/sm90_epilogue_tma_warpspecialized.hpp#L803)），最后由 `Sm90SrcFetch` 在 `visit` 取出。
- **`D` 到哪里去**：根 `visit` 的返回值 → `tRS_rCompute_frg` → 数值类型转换 → reg→smem → TMA store → gmem D。

**第 4 步（可选, 需 Hopper 硬件）**：按 4.4.4 把 example 49 改成 `LinCombEltAct<ReLu,...>`，编译运行，确认输出符合上述数据流（负数被截断）。

**验收标准**：你能不查源码、对着公式写出这棵树的结构，并指出每个节点的值由哪个 collective 入口点产出。

## 6. 本讲小结

- CUTLASS 3.x 的 epilogue 把 **「搬运调度」**（`CollectiveEpilogue` 的 `load`/`store` 两段、TMA 流水线、`EpilogueTile` 子分块）与 **「数学计算」**（`FusionCallbacks`）彻底解耦；collective 只在固定入口点（最重要的是 `visit`）调用融合回调。
- **EVT（访客树）** 是构造 `FusionCallbacks` 的统一方式：一棵由叶子节点（`Sm90AccFetch`/`Sm90SrcFetch`/`Sm90ScalarBroadcast`/`Sm90RowBroadcast`/`Sm90ColBroadcast`/`Sm90AuxLoad`）、计算节点（`Sm90Compute<ComputeFn>`）和组合原语（`Sm90TreeVisitor`/`Sm90SplitTreeVisitor`/`Sm90TopologicalVisitor`）拼成的表达式树；`visit()` 自顶向下递归求值。
- 面向用户的别名是 **`Sm90EVT<NodeOp, ChildOps...>` = `Sm90TreeVisitor<...>`**；写 EVT 就是写嵌套的 `Sm90EVT` 类型。
- **声明式标签**（`operations.hpp` 的 `LinearCombination`/`LinCombEltAct`/...，只含元数据）经 `FusionCallbacks<策略, 标签, ...>` 偏特化翻译成具体树；`CollectiveBuilder` 接受标签或自定义树作为最后一个模板参数。
- 用户填**扁平命名 `Arguments`**（`alpha/beta/...`），由 `operator Impl::Arguments()` 自动重打包成树的嵌套结构——你按名字填，库负责映射。
- 「LinearCombination + ReLU」就是 **`fusion::LinCombEltAct<ReLu,...>`**，等价于一棵以 `Sm90Compute<ReLu>` 为根、`Sm90LinearCombination` 为唯一子树的 EVT。

## 7. 下一步学习建议

- **u3-l1（异步流水线）**：本讲多次提到 `PipelineTmaAsync`、`producer_acquire/consumer_wait`。下一讲会深入这些同步原语如何让 producer/consumer 重叠搬算。
- **u3-l2（TMA）**：epilogue 的 `load`/`store` 都依赖 TMA 描述符。理解 TMA 描述符的构造，能帮你读懂 `Params::TMA_C`/`TMA_D` 是怎么从 `Arguments::ptr_C/ptr_D` 建出来的。
- **进阶 EVT**：阅读 [`sm90_callbacks_tma_warpspecialized.hpp`](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_callbacks_tma_warpspecialized.hpp) 里更复杂的菜谱（如 `Sm90LinCombPerRowBiasEltActAux`、`Sm90LinCombDeEltActDePerRowBias`），以及 example 113 的 [`sm90_visitor_gated_act.hpp`](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/113_hopper_gemm_activation_fusion/sm90_visitor_gated_act.hpp) 学习手写嵌套 EVT。
- **Python EVT**：CUTLASS Python 前端把 EVT 暴露成更友好的图构造 API，可参考 `test/python/cutlass/evt/` 与 `python/cutlass_cppgen/epilogue/`，体会「同一棵树，两种表达」。
- **Blackwell（SM100）epilogue**：对照 [`sm100_callbacks_tma_warpspecialized.hpp`](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm100_callbacks_tma_warpspecialized.hpp)，看 EVT 模型如何跨架构复用（u3-l7 会讲 SM100 的 UMMA/TMEM）。
