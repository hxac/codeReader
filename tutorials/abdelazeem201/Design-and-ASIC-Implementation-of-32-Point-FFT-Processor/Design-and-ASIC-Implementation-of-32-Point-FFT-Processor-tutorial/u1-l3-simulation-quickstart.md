# 仿真快速上手：用 testbench 跑通 FFT

## 1. 本讲目标

上一讲（u1-l2）我们画出了仓库的文件地图，知道了 `RTL/` 是设计源、`SIM/` 是仿真目录。本讲要解决的问题是："**这些源码怎么动起来？我怎么确认这颗 FFT 算得对？**"

答案就藏在 `SIM/FFT_tb.v` 这个 testbench 里。它是整个项目唯一可执行的"考试卷"：自动喂入 5 组测试数据、把硬件输出和黄金答案逐点比对、算出一个 SNR（信噪比）分数，并打印 PASS / FAIL。

学完本讲，你应该能够：

- 看懂 testbench 里的时钟生成、复位脉冲、`in_valid` 激励时序这条"考试流程"。
- 说清楚 5 组 `IN_real/IN_imag_pattern0X.txt` 输入和 `OUT_real/OUT_imag_16_pattern0X.txt` 黄金输出是怎么被读进来的。
- 写出 SNR 的能量计算公式，并解释"SNR ≥ 40 dB 或噪声为 0"这条通过判定的来历。
- 独立把 RTL 和 testbench 加载进仿真器、跑出 5 组数据集的 SNR 与平均延迟。

## 2. 前置知识

### 2.1 testbench 与 DUT 是什么

在数字电路仿真里有两个角色：

- **DUT（Design Under Test，被测设计）**：你要验证的电路，本项目中就是 `RTL/FFT.v` 描述的 32 点 FFT 处理器，在 testbench 里被例化为 `FFT_CORE`。
- **testbench（测试平台）**：一段"陪练"代码，它自己不是真实硬件，而是负责产生时钟、复位、输入激励，并检查 DUT 的输出对不对。

可以把 testbench 想象成一台自动阅卷机：它一边往 DUT 里"喂"题（输入样本），一边对照标准答案（黄金数据）打分（SNR）。本讲的 `TESTBED` 模块就是这台阅卷机。

### 2.2 仿真在 ASIC 流程里的位置

回顾 u1-l2 的主线："写 RTL → **仿真** → 综合 → 布局布线 → 出版图"。仿真排在综合之前，目的是**在 RTL 阶段就把功能 bug 暴露出来**——这比流片后再发现便宜几个数量级。本讲的 testbench 还预留了 `RTL` / `GATE` 两套模式，既能验 RTL，也能在综合后带上时序信息（SDF）验门级网表，这一点我们在 4.4 节再展开。

### 2.3 用 SNR 给"算得准不准"打分的直觉

FFT 是定点硬件，输出不可能和浮点参考值一模一样，总会有量化误差。所以不能简单问"等不等"，而要问"差多少"。SNR（Signal-to-Noise Ratio）就是这样一个指标：

\[ \text{SNR}_{\text{dB}} = 10\cdot\log_{10}\frac{\text{信号能量}}{\text{噪声能量}} \]

- 把黄金答案当成"信号"，硬件输出与黄金答案的差当成"噪声"。
- SNR 越高，说明硬件输出越接近黄金答案。
- 本项目规定 **SNR ≥ 40 dB**（或噪声恰好为 0）就算通过。40 dB 意味着信号能量是噪声能量的 10000 倍，对 16 位定点输出来说是相当严格的门槛。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件展开，另外两个文本文件是它要读的"考题和答案"。

| 路径 | 作用 |
|------|------|
| [SIM/FFT_tb.v](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v) | testbench 主体：生成时钟/复位、喂 5 组激励、算 SNR、判通过 |
| [SIM/Test_cases/IN_real_pattern01.txt](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/Test_cases/IN_real_pattern01.txt) | 第 1 组输入实部，32 个十进制整数（12 位有符号范围） |
| [SIM/Test_cases/OUT_real_16_pattern01.txt](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/Test_cases/OUT_real_16_pattern01.txt) | 第 1 组黄金输出实部，32 个十进制整数（16 位有符号范围） |

> 提示：`SIM/Test_cases/` 下一共有 5 组，每组 4 个文件（`IN_real` / `IN_imag` / `OUT_real_16` / `OUT_imag_16`，后缀 `pattern01`~`pattern05`）。testbench 通过 `case(i)` 切换文件名来逐组喂入。

