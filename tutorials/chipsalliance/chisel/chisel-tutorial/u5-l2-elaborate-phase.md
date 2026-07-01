# Elaborate 阶段

## 1. 本讲目标

上一讲（u5-l1）我们把 Chisel 编译器看成一条 `AnnotationSeq => AnnotationSeq` 的 Phase 管道，并用 `DependencyAPI` 解释了阶段之间如何靠注解声明依赖。本讲要钻进这条管道里**第一个真正「长出电路」的阶段**——`chisel3.stage.phases.Elaborate`。

学完本讲，你应当能够：

1. 说清 `Elaborate.transform` 是如何从一串注解里捞出 `ChiselGeneratorAnnotation`，并把其中的 Scala 生成器函数「跑」成一个电路的。
2. 对照源码，列出 `ChiselOptions`（以及 `LoggerOptions`）里的哪些字段被读出、又分别填进了 `DynamicContext` 的哪一个构造参数——即**配置如何从注解流入一次 elaboration 的运行账本**。
3. 解释 `Builder.build` 内部的「先绑定上下文、后执行构造体」顺序，以及它如何收口产出 `ChiselCircuitAnnotation`。
4. 理解 elaboration 抛错时，Chisel 为什么能把冗长的 Scala/Java 内部栈裁剪成「只剩用户代码」的精简堆栈。

本讲是 u4-l1（Builder 全局状态机）的下游：u4-l1 讲了 `DynamicContext`/`Builder` 在 elaboration 期间**维护**什么状态，本讲讲**谁创建**这个 `DynamicContext`、**何时**把它交给 `Builder`。

## 2. 前置知识

在进入源码前，先用最直白的话把几个概念对齐：

- **elaboration（细化）**：运行你写的 Scala 构造体，让 `Module`、`IO`、`Reg`、`when` 这些 API「长出」一棵电路对象树。前端 API「只登记不施工」，登记的命令最终被收拢成内部 IR（见 u4-l2）。
- **Annotation（注解）/ AnnotationSeq**：贯穿整条 Phase 管道的唯一数据载体。一个注解就是一条「带类型的信息」，可以是配置开关、生成的电路、输出文件路径等。`AnnotationSeq` 就是注解的列表，每个 Phase 读它、改它、再传给下一个 Phase（见 u5-l1）。
- **Phase**：一个 `AnnotationSeq => AnnotationSeq` 的纯函数。`Elaborate` 就是一个 Phase，它的职责是「输入里若有 `ChiselGeneratorAnnotation`，就跑 elaboration，把结果替换成 `ChiselCircuitAnnotation`」。
- **OptionsView（配置投影）**：注解是「扁平」的一条条记录，而代码里更愿意读一个「结构化的配置对象」。`OptionsView` 就是一个把一串注解 `fold` 成一个 case-class 风格配置对象的工具。本讲里 `view[ChiselOptions](annotations)` 干的就是这件事。
- **DynamicVariable**：Scala 标准库里的一种「动态作用域变量」。`dynamicContextVar.withValue(某个值){ 代码块 }` 会让代码块内对该变量的读取都返回「某个值」，离开代码块后自动恢复。Chisel 用它来隐式地把「当前正在进行的这次 elaboration 的上下文」传递给深层的 API 调用（见 u4-l1）。

一句话串起本讲：**注解 → 配置投影 → DynamicContext → Builder.build → 电路注解**。这就是 `Elaborate` 阶段做的事。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [src/main/scala/chisel3/stage/phases/Elaborate.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala) | 本讲主角。定义 `class Elaborate extends Phase`，在 `transform` 里创建 `DynamicContext`、调用 `Builder.build`、捕获并裁剪异常。 |
| [src/main/scala/chisel3/stage/ChiselOptions.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/ChiselOptions.scala) | 结构化的 Chisel 配置对象，列出所有可配置开关（位宽行为、警告、层映射等），是 `DynamicContext` 字段的来源。 |
| [src/main/scala/chisel3/stage/package.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/package.scala) | 定义 `ChiselOptionsView`，说明每条注解如何映射成 `ChiselOptions` 的某个字段（配置的「上游」）。 |
| [src/main/scala/chisel3/stage/ChiselAnnotations.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/ChiselAnnotations.scala) | 定义 `ChiselGeneratorAnnotation`（输入注解）、`ChiselCircuitAnnotation`（输出注解）、`DesignAnnotation`。 |
| [core/src/main/scala/chisel3/internal/Builder.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala) | 定义 `DynamicContext`（一次 elaboration 的运行账本）和 `Builder.build`/`buildImpl`（真正执行 elaboration、组装电路的入口）。 |
| [core/src/main/scala/chisel3/internal/Error.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Error.scala) | 提供 `trimStackTraceToUserCode`，把异常堆栈裁剪到只剩用户代码（异常处理的实现）。 |
| [core/src/main/scala/chisel3/internal/ElaborationTrace.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/ElaborationTrace.scala) | 可选的模块级耗时追踪器，由 `Elaborate` 创建并注入 `DynamicContext`。 |

> 提示：`core` 子项目里的 `internal` 包是私有实现，`src/main` 子项目里的 `stage` 包是面向管道的整合层。`Elaborate` 属于后者，但它「伸手」调用了 `core` 的 `Builder`/`DynamicContext`。关于子项目划分见 u1-l3。

## 4. 核心概念与源码讲解

本讲按数据流把内容拆成四个最小模块：

