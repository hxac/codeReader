# SoC 顶层子系统装配

## 1. 本讲目标

前两个单元（[u1](u1-l1-project-overview.md)、[u2](u2-l2-write-compile-program.md)）我们站在「使用者和程序视角」认识了 CoralNPU：它是一颗 rv32imf 的 ML 加速核，程序跑在 ITCM/DTCM 上，能被外部主机经 AXI 启动。从本讲起，我们正式钻进**硬件 RTL**，自顶向下拆解这颗芯片是怎么「搭」出来的。

本讲聚焦 **SoC 顶层**——也就是把标量核、总线、外设、中断、内存这些零件**装配**成一个完整芯片的那层胶水代码。学完本讲，你应当能够：

- 说清 `CoralNPUChiselSubsystem` 为什么是一台「配置驱动的 SoC 装配器」，而不是一堆手写的 `Module()` + `<>`。
- 在 `SoCChiselConfig` 里找到 SoC 的「单一真相源」：哪些模块被装配、各自的参数是什么、怎么挂到总线上、哪些端口要暴露到芯片引脚。
- 理解 `enableRvv` / `enableFloat` / `enableFetchL0` 等开关如何从配置一路流进标量核 `CoreTlul`。
- 看懂 `ExternalPort` 机制如何把非总线端口（例如 SPI 的片选 `spi.csb`、内核的 `halted` 状态）「提升」到芯片顶层。

本讲是第 3 单元（SoC 与总线集成）的入口，也是 [u3-l2](u3-l2-axi-integration.md)（AXI 接口）、[u3-l3](u3-l3-tlul-axi-bridge.md)（TileLink↔AXI 桥接）、[u3-l4](u3-l4-crossbar-fabric.md)（总线互联）的前置。

## 2. 前置知识

阅读本讲前，建议你已经了解：

- **Chisel 基本语法（一句话版）**：Chisel 是嵌在 Scala 里的硬件构造语言，`Module(new Foo)` 实例化一个硬件模块，`a <> b` 把两个端口连起来，`val io = IO(...)` 声明对外的引脚。CoralNPU 的标量核、SoC、总线、外设都是用 Chisel 写的（见 [u1-l2](u1-l2-directory-structure.md)）。
- **总线里的 host / device**：在 TileLink（以及 AXI）语境下，**host（主机/master）**发起读、写事务，**device（从机/slave）**响应。标量核是 host，外设寄存器、内存是 device。`xbar`（crossbar，交叉开关）负责把多个 host 的请求按地址路由到正确的 device。
- **「反射」与数据驱动编程（直觉版）**：普通写法是「点名」——`val core = Module(new Core); core.io.x := ...`。本讲的 SoC 顶层用了另一种写法：把所有模块的配置塞进一张表，再用循环 + Scala 反射「按名字」自动实例化和连线。先有这个印象即可，下面会逐行讲。
- **上一讲建立的认知**：CoralNPU 的程序「无 `printf`」，主机与内核靠共享 DTCM 通信（见 [u2-l4-summary](u2-l4-cocotb-testbench-intro.md)）。本讲你会看到这套「主机经 AXI 写 DTCM、启动内核、读 STATUS」的机制在 RTL 顶层是怎么留出端口的。

> 名词速查：**SoC**（System on Chip）= 把 CPU 核、总线、外设、内存控制器集成在同一片芯片上的完整系统。**TL-UL**（TileLink Uncached Lightweight）= CoralNPU 内部用的轻量总线协议（详见 [u3-l3](u3-l3-tlul-axi-bridge.md)）。**顶层 / top-level** = 整个芯片最外面那一层模块，它的 IO 就是芯片的物理引脚。

## 3. 本讲源码地图

本讲围绕 SoC 顶层装配的三个文件展开。它们都位于 `hdl/chisel/src/soc/`：

| 路径 | 作用 |
|------|------|
| `hdl/chisel/src/soc/SoCChiselConfig.scala` | **单一真相源**：声明 SoC 装配哪些模块、各模块参数、与总线的连接关系、要暴露的顶层端口。 |
| `hdl/chisel/src/soc/CoralNPUChiselSubsystem.scala` | **顶层装配器**：读取上面的配置，动态实例化所有模块、自动连线，并生成最终 SystemVerilog。 |
| `hdl/chisel/src/soc/SoCRecords.scala` | **动态 Record 工具**：提供 `DataRecord` / `TLBundleMap` / `ClockResetBundle`，让顶层 IO 能「按名字」动态生成端口。 |

> 辅助理解（非本讲精读对象）：`CrossbarConfig.scala` 描述总线拓扑（哪些 host、哪些 device、地址映射），`CoralNPUXbar.scala` 是真正的总线互联实现——它们是 [u3-l4](u3-l4-crossbar-fabric.md) 的主角，本讲只把它们当作「被装配器调用的一个子模块」。

## 4. 核心概念与源码讲解

本讲把 SoC 顶层装配拆成 4 个最小模块，按「配置 → 实例化 → 连线 → 参数与端口」的顺序讲。

### 4.1 配置驱动的 SoC 装配：SoCChiselConfig 是「单一真相源」

#### 4.1.1 概念说明

