# TCM 紧耦合存储与 SRAM

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 **TCM（Tightly Coupled Memory，紧耦合存储）** 是什么、它与 Cache 的本质区别，以及为什么 CoralNPU 把代码和数据的主存放区选成 TCM 而不是 Cache。
- 看懂 Chisel 侧的 `TCM128` 如何把一块 128 位宽的 SRAM 包装成内核可按字节访问的「子条目（sub-entry）」接口。
- 推导 `Sram_Nx128` 如何按「最大可用块」切分存储、并用读出 Mux 处理多块情形，以及 1 拍读延迟从哪里来。
- 理解 `Sram.v` 这一份可替换的 Verilog 行为模型：它在流片时换_foundry 宏_、在仿真时走 _DPI-C 后门_，并解释这个后门正是仿真器「秒级加载 ELF」的实现机制。

本讲是第 6 单元（存储子系统）的第二篇。上一篇（u6-l1）讲的是 LSU 如何把访存请求分发到 ibus/dbus/ebus；本讲往下走一层，回答「请求最终落到的那块 SRAM 到底长什么样」。

## 2. 前置知识

- **SRAM（静态随机存储）**：一种按地址读写、上电即可用的存储器，访问延迟固定（通常 1 拍）。TCM 在物理上就是一块或多块 SRAM。
- **行为模型（behavioral model）**：用 Verilog 描述的、功能等价但非可综合的存储模型，用于仿真。CoralNPU 的 `Sram.v` 同时提供可综合路径（给流片）和行为路径（给仿真）。
- **BlackBox**：Chisel 里的「黑盒」，表示「这个模块用 Verilog 实现，Chisel 只声明它的端口和参数」。本讲的 `SramBlock` 就是一个 BlackBox。
- **字节使能（byte-enable / wmask）**：宽通道存储器一次读写一整行（这里是 16 字节），但一条 `sw` 只想改其中 4 个字节。`wmask` 是 16 位掩码，第 i 位为 1 表示「本拍允许写入第 i 个字节」。
- **DPI-C（Direct Programming Interface）**：SystemVerilog/Verilog 与 C 互相调用的接口。本讲里它让 Verilog 里的 SRAM 把存储实体托管给一个 C 数组，从而可以绕过总线、直接把 ELF 内容「灌」进存储。
- **小端字节序（little-endian）**：地址最低的字节放在一个字的最低 8 位。RISC-V 与 AXI 都是小端。

如果上一讲的「ibus/dbus/ebus 三类总线」、以及 u2-l1 的「代码进 ITCM、数据进 DTCM」你还有印象，本讲会非常顺。

## 3. 本讲源码地图

| 文件 | 角色 | 关键点 |
|---|---|---|
| [hdl/chisel/src/coralnpu/TCM.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/TCM.scala) | **TCM 的 Chisel 顶层包装** | 把 128 位 SRAM 暴露成 16 个字节子条目；处理字节序 |
| [hdl/chisel/src/coralnpu/SramNx128.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/SramNx128.scala) | **宽通道 SRAM 的分块组织** | 选最大块、多块读出 Mux、`rvalid` 读延迟 |
| [hdl/chisel/src/coralnpu/Sram.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Sram.scala) | **BlackBox 桥接** | `SramBlock` 继承 `SRAM128`，挂上 `Sram.v` 资源 |
| [hdl/chisel/src/coralnpu/SRAM.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/SRAM.scala) | **fabric 侧适配器**（注意大小写，与 BlackBox 无关） | 把总线 `FabricIO` 翻译成字节向量 SRAM 接口 |
| [hdl/chisel/src/coralnpu/Interfaces.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Interfaces.scala) | **SRAM128 BlackBox 声明** | 声明端口与 `NUM_ENTRIES`/`GLOBAL_BASE_ADDR` 参数 |
| [hdl/verilog/Sram.v](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/Sram.v) | **可替换的 Verilog 行为/宏模型** | foundry 宏 / 综合寄存器阵列 / DPI 后门 三选一 |

调用层次（自顶向下，请求流向）：

```
FabricArbiter（三方仲裁：core / AXI-slave / debug）
        │  FabricIO
        ▼
SRAM.scala 的 SRAM 类        ← fabric 适配，1 拍读出寄存
        │  SRAMIO（16 字节向量）
        ▼
TCM.scala 的 TCM128          ← Vec↔UInt 宽度与字节序适配
        │  128 位 UInt
        ▼
SramNx128.scala 的 Sram_Nx128 ← 分块 + 读出 Mux
        │
        ▼
Sram.scala 的 SramBlock      ← BlackBox，模块名 "Sram"，挂 Sram.v
        │
        ▼
Sram.v                       ← 真正的存储实体（宏 / 行为模型）
```

记住这张图，下面每一节都是在拆这张图的某一层。

## 4. 核心概念与源码讲解

### 4.1 TCM 的定位：单周期紧耦合存储与三方仲裁

#### 4.1.1 概念说明

**TCM（紧耦合存储）** 是一块与内核处于「同一时钟域、固定地址、1 拍可访问」的 SRAM。它的关键特征是 **确定性与低延迟**：

