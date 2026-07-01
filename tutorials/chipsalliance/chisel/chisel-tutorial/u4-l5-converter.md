# Converter：IR 到 FIRRTL 的转换

## 1. 本讲目标

本讲承接 u4-l2（命令记录与内部 FIRRTL IR）和 u4-l4（IR 序列化 Serializer），回答一个关键问题：**Chisel 自己的内部 IR，是如何变成「FIRRTL 编译器认识的那套 IR」的？**

读完本讲你应该能够：

- 分清两个名字都叫 Circuit、但属于不同体系的 IR：`chisel3.internal.firrtl.ir.Circuit` 与 `firrtl.ir.Circuit`。
- 说清 `ElaboratedCircuit` 这个对外封装承载了什么、它与底层 `_circuit` 的关系。
- 在源码里定位 `Converter` 的顶层入口，追踪一个模块（Component）、一个端口（Port）、一条命令（Command）是如何被逐层翻译的。
- 亲手追踪「一个 `Reg` 是如何变成 FIRRTL 中 `reg` 节点的」这条调用链。
- 知道这条 Converter 路径在当前版本（自 Chisel 7.11.0 起）已被标记为 deprecated，以及为什么它仍然值得读懂。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 「两个 Circuit」的迷思

初学者最容易踩的坑是：**Chisel 源码里到处都是 Circuit，它们不一定是同一个东西**。本讲涉及两个：

| 名字 | 所属 | 作用 | 产出者 |
| --- | --- | --- | --- |
| `chisel3.internal.firrtl.ir.Circuit` | Chisel 私有包 | Chisel elaboration 长出来的内部 IR，是一棵 Scala case class 树 | `Builder.build` 收口组装 |
| `firrtl.ir.Circuit` | FIRRTL 编译器项目（独立库） | FIRRTL 工具链认的「标准 IR」，下游 pass/变换都吃它 | 需要由 **Converter** 翻译得到 |

前者是「Chisel 内部账本」，后者是「FIRRTL 世界语」。Converter 就是这两者之间的**翻译官**。这层区分在 u1-l5 已埋下伏笔，本讲把它彻底讲透。

### 2.2 为什么需要翻译，而不是直接用 firrtl.ir？

历史原因：Chisel 早期并不直接产出 `firrtl.ir`，而是维护了一套语法酷似 FIRRTL、但由自己控制的内部 IR（即 `chisel3.internal.firrtl`）。这样做的好处是 Chisel 可以自由演进内部表示（例如加入 Probe、Layer、Property、Domain 等 FIRRTL 原生不存在的概念），而不被 FIRRTL 编译器的发布节奏绑死。

但 FIRRTL 编译器（以及一些下游工具、注解系统）只认 `firrtl.ir.Circuit`。所以需要一座桥，把 Chisel 内部 IR 翻译过去——这座桥就是 Converter。

### 2.3 翻译 vs 序列化：两条不同的路

上一讲 u4-l4 讲的 `Serializer`，是把内部 IR **变成人类可读的 CHIRRTL 文本字符串**（`emitCHIRRTL` 走这条路）。本讲的 `Converter`，是把内部 IR **变成另一个 Scala AST（`firrtl.ir.Circuit`）**。两者读的是同一份内部 IR，但产出物完全不同：

```
                         ┌──► Serializer ──► CHIRRTL 文本（字符串）  ← u4-l4
chisel3.internal.firrtl  │
       .ir.Circuit  ─────┼──► Converter  ──► firrtl.ir.Circuit（AST） ← 本讲
                         │
                         └──► Panama CIRCT ──► SystemVerilog          ← u5 系列
```

记住这张图，它是本讲（以及整个 u4/u5）的导航图。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 角色 |
| --- | --- |
| `core/src/main/scala/chisel3/internal/firrtl/Converter.scala` | **主角**。翻译官，把内部 IR 翻译成 `firrtl.ir.*`。 |
| `core/src/main/scala/chisel3/ElaboratedCircuit.scala` | elaboration 结果的对外封装 trait，内部持有 `_circuit`。 |
| `core/src/main/scala/chisel3/internal/firrtl/IR.scala` | Chisel 内部 IR 定义（Circuit/Component/Command 等），u4-l2 已讲，本讲复习其中几个节点。 |
| `src/main/scala/chisel3/stage/phases/Convert.scala` | 唯一调用 Converter 全量翻译的编译阶段（已 deprecated）。 |
| `src/main/scala/circt/stage/ChiselStage.scala` | `emitCHIRRTL` 等用户入口，用来在实践中观察输出。 |

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：① 两个 IR 与翻译总览；② `ElaboratedCircuit` 封装；③ Converter 的顶层与模块/端口映射；④ Command→Statement 的逐条翻译（含 Reg 追踪）；⑤ Arg→Expression 与 Data→Type 的表达式/类型翻译。

