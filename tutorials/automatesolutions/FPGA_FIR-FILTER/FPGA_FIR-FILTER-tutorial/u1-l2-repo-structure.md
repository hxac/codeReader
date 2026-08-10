# 仓库目录结构与三大组成

## 1. 本讲目标

上一讲（u1-l1）我们已经知道：这个项目是用 FPGA 上的 7 抽头 FIR 滤波器对 720p 视频做锐化（边缘增强），并且遵循「Octave 设计算法 → VHDL 实现硬件 → testbench 仿真验证」的工作流。

本讲不进入算法细节，而是先帮你在仓库里建立一张**「文件地图」**。学完后你应当能够：

1. 说出仓库四个顶层目录（`FPGA-Design`、`Verification`、`Octave`、`Docs`）各放什么、彼此什么关系。
2. 把 `FPGA-Design/` 下每一个 `.vhd` 文件对应到它在数据通路中的职责，并指出哪个是顶层、哪些是被例化的子模块。
3. 区分**手写源码**与 Quartus / Questa 生成的**产物目录**（`db/`、`incremental_db/`、`output_files/`、`work/` 等），知道哪些可以删除后重新生成、哪些不能动。

掌握这张地图之后，后面所有讲义在引用某个文件时，你都能立刻知道它在工程里扮演什么角色。

## 2. 前置知识

本讲是纯「目录与文件结构」的入门内容，不要求你懂 VHDL 语法。下面几个上一讲引入的术语会在文中出现，这里只做最简提醒：

- **像素 / RGB**：一张彩色图像由很多像素组成，每个像素有红（R）、绿（G）、蓝（B）三个分量，本项目里每个分量是 0~255 的整数。
- **视频时序信号 vs / hs / de**：硬件视频流靠三个同步信号组织——`vs`（垂直同步，标记一帧开始）、`hs`（水平同步，标记一行开始）、`de`（数据有效，`de=1` 时当前像素是有效的画面像素）。
- **FIR / 抽头 / 系数**：FIR 滤波器把当前像素和它周围的若干像素加权求和；这些被加的像素位置叫「抽头（tap）」，权重叫「系数」。本项目的锐化系数是 `[1, 0, -9, 48, -9, 0, 1] / 32`。
- **定点化**：把浮点系数放大成整数（乘以 32）再参与运算，方便硬件实现。
- **可分离滤波**：二维滤波可以拆成「先垂直方向、再水平方向」两次一维卷积。

另外两个工具名词：

- **Quartus Prime**：Intel FPGA 的官方综合 / 布局布线 / 下载工具。工程用 `.qpf`（工程文件）和 `.qsf`（设置与约束文件）描述。
- **Questa / ModelSim**：VHDL 仿真器，用来跑 testbench 验证设计。仓库根目录下的 `work/`、`*.mpf` 等就是它的工作区产物。

## 3. 本讲源码地图

本讲涉及的关键文件如下（均为真实存在文件）：

| 文件 | 所属目录 | 在本讲中的作用 |
| --- | --- | --- |
| `FPGA-Design/sharp.vhd` | FPGA-Design | **顶层实体**，看清整体端口与模块例化 |
| `FPGA-Design/sharp_slice.vhd` | FPGA-Design | 单颜色通道的二维滤波，例化 linemem + arith |
| `FPGA-Design/sharp_control.vhd` | FPGA-Design | 同步信号延迟（被顶层例化） |
| `FPGA-Design/sharp_arith.vhd` | FPGA-Design | 定点乘加与饱和截断（被 slice 例化） |
| `FPGA-Design/sharp_linemem.vhd` | FPGA-Design | 行存储循环缓冲（被 slice 例化） |
| `FPGA-Design/FIR.qsf` | FPGA-Design | Quartus 设置：器件、顶层、引脚、文件清单 |
| `Verification/sim_sharp.vhd` | Verification | 主 testbench：读 PPM、驱动顶层、写输出 PPM |
| `Octave/sharp_image_filter.m` | Octave | 参考锐化实现（`imfilter` 两次） |

