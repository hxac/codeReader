# 仓库结构与四大组件

## 1. 本讲目标

UHD 并不是一个单一的二进制文件，而是一整套「横跨主机与设备端」的软件集合。学完本讲，你应当能够：

- 说出 UHD 仓库顶层六大目录（`host`、`mpm`、`firmware`、`fpga`、`images`、`tools`）各自的职责。
- 区分**哪些代码运行在你的电脑（主机）上**，**哪些代码运行在 USRP 设备内部**。
- 理解这些组件如何协作，构成一台可工作的 USRP 收发系统。
- 在仓库里迅速定位「我要找的某段逻辑大概在哪个目录」。

本讲只看「目录门牌」和「README 自述」，不深入任何源码逻辑。这是后续所有讲义的地理坐标——先把地图看熟，再逐区探索。

## 2. 前置知识

在开始前，请确认你已经掌握上一讲（u1-l1）建立的几个认知：

- **USRP** 是 Ettus Research 制造的 SDR 硬件平台；**UHD** 是运行在主机上的开源驱动与 API。
- UHD 用「统一的软件接口」屏蔽不同型号 USRP 硬件的差异。
- 仓库整体采用 GPLv3 许可证（`fpga/` 另有规定），升级前要查 `CHANGELOG`。

本讲会用到的两个通俗概念：

- **主机（host）**：你手边那台运行 GNU Radio、srsRAN 或自写程序的电脑。UHD 库就装在这里。
- **设备端（device）**：USRP 硬件盒子本身。它内部通常既有 **微处理器（跑固件/MPM）**，又有 **FPGA（跑硬件描述语言镜像）**。

记住一条主线：**主机程序通过 UHD 库发指令 → 设备端的 MPM/固件/FPGA 协同执行 → 射频信号被收发。** 本讲的任务就是把这条链路上每个「零件」对应到仓库里的目录。

## 3. 本讲源码地图

本讲涉及的关键文件，都来自仓库的「门牌文件」（README 与顶层 CMake），它们最能反映目录职责：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 仓库总说明，其中有一节 **Directories** 集中介绍六大顶层目录。 |
| `host/README.md` | `host/` 目录的自述，说明主机库「完全运行在用户空间」。 |
| `host/CMakeLists.txt` | `host/` 的 CMake 构建入口，定义了项目名与默认构建类型。 |
| `mpm/CMakeLists.txt` | `mpm/` 的 CMake 构建入口，从它能看出 MPM 与 host 的依赖关系。 |
| `firmware/README.md` | 列出各类微处理器固件及其对应的设备与工具链。 |
| `fpga/README.md` | 按「代（generation）」组织 FPGA HDL 源码的说明。 |
| `images/README.md` | 镜像打包构建器（maintainer 工具）的说明。 |
| `tools/README.md` | 调试辅助工具的说明，并强调这些工具**不属于 UHD 本体**。 |

## 4. 核心概念与源码讲解

本讲按「最小模块」拆分为六个目录，每个目录对应一节。最后用一张总览表把它们串起来。

### 4.1 host —— 主机端用户空间驱动（本手册主角）

#### 4.1.1 概念说明

`host/` 是**整个仓库里你最常打交道的目录**，也是本学习手册绝大部分讲义的对象。它构建出的产物是 **UHD 库（`libuhd`）**——一个运行在你电脑上的 C++ 共享库，外加一组命令行工具和示例程序。

它的关键定位写得很清楚：「这一目录树构建出运行于主机上的 UHD 软件库，该库**完全运行在用户空间（user-space）**」。也就是说，它不需要内核驱动、不要求 root 权限（除个别 USB/网络权限配置外），就是一个普通的用户态库。

`host/` 内部的进一步划分（后面讲义会逐个深入）：

