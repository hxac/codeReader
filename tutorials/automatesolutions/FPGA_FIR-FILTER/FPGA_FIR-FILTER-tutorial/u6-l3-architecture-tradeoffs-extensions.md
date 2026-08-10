# 架构取舍与二次开发实践

## 1. 本讲目标

前面十一讲我们把这套 FPGA FIR 滤波器拆得很细：U2 讲了锐化系数 `[1,0,-9,48,-9,0,1]/32` 从哪来；U3 顺着 `sharp_slice` 走完「先垂直后水平」的可分离二维数据流；U4 进到行存储与定点乘加；U5 把 Octave 软件参考与硬件仿真逐像素对齐；U6 前两讲又落到了引脚约束与时序收敛。到这里，这个项目已经被我们从算法到物理、从软件到硬件完整走了一遍。

本讲是收官篇，不再讲新机制，而是退后一步回答三个「为什么」和「怎么改」：

1. 这个设计在**可分离滤波、定点精度、片上存储**三方面做了哪些取舍？代价是什么、换来了什么？
2. 如果想把锐化换成别的效果（比如平滑、边缘检测），**改哪里、改多少、哪些能不动**？
3. 怎么把「改系数 → 改硬件 → 跑自校验」串成一个**可重复的二次开发闭环**，让每次改动都能被自动验证？

读完本讲你应该能够：

- 用一张表说清楚「直接二维卷积 vs 可分离」「浮点 vs 定点」「移位寄存器 vs 行存储 RAM」这几组取舍的得与失。
- 判断一个新滤波需求**需要动哪几个文件**：是只改一行 `sum` 表达式，还是必须重写 `sharp_slice` 的抽头结构。
- 把 Octave 系数设计、VHDL 运算核、自校验测试台串成一条闭环，独立完成一次滤波器改型并验证通过。

本讲对应的最小模块是：**架构取舍分析**、**系数与抽头修改**、**二次开发闭环验证**。

## 2. 前置知识

本讲是综合篇，假设你已掌握前三篇进阶讲义的结论。这里只把最关键的几条拎出来复述，细节不再展开。

### 可分离二维滤波的硬件兑现

锐化核是同一个一维核的外积（秩为 1），因此 7×7 二维卷积可以拆成「先垂直 7 抽头、再水平 7 抽头」两次一维卷积。`sharp_slice` 用 6 块 `sharp_linemem` 级联凑出 7 个垂直抽头 `v_tap(0..6)`，再用 6 级移位寄存器凑出 7 个水平抽头 `h_tap(0..6)`，两段各接一个 `sharp_arith`。乘法次数从 49 降到 14（再扣掉两个 0 系数抽头，实际只有 10 次）。详见 u3-l3。

### 定点乘加、舍入与饱和

`sharp_arith` 把 7 个抽头按 `[1,0,-9,48,-9,0,1]/32` 做加权求和，先在全精度整数上累加，最后整体除以 32（右移 5 位）；除之前 `+16` 实现「四舍五入」而不是「截断」；最后用三分支 `if` 把结果饱和到 `0..255`。`tap_m2`/`tap_p2` 因 sinc 过零而系数为 0，端口保留却不参与运算。详见 u2-l2 与 u4-l2。

### 自校验闭环

`sim_sharp_self-checking.vhd` 同时读「输入 PPM」和「Octave 生成的期望 PPM」，把硬件输出与期望像素逐个比对，跳过图像边缘，累计 `mismatch`，仿真结束自动报 `EVERYTHING OK` 或 `N MISMATCHES`。期望图由 `sharp_generate_testbench_images.m` 用与硬件**完全相同**的系数和舍入方式产出。详见 u5-l2、u5-l3。

### 资源与时序基线

综合后约 4% ALM、16% 片上存储、0 个 DSP；行存储是存储大户；74.25 MHz 下 Setup Slack 仅 +0.658 ns，关键路径在 `sharp_arith` 的乘加进位链。详见 u6-l1、u6-l2。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注的部分 |
|------|------|----------------|
| `FPGA-Design/sharp_arith.vhd` | **改系数的主战场**：一行 `sum` 表达式 + 饱和逻辑 | 第 27、30、34、37–43 行 |
| `FPGA-Design/sharp_slice.vhd` | **抽头结构**：决定要不要动 linemem/移位寄存器数量 | 第 20、27、29–36、39–49、51–58、60–70 行 |
| `FPGA-Design/sharp_linemem.vhd` | 行存储 RAM，1280 项循环缓冲 | 第 20、31–46 行 |
| `Octave/sharp_filter_coefficients.m` | 系数设计原型（`fir1` + 定点化） | 第 11–13 行 |
| `Verification/sharp_generate_testbench_images.m` | 生成输入/期望 PPM，**改型后必须同步改这里** | 第 14–18 行 |

