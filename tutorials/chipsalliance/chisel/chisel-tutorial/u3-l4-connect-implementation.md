# 连线的内部实现：MonoConnect 与 BiConnect

## 1. 本讲目标

上一讲（u3-l3）我们站在用户视角，认识了 `Connectable` 与 `:<=` / `:>=` / `:<>=` / `:#=` 这套现代连线 DSL，并提到老接口 `:=` / `<>` 最终也会落到底层的两套算法。本讲就「钻进地板下面」，把这两套算法读明白。读完本讲你应当能够：

- 说出 `MonoConnect`（处理 `:=`，**单向**）与 `BiConnect`（处理 `<>`，**双向**）的职责差异与参数语义。
- 解释这两套算法如何对一个聚合类型（`Vec` / `Record`）做**逐字段递归**，并在失败时把出错路径「拼」进错误信息。
- 指出叶子级方向检查发生在哪个函数的哪几行（`checkConnect.checkConnection` 与 `BiConnect.elemConnect` 的四个 CASE 分支）。
- 理解「方向」「可读 / 可写」「Binding」这三类校验如何叠加，并会定位一条真实连线错误是哪段逻辑抛出的。

## 2. 前置知识

本讲默认你已经掌握以下概念（来自前置讲义）：

- **Direction（方向）**：来自 u3-l2。用户层 `SpecifiedDirection`（`Input`/`Output`/`Flip`/`Unspecified`）与绑定后结算出的 `ActualDirection`（`Input`/`Output`/`Bidirectional`/`Unspecified`）。
- **Binding（绑定）**：来自 u2-l1 / u4-l3。一个 `Data` 有 `binding` 字段，`None` 表示纯类型，`Some(...)` 表示已绑定的硬件值，如 `PortBinding`（端口）、`WireBinding`（线）、`RegBinding`（寄存器）、`OpBinding`（运算结果）、`LitBinding`（字面量）、`DontCareBinding`（不关心）等。
- **只登记不施工**：来自 u1-l4 / u4-l2。连线和其它硬件构造一样，本质是向 Builder 的命令队列 `pushCommand`（`Connect` / `DefInvalid`），本身不立刻生成硬件。
- **consumer / producer**：来自 u3-l3。连线算子左侧固定叫 consumer，右侧固定叫 producer。

还需补充一个本讲频繁使用的术语：

- **sink / source（汇 / 源）**：数据流动的「接收端 / 提供端」。`MonoConnect` 在调用前就已经分好谁是 sink、谁是 source，sink 必须可写、source 必须可读。注意它与 consumer/producer 不同：consumer/producer 是算子的左右位置，sink/source 是数据流方向。

一个直觉模型：把连线想象成「给一捆导线接线」。`MonoConnect` 是「我只把电流从一头灌进去，不许回流」（单向、有明确 sink/source）；`BiConnect` 是「这捆线里有的该往左、有的该往右，自己看着方向接」（双向、commutative）。两套算法都遵循同样的套路：**先按结构递归到叶子，到叶子再用方向规则判定能不能接、接成哪条 `Connect` 命令**。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [core/src/main/scala/chisel3/internal/MonoConnect.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/MonoConnect.scala) | 单向连线算法本体，含 `connect` 递归入口、`elemConnect`、`checkConnect` 方向校验、`canBeSink/canBeSource/traceFlow`、`canFirrtlConnectData`。 |
| [core/src/main/scala/chisel3/internal/BiConnect.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/BiConnect.scala) | 双向连线算法本体，含 `connect` 递归入口、`elemConnect`、`recordConnect`、`issueConnectL2R/issueConnectR2L`、`canFirrtlConnectData`。 |
| [core/src/main/scala/chisel3/internal/Binding.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Binding.scala) | 定义 `BindingDirection`（`Internal`/`Output`/`Input`）、各类 Binding、`ViewWriteability.reportIfReadOnly` 等可写性检查。 |
| [core/src/main/scala/chisel3/Data.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala) | 用户入口 `:=`/`<>`（L822/L839）转发到私有 `connect`/`bulkConnect`（L546/L567），后者捕获异常并包装错误信息。 |
| [core/src/main/scala/chisel3/package.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/package.scala) | 定义异常类 `MonoConnectException` / `BiConnectException`（L425-L426）。 |

