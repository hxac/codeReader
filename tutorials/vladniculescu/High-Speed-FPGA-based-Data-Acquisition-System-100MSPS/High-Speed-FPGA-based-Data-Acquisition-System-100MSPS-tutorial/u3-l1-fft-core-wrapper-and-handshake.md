# FFT 核封装与握手时序

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 `Fourier` 模块是什么：它是把 Xilinx 官方 FFT IP（黑盒 `fft`）「薄薄包一层」的封装器，本身不含任何运算逻辑，只做端口改名与转接。
- 读懂 FFT 核的**握手时序**：`start` 启动 → `rfd`/`xn_index` 加载输入 → `busy` 计算 → `edone`/`done` 完成 → `dv`/`xk_index` 卸载输出，并能解释 TOP 主状态机如何用这套握手调度一次完整的变换。
- 看懂 TOP 里 FFT 的**自驱动加载/卸载**：FFT 自己输出的 `xn_index` 经 `mux_ram1` 回送成 ram1 的读地址，于是样本被「按索引自动喂回」FFT；输出端 `xk_index` 又自动成为 ram2 的写地址。
- 理解 `fwd_inv=1`（正向变换）和 `scale_sch=12'b001010101011`（缩放调度，防止定点溢出）这两个静态配置的作用。
- 在 TOP.v 里准确定位 `fft_state → fft_write_state` 的跳转条件（`edone`），以及 `fft_write_state → square_state` 的跳转条件（`index_out` 计满）。

## 2. 前置知识

本讲是「采样链」到「频谱链」的转折点——ram1 里的时域样本从这里开始变成频域频谱。动手前先建立三个概念。

### 2.1 为什么要做 FFT

示波器能看「波形随时间变化」（时域），但很多信号的特征在时域里看不清，反而看「各频率分量各占多少能量」（频域）一目了然。FFT（快速傅里叶变换）就是把 \(N\) 个时域样本 \(\{x[n]\}\) 算成 \(N\) 个频域样本 \(\{X[k]\}\) 的高速算法：

\[
X[k] = \sum_{n=0}^{N-1} x[n]\, W_N^{\,nk}, \quad W_N = e^{-j2\pi/N}
\]

每个 \(X[k]\) 是一个**复数**（有实部 Re 和虚部 Im），描述频率为 \(k\) 的分量。本工程在 FFT 之后接 Square（平方）+ Sum（求和）+ Root_square（开方），把每个 \(X[k]\) 算成幅度 \(|X[k]|=\sqrt{\text{Re}^2+\text{Im}^2}\)，这才是示波器「频谱模式」要显示的东西。本讲只走到 FFT 输出 `xk_re`/`xk_im`，后续的幅度计算放在 u3-l2。

### 2.2 Xilinx FFT IP 是个「黑盒」

手写一个高性能 FFT 非常难（要管蝶形运算、旋转因子、位反转、流水线）。Xilinx Vivado 提供了一个官方 IP 核——**FFT LogiCORE**，你在 Vivado 里用图形界面配置好（点数 \(N\)、位宽、架构、是否缩放），它会生成一个黑盒模块 `fft`，直接拿来例化即可。这个黑盒的**内部源码不在仓库里**（它在二进制的 Vivado 工程包内），我们只能通过它的**端口和握手协议**来使用它——就像用一个芯片，你看的是数据手册，而不是晶体管图。

> 术语提示：**IP 核**（Intellectual Property core）在 FPGA 语境里指「可复用的预制硬件模块」，多为厂商提供（如 Xilinx 的 FFT、乘法器、PLL、CORDIC）。本工程里凡是封装这种黑盒 IP 的模块（`Fourier`、`Square`、`Sum`、`Root_square`、`pll_loop`），都只是一层很薄的「接线壳」。

### 2.3 「握手」是什么意思

FFT 核算一次变换需要很多个时钟周期（要逐点加载 \(N\) 个输入、计算、再逐点卸载 \(N\) 个输出）。它不能用一个时钟就给你答案，于是采用**握手协议**：核用几个标志信号告诉外界「我现在能收数据了 / 我在忙 / 数据好了你可以拿了」。外界（TOP 状态机）就盯着这些标志，在正确的时刻喂数据、存结果。本讲的 `start/rfd/edone/done/dv` 就是这套握手信号，掌握它们是理解整条 DSP 链时序的钥匙。

## 3. 本讲源码地图

