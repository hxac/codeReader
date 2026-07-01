# 参数系统与内存区划

> 本讲属于第 4 单元「标量核前端与流水线」的第一讲，前置是 [u3-l1 SoC 顶层子系统装配](u3-l1-soc-subsystem.md)。在动手读流水线任何一节之前，我们必须先看懂 CoralNPU 是怎样用一份数据结构集中描述「内核有多宽、有几块内存、开了哪些功能」的——这就是 `Parameters` 与 `MemorySize`/`MemoryRegion` 的职责。

## 1. 本讲目标

学完本讲，你应当能够：

1. 解释 `MemorySize` 如何以「字节」为唯一真相单位描述一块存储的容量，并能换算 KB/MB。
2. 解释 `MemoryRegion`/`MemoryRegions` 如何用「起始地址 + 大小 + 类型」三元组刻画一段地址区，并能用 `contains(addr)` 判断地址归属。
3. 读懂 `Parameters` 这份「内核全局配置清单」，分清哪些是固定常量（宽通道、寄存器数量、Cache/AXI 位宽）、哪些是可裁剪的功能开关（`enable*`）。
4. 说出 SoC 顶层 `SoCChiselConfig` 是如何把这些参数注入给 `Core` 的，以及裸核默认值（256 位宽）与生产 SoC 配置（128 位宽）的差别。

## 2. 前置知识

- **Chisel 基础**：能看懂 `class`、`object`、`val/var`、`UInt`、`ChiselEnum` 的 Scala/Chisel 写法。
- **寄存器传输层（RTL）直觉**：所谓「宽通道」就是一根数据总线的位宽，例如 256 位 = 32 字节，一个周期可以搬 8 条 32 位指令。
- **TCM（紧耦合存储）**：CoralNPU 把代码放 ITCM、数据放 DTCM，二者都是单周期可访问的片上 SRAM。详见 [u2-l1 工具链与 TCM 链接脚本](u2-l1-toolchain-linker-tcm.md)。
- **地址区（address region）**：一段连续地址，用 `[起始, 起始+大小)` 这种半开区间来表示，这是本讲反复出现的数学表达方式。

如果上面这些名词你还觉得陌生，建议先回到 u2-l1、u3-l1 复习一遍再继续。

## 3. 本讲源码地图

本讲涉及三个文件，职责分工如下：

| 文件 | 行数规模 | 作用 |
| --- | --- | --- |
| [MemorySize.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/MemorySize.scala) | 极小（约 12 行） | 定义「一块存储有多大」的容量单位，只关心字节数。 |
| [Parameters.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala) | 中等（约 216 行） | 内核全局配置的「单一真相源」：内存区划、宽通道、寄存器堆、Cache/AXI 接口、`enable*` 开关。 |
| [SoCChiselConfig.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/SoCChiselConfig.scala) | 较大（约 256 行） | SoC 顶层装配器，负责选择内存区划并把参数注入给 `Core` 等模块。 |

一句话定位：`MemorySize` 描述「有多大」，`MemoryRegion` 描述「在哪、什么类型」，`Parameters` 把这两者连同宽通道、开关打包成内核配置，`SoCChiselConfig` 在装配时把这些配置喂给 `Core`。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

- **4.1 MemorySize 与内存区划**（覆盖 `MemorySize.scala` + `Parameters.scala` 中的 `MemoryRegion`/`MemoryRegions`）
- **4.2 Parameters 全局配置**（覆盖 `Parameters.scala` 的宽通道、寄存器堆、Cache/AXI 接口）
- **4.3 enable* 功能开关**（覆盖 `Parameters.scala` 的可裁剪开关）
- **4.4 参数注入：从 SoCChiselConfig 到 Core**（覆盖 `SoCChiselConfig.scala`）

---

### 4.1 MemorySize 与内存区划

#### 4.1.1 概念说明

处理器要访问存储，首先要回答两个问题：**这块存储有多大？** 和 **这块存储在地址空间里处于什么位置？**

CoralNPU 把这两个问题拆成两层抽象：

- `MemorySize` 只回答「有多大」——内部只存一个字节数，附带 KB/MB 换算。这样设计的好处是：所有容量统一以字节为基准，避免「这个文件用 KB、那个文件用 MB」造成的单位错配。
- `MemoryRegion` 在 `MemorySize` 之上加两个维度——「起始地址」与「类型（指令/数据/外设/外部）」，从而刻画一段完整的地址区。

这种分层是典型的「单一职责」：`MemorySize` 可被任意需要容量的模块复用（比如 SRAM 大小、DMA 传输长度），而 `MemoryRegion` 专门服务于地址译码。

#### 4.1.2 核心流程

