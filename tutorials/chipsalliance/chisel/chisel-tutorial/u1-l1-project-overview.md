# Chisel 项目概览与定位

## 1. 本讲目标

本讲是整本《Chisel 源码学习手册》的第一篇，面向**完全没接触过 Chisel** 的读者。学完本讲后，你应该能够：

- 用一句话说清楚 **Chisel 是什么**，以及它和 Scala、Verilog、FIRRTL、CIRCT 各自的关系。
- 看懂 README 里的 **Blinky（LED 闪烁）** 与 **FIR 滤波器** 两个示例代码，知道每一行大致在做什么。
- 在示例中**精确指出哪一行代码触发了 Verilog 生成**。
- 知道 `chisel3` 顶层包对象、`Module`、`IO`、`ChiselStage` 这几个名字分别出现在源码的哪个文件里，为后续讲义建立一张「源码地图」。

本讲**不要求**你会写 Scala，也不要求你已经懂硬件设计；我们会把必要的概念用通俗语言补齐。

---

## 2. 前置知识

在进入源码之前，先把几个名词解释清楚。这些词后面会反复出现：

| 名词 | 通俗解释 |
| --- | --- |
| **HDL（硬件描述语言）** | 用来描述数字电路的语言，最常见的是 Verilog / SystemVerilog、VHDL。你写的代码最终会被「综合」成真实的逻辑门电路。 |
| **RTL（寄存器传输级）** | Register-Transfer Level。一种描述电路的抽象层级：在时钟边沿之间，数据在寄存器之间经过组合逻辑流动。Chisel 工作在这一层。 |
| **Verilog / SystemVerilog** | 业界标准的硬件描述语言，综合工具、仿真器都认它。可以理解为「硬件世界的汇编语言」——几乎所有高层 HDL 最终都要回到这里。 |
| **Scala** | 一门运行在 JVM 上的编程语言，融合了面向对象和函数式特性。**Chisel 本身不是一门独立语言，而是一个 Scala 库**（见下文 EDSL）。 |
| **EDSL（嵌入式 DSL）** | Embedded Domain-Specific Language。不发明新语法，而是「借用」宿主语言（Scala）的语法来表达新含义。Chisel 就是一个嵌在 Scala 里的硬件 DSL。 |
| **IR（中间表示）** | Intermediate Representation。编译器内部用来表示程序的数据结构。Chisel 会把 Scala 代码先翻译成自己的 IR，再交给后端处理。 |
| **FIRRTL** | Flexible Intermediate Representation for RTL，一种专门为硬件设计的 IR 与编译器框架。 |
| **CIRCT / firtool** | LLVM 下的硬件编译器基础设施；`firtool` 是它的命令行可执行文件，负责把 FIRRTL IR 编译成 SystemVerilog。 |

> 一句话直觉：**Chisel 让你用 Scala「编程生成」Verilog**。你写的不是某一块具体的电路，而是一个能**按参数生成电路的程序**（即「生成器 / generator」）。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下，先混个眼熟，后面会逐个打开看：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/README.md) | 项目自述，包含 Chisel 的定位说明、Blinky 与 FIR 滤波器示例、架构总览、子项目划分。本讲的主要「教材」。 |
| [core/src/main/scala/chisel3/package.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/package.scala) | `chisel3` 顶层**包对象**，集中放置 `.U` / `.S` / `.B` / `.W` 等字面量隐式转换，是「Chisel API 的门面」。 |
| [core/src/main/scala/chisel3/Module.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala) | 定义 `Module`——所有硬件模块的抽象基类（自带隐式 clock/reset）。 |
| [core/src/main/scala/chisel3/IO.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/IO.scala) | 定义 `IO(...)`，用来声明模块对外暴露的端口。 |
| [src/main/scala/circt/stage/ChiselStage.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala) | `ChiselStage`，把一个 Chisel 模块编译成 SystemVerilog 字符串的入口，背后串起整个编译管道。 |
| [src/main/scala/chisel3/verilog.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/verilog.scala) | `chisel3.getVerilogString`，README 里用到的另一个生成 Verilog 的便捷入口。 |

