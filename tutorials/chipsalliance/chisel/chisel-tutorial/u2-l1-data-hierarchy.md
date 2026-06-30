# Data 抽象基类与类型层级

## 1. 本讲目标

本讲是「数据类型系统」单元（单元 2）的第一篇。学完本讲，你应当能够：

- 说清楚 `Data` 在 Chisel 类型系统里的「根抽象」地位，以及它为什么是 `abstract class` 而不是 `trait`。
- 区分 Chisel 硬件类型的两大分支：**叶子** `Element` 与**聚合** `Aggregate`，并能说出它们各自代表谁（`UInt`/`SInt`/`Bool`/`Clock`… vs `Bundle`/`Vec`/`Record`）。
- 理解 `Width` 这个「位宽」抽象，特别是它为什么有「已知 / 未知」两种状态，以及这和位宽推断（width inference）的关系。
- 牢牢抓住一个贯穿全手册的核心区分：**类型（type）** vs **硬件值（hardware value）**——同一个 `UInt(8.W)`，写在 `IO(...)` 里和写在 `Wire(...)` 里是两回事。

本讲只讲「骨架」，不深入每个具体类型（`UInt` 的运算、`Bundle` 的字段命名等留到 u2-l2、u2-l3）。

## 2. 前置知识

阅读本讲前，你应当已经具备（来自单元 1）：

- **Chisel 是嵌在 Scala 里的硬件构造 DSL**：你写的是合法 Scala 代码，运行时（elaboration 细化）才「长出」电路（见 u1-l1、u1-l5）。
- **字面量 `.U` / `.S` / `.B` / `.W`**：`chisel3` 包对象用隐式转换让 Scala 的 `Int/BigInt/Boolean` 凭空获得这些方法（见 u1-l4）。
- **「只登记不施工」**：模块构造体里的每一行（`IO`、`:=`、`Reg`…）只是向 `Builder` 记录命令，本身不产生硬件文件（见 u1-l5）。

本讲会用到的两个 Scala 概念，先一句话解释：

- **`abstract class`（抽象类）vs `trait`（特质）**：两者都能定义抽象成员（没有方法体的方法，由子类去实现）。`abstract class` 更像传统的「基类」，每个子类只能继承一个抽象类；`trait` 更像「可叠加的能力接口」，可以混入多个。Chisel 用 `abstract class` 搭主干，用 `trait` 加能力。
- **`sealed`（密封）**：被 `sealed` 修饰的类型，其所有直接子类必须写在**同一个源文件**里。这意味着编译器能知道「全部子类就这些」，从而让模式匹配做到穷尽检查（exhaustive checking）。Chisel 大量用 `sealed` 来锁死类型层级的边界。

## 3. 本讲源码地图

本讲涉及的关键文件，都在 `core` 子项目里（关于 `core` 与其他子项目的划分，见 u1-l3）：

| 文件 | 作用 |
| --- | --- |
| [core/src/main/scala/chisel3/Data.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala) | 所有硬件数据类型的**根抽象**，定义方向、绑定、连线、位宽、克隆等通用接口。 |
| [core/src/main/scala/chisel3/Element.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Element.scala) | **叶子**分支基类：不能再包含其它 `Data` 的原子类型（`UInt`、`Clock`…）。 |
| [core/src/main/scala/chisel3/Width.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Width.scala) | **位宽**抽象，只有两种取值：`KnownWidth`（已知）与 `UnknownWidth`（未知）。 |
| core/src/main/scala/chisel3/Aggregate.scala | **聚合**分支基类 `Aggregate` 及其子类 `Record`/`Bundle`/`Vec`（本讲只看它的「分支身份」）。 |
| core/src/main/scala/chisel3/Bits.scala | `Bits` 抽象与 `UInt`/`SInt`/`Bool` 等（本讲只看它们在层级里的位置）。 |

## 4. 核心概念与源码讲解

### 4.1 Data：所有硬件类型的根抽象

#### 4.1.1 概念说明

在 Chisel 里，**凡是能代表「一段硬件信号」的东西，都是 `Data` 的子类**。无论是一根 8 位无符号线（`UInt(8.W)`）、一个时钟（`Clock`）、一个自定义接口（`Bundle`），还是一个向量（`Vec`），它们的最顶层共同祖先都是 `Data`。

为什么需要一个根抽象？因为 Chisel 的编译器、连线算法、elaboration 流程都要用「统一的方式」处理所有信号：连一连（`:=` / `<>`）、算位宽、克隆一份、展平成叶子位序列……这些操作的签名都写成 `T <: Data`，即「任意一种硬件类型」。`Data` 就是这个 `<: Data` 约束的名字。

