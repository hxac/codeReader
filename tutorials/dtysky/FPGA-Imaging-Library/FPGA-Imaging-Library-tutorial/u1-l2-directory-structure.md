# 目录结构与标准化 IP 文件夹布局

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 F-I-L 中「任意一个 IP」自带的固定目录约定，不依赖记忆具体 IP 名字。
- 解释 `HDL/<IP>.srcs/` 下 `sources_1`、`sim_1`、`xgui`、`component.xml` 各自承担什么职责。
- 在一个陌生的 IP 目录里，快速定位「RTL 源码在哪、软件参考实现在哪、仿真数据怎么生成、结果怎么比对」。
- 把一个 IP 的完整目录树画出来，并逐项标注作用。

本讲承接 [u1-l1 项目总览与定位](u1-l1-project-overview.md)：上一讲我们建立了「七大功能分类 + 软硬一致性仿真闭环」的整体心智模型；这一讲我们把镜头拉近，钻进**单个 IP 文件夹内部**，看它的标准骨架长什么样。理解了这个骨架，后面所有讲义里的源码引用你都能对号入座。

## 2. 前置知识

### 2.1 什么是「IP 核」

在 FPGA 世界里，IP 核（Intellectual Property core）是一段可复用、可参数化的硬件描述代码，封装后可以像集成电路芯片一样「拖进」工程里连线使用。Xilinx Vivado 把每个 IP 核打包成一个带 `component.xml` 描述文件的目录，这就是本讲反复出现的 `.srcs/component.xml` 的来历。

### 2.2 为什么要有「标准文件夹布局」

F-I-L 里有几十个图像处理 IP。如果每个 IP 的目录组织都自由发挥，维护成本会爆炸。因此作者给每个 IP 都套了**同一套目录约定**：

- 同样的子目录名（`HDL`、`SoftwareSim`、`HDLSimDataGen`、`FunSimForHDL`、`SimResCheck`、`ImageForTest`）。
- 同样的脚本命名（`sim.py`、`create.py`、`convert.py`、`compare.py`）。
- 同样的输入/输出文件后缀约定（`.dat` 喂给仿真、`.res` 由仿真吐出、`.bmp` 给人看）。

这样无论是 ColorReversal 还是 Threshold，你只要会看一个，就会看全部。**「软硬一致性」闭环也建立在这套约定之上**：软件参考结果与 RTL 仿真结果走相同的目录、相同的命名规则，才能被脚本自动配对比对。

### 2.3 名词速查

| 术语 | 含义 |
| --- | --- |
| RTL | Register Transfer Level，用 Verilog 描述的寄存器级硬件代码，即 `.v` 文件 |
| Testbench | 仿真测试平台，给被测模块喂激励、收输出的那一层，即 `_TB.sv` |
| `.xpr` | Vivado 工程文件，记录该 IP 用了哪些源码、综合/仿真设置 |
| `component.xml` | IP-XACT 标准的 IP 描述文件，告诉 Vivado 这个 IP 有哪些端口、参数、文件 |
| `.dat` / `.res` | 仿真用的纯文本数据：`.dat` 是输入激励，`.res` 是仿真输出 |

## 3. 本讲源码地图

本讲以 `Point/ColorReversal`（颜色取反，最简单的点运算 IP）为样板，涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [Point/ColorReversal/README.md](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/README.md) | 该 IP 的仿真步骤说明 |
| [Point/ColorReversal/HDL/ColorReversal.srcs/sources_1/new/ColorReversal.v](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sources_1/new/ColorReversal.v) | RTL 主模块（被测设计） |
| [Point/ColorReversal/HDL/ColorReversal.srcs/sim_1/new/ColorReversal_TB.sv](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sim_1/new/ColorReversal_TB.sv) | SystemVerilog 测试平台 |
| [Point/ColorReversal/HDL/ColorReversal.srcs/component.xml](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/component.xml) | IP-XACT 描述（端口/参数/文件清单） |
| [Point/ColorReversal/HDL/ColorReversal.srcs/xgui/ColorReversal_v1_0.tcl](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/xgui/ColorReversal_v1_0.tcl) | Vivado 里配置参数的图形界面脚本 |
| [Point/ColorReversal/HDLSimDataGen/create.py](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDLSimDataGen/create.py) | 把测试图像转成 `.dat` 激励 |
| [Point/ColorReversal/SimResCheck/convert.py](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/SimResCheck/convert.py) | 把仿真 `.res` 转回 `.bmp` |
| [Point/ColorReversal/SimResCheck/compare.py](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/SimResCheck/compare.py) | 软件结果与硬件结果比对，算 PSNR |

