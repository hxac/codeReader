# Epilogue 与 EVT 访客树

## 1. 本讲目标

在 [u2-l8](u2-l8-collective-builder.md) 与 [u2-l9](u2-l9-hopper-warp-specialized-gemm.md) 里，我们把一次 GEMM 拆成了「kernel 外壳 + collective mainloop（搬 A/B、做乘加）+ collective epilogue（后处理并写回）」三段式，并用 `CollectiveBuilder` 自动组装出 mainloop。本讲聚焦最后一段——**epilogue**，学完后你应当能够：

1. 说清楚 epilogue collective 在 GEMM 中负责什么、它和 mainloop 是如何衔接的。
2. 看懂 `fusion::FusionOperation` 这套「元数据配方」是如何用纯类型描述一段融合数学的。
3. 理解 **EVT（Epilogue Visitor Tree，访客树）** 的树形组合模型：叶子节点取数据、中间节点做逐元素计算、根节点写出。
4. 跟踪 visitor 节点如何通过 producer/consumer 两套 callback 钩子接入 warp-specialized epilogue 的异步流水线。
5. 在源码中定位 `LinearCombination + ReLU` 这样的常用融合，并画出它的访客树与数据流向。

---

## 2. 前置知识

本讲默认你已经读过以下讲义建立的概念，这里只做最简回顾：

- **[u2-l7](u2-l7-gemm-3x-universal-model.md) 三段式通用模型**：CUTLASS 3.x 一次 GEMM = 一个无状态 `kernel::GemmUniversal` 外壳 + 三个可替换部件：`CollectiveMainloop`、`CollectiveEpilogue`、`TileScheduler`。
- **[u2-l8](u2-l8-collective-builder.md) CollectiveBuilder**：编译期「装配车间」，吃高层参数（数据类型、tile、策略）自动推断出 collective 的全部模板参数；epilogue 也有一个对应的 `EpilogueBuilder`。
- **[u2-l9](u2-l9-hopper-warp-specialized-gemm.md) warp specialization**：Hopper 内核把 warp 分成 producer（搬数据）与 consumer（算）两类并发执行；epilogue 也采用同样的 producer/consumer 划分。
- **[u1-l4](u1-l4-numeric-types.md) 数值类型**：累加器通常是 `float`，而输出 D 可能是 `half_t`/`bfloat16_t`/`float_e4m3_t` 等更窄的类型，因此 epilogue 里充满了 `NumericArrayConverter` 的精度转换。

几个本讲会用到的术语：

| 术语 | 含义 |
|------|------|
| **accumulator / acc** | mainloop 在寄存器（rmem）里累加得到的矩阵乘结果片段 |
| **epilogue** | GEMM 的「收尾段」：消费 acc，做缩放、加偏置、激活等后处理，再写回显存 |
| **fusion（融合）** | 把「矩阵乘 + 后处理」合并进同一个内核，避免再读写一遍显存 |
| **visitor / 回调（callback）** | epilogue 在数据搬运/计算的不同时机调用的钩子函数 |
| **EVT** | Epilogue Visitor Tree，用一棵树组合出任意融合计算 |

---

## 3. 本讲源码地图

本讲涉及的关键源码文件（都属于 `include/cutlass/epilogue/`，命名空间 `cutlass::epilogue::fusion` / `cutlass::epilogue::collective`）：

| 文件 | 作用 |
|------|------|
| `collective/sm90_epilogue_tma_warpspecialized.hpp` | Hopper SM90 的 epilogue **collective** 本体：组织 producer/consumer、加载 C、写回 D |
| `fusion/operations.hpp` | **元数据配方库**：`ScaledAcc`、`LinearCombination`、`LinCombEltAct` 等纯类型描述 |
| `fusion/callbacks.hpp` | **分派接口** `FusionCallbacks`：把（策略, 配方）映射到具体实现 |
| `fusion/sm90_callbacks_tma_warpspecialized.hpp` | **EVT 的预置别名与偏特化**：`Sm90EVT`、`Sm90LinearCombination`、`Sm90LinCombEltAct` |
| `fusion/sm90_visitor_tma_warpspecialized.hpp` | **访客树骨架**：`Sm90VisitorImplBase`、`Sm90TreeVisitor`、callback 生命周期 |
| `fusion/sm90_visitor_load_tma_warpspecialized.hpp` | **叶子节点**：`Sm90AccFetch`、`Sm90SrcFetch`、`Sm90ScalarBroadcast` 等 |
| `fusion/sm90_visitor_compute_tma_warpspecialized.hpp` | **计算节点**：`Sm90Compute`（N 元逐元素运算）|
| `epilogue/dispatch_policy.hpp` | epilogue 的策略 tag，如 `Sm90TmaWarpSpecialized` |

一个简单的 mental model：**`operations.hpp` 描述「要算什么」，`sm90_visitor_*.hpp` 提供「积木块」，`sm90_callbacks_tma_warpspecialized.hpp` 用积木拼成「预置配方」，`sm90_epilogue_tma_warpspecialized.hpp` 是把这些配方装进内核的「容器」。**

---

## 4. 核心概念与源码讲解

### 4.1 Epilogue collective 概览

#### 4.1.1 概念说明