- **4.1 ChiselGeneratorAnnotation**：输入——把「如何构造一个电路」装进注解。
- **4.2 Elaborate Phase**：触发器——管道里识别输入注解并启动 elaboration 的阶段。
- **4.3 DynamicContext**：账本——配置如何从 `ChiselOptions` 注入一次 elaboration 的运行状态。
- **4.4 Builder.build 与 ChiselCircuitAnnotation**：执行与产出——真正跑构造体、组装电路、处理异常。

### 4.1 ChiselGeneratorAnnotation：把「如何构造电路」装进注解

#### 4.1.1 概念说明

Phase 管道里流动的是注解，但「一个电路」本身不是一个静态数据——它需要**被执行**才能长出来。于是 Chisel 用了一个巧妙的设计：把「构造电路的方法」——一个返回 `RawModule` 的**零参函数**（generator，生成器）——塞进一条注解里，让它随管道流动。到了 `Elaborate` 阶段，再把这个函数「调用一次」，电路就长出来了。

这就是 `ChiselGeneratorAnnotation`：它不存电路，它存「**怎么造电路**」。

#### 4.1.2 核心流程

- 用户/上层 API 把一个 `() => RawModule` 函数包成 `ChiselGeneratorAnnotation`，放入 `AnnotationSeq`。
- 这条注解随管道流过若干前置 Phase（如 `Checks`）。
- 到达 `Elaborate` 时，函数被调用一次：`gen()` 返回一个 `RawModule` 实例，再用 `Module(...)` 包一层走完模块构造生命周期（见 u3-l1）。
- 函数体内所有 `IO`/`Reg`/`when` 调用被登记成命令（见 u4-l2）。

注意「零参函数」的语义：注解携带的是**延迟计算**。电路在注解流到 `Elaborate` 之前并不会被构造，这与「只登记不施工」一脉相承。

#### 4.1.3 源码精读

`ChiselGeneratorAnnotation` 的定义只有一个字段——生成器函数：

```scala
case class ChiselGeneratorAnnotation(gen: () => RawModule) extends NoTargetAnnotation with Unserializable {

  /** Run elaboration on the Chisel module generator function stored by this [[firrtl.annotations.Annotation]]
    */
  def elaborate: AnnotationSeq = (new chisel3.stage.phases.Elaborate).transform(Seq(this))
}
```

