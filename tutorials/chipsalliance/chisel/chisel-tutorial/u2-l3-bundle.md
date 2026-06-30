# Bundle：自定义聚合类型

## 1. 本讲目标

在上一讲（u2-l1）里，我们搭好了 Chisel 类型系统的骨架：`Data` 是根抽象，往下分成叶子分支 `Element`（`UInt`/`SInt`/`Bool` 等不可再拆的单个信号）和聚合分支 `Aggregate`（由多个子信号组装而成）。本讲专门下钻到**聚合分支里最常用的一支——`Bundle`**。

学完本讲，你应当能够：

- 说清 `Aggregate`、`Record`、`Bundle` 三者的层次关系与各自职责；
- 用 `class MyBundle extends Bundle { ... }` 定义一个自定义聚合类型，把 `valid`、`data`、`last` 这样的多个信号打包成一个有名字的整体；
- 理解为什么 Bundle 的字段必须是 `val`、字段名是怎么自动获得的、为什么需要 `cloneType`；
- 把一个 Bundle 用到模块的 `IO` 里，并解释 `IO(...)` 内部对它做了什么；
- 通过生成 Verilog 验证字段命名与字段在位拼接中的先后（高低位）顺序。

## 2. 前置知识

本讲默认你已经读过 u2-l1，并掌握以下概念。这里只做最简回顾，不展开：

- **`Data` 是所有硬件信号类型的根**。它同时扮演「类型（type）」和「硬件值（hardware）」两种角色，由内部的 `binding` 字段区分：`None` 表示纯类型，`Some(...)` 表示已绑定的硬件值。`requireIsChiselType` / `requireIsHardware` 在 API 入口把关。
- **`Element` 是叶子**（如 `UInt(8.W)`），不可再拆；**`Aggregate` 是聚合**（如 `Vec`、`Bundle`），由子 `Data` 组装。本讲的主角 `Bundle` 就在 `Aggregate` 这一支。
- **「只登记不施工」**：和 u2-l2 讲运算符时一样，你在 Scala 里写出的每一行硬件构造代码，本质上都只是往 Builder 的命令队列里追加命令，真正生成硬件/文件要等到后续发射阶段。

还有一个软件层面的直觉需要先建立：**Bundle 本质上就是一个普通的 Scala 类**。Chisel 没有发明新的「记录/结构体」语法，而是直接复用 Scala 的 `class` + `val` 成员来表达「一组命名字段」。理解了这一点，后面所有「字段名从哪来」「为什么要 cloneType」的问题，都会回到「它是个 Scala 对象」这个事实上。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `core/src/main/scala/chisel3/` 下：

| 文件 | 作用 |
| --- | --- |
| [Aggregate.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala) | 本讲的「主战场」。`Aggregate`、`Record`、`Bundle` 三个抽象全部定义在这个文件里（注意：文件名叫 Aggregate，但内容覆盖了三层）。`Vec` 也在其中，留给 u2-l4。 |
| [IO.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/IO.scala) | `IO(...)` 工厂方法的实现，说明一个 Bundle 是如何被登记为模块端口的。 |
| [Data.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala) | `Input`/`Output`/`Flipped` 三个方向工厂定义在此（实践任务会用到，方向的细节留到 u3-l2）。 |

> 小提示：Chisel 源码里「一个文件装多个相关类」很常见，`Aggregate.scala` 就是典型——读源码时别只看文件名，要用 `Grep`/IDE 按类名定位。

## 4. 核心概念与源码讲解

本讲按「自顶向下」拆成三个最小模块：先看聚合分支的根 `Aggregate`（4.1），再看键值式聚合的基类 `Record`（4.2），最后落到用户直接继承的 `Bundle`（4.3）。

### 4.1 Aggregate：聚合类型的共同抽象

#### 4.1.1 概念说明

`Aggregate` 是「**完全由其它 `Data` 组装而成**」的类型——它本身不持有具体的二进制位，而是把一组子 `Data` 聚在一起当作一个整体来用。

