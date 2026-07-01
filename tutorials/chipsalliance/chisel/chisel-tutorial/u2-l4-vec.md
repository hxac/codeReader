# Vec：硬件向量类型

## 1. 本讲目标

学完本讲后，你应该能够：

- 区分两种「构造向量」的方式：`Vec(n, gen)` 造的是**类型**（未接线的模板），`VecInit(...)` 造的是**硬件值**（已接线的线网）。
- 理解 `Vec` 与 Scala 原生 `Seq`/`Array` 的本质区别：`Vec` 是会进入电路的**硬件**，而 `Vec` 上的集合方法（如 `.map`）返回的是 Scala 软件 `Seq`。
- 掌握向量的两种索引方式：静态整数索引 `vec(2)`（编译期确定，不产生多路选择器）与动态硬件索引 `vec(sel)`（产生多路选择器，并对索引位宽做截断/越界处理）。
- 学会用 `VecInit.tabulate` / `VecInit.fill` / `VecInit.iterate` 等「生成器」批量构造向量，并看懂它们底层都汇入 `VecInit.apply`。
- 理解 `SeqUtils`（`asUInt` / `priorityMux` / `oneHotMux`）在聚合类型与向量查询中的辅助作用。

## 2. 前置知识

本讲建立在已学讲义的基础认知之上（请确保你已读过 u2-l3 Bundle），下面用通俗语言补几个本讲要用到的小概念：

- **类型 vs 硬件值**：在 Chisel 里，`UInt(8.W)` 是一个**类型**（描述「这是一个 8 位无符号数」），而 `5.U(8.W)` 是一个**硬件值**（已经绑定了一个具体常量）。区分二者是理解 `Vec` 与 `VecInit` 区别的钥匙。具体由 `binding` 字段判定（见 u4-l3）。回忆 `requireIsChiselType` / `requireIsHardware` 这两道关卡。

- **聚合类型（Aggregate）**：`Vec` 与 `Bundle` 都属于 `Aggregate` 分支——它们「由若干子元素组装而成」。`Bundle` 是「按名字」组装（异构），`Vec` 是「按下标」组装（同构：所有元素同类型）。本讲只讲同构这一支。

- **静态 vs 动态**：如果索引值在「编译/elaboration 时就能确定」（一个 Scala `Int` 字面量），就是静态的；如果索引是一个「硬件信号」（一个 `UInt`），在电路运行时才会变化，就是动态的。动态索引会综合出多路选择器（mux）硬件。

- **只登记不施工**：和 u1-l5 强调的一样，你在模块构造体里写的每一行 Chisel API 调用，本质都是向 Builder 记录一条命令，**本身不立即产生硬件文件**。本讲的索引、构造也不例外。

- **IndexedSeq**：Scala 标准库里的一个「按下标访问」的集合特质。`Vec` 复用了它，因此你能在 `Vec` 上用 `.map` / `.zip` / `.fold` 等 Scala 集合方法——但要小心返回类型（见 4.1.1）。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `core/src/main/scala/chisel3/Aggregate.scala` | 定义 `Vec` 类、`VecFactory`（`Vec.apply(n, gen)` 工厂）、`VecInit` 对象、`VecLike` 特质。本讲的主战场。 |
| `core/src/main/scala-2/chisel3/AggregateIntf.scala` | 定义 `VecIntf` / `VecInitObjIntf` / `VecLikeImpl` 接口层，用宏 `apply/do_apply` 桥接用户调用与内部实现 `_applyImpl`。 |
| `core/src/main/scala/chisel3/SeqUtils.scala` | 向量/聚合的通用辅助：`asUInt`（拼接）、`priorityMux`、`oneHotMux`、`count`。`VecLike` 的 `indexWhere` 等查询方法都依赖它。 |

一句话定位：`Aggregate.scala` 负责「Vec 是什么、怎么造、怎么连」，`AggregateIntf.scala` 是夹在用户和实现之间的「宏桥」，`SeqUtils.scala` 是 Vec 查询/拼接时调用的「工具箱」。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 Vec**：同构元素的硬件向量「类型」。
- **4.2 VecInit**：从已有硬件值「接线」出一个向量硬件值。
- **4.3 索引与 SeqUtils**：静态/动态索引的实现，以及向量查询背后的 `SeqUtils`。

### 4.1 Vec：同构元素的硬件向量

#### 4.1.1 概念说明

`Vec[T]` 表示「长度固定、元素类型相同」的硬件向量。它与 `Bundle` 是一对兄弟，都属于聚合分支 `Aggregate`：

- `Bundle` / `Record`：按**名字**组织子元素，元素可异构（字段类型不同）。
- `Vec[T]`：按**下标** `0..length-1` 组织子元素，元素必须同构（类型一致）。

