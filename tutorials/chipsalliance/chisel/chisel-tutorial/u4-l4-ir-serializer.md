# IR 序列化：Serializer

## 1. 本讲目标

本讲承接 [u4-l2 命令记录与内部 FIRRTL IR](u4-l2-internal-firrtl-ir.md)，回答一个具体问题：**elaboration 期间长出来的那棵 Scala 对象树（Circuit ⊃ Component ⊃ Command），是怎么变成你在屏幕上看到的 CHIRRTL 文本的？**

学完后你应当能够：

1. 说清 `Serializer` 这个对象的分发结构：它如何按 IR 的三层（Circuit / Component / Command）逐层调用不同的 `serialize` 方法。
2. 看懂任意一条命令（如 `Connect`、`DefPrim`、`DefRegInit`）在源码中被翻译成哪一行 CHIRRTL 文本。
3. 区分一个容易混淆的点：CHIRRTL 文本里的 `node` 关键字，对应的是 Chisel 内部 IR 的 **`DefPrim`** 节点，而不是一个叫 `DefNode` 的类。
4. 理解 `printf(p"...")` 背后的 `Printable` / `PrintableHelper` 格式化机制，以及它和 `Serializer` 的衔接点。
5. 亲手用 `ChiselStage.emitCHIRRTL` 生成一段 CHIRRTL，并回到 `Serializer.scala` 找到生成它的那几行代码。

---

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 什么是 CHIRRTL

FIRRTL 是一种硬件中间表示语言，有自己的文本语法。**CHIRRTL**（Chisel FIRRTL）是 FIRRTL 的一个超集/方言，多了一些「Chisel 私有」的语法（例如 `cmem`/`smem` 内存、`mport` 内存端口、`printf`/`stop` 等），这些是 Chisel elaboration 的直接产物，下游 CIRCT（firtool）会把它们规整成标准 FIRRTL 再生成 Verilog。

你用 `ChiselStage.emitCHIRRTL(...)` 看到的文本，就是 `Serializer` 的输出。

### 2.2 序列化 = 模式匹配 + 拼 StringBuilder

`Serializer` 没有「先构造一棵语法树再 pretty-print」的设计，而是最朴素的写法：**对每一种 IR 节点写一个 `case` 分支，分支里直接往一个 `StringBuilder` 里追加字符**。整体看像一张巨大的「节点类型 → 文本片段」对照表。

这种写法的优点是直白、易对照；代价是 `Serializer.scala` 是个近 830 行的大对象，但只要抓住它的**分发骨架**（见 4.1），就不会迷路。

### 2.3 Printable 是什么

硬件里有些节点需要携带「格式化文本」，最典型的是 `printf` 的格式串。`Printable` 是这些文本片段的抽象基类，它不是「立即算出一个字符串」，而是**延迟到序列化时**才把格式串和数据引用拆开（`unpack`）。这与 `Serializer` 在第 4.3 节会合流。

> 关键术语复习（来自 u4-l2）：`Command`（命令）是对已有信号做动作或定义新硬件；`Definition` 是带 `id`/`name` 的 `Command` 子类；`Arg` 是表达式/引用（如 `Node`、`Ref`、`ULit`、`PrimExpr`）。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [core/src/main/scala/chisel3/internal/firrtl/Serializer.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Serializer.scala) | 本讲主角。把内部 IR 序列化成 CHIRRTL 文本的全部逻辑。 |
| [core/src/main/scala/chisel3/internal/firrtl/IR.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala) | 被序列化的 IR 节点定义（`Circuit`/`Component`/`Command`/`Arg` 等）。对照阅读才能知道每个 `case` 匹配的是什么。 |
| [core/src/main/scala/chisel3/Printable.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Printable.scala) | `Printable` 抽象及其子类（`PString`/`Decimal`/`Name` 等），`printf` 格式化的核心。 |
| [core/src/main/scala/chisel3/package.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/package.scala) | 提供 `PrintableHelper` 隐式类，即 `p"..."` / `cf"..."` 字符串插值器。 |
| [core/src/main/scala/chisel3/ElaboratedCircuit.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/ElaboratedCircuit.scala) | `Serializer.lazily` 的调用方，是用户 API（`emitCHIRRTL`）与 `Serializer` 之间的桥。 |
| [src/main/scala/circt/stage/ChiselStage.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala) | 用户入口 `emitCHIRRTL`，最终走到序列化。 |

---

## 4. 核心概念与源码讲解

### 4.1 序列化的整体架构：三层分发

#### 4.1.1 概念说明

`Serializer` 是一个 `private[chisel3] object`（包私有单例），它**对内**消费 Chisel 的内部 IR 树，**对外**产出 CHIRRTL 文本。

