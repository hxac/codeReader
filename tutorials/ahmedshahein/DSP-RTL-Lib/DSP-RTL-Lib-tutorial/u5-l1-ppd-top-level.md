# 多相分解原理与 filt_ppd 顶层

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚「多相分解（polyphase decomposition）」把一条长 FIR 拆成 M 路并行短滤波器的数学原理与工程动机。
- 读懂 `filt_ppd` 顶层如何用两个子模块——换向器 `commutator` 与乘加引擎 `mul_add`——拼装成一个抽取滤波器，并能画出从串行 `i_data` 到并行 `comm_data` 再到标量 `o_data` 的完整数据流。
- 解释 `o_sclk` 这个「慢时钟脉冲」在模块内外扮演的双重角色：对内它是 `mul_add` 的工作时钟，对外它是「一个有效输出样本已就绪」的选通信号。
- 对照真实参数表，说明 `gp_decimation_factor`、`gp_coeff_length`、`gp_tf_df` 等参数各自控制什么，并能手算 `comm_data` 宽度与输出位宽。

本讲是「多相滤波器」单元（u5）的入口，只聚焦**顶层骨架与数据流**；换向器的逐位索引、`mul_add` 的四象限乘法编排等细节留给 u5-l2、u5-l3。

## 2. 前置知识

本讲承接 u3-l1（FIR 卷积与 `filt_fir` 结构），并复用单元 2 的全部地基。开始前请确认你理解以下概念：

- **FIR 卷积**：\(y[n]=\sum_{k=0}^{N-1} h[k]\,x[n-k]\)，硬件上由「延迟线 + 乘法器 + 加法树」实现（见 u3-l1、u3-l2）。
- **抽取（decimation）**：先滤波后降采样，\(y[m]=v[mM]\)，其中 \(v=x*h\)，\(M\) 是抽取因子。本讲的 `gp_decimation_factor` 就是 \(M\)。
- **补码定点与位宽增长**：乘法最坏增长 \(A+B\) 位，求和 \(K\) 项再增长 \(\lceil\log_2 K\rceil\) 位；全库用 `$clog2` 自动推导（见 u2-l1）。
- **dff 原语与命名约定**：`i_rst_an`（异步低有效复位）、`i_ena`（同步高有效使能）、`posedge i_clk`；`gp_/c_/r_/w_` 前缀（见 u2-l2、u1-l4）。
- **黄金参考模型（GRM）与比特真**：Octave 给标准答案，测试台逐样本比对（见 u1-l3、u4-l3）。

一个关键直觉：**朴素「先滤波后扔样本」的做法极其浪费**——如果 \(M=4\)，你算出的 4 个输出里要扔掉 3 个，相当于 75% 的乘法白做。多相分解就是用来消灭这份浪费的，这正是本讲要回答的核心问题。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `filt_ppd` 模块内（目录约定见 u1-l2）：

| 文件 | 作用 |
|------|------|
| `.drl_src_code/filt_ppd/rtl/filt_ppd.v` | **顶层**。例化换向器与乘加引擎，定义对外端口与参数表，是本讲精读主角。 |
| `.drl_src_code/filt_ppd/rtl/commutator.v` | **换向器（串→并）**。把串行 `i_data` 按相位分发到 M 路寄存器，并产生慢时钟脉冲 `o_clk`。逐位细节见 u5-l2。 |
| `.drl_src_code/filt_ppd/rtl/mul_add.v` | **多相乘加引擎**。对并行字做 M 路子滤波器的乘加并求和，产出标量输出。逐位细节见 u5-l3。 |
| `.drl_src_code/filt_ppd/rtl/shift_register.v` | 级联 dff 延迟线，被换向器用于相位对齐（见 u2-l3）。 |
| `.drl_src_code/filt_ppd/rtl/dff.v` | 全库原子寄存器（见 u2-l2）。 |
| `.drl_param/filt_ppd_1.param` | 默认设计参数（三列「键 = 值」格式，见 u1-l3）。 |
| `.drl_src_code/filt_ppd/octave/ppd_bittrue_model.m` | Octave 黄金参考模型，演示多相矩阵的数学构造（参数与回归脚本略有不同，见后文）。 |
| `.drl_src_code/filt_ppd/sim/testbench/filt_ppd_tb.sv` | SystemVerilog 测试台，用 `s_clk` 选通输出比对。 |

## 4. 核心概念与源码讲解

### 4.1 多相分解思想

#### 4.1.1 概念说明

