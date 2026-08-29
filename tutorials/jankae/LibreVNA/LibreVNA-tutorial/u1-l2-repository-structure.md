# 仓库地图：目录结构与三大组件

## 1. 本讲目标

上一讲我们从 RF 框图层面理解了 LibreVNA 的硬件架构。这一讲我们把视角拉回代码仓库本身，解决一个很实际的问题：**面对一个同时包含 PC 程序、单片机固件和 FPGA 逻辑的"三合一"仓库，我该去哪里找东西？**

学完本讲，你应该能够：

1. 画出 `Software/PC_Application`、`Software/VNA_embedded`、`FPGA` 三个子树各自的职责图，说清楚它们分别对应三大组件中的哪一个。
2. 拿到一个功能需求（例如"频谱分析仪的扫描设置界面在哪""Si5351C 的驱动在哪""DFT 模块在哪"），不用搜索引擎，直接在目录树中定位到对应的目录甚至文件。
3. 识别仓库中的文档资产：用户手册、SCPI 编程指南、三份协议文档（USB / Device / FPGA）分别放在哪里。

## 2. 前置知识

本讲几乎不需要编程基础，但需要理解上一讲建立的几个概念（这里简要复习）：

- **三大组件**：LibreVNA = **PC 端 Qt GUI 程序**（校准、绘图、数学运算都在这里）+ **STM32G431 单片机固件**（运行 FreeRTOS，负责调度）+ **Spartan 6 FPGA 逻辑**（VHDL 编写，贴着 ADC 做实时采样和射频时序控制）。
- **PCB 只是射频前端**：这是 README 中原话的含义——昂贵的"处理"全部发生在 PC 上，所以 GUI 子树是整个仓库中代码量最大、功能最丰富的部分。
- **数据链路**：FPGA → MCU → USB → PC GUI。三个子树正好对应这条链路上的三站。
- **.pro 文件**：Qt 的 qmake 工程文件，相当于这个项目的"Makefile 前置描述"，列出了工程包含哪些头文件、源文件、UI 文件和依赖库。
- **VHDL**：一种硬件描述语言，用来写 FPGA 逻辑；`.vhd` 是它的源文件后缀。
- **CubeMX / USER CODE 区**：STM32 固件由 ST 的图形化工具 CubeMX 生成外设初始化代码，生成代码中用 `/* USER CODE BEGIN ... */ ... /* USER CODE END ... */` 标记出"允许人工修改"的区段，工具重新生成时不会覆盖这些区段。

## 3. 本讲源码地图

本讲涉及的关键文件如下（均已在正文中配合永久链接讲解）：

| 文件 / 目录 | 作用 |
| --- | --- |
| `README.md` | 项目门面：安装方式、快速上手、三大组件职责的文字描述 |
| `AssembleFirmware.py` | 把 FPGA bitstream 与 MCU 固件拼成一个可 USB 刷写的文件 |
| `Software/PC_Application/LibreVNA/LibreVNA.pro` | PC 程序的顶层 qmake 工程（汇总子工程） |
| `Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro` | GUI 主工程的完整文件清单——一份"活的目录索引" |
| `Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp` | 频谱分析仪模式（实践任务的定位目标之一） |
| `Software/VNA_embedded/Src/main.c` | 固件入口：CubeMX 生成的 main() 与外设初始化 |
| `Software/VNA_embedded/Application/Drivers/Si5351C.hpp` | 时钟芯片驱动（实践任务的定位目标之二） |
| `FPGA/VNA/top.vhd` | FPGA 顶层：把所有 VHDL 功能块连成一个整体（实践任务的定位目标之三） |
| `Documentation/` | 用户手册、编程指南、三份协议文档、示例测量 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：

1. **顶层目录导览**——仓库根目录下每个条目是什么；
2. **PC_Application 子树**——GUI 与单元测试如何组织；
3. **VNA_embedded 与 FPGA 子树**——固件与 FPGA 逻辑如何组织。

### 4.1 顶层目录导览

#### 4.1.1 概念说明

一个"全栈"硬件项目（软件 + 固件 + 逻辑 + 电路板 + 文档）通常会把不同技术栈的东西放进各自独立的目录，而不是混在一起。LibreVNA 的根目录就是这样组织的：每个顶层目录对应一种独立的"交付物"，彼此之间只通过少数几个明确定义的接口（USB 协议、SPI 协议、bitstream 文件）发生关系。

理解这张地图的收益是：**当你以后想改某个功能时，能立即知道这个改动只会落在一个子树里，不会波及其他两个工程。**

#### 4.1.2 核心流程

仓库根目录一览（按重要性排序）：

