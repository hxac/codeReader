# CIC、FIR/IIR 滤波器与抽取插值

## 1. 本讲目标

上一讲（u3-l3）我们把射频数据通路从下变频拼到了上变频，那条链路里其实已经悄悄用到了「抽取」与「插值」。本讲换一个视角，专门讲 Bedrock `dsp/` 子系统里负责**滤波与速率变换**的几类基础积木。读完本讲你应当能够：

- 说清楚 **CIC（级联积分梳状）滤波器**为什么不需要乘法器、位宽为什么会随抽取倍数增长，并能读懂 `cic_simple_s`、`cic_interp`、`cic_multichannel` 三种实现。
- 理解 **半带（half-band）FIR 滤波器** `half_filt` 如何利用对称性做到「无乘法器 + 抽取两倍」。
- 掌握 **biquad 二阶节 IIR** 的差分方程、系数加载协议，以及它如何用一个乘累加（MAC）单元分时复用完成 5 次乘法。
- 理解 **`iirFilter` 如何把多个 biquad 串成高阶 IIR**，以及 `fifo` / `circle_buf` / `dpram` 这些存储原语在 DSP 流水里的缓冲作用。

本讲只聚焦「滤波与速率变换」这一主题，不涉及控制环路（那是 cmoc 的事）。

## 2. 前置知识

### 2.1 FIR 与 IIR

- **FIR（有限长单位冲激响应）**：输出只由当前和过去若干输入决定，\( y[n]=\sum_{k=0}^{N-1} h_k\,x[n-k] \)。它天然稳定、可以做线性相位，代价是阶数高、乘法多。本讲的 `half_filt` 是 FIR。
- **IIR（无限长单位冲激响应）**：输出还依赖过去的输出，\( y[n]=\sum b_k x[n-k]-\sum a_k y[n-k] \)。同样陡的过渡带，IIR 需要的阶数远低于 FIR，但要小心稳定性与相位。本讲的 `lpass1`、`biquad`、`iirFilter` 都是 IIR。

### 2.2 抽取与插值

- **抽取（decimation）**：先低通滤波再降采样，把速率从 \( f_s \) 降到 \( f_s/R \)。
- **插值（interpolation）**：先升采样（插零）再低通滤波，把速率从 \( f_s \) 升到 \( L\cdot f_s \)。
- CIC 滤波器把「积分/梳状 + 抽取/插值」合并，且**完全不需要乘法器**，所以常被放在抽取/插值链的最前/最后一级，做粗粒度的大倍率速率变换。

### 2.3 定点与位宽增长

滤波会改变数据动态范围。CIC 的直流增益随抽取倍数指数增长，因此内部累加器必须预留额外的「增长位」，否则会溢出。本讲会反复出现「位宽 = 数据位 + 增长位」的设计套路。

### 2.4 valid/ready 握手

`biquad` 与 `iirFilter` 使用一对 `S_TVALID/S_TREADY`（输入）和 `M_TVALID/M_TREADY`（输出）信号做反压握手，这是 Xilinx AXI-Stream 风格的约定：当 `VALID` 与 `READY` 同时为高时，一个数据真正被搬运。本讲读到这些端口时按此理解即可。

## 3. 本讲源码地图

本讲涉及的关键文件如下（均在 `dsp/` 目录下）：

| 文件 | 作用 |
| --- | --- |
| `cic_simple_s.v` | 一阶 CIC 抽取器（有符号） |
| `cic_simple_us.v` | 一阶 CIC 抽取器（无符号） |
| `cic_interp.v` | 一阶 CIC 插值器 |
| `cic_multichannel.v` | 多通道二阶 CIC（积分器 + 串行化 + 梳状 + 后级滤波） |
| `double_inte_smp.v` | 二阶积分器（被 `cic_multichannel` 每通道实例化） |
| `ccfilt.v` | 级联微分器 + 桶形移位 + 可选半带后滤波 |
| `doublediff.v` | 二阶梳状（被 `ccfilt` 实例化） |
| `half_filt.v` | 无乘法器半带 FIR + 抽取两倍 |
| `biquad.v` | 二阶节 IIR（单 MAC 分时复用） |
| `iirFilter.v` | 把多个 `biquad` 串成高阶 IIR |
| `lpass1.v` | 最简单的一阶 IIR 低通（RC 低通） |
| `fifo.v` / `circle_buf.v` / `dpram.v` / `reg_delay.v` | 流水线里的存储/延迟原语 |
| `rules.mk` | `dsp/` 的测试台清单与自定义 make 规则 |

> 提示：这些模块大多有对应的 `*_tb.v` 测试台，统一通过 `make -C dsp <模块>_check` 跑仿真自校验（参见 u2-l1 讲过的 `%_check` 模式规则）。

