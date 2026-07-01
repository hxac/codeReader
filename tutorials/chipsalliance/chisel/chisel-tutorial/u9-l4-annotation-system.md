# Annotation 注解系统

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 **Annotation（注解）** 是什么、为什么 Chisel 需要它，以及它和「硬件本身」的区别。
- 理解 **`AnnotationSeq`** 作为「贯穿整个编译管道的唯一数据载体」的角色——电路、配置、产物都以注解形式在其中流动。
- 掌握用 **`annotate(...)`** 在 elaboration 期间给某个硬件对象挂上自定义注解，并理解它**为什么是延迟求值**的。
- 认识 **`ChiselAnnotations.scala`** 中那些内建注解（`ChiselGeneratorAnnotation`、`ChiselCircuitAnnotation`、`ChiselOutputFileAnnotation` 等），并理解 `ChiselOption` 这个标记 trait 如何把注解投影成结构化配置。
- 能追踪一条注解从「在模块体里被 `annotate()` 登记」→「随电路收口」→「经 `Elaborate`/`Convert` 流转」→「最终序列化进 CHIRRTL 文本/传给 firtool」的完整生命线。

本讲是单元 9（测试、诊断与二次开发）的一篇，依赖 u5-l1（Stage / Phase 管道）。它回答一个贯穿全手册的疑问：既然「模块构造体只登记不施工」，那么**配置开关、随机化控制、模块裁剪指令**这些「不属于硬件图」的元数据，到底靠什么机制穿过 Chisel 内部、最终去影响 CIRCT/firtool？答案就是 Annotation 系统。

## 2. 前置知识

本讲假设你已具备以下认知（若不熟悉，请先读对应讲义）：

- **elaboration（细化）** 与「只登记不施工」（u1-l5、u3-l1、u4-l1）：模块构造体里的 `IO`、`:=`、`when`、`Reg` 都只是往 Builder 登记命令，真正的固化发生在收口阶段。
- **Builder 全局状态机**（u4-l1）：`DynamicContext` 是一次细化的运行账本，`Builder` 是转发到它的门面。
- **Phase / Stage 管道**（u5-l1）：每个 `Phase` 是一个纯函数 `AnnotationSeq => AnnotationSeq`，靠 `DependencyAPI` 声明依赖；`ChiselStage` 用 `PhaseManager` 按 DAG 拓扑序串联 `Elaborate → Convert → … → CIRCT`。**`AnnotationSeq` 就是 Phase 之间传递数据的唯一通道**——本讲正是把这句话展开。
- **FIRRTL IR**（u4-l2、u4-l5）：Chisel 内部有一棵自己的 Scala 对象树 IR（`chisel3.internal.firrtl.ir.Circuit`），可被 `Converter` 翻译成标准 `firrtl.ir.Circuit`。

一个需要先建立的直觉：**Annotation 是「贴在硬件上的便利贴」，不是硬件本身。** 它不综合进 Verilog（除非它是一条指令，比如「请把这个模块内联掉」），而是携带元数据/指令，随编译链流动，最终或被序列化成 JSON、或被某个下游 Pass 消费。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| [core/src/main/scala/chisel3/Annotation.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Annotation.scala) | 用户 API 入口：`object annotate` 及其包装 `doNotDedup` / `inlineInstance` / `flattenInstance` 等 |
| [src/main/scala/chisel3/stage/ChiselAnnotations.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/ChiselAnnotations.scala) | 内建注解家族 + `ChiselOption` 标记 trait + `ChiselGeneratorAnnotation`/`ChiselCircuitAnnotation` 等 |
| [src/main/scala/chisel3/stage/phases/AddSerializationAnnotations.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/AddSerializationAnnotations.scala) | 一个示范性 Phase：根据 `ChiselOutputFileAnnotation` 派生出 `CircuitSerializationAnnotation` |
| [core/src/main/scala/chisel3/internal/firrtl/IR.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala) | 内部 IR 的 `Circuit` 节点——**注解以「延迟函数」形式存放于此** |
| [core/src/main/scala/chisel3/internal/Builder.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala) | `DynamicContext` 持有 `annotations` 与 `annotationSeq` 两个缓冲区；`buildImpl` 收口时把它们灌进 `Circuit` |
| [core/src/main/scala/chisel3/ElaboratedCircuit.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/ElaboratedCircuit.scala) | elaboration 结果的对外封装；合并「传入注解 + 生成注解」并做序列化前的过滤 |
| [core/src/main/scala/chisel3/experimental/Targetable.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/Targetable.scala) | 类型类 `Targetable` 与存在类型 `AnyTargetable`：把任意「能被指向的硬件对象」统一成一种可注解的目标 |
| [src/main/scala/chisel3/stage/phases/Convert.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Convert.scala) | `Convert` 阶段：消费 `ChiselCircuitAnnotation`，把内部注解抽出来塞回 `AnnotationSeq` |
| [src/test/scala/chiselTests/NewAnnotationsSpec.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala/chiselTests/NewAnnotationsSpec.scala) | 官方测试：演示 `annotate(...)` + `stage.execute(...).collect{...}` 与 `emitCHIRRTL.fileCheck` 两种验证手段 |

> 说明：本仓库注解的「根类型」`firrtl.annotations.Annotation`、`NoTargetAnnotation`、`Unserializable`、`CustomFileEmission` 等都定义在上游 `firrtl` 依赖里（`firrtl.annotations._` / `firrtl.options._`）。Chisel 自己只定义了「如何产生注解、如何用注解」。

---

## 4. 核心概念与源码讲解

### 4.1 Annotation 与 AnnotationSeq：贯穿 Phase 的数据载体

#### 4.1.1 概念说明

先区分两件容易混的事：