这是 [src/main/scala/chisel3/stage/ChiselAnnotations.scala:218-223](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/ChiselAnnotations.scala#L218-L223)。两个细节值得注意：

- `gen: () => RawModule`：类型是一个**无参函数值**，调用它（`gen()`）才返回模块。`RawModule` 是不带隐式 clock/reset 的模块基类（见 u3-l1）。
- `def elaborate`：这是 `Elaborate` 阶段的一个**独立入口**——直接 `new` 一个 `Elaborate` 并对其 `transform` 喂入只含自己的注解序列。后面 4.4 的综合实践会用到它，让你**绕开完整管道、单独跑 elaboration**。

`extends ... with Unserializable` 表示这条注解不能被序列化（函数无法序列化），只活在一次编译运行的内存里。这与 `ChiselCircuitAnnotation` 一致——电路注解也是 `Unserializable`（见 [src/main/scala/chisel3/stage/ChiselAnnotations.scala:300-305](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/ChiselAnnotations.scala#L300-L305)）。

#### 4.1.4 代码实践

**实践目标**：确认 `ChiselGeneratorAnnotation` 携带的是一个「待执行」的函数，而非已构造的电路。

**操作步骤**（源码阅读型）：

1. 打开 [ChiselAnnotations.scala:218](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/ChiselAnnotations.scala#L218)，确认 `gen` 的类型是 `() => RawModule`。
2. 在仓库内搜索 `ChiselGeneratorAnnotation(` 的调用点（例如 `ChiselStage` 相关代码或测试），观察调用方传入的通常形如 `() => new MyModule`。

**需要观察的现象**：传入的是一个 lambda，模块 `new MyModule` 的构造**没有立即发生**。

**预期结果**：你会看到形如 `ChiselGeneratorAnnotation(() => new Foo)` 的写法，证明电路构造被推迟到 `Elaborate.transform` 内部的 `gen()` 调用。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ChiselGeneratorAnnotation` 要存「函数」而不是直接存「已经构造好的 `RawModule` 对象」？

**参考答案**：因为 elaboration 必须在 `Builder.build` 绑定的 `DynamicContext` 内执行，模块构造体里的每一条 API 调用都要登记到「当前上下文」。如果提前在注解创建时就构造好模块，那些命令就登记到了一个还不存在的上下文里。把构造延迟到 `Elaborate` 阶段、并在 `Builder.build` 内部调用 `gen()`，才能保证登记发生在正确的上下文中。

**练习 2**：`def elaborate` 这一行 `(new chisel3.stage.phases.Elaborate).transform(Seq(this))` 的入参为什么是 `Seq(this)`？

**参考答案**：`transform` 的入参是整个 `AnnotationSeq`。独立调用时，把「只含本注解」的序列传进去，`transform` 会识别出这条 `ChiselGeneratorAnnotation` 并执行 elaboration，其余（不匹配的）注解原样透传。

---

### 4.2 Elaborate Phase：管道里的 elaboration 触发器

#### 4.2.1 概念说明

`Elaborate` 是一个标准的 `Phase`：它声明自己的前置依赖，提供一个 `transform` 方法。它的核心使命只有一句话——**遍历注解，遇到 `ChiselGeneratorAnnotation` 就跑 elaboration，把结果替换成 `ChiselCircuitAnnotation`；其它注解原样保留**。它是整条管道里第一个「产生硬件对象树」的阶段。

#### 4.2.2 核心流程

`Elaborate.transform` 的执行过程可以概括为：

1. 用 `annotations.flatMap { ... }` 逐条处理注解。
2. 命中 `ChiselGeneratorAnnotation(gen)`：
   - 用 `view[ChiselOptions](annotations)` 把注解投影成结构化配置；
   - 同样投影出 `LoggerOptions`；
   - `new` 一个 `DynamicContext`，把上述配置塞进去；
   - 调 `Builder.build(Module(gen()), context)` 执行 elaboration，拿到 `(elaboratedCircuit, dut)`；
   - 返回两条注解：`ChiselCircuitAnnotation(elaboratedCircuit)` 和 `DesignAnnotation(dut, ...)`。
3. 非该类型的注解（`case a => Some(a)`）原样透传。
4. 整个过程包在 `try/catch` 里，捕获 `NonFatal` 异常并按需裁剪堆栈。

用伪代码表示（省略细节）：

```
transform(annotations) =
  annotations.flatMap {
    case ChiselGeneratorAnnotation(gen) =>
      chiselOptions = view[ChiselOptions](annotations)
      loggerOptions = view[LoggerOptions](annotations)
      context       = new DynamicContext(... 各配置字段 ...)
      (circuit, dut)= Builder.build(Module(gen()), context)   // 真正长出电路
      Seq(ChiselCircuitAnnotation(circuit), DesignAnnotation(dut, ...))
    case a => Some(a)   // 其它注解原样保留
  }
```

#### 4.2.3 源码精读

`Elaborate` 的依赖声明（这是 u5-l1 讲的 `DependencyAPI`）：

```scala
class Elaborate extends Phase {

  override def prerequisites: Seq[Dependency[Phase]] = Seq(
    Dependency[chisel3.stage.phases.Checks],
    Dependency(_root_.logger.phases.Checks)
  )
  override def optionalPrerequisites = Seq.empty
  override def optionalPrerequisiteOf = Seq.empty
  override def invalidates(a: Phase) = false
```

见 [src/main/scala/chisel3/stage/phases/Elaborate.scala:29-37](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala#L29-L37)。它声明**硬前提**是 `Checks`（Chisel 侧）和 `logger.phases.Checks`（日志侧）——即这两个校验阶段必须先跑。`invalidates(a) = false` 表示它不撤销任何其它阶段的效果。

`transform` 的入口与注解分流：

```scala
def transform(annotations: AnnotationSeq): AnnotationSeq = annotations.flatMap {
  case ChiselGeneratorAnnotation(gen) =>
    val chiselOptions = view[ChiselOptions](annotations)
    val loggerOptions = view[LoggerOptions](annotations)
    ...
  case a => Some(a)
}
```

见 [src/main/scala/chisel3/stage/phases/Elaborate.scala:39-42](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala#L39-L42) 与 [第 87 行](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala#L87)。

- `view[ChiselOptions](annotations)`：`view` 来自 `firrtl.options.Viewer.view`，配合隐式对象 `ChiselOptionsView`（见 [package.scala:18](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/package.scala#L18)），把一串扁平注解 `fold` 成一个结构化的 `ChiselOptions`。
- `flatMap` + `case a => Some(a)`：把一条输入注解「展开」成零到多条输出注解。`ChiselGeneratorAnnotation` 展开成两条（电路注解 + 设计注解），其它注解保持为一条。

> 这里有一个重要的设计点：`view[ChiselOptions]` 在每个 `ChiselGeneratorAnnotation` 上**都重新投影一次**整条 `annotations`。因为 `flatMap` 是逐条处理，而配置是「全局」的，所以每次都要从完整注解序列里重新算出配置。对于一次只含一个顶层模块的典型场景，开销可忽略。

#### 4.2.4 代码实践

**实践目标**：确认 `Elaborate` 在管道依赖图里的位置，以及它「只对 `ChiselGeneratorAnnotation` 起作用、其余透传」的行为。

**操作步骤**（源码阅读型）：

1. 打开 [Elaborate.scala:31-34](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala#L31-L34)，记下它的 `prerequisites`。
2. 用搜索查找 `chisel3.stage.phases.Checks` 的定义，确认它确实先于 `Elaborate`。

**预期结果**：你会看到 `Elaborate` 依赖 `Checks`，说明在电路被真正长出来之前，配置类注解的合法性已经被检查过。

#### 4.2.5 小练习与答案

**练习 1**：`Elaborate` 用的是 `flatMap` 而不是 `map`，这暗示了什么？

**参考答案**：`map` 要求「一进一出」，而 `flatMap` 允许「一进多出/一进零出」。这里一条 `ChiselGeneratorAnnotation` 进来，要展开成 `ChiselCircuitAnnotation` 和 `DesignAnnotation` 两条出去，所以必须用 `flatMap`。

**练习 2**：如果把 `case a => Some(a)` 这一行删掉，会发生什么？

**参考答案**：所有非 `ChiselGeneratorAnnotation` 的注解都会被丢弃（`flatMap` 对未匹配且无返回的情况会跳过）。后续阶段（如 `Convert`、`Emitter`）依赖的配置注解、输出文件注解等会全部丢失，管道会出错或产出错误结果。

---

### 4.3 DynamicContext：一次 elaboration 的运行账本

#### 4.3.1 概念说明

`DynamicContext` 是「一次 elaboration 的运行账本」——它持有这次细化过程中所有可变状态：当前正在构造的模块、已收集的命令、错误日志、命名栈、层与选项集合等（u4-l1 已详细讲过它**内部**维护什么）。本讲不重复那些字段，而是聚焦在另一个问题：**这些字段的初值从哪里来？**

答案就是 4.2 里的 `view[ChiselOptions]`：用户通过命令行/注解设置的开关，先被 `ChiselOptionsView` 投影成 `ChiselOptions`，再由 `Elaborate` 把相关字段**逐个填进** `DynamicContext` 的构造参数。理解这条「配置 → 账本」的映射，是本讲的核心实践任务。

#### 4.3.2 核心流程

配置的完整溯源分三段：

1. **CLI / 上游注解 → `ChiselOptions`**：`ChiselOptionsView`（[package.scala:18-45](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/package.scala#L18-L45)）用 `foldLeft` 把每条 `ChiselOption` 注解累加成一个 `ChiselOptions`，例如 `ThrowOnFirstErrorAnnotation → copy(throwOnFirstError = true)`。
2. **`ChiselOptions` → `DynamicContext`**：`Elaborate` 在 `new DynamicContext(...)` 时，把 `ChiselOptions` 的字段**按位置**传给构造器（少数字段由 `Elaborate` 自己填默认值）。
3. **`DynamicContext` → `Builder` 全局状态**：`Builder.build` 用 `DynamicVariable` 把这个 context 绑定为「当前上下文」，此后所有 `Builder.xxx` 访问器都转发到它（见 u4-l1）。

#### 4.3.3 源码精读

先看 `DynamicContext` 的构造参数清单（顺序很重要，因为 `Elaborate` 是按位置传参的）：

```scala
private[chisel3] class DynamicContext(
  val annotationSeq:       AnnotationSeq,
  val throwOnFirstError:   Boolean,
  val useLegacyWidth:      Boolean,
  val includeUtilMetadata: Boolean,
  val useSRAMBlackbox:     Boolean,
  val warningFilters:      Seq[WarningFilter],
  val sourceRoots:         Seq[File],
  val defaultNamespace:    Option[Namespace],
  val loggerOptions:      LoggerOptions,
  val definitions:        mutable.LinkedHashSet[Definition[_ <: BaseModule]],
  val contextCache:       BuilderContextCache,
  val layerMap:           Map[layer.Layer, layer.Layer],
  val inlineTestIncluder: InlineTestIncluder,
  val suppressSourceInfo: Boolean,
  var elideLayerBlocks:   Boolean,
  val elaborationTrace:   ElaborationTrace
)
```

见 [core/src/main/scala/chisel3/internal/Builder.scala:465-483](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L465-L483)。再对照 `Elaborate` 里 `new DynamicContext(...)` 的实参：

```scala
val elaborationTrace = new ElaborationTrace
val context =
  new DynamicContext(
    annotations,                                              // annotationSeq
    chiselOptions.throwOnFirstError,                          // throwOnFirstError
    chiselOptions.useLegacyWidth,                             // useLegacyWidth
    chiselOptions.includeUtilMetadata,                        // includeUtilMetadata
    chiselOptions.useSRAMBlackbox,                            // useSRAMBlackbox
    chiselOptions.warningFilters,                             // warningFilters
    chiselOptions.sourceRoots,                                // sourceRoots
    None,                                                     // defaultNamespace
    loggerOptions,                                            // loggerOptions
    mutable.LinkedHashSet[Definition[_ <: BaseModule]](),     // definitions（空）
    BuilderContextCache.empty,                                // contextCache
    chiselOptions.layerMap,                                   // layerMap
    chiselOptions.inlineTestIncluder,                         // inlineTestIncluder
    chiselOptions.suppressSourceInfo,                         // suppressSourceInfo
    false,                                                    // elideLayerBlocks
    elaborationTrace                                          // elaborationTrace
  )
```

见 [src/main/scala/chisel3/stage/phases/Elaborate.scala:44-63](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala#L44-L63)。

把「实参来源」逐行整理成下表（本讲的核心结论之一）：

| `DynamicContext` 字段 | `Elaborate` 传入的实参 | 来自哪里 |
| --- | --- | --- |
| `annotationSeq` | `annotations` | 原始 `AnnotationSeq` 整体 |
| `throwOnFirstError` | `chiselOptions.throwOnFirstError` | `ChiselOptions` |
| `useLegacyWidth` | `chiselOptions.useLegacyWidth` | `ChiselOptions` |
| `includeUtilMetadata` | `chiselOptions.includeUtilMetadata` | `ChiselOptions` |
| `useSRAMBlackbox` | `chiselOptions.useSRAMBlackbox` | `ChiselOptions` |
| `warningFilters` | `chiselOptions.warningFilters` | `ChiselOptions` |
| `sourceRoots` | `chiselOptions.sourceRoots` | `ChiselOptions` |
| `defaultNamespace` | `None` | `Elaborate` 写死为 `None` |
| `loggerOptions` | `loggerOptions` | `view[LoggerOptions]` |
| `definitions` | 空 `LinkedHashSet` | `Elaborate` 新建空集 |
| `contextCache` | `BuilderContextCache.empty` | `Elaborate` 给空缓存 |
| `layerMap` | `chiselOptions.layerMap` | `ChiselOptions` |
| `inlineTestIncluder` | `chiselOptions.inlineTestIncluder` | `ChiselOptions` |
| `suppressSourceInfo` | `chiselOptions.suppressSourceInfo` | `ChiselOptions` |
| `elideLayerBlocks` | `false` | `Elaborate` 写死为 `false` |
| `elaborationTrace` | `new ElaborationTrace` | `Elaborate` 新建 |

**结论**：9 个 `ChiselOptions` 字段（`throwOnFirstError`、`useLegacyWidth`、`includeUtilMetadata`、`useSRAMBlackbox`、`warningFilters`、`sourceRoots`、`layerMap`、`inlineTestIncluder`、`suppressSourceInfo`）直接流入 `DynamicContext`；另有 `loggerOptions` 来自独立的 `LoggerOptions` 投影；剩余几个字段（`defaultNamespace`、`definitions`、`contextCache`、`elideLayerBlocks`、`elaborationTrace`）由 `Elaborate` 填默认值。

> 注意：`ChiselOptions` 里的 `printFullStackTrace` 和 `outputFile` **没有**进入 `DynamicContext`。前者在 4.4 的异常处理里被 `Elaborate` 自己读取（控制是否裁剪堆栈），后者属于输出阶段（`Emitter`）关心的配置，与 elaboration 运行账本无关。这正好说明：`ChiselOptions` 是「全集」，`DynamicContext` 只取「elaboration 需要的子集」。

配置的「上游」（注解 → `ChiselOptions`）可以在 [package.scala:18-45](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/package.scala#L18-L45) 看到，例如：

```scala
implicit object ChiselOptionsView extends OptionsView[ChiselOptions] {
  def view(options: AnnotationSeq): ChiselOptions = options.collect { case a: ChiselOption => a }
    .foldLeft(new ChiselOptions()) { (c, x) =>
      x match {
        case ThrowOnFirstErrorAnnotation   => c.copy(throwOnFirstError = true)
        case UseLegacyWidthBehavior        => c.copy(useLegacyWidth = true)
        case UseSRAMBlackbox               => c.copy(useSRAMBlackbox = true)
        ...
      }
    }
}
```

`ElaborationTrace` 是一个可选的模块级耗时追踪器（用于生成火焰图），它只认 `chisel.trace.file` 系统属性或 `CHISEL_TRACE_FILE` 环境变量；未设置时它的 `pushModule`/`popModule`/`finish` 都是空操作（见 [ElaborationTrace.scala:39-82](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/ElaborationTrace.scala#L39-L82)）。`Elaborate` 在 `Builder.build` 之后调用 `elaborationTrace.finish()`（[Elaborate.scala:67](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala#L67)）写出追踪文件。

#### 4.3.4 代码实践（本讲核心实践任务）

**实践目标**：在源码里亲手把「`ChiselOptions` 字段 → `DynamicContext` 构造参数」的对应关系走一遍，验证 4.3.3 的表格。

**操作步骤**：

1. 打开 [ChiselOptions.scala:11-24](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/ChiselOptions.scala#L11-L24)，列出 `ChiselOptions` 的全部字段。
2. 打开 [Elaborate.scala:44-63](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala#L44-L63)，逐行把每个实参对到 `DynamicContext` 的构造参数（参考 [Builder.scala:465-483](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L465-L483) 的形参顺序）。
3. 圈出哪些 `ChiselOptions` 字段**没有**出现在 `new DynamicContext(...)` 里。

**需要观察的现象**：按位置传参时，第 N 个实参恰好对应构造器第 N 个形参；`printFullStackTrace` 和 `outputFile` 找不到对应形参。

**预期结果**：你应得到与 4.3.3 表格一致的结论——9 个 `ChiselOptions` 字段流入 `DynamicContext`，`printFullStackTrace` 与 `outputFile` 不流入。

**如果无法确定运行结果**：本实践为纯源码阅读，无需运行；结论可直接从三处源码对照得出。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `defaultNamespace` 由 `Elaborate` 写死为 `None`，而不像其它字段那样从 `ChiselOptions` 读？

**参考答案**：`defaultNamespace` 用于复用一个已存在的全局命名空间（例如跨多次 elaboration 共享名字去重器），而一次独立的顶层 elaboration 应当用全新的命名空间。`DynamicContext` 内部对 `None` 的处理是 `defaultNamespace.getOrElse(Namespace.empty)`（[Builder.scala:514](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L514)），即「不指定就用空命名空间」。所以 `Elaborate` 传 `None` 表示「这次从头开始命名」。

**练习 2**：`loggerOptions` 为什么不和 `chiselOptions` 合并成一个配置对象？

**参考答案**：因为日志（`logger`）是独立的库，有自己的选项体系和 `LoggerOptionsView`。Chisel 只是「借用」日志系统，在 elaboration 期间需要把日志选项也带进上下文（`Builder.build` 会用它建立日志作用域）。保持两个独立投影，避免 Chisel 与 logger 的配置耦合。

---

### 4.4 Builder.build 与 ChiselCircuitAnnotation：执行与产出

#### 4.4.1 概念说明

`DynamicContext` 只是账本，真正「按下启动键」的是 `Builder.build`。它做两件事：**先把 context 绑定为当前上下文，再执行模块构造体**——顺序绝不能反，因为构造体里的每条 API 都要往「当前 context」里登记。跑完后，它把收集到的命令收拢成内部 IR `Circuit`，包成 `ElaboratedCircuit` 返回。`Elaborate` 再把这个返回值包成 `ChiselCircuitAnnotation`，送回管道。同时，整个执行包在 `try/catch` 里——elaboration 出错时，Chisel 会裁剪堆栈，让报错只显示用户代码部分。

#### 4.4.2 核心流程

`Builder.build` 的执行顺序（关键：**先绑定后执行**）：

1. `Builder.build(f, context)` 先用 `logger.Logger.makeScope(loggerOptions)` 建立日志作用域，再调 `buildImpl`。
2. `buildImpl` 用 `dynamicContextVar.withValue(Some(context)) { ... }` 把 context 绑定为当前上下文。
3. 在该绑定**作用域内**，求值 by-name 参数 `f`（即 `Module(gen())`）——此时模块构造体执行，命令被登记进 context。
4. 调 `errors.checkpoint(logger)` 把收集到的错误/警告真正打印出来。
5. 把 `components`、层邻接表、选项、域等组装成一个内部 IR `Circuit`。
6. 返回 `(ElaboratedCircuit(circuit, annotations), mod)`。

「先绑定后执行」之所以成立，依赖一个 Scala 关键特性：**by-name 参数**。`build` 与 `buildImpl` 的第一个参数 `f: => T` 是按名调用的——`Module(gen())` 不是在调用 `build` 时立刻求值，而是推迟到 `withValue` 代码块内部才求值。可用一个等式表达这个时序：

\[
  \text{bind}(context) \;\longrightarrow\; \text{eval}(\textit{Module}(\textit{gen}())) \;\longrightarrow\; \text{checkpoint} \;\longrightarrow\; \text{assemble}(\textit{Circuit})
\]

#### 4.4.3 源码精读

`Elaborate` 里的调用点：

```scala
val (elaboratedCircuit, dut) = {
  Builder.build(Module(gen()), context)
}
elaborationTrace.finish()
```

见 [src/main/scala/chisel3/stage/phases/Elaborate.scala:64-67](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala#L64-L67)。注意 `Module(gen())` 作为 by-name 实参传入，**此刻并未执行**。

`Builder.build` 与 `buildImpl`：

```scala
private[chisel3] def build[T <: BaseModule](
  f:              => T,
  dynamicContext: DynamicContext
): (ElaboratedCircuit, T) = {
  _root_.logger.Logger.makeScope(dynamicContext.loggerOptions) {
    buildImpl(f, dynamicContext)
  }
}

private def buildImpl[T <: BaseModule](
  f:              => T,
  dynamicContext: DynamicContext
): (ElaboratedCircuit, T) = {
  dynamicContextVar.withValue(Some(dynamicContext)) {
    ...
    val m = f            // 此时才真正执行 Module(gen())
    ...
    errors.checkpoint(logger)
    ...
    val circuit = Circuit(...)
    (ElaboratedCircuit(circuit, dynamicContext.annotationSeq.toSeq), mod)
  }
}
```

见 [core/src/main/scala/chisel3/internal/Builder.scala:1054-1062](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L1054-L1062) 与 [第 1064-1077 行](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L1064-L1077)。

- `f: => T`：by-name 参数，是「先绑定后执行」能成立的关键。
- `dynamicContextVar.withValue(Some(dynamicContext)) { ... }`：把 `DynamicContext` 绑定为全局可隐式访问的「当前上下文」。此后模块构造体里所有 `Builder.pushCommand`、`Builder.currentModule` 等访问器都转发到这个 context（见 u4-l1）。
- `errors.checkpoint(logger)`：elaboration 期间 `Builder.error` 收集的错误并不会立即抛出，而是累积在 `ErrorLog` 里；`checkpoint` 在此统一打印（见 [Error.scala:345-357](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Error.scala#L345-L357)）。这就是 Chisel 能「一次报告多条错误」的原因。
- 末尾 `(ElaboratedCircuit(circuit, ...), mod)`：把内部 IR `Circuit`（见 u4-l2）封装成 `ElaboratedCircuit`，连同顶层模块引用一起返回。

> 为什么返回的是 `ElaboratedCircuit` 而不是直接返回内部 `Circuit`？因为 `Circuit` 是 `chisel3.internal.firrtl.ir` 下的私有 IR，`ElaboratedCircuit` 是它的对外封装（把 `_circuit` 藏在 `private[chisel3]` 之后），其文本序列化走 `Serializer`（见 u4-l4）。这样公开 API 不暴露内部 IR 细节。

回到 `Elaborate`，拿到 `elaboratedCircuit` 后产出两条注解：

```scala
Seq(
  ChiselCircuitAnnotation(elaboratedCircuit),
  DesignAnnotation(dut, layers = elaboratedCircuit._circuit.layers.flatMap(walkLayers(_)))
)
```

见 [src/main/scala/chisel3/stage/phases/Elaborate.scala:74-77](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala#L74-L77)。

- `ChiselCircuitAnnotation(elaboratedCircuit)`：把电路包回注解，交给下游 `Convert`/`Emitter`（见 u5-l3）。`ChiselCircuitAnnotation` 本身只是 `ElaboratedCircuit` 的注解外壳，且为了对大电路避免重复 `hashCode` 计算做了缓存（见 [ChiselAnnotations.scala:300-324](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/ChiselAnnotations.scala#L300-L324)）。
- `DesignAnnotation(dut, layers = ...)`：携带**顶层模块的对象引用**（`dut`）和电路里用到的层列表，供后续需要直接访问 Scala 对象的阶段使用。

**异常处理与堆栈裁剪**。整个 elaboration 包在 `try/catch` 里：

```scala
} catch {
  case scala.util.control.NonFatal(a) =>
    if (!chiselOptions.printFullStackTrace) {
      a.trimStackTraceToUserCode()
    }
    throw (a)
}
```

见 [src/main/scala/chisel3/stage/phases/Elaborate.scala:78-86](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala#L78-L86)。

- `NonFatal(a)`：只捕获「非致命」异常（排除 `OutOfMemoryError`、`StackOverflowError` 等虚拟机错误，这些应原样上抛）。
- `chiselOptions.printFullStackTrace`：这就是 4.3 提到「没进 `DynamicContext`」的那个字段——它在 `Elaborate` 自己手里，用来决定是否裁剪堆栈。
- `a.trimStackTraceToUserCode()`：原地修改异常的堆栈，裁掉 Chisel/Scala/Java 内部帧，只留用户代码帧。
- `throw (a)`：裁剪后再抛出，让上层（管道/CLI）决定如何终止。

`trimStackTraceToUserCode` 的裁剪依据是一个「包名黑名单」和一个「锚点」：

```scala
final val packageTrimlist: Set[String] = Set("chisel3", "scala", "java", "jdk", "sun", "sbt")
final val builderName: String = chisel3.internal.Builder.getClass.getName
```

见 [core/src/main/scala/chisel3/internal/Error.scala:18-24](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Error.scala#L18-L24)。裁剪逻辑（[Error.scala:75-98](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Error.scala#L75-L98)）大致是：从栈顶起删掉所有属于黑名单包的帧、从栈底起删到锚点（`Builder`）为止、再删掉锚点之后的黑名单帧，最后插入省略号 `...` 帧并附上「可用 `--full-stacktrace` 看完整堆栈」的提示。这就是你在 Chisel 报错里通常只看到**你自己代码**那一两行的原因。

#### 4.4.4 代码实践

**实践目标**：亲手触发一次 elaboration 异常，观察「堆栈裁剪」前后差异。

**操作步骤**（示例代码，可在 `scala>` REPL 或测试里运行；如无运行环境则标注为待本地验证）：

1. 写一个故意出错的小模块（例如把两个 `Output` 端口用 `:=` 连接，这会被 `MonoConnect` 拒绝，见 u3-l4）：

```scala
// 示例代码
import chisel3._
import chisel3.stage.ChiselGeneratorAnnotation

class BadMod extends RawModule {
  val a = IO(Output(Bool()))
  val b = IO(Output(Bool()))
  a := b   // 两个 Output 之间单向连接，方向冲突
}

ChiselGeneratorAnnotation(() => new BadMod).elaborate
```

2. 运行上述代码，记录报错的堆栈。
3. 改用 `--full-stacktrace`（或对应注解 `PrintFullStackTraceAnnotation`）再跑一次，对比堆栈长度。

**需要观察的现象**：默认模式下，堆栈里几乎看不到 `chisel3.*`、`scala.*` 的帧，错误指向用户模块 `BadMod` 的 `a := b` 行；加上 `PrintFullStackTraceAnnotation` 后，堆栈会包含大量 Chisel/Scala 内部帧。

**预期结果**：验证 `trimStackTraceToUserCode` 的效果——默认裁剪、加注解后不裁剪。

**待本地验证**：具体错误信息文本与堆栈帧数依 Chisel 版本与运行环境而定，请以本地实际输出为准。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `Builder.build` 的第一个参数 `f: => T` 改成普通按值参数 `f: T`，会出什么问题？

**参考答案**：按值参数会在**调用 `build` 之前**就求值 `Module(gen())`，而那时 `dynamicContextVar` 还没被 `withValue` 绑定，模块构造体里的 `Builder.pushCommand` 等会读到「没有当前上下文」的状态（`dynamicContext` 访问器会抛 `require` 失败「must be inside Builder context」）。by-name 参数正是为了把构造推迟到绑定之后。

**练习 2**：为什么 `Builder.build` 先用 `Logger.makeScope` 包一层，再用 `withValue` 绑定 context？

**参考答案**：日志系统（`logger` 库）有自己的、独立于 Chisel 的动态上下文。`makeScope(loggerOptions)` 先把日志选项注入日志系统的上下文，随后在 `buildImpl` 里再绑定 Chisel 的 `DynamicContext`。这样模块构造体里既能访问 Chisel 上下文，也能让 `logger` 正确工作——两个上下文互不干扰。

**练习 3**：`errors.checkpoint(logger)` 为什么放在 `val m = f` 之后、组装 `Circuit` 之前？

**参考答案**：`checkpoint` 把 elaboration 期间累积的错误真正打印出来。它放在构造体执行（`f`）之后，确保所有错误都已收集；放在组装 `Circuit` 之前，是因为若已有致命错误，继续组装可能产生噪声。此外，`f` 的 `catch` 分支里也会调一次 `errors.checkpoint`（[Builder.scala:1086-1091](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L1086-L1091)），保证即使构造体抛异常，已收集的错误也不会丢失。

---

## 5. 综合实践

本实践把四个最小模块串起来：用 `ChiselGeneratorAnnotation.elaborate` **单独跑 `Elaborate` 阶段**，拿到 `ChiselCircuitAnnotation`，并打印出内部 CHIRRTL 文本，从而验证「注解 → 配置 → context → 电路注解」的完整链路。

**实践目标**：绕开完整 Phase 管道，只跑 elaboration，亲手看到 `Elaborate` 的产出。

**操作步骤**（示例代码）：

```scala
// 示例代码
import chisel3._
import chisel3.stage.{ChiselGeneratorAnnotation, ChiselCircuitAnnotation}

class Adder extends Module {
  val io = IO(new Bundle {
    val a = Input(UInt(8.W))
    val b = Input(UInt(8.W))
    val y = Output(UInt(8.W))
  })
  io.y := io.a + io.b
}

// 1) 用 ChiselGeneratorAnnotation 携带生成器函数
// 2) .elaborate 内部 new 一个 Elaborate 并 transform，跑出注解序列
val annos: firrtl.annotations.AnnotationSeq =
  ChiselGeneratorAnnotation(() => new Adder).elaborate

// 3) 从产出注解里捞出 ChiselCircuitAnnotation，取出 ElaboratedCircuit
val circuit = annos.collectFirst { case ChiselCircuitAnnotation(c) => c }.get

// 4) 序列化成 CHIRRTL 文本（走 Serializer，见 u4-l4）
circuit.serialize.foreach(println)
```

**需要观察的现象**：

- 步骤 2 返回的 `annos` 里应包含一条 `ChiselCircuitAnnotation` 和一条 `DesignAnnotation`（对应 [Elaborate.scala:74-77](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala#L74-L77) 的两条产出）。
- 步骤 4 打印的 CHIRRTL 文本里应能看到 `module Adder`、输入端口 `a`/`b`、输出端口 `y`，以及一个表示 `a + b` 的 `add` 原语节点。

**预期结果**：你看到的 CHIRRTL 大致形如：

```
circuit Adder :
  module Adder :
    input clock : Clock
    input reset : Reset
    ...
    input a : UInt<8>
    input b : UInt<8>
    output y : UInt<8>
    ...
    node _y_T = add(a, b)
    ...
```

**延伸任务**（可选）：

1. 在 `new DynamicContext(...)` 处对应（见 4.3.3 表格），解释为什么这个 `Adder` 的 elaboration 用的是默认配置（因为没有往 `annos` 里加入任何 `ChiselOption` 注解，`view[ChiselOptions]` 返回全默认的 `ChiselOptions`）。
2. 在 `annos` 里追加 `chisel3.stage.ThrowOnFirstErrorAnnotation`，观察 `DynamicContext.throwOnFirstError` 是否随之变为 `true`（验证「注解 → `ChiselOptions` → `DynamicContext`」链路可被配置）。

**待本地验证**：实际 CHIRRTL 文本会随 Chisel 版本（尤其是寄存器、reset、随机化等默认插入行为）有所变化，请以本地 `emitCHIRRTL`/`serialize` 的实际输出为准。

## 6. 本讲小结

- `Elaborate` 是一个 `Phase`，在 `transform` 里用 `flatMap` 把 `ChiselGeneratorAnnotation` 替换成 `ChiselCircuitAnnotation`（外加 `DesignAnnotation`），其余注解原样透传。
- `ChiselGeneratorAnnotation` 携带的是「`() => RawModule` 生成器函数」而非已构造的电路，从而把构造推迟到 `Elaborate`、并保证它在正确的 `DynamicContext` 内执行。
- `DynamicContext` 的字段来自三处：9 个直接取自 `view[ChiselOptions]`（`throwOnFirstError`/`useLegacyWidth`/`includeUtilMetadata`/`useSRAMBlackbox`/`warningFilters`/`sourceRoots`/`layerMap`/`inlineTestIncluder`/`suppressSourceInfo`），1 个取自 `view[LoggerOptions]`，其余由 `Elaborate` 填默认值；`printFullStackTrace`/`outputFile` 不进入 context。
- `Builder.build` 依赖 by-name 参数实现「先绑定 `DynamicContext`、后执行 `Module(gen())`」的关键时序；跑完后用 `errors.checkpoint` 统一打印累积错误，再把内部 `Circuit` 封装成 `ElaboratedCircuit` 返回。
- elaboration 异常被 `NonFatal` 捕获，默认经 `trimStackTraceToUserCode` 裁剪掉 `chisel3`/`scala`/`java` 等内部帧，只留用户代码；`PrintFullStackTraceAnnotation` 可关掉裁剪。

## 7. 下一步学习建议

本讲讲清了「`Elaborate` 如何产出 `ChiselCircuitAnnotation`」。沿着管道往下：

- **u5-l3（Convert / Checks / Emitter 阶段）**：看 `ChiselCircuitAnnotation` 如何被 `Convert` 阶段消费、调用 `Converter`（见 u4-l5）转成 `firrtl.ir.Circuit`，再由 `Emitter` 产出中间文件。这是本讲的直接下游。
- **u4-l5（Converter）**：若你想深入理解 `ElaboratedCircuit` 里的内部 IR 与 FIRRTL IR 的对应关系，可回到该讲对照 `convert` 方法的逐节点映射。
- **u9-l2（错误处理与 ElaborationTrace）**：若你对 `errors.checkpoint`、`ErrorLog`、`trimStackTraceToUserCode` 的细节感兴趣，该讲会系统讲解错误收集与堆栈裁剪机制。
- 继续阅读 [Elaborate.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala) 全文，并结合 [Builder.scala 的 buildImpl](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L1064-L1178) 追踪 `components` 是如何从空 `ArrayBuffer` 被模块构造体逐步填满、最终组装成 `Circuit` 的。