回忆 u4-l2 的结论：内部 IR 是三层树 `Circuit ⊃ Component ⊃ Command`。`Serializer` 的设计恰好**为每一层配备了一个 `serialize` 方法**，逐层下钻：

- `serialize(circuit: Circuit, annotations)` —— 顶层电路
- `serialize(component: Component, typeAliases)` —— 模块（`DefModule`/`DefBlackBox`/…）
- `serialize(block: Block, ctx, typeAliases)` + `serializeCommand(...)` —— 模块体内的命令

最底层是 `serialize(arg: Arg, ...)`，负责把**表达式/引用**（`Node`、`ULit`、`PrimExpr`…）拼成文本。所以整张分发图是：

```
serialize(Circuit)              → 电路头 + 逐个模块
  └─ serialize(Component)       → "module NAME :" + 端口 + 体
       └─ serialize(Block)      → 遍历命令
            └─ serializeCommand → when/layerblock 等嵌套结构，其余转交
                 └─ serializeSimpleCommand → 单行命令（connect/node/wire/…）
                      └─ serialize(Arg)    → 表达式（add(a,b)、字面量、引用）
```

公共入口是 `Serializer.lazily`：它返回一个**惰性的** `Iterable[String]`，每段字符串是 CHIRRTL 的一行（或多行）。惰性是为了降低峰值内存、并支持超过 2 GiB 的超大电路。

#### 4.1.2 核心流程

整个序列化从用户调用到文本，链路如下（粗箭头表示调用）：

1. 用户写 `ChiselStage.emitCHIRRTL(new MyMod)`。
2. `emitCHIRRTL` 跑完 Phase 管道，拿到 `ElaboratedCircuit`，调用 `elaboratedCircuit.serialize(annos)`。
3. `ElaboratedCircuit.serialize` 等价于 `lazilySerialize(annos).mkString`。
4. `lazilySerialize` 调用 `Serializer.lazily(circuit, annos)`。
5. `Serializer.lazily` 返回惰性 `Iterable[String]`，`mkString` 拼成最终字符串。

关键设计点：**所有 `serialize` 方法都返回 `Iterator[String]`**（除表达式层直接写 `StringBuilder`）。用 `Iterator` 而非提前拼好 `String`，是为了让超大电路可以「流式」产出，不一次性占用整段内存。`Serializer` 内部用 `implicit b: StringBuilder` 与 `implicit indent: Int` 两个隐式参数在线程内传递「当前输出缓冲」与「当前缩进层级」。

#### 4.1.3 源码精读

对象本体与版本声明。`Serializer` 支持的 FIRRTL 版本写死为 `7.0.0`，会作为文本第一行输出：

