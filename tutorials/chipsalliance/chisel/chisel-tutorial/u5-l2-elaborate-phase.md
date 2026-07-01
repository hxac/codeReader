# Elaborate 阶段

## 1. 本讲目标

在 [u5-l1](u5-l1-stage-phase-pipeline.md) 里，我们把 Chisel 编译看成一条由 `PhaseManager` 串联的 `Phase` 管道，并用 `DependencyAPI` 解释了阶段之间如何靠注解声明依赖。本讲要钻进这条管道里**第一个真正「长出电路」的阶段**——`chisel3.stage.phases.Elaborate`。

学完本讲，你应当能够：

1. 说清 `Elaborate.transform` 如何从一串注解里捞出 `ChiselGeneratorAnnotation`，并把其中的 Scala 生成器函数「跑」成一个电路，再产出 `ChiselCircuitAnnotation` 与 `DesignAnnotation`。
2. 对照源码，列出 `ChiselOptions`（以及 `LoggerOptions`）里的哪些字段被读出、又分别填进了 `DynamicContext` 的哪一个构造参数——即**配置如何从命令行注解流入一次 elaboration 的运行账本**。
3. 解释 `Builder.build` 内部「先绑定上下文、后执行构造体」的顺序为何依赖 by-name 参数，以及它如何收口产出 `ElaboratedCircuit`。
4. 理解 elaboration 抛错时，Chisel 为什么能把冗长的 Scala/Java 内部堆栈裁剪成「只剩用户代码」的精简堆栈。

本讲是 [u4-l1](u4-l1-builder-global-state.md)（Builder 全局状态机）的下游：u4-l1 讲了 `DynamicContext`/`Builder` 在 elaboration 期间**维护**什么状态，本讲讲**谁创建**这个 `DynamicContext`、**何时**把它交给 `Builder`。

## 2. 前置知识

进入源码前，先用最直白的话把几个概念对齐：

- **elaboration（细化）**：运行你写在 `Module` 构造体里的 Scala 代码，让 `IO`/`Reg`/`when` 这些 API「长出」一棵电路对象树。前端 API「只登记不施工」，登记的命令最终被收拢成内部 IR（见 [u4-l2](u4-l2-internal-firrtl-ir.md)）。
- **Annotation（注解）/ AnnotationSeq**：贯穿整条 Phase 管道的唯一数据载体。一个注解是一条「带类型的信息」，可以是配置开关、生成的电路、输出文件路径等。`AnnotationSeq` 就是注解的列表，每个 `Phase` 读它、改它、再传给下一个 `Phase`（见 [u5-l1](u5-l1-stage-phase-pipeline.md)）。
- **Phase**：一个 \(\;f : \text{AnnotationSeq} \to \text{AnnotationSeq}\;\) 的纯函数。`Elaborate` 就是一个 `Phase`，它的职责是「输入里若有 `ChiselGeneratorAnnotation`，就跑 elaboration，把结果替换成 `ChiselCircuitAnnotation`」。
- **OptionsView（配置投影）**：注解是「扁平」的一条条记录，而代码里更愿意读一个「结构化的配置对象」。`OptionsView` 就是把一串注解 `fold` 成一个 case-class 风格配置对象的工具。本讲里 `view[ChiselOptions](annotations)` 干的就是这件事。
- **DynamicVariable**：Scala 标准库的「动态作用域变量」。`dynamicContextVar.withValue(某个值){ 代码块 }` 让代码块内对该变量的读取都返回「某个值」，离开后自动恢复。Chisel 用它隐式地把「当前这次 elaboration 的上下文」传递给深层的 API 调用（见 [u4-l1](u4-l1-builder-global-state.md)）。

一句话串起本讲：**命令行注解 → 配置投影 `ChiselOptions` → `DynamicContext` → `Builder.build` → 电路注解**。这就是 `Elaborate` 阶段做的事。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/main/scala/chisel3/stage/phases/Elaborate.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala) | 本讲主角。定义 `class Elaborate extends Phase`，在 `transform` 里创建 `DynamicContext`、调用 `Builder.build`、捕获并裁剪异常。 |
| [src/main/scala/chisel3/stage/ChiselOptions.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/ChiselOptions.scala) | 结构化的 Chisel 配置对象，列出所有可配置开关，是 `DynamicContext` 字段的来源。 |
| [src/main/scala/chisel3/stage/package.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/package.scala) | 定义 `ChiselOptionsView`，说明每条命令行注解如何映射成 `ChiselOptions` 的某个字段（配置的「上游」）。 |
| [src/main/scala/chisel3/stage/ChiselAnnotations.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/ChiselAnnotations.scala) | 定义 `ChiselGeneratorAnnotation`（输入）、`ChiselCircuitAnnotation`（输出）、`DesignAnnotation`。 |
| [core/src/main/scala/chisel3/internal/Builder.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala) | 定义 `DynamicContext`（一次 elaboration 的运行账本）和 `Builder.build`/`buildImpl`（真正执行 elaboration、组装电路的入口）。 |
| [core/src/main/scala/chisel3/internal/Error.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Error.scala) | 提供 `trimStackTraceToUserCode`，把异常堆栈裁剪到只剩用户代码。 |
| [core/src/main/scala/chisel3/internal/ElaborationTrace.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/ElaborationTrace.scala) | 可选的模块级耗时追踪器，由 `Elaborate` 创建并注入 `DynamicContext`。 |

> 提示：`core` 子项目的 `internal` 包是私有实现，`src/main` 子项目的 `stage` 包是面向管道的整合层。`Elaborate` 属于后者，但它「伸手」调用了 `core` 的 `Builder`/`DynamicContext`。子项目划分见 [u1-l3](u1-l3-repository-layout.md)。

## 4. 核心概念与源码讲解

