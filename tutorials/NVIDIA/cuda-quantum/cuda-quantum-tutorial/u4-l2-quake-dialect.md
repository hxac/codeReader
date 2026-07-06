# Quake 方言：操作与语义

## 1. 本讲目标

本讲深入 CUDA-Q 编译器的「量子中间表示」—— **Quake 方言**。读完本讲，你应当能够：

1. 说出 Quake 在 CUDA-Q 编译流水线中的位置（C++/Python 源码 → **Quake/CC MLIR** → QIR/LLVM），以及它为什么被设计成一个独立的 MLIR 方言。
2. 解释 Quake 区别于其他量子 IR 的核心特征：**内存语义（reference / wire 的内存模型）** 与 **值语义（value model）** 两种比特建模方式，以及它们各自的存在动机。
3. 识别 Quake 的核心操作家族：分配/释放（`alloca`/`dealloc`）、量子门（`h`/`x`/`rx`/`swap`…）、子内核应用（`apply`）、测量与判别（`mz`/`mx`/`my`/`discriminate`）。
4. 打开 `QuakeOps.td` 等 TableGen 定义文件，看懂一个操作的字段（参数、结果、trait、接口、汇编格式），并能据此反推它生成的 IR 长什么样。

本讲是「编译器」单元的第二篇，承接 [u4-l1](u4-l1-mlir-overview.md) 建立的 MLIR 全景图，把镜头拉近到 Quake 方言内部。它为后续 [u4-l4 AST Bridge](u4-l4-ast-bridge.md)（C++ 如何被翻译成这些 Quake 操作）、[u4-l6 优化 Pass](u4-l6-optimizer-pipeline.md)（如何在 Quake 层做量子优化）、[u4-l7 CodeGen](u4-l7-codegen-qir.md)（如何把 Quake 降低到 QIR）提供「地基词汇表」。

## 2. 前置知识

本讲假设你已经具备以下概念（若不熟悉，可先读 [u4-l1](u4-l1-mlir-overview.md)）：

- **MLIR 四抽象**：方言（Dialect）、操作（Operation）、特征（Trait）、Pass。一个 MLIR 程序就是若干「操作」组成的嵌套结构，每个操作属于某个「方言」，并可附带「特征」与「接口」描述其性质。
- **SSA 值（Static Single Assignment）**：每个值只被赋值一次，用 `%名字` 表示；操作之间通过 SSA 值传递数据，形成显式的数据流图（DAG）。
- **量子电路模型**：计算 = 一串「量子门」作用在「量子比特」上，最后「测量」得到经典结果。
- **TableGen（`.td`）**：LLVM/MLIR 用来声明操作、类型、接口的领域特定语言；编译期由 `mlir-tblgen` 翻译成 C++ 代码。本讲会手把手教你怎么读 Quake 的 `.td` 文件。

如果你还没建立「CUDA-Q 内核是什么样的」直觉，建议先看 [u1-l4 第一个 C++ 量子内核](u1-l4-first-cpp-kernel.md)：你会发现本讲里讨论的每一个 Quake 操作，几乎都能在那篇讲义里的 `__qpu__` 内核源码里找到对应。

## 3. 本讲源码地图

本讲涉及的关键源码文件集中在编译器的「方言定义」目录下：

| 文件 | 作用 |
| --- | --- |
| [cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeDialect.td](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeDialect.td) | 声明 `quake` 方言本身（名字、命名空间、简介）。整本讲「舞台」的入口。 |
| [cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeTypes.td](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeTypes.td) | 声明 Quake 的全部量子类型：`wire`/`ref`/`veq`/`control`/`measure`/`struq`/`cable`。是理解「两种语义」的钥匙。 |
| [cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeOps.td](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeOps.td) | 声明 Quake 的全部操作（约 1940 行）。本讲反复精读的核心文件。 |
| [cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeInterfaces.td](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeInterfaces.td) | 声明 `OperatorInterface`、`MeasurementInterface` 两个操作接口。 |
| [cudaq/include/cudaq/Optimizer/Dialect/Common/Traits.td](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Optimizer/Dialect/Common/Traits.td) | 声明跨方言复用的操作特征：`QuantumGate`/`Hermitian`/`Rotation` 与约束 `NumParameters`/`NumTargets`。 |
| [docs/sphinx/specification/quake-dialect.md](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/specification/quake-dialect.md) | 官方对 Quake 值模型（value model）动机的权威说明文档。本讲 4.1 节直接引用它。 |
| [cudaq/test/Translate/qalloc_initialization.qke](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/test/Translate/qalloc_initialization.qke) | 一段真实的 Quake MLIR 测试用例，本讲用来展示「真世界」的 IR 长什么样。 |

一个总体认知：Quake 的「舞台」很小——一个方言声明、七个类型、几十个操作。但它承载了 CUDA-Q 全部量子优化的可能性空间，理解它就等于拿到了读懂后续所有编译器讲义的「词典」。

## 4. 核心概念与源码讲解

### 4.1 内存语义 vs 值语义：Quake 的两种量子建模

#### 4.1.1 概念说明

一个量子门作用在比特上，本质是「读取当前态 → 做酉变换 → 写回新态」。问题在于：在 IR 里，**这个「写回」该如何表达？** Quake 给出了两种答案，这正是它最区别于其他量子 IR 的设计。

- **内存语义（memory semantics / reference model）**：把每个比特当成一块「易变内存」（volatile memory），用 `!quake.ref` 引用。门操作就像对内存地址的副作用（side-effect）：`quake.h %q0` 读取并就地改写 `%q0`。两个作用在同一 `ref` 上的门，因为共享同一内存地址，天然不能交换顺序——顺序由「内存副作用」隐式保证。

- **值语义（value semantics / value model）**：把每个比特当成一条流动的「线」（wire），用 `!quake.wire` 表示。门操作消费一条线、产出一条新线：`%q1 = quake.h %q0 : (!quake.wire) -> !quake.wire`。门与门之间的依赖由 SSA 值的「生产—消费」关系显式连成数据流。

