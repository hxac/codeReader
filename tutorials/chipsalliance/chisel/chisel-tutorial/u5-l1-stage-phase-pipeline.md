# Stage / Phase 管道

## 1. 本讲目标

本讲是单元 5「Stage 与 CIRCT 集成」的首篇，承接 u1-l5 给出的四段式架构（前端 → Builder → 内部 IR → 发射），打开其中最后一段「如何把这些阶段串成一条管道」的机制。

读完本讲你应当能够：

1. 读懂 `circt.stage.ChiselStage` 的两条管道：`class ChiselStage.run`（命令行入口）与 `object ChiselStage.phase`（库 API 入口），并指出它们的差别与共性。
2. 说清 `Phase` 是什么，以及 `prerequisites` / `optionalPrerequisites` / `optionalPrerequisiteOf` / `invalidates` 这四组依赖关系如何**取代写死的顺序**来描述管道。
3. 理解 `PhaseManager` 如何把「一组依赖关系」拓扑排序成一个可执行的 `Phase` 线性序列，并据此自动拉入未声明的必需阶段（如 `Elaborate`）。
4. 说清 `Shell` / `CLI` 如何把命令行字符串（如 `--target systemverilog`）解析成 `Annotation`，以及 `AnnotationSeq` 如何作为唯一的数据载体贯穿整条管道。

> 本讲只讲「管道怎么搭」，具体每个阶段内部做什么分别留给 u5-l2（Elaborate）、u5-l3（Convert/Checks/Emitter）、u5-l4（CIRCT/firtool）。

## 2. 前置知识

在进入源码前，先建立三个直觉。它们来自 u1-l5，这里做一句话回顾并补充本讲要用到的新术语。

### 2.1 「注解流」而非「数据流」

很多编译器把中间表示（IR）从头传到尾，每个 pass 加工 IR。Chisel/FIRRTL 的硬件编译框架（HCF, Hardware Compiler Framework）走的是另一条路：**所有阶段共享的唯一数据结构是一个注解列表 `AnnotationSeq`，即 `Seq[Annotation]`**。每个阶段读入一批注解、产出另一批注解；电路本身（`ElaboratedCircuit`、`firrtl.ir.Circuit`、生成的 Verilog 字符串）也都以**特殊注解**的形式被塞进这个列表里传来传去。

这种设计的好处是阶段之间**极度松耦合**：阶段 A 不需要知道阶段 B 的存在，只需要认得自己关心的那几类注解。代价是你得习惯「数据全藏在注解里」。

### 2.2 阶段（Phase）：一个数学变换

一个 `Phase` 在数学上就是 `AnnotationSeq => AnnotationSeq` 的一个函数（源码里叫 `TransformLike`）。它不做 I/O 副作用以外的事，输入输出同类型。把若干 `Phase` 串起来 `foldLeft`，就是一条编译管道。

### 2.3 关键术语表

| 术语 | 含义 | 首次出现 |
|------|------|----------|
| `Annotation` | 一条带类型标签的元数据，是管道里流动的「数据包」 | §4.5 |
| `AnnotationSeq` | `Seq[Annotation]`，贯穿全管道的唯一数据载体 | §4.5 |
| `Phase` | 对 `AnnotationSeq` 的一次数学变换，编译管道的一个「小步骤」 | §4.2 |
| `Dependency` | 对某个 `Phase` 的引用（按类名或单例对象），用于声明依赖关系 | §4.2 |
| `PhaseManager` | 读入一组 `Phase` 依赖关系，拓扑排序出执行顺序的调度器 | §4.3 |
| `Stage` | 带命令行界面的 `Phase`（「一个能从命令行驱动的阶段」） | §4.1 |
| `Shell` / `CLI` | 命令行选项解析器，把 `--xxx` 字符串变成 `Annotation` | §4.4 |

## 3. 本讲源码地图

本讲涉及的关键文件，按「从用户入口到底层机制」排列：

| 文件 | 作用 |
|------|------|
| [src/main/scala/circt/stage/ChiselStage.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala) | 用户入口。`class ChiselStage`（命令行）与 `object ChiselStage`（库 API，如 `emitSystemVerilog`），各自组装一条 `PhaseManager` 管道。 |
| [src/main/scala/circt/stage/Shell.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/Shell.scala) | `CLI` trait，声明 Chisel/CIRCT 的全部命令行选项。 |
| [src/main/scala/chisel3/stage/package.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/package.scala) | `ChiselOptionsView`：把散落的注解「投影」成一个结构化的 `ChiselOptions` 配置对象。 |
| [src/main/scala/circt/stage/Annotations.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/Annotations.scala) | `CIRCTTargetAnnotation`、`FirtoolOption` 等注解定义及其 `--target` / `--firtool-option` 选项绑定。 |
| [firrtl/src/main/scala/firrtl/options/Phase.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/firrtl/src/main/scala/firrtl/options/Phase.scala) | `Phase`、`DependencyAPI`、`Dependency` 的定义——依赖机制的根基。 |
| [firrtl/src/main/scala/firrtl/options/DependencyManager.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/firrtl/src/main/scala/firrtl/options/DependencyManager.scala) | `PhaseManager`：拓扑排序、`transformOrder`、执行引擎。 |
| [firrtl/src/main/scala/firrtl/options/Stage.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/firrtl/src/main/scala/firrtl/options/Stage.scala) | `Stage` 抽象基类与 `StageMain`（命令行 `main` 方法）。 |
| [firrtl/src/main/scala/firrtl/options/Shell.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/firrtl/src/main/scala/firrtl/options/Shell.scala) | `BareShell` / `Shell`：基于 scopt 的命令行解析骨架。 |
| [src/main/scala/chisel3/stage/phases/Elaborate.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala) | `Elaborate` 阶段示例：看它如何声明 `prerequisites`。 |

> 提示：`firrtl.*` 包下的 API 在源码里整体标注了 `@deprecated("All APIs in package firrtl are deprecated.", "Chisel 7.0.0")`，但它们**目前仍是 Chisel 编译管道的实际实现**（见后续模块说明）。这里的 deprecated 是「将来要从 firrtl 命名空间迁出」，不是「已废弃勿用」，学习阶段照常阅读。