本讲按数据流把内容拆成四个最小模块：

- **4.1 ChiselGeneratorAnnotation**：输入——把「如何构造一个电路」装进注解。
- **4.2 Elaborate Phase**：触发器——管道里识别输入注解并启动 elaboration 的阶段（含异常与堆栈裁剪）。
- **4.3 DynamicContext**：账本——配置如何从 `ChiselOptions` 注入一次 elaboration 的运行状态。
- **4.4 Builder.build 与产出注解**：执行与产出——真正跑构造体、组装电路、产出 `ChiselCircuitAnnotation`。

### 4.1 ChiselGeneratorAnnotation：把「如何构造电路」装进注解

#### 4.1.1 概念说明

Phase 管道里流动的是注解，但「一个电路」本身不是一个静态数据——它需要**被执行**才能长出来。于是 Chisel 用了一个巧妙的设计：把「构造电路的方法」——一个返回 `RawModule` 的**零参函数**（generator，生成器）——塞进一条注解里，让它随管道流动。到了 `Elaborate` 阶段，再把这个函数「调用一次」，电路就长出来了。

这就是 `ChiselGeneratorAnnotation`：它不存电路，它存「**怎么造电路**」。它带了两个 mixin 标记：`NoTargetAnnotation`（不绑定到任何电路节点）和 `Unserializable`（不可序列化——函数无法序列化），所以它只活在内存的注解流中，永远不会被写到磁盘的注解文件。

#### 4.1.2 核心流程

`ChiselGeneratorAnnotation` 有两条产生路径：

```text
① 库 API：ChiselGeneratorAnnotation(() => new MyMod)        // 直接塞生成器函数
② 命令行：--module <全限定类名> ──反射──► ChiselGeneratorAnnotation(gen)
        │
        ├── .elaborate ─► (new Elaborate).transform(Seq(this))   // 单步捷径
        └── 进入完整 PhaseManager 管道 ─► Elaborate.transform(全部注解)
```

注意「零参函数」的语义：注解携带的是**延迟计算**。电路在注解流到 `Elaborate` 之前并不会被构造，这与「只登记不施工」一脉相承。

#### 4.1.3 源码精读

