# ChiselEnum 枚举类型

## 1. 本讲目标

学完本讲后，你应当能够：

- 用 `object State extends ChiselEnum { val A, B, C = Value }` 定义一组命名的硬件枚举常量。
- 说清楚枚举的编码宽度是如何被**自动推断**出来的，并能手算一个给定枚举的宽度。
- 理解 `EnumType` 作为一种叶子硬件类型（继承自 `Element`）所支持的运算（比较、`isValid`、`next`、与 `UInt` 互转）。
- 用 `switch` / `is` 写出一个基于枚举的有限状态机（FSM），并生成 Verilog 验证编码宽度。

## 2. 前置知识

本讲默认你已经掌握：

- **类型层级**（u2-l1）：`Data` 是所有硬件类型的根，`Element` 是叶子分支，`Width` 是 `KnownWidth | UnknownWidth` 的和类型。
- **数值类型与运算登记**（u2-l2）：`UInt`/`Bool` 的字面量 `.U`/`.B`，以及「运算符经 helper 调用 `pushOp(DefPrim(...))` 只登记不施工」的机制（`a+b` → `do_+` → `_impl_+` → `binop` → `pushOp`）。
- **模块与寄存器**（u1-l4、u3-l1、u3-l5）：`Module` + `IO`，以及 `RegInit` 的用法。

补充一个本讲会用到的 Scala 概念：**宏（macro）**。Chisel 在若干处用编译期宏在「调用处」自动改写代码——例如把 `val Idle = Value` 里的变量名 `Idle` 当成字符串捕获下来。你不需要会写宏，只需知道「某些看似普通的方法调用，在编译期被替换成了另一段代码」即可。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [ChiselEnum.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/ChiselEnum.scala) | 本讲主源码：定义 `EnumType`（枚举值类型）与 `ChiselEnum`（枚举工厂基类） |
| [core/src/main/scala-2/chisel3/ChiselEnumIntf.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala-2/chisel3/ChiselEnumIntf.scala) | `Value` 宏与比较运算符宏（Scala 2 实现） |
| [src/main/scala-2/chisel3/util/Switch.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala-2/chisel3/util/Switch.scala) | `switch` 宏：把 `switch(x){ is(...){} }` 改写成嵌套 `when` |
| [src/main/scala/chisel3/util/Conditional.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Conditional.scala) | `SwitchContext.is`：`is` 的真正实现，构造比较谓词 |
| [src/test/scala/cookbook/FSM.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala/cookbook/FSM.scala) | 官方 cookbook 里的真实 FSM 例子，本讲实践参考它 |

## 4. 核心概念与源码讲解

### 4.1 ChiselEnum：用 object 定义一组命名常量

#### 4.1.1 概念说明

写状态机时，你需要一组「有名字、有编码」的常量，比如 `Idle=0`、`Busy=1`、`Done=2`。

如果直接用 `UInt`，你会写出 `state === 1.U` 这样难读、易错的代码——`1` 是什么意思？换编码就要满模块改数字。`ChiselEnum` 解决的就是这个问题：让你用名字（`State.Busy`）引用状态，由 Chisel 自动分配编码、推断位宽，并在生成 Verilog 时把名字保留为可读注释/调试信息。

设计要点：

- `ChiselEnum` 是一个 `abstract class`，你用一个 **`object`** 去扩展它。
- 在 object 体里用 `val 名字 = Value` 声明每个枚举值。
- 一个**编译期宏**会自动捕获这个 `val` 的变量名作为枚举名（下文 4.1.3 详述）。
- 工厂内部用一个自增的 `BigInt` 计数器 `id` 给每个值编号，并根据最大编号自动算出位宽。

#### 4.1.2 核心流程

当你写：

```scala
object State extends ChiselEnum {
  val Idle, Busy, Done = Value
}
```

发生的事情是（编译期 + 该 object 首次被访问时）：

