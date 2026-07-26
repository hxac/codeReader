# XLA / StableHLO 与 tf2xla

## 1. 本讲目标

上一讲（u7-l1）我们建立了「MLIR + TF dialect」的全局认知：TF 把计算图表示成 MLIR 的 `tf` dialect，再经一系列 pass 做 lowering。但 lowering 的「终点」是什么？答案就是 XLA 与 StableHLO。

本讲要解决三个问题：

1. **XLA 是什么、为什么要编译一张图**——理解 JIT/AOT 编译带来的性能收益，以及 TF 的 op 与 XLA 的 HLO 之间的语义鸿沟。
2. **tf2xla 的翻译职责**——掌握 `compiler/tf2xla` 如何把一张 `GraphDef`「翻译」成 `XlaComputation`，包含经典路径与 MLIR 路径两条链路。
3. **StableHLO 作为可移植 IR 的定位**——认识 StableHLO 为何被设计成「跨框架、跨运行时的稳定序列化格式」，以及它和 XLA、MHLO 的层级关系。

学完后，你应能在仓库中追踪「一段 TF 子图 → XLA 计算」的完整调用链，并能说清 StableHLO 在整条编译流水线里扮演的角色。

## 2. 前置知识

本讲为 advanced 层，需要你已掌握以下概念（来自前置讲义）：

- **计算图与 GraphDef**（u3-l1）：TF 用 `Graph`/`Node`/`Edge` 表示 DAG，序列化为扁平的 `GraphDef`（`NodeDef` 列表）。
- **Op 与 OpKernel**（u4-l1、u4-l2）：Op 是声明式的「说明书」，OpKernel 是「干活的工人」；`Compute(OpKernelContext*)` 是执行入口。
- **MLIR dialect 与 pass**（u7-l1）：dialect 是 op 的命名空间，pass 是 IR 改写单元，lowering 是把高层 dialect 降级到低层。
- **DeviceType / 设备抽象**（u6-l1）：op 最终要落到某台设备（CPU/GPU/TPU）上执行。

几个本讲会用到的术语：

- **HLO（High-Level Optimizer）**：XLA 自定义的中间表示，是「算子级」的 IR。XLA 的优化（融合、缓冲区复用）都发生在 HLO 层。
- **JIT（Just-In-Time）/ AOT（Ahead-Of-Time）**：前者运行时按需编译子图，后者在部署前离线编译成机器码（如 `tfcompile`）。
- **算子融合（fusion）**：把多个细粒度 op 合并成一个更大粒度的 kernel，减少 kernel 启动开销与中间张量的显存读写。这是 XLA 最核心的收益来源。

## 3. 本讲源码地图

本讲涉及的关键文件按职责分为三组：

| 文件 | 作用 |
| --- | --- |
| [tensorflow/compiler/tf2xla/tf2xla.h](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/tf2xla/tf2xla.h) | tf2xla 的公共头，声明两个转换入口函数 |
| [tensorflow/compiler/tf2xla/tf2xla.cc](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/tf2xla/tf2xla.cc) | 经典路径实现：图 → XLA 计算（符号执行） |
| [tensorflow/compiler/tf2xla/tf2xla.proto](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/tf2xla/tf2xla.proto) | 定义 `Config`/`Feed`/`Fetch`/`Variable`，描述「编译哪一部分图」 |
| [tensorflow/compiler/tf2xla/graph_compiler_util.cc](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/tf2xla/graph_compiler_util.cc) | 图预处理：建占位符、剪枝、控制流函数化、构造 XLA 参数 |
| [tensorflow/compiler/tf2xla/xla_compiler.h](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/tf2xla/xla_compiler.h) | `XlaCompiler`：对子图做符号执行，产出 `XlaComputation` |
| [tensorflow/compiler/tf2xla/mlir_tf2xla.cc](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/tf2xla/mlir_tf2xla.cc) | MLIR 路径实现：图 → MLIR → XLA 计算 |
| [tensorflow/compiler/mlir/tf2xla/mlir_bridge_rollout_policy.cc](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/tf2xla/mlir_bridge_rollout_policy.cc) | MLIR Bridge 的灰度启用策略 |
| [tensorflow/compiler/mlir/tf2xla/api/v1/compile_mlir_util.h](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/tf2xla/api/v1/compile_mlir_util.h) | `ConvertMLIRToXlaComputation`：把 MLIR 模块 lower 到 HLO |
| [tensorflow/compiler/mlir/stablehlo/transforms/tf_stablehlo_pass.cc](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/stablehlo/transforms/tf_stablehlo_pass.cc) | TF → MHLO → StableHLO 的 pass 流水线 |
| [tensorflow/compiler/mlir/stablehlo/stablehlo.cc](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/stablehlo/stablehlo.cc) | StableHLO 可移植 Python API 的 nanobind 桥 |

