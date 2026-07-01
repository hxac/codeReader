# Conditional / Mux / Counter 等条件工具

> 单元 6 · 第 4 讲 · `chisel3.util` 高层条件与计数工具
> 依赖：u3-l5（`when` 条件块与 `Reg` 寄存器）、u2-l5（`ChiselEnum`）

## 1. 本讲目标

学完本讲，你应当能够：

- 用 `switch` / `is` 写出清晰的多路分支（状态机、查表），并说清它底层是怎样被展开成一串嵌套 `when` 的。
- 区分 `Mux` 家族的五位成员：基础 `Mux`、`Mux1H`、`MuxLookup`、`MuxCase`、`PriorityMux`，知道何时该用哪一个，以及它们各自的综合结构（选择器链 vs. AND-OR 树）。
- 用 `Counter` 一行生成一个带使能、带回绕输出的硬件计数器，并解释它何时会“省掉一个 mux”。
- 对同一功能（4 路选择器），分别用 `switch/is` 与 `MuxLookup` 实现，并对比二者生成的 Verilog。

## 2. 前置知识

在进入本讲前，请确认你已经理解以下概念（来自前置讲义）：

- **`when` 的硬件语义**：`when`/`elsewhen`/`otherwise` 不是软件 `if`，它生成的两路硬件都在，条件只决定“这一拍接谁的值”，本质是多路选择器（mux）。FIRRTL 没有 `elsif`，所以 `elsewhen` 会被翻译成嵌套在父 `when` 的 `else` 里的新 `when`（见 u3-l5）。
- **“只登记不施工”**：`when`、`:=`、`Reg` 等构造体本身只向 Builder 的命令队列里追加命令，真正变成 Verilog 是下游 firtool 的事。
- **`ChiselEnum`**：用 `object X extends ChiselEnum { val A = Value }` 定义一组命名硬件常量，枚举类型 `X.Type` 与 `UInt` 同构，可经 `.asUInt` 互转（见 u2-l5）。
- **`RegInit`**：带复位值的寄存器，复位值直接写进 IR 节点（见 u3-l5）。

本讲的全部工具都可以看作“在 `when`/`Mux`/`Reg` 之上再加一层语法糖或生成器”。理解了糖衣下面的那几行 `when` 与 `Mux`，本讲就通了。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/main/scala/chisel3/util/Conditional.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Conditional.scala) | `SwitchContext` 类与真正的 `is` 实现；`object is` 是占位假实现（在 `switch` 外调用会报错）。 |
| [src/main/scala-2/chisel3/util/Switch.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala-2/chisel3/util/Switch.scala) | Scala 2 宏，把 `switch(cond){ is(...){...} ... }` 改写成对 `SwitchContext` 的链式 `.is(...)` 调用。 |
| [core/src/main/scala/chisel3/Mux.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Mux.scala) | 最基础的两选一 `Mux`，登记为 `MultiplexOp` 原语。 |
| [src/main/scala/chisel3/util/Mux.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Mux.scala) | `Mux1H`、`PriorityMux`、`MuxLookup`、`MuxCase` 四个生成器。 |
| [core/src/main/scala/chisel3/SeqUtils.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/SeqUtils.scala) | `Mux1H`/`PriorityMux` 的底层实现 `oneHotMux`/`priorityMux`。 |
| [src/main/scala/chisel3/util/Counter.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Counter.scala) | 内联硬件计数器 `Counter`（类与伴生对象工厂）。 |

> 说明：`switch` 是宏，故它在 `src/main/scala-2`（Scala 2）与 `src/main/scala-3`（Scala 3）下各有一份实现。本讲以 Scala 2 版本为主讲解，二者语义一致。

---

## 4. 核心概念与源码讲解

### 4.1 switch / is：多路分支的语法糖

#### 4.1.1 概念说明

写状态机或查表时，一连串 `when(x === a){...}.elsewhen(x === b){...}.elsewhen(x === c){...}` 既啰嗦又容易写错比较对象。`switch` / `is` 提供了一种更接近 C 语言 `switch-case` 的写法：

