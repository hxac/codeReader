# MLIR 与 TF dialect 编译流水线

## 1. 本讲目标

在前面六个单元里，我们走过的都是「运行时」的世界：

- u3-l1 讲清了运行时如何用 C++ 的 `Graph`/`Node`/`Edge` 表示一张计算图，又如何序列化成扁平的 `GraphDef`。
- u3-l2 讲清了 `DirectSession::Run` 在首次执行时如何 **放置 → 优化 → 分区 → 执行**。
- u6-l3 讲清了 Grappler 这套基于 `GraphDef` 的图优化器，如何把图「改写得更快」。

本讲我们换一个视角，进入 **编译器** 的世界，回答一个贯穿 TensorFlow 后端的核心问题：

> **TensorFlow 是怎么用一套现代化的「中间表示 + 编译流水线」来连接上层模型与下层硬件（CPU/GPU/TPU/移动端）的？**

答案就是 **MLIR**。学完本讲，你应当掌握：

1. 理解 **MLIR（Multi-Level Intermediate Representation）** 是什么，以及 TensorFlow 为什么要在 `GraphDef`/`OpKernel` 之上再引入一层 IR。
2. 理解 **dialect（方言）** 与 **pass（编译通参/变换）** 这两个 MLIR 的核心概念，认识 TF 自己定义的 `tf`/`tf_device`/`tf_executor`/`tf_saved_model` 等 dialect。
3. 掌握 **lowering（降级）** 思路：高层 IR 如何被一连串 pass 逐步改写成低层 IR。
4. 认识 `tensorflow/compiler/mlir` 的目录组织，以及 `tf-opt`、`tf-mlir-translate` 这两个面向开发者的命令行工具。
5. 知道 MLIR 是如何被嵌入 TF 运行时的——即所谓的 **MLIR Bridge**。

---

## 2. 前置知识

本讲需要你已经具备以下认知（来自前置讲义），这里用通俗的话再点一遍：

- **GraphDef 与 NodeDef**（u3-l1）：TF 的计算图在磁盘/传输时是一串 `NodeDef`，每个节点用 `input` 字符串列表表达「我从谁拿数据」。它是一份**扁平的、面向运行时**的描述。
- **Op 与 OpKernel**（u4-l1、u4-l2）：`OpDef` 是 op 的「说明书」，`OpKernel::Compute` 才是某个设备上真正的计算实现。这一层是「执行」视角，不是「编译」视角。
- **形状推导**（u4-l3）：`core/ops/` 里的 shape function 能在不执行 kernel 的前提下推出输出形状。MLIR 里也有几乎等价的能力，只是换了一种载体。
- **图优化发生在执行之前**（u3-l2、u6-l3）：`DirectSession::Run` 首次执行前，Grappler 会先做图优化。MLIR 在 TF 中的接入点，本质上也是这条「执行前的编译/优化」链路上的一环。

一个贯穿全讲的直觉：

> **MLIR = 一套「可扩展的多层中间表示 + 通用编译工具链」。TF 用它把「用户写的计算图」一步步翻译、改写、降级成「某块硬件能高效执行的形态」。**

如果你把 GraphDef 想成一张「照片」（扁平、定型），那么 MLIR 就更像一个「活的结构」：它有类型系统、有属性、有可被工具改写的 op，并且能**同时容纳多个抽象层次**的 op 在同一个模块里。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `tensorflow/compiler/mlir/g3doc/overview.md` | MLIR 的官方说明：定位、设计动机、与 LLVM 的关系。本讲的概念基石。 |
| `tensorflow/compiler/mlir/g3doc/dialects.md` | 解释 dialect 概念，列举 TF/XLA/Affine/LLVM/TFLite 等 dialect。 |
| `tensorflow/compiler/mlir/g3doc/_includes/tf_passes.md` | 自动生成的「TF dialect 全部 pass 清单」（由 `mlir-tblgen` 产出），是浏览可用 pass 的目录。 |
| `tensorflow/compiler/mlir/tensorflow/ir/tf_dialect.h` | `tf` dialect（`TensorFlowDialect`）的定义，namespace 为 `"tf"`。 |
| `tensorflow/compiler/mlir/tensorflow/ir/tf_device.h` / `tf_executor.h` | `tf_device`、`tf_executor` 两个辅助 dialect 的定义。 |
| `tensorflow/compiler/mlir/tensorflow/dialect_registration.h` | `RegisterAllTensorFlowDialects()`：把 TF 的全部 dialect 一次性注册进 registry。 |
| `tensorflow/compiler/mlir/register_common_dialects.cc` | `RegisterCommonToolingDialects()`：工具（tf-opt 等）启动时注册 TF + MHLO + StableHLO + 上游全部 dialect。 |
| `tensorflow/compiler/mlir/tensorflow/transforms/fold_broadcast.cc` | 一个**具体可读的 pass**（`tf-broadcast-fold`），作为本讲精读的案例。 |
| `tensorflow/compiler/mlir/tensorflow/tests/fold-broadcast.mlir` | 上面这个 pass 的 lit 测试，是理解「输入 IR → 输出 IR」的最佳样本。 |
| `tensorflow/compiler/mlir/tf_mlir_opt_main.cc` | `tf-opt` 工具的 `main`：注册所有 pass 和 dialect 后进入 MLIR 的通用 `MlirOptMain`。 |
| `tensorflow/compiler/mlir/tf_mlir_translate_main.cc` | `tf-mlir-translate` 工具的 `main`：在 SavedModel/GraphDef 与 MLIR 文本之间互转。 |
| `tensorflow/compiler/mlir/mlir_graph_optimization_pass.cc` | **MLIR Bridge 接入点**：把运行时的 `Graph` 转成 MLIR、跑 pass、再转回 `Graph`。 |
| `tensorflow/compiler/mlir/tf2xla/mlir_bridge_rollout_policy.cc` | 决定「MLIR Bridge 该不该启用」的灰度策略。 |
| `tensorflow/compiler/mlir/tensorflow/transforms/add_functions_for_exported_names.cc` | 另一个可读的 pass（`tf_saved_model` dialect），演示 pass 如何改写 IR。 |
| `tensorflow/compiler/mlir/tensorflow/transforms/bridge.h` | 旧版「标准流水线」`RunBridgeWithStandardPipeline` 的入口（已标记 deprecated，指向 v2）。 |