## 4. 核心概念与源码讲解

### 4.1 CIC 抽取/插值滤波器

#### 4.1.1 概念说明

CIC（Cascaded Integrator–Comb）是 Hogenauer 提出的一类**无乘法器**的线性相位滤波器，专门用于大倍率的抽取/插值。它的核心观察是：

> 把「积分」放在高速率侧，把「梳状（差分）」放在低速率侧，中间夹一个降/升采样，二者在数学上可以合并，从而省掉所有乘法器。

一阶 CIC 抽取器的结构是：

```
x[n] --► 积分器 ∑ --► (↓R) --► 梳状 (1 - z^(-1)) --► y[m]
        (高速 fs)            (低速 fs/R)
```

- 积分器：\( I[n]=I[n-1]+x[n] \)，就是一个不断累加的寄存器。
- 梳状（差分）：\( y[m]=w[m]-w[m-1] \)，减去「一个抽取周期前」的值。

它等价于一个**滑动平均**：连续 \( R \) 个样本求和后再差分，直流增益为 \( R \)。因此一阶 CIC 的位增长是 \( \log_2 R \) 位。若把积分器/梳状各堆 \( N \) 级，就得到 \( N \) 阶 CIC，直流增益变成 \( R^N \)，位增长 \( N\log_2 R \) 位。这就是 CIC 设计中最重要的位宽公式。

#### 4.1.2 核心流程

一阶 CIC 抽取器（`cic_simple_s`）的执行流程：

1. 每来一个有效输入（`data_in_gate` 为高），积分器 `data_int` 累加一次。
2. 一个 `ex` 位宽的计数器 `div` 同时计数，每计满 \( 2^{ex} \) 次产生一个 `roll` 脉冲——这就是抽取时刻 \( R=2^{ex} \)。
3. 在 `roll` 时刻，梳状做差分：`diff = data_int - data_int_h`，并把当前积分值存进 `data_int_h` 供下次差分。
4. 输出取 `diff` 的高 `dw` 位（右移 `ex` 位），抵消掉 CIC 的 \( 2^{ex} \) 倍增益。

二阶多通道版本（`cic_multichannel`）的流程多了一层：

1. 每个通道各自跑一个**二阶积分器** `double_inte_smp`（\( N=2 \)），结果比输入多 \( N\log_2 R \) 位。
2. `serializer_multichannel` 在 `cic_sample` 时刻把所有通道的积分结果**串行化**成一条移位链（一条「传送带」）。
3. `ccfilt` 在低速率侧对这条传送带做**二阶梳状** `doublediff`，再用桶形移位 `cc_shift` 调整定标，最后可选地接一个半带滤波器 `half_filt` 做进一步抽取。

#### 4.1.3 源码精读

**一阶抽取器 `cic_simple_s.v`**——先看端口与参数，抽取倍数由 `ex` 决定（\( R=2^{ex} \)）：