一个最关键的认知是：**`Vec(n, gen)` 造出来的是「类型」，不是「值」**。它像一个「`n` 个 `gen` 摆成一排」的空槽位模板。正因为是类型，它才能被用在 `IO(...)`、`Wire(...)`、`Reg(...)` 里，声明「这个端口/线网/寄存器是一组向量」。

```scala
// 这三处的 Vec 都是「类型」角色
val in  = Input(Vec(4, UInt(8.W)))   // 一个端口：4 个 8 位输入摆成一排
val reg = Reg(Vec(4, UInt(8.W)))     // 一组 4 个寄存器
val w   = Wire(Vec(4, UInt(8.W)))    // 一组 4 根线网
```

> ⚠️ 反直觉点：`Vec` 混入了 Scala 的 `IndexedSeq[T]`（见 4.1.3 源码），所以你能对它调 `.map`、`.filter` 等 Scala 集合方法。但**这些方法返回的是 Scala 的软件 `Seq`，不是硬件 `Vec`**。例如 `vec.map(_ + 1.U)` 的结果是一个 Scala `Seq[UInt]`，要再变成硬件向量必须包一层 `VecInit(...)`（见 4.2）。这正是「Vec 是硬件、Seq 是软件」这一本质区别的体现。

#### 4.1.2 核心流程

`Vec` 类内部并不预先实例化所有元素，而是「按需」生成，核心状态有三个：

- `length: Int` —— 向量长度，构造时确定，不可变。
- `gen: => T` —— 一个**传名调用**（by-name）的元素生成器，每次求值都产出一个新的 `T`。
- `sample_element: T` —— 一个「样本元素」，用于推断 FIRRTL 类型、判断方向（flipped？）、处理长度为 0 的退化情况。

元素的真正创建发生在惰性字段 `self` 里：用 `Vector.fill(length)(gen)` 调 `length` 次 `gen`，得到 `length` 个独立元素，再给每个元素打上「我是这个 Vec 的第 i 个槽位」的引用（`setRef`）。也就是说，向量的元素是**懒构造、共享同一个父节点引用**的。

构造工厂 `Vec.apply(n, gen)` 的流程可概括为：

```
Vec(n, gen)
  → requireIsChiselType(gen)        // 校验 gen 必须是「类型」而非已接线硬件
  → new Vec(gen.cloneTypeFull, n)   // 克隆一个干净的类型作为元素生成器
```

注意它先 `cloneTypeFull` 再存进 `gen`：这是为了避免多个 Vec 意外共享同一个元素对象（那会导致命名/binding 冲突）。

#### 4.1.3 源码精读

**Vec 工厂方法**——这是 `Vec(4, UInt(8.W))` 的入口：

[core/src/main/scala/chisel3/Aggregate.scala:L166-L175](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L166-L175)

上面这段定义了 `VecFactory.apply(n, gen)`：它要求 `gen` 必须是 Chisel 类型（`requireIsChiselType`），然后用 `gen.cloneTypeFull` 构造 `new Vec(...)`。这就解释了为什么 `Vec(4, UInt(8.W))` 里 `UInt(8.W)` 必须是类型而非字面量——传 `5.U` 会被这行拒绝。

**Vec 类声明**——注意它是 `private[chisel3]` 构造器，只能通过工厂创建：

[core/src/main/scala/chisel3/Aggregate.scala:L219-L222](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L219-L222)

`Vec` 混入了 `Aggregate`、`VecIntf[T]`、`VecLike[T]`。构造参数 `gen: => T` 是传名参数（`=>`），`length` 是 `val`。注释（L262-264）解释了为什么用 `gen()` 函数而不是直接传一个 `Seq`：**强制所有元素必须同类型**，并且让 FIRRTL 生成更简单。

**惰性元素数组 `self`**——元素按需生成：

[core/src/main/scala/chisel3/Aggregate.scala:L265-L271](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L265-L271)

`Vector.fill(length)(gen)` 调 `length` 次 `gen` 造出独立元素；`Node(this)` 让所有子元素共享同一个父节点引用；`elt.setRef(thisNode, i)` 给每个元素钉上「我是第 i 个」的下标引用。注意 `self` 是 `lazy val`，只有第一次被访问（比如索引、连线、序列化）时才真正构造元素。

**样本元素 `sample_element`**：

[core/src/main/scala/chisel3/Aggregate.scala:L273-L280](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L273-L280)

注释点明了 `sample_element` 的作用：它「跟踪」元素类型，用于动态索引端口的创建和输出 FIRRTL 类型，**尤其为长度为 0 的 Vec 兜底**（长度 0 时 `self` 是空的，没有元素可看类型，只能看 `sample_element`）。

**`VecLike` 继承 `IndexedSeq`**——这是「Vec 既是硬件又是 Scala 集合」的根源：

[core/src/main/scala/chisel3/Aggregate.scala:L768-L772](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L768-L772)

