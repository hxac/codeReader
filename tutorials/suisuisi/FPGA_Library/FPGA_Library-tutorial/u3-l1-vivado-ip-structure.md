# Vivado 自定义 IP 结构与打包流程

## 1. 本讲目标

本讲是 Unit 3「AXI-Lite IP 封装与软硬件协同」的第一讲。在 Unit 2 中，我们把 AES 加密核心的数据通路（`hdl/src/` 下的 RTL）从算法层面读了一遍。本讲要回答一个新问题：

> 这堆 Verilog 文件，怎么才能变成 Vivado 里那个可以拖进块设计（Block Design）、能被处理器（如 Zynq 的 ARM 核）通过寄存器读写控制的「IP 方块」？

学完本讲，你应当能够：

- 说出 Vivado 自定义 IP 的 `ip_repo` 目录里每个子目录（`hdl` / `drivers` / `xgui` / `bd`）的职责。
- 看懂 `component.xml` 这个 XML 清单如何用一个 VLNV 名称 + 总线接口 + 文件集合（fileSet）来完整描述一个 IP。
- 看懂顶层包装模块 `AesCryptoCore_v1_0.v` 如何把 AXI 端口扁平地暴露出来、再例化两个 AXI 从机子模块，并理解为什么 AES 算法核心「还没接进去」。
- 理解 `xgui/*.tcl` 脚本如何生成 IP 的参数化定制界面（XGUI），把图形界面上的参数传递到 Verilog 的 `parameter`。

本讲**只讲封装骨架**，不深入 AXI 握手时序（留给 u3-l2）、中断寄存器（留给 u3-l3）、C 驱动（留给 u3-l4）。

## 2. 前置知识

在开始前，用通俗语言澄清几个概念。

- **IP 核（IP Core）**：可复用的硬件模块，相当于硬件世界的「库」。Vivado 自带很多 IP（如 FIFO、BRAM），也允许你把自己的 Verilog 打包成自定义 IP。
- **IP Catalog / ip_repo**：Vivado 管理 IP 的「应用商店」。你把一个目录指定为 IP 仓库（repository），Vivado 就会扫描其中的 `component.xml`，把这个 IP 列出来供拖拽使用。
- **AXI 接口**：ARM 设计的总线协议，是 Zynq 等 SoC 上处理器与外设通信的标准。本讲只需要知道：这个 AES IP 对外暴露了**两条 AXI 总线**——一条用于读写寄存器（`S00_AXI`），一条用于中断管理（`S_AXI_INTR`）。握手细节后续讲义再讲。
- **SPIRIT / IP-XACT**：一种 IEEE/XML 标准，用于描述电子元件（含 IP）的元数据。Vivado 的 `component.xml` 就是按这个标准写的，所以你会看到大量 `spirit:` 前缀的标签。
- **Tcl**：Vivado 内置的脚本语言。Vivado 几乎所有自动化操作（建工程、生成 GUI、生成驱动头文件）都用 Tcl 完成。
- **VLNV**：Vendor : Library : Name : Version 的缩写，是 IP-XACT 里给 IP 命名的四元组，相当于 IP 的「身份证号」。

> 承接：本讲建立在 u1-l3（Vivado 工程模板，知道了 `ip_repo/` 是放自定义 IP 的目录）和 u2-l1（AES 顶层架构，知道了 `hdl/src/aes_top.v` 是算法 RTL）之上。本讲的主角是 `ip_repo/` 里的**包装层**，与 `hdl/src/` 的**算法层**是分离的两套文件——这是理解全仓库的关键分界线。

## 3. 本讲源码地图

本讲聚焦的目录是 `HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/`。先把它的内部结构画出来：

```
ip_repo/AesCryptoCore_1.0/
├── component.xml                 # 【身份证】IP 元数据清单（IP-XACT XML）
├── bd/
│   └── bd.tcl                    # 【集成脚本】块设计中 AXI 参数自动传播
├── hdl/                          # 【硬件】RTL 包装层（注意：不是算法层）
│   ├── AesCryptoCore_v1_0.v            # 顶层包装（本讲主角）
│   ├── AesCryptoCore_v1_0_S00_AXI.v    # AXI4-Lite 寄存器从机（u3-l2 主角）
│   └── AesCryptoCore_v1_0_S_AXI_INTR.v # AXI 中断从机（u3-l3 主角）
├── drivers/AesCryptoCore_v1_0/   # 【软件】裸机 C 驱动（u3-l4 主角）
│   ├── data/AesCryptoCore.mdd          # 驱动描述文件
│   ├── data/AesCryptoCore.tcl          # 生成 xparameters.h 的脚本
│   └── src/                            # AesCryptoCore.h/.c/selftest.c + Makefile
└── xgui/
    └── AesCryptoCore_v1_0.tcl    # 【界面】参数化定制 GUI 布局（本讲主角）
```