`ChiselGeneratorAnnotation` 的定义只有一个字段——生成器函数（[src/main/scala/chisel3/stage/ChiselAnnotations.scala:L218-L223](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/ChiselAnnotations.scala#L218-L223)）：

```scala
case class ChiselGeneratorAnnotation(gen: () => RawModule) extends NoTargetAnnotation with Unserializable {

  /** Run elaboration on the Chisel module generator function stored by this [[firrtl.annotations.Annotation]] */
  def elaborate: AnnotationSeq = (new chisel3.stage.phases.Elaborate).transform(Seq(this))
}
```

两个细节：

- `gen: () => RawModule`：类型是一个**无参函数值**，调用它（`gen()`）才返回模块。`RawModule` 是不带隐式 clock/reset 的模块基类（见 [u3-l1](u3-l1-module-rawmodule.md)）。
- `def elaborate`：这是 `Elaborate` 阶段的一个**独立入口**——直接 `new` 一个 `Elaborate` 并对其 `transform` 喂入只含自己的注解序列，让你**绕开完整管道、单独跑 elaboration**（4.4 综合实践会用到它）。

命令行路径由伴生 `object` 用反射把类名字符串变成生成器函数，并注册 `--module` 选项（[src/main/scala/chisel3/stage/ChiselAnnotations.scala:L286-L293](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/ChiselAnnotations.scala#L286-L293)）：

```scala
val options = Seq(
  new ShellOption[String](
    longOption = "module",
    toAnnotationSeq = (a: String) => Seq(ChiselGeneratorAnnotation(a)),
    helpText = "The name of a Chisel module to elaborate (module must be in the classpath)",
    helpValueName = Some("<package>.<module>")
  )
)
```

`apply(name: String)`（[src/main/scala/chisel3/stage/ChiselAnnotations.scala:L255-L284](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/ChiselAnnotations.scala#L255-L284)）内部用反射 `constructor.newInstance(...)` 构造模块，并把反射失败翻译成更友好的 `OptionsException`（如「类找不到」「不是 RawModule」），最终也产出 `ChiselGeneratorAnnotation(gen)`。

#### 4.1.4 代码实践

**实践目标**：确认 `ChiselGeneratorAnnotation` 携带的是一个「待执行」的函数，而非已构造的电路，并用 `.elaborate` 单步触发 elaboration。

**操作步骤**（示例代码，可在 `./mill -i chisel[].console` 或测试里运行）：

```scala
// 示例代码
import chisel3._
import chisel3.stage.ChiselGeneratorAnnotation

class Adder extends Module {
  val io = IO(new Bundle {
    val a = Input(UInt(8.W))
    val b = Input(UInt(8.W))
    val y = Output(UInt(8.W))
  })
  io.y := io.a + io.b
}

// 携带的是 () => new Adder，而非已构造的 Adder 实例
val annos = ChiselGeneratorAnnotation(() => new Adder).elaborate
println(annos.map(_.getClass.getSimpleName).mkString(", "))
```

**需要观察的现象**：传入的是一个 lambda，模块 `new Adder` 的构造在 `.elaborate` 被调用时才发生；打印出的注解类型里应包含 `ChiselCircuitAnnotation` 与 `DesignAnnotation`——这正是下一节 `Elaborate` 的产出。

**预期结果**：输出形如 `ChiselCircuitAnnotation, DesignAnnotation`（顺序与条数以本地实际为准）。本例为示例代码，具体输出「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ChiselGeneratorAnnotation` 要存「函数」而不是直接存「已经构造好的 `RawModule` 对象」？

**参考答案**：因为 elaboration 必须在 `Builder.build` 绑定的 `DynamicContext` 内执行，模块构造体里的每一条 API 调用都要登记到「当前上下文」。如果提前在注解创建时就构造好模块，那些命令就登记到了一个还不存在的上下文里。把构造延迟到 `Elaborate` 阶段、并在 `Builder.build` 内部调用 `gen()`，才能保证登记发生在正确的上下文中。

**练习 2**：`def elaborate` 这一行的入参为什么是 `Seq(this)`？

**参考答案**：`transform` 的入参是整个 `AnnotationSeq`。独立调用时，把「只含本注解」的序列传进去，`transform` 会识别出这条 `ChiselGeneratorAnnotation` 并执行 elaboration，其余（不匹配的）注解原样透传。

### 4.2 Elaborate Phase：管道里的 elaboration 触发器

#### 4.2.1 概念说明

`Elaborate` 是一个标准的 `Phase`：它声明自己的前置依赖，提供一个 `transform` 方法。它的核心使命只有一句话——**遍历注解，遇到 `ChiselGeneratorAnnotation` 就跑 elaboration，把结果替换成 `ChiselCircuitAnnotation` + `DesignAnnotation`；其它注解原样保留**。它是整条管道里第一个「产生硬件对象树」的阶段。这种「编排（在 stage 层搭台）」与「执行（在 internal 层由 Builder 干活）」的分层，正是 Chisel 把 `stage` 与 `internal` 分开的体现。

#### 4.2.2 核心流程

`Elaborate.transform` 的执行过程（伪代码）：

```text
transform(annotations) =
  annotations.flatMap {
    case ChiselGeneratorAnnotation(gen) =>
      chiselOptions = view[ChiselOptions](annotations)   // 投影全局配置
      loggerOptions = view[LoggerOptions](annotations)
      try {
        elaborationTrace = new ElaborationTrace
        context         = new DynamicContext(... 各配置字段 ...)
        (circuit, dut)  = Builder.build(Module(gen()), context)   // 真正长出电路
        elaborationTrace.finish()
        Seq(ChiselCircuitAnnotation(circuit), DesignAnnotation(dut, layers = ...))
      } catch {
        case NonFatal(e) =>
          if (!chiselOptions.printFullStackTrace) e.trimStackTraceToUserCode()
          throw e
      }
    case a => Some(a)   // 其它注解原样保留
  }
```

要点：

1. 用 `flatMap` 逐条处理注解，因为一条生成器注解要展开成**两条**产出注解。
2. 命中生成器注解时，用 `view[ChiselOptions](annotations)` 把**整条**注解投影成全局配置（详见 4.3）。
3. 整个 elaboration 包在 `try/catch` 里，捕获 `NonFatal` 异常并按需裁剪堆栈。

#### 4.2.3 源码精读

`Elaborate` 的依赖声明（这是 [u5-l1](u5-l1-stage-phase-pipeline.md) 讲的 `DependencyAPI`），见 [src/main/scala/chisel3/stage/phases/Elaborate.scala:L29-L37](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala#L29-L37)：

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

它声明**硬前提**是 `Checks`（Chisel 侧，注解合法性检查）和 `logger.phases.Checks`（日志侧）——即这两个校验阶段必须先跑。注意这里的 `Checks` 是「注解检查」，不是 elaboration 后的电路合法性检查。`invalidates(a) = false` 表示它不撤销任何其它阶段的效果（只往注解流「加料」）。

`transform` 的主骨架（[src/main/scala/chisel3/stage/phases/Elaborate.scala:L39-L88](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala#L39-L88)）：

```scala
def transform(annotations: AnnotationSeq): AnnotationSeq = annotations.flatMap {
  case ChiselGeneratorAnnotation(gen) =>
    val chiselOptions = view[ChiselOptions](annotations)
    val loggerOptions = view[LoggerOptions](annotations)
    try {
      val elaborationTrace = new ElaborationTrace
      val context = new DynamicContext(/* 见 4.3 */)
      val (elaboratedCircuit, dut) = {
        Builder.build(Module(gen()), context)
      }
      elaborationTrace.finish()
      // ... walkLayers ...
      Seq(
        ChiselCircuitAnnotation(elaboratedCircuit),
        DesignAnnotation(dut, layers = elaboratedCircuit._circuit.layers.flatMap(walkLayers(_)))
      )
    } catch {
      case scala.util.control.NonFatal(a) =>
        if (!chiselOptions.printFullStackTrace) {
          a.trimStackTraceToUserCode()
        }
        throw (a)
    }
  case a => Some(a)
}
```

几个要点：

- `view[ChiselOptions](annotations)` 用 `OptionsView`（[src/main/scala/chisel3/stage/package.scala:L18-L45](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/package.scala#L18-L45)）把注解折叠成 `ChiselOptions`——这是「命令行选项 → 结构化配置」的投影点。注意它在**每个**生成器注解上都重新投影**整条** `annotations`，因为配置是「全局」的。
- `Builder.build(Module(gen()), context)` 是唯一的细化触发点（详见 4.4）。`Module(gen())` 用工厂包装把 `RawModule` 包成模块（见 [u3-l1](u3-l1-module-rawmodule.md)）。
- `walkLayers`（[Elaborate.scala:L70-L72](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala#L70-L72)）用一次遍历把电路里声明的 `ir.Layer` 树摊平成 `Seq[chisel3.layer.Layer]`，挂到 `DesignAnnotation` 上，供下游 layer 提取使用（详见 [u8-l3](u8-l3-layers-and-probe.md)）。
- 末尾的 `case a => Some(a)` 把非生成器注解原样透传——这正是 `flatMap` 的妙处：生成器注解被「展开替换」，其他注解不受影响。

异常处理（[src/main/scala/chisel3/stage/phases/Elaborate.scala:L78-L86](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala#L78-L86)）用 `NonFatal` 捕获所有非致命异常（排除 `OutOfMemoryError`、`StackOverflowError` 等虚拟机错误），在未开 `printFullStackTrace` 时先 `trimStackTraceToUserCode()` 再 `throw`。裁剪逻辑见 [core/src/main/scala/chisel3/internal/Error.scala:L75-L88](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Error.scala#L75-L88)：从栈顶删掉所有属于 `chisel3.*`/`scala.*`/`java.*` 等框架包的帧，只留用户代码帧，让你一眼看到「是我的哪一行报错」。

#### 4.2.4 代码实践

**实践目标**：感受「堆栈裁剪」开关的效果，理解 `printFullStackTrace` 这条命令行选项如何流到 `Elaborate` 的 catch 块。

**操作步骤**（示例代码）：

1. 写一个故意报错的模块（把 `UInt` 连到 `Bool`，类型不匹配）：

```scala
// 示例代码
import chisel3._
import chisel3.stage._
import chisel3.stage.phases.Elaborate

class Bad extends Module {
  val io = IO(new Bundle {
    val a = Input(UInt(8.W))
    val b = Output(Bool())
  })
  io.b := io.a   // 类型不匹配，触发 elaboration 错误
}

// 默认：裁剪堆栈
val annosTrim = Seq(ChiselGeneratorAnnotation(() => new Bad))
try { (new Elaborate).transform(annosTrim) }
catch { case e: Throwable => e.printStackTrace() }

// 开启完整堆栈
val annosFull = Seq(
  ChiselGeneratorAnnotation(() => new Bad),
  PrintFullStackTraceAnnotation
)
try { (new Elaborate).transform(annosFull) }
catch { case e: Throwable => e.printStackTrace() }
```

2. 对比两次的堆栈长度。

**需要观察的现象**：默认模式下堆栈里 `chisel3.internal.*`/`firrtl.*` 的帧被省略成 `...`，只留用户模块 `Bad` 的几帧；开启 `PrintFullStackTraceAnnotation` 后看到一长串框架内部帧。

**预期结果**：裁剪后的堆栈明显更短、且顶部指向用户代码。精确的省略行数与文本「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`Elaborate.transform` 用的是 `flatMap` 而非 `map`，为什么？

**参考答案**：因为一条 `ChiselGeneratorAnnotation` 要被展开成两条产物注解（`ChiselCircuitAnnotation` + `DesignAnnotation`），即「1 进 N 出」；`map` 只能 1 进 1 出。同时 `case a => Some(a)` 要求把其他注解原样保留为一个 `Option`，`flatMap` 恰好能统一处理 `Seq` 与 `Option`。

**练习 2**：为什么异常捕获用 `NonFatal` 而不是普通 `catch { case e: Exception }`？

**参考答案**：`NonFatal` 排除了 `VirtualMachineError`（如 `OutOfMemoryError`、`StackOverflowError`）等不应被框架吞掉或裁剪的致命错误。事实上 `Builder.buildImpl` 里还专门提前初始化了 `scala.util.control.NonFatal` 单例，正是为了避免在 OOM 时把错误报告成 `NoClassDefFoundError`（见 [u4-l1](u4-l1-builder-global-state.md)）。

### 4.3 DynamicContext：一次 elaboration 的运行账本

#### 4.3.1 概念说明

`DynamicContext`（[u4-l1](u4-l1-builder-global-state.md) 已介绍其内部状态）是「一次 elaboration 的账本」。它既持有贯穿细化的可变状态（`currentModule`、`components`、`errors` 等，定义在 `DynamicContext` 类体里），也有一批「配置开关」——后者正是从 `ChiselOptions` 一一搬过来的。理解这一节的关键是建立一条**配置流动链**：

```text
命令行/API 注解  ──ChiselOptionsView 折叠──►  ChiselOptions（结构化）
                                                   │
                                           Elaborate.transform 按位置搬字段
                                                   ▼
                                           DynamicContext（elaboration 账本）
                                                   │
                                           Builder 读取这些字段决定行为
```

`ChiselOptions` 是不可变的值对象（所有字段都是 `val`），把散落的命令行注解聚拢；`DynamicContext` 是可变的、运行期的账本。`Elaborate` 就是两者之间的搬运工。

#### 4.3.2 核心流程

`DynamicContext` 的主构造器没有用命名参数，所以 `Elaborate` 是按**位置**把字段一个个传进去的——顺序就是契约。下表给出 `new DynamicContext(...)`（[src/main/scala/chisel3/stage/phases/Elaborate.scala:L45-L63](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala#L45-L63)）每一项实参的来源，对照 `DynamicContext` 形参（[core/src/main/scala/chisel3/internal/Builder.scala:L465-L483](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L465-L483)）：

| # | DynamicContext 形参 | Elaborate 传入的实参 | 来自 |
| - | --- | --- | --- |
| 1 | `annotationSeq: AnnotationSeq` | `annotations` | 原始注解流（非 ChiselOptions 字段） |
| 2 | `throwOnFirstError: Boolean` | `chiselOptions.throwOnFirstError` | `ThrowOnFirstErrorAnnotation` |
| 3 | `useLegacyWidth: Boolean` | `chiselOptions.useLegacyWidth` | `UseLegacyWidthBehavior` |
| 4 | `includeUtilMetadata: Boolean` | `chiselOptions.includeUtilMetadata` | `IncludeUtilMetadata` |
| 5 | `useSRAMBlackbox: Boolean` | `chiselOptions.useSRAMBlackbox` | `UseSRAMBlackbox` |
| 6 | `warningFilters: Seq[WarningFilter]` | `chiselOptions.warningFilters` | `WarningsAsErrorsAnnotation`/`WarningConfiguration*` |
| 7 | `sourceRoots: Seq[File]` | `chiselOptions.sourceRoots` | `SourceRootAnnotation(s)` |
| 8 | `defaultNamespace: Option[Namespace]` | `None` | `Elaborate` 写死 |
| 9 | `loggerOptions: LoggerOptions` | `loggerOptions` | `view[LoggerOptions]`（日志库） |
| 10 | `definitions: LinkedHashSet[Definition[...]]` | 空 `LinkedHashSet` | `Elaborate` 新建空集 |
| 11 | `contextCache: BuilderContextCache` | `BuilderContextCache.empty` | `Elaborate` 给空缓存 |
| 12 | `layerMap: Map[Layer, Layer]` | `chiselOptions.layerMap` | `RemapLayer(old, new)` |
| 13 | `inlineTestIncluder: InlineTestIncluder` | `chiselOptions.inlineTestIncluder` | `IncludeInlineTest*` 选项 |
| 14 | `suppressSourceInfo: Boolean` | `chiselOptions.suppressSourceInfo` | `SuppressSourceInfoAnnotation` |
| 15 | `elideLayerBlocks: Boolean`（`var`） | `false` | `Elaborate` 写死初值 |
| 16 | `elaborationTrace: ElaborationTrace` | `elaborationTrace`（本帧 new） | 本地新建 |

**结论**：9 个 `ChiselOptions` 字段（`throwOnFirstError`/`useLegacyWidth`/`includeUtilMetadata`/`useSRAMBlackbox`/`warningFilters`/`sourceRoots`/`layerMap`/`inlineTestIncluder`/`suppressSourceInfo`）直接流入 `DynamicContext`；1 个（`loggerOptions`）来自独立的 `LoggerOptions` 投影；剩余几个（`defaultNamespace`/`definitions`/`contextCache`/`elideLayerBlocks`/`elaborationTrace`）由 `Elaborate` 填默认值。

注意三个**不在** `DynamicContext` 里的 `ChiselOptions` 字段：

- `printFullStackTrace`：在 `Elaborate` 的 catch 块里**直接**读，不进账本。
- `outputFile`：由下游 Emitter 阶段消费，与 elaboration 无关。
- `elaboratedCircuit`：这是 `ChiselOptions` 上的**输出**字段（`ChiselOptionsView` 从 `ChiselCircuitAnnotation` 反向投影出来），供后续阶段读取，不是 elaboration 的输入。

也就是说，并非所有 `ChiselOptions` 字段都流进 `DynamicContext`——取决于「这个开关是否在 elaboration 期间就需要被 `Builder` 读到」。

#### 4.3.3 源码精读

`ChiselOptions` 的字段表（[src/main/scala/chisel3/stage/ChiselOptions.scala:L11-L24](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/ChiselOptions.scala#L11-L24)）：

```scala
class ChiselOptions private[stage] (
  val printFullStackTrace: Boolean = false,
  val throwOnFirstError:   Boolean = false,
  val outputFile:          Option[String] = None,
  val sourceRoots:         Vector[File] = Vector.empty,
  val warningFilters:      Vector[WarningFilter] = Vector.empty,
  val useLegacyWidth:      Boolean = false,
  val layerMap:            Map[Layer, Layer] = Map.empty,
  val includeUtilMetadata: Boolean = false,
  val useSRAMBlackbox:     Boolean = false,
  val elaboratedCircuit:   Option[ElaboratedCircuit] = None,
  val inlineTestIncluder:  InlineTestIncluder = InlineTestIncluder.none,
  val suppressSourceInfo:  Boolean = false
)
```

`ChiselOptionsView` 展示这些字段如何由注解折叠而来（[src/main/scala/chisel3/stage/package.scala:L20-L43](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/package.scala#L20-L43)）：

```scala
def view(options: AnnotationSeq): ChiselOptions = options.collect { case a: ChiselOption => a }
  .foldLeft(new ChiselOptions()) { (c, x) =>
    x match {
      case ThrowOnFirstErrorAnnotation   => c.copy(throwOnFirstError = true)
      case UseLegacyWidthBehavior        => c.copy(useLegacyWidth = true)
      case UseSRAMBlackbox               => c.copy(useSRAMBlackbox = true)
      case SuppressSourceInfoAnnotation  => c.copy(suppressSourceInfo = true)
      // ... 其余映射 ...
    }
  }
```

于是「`--throw-on-first-error` 命令行 → `ThrowOnFirstErrorAnnotation` → `ChiselOptions.throwOnFirstError = true` → `DynamicContext.throwOnFirstError = true` → `Builder` 收集错误时第一个就抛」这条链就完整闭合了。`Builder` 如何用 `throwOnFirstError` 决定错误策略，见 [u4-l1](u4-l1-builder-global-state.md) 与 [u9-l2](u9-l2-error-and-elaboration-trace.md)。

`ElaborationTrace`（[core/src/main/scala/chisel3/internal/ElaborationTrace.scala:L39-L45](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/ElaborationTrace.scala#L39-L45)）是一个可选的模块级耗时追踪器，只认 `chisel.trace.file` 系统属性或 `CHISEL_TRACE_FILE` 环境变量；未设置时它的方法都是空操作。`Elaborate` 在 `Builder.build` 之后调 `elaborationTrace.finish()`（[Elaborate.scala:L67](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala#L67)）写出追踪文件。

#### 4.3.4 代码实践（本讲核心实践任务）

**实践目标**：在源码里亲手把「`ChiselOptions` 字段 → `DynamicContext` 构造参数」的对应关系走一遍，并验证一个开关确实改变了 elaboration 行为。

**操作步骤**：

1. 打开 [ChiselOptions.scala:L11-L24](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/ChiselOptions.scala#L11-L24)，列出 `ChiselOptions` 全部字段。
2. 打开 [Elaborate.scala:L45-L63](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala#L45-L63)，对照 [Builder.scala:L465-L483](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L465-L483) 的形参顺序，逐行把每个实参对上号。
3. 圈出哪些 `ChiselOptions` 字段**没有**出现在 `new DynamicContext(...)` 里。

4. 用 `useLegacyWidth` 开关观察行为差异（示例代码）。它会影响位宽推断规则（见 [u2-l2](u2-l2-bits-uint-sint-bool.md)）：

```scala
// 示例代码
import chisel3._
import circt.stage.ChiselStage

class WidthDemo extends Module {
  val io = IO(new Bundle {
    val a = Input(UInt(4.W))
    val b = Input(UInt(4.W))
    val y = Output(UInt())   // 位宽留给推断
  })
  io.y := io.a +& io.b       // 4+4 带进位 → 5 位
}

val v1 = ChiselStage.emitSystemVerilog(new WidthDemo)
val v2 = ChiselStage.emitSystemVerilog(Seq(chisel3.stage.UseLegacyWidthBehavior), new WidthDemo)
println(v1.lines.find(_.contains("output y")))
println(v2.lines.find(_.contains("output y")))
```

> 注：`emitSystemVerilog` 的注解传参方式因版本而异；若上面的签名不匹配，可改用命令行 `--use-legacy-width` 或查阅本地 `ChiselStage` 的实际 API。具体可用形式「待本地验证」。

**需要观察的现象**：按位置传参时第 N 个实参对应第 N 个形参；`printFullStackTrace`/`outputFile`/`elaboratedCircuit` 找不到对应形参。`+&` 在新旧位宽规则下，`y` 的推断位宽可能不同。

**预期结果**：得到与 4.3.2 表格一致的结论（9 个字段流入、3 个不流入）；两份 Verilog 中 `y` 的位宽声明可能不同。位宽具体值「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `defaultNamespace` 由 `Elaborate` 写死为 `None`，而不从 `ChiselOptions` 读？

**参考答案**：`defaultNamespace` 用于复用一个已存在的全局命名空间（例如跨多次 elaboration 共享名字去重器），而一次独立的顶层 elaboration 应当用全新的命名空间。`DynamicContext` 内部对 `None` 的处理是 `defaultNamespace.getOrElse(Namespace.empty)`（见 [Builder.scala:L514](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L514)），即「不指定就用空命名空间」。所以传 `None` 表示「这次从头开始命名」。

**练习 2**：`loggerOptions` 为什么不和 `chiselOptions` 合并成一个配置对象？

**参考答案**：因为日志（`logger`）是独立的库，有自己的选项体系和 `LoggerOptionsView`。Chisel 只是「借用」日志系统，在 elaboration 期间需要把日志选项也带进上下文（`Builder.build` 会用它建立日志作用域）。保持两个独立投影，避免 Chisel 与 logger 的配置耦合。

### 4.4 Builder.build 与产出注解：执行与产出

#### 4.4.1 概念说明

配置备齐、账本 new 好之后，`Elaborate` 用一行 `Builder.build(Module(gen()), context)` 按下 elaboration 的启动键。`Builder.build` 做两件事：**先把 context 绑定为当前上下文，再执行模块构造体**——顺序绝不能反，因为构造体里的每条 API 都要往「当前 context」里登记。跑完后，它把收集到的命令收拢成内部 IR `Circuit`，包成 `ElaboratedCircuit` 返回。`Elaborate` 再把这个返回值包成 `ChiselCircuitAnnotation`，与 `DesignAnnotation` 一起送回管道。

`Builder.build` 返回一个二元组 `(ElaboratedCircuit, T)`：第一个是细化出的整个电路（详见 [u4-l5](u4-l5-converter.md) 对 `ElaboratedCircuit` 的讨论）；第二个 `T` 是顶层的模块实例（`dut`），常被测试框架用来访问内部信号。`Builder.build` 内部机制属 [u4-l1](u4-l1-builder-global-state.md) 的内容，本讲只把它当黑盒，关注「输入是生成器+账本、输出是电路+实例」。

#### 4.4.2 核心流程

`Builder.build` 的执行顺序（关键：**先绑定后执行**），可用一个时序等式表达：

\[
  \text{makeScope}(\textit{loggerOptions}) \;\to\; \text{bind}(\textit{context}) \;\to\; \text{eval}(\textit{Module}(\textit{gen}())) \;\to\; \text{checkpoint} \;\to\; \text{assemble}(\textit{Circuit})
\]

「先绑定后执行」之所以成立，依赖一个 Scala 关键特性：**by-name 参数**。`build` 与 `buildImpl` 的第一个参数 `f: => T` 是按名调用的——`Module(gen())` 不是在调用 `build` 时立刻求值，而是推迟到 `withValue` 代码块内部才求值，从而保证模块构造发生在账本已绑定的环境里。

#### 4.4.3 源码精读

`Builder.build` 的签名与 `makeScope` 包装（[core/src/main/scala/chisel3/internal/Builder.scala:L1054-L1062](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L1054-L1062)）：

```scala
private[chisel3] def build[T <: BaseModule](
  f:              => T,
  dynamicContext: DynamicContext
): (ElaboratedCircuit, T) = {
  // Logger 有自己独立于 Chisel 动态上下文的作用域
  _root_.logger.Logger.makeScope(dynamicContext.loggerOptions) {
    buildImpl(f, dynamicContext)
  }
}
```

`buildImpl` 用 `DynamicVariable` 绑定 context，再在作用域内求值 `f`（[core/src/main/scala/chisel3/internal/Builder.scala:L1064-L1093](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L1064-L1093)）：

```scala
private def buildImpl[T <: BaseModule](f: => T, dynamicContext: DynamicContext): (ElaboratedCircuit, T) = {
  dynamicContextVar.withValue(Some(dynamicContext)) {
    ...
    val mod =
      try {
        val m = f            // 此时才真正执行 Module(gen())
        if (!inDefinition) { m._forceName(m.name, globalNamespace) }
        m
      } catch {
        case NonFatal(e) =>
          errors.checkpoint(logger)   // 构造体抛异常时也先报告已收集的错误
          throw e
      }
    errors.checkpoint(logger)         // 统一打印累积错误/警告
    ...
    val circuit = Circuit(...)        // 组装内部 IR
    (ElaboratedCircuit(circuit, ...), mod)
  }
}
```

几个要点：

- `f: => T`：by-name 参数，是「先绑定后执行」能成立的关键。
- `dynamicContextVar.withValue(Some(dynamicContext)) { ... }`：把 `DynamicContext` 绑定为全局可隐式访问的「当前上下文」。此后模块构造体里所有 `Builder.pushCommand`、`Builder.currentModule` 等访问器都转发到这个 context（见 [u4-l1](u4-l1-builder-global-state.md)）。
- `errors.checkpoint(logger)`：elaboration 期间 `Builder.error` 收集的错误并不会立即抛出，而是累积在 `ErrorLog` 里；`checkpoint` 在此统一打印。这就是 Chisel 能「一次报告多条错误」的原因。`f` 的 `catch` 分支里也调一次 `checkpoint`，保证即使构造体抛异常，已收集的错误也不丢失。
- 末尾 `(ElaboratedCircuit(circuit, ...), mod)`：把内部 IR `Circuit`（见 [u4-l2](u4-l2-internal-firrtl-ir.md)）封装成 `ElaboratedCircuit`——因为 `Circuit` 是 `chisel3.internal.firrtl.ir` 下的私有 IR，`ElaboratedCircuit` 是它的对外封装（把 `_circuit` 藏在 `private[chisel3]` 之后），公开 API 不暴露内部 IR 细节。

回到 `Elaborate`，拿到 `elaboratedCircuit` 后产出两条注解（[src/main/scala/chisel3/stage/phases/Elaborate.scala:L74-L77](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala#L74-L77)）：

```scala
Seq(
  ChiselCircuitAnnotation(elaboratedCircuit),
  DesignAnnotation(dut, layers = elaboratedCircuit._circuit.layers.flatMap(walkLayers(_)))
)
```

- `ChiselCircuitAnnotation` 装着 `ElaboratedCircuit`（[src/main/scala/chisel3/stage/ChiselAnnotations.scala:L300-L324](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/ChiselAnnotations.scala#L300-L324)），同样带 `Unserializable`，交给下游 `Convert`/`Emitter`（见 [u5-l3](u5-l3-convert-checks-emit.md)）。
- `DesignAnnotation` 装着顶层实例 `dut` 与摊平后的 layer 列表（[src/main/scala/chisel3/stage/ChiselAnnotations.scala:L457](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/ChiselAnnotations.scala#L457)），主要给测试与 layer 提取用。

#### 4.4.4 代码实践

**实践目标**：跑通 `Elaborate` 后，确认它的产出确实包含 `ChiselCircuitAnnotation`，并能从中拿到电路。

**操作步骤**（示例代码）：

```scala
// 示例代码
import chisel3._
import chisel3.stage._
import chisel3.stage.phases.Elaborate

class Reg2x extends RawModule {
  val io = IO(new Bundle { val in = Input(UInt(8.W)); val out = Output(UInt(8.W)) })
  io.out := RegNext(io.in)
}

val out: firrtl.annotations.AnnotationSeq =
  (new Elaborate).transform(Seq(ChiselGeneratorAnnotation(() => new Reg2x)))

val cca = out.collect { case a: ChiselCircuitAnnotation => a }.head
val des = out.collect { case a: DesignAnnotation[_]     => a }.head

println("电路顶层名: " + cca.elaboratedCircuit.name)
println("顶层实例类: " + des.design.getClass.getName)
```

**需要观察的现象**：`cca.elaboratedCircuit.name` 应为 `Reg2x`；`des.design` 应是 `Reg2x` 的实例。

**预期结果**：打印出 `电路顶层名: Reg2x` 与 `顶层实例类: ...Reg2x`。具体文本「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `Builder.build` 的第一个参数 `f: => T` 改成普通按值参数 `f: T`，会出什么问题？

**参考答案**：按值参数会在**调用 `build` 之前**就求值 `Module(gen())`，而那时 `dynamicContextVar` 还没被 `withValue` 绑定，模块构造体里的 `Builder.pushCommand` 等会读到「没有当前上下文」的状态（访问器会抛「must be inside Builder context」）。by-name 参数正是为了把构造推迟到绑定之后。

**练习 2**：`Elaborate` 的产出里为什么除了 `ChiselCircuitAnnotation` 还要带一条 `DesignAnnotation`？

**参考答案**：`ChiselCircuitAnnotation` 给的是「电路结构」（用于下游编译到 Verilog），`DesignAnnotation` 给的是「顶层 Scala 模块实例」（`dut`），供测试框架、层级化 API 等在 elaboration 之后**仍能以对象引用方式**访问模块端口与内部信号。两者用途不同，缺一不可。

## 5. 综合实践

把本讲四个模块串起来，完成「手动驱动一次 elaboration 并观察配置流动」的任务：用 `ChiselGeneratorAnnotation.elaborate` **单独跑 `Elaborate` 阶段**，拿到 `ChiselCircuitAnnotation` 并打印内部 CHIRRTL 文本，再验证某个 `ChiselOptions` 开关确实改变了行为。

**操作步骤**（示例代码）：

```scala
// 示例代码
import chisel3._
import chisel3.stage._

class Counter extends Module {
  val io = IO(new Bundle { val en = Input(Bool()); val cnt = Output(UInt(8.W)) })
  val c = RegInit(0.U(8.W))
  when(io.en) { c := c + 1.U }
  io.cnt := c
}

// 1) 单步跑 Elaborate：ChiselGeneratorAnnotation.elaborate 内部 new 一个 Elaborate 并 transform
val annos: firrtl.annotations.AnnotationSeq =
  ChiselGeneratorAnnotation(() => new Counter).elaborate

// 2) 捞出电路注解，序列化成 CHIRRTL 文本（走 Serializer，见 u4-l4）
val circuit = annos.collectFirst { case ChiselCircuitAnnotation(c) => c }.get
circuit.serialize.foreach(println)

// 3) 验证配置链路：加入 ThrowOnFirstErrorAnnotation，观察 DynamicContext.throwOnFirstError 随之生效
class BadCnt extends Module {
  val io = IO(new Bundle { val a = Input(UInt(8.W)); val b = Output(Bool()) })
  io.b := io.a   // 类型不匹配，触发错误
}
val annos2 = Seq(ChiselGeneratorAnnotation(() => new BadCnt), ThrowOnFirstErrorAnnotation)
try { (new chisel3.stage.phases.Elaborate).transform(annos2) }
catch { case e: chisel3.ChiselException => println("在第一个错误就抛出了: " + e.getMessage) }
```

**自查清单**：

1. `ThrowOnFirstErrorAnnotation` 经 `view[ChiselOptions]` → `chiselOptions.throwOnFirstError` → `DynamicContext` 第 2 个参数流进去（见 [Elaborate.scala:L45-L63](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala#L45-L63)）。
2. `Builder.build` 在 [Builder.scala:L1054-L1062](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L1054-L1062) 是 elaboration 的唯一启动键。
3. 产物在 [Elaborate.scala:L74-L77](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala#L74-L77) 被包装成 `ChiselCircuitAnnotation` + `DesignAnnotation`。

**需要观察的现象**：步骤 2 的 CHIRRTL 文本里应能看到 `module Counter`、`input en`、`output cnt`、`reg c : UInt<8>` 以及一个 `add` 原语节点；步骤 3 在第一个错误就抛出异常。

**预期结果**：CHIRRTL 大致形如（精确文本随版本变化，**待本地验证**）：

```
circuit Counter :
  module Counter :
    input clock : Clock
    input reset : UInt<1>
    input en : UInt<1>
    output cnt : UInt<8>
    ...
    reg c : UInt<8>, clock with : (reset => (reset, UInt<8>("h00")))
    ...
    node _c_T = add(c, UInt<1>("h01"))
    ...
```

如果你能在加/不加 `ThrowOnFirstErrorAnnotation` 时观察到错误抛出时机的不同，就说明你已经看懂了「命令行注解 → `ChiselOptions` → `DynamicContext` → `Builder` 行为」这条完整的配置流动链。

## 6. 本讲小结

- `Elaborate` 是一个 `Phase`，在 `transform` 里用 `flatMap` 把 `ChiselGeneratorAnnotation` 替换成 `ChiselCircuitAnnotation`（外加 `DesignAnnotation`），其余注解原样透传；它声明 `Checks`（chisel3 与 logger）为硬前提，`invalidates` 恒为 `false`。
- `ChiselGeneratorAnnotation` 携带的是「`() => RawModule` 生成器函数」而非已构造的电路，从而把构造推迟到 `Elaborate`、并保证它在正确的 `DynamicContext` 内执行。
- 配置流动链：命令行注解 → `ChiselOptionsView` 折叠成 `ChiselOptions` → `Elaborate` 按位置把字段搬进 `new DynamicContext(...)` → `Builder` 在细化期间读取这些字段决定行为。9 个 `ChiselOptions` 字段直接流入 `DynamicContext`；`printFullStackTrace`/`outputFile`/`elaboratedCircuit` 不流入。
- `Builder.build` 依赖 by-name 参数实现「先绑定 `DynamicContext`、后执行 `Module(gen())`」的关键时序；跑完后用 `errors.checkpoint` 统一打印累积错误，再把内部 `Circuit` 封装成 `ElaboratedCircuit` 返回。
- elaboration 异常被 `NonFatal` 捕获，默认经 `trimStackTraceToUserCode` 裁剪掉 `chisel3`/`scala`/`java` 等内部帧，只留用户代码；`PrintFullStackTraceAnnotation` 可关掉裁剪。

## 7. 下一步学习建议

- 沿管道往下读 **Convert / Checks / Emitter 阶段**（[u5-l3](u5-l3-convert-checks-emit.md)）：看本讲产出的 `ChiselCircuitAnnotation` 如何被 `Convert` 阶段消费、调用 `Converter`（见 [u4-l5](u4-l5-converter.md)）转成 `firrtl.ir.Circuit`，再由 `Emitter` 产出中间文件。这是本讲的直接下游。
- 再往下读 **CIRCT 集成**（[u5-l4](u5-l4-circt-firtool.md)）：看电路最终如何被交给 firtool 编译成 SystemVerilog。
- 若想深究 `Builder.build` 内部如何收拢命令、组装 `ElaboratedCircuit`，回到 [u4-l1](u4-l1-builder-global-state.md)（Builder 全局状态机）与 [u4-l2](u4-l2-internal-firrtl-ir.md)（内部 FIRRTL IR）。
- 错误收集与堆栈裁剪的完整机制留到 [u9-l2](u9-l2-error-and-elaboration-trace.md)（错误处理与 ElaborationTrace）展开，本讲只触及了 `errors.checkpoint` 与 `trimStackTraceToUserCode` 这两个出口。