两种语义的对比如下表：

| 维度 | 内存语义（reference） | 值语义（value / wire） |
| --- | --- | --- |
| 类型 | `!quake.ref`、`!quake.veq<N>` | `!quake.wire`、`!quake.control`、`!quake.cable<N>` |
| 门形态 | `quake.h %q : (!quake.ref) -> ()`（就地改写） | `%q1 = quake.h %q0 : (!quake.wire) -> !quake.wire`（消费旧线、产新线） |
| 依赖关系 | 隐式：同一 `ref` 上的副作用链 | 显式：SSA 值的数据流边 |
| 是否真 SSA | 是（每个 `ref` 只定义一次） | 「线性 SSA」：每条 wire 必须被使用**恰好一次** |
| 主要用途 | 桥接器（AST Bridge）默认产出形态 | 量子优化的目标形态 |

#### 4.1.2 核心流程：为什么要同时存在两种语义？

答案只有一句话：**为了让量子优化既安全又容易做。** 我们用一个具体例子说明，这个例子直接来自官方文档。

设想这样一段 Quake（内存语义）代码：对同一个比特 `%q0` 施加两次 Hadamard，中间隔着一次对整组比特 `%veq` 的测量：

```text
quake.h %q0 : (!quake.ref) -> ()
%result = quake.mz %veq : (!quake.veq<2>) -> cc.stdvec<i1>
quake.h %q0 : (!quake.ref) -> ()
```

一个天真的优化 Pass 看到「相邻两个 `H`」会想把它们消掉（因为 \(H \cdot H = I\)）。但这是**错的**：中间的测量作用在包含 `%q0` 的 `%veq` 上，已经塌缩了 `%q0` 的态。两次 `H` 之间隔着一次测量，不能合并。

在内存语义下，要正确判断「两个 `H` 能否合并」，Pass 必须**额外分析**：测量操作 `mz %veq` 隐式读写了 `%q0`——这种「隐式依赖」既容易出错又难写。这正是 Quake 引入值语义的动机：在值语义里，所有依赖都被 SSA 边**显式**串起来，没有隐式副作用，优化 Pass 不必再做复杂的别名分析。

文档用一个图直观对比了两种形态（左：内存语义，比特是一根贯穿左右的「线」；右：值语义，每个门把输入线变换成一条新的输出线）：

```text
        内存语义                                   值语义

    ┌──┐ ┌──┐     ┌──┐                  ┌──┐ %q0_1 ┌──┐     ┌──┐
%q0 ─┤  ├─┤  ├─···─┤  ├─ %q0    %q0_0 ─┤  ├───────┤  ├─···─┤  ├─ %q0_Z
    └──┘ └──┘     └──┘                  └──┘       └──┘     └──┘
```

值语义下，两次 `H` 分别作用在不同的 wire 值上（`%q0_L → %q0_M` 与 `%q0_Y → %q0_Z`），中间隔着一整段测量带回来的新 wire 链，**没有共享的 SSA 值**，于是「能否合并」直接退化为「两个 `H` 是否作用在同一条 wire 链的相邻段上」——一眼可见，无需别名分析。

> 结论：内存语义是「贴近编程模型、易产出」的形态；值语义是「显式数据流、易优化」的形态。CUDA-Q 的策略是 **AST Bridge 默认产出内存语义**，再由专门的 Pass（如 `ConvertToValueSemantics` 类变换）在需要时把它转成值语义去做量子优化。

#### 4.1.3 源码精读

**方言声明本身就把「内存语义」写在招牌上。** 看 `QuakeDialect.td`：