地址归属判定的核心是「半开区间」检查。给定一个地址 `addr` 与一段区 `[start, start+size)`，判断它是否落在区内：

\[
\text{contains}(\text{addr}) \;=\; (\text{addr} \ge \text{start}) \;\land\; (\text{addr} < \text{start} + \text{size})
\]

注意这是**左闭右开**区间：上界用 `<` 而非 `<=`。这样两段相邻的区（例如 ITCM `[0x0, 0x2000)` 与 DTCM `[0x10000, 0x18000)`）不会在边界地址上产生歧义，一个地址要么属于唯一一段，要么不属于任何一段。

CoralNPU 默认的内存区划如下（与 `integration_guide.md` 的内存映射表一致）：

| 区名 | 类型 | 起始地址 | 大小 | 区间（半开） | 用途 |
| --- | --- | --- | --- | --- | --- |
| ITCM | IMEM | `0x00000000` | 8 KB = `0x2000` | `[0x0000, 0x2000)` | 代码存储 |
| DTCM | DMEM | `0x00010000` | 32 KB = `0x8000` | `[0x10000, 0x18000)` | 数据存储 |
| CSR | Peripheral | `0x00300000` | 4 KB = `0x1000` | `[0x30000, 0x31000)` | 控制/状态寄存器 |

> 注：这里的 0x30000 与你在 [u3-l5 CSR 接口](u3-l5-csr-boot-control.md) 中看到的 RESET_CONTROL/PC_START/STATUS 寄存器基址完全对应。

#### 4.1.3 源码精读

先看容量单位 `MemorySize`，它是一个不可变 `case class`，只持有字节数：