- **`Annotation`（单数，一条注解）**：上游 FIRRTL 库定义的 trait，是「贴在电路某个目标上、或全局生效的一段元数据」。一条注解通常包含两部分——一个**目标（target）**（指明贴在哪个模块/信号上，也可能「无目标」即全局）和一个**负载（payload）**（任意用户数据）。它在 Scala 里就是一个 `case class`，能被序列化成 JSON。
- **`AnnotationSeq`（复数，注解序列）**：就是 `Seq[Annotation]` 的别名（上游 `firrtl.AnnotationSeq`）。它是 Chisel/FIRRTL 编译管道里**贯穿所有 Phase 的唯一数据载体**。

回忆 u5-l1 的关键结论：**每个 `Phase` 是一个纯函数 `AnnotationSeq => AnnotationSeq`**。这意味着 Phase 之间不靠共享可变状态、不靠全局变量传数据，而是**靠这条注解序列**。电路本身、命令行配置、输出文件名、生成产物……全部以 `Annotation` 的形式躺在 `AnnotationSeq` 里流动。这是一个非常「函数式」的设计：把「编译状态」显式化成一条数据流。

打个比方：`AnnotationSeq` 是一条在工厂流水线上移动的「传送带」，每个车间（Phase）从带上取下自己关心的零件（注解），加工或替换后放回带，交给下一个车间。

#### 4.1.2 核心流程

一条注解在管道里的典型旅程：

```
 命令行 / 库 API
       │  （Shell 解析 --xxx 成 Annotation，或用户直接 new）
       ▼
 ┌─────────────── AnnotationSeq（传送带）───────────────┐
 │  ChiselGeneratorAnnotation(gen)   ← 输入：模块生成器   │
 │  ChiselOutputFileAnnotation(...)  ← 输入：输出文件名   │
 │  PrintFullStackTraceAnnotation    ← 输入：CLI 开关    │
 └────────────────────┬──────────────────────────────────┘
                      │ Elaborate（细化，长出电路）
                      ▼
 ┌─────────────── AnnotationSeq ─────────────────────────┐
 │  ChiselCircuitAnnotation(ec)     ← 新增：细化出的电路  │
 │  DesignAnnotation(dut)           ← 新增：DUT 句柄      │
 │  + 用户在模块体里 annotate() 的注解（藏在 ec 内）       │
 └────────────────────┬──────────────────────────────────┘
                      │ Convert / AddSerializationAnnotations / …
                      ▼
 ┌─────────────── AnnotationSeq ─────────────────────────┐
 │  CircuitSerializationAnnotation(...)  ← 派生：写盘指令 │
 │  FirrtlCircuitAnnotation(...)         ← 翻译出的标准 IR │
 └────────────────────┬──────────────────────────────────┘
                      │ CIRCT（调用 firtool）
                      ▼
                 SystemVerilog 文本 + 注解 JSON
```

关键点：注解只增不减地在这条带上流动，每个 Phase 用 `flatMap` 把自己关心的注解替换/展开成新注解。

#### 4.1.3 源码精读

