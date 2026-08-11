# Mandelbrot 集与数学 demo

> 本讲是 Unit 7（FPGA 数学运算）的收官篇，也是一次「综合实战」：我们把前面学过的**定点数（u7-l1）**、**有符号乘法器 `mul`（u7-l3）**、**帧缓冲 / linebuffer（u6-l4）**、**显示时序（u6-l1）**、**BRAM（u5-l3）** 与**跨时钟域同步器 `xd`（u5-l2）** 全部串起来，在 FPGA 上实时渲染一张数学上著名的分形图像——Mandelbrot（曼德博）集。

---

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 Mandelbrot 集的数学定义：复数迭代 \(z_{n+1}=z_n^2+c\) 与「逃逸时间」判别。
- 看懂 `mandelbrot.sv` 如何用**一个共享的有符号定点乘法器**、通过 6 状态机串行完成一次复数平方迭代的全部运算。
- 解释为什么迭代核心需要计算 \(x^2\)、\(y^2\)、\(xy\) 三个乘积，以及它们如何复用于「求新 z」与「判逃逸」。
- 理解 `render_mandel.sv` 如何逐像素扫描帧缓冲、用 4 个并行实例做超采样（supersampling）抗锯齿、并把逃逸迭代次数映射为颜色。
- 学会通过修改 `X_START / Y_START / STEP / FP_WIDTH / ITER_MAX` 来**放大（zoom）** 到 Mandelbrot 集的某个细节区域。

---

## 2. 前置知识

本讲默认你已经读过以下讲义（若某个概念陌生，先回看对应讲义）：

| 概念 | 来自哪一讲 | 在本讲的作用 |
|---|---|---|
| 定点数 Q 格式（WIDTH / FBITS / IBITS） | u7-l1 | Mandelbrot 用 Q4.21 定点表示复数坐标 |
| 有符号定点乘法器 `mul`（高斯舍入、start/busy/valid 握手） | u7-l3 | 迭代核心**唯一**的乘法单元，被反复复用 |
| `bram_sdp` 简单双口 Block RAM | u5-l3 | 存放渲染好的帧缓冲 |
| `display_480p` 显示时序 | u6-l1 | 产生扫描坐标 sx/sy、同步信号 |
| `linebuffer`、帧缓冲缩放显示 | u6-l4 | 把 320×180 的小帧缓冲放大显示 |
| 跨时钟域同步器 `xd` | u5-l2 | 把像素时钟域的 frame/line 脉冲传到系统时钟域 |
| 按键消抖 `debounce` | u5-l5 | 处理开发板按键（移动 / 缩放控制） |

还需要一点**复数**常识：一个复数 \(z = x + yi\)（其中 \(i^2=-1\)），\(x\) 是实部，\(y\) 是虚部。复数也可以用二维平面上的点 \((x,y)\) 表示。

> 一个常见疑问：为什么要用**定点**而不用浮点？因为 FPGA 的浮点运算要消耗大量 LUT/DSP，而 Mandelbrot 的数值范围是已知的（坐标在 \([-2,2]\) 附近、\(|z|^2\) 在 \(0\sim 5\)），用定点 Q4.21 完全够用、且能映射到 DSP48 硬核高速完成乘法。这正是 u7-l1 讲过的「FPGA 偏爱定点」的典型应用。

---

## 3. 本讲源码地图

本讲聚焦 `ThreePart/projf-explore/demos/mandelbrot/` 目录，这是 projf 一个完整的、可上板可仿真的数学 demo。关键文件如下：

| 文件 | 作用 |
|---|---|
| [mandelbrot.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/demos/mandelbrot/mandelbrot.sv) | **迭代核心**：给定一个复数坐标 \(c\)，迭代 \(z_{n+1}=z_n^2+c\)，输出逃逸时所用的迭代次数。本讲的主角。 |
| [render_mandel.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/demos/mandelbrot/render_mandel.sv) | **渲染器**：逐像素扫描帧缓冲区域，例化多个 `mandelbrot` 做超采样，把迭代次数映射为颜色索引，逐像素写帧缓冲。 |
| [xc7-vga/top_mandel.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/demos/mandelbrot/xc7-vga/top_mandel.sv) | **顶层**（VGA 输出版）：整合时钟、显示时序、帧缓冲、按键控制、缩放逻辑、配色方案。 |
| [verilator-sdl/top_mandel.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/demos/mandelbrot/verilator-sdl/top_mandel.sv) | **顶层**（Verilator/SDL 仿真版）：与 VGA 版逻辑几乎相同，但把视频输出送到 SDL 窗口，便于在没有开发板时也能「看到」渲染结果。 |
| [README.md](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/demos/mandelbrot/README.md) | demo 说明：默认参数、缩放限制、按键控制、各板卡构建方法。 |
| [lib/maths/mul.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/mul.sv) | 迭代核心依赖的**有符号定点乘法器**（u7-l3 已精读）。 |

