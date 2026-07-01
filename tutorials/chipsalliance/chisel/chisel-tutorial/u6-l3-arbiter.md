# Arbiter：仲裁器

## 1. 本讲目标

本讲讲解 Chisel 标准库 `chisel3.util` 中的仲裁器。读完本讲，你应当能够：

- 读懂 `ArbiterIO` 的端口契约，理解为什么 `in` 要 `Flipped`、`chosen` 为什么是 `UInt`。
- 用一行 `Module(new Arbiter(UInt(8.W), 4))` 搭出一个固定优先级仲裁器，并解释"低编号优先"是怎么用一段 `for` 循环 + 优先编码器实现的。
- 说出 `Arbiter`（固定优先级）与 `RRArbiter`（轮询 / Round-Robin）的本质区别：后者靠一个 `lastGrant` 寄存器避免高优先级输入"饿死"其它输入。
- 知道 `LockingArbiter` / `LockingRRArbiter` 这两个带"锁"变体的存在及其用途（多拍事务不被打断）。

本讲是 u6-l1（Decoupled / ReadyValidIO）的直接下游——仲裁器就是把若干个 `Decoupled` 输入选出一个接到输出的标准生成器。

## 2. 前置知识

在进入仲裁器之前，请确保你理解以下概念（前几讲已建立）：

- **Decoupled / ReadyValidIO 握手协议**：一个 `Decoupled(gen)` 信号由 `valid`、`ready`、`bits` 三根线组成；只有同一拍 `valid && ready` 同时为高，才发生一次数据传输，称为 `fire`。`Decoupled` 的方向约定是"原样即生产者"：`valid`/`bits` 是输出、`ready` 是输入。详见 u6-l1。
- **Flipped 翻转方向**：`Flipped(x)` 把一个 Bundle 内所有信号方向取反。仲裁器作为"消费者"接收多个生产者输入，故 `in` 端口要整体 `Flipped`。详见 u3-l2。
- **Vec 硬件向量**：`Vec(n, Decoupled(gen))` 表示 n 个同类型的 Decoupled 端口，可用 `in(i)` 索引。详见 u2-l4。
- **when 的"最后连接胜出"语义**：同一拍里多条对同一信号的赋值，最后一条条件成立的 `when` 生效。详见 u3-l5。
- **只登记不施工**：模块构造体里的每一行只是向 Builder 登记命令，真正的 Verilog 由下游 firtool 产出。详见 u4-l1 / u5-l4。

一个直觉性的问题先放在脑子里：**如果有 4 个模块都要往同一个输出端口发数据，同一时刻该让谁发？** 仲裁器就是回答这个问题的硬件。

## 3. 本讲源码地图

本讲全部源码集中在一个文件里：

| 文件 | 作用 |
|------|------|
| [src/main/scala/chisel3/util/Arbiter.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Arbiter.scala) | 仲裁器的全部实现：接口 `ArbiterIO`、核心原语 `ArbiterCtrl`、固定优先级 `Arbiter`、轮询 `RRArbiter`，以及带锁变体 `LockingArbiter` / `LockingRRArbiter` 和它们的公共父类 `LockingArbiterLike`。 |

辅助但不在本文件内的工具（用到时点一下）：

| 符号 | 出处 | 用途 |
|------|------|------|
| `Decoupled(gen)` | `src/main/scala/chisel3/util/Decoupled.scala` | 包装出 ready/valid/bits 三件套 |
| `RegEnable(next, en)` | `src/main/scala/chisel3/util/Reg.scala` | 带 enable 的寄存器，RRArbiter 用它记 `lastGrant` |
| `Counter(n)` | `src/main/scala/chisel3/util/Counter.scala` | 0..n-1 循环计数器，锁变体用来数锁定拍数 |
| `log2Ceil(n)` | `core/src/main/scala/chisel3/util/...`（chisel3 包对象导出） | 算 `chosen` 索引所需的位宽 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：先看接口 `ArbiterIO`，再吃透核心积木 `ArbiterCtrl`（它是后面所有仲裁策略的算法基础），然后分别讲固定优先级的 `Arbiter` 和轮询的 `RRArbiter`。