```scala
switch(x) {
  is(a) { /* x === a 时 */ }
  is(b) { /* x === b 时 */ }
  is(c) { /* x === c 时 */ }
}
```

它本质上就是上面那一串嵌套 `when`/`elsewhen` 的语法糖——只不过比较对象 `x` 只写一次，每个 `is(v)` 自动生成 `x === v` 的判断。

注意一个关键约束：`is` 的参数**必须是字面量（literal）**，而且**各 `is` 之间必须互斥**。这两条会在编译/细化期被强制检查。这和 C 的 `switch` 很像（C 的 case 也是常量），但 Chisel 是在 Scala 层用 `require` 把规则钉死的。

#### 4.1.2 核心流程

`switch` 之所以能“自动”展开，靠的是一个 Scala 宏。整个流程是：

1. 你写 `switch(cond){ is(a){...}; is(b){...} }`。
2. 编译期，宏 `switch.impl` 读到这块代码，把它当作一个语句块序列。
3. 宏把语句块折叠（`foldLeft`）成一条链：以 `new SwitchContext(cond, None, Set.empty)` 为起点，每遇到一个 `is(params)(body)`，就改写成 `acc.is(params)(body)`。
4. 运行时（细化期），`SwitchContext.is` 真正执行：对每个 `is` 的值做相等比较，把多个候选值用 `||` 连起来，再用 `when`/`elsewhen` 串成嵌套条件块。
5. 最终落到 Builder 命令队列里的，就是一组嵌套的 `When` 命令——和手写 `when/elsewhen` 完全等价。

伪代码描述第 4 步对单个 `is(v1, v2){body}` 的展开：

```
p = (cond === v1) || (cond === v2)      // 候选值的“或”
若已有 when 上下文 w： w.elsewhen(p){ body }   // 接在前一个 is 的 else 里
否则：                when(p){ body }          // 第一个 is 开一个新的 when
```

#### 4.1.3 源码精读

先看宏本体。`switch` 是一个 `object`，`apply` 被标记为宏：