目录里还有 `xc7-dvi/`（Nexys Video 的 DVI 输出版）、`verilator-sdl/Makefile` 与 `main_mandel.cpp`（SDL 主程序）、以及各板卡的约束 `.xdc` 与 `create_project.tcl`。本讲只读最核心的三层：迭代核心 → 渲染器 → 顶层。

---

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：

1. **复数定点运算与 Mandelbrot 数学**——把数学公式翻译成定点硬件能算的三个实数乘法。
2. **`mandelbrot` 迭代核心**——一个共享 `mul`、6 状态机，串行完成一次迭代。
3. **`render_mandel` 渲染器**——扫描像素、超采样、上色、对接帧缓冲。

---

### 4.1 复数定点运算与 Mandelbrot 数学

#### 4.1.1 概念说明

Mandelbrot 集是复数平面上的一个点集。对平面上每一个点 \(c\)，我们都做下面这件事：

- 令 \(z_0 = 0\)。
- 反复迭代 \(z_{n+1} = z_n^2 + c\)。
- 看 \(z_n\) 的模长 \(|z_n|\) 是否会「跑飞」（趋向无穷大）。

如果无论迭代多少次 \(|z_n|\) 都保持有界，就说 \(c\) **属于** Mandelbrot 集（通常染成黑色）；如果迭代若干次后 \(|z_n|\) 超过某个阈值，就说 \(c\) **不属于**该集，并且**它逃逸时所用的迭代次数 \(n\)** 决定了我们给它染什么颜色——这就是「**逃逸时间染色**（escape-time coloring）」。

> 关键直觉：Mandelbrot 集「外面」的点逃逸得快（迭代次数少），「靠近边界」的点逃逸得慢（迭代次数多），所以颜色随迭代次数变化就能勾勒出那些令人着迷的分形边界细节。

#### 4.1.2 核心流程

把复数迭代展开成实数运算。设 \(z = x + yi\)、\(c = c_x + c_y i\)（即输入坐标的实部与虚部）：

\[
z^2 = (x+yi)^2 = x^2 - y^2 + 2xy\,i
\]

所以一次迭代 \(z_{n+1} = z_n^2 + c\) 等价于两个**实数**递推：

\[
x_{n+1} = x_n^2 - y_n^2 + c_x
\]

\[
y_{n+1} = 2\,x_n y_n + c_y
\]

逃逸判据：若 \(|z_n|^2 = x_n^2 + y_n^2\) 超过 4（即 \(|z_n|>2\)），则后续必然发散。于是：

\[
\text{逃逸条件}: \quad x_n^2 + y_n^2 > 4
\]

注意一个重要的「复用」：求新 \(z\) 需要 \(x^2\)、\(y^2\)、\(xy\) 三个乘积；判逃逸需要 \(x^2+y^2\)。也就是说**整个迭代只需要三个实数乘法**：\(x^2\)、\(y^2\)、\(xy\)。判逃逸的 \(x^2+y^2\) 正好是把刚算出的 \(x^2\) 和 \(y^2\) 加起来——免费得到。这是 Mandelbrot 硬件实现里最关键的观察，也是 `mandelbrot.sv` 用**一个乘法器**就能搞定的前提。

定点表示方面，demo 默认用 **Q4.21** 格式（参数 `FP_WIDTH=25`、`FP_INT=4`，故小数位 `FBITS = 25-4 = 21`）：

