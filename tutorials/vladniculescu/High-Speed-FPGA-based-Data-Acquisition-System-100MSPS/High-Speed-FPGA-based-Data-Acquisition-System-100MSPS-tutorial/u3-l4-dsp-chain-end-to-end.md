# 信号处理链整体串联

## 1. 本讲目标

前三讲我们分别拆开了 DSP 信号链的三段零件：u3-l1 讲了 `Fourier`（FFT 核封装），u3-l2 讲了 `Square`＋`Sum`（平方求和），u3-l3 讲了 `Root_square`（开方）。本讲把它们**装回一条完整的流水线**，目标是：

1. 能把 **采样→FFT→平方求和→开方** 四段串成一条完整的幅度谱计算链，说出每一级的输入输出和位宽。
2. 理解 ram1／ram2／ram3 三块 RAM 在链中扮演的**级间缓冲**角色，以及它们各自的写使能由哪个状态驱动。
3. 能解释**为什么开方放在最后**、为什么最终幅度谱存入 ram3，而不是中途某块 RAM。
4. 能用一段时序叙述，把一帧数据从 `acq_state` 采集完成到 ram3 存满幅度谱的**逐阶段走查**讲清楚。

学完本讲，你应该能拿到一张「一个采样帧在 FPGA 内部经历了什么」的完整地图。

## 2. 前置知识

本讲默认你已经读过 u3-l1～u3-l3，因此对以下概念只做一句话回顾，不重复细节：

- **幅度（magnitude）**：复数 \( z=\mathrm{Re}+j\,\mathrm{Im} \) 的模长，定义为
  \[ |z|=\sqrt{\mathrm{Re}^2+\mathrm{Im}^2} \]
  FFT 的每个输出频点都是一个复数，要画出频谱就要算它的模长。
- **平方求和（re²＋im²）**：幅度的平方，等于实部平方加虚部平方。它**不需要开方**就能算出来，且是纯组合逻辑（零延迟）。
- **开方（√）**：CORDIC 算法实现，有 8 拍流水线延迟，在本工程里还被设计成「逐点复位」的串行处理，是最慢的一级。
- **三块 RAM**：ram1 存 ADC 原始采样（10 位），ram2 存平方和（21 位），ram3 存开方后的幅度（10 位）。它们都是「同步写＋组合读」的双端口寄存器内存。
- **时钟域**：系统主时钟 `clk`（200 MHz）驱动主状态机、三块 RAM 与开方模块；`clk_100`（100 MHz）驱动 FFT；ADC 走自己的 `clock_adc_out` 时基。

一个关键直觉先记住：**平方求和是「免费的」，开方是「昂贵的」**。这条链的形状几乎完全由这个不对称决定。

## 3. 本讲源码地图

本讲涉及的关键文件与作用：

| 文件 | 模块 | 在 DSP 链中的位置 |
|---|---|---|
| [verilog files/TOP.v](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v) | `TOP` | 顶层编排器：例化全部模块，并用主状态机把整条链按阶段调度起来 |
| [verilog files/Fourier.v](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/Fourier.v) | `Fourier` | Xilinx FFT 黑盒 IP 的封装壳，链路的第 2 级 |
| [verilog files/Square.v](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/Square.v) | `Square` | Xilinx 乘法器 IP 封装，做 10×10→20 位平方 |
| [verilog files/Sum.v](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/Sum.v) | `Sum` | Xilinx 加法器 IP 封装，做 20＋20→21 位求和 |
| [verilog files/Radical.v](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/Radical.v) | `Root_square` | Xilinx CORDIC 开方 IP 封装，链路的最后一级 |

> 命名提醒（u1-l2 已详述）：文件名常与模块名错位，例如 `Radical.v` 里写的模块叫 `Root_square`。本讲一律以 TOP.v 里的**例化名**（`ram_adc`／`ram_fft_20bit`／`ram_fft_10bit`）为准，并分别称它们为 ram1／ram2／ram3。

## 4. 核心概念与源码讲解

### 4.1 整条 DSP 信号链的数据通路全景

#### 4.1.1 概念说明

一条「幅度谱计算链」要解决的问题是：**给定一段时域采样波形，算出它的频域幅度谱**。数学上分两步：

1. 做一次 FFT，把 N 个时域采样变成 N 个复数频点 \( X[k]=\mathrm{Re}_k+j\,\mathrm{Im}_k \)。
2. 对每个频点算模长 \( |X[k]|=\sqrt{\mathrm{Re}_k^2+\mathrm{Im}_k^2} \)，得到幅度谱。