> 说明：仓库实际内容都在 `FPGA-FIR-Filter-master/` 这个目录里；仓库根目录另有一份简短的 `README.md`、`LICENSE`，以及 Questa 仿真留下的工作区文件（见 4.4 节）。下文为简洁起见，路径有时省略 `FPGA-FIR-Filter-master/` 前缀，永久链接中会保留完整路径。

下面是一张精简的目录树（省略了大量编译产物文件，只保留结构性条目）：

```
FPGA-FIR-Filter-master/
├── README.md
├── LICENSE
├── Docs/                         ← 讲义与幻灯片
├── FPGA-Design/                  ← ① VHDL 设计源码 + Quartus 工程
│   ├── sharp.vhd                 ← 顶层实体 sharp
│   ├── sharp_slice.vhd           ← 单通道二维滤波
│   ├── sharp_control.vhd         ← 同步信号延迟
│   ├── sharp_arith.vhd           ← 定点乘加 / 饱和截断
│   ├── sharp_linemem.vhd         ← 行存储循环缓冲
│   ├── sharp.sdc                 ← 时序约束（74.25 MHz）
│   ├── FIR.qpf / FIR.qsf         ← Quartus 工程与设置
│   ├── sharp_default_Cyclone_{V,IV,10}.qsf  ← 三套备选约束（不同器件）
│   ├── db/                       ← 编译中间库（生成产物）
│   ├── incremental_db/           ← 增量编译数据（生成产物）
│   ├── output_files/             ← 最终产物：.sof 编程文件、各类报告
│   └── simulation/questa/        ← 门级仿真网表 FIR.vo
├── Verification/                 ← ② testbench + 测试图 + 生成脚本
│   ├── sim_sharp.vhd             ← 主 testbench
│   ├── sim_sharp_self-checking.vhd ← 逐像素自校验 testbench
│   ├── Sample.jpg / Sample_Test.ppm / Sample_Sharp.ppm  ← 测试图
│   ├── sharp_generate_testbench_images.m ← Octave 生成输入/期望图
│   └── write_ascii_ppm.m         ← 写 ASCII PPM 的辅助函数
└── Octave/                       ← ③ 算法设计与参考实现
    ├── sharp_filter_coefficients.m   ← fir1 设计定点系数
    ├── sharp_image_filter.m          ← 参考锐化（imfilter 两次）
    └── sharp_frequency_response.m    ← 频率响应 freqz
```

## 4. 核心概念与源码讲解

本讲把仓库拆成四个最小模块：**① FPGA-Design 设计源码**、**② Verification 仿真验证**、**③ Octave 算法脚本**、**④ 生成产物目录**。

### 4.1 FPGA-Design 设计源码

#### 4.1.1 概念说明

`FPGA-Design/` 是整个项目的「硬件实现」核心目录，存放两类东西：

- **VHDL 设计源码**（5 个 `sharp*.vhd` 文件）：描述 FPGA 内部的数字逻辑，是要被综合成硬件电路的「真正源码」。
- **Quartus 工程文件**（`FIR.qpf` / `FIR.qsf` / `sharp.sdc` 等）：告诉 Quartus 用哪个器件、顶层是谁、源码文件清单、引脚怎么接、时序怎么约束。

理解这个目录的关键，是抓住一条**模块例化层次（design hierarchy）**：顶层 `sharp` 把工作分派给几个子模块，子模块再分派给更小的子模块。这一讲我们只看「谁例化谁」，不深入每个模块的内部算法（那是 u3、u4 讲义的内容）。

#### 4.1.2 核心流程：从顶层到子模块的例化层次

```
sharp  (顶层实体，端口即 FPGA 对外引脚)
 │
 ├── sharp_slice × 3        （R / G / B 三个颜色通道各一个）
 │    ├── sharp_linemem × 6  （垂直方向 7 个抽头靠 6 个行存储级联）
 │    └── sharp_arith × 2    （先做垂直滤波，再做水平滤波）
 │
 └── sharp_control × 1      （把 vs/hs/de 同步信号延迟 6 拍，与数据对齐）
```