---

## 4. 核心概念与源码讲解

### 4.1 MLIR 是什么：夹在模型与硬件之间的中间表示

#### 4.1.1 概念说明

MLIR 的全称是 **Multi-Level Intermediate Representation（多级中间表示）**。它的定位，TensorFlow 自己在文档里说得很清楚：

> MLIR 是一种表示格式和一组编译工具库，**它坐在「模型表示」和「生成硬件专用代码的低层编译器/执行器」之间**。

也就是说，从「用户的 Python 代码」到「GPU/TPU 上真正跑起来的机器码」，中间有很长一段路。MLIR 想做这段路上的**通用基建**：你把高层模型翻译成 MLIR，然后用它提供的工具链一步步把高层 op 改写、降级成低层 op，最后再交给 LLVM/XLA 等去生成机器码。

MLIR 受 **LLVM** 影响很深，复用了 LLVM 的许多思想（类型系统、pass 基础设施、 SSA 形式等），但有一个关键创新：

> **它允许「同一个编译单元里，同时混合多个抽象层次」**——高层 TensorFlow op、循环嵌套、甚至 LLVM 指令，可以共存于一个 MLIR 模块中。高层的 pass 会「跳过」它看不懂的低层部分，等低层 pass 来处理。

这正是「Multi-Level」这个名字的含义：传统编译器通常只有一两个固定层次（如 LLVM 只有一层 IR），而 MLIR 是**可分层、可扩展**的。

#### 4.1.2 核心流程

从高空俯瞰，TF 的一条编译流水线长这样：

```
用户的 Python 模型 (tf.function / Keras / SavedModel)
        │  导入(import)
        ▼
   MLIR 模块 (ModuleOp)            ← 高层：tf dialect 的 op
        │  一连串 pass：优化、改写、降级(lowering)
        ▼
   MLIR 模块                        ← 低层：mhlo / stablehlo / tosa / llvm 等 dialect
        │  翻译(translate) / 代码生成
        ▼
   设备代码 (XLA 编译结果 / LLVM 机器码 / TFLite flatbuffer)
```

两个关键词：

- **import（导入）**：把 GraphDef / SavedModel 翻译成 MLIR 文本/对象。
- **lowering（降级）**：用 pass 把高层 op 改写成低层 op，比如把 `tf.Add` 降级成 `mhlo.add`。

#### 4.1.3 源码精读

