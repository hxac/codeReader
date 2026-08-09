# CIC 原理与 filt_cicd 抽取滤波器

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 CIC（Cascade Integrator Comb，级联积分梳状）抽取滤波器「积分器 → 下采样 → 微分器」三段数据流的含义，以及为什么下采样可以插在中间。
- 读懂 `filt_cicd.v` 里用 `generate` 级联出的积分器与微分器结构，理解它们如何复用 `dff` 与 `shift_register` 原语。
- 解释环形计数器（ring counter）如何用一根线 `w_sclk` 产生「每 R 拍一次」的下采样脉冲，以及 `gp_phase` 如何选择抽取相位。
- 看懂输入端为什么要把数据符号扩展到 `gp_oup_width`，理解 CIC 内部位宽增长的处理方式。

本讲是单元 4（CIC 滤波器）的第一篇，承接单元 2 的定点数位宽推导（u2-l1）、`dff` 原语（u2-l2）与 `shift_register`/`upsample` 原语（u2-l3）。CIC 的位宽增长公式（Hogenauer）与黄金参考模型的细节会放到本单元 u4-l3 专讲，本讲只在需要时点一句，不展开。

## 2. 前置知识

### 2.1 为什么需要 CIC

在很多数字通信前端（如无线接收机的中频到基带）里，采样率要从几十 MHz 降到几百 kHz，抽取比（decimation factor）可能高达几十甚至上百。如果直接用一个长 FIR 去做这么大的抽取，乘法器数量会爆炸。CIC 滤波器由 Eugene Hogenauer 提出，它的最大特点是**完全不需要乘法器**——只用累加器（积分器）和差分器（梳状/微分器），因而面积小、速度高，常作为多级抽取的第一级（粗抽取），后面再接一个短 FIR 做补偿。

CIC 的代价是：通带不平坦、阻带衰减有限，所以它通常不单独使用，而是与补偿 FIR 配合。但作为「无乘法器的可综合抽取级」，它的硬件结构非常优雅。

### 2.2 单级 CIC 的传递函数

先看一级积分器（integrator）和一级梳状（comb）：

- 积分器是一个累加器，传递函数为：

\[ H_{\text{int}}(z) = \frac{1}{1-z^{-1}} \]

- 梳状是一个差分器，对相隔 \(RM\) 个（慢速）样本做差（\(R\) 是抽取比、\(M\) 是差分延迟 differential delay）：

\[ H_{\text{comb}}(z) = 1-z^{-RM} \]

两者级联，恰好是一个长度为 \(RM\) 的滑动平均（boxcar）：

\[ H(z) = \frac{1-z^{-RM}}{1-z^{-1}} = 1+z^{-1}+z^{-2}+\cdots+z^{-(RM-1)} \]

这个化简是 CIC 的灵魂：看上去一个带反馈的积分器和一个带前馈的梳状，合起来其实是个 FIR 滑动平均，**天然稳定**。级联 \(N\) 级就是把上式自乘 \(N\) 次：

\[ H_{\text{CIC}}(z) = \left(\frac{1-z^{-RM}}{1-z^{-1}}\right)^{N} \]

### 2.3 Hogenauer 的关键观察：下采样可以插在中间

理论上「先积分、再梳状」全程在高速采样率上算就行。但 Hogenauer 指出：由于梳状的差分只用到**每 R 个样本中的一个**（差分延迟 \(RM\) 是在抽取后的慢速域定义的），所以可以先把积分器跑在快时钟，**在下采样之后再做梳状**，让梳状也跑在慢时钟上。这样梳状的存储和运算量都降到 1/R。这就是 `filt_cicd` 的结构：

\[ \text{输入(快)} \xrightarrow{N\text{ 级积分器}} \boxed{\downarrow R} \xrightarrow{N\text{ 级梳状}} \text{输出(慢)} \]

一句话记忆：**积分器在快域累加，梳状在慢域差分，中间换速。**

### 2.4 你需要带进来的旧知识