`VecLike[T] extends VecLikeImpl[T] with IndexedSeq[T] with HasId`。因为混入了 `IndexedSeq[T]`，`Vec` 自动拥有 `.map` / `.zip` / `.fold` / `.head` 等方法——但它们继承自 Scala 集合，返回的是软件 `Seq`。同时这里特意覆盖了 `hashCode`/`equals`，强制走 `HasId` 的对象身份比较，避免 `IndexedSeq` 的「按内容比较」语义干扰硬件对象身份。

#### 4.1.4 代码实践

**实践目标**：亲手验证「`Vec(n, gen)` 造的是类型」，并体会「同构」「按下标」两个特性。

**操作步骤**：

1. 在一个可运行 Chisel 的环境（参考 u1-l2 用 `./mill`，或在自己的 sbt 工程里依赖已 `publishLocal` 的 chisel）里，写下面这段「示例代码」并生成 Verilog：

```scala
// 示例代码
import chisel3._
import chisel3.stage.ChiselStage

class VecRegFile extends Module {
  val io = IO(new Bundle {
    val wen   = Input(Bool())
    val waddr = Input(UInt(2.W))
    val wdata = Input(UInt(8.W))
    val rdata = Output(UInt(8.W))
  })
  // Vec 在这里扮演「类型」：声明一组 4 个 8 位寄存器
  val regs = Reg(Vec(4, UInt(8.W)))
  // 静态整数索引：regs(0) 在 elaboration 时就确定
  regs(0) := 0.U
}
// ChiselStage.emitSystemVerilog 生成 Verilog
```

2. 把 `Reg(Vec(4, UInt(8.W)))` 改成 `Reg(Vec(4, UInt(16.W)))`，重新生成 Verilog。

**需要观察的现象**：

- 生成的 Verilog 中应出现一个数组化的寄存器，例如 `reg [7:0] regs [0:3];`（具体写法取决于 firtool）。
- 静态索引 `regs(0) := 0.U` 应只对第 0 号寄存器赋值，不产生 mux。
- 把宽度从 8 改到 16 后，`regs` 的位宽随之变成 `[15:0]`，体现「同构 + 以 `gen` 为模板」。

**预期结果**：Verilog 中 `regs` 是 4 份相同位宽的寄存器；`regs(0)` 的写入是静态选定，没有选择器。**待本地验证**：若你的环境尚未配好 firtool，可先只看 CHIRRTL（`ChiselStage.emitCHIRRTL`），其中 `reg [7:0] regs [0:3]` 形态更直观。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Vec` 的构造参数 `gen` 用 `=> T`（传名）而不是 `T`？

**参考答案**：传名参数让每次访问元素时都能**重新调用 `gen` 生成一个全新的、独立的 `T` 对象**。如果直接传一个 `T` 值，`Vector.fill(length)(gen)` 会把「同一个对象」重复塞进每个槽位，导致所有元素共享同一份 binding/引用，命名与连线都会错乱。`self`（L265-271）正是靠反复调用 `gen()` 拿到 `length` 个独立元素。

**练习 2**：下面这段代码能编译吗？为什么？

```scala
val v = Vec(4, 5.U(8.W))   // 注意：5.U 是字面量硬件值
```

**参考答案**：不能（会在 elaboration 期报错）。`Vec.apply` 的第一行 `requireIsChiselType(gen, "vec type")` 要求 `gen` 必须是 Chisel **类型**，而 `5.U(8.W)` 是已绑定的硬件字面量。正确写法是 `Vec(4, UInt(8.W))`（类型），再用 `VecInit` 填值。

**练习 3**：`Vec(0, UInt(8.W))`（长度为 0 的向量）合法吗？它的类型信息从哪来？

**参考答案**：合法。虽然 `self` 是空数组，但 `sample_element`（L280）单独保存了一个样本元素，FIRRTL 类型和方向判断都看它，因此长度为 0 的 Vec 不会丢失类型信息。

---

### 4.2 VecInit：从已有硬件值构造向量

#### 4.2.1 概念说明

`Vec` 造「类型」，而 `VecInit` 造「**硬件值**」——它把若干**已经存在的硬件信号**摆成一个向量，并自动接好线。当你手里已经有了一批信号（常量、端口、运算结果），想把它们「打包成向量」用于索引或拼接，就用 `VecInit`。

```scala
val v = VecInit(0.U, 1.U, 4.U, 9.U)   // 4 个平方数常量摆成向量
val sel = io.addr
io.out := v(sel)                       // 动态查表
```

与 `Vec(n, gen)` 的对比要牢记：

| | `Vec(n, gen)` | `VecInit(elts)` |
| --- | --- | --- |
| 输入 | 元素**类型** `gen` | 元素**硬件值**序列 `elts` |
| 产物 | Vec **类型**（空槽位） | Vec **硬件值**（已接线 Wire） |
| 典型用途 | `IO` / `Reg` / `Wire` 的类型声明 | 把已有信号打包、查表、`asUInt` 拼接 |
| 元素宽度 | 由 `gen` 决定 | 取所有输入中**最宽**的那个（见下） |

`VecInit` 还提供了一族「生成器」方法，模仿 Scala 集合 API：

- `VecInit.tabulate(n)(i => ...)` —— 按下标生成（类比 `Seq.tabulate`）。
- `VecInit.fill(n)(gen)` —— 重复填同一个值（注意 `gen` 是传名，每次重新求值）。
- `VecInit.iterate(start, len)(f)` —— 用递推函数 `f` 链式生成。
- 还有 2D / 3D 的 `tabulate(n, m)` / `fill(n, m, p)` 重载。

#### 4.2.2 核心流程

`VecInit.apply(elts)` 的本质是「**造一个 Wire 类型的 Vec，再把输入逐个连过去**」，流程：

```
VecInit(elts)
  → require(elts 非空)                         // 不允许空向量
  → elts.foreach(requireIsHardware)            // 每个元素必须是硬件值
  → vec = Wire(Vec(elts.length, 共同父类型))    // 推断类型：取共同父类型，宽度取最大
  → for ((lhs,rhs) <- vec.zip(elts)) lhs :<>= rhs  // 逐元素连接
  → 返回 vec
