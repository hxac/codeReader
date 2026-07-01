# BitPat / Cat / OneHot / Lookup 杂项工具

## 1. 本讲目标

本讲聚焦 `chisel3.util` 里四个高频但彼此独立的「位级工具」：

- **BitPat**：描述带「无关位（don't-care）」的位模式，用来做模糊匹配。
- **Cat**：把多个信号拼接成一个更宽的信号。
- **OneHot 家族**（`UIntToOH` / `OHToUInt` / `PriorityEncoder` / `PriorityEncoderOH`）：独热编码与位优先级编码。
- **Lookup / ListLookup**：以 `BitPat` 为键的查表多路选择器。

学完后你应当能够：

1. 用 `BitPat("b1?0")` 描述「只关心其中几位」的匹配，并说清它内部用 `value` + `mask` 两个 `BigInt` 表示。
2. 用 `Cat(a, b)` 拼接信号，并解释「第一个参数是最高位」的约定及其与 `SeqUtils.asUInt` 的反向关系。
3. 区分 `UIntToOH`、`OHToUInt`、`PriorityEncoder`、`PriorityEncoderOH` 四者的输入输出方向。
4. 用 `Lookup` 写出基于位模式的查表，并知道它内部就是一串 `Mux`。

> 本讲依赖 u2-l2（`UInt`/`Bits` 与字面量）。所有工具都遵循 Chisel「只登记不施工」的原则：它们只是向 Builder 压入命令，真正的门电路由下游 firtool 综合生成。

---

## 2. 前置知识

- **字面量与硬件值**：`5.U(8.W)` 是硬件常量；`UInt(8.W)` 是纯类型（见 u2-l2）。
- **独热编码（one-hot）**：一个 N 位向量里恰好只有一位为 1，该位的位置即编码值。例如 `0010` 表示数值 1（从 LSB=0 起计）。
- **优先级编码（priority encode）**：向量里可能有多位为 1，取「最低位的那个 1」的位置。
- **位掩码（mask）**：用一个同宽向量标记「哪些位需要比较、哪些位忽略」。`1` 表示关心，`0` 表示忽略。
- **`Mux`**：`Mux(sel, a, b)`，`sel` 为真选 `a`，否则选 `b`（见 u6-l4）。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/main/scala/chisel3/util/BitPat.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/BitPat.scala) | `BitPat` 类本体与 `object BitPat` 工厂（解析字符串、`Y`/`N`/`dontCare`、`===` 实现） |
| [src/main/scala-2/chisel3/util/BitPatIntf.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala-2/chisel3/util/BitPatIntf.scala) | Scala 2 宏桥接：把 `bitpat === uint` 改写为 `do_===`，并给 `UInt` 注入 `=== BitPat` |
| [src/main/scala/chisel3/util/Cat.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Cat.scala) | `Cat` 工厂，委托 `SeqUtils.asUInt` |
| [src/main/scala-2/chisel3/util/CatIntf.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala-2/chisel3/util/CatIntf.scala) | `Cat.apply` 的两个用户重载（变参与 `Seq`） |
| [core/src/main/scala/chisel3/SeqUtils.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/SeqUtils.scala) | `SeqUtils.asUInt`：真正的拼接原语，产出单条 `ConcatOp` |
| [src/main/scala/chisel3/util/OneHot.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/OneHot.scala) | `UIntToOH` / `OHToUInt` / `PriorityEncoder` / `PriorityEncoderOH` |
| [src/main/scala/chisel3/util/Lookup.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Lookup.scala) | `ListLookup` 与 `Lookup`：基于 `BitPat` 的查表 |
| [src/main/scala/chisel3/util/Mux.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Mux.scala) | `PriorityMux` / `Mux1H`，被 OneHot 家族复用 |

> 说明：`BitPatIntf.scala` 与 `CatIntf.scala` 都有 Scala 2 / Scala 3 两个版本（`scala-2/`、`scala-3/` 目录）。本讲引用 Scala 2 版本，宏机制在 Scala 3 中由语言内置 `inline` 等价实现，语义一致。

---

## 4. 核心概念与源码讲解

### 4.1 BitPat：带无关位的位模式

#### 4.1.1 概念说明

`UInt` 的 `===` 是逐位精确比较。但硬件里常需要「只比较其中几位」的匹配，例如：判断一个 4 位操作码 `op` 是否属于「高位是 `10`、最低位随意」的一类。Verilog 用 `x`/`z` 或 `casex` 表达，Chisel 用 `BitPat`。

