# XLA / StableHLO 与 tf2xla

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 XLA（Accelerated Linear Algebra）是什么、它做 JIT/AOT 编译到底带来了什么收益（尤其是算子融合与减少内存读写）。
- 读懂 `tensorflow/compiler/tf2xla/tf2xla.h` 这张「翻译入口」名片，分清经典符号执行路径与基于 MLIR 的路径两条编译前端。
- 理解 `tf2xla::Config` 中的 feed / fetch 是如何把一张 TensorFlow 子图裁剪、定位成「要翻译的那一部分」。
- 掌握「符号执行」这一把 TF 子图逐 op 翻译成 XLA HLO 计算的核心机制。
- 认识 StableHLO 作为「可移植 IR / 便携工件」的定位，理解它与 XLA 内部 HLO 的区别与联系。

本讲承接 u7-l1（MLIR 与 TF dialect）。在 u7-l1 里你已经知道 MLIR 用「dialect + pass」来表达和变换中间表示；本讲会把这些 IR 工具用到「编译加速」这条主线上，看看一张 TF 子图最终如何变成设备上跑得最快的代码。

## 2. 前置知识

### 2.1 什么是 XLA、为什么要「编译」一张图

你在 u3-l2 见过 `DirectSession::Run`：每个 op 被调度到自己的 OpKernel 上，逐个执行，op 之间靠张量（内存缓冲）传递数据。这种「一个 op 一个 kernel」的执行方式有一个明显的代价：

- 每两个相邻 op 之间，结果都要写回显存/内存，再被下一个 op 读出来。**内存读写**往往比计算本身还慢。
- 每个 op 各自调用一次 cuDNN/BLAS 之类的库，优化发生在 op 粒度，无法跨 op 做优化。

**XLA**（Accelerated Linear Algebra）就是 TensorFlow 的编译器，它把「一张可以编译的子图」整体看成一个计算，编译成一段针对具体设备（GPU/TPU/CPU）的原生代码。它的核心收益是：

1. **算子融合（kernel fusion）**：把多个 op（比如 `matmul → bias_add → relu`）融合进一个 kernel，中间结果只留在寄存器/片上缓存，**不落盘到显存**。
2. **更少的临时缓冲**：编译器可以规划内存复用。
3. **常量折叠、layout 优化、流水线编排**等全局优化。

简言之：逐 op 执行是「解释执行」，XLA 是「编译执行」。要让 XLA 工作，第一关就是——**把 TF 子图翻译成 XLA 能消化的中间表示**。这一关的代码，就住在 `tensorflow/compiler/tf2xla/`。

### 2.2 HLO、XlaComputation 与 StableHLO 三个名词

为避免后面混淆，先把三个名词摆好：

| 名词 | 是什么 | 在哪 |
|------|--------|------|
| **HLO**（High-Level Optimizer） | XLA **内部的**高级 IR，描述 add/matmul/convolution 等数组运算 | XLA 编译流水线内部（vendored 在 `third_party/xla`） |
| **XlaComputation** | 一个 HLO 计算的 C++ 句柄/容器（内部是 protobuf `HloModule`） | `xla/hlo/builder/xla_computation.h` |
| **StableHLO** | 从 HLO/MHLO 抽出来的**稳定、可移植**的 MLIR dialect，作为「便携工件」跨 XLA 版本/框架保存 | `tensorflow/compiler/mlir/stablehlo/`（dialect 本体在 `@stablehlo`） |

一句话区分：HLO 是 XLA 内部用的 IR，**跟着编译器版本走、不保证向后兼容**；StableHLO 是给「保存模型、跨版本/跨编译器消费」用的**稳定序列化格式**。本讲后半段会专门讲它。

### 2.3 你需要复习的几个前置概念

- **GraphDef / Node / Edge**（u3-l1）：TF 计算图在序列化时是扁平的 `NodeDef` 列表，tf2xla 的输入就是一张 `GraphDef`。
- **Op / OpKernel / Compute**（u4-l1、u4-l2）：每个 op 有「说明书」OpDef 和「干活工人」OpKernel。
- **MLIR dialect / pass**（u7-l1）：用方言（dialect）表示 IR，用 pass 做 lowering（逐层下沉变换）。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 |
|-------------|------|
| `tensorflow/compiler/tf2xla/tf2xla.h` | **翻译入口名片**。声明两个把 `GraphDef` 变成 `XlaComputation` 的函数。 |
| `tensorflow/compiler/tf2xla/tf2xla.cc` | 经典路径的实现：符号执行把图逐 op 翻译成 HLO。 |
| `tensorflow/compiler/tf2xla/tf2xla.proto` | `Config` / `Feed` / `Fetch` / `Variable` 协议定义，指明「要翻译子图的哪一段」。 |
| `tensorflow/compiler/tf2xla/xla_compiler.h` / `.cc` | `XlaCompiler` 类——真正做符号执行、驱动 `XlaBuilder` 构造 HLO 的核心。 |
| `tensorflow/compiler/tf2xla/mlir_tf2xla.cc` | 基于 MLIR 的翻译路径实现（`ConvertGraphDefToXlaViaMlir`）。 |
| `tensorflow/compiler/mlir/tf2xla/mlir_bridge_rollout_policy.cc` / `.h` | 决定「走经典前端还是 MLIR 前端」的策略开关。 |
| `tensorflow/compiler/mlir/stablehlo/` | StableHLO 的 Python 可移植 API 与 TF→StableHLO 变换 pass。 |
| `tensorflow/compiler/mlir/tensorflow_to_stablehlo/README.md` | 把 SavedModel 整体翻译成 StableHLO 的工具与 API 说明。 |