| 子目录 | 职责 |
| --- | --- |
| `include/` | 公共 API 头文件（C++ 与 C 两套）。u1-l4 会专门导览。 |
| `lib/` | 库的实现代码（设备驱动、RFNoC、传输层、转换器等）。 |
| `examples/` | 示例程序，如 `rx_samples_to_file`。u1-l6 会精读。 |
| `utils/` | 命令行工具，如 `uhd_find_devices`、`uhd_usrp_probe`。u1-l5 会讲。 |
| `tests/` | 单元测试。 |
| `python/` | 基于 pybind11 的 Python 绑定（pyuhd）。 |
| `cmake/` | CMake 模块脚本。 |
| `docs/` | Doxygen 文档源。 |

#### 4.1.2 核心流程

从「源码」到「可用库」的流程：

```text
host/CMakeLists.txt  (配置项目、检测依赖、设默认 Release)
        │
        ├── host/include/   → 安装为公共头文件
        ├── host/lib/       → 编译链接成 libuhd.so / uhd.dll
        ├── host/utils/     → 编译成 uhd_* 命令行可执行文件
        └── host/examples/  → 编译成示例可执行文件
```

最终你的应用程序通过 `#include <uhd/...>` 链接 `libuhd`，即可驱动 USRP。

#### 4.1.3 源码精读

仓库总 README 的 **Directories** 一节对 `host/` 的一句话定义：

