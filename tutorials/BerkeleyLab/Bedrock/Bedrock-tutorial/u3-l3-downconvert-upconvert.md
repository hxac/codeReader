# 数字下变频与上变频

## 1. 本讲目标

本讲承接 u3-l2（混频器、DDS 与复数乘法），把单点的「乘法」拼成一条完整的射频数据通路：**下变频 → 基带处理 → 上变频**。

学完后你应该能够：

1. 说清 `fdownconvert` 为什么输出**半速率**的交织 IQ，以及它如何用一个特殊矩阵把「混频 + 低通」合并成「隐式抽取」。
2. 说清 `fiq_interp` 如何把半速率的交织 IQ **还原**成两条全速率的 I、Q 流。
3. 理解 `flevel_set` 的「点积」就是上变频的乘法核，以及 `afterburner` 为什么用割线（secant）插值而不是简单平均来求中点。
4. 读懂 `ssb_out` 如何把上面三块积木拼成一个 Hartley 单边带（SSB）调制器，驱动 DDR DAC。
5. 能画出整条通路的框图，并标注每一级的数据速率变化。

## 2. 前置知识

在进入源码前，先对齐几个本讲会用到的概念。如果你已经学过 u3-l2，可以快速跳过前两条。

- **IQ 基带**：一个复信号 \(I + jQ\)。实数 ADC 采到的是实信号，要得到复基带，需要把它和一对正交本振 cos/sin 相乘，分别得到 I（同相）和 Q（正交）。
- **本振 LO（Local Oscillator）**：这里由 u3-l2 的 `rot_dds` 产生，底层是 u3-l1 的 CORDIC。`ssb_out` / `fdownconvert` 的 `cosa/sina`、`cosd/sind` 端口就是这两路正弦/余弦本振，约定 18 位有符号定点，且**满量程负值是非法的 LO**。
- **下变频 / 上变频**：把信号从射频搬到基带叫下变频，反过来叫上变频。核心操作都是「与本振相乘」。
- **抽取（decimation）与插值（interpolation）**：降低采样率叫抽取（每 N 个样本取一个 + 抗混叠滤波），提高采样率叫插值（在样本之间补点 + 平滑）。
- **交织 IQ 流**：为了节省一条数据线，I 和 Q 轮流占用同一条总线：`I, Q, I, Q, ...`。一条 `trig`（或 `div_state[0]`）标志「当前这一拍是 I 还是 Q」。
- **DDR DAC**：在时钟的上升沿和下降沿都更新数据，等于把输出数据率翻倍。要喂满 DDR DAC，FPGA 侧需要以 2 倍速率准备两路样本（原始样本 + 插值中点）。
- **偏移二进制（Offset Binary）**：DAC 常用的一种编码，与有符号二补码只差一个最高位翻转。`afterburner` 直接吐偏移二进制，`ssb_out` 再翻回有符号。

> 一句话总览：Bedrock 这条链路的妙处在于「**位宽与速率处处守恒**」——下变频用半速率 IQ 换来无需显式低通；上变频用插值把半速率 IQ 补回全速率再喂 DDR DAC。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `dsp/README.md` | 这条链路的官方说明，讲了「半速率 IQ」「隐式抽取低通」的设计动机 |
| `dsp/fdownconvert.v` | **下变频**：ADC 实信号 + LO → 交织半速率 IQ |
| `dsp/fiq_interp.v` | **IQ 插值**：交织半速率 IQ → 两条全速率 I、Q 流（上变频的前置） |
| `dsp/flevel_set.v` | **载波电平重建**：LO 与 (I,Q) 做点积，即上变频的乘法核 |
| `dsp/afterburner.v` | **割线插值**：在相邻样本间求「圆上中点」，把速率翻倍喂 DDR DAC |
| `dsp/ssb_out.v` | **上变频顶层**：把 `fiq_interp` + `flevel_set` + `afterburner` 拼成 Hartley SSB 调制器 |

辅助阅读：`dsp/upconv.v` 是另一条更早期的上变频实现（用 `doublediff` + `interp1`），可作为对照；`dsp/ssb_out_tb.v`、`dsp/afterburner_tb.v` 是它们的测试台。