```

两个关键设计：

1. **类型推断用「共同父类型」**：`cloneSupertype(elts, "Vec")` 会找出所有 `elts` 的公共类型，并把宽度设为「最宽元素的宽度」。所以 `VecInit(1.U(4.W), 7.U(8.W))` 得到的是 `Vec[UInt]`，每个槽位 8 位。
2. **逐元素用 `:<>=` 连接**：`:<>=` 是「强连」（chisel3._ 里单向、忽略方向的版本），把每个输入信号驱动到对应槽位。也就是说 `VecInit` **不是零开销的别名**，它在电路上插了一根 Wire 和一组连线（不过 firtool 通常会优化掉冗余连线）。

所有「生成器」方法（`tabulate`/`fill`/`iterate`）最终都**汇入** `apply`——它们只是先用 Scala 集合方法（`Seq.tabulate` / `Seq.fill` / `Seq.iterate`）算出一个 `Seq[T]`，再交给 `_applyImpl`。

#### 4.2.3 源码精读

**`VecInit.apply` 的实现 `_applyImpl`**：

[core/src/main/scala/chisel3/Aggregate.scala:L651-L669](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L651-L669)

这段是 `VecInit` 的「心脏」。`require(elts.nonEmpty, ...)` 拒绝空向量；`requireIsHardware` 保证每个输入都已接线；接着 `Wire(Vec(elts.length, cloneSupertype(elts, "Vec")))` 造出目标向量 Wire；最后 `lhs :<>= rhs` 逐元素连接。注意 `cloneSupertype` 决定了「宽度取最大」的规则。

**变长参数重载**——让 `VecInit(a, b, c)` 这种写法成立：

[core/src/main/scala/chisel3/Aggregate.scala:L671-L672](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L671-L672)

它把 `elt0 +: elts.toSeq` 拼成一个 `Seq` 后转交给上面的 `_applyImpl(Seq)`。

**`tabulate` 的实现**——证明「生成器只是 `apply` 的语法糖」：

[core/src/main/scala/chisel3/Aggregate.scala:L674-L679](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L674-L679)

`_tabulateImpl(n)(gen)` 一行：`(0 until n).map(i => gen(i))` 算出元素 `Seq`，立刻交给 `_applyImpl`。2D 版本（L681-703）同理，只是构造 `Vec(n, Vec(m, tpe))` 并双层 `zip` 连接。

**`fill` 与 `iterate`**：

[core/src/main/scala/chisel3/Aggregate.scala:L733-L735](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L733-L735)

[core/src/main/scala/chisel3/Aggregate.scala:L756-L762](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L756-L762)

`fill` 对 `n==0` 做了特判（造一个长度 0 的 Wire Vec），否则 `Seq.fill(n)(gen)` 后交 `_applyImpl`。注意 `gen` 是传名的，所以 `VecInit.fill(3)(Wire(UInt(8.W)))` 会创建 3 个**独立**的 Wire，而不是共享同一个。

**用户层入口是宏**：用户写的 `VecInit(elts)` 其实经过宏 `apply` → `do_apply` → `_applyImpl`。宏只为隐式注入源信息（文件名/行号），核心逻辑都在 `_applyImpl`：

[core/src/main/scala-2/chisel3/AggregateIntf.scala:L52-L55](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala-2/chisel3/AggregateIntf.scala#L52-L55)

#### 4.2.4 代码实践

**实践目标**：用三种方式（直接 `apply`、`tabulate`、`fill`）构造向量，并验证「宽度取最大」「空向量被拒绝」两条规则。

**操作步骤**：

1. 写下面这段「示例代码」并生成 CHIRRTL（更易读）：

```scala
// 示例代码
import chisel3._
import chisel3.stage.ChiselStage