---

## 4. 核心概念与源码讲解

本讲覆盖四个最小模块：

- **4.1** `circt.stage.ChiselStage`：用户入口与两条管道
- **4.2** `Phase` 与 `DependencyAPI`：用依赖关系代替写死的顺序
- **4.3** `PhaseManager`：把依赖关系拓扑排序成执行顺序
- **4.4** `Shell` 与 CLI：命令行如何变成注解
- **4.5** `AnnotationSeq`：贯穿整条管道的数据载体

### 4.1 circt.stage.ChiselStage：用户入口与两条管道

#### 4.1.1 概念说明

`circt.stage.ChiselStage` 是现代 Chisel 生成 Verilog 的**统一入口**。它有两副面孔：

- **`class ChiselStage`**：一个「Stage」（带命令行的阶段），供命令行脚本或 `(new ChiselStage).execute(...)` 调用。它的核心方法是 `run`，在里面用 `PhaseManager` 搭出管道。
- **`object ChiselStage`**（伴生对象）：一组更友好的库 API，如 `emitSystemVerilog`、`emitCHIRRTL`、`elaborate`。它们各自组装一条**独立的** `PhaseManager`（见私有方法 `phase`），然后把结果注解 `collectFirst` 出来。

之所以有两条管道，是因为命令行入口和库 API 的需求略有不同（例如库 API 通常想直接拿到字符串结果，命令行入口想写文件）。但它们都用同一个机制——`PhaseManager`——只是往里塞的 `targets`（目标阶段）集合不同。

> 历史提示：`chisel3.stage` 包里有一个旧入口 `chisel3.stage.ChiselStage`。源码里专门留了一条提示字符串 `pleaseSwitchToCIRCT`，告诉大家改用 `circt.stage.ChiselStage`。

#### 4.1.2 核心流程

`class ChiselStage` 的工作流程：

1. 收到一组输入 `AnnotationSeq`（来自命令行解析或调用方）。
2. 在 `run` 里 `new PhaseManager(targets = ..., currentState = ...)`。
3. `pm.transform(annotations)` 让 `PhaseManager` 自动拓扑排序并依次执行各阶段。
4. 返回变换后的 `AnnotationSeq`。

`object ChiselStage.emitSystemVerilog` 的工作流程：

1. 用 `ChiselGeneratorAnnotation(() => gen)` 告诉管道「要细化哪个模块」，用 `CIRCTTargetAnnotation(SystemVerilog)` 告诉管道「目标是 SystemVerilog」。
2. 用 `(new Shell("circt")).parse(args)` 把额外命令行参数也变成注解；用 `firtoolOpts.map(FirtoolOption(_))` 把 firtool 选项变成注解。
3. `phase.transform(annos)` 跑另一条 `PhaseManager` 管道。
4. 从结果注解里 `collectFirst { case EmittedVerilogCircuitAnnotation(a) => a }.get.value` 取出 Verilog 字符串。

#### 4.1.3 源码精读

先看 `class ChiselStage`——注意它 `extends Stage`，且声明「自己不依赖任何阶段、不使任何阶段失效」：