一个关键直觉：**`Data` 既是「类型模板」，也是「硬件值」的载体**——这两种角色由同一个类家族承担，靠 `binding` 字段（见 4.1.3）来区分。这是 Chisel 最容易让初学者困惑的点，我们在 4.1 和 4.3 里反复强化它。

#### 4.1.2 核心流程

`Data` 自己不实现大部分行为，它只**声明契约**，把实现下放给 `Element` / `Aggregate` 两大子类。可以把 `Data` 提供的能力分成五组：

1. **身份与命名**：每个 `Data` 都有唯一 id、父模块归属、可读名字（继承自 `HasId` / `NamedComponent`）。
2. **方向（direction）**：输入 / 输出 / 翻转，由 `specifiedDirection` 与解析后的 `direction` 共同表达。
3. **绑定（binding）**：记录这个 `Data` 在电路图里的角色——是端口、是线、是寄存器、是字面量，还是「只是个类型没接线」。这正是区分「类型」与「硬件值」的关键。
4. **连线**：`:=`（单向）与 `<>`（批量）两个操作符（具体算法在 u3-l3、u3-l4）。
5. **位宽与克隆**：抽象的 `width`、`cloneType`、`getWidth` 等。

`Data` 的两条最重要的「抽象契约」是：

- `def cloneType: this.type` —— 造一个**同类型、不带绑定**的副本（「类型模板」的工厂）。
- `def width: Width` —— 这个信号占多少 bit（可能未知）。

#### 4.1.3 源码精读

先看 `Data` 的类声明——它是 **`abstract class`，不是 `trait`**：

[core/src/main/scala/chisel3/Data.scala:336](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L336) —— `Data` 混入了 `HasId`（身份与唯一 id）、`NamedComponent`（可命名）和 `DataIntf`（用户文档接口）：

```scala
abstract class Data extends HasId with NamedComponent with DataIntf {
```

> 注意：`DataIntf` 是一个带 self-type `self: Data =>` 的 trait，用来挂 ScalaDoc，不改变继承层级。

接着看区分「类型」与「硬件值」的核心——**`binding` 字段**。源码注释说得很直白：

[core/src/main/scala/chisel3/Data.scala:421-427](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L421-L427) —— `binding` 记录「这个节点在电路图里的位置」，初始为 `null`（即「纯类型，未绑定」）：

```scala
// Binding stores information about this node's position in the hardware graph.
private var _bindingVar: Binding = null
...
protected[chisel3] def binding: Option[Binding] = _binding
```

- `_bindingVar == null` ⟹ `binding == None` ⟹ 这是一个**纯类型**（比如直接写 `UInt(8.W)` 当模板）。
- `_bindingVar != null` ⟹ 这是一个**已绑定的硬件值**（端口、Wire、Reg、字面量……）。

`Data` 提供了两个守卫函数来强制区分这两种角色（在 [Data.scala:7](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L7) 导入），它们在许多 API 入口被调用。例如 `Wire` 工厂就要求传入的是**类型**而非已接线的硬件：

[core/src/main/scala/chisel3/Data.scala:1092](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L1092) —— `WireFactory` 里用 `requireIsChiselType` 把关：

```scala
val t = source
requireIsChiselType(t, "wire type")
```

反过来，`chiselTypeOf` 给你「从一个硬件值取出它的类型模板」的能力——内部就是克隆并清掉绑定：

[core/src/main/scala/chisel3/Data.scala:297-302](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L297-L302) —— 先要求它是硬件，再 `cloneTypeFull`：

```scala
object chiselTypeOf {
  def apply[T <: Data](target: T): T = {
    requireIsHardware(target)
    target.cloneTypeFull.asInstanceOf[T]
  }
}
```

而 `cloneType` 是 `Data` 留给子类的**抽象契约**（每个具体类型必须实现）：

[core/src/main/scala/chisel3/Data.scala:795](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L795)：

```scala
def cloneType: this.type
```

`cloneTypeFull`（[Data.scala:802-809](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L802-L809)）在 `cloneType` 基础上补回方向与探针信息，是 Chisel 内部更常用的「造一份干净类型」入口。

最后看位宽的抽象声明——`Data` 只声明、不实现：

[core/src/main/scala/chisel3/Data.scala:785-786](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L785-L786)：