| 文件 | 模块名 | 作用 |
|---|---|---|
| `verilog files/Fourier.v` | `Fourier` | Xilinx FFT IP（`fft`）的封装壳：把黑盒端口改个名暴露出去，自身不含逻辑 |
| `verilog files/TOP.v` | `TOP` | 顶层：例化 `Fourier`、用主状态机的 `fft_state`/`fft_write_state` 驱动一次变换的握手，并把输出经 Square+Sum 写入 ram2 |

数据与时序通路一句话总结：

- **输入**（加载）：ram1 读出 `buffer` → `decoder` 转补码 `decoder_out`（见 u2-l4）→ FFT 实部 `xn_re`；虚部 `xn_im` 恒为 0。加载地址由 FFT 自己的 `xn_index` 经 `mux_ram1` 回送。
- **时钟**：FFT 核跑在 `clk_100`（100 MHz），与系统主时钟 `clk`（200 MHz）不同域（见 u2-l1）。
- **输出**（卸载）：FFT 给出 `xk_re`/`xk_im`（各 10 位），经 Square×2 + Sum 合成 `re²+im²` 写入 ram2；卸载地址由 FFT 自己的 `xk_index`（`index_out`）担当。

## 4. 核心概念与源码讲解

### 4.1 Fourier 模块：Xilinx FFT IP 的薄封装

#### 4.1.1 概念说明

`Fourier` 是一个「壳」（wrapper）：它把 Xilinx FFT 黑盒 `fft` 包起来，给每个端口取一个更短、更语义化的名字，方便顶层例化时书写。它**自身没有任何 `always` 或 `assign` 运算逻辑**——整个模块只有一份端口声明和一句 IP 例化。理解这一点很重要：你看到的 `rfd`、`edone`、`dv` 等信号，全是 FFT IP 黑盒直接驱动的，`Fourier` 只是个「转接排针」。

为什么要包这层壳？因为黑盒 `fft` 的端口名是 IP 自动生成的（如实例名 `your_instance_name`、信号名直接用 IP 默认名），又长又没有业务含义。包一层 `Fourier` 后，顶层就能用 `start_fft`、`xk_re` 这种贴近本工程语义的名字来接线。

#### 4.1.2 核心流程

封装的流程就是把外部的 `Fourier` 端口「一对一」接到内部 `fft` 实例的同名端口上：

```
外部端口 start ──► fft.start
外部端口 xn_re ──► fft.xn_re   （10 位实部输入）
外部端口 xn_im ──► fft.xn_im   （10 位虚部输入）
   ...（其余同理）...
fft.rfd      ──► 外部端口 rfd
fft.xk_re    ──► 外部端口 xk_re（10 位实部输出）
fft.xk_im    ──► 外部端口 xk_im（10 位虚部输出）
```

`Fourier` 的端口可以分成三组来记：

| 分组 | 端口 | 方向 | 含义 |
|---|---|---|---|
| 配置 | `fwd_inv`、`fwd_inv_we`、`scale_sch`、`scale_sch_we` | 输入 | 正/逆变换选择、缩放调度（4.3 节详解） |
| 加载（输入） | `start`、`xn_re`、`xn_im`、`rfd`、`xn_index[10:0]` | 混合 | `start` 启动；`rfd` 表示「可收数据」；`xn_index` 是核给出的加载索引 |
| 卸载（输出） | `busy`、`edone`、`done`、`dv`、`xk_index[10:0]`、`xk_re`、`xk_im` | 输出 | `busy` 计算中；`edone`/`done` 完成；`dv` 数据有效；`xk_index` 卸载索引 |

注意两个索引都是 11 位（`[10:0]`），最多可寻址 2048 个点。

#### 4.1.3 源码精读

先看 `Fourier` 的模块声明与端口列表：

