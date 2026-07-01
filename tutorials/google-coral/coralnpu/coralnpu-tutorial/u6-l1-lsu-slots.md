# LSU 加载存储单元

## 1. 本讲目标

标量核派发一条 `lw`/`sw` 之后，谁来真正把字节搬进/搬出存储器？答案就是 **LSU（Load Store Unit，加载存储单元）**。本讲聚焦 CoralNPU 标量核的 LSU，学完后你应当能够：

- 说清 **slot（槽）** 这张「字节粒度状态表」是如何跟踪一次访存操作逐字节的完成情况的；
- 复述 slot 的 **四个生命周期状态**（Idle → Vector Update → Transfer Memory → Writeback），并解释「全核只有一个 slot」带来的串行约束；
- 在源码里指出 **ibus / dbus / ebus** 三条总线分别服务于哪类地址区段（ITCM / DTCM / 外部），以及它们的选择条件；
- 描述 **scatter/gather** 如何把零散的字节打包成一次总线事务、又如何从一拍 128 位数据里收集所需字节；
- 理解向量访存如何经 **rvv2lsu / lsu2rvv** 接口与 RVV 后端交互。

本讲是「存储子系统」单元的入口，承接 u4-l4（派发与退休）和 u3-l4（总线互联）。

## 2. 前置知识

在进入 LSU 之前，请确认你已理解下列概念（在 u1、u3、u4 讲义中讲过）：