class VecInitDemo extends Module {
  val io = IO(new Bundle {
    val out = Output(UInt(8.W))
  })
  // 方式 A：显式列出常量
  val a = VecInit(1.U(4.W), 7.U(8.W))     // 期望：Vec[UInt]，宽度 8
  // 方式 B：tabulate 按下标生成
  val b = VecInit.tabulate(4)(i => (i * i).U(8.W))  // 0,1,4,9
  // 方式 C：fill 填同一个表达式
  val c = VecInit.fill(3)(0.U(8.W))
  io.out := a(1) + b(2) + c(0)
}
// ChiselStage.emitCHIRRTL(new VecInitDemo)
```

2. 再尝试一个**故意失败**的写法，观察报错：`VecInit()`（空参数）。

**需要观察的现象**：

- 方式 A 的 `a` 类型是 `Vec[UInt]`，且因为 `7.U(8.W)` 比 `1.U(4.W)` 宽，整体宽度是 8 位（体现「宽度取最大」）。
- 方式 B 的 `b` 是 4 个常量 `0,1,4,9`。
- 方式 C 的 `c` 是 3 个 0。
- `VecInit()` 应抛出 `require` 失败：「Vec hardware values are not allowed to be empty」。

**预期结果**：CHIRRTL 中能看到 `wire a : UInt<8>[2]` 之类的声明，以及把常量连进去的节点。空 `VecInit()` 报上述错误。**待本地验证**：CHIRRTL 文本里常量与 wire 的具体命名。

#### 4.2.5 小练习与答案

**练习 1**：`VecInit(1.U(4.W), 7.U(8.W))(1)` 的值和位宽分别是什么？

**参考答案**：值是 `7`，位宽是 8。`cloneSupertype` 取共同父类型 `UInt` 并把宽度设为「最宽输入」即 8 位；整个 `Vec` 每个槽位都是 8 位，索引第 1 号得到那个 8 位的 `7`。

**练习 2**：`VecInit.fill(3)(Wire(UInt(8.W)))` 和「先 `val w = Wire(UInt(8.W))` 再 `VecInit.fill(3)(w)`」在电路上有何不同？

**参考答案**：前者 `gen` 是传名表达式，`Seq.fill(3)(gen)` 会求值 3 次，创建 **3 个独立的 Wire**；后者三次都引用**同一个** Wire 对象 `w`，于是 `VecInit` 把同一个信号连到 3 个槽位（电路等价于 3 个槽位都驱动自 `w`）。前者多出冗余 Wire，但语义独立；后者共享信号。firtool 通常都会优化成等价结果。

**练习 3**：为什么不直接让用户写 `Vec(0.U, 1.U, 4.U)` 来造值向量，而要单独发明 `VecInit`？

**参考答案**：因为 `Vec(n, gen)` 的契约是「造**类型**」，第一行就用 `requireIsChiselType` 拒绝硬件值；它的设计目标是给 `IO/Reg/Wire` 当类型模板。造「值向量」需要逐元素连线（`:<>=`）和宽度推断，语义完全不同，所以单独用 `VecInit` 承担，二者职责分离、避免混淆。

---

### 4.3 索引：静态 vs 动态，以及 SeqUtils 辅助

#### 4.3.1 概念说明

有了 `Vec`，自然要「取第 i 个元素」。Chisel 提供两种重载的 `apply`：

- **静态索引** `vec(i: Int)`：`i` 是 Scala `Int`，在 elaboration 时就定死。它只是取出 `self` 数组里的第 i 个元素，**不产生任何选择器硬件**，相当于「直接把这根线引出来」。
- **动态索引** `vec(sel: UInt)`：`sel` 是硬件 `UInt`，电路运行时才变化。它会综合出一个**多路选择器**（mux），根据 `sel` 选通对应元素。

| | `vec(2)`（静态） | `vec(sel)`（动态） |
| --- | --- | --- |
| 参数类型 | Scala `Int` | 硬件 `UInt` |
| 决定时机 | elaboration 期 | 电路运行时 |
| 产物 | 直接取元素，无 mux | 一个 mux，`sel` 作选择信号 |
| 越界 | Scala 数组越界异常 | 自动**截断/回绕**（见下） |

动态索引有个贴心的安全网：**索引位宽检查与截断**。如果你给一个长度 4 的 Vec 传一个 4 位的索引（能表示 0..15），Chisel 会发警告说「索引太宽」，并按「2 的幂取模」把索引截断到需要的位数。对长度 `n` 的 Vec，需要的索引位数是：

\[
w = \mathrm{bitLength}(n-1)
\]

例如 `n = 4` 时 `n-1 = 3`，`3.bitLength = 2`，所以索引被截到 2 位。这意味着传索引 `7`（二进制 `111`）会被截成 `11` = `3`，即 **7 mod 4 = 3**，实现了「对 2 的幂长度的回绕寻址」。

此外，`VecLike` 还提供了一族**硬件查询方法**，它们都建立在 `SeqUtils` 之上：

- `indexWhere(p)` —— 第一个满足 `p` 的下标（底层 `SeqUtils.priorityMux`）。
- `lastIndexWhere(p)` —— 最后一个满足 `p` 的下标。
- `onlyIndexWhere(p)` —— 假设恰有一个满足时的高效版（底层 `SeqUtils.oneHotMux`）。
- `count(p)` —— 满足 `p` 的元素个数（底层 `SeqUtils.count`）。
- `exists(p)` / `forall(p)` —— 是否存在 / 是否全满足（用 `||` / `&&` 折叠）。

而把一个 `Vec[Bits]` 「拼接成一个 `UInt`」（高位在下标大的一端）由 `SeqUtils.asUInt` 完成，`Aggregate.asUInt` 内部就调它。

#### 4.3.2 核心流程

**静态索引** `vec(i)` 直接走 `Vec.apply(idx: Int)`：

```
vec(i: Int)  →  self(i)   // self 是 Vector.fill(length)(gen)，纯 Scala 取下标
```

**动态索引** `vec(sel)` 走宏 `apply` → `do_apply` → `_applyImpl`，流程：

```
vec(sel: UInt)
  → 若 sel 是字面量且 < length：直接转成静态 apply（提前返回，省掉 mux）
  → 否则做位宽检查（太宽/太窄发 Warning）
  → port = gen                              // 新建一个结果元素
  → port.bind(DynamicIndexBinding(this))    // 标记「我是动态索引结果」
  → i = Vec.truncateIndex(sel, length)      // 截断索引到位长 bitLength(length-1)
  → port.setRef(Node(this), i)              // 记录「选 this 的第 i 个」
  → 返回 port