```text
LibreVNA/
├── Software/               ← 三大组件中的两个：PC GUI 程序 + MCU 固件
│   ├── PC_Application/         Qt 桌面程序与单元测试
│   ├── VNA_embedded/           STM32 固件（CubeIDE/FreeRTOS 工程）
│   ├── Integrationtests/       连接真实设备的 Python 集成测试
│   └── HelperTools/            辅助小工具
├── FPGA/                   ← 第三大组件：VHDL 逻辑工程
│   ├── VNA/                    主 FPGA 工程（含 testbench 与 bitstream）
│   ├── Generator/              Generator 模式相关的 IP 核目录
│   ├── WindowCoefficientGenerator.py   生成窗系数 .dat 文件的脚本
│   └── AMAttenuationCalculator.py       计算衰减器系数的脚本
├── Documentation/          ← 文档资产（手册、协议、示例测量）
│   ├── UserManual/             用户手册、SCPI 编程指南、规格书
│   ├── DeveloperInfo/          三份协议文档、三张框图、构建烧写说明
│   ├── Measurements/           示例测量文件（.s2p + 截图）
│   └── FAQ.md
├── Hardware/               ← PCB 设计文件（Eagle、KiCad、机械模型）
├── .github/workflows/      ← CI：Build / Test / HIL_Tests / Release
├── AssembleFirmware.py     ← 固件组装脚本（见下）
├── CHANGELOG.md            ← 版本变更记录
├── LICENSE                 ← 许可证（GPL 风格长文本）
└── README.md               ← 项目门面
```

一句话概括三大子树的分工：

- `Software/PC_Application` → **跑在你的电脑上**，负责"想"（校准、绘图、数学、远程控制）；
- `Software/VNA_embedded` → **跑在 STM32 上**，负责"调度"（配置 FPGA、预处理测量、走 USB 传输）；
- `FPGA` → **变成 bitstream 烧进 Spartan 6**，负责"快"（实时采样、射频时序、片上 DFT）。

#### 4.1.3 源码精读

**（1）README 是仓库的"导览图"**

README 明确写出了"PCB 只是射频前端"这一架构取舍，这句话是理解三大子树权重分配的钥匙：