为什么需要它？因为现实里的硬件接口很少是「一根线」：一个 AXI 总线口、一个 FIFO 的读写口、一个状态机的输出，都由很多根信号组成，而且这些信号要**一起**被连接、一起被传递、一起拥有方向。`Aggregate` 就是给「一组信号打包」这件事提供统一抽象：无论你打包的是「同类型数组」（`Vec`）还是「命名字段集合」（`Record`/`Bundle`），对外都有共同的「宽度求和」「展平成叶子」「整体当字面量」「转成 `UInt`」等能力。

#### 4.1.2 核心流程

`Aggregate` 的核心是三个能力：

1. **列出子元素**：`getElements` / `elementsIterator` 返回直接子 `Data`。
2. **宽度求和**：聚合的位宽 = 所有叶子位宽之和。
3. **整体操作**：可以整体当字面量（`litValue`）、整体连成 `UInt`（`_asUIntImpl`）。

其中宽度的计算规则可以用一个简单式子表达。设聚合的所有叶子位宽为 \(w_0, w_1, \dots, w_{n-1}\)，则：

\[
W_{\text{aggregate}} = \sum_{i=0}^{n-1} w_i
\]

这与 u2-l1 讲过的「聚合位宽求和、叶子位宽存储」完全对应。

#### 4.1.3 源码精读

`Aggregate` 是一个 `sealed trait`，继承自 `Data`——这就是 u2-l1 里「聚合分支」的具体定义处：

[Aggregate.scala:27](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L27) —— `sealed trait Aggregate extends Data`，把聚合分支封死，只允许 `Vec`/`Record` 两个子分支（`Record` 再被 `Bundle` 继承）。

[Aggregate.scala:83](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L83) 与 [Aggregate.scala:90](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L90) —— 声明了 `def getElements: Seq[Data]` 和更高效的 `elementsIterator`，由子类实现，用来列出直接子元素。

[Aggregate.scala:92](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L92) —— 宽度求和的精确实现：

```scala
private[chisel3] def width: Width = elementsIterator.map(_.width).foldLeft(0.W)(_ + _)
```

这正是上面的求和公式 \(W_{\text{aggregate}} = \sum w_i\) 的直接翻译。

[Aggregate.scala:108-117](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L108-L117) —— `_asUIntImpl` 把整个聚合压成一个 `UInt`，靠的是把每个子元素的 `_asUIntImpl` 结果交给 `SeqUtils.asUInt` 拼接。注意 `first` 参数是为了兼容「空聚合在顶层返回 `0.U`、在嵌套时塌缩成 0 位」的历史行为。

> 一句话定位：`Aggregate` 是「我有孩子，我的宽度/字面量/位流都由孩子推导」的公共逻辑层。它**不知道**孩子叫什么名字——命名是 `Record` 的事。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：确认「聚合位宽 = 叶子位宽之和」在源码里是怎样一行写出的。
2. **步骤**：打开 [Aggregate.scala:92](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L92)，顺着 `foldLeft(0.W)(_ + _)` 理解：从 0 位宽开始，依次把每个子元素的 `width` 加上去。注意这里的 `+` 是 `Width` 上的加法（u2-l1 讲过 `KnownWidth` 相加、`UnknownWidth` 会「感染」传播）。
3. **观察**：如果某个子元素是未知位宽（`UnknownWidth`），求和结果也会变成未知——这解释了为什么一个字段位宽未定时，整个 Bundle 的位宽也跟着未定。
4. **预期结果**：你能用一句话说出「聚合的 width 是一行 foldLeft 算出来的」。

#### 4.1.5 小练习与答案

**练习 1**：`Aggregate` 是 `trait` 还是 `class`？为什么用 `sealed`？

**参考答案**：它是 `sealed trait`。`sealed` 把聚合分支「封顶」，编译器保证 `Aggregate` 的直接子类型只有本文件里的 `Vec` 和 `Record`，这样模式匹配（如 `flatten` 里的 `case elt: Aggregate => ...`）可以穷尽检查，避免漏掉新分支。

**练习 2**：一个 `Bundle` 里有 `a = UInt(8.W)` 和 `b = Bool()` 两个字段，整个 Bundle 的位宽是多少？

**参考答案**：`Bool()` 是 1 位，所以总位宽 \(8 + 1 = 9\) 位，由 [Aggregate.scala:92](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L92) 的求和得到。

### 4.2 Record：命名键值聚合的基类

