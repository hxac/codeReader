# 项目概览：用 FPGA FIR 滤波器做图像锐化

> 本讲是《FPGA_FIR-FILTER 学习手册》的第一篇，面向零基础读者。读完本讲你不需要会写 VHDL，只要能说清楚「这个项目到底在干什么」就够了。

## 1. 本讲目标

学完本讲，你应该能够：

- 用一句话说清项目定位：在 FPGA 上用一个 **7 抽头 FIR 滤波器**对 **720p 视频**做**边缘增强（锐化）**。
- 说清 **Octave 算法设计 → VHDL 硬件实现 → 仿真验证** 这三部分各自负责什么、如何串成一条完整的工作流。
- 知道项目来源于 **FPGA Vision Remote Lab（FPGA 视觉远程实验室）**，并了解可用的视频讲义与远程硬件资源。
- 在本地用 GNU Octave 跑通软件版锐化脚本，亲眼看到图像锐化前后的差异。

## 2. 前置知识

如果你对下面这些词感到陌生，不用担心，本讲只要求你建立「直觉」，不要求掌握细节。

- **像素（pixel）**：一张数字图像由很多小方格组成，每个方格叫一个像素。彩色像素通常由红（R）、绿（G）、蓝（B）三个 0~255 的数值表示。
- **视频（video）**：视频就是一帧帧连续播放的图像。本项目处理的是 **720p** 视频，即每帧有效画面为 1280×720 个像素。
- **滤波器（filter）**：一种「按规则改造信号」的操作。比如「让变化剧烈的地方更突出」就是锐化滤波器；「让画面变模糊」就是平滑滤波器。
- **FIR 滤波器**：FIR = Finite Impulse Response（有限长单位冲激响应）。简单说，它的输出是「当前和过去若干个输入」的加权和，权重就是一组固定数字，称为**系数（coefficients）**。「抽头（tap）」指参与运算的输入个数，本项目是 7 抽头。
- **FPGA**：Field Programmable Gate Array，现场可编程门阵列。你可以把它理解成「可以反复重新连线、重新定义功能的硬件芯片」，常用于做实时、并行的信号处理。
- **VHDL**：一种硬件描述语言，用来告诉 FPGA「芯片内部该怎么连、怎么算」。
- **Octave**：一个和 MATLAB 语法高度兼容的免费数值计算软件，本项目用它来「先用软件把算法想清楚、算清楚」，再搬到硬件上。

> 一个核心直觉：**先在 Octave 里用软件把滤波器设计好、验证有效，再用 VHDL 把同样的算法做成硬件电路，最后用仿真（testbench）确认硬件算出来的结果和软件一致。** 这就是本项目最本质的工作流。

## 3. 本讲源码地图

本讲会引用下面这些文件，它们都真实存在于仓库中。

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/README.md) | 仓库顶层说明：项目是「图像处理用 FIR 滤波器」，VHDL + Octave 实现。 |
| `FPGA-FIR-Filter-master/README.md` | 子项目说明：定位为「FPGA 视频锐化」，并指向远程实验室。 |
| `FPGA-FIR-Filter-master/Octave/sharp_image_filter.m` | 软件版锐化：用 Octave 对图像做垂直 + 水平两次 FIR 滤波。 |
| `FPGA-FIR-Filter-master/Octave/sharp_filter_coefficients.m` | 设计滤波器系数：用 `fir1` 生成高通 FIR 系数并定点化。 |
| `FPGA-FIR-Filter-master/Octave/sharp_frequency_response.m` | 画出滤波器的频率响应，判断它是「锐化（高通）」。 |
| `FPGA-FIR-Filter-master/FPGA-Design/sharp.vhd` | 硬件顶层实体：接收视频输入、例化处理模块、输出视频。 |
| `FPGA-FIR-Filter-master/FPGA-Design/FIR.qsf` | Quartus 工程约束：器件、顶层实体、VHDL 文件清单。 |
| `FPGA-FIR-Filter-master/Docs/FPGA_Vision_Experiments_FIR-Filter.pdf` | 配套教学讲义（视频课的文字版），可作为延伸阅读。 |

