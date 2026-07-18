# 复数运算与流水线辅助原语

## 1. 本讲目标

OpenOFDM 的接收链路从「I/Q 样本」一路走到「字节」，中间需要做大量的复数乘法、复数幅值比较、滑动平均和流水线对齐。作者把这些反复出现的小运算提炼成一组「乐高积木」式的通用原语（primitive），几乎每个算法模块（`sync_short`、`sync_long`、`equalizer`、`rotate`、`phase`）都站在它们之上。

本讲不涉及任何 OFDM 算法本身，只回答一个问题：**这些积木是怎么实现的，怎么对齐时序的？** 学完后你应当能够：

1. 读懂 `complex_mult` 的「输入寄存 → Xilinx IP → 输出寄存 + strobe 延时」封装风格，并说出它的握手时序；
2. 解释 `complex_to_mag` 采用 \(\alpha=1,\ \beta=\tfrac14\) 的幅值近似算法及其误差来源；
3. 理解 `complex_to_mag_sq` 用「信号乘自己的共轭」这一技巧来计算幅值平方；
4. 区分三个看似都「把信号往后挪」的原语：`delayT`（固定拍数延时）、`moving_avg`（滑动窗口平均）、`calc_mean`（两路带符号平均）。

## 2. 前置知识

- **复数表示**：一个基带复样本写作 \(z = I + jQ\)，\(I\) 是同相分量（实部）、\(Q\) 是正交分量（虚部）。复数乘法满足
  \[
  (a_r+j a_q)(b_r+j b_q) = (a_r b_r - a_q b_q) + j(a_r b_q + a_q b_r).
  \]
  在本项目的端口命名里，`_i` 后缀表示 I（同相/实部），`_q` 后缀表示 Q（正交/虚部），不要和「imaginary」混淆。
- **流水线与握手**：OpenOFDM 全项目采用「数据 + strobe」的单向握手风格——数据有效时配套拉高一个时钟周期的 strobe 脉冲，下游只在 strobe 有效时消费数据（见 u1-l4、u2-l2）。
- **定点小技巧**：补码取负用 `~x+1`；有符号数除 2 用算术右移 `>>>1`；除以 \(2^k\) 用右移 `>>k`。FPGA 上要尽量避免 `sqrt` 和除法，所以幅值都用近似。
- **为什么需要近似幅值**：精确幅值 \(\sqrt{I^2+Q^2}\) 需要开方，硬件代价高。在「只需要比较大小、做门限判决」的场景（包检测、互相关取峰），近似幅值完全够用，且省掉一个开方器。

## 3. 本讲源码地图

| 文件 | 作用 | 典型消费者 |
| --- | --- | --- |
| [verilog/complex_mult.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/complex_mult.v) | 复数乘法原语：封装 Xilinx `complex_multiplier` IP，并加上输入/输出寄存与 strobe 延时 | `sync_short`、`equalizer`、`rotate`、`complex_to_mag_sq` |
| [verilog/complex_to_mag.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/complex_to_mag.v) | 复数 → 标量幅值的**近似**（\(\alpha=1,\beta=\tfrac14\)） | `sync_short`、`sync_long` |
| [verilog/complex_to_mag_sq.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/complex_to_mag_sq.v) | 复数 → 幅值平方 \(I^2+Q^2\)，用共轭自乘实现 | `sync_short`（算瞬时功率） |
| [verilog/delayT.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/delayT.v) | 固定拍数延时移位寄存器，用于把 strobe/数据对齐到流水线出口 | 几乎每个模块都用（对齐 strobe） |
| [verilog/moving_avg.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/moving_avg.v) | 参数化滑动窗口平均（窗口大小为 \(2^k\)） | `sync_short`（平滑功率、平滑相关） |
| [verilog/calc_mean.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/calc_mean.v) | 两路带符号样本求平均（可选取反） | `equalizer`（两段 LTS 求平均） |
| [verilog/coregen/complex_multiplier.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/complex_multiplier.v) | Xilinx 复数乘法器 IP 的**仿真行为模型**（内部用 DSP48A 硬核） | 被 `complex_mult` 例化 |
| [verilog/usrp2/ram_2port.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/usrp2/ram_2port.v) | 双口 RAM 行为模型 | `moving_avg`（做环形缓冲） |

> 提示：`complex_multiplier.v` 是 coregen IP 的「验证模型」，不可综合，但功能正确，所以无 Xilinx 工具链也能用 iverilog 仿真（详见 u1-l3、u6-l3）。

