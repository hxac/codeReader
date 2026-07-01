# when 条件块与 Reg 寄存器

## 1. 本讲目标

前面四讲我们讲完了「模块怎么搭起来」（u3-l1）、「端口方向怎么定」（u3-l2）、和「端口之间怎么连线」（u3-l3、u3-l4）。但到目前为止，我们描述的还都是**组合逻辑**——输出由当前输入完全决定，电路里没有任何「记忆」。真实的数字电路几乎都离不开**时序逻辑**：电路要能记住上一个时钟周期的值，要能在满足某个条件时才更新状态。本讲就补上这两块拼图。读完本讲你应当能够：

- 说出 `when` / `elsewhen` / `otherwise` 三个条件构造的**硬件语义**：它描述的是多路选择器（mux），而不是软件里的 `if`，两条分支的硬件**都会被生成**，只是连接关系受条件控制。
- 解释 `WhenContext` 如何把链式的 `elsewhen` / `otherwise` 翻译成 FIRRTL 的**嵌套 if-else**（因为 FIRRTL 没有 `elsif`），并说出命令是如何被路由进 `ifRegion` / `elseRegion` 两个 `Block` 的。
- 区分 `Reg` / `RegNext` / `RegInit` 三种寄存器工厂的语义差异：`Reg` 只造不连、`RegNext` 造好就接「下一拍」、`RegInit` 带「复位值」。
- 指出这四个构造最终都遵循 Chisel 的铁律——**只登记不施工**：它们分别向 Builder 压入 `When` / `DefReg` / `DefRegInit` 命令，并把信号 `bind` 成 `RegBinding`。

本讲是时序逻辑的入口，也是下一讲 u3-l6（`Mem` / `SyncReadMem`）的前置。

## 2. 前置知识

本讲默认你已经掌握（来自前置讲义）：

- **只登记不施工**（u1-l4 / u4-l2）：模块构造体里的每一行 `IO` / `:=` / `when` / `Reg` 都只是向 Builder 的命令队列 `pushCommand`，本身不立刻生成硬件或文件；真正生成 Verilog 的是 `ChiselStage.emitSystemVerilog`。
- **Module 与隐式 clock/reset**（u3-l1）：`Module` 混入了 `ImplicitClock` / `ImplicitReset`，会自动创建 `clock` / `reset` 端口；`RawModule` 则**不会**。这一点本讲很关键，因为寄存器必须有 clock（带复位值的还要有 reset）。
- **类型 vs 硬件值**（u2-l1 / u4-l3）：一个 `Data` 是否带 `binding` 决定了它是纯类型还是已绑定的硬件值；`requireIsChiselType` 把关「必须是类型」，`requireIsHardware` 把关「必须是硬件值」。
- **`:=` 与底层 MonoConnect**（u3-l3 / u3-l4）：`a := b` 是单向连线，最终走 `MonoConnect` 算法并发出一条 `Connect` 命令；本讲里 `reg := next` 走的就是同一条路。

还需补充两个本讲要用到的术语：

- **时序逻辑 / 组合逻辑**：组合逻辑的输出只取决于「此刻」的输入；时序逻辑的输出还取决于「历史」，靠寄存器在时钟边沿采样保存。计数器、状态机、流水线都是时序逻辑。
- **同步复位 / 异步复位**：复位信号生效的时机。Chisel 的 `RegInit` 把复位值和 clock、reset 一起交给下游 CIRCT（firtool），最终是同步还是异步取决于 `Module` 使用的 `reset` 类型（`Bool` 同步、`AsyncReset` 异步），本讲不展开。

一个贯穿本讲的直觉模型：把 `when` 想象成「**带条件的接线开关**」，把 `Reg` 想象成「**每过一个时钟沿才刷新一次的小盒子**」。`when` 决定的是「这一拍**该不该接上**某根线」，`Reg` 决定的是「这根线的值要**留到下一拍**」。两者合起来——在 `when` 里给 `Reg` 赋值——就是最经典的时序逻辑写法（如带使能的计数器）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [core/src/main/scala/chisel3/When.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/When.scala) | `when` / `elsewhen` / `otherwise` 的用户入口 `object when`、流式 API 对象 `WhenContext`、`Scope`（If/Else）标记、`localCond` 条件合成。 |
| [core/src/main/scala/chisel3/Reg.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Reg.scala) | 三个寄存器工厂：`object Reg`（只造不连）、`object RegNext`（造好接「下一拍」）、`object RegInit`（带复位值），含单参/双参两种重载。 |
| [core/src/main/scala/chisel3/internal/firrtl/IR.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala) | 内部 IR 节点：`DefReg`（L328）、`DefRegInit`（L330）、`class When`（L458，含 `ifRegion` / `elseRegion` 两个 `Block`）、`Block`（L406，命令容器）。 |
| [core/src/main/scala/chisel3/internal/Builder.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala) | 全局状态：`whenStack`（L546）、`pushWhen` / `popWhen`（L760-L768）、`forcedClock` / `forcedReset`（L884-L891）、`pushCommand`（L895）。 |
| [core/src/main/scala/chisel3/RawModule.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala) | `withRegion`（L70）：把一段代码块挂到指定 `Block` 上执行，使其中发出的命令进入该区域；`addCommand`（L78）：把命令塞进当前 `Block`。 |