---

## 4. 核心概念与源码讲解

本讲按"考试流程"的先后顺序拆成四个最小模块：**时钟与复位 → 输入激励 → 输出比对与 SNR → 延迟统计与通过判定**。这正好对应 testbench 里 `initial` 块从上到下的执行顺序。

### 4.1 时钟与复位生成

#### 4.1.1 概念说明

任何同步电路都需要两样东西才能动起来：一个**周期性翻转的时钟**，和一个**确定的初始状态**（复位）。testbench 必须把这两样"基础设施"造好，DUT 内部的流水线寄存器才能从已知状态开始一拍一拍地工作。本项目目标频率是 100 MHz（周期 10 ns），testbench 就要忠实地产生 10 ns 周期的时钟。

#### 4.1.2 核心流程

时钟与复位的产生流程可以概括为：

1. 用 `timescale` 声明时间单位为 1 ns。
2. 用一个 `always` 块让 `clk` 每过半个周期翻转一次，得到周期 = 2 × 半周期 的方波。
3. 在 `initial` 块里给 `clk / rst_n / in_valid` 一个起始值。
4. 在喂每组数据前，插入一个"复位脉冲"：先把复位拉到有效电平一拍，再释放，让 DUT 所有寄存器归零。

伪代码：

```text
cycle = 10.0
每隔 cycle/2 = 5 ns:  clk ← ~clk        // 得到 10 ns 周期 = 100 MHz

每组数据开始前:
    等 2 个下降沿
    在下降沿把 rst_n 拉成有效，持续 1 拍
    在下降沿释放 rst_n
    再等 1 个下降沿，开始喂数据
```

#### 4.1.3 源码精读

**时间标尺与周期参数**：第 2 行声明仿真时间单位为 1 ns、精度 10 ps；第 16 行把周期定为 10.0 ns。

[SIM/FFT_tb.v:L2-L2](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L2) —— `` `timescale 1ns/10ps ``，这是后面所有 `#` 延时的基准。

[SIM/FFT_tb.v:L16-L16](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L16) —— `parameter cycle = 10.0;`，对应 100 MHz 时钟，与 README 的 10 ns 设计指标一致。

**时钟生成**：用一个永远不停的 `always` 块，每半个周期翻转 `clk`。

[SIM/FFT_tb.v:L27-L27](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L27) —— `always #(cycle/2.0) clk = ~clk;`，半周期 5 ns 翻转一次 → 周期 10 ns。

**信号初值**：在 `initial` 开头把所有控制信号置为安全初值。

[SIM/FFT_tb.v:L49-L55](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L49-L55) —— `clk = 0; rst_n = 1; in_valid = 0;` 并把能量、延迟累加器清零。

**复位脉冲**：在每组数据喂入前，用下降沿对齐插入一个复位脉冲。

[SIM/FFT_tb.v:L90-L93](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L90-L93) —— 连续等待下降沿，期间把 `rst_n` 先拉 0 再拉回 1，形成一个一拍宽的复位脉冲。注意 testbench 用 `@(negedge clk)` 对齐，避免在时钟沿中间变化导致亚稳态。

> ⚠️ 一个值得在仿真时核对的细节：testbench 在第 219 行用 `.rst_n(rst_n)` 连接 DUT，而 [RTL/FFT.v:L27](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L27) 中顶层复位端口名是 `reset`、且是高有效（`posedge reset`）。`rst_n` 这种命名通常暗示"低有效"，与 DUT 的高有效 `reset` 之间存在命名/有效电平的差异。到底如何映射、是否影响仿真结果，请在本地跑通后对照波形确认（详见第 5 节实践），此处先**待本地验证**，不臆断结论。

#### 4.1.4 代码实践

- **实践目标**：确认时钟周期与复位时序符合预期。
- **操作步骤**：
  1. 用仿真器编译 `SIM/FFT_tb.v`（编译方法见第 5 节综合实践）。
  2. 在波形窗口里把 `clk`、`rst_n` 加入信号列表。
  3. 用游标测量 `clk` 两次上升沿之间的时间。
- **需要观察的现象**：`clk` 是周期方波；`rst_n` 在每组数据开始处有一个一拍宽的脉冲。
- **预期结果**：`clk` 两个上升沿间隔为 **10 ns**（即 100 MHz）。

