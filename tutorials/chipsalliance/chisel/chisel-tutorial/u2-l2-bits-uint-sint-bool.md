# Bits / UInt / SInt / Bool 数值类型

## 1. 本讲目标

本讲是「数据类型系统」单元（单元 2）的第二篇，紧接 u2-l1 讲完的「`Data` 类型骨架」。u2-l1 只画到了 `Element`（叶子分支基类）这一层；本讲往下走一层，**把硬件里最常用的数值类型一次性讲透**。学完本讲，你应当能够：

- 说清楚 `Bits` 是什么：`UInt` 与 `SInt` 的共同父类，代表「二进制位向量」，并解释为什么 `Bits` 的伴生对象是一个 `UIntFactory`。
- 用 `UInt` / `SInt` / `Bool` 写出常见的算术运算（`+ - * / %`）、位运算（`& | ^ ~`）、移位（`<< >>`）和比较（`< > == =/=`），并能**预测每个运算结果的位宽**。
- 解释 Chisel 位宽推断（width inference）的核心规则：乘法「位宽相加」、加法「取最大」、带进位加法 `+&`「取最大再加一」、未知位宽会「感染」传播。
- 说清楚 `Bool` 的特殊地位：它**继承自 `UInt`**（`Bool extends UInt(1.W)`），所以能直接用 `UInt` 的位运算。
- 看懂 `Num[T]` 这个数值抽象提供了哪些「与符号无关」的通用操作（`abs` / `min` / `max` / 比较）。
- 在源码层面追踪一条完整的运算调用链：`a + b`（用户 API）→ 宏改写 → `do_+` → `_impl_+` → `binop` → `pushOp(DefPrim(...))`，理解「运算符只是登记一条 `PrimOp` 命令」。

本讲只讲**叶子数值类型**；把多个信号「打包」的聚合类型（`Bundle` / `Vec`）留到 u2-l3、u2-l4。

## 2. 前置知识

阅读本讲前，你应当已经具备（来自单元 1 和 u2-l1）：

- **Chisel 是嵌在 Scala 里的硬件构造 DSL**：你写的是合法 Scala 代码，运行时 elaboration 才「长出」电路（见 u1-l1、u1-l5）。
- **「只登记不施工」**：模块构造体里每写一个运算符，只是向 `Builder` 的命令队列追加一条命令（`pushOp` / `pushCommand`），本身不产生硬件（见 u1-l5）。
- **`Data` 类型骨架**：`Data` 是根，往下分 `Element`（叶子）与 `Aggregate`（聚合）；`Bits` 经 `BitsIntf` / `ToBoolable` 间接继承 `Element`（见 u2-l1）。
- **类型 vs 硬件值**：同一个 `UInt(8.W)`，写在 `IO(...)` / `Wire(...)` 里是「硬件值」，直接当模板用是「纯类型」，由 `binding` 字段区分；运算符只接受硬件值（`requireIsHardware`）（见 u2-l1）。
- **`Width` 是和类型**：只有 `KnownWidth(n)` 与 `UnknownWidth` 两种，未知位宽在运算里会「感染」传播（见 u2-l1）。
- **字面量 `.U` / `.S` / `.B` / `.W`**：由 `chisel3` 包对象的隐式类提供（见 u1-l4），本讲 4.5 会下钻到它的源码。

本讲会用到的一个 Scala 概念，先一句话解释：

- **伴生对象（companion object）**：与某个类同名、写在同一个文件里的 `object`。类是「每个实例的模板」，伴生对象是「跟这个类相关的静态工厂 / 常量」。在 Chisel 里，写 `UInt(8.W)` 调用的其实是**伴生对象 `object UInt` 的 `apply` 方法**，它 `new` 出一个 `UInt` 实例返回。所以 `UInt` 这个名字既是「类型」，也是「工厂」。

## 3. 本讲源码地图

本讲涉及的关键文件，都在 `core` 子项目里（关于 `core` 与其他子项目的划分，见 u1-l3）：

| 文件 | 作用 |
| --- | --- |
| [core/src/main/scala/chisel3/Bits.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Bits.scala) | **本讲主战场**：`Bits` 抽象基类、`UInt` / `SInt` / `Bool` 三个具体类，以及所有运算符的真正实现 `_impl_*` 和位宽推断规则。 |
| [core/src/main/scala/chisel3/Num.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Num.scala) | 数值抽象 `Num[T]` 与 `NumIntf[T]`，提供与符号无关的通用操作（`abs` / `min` / `max` / 比较）。 |
| [core/src/main/scala/chisel3/UIntFactory.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/UIntFactory.scala) | `UInt` 的工厂 trait：`apply()` / `apply(width)` 造类型，`Lit(...)` 造字面量。`Bits` 与 `UInt` 共用它。 |
| [core/src/main/scala/chisel3/SIntFactory.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/SIntFactory.scala) | `SInt` 的工厂 trait：与 `UIntFactory` 结构对称。 |
| [core/src/main/scala/chisel3/BoolFactory.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/BoolFactory.scala) | `Bool` 的工厂 trait：`apply()` 造空 `Bool`，`Lit(x: Boolean)` 造布尔字面量。 |
| [core/src/main/scala/chisel3/package.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/package.scala) | `chisel3` 包对象，提供 `.U` / `.S` / `.B` / `.W` 等字面量隐式转换。 |
| [core/src/main/scala/chisel3/Width.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Width.scala) | 位宽抽象 `KnownWidth` / `UnknownWidth`，位宽推断的「算术」就发生在这里的 `+` / `max` / `min`。 |
| core/src/main/scala-2/chisel3/NumIntf.scala、BitsIntf.scala | Scala 2 下的**用户可见运算符声明**（`final def + = macro ...`）与 `do_*` 桥接方法。它们是「宏入口」，由宏改写到 `Bits.scala` 里的 `_impl_*`。 |

> 小贴士：为什么运算符分在两个地方？用户写 `a + b` 调用的 `+` 定义在 `NumIntf` / `BitsIntf`（`scala-2` 目录，依赖 Scala 宏，所以按 Scala 大版本分目录）；真正干活的 `_impl_+` 在 `Bits.scala`（与 Scala 版本无关）。这条「宏壳子 + 实现」的分层是本讲 4.2 的重点。

## 4. 核心概念与源码讲解

### 4.1 Bits：位向量基类

#### 4.1.1 概念说明