## 4. 核心概念与源码讲解

### 4.1 XLA 编译器：为什么要「编译」一张计算图

#### 4.1.1 概念说明

XLA（**Accelerated Linear Algebra**）是 TensorFlow 的线性代数编译器。它的输入不是 Python 代码，也不是 `GraphDef`，而是它自己定义的 IR——**HLO**。XLA 把 HLO 经过一系列优化（融合、布局选择、缓冲区分析）后，最终 lower 成目标设备（CPU/GPU/TPU）的机器码。

为什么需要这一层编译？关键在于 **kernel 融合（fusion）**。在普通执行模式下，TF 为每个 op 调用一个独立的 OpKernel（u4-l2）。考虑这样一个表达式：

\[ y = \text{ReLU}(W \cdot x + b) \]

它对应 4 个 op：`MatMul`、`Add`（或 `BiasAdd`）、`Relu`。在解释执行下，每个 op 都要：① 启动一次 kernel；② 把中间张量写回显存；③ 下一个 op 再从显存读回来。**显存带宽**是 GPU 上最昂贵的资源，这种「写回—重读」严重浪费带宽。

XLA 编译后，会把这几个 op 融合成**一个** fused kernel：数据从寄存器/缓存直接流到下一个计算，中间张量不必落盘到显存。这就是 XLA 的核心收益，可以用一个粗略的代价对比来理解：

\[ T_{\text{解释执行}} \approx \sum_i (\text{kernel 启动}_i + \text{显存读写}_i) \quad>\quad T_{\text{XLA}} \approx \text{kernel 启动}_{\text{单次}} + \text{寄存器内流转} \]

#### 4.1.2 核心流程

XLA 的位置可以用一张三层图概括：

```
   TF 计算图 (GraphDef / tf.function 子图)
                   │
                   │  ← tf2xla（本讲重点）：跨过「语义鸿沟」
                   ▼
   XLA HLO (XlaComputation)        ← XLA 的输入 IR
                   │
                   │  ← XLA 后端：融合 / 布局 / 缓冲区分析
                   ▼
   设备机器码 (CPU/GPU/TPU)
```

中间那道「跨过语义鸿沟」的桥，就是 `compiler/tf2xla` 的职责。注意：**tf2xla 本身不做性能优化**，它只是翻译官；真正的算子融合发生在 XLA 后端。仓库里的 XLA 源码现在已迁出到外部 `@xla` 仓库（见 u1-l2 的 vendoring 说明），TF 仓库内只保留「TF → XLA」这一侧的桥。

#### 4.1.3 源码精读

仓库通过文档列出了「哪些 TF op 能被 XLA 接受」。`tf2xla/g3doc/` 下有 CPU/GPU 两份清单，例如 CPU 侧列出 `Add` 支持的类型约束：

```
`Add` | `T={complex64,double,float,int32,int64}`
```

文件：[tensorflow/compiler/tf2xla/g3doc/cpu_supported_ops.md](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/tf2xla/g3doc/cpu_supported_ops.md)（每行一个 op，附支持的 `T` 类型集合）。这说明一个关键事实：**不是所有 TF op、也不是所有 dtype 都能进 XLA**——只有注册了对应 HLO 翻译规则的 op 才行。这份清单就是「编译可行性边界」的文档化。

#### 4.1.4 代码实践

1. **实践目标**：建立「op 是否可被 XLA 编译」的直觉。
2. **操作步骤**：打开上面两个清单文件（`cpu_supported_ops.md`、`gpu_supported_ops.md`），找一个你在常用模型里见过的 op（如 `Conv2D`、`MatMul`），对比它在 CPU 与 GPU 上支持的 `T` 类型是否一致。
3. **需要观察的现象**：注意有些 op 只在某一后端出现，有的 op 对 dtype 有严格限制。
4. **预期结果**：你会直观感受到「XLA 可编译 = op 在目标设备上有对应的 HLO 翻译规则」。

#### 4.1.5 小练习与答案

**练习 1**：XLA 的算子融合主要省下了哪一项开销？为什么不直接在 OpKernel 层做融合？

**参考答案**：主要省下**中间张量的显存读写带宽**与 kernel 启动开销。OpKernel 层是「逐 op 解释执行」的，每个 kernel 只看到自己的输入输出，无法跨 op 做全局优化；而 XLA 看到的是整段子图的 HLO，能在编译期做全局融合与缓冲区规划。

---

### 4.2 tf2xla 经典路径：GraphDef → XlaComputation

#### 4.2.1 概念说明

这是 tf2xla 最早、也最核心的翻译方式：输入一张 `GraphDef`，输出一个 `XlaComputation`（XLA 的 HLO 模块句柄）。

