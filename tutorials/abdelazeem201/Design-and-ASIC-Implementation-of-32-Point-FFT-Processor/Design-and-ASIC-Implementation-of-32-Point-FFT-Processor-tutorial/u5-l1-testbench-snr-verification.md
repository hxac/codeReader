# u5-l1 Testbench 与 SNR 验证方法

## 1. 本讲目标

本讲围绕项目里唯一一个可执行 testbench `SIM/FFT_tb.v` 展开。读完本讲，你应当能够：

- 把 `FFT_tb.v` 从第 1 行到第 228 行完整读懂，说清这台「自动阅卷机」按什么顺序工作。
- 理解 5 组数据集（`dataset=5`）的循环结构、复位/喂入时序、以及 `in_valid`/`out_valid` 握手。
- 掌握基于能量的信噪比公式 \( \text{SNR}_{\text{dB}}=10\cdot\log_{10}(E_s/E_n) \) 的来源、代码实现与 40 dB 通过判据。
- 理解 `RTL` / `GATE` 两套编译宏的差异，以及 `$sdf_annotate` 如何把门级时序反标回 DUT 做后仿真。

本讲是 U5（功能验证方法）的第一篇，承接 u1-l3 建立的「DUT + testbench」概念与 u3-l1 的顶层端口定义，往下不讲黄金数据如何生成（那是 u5-l2 的内容）。

## 2. 前置知识

在读懂本讲前，你需要先具备以下概念（前几讲已建立）：

- **DUT 与 testbench**：DUT（Design Under Test，被测设计）是例化为 `FFT_CORE` 的 `RTL/FFT.v`；testbench（本讲的 `TESTBED` 模块）扮演「自动阅卷机」，产生激励、采集输出、判分（来自 u1-l3）。
- **顶层端口**：`FFT` 模块对外端口为 `clk`、`reset`、`in_valid`、`din_r/din_i`（12 位有符号）、`out_valid`、`dout_r/dout_i`（16 位有符号）（来自 u3-l1）。
- **out_valid 行为**：一旦拉高就自锁保持高电平，配合 testbench 连续采样 32 个输出（来自 u4-l3）。
- **Verilog 基础语法**：`initial` / `always`、`@(negedge clk)`、`$fopen` / `$fscanf` / `$fclose` / `$display`、条件编译 `` `ifdef `` / `` `elsif ``。
- **dB 与对数**：分贝是对数刻度，\( 10\cdot\log_{10}(x) \) 把功率/能量比换成 dB。

几个本讲会用到的术语：

| 术语 | 含义 |
|------|------|
| 黄金数据（golden） | 由软件参考模型算出的「标准答案」，存于 `SIM/Test_cases/OUT_*` 文件 |
| SNR（信噪比） | 信号能量与噪声能量之比，衡量硬件输出偏离标准答案的程度 |
| 仿真模式（RTL/GATE） | 分别对应「仿真 RTL 源码」与「仿真综合后门级网表 + 时序」 |
| SDF（Standard Delay Format） | 标准延迟格式文件，记录每个门/连线的真实延时，用于后仿真 |
| 看门狗（watchdog） | 一个时间上限，超过即认定设计卡死并中止仿真 |

## 3. 本讲源码地图

本讲几乎全部内容集中在一个文件里：

| 文件 | 行数 | 作用 |
|------|------|------|
| [SIM/FFT_tb.v](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v) | 228 | 唯一的 testbench，本讲通读对象 |

需要时还会引用以下文件做交叉对照（只读不改）：

| 文件 | 用途 |
|------|------|
| [RTL/FFT.v](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v) | 核对 DUT 的复位端口名与复位极性 |
| `SIM/Test_cases/OUT_real_16_pattern01.txt` | 观察黄金数据的数值量级，判断能量是否会溢出 |
| `SIM/pre_Synthesis.mpf`、`SIM/work/` | QuestaSim/ModelSim 工程与编译产物，佐证本设计用 Mentor 系仿真器 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**(4.1) 数据集循环与喂入时序**、**(4.2) SNR 能量计算**、**(4.3) 通过/失败判定**、**(4.4) RTL 与 GATE 仿真模式**。它们正好对应 testbench 主 `initial` 块的四段职责。

### 4.1 数据集循环与喂入时序

#### 4.1.1 概念说明

testbench 的第一大职责是**按时序喂输入**。它不能像软件那样「直接调用函数」，而要模仿真实硬件的握手：

- 拉时钟与复位；
- 用 `in_valid` 告诉 DUT「现在 `din_r/din_i` 上是有效样本」；
- 连续喂 32 个样本（一组完整 FFT 帧）；
- 等待 DUT 算完（`out_valid` 拉高）再去采样输出。

这一套动作要对 **5 组数据集** 各做一遍，所以外层是一个 `for (i=0; i<dataset; i++)` 循环。本模块解决「激励怎么产生、什么时候产生」。