一句话总览：二次开发时，**算法层**动 `sharp_filter_coefficients.m` 和 `sharp_generate_testbench_images.m`，**硬件层**动 `sharp_arith.vhd`（小改）或连带 `sharp_slice.vhd`（大改），**验证层**几乎不用动 `sim_sharp_self-checking.vhd` 本身——它只是「重新喂一张新期望图」。

## 4. 核心概念与源码讲解

### 4.1 架构取舍分析

#### 4.1.1 概念说明

任何工程实现都是一连串取舍。这套设计在三个维度上做了明确选择，理解这三个维度，你才知道改型时什么能碰、什么碰了要付代价。

- **可分离 vs 直接二维**：把 7×7 卷积拆成两次 7 抽头一维卷积，省乘法、省硬件，但**前提是核必须可分离**（秩为 1）。锐化核恰好可分离，所以能用；换成 Sobel、Laplacian 这类不可分离核，这套两级架构就失效了。
- **定点 vs 浮点**：系数统一放大 32 倍取整，除法退化成右移 5 位。好处是硬件里没有真正的「除法器」也没有浮点单元；代价是引入量化误差，且系数必须凑成「乘以 32 后接近整数」。
- **行存储 RAM vs 移位寄存器**：垂直方向要访问「上一行、上上行……」，必须存整行像素。设计用 1280 项 RAM 循环缓冲而不是 1280 级触发器移位寄存器，省下海量触发器；代价是读写需要地址管理、读改写时序更微妙。

这三条取舍的共同主题是：**用结构上的约束换资源上的节省**。可分离是「用算法约束换乘法器」；定点是「用精度换掉除法器/浮点单元」；行存储是「用地址管理换掉触发器」。FPGA 上资源（ALM、RAM、DSP）都有限，这种「拿约束换资源」的思路是嵌入式图像处理的通用范式。

#### 4.1.2 核心流程：三组取舍的得失

下面把三组取舍整理成可直接对照的形式。

**取舍一：可分离滤波的乘法量。** 直接做 7×7 二维卷积，每个输出像素要 49 次乘加。拆成两次 7 抽头一维卷积后：

\[ \text{乘加次数}_{\text{可分离}} = 2 \times 7 = 14 \]

再扣掉本设计里两个系数恰为 0 的抽头（`tap_m2`/`tap_p2`），实际每个 `sharp_arith` 只做 5 次乘法，两级合计 10 次。也就是说，相对直接二维卷积，乘法量降到约 \(10/49 \approx 20\%\)。代价是：垂直结果必须先算完、存进水平移位寄存器，才能开始水平卷积——多了一级中间存储与一级流水线延迟。

**取舍二：定点系数与常数乘法。** `sharp_arith` 里的乘数只有 `9`、`48`、`1`（以及隐含的 `0`），它们对综合器而言不是「通用乘法」，而是**移位加法树**：

\[ 48 \cdot x = (x \ll 5) + (x \ll 4), \qquad 9 \cdot x = (x \ll 3) + x \]

这正是编译报告里 **0 个 DSP** 的原因——不需要硬件乘法器，几级 LUT 进位链就够。代价是：系数必须凑成「移位加法友好」的整数，且整体除以 32（右移 5）才能恢复正确增益。如果你改型时引入一个像 `85` 这样的系数（例如把 `/3` 近似成 `85/256`），移位加法树会变深，关键路径变长，可能挤掉本来就不富裕的时序余量。

**取舍三：行存储。** 每个颜色通道 6 块 linemem，每块 1280 项，三项 RGB 合计：

\[ 3 \times 6 \times 1280 = 23040 \text{ 字节} \]

这就是 16% 片上存储的来源，也是本设计**最重的资源开销**。相比之下，ALM 只占约 4%，运算很轻、存储很重——这是个典型的「面向大窗口、面向行缓冲」的视频处理资源画像。

#### 4.1.3 源码精读

先看「常数乘法」落在哪里——就是这一行：