## 4. 核心概念与源码讲解

### 4.1 下变频 `fdownconvert`：输出半速率交织 IQ

#### 4.1.1 概念说明

下变频的任务是把 ADC 采到的实信号搬到基带并得到复 IQ。常规做法是：

1. 实信号分别乘 cos（得 I）和 sin（得 Q）；
2. 再用一个**低通滤波器**滤掉乘法产生的「和频」分量。

`fdownconvert` 的特别之处在于：它输出的 IQ 流是 **ADC 速率的一半**，而那个本该显式做的低通滤波，被一个巧妙的矩阵运算**隐式吸收**成了抽取滤波——于是「低通」和「降速」合二为一，省掉了一整个滤波器。这套数学来自 Larry Doolittle 的笔记（源码注释里有链接）。

#### 4.1.2 核心流程

它不是逐拍独立地算 I、Q，而是**取相邻两个样本 a[n]、a[n+1]，一次性算出一个 I 和一个 Q**：

\[
\begin{bmatrix} I \\ Q \end{bmatrix}
=
\begin{bmatrix}
 \sin[(n+1)\theta] & -\sin(n\theta) \\
-\cos[(n+1)\theta] &  \cos(n\theta)
\end{bmatrix}
\begin{bmatrix} a[n] \\ a[n+1] \end{bmatrix}
\]

其中 \(\theta\) 是每拍的相位步进。展开后：

\[
I = a[n]\sin[(n+1)\theta] - a[n+1]\sin(n\theta)
\]
\[
Q = -a[n]\cos[(n+1)\theta] + a[n+1]\cos(n\theta)
\]

因为 2 个 ADC 样本才产生 1 对 (I,Q)，**采样率天然减半**；而这 2 个样本的加权相减，正好起到了低通（抑制和频）的作用。最后 I、Q **轮流**从同一条 `o_data` 总线输出，保持「n bits/sec 进、n bits/sec 出」的位宽守恒。

#### 4.1.3 源码精读

模块端口：注意 `cosd/sind` 是 18 位有符号 LO 输入，`a_data` 是 ADC 读数，`o_data` 是 16 位交织 IQ，`mod2` 是「当前是第几个样本」的翻转位（计数器 LSB）。

