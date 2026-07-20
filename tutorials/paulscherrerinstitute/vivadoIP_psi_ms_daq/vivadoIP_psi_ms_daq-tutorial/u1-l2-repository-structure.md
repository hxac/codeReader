# 仓库目录结构与关键文件导览

## 1. 本讲目标

上一讲（u1-l1）我们已经建立了项目画像：本仓库 `vivadoIP_psi_ms_daq` 只是一个 **Vivado IP 封装层（wrapper）**，真正的功能逻辑在另一个仓库 `psi_multi_stream_daq` 里。本讲不再谈「是什么」，而是解决「东西放在哪」。

学完本讲，你应当能够：

1. 闭着眼睛说出每个顶层目录（`hdl` / `scripts` / `xgui` / `bd` / `drivers` / `refdesign` / `doc`）各负责什么。
2. 在仓库里**一眼定位**四个最常被打交道的文件：唯一的 RTL 文件、IP 打包脚本、C 驱动头文件、ZCU102 参考应用的 `main.c`。
3. 理解 `scripts/package.tcl`（人写的打包脚本）与 `component.xml`（机器生成的 IP-XACT 描述）之间的**源与产物**关系，从而知道改哪里才有效。

本讲全部是「地图」层面的内容，不展开任何具体算法或寄存器细节——那是后续讲义的任务。

## 2. 前置知识

阅读本讲前，你需要先建立以下概念（若不熟悉，请先看 u1-l1）：

- **IP-Core（IP 核）**：Vivado 里可复用、可在 Block Design（BD）里拖来拖去的功能模块。它由 RTL 代码 + 一份机器可读的描述文件组成。
- **IP-XACT**：一种 IEEE 标准（SPIRIT/1685-2009）的 XML 格式，用来描述一个 IP 有哪些参数、哪些总线接口、哪些端口。Vivado 正是靠它来「认识」一个 IP。
- **Wrapper（封装）**：把一个内部实现（`psi_ms_daq_axi`）包一层外壳（`psi_ms_daq_vivado`），把「标量参数」翻译成 Vivado IP 喜欢的形式。
- **AXI / AXI-Stream**：两类总线协议。AXI（含 Slave/Master）用于寄存器访问与内存读写；AXI-Stream 用于高速数据流输入。
- **BSP（Board Support Package）**：Vitis/XSDK 里「板级支持包」，把 IP 的地址、中断等信息生成 `xparameters.h` 等，让 C 程序能用。

> 名词提示：本仓库里你会反复看到 `psi_ms_daq_axi`（带 `_axi`）这个名字。注意区分——
> - `psi_ms_daq_vivado`（本仓库的 VHDL 文件名、entity 名）= 封装外壳；
> - `psi_ms_daq_axi`（IP 名、上游实现 entity 名）= 真正干活的实现，也是 Vivado 里 IP 的注册名。

## 3. 本讲源码地图

本讲只看「骨架」级别的内容，涉及的关键文件如下：

| 文件 | 作用 | 本讲怎么看 |
| --- | --- | --- |
| `README.md` | 项目说明、依赖清单、功能描述 | 看 Description / Important Note 两节，确认封装层定位 |
| `hdl/psi_ms_daq_vivado.vhd` | **唯一的 RTL 文件**，封装外壳 | 只确认它存在、是 entity+architecture 结构、内部例化了 `psi_ms_daq_axi` |
| `scripts/package.tcl` | **IP 打包脚本**（人写） | 通读结构：元信息→源文件→驱动→GUI→端口使能→`package_ip` |
| `component.xml` | **IP-XACT 描述**（机器生成） | 只看头部：vendor/library/name/version 与总线接口组织 |
| `scripts/dependencies.py` | 依赖拉取脚本 | 确认它解析 README 来获取依赖 |
| `bd/bd.tcl` | Block Design 钩子脚本 | 确认它处理 AXI ID_WIDTH 传播 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**顶层目录划分**、**唯一 RTL 文件的位置与角色**、**package.tcl 与 component.xml 的关系**。

### 4.1 顶层目录划分

#### 4.1.1 概念说明

一个「Vivado IP 封装仓库」本质上要同时服务三类读者：

- **FPGA 工程师**：需要 RTL 源码、打包脚本、约束文件。
- **嵌入式软件工程师**：需要 C 驱动、寄存器映射、参考应用。
- **Vivado 工具本身**：需要 IP-XACT 描述、GUI 定义、BD 钩子。