[QuakeDialect.td:L18-L26](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeDialect.td#L18-L26) —— 声明 `quake` 方言，名字 `"quake"`，摘要直写「Higher level, **memory-semantics** dialect for cudaq」，并自述「与具体量子硬件无关的可移植高层电路构造语言」。C++ 命名空间是 `cudaq::quake`（你会在生成的 `QuakeOps.cpp.inc` 里反复看到这个命名空间）。

**两种语义对应两族类型，定义在 `QuakeTypes.td`。** 先看内存语义的「引用」类型 `ref`：

[QuakeTypes.td:L94-L134](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeTypes.td#L94-L134) —— `ref` 是「对一条 wire 的引用」，文档里画了电路图并写道：「使用 `ref` 值的量子算子**隐式**对该 wire 值做 unwrap（读）—修改—wrap（写）」，并强调「同一 `ref` 上的操作不能互换顺序」。这段话是「内存语义」的官方定义。再看值语义的「线」类型 `wire`：

[QuakeTypes.td:L29-L62](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeTypes.td#L29-L62) —— `wire` 是「原始的量子 SSA 值」，但被声明为**线性类型（linear type）**：「wire 值必须被使用**恰好一次**」。文档比喻它「类似 volatile 内存」，区别于传统 SSA 的地方在于「使用即修改」。线性约束由类型系统在编译期强制，正好对应量子不可克隆定理（一条 wire 不能被复制给两个下游使用者）。

两个「容器/视图」类型把单个比特扩成一组：`veq`（一组 `ref`，内存语义）见 [QuakeTypes.td:L140-L167](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeTypes.td#L140-L167)，`cable`（一组 `wire`，值语义）见 [QuakeTypes.td:L212-L230](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeTypes.td#L212-L230)。

**两种语义之间的「翻译」操作。** 既然两种模型并存，就需要在它们之间转换。`unwrap`/`wrap` 就是这个桥梁：

[QuakeOps.td:L571-L598](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeOps.td#L571-L598) —— `quake.unwrap %ref : (!quake.ref) -> !quake.wire`：把一个引用「拆」成它背后的 wire，从而进入值语义域。[QuakeOps.td:L600-L618](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeOps.td#L600-L618) —— `quake.wrap %wire to %ref`：把变换后的 wire「包」回引用。两者成对出现，正是文档示例里「unwrap → 门 → wrap」模板的来源。

**官方对整套动机的权威叙述** 见：

[quake-dialect.md:L26-L60](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/specification/quake-dialect.md#L26-L60) —— 「Motivation」一节，用上面那个「两次 H 夹一次测量」的例子说明内存语义下优化的陷阱；[quake-dialect.md:L82-L135](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/specification/quake-dialect.md#L82-L135) —— 给出同一段程序在值语义下的等价形态（`unwrap`/`wrap` 成对出现），点明「值形态下，相邻 H 不再共享 SSA 值，故不能相互抵消」。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「AST Bridge 默认产出内存语义 IR」这件事。

**操作步骤**：

1. 写一个最小内核（示例代码，非项目原有）：

   ```cpp
   #include <cudaq.h>
   struct Kernel {
     __qpu__ void operator()() {
       cudaq::qubit q;
       h(q);
       h(q);      // 相邻两个 H
       mz(q);
     }
   };
   ```

2. 用 `nvq++` 编译它。`nvq++` 内部调用 `cudaq-quake`，由 [cudaq/tools/nvqpp/nvq++.in](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/tools/nvqpp/nvq%2B%2B.in) 第 916 行可知，`cudaq-quake` 会把 Quake MLIR 写到一个 `.qke` 中间文件。可加 `-k`（保留中间文件）或在 `build/` 下查找 `*.qke`。

**需要观察的现象**：在生成的 `.qke` 里，应能看到 `quake.h` 两次作用在**同一个** `%q`（`!quake.ref`）上，形如 `quake.h %q : (!quake.ref) -> ()`，且两个 `h` 之间没有 wire 的「生产—消费」关系——这就是内存语义的指纹。

**预期结果**：IR 里出现 `quake.alloca`、两次 `quake.h`（同一 `%q`）、一次 `quake.mz`，全程是 `ref` 类型而非 `wire` 类型。若你在 `.qke` 中看到 `quake.unwrap`/`quake.wrap` 成对出现，说明后续优化 Pass 已经把它转成了值语义。

> 若无法本地构建，标注「待本地验证」：重点不是跑通命令，而是建立「内存语义 = 同一 `ref` 上的副作用链」这一阅读直觉。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `wire` 类型被设计成「线性类型」（必须恰好使用一次），而 `ref` 不是？

> **答案**：`wire` 是值语义下显式数据流的载体，若一条 wire 被复制给两个下游门，就会产生「分叉」的量子态——违反不可克隆定理，也会破坏 SSA 数据流的单线性。`ref` 不需要线性约束，因为它表达的是「内存地址」，多个门可以合法地读写同一地址（顺序由副作用保证），如同多个语句操作同一 `volatile` 变量。

**练习 2**：在内存语义下，编译器凭什么保证「同一 `ref` 上的两个门不互换顺序」？

> **答案**：靠 MLIR 的内存副作用接口（`MemoryEffectsOpInterface`）。门操作对 `ref` 目标声明 `MemRead`/`MemWrite` 副作用（见 4.2 节 `QuakeOperator` 的 `getEffectsImpl`），MLIR 据此禁止把它们重排到彼此之上。

---

### 4.2 Quake 核心操作：分配、门、apply 与测量

#### 4.2.1 概念说明

掌握了两种语义，本节把它们装进具体的「操作家族」。Quake 的操作按功能可以分成六组：

1. **分配与释放**：`alloca`、`dealloc`、`init_state`——量子比特的「生」与「灭」。
2. **引用/向量操作**：`extract_ref`、`subveq`、`concat`、`veq_size`——对 `veq` 做索引、切片、拼接、求长，对应 C++ 里 `q[i]`、`.slice(...)` 等写法。
3. **量子门**：`h`/`x`/`y`/`z`/`s`/`t`/`rx`/`ry`/`rz`/`r1`/`swap`/`u2`/`u3` 等——电路里的「主角」。
4. **子内核应用**：`apply`、`compute_action`——把一个内核当作可受控/可求逆的整体调用。
5. **测量与判别**：`mz`/`mx`/`my`、`discriminate`——量子态到经典比特的转换。
6. **值语义辅助**：`unwrap`/`wrap`、`null_wire`/`sink`、`to_ctrl`/`from_ctrl`——仅在内存↔值语义转换期间出现。

一个关键点：第 3 组（门）和第 5 组（测量）的操作数量最多，但它们**不是一个个手写的**，而是用 TableGen 的「基类 + 一行声明」批量生成的——这正是 4.3 节要拆解的技巧。本节先把它们当成「黑盒操作」看清形状与用法。

#### 4.2.2 核心流程：一段真实 IR 是怎么拼出来的

我们直接看一段来自测试套件的**真实** Quake IR（这是 CUDA-Q 自己用来回归测试的 `.qke` 文件，不是杜撰）：

[qalloc_initialization.qke:L28-L47](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/test/Translate/qalloc_initialization.qke#L28-L47) —— 摘录其中的量子部分：

```mlir
%3 = quake.alloca !quake.veq<?>[%1 : i64]          // ① 分配运行时大小的比特向量
%4 = quake.init_state %3, %2 : (...) -> !quake.veq<?>   // ② 用态向量初始化
%5 = quake.veq_size %4 : (!quake.veq<?>) -> i64     // ③ 取长度（运行时）
// ... cc.loop 遍历每个比特 ...
%9 = quake.extract_ref %4[%arg1] : (!quake.veq<?>, i64) -> !quake.ref   // ④ 取第 i 个 ref
quake.h %9 : (!quake.ref) -> ()                     // ⑤ 对它施加 H 门
// ... 循环结束 ...
%7 = quake.extract_ref %4[1] : (!quake.veq<?>) -> !quake.ref
%8 = quake.extract_ref %4[0] : (!quake.veq<?>) -> !quake.ref
quake.x [%7] %8 : (!quake.ref, !quake.ref) -> ()   // ⑥ 受控 X：方括号里是控制比特
```

这段 IR 把本节要讲的几组操作串成了一条流水线：

```text
alloca(分配) → init_state(初始化) → veq_size(求长)
            → extract_ref(取下标) → h(门)            // 循环体
            → x [control] target(受控门)
```

注意几个「语法指纹」，它们贯穿整个 Quake：

- **方括号 `[...]` = 控制比特位置**。`quake.x [%7] %8` 表示「以 `%7` 为控制、`%8` 为目标」的 CNOT。这是 Quake 用**位置**而非用不同操作名来表达「受控」的统一约定——所有门共用一套语法，控制比特写在方括号里。
- **圆括号 `(...)` = 旋转参数**。如 `quake.rx (%theta) %q`。门有没有参数、有几个，由 4.3 节的 `NumParameters<n>` 约束决定。
- **`<adj>` = 伴随（逆）**。如 `quake.h <adj> %q` 表示 `H†`。
- **`neg` = 负控**。`quake.x [%c neg [true]] %t` 表示「控制比特为 0 时才触发」。

#### 4.2.3 源码精读

**① 分配：`alloca`。**

[QuakeOps.td:L39-L109](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeOps.td#L39-L109) —— `quake.alloca` 分配单个 `!quake.ref` 或一组 `!quake.veq<N>`，**所有比特初始化为 `|0⟩`**。长度可静态（`!quake.veq<4>`）也可动态（`alloca(%size : i32) !quake.veq<?>`，对应运行时才知道大小的内核）。文档点名两个下游 Pass：`QuakeAddDeallocs` 与 `UnwindLowering` 会自动插入 `dealloc`，因为 QIR 这类目标要求 alloc/dealloc 成对。它带 `MemoryEffects<[MemAlloc, MemWrite]>` 副作用。

**释放：`dealloc`。**

[QuakeOps.td:L137-L165](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeOps.td#L137-L165) —— 与 `alloca` 对偶，回收单个 `ref` 或一组 `veq`。注意它声明了 `[MemFree]>` 副作用。

**② 取下标：`extract_ref`。**

[QuakeOps.td:L206-L261](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeOps.td#L206-L261) —— 从 `veq` 取出第 `i` 个 `ref`。它带 `[Pure]` 特征（无副作用、可重排/CSE），因为取出的 `ref` 只是「指向同一比特的另一个名字」，并不复制量子态。注意它同时接受一个 SSA `index` 和一个 `I64Attr rawIndex`——前者是运行时下标，后者是编译期常量下标，二者互斥（见 `hasConstantIndex()`）。这是 Quake 普遍采用的「静态/动态双轨」设计。

**③ 门：以 `h` 与 `x` 为例。**

[QuakeOps.td:L1495-L1515](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeOps.td#L1495-L1515) —— `HOp`（汇编名 `h`），声明为 `OneTargetOp<"h", [Hermitian]>`：单目标、零参数、且自伴（\(H = H^\dagger\)）。文档给出矩阵 \(H = \frac{1}{\sqrt 2}\begin{pmatrix}1&1\\1&-1\end{pmatrix}\)。

[QuakeOps.td:L1715-L1731](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeOps.td#L1715-L1731) —— `XOp`（`x`），同为 `OneTargetOp`、`[Hermitian]`。注意：CNOT（受控非）在 Quake 里**不是**一个独立操作，而是 `x` 带上方括号写控制比特：`quake.x [%c] %t`。Toffoli 同理：`quake.x [%a, %b] %t`。

[QuakeOps.td:L1554-L1571](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeOps.td#L1554-L1571) —— `RxOp`（`rx`），声明为 `OneTargetParamOp<"rx", [Rotation]>`：单目标、**一参数**、旋转门。这是带参数门的标准模板。

[QuakeOps.td:L1631-L1651](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeOps.td#L1631-L1651) —— `SwapOp`（`swap`），`TwoTargetOp<"swap", [Hermitian]>`：双目标、零参数。

这些门都共享同一个「操作基类」`QuakeOperator`，它统一规定了「参数 / 控制 / 目标 / 负控 / 伴随」五个操作数段与统一汇编格式——细节留到 4.3 节。

**④ 子内核应用：`apply`。**

[QuakeOps.td:L384-L481](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeOps.td#L384-L481) —— `quake.apply` 是「带控制/伴随地调用一个用户内核」的统一入口，对应 C++ 的 `cudaq::control` / `cudaq::adjoint` / `compute_action`（见 [u2-l2](u2-l2-gates-and-modifiers.md)）。它实现 `CallOpInterface`（像函数调用），可带 `is_adj`（伴随）、`controls`（控制比特列表）和 `actuals`（实参）。文档说明：用户内核同时定义了「带谓词（受控）」和「不带谓词」两种形式，`apply` 引用其中之一，由后续 Pass 补出另一种。[QuakeOps.td:L484-L510](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeOps.td#L484-L510) 的 `compute_action` 则专门捕获「计算—作用—逆计算」（CAC†）惯用法，`is_dagger` 属性可把它反转成「逆计算—作用—计算」。

**⑤ 测量与判别。**

[QuakeOps.td:L1101-L1134](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeOps.td#L1101-L1134) —— 定义了一个 TableGen **类** `Measurement`（不是具体操作），把 `mx`/`my`/`mz` 三个测量的共性抽出来：都带 `MeasurementInterface` 与 `QuantumMeasure` 特征，都接受 `targets` 与可选的 `registerName`（命名寄存器，对应 [u2-l3](u2-l3-measurement-and-sampling.md) 讲过的寄存器命名）。结果类型是 `!quake.measure` 或 `!cc.measure_handle`（后者是 `cudaq::measure_handle` 调用者专用的句柄形态，见注释 L1107-L1114）。

[QuakeOps.td:L1164-L1177](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeOps.td#L1164-L1177) —— `MzOp`（`mz`），`def MzOp : Measurement<"mz">`，一行声明就生成了完整的 Z 基测量操作。`mx`/`my` 同理（L1136/L1150）。

[QuakeOps.td:L1179-L1218](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeOps.td#L1179-L1218) —— `quake.discriminate`：把测量结果（`!quake.measure` 或句柄）**判别**成经典整数值（通常是 `i1`，qutrit 是 `i2`，一般 qudit 是 `i8`）。这正是 [u2-l5](u2-l5-mid-circuit-measurement.md) 讲过的「测量句柄被 `if` 消费时插入 `quake.discriminate`」在 IR 层的落点。文档特别区分了「标量句柄是 pure、可 CSE」与「向量句柄别名可变存储、不可投机执行」两种情形（L1190-L1202），这是给优化 Pass 看的精细副作用约束。

#### 4.2.4 代码实践

**实践目标**：用一段最小 C++ 内核，亲手「召唤」出 `alloca`、受控 `x`、`mz` 三种操作，并在 IR 里指认它们。

**操作步骤**：

1. 编写 Bell 态内核（示例代码）：

   ```cpp
   #include <cudaq.h>
   struct Bell {
     __qpu__ void operator()() {
       cudaq::qvector<2> q;   // 期望生成 quake.alloca !quake.veq<2>
       h(q[0]);                // 期望生成 quake.h
       x(q[0], q[1]);          // 期望生成 quake.x [%q0] %q1（受控）
       mz(q);                  // 期望生成 quake.mz ... !quake.veq<2>
     }
   };
   ```

   > 说明：`x(control, target)` 是 CUDA-Q 对受控非的语法糖，等价于 `cudaq::ctrl(x, control, target)`（见 [u2-l2](u2-l2-gates-and-modifiers.md)）。

2. 用 `nvq++ bell.cpp -o bell.x` 编译；如需查看 Quake IR，可在构建目录下查找 `bell.qke`（`cudaq-quake` 默认输出 Quake MLIR 到 `.qke`，见 [nvq++.in:L916](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/tools/nvqpp/nvq%2B%2B.in#L916)）。也可用 `cudaq-opt bell.qke` 进一步处理。

**需要观察的现象**：`.qke` 中应依次出现：

- `quake.alloca !quake.veq<2>`（或两个 `quake.alloca !quake.ref`）；
- `quake.h %q0 : (!quake.ref) -> ()`；
- `quake.x [%q0] %q1 : (!quake.ref, !quake.ref) -> ()`——注意控制比特在方括号里；
- `quake.mz %... : (...) -> !cc.stdvec<i1>`（或 `!quake.measure` 形态）。

**预期结果**：上述四种操作一一对应，证明你已能从 IR 反推出源码语义。若 `nvq++` 默认删掉了中间 `.qke`，可改用 `cudaq-quake` 直接前端、或查阅 [u8-l2 调试与日志](u8-l2-debugging-and-logging.md) 介绍的环境变量手段。

> 命令的精确开关可能随版本变化，标注「待本地验证」；核心是确认 `.qke` 文件存在且含上述 IR 形态。

#### 4.2.5 小练习与答案

**练习 1**：`quake.x [%a, %b] %c` 在电路上等价于什么门？为什么 Quake 不为它单独定义一个操作？

> **答案**：等价于 Toffoli（双控非，CCX）。Quake 不单独定义，是因为它用「方括号写控制比特」的**统一约定**表达「受控」——任何门加上任意多个控制比特都只需在方括号里列出，无需为每种受控组合造一个新操作名。这把「门」与「控制」两个正交概念解耦，大大压缩了操作数量。

**练习 2**：`extract_ref` 标了 `[Pure]`，`alloca` 没标。这个差异对优化 Pass 意味着什么？

> **答案**：`[Pure]` 表示操作无副作用、仅依赖输入——优化器可以自由地把它 CSE（公共子表达式消除）、死代码消除、或移到循环外。`alloca` 有 `MemAlloc`/`MemWrite` 副作用，不能随意移动或合并（两次 `alloca` 不是「同一块内存」）。这就是为什么 `extract_ref %v[0]` 出现两次可以被合并，而 `alloca` 不会。

**练习 3**：`mz` 返回的 `!quake.measure` 为什么不直接是 `i1`？中间多一个 `discriminate` 操作有什么好处？

> **答案**：`measure` 是「测量产生的经典结果」，但它的逻辑态数量取决于目标系统（比特是 2 态、qutrit 是 3 态…），到 `i1`/`i2`/`i8` 的「判别」是后一步。把「测量」与「判别」分成两个操作，让 mid-circuit measurement 可以「延迟判别」（见 [u2-l5](u2-l5-mid-circuit-measurement.md)）：句柄可以被多次读取（标量形式是 pure），只有在真正进入 `if` 时才 `discriminate` 成比特。这给模拟器后端留出了批量采样与逐 shot 塌缩两种实现空间。

---

### 4.3 用 TableGen 阅读 Quake 操作定义

#### 4.3.1 概念说明

前两节我们把 Quake 操作当「黑盒」用。本节教你怎么**自己读** `QuakeOps.td`——这是后续阅读任何 Quake 相关 Pass、改写或新增操作时必备的技能。

TableGen（`.td`）是 LLVM 的「声明式代码生成」语言：你用 `def` 声明一个操作，`mlir-tblgen` 在编译期把它展开成几千行 C++（`QuakeOps.h.inc`/`.cpp.inc`）。读 `.td` 而非读生成代码，是因为 `.td` 是「真源」，简洁且权威。

一个典型的操作定义长这样（伪代码）：

```tablegen
def XOp : OneTargetOp<"x", [Hermitian]> {
  let summary = "...";
  let description = [{ ... }];
  // arguments/results/assemblyFormat 由基类 OneTargetOp 提供
}
```

读懂它要抓四个要素：

1. **基类**（`OneTargetOp` / `OneTargetParamOp` / `TwoTargetOp` / `QuakeOperator`）：决定了操作的「骨架」（几个目标、几个参数、统一汇编格式）。
2. **特征列表**（`[Hermitian]`、`[Rotation]`、`[Pure]`）：标记操作的数学/副作用性质，给优化 Pass 看。
3. **参数与结果**（`let arguments`、`let results`）：操作数与返回值的类型约束。
4. **汇编格式**（`let assemblyFormat`）：操作在 `.qke` 里打印出来长什么样——这是你「肉眼读 IR」与「读 `.td`」之间的桥梁。

#### 4.3.2 核心流程：从一个具体门回溯到它的定义

以「IR 里看到 `quake.x [%c] %t`」为例，逆向阅读流程是：

```text
IR: quake.x [%c] %t : (!quake.ref, !quake.ref) -> ()
        │ 汇编名 "x"
        ▼
def XOp : OneTargetOp<"x", [Hermitian]>            // QuakeOps.td:1715
        │ 基类 OneTargetOp
        ▼
class OneTargetOp : QuakeOperator<..., [NumTargets<1>, NumParameters<0>]>  // L1332
        │ 基类 QuakeOperator
        ▼
class QuakeOperator : QuakeOp<..., [QuantumGate, OperatorInterface, ...]>  // L1224
   统一规定：arguments = (is_adj, parameters, controls, targets, negated_qubit_controls)
             assemblyFormat = (`<adj>`)? (`(params)`)? (`[controls]`)? targets `:` ...
```

也就是说：IR 里那个简洁的 `quake.x [%c] %t`，其**每一个语法部件**（`x`、方括号、目标、`:`）都由「基类 `QuakeOperator` 的统一汇编格式」规定；而 `x` 之所以「无参数、单目标、自伴」，是因为 `XOp` 选择了 `OneTargetOp`（注入 `NumTargets<1>, NumParameters<0>`）和 `[Hermitian]` 特征。一条 `def` 语句，就把 IR 形态、操作数约束、数学性质一次性钉死。

这套「基类 + 特征 + 一行 `def`」的设计，让新增一个门只需写 5~10 行 TableGen——这正是 Quake 能用不到两千行 `.td` 定义出几十个门的原因。

#### 4.3.3 源码精读

**操作基类 `QuakeOperator`（所有门的「模板」）。**

[QuakeOps.td:L1224-L1330](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeOps.td#L1224-L1330) —— 这是本节最关键的一段。要点逐条对应：

- **基类链**（L1226-L1228）：`QuakeOperator` 继承 `QuakeOp`，并强制带上 `QuantumGate`、`OperatorInterface`、`AttrSizedOperandSegments`、`MemoryEffectsOpInterface` 四个特征/接口——所以**所有门自动是「量子门」、自动实现「算子接口」、自动按段定长操作数、自动声明内存副作用**。
- **统一参数段**（L1230-L1236）：每个门的操作数都按相同顺序排列——`is_adj`（是否伴随）、`parameters`（旋转角等经典参数）、`controls`（控制比特）、`targets`（目标比特）、`negated_qubit_controls`（哪些控制是负控）。这五段正对应 IR 里的 `<adj>`、`(...)`、`[...]`、目标、`neg`。
- **统一汇编格式**（L1290-L1294）：用一条 `assemblyFormat` 把上述五段的打印顺序钉死：可选 `<adj>` → 可选 `(params)` → 可选 `[controls (neg ...)?]` → `targets` → `:` 类型。**这一行决定了你眼里所有门的 IR 长相。**
- **副作用实现**（L1303-L1306）：`getEffectsImpl` 调 `getOperatorEffectsImpl(effects, controls, targets)`——控制比特只读、目标比特读写，由公共 helper 统一注入，无需每个门重复写。
- **算子接口方法**（L1318-L1328）：`getTarget(i)`、`getParameter(i)`、`getOperatorMatrix(matrix)` 等——让优化 Pass 不必关心具体是哪个门，只需通过 `OperatorInterface` 拿到「控制/目标/矩阵」即可（接口定义见 [QuakeInterfaces.td:L14-L89](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeInterfaces.td#L14-L89)）。

**三个糖基类（按「目标数 × 参数数」分类）。**

[QuakeOps.td:L1332-L1342](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeOps.td#L1332-L1342) —— 三行就定义了三个常用基类：

| 基类 | 注入约束 | 用于 | 例子 |
| --- | --- | --- | --- |
| `OneTargetOp` | `NumTargets<1>, NumParameters<0>` | 单目标、无参数 | `h`/`x`/`y`/`z`/`s`/`t` |
| `OneTargetParamOp` | `NumTargets<1>, NumParameters<1>` | 单目标、一参数 | `rx`/`ry`/`rz`/`r1` |
| `TwoTargetOp` | `NumTargets<2>, NumParameters<0>` | 双目标、无参数 | `swap` |

`NumTargets<n>` 与 `NumParameters<n>` 的定义见 [Traits.td:L49-L55](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Optimizer/Dialect/Common/Traits.td#L49-L55)：它们是 `PredOpTrait`，即「编译期断言操作数个数等于 n」。于是 `def XOp : OneTargetOp<"x", ...>` 在生成 C++ 时会带一个 `assert(targets.size()==1 && parameters.size()==0)`，构造错误 IR 会在 verifier 阶段被拒。

**特征 `QuantumGate`/`Hermitian`/`Rotation`。**

[Traits.td:L18-L28](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Optimizer/Dialect/Common/Traits.td#L18-L28) —— 三个 `NativeOpTrait`，分别标记「这是量子门」「自伴（\(U=U^\dagger\)，伴随等于自身）」「旋转门」。优化 Pass 据此做特化：例如求伴随时，`Hermitian` 门直接原样保留；`Rotation` 门的伴随只需把参数取负。

**两个操作接口。**

[QuakeInterfaces.td:L14-L89](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeInterfaces.td#L14-L89) —— `OperatorInterface` 暴露 `isAdj`/`getParameters`/`getControls`/`getTargets`/`getNegatedControls`/`getOperatorMatrix`，让 Pass 以「统一的算子视角」处理任何门（无需 `dyn_cast` 到具体 `XOp`/`HOp`）。[QuakeInterfaces.td:L91-L134](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeInterfaces.td#L91-L134) —— `MeasurementInterface` 暴露 `getTargets`/`getOptionalRegisterName`/`setRegisterName`，让测量相关的 Pass（如术语合成、测量基变换，见 [u3-l3](u3-l3-observe-and-spinop.md)）统一处理 `mx`/`my`/`mz`。

**一个「特例」：`exp_pauli` 不走糖基类。**

[QuakeOps.td:L1348-L1357](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeOps.td#L1348-L1357) —— `quake.exp_pauli`（实现 \(e^{i\theta P}\)，\(P\) 为泡利张量积）直接继承 `QuakeOp` 并手动列出 `QuantumGate`/`OperatorInterface`，因为它需要一个额外的 `pauli` 操作数（泡利字面量字符串），不适合塞进 `QuakeOperator` 的五段模板。这个对比正好说明：**糖基类是「常用形状的快捷方式」，遇到特殊形状仍可退回到裸 `QuakeOp` + 手工字段。**

#### 4.3.4 代码实践

**实践目标**：从 `QuakeOps.td` 中摘录三个操作的定义，对照说明它们各自会生成什么 IR。

**操作步骤**：

1. 打开 [QuakeOps.td](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeOps.td)，摘录以下三个 `def`：
   - `def HOp : OneTargetOp<"h", [Hermitian]>`（L1495）
   - `def RxOp : OneTargetParamOp<"rx", [Rotation]>`（L1554）
   - `def SwapOp : TwoTargetOp<"swap", [Hermitian]>`（L1631）
2. 对每个操作，按「基类 → 注入约束 → 特征 → 汇编格式」四步推导它的 IR 形态。

**需要观察的现象 / 推导结果**：

| 操作 | 基类注入 | 特征含义 | 推出的 IR 形态 |
| --- | --- | --- | --- |
| `h` | `NumTargets<1>, NumParameters<0>` | `Hermitian`：伴随=自身 | `quake.h %q : (!quake.ref) -> ()`（无参数、单目标） |
| `rx` | `NumTargets<1>, NumParameters<1>` | `Rotation`：伴随=参数取负 | `quake.rx (%theta) %q : (f64, !quake.ref) -> ()`（一参数、单目标） |
| `swap` | `NumTargets<2>, NumParameters<0>` | `Hermitian` | `quake.swap %a, %b : (!quake.ref, !quake.ref) -> ()`（双目标） |

3. 写一段最小 C++ 内核同时召唤这三者（示例代码）：

   ```cpp
   #include <cudaq.h>
   struct ThreeGates {
     __qpu__ void operator()(double theta) {
       cudaq::qvector<2> q;
       h(q[0]);
       rx(theta, q[0]);
       swap(q[0], q[1]);
       mz(q);
     }
   };
   ```

**预期结果**：编译后 `.qke` 中应出现与上表「推出的 IR 形态」逐一对应的操作行。这种「从 `.td` 定义反推 IR」的能力，是阅读任何 Quake 相关 Pass（它们都按 `OperatorInterface` 处理这些门）的前置技能。

> 若本地未构建，可改为纯阅读型实践：在 `QuakeOps.td` 中找到 `RzOp`/`TOp`/`U3Op`，自行推导它们的 IR 形态，再对照文档里的矩阵说明验证理解。

#### 4.3.5 小练习与答案

**练习 1**：`def RyOp : OneTargetParamOp<"ry", [Rotation]>` 比它的基类多写了什么？为什么 `let arguments`/`let results` 都没有出现？

> **答案**：`RyOp` 比基类只多了名字 `"ry"` 和特征 `[Rotation]`——它本身**没有**重新声明 `arguments`/`results`/`assemblyFormat`，因为这些全部由基类链 `OneTargetParamOp → QuakeOperator → QuakeOp` 提供。`OneTargetParamOp` 注入 `NumParameters<1>`，`QuakeOperator` 统一规定操作数段与汇编格式。这正是 TableGen 继承的威力：一条 `def` 即可生成一个完备的操作。

**练习 2**：为什么 `OperatorInterface` 要把 `getOperatorMatrix` 设为接口方法，而不是让 Pass 直接 `dyn_cast<HOp>` 去查矩阵？

> **答案**：因为很多量子优化 Pass（如门合并、相邻门消去）只关心「这个门的矩阵是什么、作用在哪些控制/目标上」，而不关心它具体是 `HOp` 还是 `XOp`。通过接口，Pass 写一遍就能处理所有门；新增门时只要在 `def` 里实现 `getOperatorMatrix`（或沿用基类默认），Pass 自动获得支持。这是 MLIR「接口多态」优于「C++ 类型多态」的典型场景，也避免了 Pass 代码里出现长长的 `if/dyn_cast` 链。

**练习 3**：`QuakeOperator` 的 `assemblyFormat` 里有 `( `[` $controls^ (`neg` $negated_qubit_controls^ )? `]`)?`。请据此写出「以 `%c` 为负控、`%t` 为目标的 X 门」的 IR。

> **答案**：`quake.x [%c neg [true]] %t : (!quake.ref, !quake.ref) -> ()`。方括号里的 `neg [true]` 标记该控制比特为负控（control-on-0）。这与 [u2-l2](u2-l2-gates-and-modifiers.md) 讲的「写在比特上的 `!` 操作符」对应：负控在源码层是 `!q`，在 IR 层是 `negated_qubit_controls` 属性。

## 5. 综合实践

把本讲三块知识串起来，完成下面这个「IR 考古」小任务。

**任务**：下面是一段真实的 Quake IR（节选自 [qalloc_initialization.qke](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/test/Translate/qalloc_initialization.qke)），请逐行「考古」：

```mlir
%3 = quake.alloca !quake.veq<?>[%1 : i64]
%4 = quake.init_state %3, %2 : (!quake.veq<?>, !cc.ptr<f64>) -> !quake.veq<?>
%5 = quake.veq_size %4 : (!quake.veq<?>) -> i64
%9 = quake.extract_ref %4[%arg1] : (!quake.veq<?>, i64) -> !quake.ref
quake.h %9 : (!quake.ref) -> ()
%7 = quake.extract_ref %4[1] : (!quake.veq<?>) -> !quake.ref
%8 = quake.extract_ref %4[0] : (!quake.veq<?>) -> !quake.ref
quake.x [%7] %8 : (!quake.ref, !quake.ref) -> ()
```

**要求**：

1. **指认操作**：列出出现的所有 Quake 操作，并按本讲 4.2 节的「六组家族」分类（分配/向量操作/门/…）。
2. **回溯定义**：任选其中 3 个操作，在 [QuakeOps.td](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeOps.td) 中找到它们的 `def`，抄出「基类 + 特征 + 关键字段」，并据此解释 IR 中每一行的语法部件（方括号、`<?>`、类型签名）从何而来。
3. **判断语义**：这段 IR 是内存语义还是值语义？依据是什么？（提示：看 `h`/`x` 作用的对象类型，以及是否出现 `unwrap`/`wrap`。）
4. **手写源码**：写一段最小 C++ `__qpu__` 内核，使其尽可能生成与上表相似的 IR（含 `alloca`、`h`、受控 `x`、`mz`），并用 `nvq++` 编译查看 `.qke` 验证（或标注「待本地验证」）。

**参考思路**：

- 操作分类：`alloca`/`init_state`（分配/初始化组）、`veq_size`/`extract_ref`（向量操作组）、`h`/`x`（门组）。
- 定义回溯示例：`h` ← `def HOp : OneTargetOp<"h", [Hermitian]>`（L1495），其单目标无参数形态由 `OneTargetOp` 的 `NumTargets<1>, NumParameters<0>` 决定，方括号控制位语法由 `QuakeOperator` 的统一 `assemblyFormat`（L1290）决定。
- 语义判断：全程使用 `!quake.ref`/`!quake.veq`，门写作 `quake.h %9 : (!quake.ref) -> ()`（就地副作用、无返回 wire），且无 `unwrap`/`wrap`——**典型内存语义**。
- 源码：可参考 4.2.4 的 Bell 态内核，把 `init_state`（用态向量初始化）那条加进去即可。

## 6. 本讲小结

- **Quake 是 CUDA-Q 自定义的量子电路方言**，处在编译流水线的中段（C++/Python 源码 → **Quake/CC MLIR** → QIR/LLVM），与具体硬件无关，是承载量子优化的主战场。
- **Quake 的灵魂是「两种语义」**：内存语义（`ref`/`veq`，门作为对易变内存的副作用，AST Bridge 默认产出）与值语义（`wire`/`control`/`cable`，门消费并产生新线，把数据流显式化以便安全优化）。两者通过 `unwrap`/`wrap` 互相转换。
- **核心操作分六组**：分配/释放（`alloca`/`dealloc`/`init_state`）、向量操作（`extract_ref`/`subveq`/`concat`/`veq_size`）、门（`h`/`x`/`rx`/`swap`…，控制位写方括号、参数写圆括号、伴随写 `<adj>`、负控写 `neg`）、子内核应用（`apply`/`compute_action`）、测量与判别（`mz`/`mx`/`my`/`discriminate`）、值语义辅助（`unwrap`/`wrap`/`null_wire`/`sink`/`to_ctrl`）。
- **门与测量是 TableGen 批量生成的**：一个 `QuakeOperator` 基类统一规定五段操作数与汇编格式；`OneTargetOp`/`OneTargetParamOp`/`TwoTargetOp` 三个糖基类按「目标数 × 参数数」分类；具体门只需一行 `def`（如 `def XOp : OneTargetOp<"x", [Hermitian]>`）。
- **读 `.td` 的四要素**：基类（决定骨架）、特征（`QuantumGate`/`Hermitian`/`Rotation`/`Pure`，标记性质给 Pass 看）、参数/结果（类型约束）、汇编格式（决定 IR 长相）。配套的 `OperatorInterface`/`MeasurementInterface` 让 Pass 以统一视角处理任一门/测量。
- **CNOT/Toffoli 不是独立操作**：它们是 `x` 带控制位方括号的语法形式，体现了 Quake「门」与「控制」正交解耦的设计。

## 7. 下一步学习建议

本讲建立的是「Quake 静态词典」。要让它「动」起来，建议接着读：

1. **[u4-l3 CC 方言](u4-l3-cc-dialect.md)**：本讲的 IR 例子里那些 `cc.loop`/`cc.cast`/`cc.stdvec` 来自 CC 方言——它建模 Quake 函数里的经典计算（循环、数组、指针），是 Quake 的「另一半」。
2. **[u4-l4 AST Bridge](u4-l4-ast-bridge.md)**：本讲反复说「AST Bridge 默认产出内存语义 IR」——下一讲就拆解 `x(q)` 这样的 C++ 调用是如何被翻译成 `quake.x %q` 的，把 4.1 节的「默认产出」落实成具体代码路径。
3. **[u4-l6 优化 Pass 流水线](u4-l6-optimizer-pipeline.md)**：本讲埋了两处伏笔——「值语义便于优化」与「`OperatorInterface` 让 Pass 统一处理门」。优化 Pass 讲义会展示 Pass 如何利用本讲讲的 trait/interface 真正消去相邻门、合成术语。
4. **动手验证**：用本讲 4.2.4 的方法，把你写过的任意一个内核（如 [u1-l4](u1-l4-first-cpp-kernel.md) 的 Bell 态）编译出 `.qke`，逐行指认其中的 Quake 操作——这是巩固本讲最快的方式。

> 阅读源码建议：先通读 [quake-dialect.md](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/docs/sphinx/specification/quake-dialect.md) 建立价值观，再把 [QuakeOps.td](https://github.com/NVIDIA/cuda-quantum/blob/61face2b9a41d1ef9b6ea6e7941bc22441c75ab9/cudaq/include/cudaq/Optimizer/Dialect/Quake/QuakeOps.td) 当字典按需查——不必一次读完 1940 行，遇到一个操作回头查它的 `def` 即可。