想象你要搭一个 SoC。最直白的写法是「点名式」：在一个大 `Module` 里手写 `val core = Module(new CoreTlul(...))`、`val spi = Module(new Spi2TLUL(...))`，再手写几十条 `core.io <> xbar.io...`。这种写法能跑，但有两个痛点：

1. **改动牵一发动全身**：加一个外设，要改实例化、改连线、改顶层 IO，三处容易漏。
2. **重复劳动**：每个外设的「实例化 + 挂总线 + 暴露引脚」套路都一样，却要一遍遍手写。

CoralNPU 的做法是**配置驱动（data-driven）**：把「装什么、怎么连、暴露什么」全部写进一张声明式的配置表 `SoCChiselConfig`，让顶层装配器用循环去解读它。这张表就是整个 Chisel 侧 SoC 的**单一真相源（single source of truth）**——想知道 SoC 长什么样，看这一张表就够了。

这张表里的每一行就是一个 `ChiselModuleConfig`，它用四个字段描述一个模块：

- `name`：实例名（如 `"rvv_core"`、`"spi2tlul"`），后续按这个名字连线。
- `params`：模块参数（类型安全，下面 4.4 详讲）。
- `hostConnections` / `deviceConnections`：这个模块的哪个 TL 端口，连到 crossbar 的哪个命名端口。
- `externalPorts`：这个模块的哪些**非总线**端口要提升到芯片顶层。

#### 4.1.2 核心流程

SoC 配置表的解读流程是：

1. 读 `SoCChiselConfig` → 得到 `memoryRegions`（内存映射）和 `crossbar`（总线拓扑）。
2. 读 `modules`（一个 `Seq[ChiselModuleConfig]`）→ 得到要装配的全部模块清单。
3. 对每个 `ChiselModuleConfig`：由装配器自动实例化、按 `hostConnections`/`deviceConnections` 挂总线、按 `externalPorts` 暴露引脚。

#### 4.1.3 源码精读

先看 `ChiselModuleConfig` 这个「一行模块」的数据结构定义：