```scala
private[chisel3] def width: Width
private[chisel3] def firrtlConnect(that: Data)(implicit sourceInfo: SourceInfo): Unit
```

`width` 是抽象的，由 `Element` 的子类各自存储、由 `Aggregate` 求和计算（见 4.2.3）。面向用户的 `getWidth` / `isWidthKnown` 则是对它的封装：

[core/src/main/scala/chisel3/Data.scala:859-866](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L859-L866)：

```scala
final def getWidth: Int =
  if (isWidthKnown) width.get else throwException(s"Width of $this is unknown!")
final def isWidthKnown: Boolean = width.known
final def widthOption: Option[Int] = if (isWidthKnown) Some(getWidth) else None
```

注意 `isWidthKnown` 直接调用 `width.known`——位宽是否已知，完全取决于 `Width` 这个抽象（见 4.3）。

#### 4.1.4 代码实践

**实践目标**：亲手在源码里确认「`Data` 是 `abstract class`」以及「类型 vs 硬件值」的边界。

**操作步骤**：

1. 打开 [Data.scala:336](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L336)，确认关键字是 `abstract class`（不是 `trait`），并记下它混入的三个父类型。
2. 在同文件搜索 `requireIsChiselType` 和 `requireIsHardware`，各举一处调用（提示：`Wire` 用前者，`chiselTypeOf` 用后者）。
3. 阅读下面的「示例代码」，预测哪一行会编译失败、哪一行会 elaboration 报错：

```scala
// 示例代码（非项目原有代码），用于理解 type vs hardware
import chisel3._
class Demo extends Module {
  val typ = UInt(8.W)          // (A) 这是「类型」，binding 为空
  val w = Wire(typ)            // (B) 把类型实例化成一根硬件线，binding 非空
  // Wire(w)                   // (C) 取消注释：把「硬件值」再当类型传给 Wire，会怎样？
}
```

**需要观察的现象**：

- 第 (A) 行的 `typ` 此时还**不代表任何真实硬件**，它只是一个「8 位无符号」的类型模板。
- 第 (B) 行 `Wire` 调用后，`w` 才真正是电路里的一根线。
- 第 (C) 行若取消注释：因为 `w` 已经被绑定（是硬件值），`requireIsChiselType` 会判定它不是「纯类型」。

**预期结果**：

- (A)、(B) 正常通过 elaboration。
- (C) 会触发 Chisel 的 elaboration 错误，提示传入的应是 chisel type 而非已绑定的硬件。具体报错文案**待本地验证**（不同版本措辞可能略有差异），但根因就是 `binding` 非空。

#### 4.1.5 小练习与答案

**练习 1**：`Data` 为什么用 `abstract class` 而不是 `trait`？请从「它持有什么状态」角度回答。

**参考答案**：`Data` 持有大量**可变状态**（`_bindingVar`、`_directionVar`、`_specifiedDirection`、`_probeInfoVar`、`_isConst` 等 `var` 字段，见 [Data.scala:374-433](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L374-L433)）。Scala 的 `trait` 虽然也能有字段，但带状态的「主干基类」用 `abstract class` 语义更清晰，也便于约束单继承的层级骨架；`trait` 则留给 `DataIntf`/`BitsIntf` 这类「加能力/加文档」的横切关注点。

**练习 2**：`cloneType` 与 `cloneTypeFull` 的关键差别是什么？

**参考答案**：`cloneType` 由各子类实现，返回一个**不带绑定**的同类型副本；`cloneTypeFull`（[Data.scala:802](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L802)）在 `cloneType` 基础上**额外补回 specifiedDirection、probeInfo、isConst**。所以「造一份干净类型」用 `cloneTypeFull`，它保留了方向等信息、只清掉硬件绑定。

### 4.2 Element 与 Aggregate：叶子与聚合两大分支

#### 4.2.1 概念说明

`Data` 直接下面只有**两个**分支（再加上少数特殊类型），这是 Chisel 类型系统最重要的二分法：

- **`Element`（叶子）**：原子的、**不能再拆分**成更小 `Data` 的类型。例如 `UInt`、`SInt`、`Bool`、`Clock`、`AsyncReset`、`Analog`、枚举 `EnumType`。它们对应「一段连续的位」。
- **`Aggregate`（聚合）**：由**其它 `Data` 组装**而成的容器。例如 `Vec`（同构向量）、`Record`/`Bundle`（命名字段聚合）。它们本身没有「值」，只是把子元素组织起来。