本讲精读的三个文件：

| 文件 | 角色 | 本讲关注点 |
|------|------|-----------|
| `component.xml` | IP 的元数据清单 | VLNV、总线接口、视图与文件集合 |
| `hdl/AesCryptoCore_v1_0.v` | 顶层包装 | 端口分组、两个 AXI 子模块例化、用户逻辑空位 |
| `xgui/AesCryptoCore_v1_0.tcl` | GUI 布局脚本 | `init_gui` / `update_PARAM_VALUE` / `update_MODELPARAM_VALUE` |

辅助理解的文件（简要提及）：`bd/bd.tcl`（块设计集成）、`drivers/.../data/*.tcl` 与 `*.mdd`（驱动生成）。

## 4. 核心概念与源码讲解

### 4.1 component.xml：IP 的元数据清单（身份证）

#### 4.1.1 概念说明

`component.xml` 是一个遵循 **IP-XART / SPIRIT 1685-2009** 标准的 XML 文件，它是 Vivado 识别一个 IP 的唯一入口。你可以把它理解成 IP 的「身份证 + 说明书」：

- **身份证**：用一个四元组 VLNV（Vendor : Library : Name : Version）唯一标识这个 IP。
- **说明书**：声明这个 IP 对外提供哪些总线接口、需要哪些源文件、有哪些可配置参数、支持哪些 FPGA 家族。

Vivado 在加载 IP 仓库时，扫描每个子目录的 `component.xml`，读到 VLNV 后就把该 IP 登记进 IP Catalog。没有 `component.xml` 的目录对 Vivado 而言「不存在」。

#### 4.1.2 核心流程

Vivado 使用一个自定义 IP 的完整生命周期，每一步都依赖 `component.xml` 的不同区段：

1. **登记**：把 `ip_repo` 加入仓库 → Vivado 扫描 `component.xml` 的根 `<spirit:vendor/library/name/version>` → 在 Catalog 列出 IP。
2. **展示接口**：用户拖入块设计 → Vivado 读 `<busInterfaces>`，知道这个 IP 有哪些 AXI / 时钟 / 复位接口可以连线。
3. **参数化**：双击 IP → Vivado 读 `<parameters>` + 调用 `xgui/*.tcl`（见 4.3）→ 弹出参数配置界面。
4. **综合 / 仿真**：Vivado 读 `<model>/<views>`，根据当前是「综合」还是「仿真」选择对应 `<fileSet>`，编译其中的源文件。
5. **地址分配**：Vivado 读 `<memoryMaps>`，知道每个 AXI 从机要占用多大地址窗口（本例为 4 KB）。
6. **导出硬件 / 软件**：Vivado 读 `xilinx_softwaredriver_view_fileset`，把 C 驱动复制给 Vitis/SDK，并生成 `xparameters.h`（基地址等宏）。
7. **块设计自动化**：IP 被放进块设计时，Vivado 运行 `bd/bd.tcl`，在 AXI 总线间自动传播 `ID_WIDTH` 等标准参数。

#### 4.1.3 源码精读

**(a) VLNV —— IP 的身份证号**