## 4. 核心概念与源码讲解

### 4.1 when / elsewhen / otherwise 条件构造

#### 4.1.1 概念说明

`when` 长得像 Scala 的 `if`，但**硬件语义完全不同**，这是本讲最重要的一个反直觉点：

- 软件 `if (cond) { f() } else { g() }`：运行时**只执行** `f()` 或 `g()` 中的一个。
- 硬件 `when (cond) { r := a } otherwise { r := b }`：`a` 和 `b` 两段逻辑**都会被综合成实际电路**，`cond` 只决定「这一拍把谁的值接给 `r`」。本质上 `when` 描述的是一个**多路选择器（mux）**：

\[ \text{r} \leftarrow \text{cond}\ ?\ \text{a}\ :\ \text{b} \]

换句话说，`when` 控制的是**连接关系**，而不是**电路的存在性**。这也是为什么 Chisel 的 scaladoc 把它定义为「Create a `when` condition block, where whether a block of logic is executed or not depends on the conditional」——这里的「executed」要理解成「这一拍的赋值是否生效」。

第二个要点是 `elsewhen` 的实现。FIRRTL（Chisel 的目标 IR）**只有 `when ... else ...`，没有 `elsif`**。源码注释里明确写了这一点：

> Since FIRRTL does not have an "elsif" statement, alternatives must be mapped to nested if-else statements inside the alternatives of the preceeding condition.

所以 `when(c1){...}.elsewhen(c2){...}.otherwise{...}` 必须被翻译成**嵌套**结构：

```
when(c1) { ...分支1... }
else {
  when(c2) { ...分支2... }    // elsewhen(c2) 变成 else 里的嵌套 when
  else { ...otherwise 分支... }
}
```

Chisel 用一个流式 API 对象 `WhenContext` 和「延迟创建 else 区域」的技巧把这个嵌套结构拼出来。

#### 4.1.2 核心流程

`when(cond)(block)` 的执行可以拆成下面几步（先建立直觉，4.1.3 再对着源码看）：

```
when(cond)(block):
  1. 在【当前 Block】里压入一个 When 命令，pred = cond
       （When 内部自带两个空 Block：ifRegion 和【按需创建的】elseRegion）
  2. 把当前 WhenContext 压入全局 whenStack
  3. 把 block 放进 whenCommand.ifRegion 执行
       → block 内发出的命令都会进入 ifRegion（靠 withRegion 切换 currentBlock）
  4. 弹出 whenStack

whenContext.elsewhen(c2)(block2):
  1. 先切进【当前 WhenCommand 的 elseRegion】执行后续代码
  2. 在 elseRegion 里 new 一个新的 WhenContext(c2)
       → 第 4.1.1 步的嵌套就发生在这里：elsewhen 的 When 命令落在父 when 的 elseRegion
  3. 返回新 WhenContext，供链式继续 .otherwise

whenContext.otherwise(block):
  1. 直接把 block 放进【当前 WhenCommand 的 elseRegion】执行
  2. 把 scope 标成 Else（影响条件合成 localCond）
```

关键机制是「**区域（Block）切换**」：Builder 维护一个 `blockStack`，`currentBlock` 永远指向栈顶 `Block`；任何 `pushCommand` 最终都通过 `RawModule.addCommand` 把命令塞进**当前 `Block`**。`withRegion(block)(thunk)` 的作用就是把 `block` 临时压栈、执行 `thunk`、再弹栈，从而让 `thunk` 里发出的命令全部落进指定的 `block`。

`whenStack` 的作用则不同——它不是用来路由命令的，而是让「**嵌在 when 里的 when**」和公共方法 `when.cond` 能感知到外层条件（见练习 4.1.5）。

#### 4.1.3 源码精读

**入口 `object when.apply`**——`when(cond){ block }` 实际只是构造一个 `WhenContext`，注意两个参数都是**按名传递**（`=> Bool` / `=> Any`），条件求值被推迟：