这个二分法不是装饰——Chisel 的很多算法就是「对叶子做事，对聚合递归到叶子」。最典型的就是**展平（flatten）**：把任意 `Data` 摊平成一串叶子 `Element`，因为最终下到 FIRRTL/Verilog 时，所有信号都是位向量。

#### 4.2.2 核心流程

「展平」逻辑最能体现这个二分法。对一个 `Data` 调用 `flatten`：

```
若它是 Aggregate → 对它的每个子元素递归 flatten，再拼接
若它是 Element   → 它自己就是叶子，返回 Seq(它自己)
否则             → 报错（理论上不会发生）
```

位宽的计算方式也随分支不同：

- **叶子**：位宽由具体类型自己「存着」（如 `Bits` 把 `width` 作为构造参数保存）。
- **聚合**：位宽 = 所有子元素位宽之和（动态计算）。

#### 4.2.3 源码精读

先看 `flatten` 是如何用一次模式匹配体现二分法的：

[core/src/main/scala/chisel3/Data.scala:340-346](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L340-L346)：

```scala
private[chisel3] def flatten: IndexedSeq[Element] = {
  this match {
    case elt: Aggregate => elt.elementsIterator.toIndexedSeq.flatMap { _.flatten }
    case elt: Element   => IndexedSeq(elt)
    case elt => throwException(s"Cannot flatten type ${elt.getClass}")
  }
}
```

> 这段代码也反过来证明：**任何一个能用的 `Data`，要么是 `Aggregate`，要么是 `Element`**——否则就会落入第三个 `case` 抛异常。

再看两个分支的类声明。`Element` 是叶子基类：

