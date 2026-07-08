# 项目总览与定位

## 1. 本讲目标

本讲是整本学习手册的第一篇，面向从未接触过本项目的读者。学完本讲后，你应该能够：

- 用一句话说清 **FPGA-Imaging-Library（F-I-L）** 是什么、解决什么问题。
- 说出项目的作者、技术栈与开源许可证。
- 认识项目根目录下的 **七大功能分类**，以及每个分类里各自包含哪些 IP 核。
- 用一句话描述每个分类在「图像处理流水线」中承担的职责。
- 了解运行/仿真本项目需要的工具链（Python 2.7 + PIL、ModelSim 10.1+、Vivado）与基本仿真流程。

本讲只做「建立心智模型」的工作，不深入任何一段 RTL 细节——那是后续讲义的任务。

## 2. 前置知识

阅读本讲前，建议你大概了解以下概念（不懂也没关系，本讲会用通俗语言再解释一遍）：

- **FPGA（现场可编程门阵列）**：一种可以通过代码重新配置硬件电路的芯片。与 CPU 顺序执行不同，FPGA 适合做并行的、流式的数据通路处理，因此常用于图像/视频实时处理。
- **IP 核（Intellectual Property Core）**：在硬件设计中，IP 核是一段可复用、可参数化的硬件模块。把它理解成「硬件世界的函数库」即可——你调用它、配置参数，它就完成一个固定功能。
- **Verilog / SystemVerilog**：描述数字电路的硬件描述语言（HDL）。本项目的 IP 核主体用 Verilog 编写，部分仿真用 SystemVerilog。
- **Vivado**：Xilinx 公司的 FPGA 开发套件，用于综合、实现、生成比特流以及打包 IP 核。本项目目前只支持 Vivado。
- **仿真（Simulation）**：在不烧录真实芯片的前提下，用软件模拟硬件电路的运行，观察输入输出是否正确。本项目把仿真细分为「软件仿真（Python 参考实现）」和「功能仿真（ModelSim 跑 RTL）」两套，并自动比对两者结果。

## 3. 本讲源码地图

本讲涉及的文件很少，都是「说明性」文件，目的是先让你看清项目全貌：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 项目根 README，给出项目定位、使用方式与许可证。 |
| `TestOnBoard/README.md` | 板上测试总说明，列出在 Vivado + SDK 上跑通一个分类工程的步骤。 |
| `Point/ColorReversal/README.md` | 单个 IP（ColorReversal，颜色取反）的 README，同时也是「仿真流程标准模板」，所有 IP 的仿真步骤都长得和它一样。 |
| 根目录下的七个分类目录（`Point` / `Geometry` / `LocalFilter` / `Generator` / `Connector` / `InOut` / `BoardInit_AXI`） | 七大功能分类的物理载体，每个目录下挂着一组同类 IP。 |

> 提示：本讲引用的永久链接均指向当前 HEAD `c8cd350`。行号以该版本为准。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**项目 README**（项目是什么）、**功能分类目录**（项目里有哪些东西）、**仿真依赖说明**（怎么跑起来）。

### 4.1 项目 README —— 定位、形态与许可证

#### 4.1.1 概念说明

`README.md` 是项目的「门面」。它回答三个问题：

1. **项目是什么？** 一个面向 FPGA 的开源图像处理库。
2. **它以什么形态提供？** 所有操作都被封装成 **IP 核**，遵循同一种规范化接口，并支持「流水线模式」和「请求响应模式」两种使用方式。
3. **它是开源的，许可证是什么？** LGPL。

这里有两个关键词需要特别理解：

- **IP 核化**：意味着每个图像操作（取反、阈值、缩放、滤波……）都是一个独立、可参数化、可复用的硬件模块，而不是写死在某一个工程里的代码。这让你可以像搭积木一样把多个 IP 串成一条图像处理流水线。
- **统一接口 + 两种模式**：所有 IP 的参数名（如 `work_mode`、`color_channels`、`color_width`）和端口名（如 `clk`、`rst_n`、`in_enable`、`in_data`、`out_ready`、`out_data`）都是一致的；区别只在于 `work_mode` 取 0 还是 1，对应「来一个像素吐一个像素」的流水线模式，还是「你请求我才给」的请求响应模式。这是本项目的核心设计哲学，本讲只需建立印象，细节留给后续讲义。