[dsp/fdownconvert.v:16-33](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/fdownconvert.v#L16-L33) — 模块声明。注释 `//%` 开头的两行是 newad.py（见 u2-l3）识别的文档标记。`a_dw/o_dw` 都标了 `XXX don't change this`，因为位宽被矩阵运算的移位写死了。

第一段是 **LO 重排**：把本振延时出 `cosd_d1/d2`，再用 `mod2` 在「时刻 n」与「时刻 n+1」的本振之间选择，准备矩阵所需的两个时刻的 LO。

[dsp/fdownconvert.v:36-45](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/fdownconvert.v#L36-L45) — `cosd_r` 在 `~mod2` 时取 `cosd_d2`（对应 a[n] 时刻），否则取 `cosd`（对应 a[n+1] 时刻）。`sind_r` 同理。

第二段是 **乘法**：`a_data` 分别乘以重排后的 cos 和 sin，并多打几拍寄存器（`mul_i1/mul_i2`）帮布线。

[dsp/fdownconvert.v:49-57](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/fdownconvert.v#L49-L57) — `mul_i <= a_data * cosd_r; mul_q <= a_data * sind_r;`，取高 16 位（`[32:17]`）相当于右移 17 位做定标。

第三段是 **矩阵相减 + 交织输出**，这是「隐式低通/抽取」的关键：

[dsp/fdownconvert.v:59-68](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/fdownconvert.v#L59-L68) — `sum_i1x = mul_i1 - mul_i2` 实现 \(I\) 的差；`sum_q1x = mul_q2 - mul_q1` 实现 \(Q\) 的差；`sum_q2x` 再多打一拍让 Q 和 I 时间对齐。`iq_mux` 用 `mod2` 选：`mod2=0` 出 I，`mod2=1` 出 Q。`SAT` 宏做饱和截断到 16 位。

最后是输出握手：`o_gate` 恒为 1（数据始终有效），`o_trig = mod2`（标记 IQ 边界），`time_err` 监测 `mod2` 是否按预期翻转。

[dsp/fdownconvert.v:78-81](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/fdownconvert.v#L78-L81) — 输出赋值。

#### 4.1.4 代码实践（源码阅读型）

本仓库没有给 `fdownconvert` 单独的 `fdownconvert_tb`（它通常被上层 `iq_chain4_tb` 等间接测试），所以这里做源码阅读实践：

1. **目标**：验证「2 个 ADC 样本 → 1 对 (I,Q)」的半速率关系。
2. **步骤**：打开 `dsp/fdownconvert.v`，对照 4.1.2 的矩阵公式，逐拍画出 `mod2` 在 `0→1→0→1` 时，`cosd_r/sind_r` 取的是哪个时刻的本振，`iq_mux` 输出的是 I 还是 Q。
3. **观察**：注意 `mul_i2` 比 `mul_i1` 多一拍，`sum_q2x` 比 `sum_q1x` 多一拍——这些延迟都是为了让 I 路和 Q 路在**同一次 `mod2` 翻转窗口**里输出。
4. **预期结果**：每两个时钟周期输出一对 (I,Q)，与「半速率」结论一致。
5. 待本地验证：若本地装了 iverilog，可写一个最小 testbench 喂方波 ADC + 固定相位步进，dump `o_data` 与 `mod2` 看是否满足上述节拍。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `fdownconvert` 不需要像常规下变频那样再加一个低通滤波器？
**答案**：因为它一次取相邻两个样本做加权差（矩阵相减），这个差分运算本身就抑制了乘法产生的「和频」分量，等价于一个低通；同时 2 样本出 1 对 IQ，低通与抽取合二为一。

**练习 2**：输出 `o_data` 是 16 位，ADC `a_data` 也是 16 位，但中间乘法是 33 位。这些位是怎么收回到 16 位的？
**答案**：乘法后取高 16 位（`[32:17]`）做定标，相减后再用 `SAT` 宏饱和截断到 16 位，保证不溢出的同时维持位宽守恒。

### 4.2 IQ 插值 `fiq_interp`：把半速率交织流还原成全速率

#### 4.2.1 概念说明

`fdownconvert` 输出的是**交织**的半速率 IQ（一拍 I、一拍 Q 轮流出）。但到了上变频，`flevel_set` 需要的是**同时**拿到 I 和 Q 才能做点积。`fiq_interp` 就是这座桥：它接收交织 IQ，解交织成两条独立的 I、Q 流，并对每条流做线性插值，把每条流都补到**全速率**（输入速率 X，输出 I 和 Q 各自也是 X，而不是各 X/2）。

#### 4.2.2 核心流程

1. 用 `a_trig`（=「当前是 I 还是 Q」）把交织总线解复用成 `i_raw`、`q_raw`。
2. 对每条流做**线性插值**填补缺失拍：`i2i = i_raw + i_raw1`（当前 I + 上一拍 I），相当于相邻两点求和，得到一个带 ×2 增益的中点。
3. 输出 `i_data/q_data`（各 17 位，多出的 1 位来自求和的增益），`i_gate/i_trig` 恒为 1，表示已是连续的全速率流。

注意：这里的「插值」不是提高采样率到 2X，而是把「每隔一拍才有一个有效样本」的半速率 I 流，填补成「每拍都有」的全速率 I 流。这是为 `flevel_set` 准备同时刻的 (I,Q)。

#### 4.2.3 源码精读

端口：`a_data` 进交织 IQ，`a_trig` 标志当前是 I 还是 Q，`i_data/q_data` 出两路 17 位。

[dsp/fiq_interp.v:6-22](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/fiq_interp.v#L6-L22) — 模块声明。

解交织：用两级寄存器 `iq_in1/iq_in2` 缓存，根据 `iq_sync` 选择。

[dsp/fiq_interp.v:25-31](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/fiq_interp.v#L25-L31) — `i_raw`、`q_raw` 分别从 `iq_in1`/`iq_in2`/`a_data` 中选出当前拍对应的 I 与 Q。

插值求和：

[dsp/fiq_interp.v:34-40](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/fiq_interp.v#L34-L40) — `i2i <= i_raw + i_raw1; i2q <= q_raw + q_raw1;`。这就是线性插值的「求和」形式（中点 = (a+b)，未除 2，所以多 1 位增益，由后续 `flevel_set` 的定标吸收）。

输出恒为有效：

[dsp/fiq_interp.v:48-54](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/fiq_interp.v#L48-L54) — `i_gate=1, i_trig=1, q_gate=1, q_trig=1`。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：理解 `iq_sync` 如何驱动解复用。
2. **步骤**：假设输入序列是 `I0, Q0, I1, Q1, I2, ...`，`a_trig` 在 I 时刻为 1、Q 时刻为 0（或反之，取决于上层约定）。逐拍追踪 `iq_in1/iq_in2`，写出 `i_raw/q_raw` 的取值序列。
3. **观察**：`i_raw` 在「应该是 I 但还没到」的拍上会是什么？答案是上一拍缓存下来的值——这正是「填补缺失拍」的实现。
4. **预期结果**：每个时钟 `i_data` 都有一个值（要么是真实 I、要么是插值），`q_data` 同理，二者时间对齐。
5. 待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`fiq_interp` 的输出为什么是 17 位而输入是 16 位？
**答案**：因为 `i2i = i_raw + i_raw1`，两个 16 位数相加需要 17 位承载，多出的 1 位是 ×2 的插值增益，后续模块在定标时会把它吃掉。

**练习 2**：`i_gate/i_trig` 为什么恒为 1？
**答案**：经过插值后，I、Q 已是每拍都有效的全速率连续流，没有「空拍」需要标记，所以 valid 信号恒为 1。

### 4.3 载波电平重建 `flevel_set`：上变频的乘法核

#### 4.3.1 概念说明

`flevel_set` 本质就是 LO 向量 \((\cos\theta, \sin\theta)\) 与基带向量 \((I, Q)\) 的**点积**：

\[
\text{out} = I\cos\theta + Q\sin\theta
\]

这正是上变频「乘本振」的核心：它把基带复信号搬到本振频率上，得到实信号的某一个分量（实部）。`ssb_out` 里会调用它两次（第二次喂入 90° 旋转后的 IQ）来分别得到 SSB 的同相和正交分量。

#### 4.3.2 核心流程

1. `cosp = i_data * cosd`，`sinp = q_data * sind`（17 位 × 18 位 = 35 位）。
2. 取各自高 17 位（`[33:17]`）定标后相加，再加 1 做四舍五入。
3. `SAT` 饱和到 16 位，再右移 1 位（`sum2[16:1]`）去掉插值带来的 ×2 增益，输出 16 位。

#### 4.3.3 源码精读

端口与点积实现：

[dsp/flevel_set.v:7-24](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/flevel_set.v#L7-L24) — 模块声明，`i_dw/q_dw=17`、`o_dw=16` 同样被标为不要改动。

[dsp/flevel_set.v:27-37](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/flevel_set.v#L27-L37) — `cosp <= i_data * cosd; sinp <= q_data * sind; sum <= cosp_msb + sinp_msb + 1;`。`+1` 是截断前的舍入偏置（四舍五入）。`SAT` 饱和到 16 位，防止相加溢出。

输出：

[dsp/flevel_set.v:46-49](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/flevel_set.v#L46-L49) — `o_data = sum2[16:1]`，丢掉最低位等价于 ÷2，抵消 `fiq_interp` 留下的 ×2 增益，回到 16 位。`o_gate=1`，`o_trig=0`。

#### 4.3.4 代码实践（数值推演型）

1. **目标**：手算一次点积，验证定标。
2. **步骤**：取 \(I = 2^{16}\)（即 `i_data` 的 17 位值 65536）、\(Q = 0\)、\(\cos\theta = 2^{17}\)（`cosd` 满量程）、\(\sin\theta = 0\)。算 `cosp = 65536 × 131072`，取 `[33:17]`，再 `+1`、饱和、`[16:1]`。
3. **观察**：因为 LO 满量程负值非法、且这里几乎满量程，结果应接近 `i_data` 的量级。
4. **预期结果**：输出接近 65536 的一半左右（因定标），即一个合理的 16 位载波电平值。
5. 待本地验证：可用 `iverilog` 写 4 行激励直接 `$display` 出 `o_data` 对照手算。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `flevel_set` 里有个 `+1`？
**答案**：截断低位前先加 1，实现「四舍五入」而非「向下取整」，减少定点截断带来的直流偏置。

**练习 2**：`o_data = sum2[16:1]` 丢掉一位，这一位对应什么？
**答案**：对应 `fiq_interp` 插值时引入的 ×2 增益；丢掉它把输出从 17 位压回 16 位，完成增益归一。

### 4.4 割线插值 `afterburner`：为 DDR DAC 求圆上中点

#### 4.4.1 概念说明

上变频得到的全速率实信号，要喂给一个 **DDR（双倍数据率）DAC**——时钟上下沿都更新，等于要 2 倍速率的样本。最朴素的做法是在相邻样本间取算术平均当「中点」。但 `afterburner` 指出：如果两个样本本来就是「同一个正弦波上的相邻点」，它们位于同一个圆上，**圆上两点的中点不是算术平均**，而要乘一个修正系数。

#### 4.4.2 核心流程

设两相邻样本对应相位差 \(\phi\)，则圆上中点的幅值修正系数为：

\[
k = \tfrac{1}{2}\sec(\phi)
\]

于是中点 = \(k \cdot (a_n + a_{n+1})\)。流程：

1. 求相邻样本之和 `avg = data + data1`（即 \(a_n + a_{n+1}\)）。
2. `prod = avg * coeff`，其中 `coeff = round(32768·k)` 是定点化的修正系数。
3. 饱和到 16 位，输出 **偏移二进制** 给 DAC。
4. 另一路 `data_out1` 把原始样本（不插值）直通，这样原始样本与中点交替 → 数据率翻倍，喂满 DDR DAC。

#### 4.4.3 源码精读

顶部注释把「圆上中点」的概念和系数讲得很清楚：

[dsp/afterburner.v:9-12](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/afterburner.v#L9-L12) — 概念说明：\(k = 0.5\sec(\theta)\)。

[dsp/afterburner.v:23-32](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/afterburner.v#L23-L32) — 系数计算示例：`coeff = floor(32768*0.5*sec(pi*num/den)+0.5)`，并给出 55MHz 输出 @70MHz 时钟等具体数值。

插值流水线（注意有 `triple` 参数选择相加的是 `data1` 还是 `data3`，对应不同速率比的配置）：

[dsp/afterburner.v:34-47](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/afterburner.v#L34-L47) — `avg <= data + (triple ? data3 : data1); prod <= avg * coeff; sat <= 饱和(prod[31:16]);`。`prod` 右移 16 位即除以 32768，把定点系数还原。

输出为偏移二进制（翻转 MSB）：

[dsp/afterburner.v:50-51](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/afterburner.v#L50-L51) — `data_out0 = {~sat[15], sat[14:0]}`（插值中点），`data_out1 = {~thru[16], thru[15:1]}`（原始样本）。两路交替送 DAC 实现 DDR。

#### 4.4.4 代码实践（可运行）

`afterburner` 有自带的自校验测试台，会算一个傅里叶系数来判定插值是否正确。

1. **目标**：跑通 `afterburner_check`，观察割线插值对单频正弦的重建。
2. **步骤**：在仓库根目录执行 `make -C dsp afterburner_check`（若未配置 iverilog 会报缺工具，见 u1-l2）。
3. **观察**：测试台 `afterburner_tb.v` 顶部 `define AFTERBURNER_COEFF (18185)`，对应 `floor(32768*0.5*sec(2*pi*1/7/2)+0.5)`，即 num=1、den=7 的相位比。它会对输出做傅里叶分析比对期望正弦。
4. **预期结果**：终端打印 `PASS`。
5. **拓展**：用 `make -C dsp afterburner.vcd VFLAGS_afterburner_tb=+vcd`（参考 u2-l1 的按目标定制）生成波形，用 gtkwave 打开 `afterburner.gtkw`（如有）看 `data_out0`（中点）与 `data_out1`（原始）如何交替。若无法运行，待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：如果两相邻样本相位差 \(\phi \to 0\)，`coeff` 应该是多少？
**答案**：\(\sec(0)=1\)，所以 \(k=0.5\)，`coeff = round(32768·0.5)=16384`。此时圆上中点退化成算术平均。

**练习 2**：为什么 `afterburner` 输出偏移二进制而不是二补码？
**答案**：DAC 直接吃偏移二进制，省掉一次格式转换；需要二补码的上层（如 `ssb_out`）再翻 MSB 转回去。

### 4.5 上变频 `ssb_out`：把积木拼成 Hartley SSB 调制器

#### 4.5.1 概念说明

`ssb_out`（Single Side Band Out）是上变频的**顶层集成**。它的目标是：把反馈环路输出的**半速率交织 IQ 基带**，搬回本振频率，变成能驱动 DDR DAC 的实信号。

它用 **Hartley 调制器** 结构来选出一个边带（SSB），核心是调用两次 `flevel_set`：一次用原始 (I,Q)，一次用 90° 旋转后的 (−Q, I)，两者组合就能抵消掉一个边带、保留另一个。注释里写得很直白：

\[
(I, Q)\cdot(\cos\omega t, \sin\omega t) = I\cos\omega t + Q\sin\omega t
\]

#### 4.5.2 核心流程

完整链路（注意每级速率）：

1. **输入**：`drive` 是半速率交织 IQ（`div_state[0]` 标志 I/Q）。
2. **`fiq_interp`**：交织半速率 IQ → 全速率的 `drive_i`、`drive_q`（速率 X）。
3. **`flevel_set level1`**：点积 (I,Q)·(cosa,sina) → SSB 同相分量 `out1`（速率 X）。
4. **`afterburner1`**：在相邻样本间插中点 → DDR 速率（2X），喂 `dac1`。
5. **`flevel_set level2`**：喂入 90° 旋转 IQ（`i_data=~drive_q, q_data=drive_i`）→ SSB 正交分量 `out2`。
6. **`afterburner2`**：同样插中点 → DDR 速率，喂 `dac2`。
7. **偏移二进制 → 有符号**：翻转每个输出 MSB，把 `afterburner` 的偏移二进制转回二补码。

> 速率小结：`drive`（半速率 IQ）→ `fiq_interp`（全速率 I/Q）→ `flevel_set`（全速率实信号）→ `afterburner`（DDR 2 倍速）。`enable` 为 0 时输出强制清零；`ssb_flip` 可翻转 `dac2` 对的符号（单驱模式无用）。

#### 4.5.3 源码精读

顶部注释说明设计意图：依赖插值器把输出升采样到 DDR 以驱动双频 DAC，直接使用外部 LO（故输出 IF 由 LO 频率决定）。

[dsp/ssb_out.v:1-13](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/ssb_out.v#L1-L13) — SSB 设计说明与 Wikipedia 链接。

端口：`aftb_coeff` 是传给 `afterburner` 的割线修正系数，注释给了 FNAL 测试的算例（1313MHz LO、13MHz IF 时 `aftb_coeff=18646`）。

[dsp/ssb_out.v:15-35](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/ssb_out.v#L15-L35) — 模块声明。

实例 1：`fiq_interp` 把交织半速率 IQ 升到全速率。

[dsp/ssb_out.v:41-43](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/ssb_out.v#L41-L43) — `fiq_interp interp(...)`，注意 `.a_data(drive[17:2])`（取 `drive` 的高 16 位作 16 位 IQ 数据）、`.a_trig(iq)`。

实例 2：`level1` 算同相分量。

[dsp/ssb_out.v:51-55](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/ssb_out.v#L51-L55) — `flevel_set level1`，点积出 `out1`。

实例 3：`afterburner1` 升速到 DDR。

[dsp/ssb_out.v:60-62](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/ssb_out.v#L60-L62) — `afterburner afterburner1`，`.data({outk1,1'b0})` 把 16 位 `outk1` 拼成 17 位喂入。

实例 4：`level2` 用 90° 旋转 IQ 算正交分量。

[dsp/ssb_out.v:67-71](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/ssb_out.v#L67-L71) — 注意 `.i_data(~drive_q)`（Q 取反）`.q_data(drive_i)`，这就是 90° 旋转，配合 `level1` 实现边带选择。

实例 5：`afterburner2` 与格式转换。

[dsp/ssb_out.v:77-86](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/ssb_out.v#L77-L86) — `afterburner2` 同样升速；最后 4 行 `{~dac1_ob0[15], dac1_ob0[14:0]}` 把偏移二进制翻回有符号二补码输出。

#### 4.5.4 代码实践（可运行 + 端口验证）

这是本讲的主实践之一。`ssb_out` 有自带测试台 `ssb_out_tb.v`，由 `ssb_out_check` 驱动，但它**不是自校验**的——它 dump 出 `ssb_out.dat` 交由 Python 脚本 `ssb_drive_test.py` 验证。

1. **目标**：跑通 `ssb_out_check`，并验证端口连接（两个 `flevel_set` + 两个 `afterburner` + 一个 `fiq_interp`）。
2. **步骤**：
   - 先编译看端口是否齐全：`make -C dsp ssb_out_tb`（这会先生成 `cordicg_b22.v`，见 u3-l1）。
   - 再跑校验：`make -C dsp ssb_out_check`。该目标等价于（见 `dsp/rules.mk`）：先 `vvp ssb_out_tb +trace` 生成 `ssb_out.dat`，再用 `ssb_drive_test.py` 分析；然后 `+single` 模式再跑一次（单驱模式）。
3. **观察**：测试台 `ssb_out_tb.v` 用 `rot_dds`（`lo_amp=74840`）产生 LO，`div_state` 自增产生 `iq` 标志，`drive` 在 `cc>40` 后给 120000 当激励；`aftb_coeff` 取 `18646`。注意它打印：`WARNING: Not a self-checking testbench. Will always pass.`——所以**一定要让 Python 校验脚本跑完**才算真验证。
4. **预期结果**：`ssb_drive_test.py` 对 `ssb_out.dat` 做频谱/边带分析后不报错（待本地验证：取决于本地是否装了 numpy 等，见 u1-l2 的依赖分层）。
5. **端口连接验证**：对照 4.5.3 的 5 个实例，在 `ssb_out.v` 里数清楚：1 个 `fiq_interp`、2 个 `flevel_set`、2 个 `afterburner`，并确认 `level2` 喂的是 90° 旋转 IQ（`~drive_q`、`drive_i`）。

> 另一条可对照的上变频实现是 `dsp/upconv.v`（用 `doublediff` + `interp1`），其测试台是 `upconv_tb`，波形配置 `upconv.gtkw`。可用 `make -C dsp upconv_tb` 编译它做对比阅读。

#### 4.5.5 小练习与答案

**练习 1**：`ssb_out` 为什么要调用 `flevel_set` 两次，且第二次喂旋转后的 IQ？
**答案**：第一次 (I,Q)·(cos,sin) 得到一个边带分量；第二次喂 90° 旋转的 (−Q, I) 得到与之正交的另一分量；两路经 `afterburner` 后分别驱动 dac1/dac2，组合后抵消一个边带、保留另一个，实现单边带（SSB）。

**练习 2**：`ssb_out_tb` 不是自校验测试台，那它怎么算「通过」？
**答案**：它只 dump 数据到 `ssb_out.dat` 并恒打印 `PASS`；真正的功能校验由 `ssb_out_check` 调用的 `ssb_drive_test.py` 完成，所以必须跑 `_check` 目标而非仅跑 `_tb`。

## 5. 综合实践：画出完整 IQ 数据通路并标注速率

把本讲四块积木串起来，完成下面这个贯穿任务：

1. **画框图**：对照 `dsp/README.md` 与本讲 4.1–4.5，画出
   `ADC → fdownconvert → [基带反馈处理] → ssb_out → DDR DAC`
   的完整框图，把 `fiq_interp`、`flevel_set ×2`、`afterburner ×2` 都画进 `ssb_out` 内部。
2. **标注速率**：在每一级标上数据速率（设 ADC 速率为 X）：
   - ADC 实信号：X
   - `fdownconvert` 输出：交织 IQ，**半速率**（一对 IQ/2 拍），有效 IQ 速率 = X/2
   - 基带处理：X/2
   - `ssb_out` 输入 `drive`：交织半速率 IQ（X/2）
   - `fiq_interp` 输出 `drive_i/drive_q`：**全速率** X
   - `flevel_set` 输出：X
   - `afterburner` 输出：**DDR 2X**
   - DAC：2X（上下沿都更新）
3. **验证端口**：运行 `make -C dsp ssb_out_check`（见 4.5.4），确认上述实例化关系与外部 Python 校验通过。
4. **思考题**：为什么下变频选择「半速率 IQ」而上下链路又要用 `fiq_interp` 把它补回全速率？一句话答案：下变频用半速率省掉了显式低通（隐式抽取）；上变频必须把 IQ 补回全速率，才能让 `flevel_set` 在每个时钟都同时拿到 (I,Q) 做点积，并最终用 `afterburner` 升到 DDR 喂满 DAC。

> 若本地缺 iverilog/numpy 无法运行 `_check`，至少完成框图与速率标注，并把运行命令记录为「待本地验证」。

## 6. 本讲小结

- `fdownconvert` 用相邻两样本的矩阵运算一次性算出 (I,Q)，**隐式**完成了低通与抽取，输出**半速率交织 IQ**，位宽守恒（16 bits/sec 进出）。
- `fiq_interp` 把交织半速率 IQ 解复用并对每条流线性插值，还原成两条**全速率**的 I、Q（多 1 位增益）。
- `flevel_set` 是 LO 与 (I,Q) 的**点积**，即上变频的乘法核；其 `+1` 做舍入、`SAT` 做饱和、`sum2[16:1]` 抵消插值增益。
- `afterburner` 用割线系数 \(k=0.5\sec(\phi)\) 求「圆上中点」，把速率翻倍喂 DDR DAC，输出偏移二进制。
- `ssb_out` 把上述三块拼成 Hartley **单边带**调制器：两次 `flevel_set`（第二次喂 90° 旋转 IQ）选边带，两个 `afterburner` 升速到 DDR，最后翻 MSB 转回有符号。
- 整条链路处处体现「**速率与位宽守恒**」的设计哲学：用半速率换无低通、用插值补回全速率、用 DDR 翻倍喂 DAC。

## 7. 下一步学习建议

- **u3-l4（滤波器与抽取插值）**：本讲的「插值」是裸线性/割线插值；下一讲讲 CIC、半带、biquad/IIR 等正经的速率变换与抗混叠滤波器，会加深你对「为什么 `fdownconvert` 能省掉低通」的理解。
- **u4 单元（CDC 与片上互联）**：上变频输出最终要跨时钟域、经 localbus 配置；可接着读 `data_xdomain`（u4-l1）看 LO 与 DAC 跨域如何处理。
- **u6-l2（rtsim）与 u6-l3（cmoc）**：这条下变频→反馈→上变频链路在真实 LLRF 控制器 `cmoc` 里被实例化，被控对象由 `rtsim` 的腔体模型提供；学完本讲再去看 cmoc，就能把这套 DSP 链路放进系统语境。
- 继续阅读：`dsp/upconv.v`（早期上变频对照实现）、`dsp/lo_lut/`（LO 查找表）、以及 Larry Doolittle 的下变频数学笔记（源码注释链接）。