## 4. 核心概念与源码讲解

### 4.1 IP 顶层目录的六大固定部件

#### 4.1.1 概念说明

每个 IP 文件夹（无论属于 `Point`、`Geometry` 还是 `LocalFilter`）都由固定的几块拼成。我们可以把它们分成两类：

- **硬件侧**：`HDL/` —— 存放 RTL、testbench、Vivado 工程与 IP 打包描述。
- **软件/仿真侧**：`ImageForTest/`（喂图）、`SoftwareSim/`（软件黄金模型）、`HDLSimDataGen/`（造激励）、`FunSimForHDL/`（跑 RTL 仿真）、`SimResCheck/`（出结果 + 比对）。

外加一份 `README.md` 说明本 IP 怎么跑仿真。这套分工直接对应 u1-l1 讲过的「软硬一致性」五步流程。

#### 4.1.2 核心流程

以 ColorReversal 为例，完整目录树（依据 `git ls-files` 的真实条目）如下：

```text
Point/ColorReversal/
├── README.md                  # 本 IP 仿真步骤说明
├── .gitignore                 # 屏蔽 Vivado/ModelSim 生成物，只跟踪源码与脚本
├── HDL/
│   ├── ColorReversal.xpr      # Vivado 工程文件
│   └── ColorReversal.srcs/
│       ├── component.xml      # IP-XACT：端口/参数/文件清单（打包成 IP 的关键）
│       ├── sources_1/new/
│       │   └── ColorReversal.v        # RTL 主模块（被测设计 DUT）
│       ├── sim_1/new/
│       │   └── ColorReversal_TB.sv    # SystemVerilog 测试平台
│       └── xgui/
│           └── ColorReversal_v1_0.tcl # Vivado 参数配置 GUI 脚本
├── ImageForTest/
│   └── conf.json              # 仿真配置（选用哪些 conf 变体）
├── SoftwareSim/
│   └── sim.py                 # 软件参考实现（黄金模型）
├── HDLSimDataGen/
│   └── create.py              # 测试图 -> .dat 激励
├── FunSimForHDL/
│   ├── ColorReversa.mpf       # ModelSim 工程文件
│   ├── ColorReversa.cr.mti    # ModelSim 编译状态
│   ├── Run.do                 # 仿真脚本（带波形，可观察）
│   └── RunOver.do             # 仿真脚本（只跑完，不看波形）
└── SimResCheck/
    ├── convert.py             # .res -> .bmp（把硬件结果变回图片）
    └── compare.py             # 软件 vs 硬件 比对，生成 PSNR 报告
```

> 说明：上面的树是 ColorReversal 当前 HEAD 下**实际被 git 跟踪**的文件。`FunSimForHDL/` 里的 `.dat`、`.res`、`imgindex.dat` 等是运行时产物，被 `.gitignore` 排除，所以不出现在仓库里——你本地跑完仿真后才会看到它们。

#### 4.1.3 源码精读

`README.md` 把这六块串成一条操作链。注意一个细节：README 里写的脚本名是 `creat.py` 和 `covert.py`，但仓库里**实际文件名**是 `create.py` 和 `convert.py`（README 有拼写笔误）。以实际文件为准：

