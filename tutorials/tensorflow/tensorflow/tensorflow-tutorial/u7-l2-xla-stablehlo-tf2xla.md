# u7-l2 XLA / StableHLO 与 tf2xla

> 本讲承接 u7-l1（MLIR 与 TF dialect 编译流水线）。u7-l1 讲清了「TF 计算图如何变成 MLIR 模块、由一组 pass 做 lowering」；本讲顺着这条流水线往下走，回答一个问题：**这一堆被 lowering 出来的算子，最终被谁编译成能在 CPU/GPU/TPU 上高速执行的设备代码？** 答案是 XLA 编译器，而把 TensorFlow 世界接入 XLA 世界的「翻译官」，就是本讲的主角 `tf2xla` 与 `StableHLO`。

---

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清 **XLA 是什么、为什么要用它**（融合、内存、少 kernel launch 带来的 JIT 收益），并能区分「执行」与「编译」两个阶段。
2. 看懂 `tf2xla.h` 暴露的两个入口 `ConvertGraphDefToXla` 与 `ConvertGraphDefToXlaViaMlir`，并说明一个 TF 子图被翻译成 XLA 计算要经过哪些步骤。
3. 区分「经典 tf2xla（XlaCompiler 符号执行）」与「MLIR 路线（TF dialect → MHLO）」两条翻译路线，以及它们如何通过 `enable_op_fallback` 互相兜底。
4. 理解 **StableHLO 作为可移植 IR** 的定位：它与 MHLO 的关系、它如何被序列化为「可移植 artifact」、以及它在跨框架（JAX / PyTorch / TF）互通中扮演的角色。

---

## 2. 前置知识

本讲需要你已经掌握以下概念（部分来自前置讲义）：

- **计算图与 GraphDef**（u3-l1）：TF 用有向无环图表示计算，`GraphDef` 是其序列化形式（一张 `NodeDef` 列表）。本讲要做的，就是把这张 `GraphDef` 翻译成另一种 IR。
- **Op 与 OpKernel**（u4-l1、u4-l2）：TF 的算子既有「声明（OpDef）」也有「实现（OpKernel）」。XLA 编译走的是第三条路——它不调用普通 OpKernel，而是为每个 op 提供一份「编译期 kernel」，在编译阶段把 op 翻成 XLA 的 HLO 指令。
- **MLIR dialect 与 pass**（u7-l1）：MLIR 把计算表示成多种「方言（dialect）」的嵌套 IR，再通过一串 pass 逐步 lowering。本讲会反复出现 TF dialect、tf_executor dialect、MHLO dialect、StableHLO dialect 这几个名字。

再用通俗语言补三个本讲特有的概念：

| 术语 | 一句话解释 |
| --- | --- |
| **XLA**（Accelerated Linear Algebra） | Google 的线性代数编译器。给它一段计算，它能把其中多个算子融合成少量大 kernel，并生成针对 CPU/GPU/TPU 的设备代码。 |
| **HLO**（High-Level Optimizer IR） | XLA 自己的中间表示。一段 HLO `Add → Mul → Add` 可以被 XLA 融合成一条融合 kernel。 |
| **JIT vs AOT** | JIT（即时编译）= 运行时按需编译（TF2 默认走这条）；AOT（提前编译）= 离线把图编译成静态库（tfcompile 场景，本讲的 `tf2xla.h` 两个函数主要服务于此）。 |
| **MHLO / StableHLO** | MHLO 是「在开发的、随 XLA 演进的」HLO 方言；StableHLO 是从 MHLO 抽出的「稳定、可移植、可序列化」版本，用于跨框架长期互通。 |

> 一个关键直觉：**StableHLO 与 HLO 几乎是同一套算子，但 StableHLO 承诺向后兼容**，因此它可以被保存到磁盘、跨版本/跨框架加载；MHLO 则不保证。所以仓库里常见「先 legal 到 MHLO，再 `HloLegalizeToStablehlo` 转成 StableHLO」的做法。

---

## 3. 本讲源码地图

本讲涉及的关键文件全部在 `tensorflow/compiler/` 下：

| 文件 | 作用 |
| --- | --- |
| `tensorflow/compiler/tf2xla/tf2xla.h` | tf2xla 的对外头文件，声明两个翻译入口。 |
| `tensorflow/compiler/tf2xla/tf2xla.cc` | 经典路线实现：`ConvertGraphDefToXla`，靠 `XlaCompiler` 符号执行。 |
| `tensorflow/compiler/tf2xla/tf2xla.proto` | 定义 `Config`（feed / fetch / variable），描述「要翻译图的哪一段」。 |
| `tensorflow/compiler/tf2xla/xla_compiler.h` | `XlaCompiler` 类文档：符号执行、`_Arg`/`_Retval`、参数种类。 |
| `tensorflow/compiler/tf2xla/mlir_tf2xla.cc` | MLIR 路线实现：`ConvertGraphDefToXlaViaMlir`。 |
| `tensorflow/compiler/mlir/tf2xla/api/v1/compile_mlir_util.h` | MLIR 路线的 lowering pipeline 文档（`ConvertMLIRToXlaComputation`）。 |
| `tensorflow/compiler/mlir/tf2xla/mlir_bridge_rollout_policy.{h,cc}` | 决定「是否启用 MLIR 桥」的策略枚举与实现。 |
| `tensorflow/compiler/mlir/stablehlo/transforms/tf_stablehlo_pass.cc` | TF → MHLO → StableHLO 的导出 pipeline 核心。 |
| `tensorflow/compiler/mlir/stablehlo/stablehlo.{cc,py}` | StableHLO 可移植 Python API 的 nanobind 绑定壳。 |
| `tensorflow/compiler/tf2xla/g3doc/cpu_supported_ops.md` | XLA 在 CPU 后端支持的算子清单（实践用）。 |