[Switch.scala:26-41](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala-2/chisel3/util/Switch.scala#L26-L41) —— `switch` 宏：把语句块折叠成对 `SwitchContext` 的链式 `.is` 调用。

关键在第 31 行的 `foldLeft`：初始累积值是 `new chisel3.util.SwitchContext($cond, None, Set.empty)`；第 34–35 行用模式匹配认出形如 `is.apply(...)(...)` 的语句，改写成 `$acc.is(...)(...)`；第 36 行规定：**`switch` 块里只能放 `is(...){}`，放别的会直接抛异常**。

再看运行期真正干活的 `SwitchContext.is`：

[Conditional.scala:15-46](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Conditional.scala#L15-L46) —— `SwitchContext` 类与 `is` 方法。

要点：

- 第 23 行 `require(w.litOption.isDefined, "is condition must be literal")`：强制 `is` 的值必须是字面量。
- 第 25 行 `require(!lits.contains(value), "all is conditions must be mutually exclusive!")`：用集合 `lits` 累积已出现的值，强制互斥——重复值直接报错。
- 第 29 行 `def p = v.map(_.asUInt === cond.asUInt).reduce(_ || _)`：把一个 `is` 里的多个候选值（`is(a, b)` 这种写法）先各自比较、再用 `||` 合并成一个布尔条件 `p`。注意它用 `def` 而非 `val`，注释说“让逻辑落在合法位置”。
- 第 30–33 行：把 `p` 接到既有 `when` 链上。第一个 `is` 用 `when(p)(block)`，后续都用 `w.elsewhen(p)(block)`——这正是 u3-l5 讲过的“`elsewhen` = 嵌套进父 `when` 的 else 里的新 `when`”。

最后，那个看起来能用的 `object is` 其实是个“诱饵”：

[Conditional.scala:57-76](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Conditional.scala#L57-L76) —— `object is` 的 `apply` 全部 `require(false, ...)`，调用即报错。

它的存在只是为了让 IDE/编译器认为 `is` 是一个合法符号；真正生效的是宏把 `is(...)` 改写成了 `acc.is(...)`，绕过了这个会报错的对象。注释里写得很直白：“dummy implementation, a macro inside switch transforms this”。

#### 4.1.4 代码实践

**实践目标**：验证 `switch/is` 展开后就是嵌套 `when`，并观察对枚举类型的位宽。

**操作步骤**：

1. 把下面这段保存为 `SwitchDemo.scala`（示例代码，非仓库原有文件）：

```scala
import chisel3._
import chisel3.util.{switch, is}
import chisel3.stage.ChiselStage

object Op extends ChiselEnum { val ADD, SUB, AND, OR = Value }

class ALU extends Module {
  val io = IO(new Bundle {
    val op   = Input(Op())
    val a, b = Input(UInt(8.W))
    val out  = Output(UInt(8.W))
  })
  switch(io.op) {
    is(Op.ADD) { io.out := io.a + io.b }
    is(Op.SUB) { io.out := io.a - io.b }
    is(Op.AND) { io.out := io.a & io.b }
    is(Op.OR)  { io.out := io.a | io.b }
  }
}

object SwitchDemo extends App {
  println(ChiselStage.emitSystemVerilog(new ALU))
}
```

2. 用 mill 在测试里跑，或在你自己的 Chisel 工程里 `run` 它。

**需要观察的现象 / 预期结果**（待本地验证具体行号）：

- 生成的 Verilog 里 `out` 应当是一串嵌套的 `assign out = (op == ...) ? ... : (...)`，即一个优先级 mux 链，与手写 `when/elsewhen` 完全相同。
- `op` 端口的位宽应为 2 位（4 个枚举值，`Op` 工厂自动推断位宽为 \(\lceil \log_2 4\rceil = 2\)，见 u2-l5）。
- 故意把两个 `is` 写成相同值（如两个 `is(Op.ADD)`），细化期会因第 25 行的互斥 `require` 报错。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `switch` 块里塞一句 `io.out := 0.U`（不是 `is(...){}`），会发生什么？
**答案**：编译期宏在第 36 行抛异常 “Cannot include blocks that do not begin with is() in switch.”。`switch` 块内只允许 `is(...){}` 一种语句。

**练习 2**：`is(Op.ADD, Op.OR){ ... }` 这种一个 `is` 写多个值的语义是什么？
**答案**：表示“当条件等于其中任意一个时执行 block”。源码第 29 行把多个值各自比较后用 `||` 合并成一个条件，等价于 `when(io.op === Op.ADD || io.op === Op.OR){ ... }`。

---

### 4.2 Mux 家族：Mux / Mux1H / MuxLookup / MuxCase / PriorityMux

#### 4.2.1 概念说明

`switch/is` 是“命令式”写法（在模块体里一句句写 `:=`）。有时候你想要的是一个“表达式”——一个能直接出现在赋值右值里的多路选择结果。这就是 `Mux` 家族的用途。它们都是**表达式**，返回一个 `Data`。

五个成员，按“选择方式”分类：

| 工具 | 选择方式 | 综合结构 | 典型用途 |
| --- | --- | --- | --- |
| `Mux(c, a, b)` | 单布尔条件 | 一个 2 选 1 mux | 二选一，是所有其他 Mux 的积木 |
| `MuxCase(default, Seq(c0->a, c1->b))` | 多个布尔条件 | mux 链（首个命中优先） | 一组互斥/优先条件 |
| `MuxLookup(key, default, Seq(k0->v0, ...))` | 用 key 比相等 | mux 链（查表） | 按 key 查表，类似 `switch` 但表达式形式 |
| `PriorityMux(sel, in)` | 多个布尔选择 | mux 链（首个命中优先） | 优先编码选择 |
| `Mux1H(sel, in)` | **独热码**选择 | AND-OR 树 | 已知恰好一位为 1 的高速选择 |

两两容易混淆，记两条区分线：

- **`MuxLookup` vs `switch/is`**：功能几乎一样（都是“key 等于某常量时取对应值”），但 `MuxLookup` 是表达式、可写在赋值右值；`switch/is` 是语句块、适合在块里写多行 `:=`。4.1 的 `ALU` 也能用 `MuxLookup` 一行写完（见综合实践）。
- **`Mux1H` vs `PriorityMux`**：都是“多个 `Bool` 选一个值”，但 `Mux1H` 假设选择信号是**独热码**（恰好一位为 1），可以综合成又快又平的 AND-OR 树；`PriorityMux` 不假设独热，按从前往后**优先**选第一个为 1 的，综合成一条 mux 链。

#### 4.2.2 核心流程

**基础 `Mux`**：登记一个 `MultiplexOp` 原语（三参数：条件、真值、假值），结果位宽由 `cloneSupertype` 从两个分支推断。这是积木，下面的都靠它拼。

**`MuxCase(default, mapping)`**：把 `mapping` 反转后逐个 `Mux` 包裹。因为先反转再折叠，**列表里靠前的条件优先级最高**（“returns the first value in mapping that is enabled”）。

**`MuxLookup(key, default, mapping)`**：本质是 `MuxCase` 的特化——把每个 `key -> value` 转成条件 `key === k`。它还带一个优化：如果映射已经覆盖了 key 的所有可能取值，就丢掉 `default`，省一个分支。展开用 `foldLeft`：

\[ \text{out} = \text{Mux}(k_{n-1}\equiv key,\; v_{n-1},\; \ldots \text{Mux}(k_0\equiv key,\; v_0,\; \text{default})\ldots) \]

**`PriorityMux`**：`in` 反转后 `foldLeft`，第一个选择信号为 1 的胜出。

**`Mux1H`**（独热）：对独热选择 \((s_0,\dots,s_{n-1})\) 与数据 \((v_0,\dots,v_{n-1})\)，输出为 AND-OR 树：

\[ \text{out} = \bigvee_{i=0}^{n-1}(s_i \wedge v_i) \]

即“每个被选中的位把自己的数据掩码出来，再把所有结果按位或”。因为独热，最多一项非零，等价于选择。这种结构深度浅、延迟低，是它相对 mux 链的优势。

#### 4.2.3 源码精读

**基础 `Mux`**（在 `core`，不是 `util`）：

[core/Mux.scala:11-40](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Mux.scala#L11-L40) —— 基础 `Mux`。

- 第 23 行 `cloneSupertype(Seq(con, alt), "Mux")`：从两个分支推出结果类型/位宽。
- 第 38 行 `pushOp(DefPrim(sourceInfo, d, MultiplexOp, cond.ref, conRef, altRef))`：登记为 `MultiplexOp` 原语（只登记不施工）。注意它对 `DontCare` 分支做了特殊处理（24–37 行），会自动建一根线并 `:= DontCare`。

**`Mux1H`**：

[util/Mux.scala:26-41](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Mux.scala#L26-L41) —— `Mux1H` 的四个重载。

- 第 34–35 行 `(Bool, T)` 对序列版本直接转交 `SeqUtils.oneHotMux(in)`。
- 第 40 行有个特殊用法：`Mux1H(sel: UInt, in: UInt): Bool = (sel & in).orR`——当数据和选择都是 `UInt` 时，它退化成“按位与再求或”，返回 `Bool`（判断二者是否有公共的 1 位）。

AND-OR 树的真正实现在 `SeqUtils`：

[SeqUtils.scala:85-99](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/SeqUtils.scala#L85-L99) —— `oneHotMux`。

第 97–98 行正是公式 \(\bigvee(s_i \wedge v_i)\) 的直译：`masked = for ((s,i) <- inputs) yield Mux(s, i.asUInt, 0.U)`（选中则取数据，否则掩 0），再 `masked.reduceLeft(_ | _)` 按位或。（对 `SInt`、聚合类型有专门的符号扩展/逐字段处理分支，见 102 行起。）

**`MuxLookup`**：

[util/Mux.scala:78-112](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Mux.scala#L78-L112) —— `MuxLookup`。

- 第 97–108 行的优化：用 `key.widthOption` 算出 key 的取值总数 `keySetSize = 1 << width`；若映射里的不同字面量 key 已经覆盖了全部取值（`distinctLitKeys.size == keySetSize`），就把 `default` 丢掉、用第一个映射值当默认，省一个分支。
- 第 110 行 `mappingx.foldLeft(defaultx) { case (d, (k, v)) => Mux(k === key, v, d) }`：用基础 `Mux` 把查表折叠成 mux 链。
- 第 80–87 行 `_applyEnumImpl`：对 `EnumType` 类型的 key，先 `.asUInt` 转成 `UInt` 再走 `UInt` 版本——所以枚举也能直接当 key。

**`MuxCase` 与 `PriorityMux`**：

[util/Mux.scala:121-134](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Mux.scala#L121-L134) —— `MuxCase`：第 129 行 `mapping.reverse` 后逐个 `Mux(t, v, res)`，故**首个条件优先**。

[SeqUtils.scala:64-77](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/SeqUtils.scala#L64-L77) —— `priorityMux`：第 73 行 `in.view.reverse` 后 `foldLeft`，第一个选择信号胜出（`PriorityMux` 在 [util/Mux.scala:56-69](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Mux.scala#L56-L69) 转调它）。

#### 4.2.4 代码实践

**实践目标**：体会 `Mux1H` 的独热 AND-OR 结构与 `MuxLookup` 的 mux 链结构在 Verilog 里的差异。

**操作步骤**（示例代码）：

```scala
import chisel3._
import chisel3.util.{Mux1H, MuxLookup}

class MuxDemo extends Module {
  val io = IO(new Bundle {
    val onehot = Input(UInt(3.W))   // 独热：001/010/100
    val key    = Input(UInt(2.W))   // 0/1/2
    val d      = Input(Vec(3, UInt(8.W)))
    val oh     = Output(UInt(8.W))
    val lk     = Output(UInt(8.W))
  })
  // 独热选择：AND-OR 树
  io.oh := Mux1H(io.onehot, io.d)
  // 按 key 查表：mux 链
  io.lk := MuxLookup(io.key, 0.U, Seq(0.U -> io.d(0), 1.U -> io.d(1), 2.U -> io.d(2)))
}
```

**需要观察的现象 / 预期结果**（待本地验证具体表达）：

- `io.oh` 的逻辑应是 `d[0]&onehot[0] | d[1]&onehot[1] | d[2]&onehot[2]`（AND-OR 树），延迟路径浅。
- `io.lk` 的逻辑应是嵌套三目 `key==2 ? d[2] : (key==1 ? d[1] : d[0])`（mux 链），延迟路径较深。
- 由于此处的 `MuxLookup` 映射已覆盖 2 位 key 的全部 4 个取值中的 3 个（未覆盖 3），`default = 0.U` 仍会保留；若补上 `3.U -> ...` 覆盖全部 4 个值，`default` 会被优化掉（验证第 97–108 行的行为）。

#### 4.2.5 小练习与答案

**练习 1**：`Mux1H` 的选择信号如果不是独热码（比如同时两位为 1），结果会怎样？
**答案**：源码注释明确 “results unspecified unless exactly one select signal is high”。由 AND-OR 公式，两位同时为 1 时输出会是两个数据的按位或，并非任一原值——所以调用方必须保证独热。

**练习 2**：想实现“一组优先级中断，序号小的优先”，该用 `Mux1H` 还是 `PriorityMux`？
**答案**：用 `PriorityMux`。中断请求通常不保证独热，需要“首个命中优先”的语义；`Mux1H` 要求独热，会把多个同时有效的请求错误地按位或。

---

### 4.3 Counter：内联硬件计数器

#### 4.3.1 概念说明

计数器是时序电路里最常见的元件。你当然可以每次手写 `val cnt = RegInit(0.U(n.W)); cnt := Mux(cnt === max, 0.U, cnt + 1.U)`，但 `chisel3.util.Counter` 把它封装成一行：

```scala
val (cnt, wrap) = Counter(io.en, 4)   // en 为真时计数，0→1→2→3→0，到顶时 wrap 为真
```

它有两个关键特点：

- **内联**：`Counter` 不是一个 `Module`，它直接在你当前模块里“长出”一个寄存器和若干逻辑，不会产生实例层次（注释里写明 “inline ... no internal Module is created”）。
- **返回值是元组 `(value, wrap)`**：`value` 是当前计数值（一个 `UInt`），`wrap` 是一个 `Bool`，表示“本拍 en 有效且当前值已到顶，下一拍会回绕”。

#### 4.3.2 核心流程

`Counter` 的核心是“一个 `RegInit` 寄存器 + 回绕逻辑”。以 `Counter(n)`（即范围 `0 until n`，步长 1）为例：

1. 构造时算出位宽 `width = log2Up(last+1)`（对 `0 until n` 即 \(\lceil\log_2 n\rceil\)）。
2. `value` 是一个 `RegInit(0.U(width.W))`。
3. `inc()` 被调用时：先算 `wrap = value === last.U`；然后无条件 `value := value + 1.U`；**如果**回绕不能靠自然溢出完成（见下），再补一个 `when(wrap){ value := 0.U }`。

一个很精巧的优化（源码注释 “avoid wasting an extra mux”）：当范围是从 0 开始、且计数长度是 2 的幂时（如 `Counter(4)`，范围 `0..3`，2 位寄存器），`3 + 1` 在 2 位里自然溢出回 0，**不需要额外的回绕 mux**；否则（如 `Counter(5)`，范围 `0..4`，3 位寄存器，`4+1=5` 不会溢出回 0）就必须显式 `when(wrap){ value := 0.U }`。

工厂方法把 `inc()` 用 `when(cond)` 包起来，实现“条件计数”：

```
apply(cond, n):
  c = new Counter(n); wrap = WireInit(false.B)
  when(cond) { wrap := c.inc() }       // 只有 cond 为真才自增、才可能 wrap
  返回 (c.value, wrap)

apply(r, enable, reset):
  when(reset) { c.reset() }            // reset 优先
  .elsewhen(enable) { wrap := c.inc() }
```

#### 4.3.3 源码精读

**类本体与状态**：

[Counter.scala:30-63](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Counter.scala#L30-L63) —— `class Counter`。

- 第 34 行 `delta = math.abs(r.step)`：支持非 1 步长（如 `Counter(0 until 10 by 2)`）。
- 第 35 行 `width = math.max(log2Up(r.last + 1), log2Up(r.head + 1))`：位宽取“首尾各自所需”的较大值，保证正反计数都能表示。
- 第 58 行 `def this(n: Int) = this(0 until math.max(1, n), Some(n))`：`Counter(n)` 实际是 `0 until n` 的语法糖，`math.max(1,n)` 防止 `n=0` 产生空范围。
- 第 61 行 `val value = if (r.length > 1) RegInit(r.head.U(width.W)) else WireInit(r.head.U)`：单步范围（length==1）退化成一根常量线，连寄存器都不建。

**自增与回绕**：

[Counter.scala:71-94](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Counter.scala#L71-L94) —— `inc()`。

- 第 73 行 `val wrap = value === r.last.U`：到顶判断。
- 第 75–81 行：按 `step` 正负决定加还是减，加减量都是 `delta`。
- 第 86–88 行 `if (!(r.head == 0 && isPow2(r.last + delta))) { when(wrap) { value := r.head.U } }`：正是上面说的“能自然溢出就省 mux”的优化。
- 第 91–93 行：单步范围恒返回 `true.B`。

**工厂方法**：

[Counter.scala:102-142](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Counter.scala#L102-L142) —— `object Counter`。

- 第 106 行 `apply(n: Int): Counter`：只造计数器、不自增，调用方需自己择机调 `c.inc()`。
- 第 115–120 行 `apply(cond: Bool, n: Int): (UInt, Bool)`：最常用入口，`when(cond){ wrap := c.inc() }`，返回 `(value, wrap)`。
- 第 130–141 行 `apply(r: Range, enable, reset): (UInt, Bool)`：支持任意范围与 `reset`/`enable`，注意 `when(reset).elsewhen(enable)`——复位优先于计数。

#### 4.3.4 代码实践

**实践目标**：观察 `Counter(4)` 与 `Counter(5)` 在 Verilog 里回绕逻辑的差异，验证“省 mux”优化。

**操作步骤**（示例代码）：

```scala
import chisel3._
import chisel3.util.Counter
import chisel3.stage.ChiselStage

class CntDemo extends Module {
  val io = IO(new Bundle {
    val en = Input(Bool())
    val v4 = Output(UInt(2.W))
    val w4 = Output(Bool())
    val v5 = Output(UInt(3.W))
    val w5 = Output(Bool())
  })
  val (c4, w4) = Counter(io.en, 4)   // 0..3，2 位，自然溢出
  val (c5, w5) = Counter(io.en, 5)   // 0..4，3 位，需显式回绕
  io.v4 := c4; io.w4 := w4
  io.v5 := c5; io.w5 := w5
}
```

**需要观察的现象 / 预期结果**（待本地验证具体 RTL）：

- `c4` 的寄存器更新逻辑里**没有** “等于 3 就清零” 的 mux，只有 `c4 <= c4 + 1`（靠 2 位自然溢出回 0）。
- `c5` 的寄存器更新逻辑里**有**一个 `c5 == 4 ? 0 : c5 + 1` 的选择，对应第 86–88 行补的 `when(wrap)`。
- `wrap` 信号（`w4`/`w5`）应同时依赖 `en` 与“当前值到顶”，符合 `when(en){ wrap := (value === last) }`。

#### 4.3.5 小练习与答案

**练习 1**：`Counter(0 until 10 by 2)` 的计数序列和位宽各是多少？
**答案**：序列 `0,2,4,6,8`（步长 2，`delta=2`），`width = max(log2Up(8+1), log2Up(0+1)) = max(4,1) = 4` 位。`inc()` 里 `value := value + 2.U`，到 `8` 后回 `0`（`last=8`，`isPow2(8+2)=isPow2(10)=false`，故有显式回绕）。

**练习 2**：为什么 `apply(cond, n)` 把 `c.inc()` 放在 `when(cond)` 里，而不是无条件自增？
**答案**：这样 `cond` 就是“计数使能”——`cond` 为假时寄存器保持不变（`inc()` 根本不执行，不产生 `value := value + 1` 命令），从而实现门控计数；同时 `wrap` 也只有在 `cond` 为真且到顶时才为真，语义正确。

---

## 5. 综合实践

把本讲三个工具串起来：用**两种等价写法**实现同一个“4 路选择器”，并对比生成的 Verilog。

**任务**：给定一个 2 位的 `sel`（或枚举）和 4 个 8 位输入，输出被选中的那个。分别用：

- (A) `switch` / `is`（基于 `ChiselEnum`）；
- (B) `MuxLookup`。

**参考实现**（示例代码，可放入一个文件编译）：

```scala
import chisel3._
import chisel3.util.{switch, is, MuxLookup}
import chisel3.stage.ChiselStage

object Sel extends ChiselEnum { val S0, S1, S2, S3 = Value }

class Mux4Switch extends Module {           // 写法 A：switch/is
  val io = IO(new Bundle {
    val sel = Input(Sel())
    val in  = Input(Vec(4, UInt(8.W)))
    val out = Output(UInt(8.W))
  })
  switch(io.sel) {
    is(Sel.S0) { io.out := io.in(0) }
    is(Sel.S1) { io.out := io.in(1) }
    is(Sel.S2) { io.out := io.in(2) }
    is(Sel.S3) { io.out := io.in(3) }
  }
}

class Mux4Lookup extends Module {           // 写法 B：MuxLookup
  val io = IO(new Bundle {
    val sel = Input(Sel())
    val in  = Input(Vec(4, UInt(8.W)))
    val out = Output(UInt(8.W))
  })
  io.out := MuxLookup(io.sel.asUInt, 0.U,
    Seq(Sel.S0.asUInt -> io.in(0), Sel.S1.asUInt -> io.in(1),
        Sel.S2.asUInt -> io.in(2), Sel.S3.asUInt -> io.in(3)))
}
```

**操作步骤**：

1. 分别用 `ChiselStage.emitSystemVerilog(new Mux4Switch)` 和 `ChiselStage.emitSystemVerilog(new Mux4Lookup)` 打印 Verilog。
2. 用 `diff` 或肉眼对比两段 `out` 的赋值逻辑。

**需要观察的现象 / 预期结果**（待本地验证）：

- 两者功能等价：`out` 都是按 `sel` 从 4 个输入里选一个。
- 写法 A 的 `out` 是嵌套三目（`switch` 展开成嵌套 `when`）；写法 B 的 `out` 也是嵌套三目（`MuxLookup` 折叠成 mux 链）。两者的综合结果在结构上高度相似——这正说明 `switch/is` 与 `MuxLookup` 是同一件事的“语句版”与“表达式版”。
- 进阶观察：写法 B 因为 4 个 `asUInt` 值覆盖了 2 位 `sel` 的全部 4 个取值，`MuxLookup` 第 97–108 行的优化会生效，`default = 0.U` 被丢弃，最终只有 3 个 mux 而非 4 个。写法 A 没有这种“全覆盖省默认”的优化（它没有 default 概念，未命中的 `is` 不赋值即保持）。

**思考题**：如果把上面 4 路 `is`/映射减少到 3 路（即 `sel` 有一个取值未覆盖），写法 A 和写法 B 的 `out` 在未覆盖取值下分别是什么？（提示：A 不赋值→保持上次/未驱动；B 命中 `default`。）

---

## 6. 本讲小结

- `switch` / `is` 是嵌套 `when`/`elsewhen` 的语法糖：一个 Scala 宏（[Switch.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala-2/chisel3/util/Switch.scala)）把语句块改写成对 `SwitchContext` 的链式 `.is` 调用，`SwitchContext.is`（[Conditional.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Conditional.scala)）再生成 `cond === v` 的判断；`is` 的值必须是字面量且互斥。
- `Mux` 家族是“表达式版”的条件选择：基础 `Mux`（[core/Mux.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Mux.scala)，登记 `MultiplexOp`）是积木；`MuxLookup`（查表，mux 链，带“全覆盖省 default”优化）是 `switch` 的表达式对应物；`Mux1H`（独热，AND-OR 树）与 `PriorityMux`（首个命中优先，mux 链）用于多 `Bool` 选择。
- `Counter`（[Counter.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Counter.scala)）是内联计数器，返回 `(value, wrap)` 元组；它在范围从 0 开始且长度为 2 的幂时会省掉回绕 mux，靠寄存器自然溢出回 0。
- 选择工具的依据：要“在块里写多行赋值”用 `switch/is`；要“一个表达式结果”用 `Mux*`；选择信号独热用 `Mux1H`，否则按优先级用 `PriorityMux`/`MuxCase`。
- 所有这些工具都遵循“只登记不施工”：`switch` 落成 `When` 命令，`Mux` 落成 `DefPrim(MultiplexOp)`，`Counter` 落成 `DefRegInit`+`Connect`，真正的 Verilog 由下游 firtool 产出。

## 7. 下一步学习建议

- **本单元后续**：下一讲 u6-l5 会讲 `BitPat` / `Cat` / `OneHot` / `Lookup` 等位运算工具。其中 `BitPat` 可以直接用在 `is(...)` 里做“带 don't-care 位的模式匹配”，是本讲 `switch/is` 的自然延伸；`OneHot`/`PriorityEncoder` 则与本讲的 `Mux1H`/`PriorityMux` 配合使用（先编码出独热或优先位，再选择）。
- **回到状态机**：本讲的 `switch` + `ChiselEnum` + `Counter` 已经足够写一个完整的有限状态机（FSM）。建议结合 u2-l5 的枚举，自己写一个简易自动售货机或分频器作为练习。
- **深入积木**：若想理解 `Mux` 登记的 `MultiplexOp` 原语如何被序列化和 lowers，可回到 u4-l2（内部 FIRRTL IR）与 u4-l5（Converter）追看 `DefPrim` 的处理路径。