[dsp/cic_simple_s.v:3-14](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/cic_simple_s.v#L3-L14) — 模块声明，参数 `ext_roll`（用外部 roll 还是内部分频）、`dw`、`ex`。

积分器和梳状寄存器都开成 `dw+ex` 位，多出的 `ex` 位正是为了吸收 \( 2^{ex} \) 倍的 CIC 增益：

[dsp/cic_simple_s.v:16-17](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/cic_simple_s.v#L16-L17) — `data_int/data_int_h/diff` 为 `dw+ex` 位，`div` 为 `ex` 位计数器。

积分器在每个有效样本上累加，同时计数器自增并在最高位产生 `iroll`（每 \( 2^{ex} \) 拍翻转）：

[dsp/cic_simple_s.v:19-24](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/cic_simple_s.v#L19-L24) — `data_int <= data_int + data_in`；`{iroll, div} <= div + 1`；`uroll` 在内/外 roll 间选择。

梳状（差分）只在 `roll` 时刻触发，做「当前积分值 − 上一次 roll 时的积分值」：

[dsp/cic_simple_s.v:31-38](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/cic_simple_s.v#L31-L38) — `diff <= data_int - data_int_h`；输出 `data_out = diff[dw+ex-1:ex]`（右移 `ex` 位归一化）。

> `cic_simple_us.v` 与 `cic_simple_s.v` 逐行对应，唯一区别是全部改为**无符号**算术（`data_in` 零扩展而非符号扩展），见 [dsp/cic_simple_us.v:18-25](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/cic_simple_us.v#L18-L25)。选哪一个取决于上游数据是否带符号。

**插值器 `cic_interp.v`** 是镜像结构：梳状跑在低速率（`strobe` 时刻做差分），积分器跑在高速（每拍累加），插值倍数 \( L=2^{\text{span}} \)：

[dsp/cic_interp.v:17-31](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/cic_interp.v#L17-L31) — `span=6` 决定插值倍数；`diff <= d_in - d_last`；`i1 <= i1+diff`；输出取 `i1[17+span:span]`。

**多通道二阶 CIC `cic_multichannel.v`** 的数据流画在了文件头部的 ASCII 图里，值得一看：

[dsp/cic_multichannel.v:14-30](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/cic_multichannel.v#L14-L30) — 每通道一个双积分器（DI），经串行化器（SERIAL）汇成传送带，再进级联梳状 + 后级滤波（CCFILT）。

参数里的位宽关系注释把 CIC 位增长公式写得明明白白：

[dsp/cic_multichannel.v:36-41](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/cic_multichannel.v#L36-L41) — `di_rwi`（结果位宽）应比 `di_dwi`（输入位宽）多 \( N\log_2(\text{每 CIC 周期最大样本数}) \)，其中 \( N=2 \) 是 CIC 阶数。

`generate` 循环为每个通道实例化一个二阶积分器：

[dsp/cic_multichannel.v:84-97](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/cic_multichannel.v#L84-L97) — 每通道一个 `double_inte_smp`，把拍平的 `d_in` 按通道切片喂进去。

二阶积分器 `double_inte_smp.v` 就是两个串联的累加器 `int1`、`int2`：

[dsp/double_inte_smp.v:25-39](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/double_inte_smp.v#L25-L39) — `int1 <= int1 + in; int2 <= int2 + int1;`，只在 `stb_in` 选通时累加（支持低于线速率运行）。

串行化器把所有通道结果汇成一条传送带：

[dsp/cic_multichannel.v:106-115](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/cic_multichannel.v#L106-L115) — `serializer_multichannel` 在 `cic_sample` 时把各通道 DI 输出采样进移位链。

低速率侧的级联梳状 + 桶形移位 + 可选半带都在 `ccfilt.v` 里：

[dsp/ccfilt.v:35-36](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/ccfilt.v#L35-L36) — 实例化二阶梳状 `doublediff`。
[dsp/ccfilt.v:50-57](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/ccfilt.v#L50-L57) — 桶形右移 `d2e >>> full_shift` 做运行时定标，并做溢出检测。
[dsp/ccfilt.v:92-110](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/ccfilt.v#L92-L110) — `use_hb=1` 时接一个 `half_filt` 做进一步抽取（本讲 4.2 的主角）。

二阶梳状 `doublediff.v` 用 `reg_delay` 实现差分延迟 `dsr_len`：

[dsp/doublediff.v:19-32](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/doublediff.v#L19-L32) — `d1 <= d_in - dpass1; d2 <= d1 - dpass2;`，延迟长度由 `dsr_len` 决定（在 `cic_multichannel` 里被设成 `n_chan`，因为传送带上「同一通道上一周期的值」相隔正好 `n_chan` 个位置）。

#### 4.1.4 代码实践

1. **实践目标**：跑通多通道 CIC 的仿真自校验，并理解一阶 CIC 的差分延迟由谁决定。
2. **操作步骤**：
   - 在仓库根目录执行 `make -C dsp cic_multichannel_check`，观察 testbench 打印的 `amp`、`phs` 列与最终的 `PASS`/`FAIL`。
   - 打开 [dsp/cic_simple_s_tb.py](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/cic_simple_s_tb.py)（cocotb 测试），看它如何生成三音正弦、可选加噪、逐拍喂入并在 `data_out_gate` 有效时收集输出。
3. **需要观察的现象**：`cic_multichannel_check` 应打印每个抽取周期的复幅度/相位并与期望比对；`cic_simple_s_tb.py` 中 `CIC_DECIMATION = 2**5`（即 `ex=5`，抽取 32 倍）。
4. **预期结果**：仿真自校验打印 `PASS`（**待本地验证**，取决于是否装好 iverilog 与 cocotb）。
5. **关键结论**：一阶 CIC 的差分延迟寄存器 `data_int_h` 只有**一个**（差分延迟 \( M=1 \)），但它「回看」的输入样本数 = 抽取倍数 \( 2^{ex} \)，由参数 **`ex`** 决定；相应地，积分/梳状寄存器的位宽 `dw+ex` 也由 `ex` 决定（吸收 \( 2^{ex} \) 倍增益）。在二阶多通道版本里，位增长变为 \( N\log_2 R \)，由 **CIC 阶数 \( N \)**（级联积分器个数）与抽取率共同决定。

#### 4.1.5 小练习与答案

- **练习 1**：把 `cic_simple_s` 的 `ex` 从 5 改成 8，积分器寄存器位宽会变成多少？输出归一化右移几位？
  - **答**：寄存器变成 `dw+8` 位；输出 `diff[dw+8-1:8]`，右移 8 位（抽取 256 倍）。
- **练习 2**：为什么 `cic_simple_us` 用零扩展而 `cic_simple_s` 用符号扩展？
  - **答**：前者处理无符号数据（如某些 ADC 原始码），后者处理带符号的补码；符号扩展保证负数累加正确，零扩展保证无符号数不溢出符号位。

---

### 4.2 半带 FIR 滤波器与抽取（half_filt）

#### 4.2.1 概念说明

**半带滤波器（half-band filter）**是一类特殊的 FIR：通带、阻带关于 \( f_s/4 \) 对称，过渡带中心恰在 \( f_s/4 \)，且**几乎所有奇数位置的抽头都为 0**，中心抽头固定为 0.5（归一化）。这带来两个巨大好处：

1. 一半的乘法直接省掉（系数为 0）。
2. 它天然适合**抽取两倍**：把速率从 \( f_s \) 降到 \( f_s/2 \)，过渡带刚好落在不会被混叠的区间。

Bedrock 的 `half_filt` 更进一步：仅有的几个非零系数也被选成「2 的幂次的和/差」（移位 + 加法即可实现），所以它**完全不需要乘法器**，在小 FPGA 上也能跑到很高频率。

#### 4.2.2 核心流程

`half_filt` 是一个 11 抽头（tap）对称 FIR，抽头为：

\[ [2,\ 0,\ -9,\ 0,\ 39,\ 64,\ 39,\ 0,\ -9,\ 0,\ 2] \]

非零抽头只有 7 个（端点 2、−9、39 各一对，中心 64）。它的工作流程：

1. 用一串 `reg_delay` 把输入延时出 11 个版本 `d0..d10`（每个延时 `len` 拍，支持 `len` 路交织数据流）。
2. 利用对称性，把「成对」的抽头先相加：`s1=d0+d10`、`s2=d2+d8`、`s3=d4+d6`、`s4=d5`（中心抽头），把 11 次乘法压成 4 个等效系数。
3. 用**移位 + 加法**实现这 4 个系数（`a1..a4`），无需乘法器。
4. 合并、饱和相加（`sat_add`），再用 `samp/show` 计数器**抽取两倍**输出。

#### 4.2.3 源码精读

文件头把抽头、增益、群延迟讲得很清楚，是本模块最好的入门资料：

[dsp/half_filt.v:1-18](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/half_filt.v#L1-L18) — 列出 11 个抽头、标称直流增益为 1（峰值 +0.074 dB）、线性相位、群延迟 5 个样本。

延时链生成 11 个抽头数据（`len=4` 表示支持 4 路交织流，延时单位是 `len` 拍）：

[dsp/half_filt.v:43-56](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/half_filt.v#L43-L56) — `parameter len=4`；一串 `reg_delay #(.dw(20),.len(len))` 产出 `d0..d10`。

利用对称性先做「成对求和」，把 11 抽头压成 4 个部分和：

[dsp/half_filt.v:58-70](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/half_filt.v#L58-L70) — `s1<=d0+d10; s2<=d2+d8; s3<=d4+d6; s4<=d5;`。

「无乘法器」的关键：用移位与加减实现 4 个系数，注释里给出了它们的移位分解：

[dsp/half_filt.v:73-95](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/half_filt.v#L73-L95) — 例如 `a2 <= s2/4 + s2/32`（即系数 −9 的移位分解）、`a3 <= s3/4 - s3/32`；`TRUST_VERILOG_DIVISION` 宏分支用显式符号扩展移位代替除法，便于综合。

抽取两倍由 `samp/show` 计数器完成：每 `len` 拍翻转一次 `show`，使 `outg` 周期性有效/静默：

[dsp/half_filt.v:97-126](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/half_filt.v#L97-L126) — 合并 `b1/b2`、`sat_add` 饱和相加、`outg = cg = bg & show` 实现抽取两倍。

> 顺带认识两个被它调用的原语：`reg_delay` 是「\( z^{-n} \)」延时（[dsp/reg_delay.v:5-38](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/reg_delay.v#L5-L38)，在 Xilinx 上希望被推断成 SRL16 移位寄存器）；`sat_add` 是饱和加法（[dsp/sat_add.v:13-18](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/sat_add.v#L13-L18)）。

#### 4.2.4 代码实践

1. **实践目标**：确认半带滤波器的校验流程，并理解「抽取两倍 + 无乘法器」。
2. **操作步骤**：
   - 执行 `make -C dsp half_filt_check`（自定义规则见 [dsp/rules.mk:47-48](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/rules.mk#L47-L48)，由 `half_filt.py` 读波形数据 `half_filt.dat` 做幅度/噪声校验）。
   - 阅读 [dsp/half_filt_tb.v:30-37](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/half_filt_tb.v#L30-L37)，看激励如何用 `ing` 门控每 `per` 周期里的 `len` 个有效样本。
3. **需要观察的现象**：`half_filt.py` 打印的实际幅度应约 200000、标准差接近理论值（见 [dsp/half_filt.py:13-31](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/half_filt.py#L13-L31)）。
4. **预期结果**：校验脚本打印 `PASS`（**待本地验证**）。

#### 4.2.5 小练习与答案

- **练习 1**：为什么半带滤波器特别适合「抽取两倍」？
  - **答**：它的过渡带中心在 \( f_s/4 \)，抽取两倍后 \( f_s/2 \) 的新奈奎斯特频率恰好落在阻带里，混叠只会把阻带能量折叠回阻带，不影响通带。
- **练习 2**：`half_filt` 里没有任何 `*` 乘号，那它怎么做 FIR？
  - **答**：所有系数都是 2 的幂次的和/差，用 `>>`、符号扩展拼接和加减实现，所以叫「multiplier-free」。

---

### 4.3 biquad 二阶节 IIR

#### 4.3.1 概念说明

**biquad（二阶节）**是 IIR 滤波器的「原子单位」：任何高阶 IIR 都可以拆成若干个二阶节级联（避免高阶直接型系数灵敏度太高的毛病）。一个 biquad 的差分方程是：

\[ y(t)=b_0\,u(t)+b_1\,u(t{-}1)+b_2\,u(t{-}2)-a_1\,y(t{-}1)-a_2\,y(t{-}2) \]

Bedrock 的 `biquad` 模块有几个工程化巧思：

- **单 MAC 分时复用**：5 个乘积全部用**一个** `macc`（乘累加）单元，靠一个 6 状态有限状态机分时完成，省 DSP 片。
- **跨时钟域系数加载**：系数由 `sysClk` 写入，数据流跑在 `dataClk`，二者异步，用 `reg_tech_cdc` 做复位同步。
- **AXI-Stream 风格握手**：`S_TVALID/S_TREADY/M_TVALID/M_TREADY`，可无缝对接 Xilinx 工具链。
- **原子性**：写系数期间滤波器保持复位，直到写地址 7 才解除，保证使用一组一致的系数。

#### 4.3.2 核心流程

1. **加载系数**：在 `sysClk` 下按地址 0–4 依次写 `b0,b1,b2,-a2,-a1`（注意 RAM 里存的是已经取负的 `−a1,−a2`，这样累加器只需做加法）；写地址 7 解除复位。
2. **跨域**：复位信号经 `reg_tech_cdc` 搬到 `dataClk`。
3. **计算**：状态机 `state` 在 0–5 之间循环：
   - state 0：等输入握手（`S_TVALID&S_TREADY`），锁存 `u`；
   - state 1–5：用一个 `macc`，每拍换一组乘数（靠 `parameterMux` 选择 `u/uOld/yOld` 与对应系数），把 5 个乘积累加成一个输出；
   - state 5：握手输出（`M_TVALID&M_TREADY`），回到 state 0。
4. **截断**：累加结果经 `reduceWidth` 饱和截位回 `DATA_WIDTH`。

#### 4.3.3 源码精读

文件头给出了差分方程和设计意图（最小化延迟、复用 DSP 寄存器、原子系数更新）：

[dsp/biquad.v:1-12](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/biquad.v#L1-L12) — 差分方程注释；「Writing a coefficient holds the filter in reset until address 7 is written」。

系数双口 RAM 的地址编排——注意 `−a1/−a2` 已经取负存入：

[dsp/biquad.v:36-49](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/biquad.v#L36-L49) — `coefficientRAM`：`0:b0, 1:b1, 2:b2, 3:-a2, 4:-a1`，系数范围 \([-2,2)\)（小数点左边 2 位）；写地址 7 时 `sysReset<=0`。

输入/输出历史与 MAC 输入多路选择：

[dsp/biquad.v:52-59](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/biquad.v#L52-L59) — `u/uOld/yOld` 保存 \( u(t),u(t{-}1),y(t{-}1) \)；`parameterMux` 按 `state` 选出当前要乘的数据。

状态机的核心几拍（注释说明了「提前一拍算好除 \( b_0 \) 以外的项」的重叠技巧）：

[dsp/biquad.v:82-141](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/biquad.v#L82-L141) — state 0–5；每态切换 `parameterMux` 与系数地址，复用同一个 `macc`，并在 state 5 完成输出握手、更新 `yOld`、回到 state 0。

每条数据 lane 一个 `macc` + 饱和截位（支持 `DATA_COUNT` 路并行）：

[dsp/biquad.v:155-185](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/biquad.v#L155-L185) — `macc` 做乘累加；`reduceWidth` 把 `MAC_WIDTH` 饱和回 `DATA_WIDTH`，`-2` 偏移对应系数 \([-2,2)\) 的 2 位整数部分。

`macc` 本身是 Vivado 模板风格的乘累加（带 `sload` 清零）：

[dsp/biquad.v:191-236](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/biquad.v#L191-L236) — `mult_reg <= a_reg * b_reg; adder_out <= old_result + mult_reg;`。

#### 4.3.4 代码实践

1. **实践目标**：用现成测试台验证 biquad 在多种系数下的正确性（含单位增益、延迟、滑动平均、一阶/二阶低通、溢出恢复）。
2. **操作步骤**：执行 `make -C dsp biquad_check`。该 target 先编译 `biquad_tb`（依赖 `saturateMath.v`，见 [dsp/rules.mk:67-68](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/rules.mk#L67-L68)），再用 `$(VVP)` 跑仿真。
3. **需要观察的现象**：testbench 会逐项打印 `Unity gain`、`Boxcar average`、`Weighted average (FIR)`、`First order low pass`、`Second order low pass` 等用例，每行给出 `expect/got` 与 `PASS/NEAR/FAIL`。
4. **预期结果**：末尾打印 `PASS`（**待本地验证**）。可参考 [dsp/biquad_tb.v:137-177](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/biquad_tb.v#L137-L177) 的 `setCoefficients` 任务理解「写 5 个系数 + 写地址 7 解除复位」的协议。

#### 4.3.5 小练习与答案

- **练习 1**：为什么系数 RAM 里存的是 `−a1`、`−a2` 而不是 `a1`、`a2`？
  - **答**：差分方程里本就是减去 \( a_1 y(t{-}1), a_2 y(t{-}2) \)；存成负数后累加器统一做加法，省掉减法器。
- **练习 2**：写系数过程中为什么要保持复位、直到写地址 7？
  - **答**：保证滤波器始终用一整套一致的系数，避免在新旧系数混合的状态下产生瞬态错误输出。

---

### 4.4 iirFilter：biquad 级联

#### 4.4.1 概念说明

`iirFilter` 不做新的滤波运算，它只是**把 `STAGES` 个 `biquad` 串起来**，得到一个 \( 2\times\text{STAGES} \) 阶的 IIR。这种「二阶节级联」是工程上实现高阶 IIR 的标准做法，因为：

- 每节只有二阶，系数灵敏度低、数值稳定；
- 可以把零极点成对分配到各节；
- 复用同一个经过验证的 `biquad`，降低设计风险。

它的另一个职责是**系数分发**：用一组 `sysGPIO` 接口（一个 32 位字 + 选通）把系数「广播」给指定那一级 biquad，靠一个 one-hot 的 `sysStageSelect` 选台。

#### 4.4.2 核心流程

1. `sysGPIO_Out[31]` 为 1 表示这是「系数值」写操作；用 `sysGPIO_Out[3+:STAGE_ADDRESS_WIDTH]` 译码出 one-hot 的 `sysStageSelect`，选中某一级；用低 3 位做该级内的系数地址。
2. 用 `interStageData/Valid/Ready` 三组「线网数组」把 `STAGES` 个 biquad 首尾相连：第 0 节的输入是顶层 `S_TDATA`，第 `STAGES-1` 节的输出是顶层 `M_TDATA`，反压 `Ready` 反向传递。
3. 每一级只在自己被选中时才接收系数写。

#### 4.4.3 源码精读

模块说明与端口（与 `biquad` 同款 AXI-Stream 握手）：

[dsp/iirFilter.v:1-23](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/iirFilter.v#L1-L23) —「Chain of biquad elements」；系数经 `sysGPIO_Strobe/sysGPIO_Out` 加载。

系数地址译码 + one-hot 选台：

[dsp/iirFilter.v:26-40](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/iirFilter.v#L26-L40) — `sysStageSelect <= 1 << sysGPIO_Out[3+:STAGE_ADDRESS_WIDTH]`，把高几位译成级号、低 3 位译成系数地址。

级间互连数组（数据流正向、反压反向）：

[dsp/iirFilter.v:47-55](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/iirFilter.v#L47-L55) — `interStageData/Valid/Ready` 把首尾接到顶层 `S_*`/`M_*` 端口。

`generate` 串起 `STAGES` 个 biquad，每级只在自己被选中时收系数：

[dsp/iirFilter.v:57-77](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/iirFilter.v#L57-L77) — `biquad_i` 链；`.sysCoefficientStrobe(sysGPIO_Strobe && sysIsValue && sysStageSelect[i])`。

#### 4.4.4 代码实践

1. **实践目标**：验证高阶 IIR（多 biquad 级联）仍能正确滤波。
2. **操作步骤**：执行 `make -C dsp iirFilter_check`（依赖 `saturateMath.v`，见 [dsp/rules.mk:68](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/rules.mk#L68)）；对照 [dsp/iirFilter_tb.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/iirFilter_tb.v) 看它如何给不同级写不同系数。
3. **需要观察的现象**：每级系数独立加载、整体滤波特性是各级的乘积。
4. **预期结果**：打印 `PASS`（**待本地验证**）。

#### 4.4.5 小练习与答案

- **练习 1**：要把一个 6 阶 IIR 装进 `iirFilter`，`STAGES` 应设为几？
  - **答**：3（每个 biquad 贡献 2 阶）。
- **练习 2**：为什么反压 `Ready` 要从输出侧反向连到输入侧？
  - **答**：下游不收（`M_TREADY` 低）时，必须把「别送」逐级回传到上游，否则会丢数据；这正是 AXI-Stream 握手的方向。

---

### 4.5 基础存储原语与一阶低通

这一节不是独立的「最小模块」，而是支撑前面所有滤波器的**共用原语**：`reg_delay`（已在 4.2 见过）做固定延时，`fifo`/`circle_buf`/`dpram` 做缓冲，`lpass1` 是最简单的一阶 IIR。理解它们能让你看懂任意 DSP 流水的存储与缓冲。

#### 4.5.1 概念说明

- **`dpram`**：真双口 RAM（A 口读写、B 口只读，独立时钟），是所有块存储的基础。Xilinx/Altera 综合工具都能把它推断成 Block RAM。
- **`fifo`**：基于 `dpram` 的环形 FIFO，带 `full/empty/count`，关键技巧是用一个 `last_write` 旁路寄存器解决「Block RAM 两拍延迟」与「单拍读」的矛盾。
- **`circle_buf`**：双缓冲（double-buffered）环形缓冲，写入侧填满一半、读出侧读另一半，靠 `wbank/rbank` 翻转交接，常用于触发后保存故障波形。
- **`lpass1`**：一阶 RC 低通 IIR，\( y[n]=y[n{-}1]+k(x[n]-y[n{-}1]) \)，\( k=2^{-\text{klog2}} \)，极点在 \( (1-k) \)。

#### 4.5.2 核心流程

- `fifo`：写指针 `wr_addr`、读指针 `rd_addr`，填充量 `fill = wr_addr - rd_addr`；`empty=fill==0`、`full=fill>=len`；`dout = last ? last_write : read`（单元素时走旁路）。
- `circle_buf`：写入侧在 `iclk` 域按 `write_addr` 写当前 bank；读出侧在 `oclk` 域读另一个 bank；两边各持一个 `wbank/rbank` 标志，通过两路 `reg_tech_cdc` 跨域握手交换 buffer 所有权；内部就是一片 `dpram`。
- `lpass1`：把 `din` 左移 `klog2` 位提高精度，做差 `sub = (din<<full_sh) - dout_r`，再右移回累加 `dout_r += sub>>>full_sh`。

#### 4.5.3 源码精读

`dpram` 的双口读写（注释说明它能被识别为块存储）：

[dsp/dpram.v:4-46](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/dpram.v#L4-L46) — 独立 `clka/clkb`；`ala/alb` 寄存地址制造读延迟；可选 `$readmemh` 初始化。

`fifo` 的填充量与旁路读：

[dsp/fifo.v:42-58](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/fifo.v#L42-L58) — `fill = wr_addr - rd_addr`；`dout = last ? last_write : read`；用旁路寄存器补上 Block RAM 的两拍延迟。

`circle_buf` 的双缓冲交接与内部 `dpram`：

[dsp/circle_buf.v:49-123](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/circle_buf.v#L49-L123) — `wbank/rbank` 翻转；两路 `reg_tech_cdc` 做跨域标志传递；末尾 `dpram #(.aw(aw+1),...)` 实例（地址最高位选 bank）。

`lpass1` 的一阶 IIR 实现：

[dsp/lpass1.v:1-38](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/lpass1.v#L1-L38) — 差分方程注释与截止频率公式 `fc = k/[(1-k)*2*PI*dt]`；用移位代替乘法实现 \( k=2^{-\text{klog2}} \)。

> 顺带一提：`fifo.v` 里有一段 `ifdef FORMAL` 的形式化验证（基于 ZipCPU 的 FIFO 练习），用 SymbiYosys 证明 FIFO 的顺序与边界性质——这是 u6-l1「形式化验证」会展开的话题，这里先留个印象。

#### 4.5.4 代码实践

1. **实践目标**：跑通 FIFO 与 circle_buf 的仿真自校验。
2. **操作步骤**：执行 `make -C dsp circle_buf_tb`（生成可仿真可看波的 testbench）；`fifo` 在 `dsp/` 没有独立 `_tb`，但可阅读 [dsp/fifo.v:80-193](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/fifo.v#L80-L193) 的 `FORMAL` 段，看它如何用 `assert` 保证「写顺序 = 读顺序」「不能同时 full 和 empty」。
3. **需要观察的现象**：`circle_buf` 的 `buf_transferred` 在 buffer 交接时拉高一拍；`fifo` 的 `count` 随读写增减。
4. **预期结果**：`circle_buf_tb` 仿真完成无 `FAIL`（**待本地验证**）。

#### 4.5.5 小练习与答案

- **练习 1**：`fifo` 为什么需要 `last_write` 旁路寄存器？
  - **答**：Block RAM 读有一拍（或两拍）延迟，当 FIFO 里只剩一个元素时来不及从 RAM 读出，所以用寄存器直接缓存「最近一次写入」。
- **练习 2**：`lpass1` 为什么不做饱和算术？
  - **答**：它的增益严格小于 1（极点在单位圆内），输出幅度不会超过输入，故无需饱和。

## 5. 综合实践

把本讲的知识串起来，完成下面这个「**信号抽取链阅读 + 验证**」任务：

1. **画出一条完整的粗抽取链**：ADC 原始数据 → `cic_multichannel`（二阶 CIC 大倍率抽取 + 串行化）→ `ccfilt` 内的 `half_filt`（半带抽取两倍）→ 后续可能再接 `iirFilter`（高阶 IIR 整形）。在图上标出每一级的**速率**（\( f_s \to f_s/R \to f_s/(2R) \)）与**位宽**变化。
2. **运行验证**：
   - `make -C dsp cic_multichannel_check`
   - `make -C dsp biquad_check`
   - `make -C dsp half_filt_check`
   - `make -C dsp iirFilter_check`
3. **回答关键问题**：在 `cic_multichannel` 里，从「全速率多通道输入」到「`cc_sr_out` 低速率输出」，数据经历了哪几次速率变化？位宽为什么先涨后缩？（提示：积分器涨、梳状 + 桶形移位缩。）
4. **延伸（可选）**：阅读 [dsp/cic_multichannel_tb.v:204-234](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/cic_multichannel_tb.v#L204-L234) 看 DUT 是如何被参数化实例化的（`n_chan=12`、`di_rwi=32`、`cc_halfband=1`），并解释为什么测试台把 `di_noise_bits` 设成 1。

> 这个任务把「CIC 位增长 → 半带无乘法器抽取 → IIR 整形」三件事串成一条真实的 Bedrock 抽取链，是后续 cmoc/rtsim 里射频下变频链的雏形。

## 6. 本讲小结

- **CIC 是无乘法器的速率变换利器**：积分在高速侧、梳状在低速侧，位宽按 \( N\log_2 R \) 增长；`cic_simple_s/us` 是一阶版，`cic_multichannel` 是二阶多通道版（积分器 `double_inte_smp` + 串行化器 + 梳状 `ccfilt/doublediff`）。
- **`cic_interp` 是 CIC 的插值镜像**：梳状在低速率、积分在高速率，插值倍数 \( 2^{\text{span}} \)。
- **`half_filt` 是无乘法器半带 FIR**：用对称性 + 移位加减实现 11 抽头，并自带抽取两倍。
- **`biquad` 是 IIR 的原子单元**：单 MAC 分时复用完成 5 次乘法，系数经 `sysClk` 写入、跨域到 `dataClk`，写地址 7 解除复位保证系数原子一致。
- **`iirFilter` 把多个 biquad 串成高阶 IIR**，靠 one-hot `sysStageSelect` 分发系数。
- **`fifo/circle_buf/dpram/reg_delay` 是贯穿全流的存储原语**，`lpass1` 是最简一阶 IIR；`fifo` 还自带形式化验证。

## 7. 下一步学习建议

- **下一个自然的方向是 u4（时钟域跨越与片上互联）**：本讲的 `biquad` 已经用到了 `reg_tech_cdc` 做复位跨域，`circle_buf` 用到了两路 `reg_tech_cdc` 做 buffer 交接——u4-l1 会系统讲清楚这些 CDC 模块。
- **想看滤波器在真实系统里如何组合**：跳到 u6-l2（rtsim）与 u6-l3（cmoc），那里会把 CIC/半带/IIR 拼进完整的射频下变频与反馈控制链路。
- **对 `fifo.v` 的形式化验证好奇**：直接去 u6-l1（cdc_snitch 与形式化验证），那里会讲 SymbiYosys/yosys 的用法。
- **建议继续阅读的源码**：`dsp/fdownconvert.v`（u3-l3 讲过，里面藏着隐式抽取低通）、`dsp/ssb_out.v`（上变频链用到的插值），把它们和本讲的抽取/插值模块对照看，会对「速率与位宽处处守恒」有更直观的感受。
