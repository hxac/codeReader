# IO、方向与 Flipped 翻转

## 1. 本讲目标

本讲承接 [u3-l1](u3-l1-module-rawmodule.md)（模块生命周期）与 [u2-l3](u2-l3-bundle.md)（Bundle 聚合类型），专门回答一个问题：**一个 `Data` 信号，到底是输入还是输出？这个「方向」从哪里来、又是如何被 Chisel 内部结算出来的？**

读完本讲，你应当能够：

- 说清 `IO(...)` 这一层包装到底做了什么（不只是「声明端口」那么简单）。
- 区分两套方向模型：用户声明的 `SpecifiedDirection` 与绑定后结算出的 `ActualDirection`。
- 理解 `Input`/`Output` 与 `Flipped` 的本质差别：前者「强制」、后者「相对翻转」。
- 看懂 `fromParent` / `fromSpecified` 这两个核心函数如何把一棵类型树的方向结算清楚。
- 用 `Flipped` 设计「相对方向」的子 Bundle，并能预测生成 Verilog 里每个端口的 `input`/`output`。

## 2. 前置知识

在进入源码前，先建立两个直觉。

**直觉一：方向是「贴在类型上的标签」，不是单独的信号。**
在 Chisel 里你不会单独写「这是一个 input 端口」。你先有一个 `Data` 类型（比如 `UInt(8.W)` 或某个 `Bundle`），再用 `Input(...)`/`Output(...)`/`Flipped(...)` 给它「贴一张方向标签」。标签贴在节点上，并随类型树向下传播。

**直觉二：「相对方向」与「绝对方向」是两回事。**
考虑一个握手接口 `valid`（模块驱动）/ `ready`（对端驱动）。从「生产者」模块看，`valid` 是输出、`ready` 是输入；但从「消费者」模块看刚好相反。如果每次实例化都要重新标一遍方向会很痛苦。所以 Chisel 允许你用**相对方向**定义一次 Bundle，再用 `Flipped` 整体翻转复用——这正是 `DecoupledIO` 等标准接口的设计基础（见 [u6-l1](u6-l1-decoupled-readyvalid.md)）。

为了支撑这两种直觉，Chisel 内部维护了两层方向表示：

| 层 | 名称 | 含义 | 何时确定 |
|---|---|---|---|
| 用户层 | `SpecifiedDirection` | 用户用 `Input/Output/Flipped` 写下的「意图标签」，**局部于单个节点** | 写代码时 |
| 结算层 | `ActualDirection` | 综合了父节点、子节点后，该节点**最终**的方向 | `bind`（绑定）之后 |

一个通俗类比：`SpecifiedDirection` 像你给每个零件贴的「我希望它朝哪」的便签；`ActualDirection` 像质检员把整台机器装完后，量出来的「它实际朝哪」。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [core/src/main/scala/chisel3/IO.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/IO.scala) | `IO` / `FlatIO` 工厂：把一个 Chisel 类型登记为模块端口 |
| [core/src/main/scala/chisel3/Data.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala) | `SpecifiedDirection`、`ActualDirection` 两套方向枚举，以及 `Input`/`Output`/`Flipped` 三个操作函数 |
| [core/src/main/scala/chisel3/Element.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Element.scala) | 叶子节点的 `bind`：用 `fromParent`+`fromSpecified` 结算方向 |
| [core/src/main/scala/chisel3/Aggregate.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala) | `Vec`/`Record`（Bundle 父类）的 `bind`：递归结算子节点方向 |
| [core/src/main/scala/chisel3/Module.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala) | `bindIoInPlace` 与 `assignCompatDir`：`IO(...)` 落地时调用的内部入口 |
| [core/src/main/scala/chisel3/connectable/Alignment.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Alignment.scala) | 把结算后的方向转译成连线算法可用的「对齐/翻转」视图（桥接下一讲 `:=`/`<>`） |

---

## 4. 核心概念与源码讲解

### 4.1 IO：把一个 Chisel 类型登记为端口

#### 4.1.1 概念说明

回顾 [u3-l1](u3-l1-module-rawmodule.md) 的结论：模块构造体里的代码「只登记不施工」。`IO(...)` 就是这层登记的入口——它接收一个 Chisel 类型，把它**登记为当前模块的端口**，并触发绑定（`bind`），让方向、绑定关系等信息被真正写入。

注意三点反直觉之处：

1. `IO(...)` 接收的必须是**纯类型**（未绑定的 Chisel type），不能是已经接线的硬件值——内部会用 `requireIsChiselType` 把关。
2. `IO(...)` 会**克隆**传入的类型，以保持类型对象的不可变性（你给 `IO` 的那个对象本身不会被改脏）。
3. `IO(...)` 不能在模块关闭后再调用（`require(!module.isClosed, ...)`）。