1. 编译期：`Value` 这个「方法」其实是宏，编译器把它替换成 `this.do_Value("Idle")` / `this.do_Value("Busy")` / `this.do_Value("Done")`——名字字符串是宏从 `val` 的左边抓出来的。
2. 运行期（elaboration 时 object 被实例化）：`do_Value` 被依次调用，`id` 从 0 开始递增：`Idle=0, Busy=1, Done=2`。
3. 每次调用都重算枚举的总位宽。

位宽的推断公式可以写成：

\[
\text{width} = \max\Bigl(1,\;\max_{v\in\text{values}}\operatorname{bitLength}(v),\;\text{maxUserWidth}\Bigr)
\]

其中 \(\operatorname{bitLength}(v)\) 表示「表示 \(v\) 所需的最小二进制位数」，且 \(\operatorname{bitLength}(0)=0\)（例如 `bitLength(2)=2`，`bitLength(3)=2`，`bitLength(4)=3`）。`maxUserWidth` 只在你用 `Value(id)` 显式指定编码时才可能大于 0。

以 `Idle=0, Busy=1, Done=2` 为例：最大值是 2，`bitLength(2)=2`，所以 `width = max(1, 2, 0) = 2`，即 2 位。最终的 `state` 寄存器在 Verilog 里就是 `[1:0]`，编码为 `2'd0 / 2'd1 / 2'd2`。

#### 4.1.3 源码精读

`ChiselEnum` 的类声明与它持有的可变状态：