#### 4.2.1 概念说明

`Aggregate` 有两个子分支：`Vec`（同类型数组，u2-l4 讲）和 `Record`（**由「名字 → Data」键值对**组成的聚合）。`Record` 是 `Bundle` 的直接父类。

那为什么要有 `Record`，不直接让大家继承 `Bundle`？源码注释说得很直白：

> Record should only be extended by libraries and fairly sophisticated generators. RTL writers should use Bundle.

也就是说：`Record` 是给**库作者**留的更底层、更灵活的口子——你可以用任意 `Map[String, Data]`（比如运行时动态生成的字段集合）来实现一个 `Record`；而 `Bundle` 是给**普通 RTL 开发者**用的、靠 `val` 字段定义的「静态」聚合。本讲我们重点用 `Bundle`，但要先理解 `Record` 提供了哪些底层能力——因为 `Bundle` 的命名、绑定、克隆机制全都挂在 `Record` 上。

#### 4.2.2 核心流程

`Record` 的生命周期关键步骤（elaboration 期间发生）：

1. **收集字段**：通过抽象成员 `elements: SeqMap[String, Data]` 拿到「名字 → 子 Data」的有序映射（`Bundle` 子类负责实现它）。
2. **净化名字**：`_elements` 把字段名交给 `Namespace` 做 sanitize（去掉非法字符、处理重名），产出可被 FIRRTL/Verilog 接受的标识符。
3. **递归绑定**：`bind` 把每个子元素挂上 `ChildBinding`，并递归算出整体方向。
4. **设置引用**：`setElementRefs` 给每个子元素分配形如 `父节点.字段名` 的引用，字段名就此变成实际信号名。
5. **克隆**：`cloneType` → `_cloneTypeImpl`，由编译器插件自动实现，重建一个全新的、未绑定的同类型对象。

#### 4.2.3 源码精读

[Aggregate.scala:806](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L806) —— `abstract class Record extends Aggregate with Selectable`，并附注释「库和高级生成器才继承 Record，RTL 开发者用 Bundle」。

[Aggregate.scala:1101](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L1101) —— 抽象成员 `def elements: SeqMap[String, Data]`。注意类型是 `SeqMap`（保序 Map）：字段**必须保持定义顺序**，因为顺序决定了序列化时的位拼接先后。`Bundle` 会实现它。

[Aggregate.scala:1114-1147](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L1114-L1147) —— `private[chisel3] lazy val _elements`：对 `elements` 做名字 sanitize。注释解释了为什么要 sanitize——`Namespace` 会把名字改成合法的 FIRRTL/Verilog 标识符，这有可能让两个原本不同的名字撞车，所以这里用 `Namespace.name` 统一去重。

[Aggregate.scala:895-935](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L895-L935) —— `Record.bind`：递归给每个子元素绑定 `ChildBinding(this)`、累计 `_containsAFlipped`（是否有翻转方向）、由子元素方向推导本节点方向，最后调用 `setElementRefs()`。

[Aggregate.scala:844-855](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L844-L855) —— `setElementRefs()`：遍历 `_elements`，用（已净化的）字段名给每个子元素 `setRef(thisNode, name, ...)`。**这一步就是把 Scala 字段名「钉」成 FIRRTL 信号名的地方**。

[Aggregate.scala:836-840](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L836-L840) 与 [Aggregate.scala:1184-1188](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L1184-L1188) —— `cloneType` 转发给 `_cloneTypeImpl`，而后者默认实现是「抛异常，说本应由 chisel3-plugin 实现」。这正是 `cloneType` 与编译器插件的交接点（插件的细节在 u7-l1）。

[Aggregate.scala:858-887](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L858-L887) —— `checkForAndReportDuplicates()`：检测「同一个 Data 对象被当成多个字段」的别名错误，抛 `AliasedAggregateFieldException`。这解释了一个常见报错：把同一个 `val` 在两个地方复用、或把 Data 嵌进非 val 结构里，就会触发它。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：跟踪「字段名 → FIRRTL 信号名」的转换链路。
2. **步骤**：
   - 从 [Aggregate.scala:1101](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L1101) 的 `elements`（原始名字）出发；
   - 跟到 [Aggregate.scala:1114](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L1114) 的 `_elements`（sanitize 后名字）；
   - 再到 [Aggregate.scala:844](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L844) 的 `setElementRefs`（写入引用）。