> 注意路径前缀：`core/src/main/scala/...` 是 Chisel 主体源码；`src/main/scala/...`（没有 core）是把各模块整合在一起、并包含 `chisel3.util` 标准库的「main」子项目。这种划分的原因会在 [u1-l3 仓库结构](u1-l3-repository-layout.md) 讲清楚。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **Chisel 是什么：嵌入式 HDL 与生成器方法学**
2. **`chisel3` 顶层包对象与字面量（`.U` / `.B` / `.W`）**
3. **`Module` / `IO` / `ChiselStage` 示例：一行代码触发 Verilog 生成**

---

### 4.1 Chisel 是什么：嵌入式 HDL 与生成器方法学

#### 4.1.1 概念说明

README 开篇第一段就给 Chisel 下了定义：

> **Constructing Hardware in a Scala Embedded Language**（Chisel）是一个开源的硬件描述语言（HDL），用于在**寄存器传输级（RTL）**描述数字电路，面向 ASIC 与 FPGA 设计，强调**高级电路生成与设计复用**。

这里有三个关键词要拆开看：

- **Scala Embedded Language（嵌入 Scala 的语言）**：Chisel **没有独立的编译器前端**。当你「写 Chisel」时，你其实是在写一段合法的 Scala 程序，只是这个程序调用了 Chisel 提供的库函数（如 `Module`、`IO`、`RegInit`）。Scala 编译器照常编译它，程序**运行起来后**才会「长出」电路。这种「运行程序来产生电路」的方式叫 **elaboration（细化）**。
- **RTL**：Chisel 描述的是寄存器、组合逻辑、连线，和 Verilog 同一抽象层级，不是更底层的门级。
- **生成器方法学（generator methodology）**：这是 Chisel 相对传统 Verilog 最大的卖点。传统 Verilog 写的是「一块具体电路」；Chisel 写的是「一个能按参数生产电路的程序」。比如同一个 FIR 滤波器代码，传不同的系数就能生成不同的电路。

README 紧接着说明了 Chisel 的「后端」由谁驱动：

> Chisel 由 **FIRRTL** 驱动，而 FIRRTL 是由 **LLVM CIRCT** 实现的硬件编译器框架。

把这两段合起来，Chisel 的定位就很清楚了：**前端是嵌在 Scala 里的 DSL，后端是 FIRRTL/CIRCT，最终产物是可综合的 SystemVerilog。**

#### 4.1.2 核心流程

从「一段 Scala 代码」到「一段 Verilog」，整个过程可以概括成下面这条流水线：

```text
你写的 Scala/Chisel 代码
        │  （JVM 运行这段程序，调用 Chisel API）
        ▼
   ① 前端 API + Builder   ← chisel3.* 把硬件构造「记录」成命令
        ▼
   ② Chisel 内部 IR        ← 一棵类似 FIRRTL 的数据结构（Circuit）
        ▼
   ③ Convert → FIRRTL IR   ← 转成标准的 firrtl.ir.Circuit
        ▼
   ④ CIRCT (firtool)       ← 调用 LLVM CIRCT 编译
        ▼
   SystemVerilog 字符串 / 文件
```

这条流水线和 README「Architecture Overview」里列的四大部件一一对应：

1. **前端（frontend）**：`chisel3.*`，公开 API。
2. **Builder**：`chisel3.internal.Builder`，维护全局状态（比如「当前正在构造哪个模块」）并收集命令。
3. **中间数据结构（intermediate data structures）**：一棵语法上很像 FIRRTL 的 IR 树，顶层对象是 `Circuit`。
4. **FIRRTL emitter**：把中间结构变成可写出的 FIRRTL 文本，交给后续处理。

> 说明：README 出于简明，把中间结构记作 `chisel3.firrtl.*`；在实际源码里它位于 `core/src/main/scala/chisel3/internal/firrtl/`（多了一层 `internal`）。后续单元会深入这里。

#### 4.1.3 源码精读

Chisel 的定义出现在 README 的最开头：