## 4. 核心概念与源码讲解

### 4.1 complex_mult：复数乘法原语

#### 4.1.1 概念说明

整条接收链里最高频的运算是复数乘法：延迟自相关（`sync_short`）、LTS 互相关（`sync_long` 的 `stage_mult`）、信道估计与均衡（`equalizer`）、频偏旋转（`rotate`）都要做。作者没有让每个模块各写一份乘法器，而是统一封装成一个带握手的 `complex_mult`，内部调用 Xilinx 的 `complex_multiplier` IP（映射到 DSP48A 硬核乘加单元）。

它解决两个问题：(1) 给 IP 包一层统一的端口与时钟域；(2) **让 strobe 沿着数据通路同步延时**，保证 `output_strobe` 拉高的那一拍，`p_i/p_q` 上正好是对应输入的乘积。

#### 4.1.2 核心流程

复数乘积的两个分量（记 \(a=(a_i,a_q)\)、\(b=(b_i,b_q)\)）：

\[
p_i = a_i b_i - a_q b_q,\qquad p_q = a_i b_q + a_q b_i.
\]

封装后的流水线可以理解成三段：

1. **输入寄存**：把 `a_i/a_q/b_i/b_q` 打一拍存进 `ar/ai/br/bi`，送给 IP；
2. **IP 计算**：`complex_multiplier` 内部有多级 DSP48A 流水线，完成上面两个乘积；
3. **输出寄存 + strobe 延时**：把 IP 的 `prod_i/prod_q` 再打一拍输出；与此同时，用一个 `delayT#(DELAY=5)` 把 `input_strobe` 延时相同拍数得到 `output_strobe`。

数据通路有几级寄存，strobe 就用 `delayT` 延时几拍——这是 OpenOFDM 所有原语对齐时序的统一手法。

#### 4.1.3 源码精读