## 4. 核心概念与源码讲解

### 4.1 XLA 的编译收益与 tf2xla 的职责

#### 4.1.1 概念说明

tf2xla 名字直译就是「TensorFlow → XLA」。它解决的核心问题是：**TF 用图（GraphDef）描述计算，XLA 用 HLO 描述计算，两者是两套语言，需要一个翻译器。**

tf2xla 的输出是一个 `XlaComputation`——它是一个自包含的 HLO 计算，之后可以被 XLA 编译成具体设备代码，也可以被序列化保存。需要注意的是，tf2xla 只负责「翻译」，**不负责**后面的 HLO 优化、代码生成、执行——那些是 XLA 编译流水线自己的事。tf2xla 是 TF 世界通往 XLA 世界的「翻译关」。

#### 4.1.2 核心流程

一个 TF 子图被翻译成 XLA 计算，宏观上经历：

1. **圈定子图**：用 `Config` 里的 feed（输入张量）和 fetch（输出张量）说明「要翻译哪一段」。
2. **图预处理**：替换 VarHandle、为 feed 加占位符、剪枝掉与 fetch 无关的节点。
3. **翻译**：把每个 op 翻译成对应的 HLO 指令，组装成一个 `XlaComputation`。

这一节先看「翻译入口」长什么样。

#### 4.1.3 源码精读

整张名片只有两个对外函数，先看头文件：