[README.md:L57-L59](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/README.md#L57-L59) —— 说明 `host/` 是「用户空间驱动（user-space driver）的源码」。

`host/README.md` 把「用户空间」这点讲得更明确：

[host/README.md:L4-L7](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/README.md#L4-L7) —— 「The UHD library runs entirely in user-space」（UHD 库完全运行在用户空间）。

`host/CMakeLists.txt` 顶部的项目声明与默认构建类型：

[host/CMakeLists.txt:L21-L25](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/CMakeLists.txt#L21-L25) —— 若未指定构建类型，**默认使用 `Release`** 以获得优化。

[host/CMakeLists.txt:L43](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/host/CMakeLists.txt#L43) —— `project(UHD CXX C)`，声明这是一个 C++/C 混合项目，名为 `UHD`。（构建系统的细节留到 u1-l3 详讲。）

#### 4.1.4 代码实践

**实践目标**：建立对 `host/` 内部布局的直观认识。

**操作步骤**：

1. 打开 `host/README.md`，阅读开头 3 段。
2. 在仓库根目录列出 `host/` 下的内容（参考本讲 4.1.1 的表格）。
3. 把 `host/` 的子目录分成三类：「公共接口」「实现」「可执行程序入口」。

**需要观察的现象**：你会发现 `include/`、`lib/` 是库的两半，而 `utils/` 和 `examples/` 都会产出可执行文件。

**预期结果**：得到一张类似下面的分类：

```text
公共接口： include/
实现代码：  lib/
可执行入口：utils/  examples/
测试与绑定：tests/  python/
构建与文档：cmake/  docs/
```

> 说明：本实践为「源码阅读型」，无需硬件，也无需编译。

#### 4.1.5 小练习与答案

**练习 1**：为什么 UHD 强调「运行在用户空间」？这给开发者带来什么好处？
**参考答案**：用户空间库不需要写内核驱动、不需要随内核升级而重编，部署和调试都更简单；普通用户权限即可运行，降低了使用门槛。

**练习 2**：`host/utils/` 和 `host/examples/` 都产出可执行文件，它们的区别是什么？
**参考答案**：`utils/` 是官方提供的**实用/诊断工具**（如发现设备、探测子板），属于日常运维工具；`examples/` 是**教学样例**，演示如何用 UHD API 写一个收发程序，供学习者参考。

---

### 4.2 mpm —— 设备端外设管理进程

#### 4.2.1 概念说明

`mpm/` 是 **Module Peripheral Manager（模块外设管理器）** 的缩写。与 `host/` 不同，**这里的代码不是跑在你电脑上的，而是跑在 USRP 设备内部的嵌入式处理器上**（例如 X 系列、N 系列设备里的 ARM SoC）。

它扮演「设备端大管家」的角色：设备上电后，MPM 作为一个常驻进程启动，负责管理设备上的各类外设（射频子板、时钟芯片、网络接口、传感器等），并**等待主机侧的 UHD 来连接它**。现代 USRP（N3xx、X3xx、X4xx 等）的主机驱动 `mpmd`（见 u4-l4）就是通过网络与设备上的 MPM 通信的。

MPM 主要用 **Python** 编写（这是它与固件/FPGA 的关键区别），辅以少量 C++。它的核心模块都在 `mpm/python/usrp_mpm/` 下，入口守护脚本之一是 `mpm/python/usrp_hwd.py`。

#### 4.2.2 核心流程

MPM 在系统中的位置：

```text
[主机]                              [USRP 设备]
  你的程序                             上电
    │                                   │
  libuhd (含 mpmd 驱动)  ──网络/PCIe──▶  MPM 进程 (mpm/)  ──管理──▶  射频/时钟/传感器外设
                                          │
                                       (也会借助 firmware/FPGA)
```

要点：**主机上的 UHD 把命令发给设备上的 MPM，再由 MPM 操作具体硬件。**

#### 4.2.3 源码精读

仓库总 README 对 `mpm/` 的定义：

[README.md:L61-L64](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/README.md#L61-L64) —— 「module peripheral manager (MPM) 的源码，**这是运行在嵌入式设备上的代码**」。

`mpm/CMakeLists.txt` 暴露了两条重要信息。第一，MPM 项目同时包含 C/C++ 与 Python：

[mpm/CMakeLists.txt:L9](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/mpm/CMakeLists.txt#L9) —— `project(MPM C CXX)`，注释里写明「Also has Python」。

第二，MPM 的构建**直接依赖 host 目录**：

[mpm/CMakeLists.txt:L20-L22](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/mpm/CMakeLists.txt#L20-L22) —— `set(UHD_SOURCE_DIR ${CMAKE_SOURCE_DIR}/../host)`，即 MPM 复用了 `host/cmake/Modules` 里的 CMake 脚本。这说明 host 与 mpm 虽分居两处，却是同一套构建体系。

#### 4.2.4 代码实践

**实践目标**：从构建脚本里找到「MPM 依赖 host」的硬证据，加深对二者关系的理解。

**操作步骤**：

1. 打开 `mpm/CMakeLists.txt`，找到 `UHD_SOURCE_DIR` 的定义。
2. 跟踪它随后如何被加入 `CMAKE_MODULE_PATH`（同一段代码）。
3. 浏览 `mpm/python/` 目录，确认 `usrp_mpm/`、`usrp_hwd.py` 等 Python 入口确实存在。

**需要观察的现象**：MPM 的 CMake 会把 `../host/cmake/Modules` 插到模块搜索路径最前面。

**预期结果**：你能用自己的话解释——「MPM 跑在设备端，但它的构建脚本借用了 host 的 CMake 基础设施，二者是同源的」。

> 说明：无需真实设备，仅阅读脚本即可。

#### 4.2.5 小练习与答案

**练习 1**：MPM 与 host 库分别运行在哪里？
**参考答案**：host 库运行在**主机电脑**的用户空间；MPM 运行在 **USRP 设备内部的嵌入式处理器**上，作为常驻进程管理设备外设。

**练习 2**：为什么 MPM 选择主要用 Python 编写，而不是 C？
**参考答案**：MPM 运行在设备端，开发者能完全掌控其运行环境（见 `mpm/CMakeLists.txt` 注释「we control the build environment」），用 Python 更便于快速迭代、对接各种外设驱动与配置；性能关键的底层交互则交给 C++ 与 FPGA。

---

### 4.3 firmware —— 设备端微处理器固件

#### 4.3.1 概念说明

`firmware/` 是「**USRP 硬件里所有微处理器（microprocessor）的固件源码**」。固件（firmware）是一类很底层的程序，通常烧录到某个专用 MCU/USB PHY 芯片里，负责「上电后最早期的那些事」，比如 USB 枚举、与主机建立最初连接、控制时钟等。

需要把它和 MPM、FPGA 区分开：

- **firmware**：跑在**专用小芯片**上的程序（如 USB PHY、OctoClock 的 AVR）。
- **MPM**：跑在**嵌入式 SoC**上的高级管理进程（Python 为主）。
- **FPGA**：跑在 **FPGA 芯片**里的硬件逻辑（Verilog 为主）。

`firmware/README.md` 按芯片/设备把固件分了若干子目录，例如 `fx2`、`fx3`、`octoclock` 等。

#### 4.3.2 核心流程

固件在设备启动流程中的位置：

```text
设备上电
   │
   ├── 固件 (firmware/)  在 USB PHY / 小 MCU 上启动
   │      └─ 让设备能被主机识别（USB 枚举等基础能力）
   │
   ├── FPGA 镜像 (fpga/)  加载进 FPGA
   │
   └── MPM (mpm/)  在 SoC 上启动，接管高级外设管理
```

> 这三者在不同设备上的组合方式不同；现代设备主要靠 MPM + FPGA，老设备更依赖固件。

#### 4.3.3 源码精读

仓库总 README 对 `firmware/` 的定义：

[README.md:L66-L68](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/README.md#L66-L68) —— 「**USRP 硬件中所有微处理器**的源码」。

`firmware/README.md` 列出了每类固件对应的设备与工具链，典型几条：

[firmware/README.md:L4-L10](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/firmware/README.md#L4-L10) —— `fx2/` 是 **FX2 USB PHY** 的固件，用于 USRP1 与 B100；工具链是 `sdcc` + `cmake`。

[firmware/README.md:L19-L25](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/firmware/README.md#L19-L25) —— `fx3/` 是 **FX3 USB PHY** 的固件，用于 USRP B200/B210；工具链是 Cypress FX3 SDK。

[firmware/README.md:L31-L37](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/firmware/README.md#L31-L37) —— `octoclock/` 是 **OctoClock** 时钟分发设备的固件；工具链是 `avrtools`。

#### 4.3.4 代码实践

**实践目标**：建立「固件子目录 ↔ 设备 ↔ 工具链」的映射。

**操作步骤**：

1. 打开 `firmware/README.md`，逐节阅读每个子目录的「Description / Devices / Tools」三行。
2. 用一张表把 `fx2`、`fx3`、`octoclock` 三类固件对应的设备和工具链填进去。
3. 对照仓库里 `firmware/` 的实际子目录（如 `e300`、`fx2`、`fx3`、`octoclock`、`usrp2`、`usrp3`），确认它们确实存在。

**需要观察的现象**：你会发现不同固件需要**完全不同的工具链**（sdcc / Cypress SDK / avrtools / zpu-gcc）。

**预期结果**：得到一张类似下表的映射：

| 固件子目录 | 设备 | 工具链 |
| --- | --- | --- |
| `fx2/` | USRP1, B100 | sdcc, cmake |
| `fx3/` | USRP B200, B210 | Cypress FX3 SDK |
| `octoclock/` | OctoClock | avrtools, cmake |

> 说明：本实践仅阅读 README 与列目录，无需安装任何工具链。

#### 4.3.5 小练习与答案

**练习 1**：固件与 MPM 都是「设备端的程序」，本质区别是什么？
**参考答案**：固件跑在**专用小芯片**（如 USB PHY、AVR）上，做最底层的早期初始化（USB 枚举、时钟控制等），体量小、用专用工具链编译；MPM 跑在**嵌入式 SoC** 上，是功能丰富的高级管理进程（Python 为主），负责大量外设的运行时管理。

**练习 2**：你的 USRP B210 用 USB 连电脑，它的 USB 能力由仓库里哪部分代码提供？
**参考答案**：由 `firmware/fx3/`（FX3 USB PHY 固件）配合主机侧 UHD 的 USB 传输层共同提供；设备端最早由 fx3 固件让 USB 接口可用。

---

### 4.4 fpga —— 设备端 FPGA HDL 镜像

#### 4.4.1 概念说明

`fpga/` 是 **USRP FPGA 镜像的硬件描述语言（HDL）源码**，绝大部分用 **Verilog** 编写。FPGA 是 USRP 上负责「实时、高速」数字信号处理的核心芯片：数字上下变频（DDC/DUC）、样本打包/解包、与主机之间的高速数据流（CHDR/RFNoC 通路）都在这里以硬件逻辑实现。

FPGA 镜像决定了设备能跑多高的数据率、支持哪些 RFNoC 块。它由厂商工具（Xilinx Vivado/ISE、Altera Quartus）综合布线后，烧录或加载进设备。

`fpga/README.md` 按设备的「**代（generation）**」组织源码：`usrp1`（第一代）、`usrp2`（第二代）、`usrp3`（第三代）。

#### 4.4.2 核心流程

FPGA 镜像在数据通路中的位置（接收方向示意）：

```text
天线 → 射频前端 →【FPGA: DDC 降采样 + 打包成 CHDR 包】→ 高速链路 → 主机 libuhd → 你的程序
                       ▲
                 fpga/ 里的 Verilog 逻辑（这里发生）
```

FPGA 做的是「线速」处理，是整个链路里吞吐量最大、延迟最低的一环。

#### 4.4.3 源码精读

仓库总 README 对 `fpga/` 的定义：

[README.md:L70-L72](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/README.md#L70-L72) —— 「UHD FPGA 镜像的源码」。

`fpga/README.md` 说明本目录是开源 HDL，并按代划分：

[fpga/README.md:L4-L7](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/fpga/README.md#L4-L7) —— 「本仓库包含 USRP 平台**自由开源的 FPGA HDL**，大部分用 Verilog 编写」。

[fpga/README.md:L10-L12](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/fpga/README.md#L10-L12) —— 「本仓库包含以下几代 USRP 设备的 FPGA 源码」。

其中第三代（`usrp3`）覆盖了现代主力设备：

[fpga/README.md:L29-L35](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/fpga/README.md#L29-L35) —— `usrp3/` 对应 USRP B2X0、X 系列、E3X0、N3xx；工具为 Vivado/ISE。

另外，README 还指明：预编译镜像不在此处，应通过 `uhd_images_downloader` 下载（见 4.5）。

#### 4.4.4 代码实践

**实践目标**：建立「FPGA 代 ↔ 设备 ↔ 综合工具」的映射。

**操作步骤**：

1. 打开 `fpga/README.md`，阅读 Generation 1/2/3 三个小节。
2. 列出 `usrp1`、`usrp2`、`usrp3` 各自对应的设备家族与厂商工具。
3. 注意 README 中关于 `uhd_images_downloader` 的说明：理解「源码在此，预编译镜像需另下」的分工。

**需要观察的现象**：第三代 `usrp3` 涵盖了最多的现代设备，这正是 RFNoC 架构（u3 单元）的主战场。

**预期结果**：得到一张代际映射表：

| FPGA 子目录 | 代表设备 | 综合工具 |
| --- | --- | --- |
| `usrp1/` | USRP Classic | Quartus (Altera) |
| `usrp2/` | N2X0, B100, E1X0, USRP2 | ISE (Xilinx) |
| `usrp3/` | B2X0, X 系列, E3X0, N3xx | Vivado/ISE (Xilinx) |

> 说明：仅阅读 README，无需安装 Vivado。

#### 4.4.5 小练习与答案

**练习 1**：FPGA 镜像和主机上的 UHD 库，谁负责「实时高速」的样本处理？为什么？
**参考答案**：FPGA 镜像负责。FPGA 是硬件逻辑，能以「线速」并行处理高速数据流（DDC/DUC、打包），延迟低、吞吐高；主机 UHD 库受操作系统调度与 CPU 算力限制，适合做控制面与较慢的数据面处理。

**练习 2**：为什么仓库同时提供 FPGA **源码**和（通过下载器获取的）**预编译镜像**？
**参考答案**：源码供二次开发与定制（改信号链、加 RFNoC 块）的人使用；大多数用户只需要标准功能，直接用预编译镜像省去昂贵的综合工具与漫长编译时间。

---

### 4.5 images —— 镜像打包构建器（maintainer 向）

#### 4.5.1 概念说明

`images/` **不是某个运行时组件，而是一套「打包脚本」**。它的作用是把前面 `firmware/` 和 `fpga/` 产出的各种二进制镜像，整理、压缩、打包成可分发的「镜像包（images package）」，供发布和下载。

README 明确指出：这里的脚本主要面向 **UHD 维护者和开发者**，普通用户通常用不到——普通用户用的是 `uhd_images_downloader` 工具去下载已经打好的包。

关键文件如 `create_imgs_package.py`（制作发布包）、`populate_images.py`、`manifest.txt`（镜像清单）。

#### 4.5.2 核心流程

镜像的「生产—分发—使用」链路：

```text
源码：firmware/ + fpga/
      │ (各工具链编译/综合)
      ▼
二进制镜像
      │
      ▼ images/ 脚本打包（create_imgs_package.py + manifest.txt）
镜像包（tar/zip）
      │ (发布到服务器)
      ▼
普通用户：uhd_images_downloader 下载并安装到本机
```

`images/` 处于「打包」这一中间环节。

#### 4.5.3 源码精读

仓库总 README 对 `images/` 的定义：

[README.md:L74-L78](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/README.md#L74-L78) —— 「FPGA 与固件镜像的打包构建器；这里的脚本主要与 **UHD 维护者和开发者**相关」。

`images/README.md` 进一步说明用途与边界：

[images/README.md:L4-L6](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/images/README.md#L4-L6) —— 「images 目录是辅助准备镜像包的工具，**实际的 FPGA 镜像构建不在这里完成**」。

[images/README.md:L13-L18](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/images/README.md#L13-L18) —— 在 release tag 上运行 `./create_imgs_package.py` 可创建用于上传到 GitHub 的镜像包。

#### 4.5.4 代码实践

**实践目标**：厘清「打包脚本」与「镜像构建」的边界。

**操作步骤**：

1. 打开 `images/README.md`，注意它反复强调「这里只负责打包，不负责构建 FPGA 镜像」。
2. 浏览 `images/` 目录，找到 `create_imgs_package.py`、`populate_images.py`、`manifest.txt` 三个文件。
3. 打开 `manifest.txt`，感受它如何作为「镜像清单」列出各设备需要的文件。

**需要观察的现象**：`manifest.txt` 里会列出大量按设备/版本组织的镜像文件名，这正是「打包」要归拢的对象。

**预期结果**：你能解释——「`images/` 是 maintainer 的打包流水线脚本，普通用户基本用不到，普通用户用 `uhd_images_downloader`」。

> 说明：仅阅读脚本与清单，无需运行打包流程。

#### 4.5.5 小练习与答案

**练习 1**：普通用户升级设备镜像，需要 clone 仓库并用 `images/` 脚本自己打包吗？
**参考答案**：不需要。`images/` 面向维护者。普通用户直接运行 `uhd_images_downloader` 即可下载并安装预编译好的官方镜像包。

**练习 2**：`images/README.md` 为什么强调「实际 FPGA 镜像构建不在本目录完成」？
**参考答案**：因为真正把 Verilog 综合成比特流，需要 Xilinx/Altera 厂商工具，且流程在 `fpga/` 体系内完成；`images/` 只是把已经产出的二进制镜像归拢、压缩、生成清单与发布包，是纯粹的「打包」步骤。

---

### 4.6 tools —— 独立调试工具（不属于 UHD 本体）

#### 4.6.1 概念说明

`tools/` 是一组**辅助调试工具**。它最容易和 `host/utils/` 混淆，所以必须强调 README 里的一句关键话：**「本目录里的工具不属于 UHD」**。它们要么是独立小程序，要么是给第三方软件用的插件。

如果你要找的是 UHD **官方自带的命令行工具**（如 `uhd_find_devices`），那应该去 `host/utils/`（u1-l5 会讲），而不是这里。这里的 `tools/` 是更外围的、调试性质的工具集合，例如 Wireshark 的 CHDR 包解析插件、X3xx 的 JTAG 烧写脚本、压测连接的 `kitchen_sink` 等。

#### 4.6.2 核心流程

`tools/` 与 `host/utils/` 的定位区分：

```text
需要官方 UHD 命令行工具？  → host/utils/  (随 libuhd 一起安装，属于 UHD)
需要外围/调试/第三方工具？ → tools/        (独立程序，不属于 UHD 本体)
```

#### 4.6.3 源码精读

仓库总 README 对 `tools/` 的定义：

[README.md:L80-L83](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/README.md#L80-L83) —— 「附加工具，主要用于调试；详见该目录的 readme」。

`tools/README.md` 直接划清了边界，并指引你去正确的位置找 UHD 工具：

[tools/README.md:L4-L8](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/tools/README.md#L4-L8) —— 「这里的工具**不是 UHD 的一部分**……若要找 UHD 软件工具，请看 `uhd/host/utils`」。

它还列举了几个具体工具，例如 Wireshark 的 CHDR 包解析插件：

[tools/README.md:L13-L17](https://github.com/EttusResearch/uhd/blob/2af4ddb96219a99d2300804830e0971f79557b23/tools/README.md#L13-L17) —— `chdr-dissector/` 是 Wireshark 插件，用于查看 **CHDR（Compressed HeaDeR）格式**数据包，B2xx 与 X3xx 设备使用该格式。

#### 4.6.4 代码实践

**实践目标**：准确区分 `tools/`（外围调试工具）与 `host/utils/`（UHD 官方工具）。

**操作步骤**：

1. 打开 `tools/README.md`，留意它明确声明「这些工具不属于 UHD」。
2. 浏览 `tools/` 目录，记下几个工具名（如 `chdr-dissector/`、`kitchen_sink/`、`usrp_x3xx_fpga_jtag_programmer.sh`）。
3. 对比 `host/utils/` 目录（可只看文件名清单），体会两者性质不同。

**需要观察的现象**：`tools/` 里多是脚本、插件、压测程序；`host/utils/` 里多是 `uhd_` 开头的官方工具源码。

**预期结果**：你能给出一句口诀——「官方工具去 `host/utils/`，外围调试去 `tools/`」。

> 说明：仅阅读 README 与列目录，无需运行任何工具。

#### 4.6.5 小练习与答案

**练习 1**：你想用 Wireshark 抓包分析 USRP 与主机之间的 CHDR 数据流，该用仓库哪部分？
**参考答案**：用 `tools/chdr-dissector/`，它是专门为 Wireshark 写的 CHDR 包解析插件。

**练习 2**：为什么 README 要特意提醒「这里的工具不属于 UHD」？
**参考答案**：避免用户误以为这些调试脚本是 UHD 库的组成部分、随库安装或受库的版本/许可证约束；它们是独立维护的外围工具，定位、发布方式都与 `libuhd` 不同。

---

### 4.7 六大组件协作总览

把六个目录放在一起，按「运行位置」归类：

| 目录 | 运行位置 | 性质 | 一句话职责 |
| --- | --- | --- | --- |
| `host/` | **主机**（用户空间） | C++ 库 + 工具 + 示例 | UHD 驱动与 API，本手册主角 |
| `mpm/` | **设备端**（嵌入式 SoC） | Python 为主 | 外设管理常驻进程，与主机 mpmd 通信 |
| `firmware/` | **设备端**（专用 MCU/PHY） | C/汇编 | 各微处理器的底层固件 |
| `fpga/` | **设备端**（FPGA 芯片） | Verilog HDL | 实时高速数字信号处理与数据通路 |
| `images/` | **主机**（maintainer 用） | 脚本 | 打包固件/FPGA 镜像成可分发包 |
| `tools/` | **主机**（调试用） | 脚本/插件 | 外围调试工具，不属于 UHD 本体 |

一个完整的「主机程序收发样本」动作，跨越的组件：

```text
你的程序 → host/lib (libuhd) → 网络/USB → 设备端 MPM (mpm/) → firmware/ + fpga/ 协同 → 射频
                              ◀──────────  样本回流  ────────────
```

记住：**本学习手册后续几乎所有讲义，都聚焦在最左边的 `host/` 目录。** 把 mpm/firmware/fpga 当作「设备端黑盒」先有概念即可，需要时再回头深入。

## 5. 综合实践

**任务**：绘制一张 UHD 仓库目录树，标注每个顶层目录的职责，并区分主机端 / 设备端代码。

**步骤**：

1. 用 `ls` 或文件浏览器，把仓库根目录的六大目录（`host`、`mpm`、`firmware`、`fpga`、`images`、`tools`）画成一棵树。
2. 在每个目录旁，用一句话写出它的职责（参考本讲 4.7 的总览表）。
3. 用两种颜色（或标记 🖥️ / 📦）区分：哪些**运行在主机**，哪些**运行在设备**，哪些是**打包/调试工具**。
4. 用箭头画出一次「主机发送 → 设备执行 → 射频」的跨组件调用链。

**参考产出（你可以照此格式画自己的版本）**：

```text
uhd/
├── host/      🖥️ 主机    C++ UHD 库 + 工具 + 示例（本手册主角）
├── mpm/       📦 设备端  外设管理进程（Python 为主，与 host/lib 通信）
├── firmware/  📦 设备端  微处理器固件（USB PHY / OctoClock / 等）
├── fpga/      📦 设备端  FPGA HDL 镜像（Verilog，实时信号处理）
├── images/    🖥️ 主机    打包脚本（maintainer 向，生成镜像发布包）
└── tools/     🖥️ 主机    外围调试工具（不属于 UHD 本体）

跨组件调用链：
  程序 → host/lib ──网络/USB──▶ mpm/ → firmware/ + fpga/ → 射频
```

**自检问题**（不必写进作业，心里回答即可）：如果你只关心「怎么用 C++ 写一个收发程序」，你应该重点看哪个目录？为什么？（答：`host/`，因为 libuhd 与示例都在这里，其余是设备端或工具链。）

> 说明：本实践无需硬件、无需编译，纯目录梳理与画图。如果你能不查资料就画出上面这张树并标对运行位置，本讲就过关了。

## 6. 本讲小结

- UHD 仓库顶层有 **六大目录**：`host/`、`mpm/`、`firmware/`、`fpga/`、`images/`、`tools/`。
- **`host/`** 是运行在主机用户空间的 UHD 库，是本学习手册的主角；其余多数目录在设备端。
- **`mpm/`** 是设备端嵌入式 SoC 上的外设管理进程（Python 为主），现代设备靠它与主机驱动 `mpmd` 通信。
- **`firmware/`**（微处理器固件）与 **`fpga/`**（FPGA HDL 镜像）都在设备端，分别负责底层初始化与实时高速信号处理。
- **`images/`** 是 maintainer 的镜像打包脚本；**`tools/`** 是不属于 UHD 本体的外围调试工具——两者都跑在主机，但都不是驱动本体。
- 区分运行位置（主机 🖥️ / 设备 📦）是理解整个仓库的关键视角。

## 7. 下一步学习建议

下一讲 **u1-l3（构建系统：CMake 构建流程与依赖）** 将带你进入 `host/CMakeLists.txt` 的内部，理解 libuhd 是如何被配置、检测依赖并编译出来的。在那之后：

- **u1-l4（公共 API 头文件全景）** 会导览 `host/include/uhd/` 的头文件布局。
- **u1-l5（命令行工具导览）** 会讲 `host/utils/` 下的 `uhd_config_info` 等官方工具。
- **u1-l6（第一个示例 rx_samples_to_file）** 会带你跑通一个完整接收程序。

建议你先用本讲建立的「目录地图」四处浏览一遍 `host/`，特别是 `host/include/` 和 `host/examples/`，为下一讲的构建系统讲解建立感性认识。