## 4. 核心概念与源码讲解

### 4.1 MonoConnect：单向连线算法（`:=`）

#### 4.1.1 概念说明

`MonoConnect` 解决的问题是：**给定一个 sink 和一个 source，把 source 的值单向灌进 sink**。关键词是「单向（mono）」——算法不关心 source 那一侧的字段方向是 Input 还是 Output，它只关心 sink 这一侧能不能被写。

调用方在进入算法**之前**就已经把「谁是 sink、谁是 source」定死了。这一点直接写在文件顶部的 scaladoc 里：

> Note that this isn't commutative. There is an explicit source and sink already determined before this function is called.

合法 sink 的判据（也写在 scaladoc）：

- 是当前模块内的可写节点（`Reg` / `Wire`）；或
- 是当前模块的输出端口；或
- 是子模块的输入端口。

合法 source 的判据：

- 是当前模块内的可读节点（`Reg`/`Wire`/Op）；或
- 是字面量；或
- 是当前模块或子模块的端口。

#### 4.1.2 核心流程

`MonoConnect.connect` 是一个大 `match`，按 `(sink, source)` 的结构类型分流。整体流程：

```
connect(sink, source, context_mod):
  ┌─ Probe 类型？ → probeDefine（探针特殊处理，本讲略）
  ├─ (Element, Element)？ → elemConnect(...)            # 叶子：方向校验 + 发命令
  ├─ (Vec, Vec)？          → 长度必须相等；
  │                          若整体能 FIRRTL 直连 → pushCommand(Connect) 一条搞定
  │                          否则 for 每个元素 → connect(sink(idx), source(idx))  # 递归
  ├─ (Record, Record)？    → 同理，逐字段递归；source 可多字段，sink 缺字段才报错
  ├─ (_, DontCare)？       → 对 sink pushCommand(DefInvalid)   # 「不关心」=置无效
  ├─ (DontCare, _)？       → 抛 DontCareCantBeSink
  ├─ Analog 相关？         → 抛错（单向不许接 Analog）
  └─ 其它                  → 抛 MismatchedException（类型不匹配）
```

两个关键设计：

1. **整体直连 vs 逐字段爆破**：对 `Vec`/`Record`，算法先调 `canFirrtlConnectData` 判断「能不能用一条 FIRRTL `Connect`（`<=`）整体接」。能就发一条命令（高效）；不能（例如带双向字段的 `Decoupled`）就退化为逐字段递归。这正是 scaladoc 里说的「带双向信号的聚合必须改用 `BiConnect`」。
2. **错误路径拼接**：递归时用 `try/catch` 捕获子层抛出的 `MonoConnectException`，**重新抛出时在消息前面加上当前字段名或下标**（`s".$field$message"` 或 `s"($idx)$message"`）。这样最终错误信息会带一条类似 `io.bits.foo(2)...` 的路径，告诉你错在哪个叶子。

叶子级真正「校验方向 + 发命令」的工作分给两个对象：