---

## 4. 核心概念与源码讲解

本讲覆盖两个最小模块：**`compiler.tf2xla`**（翻译官）与 **`compiler.mlir.stablehlo`**（可移植 IR）。下面分四节展开。

### 4.1 XLA 与 tf2xla：为什么要编译，谁负责翻译

#### 4.1.1 概念说明

先回答「为什么」：直接用 TF 原生 OpKernel 逐个执行一段 `y = relu(matmul(x, w) + b)`，存在三层浪费——

1. **kernel launch 开销**：每个 op 都要单独启动一次（尤其 GPU 上 launch 成本可观）。
2. **中间结果落地显存**：`matmul` 的输出要写回显存，`add` 再读出来，`relu` 又读一次。
3. **无法跨算子优化**：每个 op 各自为政，无法把 `add+relu` 融合成一条指令。

XLA 解决这三点：它把整段子图当作一个整体编译，做**算子融合（fusion）**、**缓冲区复用**、**常量折叠**等优化，最终生成一两条大 kernel。这就是 XLA 的 JIT 收益。

再回答「谁负责翻译」：TF 和 XLA 是两套独立的计算表示。TF 用 `GraphDef`（算子名如 `MatMul`、`AddV2`），XLA 用 HLO（指令如 `dot`、`add`）。中间需要一个翻译层把 TF 算子逐一映射成 HLO 指令——这个翻译层就叫 **tf2xla**。它对外只暴露两个核心函数，都写在 `tf2xla.h` 里。

#### 4.1.2 核心流程

无论走哪条路线，tf2xla 的输入输出是固定的：

```
输入: GraphDef（整张图） + tf2xla::Config（说明要翻译哪一段：feed=输入, fetch=输出, variable=状态变量）
       │
       ▼
   [翻译]（两条路线之一）
       │
       ▼
输出: xla::XlaComputation（一段可被 XLA 编译/执行的 HLO 计算）
```

两条翻译路线：

- **经典路线** `ConvertGraphDefToXla`：直接在 TF 图上做**符号执行**，每遇到一个 op 就调用其「编译期 kernel」往 `XlaBuilder` 里追加一条 HLO。实现在 `tf2xla.cc`。
- **MLIR 路线** `ConvertGraphDefToXlaViaMlir`：先把图导入成 MLIR 模块（TF dialect），跑 TF 标准 pipeline，再 legal 到 MHLO，最后塞进 `XlaBuilder`。实现在 `mlir_tf2xla.cc`。

`tf2xla::Config` 这个「描述要翻译哪一段」的协议定义在 proto 里。

#### 4.1.3 源码精读

先看对外头文件，它把两条路线并列声明，注释写得非常清楚：