[MemorySize.scala:1-12](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/MemorySize.scala#L1-L12) — 定义 `case class MemorySize(bytes: Int)`，并提供 `fromBytes`/`fromKBytes`/`fromMBytes` 三个工厂方法做单位换算；`kBytes` 把字节折回 KB。整个类不关心地址，只关心「大小」。

```scala
case class MemorySize(bytes: Int) {
  def kBytes: Int = bytes / 1024
}
object MemorySize {
  def fromKBytes(kBytes: Int): MemorySize = MemorySize(kBytes * 1024)
}
```

地址区定义在 `Parameters.scala` 顶部。先用枚举把「类型」枚举出来：

[Parameters.scala:21-26](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala#L21-L26) — `MemoryRegionType` 是一个 `ChiselEnum`，列出四种类型：`IMEM`（指令）、`DMEM`（数据）、`Peripheral`（外设/CSR）、`External`（片外）。后续的地址译码会按类型决定走哪条总线。

然后是 `MemoryRegion` 本体，它的 `contains` 方法把上面的半开区间公式直接翻译成硬件比较：

[Parameters.scala:28-39](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala#L28-L39) — 三元组 `(memStart, memSize, memType)`；`contains(addr)` 返回一个 `Bool`，即 `addr >= memStart && addr < memStart + memSize`。注意它把字面量提升成与 `addr` 等宽的 `UInt`（`addrWidth = addr.getWidth.W`），这是为了综合时不出现位宽不匹配。

```scala
def contains(addr: UInt): Bool = {
  val addrWidth = addr.getWidth.W
  (addr >= memStart.U(addrWidth)) && (addr < memStart.U(addrWidth) + memSize.U(addrWidth))
}
```

默认区划集中在 `MemoryRegions` 单例对象里：

[Parameters.scala:41-54](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala#L41-L54) — 提供两套布局：`default` 是当前 IP 的默认内存映射（ITCM 8KB / DTCM 32KB / CSR 4KB），`highmem` 则把 DTCM 与 CSR 的基址整体抬高（DTCM→`0x100000`、CSR→`0x200000`），注释明确说明这是为了「让每段大小可变到 1MB 而不互相覆盖」。`highmem` 的大小由参数 `itcmSizeKBytes`/`dtcmSizeKBytes` 决定，最大可达 1024KB。

#### 4.1.4 代码实践

**目标**：验证 RTL 的默认内存区划与文档、链接脚本三方一致。

**步骤**：

1. 打开 [Parameters.scala:42-46](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala#L42-L46)，读出 ITCM/DTCM 的起始地址与大小。
2. 打开 [integration_guide.md:160-164](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/integration_guide.md#L160-L164)，对照文档表格的 Range 列。
3. 回顾 [u2-l1](u2-l1-toolchain-linker-tcm.md) 讲过的链接脚本 `coralnpu_tcm.ld.tpl`：`.text` 落 ITCM、`.data` 落 DTCM（起址 `0x10000`）。

**需要观察的现象**：

- ITCM 起址 `0x0`、大小 `0x2000`（8KB）；DTCM 起址 `0x10000`、大小 `0x8000`（32KB）——RTL、文档、链接脚本三处数字应当完全吻合。
- 用半开区间公式手算 DTCM 上界：`0x10000 + 0x8000 = 0x18000`，即文档里写的 `0x17FFF` 是上界减一。

**预期结果**：三方一致，证明 `MemoryRegions.default` 就是整个 IP 对外承诺的内存映射。如果你在 `contains` 里把 `<` 改成 `<=`，理论上会让 `0x18000` 这个边界地址同时被判定为「属于 DTCM」和「不属于 DTCM 之外」，从而破坏区间的无歧义性——这只是思维实验，**请勿改动源码**。

#### 4.1.5 小练习与答案

**练习 1**：`MemoryRegions.highmem` 为什么要把 DTCM 基址从 `0x10000` 抬到 `0x100000`？

**参考答案**：默认布局里 DTCM 紧跟在 ITCM 之后不远，若把 DTCM 放大到 1MB，会与 CSR 区重叠。`highmem` 给每个区预留 1MB 的地址窗口（ITCM 区在 `0x000000`、DTCM 区在 `0x100000`、CSR 在 `0x200000`），这样各区可独立放大到 1MB 而互不影响。

**练习 2**：地址 `0x17FFC`（DTCM 内最后一个 4 字节字）属于哪一段？地址 `0x18000` 呢？

**参考答案**：`0x17FFC < 0x18000`，落在 DTCM 的半开区间 `[0x10000, 0x18000)` 内，属 DTCM。`0x18000` 因 `contains` 用严格小于 `<`，**不属于** DTCM，也**不属于**任何默认区段（CSR 从 `0x30000` 起）。

---

### 4.2 Parameters 全局配置

#### 4.2.1 概念说明

`MemorySize`/`MemoryRegion` 解决了「存储」问题，但一个处理器还有大量其它参数：地址位宽是多少？一个周期取几条指令？寄存器堆有几个？L1 Cache 多大、几路组相联？对外 AXI 总线位宽多少？

CoralNPU 把所有这些常量集中到一个 `Parameters` 类里，作为**单一真相源（single source of truth）**。这样做有三个好处：

1. **一致性**：所有模块从同一份参数表里读数，不会出现「取指单元以为宽 256、译码单元以为宽 128」的口径错配。
2. **可配置**：改一个数字，整条流水线自动跟着变。
3. **软硬协同**：通过反射，这份参数还能导出成 C 头文件，让软件侧（C/C++ 程序）和 RTL 共用同一套常量。

#### 4.2.2 核心流程

`Parameters` 类的字段大致分五组，可按下面这张表建立全局印象：

| 分组 | 代表字段 | 含义 |
| --- | --- | --- |
| 机器基础 | `xlen`、`programCounterBits`、`instructionBits`、`instructionLanes` | 32 位 ISA，PC 32 位，指令 32 位，每周期最多发射 4 条标量指令 |
| 取指通道 | `fetchDataBits`、`fetchInstrSlots`、`enableFetchL0`、`fetchCacheBytes` | 取指总线位宽（默认 256 位=8 条指令/周期）、L0 缓存开关 |
| 访存通道 | `lsuDataBits`、`lsuDataBytes`、`dbusSize` | LSU 数据总线位宽（默认 256 位=32 字节/周期） |
| 寄存器堆 | `scalarRegCount`、`floatRegCount`、`rvvRegCount`、`retirementBufferSize` | 32 个整型、32 个浮点、32 个向量寄存器，ROB 8 项 |
| Cache/AXI | `l1iassoc`、`l1dassoc`、`axi0DataBits`、`axi1DataBits`、`axi2DataBits` | L1I/L1D 的容量与相联度、三组 AXI 接口位宽 |

一个关键细节：`fetchDataBits / instructionBits` 算出「每周期取多少条指令」：

\[
\text{fetchInstrSlots} \;=\; \frac{\text{fetchDataBits}}{\text{instructionBits}}
\]

默认 `fetchDataBits = 256`、`instructionBits = 32`，所以 `fetchInstrSlots = 8`——这与第 4 单元后续要讲的「四发射」是两件事：取指带宽一次能拿 8 条，但派发端每周期只放行 4 条（`instructionLanes = 4`）。代码里用 `assert` 守住「位宽必须能被 32 整除」这条不变量。

#### 4.2.3 源码精读

`Parameters` 类声明带默认参数，这样 `new Parameters` 就能拿到一份「IP 设计默认值」：

[Parameters.scala:69-74](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala#L69-L74) — 构造参数有三：内存区序列 `m`（默认空，装配时再注入）、`hartId`（默认 0）、`xlen`（默认 32）。类体第一组就是机器基础常量：`programCounterBits = xlen = 32`、`instructionBits = 32`、`instructionLanes = 4`。

取指与访存「宽通道」字段，是后续流水线讲义反复引用的数字：

[Parameters.scala:128-143](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala#L128-L143) — 取指总线：`fetchAddrBits = programCounterBits`、`fetchDataBits = 256`（注释 `do not change`），并用 `fetchInstrSlots` 计算「每周期指令数」。访存总线：`lsuAddrBits = programCounterBits`、`lsuDataBits = 256`、`lsuDataBytes = 32`、`dbusSize = log2Ceil(32)+1 = 6`。`dbusSize` 这个对数尺寸会直接喂给 [u3-l2](u3-l2-axi-integration.md) 讲过的 `DBus2Axi`，用来生成 AXI 的 `size` 字段。

```scala
var fetchDataBits = 256  // do not change
def fetchInstrSlots: Int = {
  assert(fetchDataBits % instructionBits == 0)
  fetchDataBits / instructionBits
}
val lsuDataBits = 256
def dbusSize: Int = { log2Ceil(lsuDataBits / 8) + 1 }
```

寄存器堆与退休缓冲（ROB）的尺寸，关系到 [u4-l4 派发与退休](u4-l4-dispatch-scoreboard-retire.md)：

[Parameters.scala:109-122](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala#L109-L122) — 32 个标量寄存器、32 个浮点寄存器、32 个 RVV 寄存器；浮点基址 32、RVV 基址 64（即它们在统一编号空间里错开）；ROB 深度 `retirementBufferSize = 8`。`retirementBufferIdxWidth` 是按「当前真正启用的寄存器数 +2（占位的 no-write/store 槽）」算出的索引位宽，会随 `enableFloat`/`enableRvv` 动态变化。

L1 Cache 与三组 AXI 接口位宽，对应 [u6-l3 L1 Cache](u6-l3-l1-cache.md)：

[Parameters.scala:149-167](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala#L149-L167) — `axi0`（L1I 接口，数据位宽=取指位宽）、`axi1`（L1D 接口，数据位宽=访存位宽）、`axi2`（TCM 向量/标量接口，数据位宽=访存位宽）。Cache 容量由 `l1islots/l1dslots = 256` 与相联度 `l1iassoc/l1dassoc = 4` 共同决定。

最后是一个容易被忽略、却把软硬件「焊」在一起的关键机制——用 Scala 反射把参数导出成 C 头文件：

[Parameters.scala:177-216](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala#L177-L216) — `EmitParametersHeader.apply(p)` 遍历 `Parameters` 实例的所有 `val`/`var` 字段，凡类型为 `Int` 或 `Boolean` 的，就输出一行 `#define KP_<字段名> <值>`。这样软件侧只需 `#include "parameters.h"` 就能拿到与 RTL 完全一致的常量（例如 `KP_dbusSize`、`KP_retirementBufferIdxWidth`），避免软件硬编码后与硬件漂移。这正是「单一真相源」理念的落地。

#### 4.2.4 代码实践

**目标**：亲手走一遍 `fetchInstrSlots` 与 `dbusSize` 的推导，理解宽通道如何换算成「每周期搬多少」。

**步骤**：

1. 在 [Parameters.scala:130-136](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala#L130-L136) 找到 `fetchDataBits = 256` 与 `fetchInstrSlots` 的定义。
2. 在 [Parameters.scala:140-143](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala#L140-L143) 找到 `lsuDataBits = 256` 与 `dbusSize` 的定义。
3. 手算两个值。

**需要观察的现象**：

- `fetchInstrSlots = 256 / 32 = 8`：取指每周期可拿 8 条 32 位指令。
- `dbusSize = log2Ceil(256/8) + 1 = log2Ceil(32) + 1 = 5 + 1 = 6`：注意这是「size+1」的编码习惯，因为 `dbusSize` 取自 `log2Ceil(bytes)+1`，用来覆盖 0 字节的边界情形。

**预期结果**：取指带宽 8 条/周期、访存带宽 32 字节/周期。这两个数将在第 4 单元后续讲义（取指缓冲、LSU）中反复出现。**待本地验证**：如果你在本机装了 Scala，可把这两段拷出单独运算确认；否则按上面手算即可。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `fetchAddrBits` 与 `lsuAddrBits` 都被设成 `programCounterBits`（即 `xlen`）并标注「do not change」？

**参考答案**：地址位宽必须等于 ISA 的地址空间宽度。RV32 的地址是 32 位，所以取指与访存的地址总线也是 32 位。改它会导致地址译码、`contains` 的位宽提升（`addrWidth.W`）以及 AXI 地址位宽全部失配，属破坏性改动，故标注不可变。

**练习 2**：`EmitParametersHeader` 只导出 `Int` 和 `Boolean` 类型的字段，为什么不导出 `MemoryRegion` 这类复杂字段？

**参考答案**：C 头文件里只能表达标量常量（`#define`），无法表达对象/序列。`MemoryRegion` 是运行期对象，软件侧通常通过链接脚本与符号表而非编译期宏来感知内存布局，所以不在头文件里导出。

---

### 4.3 enable\* 功能开关

#### 4.3.1 概念说明

`Parameters` 里的字段分成两类：

- **常量（`val`）**：如 `fetchDataBits = 256`，代表「设计选定的不可变参数」。
- **开关（`var`，且以 `enable` 开头）**：代表「可裁剪的功能模块」，装配时可按目标场景打开或关闭。

CoralNPU 是一个面向多种部署形态的 IP：完整 SoC 要 RVV+浮点全开；轻量仿真目标（如 `core_mini_axi_sim`）可能只开浮点、关 RVV；某些场景还要额外打开验证逻辑或调试端口。`enable*` 开关就是这些裁剪的「旋钮」。

#### 4.3.2 核心流程

开关的取值有两种注入路径（详见 4.4 与 [Core.scala](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Core.scala)）：

1. **命令行参数**：在 `EmitCore` 里逐条解析 `--enableRvv=true` 之类的参数，写入 `p.enableRvv`。
2. **SoC 装配**：`SoCChiselConfig` 在构造 `CoreTlulParameters` 时直接传 `enableRvv = true` 等字面量。

开关一旦设定，下游模块就用 `Option.when(p.enableXxx)(...)` 这种条件构造来决定是否实例化某个子模块、是否暴露某组 IO。例如 `Core.scala` 里 `val rvvCore = Option.when(p.enableRvv)(RvvCore(p))`——关掉 RVV 时，整个向量后端根本不会出现在生成的 RTL 里。

下面这张表是本讲最重要的速查表：

| 开关 | 默认值 | 打开后启用 |
| --- | --- | --- |
| `enableRvv` | `false` | RVV 向量后端（`RvvCore`、向量 CSR、`CsrRvvIO`） |
| `enableFloat` | `false` | 标量浮点（RV32F、`fs`/`mstatus.FS` 置位、`CsrFloatIO`） |
| `enableZfbfmin` | `false` | 标量 Zfbfmin BFloat16 扩展 |
| `enableVectorBf16` | `false` | 向量 BFloat16 支持 |
| `enableFetchL0` | `true` | 取指 L0 缓存（生产 SoC 通常关闭，见 4.4） |
| `enableVerification` | `false` | 完整 RetirementBuffer 跟踪模式（`mini = false`）+ 自动暴露 debug 端口 |
| `exposeDebugPorts` | `false` | 显式暴露 debug/trace 端口（与上一项解耦） |

#### 4.3.3 源码精读

所有开关都集中在 `Parameters` 类的上半部分，且默认值几乎都是 `false`（除了 `enableFetchL0`）：

[Parameters.scala:75-107](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala#L75-L107) — `enableVerification`、`exposeDebugPorts`、`enableRvv`、`enableFloat`、`enableZfbfmin`、`enableVectorBf16` 一字排开，外加 RVV 的向量长度 `rvvVlen = 128`（`rvvVlenb = 128/8 = 16` 字节）与一个浮点除法器选型 `floatPulpDivsqrt = 0`（注释说明这是 PULP 版除/开方，更小但有微小舍入误差）。

这里有一段很值得读的注释，解释了为什么 `enableVerification` 与 `exposeDebugPorts` 是**解耦**的两个开关，而不是合并成一个：

[Parameters.scala:78-92](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala#L78-L92) — 注释说明：直觉上「调试端口只为验证用」应当与 `enableVerification` 耦合，但耦合并不简单——因为 `enableVerification` 还会同时启用「完整 ROB 跟踪模式」。在「关 RVV、开 Float」的配置下（如 `core_mini_axi_sim`），完整 ROB 模式有硬件 bug（向量 store 等待、复位振荡）。此时仍需要暴露 debug 端口来给 riscv-dv 协同仿真产出指令轨迹，但必须让 ROB 跑在 `mini` 模式（`enableVerification = false`）以避免挂死。因此二者被刻意分开。`shouldExposeDebugPorts` 则是「显式请求 或 自动（开了完整验证）」的合成条件。

开关下游如何生效？以 `enableRvv` 为例，`Csr.scala` 与 `Core.scala` 都用 `Option.when` 条件构造：

[Core.scala:63-66](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Core.scala#L63-L66) — `val rvvCore = Option.when(p.enableRvv)(RvvCore(p))`，且只有 `enableRvv` 为真时才把 `rvvCore.io` 接到标量核的 `rvvcore` 端口。关掉它，RTL 里就不会有 `RvvCore` 实例。

#### 4.3.4 代码实践

**目标**：把所有 `enable*` 开关按「场景」对号入座，建立一张「目标→开关组合」的选型表。

**步骤**：

1. 在 [Parameters.scala:75-107](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala#L75-L107) 列出全部开关及默认值。
2. 对照 [Core.scala:99-117](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Core.scala#L99-L117)，看命令行支持哪几个开关。
3. 对照下一节 4.4 的 `SoCChiselConfig`，看生产 SoC 实际打开的是哪几个。

**需要观察的现象**：

- 命令行可设的开关：`enableFetchL0`、`enableRvv`、`enableFloat`、`enableZfbfmin`、`enableVectorBf16`、`enableVerification`、`exposeDebugPorts`（外加 `fetchDataBits`、`lsuDataBits`、`itcmSizeKBytes`、`dtcmSizeKBytes` 等数值参数）。
- 注意 `enableVerification` 与 `exposeDebugPorts` 在命令行里是两个独立参数，印证了 4.3.3 的「解耦」设计。

**预期结果**：你能口述「跑一个最小标量核」需要的开关组合（关 RVV、关 Float、关验证）与「跑完整 ML SoC」需要的开关组合（开 RVV、开 Float）。**待本地验证**：如要确认某目标实际用了哪些开关，可在该目标的 Bazel 规则里查找传给 `EmitCore` 的参数。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `enableFetchL0` 默认是 `true`，而生产 SoC（见 4.4）却把它设成 `false`？

**参考答案**：`Parameters` 的默认值面向「裸核/IP 设计」场景，假设取指可能 miss 到片外，因此默认带一个小 L0 缓存以平滑延迟。生产 SoC 里代码常驻 ITCM（单周期可取），取指几乎不会 miss，L0 缓存的收益有限却占面积，故装配时关闭。

**练习 2**：如果某仿真目标「需要指令轨迹，但跑完整 ROB 会挂死」，应当怎么组合 `enableVerification` 与 `exposeDebugPorts`？

**参考答案**：`enableVerification = false`（让 ROB 跑 mini 模式避免挂死）+ `exposeDebugPorts = true`（仍产出 debug/trace 端口）。这正是 `shouldExposeDebugPorts = exposeDebugPorts || enableVerification` 这条合成规则存在的意义。

---

### 4.4 参数注入：从 SoCChiselConfig 到 Core

#### 4.4.1 概念说明

到目前为止，`Parameters` 还只是一份「带默认值的清单」。真正让它「活起来」的是装配阶段——有人要在生成 RTL 之前，按目标场景把开关拨到正确位置、把内存区序列塞进 `p.m`、再把这份配置传给 `Core`。

CoralNPU 有两个注入入口：

1. **命令行注入**：`object EmitCore` 的 `main` 逐条解析 `--enableXxx=...` 参数，直接写入 `val p = new Parameters` 的字段。适合「裸核独立综合」场景。
2. **SoC 装配注入**：`SoCChiselConfig` 把内核相关参数包成 `CoreTlulParameters`，在装配 `rvv_core` 模块时传进去。适合「完整 SoC」场景。

本节聚焦第二条，因为它体现了「单一真相源 + 配置驱动装配」的整体思路（与 [u3-l1](u3-l1-soc-subsystem.md) 一脉相承）。

#### 4.4.2 核心流程

注入流程可以概括为三步：

```
SoCChiselConfig.apply(itcmSize, dtcmSize)
        │
        ▼
   ① 选内存区划 memoryRegions
      ├─ 若 itcm/dtcm 都是默认大小 → MemoryRegions.default
      └─ 否则                      → MemoryRegions.highmem(...)
        │
        ▼
   ② 构造 CrossbarConfig + modules 序列
      └─ 其中 rvv_core 的 params = CoreTlulParameters(
              enableRvv = true, enableFloat = true,
              enableFetchL0 = false, lsuDataBits = 128, ...)
        │
        ▼
   ③ 顶层 CoralNPUChiselSubsystem 按 params 实例化 CoreTlul → Core
```

注意一个重要细节：`SoCChiselConfig` 里 `rvv_core` 用的是 **128 位**宽通道（`lsuDataBits = 128`、`fetchDataBits = 128`），而裸核 `Parameters` 默认是 **256 位**。也就是说，「IP 设计默认」与「生产 SoC 实际」并不相同——生产 SoC 出于面积/功耗权衡选择了更窄的通道。这是读源码时最容易踩坑的地方：**不要把 `Parameters` 的默认值当成 SoC 的真实配置**。

#### 4.4.3 源码精读

`SoCChiselConfig` 的工厂方法把 ITCM/DTCM 大小作为入口参数，默认值直接取自 `Parameters`：

[SoCChiselConfig.scala:110-127](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/SoCChiselConfig.scala#L110-L127) — `apply` 默认 `itcmSize = 8KB`、`dtcmSize = 32KB`（取自 `Parameters.itcmSizeKBytesDefault/dtcmSizeKBytesDefault`）。`memoryRegions` 的选择逻辑与 `EmitCore` 完全对称：默认大小用 `MemoryRegions.default`，否则用 `highmem`。注释称自己是「整个 Chisel SoC 的单一真相源」。

```scala
val memoryRegions = {
  if (itcmSize == defaultItcmSize && dtcmSize == defaultDtcmSize)
    MemoryRegions.default
  else
    MemoryRegions.highmem(itcmSize.kBytes, dtcmSize.kBytes)
}
```

真正把参数喂给 Core 的是 `rvv_core` 这一行的 `CoreTlulParameters`：

[SoCChiselConfig.scala:130-160](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/SoCChiselConfig.scala#L130-L160) — `rvv_core` 模块用 `CoreTlulParameters(lsuDataBits = 128, enableRvv = true, enableFetchL0 = false, fetchDataBits = 128, enableFloat = true, memoryRegions = memoryRegions)` 注入内核配置。这正是生产 SoC 的「满配但窄通道」选择：RVV 与浮点全开、取指 L0 关闭、通道降到 128 位。`memoryRegions` 也通过这个参数对象传进 Core，供内部的地址译码使用。

`CoreTlulParameters` 这个 case class 把「内核真正关心的参数」从庞大的 `Parameters` 里挑出来、显式列名，避免 Core 与它不需要的字段耦合：

[SoCChiselConfig.scala:42-49](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/SoCChiselConfig.scala#L42-L49) — `CoreTlulParameters` 只含六个字段：`lsuDataBits`、`enableRvv`、`enableFetchL0`、`fetchDataBits`、`enableFloat`、`memoryRegions`。这是一种「显式接口」做法——Core 对外只承诺依赖这几样，参数表的其余变动不会波及它。

命令行注入路径则在 `EmitCore` 里，与 SoC 注入是并列的两条路：

[Core.scala:99-135](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Core.scala#L99-L135) — `EmitCore` 的 `main` 用一个 `for (arg <- args)` 循环逐条匹配 `--enableXxx=`、`--fetchDataBits=`、`--itcmSizeKBytes=` 等参数，写入 `val p = new Parameters` 的字段。随后 [Core.scala:138-166](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Core.scala#L138-L166) 根据最终 `itcmSizeKBytes/dtcmSizeKBytes` 决定模块名后缀（默认无后缀、highmem 加 `Highmem`、其它加 `_ITCM<n>KB_DTCM<n>KB`），并按 `--useAxi`/`--useTlul` 选择实例化 `CoreAxi` 还是 `CoreTlul`，同时把选好的 `memoryRegions` 赋给 `p.m`。

#### 4.4.4 代码实践

**目标**：对比「裸核默认」与「生产 SoC」两套配置，体会「默认值 ≠ 实际值」。

**步骤**：

1. 在 [Parameters.scala:130,140](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala#L128-L143) 读出裸核默认 `fetchDataBits = 256`、`lsuDataBits = 256`、`enableRvv = false`、`enableFloat = false`、`enableFetchL0 = true`。
2. 在 [SoCChiselConfig.scala:134-141](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/SoCChiselConfig.scala#L130-L160) 读出生产 SoC 的 `fetchDataBits = 128`、`lsuDataBits = 128`、`enableRvv = true`、`enableFloat = true`、`enableFetchL0 = false`。
3. 把两者列成对照表。

**需要观察的现象**：

| 字段 | 裸核默认 | 生产 SoC |
| --- | --- | --- |
| `fetchDataBits` | 256 | 128 |
| `lsuDataBits` | 256 | 128 |
| `enableRvv` | false | true |
| `enableFloat` | false | true |
| `enableFetchL0` | true | false |

**预期结果**：通道位宽减半、功能全开、L0 关闭——这正是 SoC 装配「覆盖默认值」的结果。结论：阅读任何具体模块时，若它用到通道位宽或开关，必须回到 `SoCChiselConfig`（或具体 Bazel 目标的命令行参数）确认真实取值，不能直接信 `Parameters` 的默认值。**待本地验证**：如需确认某个仿真目标（如 `core_mini_axi_sim`）的真实开关组合，可在 `tests/` 对应 BUILD 里查它传给 `EmitCore` 的参数。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `CoreTlulParameters` 只挑了六个字段，而不是把整个 `Parameters` 对象直接传给 Core？

**参考答案**：显式接口降低了耦合。Core 只依赖这六个字段，`Parameters` 其余字段（如 L1 Cache 相联度、AXI id 位宽等）的变动不会强迫 Core 重新审视自己的参数契约；同时装配方在构造 `CoreTlulParameters` 时被强制思考「Core 到底需要什么」，避免无意中把内部参数泄露出去。

**练习 2**：`EmitCore` 里 [Core.scala:138-144](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Core.scala#L138-L144) 根据内存大小给模块名加后缀，这样做有什么好处？

**参考答案**：不同 ITCM/DTCM 尺寸会生成不同的内存映射（default vs highmem），从而是不同的 RTL 模块。给模块名加后缀（如 `CoreHighmem`、`Core_ITCM16KB_DTCM64KB`）可以让 Bazel 缓存按配置区分产物，避免不同尺寸的 RTL 互相覆盖，也方便在波形和综合报告里一眼认出是哪套配置。

## 5. 综合实践

把本讲四节串起来，完成下面这张「参数速查表」的填写与核对：

1. **容量**：用 `MemorySize.fromKBytes(32)` 构造一个 32KB 的容量对象，写出它的 `bytes` 与 `kBytes` 字段值（答案：32768 / 32）。
2. **区划**：在 [Parameters.scala:42-46](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/coralnpu/Parameters.scala#L42-L46) 读出默认三段区，用半开区间写下 ITCM/DTCM/CSR 的范围，并与 [integration_guide.md:160-164](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/doc/integration_guide.md#L160-L164) 对照。
3. **宽通道**：手算默认 `fetchInstrSlots` 与 `dbusSize`（8 与 6）。
4. **开关**：列出全部 `enable*` 开关与默认值，标注哪些会被生产 SoC 改写。
5. **注入**：画出「`SoCChiselConfig.apply` → `memoryRegions` 选择 → `CoreTlulParameters` → `rvv_core`」这条数据流，并在图上标出「128 位 vs 256 位」的差异点。

完成后，你应能用一句话回答：「CoralNPU 的内核配置是怎么从一份数据结构最终落到 RTL 里的？」

## 6. 本讲小结

- `MemorySize` 只描述「有多大」（字节数 + KB/MB 换算），`MemoryRegion` 描述「在哪、什么类型」，二者是分层抽象。
- 地址归属用**半开区间** `[start, start+size)` 判定，`contains(addr)` 是 `>=` 与 `<` 的合取，保证边界无歧义。
- 默认内存映射：ITCM `0x0`/8KB、DTCM `0x10000`/32KB、CSR `0x30000`/4KB，与 `integration_guide`、链接脚本三方一致。
- `Parameters` 是内核全局配置的**单一真相源**：宽通道（取指 256、访存 256）、寄存器堆（32+32+32）、Cache/AXI 接口、`enable*` 开关，并经 `EmitParametersHeader` 反射导出 C 头文件与软件侧共享。
- `enable*` 开关是功能裁剪旋钮；其中 `enableVerification` 与 `exposeDebugPorts` 因 ROB 完整模式有 bug 而**刻意解耦**。
- `SoCChiselConfig` 是参数注入入口，生产 SoC 用 `CoreTlulParameters` 把内核配成「RVV+Float 全开、L0 关闭、通道 128 位」——与裸核 256 位默认值不同，读模块时务必以 SoC 配置为准。

## 7. 下一步学习建议

本讲建立的「参数与内存区划」是整个标量核阅读的地基，接下来建议按顺序：

- **[u4-l2 取指、指令缓冲与重排序](u4-l2-fetch-instruction-buffer.md)**：看 `fetchDataBits`/`fetchInstrSlots` 如何变成每周期取 8 条指令的宽通道，以及 `InstructionBuffer` 如何缓冲。
- **[u4-l3 指令译码](u4-l3-decode.md)**：看译码如何识别 ALU/BRU/MLU/DVU/LSU/CSR 等类型，这些类型正是 `instructionLanes` 与执行单元选择的依据。
- **[u6-l2 TCM 与 SRAM](u6-l2-tcm-sram.md)**：看本讲的 ITCM/DTCM 区划如何落到具体的 `TCM.scala` 与 `SramNx128` 硬件上，验证宽通道在 SRAM 端的实现。

如果对 SoC 装配还想加深，可重读 [u3-l1 SoC 顶层子系统装配](u3-l1-soc-subsystem.md) 中 `populatePorts` 与 `modulePorts` 的反射装配机制，那是本讲 4.4 的上游。