- 4 个整数位（含 1 位符号），可表示 \([-8, +8)\)，足够覆盖 Mandelbrot 集所在的 \([-2,2]\) 区域与逃逸半径。
- 21 个小数位，精度 \(2^{-21}\approx 4.8\times10^{-7}\)。README 指出默认可缩放约 15 次，最小步长到 \(1/2^{21}\)。

#### 4.1.3 源码精读

`mandelbrot.sv` 的端口与参数把上面的数学直接落成了硬件约定。注意 `re, im` 就是输入坐标 \(c\) 的实部与虚部，类型是 `signed` 定点：

[mandelbrot.sv:10-23](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/demos/mandelbrot/mandelbrot.sv#L10-L23) —— 参数 `FP_WIDTH=25 / FP_INT=4 / ITER_MAX=255`，端口 `re, im` 为有符号定点输入，输出迭代次数 `iter` 与握手信号 `calculating / done`。

其中 `ITERW = $clog2(ITER_MAX+1)` 自动算出迭代次数寄存器的位宽（`ITER_MAX=255` 时为 8 位）。`mul` 例化时把 `FBITS` 设为 `FP_WIDTH - FP_INT`（即 21），与 u7-l3 讲过的 `mul` 接口对齐：

[mandelbrot.sv:31-46](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/demos/mandelbrot/mandelbrot.sv#L31-L46) —— 例化唯一的乘法器 `mul_inst`，所有乘法（\(xy\)、\(x^2\)、\(y^2\)）都复用它，靠 `mul_start/mul_done` 握手轮流启动。

#### 4.1.4 代码实践（源码阅读型）

**目标**：确认「三个乘积就够了」这一论断。

1. 打开 `mandelbrot.sv`，在源码里圈出与 \(x^2\)、\(y^2\)、\(xy\)、\(x^2+y^2\) 对应的变量（提示：`x2`、`y2`、`mul_val_p`/`xy2`）。
2. 写一张表，列出「数学量 → Verilog 变量 → 在哪个状态被算出」三列。
3. 验证：判逃逸用的 `xy2` 是否恰好等于某次 `x2 + y2`？它有没有为逃逸判断额外消耗一次乘法？

**预期结果**：你会发现逃逸判据 `xy2 = x2 + mul_val`（见后文 STEP4）复用了 \(x^2\) 和 \(y^2\)，**没有**为判逃逸增加乘法——硬件上一次迭代恰好 3 次乘法。

#### 4.1.5 小练习与答案

**练习 1**：若把 `FP_INT` 改成 3（即 Q3.22），会出什么问题？

**答案**：整数位只剩 3 位（含 1 位符号），可表示范围缩小到 \([-4,+4)\)。逃逸判据比较的是整数部分是否 \(>4\)，而 \(x^2+y^2\) 可能达到约 5，整数部分会变成 `101`（带符号解释为 -3）从而误判。所以 `FP_INT=4` 是为了让整数部分能正确容纳 0~7 的逃逸值，不能随便减小。

**练习 2**：为什么用定点 Q4.21 而不是浮点？

**答案**：FPGA 浮点运算耗资源、慢；而 Mandelbrot 的数值范围已知且小，定点足够、且 `signed` 乘法能直接映射到 DSP48 硬核（见 u7-l3）。定点还让「比较整数部分判逃逸」这种省硬件的技巧成为可能。

---

### 4.2 mandelbrot 迭代核心（状态机 + 共享乘法器）

#### 4.2.1 概念说明

`mandelbrot.sv` 回答一个问题：**给定 \(c\)，迭代多少次后逃逸？** 它的难点在于 FPGA 不像 CPU 那样一条乘法指令就能算完——u7-l3 讲过，定点 `mul` 是个**多周期**模块（start 握手启动、约 4 拍后给出 `done` 与乘积）。如果一次迭代需要 3 个乘积，最朴素的硬件做法是例化 3 个乘法器并行算；而这个 demo 选择**只例化 1 个乘法器**，用状态机把 3 个乘法**串行排程**——省面积（少用 2 个 DSP），代价是单次迭代多花十几拍。这是一次典型的「面积换时间」取舍。

#### 4.2.2 核心流程

一次完整迭代（未逃逸、`iter < ITER_MAX` 时）的状态流转：

```
STEP1: 判逃逸（用上一轮的 x²+y²）与迭代上限
       ├─ 若继续: 启动 mul(x, y)        ──→ STEP2
       └─ 若逃逸/达上限: 回 IDLE，done=1

STEP2: 等 mul_done → 记下 xy=mul_val，算 new_x = x²-y²+c_x ──→ STEP2A
STEP2A: 更新 x<=new_x, y<=2*xy+c_y；启动 mul(new_x, new_x) ──→ STEP3
STEP3: 等 mul_done → 记下 new_x²；启动 mul(new_y, new_y)   ──→ STEP4
STEP4: 等 mul_done → 记下 new_y²；xy2 = new_x²+new_y²；
        iter++ ──→ STEP1（下一轮判逃逸就用这个 xy2）
```

> 最巧妙的一处在 STEP2A：它用**非阻塞赋值** `x <= xt; y <= 2*mul_val_p + y0;` 更新成「新的 z」。由于非阻塞在下一拍才生效，紧接的 STEP3 里 `mul_a=y` 读到的已是**新 y**，于是 STEP3/STEP4 算出的 `y2`、`xy2` 自然就是「新 z 的平方和」，正好供下一轮 STEP1 判逃逸。一次非阻塞赋值的时间差被用得淋漓尽致。

判逃逸的具体写法值得专门一看：它只比较 `xy2` 的**整数位**：

[mandelbrot.sv:56-67](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/demos/mandelbrot/mandelbrot.sv#L56-L67) —— STEP1：`xy2[FP_WIDTH-1-:FP_INT] <= 4` 取出整数位（`FP_WIDTH=25,FP_INT=4` 即 `xy2[24:21]`）按无符号比较，连同 `iter < ITER_MAX` 决定继续还是结束。

这是一个**故意简化**的逃逸测试：标准判据是 \(|z|^2>4\)（即 \(|z|>2\)）就逃逸；而这里只看整数部分，相当于「整数部分 \(\le 4\)」时继续（即 \(|z|^2\) 落在 \([0,5)\) 内），「整数部分 \(\ge 5\)」才判逃逸。略大的逃逸半径对**绘制效果**几乎无影响（边界稍多算几步迭代），却省去了对 21 位小数的比较，是合理的工程取舍。

`IDLE` 态负责把输入坐标寄存为 \(c\) 并把 \(z\) 归零：

[mandelbrot.sv:103-115](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/demos/mandelbrot/mandelbrot.sv#L103-L115) —— 收到 `start`：`x0<=re; y0<=im;`（保存 \(c\)），`x,y,x2,y2,xy2,iter` 全部清零（即 \(z_0=0\)）。

#### 4.2.3 源码精读

状态机定义与中间变量：

[mandelbrot.sv:52-52](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/demos/mandelbrot/mandelbrot.sv#L52) —— 六个状态 `enum {IDLE, STEP1, STEP2, STEP2A, STEP3, STEP4}`。

STEP2/STEP2A（算 \(xy\)、合成新 z、启动 \(x^2\)）：

[mandelbrot.sv:68-83](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/demos/mandelbrot/mandelbrot.sv#L68-L83) —— STEP2 等乘积 `mul_val` 存为 `mul_val_p`（即 \(xy\)），并提前用旧 `x2,y2` 算出 `xt = x2 - y2 + x0`（即 \(x_{n+1}\)）；STEP2A 更新 `x,y` 并启动 `mul(xt, xt)` 求 \(x_{n+1}^2\)。

STEP3/STEP4（算 \(x^2\)、\(y^2\)，更新 `xy2`，迭代计数加一）：

[mandelbrot.sv:84-102](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/demos/mandelbrot/mandelbrot.sv#L84-L102) —— STEP3 收 `x2`，启动 `mul(y,y)`；STEP4 收 `y2`，令 `xy2 = x2 + mul_val`，`iter <= iter + 1`，回到 STEP1。

被复用的 `mul` 本身（u7-l3 已精读，这里只看握手节奏）：它在 IDLE 收 `start` 寄存输入，经 CALC→TRUNC→ROUND 三拍完成「乘→截位→高斯舍入→溢出检测」，在 ROUND 拍拉高一拍 `done`：

[mul.sv:41-75](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/maths/mul.sv#L41-L75) —— `mul` 的状态机：CALC 算全宽乘积 `prod <= a1*b1`，TRUNC 截取并准备舍入位，ROUND 做向偶舍入（round half to even）与溢出判断后回 IDLE。

于是单次 Mandelbrot 迭代的时钟开销约为：

\[
T_{\text{iter}} \approx 1\,(\text{STEP1}) + 4\,(\text{mul: }xy) + 1\,(\text{STEP2A}) + 4\,(\text{mul: }x^2) + 4\,(\text{mul: }y^2) \approx 14\,\text{拍}
\]

最坏情况 `ITER_MAX=255` 全跑满，单像素 \(\approx 14\times255\approx 3570\) 拍；320×180 像素全屏渲染就是上亿拍——这也是为什么 README 强调「渲染进行中按键无效」，一次全屏重绘要花一两秒。

#### 4.2.4 代码实践（源码阅读 + 仿真型）

**目标**：跟踪 `mandelbrot.sv` 对一个具体坐标的迭代过程，标出三个乘法与逃逸/上限判断。

1. 选一个**显然在集内**的点 \(c = -0.5 + 0i\)（实部 -0.5、虚部 0），和一个**显然在集外**的点 \(c = 1 + 0i\)。
2. 阅读状态机，对每个点在纸上画出 `state → mul_a,mul_b → 结果` 的序列，记录 `iter` 何时停止增长。
3. （可选）写一个最小 testbench：`start=1` 一拍后拉低，给 `re/im` 喂上面两个坐标，时钟跑若干拍，在 `done` 拉高时打印 `iter`。
4. 对比 \(c=1\)：手算 \(z_1=0^2+1=1\)、\(z_2=1^2+1=2\)、\(z_3=2^2+1=5\)，此时 \(|z|^2\approx25\)，整数部分 \(\ge5\)，应在第 3 次左右逃逸。

**预期结果**：\(c=-0.5\) 应一直迭代到 `ITER_MAX=255`（不逃逸，染黑）；\(c=1\) 应在第 3 次迭代左右逃逸，`iter` 远小于 255。若手上没有仿真器，明确写「待本地验证」并记录你预测的 `iter` 值。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `mul` 例化数从 1 个增加到 3 个（并行算 \(x^2,y^2,xy\)），单次迭代能快多少？有什么代价？

**答案**：理论上可把「串行 3 次乘法约 12 拍」压成「并行 1 次约 4 拍」，单次迭代从 ~14 拍降到 ~6 拍，速度约 2~3 倍。代价是多用 2 个 DSP48 单元与相关寄存器，且状态机要改写以处理同时返回的 3 个 `done`。这正是「面积换时间」的另一端。

**练习 2**：STEP2A 里若把 `x <= xt; y <= 2*mul_val_p + y0;` 改成阻塞赋值 `=`，会发生什么？

**答案**：阻塞赋值会在 STEP2A **当拍**立即更新 `x,y`，会破坏后续状态依赖「STEP2A 末尾寄存器才更新」的隐含时序约定；`2*mul_val_p` 这类组合值用阻塞写进寄存器也会改变与 `mul_a/mul_b` 的相对节拍，可能让 STEP3 读到的 `y` 错位。demo 全程用非阻塞 `<=` 保持时序清晰，不应混用阻塞赋值。

---

### 4.3 render_mandel 渲染器：扫描、超采样与上色

#### 4.3.1 概念说明

`mandelbrot.sv` 一次只算**一个像素**。`render_mandel.sv` 的工作是：把一块矩形区域（默认 320×180）的每一个像素坐标 \((x,y)\) 映射到复数平面上的 \(c\)，调用 `mandelbrot` 算出迭代次数，再映射成一个 8 位颜色索引 `cidx`，逐像素写进帧缓冲。它还做了一件提升画质的事——**超采样（supersampling）**：对每个像素在亚像素位置取 4 个样本，求迭代次数的平均，起到抗锯齿作用。

#### 4.3.2 核心流程

渲染器的状态机遍历帧缓冲的每个像素：

```
IDLE  ──start──→ INIT
INIT  : 启动 4 个 mandelbrot 实例（calc_start=1）──→ CALC
CALC  : 等 4 个实例全部 done (calc_done)；
        iter = SUPERSAMPLE ? (iter_00+01+10+11)/4 : iter_00 ──→ DRAW
DRAW  : 把 iter 映射为颜色索引 cidx；drawing=1 一拍 ──→ NEXT
NEXT  : 像素坐标前进一步：
        ├─ 行内: x++, fx += step ──→ INIT
        ├─ 行末换行: x=0, y++, fx=x_start, fy+=step ──→ INIT
        └─ 最后一像素: ──→ DONE ──→ IDLE
```

**坐标映射**：渲染器维护「函数坐标」`fx, fy`（定点复数平面坐标）。`fx` 从输入 `x_start`（左边界）出发，每像素加 `step`；`fy` 从 `y_start`（上边界）出发，每行加 `step`。于是「屏幕像素 \((x,y)\) ↔ 复数 \(c=(fx, fy)\)」一一对应，`step` 就是「每个像素代表多大的复数平面间距」——**改 `step` 就是缩放**。

**超采样**：当 `SUPERSAMPLE=1` 时，4 个实例分别采样像素四角的亚像素位置：

[render_mandel.sv:54-59](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/demos/mandelbrot/render_mandel.sv#L54-L59) —— `fx_left = fx - step/4`、`fx_right = fx + step/4`，同理 `fy_top/fy_bottom`（`step >>> 2` 即除 4）。4 个实例采样左/右 × 上/下四个角点。

**上色**：迭代次数到位宽 `CIDXW=8` 的颜色索引的映射用「取高位」实现：

[render_mandel.sv:49-51](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/demos/mandelbrot/render_mandel.sv#L49-L51) —— `colr = iter[ITERW-1-:CIDXW]` 取 `iter` 的高 `CIDXW` 位。这是为什么 README 推荐 `ITER_MAX` 取 \(2^n-1\)（如 511）——此时高位刚好铺满 8 位颜色范围，色阶最丰富。

随后 DRAW 态做两件特殊情况处理：

[render_mandel.sv:80-85](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/demos/mandelbrot/render_mandel.sv#L80-L85) —— 若 `iter==ITER_MAX`（未逃逸，属于集合）染 `0x00`（黑）；否则 `cidx = (colr==0) ? 1 : colr`，把「很快逃逸」的 0 强制改成 1，避免与「集内黑色」混淆。

#### 4.3.3 源码精读

渲染器端口揭示它如何与顶层/帧缓冲对接：

[render_mandel.sv:8-30](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/demos/mandelbrot/render_mandel.sv#L8-L30) —— 输入起点坐标 `x_start/y_start` 与步长 `step`（均为有符号定点），输出绘制坐标 `x,y`、颜色索引 `cidx`、以及 `drawing/busy/done` 握手。`SUPERSAMPLE` 参数决定是否启用 4 倍采样。

NEXT 态负责像素扫描的「行进」：

[render_mandel.sv:86-104](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/demos/mandelbrot/render_mandel.sv#L86-L104) —— 行内 `x++` 且 `fx += step`；行末 `x=0, y++, fx=x_start, fy+=step`；到达 `(FB_WIDTH-1, FB_HEIGHT-1)` 进入 DONE。

「4 个实例全部完成」的会合逻辑（barrier）：

[render_mandel.sv:131-145](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/demos/mandelbrot/render_mandel.sv#L131-L145) —— `calc_done = calc_done_00r && ... && calc_done_11r`，把 4 路异步到达的 `done` 各自「锁存」一次（一旦某实例完成就记下，直到本轮 CALC 结束才清零），实现「等最慢的一个」。

4 个 `mandelbrot` 实例的例化（仅示其一，其余结构相同，只是坐标喂入 `fx_left/fx_right`、`fy_top/fy_bottom` 的不同组合）：

[render_mandel.sv:148-217](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/demos/mandelbrot/render_mandel.sv#L148-L217) —— 4 个实例 `mandelbrot_inst_00/01/10/11` 分别对应左上、左下、右下、右上 4 个亚像素样本，共用 `calc_start` 启动，各自输出 `iter_00..11`。

颜色最终在顶层 `top_mandel.sv` 里由 `cidx`（8 位索引）经两套配色方案之一映射成 RGB（这里看 VGA 版）：

[top_mandel.sv:347-368](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/demos/mandelbrot/xc7-vga/top_mandel.sv#L347-L368) —— `COLR_SCHEME=1`（蓝绿）：`R=cidx/2, G=cidx, B=cidx`，集内（0）为黑、向外渐变到青白；`COLR_SCHEME=0`（蓝-紫-金）分段染色。注意输出再截高 4 位给 4 位 DAC 的 VGA。

顶层里「缩放/移动」按键状态机值得一看——它直接对应「如何放大到某区域」：

[top_mandel.sv:180-198](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/demos/mandelbrot/xc7-vga/top_mandel.sv#L180-L198) —— ZOOM 态：放大时 `step_p <= step/2` 且按位移量平移 `x_start/y_start` 把目标点拉向视野中央；缩小则 `step_p <= 2*step`。这正是综合实践里「手动改坐标」的硬件等价物。

#### 4.3.4 代码实践（参数修改型）

**目标**：动手改参数观察渲染变化（无需上板，可纯源码阅读 + Verilator 仿真）。

1. **改迭代上限**：在 `top_mandel.sv` 把 `ITER_MAX` 从 255 改成 511（注意 `2^n-1` 推荐）。预测：边界细节更丰富、颜色梯度更细，但渲染时间约翻倍。
2. **改配色**：把 `COLR_SCHEME` 从 1 改成 0，观察「蓝-紫-金」与「蓝绿」两套方案的差异。
3. **关掉超采样**：把 `SUPERSAMPLE` 改成 0。预测：渲染速度提升约 4 倍（只算 1 个样本），但边界锯齿明显。
4. **改帧缓冲大小**：把 `FB_WIDTH/FB_HEIGHT` 改一改，观察 `STEP` 是否需要同步调整以保持视野范围（视野宽度 = `FB_WIDTH * step`）。

**预期结果**：前三项的预期已在各步给出。若手头有 Verilator+SDL 环境，可进入 `verilator-sdl/` 目录 `make` 后运行 `./obj_dir/mandelbrot` 实际看到窗口中的分形图像（README 有说明）。否则写「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`colr = iter[ITERW-1-:CIDXW]` 为什么用「取高位」而不是「取低位」或直接相等？

**答案**：迭代次数 `iter` 的位宽 `ITERW` 随 `ITER_MAX` 变（如 `ITER_MAX=511` 时 `ITERW=9`），而颜色索引固定 8 位。取高位等价于「把 0~ITER_MAX 的范围线性压缩到 0~255」，让低迭代次数（远离集合，逃逸快）映射到低色值、高迭代次数（靠近边界）映射到高色值，过渡均匀。取低位则会丢失量级信息、颜色杂乱。

**练习 2**：为什么 DRAW 里要把 `colr==0` 改写成 1？

**答案**：`colr==0` 表示「非常快就逃逸」的点（迭代次数落到颜色范围最低端）。但集内点也被染成 `0x00`（黑），若允许 `colr` 为 0，那么「最快逃逸」与「集内」会撞色。强制改成 1 保证集内（0）独占黑色，集外点至少是色阶 1，便于区分。

---

## 5. 综合实践：放大到「海马谷」

把本讲知识串起来，做一个完整的「选定区域 → 推导参数 → 预测精度」的练习。著名分形细节「海马谷（Seahorse Valley）」位于实部约 \(-0.75\)、虚部约 \(0.1\) 附近。

**任务**：

1. **选定目标区域**：设要显示的复数平面窗口为 \(x \in [-0.85, -0.65]\)、\(y \in [0.05, 0.15]\)。窗口宽 0.2、高 0.1。
2. **求 `STEP`**：窗口横向跨 320 像素，故 `step = 0.2 / 320 = 1/1600`。换算成 Q4.21 定点整数 = \(2^{21}/1600 \approx 1310\)（即约 `25'd1310`）。验证纵向：`180 × 1/1600 = 0.1125`，与目标高 0.1 接近（若要严格匹配可微调 `FB_HEIGHT` 或窗口）。
3. **求 `X_START / Y_START`**：左边界 \(-0.85\)、上边界 \(0.15\)。把这两个十进制定点数换算成 25 位 Q4.21 二进制补码（提示：乘 \(2^{21}\) 取整再转二进制）。可写一小段 Python：`to_fp = lambda v: int(round(v * 2**21)) & (2**25-1)`，再用 `bin()` 查看。
4. **判断精度是否够**：当前 `STEP ≈ 1/1600` 远大于精度下限 \(1/2^{21}\)，所以 `FP_WIDTH=25` 完全够用。如果你要继续放大到 `step ≈ 1/2^{20}`，README 指出默认 25 位约能再缩放 15 次；超过后需要增大 `FP_WIDTH`（同时调整 `X_START/Y_START/STEP` 的位宽）。
5. **修改并验证**：在 `top_mandel.sv`（任选 VGA 或 Verilator 版）顶部修改 `X_START / Y_START / STEP` 三个 `localparam`，重新综合或 `make` 运行，观察是否放大到了目标区域。
6. **交叉验证（强烈推荐）**：用 Python 写一个 30 行的参考模型（`z=0; for n in range(ITER_MAX): z = z*z + c; if abs(z)>2: break`），对你选的区域逐像素算迭代次数并染色，与 Verilator 输出对比，确认硬件渲染正确。

**预期结果**：改参后应能看到放大的海马谷细节；Python 参考模型与硬件渲染的形状应一致（颜色映射方案不同会导致色阶不同，但**分形结构**应吻合）。若没有综合/仿真环境，至少完成第 1~4 步的纸面推导，并标注「待本地验证」。

> 这个练习一次性用上了本讲的全部要点：定点 Q4.21 表示（4.1）、缩放就是改 `step`（4.3）、以及「定点精度决定最大缩放倍数」这一工程约束。

---

## 6. 本讲小结

- Mandelbrot 集由复数迭代 \(z_{n+1}=z_n^2+c\) 定义，**逃逸时间**（迭代多少次后 \(|z|>2\)）决定染色，永不逃逸的点属于集合（染黑）。
- 复数迭代可展开成三个**实数乘法** \(x^2\)、\(y^2\)、\(xy\)，且判逃逸的 \(x^2+y^2\) 是免费复用——这是一切硬件简化的前提。
- `mandelbrot.sv` 用**一个共享 `mul`** + 6 状态机（IDLE/STEP1/2/2A/3/4）串行完成一次迭代，单次约 14 拍；非阻塞赋值的时间差被巧妙用于「更新 z 的同时算出下一轮的平方和」。
- 逃逸判据 `xy2[整数位] <= 4` 是对标准 \(|z|^2>4\) 的**整数位近似**（实际逃逸半径略大），省去了 21 位小数比较，是合理的渲染取舍。
- `render_mandel.sv` 逐像素扫描、用 4 个 `mandelbrot` 实例做**超采样抗锯齿**，用「取 iter 高位」把迭代次数线性映射成 8 位颜色索引。
- **缩放就是改 `step`**（`X_START/Y_START` 定视野左上角）；定点 Q4.21 的 21 位小数决定了约 15 次的最大放大倍数，放大到极限需增大 `FP_WIDTH`。

---

## 7. 下一步学习建议

- **想看更完整的顶层集成？** 精读 [xc7-vga/top_mandel.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/demos/mandelbrot/xc7-vga/top_mandel.sv) 中按键状态机（HORIZONTAL/VERTICAL/ZOOM 三态）与帧缓冲读写地址的生成，它把本讲的渲染器与显示时序、linebuffer、BRAM 完整对接。
- **想动手仿真？** 进入 `verilator-sdl/`，按 README 安装 Verilator 与 SDL2 后 `make && ./obj_dir/mandelbrot`，在窗口里实时看到 Mandelbrot 集并用方向键移动/缩放（等价于 u6 系列的「上板」体验）。
- **想深入数学库？** 回到 `lib/maths/`，对比 `sqrt.sv`（u7-l3）与 `div.sv`（u7-l2）的迭代状态机风格，它们与本讲的 `mandelbrot.sv` 共享同一种「多周期握手 + enum 状态机」范式。
- **下一步讲义？** 本讲是 Unit 7 的最后一篇，也是整本手册数学主线的高潮。若你按大纲顺序学习，可进入 Unit 8（第三方 IP 资源与综合实践），其中 u8-l4 demos 综合分析会再次回到 projf 的 demos（生命游戏、星空等），从「复用 lib 库搭建系统」的角度做一次综合复盘。
