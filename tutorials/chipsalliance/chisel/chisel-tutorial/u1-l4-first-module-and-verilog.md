# Hello Chisel：第一个模块与 Verilog 生成

## 1. 本讲目标

前几讲我们已经知道：Chisel 是嵌在 Scala 里的硬件构造语言，本身没有独立的编译前端，你写的 Scala 程序在运行时（elaboration 细化）「长出」电路，再交给 FIRRTL/CIRCT（firtool）生成可综合的 SystemVerilog。本讲把这些抽象结论变成**看得见、跑得动**的代码。

学完本讲，你应当能够：

1. 用 `Module` + `IO(new Bundle { ... })` 写出一个完整的硬件模块。
2. 理解 `8.W`、`0.U`、`true.B` 这些字面量背后到底发生了什么。
3. 用 `ChiselStage.emitSystemVerilog` 把模块变成一段 SystemVerilog 字符串，并理解 `firtoolOpts` 参数的作用。
4. 看懂仓库里现成的 `PlusOne` 测试用例，知道如何在自己的代码里复刻它。

## 2. 前置知识

在动手前，先建立三条直觉（如果哪条不清楚，建议先回到对应的前置讲义）：

- **Chisel 代码就是 Scala 代码。** 你写的 `class Adder extends Module { ... }` 是一个普通的 Scala 类，构造它时会顺带「记录」下电路结构。这一点来自 [u1-l1]。
- **构造期只记录，不生成 Verilog。** 真正触发「Scala → Verilog」转换的是 `ChiselStage.emitSystemVerilog(...)` 这一行调用，模块构造体本身不会输出任何文件。这一点同样来自 [u1-l1]。
- **本讲用到的类都在 `chisel3` 包里。** 用 `./mill chisel[].compile`（见 [u1-l2]）确认仓库能编译通过后再动手，能少踩很多环境坑。

三个本讲会反复出现的小术语：

| 术语 | 含义 |
| --- | --- |
| 端口（port） | 模块对外可见的信号，对应 Verilog `module` 的 `input`/`output` |
| 字面量（literal） | 编译期已知的硬件常量，如 `0.U`、`true.B` |
| 方向（direction） | 端口的朝向，用 `Input(...)` / `Output(...)` 指定 |

## 3. 本讲源码地图

本讲涉及的关键文件如下，建议边读讲义边在编辑器里打开它们：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/README.md) | 仓库自带的 `Blinky` 闪烁灯示例，是「最短可运行」的 Chisel 程序 |
| [core/src/main/scala/chisel3/package.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/package.scala) | `chisel3` 包对象，提供 `.U/.S/.B/.W` 字面量的隐式转换 |
| [core/src/main/scala/chisel3/Module.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala) | `Module` 抽象类与 `object Module`，定义模块的构造生命周期 |
| [core/src/main/scala/chisel3/IO.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/IO.scala) | `IO(...)` 工厂，把一个数据类型登记为模块端口 |
| [src/main/scala/circt/stage/ChiselStage.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala) | `ChiselStage.emitSystemVerilog`，触发 elaboration 并调用 CIRCT 产出 Verilog |
| [src/main/scala/chisel3/verilog.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/verilog.scala) | `getVerilogString`，对 `emitSystemVerilog` 的一行封装 |
| [src/test/scala/chiselTests/ModuleSpec.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala/chiselTests/ModuleSpec.scala) | 真实的测试用例 `PlusOne`，本讲把它作为「经过验证」的参照样本 |

> 提示：`circt.stage.ChiselStage` 在 `src/main`（整合层），而 `Module`/`IO`/字面量在 `core`。这种「用户词汇在 core、编译入口在 src/main」的分层正是 [u1-l3] 讲过的子项目划分原则。

## 4. 核心概念与源码讲解

本讲的三个最小模块按「从零件到整车」的顺序讲解：先用字面量造出最小的硬件常量（4.1），再用 `Module`/`IO` 把它们组装成一个模块（4.2），最后用 `ChiselStage.emitSystemVerilog` 把模块「打印」成 Verilog（4.3）。