[Point/ColorReversal/README.md:13-25](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/README.md#L13-L25) —— 这段 README 把流程切成「Preparing（放图 + 改 conf.json）→ Software simulation（跑 sim.py）→ Creat preparing data（跑 create.py）→ Functional simulation（ModelSim）」。每一步都对应一个固定子目录，目录与步骤一一映射。

`.gitignore` 解释了为什么仓库看起来「干净」：

[Point/ColorReversal/.gitignore:1-7](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/.gitignore#L1-L7) —— 先用 `HDL/*` 与 `FunSimForHDL/*` 把两个目录里所有生成物整体忽略，再用 `!HDL/*.srcs/`、`!HDL/*.xpr`、`!FunSimForHDL/*.mpf`、`!FunSimForHDL/*.mti`、`!FunSimForHDL/*.do` 把需要版本化的源码与脚本「放」回来。这是 F-I-L 每个 IP 复用的同一套忽略规则。

#### 4.1.4 代码实践

**实践目标**：亲手把一棵 IP 目录树画出来，建立空间感。

**操作步骤**：

1. 在仓库根目录执行 `git ls-files -- Point/ColorReversal/`。
2. 把输出按目录层级缩进成树（参考 4.1.2 的格式）。
3. 对每个文件，写一句中文注释说明它属于「硬件侧」还是「软件/仿真侧」。

**需要观察的现象**：列表里**没有**任何 `.dat`、`.res`、`.bmp`、`work/` 目录——它们都是运行时产物。

**预期结果**：你画出的树应当与 4.1.2 完全一致；如果你看到 `FunSimForHDL/*.dat`，说明本地已经跑过仿真（这些文件未被跟踪，不会进 git）。

#### 4.1.5 小练习与答案

**练习 1**：F-I-L 的几十个 IP 共用同一套目录约定，最大的好处是什么？

**参考答案**：可复用性与可自动化。维护者看任何一个 IP 都用同一套心智模型；脚本（`create.py`/`convert.py`/`compare.py`）可以跨 IP 复制改名即用，「软硬一致性」比对也能自动化配对文件，无需为每个 IP 重写流程。

**练习 2**：为什么 `FunSimForHDL/` 里的 `.dat`、`.res` 不进 git？

**参考答案**：它们是由测试图像和 RTL 仿真**生成**的派生产物，体积大且可随时重建。版本控制只需保留「能生成它们的源码与脚本」，由 `.gitignore` 显式排除，避免仓库膨胀和冲突。

---

### 4.2 HDL 源码目录：`.srcs` 的内部分工

#### 4.2.1 概念说明

`HDL/<IP>.srcs/` 是 Vivado 工程的源码容器，里面按「用途」分成几个 `fileset`（文件集）：

- `sources_1/new/` —— **综合用**的 RTL 主模块（最终会被实现成硬件的设计，Design Under Test）。
- `sim_1/new/` —— **仿真用**的 testbench（不会被综合进硬件，只用于验证）。
- `xgui/` —— **参数配置界面**的 Tcl 脚本，决定你在 Vivado GUI 里看到哪些可调参数。
- `component.xml` —— 把以上文件集、端口、参数**登记**成「一个 IP」的清单。

为什么要把源码和仿真分开放？因为综合（synthesis，生成真实电路）和仿真（simulation，验证行为）是两条独立链路，Vivado 需要明确区分「哪些文件参与综合、哪些只参与仿真」。`component.xml` 里就为不同用途分别建了 `view`。

#### 4.2.2 核心流程

```text
component.xml  ──登记──▶  sources_1/new/*.v   （综合 view）
                ──登记──▶  sim_1/new/*_TB.sv   （testbench view）
                ──登记──▶  xgui/*.tcl          （xpgui view）
                ──声明──▶  端口 clk/rst_n/in_enable/in_data/out_ready/out_data
                ──声明──▶  参数 work_mode/color_channels/color_width
```

`component.xml` 里一个关键设计：端口 `in_data`/`out_data` 的位宽不是写死的，而是用 `dependency` 表达式 `color_channels * color_width - 1` 动态算出来。这就是 F-I-L 所有 IP 「可参数化」的底层机制——你在 GUI 改 `color_width`，端口位宽自动跟着变。

#### 4.2.3 源码精读

**RTL 主模块的端口**（统一接口，u1-l4 会详讲，这里只看它在源码树里的位置）：

[Point/ColorReversal/HDL/ColorReversal.srcs/sources_1/new/ColorReversal.v:54-60](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sources_1/new/ColorReversal.v#L54-L60) —— `module ColorReversal(clk, rst_n, in_enable, in_data, out_ready, out_data)`，这是所有 F-I-L IP 共用的六端口约定。

[Point/ColorReversal/HDL/ColorReversal.srcs/sources_1/new/ColorReversal.v:68-80](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sources_1/new/ColorReversal.v#L68-L80) —— 三个参数 `work_mode`、`color_channels`、`color_width`，它们会原样出现在 `component.xml` 的参数表里。

**testbench 的复数例化**（一个 TB 同时验证多种配置）：

[Point/ColorReversal/HDL/ColorReversal.srcs/sim_1/new/ColorReversal_TB.sv:83-107](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sim_1/new/ColorReversal_TB.sv#L83-L107) —— TB 例化了 6 个 ColorReversal 实例：`{RGB, Gray, Bin} × {Pipeline, ReqAck}`，用参数 `#(work_mode, color_channels, color_width)` 区分。这是 F-I-L testbench 的典型写法——一次仿真覆盖「三种图像格式 × 两种工作模式」。

**component.xml 的「视图（view）」划分**：

[Point/ColorReversal/HDL/ColorReversal.srcs/component.xml:8-71](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/component.xml#L8-L71) —— 定义了四种 view：`xilinx_verilogsynthesis`（综合）、`xilinx_verilogbehavioralsimulation`（行为仿真）、`xilinx_verilogtestbench`（测试平台）、`xilinx_xpgui`（GUI）。每种 view 引用不同的 fileset。

[Point/ColorReversal/HDL/ColorReversal.srcs/component.xml:178-184](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/component.xml#L178-L184) —— `work_mode` 参数被声明为一个枚举下拉框：`Pipeline=0`、`ReqAck=1`。这个枚举就是你在 Vivado GUI 里看到的下拉选项来源。

**xgui 的 GUI 装配**：

[Point/ColorReversal/HDL/ColorReversal.srcs/xgui/ColorReversal_v1_0.tcl:2-8](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/xgui/ColorReversal_v1_0.tcl#L2-L8) —— `init_gui` 过程在 Vivado 里建一个「Parameters」页，把 `work_mode` 渲染成 `comboBox`，`color_channels`、`color_width` 渲染成普通输入框。`component.xml` 负责「数据」，`xgui/*.tcl` 负责「外观」。

#### 4.2.4 代码实践

**实践目标**：验证「参数 → 端口位宽」的联动关系。

**操作步骤**：

1. 打开 [component.xml](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/component.xml)，找到 `in_data` 端口的 `spirit:left`（约第 117 行）。
2. 阅读它的 `dependency` 属性：`((color_channels * color_width) - 1)`。
3. 代入默认值 `color_channels=3`、`color_width=8`，手算 `left`。
4. 与 `.v` 源码里 `input [color_channels * color_width - 1 : 0] in_data` 对照。

**需要观察的现象**：`component.xml` 第 117 行硬写的 `>23<`，正是 `3*8-1=23` 的结果。

**预期结果**：默认配置下 `in_data` 是 24 位（RGB 三通道 × 8 位），高 8 位 R、中 8 位 G、低 8 位 B——这与 testbench 里 `[23:16]/[15:8]/[7:0]` 的切片完全对应。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `sources_1` 和 `sim_1` 要分成两个目录，而不是放一起？

**参考答案**：综合工具只应吸收 `sources_1` 里的设计文件，绝不能把 `sim_1` 的 testbench 综合成电路。分开存放 + 在 `component.xml` 里用不同 view（`xilinx_verilogsynthesis` vs `xilinx_verilogtestbench`）登记，从目录结构和元数据两个层面保证 testbench 不污染综合结果。

**练习 2**：如果要把 `color_width` 从 8 改成 10（10 位精度），`component.xml` 里哪些地方会变？

**参考答案**：参数 `color_width` 的默认值（约第 233 行 `PARAM_VALUE.color_width`）会变；`in_data`/`out_data` 端口的 `dependency` 表达式不变（它本来就是公式），但它求值出的 `left` 会从 23 变成 29（`3*10-1`）。公式化是 IP 可参数化的关键。

---

### 4.3 仿真脚本目录：从图像到激励

#### 4.3.1 概念说明

RTL 不能直接「吃」一张 `.bmp` 图片，它只认二进制比特流。所以需要一组 Python 脚本做格式翻译：

- `ImageForTest/conf.json` —— 声明本次仿真用哪些「配置变体」（`conf`）。
- `SoftwareSim/sim.py` —— **软件黄金模型**：用 PIL 算出「正确答案」图片。
- `HDLSimDataGen/create.py` —— 把图片像素逐个翻译成 `.dat` 文本，喂给 testbench。
- `FunSimForHDL/Run.do`、`RunOver.do` —— ModelSim 仿真脚本，驱动 testbench 跑出 `.res`。

这一段解决的核心问题是：**如何让同一张图片，既能被 Python 处理，又能被 Verilog 处理，且二者输入完全一致**。

#### 4.3.2 核心流程

```text
ImageForTest/*.bmp            （人放进去的测试图）
        │
        ├──▶ SoftwareSim/sim.py        ──▶ SimResCheck/*-soft.bmp   （软件参考答案）
        │
        └──▶ HDLSimDataGen/create.py   ──▶ FunSimForHDL/*.dat        （RTL 激励）
                                          + imgindex.dat（文件清单）
                                                      │
                                          FunSimForHDL/Run.do（ModelSim）
                                                      ▼
                                          FunSimForHDL/*.res          （RTL 输出）
```

`create.py` 会先写出图像尺寸（`xsize`、`ysize`）和模式（`RGB`/`L`/`1`）作为文件头，再把每个像素翻译成定长二进制串（每通道 10 位）。testbench 读到这些信息后，按相同模式解析。

#### 4.3.3 源码精读

**conf.json 的极简结构**：

[Point/ColorReversal/ImageForTest/conf.json:1-5](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/ImageForTest/conf.json#L1-L5) —— 只有一个 `conf` 数组，当前值是 `["default"]`。对 ColorReversal 这种无参数 IP，一个变体就够；对带增益的 IP（如 LuminanceTransform），这里会有多个变体，每个变体产出一组独立的 `.dat`/`.res`。

**软件黄金模型的算法核心**：

[Point/ColorReversal/SoftwareSim/sim.py:68-73](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/SoftwareSim/sim.py#L68-L73) —— `transform` 函数只有一行实质逻辑：`im.point(lambda p : 255 - p)`，即对每个像素做 `255 - p`（颜色取反）。这就是 ColorReversal 的「正确答案」来源，也是 RTL 必须复现的行为。结果存为 `SimResCheck/*-soft.bmp`。

**激励生成器的像素翻译**：

[Point/ColorReversal/HDLSimDataGen/create.py:22-33](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDLSimDataGen/create.py#L22-L33) —— `color_format` 把每个通道的整数值转成 10 位二进制字符串（高位补零）。注意是 **10 位**而非 8 位：这是为了在 `.dat` 文件里统一字段宽度，testbench 用 `$fscanf(fi, "%b", ...)` 按二进制读回。

[Point/ColorReversal/HDLSimDataGen/create.py:53-68](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDLSimDataGen/create.py#L53-L68) —— 遍历 `ImageForTest` 下所有 `.jpg`/`.bmp`，对每张图写一个 `<name>.dat`（前两行是尺寸、第三行是模式、之后是像素），并把所有文件名汇总进 `imgindex.dat`。testbench 正是先读 `imgindex.dat` 拿到待测文件清单（见 TB 第 188 行 `fi = $fopen("imgindex.dat","r")`）。

**ModelSim 驱动脚本**：

[Point/ColorReversal/FunSimForHDL/Run.do:1](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/FunSimForHDL/Run.do#L1) —— `vsim -voptargs=+acc -L unisims_ver work.ColorReversal_TB`：加载 testbench 并链接 Xilinx 原语库 `unisims_ver`（所以 README 强调要先把 Vivado 库编译进 ModelSim）。`Run.do` 会拉出波形并 `run -all`，而 `RunOver.do` 只跑完不看波形。

#### 4.3.4 代码实践

**实践目标**：追踪「一张图 → 一份 `.dat`」的翻译过程，理解 RTL 输入从哪来。

**操作步骤**：

1. 准备一张极小的 RGB 图（例如 2×2），心中给定 4 个像素的 RGB 值。
2. 阅读 [create.py 的 `color_format`](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDLSimDataGen/create.py#L22-L33)，手算第一个像素会变成哪一串二进制。
3. 阅读对应 testbench 的读取逻辑：

[Point/ColorReversal/HDL/ColorReversal.srcs/sim_1/new/ColorReversal_TB.sv:134-143](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sim_1/new/ColorReversal_TB.sv#L134-L143) —— `work_pipeline` 任务用 `$fscanf(fi, "%b", RGBPipeline.in_data)` 把二进制串读进 24 位 `in_data`，并按 `imconf`（RGB/L/1）决定写入哪个实例。

**需要观察的现象**：`.dat` 里每通道 10 位，但 `in_data` 是 24 位（3×8）。`%b` 读取时高位被截断，只保留低 8 位有效位。

**预期结果**：第一个 RGB 像素 `(R,G,B)` 在 `.dat` 中写作三段 10 位二进制（共 30 字符）；testbench 读入后 `in_data[23:16]=R`、`[15:8]=G`、`[7:0]=B`。（完整运行需本地 Python 2.7 + PIL + ModelSim，若环境不满足，则标注「待本地验证」并停留在纸面推导。）

#### 4.3.5 小练习与答案

**练习 1**：`create.py` 里每通道用 10 位二进制，但 RTL 的 `color_width` 默认是 8，会不会出错？

**参考答案**：不会。testbench 用 `$fscanf(..., "%b", in_data)` 读取，Verilog 的 `%b` 按目标位宽截断——`in_data` 是 24 位，只吸收每段 10 位中的低 8 位，多出的 2 位被丢弃。10 位是文件格式的「字段宽度约定」，与硬件位宽解耦。

**练习 2**：`SoftwareSim/sim.py` 和 `HDLSimDataGen/create.py` 都遍历 `ImageForTest`，但输出完全不同，分别是什么？

**参考答案**：`sim.py` 输出**图片**（`*-soft.bmp`，人眼可看的软件参考答案）；`create.py` 输出**文本激励**（`*.dat` + `imgindex.dat`，喂给 testbench 的机器格式）。一个面向「比对」，一个面向「RTL 输入」。

---

### 4.4 结果校验目录：把硬件结果变回图片并打分

#### 4.4.1 概念说明

RTL 仿真吐出的是 `.res` 纯文本（testbench 用 `$fwrite` 写的数字），人眼看不懂，也没法跟软件参考图直接比。`SimResCheck/` 两个脚本就是「最后一公里」：

- `convert.py` —— 把 `.res` 转回 `.bmp`，让你肉眼对比。
- `compare.py` —— 把「软件图」和「硬件图」逐像素比，算 **PSNR**（峰值信噪比），输出报告。

PSNR 是图像处理里衡量「两张图有多接近」的标准指标，F-I-L 用它量化「软硬一致性」。

#### 4.4.2 核心流程

PSNR 的计算（对应 `compare.py` 的 `get_psnr`）：

\[
\text{RMS} = \sqrt{\frac{1}{N}\sum_{i}(p_{\text{soft},i} - p_{\text{hdl},i})^2}
\]

\[
\text{PSNR} = 20 \cdot \log_{10}\!\left(\frac{255}{\text{RMS}}\right)
\]

单位是分贝（dB）。PSNR 越高表示两张图越接近；当 RMS 为 0（完全一致）时，`compare.py` 给一个极大值（`1000*1000`）表示「无穷大 PSNR」。一般 PSNR > 40 dB 即可认为肉眼无差别。

#### 4.4.3 源码精读

**`.res` → `.bmp` 的还原**：

[Point/ColorReversal/SimResCheck/convert.py:11-23](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/SimResCheck/convert.py#L11-L23) —— `convert` 读 `.res`：前两行是尺寸、第三行是模式（`RGB`/`L`/`1`）、之后是像素。RGB 模式下用空格分三个通道，灰度/二值则是单值。还原后存成 `*-hdlfun.bmp`。注意它读的是 `data[2]`（第 3 行）做模式判断，与 `create.py` 写文件头的顺序严格对应——这就是「约定」的威力。

**PSNR 计算与报告**：

[Point/ColorReversal/SimResCheck/compare.py:11-20](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/SimResCheck/compare.py#L11-L20) —— `get_psnr` 用 `ImageChops.difference` 算逐像素差、`ImageStat.Stat(...).rms` 取均方根，再套用上面的 PSNR 公式。RMS 为 0 时返回 `1000*1000` 兜底。

[Point/ColorReversal/SimResCheck/compare.py:50-67](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/SimResCheck/compare.py#L50-L67) —— 比对脚本靠**文件名约定**配对：扫当前目录找形如 `*-reqack-hdl`（硬件结果）和 `*-soft`（软件结果）的文件，提取共同前缀作为图像名归组，写入 `compare_report.txt` 和 `compare_report_table.txt`。

> 注意：`compare.py` 第 9 行的 `name_format` 用了 `conf['lm_gain']`，这是从带参数的 IP（如 LuminanceTransform）复制过来的残留，对 ColorReversal 这个无参数 IP 实际不会走到那行。这正说明 F-I-L 的脚本「跨 IP 复制改名」的开发模式。

#### 4.4.4 代码实践

**实践目标**：读懂「文件名约定」如何驱动自动比对。

**操作步骤**：

1. 阅读 [compare.py 的文件名解析](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/SimResCheck/compare.py#L50-L67)。
2. 回顾 `sim.py` 输出 `*-soft.bmp`、`convert.py` 输出 `*-hdlfun.bmp`。
3. 在纸上推演：给定一张名为 `girl.bmp` 的测试图，软件结果与硬件结果分别叫什么名字？`compare.py` 如何把它们配成一对？

**需要观察的现象**：脚本完全不依赖任何「清单文件」，纯粹靠文件名后缀（`-soft` / `-reqack-hdl`）的正则匹配来配对。

**预期结果**：软件图 `girl-soft.bmp` 与硬件图 `girl-reqack-hdl.bmp` 被正则 `(.*)-soft` / `(.*)-reqack-hdl` 提取出共同前缀 `girl`，归入同一组计算 PSNR。（若本地无 Python 2.7 + PIL 环境，则标注「待本地验证」。）

#### 4.4.5 小练习与答案

**练习 1**：为什么 `convert.py` 要从 `.res` 第三行读 `mode`，而不是写死 RGB？

**参考答案**：因为同一个 IP 通常要同时支持 RGB、灰度（L）、二值（1）三种格式（testbench 例化了三套实例）。`mode` 由 `create.py` 在生成 `.dat` 时写入文件头，经过 testbench 透传到 `.res`，`convert.py` 读回后才能用正确方式（三元组 vs 单值）重建图片，保证三种格式都能还原。

**练习 2**：PSNR 为无穷大（程序里用 `1000*1000` 表示）意味着什么？

**参考答案**：意味着软件参考图与 RTL 仿真图**逐像素完全相同**（RMS=0），即硬件实现 100% 复现了软件算法。对 ColorReversal 这种纯组合逻辑的点运算，理想情况下 PSNR 应当是无穷大；一旦不是，说明 RTL 与软件模型存在行为差异，需要排查。

---

## 5. 综合实践

**任务**：扮演一次「F-I-L 新成员」，把本讲学到的目录骨架用起来。

请选择 `Point/` 下任意一个你还没读过的 IP（例如 `Threshold` 或 `Graying`），完成以下事情：

1. 用 `git ls-files -- Point/<IP名>/` 列出它的全部跟踪文件，画出目录树。
2. 在树上标注：哪个文件是 RTL 主模块？哪个是 testbench？哪个脚本生成 `.dat`？哪个脚本算 PSNR？
3. 打开它的 `component.xml`，找出它比 ColorReversal **多出来**的参数（例如 Threshold 会有阈值相关参数），并解释这些参数如何影响端口或行为。
4. 对比它的 `SoftwareSim/sim.py` 的 `transform` 函数与 ColorReversal 的 `255 - p`，说明该 IP 的「软件黄金模型」在做什么。
5. 写一句话总结：「这个 IP 的软硬一致性闭环里，软件答案和硬件答案分别叫什么文件名？」

**验收标准**：

- 目录树与 `git ls-files` 输出一致，无臆造文件。
- 能指出该 IP 相对 ColorReversal 的「参数差异」并定位到 `component.xml` 的具体行。
- 能说出软件/硬件输出文件的命名后缀。

这个任务把本讲三个最小模块（HDL 源码目录、仿真脚本目录、结果校验目录）一次性串起来，做完你就具备「快速读任意一个 F-I-L IP 目录」的能力，为下一讲理解统一接口与时序打好基础。

## 6. 本讲小结

- 每个 F-I-L IP 都遵循同一套顶层布局：硬件侧 `HDL/` + 软件仿真侧 `ImageForTest`/`SoftwareSim`/`HDLSimDataGen`/`FunSimForHDL`/`SimResCheck` + `README.md`。
- `HDL/<IP>.srcs/` 内部按 fileset 分工：`sources_1`（综合用 RTL）、`sim_1`（testbench）、`xgui`（参数 GUI）、`component.xml`（把这一切登记成 IP）。
- `component.xml` 用 `dependency` 表达式让端口位宽随 `color_channels * color_width` 动态变化，这是「可参数化 IP」的底层机制；`work_mode` 还被声明为 `Pipeline=0 / ReqAck=1` 的下拉枚举。
- 软件侧形成一条单向流水：`*.bmp` → `sim.py` 出软件答案、`create.py` 出 `.dat` 激励 → ModelSim 出 `.res` → `convert.py` 还原成图 → `compare.py` 用 PSNR 打分。
- `.gitignore` 用「先全忽略、再放行源码与脚本」的策略，让仓库只保留可重建的源文件，运行时产物（`.dat`/`.res`/`work/`）一律不入库。
- README 里的 `creat.py`/`covert.py` 是拼写笔误，仓库实际文件名为 `create.py`/`convert.py`，以实际文件为准。

## 7. 下一步学习建议

下一讲 [u1-l3 工具链与仿真运行方式](u1-l3-toolchain-and-simulation.md) 会把本讲的目录骨架**跑起来**：动手执行 `sim.py`、`create.py`，在 ModelSim 里 `do Run.do`，亲眼看到 `.dat`/`.res`/`-soft.bmp`/`-hdlfun.bmp` 被生成并比对。

在那之前，建议你：

- 重新打开本讲的目录树，对照 ColorReversal 的真实文件，确保每个目录你都能说出「它属于五步流程的哪一步」。
- 跳读 [ColorReversal.v 的 generate 双分支](https://github.com/dtysky/FPGA-Imaging-Library/blob/c8cd350dc07397be1979b51f5f99e5a7fddf98f6/Point/ColorReversal/HDL/ColorReversal.srcs/sources_1/new/ColorReversal.v#L115-L135)，为 [u1-l4 标准化 IP 接口与两种工作模式](u1-l4-standard-interface-and-modes.md) 做铺垫——那讲会展开 `work_mode=0/1` 的时序差异。