`AnnotationSeq` 的真实定义来自上游 `firrtl`，但在 Chisel 源码里到处可见它的用法。一个最干净的「Phase = AnnotationSeq => AnnotationSeq」范本就是 [AddSerializationAnnotations.scala:22-31](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/AddSerializationAnnotations.scala#L22-L31)：

```scala
class AddSerializationAnnotations extends Phase {
  override def prerequisites = Seq(Dependency[Elaborate], Dependency[AddImplicitOutputFile])
  ...
  def transform(annotations: AnnotationSeq): AnnotationSeq = {
    val chiselOptions = view[ChiselOptions](annotations)
    val circuit = chiselOptions.elaboratedCircuit.getOrElse { throw ... }
    val filename = chiselOptions.outputFile.getOrElse(circuit.name).stripSuffix(".fir")
    CircuitSerializationAnnotation(circuit, filename) +: annotations   // ← 在带首加一条，原样保留其余
  }
}
```

这段代码展示了三件本讲反复出现的事：

1. `transform(annotations: AnnotationSeq): AnnotationSeq`——签名就是「注解进、注解出」。
2. `view[ChiselOptions](annotations)`——把扁平的注解序列**投影**成一个结构化配置对象（下一节细讲）。
3. `CircuitSerializationAnnotation(...) +: annotations`——产出新注解、原样保留旧注解。

`view[ChiselOptions]` 的目标类型 `ChiselOptions` 是一个聚合配置，见 [ChiselOptions.scala:11-24](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/ChiselOptions.scala#L11-L24)：它的每个字段（`outputFile`、`useLegacyWidth`、`warningFilters`、`elaboratedCircuit`……）都对应**某一条** `Annotation`。`view` 的职责就是把 `AnnotationSeq` 里这些散落的注解 fold 成一个 `ChiselOptions` 实例供 Phase 读取。这正是 u5-l1 讲过的「OptionsView 把扁平注解投影成结构化配置」。

#### 4.1.4 代码实践

**实践目标**：亲手感受「Phase = AnnotationSeq => AnnotationSeq」。

1. 打开 [AddSerializationAnnotations.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/AddSerializationAnnotations.scala)。
2. 阅读它的 `prerequisites`：它依赖 `Elaborate`（必须先长出电路）和 `AddImplicitOutputFile`（先确定输出文件）。
3. 对比 `Elaborate`（[Elaborate.scala:39-88](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala#L39-L88)）的 `transform`：它用 `annotations.flatMap { case ChiselGeneratorAnnotation(gen) => ...; case a => Some(a) }` 把输入注解里的 `ChiselGeneratorAnnotation` **替换**成 `ChiselCircuitAnnotation`+`DesignAnnotation`，其余注解原样透传。

**需要观察的现象**：两个 Phase 都遵循同一个模式——「关心某条注解就替换它，不关心的用 `case a => Some(a)` 原样放回」。这就是 `AnnotationSeq` 作为传送带的使用范式。

**预期结果**：你能用自己的话回答——「Phase 之间靠什么传数据？为什么不需要全局可变变量？」

#### 4.1.5 小练习与答案

**练习 1**：为什么 Chisel 用一条 `AnnotationSeq` 而不是一个全局可变的 `Config` 对象来贯穿管道？

> **参考答案**：`AnnotationSeq => AnnotationSeq` 让每个 Phase 成为纯函数，无副作用、可单独测试、可按依赖（`DependencyAPI`）任意重排与并行；配置项以「数据」形式存在带子上，谁需要谁投影（`view`），而不是谁都能改全局变量。这正是 u5-l1 里 PhaseManager 能按 DAG 拓扑排序的前提。

**练习 2**：`view[ChiselOptions](annotations)` 与直接 `annotations.collect{...}` 找某条注解相比，优势在哪？

> **参考答案**：`view` 把一整族相关注解一次性折叠成一个结构化对象（`ChiselOptions` 的 13 个字段），并提供默认值与合法性；逐处 `collect` 既啰嗦又容易遗漏默认值处理，且无法表达「这些字段同属一组」。

---

### 4.2 annotate()：用户添加自定义注解的入口

#### 4.2.1 概念说明

读者在前几讲见过 `dontTouch(x)`、`addAttribute(reg, "...")` 这类「给信号打标记」的 API。它们背后统一调用的就是 [Annotation.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Annotation.scala) 里的 `object annotate`。本节就讲它。

`annotate` 的设计哲学浓缩在三件事里：

1. **强制把目标作为参数传入**——`annotate(targets: ...*)(mkAnnos: ...)`，被注解的对象（信号/模块）必须显式列在 `targets` 里，**不能藏在 `mkAnnos` 闭包里偷偷用**。这样 Chisel 才能对它们做安全检查。
2. **安全检查是即时（同步）的，但产注解是延迟（异步）的**——`targets` 立刻被 `requireIsAnnotatable` 校验，而真正构造注解的 `mkAnnos`（里面会调 `.toTarget`）被包成 thunk 推迟执行。
3. **产出的不是 `Annotation`，而是「`() => Seq[Annotation]`」**——一个待求值的函数，被登记进 Builder 的缓冲区。

为什么必须延迟？因为调用 `annotate(...)` 的那一刻，你身处模块构造体内部，**信号名、模块实例名都还没定稿**（命名发生在收口阶段，见 u7-l3）。如果立刻调 `data.toTarget`，拿到的名字是错的甚至抛异常。把 `.toTarget` 推迟到整电路命名完成后再求值，目标才能正确解析。

#### 4.2.2 核心流程

```
annotate(target)(Seq(SomeAnnotation(target.toTarget)))
        │ │
        │ └─ mkAnnos: => Seq[Annotation]  （by-name，暂不求值）
        │
        ├─ 同步：target match { case d: Data => requireIsAnnotatable(d); 视图则登记重命名 }
        │
        └─ Builder.annotations += (() => mkAnnos)   ← 包成 thunk 入队
                                  │
              （整电路命名完成后，Circuit.firrtlAnnotations 求值）
                                  ▼
                     thunk() => mkAnnos => Seq(SomeAnnotation(正确名字))
```

两个缓冲区（[Builder.scala:466](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L466) 与 [Builder.scala:534](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L534)）的区别务必分清：

- `annotationSeq: AnnotationSeq`——**入口注解**，即调用 `ChiselStage` 时外部传入的注解（命令行、库 API 注入）。
- `annotations: ArrayBuffer[() => Seq[Annotation]]`——**生成注解**，即 elaboration 期间用 `annotate()` 登记的「延迟函数」。

收口时 [Builder.scala:1164-1176](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L1164-L1176) 把前者塞进 `ElaboratedCircuit` 的第二参数、把后者塞进 `Circuit` 节点的 `annotations` 字段——延迟函数尚未求值。

#### 4.2.3 源码精读

`annotate` 的两个重载入口在 [Annotation.scala:24-45](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Annotation.scala#L24-L45)：

```scala
def apply(targets: AnyTargetable*)(mkAnnos: => Seq[Annotation]): Unit = {
  targets.map(_.a).foreach {
    case d: Data =>
      requireIsAnnotatable(d, "Data marked with annotation")   // ← 同步安全检查
      if (dataview.isView(d)) dataview.recordViewForRenaming(d) // ← 视图登记重命名
    case _ => ()
  }
  Builder.annotations += (() => mkAnnos)                        // ← 延迟登记
}

def apply[T: Targetable](targets: Seq[T])(mkAnnos: => Seq[Annotation]): Unit =
  annotate(targets.map(t => AnyTargetable.toAnyTargetable(t)): _*)(mkAnnos)  // ← 委托给上面
```

逐行解读：

- `targets: AnyTargetable*`——可变参数，每个目标都被「类型擦除」成 `AnyTargetable`（见 4.3 节），这样同一个 `annotate` 调用能混注 `Data` 和 `Module`。
- `mkAnnos: => Seq[Annotation]`——**按名调用（by-name）**参数，调用方传进去的表达式此时**不会执行**。
- `targets.map(_.a).foreach { case d: Data => ... }`——同步遍历目标做检查。`requireIsAnnotatable` 见 [experimental/package.scala:68-83](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/package.scala#L68-L83)：它先 `requireIsHardware`（必须是已绑定硬件值，不能是纯类型或字面量），再拒绝字面量（`isLit`）和动态索引（`DynamicIndexBinding`）。
- `Builder.annotations += (() => mkAnnos)`——**核心一行**：把 `mkAnnos` 包成无参函数 `() => mkAnnos` 入队。注意 `mkAnnos` 是 by-name，所以这里捕获的是「将来要执行的表达式」，而非其当前值。
- 第二个重载用 `AnyTargetable.toAnyTargetable(t)` 把强类型 `Seq[T]` 擦除后委托给第一个。

配套的用户友好包装都在同一文件里，例如 [Annotation.scala:80-90](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Annotation.scala#L80-L90) 的 `doNotDedup`、[Annotation.scala:105-115](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Annotation.scala#L105-L115) 的 `inlineInstance`、以及不在本文件但同样套路的 [dontTouch.scala:36-46](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/dontTouch.scala#L36-L46) 与 [AttributeAnnotation.scala:37-39](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/AttributeAnnotation.scala#L37-L39)。它们共同模式都是 `annotate(target)(Seq(某种具体 Annotation(target.toTarget)))`。

#### 4.2.4 代码实践

**实践目标**：亲手写一个最小自定义注解并用 `annotate()` 挂上，验证它确实被延迟求值、并最终出现在产出里。

下面是**示例代码**（非仓库原有，参考 [NewAnnotationsSpec.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala/chiselTests/NewAnnotationsSpec.scala) 的写法）：

```scala
// 示例代码
import chisel3._
import chisel3.experimental.annotate
import chisel3.stage.{ChiselGeneratorAnnotation, ChiselCircuitAnnotation}
import firrtl.annotations.{Annotation, NoTargetAnnotation}
import circt.stage.ChiselStage

// 1) 定义一个最小注解：NoTargetAnnotation 表示「全局、不指向具体信号」
case class AuthorMeta(name: String) extends NoTargetAnnotation

class Greet extends RawModule {
  val in  = IO(Input(UInt(8.W)))
  val out = IO(Output(UInt(8.W)))
  out := in
  // 2) 用 annotate 挂上，注意目标 this（模块本身）显式传入
  annotate(this)(Seq(AuthorMeta("chisel-learner")))
}

// 3) 跑管道，从返回的 AnnotationSeq 里回收自己的注解
val annos: Seq[Annotation] = new ChiselStage().execute(
  Array("--target", "chirrtl", "--target-dir", "test_run_dir"),
  Seq(ChiselGeneratorAnnotation(() => new Greet))
)
val mine = annos.collect { case a: AuthorMeta => a }
require(mine == Seq(AuthorMeta("chisel-learner")), s"got $mine")
```

**操作步骤**：

1. 把上面片段放进一个 ScalaTest（参考 [NewAnnotationsSpec.scala:18](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala/chiselTests/NewAnnotationsSpec.scala#L18) 的类骨架）。
2. 用 `./mill chisel[].test.test chiselTests.NewAnnotationsSpec` 同款命令跑（替换类名）。

**需要观察的现象**：

- 即使 `AuthorMeta("chisel-learner")` 里**没有** `.toTarget` 调用，它也是延迟求值的——你可以在 `annotate` 这一行的 `mkAnnos` 里加 `println("eval now")`，会发现它**不是在构造模块时打印**，而是在收口/序列化阶段才打印。
- 若把注解改成带目标的，例如 `case class MyA(t: firrtl.annotations.Target) extends ...` 并在 `mkAnnos` 里写 `MyA(in.toTarget)`，同样会延迟到名字定稿后才解析 `in` 的最终名字。

**预期结果**：`mine` 恰好是一条 `AuthorMeta("chisel-learner")`，断言通过。若运行环境无 firtool，可用 `./mill chisel[].compile` 至少保证编译通过；仿真执行结果**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `annotate(this)(Seq(AuthorMeta("x")))` 写成 `annotate()(Seq(AuthorMeta("x")))`（不传 target）会怎样？

> **参考答案**：能编译通过并工作——`NoTargetAnnotation` 本就不需要目标。但 `annotate` 的设计鼓励**即使不立即用目标，也把它传入**，以便 Chisel 做安全检查；这条注解因为没有目标，无法在下游被 `target` 定位，只能被全局消费。

**练习 2**：为什么 `mkAnnos` 必须是 by-name（`=> Seq[Annotation]`）而不是普通 `Seq[Annotation]`？

> **参考答案**：若按值传递，`mkAnnos` 会在调用 `annotate` 的瞬间被求值，那时 `.toTarget` 拿到的信号名/模块名尚未定稿，会得到错误目标甚至抛异常。by-name 配合 `() => mkAnnos` 才能把求值推迟到命名完成后。

---

### 4.3 Targetable 与 AnyTargetable：注解如何「指向」硬件

#### 4.3.1 概念说明

一条「带目标的」注解需要能精确指到「电路图里的某个具体对象」——某个模块实例、某个端口、某条线。这个「指向」在 FIRRTL 里叫 **Target**（`firrtl.annotations.IsMember`/`Target`）。问题在于：在 Chisel 的 Scala 侧，能被指向的东西种类繁多——`Data`（信号）、`BaseModule`（模块实例）、`Hierarchy[BaseModule]`（层级句柄）、`MemBase`（内存）……

Chisel 用 **类型类（type class）** 而不是继承来统一它们，定义在 [Targetable.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/Targetable.scala)：

- `sealed trait Targetable[A]`——「类型 `A` 能被转成 Target」，提供 `toTarget`/`toAbsoluteTarget`/`toRelativeTarget` 等方法。
- 它的伴生对象里给了若干隐式实例：`forNamedComponent`（覆盖 `Data`/`MemBase`）、`forBaseModule`（模块）、`forHierarchy`（层级句柄）、`forHasTarget` 等。
- `AnyTargetable`——一个**存在类型**（existential type），用来「类型擦除」地装下任意 `Targetable`，使 `annotate(targets: AnyTargetable*)` 能混注不同种类的目标。

> **关键术语：type class（类型类）**——一种「外加」的能力声明。`Targetable[A]` 不要求 `A` 继承某个 trait，而是由隐式实例「旁证」`A` 具备该能力。这比继承灵活：例如 `Hierarchy[A]` 不约束 `A`，无法靠继承实现，但靠类型类可以。源码注释 [Targetable.scala:16-18](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/Targetable.scala#L16-L18) 明确说明了这一点。

#### 4.3.2 核心流程

```
   用户代码: annotate(mod, mod.io.in)(...)
                    │     │
   隐式解析 Targetable[Module]  /  Targetable[UInt]
                    │     │
                    ▼     ▼
            AnyTargetable.apply(a)(implicit targetable)   ← 类型擦除
                    │
                    ▼
   annotate(targets: AnyTargetable*)(...): targets.map(_.a).foreach{ 同步检查 }
                    │
                    ▼  （延迟）
            mkAnnos 里调用  a.toTarget  →  实际委托 targetable.toTarget(a) → firrtl IsMember
```

`AnyTargetable.toAnyTargetable(a)`（[Targetable.scala:131-140](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/Targetable.scala#L131-L140)）把强类型 `a: A` 连同其隐式 `Targetable[A]` 实例打包成一个 `AnyTargetable`（`type A` 被存在量化），从而允许不同 `A` 进入同一集合。

#### 4.3.3 源码精读

类型类本体在 [Targetable.scala:19-50](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/Targetable.scala#L19-L50)：

```scala
sealed trait Targetable[A] {
  def toTarget(a: A): IsMember
  def toAbsoluteTarget(a: A): IsMember
  def toRelativeTarget(a: A, root: Option[BaseModule]): IsMember
  ...
}
```

伴生对象的扩展方法把 `toTarget` 「挂」回 `A` 上（[Targetable.scala:55-61](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/Targetable.scala#L55-L61)），所以用户写 `data.toTarget` 看起来像普通方法调用，背后其实是 `targetable.toTarget(data)`：

```scala
implicit class TargetableSyntax[A](a: A)(implicit targetable: Targetable[A]) {
  def toTarget: IsMember = targetable.toTarget(a)
  ...
}
```

存在类型 `AnyTargetable` 在 [Targetable.scala:112-123](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/Targetable.scala#L112-L123)：

```scala
sealed trait AnyTargetable {
  type A
  def a: A
  def targetable: Targetable[A]
  def toTarget: IsMember = targetable.toTarget(a)   // ← 仍能调用，擦除了类型但保留了能力
  ...
}
```

一个混注不同种类目标的真实例子见 [NewAnnotationsSpec.scala:88-96](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala/chiselTests/NewAnnotationsSpec.scala#L88-L96)：把模块 `this` 与 `Seq[UInt]` 拼进同一个 `Seq[AnyTargetable]` 后一次性 `annotate`。

#### 4.3.4 代码实践

**实践目标**：体会 type class「外加能力」的写法，与继承对比。

1. 打开 [Targetable.scala:67-97](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/Targetable.scala#L67-L97)，列出 4 个隐式实例各自服务的类型。
2. 在 [NewAnnotationsSpec.scala:84-97](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala/chiselTests/NewAnnotationsSpec.scala#L84-L97) 里看 `val ys = Seq[AnyTargetable](this) ++ xs.map(AnyTargetable(_))` 如何把 `RawModule` 和一堆 `UInt` 放进同一序列。

**需要观察的现象**：`AnyTargetable(_)` 是 [Targetable.scala:143](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/Targetable.scala#L143) 的隐式转换，编译器自动为每个 `UInt`/`Module` 找到对应 `Targetable` 实例并擦除包装。

**预期结果**：你能解释——「为什么 Chisel 不让 `Data` 和 `BaseModule` 都继承一个 `Targetable` 父类？」答：因为 `Hierarchy[A]` 不约束 `A`，继承做不到，type class 可以（见源码注释）。

#### 4.3.5 小练习与答案

**练习 1**：`toTarget` 与 `toAbsoluteTarget` 有何区别？

> **参考答案**：`toTarget` 返回相对当前上下文的目标；`toAbsoluteTarget` 返回从电路顶层开始的完整层次路径。当你在嵌套模块里注解一个深处的信号时，二者不同；下游 Pass 通常需要绝对目标来唯一定位。

**练习 2**：`AnyTargetable` 为什么要用存在类型（`type A`）而不是直接 `Any`？

> **参考答案**：直接 `Any` 会丢失「这个值附带了一个 `Targetable` 实例」的信息。存在类型把「某个 `A`」连同「它的 `Targetable[A]` 实例」一起封存，从而擦除静态类型后仍能在运行期安全调用 `targetable.toTarget(a)`。

---

### 4.4 ChiselAnnotations：内建注解家族

#### 4.4.1 概念说明

[ChiselAnnotations.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/ChiselAnnotations.scala) 是 Chisel 自带的一柜子「标准注解」。按用途可分四类：

| 类别 | 代表注解 | 作用 |
|------|----------|------|
| **输入：要细化的设计** | `ChiselGeneratorAnnotation(gen)` | 携带 `() => RawModule` 生成器，是 Elaborate 的原料 |
| **输出：细化产物** | `ChiselCircuitAnnotation(ec)`、`DesignAnnotation(dut)` | 长出电路后由 Elaborate 产生 |
| **配置：CLI 开关** | `PrintFullStackTraceAnnotation`、`ThrowOnFirstErrorAnnotation`、`WarningsAsErrorsAnnotation`、`ChiselOutputFileAnnotation`、`UseLegacyWidthBehavior`、`UseSRAMBlackbox`、`SuppressSourceInfoAnnotation`… | 命令行 `--full-stacktrace` 等经 `Shell` 解析而来，控制 Chisel 行为 |
| **派生/特殊** | `CircuitSerializationAnnotation`、`RemapLayer`、`WarningConfigurationAnnotation` | 由 Phase 派生，或携带特殊数据 |

两个贯穿全柜的关键设计：

1. **`sealed trait ChiselOption { this: Annotation => }`**（[ChiselAnnotations.scala:31](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/ChiselAnnotations.scala#L31)）——一个**标记 trait**（mixin）。任何 `Annotation` 混入它，就声明「我属于 Chisel 配置族」，`ChiselOptionsView` 据此把它们 fold 进 `ChiselOptions`。注意它是自类型 `this: Annotation =>`，强制只能混进真正的 `Annotation`。
2. **`with Unserializable`**——大量注解混入它（来自 `firrtl.options.Unserializable`）。意为「这条注解**不进**最终序列化的 JSON/anno 文件，只在 Chisel 进程内有效」。例如 `ChiselGeneratorAnnotation` 携带一个 Scala 闭包，根本无法序列化。与之对应，`CircuitSerializationAnnotation` 混入 `BufferedCustomFileEmission`，负责把电路写盘。

#### 4.4.2 核心流程

一条 CLI 开关如何变成行为：

```
  命令行 --warnings-as-errors
        │  ShellOption 解析（HasShellOptions）
        ▼
  WarningsAsErrorsAnnotation   （混 ChiselOption + Unserializable）
        │  加入 AnnotationSeq
        ▼
  view[ChiselOptions](annotations) 把它 fold → ChiselOptions.warningFilters 等
        │  （其实 asFilter 转成 WarningFilter.Error，见 ChiselAnnotations.scala:85）
        ▼
  DynamicContext(..., chiselOptions.warningFilters, ...)（Elaborate.scala:52）
        │
        ▼
  影响 ErrorLog 的警告处理行为
```

#### 4.4.3 源码精读

`ChiselOption` 标记 trait 与一个典型 CLI 注解见 [ChiselAnnotations.scala:31-49](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/ChiselAnnotations.scala#L31-L49)：

```scala
sealed trait ChiselOption { this: Annotation => }     // ← 标记 + 自类型

case object PrintFullStackTraceAnnotation
    extends NoTargetAnnotation
    with ChiselOption
    with HasShellOptions
    with Unserializable {
  val options = Seq(new ShellOption[Unit](
    longOption = "full-stacktrace",
    toAnnotationSeq = _ => Seq(PrintFullStackTraceAnnotation),
    helpText = "Show full stack trace when an exception is thrown"
  ))
}
```

读法：

- `extends NoTargetAnnotation`——无目标（全局），来自上游 FIRRTL。
- `with ChiselOption`——声明属于配置族，会被 `ChiselOptionsView` 收走。
- `with HasShellOptions`——自带 `options: Seq[ShellOption[_]]`，让命令行 Shell 能识别 `--full-stacktrace` 并产出本注解（u5-l1 的 Shell/CLI 机制）。
- `with Unserializable`——不进序列化输出。

管道两端的「输入注解 / 输出注解」分别见：

- 输入：[ChiselAnnotations.scala:218-223](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/ChiselAnnotations.scala#L218-L223) 的 `ChiselGeneratorAnnotation`，其 `def elaborate: AnnotationSeq = (new Elaborate).transform(Seq(this))` 是「一行触发细化」的便捷入口。
- 输出：[ChiselAnnotations.scala:300-331](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/ChiselAnnotations.scala#L300-L331) 的 `ChiselCircuitAnnotation`，包着一个 `ElaboratedCircuit`，注意它手写了 `equals`/`hashCode`/`Product`——因为大电路的 `hashCode` 反复查询会严重拖慢性能（见注释 [ChiselAnnotations.scala:320-323](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/ChiselAnnotations.scala#L320-L323)），故缓存 `lazy val hashCode`。

「派生」类的代表是 [ChiselAnnotations.scala:361-434](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/ChiselAnnotations.scala#L361-L434) 的 `CircuitSerializationAnnotation`——它混入 `BufferedCustomFileEmission`，其 `writeToFileImpl`（[ChiselAnnotations.scala:409-415](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/ChiselAnnotations.scala#L409-L415)）调用 `emitLazily` 把电路流式写盘。它正是 4.1 节 `AddSerializationAnnotations` 派生出来的那条注解。

#### 4.4.4 代码实践

**实践目标**：用一条 CLI 开关注解观察其对行为的影响。

1. 阅读上面的 `PrintFullStackTraceAnnotation` 与 [ChiselAnnotations.scala:53-67](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/ChiselAnnotations.scala#L53-L67) 的 `ThrowOnFirstErrorAnnotation`。
2. 在 [Elaborate.scala:39-88](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala#L39-L88) 里追踪 `chiselOptions.throwOnFirstError`（[Elaborate.scala:48](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala#L48)）和 `printFullStackTrace`（[Elaborate.scala:82](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala#L82)）如何被消费。

**需要观察的现象**：`ThrowOnFirstErrorAnnotation` 经 `view[ChiselOptions]` 进入 `ChiselOptions.throwOnFirstError`，再传入 `DynamicContext`，最终被 `ErrorLog`（[Builder.scala:555](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L555)）使用——这正是 u9-l2 讲的「首错即抛 vs 先收集后汇报」开关的落点。

**预期结果**：你能画出 `--throw-on-first-error` 字符串 → `ShellOption` → `ThrowOnFirstErrorAnnotation` → `ChiselOptions.throwOnFirstError` → `DynamicContext` → `ErrorLog` 的完整链路。这是 CLI 字符串「穿透」到 elaboration 行为的注解路径。

#### 4.4.5 小练习与答案

**练习 1**：为什么几乎所有 CLI 注解都混入 `Unserializable`？

> **参考答案**：它们是 Chisel **进程内**的行为开关（栈轨迹长度、警告策略），对下游 CIRCT/firtool 无意义，且部分携带不可序列化的 Scala 对象（如闭包）。`Unserializable` 确保它们不进最终 `.anno` JSON，避免污染下游与序列化失败。

**练习 2**：`ChiselCircuitAnnotation` 为什么要手写 `equals`/`hashCode` 而不靠 `case class` 自动生成？

> **参考答案**：它持有巨大的 `ElaboratedCircuit`，自动 `hashCode` 会在反复查询时重算整棵电路导致性能崩溃；手写并缓存 `lazy val hashCode` 避免此问题（[ChiselAnnotations.scala:323](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/ChiselAnnotations.scala#L323)）。同类优化也出现在 `CircuitSerializationAnnotation`。

---

### 4.5 注解的延迟求值与管道流转

#### 4.5.1 概念说明

前四节解决了「注解是什么、怎么加、指向谁、有哪些内建」；本节把它们串成一条完整的生命线，回答规格里的核心问题——**「`annotate()` 登记的注解如何穿越 Elaborate→Convert 管道并影响下游」**。

核心机制是 **延迟求值的求值点**。回忆 4.2：`Builder.annotations` 存的是 `ArrayBuffer[() => Seq[Annotation]]`，是一堆**没求值的 thunk**。这些 thunk 在收口时被原样塞进内部 IR 的 `Circuit` 节点（[IR.scala:663](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L663)），**直到有人读取 `Circuit.firrtlAnnotations` 时才真正求值**。这个求值点在 [IR.scala:671](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L671)：

```scala
def firrtlAnnotations: Iterable[Annotation] = annotations.flatMap(_().flatMap(_.update(renames)))
```

- 外层 `_()`——对每个 thunk 求值，得到 `Seq[Annotation]`（此刻信号名已定稿，`.toTarget` 解析正确）。
- 内层 `_.update(renames)`——把每条注解的目标过一遍 **`RenameMap`**。这服务于 DataView（u8-l2）：若被注解的信号其实是个视图（view），其真实目标要重命名，`dataview.recordViewForRenaming`（[Annotation.scala:29](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Annotation.scala#L29)）登记的就是这张 `renames` 表。

#### 4.5.2 核心流程

```
 模块体内 annotate(t)( mkAnnos )
        │
        ▼ Builder.annotations += (() => mkAnnos)        （thunk，未求值）
        │
 收口 buildImpl: Circuit(..., annotations.toSeq, ...)   （仍是 thunk 序列）
        │
 Elaborate 产出 ChiselCircuitAnnotation(ElaboratedCircuit(circuit, initialAnnos))
        │
        ├─ 若走 emitCHIRRTL：ElaboratedCircuit.lazilySerialize
        │     合并 initialAnnotations ++ circuit.firrtlAnnotations  ← 此刻求值 thunk
        │     过滤掉 Unserializable / CustomFileEmission
        │     → Serializer 流式产出 CHIRRTL 文本 + 注解 JSON
        │
        └─ 若走 Convert（旧路径，7.11.0 起 deprecated）：
              a.elaboratedCircuit.annotations  ← 同样调 circuit.firrtlAnnotations，求值 thunk
              把它们塞回 AnnotationSeq，供下游 Pass/firtool 消费
```

`ElaboratedCircuit` 把「入口注解」与「生成注解」合二为一的代码在 [ElaboratedCircuit.scala:77-84](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/ElaboratedCircuit.scala#L77-L84)：

```scala
override def lazilySerialize: Iterable[String] = {
  val annotations = (initialAnnotations.view ++ circuit.firrtlAnnotations).flatMap {
    case _: Unserializable     => None     // ← CLI 开关注解被滤掉
    case _: CustomFileEmission => None     // ← 写盘类注解被滤掉
    case a => Some(a)
  }.toVector
  lazilySerialize(annotations)
}
```

而 `Convert` 阶段抽取注解的代码在 [Convert.scala:24-32](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Convert.scala#L24-L32)：

```scala
case a: ChiselCircuitAnnotation =>
  Some(a) ++
  Some(FirrtlCircuitAnnotation(Converter.convert(a.elaboratedCircuit._circuit))) ++
  a.elaboratedCircuit.annotations    // ← circuit.firrtlAnnotations，求值 thunk，塞回 AnnotationSeq
```

#### 4.5.3 源码精读

把求值链条的三个关键点并排对照：

1. **登记（thunk 入队）**——[Annotation.scala:33](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Annotation.scala#L33)：`Builder.annotations += (() => mkAnnos)`。
2. **入 IR（仍 thunk）**——[Builder.scala:1168](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L1168)：`Circuit(..., annotations.toSeq, ...)`；类型即 [IR.scala:663](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L663) 的 `annotations: Seq[() => Seq[Annotation]]`。
3. **求值（thunk → Annotation）**——[IR.scala:671](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L671)：`def firrtlAnnotations = annotations.flatMap(_().flatMap(_.update(renames)))`。

另外，`Definition`（u8-l1）会把子电路的注解上抛到父级 `Builder.annotations`：[Definition.scala:132](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/experimental/hierarchy/core/Definition.scala#L132) 的 `Builder.annotations ++= ir._circuit.annotations`——这是层级化设计里注解跨层汇聚的入口。

#### 4.5.4 代码实践

**实践目标**：追踪一条带目标的注解从登记到出现在 CHIRRTL 文本里的全过程。

**操作步骤**：

1. 阅读官方测试 [NewAnnotationsSpec.scala:78-107](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala/chiselTests/NewAnnotationsSpec.scala#L78-L107)。它在 `emitCHIRRTL` 后用 `fileCheck()` 断言 CHIRRTL 文本里包含形如下面的 JSON：
   ```
   "class":"firrtl.transforms.DontTouchAnnotation"
   "target":"~|Top>in"
   ```
2. 对照 [ElaboratedCircuit.scala:77-84](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/ElaboratedCircuit.scala#L77-L84) 与 [IR.scala:671](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L671)，定位「thunk 在哪一步被求值」。
3. 想验证可在 4.2.4 的示例里把 `AuthorMeta` 改成带目标版（继承 `firrtl.annotations.Annotation` 并持有一个 `Target`），再用 `ChiselStage.emitCHIRRTL(new Greet).fileCheck()(...)`（混入 `chisel3.testing.scalatest.FileCheck` trait，见 u9-l1）断言 JSON 出现。

**需要观察的现象**：

- CHIRRTL 文本末尾会有一段注解 JSON（FIRRTL 的注解序列化格式）。
- `Unserializable` 的注解（如 `AuthorMeta` 若混了它）**不会**出现在 JSON 里——这正是过滤逻辑的体现。
- 目标里的 `~|Top>in` 是 FIRRTL Target 语法：`~` 电路名省略、`|Top` 顶层模块、`>in` 模块内端口 `in`。

**预期结果**：你能指着代码回答「thunk 在哪一行被求值」「注解 JSON 在哪一步被拼进 CHIRRTL」。命令的仿真执行结果**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：把求值从 [IR.scala:671](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L671) 提前到 `annotate()` 调用那一刻，会发生什么？

> **参考答案**：`mkAnnos` 里的 `.toTarget` 会在模块构造期（命名未定稿时）被求值，拿到错误信号名/模块名甚至抛 `NamedException`；且 DataView 注解的重命名（`renames`）此时尚未建立，视图目标会错。延迟到收口后求值是正确性的前提。

**练习 2**：`ElaboratedCircuit.lazilySerialize` 为什么要过滤 `Unserializable` 和 `CustomFileEmission` 两类？

> **参考答案**：`Unserializable` 是进程内配置（CLI 开关），不该进输出文件；`CustomFileEmission`（如 `CircuitSerializationAnnotation`）自己负责写盘（写 `.fir` 文件），其内容不应再以 JSON 形式重复塞进 CHIRRTL 文本。

---

## 5. 综合实践

**任务**：实现一个「带元数据的加法器」，把本讲四个最小模块串起来用一遍。

要求：

1. **定义**一个自定义注解 `case class DocAnnotation(note: String, target: firrtl.annotations.Target)`，它**可序列化**（不混 `Unserializable`）。
2. 在一个 `RawModule` 里声明两个 `Input(UInt(8.W))` 与一个 `Output(UInt(8.W))`，做加法（用 `+&` 保留进位或截断均可）。
3. 用 `annotate(out)(Seq(DocAnnotation("sum output", out.toTarget)))` 给输出端口挂上注解（注意目标显式传入，体会 type class 隐式解析）。
4. 用两种方式验证注解确实穿越了管道：
   - **方式 A（回收 AnnotationSeq）**：`new ChiselStage().execute(Array("--target","chirrtl","--target-dir","test_run_dir"), Seq(ChiselGeneratorAnnotation(() => new MyAdder)))`，再 `.collect { case a: DocAnnotation => a }`，断言拿到 1 条且 `target` 正确。
   - **方式 B（文本对照）**：`ChiselStage.emitCHIRRTL(new MyAdder)` 后混入 `FileCheck` trait，断言文本含 `"class":...DocAnnotation` 与 `"target":"~|...>out"`。
5. **进阶**：给模块同时加一个 `dontTouch(out)`（来自 [dontTouch.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/dontTouch.scala)），在回收结果里同时 `collect` 出 `DontTouchAnnotation`，验证它和你的 `DocAnnotation` 走的是同一条 `annotate` → thunk → `firrtlAnnotations` 路径。

**检查清单**：

- [ ] 能指出 `annotate` 把 thunk 存进 `Builder.annotations`（[Annotation.scala:33](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Annotation.scala#L33)）。
- [ ] 能指出 thunk 在 `Circuit.firrtlAnnotations`（[IR.scala:671](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L671)）求值。
- [ ] 能解释 `view[ChiselOptions]` 把注解投影成配置（[AddSerializationAnnotations.scala:23](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/AddSerializationAnnotations.scala#L23)）。
- [ ] 能解释 `Unserializable` 为何从最终 JSON 中消失（[ElaboratedCircuit.scala:79-81](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/ElaboratedCircuit.scala#L79-L81)）。

> 提示：若 firtool 未配置，至少用 `./mill chisel[].compile` 保证编译通过；运行结果**待本地验证**。

## 6. 本讲小结

- **Annotation 是「贴在硬件上的便利贴」，不是硬件本身**；`AnnotationSeq`（= `Seq[Annotation]`）是贯穿所有 Phase 的**唯一数据载体**，每个 Phase 是纯函数 `AnnotationSeq => AnnotationSeq`。
- **`annotate(targets)(mkAnnos)`** 是用户入口：目标**同步**做 `requireIsAnnotatable` 安全检查，`mkAnnos`（by-name）被包成 thunk **延迟**登记进 `Builder.annotations`——因为信号名要到收口才定稿，`.toTarget` 必须推迟。
- **`Targetable` 类型类**统一了 `Data`/`BaseModule`/`Hierarchy` 等可指向对象的 `toTarget` 能力；**`AnyTargetable` 存在类型**做类型擦除，让一次 `annotate` 能混注不同种类的目标。
- **`ChiselAnnotations.scala`** 是内建注解柜：`ChiselGeneratorAnnotation`（输入）、`ChiselCircuitAnnotation`/`DesignAnnotation`（产物）、一堆 `with ChiselOption + HasShellOptions + Unserializable` 的 CLI 开关；`ChiselOption` 标记 trait 让 `view[ChiselOptions]` 把它们投影成结构化配置。
- **延迟求值的求值点在 `Circuit.firrtlAnnotations`**（[IR.scala:671](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L671)）：`annotations.flatMap(_().flatMap(_.update(renames)))`——求值 thunk、并过 `RenameMap` 处理 DataView 重命名。
- 注解经 `ElaboratedCircuit.lazilySerialize`（合并入口+生成注解、滤掉 `Unserializable`/`CustomFileEmission`）进入 CHIRRTL 文本，或经 `Convert`（旧路径）塞回 `AnnotationSeq` 供下游 CIRCT/firtool 消费。

## 7. 下一步学习建议

- **回到 u5-l1 / u5-l3**：把 `PhaseManager` 的依赖 DAG 与 `Convert`/`Emitter`/`AddSerializationAnnotations` 联起来看，你会发现自己已经能读懂一个 Phase 是怎么「在合适的时机被拉进管道」的。
- **读 u9-l1（测试体系）**：`FileCheck` 是验证注解 JSON 最趁手的工具，本讲的 `emitCHIRRTL.fileCheck` 用法来自那里；`UnitTest` + circt-test 则能把注解一路跑到仿真。
- **拓展阅读**：在仓库里 `grep` `extends NoTargetAnnotation` 与 `extends Annotation`，会发现 `chisel3.util`（如 `decoder.scala`、`SRAM`）、`properties`（u8-4 的 OMR 注解）、`layer`/`probe`（u8-3）都重度使用注解——它们是本讲机制的更复杂实例。
- **进阶练习**：仿照 `AddSerializationAnnotations` 写一个自定义 `Phase`，读取你定义的 `DocAnnotation`，把 `note` 字段写进一个旁路 `.txt` 文件（模仿 `CustomFileEmission`）。这将同时检验你对「Phase = AnnotationSeq => AnnotationSeq」与「注解可序列化」两点的掌握。