#### 4.1.2 核心流程

单组数据集的喂入时序伪代码如下（对应 `FFT_tb.v` 主 `initial` 块的前半段）：

```
for i = 0 .. dataset-1:                          // 外层：5 组数据集
    打开 IN_real/IN_imag pattern0(i+1).txt
    @(negedge clk)                                // 对齐节拍
    @(negedge clk) rst_n = 0;                     // 复位脉冲（下降沿）
    @(negedge clk) rst_n = 1;
    @(negedge clk)
    for j = 0 .. FFT_size-1:                      // 喂 32 个输入样本
        @(negedge clk)
        in_valid = 1
        din_r = $fscanf(...)                       // 从文件读一个实部
        din_i = $fscanf(...)                       // 从文件读一个虚部
    @(negedge clk) in_valid = 0                    // 喂完，撤销握手
    关闭输入文件
    latency = 0
    while (!out_valid):                            // 等 DUT 算完
        @(negedge clk) latency = latency + 1
        if (latency > 68): $finish                 // 看门狗兜底
    ... 进入 4.2 的采样比对 ...
```

几个关键设计意图：

1. **所有驱动都对齐到 `negedge clk`**（下降沿）。因为 DUT 内部寄存器在 `posedge clk`（上升沿）采样，让 testbench 在下降沿改变输入，可以避免「同一沿同时变」的竞争，保证喂进去的值稳稳被采到。
2. **`in_valid` 在喂样本期间保持 1，喂完立即拉 0**。这是与 DUT 的握手约定（与 u4-l1 的 valid 菊花链呼应）。
3. **看门狗 `latency_limit = 68`**：如果等了 68 拍 `out_valid` 还没起来，认定设计卡死，直接 `$finish`，避免仿真空转。

#### 4.1.3 源码精读

**参数定义**——决定了整个 testbench 的规模与时钟：

[SIM/FFT_tb.v:L10-L16](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L10-L16) 定义了 FFT 点数（32）、数据集组数（5）、输入位宽（12）、输出位宽（16）、看门狗（68 拍）和时钟周期（10.0 ns，即 100 MHz）。

**时钟生成**——一句 `always` 产生方波时钟：

[SIM/FFT_tb.v:L27-L27](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L27-L27) 用 `always #(cycle/2.0) clk = ~clk;` 每 5 ns 翻转一次，得到周期 10 ns 的方波。

**外层循环与输入文件选择**：

[SIM/FFT_tb.v:L57-L87](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L57-L87) 是 `for(i=0;i<dataset;i=i+1)` 外层循环，开头先用 `case(i)` 选出当前组的两个输入文件 `IN_real_pattern0X.txt` 与 `IN_imag_pattern0X.txt` 并 `$fopen` 打开。注意 `default` 分支会打印错误并 `$finish`——这是防御式写法，理论上 `i` 取不到 5 以外的值。

**复位序列**：

[SIM/FFT_tb.v:L90-L93](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L90-L93) 依次：等待一个下降沿、把 `rst_n` 拉低、下一个下降沿再拉高，构成一个持续一拍的复位脉冲。

**喂入 32 个样本**：

[SIM/FFT_tb.v:L95-L102](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L95-L102) 内层 `for(j=0;j<FFT_size;j=j+1)` 每拍把 `in_valid` 置 1，并用 `$fscanf` 从文件读一个整数赋给 `din_r`/`din_i`。

[SIM/FFT_tb.v:L103-L107](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L103-L107) 喂满 32 个后，下一拍把 `in_valid` 拉回 0，并关闭两个输入文件。

**等待 out_valid（带看门狗）**：

[SIM/FFT_tb.v:L109-L116](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L109-L116) 是 `while(!out_valid)` 循环：每等一个下降沿 `latency` 加 1，一旦 `out_valid` 拉高就退出；若超过 `latency_limit`（68）仍未拉高，打印「Latency too long」并 `$finish`。

> ⚠️ **重要：仓库现状下这个 testbench 无法直接跑通，存在三处需要修复的不一致**（这些是真实源码问题，不是本讲义编造，初学者照搬运行一定会撞上）：
>
> 1. **端口名不匹配**：testbench 在 [SIM/FFT_tb.v:L218-L218](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L218-L218) 用的是命名连接 `.rst_n(rst_n)`，但 DUT `FFT` 模块的端口叫 `reset`（见 [RTL/FFT.v:L26-L28](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L26-L28)）。命名连接要求名字必须与模块端口一致，否则 QuestaSim 等仿真器会在 elaborate 阶段报错。修复方式是把连接改成 `.reset(...)`。
> 2. **复位极性不匹配**：DUT 用的是**高有效**异步复位，见 [RTL/FFT.v:L254-L255](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L254-L255) 的 `always@(posedge clk or posedge reset) if(reset)`；而 testbench 把信号当**低有效**来驱动（`rst_n=0` 表示复位，`rst_n=1` 表示正常）。即便修好端口名，极性也需要对齐。
> 3. **文件路径不匹配**：testbench 里写的是 `../Test_pattern/input/...` 与 `../Test_pattern/output/...`（见上面 L64-L81、L121-L138），但仓库里实际数据文件位于 `SIM/Test_cases/`。
>
> 综合实践（第 5 节）会给出一个修复与运行的最小清单。这三处不一致在 u1-l3 已埋下伏笔，本讲把它们定位到具体行号。