`sum` 表达式 [`sharp_arith.vhd:34`](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_arith.vhd#L34)：

```vhdl
sum := (tap_m3 - (9*tap_m1) + (48*tap_00) - (9*tap_p1) + tap_p3 + 16) / 32;
```

注意它只出现 `tap_m3`、`tap_m1`、`tap_00`、`tap_p1`、`tap_p3` 这 5 个抽头——`tap_m2`/`tap_p2`（系数为 0）被直接省略。乘数 `9`、`48`、隐含的 `1` 全是移位加法友好的整数，这就是「0 DSP」的代码层证据。系数注释 [`sharp_arith.vhd:27`](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_arith.vhd#L27) 也写明 `[1;0;-9;48;-9;0;1]/32`，两个 0 与省略的两个抽头一一对应。

再看「行存储」有多重——linemem 的 RAM 容量声明 [`sharp_linemem.vhd:20`](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_linemem.vhd#L20)：

```vhdl
type ram_array is array (0 to 1279) of integer range 0 to 255;
```

1280 项 × 8 位 = 每块 1280 字节。`sharp_slice` 用 `generate` 循环 [`sharp_slice.vhd:29-L36`](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_slice.vhd#L29-L36) 把 6 块这样的 RAM 级联起来。最后看「可分离」的两级结构：垂直 arith [`sharp_slice.vhd:39-L49`](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_slice.vhd#L39-L49) 吃 7 个 `v_tap`、吐 `v_out`；`v_out` 进水平移位寄存器 [`sharp_slice.vhd:51-L58`](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_slice.vhd#L51-L58) 凑出 `h_tap`；水平 arith [`sharp_slice.vhd:60-L70`](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_slice.vhd#L60-L70) 再做一次同结构卷积。两个 `sharp_arith` 是同一个实体、同一组系数——这就是「可分离」在硬件上的字面体现：**一个一维核用了两次**。

#### 4.1.4 代码实践：从源码盘点资源画像

这是一个**源码阅读型实践**，不需要仿真器，目标是让你亲手从代码里「算出」资源画像，体会存储重、运算轻。

1. **实践目标**：用源码里的常量算出行存储总量与有效乘法次数，与编译报告的「16% 存储、0 DSP」对上号。
2. **操作步骤**：
   - 打开 [`sharp_linemem.vhd:20`](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_linemem.vhd#L20)，确认每块 RAM = 1280 字节。
   - 打开 [`sharp_slice.vhd:29-L36`](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_slice.vhd#L29-L36)，数 `generate` 循环例化了几块 linemem（应为 6）。
   - 算出每通道行存储 = 6 × 1280 = 7680 字节；顶层例化了 3 个 `sharp_slice`（见 u3-l1），合计 23040 字节。
   - 打开 [`sharp_arith.vhd:34`](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_arith.vhd#L34)，数实际出现的乘法：`9*tap_m1`、`48*tap_00`、`9*tap_p1` 共 3 次（`tap_m3`、`tap_p3` 系数为 1 不算乘法）。
   - 一个 `sharp_slice` 有两个 `sharp_arith`，所以每通道 6 次乘法、三通道共 18 次（全部是常数乘法 → 0 DSP）。
3. **需要观察的现象**：行存储字节远大于乘法次数；乘数全是 `9`、`48` 这种「2 的幂之和」。
4. **预期结果**：行存储 ≈ 23 KB（对应 16% 片上 RAM），乘法全是移位加法可实现（对应 0 DSP）。这与 u6-l1 的编译结论一致。
5. 若你手头有编译报告（Fitter 报告），可对照实际数字；若没有，以上计算即为结论。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `sharp_slice` 的垂直抽头链从 7 个减到 5 个（去掉 `tap_m3`/`tap_p3`），行存储能省多少？锐化核还能原样用吗？

> **答案**：linemem 从 6 块减到 4 块，每通道省 2 × 1280 = 2560 字节，三通道省约 7.5 KB。但锐化核 `[1,0,-9,48,-9,0,1]/32` 的两端系数为 1，去掉 `tap_m3`/`tap_p3` 会丢掉这两项，直流增益从 1 变成 \(30/32\)，图像会整体变暗，且边缘响应改变——系数必须重新设计。

**练习 2**：为什么本设计用 RAM 做行存储，而水平方向用触发器移位寄存器？

> **答案**：水平方向只需延迟 1～6 个像素（几十位），用触发器很便宜；垂直方向要延迟一整行 1280 个像素，若用触发器移位寄存器要 1280 级 ×8 位，远比一块 RAM 贵。「行用 RAM、列用触发器」是视频处理的经典划分。

**练习 3**：系数 `48` 和 `9` 为什么不会消耗 DSP？

> **答案**：因为它们是常数，综合器把它们实现成移位加法树：\(48x=(x\ll5)+(x\ll4)\)、\(9x=(x\ll3)+x\)，只需 LUT 与进位链，不必动用通用硬件乘法器。

---

### 4.2 系数与抽头修改

#### 4.2.1 概念说明

二次开发的本质是「换滤波效果」。本项目的运算核 `sharp_arith` 把系数**硬编码**在一行 `sum` 表达式里，抽头结构**硬编码**在 `sharp_slice` 里。所以改型的影响范围完全取决于「新核需要几个抽头、系数是不是 2 的幂友好」。由此分出三档改动量：

- **小改（只动 `sharp_arith` 一行）**：新核仍是 7 抽头可分离、系数仍可凑成 `/32`。最理想。
- **中改（动 `sharp_arith` + 用零填充借用 7 抽头结构）**：新核实际只要 3 抽头，但故意写成 `[0,0,a,b,a,0,0]/32` 塞进 7 抽头。这样 `sharp_slice` 完全不动，是本讲的推荐做法。
- **大改（动 `sharp_slice` 抽头数 + `sharp_arith` 端口）**：真把结构从 7 抽头缩到 3 抽头来省资源。改动面最大，但能真正减少 linemem。

判断该走哪一档，是这一节的核心技能。

#### 4.2.2 核心流程：以「3×3 均值平滑」为例选系数

本讲的实践任务是把锐化换成 **3×3 均值平滑核**。3×3 均值是 \(\frac{1}{9}\begin{bmatrix}1&1&1\\1&1&1\\1&1&1\end{bmatrix}\)。它是可分离的（全 1 矩阵是秩 1），一维核是 \([1,1,1]/3\)。所以可以用这套两级架构。难点在于系数的定点化。

**问题：除以 3 不是 2 的幂。** 硬件目前依赖「除以 32 = 右移 5」。如果直接写 `sum := (tap_m1 + tap_00 + tap_p1) / 3`，VHDL 整数除以 3 能综合，但会生成一个真正的「除 3」电路，比移位贵得多，也打破了本项目「0 个真除法器」的简洁性。

**解法：保持 `/32` 缩放，凑出和为 32 的系数。** 把一维均值核塞进 7 抽头结构的中心 3 位，其余位置填 0：希望找到 \(a,b,a\) 使 \(2a+b=32\)（直流增益为 1）、且三者尽量接近（尽量均匀平滑）。取 \(a=11, b=10\)：

\[ [0,\,0,\,11,\,10,\,11,\,0,\,0] \;/\; 32 \]

校验直流增益：\((11+10+11)/32 = 32/32 = 1\) ✓。校验平滑性：三个系数接近（10、11、11），近似等权均值。乘数 `11 = 8+2+1 = (x\ll3)+(x\ll1)+x`、`10 = 8+2 = (x\ll3)+(x\ll1)`，仍是移位加法友好，不引入 DSP、不显著加长关键路径。

> 关键洞察：**因为 sinc 在 ±2 过零让锐化核有了两个 0 系数，7 抽头结构天然「中间 3 位」是自由的。** 把均值核塞进这 3 个自由位，等于「免费」复用了整套 linemem 和移位寄存器，无需改动 `sharp_slice`。这正是「中改」路线的精妙之处——**用零填充把小核装进大核的结构里**。

下表把三档改法对照清楚（以 3×3 均值为例）：

| 维度 | 锐化（原设计）| 均值·零填充（推荐中改）| 均值·真 3 抽头（大改）|
|------|------|------|------|
| 抽头结构 | 7 | 7（结构不变）| 3 |
| 有效非零系数 | 5 | 3（m1, 00, p1）| 3 |
| 系数（每方向）| `[1,0,-9,48,-9,0,1]/32` | `[0,0,11,10,11,0,0]/32` | `[11,10,11]/32` |
| 直流增益 | 1 | 1 | 1 |
| 缩放 | `/32`（右移 5）| `/32` | `/32` |
| 行存储/通道 | 6 块 | 6 块（不动）| 2 块 |
| 需改文件 | — | 仅 `sharp_arith.vhd` | `sharp_slice.vhd` + `sharp_arith.vhd` |
| `sharp_control` delay | 6 | 6（不变）| 需重算（抽头数变了）|
| 边缘跳过范围（testbench）| ±3 | ±3（仍有效）| ±1（可放宽）|

注意「真 3 抽头」虽然省了 4 块 linemem，但会改变数据通路延迟，`sharp_control` 的 `delay=>6` 要重算（见 u3-l2 的对齐公式），边缘跳过范围也要相应放宽——改动会传染到多个文件。所以**除非存储真的吃紧，否则优先走零填充的中改路线**。

#### 4.2.3 源码精读

先看要改的那一行。原锐化的 `sum` [`sharp_arith.vhd:34`](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_arith.vhd#L34)：

```vhdl
sum := (tap_m3 - (9*tap_m1) + (48*tap_00) - (9*tap_p1) + tap_p3 + 16) / 32;
```

均值零填充版只需把这一行换成（**示例代码**，非项目原有）：

```vhdl
-- filter coefficients [0;0;11;10;11;0;0]/32  (3x3 box blur, separable)
sum := (11*tap_m1 + 10*tap_00 + 11*tap_p1 + 16) / 32;
```

几个要点：

- 系数全是正数，平滑核是低通的，**几乎不会产生过冲（>255）或振铃（<0）**，所以饱和逻辑 [`sharp_arith.vhd:37-L43`](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_arith.vhd#L37-L43) 很少触发，但仍要保留（图像里本就饱和的 255 区域平滑后仍可能贴边）。
- `+16` 四舍五入 [`sharp_arith.vhd:34`](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_arith.vhd#L34) 保持不变——它是「除以 32 前加半截」的通用技巧，与具体系数无关。
- `sum` 变量范围 [`sharp_arith.vhd:30`](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_arith.vhd#L30) 声明为 `integer range -512 to 511`。均值核最大输入 \(3\times255\times11\) 量级远小于 511？要核对：实际 `sum` 在除以 32 之前的原始累加值最大约 \(11\times255+10\times255+11\times255 = 32\times255 = 8160\)，**远超 511**！

> ⚠️ **这是一个必须留意的陷阱**：原锐化核的累加值因为有正有负、且系数小，落在 ±512 内；但均值核全正且系数和为 32，\(32\times255=8160\) 远超 `sum` 当前的 `-512..511` 范围，**会溢出**。改型时必须同时放宽 `sum` 的范围声明，例如改为 `integer range 0 to 8191`。这是「只改一行 `sum` 表达式」最容易漏掉的连带改动。具体上限值待本地根据系数核对后确定。

这也提醒我们：**改系数不是孤立的**，要顺着 `sum` 的范围、饱和上下限一起检查。

再看 Octave 侧的系数原型 [`sharp_filter_coefficients.m:11-L13`](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Octave/sharp_filter_coefficients.m#L11-L13)：

```matlab
fir1(8,0.5, "high")
round(32*fir1(8,0.5, "high"))
```

原脚本是「`fir1` 设计高通 → 乘 32 取整」。均值核不需要 `fir1`，可以直接写死系数做验证（**示例代码**）：

```matlab
f_hor = [0, 0, 11, 10, 11, 0, 0] / 32;   % 与硬件 sharp_arith 完全一致
```

软件与硬件共用同一组数字，是闭环能对齐的前提。

#### 4.2.4 代码实践：把 `sharp_arith` 改成均值核

1. **实践目标**：仅修改 `sharp_arith.vhd` 一个文件，把锐化核替换为零填充的 3×3 均值核，并处理好 `sum` 范围陷阱。
2. **操作步骤**（在**副本**上改，勿破坏原工程；本讲只读不写源码，请你在自己的工作副本里操作）：
   - 复制 `sharp_arith.vhd` 为 `blur_arith.vhd`（或在原文件上试验），把第 34 行 `sum` 表达式换成上文的 `11*tap_m1 + 10*tap_00 + 11*tap_p1` 版本。
   - 把第 27 行注释改成 `[0;0;11;10;11;0;0]/32`，保持「注释 = 真值」。
   - 把第 30 行 `sum` 的范围从 `-512 to 511` 放宽到能容纳 \(32\times255=8160\) 的范围，例如 `0 to 8191`（待本地核对）。
   - 饱和逻辑保持不变（均值仍可能贴 0 或 255 边界）。
3. **需要观察的现象**：
   - 改完后 `sharp_slice` 的两级 arith 端口映射 [`sharp_slice.vhd:39-L49`](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_slice.vhd#L39-L49)、[`sharp_slice.vhd:60-L70`](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_slice.vhd#L60-L70) 完全不用动——7 个 `tap` 端口照旧接 7 个抽头，只是 arith 内部不再用其中 4 个。
   - 直流增益仍为 1：平坦区域（全 128）输出应仍为 128。
4. **预期结果**：综合通过、无范围溢出告警；行为上从「增强边缘」变成「模糊边缘」。
5. 仿真验证留到 4.3 节闭环里做；此处先确保代码改对、`sum` 范围足够。若你暂无仿真环境，可用 Octave 先验证系数的频率响应是低通（直流 0 dB、高频衰减），作为「待本地验证」的替代信心来源。

#### 4.2.5 小练习与答案

**练习 1**：为什么均值核写成 `[0,0,11,10,11,0,0]/32` 而不是 `[1,1,1]/3`？

> **答案**：`/3` 不是 2 的幂，硬件要生成真正的除法电路，违背本项目「移位代替除法」的设计哲学；`/32` 是右移 5 位，几乎不占资源。代价是系数不能完全等权（10 与 11），但直流增益仍精确为 1。

**练习 2**：把均值核改成 `[0,0,11,10,11,0,0]/32` 后，`sum` 变量范围为什么必须从 `-512..511` 放宽？

> **答案**：均值核全正、系数和为 32，最大累加值 \(32\times255=8160\)，远超 511；不放宽会在仿真中出现错误的环绕值。注意原锐化核因正负抵消才落在小范围内——这是改型时最易忽略的连带项。

**练习 3**：如果想要一个**更强的** 7 抽头高斯式平滑（中间权重大、两边权重小），需要动 `sharp_slice` 吗？

> **答案**：不需要。只要新核可分离、且系数能凑成 `/32`（或可接受的 2 的幂缩放），就只改 `sharp_arith` 的 `sum` 表达式与 `sum` 范围。`sharp_slice` 的抽头结构对「系数具体是多少」是无感的——它只负责把 7 个抽头摆好。

---

### 4.3 二次开发闭环验证

#### 4.3.1 概念说明

改完代码不等于改对。本项目的杀手锏是**自校验闭环**：只要 Octave 侧的系数和舍入与硬件完全一致，`sim_sharp_self-checking.vhd` 就能把硬件输出和软件参考逐像素比对，自动报 PASS/FAIL。这意味着二次开发的验证几乎**零额外成本**——你不用人眼看图，改完跑一次仿真就知道对不对。

闭环的三段式：

1. **算法侧（Octave）**：用新系数重新生成「输入 PPM」和「期望 PPM」。
2. **硬件侧（VHDL）**：把新系数写进 `sharp_arith`。
3. **验证侧（testbench）**：跑 `sim_sharp_self-checking.vhd`，它读新期望图，比对，报 `mismatch`。

验证侧脚本本身通常**不用改**——它只是一个「喂图、比对、计数」的通用框架。真正要同步的是「算法侧的系数」和「硬件侧的系数」必须一字不差。

#### 4.3.2 核心流程：改型后的闭环步骤

把 4.2 的均值核接入闭环，流程如下：

```
[Octave]                        [VHDL 硬件]                 [testbench]
f_hor = [0,0,11,10,             sharp_arith.vhd:            sim_sharp_self-checking.vhd
        11,0,0]/32              sum := 11*m1+10*00          (通常不改)
        +16 舍入 + 饱和          +11*p1, +16, /32, 饱和
      │                              │                          │
      ▼                              ▼                          ▼
 imfilter(垂直) ──► imfilter(水平) ──► 期望 PPM ──► 逐像素比对 ──► mismatch=0 ?
```

关键一致性要求（决定能否逐像素对齐）：

- **系数完全相同**：Octave 的 `f_hor` 与 `sharp_arith` 的 `sum` 表达式必须是同一组整数。
- **舍入方式相同**：硬件是 `(累加 + 16) / 32` 的整数截断（等价四舍五入）。Octave 侧若直接用 `imfilter` 配 `/32` 核，其默认输出经过 `write_ascii_ppm` 的 `%i` 格式化时会**截断**小数，可能与硬件的「四舍五入」差 1 个 LSB。为保险，建议在 Octave 侧**显式复刻**硬件公式：先按整数系数累加，再 `floor((sum+16)/32)`，最后饱和。这样两侧舍入机制严格一致。
- **边缘处理不同，必须跳过**：Octave `imfilter` 默认零填充边界，硬件靠 linemem/移位寄存器未填满，两者在边缘不可比。`sim_sharp_self-checking.vhd` 已经跳过左/右各 3 列、上方合计 6 行、并补偿 3 行垂直偏移（见 u5-l2）。**均值零填充核仍是 7 抽头结构，物理触达范围仍是 ±3，所以这套边缘跳过对均值核依然有效**——这是零填充路线的又一 bonus。

> 提示：这正是 4.2 强调「优先零填充、别真改抽头数」的验证层理由。如果你走「真 3 抽头」大改路线，物理触达变成 ±1，testbench 的 `x_pos > 2`、`y_pos > 5` 边界判定就过宽了（虽不会误报，但浪费了可比像素），垂直偏移补偿也要重算。零填充让验证脚本保持原样不动。

#### 4.3.3 源码精读

先看算法侧要改的地方。`sharp_generate_testbench_images.m` 的核与两次 `imfilter` [`sharp_generate_testbench_images.m:14-L18`](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sharp_generate_testbench_images.m#L14-L18)：

```matlab
f_hor = [1, 0, -9, 48, -9, 0, 1]/32;
f_ver = f_hor'; % transpose filter matrix
img_tmp = imfilter(img_in, f_ver);
img_out = imfilter(img_tmp,f_hor);
```

均值核只需把 `f_hor` 换成新系数（**示例代码**）：

```matlab
f_hor = [0, 0, 11, 10, 11, 0, 0] / 32;
f_ver = f_hor';
```

两次 `imfilter` 的「先垂直后水平」顺序与硬件 `sharp_slice` 的两级 arith 顺序一致，这是可分离滤波能逐像素对齐的结构保证。生成的两份 PPM（输入 + 期望）由 [`sharp_generate_testbench_images.m:20-L21`](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sharp_generate_testbench_images.m#L20-L21) 的 `write_ascii_ppm` 写出。

再看验证侧为什么不用改。`sim_sharp_self-checking.vhd` 的期望文件名是常量 [`sim_sharp_self-checking.vhd:23-L25`](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp_self-checking.vhd#L23-L25)：

```vhdl
constant stimuli_filename  : string  := "Lindau_Harbour_720p.ppm";
constant expected_filename : string  := "Lindau_Harbour_expected.ppm";
```

你只要保证 Octave 重新生成的期望图**覆盖同名文件**，testbench 不用动一行。比对核心在 `response_process` 里：先校验尺寸一致 [`sim_sharp_self-checking.vhd:216-L220`](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp_self-checking.vhd#L216-L220)，再在 `y_pos > 2` 后逐像素读期望 [`sim_sharp_self-checking.vhd:239-L247`](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp_self-checking.vhd#L239-L247)，跳过边缘后比对 [`sim_sharp_self-checking.vhd:248-L255`](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp_self-checking.vhd#L248-L255)，失配则累加 `mismatch` [`sim_sharp_self-checking.vhd:251`](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp_self-checking.vhd#L251)。仿真末尾据 `mismatch` 是否为 0 报告 [`sim_sharp_self-checking.vhd:164-L172`](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/Verification/sim_sharp_self-checking.vhd#L164-L172)。

整条链路里，**唯一需要人工保持同步的，是「Octave 的系数」与「VHDL 的系数」这一对数字**。testbench 只认期望图的内容，不关心它是什么滤波产生的。

#### 4.3.4 代码实践：跑通均值核的闭环

这是本讲的主实践，把 4.2 的硬件改动与 Octave 改动接成完整闭环。**待本地验证**（需要 Octave + VHDL 仿真器，如 GHDL/ModelSim/Quartus 仿真）。

1. **实践目标**：端到端验证均值核——Octave 产出期望图，硬件仿真逐像素匹配，`mismatch = 0`。
2. **操作步骤**：
   - **算法侧**：复制 `sharp_generate_testbench_images.m`，把 `f_hor` 改成 `[0,0,11,10,11,0,0]/32`（4.2.3 示例）。为对齐舍入，建议把 `imfilter` 后的结果手动按 `floor((sum+16)/32)` 重算再饱和（**示例代码**），或先按原脚本跑、若出现零星 ±1 失配再回来加这一步。运行后得到新的 `*_720p.ppm`（输入）与 `*_expected.ppm`（期望）。
   - **硬件侧**：按 4.2.4 改好 `sharp_arith.vhd`（`sum` 表达式 + `sum` 范围放宽）。确认 `sharp_slice`、`sharp.vhd` 不动。
   - **验证侧**：把两份 PPM 放到仿真工作目录，文件名与 testbench 常量一致。按 u5-l1 的做法，把 testbench 里可能的硬编码绝对路径改成你的相对路径。
   - **运行**：编译全部 `.vhd` 并启动 `sim_sharp_self-checking`。
3. **需要观察的现象**：
   - 仿真末尾打印 `Simulation completed, EVERYTHING OK`（`mismatch = 0`）。
   - 若出现少量失配，几乎都在舍入边界（±1 LSB），说明 Octave 侧需改用显式 `floor((sum+16)/32)` 复刻硬件舍入。
   - 若大面积失配，先查「两侧系数是否真的一致」「`sum` 范围是否溢出」。
   - 若失配集中在边缘，说明 testbench 的边缘跳过范围与你的核触达不匹配——零填充均值核不应出现这种情况。
4. **预期结果**：内部有效区逐像素匹配，`mismatch = 0`，报 `EVERYTHING OK`。同时输出图 `*_response.ppm` 视觉上是从锐化变模糊。
5. **故障排查口诀**：失配先看「位置」——边缘失配查边界跳过，全场失配查系数一致性，零星失排查舍入方式。
6. 若你当前没有 VHDL 仿真器，可降级为「Octave 单边验证」：用新系数生成期望图、再用同一系数在 Octave 里「软件实现硬件公式」自比，确认舍入逻辑自洽。完整闭环待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么改了系数，`sim_sharp_self-checking.vhd` 本身通常不需要改？

> **答案**：因为 testbench 是通用的「读期望图 → 逐像素比对 → 计 mismatch」框架，它只认期望图的内容，不关心图是怎么产生的。只要新期望图覆盖同名文件、系数的物理触达范围（7 抽头 → ±3）没变，跳过边界与垂直偏移补偿就仍然正确。

**练习 2**：仿真报了零星 `MISMATCH`，每个都差 1，位置随机分布。最可能的原因是什么？怎么修？

> **答案**：最可能是 Octave 侧 `imfilter` + `write_ascii_ppm` 的 `%i` 截断与硬件 `(sum+16)/32` 的四舍五入不一致，差 1 个 LSB。修法：在 Octave 侧显式复刻硬件公式——按整数系数累加后 `floor((sum+16)/32)` 再饱和，再写 PPM。

**练习 3**：如果你走「真 3 抽头」大改路线，testbench 的哪些常量/判断可能需要复核？

> **答案**：边缘跳过边界（`x_pos > 2`、`y_pos > 5`，`sim_sharp_self-checking.vhd:248`）对应 ±3 触达；真 3 抽头触达只有 ±1，边界虽不会误报但可放宽以增加可比像素。更重要的是垂直偏移补偿（`y_pos > 2`，第 239 行）依赖「中心抽头在第 3 行」，真 3 抽头中心在第 1 行，补偿值要重算，否则期望图整体错位、大面积失配。

---

## 5. 综合实践：从锐化到平滑的一次完整改型

把本讲三个模块串起来，完成一次端到端二次开发，并写一份「改型记录」。

**任务**：把视频处理通路从**锐化**切换为**3×3 均值平滑**，走「零填充中改」路线，并用自校验闭环证明正确。

**交付物**（建议在自己的工程副本里做，本讲不改原源码）：

1. **改型决策表**：参考 4.2.2 的对照表，写下你选零填充路线的理由（一句话）、新系数、`sum` 新范围。
2. **算法侧改动**：在 `sharp_generate_testbench_images.m` 副本里改 `f_hor`，生成新输入/期望 PPM。
3. **硬件侧改动**：在 `sharp_arith.vhd` 副本里改 `sum` 表达式（4.2.3 示例）与 `sum` 范围（4.2.3 陷阱），改注释。确认 `sharp_slice.vhd`、`sharp.vhd` 不动。
4. **闭环验证**：跑 `sim_sharp_self-checking.vhd`，截图或记录末尾报告（`EVERYTHING OK` 或失配数）。
5. **资源对比**：重新综合（若有 Quartus），记录 ALM / 存储 / DSP 占用，与原锐化基线（约 4% ALM、16% 存储、0 DSP）对比。零填充路线预期三者基本不变——你能解释为什么吗？（因为结构没动，只是 arith 里几个常数变了。）
6. **反思题**：如果把同样的零填充思路用到「边缘检测」（例如一维核 `[0,0,-1,2,-1,0,0]` 之类的高通），`sum` 范围、饱和逻辑、直流增益会有什么不同？（提示：高通会重新引入过冲与振铃，饱和逻辑会重新派上用场，直流增益需单独设计。）

**进阶挑战**（可选）：再走一次「真 3 抽头」大改路线，对比两种路线的资源占用与 testbench 改动量，亲手体会「零填充省事、大改省资源」的取舍。

## 6. 本讲小结

- **架构取舍**：本项目用三组「以约束换资源」的取舍定了基调——可分离滤波把乘法从 49 降到 10、定点 `/32` 把除法退化成右移（0 DSP）、行存储 RAM 把整行延迟从触发器换成循环缓冲。结果是「运算轻（4% ALM、0 DSP）、存储重（16% RAM）」。
- **改动分档**：改型影响面取决于新核的抽头数与系数友好度——只改 `sharp_arith` 一行（小改）、零填充借用 7 抽头结构（中改，推荐）、连 `sharp_slice` 抽头数一起改（大改）。
- **零填充技巧**：把 3 抽头均值塞成 `[0,0,11,10,11,0,0]/32`，复用整套 linemem 与移位寄存器，`sharp_slice`、`sharp_control`、testbench 边缘跳过全不动——这是最小侵入的改型法。
- **`sum` 范围陷阱**：均值核全正、系数和 32，累加值可达 8160，远超原 `-512..511`；改系数必须同步放宽 `sum` 范围声明。
- **闭环验证**：二次开发的验证几乎零成本——Octave 用同系数生成期望图，`sim_sharp_self-checking.vhd` 逐像素比对自动报 PASS/FAIL；唯一要人工同步的是「两侧系数」与「舍入方式」。
- **取舍贯穿全程**：从 U2 的算法选择到 U6 的时序收敛，每个决定（可分离、定点、行存储、零填充）都是「拿一种约束换一种节省」，理解这条主线，就能举一反三地改造这套滤波器。

## 7. 下一步学习建议

本讲是手册收官，项目本身已无更多模块。若想继续深入，建议三个方向：

1. **做一个不可分离核**：尝试 Sobel 边缘检测或 Laplacian。它们秩 > 1，两级 `sharp_arith` 架构失效——你需要设计真正的二维 MAC 阵列（49 个乘法或更聪明的并行结构）。这会逼你直面「可分离」假设破灭后的资源爆炸，是理解本项目架构选择价值最好的反面教材。
2. **把常数乘法换成 DSP 并研究时序**：把 `sharp_arith` 的移位加法树换成显式 `signed` 乘法调用 DSP 块，重跑 STA，对比关键路径与 Setup Slack 的变化。这能把 u6-l2 的时序分析与本讲的资源取舍连成一体。
3. **流水线化 `sharp_arith`**：u6-l2 指出关键路径在 arith 的乘加进位链。尝试把 `sum` 的累加拆成两级流水线（先乘、后加），观察 Slack 是否变正、代价是延迟增加几拍、`sharp_control` 的 `delay` 要相应怎么调。这是一个完整的「用延迟换频率」的小课题。

此外，作者 Marco Winzker 的 FPGA Vision Remote Lab（h-brs.de/fpga-vision-lab）提供了配套视频讲座与远程硬件访问，可以把你的改型真正下载到 Cyclone V 板上看实时视频效果——那是验证「算法 → RTL → 真实视频」全链路的最终一步。

恭喜走完整套手册。从一行 `fir1` 到一块跑在板上的锐化电路，你现在已经具备了独立改造这类视频 FIR 系统的能力。