[core/src/main/scala/chisel3/ChiselEnum.scala:215-223](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/ChiselEnum.scala#L215-L223) —— `abstract class ChiselEnum`，内部定义了 `class Type extends EnumType(this)`（这就是「该枚举对应的硬件类型」），并持有自增计数器 `id`、当前推断位宽 `width`、用户显式位宽 `maxUserWidth`，以及一个记录所有 `(值, 名字)` 的 `enumRecords` 缓冲区。

核心方法 `do_Value(name)`：每声明一个值就调用一次。

[core/src/main/scala/chisel3/ChiselEnum.scala:269-281](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/ChiselEnum.scala#L269-L281) —— 把当前 `id` 绑定成一个字面量（`bindToLiteral`），追加一条 `EnumRecord`，然后用 `width = (1.max(id.bitLength).max(maxUserWidth)).W` 重算位宽，最后 `id += 1`。注意位宽是在 `id` 自增**之前**用当前 `id` 算的，所以它正好反映了「到目前位置为止的最大编号」。

如果你想自定义编码（而非从 0 自增），用 `Value(id: UInt)`：

[core/src/main/scala/chisel3/ChiselEnum.scala:283-298](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/ChiselEnum.scala#L283-L298) —— `do_Value(name, id)`：要求传入的字面量**严格递增**（`id.litValue < this.id` 则报错），把 `this.id` 设为你给的值，必要时更新 `maxUserWidth`，再转交给上面的 `do_Value(name)`。例如 `val A = Value; val B = Value(4.U)` 会得到 `A=0, B=4`。

那 `Value` 到底是怎么变成 `do_Value("Idle")` 的？靠宏：

[core/src/main/scala-2/chisel3/ChiselEnumIntf.scala:28](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala-2/chisel3/ChiselEnumIntf.scala#L28) —— `protected def Value: Type = macro EnumMacros.ValImpl`，说明 `Value` 不是普通方法而是宏。

[core/src/main/scala-2/chisel3/ChiselEnumIntf.scala:33-46](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala-2/chisel3/ChiselEnumIntf.scala#L33-L46) —— `ValImpl` 用 `c.internal.enclosingOwner` 拿到「这个 `Value` 调用将要赋值给的那个 `val`」（即 `Idle`），取它的名字字符串，再生成 `this.do_Value("Idle")`。如果 `Value` 没有赋给任何 `val`（名字含空格），就 `c.abort` 报错——这就是为什么你必须写 `val Idle = Value` 而不能裸调 `Value`。

工厂还提供几个有用的查询接口：

- [ChiselEnum.scala:240](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/ChiselEnum.scala#L240) `getWidth`：返回推断出的位宽整数。
- [ChiselEnum.scala:243](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/ChiselEnum.scala#L243) `all`：所有枚举值。
- [ChiselEnum.scala:235-238](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/ChiselEnum.scala#L235-L238) `isTotal`：判断「当前位宽下的所有位向量是否都是合法状态」（即值数 == `2^width`）。3 个值、2 位宽时 `3 != 4`，所以 `isTotal=false`；4 个值、2 位宽时 `4 == 4`，`isTotal=true`。这个标志会影响 `isValid` 的实现（见 4.2）。

#### 4.1.4 代码实践

**目标**：直观观察「枚举位宽由最大编号自动推断」。

**操作步骤**：把下面这个最小模块（示例代码）放进一个可运行的 Chisel 工程里，再用 `ChiselStage.emitSystemVerilog` 打印 Verilog（参考 u1-l4 的用法）。

```scala
// 示例代码：观察 ChiselEnum 的位宽推断
import chisel3._
import chisel3.util._

object State extends ChiselEnum {
  val Idle, Busy, Done = Value   // 0, 1, 2
}

class EnumWidth extends Module {
  val io = IO(new Bundle {
    val s = Output(State())      // State() 返回一个未绑定的枚举类型
  })
  io.s := State.Done
}
```

```scala
println(ChiselStage.emitSystemVerilog(new EnumWidth))
```

**需要观察的现象**：输出端口 `s` 的位宽是多少位？

**预期结果**：`s` 是 2 位（`output [1:0] s`），因为最大编号 `Done=2`，`bitLength(2)=2`。把枚举改成 4 个值（如加一个 `Wait`）后位宽仍是 2 位（`3.bitLength=2`）；加到第 5 个值（编号 4，`bitLength(4)=3`）后位宽才涨到 3 位。如果无法本地运行，标注「待本地验证」，但你应能手算出各情况下的位宽。

#### 4.1.5 小练习与答案

**练习 1**：定义 `object E extends ChiselEnum { val A, B, C, D, E = Value }`（5 个值），手算它的位宽。
**答案**：值为 0..4，`bitLength(4)=3`，`width = max(1,3,0) = 3` 位。

**练习 2**：为什么 `val Idle, Busy, Done = Value` 必须用 `val` 接住，而不能直接写 `Value`？
**答案**：`Value` 是宏，它依赖 `c.internal.enclosingOwner` 读取「赋值目标 `val` 的名字」作为枚举名；没有 `val` 接住时取不到名字，宏会在编译期 `c.abort` 报错。

### 4.2 EnumType：枚举值的硬件类型

#### 4.2.1 概念说明

`ChiselEnum`（上一节）是「工厂/定义」，而 `EnumType` 是「枚举值在硬件世界里的类型」。每个 `ChiselEnum` 子类内部都有一个 `class Type extends EnumType(this)`，你写的 `State.Idle`、`State.Busy` 都是 `State.Type` 的实例。

关键点：

- `EnumType extends Element`（u2-l1 讲过的叶子分支），所以它和 `UInt`/`Bool` 一样是**不可再拆的叶子类型**，可以直接当 `IO`/`Reg`/`Wire` 的元素类型。
- 同一个 `State.Type` 既能扮演「类型」（未绑定，如 `State()`），也能扮演「硬件值」（已绑定，如 `State.Idle` 字面量、`RegInit(...)` 里的寄存器值）——这套「类型 vs 硬件值」的二分法在 u2-l1 已建立，由 `binding` 字段判定。
- 它支持 `===`/`=/=`/`<`/`>` 等比较运算，返回 `Bool`，背后机制和 u2-l2 讲的 `UInt` 运算登记同源。

#### 4.2.2 核心流程

**比较运算**：`state === State.Busy` 看起来像普通方法，实际走的是 u2-l2 同款的「宏 → `do_` → `_impl_` → helper → `pushOp`」链路，只是 helper 换成了枚举自己的 `compop`：

```
state === State.Busy          // 用户写法
  → (宏) do_===(State.Busy)   // SourceInfoTransform 注入源信息
  → _impl_===(State.Busy)     // 进入 EnumType
  → compop(EqualOp, that)     // 先做类型等价检查，再登记
  → pushOp(DefPrim(Bool, EqualOp, this.ref, other.ref))
```

和 `UInt` 的 `compop` 唯一的区别是：`EnumType.compop` 在 `pushOp` 之前会先检查两个枚举**类型等价**（同属一个 `ChiselEnum`），否则抛 `"Enum types are not equivalent"`。这能在 elaboration 期就把「拿 `State.Busy` 去和 `Color.Red` 比较」这种跨枚举错误拦下来。

**几个常用派生方法**：

- `isValid`：返回一个 `Bool`，表示当前值是否是合法枚举值。字面量恒为 `true.B`；若工厂 `isTotal`（所有位模式都合法）也直接返回 `true.B`（不生成任何硬件）；否则生成 `((state===v0) || (state===v1) || ...)`。
- `next`：返回「下一个」枚举值，末尾回绕到第一个。字面量在 elaboration 期算出；非字面量（如寄存器）用 `priorityMux` 生成选择硬件。
- `isOneOf(...)`：判断是否等于给定序列中的任意一个，用 `VecInit(...).asUInt.orR` 实现。
- `apply(n: UInt)` / `safe(n: UInt)`：把 `UInt` 转回枚举；`safe` 额外返回一个 `Bool` 表示是否合法（非 total 枚举常用）。

#### 4.2.3 源码精读

`EnumType` 的类声明与 `cloneType`：

[core/src/main/scala/chisel3/ChiselEnum.scala:14](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/ChiselEnum.scala#L14) —— `abstract class EnumType(...) extends Element with EnumTypeIntf`，确认它是叶子类型。
[core/src/main/scala/chisel3/ChiselEnum.scala:32](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/ChiselEnum.scala#L32) —— `cloneType` 直接委托给工厂 `factory()`，所以枚举类型的克隆总是由工厂造一个新的未绑定实例。位宽也来自工厂：

[core/src/main/scala/chisel3/ChiselEnum.scala:89](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/ChiselEnum.scala#L89) —— `override def width: Width = factory.width`，即任何 `EnumType` 实例的位宽都等于其工厂推断出的位宽（4.1 的那个 `width`）。

比较运算的实现链：

[core/src/main/scala-2/chisel3/ChiselEnumIntf.scala:12-17](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala-2/chisel3/ChiselEnumIntf.scala#L12-L17) —— `===`/`=/=`/`<`/`<=`/`>`/`>=` 都是宏，被改写成 `do_xxx`。
[core/src/main/scala/chisel3/ChiselEnum.scala:49-54](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/ChiselEnum.scala#L49-L54) —— `_impl_===` 等方法把对应的 `PrimOp`（`EqualOp` 等）传给 `compop`。
[core/src/main/scala/chisel3/ChiselEnum.scala:34-44](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/ChiselEnum.scala#L34-L44) —— `compop`：先 `requireIsHardware` 两边，再做 `typeEquivalent` 检查（跨枚举比较在此报错），最后 `pushOp(DefPrim(sourceInfo, Bool(), op, this.ref, other.ref))`——和 u2-l2 的 `pushOp(DefPrim)` 完全同构。

派生方法：

[core/src/main/scala/chisel3/ChiselEnum.scala:91-97](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/ChiselEnum.scala#L91-L97) —— `isValid`：字面量或 `factory.isTotal` 时返回常量 `true.B`，否则 `factory.all.map(this === _).reduce(_ || _)`。
[core/src/main/scala/chisel3/ChiselEnum.scala:131-145](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/ChiselEnum.scala#L131-L145) —— `next`：字面量直接取序列下一个；非字面量用 `SeqUtils.priorityMux` 生成硬件选择。
[core/src/main/scala/chisel3/ChiselEnum.scala:104-109](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/ChiselEnum.scala#L104-L109) —— `isOneOf`：`VecInit(s.map(this === _)).asUInt.orR`。

`UInt` ↔ 枚举互转（处理「外部送进来的原始位向量」）：

[core/src/main/scala/chisel3/ChiselEnum.scala:300](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/ChiselEnum.scala#L300) —— `apply(): Type = new Type`，返回一个**未绑定**的类型实例，供 `IO`/`Reg`/`Wire` 声明用。
[core/src/main/scala/chisel3/ChiselEnum.scala:343-344](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/ChiselEnum.scala#L343-L344) —— `apply(n: UInt)`：把 `UInt` 强转为枚举；若该枚举非 total，会发一条 elaboration 警告（提示你可能命中非法状态），可用 `.safe` 或包一层 `suppressEnumCastWarning` 抑制。
[core/src/main/scala/chisel3/ChiselEnum.scala:352-355](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/ChiselEnum.scala#L352-L355) —— `safe(n: UInt): (Type, Bool)`：返回转换结果和一个 `isValid` 的 `Bool`，适合用来做「非法状态恢复」。

> 调试小贴士：`EnumType.toPrintable`（[ChiselEnum.scala:192-212](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/ChiselEnum.scala#L192-L212)）会生成一段硬件，在 `printf` 里把状态值打印成可读名字（如 `Busy` 而非 `1`），仿真看波形时很有用。

#### 4.2.4 代码实践

**目标**：观察 `isValid` 在 total / non-total 两种枚举下生成的硬件差异。

**操作步骤**：定义两个枚举——一个 3 个值（non-total），一个 4 个值（total，2 位宽恰好覆盖 4 种状态）——各做一个模块，把 `state.isValid` 接到输出，对比 Verilog。

```scala
// 示例代码
import chisel3._

object E3 extends ChiselEnum { val A, B, C = Value }              // 3 值, non-total
object E4 extends ChiselEnum { val A, B, C, D = Value }           // 4 值, total

class ValidCheck[E <: EnumType](e: E) extends Module {            // 简化示意
  val io = IO(new Bundle {
    val state = Input(e)
    val valid = Output(Bool())
  })
  io.valid := io.state.isValid
}
```

**需要观察的现象**：`E3` 的 `valid` 输出是否综合出比较逻辑？`E4` 的呢？

**预期结果**：`E3`（3 值）非 total，`valid` 会生成 `((state==0)||(state==1)||(state==2))` 这类硬件；`E4`（4 值）是 total，`isValid` 直接返回常量 `1'b1`，几乎不生成硬件。这正是 `isTotal` 优化的意义。标注「待本地验证」若你无法运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `compop` 里要 `requireIsHardware` 两次？用 u2-l1/u2-l2 的概念解释。
**答案**：因为 `===` 等运算只接受已绑定的硬件值（不能拿两个纯类型做硬件比较）。`requireIsHardware` 检查 `binding.isDefined`，确保 `this` 和 `other` 都是硬件实例而非裸类型。

**练习 2**：写一行代码，把一个 `UInt` 安全转成 `State` 枚举并拿到「是否合法」的标志。
**答案**：`val (st, ok) = State.safe(myUInt)`，其中 `ok: Bool` 为真表示 `myUInt` 落在合法编码上。

### 4.3 switch / is：枚举状态机写法

#### 4.3.1 概念说明

定义了枚举，下一步就是「根据当前状态做不同的事」。`switch` / `is` 是 Chisel 提供的语法糖，专门用来把「对一个叶子类型（`Element`）匹配若干字面量」写成易读的多分支形式，等价于一串嵌套的 `when` / `elsewhen`。因为 `EnumType extends Element`，枚举天然能用 `switch`。

`switch` / `is` 在 `chisel3.util` 包里，使用前需 `import chisel3.util._`。（`switch`/`is` 的更多用法和 `Mux` 系列的对比会在 u6-l4 详讲，本节只聚焦枚举 FSM。）

#### 4.3.2 核心流程

你写的：

```scala
switch (state) {
  is (State.Idle) { ... }
  is (State.Busy) { ... }
}
```

背后两步改写：

1. **`switch` 宏**把整个块拆开，要求块里**每一条语句**都是 `is(...){}`，然后 fold 成一条 `SwitchContext.is(...).is(...)` 链。
2. **`SwitchContext.is`** 对每个分支：取出 `is` 里的字面量值，断言它们「互斥」（同一个值不能在两个 `is` 里出现），构造比较谓词 `v.asUInt === cond.asUInt`，再用 `when`/`elsewhen` 把各分支串起来。

也就是说，`switch` 最终还是会展开成你熟悉的 `when(...) { ... }.elsewhen(...) { ... }`，只是写法更整洁、且编译期保证了分支互斥。

#### 4.3.3 源码精读

`switch` 宏：

[src/main/scala-2/chisel3/util/Switch.scala:27-40](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala-2/chisel3/util/Switch.scala#L27-L40) —— `def apply[T <: Element](cond: T)(x: => Any): Unit = macro impl`。`impl` 用准引用 `q"..$body"` 把块体拆成语句列表，`foldLeft` 从 `new SwitchContext(cond, None, Set.empty)` 开始，对每条语句模式匹配 `is.apply(...)`，拼成 `$acc.is(...)(...)`；遇到非 `is` 语句直接抛异常（所以 switch 块里只能写 `is`）。

`SwitchContext.is` 的实现：

[src/main/scala/chisel3/util/Conditional.scala:15-46](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Conditional.scala#L15-L46) —— `class SwitchContext[T <: Element](...)`，`is` 方法要求每个条件 `w.litOption.isDefined`（必须是字面量），并 `require(!lits.contains(value), "all is conditions must be mutually exclusive!")` 保证互斥。
[src/main/scala/chisel3/util/Conditional.scala:29](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Conditional.scala#L29) —— `def p = v.map(_.asUInt === cond.asUInt).reduce(_ || _)` 构造分支谓词，然后第 30-33 行用 `when(p)(block)` 或 `w.elsewhen(p)(block)` 串接。注意它用 `asUInt` 比较，所以枚举值按其底层编码（4.1 推断出的位宽）参与比较。

真实的 FSM 范例（本讲实践的模板）：

[src/test/scala/cookbook/FSM.scala:21-47](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala/cookbook/FSM.scala#L21-L47) —— cookbook 的「检测连续两个 1」状态机：`object State extends ChiselEnum { val sNone, sOne1, sTwo1s = Value }`，`val state = RegInit(State.sNone)`，再用 `switch(state) { is(State.sNone){...} is(State.sOne1){...} ... }` 描述转移。这是官方推荐写法。

#### 4.3.4 代码实践

**目标**：定义 `Idle / Busy / Done` 三态枚举，用 `Reg` 存当前状态，用 `switch` 实现转移，生成 Verilog 查看编码宽度。

**操作步骤**：把下面模块（示例代码，仿照 cookbook/FSM.scala 改写）放进工程，打印 Verilog。

```scala
// 示例代码（参考 src/test/scala/cookbook/FSM.scala 改写）
import chisel3._
import chisel3.util._

object State extends ChiselEnum {
  val Idle, Busy, Done = Value       // 编码: Idle=0, Busy=1, Done=2
}

class SimpleFSM extends Module {
  val io = IO(new Bundle {
    val start = Input(Bool())
    val done  = Output(Bool())
  })

  val state = RegInit(State.Idle)     // 复位值为 Idle 字面量

  io.done := (state === State.Done)

  switch (state) {
    is (State.Idle) {
      when (io.start) { state := State.Busy }
    }
    is (State.Busy) {
      state := State.Done             // 一拍后进入 Done
    }
    is (State.Done) {
      state := State.Idle             // 完成后回到 Idle
    }
  }
}
```

```scala
println(ChiselStage.emitSystemVerilog(new SimpleFSM))
```

**需要观察的现象**：
1. `state` 寄存器的位宽是多少？
2. `state := ...` 的赋值常数分别是什么？

**预期结果**：`state` 是 `[1:0]`（2 位，因为最大编号 `Done=2`，`bitLength(2)=2`）。赋值分别为 `2'd0`(Idle)、`2'd1`(Busy)、`2'd2`(Done)。`done` 输出是 `state == 2'd2` 的比较。这印证了 4.1 的位宽推断结论。若无法本地运行，标「待本地验证」，但 2 位宽度可由公式手算确定。

#### 4.3.5 小练习与答案

**练习 1**：如果在 `switch` 块里写两个 `is (State.Idle) { ... }` 会怎样？
**答案**：编译/编译期展开不报错，但 `SwitchContext.is` 会在 elaboration 时 `require(!lits.contains(value), ...)` 抛错——「all is conditions must be mutually exclusive」，因为同一个值被列了两次。

**练习 2**：把上面 FSM 的 `is (state)` 条件换成非字面量（例如 `is (someWire)`）会怎样？
**答案**：`SwitchContext.is` 里 `require(w.litOption.isDefined, "is condition must be literal")` 会报错——`is` 的条件必须是字面量。这正是 `switch` 适合枚举（枚举值都是字面量）的原因。

## 5. 综合实践

把本讲三块知识串起来：实现一个「带非法状态恢复」的 3 态 FSM。

要求：

1. 定义 `object State extends ChiselEnum { val Idle, Busy, Done = Value }`。
2. 用 `RegInit(State.Idle)` 保存状态，用 `switch`/`is` 描述转移：`Idle --(start)--> Busy --(一拍)--> Done --(一拍)--> Idle`。
3. 由于 3 个值占 2 位、存在 `2'd3` 这个非法编码，再用 `State.safe` 或 `state.isValid` 做保护：当 `state` 不合法时强制回到 `Idle`。
4. 用 `ChiselStage.emitSystemVerilog` 生成 Verilog，确认：`state` 是 2 位；存在一段 `((state==0)||(state==1)||(state==2))` 的合法检测逻辑（来自 4.2 的 `isValid`，因 `isTotal=false`）；非法分支把 `state` 复位到 `2'd0`。

提示：可在 `switch` 之后加一句 `when (!state.isValid) { state := State.Idle }`。这个练习同时用到 4.1（位宽/编码）、4.2（`isValid`/非 total）、4.3（`switch`）。

## 6. 本讲小结

- `ChiselEnum` 是 `abstract class`，用 `object` 扩展，靠 `val X = Value` 声明命名常量；`Value` 是宏，自动捕获 `val` 名字，转成 `do_Value(name)`。
- 编码从 0 自增（也可用 `Value(id)` 自定义），位宽按 `max(1, max bitLength, maxUserWidth)` 自动推断，无需手写。
- `EnumType extends Element`，是叶子硬件类型；比较运算经 `compop → pushOp(DefPrim)` 登记（与 u2-l2 的 `UInt` 同构），并额外做跨枚举类型等价检查。
- `isValid`/`next`/`isOneOf`/`safe` 是常用派生能力；`isTotal` 时 `isValid` 退化为常量 `true.B`。
- `switch`/`is` 是嵌套 `when` 的语法糖，要求 `is` 条件为字面量且互斥，非常适合枚举状态机。

## 7. 下一步学习建议

- **下一讲 u3-l1（Module 与 RawModule 生命周期）**：本讲用了 `Module` + `RegInit`，接下来正式拆解 `Module` 在 elaboration 期是如何「evaluate → generateComponent」的。
- **u6-l1 / u6-l4**：学 `Decoupled` 握手接口，以及 `switch`/`is` 与 `Mux1H`/`MuxLookup` 的系统对比。
- **继续阅读源码**：想深入了解枚举与 FIRRTL/CIRCT 的交互，可读 `EnumType._asUIntImpl`（[ChiselEnum.scala:78-87](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/ChiselEnum.scala#L78-L87)）和枚举注解相关代码，看枚举如何被发射为带调试信息的 Verilog。