本工程把第 2 步进一步拆成「平方求和」和「开方」两段，并用三块 RAM 把四段串起来：

```
adc_read ──we──▶ ┌──── ram1 (10bit) ────┐
                 │ 原始时域采样           │
                 └──────────┬────────────┘
                 addr_r=│ (组合读，MUX 切换)
                        ▼
            buffer → decoder(偏移→补码) → xn_re ─┐
                                                ▼  ┌── Fourier (FFT, clk_100) ──┐
                                  xn_im = 0 ───────▶│ 输出 xk_re, xk_im (各10bit) │
                                                    └──┬───────────────┬──────────┘
                            ┌──────────────────────────┘               └──────────────┐
                            ▼                                                        ▼
                  sq_real: xk_re² (20bit)                               sq_im: xk_im² (20bit)
                            └──────────────────────────┬───────────────────────────────┘
                                                       ▼
                                              Sum: re²+im² (21bit)
                                                       │ we2（fft_write_state 期间写）
                                                       ▼
                                          ┌──── ram2 (21bit) ────┐
                                          │ 幅度平方 re²+im²      │
                                          └──────────┬───────────┘
                                            addr_r=│ (组合读，cnt_s)
                                                     ▼
                                   Root_square: √(·)  (20→11bit，8 拍延迟，逐点串行)
                                                     │ we3（square_state 循环期间写）
                                                     ▼
                                          ┌──── ram3 (10bit) ────┐
                                          │ 幅度谱 |X|（取低 10 位）│ ──▶ data_send ──▶ 上传 PC
                                          └──────────────────────┘
```

整条链的核心设计取舍是：**把组合的、零延迟的「平方求和」夹在 FFT 输出和 ram2 之间，让它「免费」搭 FFT 卸载的便车；把昂贵的、串行的「开方」单独留到最后一段，对 ram2 里的中间结果逐点处理。**

#### 4.1.2 核心流程

用伪代码描述整条链的逐级数据演化（一个频点 k）：

```
sample[i]      = adc_read[i]                      # 10 位二进制偏移码
ram1[i]        = sample[i]                        # 时域波形
xn_re[i]       = decoder(ram1[i])                 # 10 位二进制补码（FFT 要补码）
X[k]           = FFT({xn_re, xn_im=0})            # 复数频点: xk_re[k], xk_im[k]（各 10 位补码）
in1_s[k]       = xk_re[k] * xk_re[k]              # 20 位（sq_real，a 与 b 接同一信号即平方）
in2_s[k]       = xk_im[k] * xk_im[k]              # 20 位（sq_im）
sum[k]         = in1_s[k] + in2_s[k]              # 21 位（加法进位 +1）
ram2[k]        = sum[k]                           # 幅度平方
square_out[k]  = sqrt(ram2[k])                    # 11 位（CORDIC），实际 2048→... 流水
ram3[k]        = square_out[k][9:0]               # 10 位幅度谱
```

位宽沿链路的演化可以画成一条「膨胀—压缩」曲线：

\[ \underbrace{10}_{\text{采样/补码}} \xrightarrow{\times 10} \underbrace{20}_{\text{平方}} \xrightarrow{+20\,(+1\,\text{进位})} \underbrace{21}_{\text{平方和}} \xrightarrow{\sqrt{\cdot}} \underbrace{11\,(\text{取}\,10)}_{\text{幅度}} \]

- **乘法翻倍**：两个 10 位数相乘最多 20 位。
- **加法进位**：两个 20 位相加最多 21 位，这就是 ram2 必须用 21 位宽的原因（u3-l2 已证）。
- **开方压缩**：开方把数值压回原量级，例如最大幅度平方 \( \approx 2^{20} \) 开方后 \( \approx 2^{10}<1024 \)，正好回到 10 位，故 ram3 又缩回 10 位。

#### 4.1.3 源码精读

整条链的「接线图」几乎全部写在 TOP.v 的例化区。按数据流顺序逐段看：

**① 采集与 ram1** —— ADC 的 10 位并行数据 `adc_read` 在写使能 `we` 下同步写入 ram1：

[verilog files/TOP.v:128-134](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L128-L134) —— 例化 `ram_adc`（ram1），`data_in` 接 `adc_read`，`data_out` 接 `buffer`，读地址 `addr_r` 接 `ram_read`（由 MUX 在 FFT 读 / 上传读之间切换）。