### 4.1 字面量 `.U` / `.B` / `.W`：Scala 数如何变成硬件常量

#### 4.1.1 概念说明

在 Scala 里，`5` 是一个 `Int`，`true` 是一个 `Boolean`。但硬件世界里的常量需要带**位宽**和**类型**（无符号 `UInt`、有符号 `SInt`、布尔 `Bool`）。Chisel 用一个巧妙的技巧把它们连起来：通过 Scala 的**隐式转换**（implicit conversion），让普通的 Scala 数字「凭空」获得 `.U`、`.S`、`.B`、`.W` 等方法。

于是你可以写出极其自然的代码：

- `5.U` —— 一个无符号硬件常量 5（位宽自动推断）。
- `5.U(8.W)` —— 一个位宽明确为 8 的无符号常量 5。
- `true.B` —— 一个值为真的硬件 `Bool` 常量。
- `8.W` —— 一个「宽度」对象，表示 8 位。

这种写法之所以能工作，是因为 `import chisel3._` 把一组 `implicit class` 引入了作用域。

#### 4.1.2 核心流程

当编译器看到 `5.U` 时，发现 `Int` 上并没有 `U` 方法，于是去隐式作用域里找能「包装」`Int` 的类，找到 `fromIntToLiteral`，于是把 `5` 包装进去，再调用 `.U`。核心流程是：

```
Scala 字面量 5 (Int)
      │  编译器寻找隐式转换
      ▼
fromIntToLiteral(5)  ← 继承自 fromBigIntToLiteral
      │  调用 .U 方法
      ▼
UInt.Lit(bigint = 5, width = Width())  ← 一个未指定位宽的 UInt 硬件常量
```

`.U`、`.U(width)` 的区别只在于传入的 `Width` 对象：不传则是 `Width()`（未知宽度，留给后续推断），传 `8.W` 则是 `Width(8)`（已知 8 位）。

#### 4.1.3 源码精读