mainloop 结束后，每个 consumer 线程的寄存器里都拿着一块累加器片段 `acc`（即 \(A \times B\) 的部分和）。但 GEMM 的真实输出往往是

\[
D = \mathrm{act}\bigl(\alpha \cdot \mathrm{acc} + \beta \cdot C\bigr)
\]

其中 \(\alpha,\beta\) 是标量，\(C\) 是「源矩阵」（残差/前一层输出），\(\mathrm{act}\) 是激活函数（ReLU、GELU……）。把这些后处理**和矩阵乘融合进同一个内核**，就省去了「先把 acc 写回显存、再启动一个 elementwise kernel」的来回搬运——这就是 epilogue 的核心价值。

epilogue collective 干的全部事情可以归纳为四步：

1. 把 CTA 的累加器按 `EpilogueTile` 切成更小的子块（epilogue 子 tile）。
2. **producer（load warp）**：用 TMA 异步把源矩阵 \(C\)（以及可能的 aux/bias）从显存搬到共享内存。
3. **consumer（store warps）**：把 acc 拷到寄存器，按访客树做逐元素融合计算。
4. 把结果 \(D\) 经共享内存用 TMA 异步写回显存。

它和 mainloop 一样是**无状态 collective**：自身不持有可变成员，所有运行期信息都通过 `Params`（内核参数）传入；它也采用 warp specialization 的 producer/consumer 划分，只不过这里 producer 搬的是 C、consumer 算的是融合结果。

#### 4.1.2 核心流程

```text
mainloop 产出 acc (rmem)
        │
        ▼
epilogue 遍历 EpilogueTile 子块 (epi_m, epi_n):
  ┌─ producer / load warp ──────────────────────────────┐
  │  for each sub-tile:                                  │
  │    acquire 缓冲锁  → TMA 加载 C/aux → expect-tx      │
  └──────────────────────────────────────────────────────┘
  ┌─ consumer / store warps ────────────────────────────┐
  │  wait 缓冲  → acc 拷到寄存器                         │
  │  → 调用访客树 visit():  α·acc + β·C + bias → 激活     │
  │  → 结果写 smem → TMA store 回 D                      │
  └──────────────────────────────────────────────────────┘
```

注意 `StagesC` / `StagesD`：C 的加载和 D 的写回各有自己的多级缓冲级数，二者独立配置，让加载、计算、写回三件事可以像流水线一样重叠。

#### 4.1.3 源码精读

SM90 epilogue collective 是 `CollectiveEpilogue` 针对策略 `Sm90TmaWarpSpecialized<...>` 的偏特化，模板参数清楚地列出了它管什么：