- **`$signed`、符号扩展、`$clog2`**（u2-l1）：CIC 内部全程用补码定点，位宽随级数增长。
- **`dff` 原语**（u2-l2）：它就是一个带异步低有效复位、同步高有效使能的上升沿寄存器，改变 `i_clk`/`i_ena` 即可让它在快域或慢域工作。CIC 的积分器就是 `dff`。
- **`shift_register` 原语**（u2-l3）：用 `generate` 把若干个 `dff` 串成延迟线，CIC 的梳状差分延迟 \(z^{-M}\) 用它实现。
- **统一接口约定**（u1-l4）：`i_rst_an / i_ena / i_clk` 三件套、`gp_/c_/r_/w_` 前缀，本讲全程沿用。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [`.drl_src_code/filt_cicd/rtl/filt_cicd.v`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v) | CIC 抽取滤波器顶层，含环形计数器、积分器级联、下采样、梳状级联 |
| [`.drl_src_code/filt_cicd/rtl/dff.v`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/dff.v) | 全库寄存器原子原语，积分器级与下采样级都例化它 |
| [`.drl_src_code/filt_cicd/rtl/shift_register.v`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/shift_register.v) | 串行延迟线原语，梳状级用它实现差分延迟 \(z^{-M}\) |
| [`.drl_src_code/filt_cicd/octave/CICFilter.m`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/CICFilter.m) | 黄金参考模型（GRM），用 \((1+z^{-1}+\cdots+z^{-(RM-1)})^N\) 滤波后下采样 |
| [`.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv) | 测试台，把 `w_sclk` 引出为慢时钟 `s_clk`，在其上升沿逐样本比对 |

顶层 `filt_cicd.v` 只有约 150 行，但其数据流非常紧凑。建议先通读一遍 [filt_cicd.v:6-19](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v#L6-L19) 的参数表与端口，再按本讲 4.1～4.4 的顺序对照信号流阅读。

## 4. 核心概念与源码讲解

下面按**信号流经的顺序**拆成四个最小模块：先符号扩展（输入端），再积分器级联（快域），再环形计数器下采样（换速），最后梳状级联（慢域）。

---

### 4.1 MSB 符号扩展填充与统一位宽通路

#### 4.1.1 概念说明

CIC 的积分器是累加器，每级都会让数值「滚雪球」式增长（一个常数输入会让积分器线性增长、再上一级就二次增长……）。为了让 RTL 在任何参数下都不溢出、且与黄金参考模型逐比特一致（比特真，bit-true），Hogenauer 给出了一个**保守的输出位宽公式**（详见 u4-l3）：

\[ B_{\text{out}} = B_{\text{in}} + N\cdot\lceil\log_2(RM)\rceil \]

`filt_cicd` 把它直接写成 `gp_oup_width` 的默认表达式。关键设计决策是：**整个数据通路（积分器、下采样、梳状）全部用 `gp_oup_width` 这一种位宽**，不做中间截断。于是输入端的 `gp_inp_width` 位数据，必须先「补齐」到 `gp_oup_width` 位——补的方法就是符号扩展（sign extension）。

#### 4.1.2 核心流程

1. 计算需要补多少位：`c_fill_width = gp_oup_width - gp_inp_width`。
2. 取输入数据的符号位（最高位 `i_data[gp_inp_width-1]`）。
3. 把符号位复制 `c_fill_width` 份，拼到原数据高位前，得到 `gp_oup_width` 位的 `w_data`。
4. 之后所有加减法都在 `gp_oup_width` 上进行，结果自然不会溢出，也无需额外对齐。

#### 4.1.3 源码精读

派生位宽与填充宽度在常量声明区：

[`.drl_src_code/filt_cicd/rtl/filt_cicd.v:12`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v#L12) 把 Hogenauer 公式写成 `gp_oup_width` 的默认值（`gp_inp_width + gp_order*$clog2(gp_decimation_factor*gp_diff_delay)`），它是个**派生参数**，不在 `.param` 文件里出现。

[`.drl_src_code/filt_cicd/rtl/filt_cicd.v:22`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v#L22) 计算填充位数 `c_fill_width`。

符号扩展本体只有一行：

```verilog
assign w_data = {{c_fill_width{i_data[gp_inp_width-1]}}, i_data};
```

见 [filt_cicd.v:57-60](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v#L57-L60)。`{c_fill_width{...}}` 是复制运算符，把符号位复制 `c_fill_width` 份；负数补 1、正数补 0，数值不变而位宽变宽。这正是 u2-l1 讲过的 `{{fill{sign}}, data}` 模式。注意端口声明里 `i_data` 是 `wire signed`，而 `w_data` 也是 `wire signed`，后续加法还会用 `$signed()` 再声明一次有符号性，避免位拼接丢符号（拼接结果默认无符号）。

#### 4.1.4 代码实践

**目标**：亲手算出默认参数下的位宽，理解「输入 8 位、输出 14 位」是怎么来的。

1. 取默认参数 `gp_inp_width=8`、`gp_order=3`、`gp_decimation_factor=4`、`gp_diff_delay=1`。
2. 代入公式：`$clog2(4*1) = $clog2(4) = 2`，所以 `gp_oup_width = 8 + 3*2 = 14`，`c_fill_width = 14-8 = 6`。
3. 假设输入 `i_data = -1`（8 位补码 `8'b1111_1111`），手算 `w_data` 应为 14 位的 `14'b11_1111_1111_1111`（仍表示 -1）。
4. **待本地验证**：把 `filt_cicd.v` 单独用 `iverilog -g2012` elaborate（不一定要跑仿真），在初步网表里确认 `gp_oup_width` 推导为 14；或直接在测试台临时加 `$display("W=%0d", `P_OUP_DATA_W)` 观察 `defines.sv` 里写入的值。

#### 4.1.5 小练习与答案

- **练习 1**：如果把 `gp_decimation_factor` 从 4 改成 8（其余不变），`gp_oup_width` 变成多少？
  - **答**：`$clog2(8*1)=3`，`gp_oup_width = 8 + 3*3 = 17`。抽取比越大，位宽增长越快。
- **练习 2**：为什么不能简单地用 `{8'b0, i_data}` 来补高位？
  - **答**：那样是零扩展，会把负数变成大正数；CIC 处理的是有符号补码，必须复制符号位（负数补 1）才能保持数值不变。

---

### 4.2 积分器级联

#### 4.2.1 概念说明

积分器（integrator）就是一个**累加器**：输出等于「当前输入 + 上一次的输出」。

\[ y[n] = y[n-1] + x[n] \quad\Leftrightarrow\quad H(z)=\frac{1}{1-z^{-1}} \]

它在硬件上是一个加法器加一个寄存器（反馈）。`filt_cicd` 把 `gp_order` 级（记作 \(N\)）积分器首尾相连：第 0 级吃输入 `w_data`，第 \(k\) 级吃第 \(k-1\) 级的加法结果。注意积分器全部跑在**快时钟 `i_clk`** 上、用全库统一的 `i_ena` 使能——它必须在每个输入样本上都更新，否则累加就错了。

#### 4.2.2 核心流程

积分器链（每级是一个累加器，寄存器反馈）：

```
w_data ──>(+)─[dff]─> I0 ──>(+)─[dff]─> I1 ──>(+)─[dff]─> I2 ─► (送给下采样)
            ▲              ▲              ▲
            └──────────────┴──────────────┘  (各自反馈到本级加法器)
```

- 第 0 级：`w_int_add[0] = $signed(w_data) + $signed(I0 的寄存器反馈)`，结果送入一个 `dff`，`dff` 输出就是 `I0`。
- 第 \(k\) 级（\(k>0\)）：`w_int_add[k] = $signed(w_int_add[k-1]) + $signed(Ik 的寄存器反馈)`。
- 所有 `dff` 都 `.i_clk(i_clk), .i_ena(i_ena)`——快域、每拍更新。

#### 4.2.3 源码精读

积分器用一个 `generate` 循环铺出 `gp_order` 级，见 [filt_cicd.v:62-95](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v#L62-L95)。其中第 0 级与第 1～N-1 级分开写，唯一区别是加法的「上一个被加数」来源：

- [filt_cicd.v:70](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v#L70)：第 0 级加 `w_data`（外部输入）。
- [filt_cicd.v:83](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v#L83)：其余级加上一级的 `w_int_add` 切片。

每级都例化同一个 `dff`，见 [filt_cicd.v:71-79](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v#L71-L79)（第 0 级）与 [filt_cicd.v:84-92](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v#L84-L92)（第 1～N-1 级）。注意 `.i_ena(i_ena)`——积分器跑在快域。

**两个值得拆解的写法：**

1. **打包向量存数组**：`r_int_dly` 是一根 `gp_order*gp_oup_width` 位的线，被当成「\(N\) 个寄存器拼在一起」。取第 \(i\) 级那一段用索引片段 `[(i+1)*gp_oup_width-1 -: gp_oup_width]`。`-:` 是「起始位 − 宽度」的定宽向下切片，等价于 `[(i+1)*W-1 : i*W]`，但在 `generate` 里用变量更安全。这是全库复用的「一根宽线模拟寄存器数组」技巧。
2. **加法结果也是打包的**：`w_int_add` 同样是 `gp_order*gp_oup_width` 位的组合线网，各级加法结果各自占一段。第 \(k\) 级的输入是第 \(k-1\) 级的 `w_int_add` 切片——也就是说级间走的是**加法器的组合输出**而不是寄存器输出，寄存器只在本级反馈路径上。这保证了一拍内整条链都能向前推进。

> 与 u2-l2 的联系：这里的 `dff` 就是那个「使能态下等价于 \(z^{-1}\)」的原子块。把它的 `i_data` 接成「输入 + 自身反馈」，它就从延迟单元变成了累加器。这就是「一个原语撑起整套库」在 CIC 里的具体体现。

#### 4.2.4 代码实践

**目标**：在草稿纸上感受「积分器把冲击滚成多项式增长」。设 \(N=3\)（三级积分器），输入为单位冲击 \(x=[1,0,0,\ldots]\)（先忽略抽取，只看积分器段）。

1. 第 0 级 \(I_0[n]\) 是 \(x\) 的前缀和：冲击后恒为 1，即 \(I_0=[1,1,1,1,\ldots]\)。
2. 第 1 级 \(I_1[n]\) 是 \(I_0\) 的前缀和：\(I_1=[1,2,3,4,5,\ldots]\)（线性增长）。
3. 第 2 级 \(I_2[n]\) 是 \(I_1\) 的前缀和：\(I_2=[1,3,6,10,15,\ldots]\)（三角数，二次增长）。
4. 观察：每多一级积分器，常数输入（这里冲击后等效直流=1）的稳态增长阶数提高一次。这正是 CIC 位宽必须随级数增长的直观原因。

**预期结果**：你应该能在纸上得到上表；级数越多、抽取比越大，后面梳状差分前的中间值越大，需要的位宽越宽。精确波形**待本地验证**（取决于下采样相位的对齐，见 4.3）。

#### 4.2.5 小练习与答案

- **练习 1**：为什么积分器必须用 `i_ena`（快域使能）而不是用 `w_sclk`（慢域脉冲）？
  - **答**：积分器要在**每一个输入样本**上累加，漏掉任何一个都会让前缀和错误。慢域脉冲每 R 拍才一次，会使积分器只累加 1/R 的样本，传递函数完全改变。
- **练习 2**：第 0 级与第 1～N-1 级的 `generate` 分支为什么要分开写？
  - **答**：只有第 0 级的加法被加数是外部输入 `w_data`；其余级被加数是上一级的 `w_int_add` 切片。分开写是为了让「链头」接外部、其余接前级。

---

### 4.3 环形计数器与下采样相位选择

#### 4.3.1 概念说明

下采样（downsampling）就是每 \(R\) 个快域样本里挑 1 个送给梳状。「挑哪一个」由相位 `gp_phase` 决定。`filt_cicd` 用一个**一位热码环形计数器（one-hot ring counter）**来同时实现「每 R 拍产生一个脉冲」和「相位选择」两件事，并把那根脉冲线命名为 `w_sclk`（slow clock）。

环形计数器的思想很简单：一个 R 位的寄存器，任意时刻只有 1 位是 1，这个「1」每个时钟向上移一位、到顶后绕回最低位，周期正好是 R。于是 `r_count[k]` 就是「每 R 拍高一次、且各自错开一拍」的脉冲集；`w_sclk = r_count[gp_phase]` 选其中一根，就是我们要的下采样使能。

#### 4.3.2 核心流程

以 \(R=4\)、`gp_phase=0` 为例，`r_count`（写成 `r_count[3] r_count[2] r_count[1] r_count[0]`）的状态演化：

| 拍数 | r_count | r_count[0]（=w_sclk，phase=0） |
|------|---------|-------------------------------|
| 复位 | 0000    | 0 |
| 1    | 0001    | 0（本拍仍为 0，下一拍才生效）|
| 2    | 0001    | 1 ← 下采样脉冲 |
| 3    | 0010    | 0 |
| 4    | 0100    | 0 |
| 5    | 1000    | 0 |
| 6    | 0001    | 1 ← 下采样脉冲 |
| …    | …       | 每 4 拍一次 |

（注：上表的「拍数」对齐与复位/使能的精确时序有关，见下方源码精读与「待本地验证」。）

随后用一个 `dff` 实现下采样本身：它的 `.i_ena(w_sclk)`，于是只有在 `w_sclk=1` 的那一拍，它才把最后一级积分器的输出锁存进 `r_comb_inp`。这就是「换速」的物理实现——同一个 `i_clk` 下，靠使能信号把快域值「采」到慢域。

#### 4.3.3 源码精读

环形计数器是一段独立的 `always` 块，见 [filt_cicd.v:36-55](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v#L36-L55)。复位时全 0；使能后：

```verilog
if (r_count=='d0)
  r_count[0] <= 1'b1;                       // 启动：点亮 bit0
else begin
  r_count[0]                       <= r_count[gp_decimation_factor-1];          // bit0 <= 最高位（绕回）
  r_count[gp_decimation_factor-1:1] <= r_count[gp_decimation_factor-2:0];      // 整体左移一位
end
```

这段是一个**循环左移**：唯一的「1」从 bit0 逐拍走向 bit(R-1)，到顶后由第一行 `r_count[0] <= r_count[R-1]` 绕回 bit0。所以 `r_count` 在 `0001→0010→0100→1000→0001` 之间循环（\(R=4\)），周期 4。任意 `r_count[k]` 都是占空比 1/R 的脉冲，彼此相位错开一拍。

下采样由两行完成：

```verilog
assign w_sclk = r_count[gp_phase];          // 选相位
dff #(.gp_data_width(gp_oup_width)) cicd_downsample (
  .i_rst_an(i_rst_an), .i_ena(w_sclk), .i_clk(i_clk),
  .i_data(w_int_add[gp_order*gp_oup_width-1 -: gp_oup_width]),  // 末级积分器输出
  .o_data(r_comb_inp) );
```

见 [filt_cicd.v:97-109](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v#L97-L109)。`.i_ena(w_sclk)` 是点睛之笔：把同一个 `dff` 原语的使能端接成「每 R 拍一次」，它就从普通寄存器变成了**下采样保持器**。改 `gp_phase` 就改了「挑 4 个里的哪一个」。

> **与测试台的联系**：测试台 [filt_cicd_tb.sv:47](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv#L47) 把这根线引出来当慢时钟用：`assign #1 s_clk = dut.w_sclk;`。激励在快时钟 `i_clk` 上升沿喂入，而响应比对在慢时钟 `s_clk` 上升沿进行——因为只有 `w_sclk=1` 那一拍才会产生一个新的有效输出。这正对应 u7-l1 要讲的「比特真验证节拍」。

#### 4.3.4 代码实践

**目标**：确认环形计数器的周期与脉冲位置。

1. 设 `gp_decimation_factor=4`、`gp_phase=0`。
2. 在草稿纸上按上面的状态表画出 `r_count[3:0]` 与 `w_sclk` 的前 8 拍波形。
3. 预期：`w_sclk` 应每 4 拍出现一个宽度为 1 拍的高脉冲。
4. 改 `gp_phase=2`，重画——此时 `w_sclk=r_count[2]`，脉冲整体后移两拍，但周期仍是 4。
5. **待本地验证**：用 `./dsp_rtl_lib.sh -s filt_cicd 1` 跑单测试用例，或在测试台加 `VCD` 宏导出波形，观察 `dut.r_count` 与 `dut.w_sclk` 是否与手画一致。

#### 4.3.5 小练习与答案

- **练习 1**：为什么用一个 R 位的 one-hot 计数器，而不是普通的 \(\lceil\log_2 R\rceil\) 位二进制计数器？
  - **答**：one-hot 的每一位直接就是一根「相位脉冲」，选相位只需一根多路选择线 `r_count[gp_phase]`，无需比较器；且脉冲天然单周期宽，正好做使能。代价是位宽随 R 线性增长，但 CIC 的 R 通常不大，可接受。
- **练习 2**：把 `gp_phase` 从 0 改成 3（\(R=4\)），输出序列的数值会变吗？
  - **答**：会。`gp_phase` 改变「挑哪一个样本」，相当于改变抽取相位，输出序列会整体时移。黄金参考模型 `CICFilter.m` 里 `downsample(..., R, P)` 的 `P` 就是用来对齐这个相位的，必须与 RTL 的 `gp_phase` 一致才能比特真。

---

### 4.4 微分器（梳状）级联

#### 4.4.1 概念说明

梳状（comb）级也叫微分器/差分器，它做的是：

\[ y[n] = x[n] - x[n-M] \quad\Leftrightarrow\quad H(z)=1-z^{-RM} \]

这里的 \(M\) 就是 `gp_diff_delay`（差分延迟），\(z^{-RM}\) 是在**慢域**（抽取之后）计算的，所以延迟量是 \(M\) 个慢域样本。`filt_cicd` 用 `shift_register` 原语（u2-l3）实现这条 \(z^{-M}\) 延迟线，再把「当前值 − 延迟值」作为本级输出。`gp_order` 级梳状首尾相连，与积分器级数对称。

关键点：**梳状跑在慢域**。它的 `shift_register` 使能端接的是 `r_count[gp_phase]`（即 `w_sclk`），每 R 拍才移位一次。这正是 2.3 节「下采样插在中间」的落地——梳状只需处理 1/R 的样本，存储和功耗都省。

#### 4.4.2 核心流程

梳状链（每级 = 一条 \(z^{-M}\) 延迟线 + 一个减法器）：

```
r_comb_inp ─┬─>(−)─> c0 ─┬─>(−)─> c1 ─┬─>(−)─> c2 = o_data
   ↑        │   ↑        │   ↑        │
   │      [sr M]        [sr M]       [sr M]   (各延迟 M 个慢域样本)
   └────────┘    └────────┘    └───────┘  (延迟线输出回到本级减法器)
```

- 第 0 级：`w_comb_diff[0] = $signed(r_comb_inp) - $signed(延迟线输出)`，延迟线输入是 `r_comb_inp`（下采样结果）。
- 第 \(k\) 级（\(k>0\)）：延迟线输入是上一级 `w_comb_diff[k-1]`，输出 `w_comb_diff[k] = w_comb_diff[k-1] - 延迟线输出`。
- 所有 `shift_register` 的 `.i_ena(r_count[gp_phase])`——慢域。

#### 4.4.3 源码精读

梳状段同样用 `generate` 铺 `gp_order` 级，见 [filt_cicd.v:111-149](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v#L111-L149)。第 0 级与第 1～N-1 级的区别仍是「延迟线输入来源」：

- [filt_cicd.v:119-130](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v#L119-L130)：第 0 级，延迟线 `.i_data(r_comb_inp)`，减法 `r_comb_inp - r_comb_dly[0]`。
- [filt_cicd.v:134-146](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v#L134-L146)：其余级，延迟线 `.i_data(w_comb_diff[k-1])`。

每级例化一个 `shift_register`，关键参数：

```verilog
shift_register #(
  .gp_data_width (gp_oup_width),
  .gp_nr_stages  (gp_diff_delay)   // = M，差分延迟
) CIC_COMB_SR (
  .i_ena (r_count[gp_phase]),      // 慢域使能！
  ...
  .o_shift_done ()                 // 本模块不用「填满」标志，悬空
);
```

两个要点：

1. **`.gp_nr_stages(gp_diff_delay)`**：把延迟线的级数设成 \(M\)。默认 \(M=1\) 时，`shift_register` 就是一级 `dff`，梳状退化为最简单的 `x[n]-x[n-1]`。
2. **`.i_ena(r_count[gp_phase])`**：梳状跑在慢域。注意它直接用 `r_count[gp_phase]`（与 `w_sclk` 同一根线），而不是 `w_sclk` 这个别名——二者等价，但源码这里写出了原始表达式，读代码时要意识到「这就是下采样脉冲」。

最终输出取最后一级梳状结果，见 [filt_cicd.v:151](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v#L151)：`assign o_data = w_comb_diff[gp_order*gp_oup_width-1 -: gp_oup_width];`。

> **与 u2-l3 的联系**：`shift_register` 在 u2-l3 里被描述为「用 `generate` 级联 `dff`、输出末级、并用计数器产生 `o_shift_done` 标志」。CIC 梳状只用到它的「\(z^{-M}\) 延迟」功能，所以 `o_shift_done` 端口悬空（不接）。而它的 `.i_ena` 被改接成慢域脉冲，于是这条延迟线只在慢速节拍上移位——这是原语复用的典型手法：同一个 `shift_register`，换使能就换了速率。

#### 4.4.4 代码实践

**目标**：完成「下采样 → 三级梳状」的手算，与本讲开头的脉冲追踪接上。

承接 4.2 的结果（\(N=3\)，单位冲击）：末级积分器输出 \(I_2=[1,3,6,10,15,21,28,36,45,55,66,78,91,\ldots]\)。按 \(R=4\)、`gp_phase=0` 下采样（取 \(n=0,4,8,12\)）得慢域序列 \(d=[1,15,45,91,\ldots]\)。现在做三级梳状（\(M=1\)，即 `d[n]-d[n-1]`，初值 \(d[-1]=0\)）：

| n | d[n] | c0=d−d₋ | c1=c0−c0₋ | c2=c1−c1₋（=o_data）|
|---|------|---------|-----------|---------------------|
| 0 | 1    | 1       | 1         | 1 |
| 1 | 15   | 14      | 13        | 12 |
| 2 | 45   | 30      | 16        | 3 |
| 3 | 91   | 46      | 16        | 0 |

**预期结果**：抽取后的脉冲响应为 `[1, 12, 3, 0, 0, ...]`。

**交叉验证**：把 CIC 看成「不抽取的 FIR」再抽取。单级滑动平均 `[1,1,1,1]` 自卷积三次（\(N=3, R=4, M=1\)）得不抽取脉冲响应 `[1,3,6,10,12,12,10,6,3,1]`；按相位 0 每 4 个取 1 个（下标 0,4,8）正好得到 `[1,12,3]`，与上面逐级差分的结果一致。这说明「积分(快)→下采样→梳状(慢)」与「整体 FIR→下采样」数学等价——也就是 Hogenauer 的可换序结论。

> 精确的 RTL 采样对齐（哪一拍正好对应相位 0）取决于环形计数器与复位/使能的时序细节，**待本地验证**；但稳态下输出序列应为 `[1, 12, 3]` 后跟 0。

#### 4.4.5 小练习与答案

- **练习 1**：把 `gp_diff_delay` 从 1 改成 2，梳状的传递函数怎么变？
  - **答**：延迟线变成 \(z^{-2}\)（慢域），单级梳状变成 \(1-z^{-2\cdot 4}=1-z^{-8}\)，滑动平均长度从 4 变成 8。阻带特性会改变（第一零点位置移动）。
- **练习 2**：梳状的 `shift_register` 为什么 `.i_ena` 接 `r_count[gp_phase]` 而不是常高？
  - **答**：梳状定义在慢域，差分延迟 \(RM\) 是按慢速样本数的。若常高使能，延迟线会在快域移位，每 R 拍移 R 次，差分就变成了快域的 \(z^{-M}\) 而非慢域的 \(z^{-RM}\)，传递函数完全错误。

---

## 5. 综合实践

**任务**：从参数到仿真，完整跑通一个 CIC 抽取滤波器，并把本讲四个模块串起来。

**步骤**：

1. **配参数**。复制参数模板并编辑：
   ```bash
   cp .drl_param/filt_cicd_1.param ./
   # 编辑 filt_cicd_1.param，设为：
   #   gp_decimation_factor = 4
   #   gp_order             = 3
   #   gp_diff_delay        = 1
   #   gp_phase             = 0
   #   gp_inp_width         = 8
   ```
2. **生成并回归仿真**：
   ```bash
   ./dsp_rtl_lib.sh -d filt_cicd_1.param
   ```
   该命令会：复制 RTL 模板 → `sed` 注入参数 → Octave 跑 `stimuli.m` 生成激励/响应/`defines.sv` → 对 9 个测试用例逐个编译仿真（详见 u1-l3）。
3. **手算交叉验证**。对测试用例 1（[stimuli.m:21-27](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/stimuli.m#L21-L27) 是稀疏脉冲序列）中的某个单位脉冲，按 4.2～4.4 的方法手算「积分→下采样→梳状」的前几个输出，确认其形态与 `[1,12,3]` 这类「有限长冲击响应」一致。
4. **改相位观察**。把 `gp_phase` 改成 2 重新生成，观察输出序列是否整体时移（数值集合不变、时间位置移动），印证 4.3 关于相位选择的结论。
5. **看位宽**。在生成的 `defines.sv` 里找到 `P_OUP_DATA_W`，确认它等于 \(8+3\times\lceil\log_2 4\rceil=14\)，印证 4.1 的位宽推导。

**需要观察的现象**：

- 回归仿真每个测试用例应打印 `### INFO: Testcase PASSED`（`error_count==0`），即 RTL 与 GRM 逐样本比特一致。
- 改 `gp_phase` 后输出序列时移但回归仍应 PASSED（因为 GRM 的 `downsample(...,R,P)` 用同一个 `P` 对齐）。
- 改 `gp_decimation_factor` 后 `P_OUP_DATA_W` 随之变化。

**如果无法运行**：若环境没有 iverilog 或 Octave，脚本会以退出码 1 或 10 报错（见 u1-l3）。此时可退化为「源码阅读型实践」：只读 [filt_cicd.v](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/rtl/filt_cicd.v)，沿 `w_data → w_int_add → r_comb_inp → w_comb_diff → o_data` 画出完整数据流图，并标注每一段的位宽与使能时钟（快/慢）。

## 6. 本讲小结

- CIC 抽取器 = 「\(N\) 级积分器（快域）→ \(\downarrow R\) → \(N\) 级梳状（慢域）」，中间换速是 Hogenauer 的核心观察，让梳状只需处理 1/R 的样本。
- 积分器是累加器 \(1/(1-z^{-1})\)，用 `dff` 原语加反馈实现，全部跑在快时钟；梳状是差分器 \(1-z^{-RM}\)，用 `shift_register` 原语做 \(z^{-M}\) 延迟，使能接慢域脉冲。
- 下采样用一个 one-hot **环形计数器**实现：每一位是一根相位脉冲，`w_sclk = r_count[gp_phase]` 选相位，再用一个 `.i_ena(w_sclk)` 的 `dff` 把快域值采进慢域。
- 全通路统一用 `gp_oup_width` 位宽，输入端靠 MSB 符号扩展补齐，保证中间不截断、与 GRM 比特真。
- 模块大量复用单元 2 的原语：`dff`（积分器、下采样）、`shift_register`（梳状延迟线），靠改 `i_ena` 切换快/慢域。
- 手算验证：\(R=4,N=3,M=1\) 时单位冲击的抽取响应为 `[1, 12, 3]`，与「FIR 整体响应再抽取」的结果一致。

## 7. 下一步学习建议

- **u4-l2（filt_cici 插值）**：看 CIC 反过来用——先梳状（慢域）→ 上采样 → 积分器（快域），并引入 `upsample` 原语与双时钟域 `i_clk/i_fclk`。本讲的积分器/梳状结构与「换速」思想会原样复用，只是顺序颠倒。
- **u4-l3（CIC 位宽与 GRM）**：深入 Hogenauer 位宽公式的推导、增益缩放因子 `SF` 的含义，以及 [CICFilter.m](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/octave/CICFilter.m) 如何用 \((1+z^{-1}+\cdots+z^{-(RM-1)})^N\) 产出比特真响应。本讲只用了公式结论，那里讲清「为什么是这个公式」。
- **继续阅读**：对照 [filt_cicd_tb.sv](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_cicd/sim/testbench/filt_cicd_tb.sv) 的 `s_clk` 节拍，提前感受 u7-l1 的比特真验证闭环。