[core/src/main/scala/chisel3/Element.scala:17-20](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Element.scala#L17-L20) —— `abstract class Element extends Data`，且它的 `allElements` 就是它自己（叶子没有子元素）：

```scala
abstract class Element extends Data {
  private[chisel3] final def allElements: Seq[Element] = Seq(this)
  def widthKnown:                         Boolean = width.known
```

`Aggregate` 是聚合基类，**`sealed trait`**（锁死边界）：

[core/src/main/scala/chisel3/Aggregate.scala:27](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L27)：

```scala
sealed trait Aggregate extends Data {
```

聚合的位宽是**求和**算出来的（与叶子「自己存」形成对比）：

[core/src/main/scala/chisel3/Aggregate.scala:92](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L92)：

```scala
private[chisel3] def width: Width = elementsIterator.map(_.width).foldLeft(0.W)(_ + _)
```

> 这里的 `0.W` 是 `Width(0)` 即 `KnownWidth(0)`，`_ + _` 用的是 `Width.+(that: Width)`（见 4.3.3）。两个 `KnownWidth` 相加仍是 `KnownWidth`；只要有一个是 `UnknownWidth`，结果就「感染」成未知——这正是位宽推断的传播规则。

现在把两大分支下的主要类型定位到源码（**建议读者按 4.2.4 自己核对**）：

```
Data (abstract class, Data.scala:336)
├── Element (abstract class, Element.scala:17) ── 叶子分支
│    ├── Clock           (Clock.scala:15)
│    ├── AsyncReset      (Bits.scala:609)   ；trait Reset (Bits.scala:555)
│    ├── Analog          (experimental/Analog.scala:26)
│    ├── EnumType        (ChiselEnum.scala:14)
│    ├── Property[T]     (properties/Property.scala:215) ── trait
│    ├── DontCare        (Data.scala:1245) ── 特殊：表示「不关心」
│    ├── Type            (domain/Type.scala:51)
│    └── ToBoolable (trait, BitsIntf.scala:18) ── 可转 Bool 的中间层
│         └── BitsIntf (trait, BitsIntf.scala:30) { self: Bits => }
│              └── Bits (abstract class, Bits.scala:24)
│                   ├── UInt (Bits.scala:239)
│                   │    └── Bool (Bits.scala:639)
│                   └── SInt (Bits.scala:431)
└── Aggregate (sealed trait, Aggregate.scala:27) ── 聚合分支
     ├── Vec[T]  (Aggregate.scala:219) ── 同构向量
     └── Record  (abstract class, Aggregate.scala:806) { with Selectable }
          └── Bundle (abstract class, Aggregate.scala:1261) ── 命名字段聚合
```

**一个容易踩坑的点**：`Bits`（以及 `UInt`/`SInt`/`Bool`）**并不是直接 `extends Element`**。看声明：

[core/src/main/scala/chisel3/Bits.scala:24](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Bits.scala#L24) —— `Bits` 只写 `extends BitsIntf`：

```scala
sealed abstract class Bits(private[chisel3] val width: Width) extends BitsIntf {
```

而 `BitsIntf` 又继承自 `ToBoolable`，`ToBoolable` 才继承 `Element`（[scala-2/chisel3/BitsIntf.scala:18](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala-2/chisel3/BitsIntf.scala#L18)、[:30](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala-2/chisel3/BitsIntf.scala#L30)）：

```scala
private[chisel3] sealed trait ToBoolable extends Element { ... }
private[chisel3] trait BitsIntf extends ToBoolable { self: Bits => }
```

所以继承链是 `UInt → Bits → BitsIntf → ToBoolable → Element → Data`。这些中间 trait 的作用是**共享 API**（`ToBoolable` 提供 `.asBool` 一类能力、`BitsIntf` 提供位运算文档接口），它们带 `self: Bits =>` 自类型约束，保证这些能力只能挂在 `Bits` 上。结论：**`UInt`/`SInt`/`Bool` 确实属于 `Element` 叶子分支，但是「间接」继承**——这也是为什么 4.2.4 的实践里，「直接 `extends Element` 的类」名单里看不到 `Bits`。

> 另一个细节：`Bool extends UInt`（[Bits.scala:639](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Bits.scala#L639)），即 `Bool` 是 1 位的 `UInt`，同时还混入 `Reset` trait——所以 `Bool` 既能当布尔值，也能当复位信号。

#### 4.2.4 代码实践

**实践目标**：在源码里亲自核验「Element 的直接子类」名单，并画出一张本节这样的类型层级小图。这是本讲的主打实践任务。

**操作步骤**：

1. 打开 [Element.scala:17](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Element.scala#L17)，确认 `Element extends Data`。
2. 在 `core/src/main/scala` 下搜索字符串 `extends Element`，把命中项分类：哪些是 `class`、哪些是 `trait`。你会得到：`Clock`、`AsyncReset`、`Analog`、`EnumType`、`Property`、`DontCare`、`Type`、`Reset`(trait)、`ToBoolable`(trait)。
3. 注意 `Bits` **不在**上面这份名单里——按 4.2.3 追一遍 `Bits → BitsIntf → ToBoolable → Element`，理解为什么。
4. 再搜索 `extends Aggregate`，确认聚合分支只有 `Vec` 与 `Record`（`Bundle` 是 `extends Record`）。
5. 把以上结果整理成一张层级小图（可参照 4.2.3 的树）。

**需要观察的现象 / 预期结果**：

- `Element` 的**直接**声明子类里没有 `Bits/UInt/SInt/Bool`，但它们通过 `ToBoolable` 间接属于 `Element`。
- `Aggregate` 是 `sealed trait`，直接子类被锁死在 `Aggregate.scala` 同文件内（`Vec`、`Record`；`Bundle extends Record`）。
- 整棵树只有两个「大类」：叶子（`Element` 系）与聚合（`Aggregate` 系）。

**说明**：本实践为「源码阅读型实践」，不需要运行代码；若想用脚本辅助，可执行（**待本地验证**，因只读 grep 也可手工完成）：

```bash
# 示例命令：列出所有「直接继承 Element」的声明
grep -rn "extends Element" core/src/main/scala
```

#### 4.2.5 小练习与答案

**练习 1**：为什么 `flatten` 的返回类型是 `IndexedSeq[Element]` 而不是 `IndexedSeq[Data]`？

**参考答案**：因为展平的终点就是叶子。聚合会在递归中被拆开，最终留下的全部是 `Element`（[Data.scala:340](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L340)）。用 `Element` 作返回类型能把「结果一定是叶子」这一不变量编码进类型，调用方不必再处理聚合。

**练习 2**：`Aggregate` 的 `width` 是「算出来的」，`Bits` 的 `width` 是「存起来的」。请各指一行源码佐证。

**参考答案**：聚合求和——[Aggregate.scala:92](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L92) `def width: Width = elementsIterator.map(_.width).foldLeft(0.W)(_ + _)`；`Bits` 存储——[Bits.scala:24](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Bits.scala#L24) 构造参数 `private[chisel3] val width: Width`。

### 4.3 Width：位宽与「已知 / 未知」

#### 4.3.1 概念说明

硬件信号最关键的物理属性之一是**位宽**：它占多少 bit。但 Chisel 允许你**先不指定位宽**，让编译器根据用法去**推断**（width inference）。比如 `val x = Wire(UInt())` 不写宽度，等 `x := a + b` 之后，再由 `a`、`b` 的宽度推出 `x` 的宽度。

为了同时表达「已知 8 位」和「暂时未知」两种状态，Chisel 抽象出一个 `Width` 类型，它只有两个 concrete 取值：

- `KnownWidth(n)` —— 已知是 `n` 位。
- `UnknownWidth` —— 位宽未知，等待推断。

这其实是一个经典的**和类型（sum type）**：`Width = KnownWidth | UnknownWidth`。`sealed` 保证没有第三种可能。

和类型 vs 硬件值的区分类似，这里也有一个「三态位宽」的直觉：

- 当你写 `8.W`，得到 `KnownWidth(8)`。
- 当你写 `UInt()`（不传宽度），里面的 `width` 是 `UnknownWidth`。
- 位宽运算（加法、取 max）会传播「未知」：只要参与运算的有一个未知，结果就未知——这模拟了「推断尚未完成」。

#### 4.3.2 核心流程

`Width` 的运算遵循「未知感染」规则。用伪代码描述两个 `Width` `w1 op w2`（`op` 可以是 `+`、`min`、`max`）：

```
若 w1 与 w2 都已知(KnownWidth(a), KnownWidth(b)) → 结果 KnownWidth(f(a,b))
否则（至少一个 UnknownWidth）                → 结果 UnknownWidth
```

这正是代数里「底元素 ⊥ 吸收一切」的味道：把 `UnknownWidth` 想成「未定的 ⊥」，任何与 ⊥ 运算的结果仍是 ⊥。形式化地，对已知位宽 \(a, b\) 与运算 \(\odot\)：

\[
\mathrm{op}(\mathrm{Known}(a), \mathrm{Known}(b)) = \mathrm{Known}(a \odot b), \qquad
\mathrm{op}(\mathrm{Unknown}, \cdot) = \mathrm{op}(\cdot, \mathrm{Unknown}) = \mathrm{Unknown}
\]

`Width` 还提供面向用户的方法：`known`（是否已知）、`get`（取出位数）。`Data` 的 `isWidthKnown` / `getWidth` 就是它们的薄封装。

#### 4.3.3 源码精读

`Width` 的伴生对象只暴露两个工厂方法——这就是用户构造位宽的全部入口：

[core/src/main/scala/chisel3/Width.scala:5-8](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Width.scala#L5-L8)：

```scala
object Width {
  def apply(x: Int): Width = KnownWidth(x)   // 8.W → Width(8) → KnownWidth(8)
  def apply():       Width = UnknownWidth     // 不传参 → 未知
}
```

> 于是 `8.W`（来自 u1-l4 讲过的隐式类 `fromIntToWidth`）最终落到 `Width(8) = KnownWidth(8)`；而 `UInt()` 不传宽度时用的是 `Width()` = `UnknownWidth`。

抽象基类声明了所有运算和两个抽象成员 `known` / `get`：

[core/src/main/scala/chisel3/Width.scala:10-29](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Width.scala#L10-L29)（节选）：

```scala
sealed abstract class Width {
  type W = Int
  def min(that: Width): Width = this.op(that, _ min _)
  def max(that: Width): Width = this.op(that, _ max _)
  def +(that:   Width): Width = this.op(that, _ + _)
  ...
  def known: Boolean
  def get:   W
  protected def op(that: Width, f: (W, W) => W): Width
}
```

`UnknownWidth` 实现 `op` 时**直接返回自己**——这就是「未知感染」：

[core/src/main/scala/chisel3/Width.scala:31-35](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Width.scala#L31-L35)：

```scala
case object UnknownWidth extends Width {
  def known: Boolean = false
  def get:     Int = None.get                // 取值会抛异常——未知宽度没有具体位数
  def op(that: Width, f: (W, W) => W): Width = this   // 任何运算结果都是未知
```

`KnownWidth` 则把两个已知位宽按函数 `f` 算出来；遇到 `UnknownWidth` 就退化为未知：

[core/src/main/scala/chisel3/Width.scala:44-55](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Width.scala#L44-L55)：

```scala
sealed case class KnownWidth private (value: Int) extends Width {
  require(value >= 0, s"Widths must be non-negative, got $value")   // 位宽不可为负
  def known: Boolean = true
  def get:   Int = value
  def op(that: Width, f: (W, W) => W): Width = that match {
    case KnownWidth(x) => KnownWidth(f(value, x))   // 都已知 → 算
    case _             => that                       // 另一方未知 → 结果未知
  }
```

两个工程细节值得注意：

1. **位宽不可为负**：[Width.scala:49](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Width.scala#L49) 的 `require(value >= 0, ...)` 在构造时就拦截负位宽。
2. **小位宽被缓存**：`KnownWidth` 伴生对象对 0–1024 的位宽做了数组缓存（[Width.scala:59-72](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Width.scala#L59-L72)），因为位宽对象在 elaboration 中会被海量创建（每个信号的每次运算都会触碰 `Width`），缓存能显著降低分配开销。

最后，把 `Width` 和 4.1 的「类型 vs 硬件值」串起来：`UInt(8.W)` 作为**类型**时，`width = KnownWidth(8)` 已经确定；而 `Wire(UInt(8.W))` 这个**硬件值**继承同一个 `width`。位宽属于「类型」的属性，绑定（`binding`）才决定它是不是真实硬件——两者正交。

#### 4.3.4 代码实践

**实践目标**：用一段最小 Chisel 代码观察「已知位宽」与「未知位宽」的区别，并理解位宽推断的传播。

**操作步骤**：

1. 阅读下面「示例代码」，预测三处 `getWidth` / `isWidthKnown` 的行为。

```scala
// 示例代码（非项目原有代码）
import chisel3._
class WidthDemo extends Module {
  val a = IO(Input(UInt(8.W)))      // a：已知 8 位
  val b = IO(Input(UInt()))         // b：位宽未知（不传宽度）
  // val bad = IO(Input(UInt(-1.W)))// 取消注释：负位宽会怎样？
  val s = a + b                      // s 的位宽 = a.width + b.width = ?
}
```

2. 想清楚：`a + b` 的结果位宽按 `Width.+` 传播——`KnownWidth(8) + UnknownWidth` 等于什么？
3. 若环境允许，把这段放进一个可运行的 `Module`，用 `ChiselStage.emitSystemVerilog` 生成 Verilog（参考 u1-l4 的写法），观察 `b` 和 `s` 最终被推断成多少位。

**需要观察的现象 / 预期结果**：

- `a.isWidthKnown` 为 `true`，`a.getWidth == 8`。
- `b.isWidthKnown` 在 elaboration 早期为 `false`；若 `b` 作为输入端口最终必须被确定宽度，下游推断/FIRRTL 会补全（具体补全值**待本地验证**，取决于连接上下文）。
- `s` 的位宽在「感染」规则下会先变成 `UnknownWidth`，再随 `b` 一起被推断。
- 取消注释 `UInt(-1.W)`：会在 `KnownWidth` 构造时被 `require(value >= 0)` 拦截，抛出「Widths must be non-negative」错误。

> 说明：本讲聚焦类型骨架，不展开「FIRRTL/CIRCT 如何最终求解未知位宽」——那是 u4（Builder/IR）与 u5（Stage/CIRCT）的内容。

#### 4.3.5 小练习与答案

**练习 1**：`UnknownWidth.op(that, f)` 为什么直接 `return this`，而不是抛异常？

**参考答案**：因为「未知」是位宽推断过程中的**合法中间态**，不是错误。两个信号相加时，若一方宽度还没推断出来，结果宽度自然也未知——这个「未知」会继续向下游传播，等推断完成时一并被回填（[Width.scala:34](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Width.scala#L34)）。抛异常会阻断正常的推断流程。

**练习 2**：`getWidth`（[Data.scala:859](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L859)）在位宽未知时会怎样？它和 `widthOption` 的区别是什么？

**参考答案**：位宽未知时 `getWidth` 会 `throwException("Width of ... is unknown!")`（因为它强行调用 `width.get`，而 `UnknownWidth.get == None.get` 会抛异常）。`widthOption`（[Data.scala:866](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L866)）则更安全：未知时返回 `None`，已知时返回 `Some(n)`。所以「不确定位宽是否已知」时应优先用 `widthOption` 或先查 `isWidthKnown`。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「类型层级探针」小任务：

**任务**：写一个最简 `Module`，在其中分别声明以下五种信号，然后回答每种「属于哪个分支（Element/Aggregate）、位宽是否已知、是类型还是硬件值」：

```scala
// 示例代码（非项目原有代码）
import chisel3._
class Probe extends Module {
  val a = IO(Input(UInt(8.W)))                 // 1
  val b = IO(Input(Bool()))                    // 2
  val c = IO(Input(Vec(4, UInt(8.W))))         // 3
  val d = Wire(UInt())                         // 4
  val e = IO(Input(new Bundle {                // 5
    val valid = Bool()
    val data  = UInt(32.W)
  }))
}
```

**要求**：

1. 对 1–5 逐项填表：分支（Element/Aggregate）、`width` 当前状态（`KnownWidth(n)` / `UnknownWidth` / 由子元素求和）、是类型还是硬件值。
2. 用 4.2.4 的方法在源码里核验：`Bool`、`Vec`、`Bundle` 分别在层级树的哪个节点。
3. 解释：为什么 `c`（`Vec`）和 `e`（`Bundle`）的 `width` 不能用一个简单数字表达，而要「求和」？

**参考结论**（请先自己做再对照）：

| 信号 | 分支 | 位宽状态 | 类型/硬件值 |
| --- | --- | --- | --- |
| `a` `UInt(8.W)` | Element（经 Bits） | `KnownWidth(8)` | 经 `IO/Input` 绑定为硬件端口 |
| `b` `Bool()` | Element（`Bool→UInt→Bits`） | `KnownWidth(1)` | 硬件端口 |
| `c` `Vec(4, UInt(8.W))` | Aggregate（`Vec`） | 4 个 8 位求和 = 32 位 | 硬件端口 |
| `d` `Wire(UInt())` | Element（经 Bits） | `UnknownWidth`（待推断） | 经 `Wire` 绑定为硬件线 |
| `e` 自定义 `Bundle` | Aggregate（`Bundle→Record`） | 子元素求和（1 + 32 = 33 位） | 硬件端口 |

第 3 问：因为聚合类型本身不是一个「定长位向量」，它的总位宽取决于**所有子元素**；只有把每个叶子的位宽相加才能得到总宽，所以 `Aggregate.width` 用 `foldLeft(0.W)(_ + _)` 动态求和（[Aggregate.scala:92](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L92)）。

## 6. 本讲小结

- **`Data` 是根**：所有硬件信号类型的最顶层共同祖先，是 **`abstract class`**（非 `trait`），混入 `HasId`/`NamedComponent`/`DataIntf`；它声明了方向、绑定、连线、`cloneType`、`width` 等通用契约（[Data.scala:336](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L336)）。
- **类型 vs 硬件值**：同一家族承担两种角色，靠 `binding` 字段区分——`None` 是纯类型，`Some` 是已绑定的硬件值；`requireIsChiselType` / `requireIsHardware` 在 API 入口把关（[Data.scala:421-427](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L421-L427)）。
- **二分法**：`Data` 下分叶子 `Element`（`UInt`/`SInt`/`Bool`/`Clock`…，不可再拆）与聚合 `Aggregate`（`Vec`/`Record`/`Bundle`，由子元素组装）；`flatten` 的一次模式匹配就是这条二分法的缩影（[Data.scala:340-346](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L340-L346)）。
- **`Bits` 是间接叶子**：`UInt`/`SInt`/`Bool` 经 `BitsIntf → ToBoolable` 间接继承 `Element`，而不是直接 `extends Element`。
- **位宽是和类型**：`Width = KnownWidth | UnknownWidth`，`sealed` 锁死；未知位宽在运算中「感染」传播，模拟位宽推断的中间态（[Width.scala:31-55](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Width.scala#L31-L55)）。
- **位宽的两种来源**：叶子「存」（`Bits` 构造参数），聚合「算」（子元素求和）——同一条 `def width: Width` 契约的两种实现。

## 7. 下一步学习建议

本讲只搭好了类型系统的「骨架」，接下来按依赖顺序深入：

- **u2-l2 Bits / UInt / SInt / Bool 数值类型**：展开本讲里的 `Bits` 分支，讲字面量构造、位运算与算术运算，以及 `Num` 抽象。
- **u2-l3 Bundle：自定义聚合类型**：展开本讲里的 `Aggregate → Record → Bundle` 分支，讲字段命名与 `cloneType` 的必要性（你会真切体会到「类型」要能被反复克隆）。
- **u2-l4 Vec：硬件向量类型**：展开 `Aggregate → Vec`，讲 `VecInit` 与动态索引。
- 若你对「绑定（binding）到底有哪几种」更感兴趣，可以提前跳读 `core/src/main/scala/chisel3/internal/Binding.scala`，那会在 **u4-l3 Binding 系统** 系统讲解——本讲提到的 `binding: Option[Binding]` 届时会展开成 `LitBinding`/`PortBinding`/`NodeBinding`/`RegBinding` 等具体种类。