[verilog files/Fourier.v:8-28](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/Fourier.v#L8-L28) — `Fourier` 模块的端口声明。可以看到 `clk`、`start`、`xn_re[9:0]`、`xn_im[9:0]`（输入各 10 位）、`fwd_inv`/`fwd_inv_we`、`scale_sch[11:0]`/`scale_sch_we`（4.3 节的缩放调度）；输出侧有 `rfd`、`xn_index[10:0]`、`busy`、`edone`、`done`、`dv`、`xk_index[10:0]`、`xk_re[9:0]`、`xk_im[9:0]`（实部虚部各 10 位）。

再看唯一的逻辑——对黑盒 `fft` 的例化：

[verilog files/Fourier.v:32-50](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/Fourier.v#L32-L50) — 例化 Xilinx FFT IP，实例名 `your_instance_name`（IP 默认名）。每一行都是把 `Fourier` 的某个端口 `.xxx(yyy)` 直接连到 `fft` 实例的同名端口 `.xxx(yyy)` 上，没有任何额外运算。这就印证了「`Fourier` 只是一层壳」。

接着看顶层如何例化 `Fourier`：

[verilog files/TOP.v:153-170](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L153-L170) — 顶层例化 `Fourier FFT(...)`。注意几个关键接线：

- `.clk(clk_100)`：FFT 核用 100 MHz 时钟（不是 200 MHz 的 `clk`）。
- `.start(start_fft)`：启动信号由主状态机驱动（4.2 节）。
- `.xn_re(decoder_out)`：实部输入接 u2-l4 的补码转换结果；`.xn_im(10'b0000000000)`：虚部**恒为 0**（输入是实信号，时域样本没有虚部）。
- `.fwd_inv(1'b1)`、`.fwd_inv_we(1'b1)`：写「正向变换」（时域→频域）。
- `.scale_sch(12'b001010101011)`、`.scale_sch_we(1'b1)`：写缩放调度（4.3 节）。
- `.xn_index(index_in)`、`.xk_index(index_out)`：加载索引 `index_in`、卸载索引 `index_out`。

> ⚠️ **注释陷阱（延续前几讲的习惯，先点名）**：TOP.v 第 53 行 `wire rfd; //ready for data- Root Square Module`——这条注释把 `rfd` 说成「Root Square Module」的信号，是**错的**。`rfd`（ready for data）是 FFT 核的加载就绪标志（它连到 `Fourier` 的 `.rfd(rfd)`），与开方模块无关。读代码时一律以例化接线为准，不要被注释带偏。

#### 4.1.4 代码实践：数一数「壳」里有多少自己的逻辑

1. **实践目标**：建立「`Fourier` 是纯封装、零运算」的直观印象。
2. **操作步骤**：
   - 打开 `Fourier.v`，从第 1 行读到第 51 行。
   - 找一找里面有没有 `assign`、`always`、算术运算符（`+`、`*`、`~`）或寄存器声明。
3. **需要观察的现象**：除了端口声明（`input`/`output`）和那一句 `fft your_instance_name (...);` 例化，**整段代码没有任何运算逻辑**——每个端口只是「穿过去」接到 IP 上。
4. **预期结果**：你会确认 `Fourier` 不做任何计算，所有运算都在黑盒 `fft` 内部。这也意味着，想知道 FFT 的具体点数 \(N\)、架构（基-2 还是基-4）、流水线深度，都得到 Vivado 工程里看 IP 的配置（二进制，**待确认**）——光读 `Fourier.v` 看不到。
5. **待本地验证**：FFT IP 的点数 \(N\) 与架构配置在二进制 Vivado 工程包内，本仓库的可读源码无法直接给出。

#### 4.1.5 小练习与答案

**练习 1**：`Fourier` 的输入 `xn_re`/`xn_im` 和输出 `xk_re`/`xk_im` 位宽分别是多少？为什么输入虚部在本工程里没用上？

**参考答案**：都是 10 位。输入虚部 `xn_im` 在 TOP 里接 `10'b0000000000`（恒 0），因为 ADC 采集的是实信号，时域样本没有虚部；FFT 内部会从实输入算出复数输出，所以输出 `xk_re`/`xk_im` 都是有意义的。

**练习 2**：为什么说读懂 `Fourier.v` 还不足以知道 FFT 的点数 \(N\)？

**参考答案**：`Fourier` 只是黑盒 `fft` 的端口封装，点数 \(N\)、架构、流水线深度等参数是在 Vivado 里配置 FFT IP 时写死在生成的 IP 文件里的（属二进制工程包，不在可读源码中）。`Fourier.v` 只暴露端口，不暴露这些配置，故 \(N\) 待确认。

---

### 4.2 FFT 握手时序：从 start 到 done 的完整过程

#### 4.2.1 概念说明

Xilinx FFT 核处理一次变换分四个阶段，每个阶段用一组握手信号对外沟通：

- **启动**：外界拉高 `start`，核开始一轮变换。
- **加载（loading）**：核拉高 `rfd`（ready for data）并自动递增 `xn_index`（0,1,2,…）。外界在每拍把第 `xn_index` 个样本送上 `xn_re`/`xn_im`。收满 \(N\) 个后 `rfd` 变低。
- **计算**：`busy` 拉高，核内部做蝶形运算，外界既不喂也不取。
- **卸载（unloading）**：在计算结束**前一个时钟**，核拉高 `edone`（early done）一个周期；下一拍 `done` 脉冲一个周期；随后 `dv`（data valid）拉高，核递增 `xk_index`（0,1,2,…）并在每拍给出 `xk_re[xk_index]`/`xk_im[xk_index]`。外界逐点把结果存走。

本工程的巧妙之处在于：**加载和卸载的地址都不需要 TOP 状态机手动遍历**——FFT 核自己输出的 `xn_index`/`xk_index` 直接回送成 ram1 的读地址和 ram2 的写地址，于是样本「被核按索引自动拉走 / 自动写回」。状态机只需管「何时启动、何时算完、何时切到下一阶段」。

#### 4.2.2 核心流程

一次完整 FFT 握手（伪时序，省略无关信号）：

```
clk_100 节拍:   t0      t1      t2      ...   t_load   ...   t_edone   t_done   t_unload0  t_unload1 ...
start_fft:      1       1       1             1              1          0         0          0
rfd:            0       1       1             1→0            0          0         0          0
xn_index:       -       0       1             N-1            -          -         -          -
xn_re:          -       x[0]    x[1]          x[N-1]         -          -         -          -
busy:           0       0       0             0    1......1  1          0         0          0
edone:          0       0       0             0              1          0         0          0
done:           0       0       0             0              0          1         0          0
dv:             0       0       0             0              0          0         1          1
xk_index:       -       -       -             -              -          -         0          1
xk_re/xk_im:    -       -       -             -              -          -         X[0]       X[1]
```

TOP 状态机对应的两个状态：

- `fft_state`（启动 + 加载 + 计算）：拉高 `start_fft` 和 ram2 写使能 `we2`，**等 `edone` 一拉高就跳到 `fft_write_state`**。
- `fft_write_state`（卸载）：拉低 `start_fft`；此时 `dv` 升起、`xk_index` 递增，`xk_re`/`xk_im` 经 Square×2 + Sum 算成 `re²+im²` 写入 ram2（写地址 = `xk_index`）；**等 `index_out`（即 `xk_index`）计到接近末尾就跳到 `square_state`**（去做开方）。

为什么用 `edone` 而不是 `done` 做跳转？因为 `edone` 比 `done` 早一个时钟——在数据真正开始卸载（`dv` 升起）的前一拍，状态机就提前进入 `fft_write_state` 并打开 `we2`，这样 `dv` 一来、`xk_index` 一动，结果就能立即被写进 ram2，不会漏掉第一个输出点。

#### 4.2.3 源码精读

先看主状态机里 FFT 相关的状态参数定义：

[verilog files/TOP.v:213-227](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L213-L227) — 状态参数表。其中 `fft_state = 5'b00101`（第 218 行）、`fft_write_state = 5'b00110`（第 219 行）。前驱是 `acq_state`（采集填满 ram1 后进入 `fft_state`），后继是 `square_state`（开方阶段）。

再看握手相关信号在 TOP 顶层的声明与注释：

[verilog files/TOP.v:52-61](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L52-L61) — 这些 `wire` 全部连到 `Fourier` 实例上：`start_fft`（启动）、`rfd`（加载就绪）、`index_in`（加载索引）、`index_out`（卸载索引）、`busy`、`edone`（提前一拍完成）、`done`、`dv`（数据有效）、`xk_re`/`xk_im`（复数输出）。注释对每个信号的握手含义给了简短说明。

现在看本讲最关键的两段状态——`fft_state` 与 `fft_write_state`：

[verilog files/TOP.v:339-343](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L339-L343) — `fft_state`：

- `we2<=1'b1`：打开 ram2（存 `re²+im²`）的写使能，为接下来的卸载写做准备。
- `start_fft<=1'b1`：**启动 FFT**。这一赋值在 `fft_state` 的每个时钟都执行，所以 `start_fft` 在整个状态期间保持为 1，直到进入 `fft_write_state` 才拉低（FFT 核在 `busy` 期间会忽略后续的 `start`，故保持高电平不会重复触发；具体 `start` 的边沿/电平敏感性取决于 IP 配置，**待确认**）。
- `if(edone==1'b1) state<=fft_write_state;`：**这就是 `fft_state → fft_write_state` 的跳转条件**——`edone` 拉高即跳。状态机并不显式轮询 `rfd` 或遍历加载地址，加载是「自驱动」的（见下）。

[verilog files/TOP.v:346-354](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L346-L354) — `fft_write_state`：

- `start_fft<=1'b0`：撤销启动。
- `if(index_out==10'b1111111110)`：**这是 `fft_write_state → square_state` 的跳转条件**——等卸载索引 `index_out`（= `xk_index`）计到 `10'b1111111110`（= 1022）就认为卸载完毕，于是关 ram2 写（`we2<=0`）、开 ram3 写（`we3<=1`）、复位开方计数 `cnt_s`、进入 `square_state`。
  - 注意：`index_out` 是 11 位，而这里与一个 10 位字面量比较（零扩展为 1022）。这个「计到 1022 就停」的阈值暗示实际处理的点数大约在 1024 量级，但 FFT IP 的真实点数 \(N\) 藏在二进制工程里，**待确认**（详见 4.2.4 的讨论）。

**自驱动加载**是怎么实现的？关键在 ram1 的读地址选择器 `mux_ram1`：

[verilog files/TOP.v:195-198](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L195-L198) — `mux_ram1` 给 ram1 选读地址：`sel2 ? index_in : cnt_waveform`。在采集结束进入 FFT 阶段时，`sel2=1`（在 `wait_state` 里被置 1），所以 `ram_read = index_in = xn_index`。

于是链路闭环：

[verilog files/TOP.v:128-134](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L128-L134) — ram1（`SRAM ram_adc`）的读地址 `.addr_r(ram_read)` 就是上面 MUX 的输出。ram1 采用组合读（`data_out` 随 `addr_r` 即时变化，见 u2-l2），所以 `buffer = mem[xn_index]`——FFT 核给哪个索引，ram1 就即时吐出第几个样本。

[verilog files/TOP.v:200-201](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L200-L201) — `decoder` 把 `buffer`（偏移码）转成补码 `decoder_out`，再喂给 FFT 的 `xn_re`（见 TOP.v 第 156 行）。

把上面三段串起来：**FFT 核递增 `xn_index` → `mux_ram1` 把它送到 ram1 读地址 → ram1 组合读出 `buffer` → `decoder` 转补码 → 回到 FFT 的 `xn_re`。** 状态机一行加载代码都不用写，加载就被核自己的索引「拉」完了。

卸载端同理：`xk_index`（`index_out`）直接当 ram2 的写地址用：

[verilog files/TOP.v:137-142](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L137-L142) — ram2（`SRAM2 ram_fft_20bit`）：`.addr(index_out)`（写地址 = 卸载索引）、`.data_in(sum)`（`xk_re²+xk_im²`，由 Square×2+Sum 组合算出，详见 u3-l2）、`.we(we2)`。注释也点明 "index_out is automatically incremented when transform is done"——卸载地址同样是 FFT 核自动递增的。

> 跨时钟域提示：FFT 核跑在 `clk_100`（100 MHz），而 ram1/ram2 的写时钟是 `clk`（200 MHz）。加载路径上，`xn_index`（100 MHz 域）经组合的 `mux_ram1` → ram1 组合读 → `decoder` → `xn_re`，最终又被 100 MHz 的 FFT 核采样。只要这条组合链能在 10 ns（100 MHz 周期）内稳定，加载就能正确收敛。这是本工程「用组合读桥接两个时钟域」的务实做法；更严谨的设计会加同步 FIFO，这里从简。

#### 4.2.4 代码实践：找出跳转条件，还原一次完整握手（本讲主实践）

1. **实践目标**：在 TOP.v 里亲手定位 FFT 两个状态的跳转条件，并把一次「start → done」的完整握手讲清楚。
2. **操作步骤**：
   - 在 TOP.v 第 339–343 行（`fft_state`）确认：跳到 `fft_write_state` 的条件是 `edone==1'b1`（第 342 行）。
   - 在第 346–354 行（`fft_write_state`）确认：跳到 `square_state` 的条件是 `index_out==10'b1111111110`（第 348 行，= 1022）。
   - 对照 4.2.2 的伪时序图，把 `start_fft`、`rfd`、`xn_index`、`busy`、`edone`、`done`、`dv`、`xk_index` 八个信号在每个阶段（启动/加载/计算/卸载）的高低态填出来。
3. **需要观察的现象**：
   - `start_fft` 在 `fft_state` 全程为 1，在 `fft_write_state` 开头被拉 0。
   - 状态机**没有**显式遍历 `xn_index` 去加载；加载是被「`xn_index` → `mux_ram1` → ram1 → `decoder` → `xn_re`」这条回送链自驱动完成的。
   - 状态机用 `edone`（而非 `done`）做跳转，目的是在 `dv` 升起、`xk_index` 开始递增之前，提前一个时钟进入 `fft_write_state` 并打开 `we2`，从而不漏掉第 0 个输出点。
4. **预期结果**：你能用一段话讲清一次完整握手——
   > ram1 填满后主 FSM 进入 `fft_state`，拉高 `start_fft` 启动 FFT 核；核拉高 `rfd` 并递增 `xn_index`，经 `mux_ram1` 把索引回送成 ram1 读地址，于是第 `xn_index` 个样本经 `decoder` 转补码后自动回到 `xn_re`，逐点加载；收满后 `rfd` 变低、`busy` 升起开始计算；计算结束前一拍 `edone` 脉冲，FSM 立刻跳到 `fft_write_state` 并拉低 `start_fft`；随后 `dv` 升起、`xk_index` 递增，`xk_re`/`xk_im` 经 Square×2+Sum 写入 ram2 的对应地址；当 `index_out` 计到 1022，FSM 关 ram2 写、开 ram3 写，跳到 `square_state` 去做开方。
5. **待本地验证**：`index_out==1022` 这个卸载终止阈值与 FFT 实际点数 \(N\) 的关系。`index_out`/`index_in` 虽是 11 位（最大可寻址 2048），但这里用 10 位字面量 1022 比较，且后续 `square_state` 的循环界 `cnt_s<11'b1111111111`（=1023）也在 1024 量级，**暗示实际点数 \(N\approx 1024\)**；但 \(N\) 的确切值由二进制 IP 配置决定，且该阈值是否会造成「最后 1~2 个点漏存」也需在仿真/硬件上确认。

#### 4.2.5 小练习与答案

**练习 1**：为什么状态机用 `edone` 而不是 `done` 作为 `fft_state → fft_write_state` 的跳转条件？

**参考答案**：`edone` 比 `done` 早一个时钟。提前一拍进入 `fft_write_state` 并打开 `we2`，能保证在 `dv` 升起、`xk_index` 开始递增的那一刻，ram2 的写使能已经就绪，从而不漏掉第 0 个输出点。若等 `done` 再切，第一拍输出可能来不及写入。

**练习 2**：TOP 状态机里**没有任何**「`for n=0..N-1` 把样本送给 FFT」的代码，那 1024/2048 个样本是怎么进到 FFT 核里的？

**参考答案**：是「自驱动加载」——FFT 核自己递增 `xn_index`，该索引经 `mux_ram1`（`sel2=1` 时选 `index_in`）回送成 ram1 的读地址；ram1 是组合读，立刻吐出第 `xn_index` 个样本到 `buffer`；`decoder` 把它转成补码 `decoder_out` 送回 FFT 的 `xn_re`。于是索引每加一，对应样本就自动喂回，无需状态机手动遍历。

**练习 3**：`fft_write_state` 里 `start_fft<=1'b0` 这一句若漏掉，会怎样？

**参考答案**：`start_fft` 会一直保持 1。FFT 核在一次变换 `done` 之后，若 `start` 仍为高，可能在下个时机被重新触发，启动一次非预期的变换，干扰正在进行的卸载写。所以卸载阶段必须把 `start_fft` 拉低。（具体 `start` 是电平还是边沿有效取决于 IP 配置，待确认。）

---

### 4.3 scale_sch 缩放调度与 fwd_inv 方向

#### 4.3.1 概念说明

FFT 核还有两个静态配置输入：`fwd_inv`（变换方向）和 `scale_sch`（缩放调度）。它们在 `Fourier` 例化时被接成常量，等于「上电时就配置好，运行中不再改」。

- **`fwd_inv`**：1 = 正向 FFT（时域 → 频域），0 = 逆 FFT（频域 → 时域）。本工程做频谱分析，所以接 `1'b1`（正向）。配套的 `fwd_inv_we` 也接 1，表示「把方向位写进核的配置寄存器」。
- **`scale_sch`**：定点 FFT 的**缩放调度**。FFT 运算会让信号幅度增长——最坏情况下，一个满幅直流输入会让某个频域 bin 的幅度达到输入的 \(N\) 倍：

\[
|X[k]| \le \sum_{n=0}^{N-1} |x[n]| \le N \cdot \max_n |x[n]|
\]

  若不做缩放，10 位输入算到后面必然溢出。Xilinx FFT 核允许在每个蝶形阶段右移若干位（相当于除以 2 的幂），把整体增益压回到「输入多大、输出也大致多大」，从而让 10 位输入算出的结果仍能用 10 位装下。`scale_sch` 就是这串「每级移几位」的编码，配套的 `scale_sch_we` 接 1 表示写入。

#### 4.3.2 核心流程

缩放调度的使用流程很简单：

1. `fwd_inv_we=1` 时，把 `fwd_inv`（这里 = 1）写进核 → 选定正向变换。
2. `scale_sch_we=1` 时，把 `scale_sch`（这里 = `12'b001010101011`）写进核 → 选定每级的右移方案。
3. 这两个写操作只需在 `start` 之前生效一次；之后每次 `start` 启动的变换都沿用这套配置。

至于 `12'b001010101011` 这个具体位串怎么拆解成「每级移几位」，取决于 FFT 核的架构与点数（基-2 还是基-4、共几级），而这些都是二进制 IP 配置里定的，**待确认**。可以确定的是：它是作者调好的一个固定值，目的是在「不溢出」与「不损失太多精度」之间取平衡——缩得太狠会丢精度，缩得不够会溢出。

#### 4.3.3 源码精读

[verilog files/TOP.v:154-161](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L154-L161) — `Fourier` 例化的配置接线：

- `.fwd_inv(1'b1)` + `.fwd_inv_we(1'b1)`：写「正向 FFT」。
- `.scale_sch(12'b001010101011)` + `.scale_sch_we(1'b1)`：写 12 位缩放调度，注释写 "scale number: data is scaled to use less bits"，点明了它的作用就是缩放以节省位宽、防止溢出。

[verilog files/Fourier.v:36-40](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/Fourier.v#L36-L40) — `Fourier` 把 `fwd_inv`、`fwd_inv_we`、`scale_sch`、`scale_sch_we` 四个端口直接穿给黑盒 `fft`，自身不做任何处理。

> 提示：`scale_sch` 是 12 位。按 Xilinx FFT 核的惯例，每位/每两位对应一个蝶形级；12 位宽度对应的级数（从而点数 \(N\)）取决于核架构（基-2 还是基-4），这些都在二进制 IP 配置里，**待确认**。学习时只需记住「它是一串固定的每级缩放编码，防溢出」即可，不必死记位串含义。

#### 4.3.4 代码实践：观察「不缩放会怎样」

1. **实践目标**：建立「缩放是定点 FFT 必备」的直觉。
2. **操作步骤**：
   - 假设把 `.scale_sch` 改成 `12'b000000000000`（各级都不移位，即不缩放），`.scale_sch_we` 保持 1。
   - 思考一个直流输入：所有 \(N\) 个样本都等于满幅正值（补码 +511）。
3. **需要观察的现象**：直流输入的全部能量会集中到 \(X[0]\)，理论上 \(|X[0]| \approx N \times 511\)，远超 10 位能表示的范围（±512）。
4. **预期结果**：不缩放时 \(X[0]\) 严重溢出，10 位输出回绕成无意义的负数；频谱的直流 bin 失真。这就解释了为什么作者必须设一个非零的 `scale_sch`。
5. **待本地验证**：`12'b001010101011` 的具体缩放总量与该输入下 \(X[0]\) 是否恰好不溢出，需在 Vivado 仿真中验证。

#### 4.3.5 小练习与答案

**练习 1**：`fwd_inv=1` 和 `fwd_inv=0` 分别对应什么变换？本工程为什么选前者？

**参考答案**：1 = 正向 FFT（时域→频域），0 = 逆 FFT（频域→时域）。本工程要把采集到的时域波形转成频谱来显示，所以用正向 FFT，接 `1'b1`。

**练习 2**：用一句话解释 `scale_sch` 为什么是必需的。

**参考答案**：定点 FFT 最坏情况会让输出幅度增长到输入的 \(N\) 倍，不做缩放必然溢出；`scale_sch` 让核在每个蝶形级右移若干位，把整体增益压回，使结果仍能装进 10 位。

---

## 5. 综合实践

把本讲三节内容串起来，做一次「从 ram1 填满到 ram2 开始写」的端到端时序追踪。

1. **实践目标**：用一个具体场景，把「启动 → 自驱动加载 → 计算 → edone 提前切档 → 自驱动卸载写 ram2」整条 FFT 握手链讲清楚，并标出每一步对应的 TOP.v 行号。
2. **操作步骤**：
   - **起点**：`acq_state` 检测到 `carry==1`（ram1 写满），跳到 `fft_state`（TOP.v 第 331 行）。
   - **启动与加载**：进入 `fft_state`（第 339–343 行），`start_fft<=1` 启动核；核递增 `xn_index`，经 `mux_ram1`（`sel2=1`，第 195–198 行）回送成 ram1 读地址（第 128–134 行），样本经 `decoder`（第 200–201 行）转补码后回到 `xn_re`（第 156 行）。逐点加载直到 `rfd` 变低。
   - **计算**：`busy` 升起，核内部运算。
   - **提前切档**：`edone` 脉冲 → 跳到 `fft_write_state`（第 342、346–347 行），`start_fft<=0`，`we2` 已为 1。
   - **卸载写 ram2**：`dv` 升起，`xk_index` 递增；`xk_re`/`xk_im` 经 Square×2 + Sum（第 172–182 行，u3-l2 详解）合成 `sum`，按 `.addr(index_out)` 写入 ram2（第 137–142 行）。
   - **终点**：`index_out==1022`（第 348 行）→ 关 `we2`、开 `we3`、跳到 `square_state`（第 350 行），交给开方阶段。
3. **需要观察的现象**：整条链里，状态机**只发三个调度指令**——`start_fft` 拉高、`edone` 到了切档、`index_out` 到了再切档；其余的「逐点加载/卸载地址」全由 FFT 核自己的 `xn_index`/`xk_index` 驱动。这是本工程 FFT 模块最值得学习的设计手法。
4. **预期结果**：你能画出一张时序图，横轴是 `clk_100` 节拍，标出 `start_fft / rfd / xn_index / busy / edone / done / dv / xk_index` 八条线的分段，并用箭头标出「`xn_index` → mux_ram1 → ram1 → decoder → xn_re」这条加载回送环，以及「`xk_index` → ram2 写地址」这条卸载直连。
5. **进阶思考（待本地验证）**：FFT 在 `clk_100`（100 MHz）域，而 ram1/ram2 写在 `clk`（200 MHz）域。试着找出：加载时 `xn_re` 被 100 MHz 采样，而 `buffer` 由 200 MHz 域的 ram1 组合读出——这条组合跨域路径为何在本设计里能工作？如果要更严谨，应插入什么样的同步结构？

## 6. 本讲小结

- `Fourier` 是 Xilinx FFT 黑盒 IP（`fft`）的**纯封装壳**：自身零运算逻辑，只把端口改名转接；想看 FFT 的点数 \(N\)、架构、流水线深度都得翻二进制 Vivado 工程（**待确认**）。
- FFT 用四个阶段的握手：`start` 启动 → `rfd`/`xn_index` 加载 → `busy` 计算 → `edone`/`done`/`dv`/`xk_index` 卸载。输入实部 `xn_re` 接 `decoder_out`，虚部 `xn_im` 恒 0，输出 `xk_re`/`xk_im` 各 10 位。
- TOP 的 `fft_state` 等 `edone` 跳到 `fft_write_state`；`fft_write_state` 等 `index_out==1022` 跳到 `square_state`。用 `edone`（而非 `done`）跳转是为了提前一拍打开 ram2 写使能，不漏第 0 个输出点。
- **自驱动加载/卸载**是本讲核心设计：FFT 核的 `xn_index` 经 `mux_ram1` 回送成 ram1 读地址（样本经 `decoder` 自动喂回 `xn_re`）；`xk_index` 直接当 ram2 写地址。状态机无需手动遍历地址。
- `fwd_inv=1` 选正向变换；`scale_sch=12'b001010101011` 是定点防溢出的每级缩放编码，其精确拆解依赖 IP 架构配置（**待确认**），但作用明确——压回增益、让 10 位结果不溢出。
- FFT 跑在 `clk_100`（100 MHz），与 ram1/ram2 所在的 `clk`（200 MHz）不同时钟域，靠 ram1 的组合读桥接加载路径。

## 7. 下一步学习建议

- FFT 输出的 `xk_re`/`xk_im` 接下来要算成幅度。建议下一讲学习 **u3-l2（幅度平方与求和）**，看 `Square`（乘法器封装）和 `Sum`（加法器封装）如何把这两个 10 位复数分量算成 `re²+im²`（21 位）写入 ram2。
- 想回顾 ram1/ram2 的双端口读写结构，可回看 **u2-l2（采样存储与三块 RAM）**，对照本讲的「ram1 读地址 = xn_index、ram2 写地址 = xk_index」。
- 想理解 `sel2`/`we2` 等控制信号在整个主状态机里何时被置位，可预习 **u5-l1（主采集状态机与命令协议）**，把本讲的 `fft_state`/`fft_write_state` 放回完整 FSM 语境。