[component.xml:1-6](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/component.xml#L1-L6) 声明了 XML 命名空间（`spirit` 即 SPIRIT 标准）和四元组：

```xml
<spirit:vendor>xilinx.com</spirit:vendor>
<spirit:library>user</spirit:library>
<spirit:name>AesCryptoCore</spirit:name>
<spirit:version>1.0</spirit:version>
```

所以这个 IP 的全名是 `xilinx.com:user:AesCryptoCore:1.0`。`user` 这个 library 是 Vivado「创建并打包 IP」向导默认填的，表示用户自创 IP（区别于 `xilinx.com:ip:...` 官方 IP）。

**(b) 总线接口 busInterfaces —— IP 对外的「插座」**

[component.xml:7-478](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/component.xml#L7-L478) 一口气声明了 **7 个总线接口**：

| 接口名 | 类型 | 角色 | 关键信息 |
|--------|------|------|---------|
| `S00_AXI` | aximm（AXI 内存映射） | slave | 寄存器读写口，20 个 portMap（AW/W/B/AR/R 五通道全套），数据宽 32、寄存器数 4 |
| `S_AXI_INTR` | aximm | slave | 中断寄存器口，数据宽 32、寄存器数 5 |
| `IRQ` | interrupt（信号） | master | 中断输出，`SENSITIVITY=LEVEL_HIGH`（高电平有效）|
| `S00_AXI_RST` | reset（信号） | slave | `s00_axi_aresetn`，`POLARITY=ACTIVE_LOW`（低有效复位）|
| `S00_AXI_CLK` | clock（信号） | slave | `s00_axi_aclk`，关联总线 `S00_AXI` |
| `S_AXI_INTR_RST` | reset | slave | 中断域复位，低有效 |
| `S_AXI_INTR_CLK` | clock | slave | 中断域时钟，关联总线 `S_AXI_INTR` |

以 `S00_AXI` 为例，[component.xml:8-183](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/component.xml#L8-L183) 用 `<spirit:portMap>` 把 **逻辑名**（如 `AWADDR`，AXI 标准名）映射到 **物理端口名**（如 `s00_axi_awaddr`，Verilog 里实际的端口名）。这种「逻辑↔物理」映射是 IP-XART 的核心思想：它让 Vivado 知道哪几根 Verilog 线属于同一条 AXI 总线，从而在块设计里把它们打包成一根「粗线」自动连。

[component.xml:184-205](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/component.xml#L184-L205) 是中断接口 `IRQ`，注意它是 `master`（IP 是中断的发出方），且参数 `SENSITIVITY=LEVEL_HIGH`。

**(c) 内存映射 memoryMaps —— 地址窗口**

[component.xml:479-520](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/component.xml#L479-L520) 为两个 AXI 从机各声明一个地址块，`range=4096`（即 4 KB），并通过 `OFFSET_BASE_PARAM`/`OFFSET_HIGH_PARAM` 把基地址绑定到参数 `C_S00_AXI_BASEADDR` / `C_S00_AXI_HIGHADDR`。注意 4 KB 是系统分配给该 IP 的**地址窗口大小**，并不等于寄存器数量。`S00_AXI` 的地址位宽 `C_S00_AXI_ADDR_WIDTH=4`，故实际可寻址空间：

\[
2^{4} = 16 \text{ 字节} = 4 \text{ 个 32 位寄存器}
\]

也就是说，虽然有 4 KB 窗口，但真正实现的寄存器只有 4 个（`slv_reg0`~`slv_reg3`，见 u3-l2）。

**(d) 模型视图 views —— 场景→文件集合的映射**

[component.xml:521-567](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/component.xml#L521-L567) 定义了 **5 个视图（view）**，每个视图把一种「使用场景」指到一个文件集合：

| 视图 | 用途 | 指向的 fileSet |
|------|------|---------------|
| `xilinx_verilogsynthesis` | 综合 | `..._view_fileset`（3 个 RTL）|
| `xilinx_verilogbehavioralsimulation` | 仿真 | 同上（3 个 RTL）|
| `xilinx_softwaredriver` | 软件驱动 | 驱动 fileSet（6 个 C/mdd/tcl 文件）|
| `xilinx_xpgui` | 参数化界面 | xgui tcl |
| `bd_tcl` | 块设计集成 | `bd/bd.tcl` |

所有视图的 `modelName` 都是 `AesCryptoCore_v1_0`（顶层模块名）。

**(e) 文件集合 fileSets —— 真正的源文件清单**

[component.xml:1269-1349](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/component.xml#L1269-L1349) 列出了 IP 声明的全部文件，共 **5 个 fileSet、11 个文件**：

- `xilinx_verilogsynthesis_view_fileset`（1271-1285）：`AesCryptoCore_v1_0_S00_AXI.v`、`AesCryptoCore_v1_0_S_AXI_INTR.v`、`AesCryptoCore_v1_0.v`
- `xilinx_verilogbehavioralsimulation_view_fileset`（1286-1300）：同样 3 个 RTL
- `xilinx_softwaredriver_view_fileset`（1301-1332）：`AesCryptoCore.mdd`、`AesCryptoCore.tcl`、`Makefile`、`AesCryptoCore.h`、`AesCryptoCore.c`、`AesCryptoCore_selftest.c`
- `xilinx_xpgui_view_fileset`（1333-1341）：`xgui/AesCryptoCore_v1_0.tcl`
- `bd_tcl_view_fileset`（1342-1348）：`bd/bd.tcl`

> 关键观察：这里的 RTL 文件集合**只包含 `hdl/` 下的 3 个 AXI 包装文件**，**不包含** `hdl/src/` 下的任何 AES 算法 RTL（如 `aes_top.v`）。这印证了一个重要事实：当前 IP 只是一个 AXI 外设**骨架**，AES 算法核心尚未被接进 IP（详见 4.2.3 的「Add user logic here」空位）。

**(f) 厂商扩展 vendorExtensions —— 支持家族与来源信息**

[component.xml:1487-1502](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/component.xml#L1487-L1502) 给出几条很有信息量的元数据：

- `<xilinx:supportedFamilies>` = `zynq`（Pre-Production 生命周期）→ 该 IP 面向 Zynq 系列。
- `<xilinx:taxonomies>` = `AXI_Peripheral` → 在 IP Catalog 里归到「AXI 外设」分类。
- `<xilinx:coreCreationDateTime>` = `2019-05-08T15:22:19Z` → 创建时间。
- `<xilinx:xilinxVersion>` = `2018.2` → 用 Vivado 2018.2 打包。

这与 u1-l3 中 `create_project.tcl` 指向 Zynq-7020 / Zybo Z7-20 的目标是吻合的。

#### 4.1.4 代码实践

> **实践目标**：亲手从 `component.xml` 提取 IP 的「身份三件套」——源文件、总线接口、版本号，验证你对清单结构 的理解。

**操作步骤**：

1. 打开 [component.xml](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/component.xml)。
2. 在文件中搜索 `<spirit:file>` 标签，记录每个文件所属的 fileSet 名与用途。
3. 搜索 `<spirit:busInterface>` 标签，数出接口总数，并标注每个的 busType（`aximm` / `interrupt` / `reset` / `clock`）。
4. 读取根标签下的 `<spirit:version>`。

**需要观察的现象**：

- 你会看到 5 个 fileSet，其中两个 RTL fileSet 内容完全相同（综合与仿真各一份）。
- 驱动 fileSet 里混有 `.mdd`、`.tcl`、`.c`、`.h`、`Makefile` 五类文件——它们是「软件侧」的全部资产。
- RTL fileSet 里**没有**任何 `aes_*.v` 文件。

**预期结果**（参考答案）：见 4.1.5。

#### 4.1.5 小练习与答案

**练习 1**：这个 IP 的 VLNV 全名是什么？它在 IP Catalog 的哪个分类下？

**答案**：`xilinx.com:user:AesCryptoCore:1.0`；分类为 `AXI_Peripheral`（见 [component.xml:1493](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/component.xml#L1493)）。

**练习 2**：`S00_AXI` 接口一共有多少个 `portMap`？它们覆盖了 AXI 的哪几个通道？

**答案**：20 个 `portMap`，覆盖 AXI 的写地址（AW）、写数据（W）、写响应（B）、读地址（AR）、读数据（R）全部五个通道，每通道 4 个信号（`AWADDR/AWPROT/AWVALID/AWREADY` 等）。这正是 AXI4-Lite 的完整信号集（见 [component.xml:15-167](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/component.xml#L15-L167)）。

**练习 3**：为什么综合视图和仿真视图指向同一组 RTL 文件，却要分成两个 fileSet？

**答案**：IP-XART 允许综合与仿真使用不同文件（例如仿真可用带延迟的 behavioral 模型、综合用可综合 RTL）。本 IP 二者一致，但分两个 fileSet 是标准做法，方便将来为仿真单独提供 testbench-friendly 模型。

---

### 4.2 AesCryptoCore_v1_0：顶层包装与两个 AXI 子模块

#### 4.2.1 概念说明

当你在 Vivado 里执行「Tools → Create and Package New IP → AXI4 Peripheral」时，向导会自动生成一个**顶层包装模块**（本例即 `AesCryptoCore_v1_0.v`）。它的职责很纯粹：

- 把所有 AXI 信号**扁平地**暴露在顶层端口上（这样 `component.xml` 的 portMap 才能引用它们）。
- 在内部**例化一个或多个 AXI 从机子模块**，把信号分组接进去。
- 留出一段 `// Add user logic here` 注释区，让用户把自己的算法核心（本例是 AES）接线进去。

这种「包装层 / 用户逻辑分离」的设计让 AXI 这套复杂的总线协议代码（由向导生成、经过验证）与你自己的业务逻辑互不干扰。

> 重要事实：在本仓库中，`// Add user logic here` 区域是**空的**，AES 算法核心（`hdl/src/aes_top.v`）**并没有被例化进 IP**。也就是说，这个 IP 目前只是「一个能被处理器读写 4 个寄存器 + 产生 1 个中断」的空壳，还没接上真正的加密运算。这是一个需要诚实指出的现状——IP 封装层与算法层尚未打通。

#### 4.2.2 核心流程

顶层包装的工作流程可以用下面的伪代码描述：

```
module AesCryptoCore_v1_0(全部 AXI 端口扁平展开):
    例化 S00_AXI 子模块:
        把 s00_axi_* 端口连进去   // 处理器读写 4 个寄存器
    例化 S_AXI_INTR 子模块:
        把 s_axi_intr_* 端口连进去 // 处理器配置/确认中断
        连出 irq 输出
    // Add user logic here:
    //   理应在这里把 slv_reg0..3 接到 aes_top 的输入/输出，
    //   并把 aes_done 信号接到中断子模块的中断源 —— 但目前为空
```

#### 4.2.3 源码精读

**(a) 参数列表**

[AesCryptoCore_v1_0.v:4-24](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0.v#L4-L24) 定义了一组 `parameter`，与 `component.xml` 的 modelParameters 一一对应：

```verilog
parameter integer C_S00_AXI_DATA_WIDTH = 32,
parameter integer C_S00_AXI_ADDR_WIDTH = 4,
parameter integer C_S_AXI_INTR_DATA_WIDTH = 32,
parameter integer C_S_AXI_INTR_ADDR_WIDTH = 5,
parameter integer C_NUM_OF_INTR    = 1,
parameter  C_INTR_SENSITIVITY     = 32'hFFFFFFFF,
parameter  C_INTR_ACTIVE_STATE    = 32'hFFFFFFFF,
parameter integer C_IRQ_SENSITIVITY    = 1,
parameter integer C_IRQ_ACTIVE_STATE   = 1
```

注意 `C_S00_AXI_ADDR_WIDTH=4`（4 位地址 → 16 字节 → 4 个寄存器），而中断接口 `C_S_AXI_INTR_ADDR_WIDTH=5`（5 位地址 → 32 字节 → 8 个寄存器槽位，用于全局使能、挂起、使能、确认等中断寄存器，详见 u3-l3）。`// Users to add parameters here` 与 `// User parameters ends` 之间的空白是向导留给用户加自定义参数的位置。

**(b) 端口列表**

[AesCryptoCore_v1_0.v:25-78](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0.v#L25-L78) 把端口分成三组：

- **S00_AXI 组**（33-53）：`s00_axi_aclk`、`s00_axi_aresetn`、`s00_axi_awaddr`…`s00_axi_rready`。
- **S_AXI_INTR 组**（55-76）：`s_axi_intr_aclk`、`s_axi_intr_aresetn`、`s_axi_intr_awaddr`…`s_axi_intr_rready`。
- **中断输出**（77）：`output wire irq`。

这些端口名正是 `component.xml` portMap 里的 `physicalPort` 名——二者必须一字不差，否则 Vivado 会报端口找不到。

**(c) 两个 AXI 子模块的例化**

[AesCryptoCore_v1_0.v:79-105](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0.v#L79-L105) 例化了**第一个 AXI 子模块**——`AesCryptoCore_v1_0_S00_AXI`（AXI4-Lite 寄存器从机）：

```verilog
AesCryptoCore_v1_0_S00_AXI # ( 
    .C_S_AXI_DATA_WIDTH(C_S00_AXI_DATA_WIDTH),
    .C_S_AXI_ADDR_WIDTH(C_S00_AXI_ADDR_WIDTH)
) AesCryptoCore_v1_0_S00_AXI_inst (
    .S_AXI_ACLK(s00_axi_aclk),
    .S_AXI_AWADDR(s00_axi_awaddr),
    ... // 把顶层 s00_axi_* 端口原样接给子模块的 S_AXI_* 端口
);
```

注意顶层的 `s00_axi_*` 前缀被「剥掉」，映射为子模块内统一的 `S_AXI_*` 命名——这是向导生成的固定命名约定。

[AesCryptoCore_v1_0.v:107-139](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0.v#L107-L139) 例化了**第二个 AXI 子模块**——`AesCryptoCore_v1_0_S_AXI_INTR`（AXI 中断从机），除了 AXI 信号外，还多传了 `C_NUM_OF_INTR`、`C_INTR_SENSITIVITY`、`C_IRQ_SENSITIVITY` 等中断专用参数，并连出 `.irq(irq)`。

**(d) 空的用户逻辑区**

[AesCryptoCore_v1_0.v:141-143](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0.v#L141-L143) 是向导留下的接入点：

```verilog
// Add user logic here

// User logic ends
```

这里本应出现 `aes_top` 的例化：把 `S00_AXI` 子模块里的 `slv_reg0~3`（密钥、明文、控制位）接到 `aes_top` 的输入，把密文回读，并把「加密完成」信号接到 `S_AXI_INTR` 的中断源。但当前为空——这是本仓库 IP 尚未完工的直接证据。结合 4.1.3 (e) 中 fileSet 不含 `aes_*.v`，可以双重确认：**封装层与算法层目前是脱节的**。

#### 4.2.4 代码实践

> **实践目标**：定位顶层包装例化的两个 AXI 子模块，并理解端口如何从顶层传递到子模块。

**操作步骤**：

1. 打开 [AesCryptoCore_v1_0.v](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0.v)。
2. 找到两处 `AesCryptoCore_v1_0_*_inst`，记录：实例名、模块名、传递了哪些 `parameter`。
3. 检查 `// Add user logic here` 与 `// User logic ends` 之间是否为空。
4. 思考：如果要把 `aes_top` 接进来，需要先在 `S00_AXI` 子模块里把哪些内部信号（提示：`slv_reg0`）暴露到顶层？这一步**只需画图、不要改源码**。

**需要观察的现象**：

- 两个子模块的端口连接都是「同名剥前缀」的纯映射，没有任何组合/时序逻辑夹在中间。
- 顶层模块里除了两条例化语句，没有别的逻辑——整个模块就是「一根转接线」。

**预期结果**：两个子模块分别是 `AesCryptoCore_v1_0_S00_AXI`（寄存器从机，参数 DATA_WIDTH=32、ADDR_WIDTH=4）和 `AesCryptoCore_v1_0_S_AXI_INTR`（中断从机，ADDR_WIDTH=5、含中断敏感度参数）；用户逻辑区为空，AES 核心未接入。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `S00_AXI` 的地址宽度是 4，而 `S_AXI_INTR` 的是 5？

**答案**：寄存器从机只需 4 个 32 位寄存器（\(2^4=16\) 字节 = 4 个寄存器），故地址宽 4；中断从机需要更多寄存器槽位（全局使能、IP 中断使能、挂起、确认等），故地址宽 5（\(2^5=32\) 字节空间）。这与 `component.xml` 里 `WIZ_NUM_REG` 分别为 4 和 5 一致（[component.xml:176](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/component.xml#L176) 与 [component.xml:374](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/component.xml#L374)）。

**练习 2**：如果要让处理器通过 `S00_AXI` 控制 AES，最低限度需要在 `// Add user logic here` 处做什么？

**答案**：（思路题，无需改码）至少需要：(1) 在 `S00_AXI` 子模块中把 `slv_reg0~3` 的内容作为 `aes_top` 的密钥/明文/控制输入；(2) 把 `aes_top` 的密文输出接回可被读的寄存器；(3) 把 `aes_top` 的「完成」信号作为 `S_AXI_INTR` 的中断源。当前这些都未实现。

---

### 4.3 xgui tcl：参数化定制界面

#### 4.3.1 概念说明

当你在块设计里双击一个 IP，会弹出一个图形化的「Customize IP」对话框，让你配置参数（如数据宽度、基地址）。这个对话框不是固定写死在 Vivado 里的，而是由 IP 自带的一个 Tcl 脚本**动态生成**的——这就是 `xgui/<IP名>.tcl`。XGUI（Xilinx GUI）的工作方式是：

- Vivado 提供一组 `ipgui::add_param`、`ipgui::add_page` 等命令，脚本用它们「画」界面。
- 界面里每个参数都对应 `component.xml` 里声明的一个 `parameter`。
- 当用户改动某个参数时，Vivado 会回调脚本里特定命名的 `proc`（过程），让你写联动/校验逻辑。

简单说：`component.xml` 声明「有哪些参数」，`xgui/*.tcl` 决定「这些参数在界面上长什么样、怎么联动」。

#### 4.3.2 核心流程

XGUI 脚本里有三类 proc，命名都遵循固定约定：

1. **`init_gui { IPINST }`**：打开对话框时调用一次，负责画出页面、控件、提示文字（tooltip）。
2. **`update_PARAM_VALUE.X { ... }` / `validate_PARAM_VALUE.X { ... }`**：当参数 `X` 改变时被回调。`update` 用于联动其它参数，`validate` 返回 `true/false` 决定是否接受新值。
3. **`update_MODELPARAM_VALUE.X { ... }`**：把 GUI 参数 `X` 的值「灌」进 Verilog 的 model parameter（即真正的 `parameter`），完成「界面→RTL」的最终传递。

一次参数修改的数据流：

```
用户在 GUI 改参数 X
   → validate_PARAM_VALUE.X 校验
   → update_PARAM_VALUE.X 联动
   → 点 OK
   → update_MODELPARAM_VALUE.X 把值写入 MODELPARAM_VALUE.X
   → Vivado 用该值重新例化顶层 Verilog 的 parameter X
```

#### 4.3.3 源码精读

**(a) init_gui —— 画界面**

[AesCryptoCore_v1_0.tcl:2-30](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/xgui/AesCryptoCore_v1_0.tcl#L2-L30) 是界面绘制主体：

```tcl
proc init_gui { IPINST } {
  ipgui::add_param $IPINST -name "Component_Name"
  set Page_0 [ipgui::add_page $IPINST -name "Page 0"]
  set C_S00_AXI_DATA_WIDTH [ipgui::add_param $IPINST -name "C_S00_AXI_DATA_WIDTH" \
                              -parent ${Page_0} -widget comboBox]
  set_property tooltip {Width of S_AXI data bus} ${C_S00_AXI_DATA_WIDTH}
  ...
}
```

它先加 `Component_Name`（IP 实例名），再建一个「Page 0」页，把 `C_S00_AXI_DATA_WIDTH` 等参数作为控件放到该页上。`comboBox` 控件意味着该参数只能从下拉列表选（列表内容对应 `component.xml` 里的 `choice_list_ea018de4`，即只有 32 一个选项，见 [component.xml:1259-1262](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/component.xml#L1259-L1262)）。

> 诚实提示（文档笔误）：[AesCryptoCore_v1_0.tcl:21](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/xgui/AesCryptoCore_v1_0.tcl#L21) 的 tooltip 文字写的是 `0 - LEVEL_LOW, 1 - LEVEL_LOW`，明显是复制粘贴错误，正确应为 `0 - LEVEL_LOW, 1 - LEVEL_HIGH`。同样的笔误也出现在第 25 行（IRQ）。这与前几讲指出的「仓库为草稿级、需批判阅读」一脉相承，以 `component.xml` 与仿真实际行为为准。

**(b) update / validate proc —— 联动与校验（多为空壳）**

[AesCryptoCore_v1_0.tcl:32-147](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/xgui/AesCryptoCore_v1_0.tcl#L32-L147) 为每个参数都生成了 `update_PARAM_VALUE.X` 和 `validate_PARAM_VALUE.X`，但**函数体基本是空的**，`validate` 一律 `return true`。这说明该 IP 的参数之间没有联动约束（例如改地址宽度不会自动改寄存器数）。如果将来要让 `C_NUM_OF_INTR` 改变时自动调整中断敏感度位宽，就在对应的 `update_PARAM_VALUE.C_NUM_OF_INTR` 里写逻辑。

**(c) update_MODELPARAM_VALUE proc —— 灌入 RTL 参数**

[AesCryptoCore_v1_0.tcl:150-193](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/xgui/AesCryptoCore_v1_0.tcl#L150-L193) 完成界面→RTL 的传递。以数据宽度为例（150-153）：

```tcl
proc update_MODELPARAM_VALUE.C_S00_AXI_DATA_WIDTH { MODELPARAM_VALUE.C_S00_AXI_DATA_WIDTH PARAM_VALUE.C_S00_AXI_DATA_WIDTH } {
    set_property value [get_property value ${PARAM_VALUE.C_S00_AXI_DATA_WIDTH}] ${MODELPARAM_VALUE.C_S00_AXI_DATA_WIDTH}
}
```

含义：把 GUI 参数（`PARAM_VALUE.C_S00_AXI_DATA_WIDTH`）的当前值，赋给 model 参数（`MODELPARAM_VALUE.C_S00_AXI_DATA_WIDTH`），后者就是 `component.xml` 中 [component.xml:1202-1207](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/component.xml#L1202-L1207) 声明的同名 `modelParameter`，最终落到 `AesCryptoCore_v1_0.v` 的 `parameter integer C_S00_AXI_DATA_WIDTH`。

#### 4.3.4 代码实践

> **实践目标**：追踪一个参数从 GUI 控件一路到 Verilog `parameter` 的完整链路，理解 XGUI 与 `component.xml`、`.v` 三者的对应关系。

**操作步骤**（源码阅读型，无需 Vivado）：

1. 在 [component.xml](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/component.xml) 中找到 `C_S00_AXI_DATA_WIDTH` 的三处出现：`<spirit:parameter>`（用户可见参数）、`<spirit:modelParameter>`（RTL 参数）、`<spirit:choice>`（可选值列表）。
2. 在 [xgui tcl](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/xgui/AesCryptoCore_v1_0.tcl) 中找到该参数的 `init_gui` 控件、`validate`、`update_MODELPARAM_VALUE`。
3. 在 [AesCryptoCore_v1_0.v](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0.v) 中找到同名 `parameter`。
4. 在纸上画出数据流：`GUI 控件 → PARAM_VALUE → update_MODELPARAM_VALUE → MODELPARAM_VALUE → Verilog parameter → 子模块例化`。

**需要观察的现象**：

- 同一个名字 `C_S00_AXI_DATA_WIDTH` 在三个文件里反复出现，分别是「界面层」「清单层」「RTL 层」的同一个参数。
- `comboBox` 的可选值来自 `choice_list_ea018de4`，里面只有一个 `32`——所以这个参数实际上**不可改**（无其它选项）。

**预期结果**：你能画出一条贯通三个文件的参数链；并理解为何该 IP 的参数化能力很有限（多数参数要么禁用、要么只有一个选项）。

**待本地验证**：若有 Vivado 环境，可把 `ip_repo` 加入 IP Catalog，双击 IP 观察实际 GUI 是否与 `init_gui` 描述一致（Page 0、各参数 tooltip）。

#### 4.3.5 小练习与答案

**练习 1**：`init_gui` 里的 `comboBox` 控件和普通 `add_param` 有什么区别？它的可选项从哪来？

**答案**：`comboBox` 是下拉框，限定用户只能选预设值；普通 `add_param` 是文本框。可选项来自 `component.xml` 的 `<spirit:choices>`，通过 `component.xml` 参数的 `choiceRef` 属性关联（见 [component.xml:1356](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/component.xml#L1356) 的 `choiceRef="choice_list_ea018de4"`）。

**练习 2**：`PARAM_VALUE.X` 和 `MODELPARAM_VALUE.X` 有何区别？为什么要分两个？

**答案**：`PARAM_VALUE.X` 是「用户在 GUI 看到/编辑的参数」，`MODELPARAM_VALUE.X` 是「最终传给 Verilog 模块的 model parameter」。分开是为了解耦：用户参数可以经过校验/联动/换算后再决定写入 model 参数的值。本 IP 二者直接相等，但复杂 IP 可在此做单位换算、范围裁剪等。

## 5. 综合实践

**任务**：为 `AesCryptoCore` IP 制作一张「IP 解剖图」，把本讲三个文件的信息汇总到一张表/图里，并回答一个关键判断题。

要求：

1. 列出该 IP 的 VLNV、支持家族、打包用的 Vivado 版本（来自 `component.xml` 的 vendorExtensions）。
2. 列出全部 7 个总线接口及其类型/角色。
3. 列出顶层 `AesCryptoCore_v1_0.v` 例化的两个 AXI 子模块名 + 各自地址宽度。
4. **判断题**：当前这个 IP 能不能真正完成一次 AES 加密？用本讲找到的两条证据支撑你的结论（提示：看 fileSet 里的源文件 + 看 `// Add user logic here`）。

**参考结论**：

- VLNV `xilinx.com:user:AesCryptoCore:1.0`，家族 `zynq`，Vivado 2018.2（[component.xml:1490-1500](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/component.xml#L1490-L1500)）。
- 7 个接口见表 4.1.3 (b)。
- 两个子模块：`AesCryptoCore_v1_0_S00_AXI`（ADDR_WIDTH=4）、`AesCryptoCore_v1_0_S_AXI_INTR`（ADDR_WIDTH=5）。
- **不能**完成 AES 加密。证据一：`component.xml` 的 RTL fileSet 只含 3 个 AXI 包装文件，不含任何 `aes_*.v` 算法源（[component.xml:1271-1285](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/component.xml#L1271-L1285)）；证据二：顶层 `// Add user logic here` 区为空，未例化 `aes_top`（[AesCryptoCore_v1_0.v:141-143](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/ip_repo/AesCryptoCore_1.0/hdl/AesCryptoCore_v1_0.v#L141-L143)）。当前 IP 只是一个 AXI 外设骨架。

## 6. 本讲小结

- Vivado 自定义 IP 以 `ip_repo/<IP名>/` 为单位，核心是 `component.xml`（身份证/清单），辅以 `hdl`（RTL 包装）、`drivers`（C 驱动）、`xgui`（GUI 脚本）、`bd`（块设计脚本）。
- `component.xml` 按 IP-XACT/SPIRIT 标准描述了 VLNV、7 个总线接口、2 个内存映射、5 个视图与 5 个 fileSet（共 11 个文件），是 Vivado 识别和使用 IP 的唯一依据。
- 顶层 `AesCryptoCore_v1_0.v` 是纯包装层：扁平暴露 AXI 端口，例化 `S00_AXI`（寄存器从机，4 位地址）和 `S_AXI_INTR`（中断从机，5 位地址）两个子模块，端口用「剥前缀」方式映射。
- 关键现状：`// Add user logic here` 区为空、fileSet 不含 `aes_*.v`，说明 AES 算法核心**尚未接入** IP，当前只是 AXI 外设骨架——封装层（`ip_repo/.../hdl/`）与算法层（`hdl/src/`）目前脱节。
- `xgui/*.tcl` 用 `init_gui` 画界面、`update/validate_PARAM_VALUE` 做联动校验、`update_MODELPARAM_VALUE` 把 GUI 参数灌进 Verilog `parameter`；脚本中存在 LEVEL_LOW 复制粘贴笔误，需批判阅读。
- 该 IP 用 Vivado 2018.2 于 2019-05-08 创建，面向 Zynq 家族，归在 `AXI_Peripheral` 分类。

## 7. 下一步学习建议

本讲只看了 IP 的「外壳」。接下来建议：

1. **u3-l2（AXI4-Lite 从机接口实现）**：打开 `AesCryptoCore_v1_0_S00_AXI.v`，深入五通道握手时序与 `slv_reg0~3` 的读写实现——这是处理器控制 IP 的真正入口。
2. **u3-l3（AXI 中断接口）**：打开 `AesCryptoCore_v1_0_S_AXI_INTR.v`，理解中断使能/挂起/确认寄存器与 `irq` 的产生。
3. **u3-l4（软件驱动）**：阅读 `drivers/.../src/` 下的 C 代码，看处理器如何用基地址宏和读写 API 操控这个 IP。
4. **延伸思考**：结合本讲的「骨架未接入算法」结论，尝试在纸上设计：若要把 `aes_top`（Unit 2）接进 `S00_AXI`，应如何分配 4 个寄存器（密钥/明文/控制/密文），这会自然过渡到软硬件协同设计。

继续阅读建议：先重读本讲的 `component.xml` 与顶层包装，确保能在脑海里把「IP 的 XML 描述 ↔ RTL 包装 ↔ GUI 脚本」三者对应起来，再进入 u3-l2。