3. **观察**：名字在 `elements` 里还是你在 Scala 里写的原名，到了 `_elements` 被 `Namespace` 改写，最后在 `setElementRefs` 里成为引用的一部分。
4. **预期结果**：你能画出「`elements` → `_elements`(sanitize) → `setElementRefs`(setRef)」三步链路。

#### 4.2.5 小练习与答案

**练习 1**：`Record` 和 `Bundle` 谁是父类？普通 RTL 开发者应该继承谁？

**参考答案**：`Record` 是父类，`Bundle extends Record`。普通 RTL 开发者继承 `Bundle`；`Record` 留给需要用动态 `Map[String, Data]` 构造字段的库作者。

**练习 2**：为什么 `Record.elements` 的类型用 `SeqMap` 而不是普通 `Map`？

**参考答案**：因为字段顺序决定了序列化时位拼接的先后（进而影响 `.asUInt` 的高低字节序），必须保序。普通 `Map` 不保证迭代顺序，`SeqMap` 才保留插入顺序。

### 4.3 Bundle：用户自定义聚合类型

#### 4.3.1 概念说明

`Bundle` 是你日常最常写的聚合类型。写法极简——继承 `Bundle`，在类体里用 `val` 声明若干 `Data` 字段即可：

```scala
// 示例代码
class MyBundle extends Bundle {
  val valid = Bool()
  val data  = UInt(32.W)
  val last  = Bool()
}
```

它解决的核心问题是：**把多个相关信号打包成一个有名字、有结构、可整体传递与连接的类型**。最典型的用途就是定义模块的 IO 接口——一个模块的 `io` 往往就是一个 `Bundle`。

理解 `Bundle` 有两个关键直觉：

1. **它就是个 Scala 类**：`val valid = Bool()` 和你在普通 Scala 类里写 `val x = 0` 没有本质区别——只是这里的值是一个 Chisel 类型对象。字段名 `valid`/`data`/`last` 就是 Scala 的 `val` 名。
2. **字段名是「自动」获得的，但要靠编译器插件**：Chisel 的 scalac 插件（`chisel-plugin`）会在编译期扫描你的 `val` 成员，把「字段名 → 字段对象」收集起来，自动实现 `Bundle` 的 `_elementsImpl`。所以你从不手动写「这个字段叫 valid」，插件替你做了。插件的细节在 u7-l1，这里只要记住「没有插件，Bundle 就无法工作」。

#### 4.3.2 核心流程

定义并使用一个 Bundle 的端到端流程：

1. **定义类型**：`class MyBundle extends Bundle { val ... }`，每个 `val` 是一个**未绑定的 Chisel 类型**（不是硬件值）。
2. **实例化**：`new MyBundle` 产生一个该类型的对象；`Wire(new MyBundle)` / `Reg(new MyBundle)` / `IO(...)` 会在需要时调用 `cloneType` 复制出干净的副本。
3. **插件收集字段**：编译期，插件实现 `_elementsImpl`，返回 `Iterable[(String, Any)]`，即所有 `val` 字段名与对象。
4. **运行期处理**：`Bundle.elements` 调 `_processRawElements` 过滤掉非 `Data`/非可综合字段，并**把顺序反转**，得到保序的 `SeqMap`。
5. **绑定与命名**：继承自 `Record` 的 `bind`/`setElementRefs` 给每个字段分配方向和引用，字段名成为信号名。

#### 4.3.3 源码精读

[Aggregate.scala:1261](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L1261) —— `abstract class Bundle extends Record`，并带详细用法注释（匿名 IO Bundle 与命名 `Packet` 类两种写法）。

[Aggregate.scala:1266](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L1266) —— `assert(_usingPlugin, mustUsePluginMsg)`：Bundle **强制要求**编译器插件。`_usingPlugin` 默认 `false`，由插件在编译你的 Bundle 子类时改写为 `true`；若没挂插件，这里直接断言失败。