---

### 4.1 ArbiterIO：仲裁器的端口契约

#### 4.1.1 概念说明

仲裁器（Arbiter）做的事可以用一句话概括：**从 n 个请求输入里，至多挑出一个，送到唯一的输出上。** 这里的"输入"和"输出"都遵循 Decoupled 握手协议——每个输入是一个会喊 `valid`、能接 `ready`、带 `bits` 数据的生产者；输出也是一个 Decoupled，面向下游消费者。

除了"被选中的那个数据"之外，硬件上往往还想知道"到底选了第几个输入"，于是多出一根 `chosen`（被选中的编号）。这就是 `ArbiterIO` 的三个字段：`in`、`out`、`chosen`。

一个关键的方向细节：仲裁器是 n 个生产者的**消费者**，所以从仲裁器自身看，`in` 这组 Decoupled 的方向要与默认相反（`ready` 要变成输出、`valid/bits` 要变成输入），这正是 `Flipped` 的作用。

#### 4.1.2 核心流程

`ArbiterIO` 只是一个 Bundle 定义，流程上很简单：

1. 用 `gen`（数据类型，如 `UInt(8.W)`）和 `n`（输入个数）参数化。
2. `in` = `Flipped(Vec(n, Decoupled(gen)))`：n 个被翻转方向的 Decoupled 输入。
3. `out` = `Decoupled(gen)`：一个正常方向的 Decoupled 输出。
4. `chosen` = `Output(UInt(log2Ceil(n).W))`：被选中输入的编号，宽度刚好够表示 0..n-1。

注意 `chosen` 是一个**组合输出**，只要某个输入 `valid`，它就指向最低优先级的那个有效输入（即便输出还没 `fire`）。它表示"如果现在发生传输，会选谁"，而不表示"已经传了一拍"。

#### 4.1.3 源码精读