[include/cutlass/epilogue/collective/sm90_epilogue_tma_warpspecialized.hpp:83-100](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/collective/sm90_epilogue_tma_warpspecialized.hpp#L83-L100) — 这是偏特化的签名，参数包含 `StagesC_`/`StagesD_`（C/D 的缓冲级数）、`EpilogueTile_`（子 tile 形状）、`ElementC_`/`StrideC_`/`ElementD_`/`StrideD_`（源/输出的类型与步长），以及本讲的焦点 `FusionCallbacks_`（融合计算核心）。

紧接着它用 `static_assert` 强约束几何关系，例如 epilogue 子 tile 必须整除 CTA tile：

[include/cutlass/epilogue/collective/sm90_epilogue_tma_warpspecialized.hpp:126-130](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/collective/sm90_epilogue_tma_warpspecialized.hpp#L126-L130) — 断言 `EpilogueTile` 必须是 rank-2，且 `EPI_TILE_M`/`EPI_TILE_N` 必须分别整除 `CTA_M`/`CTA_N`。

而 `FusionCallbacks_` 到底是什么？collective 通过一个 trait 把它解析成「线程级的融合算子」类型 `ThreadEpilogueOp`：

[include/cutlass/epilogue/collective/sm90_epilogue_tma_warpspecialized.hpp:122](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/collective/sm90_epilogue_tma_warpspecialized.hpp#L122) — `ThreadEpilogueOp` 由 `FusionCallbacksTraits<FusionCallbacks>::Operation` 给出，这正是 epilogue 真正「算东西」的入口。理解了 `FusionCallbacks`，就理解了 epilogue 的计算灵魂——下面四节都在讲它。

主机端参数翻译则发生在 `to_underlying_arguments`：

[include/cutlass/epilogue/collective/sm90_epilogue_tma_warpspecialized.hpp:302](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/collective/sm90_epilogue_tma_warpspecialized.hpp#L302) — collective 调用 `FusionCallbacks::to_underlying_arguments(problem_shape, args.thread, workspace)`，把主机 `Arguments` 翻译成内核 `Params`（含 TMA descriptor 等）。

#### 4.1.4 代码实践

1. **实践目标**：熟悉 epilogue collective 的模板骨架。
2. **操作步骤**：打开 [sm90_epilogue_tma_warpspecialized.hpp:61-130](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/collective/sm90_epilogue_tma_warpspecialized.hpp#L61-L130)，逐行列出偏特化的全部模板参数，并标注每个参数属于「缓冲配置 / 几何 / 类型 / 融合 / 拷贝原子」中的哪一类。
3. **需要观察的现象**：注意 `CopyOpG2S_`（global→smem）、`CopyOpS2G_`（smem→global）、`CopyAtomC_` 等多个拷贝原子，它们分别服务于「加载 C」和「写回 D」两条数据通路。
4. **预期结果**：你能用一句话回答「epilogue collective 为什么需要这么多拷贝原子」——因为它要同时管理 C 的 gmem→smem、acc 的 smem/rmem→寄存器、以及 D 的 rmem→smem→gmem 多条通路。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `StagesC` 和 `StagesD` 要分开配置，而不是共用一个 `Stages`？
**答案**：C 的加载和 D 的写回是两条独立的异步流水线，需要的缓冲级数取决于各自的吞吐与延迟；分开配置才能让两条流水线各自达到最优，而不互相迁就。

**练习 2**：epilogue collective 自身有成员变量吗？为什么？
**答案**：没有可变成员。它是无状态 collective，所有运行期数据都通过 `Params` 传入，这样同一个内核类型可以被不同参数复用，且不存在隐式状态。

---

### 4.2 FusionOperation：用元数据描述融合数学

#### 4.2.1 概念说明

`operations.hpp` 里定义了一组**纯类型「配方」**，比如 `ScaledAcc`、`LinearCombination`、`LinCombEltAct`。注意一个反直觉的点：**这些结构体里没有任何代码逻辑，只有类型别名（`ElementOutput`、`ElementCompute`……）和一串 `static constexpr bool` 标志位**。它们的作用是「声明我想要一段什么样的融合数学」，相当于一张规格表，由 builder 读取后决定生成哪些 visitor。

基类 `FusionOperation` 把所有可能用到的元数据字段都列了出来，默认全是「不支持」：

[include/cutlass/epilogue/fusion/operations.hpp:52-91](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/operations.hpp#L52-L91) — 基类定义了 `ElementOutput`/`ElementCompute`/`RoundStyle`，以及一整套 `IsSourceSupported`、`IsPerRowBiasSupported`、`IsEltActSupported`、`IsAuxOutSupported`、`IsAbsMaxSupported`、`IsBlockScaleSupported` 等开关，默认 `false` / `void`。

派生配方只需把需要的开关「打开」。例如 `LinearCombination` 相比 `ScaledAcc` 就是多了「需要源矩阵 C」：

[include/cutlass/epilogue/fusion/operations.hpp:108-120](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/operations.hpp#L108-L120) — `LinearCombination` 继承自 `ScaledAcc`，注释 `D = alpha * acc + beta * C`，并把 `IsSourceSupported = true`。注释里的数学式就是这段配方的全部语义。

#### 4.2.2 核心流程

这套配方的存在是为了**解耦「想算什么」与「怎么算」**：

```text
用户在 EpilogueBuilder::Arguments 里写：
    fusion::LinCombPerRowBiasEltAct<ReLu, half_t, float, ...>
            │  （纯元数据：要 ReLU、要 per-row bias、输出 half、计算 float）
            ▼
builder 读 IsEltActSupported / IsPerRowBiasSupported 等开关
            │
            ▼
builder 选中对应的 EVT 树实现（见 4.5）
```

因为配方不带实现，同一份 `fusion::LinearCombination` 可以在 SM90、SM100、SM120 上分别映射到不同的 visitor 实现，而用户代码完全不用改。

#### 4.2.3 源码精读

配方之间通过继承表达「叠加特性」，形成一棵小继承树。注意每个结构体顶部的注释就是它的数学定义：

[include/cutlass/epilogue/fusion/operations.hpp:93-106](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/operations.hpp#L93-L106) — `ScaledAcc`，注释 `D = alpha * acc`，只设 `AlignmentScalar=1` 与 `RoundStyle`。

[include/cutlass/epilogue/fusion/operations.hpp:122-135](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/operations.hpp#L122-L135) — `LinCombEltAct`，注释 `D = activation(alpha * acc + beta * C)`，它继承 `LinearCombination` 并多加一个模板模板参数 `ActivationFn_`，打开 `IsEltActSupported = true`。**这就是本讲实践要找的「LinearCombination + 激活」配方。**

更复杂的特性（per-row/per-col bias、aux 输出、amax、block scale）都是用同样的方式一层层叠加，例如 `LinCombPerRowBiasEltAct`（[operations.hpp:185-201](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/operations.hpp#L185-L201)）打开 `IsPerRowBiasSupported`。

#### 4.2.4 代码实践

1. **实践目标**：建立「配方 = 元数据」的直觉。
2. **操作步骤**：通读 [operations.hpp:52-521](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/operations.hpp#L52-L521)，挑出 5 个配方，记录它们的「数学注释 + 打开了哪些 `Is*Supported` 开关」。
3. **需要观察的现象**：你会看到所有配方的结构体体几乎为空——只有 `using` 和 `static constexpr`。
4. **预期结果**：你能解释「为什么把这些放进 `struct` 而不是 `enum`」——因为它们要携带类型（`ElementOutput` 等）和模板参数（`ActivationFn`），而 `enum` 做不到。

#### 4.2.5 小练习与答案

**练习 1**：`fusion::LinearCombination` 和 2.x 时代的 `epilogue::thread::LinearCombination` 是一回事吗？
**答案**：不是。前者（本讲的 `operations.hpp`）是**纯元数据配方**，只声明意图；后者是 2.x 的**实际逐元素计算类**，含真正的 `operator()`。3.x 里真正的计算由 EVT 访客树（4.3、4.4）承担。

**练习 2**：如果想新增一种「逐元素对 D 做 tanh」的配方，需要在 `FusionOperation` 基类里加什么？
**答案**：理论上复用现有的 `IsEltActSupported` + `ActivationFn` 即可（把 `ActivationFn` 设为 tanh）；只有当新特性无法用现有开关描述时，才需要在基类里加新的 `using`/`static constexpr bool` 字段，并在 builder 与 visitor 里都接入。

---

### 4.3 EVT 访客树模型

#### 4.3.1 概念说明

配方只描述「要算什么」，**EVT（Epilogue Visitor Tree）** 则给出「怎么把它拼出来」。EVT 把一段融合表达成一棵树：

- **叶子节点（load visitor）**：数据的来源。例如 `Sm90AccFetch`（取累加器）、`Sm90SrcFetch`（取源 C）、`Sm90ScalarBroadcast`（标量广播 α/β）、`Sm90RowBroadcast`/`Sm90ColBroadcast`（逐行/逐列 bias 广播）、`Sm90AuxLoad`（取 aux 矩阵）。
- **中间/根节点（compute visitor）**：N 元逐元素运算。`Sm90Compute<multiplies>`、`Sm90Compute<homogeneous_multiply_add>`、`Sm90Compute<plus>`、`Sm90Compute<ReLu>` 等。
- **写出节点（store visitor）**：`Sm90AuxStore` 等，把中间结果写到 aux 输出（主输出 D 由 collective 统一回写）。

整棵树的「胶水」就是 `Sm90EVT`：

[include/cutlass/epilogue/fusion/sm90_callbacks_tma_warpspecialized.hpp:57-58](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_callbacks_tma_warpspecialized.hpp#L57-L58) — `Sm90EVT<NodeOp, ChildOps...>` 只是 `Sm90TreeVisitor<NodeOp, ChildOps...>` 的别名。`NodeOp` 是这棵子树的根运算，`ChildOps...` 是它的若干个孩子（孩子本身又可以是一棵 `Sm90EVT` 子树）。

#### 4.3.2 核心流程

访客树的计算是**后序遍历**：先递归 `visit` 每个孩子（拿到孩子输出的寄存器 fragment），再把所有孩子的输出作为参数喂给根节点的 `visit`。以 \( Z = \beta\cdot C + (\alpha\cdot \mathrm{acc}) \) 为例，对应树是：

```text
            homogeneous_multiply_add   ← 根（三元：β, C, Z0）
            /        |        \
    ScalarBroadcast  SrcFetch   multiplies   ← Z0 = α·acc
       (β)           (C)       /      \
                          ScalarBroadcast  AccFetch
                             (α)          (acc)
```

伪代码：

```text
visit(frg_acc):
    z0 = child[α·acc].visit(frg_acc)        # = multiplies(α_broadcast, acc)
    c  = child[SrcFetch].visit(frg_acc)     # = C 的寄存器片段
    b  = child[ScalarBroadcast].visit(...)  # = β
    return homogeneous_multiply_add(b, c, z0)   # β*C + (α*acc)
```

注意 `homogeneous_multiply_add(a, b, c)` 的语义是 \(a \cdot b + c\)，所以上式 = \(\beta\cdot C + (\alpha\cdot\mathrm{acc})\)，正好是 `LinearCombination`。

#### 4.3.3 源码精读

树的 `visit` 实现在 `Sm90TreeVisitor::ConsumerStoreCallbacks::visit`，它用 `tapply` 先算前 \(R-1\) 个孩子、再把结果交给第 \(R\) 个（根）运算：

[include/cutlass/epilogue/fusion/sm90_visitor_tma_warpspecialized.hpp:734-747](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_visitor_tma_warpspecialized.hpp#L734-L747) — 先用 `make_seq<Rm1>{}` 限制只对前 \(R-1\) 个孩子调用 `child.visit(frg_acc,...)`（要求孩子是「零元」叶子或子树），收集它们的 `frg_inputs...`，最后调用根 `get<Rm1>(callbacks_tuple).visit(frg_acc, epi_v, epi_m, epi_n, frg_inputs...)`。这就是「后序遍历 + 把孩子输出喂给根」的精髓。

叶子节点本身极简。`Sm90AccFetch` 只是把传入的累加器片段原样返回：

[include/cutlass/epilogue/fusion/sm90_visitor_load_tma_warpspecialized.hpp:62-64](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_visitor_load_tma_warpspecialized.hpp#L62-L64) — `Sm90AccFetch` 继承自 `Sm90VisitorImpl<>`，是最常见的「取 acc」叶子。

`Sm90SrcFetch`（[同文件:91](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_visitor_load_tma_warpspecialized.hpp#L91)）则负责取源 C 的寄存器片段。

#### 4.3.4 代码实践

1. **实践目标**：亲手读懂一棵 EVT。
2. **操作步骤**：打开预置别名 [Sm90LinearCombination](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_callbacks_tma_warpspecialized.hpp#L182-L190)（见 4.5.3），逐行把它还原成上面 4.3.2 那样的树形图。
3. **需要观察的现象**：注意根是 `homogeneous_multiply_add`，它有 3 个孩子：`ScalarBroadcast(β)`、`SrcFetch(C)`、以及一棵 `multiplies(α, acc)` 子树。
4. **预期结果**：你能写出这棵树的「层序遍历列表」：`[hma, β, C, mul, α, acc]`，并解释每个节点是叶子还是计算。

#### 4.3.5 小练习与答案

**练习 1**：`homogeneous_multiply_add` 是几元运算？为什么 `LinearCombination` 树的根恰好需要它？
**答案**：三元，语义 \(a\cdot b + c\)。`LinearCombination` 是 \(\beta C + (\alpha\cdot\mathrm{acc})\)，正好是「一个乘积加另一个乘积」，所以根用三元 `hma(β, C, α·acc)` 最直接（把内层 `α·acc` 作为第三个孩子）。

**练习 2**：如果一种融合需要共享中间结果（DAG 而非树），EVT 还能用吗？
**答案**：能。`sm90_visitor_tma_warpspecialized.hpp` 还提供了 `Sm90SplitTreeVisitor`（[L770](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_visitor_tma_warpspecialized.hpp#L770)）和 `Sm90TopologicalVisitor`（[L819](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_visitor_tma_warpspecialized.hpp#L819)），后者用一个 `EdgeTuple`（孩子下标序列）表达任意 DAG。

---

### 4.4 Fusion visitor 与 callback 生命周期

#### 4.4.1 概念说明

一个 visitor 节点并不只是 `visit()` 这一个函数。在 warp-specialized epilogue 里，节点要全程参与异步数据搬运，因此它通过**两套 callback 钩子**接入两个角色：

- **`ProducerLoadCallbacksImpl`**（被 **producer / load warp** 调用）：负责发 TMA 加载（C、aux）、对应的 `mbarrier expect-tx`。大多计算节点用空实现。
- **`ConsumerStoreCallbacksImpl`**（被 **consumer / store warps** 调用）：负责 smem 广播、真正的 `visit` 逐元素计算、smem 归约、TMA store。**所有节点都必须实现这套**。

整棵 EVT 树的 callback 是把所有节点的 callback 打包成一个 tuple，用 `for_each` 逐个调用——这样加一个节点就等于在 tuple 里多加一个元素，互不打扰。

#### 4.4.2 核心流程

consumer 侧完整的生命周期（每个 sub-tile 迭代）：

```text
begin          ← 进入 store 循环前；做 gmem 标量广播
  └─ begin_loop(epi_m, epi_n)
       ├─ previsit(...)     ← producer 加载已完成；做 smem 广播
       ├─ visit(frg_acc)    ← 真正的逐元素融合计算（返回结果片段）
       ├─ reduce(...)       ← smem 归约（如 per-row bias 求和）
       ├─ postreduce(...)   ← smem 异步 store
       ├─ tma_store(...)    ← 发 TMA 写回 D/aux
       └─ end_loop(...)
  end            ← 退出循环；做最终 gmem 归约
```

producer 侧更短：`begin → step(发 TMA + expect-tx) → end`。两边靠多级缓冲锁（与 [u2-l8](u2-l8-collective-builder.md)/[u3-l1](u3-l1-async-pipeline.md) 的 `PipelineTmaAsync` 同源）同步。

#### 4.4.3 源码精读

producer 侧 callback 的三段式 `begin/step/end`：

[include/cutlass/epilogue/fusion/sm90_visitor_tma_warpspecialized.hpp:127-173](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_visitor_tma_warpspecialized.hpp#L127-L173) — `ProducerLoadCallbacksImpl` 持有一个 `callbacks_tuple`，`step()` 把 `full_mbarrier_ptr`、`epi_m/epi_n`、`issue_tma_load` 透传给每个子 callback。注释明确：callback 负责为它发的 TMA 配 `expect-tx`，但不负责 `producer_commit`（那由 collective 统一发）。

consumer 侧则把上面流程图里每一个时机都做成一个钩子：

[include/cutlass/epilogue/fusion/sm90_visitor_tma_warpspecialized.hpp:181-305](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_visitor_tma_warpspecialized.hpp#L181-L305) — `ConsumerStoreCallbacksImpl` 依次定义 `begin`、`begin_sync_needed`、`begin_loop`、`previsit`、`visit`（`= delete`，强制每个运算自己实现）、`reduce`、`postreduce`、`tma_store`、`end_loop`、`end`。注意 [L229-L231](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_visitor_tma_warpspecialized.hpp#L229-L231) 里 `visit` 被声明为 `= delete`：基类只提供遍历框架，真正的计算必须由各 compute 节点重写。

`Sm90Compute` 正是重写 `visit` 的典型。它先把每个输入片段用 `NumericArrayConverter` 转成 `ElementCompute`，再调用户传入的 `ComputeFn`，最后转回 `ElementOutput`：

[include/cutlass/epilogue/fusion/sm90_visitor_compute_tma_warpspecialized.hpp:171-203](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_visitor_compute_tma_warpspecialized.hpp#L171-L203) — `Sm90Compute::visit` 用 `transform_apply`：第一段对每个 `frg_input` 做 `ConvertInput`（→`ElementCompute`），第二段构造 `ComputeFn<Array<ElementCompute,FragmentSize>>` 并调用 `compute_output(cvt_frg_inputs..., params)`（若 `Arguments` 非空则附带激活超参），最后 `ConvertOutput` 转回 `ElementOutput`。

最后，整棵树的「汇总器」是 `Sm90VisitorImplBase`，它把所有子节点的 `SharedStorage`/`Arguments`/`Params` 各自打包成一个 tuple：

[include/cutlass/epilogue/fusion/sm90_visitor_tma_warpspecialized.hpp:483-494](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_visitor_tma_warpspecialized.hpp#L483-L494) — `Sm90VisitorImplBase<Ops...>` 定义 `SharedStorage = tuple<Ops::SharedStorage...>`、`Arguments = tuple<Ops::Arguments...>`、`Params = tuple<Ops::Params...>`，并提供 `to_underlying_arguments`/`can_implement`/`get_workspace_size` 对整个 tuple 做遍历。这就是「加一个节点 = tuple 多一项」的落地。

#### 4.4.4 代码实践

1. **实践目标**：跟踪一个 compute 节点在一次 `visit` 里的完整数据流。
2. **操作步骤**：以 [Sm90Compute<homogeneous_multiply_add,...>::visit](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_visitor_compute_tma_warpspecialized.hpp#L171-L203) 为目标，假设 `ElementCompute=float`、`ElementOutput=half_t`、`FragmentSize=4`，手动模拟输入 `frg_inputs = {β片段, C片段, α·acc片段}` 的转换路径。
3. **需要观察的现象**：每个片段先被 `NumericArrayConverter<float, half_t, 4>` 提升到 float，运算后再被 `NumericArrayConverter<half_t, float, 4>` 收回 half——精度提升发生在计算阶段、回缩发生在写回前。
4. **预期结果**：你能解释「为什么 compute 类型通常选 float 而不是直接用输出类型 half」——为了避免 α·acc 这种缩放在低精度下丢精度。

> 说明：以上是源码阅读型实践，不需要 GPU；若要在真机上观察，需要 SM90a 硬件，运行结果待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么基类的 `ConsumerStoreCallbacksImpl::visit` 要声明为 `= delete` 而不是纯虚函数？
**答案**：CUTLASS 用 CRTP/模板组合而非运行时多态。`= delete` 保证若某 compute 节点忘记重写 `visit`，编译期就会报错；而模板组合在编译期就完成分发，零运行时开销。

**练习 2**：`Sm90Compute` 默认 `is_producer_load_needed() = false`、`is_C_load_needed() = false`（[compute:138-146](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_visitor_compute_tma_warpspecialized.hpp#L138-L146)），为什么？
**答案**：计算节点本身不需要从 gmem 加载任何东西——它消费的是孩子（叶子）已经取来的片段。真正需要 producer 发 TMA 的是 `Sm90SrcFetch`/`Sm90AuxLoad` 这类 load 节点，它们会把这个标志设为 `true`。

---

### 4.5 常用 epilogue 融合模式

#### 4.5.1 概念说明

虽然 EVT 允许你用 `Sm90EVT` 手搓任意树，但绝大多数场景用 CUTLASS **预置好的别名**就够了。这些别名都写在 `sm90_callbacks_tma_warpspecialized.hpp` 里，并且对应一个 `FusionCallbacks` 偏特化——后者把「元数据配方」和「EVT 实现」粘起来：用户在 builder 里传 `fusion::LinearCombination<...>`，偏特化就自动选 `Sm90LinearCombination` 这棵树。

#### 4.5.2 核心流程

```text
用户传 fusion::LinearCombination<half, float, half, float>
        │  （operations.hpp 的元数据配方）
        ▼
FusionCallbacks<Sm90TmaWarpSpecialized<...>, fusion::LinearCombination<...>, ...>
        │  （callbacks.hpp 的分派接口 + sm90_callbacks 的偏特化）
        ▼
继承 Sm90LinearCombination<half, float, half, float>
        │  （一棵 homogeneous_multiply_add 的 EVT 树）
        ▼
host Arguments {alpha, beta, ...} ──operator Impl::Arguments()──▶ 嵌套的叶子 Params
```

关键技巧是 `Arguments` 里那个 `operator typename Impl::Arguments()`：它把**扁平、对用户友好的字段**（`alpha`、`beta`、`alpha_ptr`……）翻译成**和树结构同构的嵌套 tuple**（每个叶子一组参数）。

#### 4.5.3 源码精读

预置别名 `Sm90LinearCombination` 就是 4.3.2 那棵 \(D=\alpha\cdot\mathrm{acc}+\beta\cdot C\) 的树：

[include/cutlass/epilogue/fusion/sm90_callbacks_tma_warpspecialized.hpp:182-190](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_callbacks_tma_warpspecialized.hpp#L182-L190) — `Sm90LinearCombination` = `Sm90EVT<homogeneous_multiply_add, ScalarBroadcast(β), SrcFetch(C), Sm90EVT<multiplies, ScalarBroadcast(α), AccFetch>>`，注释精确标注每个节点：`beta * C + (alpha * acc)`。

对应的偏特化把配方映射到这棵树，并把扁平 args 翻译成嵌套 args：

[include/cutlass/epilogue/fusion/sm90_callbacks_tma_warpspecialized.hpp:206-244](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_callbacks_tma_warpspecialized.hpp#L206-L244) — `FusionCallbacks<Sm90TmaWarpSpecialized<...>, fusion::LinearCombination<...>>` 继承 `Sm90LinearCombination<...>`；[L227-L239](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_callbacks_tma_warpspecialized.hpp#L227-L239) 的 `operator Impl::Arguments()` 用大括号嵌套出 `{beta叶子, C叶子, {α叶子, acc叶子, mul参数}, hma参数}`——结构完全镜像树。

而 **LinearCombination + ReLU** 就是再套一层激活：

[include/cutlass/epilogue/fusion/sm90_callbacks_tma_warpspecialized.hpp:340-343](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_callbacks_tma_warpspecialized.hpp#L340-L343) — `Sm90LinCombEltAct<ActivationFn, ...>` = `Sm90EVT<Sm90Compute<ActivationFn, ...>, Sm90LinearCombination<...>>`，注释 `D = activation(alpha * acc + beta * C)`。把模板模板参数 `ActivationFn` 实例化为 [`cutlass::epilogue::thread::ReLu`](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/thread/activation.h#L145)（[activation.h:145](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/thread/activation.h#L145)）即得到 ReLU 版本。

`Sm90Compute` 默认 `is_producer_load_needed() = false`、`is_C_load_needed() = false`（[compute:138-146](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_visitor_compute_tma_warpspecialized.hpp#L138-L146)），因为计算节点只消费孩子已取来的片段。

#### 4.5.4 代码实践（对应本讲指定的实践任务）

**实践目标**：找到 `LinearCombination + ReLU` 的组合，画出访客树并说明数据流向。

**操作步骤**：

1. 在 [sm90_callbacks_tma_warpspecialized.hpp:340](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_callbacks_tma_warpspecialized.hpp#L340) 定位 `Sm90LinCombEltAct`，确认它的形式是「外层 `Sm90Compute<ActivationFn>` 套住一棵 `Sm90LinearCombination`」。
2. 把 `ActivationFn` 替换为 [`cutlass::epilogue::thread::ReLu`](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/thread/activation.h#L145)（标量版在 [activation.h:145](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/thread/activation.h#L145)，数组向量化重载在同文件 [L169](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/thread/activation.h#L169)）。
3. 展开 `Sm90LinearCombination`（[L182-L190](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_callbacks_tma_warpspecialized.hpp#L182-L190)），把两层 `Sm90EVT` 合并成一棵完整的树。

**需要画出的访客树**（这是答案模板，请对照源码确认）：

```text
                    ReLu                        ← 根（一元激活），D = max(0, Z)
                     │  Z
        homogeneous_multiply_add                 ← β·C + (α·acc)
        /        |           \
 ScalarBroadcast  SrcFetch     multiplies        ← α·acc
     (β)           (C)        /        \
                       ScalarBroadcast  AccFetch
                          (α)           (acc)
```

**数据流向说明**：

1. `AccFetch` 把 mainloop 产出的累加器片段 `acc` 取出 → `multiplies` 算出 \( \alpha\cdot\mathrm{acc} \)。
2. `SrcFetch` 取出源矩阵片段 \(C\)；`ScalarBroadcast` 广播标量 \(\beta\)。
3. 根 `homogeneous_multiply_add` 拼出 \( Z = \beta\cdot C + (\alpha\cdot\mathrm{acc}) \)。
4. 最外层 `ReLu` 算出 \( D = \max(0, Z) \)，由 collective 写回显存。

**预期结果**：你能解释「为什么 ReLU 是整棵树最外层（根）」——因为数学上激活作用在线性组合的结果上，对应后序遍历里最后被求值。

> 进阶可选：阅读真实示例 [examples/113_hopper_gemm_activation_fusion/sm90_lin_comb_elt_act_scaled.hpp:90](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/113_hopper_gemm_activation_fusion/sm90_lin_comb_elt_act_scaled.hpp#L90)（自定义 EVT `Sm90AccCastLinCombEltAct`，带中间精度转换），它在 [113_hopper_gemm_fused_act.cu:141](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/113_hopper_gemm_activation_fusion/113_hopper_gemm_fused_act.cu#L141) 被用作一个融合 GEMM 的 epilogue。编译运行该示例需要 `CUTLASS_NVCC_ARCHS=90a` 及 Hopper 硬件，运行结果待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：要把上面的 ReLU 换成 GELU 或 Sigmoid，需要改哪里？
**答案**：只改 `Sm90LinCombEltAct` 的模板模板参数：把 `ReLu` 换成 [`cutlass::epilogue::thread::Sigmoid`](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/thread/activation.h#L408)（[activation.h:408](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/thread/activation.h#L408)）或 `GELU`（同文件内）。树的其他部分完全不变，因为 `ActivationFn` 是最外层根。

**练习 2**：用户在主机端传 `alpha=1.0, beta=1.0`，这个值是怎么流到设备端 `visit` 里的？
**答案**：`Arguments{alpha, beta}` 经 [operator Impl::Arguments()](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/sm90_callbacks_tma_warpspecialized.hpp#L227-L239) 翻译成嵌套叶子参数，再由 `to_underlying_arguments` 转成 `Params`，最终被 `Sm90ScalarBroadcast` 在设备端广播给每个线程，作为 `Sm90Compute` 的输入片段之一。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成下面这个**「从配方到树」的全链路阅读任务**。

**任务**：用一个自定义 EVT 实现

\[
D = \mathrm{ReLU}\bigl(\alpha\cdot\mathrm{acc} + \beta\cdot C + b_{\text{row}}\bigr)
\]

即「线性组合 + 逐行 bias + ReLU」。

**步骤**：

1. **配方层**：在 [operations.hpp](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/operations.hpp) 找到对应元数据配方 `LinCombPerRowBiasEltAct`（[L185-L201](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/epilogue/fusion/operations.hpp#L185-L201)），确认它同时打开了 `IsSourceSupported`、`IsPerRowBiasSupported`、`IsEltActSupported`。
2. **树层**：参考测试文件 [test/unit/gemm/device/sm90_evt_operations.hpp:460-470](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/test/unit/gemm/device/sm90_evt_operations.hpp#L460-L470)，那里用 `Sm90EVT` 手写了一棵「`hma(β, C, hma(α·acc, row_bias))` + per-row bias」的树，作为模板。
3. **生命周期层**：在你的树上指出——哪个节点会让 `is_producer_load_needed()` 返回 `true`？（答：负责加载 bias 的 `Sm90RowBroadcast`/对应 load 节点，因为它要从 gmem 取逐行 bias。）哪个节点负责最终的 `tma_store`？（答：由 collective 统一回写 D，bias 节点只参与计算。）
4. **验证**：用一张表对照「数学符号 ↔ EVT 节点 ↔ callback 时机」三列，确保每个数学运算都能指到具体源码行。

**交付物**：一棵手画访客树 + 一张三列对照表。**运行验证**依赖 SM90a 硬件（运行 example 113 同款 `CUTLASS_NVCC_ARCHS=90a`），具体运行性能与数值待本地验证；本任务以源码阅读与树形推导为达标标准。

---

## 6. 本讲小结

- epilogue 是 GEMM 三段式的最后一段，消费 mainloop 的累加器 `acc`，完成 \(D=\mathrm{act}(\alpha\cdot\mathrm{acc}+\beta\cdot C+\cdots)\) 并写回显存；它和 mainloop 一样是无状态 collective，也用 producer（load warp，TMA 加载 C）/ consumer（store warps，融合计算 + TMA 写回）的 warp specialization。
- `fusion::FusionOperation`（`operations.hpp`）是一组**纯元数据配方**，只用类型与 `static constexpr bool` 描述「想算什么」，不含任何逻辑，从而把「意图」与「实现」解耦、跨架构复用。
- **EVT（访客树）** 用 `Sm90EVT<NodeOp, ChildOps...>`（即 `Sm90TreeVisitor`）组合：叶子（`AccFetch`/`SrcFetch`/`ScalarBroadcast`/`RowBroadcast`）取数据，中间节点（`Sm90Compute<multiplies/hma/ReLu/...>`）做逐元素运算，计算是后序遍历——先算孩子、再喂给根。
- 每个 visitor 节点通过两套 callback 接入流水线：producer 的 `begin/step/end`（发 TMA + expect-tx）和 consumer 的 `begin/previsit/visit/reduce/postreduce/tma_store/end`；整棵树的 callback 用 tuple 打包、`for_each` 遍历，加节点即加 tuple 项。
- 常用融合有预置别名：`Sm90LinearCombination`（\(\beta C+\alpha\cdot\mathrm{acc}\) 的 `hma` 树）、`Sm90LinCombEltAct`（再套一层 `ActivationFn`，传 `ReLu` 即 LinearCombination+ReLU）；`FusionCallbacks` 偏特化把元数据配方映射到这些树，`Arguments::operator Impl::Arguments()` 负责把扁平的 `{alpha,beta}` 翻译成与树同构的嵌套参数。

---

## 7. 下一步学习建议

- **去 [u3-l1](u3-l1-async-pipeline.md) 深入异步流水线**：本讲反复提到的 producer/consumer、`expect-tx`、多级缓冲锁，其底层同步原语在 `cutlass/pipeline/` 与 `PipelineTmaAsync` 中定义，那是理解「搬算重叠」的下一站。
- **去 [u3-l2](u3-l2-tma-copy.md) 深入 TMA**：epilogue 的 C 加载与 D 写回都依赖 TMA descriptor，理解 TMA 描述符如何表达一个张量盒，能帮你读懂 `Sm90SrcFetch`/`Sm90AuxLoad` 的 producer 路径。
- **手搓一棵自定义 EVT**：仿照 [examples/113_hopper_gemm_activation_fusion/sm90_lin_comb_elt_act_scaled.hpp](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/113_hopper_gemm_activation_fusion/sm90_lin_comb_elt_act_scaled.hpp) 与 [test/unit/gemm/device/sm90_evt_operations.hpp](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/test/unit/gemm/device/sm90_evt_operations.hpp)，用 `Sm90EVT` 写一个带 aux 输出（`Sm90AuxStore`）的 GEMM+融合，这是掌握 EVT 最快的路径。
- **扩展到 Blackwell**：阅读 `fusion/sm100_callbacks_tma_warpspecialized.hpp` 与 `fusion/sm100_visitor_*`，对比 SM90→SM100 的 visitor 接口差异，体会 EVT 抽象如何跨架构延伸。