[ChiselStage.scala:18-28](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala#L18-L28) —— `class ChiselStage extends Stage`，并重写 `shell` 为带 `CLI` 的 Shell。这里 `Stage` 是「带命令行的 Phase」（见 §4.2）。

`run` 方法是命令行管道的组装处：

[ChiselStage.scala:30-49](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala#L30-L49) —— `run` 构造一个 `PhaseManager`。要点：

- `targets`（第 33–42 行）是「你想要最终跑完的阶段」，共 8 个：`AddImplicitOutputFile`、`AddImplicitOutputAnnotationFile`、`AddSerializationAnnotations`、`AddDebugIntrinsics`、`Convert`、`AddDedupGroupAnnotations`、`circt.stage.phases.AddImplicitOutputFile`、`CIRCT`。
- 注意 `targets` 里**没有** `Elaborate`！但 `Convert` 声明了 `prerequisites = [Elaborate]`，所以 `PhaseManager` 会**自动把 `Elaborate` 及其前提拉进来**。这就是依赖式调度的威力（详见 §4.3）。
- `currentState`（第 43–46 行）是「假定已经跑过、不必再跑」的阶段：`firrtl.stage.phases.AddDefaults` 与 `firrtl.stage.phases.Checks`。
- 第 48 行 `pm.transform(annotations)` 真正驱动整条管道。

再看伴生对象的库 API 管道——它与上面的 `run` 管道**不同**：

[ChiselStage.scala:56-68](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala#L56-L68) —— 私有方法 `phase` 构造**另一条** `PhaseManager`，它的 `targets` 显式包含 `Elaborate`，还包含 `circt.stage.phases.Checks`，但不包含 `AddSerializationAnnotations`、`chisel3.AddImplicitOutputFile`。两条管道目标不同，但因为依赖图会自动补全前提，最终执行到的阶段集合高度重合。

`emitSystemVerilog` 是这条管道的典型消费者：

[ChiselStage.scala:197-213](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala#L197-L213) —— 组装初始注解（生成器 + 目标 + 命令行 + firtool 选项），`phase.transform` 跑完，再 `collectFirst` 抓出 `EmittedVerilogCircuitAnnotation` 的 `.value`（即 Verilog 字符串）。这就是 u1-l4 里「按下生成按钮的那一行」落到源码上的样子。

命令行入口由 `ChiselMain` 提供：

[ChiselStage.scala:258-259](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala#L258-L259) —— `object ChiselMain extends StageMain(new ChiselStage)`。`StageMain` 给一个 `Stage` 套上标准 `main(args)`（详见 §4.4），于是 `ChiselMain` 就成了一个可执行程序的入口。

#### 4.1.4 代码实践

**实践目标**：确认「库 API」与「命令行」两条管道确实存在且目标集合不同。

**操作步骤**：

1. 打开 [ChiselStage.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala)。
2. 找到 `class ChiselStage` 的 `run`（第 30 行）与 `object ChiselStage` 的 `phase`（第 57 行）。
3. 把两个 `targets` 列表逐条抄下来，画一张对照表（左列「`run` 管道」、右列「`phase` 管道」），用 ✅/❌ 标出某阶段是否在各自 `targets` 里。
4. 用笔圈出：哪条管道**显式**列了 `Elaborate`？哪条没有？（答案：`phase` 显式列了，`run` 没有。）

**需要观察的现象**：两条管道的 `targets` 既不完全相同，也不完全互斥——它们只是从不同角度描述「我想要的最终产物」。

**预期结果**：你能给出一张表，说明 `Convert`、`CIRCT` 在两条管道都出现；`Elaborate`、`circt.Checks` 只在 `phase` 显式出现；`AddSerializationAnnotations`、`chisel3.AddImplicitOutputFile` 只在 `run` 出现。

> 若想验证「`run` 没列 `Elaborate` 但它仍然会跑」，可在 §4.3 的实践中用日志确认。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `class ChiselStage.run` 的 `targets` 里不写 `Elaborate`，elaboration 却依然会发生？

**参考答案**：因为 `Convert` 阶段声明了 `prerequisites = Seq(Dependency[Elaborate])`（见 [Convert.scala:19](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Convert.scala#L19)）。`PhaseManager` 解析依赖图时会自动把 `Elaborate`（及其前提 `chisel3.Checks`、`logger.Checks`）补进执行序列。`targets` 只表达「最终想要的」，前提由依赖关系自动推导。

**练习 2**：`emitSystemVerilog` 与 `(new ChiselStage).execute(...)` 用的是同一个 `PhaseManager` 吗？

**参考答案**：不是。前者用伴生对象的私有 `phase`（[ChiselStage.scala:57](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala#L57)），后者用 `class ChiselStage.run` 内构造的 `PhaseManager`（[ChiselStage.scala:32](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala#L32)）。两者 `targets` 不同，但因依赖自动补全，实际执行到的阶段集合大体一致。

---

### 4.2 Phase 与 DependencyAPI：用依赖关系代替写死的顺序

#### 4.2.1 概念说明

最朴素的搭管道方式是写一个 `Seq[Phase]` 然后 `foldLeft`——但这样**顺序写死、刚性**：你想插一个新阶段，得手动找它在列表里的位置，还得保证它依赖的阶段排在前面。

HCF 用了一套更弹性的方案：**每个阶段只声明「我需要谁先跑」「我使谁失效」，由调度器去算出合法顺序**。这套声明的接口叫 `DependencyAPI`，承载它的抽象叫 `Phase`。

四个声明维度（直接引自源码注释）：

| 声明 | 语义 | 何时触发调度 |
|------|------|-------------|
| `prerequisites` | 必须在我**之前**跑的阶段（硬依赖） | 总是会被拉入并排在我之前 |
| `optionalPrerequisites` | 如果它在图里，就应排在我之前（软依赖） | 仅当该阶段已是图中节点时才生效 |
| `optionalPrerequisiteOf` | 把我自己注入为别人的软依赖（「我想排在 X 之前」） | 不会让 X 被拉入图，只施加顺序约束 |
| `invalidates(a)` | 我会**撤销** `a` 的效果（默认 `true`） | 若被撤销的 `a` 仍被下游需要，则 `a` 需要重跑 |

> 默认 `invalidates(a) = true` 意味着「一个阶段默认会推翻其他所有阶段的成果」。Chisel 自己的 `Phase` 几乎都把 `invalidates` 重写为 `false`（即「我跑完不影响别人」），因为它们都只是在往 `AnnotationSeq` 里追加注解，互不破坏。

#### 4.2.2 核心流程

`Phase` 的依赖模型可以用一张有向图理解：

- 节点 = `Phase`。
- 边 = 「A 是 B 的 prerequisite」⇒ 有一条 A → B 的边（A 必须先跑）。
- 调度 = 对这张图做**拓扑排序**，得到一个线性执行序列（DAG 的 `linearize`）。
- 冲突 = 图里有环 ⇒ 抛 `DependencyManagerException`。

一个 `Phase` 对外其实只有一个真正干活的方法：`transform(a: A): A`（来自 `TransformLike`）。四个依赖声明只是**元数据**，调度器读它们来排序，排序完了才挨个调 `transform`。

#### 4.2.3 源码精读

先看 `Phase` 的根基 `TransformLike`——它定义了「一个数学变换」：

[Phase.scala:82-94](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/firrtl/src/main/scala/firrtl/options/Phase.scala#L82-L94) —— `trait TransformLike[A]` 只有两个抽象成员：`name`（用于日志）和 `transform(a: A): A`。这就是「阶段 = 一个纯函数」的最简定义。

`DependencyAPI` 在此之上加四组依赖声明：

[Phase.scala:137-173](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/firrtl/src/main/scala/firrtl/options/Phase.scala#L137-L173) —— 四个方法 + 四个 `_` 前缀的内部缓存版本（`_prerequisites` 等，懒求值为 `LinkedHashSet`）。注意第 171 行 `def invalidates(a: A): Boolean = true`：**默认使所有其他阶段失效**。

`Phase` 把两者合体，并固定变换类型为 `AnnotationSeq`：

[Phase.scala:181-191](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/firrtl/src/main/scala/firrtl/options/Phase.scala#L181-L191) —— `trait Phase extends TransformLike[AnnotationSeq] with DependencyAPI[Phase]`。`name` 默认取类全名。所以你写 `class Elaborate extends Phase`，就自动得到一个带依赖元数据的 `AnnotationSeq => AnnotationSeq` 变换。

依赖关系用 `Dependency` 类型来引用阶段，它本质上「按类名指向一个类」：

[Phase.scala:49-77](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/firrtl/src/main/scala/firrtl/options/Phase.scala#L49-L77) —— `case class Dependency[+A](id: Either[Class[_], A with Singleton])`，`getObject()` 通过反射 `newInstance` 构造（`Left`）或直接取单例（`Right`）。这就是为什么 `Dependency[Elaborate]` 能在运行时实例化一个 `Elaborate`。

来看一个真实 `Phase` 是怎么写依赖的——`Elaborate`：

[Elaborate.scala:29-37](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala#L29-L37) —— `prerequisites` 写了 `chisel3.stage.phases.Checks` 和 `logger.phases.Checks`（硬依赖），`invalidates(a) = false`（不破坏别人）。`Elaborate` 内部具体做什么留给 u5-l2，本讲只关注它「如何声明依赖」。

再看一个用 `optionalPrerequisiteOf` 的例子——`circt.stage.phases.Checks`：

[Checks.scala:13-17](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/phases/Checks.scala#L13-L17) —— `optionalPrerequisiteOf = Seq(Dependency[circt.stage.phases.CIRCT])`。意思是「只要 CIRCT 在图里，就把我排在 CIRCT 之前做合法性检查」。注意它**不会**让 CIRCT 被拉入图，只是施加顺序约束。

#### 4.2.4 代码实践

**实践目标**：亲手读懂一个真实 `Phase` 的四组依赖声明，判断它的「调度行为」。

**操作步骤**：

1. 打开 [CIRCT.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/phases/CIRCT.scala)，定位 `class CIRCT extends Phase`（第 124 行）及其重写的依赖（第 130–135 行）。
2. 同样读 [AddDedupGroupAnnotations.scala:15-19](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/AddDedupGroupAnnotations.scala#L15-L19) 与 [AddDebugIntrinsics.scala:14-18](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/AddDebugIntrinsics.scala#L14-L18)。
3. 填一张表：每个阶段，`prerequisites` / `optionalPrerequisites` / `optionalPrerequisiteOf` / `invalidates` 各填什么。
4. 推理：`AddDebugIntrinsics` 的 `optionalPrerequisiteOf = [Convert, AddDedupGroupAnnotations]` 说明了它相对这两个阶段的什么顺序？

**需要观察的现象**：不同阶段用不同的依赖维度表达需求。`prerequisites` 是硬性的，其余是柔性的顺序建议。

**预期结果**：你应该能口述——`CIRCT` 硬依赖 `circt.AddImplicitOutputFile`；`AddDebugIntrinsics` 硬依赖 `Elaborate`，并柔性地要求排在 `Convert`、`AddDedupGroupAnnotations` 之前；`AddDedupGroupAnnotations` 柔性地要求排在 `Convert` 之后（`optionalPrerequisites = [Convert]`）。

#### 4.2.5 小练习与答案

**练习 1**：`prerequisites` 与 `optionalPrerequisites` 的关键区别是什么？

**参考答案**：`prerequisites` 是**硬依赖**——声明的阶段一定会被拉进执行图并排在本阶段之前；`optionalPrerequisites` 是**软依赖**——仅当那个阶段**已经是**图中节点（被别人拉进来了）时，才要求它排在本阶段之前，否则不会因此把那个阶段拉进图。

**练习 2**：为什么几乎所有 Chisel `Phase` 都把 `invalidates` 重写成 `false`？

**参考答案**：这些阶段都是在往 `AnnotationSeq` 里**追加**注解（只增不改不删），彼此不破坏对方的产物。若保留默认值 `true`，则一个阶段跑完会让其他阶段「失效」，下游若还需要这些阶段就得重跑，造成无谓的重复劳动。设为 `false` 表示「我跑完不影响别人已建立的成果」。

---

### 4.3 PhaseManager：把依赖关系拓扑排序成执行顺序

#### 4.3.1 概念说明

`PhaseManager` 是把「依赖声明」变成「可执行线性序列」的调度器。你告诉它两件事：

- `targets`：你**想要**最终跑完的阶段（愿望清单）。
- `currentState`：你**假定**已经跑过的阶段（起点，跳过它们及其前提）。

它做的事是：

1. 从 `targets` 出发，沿 `prerequisites` 边做反向 BFS，把所有必需的前提阶段都拉进图（这就是 §4.1 里 `Elaborate` 被自动补入的原因）。
2. 叠加 `optionalPrerequisites`、`optionalPrerequisiteOf` 形成的柔性顺序边。
3. 对合成后的有向无环图（DAG）做拓扑排序，得到 `transformOrder: Seq[Phase]`。
4. 处理 `invalidates`：若某阶段使下游需要的阶段失效，则把这些阶段作为**子问题**重新调度（re-lowering）。
5. 把排好序的阶段依次 `foldLeft` 跑在 `AnnotationSeq` 上。

`PhaseManager` 自己也是一个 `Phase`（它 `extends ... with Phase`），所以可以嵌套——这也是「一个 Phase 内部由多个 Phase 组成」的实现方式。

#### 4.3.2 核心流程

调度算法的骨架（伪代码）：

```
输入: targets（愿望）, currentState（起点）
1. prerequisiteGraph = 从 (targets − currentState) 出发，
                       沿 prerequisites 反向 BFS，跳过 currentState，得到 DAG
2. 叠加 optionalPrerequisiteOfGraph / optionalPrerequisitesGraph 得到 dependencyGraph
3. invalidateGraph = 沿 invalidates 边的 BFS（决定重跑哪些）
4. transformOrder = 对 dependencyGraph 做拓扑排序，
                    dropWhile（已在 currentState 里的），并按需插入「重跑子问题」
5. 执行: annos.foldLeft over transformOrder，逐个调 phase.transform(annos)
```

拓扑排序保证：若 A 是 B 的 prerequisite，则 A 排在 B 前。对**互不依赖**的阶段，其相对顺序由排序算法决定（不一定唯一）。

最终执行（`transform`）时，每个阶段调用前会校验其 `prerequisites` 是否都已满足，否则抛 `DependencyManagerException`：

\[ \texttt{transformOrder} = \texttt{topoSort}(\texttt{prerequisiteGraph} \cup \texttt{optionalEdges}) \setminus \texttt{currentState} \]

#### 4.3.3 源码精读

`PhaseManager` 的类定义极简——它只是把参数喂给 `DependencyManager`：

[DependencyManager.scala:441-459](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/firrtl/src/main/scala/firrtl/options/DependencyManager.scala#L441-L459) —— `class PhaseManager(val targets, val currentState, val knownObjects) extends DependencyManager[AnnotationSeq, Phase] with Phase`。`PhaseDependency` 就是 `Dependency[Phase]` 的别名（第 457 行）。

真正干活的是 `DependencyManager` trait。先看它如何从 `targets` 反向拉入前提：

[DependencyManager.scala:119-127](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/firrtl/src/main/scala/firrtl/options/DependencyManager.scala#L119-L127) —— `prerequisiteGraph`：`start = _targets &~ _currentState`（愿望减去已有），`blacklist = _currentState`（跳过起点），`extractor` 沿 `_prerequisites` 继续展开。这段 BFS 就是「为什么没在 `targets` 里写 `Elaborate`，它也会被拉进来」的代码级原因。

合成完整依赖图与失效图：

[DependencyManager.scala:163-184](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/firrtl/src/main/scala/firrtl/options/DependencyManager.scala#L163-L184) —— `dependencyGraph` 把三类边（硬前提、optionalPrerequisiteOf、optionalPrerequisites）合并；`invalidateGraph` 沿 `invalidates` 边做 BFS。这两个图都可能有环，环会在下一步被检测出来。

排序与执行的核心：

[DependencyManager.scala:205-245](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/firrtl/src/main/scala/firrtl/options/DependencyManager.scala#L205-L245) —— `transformOrder`：先用 `invalidateGraph.linearize` 做拓扑排序得到比较基准（第 211 行），再对 `dependencyGraph` 排序并 `dropWhile` 掉 `currentState`（第 222–225 行）；遇到前提未满足的阶段，就用 `this.copy(...)` 造一个**子 `PhaseManager`** 来补跑（第 233–236 行）。这就是注释里说的「re-lowerings implemented as new DependencyManagers」。

最终执行入口：

[DependencyManager.scala:255-286](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/firrtl/src/main/scala/firrtl/options/DependencyManager.scala#L255-L286) —— `final override def transform(annotations)`：把 `flattenedTransformOrder`（展平嵌套子 manager 后的纯 `Phase` 序列）`foldLeft` 跑在注解上。第 270–276 行做运行时前提校验（双重保险）；第 279 行 `t.transform(a)` 是真正调到你写的阶段代码的地方；第 283 行用 `invalidates` 更新 `currentState`。

> 这套 `transformOrder` 算法相对复杂。初读时只要抓住三件事：① 前提自动补全；② 拓扑排序；③ 失效触发子问题重跑。其余细节属于「想精细控制调度时再回头看」。

#### 4.3.4 代码实践

**实践目标**：根据各 `Phase` 的依赖声明，手动推出 `object ChiselStage.phase` 管道的一种合法执行顺序，验证 `Elaborate → Convert → CIRCT` 这条主干。

**操作步骤**：

1. 从 [ChiselStage.scala:58-67](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala#L58-L67) 抄下 `phase` 的 8 个 `targets`。
2. 查每个 target 的依赖声明（参考 §4.2.3 已读到的，必要时打开对应文件）：`Elaborate`（[Elaborate.scala:31-34](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala#L31-L34)）、`AddDebugIntrinsics`、`Convert`、`AddDedupGroupAnnotations`、`circt.AddImplicitOutputFile`、`chisel3.AddImplicitOutputAnnotationFile`、`circt.Checks`、`CIRCT`（[CIRCT.scala:130-132](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/phases/CIRCT.scala#L130-L132)）。
3. 把依赖画成一张 DAG（A 是 B 的 prerequisite ⇒ 画 A→B；`optionalPrerequisiteOf` ⇒ 画虚线 A→B）。
4. 对 DAG 手工拓扑排序，得到一个线性序列。

**需要观察的现象**：`Elaborate` 是 `Convert`/`AddDebugIntrinsics`/`chisel3.AddImplicitOutputFile`/`AddImplicitOutputAnnotationFile` 的共同前提，故必靠前；`CIRCT` 依赖 `circt.AddImplicitOutputFile`，且 `circt.Checks` 柔性地要求排在 `CIRCT` 之前，故 `CIRCT` 必靠后。

**预期结果**：一种**合法**的拓扑顺序（互不依赖的阶段相对顺序可能不同）如下，主干 `Elaborate → Convert → CIRCT` 清晰可见：

```
chisel3.Checks / logger.Checks   ← Elaborate 的前提，自动补入
        ↓
    Elaborate                    ← 产出 ChiselCircuitAnnotation
        ↓
AddDebugIntrinsics               ← (optionalPrerequisiteOf Convert/AddDedup)
        ↓
    Convert                      ← (prereq Elaborate) 产出 FirrtlCircuitAnnotation
        ↓
AddDedupGroupAnnotations         ← (optionalPrerequisites Convert)
        ↓
circt.AddImplicitOutputFile      ← (prereq of CIRCT)
chisel3.AddImplicitOutputAnnotationFile
        ↓
circt.Checks                     ← (optionalPrerequisiteOf CIRCT)
        ↓
    CIRCT                        ← 调用 firtool，产出 EmittedVerilogCircuitAnnotation
```

> 待本地验证：互不依赖的阶段（如 `circt.AddImplicitOutputFile` 与 `chisel3.AddImplicitOutputAnnotationFile`）的相对先后，取决于 `transformOrder` 的具体排序实现，可在本机用 `-verbose-pass-executions` 或日志确认。

#### 4.3.5 小练习与答案

**练习 1**：`PhaseManager` 的 `currentState` 参数有什么作用？

**参考答案**：`currentState` 表示「这些阶段假定已经跑过、其效果已存在」。调度时，`currentState` 里的阶段及其前提**不会被拉进执行图**（在 `prerequisiteGraph` 的 `start = _targets &~ _currentState`、`blacklist = _currentState` 处体现），排序后还会 `dropWhile` 掉它们。`class ChiselStage.run` 用它跳过 `AddDefaults`、`Checks`（因为外层 `Stage` 框架已跑过）。

**练习 2**：如果两个 `Phase` 互相把对方写进 `prerequisites`，会发生什么？

**参考答案**：依赖图会出现环。`PhaseManager` 在拓扑排序时（`cyclePossible("prerequisites", dependencyGraph) { DiGraph(edges).linearize ... }`，[DependencyManager.scala:222](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/firrtl/src/main/scala/firrtl/options/DependencyManager.scala#L222)）会捕获 `CyclicException` 并抛出 `DependencyManagerException`，列出环中的强连通分量。

---

### 4.4 Shell 与 CLI：命令行如何变成注解

#### 4.4.1 概念说明

「Stage」比「Phase」多的那一件事，就是**命令行界面**。`Stage` 持有一个 `shell: Shell`，`Shell` 内部是一个基于 [scopt](https://github.com/scopt/scopt) 的 `OptionParser`。每个命令行选项（如 `--target`、`--firtool-option`）背后都绑定了一个 `Annotation` 子类型；解析成功后，这些字符串就变成了一批 `Annotation`，拼到输入 `AnnotationSeq` 前面，再交给 `run`（即你搭的 `PhaseManager` 管道）。

所以命令行只是「产生注解的另一种途径」，与你在 Scala 代码里手写 `Seq(CIRCTTargetAnnotation(...), FirtoolOption(...))` 完全等价。这就是为什么 `emitSystemVerilog` 既能用 `args: Array[String]` 接命令行参数，又能用对象 API 接注解。

#### 4.4.2 核心流程

`Stage.execute` 的链路：

```
execute(args, initialAnnotations)
  → shell.parse(args, initialAnnotations)      // scopt 解析，产出 AnnotationSeq
  → transform(annos)                            // Stage.transform
      → AddDefaults / Checks / run(你的管道) / WriteOutputAnnotations  (见 §4.4.3)
```

而 `StageMain.main(args)` 就是 `(new Stage).execute(args, Seq.empty)` 的薄封装，让一个 `Stage` 成为可执行程序。

#### 4.4.3 源码精读

`Stage` 抽象基类——它本身就是个 `Phase`，多了 `shell` 和 `run`：

[Stage.scala:18-28](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/firrtl/src/main/scala/firrtl/options/Stage.scala#L18-L28) —— `abstract class Stage extends Phase`，抽象成员 `val shell: Shell` 与 `protected def run(annotations)`。注释明确：「A Stage is, conceptually, a Phase that includes a command line interface.」

`execute` 把命令行和注解合流：

[Stage.scala:77-79](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/firrtl/src/main/scala/firrtl/options/Stage.scala#L77-L79) —— `execute(args, annotations) = transform(shell.parse(args, annotations))`。`shell.parse` 先把字符串变注解，再进 `transform`。

`Stage.transform` 在你的 `run` 外面又套了几个固定阶段：

[Stage.scala:45-59](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/firrtl/src/main/scala/firrtl/options/Stage.scala#L45-L59) —— 用 `GetIncludes` →（`Logger.makeScope` 内）`AddDefaults`、`Checks`、一个调用 `run` 的匿名 `Phase`、`WriteOutputAnnotations` 包夹你的管道。这解释了为什么 `class ChiselStage.run` 的 `currentState` 要把 `AddDefaults`、`Checks` 标成「已跑」——它们已经被这层外衣跑过了。

`StageMain` 提供命令行 `main`：

[Stage.scala:84-100](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/firrtl/src/main/scala/firrtl/options/Stage.scala#L84-L100) —— `class StageMain(val stage)` 的 `main(args)` 就是 `stage.execute(args, Seq.empty)`，并处理 `StageError` / `OptionsException` 的退出码。

解析骨架在 `BareShell`：

[Shell.scala:19-41](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/firrtl/src/main/scala/firrtl/options/Shell.scala#L19-L41) —— `BareShell` 持有 scopt `parser`，`parse(args, initAnnos)` 调 `parser.parse(...)`，失败则抛 `OptionsException`。`Shell`（第 48 行起）在此基础上预置了 `TargetDirAnnotation`、日志等通用选项。

Chisel 自己的选项在 `circt.stage.CLI` trait 里集中声明：

[Shell.scala:30-72](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/Shell.scala#L30-L72) —— `trait CLI extends BareShell`，分三段往 `parser` 注册选项：日志选项（第 38–41 行）、Chisel 选项（第 43–62 行，如 `ChiselGeneratorAnnotation`、`UseLegacyWidthBehavior`、`EmitDebugIntrinsicsAnnotation`）、CIRCT 选项（第 64–71 行，如 `CIRCTTargetAnnotation`、`SplitVerilog`、`FirtoolBinaryPath`、`FirtoolOption`）。`ChiselStage` 的 `shell` 就是 `new firrtl.options.Shell("circt") with CLI`（[ChiselStage.scala:25](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala#L25)）。

每个注解类型如何绑定到具体 `--xxx` 选项，看 `CIRCTTargetAnnotation` 的伴生：

[Annotations.scala:67-86](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/Annotations.scala#L67-L86) —— `object CIRCTTargetAnnotation extends HasShellOptions`，`options` 里用一个 `ShellOption` 把 `--target` 的取值（`chirrtl`/`firrtl`/.../`btor2`）映射成对应的 `CIRCTTargetAnnotation`。这就是命令行字符串 `--target systemverilog` 变成注解 `CIRCTTargetAnnotation(CIRCTTarget.SystemVerilog)` 的代码点。同理 [Annotations.scala:124-135](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/Annotations.scala#L124-L135) 把 `--firtool-option` 绑到 `FirtoolOption`。

#### 4.4.4 代码实践

**实践目标**：用一条命令行调用 `ChiselMain`，验证命令行选项确实经 `Shell` 变成注解并驱动了管道。

**操作步骤**：

1. 在 [ChiselStage.scala:258](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala#L258) 确认入口类全名是 `circt.stage.ChiselMain`。
2. 参照 [README.md:186-195](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/README.md#L186-L195) 的示例，理解 `(new ChiselStage).execute(Array("--target","systemverilog"), Seq(ChiselGeneratorAnnotation(...)))` 等价于命令行 `--target systemverilog`。
3. 在本机用 mill 启动一个交互式 REPL 或写一个最小 main，执行：
   ```scala
   (new circt.stage.ChiselStage).execute(
     Array("--target", "chirrtl", "--target-dir", "build"),
     Seq(chisel3.stage.ChiselGeneratorAnnotation(() => new chisel3.RawModule {
       override def desiredName = "Empty"
     }))
   )
   ```
4. 故意把 `--target` 写成不存在的值（如 `--target wat`），观察报错。

**需要观察的现象**：合法调用会在 `--target-dir` 目录下产出 CHIRRTL 文件；非法 `--target` 会抛 `OptionsException: Unknown target name 'wat'!`（来自 [Annotations.scala:79](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/Annotations.scala#L79)）。

**预期结果**：你能把「命令行字符串 → `ShellOption.toAnnotationSeq` → `Annotation` → 进入 `PhaseManager` 管道」这条链用一句话说清。

> 待本地验证：实际产出路径与文件名取决于 `--target-dir` 和 `OutputFileAnnotation`，请在自己的环境里跑一次确认。

#### 4.4.5 小练习与答案

**练习 1**：`Stage` 与 `Phase` 的关系是什么？

**参考答案**：`Stage extends Phase`（[Stage.scala:19](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/firrtl/src/main/scala/firrtl/options/Stage.scala#L19)）。`Stage` 是「带命令行界面的 `Phase`」：它额外持有 `shell`，能经 `execute(args, annos)` 把命令行解析成注解；其 `transform` 还在用户的 `run` 外面套了 `AddDefaults`/`Checks`/`WriteOutputAnnotations` 等固定阶段。`Phase` 是更底层的「无界面的纯变换」。

**练习 2**：`--target systemverilog` 这串字符串最终如何影响管道跑哪些阶段？

**参考答案**：`Shell` 的 scopt 解析器按 `CIRCTTargetAnnotation` 绑定的 `ShellOption`（[Annotations.scala:69-84](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/Annotations.scala#L69-L84)）把字符串变成注解 `CIRCTTargetAnnotation(SystemVerilog)`，注入 `AnnotationSeq`。下游 `circt.stage.phases.CIRCT` 读这个注解（`view[CIRCTOptions]`），据此决定调用 firtool 时加哪些参数、最终产出 `EmittedVerilogCircuitAnnotation`（详见 u5-l4）。

---

### 4.5 AnnotationSeq：贯穿整条管道的数据载体

#### 4.5.1 概念说明

前面四个模块反复出现「注解」。本模块收尾把这条线讲透：**`AnnotationSeq`（即 `Seq[Annotation]`）是贯穿整条管道的唯一数据载体**。

每个 `Phase.transform(annos): AnnotationSeq` 都是「读一批注解、写一批注解」。电路产物（细化结果、FIRRTL IR、Verilog 字符串、要写的文件）全都以特定 `Annotation` 子类的形式被装进这个 `Seq` 里流动。`PhaseManager` 的作用只是给这些 `Phase` 排序；真正「数据」全在 `AnnotationSeq` 里。

因为注解是**扁平**的（一个 `Seq` 里混着几十种类型），读起来不方便。所以 Chisel 提供了 **OptionsView** 机制：用一个 `view[Options](annos)` 把散落的注解「投影」成一个结构化配置对象（如 `ChiselOptions`），供阶段内部方便地读取配置。

#### 4.5.2 核心流程

注解的「产生—消费」循环：

```
产生:  命令行 Shell.parse  /  调用方手写  /  某 Phase.transform 的返回值
   ↓
流动:  AnnotationSeq  ──(Phase A.transform)──▶  AnnotationSeq  ──(Phase B)──▶  ...
   ↓
消费:  collectFirst { case 某注解 => ... }   // 取出最终产物（如 Verilog 字符串）
        或 view[ChiselOptions](annos)         // 投影成结构化配置读取
```

#### 4.5.3 源码精读

`ChiselStage.emitSystemVerilog` 取最终产物就是典型的「消费」：

[ChiselStage.scala:206-212](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala#L206-L212) —— `phase.transform(annos).collectFirst { case EmittedVerilogCircuitAnnotation(a) => a }.get.value`。CIRCT 阶段把 Verilog 字符串包成 `EmittedVerilogCircuitAnnotation` 塞进 `AnnotationSeq`，这里再 `collectFirst` 抠出来。

`ChiselOptionsView` 演示了「投影」：

[package.scala:17-45](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/package.scala#L17-L45) —— `implicit object ChiselOptionsView extends OptionsView[ChiselOptions]`，`view(options)` 用 `collect{ case a: ChiselOption => a }` 把注解筛出来，再 `foldLeft` 进一个 `ChiselOptions` 配置对象。比如遇到 `ThrowOnFirstErrorAnnotation` 就 `c.copy(throwOnFirstError = true)`（第 24 行），遇到 `ChiselCircuitAnnotation` 就把 `elaboratedCircuit` 塞进去（第 28–29 行）。这样 `Elaborate` 阶段用一句 `view[ChiselOptions](annotations)` 就能拿到所有配置，而不必手写一堆 `collect`。

`Elaborate` 就是这么用它的：

[Elaborate.scala:41-42](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala#L41-L42) —— `val chiselOptions = view[ChiselOptions](annotations)`、`val loggerOptions = view[LoggerOptions](annotations)`，随后这些配置被用来构造 `DynamicContext`（详见 u4-l1、u5-l2）。这正是 u1-l5 所说「注解在阶段间传递数据」的具体落地。

#### 4.5.4 代码实践

**实践目标**：体会「同一个信息既可用注解表达，也可用结构化配置读取」。

**操作步骤**：

1. 打开 [package.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/package.scala)，把 `ChiselOptionsView.view` 处理的注解类型逐条列出（`ThrowOnFirstErrorAnnotation`、`ChiselOutputFileAnnotation`、`ChiselCircuitAnnotation`、`UseLegacyWidthBehavior` 等）。
2. 对照这些注解类型的定义（在 `chisel3.stage` 包内，可自行 `Grep` `case class ThrowOnFirstErrorAnnotation` 等），确认它们都是 `Annotation` 的子类。
3. 在 [Elaborate.scala:41-63](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala#L41-L63) 里数一数 `chiselOptions.xxx` 被读取了多少次，体会「注解 → 配置 → 使用」的三段式。

**需要观察的现象**：`view` 把扁平注解聚合成了一个对象，阶段代码读起来就像读普通配置类。

**预期结果**：你能解释为什么新增一个命令行选项需要同时改三处：① 定义 `Annotation` 子类（及其 `HasShellOptions`）；② 在 `CLI` 里 `addOptions`；③ 在对应的 `OptionsView` 里加一个 `case` 分支。

#### 4.5.5 小练习与答案

**练习 1**：`emitSystemVerilog` 跑完管道后，Verilog 字符串「藏」在哪里？怎么取出来？

**参考答案**：藏在返回 `AnnotationSeq` 里的一个 `EmittedVerilogCircuitAnnotation(EmittedVerilogCircuit(...))` 注解中。用 `collectFirst { case EmittedVerilogCircuitAnnotation(a) => a }.get.value` 取出字符串（[ChiselStage.scala:208-212](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala#L208-L212)）。

**练习 2**：`ChiselOptionsView` 解决了什么问题？

**参考答案**：注解是扁平的 `Seq[Annotation]`，直接用要在每个阶段写一堆 `collect`/模式匹配。`ChiselOptionsView.view` 把相关注解**一次性投影**成结构化的 `ChiselOptions` 对象（`foldLeft` 累积各字段），阶段只需 `view[ChiselOptions](annotations)` 就能像读配置类一样取值（[package.scala:20-43](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/package.scala#L20-L43)）。

---

## 5. 综合实践

**任务**：把本讲四个模块串起来——读懂「一条命令行从敲下回车到产出 Verilog」的全链路，并画出一张完整的「管道顺序图」。

**操作步骤**：

1. **起点**：假设用户执行 `circt.stage.ChiselMain` 并传入 `--target systemverilog --module ...`。从 [ChiselStage.scala:259](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala#L259) 的 `StageMain.main` → `stage.execute` → `shell.parse`（[Stage.scala:77-79](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/firrtl/src/main/scala/firrtl/options/Stage.scala#L77-L79)），把命令行变注解。
2. **组装**：进入 `class ChiselStage.run`（[ChiselStage.scala:30-49](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala#L30-L49)），`new PhaseManager(targets, currentState)`。
3. **调度**：对照 §4.3，说明 `PhaseManager` 如何沿 `prerequisites` 自动补入 `Elaborate`，并拓扑排序出执行序列。
4. **流动**：对照 §4.5，说明 `AnnotationSeq` 如何从「只有 `ChiselGeneratorAnnotation`」一步步被各阶段追加（`ChiselCircuitAnnotation` → `FirrtlCircuitAnnotation` → … → `EmittedVerilogCircuitAnnotation`）。
5. **产出**：画一张从左到右的流程图，包含：`Shell.parse` → `PhaseManager.transformOrder`（列出至少 `Elaborate → Convert → CIRCT` 主干）→ 最终 `AnnotationSeq` 里的 `EmittedVerilogCircuitAnnotation`。

**验收标准**：你的图里应当能回答这三个问题——

- 命令行字符串在哪一行变成注解？
- `Elaborate` 为什么会跑（尽管没在 `run` 的 `targets` 里）？
- 最终 Verilog 字符串以什么注解形式存在、由哪个阶段产出？

> 待本地验证：若想看真实执行顺序与各阶段耗时，可在命令行加日志相关选项（如提高 log level），`DependencyManager.transform`（[DependencyManager.scala:277-282](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/firrtl/src/main/scala/firrtl/options/DependencyManager.scala#L277-L282)）会打印每个阶段的 `Starting/Finished` 与耗时，据此核对你在步骤 5 画的顺序图。

## 6. 本讲小结

- `circt.stage.ChiselStage` 有两条 `PhaseManager` 管道：`class ChiselStage.run`（命令行）与 `object ChiselStage.phase`（库 API），`targets` 不同但依赖自动补全后执行到的阶段集合大体一致。
- 编译管道由若干 `Phase`（`AnnotationSeq => AnnotationSeq` 的数学变换）组成，每个 `Phase` 用 `prerequisites` / `optionalPrerequisites` / `optionalPrerequisiteOf` / `invalidates` 四组依赖关系声明调度需求，**而非写死顺序**。
- `PhaseManager` 从 `targets` 出发沿 `prerequisites` 反向 BFS 自动补入前提（故 `Elaborate` 即使不在 `targets` 也会被拉进来），再对依赖 DAG 拓扑排序得到 `transformOrder`；`invalidates` 触发子问题重跑。
- `Stage` 是「带命令行的 `Phase`」：经 `Shell`（scopt 解析器）把 `--target`/`--firtool-option` 等字符串经 `ShellOption` 变成 `Annotation`；`StageMain` 再把 `Stage` 包成可执行 `main`。
- `AnnotationSeq`（`Seq[Annotation]`）是贯穿全管道的唯一数据载体：电路、配置、产物都以注解形式流动；`OptionsView`（如 `ChiselOptionsView`）把扁平注解投影成结构化配置供阶段读取，`collectFirst` 取出最终产物。

## 7. 下一步学习建议

本讲把「管道怎么搭、怎么调度」讲完了，但故意没深入每个阶段内部。接下来按顺序：

1. **u5-l2 Elaborate 阶段**：打开 `Elaborate.transform`，看它如何 new 出 `DynamicContext`、调 `Builder.build` 把 Scala 构造体细化成 `ElaboratedCircuit`（承接 u4-l1 的 Builder 全局状态机）。
2. **u5-l3 Convert / Checks / Emitter 阶段**：看 `Convert` 如何调 `Converter`（承接 u4-l5）把内部 IR 翻成 `firrtl.ir.Circuit`，以及 `Checks` 如何在跑 firtool 前校验注解合法性。
3. **u5-l4 CIRCT 集成：调用 firtool**：打开 `circt.stage.phases.CIRCT`，看它如何拼 firtool 命令行、用 `os.proc` 调起 firtool、解析返回的 Verilog 并包成 `EmittedVerilogCircuitAnnotation`——这是整条管道的终点。
4. 若对调度算法本身感兴趣，可回头精读 `DependencyManager.transformOrder`（[DependencyManager.scala:205-245](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/firrtl/src/main/scala/firrtl/options/DependencyManager.scala#L205-L245)）及其 `dependenciesToGraphviz`，用 Graphviz 可视化某条管道的依赖图。