「多相分解」要做的事，用一句话讲就是：**把一条 \(N\) 抽头的长 FIR，按抽取因子 \(M\) 重新编排成 \(M\) 条短子滤波器，让全部运算都跑在抽取后的慢速率上**，从而既不丢精度，又不必算那些注定要被扔掉的样本。

为什么能做到？关键观察来自把卷积求和的下标 \(k\) 按 \(M\) 重新编号。设长滤波器冲激响应为 \(h[k],\,k=0..N-1\)，先滤波后抽取的输出是：

\[
y[m] = v[mM] = \sum_{k=0}^{N-1} h[k]\,x[mM-k]
\]

把 \(k\) 写成「块号 × 抽取因子 + 相位」\(k = qM + r\)，其中相位 \(r\in[0,M-1]\)、块号 \(q\in[0,\lceil N/M\rceil-1]\)，代入得：

\[
y[m] = \sum_{r=0}^{M-1}\sum_{q} h[qM+r]\;x[(m-q)M - r]
\]

定义第 \(r\) **相子滤波器**与第 \(r\) **相子序列**：

\[
h_r[q] \triangleq h[qM+r], \qquad x_r[n] \triangleq x[nM - r]
\]

于是输出被改写成 **M 路短卷积之和**：

\[
y[m] = \sum_{r=0}^{M-1} \underbrace{\sum_{q} h_r[q]\,x_r[m-q]}_{\text{第 }r\text{ 相子滤波器的输出}}
\]

这就是多相分解。三个直接收益：

1. **降速**：每条子滤波器只在抽取后的慢时钟上工作，吞吐需求降为 \(1/M\)。
2. **省算力**：朴素做法每输入样本算 \(N\) 次乘法；多相做法每**输出**样本算 \(N\) 次乘法（拆成 \(M\) 条 \(\lceil N/M\rceil\) 抽头），等效工作量降为 \(1/M\)。
3. **天然并行**：M 条子滤波器结构一致，适合硬件并行展开。

> 术语提示：「相（phase）」指对下标取模 \(M\) 的余数类别；M 条支路即 M 个相位，故称「多相」。这与 NCO 的「相位累加」无关，只是同名。

#### 4.1.2 核心流程

把上面的数学翻成数据流，多相抽取滤波器的标准结构是「换向器 → M 路子滤波器 → 求和」：

```
            ┌──────────── 换向器 commutator ────────────┐
串行 i_data │  旋转的一热环计数器，把第 n 个样本分发到   │
(快速率) ──►│  第 (n mod M) 路寄存器；每 M 拍输出 1 个   │── comm_data (M 样本并行)
            │  完整并行字，并发出 1 个慢时钟脉冲 s_clk    │
            └───────────────────────────────────────────┘
                                  │ (慢速率：每 M 个快拍 1 次)
                                  ▼
            ┌──────────── 乘加引擎 mul_add ─────────────┐
            │  对并行字里的 M 个样本分别乘以各自子滤波   │
            │  器系数（每相 ⌈N/M⌉ 个抽头），加法树求和， │── o_data (标量，每慢拍 1 个)
            │  流水累加跨块部分和 ────────────────────── │
            └───────────────────────────────────────────┘
```

伪代码（慢时钟域，每个 s_clk 执行一次）：

```
for r in 0..M-1:                     # 遍历 M 个相位
    branch_sum[r] = 0
    for q in 0..(c_col-1):            # c_col = ⌈N/M⌉，每相抽头数
        branch_sum[r] += h_r[q] * sample[r, q]
y = sum(branch_sum)                  # M 路相加
```

#### 4.1.3 源码精读

顶层 `filt_ppd.v` 把「换向器 + 乘加引擎」原样拼起来。先看它的**参数表**——这正是多相分解三个关键量的落点：

[`.drl_src_code/filt_ppd/rtl/filt_ppd.v#L6-L18`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/filt_ppd.v#L6-L18)：模块声明，定义了 \(M\)（`gp_decimation_factor`）、\(N\)（`gp_coeff_length`）、抽头位宽、拓扑选择与输出位宽默认表达式。

```verilog
module filt_ppd #(  
  parameter gp_idata_width       = 6,   // 输入样本位宽
  parameter gp_decimation_factor = 31,  // = M：抽取因子 ＝ 多相支路数 ＝ 输出通道数
  parameter gp_coeff_length      = 53,  // = N：长 FIR 的抽头数
  parameter gp_coeff_width       = 16,  // 系数位宽
  parameter gp_tf_df             = 0,   // 子滤波器拓扑：1->TF | 0->DF
  ...
  parameter gp_odata_width = gp_idata_width+gp_coeff_width
                           +$clog2(gp_decimation_factor)
                           +$clog2(`DIV(gp_coeff_length,gp_decimation_factor))