[Aggregate.scala:1350](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L1350) 与 [Aggregate.scala:1297](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L1297) —— `protected def _elementsImpl` 默认抛「应由插件实现」的异常；`final lazy val elements = _processRawElements(_elementsImpl)`。插件会重写 `_elementsImpl`，让它返回你所有 `val` 字段的 `(名字, 对象)` 序列。

[Aggregate.scala:1301-1342](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L1301-L1342) —— `_processRawElements`：逐个检查 raw 字段——是 `Data` 且 `isSynthesizable` 才纳入；遇到 `Option[Data]` 也兼容；遇到 `Seq[Data]` 会直接报错（提示「请用 Vec 或 MixedVec」）。最关键的是末尾的 `.reverse`（[Aggregate.scala:1341](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L1341)）：字段在内部按**定义顺序的逆序**存储。

**字段顺序与高低位**（重要且容易踩坑）：由于内部存储是逆序，再配合 `SeqUtils.asUInt`「序列最后一个元素是最高有效位」的约定（见 [SeqUtils.scala:18-25](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/SeqUtils.scala#L18-L25)），得到的结论是：**在 Bundle 中先定义的字段，位于 `asUInt` 的高位**。源码注释给了精确例子（[Aggregate.scala:1283-1295](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L1283-L1295)）：

```scala
// 引自源码注释
class MyBundle extends Bundle {
  val foo = UInt(16.W)   // 先定义 → 高位
  val bar = UInt(16.W)   // 后定义 → 低位
}
val bundle = Wire(new MyBundle)
bundle.foo := 0x1234.U
bundle.bar := 0x5678.U
val uint = bundle.asUInt
// assert(uint === "h12345678".U)  // foo 在高 16 位
```

**cloneType 的必要性**：Bundle 是带可变状态（`binding`）的 Scala 对象。当 Chisel 需要一个「同类型、但未绑定」的全新副本时（例如 `IO` 会克隆传入的类型、`Wire`/`Reg` 也会），就调用 `cloneType`。它最终走 [Aggregate.scala:836](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L836) 的 `Record.cloneType` → `_cloneTypeImpl`，由插件实现成「重新执行一次你的 Bundle 构造器」。如果你把 Data 嵌进了插件重建不了的构造里（如塞进普通 `Array`/闭包），插件克隆失败，会抛 [Aggregate.scala:822-834](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L822-L834) 的 `AutoClonetypeException`——这是写 Bundle 时最常见的报错之一。

**把 Bundle 用作 IO**：[IO.scala:25-64](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/IO.scala#L25-L64) 的 `IO.apply`。几条关键约束：

- [IO.scala:36](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/IO.scala#L36) —— `requireIsChiselType(data, "io type")`：传入的必须是**类型**而非已绑定的硬件值。所以 `IO(new MyBundle)` 正确，而 `IO(someWire)` 错误。
- [IO.scala:51-61](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/IO.scala#L51-L61) —— 必要时调用 `cloneTypeFull` 克隆一份，保证「类型不可变」。
- [IO.scala:62](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/IO.scala#L62) —— `module.bindIoInPlace(iodefClone)`：把这个 Bundle 登记为端口、完成方向绑定。

**给字段加方向**：方向工厂在 [Data.scala:311-326](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L311-L326)，`Input`/`Output`/`Flipped` 都委托给 [Data.scala:69-78](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Data.scala#L69-L78) 的 `specifiedDirection`，它会**克隆**源对象并打上方向标记。所以你可以逐字段加方向（`val out = Output(UInt(8.W))`），也可以整体包一层（`IO(Output(new MyBundle))`）。方向的完整规则留到 u3-l2，本讲实践只用最基础的 `Output`/`Input`。

#### 4.3.4 代码实践（可运行）

这是本讲的核心实践，对应任务里的 MyBundle。

1. **实践目标**：定义一个 `MyBundle`（含 `valid`/`data`/`last`），用作模块 IO，生成 Verilog 后核对字段命名与位序。

2. **操作步骤**：在你已经能编译运行 Chisel 的环境里（构建方式见 u1-l2、第一个模块见 u1-l4），新建一个 Scala 文件，写入下面的示例代码并运行（或在 `scala-cli`/REPL 里执行）。

```scala
// 示例代码
import chisel3._
import circt.stage.ChiselStage

// 1. 定义自定义聚合类型
class MyBundle extends Bundle {
  val valid = Bool()
  val data  = UInt(32.W)
  val last  = Bool()
}

// 2. 把它用作模块 IO
class MyModule extends Module {
  val io = IO(new Bundle {
    val in  = Input(new MyBundle)
    val out = Output(new MyBundle)
  })
  // 简单直通：把 in 整体连到 out（后续 u3-l3 讲 := 与 <>）
  io.out := io.in
}

// 3. 触发 Verilog 生成（这一行才是「按下生成按钮」）
object Gen extends App {
  println(ChiselStage.emitSystemVerilog(new MyModule))
}
```

3. **需要观察的现象**：
   - 生成的 SystemVerilog 里，`io_in` / `io_out` 端口是否各自展开成 `valid`、`data`、`last` 三个子信号，且名字与你写的 Scala `val` 名一致；
   - `data` 的位宽是否为 32，`valid`/`last` 是否各 1 位；
   - `io_out_valid` 等是否被一根 `assign` 连到对应的 `io_in_*`。

4. **预期结果**（端口部分示意，具体写法以本地输出为准——**待本地验证**）：

```verilog
// 示例代码（预期输出示意）
module MyModule(
  input        clock,
  input        reset,
  input        io_in_valid,
  input  [31:0] io_in_data,
  input        io_in_last,
  output       io_out_valid,
  output [31:0] io_out_data,
  output       io_out_last
);
  assign io_out_valid = io_in_valid;
  assign io_out_data  = io_in_data;
  assign io_out_last  = io_in_last;
endmodule
```

5. **位序验证（可选）**：在模块里加一行 `val u = io.in.asUInt`，对照本讲 4.3.3 的结论——`valid`（先定义）应在高位、`last`（后定义）应在低位。若想确认，可在生成结果里追踪 `u` 的位宽（应为 34 位）与拼接顺序。

6. **如果无法运行**：明确标注「待本地验证」。你也可以退而做**源码阅读型验证**——在 [Aggregate.scala:1301](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L1301) 的 `_processRawElements` 处确认：只有 `isSynthesizable` 的 `Data` 字段会被纳入 `elements`，这就是为什么你的三个 `val` 能正确出现在 Verilog 里。

#### 4.3.5 小练习与答案

**练习 1**：把上面 `MyModule` 的 IO 改成「整个 Bundle 当输出」：`val io = IO(Output(new MyBundle))`。生成的 Verilog 端口方向会怎样变化？

**参考答案**：`valid`/`data`/`last` 三个子信号都会变成 `output`（因为 `Output` 给整个 Bundle 打了输出方向，子元素继承之；方向继承的细节见 [Aggregate.scala:895-935](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L895-L935) 的 `Record.bind`）。

**练习 2**：如果你在 `MyBundle` 里写 `val xs = Seq(UInt(8.W), UInt(8.W))`，会发生什么？应该用什么替代？

**参考答案**：`_processRawElements` 检测到 `Seq[Data]` 会直接抛错并提示「请用 Vec 或 MixedVec」（见 [Aggregate.scala:1315-1327](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L1315-L1327)）。同类型用 `Vec(2, UInt(8.W))`（u2-l4），不同类型用 `MixedVec`。若这个 Seq 本就不参与硬件构造，可混入 `IgnoreSeqInBundle`（[Aggregate.scala:1204-1208](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L1204-L1208)）。

**练习 3**：为什么写 Bundle 时几乎从不手写 `cloneType`，而老资料里到处都要写？

**参考答案**：现代 Chisel 依赖编译器插件自动实现 `_cloneTypeImpl`（[Aggregate.scala:1184-1188](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L1184-L1188)），所以绝大多数情况不用手写。老版本/未挂插件时，用户必须自己 override `cloneType` 来重建对象——这就是老资料里 `override def cloneType = (new MyBundle).asInstanceOf[this.type]` 满天飞的原因。

## 5. 综合实践

把本讲的三层知识（Aggregate → Record → Bundle）串起来，做一个「带嵌套 Bundle 的请求—响应接口」小任务：

1. 定义一个 `Request` Bundle：`val addr = UInt(16.W)`、`val write = Bool()`、`val wdata = UInt(32.W)`。
2. 定义一个 `Response` Bundle：`val rdata = UInt(32.W)`、`val error = Bool()`。
3. 定义顶层 `MemIf` Bundle，把上面两个作为**嵌套字段**：`val req = Input(new Request)`、`val resp = Output(new Response)`。这验证 Bundle 可以无限嵌套——嵌套 Bundle 的位宽等于各层叶子之和。
4. 写一个 `Module`，IO 用 `MemIf`，内部把 `resp.rdata` 连到 `req.wdata`、`resp.error` 连到 `req.write` 的反（用 `!req.write`，布尔取反见 u2-l2）。
5. 生成 SystemVerilog，核对：
   - 嵌套字段名是否被展开成 `io_req_addr`、`io_resp_rdata` 这样的层级命名（命名由 `setElementRefs` 递归生成）；
   - 用 `io.req.asUInt` 验证 `Request` 内 `addr`（先定义）在高位。
6. **反思题**：如果把 `req` 改成 `Flipped(new Request)`（而不是 `Input`），端口方向会怎样？（提示：`Flipped` 把整个 Bundle 的方向翻转，方向规则详见 u3-l2。）

> 这个任务覆盖了：聚合位宽求和（4.1）、字段命名与 `setElementRefs`（4.2）、`val` 字段定义与 IO 克隆（4.3），以及与 u2-l2（`Bool` 取反、位宽）的衔接。

## 6. 本讲小结

- `Aggregate`（`sealed trait`）是聚合分支的根，提供「列出子元素 / 位宽求和 / 整体当字面量与 `UInt`」的公共能力；位宽是所有叶子之和，由 `foldLeft(0.W)(_ + _)` 一行算出。
- `Record`（`abstract class`）是「名字 → Data」键值聚合的基类，给库作者用；它持有 `elements: SeqMap[String, Data]`，负责名字 sanitize（`_elements`）、递归绑定（`bind`）、把字段名钉成信号引用（`setElementRefs`）以及 `cloneType`。
- `Bundle`（`abstract class extends Record`）是普通 RTL 开发者继承的类，靠 `val` 字段定义结构；字段名和 `cloneType` 都由 `chisel-plugin` 编译器插件自动实现（`_elementsImpl` / `_cloneTypeImpl`），没有插件就无法工作。
- Bundle 内部把字段**逆序**存储，配合 `asUInt`「末元素为最高位」的约定，结果是**先定义的字段位于高位**——这是 `.asUInt` 行为的根因。
- `IO(...)` 要求传入 Chisel **类型**而非硬件值，内部会 `cloneTypeFull` 克隆并 `bindIoInPlace` 登记；方向由 `Input`/`Output`/`Flipped` 打标，三者都克隆源对象。
- 常见坑：把 `Data` 塞进 `Seq` 会报错（用 `Vec`/`MixedVec`）；嵌套 Data 导致插件克隆失败会抛 `AutoClonetypeException`；同一对象被多处复用会触发别名检测。

## 7. 下一步学习建议

- **本讲只讲了「命名字段」式聚合，另一支同类型数组聚合 `Vec`/`VecInit` 留给 u2-l4**，建议紧接着读，对比「同构数组」与「异构命名字段」在 `Aggregate` 上的分工。
- **方向系统**（`SpecifiedDirection`/`ActualDirection`/`Flipped` 的精确规则、字段方向如何继承）在 u3-l2 详解，本讲的 `Input`/`Output`/`Flipped` 只是入门用法。
- **连线**：示例里的 `:=` 与 `<>` 操作符背后的 `Connectable`/`MonoConnect`/`BiConnect` 在 u3-l3、u3-l4 讲；这两篇会解释「为什么两个同类型 Bundle 能整体连」。
- **想理解「字段名到底怎么被插件收集」「`cloneType` 怎么被插件实现」**，直接跳到 u7-l1（`ChiselPlugin`/`BundleComponent`）和 u7-l2（`SourceInfo` 宏）。
- 源码阅读建议：把 [Aggregate.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala) 当作「聚合类型一站式参考」，先读三个类的 scaladoc 注释（注释里就有可运行的例子），再按 `bind` → `setElementRefs` → `cloneType` 的顺序通读 `Record`，能很快建立全局心智模型。