```

**截断函数 `truncateIndex`** 的分支：

\[
\text{截断后} =
\begin{cases}
0 & n \le 1 \\
\text{sel} & \text{sel 位宽} \le w \\
\text{sel}[w-1:0] & \text{sel 位宽已知且} > w \\
(\text{sel} \,|\, 0)[w-1:0] & \text{sel 位宽未知}
\end{cases}
\]

其中 \( w = \mathrm{bitLength}(n-1) \)。`n <= 1` 时返回 `WireInit(0.U)`，因为「vec[0]」在 FIRRTL 里是非法的，必须用一个常量 0 绕开。

#### 4.3.3 源码精读

**静态索引 `apply(idx: Int)`**——就一行：

[core/src/main/scala/chisel3/Aggregate.scala:L426-L428](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L426-L428)

`def apply(idx: Int): T = self(idx)`。纯 Scala 数组取下标，不登记任何 mux 命令。

**动态索引核心 `_applyImpl(p: UInt)`**：

[core/src/main/scala/chisel3/Aggregate.scala:L361-L386](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L361-L386)

前半段：先尝试「字面量短路」（L367-370）——如果 `p` 是编译期常量且小于 `length`，直接转静态 `apply`，省掉 mux。否则（L372-386）对非零长度做位宽检查，太宽/太窄都发对应的 `Warning`（`WarningID.DynamicIndexTooWide` / `DynamicIndexTooNarrow`）。长度为 0 则发 `ExtractFromVecSizeZero` 警告。

[core/src/main/scala/chisel3/Aggregate.scala:L402-L424](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L402-L424)

后半段：新建结果元素 `port = gen`（L402），用 `DynamicIndexBinding(this)` 绑定（L418）——这正是 u4-l3 要讲的 binding 家族之一，标记「这个值是某个 Vec 的动态索引结果」；`Vec.truncateIndex(p, length)` 截断索引（L420）；最后 `port.setRef(Node(this), i)` 把「选 `this` 的第 `i` 个」记进引用（L421），后续 Converter 会据此生成 mux。

**索引截断 `truncateIndex`**：

[core/src/main/scala/chisel3/Aggregate.scala:L177-L190](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L177-L190)

注意 `val w = (n - 1).bitLength`：对长度 `n` 的 Vec，索引只需 `bitLength(n-1)` 位。`n <= 1` 时返回 `WireInit(0.U)`（避免非法的 `vec[0]`）。其余分支按 sel 位宽是否已知分别截到 `w` 位。

**用户层宏入口**——`apply(p: UInt)` 是宏，转发到 `do_apply`：

[core/src/main/scala-2/chisel3/AggregateIntf.scala:L198-L203](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala-2/chisel3/AggregateIntf.scala#L198-L203)

**`VecLike` 的查询方法**——都委托 `SeqUtils`：

[core/src/main/scala/chisel3/Aggregate.scala:L786-L799](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L786-L799)

`_indexWhereImpl` 用 `SeqUtils.priorityMux`（取第一个真），`_onlyIndexWhereImpl` 用 `SeqUtils.oneHotMux`（独热选择，更高效但要求恰一个真）。`indexWhereHelper` 把「谓词结果」和「下标」配对成 `(Bool, UInt)` 序列喂给 mux。

**`SeqUtils.priorityMux`**——「第一个真」选择器：

[core/src/main/scala/chisel3/SeqUtils.scala:L64-L78](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/SeqUtils.scala#L64-L78)

它把序列反转后用 `foldLeft` 嵌套 `Mux`：靠前的 `(sel, elt)` 优先级更高（`Mux(sel, elt, alt)`），从而实现「第一个真胜出」。

**`SeqUtils.asUInt`**——把 `Vec[Bits]` 拼成 `UInt`：

[core/src/main/scala/chisel3/SeqUtils.scala:L25-L50](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/SeqUtils.scala#L25-L50)

第一个元素是最低位、最后一个元素是最高位（L22-23 注释）。它累加各元素宽度，用 `ConcatOp`（`##`）反向拼接（`args.reverse`，因为 FIRRTL cat 的约定是「先写的是高位」）。空序列返回 `0.U`。这正是 `Aggregate._asUIntImpl`（L108-117）和 `asUInt` 调用的底层。