> 仓库里还有 `db/`、`incremental_db/`、`output_files/` 等大量文件，它们是 Quartus **编译生成的中间产物**，不是我们要学习的源码，本讲（以及整个手册）一律跳过。

## 4. 核心概念与源码讲解

本讲对应三个最小模块：**项目定位与背景**、**Octave + VHDL 协同工作流**、**远程实验室资源**。

### 4.1 项目定位与背景

#### 4.1.1 概念说明

这个项目要解决的问题是：**让一段视频看起来更「清晰锐利」**。

人眼对「边缘」最敏感——物体轮廓、纹理交界处亮度变化剧烈的地方，就是图像信息量最大的地方。**锐化（sharpening）/ 边缘增强（edge enhancement）** 的做法是：检测出亮度变化剧烈的区域，把它们「放大」一点，于是轮廓显得更清楚。

从信号处理角度，亮度变化剧烈 = 高频成分多，平坦区域 = 低频成分多。所以**锐化滤波器本质上是一个「高通滤波器（high-pass filter）」**：让高频通过、压制低频。本项目用的就是一个 7 抽头的高通 FIR 滤波器，它的系数是：

\[
h = \frac{[\,1,\ 0,\ -9,\ 48,\ -9,\ 0,\ 1\,]}{32}
\]

注意几个关键点：

- 中心系数是 **48**（最大权重），代表「当前像素自身」。
- 两侧是 **-9**（负权重），代表「邻居减一点」——这正是「强调与邻居的差异」，即边缘。
- 最外两个 **1** 是轻微的回调，让过渡更自然。
- 中间有两个 **0**，所以虽然名义是 7 抽头，实际只有 5 个非零抽头参与乘法（后面讲硬件时会看到，这能省掉两次乘法）。
- 把所有系数加起来：\(1+0-9+48-9+0+1 = 32\)，再除以 32 正好等于 1。这说明**滤波器的直流增益为 1**，即不会改变整张图的平均亮度，只改变局部细节——这正是「锐化而非整体变亮/变暗」的保证。

#### 4.1.2 核心流程

从外部看，这个项目像一根「视频管道」：

```
视频输入 (vs/hs/de + R/G/B 像素流, 74.25 MHz)
        │
        ▼
   [ 锐化 FIR 滤波器 ]   ← 本项目要实现的核心
        │
        ▼
视频输出 (同样格式的像素流，但画面更锐利)
```

视频用三组信号描述：

- **vs（vertical sync）**：垂直同步，标记一帧（新画面）的开始。
- **hs（horizontal sync）**：水平同步，标记一行的开始。
- **de（data enable）**：数据有效，`de=1` 时当前时钟周期送进来的是「真正的像素」，`de=0` 是行/场消隐（空白）期。

时序上，本项目对应 **720p、74.25 MHz 像素时钟**：每个时钟周期处理一个像素，配合消隐期，整体构成标准的视频时序。这一点会直接写进顶层实体的注释里（见 4.1.3）。

#### 4.1.3 源码精读

顶层 README 把项目定位讲得很清楚——它是一个用于**图像处理**的 FIR 滤波器，用 VHDL 部署到 FPGA，用 Octave 做仿真与验证：

