# Mem：内存与 SyncReadMem

## 1. 本讲目标

上一讲（u3-l5）我们学会了用 `Reg` / `RegNext` / `RegInit` 记住「一个」值。但真实电路里常常需要记住「一大批」值——寄存器堆、查找表、FIFO 的存储体、指令/数据存储器……如果一个一个声明 `Reg`，不但冗长，综合工具也无法把它识别成一块存储器（SRAM / Block RAM）。Chisel 提供了 `Mem` 与 `SyncReadMem` 来描述这种「成片」的存储。读完本讲你应当能够：

- 说清 `Mem` 与 `SyncReadMem` 的**时序差异**：`Mem` 是「组合读、同步写」（当拍给地址、当拍出数据），`SyncReadMem` 是「同步读、同步写」（读数据要等到下一拍）。
- 解释内存「端口（port）」的概念：读写不是直接发生在内存对象上，而是每次 `mem(addr)` / `mem.read(addr)` / `mem.write(addr, data)` 都会向 Builder 登记**一个新的端口**（`DefMemPort`），并 `bind` 成 `MemoryPortBinding`。
- 区分四种端口方向 `MemPortDirection`：`INFER`（按连线上下文推断）/ `READ` / `WRITE` / `RDWR`，并指出 `SyncReadMem.readWrite` 为什么显式生成 `RDWR` 端口。
- 指出这同样遵循 Chisel 的铁律——**只登记不施工**：构造内存压入 `DefMemory` / `DefSeqMemory`，开端口压入 `DefMemPort`，写操作压入一条 `Connect`。
- 了解 `useSRAMBlackbox` 等「黑盒化」选项如何把内存换成可移植的 SystemVerilog 黑盒。

本讲是单元 3（模块与连线）的收尾，也是后续 u6-l2（`Queue`，其存储体正是 `Mem`）的基础。

## 2. 前置知识

本讲默认你已经掌握（来自前置讲义）：

- **只登记不施工**（u1-l4 / u4-l2）：模块构造体里每一行都只是向 Builder 的命令队列 `pushCommand`，本身不立刻生成硬件；真正生成 Verilog 的是 `ChiselStage.emitSystemVerilog`。
- **Module 与隐式 clock/reset**（u3-l1）：`Module` 混入 `ImplicitClock` / `ImplicitReset`，自动提供 `clock` / `reset`；存储器的写端口和 `SyncReadMem` 的读端口都需要 clock，默认用 `Builder.forcedClock`（即隐式 clock）。
- **类型 vs 硬件值**（u2-l1 / u4-l3）：`Mem(size, UInt(8.W))` 里传进去的 `UInt(8.W)` 必须是**纯类型**（无 binding），由 `requireIsChiselType` 把关；内部会 `cloneTypeFull` 复制一份再 `bind` 成内存类型。
- **`:=` 与底层 MonoConnect**（u3-l3 / u3-l4）：`mem.write(addr, data)` 本质上是对「写端口」做 `port := data`，最终走 `MonoConnect` 发出一条 `Connect` 命令。
- **`when` 条件块**（u3-l5）：`SyncReadMem` 的读/读写端口内部用 `when(enable) { ... }` 实现「使能」，本讲会读这段源码。

还需补充三个本讲要用到的术语：

- **组合读 / 同步读**：「组合读」（asynchronous / combinational read）指给出地址的**同一拍**就能拿到数据（内存表现为纯组合查表）；「同步读」（synchronous read）指地址先寄存一拍，数据在**下一拍**才出现（对应真实 SRAM / Register File 的行为）。
- **读优先 / 写优先 / 未定义（Read-Under-Write）**：当同一拍对同一地址「既读又写」时，读端口拿到的是旧值（Read First / Old）、新值（Write First / New）还是未定义（Undefined）。这是 `SyncReadMem` 特有、`Mem` 不存在的语义问题。
- **内存端口（memory port）**：FIRRTL 里内存不是「直接寻址的变量」，而是通过「端口」访问；每个端口有方向（读 / 写 / 读写）、地址、使能、时钟。Chisel 把这件事藏在 `mem(addr)` 这种语法糖背后。

一个贯穿本讲的直觉模型：把 `Mem(size, T)` 想成「一块 `size` 个 `T` 类型格子的阵列」，把 `mem(addr)` 想成「在阵列上开一个**窗口**」。窗口本身不是数据，而是一根带方向的接线——读窗口把阵列里的值「引出来」，写窗口把外面的值「灌进去」。`Mem` 和 `SyncReadMem` 的差别，仅仅是这扇窗口的「读」是立刻透光（组合）还是延迟一拍才透光（同步）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [core/src/main/scala/chisel3/Mem.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Mem.scala) | 本讲主战场。`object Mem` / `class Mem`（组合读同步写）、`object SyncReadMem` / `class SyncReadMem`（同步读写），以及两者共同的基类 `MemBase`（含端口工厂 `makePort`、读写 `_applyImpl` / `_readImpl` / `_writeImpl`）。 |
| [core/src/main/scala-2/chisel3/MemIntf.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala-2/chisel3/MemIntf.scala) | 用户可见的 `apply` / `read` / `write` / `readWrite` 接口（trait `MemObjIntf` / `MemBaseIntf` / `SyncReadMemObjIntf` / `SyncReadMemIntf`），它们是挂了 `SourceInfoTransform` 宏的入口，转发到 `Mem.scala` 里的 `do_*` / `_xxxImpl`。 |
| [core/src/main/scala/chisel3/internal/firrtl/IR.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala) | 内部 IR 节点：`MemPortDirection`（L295）、`DefMemory`（L332）、`DefSeqMemory`（L334）、`FirrtlMemory`（L342）、`DefMemPort`（L354）。 |
| [core/src/main/scala/chisel3/internal/firrtl/Converter.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala) | 把内部 IR 翻译成 `firrtl.ir` 节点：`convert(MemPortDirection)`（L57）、`DefMemory → CDefMemory`（L155）、`DefSeqMemory`（L157）、`FirrtlMemory → fir.DefMemory`（L170）、`DefMemPort → CDefMPort`（L181）。 |
| [core/src/main/scala/chisel3/internal/Binding.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Binding.scala) | 绑定种类：`MemoryPortBinding`（L85）、`MemTypeBinding`（L120，标记「这是某块内存的类型」）、`SramPortBinding`（L88）/ `FirrtlMemTypeBinding`（L125，给 `SRAM` 工具用）。 |
| [core/src/main/scala/chisel3/Aggregate.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala) | `Vec.truncateIndex`（L178）：把任意宽度的地址截到刚好能寻址内存长度所需的位数。 |
| [src/main/scala/chisel3/util/SRAM.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/SRAM.scala) | `chisel3.util.SRAM`：高层多端口内存生成器，当 `Builder.useSRAMBlackbox` 为真时改走 `memInterface_blackbox_impl`（L905），实例化 `SRAMBlackbox`（L205）。 |
| [src/main/scala/chisel3/stage/ChiselOptions.scala](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/ChiselOptions.scala) | `useSRAMBlackbox` 选项字段（L20），经 `ChiselOptions` 注解透传到 `Builder`。 |