#### 4.3.4 代码实践

**实践目标**：实现本讲规格要求的「4 选 1 多路选择器」，观察动态索引如何变成 mux，并验证索引截断行为。

**操作步骤**：

1. 写下面这段「示例代码」，用 `ChiselStage.emitSystemVerilog` 生成 Verilog：

```scala
// 示例代码
import chisel3._
import chisel3.stage.ChiselStage

class Mux4 extends Module {
  val io = IO(new Bundle {
    val in  = Input(Vec(4, UInt(8.W)))   // 4 个 8 位输入
    val sel = Input(UInt(2.W))           // 2 位选择信号
    val out = Output(UInt(8.W))
  })
  io.out := io.in(io.sel)                // 动态索引
}
// 打印生成的 Verilog
// println(ChiselStage.emitSystemVerilog(new Mux4))
```

2. 把 `sel` 的宽度从 `2.W` 改成 `4.W`，重新生成，留意控制台的 **Warning**。

3. 对照练习：把 `io.in(io.sel)` 换成静态 `io.in(2)`，再生成一次。

**需要观察的现象**：

- 用 `2.W` 的 `sel` 时，Verilog 里应出现一个 4 选 1 选择逻辑（综合后通常是 `case` 或嵌套三元/与或树），`sel` 作选择信号，没有越界警告。
- 把 `sel` 改成 `4.W` 时，控制台应打印类似「Dynamic index with width 4 is too wide for Vec of size 4 (expected index width 2)」的警告，且生成的选择逻辑只用到 `sel` 的低 2 位。
- 静态 `io.in(2)` 则**不产生选择器**，`out` 直接被第 2 号输入驱动。

**预期结果**：动态索引版本综合出 4 选 1 mux；静态索引版本无 mux、直连。**待本地验证**：firtool 输出的具体 Verilog 写法（`case` / `assign ... ? ... : ...` / 与或门）。

#### 4.3.5 小练习与答案

**练习 1**：对长度为 4 的 Vec，索引位宽期望是几位？为什么传 4 位索引会触发「too wide」警告但仍能工作？

**参考答案**：期望 2 位，因为 `w = bitLength(4-1) = bitLength(3) = 2`。传 4 位索引能表示 0..15，超过 0..3 的有效范围，所以警告「太宽」。但仍能工作，因为 `truncateIndex` 会把索引截到低 2 位（L189 的 `idx(w-1, 0)`），实现 `index mod 4` 的回绕寻址。

**练习 2**：`vec.indexWhere(_ === x)` 和 `vec.onlyIndexWhere(_ === x)` 在底层用了什么？有何差别？

**参考答案**：`indexWhere` 用 `SeqUtils.priorityMux`（L792），按优先级取「第一个满足」的下标，多个满足时返回最前的；`onlyIndexWhere` 用 `SeqUtils.oneHotMux`（L798），假设恰有一个满足，用独热与或树实现，更高效但若不止一个满足则结果未定义。二者都不检查「是否真的恰有一个」。

**练习 3**：`VecInit(1.U, 2.U, 3.U).asUInt` 的值是多少？依据是哪段源码？

**参考答案**：值是 `0b11_10_01 = 0x2D`（十进制 45），因为 `SeqUtils.asUInt`（L22-23、L48）规定「第一个元素是最低位、最后是最高位」，1 在最低位、3 在最高位，拼成 `0b111001`。注意此处宽度取最大为每位 1 位宽（常量默认宽度）。

---

## 5. 综合实践

**任务**：实现一个 **4 项 × 8 位寄存器堆（register file）**，把本讲三个最小模块（`Vec` 类型、`VecInit` 造值、动态索引）串起来。

**要求**：