#### 4.1.4 代码实践

**实践目标**：在源码层面跟踪「单组数据集」从复位到 `out_valid` 起来的完整时间线，并核对 32 个样本确实被喂入。

**操作步骤**：

1. 打开 [SIM/FFT_tb.v:L57-L116](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L57-L116)。
2. 数一数从进入 `case(i)` 到 `in_valid=0` 之间，testbench 一共经历了多少个 `@(negedge clk)`：复位段 4 个 + 喂入段 32 个 + 撤销段 1 个 = **37 个下降沿**。
3. 对照 `SIM/Test_cases/IN_real_pattern01.txt`，确认它正好有 32 行整数（每行一个样本），与 `FFT_size=32` 对应。
4. 把第 1 组的输入文件名 `IN_real_pattern01.txt` / `IN_imag_pattern01.txt`、第 5 组的 `IN_real_pattern05.txt` / `IN_imag_pattern05.txt` 分别对应到 `case(i)` 的 `0:` 与 `4:` 分支，验证索引 `i` 与文件序号 `0X` 之间是 `i+1` 的关系。

**需要观察的现象**：`in_valid` 在连续 32 个下降沿保持 1，随后回到 0；大约再过若干拍 `out_valid` 才拉高（这部分由 DUT 决定）。

**预期结果**：单组喂入耗时 37 个时钟沿；`out_valid` 上升前的等待拍数（即 `latency`）应明显小于看门狗 68。结合 u4-l1 的结论（填充延时约等于各级 shift 深度之和 16+8+4+2+1=31，再加少量控制开销），`latency` 大致落在 30～45 拍区间。

**运行结果**：**待本地验证**。原因即上面三处不一致——必须先修好端口名、复位极性、文件路径，才能在仿真器里真正看到波形。在仅做源码阅读的前提下，上述「37 拍喂入 + 约 30～45 拍等待」是基于代码与 u4-l1 推得的理论值。

#### 4.1.5 小练习与答案

**练习 1**：`cycle=10.0` 时，时钟频率是多少？为什么用 `always #(cycle/2.0)` 而不是 `always #cycle`？

> **答案**：频率 = 1/10 ns = 100 MHz。`always #(cycle/2.0)` 每 5 ns 翻转一次，两次翻转拼成一个 10 ns 完整周期；若用 `always #cycle`，周期会变成 20 ns（频率 50 MHz），与设计规格不符。

**练习 2**：为什么 testbench 把所有输入驱动都放在 `@(negedge clk)` 之后？

> **答案**：DUT 内部寄存器在 `posedge clk` 采样。testbench 在下降沿改变输入，到下一个上升沿之间输入已稳定，可避免「同一时刻既变又采」的仿真竞争，保证喂入的值被正确采集。

**练习 3**：`latency_limit=68` 这个看门狗在什么情况下会触发？触发后仿真如何收场？

> **答案**：当 `while(!out_valid)` 等待的拍数超过 68 时触发，说明 DUT 迟迟没有给出有效输出（设计卡死或接线错误）。触发后 testbench 打印「Latency too long」并调用 `$finish` 立即结束仿真，避免空转。

---

### 4.2 SNR 能量计算

#### 4.2.1 概念说明

testbench 的第二大职责是**给 DUT 打分**。这里没有用「逐位比对是否相等」这种非黑即白的判分，而是用**信噪比 SNR**：把黄金值 `gold` 当作「信号」，把硬件输出与黄金值之差 `gold - dout` 当作「噪声」，算两者的能量比。这样既能判「完全相等」，也能在定点量化带来微小误差时给出一个连续的分数，容许一定偏差。

这是定点 FFT 验证里最常用的打分方式之一——因为硬件做了截位/定点化，输出未必与浮点参考模型位位相等，但只要噪声能量足够小就算通过。

#### 4.2.2 核心流程

设第 \(k\) 个输出点的黄金实部/虚部为 \(g_r[k], g_i[k]\)，硬件输出为 \(d_r[k], d_i[k]\)，共 \(N=32\) 个点。定义：

信号能量（黄金值自身的能量）：

\[
E_s = \sum_{k=0}^{N-1}\left( g_r^2[k] + g_i^2[k] \right)
\]