它面对一个核心难题：TF 的 op 是「命令式」的（执行 `Compute` 才产生数值），而 XLA 需要的是「声明式」的 HLO 数据结构。如何让既有 OpKernel 代码「吐出 HLO」而非「吐出数值」？

TF 的解法非常巧妙——**符号执行（symbolic execution）**：把整张图在一个「假的 XLA JIT 设备」上跑一遍，每个 op 的 kernel 在这个设备上不再是真计算，而是构建 HLO 表达式。跑完整张图，HLO 就被「录制」了下来。这与 u5-l1 自动微分的「grad_fn」、u3-l4 `tf.function` 的 tracing 是同一种「跑一遍、把副作用录制下来」的范式。

#### 4.2.2 核心流程

经典路径的入口是 `ConvertGraphDefToXla`，它把工作分成三大步：

```
ConvertGraphDefToXla(graph_def, config, client, computation)
   │
   ├─ ① ConvertVarHandlesToAotVarHandles   // 改写 VarHandleOp 节点
   ├─ ② InitGraph                          // 配置校验 + 建占位符 + 剪枝 + 控制流函数化
   └─ ③ ConvertGraphToXla                  // 符号执行 → CompileGraph → XlaComputation
```

其中「编译哪一部分图」由 `config` 决定：`feed` 是位置输入参数、`fetch` 是位置输出参数、`variable` 是既作输入又作输出的资源变量。

#### 4.2.3 源码精读

**入口声明**——两个转换函数并列，注释清楚说明 `config` 的 feed/fetch 如何对应「位置参数」：

