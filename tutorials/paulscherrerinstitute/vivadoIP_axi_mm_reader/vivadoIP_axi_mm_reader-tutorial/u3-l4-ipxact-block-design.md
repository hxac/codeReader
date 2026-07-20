# IP-XACT 打包产物与 Block Design 集成

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `component.xml` 这份 **IP-XACT** 文件在整个 IP 生命周期里扮演的角色：它是 `package.tcl` 打包后产出的「总账本」，Vivado 与 Vitis 全靠它认识这个 IP。
- 读懂 `bd/bd.tcl` 里 `init`、`pre_propagate`、`propagate` 三个回调分别在 Block Design（BD）的什么时机被调用，以及它们如何自动把 AXI4 的 `ID_WIDTH` 在主/从接口之间传播。
- 理解 `drivers/axi_mm_reader/data/` 下的 `.mdd` 与 `.tcl` 如何让 Vitis 在生成 BSP 时识别这个驱动、并把 BD 里分配的地址写成 `xparameters.h` 里的 `C_BASEADDR`/`C_HIGHADDR` 等宏。
- 把「BD 地址编辑器 → `component.xml` 内存映射 → MDD/TCL → `xparameters.h` → C 驱动 `baseAddr` → 寄存器偏移」这条端到端链路串起来。

## 2. 前置知识

本讲是打包环节（[u1-l4](u1-l4-ip-packaging.md)）的延伸，不再讲 `package.tcl` 的流水线本身，而是钻进它产出的产物与集成机制。你需要先大致了解下面几个概念，本讲会用通俗语言再过一遍：

- **IP-XACT / `component.xml`**：IEEE 1685 标准（早期由 SPIRIT 联盟制定）规定的一种 XML 格式，用来「自描述」一个 IP——它对外有哪些接口、哪些参数、包含哪些源文件、支持哪些器件。Vivado 打包 IP 后，产物里最核心的就是 `component.xml`。
- **AXI4 的 ID 信号**：AXI4 总线里有一组可选的标识信号 `AWID`/`BID`/`ARID`/`RID`，宽度由参数 `ID_WIDTH` 决定。主机（master）发出事务时打上 ID，从机（slave）原样回带，用于在乱序/并发事务里配对。读写两端必须 **ID 宽度一致** 才能合法连接。
- **Block Design（BD）与参数传播**：Vivado 里把多个 IP 用连线图拼起来的画布叫 BD。BD 有一套「自动化（automation）」机制，会在连线上自动推导并传递参数（比如把上游主机的 `ID_WIDTH` 传给下游从机）。`bd.tcl` 就是挂在这套机制上的钩子（hook）。
- **Vitis BSP / `xparameters.h` / `libxil.a`**：把 BD 导出成硬件交付件（XSA）后，Vitis 会据此生成板级支持包（BSP）。BSP 里有两个关键产物：头文件 `xparameters.h`（列出每个 IP 实例的基地址、设备号等宏）和静态库 `libxil.a`（把各外设驱动编译归档进去）。
- **VHDL generic（类属）**：综合时定死的参数，例如本 IP 的 `AxiSlaveAddrWidth_g`。在打包侧它对应 `component.xml` 里的 `MODELPARAM_VALUE`（详见 [u1-l4](u1-l4-ip-packaging.md) 与 [u3-l3](u3-l3-parameters-gui.md)）。

> 一句话定位：`component.xml` 是「这张 IP 的身份证」，`bd.tcl` 负责「IP 进了 BD 后自动调好 AXI ID 宽度」，`MDD/TCL/Makefile` 负责「IP 进了 Vitis 后被认成驱动并算出基地址」。三者合力把一个 RTL 设计变成可在 BD 里拖拽、在软件里 `#include` 调用的完整外设。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用来讲什么 |
| --- | --- | --- |
| `component.xml` | IP-XACT 打包产物（由 `package.tcl` 生成） | IP 的接口、地址空间、内存映射、参数、驱动文件清单如何在一份 XML 里自描述 |
| `bd/bd.tcl` | Block Design 钩子脚本 | `init`/`pre_propagate`/`propagate` 三个回调如何自动传播 AXI4 `ID_WIDTH` |
| `drivers/axi_mm_reader/data/axi_mm_reader.mdd` | 微处理器驱动定义（MDD） | 声明「这个外设有一个叫 `axi_mm_reader` 的驱动」 |
| `drivers/axi_mm_reader/data/axi_mm_reader.tcl` | 驱动 TCL（`generate` 过程） | BSP 生成时向 `xparameters.h` 写入哪些字段 |
| `drivers/axi_mm_reader/src/Makefile` | 驱动编译脚本 | 把 `.c` 编进 `libxil.a` 的标准 Xilinx 流程 |
| `scripts/package.tcl` | 打包流水线（[u1-l4](u1-l4-ip-packaging.md) 已讲） | 回顾它如何把驱动文件挂进 IP，产出上面的 `component.xml` |
| `xgui/axi_mm_reader_v1_0.tcl` | GUI 脚本（[u1-l4](u1-l4-ip-packaging.md)/[u3-l3](u3-l3-parameters-gui.md) 已讲） | 其中的 `update_MODELPARAM_VALUE.C_S00_AXI_ID_WIDTH` 把 BD 传进来的 `ID_WIDTH` 桥接到 RTL |
| `hdl/axi_mm_reader_wrp.vhd` | wrapper | `C_S00_AXI_ID_WIDTH` 这个 generic 如何决定 `s00_axi` 的 ID 信号宽度 |

## 4. 核心概念与源码讲解

### 4.1 component.xml：IP-XACT 组件描述（打包总账本）

#### 4.1.1 概念说明