噪声能量（硬件输出与黄金值之差的能量）：

\[
E_n = \sum_{k=0}^{N-1}\left( (g_r[k]-d_r[k])^2 + (g_i[k]-d_i[k])^2 \right)
\]

线性信噪比与 dB 信噪比：

\[
\text{SNR}_{\text{ratio}} = \frac{E_s}{E_n}, \qquad
\text{SNR}_{\text{dB}} = 10\cdot\log_{10}\left(\frac{E_s}{E_n}\right)
\]

两个关键性质：

- 当 \(E_n=0\)（每个输出都位精确等于黄金值）时，\(\text{SNR}\to\infty\)，代码走专门的「infinity」分支直接判通过。
- 当 \(E_n\neq 0\) 时，用 \(\text{SNR}_{\text{ratio}}\) 与阈值比较（见 4.3）。

> 数学注记：这里用「能量」而非「功率」，但因为信号和噪声都除以同一个样本数 \(N\)，比值 \(E_s/E_n\) 与功率比完全相同，所以直接用能量比代入 \(10\log_{10}\) 是正确的。

#### 4.2.3 源码精读

**相关变量声明**：

[SIM/FFT_tb.v:L24-L25](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L24-L25) 声明了 4 个累加相关变量：`noise`、`signal` 是 32 位**有符号**寄存器，用于暂存单个样本及其差值；`noise_energy`、`signal_energy` 是 32 位**无符号**寄存器，用于累加平方和。

**采样并累加能量**（本模块的核心）：

[SIM/FFT_tb.v:L156-L169](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L156-L169) 在内层 `for(j)` 循环里，每拍先 `$fscanf` 读入一对黄金值 `gold_r/gold_i`，然后做两件事：

- **信号能量**：`signal = gold_r; signal_energy += signal*signal;` 再对 `gold_i` 做一次，累加 \(g_r^2+g_i^2\)。
- **噪声能量**：`noise = gold_r - dout_r; noise_energy += noise*noise;` 再对虚部做一次，累加 \((g_r-d_r)^2+(g_i-d_i)^2\)。

这正对应上面的 \(E_s\) 与 \(E_n\) 公式。注意 `dout_r/dout_i` 是 DUT 当前拍输出的硬件值，`gold_r/gold_i` 是从文件读的黄金值——两者在同一拍对齐比对。

> 细节注记：`gold_r/gold_i` 声明为 `reg signed [OUT_width:0]`，即 17 位有符号（见 [SIM/FFT_tb.v:L22-L22](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L22-L22)），比 16 位的 `dout` 多一位符号空间，做减法 `gold - dout` 时不易溢出，差值再提升到 32 位 `noise` 里计算平方。

> 关于溢出：`signal_energy`/`noise_energy` 是 32 位无符号（上限约 \(4.29\times10^9\)）。以 pattern01 为例，`OUT_real_16_pattern01.txt` 里数值多在 ±2000 量级，\(E_s\) 约 \(10^7\sim10^8\)，不会溢出。但若某组黄金值普遍接近 16 位有符号的上下限（±32767），平方和可能逼近或超过 32 位上限——这是该累加器的一个潜在弱点，对极端激励可能失真。日常验证用的 5 组 pattern 量级温和，不受影响。

#### 4.2.4 代码实践

**实践目标**：定位 `signal_energy` / `noise_energy` 的累加公式，手算「硬件输出与黄金值完全相等」时的 SNR 表现，并验证公式与 4.2.2 的数学定义一致。

**操作步骤**：

1. 在 [SIM/FFT_tb.v:L156-L169](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L156-L169) 找到 `signal = gold_r; signal_energy = signal_energy + signal*signal;` 这一组语句，确认它对应 \(E_s\)；同样找到 `noise = gold_r - dout_r; noise_energy = noise_energy + noise*noise;` 确认它对应 \(E_n\)。
2. 做一个**手算实验**：假设某个数据集里 `dout_r[k]=gold_r[k]`、`dout_i[k]=gold_i[k]` 对所有 32 个点都成立。代入噪声公式：
   \[
   \text{noise} = g_r - d_r = 0, \quad \text{noise}^2 = 0 \;\Rightarrow\; E_n = 0
   \]
   于是 \(\text{SNR}_{\text{ratio}}=E_s/0\) 在数学上发散，\(\text{SNR}_{\text{dB}}\to+\infty\)。
3. 再看代码怎么处理这个发散：[SIM/FFT_tb.v:L172-L175](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L172-L175) 用 `if(noise_energy == 0)` 单独判了一支，直接打印「SNR = infinity」并判通过——**根本不去算除法**，巧妙避开了除零。

**需要观察的现象**：当硬件实现与黄金模型位精确一致时，testbench 输出的是 `SNR = infinity` 而不是某个有限 dB 值。