[SoCChiselConfig.scala:98-105](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/SoCChiselConfig.scala#L98-L105)：定义 `ChiselModuleConfig`，四个字段分别是实例名、模块类全名、参数、以及默认为空的 host/device 连接映射和外部端口序列。

再看「单一真相源」的入口——`SoCChiselConfig` 伴生对象，它根据 ITCM/DTCM 大小决定用默认内存映射还是 `highmem`：

[SoCChiselConfig.scala:110-127](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/SoCChiselConfig.scala#L110-L127)：`apply` 工厂方法构造配置；`memoryRegions` 在默认 8KB/32KB 时用 `MemoryRegions.default`，否则用 `highmem`（把 DTCM/CSR 基址上移，留出可扩到 1MB 的空间）。

最关键的是 `modules` 这个 `Seq`——它就是整张 SoC 装配清单。先看头部两条（标量核 + SPI 从机）：

[SoCChiselConfig.scala:131-160](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/SoCChiselConfig.scala#L131-L160)：`rvv_core` 的配置——实例化 `coralnpu.CoreTlul`，参数里把 `enableRvv`/`enableFloat` 置 `true`、`enableFetchL0` 置 `false`；它的 `io.tl_host` 连到 crossbar 的 `coralnpu_core` 主机口，`io.tl_device` 连到 `coralnpu_device` 从机口，并把 `halted`/`fault`/`wfi`/`te`/`boot_addr`/`dm.*` 等状态与调试端口声明为 `externalPorts`。

[SoCChiselConfig.scala:161-172](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/SoCChiselConfig.scala#L161-L172)：`spi2tlul` 的配置——它是 host（`io.tl` 连到 crossbar 的 `spi2tlul` 主机口），同时把 SPI 物理引脚 `spi_clk`/`spi_csb`/`spi_mosi`/`spi_miso` 暴露到顶层。这正是「非总线端口经 `externalPorts` 提升到引脚」的典型例子。

剩余外设的装配清单结构完全一致，集中在同一段：

[SoCChiselConfig.scala:187-254](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/SoCChiselConfig.scala#L187-L254)：依次声明 `spi_master`、`gpio`、`dma`、`spi_master_flash`、`clint`、`plic`、`sram` 七个模块的实例化与连接关系。注意 `dma` 同时是 host（`io.tl_host`→`dma`）又是 device（`io.tl_device`→`dma`）——DMA 既能发起也能被配置；`sram` 是一片 4MB 的 TL-UL SRAM（基址 `0x20000000`）。

> 💡 一句话理解：`SoCChiselConfig` 像一份「SoC 物料清单（BOM）」，每个 `ChiselModuleConfig` 是一行料号，装配器照单抓药。

#### 4.1.4 代码实践

**实践目标**：建立对「配置即 SoC」的直觉。

**操作步骤**：
1. 打开 `hdl/chisel/src/soc/SoCChiselConfig.scala`，定位 `val modules = Seq(...)`。
2. 逐行数出 `modules` 里一共有几个 `ChiselModuleConfig`（即几个被装配的模块）。
3. 对每个模块，记下：实例名、它是 host / device / 两者皆是。

**需要观察的现象**：你会发现 `rvv_core`、`dma`、`spi2tlul`、`ispyocto` 是 host（在 `hostConnections` 里有键），其余大多是纯 device；`dma` 同时出现在 `hostConnections` 和 `deviceConnections`，是「既是主又是从」的特例。

**预期结果**：`modules` 共 10 个 `ChiselModuleConfig`（`rvv_core`、`spi2tlul`、`ispyocto`、`spi_master`、`gpio`、`dma`、`spi_master_flash`、`clint`、`plic`、`sram`）。若你数到的数量不同，回到源码核对。

> 本实践为纯源码阅读型，无需运行命令。

#### 4.1.5 小练习与答案

**练习 1**：如果要在 SoC 里新增一个 `uart` 外设（TL-UL 从机，基址 `0x40000000`），按本表的写法，你需要改 `SoCChiselConfig` 的哪几处？

**参考答案**：(1) 在 `CrossbarConfig` 的 `devices` 里加一条 `DeviceConfig("uart", Seq(AddressRange(0x40000000, 0x1000)))`（地址映射）；(2) 在允许访问它的 host 的 `connections` 里加上 `"uart"`；(3) 在 `SoCChiselConfig.modules` 里加一个 `ChiselModuleConfig(name = "uart", ..., deviceConnections = Map("io.tl" -> "uart"), externalPorts = ...)`。注意三处都要改——这正是「单一真相源」的好处：改动集中在配置表，装配代码不用动。

**练习 2**：为什么 `clint` 和 `plic` 的 `externalPorts` 几乎为空，而 `spi2tlul` 却有一堆 SPI 引脚？

**参考答案**：`clint`/`plic` 只通过 TL-UL 总线与外界交互（定时器中断、外部中断都经寄存器或专用内部信号传递，中断连线由装配器手工接到核——见 4.3），没有「对外物理引脚」；而 SPI 控制器必须把 `sclk/csb/mosi/miso` 这些与片外 SPI 设备通信的物理信号送到芯片引脚上，所以必须声明为 `externalPorts`。

---

### 4.2 顶层装配器 CoralNPUChiselSubsystem：动态实例化

#### 4.2.1 概念说明

有了配置表，还需要一个「照单抓药」的执行者——`CoralNPUChiselSubsystem`。它的核心技巧是**用 Scala 反射按名字访问端口**：

普通 Chisel 连线要「知道类型」——`core.io.tl_host <> xbar.io.hosts("...")`。但装配器面对的是一张配置表，表里只有**字符串名字**（如 `"io.tl_host"`、`"io.spi.csb"`）。怎么把字符串变成真实的硬件端口？答案是 `DataMirror.modulePorts` + 一个递归遍历函数 `populatePorts`：它把每个模块 IO 树上的每个叶子端口，登记进一个 `Map[String, Data]`，键就是 `"实例名.io.spi.csb"` 这样的全路径字符串。之后连线时，只要 `modulePorts("spi2tlul.io.spi.csb")` 就能取到这个端口。

这层抽象让装配器能写成「对每个配置项，用字符串键去连对应的端口」的循环，而无需为每种外设写专用代码。

#### 4.2.2 核心流程

装配器的实例化阶段：

1. **实例化 crossbar**：`val xbar = Module(new CoralNPUXbar(...))`，它是所有总线流量的汇聚点。
2. **按配置实例化每个模块**：遍历 `SoCChiselConfig(...).modules`，对每个 `ChiselModuleConfig`，依据其 `params` 的具体类型（`match`）实例化对应的 Chisel 模块。
3. **建端口索引**：对每个已实例化模块，递归遍历其 IO，建立 `"名字.端口路径" -> Data` 的映射表 `modulePorts`。
4. **接线**：进入 4.3 详讲的连线阶段。

#### 4.2.3 源码精读

装配器本体是一个 `RawModule`（不用默认时钟域，自己管 clock/reset）：

[CoralNPUChiselSubsystem.scala:92-104](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUChiselSubsystem.scala#L92-L104)：`CoralNPUChiselSubsystem` 构造参数为 host/device 参数序列、`enableTestHarness`、ITCM/DTCM 大小；`desiredName` 按 TCM 大小取不同模块名（默认 `CoralNPUChiselSubsystem`、大内存 `...Highmem`、其它自定义），并 `IO(new CoralNPUChiselSubsystemIO(...))` 声明顶层端口。

实例化的第一步是放进统一的时钟复位域，并实例化 crossbar：

[CoralNPUChiselSubsystem.scala:125-127](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUChiselSubsystem.scala#L125-L127)：`withClockAndReset(...)` 把后续实例化圈进「主时钟 + 低有效复位取反」的统一域；先实例化 `CoralNPUXbar`。

「按配置实例化」的核心是一个 `instantiateModule` 辅助函数，它对 `params` 做**模式匹配**：

[CoralNPUChiselSubsystem.scala:130-191](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUChiselSubsystem.scala#L130-L191)：`instantiateModule` 用 `config.params match { ... }` 区分模块类型——`CoreTlulParameters` 实例化 `CoreTlul`、`Spi2TlulParameters` 实例化 `Spi2TLUL`、`DmaParameters` 实例化 `DmaEngine`，依此类推；`IspParameters` 返回 `null`（ISP 在顶层外部处理）。

其中标量核的实例化最值得细看——它把 `CoreTlulParameters` 里的开关逐字段拷进一个 `Parameters` 对象：

[CoralNPUChiselSubsystem.scala:132-142](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUChiselSubsystem.scala#L132-L142)：实例化 `CoreTlul` 时，把 `enableRvv`、`enableFetchL0`、`fetchDataBits`、`enableFloat`、`lsuDataBits`、`memoryRegions`、TCM 大小全部灌入 `core_p`，再 `Module(new CoreTlul(core_p, config.name))`。这正是 4.1.3 看到 `enableRvv = true` 等开关生效的落点。

实例化结果收集进一个 `Map[名字 -> 模块]`，并给每个模块起好名字（`suggestName`）：

[CoralNPUChiselSubsystem.scala:193-202](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUChiselSubsystem.scala#L193-L202)：遍历 `SoCChiselConfig(...).modules`，调用 `instantiateModule`，跳过返回 `null` 的（ISP），用 `suggestName(config.name)` 固定实例名，结果存进 `instantiatedModules` 映射。

接着是反射建索引的关键工具——`populatePorts` 递归把整棵 IO 树拍平成路径字符串：

[CoralNPUChiselSubsystem.scala:110-123](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUChiselSubsystem.scala#L110-L123)：`populatePorts(prefix, data, map)` 递归遍历 `Record`（按字段名展开）和 `Vec`（按下标展开），把每个叶子端口登记为 `prefix` 全路径，例如最终得到 `"spi2tlul.io.spi.csb"` 这样的键。

然后用它建立全模块的端口索引：

[CoralNPUChiselSubsystem.scala:208-214](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUChiselSubsystem.scala#L208-L214)：对每个已实例化模块，用 `DataMirror.modulePorts(module)` 取出其顶层 IO，再 `populatePorts` 拍平进 `modulePorts`，键形如 `"模块名.io.端口"`。

> 💡 一句话理解：`modulePorts` 是一张「端口全路径 → 硬件对象」的电话簿，`populatePorts` 负责把整本通讯录建好，后面连线只需查名字。

#### 4.2.4 代码实践

**实践目标**：跟踪一条「配置 → 实例化」的路径，验证反射机制的键名格式。

**操作步骤**：
1. 在 `CoralNPUChiselSubsystem.scala` 里找到 `instantiateModule` 的 `case p: SpiMasterParameters =>` 分支。
2. 回到 `SoCChiselConfig.scala`，确认 `spi_master` 模块的 `externalPorts` 里有一个 `ExternalPort("spim_csb", Bool, Out, "io.spi.csb")`。
3. 推演：装配器实例化后，`modulePorts` 里 `spi_master` 的片选端口键名应该是什么？

**需要观察的现象**：键名由 `suggestName(config.name)` 得到的实例名（`spi_master`）加上 `populatePorts` 的路径（`io` → `spi` → `csb`）拼成。

**预期结果**：键名应为 `spi_master.io.spi.csb`。你可以在 [CoralNPUChiselSubsystem.scala:236-247](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUChiselSubsystem.scala#L236-L247) 的 `externalPorts.foreach` 里看到 `modulePorts(s"${config.name}.${extPort.modulePort}")` 正是用这个格式取端口——对 `spi_master` 的 `csb`，拼出的就是 `spi_master.io.spi.csb`。

> 本实践为源码阅读 + 字符串推演型，无需运行命令。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `instantiateModule` 对 `IspParameters` 返回 `null`，而不是像别的模块那样实例化一个 Chisel 模块？

**参考答案**：ISP（图像信号处理器）是一个独立的外部 IP，不在本 Chisel 子系统内部实例化。装配器把它的控制口（`ispyocto_ctrl`）和两个 AXI 主机口（`ispyocto_m1`/`m2`）作为顶层端口暴露出去（见 `CoralNPUChiselSubsystem.scala` 末尾的 ISP 手工连线段），等 FPGA/SoC 集成时再从外部接进来。返回 `null` 表示「在内部跳过实例化」，但相关的总线端口仍要预留。

**练习 2**：如果新增一种外设类型，`instantiateModule` 需要怎么改？

**参考答案**：(1) 在 `SoCChiselConfig.scala` 加一个对应的 `XxxParameters extends ModuleParameters` 参数类；(2) 在 `instantiateModule` 的 `match` 里加一个 `case p: XxxParameters => Module(new Xxx(...))` 分支；(3) 在 `modules` 里用它。可见扩展点是「加一个 case 分支」，符合开闭原则。

---

### 4.3 动态连线：从配置到 `<>` 接线

#### 4.3.1 概念说明

实例化只是把零件摆好，还要把它们**连**起来。装配器的连线也分两类：

- **通用连线（循环驱动）**：对每个 `ChiselModuleConfig`，按 `hostConnections`/`deviceConnections`/`externalPorts` 三张映射，用 `modulePorts` 查名接线。这部分完全是「数据驱动」的。
- **专用连线（手工特例）**：少数模块无法套用通用套路，需要手工 `<>`——包括 **CLINT/PLIC 的中断**（要跨接到核的 `irq`/`timer_irq` 端口）、**DDR**（要把 TL-UL 经宽度桥 + `TLUL2Axi` 转成对外的 AXI）、**ISP**（外部 IP，端口经 `Axi2TLUL` 接进总线）。这些就是源码里反复出现的 `speciallyHandledDevices` / `speciallyHandledHosts`。

#### 4.3.2 核心流程

通用连线的算法：

1. 对每个配置项的 `hostConnections`：把模块端口 `<>` 到 `xbar.io.hosts(xbarPort)`。
2. 对每个 `deviceConnections`：把 `xbar.io.devices(xbarPort)` `<>` 到模块端口。
3. 对每个 `externalPorts`：按方向（In/Out）把模块端口接到顶层 `io.external_ports(name)`。
4. 顶层对外残留的 TL 端口（`externalHostPorts`/`externalDevicePorts`）也接到 crossbar。
5. 异步时钟域的 clock/reset 接到 crossbar 对应端口。
6. 手工接 CLINT/PLIC 中断、DDR AXI 桥、ISP。

#### 4.3.3 源码精读

先看时钟/复位如何统一注入每个模块（注意 `modulePorts.get(...).foreach` 的写法——按端口是否存在条件式连接）：

[CoralNPUChiselSubsystem.scala:216-223](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUChiselSubsystem.scala#L216-L223)：对每个模块，按其时钟端口名（`io.clk` / `io.clk_i` / `io.clock`，不同模块命名不一）和复位端口名（`io.rst_ni` / `io.reset`）条件式地把统一时钟/复位接上去。

通用连线的主体——遍历配置、按三张映射接线：

[CoralNPUChiselSubsystem.scala:226-248](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUChiselSubsystem.scala#L226-L248)：对每个配置项：`hostConnections` 把模块端口 `<>` 到 `xbar.io.hosts(xbarPort)`（跳过特例 host）；`deviceConnections` 把 `xbar.io.devices(xbarPort)` `<>` 到模块端口；`externalPorts` 按 `In`/`Out` 方向把模块端口接到顶层 `io.external_ports(extPort.name)`，必要时用 `asTypeOf` 做类型适配。

顶层对外残留的 TL 端口（过滤掉内部已连和特例的）也接到 crossbar：

[CoralNPUChiselSubsystem.scala:250-256](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUChiselSubsystem.scala#L250-L256)：把未被内部消费的对外 host/device TL 端口接到 crossbar 对应端口——这些就是芯片对外暴露的总线接口。

接下来是**专用连线**。第一组是中断——CLINT 的定时器/软件中断、PLIC 的外部中断跨接到核：

[CoralNPUChiselSubsystem.scala:273-286](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUChiselSubsystem.scala#L273-L286)：`clint.io.mtip` → `rvv_core.io.timer_irq`、`clint.io.msip` → `rvv_core.io.software_irq`、`plic.io.irq` → `rvv_core.io.irq`。注意这里直接用 `modulePorts("clint.io.mtip")` 这样的全路径字符串取端口——中断是「点对点」信号，不走总线，所以必须手工接。

第二组特例是 DDR——内部 TL-UL 经宽度桥和 `TLUL2Axi` 转成对外的 AXI4：

[CoralNPUChiselSubsystem.scala:305-338](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUChiselSubsystem.scala#L305-L338)：`ddr_mem` 通路：crossbar 出来的 128 位 TL-UL 先经 `TlulWidthBridge` 升到 256 位，再经 `TLUL2Axi` 转成 256 位 AXI4 接到顶层 `io.ddr_mem_axi`；这些桥跑在 `ddr` 时钟域（`ddr_clk`/`ddr_rst`）。

第三组特例是 ISP——外部 AXI 主机经 `Axi2TLUL` 转成 TL-UL 后接入总线：

[CoralNPUChiselSubsystem.scala:340-376](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUChiselSubsystem.scala#L340-L376)：ISP 集成：控制口 `ispyocto_ctrl` 直接连到 crossbar device；两个 AXI 主机口 `m1`/`m2` 各经一个 `Axi2TLUL`（跑在 `isp_axi_clk` 域）转成 TL-UL 后注入 `xbar.io.hosts("ispyocto_m1"/"ispyocto_m2")`，并把 `user.instr_type` 固定为 `MuBi4.False`（这些是数据访问，非取指）。

> 💡 为什么 ISP/DDR 要特例处理？因为它们是** AXI 接口**（对外协议）且**跨时钟域**（`isp_axi_clk`/`ddr`），既不能直接用 TL-UL 套路连，也不能进通用实例化循环——必须手工实例化桥并指定时钟域。通用循环只服务「同域、同 TL-UL 协议」的模块。

#### 4.3.4 代码实践

**实践目标**：跟踪一条从核到 SRAM 的完整总线路径，区分「通用连线」与「特例连线」。

**操作步骤**：
1. 在 `SoCChiselConfig.scala` 确认 `rvv_core` 的 `hostConnections = Map("io.tl_host" -> "coralnpu_core")`。
2. 在 `CrossbarConfig.scala`（`hdl/chisel/src/soc/CrossbarConfig.scala`）的 `connections` 里查 `coralnpu_core` 能访问哪些 device，确认 `sram` 在其中。
3. 在装配器的通用连线段（[CoralNPUChiselSubsystem.scala:228-232](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUChiselSubsystem.scala#L228-L232)）确认 `io.tl_host` 经 `xbar.io.hosts("coralnpu_core")` 进入总线。

**需要观察的现象**：核 → crossbar 这一段是**通用连线**（由循环驱动，按 `hostConnections` 接）；crossbar 内部如何把请求路由到 `sram` 则是 `CoralNPUXbar` 自己的事（socket 仲裁，[u3-l4](u3-l4-crossbar-fabric.md) 详讲）。

**预期结果**：一条「核 `io.tl_host` → `xbar.io.hosts("coralnpu_core")` →（crossbar 内部路由）→ `xbar.io.devices("sram")` → `sram` 模块」的路径。注意 `sram` 在 `externalDevicePorts` 过滤后**不是**对外端口（它是内部 device），所以不会出现在芯片引脚上。

> 本实践为源码阅读 + 路径跟踪型。若本地能跑 Bazel 且已配置 Chisel 工具链，可尝试生成顶层 SystemVerilog 后用 `grep` 在产物 `.sv` 里搜索 `coralnpu_core` / `sram_socket` 等实例名，验证你的路径图（属于可选项，生成 RTL 耗时较长）。

#### 4.3.5 小练习与答案

**练习 1**：`speciallyHandledDevices = Set("ddr_ctrl", "ddr_mem")` 和 `speciallyHandledHosts = Set("ispyocto_m1", "ispyocto_m2")` 各自把谁排除在「自动建顶层 TL 端口」之外？为什么？

**参考答案**：`ddr_ctrl`/`ddr_mem` 是 device 但被排除，因为它们的 TL-UL 流量会被手工转成 AXI（`io.ddr_ctrl_axi`/`io.ddr_mem_axi`），不应再生成 TL-UL 顶层端口；`ispyocto_m1`/`m2` 是 host 但被排除，因为它们来自外部的 ISP AXI 主机，要手工经 `Axi2TLUL` 注入，也不走通用 TL-UL host 端口。简言之：凡是「要转协议或跨域、需手工处理」的，都列入特例集合，避免通用流程重复造端口。

**练习 2**：PLIC 的外部中断信号 `ext_intrs`（31 位）在装配里怎么接？

**参考答案**：`ext_intrs` 是 `plic` 的一个 `externalPort`（`ExternalPort("ext_intrs", Logic(31), In, "io.srcs")`，见 `SoCChiselConfig.scala`），所以它经通用连线段的 `externalPorts.foreach` 提升成顶层输入端口 `io.external_ports("ext_intrs")`；而 PLIC 对内核的输出 `plic.io.irq` 则由装配器**手工**接到 `rvv_core.io.irq`（特例连线）。前者是「芯片引脚进来的中断源」，后者是「仲裁后送给核的中断线」。

---

### 4.4 参数开关与可观测端口：enableRvv/enableFloat 与 ExternalPort

#### 4.4.1 概念说明

最后这一模块把前面散见的两个机制收口：**类型安全的参数传递** 和 **可观测端口**。

**参数传递**：`SoCChiselConfig` 用一组 `case class`（`CoreTlulParameters`、`SpiMasterParameters`、…）做**类型安全**的参数容器——每种模块有自己的参数类型，装配器 `match` 时不会张冠李戴。其中 `CoreTlulParameters` 承载了核最重要的几个开关：`enableRvv`（是否启用 RVV 向量后端）、`enableFloat`（是否启用标量浮点）、`enableFetchL0`（取指是否带 L0 缓存）、`fetchDataBits`/`lsuDataBits`（通道宽度）、`memoryRegions`（内存映射）。这些开关最终流进 `Parameters` 对象，决定核内部要不要例化向量后端、浮点单元、L0 取指缓存。

**ExternalPort 与 SoCRecords**：`ExternalPort` 描述「一个非总线端口如何提升到顶层」。它的 `portType` 可以是 `Clk`/`Bool`/`Logic(width)`/`Custom(gen)` 四种之一，方向 `In`/`Out`。而 `SoCRecords.scala` 提供的 `DataRecord`、`TLBundleMap`、`ClockResetBundle` 则是构造「动态端口集合」的积木——顶层 IO 不是固定字段，而是根据配置 `Seq` 拼出来的 `Record`。

#### 4.4.2 核心流程

参数与端口的生成流程：

1. `SoCChiselConfig` 里每个模块的 `params` 是强类型 `ModuleParameters`。
2. 装配器 `match` 出具体类型，把字段拷进 `Parameters`，实例化核/外设。
3. `Parameters` 的开关（`enableRvv` 等）在核内部驱动条件式例化（如 `if (p.enableRvv) RvvCore else ...`）。
4. 顶层 IO 的 `external_ports` 是一个 `DataRecord`，由所有模块的 `externalPorts` 拼成；`external_hosts`/`external_devices` 是 `TLBundleMap`。

#### 4.4.3 源码精读

类型安全的参数定义——`CoreTlulParameters` 承载核的全部开关：

[SoCChiselConfig.scala:42-49](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/SoCChiselConfig.scala#L42-L49)：`CoreTlulParameters` 字段包括 `lsuDataBits`、`enableRvv`、`enableFetchL0`、`fetchDataBits`、`enableFloat`、`memoryRegions`——它们决定了核的指令集与通道宽度。

`ExternalPort` 的定义——描述一个待提升的端口：

[SoCChiselConfig.scala:29-34](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/SoCChiselConfig.scala#L29-L34)：`ExternalPort(name, portType, direction, modulePort)` 四字段：顶层端口名、端口类型枚举、方向、模块上的源端口路径。

端口类型枚举与方向枚举：

[SoCChiselConfig.scala:9-18](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/SoCChiselConfig.scala#L9-L18)：`PortDirection`（`In`/`Out`）与 `PortType`（`Clk`/`Bool`/`Logic(width)`/`Custom(gen)`）两个枚举，覆盖了时钟、布尔、定宽逻辑、自定义类型四种端口。

`SoCRecords.scala` 的三个积木——让顶层 IO 能动态拼装：

[SoCRecords.scala:9-22](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/SoCRecords.scala#L9-L22)：`DataRecord` 把任意 `Seq[(String, Data)]` 包成一个 `Record`（按名字访问），用于 `external_ports`、异步时钟端口等；`TLBundleMap` 类似，但值是 `TLULParameters`，自动生成对应的 TileLink `Host2Device` 端口，用于 `external_hosts`/`external_devices`。

顶层 IO 如何用这些积木**动态生成**端口集合（注意 `external_ports` 是按配置 `Seq` 现场拼出的 `DataRecord`）：

[CoralNPUChiselSubsystem.scala:53-63](https://github.com/google-coral/coralnpu/blob/a8281b0d5701d8939cf9ea424b1cd34e18c43230/hdl/chisel/src/soc/CoralNPUChiselSubsystem.scala#L53-L63)：`external_ports` 用 `allExternalPortsConfig.map { p => ... p.name -> Input/Output(port) }` 拼出一个 `DataRecord`，其中 `port` 按 `portType` 分派为 `Clock()`/`Bool()`/`UInt(width.W)`/`gen()`。这就是 `spi_csb`、`halted`、`ext_intrs` 等端口在顶层「凭空出现」的原理。

> 💡 把 4.4 与 4.1–4.3 串起来：`SoCChiselConfig`（声明）→ `CoralNPUChiselSubsystem`（实例化+连线）→ `ExternalPort` + `SoCRecords`（端口提升）三件套，共同构成一个「加外设只改配置、装配代码零改动」的可扩展 SoC 框架。

#### 4.4.4 代码实践

**实践目标**：弄清 `enableRvv`/`enableFloat`/`enableFetchL0` 在**生产 SoC** 里的取值及后果。

**操作步骤**：
1. 在 `SoCChiselConfig.scala:131-141` 确认 `rvv_core` 的 `CoreTlulParameters` 三个开关取值。
2. 打开 `hdl/chisel/src/coralnpu/Parameters.scala`，看 `enableRvv`、`enableFloat`、`enableFetchL0` 的默认值。
3. 打开 `hdl/chisel/src/coralnpu/scalar/SCore.scala:57`（`val fetch = if (p.enableFetchL0) { Fetch(p) } else { Module(new UncachedFetch(p)) }`），理解 `enableFetchL0` 的作用。

**需要观察的现象**：
- `SoCChiselConfig` 里 `enableRvv = true`、`enableFloat = true`、`enableFetchL0 = false`。
- `Parameters` 里 `enableFetchL0` 默认是 `true`，但 SoC 配置显式覆盖成 `false`。
- 因此生产 SoC 用 `UncachedFetch`（无 L0 取指缓存）——因为指令放在单周期 ITCM 里，不需要取指缓存。

**预期结果**：生产 SoC 是「满配」核（RVV + 浮点都开），但取指不带 L0 缓存（靠 ITCM 单周期访问）。对比之下，`Parameters.scala` 注释提到的轻量目标 `core_mini_axi_sim` 则是「RVV 关、Float 开」的精简配置——同一份核代码靠这些开关裁剪出不同变体。

> 本实践为源码阅读型。取指模块的选择（`Fetch` vs `UncachedFetch`）详见 [u4-l2](u4-l2-fetch-instruction-buffer.md)。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `rvv_core` 的 `enableRvv` 改成 `false`，重新生成 RTL，核里会发生什么？

**参考答案**：装配器在 `instantiateModule` 里把 `enableRvv = false` 灌进 `core_p`，传给 `CoreTlul`→`CoreAxi`→核内部。核内部凡是 `if (p.enableRvv)` 保护的条件式例化（RVV 向量后端、向量寄存器堆、RVV 相关 CSR）都会被裁掉，生成的 RTL 面积更小，但失去向量/矩阵加速能力。这正是 CoralNPU 用同一套源码派生「满配 SoC」与「精简仿真核」的机制。

**练习 2**：`external_ports` 为什么用 `DataRecord` 而不是固定的 `Bundle` 字段？

**参考答案**：因为不同配置下要暴露的端口集合不同——加一个外设就多几个引脚，去掉一个就少几个。固定 `Bundle` 字段写死了端口集合，无法随配置增减；`DataRecord` 由配置 `Seq` 现场拼接，端口集合跟着 `SoCChiselConfig.modules.flatMap(_.externalPorts)` 动态变化，这正是「配置驱动」在 IO 层的体现。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成一张「CoralNPU SoC 顶层实例化与连线关系图」。这是本讲的主实践，建议动手画。

**任务**：

1. **列清单**：从 `SoCChiselConfig.scala` 的 `modules` 抄出全部 10 个模块的实例名，并标注每个模块的类型（host/device/两者）和它对外暴露的 `externalPorts` 数量。
2. **画总线骨架**：在图中央画一个 `CoralNPUXbar` 框；把所有 host（`coralnpu_core`、`spi2tlul`、`dma`、`ispyocto_m1`、`ispyocto_m2`、`autoboot`，外加 `enableTestHarness` 时的 `test_host_32`）画在左侧，所有 device（`coralnpu_device`、`rom`、`sram`、`uart0/1`、`spi_master`、`gpio`、`dma`、`clint`、`plic`、`ddr_ctrl`、`ddr_mem`、`ispyocto_ctrl` 等）画在右侧，用线连出 host→device 的可达关系（依据 `CrossbarConfig.scala` 的 `connections`）。
3. **标特例**：用不同颜色标出三条特例通路——
   - 中断：`clint.mtip/msip → rvv_core.timer_irq/software_irq`、`plic.irq → rvv_core.irq`；
   - DDR：`xbar → TlulWidthBridge(128→256) → TLUL2Axi → io.ddr_mem_axi`（`ddr` 时钟域）；
   - ISP：`io.ispyocto_m1_axi/m2_axi → Axi2TLUL → xbar.hosts("ispyocto_m1/m2")`（`isp_axi_clk` 域）。
4. **标开关**：在 `rvv_core` 框上注明 `enableRvv=true`、`enableFloat=true`、`enableFetchL0=false`，并写一句话解释取指为何用 `UncachedFetch`。
5. **核验**：在你的图上跟踪「外部主机写 DTCM、启动核」的路径——它应经过 `external_hosts`/`io.tl_host` → `coralnpu_core` → crossbar → `coralnpu_device` → 核内部（这条路径对应 [u2-l3](u2-l3-run-on-verilator.md)/[u2-l4](u2-l4-cocotb-testbench-intro.md) 里主机经 AXI slave 写 TCM 的行为）。

**预期成果**：一张能回答「CoralNPU SoC 装了哪些模块、谁连谁、哪些是特例、核的关键开关是什么」的关系图。画完后，你应当能不查源码就说出：新增一个 TL-UL 外设要改 `SoCChiselConfig` 的哪几处（见 4.1.5 练习 1）。

> 本实践为纯设计/阅读型，不依赖运行命令。若想验证图的正确性，可选地在本地用 Bazel 生成顶层 SystemVerilog（命令见 `hdl/chisel/src/soc/BUILD`，耗时较长），在产物 `.sv` 里搜索实例名核对你的连线。

## 6. 本讲小结

- `CoralNPUChiselSubsystem` 是一台**配置驱动的 SoC 装配器**：它读 `SoCChiselConfig` 这张「物料清单」，用循环 + Scala 反射自动实例化和连线，而不是手写一堆 `Module()` + `<>`。
- `SoCChiselConfig` 是 Chisel 侧 SoC 的**单一真相源**：`modules` 序列声明了全部 10 个模块（核、SPI、GPIO、DMA、CLINT、PLIC、SRAM…）及其参数、总线连接、对外端口。
- 装配器用 `populatePorts` 把每个模块的 IO 树拍平成 `"实例名.io.端口路径" -> Data` 的电话簿 `modulePorts`，从而支持「按字符串名字」查端口接线。
- 连线分两类：**通用循环**（`hostConnections`/`deviceConnections`/`externalPorts`）处理同域 TL-UL 模块；**手工特例**处理 CLINT/PLIC 中断、DDR（TL-UL→AXI+宽度桥）、ISP（外部 AXI→TL-UL）。
- `enableRvv`/`enableFloat`/`enableFetchL0` 等开关从 `CoreTlulParameters` 流进 `Parameters`，决定核是否例化向量后端、浮点单元、L0 取指缓存；生产 SoC 是「RVV+浮点满配、取指不带 L0」。
- `ExternalPort` + `SoCRecords`（`DataRecord`/`TLBundleMap`/`ClockResetBundle`）让顶层 IO 能随配置**动态生成**端口集合，使「加外设只改配置、装配代码零改动」成为可能。

## 7. 下一步学习建议

本讲只讲了 SoC 顶层「怎么装配」，还没有深入**总线本身**和**对外接口**。建议按以下顺序继续：

1. **[u3-l2 AXI 接口与外部系统集成](u3-l2-axi-integration.md)**：本讲出现的 `io.ddr_mem_axi`、`io.ispyocto_m1_axi` 都是 AXI4 端口，下一讲拆解 CoralNPU 作为 AXI 外设的 `s_axi`/`m_axi` 信号语义和 `CoreAxi` 顶层。
2. **[u3-l3 TileLink-UL 与 AXI 桥接](u3-l3-tlul-axi-bridge.md)**：本讲反复出现的 `Axi2TLUL`/`TLUL2Axi`/`TlulWidthBridge` 是怎么做协议和宽度转换的，这一讲逐通道讲清楚。
3. **[u3-l4 总线互联 Crossbar 与 Socket](u3-l4-crossbar-fabric.md)**：本讲把 `CoralNPUXbar` 当作黑盒调用，下一讲打开它，看 `TlulSocket1N`/`TlulSocketM1` 如何完成多主多从的路由与仲裁。
4. 想看 SoC 顶层最终生成长什么样，可直接读 `CoralNPUChiselSubsystemEmitter`（`CoralNPUChiselSubsystem.scala` 文件末尾），理解 `--itcmSizeKBytes`/`--dtcmSizeKBytes`/`--enableTestHarness` 参数如何驱动 RTL 生成。