[README.md:L67-L68](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/README.md#L67-L68)

> The PCB is really only the RF frontend with some processing power. Everything else is handled in the PC application once the data is transferred via USB.

（PCB 真的只是带一点处理能力的射频前端。其他一切都由 PC 应用在数据经 USB 传上来之后处理。）

README 同一段的"Digital section"小节则用三句话概括了 FPGA、MCU、Flash 三者的分工——FPGA 负责与射频块的通信和 ADC 采样，MCU 负责设置扫描并预处理数据，Flash 里存 FPGA bitstream、因此不需要 JTAG 工具：

[README.md:L82-L87](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/README.md#L82-L87)

README 还指出了两个重要的文档入口（用户手册和 SCPI 编程指南），它们都在 `Documentation/` 下，后面 4.1.4 的实践会用到：

[README.md:L50-L56](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/README.md#L50-L56)

**（2）AssembleFirmware.py：三大组件的"汇合点"**

根目录下的 `AssembleFirmware.py` 是唯一同时引用固件子树和 FPGA 子树的脚本，它揭示了固件发布物的组装方式——把 FPGA 的 `top.bin` 和 MCU 固件 `.bin` 拼接成一个带 `"VNA!"` 魔数的 `combined.vnafw` 文件，GUI 里的固件升级对话框刷的就是这个文件：

[AssembleFirmware.py:L1-L14](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/AssembleFirmware.py#L1-L14)

注意第 5–8 行的两个常量：`FPGA_BITSTREAM` 指向 `FPGA/VNA/top.bin`（FPGA 工程的输出），`MCU_FW` 指向固件工程的候选输出路径。这两个路径本身就是"FPGA 子树产出什么、VNA_embedded 子树产出什么"的最好证据。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：把根目录地图"用起来"一次，建立手感。

**操作步骤**：

1. 在仓库根目录执行 `ls`，对照上面 4.1.2 的树状图，把每个条目归入"软件 / 固件 / FPGA / 文档 / 硬件 / CI / 杂项"七类中的一类。
2. 打开 [README.md](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/README.md)，找到第 68 行和第 82–87 行，用自己的话各写一句摘要。
3. 阅读 [AssembleFirmware.py](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/AssembleFirmware.py)（只有几十行），确认它读取的两个输入分别来自哪个子树。
4. 浏览 `Documentation/` 的四个子目录，找到下面三份协议文档的确切文件名：
   - USB 协议：`Documentation/DeveloperInfo/USB_protocol_v12.pdf`（LaTeX 源码 `USB_protocol_v12.tex`）
   - 设备协议：`Documentation/DeveloperInfo/Device_protocol_v13.pdf`
   - FPGA 协议：`Documentation/DeveloperInfo/FPGA_protocol.pdf`

**需要观察的现象**：`Documentation/DeveloperInfo/` 下同时存在 `.tex` 和 `.pdf` 成对出现——文档是 LaTeX 维护、随仓库编译发布的；`Documentation/UserManual/` 下还有 `ProgrammingGuide.pdf`（SCPI 编程指南）和 `SCPI_Examples/` 目录（内含 `S11_calibration.py` 等 Python 示例脚本）。

**预期结果**：你能不假思索地说出"三份协议文档在 `Documentation/DeveloperInfo/`，SCPI 示例在 `Documentation/UserManual/SCPI_Examples/`"。本实践为纯目录浏览与阅读，无需运行任何程序。

#### 4.1.5 小练习与答案

**练习 1**：我想知道某个版本固件改了什么，应该看根目录下哪个文件？

答案：`CHANGELOG.md`。它按版本记录了 GUI、固件、FPGA 各自的变更；更细粒度的历史可以用 `git log` 查看。

**练习 2**：`Hardware/` 目录和三大组件是什么关系？为什么它几乎不会影响编译？

答案：`Hardware/` 存放 PCB 设计文件（`Eagle/`、`Kicad/`）、屏蔽罩机械模型（`.step` 文件）和一些实验电路。它描述的是"电路板长什么样"，不参与任何软件/固件/FPGA 的构建过程；只有当硬件改版引起引脚分配变化时，才会反过来要求固件（引脚定义）和 FPGA（`top.ucf` 约束文件）跟着改。

**练习 3**：`.github/workflows/` 里有一个 `HIL_Tests.yml`，从名字推测它和 `Build.yml`、`Test.yml` 的区别是什么？

答案：`Build.yml` 负责编译产物（GUI、固件），`Test.yml` 负责单元测试，而 `HIL_Tests.yml` 是 "Hardware-In-the-Loop" 测试——需要连接真实 LibreVNA 硬件才能跑的集成测试，对应 `Software/Integrationtests/` 子树。

### 4.2 PC_Application 子树

#### 4.2.1 概念说明

`Software/PC_Application` 是"跑在电脑上的那一半 LibreVNA"。它其实不是一个工程，而是**一个顶层工程 + 两个子工程**：

```text
Software/PC_Application/
├── LibreVNA/LibreVNA.pro        ← 顶层：SUBDIRS 容器
├── LibreVNA-GUI/                ← 子工程 1：GUI 主程序（约 30+ 目录）
└── LibreVNA-Test/               ← 子工程 2：单元测试程序
```

这种"容器工程 + 子工程"结构是 qmake 的标准做法：在 `LibreVNA/` 目录里执行 `qmake && make`，会依次构建两个子工程。

#### 4.2.2 核心流程

GUI 子工程内部的目录划分遵循"**功能域**"原则——每个子目录对应一大块用户可见的功能：

```text
LibreVNA-GUI/
├── main.cpp / appwindow.cpp     ← 启动入口与主窗口（根级文件）
├── mode.cpp / modehandler.cpp   ← 模式系统（VNA/SA/Generator 三种模式的抽象基类）
├── VNA/                         ← VNA 模式（含 Deembedding/ 去嵌入）
├── SpectrumAnalyzer/            ← 频谱分析仪模式
├── Generator/                   ← 信号发生器模式
├── Traces/                      ← 数据曲线：模型、绘图（Smith/XY/瀑布）、Marker、Math 数学运算
├── Calibration/                 ← 校准与校准件（含 LibreCAL/ 电子校准件支持）
├── Device/                      ← 设备驱动抽象 + 各厂商驱动（LibreVNA、SSA3000X、SNA5000A、Harogic）
├── Tools/                       ← S 参数数学、阻抗匹配等工具
├── CustomWidgets/               ← 可复用自定义控件
├── Util/                        ← 通用工具（含 usbinbuffer、QMicroz 压缩库）
└── scpi.cpp / tcpserver.cpp / streamingserver.cpp   ← 远程控制三件套
```

定位口诀（本讲最重要的速查表）：

| 想找什么 | 去哪里 |
| --- | --- |
| 程序怎么启动 | 根级 `main.cpp` → `appwindow.cpp` |
| 某种测量模式的界面/逻辑 | `VNA/`、`SpectrumAnalyzer/`、`Generator/` 同名目录 |
| 曲线怎么画、Marker 怎么算 | `Traces/`（绘图）与 `Traces/Math/`（数学运算） |
| 校准、校准件 | `Calibration/` |
| 设备怎么连、数据怎么收 | `Device/`（`devicedriver.h` 是抽象入口） |
| SCPI 远程控制 | `scpi.cpp`、`tcpserver.cpp`、`streamingserver.cpp` |
| 单元测试 | `../LibreVNA-Test/` |

#### 4.2.3 源码精读

**（1）顶层容器工程**

[Software/PC_Application/LibreVNA/LibreVNA.pro:L1-L4](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA/LibreVNA.pro#L1-L4)

```qmake
TEMPLATE = subdirs

SUBDIRS += \
    ../LibreVNA-GUI \
    ../LibreVNA-Test
```

这两行声明：这是一个"只负责进入子目录"的工程，构建顺序为先 GUI 后测试。

**（2）LibreVNA-GUI.pro 是一份"活的目录索引"**

GUI 主工程文件长达 400 多行，把所有 `.h/.cpp/.ui` 全部显式列出。它有一个非常值得注意的细节——**开头就把固件子树里的协议头文件纳入了编译**：

[Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro:L1-L3](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro#L1-L3)

```qmake
HEADERS += \
    ../../VNA_embedded/Application/Communication/Protocol.hpp \
    ../../VNA_embedded/Application/Communication/PacketConstants.h \
```

对应地，SOURCES 部分（第 175 行）也编译了 `../../VNA_embedded/Application/Communication/Protocol.cpp`：

[Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro:L174-L175](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro#L174-L175)

这说明：**USB 协议的定义不是"文档 + 两边各自实现"，而是两端共用同一份 C++ 源文件**（GUI 与固件都编译 `Protocol.cpp`），从机制上保证了收发双方对包格式的理解一致。这是跨工程协作的一个漂亮设计，也是后续单元 4 讲协议时的地基。

工程文件末尾还交代了外部依赖与语言标准——GUI 依赖 libusb（直接访问 USB 设备）和 Qt 的 widgets/network/svg 三个模块，使用 C++17，并把 git 哈希和固件版本号编译进程序：

[Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro:L332-L345](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro#L332-L345)

[Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro:L435-L438](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro#L435-L438)

**（3）以频谱分析仪模式为例验证"功能域"划分**

按上面的口诀，"频谱分析仪模式的扫描设置"应在 `SpectrumAnalyzer/spectrumanalyzer.cpp`。事实确实如此——该文件的构造函数里用代码创建了采集工具栏，其中 RBW（分辨率带宽）输入框的接线清晰可见：

[Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp:L178-L181](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L178-L181)

```cpp
eBandwidth->setToolTip("RBW");
connect(eBandwidth, &SIUnitEdit::valueChanged, this, &SpectrumAnalyzer::SetRBW);
connect(this, &SpectrumAnalyzer::RBWChanged, eBandwidth, &SIUnitEdit::setValueQuiet);
tb_acq->addWidget(new QLabel("RBW:"));
```

对应的槽函数 `SpectrumAnalyzer::SetRBW` 在同文件第 722 行起：

[Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp:L722-L730](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp#L722-L730)

它会把用户输入钳位到设备能力范围内（`Limits.SA.maxRBW` / `minRBW`），再存入 `settings.RBW` 并发出 `RBWChanged` 信号。"UI 控件 → 槽函数 → settings 结构"这条微观链路，就是以后单元 7 精读 SA 模式的入口。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：仅凭目录名和 `.pro` 文件，定位三个 GUI 功能的实现文件，验证"功能域"口诀。

**操作步骤**：

1. 打开 [LibreVNA-GUI.pro](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/PC_Application/LibreVNA-GUI/LibreVNA-GUI.pro)，在浏览器/编辑器里搜索以下关键词，记下命中的行号和所属目录：
   - `SpectrumAnalyzer/spectrumanalyzer.cpp`
   - `Device/devicedriver.h`
   - `Calibration/calibration.cpp`
   - `Traces/tracesmithchart.cpp`
2. 对照 4.2.2 的口诀表，确认每个文件所在目录与口诀一致。
3. 进入 `LibreVNA-Test/` 目录，数一数有几个 `*tests.cpp` 文件，并和它们测试的 GUI 模块对应起来（如 `calibrationtests.cpp` ↔ `Calibration/`）。

**需要观察的现象**：GUI 中每个功能子目录（`VNA/`、`Calibration/`、`Traces/Math/`……）在 `.pro` 文件中都有一组对应的 `HEADERS/SOURCES/FORMS` 条目；单元测试文件名与其测试目标一一对应。

**预期结果**：得到一张"功能 → 目录 → .pro 中的行号"三列对照表。至此你应该能确信：**在 GUI 子树里找功能，先查 `.pro` 文件往往比全文搜索更快**。本实践为纯阅读，无需编译。

#### 4.2.5 小练习与答案

**练习 1**：我想给 GUI 加一个新的自定义 Qt 控件，应该放进哪个目录、还需要做什么？

答案：放进 `CustomWidgets/`（参考 `siunitedit.cpp` 等），并把新文件的 `.h/.cpp` 追加到 `LibreVNA-GUI.pro` 的 `HEADERS`/`SOURCES` 列表中——qmake 工程不会自动发现新文件，忘记登记会链接失败。

**练习 2**：为什么 `Device/` 目录下除了 LibreVNA 自己的驱动，还有 SSA3000X、SNA5000A、Harogic 这些第三方仪器的目录？

答案：GUI 的设备层是抽象的（`Device/devicedriver.h` 定义统一接口），因此可以为其他厂商的仪器编写驱动，让同一个 GUI、同一套 Traces/校准体系直接控制第三方设备。这属于单元 3 的主题，这里只需记住目录形态体现的设计意图。

**练习 3**：`LibreVNA-Test` 中的测试能连真实设备吗？

答案：不能。`LibreVNA-Test` 是纯单元测试（对 S 参数运算、校准算法、FFT 等数学代码做已知答案验证）；需要真实硬件的测试在 `Software/Integrationtests/`（Python 编写，由 CI 中 `HIL_Tests.yml` 触发）。

### 4.3 VNA_embedded 与 FPGA 子树

#### 4.3.1 概念说明

这两个子树共同构成了设备端的"大脑 + 神经"：`Software/VNA_embedded` 是烧进 STM32G431 的固件（C/C++，FreeRTOS），`FPGA` 是综合成 bitstream 烧进 Spartan 6 的逻辑（VHDL）。

它们有一个共同的阅读难点：**大量"机器生成代码"与"手写代码"混居**。固件里 CubeMX 生成的外设初始化代码占很大篇幅，FPGA 工程里 IP 核目录也全是工具产物。识别哪些文件值得精读、哪些只需知道存在，是这两个子树的导读重点。

#### 4.3.2 核心流程

**固件子树的组织原则：生成代码与手写代码分层**

```text
Software/VNA_embedded/
├── VNA_embedded.ioc            ← CubeMX 工程配置（外设怎么配的真相来源）
├── STM32G431CBUX_FLASH.ld      ← 链接脚本（内存布局）
├── Startup/                    ← 启动汇编
├── Src/                        ← CubeMX 生成的 C 代码（main.c、app_freertos.c...）
├── Inc/                        ← CubeMX 生成的头文件
├── Middlewares/                ← 第三方中间件（FreeRTOS、USB 协议栈）
└── Application/                ← ★ 项目自有代码（精读主要在这里）
    ├── App.cpp                     ← 固件主逻辑入口
    ├── Hardware.cpp / HW_HAL.cpp   ← 硬件门面与板级引脚封装
    ├── VNA.cpp / SpectrumAnalyzer.cpp / Generator.cpp   ← 设备端三种模式
    ├── Cal.cpp / Trigger.cpp / Firmware.cpp ...
    ├── Communication/              ← USB 通信与协议定义（Protocol.hpp 两端共用！）
    └── Drivers/                    ← 各芯片驱动（Si5351C、max2871、Flash、FPGA/、USB/）
```

固件启动的数据流（后续单元 5 会逐行精读，这里先建立骨架）：

```text
复位 → Startup 汇编 → main()（CubeMX 生成）
     → HAL_Init / SystemClock_Config / MX_* 外设初始化
     → 创建 FreeRTOS defaultTask → osKernelStart() 启动调度器
     → StartDefaultTask() → App_Start()（进入项目自有代码 Application/App.cpp）
```

**FPGA 子树的组织原则：一个顶层 + 一组功能块 + 每块自带 testbench**

```text
FPGA/VNA/
├── VNA.xise                 ← Xilinx ISE 工程文件
├── top.vhd                  ← ★ 顶层：把所有功能块连成整体（853 行）
├── top.ucf                  ← 引脚/时序约束（FPGA 引脚 ↔ PCB 走线）
├── top.bin                  ← 编译产物 bitstream（AssembleFirmware.py 的输入）
├── Sweep.vhd                ← 扫描状态机（自主推进多点扫描）
├── Sampling.vhd / MCP33131.vhd   ← 采样汇聚 / ADC 接口时序
├── Windowing.vhd / window.vhd    ← 加窗 / 窗函数系数 ROM
├── DFT.vhd                  ← 片上离散傅里叶变换
├── MAX2871.vhd              ← FPGA 直接驱动两颗 PLL 的寄存器写入时序
├── spi_slave.vhd / SPIConfig.vhd ← MCU→FPGA 的 SPI 从机与命令分发
├── Synchronizer.vhd / ResetDelay.vhd
├── Hann.dat / Flattop.dat / Kaiser.dat   ← 窗系数（由 WindowCoefficientGenerator.py 生成）
├── Test_*.vhd               ← 各模块的仿真测试台（10 个）
└── ipcore_dir/              ← ISE IP 核（PLL、SweepConfigMem、DSP48...）

FPGA/Generator/
└── ipcore_dir/              ← Generator 模式使用的 IP 核（VCO_Mem、ModulationMemory 等）
```

注意一个容易误解的地方：`FPGA/Generator/` 目录**当前只包含工具生成的 IP 核**（`ipcore_dir/`），Generator 模式在设备上运行时同样使用 `FPGA/VNA` 的 bitstream；`Generator/` 里只是该功能用到的部分 IP 核产物，主工程仍在 `FPGA/VNA`。

#### 4.3.3 源码精读

**（1）固件入口 main.c：先跑生成代码，再交棒给 App**

`Src/main.c` 是 CubeMX 生成的，`main()` 的骨架是所有 STM32 项目的标准样式：

[Software/VNA_embedded/Src/main.c:L94-L172](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Src/main.c#L94-L172)

其中第 110–129 行是时钟与全部外设的初始化（I2C、SPI、USB、定时器、ADC……每一个都对应 PCB 上的一类接口）：

[Software/VNA_embedded/Src/main.c:L110-L129](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Src/main.c#L110-L129)

第 150–160 行创建 FreeRTOS 默认任务并启动调度器——`osKernelStart()` 之后 `main()` 就再也不返回了：

[Software/VNA_embedded/Src/main.c:L150-L160](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Src/main.c#L150-L160)

真正的"交棒点"在文件末尾的 `StartDefaultTask`：任务函数只做一件事——调用 `App_Start()`，从此进入项目自有代码 `Application/App.cpp`：

[Software/VNA_embedded/Src/main.c:L789-L799](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Src/main.c#L789-L799)

```c
void StartDefaultTask(void const * argument)
{
  /* USER CODE BEGIN 5 */
  App_Start();
  /* Infinite loop */
  for(;;)
  {
    osDelay(1);
  }
  /* USER CODE END 5 */
}
```

`App_Start()` 这个调用写在 `/* USER CODE BEGIN 5 */ ... /* USER CODE END 5 */` 区段内（对应的 `#include "App.h"` 在第 24–26 行的 USER CODE Includes 区段）——这正是 CubeMX 保护人工代码不被重新生成覆盖的机制。**读固件代码时，USER CODE 区段永远是最值得看的地方。**

**（2）芯片驱动长什么样：以 Si5351C 为例**

`Application/Drivers/` 下每个文件对应 PCB 上一颗需要软件配置的芯片。Si5351C 是全板时钟源（上一讲讲过，它兼任 25 MHz 以下激励源），其驱动接口干净到可以直接当文档读：

[Software/VNA_embedded/Application/Drivers/Si5351C.hpp:L5-L40](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/Si5351C.hpp#L5-L40)

```cpp
class Si5351C {
public:
    enum class PLL : uint8_t { A = 0, B = 1, };
    ...
    bool Init(uint32_t clkin_freq = 0);
    bool SetPLL(PLL pll, uint32_t frequency, PLLSource src, bool exactFrequency=true);
    bool SetCLK(uint8_t clknum, uint32_t frequency, PLL source, ...);
    ...
};
```

从接口就能看出这颗芯片的抽象模型：内部有两个 PLL（A/B）、多个输出时钟（CLK0..CLK7），可选用晶体或外部 CLKIN 作参考。"找到某颗芯片的驱动"在这类嵌入式项目里，就是到 `Application/Drivers/` 下找同名的 `.hpp/.cpp`。

**（3）FPGA 顶层 top.vhd：一张"芯片级接线图"**

`top.vhd` 的实体（entity）声明列出了 FPGA 的全部物理引脚——对照上一讲的数字框图读，会发现每个端口都是框图里的一条线：

[FPGA/VNA/top.vhd:L32-L87](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L32-L87)

例如其中几组：

```vhdl
entity top is
    Port ( CLK : in  STD_LOGIC;
           RESET : in  STD_LOGIC;
           MCU_MOSI : in  STD_LOGIC;        -- MCU→FPGA 的 SPI 数据线
           MCU_NSS : in  STD_LOGIC;         -- SPI 片选
           MCU_INTR : out STD_LOGIC;        -- FPGA→MCU 中断（如：测量完成）
           PORT1_CONVSTART : out STD_LOGIC; -- 触发端口 1 的 ADC 开始转换
           PORT1_SDO : in STD_LOGIC;        -- 端口 1 ADC 的采样数据回读
           LO1_LD : in STD_LOGIC;           -- 本振 PLL 的锁定检测
           ATTENUATION : out STD_LOGIC_VECTOR (6 downto 0);  -- 数字衰减器控制
           ...
```

结构体（architecture）部分先声明了 11 个功能块组件（`PLL`、`ResetDelay`、`Sweep`、`Windowing`、`Sampling`、`MCP33131`、`MAX2871`、`SPICommands`、`DFT`、`SweepConfigMem`、`Synchronizer`，见第 90–330 行的 COMPONENT 声明），然后逐一实例化。几个关键实例化点：

时钟管理——外部时钟经 PLL IP 核倍频得到内部主时钟（第 485 行起）：

[FPGA/VNA/top.vhd:L485-L494](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L485-L494)

跨时钟域同步——外部来的异步信号（如 MCU 片选、PLL 锁定检测）都要过两级触发器同步器（第 506 行起，共 8 个实例）：

[FPGA/VNA/top.vhd:L506-L512](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L506-L512)

FPGA 直接驱动两颗 MAX2871 PLL——`Source`（激励源）与 `LO1`（本振）各一个实例（第 564、579 行起）：

[FPGA/VNA/top.vhd:L564-L578](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L564-L578)

**片上 DFT 实例**——本讲实践任务的定位目标。频谱分析模式使用的 DFT 核以 96 个频点（`BINS => 96`）配置，吃进两路加窗后的采样数据，输出频谱结果（第 825 行起）：

[FPGA/VNA/top.vhd:L825-L838](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L825-L838)

```vhdl
SA_DFT: DFT GENERIC MAP(BINS => 96)
PORT MAP(
    CLK => clk_pll,
    RESET => dft_reset,
    PORT1 => port1_windowed,
    PORT2 => port2_windowed,
    NEW_SAMPLE => windowing_ready,
    ...
```

紧随其后的 `ConfigMem : SweepConfigMem`（第 840–850 行）是扫描配置存储器——MCU 通过 SPI 把每个扫描点的配置写进这块片上 RAM，`Sweep` 状态机再逐点读出执行：

[FPGA/VNA/top.vhd:L840-L850](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L840-L850)

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：不看任何搜索工具，仅凭本讲建立的两张子树图，手工预测三个功能的落点，然后验证。

**操作步骤**：

1. **先做预测**。合上本讲义，在纸上写下以下三个功能各自"应该"所在的目录和文件名（只凭目录名推理）：
   - A. GUI 中频谱分析仪模式的扫描设置界面
   - B. 固件中 Si5351C 时钟芯片驱动
   - C. FPGA 中 DFT 模块
2. **再验证**。用目录浏览（`ls` 或文件管理器）逐个确认，记录真实路径。
3. 对功能 C 再进一步：在 [top.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd) 中找到 DFT 组件被实例化的行号（提示：实例名以 `SA_` 开头，因为它服务于频谱分析模式）。
4. 对功能 B 再进一步：打开 `Si5351C.hpp`，数一数公开成员函数有多少个与 `CLK`（输出时钟）相关，印证"这颗芯片 = 时钟多路输出源"的定位。

**需要观察的现象**：

- 三个预测与验证结果是否一致？如果某个预测错了，错在"目录名望文生义偏差"还是"不知道该进哪个子树"？
- 在 `FPGA/VNA/` 目录里，每个手写功能块 `.vhd` 是否几乎都有同名的 `Test_*.vhd` 陪伴？

**预期结果**（参考答案）：

- A → `Software/PC_Application/LibreVNA-GUI/SpectrumAnalyzer/spectrumanalyzer.cpp`（该目录另有 `tracewidgetsa.cpp` 和头文件；设置控件在代码中动态创建，参见 4.2.3 第 (3) 点）。
- B → `Software/VNA_embedded/Application/Drivers/Si5351C.cpp` 与 `Si5351C.hpp`（`.cpp` 实现约 388 行，通过 I2C 操作芯片）。
- C → `FPGA/VNA/DFT.vhd`，并在 `top.vhd` 第 825 行以 `SA_DFT : DFT` 实例化（`BINS => 96`）。

本实践为纯目录浏览与阅读，无需硬件、无需编译，结果可完全本地验证。

#### 4.3.5 小练习与答案

**练习 1**：我想知道 STM32 固件里 SPI1 的波特率分频是怎么配的，应该先看哪个文件？为什么？

答案：先看 `Software/VNA_embedded/VNA_embedded.ioc`（CubeMX 配置文件，外设配置的"真相来源"），再看生成代码 `Src/main.c` 中 `MX_SPI1_Init()`（本仓库中位于第 335 行起，可见 `SPI_BAUDRATEPRESCALER_4`）。如果只看 `.c` 文件然后直接改，下次用 CubeMX 重新生成时改动会被覆盖。

**练习 2**：`FPGA/VNA/` 下既有 `top.ucf` 又有 `top.bin`，两者是什么关系？

答案：`.ucf` 是用户约束文件，声明"VHDL 信号 ↔ FPGA 封装引脚"的映射以及时序约束，是**源文件**（与 PCB 走线强相关）；`.bin` 是 ISE 综合/布局布线后生成的 **bitstream 产物**，被根目录 `AssembleFirmware.py` 读取并拼进最终固件包。修改 `.vhd` 或 `.ucf` 后需要重新跑 ISE 才能得到新的 `.bin`。

**练习 3**：`FPGA/VNA/Test_DFT.vhd` 这类文件的作用是什么？它会被综合进 bitstream 吗？

答案：它是仿真测试台（testbench），为 DFT 模块注入激励信号并检查输出，用于在 ISE 仿真器中验证逻辑正确性。Testbench 只用于仿真，不会（也不应）被综合进 `top.bin`。仓库为 DFT、Sampling、SPI、Windowing、MAX2871 等模块各配了 testbench，单元 6 会专门学习这种验证文化。

## 5. 综合实践

**任务：制作你自己的"三站式功能定位表"。**

把仓库想象成一条数据链路上的三个站点（FPGA → MCU → PC），下面 6 个功能每个都横跨至少一个站点，请为每个功能填写它所在的子树、具体文件和一句职责描述，然后全部用目录浏览/文件打开验证：

| # | 功能提示 | 落点（子树 / 文件） | 一句话职责 |
| --- | --- | --- | --- |
| 1 | USB 协议的包格式定义（两端共用） | 待填 | 待填 |
| 2 | 扫描点配置写进 FPGA 片上 RAM | 待填 | 待填 |
| 3 | 把 bitstream 和 MCU 固件拼成刷机包 | 待填 | 待填 |
| 4 | GUI 端 Smith 圆图绘制 | 待填 | 待填 |
| 5 | STM32 交棒进入项目自有代码的那个调用 | 待填 | 待填 |
| 6 | 窗函数系数的生成脚本 | 待填 | 待填 |

参考答案（做完再看）：

1. `Software/VNA_embedded/Application/Communication/Protocol.hpp`（同时被 GUI 的 `.pro` 编译，见 4.2.3）——以 C++ 结构体定义每种包的二进制布局，保证两端一致。
2. `FPGA/VNA/top.vhd` 中的 `SweepConfigMem` 实例（L840-L850），配合 `Sweep.vhd` 状态机逐点读取执行。
3. 根目录 `AssembleFirmware.py`（L5-L14 读取 `FPGA/VNA/top.bin` 与固件 `.bin`，输出 `combined.vnafw`）。
4. `Software/PC_Application/LibreVNA-GUI/Traces/tracesmithchart.cpp`（把复数 S 参数映射到史密斯圆坐标）。
5. `Software/VNA_embedded/Src/main.c` 中 `StartDefaultTask` 对 `App_Start()` 的调用（L789-L799），从此进入 `Application/App.cpp`。
6. `FPGA/WindowCoefficientGenerator.py`（生成 `FPGA/VNA/` 下的 `Hann.dat`、`Flattop.dat`、`Kaiser.dat`）。

完成后再做一次反向挑战：随机打开本讲 4.2.2 口诀表和 4.3.2 两棵目录树中的任一文件，不看注释，仅凭路径推断它的功能——如果 10 个里能说对 8 个以上，本讲的目标就达到了。

## 6. 本讲小结

- 仓库根目录按"交付物"分层：`Software/`（GUI + 固件）、`FPGA/`（VHDL 工程）、`Documentation/`（手册与三份协议文档：USB / Device / FPGA）、`Hardware/`（PCB 设计）、`.github/workflows/`（CI），以及连接固件与 FPGA 产物的组装脚本 `AssembleFirmware.py`。
- `Software/PC_Application` 是"容器工程 + GUI 子工程 + 测试子工程"结构；GUI 内部按功能域分目录（`VNA/`、`SpectrumAnalyzer/`、`Generator/`、`Traces/`、`Calibration/`、`Device/`、`Tools/`……），`LibreVNA-GUI.pro` 是一份活的文件索引，查它常常比全文搜索更快。
- 一个关键设计：`Protocol.hpp/.cpp` 由 GUI 与固件**共同编译**（`LibreVNA-GUI.pro` 第 1–3、175 行），两端协议定义同源，从机制上避免格式漂移。
- 固件子树把 CubeMX 生成代码（`Src/`、`Inc/`、`Middlewares/`）与手写代码（`Application/`）分层；`main.c` 的 USER CODE 区段调用 `App_Start()` 交棒，芯片驱动集中在 `Application/Drivers/`（如 `Si5351C.cpp`）。
- FPGA 子树以 `top.vhd` 为顶层，把 Sweep / Sampling / Windowing / DFT / MAX2871 / SPICommands 等功能块连成整体（例如 DFT 在 L825 实例化为 `BINS => 96` 的 `SA_DFT`），且几乎每个功能块都有配套 `Test_*.vhd` 仿真；`FPGA/Generator/` 目前只包含 IP 核目录。
- 定位功能的通用方法：先判断功能属于哪一站（PC / MCU / FPGA），再在该子树内按功能域名望文生义，最后用目录浏览或 `.pro` 文件验证。

## 7. 下一步学习建议

本讲我们已经三次路过 `LibreVNA-GUI.pro`、`main.cpp` 所在的 GUI 子树，但还没有真正把它跑起来。下一讲 **u1-l3《构建与运行 PC GUI 应用》** 将动手编译这个工程：安装 Qt6 与 libusb 依赖、理解 `.pro` 文件的完整语法、处理 Linux 下的 udev 权限规则，并在没有实体设备的情况下用示例测量（`Documentation/Measurements/` 下的 `.s2p` 文件）验证 GUI 可用。

如果你对设备端更感兴趣，可以先跳读 `Documentation/DeveloperInfo/BuildAndFlash.md`，预习固件与 FPGA 的构建流程（那是 u1-l4 的主题）；无论走哪条线，建议顺手把本讲 4.2.2 的定位口诀表保存在你的笔记里——它是后续 30 多讲通读源码时最常用的检索工具。