#### 4.1.2 核心流程

`IO.apply` 的执行可以拆成五步：

```
1. 取当前模块  Module.currentModule.get
2. 合法性检查（IO 创建是否被允许、模块是否已关闭）
3. 按名求值 iodef，并 requireIsChiselType 把关
4. 必要时 cloneTypeFull（克隆类型，保护不可变）
5. module.bindIoInPlace(clone) —— 真正落地：分配兼容方向 + bind(PortBinding) + 登记进 _ports
```

第 5 步是关键，它把一个「裸类型」变成了「带 `PortBinding`、带 `ActualDirection` 的端口」。

#### 4.1.3 源码精读

`IO.apply` 的完整入口在 [core/src/main/scala/chisel3/IO.scala:25-64](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/IO.scala#L25-L64)，关键片段：

```scala
def apply[T <: Data](iodef: => T)(implicit sourceInfo: SourceInfo): T = {
  val module = Module.currentModule.get
  ...
  require(!module.isClosed, "Can't add more ports after module close")
  val prevId = Builder.idGen.value
  val data = iodef                       // 按名求值，只求一次
  requireIsChiselType(data, "io type")   // 必须是纯类型
  ...
  // 必要时克隆，保持类型不可变
  val iodefClone = if (!data.mustClone(prevId)) data else data.cloneTypeFull
  module.bindIoInPlace(iodefClone)       // 落地：绑定 + 登记端口
  iodefClone
}
```

注意 `iodef: => T` 是**按名传参**——这意味着 `IO(new Bundle { ... })` 里那个 `new Bundle{}` 只有在函数体内 `val data = iodef` 这一行才被求值一次。`mustClone(prevId)` 用 `prevId`（调用前的 id 计数器）判断这个对象是不是「刚刚新建的」；如果是全新对象就不必再克隆，省一次开销。

真正的落地逻辑在 `bindIoInPlace`，它转调 `_bindIoInPlace`，见 [core/src/main/scala/chisel3/Module.scala:903-910](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L903-L910)：

```scala
protected def _bindIoInPlace(iodef: Data)(implicit sourceInfo: SourceInfo): Unit = {
  // 兼容层：把 Unspecified/Flipped 的叶子显式标成 Output/Input
  Module.assignCompatDir(iodef)
  iodef.bind(PortBinding(this))          // 用端口绑定触发整棵树的方向结算
  _ports += iodef -> sourceInfo          // 登记进模块的端口表
}
```

这三行就是 `IO(...)` 的全部「施工」：先跑兼容层、再 `bind`、最后登记。`PortBinding(this)` 表示「这是属于当前模块的端口绑定」，它会触发 [4.3](#43-actualdirection绑定后结算出的真实方向) 要讲的整棵树方向结算。

> **补充：FlatIO。** 同文件里还有一个 `FlatIO`（[core/src/main/scala/chisel3/IO.scala:83-118](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/IO.scala#L83-L118)），它把一个 `Record` 的每个字段都「摊平」成独立的顶层端口（不带 `io_` 前缀）。它内部仍然逐字段调用 `IO(...)`，并用 `coerceDirection`（[core/src/main/scala/chisel3/IO.scala:87-95](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/IO.scala#L87-L95)）把 `Flip` 映射成 `Flipped(...)`、`Input`/`Output` 原样保留。本讲聚焦 `IO`，`FlatIO` 了解即可。

#### 4.1.4 代码实践

**实践目标**：亲手验证「`IO(...)` 接收类型、克隆、登记」这三步，并观察一次「双重 `IO`」会触发什么报错。

**操作步骤**：

1. 写一个最小模块，在 `IO(...)` 里直接 `println` 一个标记，观察求值时机。
2. 故意把一个已绑定的硬件值传给 `IO(...)`，观察 `requireIsChiselType` 报错。

```scala
// 示例代码（非项目原有）
import chisel3._
import chisel3.stage.ChiselStage

class ProbeIO extends Module {
  // 步骤 1：观察这行何时被打印
  val io = IO(new Bundle {
    println(">>> Bundle 构造体正在被执行（elaboration 期）")
    val a = Input(UInt(4.W))
    val b = Output(UInt(4.W))
  })
  io.b := io.a + 1.U
}

// 步骤 2（请在 REPL 或单独 main 中试）：
//   val w = Wire(UInt(4.W))   // w 已是硬件值
//   IO(w)                      // 期望：requireIsChiselType 报错
```

**需要观察的现象**：

- 步骤 1：`>>> Bundle 构造体...` 会在 `emitSystemVerilog` 触发 elaboration 时打印一次——印证「构造体在 elaboration 期才执行」。
- 步骤 2：抛出类似「io type must be a Chisel type, not a hardware value」的错误。

**预期结果**：步骤 1 能正常生成 Verilog；步骤 2 报错中止。**待本地验证**具体报错文案随版本略有差异。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `IO(...)` 要在传入类型前先 `requireIsChiselType`？

**参考答案**：端口声明需要的是一个「类型模板」，`IO` 内部还要克隆它、再绑定成端口硬件值。如果传入的已经是绑定过的硬件值（比如一个 `Wire`），再次绑定会触发 `RebindingException`，且语义上「把一根已存在的线当成端口类型」本身就是错的。

**练习 2**：`_bindIoInPlace` 里 `assignCompatDir` 和 `bind` 能不能调换顺序？

**参考答案**：不能。`assignCompatDir`（见 [4.4](#44-flipped--input--output三个方向操作函数)）负责在绑定前把 `Unspecified`/`Flipped` 的叶子显式改成 `Output`/`Input`，这是 Chisel2 兼容约定。若先 `bind`，方向结算会基于未修正的 `specifiedDirection` 完成，之后再改字段就为时已晚（`direction` 已被 `direction_=` 锁定，重复赋值会抛 `RebindingException`）。

---

### 4.2 SpecifiedDirection：用户声明的「方向标签」

#### 4.2.1 概念说明

`SpecifiedDirection` 是**用户层的方向标签**，记录「你在某个节点上写过 `Input`/`Output`/`Flipped` 没」。它是 `sealed abstract class`，只有四个取值，定义在 [core/src/main/scala/chisel3/Data.scala:24-80](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L24-L80)：

| 取值 | 值 | 含义 | 类别 |
|---|---|---|---|
| `Unspecified` | 0 | 默认，未标方向（等价于「不翻转」） | 相对 |
| `Output` | 1 | 强制为输出 | 强制 |
| `Input` | 2 | 强制为输入 | 强制 |
| `Flip` | 3 | 相对父方向翻转一次 | 相对 |

关键分类：`Output`/`Input` 是**强制类**（coercing），会盖住所有子节点的方向；`Unspecified`/`Flip` 是**相对类**，方向取决于父节点。这个二分法是理解整个方向系统的钥匙。

#### 4.2.2 核心流程

每个 `Data` 节点内部用一个 `Byte` 字段存这个标签（见 [4.3](#43-actualdirection绑定后结算出的真实方向) 的字段定义）。结算方向时，核心算法是 `SpecifiedDirection.fromParent(parentDirection, thisDirection)`，它回答：「已知父节点结算后的方向、以及本节点用户写的方向，本节点的有效方向是什么？」见 [core/src/main/scala/chisel3/Data.scala:61-67](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L61-L67)：

```scala
def fromParent(parentDirection: SpecifiedDirection, thisDirection: SpecifiedDirection): SpecifiedDirection =
  (parentDirection, thisDirection) match {
    case (Output, _)            => Output      // 父强制输出 → 子必然输出
    case (Input,  _)            => Input       // 父强制输入 → 子必然输入
    case (Unspecified, thisDir) => thisDir     // 父未定 → 用子自己的方向
    case (Flip, thisDir)        => flip(thisDir) // 父翻转 → 把子方向翻转
  }
```

用伪代码描述这棵树的结算：

```
resolve(node, parentDir):
  myDir = node.specifiedDirection          # 用户贴的标签
  effective = fromParent(parentDir, myDir) # 结算本节点
  for child in node.children:
    resolve(child, effective)              # 把本节点有效方向作为子的父方向
```

而 `flip` 本身是一张固定的翻转表（[core/src/main/scala/chisel3/Data.scala:51-56](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L51-L56)）：`Unspecified ↔ Flip`、`Output ↔ Input`。连续翻转两次回到自身，这是 `Flipped(Flipped(x))` 与 `x` 同向的原因。

#### 4.2.3 源码精读

四个标签的定义（[core/src/main/scala/chisel3/Data.scala:29-41](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L29-L41)）：

```scala
case object Unspecified extends SpecifiedDirection(0)   // 默认，未翻转
case object Output      extends SpecifiedDirection(1)   // 强制输出
case object Input       extends SpecifiedDirection(2)   // 强制输入
case object Flip        extends SpecifiedDirection(3)   // 容器：子节点翻转
```

注意每个 `case object` 都带一个 `Byte` 值，配合 `fromByte`（[core/src/main/scala/chisel3/Data.scala:43-49](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L43-L49)）。这样做是因为 `Data` 内部为了内存效率把方向存成原始 `Byte` 而非对象引用（详见 [4.3.3](#433-源码精读-2)），读写时再用 `fromByte`/`.value` 互转。

#### 4.2.4 代码实践

**实践目标**：用纸笔（或 REPL）走一遍 `fromParent`，预测一段接口的方向，再用生成的 Verilog 验证。

**操作步骤**：考虑下面这个 Bundle 的字段（暂不放进 `IO`，只看标签）：

```scala
// 示例代码
class Iface extends Bundle {
  val a = UInt(4.W)              // Unspecified
  val b = Output(UInt(4.W))      // Output
  val c = Flipped(UInt(4.W))     // Flip
}
```

1. 假设整个 `Iface` 被包成 `Input(new Iface)`（顶层 `Input` 强制）。
2. 逐字段套用 `fromParent`：父方向先是 `Input`。
3. 再假设被包成 `Flipped(new Iface)`（顶层 `Flip`），重新算一遍。

**需要观察的现象 / 预期结果**：

- 包成 `Input(...)`：父=`Input` → `fromParent(Input, *)` 恒为 `Input`，故 `a`/`b`/`c` 最终都是输入。印证「`Input` 强制盖住一切」。
- 包成 `Flipped(...)`：顶层有效方向 = `fromParent(Unspecified, Flip)` = `Flip`；再到字段：
  - `a`：`fromParent(Flip, Unspecified)` = `flip(Unspecified)` = `Flip`
  - `b`：`fromParent(Flip, Output)` = `flip(Output)` = `Input`
  - `c`：`fromParent(Flip, Flip)` = `flip(Flip)` = `Unspecified`

最终这些 `SpecifiedDirection` 还要经 `fromSpecified` 转成 `ActualDirection`（见 [4.3](#43-actualdirection绑定后结算出的真实方向)）。**待本地验证**：把两种写法分别生成 Verilog，核对 `a`/`b`/`c` 的 `input`/`output`。

#### 4.2.5 小练习与答案

**练习 1**：`fromParent(Output, Flip)` 等于什么？为什么？

**参考答案**：等于 `Output`。因为第一个分支 `case (Output, _) => Output` 直接命中——父节点一旦强制 `Output`，子节点写什么都无所谓，整棵子树都被钉成输出。这正是「强制类盖住相对类」的体现。

**练习 2**：为什么 `Flip` 的注释写「Mainly for containers」（主要用于容器）？

**参考答案**：`Flip` 的语义是「把子树方向翻转」，它本身不指明「输入还是输出」，只有作用在容器（`Bundle`/`Vec`）上、再由子节点各自的标签一起结算时才有意义。对一个叶子直接写 `Flipped(UInt(4.W))` 当然也可以（等价于输入），但 `Flip` 的设计初衷是描述「整片子树相对翻转」，最典型的就是复用同一份握手接口定义。

---

### 4.3 ActualDirection：绑定后结算出的「真实方向」

#### 4.3.1 概念说明

`ActualDirection` 是**结算层**：当一个 `Data` 被 `bind`（绑定到硬件图）之后，它最终的方向。它定义在 [core/src/main/scala/chisel3/Data.scala:86-193](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L86-L193)，取值比 `SpecifiedDirection` 多，因为要表达「混合方向」的聚合体：

| 取值 | 含义 |
|---|---|
| `Empty` | 节点为空，无方向 |
| `Unspecified` | 无方向的 struct（既非纯入也非纯出） |
| `Output` | 输出叶子，或「全输出」的容器 |
| `Input` | 输入叶子，或「全输入」的容器 |
| `Bidirectional(Default)` | 混合方向的容器（默认朝向：含输出字段） |
| `Bidirectional(Flipped)` | 混合方向的容器（翻转朝向：含输入字段） |

为什么需要 `Bidirectional`？因为一个 `Bundle` 可能既有输入字段又有输出字段（比如握手接口的 `valid` 出、`ready` 入），它既不是纯 `Input` 也不是纯 `Output`，必须用「双向」来描述，并区分「默认」与「翻转」两种朝向。

#### 4.3.2 核心流程

从 `SpecifiedDirection` 到 `ActualDirection` 的关键桥梁是 `ActualDirection.fromSpecified`（[core/src/main/scala/chisel3/Data.scala:151-154](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L151-L154)）：

```scala
def fromSpecified(direction: SpecifiedDirection): ActualDirection = direction match {
  case Output | Unspecified => Output   // 不标 = 输出
  case Input  | Flip        => Input    // 翻转 = 输入
}
```

这两行写死了 Chisel 最重要的一条**默认约定**：

> 在没有强制父方向时，`Unspecified`（未标）默认是 **Output**，`Flip`（翻转）默认是 **Input**。

这就是为什么你在 `IO(new Bundle{ val a = UInt(8.W) })` 里那个没贴标签的 `a` 最终是个 `output` 端口——它走的就是 `Unspecified → Output` 这条路。

对容器（`Record`/`Vec`），方向还要综合子节点，用 `fromChildren`（[core/src/main/scala/chisel3/Data.scala:159-192](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L159-L192)）：若所有子节点同为 `Input` → 容器是 `Input`；同为 `Output` → `Output`；既有入又有出 → `Bidirectional(Default/Flipped)`。

叶子与容器两条结算路径合起来：

```
叶子(Element.bind):
  effective = fromParent(parentDir, mySpecDir)
  direction = fromSpecified(effective)          # 直接映射

容器(Record.bind):
  effective = fromParent(parentDir, mySpecDir)
  for child: child.bind(_, effective)           # 递归
  direction = fromChildren({子方向集合}, effective) # 综合子节点
```

#### 4.3.3 源码精读

**叶子结算**在 [core/src/main/scala/chisel3/Element.scala:22-27](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Element.scala#L22-L27)：

```scala
private[chisel3] override def bind(target: Binding, parentDirection: SpecifiedDirection): Unit = {
  this.maybeAddToParentIds(target)
  binding = target
  val resolvedDirection = SpecifiedDirection.fromParent(parentDirection, specifiedDirection)
  direction = ActualDirection.fromSpecified(resolvedDirection)   // 结算落定
}
```

**容器结算**（以 `Record`，即 `Bundle` 的父类为例）在 [core/src/main/scala/chisel3/Aggregate.scala:895-928](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L895-L928)，核心是递归 `child.bind(childBinding, resolvedDirection)` 把自己的有效方向作为子的父方向传下去，最后用 `fromChildren` 汇总：

```scala
override private[chisel3] def bind(target: Binding, parentDirection: SpecifiedDirection): Unit = {
  ...
  val resolvedDirection = SpecifiedDirection.fromParent(parentDirection, specifiedDirection)
  ...
  for (((_, child), ...) <- this.elements.iterator.zip(...)) {
    child.bind(childBinding, resolvedDirection)   // 关键：把 resolvedDirection 传给子
    ...
  }
  val childDirections = elementsIterator.map(_.direction).toSet - ActualDirection.Empty
  direction = ActualDirection.fromChildren(childDirections, resolvedDirection) match {
    case Some(dir) => dir
    case None      => throwException(...)         // 子方向无法调和 → 内部错误
  }
}
```

这两段是本讲「方向如何被算出来」的核心证据。注意 `Vec.bind`（[core/src/main/scala/chisel3/Aggregate.scala:245-260](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L245-L260)）逻辑相同，只是 `Vec` 所有元素同类型，故直接用 `sample_element` 代表全体子节点。

**字段存储**：`Data` 用两个 `Byte` 字段分别缓存这两层方向，见 [core/src/main/scala/chisel3/Data.scala:389-406](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L389-L406)：

```scala
private var _specifiedDirection: Byte = SpecifiedDirection.Unspecified.value
private[chisel3] def specifiedDirection: SpecifiedDirection = SpecifiedDirection.fromByte(_specifiedDirection)

private var _directionVar: Byte = ActualDirection.Unset
private[chisel3] def direction: ActualDirection = _direction.get   // 仅绑定后有效
private[chisel3] def direction_=(actualDirection: ActualDirection): Unit = {
  if (_direction.isDefined) throw RebindingException(...)          // 方向只能定一次
  _directionVar = actualDirection.value
}
```

注释里写明「`_direction` 仅在 binding 设置后有效」，且 `direction_=` 一次性写入、重复赋值会抛 `RebindingException`——这解释了 [4.1.5](#4115-小练习与答案) 练习 2 里「不能调换 `assignCompatDir` 与 `bind`」的根因。

#### 4.3.4 代码实践

**实践目标**：观察「默认约定」——不贴标签的字段会变成 `output`，而 `Flipped` 的字段会变成 `input`。

**操作步骤**：

```scala
// 示例代码
import chisel3._
import chisel3.stage.ChiselStage

class DefaultDir extends Module {
  val io = IO(new Bundle {
    val a = UInt(4.W)              // 不贴标签
    val b = Flipped(UInt(4.W))     // 翻转
  })
}
```

用 `println(ChiselStage.emitSystemVerilog(new DefaultDir))` 生成 Verilog。

**需要观察的现象**：端口列表里 `a` 是 `output`、`b` 是 `input`。

**预期结果**：

```verilog
module DefaultDir(
  input        clock,
  input        reset,
  output [3:0] a,   // Unspecified → fromSpecified → Output
  input  [3:0] b    // Flip        → fromSpecified → Input
);
```

注意：实际进入 `bind` 前，`assignCompatDir` 已把 `a` 的 `Unspecified` 显式改成 `Output`、`b` 的 `Flip` 改成 `Input`（见 [4.4](#44-flipped--input--output三个方向操作函数)），但最终 `ActualDirection` 与「默认约定」算出的结果一致。**待本地验证**生成的端口顺序与命名风格。

#### 4.3.5 小练习与答案

**练习 1**：一个 `Bundle` 同时含一个 `Output` 字段和一个 `Flipped`（即 `Input`）字段，它的 `ActualDirection` 是什么？

**参考答案**：是 `Bidirectional(Default)`（若该 Bundle 本身未被外层翻转）。因为 `fromChildren` 发现子方向集合 = `{Output, Input}`，既不全是 `Input` 也不全是 `Output`，于是落入双向分支；容器自身的 `specifiedDirection` 为 `Unspecified` 时返回 `Bidirectional(Default)`。这正是握手接口（既有出又有入）的典型方向。

**练习 2**：为什么 `_direction` 要设计成「只能写一次」？

**参考答案**：方向是绑定后由整棵树结构**推导**出的确定结果，不是可变状态。允许重复赋值意味着同一节点在不同时刻方向不同，会让后续连线检查（`MonoConnect`/`BiConnect`，见 [u3-l4](u3-l4-connect-implementation.md)）无法依赖一个稳定的方向。一次性写入用 `RebindingException` 把「重复绑定」这类编程错误尽早暴露。

---

### 4.4 Flipped / Input / Output：三个方向操作函数

#### 4.4.1 概念说明

`Input`、`Output`、`Flipped` 是用户给类型「贴标签」的三个函数，定义在一起（[core/src/main/scala/chisel3/Data.scala:304-326](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L304-L326)）。它们的共同点是：**克隆入参类型，再设置其 `specifiedDirection` 字段**。区别在于设成哪个值：

| 函数 | 设置的 `specifiedDirection` | 类别 | 行为 |
|---|---|---|---|
| `Output(d)` | `Output` | 强制 | 整棵子树强制为输出 |
| `Input(d)` | `Input` | 强制 | 整棵子树强制为输入 |
| `Flipped(d)` | `flip(d.specifiedDirection)` | 相对 | 把现有方向翻转一次 |

注意 `Flipped` 不是简单写成 `Flip`，而是 `flip(d.specifiedDirection)`——它翻转的是 **`d` 原有的方向标签**。所以：

- `Flipped(UInt(...))`：原 `Unspecified` → `flip(Unspecified)` = `Flip`。
- `Flipped(Input(UInt(...)))`：原 `Input` → `flip(Input)` = `Output`。

#### 4.4.2 核心流程

三者都转调同一个私有 helper `SpecifiedDirection.specifiedDirection(source)(dir)`（[core/src/main/scala/chisel3/Data.scala:69-78](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L69-L78)）：

```
specifiedDirection(source)(dir):
  1. 记下当前 idGen（prevId）
  2. 求值 source（按名），requireIsChiselType
  3. 若 source 不是「新鲜」对象 → cloneTypeFull（保护不可变）
  4. out.specifiedDirection = dir(source)   # 在克隆体上贴标签
  5. 返回克隆体
```

这里和 `IO.apply` 一样用了「按名传参 + `mustClone(prevId)`」的模式：尽量复用新鲜对象，否则克隆。**重要**：标签是贴在**克隆体**上的，原对象不变——这就是函数签名注释里那句「they currently clone their source argument」的含义（[core/src/main/scala/chisel3/Data.scala:307-310](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L307-L310)）。

**Chisel2 兼容层**：在 `IO(...)` 落地时，`Module.assignCompatDir`（[core/src/main/scala/chisel3/Module.scala:191-203](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L191-L203)）会遍历所有叶子，把仍是 `Unspecified`/`Flip` 的显式改成 `Output`/`Input`（调用 `_assignCompatibilityExplicitDirection`，[core/src/main/scala/chisel3/Data.scala:408-419](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L408-L419)）。这正是 `fromSpecified` 那条「`Unspecified`→`Output`、`Flip`→`Input`」约定的「施工队」。

#### 4.4.3 源码精读

三个函数本体极简（[core/src/main/scala/chisel3/Data.scala:311-326](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L311-L326)）：

```scala
object Input {
  def apply[T <: Data](source: => T): T =
    SpecifiedDirection.specifiedDirection(source)(_ => SpecifiedDirection.Input)   // 恒为 Input
}
object Output {
  def apply[T <: Data](source: => T): T =
    SpecifiedDirection.specifiedDirection(source)(_ => SpecifiedDirection.Output)  // 恒为 Output
}
object Flipped {
  def apply[T <: Data](source: => T): T =
    SpecifiedDirection.specifiedDirection(source)(x => SpecifiedDirection.flip(x.specifiedDirection)) // 翻转原方向
}
```

`Input`/`Output` 的回调是常量函数 `_ => ...`（无视入参方向），体现「强制」；`Flipped` 的回调读取 `x.specifiedDirection` 再 `flip`，体现「相对」。短短三行差异，正是「强制 vs 相对」二分法的源码化身。

**桥接下一讲——Alignment。** 结算出的方向，最终要被连线算法（`:=`/`<>`）消费。连线侧不直接读 `ActualDirection`，而是把它转译成一种「相对根的对齐关系」，即 [core/src/main/scala/chisel3/connectable/Alignment.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Alignment.scala) 里的 `AlignedWithRoot` / `FlippedWithRoot`（[core/src/main/scala/chisel3/connectable/Alignment.scala:57-71](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Alignment.scala#L57-L71)）。其中 `Alignment.isCoercing`（[core/src/main/scala/chisel3/connectable/Alignment.scala:82-96](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Alignment.scala#L82-L96)）顺着 `ChildBinding` 往上爬，只要祖先链上有 `Input`/`Output` 就标记为「被强制」——这与本讲「强制 vs 相对」的划分完全对应。`deriveChildAlignment`（[core/src/main/scala/chisel3/connectable/Alignment.scala:98-106](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/connectable/Alignment.scala#L98-L106)）则按子节点的 `SpecifiedDirection` 推导子的对齐关系，逻辑与 `fromParent` 同构。本讲你只需要记住：**连线时用到的「谁该驱动谁」的判断，源头就是本讲这套方向标签**；细节留到 [u3-l3](u3-l3-connectable-operators.md) 与 [u3-l4](u3-l4-connect-implementation.md)。

#### 4.4.4 代码实践

**实践目标**：定义一个相对方向的子 Bundle，分别用 `Input`/`Output`/`Flipped` 包裹它，预测并核对每个端口的 `input`/`output` 方向。这是本讲的主实践。

**操作步骤**：

```scala
// 示例代码
import chisel3._
import chisel3.stage.ChiselStage

// 用相对方向定义的握手通道：data/valid 朝外，ready 朝内（翻转）
class Channel extends Bundle {
  val data  = Output(UInt(8.W))    // 相对：朝外
  val valid = Output(Bool())       // 相对：朝外
  val ready = Flipped(Bool())      // 相对：翻转 → 朝内
}

class DirectionDemo extends Module {
  val io = IO(new Bundle {
    val in  = Input(new Channel)         // 强制：整棵 Channel 全是 input
    val out = Output(new Channel)        // 强制：整棵 Channel 全是 output
    val rev = Flipped(new Channel)       // 翻转：data/valid 变 input，ready 变 output
  })
  // 把所有输出端口接到常量，避免「未连接」警告，聚焦观察方向
  io.out.data  := 0.U
  io.out.valid := false.B
  io.out.ready := false.B
  io.rev.ready := false.B
}

object DirectionDemoApp extends App {
  println(ChiselStage.emitSystemVerilog(new DirectionDemo))
}
```

**逐端口预测**（套用 `fromParent` + `fromSpecified`）：

| 字段 | in（Input 强制） | out（Output 强制） | rev（Flipped 翻转） |
|---|---|---|---|
| `data`（原 Output） | input | output | input（Output 被翻转） |
| `valid`（原 Output） | input | output | input（Output 被翻转） |
| `ready`（原 Flip→Input） | input | output | output（Input 被翻转） |

**需要观察的现象**：生成 Verilog 的端口表里，`in_*` 三个全是 `input`；`out_*` 三个全是 `output`；`rev_data`/`rev_valid` 是 `input`，而 `rev_ready` 是 `output`。

**预期结果**（端口列表形如）：

```verilog
module DirectionDemo(
  input        clock,  input        reset,
  input  [7:0] in_data,  input        in_valid,  input        in_ready,
  output [7:0] out_data, output       out_valid, output       out_ready,
  input  [7:0] rev_data, input        rev_valid, output       rev_ready
);
```

**待本地验证**：字段命名风格（下划线分隔）与端口顺序可能因 `suggestName`/插件而略有差异，但每个端口的 `input`/`output` 朝向必须与上表一致。

#### 4.4.5 小练习与答案

**练习 1**：`Output(Flipped(x))` 与 `Flipped(Output(x))` 结果相同吗？

**参考答案**：相同，最终都让 `x` 这棵子树强制为 `Output`。
- `Flipped(Output(x))`：先内层 `Output(x)` 设为 `Output`，外层 `Flipped` 设为 `flip(Output)` = `Input`……等一下——注意 `Flipped/Output` 贴的是**各自节点**的局部标签，而非整树。准确说：`Output(Flipped(x))` 让外层节点标签=`Output`、内层 `x` 标签=`Flip`；结算时 `fromParent(Output, ...)` 强制为 `Output`，内层被父强制盖住，整体输出。`Flipped(Output(x))` 让外层=`flip(Output 的 specifiedDirection)`——但这里 `Output(x)` 返回的新对象 specifiedDirection 已是 `Output`，外层 `Flipped` 再 `flip(Output)` = `Input`，于是外层强制 `Input`，整树输入。**所以两者并不相同**：前者整体 `Output`，后者整体 `Input`。这道题的陷阱正是「`Flipped` 翻转的是入参已有标签」。建议你本地用 Verilog 验证后再下结论。

**练习 2**：为什么不推荐在叶子节点上滥用 `Flipped`，而推荐在子 Bundle 层面用？

**参考答案**：`Flipped` 的价值在于「整体翻转一片子树以复用接口定义」（如把生产者接口翻成消费者接口）。在单个叶子上写 `Flipped(UInt(...))` 虽然合法（等价于输入），但丧失了复用意义，且可读性不如直接 `Input(UInt(...))`。把 `Flipped` 用在子 Bundle 上，配合相对方向的字段定义，才是它的设计意图——这也是 `DecoupledIO` 等标准接口能「一次定义、双向复用」的基础。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「**方向侦探**」小任务。

**任务**：下面这份接口定义里，每个叶子的最终方向（`input`/`output`）是什么？请先用本讲的 `fromParent`/`fromSpecified` 手算预测，再生成 Verilog 核对。

```scala
// 示例代码
class Sub extends Bundle {
  val p = UInt(4.W)            // Unspecified
  val q = Output(UInt(4.W))    // Output
  val r = Flipped(UInt(4.W))   // Flip
}

class Detective extends Module {
  val io = IO(new Bundle {
    val a = Input(new Sub)         // 强制输入
    val b = Output(new Sub)        // 强制输出
    val c = Flipped(new Sub)       // 翻转
    val d = new Sub                // 不包裹：相对顶层 Unspecified
  })
}
```

**要求**：

1. 画出 `Sub` 的字段表，标注每个字段的 `SpecifiedDirection`。
2. 对 `a`/`b`/`c`/`d` 四个字段，分别写出顶层有效方向，再逐叶子结算。
3. 特别留意 `d`：它没有被 `Input`/`Output`/`Flipped` 包裹，顶层方向是 `Unspecified`，子字段会沿默认约定结算（`p` 的 `Unspecified` → `Output`，`r` 的 `Flip` → `Input`，`q` 仍是 `Output`）。
4. 用 `ChiselStage.emitSystemVerilog(new Detective)` 生成 Verilog，逐一核对 `a_p`/`a_q`/.../`d_r` 共 12 个端口的方向。

**验收标准**：手算结果与 Verilog 端口表完全一致；若不一致，定位是 `fromParent` 哪个分支算错，或漏算了 `assignCompatDir` 的兼容层影响。

---

## 6. 本讲小结

- `IO(...)` 是端口登记入口：按名求值类型 → `requireIsChiselType` 把关 → 必要时克隆 → `bindIoInPlace`（`assignCompatDir` + `bind(PortBinding)` + 登记进 `_ports`）。
- 方向系统是两层：用户层 `SpecifiedDirection`（`Unspecified`/`Output`/`Input`/`Flip`）与结算层 `ActualDirection`（含 `Bidirectional` 表达混合容器）。
- 二分法是钥匙：`Output`/`Input` 属**强制类**（`fromParent` 里盖住一切）；`Unspecified`/`Flip` 属**相对类**（取决于父方向）。
- 默认约定：无强制父时 `Unspecified → Output`、`Flip → Input`，由 `fromSpecified` 与 `assignCompatDir` 共同落实。
- `Input/Output/Flipped` 都通过 `specifiedDirection(source)(dir)` helper 在**克隆体**上贴标签；`Flipped` 翻转的是入参原有标签，故 `Flipped(Output(x))` 与 `Output(Flipped(x))` 不同。
- 结算靠递归 `bind`：叶子用 `fromParent`+`fromSpecified` 一锤定音，容器用 `fromChildren` 综合子节点；方向一次性写入，重复赋值抛 `RebindingException`。

## 7. 下一步学习建议

本讲把「方向是怎么算出来的」讲透了，但**方向如何影响连线**还没展开。建议接着学：

- **[u3-l3 Connectable 与连线操作符](u3-l3-connectable-operators.md)**：`:=`/`<>` 等操作符背后的 `Connectable` 抽象，以及本讲末尾提到的 `Alignment` 如何被连线算法消费。
- **[u3-l4 连线的内部实现：MonoConnect 与 BiConnect](u3-l4-connect-implementation.md)**：单向/双向连接的方向匹配规则，以及「为什么两个 `Output` 用 `:=` 连接会报错」的根因——那里会反复用到本讲的 `ActualDirection`。
- 之后进入 **[u4 Builder 与 elaboration 内部机制](../)**，你会看到 `bind`、`PortBinding`、命令记录是如何被 `Builder` 全局状态机统筹的。