也就是说：数据流是 `顶层 → slice → (linemem, arith)`，而 `control` 是旁路，专门负责让同步信号和「被滤波器延迟了的数据」同时到达输出。这个层次关系是后面所有讲义的骨架。

#### 4.1.3 源码精读

**(1) 顶层 `sharp.vhd` 的端口**——这一段定义了 FPGA 对外的全部引脚：输入时钟、复位、视频输入（`vs_in/hs_in/de_in` 与 `r_in/g_in/b_in`）、视频输出、以及输出时钟和 LED。注意 `clk` 的注释写明是 74.25 MHz、720p 视频：

[FPGA-Design/sharp.vhd:12-33](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.vhd#L12-L33) —— 顶层实体 `sharp` 的端口声明，对外的视频输入/输出都在这里。

**(2) 顶层如何例化子模块**——顶层把输入寄存、类型转换后，**并行例化三个 `sharp_slice`**（分别处理 R/G/B）和一个 `sharp_control`（注意它带了 `generic map (delay => 6)`）：

[FPGA-Design/sharp.vhd:65-95](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.vhd#L65-L95) —— `r_slice`/`g_slice`/`b_slice` 三个 `sharp_slice` 实例 + 一个 `sharp_control` 实例，这就是顶层的全部例化。

输入寄存器把 `std_logic_vector` 转成 0~255 整数、输出寄存器再把整数转回 `std_logic_vector`，这两段转换在后面讲义会细讲，这里只要知道「类型转换发生在顶层」即可：

[FPGA-Design/sharp.vhd:48-63](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.vhd#L48-L63) —— 输入寄存器进程，`to_integer(unsigned(r_in))` 把位向量转成整数喂给 `sharp_slice`。

**(3) `sharp_slice` 如何例化下一级**——它是「承上启下」的中间层：用 `for ... generate` 例化 6 个 `sharp_linemem` 拼出 7 个垂直抽头，再例化两个 `sharp_arith` 分别做垂直、水平滤波：

[FPGA-Design/sharp_slice.vhd:29-36](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_slice.vhd#L29-L36) —— 用 generate 循环例化 6 个 `sharp_linemem`，级联形成垂直抽头链。

[FPGA-Design/sharp_slice.vhd:39-49](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_slice.vhd#L39-L49) —— 第一个 `sharp_arith`（垂直滤波），7 个抽头来自上面的 `v_tap` 数组。

**(4) Quartus 怎么知道这些文件属于工程**——看 `FIR.qsf` 里这几行关键设置：器件族、具体型号、顶层实体名，以及源码文件清单（注意 `sharp.sdc` 作为时序约束文件也在清单里）：

[FPGA-Design/FIR.qsf:40-42](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/FIR.qsf#L40-L42) —— `FAMILY "Cyclone V"`、`DEVICE 5CEBA2F17C6`、`TOP_LEVEL_ENTITY sharp`，工程的三项基本设置。

[FPGA-Design/FIR.qsf:125-130](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/FIR.qsf#L125-L130) —— `VHDL_FILE` / `SDC_FILE` 清单，Quartus 就是据此把 5 个 `.vhd` + 1 个 `.sdc` 纳入编译。

> 旁注：同目录还有 `sharp_default_Cyclone_V.qsf`、`sharp_default_Cyclone_IV.qsf`、`sharp_default_Cyclone_10.qsf` 三套文件，它们是为**不同 FPGA 器件**（Cyclone V / Cyclone IV E / Cyclone 10 LP）准备的备选约束，方便你在不同板子上移植；当前 `FIR.qsf` 用的是 Cyclone V。

#### 4.1.4 代码实践

**实践目标**：亲手建立 `FPGA-Design/` 下 5 个 `sharp*.vhd` 文件的「职责表」，并标注顶层 / 子模块。

**操作步骤**：
1. 用编辑器打开 `FPGA-Design/` 目录，列出所有 `sharp*.vhd`（应为 5 个；`sharp_control.vhd.bak` 是备份，不算）。
2. 对每个文件打开看一眼**文件头注释**和 `entity` 名，用一句话写出它的职责。
3. 在 `sharp.vhd` 和 `sharp_slice.vhd` 里搜索 `entity work.` 关键字，确认「谁例化了谁」。

**参考答案（对照参考）**：

| 文件 | 一句话职责 | 角色 |
| --- | --- | --- |
| `sharp.vhd` | 顶层实体，端口即 FPGA 引脚；例化 3 个 slice + 1 个 control，做输入/输出寄存与类型转换 | **顶层** |
| `sharp_slice.vhd` | 处理单个颜色通道的二维滤波；例化 6 个 linemem + 2 个 arith | 被 sharp 例化 |
| `sharp_control.vhd` | 把 vs/hs/de 同步信号延迟固定拍数 | 被 sharp 例化 |
| `sharp_arith.vhd` | 7 抽头 FIR 的定点乘加 + 四舍五入 + 饱和截断到 0~255 | 被 slice 例化 |
| `sharp_linemem.vhd` | 用 1280 项 RAM 做行延迟（循环缓冲） | 被 slice 例化 |

**预期结果**：你能画出 4.1.2 节那张层次图，并指出 `sharp` 是唯一顶层，其余 4 个都是被例化的子模块。**待本地验证**：你也可以用 `grep -n "entity work" sharp.vhd sharp_slice.vhd` 在本地确认例化关系。

#### 4.1.5 小练习与答案

**练习 1**：为什么顶层对 R/G/B **三个**通道各例化一个 `sharp_slice`，而不是只例化一个？
**参考答案**：因为每个 `sharp_slice` 只处理一个 0~255 的标量数据流（`data_in` / `data_out` 是单个整数）。R、G、B 是三路独立的像素分量，需要三路独立的滤波数据通路，所以例化三次。颜色之间互不影响，三个 slice 共用同一套 `clk/reset/de_in`。

**练习 2**：`FIR.qsf` 里的 `TOP_LEVEL_ENTITY sharp` 如果写错成别的名字，会发生什么？
**参考答案**：Quartus 会把那个名字当成顶层去综合；如果找不到对应实体，综合/适配阶段会报错（找不到顶层）。所以「顶层实体名」是工程设置与源码之间必须一致的关键纽带。

### 4.2 Verification 仿真验证

#### 4.2.1 概念说明

`Verification/` 目录解决的问题是：**怎么证明 VHDL 设计真的实现了锐化？** 它不靠「上板看一眼」，而是用 testbench（测试台）做可重复的、逐像素的验证。这个目录包含：

- **testbench 文件**（`sim_sharp.vhd`、`sim_sharp_self-checking.vhd`）：仿真时充当「外部世界」，给设计喂输入、采集输出。
- **测试图像**（`Sample.jpg`、`Sample_Test.ppm`、`Sample_Sharp.ppm`）：`Sample_Test.ppm` 是输入图，`Sample_Sharp.ppm` 是仿真产生的输出图。
- **测试图生成脚本**（`sharp_generate_testbench_images.m`、`write_ascii_ppm.m`）：用 Octave 产出输入 PPM 和「期望结果」PPM，供自校验比对。

#### 4.2.2 核心流程：testbench 如何驱动设计

一个 testbench 通常包含「激励进程」和「响应进程」两条并发的 VHDL 进程：

```
激励进程 stimuli_process              响应进程 response_process
   读输入 PPM 像素                       等待 hs_out='1' 开始
   → 重建 vs/hs/de 时序                  → 当 de_out='1' 时采集 r_out/g_out/b_out
   → 喂给顶层 sharp                       → 写入输出 PPM
   → 全部喂完置 end_tb=1                  → 看到 end_tb=1 关闭文件
```

`sim_sharp.vhd` 走「**只生成输出图**」的路线；`sim_sharp_self-checking.vhd` 在此基础上还读一张「期望图」，逐像素比对并统计 mismatch（后面 u5 讲义会精读）。

#### 4.2.3 源码精读

**(1) testbench 例化被测设计**——`sim_sharp.vhd` 把顶层 `sharp` 当作「被测器件（DUV, Design Under Verification）」例化进来，并把 testbench 内部的信号连到它的端口：

[Verification/sim_sharp.vhd:56-73](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp.vhd#L56-L73) —— `duv : entity work.sharp port map (...)`，testbench 通过这处例化驱动整个设计。

**(2) 硬编码的文件路径**——注意开头这两个常量是写死的 **Windows 绝对路径**（原作者机器上的路径），你本地跑之前必须改：

[Verification/sim_sharp.vhd:22-26](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp.vhd#L22-L26) —— `stimuli_filename` / `response_filename` 指向输入 PPM 与输出 PPM，以及 `x_blank`（水平消隐）、`trail`（收尾时钟周期）两个仿真常量。

**(3) 激励进程与响应进程**——这两个进程分别负责「喂数据」和「采数据」，用 `end_tb` 信号协调结束：

[Verification/sim_sharp.vhd:76-167](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp.vhd#L76-L167) —— 激励进程：逐行读 PPM，按 `x_blank` 重建水平消隐，把 `de_in` 置 1 时喂有效像素。

[Verification/sim_sharp.vhd:170-216](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp.vhd#L170-L216) —— 响应进程：每当 `de_out='1'` 就把 `r_out/g_out/b_out` 写成一行 PPM，`end_tb=1` 时关闭文件。

#### 4.2.4 代码实践

**实践目标**：定位 testbench 的两个关键点（文件路径、两条进程），为以后改路径、跑仿真做准备。

**操作步骤**：
1. 打开 `Verification/sim_sharp.vhd`。
2. 找到第 23、24 行的两个文件路径常量，记住它们的格式（`C:\Users\...\\...\\Sample_Test.ppm`）。
3. 在文件里搜索 `stimuli_process` 和 `response_process`，确认这是两个独立 `process`。

**需要观察的现象 / 预期结果**：你能指出——「输入图路径」在 23 行、「输出图路径」在 24 行；激励进程负责生成 `vs_in/hs_in/de_in` 时序并喂像素，响应进程负责在 `de_out='1'` 时把输出像素写回 PPM。**待本地验证**：把这两行路径改成你本地的相对路径后，testbench 才能在你的仿真器里找到图像。

#### 4.2.5 小练习与答案

**练习 1**：`sim_sharp.vhd` 用的是哪种 PPM 格式？从哪里能看出来？
**参考答案**：用的是 **ASCII（P3）格式**的 PPM。证据在响应进程里写出文件头 `write(l_sim, string'("P3"))`（`P3` 就是 ASCII PPM 的魔数），并且像素值是逐个整数写出的，而不是二进制（二进制是 `P6`）。

**练习 2**：`x_blank`（第 25 行）这个常量在 testbench 里模拟什么？
**参考答案**：它模拟**水平消隐**——每行有效像素之前的一段「没有画面」的时钟周期。激励进程在喂有效像素前，先循环等待 `x_blank` 个时钟，并把 `hs_in` 在这段时间内置 1，从而还原真实视频流的时序结构。

### 4.3 Octave 算法脚本

#### 4.3.1 概念说明

`Octave/` 目录是「软件先行」那一半：在写任何 VHDL 之前，先用 GNU Octave（一个免费的、语法接近 MATLAB 的数值计算环境）把**算法和系数**算清楚。这个目录有三个脚本，各管一件事：

- `sharp_filter_coefficients.m`——用 `fir1` 设计高通 FIR 系数并定点化（产出 `[1,0,-9,48,-9,0,1]/32`）。
- `sharp_image_filter.m`——对一张真实图片跑参考锐化（`imfilter` 两次），看效果对不对。
- `sharp_frequency_response.m`——画出滤波器的频率响应（`freqz`），确认它确实是「增强高频、保留直流」。

> 这些脚本是「参考实现」：硬件 `sharp_arith.vhd` 里的系数必须和这里的系数**一一对应**，否则软硬件就不一致了。

#### 4.3.2 核心流程：从系数到参考图

```
sharp_filter_coefficients.m           sharp_image_filter.m
   fir1(8,0.5,"high")  → 高通系数        f_hor = [1,0,-9,48,-9,0,1]/32
   round(32 * ...)     → 定点整数          f_ver = f_hor' （转置成垂直核）
   得到 [1,0,-9,48,-9,0,1]                img_tmp = imfilter(img_in, f_ver)   ← 先垂直
                                          img_out = imfilter(img_tmp, f_hor)   ← 再水平
```

注意：水平核 `f_hor` 是 1×7 的行向量，垂直核 `f_ver = f_hor'` 是 7×1 的列向量，两者数值相同，这正是「可分离滤波」让硬件能复用同一个 `sharp_arith` 的原因。

#### 4.3.3 源码精读

**(1) 参考锐化的核心三行**——读图、两次 `imfilter`、写图，浓缩在很短的脚本里：

[Octave/sharp_image_filter.m:10-19](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Octave/sharp_image_filter.m#L10-L19) —— `f_hor` 是水平系数、`f_ver = f_hor'` 是转置后的垂直系数；先 `imfilter(img_in, f_ver)` 再 `imfilter(img_tmp, f_hor)` 完成二维锐化。

**(2) 系数设计脚本**——只有两行有效代码，但它是整个项目系数的「源头」：

[Octave/sharp_filter_coefficients.m:9-13](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Octave/sharp_filter_coefficients.m#L9-L13) —— `fir1(8,0.5,"high")` 设计 8 阶高通 FIR，`round(32*...)` 把浮点系数量化成定点整数。把它的输出和 `sharp_arith.vhd` 第 27 行的注释 `[1;0;-9;48;-9;0;1]/32` 对照，就能确认软硬件一致。

#### 4.3.4 代码实践

**实践目标**：把三个 Octave 脚本和它们的职责对上号（纯阅读型实践，不需要立刻运行）。

**操作步骤**：
1. 打开 `Octave/` 下三个 `.m` 文件，只看每个文件**开头几行注释**和**关键函数名**。
2. 把脚本名与它调用的关键函数（`fir1` / `imfilter` / `freqz`）对应起来。

**预期结果（对照参考）**：

| 脚本 | 关键函数 | 职责 |
| --- | --- | --- |
| `sharp_filter_coefficients.m` | `fir1`、`round` | 设计并定点化高通系数 |
| `sharp_image_filter.m` | `imread`、`imfilter`、`imwrite` | 跑参考锐化、看效果 |
| `sharp_frequency_response.m` | `freqz` | 画频率响应 |

**待本地验证**：若你已装好 Octave 并加载 `signal` / `image` 包，可运行 `sharp_image_filter.m`（需把第 11 行的图片名换成你本地的图）观察锐化效果；这同时也是 u1-l1 综合实践的内容。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `f_ver = f_hor'`（转置）就能从水平核得到垂直核？
**参考答案**：`f_hor` 是 1×7 的行向量，代表水平方向 7 个抽头的权重；转置成 7×1 的列向量后，同样这 7 个权重就作用在「垂直方向的 7 个像素」上。因为本项目水平和垂直用**同一组系数**，所以一个转置就够了——这正是可分离滤波的好处。

**练习 2**：Octave 脚本算出的系数，和硬件里哪个文件直接相关？
**参考答案**：和 `FPGA-Design/sharp_arith.vhd` 直接相关——该文件第 27 行的注释 `[1;0;-9;48;-9;0;1]/32`、第 34 行的乘加表达式 `sum := (tap_m3 - (9*tap_m1) + (48*tap_00) - (9*tap_p1) + tap_p3 + 16) / 32`，正是把这里的定点系数硬编码进了硬件。

### 4.4 生成产物目录

#### 4.4.1 概念说明

仓库里还有不少文件**不是手写源码**，而是 Quartus 编译或 Questa 仿真**自动生成**的产物。初学者最容易把它们误当成「项目的一部分」而小心翼翼不敢动，或者反过来误删了源码。本模块帮你把两类分清：

- **Quartus 产物**（在 `FPGA-Design/` 内）：`db/`、`incremental_db/`、`output_files/`、`simulation/questa/`。
- **Questa 仿真产物**（在仓库根目录）：`work/`、`FIR_Filter.mpf`、`FIR_Filter.cr.mti`、`transcript`、`vsim.wlf`。

它们的共同点是：**删掉之后，重新编译 / 重新仿真就会再次生成**。因此它们通常不进版本控制（本项目仓库里包含了它们，属于历史遗留，但你不应该去手改）。

#### 4.4.2 核心流程：Quartus 一次编译会产出什么

```
源码 (.vhd) + 约束 (.qsf/.sdc)
        │  Analysis & Synthesis（分析综合）
        ▼
      db/                       ← 中间数据库（映射、网表片段）
        │  Fitter（布局布线）
        ▼
 incremental_db/               ← 增量编译数据（加速重编译）
        │  Assembler / TimeQuest
        ▼
 output_files/                 ← 最终交付：
   ├── FIR.sof                  ← ★ 下载到 FPGA 的编程文件
   ├── FIR.fit.rpt / .sta.rpt   ← 资源占用 / 时序报告
   └── FIR.pin                  ← 引脚报告
```

`simulation/questa/FIR.vo` 则是编译后产出的**门级仿真网表**（Verilog 格式），用于做更贴近真实硬件的时序仿真。

#### 4.4.3 源码精读（产物识别）

这一节没有「手写源码」可读，重点是**识别关键产物文件**。最有价值的是 `output_files/` 下的报告，它们是读懂「设计在真实器件上表现如何」的入口：

- `output_files/FIR.sof`——**SRAM Programmer Object File**，最终下载到 Cyclone V 的比特流文件。这就是「编译的最终产物」。
- `output_files/FIR.fit.summary`——适配摘要，告诉你用了多少逻辑单元（LE）、多少存储位。
- `output_files/FIR.sta.summary`——时序摘要，告诉你是否满足 74.25 MHz 时序（slack 是否为正）。
- `output_files/FIR.pin`——引脚报告，列出每个信号实际被分配到哪个物理引脚。

> 提示：报告类文件（`.rpt` / `.summary` / `.pin`）是**纯文本**，可以用编辑器直接打开阅读，是后续 u1-l3、u6 讲义的重要依据。`work/`、`*.mpf`、`transcript`、`vsim.wlf` 则是 Questa 仿真器的工作区与日志，跑过一次仿真后自动生成。

#### 4.4.4 代码实践

**实践目标**：学会区分「源码」与「产物」，知道哪些可以安全删除。

**操作步骤**：
1. 在 `FPGA-Design/output_files/` 下找到 `FIR.sof`，确认它是二进制编程文件（下载用的）。
2. 用编辑器打开 `output_files/FIR.fit.summary`（纯文本），看一眼它的资源占用数字。
3. 在仓库根目录找到 `work/` 目录和 `FIR_Filter.mpf` 文件，确认它们是 Questa 的工作区。

**需要观察的现象 / 预期结果**：
- `.sof` 是二进制；`.fit.summary` / `.sta.summary` / `.pin` 是可读文本。
- `db/`、`incremental_db/`、`output_files/`、`work/` 都是**可以删除并重新生成**的，删除后只要重新跑一次 Quartus 编译 / Questa 仿真就会再生成。
- 而 5 个 `sharp*.vhd`、`FIR.qsf`、`FIR.qpf`、`sharp.sdc` 是**源码/工程文件，不能删**。**待本地验证**：你可以在 Quartus 里删掉 `output_files/` 后重新编译，确认它会重新出现。

#### 4.4.5 小练习与答案

**练习 1**：假如你想把整个工程发给别人并希望仓库尽量小，哪些目录可以删掉？
**参考答案**：可以删掉所有生成产物：`FPGA-Design/db/`、`FPGA-Design/incremental_db/`、`FPGA-Design/output_files/`、`FPGA-Design/simulation/`，以及根目录的 Questa 工作区 `work/`、`*.mpf`、`*.mti`、`transcript`、`vsim.wlf`。保留 5 个 `.vhd`、`FIR.qpf`、`FIR.qsf`、`sharp.sdc`、`Octave/`、`Verification/` 即可，对方重新编译就能还原。

**练习 2**：我想知道设计在 Cyclone V 上占用了多少逻辑单元，该看哪个文件？
**参考答案**：看 `FPGA-Design/output_files/FIR.fit.summary`（适配摘要）或更详细的 `FIR.fit.rpt`，里面有逻辑单元（LE）、寄存器、存储块等资源占用统计。

## 5. 综合实践

**实践目标**：把四个目录串成一条完整的「协同链路」，建立对整个仓库的全局认知。

**任务**：请画一张「四目录协同图」，并回答以下问题，把本讲的知识点串起来。

1. **算法从哪里来**：锐化系数最初是哪个目录、哪个脚本算出来的？（提示：`Octave/sharp_filter_coefficients.m`）
2. **硬件实现是谁**：这个系数被硬编码进了 `FPGA-Design/` 下的哪个文件？（提示：`sharp_arith.vhd`）
3. **怎么验证**：`Verification/` 里的哪个脚本负责生成 testbench 需要的「输入 PPM」和「期望 PPM」？（提示：`sharp_generate_testbench_images.m`）
4. **怎么上板**：`FPGA-Design/` 里哪个生成产物是最终下载到 FPGA 的？（提示：`output_files/FIR.sof`）
5. **层次关系**：在 `FPGA-Design/` 下，画出 `sharp → sharp_slice → (sharp_linemem, sharp_arith)` 以及 `sharp → sharp_control` 的例化树。

**预期结果**：你应该得到一条清晰的链路——

```
Octave（算系数/参考图） → FPGA-Design（VHDL 实现 + Quartus 编译出 .sof）
                              ↑ 被验证
                   Verification（testbench 用 PPM 逐像素比对）
```

这条链路正是上一讲「软件先行、硬件跟进、仿真把关」工作流在文件层面的落地。如果某一步你答不上来，回到对应小节再确认一遍。

## 6. 本讲小结

- 仓库主体在 `FPGA-FIR-Filter-master/` 下，分为四大块：`FPGA-Design`（硬件实现）、`Verification`（仿真验证）、`Octave`（算法与系数）、`Docs`（讲义资料）。
- `FPGA-Design/` 有 5 个 `sharp*.vhd`：`sharp.vhd` 是顶层，它例化 `sharp_slice`（×3）和 `sharp_control`；`sharp_slice` 又例化 `sharp_linemem`（×6）和 `sharp_arith`（×2）。
- 顶层到子模块的层次关系是后面所有讲义的骨架：数据走 `sharp → slice → (linemem, arith)`，同步信号走 `sharp → control`。
- `Verification/sim_sharp.vhd` 是主 testbench，靠「激励进程 + 响应进程」两条并发进程驱动顶层 `sharp`，读写 ASCII(P3) PPM 图像；注意它的文件路径是硬编码的 Windows 路径，本地使用前要改。
- `Octave/` 是「软件参考实现」，`sharp_filter_coefficients.m` 产出的定点系数与 `sharp_arith.vhd` 里的硬编码系数一一对应，保证软硬件一致。
- `db/`、`incremental_db/`、`output_files/`、`simulation/`、根目录 `work/` 等都是**生成产物**，可删可重建；源码与工程文件（`.vhd` / `.qpf` / `.qsf` / `.sdc`）才是必须保留的。

## 7. 下一步学习建议

本讲只让你「看懂地图」，还没有打开 Quartus 工程实际编译。下一讲 **u1-l3《Quartus 工程与目标平台 Cyclone V》** 会带你：

- 用 Quartus Prime 打开 `FIR.qpf`，确认器件 `5CEBA2F17C6`、顶层实体 `sharp`、74.25 MHz 时钟等工程设置。
- 走一遍编译流程，并学会读 `output_files/` 下的适配报告（`FIR.fit.summary`）。

如果你想提前预习源码，建议先粗读 **`FPGA-Design/sharp.vhd`**（最短、最顶层），建立「端口 → 例化」的整体印象；等进入 u3 单元再逐层下钻到 `sharp_slice` / `sharp_arith` / `sharp_linemem` 的内部实现。