```

注意 `gp_odata_width` 这个默认表达式正是 4.1.1 数学里位宽增长的直接翻译：

\[
B_{out} = \underbrace{B_{in}+B_{coeff}}_{\text{乘积位宽 }c\_mul\_out\_width}
        + \underbrace{\lceil\log_2 M\rceil}_{\text{M 路求和增长}}
        + \underbrace{\lceil\log_2 \lceil N/M\rceil\rceil}_{\text{跨 ⌈N/M⌉ 块累加增长}}
\]

其中 `DIV(N,D)` 是顶部的上取整宏 `define DIV(N, D) (N%D==0) ? (N/D) : (N/D+1)`，即 \(\lceil N/D\rceil\)（[`filt_ppd.v#L4`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/filt_ppd.v#L4)）。

GRM 侧 `gen_defines.m` 用同一个公式算 `P_OUP_W` 宏（`ceil(log2(M)) + ceil(log2(N/M))`），保证 RTL 与 GRM 输出位宽一致——这是比特真验证的前提（见 [`.drl_src_code/filt_ppd/octave/gen_defines.m#L16-L17`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/octave/gen_defines.m#L16-L17)）。

GRM `ppd_bittrue_model.m` 则把「长系数向量 `b` 重排成 M×c_col 多相矩阵」这一步明明白白写了出来，正好对应 4.1.1 的 \(h_r[q]=h[qM+r]\)。这里节选它构造多相矩阵的主循环：

[`.drl_src_code/filt_ppd/octave/ppd_bittrue_model.m#L23-L51`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/octave/ppd_bittrue_model.m#L23-L51)：按 TF/DF 与 CW/CCW 四种组合，把一维系数 `b` 填进二维 `ppd_mul(row,col)` 矩阵——`row=M`、`col=⌈N/M⌉`，不足处补零。

```matlab
row = p_decimation_factor;            % M
col = ceil(length(b)/p_decimation_factor);  % ⌈N/M⌉
if (length(b) ~= row*col)
  b = [b zeros(1,row*col-length(b))]; % 不足补零，对应 RTL 里 'd0 的空槽
end
ppd_mul = zeros(row, col);
% 双重 for 把一维 b 填进 row×col 矩阵（顺序随 TF/DF×CW/CCW 变）
```

这段 GRM 就是「把 \(h[k]\) 重新编排成 \(h_r[q]\)」的可执行版本，行数 = 相位数 \(M\)，列数 = 每相抽头数 \(\lceil N/M\rceil\)。RTL 的 `mul_add` 做的是同一件事（见 4.2.3）。

#### 4.1.4 代码实践

**目标**：用默认 `.param` 的数值，亲手验证多相分解的尺寸推导。

**操作步骤**：

1. 打开 [`.drl_param/filt_ppd_1.param`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_param/filt_ppd_1.param)，记下默认值：\(M=31\)、\(N=53\)、\(B_{in}=6\)、\(B_{coeff}=16\)。
2. 在草稿纸上算三个量，并与 `mul_add.v` 的 localparam 对照：
   - 每相抽头数 `c_col = DIV(N,M) = DIV(53,31) = ⌈53/31⌉ = 2`
   - 系数矩阵总槽位 `c_row_x_col = M×c_col = 31×2 = 62`
   - 半系数长度 `c_coeff_2 = DIV(N,2) = DIV(53,2) = 27`（因库支持对称系数，只存半数，见 u3-l3）
3. 解释 53 个真实系数如何填进 62 个槽：前 53 个填 `h_r[q]`，剩余 \(62-53=9\) 个槽补零（即 `mul_add.v` 里的 `else assign ... = 'd0` 分支）。
4. 手算输出位宽：\(B_{out}=6+16+\lceil\log_2 31\rceil+\lceil\log_2 2\rceil = 6+16+5+1 = 28\) 位。

**需要观察的现象 / 预期结果**：

- `c_col=2`、`c_row_x_col=62`、`c_coeff_2=27` 应与 [`.drl_src_code/filt_ppd/rtl/mul_add.v#L24-L26`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/mul_add.v#L24-L26) 的 localparam 完全一致。
- 输出 28 位应与 `filt_ppd.v` 默认表达式在该参数下的取值一致。
- **待本地验证**：可用 `iverilog` 仅 elaborate（`-E`）一个实例，`$display` 打印这些 localparam 来核对。

#### 4.1.5 小练习与答案

**练习 1**：若把 `gp_coeff_length` 从 53 改成 64，`c_col` 变成多少？补零槽还剩几个？

**答案**：`c_col = DIV(64,31) = ⌈64/31⌉ = 3`；`c_row_x_col = 31×3 = 93`；补零槽 = \(93-64=29\) 个。可见 \(N\) 不变时，\(M\) 越大每相抽头越少、补零越多。

**练习 2**：为什么多相分解后「等效工作量」降为朴素 FIR 的 \(1/M\)？

**答案**：朴素做法每个**输入**样本都要算 \(N\) 次乘法（即便 \(M-1\) 个输出会被扔掉）；多相做法把这 \(N\) 次乘法摊到 \(M\) 个输入上、只在第 \(M\) 个输入时一次性产出 1 个输出，故每个输入样本平均只承担 \(N/M\) 次乘法。

---

### 4.2 commutator + mul_add 数据流

#### 4.2.1 概念说明

4.1 讲清了「为什么拆」，本节讲清「顶层怎么连」。`filt_ppd` 顶层只做三件事：

1. 用 `commutator`（换向器）把**串行**输入 `i_data`（快速率）打包成 **M 样本并行**字 `comm_data`，每攒满 M 个就发一个慢时钟脉冲 `s_clk`；
2. 用 `mul_add`（乘加引擎）在这个**慢时钟**上对并行字做多相卷积，产出标量 `o_data`；
3. 把内部 `s_clk` 引到顶层端口 `o_sclk`，告诉外部「输出有效」。

换向器本质上是一个**旋转的串并转换器**（rotary demux）：一根旋转的「指针」每拍指向下一路寄存器，把当前输入样本写进去；转满一圈（M 拍），M 路寄存器刚好装满一组完整样本，就吐出一个并行字。这正是 4.1.2 数据流图里第一级的硬件实现，也实现了「抽取」这个变速动作——M 个快拍压成 1 个慢拍。

> 这个「旋转指针」在 RTL 里是一个一位热码（one-hot）环计数器 `r_ring_cnt`：每拍只有一位置 1，且逐拍移位。它既是「分发样本到哪一路」的选择信号，又被直接当作各路捕获寄存器的时钟（见 u5-l2）。

#### 4.2.2 核心流程

顶层的连接关系极其简洁，全模块只有两个例化加一行 assign：

```
i_data (串, 快) ──► commutator ──comm_data (M 样本并行)──► mul_add ──► o_data (标量)
                  │                                         ▲
                  └────── s_clk (每 M 快拍 1 脉冲) ──────────┘  ← mul_add 的 i_clk 就是 s_clk！
                                                                    （MAC 引擎跑在慢时钟上）
assign o_sclk = s_clk;   ← 同一根脉冲对外暴露
```

要特别注意**时钟域的交接**：

- `commutator` 跑在**快时钟** `i_clk` 上（逐拍分发样本）。
- `mul_add` 跑在**慢时钟** `s_clk` 上（每 M 拍才算一次卷积）——它的 `.i_clk(s_clk)`（见 4.2.3）。
- `comm_data` 是跨域的握手数据：它在快域被逐步填满，在 `s_clk` 上升沿被慢域一次性消费。由于 `s_clk` 恰好只在 `comm_data` 完整且稳定的时刻拉高，无需额外的同步器。

#### 4.2.3 源码精读

先看顶层例化——**整个模块的设计哲学都浓缩在这 35 行里**：

[`.drl_src_code/filt_ppd/rtl/filt_ppd.v#L28-L63`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/filt_ppd.v#L28-L63)：声明 `s_clk`、`comm_data` 两条内部线网，例化换向器与乘加引擎，并把 `s_clk` 引到顶层 `o_sclk`。

```verilog
wire                                           s_clk;
wire [gp_decimation_factor*gp_idata_width-1:0] comm_data;  // M 样本并行字
// ---- 换向器：串行 i_data → 并行 comm_data + 慢时钟 s_clk ----
commutator #(.gp_decimation_factor(gp_decimation_factor), ...) ppd_commutator (
    .i_clk  (i_clk    ),   // 快时钟
    .i_data (i_data   ),   // 串行输入
    .o_data (comm_data),   // M 样本并行
    .o_clk  (s_clk    )    // 每 M 拍 1 脉冲
);
// ---- 乘加引擎：在慢时钟 s_clk 上做 M 相卷积 ----
mul_add #(.gp_decimation_factor(gp_decimation_factor), ...) ppd_mul_add (
    .i_clk  (s_clk    ),   // ★ 注意：mul_add 的时钟是慢时钟 s_clk
    .i_data (comm_data),   // 消费并行字
    .o_data (o_data   )    // 每慢拍 1 个标量输出
);
assign o_sclk = s_clk;
```

三处要点：

1. **`comm_data` 宽度** = `gp_decimation_factor*gp_idata_width` = \(M \times B_{in}\) 位，即 M 个样本紧挨打包（[`filt_ppd.v#L29`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/filt_ppd.v#L29)）。
2. **`mul_add` 的 `.i_clk(s_clk)`**（[`filt_ppd.v#L58`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/filt_ppd.v#L58)）：MAC 引擎不在快时钟上算，而在换向器产出的慢时钟上算——这是「降速省算力」在顶层的直接体现。
3. **`assign o_sclk = s_clk`**（[`filt_ppd.v#L63`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/filt_ppd.v#L63)）：同一根脉冲既驱动 `mul_add`，又对外宣告输出有效。

再看换向器如何产生这根慢时钟脉冲。`commutator` 用一个一位热码环计数器逐拍旋转，转满一圈触发一次 `done`：

[`.drl_src_code/filt_ppd/rtl/commutator.v#L124-L147`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/commutator.v#L124-L147)（CCW 分支的环计数器）：每拍把一热位移位一次，当指针回到末端时 `w_done` 拉高。

```verilog
if (r_ring_cnt == 'd0)
  r_ring_cnt[0] <= 1'b1;                       // 启动：置最低位
else begin
  r_ring_cnt[0]               <= r_ring_cnt[c_cnt_width-1];   // 左旋一位
  r_ring_cnt[c_cnt_width-1:1] <= r_ring_cnt[c_cnt_width-2:0];
end
r_done <= w_done;                              // 寄存一拍
```

[`commutator.v#L201`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/commutator.v#L201) 与 [`commutator.v#L205`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/commutator.v#L205)：`w_done` 在一热指针抵达最高位时为真；`assign o_clk = r_done` 把这个「每 M 拍一次」的脉冲输出成慢时钟。各路样本则由 `r_ring_cnt[x]` 直接当捕获时钟写入对应寄存器（[`commutator.v#L168-L179`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/commutator.v#L168-L179)）——逐位细节留待 u5-l2。

最后看 `mul_add` 在这根慢时钟上做什么（高层视角，细节见 u5-l3）。它把并行字展开成 M 相、每相 `c_col` 个抽头的乘加，再用加法树与流水累加器求和：

[`.drl_src_code/filt_ppd/rtl/mul_add.v#L24-L31`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/mul_add.v#L24-L31)：派生常量，把多相矩阵的尺寸与各级位宽全部算好。

```verilog
localparam c_col         = `DIV(gp_coeff_length, gp_decimation_factor); // 每相抽头数 ⌈N/M⌉
localparam c_row_x_col   = gp_decimation_factor * c_col;               // 系数矩阵总槽 M·⌈N/M⌉
localparam c_mul_out_width = gp_idata_width + gp_coeff_width;          // 乘积位宽
localparam c_add_out_width = $clog2(gp_decimation_factor) + c_mul_out_width; // M 路求和
localparam c_sum_out_width = c_add_out_width + $clog2(c_col);          // 跨块累加 = gp_odata_width
```

注意 `c_sum_out_width` 恰好等于顶层 `gp_odata_width`——4.1.1 的位宽公式在这里被逐级实现。乘法编排（TF/DF × CW/CCW 四象限）见 [`mul_add.v#L53-L114`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/mul_add.v#L53-L114)，加法树与流水累加见 [`mul_add.v#L119-L212`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/mul_add.v#L119-L212)，本讲只要求读懂它的「在 s_clk 上消费 comm_data、产出 o_data」这一顶层角色。

#### 4.2.4 代码实践

**目标**（本讲主实践）：对照参数表说清三个关键参数，并画出 `comm_data` 宽度与 `s_clk` 节拍的关系。

**操作步骤**：

1. **参数解读**。对照 [`filt_ppd.v#L7-L15`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/filt_ppd.v#L7-L15)，用自己的话写明：
   - `gp_decimation_factor`（默认 31）：既是抽取因子 \(M\)，又是多相支路数，也是并行字里的样本数；它决定 `comm_data` 宽度 = \(M \times B_{in}\)。
   - `gp_coeff_length`（默认 53）：长 FIR 抽头数 \(N\)；它决定每相抽头数 `c_col=⌈N/M⌉` 与输出位宽里的 \(\lceil\log_2 c\_col\rceil\) 项。
   - `gp_tf_df`（默认 0）：子滤波器拓扑选择（1=转置型 TF，0=直接型 DF，见 u3-l2）；只影响 `mul_add` 内部乘加结构，不改变顶层的串→并→标量数据流。
2. **画时序图**。取一个小例子 \(M=4\)、\(B_{in}=8\)，画出「快时钟 `i_clk`、串行 `i_data`、4 路捕获、`comm_data` 有效、`s_clk` 脉冲、`o_data`」的对齐关系。

**需要观察的现象 / 预期结果**：

参考时序（\(M=4\)）：

```
i_clk   : _|-|_|-|_|-|_|-|_|-|_|-|_|-|_     ← 快时钟
i_data  :  x0  x1  x2  x3  x4  x5  x6  ...  ← 串行样本
环指针  : [0] [1] [2] [3] [0] [1] [2] ...
捕获    : 路0←x0 ...         路0←x4 ...
comm_data有效:        [x0 x1 x2 x3]        [x4 x5 x6 x7]   ← 每 M=4 拍更新一次完整字
s_clk   :                 |____                 |____      ← 每 4 个 i_clk 1 个脉冲
o_data  :                       y[0]                   y[1] ← 每个慢拍 1 个输出
```

要点：`comm_data` 宽度 \(=4\times8=32\) 位；完整有效的并行字每 \(M=4\) 个快拍出现一次，且正好与 `s_clk` 脉冲对齐；`o_data` 随后在每个 `s_clk` 产出 1 个样本。

3. **运行回归（待本地验证）**。在装好 iverilog 与 octave 的环境执行 `./dsp_rtl_lib.sh -d filt_ppd_1.param`，观察 9 个测试用例逐个 PASSED/FAILED。预期全部 PASSED，但因默认参数（\(M=31,N=53\)）较大、未亲验，请以本机实际结果为准。

#### 4.2.5 小练习与答案

**练习 1**：若 `gp_decimation_factor` 翻倍而 `gp_coeff_length` 不变，`comm_data` 宽度与每相抽头数 `c_col` 各如何变化？

**答案**：`comm_data` 宽度 \(M\times B_{in}\) 随 \(M\) 翻倍；`c_col=⌈N/M⌉` 大致减半（上取整可能导致不严格减半）。

**练习 2**：为什么 `mul_add` 的 `i_clk` 接 `s_clk` 而不是顶层 `i_clk`？

**答案**：因为多相分解已把卷积降到抽取后的慢速率——每个慢拍才有一个完整并行字可算，接快时钟会让 `mul_add` 在 \(M-1\) 个空拍里反复算同一组数据，既浪费又需额外使能屏蔽；直接用 `s_clk` 当时钟，天然实现「每 M 拍算一次」。

---

### 4.3 o_sclk 慢时钟脉冲的作用

#### 4.3.1 概念说明

`o_sclk` 是顶层对外的一根单 bit 输出，但它身兼两职：

1. **对内（模块自身）**：它就是 `commutator` 产出的 `s_clk`，并已被当作 `mul_add` 的工作时钟（4.2 已述）。所以 `o_sclk` 的频率 = 快时钟频率 \(/M\)。
2. **对外（消费者/测试台）**：它是一个「**输出有效选通（strobe）**」——每次脉冲表示 `o_data` 上出现了一个新的、比特真有效的样本。

把「慢时钟」当「有效标志」对外送出，是抽取/插值类多速率模块的通用约定：下游不知道、也不需要知道 \(M\) 是几，只要在 `o_sclk` 脉冲沿去采 `o_data` 即可。这跟 CIC 抽取器里 `w_sclk` 的角色完全一致（见 u4-l1）。

> 术语提示：「选通 / strobe」指一个周期性的单拍脉冲，用来标志某个数据有效；「使能 / enable」是允许动作发生的条件。这里 `o_sclk` 既是 `mul_add` 的时钟，又是外部的选通——同一根线，两个视角。

#### 4.3.2 核心流程

`o_sclk` 的产生与消费链：

```
commutator 内部：
  一热环计数器转满 M 拍 → w_done（1 拍）→ r_done（寄存 1 拍）→ o_clk = r_done
顶层：     assign o_sclk = s_clk;        （s_clk 即 commutator.o_clk）
对内消费： mul_add.i_clk = s_clk;        （每个 s_clk 沿算一次多相卷积 → 更新 o_data）
对外消费： 测试台/下游在 s_clk 沿采样 o_data
```

节拍关系：每 \(M\) 个 `i_clk` 上升沿 → 1 个 `s_clk`（`o_sclk`）脉冲 → 1 个新 `o_data` 样本。

#### 4.3.3 源码精读

顶层暴露这根线只有一行：

[`.drl_src_code/filt_ppd/rtl/filt_ppd.v#L24`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/filt_ppd.v#L24)：端口声明 `output wire o_sclk`，注释写明 "Slow clock pulsed output"。

[`.drl_src_code/filt_ppd/rtl/filt_ppd.v#L63`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/rtl/filt_ppd.v#L63)：`assign o_sclk = s_clk;` 把内部慢时钟原样对外。

测试台 `filt_ppd_tb.sv` 正是把 `s_clk` 当作「比对时刻」来用的——这是 `o_sclk` 对外角色的最佳范例：

[`.drl_src_code/filt_ppd/sim/testbench/filt_ppd_tb.sv#L128-L136`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/sim/testbench/filt_ppd_tb.sv#L128-L136)：在 `s_clk` 的**下降沿**比对 RTL 输出与 GRM 答案，不一致则 `error_count++`。

```verilog
always @(negedge s_clk) begin
  if (i_rst_an && i_ena)
    if (o_data_rtl != o_data_mat)
      begin
        $error("### RTL = %d, MAT = %d", o_data_rtl, o_data_mat);
        error_count <= error_count + 1;
      end
end
```

为什么比对取 `negedge s_clk`？因为 `o_data` 在 `s_clk` 上升沿被 `mul_add` 更新，到下降沿时已稳定，采下来最安全。而激励读入发生在 `posedge i_clk`（快域，[`filt_ppd_tb.sv#L90-L100`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/sim/testbench/filt_ppd_tb.sv#L90-L100)），响应比对发生在 `negedge s_clk`（慢域）——两域节拍比为 \(M{:}1\)，正对应换向器的串并压缩。

#### 4.3.4 代码实践

**目标**：通过读测试台，亲眼看清 `o_sclk` 如何同时充当「内部工作时钟」与「外部比对选通」。

**操作步骤**：

1. 打开 `filt_ppd_tb.sv`，找到 DUT 例化（[`L108-L126`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/sim/testbench/filt_ppd_tb.sv#L108-L126)），确认 `.o_sclk(s_clk)` 把模块的慢时钟接到测试台内部线 `s_clk`。
2. 追踪 `s_clk` 的两个消费者：
   - 经 `forever #1 res_clk = s_clk;`（[`L52-L57`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/sim/testbench/filt_ppd_tb.sv#L52-L57)）派生出 `res_clk`，GRM 响应文件在 `posedge res_clk` 读入（[`L102-L106`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/sim/testbench/filt_ppd_tb.sv#L102-L106)）。
   - 在 `negedge s_clk` 处把 RTL 与 GRM 比对（[`L128-L136`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/sim/testbench/filt_ppd_tb.sv#L128-L136)）。
3. 用一句话回答：为什么激励在 `i_clk` 沿读、响应在 `s_clk` 沿比？

**需要观察的现象 / 预期结果**：

- 激励读入速率 = 快时钟（每个 `i_clk` 喂一个串行样本）。
- 响应比对速率 = 慢时钟（每 \(M\) 个 `i_clk` 才比一次），与模块「M 进 1 出」的抽取节拍吻合。
- 全程 `error_count` 保持 0 → 测试台打印 `### INFO: Testcase PASSED`（[`L81-L84`](https://github.com/ahmedshahein/DSP-RTL-Lib/blob/6bc926f160bd55aab7b77a088e71f9655bbc85ad/.drl_src_code/filt_ppd/sim/testbench/filt_ppd_tb.sv#L81-L84)）。
- **待本地验证**：实际 PASSED/FAILED 以本机回归结果为准。

#### 4.3.5 小练习与答案

**练习 1**：若下游模块想用 `filt_ppd` 的输出，它该在哪个沿采 `o_data`？为什么？

**答案**：在 `o_sclk` 的下降沿（或上升沿后等一拍稳定时刻）采。因为 `o_data` 在 `s_clk` 上升沿更新，下降沿处数据已稳定，最不易采到翻转中的值。

**练习 2**：`o_sclk` 的高电平持续几个快时钟周期？

**答案**：`s_clk = r_done`，而 `r_done` 是 `w_done` 寄存一拍的结果，`w_done` 又只在环指针抵达末端时为真 1 拍。故 `o_sclk` 每个脉冲高电平持续约 1 个快时钟周期，每 \(M\) 个快周期出现一次（具体建立/保持细节见 u5-l2）。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个小任务：

**任务**：用一组小参数配置一个 `filt_ppd`，亲手走一遍「参数 → 多相尺寸 → 数据流 → 验证」全链路。

1. **配置参数**。复制默认参数文件并改成接近 GRM 模板的小配置（即 `stimuli.m` 顶部那组值）：

   ```
   gp_idata_width       = 2
   gp_decimation_factor = 4     # M
   gp_coeff_length      = 12    # N
   gp_coeff_width       = 6
   gp_tf_df             = 1     # TF
   gp_comm_reg_oup      = 1
   gp_comm_ccw          = 1
   gp_mul_ccw           = 0
   gp_comm_phase        = 2
   ```

2. **手算多相尺寸**。算出 `c_col`、`c_row_x_col`、`c_coeff_2`、`gp_odata_width`，并与 `mul_add.v#L24-L31` 核对。
   - 参考答案：`c_col=DIV(12,4)=3`；`c_row_x_col=4×3=12`（无补零）；`c_coeff_2=DIV(12,2)=6`；`gp_odata_width=2+6+$clog2(4)+$clog2(3)=2+6+2+2=12` 位。
3. **画数据流**。画出这组参数下的 `comm_data` 宽度（\(4\times2=8\) 位）与 `s_clk` 节拍（每 4 个 `i_clk` 1 脉冲）的关系图，标出 `mul_add` 在慢时钟上消费并行字、产出 12 位 `o_data`。
4. **运行回归（待本地验证）**。执行 `./dsp_rtl_lib.sh -d <你的.param>`，观察 9 个测试用例是否 PASSED。把这组参数与默认 `filt_ppd_1.param`（\(M=31,N=53\)）的结果对比，体会 \(M\) 增大对「并行字宽度」与「慢时钟频率」的影响。

**验收标准**：能不看书说出「换向器每 M 拍产 1 个并行字与 1 个 s_clk 脉冲；mul_add 在 s_clk 上算一次多相卷积；o_sclk 对外标志输出有效」这三句话，并解释输出位宽公式里两项 \(\lceil\log_2 M\rceil\) 与 \(\lceil\log_2 c\_col\rceil\) 各来自哪一级求和。

## 6. 本讲小结

- **多相分解**把 \(N\) 抽头长 FIR 按抽取因子 \(M\) 重排为 \(M\) 条 \(\lceil N/M\rceil\) 抽头的短子滤波器，让全部运算降到 \(1/M\) 慢速率，等效工作量降为朴素 FIR 的 \(1/M\)。
- `filt_ppd` 顶层用**两个子模块**实现它：`commutator` 把串行输入串并转换成 \(M\) 样本并行字 `comm_data` 并产慢时钟 `s_clk`；`mul_add` 在 `s_clk` 上做多相卷积，产出标量 `o_data`。
- 关键参数：`gp_decimation_factor`（\(M\)，决定并行度与抽取比）、`gp_coeff_length`（\(N\)，决定每相抽头数）、`gp_tf_df`（子滤波器 TF/DF 拓扑）。
- 输出位宽 `gp_odata_width = B_in+B_coeff+⌈log2 M⌉+⌈log2⌈N/M⌉⌉`，RTL 的 `$clog2` 与 GRM 的 `ceil(log2)` 用同一公式，是比特真的根基。
- **`o_sclk` 一根线两角色**：对内是 `mul_add` 的工作时钟，对外是「输出有效」的选通；测试台在 `negedge s_clk` 比对 RTL 与 GRM。
- 本讲只到**顶层骨架**；换向器逐位索引、`mul_add` 四象限乘法编排分别见 u5-l2、u5-l3。

## 7. 下一步学习建议

- **u5-l2（commutator 换向器）**：精读一位热码环计数器的 CW/CCW 旋转、用 `r_ring_cnt[x]` 当捕获时钟的机制、`gp_phase` 相位对齐——回答本讲里悬而未决的「样本到底进哪一路」。
- **u5-l3（mul_add 多相乘加引擎）**：精读系数矩阵化 `c_row_x_col`、TF/DF × CW/CCW 四象限乘法输入编排、加法树 `w_add_tree` 与流水累加 `w_sum`——回答「并行字如何变成标量」。
- **u5-l4（filt_ppi 多相插值）**：对比插值器的反向数据流（先 `mul_add` 后 `commutator`）与索引式输出，把「抽取/插值对偶」看全。
- 复习建议：若对 TF/DF 拓扑已生疏，回头翻 u3-l2；对 `$clog2` 位宽推导不熟，翻 u2-l1。