### 4.1 两个 IR 与翻译总览

#### 4.1.1 概念说明

翻译的本质是**同构映射**：内部 IR 的每个节点，几乎都能在 `firrtl.ir` 里找到一个对应的 case class，Converter 的工作就是「按形状逐个搬过去」。两棵树的节点对应关系大致如下：

| Chisel 内部 IR（`chisel3.internal.firrtl.ir`） | FIRRTL IR（`firrtl.ir`） |
| --- | --- |
| `Circuit` | `fir.Circuit` |
| `DefModule` / `DefBlackBox` / `DefClass` | `fir.Module` / `fir.ExtModule` / `fir.DefClass` |
| `Port` | `fir.Port` |
| `Block`（命令容器） | `fir.Block` |
| `Command`（DefReg/Connect/When…） | `fir.Statement` |
| `Arg`（Ref/Slot/Index/ULit…） | `fir.Expression` |
| `Data`（UInt/SInt/Vec/Record…） | `fir.Type` |

注意第三列：FIRRTL IR 的粒度更「标准」，比如内部 `DefPrim`（原语运算）在 FIRRTL 里被表达成一条 `fir.DefNode` 包着 `fir.DoPrim` 表达式——这就是 u4-l2 提到的「Chisel 用 DefPrim 表达原语运算，经翻译为 fir.DefNode」的具体落点。

#### 4.1.2 核心流程

Converter 是一个 `private[chisel3] object`，对外暴露一组重载的 `convert`/`convertCommand`/`extractType` 方法，自顶向下递归翻译：

```
convert(Circuit)              → fir.Circuit          （顶层入口）
  └─ convert(Component)       → fir.DefModule        （逐模块）
       ├─ convert(Port)       → fir.Port             （逐端口）
       └─ convert(Block)      → fir.Statement        （模块体）
            └─ convertCommand(Command) → fir.Statement（逐条命令）
                 └─ convert(Arg)      → fir.Expression（命令里的表达式）
                 └─ extractType(Data) → fir.Type      （类型）
```

关键点：**`ctx: Component`（当前模块）会被一路透传**。这是因为同一个端口引用，在「定义它的模块内部」翻译成 `fir.Reference`，而在「父模块里引用它」时则要翻译成 `fir.SubField(父实例名, 端口名)`——这是后面 4.5 会看到的细节。

#### 4.1.3 源码精读