[tf2xla.h:28-38](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/tf2xla/tf2xla.h#L28-L38) 注释 + `ConvertGraphDefToXla` 声明。这段说明 `config` 通过 feeds/fetches 指定「图的哪一部分」参与转换，每个 feed 对应生成计算的一个位置输入，每个 fetch 对应一个位置输出。

**Config 的 protobuf 定义**——三个 message 各司其职：

[tf2xla.proto:76-85](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/tf2xla/tf2xla.proto#L76-L85) `Config` 消息，包含 `feed`/`fetch`/`variable` 三个 repeated 字段，顺序就是生成计算的参数顺序。

[tf2xla.proto:39-49](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/tf2xla/tf2xla.proto#L39-L49) `Feed` 消息，用 `TensorId`（节点名 + 输出索引）定位被喂入的张量，可附可选的 `shape`/`type`（用于 op 未链接进二进制、无法推断类型的情况）。

**经典路径主体**——三步串联：

[tf2xla.cc:160-170](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/tf2xla/tf2xla.cc#L160-L170) `ConvertGraphDefToXla` 函数体，依次调用 `ConvertVarHandlesToAotVarHandles` → `InitGraph` → `ConvertGraphToXla`，三步任一失败即 `TF_RETURN_IF_ERROR` 提前返回。

**符号执行的核心**——这是全讲义最关键的一段代码：

[tf2xla.cc:55-65](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/tf2xla/tf2xla.cc#L55-L65) 注释明确写出「executing the graph symbolically, with each op building up the XLA HLO」（符号执行图，每个 op 构建出 XLA HLO）。随后第 61 行 `XlaOpRegistry::RegisterCompilationKernels()` 注册所有可编译 kernel，第 62-65 行把图中**每个节点**的设备名强制改成 `DEVICE_CPU_XLA_JIT`——这就是「把图搬到假设备上」的关键一步。

[tf2xla.cc:78-86](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/tf2xla/tf2xla.cc#L78-L86) 构造 `XlaCompiler` 并调用 `CompileGraph`，把符号执行的产物收进 `result.computation`。`XlaCompiler::Options` 里 `device_type` 就是上面那个 `DEVICE_CPU_XLA_JIT`。

**图的预处理**（`InitGraph`）——尤其注意控制流函数化：

[graph_compiler_util.cc:271-316](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/tf2xla/graph_compiler_util.cc#L271-L316) `InitGraph` 依次做：`ValidateConfig` → `AddPlaceholdersForFeeds`（为每个 feed 建占位符节点）→ `PruneGraphDefInto`（剪掉与 fetch 无关的节点）→ `ConvertGraphDefToGraph` → `RewriteAndPruneGraph`。

[graph_compiler_util.cc:308-312](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/tf2xla/graph_compiler_util.cc#L308-L312) **FunctionalizeControlFlow**——把 TF 的 `Switch`/`Merge`/`Enter`/`Exit` 等低层控制流 op，重写成 XLA 能理解的 `XlaIf`/`XlaWhile` 函数调用形式。这一步必不可少，因为 HLO 的控制流是「函数化的」（while 对应一个循环体函数），而 TF 图里是「数据流边」的。函数化后还会把新生成的 then/else 分支与循环体 `FunctionDef` 加回图。

**XLA 参数的构造**：

[graph_compiler_util.cc:238-269](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/tf2xla/graph_compiler_util.cc#L238-L269) `CreateXlaArgs` 从图的 `_Arg` 节点读出每个输入的类型、形状、名字，构造成 `kParameter` 参数；`PopulateXlaArgs` 则把 `config.variable()` 里的资源变量追加为 `kResource` 参数。这两类参数最终成为生成计算的入口签名。

> 关于 `Argument` 的三种 `kind`：`kConstant`（编译期常量，不进运行时参数）、`kParameter`（运行时输入）、`kResource`（资源变量，既是输入又是输出）。只有 `kParameter` 和已初始化的 `kResource` 会成为运行时参数。详见 [xla_compiler.h:82-90](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/tf2xla/xla_compiler.h#L82-L90) 的类注释。

#### 4.2.4 代码实践（本讲主实践）

1. **实践目标**：对照 `tf2xla.h` 与实现，完整说清「一张 TF 子图被翻译成 XLA 计算经过哪些步骤」。这正是本讲规格里要求的实践任务。
2. **操作步骤**：
   - 阅读 [tf2xla.h:35-38](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/tf2xla/tf2xla.h#L35-L38)，记住函数签名：`(GraphDef, Config, Client*, XlaComputation*)`。
   - 跟进 [tf2xla.cc:160-170](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/tf2xla/tf2xla.cc#L160-L170) 的三步调用。
   - 分别打开 `graph_compiler_util.cc` 的 `InitGraph`（271 行起）与 `tf2xla.cc` 的 `ConvertGraphToXla`（55 行起）。
3. **需要观察的现象**：注意「符号执行」与「控制流函数化」两处——前者把命令式 op 变成声明式 HLO，后者把图数据流的控制流变成函数式控制流。
4. **预期结果**：你能用自己的话写出至少 5 个有序步骤，例如：① 配置校验 → ② 建 feed 占位符并剪枝 → ③ 控制流函数化 → ④ 构造 XLA 参数 → ⑤ 在 XLA_JIT 假设备上符号执行，由 `XlaCompiler::CompileGraph` 录制出 `XlaComputation`。
5. **关于运行**：本实践为**源码阅读型**，无需运行命令；如需运行，需先配置好 Bazel 并构建 `//tensorflow/compiler/tf2xla:tf2xla` 目标，本地环境若未配置则结果「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ConvertGraphToXla` 要把所有节点的设备名强制改成 `/device:CPU_XLA_JIT`？

**参考答案**：这是符号执行的前提。`DEVICE_CPU_XLA_JIT` 是一个特殊的「编译用假设备」，其 OpKernel 的 `Compute` 不做真计算，而是向 `XlaBuilder` 追加 HLO 指令。只有把节点都指派到这个设备，运行时才会路由到「构建 HLO」的 kernel，而非普通数值 kernel。

**练习 2**：`FunctionalizeControlFlow` 解决了什么问题？如果不做这一步会怎样？

**参考答案**：TF 图用 `Switch`/`Merge`/`Enter`/`Exit` 等数据流边表达 if/while，但 HLO 的控制流是「函数化」的（`while` = 一个循环体函数 + 逐次调用）。函数化把它们重写为 `XlaIf`/`XlaWhile`。若不做，XLA 无法识别这种低层控制流，编译会失败。

---

### 4.3 tf2xla 的 MLIR 路径与 Bridge 灰度策略

#### 4.3.1 概念说明

经典路径（4.2）直接对 `Graph*` 做符号执行。随着 MLIR 成为主流（u7-l1），TF 又新增了一条 **MLIR 路径**：先把图导入成 MLIR 的 `tf` dialect 模块，再跑 MLIR pass 把它 lower 到 MHLO，最后插入 `XlaBuilder` 得到 `XlaComputation`。

两条路径的输入输出完全一样（都是 `GraphDef → XlaComputation`），区别只在中间表示：经典路径走「符号执行 + HLO」、MLIR 路径走「MLIR dialect + pass」。后者更模块化、更易扩展，但要替换已稳定运行多年的经典路径，需要一个**灰度上线**机制——这就是 MLIR Bridge 的 Rollout Policy。

#### 4.3.2 核心流程

MLIR 路径的入口是 `ConvertGraphDefToXlaViaMlir`：

```
ConvertGraphDefToXlaViaMlir(graph_def, config, ...)
   │
   ├─ AddPlaceholdersForFeeds + PruneGraphDefInto        // 与经典路径相同的前处理
   ├─ ConvertInputInfo / ConvertOutputInfo               // Config → GraphImportConfig
   ├─ tf2xla::v2::ConvertGraphToTfExecutor(...)          // GraphDef → MLIR 模块(tf_executor dialect)
   ├─ AddDevicesToOp(...)                                // 把 CPU 设备信息写到 op 上
   ├─ mlir::TF::RunBridgeWithStandardPipeline(...)       // 跑 MLIR Bridge 标准流水线
   └─ ConvertMLIRToXlaComputation(...)                   // MLIR → HLO → XlaComputation
```

最后一步 `ConvertMLIRToXlaComputation` 内部的 pass 结构（见头文件注释）是固定的三段：

```
TensorFlow passes → Legalization passes → MHLO passes
```

即「TF dialect 优化 → TF op 翻译成 MHLO op → MHLO 自身的优化与 lower」。

#### 4.3.3 源码精读

**MLIR 路径主体**：

[mlir_tf2xla.cc:120-198](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/tf2xla/mlir_tf2xla.cc#L120-L198) `ConvertGraphDefToXlaViaMlir` 完整函数。注意它开头先做 `AddPlaceholdersForFeeds` + `PruneGraphDefInto`，与经典路径的 `InitGraph` 前半段几乎一致——这说明「图前处理」是两条路径共享的公共底座。

[mlir_tf2xla.cc:174-177](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/tf2xla/mlir_tf2xla.cc#L174-L177) `tf2xla::v2::ConvertGraphToTfExecutor` 把 `GraphDef` 翻译成 MLIR 的 `tf_executor` dialect 模块。`tf_executor` 是 TF1 风格执行序的 dialect（u7-l1）。

[mlir_tf2xla.cc:188-189](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/tf2xla/mlir_tf2xla.cc#L188-L189) `RunBridgeWithStandardPipeline` 跑标准桥流水线，这一步正是受 Rollout Policy 控制的入口。

[mlir_tf2xla.cc:194-197](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/tf2xla/mlir_tf2xla.cc#L194-L197) 最后调 `ConvertMLIRToXlaComputation`，参数 `device_type="XLA_CPU_JIT"`、`prefer_tf2xla=false`，把 lower 后的 MHLO 插入 XlaBuilder 得到 `XlaComputation`。

**MLIR→XLA 的 pass 流水线结构**：

[compile_mlir_util.h:86-90](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/tf2xla/api/v1/compile_mlir_util.h#L86-L90) 注释写明流水线由「TensorFlow passes / Legalization passes / MHLO passes」三段组成。[compile_mlir_util.h:71-78](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/tf2xla/api/v1/compile_mlir_util.h#L71-L78) 是 `ConvertMLIRToXlaComputation` 的声明，`device_type` 决定走哪个 JIT 后端（`XLA_CPU_JIT`/`XLA_GPU_JIT`/`XLA_TPU_JIT`）。

**Bridge 的灰度策略**——核心是一个四态枚举：

[mlir_bridge_rollout_policy.h:28-41](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/tf2xla/mlir_bridge_rollout_policy.h#L28-L41) `MlirBridgeRolloutPolicy` 枚举：`kDisabledByUser`（用户显式禁用）、`kEnabledByUser`（用户显式启用，出错**不**回退）、`kDisabledAfterGraphAnalysis`（分析后认为不该跑）、`kEnabledAfterGraphAnalysis`（分析后认为该跑，出错可回退）。

[mlir_bridge_rollout_policy.cc:27-43](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/tf2xla/mlir_bridge_rollout_policy.cc#L27-L43) `GetMlirBridgeRolloutPolicy` 的实现：它 `switch` 在 `GetMlirBridgeRolloutState(config_proto)` 上——`ENABLED` 返回 `kEnabledByUser`、`DISABLED` 返回 `kDisabledByUser`、**default 分支返回 `kDisabledAfterGraphAnalysis`**，注释直白地写「For now, disable the bridge」。

> 这是一个非常关键的现状结论：**当前 HEAD 下，除非用户在 `ConfigProto` 里显式设 `MLIR_BRIDGE_ROLLOUT_ENABLED`，否则 MLIR Bridge 默认是关闭的**（走经典路径）。这也呼应了 u7-l1 所说的「环出再环回、默认禁用、主要面向 XLA/TPU」。

#### 4.3.4 代码实践

1. **实践目标**：理解 Bridge 默认关闭这一现状，并学会如何显式控制它。
2. **操作步骤**：
   - 阅读 [mlir_bridge_rollout_policy.cc:33-42](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/tf2xla/mlir_bridge_rollout_policy.cc#L33-L42) 的 `switch` 三分支。
   - 在 `tensorflow/core/protobuf/config.proto` 中搜索 `MLIR_BRIDGE_ROLLOUT` 枚举，确认有哪些取值。
3. **需要观察的现象**：注意 default 分支（用户不设置时）落在「禁用」。
4. **预期结果**：你能回答「想让一个 TF1 图走 MLIR Bridge，应在 `ConfigProto.Experimental` 里设什么字段」。答案：`mlir_bridge_rollout = MLIR_BRIDGE_ROLLOUT_ENABLED`。
5. 本实践为源码阅读型，命令执行结果「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`kEnabledByUser` 与 `kEnabledAfterGraphAnalysis` 在出错处理上的关键区别是什么？

**参考答案**：`kEnabledByUser` 表示用户**显式**要求启用，若 Bridge 出错，**不会**回退到经典路径（因为用户明确表态了）；`kEnabledAfterGraphAnalysis` 是分析后自动启用，出错时**可以**回退到经典路径以保住可用性。

**练习 2**：MLIR 路径与经典路径相比，最大的架构优势是什么？

**参考答案**：MLIR 路径用「dialect + pass」表达 lowering，每一步 pass 都是独立、可组合、可测试的；新增对某个 op 的支持只需加一条 legalize 规则，而不必修改符号执行的内核代码。这让编译流水线更易扩展和维护。

---

### 4.4 StableHLO：面向可移植性的中间表示

#### 4.4.1 概念说明

到目前为止，无论经典路径还是 MLIR 路径，产物都是 `XlaComputation`——**绑定到具体 XLA 版本**的 HLO。这意味着：用 XLA v2.x 编译的模型，换一个 XLA 版本或换一个非 XLA 运行时，可能无法运行。

**StableHLO** 要解决的就是这个「可移植性」问题。它是一个**稳定、可移植的算子级 IR**，定位在「各种 ML 框架（TF、JAX、PyTorch）」与「各种编译器/运行时（XLA、TFLite、第三方）」之间。你可以把模型编译成 StableHLO 这个「通用字节码」，然后任何兼容 StableHLO 的运行时都能加载执行它。

StableHLO 与几个相邻概念的关系：

| 概念 | 性质 | 稳定性 |
| --- | --- | --- |
| **HLO / XlaComputation** | XLA 内部 IR，protobuf 序列化 | 随 XLA 版本变，**不**保证稳定 |
| **MHLO**（MLIR HLO） | HLO 的 MLIR dialect 表达 | XLA 的「实验性」MLIR 前端，**不**保证稳定 |
| **StableHLO** | 面向可移植的 MLIR dialect，带版本号 | **保证**向后兼容，有可移植序列化格式 |
| **CHLO**（Client HLO） | 比 MHLO 更高层一点的「客户端」方言 | 先 lower 到 MHLO 再处理 |

#### 4.4.2 核心流程

TF 把 `tf` dialect 翻译成 StableHLO 的流水线注册在 `tf-stablehlo`，结构是：

```
tf dialect ops
      │  TFToMhloPass          (tf → MHLO，复用 tf2xla 的翻译规则)
      ▼
MHLO ops
      │  CanonicalizerPass     (化简)
      │  createHloLegalizeToStablehloPass  (MHLO → StableHLO)
      ▼
StableHLO ops
```

注意一个细节：`TFToMhloPass` 内部调用的 `PopulateLegalizeTfWithTf2XlaPatterns` **正是 tf2xla 的翻译规则**——也就是说，StableHLO 路径复用了 4.2/4.3 里那套「TF op → HLO」的翻译知识，只是最终落点从 `XlaComputation` 换成了 StableHLO dialect。这体现了翻译规则的复用。

得到 StableHLO 模块后，可以用**可移植序列化（portable serialization）**把它转成可长期保存、跨运行时加载的字节串。

#### 4.4.3 源码精读

**TF→StableHLO 流水线**：

[tf_stablehlo_pass.cc:155-165](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/stablehlo/transforms/tf_stablehlo_pass.cc#L155-L165) `PopulateLegalizeTFToStablehloPipeline`：依次添加 `TFToMhloPass` → `createCanonicalizerPass` → `createHloLegalizeToStablehloPass()`。这正是上面流程图的三步。

[tf_stablehlo_pass.cc:157-159](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/stablehlo/transforms/tf_stablehlo_pass.cc#L157-L159) 一条 `TODO` 注释坦诚说明现状：当前是「先产 MHLO 再转 StableHLO」，未来计划直接产出 StableHLO。这条注释本身就是理解「为什么路径要绕一下」的钥匙。

**TFToMhloPass 的核心**——用 `applyPartialConversion` 做模式重写：

[tf_stablehlo_pass.cc:98-141](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/stablehlo/transforms/tf_stablehlo_pass.cc#L98-L141) `runOnOperation`。第 102-110 行收集四类重写模式：`PopulateLegalizeTfPatterns`（TF→TF/MHLO）、`PopulateTFLoweringBeforeHLOPatterns`（TF 内部降级）、**`PopulateLegalizeTfWithTf2XlaPatterns("XLA_CPU_JIT", ...)`**（复用 tf2xla 规则）、`chlo→hlo`。第 112-119 行定义「合法目标」：CHLO 非法、MHLO/Arith/Func/Tensor/Shape 合法。第 138 行 `applyPartialConversion` 执行重写。

[tf_stablehlo_pass.cc:167-170](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/stablehlo/transforms/tf_stablehlo_pass.cc#L167-L170) 把这套流水线注册成名为 `tf-stablehlo` 的 MLIR pipeline，命令行工具可用 `--tf-stablehlo` 触发。

**可移植 Python API**——`stablehlo.cc` 是一个 nanobind 扩展模块：

[stablehlo.cc:22](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/stablehlo/stablehlo.cc#L22) `NB_MODULE(stablehlo_extension, m) { mlir::stablehlo::AddPortableApi(m); }`——它把上游 `@stablehlo` 仓库的「可移植 API」注册进 Python 模块。设计意图见 [stablehlo.py:15-23](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/stablehlo/stablehlo.py#L15-L23)：刻意只导出**不依赖 MLIR 类**的 Portable C++ API，以降低维护成本（因为 TF 频繁更新 LLVM 版本，全量导出 MLIR Python 绑定代价过高）。

**可移植序列化的用法**——测试就是最好的文档：

[stablehlo_test.py:33-36](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/stablehlo/stablehlo_test.py#L33-L36) 三行展示了核心 API：`get_current_version()` 取当前 StableHLO 版本作为 target；`serialize_portable_artifact_str(assembly, target)` 把一段 StableHLO 文本（第 23-31 行那段含 `stablehlo.compare` 的 MLIR 汇编）序列化成可移植字节串；`deserialize_portable_artifact_str(artifact)` 再反序列化回来。断言 `artifact == rountrip` 验证序列化是无损且确定的。

> 这里的 `stablehlo.compare`、`stablehlo.constant` 就是 StableHLO dialect 的 op。它们语义上等价于 HLO，但带版本保证。

**TFLite 侧的 StableHLO 入口**：除了 TF→StableHLO 流水线，仓库还有一条「TFLite 的 `XlaCallModule` op → StableHLO」的 pass（[legalize_tf_xla_call_module_to_stablehlo_pass.h:27-30](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/stablehlo/transforms/legalize_tf_xla_call_module_to_stablehlo_pass.h#L27-L30)），用于移动端加载已编译的 StableHLO 模块。这呼应了 u8 TFLite 单元——StableHLO 是连接「桌面端编译」与「移动端推理」的桥梁。

#### 4.4.4 代码实践

1. **实践目标**：亲手走一遍「StableHLO 文本 → 可移植字节串 → 还原」的往返，理解可移植序列化的确定性。
2. **操作步骤**：
   - 阅读 [stablehlo_test.py:20-36](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/stablehlo/stablehlo_test.py#L20-L36) 的 `smoketest`。
   - 理解 `assembly` 变量里那段 MLIR 汇编（一个 `stablehlo.compare` 做无符号 `>=` 比较）。
   - 若本地已构建 `//tensorflow/compiler/mlir/stablehlo:stablehlo` 目标，可运行该测试。
3. **需要观察的现象**：序列化与反序列化再序列化的结果完全相等（`artifact == rountrip`）。
4. **预期结果**：你体会到 StableHLO 的「可移植工件」是一种**确定、版本化**的字节串——这正是它能跨运行时复用的根基。
5. 命令执行依赖本地 Bazel 构建，未配置则「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：既然 MHLO 和 StableHLO 语义上很像，为什么不直接用 MHLO 做可移植格式，而要再定义 StableHLO？

**参考答案**：MHLO 是 XLA 的「实验性」MLIR 前端，其 op 集合随 XLA 研发频繁变动，**不保证向后兼容**。StableHLO 则明确承诺稳定性与版本化，专门为「长期保存、跨运行时可移植」设计。用 MHLO 做存储格式会因版本演进而失效。

**练习 2**：`tf-stablehlo` 流水线为什么中间要先产 MHLO，而不是直接从 TF dialect 到 StableHLO？

**参考答案**：因为「TF op → HLO 语义」的翻译规则已在 MHLO 层大量积累（`PopulateLegalizeTfWithTf2XlaPatterns` 等复用了 tf2xla 的工作）。复用这套 MHLO 翻译规则、再用一步 `HloLegalizeToStablehlo` 转换，比重写一套直达 StableHLO 的规则成本更低。`tf_stablehlo_pass.cc` 里的 TODO 也表明未来计划改为直接产出。

---

## 5. 综合实践

把本讲三条线索串起来：**追踪一段 TF 子图从 `GraphDef` 到最终变成「可跨运行时携带的 StableHLO 工件」的全过程，并标注每一步发生在哪个文件、用了什么机制。**

请完成以下任务：

1. **画一张三栏对照图**，分别画出经典路径、MLIR 路径、StableHLO 路径的流水线节点。要求：
   - 经典路径必须包含：`InitGraph`（含 `FunctionalizeControlFlow`）→ `ConvertGraphToXla`（符号执行 / `DEVICE_CPU_XLA_JIT`）→ `XlaCompiler::CompileGraph` → `XlaComputation`。
   - MLIR 路径必须包含：`ConvertGraphToTfExecutor` → `RunBridgeWithStandardPipeline`（受 Rollout Policy 控制）→ `ConvertMLIRToXlaComputation`（TF passes / Legalization / MHLO）→ `XlaComputation`。
   - StableHLO 路径必须包含：`TFToMhloPass`（复用 tf2xla 规则）→ Canonicalizer → `HloLegalizeToStablehloPass` → StableHLO（可序列化为 portable artifact）。

2. **回答一个综合问题**：如果目标分别是 ① 在当前进程的 GPU 上做 JIT 加速；② 把模型离线编译成一个可被任意 StableHLO 运行时加载的文件，你会分别选哪条路径？为什么？
   - 参考思路：① 选经典或 MLIR 路径产 `XlaComputation` 交给 XLA 后端即时编译执行；② 选 StableHLO 路径，因为只有 StableHLO 提供稳定可移植的序列化格式，`XlaComputation` 绑定具体 XLA 版本、不可移植。

3. **（进阶）** 阅读 [tf2xla.proto:62-73](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/tf2xla/tf2xla.proto#L62-L73) 的 `Variable` 消息，解释 `readonly` 字段配合 [tf2xla.cc:111-128](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/tf2xla/tf2xla.cc#L111-L128) 的校验逻辑，是为了防止用户犯什么配置错误。

## 6. 本讲小结

- **XLA 是编译器，tf2xla 是翻译官**：XLA 把 HLO lower 成设备码并做融合优化；tf2xla 负责「跨过语义鸿沟」，把 TF 图翻译成 XLA 能吃的 `XlaComputation`，自身不做性能优化。
- **经典路径靠符号执行**：`ConvertGraphDefToXla` 把整张图在 `DEVICE_CPU_XLA_JIT` 这个假设备上跑一遍，每个 op 的 kernel 录制 HLO 而非算数值；前处理含「占位、剪枝、控制流函数化」三步。
- **MLIR 路径是同输入输出的现代化替代**：`ConvertGraphDefToXlaViaMlir` 走 GraphDef→MLIR→MHLO→`XlaComputation`，结构是「TF passes / Legalization / MHLO」三段，但当前默认**关闭**，需显式 `MLIR_BRIDGE_ROLLOUT_ENABLED`。
- **Rollout Policy 四态控制灰度**：`kEnabledByUser`（出错不回退）与 `kEnabledAfterGraphAnalysis`（出错可回退）的区别是理解 Bridge 安全网的关键。
- **StableHLO 解决可移植性**：它是有版本保证、可稳定序列化的算子 IR，定位在「框架—运行时」之间；`tf-stablehlo` 流水线复用 tf2xla 翻译规则先到 MHLO 再转 StableHLO。
- **可移植工件**通过 `serialize_portable_artifact_str` / `deserialize_portable_artifact_str` 做确定、无损的往返，是模型长期保存与跨运行时分发的基石。

## 7. 下一步学习建议

- **下一讲 u7-l3「JIT 自动聚类」**：本讲讲的是「给定一张子图，怎么翻译成 XLA」。但在运行时，是**谁**决定哪些 op 凑成一个子图交给 XLA？答案就是 auto-clustering。建议接着阅读 [tensorflow/compiler/jit/xla_cluster_util.h](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/jit/xla_cluster_util.h)，理解聚类判定与不支持的 op 如何回退。
- **AOT 编译**：本讲的 `ConvertGraphDefToXla` 也是 AOT 编译（`tfcompile`）的入口，产物可离线生成 `.o` 与头文件。建议浏览 `tensorflow/compiler/aot/` 与 `tf2xla/g3doc/`。
- **延伸阅读源码**：`tf2xla/transforms/legalize_tf.cc`（庞大的 TF→HLO 模式库）与上游 `@stablehlo` 仓库的 `StablehloApi.cpp`（可移植 API 的真正实现）。
- **承接 u8**：移动端的 `XlaCallModule` → StableHLO pass（`legalize_tf_xla_call_module_to_stablehlo_pass`）会把本讲的 StableHLO 与 TFLite 串起来，学完 u8 后可回头再看这条路径。