#### 4.1.2 核心流程

从根 README 出发理解项目整体形态的流程是：

1. 读项目一句话定位 → 知道这是「FPGA 图像处理库」。
2. 读「How to use」→ 知道它以 IP 核形式提供，且目前只支持 Vivado。
3. 读许可证 → 知道可以自由使用、但需遵守 LGPL。
4. 进入各分类目录 → 看到具体 IP（下一模块展开）。

#### 4.1.3 源码精读

项目根 README 的核心定位段：

[README.md:17-19](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/README.md#L17-L19)

这段同时给出中英文定义，要点是：所有操作被封装为 IP 核、遵循统一接口、具备流水线和请求响应两种模式。

使用方式与平台支持：

[README.md:23-27](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/README.md#L23-L27)

这里说明了三件事：大部分 IP 都有软件仿真、功能仿真、板上测试三套验证；统一的文件结构和接口让仿真/测试很方便；IP 形式目前只支持 Xilinx Vivado。

许可证：

[README.md:40-42](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/README.md#L40-L42)

项目采用 **LGPL（GNU Lesser General Public License）**。LGPL 相比 GPL 更「宽松」：你可以把本库作为库链接进闭源项目而不必开源你的整个工程，但对库本身的修改仍需开源。这一点对嵌入式/商业项目尤其重要。仓库根目录的 `LICENSE` 文件第 1 行也确认了这是「GNU LESSER GENERAL PUBLIC LICENSE Version 2.1」。

#### 4.1.4 代码实践

1. **实践目标**：通过阅读根 README 建立对项目的一句话认知。
2. **操作步骤**：
   - 打开仓库根目录的 `README.md`，只读前 30 行。
   - 用你自己的话，写一句不超过 30 字的项目定位。
3. **需要观察的现象**：注意 README 是「中英文逐段对照」的排版风格。
4. **预期结果**：你的定位应包含「FPGA」「图像处理」「IP 核库」三个要素，例如「一个面向 Xilinx FPGA、以统一接口 IP 核形式提供的开源图像处理库」。

#### 4.1.5 小练习与答案

**练习 1**：F-I-L 把每个图像操作封装成什么形式？这样做的好处是什么？
**参考答案**：封装成 IP 核。好处是每个操作可复用、可参数化、接口统一，能像搭积木一样串成流水线，并方便单独仿真验证。

**练习 2**：项目支持哪两种使用模式？它们由哪个参数区分？
**参考答案**：流水线模式（pipeline）和请求响应模式（req-ack），由参数 `work_mode` 区分（0 / 1）。细节会在后续讲义展开。

**练习 3**：一个商业项目想把 F-I-L 作为库集成进自己的闭源固件，许可证允许吗？
**参考答案**：允许。LGPL 允许以库形式链接进闭源项目，只要不对 F-I-L 库本身的源码做闭源修改；若修改了库源码，则修改部分须按 LGPL 开源。

### 4.2 功能分类目录 —— 七大分类与各自职责

#### 4.2.1 概念说明

项目根目录下挂着七个分类目录，每个目录里是一组「职责相近」的 IP 核。理解这七个分类，就理解了 F-I-L 的能力地图。下面先给一张总表，再逐个说明。

| 分类目录 | 中文名 | 在图像处理流水线中的职责 |
| --- | --- | --- |
| `Point` | 点运算 | 对单个像素做逐点映射（取反、阈值、灰度化等）。 |
| `Geometry` | 几何变换 | 改变像素的空间位置（裁剪、镜像、平移、旋转、缩放、错切）。 |
| `LocalFilter` | 局部滤波 | 基于像素邻域窗口做运算（均值/秩值滤波、形态学、模板匹配、局部阈值）。 |
| `Generator` | 生成器 | 产生坐标、行、窗口、帧等「驱动数据」，是流水线的源头与节拍器。 |
| `Connector` | 连接器 | 对数据做整形与多路复用（位宽转换、通道合并/拆分、多路选择）。 |
| `InOut` | 输入输出硬件接口 | 与真实外设对接（摄像头、VGA 显示器、IIC 控制、BRAM 帧缓存）。 |
| `BoardInit_AXI` | 板级初始化（AXI） | 把所有 IP 经 AXI4-Lite 寄存器聚合，接入 Zynq PS 软核统一控制。 |

#### 4.2.2 核心流程

一条典型的「摄像头 → 处理 → 显示」流水线，可以这样映射到七大分类：

```text
[InOut: Cam/IIC] 采集图像
        │
        ▼
[Generator] 产生行/窗口/帧寻址，驱动数据流
        │
        ▼
[Point / Geometry / LocalFilter] 做实际图像处理（可多级串联）
        │
        ▼
[Connector] 位宽转换 / 通道合并 / 多路选择，适配下游
        │
        ▼
[InOut: VGA] 输出到显示器
        ▲
[BoardInit_AXI] 在底层把以上 IP 经 AXI 寄存器接入 PS 软核统一调度
```

要点：`Generator` 是「源头与节拍器」，`Point/Geometry/LocalFilter` 是「处理器」，`Connector` 是「粘合剂」，`InOut` 是「与真实世界的接口」，`BoardInit_AXI` 是「系统级总控」。`Connector` 和 `Generator` 本身不改变图像内容，但没有它们流水线无法成型。

#### 4.2.3 源码精读

下面列出每个分类目录下实际存在的 IP（本表依据当前 HEAD 的目录结构整理，可直接在仓库中核对）。每个分类目录的永久链接指向 GitHub 的 tree 视图：

- **Point（点运算）** —— [Point/](https://github.com/dtysky/FPGA-Imaging-Library/tree/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point)
  - `ColorReversal`（颜色取反）、`ContrastTransform`（对比度变换）、`Graying`（灰度化）、`LightnessTransform`（亮度变换）、`Threshold`（阈值二值化）。
- **Geometry（几何变换）** —— [Geometry/](https://github.com/dtysky/FPGA-Imaging-Library/tree/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Geometry)
  - `Crop`（裁剪）、`Mirror`（镜像）、`Pan`（平移）、`Rotate`（旋转）、`Scale`（缩放）、`Shear`（错切）。
- **LocalFilter（局部滤波）** —— [LocalFilter/](https://github.com/dtysky/FPGA-Imaging-Library/tree/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/LocalFilter)
  - `ErosionDilationBin`（二值形态学腐蚀/膨胀）、`MatchTemplateBin`（二值模板匹配）、`MeanFilter`（均值滤波）、`RankFilter`（秩值滤波）、`ThresholdLocal`（局部阈值）。
- **Generator（生成器）** —— [Generator/](https://github.com/dtysky/FPGA-Imaging-Library/tree/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Generator)
  - `CountGenerator`（计数生成）、`FrameController` / `FrameController2`（帧控制器）、`RowsGenerator`（行生成）、`WindowGenerator`（窗口生成）。
- **Connector（连接器）** —— [Connector/](https://github.com/dtysky/FPGA-Imaging-Library/tree/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Connector)
  - `ColorBin2Channels`、`ColorGray2Channels`、`ColorRGB16toRGB24`、`ColorRGB24toVGA`、`DataCombin2`、`DataCombin3`、`DataDelay`、`DataSplit4`、`DataWidthConvert`、`Mux2`、`Mux4`、`Mux8`、`Or8`。覆盖通道转换、数据合并/拆分/延迟、位宽转换与多路选择。
- **InOut（输入输出硬件接口）** —— [InOut/](https://github.com/dtysky/FPGA-Imaging-Library/tree/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/InOut)
  - `Bram8x320x240`（基于 BRAM 的 320×240 帧缓存）、`Cam`（摄像头接口）、`IIC_Ctrl`（IIC 控制器，常用于配置摄像头）、`VGA640x480`（640×480 VGA 时序输出）。
- **BoardInit_AXI（板级初始化 / AXI）** —— [BoardInit_AXI/](https://github.com/dtysky/FPGA-Imaging-Library/tree/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/BoardInit_AXI)
  - 这是一个「打包好的 AXI IP」而非一组独立 IP，目录内含 `bd`（Block Design）、`component.xml`（IP 打包描述）、`drivers`（PS 端 C 驱动）、`example_designs`、`hdl`、`xgui`（参数化 GUI 配置）。它把整条流水线的控制寄存器经 AXI4-Lite 暴露给 Zynq PS 软核。

> 说明：`BoardInit_AXI` 与前六个分类的组织方式不同——前六个是「一个目录装多个同类 IP」，它是「一个完整的 AXI 系统级 IP」。这是有意的分层：前面六类是「算法 IP」，`BoardInit_AXI` 是「系统接入层」。

以 `Point` 分类中最简单的 `ColorReversal` 为例，它的 README 也遵循统一模板（这份 README 同时是下一模块「仿真依赖说明」的依据）：

[Point/ColorReversal/README.md:1-6](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/README.md#L1-L6)

可以看到每个 IP 的 README 顶部都给出项目主页、源码地址与联系方式，保持一致的文档风格。

#### 4.2.4 代码实践

1. **实践目标**：亲手核对七大分类各自包含哪些 IP，并用一句话写出每个分类的职责。
2. **操作步骤**：
   - 克隆仓库后在根目录执行 `ls Point Geometry LocalFilter Generator Connector InOut BoardInit_AXI`（或直接在 GitHub 网页点开各分类目录）。
   - 为每个分类新建一行，格式：`分类名 → 包含的 IP 名 → 一句话职责`。
3. **需要观察的现象**：注意 `BoardInit_AXI` 目录下的内容（`bd`、`component.xml`、`drivers`…）与其他六个分类「装着多个 IP 子目录」的形式不同。
4. **预期结果**：得到一张与 4.2.3 节一致的总表。如本地结果与本表不一致，以本地仓库实际目录为准（仓库会持续更新，可能有新增 IP）。

#### 4.2.5 小练习与答案

**练习 1**：想把一张 RGB 图转成灰度图，应该用哪个分类的哪个 IP？
**参考答案**：`Point` 分类下的 `Graying`（灰度化属于逐像素的点运算）。

**练习 2**：均值滤波和腐蚀属于哪个分类？为什么不在 `Point`？
**参考答案**：属于 `LocalFilter`。因为它们需要像素的邻域窗口，而非单个像素独立计算；`Point` 只处理「一对一」的逐点映射。

**练习 3**：`BoardInit_AXI` 与前六个分类在组织方式上有什么本质不同？
**参考答案**：前六个分类是「一个目录装多个算法 IP」，`BoardInit_AXI` 是「一个完整的系统级 AXI IP」，内部是 `bd`/`drivers`/`hdl`/`xgui` 等打包结构，作用是把算法 IP 经 AXI4-Lite 寄存器接入 Zynq PS 软核统一控制。

### 4.3 仿真依赖说明 —— 工具链与仿真流程概览

#### 4.3.1 概念说明

F-I-L 的一个核心设计是「软硬一致性」：同一个图像操作，既有一个 **Python 软件参考实现**，又有一份 **Verilog 硬件实现**，两者用相同的测试图像跑，最后自动比对结果是否一致。为此每个 IP 都自带一套标准化的仿真脚本和目录。

跑通仿真需要三类工具链，各司其职：

| 工具链 | 版本要求 | 在仿真中承担的工作 |
| --- | --- | --- |
| Python + PIL（Python Imaging Library） | Python 2.7 | 生成软件参考结果、生成 HDL 仿真输入数据（`.dat`）、把 HDL 输出转回图像、比对报告。 |
| ModelSim | 10.1 及以上 | 跑 RTL 功能仿真，产出硬件侧输出数据。 |
| Xilinx Vivado | —— | 综合、实现、IP 打包、板上测试；同时为 ModelSim 提供 Xilinx 器件库。 |

> 注意：本项目脚本依赖 **Python 2.7**，而非 Python 3，且依赖 **PIL**（不是较新的 Pillow，尽管 Pillow 通常兼容）。这是历史遗留，初学者很容易在这里踩坑。

#### 4.3.2 核心流程

每个 IP 的仿真流程都是同一个「五步走」模板（以 `ColorReversal` 的 README 为准）：

1. **准备图像与配置**：把测试图像放进 `ImageForTest`，编辑 `conf.json` 设置仿真参数。
2. **软件仿真**：在 `SoftwareSim` 下运行 `sim.py`，得到软件参考结果（可在 `SimResCheck` 查看）。
3. **生成 HDL 仿真数据**：在 `HDLSimDataGen` 下运行 `creat.py`（注意脚本名是 `creat.py`，README 原文如此），把图像转成 `.dat` 供 ModelSim 读取。
4. **功能仿真**：用 ModelSim 打开 `FunSimForHDL` 下的 `.mpf` 工程，先 `vlib work`，再编译源文件，然后 `do Run.do`（看波形）或 `do RunOver.do`（只看结果）。
5. **比对**：在 `SimResCheck` 下运行 `covert.py`（把功能仿真结果转回图像）和 `compare.py`（生成软件仿真与功能仿真的比对报告）。

板上测试则是另一条路径，在 `TestOnBoard/` 下按分类组织工程（`.xpr`），用 Vivado 打开、`source bulid.tcl`、导出硬件、在 SDK 里写 C 驱动运行。

#### 4.3.3 源码精读

`ColorReversal` 的 README 明确给出仿真支持的图像类型与工具依赖：

[Point/ColorReversal/README.md:7-11](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/README.md#L7-L11)

要点：仿真只支持 RGB、灰度、二值三种图像；整个仿真流程依赖 **Python 2.7 和 PIL**。

软件仿真与数据生成两步：

[Point/ColorReversal/README.md:18-25](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/README.md#L18-L25)

这里能看到 `SoftwareSim/sim.py` 与 `HDLSimDataGen/creat.py` 两个脚本入口，以及「先 `SimResCheck` 看软件结果」的约定。

功能仿真与比对两步：

[Point/ColorReversal/README.md:27-31](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/README.md#L27-L31)

[Point/ColorReversal/README.md:52-55](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/README.md#L52-L55)

注意功能仿真要求 **ModelSim 10.1 以上**，并且需要先把 Vivado 的器件库编译进 ModelSim；最后用 `covert.py`（README 原文拼写如此，非 `convert.py`）和 `compare.py` 完成软硬结果比对。

板上测试的总说明在 `TestOnBoard/README.md`：

[TestOnBoard/README.md:1-7](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/TestOnBoard/README.md#L1-L7)

它给出从「Vivado 打开 `.xpr`」到「`source bulid.tcl`（原文拼写如此）→ 导出硬件 → 启动 SDK → 把 `ForBuild/Main.c` 拷进工程 → 配置运行」的完整链路，且 `TestOnBoard/` 下按 `Geometry` / `LocalFilter` / `Point` 三个分类各提供了一个板上工程。

#### 4.3.4 代码实践

1. **实践目标**：在不实际跑仿真的前提下，定位到一个 IP 的全部仿真脚本入口，画出「输入图像 → 各脚本 → 输出报告」的数据流。
2. **操作步骤**：
   - 进入 `Point/ColorReversal/` 目录。
   - 列出 `ImageForTest`、`SoftwareSim`、`HDLSimDataGen`、`FunSimForHDL`、`SimResCheck` 五个子目录里的文件名。
   - 对照 4.3.2 的五步流程，把每个脚本填到对应步骤里。
3. **需要观察的现象**：注意脚本名拼写（`creat.py`、`covert.py`、`bulid.tcl` 均为 README 原文拼写，使用时以仓库实际文件名为准）。
4. **预期结果**：得到一张「步骤 → 目录 → 脚本 → 产物」的对照表。例如：第 2 步 → `SoftwareSim` → `sim.py` → 软件参考结果图像。**待本地验证**：若你打算真正运行，需先备好 Python 2.7 + PIL 与 ModelSim 10.1+，本讲不假定你已经跑通。

#### 4.3.5 小练习与答案

**练习 1**：F-I-L 为什么同时维护 Python 软件实现和 Verilog 硬件实现？
**参考答案**：为了「软硬一致性」验证。Python 实现作为黄金参考（golden model），硬件实现跑完后与软件结果自动比对，从而在烧板子前就确认 RTL 行为正确。

**练习 2**：功能仿真对 ModelSim 的版本要求和前置准备是什么？
**参考答案**：要求 ModelSim 10.1 及以上；前置准备是先把 Xilinx Vivado 的器件库编译进 ModelSim（因为 RTL 中会例化 Xilinx 原语/IP）。

**练习 3**：`TestOnBoard/` 下提供了哪几个分类的板上工程？运行它需要哪两个 Xilinx 工具？
**参考答案**：提供了 `Geometry`、`LocalFilter`、`Point` 三个分类的板上工程；运行需要 Vivado（建工程、综合、导出硬件）和 SDK（编写/运行 C 驱动）。

## 5. 综合实践

**任务：绘制 F-I-L 的「能力地图 + 仿真闭环」一页纸。**

把本讲三个模块串起来，完成下面这份一页纸文档：

1. **能力地图**：画一张表，列出七大分类、每个分类的代表性 IP（每类至少 2 个）、一句话职责。
2. **流水线映射**：用箭头画出「摄像头采集 → Generator 驱动 → Point/Geometry/LocalFilter 处理 → Connector 整形 → VGA 输出」的示意图，并标注 `BoardInit_AXI` 在底层的总控角色。
3. **仿真闭环**：列出五步仿真流程对应的目录与脚本，并标明三类工具链（Python 2.7+PIL / ModelSim / Vivado）各自出现在哪几步。
4. **自检**：用一句话回答「F-I-L 是什么」，必须包含「FPGA」「图像处理」「统一接口 IP 核」「两种工作模式」四个要素。

完成后，你就拥有了一份可以随时回顾的项目全貌图，后续讲义都将在这张地图上逐层放大细节。

## 6. 本讲小结

- F-I-L 是一个面向 **Xilinx FPGA** 的开源 **图像处理 IP 核库**，采用 **LGPL** 许可证。
- 所有操作被封装成接口统一的 **IP 核**，支持 **流水线** 与 **请求响应** 两种工作模式（由 `work_mode` 区分）。
- 项目根目录分七大类：`Point`（点运算）、`Geometry`（几何变换）、`LocalFilter`（局部滤波）、`Generator`（生成器）、`Connector`（连接器）、`InOut`（硬件接口）、`BoardInit_AXI`（AXI 系统接入层）。
- 仿真遵循「软硬一致性」理念：Python 软件参考实现 + ModelSim 功能仿真 + 自动比对，依赖 **Python 2.7 + PIL**、**ModelSim 10.1+**、**Vivado** 三类工具链。
- 每个 IP 都有固定的子目录约定（`ImageForTest` / `SoftwareSim` / `HDLSimDataGen` / `FunSimForHDL` / `SimResCheck`），仿真流程统一为五步走。
- 板上测试在 `TestOnBoard/` 下按分类组织，走 Vivado + SDK 的 C 驱动链路。

## 7. 下一步学习建议

本讲只建立了项目全貌。建议按以下顺序继续：

1. **下一讲 `u1-l2`（目录结构与标准化 IP 文件夹布局）**：以 `ColorReversal` 为例，深入单个 IP 内部的 `HDL/srcs`、仿真脚本、结果校验等子目录的精细分工——这是后续读懂任何 IP 的前提。
2. 随后阅读 `u1-l3`（工具链与仿真运行方式）真正跑通一次仿真闭环。
3. 再进入 `u1-l4`（标准化 IP 接口与两种工作模式）理解 `work_mode` / `in_enable` / `out_ready` 的时序语义。
4. 想先获得全局视野的话，可顺带浏览 `TestOnBoard/README.md` 和 `BoardInit_AXI/` 目录，了解系统级接入方式（这部分会在专家层讲义详细展开）。