**预期结果**：对本设计而言，黄金数据本身就是按与硬件一致的定点模型生成的（详见 u5-l2），所以正常运行最常见的输出恰恰是 `SNR = infinity`、`dataset pass!!`，而不是 40～50 dB 的有限值。这也是为什么 4.3 的「数值阈值」分支在实际跑时反而很少被触发。

**运行结果**：**待本地验证**（同样依赖先修复 4.1.3 的三处不一致）。

#### 4.2.5 小练习与答案

**练习 1**：为什么用「能量比」\(E_s/E_n\) 而不是「最大绝对误差」\(\max|g-d|\) 来打分？

> **答案**：能量比把全部 64 个分量（32 实 + 32 虚）的误差平方求和，反映整体偏离程度；最大绝对误差只看最坏一个点，容易被单个离群点主导，看不出整体好坏。SNR 是信号处理领域衡量定点化误差的标准指标。

**练习 2**：`signal_energy` 为什么声明成无符号 `reg [31:0]` 而 `signal` 是有符号 `reg signed [31:0]`？

> **答案**：`signal` 可能是负数（黄金值可正可负），需要符号位，所以有符号；而 `signal*signal`（平方）永远非负，累加结果也非负，所以用无符号即可，且无符号能把 32 位全部用于表示幅值，范围更大。

**练习 3**：若把 `gold_r - dout_r` 的减法放在 16 位宽度里做，会有什么风险？

> **答案**：两个 16 位有符号数相减，结果可能需要 17 位才不溢出（例如 32767 − (−32768) = 65535）。代码特意把 `gold_r` 声明成 17 位、`noise` 声明成 32 位，就是为了给减法留足位宽，避免溢出导致平方和失真。

---

### 4.3 通过/失败判定

#### 4.3.1 概念说明

算出 \(E_s\)、\(E_n\) 之后，testbench 要给出明确的「通过/失败」结论。本设计的判据是**两级**的：

1. 若 \(E_n=0\)（完全匹配）：直接判通过，记 `SNR = infinity`。
2. 否则按 \(\text{SNR}_{\text{ratio}}=E_s/E_n\) 是否 ≥ 10000 来判，等价于 \(\text{SNR}_{\text{dB}}\ge 40\,\text{dB}\)。

任何一个数据集失败，立即 `$finish` 中止整个仿真；5 组全过才打印庆祝横幅与平均延迟。这是一种「严格门限 + 一票否决」的策略。

#### 4.3.2 核心流程

判分伪代码（对应代码 L172-L186）：

```
if (noise_energy == 0):
    打印 "SNR = infinity"
    打印 "dataset i+1 pass!!"
else:
    SNR_ratio = signal_energy / noise_energy          // 整数除法
    打印 "SNR = 10*log10(SNR_ratio)"                   // 显示 dB 值
    if (SNR_ratio >= 10000):
        打印 "dataset i+1 passed!!"
    else:
        打印 "dataset i+1 failed!! Bye"
        $finish                                        // 一票否决
```

阈值推导：

\[
\text{SNR}_{\text{ratio}} \ge 10000
\;\Longleftrightarrow\;
\text{SNR}_{\text{dB}} = 10\log_{10}(\text{SNR}_{\text{ratio}}) \ge 10\log_{10}(10000) = 10\times 4 = 40\,\text{dB}
\]

所以「`SNR_ratio >= 10000`」与「SNR 不低于 40 dB」是同一件事的两种说法。

#### 4.3.3 源码精读

**判分主逻辑**：

[SIM/FFT_tb.v:L172-L186](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L172-L186) 是本模块全部内容：

- L172-L175：`if(noise_energy == 0)` 分支，打印 `SNR = infinity` 与 `pass`。
- L178-L179：`SNR_ratio = signal_energy/noise_energy;` 用整数除法得到比值，再用 `$log10(SNR_ratio)*10.0` 换算成 dB 打印。`$log10` 是 Verilog 系统函数（IEEE 1364 标准的以 10 为底对数），返回实数，乘 10 即得分贝数。
- L181-L185：`if(SNR_ratio >= 10000)` 判通过；`else` 打印 `failed` 并 `$finish`。

> 细节注记：`SNR_ratio` 声明为 `integer`（见 [SIM/FFT_tb.v:L8-L8](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L8-L8)），`signal_energy`/`noise_energy` 是无符号 32 位，所以 `signal_energy/noise_energy` 是**整数除法**（截断小数）。这会让显示的 dB 值比真实值略低一点点，但因为阈值 10000 留了余量，正常通过的设计（通常远超 40 dB 或直接 infinity）不受影响。

**全部通过后的收尾**：

[SIM/FFT_tb.v:L195-L212](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L195-L212) 在 5 组数据集全部通过后，打印彩色「Well Done」横幅、时钟周期与**平均延迟** `latency_total/dataset`，最后 `$finish` 收场。