- [README.md:8](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/README.md#L8) ——Chisel 的全称定义：嵌入式 Scala 硬件构造语言，RTL 级、面向 ASIC/FPGA、强调生成与复用。
- [README.md:10](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/README.md#L10) ——点明 Chisel「给 Scala 加上硬件构造原语，用来写可参数化的电路生成器，产出可综合 Verilog」。这是理解整门语言价值的关键一句。
- [README.md:15-16](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/README.md#L15-L16) ——说明 Chisel 的后端是 FIRRTL，而 FIRRTL 由 LLVM CIRCT 实现。

README 的「Architecture Overview」小节用四个 bullet 描述了上面那条流水线的概念模型：

- [README.md:347](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/README.md#L347) ——前端 `chisel3.*`：公开 API。
- [README.md:348](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/README.md#L348) ——Builder `chisel3.internal.Builder`：维护全局状态、收集命令。
- [README.md:349](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/README.md#L349) ——中间数据结构：语法上很像 FIRRTL，顶层是 `Circuit`。
- [README.md:350](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/README.md#L350) ——FIRRTL emitter：把中间结构变成 FIRRTL 文本输出。

> 这一节先建立「概念地图」即可。前端、Builder、IR、emitter 的源码细节分别在 [u4-l1 Builder 全局状态](u4-l1-builder-global-state.md)、[u4-l2 内部 FIRRTL IR](u4-l2-internal-firrtl-ir.md)、[u5 编译管道](u5-l1-stage-phase-pipeline.md) 展开。

#### 4.1.4 代码实践

**实践目标**：用自己的话把 Chisel 的定位压缩成一句话，并理清它与 Scala / Verilog / FIRRTL / CIRCT 的关系。

**操作步骤**：

1. 打开 [README.md:8-18](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/README.md#L8-L18)，通读前两段。
2. 在笔记里画一个四格关系图：`Scala`、`Chisel`、`FIRRTL`、`CIRCT(firtool)`，用箭头标出「谁嵌在谁里」「谁驱动谁」「谁产出 Verilog」。

**需要观察的现象**：你会发现 Chisel 既不是 Scala（它是 Scala 库），也不是 Verilog（它产出 Verilog），更不是 FIRRTL（它把 FIRRTL 当中间格式）。它处在「用 Scala 编程 → 经 FIRRTL → 由 CIRCT 出 Verilog」的中间环节。

**预期结果**：能写出类似「Chisel 是一个嵌在 Scala 里的硬件生成器 DSL，运行时细化出电路 IR，再经 FIRRTL/CIRCT 编译成 SystemVerilog」这样的一句话。

**待本地验证**：无需运行命令，纯阅读型实践。

#### 4.1.5 小练习与答案

**练习 1**：为什么说 Chisel 是「嵌入式」DSL，而不是「独立」语言？
> **答案**：因为 Chisel 没有独立的词法/语法/编译器前端，它完全借用 Scala 的语法与编译器。一段 Chisel 代码首先是一段合法的 Scala 程序，运行时才通过调用 Chisel 库「长出」电路。

**练习 2**：把「前端 / Builder / IR / emitter」四个部件，按执行先后排序。
> **答案**：前端 API（用户调用）→ Builder（收集命令、维护状态）→ IR（构造中间数据结构）→ emitter（输出 FIRRTL 文本）。

---

### 4.2 `chisel3` 顶层包对象与字面量

#### 4.2.1 概念说明

当你看到 Chisel 代码里写 `0.U`、`1.B`、`8.W` 这样的字面量时，可能会疑惑：Scala 标准库里的 `Int` 可没有 `.U` 方法，这是从哪来的？

答案在 `chisel3` 的**包对象（package object）**里。Scala 的 package object 用来给整个包「挂载」公共的类型、值和隐式转换。Chisel 把字面量转换、异常类型、打印工具等都集中放在了 `package object chisel3` 中。

「隐式转换（implicit conversion）」是 Scala 的一个特性：当编译器发现 `0.U` 中的 `0`（一个 `Int`）没有 `.U` 方法时，它会去隐式作用域里找一个能把 `Int` 变成「有 `.U` 方法的类型」的转换。Chisel 提供了 `fromIntToLiteral`，它把 `Int` 包装成一个带 `.U` / `.S` / `.B` 方法的隐式类，于是 `0.U` 就等价于 `UInt.Lit(0, Width())`。

#### 4.2.2 核心流程

以 `5.U` 为例，它的「展开」过程是：

```text
5.U                                   // 你写的代码
   │  Scala 编译器查到 implicit class fromIntToLiteral
   ▼
new fromIntToLiteral(5).U             // 隐式转换
   │  调用 fromBigIntToLiteral.U
   ▼
UInt.Lit(bigint = 5, width = Width()) // 产出一个宽度待定的 UInt 字面量
```

`fromIntToLiteral` 继承自 `fromBigIntToLiteral`，所以 `Int` / `Long` / `BigInt` 都能享受到同一套 `.U` / `.S` / `.B` 方法。宽度则由 `.W`（即 `fromIntToWidth`）单独提供，比如 `8.W` 会产出 `Width(8)`。

#### 4.2.3 源码精读

整个包对象的开头在这里：

- [package.scala:12-14](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/package.scala#L12-L14) ——`package object chisel3` 的声明，注释写明「这个包包含主要的 chisel3 API」。

字面量隐式转换的核心是 `fromBigIntToLiteral`，它定义了 `.U` / `.S` / `.B`：

- [package.scala:34-75](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/package.scala#L34-L75) ——`implicit class fromBigIntToLiteral`，把 `BigInt` 变成带字面量方法的对象。其中：
  - [package.scala:47](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/package.scala#L47) ——`def U: UInt = UInt.Lit(bigint, Width())`：这就是 `0.U` 真正调用的方法，产出一个**宽度待定**的 `UInt` 字面量。
  - [package.scala:38-44](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/package.scala#L38-L44) ——`def B: Bool`：把 `0` / `1` 转成 `Bool` 字面量（`0.B`、`1.B`），其他值会报错。

为了让 `Int` 和 `Long` 也能用，包对象又把它们桥接到上面的类：

- [package.scala:77-78](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/package.scala#L77-L78) ——`fromIntToLiteral` 与 `fromLongToLiteral` 都继承自 `fromBigIntToLiteral`，所以 `5.U`（Int）、`true.B`（Boolean）都能工作。

宽度的写法 `.W` 来自另一个隐式类：

- [package.scala:125-127](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/package.scala#L125-L127) ——`fromIntToWidth`：把 `8` 变成 `Width(8)`，于是 `UInt(8.W)` 表示「8 位宽的 UInt 类型」。

#### 4.2.4 代码实践

**实践目标**：亲手在源码里验证「`5.U` 是怎么变成 `UInt` 的」。

**操作步骤**：

1. 打开 [package.scala:47](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/package.scala#L47)，确认 `.U` 调用的是 `UInt.Lit(bigint, Width())`。
2. 回到 README 的 Blinky 示例，找到所有用到 `.U` / `.B` / `.W` 的地方（如 `startOn.B`、`true.B`），把它们逐个对应到上面的隐式方法。

**需要观察的现象**：你会发现字面量的「值」和「宽度」是分开的——`.U` 给值，`.W` 给宽度，`UInt(8.W)` 是一个「类型」（还没绑定到具体硬件），而 `5.U` 是一个「字面量值」。**类型 vs 硬件值**的区分是 Chisel 的核心概念之一，会在 [u4-l3 Binding 系统](u4-l3-binding-system.md) 深入。

**预期结果**：能解释 `UInt(8.W)` 和 `5.U` 的区别——前者是 8 位 UInt 的「类型模板」，后者是一个具体的无符号字面量。

**待本地验证**：纯阅读型实践，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：`UInt.Lit(bigint, Width())` 中的 `Width()` 没有传参，意味着什么？
> **答案**：`Width()` 表示「宽度待定」。Chisel 会根据字面量值或上下文在后续阶段推断出实际位宽。如果想固定宽度，可以写 `5.U(8.W)`。

**练习 2**：为什么 `fromIntToLiteral` 要继承 `fromBigIntToLiteral`，而不是各自独立实现？
> **答案**：为了避免重复。`Int`、`Long`、`BigInt` 都是整数，字面量语义完全一致，所以让 `Int`/`Long` 复用 `BigInt` 那一套 `.U/.S/.B` 实现即可，减少维护成本。

---

### 4.3 `Module` / `IO` / `ChiselStage` 示例：一行代码触发 Verilog 生成

#### 4.3.1 概念说明

本节看 README 里最完整的示例——**Blinky（LED 闪烁）**。它包含了理解 Chisel 所需的最小要素：

- **`Module`**：所有硬件模块的基类。继承它就声明了「这是一个硬件模块」，并自动带上隐式的 `clock` 和 `reset` 端口。
- **`IO`**：声明模块对外的端口（input/output）。Chisel 里端口必须用 `IO(...)` 包起来。
- **`Bundle`**：把多个信号打包成一个命名的聚合体，常用于定义端口的结构（这里只有一个 `led0`）。
- **`RegInit` / `when`**：寄存器与条件赋值，构成时序逻辑。
- **`ChiselStage.emitSystemVerilog`**：**这一行**才是真正「按下按钮」、把整个模块编译成 Verilog 的地方。

> 关键直觉：**类的构造体里写的所有代码（`IO`、`RegInit`、`when`）都不会立刻产生电路**，它们只是在「记录」要构造什么；真正把这些记录「兑现」成 Verilog 的，是 `ChiselStage.emitSystemVerilog` 这一次调用。

#### 4.3.2 核心流程

以 Blinky 为例，整个执行顺序是：

```text
object Main 的 main 方法被 JVM 调用
        │
        ▼
ChiselStage.emitSystemVerilog(new Blinky(1000), firtoolOpts = ...)
        │  ① 先 new Blinky(1000)：触发 Blinky 构造体执行
        │       → IO(...) 声明端口、RegInit 建寄存器、when 记录条件
        │       → Builder 把这些都收集成内部 IR（一棵 Circuit 树）
        ▼
   phase.transform(annos)  ← 串起 Elaborate → Convert → Checks → CIRCT
        ▼
   CIRCT(firtool) 把 FIRRTL IR 编译成 SystemVerilog
        ▼
   println(...) 把 Verilog 字符串打印出来
```

`ChiselStage` 内部用一个 `PhaseManager` 把多个阶段（Phase）按依赖关系排好序。这些阶段的名字直接对应 4.1.2 节那条流水线。

#### 4.3.3 源码精读

先看 README 的 Blinky 示例本体：

- [README.md:54-81](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/README.md#L54-L81) ——Blinky 完整示例。其中：
  - [README.md:59](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/README.md#L59) ——`class Blinky(...) extends Module`：声明一个硬件模块。
  - [README.md:60-62](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/README.md#L60-L62) ——`val io = IO(new Bundle { val led0 = Output(Bool()) })`：声明一个名为 `led0` 的输出端口。
  - [README.md:64-69](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/README.md#L64-L69) ——时序逻辑：一个寄存器 `led`，每计数到 `freq/2` 就翻转一次（于是 LED 周期性地闪烁）。
  - [README.md:75-78](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/README.md#L75-L78) ——**这就是触发 Verilog 生成的一行**：`ChiselStage.emitSystemVerilog(new Blinky(1000), firtoolOpts = Array(...))`。

这几个 API 在源码中的定义位置：

- [Module.scala:34](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L34) ——`object Module`（工厂入口，`Module(new ...)` 走它）。
- [Module.scala:232](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L232) ——`abstract class Module extends RawModule with ImplicitClock with ImplicitReset`：模块抽象基类，`with ImplicitClock with ImplicitReset` 解释了为什么 Blinky 没有显式声明 `clock`/`reset`，生成的 Verilog 里却出现了它们。
- [IO.scala:10](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/IO.scala#L10) 与 [IO.scala:25](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/IO.scala#L25) ——`object IO` 及其 `apply` 方法：把传入的 `Data` 注册为当前模块的端口。

最后看「按下按钮」的入口 `emitSystemVerilog` 以及它串起的管道：

- [ChiselStage.scala:197-213](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala#L197-L213) ——`def emitSystemVerilog(gen, args, firtoolOpts)`：组装好注解（`ChiselGeneratorAnnotation` + `CIRCTTargetAnnotation(SystemVerilog)` + `firtoolOpts`），交给 `phase.transform` 处理，最后从结果里取出 Verilog 字符串返回。
- [ChiselStage.scala:57-68](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala#L57-L68) ——`private def phase`：用 `PhaseManager` 声明的目标阶段序列：`Elaborate → AddDebugIntrinsics → Convert → AddDedupGroupAnnotations → AddImplicitOutputFile → AddImplicitOutputAnnotationFile → Checks → CIRCT`。这条序列就是 4.1.2 流水线在源码中的真实长相。

README 还给了一个更简短的写法：

- [README.md:200](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/README.md#L200) ——`chisel3.getVerilogString(new FirFilter(...))`：另一个生成 Verilog 字符串的便捷入口。
- [verilog.scala:30](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/verilog.scala#L30) ——`def apply(gen) = ChiselStage.emitSystemVerilog(gen)`：可见 `getVerilogString` 内部就是直接调用 `emitSystemVerilog`，两者殊途同归。

> 旁注：README 里 Blinky 生成的 Verilog 注释写着 `Generated by CIRCT firtool-1.37.0`，那是文档里**缓存的老示例**。你本地实际跑出来的版本号取决于你安装的 `firtool`（当前仓库 CI 已用到 firtool-1.151.x），不必和文档里的数字对上。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：阅读 Blinky 示例，用一句话写出从 Scala 代码到 Verilog 的转化路径，并指出触发它的那一行。

**操作步骤**：

1. 打开 [README.md:54-81](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/README.md#L54-L81)，逐行阅读 Blinky 的 `class` 与 `object Main`。
2. 在源码中定位「触发点」：[README.md:75](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/README.md#L75) 的 `ChiselStage.emitSystemVerilog(...)`。
3. 打开 [ChiselStage.scala:57-68](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala#L57-L68)，把 `phase` 里的阶段顺序抄下来。
4. 用一句话把链条串起来。

**需要观察的现象**：把 `ChiselStage.emitSystemVerilog(new Blinky(1000), ...)` 这一行**注释掉**（思想实验），程序仍然能编译、能运行，但**不会产生任何 Verilog**。这印证了「构造体只是在记录，真正兑现的是这一行调用」。

**预期结果**（参考答案）：

> 「`new Blinky(1000)` 运行 Blinky 构造体，经 Chisel 前端 API 与 Builder 收集成内部 IR（≈FIRRTL），`phase` 管道依次执行 Elaborate → Convert → Checks → CIRCT，最终由 CIRCT(firtool) 编译出 SystemVerilog 字符串；触发这一切的是 [README.md:75](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/README.md#L75) 的 `ChiselStage.emitSystemVerilog(...)`。」

**待本地验证**：本实践是阅读型任务，结论可由源码直接得出；若想在本地真正看到 Verilog 输出，需先按 [u1-l2 构建与运行](u1-l2-build-and-run.md) 配好 `firtool` 与 mill 环境。

#### 4.3.5 小练习与答案

**练习 1**：Blinky 的 `class` 体里并没有写 `clock` 和 `reset`，为什么生成的 Verilog 里有 `input clock, reset`？
> **答案**：因为 `Blinky` 继承的是 `Module`（见 [Module.scala:232](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L232)），它混入了 `ImplicitClock` 和 `ImplicitReset`，会自动声明这两个隐式端口。`RegInit` 也依赖隐式 clock/reset 来产生时序逻辑。

**练习 2**：`ChiselStage.emitSystemVerilog` 和 `chisel3.getVerilogString` 是什么关系？
> **答案**：`getVerilogString` 是更简洁的封装，其 `apply` 内部直接调用 `ChiselStage.emitSystemVerilog`（见 [verilog.scala:30](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/verilog.scala#L30)）。两者最终走同一条编译管道。

**练习 3**：`firtoolOpts = Array("-disable-all-randomization", ...)` 这个参数会影响什么？
> **答案**：它会被转换成 `FirtoolOption`，最终传给 `firtool`（见 [ChiselStage.scala:205](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala#L205)）。`-disable-all-randomization` 让生成的 Verilog 不在复位时给寄存器赋随机初值，输出更干净、更适合阅读和形式验证。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个小任务：

**任务**：以 README 的 **FIR 滤波器** 示例为对象，完整复述「Scala 代码 → Chisel IR → FIRRTL → Verilog」的转化路径，并和 Blinky 对比。

**操作步骤**：

1. 阅读 [README.md:129-202](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/README.md#L129-L202)，理解 `FirFilter` 如何用一个 `Seq[UInt]` 系数列表来**参数化**地生成不同滤波器（这正是 4.1 节强调的「生成器方法学」）。
2. 在该范围内找出至少两处「触发 Verilog 生成」的写法：
   - 命令行风格：[README.md:190-195](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/README.md#L190-L195) 的 `(new ChiselStage).execute(...)`；
   - 字符串风格：[README.md:200](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/README.md#L200) 的 `chisel3.getVerilogString(...)`。
3. 写一段话，说明这两个入口最终都会进入 [ChiselStage.scala:57-68](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala#L57-L68) 描述的同一条 `phase` 管道。
4. （可选，待本地验证）按 [README.md:295-301](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/README.md#L295-L301) 的步骤 `git clone` 并运行 `./mill chisel[].compile`，确认仓库可编译；再尝试在示例工程里调用 `emitSystemVerilog` 观察真实输出。

**预期产出**：一张标注了「Scala 代码 / Builder / 内部 IR / Convert / CIRCT / Verilog」六站的小流程图，并能在 FIR 滤波器代码中指明触发编译的具体行。

---

## 6. 本讲小结

- **Chisel 是嵌在 Scala 里的硬件构造 DSL**：你写的是 Scala 程序，运行时通过调用 Chisel API「长出」电路（elaboration），而不是直接描述静态电路。
- **后端是 FIRRTL/CIRCT**：Chisel 把内部 IR 转成 FIRRTL，再由 LLVM CIRCT（`firtool`）编译成可综合 SystemVerilog。
- **概念上的四大部件**：前端 API（`chisel3.*`）→ Builder（`chisel3.internal.Builder`）→ 中间 IR（顶层是 `Circuit`）→ emitter；这与源码里 `phase` 的阶段序列（`Elaborate → Convert → Checks → CIRCT`）一一对应。
- **字面量 `.U` / `.S` / `.B` / `.W`** 由 `chisel3` 包对象里的隐式类（`fromBigIntToLiteral`、`fromIntToWidth` 等）提供，本质是 Scala 隐式转换。
- **`Module` 自带隐式 clock/reset**（`ImplicitClock` / `ImplicitReset`），这解释了 Blinky 没写却出现的 `clock`/`reset` 端口。
- **真正触发 Verilog 生成的是 `ChiselStage.emitSystemVerilog(...)`** 这一行（README Blinky 第 75 行）；构造体里的代码只是在「记录」。

---

## 7. 下一步学习建议

本讲只建立了「全景地图」，还没有真正动手编译。建议按以下顺序继续：

1. **[u1-l2 构建、测试与本地发布](u1-l2-build-and-run.md)**：学会用 `./mill` 编译 Chisel、跑测试、`publishLocal` 把本地版本交给别的项目用——这是后续所有动手实践的前提。
2. **[u1-l3 目录结构与子项目划分](u1-l3-repository-layout.md)**：搞清楚 `core` / `macros` / `plugin` / `firrtl` / `src/main` / `svsim` 各自的职责与依赖，理解为什么 `package.scala` 在 `core/` 而 `ChiselStage` 在 `src/main/`。
3. **[u1-l4 Hello Chisel：第一个模块与 Verilog 生成](u1-l4-first-module-and-verilog.md)**：亲手写一个最小 `Module` 并用 `emitSystemVerilog` 看到 Verilog 输出。
4. 想提前了解编译细节，可跳读 [README.md:343-368](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/README.md#L343-L368) 的 Architecture Overview 与 Sub-Projects 两节。