- **没有 tag 检查、没有缺失、没有替换**：给地址，下一拍就出数据，每次都一样快。
- **地址固定可见**：ITCM/DTCM 在地址空间里有固定位置，编译器可以直接把变量、代码放进去（见 u2-l1 的链接脚本）。

与 **Cache** 相比，TCM 牺牲了「自动缓存任意主存地址」的便利，换来的是：

| 维度 | TCM | Cache |
|---|---|---|
| 命中延迟 | 1 拍（固定） | 命中 1～2 拍，缺失几十～上百拍 |
| 可预测性 | 完全确定（实时系统友好） | 缺失模式依赖访问历史 |
| 容量管理 | 软件显式摆放 | 硬件自动替换 |
| 面积/功耗 | 仅存储本体 | 还需 tag 阵列、替换逻辑 |

对一个 **run-to-completion、不投机的 ML 加速器**（见 u1-l1），代码和数据总量可控（ITCM 8KB / DTCM 32KB），最怕的不是「装不下」而是「不确定」。所以 CoralNPU 把 **TCM 作为主存放区、Cache 作为访问片外的配角**（Cache 留到 u6-l3）。

CoralNPU 默认内存映射（来自 [Parameters.scala:43-45](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala#L43-L45)）：

| 区段 | 起址 | 大小 | 用途 |
|---|---|---|---|
| ITCM | `0x00000000` | 8 KB（`0x2000`） | 指令 + 只读数据（`.text`/`.rodata`） |
| DTCM | `0x00010000` | 32 KB（`0x8000`） | 可写数据（`.data`/`.bss`/堆/栈） |
| CSR | `0x00030000` | 4 KB | 控制状态寄存器（见 u3-l5） |

默认容量定义在 [Parameters.scala:57-58](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala#L57-L58)（`itcmSizeKBytesDefault = 8`、`dtcmSizeKBytesDefault = 32`），与文档、链接脚本三方一致（u4-l1 已核对）。

#### 4.1.2 核心流程

一块 TCM 并不是「只有内核能访问」。在 CoralNPU 里，ITCM/DTCM 被 **三个主机共享**（见 [CoreAxi.scala:142-144](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxi.scala#L142-L144) 的注释与 `tcmPortCount = 3`）：

1. **内核自身**：取指走 ibus（ITCM），访存走 dbus（DTCM）。
2. **外部 AXI 主机**：经 AXI slave 端口写程序/数据、读结果（见 u2-l4 的 `load_elf`、写输入、读输出）。
3. **调试模块（Debug Module）**：抽象命令读写 GPR/FPR 时也要临时借用 TCM（见 u9-l1）。

三者由一个 `FabricArbiter` 仲裁，仲裁胜者的请求经 `SRAM` 适配器送入 `TCM128`。一次「内核从 DTCM 取数据」的简化时序：

```
T 拍：dbus 给出 addr+valid → FabricArbiter 选中 source(0) → SRAM 适配器送 addr 到 TCM
T+1 拍：SRAM 内部寄存器把上一拍的 addr 对应数据送出 → fabric.readData.valid=1 → dbus 拿到 rdata
```

即 **请求当拍给地址、下一拍出数据**，这就是「单周期 TCM」的含义。

#### 4.1.3 源码精读

ITCM/DTCM 的实例化在 [CoreAxi.scala:147-160](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxi.scala#L147-L160)（ITCM）与 [CoreAxi.scala:189-211](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxi.scala#L189-L211)（DTCM），二者结构对称。以 ITCM 为例：

```scala
// CoreAxi.scala:147-160 —— 构造 ITCM 并把 fabric 适配器接到 TCM128
val itcmSizeBytes: Int = 1024 * p.itcmSizeKBytes          // 8KB
val itcmSubEntryWidth = 8                                  // 子条目 = 1 字节
val itcmWidth = p.axi2DataBits                             // = lsuDataBits
val itcmEntries = itcmSizeBytes / (itcmWidth / 8)
val itcm = Module(new TCM128(itcmSizeBytes, itcmSubEntryWidth, memoryRegions(0).memStart))
val itcmWrapper = Module(new SRAM(p, log2Ceil(itcmEntries)))
itcm.io.addr   := itcmWrapper.io.sram.address
itcm.io.enable := itcmWrapper.io.sram.enable
itcm.io.wdata  := itcmWrapper.io.sram.writeData
itcmWrapper.io.sram.readData := itcm.io.rdata
val itcmArbiter = Module(new FabricArbiter(p, tcmPortCount))   // 三方仲裁
```

注意一个容易踩坑的点：`itcmWidth = p.axi2DataBits`，而 `axi2DataBits = lsuDataBits`（[Parameters.scala:166](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala#L166)）。裸核默认 `lsuDataBits = 256`（[Parameters.scala:140](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala#L140)），**但生产 SoC 配置把 `lsuDataBits` 改成了 128**（[SoCChiselConfig.scala:135](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/SoCChiselConfig.scala#L135)）。这正是 `TCM128` 把宽度写死成 128 的原因——以 SoC 配置为准（这一点 u4-l1 已强调过：读任何模块都要看 SoC 配置，而非 `Parameters` 的裸核默认值）。

`SRAM` 适配器（SRAM.scala，注意大小写，它与 BlackBox 无关）负责把总线的 `FabricIO` 翻译成 16 字节向量接口，并把读出寄存一拍：

```scala
// SRAM.scala:44-48 —— 1 拍读延迟：readIssued 寄存上一拍的读请求
val readIssued = RegInit(false.B)
val issueRead = io.fabric.readDataAddr.valid && !io.fabric.writeDataAddr.valid
readIssued := issueRead
io.fabric.readData.bits := Mux(readIssued, readData, 0.U)
io.fabric.readData.valid := readIssued
```

#### 4.1.4 代码实践

**实践目标**：确认 ITCM/DTCM 的容量、行数，并理解三方仲裁。

**操作步骤**：

1. 打开 [Parameters.scala:57-58](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala#L57-L58)，记下 ITCM/DTCM 的默认 KB 数。
2. 打开 [CoreAxi.scala:142-161](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxi.scala#L142-L161)，找到 `tcmPortCount` 与三个 `source` 各接什么。
3. 在 [CoreAxi.scala:213-224](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxi.scala#L213-L224) 确认 AXI slave 与 Debug Module 分别接到 `itcmArbiter.io.source(1)` 和 `source(2)`。

**需要观察的现象 / 预期结果**：

- ITCM = 8 KB、DTCM = 32 KB；`tcmPortCount = 3`，分别对应 core / AXI-slave / debug。
- ITCM 行数 \( 8192 / 16 = 512 \)，DTCM 行数 \( 32768 / 16 = 2048 \)（每行 16 字节）。
- DTCM 的 `dbus.ready := true.B`（[CoreAxi.scala:211](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxi.scala#L211)）——只要地址落在 DTCM，访存永远「就绪」，这正是 TCM 确定性的体现。

本步为纯源码阅读，结论可直接由代码得出，无需运行。

#### 4.1.5 小练习与答案

**Q1**：为什么 CoralNPU 选 TCM 而不是把 8KB/32KB 全做成 Cache？

**参考答案**：CoralNPU 是 run-to-completion 的 ML 加速器，代码/数据总量可控且已知。TCM 给出固定 1 拍、永不缺失的访问，时序完全可预测，利于实时性与算子调优；Cache 虽能自动缓存任意地址，但引入 tag 检查、缺失惩罚与不可预测性，对这种确定性负载是净负担。

**Q2**：ITCM/DTCM 在本设计里被哪三个主机共享？

**参考答案**：内核自身（ibus 取指 / dbus 访存）、外部 AXI 主机（经 AXI slave 灌程序与数据、读结果）、调试模块（抽象命令临时借用），由 `FabricArbiter(p, tcmPortCount=3)` 仲裁。

---

### 4.2 TCM128：宽通道 SRAM 的 Chisel 包装

#### 4.2.1 概念说明

底层 SRAM 宏是「整行读写」的——这里一行 = 128 位 = 16 字节。但内核的 LSU（u6-l1）需要 **按字节粒度** 操作：一条 `sb` 只写 1 字节、一条 `lw` 只读 4 字节。`TCM128` 就是这二者之间的 **宽度与粒度适配层**：

- 对上：暴露 16 个「子条目（sub-entry）」的向量接口（每个子条目 = 1 字节），并配 16 位字节掩码。
- 对下：把 16 字节压成一拍 128 位的 `UInt`，喂给 `Sram_Nx128`。

`TCM128` 这个名字里的「128」就是写死的行宽，与 SoC 的 `lsuDataBits=128` 对齐。

#### 4.2.2 核心流程

设 `tcmSizeBytes` 为 TCM 总字节数、`tcmSubEntryWidth` 为子条目位宽（CoralNPU 取 8），则三个派生量：

\[
tcmEntries = \frac{tcmSizeBytes}{tcmWidth/8} = \frac{tcmSizeBytes}{16}
\]

\[
tcmSubEntries = \frac{tcmWidth}{tcmSubEntryWidth} = \frac{128}{8} = 16
\]

- `tcmEntries`：存储 **行数**（每行 16 字节）。
- `tcmSubEntries`：一行被切成 **16 个字节子条目**，对应 `wdata/wmask/rdata` 三个 `Vec` 的长度。

写操作：上层给 16 字节 `wdata` + 16 位 `wmask` → `Cat` 成 128 位 `UInt` → 下层 SRAM。读操作反过来：下层 128 位 `rdata` → `UIntToVec` 拆成 16 字节。

字节序的不变量（小端）：**地址最低的字节始终落在 128 位字的 bit[7:0]**。代码用 `Cat(io.wdata.reverse)` 与 `UIntToVec(...).reverse` 两侧对称地反转，保证写进去再读出来字节位置一致。

#### 4.2.3 源码精读

整个类很短，见 [TCM.scala:21-43](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/TCM.scala#L21-L43)：

```scala
// TCM.scala:21-33 —— 接口：16 字节向量 + 16 位掩码
class TCM128(tcmSizeBytes: Int, tcmSubEntryWidth: Int, globalBaseAddr: Int = 0) extends Module {
  val tcmWidth = 128
  val tcmEntries = tcmSizeBytes / (tcmWidth / 8)
  val tcmSubEntries = tcmWidth / tcmSubEntryWidth
  val io = IO(new Bundle {
    val addr = Input(UInt(log2Ceil(tcmEntries).W))      // 行地址（字寻址）
    val enable = Input(Bool())
    val write = Input(Bool())
    val wdata = Input(Vec(tcmSubEntries, UInt(tcmSubEntryWidth.W)))
    val wmask = Input(Vec(tcmSubEntries, Bool()))
    val rdata = Output(Vec(tcmSubEntries, UInt(tcmSubEntryWidth.W)))
  })
```

注意 `addr` 是 **行地址**：因为它宽度是 `log2Ceil(tcmEntries)`，已经把字节偏移（低 4 位）省掉了——字节选择由 `wmask` 完成，不由地址完成。

```scala
// TCM.scala:36-42 —— Vec↔UInt 适配，注意两处 .reverse 保证小端
val sram = Module(new Sram_Nx128(tcmEntries, globalBaseAddr))
sram.io.addr   := io.addr
sram.io.enable := io.enable
sram.io.write  := Cat(io.write)
sram.io.wdata  := Cat(io.wdata.reverse)         // 字节0 → bit[7:0]
sram.io.wmask  := Cat(io.wmask.reverse)
io.rdata       := UIntToVec(sram.io.rdata, tcmSubEntryWidth).reverse
```

`globalBaseAddr` 透传给下层，最终落到 `Sram.v` 的 `GLOBAL_BASE_ADDR` 参数——它是 4.4 节 DPI 后门寻址的关键（见后）。

#### 4.2.4 代码实践

**实践目标**：验证 ITCM/DTCM 各自的 `tcmEntries`、`addr` 位宽，并确认字节序不变量。

**操作步骤**：

1. 在 [TCM.scala:22-24](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/TCM.scala#L22-L24) 用默认容量手算：
   - ITCM：`tcmEntries = 8192/16 = 512`，`addr` 位宽 = `log2Ceil(512) = 9`。
   - DTCM：`tcmEntries = 32768/16 = 2048`，`addr` 位宽 = `log2Ceil(2048) = 11`。
2. 对照 [TCM.scala:27](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/TCM.scala#L27) 的注释 `// 9 for 512 rows to address`，印证 ITCM 的 9 位行地址。

**预期结果**：手算结果与代码注释一致；ITCM 用 9 位行地址、DTCM 用 11 位。

**字节序自验**：跟踪一个写到地址 `0x10000`（DTCM 首字节）的 1 字节数据。它在 `wdata` 向量里是 index 0，经 `Cat(wdata.reverse)` 落到 128 位字的 bit[7:0]；读回时经 `UIntToVec(...).reverse`，bit[7:0] 又回到 index 15——但经上层 `SRAM.scala` 的 `Cat(io.sram.readData)` 再次组合后，该字节仍出现在 fabric 数据的 bit[7:0]，即最低地址位。结论：**多层 `.reverse`/`Cat` 对称使用，最终保持小端一致**。

本步为源码阅读型实践，无需运行。

#### 4.2.5 小练习与答案

**Q1**：`TCM128` 的 `addr` 端口为什么不是字节地址、而是行地址？

**参考答案**：底层 SRAM 按 16 字节行寻址，字节粒度的选择通过 16 位 `wmask` 表达，不进地址。因此 `addr` 只需 `log2Ceil(tcmEntries)` 位（行号），字节偏移低 4 位被 `wmask` 取代，节省了地址位宽并匹配 SRAM 宏的接口。

**Q2**：如果把 `tcmSubEntryWidth` 从 8 改成 16（每子条目 2 字节），`tcmSubEntries` 变成多少？接口会怎样变？

**参考答案**：`tcmSubEntries = 128/16 = 8`。`wdata/wmask/rdata` 三个 `Vec` 长度从 16 变成 8，每个元素从 8 位变成 16 位；`wmask` 变成「半字使能」。注意 CoralNPU 实际取 8（字节使能），这是为了支持 `sb`/`sh` 等任意字节粒度访存。

---

### 4.3 Sram_Nx128：分块组织与读出 Mux

#### 4.3.1 概念说明

foundry（晶圆厂）提供的 SRAM 宏只有 **几种固定规格**，比如「2048 行 × 128 位」「512 行 × 128 位」。你没法定制一个「正好 2048 行」以外的任意规格。于是 `Sram_Nx128` 的任务是：

> 给定任意行数 `tcmEntries`，用「尽可能大的标准块」拼出来，多块时用 Mux 选出读数据。

它的名字 `Sram_Nx128` 意思就是「N 行 × 128 位」的 SRAM，生成的模块名是 `SRAM_<tcmEntries>x128`（见 [SramNx128.scala:21](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/SramNx128.scala#L21)）。

#### 4.3.2 核心流程

**选块策略**（[SramNx128.scala:35-38](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/SramNx128.scala#L35-L38)）：优先用能整除 `tcmEntries` 的最大标准块——2048，其次 512，最后 128。

\[
blockSize = \begin{cases} 2048 & tcmEntries \bmod 2048 = 0 \\ 512 & tcmEntries \bmod 512 = 0 \\ 128 & \text{otherwise} \end{cases}
\]

然后：

\[
nSramModules = \frac{tcmEntries}{blockSize}, \quad sramSelectBits = \log_2(nSramModules)
\]

对默认配置：

| TCM | tcmEntries | blockSize | nSramModules | 说明 |
|---|---|---|---|---|
| ITCM 8KB | 512 | 512 | 1 | 单块，无需 Mux |
| DTCM 32KB | 2048 | 2048 | 1 | 单块，无需 Mux |

所以默认配置下两块 TCM 都只实例化 1 个 `SramBlock`，走「单块直连」快路径。多块 Mux 路径是为 **highmem 配置**（ITCM/DTCM 各 1MB，见 [Parameters.scala:59-60](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala#L59-L60)）准备的：1MB → 65536 行 → blockSize=2048 → 32 块。

**读出 Mux** 的难点：地址当拍给、数据下一拍才出。所以「这一拍该读哪一块」的判断要寄存一拍，再用 `MuxLookup` 选数据。

**`rvalid`**：`io.rvalid := RegNext(io.enable)`，把使能寄存一拍，告诉上层「本拍 rdata 有效」。

#### 4.3.3 源码精读

IO 声明见 [SramNx128.scala:20-31](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/SramNx128.scala#L20-L31)，全部是 128 位的扁平 `UInt`（不再是字节向量——粒度适配已在 4.2 节的 `TCM128` 做完）。

选块与建块：

```scala
// SramNx128.scala:35-48 —— 选最大可整除块，每块带自己的全局基址
val blockSize =
  if (tcmEntries % 2048 == 0) 2048
  else if (tcmEntries % 512 == 0) 512
  else 128
val nSramModules = tcmEntries / blockSize
val sramModules = (0 until nSramModules).map(x => {
  val subModuleAddr = globalBaseAddr + x * blockSize * 16   // 16 = 每行字节数
  Module(new SramBlock(blockSize, subModuleAddr))
})
```

注意 `subModuleAddr = globalBaseAddr + x * blockSize * 16`：每个块拿到自己在 **全局地址空间** 里的起始地址（乘 16 把「行」换算成「字节」）。这个全局基址最终传给 `Sram.v`，是 4.4 节 DPI 后门寻址的依据。

单块快路径 vs 多块 Mux 路径：

```scala
// SramNx128.scala:51-73 —— 单块直连；多块按高位选块、读出寄存一拍再 Mux
if (nSramModules == 1) {
  sramModules(0).io.addr := io.addr
  // ... enable/write/wdata/wmask 直连
  io.rdata := sramModules(0).io.rdata
} else {
  val selectedSram = io.addr(addrBits - 1, sramAddrBits)            // 当拍：选哪块
  for (i <- 0 until nSramModules) {
    sramModules(i).io.enable := (selectedSram === i.U) && io.enable // 只使能被选中的块
    sramModules(i).io.addr  := io.addr(sramAddrBits - 1, 0)
  }
  val selectedSramRead = RegNext(selectedSram, 0.U(sramSelectBits.W)) // 寄存：上拍选的哪块
  io.rdata := MuxLookup(selectedSramRead, 0.U(128.W))(
    (0 until nSramModules).map(i => i.U -> sramModules(i).io.rdata))
}
io.rvalid := RegNext(io.enable)
```

`SramBlock` 本身极薄——它只是把 `SRAM128` BlackBox 套上一层并挂上 `Sram.v` 资源，见 [Sram.scala:19-22](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Sram.scala#L19-L22)：

```scala
class SramBlock(numEntries: Int, globalBaseAddr: Int = 0)
    extends SRAM128(numEntries, globalBaseAddr) with HasBlackBoxResource {
  override val desiredName = "Sram"   // 生成的 Verilog 实例名统一叫 Sram
  addResource("Sram.v")               // 把 hdl/verilog/Sram.v 作为资源绑定
}
```

而 `SRAM128` 这个 BlackBox 声明端口与两个参数（[Interfaces.scala:89-104](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Interfaces.scala#L89-L104)）：

```scala
abstract class SRAM128(numEntries: Int, globalBaseAddr: Int = 0) extends BlackBox(Map(
  "NUM_ENTRIES" -> IntParam(numEntries),
  "GLOBAL_BASE_ADDR" -> IntParam(globalBaseAddr))) {
  val io = IO(new Bundle { /* clock/enable/write/addr/wdata/wmask(16)/rdata(128)/rvalid */ })
}
```

这两个参数会原样传给 `Sram.v` 的 `parameter NUM_ENTRIES` 与 `GLOBAL_BASE_ADDR`。

#### 4.3.4 代码实践

**实践目标**：推演默认配置与 highmem 配置下的分块结果，并定位多块 Mux 的寄存器。

**操作步骤**：

1. 默认配置：依 [SramNx128.scala:35-38](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/SramNx128.scala#L35-L38) 算 ITCM（512 行）与 DTCM（2048 行）的 `blockSize`、`nSramModules`，确认都走单块路径。
2. highmem 配置：把 ITCM/DTCM 都当成 1MB（65536 行），算 `blockSize=2048`、`nSramModules=32`、`sramSelectBits=5`，确认走多块 Mux 路径。
3. 在 [SramNx128.scala:69-72](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/SramNx128.scala#L69-L72) 找到 `selectedSramRead = RegNext(selectedSram)`，解释它为何必须寄存一拍。

**预期结果**：

| 配置 | tcmEntries | blockSize | nSramModules | 路径 |
|---|---|---|---|---|
| ITCM 8KB | 512 | 512 | 1 | 单块直连 |
| DTCM 32KB | 2048 | 2048 | 1 | 单块直连 |
| highmem 1MB | 65536 | 2048 | 32 | 多块 Mux |

`selectedSramRead` 必须寄存一拍：因为 SRAM 读是「当拍给地址、下拍出数据」，读出时刻块号已不是当拍的 `selectedSram`，而是上一拍的，所以要用 `RegNext` 还原「是哪一块产出了当前 rdata」。

本步为源码阅读型实践。

#### 4.3.5 小练习与答案

**Q1**：默认 ITCM/DTCM 配置下，会实例化几个 `SramBlock`？走哪条代码路径？

**参考答案**：各 1 个。ITCM 512 行、DTCM 2048 行，`nSramModules` 都为 1，走 [SramNx128.scala:51-58](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/SramNx128.scala#L51-L58) 的单块直连路径，无 Mux、无 `selectedSram` 逻辑。

**Q2**：`subModuleAddr` 计算里的「`* blockSize * 16`」为什么乘 16？

**参考答案**：`blockSize` 是「行数」，而全局地址以「字节」为单位。每行 128 位 = 16 字节，所以乘 16 把行数换算成字节数，得到该块在全局地址空间里的字节起始地址，供 `Sram.v` 的 DPI 后门寻址使用。

---

### 4.4 Sram.v：可替换的 Verilog 行为模型与 DPI 后门

#### 4.4.1 概念说明

`Sram.v` 是整条调用链最底层、真正「存东西」的模块。它最巧妙的地方是 **同一份代码、三条分支**，用宏切换：

1. **foundry 宏**（`USE_TSMC12FFC` / `USE_GF22`）：流片时换成晶圆厂的真实 SRAM 编译器产出（如 `TS1N12FFCLLMBLVTD2048X128M4SWBSHO`）。
2. **综合寄存器阵列**（`SYNTHESIS`）：用 `reg [127:0] mem[...]` 描述的可综合存储，用于不带 foundry 库的 ASIC 综合。
3. **仿真行为模型**（默认，含 DPI 后门）：仿真用，并且经 DPI-C 把存储托管给 C 侧，从而支持 **backdoor（后门）加载**。

第 3 条正是仿真器「秒级加载 ELF」的秘密。回顾 u2-l4：`load_elf` 默认走 backdoor，不走真实 AXI 总线——它能这么做，就是因为 `Sram.v` 的 DPI 路径把每块 SRAM 按全局地址注册到一个 C 侧存储实体，主机可以直接按地址写字节。

#### 4.4.2 核心流程

模块端口（[Sram.v:15-27](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/Sram.v#L15-L27)）：

```verilog
module Sram #(
    parameter NUM_ENTRIES = 128,
    parameter GLOBAL_BASE_ADDR = 0
) (
    input                            clock,
    input                            enable,
    input                            write,
    input  [$clog2(NUM_ENTRIES)-1:0] addr,
    input  [                  127:0] wdata,
    input  [                   15:0] wmask,   // 16 位字节使能
    output [                  127:0] rdata,
    output                           rvalid
);
```

分支选择骨架：

```
`ifdef USE_TSMC12FFC      → TSMC 12nm SRAM 宏（2048 或 512 行）
`elsif USE_GF22           → GF 22nm SRAM 宏（2048 或 512 行）
`else                     → 通用 SRAM
    `ifdef SYNTHESIS      → 可综合 reg 阵列（综合用）
    `else
      `ifdef DPI_MEMORY   → DPI-C 后门（Verilator / VCS 仿真）
      `else               → 纯 reg 阵列（其它仿真器兜底）
```

DPI 路径用 4 个 C 函数管理存储（[Sram.v:215-231](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/Sram.v#L215-L231)）：

- `sram_init(global_addr, size_bytes, width_bytes)`：把这块 SRAM 注册到 C 侧，返回句柄。
- `sram_write(handle, addr, data, wmask)`：按字节使能写。
- `sram_read(handle, addr, data)`：读一行。
- `sram_cleanup(handle)`：仿真结束回收。

读写都在 `always @(posedge clock)` 里完成，因此读数据 **寄存一拍** 出现（1 拍读延迟的来源之一）。`rvalid` 也是 `enable` 寄存一拍（[Sram.v:183-185](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/Sram.v#L183-L185)）。

#### 4.4.3 源码精读

DPI 后门路径（[Sram.v:214-254](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/Sram.v#L214-L254)）：

```verilog
import "DPI-C" function chandle sram_init(input longint global_addr,
                                          input longint size_bytes, input int width_bytes);
import "DPI-C" function void sram_write(input chandle handle, input int addr,
                                        input bit [127:0] data, input int wmask);
import "DPI-C" function void sram_read (input chandle handle, input int addr,
                                        output bit [127:0] data);

chandle backdoor_handle;
reg [127:0] rdata_reg;
assign rdata = rdata_reg;
initial begin
  backdoor_handle = sram_init(GLOBAL_BASE_ADDR, NUM_ENTRIES * SRAM_WIDTH_BYTES, SRAM_WIDTH_BYTES);
end
always @(posedge clock) begin
  if (enable & write)  sram_write(backdoor_handle, 32'(addr), wdata, {16'b0, wmask});
  if (enable & ~write) sram_read (backdoor_handle, 32'(addr), rdata_reg);
end
```

`GLOBAL_BASE_ADDR` 在 `initial` 里随 `sram_init` 注册——这就是把 4.1～4.3 节一路透传下来的 `globalBaseAddr` 用起来的地方。于是主机（Verilator 的 C++ 侧、或 cocotb 经 DPI）可以凭 **全局地址** 直接定位到某块 SRAM，把 ELF 的 `.text`/`.data` 字节灌进去，完全不需要驱动 AXI 总线。这就是 backdoor `load_elf` 快但「不贴近硬件」的原因（frontdoor 则真实走 AXI，见 u2-l4）。

foundry 宏路径以 TSMC12 为例（[Sram.v:50-76](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/Sram.v#L50-L76)）：把 16 位 `wmask` 翻成宏需要的「反相位宽使能 `nwmask`」（foundry 宏常用低有效），再按 `NUM_ENTRIES` 选 2048 或 512 行的宏实例。注意它对 `NUM_ENTRIES` 只支持 2048/512 两种规格，其它尺寸会 `$error`（[Sram.v:104-108](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/Sram.v#L104-L108)）——这正是 4.3 节「选块策略优先 2048/512」的现实约束来源。

综合路径（[Sram.v:187-206](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/Sram.v#L187-L206)）用 `bit [127:0] mem[0:NUM_ENTRIES-1]`，按 `wmask[i]` 逐字节写入，读出经 `raddr` 寄存一拍。

#### 4.4.4 代码实践

**实践目标**：分清 `Sram.v` 的三条分支何时启用，并解释 DPI 后门如何支撑 `load_elf`。

**操作步骤**：

1. 在 [Sram.v:33-113](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/Sram.v#L33-L113) 找到 `USE_TSMC12FFC` 分支，记录它支持的 `NUM_ENTRIES` 取值。
2. 在 [Sram.v:214-254](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/verilog/Sram.v#L214-L254) 找到 4 个 DPI 函数与 `sram_init(GLOBAL_BASE_ADDR, ...)` 调用。
3. 回顾 u2-l4：`load_elf` 默认 backdoor、`COCOTB_USE_FRONTDOOR=1` 切 frontdoor。把这条结论与本节的 `GLOBAL_BASE_ADDR` 注册机制对应起来。

**预期结果**：

- foundry 宏仅支持 2048/512 行（故 `Sram_Nx128` 的选块策略优先 2048、512）。
- 仿真时（Verilator/VCS）走 DPI 路径，每块 SRAM 在 `initial` 用 `GLOBAL_BASE_ADDR` 注册到 C 侧；主机按全局地址直接读写 → backdoor `load_elf` 无需 AXI 流量。
- frontdoor 模式则不依赖 DPI 后门，真实驱动 AXI slave 把字节逐拍写进 TCM，更慢但验证了总线通路。

**可选运行（待本地验证）**：若本地已配好 Verilator 工具链，可构建 `core_mini_axi_sim` 并运行 `hello_world_add_floats`：

```bash
bazel build //tests/verilator_sim:core_mini_axi_sim      # 待本地验证
```

观察日志中 ELF 是否经 backdoor 加载到 ITCM/DTCM（通常有类似 `Loading ELF ... backdoor` 的提示），并与 `COCOTB_USE_FRONTDOAR=1` 时的耗时对比。

#### 4.4.5 小练习与答案

**Q1**：`Sram.v` 用什么机制让仿真器能「不走总线」直接加载 ELF？靠哪个参数定位每块 SRAM？

**参考答案**：靠 DPI-C 后门。每块 SRAM 在 `initial` 里调用 `sram_init(GLOBAL_BASE_ADDR, size, width)` 把自己注册到 C 侧存储实体；主机凭 `GLOBAL_BASE_ADDR`（即从 `TCM128` 一路透传下来的 `globalBaseAddr`）按全局地址直接读写字节，从而实现 backdoor 加载，无需驱动 AXI 总线。

**Q2**：为什么 `Sram_Nx128` 的选块策略要优先 2048、再 512、再 128？

**参考答案**：因为 foundry 宏（TSMC12/GF22）只提供 2048 行和 512 行两种规格（其它尺寸会 `$error`）。优先选最大可整除块，既匹配宏规格、又用最少块数拼出所需容量，减少多块 Mux 的面积与延迟。

---

## 5. 综合实践

把本讲四节串起来，跟踪 **一条 `sw x5, 0(x6)`（store word，4 字节写）落到 DTCM 地址 `0x00010004`** 的全过程，并回答：

1. **fabric 适配**：`SRAM.scala` 的 `writeDataAddr` 取地址的哪几位作为 SRAM 行地址？（提示：`lsb = log2Ceil(axi2DataBits/8) = log2Ceil(16) = 4`，见 [SRAM.scala:37-41](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/SRAM.scala#L37-L41)）
2. **行号**：地址 `0x00010004` 对应 DTCM 内的哪一行？（去掉全局基址 `0x10000` 与低 4 位字节偏移）
3. **字节使能**：`0x...04` 的低 4 位是 `0100`，本拍 4 字节落在 16 字节行的哪几个字节？对应 `wmask` 的哪几位置 1？（写成 16 位二进制）
4. **分块**：该行落在 DTCM 的第几个 `SramBlock`？走单块还是多块路径？
5. **存储实体**：在 Verilator 仿真下，这次写入最终调用 `Sram.v` 的哪个 DPI 函数？`GLOBAL_BASE_ADDR` 是多少？

**参考答案**：

1. 行地址 = `addr(sramAddressWidth+lsb-1, lsb)`，即去掉低 `lsb=4` 位字节偏移后的位。
2. `0x00010004 - 0x10000 = 0x4`；低 4 位是字节偏移，行号 = `0x4 >> 4 = 0`。即 DTCM 第 0 行（第 0 个 16 字节行）。
3. 字节偏移 `0x4` 表示从第 4 字节起、连续 4 字节（字节 4/5/6/7）。`wmask` 16 位中第 4、5、6、7 位置 1，其余 0：`wmask = 16'b0000_0000_1111_0000`。
4. DTCM 默认 2048 行、`blockSize=2048`、`nSramModules=1`，第 0 行落在唯一的 `SramBlock`，走单块直连路径。
5. 调用 `sram_write(backdoor_handle, /*addr=*/0, wdata, wmask)`；DTCM 的 `GLOBAL_BASE_ADDR = memoryRegions(1).memStart = 0x00010000`（见 [CoreAxi.scala:193](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/CoreAxi.scala#L193) 传入的 `memoryRegions(1).memStart`）。

完成本题后，你就把「LSU 请求 → fabric 适配 → TCM128 字节序 → Sram_Nx128 分块 → Sram.v 存储」整条链路打通了。

## 6. 本讲小结

- **TCM 是确定性的单周期 SRAM**：ITCM 8KB@`0x0`、DTCM 32KB@`0x10000`，由 `FabricArbiter` 在 core / AXI-slave / debug 三方间仲裁；DTCM 命中时 `dbus.ready` 恒真，永不阻塞。
- **生产 SoC 数据宽度是 128 位**（`lsuDataBits=128`），与 `TCM128` 写死的 128 位行宽对齐——读模块要看 SoC 配置，而非 `Parameters` 的 256 位裸核默认值。
- **`TCM128` 是粒度适配层**：把 128 位 SRAM 行拆成 16 个字节子条目，用 16 位 `wmask` 表达字节使能；两侧对称的 `.reverse`/`Cat` 保证小端字节序（最低地址字节落在 bit[7:0]）。
- **`Sram_Nx128` 是分块组织层**：按「最大可整除块（2048/512/128）」拼存储，默认配置单块直连、highmem 配置多块经寄存一拍的 `MuxLookup` 选读；`rvalid=RegNext(enable)` 给出 1 拍读延迟。
- **`SramBlock`→`SRAM128` BlackBox→`Sram.v`** 三层把 Chisel 接到 Verilog；`NUM_ENTRIES`/`GLOBAL_BASE_ADDR` 两个参数贯穿到底。
- **`Sram.v` 一份代码三条分支**：foundry 宏（流片）/ 综合寄存器阵列 / 仿真行为模型；仿真路径的 DPI-C 后门用 `GLOBAL_BASE_ADDR` 注册每块 SRAM，正是 backdoor `load_elf` 的实现基础。

## 7. 下一步学习建议

- **本讲只讲了「TCM 命中」的快路径**。当地址 **不在** ITCM/DTCM 时（取指 miss、数据在片外 DDR），请求会经 `IBus2Axi`/`DBus2Axi` 走到 `ebus`→AXI master（见 u3-l2），那时才轮到 **Cache** 登场。下一讲 **u6-l3（L1 指令/数据 Cache）** 会讲 L1I（8KB/4 路）与 L1D（16KB 双 bank）如何缓存片外访问、以及 `fence.i` 刷写与内核 stall 的契约。
- 想看 TCM 与总线的更上层交互，可回顾 **u3-l4（Crossbar 与 Socket）**——TCM 是 fabric 上的一个 device，本讲的 `FabricArbiter`/`SRAM` 适配器就挂在其下。
- 想深入「backdoor 加载 ELF」的软件侧，可回到 **u2-l4（cocotb 测试框架）**，对照 `load_elf` 的 frontdoor/backdoor 开关，体会本讲 DPI 后门的实际用法。