#### 4.3.4 代码实践

**实践目标**：把「`SNR_ratio >= 10000`」翻译成 dB 阈值，亲手验证它就是 40 dB。

**操作步骤**：

1. 找到判据行 [SIM/FFT_tb.v:L181-L181](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L181-L181) 的 `if(SNR_ratio >= 10000)`。
2. 手算阈值：
   \[
   10\cdot\log_{10}(10000) = 10\cdot\log_{10}(10^4) = 10\times 4 = 40
   \]
   即 **40 dB**。
3. 反过来 sanity check：若某次仿真打印 `SNR = 36.02`，问通过与否？因为 \(10^{36.02/10}\approx 3992 < 10000\)，对应 `SNR_ratio` 取整后小于 10000，**不通过**，仿真会在该组 `$finish`。
4. 再验一个边界：`SNR_ratio` 恰好等于 10000 时，`$log10(10000)*10 = 40.00`，显示 `SNR = 40.00` 且刚好通过（`>=` 含等号）。

**需要观察的现象**：仿真日志里每组数据集后面要么是 `SNR = infinity` + `pass!!`，要么是形如 `SNR = XX.XX` 的有限值；后者必须 ≥ 40.00 才通过。

**预期结果**：阈值 = 40 dB；本设计正常应为 `infinity`。任何一组打印出 `< 40.00` 的有限 SNR，整个仿真立即在该组中止。

**运行结果**：**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：把通过阈值从 `SNR_ratio >= 10000` 改成 `>= 1000`，对应多少 dB？是更严还是更松？

> **答案**：\(10\log_{10}(1000)=30\) dB。阈值降低，从 40 dB 放宽到 30 dB，判分**更宽松**（允许更大的噪声）。

**练习 2**：为什么任一组失败就用 `$finish` 立刻终止，而不是继续跑完 5 组再汇总？

> **答案**：这是一种 fail-fast 策略。一旦某组不通过，说明设计有功能性错误，继续跑后面几组没有诊断意义，反而浪费时间；尽早中止能让工程师立刻定位问题。

**练习 3**：`SNR_ratio` 用整数除法 `signal_energy/noise_energy`，相比浮点除法会让结果偏大还是偏小？

> **答案**：整数除法**向零截断**（丢掉小数部分），结果 ≤ 真实比值，所以算出的 `SNR_ratio` **偏小**，显示的 dB 也略偏低。这是一种保守倾向——让判分略严，但被 10000 的阈值余量覆盖。

---

### 4.4 RTL 与 GATE 仿真模式

#### 4.4.1 概念说明

同一个 testbench 既能仿真 RTL 源码，也能仿真综合后的门级网表——靠的是 Verilog 的**条件编译宏** `` `ifdef ``。两种模式的区别：

- **RTL 模式**（`` `ifdef RTL ``）：直接仿真 `FFT.v` 等行为级源码，不带具体门延时，速度快，用于功能验证。
- **GATE 模式**（`` `elsif GATE ``）：仿真综合后的门级网表，并用 `$sdf_annotate` 把标准延迟文件（SDF）反标到每个实例上，包含真实门延时与连线延时，用于**时序签核**（sign-off），验证综合后功能仍正确、时序不违例。

这一机制让一份 testbench 同时服务 U1/U3 阶段的 RTL 仿真与 U6 阶段综合后的门级仿真，是 ASIC 验证流程的关键枢纽。

#### 4.4.2 核心流程

第一个 `initial` 块（L29-L45）根据编译宏二选一：

```
`ifdef RTL:
    $fsdbDumpfile("FFT_RTL.fsdb")              // 波形存 FSDB（给 Verdi 看）
    $fsdbDumpvars(0, FFT_CORE)                  // dump 整个 DUT 层次
`elsif GATE:
    $sdf_annotate("FFT_SYN.sdf", FFT_CORE)      // 把 SDF 延时反标到 DUT
    `ifdef VCD  : $dumpfile/$dumpvars            // 可选：VCD 波形
    `elsif FSDB : $fsdbDumpfile/$fsdbDumpvars    // 可选：FSDB 波形
`endif
```

编译时通过 `+define+RTL` 或 `+define+GATE`（QuestaSim/VCS 命令行）选择模式；GATE 模式下还可叠加 `+define+VCD` 或 `+define+FSDB` 选波形格式。

#### 4.4.3 源码精读

**模式选择与 SDF 反标**：

[SIM/FFT_tb.v:L29-L45](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L29-L45) 是这个独立的 `initial` 块：