[core/src/main/scala/chisel3/When.scala:30-36](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/When.scala#L30-L36) —— `when` 工厂只做一件事：`new WhenContext(sourceInfo, () => cond, block, Nil)`，把条件和块包成闭包，`altConds` 初始为空。

**`WhenContext` 构造体**是真正的核心，它在构造时就完成了「建命令 + 切区域 + 跑 block」：

[core/src/main/scala/chisel3/When.scala:152-167](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/When.scala#L152-L167) —— 关键三步：①`pushCommand(new When(sourceInfo, cond().ref(sourceInfo)))` 在当前区域建一条 `When` 命令；②`pushWhen(this)` 压入 `whenStack`；③`withRegion(whenCommand.ifRegion){ block }` 把块体放进 if 区域执行。`catch` 里拦住了「在 when 块里 `return`」这种误用并给出友好报错。

**`elsewhen`**——实现「elsif → 嵌套 if-else」的关键就在这里：

[core/src/main/scala/chisel3/When.scala:119-127](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/When.scala#L119-L127) —— 它**先** `withRegion(whenCommand.elseRegion)`（注意是「上一个」when 的 else 区域），**再**在里面 `new WhenContext(..., cond :: altConds)`。新 WhenContext 构造时又会在「当前 Block」（此刻就是父 when 的 elseRegion）里建一条新的 `When` 命令——4.1.1 讲的嵌套就是这么自然产生的。`cond :: altConds` 把前序条件累积起来，供条件合成用。

**`otherwise`**——比 `elsewhen` 简单，不再新建 WhenContext，直接复用当前 when 的 else 区域：

[core/src/main/scala/chisel3/When.scala:136-144](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/When.scala#L136-L144) —— `scope = Some(Scope.Else)` 标记当前在 else 半区，然后把 `block` 放进 `whenCommand.elseRegion` 执行。

**`When` IR 命令**本身非常薄，它只是「谓词 + 两个命令区域」的容器：

[core/src/main/scala/chisel3/internal/firrtl/IR.scala:458-468](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L458-L468) —— `ifRegion` 在构造时立即创建；`elseRegion` 用「懒创建」——只有真正调用 `whenCommand.elseRegion`（即出现 `elsewhen` / `otherwise`）时才 `new Block`。`hasElse` 让下游发射器据此判断要不要生成 `else` 子句。

**区域切换** `withRegion` 和命令落地 `addCommand`：

[core/src/main/scala/chisel3/RawModule.scala:70-82](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/RawModule.scala#L70-L82) —— `withRegion` 把新 `Block` 压入 `blockStack`，使 `currentBlock` 指向它；`addCommand` 把命令塞进 `currentBlock.get`。两者合起来就是「block 体里的命令自动进 ifRegion/elseRegion」的全部秘密。

**条件合成** `localCond`（理解 `when.cond` 与 `Scope` 用）：

[core/src/main/scala/chisel3/When.scala:101-111](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/When.scala#L101-L111) —— `alt` 是「前序所有条件的非」的合取（`!c1 & !c2 & ...`）。If 半区条件为 `alt && cond`，Else 半区为 `alt && !cond`。这正是「elsewhen 只在前序都不成立时才生效」的算术表达。`Scope`（If/Else）定义在 [When.scala:61-73](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/When.scala#L61-L73)。

#### 4.1.4 代码实践

**实践目标**：亲手验证「`when` 是 mux 而不是软件 if」——两分支的硬件都被生成。

**操作步骤**（可在一个 `.scala` 文件或 REPL 里，用 `ChiselStage.emitCHIRRTL` 看中间 IR，最直观）：

```scala
// 示例代码
import chisel3._
import circt.stage.ChiselStage

class MuxDemo extends Module {
  val io = IO(new Bundle {
    val sel = Input(Bool())
    val a   = Input(UInt(8.W))
    val b   = Input(UInt(8.W))
    val out = Output(UInt(8.W))
  })
  // 关键写法：两分支都给 out 赋值
  when(io.sel) {
    io.out := io.a
  }.otherwise {
    io.out := io.b
  }
}
// 打印 CHIRRTL（Chisel 自己的 IR 文本，未经 CIRCT 优化，最能看清 when 结构）
println(ChiselStage.emitCHIRRTL(new MuxDemo))
```

**需要观察的现象**：CHIRRTL 文本里应出现形如 `when io_sel :` ... `else :` 的块，且两个 `connect` 分别落在 when 与 else 下；进而可推断 CIRCT 会把它降级成一个 `mux`。

**预期结果**：CHIRRTL 中 `out` 由一个受 `io_sel` 控制的条件连接驱动，等价于 `out <= io_sel ? io_a : io_b`。最终 Verilog 形态（`assign out = sel ? a : b;`）取决于 firtool，**待本地验证**。

**源码阅读型实践（无需运行）**：在 [When.scala:152](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/When.scala#L152) 处看到 `cond().ref` 在**构造 WhenContext 时**就被求值成谓词。结合「按名传递」，解释为什么 `when` 的条件里可以引用尚未定义好的信号（只要在 `when(...)` 真正执行时它已存在即可）。

#### 4.1.5 小练习与答案

**练习 1**：如果只写 `when(io.sel){ io.out := io.a }` 而**不写** `otherwise`，`io.out` 在 `sel=0` 时是什么？

**答案**：根据 Chisel 的「last-connect（最后连接）」语义，没有 otherwise 时 `out` 在条件不成立时**保持自身**，等价于 `out := sel ? a : out`。对端口 `out` 这会导致「未定义初值」，下游通常会报 `not fully initialized` 之类的问题；对寄存器 `Reg` 则正好是「条件写、否则保持」的有用语义（见 4.2 与综合实践）。

**练习 2**：阅读 [When.scala:52-58](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/When.scala#L52-L58) 的 `when.cond`。在嵌套 `when(a){ when(b){...}.otherwise{ X } }` 中，`X` 处调用 `when.cond` 会得到什么表达式？

**答案**：`when.cond` 返回 `whenStack` 上所有 `WhenContext.localCond` 的合取。`otherwise` 把内层 context 的 scope 置为 `Else`，其 `localCond` 为 `!b`（无前序），外层为 `a`，故结果为 `a && !b`——即「a 成立且 b 不成立」。

---

### 4.2 Reg：基础寄存器

#### 4.2.1 概念说明

`Reg(t)` 创建一个**没有复位值**的寄存器：它在每个时钟上升沿采样「当前连接给它的值」，在两个边沿之间保持不变。注意三点：

1. `Reg` 的参数 `t` 是一个**纯类型**（`UInt(8.W)`、`Vec(4, UInt(8.W))`、`new MyBundle`），不是已接线的硬件值。寄存器的位宽由这个类型模板决定。
2. `Reg` **只造不连**——scaladoc 写得很直白：「Value will not change unless the [[Reg]] is given a connection」。单独的 `val r = Reg(UInt(8.W))` 只声明了一个保持型寄存器，你必须再写 `r := 某个值` 才能驱动它。
3. 寄存器需要 clock。`Reg` 内部取的是 `Module` 的**隐式 clock**（来自 u3-l1）；因此 `RawModule` 里直接调 `Reg` 会因 `forcedClock` 找不到隐式时钟而抛 `Error: No implicit clock.`。

#### 4.2.2 核心流程

```
Reg(source) 调用流程（source 按名传入，求值一次）：
  1. 领一个全局唯一 id（Builder.idGen.value，用于决定是否需要 clone）
  2. requireIsChiselType(t)        —— t 必须是纯类型，否则报错
  3. if (t.isConst) Builder.error  —— 不允许用字面量造寄存器（如 Reg(3.U) 非法）
  4. 必要时 cloneTypeFull          —— 得到一个干净的、未绑定的同类型实例 reg
  5. 取隐式时钟 clock = Builder.forcedClock
  6. reg.bind(RegBinding(模块, currentBlock))   —— 把 reg 标记为寄存器硬件值
  7. pushCommand(DefReg(sourceInfo, reg, clock)) —— 登记一条 DefReg IR 命令
  8. 返回 reg（调用方再自行 reg := next 去驱动）
```

第 6 步的 `RegBinding` 里带了 `Builder.currentBlock`——这一点和 `when` 呼应：如果你在 `when(c){ val r = Reg(...) }` 内部声明寄存器，`r` 的 binding 会记下它属于哪个 `Block` 区域（虽然 `DefReg` 这类**声明**通常会被前移到模块顶层，但 binding 信息保留下来供诊断与命名使用）。

#### 4.2.3 源码精读

[core/src/main/scala/chisel3/Reg.scala:36-48](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Reg.scala#L36-L48) —— `object Reg.apply` 全貌。逐行对应 4.2.2 的步骤；注意 `source: => T` 按名传递但 `val t = source` 只求值一次。

[core/src/main/scala/chisel3/internal/firrtl/IR.scala:328](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L328) —— `DefReg(sourceInfo, id, clock)` 命令节点，注意它**只有 clock、没有 reset、没有 init**，这正是「无复位值寄存器」在 IR 层的体现。

[core/src/main/scala/chisel3/internal/Builder.scala:884-891](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L884-L891) —— `forcedClock` / `forcedReset`。它们从 `currentClock` / `currentReset` 取隐式时钟/复位，取不到就抛异常——这解释了「`RawModule` 里直接 `Reg` 会报 No implicit clock」。

#### 4.2.4 代码实践

**实践目标**：验证「`Reg` 只造不连」——不给连接时寄存器保持自身。

**操作步骤**：

```scala
// 示例代码
import chisel3._
import circt.stage.ChiselStage

class HoldDemo extends Module {
  val io = IO(new Bundle {
    val in  = Input(UInt(8.W))
    val out = Output(UInt(8.W))
  })
  val r = Reg(UInt(8.W))   // 只声明，不连接
  r := io.in               // ← 注释掉这行，观察区别
  io.out := r
}
println(ChiselStage.emitCHIRRTL(new HoldDemo))
```

**需要观察的现象**：CHIRRTL 中应出现一个 `reg r : UInt<8>, clock` 声明，以及 `r <= io.in` 的连接。注释掉 `r := io.in` 后，`r` 没有任何驱动，`out` 将读到一个无初值的寄存器。

**预期结果**：保留连接时 `out` 比输入晚一拍（典型流水线寄存器）；去掉连接后 `r` 悬空，综合行为不确定，**待本地验证**下游报错形态。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Reg(3.U)` 非法？源码哪一行拦截了它？

**答案**：`3.U` 是字面量硬件值（带 `LitBinding`），既不是纯类型又是常量。`Reg.apply` 先用 `requireIsChiselType(t)` 拦「必须是类型」，再用 `if (t.isConst) Builder.error(...)` 拦常量，见 [Reg.scala:39-40](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Reg.scala#L39-L40)。要表达「初值为 3」应改用 `RegInit(3.U)`。

**练习 2**：`Reg(UInt(8.W))` 与 `Reg(UInt())` 在位宽上有何区别？

**答案**：位宽由类型模板决定（scaladoc 顶部示例）：`UInt(8.W)` → 8 位；`UInt()` → `Width()` 未知，留给下游按连接推断，这与 u2-l2 讲的位宽推断规则一致。

---

### 4.3 RegNext：一拍延迟寄存器

#### 4.3.1 概念说明

`RegNext(next)` 是 `Reg` 的「语法糖」，等价于：

```scala
val r = Reg(chiselTypeOf(next))   // 用 next 的类型造一个寄存器
r := next                          // 立刻把 next 接进去
```

也就是说 `RegNext` 把「造 + 连」一步做完，专用于「让信号延迟一拍」的流水线场景。它返回的寄存器当前周期等于 `next` 上一周期的值。

但 `RegNext` 有一个**反直觉**的位宽规则，源码 scaladoc 用很大篇幅强调：**对 `Bits`（叶子数值类型，如 `UInt`/`SInt`），`RegNext` 不会从 `next` 拷贝位宽**——它故意把位宽设成未知 `Width()`，交给下游推断；而对 `Aggregate`（`Bundle`/`Vec`），位宽会照拷。后果是：

```scala
val foo = Reg(UInt(4.W))      // 位宽 4
val bar = RegNext(foo)        // 位宽【未定】，会被推断，而非 4
```

如果你想要确定的位宽，scaladoc 建议别用 `RegNext`，改用 `Reg(chiselTypeOf(foo))` 再 `:=`。

#### 4.3.2 核心流程

```
RegNext(next):
  1. 由 next 构造 model（类型模板）：
       next 是 Bits  → next.cloneTypeWidth(Width())   // 位宽置未知
       next 是其它   → next.cloneTypeFull              // 位宽照拷
  2. val reg = Reg(model)          // 复用 4.2 的 Reg，造一个无复位寄存器
  3. requireIsHardware(next)       // next 必须是硬件值（不能传类型进来）
  4. reg := next                   // 单向连线，发出 Connect 命令（走 MonoConnect）
  5. 返回 reg
```

注意第 1 步对 `Bits` 与非 `Bits` 的区别对待——这就是 4.3.1 那条「反直觉位宽规则」的全部来源。双参版本 `RegNext(next, init)` 把内部的 `Reg(model)` 换成 `RegInit(model, init)`，于是寄存器还带复位值（源码里那条 `// TODO: this makes NO sense` 的注释，正是在吐槽「同时给复位值又给下一拍值」的怪异组合）。

#### 4.3.3 源码精读

[core/src/main/scala/chisel3/Reg.scala:79-90](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Reg.scala#L79-L90) —— 单参 `RegNext.apply`。重点看 `case next: Bits => next.cloneTypeWidth(Width())`：对叶子数值类型强制把位宽清成未知。

[core/src/main/scala/chisel3/Reg.scala:93-104](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Reg.scala#L93-L104) —— 双参 `RegNext.apply(next, init)`，内部调 `RegInit(model, init)`，并同样 `reg := next`。源码 `// TODO: this makes NO sense` 注释直指其语义怪异。

调用链：`RegNext` → `Reg`（4.2）→ `DefReg`；`reg := next` →（u3-l3/4）`MonoConnect` → `Connect` 命令。两条命令（`DefReg` + `Connect`）共同构成一个「延迟寄存器」。

#### 4.3.4 代码实践

**实践目标**：验证 `RegNext` 的「延迟一拍」与「位宽未知」两点。

**操作步骤**：

```scala
// 示例代码
import chisel3._
import circt.stage.ChiselStage

class PipeDemo extends Module {
  val io = IO(new Bundle {
    val in  = Input(UInt(8.W))
    val out = Output(UInt(8.W))
  })
  val p1 = RegNext(io.in)        // 延迟一拍；注意 next 是 UInt(8.W) 但 reg 位宽被设为未知
  io.out := p1                   // 通过 := 连到 out，位宽最终由 io.out(8) 反推
}
println(ChiselStage.emitCHIRRTL(new PipeDemo))
```

**需要观察的现象**：CHIRRTL 里 `reg` 声明的位宽是否写成 `UInt<?>`（未知）而非 `UInt<8>`，再靠 `connect out, p1` 与 `out` 的 8 位反推确定。

**预期结果**：`out` 的波形比 `in` 晚一个时钟周期。位宽推断细节**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`Reg(UInt(4.W))` 与 `RegNext(someUInt4)` 得到的寄存器，位宽分别是什么？

**答案**：前者位宽为 4（由类型模板 `UInt(4.W)` 决定）；后者位宽被 `RegNext` 故意清成未知（`cloneTypeWidth(Width())`），由下游推断。差异根源在 [Reg.scala:81](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Reg.scala#L81)。

**练习 2**：`RegNext` 内部为什么必须 `requireIsHardware(next)`？传一个 `UInt(8.W)`（纯类型）会怎样？

**答案**：`RegNext` 的语义是「延迟 `next` 这个具体信号的值」，所以 `next` 必须是已绑定的硬件值而非类型。若传 `UInt(8.W)`（纯类型），`requireIsHardware` 会报错。这和 `Reg` 相反——`Reg` 要的恰是纯类型（`requireIsChiselType`）。

---

### 4.4 RegInit：带复位值的寄存器

#### 4.4.1 概念说明

`RegInit` 在 `Reg` 基础上加了一个**复位值**：当隐式 `reset` 有效时，寄存器被置为 `init`；否则像普通寄存器一样采样你连接的值。这是写「上电/复位后进入已知状态」电路的标准手段——计数器复位到 0、状态机复位到 Idle 都靠它。

`RegInit` 有两种重载，区别在「类型从哪来」：

- **单参 `RegInit(init)`**：`init` 同时充当「类型模板」和「复位值」。对没有强制位宽的字面量（`1.U`）位宽会被推断；对强制位宽字面量（`1.U(8.W)`）位宽照拷；对非字面量/聚合类型，类型照拷。
- **双参 `RegInit(t, init)`**：`t` 是纯类型模板（决定位宽），`init` 是复位值（硬件值），两者解耦。scaladoc 给了一个等价心智模型：`val x = Reg(t); x := init; x`——但实现上复位值并不是一条普通 `Connect`，而是直接写进 `DefRegInit` 节点。

#### 4.4.2 核心流程

双参版本是本体（单参版本最终也调它）：

```
RegInit(t, init):
  1. requireIsChiselType(t)        —— t 必须是纯类型
  2. reg = t.cloneTypeFull         —— 干净的未绑定副本
  3. 取隐式 clock = forcedClock, reset = forcedReset
  4. reg.bind(RegBinding(模块, currentBlock))
  5. requireIsHardware(init)       —— init 必须是硬件值（字面量/信号都行）
  6. pushCommand(DefRegInit(sourceInfo, reg, clock.ref, reset.ref, init.ref))
       —— 复位值 init 直接进 IR 节点，与 clock/reset 并列
  7. 返回 reg（调用方再 reg := next 提供「非复位时的下一拍值」）
```

单参版本 `RegInit(init)` 多一步：先把 `init` 折算成 model（决定位宽的策略见 4.4.1），再委托给双参版本。

注意：`DefRegInit` **不**产生单独的 `Connect` 命令来表达「复位时 ← init」，复位逻辑直接由该节点携带，下游 CIRCT 据此生成 `reg <= reset ? init : next` 形式的逻辑。

#### 4.4.3 源码精读

[core/src/main/scala/chisel3/Reg.scala:171-182](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Reg.scala#L171-L182) —— 双参 `RegInit.apply`，是 4.4.2 流程的逐行对应。`forcedClock` / `forcedReset` 来自 [Builder.scala:884-891](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L884-L891)。

[core/src/main/scala/chisel3/Reg.scala:187-194](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Reg.scala#L187-L194) —— 单参 `RegInit.apply`。位宽策略的分支：`init: Bits if !init.litIsForcedWidth => cloneTypeWidth(Width())`（无强制位宽字面量→推断），否则 `cloneTypeFull`（照拷）。

[core/src/main/scala/chisel3/internal/firrtl/IR.scala:330](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L330) —— `DefRegInit(sourceInfo, id, clock, reset, init)`：对比 [DefReg（L328）](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L328) 多了 `reset` 和 `init` 两个字段，这正是「带复位」与「不带复位」在 IR 层的唯一差别。

#### 4.4.4 代码实践

**实践目标**：验证 `RegInit` 的复位语义，并对比单参/双参的位宽。

**操作步骤**：

```scala
// 示例代码
import chisel3._
import circt.stage.ChiselStage

class ResetCounterDemo extends Module {
  val io = IO(new Bundle {
    val en   = Input(Bool())
    val out  = Output(UInt(8.W))
  })
  // 单参形式：init=0.U，位宽被推断为所需
  val count = RegInit(0.U(8.W))   // 复位值 0，位宽 8
  when(io.en) {
    count := count + 1.U          // en 为真才自增，否则保持（无 otherwise）
  }
  io.out := count
}
println(ChiselStage.emitCHIRRTL(new ResetCounterDemo))
```

**需要观察的现象**：CHIRRTL 中 `count` 的声明应带 `reset => 0` 形式的复位值；`count <= count+1` 应位于 `when io_en` 之内，说明「条件写、否则保持」。

**预期结果**：复位期间 `count` 为 0；复位释放且 `en=1` 时每拍 +1；`en=0` 时保持。这正是综合实践要用的核心模式。最终 Verilog 的复位风格（同步/异步）取决于 `Module` 的 reset 类型，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`RegInit(0.U)` 与 `RegInit(0.U(8.W))` 得到的寄存器位宽分别是什么？为什么？

**答案**：`RegInit(0.U)`——`0.U` 是无强制位宽字面量，单参版本走 `cloneTypeWidth(Width())`，位宽被推断（按使用处，本例接 8 位 `out` 则推断为 8）；`RegInit(0.U(8.W))`——强制位宽 8，单参版本走 `cloneTypeFull`，位宽照拷为 8。依据 [Reg.scala:188-192](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Reg.scala#L188-L192)。

**练习 2**：`DefRegInit` 节点里为什么没有一条单独的 `Connect` 来表达「复位时写入 init」？

**答案**：复位值 `init` 直接作为 `DefRegInit` 的字段携带（[IR.scala:330](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L330)）。这是一种把「复位语义」直接编码进寄存器声明本身的 IR 设计，下游 CIRCT 据此生成 `reg <= reset ? init : next`，而不需要在命令流里额外发连接。

---

## 5. 综合实践

把本讲的「`when` + `RegInit`」串起来，实现规格里要求的**带使能、带复位的 8 位计数器**，并验证三件事：复位到 0、`en=1` 自增、`en=0` 保持。

```scala
// 示例代码
import chisel3._
import circt.stage.ChiselStage

class EnabledCounter extends Module {
  val io = IO(new Bundle {
    val en    = Input(Bool())
    val clear = Input(Bool())   // 额外：同步清零，演示 when 嵌套
    val cnt   = Output(UInt(8.W))
  })

  val count = RegInit(0.U(8.W))   // 复位值为 0，8 位

  // 用嵌套 when 体现优先级：clear 优先于 en
  when(io.clear) {
    count := 0.U                  // 同步清零
  }.elsewhen(io.en) {
    count := count + 1.U          // 使能自增
  }
  // 没有 otherwise：count 在 (clear=0 且 en=0) 时保持自身 → 这正是「保持」语义

  io.cnt := count
}

object EnabledCounterApp extends App {
  // 1) 看 Chisel 自己的 IR：能直接看到 when/else 与 reg 的 reset 值
  println(ChiselStage.emitCHIRRTL(new EnabledCounter))
  // 2) 生成最终 SystemVerilog（需要本地 firtool）
  println(ChiselStage.emitSystemVerilog(new EnabledCounter))
}
```

**实践步骤**：

1. 把上面的代码放进一个可运行 Chisel 的工程（或在 REPL 用 `:paste`）。建议先用 `./mill chisel[].compile`（见 u1-l2）确认能编译。
2. 先只跑 `emitCHIRRTL`，对照 [When.scala:152-167](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/When.scala#L152-L167) 与 [Reg.scala:171-182](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Reg.scala#L171-L182) 在 IR 文本里找到：① `count` 寄存器声明及其 `reset => 0`；② `when io_clear` 外层、`else` 里嵌套 `when io_en` 的结构（验证 4.1.1 的「elsewhen→嵌套 if-else」）。
3. 再跑 `emitSystemVerilog`（依赖本地 firtool，若未安装可参考 u1-l2 的 circt 下载机制），观察 `count` 的更新逻辑是否形如 `if (reset) ... else if (clear) ... else if (en) ...`。
4. **改参数观察行为**：把 `RegInit(0.U(8.W))` 改成 `RegInit(0.U)`（去掉强制位宽），重新生成，对比 `count` 的位宽声明变化（验证 4.4 练习 1）。
5. **故意制造错误**：把 `val count = RegInit(0.U(8.W))` 改成 `val count = Reg(0.U(8.W))`，编译应被 [Reg.scala:39-40](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Reg.scala#L39-L40) 的 `isConst` 检查拦下——体会 `Reg` 与 `RegInit` 接收参数的本质区别。

**预期结果**（CHIRRTL 层，确定性强）：能清晰看到「寄存器声明带复位值」+「when/else 嵌套下的条件连接」。最终 Verilog 的具体写法随 firtool 版本变化，**待本地验证**。

> 若本地无 firtool：步骤 2 的 CHIRRTL 完全由 Chisel 自身产出，不依赖 firtool，一定能完成；步骤 3 的 Verilog 才需要 firtool。可把步骤 2 作为最低交付。

## 6. 本讲小结

- `when` / `elsewhen` / `otherwise` 描述的是**多路选择器**而非软件 `if`：两分支的硬件都会生成，条件只决定「这一拍把谁的值接上去」。
- 因为 FIRRTL 没有 `elsif`，`elsewhen` 被翻译成**嵌套在父 `when` 的 else 区域里的新 `when`**；这一切由 `WhenContext` 配合 `withRegion` 切换 `Block` 完成，命令通过 `ifRegion` / `elseRegion` 两个 `Block` 容器分流。
- `when` 块体里的命令依然遵循「只登记不施工」——它们被路由进对应 `Block`，最终由 `When` IR 节点（[IR.scala:458](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L458)）承载。
- `Reg(t)` 只造不连、无复位值，位宽取自类型模板，需要隐式 clock；`reg := next` 才驱动它。
- `RegNext(next)` = `Reg(model)` + 立即 `:= next`，专做一拍延迟；对 `Bits` 类型**故意不拷位宽**（设为未知），这是它最易踩的坑。
- `RegInit` 提供「复位值」，单参形式用 `init` 兼当类型模板，双参形式把类型与复位值解耦；复位值直接写进 `DefRegInit` 节点（无单独 `Connect`）。三者都 `bind` 成 `RegBinding` 并发出 `DefReg` / `DefRegInit` 命令。

## 7. 下一步学习建议

- **紧接 u3-l6（Mem / SyncReadMem）**：寄存器只能存「一个」值，要存「一片」值就用存储器。`Mem` / `SyncReadMem` 与 `Reg` 共享同一套 `DefReg` 式的「登记 + binding」机制，但读端口有组合读与同步读之分，是本讲自然的延伸。
- **横向回看 u4（Builder 与 IR）**：本讲反复出现的 `pushCommand` / `Block` / `RegBinding` / `DefReg`，其全局状态机细节在 u4-l1（Builder）、u4-l2（命令记录与内部 FIRRTL IR）、u4-l3（Binding 系统）有完整拆解，读完后会对「为什么 `when` 能用 `Block` 切换」有更底层的理解。
- **动手方向**：试着把综合实践的计数器扩展成「带最大值回绕」的计数器（`when(count === max.U){ count := 0.U }.elsewhen(en){ count := count + 1.U }`），并用 `emitCHIRRTL` 验证嵌套 `when` 的结构是否符合 4.1.1 的预测。