## 4. 核心概念与源码讲解

### 4.1 MemBase 与 MemPort：内存端口抽象

#### 4.1.1 概念说明

很多初学者以为 `mem(addr)` 就是「读出第 `addr` 个元素」，和 Scala 数组 `arr(addr)` 差不多。这个直觉对了一半，但漏掉了硬件里最关键的一点：**每次访问都会在电路上「开一个端口」**。

软件数组访问是「瞬时」的——你访问 100 次也只是 100 次读取动作，不留痕迹。硬件存储器访问则是「结构性的」——你写 `mem(a)` 和 `mem(b)`，就会在生成的电路上长出**两个独立的读端口**，综合后对应两套地址译码与读数据线。这正是为什么 FIRRTL（以及真实 FPGA/ASIC）用「内存 + 端口」而非「数组」来建模存储：端口数量直接决定这块内存能否映射成单口 RAM、双口 RAM 还是多端口寄存器堆。

Chisel 把这套机制抽象进共同基类 `MemBase[T]`，`Mem[T]` 和 `SyncReadMem[T]` 都继承自它。`MemBase` 持有三个核心信息：元素类型 `t`、深度 `length`、以及创建时捕获的隐式 clock `clockInst`（用于检查端口时钟是否一致）。所有「开端口」的活儿都收拢到一个私有方法 `makePort`。

第二个要点是端口**方向**。一个端口可以是：

\[ \text{MemPortDirection} \in \{\,\text{INFER},\ \text{READ},\ \text{WRITE},\ \text{RDWR}\,\} \]

- `INFER`：不指定，由后续连线（`:=`）上下文推断是读还是写——这是 `mem(addr)` 这种「万能访问」语法糖用的。
- `READ` / `WRITE`：显式只读 / 只写端口（`mem.read` / `mem.write`）。
- `RDWR`：读写复合端口（`SyncReadMem.readWrite`），同一端口既能读又能写，由额外信号控制本拍方向。

#### 4.1.2 核心流程

开一个端口的完整流程（以 `mem(addr)` 为例）：

1. 宏把 `mem(addr)` 改写为 `mem.do_apply(addr)`，注入隐式 `SourceInfo`。
2. `MemBaseIntf.do_apply` 转发到 `MemBase._applyImpl(idx, clock, dir, warn)`（经多重载层层收口）。
3. `_applyImpl` 检查「端口时钟」与「内存创建时的隐式 clock」是否一致，不一致则发一条 deprecated 警告（将来会变错误）。
4. 调 `makePort(sourceInfo, idx, dir, clock)`：
   - 校验当前模块就是内存所属模块（端口不能跨模块开）；
   - `requireIsHardware(idx)` 确保地址是硬件值；
   - `Vec.truncateIndex(idx, length)` 把地址截到刚好够用的位宽；
   - `pushCommand(DefMemPort(...))` 登记一个端口节点；
   - 把返回的 `port` 用 `MemoryPortBinding` 绑定，使其成为可连线的硬件值。
5. 对于写操作，`makePort` 返回的端口再执行 `port := data`，发出一条 `Connect` 命令。

伪代码：

```
mem(addr)  ──宏──▶  do_apply(addr)
                 ──▶  _applyImpl(addr, forcedClock, INFER, warn=true)
                        ├── clockWarning 检查
                        └── makePort(info, addr, INFER, clock)
                              ├── truncateIndex(addr, length)
                              ├── pushCommand(DefMemPort(...))   // 登记端口
                              └── port.bind(MemoryPortBinding)    // 变成硬件值
mem.write(addr, data) ──▶ makePort(..., WRITE, clock) := data    // 再发一条 Connect
```

#### 4.1.3 源码精读