[tensorflow/compiler/tf2xla/tf2xla.h:28-38](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/tf2xla/tf2xla.h#L28-L38) —— 头文件注释说明 `ConvertGraphDefToXla` 的契约：`config` 通过 feed/fetch 指定要翻译的图片段，每个 feed 是一个位置输入参数，每个 fetch 是一个位置输出参数；计算在给定 `client` 上下文中构建，随后可用该 client 编译或执行。

[tensorflow/compiler/tf2xla/tf2xla.h:46-49](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/tf2xla/tf2xla.h#L46-L49) —— 声明 `ConvertGraphDefToXlaViaMlir`：与上面函数同名职责，但「使用 MLIR」并额外处理调试信息（`debug_info_filename` 等）。

再看驱动整个翻译的协议 `tf2xla::Config`：

[tensorflow/compiler/tf2xla/tf2xla.proto:76-85](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/tf2xla/tf2xla.proto#L76-L85) —— `Config` 只有三个字段：`feed`（输入参数列表，顺序即参数顺序）、`fetch`（输出参数列表）、`variable`（带状态的资源变量，既是输入也是输出）。这正是「描述要翻译哪一段」的全部信息。

[tensorflow/compiler/tf2xla/tf2xla.proto:32-49](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/tf2xla/tf2xla.proto#L32-L49) —— `TensorId` 用 `(node_name, output_index)` 定位图里某个张量；`Feed` 在此基础上加 `shape`/`type`（当被喂入的 op 不在当前二进制里、类型无法推断时必须显式给出）和一个可选代码生成名字。

> 注意 `Config` 的设计体现了「**翻译一个有明确边界的子图**」这一核心抽象：feed 是子图的入口（对应 `_Arg`），fetch 是出口（对应 `_Retval`），variable 是可读写的状态。这与 u5-3 讲过的「描述与数据分离」是同一种思路。

#### 4.1.4 代码实践

**实践目标**：用源码阅读的方式，确认 tf2xla 对外暴露的两个函数签名与 `Config` 协议，画出「输入—翻译—输出」的边界。

**操作步骤**：

1. 打开 `tensorflow/compiler/tf2xla/tf2xla.h`，抄下两个函数的完整签名（注意返回类型都是 `absl::Status`，符合 TF 的错误处理惯例）。
2. 打开 `tensorflow/compiler/tf2xla/tf2xla.proto`，数清 `Config` 有几个 `repeated` 字段，并回答「一个 feed 在生成计算里对应什么？」
3. 在仓库里搜索这两个函数的调用点（见 4.1.5 练习）。

**需要观察的现象**：两个函数签名几乎一样，唯一区别是 MLIR 版多了两个调试信息参数；这说明它们是「同一件事的两种实现」，调用方可以二选一。

**预期结果**：你能不看讲义，用三句话向别人解释「tf2xla 把什么翻译成什么，靠什么指定翻译范围」。

#### 4.1.5 小练习与答案

**Q1**：`tf2xla::Config` 里的 `feed` / `fetch` / `variable` 分别对应生成计算里的什么？
**答**：`feed` → 位置输入参数（计算入口）；`fetch` → 位置输出参数（计算出口）；`variable` → 既是输入又是输出的资源变量（带状态，可被计算修改）。

**Q2**：`ConvertGraphDefToXla` 与 `ConvertGraphDefToXlaViaMlir` 签名上的关键区别是什么？这暗示了什么？
**答**：后者多出 `debug_info_filename` 与 `debug_info_path_begin_marker` 两个参数。这暗示 MLIR 路线能携带源码级调试信息（因为 MLIR 天然支持 location/调试元数据），而经典路线没有这层能力。

---

### 4.2 经典路线：XlaCompiler 的符号执行

> 本节属于最小模块 **`compiler.tf2xla`**。

#### 4.2.1 概念说明

经典路线的核心思想是**符号执行（symbolic execution）**：不真正算出数值，而是「假装执行」一遍图——每经过一个 op，就调用它的「编译期 kernel」，往 `XlaBuilder` 里追加一条 HLO 指令。走完整张图，`XlaBuilder` 里就攒出了一段完整的 `XlaComputation`。

为了让「符号执行」能跑起来，TF 专门造了一个虚构的「XLA JIT 设备」（`DEVICE_CPU_XLA_JIT` 等）。图里所有节点都被指派到这个设备上，于是运行时实际触发的不是普通 OpKernel，而是每个 op 注册的 **XLA 编译期 kernel**（`XlaOpKernel`）。这就是 u4-2 讲的「一个 Op 一对多 Kernel」的延伸：同一个 `MatMul`，可以有 CPU kernel、GPU kernel，还有 XLA 编译 kernel。

`XlaCompiler` 类是这条路线的总指挥。

#### 4.2.2 核心流程

```
ConvertGraphDefToXla(graph_def, config, client, &computation)
   │
   ├─ ConvertVarHandlesToAotVarHandles   # 把 VarHandleOp 改写为 AOT 专用形态
   ├─ InitGraph(config)                  # 用 feed/fetch 裁出子图，建 Graph 对象
   └─ ConvertGraphToXla(graph, config, client, &computation)
         │
         ├─ XlaOpRegistry::RegisterCompilationKernels()   # 注册所有 XLA 编译 kernel
         ├─ 把所有节点指派到 DEVICE_CPU_XLA_JIT
         ├─ CreateXlaArgs(...)            # 由 _Arg 节点构造 XlaCompiler::Argument
         ├─ 构造 XlaCompiler(client, device_type=XLA_JIT, flib_def, ...)
         └─ compiler.CompileGraph(...)    # ★ 符号执行：逐 op 调 XlaOpKernel，输出 HLO
                                          #   结果写入 result.computation
```

`CompileGraph` 内部的符号执行有个隐含前提（见源码注释）：**所有输入形状必须已知**。因为符号执行要为每个 op 推导 HLO 形状，未知形状会让推导卡住。这也是为什么 XLA 通常在「形状确定后」才被触发。

#### 4.2.3 源码精读

[tensorflow/compiler/tf2xla/tf2xla.cc:160-170](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/tf2xla/tf2xla.cc#L160-L170) —— `ConvertGraphDefToXla` 的函数体只有三步：先把 `VarHandleOp` 改写成 AOT 专用 op，再 `InitGraph` 用 config 裁出子图，最后交给 `ConvertGraphToXla`。注意它按值传入 `GraphDef`（可安全改写）。

[tensorflow/compiler/tf2xla/tf2xla.cc:55-86](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/tf2xla/tf2xla.cc#L55-L86) —— 匿名函数 `ConvertGraphToXla` 的开头注释点明整条路线的精髓：「**by executing the graph symbolically, with each op building up the XLA HLO**」。随后：注册编译 kernel → 把每个节点 `set_assigned_device_name("/device:CPU_XLA_JIT")` → 构造 `xla_args` → 配置 `XlaCompiler::Options`（`device_type`、`flib_def`、`allow_cpu_custom_calls`）→ `compiler.CompileGraph(...)`。

[tensorflow/compiler/tf2xla/tf2xla.cc:88-110](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/tf2xla/tf2xla.cc#L88-L110) —— 符号执行后的一个安全校验：若某个 fetch（输出）在编译后变成了常量，就报 `UnimplementedError`——因为这会导致用户请求的输出「被丢掉」，多半是 config 配错。

[tensorflow/compiler/tf2xla/xla_compiler.h:72-87](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/tf2xla/xla_compiler.h#L72-L87) —— `XlaCompiler` 类的文档注释，浓缩了整条经典路线：它「**symbolic execution of the graph starting from specific input shapes, using a JIT device to convert operators into XLA computations**」；通常由 `XlaLaunch` 算子在「所有输入参数形状已知后」触发；编译的图用 `_Arg` 接收输入、`_Retval` 产生输出；每个 `_Arg` 对应一个 `Argument`，分为 `kConstant`（编译期常量）/`kParameter`（运行时参数）/`kResource`（资源变量）三种。

> 注意 `kConstant` 与 `kParameter` 的区别：前者是编译期已知的常量（会被烤进生成的 HLO），后者是运行时才传入的参数（成为 HLO 计算的入口参数）。只有 `kParameter` 和「已初始化的 `kResource`」才会成为生成计算的运行时参数。

#### 4.2.4 代码实践

**实践目标**：通过阅读源码，确认「符号执行」这一抽象如何落地为代码，并理解 `Argument` 三种 kind。

**操作步骤**：

1. 读 `tensorflow/compiler/tf2xla/xla_compiler.h` 第 72–123 行的类注释，重点看「输出排列顺序」那段（`_Retval values` 在前，`kResource` 更新值在后）。
2. 在 `tensorflow/core/ops/` 或 `tensorflow/compiler/tf2xla/kernels/` 目录里找一个 XLA 编译 kernel（如搜索类名含 `XlaOpKernel` 的文件），对比它与普通 OpKernel 的区别——它不产生数值，而是调用 `XlaBuilder`。
3. 思考：为什么经典路线要求「所有输入形状已知」？

**需要观察的现象**：XLA 编译 kernel 的 `Compute`（或 `Compile`）方法里出现的是 `ctx->builder()`、`xla::Add`、`xla::Dot` 之类的 HLO 构造调用，而不是真正的数值运算。

**预期结果**：你能用一句话概括「符号执行 = 用 XLA 编译 kernel 替代普通 kernel，边走图边往 builder 里加 HLO」。

#### 4.2.5 小练习与答案

**Q1**：`XlaCompiler::Argument` 有哪几种 kind？哪些会成为生成计算的运行时参数？
**答**：`kConstant` / `kParameter` / `kResource`。只有 `kParameter` 与「已初始化的 `kResource`」成为运行时参数；`kConstant` 被烤进 HLO，不是参数。

**Q2**：为什么符号执行要求输入形状已知？
**答**：因为每经过一个 op，编译期 kernel 要为它推导 HLO 输出形状；若输入形状未知，输出形状推导会失败（XLA 的形状推导是静态的、需要具体维度的），整条符号执行链就会卡住。

---

### 4.3 MLIR 路线：TF dialect → MHLO → XlaComputation

> 本节仍属于最小模块 **`compiler.tf2xla`**，是其「现代实现」。

#### 4.3.1 概念说明

经典路线有一个长期痛点：**符号执行把「图结构变换」和「翻译」耦合在 C++ 的 kernel 代码里**，难以扩展、难以复用、难以跨后端。随着 MLIR 成熟，TF 把翻译迁移到 MLIR 上：图先变成 MLIR 模块（TF dialect），所有变换都用「声明式 pass」表达（这正是 u7-l1 讲的内容），最后再 legal 到 MHLO 并喂给 `XlaBuilder`。

这条路线由 `ConvertGraphDefToXlaViaMlir` 实现，内部又复用了一个更大的「MLIR 桥（MLIR Bridge）」基础设施。是否启用 MLIR 桥，由一个 rollout 策略决定。

#### 4.3.2 核心流程

```
ConvertGraphDefToXlaViaMlir(graph_def, config, &computation, debug_info...)
   │
   ├─ AddPlaceholdersForFeeds(config)        # 给每个 feed 造一个 placeholder 节点
   ├─ PruneGraphDefInto(config)              # 按 feed/fetch 裁出子图
   ├─ ConvertInputInfo / ConvertOutputInfo   # 把 tf2xla::Config 翻译成 GraphImportConfig
   │
   ├─ ConvertGraphToTfExecutor(...)          # ★ GraphDef → MLIR 模块（TF + tf_executor dialect）
   ├─ AddDevicesToOp(module, device_set)     # 把 CPU 设备信息标到 IR 上
   ├─ RunBridgeWithStandardPipeline(module)  # ★ 跑 TF 标准 MLIR pipeline（控制流/legalize 等）
   └─ ConvertMLIRToXlaComputation(module, "XLA_CPU_JIT", &computation, ...)
                                            # ★ TF dialect → MHLO → 塞进 XlaBuilder
```

`ConvertMLIRToXlaComputation` 内部的 lowering 步骤在其头文件注释里写得很清楚（见 4.3.3）。

#### 4.3.3 源码精读

[tensorflow/compiler/tf2xla/mlir_tf2xla.cc:124-148](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/tf2xla/mlir_tf2xla.cc#L124-L148) —— MLIR 路线前半段：`AddPlaceholdersForFeeds` 既为剪枝做准备，也是为了规避 importer 不允许未知 op 的历史问题（注释里的 b/149029125）；随后 `ConvertInputInfo`/`ConvertOutputInfo` 把 proto 里的 feed/fetch 翻译成 `GraphImportConfig` 的输入输出数组信息。

[tensorflow/compiler/tf2xla/mlir_tf2xla.cc:166-189](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/tf2xla/mlir_tf2xla.cc#L166-L189) —— 这是整条 MLIR 路线的「三连击」：`ConvertGraphToTfExecutor` 把图导入成 MLIR 模块（此时还是 `tf_executor` dialect 包装的图执行器形态）；`RunBridgeWithStandardPipeline` 跑 TF 标准 pipeline 做控制流消解、资源 op 拆分等（即 u7-l1 的 MLIR 桥）；注释明确：若上一步没能把图降到「单图单 island」形态，下一步就会报错。

[tensorflow/compiler/tf2xla/mlir_tf2xla.cc:194-197](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/tf2xla/mlir_tf2xla.cc#L194-L197) —— 最后一步 `ConvertMLIRToXlaComputation(..., "XLA_CPU_JIT", &computation, /*use_tuple_args=*/false, /*prefer_tf2xla=*/false, /*return_tuple=*/true)`。注意第 5 个参数 `prefer_tf2xla=false`——它映射到 `enable_op_fallback`，是两条路线的**互锁开关**。

[tensorflow/compiler/mlir/tf2xla/api/v1/compile_mlir_util.h:41-54](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/mlir/tf2xla/api/v1/compile_mlir_util.h#L41-L54) —— `ConvertMLIRToXlaComputation` 的文档注释列出了 TF op 被降到 XLA HLO 的五步：① legal 控制流；② 拆解复合资源 op（只剩 read/write）；③ 用函数输入输出替换资源读写、消除资源变量；④ legal 到 XLA HLO；⑤ 规范化。这套步骤正是 MLIR 桥「 legalization」的全部内容。

[tensorflow/compiler/mlir/tf2xla/api/v1/compile_mlir_util.h:60-78](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/mlir/tf2xla/api/v1/compile_mlir_util.h#L60-L78) —— 参数 `enable_op_fallback` 的注释「when this is true, **prefer tf2xla fallback kernels over MLIR native kernels** for legalization to HLO」印证了上面的判断：当 MLIR 还没为某个 op 写好原生 legalization pattern 时，可以回退用 4.2 节那条经典 tf2xla 的编译 kernel 来兜底。**两条路线不是互斥替换，而是「MLIR 为主、tf2xla 兜底」的组合**。同时注意这组函数都标了 `ABSL_DEPRECATED`，指向更新的 `v2/legalize_tf.h::LegalizeMlirToHlo`——说明这条 pipeline 仍在持续重构演进。

再看「是否启用 MLIR 桥」的决策点：

[tensorflow/compiler/mlir/tf2xla/mlir_bridge_rollout_policy.h:28-41](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/mlir/tf2xla/mlir_bridge_rollout_policy.h#L28-L41) —— `MlirBridgeRolloutPolicy` 枚举定义了四态：用户显式禁用 `kDisabledByUser`、用户显式启用 `kEnabledByUser`（出错也不回退）、分析后禁用 `kDisabledAfterGraphAnalysis`、分析后启用 `kEnabledAfterGraphAnalysis`（出错可回退）。

[tensorflow/compiler/mlir/tf2xla/mlir_bridge_rollout_policy.cc:27-43](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/mlir/tf2xla/mlir_bridge_rollout_policy.cc#L27-L43) —— 当前实现：尊重用户的显式 `ENABLED`/`DISABLED`；若用户未指定，**默认返回 `kDisabledAfterGraphAnalysis`（禁用）**。这是一个保守的渐进式 rollout 策略——MLIR 桥仍在分阶段开放，未明确开启时优先沿用老路径。

#### 4.3.4 代码实践

**实践目标**：对比经典路线与 MLIR 路线的「翻译时机」，理解 `enable_op_fallback` 的兜底语义。

**操作步骤**：

1. 在 `mlir_tf2xla.cc` 里数清 `ConvertGraphDefToXlaViaMlir` 一共调用了几次「图变换」性质的函数（`ConvertGraphToTfExecutor`、`RunBridgeWithStandardPipeline`、`ConvertMLIRToXlaComputation`），与 4.2 节经典路线的「一次性符号执行」对比。
2. 读 `compile_mlir_util.h` 第 60–62 行关于 `enable_op_fallback` 的注释，回答：「为什么 MLIR 路线还需要保留经典 tf2xla 的能力？」
3. 把 `mlir_bridge_rollout_policy.cc` 的默认返回值（`kDisabledAfterGraphAnalysis`）记下来，解释这意味着什么。

**需要观察的现象**：MLIR 路线明显更「分层」——导入、桥、lowering 是三个独立阶段，每层都可单独测试与替换；而经典路线是一锤子符号执行。

**预期结果**：你能说清「MLIR 路线 = 多个声明式 pass 串联 + 必要时回退到 tf2xla 编译 kernel」，并能解释这种分层带来的可维护性收益。

#### 4.3.5 小练习与答案

**Q1**：`ConvertGraphDefToXlaViaMlir` 最后一步调用 `ConvertMLIRToXlaComputation` 时，`prefer_tf2xla=false` 意味着什么？
**答**：它对应 `enable_op_fallback=false`，表示优先使用 MLIR 原生 legalization pattern 把 TF op 降到 HLO；仅当某 op 没有原生 pattern 时，才考虑回退到经典 tf2xla 编译 kernel。

**Q2**：当前 `GetMlirBridgeRolloutPolicy` 在用户未显式指定时返回什么？这反映了一种什么策略？
**答**：返回 `kDisabledAfterGraphAnalysis`（禁用）。反映 MLIR 桥采用「保守渐进式 rollout」——在未显式开启时沿用稳定的老路径，避免未充分验证的路径影响生产。

---

### 4.4 StableHLO：可移植的 IR 与 TF→StableHLO 导出

> 本节属于最小模块 **`compiler.mlir.stablehlo`**。

#### 4.4.1 概念说明

前面三节都在把 TF 翻译成 **XLA 的 HLO**，目的是「让 XLA 编译并执行」。但 MLIR 生态里还有一个平行目标：**把模型导出成一种稳定、可移植、可长期保存的 IR**，好让 JAX、PyTorch、TF 等不同前端、不同版本都能读写同一份计算描述。这就是 **StableHLO** 的定位。

理解 StableHLO 的关键是它与 MHLO 的关系：

- **MHLO**（「旧」HLO 方言）随 XLA 快速演进，不保证向后兼容——它面向「在编译流水线内部传递」。
- **StableHLO** 是从 MHLO 提炼出的「稳定子集 + 向后兼容承诺」——它面向「保存到磁盘、跨版本跨框架互通」。

所以仓库里的常见做法是：**先把 TF op legal 到 MHLO（因为 legalization pattern 都针对 MHLO 写的），再用 `HloLegalizeToStablehlo` 一步转成 StableHLO 用于导出**。这套逻辑封装在 `tf-stablehlo` pipeline 里。

#### 4.4.2 核心流程

`tf-stablehlo` pipeline 把一个 TF 方言函数转换成 StableHLO，分三步：

```
TF 方言函数
   │
   ├─ TFToMhloPass          # TF op → MHLO op（可按需回退 tf2xla kernel）
   ├─ Canonicalizer         # 规范化、化简、消解中间产物
   └─ HloLegalizeToStablehloPass   # MHLO op → StableHLO op（仅是等价换方言）
   │
   ▼
StableHLO 方言函数（可序列化为可移植 artifact）
```

注意：`tf-stablehlo` 是一条**导出 pipeline**，与 4.3 节的「编译到 XLA 执行」用途不同——它产出的是可移植 IR，而不是直接执行的设备代码。

#### 4.4.3 源码精读

[tensorflow/compiler/mlir/stablehlo/transforms/tf_stablehlo_pass.cc:98-119](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/mlir/stablehlo/transforms/tf_stablehlo_pass.cc#L98-L119) —— `TFToMhloPass::runOnOperation` 的 pattern 收集阶段，把四类重写模式合在一起：① `PopulateLegalizeTfPatterns`（TF→TF 的下层 lowering）；② `PopulateTFLoweringBeforeHLOPatterns`（TF→MHLO 前的预处理）；③ `PopulateLegalizeTfWithTf2XlaPatterns("XLA_CPU_JIT", ...)`（**这里再次出现 tf2xla**，作为 legalization 的兜底模式，与 4.3 节呼应）；④ CHLO→HLO 的模式。随后设 `ConversionTarget`：CHLO 非法、MHLO 合法。

[tensorflow/compiler/mlir/stablehlo/transforms/tf_stablehlo_pass.cc:155-170](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/mlir/stablehlo/transforms/tf_stablehlo_pass.cc#L155-L170) —— `PopulateLegalizeTFToStablehloPipeline` 正是「三步走」：先加 `TFToMhloPass`，再加 `createCanonicalizerPass()`，最后加 `createHloLegalizeToStablehloPass()`。注意 `PassPipelineRegistration` 注册的命令行名字就叫 `"tf-stablehlo"`。注释里也诚实标注了 TODO：未来希望直接产出 StableHLO 而非先走 MHLO 再转换——这印证了「MHLO 是过渡实现、StableHLO 是目标」的现状。

[tensorflow/compiler/mlir/stablehlo/stablehlo.cc:16-25](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/mlir/stablehlo/stablehlo.cc#L16-L25) —— StableHLO 的 Python 绑定其实非常薄：用 nanobind 定义一个模块 `stablehlo_extension`，仅调用 `mlir::stablehlo::AddPortableApi(m)` 注册「可移植 API」。注意它刻意只导出**Portable API**（签名不依赖 MLIR 类的 API）。

[tensorflow/compiler/mlir/stablehlo/stablehlo.py:16-22](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/mlir/stablehlo/stablehlo.py#L16-L22) —— Python 侧只是一个再导出层 `from .stablehlo_extension import *`。文件头注释解释了为什么这么克制：把整套 MLIR Python 绑定都暴露到 TF OSS 维护成本太高（尤其 TF 频繁升级 LLVM 版本），所以只导出签名稳定的 Portable API。

[tensorflow/compiler/mlir/stablehlo/stablehlo_test.py:20-36](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/compiler/mlir/stablehlo/stablehlo_test.py#L20-L36) —— 这个冒烟测试展示了 StableHLO Portable API 的核心用法：`get_api_version()`（API 版本）、`get_current_version()`（目标 StableHLO 版本）、`serialize_portable_artifact_str(assembly, target)`（把一段 StableHLO 文本序列化成可移植 artifact 字节串）、`deserialize_portable_artifact_str`（反序列化）、并断言「序列化→反序列化→再序列化」结果一致——这正是「可移植 artifact」的核心承诺。

#### 4.4.4 代码实践

**实践目标**：亲手把一段 StableHLO 文本变成「可移植 artifact」并验证往返一致，理解 StableHLO 作为序列化格式的意义。

**操作步骤**：

1. 阅读 `tensorflow/compiler/mlir/stablehlo/stablehlo_test.py` 的 `smoketest` 函数，看清那段 `stablehlo.compare` 的 IR 长什么样（它已经是一段合法的 StableHLO 文本）。
2. 在一个能 import 到 `tensorflow.compiler.mlir.stablehlo` 的环境里（构建产物，见预期结果说明），运行：

   ```python
   from tensorflow.compiler.mlir.stablehlo import stablehlo
   assembly = '''
     module @jit_f_jax.0 {
       func.func public @main(%arg0: tensor<ui32>) -> tensor<i1> {
         %0 = stablehlo.constant dense<1> : tensor<ui32>
         %1 = "stablehlo.compare"(%arg0, %0) {compare_type = #stablehlo<comparison_type UNSIGNED>, comparison_direction = #stablehlo<comparison_direction GE>} : (tensor<ui32>, tensor<ui32>) -> tensor<i1>
         return %1 : tensor<i1>
       }
     }
   '''
   target = stablehlo.get_current_version()
   artifact = stablehlo.serialize_portable_artifact_str(assembly, target)
   print("artifact bytes:", len(artifact))
   print("roundtrip equal:", artifact == stablehlo.serialize_portable_artifact_str(
       stablehlo.deserialize_portable_artifact_str(artifact), target))
   ```

3. 对照 `tf_stablehlo_pass.cc` 的 `PopulateLegalizeTFToStablehloPipeline`，回答：「如果输入是一段 TF 方言函数，要经过哪几个 pass 才能变成上面这段 StableHLO？」

**需要观察的现象**：`serialize_portable_artifact_str` 把人类可读的 IR 文本压成一个二进制 artifact；反序列化后内容等价；再序列化结果与第一次一致。

**预期结果**：你理解了 StableHLO 的「可移植 artifact = 一种可序列化、向后兼容、跨框架可读的计算描述」。能否在本机直接 import 该模块取决于是否已构建 `stablehlo_extension`（nanobind 编译产物），若未构建会报 `ModuleNotFoundError`，此情形下请按本步骤改为**源码阅读型实践**（只读 `stablehlo_test.py` 与 `tf_stablehlo_pass.cc` 并回答第 3 步），标注「待本地验证」即可。

#### 4.4.5 小练习与答案

**Q1**：为什么 TF 导出 StableHLO 时要先 legal 到 MHLO，而不是直接产出 StableHLO？
**答**：因为现有的 legalization pattern（`PopulateLegalizeTfPatterns`、`PopulateLegalizeTfWithTf2XlaPatterns` 等）都是针对 MHLO 写的；最简单稳妥的做法是先到 MHLO，再用 `createHloLegalizeToStablehloPass` 等价转换。`tf_stablehlo_pass.cc` 里的 TODO 也确认了这一点，并计划未来直接产出 StableHLO。

**Q2**：StableHLO 的 Python 绑定为什么只导出 Portable API？
**答**：整套 MLIR Python 绑定与 LLVM 版本强耦合，而 TF 频繁升级 LLVM，维护成本极高；Portable API 签名不依赖 MLIR 类、相对稳定，因此只导出这一层来降低维护负担（见 `stablehlo.py` 头部注释）。

**Q3**：MHLO 与 StableHLO 的角色分工是什么？
**答**：MHLO 是随 XLA 演进、不保证向后兼容的「内部流水线」方言，面向编译过程内部传递；StableHLO 是从 MHLO 提炼出的「稳定 + 向后兼容」子集，面向跨版本/跨框架的可移植序列化与互通。

---

## 5. 综合实践：触发 XLA JIT 并观察整条翻译流水线

本实践把全讲内容串起来：从「开一个会触发 XLA 的函数」出发，观察「TF →（MLIR 桥）→ MHLO → HLO → 设备代码」这条真实链路。它结合了 4.3（MLIR 路线）与 4.4（StableHLO/HLO）。

**实践目标**：用 `@tf.function(jit_compile=True)` 触发 XLA 编译，dump 出中间 HLO，对照本讲讲过的翻译步骤，验证「TF 子图确实被翻译成了一段 XLA 计算」。

> 前置说明：`jit_compile=True` 是 `tf.function` 的合法参数（见 [tensorflow/python/eager/polymorphic_function/polymorphic_function.py:1594](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/python/eager/polymorphic_function/polymorphic_function.py#L1594) 的参数文档）。它会让该函数在 tracing 后整体交给 XLA 编译。

**操作步骤**：

1. 写一个最小的 XLA 函数：

   ```python
   import tensorflow as tf

   @tf.function(jit_compile=True)
   def f(x, w, b):
     return tf.nn.relu(tf.matmul(x, w) + b)

   out = f(tf.ones((4, 8), tf.float32),
           tf.ones((8, 16), tf.float32),
           tf.zeros((16,), tf.float32))
   print(out.shape)   # 期望 (4, 16)
   ```

2. 设法 dump 编译产物。常见做法是在运行前设置环境变量（不同 TF 版本支持程度不同）：

   ```bash
   XLA_FLAGS="--xla_dump_to=/tmp/xla_dump --xla_dump_hlo_as_text" python your_script.py
   ```

3. 在 `/tmp/xla_dump` 目录里找到生成的 `.hlo` 或 `.hlomodule` 文本文件，打开查看。

**需要观察的现象（待本地验证）**：dump 出的 HLO 文本里，原本三个 op（`MatMul`、`AddV2`、`Relu`）通常被融合成一条 `fusion` 指令（或少量指令），且能直观看到「dot → add → max(relu)」被合并。这正是 4.1 节讲的「算子融合」收益。

**对照源码回答（必做，不依赖运行环境）**：

1. 这次编译走的是 4.2 经典路线还是 4.3 MLIR 路线？依据 `mlir_bridge_rollout_policy.cc` 的默认策略说明你的判断（注意：默认 `kDisabledAfterGraphAnalysis` 是「桥」的默认；但 `jit_compile=True` 触发的 MLIR XLA 编译路径与 rollout policy 是相关但不同的决策点，若无法确定，标注「待确认」）。
2. 用本讲源码里的函数名，把这次编译的主链路填出来（提示：tracing 出 ConcreteFunction → 子图导入 MLIR → `RunBridgeWithStandardPipeline` → `ConvertMLIRToXlaComputation` → HLO → XLA 后端编译）。
3. 说明 StableHLO 在「这次 JIT 执行」中是否扮演直接角色（提示：JIT 执行面向 HLO/MHLO，StableHLO 主要面向导出/互通；二者在 `tf-stablehlo` 导出 pipeline 才交汇）。

**预期结果**：你能在一张图上把「TF 计算图 → MLIR 模块 → MHLO → HLO → 设备代码」与「TF → MHLO → StableHLO artifact」两条线分清楚，并指出前者服务于执行、后者服务于可移植互通。

> 若环境无法 dump 或运行，请退化为**纯源码阅读型实践**：只完成「对照源码回答」三问，并打开 `tensorflow/compiler/tf2xla/g3doc/cpu_supported_ops.md` 找到 `MatMul`/`Add`/`Relu` 是否在 XLA CPU 支持清单里、各自支持哪些 dtype，以此佐证「这些 op 能被 tf2xla 翻译」。

---

## 6. 本讲小结

- **XLA 是线性代数编译器**，把整段子图融合编译成少量高效 kernel，换来更少的 kernel launch、更低的中间结果落地、更强的跨算子优化——这就是 JIT 收益。
- **tf2xla 是翻译官**，对外只暴露 `ConvertGraphDefToXla` 与 `ConvertGraphDefToXlaViaMlir` 两个入口；翻译范围由 `tf2xla::Config`（feed/fetch/variable）指定。
- **经典路线**靠 `XlaCompiler` 做**符号执行**：把节点指派到虚构的 XLA JIT 设备，逐 op 调用 XLA 编译 kernel，往 `XlaBuilder` 里追加 HLO；要求输入形状已知，参数分 `kConstant`/`kParameter`/`kResource` 三种。
- **MLIR 路线**把翻译拆成「导入 → TF 标准 pipeline → legalize 到 MHLO」三段声明式 pass，并通过 `enable_op_fallback`（`prefer_tf2xla`）在缺 pattern 时回退经典 tf2xla kernel——两路线是「主 + 兜底」而非互斥。
- **StableHLO 是可移植 IR**：与快速演进的 MHLO 相对，它承诺向后兼容，用于序列化为「可移植 artifact」做跨框架/跨版本互通；TF 通过 `tf-stablehlo` pipeline（TF→MHLO→Canonicalize→HloLegalizeToStablehlo）导出。
- MLIR 桥当前采用**保守渐进式 rollout**：用户未显式指定时 `GetMlirBridgeRolloutPolicy` 默认返回 `kDisabledAfterGraphAnalysis`。

---

## 7. 下一步学习建议

- 本讲讲了「单段子图如何被翻译/编译」，但还没回答「**运行时如何自动从一整张大图里找出可编译的子图、把它们圈成一个个 XLA 簇**」。这正是下一讲 **u7-l3 JIT 自动聚类** 的主题，它会讲 `compiler/jit/build_xla_ops_pass.cc` 与 `xla_cluster_util.h`，把本讲的「翻译」放进真实的运行时调度里。
- 想深入 XLA 后端如何把 HLO 真正生成 CPU/GPU 机器码，可继续阅读 `third_party/xla`（vendored 的 XLA 项目）下的后端代码，但注意那是外部仓库、版本随 TF 演进。
- 想理解 TF2 中 `@tf.function(jit_compile=True)` 触发 XLA 的完整 Python/C++ 衔接，可结合 u3-l4（tf.function）回看 `polymorphic_function.py` 中 `_jit_compile` 相关分支。