[overview.md:1-36](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/g3doc/overview.md#L1-L36) 是 MLIR 的总纲说明。它点明 MLIR「本质上是一套现代优化编译器的灵活基础设施」，由两部分组成：**IR 的规格** + **对这套 IR 做变换的工具集**。其中第 19-21 行特别重要——它强调 MLIR 能「在同一个编译单元里组合、分析、变换多个抽象层次的图」，这正是它区别于 LLVM 单层 IR 的地方。

> 关键结论：对 TF 而言，引入 MLIR 是为了**统一**原本散落各处的编译器/优化器（图优化、XLA、TFLite 转换……），用一个可扩展的框架把它们串起来。

---

### 4.2 TF dialect 家族：用 IR 重新表达计算图

> 对应最小模块：**compiler.mlir.tensorflow**

#### 4.2.1 概念说明

MLIR 里没有「全局内置 op 列表」。一切 op 都属于某个 **dialect（方言）**。一个 dialect 就是一组「有名字、有类型约束、有不变量」的 op 集合。比如可以规定「这是一个二元 op，输入输出类型必须相同」。

TensorFlow 为自己的计算图定义了一整套 dialect，最重要的是：

| dialect（namespace） | 作用 |
| --- | --- |
| `tf` | **核心 dialect**。对应 TensorFlow 图里几乎所有 op（`tf.Add`、`tf.MatMul`、`tf.Const`……），是高层表示。 |
| `tf_type` | TF 专有**类型**（如 `tensor<*x!tf_type.variant>`、resource 类型等），与 op 分离。 |
| `tf_device` | 用来表达「设备启动/集群」的辅助 op（`tf_device.launch`、`tf_device.cluster`、`tf_device.replicate`）。 |
| `tf_executor` | 表达 TF V1 图的执行模型（`tf_executor.graph`、`tf_executor.island`、`tf_executor.fetch`），用来忠实承载 V1 控制依赖。 |
| `tf_saved_model` | 表达 SavedModel 的语义（导出名 `tf_saved_model.exported_names`、`bound_input` 等）。 |
| `tfg` | 新一代 GraphDef 的 dialect（`tensorflow/core/ir`），是 `tf` 的近亲。 |

为什么要拆成这么多 dialect？因为它们各自承担**不同抽象层次/不同关注点**的职责：`tf` 负责「计算」，`tf_device` 负责「放在哪台设备上」，`tf_executor` 负责「V1 执行序」，`tf_saved_model` 负责「序列化与导出语义」。这种分层正是 MLIR「多级」思想的体现。

#### 4.2.2 核心流程

一个 MLIR **模块（ModuleOp）** 是最外层容器，里面装着若干 **函数（func.func）**，每个函数体是一串 op。每个 op 的样子大概是：

```mlir
%1 = "tf.AddV2"(%arg0, %arg1) : (tensor<5x7xf32>, tensor<7xf32>) -> tensor<5x7xf32>
```

- `tf.AddV2`：dialect 是 `tf`，op 名是 `AddV2`。
- `%arg0`、`%arg1`：操作数（SSA 值，用 `%` 开头的名字引用）。
- `: (...) -> (...)`：函数式类型签名，明确输入输出张量的形状与元素类型。

这与 GraphDef 里的 `NodeDef` 描述的是同一件事，但形式更结构化、更便于程序化改写。

dialect 的「可用性」靠 **注册（registration）**：一个 MLIRContext 只有先 `registry.insert<某Dialect>()` 注册了某 dialect，才能解析/创建该 dialect 的 op。所以 TF 提供了两个层次的注册函数。

#### 4.2.3 源码精读

**`tf` dialect 的定义**——注意它的 namespace 字符串就是 `"tf"`，这是 MLIR 文本里 `tf.XXX` 前缀的来源：

[tf_dialect.h:34-39](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/tensorflow/ir/tf_dialect.h#L34-L39) 定义 `TensorFlowDialect` 类，其中 `getDialectNamespace()` 返回 `"tf"`。它继承自 MLIR 的 `Dialect` 基类，并重写了类型/属性的解析（重定向到 `tf_type` dialect）。

**辅助 dialect 的 namespace**——`tf_device` 与 `tf_executor` 同样各自声明自己的 namespace：

[tf_device.h:40-42](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/tensorflow/ir/tf_device.h#L40-L42) 定义 `TensorFlowDeviceDialect`，`getDialectNamespace()` 返回 `"tf_device"`。
[tf_executor.h:38-40](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/tensorflow/ir/tf_executor.h#L38-L40) 定义 `TensorFlowExecutorDialect`，namespace 为 `"tf_executor"`。

**一次性注册全部 TF dialect**——运行时/工具不需要逐个 `insert`，调一个函数即可：

[dialect_registration.h:41-59](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/tensorflow/dialect_registration.h#L41-L59) 中的 `RegisterAllTensorFlowDialectsImpl` 一次性把 `ArithDialect`、`FuncDialect`、`MLProgramDialect`、`TF::TensorFlowDialect`、`tf_type::TFTypeDialect`、`cf::ControlFlowDialect`、`tf_device`、`tf_executor`、`tf_saved_model`、`tfg::TFGraphDialect` 全部 `insert` 进 registry。

```cpp
registry
    .insert<mlir::arith::ArithDialect, mlir::func::FuncDialect,
            mlir::ml_program::MLProgramDialect, mlir::TF::TensorFlowDialect,
            mlir::tf_type::TFTypeDialect, mlir::cf::ControlFlowDialect,
            mlir::tf_device::TensorFlowDeviceDialect,
            mlir::tf_executor::TensorFlowExecutorDialect,
            mlir::tf_saved_model::TensorFlowSavedModelDialect,
            mlir::tfg::TFGraphDialect>();
```

关键认知：**dialect 是「按需注册」的**。运行时只需 TF 那几个（见 4.5），而开发工具则需要更全的集合（见 4.4）。

#### 4.2.4 代码实践

1. **实践目标**：建立「dialect = 一组带 namespace 的 op」的直观感受。
2. **操作步骤**：
   - 打开 [dialects.md:6-22](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/g3doc/dialects.md#L6-L22)，通读 dialect 列表。
   - 用 Grep 在 `tensorflow/compiler/mlir/tensorflow/ir/` 下搜索 `getDialectNamespace`，列出 TF 定义的每个 dialect 及其 namespace 字符串。
3. **需要观察的现象**：每个 dialect 类都返回一个不同的 namespace 字符串（`"tf"`、`"tf_device"`、`"tf_executor"`、`"tf_saved_model"`），它们正好对应 MLIR 文本里 `xxx.op` 前缀的 `xxx`。
4. **预期结果**：你能说出一句话——「MLIR 文本里 `tf.AddV2` 的 `tf` 不是关键字，而是被注册进 context 的某个 dialect 的 namespace」。

---

### 4.3 Pass 与 lowering 流水线：用编译 pass 改写图

> 仍属最小模块：**compiler.mlir.tensorflow**（transforms 子目录）

#### 4.3.1 概念说明

光有 IR 还不能编译，还需要能「改写」它的东西。MLIR 里的基本改写单元叫 **pass（通行/变换）**。一个 pass 接收一个 MLIR 模块，按某种规则把它等价地变成另一个（通常是「更好」的）模块。

TF 的 `transforms/` 目录里有 **上百个 pass**，文档 [_includes/tf_passes.md](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/g3doc/_includes/tf_passes.md)（由 `mlir-tblgen` 自动生成）就是它们的目录。每个 pass 都带一个命令行名字（如 `-tf-broadcast-fold`、`-tf-shape-inference`、`-tf-device-cluster-formation`）和一段说明。

pass 大致分三类：

| 类别 | 例子 | 作用 |
| --- | --- | --- |
| **优化（optimization）** | `tf-broadcast-fold`、`tf-shape-inference` | 让图更小/更精确，不改变抽象层次。 |
| **改写（rewrite/legalize）** | `tf-functional-control-flow-to-regions`、`tf-tensor-list-ops-decomposition` | 把一种 op 形态换成另一种等价形态，为降级做准备。 |
| **降级（lowering）** | `convert-tf-control-flow-to-scf`、TF→MHLO 的 legalize | 把高层 dialect 的 op 替换成低层 dialect 的 op。 |

**lowering（降级）** 是其中最关键的概念：它把一个「高抽象」的 op 换成一串「低抽象」的 op。例如 `tf.Add` 降级成 `mhlo.add`，或者 TF 的函数式控制流 `tf.If` 改写成区域式 `tf.IfRegion`（更利于后续分析）。一连串 pass 串起来，就构成一条「从 tf dialect 一路降到硬件」的流水线。

#### 4.3.2 核心流程

写一个 pass 的骨架是高度模式化的：

```
1. 定义一个继承自「PassBase 模板」的结构体，重写 runOnOperation()。
2. 在 runOnOperation() 里：
   - 取出要处理的顶层 op（getOperation()）；
   - 用 Pattern（匹配-重写规则）或直接遍历去改写 IR；
   - 改写失败时可标记 pass 失败（signalPassFailure），或安静返回。
3. 用 GEN_PASS_DEF_XXX 宏 + .inc 文件自动生成注册样板代码。
4. 提供一个 CreateXxxPass() 工厂函数，供流水线组装。
```

这套「声明式 pass」机制（TableGen + `mlir-tblgen`）让新增 pass 的样板代码降到最低，是 MLIR 相比手写图变换的一大优势。

#### 4.3.3 源码精读

我们精读一个**短小完整**的 pass：`tf-broadcast-fold`（broadcast 折叠）。

**直觉**：如果 `tf.BroadcastTo(x)` 的结果马上喂给一个支持「隐式广播」的 op（如 `tf.AddV2`/`tf.Mul`），那这次显式 `BroadcastTo` 就是多余的，可以删掉，直接把原始 `x` 喂给后续 op。少一次广播 = 少一次内存搬运。

**pass 类定义**——继承自动生成的 base，重写 `runOnOperation`：

[fold_broadcast.cc:62-69](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/tensorflow/transforms/fold_broadcast.cc#L62-L69) 用 `GEN_PASS_DEF_BROADCASTFOLDPASS` 宏引入自动生成的 `BroadcastFoldPassBase`，再定义 `BroadcastFoldPass` 子类：

```cpp
#define GEN_PASS_DEF_BROADCASTFOLDPASS
#include ".../tf_passes.h.inc"

class BroadcastFoldPass
    : public impl::BroadcastFoldPassBase<BroadcastFoldPass> {
 public:
  void runOnOperation() override;
};
```

**真正的改写逻辑**——`runOnOperation` 只做两件事：注册一组「匹配-重写」模式，然后让贪婪驱动器去反复应用：

[fold_broadcast.cc:195-201](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/tensorflow/transforms/fold_broadcast.cc#L195-L201) 是 pass 的入口：

```cpp
void BroadcastFoldPass::runOnOperation() {
  RewritePatternSet patterns(&getContext());
  auto func = getOperation();                       // 拿到 func::FuncOp
  patterns.add<ConvertResultsBroadcastableShapeOp>(func.getContext());
  (void)applyPatternsGreedily(func, std::move(patterns));  // 反复套用直到不动点
}
```

核心改写发生在 `ConvertResultsBroadcastableShapeOp::RewriteOp`（[fold_broadcast.cc:141-193](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/tensorflow/transforms/fold_broadcast.cc#L141-L193)）：它检查某个 op 的第 `i` 个操作数是否来自一个 `tf.BroadcastTo`，若是、且形状兼容，就用 `rewriter.modifyOpInPlace` 把这个操作数直接替换成 `BroadcastTo` 的输入，从而消掉那次广播。

**对应的 lit 测试**——这是看「输入 IR → 输出 IR」最直观的样本：

[fold-broadcast.mlir:17-25](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/tensorflow/tests/fold-broadcast.mlir#L17-L25) 是一个测试用例：

```mlir
// RUN: tf-opt -tf-broadcast-fold %s | FileCheck %s
func.func @broadcast_mul0(%arg0: tensor<5x7xf32>, %arg1: tensor<7xf32>) -> tensor<5x7xf32> {
  %cst = arith.constant dense<[5, 7]> : tensor<2xi32>
  %0 = "tf.BroadcastTo"(%arg1, %cst) : (tensor<7xf32>, tensor<2xi32>) -> tensor<5x7xf32>
  %1 = "tf.Mul"(%arg0, %0) : (tensor<5x7xf32>, tensor<5x7xf32>) -> tensor<5x7xf32>
  func.return %1 : tensor<5x7xf32>
  // CHECK: %[[V0:.*]] = "tf.Mul"(%arg0, %arg1) : ... -> tensor<5x7xf32>
}
```

第 15 行的 `RUN:` 说明：用 `tf-opt` 工具跑 `-tf-broadcast-fold` 这个 pass。`CHECK:` 行断言：跑完后，`tf.Mul` 的第二个操作数从广播后的 `%0` 变成了原始的 `%arg1`——`tf.BroadcastTo` 被消掉了。这就是一个 pass 的「输入 IR → 输出 IR」契约。

> 对照 GraphDef 的世界：Grappler 的常量折叠是在 `NodeDef` 列表上「真正跑一遍 OpKernel 求值」；而 MLIR 的 `tf-broadcast-fold` 是在带类型的 IR 上做「模式匹配 + 重写」，根本不需要执行任何 kernel。后者更安全、更可组合。

#### 4.3.4 代码实践

1. **实践目标**：看懂「一个 pass 的输入/输出 IR 形态」，并理解它比手写图变换强在哪。
2. **操作步骤**：
   - 打开 [_includes/tf_passes.md](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/g3doc/_includes/tf_passes.md)，挑一个带「前后 MLIR 示例」的 pass（推荐 `-tf-device-cluster-formation` 或 `-tf-functional-control-flow-to-regions`）。
   - 对照它的「before / after」两段 MLIR，写下：输入里哪个 op、在输出里被替换成了什么。
3. **需要观察的现象**：每个 pass 的文档都给出一对「输入 MLIR / 输出 MLIR」，这正是该 pass 的可执行规格说明。
4. **预期结果**：你能用自己的话回答——「MLIR pass 的优势在于：它在带类型系统的 IR 上做声明式的模式匹配重写，可组合、可测试（lit）、可复用上游基础设施；而手写图变换通常要在扁平 NodeDef 上特判字符串、易错且难复用。」
5. **待本地验证**：如果你能本地构建出 `tf-opt`（`bazel build tensorflow/compiler/mlir:tf-opt`），可实际跑一遍 `tf-opt -tf-broadcast-fold fold-broadcast.mlir` 观察输出；若不具备构建环境，上面的「读 lit 测试」已是完整实践。

#### 4.3.5 小练习与答案

**练习 1**：`tf-broadcast-fold` 的 `runOnOperation` 为什么只调一次 `applyPatternsGreedily`，却没有手动写循环？

> **答案**：`applyPatternsGreedily` 这个「贪婪驱动器」本身就会**反复套用注册的模式直到不动点**（没有模式再能匹配为止）。所以一次调用就足够，不需要手写循环——这正是 MLIR pattern 框架提供的能力。

**练习 2**：pass 类定义里的 `GEN_PASS_DEF_BROADCASTFOLDPASS` 宏和 `tf_passes.h.inc` 起什么作用？

> **答案**：它们是 TableGen 的产物。pass 的命令行名、选项、文档等在 `.td` 文件里声明一次，由 `mlir-tblgen` 生成 `BroadcastFoldPassBase` 基类和注册样板（`.inc`），从而避免每个 pass 都手写大量重复的样板代码。文档 [_includes/tf_passes.md](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/g3doc/_includes/tf_passes.md) 也是同一套 TableGen 生成的。

---

### 4.4 编译工具链 compiler.mlir.tools：tf-opt 与 tf-mlir-translate

> 对应最小模块：**compiler.mlir.tools**

#### 4.4.1 概念说明

MLIR 的好处之一，是它自带一套**通用的命令行工具基建**。TF 在此之上做了两个面向开发者的工具：

| 工具 | 作用 |
| --- | --- |
| `tf-opt` | 「TF pass 驱动器」。读一个 `.mlir` 文本文件，对其运行若干 pass，输出变换后的 `.mlir`。是开发/调试单个 pass 的主力工具，相当于 MLIR 上游 `mlir-opt` 的 TF 定制版。 |
| `tf-mlir-translate` | 「格式翻译器」。在 SavedModel / GraphDef 与 MLIR 文本之间互转（如 `--savedmodel-objectgraph-to-mlir`）。相当于 MLIR 上游 `mlir-translate` 的 TF 定制版。 |

这两个工具的共同点是：**`main` 函数极短**。它们只负责「注册所有 dialect + 注册所有 pass/translation，然后交给 MLIR 上游的通用入口」。

#### 4.4.2 核心流程

```
tf-opt 的 main():
   1. InitMlir（初始化 LLVM/MLIR）
   2. registerAllPasses() / registerTensorFlowPasses() / ... 一长串 register
   3. RegisterCommonToolingDialects(registry)   // 注册全部 dialect
   4. MlirOptMain(argc, argv, "...", registry)  // 进入 MLIR 上游的通用 opt 主循环
```

第 4 步是关键：**TF 没有自己写「读文件、跑 pass、写文件」的主循环**，而是直接复用 MLIR 上游的 `MlirOptMain`。TF 只贡献「注册了哪些 dialect 和 pass」。

#### 4.4.3 源码精读

**`tf-opt` 的 main**——一长串 `register*` 之后，核心只有两行：

[tf_mlir_opt_main.cc:37-69](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/tf_mlir_opt_main.cc#L37-L69) 是 `tf-opt` 的 `main`。前半段是密集的 `register*` 调用（注册上游 pass、TF pass、TF device pass、saved_model pass、MHLO pass、StableHLO bridge pass、TFXLA 聚类/回写 pass……），最后几行才是重点：

```cpp
mlir::DialectRegistry registry;
mlir::RegisterCommonToolingDialects(registry);          // 注册全部 dialect
return failed(
    mlir::MlirOptMain(argc, argv, "TensorFlow pass driver\n", registry));
```

**`RegisterCommonToolingDialects` 的内容**——比运行时用到的 dialect 多得多：

[register_common_dialects.cc:31-43](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/register_common_dialects.cc#L31-L43) 注册了「全部 TF dialect + MHLO + 上游全部 dialect + StableHLO + TFFramework/Quant/Shape/Tensor/Tosa」：

```cpp
void RegisterCommonToolingDialects(mlir::DialectRegistry& registry) {
  mlir::RegisterAllTensorFlowDialects(registry);   // TF 那一整套
  mlir::mhlo::registerAllMhloDialects(registry);   // XLA HLO
  mlir::registerAllDialects(registry);             // MLIR 上游全部
  mlir::registerAllExtensions(registry);
  mlir::stablehlo::registerAllStablehloDialects(registry);  // StableHLO
  registry.insert<...TFFrameworkDialect, QuantDialect, ShapeDialect,
                  TensorDialect, TosaDialect>();
}
```

对比 4.2 里运行时只注册 6 个 dialect，这里要注册几十个——因为工具要能解析开发者可能塞进来的**任何** dialect 的 `.mlir` 文件。

**`tf-mlir-translate` 的 main**——同样很短，但入口是上游的 `Translation` 机制：

[tf_mlir_translate_main.cc:92-99](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/tf_mlir_translate_main.cc#L92-L99) 定义命令行选项后调用注册好的 translation。它额外提供 SavedModel 导入开关（如 `--savedmodel-objectgraph-to-mlir`、`--savedmodel-signaturedefs-to-mlir`），见 [tf_mlir_translate_main.cc:58-69](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/tf_mlir_translate_main.cc#L58-L69)。

> 关键结论：TF 的编译工具几乎「免费」继承了 MLIR 上游的 `mlir-opt` / `mlir-translate` 能力。TF 的工作量集中在「注册自己的 dialect 和 pass」，主循环由上游提供。这正是「站在 MLIR 基建上」的红利。

#### 4.4.4 代码实践

1. **实践目标**：理解「工具 = 注册 dialect/pass + 复用上游主循环」这一模式。
2. **操作步骤**：
   - 对照 [tf_mlir_opt_main.cc:37-69](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/tf_mlir_opt_main.cc#L37-L69)，数一数 `main` 里有多少行是 `register*` 调用、有多少行是真正属于「TF 自己的主循环」。你会发现后者只有最后那两行。
   - 在 [_includes/tf_passes.md](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/g3doc/_includes/tf_passes.md) 里挑一个 pass 名（如 `-tf-shape-inference`），在脑中构造命令 `tf-opt -tf-shape-inference input.mlir -o output.mlir`。
3. **需要观察的现象**：`tf-opt` 的 `main` 几乎是「声明式的注册清单」。
4. **预期结果**：你能解释——「新增一个 pass 后，只要在某个 `registerXxxPasses()` 里挂上它，`tf-opt` 就自动支持用 `-新pass名` 来调用它，无需改 `main`。」

#### 4.4.5 小练习与答案

**练习**：为什么 `tf-opt` 注册的 dialect（`RegisterCommonToolingDialects`）比运行时 MLIR Bridge 注册的 dialect（见 4.5 的 `RegisterDialects`）多很多？

> **答案**：运行时只需要处理「实际会出现在 TF 图里」的那几个 dialect（`tf`/`tf_device`/`tf_executor`/`func`/`arith`/`shape`），多了会增加开销和二进制体积。而 `tf-opt` 是**开发者工具**，必须能解析任何开发者丢进来的 `.mlir`（可能含 MHLO、StableHLO、Tosa、Tensor……），所以要把能想到的 dialect 全注册上。这是「按需注册」原则的体现。

---

### 4.5 MLIR 与 TF 运行时的桥接：MLIR Bridge

#### 4.5.1 概念说明

到目前为止，MLIR 看起来像「另一套独立的编译器」。但 TF 的运行时（`DirectSession`、`tf.function`）本来就有自己的图和优化器（Grappler）。**MLIR 是怎么嵌进运行时的？**

答案是 **MLIR Bridge（MLIR 桥）**：在运行时优化图的某个阶段，把 TF 的 `Graph` **翻译成 MLIR 模块**，跑一串 MLIR pass（主要是为 XLA/TPU 做降级准备），**再翻译回 `Graph`**，让后续的放置/分区/执行照常进行。

这是一个典型的「环出再环回（round-trip）」模式：

```
TF Graph  --(ConvertGraphToTfExecutor)-->  MLIR Module
                                              │ 跑一串 MLIR pass
                                              ▼
TF Graph  <--(ConvertTfExecutorToGraph)---  MLIR Module
```

由于这条链路可能失败，TF 设计了**灰度策略**和**回退（fallback）机制**：如果 MLIR 这条路失败，就退回原来的 `Graph`，不让用户感知。

#### 4.5.2 核心流程

以函数图优化 pass（`MlirFunctionOptimizationPass::Run`）为例，主流程是：

```
1. 询问每个注册 pass 的状态（Enabled / FallbackEnabled / Disabled），汇总 overall_state。
2. 若 overall_state == Disabled，直接返回，不进 MLIR。
3. RegisterDialects(registry) + 建 MLIRContext。
4. ConvertGraphToTfExecutor(graph ...) —— Graph → MLIR（tf_executor 形态）。
   失败：若 Enabled 直接报错；若 FallbackEnabled 则警告并返回原图。
5. 逐个 pass 运行（Enabled 直接改 module；FallbackEnabled 先 clone 再改，失败丢弃 clone）。
6. 若 module 有改动：ConvertTfExecutorToGraph(module ...) —— MLIR → Graph。
7. 返回新 Graph。
```

灰度策略由 `GetMlirBridgeRolloutPolicy` 决定：用户可在 `ConfigProto` 里显式 `ENABLED`/`DISABLED`，否则默认禁用。

#### 4.5.3 源码精读

**运行时只注册 6 个 dialect**——比工具少得多：

[mlir_graph_optimization_pass.cc:164-174](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/mlir_graph_optimization_pass.cc#L164-L174) 的 `RegisterDialects` 只插入 `arith`、`func`、`TF::TensorFlowDialect`、`shape`、`tf_device`、`tf_executor` 这 6 个：

```cpp
static void RegisterDialects(mlir::DialectRegistry& registry) {
  registry.insert<mlir::arith::ArithDialect, mlir::func::FuncDialect,
                  mlir::TF::TensorFlowDialect, mlir::shape::ShapeDialect,
                  mlir::tf_device::TensorFlowDeviceDialect,
                  mlir::tf_executor::TensorFlowExecutorDialect>();
  mlir::func::registerAllExtensions(registry);
}
```

**Graph → MLIR → 跑 pass → MLIR → Graph 的主干**：

[mlir_graph_optimization_pass.cc:259-262](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/mlir_graph_optimization_pass.cc#L259-L262) 调用 `ConvertGraphToTfExecutor` 把 `Graph` 翻成 MLIR 的 `tf_executor` 形态；随后在 [mlir_graph_optimization_pass.cc:288-339](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/mlir_graph_optimization_pass.cc#L288-L339) 逐个 pass 运行（注意 FallbackEnabled 分支会先 `module_ref->clone()`，失败就丢弃克隆，保护原 module）；最后若 module 有改动，在 [mlir_graph_optimization_pass.cc:386-387](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/mlir_graph_optimization_pass.cc#L386-L387) 用 `ConvertTfExecutorToGraph` 把 MLIR 翻回 `Graph`。

**灰度策略**——用户没显式开关时默认禁用：

[mlir_bridge_rollout_policy.cc:27-43](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/tf2xla/mlir_bridge_rollout_policy.cc#L27-L43) 的 `GetMlirBridgeRolloutPolicy` 用一个 `switch` 处理三种情况：用户 `ENABLED` 返回 `kEnabledByUser`，`DISABLED` 返回 `kDisabledByUser`，默认返回 `kDisabledAfterGraphAnalysis`（即默认不走 MLIR）：

```cpp
switch (GetMlirBridgeRolloutState(config_proto)) {
  case ...MLIR_BRIDGE_ROLLOUT_ENABLED:  return kEnabledByUser;
  case ...MLIR_BRIDGE_ROLLOUT_DISABLED: return kDisabledByUser;
  default: return kDisabledAfterGraphAnalysis;   // 默认禁用
}
```

**一个改写 IR 的 saved_model pass 示例**——展示 pass 如何「造新 op、改属性」：

[add_functions_for_exported_names.cc:76-115](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/tensorflow/transforms/add_functions_for_exported_names.cc#L76-L115) 的 `runOnOperation` 处理 SavedModel 里「一个函数用别名导出」的情况：若函数的 `exported_names` 与自身名字不同，就把它改名加 `_internal` 后缀，再用 `OpBuilder` 造一个「蹦床函数」（`func::CallOp` 调用原函数）来顶替导出名。这里用到的 `OpBuilder`、`cloneWithoutRegions`、`setAttr` 等就是 MLIR 提供的「构造/改写 IR」API——和 4.3 里用 `RewritePattern` 的声明式风格不同，这是**命令式**地直接造 op。

> 关键结论：MLIR Bridge 把 MLIR 嵌入了既有运行时——`Graph` 和 MLIR 模块可以互转，pass 在 MLIR 侧跑，跑完再回到 `Graph`。配合灰度策略与 fallback，对用户几乎透明。

#### 4.5.4 代码实践

1. **实践目标**：理清 MLIR 在运行时里的「环出再环回」位置，以及它与 Grappler 的区别。
2. **操作步骤**：
   - 在 [mlir_graph_optimization_pass.cc](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/mlir_graph_optimization_pass.cc) 里定位三处：`ConvertGraphToTfExecutor`（入）、pass 循环、`ConvertTfExecutorToGraph`（出）。
   - 回顾 u6-l3 的 Grappler：它工作在 `GraphDef` 上，由 `MetaOptimizer` 调度。试着用一句话区分两者：**Grappler 改 `GraphDef`，MLIR Bridge 把 `Graph` 换成 MLIR 再改、再换回来**。
3. **需要观察的现象**：`MlirFunctionOptimizationPass::Run` 的开头会先统计各 pass 的 Enabled/FallbackEnabled/Disabled 数量，全 Disabled 时直接 `return OkStatus()`，根本不进 MLIR。
4. **预期结果**：你能回答——「MLIR Bridge 不是替代 Grappler，而是在它之外、面向 XLA/TPU 降级的另一条优化通道；二者都挂在执行前的优化阶段。」
5. **待本地验证**：若设置环境变量 `TF_DUMP_GRAPH_PREFIX=/tmp/mlirdump` 并开启相关 VLOG，可在该目录看到 `mlir_<pass>_before/after.mlir` 的转储文件，直观对比 pass 前后的 IR（对应代码里的 `DumpModule` / `DumpMlirOpToFile`）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `MlirFunctionOptimizationPass` 在 FallbackEnabled 模式下要先 `module_ref->clone()` 再跑 pass？

> **答案**：fallback 模式要求「pass 失败时不能影响原始图」。如果直接在原 module 上跑 pass、跑到一半失败，module 已被部分改坏。所以先克隆一份在克隆上跑：成功就用克隆替换原 module，失败就 `destroy()` 掉克隆、保留原 module 不变。

**练习 2**：`GetMlirBridgeRolloutPolicy` 在用户没有显式配置时返回什么？这意味着什么？

> **答案**：返回 `kDisabledAfterGraphAnalysis`，即**默认不走 MLIR Bridge**。这意味着普通 CPU/GPU 推理默认仍走老的 Graph + Grappler 路径；MLIR Bridge 主要面向 TPU/XLA 等需要降级编译的场景，需要显式或由其他条件触发才会启用。

---

## 5. 综合实践

把本讲的三条主线（dialect、pass、工具/桥接）串起来，完成下面这个「读 + 推」的小任务：

**任务**：以 `-tf-broadcast-fold` 为样本，完整追踪「一段 MLIR 文本 → 经一个 pass → 变换后的文本」的全过程，并把它和运行时桥接对应起来。

1. **读输入**：打开 [fold-broadcast.mlir:17-25](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/tensorflow/tests/fold-broadcast.mlir#L17-L25) 的 `@broadcast_mul0`。指出其中用到了哪几个 dialect（提示：看 op 前缀）。
   - 参考答案：`tf`（`tf.BroadcastTo`、`tf.Mul`）、`func`（`func.func`、`func.return`）、`arith`（`arith.constant`）。
2. **读 pass**：打开 [fold_broadcast.cc:195-201](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/tensorflow/transforms/fold_broadcast.cc#L195-L201) 与 `RewriteOp`（[fold_broadcast.cc:141-193](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/tensorflow/transforms/fold_broadcast.cc#L141-L193)）。说明这个 pass 改写的「匹配条件」和「重写动作」分别是什么。
   - 参考答案：匹配条件——某 op 的某个操作数来自 `tf.BroadcastTo`，且广播前的形状与另一操作数广播兼容、且与结果形状一致；重写动作——用 `modifyOpInPlace` 把该操作数替换成 `BroadcastTo` 的输入，从而消掉这次广播。
3. **推输出**：据此推断 `@broadcast_mul0` 跑完 `-tf-broadcast-fold` 后的 MLIR 长什么样，再和文件里的 `CHECK:` 行对照验证。
   - 参考答案：`tf.Mul` 的第二操作数从 `%0`（广播后）变成 `%arg1`（原始），`tf.BroadcastTo` 与配套的 `%cst` 常量被消去。
4. **连运行时**：说明如果这个 pass 不是被 `tf-opt` 跑、而是被运行时的 MLIR Bridge 跑，它在 [mlir_graph_optimization_pass.cc](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/compiler/mlir/mlir_graph_optimization_pass.cc) 里处在哪两步之间。
   - 参考答案：处在 `ConvertGraphToTfExecutor`（Graph→MLIR）之后、`ConvertTfExecutorToGraph`（MLIR→Graph）之前，即「在 MLIR 模块上跑的那串 pass」之中。

> 这个任务把「dialect 是 IR 的词汇表」「pass 是 IR 的改写规则」「工具/桥接是 pass 的运行宿主」三件事连成了一线。

---

## 6. 本讲小结

- **MLIR** 是夹在「模型表示」与「硬件代码生成」之间的多层中间表示 + 通用编译工具链；TF 引入它是为了统一原本散落各处的编译/优化能力。
- **dialect** 是 MLIR 里 op 的「命名空间与类型约束集合」。TF 定义了 `tf`、`tf_type`、`tf_device`、`tf_executor`、`tf_saved_model`、`tfg` 等一整套 dialect，各管一个抽象层次。
- **pass** 是 IR 的改写单元，分优化/改写/降级三类；**lowering** 就是把高层 dialect 的 op 换成低层 dialect 的 op。一连串 pass 构成降级流水线。
- TF 的编译工具 `tf-opt` / `tf-mlir-translate` 几乎「免费」继承 MLIR 上游的 `MlirOptMain`/`Translation`，TF 只需注册自己的 dialect 和 pass。
- dialect 是**按需注册**的：运行时 `RegisterDialects` 只注册 6 个，开发工具 `RegisterCommonToolingDialects` 注册几十个。
- **MLIR Bridge** 把 MLIR 嵌入运行时：`Graph → MLIR → 跑 pass → MLIR → Graph` 的「环出再环回」，配合灰度策略与 fallback 对用户透明；它与 Grappler 并存，主要面向 XLA/TPU 降级。

---

## 7. 下一步学习建议

本讲解明了「TF 用 MLIR 表达和改写计算图」这一层。接下来：

- **u7-l2（XLA / StableHLO 与 tf2xla）**：顺着 lowering 这条线往下走，看 `tf` dialect 的 op 是如何被 `legalize` 成 `mhlo`/`stablehlo`，再交给 XLA 编译成设备代码的。本讲的「降级」概念在那里会落到具体的 op 映射。
- **u7-l3（JIT 自动聚类）**：看运行时如何自动挑出「可被 XLA 编译」的子图（聚类），那是 MLIR Bridge 真正发挥作用的场景。
- **延伸阅读**：直接读 MLIR 上游文档 <https://mlir.llvm.org> 理解 dialect/pass/operand/region 等通用术语；在本仓库内可继续浏览 `tensorflow/compiler/mlir/tensorflow/transforms/` 下其它 pass，以及 `g3doc/` 的 `overview.md` / `dialects.md` / `tf_passes.md` 三篇官方说明。