- `elemConnect`（[MonoConnect.scala:419-431](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/MonoConnect.scala#L419-L431)）：reify 视图 → 调 `checkConnect(...)` 校验 → `issueConnect` 发命令。
- `checkConnect.checkConnection`（[MonoConnect.scala:508-605](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/MonoConnect.scala#L508-L605)）：纯校验，按「sink/source 各自落在哪个模块」分四个 CASE 判方向。

`issueConnect`（[MonoConnect.scala:408-415](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/MonoConnect.scala#L408-L415)）最终落地为命令：source 是 `DontCare` 就发 `DefInvalid`（把 sink 置为无效），否则发 `Connect(sink.lref, source.ref)`。

#### 4.1.3 源码精读

**入口：`MonoConnect.connect`** 的元素与聚合分支（节选）：

[MonoConnect.scala:115-185](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/MonoConnect.scala#L115-L185) —— `connect` 主体，先做 Probe 拦截，再分流到 Element / Vec 各分支。注意 Vec 分支里先尝试 `canFirrtlConnectData`，失败再 `for (idx <- ...)` 逐元素递归，并用 `s"($idx)$message"` 拼路径：

```scala
for (idx <- 0 until sink_v.length) {
  try {
    connect(sourceInfo, sink_v(idx), source_v(idx), context_mod)
  } catch {
    case MonoConnectException(message) => throw MonoConnectException(s"($idx)$message")
  }
}
```

[MonoConnect.scala:197-224](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/MonoConnect.scala#L197-L224) —— Record 分支。注意它遍历的是 **sink** 的字段，然后去 source 里查同名字段：查到就递归，查不到就抛 `MissingFieldException`。这解释了「source 允许多字段、sink 缺字段才报错」的非对称语义。

**叶子方向校验：`checkConnect.checkConnection`** 是本讲的重点之一。它先把 sink/source 各自定位到所属模块，再用 `BindingDirection.from` 算出各自方向，然后分四种空间关系判定：

[MonoConnect.scala:534-546](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/MonoConnect.scala#L534-L546) —— **CASE 1：sink 与 source 同属当前模块**。sink 必须可写，所以 sink 是 `Input`（输入端口只能从外部读入、不可在本模块内写）就抛 `UnwritableSinkException`：

```scala
if ((context_mod == sink_mod) && (context_mod == source_mod)) {
  ((sink_direction, source_direction): @unchecked) match {
    case (Output, _)   => ()          // 当前模块输出：可写，放行
    case (Internal, _) => ()          // Wire/Reg：可写，放行
    case (Input, _)    => throw UnwritableSinkException(sink, source)  // ← 经典报错点
  }
}
```

[MonoConnect.scala:579-590](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/MonoConnect.scala#L579-L590) —— **CASE 4：sink、source 分属两个子模块（context 是它们共同的父模块）**。此处 sink 若是 `Output`（子模块的输出只能向外流、不能被父模块驱动）就抛 `UnwritableSinkException`。这正是本讲综合实践里「两个 Output 端口用 `:=` 连接」会命中的分支。

**方向溯源：`traceFlow` / `canBeSink` / `canBeSource`** 用于判断「整体上这个 Data 在当前 context 能否当 sink/source」，被 `canFirrtlConnectData` 复用：

[MonoConnect.scala:362-382](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/MonoConnect.scala#L362-L382) —— 沿 `ChildBinding` 一路追到 `PortBinding`，根据「是不是别人的子模块端口」与「一路上被 Flip 翻过几次」给出布尔结论。

> 小贴士：`MonoConnect.connect` 抛出的是 `MonoConnectException`，但用户层 `Data.connect` 会把它再包一层（见 4.3）。

#### 4.1.4 代码实践

**实践目标**：亲手触发 `MonoConnect` 的方向错误，并在源码中定位是哪一行抛的。

**操作步骤**（这是「源码阅读型 + 待本地运行」实践）：

1. 新建一个最小 Scala 文件（示例代码，非项目原有文件）：

   ```scala
   // 示例代码：Demo.scala
   import chisel3._

   class Leaf extends Module {
     val io = IO(new Bundle { val out = Output(UInt(8.W)) })
     io.out := 0.U
   }

   class Top extends Module {
     val io = IO(new Bundle { val a = Output(UInt(8.W)) })
     val x = Module(new Leaf)
     val y = Module(new Leaf)
     x.io.out := y.io.out   // ★ 两个 Output 端口用 := 连接，将触发错误
     io.a := x.io.out
   }

   object Demo extends App {
     println((new chisel3.stage.ChiselStage).emitSystemVerilog(new Top))
   }
   ```

2. 用上一讲的方式运行 elaboration（如 sbt/mill 跑 `Demo`，或在测试里调用 `emitSystemVerilog`）。

**需要观察的现象**：

- 编译期（elaboration 时）报错，提示形如 `x.io.out ... cannot be written from module Top`（具体措辞以本地输出为准，**待本地验证**）。错误会被包成 `Connection between sink (...) and source (...) failed @: ...`。
- 注意：如果把两个 Output 改成「同一模块内的两个输出」（例如 `class Top` 里 `io.a := io.b`，`a`、`b` 都是 `Top` 自己的 `Output`），则**不会报错**——因为 CASE 1 里 `(Output, _) => ()` 放行，`a` 被 `b` 驱动是合法的。这正是为什么要用「分属两个子模块的 Output」才能稳定触发 CASE 4。

**预期结果**：

- 错误源于 [MonoConnect.scala:586](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/MonoConnect.scala#L586) 的 `case (Output, _) => throw UnwritableSinkException(sink, source)`。
- 因为 sink `x.io.out` 的 `sink_parent_opt == context_mod`（`x` 是 `Top` 的子模块）且 source `y.io.out` 同理，命中 CASE 4。

#### 4.1.5 小练习与答案

**练习 1**：把上面 `Top` 里的 `x.io.out := y.io.out` 改成 `y.io.out := x.io.out`，错误信息里的 sink 会变成谁？为什么？

> **答案**：sink 变成 `y.io.out`。`:=` 的左侧恒为 sink，所以对调左右两侧只改变谁是 sink，仍然命中 CASE 4 的 `(Output, _)`，只是报错里的「无法被写的端口」从 `x.io.out` 换成 `y.io.out`。

**练习 2**：`MonoConnect.connect` 处理 `Record` 时，遍历的是 sink 的字段还是 source 的字段？这意味着哪一侧「可以多带字段」？

> **答案**：遍历 sink 的字段（[L214](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/MonoConnect.scala#L214)）。因此 **source 允许有 sink 没有的额外字段**（多出来的被忽略），但 sink 缺字段会抛 `MissingFieldException`。

### 4.2 BiConnect：双向连线算法（`<>`）

#### 4.2.1 概念说明

`BiConnect` 解决的问题是：**两侧都可能既有输入又有输出（典型如 `DecoupledIO` 的 `valid/bits` 是输出、`ready` 是输入），需要按字段方向各自接对**。它的参数语义与 `MonoConnect` 不同——这里叫 `left` 和 `right`，且**操作是 commutative（可交换）的**，文件顶部 scaladoc 明确写道：

> Note that the arguments are left and right (not source and sink) so the intent is for the operation to be commutative.

也就是说 `a <> b` 与 `b <> a` 等价。算法会递归地对每一对叶子用 `elemConnect` 判断「到底哪边驱动哪边」。

#### 4.2.2 核心流程

```
BiConnect.connect(left, right, context_mod):
  ┌─ Probe 类型？ → 抛错（探针不能参与 <>）
  ├─ (Analog, Analog)？ → 走 attach（模拟量特殊路径）
  ├─ (Element, Element)？ → elemConnect(...)
  ├─ (Vec, Vec)？          → 长度必须相等；能整体 FIRRTL 直连就发一条 Connect，
  │                          否则逐元素递归 connect(left(idx), right(idx))
  ├─ (Record, Record)？    → 先判定要不要「翻转」左右（flipConnection），
  │                          能直连就发一条 Connect，否则 recordConnect 逐字段递归
  └─ 其它 → 抛 MismatchedException
```

两个与 `MonoConnect` 不同的关键点：

1. **Record 分支先判翻转方向**（[BiConnect.scala:176-199](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/BiConnect.scala#L176-L199)）：因为 `<>` commutative 但 FIRRTL 的 `<=` 不 commutative，所以算法用 `canBeSink/canBeSource` 决定到底「left 接 right」还是「right 接 left」。
2. **叶子级有两组发命令函数**：`issueConnectL2R`（left 当源、right 当汇）与 `issueConnectR2L`（right 当源、left 当汇），由 `elemConnect` 根据方向组合选用。

BiConnect 的方向校验同样分成「同模块 / 一方在子模块 / 双方都在子模块」等空间关系，但它**关心两侧方向**，所以异常类型更细：`BothDriversException`（两边都想驱动）、`NeitherDriverException`（两边都不驱动）、`UnknownDriverException`（双方都是内部节点、无法判断）。

#### 4.2.3 源码精读

**入口与 Record 翻转判定**：

[BiConnect.scala:176-199](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/BiConnect.scala#L176-L199) —— 对两个 `Record`，先用 `MonoConnect.canBeSink/canBeSource` 决定是否交换左右（`flipConnection`），再决定整体直连还是走 `recordConnect`：

```scala
val flipConnection =
  !MonoConnect.canBeSink(left_r, context_mod) || !MonoConnect.canBeSource(right_r, context_mod)
val (newLeft, newRight) = if (flipConnection) (right_r, left_r) else (left_r, right_r)
```

> 注意：`BiConnect` 在这里**复用了 `MonoConnect` 的 `canBeSink/canSource` 与 `canFirrtlConnectData`**——这是两套算法之间的重要耦合点，说明「双向」在底层仍建立在「单向方向溯源」之上。

**叶子方向校验：`BiConnect.elemConnect`**（四个 CASE）：

[BiConnect.scala:442-457](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/BiConnect.scala#L442-L457) —— **CASE 3：left 与 right 同属当前模块**。与 MonoConnect 的 CASE 1 对比着看，这里对 `(Input, Input)` 和 `(Output, Output)` 都抛 `BothDriversException`（两个都只想驱动，没人当汇），而 `(Internal, Internal)` 抛 `UnknownDriverException`：

```scala
case (Input, Input)       => throw BothDriversException
case (Output, Output)     => throw BothDriversException
case (Internal, Internal) => throw UnknownDriverException
// 其余组合按方向选 issueConnectL2R / issueConnectR2L
```

**两条发命令路径**：

[BiConnect.scala:330-382](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/BiConnect.scala#L330-L382) —— `issueConnectL2R` 与 `issueConnectR2L`。两者都先处理 `DontCareBinding`（发 `DefInvalid`），否则发 `Connect(sink.lref, source.ref)`。差别仅在「谁当 sink」：L2R 把 right 当 sink，R2L 把 left 当 sink。

**整体直连判据：`BiConnect.canFirrtlConnectData`**：

[BiConnect.scala:269-326](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/BiConnect.scala#L269-L326) —— 汇总了 8 项检查的与（`&&`）：类型检查 `CheckTypes.validConnect`、上下文检查（复用 `MonoConnect.dataConnectContextCheck`）、sink 非只读、双向可流、双方非字面量/非视图、非 BlackBox `io`、非 InstanceChoice。任一不满足就退化为逐字段递归。

#### 4.2.4 代码实践

**实践目标**：对比 `:=` 与 `<>` 对同一个带反向字段的 Bundle 的处理差异，直观体会「单向」与「双向」。

**操作步骤**（示例代码，待本地运行验证）：

```scala
// 示例代码
import chisel3._
import chisel3.util.DecoupledIO

class Dual extends Module {
  val io = IO(new Bundle {
    val prod = DecoupledIO(UInt(8.W))   // 含 valid(Output)/bits(Output)/ready(Input)
    val cons = DecoupledIO(UInt(8.W))
  })
  // ① 用 <> 双向接：valid/bits 从 prod 流向 cons，ready 从 cons 流向 prod
  io.cons <> io.prod
}
```

1. 把 `io.cons <> io.prod` 改成 `io.cons := io.prod`，分别生成 SystemVerilog。
2. 观察两次产出的端口/连线：`:=` 版本里 `cons.ready` 是否被驱动？

**需要观察的现象与预期结果**（**待本地验证**）：

- `<>` 版本：`prod.ready` 与 `cons.ready` 接上（反向字段也被接）。
- `:=` 版本：`MonoConnect` 只接 sink（`cons`）方向为 Output/Unspecified 的字段（`valid`、`bits`），`ready` 是 Input 不会被驱动，可能触发「未连接」相关告警/错误。这正是 scaladoc 强调「带双向信号的聚合必须用 BiConnect」的原因。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `BiConnect.connect` 的参数叫 left/right 而不是 sink/source？

> **答案**：因为 `<>` 是 commutative（可交换）的，事先不固定谁是汇、谁是源；到底哪边驱动哪边，是递归到叶子后由 `elemConnect` 按方向组合现场决定的（选 `issueConnectL2R` 或 `issueConnectR2L`）。

**练习 2**：`BiConnect.elemConnect` 在「同模块内两个 Output 相连」时抛什么异常？对应 [BiConnect.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/BiConnect.scala) 的哪一行？

> **答案**：抛 `BothDriversException`（两边都想当驱动者），对应 [L454](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/BiConnect.scala#L454) 的 `case (Output, Output) => throw BothDriversException`。

### 4.3 Binding 检查：方向、可读与可写校验

#### 4.3.1 概念说明

「连线」要正确，必须同时满足三类约束，它们分散在不同层次的代码里：

1. **方向（Direction）**：sink 是不是真能被写、source 是不是真能被读——由 `BindingDirection` + 空间关系（CASE 1~4）判定，4.1/4.2 已讲。
2. **可写性（Writable / ReadOnly）**：这个 `Data` 是不是只读的（如运算结果 `OpBinding`、字面量 `LitBinding`、`ClassBinding`）。只读的不能当 sink。
3. **作用域（Scope）**：信号有没有「逃逸」出它被声明的 `when` 块作用域——由 `checkBlockVisibility` 判定。

本模块把这三类检查的「入口」与「触发点」串起来，帮助你在报错时快速定位。

#### 4.3.2 核心流程

```
用户写 a := b
   └─ Data.:=(L822) → Data.connect(L546)
        ├─ requireIsHardware(a/b)              # 必须是已绑定硬件值
        ├─ a.topBinding 是否 ReadOnlyBinding？  # 是 → 直接抛「不能给只读赋值」
        └─ try MonoConnect.connect(...) 
             catch MonoConnectException → throwException 包装

用户写 a <> b
   └─ Data.<>(L839) → Data.bulkConnect(L567)
        ├─ requireIsHardware(a/b)
        ├─ 双方都 ReadOnly？/ 左侧 DontCare？   # 预检
        └─ try BiConnect.connect(...)
             catch BiConnectException → throwException 包装

叶子内部：
   elemConnect → checkConnect.checkConnection (方向 CASE 1~4)
               + checkBlockVisibility (作用域)
   发命令前 → ViewWriteability.reportIfReadOnlyUnit (视图可写性)
   整体直连前 → canFirrtlConnectData (含 ReadOnlyBinding 检查)
```

注意错误处理方式的差异：`MonoConnect`/`BiConnect` 内部用**抛异常**（`MonoConnectException`/`BiConnectException`）来中止并携带路径；而用户层 `Data.connect`/`bulkConnect` 用 `try/catch` 把异常转成 `throwException`（终止 elaboration）。现代 DSL `Connection.connect`（u3-l3）则把异常转成 `Builder.error`（**收集而非立即终止**，可一次报多条）。

#### 4.3.3 源码精读

**方向枚举 `BindingDirection`**：

[Binding.scala:15-45](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Binding.scala#L15-L45) —— 只有三个值：`Internal`（Wire/Reg/Op 等内部节点）、`Output`、`Input`。`from(binding, direction)` 的规则很简洁：`PortBinding`/`SecretPortBinding` 按 `ActualDirection` 映射成 `Output`/`Input`，其余（包括 `DynamicIndexBinding` 递归到底层 Vec）一律算 `Internal`。这个三值枚举正是 4.1/4.2 各 CASE 里模式匹配的对象。

**用户层预检与异常包装**：

[Data.scala:546-566](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L546-L566) —— `Data.connect`（`:=` 的落地处）。先 `requireIsHardware`，再检查 sink 是否 `ReadOnlyBinding`（是就直接抛「Cannot reassign to read-only」），然后 `try MonoConnect.connect` 并把 `MonoConnectException` 包装成带 sink/source 描述的错误：

```scala
try {
  MonoConnect.connect(sourceInfo, this, that, Builder.referenceUserContainer)
} catch {
  case MonoConnectException(message) =>
    throwException(s"Connection between sink ($this) and source ($that) failed @: $message")
}
```

[Data.scala:567-588](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L567-L588) —— `Data.bulkConnect`（`<>` 的落地处），结构对称，区别是预检里多了「左侧是 `DontCare` 抛 `DontCareCantBeSink`」，并调 `BiConnect.connect`。

**异常类定义**：

[package.scala:425-426](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/package.scala#L425-L426) —— 两个异常都是 `ChiselException` 的子类，构造时只吃一个 `message` 字符串（这就是为什么递归层可以反复「在 message 前面拼字段名」）。

**作用域检查 `checkBlockVisibility`**：

[MonoConnect.scala:90-102](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/MonoConnect.scala#L90-L102) —— 如果一个 `BlockBinding`（如 `when` 块里声明的 `Wire`/`Reg`）的 `parentBlock` 不在当前 `Builder.blockStack` 里，就认为它「逃逸」了，返回 `Some(sourceInfo)` 供上层 `Builder.error` 报错。它在 `checkConnection` 末尾被调用（[L596-L604](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/MonoConnect.scala#L596-L604)）。

**视图可写性 `ViewWriteability.reportIfReadOnly`**：

[Binding.scala:143-158](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Binding.scala#L143-L158) —— DataView（u8-l2）可以把目标标记为只读。连线时 `elemConnect`/`propConnect` 在发命令前调 `reportIfReadOnlyUnit`：若是只读视图就 `Builder.error` 并**跳过 pushCommand**（避免产出非法命令），否则正常发命令。这是「错误收集」式处理的一个实例。

#### 4.3.4 代码实践

**实践目标**：触发 `ReadOnlyBinding` 预检，体会它与「方向错误」是两类不同的报错。

**操作步骤**（示例代码，待本地运行验证）：

```scala
// 示例代码
import chisel3._

class ReadOnlySink extends Module {
  val io = IO(new Bundle { val out = Output(UInt(8.W)) })
  val a = io.out + 1.U      // a 是 OpBinding，只读
  a := 2.U                  // ★ 给只读节点当 sink 赋值
}
```

1. 运行 elaboration，观察报错。
2. 把 `a := 2.U` 注释掉，再观察是否还有错。

**需要观察的现象与预期结果**（**待本地验证**）：

- 报错来自 [Data.scala:554](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L554) 的 `case _: ReadOnlyBinding => throwException(s"Cannot reassign to read-only $this")`——注意它**根本没进 `MonoConnect`**，而是用户层预检就拦下了。这与 4.1 的方向错误（进入 `MonoConnect` 后才在 `checkConnection` 抛）属于不同层次，定位时要先分清。

#### 4.3.5 小练习与答案

**练习 1**：一个 `Wire`、一个模块 `Output` 端口、一个运算结果 `a + b`，它们的 `BindingDirection` 分别是什么？哪个不能当 `:=` 的 sink？

> **答案**：`Wire` → `Internal`；`Output` 端口 → `Output`；运算结果 → `Internal`（但其 `topBinding` 是 `ReadOnlyBinding`）。三者里**运算结果不能当 sink**——不是因为方向（它也是 Internal，方向上 CASE 1 会放行），而是因为它在用户层预检就被 `ReadOnlyBinding` 拦下（[Data.scala:554](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L554)）。

**练习 2**：`MonoConnect`/`BiConnect` 内部用「抛异常」，而现代 `Connection.connect`（u3-l3）把异常转成 `Builder.error`。后者的好处是什么？

> **答案**：`Builder.error` 是**收集式**的——一次 elaboration 可以累积多条错误一起报给用户，而不是碰到第一个错误就立刻中止。这对连线密集的设计更友好（用户一次看到所有接错的地方）。

## 5. 综合实践

把本讲三个最小模块串成一个排查任务。给定下面这段「接错线」的代码（示例代码，待本地运行验证）：

```scala
// 示例代码
import chisel3._

class Leaf extends Module {
  val io = IO(new Bundle {
    val din = Input(UInt(8.W))
    val dout = Output(UInt(8.W))
  })
  io.dout := io.din
}

class Buggy extends Module {
  val io = IO(new Bundle { val x = Output(UInt(8.W)) })
  val m = Module(new Leaf)
  val tmp = Wire(UInt(8.W))
  tmp := m.io.din          // 错误①：把子模块的 Input 当 source 读
  m.io.dout := io.x        // 错误②：把子模块的 Output 当 sink 写
  io.x := m.io.dout
}
```

请完成：

1. 先**预测**每条 `:=` 分别会命中 `checkConnect.checkConnection` 的哪个 CASE、抛哪个异常（参考 4.1.3 的四个 CASE）。
2. 再运行 elaboration，对照实际错误信息，逐条标注：
   - 错误来自 [MonoConnect.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/MonoConnect.scala) 的哪一行；
   - 是「方向」错误还是「可写性 / ReadOnlyBinding」错误（区分 4.1 与 4.3）。
3. 修正这两处连线（提示：子模块的 Input 应由父模块驱动，Output 应被父模块读取），使 elaboration 通过。

> 参考判断：错误② `m.io.dout := io.x` 中 sink `m.io.dout` 是子模块 Output、source `io.x` 是当前模块 Output，命中 CASE 2（[L549-L562](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/MonoConnect.scala#L549-L562)）的 `(Output, Output) => ()`……注意它**可能不报方向错**而是别的约束——这正是本题要你「先预测再验证」的训练点，实际结果以本地运行为准。

## 6. 本讲小结

- `MonoConnect`（`:=`）是**单向**算法：调用前已定好 sink/source，sink 必须可写；对 `Vec`/`Record` 先试整体 FIRRTL 直连、否则逐字段递归，错误信息靠 `try/catch` 逐层拼接字段名/下标路径。
- `BiConnect`（`<>`）是**双向**算法：参数叫 left/right、commutative，对 Record 先判定是否翻转左右，叶子级用 `issueConnectL2R`/`issueConnectR2L` 按方向组合选边；双向异常更细（`BothDrivers`/`NeitherDriver`/`UnknownDriver`）。
- 叶子方向校验在 `checkConnect.checkConnection`（Mono）与 `BiConnect.elemConnect`（Bi）里，都按「sink/source 与 context_mod 的空间关系」分四个 CASE；`BindingDirection` 只有 `Internal`/`Output`/`Input` 三值。
- 两套算法高度耦合：`BiConnect` 复用了 `MonoConnect.canBeSink/canBeSource/traceFlow/dataConnectContextCheck`，说明「双向」建立在「单向方向溯源」之上。
- 错误处理分两层：算法内部抛 `MonoConnectException`/`BiConnectException`（携带路径），用户层 `Data.connect`/`bulkConnect` 把它包装成 `throwException`；现代 DSL 则转成 `Builder.error` 收集多条。
- 用户层还有一道预检：`ReadOnlyBinding` 直接拦截「给只读节点赋值」，根本不进算法——定位报错时要先区分这一层与方向错误层。

## 7. 下一步学习建议

- **横向对比**：回到 u3-l3 的 [Connection.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Connection.scala)，把 `:<=`/`:>=`/`:<>=`/`:#=` 的 8 个布尔标志与本讲的 `MonoConnect`/`BiConnect` 对应起来——你会看到现代 DSL 最终在叶子层调用的仍是 `l := r`，即回到本讲的算法。
- **纵向下沉**：接下来建议进入单元四。`MonoConnect`/`BiConnect` 最终 `pushCommand(Connect)`/`DefInvalid`，这些命令长什么样、如何被 Builder 收拢成 IR，正是 [u4-l2 命令记录与内部 FIRRTL IR](u4-l2-internal-firrtl-ir.md) 的主题。
- **补全细节**：本讲略过了 Probe（`probeDefine`）、Analog（`attach`）、Property（`propConnect`）三类特殊连线，它们分别对应 [u8-l3 Layers 与 Probe](u8-l3-layers-and-probe.md)、Analog 文档与 [u8-4 Properties](u8-4-properties-object-model.md)，可在学完核心 IR 后回头精读 [MonoConnect.scala:459-484](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/MonoConnect.scala#L459-L484) 等段落。