- 用 `Reg(Vec(4, UInt(8.W)))` 作为存储体（`Vec` 当类型用，4.1）。
- 一个写端口：`wen` + `waddr`（动态索引写入，4.3）+ `wdata`。
- 一个读端口：`raddr`（动态索引读出，4.3）→ `rdata`。
- 用 `VecInit` 给寄存器堆一个**上电初值**：复位时把 4 个寄存器分别初始化为 `0,1,4,9`（用 `VecInit.tabulate(4)(i => (i*i).U(8.W))`，4.2），再用 `RegInit` 加载。

**参考骨架（示例代码）**：

```scala
// 示例代码
import chisel3._
import chisel3.stage.ChiselStage

class RegFile4 extends Module {
  val io = IO(new Bundle {
    val wen   = Input(Bool())
    val waddr = Input(UInt(2.W))
    val wdata = Input(UInt(8.W))
    val raddr = Input(UInt(2.W))
    val rdata = Output(UInt(8.W))
  })
  // VecInit.tabulate 造初值向量，RegInit 加载进 Reg(Vec(...))
  val init = VecInit.tabulate(4)(i => (i * i).U(8.W))   // 0,1,4,9
  val regs = RegInit(init)                              // Vec 类型 + 初值硬件值结合
  // 动态索引写
  when(io.wen) {
    regs(io.waddr) := io.wdata
  }
  // 动态索引读
  io.rdata := regs(io.raddr)
}
// println(ChiselStage.emitSystemVerilog(new RegFile4))
```

**观察与思考**：

1. 在 Verilog 里找到那 4 个寄存器（数组形态 `reg [7:0] regs [0:3];`）。
2. 确认写端口是一个「带 `wen` 使能、按 `waddr` 选定目标」的逻辑（动态索引写）。
3. 确认读端口是一个 4 选 1 mux（动态索引读）。
4. 确认复位逻辑把初值 `0,1,4,9` 写进寄存器（来自 `VecInit.tabulate`）。

**预期结果**：一个功能完整的 4 项寄存器堆，写时按址写入、读时按址读出、复位有初值。这一任务同时用到了 `Vec`（类型）、`VecInit.tabulate`（造初值硬件值）、动态索引（读写选址），把三个最小模块融为一例。**待本地验证**：生成的 Verilog 中寄存器数组、写使能、读 mux 的具体形态。

> 进阶：把容量参数化为 `class RegFile(n: Int, w: Int)`，注意 `waddr`/`raddr` 的位宽要随 `n` 调整为 `log2Ceil(n).W`，并观察动态索引的位宽检查警告是否消失。

## 6. 本讲小结

- **两种构造要分清**：`Vec(n, gen)` 造的是**类型**（用于 `IO`/`Reg`/`Wire` 声明），`VecInit(elts)` 造的是**硬件值**（把已有信号接线成向量）；二者职责分离，不可混用。
- **`VecInit` 是「Wire + 逐元素连接」的语法糖**：核心 `_applyImpl`（Aggregate.scala L651-669）造一个 Wire Vec 再 `:<>=` 连线，宽度取所有输入的「最大值」；`tabulate`/`fill`/`iterate` 最终都汇入它。
- **静态索引无 mux，动态索引有 mux**：`vec(i: Int)` 直接取 `self(i)`；`vec(sel: UInt)` 经 `_applyImpl` 绑定 `DynamicIndexBinding` 并登记选择引用，综合出多路选择器。
- **动态索引有安全网**：`truncateIndex` 把索引截到 `bitLength(n-1)` 位，实现 2 的幂回绕寻址；位宽不匹配会发 `Warning`（但不阻断）。
- **Vec 既是硬件又是 Scala 集合**：`VecLike` 混入 `IndexedSeq[T]`，所以 `.map` 等方法可用，但返回的是软件 `Seq`，要变回硬件向量需包 `VecInit`。
- **查询与拼接走 `SeqUtils`**：`indexWhere` 用 `priorityMux`、`onlyIndexWhere` 用 `oneHotMux`、`asUInt` 用 `ConcatOp` 拼接（首位在低位）。

## 7. 下一步学习建议

- **横向**：Chisel 类型系统还有一类「带命名常量的枚举」`ChiselEnum`（见 u2-l5），常与 `Vec` 配合做状态机的 `switch/is` 编码，建议接着读。
- **纵向（模块层）**：`Vec` 最常出现在模块 IO 和寄存器里，下一步进入 u3-l1（Module 生命周期）和 u3-l2（IO 与方向），看 `Vec` 如何作为端口被 `Input`/`Output` 包装、如何在 `<>` 批量连线中按序匹配。
- **深入内部**：本讲反复提到的 `DynamicIndexBinding`、`LitBinding`、`ChildBinding` 属于 binding 系统，建议读 u4-l3（Binding 系统）彻底搞清「类型 vs 硬件值」的内部表示。
- **标准库实战**：`chisel3.util` 的 `Mux1H`/`MuxLookup`（u6-l4）和 `Queue`（u6-l2）大量使用 `Vec` 与 `VecInit`，是巩固本讲的好材料。