**② 偏移码→补码→FFT 输入** —— `buffer`（ram1 的组合读出）经 `decoder` 转成补码后喂给 FFT 实部，虚部恒 0：

[verilog files/TOP.v:200-201](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L200-L201) —— `decoder dec` 把 `buffer`（二进制偏移）转成 `decoder_out`（二进制补码）。
[verilog files/TOP.v:154-170](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L154-L170) —— `Fourier FFT` 例化，`xn_re` 接 `decoder_out`、`xn_im` 接 `10'b0`；输出 `xk_re`/`xk_im` 各 10 位。注意 FFT 跑在 `clk_100`，与系统 200 MHz 的 `clk` 不同域（u3-l1 已述）。

**③ 平方求和** —— 两个 `Square` 实例分别平方实部、虚部，再用 `Sum` 加起来。这是**纯组合逻辑**，没有任何时钟与延迟：

[verilog files/TOP.v:172-182](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L172-L182) —— `sq_real`/`sq_im` 把 `a` 与 `b` 都接同一个信号（`xk_re` 或 `xk_im`），让通用乘法器退化为平方器；`adder` 把两个 20 位平方加成 21 位 `sum`。

**④ 写 ram2** —— 求和结果 `sum` 写入 ram2，写使能 `we2` 仅在 FFT 卸载阶段打开：

[verilog files/TOP.v:137-142](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L137-L142) —— 例化 `ram_fft_20bit`（ram2），`data_in` 接 `sum`，写地址 `addr` 直接接 FFT 自己递增的 `index_out`，读地址 `addr_r` 接 `cnt_s`。

**⑤ 开方** —— ram2 的组合读出 `out_fft`（21 位）取低 20 位喂给 `Root_square`：

[verilog files/TOP.v:184-188](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L184-L188) —— `root_square` 例化，`x_in` 接 `out_fft[19:0]`，输出 `square_out`（11 位）与就绪信号 `sqr_rdy`，复位脚 `sclr`。

**⑥ 写 ram3** —— 开方结果取低 10 位写入 ram3，成为最终的幅度谱：

[verilog files/TOP.v:145-150](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L145-L150) —— 例化 `ram_fft_10bit`（ram3），`data_in` 接 `square_out[9:0]`，`data_out` 接 `data_send`（后续上传用），写使能 `we3`。

把 6 段例化连起来读，就是上面那张数据流框图的源码原文。TOP.v 头部的注释也把这 8 步算法说得非常清楚，值得对照一读：

[verilog files/TOP.v:4-18](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L4-L18) —— 作者用 8 步注释概括了整条链：采集存 ram1 → 装载进 FFT → 算 FFT → 卸载经平方+加法存 ram2（得 re²＋im²）→ 开方（8 拍）→ 存 ram3 → 发 PC → LabVIEW 显示。

#### 4.1.4 代码实践

**实践目标**：用「手指跟踪法」走通一个具体频点，确认整条链的接线没有断点。

**操作步骤**：
1. 打开 [verilog files/TOP.v:154-188](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L154-L188)。
2. 假设 FFT 当前输出第 k 个频点 `xk_re=10'sd100`、`xk_im=10'sd50`（十进制有符号，示例值）。
3. 顺着连线依次手算：`in1_s`＝？`in2_s`＝？`sum`＝？`out_fft`（＝`sum`）＝？开方后 `square_out` 约等于多少？
4. 在每一步旁边标注它发生在哪个例化模块里、写入哪块 RAM。

**预期结果**：`in1_s=10000`、`in2_s=2500`、`sum=12500`、`square_out≈111`（\( \sqrt{12500}\approx111.8 \)，取整）。整条链的信号名应能首尾相接：`xk_re→in1_s`、`xk_im→in2_s`、`(in1_s,in2_s)→sum→ram2→out_fft→square_out→ram3`。

> 说明：以上数值为**示例代码**，用于验证你对连线的理解；真实 FFT 输出是 10 位补码，取值范围待结合 FFT 缩放系数 `scale_sch=12'b001010101011` 确认。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `sq_real` 的端口 `.b(xk_re)` 改成 `.b(10'd2)`，整条链算出的还是幅度平方吗？
**答案**：不是。平方器靠 `a`、`b` 接同一信号实现平方；改成常数 2 后，`sq_real` 输出的是 `2·xk_re` 而非 `xk_re²`，ram2 里就不再是 re²＋im²，开方后也得不到正确幅度。