数字电路里，绝大多数信号本质都是「一串二进制位」。Chisel 用 `Bits` 来抽象「**二进制位向量（bit vector）**」这一概念：它有一个位宽 `width: Width`，是 `UInt`（无符号）和 `SInt`（有符号）的共同父类。

一个反直觉但很重要的点：**`Bits` 本身不区分有无符号**。它只提供「对所有位向量都成立」的操作——位提取、拼接、移位、按位逻辑运算的公共骨架。至于「这一串位如何被解释成数字」（要不要看最高位当符号位），是子类 `UInt` / `SInt` 各自的规定。源码注释说得很直白：

[core/src/main/scala/chisel3/Bits.scala:15-17](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Bits.scala#L15-L17) —— `Bits` 是 `UInt` 和 `SInt` 的父类型，且 `Bits` 工厂方法返回的是 `UInt`：

```scala
/** Data type for binary bit vectors
  * The supertype for [[UInt]] and [[SInt]]. Note that the `Bits` factory method returns `UInts`.
  */
```

注意最后一句：`object Bits extends UIntFactory`。也就是说，**写 `Bits(8.W)` 得到的是一个 `UInt`**（因为「不带符号信息的裸位向量」默认按无符号处理）。这是初学者容易踩的坑——以为 `Bits` 是个能直接做算术的中性类型，其实它的工厂就是个 `UInt` 工厂。

#### 4.1.2 核心流程

`Bits` 提供三类「位级别」能力的实现骨架，运算最终都落到一组私有 helper 方法上，由它们把运算翻译成一条 `PrimOp`（原始操作）命令塞进命令队列：

1. **位提取 / 切片**：`extract(i)` 取第 `i` 位返回 `Bool`；`apply(hi, lo)` 取一段返回 `UInt`；`head(n)` / `tail(n)` 取高位 / 去高位。
2. **位拼接**：`##` 把两个位向量首尾相连，宽度为两者之和。
3. **类型转换骨架**：`asSInt` / `asBool` / `asBools`，在不改变底层位的前提下重新解释类型。

这些操作真正的「登记」动作，都收敛到 `Bits` 内部的四个 helper——`unop` / `binop` / `compop` / `redop`。它们的名字暗示了运算的「形状」：

- `unop`（一元）：1 个操作数，如按位取反 `~`。
- `binop`（二元）：2 个操作数，如 `& | ^ + *`。
- `compop`（比较）：2 个操作数，**结果一定是 `Bool`**，如 `< > ==`。
- `redop`（归约）：1 个操作数，把所有位归约成 1 个 `Bool`，如 `orR` / `andR`。

它们共同的终点是 `pushOp(DefPrim(sourceInfo, dest, op, args*))`：构造一个 `DefPrim` 命令（「定义一个原始运算节点」）压入队列。这正是 u1-l5 反复强调的「**只登记不施工**」——你在 Scala 里写的每一个运算符，到这里都变成一条 IR 命令。

#### 4.1.3 源码精读

先看 `Bits` 的类声明——它是 **`sealed abstract class`**，持有一个位宽字段：

[core/src/main/scala/chisel3/Bits.scala:24](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Bits.scala#L24) —— `Bits` 继承 `BitsIntf`，位宽以 `Width` 类型持有：

```scala
sealed abstract class Bits(private[chisel3] val width: Width) extends BitsIntf {
```

- `sealed`：所有子类（`UInt` / `SInt`）必须写在同一文件（见 u2-l1 对 `sealed` 的解释）。
- `private[chisel3] val width`：位宽字段对 `chisel3` 包内可见，正是位宽推断读取的对象。

再看四个 helper——它们是所有运算的「最后一步」。[core/src/main/scala/chisel3/Bits.scala:161-177](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Bits.scala#L161-L177)：

```scala
private[chisel3] def binop[T <: Data](sourceInfo, dest: T, op: PrimOp, other: Bits): T = {
  requireIsHardware(this, "bits operated on")
  requireIsHardware(other, "bits operated on")
  pushOp(DefPrim(sourceInfo, dest, op, this.ref, other.ref))
}
private[chisel3] def compop(sourceInfo, op: PrimOp, other: Bits): Bool = {
  requireIsHardware(this, "bits operated on")
  requireIsHardware(other, "bits operated on")
  pushOp(DefPrim(sourceInfo, Bool(), op, this.ref, other.ref))
}
```

读这段要抓住三个要点：

1. **`requireIsHardware`**：运算符只接受「硬件值」。你拿一个纯类型 `UInt(8.W)` 去做 `+`，会在这里被拒——对应 u2-l1 讲的「类型 vs 硬件值」检查。
2. **`dest: T`**：结果类型由调用方传入。这正是位宽推断发生的地方——调用方先用位宽规则算出一个「带正确位宽的结果类型」，再当作 `dest` 传进来（见 4.2.3）。
3. **`pushOp(DefPrim(...))`**：把运算登记成 `DefPrim` 命令。`this.ref` / `other.ref` 是两个操作数在 IR 里的引用。这一行就是「登记」动作本身，没有任何硬件被生成。

最后看 `Bits` 的伴生对象：[core/src/main/scala/chisel3/Bits.scala:229](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Bits.scala#L229) —— `object Bits extends UIntFactory`，证实了「`Bits` 工厂就是 `UInt` 工厂」。

#### 4.1.4 代码实践

**实践目标**：亲手验证「`Bits` 工厂返回 `UInt`」，并观察一个位拼接运算如何变成一条 IR 命令。

**操作步骤**：

1. 阅读下面的「示例代码」（不是项目原有代码，仅作演示）：

```scala
// 示例代码：在 Scala REPL（ammonite 或 sbt console）里，已 import chisel3._ 并在 Module 内
val a = Wire(UInt(4.W))
val b = Wire(UInt(4.W))
val cat = a ## b          // 位拼接，预期 8 位
val hi = a(3)             // 取最高位，结果是 Bool
val slice = b(3, 1)       // 取高 3 位，结果是 UInt(3.W)
```

2. 在 [core/src/main/scala/chisel3/Bits.scala:214-217](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Bits.scala#L214-L217) 找到 `##`（拼接）的实现 `_impl_##`，确认它的结果位宽是 `this.width + that.width`，并最终调用 `pushOp(DefPrim(..., ConcatOp, ...))`。

**需要观察的现象**：

- `cat` 的推断位宽应为 8（4 + 4）；`hi` 是 `Bool`；`slice` 是 3 位的 `UInt`。
- `##` 在源码里登记的是 `ConcatOp` 这个 `PrimOp`。

**预期结果**：拼接宽度满足 \( w(a\; \#\#\; b) = w(a) + w(b) = 8 \)。若你在 REPL 里 `println(cat.getWidth)`，应打印 `8`（待本地验证具体 REPL 行为）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `object Bits` 要继承 `UIntFactory` 而不是一个独立的 `BitsFactory`？

**参考答案**：因为「裸位向量」没有符号信息，Chisel 选择默认按无符号解释，所以 `Bits(8.W)` 直接等价于 `UInt(8.W)`；共用 `UIntFactory` 避免重复定义 `apply` / `Lit`。

**练习 2**：`binop` 和 `compop` 在「结果类型」上最关键的区别是什么？

**参考答案**：`binop` 的结果类型 `dest: T` 由调用方按位宽规则算出后传入（可以是 `UInt` / `SInt`，位宽各不相同）；`compop` 的结果**永远是 `Bool()`**（1 位），因为比较只产生真 / 假。

---

### 4.2 UInt / SInt：有符号与无符号整数

#### 4.2.1 概念说明

`UInt` 是**无符号整数**（unsigned），`SInt` 是**有符号整数**（signed，二进制补码表示）。两者都继承自 `Bits`，区别只在于「同一串位如何被解释」以及「运算的位宽规则略有不同」。

- `UInt(8.W)`：8 位无符号，能表示 \( 0 \sim 2^8-1 = 255 \)。
- `SInt(8.W)`：8 位有符号补码，能表示 \( -128 \sim +127 \)。

它们提供完整的算术（`+ - * / %`）、按位逻辑（`& | ^ ~`）、移位（`<< >>`）、比较（`< > <= >= == =/=`）和归约（`orR andR xorR`）。注意 Chisel 的比较与相等：

- `===` 是硬件相等，`=/=` 是硬件不等（**不是** Scala 的 `==` / `!=`）。
- 比较结果都是 `Bool`。

一个常被忽略的细节：Chisel 的 `+` / `-` **默认不扩展位宽**（丢弃进位 / 借位）。如果你需要保留进位，要用 `+&`（加法并扩展一位）；如果想明确「我接受截断」，默认 `+` 即可。这点直接决定了本讲综合实践的位宽结果，务必记住。

#### 4.2.2 核心流程

每个运算符的实现都遵循同一个三段式（位宽推断 → 构造结果类型 → 登记 `PrimOp`）：

1. **算位宽**：根据运算种类，用 `Width` 的 `+` / `max` / `min` 算出结果位宽。
2. **造结果类型**：用算出的位宽 `new` 一个同符号的结果类型（如 `UInt(this.width + that.width)`）。
3. **登记命令**：调用 `binop(sourceInfo, 结果类型, 某个 PrimOp, that)`，最终 `pushOp(DefPrim(...))`。

不同运算的位宽规则是本讲最该记住的「速查表」（以 `UInt` 为例，`SInt` 大体一致，除法略有差异）：

| 运算 | `PrimOp` | 结果位宽（`wa = w(this)`，`wb = w(that)`） |
| --- | --- | --- |
| 乘法 `*` | `TimesOp` | \( w_a + w_b \) |
| 加法（带进位）`+&` | `AddOp` | \( \max(w_a, w_b) + 1 \) |
| 加法（默认截断）`+` ≡ `+%` | `AddOp` 后 `tail(1)` | \( \max(w_a, w_b) \) |
| 减法（带借位）`-&` | `SubOp` | \( \max(w_a, w_b) + 1 \) |
| 除法 `/` | `DivideOp` | \( w_a \)（`SInt` 为 \( w_a + 1 \)） |
| 取模 `%` | `RemOp` | \( \min(w_a, w_b) \) |
| 按位 `& \| ^` | `BitAnd/Or/XorOp` | \( \max(w_a, w_b) \) |
| 左移 `<< n` | `ShiftLeftOp` | \( w_a + n \) |
| 比较 `< > <= >= == =/=` | `Less/Greater/...Op` | 1（`Bool`） |

> 位宽「感染」：若参与运算的某个位宽是 `UnknownWidth`，则 `Width` 的 `+` / `max` / `min` 都会返回 `UnknownWidth`（见 [Width.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Width.scala) 里 `UnknownWidth.op` 直接 `return this`）。所以「未知」会一路传播，直到被一个已知位宽的赋值目标「截断」——这就是位宽推断的本质。

#### 4.2.3 源码精读

先看 `UInt` 的类声明与乘法实现——它把上面三段式体现得最清楚。

[core/src/main/scala/chisel3/Bits.scala:239](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Bits.scala#L239) —— `UInt` 继承 `Bits`，混入 `UIntIntf`（运算符宏壳子）和 `Num[UInt]`（数值抽象）：

```scala
sealed class UInt private[chisel3] (width: Width) extends Bits(width) with UIntIntf with Num[UInt] {
```

[core/src/main/scala/chisel3/Bits.scala:266-267](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Bits.scala#L266-L267) —— `UInt` 乘法：**位宽相加**，登记 `TimesOp`：

```scala
protected def _impl_*(that: UInt)(implicit sourceInfo: SourceInfo): UInt =
  binop(sourceInfo, UInt(this.width + that.width), TimesOp, that)
```

- `UInt(this.width + that.width)`：先算位宽（`Width.+`），再造结果类型——正是三段式的前两步合并写在一起。
- `TimesOp`：FIRRTL 的乘法原语。
- 最终交给 4.1.3 的 `binop` 去 `pushOp`。

再看加法的「默认截断」如何实现——它揭示了 `+` 与 `+&` 的关系。[core/src/main/scala/chisel3/Bits.scala:260-275](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Bits.scala#L260-L275)：

```scala
protected def _impl_+(that: UInt)(implicit sourceInfo: SourceInfo): UInt = this +% that   // 默认 + 截断进位
protected def _impl_-(that: UInt)(implicit sourceInfo: SourceInfo): UInt = this -% that
...
protected def _impl_+&(that: UInt)(implicit sourceInfo: SourceInfo): UInt =
  binop(sourceInfo, UInt((this.width.max(that.width)) + 1), AddOp, that)   // +& 保留进位，max+1
protected def _impl_+%(that: UInt)(implicit sourceInfo: SourceInfo): UInt =
  (this +& that).tail(1)                                                    // +% = +& 后丢最高位 → max
```

读法：`+` 委托给 `+%`，`+%` 又是「先 `+&`（得到 max+1 位），再 `tail(1)`（丢掉最高位）」，最终回到 \( \max(w_a, w_b) \) 位。所以**默认加法会丢进位**，要保留就得显式写 `+&`。

比较运算则全部走 `compop`，结果恒为 `Bool`。[core/src/main/scala/chisel3/Bits.scala:303-316](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Bits.scala#L303-L316)：

```scala
protected def _impl_<(that: UInt)(implicit sourceInfo: SourceInfo): Bool = compop(sourceInfo, LessOp, that)
...
protected def _impl_===(that: UInt)(implicit sourceInfo: SourceInfo): Bool = compop(sourceInfo, EqualOp, that)
```

`SInt` 与 `UInt` 结构对称（[core/src/main/scala/chisel3/Bits.scala:431](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Bits.scala#L431) 类声明），主要差异在除法位宽：[core/src/main/scala/chisel3/Bits.scala:456-461](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Bits.scala#L456-L461) —— `SInt` 乘法仍是位宽相加 \( w_a+w_b \)，但除法结果是 \( w_a+1 \)（多一位符号位保护）：

```scala
protected def _impl_*(that: SInt)(implicit sourceInfo: SourceInfo): SInt =
  binop(sourceInfo, SInt(this.width + that.width), TimesOp, that)
protected def _impl_/(that: SInt)(implicit sourceInfo: SourceInfo): SInt =
  binop(sourceInfo, SInt(this.width + 1), DivideOp, that)
```

> 交叉相乘：`UInt * SInt` 和 `SInt * UInt` 也有专门重载（[Bits.scala:269](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Bits.scala#L269) 与 [456](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Bits.scala#L456) 附近），结果统一为 `SInt`——因为只要有符号数参与，结果就必须按有符号解释。

最后补一条「运算符如何被调到 `_impl_*`」的调用链。用户写的 `a + b` 其实是一个宏：

[core/src/main/scala-2/chisel3/NumIntf.scala:24-27](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala-2/chisel3/NumIntf.scala#L24-L27) —— 公开的 `+` 是一个宏，编译期被改写：

```scala
final def +(that: T): T = macro SourceInfoTransform.thatArg
/** @group SourceInfoTransformMacro */
def do_+(that: T)(implicit sourceInfo: SourceInfo): T
```

[core/src/main/scala-2/chisel3/BitsIntf.scala:306](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala-2/chisel3/BitsIntf.scala#L306) —— `do_+` 桥接到 `_impl_+`：

```scala
override def do_+(that: UInt)(implicit sourceInfo: SourceInfo): UInt = _impl_+(that)
```

于是完整链路是：

```text
a + b                               // 用户写的（NumIntf 里的 final def +，是一个 macro）
  →（宏改写，注入隐式 SourceInfo）→ a.do_+(b)(implicitly[SourceInfo])   // BitsIntf 里的 do_+
  → _impl_+(b)                      // Bits.scala 里真正的实现 + 位宽推断
  → binop(..., UInt(max+?), AddOp, b)
  → pushOp(DefPrim(...))           // 登记一条命令进 Builder 队列
```

宏的具体改写逻辑在 [macros/src/main/scala-2/chisel3/internal/sourceinfo/SourceInfoTransform.scala:21-26](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/macros/src/main/scala-2/chisel3/internal/sourceinfo/SourceInfoTransform.scala#L21-L26)，它把 `thisObj.do_+(...)(implicitSourceInfo)` 拼出来。这条链路是 u7-l2（SourceInfo 宏）的预告，本讲你只需记住「运算符 = 宏壳子 + `do_` + `_impl_` + `pushOp`」。

#### 4.2.4 代码实践

**实践目标**：用 `UInt` 与 `SInt` 各实现 `a * b + c`（三个 8 位输入），**先按源码规则预测结果位宽，再生成 Verilog 核对**。

**操作步骤**：

1. 把下面「示例代码」保存为一个可运行的 Chisel 模块（沿用 u1-l4 介绍过的 `Module` + `IO(new Bundle{...})` 写法）：

```scala
// 示例代码
import chisel3._
import circt.stage.ChiselStage

class MulAddU extends Module {
  val io = IO(new Bundle {
    val a = Input(UInt(8.W))
    val b = Input(UInt(8.W))
    val c = Input(UInt(8.W))
    val y = Output(UInt())   // 位宽故意留空，交给 Chisel 推断
  })
  io.y := io.a * io.b + io.c
}

class MulAddS extends Module {
  val io = IO(new Bundle {
    val a = Input(SInt(8.W))
    val b = Input(SInt(8.W))
    val c = Input(SInt(8.W))
    val y = Output(SInt())
  })
  io.y := io.a * io.b + io.c
}

// 触发 elaboration + CIRCT，打印生成的 SystemVerilog
println(ChiselStage.emitSystemVerilog(new MulAddU))
println(ChiselStage.emitSystemVerilog(new MulAddS))
```

2. 先**合上电脑预测**：按 4.2.2 的速查表，`a * b` 是 \( 8+8=16 \) 位；再 `+ c`（默认 `+` 截断）是 \( \max(16, 8)=16 \) 位。所以 `y` 应被推断为 **16 位**。
3. 运行（在仓库根目录，参见 u1-l2 的 mill 用法），观察打印出的 Verilog 里 `y` 的位宽。

**需要观察的现象**：

- 无论 `UInt` 版还是 `SInt` 版，输出端口 `y` 都是 16 位（Verilog 里写作 `output [15:0] y`）。
- 若把 `+` 改成 `+&`，`y` 会变成 17 位（多了进位位）。

**预期结果**：根据源码位宽规则，两版 `y` 均为 16 位；改用 `+&` 后为 17 位。这印证了「乘法位宽相加、默认加法取最大且截断进位」。Verilog 文本的确切写法待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`UInt(4.W) + UInt(4.W)` 和 `UInt(4.W) +& UInt(4.W)` 的结果位宽分别是多少？

**参考答案**：`+` 截断进位 → \( \max(4,4)=4 \) 位；`+&` 保留进位 → \( \max(4,4)+1=5 \) 位。

**练习 2**：为什么 `SInt` 除法的位宽是 \( w_a+1 \)，而 `UInt` 除法是 \( w_a \)？

**参考答案**：有符号补码除法可能产生需要多一位符号位保护的结果（例如最小负数除以 -1 的边界），所以 `SInt` 多留一位；无符号除法不存在符号问题，结果不超过被除数位宽即可。

**练习 3**：若把上面 `MulAddU` 里 `io.b` 改成 `UInt()`（不写位宽，即 `UnknownWidth`），`y` 的位宽会变成什么？

**参考答案**：会变成未知（`UnknownWidth`），因为位宽「感染」——`b` 未知导致 `a*b` 未知，进而 `y` 未知。直到 `y` 这个有明确位宽的端口在连线时才会被强制对齐（见 u3 连线讲义）。

---

### 4.3 Bool：单比特布尔

#### 4.3.1 概念说明

`Bool` 表示一个「真 / 假」信号，物理上是 1 根线。它最特别的地方在于继承关系：**`Bool extends UInt(1.W)`**。也就是说，`Bool` 在类型层级里是 `UInt` 的子类（一位的无符号数）。源码顶部那条 `REVIEW TODO` 注释甚至专门留了个疑问：「为什么 `Bool` 继承 `UInt` 而不是 `Bits`？」

[core/src/main/scala/chisel3/Bits.scala:632-634](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Bits.scala#L632-L634)：

```scala
// REVIEW TODO: Why does this extend UInt and not Bits? Does defining airth
// operations on a Bool make sense?
/** A data type for booleans, defined as a single bit indicating true or false. */
```

这个设计的好处是：`Bool` 自动复用 `UInt` 的全部位运算（`& | ^ ~`），还能当 1 位的 `UInt` 参与算术。坏处是 `Bool` 也「继承」了一些对布尔意义不大的算术（如 `+`），但日常使用无伤大雅。

除了 `& | ^ ~`，`Bool` 还额外提供**逻辑运算** `&&` / `||`（注意：在 Chisel 里 `&&` / `||` 其实是 `&` / `|` 的别名，结果仍是硬件 `Bool`，不是 Scala 短路求值）、取反 `!`、以及 `implies`（蕴含）。它还和复位（`Reset`）共用接口，所以 `Bool` 能当同步复位信号用。

#### 4.3.2 核心流程

`Bool` 的运算流程和 `UInt` 完全一致（毕竟它就是 1 位 `UInt`）：位宽固定为 1，运算走 `Bits` 的 `binop` / `compop` / `redop` / `unop` 登记 `PrimOp`。区别只在两点：

1. **位宽恒为 1**：`cloneTypeWidth` 会校验宽度必须是 1（[Bits.scala:654-657](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Bits.scala#L654-L657)）。
2. **字面量是布尔语义**：`Bool.Lit(true/false)` 内部仍存成 `ULit(1/0, Width(1))`，但额外提供 `litToBoolean` 把 `0/1` 翻译回 Scala 的 `Boolean`。

#### 4.3.3 源码精读

[core/src/main/scala/chisel3/Bits.scala:639](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Bits.scala#L639) —— `Bool` 继承 `UInt(1.W)`，混入 `BoolIntf` 与 `Reset`：

```scala
sealed class Bool() extends UInt(1.W) with BoolIntf with Reset {
```

看逻辑运算如何落到 `PrimOp`。[core/src/main/scala/chisel3/Bits.scala:669-683](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Bits.scala#L669-L683)：

```scala
protected def _impl_&(that: Bool)(implicit sourceInfo: SourceInfo): Bool = binop(sourceInfo, Bool(), BitAndOp, that)
protected def _impl_|(that: Bool)(implicit sourceInfo: SourceInfo): Bool = binop(sourceInfo, Bool(), BitOrOp, that)
...
protected def _impl_||(that: Bool)(implicit sourceInfo: SourceInfo): Bool = this | that   // || 就是 |
protected def _impl_&&(that: Bool)(implicit sourceInfo: SourceInfo): Bool = this & that   // && 就是 &
```

注意 `dest` 都是 `Bool()`（1 位）。还要特别看 `!`（逻辑非）——它**不是**位取反 `~`，而是「等于 0」：

[core/src/main/scala/chisel3/Bits.scala:318](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Bits.scala#L318) —— `Bool` 的取反继承自 `UInt`，定义为「与 0 比较」：

```scala
protected def _impl_unary_!(implicit sourceInfo: SourceInfo): Bool = this === 0.U(1.W)
```

这是 `UInt` 上定义的方法，`Bool` 直接继承。对 1 位的 `Bool` 而言，`!x` 等价于「x 是否为 0」，语义正确。

字面量的构造在工厂里：[core/src/main/scala/chisel3/BoolFactory.scala:15-20](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/BoolFactory.scala#L15-L20) —— `Bool.Lit(true)` 内部存成 `ULit(1, Width(1))`：

```scala
protected[chisel3] def Lit(x: Boolean): Bool = {
  val result = new Bool()
  val lit = ULit(if (x) 1 else 0, Width(1))
  lit.bindLitArg(result)   // 把 result 绑定成「字面量」
}
```

`bindLitArg` 就是把这个 `Bool` 标记为字面量（`LitBinding`），对应 u2-l1 讲的 binding 种类。

#### 4.3.4 代码实践

**实践目标**：验证 `Bool` 的逻辑运算，并确认 `!` 等价于「与 0 比较」。

**操作步骤**：

1. 阅读「示例代码」：

```scala
// 示例代码
val a = Wire(Bool())
val b = Wire(Bool())
val and_ab = a && b       // 预期：a & b
val or_ab  = a || b       // 预期：a | b
val not_a  = !a           // 预期：a === 0.U
```

2. 对照 [Bits.scala:669-683](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Bits.scala#L669-L683) 与 [Bits.scala:318](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Bits.scala#L318)，确认 `&& → &`、`|| → |`、`! → === 0.U`。

**需要观察的现象**：生成的 IR 里，`&&` 和 `&` 登记的是同一个 `BitAndOp`；`!a` 登记的是一个 `EqualOp` 比较。

**预期结果**：`&&` / `||` 在硬件上与 `&` / `|` 完全等价（都是按位逻辑），不存在「短路」。这是 Chisel 与 Scala 的一个重要区别——待本地验证生成的 CHIRRTL 文本。

#### 4.3.5 小练习与答案

**练习 1**：在 Chisel 里，`a && b` 和 `a & b`（`a`、`b` 都是 `Bool`）生成的硬件一样吗？

**参考答案**：一样。源码里 `_impl_&&` 直接 `= this & that`，二者都登记 `BitAndOp`。Chisel 的 `&&` / `||` 不是 Scala 的短路逻辑，而是普通硬件逻辑门。

**练习 2**：为什么 `Bool` 能直接当一个 1 位的 `UInt` 用（比如参与 `+`）？

**参考答案**：因为 `Bool extends UInt(1.W)`，它就是 `UInt` 的子类，自然继承了 `UInt` 的全部算术。位宽恒为 1 由 `cloneTypeWidth` 的校验保证。

---

### 4.4 Num：数值抽象

#### 4.4.1 概念说明

`Num[T]` 是一个**对「所有数值类硬件类型」的抽象**，`UInt` 和 `SInt` 都混入了它（`with Num[UInt]` / `with Num[SInt]`）。它的作用是抽出「与具体符号无关、任何数字都该有的」通用操作，让上层代码可以写成 `T <: Num[T]` 的泛型，而不必区分 `UInt` / `SInt`。

`Num` 提供的核心能力分三组（声明在 `NumIntf`，实现在 `Num` 或具体类）：

1. **算术四则**：`+ - * / %`（抽象方法，由 `UInt` / `SInt` 各自实现 `_impl_*`）。
2. **比较**：`< > <= >=`，结果 `Bool`。
3. **通用数值方法**：`abs`（绝对值）、`min` / `max`（取两者较小 / 较大）。

注意 `abs` / `min` / `max` 是「用比较 + 选择」**组合实现**的，不是单个 `PrimOp`——这对理解它们的结果位宽很重要。

#### 4.4.2 核心流程

- `abs`：`UInt` 的绝对值是它自己（无符号恒非负）；`SInt` 的绝对值用 `Mux(this < 0.S, -this, this)` 实现（负数取反、非负不变）。
- `min` / `max`：都用 `Mux(this < that, ...)` 实现——先比较，再二选一。所以它们的结果位宽是 `max(w(this), w(that))`（`Mux` 不改变位宽）。

这意味着 `abs` / `min` / `max` 不是「免费的」，它们会生成比较器 + 多路选择器（`Mux`）硬件。

#### 4.4.3 源码精读

[core/src/main/scala/chisel3/Num.scala:24-31](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Num.scala#L24-L31) —— `Num` 继承 `NumIntf`，并直接给出了 `min` / `max` 的默认实现（用 `Mux`）：

```scala
trait Num[T <: Data] extends NumIntf[T] {
  protected def _minImpl(that: T)(implicit sourceInfo: SourceInfo): T =
    Mux(this < that, this.asInstanceOf[T], that)
  protected def _maxImpl(that: T)(implicit sourceInfo: SourceInfo): T =
    Mux(this < that, that, this.asInstanceOf[T])
}
```

`abs` 则各类型自己实现。[core/src/main/scala/chisel3/Bits.scala:283](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Bits.scala#L283) —— `UInt.abs` 就是它自己（无符号数恒非负）：

```scala
protected def _absImpl(implicit sourceInfo: SourceInfo): UInt = this
```

[core/src/main/scala/chisel3/Bits.scala:508-510](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Bits.scala#L508-L510) —— `SInt.abs` 用 `Mux` 选择：

```scala
protected def _absImpl(implicit sourceInfo: SourceInfo): SInt = {
  Mux(this < 0.S, -this, this)
}
```

用户侧入口仍是宏。[core/src/main/scala-2/chisel3/NumIntf.scala:129-132](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala-2/chisel3/NumIntf.scala#L129-L132)：

```scala
final def abs: T = macro SourceInfoTransform.noArg
def do_abs(implicit sourceInfo: SourceInfo): T
```

> `NumObject`（[Num.scala:36-107](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Num.scala#L36-L107)）是另一回事：它是一组**纯软件**的 `BigInt` ↔ `Double` / `BigDecimal` 换算工具（带定点小数位 `binaryPoint`），不在硬件类型层级里，定点的真正类型（`FixedPoint`）在 experimental 包。本讲了解即可。

#### 4.4.4 代码实践

**实践目标**：确认 `SInt.abs` 会生成「比较 + 取反 + 选择」的硬件，而不是单个原语。

**操作步骤**：

1. 阅读「示例代码」：

```scala
// 示例代码
val x = Wire(SInt(8.W))
val y = x.abs            // 预期：Mux(x < 0.S, -x, x)
val z = x.max(5.S)       // 预期：Mux(x < 5.S, 5.S, x)
```

2. 对照 [Bits.scala:508-510](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Bits.scala#L508-L510) 与 [Num.scala:29-30](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Num.scala#L29-L30)。

**需要观察的现象**：生成的 Verilog / CHIRRTL 里，`abs` 对应一个比较器、一个取反（减法）和一个 2 选 1 多路选择器，而不是单一门。

**预期结果**：`abs` 与 `max` 都展开为 `Mux` + 比较逻辑，结果位宽与输入相同（`Mux` 不扩位）。

#### 4.4.5 小练习与答案

**练习 1**：`UInt(8.W).abs` 的结果位宽是多少？为什么？

**参考答案**：8 位。因为 `_absImpl` 对 `UInt` 直接返回 `this`，无符号数本身就是非负的，绝对值不变。

**练习 2**：为什么说 `min` / `max` 「不是免费的」？

**参考答案**：它们用 `Mux(this < that, ...)` 实现，会生成一个比较器和一个多路选择器，占面积 / 延迟，不是一条零成本的 IR 原语。

---

### 4.5 字面量隐式转换：.U / .S / .B / .W

#### 4.5.1 概念说明

在 Chisel 里你几乎不会 `new UInt(...)`，而是写 `5.U`、`-1.S`、`true.B`、`8.W`。这些「凭空多出来的方法」来自 `chisel3` 包对象里定义的一组**隐式类（implicit class）**。隐式类是 Scala 的语法糖：当编译器看到 `5.U`，而 `Int` 本身没有 `U` 方法时，它会找一个「能把 `Int` 包一层、且带 `U` 方法」的隐式类——找到 `fromIntToLiteral`，于是 `5.U` 被改写成 `new fromIntToLiteral(5).U`。

这一层把 Scala 的软件字面量（`Int` / `Long` / `BigInt` / `Boolean` / `String`）桥接成 Chisel 的**硬件字面量**（绑定成 `LitBinding` 的 `UInt` / `SInt` / `Bool`）。注意两类风格后缀的区别：

- `.U` / `.S` / `.B`：**常量**推荐写法（强调「这是一个固定值」）。
- `.asUInt` / `.asSInt` / `.asBool`：**变量**推荐写法（强调「这是一个会被求值的表达式」）。

二者在源码里实现完全一样，只是命名上的风格提示。

#### 4.5.2 核心流程

字面量构造的统一路径是：`xxx.U` → 调用 `UInt.Lit(value, width)` → 创建 `ULit` IR 节点 → `lit.bindLitArg(result)` 把返回的 `UInt` 绑定为字面量。位宽处理有两条路：

1. **不指定位宽**（如 `5.U`）：传 `Width()`（即 `UnknownWidth`），由 `ULit` 内部按值的最小位宽推导出一个已知位宽。
2. **指定位宽**（如 `5.U(8.W)`）：传 `Width(8)`，得到定宽字面量。

字符串字面量还多一层「进制解析」：`"b101".U`（二进制）、`"hff".U`（十六进制）、`"o17".U`（八进制）、`"d10".U`（十进制），由 `fromStringToLiteral.parse` 按首字母判断基数。

#### 4.5.3 源码精读

[core/src/main/scala/chisel3/package.scala:34-75](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/package.scala#L34-L75) —— 核心隐式类 `fromBigIntToLiteral`，提供 `.U` / `.S` / `.B` 及其带宽度、`asXxx` 变体：

```scala
implicit class fromBigIntToLiteral(bigint: BigInt) {
  def B: Bool = bigint match {
    case bigint if bigint == 0 => Bool.Lit(false)
    case bigint if bigint == 1 => Bool.Lit(true)
    case bigint => Builder.error(s"Cannot convert $bigint to Bool, must be 0 or 1")...; Bool.Lit(false)
  }
  def U: UInt = UInt.Lit(bigint, Width())          // 不指定位宽
  def S: SInt = SInt.Lit(bigint, Width())
  def U(width: Width): UInt = UInt.Lit(bigint, width)   // 指定位宽
  def S(width: Width): SInt = SInt.Lit(bigint, width)
  def asUInt: UInt = UInt.Lit(bigint, Width())     // 与 .U 实现相同，仅风格不同
  ...
}
```

`Int` / `Long` 复用同一套：[package.scala:77-78](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/package.scala#L77-L78)：

```scala
implicit class fromIntToLiteral(int: Int) extends fromBigIntToLiteral(int)
implicit class fromLongToLiteral(long: Long) extends fromBigIntToLiteral(long)
```

布尔与位宽：[core/src/main/scala/chisel3/package.scala:114-127](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/package.scala#L114-L127)：

```scala
implicit class fromBooleanToLiteral(boolean: Boolean) {
  def B: Bool = Bool.Lit(boolean)
  def asBool: Bool = Bool.Lit(boolean)
}
implicit class fromIntToWidth(int: Int) {
  def W: Width = Width(int)        // 8.W 就是 Width(8) 即 KnownWidth(8)
}
```

字符串字面量的进制解析：[core/src/main/scala/chisel3/package.scala:101-111](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/package.scala#L101-L111)：

```scala
protected def parse(n: String): BigInt = {
  val (base, num) = n.splitAt(1)
  val radix = base match {
    case "x" | "h" => 16
    case "d"       => 10
    case "o"       => 8
    case "b"       => 2
    case _         => Builder.error(s"Invalid base $base")...; 2
  }
  BigInt(num.filterNot(_ == '_'), radix)   // 支持下划线分隔，如 "b1010_1010"
}
```

最后看 `UInt.Lit` 如何把字面量绑定。[core/src/main/scala/chisel3/UIntFactory.scala:18-23](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/UIntFactory.scala#L18-L23)：

```scala
protected[chisel3] def Lit(value: BigInt, width: Width): UInt = {
  val lit = ULit(value, width)
  val result = new UInt(lit.width)
  lit.bindLitArg(result)   // 绑定为字面量
}
```

`SInt.Lit`（[SIntFactory.scala:16-20](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/SIntFactory.scala#L16-L20)）结构完全对称，只是用 `SLit`。

> 工厂还有「造类型」的 `apply`：`UInt()` 造推断位宽的类型、`UInt(8.W)` 造 8 位类型（[UIntFactory.scala:11-15](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/UIntFactory.scala#L11-L15)）。注意 `UInt(8.W)` 和 `5.U(8.W)` 的区别：前者是**纯类型**（无值、未绑定），后者是**字面量硬件值**（有值、已绑定）。

#### 4.5.4 代码实践

**实践目标**：用不同进制和风格写字面量，并理解「类型 vs 字面量」。

**操作步骤**：

1. 阅读「示例代码」，预测每个值的类型与位宽：

```scala
// 示例代码
val a = 5.U               // UInt，位宽由 5 的最小位宽推得 → 预期 3 位
val b = 5.U(8.W)          // UInt(8)，定宽 8 位字面量
val c = "b1010_1010".U    // UInt，二进制解析 0xAA → 预期 8 位
val d = -3.S              // SInt，负数字面量
val e = true.B            // Bool(true)
val t = UInt(8.W)         // 注意：这是「类型」，不是字面量，不能直接参与运算
```

2. 对照 [package.scala:34-75](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/package.scala#L34-L75) 与 [UIntFactory.scala:11-23](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/UIntFactory.scala#L11-L23)。

**需要观察的现象**：

- `a` 的位宽是 3（`101` 三位），不是 8。
- `c` 解析成 `0xAA`（170），8 位。
- `t` 是纯类型，若写 `t + 1.U` 会因 `requireIsHardware` 报错（它是「类型」不是「硬件值」）。

**预期结果**：`5.U` 推断为 3 位；`"b1010_1010".U` 为 8 位（值 170）；`UInt(8.W)` 是类型不能直接运算。具体 REPL 打印待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：`5.U` 和 `5.U(8.W)` 的区别是什么？各自的位宽是多少？

**参考答案**：`5.U` 不指定位宽，由 `ULit` 按值 `5`（二进制 `101`）推得最小位宽 3；`5.U(8.W)` 显式定宽 8 位。前者位宽 3，后者位宽 8。

**练习 2**：`"h3f".U` 表示什么？位宽是多少？

**参考答案**：`parse` 按首字母 `h` 判定十六进制，`3f` 即 63；最小位宽为 6（\( \lceil\log_2 63\rceil = 6 \)）。

**练习 3**：为什么 `UInt(8.W)` 不能直接写 `UInt(8.W) + 1.U`？

**参考答案**：`UInt(8.W)` 调用的是工厂的 `apply`，返回的是**未绑定的纯类型**（`binding == None`）；而 `+` 的实现里第一步就是 `requireIsHardware(this, ...)`，纯类型通不过这个检查，会抛出「期望硬件值」异常（对应 u2-l1 的类型 vs 硬件值区分）。

## 5. 综合实践

把本讲的位宽推断、运算符链路、字面量串起来，完成下面这个**「位宽推断侦探」**小任务：

> 设计一个模块 `Detective`，输入为 `a: UInt(8.W)`、`b: UInt(8.W)`、`c: UInt(8.W)`、`sel: Bool`；输出 `y` 位宽**故意不指定**（`Output(UInt())`）。让 `y` 在两种结果间二选一：
>
> - `sel` 为真：`y = a * b + c`
> - `sel` 为假：`y = (a & b) | c`
>
> 要求：

1. **先合上电脑预测** `y` 的推断位宽（提示：`Mux` 取两支的位宽最大值；`a*b+c` 按 4.2 是 16 位；`(a&b)|c` 是 `max(8,8)=8` 位）。
2. 写出模块，用 `ChiselStage.emitSystemVerilog(new Detective)` 打印 Verilog，核对 `y` 的实际位宽是否符合预测。
3. 在生成的 Verilog 里找到 `(a & b) | c` 那条支路——由于它只有 8 位、而 `Mux` 另一支是 16 位，Chisel 会自动给它**补零扩展（pad）到 16 位**。请定位这个补位发生在哪一步（提示：`Mux` 的连线会触发位宽对齐，见 u3 连线讲义）。
4. 进阶：把 `+` 改成 `+&`（保留进位），重新预测并验证 `y` 的位宽变化。

**参考骨架（示例代码）**：

```scala
// 示例代码
import chisel3._
import circt.stage.ChiselStage

class Detective extends Module {
  val io = IO(new Bundle {
    val a = Input(UInt(8.W))
    val b = Input(UInt(8.W))
    val c = Input(UInt(8.W))
    val sel = Input(Bool())
    val y = Output(UInt())     // 位宽留空，靠推断
  })
  val mulAdd = io.a * io.b + io.c        // 预期 16 位
  val logic  = (io.a & io.b) | io.c      // 预期 8 位
  io.y := Mux(io.sel, mulAdd, logic)     // 预期取 max(16,8)=16 位
}

println(ChiselStage.emitSystemVerilog(new Detective))
```

**预期结论**：`y` 推断为 16 位；`logic` 支路会被补到 16 位再参与 `Mux`；改用 `+&` 后 `mulAdd` 变 17 位，`y` 随之变 17 位。这条任务把「乘法位宽相加、默认加法取最大、`Mux` 取最大且自动对齐」三条规则一次走完。

## 6. 本讲小结

- `Bits` 是 `UInt` / `SInt` 的共同父类（`sealed abstract class`），代表「二进制位向量」；`object Bits extends UIntFactory`，所以 `Bits` 工厂返回的是 `UInt`。
- 所有运算最终都收敛到 `Bits` 的四个 helper——`unop` / `binop` / `compop` / `redop`，由它们 `pushOp(DefPrim(...))` 把运算登记成 `PrimOp` 命令（「只登记不施工」）。
- 位宽推断的核心规则：乘法位宽相加 \( w_a+w_b \)、带进位加法 `+&` 取 \( \max+1 \)、默认加法 `+` 取 \( \max \) 且**截断进位**、按位逻辑取 \( \max \)、比较恒为 1 位 `Bool`；未知位宽会「感染」传播。
- `Bool extends UInt(1.W)`，复用 `UInt` 的全部位运算；`&&` / `||` 在硬件上与 `&` / `|` 等价（非短路），`!` 等价于 `=== 0.U`。
- `Num[T]` 是数值抽象，`abs` / `min` / `max` 用「比较 + `Mux`」组合实现，不是免费的单条原语。
- 完整运算调用链：用户 API `a + b`（宏）→ `do_+`（桥接）→ `_impl_+`（实现 + 位宽推断）→ `binop` → `pushOp(DefPrim)`。
- 字面量 `.U` / `.S` / `.B` / `.W` 由 `chisel3` 包对象的隐式类提供，经 `xxx.Lit(value, width)` 绑定成 `LitBinding` 硬件值；`UInt(8.W)` 是纯类型、`5.U(8.W)` 是字面量硬件值，二者由 `binding` 区分。

## 7. 下一步学习建议

- **接下来学 u2-l3（Bundle）**：本讲只讲了「叶子数值类型」。真实的模块 IO 几乎总是把多个信号打包成 `Bundle`（如 `valid` + `data` + `last`），那篇讲如何用 `Record` / `Bundle` 定义自定义聚合类型，并把本讲的 `UInt` / `Bool` 当作 `Bundle` 的字段。
- **随后学 u2-l4（Vec）**：当你需要「一组同类型信号」（如寄存器堆、多路输入），就用 `Vec`。它会用到本讲的位宽与运算规则。
- **连线的内部机制留到单元 3**：本讲多次提到「位宽对齐 / 自动补位」（如综合实践里 8 位支路补到 16 位），这个对齐发生在 `Mux` 与 `:=` 的连线算法里，u3-l3 / u3-l4 会下沉到 `MonoConnect` / `BiConnect` 讲清楚。
- **想深挖运算符的「宏壳子」**：本讲的 `+` → `do_+` → `_impl_+` 链路里，宏改写那一步（`SourceInfoTransform`）在 u7-l2（SourceInfo 宏与隐式源信息）详细讲解，它还负责把文件名 / 行号注入每条运算，用于报错定位。
- **建议阅读的源码**：把 [core/src/main/scala/chisel3/Bits.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Bits.scala) 从头到尾通读一遍——它是本讲的主战场，也是后续 `Bundle` / `Vec` / 连线都会反复引用的基础类型定义。