[ArbiterIO 的定义：src/main/scala/chisel3/util/Arbiter.scala:L17-L37](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Arbiter.scala#L17-L37) —— 这是整个仲裁器的对外接口。三个字段一一对应上面的分析。

[字段 in：src/main/scala/chisel3/util/Arbiter.scala:L24](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Arbiter.scala#L24) —— `Flipped(Vec(n, Decoupled(gen)))`，n 个被翻转方向的输入端口。

[字段 out：src/main/scala/chisel3/util/Arbiter.scala:L30](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Arbiter.scala#L30) —— 一个正常方向的 Decoupled 输出。

[字段 chosen：src/main/scala/chisel3/util/Arbiter.scala:L36](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Arbiter.scala#L36) —— `Output(UInt(log2Ceil(n).W))`，宽度用 `log2Ceil(n)` 而非 `log2Ceil(n+1)`，因为编号范围是 0..n-1。

> 关于第 18 行那条注释（`gen is a private val`）：这是历史遗留的 API 设计说明，意思是 `gen` 故意用 `private val` 而不是公开的构造参数，以避免它被当成可被外部访问的字段。初学者可以忽略。

#### 4.1.4 代码实践

**实践目标**：亲手实例化一个 `ArbiterIO`，看它在生成的 Verilog 里长什么样。

**操作步骤**（示例代码，可放进一个 Scala 文件用 `mill` 或 sbt 跑）：

```scala
// 示例代码：把 ArbiterIO 直接当作一个模块的 IO
import chisel3._
import chisel3.util._

class ArbiterIOWrapper extends Module {
  val io = IO(new ArbiterIO(UInt(8.W), 4))
  // 暂时不接任何逻辑，仅看端口长什么样
  io.out := DontCare
  io.chosen := 0.U
}

object ArbiterIOWrapper extends App {
  import circt.stage.ChiselStage
  println(ChiselStage.emitSystemVerilog(
    new ArbiterIOWrapper,
    firtoolOpts = Array("-disable-all-randomization", "-strip-debug-info")
  ))
}
```

**需要观察的现象**：生成的 Verilog 模块端口里，`in_0_valid`、`in_0_ready`、`in_0_bits` …… `in_3_*` 各一组，外加 `out_valid`/`out_ready`/`out_bits` 和 `chosen[1:0]`。

**预期结果**：`in_*_ready` 是**输出**端口（因为 `in` 被 `Flipped`），`in_*_valid`/`in_*_bits` 是**输入**端口；`out_valid`/`out_bits` 是输出、`out_ready` 是输入；`chosen` 是 2 位输出（`log2Ceil(4)=2`）。

> 若无法本地运行，记为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果输入个数 `n=1`，`chosen` 会是多少位？还需要仲裁吗？
**答案**：`log2Ceil(1)=0`，`chosen` 是 0 位的（即不存在/恒定）。只有一个输入时确实没有"选谁"的问题，事实上 `ArbiterCtrl` 对 `n=1` 直接返回 `Seq(true.B)`（见 4.2.3）。

**练习 2**：为什么 `in` 用 `Flipped` 而 `out` 不用？
**答案**：`Decoupled` 默认方向是"生产者"（valid/bits 出、ready 入）。仲裁器是 n 个上游生产者的**消费者**，所以 `in` 要翻转；而仲裁器对下游又是**生产者**，所以 `out` 保持默认方向即可。

---

### 4.2 ArbiterCtrl：优先编码原语（所有仲裁策略的算法核心）

#### 4.2.1 概念说明

打开 `Arbiter.scala` 你会发现，无论是固定优先级的 `Arbiter` 还是轮询的 `RRArbiter`，它们选择输入的核心都依赖同一个私有工具：`ArbiterCtrl`。它本质上是一个**优先编码器（priority encoder）**：给定一串布尔"请求"信号 `Seq(r0, r1, ..., r_{n-1})`，输出一串"授权"信号 `Seq(g0, g1, ..., g_{n-1})`，其中 `g_i` 为真当且仅当"没有任何比 i 更高优先级（编号更小）的请求在喊"。

换句话说，`ArbiterCtrl` 把"谁该赢"这件事变成了一个纯组合的、可级联的布尔函数。理解了它，后面两种仲裁器都是"喂不同的请求序列给 `ArbiterCtrl`"。

#### 4.2.2 核心流程

对输入 `request = Seq(r0, r1, ..., r_{n-1})`，输出 `grant[i]` 满足：

\[ \text{grant}[i] = \neg\,(\,r_0 \lor r_1 \lor \dots \lor r_{i-1}\,), \quad \text{grant}[0] = \text{true} \]

即第 i 个请求被授权的充要条件是"它前面的所有请求都为假"。把 `grant[i]` 再与 `r_i` 本身相与，就得到"i 真正胜出"的条件。

举个具体例子，`n=4`、`request = Seq(r0, r1, r2, r3)`：

| i | grant[i] | 含义 |
|---|----------|------|
| 0 | `true` | 编号 0 优先级最高，只要它请求就一定能授权 |
| 1 | `!r0` | 仅当 0 没请求时，1 才有机会 |
| 2 | `!(r0\|\|r1)` | 仅当 0、1 都没请求时，2 才有机会 |
| 3 | `!(r0\|\|r1\|\|r2)` | 仅当 0、1、2 都没请求时，3 才有机会 |

这正是源码里那行 `scanLeft(request.head)(_ || _)` 算"前缀或（prefix-OR）"、再 `map(!_)` 取反的来历——它一次性算出了所有前缀的或。

#### 4.2.3 源码精读

[ArbiterCtrl 定义：src/main/scala/chisel3/util/Arbiter.scala:L41-L47](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Arbiter.scala#L41-L47) —— 一个 `private object`，对外只有一个 `apply(request: Seq[Bool]): Seq[Bool]`。

逐分支拆解这行（第 45 行）：

```scala
true.B +: request.tail.init.scanLeft(request.head)(_ || _).map(!_)
```

- `request.tail.init`：去掉第一个（`head` 单独处理）和最后一个（最后一项的"前缀或"就是全部请求的或，但 `grant` 末项不需要它，因为对最后一项而言"前面所有请求都为假"等价于直接被授权——实际上末项的 grant 由 `init` 后的 scanLeft 自然给出，这里 `init` 主要是为了避免多算一项）。具体地，对 `Seq(r0,r1,r2,r3)`：`tail = Seq(r1,r2,r3)`，`init = Seq(r1,r2)`。
- `.scanLeft(request.head)(_ || _)`：以前缀累积 OR，起点是 `r0`。对 `Seq(r1,r2)` 得到 `Seq(r0, r0||r1, r0||r1||r2)`。
- `.map(!_)`：取反，得到 `Seq(!r0, !(r0||r1), !(r0||r1||r2))`。
- `true.B +: ...`：在最前面补一个 `true.B`（grant[0] 恒真），得到长度为 n 的完整 grant 序列。

边界分支（第 42-44 行）：`n=0` 返回空序列；`n=1` 返回 `Seq(true.B)`——只有一个输入时永远授权它，与 4.1.5 的练习结论一致。

#### 4.2.4 代码实践

**实践目标**：用纸笔（或 REPL）追踪 `ArbiterCtrl` 在一组具体输入下的输出。

**操作步骤**：

1. 设 `n=4`，`request = Seq(false, true, true, false)`（即输入 1、2 有效，0、3 无效）。
2. 手算每个 `grant[i]`：grant[0]=true、grant[1]=!false=true、grant[2]=!(false||true)=false、grant[3]=!(false||true||true)=false。
3. 把 `grant[i] && request[i]` 算出来（真正的胜出条件）：(true&&false, true&&true, false&&true, false&&false) = (false, true, false, false)。

**需要观察的现象**：只有输入 1 真正胜出——它是"编号最小的有效输入"。

**预期结果**：胜出者 = 编号最小的有效输入。这正是"低编号优先"。

#### 4.2.5 小练习与答案

**练习**：`ArbiterCtrl(Seq(true.B, true.B, true.B))`（三个输入都请求）返回什么？谁会赢？
**答案**：grant = `Seq(true, !true, !(true||true))` = `Seq(true, false, false)`。胜出条件 `grant[i] && request[i]` = `(true, false, false)`，编号 0 赢。优先编码器总是把胜利给编号最小的请求者。

---

### 4.3 Arbiter：固定优先级仲裁器

#### 4.3.1 概念说明

有了 `ArbiterIO`（接口）和 `ArbiterCtrl`（算法），固定优先级仲裁器 `Arbiter` 就呼之欲出了：它把每个输入的 `valid` 当作"请求"喂给 `ArbiterCtrl`，得到"每个输入是否被授权"，然后据此接好 `bits`、`chosen`、`out.valid` 和每个输入的 `ready`。

**优先级方向**：低编号 = 高优先级。也就是说，如果 `in(0)` 和 `in(2)` 同时有效，永远选 `in(0)`。这会带来一个著名副作用——**饿死（starvation）**：只要 `in(0)` 一直有数据，`in(1)` 及以后永远轮不上。这正是下一节 `RRArbiter` 要解决的问题。

#### 4.3.2 核心流程

`Arbiter` 的构造体（依旧遵循"只登记不施工"，向 Builder 登记若干命令）分三件事：

1. **决定 `chosen` 与 `out.bits`**：默认指向编号最大的输入（`n-1`），然后从 `n-2` 往 `0` 倒着扫描；只要 `in(i).valid`，就用 `when` 覆盖 `chosen` 和 `bits`。因为 `when` 是"最后连接胜出"，而循环最后一次迭代是 `i=0`，所以**编号最小的有效输入最终胜出**。
2. **决定每个输入的 `ready`**：用 `ArbiterCtrl(io.in.map(_.valid))` 得到 grant 序列，`in(i).ready := grant(i) && io.out.ready`——只有被授权（即比它小编号的都没请求）且下游 ready 的输入才能被消费。
3. **决定 `out.valid`**：`!grant.last || io.in.last.valid`。`grant.last` 为真意味着"前 n-1 个输入都没请求"，所以 `out.valid` = "前 n-1 个里至少有一个有效" 或 "最后一个输入有效" = "至少一个输入有效"。

#### 4.3.3 源码精读

[Arbiter 类定义：src/main/scala/chisel3/util/Arbiter.scala:L149-L171](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Arbiter.scala#L149-L171) —— 固定优先级仲裁器本体。

[desiredName：src/main/scala/chisel3/util/Arbiter.scala:L154](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Arbiter.scala#L154) —— 重写模块名，使生成的 Verilog 模块名稳定可读，如 `Arbiter4_UInt8`。

[选择逻辑：src/main/scala/chisel3/util/Arbiter.scala:L158-L165](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Arbiter.scala#L158-L165) —— 注意循环方向 `n-2 to 0 by -1`：先尝试小编号，最后赋值的 `i=0` 胜出，等价于一个优先选择小编号的 mux 链。

[ready 与 out.valid：src/main/scala/chisel3/util/Arbiter.scala:L167-L170](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Arbiter.scala#L167-L170) —— `grant` 由 `ArbiterCtrl` 算出；`out.valid := !grant.last || io.in.last.valid`。

> 为什么 `in(i).ready := grant(i) && io.out.ready` 里没有显式判断 `in(i).valid`？因为 `grant(i)` 只表示"i 有资格被选"，若 i 自己没 `valid`，就算 ready 拉高也不会触发 `fire`（fire 需要 valid）。这种"给了一个无害的高 ready"的写法在组合逻辑上是等价且更简洁的。

#### 4.3.4 代码实践

**实践目标**：搭一个 4 输入 1 输出的固定优先级仲裁器，生成 Verilog，观察选中逻辑与优先级。

**操作步骤**（示例代码）：

```scala
// 示例代码：4 转 1 固定优先级仲裁器
import chisel3._
import chisel3.util._

class MyArbiter extends Module {
  val io = IO(new ArbiterIO(UInt(8.W), 4))
  val arb = Module(new Arbiter(UInt(8.W), 4))
  io <> arb.io   // 直接整包对接
}

object MyArbiterMain extends App {
  import circt.stage.ChiselStage
  println(ChiselStage.emitSystemVerilog(
    new MyArbiter,
    firtoolOpts = Array("-disable-all-randomization", "-strip-debug-info")
  ))
}
```

**需要观察的现象**：在生成的 Verilog 中，找到给 `chosen` 赋值的那段（一串 `in_0_valid ? 0 : in_1_valid ? 1 : ... : 3` 形式的嵌套三元）；找到给 `out_valid` 赋值的表达式，确认它是 `in_0_valid || in_1_valid || in_2_valid || in_3_valid`（即"任一有效"）。

**预期结果**：当 `in(0).valid=1` 时，无论其它输入如何，`chosen=0`、`out.bits=in_0_bits`、`out_valid=1`；只有当 `in(0..2)` 都无效时 `chosen` 才会等于 3。`in_*_ready` 只有被选中的那一拍（且 `out_ready=1`）才会为高。

> 若无法本地运行，记为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：把上面的 `Arbiter` 换成 `RRArbiter`（仅改类名），生成的 Verilog 模块名会变成什么？
**答案**：`RRArbiter4_UInt8`（见 4.4.3 中 `RRArbiter.desiredName` 的 `s"RRArbiter${n}_${gen.typeName}"`）。

**练习 2**：若同时给 `in(0)` 和 `in(3)` 喂有效数据且 `out.ready` 为高，`chosen` 是多少？哪一拍的 `in(0)_ready` 会为高？
**答案**：`chosen=0`（编号小者优先）；`in_0_ready=1`、`in_3_ready=0`（3 没被授权），只有输入 0 的数据被消费一拍。

---

### 4.4 RRArbiter：轮询（Round-Robin）仲裁器

#### 4.4.1 概念说明

固定优先级 `Arbiter` 的痛点是饿死。轮询仲裁器 `RRArbiter`（Round-Robin Arbiter）的思路是：**记住"上次服务了谁"，下次从它的下一个开始优先**，从而让每个输入都有公平机会。

实现上，`RRArbiter` 用一个寄存器 `lastGrant` 记录上次 `fire` 时选中的编号，然后在 `ArbiterCtrl` 之上"动态调整优先级"：编号大于 `lastGrant` 的输入被视作"这一轮还没服务过"，优先级更高；如果它们都没请求，才回退到从编号 0 开始的固定优先级。

#### 4.4.2 核心流程

`RRArbiter` 实际上继承自 `LockingRRArbiter(gen, n, count=1)`，而后者又继承自抽象基类 `LockingArbiterLike`。先看 `LockingArbiterLike` 定下的公共骨架：

1. 子类必须提供 `grant: Seq[Bool]`（每个输入是否被授权）和 `choice: UInt`（被选中的编号）。
2. 父类据此接线：`io.chosen := choice`、`io.out.valid := io.in(choice).valid`、`io.out.bits := io.in(choice).bits`。
3. 当 `count == 1`（`RRArbiter` 的情况）时，`in(i).ready := grant(i) && io.out.ready`，和固定优先级 `Arbiter` 形式一致。

`LockingRRArbiter` 的轮询策略在三行里：

- `lastGrant`：`RegEnable(io.chosen, io.out.fire)`——每次成功传输时，把当前选中编号记进寄存器。
- `grantMask(i) = (i > lastGrant)`：标记"编号比上次大的"输入。
- `validMask(i) = in(i).valid && grantMask(i)`：在"比上次大的"里、且正在请求的输入。

最终的 `grant` 用了一次"双段 `ArbiterCtrl`"：

\[ \text{ctrl} = \text{ArbiterCtrl}\big(\underbrace{\text{validMask}}_{\text{优先段}} \;+\; \underbrace{\text{in.map(\_.valid)}}_{\text{回退段}}\big) \]

\[ \text{grant}(i) = \big(\text{ctrl}(i) \land \text{grantMask}(i)\big) \;\lor\; \text{ctrl}(i+n) \]

- 第一项 `ctrl(i) && grantMask(i)`：在"优先段"（编号大于 lastGrant 的有效输入）里，i 是编号最小的 → i 胜出。
- 第二项 `ctrl(i+n)`：如果优先段一个都没有（没人比 lastGrant 大且有效），就回退到"全局编号最小优先"，即 `ctrl` 的后半段。

**举一个具体例子**（`n=4`，`lastGrant=1`）：

| 场景 | 有效输入 | grantMask | 胜出者 | 解释 |
|------|----------|-----------|--------|------|
| A | 0,2,3 | F,F,T,T | 2 | 优先段{2,3}里最小的是 2 |
| B | 0 | F,F,T,T | 0 | 优先段空，回退到全局最小 → 0 |
| C | 1,2 | F,F,T,T | 2 | 优先段{2}（1 不在优先段）→ 2 |

可以看到"从 lastGrant+1 开始绕一圈"的轮询效果：服务完 1 之后，下一个优先服务 2、再 3、再绕回 0、1。

#### 4.4.3 源码精读

[RRArbiter 类定义：src/main/scala/chisel3/util/Arbiter.scala:L127-L134](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Arbiter.scala#L127-L134) —— 它只是 `LockingRRArbiter(gen, n, 1)` 的薄封装（`count=1` 关闭锁定），并重写 `desiredName`。

[公共骨架 LockingArbiterLike：src/main/scala/chisel3/util/Arbiter.scala:L49-L76](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Arbiter.scala#L49-L76) —— 定义抽象方法 `grant`/`choice`，并接线 `io.chosen`/`io.out.valid`/`io.out.bits`（第 54-56 行）；`count==1` 分支在第 72-75 行。

[轮询核心 LockingRRArbiter：src/main/scala/chisel3/util/Arbiter.scala:L78-L103](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Arbiter.scala#L78-L103)。其中：

- [lastGrant 寄存器：src/main/scala/chisel3/util/Arbiter.scala:L85-L89](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Arbiter.scala#L85-L89) —— `RegEnable(io.chosen, io.out.fire)`；若 `initLastGrant=true` 则带初值 0。`RegEnable(next, en)` 即"en 为高时下一拍取 next"，见 `src/main/scala/chisel3/util/Reg.scala:L10-L14`。
- [grantMask 与 validMask：src/main/scala/chisel3/util/Arbiter.scala:L90-L91](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Arbiter.scala#L90-L91)。
- [双段 ArbiterCtrl 的 grant：src/main/scala/chisel3/util/Arbiter.scala:L93-L96](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Arbiter.scala#L93-L96)。
- [choice 的计算：src/main/scala/chisel3/util/Arbiter.scala:L98-L102](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Arbiter.scala#L98-L102) —— 先按全局优先级（第 99-100 行，逻辑同 `Arbiter`），再用 `validMask` 覆盖（第 101-102 行），保证 `choice` 与 `grant` 一致。

> **带锁变体（进阶，了解即可）**：`LockingArbiter`（[L105-L112](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Arbiter.scala#L105-L112)）和 `LockingRRArbiter` 在 `count > 1` 时会启用锁定机制（[L58-L71](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/Arbiter.scala#L58-L71)）：被选中的输入会用一个 `Counter(count)` 锁住仲裁器 `count` 拍，期间不再切换选中者。这用于"多拍事务不能被打断"的场景（如一次 burst 传输）。`RRArbiter` 传 `count=1`，不锁定。

#### 4.4.4 代码实践

**实践目标**：对比 `Arbiter` 与 `RRArbiter` 在"所有输入持续有效"时的不同表现。

**操作步骤**（示例代码）：

```scala
// 示例代码：把 4 个输入都接成持续 valid 的 Decoupled，对比两种仲裁器
import chisel3._
import chisel3.util._

class RRDemo extends Module {
  val io = IO(new ArbiterIO(UInt(8.W), 4))
  val arb = Module(new RRArbiter(UInt(8.W), 4))  // 试试换成 Arbiter 对比
  io <> arb.io
}

object RRDemoMain extends App {
  import circt.stage.ChiselStage
  println(ChiselStage.emitSystemVerilog(
    new RRDemo,
    firtoolOpts = Array("-disable-all-randomization", "-strip-debug-info")
  ))
}
```

**需要观察的现象**：生成的 Verilog 多出一个寄存器（`lastGrant`，宽度 `log2Ceil(4)=2` 位），以及一段比较 `i > lastGrant` 的逻辑——这是 `Arbiter` 版本里没有的。若配合 chiseltest 写一个让 4 个输入持续有效、`out.ready` 持续为高的激励，循环若干拍：

- 用 `Arbiter`：`chosen` 会**一直停在 0**（饿死其它输入）。
- 用 `RRArbiter`：`chosen` 会按 `0,1,2,3,0,1,2,3,...`（或因请求情况跳过空输入）**轮流**变化。

**预期结果**：`RRArbiter` 的 `chosen` 随 `lastGrant` 滚动，体现轮询公平性。

> 若无法本地运行激励，记为「待本地验证」；至少应能在 Verilog 中确认存在 `lastGrant` 寄存器与 `>` 比较逻辑。

#### 4.4.5 小练习与答案

**练习 1**：`RRArbiter` 内部那个"回退段"（`ctrl(i+n)`）什么时候起作用？
**答案**：当所有"编号大于 lastGrant 且有效"的输入都不存在时（即优先段为空），回退到全局编号最小优先。典型情形：只有编号 ≤ lastGrant 的输入在请求。

**练习 2**：把 `RRArbiter` 的 `count` 参数（通过直接用 `LockingRRArbiter`）设成大于 1 会怎样？
**答案**：启用锁定——一旦某个输入被选中并 `fire`，它会被锁住 `count` 拍，期间 `chosen` 不变、其它输入即使优先级更高也进不来，用于保护多拍事务的原子性。

## 5. 综合实践

把本讲三个最小模块串起来，做一个**带仲裁的简单共享总线**：

**任务**：设计一个模块 `SharedBus`，它有 3 个 Decoupled 输入（3 个"发送方"各发 `UInt(8.W)`）和 1 个 Decoupled 输出（共享总线）。要求：

1. 用 `ArbiterIO(UInt(8.W), 3)` 作为对外端口形状，但内部用 `RRArbiter` 实现公平仲裁（避免某个发送方饿死）。
2. 把仲裁器的 `chosen` 引到一个输出端口 `chosen` 上，方便观察当前服务的是谁。
3. 用 `ChiselStage.emitSystemVerilog` 生成 Verilog，确认：存在 `lastGrant` 寄存器；`chosen` 是 2 位（`log2Ceil(3)=2`，注意 3 不是 2 的幂，`chosen` 仍需 2 位）。

**参考骨架**（示例代码）：

```scala
import chisel3._
import chisel3.util._

class SharedBus extends Module {
  val io = IO(new Bundle {
    val in     = Flipped(Vec(3, Decoupled(UInt(8.W))))
    val out    = Decoupled(UInt(8.W))
    val chosen = Output(UInt(2.W))
  })
  val arb = Module(new RRArbiter(UInt(8.W), 3))
  // ArbiterIO 的 in/out 与本模块 IO 一一对接
  arb.io.in <> io.in
  io.out    <> arb.io.out
  io.chosen := arb.io.chosen
}

object SharedBusMain extends App {
  import circt.stage.ChiselStage
  println(ChiselStage.emitSystemVerilog(
    new SharedBus,
    firtoolOpts = Array("-disable-all-randomization", "-strip-debug-info")
  ))
}
```

**验收点**：

- Verilog 中 `RRArbiter3_UInt8` 子模块被实例化；
- 存在 `lastGrant` 寄存器与轮询比较逻辑；
- `chosen` 宽度为 2。

## 6. 本讲小结

- `ArbiterIO[T](gen, n)` 是所有仲裁器的统一接口：`Flipped` 的 n 个 `Decoupled` 输入、1 个 `Decoupled` 输出、1 个 `chosen` 编号输出。
- `ArbiterCtrl`（私有）是一个优先编码器，用"前缀或取反"一次性算出每个请求的授权位，是固定优先级与轮询两种策略的共同算法积木。
- `Arbiter(gen, n)` 是**固定优先级**仲裁器，低编号优先；用一段 `for` 循环 + `when`（最后连接胜出）选出编号最小的有效输入；缺点是高编号输入可能被饿死。
- `RRArbiter(gen, n)` 是**轮询**仲裁器，靠 `lastGrant` 寄存器 + `grantMask`/`validMask` + "双段 ArbiterCtrl"实现"从上次服务的下一个开始优先"，保证公平。
- `LockingArbiter` / `LockingRRArbiter`（`count>1`）提供"锁定选中者若干拍"的多拍事务保护；`RRArbiter` 是 `LockingRRArbiter` 在 `count=1` 时的特例。
- 所有实现依旧遵循"只登记不施工"——模块体只是向 Builder 登记命令，最终 Verilog 由下游 firtool 产出。

## 7. 下一步学习建议

- **向上游**：仲裁器常与 `Queue`（u6-l2）配合，构成"多个生产者 → 各自 FIFO → 仲裁 → 共享下游"的结构。可以试着把每个仲裁输入前接一个 `Queue`，观察反压如何传递。
- **向工具链**：想验证轮询行为，需要写激励跑仿真——这正是 u8-l5（`Simulator` / svsim）和 u9-l1（测试体系）的主题。
- **向源码深处**：`ArbiterCtrl` 的 `scanLeft` 写法是函数式硬件描述的典型范例；可对比 `SeqUtils`（u2-l4）里的 `priorityMux`/`oneHotMux`，体会 Chisel 如何用 Scala 集合运算生成 mux 树。
- **下一讲 u6-l4**：将讲解 `switch/is`、`Mux1H`/`MuxLookup`、`Counter` 等条件与计数工具，其中 `Counter` 正是本讲带锁变体用到的计数器。