> 因为本讲义无法替你运行仿真器，**具体波形数值待本地验证**。

#### 4.1.5 小练习与答案

1. **问**：`cycle = 10.0` 且 `timescale 1ns/10ps`，时钟频率是多少？
   **答**：半周期 `cycle/2 = 5 ns`，整周期 10 ns，频率 = 1/10 ns = **100 MHz**。

2. **问**：复位脉冲 `[SIM/FFT_tb.v:L90-L93](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L90-L93)` 里 `rst_n` 经历了哪些取值？
   **答**：初值 1 → 在某个下降沿拉到 0 持续 1 拍 → 下一个下降沿回到 1，形成一个低电平脉冲。注意它对 DUT 的实际复位效果取决于端口映射，需结合 DUT 复位有效电平判断（待本地验证）。

---

### 4.2 输入激励与 in_valid 控制

#### 4.2.1 概念说明

有了时钟，下一步就是"喂题"。FFT 处理器对每个输入样本都有一个**握手信号 `in_valid`**：只有当 `in_valid=1` 的那一拍，DUT 才会把 `din_r/din_i` 当成有效样本吃进去。testbench 的工作就是：每个时钟周期从文件里读一个数放到 `din_r/din_i`，同时拉高 `in_valid`，连续喂满 32 个（一组），然后拉低 `in_valid`，等待 DUT 把结果吐出来。

#### 4.2.2 核心流程

5 组数据的喂入是一个外层 `for(i)` 循环，每组内部流程如下：

```text
for i = 0 .. 4 (共 5 组 dataset):
    用 case(i) 打开第 i 组的 IN_real / IN_imag 两个输入文件
    插入复位脉冲
    for j = 0 .. 31:
        等下降沿
        in_valid = 1
        从 IN_real 读一个数 → din_r
        从 IN_imag 读一个数 → din_i
    等下降沿后 in_valid = 0          // 32 个喂完，停止喂入
    关闭输入文件
    ... 进入 4.3 的等待输出环节 ...
```

#### 4.2.3 源码精读

**外层循环与文件选择**：第 57 行起是 5 组数据的主循环；第 62–87 行用 `case(i)` 选择对应的输入文件名。

[SIM/FFT_tb.v:L57-L57](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L57) —— `for(i=0;i<dataset;i=i+1)`，`dataset=5`。

[SIM/FFT_tb.v:L62-L87](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L62-L87) —— `case(i)` 把第 0~4 组分别映射到 `IN_real/IN_imag_pattern01..05.txt`。

> ⚠️ **路径细节（直接决定能否跑通）**：所有 `$fopen` 用的是相对路径 `../Test_pattern/input/IN_real_pattern01.txt`，但仓库里这些文件其实放在 `SIM/Test_cases/` 下。这意味着仿真器的工作目录必须是某个文件夹的**子目录**，且它的同级目录下要有一个 `Test_pattern/`，结构如下（详见第 5 节）：
> ```text
> <run_dir>/                 <- 仿真器在这里运行（vsim 的工作目录）
> ../Test_pattern/input/     <- 放 IN_real/IN_imag_pattern0X.txt
> ../Test_pattern/output/    <- 放 OUT_real/OUT_imag_16_pattern0X.txt
> ```

**连续喂 32 个样本**：第 95–102 行内层循环。

[SIM/FFT_tb.v:L95-L102](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L95-L102) —— 每拍 `in_valid=1`，并用 `$fscanf(fp_r,"%d",din_r)` / `$fscanf(fp_i,"%d",din_i)` 从两个文件各读一个十进制整数，正好喂满 `FFT_size=32` 个样本。

**停止喂入**：

[SIM/FFT_tb.v:L103-L103](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L103) —— `@(negedge clk) in_valid = 0;`，喂完 32 个后下一拍把 `in_valid` 拉低，DUT 进入"消化+输出"阶段。

**输入文件长什么样**：以第 1 组实部为例，每行一个 12 位有符号十进制整数，共 32 行。

