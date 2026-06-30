# 编译流程总览：从前端到 CIRCT

## 1. 本讲目标

前四讲我们分别建立了三件事：Chisel 是嵌在 Scala 里的硬件构造 DSL（u1-l1）、仓库怎么构建与发布（u1-l2、u1-l3）、以及一个最简 `Module` 如何靠 `ChiselStage.emitSystemVerilog` 这一行「按下生成按钮」产出 Verilog（u1-l4）。

但那「一行代码」背后到底发生了什么？本讲就要把黑盒打开，给你一张**贯穿全局的心智地图**。学完后你应当能够：

1. 说出 Chisel 编译器的**四段式架构**：前端 API → Builder → 内部 FIRRTL IR → 发射器/CIRCT，并理解每一段的职责边界。
2. 在源码中**精确定位**这四段分别对应哪些文件、哪些类、哪些方法。
3. 理解 `ChiselStage` 是如何用一个 **Stage / Phase 管道**把这几段串起来的，以及为什么这一讲只看「总览」、细节留给后续单元。

本讲是后续 u4（Builder 内部机制）、u5（Stage 与 CIRCT 集成）两单元的**导航图**——我们先建立鸟瞰，再逐层下钻。

---

## 2. 前置知识

阅读本讲前，建议你已经理解下面几个概念（前四讲已覆盖）：

- **EDSL（嵌入式 DSL）**：Chisel 没有独立编译前端，你写的 `Module` 本质是合法的 Scala 程序；运行时执行这些程序会「长出」电路，这个过程叫 **elaboration（细化）**。
- **生成器方法学**：用 Scala 的参数、循环、集合来批量生成硬件，而不是一行行手写 RTL。
- **字面量与隐式转换**：`.U/.S/.B/.W` 这些后缀来自 `chisel3` 包对象里的隐式类。
- **模块构造体只是「记录命令」**：你在 `Module` 里写的 `IO(...)`、`:=`、`when` 并不会立刻生成硬件或文件，而是被记录下来，留到 elaboration 结束时统一处理。

如果你对上面任何一条感到陌生，请先回到 u1-l1 与 u1-l4 复习。本讲会出现两个**新术语**，先解释清楚：

| 术语 | 含义 |
| --- | --- |
| **IR（Intermediate Representation，中间表示）** | 编译器内部用来描述程序的数据结构。Chisel 有一套「仿 FIRRTL 语法」的内部 IR，是 Scala 对象，不是文本。 |
| **Phase（阶段）/ Stage（舞台）** | 把编译过程拆成一系列可组合的小步骤，每个 Phase 负责一步（如细化、转换、检查、发射）。Phase 之间通过「注解（Annotation）」传递数据。 |
| **CIRCT / firtool** | LLVM 旗下的硬件编译器框架；`firtool` 是它的命令行工具，负责把 FIRRTL 方言的 IR 编译成 SystemVerilog。 |

---

## 3. 本讲源码地图

本讲围绕 README 的「Architecture Overview」组织，涉及的文件如下：

| 文件 | 角色 | 本讲用途 |
| --- | --- | --- |
| [`README.md`](README.md) | 项目说明 | 第 343–355 行的「Chisel Architecture Overview」是四段式架构的官方描述，本讲以它为骨架。 |
| [`core/src/main/scala/chisel3/internal/Builder.scala`](core/src/main/scala/chisel3/internal/Builder.scala) | Builder 全局状态 | 维护 elaboration 期间的全局状态（当前模块、命令队列），并收尾产出 IR。 |
| [`core/src/main/scala/chisel3/internal/firrtl/IR.scala`](core/src/main/scala/chisel3/internal/firrtl/IR.scala) | 内部 FIRRTL IR | 定义 `Command`、`Component`、`Circuit` 等 IR 节点。 |
| [`core/src/main/scala/chisel3/internal/firrtl/Converter.scala`](core/src/main/scala/chisel3/internal/firrtl/Converter.scala) | IR 转换器 | 把 Chisel 内部 IR 转成 `firrtl.ir.Circuit`，交给下游。 |
| [`src/main/scala/chisel3/stage/phases/Elaborate.scala`](src/main/scala/chisel3/stage/phases/Elaborate.scala) | Elaborate 阶段 | 创建 `DynamicContext` 并调用 `Builder.build`，是「前端 + Builder」的入口。 |
| [`src/main/scala/chisel3/stage/phases/Convert.scala`](src/main/scala/chisel3/stage/phases/Convert.scala) | Convert 阶段 | 调用 `Converter`，把 IR 转成 FIRRTL IR 注解（注意：本阶段已标记 deprecated）。 |
| [`src/main/scala/circt/stage/ChiselStage.scala`](src/main/scala/circt/stage/ChiselStage.scala) | Stage/Phase 管道 | 用 `PhaseManager` 把上述阶段串成管道，并提供 `emitSystemVerilog` 等用户 API。 |