- L31-L33：`` `ifdef RTL `` 分支，调用 Synopsys Verdi 系的 `$fsdbDumpfile` / `$fsdbDumpvars` 把波形存成 `.fsdb` 文件。`$fsdb*` 是 Verdi/Debussy 波形录制系统函数，比标准 VCD 更紧凑（仓库里的 `SIM/vsim.wlf`、`SIM/work/` 等 QuestaSim 产物佐证了 Mentor 仿真环境）。
- L34-L35：`` `elsif GATE `` 分支，调用 `$sdf_annotate("FFT_SYN.sdf", FFT_CORE)` 把 SDF 文件里的延时标注到 `FFT_CORE` 实例的每个 cell/primitive 上，使门级网表带上真实延时。
- L36-L43：GATE 模式下的二级条件编译，可选 VCD（标准格式）或 FSDB（Verdi 格式）。

> ⚠️ **又一处需要核对的不一致**：`$sdf_annotate` 这里写的文件名是 `"FFT_SYN.sdf"`，但仓库里实际并不存在 `FFT_SYN.sdf`。用 `git ls-files` 查到仓库里的 SDF 文件只有两个：`SYN/output/FFT.sdf`（综合后）与 `Pnr/output/FFT.sdf`（布局布线后）。所以若要真正跑 GATE 模式，需要先把对应 SDF 改名为 `FFT_SYN.sdf` 并放到仿真工作目录，或把代码里的字符串改成实际路径。这是一处「名字对不上」的工程小坑，初学者照字面运行会报「找不到 SDF」。

#### 4.4.4 代码实践

**实践目标**：理解如何用编译宏切换 RTL/GATE 两种仿真，并能在命令行层面（不必真跑）说出两套调用方式的差异。

**操作步骤**：

1. 阅读 [SIM/FFT_tb.v:L31-L35](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L31-L35)，确认 RTL 分支只 dump 波形、GATE 分支先做 `$sdf_annotate`。
2. 设想两种命令行（**示例命令**，非仓库自带脚本，且需先修好 4.1.3 的端口/路径问题）：
   - RTL 仿真：`vlog +define+RTL FFT.v FFT_tb.v && vsim -do "run -all" TESTBED`
   - GATE 仿真：`vlog +define+GATE +define+FSDB <网表.v> FFT_tb.v && vsim -do "run -all" TESTBED`（并把综合网表与改名后的 `FFT_SYN.sdf` 放在工作目录）
3. 对照仓库现状：`SIM/pre_Synthesis.mpf` 是 QuestaSim 工程文件，`SIM/work/` 是已编译库——说明作者用的是 QuestaSim/ModelSim，且工程名 `pre_Synthesis` 暗示它原本就是为「综合前（RTL）」仿真准备的。
4. 思考：为什么 GATE 模式必须做 SDF 反标，而 RTL 模式不需要？

**需要观察的现象**：RTL 仿真波形里信号跳变是「瞬时」的（零延时或单位延时）；GATE 仿真波形里信号跳变会有纳秒级的真实延时，且可能因为延时违例出现毛刺。

**预期结果**：两种模式下，**功能层面** 5 组数据集都应通过（因为综合是逻辑等价变换）；但 GATE 模式额外检验了时序，若时序违例可能在门级仿真里出现 X 态或采样错误。本设计的综合报告（U6）显示关键路径 slack ≈ 0，恰好满足 10 ns 时钟，因此门级仿真预期能通过。

**运行结果**：**待本地验证**。本仓库未附带现成的 GATE 网表（`SYN/output/` 下的网表需自行综合产出），所以 GATE 模式需要先走完 U6 的综合流程才能复现。

#### 4.4.5 小练习与答案

**练习 1**：为什么用 `` `ifdef RTL `...` `elsif GATE `` 而不是写两个独立的 testbench？

> **答案**：激励产生、输出比对、SNR 计算、判分这些逻辑在 RTL 和 GATE 模式下完全相同，只有「要不要反标 SDF、波形存成什么格式」不同。用条件编译把差异隔离在一个 `initial` 块里，避免维护两份高度重复的代码，也保证两种模式跑的是同一套激励，可比性强。

**练习 2**：`$sdf_annotate("FFT_SYN.sdf", FFT_CORE)` 的第二个参数 `FFT_CORE` 起什么作用？

> **答案**：它指定 SDF 要反标到**哪个实例层次**。`FFT_CORE` 是 testbench 里 DUT 的实例名（见 L217）。SDF 里的延时路径是相对于这个实例层次来标注的，这样门级网表内部每个 cell 才能拿到正确的延时。