端口与内部寄存声明：[verilog/complex_mult.v:1-27](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/complex_mult.v#L1-L27)。注意输入是四个 16 位分量，输出是两个 32 位乘积（16×16=32）。

例化 Xilinx IP，把项目里的 `a_i/a_q/...` 接到 IP 的 `ar/ai/br/bi`（IP 命名里 `r`=real=I、`i`=imag=Q）：[verilog/complex_mult.v:29-37](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/complex_mult.v#L29-L37)。

```verilog
complex_multiplier mult_inst (
    .clk(clock),
    .ar(ar), .ai(ai), .br(br), .bi(bi),
    .pr(prod_i), .pi(prod_q)
);
```

用 `delayT` 把 strobe 延时 5 拍，与数据通路对齐：[verilog/complex_mult.v:39-45](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/complex_mult.v#L39-L45)。

数据通路本身（输入寄存 + 输出寄存，仅在 `enable` 时更新）：[verilog/complex_mult.v:47-65](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/complex_mult.v#L47-L65)。

```verilog
end else if (enable) begin
    ar <= a_i; ai <= a_q; br <= b_i; bi <= b_q;  // 输入寄存
    p_i <= prod_i; p_q <= prod_q;                 // 输出寄存
end
```

> 小注：文件顶部 `localparam DELAY = 4; reg [DELAY-1:0] delay;` 里的 `delay` 寄存器只在复位时被清零，之后既不写也不读，看上去是历史遗留的未用代码，阅读时可忽略。

#### 4.1.4 代码实践

**目标**：确认 strobe 延时与数据延时是匹配的。

1. 打开 `verilog/complex_mult.v`，数一下从 `input_strobe` 到 `output_strobe` 经过了几拍（`delayT` 的 `DELAY=5`）。
2. 打开一个真实消费者 [verilog/sync_short.v:118-132](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v#L118-L132)，看 `delay_prod_inst` 如何把 `sample_in` 与延迟共轭样本相乘得到自相关 `prod`，并用 `prod_stb` 驱动下游 `moving_avg`。
3. **现象与预期**：`prod` 上每出现一个有效值，`prod_stb` 应在同一拍拉高；`prod_stb` 的延迟就是 `complex_mult` 内部的流水线深度。具体拍数「待本地验证」（可用 u1-l2 的 `make simulate` 跑 24Mbps 样本，在 `dot11.vcd` 里量 `input_strobe` 上升沿到 `output_strobe` 上升沿的时钟数）。

#### 4.1.5 小练习与答案

- **练习 1**：若 \(a=(1,2)\)、\(b=(3,-1)\)（即 \(a=1+2j\)、\(b=3-j\)），手算 `complex_mult` 的 `p_i`、`p_q`。
  - **答**：\(p_i=1\cdot3-2\cdot(-1)=5\)，\(p_q=1\cdot(-1)+2\cdot3=5\)。与 \( (1+2j)(3-j)=5+5j \) 一致。
- **练习 2**：为什么 `output_strobe` 必须用 `delayT` 单独延时，而不能直接把 `input_strobe` 接到输出？
  - **答**：数据要走完「输入寄存 + IP 流水线 + 输出寄存」若干拍后才有效；strobe 若不跟着延时同样的拍数，就会在数据还是垃圾时提前拉高，下游错位。

### 4.2 complex_to_mag 与 complex_to_mag_sq：幅值与幅值平方

#### 4.2.1 概念说明

很多判决只关心复数的「大小」而不关心方向：功率门限、互相关取峰都只需要一个标量幅值。精确幅值 \(\sqrt{I^2+Q^2}\) 要开方，太贵；于是 OpenOFDM 提供两个廉价替代：

- `complex_to_mag`：近似幅值，只用取绝对值、比较、加法和右移，**零乘法零开方**；
- `complex_to_mag_sq`：精确的幅值平方 \(I^2+Q^2\)，复用一个 `complex_mult` 实现，适合需要严格能量度量的场合（如 `sync_short` 算瞬时功率做归一化分母）。

#### 4.2.2 核心流程

**近似幅值（α-max + β-min 估计器）**：

\[
\widehat{|z|} = \alpha\cdot\max(|I|,|Q|) + \beta\cdot\min(|I|,|Q|),\qquad \alpha=1,\ \beta=\tfrac14.
\]

即 `mag = max + (min>>2)`。直觉：复数幅值介于 \(|I|\) 与 \(\sqrt{I^2+Q^2}\le |I|+|Q|\) 之间，用「大者为主、小者补贴一点」就能逼近。

**误差来源**：把 \(|I|=r\cos\theta\)、\(|Q|=r\sin\theta\)（\(0\le\theta\le\pi/4\)，由对称性），则估计/真值 = \(\cos\theta+\tfrac14\sin\theta\)。在 \(\theta=\pi/4\)（即 \(|I|=|Q|\)，45°）处取到最严重的**低估**：
\[
\widehat{|z|}/|z| = \tfrac{\sqrt2}{2}\cdot(1+\tfrac14) \approx 0.884 \quad\Rightarrow\quad \text{约 } -11.6\%.
\]
而在 \(\tan\theta=\tfrac14\)（约 14°）处有约 \(+3\%\) 的高估。源码注释称「avg err 0.006」（平均相对误差约 0.6%），可见该估计平均上几乎无偏，最坏低估在 45° 附近——对「比大小、判门限」来说完全够用。

**幅值平方（共轭自乘）**：注意到
\[
z\cdot\overline{z} = (I+jQ)(I-jQ) = I^2+Q^2,\qquad \text{虚部自动抵消}.
\]
所以只要让 `complex_mult` 算 \((I,Q)\times(I,-Q)\)，取实部就是幅值平方，虚部恒为 0。把 \(Q\) 取负就是「共轭」。

#### 4.2.3 源码精读

`complex_to_mag` 的端口与延时对齐：[verilog/complex_to_mag.v:1-30](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/complex_to_mag.v#L1-L30)。数据通路是「取绝对值 → 选 max/min → 相加」共 3 级寄存，所以 strobe 用 `delayT#(DELAY=3)`。

近似幅值算法主体（注释里给出了 dspguru 的参考与 α、β、平均误差）：[verilog/complex_to_mag.v:33-52](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/complex_to_mag.v#L33-L52)。

```verilog
// alpha = 1, beta = 1/4    avg err 0.006
abs_i <= i[DATA_WIDTH-1]? (~i+1): i;     // 补码取绝对值
abs_q <= q[DATA_WIDTH-1]? (~q+1): q;
max   <= abs_i > abs_q? abs_i: abs_q;
min   <= abs_i > abs_q? abs_q: abs_i;
mag   <= max + (min>>2);                  // max + min/4
```

`complex_to_mag_sq` 用共轭自乘：把 \((I,Q)\) 与 \((I,-Q)\) 送进 `complex_mult`，实部即幅值平方：[verilog/complex_to_mag_sq.v:19-32](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/complex_to_mag_sq.v#L19-L32)。其中 \(Q\) 取负用 `~q+1`：[verilog/complex_to_mag_sq.v:34-46](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/complex_to_mag_sq.v#L34-L46)。

```verilog
complex_mult mult_inst (
    .a_i(input_i), .a_q(input_q),
    .b_i(input_i), .b_q(input_q_neg),   // b = 共轭(a)
    ...
    .p_i(mag_sq),                        // 实部 = I^2+Q^2
    .output_strobe(mag_sq_strobe)
);
```

#### 4.2.4 代码实践

**目标**：为 `complex_to_mag.v` 补充中文注释，讲清 `max + (min>>2)` 的误差来源。

> 这是「源码阅读型」实践，不修改项目行为。请在**你本地的副本**上添加注释（或写在笔记里），不要改动仓库源码以免影响交叉验证。

建议添加的参考注释（示例代码）：

```verilog
// 幅值近似：|z| ≈ max(|I|,|Q|) + min(|I|,|Q|)/4
// 误差来源：精确幅值是 sqrt(I^2+Q^2)；本式在 |I|=|Q|（45°）处
// 低估最严重，约 -11.6%（0.707*1.25=0.884）；平均相对误差约 0.6%。
// 只用于比大小/判门限（如互相关取峰、功率门限），不用于需要精确幅值处。
mag <= max + (min>>2);
```

完成后，自检：在 45°（`i==q`）情形下，手算 `mag / sqrt(i*i+q*q)` 是否约等于 0.884；在 `q==0` 情形下，`mag` 是否精确等于 `|i|`（此时无误差）。预期：`q==0` 时 `min=0`，`mag=max=|i|`，精确无误。

#### 4.2.5 小练习与答案

- **练习 1**：\(z=(3,4)\)，分别算精确幅值、`complex_to_mag` 的近似幅值、`complex_to_mag_sq` 的幅值平方。
  - **答**：精确 \(=5\)；近似 `max=4, min=3, mag=4+(3>>2)=4+0=4`（注意 `3>>2` 在整数定点下为 0），低估 20%；幅值平方 \(=3^2+4^2=25\)。这说明在位宽较小时 `min>>2` 的截断误差会放大，但工程上幅值通常已放大定点，影响可控。
- **练习 2**：为什么 `complex_to_mag_sq` 的虚部输出可以不接？
  - **答**：共轭自乘的虚部 \(=I\cdot(-Q)+Q\cdot I=0\)，恒为零，所以只需取实部 `p_i` 当作幅值平方。

### 4.3 delayT：固定延时寄存器链

#### 4.3.1 概念说明

`delayT` 是本讲最简单也最基础的原语——一条参数化的移位寄存器，把输入信号原样推迟若干个时钟周期再输出。它存在的唯一理由就是**对齐**：当数据被某一组合逻辑/IP 延时了 N 拍，与之配套的 strobe 也必须被延时 N 拍，否则两者错位。前面 `complex_mult`、`complex_to_mag` 里的 `stb_delay_inst` 都是它。

要把它和另两个「往后挪」的原语区分清楚：

| 原语 | 推进方式 | 用途 |
| --- | --- | --- |
| `delayT` | **每个时钟都移位**（无 enable/strobe） | 给数据或 strobe 加固定拍数延时 |
| `delay_sample`（见 u2-l2） | **每个 strobe 才推进一格**（RAM 环形） | 在「样本域」延时若干个样本（如延时 16 样本做自相关） |
| `moving_avg` | 每个 strobe 推进，并维护窗口和 | 滑动窗口平均 |

#### 4.3.2 核心流程

`delayT` 内部是一个深度为 `DELAY` 的寄存器数组 `ram[0..DELAY-1]`，每个时钟上升沿整体右移一位：`ram[0]<=data_in`，`ram[i]<=ram[i-1]`。输出取自最后一级 `ram[DELAY-1]`。于是输入值要逐级穿过 `DELAY` 个寄存器才到达输出，相当于被延时了 `DELAY` 个时钟周期——正好用来匹配一条 `DELAY` 级流水线的数据通路。

#### 4.3.3 源码精读

端口与参数（默认 32 位、延时 1 拍）：[verilog/delayT.v:1-12](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/delayT.v#L1-L12)。

存储体与输出（输出取自最高位寄存器）：[verilog/delayT.v:14-17](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/delayT.v#L14-L17)。

移位逻辑：[verilog/delayT.v:19-30](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/delayT.v#L19-L30)。

```verilog
assign data_out = ram[DELAY-1];
always @(posedge clock) begin
    if (reset) for (i=0;i<DELAY;i=i+1) ram[i]<=0;
    else begin
        ram[0] <= data_in;
        for (i=1;i<DELAY;i=i+1) ram[i] <= ram[i-1];
    end
end
```

> 关键观察：`delayT` **没有 `enable` 也没有 strobe 输入**，它在每个时钟无条件移位。所以它只适合「周期级」的固定延时；当数据/strobe 是稀疏脉冲时，脉冲会照样逐拍前移，配合下游「只在 strobe 时看数据」的约定，仍能达到对齐效果。

#### 4.3.4 代码实践

**目标**：用参数化思想写一个 `DATA_WIDTH=8、DELAY=4` 的小测试台，验证移位行为。这是「示例代码」，请新建一个文件（如 `delayT_tb.v`）放到仿真目录，**不要写入仓库源码目录**。

```verilog
// 示例代码：delayT_tb.v —— 验证 delayT #(DATA_WIDTH=8, DELAY=4) 的移位寄存器行为
`timescale 1ns/1ps
module delayT_tb;
    reg clock = 0;
    reg reset;
    reg  [7:0] data_in;
    wire [7:0] data_out;

    delayT #(.DATA_WIDTH(8), .DELAY(4)) dut (
        .clock(clock), .reset(reset),
        .data_in(data_in), .data_out(data_out)
    );

    always #5 clock = ~clock;          // 100MHz

    integer i;
    initial begin
        reset = 1; data_in = 8'h00;
        #12 reset = 0;                  // 释放复位
        for (i = 1; i <= 4; i = i + 1) begin
            @(negedge clock); data_in = i;   // 依次送入 1,2,3,4
        end
        @(negedge clock); data_in = 8'h00;   // 停止送数
        repeat (6) @(negedge clock);
        $finish;
    end

    always @(posedge clock)
        $display("data_in=%0d  data_out=%0d", data_in, data_out);
endmodule
```

**操作步骤**：
1. 把上面测试台存为 `delayT_tb.v`，连同 `verilog/delayT.v` 一起用 iverilog 编译运行：
   ```bash
   iverilog -o delayT_tb.vvp delayT_tb.v verilog/delayT.v && vvp delayT_tb.vvp
   ```
2. 观察打印的 `data_in / data_out` 序列。

**现象与预期**：送入 `1,2,3,4` 后，`data_out` 会先保持 0 若干拍，随后依次出现 `1,2,3,4`——即输出是输入的「延时回放」。具体延迟几个时钟沿取决于你如何对「输入生效时刻」计数，**待本地验证**精确拍数；但核心结论必须成立：输出序列与输入序列顺序一致、整体被推迟，且宽度/深度由参数 `DATA_WIDTH`/`DELAY` 决定。

#### 4.3.5 小练习与答案

- **练习 1**：把上面的测试台改成 `DELAY=6`，输出序列会怎样变化？
  - **答**：`data_out` 维持 0 的「预热」段变长（多 2 拍），随后仍依次回放 `1,2,3,4`。延时随 `DELAY` 增大而增大。
- **练习 2**：`delayT` 和 `delay_sample` 都能把信号延后，本质区别是什么？
  - **答**：`delayT` 按时钟拍推进（周期级延时，无 enable）；`delay_sample` 按 strobe 推进且用 RAM 做环形缓冲（样本级延时，延时量可达成百上千样本）。前者用于对齐流水线拍数，后者用于自相关这类「延时 N 个样本」的需求。

### 4.4 moving_avg 与 calc_mean：滑动平均与两路平均

#### 4.4.1 概念说明

「平均」在 OpenOFDM 里出现两种形态：

- `moving_avg`：对一段**滑窗**（窗口大小为 \(2^k\)）持续求平均，用于平滑快速波动的度量，例如 `sync_short` 里平滑瞬时功率、平滑自相关值，抑制瞬时毛刺、得到稳定的门限比较基准。
- `calc_mean`：把**两路**样本一次性求平均（可选整体取反），用于 `equalizer` 里把 LTS1、LTS2 两段长训练序列平均成更稳的信道参考。

#### 4.4.2 核心流程

**滑动平均（增量更新）**：设窗口 \(W=2^{\text{WINDOW\_SHIFT}}\)，第 \(n\) 个输出
\[
\bar{x}_n = \frac{1}{W}\sum_{k=0}^{W-1} x_{n-k}.
\]
直接每来一个样本就把整个窗口求和太贵，于是维护一个滚动和 \(S_n\)，每来新样本 \(x_n\) 就把 \(W\) 拍前的旧样本 \(x_{n-W}\) 减掉：
\[
S_n = S_{n-1} + x_n - x_{n-W},\qquad \bar{x}_n = S_n \gg \text{WINDOW\_SHIFT}.
\]
除以 \(W\) 用右移实现（因为 \(W\) 是 2 的幂）。需要一个能「读最旧样本」的存储——这里用双口 RAM `ram_2port` 当环形缓冲：写口写新样本进 `addr`，读口同时从 `addr` 读出「最旧样本」（因为 \(addr\) 此刻指向的正是 \(W\) 拍前写入的值）。窗口未填满时只累加不减（`full` 标志区分）。

**两路平均**：
\[
c = s\cdot\frac{a+b}{2},\qquad s\in\{+1,-1\}.
\]
用 `a>>>1 + b>>>1` 各自先除 2 再相加，避免 \((a+b)\) 中间溢出；`sign` 为真时再把结果取反（`~cc+1`）。

#### 4.4.3 源码精读

`moving_avg` 的参数与窗口/求和位宽（求和位宽比数据多 `WINDOW_SHIFT` 位，防止累加溢出）：[verilog/moving_avg.v:1-20](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/moving_avg.v#L1-L20)。

用 `ram_2port` 做环形缓冲（写新样本 + 读最旧样本共用同一 `addr`）：[verilog/moving_avg.v:34-47](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/moving_avg.v#L34-L47)。

滚动和更新（窗口满后做 `+new-old`，未满只做 `+new`）与右移求平均：[verilog/moving_avg.v:56-74](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/moving_avg.v#L56-L74)。

```verilog
if (full)
    running_sum <= running_sum + ext_new_data - ext_old_data;  // 滚动更新
else
    running_sum <= running_sum + ext_new_data;                 // 预热阶段
...
data_out      <= running_sum[SUM_WIDTH-1:WINDOW_SHIFT];        // >> WINDOW_SHIFT = /W
output_strobe <= full;                                         // 填满窗口后才输出
```

> 注意 `output_strobe <= full`：窗口未填满前输出无效，所以 `moving_avg` 有 \(W\) 拍的预热延迟。`sync_short` 里 `WINDOW_SHIFT` 默认使得窗口为 64（见 u2-l2）。

`calc_mean` 的两路平均与可选取反：[verilog/calc_mean.v:23-43](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/calc_mean.v#L23-L43)。

```verilog
aa <= a>>>1;                 // 先各除 2
bb <= b>>>1;
cc <= aa + bb;               // 再相加 = (a+b)/2
c  <= sign_stage[1]? ~cc+1: cc;   // 可选整体取反
```

其中 `sign` 经 `sign_stage` 两级延时对齐到 `cc` 同一拍，`output_strobe` 由 `input_strobe` 三级延时产生（与 3 级数据流水线对齐）。消费者见 [verilog/equalizer.v:153-180](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L153-L180)（I、Q 各一个 `calc_mean` 实例平均两段 LTS）。

#### 4.4.4 代码实践

**目标**：观察 `moving_avg` 的窗口预热行为，并理解 `calc_mean` 的取反用途。

1. 在 [verilog/sync_short.v:96-105](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v#L96-L105) 中，`mag_sq_avg_inst` 把 `complex_to_mag_sq` 的瞬时功率送进 `moving_avg` 平滑。在仿真波形里找到 `mag_sq_avg_stb`：它应当在包检测开始后、窗口填满后才出现第一个脉冲。
2. 在 [verilog/equalizer.v](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v) 中找到 `calc_mean` 的 `sign` 输入来自哪里（LTS 参考序列的符号位），理解「为什么平均 LTS 时还需要一个取反选项」。
3. **现象与预期**：`moving_avg` 输出稳定需要窗口预热；`calc_mean` 输出 = 两路输入之和的一半，必要时取负。具体波形「待本地验证」。

#### 4.4.5 小练习与答案

- **练习 1**：`moving_avg` 取 `WINDOW_SHIFT=6`，窗口大小和预热延迟各是多少？求和位宽比数据位宽多几位？
  - **答**：窗口 \(W=2^6=64\)；预热延迟 64 个样本；求和位宽 `SUM_WIDTH=DATA_WIDTH+6`，即多 6 位，刚好够容纳 64 个数据样本之和不溢出。
- **练习 2**：`calc_mean` 里为什么写成 `aa<=a>>>1; bb<=b>>>1; cc<=aa+bb;` 而不是 `cc <= (a+b)>>1`？
  - **答**：先各自除 2 再相加，中间值位宽与输入相同，避免 `a+b` 在 \(a,b\) 同号且较大时溢出丢失精度；这等价于 \((a+b)/2\) 但更安全。

## 5. 综合实践

**任务**：把本讲四个积木串起来，读懂 `sync_short` 里一条完整的「瞬时功率平滑」链路，并画出它的模块级框图与 strobe 流。

链路为：

```
sample_in ──▶ complex_to_mag_sq ──▶ moving_avg ──▶ mag_sq_avg（平滑功率，做归一化分母）
                 （算 I^2+Q^2）      （64 点滑窗平均）
```

请完成：

1. 在 [verilog/sync_short.v:83-105](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v#L83-L105) 中标注每一级用到的本讲原语：`complex_to_mag_sq`（内含一个 `complex_mult`，再内含 `complex_multiplier` IP 与一个 `delayT` 对齐 strobe）→ `moving_avg`（内含 `ram_2port`）。
2. 画出这条链，标出每一级的 strobe 名（`mag_sq_stb`、`mag_sq_avg_stb`）和位宽变化（16+16 → 32 → 32）。
3. 回答：为什么先用 `complex_to_mag_sq`（精确幅值平方）而不是 `complex_to_mag`（近似幅值）来做功率？提示——这一级的结果后面要作为除法/比较的基准，希望尽量精确。
4. 用 u1-l2 的 `make simulate` 跑一次 24Mbps 样本，在 `dot11.vcd` 中量出从 `sample_in_strobe` 到 `mag_sq_avg_stb` 第一次有效的样本数，验证它符合 `complex_mult` 流水线深度 + `moving_avg` 窗口预热之和（精确值「待本地验证」）。

这个任务把「复数乘法 → 幅值平方 → 滑动平均 → strobe 对齐」四件事拧成一条真实数据通路，是检验你是否真正掌握本讲原语的最佳方式。

## 6. 本讲小结

- OpenOFDM 把复数乘法、幅值/幅值平方、延时、平均提炼成一组通用原语，所有算法模块都站在它们之上。
- `complex_mult` 封装 Xilinx `complex_multiplier` IP，并用 `delayT` 让 `output_strobe` 与多级流水线的数据同步对齐——这是全项目握手时序的范本。
- `complex_to_mag` 用 \(\alpha=1,\beta=\tfrac14\)（`max + min>>2`）做无乘法无开方的近似幅值，平均误差约 0.6%、最坏低估约 11.6%（45° 处），只用于比大小/判门限。
- `complex_to_mag_sq` 利用「信号乘自身共轭 \(z\bar z=I^2+Q^2\)」复用一个 `complex_mult` 得到精确幅值平方。
- `delayT` 是每个时钟无条件移位的固定延时链，用于周期级对齐；要和按样本推进的 `delay_sample` 区分。
- `moving_avg` 用双口 RAM 做环形缓冲、滚动和 + 右移实现 \(2^k\) 窗口平均，有窗口预热延迟；`calc_mean` 先各除 2 再相加、可选取反，安全地求两路平均。

## 7. 下一步学习建议

- 这些原语的服务对象是前端同步：建议接着读 **u2-l2（sync_short）**，看它们如何被组装成「延迟自相关 + 平滑 + 门限」的短前导检测器；再看 **u2-l4（sync_long）** 里 `complex_to_mag` + `stage_mult` 如何做 LTS 互相关取峰。
- 想了解复数除法（均衡里的 \(X/H\)）如何用 `complex_mult` 配合 Xilinx `div_gen` 实现，可进入 **u3-l1（equalizer）**。
- 想理解定点缩放（本讲出现的 `>>>1`、`>>2`、`CONS_SCALE_SHIFT` 等）的全局约定，可阅读 **u6-l1（定点数与缩放约定）**。