字面量的全部秘密都在 `chisel3` 包对象里。先看「Int/BigInt → UInt/SInt/Bool」这一组隐式类，它定义在 [core/src/main/scala/chisel3/package.scala:34-75](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/package.scala#L34-L75)，其中关键的几个方法：

```scala
// 把 BigInt 转成无符号常量，位宽未知（交给后续推断）
def U: UInt = UInt.Lit(bigint, Width())          // package.scala:47
// 把 BigInt 转成有符号常量
def S: SInt = SInt.Lit(bigint, Width())          // package.scala:50
// 指定位宽的版本
def U(width: Width): UInt = UInt.Lit(bigint, width)  // package.scala:54
```

[package.scala:38-44](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/package.scala#L38-L44) 中的 `B` 方法把数字解释成 `Bool`：只有 0 和 1 合法，其它值会被 `Builder.error` 记录为错误（注意是「记录」而不是立刻抛异常，这一点在 [u1-l1] 和后续错误处理讲义里会展开）。

由于 `Int` 和 `Long` 也该享受同样的能力，[package.scala:77-78](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/package.scala#L77-L78) 让它们直接继承自 `fromBigIntToLiteral`：

```scala
implicit class fromIntToLiteral(int: Int) extends fromBigIntToLiteral(int)
implicit class fromLongToLiteral(long: Long) extends fromBigIntToLiteral(long)
```

这就是为什么 `5.U`（`Int`）和 `5L.U`（`Long`）都能工作。

`.B` 还有一个面向 `Boolean` 的版本，定义在 [package.scala:114-123](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/package.scala#L114-L123)，于是 `true.B` / `false.B` 也成立：

```scala
implicit class fromBooleanToLiteral(boolean: Boolean) {
  def B: Bool = Bool.Lit(boolean)
}
```

最后，`.W` 来自 [package.scala:125-127](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/package.scala#L125-L127)：

```scala
implicit class fromIntToWidth(int: Int) {
  def W: Width = Width(int)
}
```

把这些串起来：`8.W` 得到一个 `Width(8)`，`5.U(8.W)` 得到一个 8 位宽、值为 5 的 `UInt` 硬件常量。

#### 4.1.4 代码实践

1. **实践目标**：直观感受「同样的数字，带不带 `.W` 生成的 Verilog 位宽不同」。
2. **操作步骤**：阅读 [README.md:139-150](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/README.md#L139-L150) 里的 `MovingSum3` 示例，注意它的 IO 用的是 `UInt(bitWidth.W)`。然后在心里（或一个最小 Scala 文件里）对比下面两段（**示例代码**，非仓库原有）：

   ```scala
   val a = 5.U        // 位宽未知，由上下文推断
   val b = 5.U(8.W)   // 位宽明确为 8
   ```
3. **需要观察的现象**：当你把含 `a` / `b` 的模块用 `emitSystemVerilog` 生成 Verilog 时，`a` 对应信号的位宽会被推断为容纳 5 所需的最小宽度（3 位），而 `b` 会固定为 8 位。
4. **预期结果**：含 `b` 的版本在 Verilog 中出现 `[7:0]`；含 `a` 的版本位宽取决于它参与运算的上下文。
5. 若无法本地运行，标注「待本地验证」，但可以确定的是：`.U` 不带宽度时传入的是 `Width()`，而 `Width()` 在 Chisel 内部表示「未知宽度」。

#### 4.1.5 小练习与答案

**练习 1**：`5.U`、`5.S`、`5.B` 分别是什么类型？`5.B` 会发生什么？

> **答案**：`5.U` 是 `UInt` 常量；`5.S` 是 `SInt` 常量；`5.B` 会触发 [package.scala:38-44](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/package.scala#L38-L44) 里的 `B` 方法，因为 5 既不是 0 也不是 1，会被 `Builder.error` 记录一条错误，并回退为 `Bool.Lit(false)`。

**练习 2**：为什么 `Int` 上能调用 `.U`，尽管 `Int` 类是 Scala 标准库定义的、我们无法修改？

> **答案**：因为 `import chisel3._` 引入了隐式类 `fromIntToLiteral`（[package.scala:77](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/package.scala#L77)），编译器会自动把 `5` 包装成 `new fromIntToLiteral(5)`，再调用继承来的 `.U` 方法。这就是「嵌入式 DSL」不污染原有类型的关键手法。

### 4.2 `Module` 与 `IO`：定义一个硬件模块

#### 4.2.1 概念说明

有了常量，下一步是定义一个**模块**——它对应 Verilog 里的一个 `module`，有名字、有端口、有内部逻辑。Chisel 里所有模块的基类是 `Module`（对应带隐式 clock/reset 的模块），它的声明非常简短：

```scala
abstract class Module extends RawModule with ImplicitClock with ImplicitReset  // Module.scala:232
```

注意它混入了 `ImplicitClock` 和 `ImplicitReset`——这正是 [u1-l1] 提到的「`Module` 自带隐式 clock/reset」的来源。也就是说，任何继承 `Module` 的模块都会自动获得两个端口：`clock` 和 `reset`，即使你的逻辑根本不用它们。

`IO(...)` 则负责声明「这个模块对外暴露哪些端口」。它接受一个 Chisel **类型**（通常是一个 `Bundle`），把它登记为模块的端口集合，并赋予方向（`Input`/`Output`）。

#### 4.2.2 核心流程

`Module(new MyMod)` 的执行可以粗略拆成三步（细节留到 [u3-l1] 和 [u4-l1]）：

```
Module(new MyMod)
   │
   ├─ 1. evaluate(bc)：进入模块构造函数，执行构造体里的每一行
   │      （IO/RegInit/when 这些行只是把「命令」记录到 Builder，并不立即生成硬件）
   │
   ├─ 2. generateComponent()：把记录下来的命令整理成一个 Component（内部 IR）
   │
   └─ 3. initializeInParent()：如果本模块是被别人实例化的，在外层登记一个实例
```

关键直觉：**模块构造体的每一行都是「记录一条命令」**。`val io = IO(...)` 记录「这是一个端口集合」；`io.out := io.in + 1.U` 记录「out 连到 in+1」。这些命令先攒起来，等整个电路构造完，再统一交给后续阶段处理。

#### 4.2.3 源码精读

先看一个仓库里**真实存在、且被测试验证过**的最小模块。在 [src/test/scala/chiselTests/ModuleSpec.scala:17-27](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala/chiselTests/ModuleSpec.scala#L17-L27) 里：

```scala
class SimpleIO extends Bundle {
  val in  = Input(UInt(32.W))
  val out = Output(UInt(32.W))
}

class PlusOne extends Module {
  val io = IO(new SimpleIO)
  val myReg = RegInit(0.U(8.W))
  dontTouch(myReg)
  io.out := io.in + 1.asUInt
}
```

这段代码演示了本讲的全部要素：`Bundle` 聚合出带方向的端口、`IO(new SimpleIO)` 声明端口、`UInt(32.W)` 用到 4.1 的字面量、`:=` 把输出连到输入加一。

`Module` 自身是一个抽象类，不能直接 `new`，必须用 `Module(new MyMod)` 工厂方法包裹。`object Module` 定义在 [Module.scala:34](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L34)，它的核心实现 `_applyImpl` 在 [Module.scala:36-62](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L36-L62)，第一步就是调用 `evaluate`：

```scala
private[chisel3] def _applyImpl[T <: BaseModule](bc: => T)(...): T = {
  val module: T = evaluate[T](bc)          // Module.scala:38
  ...
}
```

`evaluate` 在 [Module.scala:65-108](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L65-L108) 里执行模块构造体，并调用 `module.generateComponent()`（[Module.scala:92](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L92)）把命令收拢成 IR：

```scala
val componentOpt = module.generateComponent()      // Module.scala:92
for (component <- componentOpt) {
  Builder.components += component                  // Module.scala:94 把本模块加入全局
}
```

`Module` 自动提供的隐式 `clock`/`reset` 端口，定义在 [Module.scala:238-239](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L238-L239)：

```scala
final val clock: Clock = IO(Input(Clock()))(this._sourceInfo).suggestName("clock")
final val reset: Reset = IO(Input(mkReset))(this._sourceInfo).suggestName("reset")
```

而这两个端口之所以能被时序逻辑（`Reg` 等）当作「当前时钟」使用，是因为 [Module.scala:307-313](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L307-L313) 和 [Module.scala:336-342](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L336-L342) 的两个 trait 把它们登记到了 `Builder.currentClock` / `Builder.currentReset`。

`IO` 的实现也很短。`object IO` 在 [IO.scala:10](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/IO.scala#L10)，入口 `apply` 在 [IO.scala:25-64](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/IO.scala#L25-L64)，关键三步是：取到当前模块、校验传入的是「类型」而非已绑定的硬件、把它就地绑定为端口：

```scala
def apply[T <: Data](iodef: => T)(implicit sourceInfo: SourceInfo): T = {
  val module = Module.currentModule.get           // IO.scala:26
  ...
  val data = iodef                                // IO.scala:35 按名取值（每次重新求值）
  requireIsChiselType(data, "io type")            // IO.scala:36 必须是「类型」
  ...
  module.bindIoInPlace(iodefClone)                // IO.scala:62 就地绑定为端口
  iodefClone
}
```

> 注意 `requireIsChiselType`（[IO.scala:36](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/IO.scala#L36)）：`IO(...)` 里只能放「类型」（如 `UInt(8.W)`、`new Bundle{...}`），不能放一个已经接了线的硬件值。类型与硬件值的区别会在 [u4-l3] 的 Binding 系统里深入。

#### 4.2.4 代码实践

1. **实践目标**：动手写一个 8 位加法器 `Adder`，作为本模块的「练手模板」。下面是**示例代码**（仿照 `PlusOne` 的写法，非仓库原有）：

   ```scala
   import chisel3._
   import circt.stage.ChiselStage

   // 8 位加法器：两个无符号输入，一个无符号输出
   class Adder extends Module {
     val io = IO(new Bundle {
       val a   = Input(UInt(8.W))
       val b   = Input(UInt(8.W))
       val sum = Output(UInt(8.W))
     })
     io.sum := io.a + io.b
   }
   ```

2. **操作步骤**：
   - 在你的项目里新建一个 `Adder.scala`，贴入上面的代码。
   - 注意 `UInt(8.W)` 同时用到了 4.1 里的 `.W` 字面量。
   - 这里 `Bundle` 是匿名内部类，字段 `a`/`b`/`sum` 会直接成为 Verilog 端口名（带 `io_` 前缀）。
3. **需要观察的现象**：暂时还不生成 Verilog（下一节才触发）。可以先确认它能通过 Scala 编译。
4. **预期结果**：编译通过、无报错，说明模块结构合法。
5. 关于「`Module` 会带 clock/reset」：`Adder` 是纯组合逻辑，但因为它继承的是 `Module` 而不是 `RawModule`，生成的 Verilog 里**仍会出现 `clock` 和 `reset` 端口**（即使没用到）。这是正常现象，下一节会亲眼看到。

#### 4.2.5 小练习与答案

**练习 1**：把 `val io = IO(new Bundle { ... })` 写成 `val io = new Bundle { ... }`（去掉 `IO`）会发生什么？

> **答案**：去掉 `IO` 后，这个 `Bundle` 只是一个普通对象，不会被登记为模块端口。模块就没有任何对外端口，`io.a + io.b` 里的 `a`、`b` 也就无从谈起。[IO.scala:25-64](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/IO.scala#L25-L64) 的 `apply` 才是「登记端口」的那一步。

**练习 2**：`Module` 与 `RawModule` 的关键区别是什么？如果你不想要默认的 `clock`/`reset`，该用哪个？

> **答案**：`Module` 混入了 `ImplicitClock` 和 `ImplicitReset`（[Module.scala:232](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L232)），自动带 `clock`/`reset` 端口；`RawModule` 不带。纯组合逻辑或想完全自管时钟的场合可以用 `RawModule`，细节见 [u3-l1]。

### 4.3 `ChiselStage.emitSystemVerilog`：把模块变成 Verilog

#### 4.3.1 概念说明

到上节为止，我们只是「记录」了一个模块结构，磁盘上没有任何 Verilog。真正按下「生成按钮」的是 `circt.stage.ChiselStage.emitSystemVerilog`。它做两件事：

1. 触发 elaboration，把 `Module` 记录的命令展开成内部 IR，再转成 FIRRTL IR。
2. 调用 CIRCT（firtool）把 FIRRTL IR 编译成 SystemVerilog 字符串返回。

它还接受一个关键参数 `firtoolOpts`：一个字符串数组，原样透传给底层的 `firtool` 可执行文件，用来控制 Verilog 的风格（比如是否随机化寄存器初值、是否剥离调试信息）。README 的 `Blinky` 示例就是典型用法。

> 顺带一提：`chisel3.getVerilogString(new MyMod)` 是它的一行封装，内部直接调用 `ChiselStage.emitSystemVerilog`。这点在 [u1-l1] 已建立，本节给出源码证据。

#### 4.3.2 核心流程

`emitSystemVerilog` 内部并不手写编译流程，而是把一组 **Phase**（阶段）交给 `PhaseManager` 串起来跑。读者此刻只需记住这条阶段链（含义在 [u1-l5] 和单元 5 详述）：

```
ChiselGeneratorAnnotation(() => gen)   ← 把「待 elaborate 的模块」打包成注解
        │
        ▼
phase（PhaseManager）依次执行：
   Elaborate → AddDebugIntrinsics → Convert → AddDedupGroupAnnotations
            → AddImplicitOutputFile → AddImplicitOutputAnnotationFile
            → Checks → CIRCT
        │
        ▼
EmittedVerilogCircuitAnnotation(a)     ← 从结果注解里取出已生成的 Verilog 电路
        │  .value
        ▼
SystemVerilog 字符串
```

`firtoolOpts` 数组里的每一项会被包成一个 `FirtoolOption` 注解，随其它注解一起进入这条管道，最终被 CIRCT 阶段读取。

#### 4.3.3 源码精读

README 的 `Blinky` 是「最短可运行」范例，见 [README.md:54-81](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/README.md#L54-L81)，其中触发 Verilog 生成的就是这几行：

```scala
object Main extends App {
  // These lines generate the Verilog output
  println(
    ChiselStage.emitSystemVerilog(
      new Blinky(1000),
      firtoolOpts = Array("-disable-all-randomization", "-strip-debug-info")
    )
  )
}
```

注意注释「These lines generate the Verilog output」——README 作者特意标注：**构造 `Blinky` 不会产生任何输出，只有 `emitSystemVerilog` 这行才会**。这正是本讲反复强调的「构造期记录，调用期生成」。

`emitSystemVerilog` 的定义在 [ChiselStage.scala:197-213](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala#L197-L213)：

```scala
def emitSystemVerilog(
  gen:         => RawModule,
  args:        Array[String] = Array.empty,
  firtoolOpts: Array[String] = Array.empty
): String = {
  val annos = Seq(
    ChiselGeneratorAnnotation(() => gen),                       // ChiselStage.scala:203
    CIRCTTargetAnnotation(CIRCTTarget.SystemVerilog)            // ChiselStage.scala:204
  ) ++ (new Shell("circt")).parse(args) ++ firtoolOpts.map(FirtoolOption(_))  // :205
  phase
    .transform(annos)                                           // ChiselStage.scala:207 跑整条管道
    .collectFirst { case EmittedVerilogCircuitAnnotation(a) => a }   // :208 取出 Verilog 电路
    .get
    .value                                                      // ChiselStage.scala:212 取字符串
}
```

这段是本讲最重要的一段源码，逐行解读：

- **第 203 行**：`ChiselGeneratorAnnotation(() => gen)` 把「按名构造模块」的能力包成一个注解。`gen` 是按名参数（`=> RawModule`），所以模块只在管道真正需要时才被构造一次。
- **第 204 行**：`CIRCTTargetAnnotation(CIRCTTarget.SystemVerilog)` 告诉管道「最终目标是 SystemVerilog」（而不是 CHIRRTL、btor2 等）。
- **第 205 行**：`firtoolOpts.map(FirtoolOption(_))` 把每个 firtool 选项字符串包成 `FirtoolOption` 注解；`(new Shell("circt")).parse(args)` 解析额外的命令行参数。这就是 `firtoolOpts` 的传递路径。
- **第 207 行**：`phase.transform(annos)` 跑完整条阶段链。
- **第 208、212 行**：从结果注解里捞出 `EmittedVerilogCircuitAnnotation`，取其 `.value` 即 Verilog 字符串。

那条 `phase` 链定义在 [ChiselStage.scala:57-68](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala#L57-L68)，可以看到 `Elaborate` 在最前、`CIRCT` 在最后：

```scala
private def phase = new PhaseManager(
  Seq(
    Dependency[chisel3.stage.phases.Elaborate],     // :59  跑 elaboration（4.2 讲的 generateComponent 在这里被触发）
    ...
    Dependency[circt.stage.phases.CIRCT]            // :67  调用 firtool 产出 Verilog
  )
)
```

`getVerilogString` 的封装证据在 [verilog.scala:8-30](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/verilog.scala#L8-L30)，单参版本就一行：

```scala
object getVerilogString {
  ...
  def apply(gen: => RawModule): String = ChiselStage.emitSystemVerilog(gen)   // verilog.scala:30
}
```

带参数的版本在 [verilog.scala:42-54](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/verilog.scala#L42-L54)，逻辑和 `emitSystemVerilog` 几乎一致，只是多接受 `annotations`。仓库测试 [ModuleSpec.scala:307-311](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala/chiselTests/ModuleSpec.scala#L307-L311) 就用它断言 `PlusOne` 生成的 Verilog 里包含 `assign io_out = io_in + 32'h1`，说明整条链确实跑通了。

#### 4.3.4 代码实践

1. **实践目标**：把 4.2 的 `Adder` 真正变成 Verilog，并观察 `firtoolOpts` 的效果。下面是**示例代码**（接在 4.2 的 `Adder` 之后，非仓库原有）：

   ```scala
   object AdderMain extends App {
     println(
       ChiselStage.emitSystemVerilog(
         new Adder,
         firtoolOpts = Array("-disable-all-randomization", "-strip-debug-info")
       )
     )
   }
   ```

   如果你已按 [u1-l2] 把本地 Chisel `publishLocal` 到了 Ivy 仓库，那么在一个依赖该 SNAPSHOT 版本的 sbt/mill 子项目里 `run` 这个 `App` 即可打印 Verilog。

2. **操作步骤**：
   - 确认 `Adder`（4.2）已就绪，再新增上面的 `AdderMain`。
   - 运行 `AdderMain`，把打印出的 SystemVerilog 复制出来。
   - 把 `firtoolOpts` 改成 `Array.empty`（即不传任何选项）再跑一次，对比两次输出。
3. **需要观察的现象**：
   - 输出里有 `module Adder(...)`，端口包含 `io_a`、`io_b`、`io_sum`，以及（因为继承自 `Module`）`clock`、`reset`。
   - 有一条形如 `assign io_sum = io_a + io_b;` 的组合逻辑赋值。
   - 带 `-disable-all-randomization` 时，Verilog 里**没有** `RANDOMIZE` 相关的宏块；不带时**会有**。
4. **预期结果**：端口名、`assign` 语句、`clock`/`reset` 的出现与消失都与上述描述一致。
5. 如果当前环境没有 sbt/mill 子项目或无法下载 firtool，则把「能否打印 Verilog」标注为「待本地验证」；但你仍然可以**静态验证**：把上面这段和 [ChiselStage.scala:197-213](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala#L197-L213) 对照，确认 `firtoolOpts` 确实经第 205 行进入了管道。

#### 4.3.5 小练习与答案

**练习 1**：去掉 `firtoolOpts` 参数后，生成的 Verilog 里多出来的 `RANDOMIZE` 宏块是用来做什么的？

> **答案**：firtool 默认会给寄存器和上电状态插入随机初值（用于仿真时发现未初始化的 bug）。`-disable-all-randomization` 关掉这个行为，Verilog 就更干净、更适合阅读。仓库测试 [ModuleSpec.scala:310-316](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/test/scala/chiselTests/ModuleSpec.scala#L310-L316) 正是用「带不带 `RANDOMIZE_REG_INIT`」来区分这两种输出的。

**练习 2**：`ChiselStage.emitSystemVerilog` 和 `ChiselStage.emitCHIRRTL` 都接收一个 `=> RawModule`，它们产出的东西有什么本质区别？

> **答案**：`emitSystemVerilog` 把 `CIRCTTarget` 设为 `SystemVerilog`（[ChiselStage.scala:204](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala#L204)），完整跑到 `CIRCT` 阶段，输出可综合的 SystemVerilog；`emitCHIRRTL` 把目标设为 `CHIRRTL`，停在 elaboration 之后、CIRCT 优化之前，输出的是接近 Chisel 内部 IR 的文本（[ChiselStage.scala:92-113](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala#L92-L113)）。前者给综合工具，后者给人看「Chisel 到底生成了什么 IR」。

## 5. 综合实践

把本讲的三个模块串成一个端到端任务：

**任务**：实现一个参数化的 `Mux2`（二选一多路选择器），并用 `ChiselStage.emitSystemVerilog` 验证。

要求（**示例代码**，需自行实现并运行）：

```scala
import chisel3._
import circt.stage.ChiselStage

class Mux2(val w: Int) extends Module {
  val io = IO(new Bundle {
    val in0 = Input(UInt(w.W))
    val in1 = Input(UInt(w.W))
    val sel = Input(Bool())
    val out = Output(UInt(w.W))
  })
  io.out := Mux(io.sel, io.in1, io.in0)   // sel 为真选 in1，否则选 in0
}

object Mux2Main extends App {
  println(ChiselStage.emitSystemVerilog(new Mux2(8)))
}
```

完成步骤：

1. 用 4.1 的字面量知识解释 `w.W` 为什么能作为 `UInt(...)` 的位宽参数（提示：`w` 是 `Int`，`w.W` 经 `fromIntToWidth` 得到 `Width`）。
2. 用 4.2 的知识确认 `Mux2` 继承 `Module` 后会自动多出 `clock`/`reset` 端口。
3. 用 4.3 的知识运行 `Mux2Main`，在生成的 Verilog 里找到 `assign io_out = ... ? ... : ...;` 形式的三目表达式，并对比「带 `-disable-all-randomization`」和「不带」两种输出。
4. 进阶：把 `Module` 换成 `RawModule`（需自行 `import chisel3.RawModule`），重新生成 Verilog，确认 `clock`/`reset` 端口消失了——这能加深你对 4.2「隐式 clock/reset」的理解。

> 无法本地运行时，至少完成第 1、2 步的静态分析，并把第 3、4 步标注「待本地验证」。

## 6. 本讲小结

- `.U/.S/.B/.W` 都是 `chisel3` 包对象里的**隐式类**方法，把 Scala 的 `Int/Long/BigInt/Boolean` 包装成硬件常量；不带宽度时位宽未知，留给后续推断（[package.scala:34-127](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/package.scala#L34-L127)）。
- `Module` 是带隐式 `clock`/`reset` 的模块基类（[Module.scala:232](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L232)）；`IO(...)` 把一个 `Bundle` 类型登记为模块端口（[IO.scala:25-64](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/IO.scala#L25-L64)）。
- 模块构造体的每一行（`IO`/`RegInit`/`when`/`:=`）只是**记录命令**，`Module` 工厂在 `evaluate`→`generateComponent` 里把它们收拢成内部 IR（[Module.scala:65-108](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Module.scala#L65-L108)）。
- `ChiselStage.emitSystemVerilog` 才是真正触发「Scala → Verilog」的那一行（[ChiselStage.scala:197-213](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala#L197-L213)）；`getVerilogString` 是它的一行封装（[verilog.scala:30](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/verilog.scala#L30)）。
- `firtoolOpts` 字符串数组经 `FirtoolOption(...)` 包成注解后透传给底层 firtool（[ChiselStage.scala:205](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/circt/stage/ChiselStage.scala#L205)），控制 Verilog 的随机化、调试信息等风格。

## 7. 下一步学习建议

本讲你已经能让一个模块「跑出」Verilog。接下来：

- 想看清「构造期记录的命令」到底长什么样，进入 **[u4-l2 命令记录与内部 FIRRTL IR]** 与 **[u4-l1 Builder 全局状态机]**，那里会展开 `pushCommand`、`DefNode`/`Connect` 等内部机制。
- 想系统理解 `emitSystemVerilog` 背后的阶段链 `Elaborate → Convert → Checks → CIRCT`，进入 **[u1-l5 编译流程总览]** 和 **[u5-l1 Stage/Phase 管道]**。
- 想掌握更多数据类型（`Bundle`/`Vec`/`ChiselEnum`），进入 **单元 2 数据类型系统**。
- 想了解连线 `:=`/`<>` 背后的方向检查，进入 **[u3-l2 IO、方向与 Flipped 翻转]** 和 **[u3-l4 连线的内部实现]**。