> `ConvertGraphDefToXla` 的注释把翻译契约讲得很清楚：`config` 用 feeds/fetches 指定要翻译的子图，**每个 feed 是生成计算的一个位置参数输入，每个 fetch 是一个位置参数输出**。
[compiler/tf2xla/tf2xla.h:28-38](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/tf2xla/tf2xla.h#L28-L38) —— 经典路径入口 `ConvertGraphDefToXla` 的声明与契约。

> 第二个函数 `ConvertGraphDefToXlaViaMlir` 是「基于 MLIR 的翻译路径」，名字里的 `ViaMlir` 表明它走的是 MLIR 前端，并能携带调试信息。这就是本讲后面要对比的两条路径的另一条。
[compiler/tf2xla/tf2xla.h:40-49](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/tf2xla/tf2xla.h#L40-L49) —— MLIR 路径入口 `ConvertGraphDefToXlaViaMlir`。

经典路径的顶层编排只有三步，非常干净：

> `ConvertGraphDefToXla` 依次做三件事：先把 VarHandle 改写成 AOT 专用的变体、再用 `InitGraph` 把 GraphDef 构造成运行时 `Graph`、最后交给 `ConvertGraphToXla` 真正翻译。
[compiler/tf2xla/tf2xla.cc:160-170](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/tf2xla/tf2xla.cc#L160-L170) —— 经典路径的三步编排。

#### 4.1.4 代码实践

**目标**：理解「一个 feed = 一个输入参数，一个 fetch = 一个输出参数」这个契约是怎么用 protobuf 表达的。

1. 打开 `tensorflow/compiler/tf2xla/tf2xla.proto`。
2. 找到 `Config` 消息（含 `feed`/`fetch`/`variable`）、`Feed`（含 `TensorId id` + `shape` + 可选 `type`）、`Fetch`、`TensorId`（`node_name` + `output_index`）。
3. 回到 `tf2xla.h` 的注释，确认：`config` 里 `feed` 列表的顺序 = 生成计算的输入参数顺序，`fetch` 列表的顺序 = 输出参数顺序。

**预期现象**：你会看到 `Config` 不过是「输入张量列表 + 输出张量列表 + 变量列表」，它本身不含任何计算逻辑，纯粹是用来**圈定子图边界**的描述。

#### 4.1.5 小练习与答案

**练习 1**：tf2xla 的输出是 `XlaComputation`。请判断：tf2xla 自己会做「把 HLO 编译成 GPU 机器码」这件事吗？

> **参考答案**：不会。tf2xla 只负责「翻译 TF 子图 → HLO 计算」，产出 `XlaComputation`。后面的 HLO 优化、lowering、代码生成、执行都是 XLA 编译流水线（`xla::Client::Compile` 等）的工作。tf2xla 是「翻译关」，不是「代码生成关」。

**练习 2**：为什么 `ConvertGraphDefToXla` 的第一个参数是按值传入的 `GraphDef graph_def` 而不是 `const GraphDef&`？

> **参考答案**：因为后续会就地修改这份 GraphDef（例如 `ConvertVarHandlesToAotVarHandles` 会改写节点、`InitGraph` 阶段还会插入 placeholder）。按值传入意味着调用方的原图不被改动，翻译器拿的是一份可以随意涂改的拷贝。

### 4.2 经典路径：符号执行逐 op 构造 HLO

#### 4.2.1 概念说明

这是 tf2xla 最核心、也最巧妙的设计。理解它需要先破除一个直觉：**翻译不是「逐节点查表替换」**，而是「**符号执行（symbolic execution）**」。

所谓符号执行，就是把这张图在「一台专门为编译而存在的假设备（JIT device）」上「跑一遍」。但这台假设备上的 op kernel 不真的计算数值——它每执行一个 op，就往一个叫 `XlaBuilder` 的对象里追加一条对应的 HLO 指令。等图「跑完」，`XlaBuilder` 里就攒出了一张完整的 HLO 计算。

为什么这么设计？因为 TF 的 op/kernel 体系（u4-l1、u4-l2）已经很完善，每个 op 都有 kernel。XLA 复用了这套机制：它为大量 op 注册了「XLA 版」的 kernel（叫 compilation kernel），这些 kernel 的 `Compute` 方法不输出数值，而是调用 `XlaBuilder` 造 HLO。于是「跑图」这个现成机制就被直接拿来当翻译器了。

类注释里说得很直白：

> `XlaCompiler` 用一台 JIT 设备对图做符号执行，把 op 转成 XLA 计算；它要求图通过 `_Arg` 节点接收输入、通过 `_Retval` 节点返回输出。
[compiler/tf2xla/xla_compiler.h:72-91](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/tf2xla/xla_compiler.h#L72-L91) —— `XlaCompiler` 的职责说明（符号执行 + JIT 设备 + `_Arg`/`_Retval` 约定）。

#### 4.2.2 核心流程

经典路径 `ConvertGraphToXla` 的内部流程：

```
ConvertGraphToXla(graph, config, client, computation):
  1. XlaOpRegistry::RegisterCompilationKernels()   # 注册「XLA 版」kernel（幂等）
  2. 把所有节点的设备改成 DEVICE_CPU_XLA_JIT         # 全部放到那台「假设备」上
  3. CreateXlaArgs / PopulateXlaArgs                # 据 config 构造每个 _Arg 的 XlaCompiler::Argument
  4. 构造 XlaCompiler（device_type=CPU_XLA_JIT）
  5. compiler.CompileGraph(... std::move(graph) ...) # 符号执行，产出 CompilationResult
  6. 把 result.computation 移交给输出参数 computation
  7. 校验：不应有「常量化的输出」、变量 readonly 标志要和实际是否被修改一致
```

其中第 5 步 `CompileGraph` 才是符号执行的发动机。

#### 4.2.3 源码精读

先看 `ConvertGraphToXla` 本体，注意那行关键注释「**executing the graph symbolically, with each op building up the XLA HLO**」：

> 这是整个 tf2xla 设计的精华注释：不是运行图求值，而是「符号地」执行图，**每个 op 负责把对应的 XLA HLO 攒出来**。
[compiler/tf2xla/tf2xla.cc:55-66](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/tf2xla/tf2xla.cc#L55-L66) —— `ConvertGraphToXla`：注册编译 kernel、把节点指派到 `DEVICE_CPU_XLA_JIT`。

> 这一段把 `XlaCompiler` 配好（client、device_type、函数库），调 `CompileGraph` 完成符号执行，并把产出的 `computation` 取出。
[compiler/tf2xla/tf2xla.cc:71-86](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/tf2xla/tf2xla.cc#L71-L86) —— 构造 `XlaCompiler` 并调用 `CompileGraph`。

`CompileGraph` 的开头点明了它要干什么：

> 日志里的 "Executing graph symbolically to populate XlaBuilder" 就是符号执行的写照；随后 `std::make_unique<xla::XlaBuilder>(name)` 造出那个会一边「执行」一边被填充的 builder。
[compiler/tf2xla/xla_compiler.cc:1521-1556](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/tf2xla/xla_compiler.cc#L1521-L1556) —— `XlaCompiler::CompileGraph`：符号执行启动，创建 `XlaBuilder`。

那么「XLA 版 kernel」从哪来？看校验函数里这一行：

> `XlaOpRegistry::RegisterCompilationKernels()` 把所有标注为可编译的 op 的 XLA kernel 登记进全局注册表（幂等，可重复调用）。没有这一步，符号执行时遇到 op 会找不到对应的「造 HLO」的 kernel。
[compiler/tf2xla/xla_compiler.cc:1370-1376](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/tf2xla/xla_compiler.cc#L1370-L1376) —— `ValidateGraph` 里登记编译 kernel。

校验函数还会把「图里出现了 XLA 不支持的 op」翻译成清晰错误：

> 当某个节点没有对应的编译 kernel，就报 "Detected unsupported operations..."——这正是「遇到不能编译的 op 就报错」的来源，承接 u7-l3 的 auto-clustering 回退话题。
[compiler/tf2xla/xla_compiler.cc:1378-1395](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/tf2xla/xla_compiler.cc#L1378-L1395) —— 不支持 op 的错误信息构造。

#### 4.2.4 代码实践

**目标**：用一个最小例子看清「GraphDef + Config → XlaComputation」的完整入参形态。

1. 打开 `tensorflow/compiler/tf2xla/tf2xla_test.cc`，阅读 `SumGraph()`（构造 `x`、`y` 两个 Placeholder + 一个 `Add` 节点）和 `SumConfig()`（声明两个 feed：`x`、`y`，一个 fetch：`sum`）。
2. 阅读 `TEST(ConvertGraphDefToXla, Sum)`：它拿到一个 `xla::LocalClient`，调用 `ConvertGraphDefToXla(graph_def, config, client, &computation)`，就得到了一个 `XlaComputation`。
3. 对照本节流程图，把这个调用映射回三步编排：VarHandle 重写（本例无 VarHandle）→ `InitGraph` → `ConvertGraphToXla`。

**预期现象**：你会确认——**要翻译一张子图，只需提供「图本身（GraphDef）」+「输入输出边界（Config）」+「一个 XLA client」**，剩下交给 `ConvertGraphDefToXla`。这就是 tf2xla 作为「翻译关」的最小契约。

#### 4.2.5 小练习与答案

**练习 1**：符号执行和「真正运行图求值」有什么本质区别？

> **参考答案**：真正运行时，op 的 `Compute` 读取真实数值张量、计算出结果张量；符号执行时，op（的 XLA 编译 kernel）读取的是代表输入的 `XlaOp` 句柄，调 `XlaBuilder` 追加一条 HLO 指令，产出的是代表输出的 `XlaOp` 句柄。全程没有真实数值流动，流动的是「HLO 指令的句柄」。

**练习 2**：为什么 `ConvertGraphToXla` 要把所有节点的设备强制改成 `DEVICE_CPU_XLA_JIT`？

> **参考答案**：因为符号执行需要走「XLA 编译 kernel」的派发路径，而这些 kernel 只在名为 `CPU_XLA_JIT`（及对应的 GPU/TPU 变体）的设备类型上注册。把节点指派到该设备，才能让 op 派发到「造 HLO」的 kernel 而非普通计算 kernel。

### 4.3 两条编译前端：经典符号执行 vs MLIR 路径

#### 4.3.1 概念说明

`tf2xla.h` 给了**两个**入口，对应两条把 TF 子图翻译成 `XlaComputation` 的「前端」：

- **经典路径** `ConvertGraphDefToXla`：4.2 节讲的符号执行，复用 op/kernel 体系，逐 op 造 HLO。这是 TF 较早期的、为 AOT（`tfcompile`）服务的翻译器。
- **MLIR 路径** `ConvertGraphDefToXlaViaMlir`：先把图导入成 MLIR 模块，跑一遍 TF→XLA 的 MLIR bridge（一系列 pass），再 lower 成 HLO。这是较新的、和 u7-l1 的 MLIR 工具链统一的路径。

为什么要有两条？因为 TF 的图优化与编译正在从「手写 C++ 图变换 + 符号执行」逐步迁移到「MLIR dialect + pass」这套更统一、更易扩展的基础设施。两条路并存，由一个「上线策略（rollout policy）」决定在给定图上走哪条。

#### 4.3.2 核心流程

MLIR 路径 `ConvertGraphDefToXlaViaMlir` 的流程：

```
ConvertGraphDefToXlaViaMlir(graph_def, config, ...):
  1. AddPlaceholdersForFeeds    # 为每个 feed 建占位符节点
  2. PruneGraphDefInto          # 剪掉与 fetch 无关的节点
  3. ConvertInputInfo/ConvertOutputInfo  # 把 config 的 feed/fetch 翻成 GraphImportConfig
  4. ConvertGraphDefToGraph     # 得到运行时 Graph
  5. tf2xla::v2::ConvertGraphToTfExecutor  # 导入成 MLIR 模块（tf_executor dialect）
  6. RunBridgeWithStandardPipeline        # 跑 TF→XLA 的 MLIR bridge pass 流水线
  7. ConvertMLIRToXlaComputation          # 把 MLIR 模块 lower 成 XlaComputation（HLO）
```

第 6 步那条 bridge 流水线，正是 u7-l1 讲的 MLIR pass 体系的具体应用：它做控制流合法化、资源算子分解、资源读写替换成函数输入输出、合法化到 HLO、再做规范化。这些步骤在头文件注释里列得很清楚：

> 这段注释列出了 MLIR 路径把 tf dialect 算子 lower 到 XLA HLO 的标准步骤：合法化控制流 → 分解复合资源算子 → 用函数输入输出替换资源读写、消除资源变量 → 合法化到 HLO → 规范化。
[compiler/mlir/tf2xla/api/v1/compile_mlir_util.h:41-54](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/mlir/tf2xla/api/v1/compile_mlir_util.h#L41-L54) —— `ConvertMLIRToXlaComputation` 的 lowering 步骤说明。

注意该函数已被标注 `ABSL_DEPRECATED`，推荐改用 `v2/legalize_tf.h::LegalizeMlirToHlo`——这也印证了「MLIR 路径在持续演进」。

#### 4.3.3 源码精读

MLIR 路径的主体实现：

> 前半段做图层面准备（占位符、剪枝、把 config 转成导入规格）；中段 `ConvertGraphToTfExecutor` 把运行时图导入成 MLIR 模块；`RunBridgeWithStandardPipeline` 跑 TF bridge 流水线；末尾 `ConvertMLIRToXlaComputation` 落到 `XLA_CPU_JIT` 设备、产出 `XlaComputation`。
[compiler/tf2xla/mlir_tf2xla.cc:120-198](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/tf2xla/mlir_tf2xla.cc#L120-L198) —— `ConvertGraphDefToXlaViaMlir` 全流程。

那么「在一张图上到底走经典前端还是 MLIR 前端」由谁决定？看上线策略：

> `GetMlirBridgeRolloutPolicy` 依据用户在 `ConfigProto` 里的显式开关决定：用户明确 enable/disable 就照办；默认情况下（未显式设置）返回 `kDisabledAfterGraphAnalysis`——即默认**不启用**这条 MLIR bridge。
[compiler/mlir/tf2xla/mlir_bridge_rollout_policy.cc:27-43](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/mlir/tf2xla/mlir_bridge_rollout_policy.cc#L27-L43) —— 上线策略的判定。

策略的几种取值定义在头文件里，含义一目了然：

> 四种策略：用户显式禁用 / 用户显式启用（出错也不回退）/ 经分析后默认禁用 / 经分析后默认启用（出错尽量回退）。这是典型的「灰度上线」策略枚举。
[compiler/mlir/tf2xla/mlir_bridge_rollout_policy.h:28-41](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/mlir/tf2xla/mlir_bridge_rollout_policy.h#L28-L41) —— `MlirBridgeRolloutPolicy` 枚举。

> 小贴士：这里讨论的「MLIR bridge」是 TF 图层面的旧桥接开关。两条**翻译前端**（`ConvertGraphDefToXla` vs `ConvertGraphDefToXlaViaMlir`）本身都可以独立被调用；现代 TF2 默认的 XLA 触发入口是 `@tf.function(jit_compile=True)`，它在内部经由 `tf2xla` 这套机制把聚类子图交给 XLA。

#### 4.3.4 代码实践

**目标**：对比两条前端的「翻译风格」差异。

1. 打开 `tf2xla.cc` 的 `ConvertGraphToXla`（经典路径）与 `mlir_tf2xla.cc` 的 `ConvertGraphDefToXlaViaMlir`（MLIR 路径）并排阅读。
2. 找出二者各自依赖的「核心引擎」：经典路径依赖 `XlaCompiler::CompileGraph`（符号执行 + `XlaBuilder`）；MLIR 路径依赖 `ConvertGraphToTfExecutor` + `RunBridgeWithStandardPipeline` + `ConvertMLIRToXlaComputation`（MLIR pass 流水线）。
3. 回答：哪条路径「复用了 op/kernel 派发」？哪条路径「用 pass 流水线做变换」？

**预期现象**：你会清楚地看到两种翻译哲学——经典路径把 TF 现成的「跑图」机制当翻译器；MLIR 路径则把图导入成 IR、用 pass 逐层 lower。两者殊途同归，都产出 `XlaComputation`。

#### 4.3.5 小练习与答案

**练习 1**：`ConvertMLIRToXlaComputation` 的注释说输入模块「应只包含 tf dialect 算子」。为什么它不接受 `tf_executor` dialect 算子？

> **参考答案**：`tf_executor` 是 TF 图执行器语义的 MLIR 表达（含 graph/island 等结构），而 lowering 到 HLO 需要的是「线性函数」形态。注释说明：必须先经过规范化把 `tf_executor` 消化掉，模块里只剩纯 tf dialect 算子时才能做 HLO 合法化。所以它会拒绝含未消化 `tf_executor` 的输入。

**练习 2**：默认的 rollout 策略返回 `kDisabledAfterGraphAnalysis`。如果你想让一张图强制走 MLIR bridge，该怎么做？

> **参考答案**：在 `ConfigProto::Experimental` 里把 `MLIR_BRIDGE_ROLLOUT` 设为 `MLIR_BRIDGE_ROLLOUT_ENABLED`。`GetMlirBridgeRolloutPolicy` 会据此返回 `kEnabledByUser`，强制启用且出错不回退。

### 4.4 StableHLO：作为可移植 IR 的稳定序列化层

#### 4.4.1 概念说明

到这里你可能有一个疑问：既然 XLA 内部已经有 HLO，`XlaComputation` 就是 HLO 计算的容器，为什么还要再搞一个 StableHLO？

关键矛盾是**稳定性**。XLA 的 HLO 是「编译器内部 IR」，它的定义会随 XLA 版本演进而改变——今天存的 HLO 文本，下个版本的 XLA 可能就不认了。这对「**保存模型、跨版本/跨编译器加载**」是致命的：你训练完一个模型，想序列化它的计算图留作日后部署，却不希望它和某个具体的 XLA 版本绑死。

**StableHLO** 就是为这个目标设计的：它是从 MHLO/HLO 演化出来的、**稳定的、可移植的** MLIR dialect，配套一套**便携工件（portable artifact）**格式和稳定版本号。你可以把它理解成：「给计算图一个像 JSON 一样稳定、可长期保存、可被不同编译器消费的中间表示」。StableHLO 的目标消费者不只是 XLA，还包括 PJRT、IREE 等其它编译/运行时后端。

所以 StableHLO 在编译链里的定位是：**与 tf2xla→HLO 并行的一条「面向可移植序列化」的路径**。tf2xla 把 TF 子图翻译成 XLA 内部 HLO（为了编译执行）；而 StableHLO 把 TF/SavedModel 翻译成稳定的可移植 IR（为了保存与跨后端部署）。两者最终都能落到 HLO、都能被 XLA 编译，但出发点不同。

#### 4.4.2 核心流程

TF 计算变成 StableHLO，仓库里提供两条线：

```
线 A（底层 MLIR pass 流水线，compiler/mlir/stablehlo/transforms/）：
   TF ops  --[TFToMhloPass]-->  MHLO  --[createHloLegalizeToStablehloPass]-->  StableHLO

线 B（面向用户的整体翻译工具，compiler/mlir/tensorflow_to_stablehlo/）：
   SavedModel / TF MLIR module  --[tf-to-stablehlo-translate]-->  StableHLO 字节码
```

注意线 A 的关键事实：目前 TF→StableHLO 是**两段式**（先到 MHLO，再到 StableHLO），代码里有一个明确的 TODO 要把它改成直通 StableHLO。

#### 4.4.3 源码精读

先看 StableHLO 暴露给 Python 的「便携工件」API 是多么薄：

> `stablehlo.cc` 用 nanobind 把 `@stablehlo` 里的可移植 C++ API（`AddPortableApi`）注册成 Python 模块——刻意只导出签名不依赖 MLIR 类的「便携」API，避免把整个 MLIR Python 绑定拖进 TF（维护成本高）。
[compiler/mlir/stablehlo/stablehlo.cc:16-24](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/mlir/stablehlo/stablehlo.cc#L16-L24) —— StableHLO 可移植 API 的 Python 扩展注册。

> Python 侧 `stablehlo.py` 只是从扩展模块 `*` 导入，文档串说明它「只导出不依赖 MLIR 类的 StableHLO 可移植 C++ API」。
[compiler/mlir/stablehlo/stablehlo.py:15-27](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/mlir/stablehlo/stablehlo.py#L15-L27) —— StableHLO Python 入口。

这套「便携 API」长什么样？看冒烟测试：

> `get_api_version` / `get_current_version` 是稳定版本号；`serialize_portable_artifact_str` / `deserialize_portable_artifact_str` 是「把 StableHLO 文本序列化成便携字节码、再反序列化回来」的核心对，且往返（roundtrip）应当字节相等——这正是「稳定可移植工件」承诺的体现。
[compiler/mlir/stablehlo/stablehlo_test.py:20-36](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/mlir/stablehlo/stablehlo_test.py#L20-L36) —— StableHLO 便携 API 的使用样例（序列化/反序列化往返）。

再看底层 TF→StableHLO 的 pass 流水线，重点看那个 TODO：

> `PopulateLegalizeTFToStablehloPipeline` 目前是「TF →（TFToMhloPass）→ MHLO →（createHloLegalizeToStablehloPass）→ StableHLO」两段式；注释里的 TODO 表明将来要直接产 StableHLO，但当前仍复用 MHLO 这一层。
[compiler/mlir/stablehlo/transforms/tf_stablehlo_pass.cc:155-165](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/mlir/stablehlo/transforms/tf_stablehlo_pass.cc#L155-L165) —— TF→StableHLO 流水线装配与 TODO。

`TFToMhloPass` 的转换里有个有意思的细节——它**复用了 tf2xla 的模式**：

> `PopulateLegalizeTfWithTf2XlaPatterns("XLA_CPU_JIT", ...)` 表明：把 TF op 合法化到 HLO/MHLO 时，直接复用了 tf2xla 那套逐 op 的翻译模式。也就是说，4.2 节那套「翻译能力」在 MLIR 路径里被重新组织成 pass 的形式继续发光发热。
[compiler/mlir/stablehlo/transforms/tf_stablehlo_pass.cc:98-110](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/mlir/stablehlo/transforms/tf_stablehlo_pass.cc#L98-L110) —— `TFToMhloPass` 复用 tf2xla 合法化模式。

最后是面向用户的整体翻译工具。它把整个 SavedModel 变成 StableHLO，提供命令行和 Python 两种入口：

> 工具 `tf-to-stablehlo-translate` 把 SavedModel 或 TF MLIR 模块整体翻译成 StableHLO，保留模型结构与签名；Python 端有 `savedmodel_to_stablehlo` 和 `tensorflow_module_to_stablehlo` 两个函数。
[compiler/mlir/tensorflow_to_stablehlo/README.md:1-18](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/mlir/tensorflow_to_stablehlo/README.md#L1-L18) —— `tf-to-stablehlo-translate` 工具与用法。

> Python API 示例：`savedmodel_to_stablehlo(input_path=..., exported_model_signatures=[...], input_arg_shapes_str=...)` 返回 StableHLO 字节码。
[compiler/mlir/tensorflow_to_stablehlo/README.md:60-77](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/compiler/mlir/tensorflow_to_stablehlo/README.md#L60-L77) —— `savedmodel_to_stablehlo` 的调用示例。

#### 4.4.4 代码实践

**目标**：动手体验「StableHLO 便携工件的序列化/反序列化往返」，直观感受「稳定可移植」。

1. 阅读 `stablehlo_test.py` 的 `smoketest`：它构造一小段 StableHLO 文本（一个 `compare` 运算），调 `get_current_version()` 拿到目标版本，`serialize_portable_artifact_str(assembly, target)` 得到字节码工件，再 `deserialize_portable_artifact_str` 反序列化、再次序列化，断言两次字节码相等。
2. （**待本地验证**）如果你已用 Bazel 构建了 `//tensorflow/compiler/mlir/stablehlo:stablehlo_test` 或安装了含 `tensorflow.compiler.mlir.stablehlo` 的 TF，可运行该测试，观察「往返字节相等」的断言通过。
3. 解释现象：序列化产物是一个**带版本号的稳定字节码**，可在日后用兼容版本的 StableHLO 重新加载——这正是「跨版本可移植」的承诺。

**预期结果**：`artifact == rountrip` 断言成立；若版本不兼容，反序列化会报错。**确切输出待本地验证。**

#### 4.4.5 小练习与答案

**练习 1**：用一句话区分 HLO 与 StableHLO 的定位。

> **参考答案**：HLO 是 XLA 编译器**内部**的 IR（随版本演进、不保证兼容）；StableHLO 是面向**保存与跨后端部署**的稳定、可移植 MLIR dialect（带版本号、承诺兼容）。

**练习 2**：`tf-stablehlo` 流水线为什么目前要先经过 MHLO 再到 StableHLO？

> **参考答案**：历史原因——TF→HLO 的合法化模式（包括复用的 tf2xla 模式）成熟于 MHLO 时代，StableHLO 是从 MHLO 演化来的。当前复用现成的 TF→MHLO 合法化、再用 `createHloLegalizeToStablehloPass` 把 MHLO 转成 StableHLO，成本最低；直通 StableHLO 是代码 TODO 标记的后续目标。

## 5. 综合实践

把本讲四条主线串起来，完成下面这个「跟踪一次 TF→XLA 翻译」的任务。

**背景**：你想向同事解释「一个 `Add` 子图是怎么变成 XLA 计算的，以及 StableHLO 在整个生态里干嘛」。

**任务步骤**：

1. **构造最小子图**：参考 `tf2xla_test.cc` 的 `SumGraph()` / `SumConfig()`，用文字写出一个 `Add` 子图的 GraphDef（两个 Placeholder 输入 `x`、`y`，一个 `Add` 输出 `sum`）和对应的 `Config`（两个 feed、一个 fetch）。
2. **跟踪经典翻译链**：按顺序列出 `ConvertGraphDefToXla` → `ConvertVarHandlesToAotVarHandles` → `InitGraph` → `ConvertGraphToXla` → `XlaCompiler::CompileGraph`，并用一句话说明每一步做了什么。指出「符号执行」发生在哪一步、「往 `XlaBuilder` 里填 HLO」由谁驱动。
3. **指出 MLIR 路径的差异**：列出 `ConvertGraphDefToXlaViaMlir` 的关键阶段（占位符 → 剪枝 → 导入 MLIR → bridge 流水线 → lower 到 HLO），并说明它与经典路径在「翻译引擎」上的根本不同。
4. **定位 StableHLO 的角色**：回答——如果目标是「**编译执行**」，走哪条路（产 `XlaComputation` 交给 XLA）？如果目标是「**长期保存、跨版本/跨编译器部署**」，又走哪条路（产 StableHLO 便携工件）？为什么后者不能直接用 XLA 内部 HLO？

**预期产物**：一段带永久链接的「翻译链路说明」，能够回答本讲开头提出的实践任务——「一个 TF 子图被翻译成 XLA 计算需要经过哪些步骤，StableHLO 在其中扮演什么角色」。

> **可选的运行时观察（待本地验证）**：在已安装 TensorFlow 的环境里，用 `@tf.function(jit_compile=True)` 包裹一个加法函数并调用，再用 `tf.config.run_functions_eagerly(False)` 确保 XLA 生效。这会在内部触发本讲这套翻译机制。具体是否打印 XLA 日志、如何 dump 出 HLO 文本取决于 TF 版本与环境变量（如 `TF_XLA_FLAGS=--tf_xla_auto_jit=2`、`XLA_FLAGS=--xla_dump_to=...`），**确切现象待本地验证**。

## 6. 本讲小结

- XLA 是 TF 的 JIT/AOT 编译器，核心收益是**算子融合**（减少跨 op 的显存读写）和全局优化；tf2xla 是 TF 图通往 XLA 的「翻译关」，只负责产出 `XlaComputation`，不做代码生成。
- `tf2xla.h` 给出两个入口：经典路径 `ConvertGraphDefToXla` 与 MLIR 路径 `ConvertGraphDefToXlaViaMlir`，两者都把「GraphDef + Config」翻译成 `XlaComputation`。
- `Config` 用 feed/fetch 圈定要翻译的子图：**feed = 输入参数，fetch = 输出参数**，顺序即位置参数顺序。
- 经典路径的灵魂是**符号执行**：把图在假设备 `CPU_XLA_JIT` 上「跑一遍」，每个 op 的 XLA 编译 kernel 不算数值，而是往 `XlaBuilder` 里追加一条 HLO 指令。
- MLIR 路径则把图导入成 MLIR、跑 TF bridge pass 流水线再 lower 到 HLO；`MlirBridgeRolloutPolicy` 决定一张图走哪条前端，默认不启用该 bridge。
- **StableHLO** 是从 HLO/MHLO 抽出的**稳定、可移植** MLIR dialect，面向「保存模型、跨版本/跨编译器部署」；它与 tf2xla→HLO 是并行的两条线，`tf-stablehlo` 流水线目前走 TF→MHLO→StableHLO 两段式。

## 7. 下一步学习建议

- **u7-l3（JIT 自动聚类）**：本讲的 `ConvertGraphDefToXla`/`CompileGraph` 假设「子图已经圈定」。下一步该问：一张大图里，**哪些 op 会被自动聚成一个 XLA 可编译簇**？哪些 op 不支持时会怎样回退？这正是 `compiler/jit/` 的 auto-clustering 主题。当你读到 `xla_compiler.cc` 里报「Detected unsupported operations」时，自然就接上了 u7-l3 的回退逻辑。
- **深入 XLA 后端**：本讲止步于「产出 `XlaComputation`」。继续阅读 `third_party/xla` 里 HLO 优化、lowering 到 LLO、以及 CPU/GPU/TPU 后端代码生成，能补齐「编译执行」的后半段。
- **StableHLO 生态**：阅读 `tensorflow/compiler/mlir/tensorflow_to_stablehlo/` 的工具实现，以及 `@stablehlo` 上游的兼容性保证文档，理解便携工件的版本与兼容性契约。
- **对比阅读**：把本讲的 MLIR bridge（`RunBridgeWithStandardPipeline`）与 u7-l1 的 TF dialect pass 体系对照，你会更清楚「编译器前端」是如何在 MLIR 上重新组织的。