> 子项目归属提醒（承接 u1-l3）：触及 `chisel3.internal` 私有 API 的 `Builder`/`IR`/`Converter` 都在 `core` 子项目；`Elaborate`/`Convert`/`ChiselStage` 这些「胶水阶段」在 `src/main`（即 `chisel`）子项目。

---

## 4. 核心概念与源码讲解

### 4.1 四段式架构：先建立直觉

#### 4.1.1 概念说明

README 的「Chisel Architecture Overview」一节用四句话概括了整个编译器，这是本讲最重要的出处：

[README.md:343-355](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/README.md#L343-L355) —— 官方对四段式架构的原文描述。

把它翻译成中文直觉，就是一条单向流水线：

```
┌─────────────┐   push    ┌──────────┐  收尾   ┌───────────────┐  转换   ┌──────────────┐  firtool  ┌────────────┐
│ ① 前端 API  │ ────────▶ │ ② Builder│ ──────▶ │③ 内部 FIRRTL IR│ ──────▶│ ④ 发射/转换  │ ────────▶ │SystemVerilog│
│  chisel3.*  │  命令     │ 全局状态 │         │   Circuit     │        │ Converter+CIRCT│          │   .sv      │
└─────────────┘           └──────────┘         └───────────────┘        └──────────────┘           └────────────┘
   你写的 Scala           记录谁在                  一棵 Scala              把内部 IR 转成
   生成器代码             哪个模块、                 对象树，                firrtl.ir，再交给
   每行只是「登记」        收集命令                  语法酷似 FIRRTL          firtool 编译
```

四段各自的「性格」可以这样区分：

- **① 前端 API（`chisel3.*`）**：是**用户可见的词汇表**（`Module`、`IO`、`UInt`、`Reg`、`when`…）。它的特点是「**只登记，不施工**」——每次调用都把一条信息塞进 Builder，自己不产生任何硬件或文件。
- **② Builder（`chisel3.internal.Builder`）**：是 elaboration 期间的**全局状态机**。它知道「现在正在构造哪个模块」「到目前为止收集了哪些命令」，并负责在最后把散落的命令收拢成一棵完整的电路树。
- **③ 内部 FIRRTL IR（`chisel3.internal.firrtl.*`）**：是 Builder 收集命令所用的**数据结构**。它是一组 Scala case class，语法上和 FIRRTL 语言很像（所以叫「仿 FIRRTL」），顶层对象是 `Circuit`。
- **④ 发射/转换**：把内部 IR 变成下游能消化的形式。历史上是「FIRRTL Emitter」直接打印成 `.fir` 文本；现代 Chisel 则是先由 `Converter` 转成 `firrtl.ir.Circuit`，再由 CIRCT（`firtool`）编译成 SystemVerilog。

> ⚠️ **一个必须指出的细节**：README 把第 ③ 段写成 `chisel3.firrtl.*`、第 ④ 段写成 `chisel3.firrtl.Emitter`。但**当前源码里这些类型实际位于 `chisel3.internal.firrtl` 包下**（带 `.internal`），且现代发射路径已由 `Converter` + CIRCT 取代了独立的 `Emitter`。这说明 README 的架构图是**概念模型**，源码包名随版本演进而有所调整。读源码时以「概念四段」为索引，但定位文件时要认 `chisel3.internal.firrtl`。

#### 4.1.2 核心流程

把四段串成一次完整的 Verilog 生成，流程是：

1. 用户调用 `ChiselStage.emitSystemVerilog(new MyMod)`（前端入口）。
2. `ChiselStage` 构造一个 `PhaseManager`，按依赖顺序依次跑若干 Phase。
3. **Elaborate** 阶段：构造 `DynamicContext`，调用 `Builder.build(Module(gen()))`。
4. `Builder` 执行用户的 Scala 构造体；每条 `IO/Reg/when/:=` 都通过 `pushCommand` 登记一条 `Command`。
5. 构造体跑完后，`Builder.build` 把命令收拢成 `Circuit`（内部 IR），包进 `ElaboratedCircuit`。
6. **Convert** 阶段：`Converter.convert` 把 `Circuit` 转成 `firrtl.ir.Circuit`（已 deprecated，新代码倾向直接用 `ElaboratedCircuit`）。
7. **CIRCT** 阶段：调用 `firtool` 把 IR 编译成 SystemVerilog 字符串返回。

如果用伪代码概括，就是：

```
def emitSystemVerilog(gen):
    annos = [ChiselGeneratorAnnotation(gen), CIRCTTargetAnnotation(SystemVerilog)]
    phase.transform(annos)           # 跑 PhaseManager 管道
        .collect(EmittedVerilogCircuitAnnotation)   # 取出最终 Verilog 注解
        .value
```

#### 4.1.3 源码精读

对应到本讲用到的源码：

- 四段式架构的原文出处：[README.md:345-350](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/README.md#L345-L350) 列出 frontend / Builder / 中间数据结构 / Firrtl emitter 四部分；[README.md:354-355](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/README.md#L354-L355) 补充了「标准库 `chisel3.util.*`」与「Chisel Stage `chisel3.stage.*`」两个附加部件。注意 Stage 这一项正是本讲 4.4 节的主角。

#### 4.1.4 代码实践

- **实践目标**：用本节的四段式模型，**口述**一遍 u1-l4 里那个加法器从 Scala 代码到 Verilog 经过了哪四段。
- **操作步骤**：回顾 u1-l4 你写的 `Adder` 模块，对照上面的流水线图，写出每一行代码分别落在「① 前端 API」的哪一部分。
- **观察现象**：你会注意到 `class Adder extends Module { ... }` 整个类体都属于 ① 前端；只有最后那行 `ChiselStage.emitSystemVerilog(new Adder)` 才触发了 ②③④。
- **预期结果**：你能清晰说出「模块类体 = 前端登记」与「`emitSystemVerilog` = 触发后续三段」的分界。
- **运行结果**：待本地验证（本节是概念梳理，无需运行命令）。

#### 4.1.5 小练习与答案

**练习 1**：README 说前端 API「just add data to the...（只是把数据加到……）」。这个「……」指的是哪一段？
> **答案**：指的是 ② Builder。前端每次调用都把一条信息（命令）登记进 Builder 维护的全局状态。

**练习 2**：README 把第 ③ 段写成 `chisel3.firrtl.*`，但实际源码包名是什么？为什么会有差异？
> **答案**：实际是 `chisel3.internal.firrtl`（带 `.internal`）。差异是因为 README 描述的是**概念模型**，而源码包名会随重构调整；以源码为准。

---

### 4.2 第一段：前端 API（`chisel3.*`）与命令登记

#### 4.2.1 概念说明

前端 API 就是你 `import chisel3._` 之后能用到的那套词汇。它的本质是一个**只写不施工的登记薄**：你在模块类体里写的每一行，都会在 elaboration 时被翻译成一条「命令（Command）」，塞进当前模块的命令队列。

这一点非常反直觉：你写了 `val r = RegNext(in)`，**此时并没有任何寄存器被创建**——只是登记了一条「这里需要一个寄存器」的命令。真正的硬件要到后续阶段才成型。

#### 4.2.2 核心流程

前端 API → Builder 命令登记的关键链路：

1. 用户的 Scala 构造体在 `Builder` 上下文里执行（由 Elaborate 阶段搭好，见 4.4）。
2. 一条 API（例如 `RegNext`）内部最终调用 `Builder.pushCommand(cmd)`。
3. `pushCommand` 把命令追加到**当前模块**（`forcedUserModule`）的命令列表里。

#### 4.2.3 源码精读

登记动作的实现只有三行，但它是理解「前端只登记」的关键：

[core/src/main/scala/chisel3/internal/Builder.scala:895-898](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L895-L898) —— `pushCommand` 把一条 `Command` 交给当前用户模块的 `addCommand`，**仅做记录，不生成硬件**。

紧挨着的 `pushOp` 是更常用的入口（运算类命令走这里），它多做一步——把返回的 `Data` 绑定成 `OpBinding`（运算结果绑定），这关系到 u4-l3 的 Binding 系统，本讲只作了解：

[core/src/main/scala/chisel3/internal/Builder.scala:899-903](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L899-L903) —— `pushOp` 先给运算结果打上 `OpBinding`，再复用 `pushCommand` 登记。

#### 4.2.4 代码实践

- **实践目标**：体会「前端只是登记」。
- **操作步骤**：写一个最小模块，在 `Module` 类体里只写 `val r = RegNext(io.in)`，**不要**调用 `emitSystemVerilog`，直接在 `App` 里 `new` 出这个模块对象。
- **观察现象**：你会发现什么 Verilog 都不会生成；甚至模块对象本身也只是个普通 Scala 对象。
- **预期结果**：直观验证「前端 API 调用本身不触发任何输出，必须由 `ChiselStage` 启动后续阶段」。
- **运行结果**：待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么说前端 API「只登记不施工」？用 `pushCommand` 的实现解释。
> **答案**：因为 `pushCommand`（Builder.scala:895）只是把命令追加进当前模块的命令队列（`addCommand`），没有任何写文件或生成 IR 顶层对象的动作；硬件要等 Builder 收尾和后续阶段才成型。

**练习 2**：`pushOp` 比 `pushCommand` 多做了什么？
> **答案**：多了一步 `cmd.id.bind(OpBinding(...))`，即把运算返回的 `Data` 标记为「运算结果绑定」，然后再调 `pushCommand` 登记。

---

### 4.3 第二段 + 第三段：Builder 全局状态与内部 FIRRTL IR

> 把 ②③ 合并讲，是因为它们在源码里紧密耦合：Builder **用** IR 的数据结构来登记命令，并在收尾时把它们组装成顶层 `Circuit`。

#### 4.3.1 概念说明

**Builder** 是 elaboration 期间的「全局变量持有者」。Chisel 用 Scala 的 `DynamicVariable`（动态作用域）在调用栈里隐式传递一个 `DynamicContext`，里面装着所有可变状态。其中最关键的两块是：

- **`currentModule`**：当前正在构造的模块。前端 API 调用 `pushCommand` 时，要知道把命令塞给谁，就靠它。
- **`components`**：所有已构造出的模块组件（`Component`）列表，elaboration 结束后构成整棵电路树。

**内部 FIRRTL IR** 是 Builder 登记命令所用的数据结构，定义在 `chisel3.internal.firrtl` 包里。它的核心是三层：

- **`Command`（命令）**：一条登记记录的基类，例如定义一个寄存器、定义一根线、一次连接。
- **`Component`（组件）**：一个模块的完整描述（端口 + 命令块），是 `Circuit` 的成员。
- **`Circuit`（电路）**：顶层对象，包含所有 `Component`、注解、层（layer）等，是整棵 IR 树的根。

#### 4.3.2 核心流程

Builder 收尾组装 IR 的过程（伪代码）：

```
Builder.build(Module(gen()), context):
    dynamicContextVar.withValue(Some(context)):     # 把 DynamicContext 挂到调用栈
        mod = gen()                                  # 执行用户构造体 → 大量 pushCommand
        mod._forceName(...)                          # 给模块取名
        errors.checkpoint(logger)                    # 汇报收集到的错误
        # 把 components / layers / options 组装成 Circuit，包进 ElaboratedCircuit
        return (ElaboratedCircuit(...), mod)
```

阶段之间的数据传递可以用一个简单的集合关系描述。设一次 elaboration 登记的命令集合为 \(C\)、模块组件集合为 \(M\)，则最终电路满足：

\[
\text{Circuit} = \langle\, name,\ \underbrace{M}_{components},\ annotations,\ layers,\ \ldots\,\rangle,\qquad
\text{每个 } m \in M \text{ 内含 } C_m \subseteq C
\]

即顶层 `Circuit` 持有所有 `Component`，而每个 `Component` 内部持有它自己的命令序列。这正是「模块化」在 IR 层的体现。

#### 4.3.3 源码精读

**Builder 全局状态**：`DynamicContext` 类定义及其关键字段——

[core/src/main/scala/chisel3/internal/Builder.scala:465-465](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L465) —— `class DynamicContext` 的起始，构造参数里包含 `annotationSeq`、`throwOnFirstError`、`useLegacyWidth` 等一系列 elaboration 选项。

[core/src/main/scala/chisel3/internal/Builder.scala:533-533](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L533) —— `val components = ArrayBuffer[Component]()`，这是所有已构造模块组件的收集处，最终塞进 `Circuit.components`。

[core/src/main/scala/chisel3/internal/Builder.scala:538-538](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L538) —— `var currentModule: Option[BaseModule]`，当前正在构造的模块；`pushCommand` 就是把命令追加给它。

**Builder 收尾入口**：

[core/src/main/scala/chisel3/internal/Builder.scala:1054-1062](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L1054-L1062) —— `def build[T <: BaseModule](f, dynamicContext): (ElaboratedCircuit, T)`，这是 Builder 的对外入口，返回的 `ElaboratedCircuit` 就是封装好的内部 IR。它把实际工作委托给 `buildImpl`（Builder.scala:1064 起），后者用 `dynamicContextVar.withValue(Some(dynamicContext))` 把上下文挂到调用栈，再执行 `f`（你的模块构造体）。

**IR 节点定义**：

[core/src/main/scala/chisel3/internal/firrtl/IR.scala:313-320](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L313-L320) —— `abstract class Command`（命令基类）与 `abstract class Definition extends Command`（带 `id` 的定义类命令）。`DefPrim`/`DefWire`/`DefReg` 等都是 `Definition` 的子类。

[core/src/main/scala/chisel3/internal/firrtl/IR.scala:322-330](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L322-L330) —— 几个最典型的命令节点：`DefPrim`（运算）、`DefWire`（线）、`DefReg`（寄存器）、`DefRegInit`（带初值的寄存器）。当你写 `RegNext(in)`，最终就会登记出一条 `DefReg`/`DefRegInit` 加一条 `Connect`。

[core/src/main/scala/chisel3/internal/firrtl/IR.scala:483-483](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L483) —— `case class Connect(sourceInfo, loc, exp)`，对应 `:=` 连线命令。

[core/src/main/scala/chisel3/internal/firrtl/IR.scala:587-587](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L587) —— `abstract class Component extends Arg`，模块组件基类（注意它本身也是一种 `Arg`）。

[core/src/main/scala/chisel3/internal/firrtl/IR.scala:660-672](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L660-L672) —— `case class Circuit(name, components, annotations, ...)`，整棵 IR 树的顶层根节点；`Builder.build` 收尾时产出的就是它的封装 `ElaboratedCircuit`。

#### 4.3.4 代码实践

- **实践目标**：在源码里把「一条用户语句 → 一条 IR 命令」对上号。
- **操作步骤**：打开 IR.scala，浏览 313–490 行区间，列出 `Command` 的主要子类（`DefPrim`/`DefWire`/`DefReg`/`DefRegInit`/`Connect`/`DefInvalid` 等）。
- **观察现象**：你会发现这些 case class 的字段非常贴近 FIRRTL 语法（`sourceInfo` + 操作数 `Arg*`）。
- **预期结果**：写出一句话说明——当你写 `val r = RegNext(in)` 时，Builder 大约会登记一条 `DefReg`（声明寄存器）和一条 `Connect`（把 `in` 连到寄存器输入）。具体细节（几条命令、是否带初值）将在 u4-l2 精讲，本讲只需建立对应关系。
- **运行结果**：待本地验证（源码阅读型实践）。

#### 4.3.5 小练习与答案

**练习 1**：`DynamicContext` 里哪两个字段分别对应「当前模块」和「全部模块组件」？
> **答案**：`currentModule`（Builder.scala:538）对应当前模块；`components`（Builder.scala:533）对应全部模块组件。

**练习 2**：`Circuit`、`Component`、`Command` 三者的包含关系是什么？
> **答案**：`Circuit`（IR.scala:660）包含若干 `Component`；每个 `Component`（IR.scala:587）内部包含若干 `Command`（IR.scala:313）。即 Circuit ⊃ Component ⊃ Command 的三层树。

**练习 3**：为什么 `pushCommand` 需要先取到「当前模块」？
> **答案**：因为命令要登记到「正在构造的那个模块」的命令队列里，而「当前模块」由 Builder 的 `currentModule` 全局状态指示（Builder.scala:538）。

---

### 4.4 第四段 + 管道：Converter、CIRCT 与 ChiselStage

> 第 ④ 段「发射」在现代 Chisel 里拆成了两步：先用 `Converter` 把内部 IR 转成 `firrtl.ir.Circuit`，再由 CIRCT（`firtool`）编译成 SystemVerilog。而把所有阶段串起来的，是 `ChiselStage` 的 Phase 管道。

#### 4.4.1 概念说明

**Converter** 是内部 IR 与外部世界之间的「翻译官」。Chisel 内部的 `Circuit`（`chisel3.internal.firrtl.Circuit`）和 FIRRTL 项目里的 `firrtl.ir.Circuit` 是**两套不同的 IR**：前者是 Chisel 自己的、私有的；后者是 firrtl 库定义的、可被 CIRCT 消费的标准 IR。`Converter` 负责前者 → 后者的翻译。

**ChiselStage** 是用户最常接触的入口（u1-l4 已用过 `emitSystemVerilog`）。它的内部不是一坨过程式代码，而是一个 **`PhaseManager` 管道**：声明一组目标 Phase，由 PhaseManager 根据「前置依赖（prerequisites）/失效规则（invalidates）」自动算出执行顺序（本质是拓扑排序），逐个 `transform` 一串注解（`AnnotationSeq`）。

> 一个 Phase 依赖的数学直觉：把每个 Phase 看作有向图的一个节点，边 \(a \to b\) 表示「\(a\) 是 \(b\) 的前置」。PhaseManager 求的是这些依赖关系构成的有向无环图（DAG）的一个**拓扑序** \( (p_1, p_2, \ldots, p_n) \)，使得对任意依赖 \(a \to b\)，\(a\) 排在 \(b\) 之前：
> \[ \forall\, (a \to b) \in E,\quad \mathrm{pos}(a) < \mathrm{pos}(b) \]

#### 4.4.2 核心流程

`emitSystemVerilog` 的内部管道（以 `ChiselStage` 伴生对象的 `phase` 为准）：

```
emitSystemVerilog(gen, firtoolOpts):
    annos = [ChiselGeneratorAnnotation(gen),                      # 待细化的模块工厂
             CIRCTTargetAnnotation(SystemVerilog)]                # 告诉 CIRCT 要 SystemVerilog
           ++ firtoolOpts.map(FirtoolOption)                      # 透传给 firtool 的选项
    phase.transform(annos):                                       # PhaseManager 跑管道
        ① Elaborate   → Builder.build → ChiselCircuitAnnotation(内部 IR)
        ② AddDebugIntrinsics
        ③ Convert     → Converter.convert → FirrtlCircuitAnnotation(firrtl IR)  [deprecated]
        ④ AddDedupGroupAnnotations
        ⑤ AddImplicitOutputFile / AddImplicitOutputAnnotationFile
        ⑥ Checks
        ⑦ CIRCT       → 调 firtool → EmittedVerilogCircuitAnnotation(.sv)
    .collect(EmittedVerilogCircuitAnnotation).value               # 取出 Verilog 字符串
```

对照 README 的四段式：①对应「前端 + Builder」（Elaborate 把前端 API 跑起来并收拢成 IR），③对应「内部 IR → firrtl IR 的转换」，⑦对应「发射」——只不过现代发射由 CIRCT/firtool 完成，而不是 README 字面意义上的 `Emitter`。

#### 4.4.3 源码精读

**Converter 的翻译入口**：

[core/src/main/scala/chisel3/internal/firrtl/Converter.scala:23-23](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala#L23) —— `private[chisel3] object Converter`，所有重载的 `convert` 方法都在这个对象里。

[core/src/main/scala/chisel3/internal/firrtl/Converter.scala:548-548](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala#L548) —— `def convert(circuit: Circuit): fir.Circuit`，**最关键的翻译入口**：把 Chisel 内部 `Circuit` 翻译成 firrtl 库的 `firrtl.ir.Circuit`。其余 `convert` 重载（如 Converter.scala:484 把单个 `Component` 翻译成 `fir.DefModule`、133 行翻译单条 `Command`）都是它的递归帮手。

**Convert 阶段（已 deprecated）**：

[src/main/scala/chisel3/stage/phases/Convert.scala:16-28](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Convert.scala#L16-L28) —— `class Convert extends Phase`（自 Chisel 7.11.0 起 deprecated），它在 `transform` 里调用 `Converter.convert(a.elaboratedCircuit._circuit)` 产出 `FirrtlCircuitAnnotation`。注释明确说「转 FIRRTL IR 已弃用，请改用 `ElaboratedCircuit`」——这是现代 Chisel 正在把发射路径从「经 firrtl.ir」迁移到「直接用 `ElaboratedCircuit`」的信号，但当前管道里 `Convert` 仍在运行。

**ChiselStage 管道**：

[src/main/scala/circt/stage/ChiselStage.scala:18-18](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala#L18) —— `class ChiselStage extends Stage`，命令行/`execute` 入口用的就是这个类。

[src/main/scala/circt/stage/ChiselStage.scala:30-49](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala#L30-L49) —— `class ChiselStage` 的 `run` 方法，构造一个 `PhaseManager`，`targets` 里列出 `Convert`、`CIRCT` 等目标 Phase，由 PhaseManager 自动排序后 `pm.transform(annotations)`。

[src/main/scala/circt/stage/ChiselStage.scala:54-68](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala#L54-L68) —— `object ChiselStage`（伴生对象，提供更友好的 API）及其私有的 `phase`：一个 `PhaseManager`，目标序列依次是 `Elaborate → AddDebugIntrinsics → Convert → AddDedupGroupAnnotations → AddImplicitOutputFile → AddImplicitOutputAnnotationFile → Checks → CIRCT`。**这就是本讲 4.4.2 那张管道图的真实出处。**

[src/main/scala/circt/stage/ChiselStage.scala:197-213](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala#L197-L213) —— `def emitSystemVerilog(gen, args, firtoolOpts): String`，你在 u1-l4 用的那一行。它组装注解（含 `firtoolOpts.map(FirtoolOption(_))`），跑 `phase.transform`，最后从 `EmittedVerilogCircuitAnnotation` 里取出 `.value`（即 Verilog 字符串）。

**Elaborate 阶段（管道起点，连接 ①②③）**：

[src/main/scala/chisel3/stage/phases/Elaborate.scala:29-29](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala#L29) —— `class Elaborate extends Phase`，管道里负责「跑前端 + 收拢 IR」的阶段。

[src/main/scala/chisel3/stage/phases/Elaborate.scala:45-65](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala#L45-L65) —— 构造 `DynamicContext`（把 `ChiselOptions` 里的 `throwOnFirstError`、`useLegacyWidth`、`useSRAMBlackbox` 等字段灌进去），随后第 65 行 `Builder.build(Module(gen()), context)` 正式启动 elaboration。这是「Stage 管道」与「Builder 全局状态」的对接点。

[src/main/scala/chisel3/stage/phases/Elaborate.scala:74-77](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Elaborate.scala#L74-L77) —— elaboration 成功后，把结果包成 `ChiselCircuitAnnotation(elaboratedCircuit)` 返回，供下游 `Convert`/`CIRCT` 阶段消费。

#### 4.4.4 代码实践（本讲的主实践）

**实践目标**：对照 README 的 Architecture Overview，在源码里找到并标注四段（含 Converter）各自所在的文件路径，把「概念模型」落到「真实文件」。

**操作步骤**：

1. 打开 [README.md 的 Architecture Overview](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/README.md#L343-L355)，记下四段的官方命名（frontend / Builder / 中间数据结构 / Firrtl emitter）。
2. 在本地仓库里按下表「寻宝」，把每一段对应的**文件路径**与**关键行**填进去：

   | README 概念段 | 你要找的文件（相对仓库根） | 关键锚点 |
   | --- | --- | --- |
   | ① frontend `chisel3.*` | `core/src/main/scala/chisel3/package.scala`（及同目录各 API 文件） | 隐式类、`Module`、`IO` |
   | ② Builder `chisel3.internal.Builder` | `core/src/main/scala/chisel3/internal/Builder.scala` | `object Builder`（:563）、`def build`（:1054） |
   | ③ 中间数据结构 `chisel3.firrtl.*` | `core/src/main/scala/chisel3/internal/firrtl/IR.scala` | `case class Circuit`（:660） |
   | ④ 发射（现代：Converter + CIRCT） | `core/src/main/scala/chisel3/internal/firrtl/Converter.scala` | `def convert(circuit): fir.Circuit`（:548） |
   | 串联管道 `chisel3.stage.*` | `src/main/scala/circt/stage/ChiselStage.scala` | `private def phase`（:57） |

3. 用 `grep` 或编辑器跳转验证上述行号确实存在（本讲给出的行号均基于当前 HEAD `b2a0e030`）。

**观察现象**：你会清楚看到「README 的 `chisel3.firrtl.*`」在源码里实为 `chisel3.internal.firrtl`；而 README 说的「Firrtl emitter」在现代代码里是由 `Converter` + `circt.stage.phases.CIRCT` 共同承担的。

**预期结果**：产出一张「概念段 → 文件路径 → 关键行号」的三列对照表，作为你后续阅读 u4/u5 单元的索引。

**运行结果**：待本地验证（源码阅读型实践，无需运行命令；如需复核行号，可在仓库内用 `grep -n` 或编辑器「转到行」）。

#### 4.4.5 小练习与答案

**练习 1**：`ChiselStage` 伴生对象的 `phase`（ChiselStage.scala:57）列出了哪些 Phase？请按顺序说出前三个。
> **答案**：依次是 `Elaborate`、`AddDebugIntrinsics`、`Convert`（其后还有 `AddDedupGroupAnnotations`、`AddImplicitOutputFile`、`AddImplicitOutputAnnotationFile`、`Checks`、`CIRCT`）。

**练习 2**：`Converter.convert(circuit: Circuit)` 的输入和输出类型分别是什么？为什么需要这步转换？
> **答案**：输入是 Chisel 私有的 `chisel3.internal.firrtl.Circuit`，输出是 firrtl 库的 `firrtl.ir.Circuit`（Converter.scala:548）。需要它是因为 CIRCT/firtool 消费的是 firrtl 库的标准 IR，而 Chisel 内部用的是另一套私有 IR，必须翻译过去。

**练习 3**：`emitSystemVerilog` 最后是如何从一串注解里取出 Verilog 字符串的？
> **答案**：跑完 `phase.transform` 后，用 `.collectFirst { case EmittedVerilogCircuitAnnotation(a) => a }` 取出携带 Verilog 的注解，再取 `.value`（ChiselStage.scala:206-212）。

---

## 5. 综合实践

把本讲四段串起来，做一个「**跟着一条语句走完四段**」的追踪任务。

**任务**：基于 u1-l4 的 `Adder` 模块（两个 `Input(UInt(8.W))`、一个 `Output(UInt(8.W))`，输出为两输入之和），完成下面这份「四段追踪表」。

| 阶段 | 在你的 `Adder` 例子里发生了什么 | 对应源码位置 |
| --- | --- | --- |
| ① 前端 API | `IO(...)`、`+` 运算被登记为命令 | `pushCommand`/`pushOp`（Builder.scala:895-903） |
| ② Builder | 维护当前模块、收集命令 | `DynamicContext`（Builder.scala:465）、`build`（:1054） |
| ③ 内部 IR | 命令收拢成 `Circuit` | `Circuit`（IR.scala:660） |
| ④ 发射/CIRCT | `Converter` 转 firrtl IR → `firtool` 出 Verilog | `Converter.convert`（Converter.scala:548）、`CIRCT` Phase |

**要求**：

1. 在「发生了什么」一列，用一句话写清这一段对 `Adder` 具体做了什么（例如 ②「把 `+` 运算登记为一条 `DefPrim` 命令，挂在 Adder 模块名下」）。
2. 在「对应源码位置」一列，填上本讲给出的文件:行号。
3. 最后运行 `ChiselStage.emitSystemVerilog(new Adder)` 生成 Verilog，**在生成的 Verilog 注释里找到 `Generated by CIRCT firtool-x.y.z` 字样**，据此回答：你的 Verilog 真正由四段中的哪一段产出？

**预期结果**：你应当能确认——前两段只产生内存中的 Scala 对象（IR），Verilog 文本最终由第 ④ 段的 CIRCT（`firtool`）产出。

**运行结果**：待本地验证（需要按 u1-l2 安装好 firtool，或在已配置的环境里运行）。

---

## 6. 本讲小结

- Chisel 编译器在概念上是**四段式流水线**：前端 API（`chisel3.*`）→ Builder（`chisel3.internal.Builder`）→ 内部 FIRRTL IR（`chisel3.internal.firrtl`）→ 发射（现代由 `Converter` + CIRCT/firtool 承担）。
- **前端 API「只登记不施工」**：每条调用经 `Builder.pushCommand` 追加进当前模块的命令队列，本身不产生硬件或文件。
- **Builder 是全局状态机**：`DynamicContext` 持有 `currentModule`（当前模块）和 `components`（全部模块组件），`build` 方法在收尾时把它们组装成顶层 `Circuit`。
- **内部 IR 是三层树**：`Circuit` ⊃ `Component` ⊃ `Command`，语法酷似 FIRRTL，但它是 Chisel 私有的 Scala 对象。
- **`ChiselStage` 用 `PhaseManager` 管道串联各段**：`Elaborate → Convert → CIRCT`，阶段间靠 `AnnotationSeq` 传递数据；`emitSystemVerilog` 是这条管道的用户入口。
- **README 是概念模型，源码是事实**：README 写的 `chisel3.firrtl.*` / `Emitter` 在源码里实际是 `chisel3.internal.firrtl` / `Converter`+`CIRCT`，且 `Convert` 阶段已自 7.11.0 起 deprecated。

---

## 7. 下一步学习建议

本讲是「导航图」，后续两个单元会分别下钻到细节：

- **想深入 ②③（Builder 与内部 IR）**：进入 **u4 单元**。建议先读 u4-l1（Builder 全局状态机）和 u4-l2（命令记录与内部 FIRRTL IR），它们会把本讲一笔带过的 `pushCommand`、`Circuit`/`Command` 节点、`ElaboratedCircuit` 讲透。
- **想深入 ④（Stage 管道与 CIRCT）**：进入 **u5 单元**。建议先读 u5-l1（Stage/Phase 管道）和 u5-l2（Elaborate 阶段），理解 `PhaseManager` 的依赖排序机制与 `DynamicContext` 各字段的来源。
- **想先看可复用生成器**：可以横向跳到 **u6 单元**（`chisel3.util` 标准库），用 `Decoupled`/`Queue`/`Arbiter` 等现成组件练手，再回头读内部机制。

无论走哪条线，记住本讲的四段式模型——它是你阅读 Chisel 任何源码时「定位自己在哪一段」的罗盘。