`BitPat` 是一个**纯软件对象**，不是 `Data`、也不是硬件信号。它只描述「一个带掩码的位模式」，等真正与某个 `UInt` 做 `===` 时，才生成比较电路。

一个 `BitPat` 由三个不可变字段唯一确定（见 [BitPat.scala:294-296](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/BitPat.scala#L294-L296)）：

- `value: BigInt`：字面值，**所有 `?` 位按 0 记**。
- `mask: BigInt`：掩码，**关心的位为 1、`?` 位为 0**。
- `width: Int`：总位宽（含 `?`）。

例如 `BitPat("b1?0")`：

| 字符 | value 贡献 | mask 贡献 |
| --- | --- | --- |
| `1` | 1 | 1 |
| `?` | 0 | 0 |
| `0` | 0 | 1 |

故 `value = 0b100 = 4`，`mask = 0b101 = 5`，`width = 3`。

#### 4.1.2 核心流程

构造流程（字符串 → 三元组）：

```
BitPat("b1?0")
  └─ parse("b1?0")           # 从左到右扫描每个字符
       ├─ '1'/'0' → mask<<1+1, bits<<1+(1或0)
       └─ '?'   → mask<<1+0, bits<<1+0   （下划线/空白跳过）
  └─ new BitPat(bits, mask, width)
```

匹配流程（`bitpat === uint`，核心是「先掩码、再比较」）：

```
bitpat === x   ⟹   bitpat.value.asUInt === (x & bitpat.mask.asUInt)
```

即把 `x` 里**不关心的位清零**，再与「`?` 也按 0 记」的 `value` 做严格相等比较。这样 `?` 位无论 `x` 是 0 还是 1，被掩码清零后都等于 `value` 里的 0，于是被忽略。

#### 4.1.3 源码精读

字符串解析 `parse`（[BitPat.scala:24-45](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/BitPat.scala#L24-L45)）逐字符累加 `bits` 与 `mask`，并要求首字符必须是 `'b'`、合法字符只能是 `0/1/?`（外加可忽略的 `_` 与空白）：

```scala
require(x.head == 'b', "BitPats must be in binary and be prefixed with 'b'")
...
mask = (mask << 1) + (if (d == '?') 0 else 1)
bits = (bits << 1) + (if (d == '1') 1 else 0)
```

工厂入口 `apply(n: String)`（[BitPat.scala:53-56](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/BitPat.scala#L53-L56)）就是 `parse` 后 `new BitPat(...)`。

三个便捷构造器都回归到字符串形式（[BitPat.scala:64-80](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/BitPat.scala#L64-L80)）：

- `BitPat.dontCare(4)` ≡ `BitPat("b????")`
- `BitPat.Y(4)` ≡ `BitPat("b1111")`（全 1）
- `BitPat.N(4)` ≡ `BitPat("b0000")`（全 0）

匹配逻辑 `_impl_===`（[BitPat.scala:325-327](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/BitPat.scala#L325-L327)）正是上文「掩码后比较」的实现：

```scala
protected def _impl_===(that: UInt)(implicit sourceInfo: SourceInfo): Bool =
  value.asUInt === (that & mask.asUInt)
```

`_impl_=/=`（[BitPat.scala:329-331](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/BitPat.scala#L329-L331)）只是对 `===` 取反。

> 为什么用户写 `bitpat === x` 会调到 `_impl_===`？因为 `===` 被声明为宏（[BitPatIntf.scala:41-42](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala-2/chisel3/util/BitPatIntf.scala#L41-L42)），由 `SourceInfoTransform` 宏改写为 `do_===`（[BitPatIntf.scala:51-52](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala-2/chisel3/util/BitPatIntf.scala#L51-L52)），后者调用 `_impl_===`。这套「宏 → `do_x` → `_impl_x`」桥接与 u2-l2 讲过的运算符注入完全同构，目的是顺便注入调用处的 `SourceInfo`（文件名/行号）供报错定位。

反向比较 `x === bitpat`（`UInt` 在左）由隐式类 `uintToBitPatComparable` 提供（[BitPatIntf.scala:31-33](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala-2/chisel3/util/BitPatIntf.scala#L31-L33)），其 `do_===` 直接委托 `that === x`（[BitPatIntf.scala:19](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala-2/chisel3/util/BitPatIntf.scala#L19)），所以两个方向语义对称。

此外还有几个有用的派生：

- `BitPat.apply(x: UInt)`（[BitPat.scala:97-102](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/BitPat.scala#L97-L102)）：把一个 `UInt` **字面量**转成无 `?` 的 `BitPat`（要求 `x.isLit`，否则报错），便于混用在 `BitPat` 列表里。
- `BitPat.apply(x: EnumType)`（[BitPat.scala:106](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/BitPat.scala#L106)）：把 `ChiselEnum` 值转成 `BitPat`。
- `##` 拼接两个 `BitPat`（[BitPat.scala:333-335](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/BitPat.scala#L333-L335)）：纯软件拼接 `value`/`mask`/`width`，不产生硬件。
- `hasDontCares`（[BitPat.scala:409](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/BitPat.scala#L409)）：判断是否含 `?`，判据是 `mask != (1<<width)-1`。

> ⚠️ **重要纠正**：实践任务提到「用 `BitPat` 在 `switch` 中匹配」。但源码中 [`switch`/`is`](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Conditional.scala#L15-L37) 的类型边界是 `SwitchContext[T <: Element]`，且 `is` 强制要求 `w.litOption.isDefined`（[Conditional.scala:23](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Conditional.scala#L23)）。而 `BitPat` 继承自 `BitSet with BitPatIntf`（[BitPat.scala:294-296](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/BitPat.scala#L294-L296)），**既不是 `Element` 也没有 `litOption`**，因此 `BitPat` 不能直接放进 `is(...)`。带无关位的匹配应改用 `when(x === BitPat(...))`（见 4.1.4 与第 5 节），或用专为 `BitPat` 设计的 `Lookup`（见 4.4）。

#### 4.1.4 代码实践

**目标**：观察 `BitPat` 的「掩码后比较」如何落到 Verilog。

**操作步骤**（示例代码，可用 `scala-cli` 或 sbt/mill 工程运行）：

```scala
// 示例代码
import chisel3._
import chisel3.util._

class BitPatDemo extends Module {
  val io = IO(new Bundle {
    val sel = Input(UInt(3.W))
    val hit = Output(Bool())
  })
  // "b1?0"：只比较 bit2==1 且 bit0==0，忽略 bit1
  io.hit := io.sel === BitPat("b1?0")
}

// 触发 Verilog 生成（这一行才真正按下「生成按钮」，见 u1-l4）
val verilog = chisel3.stage.ChiselStage.emitSystemVerilog(new BitPatDemo)
println(verilog)
```

**需要观察的现象**：`emitSystemVerilog` 这一行的调用触发了完整 elaboration；之前的 `IO`、`===` 都只是登记。

**预期结果**：生成的 `assign io_hit` 大致是「`io_sel` 与一个掩码相与后，再与一个常量比较」的形式。由 4.1.2 的推导，`mask = 0b101 = 5`、`value = 0b100 = 4`，故逻辑等价于：

```verilog
assign io_hit = (io_sel & 3'b101) == 3'b100;  // 形式待本地验证，firtool 可能进一步优化
```

把 `io.sel` 分别给 `3'b100`、`3'b110`（都满足 bit2=1,bit0=0，bit1 不同）应都得 `1`；给 `3'b101`（bit0=1）应得 `0`。精确的 Verilog 文本形式**待本地验证**（取决于 firtool 版本与优化等级）。

#### 4.1.5 小练习与答案

**练习 1**：写出 `BitPat("b?1?")` 的 `value`、`mask`、`width`。

**答案**：从左到右 `?`/`1`/`?` → value 仅中间位为 1 = `0b010 = 2`；mask = `0b010 = 2`；width = 4。

**练习 2**：`BitPat(2.U)` 与 `BitPat("b10")` 是否相等？为什么？

**答案**：相等。`BitPat(x: UInt)`（[BitPat.scala:97-102](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/BitPat.scala#L97-L102)）对字面量 `2.U` 取 `litValue=2`、`mask=(1<<2)-1=3`、`width=2`，正好等于 `BitPat("b10")` 的 `(value=2, mask=3, width=2)`。`BitPatSpec` 也断言了这一点。

---

### 4.2 Cat：信号拼接

#### 4.2.1 概念说明

`Cat` 把多个 `Bits` 信号首尾拼成一个更宽的 `UInt`。关键约定：**第一个参数是最高位（MSB），最后一个参数是最低位（LSB）**。例如 `Cat("b101".U, "b11".U)` 等于 `"b10111".U`（`101` 在高 3 位，`11` 在低 2 位）。

#### 4.2.2 核心流程

`Cat` 自身几乎不做事，它把工作完全委托给 `SeqUtils.asUInt`，只是在调用前**反转**一次入参序列：

```
Cat(a, b, c)                      # 用户想要 a=MSB, c=LSB
  └─ _applyImpl(List(a, b, c))
       └─ SeqUtils.asUInt(List(a, b, c).reverse)   # = asUInt(List(c, b, a))
            └─ pushOp(DefPrim(ConcatOp, args.reverse))   # = cat(a, b, c)
```

为什么有「两次 reverse」？因为 `SeqUtils.asUInt` 自身的约定是「序列第一个元素为 LSB」（[SeqUtils.scala:18-24](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/SeqUtils.scala#L18-L24)），而 FIRRTL 的 `cat` 原语要求第一个操作数为 MSB。所以 `asUInt` 内部对 `args` 再反转一次喂给 `ConcatOp`。`Cat` 为了对外给出「第一个参数是 MSB」的直觉约定，就在入口先反转一次抵消。

#### 4.2.3 源码精读

`object Cat` 只有两个受保护的实现方法（[Cat.scala:20-24](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Cat.scala#L20-L24)）：

```scala
protected def _applyImpl[T <: Bits](a: T, r: T*)(implicit sourceInfo: SourceInfo): UInt =
  _applyImpl(a :: r.toList)
protected def _applyImpl[T <: Bits](r: Seq[T])(implicit sourceInfo: SourceInfo): UInt =
  SeqUtils.asUInt(r.reverse)
```

用户可见的两个 `apply` 重载在 [CatIntf.scala:13-31](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala-2/chisel3/util/CatIntf.scala#L13-L31)：变参版 `Cat(a, r: _*)` 与序列版 `Cat(Seq(...))`，二者都声明为宏并改写到 `do_apply` → `_applyImpl`。两个重载都保证「序列/参数的第一个元素是 MSB」。

真正的拼接发生在 `SeqUtils.asUInt`（[SeqUtils.scala:25-50](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/SeqUtils.scala#L25-L50)）：它单趟遍历累加位宽 `width`，再把每个元素的 `ref` **反转后**塞进一条 `DefPrim(..., ConcatOp, ...)`（[SeqUtils.scala:48](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/SeqUtils.scala#L48)）。注意三个边界情形：

- 空序列 → 返回 `0.U`（[SeqUtils.scala:27-28](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/SeqUtils.scala#L27-L28)）。
- 单元素 → 直接 `.asUInt`，不发 `ConcatOp`（[SeqUtils.scala:29-30](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/SeqUtils.scala#L29-L30)）。
- 混入 `SInt` → 先用 `AsUIntOp` 把 `SInt` 转成无符号位再拼（[SeqUtils.scala:13-16](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/SeqUtils.scala#L13-L16)、[SeqUtils.scala:43-44](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/SeqUtils.scala#L43-L44)）。

> `Cat` 与 `VecInit` 的关系：`VecInit` 造的是「硬件向量」（可按下标动态选择，综合出 mux），而 `Cat` 造的是「一个拼接好的扁平 `UInt`」。`VecInit(asUInt=...)` 的底层也复用了这里的 `ConcatOp`（见 u2-l4）。

#### 4.2.4 代码实践

**目标**：把两个 4 位信号拼成 8 位，验证 MSB 约定。

```scala
// 示例代码
import chisel3._
import chisel3.util._

class CatDemo extends Module {
  val io = IO(new Bundle {
    val a = Input(UInt(4.W))
    val b = Input(UInt(4.W))
    val out = Output(UInt(8.W))
  })
  io.out := Cat(io.a, io.b)   // a 在高 4 位，b 在低 4 位
}
```

**预期结果**：生成的 Verilog 中应有形如 `assign io_out = {io_a, io_b};` 的 Verilog 拼接（`{ }` 内左高右低，与 `Cat` 的「第一个参数 MSB」一致）。把 `Cat(io.a, io.b)` 换成 `Cat(io.b, io.a)`，高低位应互换。精确文本**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`Cat(Seq(a, b, c))` 中谁是 MSB？与 `Cat(a, b, c)` 是否一致？

**答案**：`Seq` 的第一个元素 `a` 是 MSB，与变参版 `Cat(a, b, c)` 完全一致。因为序列版 `_applyImpl(r)` 同样执行 `SeqUtils.asUInt(r.reverse)`（[Cat.scala:23-24](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Cat.scala#L23-L24)）。

**练习 2**：若直接调用 `SeqUtils.asUInt(Seq(a, b, c))`（不经过 `Cat`），谁是 MSB？

**答案**：是 `c`（最后一个元素）。`SeqUtils.asUInt` 的约定是「第一个元素为 LSB」，与 `Cat` 相反（[SeqUtils.scala:18-24](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/SeqUtils.scala#L18-L24)）。这正是 `Cat` 入口要 `reverse` 一次的原因。

---

### 4.3 OneHot 家族：独热编解码与优先级编码

#### 4.3.1 概念说明

这一组工具围绕「独热（one-hot）」与「优先级」两种位向量运算：

| 函数 | 输入 | 输出 | 语义 |
| --- | --- | --- | --- |
| `UIntToOH` | 普通数值 `UInt` | 独热向量 | 数值 `n` → 只有第 `n` 位为 1 |
| `OHToUInt` | 独热向量 | 普通数值 `UInt` | `UIntToOH` 的逆，求那个 1 的位置 |
| `PriorityEncoder` | 任意位向量 | 普通数值 `UInt` | 最低位的 1 的位置 |
| `PriorityEncoderOH` | 任意位向量 | 独热向量 | 仅保留最低位的 1 |

`OHToUInt` 假设输入**恰好一位**为 1；`PriorityEncoder` 允许多位为 1（取最低）。

#### 4.3.2 核心流程

**`UIntToOH`**（[OneHot.scala:57-67](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/OneHot.scala#L57-L67)）：无宽度参数时就是一次移位 `1.U << in`；带宽度参数 `width` 时，先把移位量定宽再截取到 `width` 位，避免超出。

**`OHToUInt`**（[OneHot.scala:20-35](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/OneHot.scala#L20-L35)）：本质是求独热向量那个 1 的位置，即 `log2`。它用分治递归把宽度不断对半砍，递归到 `width ≤ 2` 直接用 `Log2`：

```
OHToUInt(in, width):
  若 width ≤ 2:  返回 Log2(in, width)
  否则:
    mid = 2^(⌈log2 width⌉ - 1)        # 中点
    hi  = in 的高半段;  lo = in 的低半段
    return Cat(hi.orR, OHToUInt(hi | lo, mid))
```

每层把「高位是否非零」作为结果的最高有效位（`Cat(hi.orR, ...)`），低有效位由折半后的子问题给出。

**`PriorityEncoder`**（[OneHot.scala:46-49](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/OneHot.scala#L46-L49)）：把输入位向量当成一组 `Bool`，用 `PriorityMux` 从最低位开始选出第一个为 1 的下标。`PriorityMux` 内部是一串嵌套 `Mux`（[SeqUtils.scala:64-78](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/SeqUtils.scala#L64-L78)）。

**`PriorityEncoderOH`**（[OneHot.scala:76-86](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/OneHot.scala#L76-L86)）：同样用 `PriorityMux`，但候选值是各个位置的独热码；末尾追加 `true.B → 0` 兜底，使得「全 0 输入」时输出全 0（而非未定义）。

#### 4.3.3 源码精读

`UIntToOH` 的带宽度分支（[OneHot.scala:59-66](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/OneHot.scala#L59-L66)）：

```scala
def apply(in: UInt, width: Int): UInt = width match {
  case 0 => 0.U(0.W)
  case 1 => 1.U(1.W)
  case _ =>
    val shiftAmountWidth = log2Ceil(width)
    val shiftAmount = in.pad(shiftAmountWidth).apply(shiftAmountWidth - 1, 0)
    (1.U << shiftAmount).apply(width - 1, 0)
}
```

它特意把移位结果 `.apply(width-1, 0)` 截断，保证输出恰好 `width` 位（超出 `width` 的位移到更高位会被丢弃）。

`OHToUInt` 的分治（[OneHot.scala:25-34](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/OneHot.scala#L25-L34)）用 `Cat(hi.orR, apply(hi | lo, mid))`：`hi.orR` 是「高位段是否有 1」，作为本层结果 MSB；`hi | lo` 把高低两段或起来（无论那个 1 在高段还是低段，折半后都能继续定位），递归求低位段。

`PriorityEncoder`（[OneHot.scala:47-48](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/OneHot.scala#L47-L48)）：

```scala
def apply(in: Seq[Bool]): UInt = PriorityMux(in, (0 until in.size).map(_.asUInt))
def apply(in: Bits):      UInt = apply(in.asBools)
```

即「选择信号 = 各位，数据 = 各下标值」，第一个为 1 的位对应的下标即结果。`in.asBools` 把 `Bits` 拆成 `Seq[Bool]`。

`PriorityEncoderOH` 的兜底设计（[OneHot.scala:77-80](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/OneHot.scala#L77-L80)）：候选 `outs` 是每个下标的独热码，再追加 `(true.B, 0.U)`，所以当输入全 0 时所有真选择都失效，兜底选中 `0.U`，输出全 0。

> `PriorityMux` 与 `Mux1H` 都定义在 [Mux.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Mux.scala)：`PriorityMux`（[Mux.scala:56-69](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Mux.scala#L56-L69)）委托 `SeqUtils.priorityMux`（嵌套 `Mux`，优先取第一个高）；`Mux1H` 委托 `SeqUtils.oneHotMux`（假设恰好一位高，可被优化成 and/or 树）。

#### 4.3.4 代码实践

**目标**：验证 `UIntToOH` 与 `PriorityEncoder` 的方向与数值。

```scala
// 示例代码
import chisel3._
import chisel3.util._

class OneHotDemo extends Module {
  val io = IO(new Bundle {
    val idx   = Input(UInt(2.W))
    val mask  = Input(UInt(4.W))
    val oh    = Output(UInt(4.W))
    val pos   = Output(UInt(2.W))
  })
  io.oh  := UIntToOH(io.idx)              // idx=2 → 4'b0100
  io.pos := PriorityEncoder(io.mask)      // mask=4'b0110 → 1（最低位的 1）
}
```

**预期结果**：`io.oh` 是 `1.U << io.idx` 的 4 位独热；`io.pos` 是 `io.mask` 最低位 1 的下标。例如 `mask = 4'b0110` → `pos = 1`、`mask = 4'b1000` → `pos = 3`、`mask = 4'b0000` → 结果未定义（`PriorityEncoder` 注明无 1 时未定义；若需确定行为改用 `PriorityEncoderOH`）。精确 Verilog**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`UIntToOH(2.U)` 的结果是什么？它等价于哪条 Chisel 表达式？

**答案**：结果是 `4'b0100`（即第 2 位为 1）。无宽度参数的 `UIntToOH(in)` 直接就是 `1.U << in`（[OneHot.scala:58](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/OneHot.scala#L58)）。

**练习 2**：为什么 `PriorityEncoderOH` 比 `PriorityEncoder` 更「安全」？

**答案**：`PriorityEncoderOH` 在候选末尾追加了 `(true.B, 0.U)` 兜底（[OneHot.scala:79](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/OneHot.scala#L79)），当输入全 0 时输出确定的 `0`；而 `PriorityEncoder` 在「无 1」时结果是未定义的（[OneHot.scala:44](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/OneHot.scala#L44) 注释）。

---

### 4.4 Lookup / ListLookup：基于 BitPat 的查表

#### 4.4.1 概念说明

`Lookup` 是一个「以 `BitPat` 为键、带默认值」的查表多路选择器：给定地址 `addr`，依次用每个 `BitPat` 去匹配 `addr`，命中则输出对应值，全不命中则输出默认值。它本质是把 `BitPat === addr` 的匹配能力封装成了一张表。

源码注释明确指出这是一个**遗留（holdover from chisel2）**、较少使用的运算符，可能被弃用，**不建议新代码使用**（[Lookup.scala:10-12](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Lookup.scala#L10-L12)）。理解它有助于读懂旧代码，新代码可用 `when` 链或 `MuxLookup` 替代。

#### 4.4.2 核心流程

`Lookup` 是 `ListLookup` 的单值特例（[Lookup.scala:50-53](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Lookup.scala#L50-L53)）：把单个默认值包成单元素 `List`，调用 `ListLookup` 后取 `.head`。

`ListLookup`（[Lookup.scala:30-35](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Lookup.scala#L30-L35)）对「每一列输出」分别用 `foldRight` 叠出一棵 `Mux` 树：

```
ListLookup(addr, default, mapping):
  map = mapping.map { case (pat, row) => (pat === addr, row) }   # 每行算出一个命中 Bool
  对第 i 列:
    default[i] foldRight map:  从后往前叠 Mux(命中, 该行第i列, 上一步结果)
```

因为是从右往左 `foldRight`，**越靠前的映射优先级越高**（命中即覆盖后面的）。

#### 4.4.3 源码精读

`ListLookup.apply`（[Lookup.scala:30-35](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Lookup.scala#L30-L35)）：

```scala
def apply[T <: Data](addr: UInt, default: List[T], mapping: Array[(BitPat, List[T])]): List[T] = {
  val map = mapping.map(m => (m._1 === addr, m._2))   // BitPat === UInt，落到 4.1 的掩码比较
  default.zipWithIndex.map { case (d, i) =>
    map.foldRight(d)((m, n) => Mux(m._1, m._2(i), n))
  }
}
```

注意三处与前文的衔接：

- `m._1 === addr` 正是 4.1 讲的 `BitPat === UInt`（经宏桥接调到 `_impl_===`），因此表项天然支持 `?` 无关位。
- 每个输出列各自一棵独立的 `Mux` 树，列之间互不干扰。
- `default` 用作 `foldRight` 的起点，故没有任何命中时输出默认值。

`Lookup` 单值版（[Lookup.scala:51-52](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Lookup.scala#L51-L52)）：

```scala
def apply[T <: Bits](addr: UInt, default: T, mapping: Seq[(BitPat, T)]): T =
  ListLookup(addr, List(default), mapping.map(m => (m._1, List(m._2))).toArray).head
```

#### 4.4.4 代码实践

**目标**：用 `Lookup` 做一张带无关位的查表，观察其等价于一串 `Mux`。

```scala
// 示例代码
import chisel3._
import chisel3.util._

class LookupDemo extends Module {
  val io = IO(new Bundle {
    val addr = Input(UInt(3.W))
    val out  = Output(UInt(4.W))
  })
  io.out := Lookup(io.addr, default = 0.U,
    Seq(
      BitPat("b1?0") -> 4'hA,   // addr=100 或 110 都命中
      BitPat("b001") -> 4'h1
    )
  )
}
```

**预期结果**：等价于 `when(io.addr === BitPat("b1?0")) { 4'hA } .elsewhen(io.addr === BitPat("b001")) { 4'h1 } .otherwise { 0 }`。`addr = 3'b100` 或 `3'b110` → `4'hA`；`addr = 3'b001` → `4'h1`；其余 → `0`。精确 Verilog**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：若 `mapping` 中两个 `BitPat` 都能匹配同一个 `addr`，哪个生效？

**答案**：**靠前的那个**生效。因为 `ListLookup` 用 `foldRight` 从右往左叠 `Mux`（[Lookup.scala:33](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Lookup.scala#L33)），最右（最靠后）的映射是默认起点，越靠前的映射越晚被包、越在外层，故优先级最高。

**练习 2**：把 `Lookup` 的 `default` 设为 `0.U` 且无任何映射命中时，输出是什么？

**答案**：输出 `default`，即 `0.U`。因为 `default` 是 `foldRight` 的起点，没有任何 `Mux` 条件为真时会一路回落到它。

---

## 5. 综合实践

把本讲四个工具串起来，设计一个**「带掩码的拼装 + 模式命中」**小模块：

- 用 `Cat` 把两个 4 位信号 `a`、`b` 拼成 8 位结果。
- 用 `BitPat` 的无关位匹配判断 `sel` 是否属于 `b1?0` 类模式，命中时把拼装结果取反输出，否则原样输出。
- 额外用 `Lookup` 演示基于 `BitPat` 的查表（作为 `when` 链的对照）。

```scala
// 示例代码
import chisel3._
import chisel3.util._

class MiscToolsDemo extends Module {
  val io = IO(new Bundle {
    val a   = Input(UInt(4.W))
    val b   = Input(UInt(4.W))
    val sel = Input(UInt(3.W))
    val cat = Output(UInt(8.W))   // {a, b}
    val hit = Output(Bool())      // sel 匹配 b1?0
    val lut = Output(UInt(4.W))   // BitPat 查表
  })

  // (1) Cat：a 为高 4 位、b 为低 4 位
  val packed = Cat(io.a, io.b)

  // (2) BitPat 无关位匹配（注意：BitPat 不能进 switch/is，用 when + ===）
  val hit = io.sel === BitPat("b1?0")

  when(hit) {
    io.cat := ~packed           // 命中则取反
  } .otherwise {
    io.cat := packed
  }
  io.hit := hit

  // (3) Lookup：基于 BitPat 的查表（遗留 API，仅作演示）
  io.lut := Lookup(io.sel, default = 0.U,
    Seq(BitPat("b1?0") -> 4'hA, BitPat("b001") -> 4'h1))
}

// 生成 Verilog
object MiscToolsDemoMain extends App {
  println(chisel3.stage.ChiselStage.emitSystemVerilog(new MiscToolsDemo))
}
```

**操作与观察**：

1. 运行 `emitSystemVerilog`，找到 `assign io_cat`、`assign io_hit`、`assign io_lut` 三段逻辑。
2. 核对 `io_cat` 中 `packed` 应形如 `{io_a, io_b}`，验证 `Cat` 的 MSB 约定。
3. 核对 `io_hit` 应是「`io_sel` 与掩码 `3'b101` 相与后等于 `3'b100`」的形式。
4. 把 `BitPat("b1?0")` 改成 `BitPat("b??")`（全无关位），观察 `io_hit` 是否退化为常量 `1`（因为掩码为 0，`addr & 0 == 0 == value(0)` 恒真）——这是验证「掩码后比较」机制的好实验。

> 若想确认「`BitPat` 不能进 `switch/is`」这一结论，可尝试把第 (2) 步改写为 `switch(io.sel) { is(BitPat("b1?0")) { ... } }`，编译期即会因类型不符（`BitPat` 不是 `Element`）或 `litOption` 缺失而报错——这与 4.1.3 引用的源码边界一致。

---

## 6. 本讲小结

- **BitPat** 用 `(value, mask, width)` 三元组描述带无关位的位模式，`?` 位在 `value` 与 `mask` 中都按 0 记；匹配即 `value === (x & mask)`（[BitPat.scala:325-327](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/BitPat.scala#L325-L327)）。它是纯软件对象，不是 `Element`，**不能用于 `switch`/`is`**。
- **Cat** 的「第一个参数是 MSB」靠入口 `reverse` 一次来抵消 `SeqUtils.asUInt` 的「第一个元素是 LSB」约定，最终产出单条 `ConcatOp`（[Cat.scala:23-24](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Cat.scala#L23-L24)、[SeqUtils.scala:48](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/SeqUtils.scala#L48)）。
- **OneHot 家族**四个函数两两互逆/相关：`UIntToOH = 1.U << in`，`OHToUInt` 用分治 `log2`，`PriorityEncoder` 用 `PriorityMux` 取最低位的 1 的位置，`PriorityEncoderOH` 多了「全 0 输出 0」的兜底。
- **Lookup / ListLookup** 把 `BitPat === addr` 的命中封装成按列 `foldRight` 的 `Mux` 树，靠前的映射优先；属遗留 API，不建议新代码使用。
- 四个工具都只是「登记命令」（`DefPrim`/`Connect`/`When`），最终门电路由下游 CIRCT/firtool 综合生成（见 u5-l4）。

---

## 7. 下一步学习建议

- 想看 `Cat` 与 `VecInit.asUInt` 的共享底层，回到 **u2-l4** 复习 `SeqUtils` 的 `ConcatOp`/`oneHotMux`。
- 想系统了解 `Mux` 家族（`Mux`/`Mux1H`/`MuxLookup`/`MuxCase`/`PriorityMux`），阅读 **u6-l4** 与 [Mux.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Mux.scala)。
- `BitPat` 还是 `chisel3.util.experimental.decode`（`TruthTable`/`decoder`，含 QMC 化简）的输入格式，想做译码器可继续阅读 [src/test/scala/chisel3/util/experiemental/decode/TruthTableSpec.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala/chisel3/util/experiemental/decode/TruthTableSpec.scala) 及其被测源码。
- 本单元（u6）到此结束，下一单元 **u7** 进入编译器插件与宏，解释 `SourceInfoTransform` 这类把 `===` 改写为 `do_===` 的宏是如何在编译期工作的。