本仓库的目录划分正是按这三类需求来的。每个顶层目录对应一种「产物来源」，理解了来源，你就知道改动应该落在哪里。

#### 4.1.2 核心流程

整个仓库的目录可以这样归类（按「谁产生它」分）：

```text
vivadoIP_psi_ms_daq/
├── README.md / Changelog.md / License.txt / LGPL2_1.txt   ← 人写的文档与许可证
├── component.xml                                          ← 工具生成（IP-XACT 产物）
│
├── hdl/        ← 人写的 RTL（本仓库只有 1 个封装文件）
├── scripts/    ← 人写的自动化脚本（打包 + 依赖）
├── xgui/       ← 工具生成（IP 定制 GUI）
├── bd/         ← 人写的 Block Design 钩子
├── drivers/    ← 人写框架 + 上游自动覆盖实现（C 驱动）
├── refdesign/  ← 人写的 ZCU102 参考设计（Vivado 工程 + SDK 应用）
└── doc/        ← 人写的说明文档（PDF/HTML/图片）
```

记住一个判断口诀：

> **「人写」的目录才需要你动手改；「工具生成」的目录（`component.xml`、`xgui/`）改了也会被下一次打包覆盖。**

#### 4.1.3 源码精读

下面给出每个目录的关键文件与一句话职责，均来自真实仓库内容（通过 `git ls-files` 确认）。

**顶层文档类**：`README.md` 的 Description 节概述了 IP 功能与「封装层」定位。