**练习 2**：为什么 `xn_im` 恒为 0，链路却仍然有意义？
**答案**：因为输入是**实信号**（只有实部采样）。实信号的 FFT 满足共轭对称性 \( X[N-k]=X[k]^* \)，频谱幅度关于中点对称，所以只需算一半频点。这也解释了为什么后续开方只处理前半段（见 4.3）。

**练习 3**：链路里哪一段是组合逻辑、哪一段是时序逻辑？
**答案**：`decoder`＋`Square`×2＋`Sum`（FFT 输出到 ram2 写入之间）是纯组合逻辑，零延迟；FFT 与 `Root_square` 是时序逻辑，分别有各自的握手与流水线延迟。

---

### 4.2 三块 RAM 作为级间缓冲

#### 4.2.1 概念说明

为什么要在 FFT、平方求和、开方之间插三块 RAM，而不是让它们直接首尾相连？因为这条链的**四级工作节奏完全不同**：

- 第 1 级（采集）跑在 ADC 时钟域，按采样率慢慢填；
- 第 2 级（FFT）跑在 100 MHz，一次性吞入整帧、一次性吐出整帧；
- 第 3 级（平方求和）是组合逻辑，必须依附于某一级的时序才能「流动」；
- 第 4 级（开方）跑在 200 MHz，却只能**一个点一个点**地算（8 拍／点）。

节奏对不齐，就不能直接对接。**RAM 的作用就是「蓄水池」**：前级按自己的节奏把结果灌进去，后级再按自己的节奏取出来。三块 RAM 恰好把链路切成三段相对独立的工作区间，每段可以在自己的时钟域、自己的状态下从容工作，互不阻塞。

这个「级间缓冲」思想在硬件信号处理里非常通用，本质就是软件里「生产者—消费者—队列」的电路版本。

#### 4.2.2 核心流程

三块 RAM 都是同一个模板的三份拷贝：**同步写**（`always @(posedge clk) if(we) mem[addr]<=data_in;`）＋**组合读**（`assign data_out=mem[addr_r];`），读写地址分离（双端口）。区别只在位宽与「谁在写、谁在读、写使能何时打开」：

| RAM | 例化名 | 位宽 | 写入者（写使能） | 写地址来源 | 读取者（读地址） | 在链中的角色 |
|---|---|---|---|---|---|---|
| ram1 | `ram_adc` | 10 | ADC 采集（`we`，`acq_state` 期间为 1） | `ADR`（ADC 域 state2 递增） | FFT（`index_in`）／上传（`cnt_waveform`），由 `sel2` 切换 | 时域波形缓冲；**被读两次** |
| ram2 | `ram_fft_20bit` | 21 | 平方求和结果（`we2`，`fft_write_state` 期间为 1） | `index_out`（FFT 自增） | 开方（`cnt_s`） | FFT 输出与开方之间的中间缓冲 |
| ram3 | `ram_fft_10bit` | 10 | 开方结果（`we3`，`square_state` 循环期间） | `cnt_s` | 上传（`ADR_r`，由 `sel` 切换） | 最终幅度谱缓冲 |

一个值得注意的设计：**写使能 `we`／`we2`／`we3` 都由主状态机显式控制**，而不是常开。这意味着每块 RAM 只在「属于它的那个阶段」才被写入，阶段切换时立即关闭写使能——这是主 FSM 协调整条链的核心手段。

模板里还有一个 `carry` 满标志（写地址走到 2047 时拉高），但**只有 ram1 真正用到它**（主 FSM 轮询 `carry` 来判断「采集满了一帧，该进 FFT 了」）；ram2／ram3 的 `carry` 悬空，因为它们的写结束条件用的是 FFT 的 `index_out` 和开方循环的 `cnt_s`，而非地址溢出。

#### 4.2.3 源码精读

**模板结构**（以 `SRAM.v` 为例，内含模块 `SRAM2`，21 位）：