[core/src/main/scala/chisel3/internal/firrtl/Serializer.scala:24-29](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Serializer.scala#L24-L29) —— `private[chisel3] object Serializer`，常量 `NewLine`/`Indent` 与 `val version = "7.0.0"`。

公共入口 `lazily`，用一个匿名 `Iterable` 包裹真正的 `serialize(circuit, annotations)` 迭代器，实现惰性：

[core/src/main/scala/chisel3/internal/firrtl/Serializer.scala:830-832](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Serializer.scala#L830-L832) —— `def lazily(circuit, annotations): Iterable[String]`，`iterator = serialize(circuit, annotations)`。

调用方在 `ElaboratedCircuit` 的实现里，一行就能把电路交给 `Serializer`：

[core/src/main/scala/chisel3/ElaboratedCircuit.scala:73-88](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/ElaboratedCircuit.scala#L73-L88) —— `serialize` 即 `lazilySerialize.mkString`，而 `lazilySerialize` 调 `Serializer.lazily(circuit, annotations.toSeq)`。

顶层 `serialize(Circuit)` 负责电路「头」：先输出 `FIRRTL version 7.0.0` 与 `circuit NAME :`，若有注解则追加 `%[...JSON...]`，再依次拼上 options、type aliases、layers、domains，最后遍历 `circuit.components`（即所有模块）：

[core/src/main/scala/chisel3/internal/firrtl/Serializer.scala:781-828](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Serializer.scala#L781-L828) —— `private def serialize(circuit: Circuit, annotations)`，先写 prelude，再用 `prelude ++ options ++ typeAliases ++ layers ++ domains ++ 各模块` 串成一条迭代器链。

模块层 `serialize(Component)` 按 `DefModule`/`DefBlackBox`/`DefIntrinsicModule`/`DefClass`/`DefTestMarker` 分别处理。最常见的 `DefModule` 分支输出 `module NAME :`，随后逐个序列化端口，再交给 `serialize(block, ...)` 处理模块体：

[core/src/main/scala/chisel3/internal/firrtl/Serializer.scala:643-657](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Serializer.scala#L643-L657) —— `DefModule` 分支：写头、写端口（含 `secretPorts`）、空行，再 `Iterator(start) ++ serialize(block, ctx, typeAliases)`。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：在脑中建立「一个 `module` 块从上到下是怎么被拼出来的」的完整路径。
2. **步骤**：
   - 打开 `Serializer.scala`，定位 4.1.3 列出的四个方法（`lazily` / `serialize(Circuit)` / `serialize(Component)` 的 `DefModule` 分支 / `serialize(Block)`）。
   - 在 [IR.scala:596-603](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L596-L603) 确认 `DefModule` 持有 `ports: Seq[Port]` 与 `block: Block`。
3. **观察现象**：注意 `DefModule` 分支里 `(ports ++ ctx.secretPorts).foreach` 与两处 `newLineNoIndent()`，理解端口声明和模块体之间的空行从何而来。
4. **预期结果**：你能用一句话说出「`module Foo :` 这一行加上它下面的端口和命令，分别由 `serialize(Component)` 和 `serialize(Block)` 两个方法产出」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `serialize` 各方法返回 `Iterator[String]` 而不直接返回 `String`？

> **答案**：为了让超大电路可以「流式」逐行产出，避免一次性把整段 CHIRRTL（可能超过 2 GiB）驻留内存。`Serializer.lazily` 返回的惰性 `Iterable[String]` 正是建立在这些 `Iterator` 之上。

**练习 2**：电路文本第一行的 `FIRRTL version 7.0.0` 来自哪里？

> **答案**：来自 [Serializer.scala:29](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Serializer.scala#L29) 的 `val version = "7.0.0"`，由 `serialize(Circuit)` 的 prelude 在 [第 786 行](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Serializer.scala#L786) 拼出。

---

### 4.2 命令与表达式的文本化规则

#### 4.2.1 概念说明

模块体里的每一条 `Command` 都要变成一行（或多行）CHIRRTL。`Serializer` 把命令分成两类处理：

- **嵌套型命令**（`When`、`LayerBlock`、`Placeholder`、`DefContract`）：它们内部还套着一段 `Block`，会展开成带缩进的多行文本。这类在 `serializeCommand` 里**就地**处理，并通过递归把 `indent + 1` 传给子命令。
- **单行型命令**（`Connect`、`DefPrim`、`DefWire`、`DefReg`、`DefRegInit`、`DefMemory`、`Printf`、`Stop`…）：只产生一行，统一交给 `serializeSimpleCommand`。

表达式层 `serialize(Arg)` 则负责命令内部出现的「右值」，例如 `connect a, add(b, c)` 里的 `add(b, c)`。

> ⚠️ **本讲最重要的辨析**：你在 CHIRRTL 里频繁看到 `node _T = add(a, b)` 这样的行，这里的 `node` 关键字对应的内部 IR 节点是 **`DefPrim`**（原语运算定义），**并不是**一个叫 `DefNode` 的类。如 u4-l2 所述，`DefNode` 是**下游** FIRRTL IR 的节点名，Chisel 内部用 `DefPrim` 表达原语运算，文本里的 `node` 只是 `Serializer` 给 `DefPrim` 起的「行首词」。这是初读源码最容易卡住的地方。

#### 4.2.2 核心流程

以一个简单模块为例，把 Chisel 代码、内部 IR 节点、序列化文本三者对齐：

```scala
class Adder extends Module {
  val io = IO(new Bundle { val a = Input(UInt(8.W)); val b = Input(UInt(8.W)); val y = Output(UInt(8.W)) })
  io.y := io.a + io.b
}
```

`io.a + io.b` 会产生一个 `DefPrim(AddOp)`，`io.y := ...` 产生一个 `Connect`。序列化后的模块体大致是：

```
node _T = add(io.a, io.b)        ; 来自 DefPrim
connect io.y, _T                  ; 来自 Connect
```

文本化的几条通用规则：

- 每个 `Definition` 都有 `name`（取自 `id.getRef.name`），序列化时先写「行首词 + 名字」。
- 缩进由隐式 `indent: Int` 控制；`doIndent(inc)` 写 `indent + inc` 个 `"  "`。
- 标识符合法化 `legalize`：以数字开头的名字加反引号。
- 源信息 `@[file line]` 由 `serialize(info)` 追加，可被 `suppressSourceInfo` 关闭。

#### 4.2.3 源码精读

命令分发的总入口。注意它只显式处理「嵌套型」四种，**其余全部 fall through 到 `serializeSimpleCommand`**：

[core/src/main/scala/chisel3/internal/firrtl/Serializer.scala:377-454](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Serializer.scala#L377-L454) —— `serializeCommand`，先 `case When` / `LayerBlock` / `Placeholder` / `DefContract`，最后 `case simple => serializeSimpleCommand(simple, ...)`。

`When` 分支最能体现「嵌套展开成多行 + 缩进」的思路：它把 `when <pred> :` 当作 `start`，把 then 区命令递归（缩进 +1）当作 `middle`，把 `else :` 与 else 区当作 `end`，三者用 `++` 拼成一条迭代器：

[core/src/main/scala/chisel3/internal/firrtl/Serializer.scala:382-406](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Serializer.scala#L382-L406) —— `case When(info, pred, ifRegion, elseRegion)`：then 区为空时输出 `skip`，否则递归 `serializeCommand` 并把缩进加 1。

单行命令的「大表」。这里摘关键的几条对照：

- **`DefPrim`**（原语运算 → 文本 `node name = op(args)`）：

[core/src/main/scala/chisel3/internal/firrtl/Serializer.scala:209-212](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Serializer.scala#L209-L212) —— `case e: DefPrim[_] =>` 写 `"node " + name + " = "`，再调 `serializePrim(e.op, e.args, ...)`，最后追加源信息。这就是 `add`/`mul`/`and` 等运算在文本里以 `node` 开头的原因。

- **`Connect`**（连线 → 文本 `connect loc, exp`）：

[core/src/main/scala/chisel3/internal/firrtl/Serializer.scala:272-273](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Serializer.scala#L272-L273) —— `case Connect(info, loc, exp)`：`"connect "` + 序列化左值 + `", "` + 序列化右值。

- **`DefWire`**（线网 → `wire name : <type>`）、**`DefReg`**（寄存器 → `reg name : <type>, <clock>`）、**`DefRegInit`**（带复位寄存器 → `regreset ...`）：

[core/src/main/scala/chisel3/internal/firrtl/Serializer.scala:213-237](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Serializer.scala#L213-L237) —— 三条相邻分支，类型部分统一交给 `serializeType(...)`。

- **`DefMemory` / `DefSeqMemory`**（内存 → `cmem`/`smem`），这是 CHIRRTL 私有语法（呼应 u3-l6 的 `Mem`/`SyncReadMem`）：

[core/src/main/scala/chisel3/internal/firrtl/Serializer.scala:238-247](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Serializer.scala#L238-L247) —— `cmem`/`smem`，末尾 `[size]`，`DefSeqMemory` 还会写 `readUnderWrite`。

表达式/引用层 `serialize(Arg)`。这里把右值拆成最小片段。几个常见分支：

[core/src/main/scala/chisel3/internal/firrtl/Serializer.scala:116-166](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Serializer.scala#L116-L166) —— `serialize(arg: Arg, ctx, info)`，逐个 `case`：`Node` 取 ref、`Ref` 直接写名字、`Slot` 写 `imm.name`、`Index` 写 `imm[idx]`、`ULit` 写 `UInt<W>(0h..)` 等。

其中无符号字面量与有符号字面量的文本规则，是「一眼看懂 CHIRRTL 字面量」的钥匙：

[core/src/main/scala/chisel3/internal/firrtl/Serializer.scala:134-148](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Serializer.scala#L134-L148) —— `ULit(n, w)` → `UInt<W>(0h十六进制)`，未知宽度时用 `minWidth`；`SLit(n, w)` → `SInt<W>(-0h十六进制)`，负数前加 `-`。

原语表达式的拼装（`add(a, b)` 这种），由 `serializePrim` 负责，它是 `serialize(Arg)` 里 `case e: PrimExpr[_]` 的实现：

[core/src/main/scala/chisel3/internal/firrtl/Serializer.scala:104-114](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Serializer.scala#L104-L114) —— `serializePrim`：写 `op.name + '('`，参数间用 `", "` 连接，最后 `')'`。

辅助工具：合法化名字与缩进。

[core/src/main/scala/chisel3/internal/firrtl/Serializer.scala:39-43](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Serializer.scala#L39-L43) —— `legalize`：名字以数字开头时加反引号，保证是合法 FIRRTL 标识符。

[core/src/main/scala/chisel3/internal/firrtl/Serializer.scala:53-55](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Serializer.scala#L53-L55) —— `doIndent`：写 `indent + inc` 个 `Indent`（两个空格）。

最后别忘了**类型序列化** `serializeType`，它把 `UInt(8.W)` 变成 `UInt<8>`、把 `Bundle` 变成 `{ a : UInt<8>, flip b : ... }`。`DefWire`/`DefReg`/端口声明都依赖它：

[core/src/main/scala/chisel3/internal/firrtl/Serializer.scala:501-572](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Serializer.scala#L501-L572) —— `serializeType`：按 `Data` 子类型分发，`UInt`/`SInt` 调 `serialize(width)` 追加 `<W>`，`Record` 输出花括号聚合，`Vec` 末尾追加 `[length]`，并处理 Probe/const/Property。

#### 4.2.4 代码实践（动手验证型，本讲主实践）

这是本讲的核心实践，来自规格要求。

1. **目标**：用 `ChiselStage.emitCHIRRTL` 生成一段 CHIRRTL，再回到 `Serializer.scala` 找到产出每一行的那段代码，亲手验证「文本 ↔ 源码」的对应关系。
2. **操作步骤**：
   - 在一个能引用 Chisel 的环境（Scala REPL 或一个最小 `mill`/`sbt` 模块）里运行下面的**示例代码**：

     ```scala
     // 示例代码：非项目原有代码，仅用于演示
     import chisel3._
     class Adder extends Module {
       val io = IO(new Bundle {
         val a = Input(UInt(8.W))
         val b = Input(UInt(8.W))
         val y = Output(UInt(8.W))
       })
       io.y := io.a + io.b   // 产生 DefPrim(AddOp) 与 Connect
     }
     println(ChiselStage.emitCHIRRTL(new Adder))
     ```
   - 打开 [Serializer.scala:204-273](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Serializer.scala#L204-L273)，对照输出文本逐行找源头。
3. **观察现象**：输出大致长这样（端口顺序、临时名 `_T` 可能因版本略有差异）：

   ```
   module Adder :
     input clock : Clock
     input reset : Reset
     output io : { a : UInt<8>, b : UInt<8>, y : UInt<8>}
     ...
     node _T = add(io.a, io.b)
     connect io.y, _T
   ```
4. **预期结果**：
   - `node _T = add(io.a, io.b)` 这一行，由 [Serializer.scala:209-212](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Serializer.scala#L209-L212) 的 `DefPrim` 分支 + [serializePrim](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Serializer.scala#L104-L114) 产出。**确认文本里的 `node` 对应的 IR 类是 `DefPrim`，而非 `DefNode`。**
   - `connect io.y, _T` 由 [Serializer.scala:272-273](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Serializer.scala#L272-L273) 的 `Connect` 分支产出。
   - 端口类型 `{ a : UInt<8>, ...}` 由 [serializeType](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Serializer.scala#L540-L567) 的 `Record` 分支产出。
5. **若无法运行**：标注「待本地验证」。即便不运行，你也可以静态地把上面的输出文本与 4.2.3 的源码片段一一对应。

#### 4.2.5 小练习与答案

**练习 1**：把 `io.y := io.a + io.b` 改成 `io.y := (io.a + io.b) & io.a`，生成的 CHIRRTL 会多出哪一行？它由哪个 `case` 产出？

> **答案**：会多一行 `node _T_1 = and(_T, io.a)`（名字可能略有不同）。`add` 那行由 `DefPrim` 产出，新增的 `and` 同样由 [Serializer.scala:209-212](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Serializer.scala#L209-L212) 的 `DefPrim` 产出，运算名 `and` 来自 [IR.scala:40](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L40) 的 `BitAndOp = PrimOp("and")`。

**练习 2**：`5.U(8.W)` 在 CHIRRTL 里长什么样？为什么？

> **答案**：`UInt<8>(0h5)`。由 [Serializer.scala:134-139](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Serializer.scala#L134-L139) 的 `ULit` 分支产出：`UInt<` + 宽度 + `>(0h` + `n.toString(16)` + `)`。

**练习 3**：为什么 `when` 块需要 `serializeCommand` 单独处理，而不能进 `serializeSimpleCommand`？

> **答案**：因为 `when` 内部套着一段 `Block`，会展开成「`when pred :` + 若干缩进的子命令 + 可选 `else :` + 子命令」的多行结构，且子命令要把缩进层级加 1。`serializeSimpleCommand` 只产出单行，无法表达嵌套，所以 `When`（以及 `LayerBlock`、`DefContract`）在 [serializeCommand:382-406](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Serializer.scala#L382-L406) 里递归处理。

---

### 4.3 Printable 格式化机制

#### 4.3.1 概念说明

硬件里有些命令需要携带「带占位符的文本」，最典型的是 `printf`：你要写 `printf(p"x = $x")`，其中 `$x` 是一个硬件信号引用。问题是——这条 `printf` 在 elaboration 时被记成一条 `Printf` 命令，但此时信号 `x` 的**最终名字**可能还没定下来（命名发生在更后面）。

`Printable` 就是为解决这个问题设计的**延迟格式化对象**：

- 它**不立即求值**成字符串，而是把「格式串」和「数据引用」分开存放。
- 等到 `Serializer` 真正要输出这条 `printf` 时（此时名字已定），再调用 `Printable.unpack` 拆出 `(格式串, Seq[Data])`，由 `Serializer` 转成 FIRRTL 的 `printf(clock, en, "格式", 参数...)`。

`PrintableHelper` 则是面向用户的语法糖：它是 `StringContext` 上的隐式类，让你能用 `p"..."` 和 `cf"..."` 两种字符串插值器方便地构造 `Printable`。

#### 4.3.2 核心流程

从用户代码到 CHIRRTL 的流程：

1. 用户写 `printf(p"value = $io.a\n")`。
2. 编译器把 `p"value = $io.a\n"` 翻成 `PrintableHelper(sc).p(io.a)`，其中 `sc` 是由字符串片段组成的 `StringContext`。
3. `p` 把所有 `%` 转义成 `%%`（因为 `p` 插值器不把 `%` 当格式符），再交给 `cf`。
4. `cf` 把每个插值参数按规则包成具体 `Printable` 子类：Chisel `Bits` 类型 + `%x` → `Hexadecimal`，默认 → 调 `Data.toPrintable`，纯 Scala 类型 → `PString`。最终拼出一个 `Printables(List(...))`。
5. 这条 `Printf` 命令被序列化时，`Serializer` 调 `Serializer.unpack(pable, ctx, info)`：先用 `Printable.resolve` 把 `Name`/`FullName` 等「Chisel 期可解析」的部分解析成字符串，再 `unpack` 得到 `(格式串, Seq[Arg])`，拼成 `printf(clock, UInt<1>(0h1), "格式", 参数...)`。

`Printable` 的子类家族一览：

| 子类 | 含义 | unpack 贡献 |
|------|------|-------------|
| `PString(str)` | 纯文本片段 | 字符串本身（`%` 转义为 `%%`） |
| `Printables(pables)` | 多个 Printable 拼接 | 各自 unpack 后拼接 |
| `Decimal`/`Hexadecimal`/`Binary`/`Character` | Bits 的 `%d/%x/%b/%c` 格式 | `"%d"` 等 + 该 Bits |
| `Name(data)` | 信号名（`%n`） | `"%n"` + data |
| `FullName(data)` | 全限定名（`%N`） | `"%N"` + data |
| `Percent` | 字面 `%` | `"%%"` |
| `HierarchicalModuleName` | Verilog `%m` | `"%m"` |

#### 4.3.3 源码精读

`Printable` 抽象基类与核心方法 `unpack`：

[core/src/main/scala/chisel3/Printable.scala:50-72](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Printable.scala#L50-L72) —— `sealed abstract class Printable`，核心是 `def unpack: (String, Seq[Data])`，把自身拆成「FIRRTL printf 格式串 + 数据参数」。其余两个 `unpack(ctx)` 是 7.0.0 起废弃的旧重载。

`PString` 与 `Printables` 这两个基础积木：

[core/src/main/scala/chisel3/Printable.scala:234-243](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Printable.scala#L234-L243) —— `PString` 的 `unpack` 把字符串里的 `%` 替换成 `%%`（FIRRTL 里 `%` 是格式符前缀，字面百分号要转义），参数列表为空。

[core/src/main/scala/chisel3/Printable.scala:227-230](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Printable.scala#L227-L230) —— `Printables` 的 `unpack` 把各子片段的格式串 `mkString`、参数 `flatten`。

Bits 格式化家族的代表 `Decimal`：

[core/src/main/scala/chisel3/Printable.scala:338-340](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Printable.scala#L338-L340) —— `case class Decimal(bits, width)`，`unpack = ("%" + width.toFormatString + "d", List(bits))`，把宽度修饰拼进 `%d`。

`Serializer` 与 `Printable` 的衔接点。`Printf` 命令序列化时，调 `Serializer.unpack` 把 `Printable` 拆成 FIRRTL 可用的 `(格式串, Seq[Arg])`：

[core/src/main/scala/chisel3/internal/firrtl/Serializer.scala:86-91](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Serializer.scala#L86-L91) —— `def unpack(pable, ctx, sourceInfo)`：先 `Printable.resolve(pable, ctx)` 解析 Chisel 期可定的部分，再 `resolved.unpack` 取 `(fmt, data)`，最后 `data.map(_.ref)` 把 `Data` 转成 `Arg`。

`resolve` 的作用——把 `Name`/`FullName` 等替换成具体字符串：

[core/src/main/scala/chisel3/Printable.scala:199-205](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Printable.scala#L199-L205) —— `private[chisel3] def resolve(pable, ctx)`：用 `map` 遍历，把 `Name(data)` 换成 `PString(data.ref.name)`、`FullName(data)` 换成全名、特殊替换对象换成其字符串，其余原样保留。

`Printf` 命令本身的序列化，能看到格式串如何被转义、参数如何拼接：

[core/src/main/scala/chisel3/internal/firrtl/Serializer.scala:300-313](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Serializer.scala#L300-L313) —— `case e @ Printf(...)`：调 `unpack(pable, ctx, info)` 得 `(fmt, args)`，写 `printf(` 或 `fprintf(`，时钟、使能 `UInt<1>(0h1)`，再用 `fir.StringLit(fmt).escape` 转义格式串，参数逐一序列化。

最后是面向用户的 `PrintableHelper` 隐式类，即 `p"..."` 与 `cf"..."` 的来源：

[core/src/main/scala/chisel3/package.scala:168-177](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/package.scala#L168-L177) —— `implicit class PrintableHelper(val sc: StringContext)`，`def p(args)` 把 `%` 转义后委托给 `cf`。

[core/src/main/scala/chisel3/package.scala:229-230](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/package.scala#L229-L230) —— `def cf(args: Any*)`：增强版 `f` 插值器，按说明文档（`%n`/`%N`/`%d`/`%x`/`%b`/`%c` 及默认 `toPrintable`）把每个参数包成对应 `Printable` 子类。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：理解一条 `printf(p"...")` 从用户代码到 CHIRRTL 文本的完整数据流。
2. **步骤**：
   - 阅读 [package.scala:168-177](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/package.scala#L168-L177)，确认 `p"..."` 实际调用 `cf`。
   - 阅读 [Serializer.scala:300-313](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Serializer.scala#L300-L313) 的 `Printf` 分支，找到 `unpack(pable, ctx, info)` 的调用点。
   - 顺着跳到 [Serializer.scala:86-91](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Serializer.scala#L86-L91)，再跳到 [Printable.scala:199-205](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Printable.scala#L199-L205) 的 `resolve`。
3. **观察现象**：注意 `resolve` 在「序列化时」（而非 elaboration 时）才把 `Name(data)` 解析成 `data.ref.name`——这正是 `Printable` 必须延迟求值的根本原因：elaboration 时信号名尚未确定。
4. **预期结果**：你能画出这条链路：`printf(p"...")` → `PrintableHelper.p` → `cf` 生成 `Printable` → 存入 `Printf` 命令 → `Serializer` 序列化时 `resolve` + `unpack` → FIRRTL `printf(clock, en, fmt, args...)`。
5. **若想进一步验证**：把 4.2.4 的 `Adder` 里加一行 `printf(p"y=%d\n", io.y)`，运行 `emitCHIRRTL`，在输出里找到 `printf(...)` 行，对照本节源码确认格式串与参数。该步**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Printable` 不能在 elaboration 时就立即算出最终字符串？

> **答案**：因为 `printf` 在 elaboration 早期就被记成一条命令，而其中引用的信号最终名字（`data.ref.name`）要到更后面的命名阶段才确定。`Printable` 把求值推迟到 `Serializer` 序列化时（经 `resolve`），此时名字已定，才能正确输出。

**练习 2**：`p"a=%d"` 和 `cf"a=%d"`（假设参数是某个 `UInt`）产生的 `Printable` 有何本质区别？

> **答案**：`p` 插值器不把 `%` 当格式符，会先把 `%` 转义成 `%%` 再交给 `cf`（见 [package.scala:173-177](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/package.scala#L173-L177)）；所以 `p"a=%d"` 里 `%d` 会变成字面文本。而 `cf"a=%d"` 会把 `%d` 当作格式符，参数为 `UInt` 时包成 `Decimal`（见 [Printable.scala:338-340](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Printable.scala#L338-L340)）。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成一个「自造 CHIRRTL 调试器」的小任务。

**任务**：写一个包含多种构造的小模块，生成 CHIRRTL，然后用本讲学到的「文本 ↔ 源码」对照能力，为输出的**每一行**标注它由 `Serializer.scala` 的哪一段代码产出。

**示例代码**（非项目原有代码）：

```scala
import chisel3._
class Demo extends Module {
  val io = IO(new Bundle {
    val en   = Input(Bool())
    val addr = Input(UInt(4.W))
    val data = Output(UInt(8.W))
  })
  val sum = RegInit(0.U(8.W))          // DefRegInit
  val mem = SyncReadMem(16, UInt(8.W)) // DefSeqMemory
  when(io.en) { sum := sum + 1.U }     // When + DefPrim + Connect
  io.data := mem(io.addr)              // DefMemPort + Connect
  printf(p"sum=%d\n", sum)             // Printf + Printable
}
```

**要求**：

1. 运行 `println(ChiselStage.emitCHIRRTL(new Demo))`（**待本地验证**，若无环境则静态分析）。
2. 在输出里至少找到并标注以下 6 类行的源头：
   - `regreset sum ...` → [Serializer.scala:232-237](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Serializer.scala#L232-L237)（`DefRegInit`）
   - `smem mem ...` → [Serializer.scala:241-247](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Serializer.scala#L241-L247)（`DefSeqMemory`）
   - `when io.en :` → [Serializer.scala:382-406](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Serializer.scala#L382-L406)（`When`）
   - `node _T = add(sum, UInt<1>(0h1))` → [Serializer.scala:209-212](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Serializer.scala#L209-L212)（`DefPrim`，**注意是 `DefPrim` 不是 `DefNode`**）
   - `read mport ...` / `connect io.data, ...` → [Serializer.scala:268-271](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Serializer.scala#L268-L273)（`DefMemPort` / `Connect`）
   - `printf(clock, UInt<1>(0h1), "sum=%d\n", sum)` → [Serializer.scala:300-313](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Serializer.scala#L300-L313)（`Printf`，经 `Printable.unpack`）
3. 写一段小结：`Serializer` 的分发是「按 IR 三层 + 命令单行/嵌套二分」组织的，遇到任何不认识的 CHIRRTL 行，你都能用这套方法逆推到源码。

完成这个任务后，你就具备了「读 CHIRRTL 文本如读源码」的能力——这是日后调试 elaboration 结果、给 CIRCT 报 bug 时最实用的技能。

---

## 6. 本讲小结

- `Serializer` 是 `private[chisel3] object` 单例，把内部 IR（`Circuit ⊃ Component ⊃ Command`）序列化成 CHIRRTL 文本；公共入口是返回惰性 `Iterable[String]` 的 `Serializer.lazily`，由 `ElaboratedCircuit.serialize` 经 `.mkString` 拼成字符串，这正是 `emitCHIRRTL` 的最终落点。
- 它的骨架是**三层分发**：`serialize(Circuit)` 写电路头与模块列表，`serialize(Component)` 写模块头与端口，`serialize(Block)`/`serializeCommand` 写命令体；表达式层 `serialize(Arg)` 写右值。
- 命令分两类：嵌套型（`When`/`LayerBlock`/`DefContract`/`Placeholder`）在 `serializeCommand` 里递归展开成多行，单行型全部 fall through 到 `serializeSimpleCommand`。
- **关键辨析**：CHIRRTL 文本里的 `node` 关键字对应内部 IR 的 `DefPrim`（原语运算），**不是** `DefNode`；`DefNode` 是下游 FIRRTL IR 的节点名。`Connect` → `connect`、`DefRegInit` → `regreset`、`DefSeqMemory` → `smem`，均可在 `serializeSimpleCommand` 找到对应分支。
- `Printable` 是 `printf` 等命令的**延迟格式化对象**：不在 elaboration 期求值，而在序列化期由 `Serializer.unpack`（经 `Printable.resolve` + `.unpack`）拆成 FIRRTL 格式串与参数；`PrintableHelper` 隐式类提供 `p"..."`/`cf"..."` 插值器。
- 延迟求值的根因是命名滞后：`printf` 被记录时信号名未定，必须等序列化时（名字已定）才解析 `Name`/`FullName`。

---

## 7. 下一步学习建议

- 顺着发射链路继续往下游：本讲产出的是 CHIRRTL 文本，下一站是 [u4-l5 Converter：IR 到 CHIRRTL 的转换](u4-l5-converter.md)（或直接进入 [u5 Stage 与 CIRCT 集成](u5-l1-stage-phase-pipeline.md)），看 CHIRRTL 文本/`ElaboratedCircuit` 是如何被交给 CIRCT（firtool）编译成 SystemVerilog 的。
- 想深入 `Printable` 的边界情况，可读测试 [src/test/scala/chiselTests/PrintableSpec.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala/chiselTests/PrintableSpec.scala)，里面覆盖了各种格式符与转义场景。
- 若对「源信息 `@[file line]` 如何被记录」感兴趣，可预习 [u7-l2 SourceInfo 宏与隐式源信息](u7-l2-sourceinfo-macro.md)，理解 `Serializer.serialize(info)` 里那个 `@[...]` 是怎么从用户代码行号传到这里的。
- 想了解序列化如何被「按文件落地」，可看 [src/main/scala/chisel3/stage/phases/Emitter.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/phases/Emitter.scala) 中 `CircuitSerializationAnnotation` 如何把 `ElaboratedCircuit` 写到 `.fir` 文件。