Converter 的定义是一个包私有的单例对象：[Converter.scala:23](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala#L23)（`private[chisel3] object Converter`）。它 import 了两个 IR：`firrtl.{ir => fir}`（FIRRTL 编译器的 IR，别名 `fir`）和 `chisel3.internal.firrtl.ir._`（Chisel 内部 IR）。两个 `ir` 同名，靠别名区分——这也是本讲反复强调「两个 Circuit」的代码证据。

#### 4.1.4 代码实践

打开 [Converter.scala:23](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala#L23)，数一数它定义了多少个名为 `convert` 的重载方法（提示：参数类型分别是 `Circuit`、`Component`、`Port`、`Command`/`Seq[Command]`/`Block`、`Arg`、`Width`、`PrimOp` 等）。这一步只为了建立「按类型分发」的直觉，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Converter 要 import 两个不同的 `ir`？  
**参考答案**：因为它同时操作两套 IR——输入是 Chisel 内部 IR（`chisel3.internal.firrtl.ir`），输出是 FIRRTL 编译器 IR（`firrtl.ir`，别名为 `fir`）。两个包里都有 `Circuit`/`Module`/`Port`，必须用别名区分，否则名字冲突。

**练习 2**：Converter 是 `class` 还是 `object`？为什么这样设计？  
**参考答案**：它是 `private[chisel3] object`（单例）。因为所有翻译方法都是**无状态**的纯函数（输入 IR 节点 + 上下文，输出对应 FIRRTL 节点），不需要实例化，单例对象最合适，也便于全工程直接 `Converter.convert(...)` 调用。

---

### 4.2 ElaboratedCircuit：elaboration 的对外封装

#### 4.2.1 概念说明

`ElaboratedCircuit` 是 elaboration（细化）跑完之后，**交给外部世界（编译阶段、用户）的「门面」**。它把内部的 `Circuit`（Chisel 私有 IR）包起来，只暴露几个经过设计的只读 API：拿电路名、序列化成 FIRRTL 文本、拿到注解、拿到顶层 Definition。

为什么要这层封装？因为 `chisel3.internal.firrtl.ir.Circuit` 是 `private[chisel3]` 的实现细节，不应让用户或下游阶段直接摸到。`ElaboratedCircuit` 用一个 `sealed trait` 把它藏起来，只开必要的口子。

#### 4.2.2 核心流程

`ElaboratedCircuit` 的实例由 elaboration 阶段创建（u5-l2 会讲 `Elaborate`），随后被装进 `ChiselCircuitAnnotation` 在编译管道里流动。它的核心能力是「序列化」：

```
ElaboratedCircuit.serialize
   └─ lazilySerialize         （拼成字符串）
        └─ Serializer.lazily(circuit, annotations)   ← 注意：是 Serializer，不是 Converter
```

也就是说，`ElaboratedCircuit` 默认的文本输出走的是 **Serializer**（u4-l4），而不是本讲的 Converter。这一点非常关键，它直接解释了 Converter 在现代代码里的处境。

#### 4.2.3 源码精读

对外门面是一个 sealed trait，声明了 `name`、`serialize`、`lazilySerialize`、`annotations`、`topDefinition` 等只读成员，并把真正的内部 IR 藏在 `private[chisel3] def _circuit` 后面：[ElaboratedCircuit.scala:18](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/ElaboratedCircuit.scala#L18)。

具体实现类 `ElaboratedCircuitImpl` 里，文本序列化最终委托给 `Serializer.lazily`：

```scala
override def lazilySerialize(annotations: Iterable[Annotation]): Iterable[String] = {
  Serializer.lazily(circuit, annotations.toSeq)
}
```

见 [ElaboratedCircuit.scala:86](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/ElaboratedCircuit.scala#L86)。注意它调用的是 `Serializer`，**不是 `Converter`**——这是「现代路径不经过 Converter」的最直接证据。`_circuit` 字段则原样持有内部 IR：[ElaboratedCircuit.scala:63](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/ElaboratedCircuit.scala#L63)。

#### 4.2.4 代码实践

`emitCHIRRTL` 是观察 `ElaboratedCircuit` 文本输出的现成入口。看它的实现 [ChiselStage.scala:92](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala#L92)：它跑完 phase 后，拿到 `a.elaboratedCircuit`，最后一句是 `elaboratedCircuit.get.serialize(inFileAnnos)`（[ChiselStage.scala:112](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala#L112)）。你可以确认：这条路径全程没有出现 `Converter`。

> 待本地验证：若你已按 u1-l2 配好 mill 与 firtool，可在 `mill` REPL 或测试里执行 `circt.stage.ChiselStage.emitCHIRRTL(new RawModule {})`，应得到一段以 `circuit` 开头的 CHIRRTL 文本。

#### 4.2.5 小练习与答案

**练习 1**：`ElaboratedCircuit` 为什么要把 `_circuit` 标记为 `private[chisel3]`，而不是公开？  
**参考答案**：`_circuit` 的类型 `chisel3.internal.firrtl.ir.Circuit` 属于 `internal` 包，是实现细节，未来可能随 Chisel 演进而改结构。`ElaboratedCircuit` 作为对外稳定接口，只暴露 `serialize`/`name`/`annotations` 等不依赖内部表示的方法，避免下游耦合到内部 IR。

**练习 2**：`serialize` 内部最终调的是 `Serializer` 还是 `Converter`？这意味着什么？  
**参考答案**：是 `Serializer`。意味着 `ElaboratedCircuit` 的默认文本输出（也就是 `emitCHIRRTL`）根本不经过 Converter；Converter 产出的 `firrtl.ir.Circuit` 是另一条（已 deprecated 的）路径。

---

### 4.3 Converter 顶层与模块/端口映射

#### 4.3.1 概念说明

本模块进入 Converter 本体，看它如何处理整棵电路树的三层：Circuit → Component（模块）→ Port（端口）。

- **Circuit 层**：遍历所有 `components`，逐个翻译；附带翻译 `layers`、`options`、`typeAliases`。
- **Component 层**：按模块种类分发——普通模块 `DefModule` 翻成 `fir.Module`、黑盒 `DefBlackBox` 翻成 `fir.ExtModule`、类 `DefClass` 翻成 `fir.DefClass` 等。
- **Port 层**：把端口方向（Chisel 的 `SpecifiedDirection`）折算成 FIRRTL 的 `fir.Input`/`fir.Output`，再把端口类型 `Data` 翻成 `fir.Type`。

#### 4.3.2 核心流程

```
convert(Circuit)
  ├─ typeAliases = circuit.typeAliases.map(_.name)   ← 收集类型别名，全程透传
  ├─ components.map(c => convert(c, typeAliases))    ← 逐模块翻译
  ├─ circuit.name
  ├─ typeAliases.map(...DefTypeAlias...)             ← 类型别名声明
  ├─ layers.map(convertLayer)                        ← layer 树
  └─ options.map(convertOption)                      ← option 树
```

`typeAliases` 这个参数会一路透传到模块、端口、命令翻译里，用于在合适的地方输出 FIRRTL 的 `AliasType`——这是 Chisel 较新加入的类型别名机制，本讲只做了解。

#### 4.3.3 源码精读

**顶层入口** `convert(circuit: Circuit): fir.Circuit`：[Converter.scala:548](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala#L548)。它先算出 `typeAliases`，再用 `circuit.components.map(c => convert(c, typeAliases))` 把每个模块翻译过去，最后组装成一个 `fir.Circuit`。旁边还有个 `convertLazily`（[Converter.scala:561](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala#L561)），用 `LazyList` 让模块按需翻译，注释里坦承「不确定要不要把它变成默认」。

**模块翻译入口** `convert(component: Component, typeAliases): fir.DefModule`：[Converter.scala:484](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala#L484)。这正是练习任务要找的「把 Chisel Component 转换为 firrtl DefModule 的入口方法」。它用一个 `match` 按模块种类分发，其中最常见的普通模块分支：

```scala
case ctx @ DefModule(id, name, public, layers, ports, block) =>
  fir.Module(
    convert(id._getSourceLocator),                                  // 源定位 info
    name,                                                            // 模块名
    public,                                                          // 是否 public
    layers.map(_.fullName),                                          // 模块所属 layer
    (ports ++ ctx.secretPorts).map(p => convert(p, typeAliases)),    // 端口（含隐藏端口）
    convert(block, ctx, typeAliases)                                 // 模块体
  )
```

见 [Converter.scala:485](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala#L485)。两个细节值得注意：① 端口列表是 `ports ++ ctx.secretPorts`，Chisel 的「隐藏端口」（如自动加的 clock/reset、调试端口）在这里被合并输出；② 模块体由 `convert(block, ctx, typeAliases)` 翻译，`ctx` 就是当前模块，会透传下去解决端口引用的歧义。

黑盒分支 `DefBlackBox → fir.ExtModule`：[Converter.scala:494](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala#L494)，它额外翻译参数 `params`（按名字排序后逐个翻译）。

**端口翻译** `convert(port, typeAliases, topDir): fir.Port`：[Converter.scala:460](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala#L460)。它做三件事：用 `firrtlUserDirOf` 算出端口在「FIRRTL 用户视角」下的方向，再把方向折算成 FIRRTL 的 `fir.Input`/`fir.Output`（默认/Output→`fir.Output`，Flip/Input→`fir.Input`），最后用 `extractType` 翻译端口类型。

#### 4.3.4 代码实践

在 [Converter.scala:484](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala#L484) 的 `match` 里，列出 Chisel 内部 IR 的 `Component` 一共有哪几种（提示：看 [IR.scala:596](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L596) 附近的 case class 定义，以及抽象基类 [IR.scala:587](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L587)）。把它们一一对应到 Converter 里翻成的 `fir.Module` / `fir.ExtModule` / `fir.IntModule` / `fir.DefClass` / `fir.TestMarker`，画一张映射表。

#### 4.3.5 小练习与答案

**练习 1**：为什么端口翻译里要区分 `clearDir`（清除方向）？  
**参考答案**：当一个端口本身已被标记为强 `Input`/`Output` 时，它的子字段不应再各自带方向（FIRRTL 里方向只体现在最外层），所以 `clearDir=true` 让 `extractType` 递归到子字段时把方向统一清成 `Default`。这是 Chisel 方向模型（u3-l2）与 FIRRTL 方向模型差异的「抹平」。

**练习 2**：`convertLazily` 相比 `convert` 改变了什么？为什么需要它？  
**参考答案**：它把 `components` 包进 `LazyList`，使模块**按需**翻译而不是一次性全算完。对超大电路，这能降低峰值内存、支持流式输出（与 Serializer 的惰性设计同理）。

---

### 4.4 Command → Statement：命令的逐条翻译（含 Reg 追踪）

#### 4.4.1 概念说明

模块体（`Block`）里装的就是一条条 `Command`。Converter 用 `convertCommand` 把每条命令翻译成一条 `fir.Statement`，再把一整批命令包进一个 `fir.Block`。这是 Converter 里**最长、也最能体现「逐形状映射」思想**的方法——几十种命令，每种一个 `case`。

本模块的重点是完成练习任务：**追踪一个 `Reg` 是如何变成 FIRRTL 中 `reg` 节点的**。

#### 4.4.2 核心流程

先看 Chisel 内部 IR 里寄存器有哪两种命令（u3-l5 / u4-l2 已讲）：

```scala
case class DefReg(sourceInfo, id: Data, clock: Arg)                            // 不带复位值
case class DefRegInit(sourceInfo, id, clock: Arg, reset: Arg, init: Arg)       // 带复位值
```

定义在 [IR.scala:328](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L328)（`DefReg`）和 [IR.scala:330](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L330)（`DefRegInit`）。它们在 Converter 里分别翻译为：

```
DefReg(id, clock)            → fir.DefRegister(info, name, tpe, clock)
DefRegInit(id, clock, reset, init)
                             → fir.DefRegisterWithReset(info, name, tpe, clock, reset, init)
```

注意一个细节：FIRRTL 原生的 `fir.DefRegister` 其实**自带 reset 参数**。Chisel 这里把「无复位」和「有复位」拆成两个 FIRRTL 节点类型（`DefRegister` vs `DefRegisterWithReset`），是为了精确表达 Chisel `Reg`（无复位值）与 `RegInit`（有复位值）的语义差异——这正是 u3-l5 讲过的「`RegInit` 把复位值直接写进节点」在 IR 层的体现。

#### 4.4.3 源码精读

`convertCommand` 的入口：[Converter.scala:133](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala#L133)。它的方法签名是 `def convertCommand(cmd: Command, ctx: Component, typeAliases: Seq[String]): fir.Statement`，注释明确说「Convert Commands that map 1:1 to Statements」。

**Reg 追踪的第一站——`DefReg` 分支**：

```scala
case e @ DefReg(info, id, clock) =>
  fir.DefRegister(
    convert(info),
    e.name,
    extractType(id, info, typeAliases),
    convert(clock, ctx, info)
  )
```

见 [Converter.scala:139](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala#L139)。它做了四件事：① 把 `info`（源信息）翻译成 `fir.Info`；② 取 `e.name`（寄存器名，来自 `id.getRef.name`）；③ 用 `extractType(id, ...)` 把寄存器的 `Data` 类型翻成 `fir.Type`（这是 4.5 的内容）；④ 用 `convert(clock, ctx, info)` 把时钟 `Arg` 翻成 `fir.Expression`。

**Reg 追踪的第二站——`DefRegInit` 分支**：

```scala
case e @ DefRegInit(info, id, clock, reset, init) =>
  fir.DefRegisterWithReset(
    convert(info),
    e.name,
    extractType(id, info, typeAliases),
    convert(clock, ctx, info),
    convert(reset, ctx, info),
    convert(init, ctx, info)
  )
```

见 [Converter.scala:146](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala#L146)。与上面相比，它多翻了 `reset` 和 `init` 两个 `Arg`，对应 `RegInit` 的复位信号与复位值。

**对比原语运算**：另一个常见的命令 `DefPrim`（如 `a + b`）翻译时，先把运算表达式 `convertPrim` 出来，再包成一条 `fir.DefNode`——[Converter.scala:134](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala#L134)。这印证了 u4-l2 的结论：「CHIRRTL 文本里的 `node` 关键字对应内部 IR 的 `DefPrim`」。Converter 里同样如此：`DefPrim → fir.DefNode(DoPrim)`。

**命令批量化**：`convertCommand` 处理单条命令，而 `convert(cmds: Seq[Command], ...)` 把一批命令逐条翻译后塞进 `fir.Block`：[Converter.scala:324](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala#L324)；处理 `Block`（含「secret commands」）的版本在 [Converter.scala:341](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala#L341)。模块体就是经这里被翻译成一整块 `fir.Block` 语句的。

#### 4.4.4 代码实践

这是本讲的主实践，目标：**在 Converter.scala 中找到把 Chisel Component 转换为 firrtl DefModule 的入口方法，追踪一个 Reg 是如何变成 FIRRTL 中 reg 节点的**。

1. **实践目标**：把「读源码」和「看输出」结合起来，确认 `DefReg`/`DefRegInit` 这两个内部 IR 节点，最终对应到 FIRRTL 的寄存器节点。

2. **操作步骤**：
   - 步骤 A（读源码）：从模块入口 [Converter.scala:484](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala#L484) 的 `DefModule` 分支出发，看到模块体由 `convert(block, ctx, typeAliases)` 翻译；点进 [Converter.scala:341](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala#L341) 的 `convert(Block)`，发现它对每条命令调用 `convertCommand`；再进 [Converter.scala:133](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala#L133)，定位到 `DefReg`（[L139](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala#L139)）与 `DefRegInit`（[L146](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala#L146)）两个分支。
   - 步骤 B（写最小用例）：写一个含 `Reg` 和 `RegInit` 的最小模块，例如（示例代码）：

     ```scala
     // 示例代码，非项目原有文件
     import chisel3._
     class RegDemo extends Module {
       val in  = IO(Input(UInt(8.W)))
       val out = IO(Output(UInt(8.W)))
       val r1  = RegNext(in)            // → DefReg + Connect
       val r2  = RegInit(0.U(8.W))      // → DefRegInit
       r2 := r1 + 1.U
       out := r2
     }
     ```
   - 步骤 C（观察输出）：用 `circt.stage.ChiselStage.emitCHIRRTL(new RegDemo)` 生成 CHIRRTL 文本。

3. **需要观察的现象**：输出文本里应出现 `reg r1 : UInt<8>, clock` 与 `regreset r2 : UInt<8>, clock, reset, ...` 两类寄存器声明。`reg` 对应无复位的 `DefReg`，`regreset` 对应有复位的 `DefRegInit`。

4. **预期结果**：CHIRRTL 里 `regreset` 关键字的存在，正是 `DefRegInit → fir.DefRegisterWithReset` 这条翻译规则的文本投影。注意：你看到的文本是 **Serializer**（u4-l4）产出的，但 Serializer 与 Converter 读的是**同一份内部 IR**（同样的 `DefReg`/`DefRegInit` 节点），因此文本里的 `reg`/`regreset` 完全可以作为「Converter 会翻译成什么」的可靠参照。

5. **运行前提**：若本地未配好 mill/firtool，本步骤标注为「待本地验证」；即使不运行，步骤 A 的源码追踪也能独立完成。

#### 4.4.5 小练习与答案

**练习 1**：在 `convertCommand` 里，`DefReg` 和 `DefRegInit` 分别翻成 FIRRTL 的哪个节点？为什么不是同一个？  
**参考答案**：`DefReg → fir.DefRegister`，`DefRegInit → fir.DefRegisterWithReset`。因为 Chisel 的 `Reg`（无复位值）与 `RegInit`（有复位值）语义不同：前者没有 `reset`/`init`，后者携带复位信号与复位值。Converter 用两个不同的 FIRRTL 节点类型精确表达这一差异，避免给无复位的寄存器凭空加上复位语义。

**练习 2**：`convertCommand` 处理 `DefPrim` 时，最终包成的是什么 FIRRTL 语句？为什么？  
**参考答案**：包成 `fir.DefNode(info, name, DoPrim(...))`。因为原语运算（如 `a+b`）在 FIRRTL 里是一个表达式（`DoPrim`），需要一个「具名节点」来承载，于是套一层 `DefNode`。这与 CHIRRTL 文本里出现的 `node _T = add(...)` 对应。

---

### 4.5 Arg → Expression 与 Data → Type：表达式与类型翻译

#### 4.5.1 概念说明

命令里出现的「操作数」（如 `clock`、`init`、`a+b` 里的 `a`）在内部 IR 里都是 `Arg`；Converter 用 `convert(arg, ctx, info)` 把它们翻译成 `fir.Expression`。而每个信号/端口的「类型」（`UInt(8.W)`、`Bundle{...}`）在内部是 `Data`，Converter 用 `extractType` 把它们翻译成 `fir.Type`。这两个方法支撑起前面所有命令的具体内容。

#### 4.5.2 核心流程

`Arg → fir.Expression` 的形状映射（节选）：

| 内部 `Arg` | 翻成 `fir.Expression` | 含义 |
| --- | --- | --- |
| `Ref(name)` | `fir.Reference(name, UnknownType)` | 最朴素的具名引用 |
| `Slot(imm, name)` | `fir.SubField(imm, name, ...)` | Bundle 字段访问 `a.b` |
| `Index(imm, LitIndex/ILit)` | `fir.SubIndex(imm, idx, ...)` | Vec 静态下标 `v(2)` |
| `Index(imm, value)` | `fir.SubAccess(imm, value, ...)` | Vec 动态下标 `v(i)` |
| `ULit(n, w)` | `fir.UIntLiteral(n, width)` | 无符号字面量 |
| `ModuleIO(mod, name)` | `Reference` 或 `SubField` | 端口引用（取决于是否在定义模块内） |
| `PrimExpr(op, args)` | `convertPrim → DoPrim` | 原语表达式 |

`Data → fir.Type` 的形状映射（节选）：

| 内部 `Data` | 翻成 `fir.Type` |
| --- | --- |
| `UInt(w)` / `SInt(w)` | `fir.UIntType(w)` / `fir.SIntType(w)` |
| `Clock` / `AsyncReset` | `fir.ClockType` / `fir.AsyncResetType` |
| `Vec[T]` | `fir.VectorType(elemType, length)` |
| `Record`（Bundle） | `fir.BundleType(fields)`，含 `fir.Field(name, Flip/Default, tpe)` |
| `EnumType` | `fir.UIntType(width)` |

#### 4.5.3 源码精读

**表达式翻译** `convert(arg: Arg, ctx, info): fir.Expression`：[Converter.scala:82](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala#L82)。这里能看到前文强调的「端口引用歧义」如何解决：

```scala
case ModuleIO(mod, name) =>
  if (mod eq ctx.id) fir.Reference(name, fir.UnknownType)        // 在定义模块内：直接 Reference
  else fir.SubField(fir.Reference(getRef(mod, info).name, ...), name, ...)  // 在父模块：包成 SubField
```

见 [Converter.scala:97](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala#L97)。`ctx.id eq mod` 判断「当前正在翻译的模块，是不是这个端口的定义模块」——这就是 `ctx` 必须一路透传的根本原因。字面量分支 `ULit → fir.UIntLiteral` 在 [Converter.scala:103](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala#L103)，`SLit` 负数则先转成对应无符号数再 `AsSInt`（[Converter.scala:107](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala#L107)）。

**类型翻译** `extractType`：入口 [Converter.scala:370](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala#L370)，主体 [Converter.scala:373](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala#L373)。叶子类型一行一个 case：

```scala
case _: Clock      => fir.ClockType
case _: AsyncReset => fir.AsyncResetType
case t: UInt       => fir.UIntType(convert(t.width))
case t: SInt       => fir.SIntType(convert(t.width))
```

见 [Converter.scala:397](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala#L397)。`Vec` 翻成 `VectorType`、`Record` 翻成 `BundleType`（注意它对 `t._elements.toIndexedSeq.reverse` 做了反转，呼应 u2-l3 讲的「字段内部按定义逆序存储、先定义在高位的约定」）。`extractType` 还处理 Probe（`ProbeType`/`RWProbeType`）、Const（`ConstType`）、类型别名（`AliasType`）等新概念，本讲只作了解。

值得强调：`extractType` 不仅是 Converter 的内部零件，它还被**实时**用于 elaboration 期的连线宽度计算（`BiConnect`、`Builder` 里直接调用 `Converter.extractType`）。也就是说，Converter 虽然整体是「事后翻译」，但它的类型翻译能力被借去参与了 elaboration 本身。

#### 4.5.4 代码实践

阅读 [Converter.scala:82](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala#L82) 的 `convert(Arg)`，回答：当你在父模块里访问子模块 `child` 的端口 `io`（即 `child.io`）时，它会被翻译成什么样的 `fir.Expression`？提示：走 `ModuleIO` 分支，且 `mod eq ctx.id` 为 false，所以是 `SubField(Reference("child"), "io")`。再用 4.4 的最小模块加一个子模块实例，观察 CHIRRTL 文本里子模块端口引用的写法（应类似 `child.io`）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `ModuleIO` 翻译时要判断 `mod eq ctx.id`？  
**参考答案**：同一个端口，在「定义它的模块内部」是直接的本地名字（`fir.Reference`），但在「父模块里通过实例访问」时则是实例的一个字段（`fir.SubField(实例名, 端口名)`）。`ctx` 记录了「当前正在翻译哪个模块」，用它来判断该用哪种形式。

**练习 2**：`extractType` 把 `Record`（Bundle）翻成 `BundleType` 时，为什么要对元素做 `reverse`？  
**参考答案**：因为 Chisel 内部把 Bundle 的字段按**定义的逆序**存储（见 u2-l3），而 FIRRTL 文本里字段需按定义顺序、高位在前。反转一次正好恢复成定义顺序，并保证「先定义的字段位于高位」的位排布约定与 `asUInt` 一致。

---

## 5. 综合实践

把本讲知识串起来，完成下面这个「翻译官体检」小任务。

**任务**：给一个稍复杂的模块，画出它从「Chisel 内部 IR」到「`firrtl.ir.Circuit`」的完整翻译树。

```scala
// 示例代码，非项目原有文件
import chisel3._
class Top extends Module {
  val a   = IO(Input(UInt(4.W)))
  val b   = IO(Output(Bool()))
  val cnt = RegInit(0.U(4.W))       // DefRegInit
  when(a > cnt) { cnt := cnt + 1.U } // When + Connect + DefPrim
  b := cnt(0)                        // Connect + Index
}
```

要求按下列步骤完成：

1. **预测内部 IR**：先不看输出，写出这个模块的 `Block` 里大概会有哪些 `Command`（提示：`DefRegInit`、`When`（内含 `Connect` 与 `DefPrim(DefPrim add/eq)`）、`Connect`、`Index`）。
2. **定位翻译规则**：在 [Converter.scala:133](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala#L133) 的 `convertCommand` 里，为每条命令找到它对应的 `case` 分支与产出的 `fir.Statement`。特别注意 `When` 分支（[Converter.scala:289](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala#L289)），它递归地把 `ifRegion`/`elseRegion` 翻成 `fir.Conditionally`。
3. **画出翻译树**：以 `convert(Circuit)` 为根，画出 `Circuit → fir.Circuit`、`DefModule → fir.Module`、各 `Command → fir.Statement`、各 `Arg → fir.Expression`、各 `Data → fir.Type` 的对应关系。
4. **对照输出验证**：用 `emitCHIRRTL(new Top)` 生成文本（Serializer 产出，但反映同一份 IR），核对：`regreset cnt`（对应 `DefRegInit`）、`when`（对应 `When`）、`node _T = gt(...)` / `add(...)`（对应 `DefPrim → fir.DefNode`）。

**预期结果**：你能用一张表/树把「Chisel 内部 IR 节点 → Converter case 分支 → firrtl.ir 节点 → CHIRRTL 文本关键字」四列对应清楚，并理解每一步翻译的「形状」几乎都是 1:1 的，Converter 本质上是一个大型、无状态的形状映射函数。运行部分若本地未配环境，标注「待本地验证」，源码追踪与表格绘制仍可独立完成。

## 6. 本讲小结

- Chisel 源码里存在**两个 Circuit**：`chisel3.internal.firrtl.ir.Circuit`（私有内部 IR）与 `firrtl.ir.Circuit`（FIRRTL 编译器 IR），Converter 是两者之间的翻译桥。
- `ElaboratedCircuit` 是 elaboration 结果的对外封装 trait，把内部 `_circuit` 藏在 `private[chisel3]` 之后；它的默认文本序列化走的是 **Serializer**，不是 Converter。
- Converter 是一个 `private[chisel3] object`，靠一组重载的 `convert`/`convertCommand`/`extractType` 方法，自顶向下递归地把 Circuit→Component→Port→Command→Arg→Data 翻译成对应的 `fir.*` 节点。
- 模块翻译入口是 `convert(component): fir.DefModule`（[L484](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala#L484)），命令翻译入口是 `convertCommand`（[L133](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala#L133)）；`DefReg → fir.DefRegister`、`DefRegInit → fir.DefRegisterWithReset` 是本讲重点追踪的映射。
- `ctx: Component` 一路透传，是为了解决「端口引用在模块内外翻译形态不同」的歧义；`extractType` 不仅是翻译零件，还被实时用于 elaboration 期的连线类型/宽度计算。
- 这条「全量翻译成 `firrtl.ir.Circuit`」的路径**自 Chisel 7.11.0 起被 deprecated**（见 `Convert` 阶段 [Convert.scala:16](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Convert.scala#L16)），现代发射改走 `ElaboratedCircuit` 直连；但 Converter 仍是理解「Chisel IR 如何对应 FIRRTL」最权威的代码级文档。

## 7. 下一步学习建议

- **进入 u5 系列**：本讲讲清了「内部 IR → firrtl.ir.Circuit」的翻译，下一讲 u5-l1（Stage/Phase 管道）会展示这条翻译在编译管道里的位置，并解释为什么 `Convert` 阶段会被 deprecated、现代路径如何绕过它直连 Panama CIRCT。
- **对比读 Serializer**（u4-l4）：把 `Serializer` 与 `Converter` 并排读，体会「同一份内部 IR，一条走向文本、一条走向 AST」的设计。
- **读 deprecated 的 `Convert` 阶段**：[Convert.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Convert.scala) 是唯一全量调用 `Converter.convert(circuit)` 的地方，配合 `@deprecated` 注解理解迁移方向。
- **延伸阅读**：`extractType` 在 `BiConnect.scala`、`Builder.scala` 里的实时调用点，能帮你理解「翻译能力」如何反哺 elaboration 本身。