[SIM/Test_cases/IN_real_pattern01.txt:L1-L8](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/Test_cases/IN_real_pattern01.txt#L1-L8) —— 前几个样本 `0, -1700, -1605, 412, 1363, -115, -209, 277`，可见数值有正有负、绝对值在 12 位有符号范围（±2047）内，与 `IN_width=12` 一致。

#### 4.2.4 代码实践

- **实践目标**：在波形上看清"每拍喂一个样本、`in_valid` 持续 32 拍高、然后拉低"的过程。
- **操作步骤**：
  1. 跑起仿真后，把 `in_valid`、`din_r`、`din_i` 加入波形。
  2. 把 `din_r` 的显示格式改成 **Signed Decimal**（有符号十进制），否则负数会显示成一长串十六进制。
  3. 在第 1 组喂入区间放大观察。
- **需要观察的现象**：`in_valid` 连续 32 个时钟周期为 1；`din_r` 依次出现 `0, -1700, -1605, 412, ...`，与 `IN_real_pattern01.txt` 的内容逐行对应。
- **预期结果**：第 33 拍 `in_valid` 变为 0。**具体数值待本地验证**。

#### 4.2.5 小练习与答案

1. **问**：每组数据集喂入多少个样本？由哪个参数决定？
   **答**：喂入 `FFT_size = 32` 个样本，由第 10 行的 `parameter FFT_size = 32;` 决定。

2. **问**：喂完 32 个样本后，`in_valid` 下一拍的取值是什么？为什么？
   **答**：变为 0。因为一组 32 点样本已喂完，第 103 行 `in_valid = 0` 告诉 DUT"本组输入结束"，之后 DUT 进入输出阶段。

3. **问**：5 组输入文件按什么规律命名？
   **答**：实部 `IN_real_pattern01.txt` ~ `IN_real_pattern05.txt`，虚部 `IN_imag_pattern01.txt` ~ `IN_imag_pattern05.txt`，由 `case(i)` 中的 `i` 选择。

---

### 4.3 输出比对与 SNR 计算

#### 4.3.1 概念说明

喂完输入后，DUT 会经过一段流水线延迟，逐拍吐出 32 个输出样本（已经过硬件 SORT 模块还原成自然顺序）。testbench 此时要做三件事：(1) 等输出有效；(2) 逐拍读取硬件输出 `dout_r/dout_i` 和黄金输出 `gold_r/gold_i`；(3) 累加"信号能量"和"噪声能量"，最后算 SNR。

#### 4.3.2 核心流程

```text
等待 out_valid 拉高（第一个有效输出到来）
打开第 i 组的 OUT_real/OUT_imag 黄金文件
for j = 0 .. 31:
    等到 out_valid=1
    从黄金文件读 gold_r, gold_i
    signal_energy += gold_r² + gold_i²           // 信号能量
    noise_energy  += (gold_r - dout_r)² + (gold_i - dout_i)²   // 噪声能量
```

用公式写清楚两组累加：

\[
E_{\text{signal}} = \sum_{k=0}^{31}\left(g_r[k]^2 + g_i[k]^2\right)
\]

\[
E_{\text{noise}} = \sum_{k=0}^{31}\left((g_r[k]-d_r[k])^2 + (g_i[k]-d_i[k])^2\right)
\]

其中 \(g\) 是黄金值、\(d\) 是 DUT 输出。最终：

\[
\text{SNR}_{\text{dB}} = 10\cdot\log_{10}\frac{E_{\text{signal}}}{E_{\text{noise}}}
\]

#### 4.3.3 源码精读

**能量累加器的声明**：

[SIM/FFT_tb.v:L24-L25](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L24-L25) —— `noise/signal` 是 32 位有符号（放单个差值/样本），`noise_energy/signal_energy` 是 32 位无符号（放 32 个平方之和）。注意平方和可能很大，32 位无符号最大约 4.3 × 10⁹，对 16 位数据的 32 个平方之和足够。

**逐拍比对与累加**：

[SIM/FFT_tb.v:L156-L168](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L156-L168) —— 关键四步：先 `$fscanf` 读黄金 `gold_r/gold_i`；把 `gold_r`、`gold_i` 的平方累加进 `signal_energy`；把 `(gold_r - dout_r)`、`(gold_i - dout_i)` 的平方累加进 `noise_energy`。

> **位宽细节**：`gold_r/gold_i` 在第 22 行声明为 `reg signed [OUT_width:0]`，即 17 位有符号（比输出多 1 位）。这样 `gold_r - dout_r` 在做 17 位减法时不会因为 16 位有符号相减的溢出而失真，差值的符号位也能正确扩展。

**黄金输出文件长什么样**：

[SIM/Test_cases/OUT_real_16_pattern01.txt:L1-L8](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/Test_cases/OUT_real_16_pattern01.txt#L1-L8) —— 前 8 个黄金实部 `-1365, -5006, 6210, 3677, -7306, -3351, -7292, 1732`。可见数值范围明显大于输入（最大接近 ±13000），与 FFT 运算会放大动态范围、以及 `OUT_width=16`（±32767 范围）一致；文件名里的 `_16` 正是在标注 16 位输出。

#### 4.3.4 代码实践

- **实践目标**：亲眼看到硬件输出逐拍逼近黄金数据，并理解噪声能量的来源。
- **操作步骤**：
  1. 波形里加入 `out_valid`、`dout_r`、`dout_i`（同样设为 Signed Decimal）。
  2. 在第 1 组输出区间，对照 `OUT_real_16_pattern01.txt` 的第一行 `-1365`，看 `dout_r` 的第一个有效值。
  3. 心算 `(gold_r - dout_r)` 看差值有多大。
- **需要观察的现象**：`out_valid` 拉高期间，`dout_r` 依次出现一串接近 `-1365, -5006, 6210, ...` 的数值。
- **预期结果**：若硬件正确，`dout_r` 与黄金值之差应非常小（多为 0 或个位数），noise_energy 趋近 0。**具体误差大小待本地验证**。

#### 4.3.5 小练习与答案

1. **问**：`SNR_ratio >= 10000` 对应的 dB 阈值是多少？
   **答**：\(10\cdot\log_{10}(10000) = 10\times 4 =\) **40 dB**，这正是本讲开头说的"SNR ≥ 40 dB"门槛的来历。

2. **问**：如果某组数据硬件输出和黄金值完全相等，`noise_energy` 是多少？testbench 会怎么报告？
   **答**：`noise_energy = 0`。第 172–175 行会专门处理这种情况，打印 `SNR = infinity` 并直接判 pass（因为做除法会除以零，所以单独分支处理）。

3. **问**：为什么 `gold_r` 声明成 17 位 `[OUT_width:0]` 而不是 16 位？
   **答**：为了给 `gold_r - dout_r` 这步有符号减法多留 1 位余量，避免两个 16 位有符号数相减时溢出，保证差值的符号和数值都被正确表达。

---

### 4.4 延迟统计与通过判定

#### 4.4.1 概念说明

知道了"算得准"还不够，工程师还关心"**算得快不快**"——也就是从开始喂输入到拿到全部 32 个输出，需要多少个时钟周期，这叫**延迟 (latency)**。testbench 在等待输出的过程中顺便数周期，最后给出平均延迟。同时，每组数据算完 SNR 后立即做通过/失败判定：任何一组不达标，整个仿真就 `$finish` 停下来；5 组全过才打印"Well Done"。

此外，本 testbench 还内置了 `RTL` / `GATE` 两套模式切换，方便综合后再做一次门级仿真。

#### 4.4.2 核心流程

```text
// (1) 等第一个有效输出，期间数 latency
latency = 0
while (out_valid != 1):
    latency += 1
    若 latency > 68 则报错退出

// (2) 逐个读 32 个输出，期间继续数 latency（见 4.3）
// (3) SNR 判定
if (noise_energy == 0):
    打印 "SNR = infinity", pass
else:
    SNR_ratio = signal_energy / noise_energy
    打印 SNR_dB
    if (SNR_ratio >= 10000): pass
    else: 打印 failed, $finish       // 一组失败立即终止

// (4) 5 组全部完成后
打印 "Well Done"
打印 "Average latency = latency_total / 5"
```

#### 4.4.3 源码精读

**等待首个有效输出并数延迟**：

[SIM/FFT_tb.v:L109-L116](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L109-L116) —— `while(!out_valid)` 每等一个下降沿 `latency++`，超过 `latency_limit=68` 就报 "Latency too long" 并 `$finish`，防止 DUT 卡死时仿真挂死。

**逐个输出时继续数延迟**：

[SIM/FFT_tb.v:L148-L154](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L148-L154) —— 内层循环里同样用 `while(!out_valid) latency++`，把等待每个有效输出所消耗的周期也计入。

**SNR 通过/失败判定**：

[SIM/FFT_tb.v:L172-L186](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L172-L186) —— `noise_energy==0` 走 infinity 分支直接 pass；否则算 `SNR_ratio = signal_energy/noise_energy`，打印 `10*log10(SNR_ratio)`，并按 `SNR_ratio >= 10000` 判 pass，否则 `$finish`。

**最终汇总与平均延迟**：

[SIM/FFT_tb.v:L195-L210](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L195-L210) —— 5 组全过后，打印带颜色的 "Well Done" 庆祝信息，并输出 `Average latency = latency_total/dataset`（即除以 5）和时钟周期，最后 `$finish`。

**RTL / GATE 双模式**：

[SIM/FFT_tb.v:L31-L44](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L31-L44) —— 用 ` `ifdef RTL` / ` `elsif GATE` 选择波形转储方式：RTL 模式 dump FSDB；GATE 模式先 `$sdf_annotate("FFT_SYN.sdf",FFT_CORE)` 把综合后时序反标到门级网表上，再 dump 波形。`VCD`/`FSDB` 两个子宏控制波形格式。说明这套 testbench 既能验 RTL，也能在 u6 综合之后带上真实门延迟再验一遍。

#### 4.4.4 代码实践

- **实践目标**：从仿真日志里读出 5 组 SNR 与平均延迟，验证设计功能。
- **操作步骤**：
  1. 编译时务必加宏 `+define+RTL`（详见第 5 节），这样波形转储分支才生效。
  2. 运行仿真到 `$finish`。
  3. 翻看仿真器主控台（transcript）输出。
- **需要观察的现象**：控制台依次出现 5 段 `---------- SNR = ...` 和 `dataset N passed!!`，最后是 `Well Done` 和 `Average latency = ... cycles`。
- **预期结果**：5 组全部 passed，平均延迟为某个不超过 68 的数值。**具体 SNR dB 与平均周期数待本地验证**。

> 注意：`$fsdbDumpfile/$fsdbDumpvars` 是 Verdi/Novas 的 PLI 系统任务，QuestaSim 原生不带，需要加载 Novas PLI 库才能用。若你的环境没有 Verdi，可临时注释掉第 32–33 行，或用 QuestaSim 自带的波形（`.wlf`，`vsim` 默认就会记录）。这一点**待本地验证**。

#### 4.4.5 小练习与答案

1. **问**：`latency_limit` 是多少？它的作用是什么？
   **答**：`68` 个周期（第 14 行）。它是"看门狗"上限：如果等了 68 拍还没等到 `out_valid`，就认为 DUT 卡死，报错并 `$finish`，避免仿真无限挂起。

2. **问**：平均延迟是怎么算出来的？
   **答**：每组结束时 `latency_total = latency_total + latency`（第 191 行）；5 组跑完后用 `latency_total/dataset`（第 209 行）求平均，`dataset=5`。

3. **问**：如果第 2 组数据 SNR 只有 30 dB，仿真会怎样？
   **答**：30 dB 对应 `SNR_ratio = 10^3 = 1000 < 10000`，第 182–185 行会打印 `dataset 2 failed!! Bye` 并立即 `$finish`，第 3、4、5 组不会再跑。

---

## 5. 综合实践

把四个模块串起来，亲手把这颗 FFT 跑通一次。本任务对应大纲里的实践要求：**编译 RTL 与 testbench、运行仿真、记录 5 组 SNR 与平均延迟、保存 `in_valid` 与 `out_valid` 的波形截图**。

### 5.1 准备测试数据目录（关键，否则 `$fopen` 会失败）

testbench 用的是 `../Test_pattern/input/...` 和 `../Test_pattern/output/...` 相对路径，而仓库里的原始文件在 `SIM/Test_cases/`。需要先搭出 testbench 期望的目录结构：

1. 在 `SIM/` 下新建 `Test_pattern/input/` 和 `Test_pattern/output/` 两个文件夹。
2. 把 `SIM/Test_cases/` 下的文件分类拷过去：
   - `input/` ← 所有 `IN_real_pattern0X.txt`、`IN_imag_pattern0X.txt`（共 10 个）。
   - `output/` ← 所有 `OUT_real_16_pattern0X.txt`、`OUT_imag_16_pattern0X.txt`（共 10 个）。
3. 在 `SIM/` 下再建一个运行目录（例如 `SIM/run/`），仿真器就在这里启动，这样 `../Test_pattern/...` 正好指向第 1 步建好的目录。

> 这一步是能否跑通的"胜负手"，请务必照做。拷贝命令与文件分组**待本地验证**。

### 5.2 用 QuestaSim 编译并仿真（示例步骤）

下面是 QuestaSim/ModelSim 风格的命令（仅供参照，具体路径以你的环境为准，**待本地验证**）：

```bash
cd SIM/run
vlib work                       # 建工作库
vlog +define+RTL ../../RTL/FFT.v ../FFT_tb.v   # 编译，加 +define+RTL 启用波形转储分支
vsim -c -t 1ns TESTBED          # 加载 testbench（-c 为命令行模式；要波形可去掉 -c）
run -all                        # 跑到 $finish
```

> 说明：`RTL/FFT.v` 顶部用 ` `include "shift_16.v"` 等拉入了全部子模块，所以只需编译 `FFT.v` 一个文件即可带出整个设计；编译时要保证 `RTL/` 在 include 搜索路径里（上面用相对路径直接指到 `../../RTL/FFT.v`，include 会优先在源文件同目录查找）。**具体编译选项待本地验证**。

### 5.3 记录结果

仿真结束后，从控制台抄下表里的内容（示例为待填）：

| 数据集 | SNR (dB) 或 infinity | passed? |
|--------|----------------------|---------|
| dataset 1 | 待本地验证 | ☐ |
| dataset 2 | 待本地验证 | ☐ |
| dataset 3 | 待本地验证 | ☐ |
| dataset 4 | 待本地验证 | ☐ |
| dataset 5 | 待本地验证 | ☐ |

并记录最后一行：**Average latency = ____ cycles**，**Clk period = 10.00 ns**。

### 5.4 保存波形

在 GUI 模式下，把 `clk`、`rst_n`、`in_valid`、`din_r[11:0]`、`out_valid`、`dout_r[15:0]` 加入波形窗口，截下"喂入阶段（`in_valid` 高 32 拍）→ 间隔 → 输出阶段（`out_valid` 高 32 拍）"的完整画面存档，方便回头对照 4.2 和 4.3 的描述。

### 5.5 加分挑战（可选）

- 在 testbench 第 178 行 `SNR_ratio = signal_energy/noise_energy;` 后面用 `$display` 额外打印 `signal_energy` 和 `noise_energy` 的值，观察"完美通过"时 `noise_energy` 是不是 0。
- 把 `latency_limit` 从 68 临时改成 10，重跑仿真，观察 testbench 是否按第 113 行打印 "Latency too long" 并退出，从而理解看门狗机制。

---

## 6. 本讲小结

- testbench (`SIM/FFT_tb.v`) 是一台"自动阅卷机"：用 `always #(cycle/2.0)` 产生 10 ns（100 MHz）时钟，用下降沿对齐的 `rst_n` 脉冲复位 DUT。
- 它通过 `for(i=0..4)` 循环喂入 5 组数据，每组用 `in_valid=1` 连续喂 32 个 `din_r/din_i` 样本，再拉低 `in_valid`。
- 输出阶段逐拍比对硬件输出 `dout_r/dout_i` 与黄金值 `gold_r/gold_i`，把平方差累加成 `noise_energy`，把黄金值平方累加成 `signal_energy`。
- 通过判据是 `SNR_ratio >= 10000`（即 **40 dB**）或 `noise_energy == 0`（infinity）；任一组失败立即 `$finish`。
- 仿真同时统计 `latency`（上限 68 拍看门狗），5 组全过后打印 `Well Done` 与平均延迟。
- testbench 还内置 `RTL` / `GATE` 双模式（` `ifdef`），可在综合后用 `$sdf_annotate` 带门级时序再验一次——为 u6 的 ASIC 流程埋下伏笔。

## 7. 下一步学习建议

到这里你已经能让整个设计"动起来"并验证功能。接下来建议：

- **横向**：进入 u2，看 `SIM/FFT.py` 和 `SIM/twiddle_gen.py`，搞清楚这些黄金数据是怎么用软件参考模型算出来的，理解 radix-2 DIF 与旋转因子定点化的来龙去脉。
- **纵向**：进入 u3，逐个拆开 `RTL/radix2.v`、`shift_*.v`、`ROM_*.v`，看清 testbench 喂进去的样本在硬件内部到底走了哪条路。
- **验证深入**：本讲只讲了"怎么跑"，u5（`u5-l1` Testbench 与 SNR 验证方法）会专门把 SNR 公式、RTL/GATE 双模式、SDF 反标的细节讲透，建议在那儿再回看本讲的 4.3 / 4.4。