`package.tcl` 跑完 `package_ip` 之后（见 [scripts/package.tcl:114](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/scripts/package.tcl#L114)），目标目录里会生成一份 `component.xml`。这份文件用的是 IP-XACT（SPIRIT 1685-2009）XML 模式，根元素是这样的：

[component.xml:2-6](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/component.xml#L2-L6) —— 用 `spirit:` 命名空间声明这是一个 IP-XACT 组件，并给出 `vendor=oliver.bruendler`、`library=PSI`、`name=axi_mm_reader`、`version=1.0`。这四个字段合起来就是这个 IP 在 Vivado IP 目录里的「唯一身份证号」（`VLNV`：Vendor-Library-Name-Version）。

要特别强调一个工程现实：**`component.xml` 是机器生成的产物，正常情况下不要手改它**。本讲的标题里写「内部细节待确认」，指的就是这一点——它的内容随 `package.tcl`/`xgui`/`bd.tcl`/`drivers` 的修改而重新生成，手改极易在下次打包时被覆盖或导致校验和不一致。我们要做的是「读懂它在描述什么」，而不是「直接编辑它」。要改行为，应改源头（`package.tcl`、`xgui`、wrapper 的 generic 等），再重新打包。

`component.xml` 用几个大段把一个 IP 描述完整：

- **`busInterfaces`**：对外总线接口（`s00_axi` 从机、`m00_axi` 主机、`m_axis` AXI-Stream、`Clk` 时钟）。
- **`addressSpaces`**：主机侧能寻址多大的空间（`m00_axi` 主机能读多大的地址范围）。
- **`memoryMaps`**：从机侧被别人看到的寄存器地图（`s00_axi` 暴露给软件的地址块）。
- **`model`**：顶层模块名、各种「视图（view）」对应的源文件集合（fileSet）、端口、参数。
- **`fileSets`**：按用途分组列出的所有源文件（综合、仿真、GUI、logo、数据手册、软件驱动）。
- **`vendorExtensions`**：Xilinx 扩展信息（支持器件族、taxonomy、打包工具版本、各段校验和）。

#### 4.1.2 核心流程

`component.xml` 在 IP 生命周期里被消费的流程：

1. **打包**：`package.tcl` 调 PsiIpPackage 命令，把接口/参数/文件等信息写成 `component.xml`。
2. **加入 IP 目录**：Vivado 把这份 `component.xml` 所在目录注册为自定义 IP 仓库。
3. **BD 中实例化**：用户在 BD 里拖入这个 IP，Vivado 读 `busInterfaces` 知道有哪些接口可连，读 `model.modelName` 知道顶层是 `axi_mm_reader_wrp`。
4. **地址分配**：Vivado 读 `memoryMaps`，在地址编辑器里给 `s00_axi` 分配一段地址（即 `C_BASEADDR`/`C_HIGHADDR` 的来源）。
5. **综合/实现**：读 `modelParameters`（`MODELPARAM_VALUE`）把 generic 值定死，读 `ports` 里对 `MODELPARAM_VALUE` 的依赖表达式确定每个端口的实际位宽。
6. **导出 → Vitis**：导出 XSA 时，`fileSets` 里 `xilinx_softwaredriver_view_fileset` 的驱动文件随行带出；Vitis 读 `.mdd`/`.tcl` 生成 BSP（见 4.3）。

#### 4.1.3 源码精读

**(a) 四个总线接口**

[component.xml:47-53](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/component.xml#L47-L53) —— `m_axis` 接口的启用条件。这一段把 `m_axis` 接口的 `isEnabled` 绑到表达式 `$Output_g == "AXIS"`，默认 `false`。这正是 [u1-l4](u1-l4-ip-packaging.md) 讲过的 `add_interface_enablement_condition` 在 `component.xml` 里的落点：默认 `Output_g="AXIMM"`，所以默认打包出来的 IP 不存在 `m_axis`。

[component.xml:55-61](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/component.xml#L55-L61) —— `m00_axi` 是 **AXI4 主机**（`<spirit:master/>`），并用 `<spirit:addressSpaceRef>` 引用了 `addressSpaces` 里名为 `m00_axi` 的地址空间（即它能读多大）。

[component.xml:177-183](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/component.xml#L177-L183) —— `s00_axi` 是 **AXI4 从机**（`<spirit:slave/>`），并用 `<spirit:memoryMapRef>` 引用了 `memoryMaps` 里名为 `s00_axi` 的寄存器地图（即它暴露给软件的地址块）。

> 主从在 `component.xml` 里的区别很直观：主机挂 `addressSpaceRef`（我读别人多大），从机挂 `memoryMapRef`（别人读我多大）。

**(b) 地址空间与内存映射**

[component.xml:494-500](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/component.xml#L494-L500) —— `m00_axi` 主机能寻址的范围是 `4294967296`（即 4 GiB），数据宽度 32 位。这与 wrapper 里 `m00_axi_araddr` 是 32 位一致（见 [u2-l6](u2-l6-axi-master-read.md)）。

[component.xml:501-512](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/component.xml#L501-L512) —— `s00_axi` 暴露的地址块 `reg0`：基地址 0、宽度 32、用途 `register`。最关键的是它的 `range` 是一个**依赖表达式**：

```
pow(2, (spirit:decode(id('MODELPARAM_VALUE.AxiSlaveAddrWidth_g')) - 1) + 1)
```

也就是范围等于 \(2^{\text{AxiSlaveAddrWidth\_g}}\) 字节。默认 `AxiSlaveAddrWidth_g=14`，所以默认范围是 \(2^{14}=16384\) 字节，与 `range` 标签里的初值 `16384` 对应。这正是 [u2-l2](u2-l2-register-map.md)/[u3-l3](u3-l3-parameters-gui.md) 里「s00_axi 地址宽度下限为 \(\lceil\log_2(\text{MaxRegCount}\times4+32)\rceil\)」的来源：寄存器区与 RegTable 内存区共享这一段地址空间，地址宽度变大，这块 `reg0` 的范围也跟着变大。

**(c) 端口位宽依赖参数**

端口位宽同样可以用表达式依赖某个 `MODELPARAM_VALUE`。最典型的就是受 `ID_WIDTH` 控制的 ID 信号：

[component.xml:660-679](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/component.xml#L660-L679) —— `s00_axi_arid` 端口的 `left` 索引是 `spirit:decode(id('MODELPARAM_VALUE.C_S00_AXI_ID_WIDTH')) - 1`。也就是说，这个端口的位宽（`left downto 0`）完全由 `C_S00_AXI_ID_WIDTH` 决定。`s00_axi_rid`/`s00_axi_awid`/`s00_axi_bid` 也是同样的依赖（参见 [component.xml:843-858](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/component.xml#L843-L858)、[935-954](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/component.xml#L935-L954)、[1202-1218](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/component.xml#L1202-L1218)）。**这就是 4.2 节 `bd.tcl` 传播 `ID_WIDTH` 的最终落点**——参数一变，这四个端口的硬件位宽就跟着变。

而地址类端口依赖的是另一个参数：

[component.xml:955-974](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/component.xml#L955-L974) —— `s00_axi_awaddr` 的 `left` 索引依赖 `MODELPARAM_VALUE.AxiSlaveAddrWidth_g`（默认 13，即 14 位）。

**(d) 参数与下拉选项**

[component.xml:1612-1626](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/component.xml#L1612-L1626) —— `modelParameters` 段把六个用户参数（`ClkFrequencyHz`/`TimeoutUs_g`/`MaxRegCount_g`/`MinBuffers_g`/`Output_g`/`AxiSlaveAddrWidth_g`）以及 `C_S00_AXI_ID_WIDTH` 都列为 `MODELPARAM_VALUE`，综合时这些值会定死成 wrapper 的 generic。注意 `C_S00_AXI_ID_WIDTH` 默认值是 `1`。

[component.xml:1629-1635](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/component.xml#L1629-L1635) —— `choices` 段定义了一个枚举列表 `{AXIMM, AXIS}`，`Output_g` 参数引用它（`choiceRef`），对应 GUI 里那个下拉框（[u1-l4](u1-l4-ip-packaging.md) 里 `gui_parameter_set_widget_dropdown` 的落点）。

**(e) 软件驱动文件清单**

[component.xml:1787-1809](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/component.xml#L1787-L1809) —— `xilinx_softwaredriver_view_fileset` 把驱动的五个文件都登记进来：`axi_mm_reader.mdd`（`userFileType=mdd`）、`axi_mm_reader.tcl`（`tclSource`）、`Makefile`、`axi_mm_reader.c`（`cSource`）、`axi_mm_reader.h`（`cSource`）。这一段就是 `package.tcl` 里 `add_drivers_relative` 的产物（见 [scripts/package.tcl:61-64](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/scripts/package.tcl#L61-L64)）。没有这一段，Vitis 就不知道这个 IP 还自带 C 驱动。

**(f) 支持器件族与打包信息**

[component.xml:1855-1876](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/component.xml#L1855-L1876) —— 支持器件族清单：`artix7` 标为 `Production`，其余（`zynq`、`zynquplus`、`kintex7` 等）标为 `Beta`。这与 `package.tcl` 里 `package_ip ... xc7a*`（Artix-7 匹配）对应（[u1-l4](u1-l4-ip-packaging.md)）。

[component.xml:1902-1911](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/component.xml#L1902-L1911) —— 打包信息：用 Vivado `2019.1` 打包，并给出各段（busInterfaces/memoryMaps/ports/…）的校验和。任何一段内容变化，对应校验和就会变——这也是为什么手改 `component.xml` 风险高。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：用一份阅读清单，在 `component.xml` 里把「IP 的对外身份」全部找出来，验证它与前面几讲的结论一致。

**操作步骤**：

1. 打开 `component.xml`，定位根元素的 `vendor/library/name/version`，写出这个 IP 的 VLNV。
2. 在 `busInterfaces` 里数出共有几个接口，分别写出每个接口的 `MODE`（master/slave）与总线类型（`aximm`/`axis`/`clock`）。
3. 找到 `m_axis` 接口的 `enablement`，确认它的启用依赖表达式确实是 `$Output_g == "AXIS"`。
4. 在 `memoryMaps` 里找到 `reg0` 的 `range` 表达式，代入默认 `AxiSlaveAddrWidth_g=14`，手算范围值。
5. 在 `xilinx_softwaredriver_view_fileset` 里数出驱动相关文件，确认与本讲「源码地图」列出的五个一致。

**需要观察的现象**：所有接口/地址/参数都能在 `component.xml` 里找到对应描述；位宽不是写死的数字，而是依赖 `MODELPARAM_VALUE.*` 的表达式。

**预期结果**：

- VLNV = `oliver.bruendler : PSI : axi_mm_reader : 1.0`。
- 共 4 个接口：`m_axis`（master/axis，条件启用）、`m00_axi`（master/aximm）、`s00_axi`（slave/aximm）、`Clk`（slave/clock）。
- `reg0` 范围 = \(2^{14}=16384\) 字节。
- 驱动文件 5 个：`.mdd`、`.tcl`、`Makefile`、`.c`、`.h`。

> 这些结论完全来自源文件，不需要运行 Vivado。

#### 4.1.5 小练习与答案

**练习 1**：为什么说「不要直接手改 `component.xml`」？要改 IP 行为应该改哪里？
**答案**：因为它是 `package.tcl`/`xgui`/`bd.tcl`/`drivers` 等源头的生成产物，且带各段校验和；手改会被下次打包覆盖或导致校验和不一致。正确做法是改源头（如 `package.tcl` 的参数声明、wrapper 的 generic、`bd.tcl` 的钩子），再重新运行打包。

**练习 2**：`m00_axi`（主机）和 `s00_axi`（从机）在 `component.xml` 里分别引用了 `addressSpaces` 和 `memoryMaps`，为什么方向相反？
**答案**：主机是「我去读别人」，所以声明自己能寻址多大（`addressSpaceRef`，4 GiB）；从机是「别人来读我」，所以声明自己暴露多大的一块寄存器区给软件（`memoryMapRef`，即 `reg0`）。

**练习 3**：`s00_axi_arid` 的位宽是怎么定出来的？
**答案**：它的 `left` 索引是 `MODELPARAM_VALUE.C_S00_AXI_ID_WIDTH - 1`，所以位宽 = `C_S00_AXI_ID_WIDTH`，默认 1 位。`C_S00_AXI_ID_WIDTH` 的值由 4.2 节的 BD 钩子自动填入。

---

### 4.2 bd.tcl：Block Design 中 AXI4 ID_WIDTH 的自动传播

#### 4.2.1 概念说明

当你在 BD 里把这个 IP 的 `s00_axi`（从机）连到一个 AXI Interconnect 或 Zynq PS 的主机口时，两边必须 `ID_WIDTH` 一致才能合法连接。问题是：PS 主机口的 ID 宽度可能是 12 位、Interconnect 还可能「加宽」ID 来区分多个主机——这个值用户很难手动算对。

Xilinx 的解决办法是给每个 AXI4 IP 配一份 `bd/bd.tcl`，里面定义三个固定名字的回调过程：`init`、`pre_propagate`、`propagate`。BD 自动化引擎在不同阶段回调它们，让 IP 自己参与到 `ID_WIDTH` 的自动推导里。这份脚本几乎是 Xilinx 官方模板的逐字搬运（作者署名 `Goran Marinkovic, Oliver Bruendler`，注释里还留着 `override property of bd_interface_net to bd_cell -- only for slaves` 这种模板原话），所以你会在很多 AXI4 IP 里看到几乎一样的 `bd.tcl`。

三个回调的分工（从代码逻辑读出来的方向性是确定的）：

| 回调 | 处理的接口方向 | 动作方向 | 对本 IP 是否实际生效 |
| --- | --- | --- | --- |
| `init` | slave（`S00_AXI`） | 把 `C_S00_AXI_ID_WIDTH` 标记为「只能由传播设置」 | ✅ 生效（锁住参数） |
| `pre_propagate` | master（`AXI4`） | 把本单元的 ID 宽度**往外推**到接口 | ⚠️ 对本 IP 是空跑（见 4.2.3） |
| `propagate` | slave（`AXI4`） | 把外部接口的 ID 宽度**收进来**写到单元参数 | ✅ 生效（实际填值） |

#### 4.2.2 核心流程

BD 自动化引擎处理一个含本 IP 的 BD 时，对本 IP 的 `bd.tcl` 大致按下面顺序回调（精确到拍级的调度是 Vivado 内部行为，但每个回调的职责由代码本身明确）：

1. **`init`（IP 被加入/重载 BD 时）**：遍历所有接口，找出名为 `S00_AXI` 的从机接口，把它的 `C_S00_AXI_ID_WIDTH` 标记为 `propagate_only`——意思是「这个参数只能由传播机制设置，用户不许在 GUI 里手填」。
2. **`pre_propagate`（参数传播之前）**：遍历所有 `AXI4` **主机**接口，把单元上记录的 `C_<busif>_ID_WIDTH` 推到接口外侧（给上游/互联看）。
3. **`propagate`（参数传播时/之后）**：遍历所有 `AXI4` **从机**接口，把接口外侧（来自上游主机/互联）的 `ID_WIDTH` 收回来，写到单元的 `C_<busif>_ID_WIDTH` 参数上。

对本 IP 来说，真正填值的是第 3 步：`s00_axi` 的 `C_S00_AXI_ID_WIDTH` 会被「上游主机的 ID 宽度」自动覆盖。

#### 4.2.3 源码精读

**init：锁住从机的 ID_WIDTH 参数**

[bd/bd.tcl:7-27](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/bd/bd.tcl#L7-L27) —— `init` 过程。关键三步：

- `full_sbusif_list = { S00_AXI }`：只认 `S00_AXI` 这一个从机接口。
- 遍历所有接口，过滤出 `MODE == slave` 且名字在白名单里的（即 `S00_AXI`）。
- 对它构造参数名 `C_S00_AXI_ID_WIDTH`，调用 `bd::mark_propagate_only` 把它标记为「仅传播可写」。

效果：用户在 BD 里选中这个 IP，会发现 `C_S00_AXI_ID_WIDTH` 是灰的、没法手动输入——它只能由下面的 `propagate` 自动填。

**pre_propagate：把主机的 ID 宽度往外推**

[bd/bd.tcl:30-58](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/bd/bd.tcl#L30-L58) —— `pre_propagate` 过程。它过滤 `PROTOCOL == AXI4 && MODE == master` 的接口，对 `ID_WIDTH`：

- 读接口外侧的值 `val_on_cell_intf_pin`（`CONFIG.ID_WIDTH` on busif）与单元上的值 `val_on_cell`（`CONFIG.C_<busif>_ID_WIDTH` on cell）。
- 若两者不同**且**单元值非空，则把单元值写到接口上（`set_property CONFIG.ID_WIDTH $val_on_cell $busif`，[bd/bd.tcl:53](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/bd/bd.tcl#L53)）。

> 对**本 IP** 这一段其实是空跑：唯一的 AXI4 主机是 `m00_axi`，但它在 `component.xml` 里根本没有 ID 信号（只读配置、无 `ARID`/`RID`，见 4.1.3(b) 与 [u2-l6](u2-l6-axi-master-read.md)），也没有对应的 `C_m00_axi_ID_WIDTH` 参数，于是 `val_on_cell` 为空、`if` 不成立、什么都不做。这是「模板通用、本 IP 用不到」的典型例子——保留它是因为同一份模板要兼容带 ID 的主机 IP。

**propagate：把外部 ID 宽度收进从机参数（本 IP 的实际生效路径）**

[bd/bd.tcl:61-90](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/bd/bd.tcl#L61-L90) —— `propagate` 过程。它过滤 `PROTOCOL == AXI4 && MODE == slave` 的接口（即 `s00_axi`），对 `ID_WIDTH`：

- 读接口外侧的值 `val_on_cell_intf_pin` 与单元上的值 `val_on_cell`。
- 若两者不同**且**接口外侧值非空，则把接口外侧值写到单元参数上（`set_property CONFIG.C_S00_AXI_ID_WIDTH $val_on_cell_intf_pin $cell_handle`，[bd/bd.tcl:85](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/bd/bd.tcl#L85)）。

这一步把「上游主机/Interconnect 推算出的 ID 宽度」写进本 IP 的 `C_S00_AXI_ID_WIDTH`。方向与 `pre_propagate` 相反：`pre_propagate` 是 cell → 外，`propagate` 是外 → cell。注释 `#override property of bd_interface_net to bd_cell -- only for slaves` 正是说「只对从机把外部值覆盖回单元」。

**落点 1：从 BD 参数到 GUI 模型参数**

被 `propagate` 写入的是 `PARAM_VALUE.C_S00_AXI_ID_WIDTH`。它还要再过一跳才变成 RTL 用的 `MODELPARAM_VALUE`：

[xgui/axi_mm_reader_v1_0.tcl:110-113](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/xgui/axi_mm_reader_v1_0.tcl#L110-L113) —— `update_MODELPARAM_VALUE.C_S00_AXI_ID_WIDTH` 把 `PARAM_VALUE.C_S00_AXI_ID_WIDTH` 的值拷给 `MODELPARAM_VALUE.C_S00_AXI_ID_WIDTH`。这正是 [u3-l3](u3-l3-parameters-gui.md) 讲的「GUI 值经 `update_MODELPARAM_VALUE.X` 桥接到 RTL generic」机制——只不过这里 `C_S00_AXI_ID_WIDTH` 的值不是用户在 GUI 敲的，而是 BD 自动传播进来的。

**落点 2：从模型参数到端口位宽与 wrapper generic**

`MODELPARAM_VALUE.C_S00_AXI_ID_WIDTH` 一旦确定，4.1.3(c) 里 `s00_axi_arid`/`rid`/`awid`/`bid` 的端口位宽表达式就定死了。再往下，wrapper 实体里有同名的 generic 接住它：

[hdl/axi_mm_reader_wrp.vhd:35](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L35) —— `C_S00_AXI_ID_WIDTH : integer := 1`。它被用来声明四个 ID 端口的宽度（[hdl/axi_mm_reader_wrp.vhd:55](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L55) 等），并传给内部的 `psi_common_axi_slave_ipif` 作为 `AxiIdWidth_g`（[hdl/axi_mm_reader_wrp.vhd:198](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L198)）。

至此整条链路闭合：

```
上游主机 ID 宽度
   │  bd.tcl::propagate  (外 → cell, 写 PARAM_VALUE.C_S00_AXI_ID_WIDTH)
   ▼
PARAM_VALUE.C_S00_AXI_ID_WIDTH
   │  xgui::update_MODELPARAM_VALUE.C_S00_AXI_ID_WIDTH
   ▼
MODELPARAM_VALUE.C_S00_AXI_ID_WIDTH
   │  component.xml 端口位宽表达式 + 综合时 generic 定死
   ▼
wrapper generic C_S00_AXI_ID_WIDTH  →  s00_axi_arid/rid/awid/bid 位宽  →  AxiIdWidth_g
```

#### 4.2.4 代码实践（源码阅读 + 推理型）

**实践目标**：把三个回调的方向与本 IP 的实际生效路径讲清楚，并解释 `pre_propagate` 为什么对本 IP 是空跑。

**操作步骤**：

1. 打开 `bd/bd.tcl`，在 `init` 里找到 `full_sbusif_list` 与 `bd::mark_propagate_only`，说明它锁的是哪个参数。
2. 对比 `pre_propagate`（[bd/bd.tcl:40-42](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/bd/bd.tcl#L40-L42)）与 `propagate`（[bd/bd.tcl:71-73](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/bd/bd.tcl#L71-L73)）的过滤条件，指出一个查 `master`、一个查 `slave`。
3. 对比两者的 `set_property` 写入对象：`pre_propagate` 写到 `$busif`（接口），`propagate` 写到 `$cell_handle`（单元）——印证「往外推 vs 收进来」。
4. 结合 `component.xml` 里 `m00_axi` 没有 ID 信号、wrapper 也没有 `C_m00_axi_ID_WIDTH`，解释为什么 `pre_propagate` 对本 IP 实际不改变任何东西。

**需要观察的现象**：三个回调代码结构高度对称，差异只在「master/slave」与「写到接口/写到单元」这两处。

**预期结果**：能用自己的话复述上表与那条闭合链路；能指出 `propagate` 才是本 IP 真正填 `C_S00_AXI_ID_WIDTH` 的回调，而 `pre_propagate` 因 `m00_axi` 无 ID 而空跑。

> 待本地验证：在 Vivado 里把本 IP 的 `s00_axi` 连到一个带 ID 宽度的主机（如 Zynq7 PS 的 M_AXI_GP0），观察 IP 的 `C_S00_AXI_ID_WIDTH` 是否被自动设成上游值、且 GUI 里该参数是否灰色不可编辑。

#### 4.2.5 小练习与答案

**练习 1**：`init` 里 `full_sbusif_list` 只列了 `S00_AXI`。如果某个 IP 有两个 AXI4 从机 `S00_AXI`、`S01_AXI`，要怎么改？
**答案**：把 `S01_AXI` 加进 `full_sbusif_list`（`list S00_AXI S01_AXI`），`init` 就会把 `C_S01_AXI_ID_WIDTH` 也标记为 propagate-only。`propagate` 不受影响，因为它按 `MODE==slave` 自动覆盖所有 AXI4 从机。

**练习 2**：为什么 `propagate` 里写单元参数前要判断 `val_on_cell_intf_pin != ""`？
**答案**：接口外侧的 `ID_WIDTH` 可能尚未被上游推导出来（值为空）。直接把空值写进单元参数会破坏配置，所以加了非空保护。

**练习 3**：对本 IP 而言，删掉 `pre_propagate` 过程会不会影响 `s00_axi` 的 ID 宽度？
**答案**：不会。`pre_propagate` 只处理主机接口，而本 IP 唯一的 AXI4 主机 `m00_axi` 没有 ID 信号/参数，该过程本来就空跑。真正决定 `s00_axi` ID 宽度的是 `propagate`。（但作为通用模板，保留 `pre_propagate` 更安全。）

---

### 4.3 MDD/TCL/Makefile：驱动在 Vitis BSP 中的识别与 xparameters.h 生成

#### 4.3.1 概念说明

光有 C 源码（`axi_mm_reader.c/.h`）还不够，Vitis 还得知道「这个外设该绑哪个驱动、驱动实例的基地址是多少」。这套信息由驱动目录 `data/` 下的两个文件提供：

- **`.mdd`（Microprocessor Driver Definition）**：声明「存在一个驱动叫 `axi_mm_reader`，它支持外设 `axi_mm_reader`，版本 1.0」。Vitis 的 BSP 生成工具（libgen）扫到外设 `axi_mm_reader` 时，就靠这条声明找到对应驱动。
- **`.tcl`（驱动 TCL）**：提供 `generate` 过程，BSP 生成时被回调，用来向 `xparameters.h` 写入这个外设实例的宏。

再加上 `src/Makefile`，把 `.c` 编进 `libxil.a`，驱动就能被应用代码链接使用了。这三个文件都在 `component.xml` 的 `xilinx_softwaredriver_view_fileset` 里登记过（见 4.1.3(e)），所以会随 XSA 一起导出给 Vitis。

#### 4.3.2 核心流程

Vitis 生成 BSP 时，针对这个外设大致经历：

1. **识别驱动**：读 `.mdd`，得知外设 `axi_mm_reader` 对应驱动 `axi_mm_reader`。
2. **生成参数宏**：调用 `.tcl` 的 `generate`，由 `xdefine_include_file` 向 `xparameters.h` 写入 `NUM_INSTANCES`/`DEVICE_ID`/`C_BASEADDR`/`C_HIGHADDR` 四类宏。其中 `C_BASEADDR`/`C_HIGHADDR` 的值来自 BD 地址编辑器给 `s00_axi` 的 `reg0` 分配的地址范围。
3. **编译归档**：执行 `Makefile`，把 `axi_mm_reader.c` 编译成 `.o` 并归档进 `libxil.a`。
4. **应用调用**：应用代码 `#include <xparameters.h>` 与 `axi_mm_reader.h`，把 `XPAR_AXI_MM_READER_0_BASEADDR` 之类作为 `baseAddr` 传给驱动 API。

#### 4.3.3 源码精读

**MDD：驱动声明**

[drivers/axi_mm_reader/data/axi_mm_reader.mdd:3-10](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/drivers/axi_mm_reader/data/axi_mm_reader.mdd#L3-L10) —— 全文只有一条 `DRIVER` 声明：`supported_peripherals = (axi_mm_reader)`、`copyfiles = all`、`VERSION = 1.0`、`NAME = axi_mm_reader`。`psf_version = 2.1` 是 MDD 文件格式版本。这一条记录就是 Vitis 把外设与驱动绑起来的依据。注意外设名 `axi_mm_reader` 必须与 `component.xml` 里 `spirit:name` 完全一致（[component.xml:5](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/component.xml#L5)），否则 BSP 找不到驱动。

**TCL：generate 过程**

[drivers/axi_mm_reader/data/axi_mm_reader.tcl:3-5](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/drivers/axi_mm_reader/data/axi_mm_reader.tcl#L3-L5) —— 全文就是一个 `generate` 过程，调用 Xilinx BSP 工具命令 `xdefine_include_file`：

```tcl
xdefine_include_file $drv_handle "xparameters.h" axi_mm_reader \
    "NUM_INSTANCES" "DEVICE_ID" "C_BASEADDR" "C_HIGHADDR"
```

四个参数每个对应一类写入 `xparameters.h` 的宏（宏前缀取外设名大写 `AXI_MM_READER`）：

| 参数 | 生成的宏（示意） | 含义 | 值的来源 |
| --- | --- | --- | --- |
| `NUM_INSTANCES` | `XPAR_AXI_MM_READER_NUM_INSTANCES` | 设计里这个外设有几个实例 | BD 中实例计数 |
| `DEVICE_ID` | `XPAR_AXI_MM_READER_0_DEVICE_ID` | 实例的唯一设备号 | BSP 自动编号 |
| `C_BASEADDR` | `XPAR_AXI_MM_READER_0_BASEADDR` | `s00_axi` 地址块起始地址 | BD 地址编辑器分配给 `reg0` 的基地址 |
| `C_HIGHADDR` | `XPAR_AXI_MM_READER_0_HIGHADDR` | `s00_axi` 地址块结束地址 | BD 地址编辑器分配给 `reg0` 的高地址 |

> 这四个是 Xilinx 驱动的「规范参数名」，`xdefine_include_file` 据此自动展开成 `#define`。`C_BASEADDR`/`C_HIGHADDR` 与 `component.xml` 里 `memoryMaps/reg0` 的 `baseAddress`/`range` 一一对应（[component.xml:506-509](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/component.xml#L506-L509)）：BD 按这块内存映射给实例分配地址，再由 `.tcl` 写成宏。

**Makefile：编译进 libxil.a**

[drivers/axi_mm_reader/src/Makefile:6-21](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/drivers/axi_mm_reader/src/Makefile#L6-L21) —— 标准的 Xilinx 驱动 Makefile：`LIB=libxil.a`，`libs` 目标用 `${COMPILER}` 编译所有 `.c`、用 `${ARCHIVER} -r` 把 `.o` 归档进 `${RELEASEDIR}/libxil.a`（`RELEASEDIR=../../../lib`）。`COMPILER`/`ARCHIVER` 变量由 BSP 工具注入（如 `arm-none-eabi-gcc`/`ar`）。`include` 目标把头文件拷到 `include/` 目录，供应用 `#include`。

**驱动如何用 baseAddr（闭合到硬件）**

驱动 API 全部以 `baseAddr` 为第一参数，结合 `axi_mm_reader.h` 里的寄存器偏移宏做一次 `Xil_In32`/`Xil_Out32`：

[drivers/axi_mm_reader/src/axi_mm_reader.c:19-24](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/drivers/axi_mm_reader/src/axi_mm_reader.c#L19-L24) —— `MmReader_SetEnable` 写 `baseAddr + MM_READER_ENA_REG`（`0x00`）。这里的 `baseAddr` 在应用里就取自 `xparameters.h` 的 `XPAR_AXI_MM_READER_0_BASEADDR`，而 `MM_READER_ENA_REG=0x00` 与 RTL 侧 `definitions_pkg.vhd` 的使能寄存器字索引一致（详见 [u2-l2](u2-l2-register-map.md)、[u3-l1](u3-l1-c-driver.md)）。

[drivers/axi_mm_reader/src/axi_mm_reader.c:53-57](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/drivers/axi_mm_reader/src/axi_mm_reader.c#L53-L57) —— `MmReader_SetRegTable` 用 `baseAddr + MM_READER_REGMAP_OFFS + 4*idx`（`0x20` 起）逐项写 RegTable，再次印证「字节地址 = 字索引×4」（[u2-l5](u2-l5-axi-slave-wrapper.md)）。

至此，从 BD 地址编辑器一路到寄存器写入的链路全部接通：

```
BD 地址编辑器给 s00_axi/reg0 分配基地址
   │  导出 XSA → Vitis BSP
   ▼
.mdd 声明驱动  →  .tcl::generate 写 xparameters.h 的 XPAR_AXI_MM_READER_0_BASEADDR
   │  Makefile 把 .c 编进 libxil.a
   ▼
应用以 XPAR_AXI_MM_READER_0_BASEADDR 作为 baseAddr  →  Xil_Out32(baseAddr + 0x00, ...)
   ▼
s00_axi 从机  →  Enable/RegCnt/RegTable（[u2-l5](u2-l5-axi-slave-wrapper.md)）
```

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：追踪 `C_BASEADDR` 从 BD 一路到一次 `Xil_Out32` 的全过程，并核对外设名与寄存器偏移在多处是否一致。

**操作步骤**：

1. 打开 `.mdd`，确认 `supported_peripherals` 的外设名；再打开 `component.xml` 确认 `spirit:name` 与之一致。
2. 打开 `.tcl`，列出 `generate` 传给 `xdefine_include_file` 的四个参数，并写出各自对应的宏名与含义。
3. 打开 `axi_mm_reader.h`，找到 `MM_READER_ENA_REG` 与 `MM_READER_REGMAP_OFFS` 的值；对照 [u2-l2](u2-l2-register-map.md) 的寄存器表，确认偏移一致。
4. 打开 `axi_mm_reader.c` 的 `MmReader_SetEnable` 与 `MmReader_SetRegTable`，确认它们用的是 `baseAddr + 偏移` 的形式。
5. 在 `component.xml` 的 `softwaredriver fileSet` 里，核对应包含 `.mdd/.tcl/Makefile/.c/.h` 五个文件。

**需要观察的现象**：外设名、寄存器偏移在 `.mdd`/`.tcl`/`.h`/`.c`/`component.xml` 多处出现且完全一致——任何一处拼错都会断链。

**预期结果**：能画出上面的端到端链路图，并说出 `baseAddr` 的最终来源是 BD 地址编辑器分配给 `reg0` 的基地址。

> 待本地验证：在 Vitis 里生成 BSP 后，打开生成的 `xparameters.h`，搜索 `XPAR_AXI_MM_READER`，确认存在 `NUM_INSTANCES`/`DEVICE_ID`/`BASEADDR`/`HIGHADDR` 四类宏，且 `BASEADDR` 与 BD 地址编辑器里 `s00_axi` 的分配值一致。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `component.xml` 里 `spirit:name` 改成了别的名字（假设），但没改 `.mdd`，会发生什么？
**答案**：BSP 工具按 `component.xml` 的外设名找驱动，而 `.mdd` 声明的是旧名，匹配失败，Vitis 会给这个外设挂不上驱动（或挂默认的 `generic` 驱动），`xparameters.h` 里也不会生成对应的宏。这也再次说明外设名是跨文件的契约。

**练习 2**：`xdefine_include_file` 的四个参数里，哪两个的值来自 BD 地址编辑器？为什么？
**答案**：`C_BASEADDR` 与 `C_HIGHADDR`。它们对应 `s00_axi` 暴露给软件的地址块 `reg0`（`component.xml` 的 `memoryMaps`），这块的基地址/高地址由 BD 地址编辑器在 BD 里分配，所以 BSP 生成时能拿到具体数值写成宏。

**练习 3**：驱动的 `baseAddr` 参数与应用代码的关系是什么？
**答案**：应用代码不直接写裸地址，而是把 `xparameters.h` 里的 `XPAR_AXI_MM_READER_0_BASEADDR`（由 `.tcl` 生成）传给驱动 API 的 `baseAddr` 形参。这样换板子/改地址映射时，只需重新生成 BSP，应用代码不用改。

---

## 5. 综合实践

**任务**：画两张「端到端链路图」，把本讲三个模块串起来，并向同学讲解。两张图都只基于本仓库真实文件。

**图 A：软件侧——从 BD 地址到一次寄存器写**

按下面顺序连线，每条连线标注「由哪个文件/机制完成」：

1. BD 地址编辑器给 `s00_axi` 的 `reg0` 分配基地址（`component.xml` 里 `memoryMaps/reg0`，[component.xml:501-512](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/component.xml#L501-L512)）。
2. 导出 XSA → Vitis BSP 读 `.mdd`（[axi_mm_reader.mdd:5-10](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/drivers/axi_mm_reader/data/axi_mm_reader.mdd#L5-L10)）识别驱动。
3. `.tcl::generate` 写 `xparameters.h` 的 `C_BASEADDR`/`C_HIGHADDR`（[axi_mm_reader.tcl:3-5](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/drivers/axi_mm_reader/data/axi_mm_reader.tcl#L3-L5)）。
4. `Makefile` 把 `axi_mm_reader.c` 编进 `libxil.a`（[Makefile:17-21](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/drivers/axi_mm_reader/src/Makefile#L17-L21)）。
5. 应用以 `XPAR_AXI_MM_READER_0_BASEADDR` 调 `MmReader_SetEnable` → `Xil_Out32(baseAddr + 0x00, ...)`（[axi_mm_reader.c:19-24](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/drivers/axi_mm_reader/src/axi_mm_reader.c#L19-L24)）。
6. 经 `s00_axi` 从机落到 Enable 寄存器（[u2-l5](u2-l5-axi-slave-wrapper.md)）。

**图 B：硬件侧——从上游主机 ID 宽度到 s00_axi 的 ID 信号位宽**

按 4.2.3 末尾那条闭合链路画出五个节点：上游主机 ID 宽度 → `bd.tcl::propagate` → `PARAM_VALUE.C_S00_AXI_ID_WIDTH` → `xgui::update_MODELPARAM_VALUE.C_S00_AXI_ID_WIDTH` → `MODELPARAM_VALUE.C_S00_AXI_ID_WIDTH`（驱动 [component.xml:660-679](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/component.xml#L660-L679) 的端口位宽）→ wrapper generic `C_S00_AXI_ID_WIDTH`（[axi_mm_reader_wrp.vhd:35](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L35)）→ `AxiIdWidth_g`（[axi_mm_reader_wrp.vhd:198](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L198)）。

**交付**：两张图（手绘或文本框图均可）+ 一段话，说明「为什么改了 `MaxRegCount_g` 之后需要重新打包、重新生成 BSP，应用代码却通常不用改」（提示：参数变化改变的是 `component.xml` 里 `reg0` 的 `range` 表达式与 RegTable 深度，进而改变 BD 地址范围与 `C_HIGHADDR`；但驱动 API 与寄存器偏移不变，所以应用代码透明）。

## 6. 本讲小结

- `component.xml` 是 `package.tcl` 产出的 IP-XACT 总账本，用 `busInterfaces`/`addressSpaces`/`memoryMaps`/`model`/`fileSets`/`vendorExtensions` 六大段把一个 IP 的接口、地址、参数、文件、器件族自描述清楚；它是生成产物，正常不手改。
- 端口位宽与地址范围在 `component.xml` 里是**依赖 `MODELPARAM_VALUE` 的表达式**：`s00_axi` 的 ID 信号位宽依赖 `C_S00_AXI_ID_WIDTH`，`reg0` 的范围依赖 `AxiSlaveAddrWidth_g`。
- `bd/bd.tcl` 是标准 Xilinx AXI4 `ID_WIDTH` 传播模板：`init` 把从机 `C_S00_AXI_ID_WIDTH` 标记为 propagate-only；`pre_propagate` 把主机 ID 往外推（对本 IP 因 `m00_axi` 无 ID 而空跑）；`propagate` 把外部 ID 收进从机参数（本 IP 的实际生效路径）。
- 传播值经 `xgui` 的 `update_MODELPARAM_VALUE.C_S00_AXI_ID_WIDTH` 桥接到 `MODELPARAM_VALUE`，再由 `component.xml` 的端口位宽表达式与 wrapper 的 generic `C_S00_AXI_ID_WIDTH`（→ `AxiIdWidth_g`）落地成硬件。
- `.mdd` 声明外设与驱动的绑定，`.tcl::generate` 用 `xdefine_include_file` 把 `NUM_INSTANCES`/`DEVICE_ID`/`C_BASEADDR`/`C_HIGHADDR` 写进 `xparameters.h`，`Makefile` 把 `.c` 编进 `libxil.a`；`C_BASEADDR` 的源头是 BD 地址编辑器分配给 `s00_axi/reg0` 的基地址。
- 驱动 API 以 `baseAddr` + 寄存器偏移做 `Xil_In32`/`Xil_Out32`，把整条软件链路（BD 地址 → `xparameters.h` → 驱动 → `s00_axi` → RegTable）闭合。

## 7. 下一步学习建议

- 想把「参数→地址范围→驱动宏」这条链路再夯实一遍，建议接着学 [u3-l3 参数化与 GUI 配置](u3-l3-parameters-gui.md)，重点看六个 generic 如何进入 `MODELPARAM_VALUE`。
- 想从软件侧完整理解驱动 API 与寄存器契约，建议（复）学 [u3-l1 C 软件驱动](u3-l1-c-driver.md)，把本讲的 `baseAddr` 与 `MmReader_*` 错误码对上。
- 想做端到端扩展（新增一个只读状态寄存器并同步到文档/测试/驱动），直接进入 [u3-l5 二次开发实践：扩展该 IP](u3-l5-extending-ip.md)，那里会用到本讲的「改源头、再重新打包、再重新生成 BSP」的全流程。
- 若要深入 BD 钩子的官方语义，可阅读 Vivado 的 *Vivado Design Suite User Guide: Creating Packaging Custom IP*（UG1118）中关于「BD parameter propagation hooks」的章节，对照本讲的 `bd.tcl` 三回调理解。