`MemBase` 是 `Mem` 与 `SyncReadMem` 的共同基类，构造时捕获创建期的隐式 clock，用于后续一致性检查（[core/src/main/scala/chisel3/Mem.scala:40-53](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Mem.scala#L40-L53)）：

```scala
sealed abstract class MemBase[T <: Data](val t: T, val length: BigInt, protected val sourceInfo: SourceInfo)
    extends MemBaseIntf[T] with HasId with NamedComponent {
  if (t.isConst) Builder.error("Mem type cannot be const.")(sourceInfo)
  requireNoProbeTypeModifier(t, "Cannot make a Mem of a Chisel type with a probe modifier.")(sourceInfo)
  _parent.foreach(_.addId(this))
  // 内存创建期捕获的隐式 clock，用于校验端口时钟是否一致
  private val clockInst: Option[Clock] = Builder.currentClock
```

注意两个守卫：元素类型**不能是 const**（const 类型无法构成可写存储），也**不能带 probe 修饰符**（probe 是 u8-l3 的主题，内存元素暂不支持）。`_parent.foreach(_.addId(this))` 把这块内存注册到所在模块的 id 列表里，后续命名阶段（u7-l3）才能给它取个可读的名字。

四个 `_applyImpl` / `_readImpl` 重载层层收口到统一的 4 参版本，做时钟一致性检查后转给 `makePort`（[core/src/main/scala/chisel3/Mem.scala:66-98](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Mem.scala#L66-L98)）：

```scala
protected def _applyImpl(idx: UInt)(implicit sourceInfo: SourceInfo): T =
  _applyImpl(idx, Builder.forcedClock, MemPortDirection.INFER, true)
...
protected def _applyImpl(idx, clock, dir, warn)(implicit sourceInfo: SourceInfo): T = {
  if (warn && clockInst.isDefined && clock != clockInst.get)
    clockWarning(Some(sourceInfo), dir)
  makePort(sourceInfo, idx, dir, clock)
}
```

`makePort` 是「开端口」的唯一出口（[core/src/main/scala/chisel3/Mem.scala:166-187](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Mem.scala#L166-L187)）：

```scala
private def makePort(sourceInfo, idx, dir, clock): T = {
  implicit val info: SourceInfo = sourceInfo
  if (Builder.currentModule != _parent)
    throwException("Cannot create a memory port in a different module ...")
  requireIsHardware(idx, "memory port index")
  val i = Vec.truncateIndex(idx, length)
  val port = pushCommand(
    DefMemPort(sourceInfo, t.cloneTypeFull, Node(this), dir, i.ref, clock.ref)
  ).id
  port.bind(MemoryPortBinding(Builder.forcedUserModule, Builder.currentBlock))
  port
}
```

四个关键动作：(1) 端口必须在内存所属模块里开；(2) 地址必须是硬件值；(3) `truncateIndex` 截断地址位宽；(4) 压入 `DefMemPort` 命令并 `bind` 成 `MemoryPortBinding`。其中 `Node(this)` 是对内存本身的引用，`dir` 决定端口方向，`i.ref` / `clock.ref` 分别是地址与时钟的引用。

`truncateIndex` 把地址截到「能表示 `length-1` 的最少位数」，避免地址位宽过宽（[core/src/main/scala/chisel3/Aggregate.scala:178-190](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Aggregate.scala#L178-L190)）：

```scala
private[chisel3] def truncateIndex(idx: UInt, n: BigInt)(implicit sourceInfo: SourceInfo): UInt = {
  val w = (n - 1).bitLength
  if (n <= 1) WireInit(0.U)
  else if (idx.width.known && idx.width.get <= w) idx
  else if (idx.width.known) idx(w - 1, 0)
  else (idx | 0.U(w.W))(w - 1, 0)
}
```

地址位宽计算：\( w = \text{bitLength}(n-1) \)，即表示最大下标 `n-1` 所需的位数。例如 `n = 16` 时 `n-1 = 15`，\( w = 4 \)。位宽已知且不超过 `w` 就原样用；已知但更宽就截低位；位宽未知（如 `UInt()`）就先或上一个 `0.U(w.W)` 强行把它「确定化」再截——这保证最终地址恰好 `w` 位，下游综合器能把内存识别为标准 RAM。

对应的 IR 节点与方向枚举在 IR.scala（[core/src/main/scala/chisel3/internal/firrtl/IR.scala:295-361](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/IR.scala#L295-L361)）：

```scala
sealed abstract class MemPortDirection(name: String) { override def toString = name }
object MemPortDirection {
  object READ extends MemPortDirection("read")
  object WRITE extends MemPortDirection("write")
  object RDWR extends MemPortDirection("rdwr")
  object INFER extends MemPortDirection("infer")
}
...
case class DefMemory(sourceInfo, id, t, size: BigInt) extends Definition        // Mem
case class DefSeqMemory(sourceInfo, id, t, size, readUnderWrite) extends Definition // SyncReadMem
case class DefMemPort[T <: Data](sourceInfo, id, source: Node, dir, index, clock) extends Definition
```

可以看到 `Mem` 对应 `DefMemory`（无读优先级），`SyncReadMem` 对应 `DefSeqMemory`（多一个 `readUnderWrite` 字段），两者开端口都是 `DefMemPort`。

Converter 把这些节点翻译成 FIRRTL 的 `CDefMemory` / `CDefMPort`（[core/src/main/scala/chisel3/internal/firrtl/Converter.scala:57-62](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala#L57-L62)、[L155-L190](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala#L155-L190)）：

```scala
def convert(dir: MemPortDirection): firrtl.MPortDir = dir match {
  case MemPortDirection.INFER => firrtl.MInfer
  case MemPortDirection.READ  => firrtl.MRead
  case MemPortDirection.WRITE => firrtl.MWrite
  case MemPortDirection.RDWR  => firrtl.MReadWrite
}
case e @ DefMemory(info, id, t, size) =>
  firrtl.CDefMemory(convert(info), e.name, extractType(t, info, typeAliases), size, false)
case e @ DefSeqMemory(info, id, t, size, ruw) =>
  firrtl.CDefMemory(convert(info), e.name, extractType(t, info, typeAliases), size, true, ruw)
case e: DefMemPort[_] =>
  firrtl.CDefMPort(convert(e.sourceInfo), e.name, fir.UnknownType,
    e.source.fullName(ctx), Seq(convert(e.index, ctx, info), convert(e.clock, ctx, info)), convert(e.dir))
```

`CDefMemory` 的最后一个布尔参数 `false/true` 正是「读是否同步」——`Mem` 给 `false`（组合读），`SyncReadMem` 给 `true`（同步读），并额外带上 `ruw`。这就是 `Mem` 与 `SyncReadMem` 在 IR 层面的**唯一**根本区别。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「每次访问都会开一个端口」，而不是把内存当软件数组。

**操作步骤**（示例代码，可用 `ChiselStage.emitSystemVerilog` 跑）：

```scala
// 示例代码
import chisel3._
import chisel3.stage.ChiselStage

class TwoReaders extends Module {
  val io = IO(new Bundle {
    val a = Input(UInt(4.W))
    val b = Input(UInt(4.W))
    val out = Output(UInt(8.W))
  })
  val mem = Mem(16, UInt(8.W))
  // 两次不同的访问 → 两个独立读端口
  io.out := mem(io.a) + mem(io.b)
}
```

**需要观察的现象**：用 `ChiselStage.emitSystemVerilog(new TwoReaders)` 生成 Verilog，在输出里找 `mem` 的声明。你会看到它带**两套**端口（地址线、使能、读数据各两份），而不是一个。

**预期结果**：综合视图里 `mem` 是一个双读端口的 RAM；若把 `mem(io.b)` 改成 `mem(io.a)`（同一个地址表达式），Chisel 仍会开两个端口——它是按「调用次数」而非「地址是否相同」来开端口的。

**待本地验证**：如果你本地已按 u1-l2 装好 firtool，可运行 `ChiselStage.emitSystemVerilog(new TwoReaders)` 查看端口数量；否则可在 CHIRRTL 文本里数 `mport` 的个数（见 4.3.4）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Chisel 要用 `truncateIndex` 把地址截到恰好 `bitLength(length-1)` 位，而不是直接用调用者传入的地址位宽？

**答案**：为了让下游综合器能把它识别成标准 RAM。若地址位宽大于实际所需（如 16 深内存却用 32 位地址），综合器会以为需要寻址 \(2^{32}\) 个表项，无法映射到 Block RAM；截到刚好 4 位后，地址空间与内存深度严格匹配。

**练习 2**：下面两段代码生成的端口数是否相同？为什么？

```scala
// (A)
val x = mem(io.a); val y = mem(io.b)
// (B)
val x = mem(io.a); val y = x
```

**答案**：不同。(A) 调用了两次 `mem(...)`，开两个读端口；(B) 只调用了一次，开一个读端口，`y` 只是把同一个硬件值再起个名。这再次说明「端口数 = 调用 `mem(addr)` 的次数」，而非「用了几个变量名」。

**练习 3**：`DefMemory`（`Mem`）和 `DefSeqMemory`（`SyncReadMem`）在 IR.scala 里的字段差异是什么？它对应到硬件行为的哪一点不同？

**答案**：`DefSeqMemory` 多了一个 `readUnderWrite: fir.ReadUnderWrite.Value` 字段，且 Converter 里它被翻译成 `CDefMemory(..., true, ruw)`（`true` 表示同步读）。对应的硬件行为是：`SyncReadMem` 的读要寄存一拍，且当同地址同拍既读又写时，由 `ruw` 决定读旧值/新值/未定义；`Mem` 组合读则不存在这个时序问题（Read-After-Write 不是 hazard）。

---

### 4.2 Mem：组合读 / 同步写的存储器

#### 4.2.1 概念说明

`Mem` 是两种内存里「更像软件数组」的那个：**给地址当拍就出数据**。它的完整时序契约写在其类注释里（[core/src/main/scala/chisel3/Mem.scala:191-199](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Mem.scala#L191-L199)）：

> A combinational/asynchronous-read, sequential/synchronous-write memory. Writes take effect on the rising clock edge after the request. Reads are combinational (requests will return data on the same cycle). Read-after-write hazards are not an issue.

拆成三句：

- **组合读**：`mem.read(addr)` 当拍返回 `addr` 处的当前值，无寄存器介入。
- **同步写**：`mem.write(addr, data)` 在**下一个**时钟上升沿才真正写入；本拍读同一地址拿到的还是旧值——但因为它「读是组合的」，写完成后的下一拍读能立刻看到新值，所以注释说「Read-after-write hazards are not an issue」。
- **多写冲突未定义**：若同一拍对同一地址有多个写，结果是未定义的（不像 `Vec` 那样「最后一条赋值胜出」）。

这种「组合读」内存在 FPGA 上通常会被综合成分布式 RAM（LUTRAM）或寄存器堆，而不是 Block RAM（Block RAM 做不到组合读）。所以当你明确想要 Block RAM 时，应该用 4.3 的 `SyncReadMem`。

#### 4.2.2 核心流程

`Mem` 的构造与读写：

1. `Mem(size, t)` → 宏 → `do_apply` → `_applyImpl(size, t)`：
   - `requireIsChiselType(t)` 校验元素是纯类型；
   - `cloneTypeFull` 复制类型；
   - `new Mem(...)` 造对象；
   - `mt.bind(MemTypeBinding(mem))` 把类型标记为「这块内存的类型」；
   - `pushCommand(DefMemory(...))` 登记内存本体；
   - 返回 `mem`。
2. 读：`mem.read(addr)` → `_readImpl` → `makePort(..., READ, clock)`，返回的端口直接当硬件值用。
3. 写：`mem.write(addr, data)` → `_writeImpl` → `makePort(..., WRITE, clock) := data`，即开一个写端口并连线。

`Mem` 相对 `MemBase` 唯一的覆盖是 `clockWarning`：因为读不涉及时钟，所以读端口不该报「时钟不一致」警告（[core/src/main/scala/chisel3/Mem.scala:202-206](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Mem.scala#L202-L206)）。

#### 4.2.3 源码精读

构造入口在 `object Mem`（[core/src/main/scala/chisel3/Mem.scala:16-38](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Mem.scala#L16-L38)）：

```scala
object Mem extends MemObjIntf {
  @implicitNotFound("Masked write requires that the data type is a Vec, got ${T}.")
  type HasVecDataType[T] = T <:< Vec[_]

  protected def _applyImpl[T <: Data](size: BigInt, t: T)(implicit sourceInfo: SourceInfo): Mem[T] = {
    requireIsChiselType(t, "memory type")
    val mt = t.cloneTypeFull
    val mem = new Mem(mt, size, sourceInfo)
    mt.bind(MemTypeBinding(mem))
    pushCommand(DefMemory(sourceInfo, mem, mt, size))
    ModulePrefixAnnotation.annotate(mem)
    mem
  }
}
```

注意 `ModulePrefixAnnotation.annotate(mem)`：它给内存挂上模块前缀注解，便于下游（CIRCT）在多模块场景下精确定位。`HasVecDataType` 是「带掩码写」的前提约束——只有元素类型是 `Vec` 时才能用字节掩码写（因为掩码要作用到 Vec 的每个元素上），后面 `_maskedWriteImpl` 会用到。

写操作的实现 `_writeImpl`，核心就是「开写端口 + 连线」（[core/src/main/scala/chisel3/Mem.scala:106-118](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Mem.scala#L106-L118)）：

```scala
private def _writeImpl(idx, data, clock, warn)(implicit sourceInfo: SourceInfo): Unit = {
  if (warn && clockInst.isDefined && clock != clockInst.get)
    clockWarning(None, MemPortDirection.WRITE)
  makePort(sourceInfo, idx, MemPortDirection.WRITE, clock) := data
}
```

`makePort(...) := data` 这一行同时做了两件事：先 `makePort`（登记 `DefMemPort` 并 `bind`），再对返回的端口执行 `:=`（走 `MonoConnect`，登记一条 `Connect`）。这就是「写 = 开写端口 + 一条连线」的全部真相。

带掩码的写（`mem.write(idx, data, mask)`）只对 `Vec` 元素内存有效，逐个元素按掩码位选择性地写（[core/src/main/scala/chisel3/Mem.scala:141-164](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Mem.scala#L141-L164)）：

```scala
val accessor = makePort(sourceInfo, idx, MemPortDirection.WRITE, clock).asInstanceOf[Vec[Data]]
val dataVec = data.asInstanceOf[Vec[Data]]
... // 长度匹配校验
for (((cond, port), datum) <- mask.zip(accessor).zip(dataVec))
  when(cond) { port := datum }
```

它把写端口当成 `Vec[Data]`，用 `when(cond)` 给每个子元素单独连线——这正是 u3-l5 学的 `when` 在「条件写」上的典型应用。`HasVecDataType[T]` 隐式证据（即 `T <:< Vec[_]`）保证了这一步的 `asInstanceOf` 安全。

#### 4.2.4 代码实践

**实践目标**：用 `Mem` 实现一个寄存器堆式的「组合读」存储，并观察读数据与地址同拍出现。

**操作步骤**（示例代码）：

```scala
// 示例代码
import chisel3._

class CombReadMem extends Module {
  val io = IO(new Bundle {
    val raddr = Input(UInt(4.W))
    val waddr = Input(UInt(4.W))
    val wdata = Input(UInt(8.W))
    val we    = Input(Bool())
    val rdata = Output(UInt(8.W))
  })
  val mem = Mem(16, UInt(8.W))
  when(io.we) { mem.write(io.waddr, io.wdata) }   // 同步写：下一拍生效
  io.rdata := mem.read(io.raddr)                  // 组合读：当拍出数据
}
```

**需要观察的现象**：生成 SystemVerilog 后，`rdata` 应当由**组合逻辑**（对内存数组的连续读取）驱动，而非经过寄存器；写则包在 `always @(posedge clock)` 里。

**预期结果**：在 Verilog 里能看到形如 `assign rdata = mem[raddr];` 的组合赋值，以及 `always @(posedge clock) if (we) mem[waddr] <= wdata;` 的同步写。注意：因为写是下一拍生效，若 `raddr == waddr`，当拍 `rdata` 仍是旧值（这正是「同步写、组合读」的语义）。

**待本地验证**：组合读 RAM 能否映射到你目标 FPGA 的 Block RAM 资源，取决于器件；多数 FPGA 会把它放到分布式 RAM / LUT。

#### 4.2.5 小练习与答案

**练习 1**：`Mem` 的类注释说「Read-after-write hazards are not an issue」，为什么？这与「写是下一拍生效」矛盾吗？

**答案**：不矛盾。「hazard 不存在」指的是：因为读是组合的，写生效后的**同一拍**（即下一个时钟周期的任意时刻）读就能看到新值，不存在「读数据被旧地址寄存」的时序错位问题。而「写下一拍生效」描述的是写的时序：本拍发起的写在下一个上升沿才落盘。两者一组合，恰恰消除了读写之间的时序歧义。

**练习 2**：把 `Mem` 的 `clockWarning` 覆盖方法里的判断 `if (dir != MemPortDirection.READ)` 去掉会怎样？

**答案**：那样对 `Mem` 的读端口也会触发「端口时钟与内存创建时钟不一致」的 deprecated 警告。但 `Mem` 的读是组合的、本就不需要时钟（`read` 内部虽传了 `forcedClock` 给 `DefMemPort`，但组合读端口实际不使用它），所以原代码特意屏蔽了读的警告，避免误导用户。

**练习 3**：为什么带掩码写 `mem.write(idx, data, mask)` 要求元素类型 `T` 必须是 `Vec`？

**答案**：掩码的粒度是「Vec 的每个元素」——`mask` 是 `Seq[Bool]`，长度等于 Vec 元素个数，第 `i` 个 `Bool` 控制是否写第 `i` 个子元素。若 `T` 不是 `Vec`（如纯 `UInt`），就没有「子元素」可分别掩码，掩码写无从定义。源码用 `type HasVecDataType[T] = T <:< Vec[_]` 在类型层面强制这一约束，编译期即可拦截误用。

---

### 4.3 SyncReadMem：同步读 / 同步写的存储器

#### 4.3.1 概念说明

`SyncReadMem` 是大多数「真 RAM」场景该选的内存：**读写都同步**。给出读地址后，数据在**下一拍**才出现。类注释（[core/src/main/scala/chisel3/Mem.scala:250-259](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Mem.scala#L250-L259)）说：

> A sequential/synchronous-read, sequential/synchronous-write memory. ... Read-after-write behavior (when a read and write to the same address are requested on the same cycle) is undefined.

两个要点：

- **同步读**：读地址先寄存一拍，数据下一拍出来。这正好匹配 FPGA 的 Block RAM / 真实 SRAM 的行为，因此 `SyncReadMem` 是综合成 Block RAM 的首选。
- **读写同址同拍的语义未定义**：可由用户通过 `ReadUnderWrite` 显式选择 `ReadFirst`（读旧值）/ `WriteFirst`（读新值）/ `Undefined`（默认，交给下游优化）。

`SyncReadMem` 还多了一个「使能（enable）」概念：`mem.read(addr, en)` 当 `en` 为假时本拍不更新输出（读地址不寄存）。这对应 RAM 的读使能端口。

#### 4.3.2 核心流程

`SyncReadMem` 的读之所以是「同步」的，全靠 `_readImpl` 里**主动插入一个 `WireDefault(..., DontCare)` + `when(enable)`**：

1. `mem.read(addr)` → `_readImpl(addr)` → 默认 `en = true.B` → 4 参 `_readImpl(addr, en, clock, warn)`。
2. 在内部声明一个地址寄存线 `_a = WireDefault(chiselTypeOf(addr), DontCare)`；
3. `when(enable) { _a := addr; _port = Some(super._applyImpl(_a, clock, READ, warn)) }`：
   - 即「使能时才把外部地址接进 `_a`」，而 `_a` 是这个读端口的实际地址；
   - 由于 `_a` 在 `when` 里被条件赋值，综合器会把它实现成「带使能的地址寄存器」，读数据自然延迟一拍。
4. 返回 `_port.get`（一个 `READ` 端口）。

读写复合端口 `readWrite` 类似，但开的是 `RDWR` 端口，并在 `when(isWrite)` 里决定本拍是写还是读。

#### 4.3.3 源码精读

`object SyncReadMem` 定义了 `ReadUnderWrite` 的三个别名，指向 FIRRTL 的 `fir.ReadUnderWrite` 枚举（[core/src/main/scala/chisel3/Mem.scala:209-248](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Mem.scala#L209-L248)）：

```scala
object SyncReadMem extends SyncReadMemObjIntf {
  type ReadUnderWrite = fir.ReadUnderWrite.Value
  val Undefined = fir.ReadUnderWrite.Undefined
  val ReadFirst = fir.ReadUnderWrite.Old     // 读旧值
  val WriteFirst = fir.ReadUnderWrite.New    // 读新值

  protected def _applyImpl[T <: Data](size: BigInt, t: T, ruw: ReadUnderWrite = Undefined)(
    implicit sourceInfo: SourceInfo): SyncReadMem[T] = {
    requireIsChiselType(t, "memory type")
    val mt = t.cloneTypeFull
    val mem = new SyncReadMem(mt, size, ruw, sourceInfo)
    mt.bind(MemTypeBinding(mem))
    pushCommand(DefSeqMemory(sourceInfo, mem, mt, size, ruw))   // 注意是 DefSeqMemory
    ModulePrefixAnnotation.annotate(mem)
    mem
  }
}
```

`ReadFirst` 映射到 `Old`、`WriteFirst` 映射到 `New`，命名上「读优先 = 拿到旧值」「写优先 = 拿到新值」，记忆时按「优先的一方决定了读到的值」来理解。`ruw` 默认 `Undefined`，并写进 `DefSeqMemory` 节点（这是 `DefMemory` 没有的字段）。

同步读的实现是本节核心（[core/src/main/scala/chisel3/Mem.scala:277-292](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Mem.scala#L277-L292)）：

```scala
private def _readImpl(addr: UInt, enable: Bool, clock: Clock, warn: Boolean)(
  implicit sourceInfo: SourceInfo): T = {
  var _port: Option[T] = None
  val _a = WireDefault(chiselTypeOf(addr), DontCare)
  when(enable) {
    _a := addr
    _port = Some(super._applyImpl(_a, clock, MemPortDirection.READ, warn))
  }
  _port.get
}
```

理解这段的关键是「`_a` 是端口地址，而 `_a` 被 `when(enable)` 条件赋值」。结合 u3-l5：`when` 描述的是带条件的连线，所以 `_a` 实际等价于「`enable ? addr : <保持>`」——这正是带使能的地址寄存器。端口本身开在 `when` 内部（`super._applyImpl` 即 `MemBase._applyImpl` → `makePort`），其地址引用 `i.ref` 取自 `_a`。综合后：地址在时钟沿采样进寄存器，读数据再延迟一拍出现 → 同步读。

注释（[L293-L294](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Mem.scala#L293-L294)）点明了设计意图：`do_read(addr)` 特意用 `do_read(addr, true.B)` 实现，保证两者行为一致。

读写复合端口 `readWrite` 开的是 `RDWR` 端口（[core/src/main/scala/chisel3/Mem.scala:310-331](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/Mem.scala#L310-L331)）：

```scala
private def _readWriteImpl(addr, data, enable, isWrite, clock, warn)(implicit sourceInfo: SourceInfo): T = {
  var _port: Option[T] = None
  val _a = WireDefault(chiselTypeOf(addr), DontCare)
  when(enable) {
    _a := addr
    _port = Some(super._applyImpl(_a, clock, MemPortDirection.RDWR, warn))
    when(isWrite) { _port.get := data }
  }
  _port.get
}
```

与纯读相比多了内层 `when(isWrite) { _port.get := data }`：同一个 `RDWR` 端口，`isWrite` 为真时本拍写 `data`、为假时本拍读。返回值在写时是未定义的（类注释里写明「the return value becomes undefined when this parameter is true」）。`MemBaseIntf`/`SyncReadMemIntf` 暴露的 `readWrite` 接口（[core/src/main/scala-2/chisel3/MemIntf.scala:263-294](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala-2/chisel3/MemIntf.scala#L263-L294)）有完整的使用示例，值得一看。

#### 4.3.4 代码实践

**实践目标**：用 `emitCHIRRTL` 直接观察 `SyncReadMem` 在 IR 文本里长什么样，建立「Scala 代码 → CHIRRTL 文本」的直觉。

**操作步骤**（示例代码）：

```scala
// 示例代码
import chisel3._
import chisel3.stage.ChiselStage

class SyncRd extends Module {
  val io = IO(new Bundle {
    val addr = Input(UInt(4.W))
    val data = Output(UInt(8.W))
  })
  val mem = SyncReadMem(16, UInt(8.W))
  io.data := mem.read(io.addr)
}

// 打印 CHIRRTL 文本（未经 firtool 优化的中间形态）
println(ChiselStage.emitCHIRRTL(new SyncRd))
```

**需要观察的现象**：输出里应有一行形如 `smem mem : UInt<8>[16]`（`smem` = sequential memory，对应 `DefSeqMemory`），以及一个 `mport ... read ...` 风格的读端口声明。对比改成 `Mem(16, UInt(8.W))` 后会变成 `mem ...`（无 `s` 前缀，对应 `DefMemory`）。

**预期结果**：`SyncReadMem` → `smem`，`Mem` → `mem`，一字之差对应「同步读 vs 组合读」。这正是 Converter 里 `CDefMemory(..., false)` 与 `CDefMemory(..., true, ruw)` 的区别在文本层的投影。

**待本地验证**：`emitCHIRRTL` 不需要 firtool，纯 Scala 即可运行，适合在没有 CIRCT 环境时做 IR 层面的验证。

#### 4.3.5 小练习与答案

**练习 1**：`SyncReadMem._readImpl` 里为什么要先声明 `val _a = WireDefault(chiselTypeOf(addr), DontCare)`，而不是直接 `super._applyImpl(addr, ...)`？

**答案**：为了实现「读使能」。若直接用 `addr`，端口地址永远等于外部地址，没有「禁止」的概念。引入 `_a` 并在 `when(enable)` 里才把 `addr` 接给它，使 `_a` 等价于一个带使能的地址寄存器：`enable` 为假时 `_a` 保持上一拍值（不更新），从而实现「读端口暂停」。附带地，地址经过这个寄存器，读数据自然延迟一拍，得到同步读语义。

**练习 2**：`SyncReadMem(16, UInt(8.W), SyncReadMem.ReadFirst)` 与默认 `Undefined` 在生成的 SystemVerilog 上可能有何差异？

**答案**：`ReadFirst`（`Old`）显式要求「同地址同拍既读又写时读旧值」，下游 firtool 会据此生成「读旁路保持旧值」的逻辑或保留一个读端口；而 `Undefined` 把这个选择权交给下游优化器，它可能合并读写端口、省去旁路逻辑而得到面积更小但行为「未定义」的实现。若你的设计依赖确定的读写顺序，就应显式指定 `ReadFirst` / `WriteFirst`。

**练习 3**：`mem.readWrite(addr, wdata, en, isWrite)` 返回的硬件值，在 `isWrite == true.B` 的那一拍应当如何使用？

**答案**：类注释明确「the return value becomes undefined when this parameter is true」。因此 `isWrite` 为真的那一拍，返回值不可依赖；只有 `isWrite` 为假（即本拍是读）时，返回值才是上一拍请求地址处的数据（且要等到这一拍才出现，因为是同步读）。设计电路时通常用一个寄存器把 `isWrite` 也延迟一拍，再决定是否采信读返回值。

---

### 4.4 黑盒化选项：useSRAMBlackbox 与 SRAM 工具

#### 4.4.1 概念说明

`Mem` / `SyncReadMem` 描述的是「抽象内存」，最终由 CIRCT（firtool）综合成具体的 RAM 实现。但有时你想**强制**把内存变成一个边界清晰的 SystemVerilog 黑盒——例如要让后端工具用自己的 SRAM 编译器、或者要在多个 Chisel 模块间复用同一份 RAM 模型。Chisel 提供两条相关途径：

- **`Builder.useSRAMBlackbox` 选项**：一个全局开关。开启后，`chisel3.util.SRAM(...)` 不再生成抽象的 FIRRTL 内存，而是实例化一个内联 SystemVerilog 的 `SRAMBlackbox`。
- **`chisel3.util.SRAM`**：一个高层多端口内存生成器，支持显式声明若干读 / 写 / 读写端口、读/写延迟、字节掩码、初始文件，并可附带 `SRAMDescription`（用 u8-l4 的 Properties 对象模型携带元数据，供 CIRCT 侧读取）。

注意区分：`useSRAMBlackbox` 影响的是 `util.SRAM`，而**不**直接影响裸 `Mem` / `SyncReadMem`。裸 `SyncReadMem` 仍是抽象内存；`SRAM` 才是封装好的、可黑盒化的多端口内存。

#### 4.4.2 核心流程

`SRAM.apply(...)` 的实现分发（[src/main/scala/chisel3/util/SRAM.scala:1026-1036](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/SRAM.scala#L1026-L1036)）：

```
SRAM(...) ──▶ memInterface_impl(...)
                ├── if (Builder.useSRAMBlackbox)  → memInterface_blackbox_impl(...)  // 实例化 SRAMBlackbox
                └── else                          → 生成 FirrtlMemory（抽象 FIRRTL 内存）
```

- 关闭时（默认）：走 `FirrtlMemory` 路径，端口引用挂到 `SramPortBinding` / `FirrtlMemTypeBinding`，类型翻译成 `fir.DefMemory`（见 [Converter.scala:170-180](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala#L170-L180)），由 firtool 决定如何实现。
- 开启时：走 `memInterface_blackbox_impl`，`Instantiate(new SRAMBlackbox(...))` 实例化一个内联 SV 的黑盒模块，用户接口 `_out` 的各端口逐字段连到黑盒的 `R` / `W` / `RW` 端口上。

`useSRAMBlackbox` 这一字段定义在 `ChiselOptions`（[src/main/scala/chisel3/stage/ChiselOptions.scala:20](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/stage/ChiselOptions.scala#L20)），由 `Builder` 暴露为只读访问器（[core/src/main/scala/chisel3/internal/Builder.scala:967](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/Builder.scala#L967)）。

#### 4.4.3 源码精读

`SRAM.memInterface_impl` 的分发判断（[src/main/scala/chisel3/util/SRAM.scala:1026-1036](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/SRAM.scala#L1026-L1036)）：

```scala
if (Builder.useSRAMBlackbox)
  return memInterface_blackbox_impl(size, tpe, readPortClocks, writePortClocks,
    readwritePortClocks, memoryFile, evidenceOpt, sourceInfo)
```

黑盒分支会实例化 `SRAMBlackbox`（[src/main/scala/chisel3/util/SRAM.scala:936-948](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/SRAM.scala#L936-L948)）：

```scala
val mem = Instantiate(
  new SRAMBlackbox(
    new CIRCTSRAMParameter(
      s"sram_${numReadPorts}R_${numWritePorts}W_${numReadwritePorts}RW_${maskGranularity}M_${size}x${tpe.getWidth}",
      numReadPorts, numWritePorts, numReadwritePorts, size.intValue, tpe.getWidth, maskGranularity)))
```

`SRAMBlackbox` 是个 `FixedIOExtModule` + `HasExtModuleInline`（[src/main/scala/chisel3/util/SRAM.scala:205-207](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/SRAM.scala#L205-L207)），它通过 `setInline` 把一段手写 SystemVerilog（含 `reg Memory[...]`、读写端口的 `always` 块）内联到输出目录。模块名按端口数与位宽参数化（如 `sram_1R_1W_0RW_0M_16x8`），保证不同配置得到不同模块。

非黑盒分支则压入 `FirrtlMemory` 命令（[src/main/scala/chisel3/util/SRAM.scala:1095-1107](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/SRAM.scala#L1095-L1107)），该节点在 Converter 中翻译成 `fir.DefMemory`（[Converter.scala:170-180](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/core/src/main/scala/chisel3/internal/firrtl/Converter.scala#L170-L180)），带显式的 `readLatency` / `writeLatency` 和读/写/读写端口名列表。

> 拓展：`SRAM` 还会用 u8-l4 的 Properties 机制生成一个 `SRAMDescription`（深度、位宽、端口数等），通过 `Property` 注解透传给 CIRCT，方便下游做 OMR（对象模型细化）。这是「内存 + 元数据」的现代写法，初学阶段了解即可。

#### 4.4.4 代码实践

**实践目标**：对比 `useSRAMBlackbox` 开 / 关时，`util.SRAM` 生成的电路边界差异。

**操作步骤**（示例代码）：

```scala
// 示例代码
import chisel3._
import chisel3.util.{SRAM, SRAMInterface}
import chisel3.stage.ChiselStage

class SramDemo extends Module {
  val io = IO(new Bundle {
    val raddr = Input(UInt(4.W))
    val waddr = Input(UInt(4.W))
    val wdata = Input(UInt(8.W))
    val we    = Input(Bool())
    val rdata = Output(UInt(8.W))
  })
  // 1 读口 + 1 写口 的 SRAM
  val sram = SRAM(size = 16, tpe = UInt(8.W), numReadPorts = 1, numWritePorts = 1, numReadwritePorts = 0)
  sram.writePorts(0).address := io.waddr
  sram.writePorts(0).data    := io.wdata
  sram.writePorts(0).enable  := io.we
  sram.readPorts(0).address  := io.raddr
  sram.readPorts(0).enable   := true.B
  io.rdata := sram.readPorts(0).data
}
```

**需要观察的现象**：默认（`useSRAMBlackbox = false`）生成的是抽象内存（CHIRRTL 里是 `mport` / FIRRTL memory）。若在 elaboration 选项里开启 `useSRAMBlackbox`，则会多出一个形如 `sram_1R_1W_0RW_...` 的独立模块实例，并附带一份内联 `.sv` 文件。

**预期结果**：开黑盒后，顶层模块通过「实例化子模块」访问内存，内存的内部实现被封装在子模块里；这便于把该子模块替换为厂家的 SRAM 编译器输出。

**待本地验证**：`useSRAMBlackbox` 通常通过 `ChiselOptions` 注解或 stage 选项设置，具体设置方式请查阅当前版本的 `circt.stage.Shell` / `chisel3.stage` 选项；本环境未实测开启命令，标注为「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`useSRAMBlackbox` 会影响裸 `SyncReadMem` 吗？

**答案**：不会。它只影响 `chisel3.util.SRAM`（在 `memInterface_impl` 开头判断）。裸 `SyncReadMem` 仍然生成 `DefSeqMemory` 抽象内存，由 firtool 决定实现。要把内存黑盒化，应使用 `util.SRAM` 并开启该选项。

**练习 2**：`SRAM` 为什么要求「至少一个读访问者且至少一个写访问者」（即 `R+RW > 0` 且 `W+RW > 0`）？

**答案**：见 `memInterface_impl` 的校验（[SRAM.scala:1042-1053](https://github.com/chipsalliance/chisel/blob/b2a0e030da9a3e90b8221436b5317afb370f3347/src/main/scala/chisel3/util/SRAM.scala#L1042-L1053)）。纯只读或纯只写的内存在 SRAM 物理实现上不合理（SRAM 宏通常同时具备读 / 写能力），且 FIRRTL 的 `mem` 若无某方向的端口也无意义。只读存储应改用 `Mem` / 加载文件等其他机制。

**练习 3**：黑盒分支与非黑盒分支生成的 IR 节点分别是什么？

**答案**：非黑盒分支压入 `FirrtlMemory`（IR.scala:342），Converter 翻成 `fir.DefMemory`，是一块「抽象」的 FIRRTL 内存。黑盒分支不压 `FirrtlMemory`，而是 `Instantiate(new SRAMBlackbox(...))`，生成一个普通的模块实例（`DefInstance`）加一份内联 SystemVerilog——内存从「抽象节点」变成了「实体子模块」。

---

## 5. 综合实践

把本讲三块内容（端口机制、`SyncReadMem` 同步时序、读写同址语义）串起来，实现题目要求的**16 深度、8 位宽单口 RAM**。

**任务**：用 `SyncReadMem` 实现一个单口 RAM：当 `we` 为高时按 `addr` 写 `wdata`（同步写）；否则按 `addr` 读，读数据在**下一拍**出现在 `rdata`（同步读）。生成 SystemVerilog 并核对时序。

**参考实现**（示例代码）：

```scala
// 示例代码
import chisel3._
import chisel3.stage.ChiselStage

class SinglePortRAM extends Module {
  val io = IO(new Bundle {
    val addr  = Input(UInt(4.W))     // 16 深度 → 4 位地址
    val wdata = Input(UInt(8.W))     // 8 位宽
    val we    = Input(Bool())
    val rdata = Output(UInt(8.W))
  })

  val mem = SyncReadMem(16, UInt(8.W))

  // we 为真 → 写；为假 → 读。用 readWrite 复合端口实现「单口」
  io.rdata := mem.readWrite(io.addr, io.wdata, enable = true.B, isWrite = io.we)
}

object SinglePortRAMApp extends App {
  println(ChiselStage.emitSystemVerilog(new SinglePortRAM))
}
```

**验证要点**：

1. **同步写**：在 Verilog 里找到 `always @(posedge clock)`，确认写发生在时钟沿，且受 `we` 控制。
2. **同步读**：确认 `rdata` 来自一个寄存器（读地址先寄存一拍），而非组合直通。可在心里推演：第 0 拍给 `addr=5`、`we=0`，第 1 拍 `rdata` 才等于地址 5 处的值。
3. **地址位宽**：`addr` 应是 4 位（`truncateIndex` 把它截到 `bitLength(16-1) = 4` 位）。
4. **进阶**：把 `readWrite` 换成独立的 `read` + `write`（双口 RAM），对比生成的端口数与上文 4.1.4 的「两读口」现象。
5. **进阶**：把 `SyncReadMem` 换成 `Mem`，重新生成 Verilog，对比读路径从「寄存一拍」变成「组合直通」——这就是 4.2 与 4.3 的本质差异。

**待本地验证**：如已按 u1-l2 配好 firtool，`emitSystemVerilog` 可直接输出；否则可先用 `emitCHIRRTL` 看 `smem` 与 `mport` 结构，再在具备 CIRCT 的环境中确认 SystemVerilog 时序。

## 6. 本讲小结

- `Mem` 是**组合读、同步写**内存（IR 节点 `DefMemory`，Converter 翻成 `CDefMemory(..., false)`）；`SyncReadMem` 是**同步读、同步写**内存（`DefSeqMemory`，多一个 `readUnderWrite` 字段，翻成 `CDefMemory(..., true, ruw)`）。两者在 IR 层只差这一个布尔位与 `ruw`。
- 读写不是直接作用于内存对象，而是每次 `mem(addr)` / `read` / `write` 都通过 `MemBase.makePort` **开一个端口**（`DefMemPort`），并 `bind` 成 `MemoryPortBinding`。端口数 = 调用次数，与地址是否相同无关。
- 端口方向有四种 `MemPortDirection`：`INFER`（按连线推断）/ `READ` / `WRITE` / `RDWR`（读写复合，由 `SyncReadMem.readWrite` 显式生成，`isWrite` 控制本拍方向）。
- `SyncReadMem` 的「同步读」并非魔法，而是在 `_readImpl` 里主动插入 `WireDefault(..., DontCare)` + `when(enable)`，把地址变成「带使能的寄存器」，从而让读数据延迟一拍。
- 所有内存构造依然遵循**只登记不施工**：构造内存压 `DefMemory` / `DefSeqMemory`，开端口压 `DefMemPort`，写操作额外压一条 `Connect`；真正的 Verilog 由下游 CIRCT（firtool）产出。
- 进阶选项 `Builder.useSRAMBlackbox` 可让 `chisel3.util.SRAM`（多端口高层内存）改走 `SRAMBlackbox` 实例化路径，把抽象内存封装成内联 SystemVerilog 黑盒，便于对接厂家 SRAM 编译器或携带 `SRAMDescription` 元数据。

## 7. 下一步学习建议

- **学完本讲，单元 3（模块与连线）已完结**。建议回头用一个稍大的例子（如一个带寄存器堆 + 状态机的小处理器译码器）综合运用 u3-l1～u3-l6，巩固「Module / IO / 方向 / 连线 / when / Reg / Mem」全套基础词汇。
- **进入单元 6（标准库 `chisel3.util`）**：u6-l2 的 `Queue`（FIFO）内部存储体正是 `Mem`，会用到本讲的 `Mem` 组合读特性；u6-l1 的 `Decoupled` 握手接口与 `Queue` 配合是 Chisel 最常见的流式设计范式，是 `SyncReadMem` 的天然应用场景（带使能的流水线缓冲）。
- **想深入内部机制**：可读 u4-l2（命令记录与内部 FIRRTL IR），对照本讲的 `DefMemory` / `DefMemPort` 看 `Command` 体系；以及 u4-l5（Converter），看 `CDefMemory` / `CDefMPort` 如何进一步变成 `firrtl.ir` 节点交给 CIRCT。
- **想了解黑盒化与对象模型**：u8-4（Properties）讲解 `Class` / `Property` / `Object`，是理解 `SRAMDescription` 如何携带元数据的前提；u8-l5（svsim）则讲解如何对包含内存的电路做仿真验证。