- [README.md:54-62](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/README.md#L54-L62)：列出 IP 的主要特性（最多 16 路 Stream、最多 32 个触发、线性/环形缓冲、64 位 2 GB/s、提供参考设计）。
- [README.md:64-65](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/README.md#L64-L65)：**Important Note**——明确声明本仓库只是 wrapper，功能改动要去 `psi_multi_stream_daq`。

**各目录职责表**（逐目录展开）：

| 目录 | 关键文件 | 一句话职责 |
| --- | --- | --- |
| `hdl/` | `psi_ms_daq_vivado.vhd` | **唯一的 RTL 文件**，把上游实现封装成可例化外壳 |
| `scripts/` | `package.tcl`, `dependencies.py` | `package.tcl` 把 RTL 打包成 IP；`dependencies.py` 拉 VHDL 依赖 |
| `xgui/` | `psi_ms_daq_axi_v1_2.tcl` | IP 定制对话框的 GUI 定义（由打包自动生成，约 1920 行） |
| `bd/` | `bd.tcl` | BD 钩子：处理 AXI Slave 的 `ID_WIDTH` 自动传播 |
| `drivers/psi_ms_daq_axi/` | `src/{psi_ms_daq.c,psi_ms_daq.h,Makefile}`, `data/{.mdd,.tcl}` | C 驱动实现 + BSP 集成脚本 |
| `refdesign/ZCU102/` | `project.tcl`, `src/system_wrapper.vhd`, `constraints/timing.xdc`, `Sdk/app/src/main.c` | ZCU102 参考设计的 Vivado 工程 + SDK 裸机应用 |
| `doc/` | `ReferenceDesignUserGuide.pdf`, `ip_core_doc.html`, `driver_doc.html` 等 | 面向使用者的说明文档 |

**四个最常被打交道的文件**（请务必记住完整路径，后续讲义反复引用）：

1. 唯一 RTL 文件：`hdl/psi_ms_daq_vivado.vhd`
2. IP 打包脚本：`scripts/package.tcl`
3. C 驱动头文件：`drivers/psi_ms_daq_axi/src/psi_ms_daq.h`
4. ZCU102 参考应用入口：`refdesign/ZCU102/Sdk/app/src/main.c`

#### 4.1.4 代码实践

**实践目标**：用 `git ls-files` 自己核对一遍目录树，确认本讲的归类是否与真实仓库一致。

**操作步骤**：

1. 在仓库根目录执行 `git ls-files`，得到全部受版本控制的文件列表。
2. 按顶层目录把文件分组，统计每个目录的文件数。
3. 对照上面的「各目录职责表」，在每个目录旁标注一句话职责。

**需要观察的现象**：

- `hdl/` 目录下**只有一个** `.vhd` 文件，印证「本仓库只有封装外壳」。
- `drivers/psi_ms_daq_axi/src/` 下同时有 `.c` 和 `.h`，说明驱动实现和声明都在这里。
- `refdesign/ZCU102/` 下有完整的 `Sdk/app`、`Sdk/bsp`、`Sdk/hw` 三件套，是一个可直接导入 Vitis 的工程。

**预期结果**：你会得到一张与本文一致的目录树。如果你看到的目录结构与本讲描述不符（例如 `hdl/` 下出现多个 `.vhd`），请先确认你 checkout 的 HEAD 是否为 `c210e3ff`（本讲基于该 HEAD）。

#### 4.1.5 小练习与答案

**练习 1**：仓库里哪两个目录是「工具自动生成、改了会被覆盖」的？

> **答案**：`component.xml`（虽然放在根目录，但属于打包产物）和 `xgui/`（IP 定制 GUI）。两者都由运行 `scripts/package.tcl` 重新生成。

**练习 2**：如果你只想读 C 驱动的寄存器宏定义，应该打开哪个文件？

> **答案**：`drivers/psi_ms_daq_axi/src/psi_ms_daq.h`（声明与宏），实现细节在同级目录的 `psi_ms_daq.c`。

---

### 4.2 唯一的 RTL 文件：`psi_ms_daq_vivado.vhd`

#### 4.2.1 概念说明

`hdl/psi_ms_daq_vivado.vhd` 是本仓库**唯一**的 RTL 文件。它存在的意义不是「实现采集逻辑」，而是「把上游 `psi_ms_daq_axi` 实现包装成 Vivado IP 能接受的样子」。

为什么需要这一层包装？因为上游实现使用的 generic（泛型）是**数组类型**（比如 `StreamWidth_g` 是一个长度为 N 的数组），而 Vivado IP 的 GUI 与 `component.xml` 更喜欢**标量**参数（一组 `Stream0Width_g`、`Stream1Width_g`……）。封装层的工作就是把 16 路标量参数「聚合成数组」，再喂给上游实现。

> 本讲只看它的「存在、位置、骨架结构」。泛型/端口/数组映射的精读在 u2-l1、u2-l2、u2-l3。

#### 4.2.2 核心流程

这个文件由三部分组成，呈现标准的 VHDL 文件骨架：

```text
1. library ieee + library work (psi_common_array_pkg)
2. entity psi_ms_daq_vivado is
       generic 区：通用配置 + 录制 + AXI + 16 路逐流标量 + BD 常量
       port 区   ：AXI Slave / AXI Master / 16 路 Stream 输入 / IRQ / Trig
3. architecture rtl of psi_ms_daq_vivado is
       声明 All_* 固定 16 路数组信号
       把 Str00..Str15 逐路接到 All_*
       例化 i_impl : entity work.psi_ms_daq_axi（上游实现）
```

也就是说，这个文件本身**不包含任何采集算法**，它只是「线缆重新排布 + 参数重新打包」。

#### 4.2.3 源码精读

文件头部确认它是 2019 年 PSI 出品、作者 Oliver Bruendler，并引用了 `psi_common_array_pkg`（说明要用到数组类型）：

- [hdl/psi_ms_daq_vivado.vhd:1-17](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/hdl/psi_ms_daq_vivado.vhd#L1-L17)：版权与 library 声明。

entity 名为 `psi_ms_daq_vivado`（注意带 `_vivado` 后缀，区别于上游的 `_axi`）：

- [hdl/psi_ms_daq_vivado.vhd:22-26](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/hdl/psi_ms_daq_vivado.vhd#L22-L26)：entity 开头与通用配置泛型（`Streams_g` 默认 3、`TsPerStream_g`、`UseLastAsTrigger_g`）。

architecture 末尾例化了上游实现——这是「封装」二字最直接的证据：

- [hdl/psi_ms_daq_vivado.vhd:554-560](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/hdl/psi_ms_daq_vivado.vhd#L554-L560)：`i_impl : entity work.psi_ms_daq_axi`，并在 `generic map` 里把 `Stream0Width_g..Stream15Width_g` 聚合成一个数组传给上游。

文件在 669 行结束，整份文件 669 行，其中绝大部分是 16 路 Stream 的逐行端口声明与赋值（重复结构），核心逻辑其实很短。

#### 4.2.4 代码实践

**实践目标**：确认 `hdl/` 目录下确实只有一个 RTL 文件，并验证它例化了上游 `psi_ms_daq_axi`。

**操作步骤**：

1. 执行 `git ls-files hdl/`，确认输出只有 `hdl/psi_ms_daq_vivado.vhd` 一行。
2. 在该文件中搜索 `entity work.psi_ms_daq_axi`（即上游实现的例化点）。

**需要观察的现象**：

- `git ls-files hdl/` 只返回一个文件——本仓库不包含任何采集算法的 RTL，全部来自上游。
- 例化语句 `i_impl : entity work.psi_ms_daq_axi` 出现在 architecture 内（约 554 行）。

**预期结果**：你会清楚看到「外壳 → 例化上游」这一层关系。如果以后要改采集行为，这里**不是**该改的地方（要去 `psi_multi_stream_daq` 仓库）。

#### 4.2.5 小练习与答案

**练习 1**：本仓库的 RTL 文件名是 `psi_ms_daq_vivado.vhd`，而 IP 注册名是 `psi_ms_daq_axi`，为什么不一样？

> **答案**：文件名/entity 名带 `_vivado` 表示「Vivado 封装外壳」；`psi_ms_daq_axi` 是上游真正实现的 entity 名，也是打包脚本里 `set IP_NAME psi_ms_daq_axi` 设定的 IP 注册名。外壳例化了实现，两者名字不同是有意区分。

**练习 2**：这个文件里有没有 `process`（进程）语句来实现时序逻辑？

> **答案**：没有。封装层只做信号重排与例化，全是并发赋值与 `generate` 块，不含 `process`。真正的时序逻辑在 `psi_ms_daq_axi` 及其子模块里（属于上游仓库）。

---

### 4.3 `package.tcl` 与 `component.xml` 的关系：源与产物

#### 4.3.1 概念说明

很多初学者第一次打开本仓库会被根目录那个 7093 行的 `component.xml` 吓到，以为要读懂它。**大可不必。** 理解这一节后你会明白：`component.xml` 是「产物」，真正该读、该改的是 `scripts/package.tcl` 这个「源」。

- **`scripts/package.tcl`（源，人写）**：一份约 189 行的 TCL 脚本，调用外部工具 `PsiIpPackage`，用人类可读的命令描述「这个 IP 叫什么、包含哪些源文件、GUI 上有哪些参数、哪些端口可选」。
- **`component.xml`（产物，工具生成）**：一份符合 IP-XACT 标准的 XML，Vivado 靠它来注册并识别这个 IP。它把 `package.tcl` 里所有声明翻译成机器格式。

两者关系类似「源代码」与「编译产物」：改了 `package.tcl`，下次打包会重新生成 `component.xml`；直接手改 `component.xml` 会被下次打包覆盖。

#### 4.3.2 核心流程

`package.tcl` 的执行流程可以拆成 6 步：

```text
① source 外部工具 PsiIpPackage.tcl，导入打包命令
② 设置元信息（IP_NAME/IP_VERSION/IP_LIBRARY/描述/logo/datasheet）
③ 添加源文件
     - add_sources_relative：本仓库自己的 RTL（只有 psi_ms_daq_vivado.vhd）
     - add_lib_relative  ：上游依赖的 VHDL（psi_common + psi_multi_stream_daq）
④ 处理驱动文件
     - 从上游 psi_multi_stream_daq 拷贝 .c/.h 到本地 drivers/（覆盖本地）
     - add_drivers_relative：把驱动登记进 IP
⑤ 定义 GUI 参数（General / AXI Master / Stream 0..15 三类页面）
⑥ 定义端口使能条件（按 Streams_g 决定哪些 StrXX 端口可见）
⑦ package_ip：综合并产出 component.xml + xgui/*.tcl
```

注意第 ④ 步的一个关键事实：**本地 `drivers/` 下的 `.c/.h` 每次打包都会被上游同名文件覆盖**。所以即使你想改驱动，也不该改本仓库的副本，而要去上游 `psi_multi_stream_daq/driver/`。

#### 4.3.3 源码精读

**`package.tcl` 第 ①② 步**——引入工具并设置元信息。注意 IP 名正是 `psi_ms_daq_axi`，版本 `1.2`：

- [scripts/package.tcl:10-23](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/scripts/package.tcl#L10-L23)：`source ../../../TCL/PsiIpPackage/PsiIpPackage.tcl`（外部工具）、`set IP_NAME psi_ms_daq_axi`、`set IP_VERSION 1.2`、`set IP_LIBRARY PSI`，然后 `init`。

**`package.tcl` 第 ③ 步**——源文件清单。这是「本仓库只贡献一个 RTL 文件」的铁证：

- [scripts/package.tcl:32-34](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/scripts/package.tcl#L32-L34)：`add_sources_relative` 只含 `../hdl/psi_ms_daq_vivado.vhd`。
- [scripts/package.tcl:37-64](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/scripts/package.tcl#L37-L64)：`add_lib_relative` 列出上游 `VHDL/psi_common/...` 与 `VHDL/psi_multi_stream_daq/...` 全部被综合进 IP 的文件（运行时依赖）。

**`package.tcl` 第 ④ 步**——驱动从上游拷贝并覆盖本地：

- [scripts/package.tcl:70-82](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/scripts/package.tcl#L70-L82)：注释明确警告「本地驱动文件会被上游覆盖」，随后 `file copy -force` 把上游 `psi_ms_daq.c/.h` 拷到 `drivers/psi_ms_daq_axi/src/`。

**`package.tcl` 第 ⑦ 步**——产出 IP（目标器件 `xczu9eg`，即 ZCU102 的主芯片，且会做综合）：

- [scripts/package.tcl:187-189](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/scripts/package.tcl#L187-L189)：`package_ip $TargetDir false true xczu9eg-ffvb1156-2-e`。

**`component.xml` 头部**——确认它就是 `package.tcl` 元信息的 IP-XACT 翻译：

- [component.xml:3-6](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/component.xml#L3-L6)：`<vendor>psi.ch</vendor>`、`<library>PSI</library>`、`<name>psi_ms_daq_axi</name>`、`<version>1.2</version>`——与 `package.tcl` 第 16-19 行一一对应。

**`component.xml` 总线接口**——以 `Str00` 为例，它把物理端口 `Str00_TData/TLast/TValid/TReady` 映射为标准 `axis` 总线接口，并带使能条件 `$Streams_g > 0`：

- [component.xml:8-54](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/component.xml#L8-L54)：`Str00` 的 `busInterface` 定义，`busType` 指向 `xilinx.com:interface:axis`，并在 `vendorExtensions` 里写出 `dependency="$Streams_g > 0"`。

这条使能条件 `Streams_g > 0` 正来自 `package.tcl` 第 173 行 `add_port_enablement_condition "Str00\_TData" "\$Streams_g > 0"`——**这就是「源与产物」对应关系的一个具体证据**。

#### 4.3.4 代码实践

**实践目标**：亲手验证「`package.tcl` 是源、`component.xml` 是产物」的对应关系，而不是只听结论。

**操作步骤（源码阅读型）**：

1. 打开 `scripts/package.tcl` 第 16-19 行，记下 `IP_NAME`、`IP_VERSION`、`IP_LIBRARY` 三个值。
2. 打开 `component.xml` 第 3-6 行，对比 `<name>`、`<version>`、`<library>` 是否完全一致。
3. 打开 `scripts/package.tcl` 第 173 行，看 `Str00` 的使能条件表达式。
4. 打开 `component.xml` 第 50 行附近，看 `Str00` 的 `isEnabled` 的 `dependency` 属性是否是同一个表达式。

**需要观察的现象**：

- 两处的 IP 名/版本/库完全相同（`psi_ms_daq_axi` / `1.2` / `PSI`）。
- `Str00` 的端口使能条件 `$Streams_g > 0` 在 `package.tcl` 和 `component.xml` 里都能找到，内容一致。

**预期结果**：你会确信 `component.xml` 是 `package.tcl` 跑完 `package_ip` 后自动写出的。以后要改 IP 参数或端口，**改 `package.tcl` 然后重新打包**，而不是直接编辑 7093 行的 XML。

> 待本地验证：本实践不要求真的运行 Vivado 打包（需要 `PsiIpPackage` 工具链与上游仓库齐备）。仅做文件对照即可达成目标。

#### 4.3.5 小练习与答案

**练习 1**：同事说他「直接改了 `component.xml` 里某个参数的默认值」，这个改动能持久化吗？

> **答案**：不能。下次有人运行 `scripts/package.tcl` 重新打包时，`component.xml` 会被重新生成，改动丢失。正确做法是改 `package.tcl` 里对应的 `gui_create_parameter` 默认值（或 RTL 的 generic 默认值），再重新打包。

**练习 2**：为什么 `drivers/psi_ms_daq_axi/src/psi_ms_daq.c` 本地副本的注释里写着「会被自动覆盖」？

> **答案**：见 [scripts/package.tcl:75-76](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/scripts/package.tcl#L75-L76)。打包脚本用 `file copy -force` 从上游 `psi_multi_stream_daq/driver/` 把 `.c/.h` 拷过来覆盖本地。驱动源头的真身在上游仓库。

**练习 3**：`xgui/psi_ms_daq_axi_v1_2.tcl`（约 1920 行）是手写的还是自动生成的？

> **答案**：自动生成。它是 `package_ip` 根据 `package.tcl` 里 `gui_add_page` / `gui_create_parameter` 等调用产出的 IP 定制 GUI 脚本，与 `component.xml` 同属「产物」。文件名里的 `v1_2` 对应 IP 版本 `1.2`。

## 5. 综合实践

把本讲三块内容串起来，完成一张「**带职责标注的目录地图 + 关键路径清单**」。

**任务**：

1. 在编辑器或纸上画出本仓库的顶层目录树（到二级即可，例如 `drivers/psi_ms_daq_axi/src/`、`refdesign/ZCU102/Sdk/app/src/`）。
2. 在每个目录旁写一句话职责（用本讲 4.1.3 的表格校对）。
3. 在树上用四种不同记号（或颜色）标出下面四个文件的**完整路径**：
   - 唯一 RTL 文件
   - IP 打包脚本
   - C 驱动头文件
   - ZCU102 参考应用 `main.c`
4. 在树旁另起一栏，标出哪两个文件/目录是「工具生成」的（`component.xml`、`xgui/`），并用箭头画出「`scripts/package.tcl` → 生成 → `component.xml` + `xgui/*.tcl`」的关系。

**验收标准**：

- 别人只看你的图，就能回答：「我想改 IP 的 GUI 参数默认值，该去哪个文件？」「我想读寄存器宏，该去哪个文件？」「我想看一个能跑的采集示例，该去哪个文件？」三个问题的答案分别是 `scripts/package.tcl`、`drivers/psi_ms_daq_axi/src/psi_ms_daq.h`、`refdesign/ZCU102/Sdk/app/src/main.c`。

> 提示：这张图建议保存下来，后续每一讲开头都会引用其中的路径。

## 6. 本讲小结

- 本仓库顶层目录按「读者类型」划分：`hdl`/`scripts`/`bd`/`refdesign` 给 FPGA 工程师，`drivers` 给嵌入式软件工程师，`component.xml`/`xgui` 给 Vivado 工具，`doc` 给所有人。
- **四个必记路径**：`hdl/psi_ms_daq_vivado.vhd`（唯一 RTL）、`scripts/package.tcl`（打包脚本）、`drivers/psi_ms_daq_axi/src/psi_ms_daq.h`（驱动头）、`refdesign/ZCU102/Sdk/app/src/main.c`（参考应用入口）。
- `hdl/` 下只有**一个** RTL 文件，它只是封装外壳——内部 `entity work.psi_ms_daq_axi`（上游实现）才是干活的，改采集逻辑要去上游仓库。
- `scripts/package.tcl` 是**源**（人写、189 行），`component.xml` 是**产物**（机器生成、7093 行 IP-XACT）。改 IP 行为要改源、重新打包，不要直接改产物。
- 本地 `drivers/*.c/*.h` 每次打包会被上游同名文件覆盖，驱动真身也在上游 `psi_multi_stream_daq`。
- `xgui/psi_ms_daq_axi_v1_2.tcl` 与 `component.xml` 同属打包产物，文件名里的 `v1_2` 对应 IP 版本 `1.2`。

## 7. 下一步学习建议

地图建立之后，下一步有两条可选路线：

- **想先理解硬件封装**：进入第 2 单元，从 [u2-l1 封装实体：泛型与端口全景](u2-l1-wrapper-entity-generics-ports.md) 开始，精读 `hdl/psi_ms_daq_vivado.vhd` 的 entity。
- **想先理解 IP 如何被打包成可例化产物**：进入 [u1-l4 IP 打包流程总览：从 RTL 到可例化 IP](u1-l4-ip-packaging-overview.md)，系统过一遍 `package.tcl` 的每一步（本讲的 4.3 是其浓缩版）。

如果你还想先补齐「依赖从哪来」的疑问，可以先看 [u1-l3 依赖关系与获取全部源码](u1-l3-dependencies-and-sources.md)，它会解释 `scripts/dependencies.py` 如何解析 README 拉取 `psi_common`、`psi_multi_stream_daq` 等仓库。

建议阅读顺序：**u1-l3 → u1-l4 → u2-l1**，把「依赖来源 → 打包流程 → RTL 实体」连成一条完整的硬件侧认知链，再进入第 3 单元读 C 驱动。