- 顶层说明：项目提供 FIR 滤波器的图像处理实现，VHDL 用于 FPGA 部署、Octave 用于仿真与验证 —— [README.md:1-5](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/README.md#L1-L5)。
- 致谢与来源：特别感谢 Marco Winzker 教授与 FPGA Vision，他们的工作是本项目的基础 —— [README.md:6-8](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/README.md#L6-L8)。
- 功能概述：FIR 滤波器可做平滑、边缘检测、降噪等图像处理，并演示了如何仿真、测试并在 FPGA 上实时实现 —— [README.md:11-12](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/README.md#L11-L12)。

子项目 README 进一步收窄到「视频锐化」，并给出资源链接：

- 「学习如何在 FPGA 上实现 FIR 滤波器；视频讲座讲解锐化滤波器（sharpness filter）用于视频信号处理的算法与实现」 —— [FPGA-FIR-Filter-master/README.md:1-4](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/README.md#L1-L4)。
- 项目主页（含视频讲座与远程实验室入口），且声明远程实验室是开放教育资源 —— [FPGA-FIR-Filter-master/README.md:6-9](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/README.md#L6-L9)。

硬件顶层 `sharp.vhd` 的端口注释，把「720p、74.25 MHz」写进了接口定义：

```vhdl
clk       : in  std_logic;   -- input clock 74.25 MHz, video 720p
```

> 见 [sharp.vhd:12-33](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.vhd#L12-L33)：顶层实体 `sharp` 的端口包含 `clk`、`reset_n`、`enable_in`、视频输入 `vs_in/hs_in/de_in/r_in/g_in/b_in`、视频输出 `vs_out/.../b_out`、以及 `clk_o` 和 `led`。这一段定义了「视频管道」两端的接头。

#### 4.1.4 代码实践

**实践目标**：通过阅读两份 README，亲手把「项目是什么、能做什么」整理成一句话。

**操作步骤**：

1. 打开 [README.md](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/README.md) 和 [FPGA-FIR-Filter-master/README.md](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/README.md)。
2. 找出顶层 README 的 `Features` 一节里提到的「可配置参数（Configurable Parameters）」有哪些。
3. 找出子项目 README 里提到的两个外部资源（项目主页、远程实验室）。

**需要观察的现象**：注意顶层 README 用词较泛（smoothing / edge detection / noise reduction 都提了），而子项目 README 更聚焦（明确说做 sharpness filter）。

**预期结果**：能写出类似「本项目在 FPGA 上用 FIR 滤波器对 720p 视频做锐化，算法用 Octave 设计、硬件用 VHDL 实现、用自校验 testbench 验证」这样的一句话总结。

#### 4.1.5 小练习与答案

**练习 1**：为什么锐化滤波器是「高通」而不是「低通」？
**参考答案**：因为锐化要强调亮度变化剧烈的边缘，而剧烈变化属于高频成分；让高频通过、压制低频的就是高通滤波器。

**练习 2**：系数和为 \(1\)（即 \(32/32\)）有什么实际意义？
**参考答案**：直流增益为 1，意味着整张图像的平均亮度不变，滤波器只增强局部细节，而不会让画面整体变亮或变暗。

---

### 4.2 Octave + VHDL 协同工作流

#### 4.2.1 概念说明

直接用 VHDL 写硬件、再上板调试，成本高、迭代慢。本项目的聪明之处在于：**先用 Octave 在软件里把滤波器算明白**——确认系数对、效果对、频率响应对——**再把它翻译成 VHDL 硬件**，最后用仿真对比「硬件输出」和「软件参考输出」是否一致。

这就是一条「软件先行、硬件跟进、仿真把关」的协同工作流，分为三个阶段：

1. **算法设计（Octave）**：设计系数、看频率响应、在真实图像上试效果。
2. **硬件实现（VHDL）**：把同样的卷积做成并行流水线电路。
3. **仿真验证（testbench）**：读入图像、驱动硬件、把输出和软件结果逐像素比对。

#### 4.2.2 核心流程

```
[阶段1: Octave 算法设计]
   sharp_filter_coefficients.m  ──→  设计系数 [1,0,-9,48,-9,0,1]/32
   sharp_frequency_response.m   ──→  画频率响应, 确认是高通
   sharp_image_filter.m         ──→  在图像上试效果, 生成参考输出
                 │
                 ▼ (系数与期望结果交给硬件)
[阶段2: VHDL 硬件实现]
   sharp.vhd (顶层) ──例化── sharp_slice / sharp_control / sharp_arith / sharp_linemem
                 │
                 ▼ (交给 testbench)
[阶段3: 仿真验证]
   sim_sharp.vhd / sim_sharp_self-checking.vhd
   ──→ 读 PPM 图像驱动硬件, 逐像素对比, 报告通过/失败
```

#### 4.2.3 源码精读

**阶段 1：在 Octave 里设计系数。** 这个高通锐化核是用 `fir1` 设计出来的，再放大 32 倍取整，得到定点系数：

```octave
fir1(8,0.5, "high")          % 设计一个 8 阶、归一化截止 0.5 的高通 FIR
round(32*fir1(8,0.5, "high")) % 放大 32 倍并四舍五入, 得到整数定点系数
```

> 见 [sharp_filter_coefficients.m:9-13](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Octave/sharp_filter_coefficients.m#L9-L13)。运行后可以看到整数系数正是 \([1, 0, -9, 48, -9, 0, 1]\)。

**阶段 1：在 Octave 里对真实图像做锐化。** 先垂直方向滤波、再水平方向滤波（两次一维卷积拼出二维锐化）：

```octave
f_hor = [1, 0, -9, 48, -9, 0, 1]/32;   % 水平方向系数
f_ver = f_hor';                         % 垂直方向系数 (转置)
img_tmp = imfilter(img_in, f_ver);      % 先做垂直 FIR
img_out = imfilter(img_tmp, f_hor);     % 再做水平 FIR
```

> 见 [sharp_image_filter.m:13-19](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Octave/sharp_image_filter.m#L13-L19)。这段代码就是「软件参考实现」，后面硬件要复现的正是它的结果。

**阶段 2：VHDL 硬件实现。** 顶层 `sharp.vhd` 对 R/G/B 三个通道各例化一个处理单元，并对同步信号做延迟对齐：

```vhdl
r_slice: entity work.sharp_slice   -- 红通道处理
   port map (clk => clk, ..., data_in => r_0, data_out => r_1);
control: entity work.sharp_control  -- 同步信号延迟对齐
   generic map (delay => 6) ...
```

> 见 [sharp.vhd:65-95](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.vhd#L65-L95)：三个 `sharp_slice`（R/G/B）加一个 `sharp_control`。具体每个模块内部如何工作，是后续讲义的内容。

**阶段 2 之工程组织：** Quartus 工程约束文件 `FIR.qsf` 登记了所有参与编译的 VHDL 文件、目标器件与顶层实体：

```
set_global_assignment -name FAMILY "Cyclone V"
set_global_assignment -name DEVICE 5CEBA2F17C6
set_global_assignment -name TOP_LEVEL_ENTITY sharp
...
set_global_assignment -name VHDL_FILE sharp.vhd          (+ sharp_slice/control/arith/linemem)
```

> 见 [FIR.qsf:40-42](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/FIR.qsf#L40-L42)（器件与顶层）与 [FIR.qsf:125-130](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/FIR.qsf#L125-L130)（5 个 VHDL 文件 + SDC 约束清单）。这告诉我们：硬件由 5 个 `.vhd` 文件 + 1 个 `.sdc` 时序约束文件组成。

> 阶段 3（testbench）的精读放在 U5 单元，本讲只需知道「它会把硬件输出和软件参考结果逐像素比对」即可。

#### 4.2.4 代码实践

**实践目标**：用 Octave 亲手复现滤波器系数的设计，验证它与硬件注释里的一致。

**操作步骤**：

1. 安装 GNU Octave，并在 Octave 命令行里执行 `pkg install -forge signal` 安装 signal 包（若尚未安装）。
2. 打开 `FPGA-FIR-Filter-master/Octave/sharp_filter_coefficients.m`，在 Octave 里运行它。
3. 观察第二条语句 `round(32*fir1(8,0.5,"high"))` 的输出。

**需要观察的现象**：输出的整数向量应当是 `[1, 0, -9, 48, -9, 0, 1]`（或因 FIR 对称性呈等价排列）。

**预期结果**：你得到的整数系数与本讲 4.1.1 给出的 \([1,0,-9,48,-9,0,1]\) 一致，由此确认「软件设计的系数」就是「硬件实现的系数」。如果本地未安装 Octave，则标记为**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么先在 Octave 里设计，而不是直接写 VHDL？
**参考答案**：Octave 迭代快、可视化方便，能迅速验证「系数和效果对不对」；确认无误后再翻译成硬件，能大幅降低硬件调试成本。

**练习 2**：`sharp_image_filter.m` 里为什么写了两次 `imfilter`（一次 `f_ver`、一次 `f_hor`）？
**参考答案**：因为二维锐化被拆成了「先垂直、再水平」两次一维卷积（可分离滤波），用同一个核分别处理行方向和列方向，就能得到二维锐化效果。

---

### 4.3 远程实验室资源

#### 4.3.1 概念说明

本项目不是孤立的代码练习，它背后连着一个**远程实验室（Remote Lab）**：你可以在浏览器里把编译好的设计下载到一块真实的 FPGA 板上，看真实视频流的锐化效果，而不必自己拥有硬件。这是德国 **Hochschule Bonn-Rhein-Sieg（H-BRS）** 的 **FPGA Vision Remote Lab** 提供的开放教育资源（Open Education）。

#### 4.3.2 核心流程

使用远程资源的大致路径：

```
阅读配套视频/PDF 讲义  ──→  理解算法与实现
        │
        ▼
本地用 Quartus 编译工程  ──→  得到可下载的比特流
        │
        ▼
登录 FPGA Vision Remote Lab  ──→  上传/选择设计, 下载到真实板卡
        │
        ▼
观察真实 720p 视频的锐化效果
```

#### 4.3.3 源码精读

子项目 README 把这些资源直接写在开头：

```text
Learn how to implement an FIR filter on an FPGA. Video lectures explain
algorithm and implementation of a sharpness filter for video signal
processing. Real hardware is available as a remote lab.
...
Project page with video lectures and access to the remote lab:
https://www.h-brs.de/de/fpga-vision-lab
The FPGA Vision Remote Lab is Open Education.
```

> 见 [FPGA-FIR-Filter-master/README.md:1-9](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/README.md#L1-L9)：项目主页是 `https://www.h-brs.de/de/fpga-vision-lab`，包含视频讲座与远程实验室入口，并声明这是开放教育。

此外，`Docs/` 目录下还有配套的教学讲义：

> [FPGA-FIR-Filter-master/Docs/FPGA_Vision_Experiments_FIR-Filter.pdf](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Docs/FPGA_Vision_Experiments_FIR-Filter.pdf)（配套 PDF，视频讲座的文字版，可作为本手册的延伸阅读；其具体页内细节待本地打开确认）。

#### 4.3.4 代码实践

**实践目标**：访问项目主页，了解你能用到的真实硬件与教学资源。

**操作步骤**：

1. 用浏览器打开 `https://www.h-brs.de/de/fpga-vision-lab`。
2. 找到「Remote Lab（远程实验室）」相关入口，了解如何申请/预约使用真实 FPGA 板。
3. 浏览是否有与本 FIR 实验对应的视频讲座章节。

**需要观察的现象**：确认这是一个面向教学的、可远程访问真实 FPGA 硬件的平台，且资源是开放（Open Education）的。

**预期结果**：你能在该站点找到 FIR 滤波器实验相关的视频讲解与远程硬件入口。具体页面布局可能随站点更新而变化，**待本地访问确认**。

#### 4.3.5 小练习与答案

**练习 1**：远程实验室相比「自己买一块 FPGA 板」有什么好处？
**参考答案**：无需自备硬件即可在真实芯片上验证设计，降低学习与实验门槛，特别适合教学和自学。

**练习 2**：本项目的算法作者是谁、来自哪个机构？
**参考答案**：算法由 Marco Winzker（Hochschule Bonn-Rhein-Sieg）设计，属于 FPGA Vision Remote Lab 开放教育资源。

---

## 5. 综合实践

把本讲学到的「软件先行」工作流亲手跑一遍：**用 GNU Octave 对一张真实图像做锐化，对比输入与输出。**

**实践目标**：跑通软件版锐化脚本 `sharp_image_filter.m`，亲眼看到高通 FIR 带来的边缘增强效果。

**操作步骤**：

1. 安装 [GNU Octave](https://octave.org/)，并在 Octave 里加载 image 包：
   ```octave
   pkg install -forge image   % 若尚未安装
   pkg load image
   ```
2. 准备一张测试图。脚本默认读 `Lindau_Harbour_720p.jpg`，但该文件**不在仓库内**（脚本注释里也写了 "change name for your test image"）。你可以：
   - 用仓库自带的图：`FPGA-FIR-Filter-master/Verification/Sample.jpg`；或
   - 用任意一张 1280×720（720p）的 JPG，重命名或修改脚本里的文件名。
3. 打开 [sharp_image_filter.m:11](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Octave/sharp_image_filter.m#L11) 和 [sharp_image_filter.m:19](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Octave/sharp_image_filter.m#L19)，把输入、输出文件名改成你的图（例如输入 `Sample.jpg`、输出 `Sample_sharp.jpg`）。
4. 在 Octave 里运行该脚本。

**需要观察的现象**：终端会打印 `Edge enhancement with vertical and horizontal FIR-filter`；生成一张输出图。把输入图和输出图并排比较，注意**物体边缘、纹理细节**处变得更清晰、更「硬」，而大面积平坦区域（如天空）几乎不变。

**预期结果**：输出图像明显比输入更锐利，但平均亮度没有整体偏移（因为系数和为 1）。这一份软件输出，也正是后续硬件要复现、testbench 要比对的「参考答案」。若本地未安装 Octave 或缺少 image 包，则本实践标记为**待本地验证**。

> 进阶观察（可选）：把第 13 行的中心系数 `48` 改大（如 `64`）或改小（如 `40`），重新运行，比较锐化强度的变化，直观体会「中心系数越大、锐化越强」。这正好为下一单元「滤波器系数设计」做铺垫。

## 6. 本讲小结

- 本项目在 **FPGA** 上用一个 **7 抽头高通 FIR 滤波器**对 **720p 视频**做**锐化（边缘增强）**，目标器件是 Cyclone V。
- 锐化核系数为 \([1, 0, -9, 48, -9, 0, 1]/32\)，系数和为 1，保证只增强细节、不改平均亮度。
- 核心工作流是「**软件先行、硬件跟进、仿真把关**」：Octave 设计系数与参考输出 → VHDL 实现硬件 → testbench 逐像素比对。
- 硬件由顶层 `sharp.vhd` 加 4 个子模块组成，工程约束集中在 `FIR.qsf`（器件、顶层、文件清单）。
- 项目来自 **FPGA Vision Remote Lab**（H-BRS），是开放教育资源，提供视频讲座与真实硬件的远程访问。
- 你已能用 Octave 在真实图像上跑通软件版锐化，为后续读懂硬件打下直觉基础。

## 7. 下一步学习建议

- **想深入算法**：进入 U2 单元，学习「图像锐化与可分离 FIR 原理」以及「用 `fir1` 设计定点系数」，把本讲的系数是怎么来的彻底搞懂。
- **想看懂硬件**：先读 U1-L2「仓库目录结构与三大组成」，建立文件到职责的映射，再进 U3 单元读顶层 `sharp.vhd` 与数据通路。
- **想上手工具链**：阅读 U1-L3「Quartus 工程与目标平台 Cyclone V」，学会打开工程、查看器件与编译结果。
- **延伸阅读**：打开 `Docs/FPGA_Vision_Experiments_FIR-Filter.pdf`，对照官方视频讲座复习本讲内容。