- **取指/访存宽通道**：CoralNPU 的数据总线很宽，生产 SoC 配置下为 **128 位（16 字节）**（见 [Parameters.scala:140](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/Parameters.scala#L140) 默认 256，但 SoC 经 `--lsuDataBits=128` 覆盖）。LSU 的 slot 表正是按这个「一行多少字节」来组织的。
- **三类地址区段**：ITCM（指令紧耦合存储，存代码）、DTCM（数据紧耦合存储，存数据）、以及它们之外的外部区段（经 AXI 访问片外存储/外设）。地址归属判定来自 u4-l1 的 `MemoryRegion.contains`。
- **ready-valid 握手**：所有总线事务都用 `valid`/`ready` 握手，`fire = valid && ready`。
- **派发到执行单元的数据流**：派发器在译码周期把命令送进执行单元的 `req`，操作数（rs1 基址、rs2 数据）经寄存器堆读端口在执行周期送达。

一个关键直觉：**LSU 不是「一条指令 = 一次总线事务」**。一条 `sw` 要写的 4 个字节可能落在同一根 16 字节行内，也可能跨行；LSU 用一张表逐字节记录「这个字节还没搬完」，然后**一行一行地**把活干完。这张表就是 slot。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [doc/microarch/lsu.md](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/doc/microarch/lsu.md) | LSU 的设计文档：slot 概念、word-store 的 slot 表例子、四态生命周期、全部接口信号表。 |
| [hdl/chisel/src/coralnpu/scalar/Lsu.scala](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/Lsu.scala) | LSU 的全部硬件实现：`LsuOp` 枚举、`LsuSlot` 数据结构（含 scatter/gather/loadUpdate/writeback 等纯函数）、`LsuV2` 顶层状态机。 |
| [hdl/chisel/src/coralnpu/Interfaces.scala](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/Interfaces.scala) | `IBusIO` / `DBusIO` / `EBusIO` 三类总线端口的字段定义。 |
| [hdl/chisel/src/common/ScatterGather.scala](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/common/ScatterGather.scala) | `Gather` / `Scatter` 两个通用向量重排函数，是 LSU 打包/收集字节的数学原语。 |
| [tests/cocotb/align_test.cc](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/cocotb/align_test.cc) | 对齐测试程序：在偏移 0..3 处反复写读 int32/int16 数组，正好检验 LSU 对非对齐访问的处理。 |

---

## 4. 核心概念与源码讲解

### 4.1 Slot：一张「字节粒度」的状态表

#### 4.1.1 概念说明

最朴素的访存实现是「一条指令对应一次总线读写」。但它处理不了两类常见情况：

1. **跨行访问**：一次 4 字节的 `lw` 如果恰好横跨两根 16 字节行，需要两次总线读；
2. **向量访存**：一条向量 load/store 要搬几十甚至上百个字节，且每个字节的地址可能完全不连续（比如 indexed load，地址来自向量寄存器堆里的下标数组）。

CoralNPU 的解法是 **slot**：一个 slot 内部维护一张「每个字节一行」的表，逐字节跟踪这次访存操作的完成情况。文档原话是：

> A slot is a data structure which manages the state of a single dispatched LSU operation and determines what memory transaction should be performed. At its core, there exists a table in each slot which tracks which part of the memory operation has been completed.
> —— [lsu.md:10-15](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/doc/microarch/lsu.md#L10-L15)

每一行有三列：`Active`（这一字节是否还需要处理）、`Address`（这一字节的目标字节地址）、`Data`（要写入或已读回的字节值）。总线事务每搬完一个字节，就把对应行的 `Active` 翻成 0；当全表 `Active` 都为 0 时，这次访存操作就完成了。

#### 4.1.2 核心流程

先看文档给的一个 word-store 的 slot 表例子（向地址 `0xDEADBEEF` 写一个字，值 `0x67452301`，小端序）：

| Index | Active | Address | Data |
| ----- | ------ | ------- | ---- |
| 0 | 1 | 0xDEADBEEF | 0x01 |
| 1 | 1 | 0xDEADBEF0 | 0x23 |
| 2 | 1 | 0xDEADBEF1 | 0x45 |
| 3 | 1 | 0xDEADBEF2 | 0x67 |
| 4 | 0 | … | 0x00 |
| … | 0 | … | 0x00 |

（来源：[lsu.md:17-27](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/doc/microarch/lsu.md#L17-L27)。文档首行的字节地址延续略有笔误，本质是连续 4 个字节地址、4 个字节值。）

slot 表随时间演化的伪代码：

```
入队时(fromLsuUOp)：          # 一个对齐的 word-store
  for i in 0..3: active[i]=1, data[i]=word的相应字节
  for i in 4..15: active[i]=0

每个周期(Transfer Memory)：
  line = 最低 index 的 active 行所在的「行地址」(addr >> elemBits)
  把本 slot 里所有「行地址==line 且 active」的字节打包(scatter)成一次写事务
  总线 fire 后：这些字节的 active 翻 0
  若还有 active → 继续选下一行；否则 → Writeback
```

关键点：slot **不要求** active 的字节地址连续。它们只要落在同一根 16 字节行里，就能被「捆」进同一次事务；落在不同行的，分多次事务逐行处理。这就是它能优雅处理跨行访问与向量访存的原因。

> **数学上**，slot 表本质是一个以字节地址低 `elemBits` 位为索引的重排问题。设总线行宽为 \( B = 2^{\text{elemBits}} \) 字节（生产配置 \(B=16\)），slot 的第 \(i\) 个字节地址 \(a_i\) 落在行号 \(\lfloor a_i / B \rfloor\)。同行的字节集合 \(\{i \mid \lfloor a_i/B\rfloor = L\}\) 构成一次事务，其中第 \(a_i \bmod B\) 字节位置填入 `data[i]`。

#### 4.1.3 源码精读

slot 表的结构定义在 `LsuSlot` 类里，三列正好对应文档：

```scala
class LsuSlot(p: Parameters, bytesPerSlot: Int) extends Bundle {
  val elemBits = log2Ceil(p.lsuDataBytes)          // 行地址移位量（16字节行→4）
  ...
  val active = Vec(bytesPerSlot, Bool())            // Active 列
  val addrs  = Vec(bytesPerSlot, UInt(p.lsuAddrBits.W))  // Address 列
  val data   = Vec(bytesPerSlot, UInt(8.W))         // Data 列（每行1字节）
  ...
}
```

见 [Lsu.scala:372-382](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/Lsu.scala#L372-L382)。注意 `bytesPerSlot` 在 `LsuV2` 里被**硬编码为 16**：

```scala
val nextSlot = LsuSlot.fromLsuUOp(opQueue.io.dataOut(0), p, 16)
val slot = RegInit(LsuSlot.inactive(p, 16))
```

见 [Lsu.scala:891-895](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/Lsu.scala#L891-L895)。这与生产 SoC 的 128 位（16 字节）数据总线一一对应，故一张 slot 表正好覆盖一根总线行。

slot 表的**初始化**（一条新指令入队时如何填表）在 `LsuSlot.fromLsuUOp`：

```scala
val active = MuxUpTo1H(0.U(bytesPerSlot.W), Seq(
  uop.op.isOneOf(LsuOp.LB, LsuOp.LBU, LsuOp.SB) -> "b1".U(bytesPerSlot.W),      // 1字节
  uop.op.isOneOf(LsuOp.LH, LsuOp.LHU, LsuOp.SH) -> "b11".U(bytesPerSlot.W),     // 2字节
  uop.op.isOneOf(LsuOp.LW, LsuOp.SW, LsuOp.FLOAT) -> "b1111".U(bytesPerSlot.W), // 4字节
  LsuOp.isVector(uop.op) -> 0.U(bytesPerSlot.W),   // 向量：初始全0，由 Vector Update 填
))
result.active := active.asBools
...
result.addrs := Mux(
  uop.op.isOneOf(LsuOp.VLOAD_STRIDED, LsuOp.VSTORE_STRIDED), <跨步地址>,
  VecInit((0 until bytesPerSlot).map(i => uop.addr + i.U)))   // 标量：连续字节地址
```

见 [Lsu.scala:714-730](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/Lsu.scala#L714-L730)。可以看到：一条标量 `sw` 初始化时只有 4 个 active 字节，地址连续；而向量操作初始全 0，要等 `Vector Update` 阶段从 RVV 后端取来掩码与地址后再填。

如果想在实际仿真中**亲眼看到**这张表的演化，LSU 提供了一个调试打印函数 `toPrintable`，逐行打印 `index / active / addr / data`：

```scala
override def toPrintable: Printable = {
  val lines = (0 until bytesPerSlot).map(i =>
      cf"  $i: ${active(i)}, 0x${addrs(i)}%x, 0x${data(i)}%x\n")
  cf"store: $store\n  op: ${op}\n  pc: 0x${pc}%x\n" + ... + lines.reduce(_+_)
}
```

见 [Lsu.scala:631-640](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/Lsu.scala#L631-L640)。在仿真里用 `printf` 调用它即可观察到与文档表例子完全一致的输出。

#### 4.1.4 代码实践

**实践目标**：用一个具体的对齐 word-store，手工推演 slot 表的演化，建立对「逐字节 active 位」的直觉。

**操作步骤**：

1. 假设有一条 `sw x14, 4(x10)`，其中 `x10 = 0x00010000`（DTCM 基址），`x14 = 0x67452301`。目标地址 = `0x00010004`，4 字节对齐。
2. 按 `fromLsuUOp` 的逻辑，画出**入队后**这张 16 行 slot 表的 `Active / Address / Data` 三列（地址列只需写出 active 的 4 行）。
3. 计算 `elemBits = 4`，于是这 4 个字节的「行地址」都是 `0x00010004 >> 4 = 0x1000`，即它们全落在同一根 16 字节行（行基址 `0x10000`）。
4. 推演 **Transfer Memory 第一拍**：scatter 把这 4 个字节捆成一次写事务，`wmask` 的哪几个比特为 1？dbus fire 后，slot 表变成什么样？

**需要观察的现象**：

- 入队后仅 index 1/2/3/4（对应字节偏移 1/2/3/4，即行内第 1..4 字节）的 active 为 1 —— 等等，请你自己确认到底哪几个 index 被置位（提示：`active` 是按 `uop.addr + i` 的**字节地址低 4 位**对齐到表 index 的，地址 `0x...04` 对应 index 4）。
- `wmask` 应为 `0x00F0`（行内第 4..7 比特）。
- dbus fire 后这 4 行 active 全部清 0，slot 立刻满足「无 active」→ 进入 Writeback；因为 `sw` 是 store（`pendingWriteback=false`），Writeback 也被跳过，slot 直接回到 Idle。

**预期结果**：一次对齐的 DTCM word-store 只产生**一次** dbus 写事务，slot 一个周期内就把 4 个 active 位全部清掉。

> 待本地验证：若你在 Verilator 仿真里对 `slot` 寄存器加 `printf(p"${slot.toPrintable}")`，应能看到上述表格逐拍变化。

#### 4.1.5 小练习与答案

**练习 1**：如果把上面例子换成 `sw x14, 0(x10)`（地址 `0x00010000`），slot 表的 active 行 index 和 `wmask` 分别是什么？

**答案**：active 行 index 为 0/1/2/3，`wmask = 0x000F`。

**练习 2**：一次 `sb`（存单字节）入队后，slot 表有几个 active 位？为什么向量 store 初始 active 全是 0？

**答案**：`sb` 只有 1 个 active 位（`"b1"`）。向量 store 初始 active 全 0，因为它的有效字节由向量掩码（`rvv2lsu.mask`）决定、地址由下标/跨步决定，都要等到 `Vector Update` 状态从 RVV 后端取来后才能填入（见 4.4 节）。

---

### 4.2 ibus / dbus / ebus：三条总线各管一段地址

#### 4.2.1 概念说明

slot 决定了「搬什么字节」，但「往哪根总线发」由**地址落在哪个区段**决定。LSU 对外暴露三条总线端口（见 [Interfaces.scala:51-131](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/Interfaces.scala#L51-L131)）：

| 端口 | 全称 | 服务区段 | 方向 | 特点 |
| --- | --- | --- | --- | --- |
| **ibus** | Instruction Bus | **ITCM** | 只读（load） | 复用取指总线；对 ITCM 的 store 会触发 fault |
| **dbus** | Data Bus | **DTCM** | 读/写 | LSU 的主力数据通道，单拍 SRAM |
| **ebus** | External Bus | **外部 / 外设** | 读/写 | 经 `DBus2Axi` 转成 AXI 事务出片外，带 fault 回报 |

注意 `EBusIO` 内部其实**包了一个完整的 `DBusIO`**（[Interfaces.scala:127-131](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/Interfaces.scala#L127-L131)），再加 `internal`（标记是否命中片内外设）和 `fault`（外部总线错误回报）。所以 ebus 在协议层和 dbus 同构，只是目的地不同。

#### 4.2.2 核心流程

每个周期，LSU 先用当前 slot 的最低 index active 行算出「目标行地址」，然后**按地址区段把事务路由到三总线之一**：

```
targetLineAddr = 选中行的行地址 (<<4 还原成字节地址)
itcm     = targetLineAddr 落在 IMEM 区?      → 走 ibus（且仅 load）
dtcm     = targetLineAddr 落在 DMEM 区?      → 走 dbus
peri     = targetLineAddr 落在 Peripheral 区? → 走 ebus（internal=1）
external = 以上都不是?                        → 走 ebus（internal=0，出 AXI）
```

三条总线的 `fire` 信号互斥（`assert(PopCount(...) <= 1.U)`），一拍最多一条总线成交。

一个重要的不对称：**ibus 只能 load 不能 store**。因为 ITCM 是指令存储，LSU 复用取指通路来加速「从 ITCM 取数据字」这种合法操作；但往 ITCM 写数据是非法的，硬件会直接报 store fault（精确异常）。

#### 4.2.3 源码精读

区段判定是纯组合逻辑，用 `p.m`（参数里的内存区表）逐区 `contains`：

```scala
val targetLineAddr = targetLine.bits << 4
val itcm = p.m.filter(_.memType == MemoryRegionType.IMEM)
              .map(_.contains(targetLineAddr)).reduceOption(_ || _).getOrElse(false.B)
val dtcm = p.m.filter(_.memType == MemoryRegionType.DMEM)
              .map(_.contains(targetLineAddr)).reduceOption(_ || _).getOrElse(true.B)
val peri = p.m.filter(_.memType == MemoryRegionType.Peripheral)
              .map(_.contains(targetLineAddr)).reduceOption(_ || _).getOrElse(false.B)
val external = !(itcm || dtcm || peri)
```

见 [Lsu.scala:925-934](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/Lsu.scala#L925-L934)。注意 `dtcm` 的默认值是 `true.B`（若参数里没声明 DMEM 区，就把未知地址当 DTCM），这是为裸核最小配置留的兜底。

三条总线的驱动条件：

```scala
// ibus：仅 ITCM + 仅 load
io.ibus.valid := loadUpdatedSlot.activeTransaction() && itcm && !slot.store && !faultReg.valid

// dbus：DTCM，读/写皆可
io.dbus.valid := dtcm && Mux(slot.store, slot.activeTransaction(),
                                       loadUpdatedSlot.activeTransaction()) && !faultReg.valid

// ebus：外部或外设，读/写皆可
io.ebus.dbus.valid := (external || peri) && Mux(slot.store, slot.activeTransaction(),
                                                          loadUpdatedSlot.activeTransaction()) && !faultReg.valid
```

见 [Lsu.scala:942-960](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/Lsu.scala#L942-L960)。三处都要求 `activeTransaction()`（即「slot 有 active 字节且不在等向量更新」）且无已记录 fault。

「往 ITCM 写」被显式判为 fault：

```scala
ibusFault.valid := loadUpdatedSlot.activeTransaction() && itcm && slot.store
ibusFault.bits.write := true.B
ibusFault.bits.epc := slot.pc
```

见 [Lsu.scala:985-989](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/Lsu.scala#L985-L989)。fault 一旦成立，会被锁进 `faultReg`，slot 直接清空回 Idle，并由 ROB 按序提交异常（精确异常，见 u4-l4）。

`io.ebus.internal := peri` 这一行（[Lsu.scala:968](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/Lsu.scala#L968)）把「这次外部事务其实是访问片内外设」的信息随事务送出，供下游桥接器区分外设与真外部存储。

#### 4.2.4 代码实践

**实践目标**：用对齐测试程序 `align_test.cc` 验证「外部地址走 ebus」，并标注三总线的服务区段。

**操作步骤**：

1. 打开 [tests/cocotb/align_test.cc](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/cocotb/align_test.cc)。注意它把数组指针指向 `0x20000000`：
   ```cpp
   volatile int8_t* extmem = reinterpret_cast<volatile int8_t*>(0x20000000L);
   ```
2. 默认内存映射里 ITCM=`0x0`、DTCM=`0x10000`、CSR=`0x30000`（见 u4-l1）。`0x20000000` 不属于任何已声明区段，按上面源码 `external = !(itcm||dtcm||peri)` 为真 → **走 ebus**。
3. 阅读 [Lsu.scala:928-934](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/Lsu.scala#L928-L934)，填一张「地址区段 → 总线端口」对照表。

**需要观察的现象**：`align_test.cc` 在偏移 0/1/2/3 处反复读写 int32/int16 数组；其中**非对齐**的访问（偏移 1、2、3）会触发 `opSize` 走「size=16 全行」路径（见 4.3 节），而这些事务**全部经 ebus**到达外部从机（仿真里是 cocotb 的 `EXTMEM`，见 u2-l4）。

**预期结果**：对照表如下——

| 地址区段 | 区段类型 | 总线端口 | 备注 |
| --- | --- | --- | --- |
| `0x0` ~ ITCM 末 | IMEM | ibus | 仅 load；store 报 fault |
| `0x10000` ~ DTCM 末 | DMEM | dbus | 读/写主力 |
| `0x20000000` 等片外 | external | ebus | 经 AXI 出片外 |
| 外设寄存器区 | Peripheral | ebus（internal=1） | 区分外设与片外存储 |

> 待本地验证：在 cocotb 测试台里跑 `align_test`，用 `AxiSlave` 监听 master 端口，应能捕获到对 `0x20000000` 的非对齐访问被拆成 ebus 上的全行事务。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `dtcm` 的 `getOrElse` 默认值是 `true.B`，而 `itcm`/`peri` 是 `false.B`？

**答案**：这是裸核最小配置的兜底——若参数里没显式声明 DMEM 区，则把任何「未识别」地址默认当作数据存储访问（走 dbus 直连 SRAM），保证最小核仍能跑程序。生产 SoC 显式声明了全部区段后，这个默认值不再被触发。

**练习 2**：一条 `lw` 从 `0x4`（ITCM 内）取一个字，会走哪条总线？一条 `sw` 往 `0x4` 写呢？

**答案**：`lw` 走 **ibus**（ITCM load 旁路）。`sw` 不走任何总线——它会触发 `ibusFault`（store 到 ITCM 非法），fault 被记录后 slot 清空，由 ROB 提交 store 异常。

---

### 4.3 scatter 与 gather：把零散字节装进/拆出一次事务

#### 4.3.1 概念说明

slot 表是「按字节散列」的，但总线是「按行传输」的（一拍 128 位 = 16 字节）。两者之间需要一座桥：

- **scatter（散射）**：把 slot 里所有「落在同一行、且 active」的字节，按各自的行内偏移**装填**到一个 16 字节的写数据 `wdata` 和字节掩码 `wmask` 里。用于 **store**。
- **gather（收集）**：总线读回一整行 16 字节后，把 slot 真正需要的那些字节**挑出来**写进各自的 `data[i]`。用于 **load**。

这两个名字借自向量计算里的 scatter/gather，语义一致：scatter 是「把紧凑数据按下标打散到各处」，gather 是「按 下标从各处收回到紧凑数组」。

#### 4.3.2 核心流程

**store 的 scatter**（每个周期）：

```
targetLine = 最低 index active 行的行地址
对 slot 的每个字节 i：
  lineActive[i] = active[i] 且 addrs[i]的行地址 == targetLine
(wdata, wmask, selected) = Scatter(lineActive, 行内偏移, data)
  # wdata: 长度16的向量，按行内偏移填入 data[i]
  # wmask: 哪些行内位置被写
  # selected: 哪些 slot 字节被这次事务消费（用于清 active）
dbus/ebus 发出 wdata + wmask，fire 后这些字节 active 清 0
```

**load 的 gather**（读数据回来后那一拍）：

```
lineAddr, lineData = 上一拍 fire 的那次读
对 slot 的每个字节 i：
  lineActive[i] = active[i] 且 addrs[i]的行地址 == lineAddr
  gatheredData[i] = lineData 的第 (行内偏移) 字节
更新：active[i] &= ~lineActive[i]   # 这些字节读到了，清位
      data[i] = gatheredData[i]
```

注意一个时序细节：读数据是「fire 后**下一拍**才到」（文档 `rdata` 注释：*Arrives one cycle after hand-shake*）。所以 LSU 用一个 `readFired` 寄存器记住「上一拍发了哪条总线、读了哪一行」，本拍再用对应总线的 `rdata` 做 gather。

#### 4.3.3 源码精读

`LsuSlot.scatter` 把 active 字节装填成一次写事务，底层调通用 `Scatter`：

```scala
def scatter(lineAddr: UInt): (Vec[UInt], Vec[Bool], Vec[Bool]) = {
  val canScatter = store && (!LsuOp.isVector(op) || !pendingVector())
  val lineAddrs = lineAddresses()
  val lineActive = VecInit((0 until bytesPerSlot).map(i =>
      canScatter && active(i) & (lineAddrs(i) === lineAddr)))
  Scatter(lineActive, elemAddresses(), data)   // 通用散射原语
}
```

见 [Lsu.scala:583-589](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/Lsu.scala#L583-L589)。`elemAddresses()` 取每个字节地址的行内偏移（低 `elemBits` 位）作为散射目标下标。

通用 `Scatter` 的语义在 [ScatterGather.scala:60-109](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/common/ScatterGather.scala#L60-L109)：它返回三元组 `(result, resultMask, indicesSelected)`，其中 `resultMask` 正是写掩码 `wmask`，`indicesSelected` 正是 `selected`（用于清 active）。

`LsuV2` 顶层调它，并按 `slotFired` 把消费掉的字节清位：

```scala
val (wdata, wmask, wactive) = slot.scatter(targetLine.bits)
...
val storeUpdate = Mux(slotFired, wactive, VecInit.fill(16)(false.B))
val transactionUpdatedSlot = Mux(slot.store,
    slot.storeUpdate(storeUpdate), loadUpdatedSlot)
```

见 [Lsu.scala:937](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/Lsu.scala#L937) 与 [Lsu.scala:1008-1010](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/Lsu.scala#L1008-L1010)。`storeUpdate` 用 `wactive`（即 `indicesSelected`）清掉已写字节。

load 侧的 gather 在 `LsuSlot.loadUpdate`：

```scala
def loadUpdate(lineAddr: UInt, lineData: UInt): LsuSlot = {
  val lineActive = VecInit((0 until bytesPerSlot).map(i =>
      active(i) && (lineAddrs(i) === lineAddr)))
  val lineDataVec = UIntToVec(lineData, 8)
  val gatheredData = Gather(elemAddresses(), lineDataVec)   # 按行内偏移挑字节
  ...
  result.active := (0 until bytesPerSlot).map(i => active(i) & ~lineActive(i))
  result.data   := VecInit((0 until bytesPerSlot).map(
      i => Mux(lineActive(i), gatheredData(i), data(i))))
}
```

见 [Lsu.scala:492-521](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/Lsu.scala#L492-L521)。`Gather` 定义为 `result[i] = data[indices[i]]`（[ScatterGather.scala:22-27](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/common/ScatterGather.scala#L22-L27)），这里 `indices` 是行内偏移、`data` 是 16 字节行数据，正好「按偏移挑字节」。

读数据的「跨拍」由 `readFired` 寄存器+`readData` 多路选择实现：

```scala
val readData = MuxLookup(readFired.bits.bus, 0.U)(Seq(
    LsuBus.IBUS -> io.ibus.rdata,
    LsuBus.DBUS -> io.dbus.rdata,
    LsuBus.EXTERNAL -> io.ebus.dbus.rdata,
))
```

见 [Lsu.scala:897-901](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/Lsu.scala#L897-L901)。`readFired` 记录上一拍是哪条总线成交、读了哪一行，本拍据此选对应 `rdata` 并调 `loadUpdate`。

**非对齐如何被自然处理**：`LsuOp.opSize` 对未对齐访问把 `size` 直接放大到 16（整行）、地址对齐到行：

```scala
val size = MuxUpTo1H(16.U, Seq(
  op.isOneOf(LsuOp.LB, LsuOp.LBU, LsuOp.SB) -> 1.U,
  op.isOneOf(LsuOp.LH, LsuOp.LHU, LsuOp.SH) -> Mux(halfAligned, 2.U, 16.U),
  op.isOneOf(LsuOp.LW, LsuOp.SW, LsuOp.FLOAT) -> Mux(wordAligned, 4.U, 16.U),
  LsuOp.isVector(op) -> 16.U,
))
```

见 [Lsu.scala:125-131](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/Lsu.scala#L125-L131)。配合 slot 的逐字节表，一个跨行的非对齐访问会被拆成「按行分组的多次事务」，每次 scatter/gather 一根行，最终把零散字节凑齐——硬件无需专门的对齐异常路径。

#### 4.3.4 代码实践

**实践目标**：推演一次**跨 16 字节行边界**的非对齐 `lh`，体会 slot 如何用两次事务凑齐 2 字节。

**操作步骤**：

1. 设 `lh x15, 15(x10)`，`x10 = 0x00010000`，目标地址 `0x0001000F`。它读 2 字节：`0x1000F`（行 `0x1000`，行内偏移 15）和 `0x10010`（行 `0x1001`，行内偏移 0）。
2. 入队后 slot 表里 index 15 和 index 16... 等等，slot 只有 16 行（index 0..15）！请思考：这两个字节地址（`0x...0F` 与 `0x...10`）在 slot 表里分别落在哪个 index？（提示：表的 index 是**入队时按 `uop.addr + i` 顺序填的连续槽位**，而非字节地址低 4 位；active 位只有 2 个，对应 i=0、i=1，但它们的**行地址不同**。）
3. 第一拍 Transfer Memory：`targetAddress` 选 index 0（行 `0x1000`），dbus 读行 `0x1000`，下一拍 gather 出行内偏移 15 的字节，清掉 index 0 的 active。
4. 第二拍：剩下 index 1（行 `0x1001`）仍 active，再发一次 dbus 读，gather 出行内偏移 0 的字节。
5. 最后 `scalarLoadResult()` 把 2 字节拼成半字并符号扩展。

**需要观察的现象**：一次跨行 `lh` 产生**两次** dbus 读事务，slot 表的 active 位分两拍清零。

**预期结果**：load 结果 = `Cat(byte_at_0x10010, byte_at_0x1000F)` 符号扩展到 32 位。这正是 `align_test.cc` 在偏移 1/2/3 处仍能正确读写 int16/int32 的底层原因。

> 待本地验证：上述「两次事务」的推演可对照 `opSize` 在非对齐时返回 16、以及 `targetAddress` 每拍只选一个行的源码逻辑。

#### 4.3.5 小练习与答案

**练习 1**：一次对齐的 `lw`（4 字节，全在同一行）需要几次总线读？`wmask`/读掩码是什么形态？

**答案**：1 次。读这一行后 gather 行内连续 4 字节；若是 store，`wmask` 是 4 个连续置 1 的比特（如 `0x00F0`）。

**练习 2**：为什么 load 的 gather 要用 `readFired` 寄存器延迟一拍，而 store 的 scatter 不需要？

**答案**：读数据「握手后下一拍才到」（`rdata` 有 1 拍延迟），所以必须用寄存器记住上一拍读的是哪条总线、哪一行，本拍才能正确 gather。store 的写数据在**发事务的同一拍**就由 scatter 算好随 `wdata/wmask` 送出，无需延迟。

---

### 4.4 向量访存：经 rvv2lsu / lsu2rvv 与 RVV 后端交互

#### 4.4.1 概念说明

前面三节都围绕标量访存。但 CoralNPU 是 NPU，主力算力在 RVV 向量/矩阵后端（u7 单元）。向量 load/store 一条指令要搬一整个向量寄存器（256 位 = 32 字节），甚至 LMUL>1 时搬多个寄存器，还可能是跨步（strided）或下标（indexed）访问。

LSU 复用同一套 slot 机制来服务向量访存，但多了两个与 RVV 后端的握手接口（见文档 [lsu.md:121-144](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/doc/microarch/lsu.md#L121-L144)）：

- **rvv2lsu**（RVV → LSU）：RVV 后端把向量访存所需的**掩码、下标、待写数据**送给 LSU；
- **lsu2rvv**（LSU → RVV）：LSU 把**读回的向量数据**或**store 完成应答**送回 RVV 后端。

这两个接口在 `Lsu` IO 里仅当 `p.enableRvv` 时才存在：

```scala
val rvv2lsu = Option.when(p.enableRvv)(Vec(2, Flipped(Decoupled(new Rvv2Lsu(p)))))
val lsu2rvv = Option.when(p.enableRvv)(Vec(2, Decoupled(new Lsu2Rvv(p))))
```

见 [Lsu.scala:52-54](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/Lsu.scala#L52-L54)。

#### 4.4.2 核心流程

slot 生命周期里专门为向量访存插入了 **Vector Update** 状态。文档（[lsu.md:32-51](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/doc/microarch/lsu.md#L32-L51)）描述的四态如下：

```
1) Idle：从命令队列取一条 LsuOperation。
        标量 → 直接到 Transfer Memory；向量 → 先去 Vector Update。
2) Vector Update：从 RvvCore 取 mask / 下标 / store 数据，填进 slot 表。
        标量操作跳过此状态。
3) Transfer Memory：按行 scatter/gather，发总线事务，逐字节清 active。
        active 全清后 → Writeback。
4) Writeback：把读回结果写回寄存器堆；向量 store 向 RVV 后端发"完成应答"。
        标量 store / 浮点 store 跳过此状态。
        LMUL>1 → 回到 Vector Update 处理下一段；否则 → Idle。
```

向量访存多了两层循环（由 `LsuVectorLoop` 管理）：**segment**（段，对应 RVV 的 nf 字段，一个向量寄存器组里有几个段）和 **lmul**（对应 LMUL，要处理几个向量寄存器）。每次 Writeback 后，若循环未结束，slot 不回 Idle 而是回到 Vector Update 取下一段/下一个寄存器的数据。

> 当前实现里 `rvv2lsu`/`lsu2rvv` 虽声明为 `Vec(2)`，但 `LsuV2` **只使用第 0 路**（第 1 路被固定 tie-off，见 [Lsu.scala:906-907](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/Lsu.scala#L906-L907) 与 [Lsu.scala:1058-1061](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/Lsu.scala#L1058-L1061)），为未来双路并发预留。

#### 4.4.3 源码精读

`Vector Update` 由 `LsuSlot.vectorUpdate` 实现，它从 `rvv2lsu` 取来掩码与数据，按操作类型算出每个字节的地址：

```scala
def vectorUpdate(rvv2lsu: Rvv2Lsu): LsuSlot = {
  ...
  val newActiveBytes = Mux(
      shouldUpdate && LsuOp.isVector(op) && rvv2lsu.mask.valid,
      VecInit(rvv2lsu.mask.bits.asBools),        # 掩码决定哪些字节 active
      VecInit.fill(bytesPerSlot)(false.B))
  val updateAddrs = MuxUpTo1H(addrs, Seq(
    op.isOneOf(LsuOp.VLOAD_UNIT, LsuOp.VSTORE_UNIT)         -> ComputeStridedAddrs(...),
    op.isOneOf(LsuOp.VLOAD_STRIDED, LsuOp.VSTORE_STRIDED)   -> ComputeStridedAddrs(...),
    op.isOneOf(LsuOp.VLOAD_OINDEXED, ...)                   -> ComputeIndexedAddrs(...),
  ))
  result.active := ... (active(i) || newActiveBytes(i))
  result.addrs  := ... (按 newActiveBytes 选 updateAddrs(i) 或保留旧值)
  result.data   := Mux(..., UIntToVec(rvv2lsu.vregfile.bits.data, 8), data)
}
```

见 [Lsu.scala:432-489](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/Lsu.scala#L432-L489)。要点：

- `rvv2lsu.mask` 决定本字节是否需要搬运（向量掩码，按字节有效）；
- unit/strided 用 `ComputeStridedAddrs` 算「基址 + 跨步」地址（[Lsu.scala:277-294](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/Lsu.scala#L277-L294)）；
- indexed 用 `ComputeIndexedAddrs`，把向量寄存器堆送来的**下标数组**加上基址（[Lsu.scala:296-330](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/Lsu.scala#L296-L330)）；
- store 的待写数据从 `rvv2lsu.vregfile.data` 取。

顶层在每拍判断 slot 是否处于「等向量更新」：

```scala
val vectorUpdatedSlot = if (p.enableRvv) {
    io.rvv2lsu.get(0).ready := slot.pendingVector()
    io.rvv2lsu.get(0)... // 见下
    slot.vectorUpdate(io.rvv2lsu.get(0).bits)
} else { slot }
```

见 [Lsu.scala:905-911](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/Lsu.scala#L905-L911)。`pendingVector()` = 子向量未处理完（[Lsu.scala:396-398](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/Lsu.scala#L396-L398)）。

`Writeback` 阶段把读回数据送回 RVV 后端，并用 `last` 比特标记「这是 store 的最后一次应答」：

```scala
io.lsu2rvv.get(0).valid := (slot.shouldWriteback() && LsuOp.isVector(currentOp)) || vectorFault
io.lsu2rvv.get(0).bits.data := Cat(slot.data.reverse)
io.lsu2rvv.get(0).bits.last := (slot.shouldWriteback() || vectorFault) &&
    currentOp.isOneOf(LsuOp.VSTORE_UNIT, LsuOp.VSTORE_STRIDED,
                      LsuOp.VSTORE_OINDEXED, LsuOp.VSTORE_UINDEXED)
```

见 [Lsu.scala:1051-1056](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/Lsu.scala#L1051-L1056)。`last` 让 RVV 后端知道整条向量 store 已完成、可以退休这条指令。

最后，整个 slot 的状态迁移由 `LsuV2` 末尾的 `MuxCase` 一锤定音，正好对应文档的四态：

```scala
val slotNext = MuxCase(slot, Seq(
  (faultReg.valid) -> LsuSlot.inactive(p, 16),                      # fault → 清空
  (slot.slotIdle() && (opQueue.io.nEnqueued > 0.U)) -> nextSlot,    # Idle → 取新指令
  vectorUpdate -> vectorUpdatedSlot,                                 # Vector Update
  slot.activeTransaction() -> transactionUpdatedSlot,                # Transfer Memory
  writebackFired -> writebackUpdatedSlot,                            # Writeback
))
slot := slotNext
```

见 [Lsu.scala:1082-1095](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/Lsu.scala#L1082-L1095)。

#### 4.4.4 代码实践

**实践目标**：跟踪一条向量 unit-stride load 在 LSU 里的完整数据流，画出 RVV 后端 ↔ LSU 的交互时序。

**操作步骤**：

1. 打开 [tests/cocotb/rvv_load_store_test.py](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/tests/cocotb/rvv_load_store_test.py)，挑一条 unit-stride 向量 load 用例。
2. 阅读该用例，确认它在 DTCM 里铺好一段连续数据，然后触发向量 load。
3. 对照源码画时序图：
   - 派发向量 load → slot 入队（active 初始全 0，`pendingWriteback=true`）；
   - **Vector Update**：RVV 后端经 `rvv2lsu` 送来 mask（全 1）+ 无下标，`vectorUpdate` 算出 16 字节连续地址、置 active；
   - **Transfer Memory**：dbus 按 16 字节行读，gather 回填 `data`，清 active；
   - **Writeback**：`shouldWriteback()` 成立，经 `lsu2rvv` 把 `Cat(slot.data.reverse)` 送回 RVV 后端，`last=0`（load 不是 store）；
   - 若 LMUL>1，`vectorLoop` 推进，回到 Vector Update 取下一段。
4. 在源码里确认：unit-stride 的字节地址由 `ComputeStridedAddrs`（`elemStride` = 元素宽度）算出（[Lsu.scala:286-288](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/Lsu.scala#L286-L288)）。

**需要观察的现象**：向量 load 的掩码有效字节会在 `Vector Update` 一次性置位 active，然后 `Transfer Memory` 按行消化；`lsu2rvv` 的 `last` 比特对 load 始终为 0（只有 store 才会拉高 `last`）。

**预期结果**：一张清晰的时序图，标出 `rvv2lsu.mask`、`rvv2lsu.vregfile.data`（store 时）、`dbus` 事务、`lsu2rvv.data`/`lsu2rvv.last` 这几个关键信号的先后顺序。

> 待本地验证：上述时序可对照 `rvv_load_store_test.py` 的预期断言；若需观察内部信号，可在仿真里 dump `slot.vectorLoop` 与 `slot.active`。

#### 4.4.5 小练习与答案

**练习 1**：为什么标量 store 会「跳过 Writeback」，而向量 store 不能跳过？

**答案**：标量 store 不需要把结果写回寄存器堆（它只是写存储器），故 `pendingWriteback=false`，`shouldWriteback()` 永不成立，直接跳过 Writeback。向量 store 虽然也不写标量/浮点寄存器堆，但它必须**向 RVV 后端发完成应答**（`lsu2rvv` 带 `last=1`），让后端能退休这条指令，所以仍要走 Writeback。

**练习 2**：indexed load（下标访存）的地址从哪里来？它与 unit-stride load 在 `vectorUpdate` 里走哪条不同分支？

**答案**：indexed load 的地址 = 基址 + 向量寄存器堆里取来的**下标数组**（经 `rvv2lsu.idx.data`）。在 `vectorUpdate` 里，unit-stride 走 `ComputeStridedAddrs`，indexed 走 `ComputeIndexedAddrs`（[Lsu.scala:473-476](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/Lsu.scala#L473-L476)）。

---

## 5. 综合实践

把本讲四个最小模块串起来：**为一次「跨行、非对齐、且落在 DTCM」的 `sw` 推演 LSU 的完整行为。**

设定：`sw x14, 14(x10)`，`x10 = 0x00010000`，`x14 = 0x44332211`。目标地址 `0x0001000E`，写 4 字节 `0x..0E/0F/10/11`，**跨 16 字节行边界**（行 `0x1000` 与行 `0x1001`）。

请完成：

1. **入队**（4.1）：画出 slot 表，标出 4 个 active 字节的 index、地址、data。提示：4 个字节地址是 `0x1000E/0x1000F/0x10010/0x10011`，data 依次为 `0x11/0x22/0x33/0x44`（小端）。
2. **区段判定**（4.2）：这 4 个地址都落在 DTCM 区 → 走 **dbus**。`itcm/peri/external` 各为多少？
3. **scatter + 跨行**（4.3）：第一拍 dbus 写行 `0x1000`，`wmask` 是什么（哪两个字节被写）？fire 后哪几个 active 位被清？第二拍写行 `0x1001`，`wmask` 又是什么？
4. **完成**（4.1/4.4）：所有 active 清零后，因为是标量 store，`pendingWriteback=false`，slot 跳过 Writeback 直接回 Idle。请确认 `shouldWriteback()` 为假。

**交付物**：一张逐拍的 slot 表演化图（含 Active/Address/Data 三列每拍的变化）、两次 dbus 事务的 `addr/wdata/wmask`、以及对「为什么需要两次事务」的一句话解释。

> 这个练习同时复用了 slot 表（4.1）、三总线选择（4.2）、scatter（4.3）与状态迁移（4.4），完成后你对 LSU 的理解就形成了闭环。

## 6. 本讲小结

- **slot 是一张字节粒度的状态表**：`active/addrs/data` 三列逐字节跟踪一次访存操作的完成情况，总线每搬完一个字节就清一位，全清即完成（[Lsu.scala:372-382](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/Lsu.scala#L372-L382)）。
- **slot 有四个生命周期状态**：Idle →（向量走 Vector Update）→ Transfer Memory → Writeback，由 `LsuV2` 末尾的 `MuxCase` 驱动（[Lsu.scala:1082-1095](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/Lsu.scala#L1082-L1095)）；当前**全核只有一个 slot**，故同一时刻最多一条在途访存操作。
- **三总线按地址区段选择**：ITCM→ibus（仅 load）、DTCM→dbus、外部/外设→ebus（[Lsu.scala:925-960](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/Lsu.scala#L925-L960)）；往 ITCM store 会被判为精确 fault。
- **scatter/gather 是 slot 与宽总线之间的桥**：store 用 `Scatter` 把同行 active 字节装填成 `wdata/wmask`，load 用 `Gather` 从回读行里挑出所需字节；非对齐访问被 `opSize` 放大到整行、再由 slot 逐行消化，天然支持跨行。
- **向量访存复用同一套 slot**：经 `rvv2lsu`（取掩码/下标/store 数据）与 `lsu2rvv`（回读数据/store 完成应答）与 RVV 后端交互，多出来的 `LsuVectorLoop` 负责 segment/LMUL 两层循环（[Lsu.scala:432-489](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/Lsu.scala#L432-L489)、[Lsu.scala:1051-1056](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/Lsu.scala#L1051-L1056)）。
- **fault 仍保证精确异常**：ibus/ebus fault 被锁入 `faultReg`，slot 清空但仍触发 writeback，让 ROB 能按序提交并陷入异常处理。

## 7. 下一步学习建议

- **u6-l2（TCM 与 SRAM）**：dbus/ibus 的对端就是 ITCM/DTCM。下一讲会讲这些紧耦合存储的 Chisel 包装与底层 SRAM 行为模型，帮你补齐「总线事务到了 SRAM 之后发生什么」。
- **u6-l3（L1 Cache）**：当 dbus/ebus miss 掉本地存储时，请求会进入 L1 数据 Cache；结合本讲的 ebus 路径阅读 `L1DCache.scala`，能看清「LSU → ebus → AXI → Cache/外部」的完整数据通路。
- **u7-l1（RVV 后端总览）**：本讲的 `rvv2lsu/lsu2rvv` 接口对端就是 RVV 后端。学完 u7 你就能看到向量 load/store 指令是如何在 `rvv_backend` 里译码、派发、最终经这对接口把数据喂给 LSU 的。
- **源码延伸阅读**：想加深对地址计算的理解，可读 `ComputeStridedAddrs`（[Lsu.scala:277-294](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/Lsu.scala#L277-L294)）与 `ComputeIndexedAddrs`（[Lsu.scala:296-330](https://github.com/google-coral/coralnpu/blob/1406dc5a856fe5edb2193ce35640b6afe4d8be73/hdl/chisel/src/coralnpu/scalar/Lsu.scala#L296-L330)），它们是向量 strided/indexed 访存的核心算术。