**练习 3**：FSDB 和 VCD 都是波形文件格式，为什么 GATE 模式还要额外用 `` `ifdef VCD `/`` `elsif FSDB `` 再选一次？

> **答案**：GATE 仿真通常数据量大（门级实例多、带延时、毛刺多），VCD 是通用文本格式、体积大、加载慢；FSDB 是 Synopsys 二进制格式、紧凑且与 Verdi 调试器深度集成。让用户按调试工具选用，灵活兼顾通用性与效率。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个**「让 testbench 真正跑起来」**的综合任务。

**任务**：诊断并修复 `SIM/FFT_tb.v` 当前与 DUT/数据文件的三处不一致，然后（若本地有 QuestaSim/ModelSim）跑通 5 组数据集，记录每组的 SNR 输出与平均延迟。

**最小修复清单**（仅用于本地学习验证，**不要提交对源码的修改**——本讲义只读源码）：

1. **端口名**：把 [SIM/FFT_tb.v:L218](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L218) 的 `.rst_n(rst_n)` 改为 `.reset(reset)`，并把内部信号 `rst_n` 重命名为 `reset`（或反之，保持一致即可）。
2. **复位极性**：DUT 是高有效复位（见 [RTL/FFT.v:L254-L255](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L254-L255)），把 testbench 的复位脉冲改成「先 `reset=1`、再 `reset=0`」，且默认值设为 0（不复位）。
3. **文件路径**：在仿真工作目录下建出 `../Test_pattern/input/` 与 `../Test_pattern/output/` 两层目录，把 `SIM/Test_cases/` 下的 20 个文件按 `IN_*`→`input/`、`OUT_*`→`output/` 分别拷入；或直接把 testbench 里的字符串路径改成 `SIM/Test_cases/`。

**进阶子任务（纯源码阅读型，无需仿真器）**：用 Python 复刻 testbench 的 SNR 判分逻辑——读入 `SIM/Test_cases/IN_real_pattern01.txt` 与 `IN_imag_pattern01.txt`，调用本仓库的 `SIM/FFT.py`（来自 u2-l1）算出参考输出当作 `gold`，再人为构造一个「加了一点噪声」的 `dout`，按 4.2 的公式算 \(E_s\)、\(E_n\)、\(\text{SNR}_{\text{dB}}\)，验证：

- 当 `dout == gold` 时，程序应输出 `infinity`；
- 当人为注入小噪声时，应输出一个 ≥ 40 dB 的有限值；
- 当人为注入大噪声时，应判 failed。

**预期结论**：

- 修复后 RTL 仿真，5 组数据集应全部打印 `SNR = infinity` 并出现 `Well Done` 横幅，平均延迟约 30～45 拍。
- Python 复刻的 SNR 函数在 `dout==gold` 时返回无穷大，与 testbench 的 `if(noise_energy==0)` 分支行为一致。

**运行结果**：**待本地验证**。

## 6. 本讲小结

- `SIM/FFT_tb.v` 是一台「自动阅卷机」：外层 `for(i=0..4)` 循环 5 组数据集，每组完成「复位 → 喂 32 样本 → 等 `out_valid` → 采 32 输出 → 算 SNR → 判分」。
- 时钟周期 10 ns（100 MHz），所有驱动对齐 `negedge clk` 以避开 posedge 采样竞争；`latency_limit=68` 是看门狗。
- SNR 用能量式定义：\(E_s=\sum(g_r^2+g_i^2)\)、\(E_n=\sum((g_r-d_r)^2+(g_i-d_i)^2)\)、\(\text{SNR}_{\text{dB}}=10\log_{10}(E_s/E_n)\)，代码在 L156-L169 累加。
- 判据是两级：\(E_n=0\) 直接 `infinity` 通过；否则 `SNR_ratio>=10000` 即 ≥ 40 dB 通过，任一组失败 `$finish` 一票否决。
- 同一 testbench 通过 `` `ifdef RTL `/`` `elsif GATE `` 同时服务 RTL 与门级仿真，GATE 模式用 `$sdf_annotate` 反标 SDF 做时序签核。
- 仓库现状下 testbench 有三处需修复的不一致：端口名（`rst_n` vs `reset`）、复位极性（低有效 vs 高有效）、文件路径（`../Test_pattern/` vs `SIM/Test_cases/`）；SDF 文件名 `FFT_SYN.sdf` 也对不上仓库实际的 `SYN/output/FFT.sdf`。

## 7. 下一步学习建议

- **u5-l2 测试激励与黄金参考模型**：本讲只讲了「怎么比对」，下一讲讲「黄金数据从哪来」——`SIM/FFT.py` 与 `SIM/FFT_test.c` 两套参考模型如何生成 `Test_cases/OUT_*` 黄金文件，以及 IN/OUT pattern 的文件格式约定。建议紧接着读。
- **复习 u4-l3**：本讲的 `out_valid` 行为与延迟统计依赖 u4-l3 讲的 `over`/`assign_out` 自锁握手，若对「为什么 `out_valid` 一旦拉高就保持」不清楚，回看那一讲。
- **延伸到 U6**：本讲的 GATE 模式是综合后仿真的入口，学完 u5 后可直接进入 u6-l1（Design Compiler 综合）看门级网表与 SDF 是怎么产出来的，把「RTL 仿真 → 综合 → 门级仿真」的闭环走完。