[verilog files/SRAM.v:15-21](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/SRAM.v#L15-L21) —— `reg [20:0] mem [0:2047];` 是 2048 深的寄存器数组；`if(we) mem[addr]<=data_in;` 是同步写，`assign data_out=mem[addr_r];` 是组合读，`addr` 与 `addr_r` 分离正是双端口的自由度；`carry` 在 `addr==2047` 时拉高。

**ram1 被读两次** —— 注释说得直白：

[verilog files/TOP.v:127-134](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L127-L134) —— 注释「the ram is read two times every process; first time by the FFT, and second time by serial module」说明 ram1 的同一份波形，先喂给 FFT 做变换，再喂给上传通道发原波形；读地址 `addr_r=ram_read` 由 `mux_ram1` 在 `index_in`（FFT 读）与 `cnt_waveform`（上传读）间切换。

**写使能的状态机控制** —— 三块 RAM 的写使能分别在不同状态被置位／清零：

[verilog files/TOP.v:330-336](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L330-L336) —— `acq_state`：ram1 写使能 `we=1`，`carry` 一拉高就关 `we` 并跳 `fft_state`。
[verilog files/TOP.v:339-354](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L339-L354) —— `fft_state`／`fft_write_state`：`we2` 在 `fft_state` 一进来就置 1（打开 ram2 写），到 `index_out==1022` 时关 `we2`、开 `we3`（切到 ram3 写）。
[verilog files/TOP.v:357-391](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L357-L391) —— `square_state`～`square_state5`：在这五个状态的小循环里，`we3` 被 `square_state3` 清 0、`square_state5` 重新置 1，逐点配合开方流水线节拍写 ram3。

读这三段状态机时，抓住「**谁在写哪块 RAM**」这条主线，整条链的调度就一目了然。

#### 4.2.4 代码实践

**实践目标**：把三块 RAM 的「写使能时间线」对齐到主 FSM 上，理解级间隔离。

**操作步骤**：
1. 列一张表，行是 `we`／`we2`／`we3`，列是 `acq_state`／`fft_state`／`fft_write_state`／`square_state` 循环／`send_state`。
2. 在 [TOP.v:330-397](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L330-L397) 里逐状态填入每个 `we*` 是 0 还是 1。
3. 检查：同一时刻是否只有一块 RAM 在写？阶段切换时旧 RAM 的写使能是否被及时关掉？

**预期结果**：应得到一张「分段独占」的时间线——`acq_state` 只写 ram1，`fft_write_state` 只写 ram2，`square_state` 循环只写 ram3，互不重叠。这正是级间缓冲能稳定工作的前提。

**待本地验证**：`we2` 在 `fft_state`（计算阶段）就被置 1，但此时 FFT 还没卸载数据，ram2 会不会被误写？结合 `index_out` 在计算阶段是否为 0 来判断（提示：写地址 `addr=index_out`，若 `index_out` 此时为 0，则只是反复刷新 ram2[0]，待结合仿真确认）。

#### 4.2.5 小练习与答案

**练习 1**：ram2 和 ram3 的 `carry` 为什么悬空？
**答案**：它们的写结束不靠地址溢出判断。ram2 写结束靠 FFT 卸载计数 `index_out==1022`；ram3 写结束靠开方循环计数 `cnt_s` 到 1023。所以 `carry` 对它们没用，只有 ram1 用 `carry` 表示「采集满帧」。

**练习 2**：如果没有 ram2，把 `Sum` 的输出直接接给 `Root_square`，会出现什么问题？
**答案**：FFT 卸载是一次性快速流式输出（约 100 MHz 每拍一个点），而开方是 8 拍／点的串行处理，两者速率严重失配。直接对接会让大部分 FFT 输出来不及被开方就丢失。ram2 的作用就是先快速把整帧 re²＋im² 存下来，再让开方慢慢取用。

**练习 3**：ram1 为什么用组合读（`assign data_out=mem[addr_r]`）而不是再寄存一拍？
**答案**：FFT 核自带地址递增（`xn_index` 回送成读地址），需要当拍给出样本才能配合 FFT 的加载握手；组合读保证「给地址即出数据」，零延迟地桥接 200 MHz RAM 与 100 MHz FFT 两个时钟域（u3-l1 已述）。

---

### 4.3 一帧数据的端到端时序走查

#### 4.3.1 概念说明

前面两节讲了「链长什么样」和「RAM 怎么隔离各级」。这一节把镜头拉远，看**一整帧数据从头到尾要走多久、卡在哪里**。这是本讲的核心综合：把 u3-l1／u3-l2／u3-l3 的模块级时序拼成帧级时序。

一个关键结论先放这里：**整条链的总耗时几乎全花在开方上**。这是因为平方求和「免费」搭了 FFT 卸载的便车，而开方却被设计成逐点串行、每点还要复位一次 CORDIC 流水线。理解了这一点，就理解了「为什么开方放在最后」——不是数学上必须最后（模长公式里开方确实在最外层），而是**工程上必须把最慢的操作隔离成独立阶段，否则会拖垮前面已经很快的 FFT 卸载**。

至于「为什么幅度存入 ram3 而不是中途某块 RAM」：因为开方是最后一段运算，它把 ram2 里的 21 位「幅度平方」压缩回 10 位「幅度」，这正是最终要显示并上传的量。把它存进 ram3，ram3 就成了「成品仓库」，上传通道只需要从 ram3 取数即可，不必再过任何运算。

#### 4.3.2 核心流程

一帧数据从 `acq_state` 结束（ram1 刚填满）到 ram3 存满幅度谱，依次经过四个阶段。下表给出主 FSM 状态、时钟域、大致耗时与关键判据（带「待确认」的为依赖二进制 IP 配置、需仿真核实的部分）：

| 阶段 | 主 FSM 状态 | 时钟域 | 大致耗时 | 结束判据 |
|---|---|---|---|---|
| ① 采集填 ram1 | `acq_state`（轮询 `carry`） | 写：`clock_adc_out`；轮询：`clk` | 2048 样本 / 采样率；@100 MSPS ≈ 20.5 µs | `carry==1` |
| ② 启动并算 FFT | `fft_state` | `clk_100`（100 MHz） | ~N 量级周期，**待确认**（IP 配置在二进制工程包内） | `edone==1` |
| ③ 卸载＋平方求和→ram2 | `fft_write_state` | 卸载：`clk_100`；写 ram2：`clk` | ~1023 周期 @100 MHz ≈ 10 µs，**待确认** | `index_out==1022` |
| ④ 逐点开方→ram3 | `square_state`～`square_state5` | `clk`（200 MHz） | ~13 拍/点 × 1023 点 ≈ 66 µs，**待验证** | `cnt_s` 达到 1023 |

> 说明：阶段 ① 的采集其实在 `trig_state` 触发条件满足后就由 ADC 域的 state2 子状态机持续进行，主 FSM 只是在 `acq_state` 等 `carry`；这里从「采集完成」起算。阶段 ④ 的「每点约 13 拍」来自 u3-l3 的逐拍分析（等 `sqr_rdy` 约 8 拍 ＋ `square_state2~5` 共约 4 拍）。

把四个阶段加起来，**一帧的 DSP 处理总耗时大致在 100 微秒量级，其中开方独占约六成**。这也是 u6-l4 会讨论「把开方改成全流水线」作为优化方向的动机。

#### 4.3.3 源码精读

逐阶段对照主 FSM（状态参数定义在 [TOP.v:213-227](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L213-L227)）：

**阶段 ①**：`acq_state` 关掉 ram1 写、切入 FFT——

[verilog files/TOP.v:330-336](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L330-L336) —— `carry==1` 表示 ram1 写到 2047、满帧；随即 `we=0`（停止采集）、`res_serial<=0`，状态跳 `fft_state`。

**阶段 ②**：`fft_state` 拉起 `start_fft` 并等 FFT 算完——

[verilog files/TOP.v:339-343](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L339-L343) —— `we2<=1`（提前打开 ram2 写）、`start_fft<=1`（启动 FFT 核）；`edone==1`（FFT「早 done 一拍」信号，u3-l1 已述）一到就跳 `fft_write_state`。`Fourier` 封装见 [Fourier.v:8-50](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/Fourier.v#L8-L50)。

**阶段 ③**：`fft_write_state` 一边卸载、一边把组合的平方求和结果写入 ram2——

[verilog files/TOP.v:346-354](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L346-L354) —— `start_fft<=0`；当 `index_out==10'b1111111110`（＝1022）时关 `we2`、开 `we3`、`cnt_s` 清零、`sclr<=0`（放行开方），跳 `square_state`。注意：`Square`＋`Sum` 在这里**没有专门的状态消耗时间**，它们作为组合逻辑，数据在 FFT 卸载的每一拍自然流过并写入 ram2（[Square.v:8-18](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/Square.v#L8-L18) 与 [Sum.v:8-18](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/Sum.v#L8-L18)）。

**阶段 ④**：`square_state`～`square_state5` 五状态小循环，逐点开方写入 ram3——

[verilog files/TOP.v:357-391](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L357-L391) —— `square_state`：若 `cnt_s<11'b1111111111`（＝1023）则等 `sqr_rdy` 拉高后进入循环体；否则（`cnt_s` 到 1023）关 `we3`、`sclr<=1`，跳 `send_state`。循环体 `square_state2→3→4→5` 依次：过渡→打 `sclr` 复位开方核并关 `we3`→`cnt_s+1`（推进 ram2 读地址与 ram3 写地址）→放行 `sclr` 并重开 `we3`，回到 `square_state` 等下一个 `sqr_rdy`。开方封装见 [Radical.v:6-23](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/Radical.v#L6-L23)。

阶段 ④ 结束后 ram3 已存满幅度谱，整条 DSP 链完成；后续 `send_state` 起把 ram3 的谱与 ram1 的波形上传给 PC，那部分属于 UART 与打包子状态机（Unit 4 与 u5-l3），不在本讲范围内。

> 关于 FFT 点数：链路里地址索引 `xn_index`／`xk_index` 为 11 位（0…2047）、ram1／ram2／ram3 均为 2048 深、`scale_sch` 为 12 位（恰好匹配 Radix-4 的级数编码），这些证据共同指向**一次 2048 点变换**；又因输入为实信号（`xn_im=0`），频谱共轭对称，故开方只处理前半段约 1023 个点（`cnt_s<1023`）。FFT 的确切点数与架构藏于二进制 IP 工程包，**待确认**。

#### 4.3.4 代码实践（本讲主任务）

**实践目标**：写一段「帧级时序叙述」，把一帧数据从 `acq_state` 采集完成到 ram3 存满幅度谱的过程讲清楚，标注每段耗时与不确定处。

**操作步骤**：
1. 重读 [TOP.v:330-391](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L330-L391) 的四个阶段。
2. 用一段连贯的文字（不超过 250 字）描述：ram1 满后，系统先做什么、等多久；然后 FFT 怎么启动、何时结束；平方求和在哪一段「顺便」完成；最后开方循环如何逐点把 ram2 变成 ram3。
3. 在每个耗时数字后标注依据（时钟频率、点数、每点拍数），对无法从源码直接确认的（如 FFT 计算精确周期数）标注「待本地验证／待仿真」。

**预期结果（参考叙述）**：

> ram1 写到 2047 触发 `carry`，主 FSM 关 `we` 进入 `fft_state`。此时 `start_fft` 拉高，FFT 核在 `clk_100`（100 MHz）下装载 ram1 的 2048 个补码样本并计算，耗时约 N 量级个周期（**待仿真确认**）；`edone` 拉高前一拍，状态切到 `fft_write_state`。在卸载阶段，FFT 每拍吐出一个复数频点 `xk_re`/`xk_im`，纯组合的 `Square`×2＋`Sum` 顺手算成 `re²+im²` 写入 ram2（写使能 `we2`），地址用 FFT 自增的 `index_out`，约 1023 拍完成（@100 MHz ≈ 10 µs，**待确认**）。`index_out==1022` 后关 `we2`、开 `we3`，进入 `square_state` 五状态循环：逐点读 ram2、开方（8 拍延迟，`sclr` 复位）、把低 10 位写 ram3，每点约 13 拍，1023 点共约 66 µs（@200 MHz，**待验证**）。循环结束后 ram3 存满幅度谱，DSP 链收尾。

**需要观察的现象**（若有仿真条件）：在波形窗口里同时看 `state`／`we2`／`we3`／`edone`／`index_out`／`sqr_rdy`／`cnt_s`，应能看到四段「亮带」依次出现且互不重叠，`sqr_rdy` 在 `square_state` 循环里周期性脉动 1023 次。

#### 4.3.5 小练习与答案

**练习 1**：如果把开方循环的判据 `cnt_s<11'b1111111111` 改成 `cnt_s<11'b00001111111`（＝127），ram3 里会得到什么？
**答案**：ram3 只会存前 127 个频点的幅度，剩余频点不被开方、ram3 对应位置保持旧值。这会丢失绝大部分频谱信息，说明该判据直接决定了「谱分辨率」。

**练习 2**：为什么阶段 ③ 的平方求和「不占额外时间」？
**答案**：因为它是组合逻辑，数据流过即出结果。FFT 卸载每给出一拍 `xk_re`/`xk_im`，`Square`＋`Sum` 当拍就给出 `sum`，与 FFT 卸载节拍完全重叠，不需要独立的状态或时钟周期——这就是「免费搭便车」。

**练习 3**：整条链最该被优化的瓶颈是哪一段？为什么？
**答案**：开方（阶段 ④）。它独占约六成帧时间，且是逐点串行。把 CORDIC 开方改成「不每点复位、连续喂入」的全流水线，理论上可以把每点从约 13 拍降到 1 拍吞吐，大幅压缩帧耗时（这是 u6-l4 的改进方向之一）。

## 5. 综合实践

**任务：为整条 DSP 链画一张「周期预算表」并设计一套调试埋点。**

把本讲四节的知识用起来：

1. **周期预算表**：用一张表列出 ram1 满→ram3 满之间四个阶段的「主时钟周期数估算」。对能从源码确定的（阶段 ④：1023 点 × 约 13 拍）给出具体数；对不能确定的（阶段 ②／③ 的 FFT 周期数）标「待仿真」，并写出你打算在仿真里看哪个信号来测（提示：数 `edone` 到 `start_fft` 之间的 `clk_100` 周期数；数 `fft_write_state` 期间 `index_out` 从 0 到 1022 的周期数）。
2. **调试埋点设计**：假设你可以改 TOP.v（仅用于调试，不改功能），设计最少几个「阶段计数器」——例如给每个阶段各加一个 `reg [31:0]` 计数器，在进入该状态时清零、每拍 ＋1、离开时锁存，从而在硬件上直接测出每段真实耗时。写出每个计数器的清零／锁存条件分别对应 [TOP.v:330-391](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L330-L391) 里的哪些状态跳转。
3. **思考题**：如果要把这条链改成「流水线连续处理」（上一帧还在开方时下一帧就能采集），三块 RAM 需要怎样改造？（提示：考虑双缓冲／乒乓 RAM，以及 ram1 此时正在被 FFT 读、还能否同时被新帧写。）

> 本实践为**源码阅读型 + 设计型实践**，不要求上板；重点是建立「把模块拼成链、再从链反推时序与瓶颈」的工程视角。计数器代码为**示例代码**，非项目原有内容。

## 6. 本讲小结

- 整条 DSP 链是 `adc_read → ram1 → decoder → Fourier(FFT) → Square×2 ＋ Sum → ram2 → Root_square → ram3`，输出即幅度谱 \( |X[k]|=\sqrt{\mathrm{Re}^2+\mathrm{Im}^2} \)。
- 位宽沿链「膨胀—压缩」：10 → 20（平方）→ 21（求和进位）→ 11 取 10（开方），ram2 最宽、ram3 与 ram1 同为 10 位。
- 三块 RAM 是级间缓冲：ram1 隔开采集与 FFT（且被读两次：一次喂 FFT、一次上传原波形），ram2 隔开快速 FFT 卸载与慢速开方，ram3 是成品幅度谱仓库。
- 平方求和是组合逻辑，搭 FFT 卸载便车「免费」完成；开方有 8 拍延迟且逐点串行，是最慢的一级。
- 主 FSM 用 `we`／`we2`／`we3` 三个写使能按阶段独占地调度三块 RAM，阶段切换时及时关闭旧写使能。
- 一帧 DSP 处理总耗时约百微秒量级，开方独占约六成，是主要瓶颈——这也解释了「为什么开方放最后、为什么 ram3 存最终幅度」。

## 7. 下一步学习建议

到本讲为止，FPGA 内部 `ram1→FFT→平方求和→ram2→开方→ram3` 的 DSP 主线已经完整。接下来可以按两条线推进：

- **数据出口线（Unit 4）**：ram3 里的幅度谱（和 ram1 的原波形）是怎么通过 UART 发回 PC 的？建议读 u4-l1（UART 接收机）与 u4-l2（UART 发射机），重点看 `serialt` 如何把 `aggregated` 拆成两字节串行发出。
- **全局编排线（Unit 5）**：本讲只覆盖了「采集完成后」的处理。要理解「PC 发一个 `P` 如何触发整条链」「触发与斜率子状态机怎么决定何时开始采集」「发送时如何加 F／T 帧头」，请读 u5-l1（主 FSM 与命令协议）、u5-l2（触发/斜率子 FSM）、u5-l3（发送打包子 FSM）。

源码层面，建议继续精读 [TOP.v:246-413](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L246-L413) 的主状态机全貌，把本讲的「DSP 链阶段」与 Unit 5 将讲的「命令解析／触发／打包」状态拼成一张完整的状态图。
