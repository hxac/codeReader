# filt_ppi 多相插值与反向数据流

## 1. 本讲目标

本讲是「多相滤波器」单元的收尾篇。在 [u5-l1](u5-l1-ppd-top-level.md)、[u5-l2](u5-l2-commutator-decimation.md)、[u5-l3](u5-l3-mul-add-engine.md) 三讲里，我们以抽取器 `filt_ppd` 为对象，理清了「换向器 `commutator` → 乘加引擎 `mul_add`」这条数据流。本讲把它整个翻过来，精读镜像模块 **`filt_ppi`（多相插值滤波器）**，看懂数据流为何变成「`mul_add` → `commutator`」，以及随之而来的一个关键改造：换向器从「**整字并行输出**」变成「**按 `r_idx` 索引、逐拍串行输出**」。

学完后你应当能够：

- 说清多相插值与多相抽取的**对偶关系**，以及为何插值器要把乘加放在前面、换向器放在后面；
- 读懂 `filt_ppi` 顶层如何用**慢时钟 `i_clk` 驱动 `mul_add`、快时钟 `i_fclk` 驱动 `commutator`**；
- 讲明白 PPI 版 `commutator` 用 `r_idx` **索引选择**输出样本的机制，以及它为何不能像 PPD 版那样直接输出整字；
- 列出 PPD 与 PPI 两个 `commutator.v` 在**端口/参数、输出选择逻辑、时钟与方向**上的关键差异。

## 2. 前置知识

本讲默认你已掌握前三讲建立的认知，下面只做最小回顾，不重复细节：

- **多相分解（[u5-l1](u5-l1-ppd-top-level.md)）**：把一条 \(N\) 抽头的长 FIR 按因子 \(M\) 重排成 \(M\) 条 \(\lceil N/M\rceil\) 抽头的短子滤波器，从而把运算降到 \(1/M\) 速率。
- **换向器（[u5-l2](u5-l2-commutator-decimation.md)）**：PPD 版用一位热码环形计数器 `r_ring_cnt` 既计数又分路，把每一位直接接到对应捕获 `dff` 的时钟端口，实现按相位捕获。
- **乘加引擎（[u5-l3](u5-l3-mul-add-engine.md)）**：`mul_add` 把系数矩阵化，用 TF/DF × CW/CCW 四象限编排乘法器，再用加法树与流水累加器求和。

此外需要两个本讲用到的新直觉：

- **抽取 vs 插值的速率关系**：抽取是把**高速率**输入降为低速率输出（\(M\) 个进、\(1\) 个出）；插值相反，把**低速率**输入升为高速率输出（\(1\) 个进、\(L\) 个出）。
- **Noble 恒等式的对偶形式（承接 [u4-l2](u4-l2-cici-interpolation.md) 的 CIC 插值）**：抽取时「滤波 → ↓M」可改写成「多相滤波器在慢域算」；插值时「↑L → 滤波」可改写成「多相滤波器在快域算」。这条对偶性正是本讲「数据流翻转」的数学根据。

> 术语约定：本讲里 \(L\) = `gp_interpolation_factor`（插值因子，对应 PPD 里的 \(M\) = `gp_decimation_factor`）；\(N\) = `gp_coeff_length`（系数长度）。

## 3. 本讲源码地图

本讲涉及的关键文件（均在 `.drl_src_code/` 下）：

| 文件 | 作用 |
| --- | --- |
| `filt_ppi/rtl/filt_ppi.v` | 插值器顶层：例化 `mul_add`（慢域）与 `commutator`（快域），决定数据流顺序与相位对齐 |
| `filt_ppi/rtl/commutator.v` | **PPI 版换向器**：把 `mul_add` 送来的并行字按 `r_idx` 逐拍串行输出，是本讲重点 |
| `filt_ppi/rtl/mul_add.v` | **PPI 版乘加引擎**：一次算出 \(L\) 个相位的短卷积，并行打包成一字输出 |
| `filt_ppd/rtl/filt_ppd.v` | 抽取器顶层，用于对比「换向器在前、乘加在后」的顺序 |
| `filt_ppd/rtl/commutator.v` | **PPD 版换向器**，用于对比输出选择逻辑 |
| `filt_ppd/rtl/mul_add.v` | PPD 版乘加引擎，用于对比位宽推导 |
| `filt_ppi/octave/stimuli.m` | 黄金参考模型（GRM）：用 `filter(b,1,upsample(...))` 给出比特真答案 |
| `filt_ppi/octave/gen_defines.m` | 生成 `defines_N.sv`，含输出位宽宏 `P_OUP_W` |
| `filt_ppi/sim/testbench/filt_ppi_tb.sv` | 测试台：展示 `i_clk`/`i_fclk` 双时钟关系与逐样本比对节拍 |

每个模块自带 `dff.v` 与 `shift_register.v` 原语（见 [u2-l2](u2-l2-dff-primitive.md)、[u2-l3](u2-l3-shift-register-and-upsample.md)），本讲不再逐行讲原语本身。

## 4. 核心概念与源码讲解

### 4.1 多相插值的对偶原理：为什么数据流要反过来

#### 4.1.1 概念说明

插值（上采样）的标准定义是：先在输入样本之间**插零**（zero-stuffing，升采样率到 \(L\) 倍），再过一条低通 FIR 滤波器 \(h\)。设零插值后的序列为 \(x_u[\cdot]\)，则第 \(n\) 个输出为卷积：

\[
y[n] = \sum_{k=0}^{N-1} h[k]\, x_u[n-k]
\]

但零插值序列里**每 \(L\) 个样本才有 \(1\) 个非零**。把下标写成 \(n=qL+r\)（\(r=0,\dots,L-1\)）后，只有 \(k\equiv r\pmod L\) 的项才碰到非零样本，于是上式塌缩成只有 \(\lceil N/L\rceil\) 项的**短卷积**：

\[
y[qL+r] = \sum_{j} h[r+jL]\, x[q-j]
\]

这就是第 \(r\) **相**子滤波器 \(h_r[j]=h[r+jL]\)。一句话总结：

- 朴素做法：在 \(L\) 倍速率上做一条 \(N\) 抽头长滤波，其中 \((L-1)/L\) 的乘法是「乘 0」——**纯浪费**。
- 多相做法：离线把长系数按相位拆成 \(L\) 条 \(\lceil N/L\rceil\) 抽头的短子滤波器，**一次算出 \(L\) 个相位的短卷积**，完全不乘零。

这正是 `mul_add` 在插值器里扮演的角色：它**一次产出 \(L\) 个并行样本**（每个样本是一条短子滤波器的输出）。

#### 4.1.2 核心流程：与抽取的对偶

把上面这条结论与抽取器对照，就得到贯穿本讲的「数据流翻转」：

```
抽取器 filt_ppd（高速率 → 低速率）：
  commutator (快 i_clk)  ──并行 M 字──▶  mul_add (慢 s_clk)  ──▶  1 个标量输出
  「收齐 M 个样本，再做 1 次大点积」

插值器 filt_ppi（低速率 → 高速率）：
  mul_add (慢 i_clk)  ──并行 L 字──▶  commutator (快 i_fclk)  ──▶  L 个标量输出（逐拍串行）
  「先做 L 次小点积，再把 L 个样本轮流送出去」
```

两者的**子模块名称相同**（都叫 `mul_add`、`commutator`），但**顺序颠倒、时钟颠倒、角色颠倒**：

| 维度 | 抽取 `filt_ppd` | 插值 `filt_ppi` |
| --- | --- | --- |
| 顺序 | commutator → mul_add | **mul_add → commutator** |
| 换向器角色 | 输入端**并行化**（串→并） | 输出端**串行化**（并→串） |
| 乘加角色 | 慢域算 **1 个大点积** | 慢域算 **\(L\) 个小点积** |
| 时钟 | 单时钟 `i_clk` + 派生 `s_clk` | 双时钟 `i_clk`(慢) + `i_fclk`(快) |
| 每输入样本产出 | \(1\) 个输出 | \(L\) 个输出 |

这条对偶关系不是巧合，而是 Noble 恒等式的直接产物（与 [u4-l2](u4-l2-cici-interpolation.md) 的 CIC 插值同源）：「↑L → 滤波」可改写为「多相滤波 → 并行展开」，于是乘加必须先于换向器完成。

#### 4.1.3 源码精读：顶层接线

顶层 [`filt_ppi.v`] 把对偶流程直接写成了两条例化：

[.drl_src_code/filt_ppi/rtl/filt_ppi.v:L31-L58](.drl_src_code/filt_ppi/rtl/filt_ppi.v#L31-L58) — 先例化 `mul_add`、再例化 `commutator`，与 PPD 的顺序正好相反。

关键看时钟连接与数据打包宽度：

```verilog
// mul_add 跑在「慢」时钟 i_clk 上，输入 1 个 i_data
mul_add #(...) ppi_mul_add (
  .i_clk  (i_clk),          // 慢：每拍收 1 个输入样本
  .i_data (i_data),
  .o_data (mul_add_2_comm)  // 输出是 L 个样本的「打包字」
);
// commutator 跑在「快」时钟 i_fclk 上，把打包字拆成 L 个串行样本
commutator #(...) ppi_commutator (
  .i_clk  (i_fclk),         // 快：每拍吐 1 个输出样本
  .i_data (mul_add_2_comm),
  .o_data (w_data)
);
```

注意两条细节，它们把「反向数据流」钉死在代码里：

1. 打包线 `mul_add_2_comm` 宽度为 `gp_interpolation_factor*gp_odata_width` 位（[.drl_src_code/filt_ppi/rtl/filt_ppi.v:L27](.drl_src_code/filt_ppi/rtl/filt_ppi.v#L27)），即 **\(L\) 个样本并排**——这正是 4.1.1 推导的「一次算出 \(L\) 个相位」。
2. `mul_add` 的时钟是 `i_clk`（慢），`commutator` 的时钟是 `i_fclk`（快），二者频率比为 \(1:L\)（由测试台约束，见 4.2.3）。

> 与 PPD 的位宽公式差异，是理解对偶的另一把钥匙，放在 4.2 讲。

#### 4.1.4 代码实践

**实践目标**：用「源码阅读 + 草稿推导」确认插值器的多相分解确实只算 \(\lceil N/L\rceil\) 项。

**操作步骤**：

1. 读 [stimuli.m](.drl_src_code/filt_ppi/octave/stimuli.m) 第 132 行的 GRM 公式：`yy = filter(b, 1, upsample(octave_data, p_interpolation_factor, p_comm_phase))`。
2. 取默认演示参数 \(L=64\)、\(N=71\)（见 `stimuli.m` 第 7、12 行）。
3. 在草稿上算：朴素零插值滤波每个输出要乘 \(N=71\) 次；多相分解后每个输出只乘 \(\lceil N/L\rceil=\lceil 71/64\rceil=2\) 次。
4. 解释：为什么 GRM 用「零插值 + 长滤波」、RTL 用「多相短滤波」，两者却给出**逐比特相同**的答案？

**预期结果**：多相实现把每输出的乘法次数从 71 降到 2，节省约 \(97\%\)；两者等价的数学根据是 4.1.1 的下标重排 \(n=qL+r\)。GRM 的「乘零」与 RTL 的「不乘」算的是同一个和，故比特真。**待本地验证**：可在 Octave 中对比 `filter(b,1,upsample(data,L))` 与手写多相循环的输出是否完全一致。

#### 4.1.5 小练习与答案

**练习 1**：若 \(L=64\)、\(N=71\)，多相分解后第 \(r=3\) 相子滤波器的系数下标是哪些？

**答案**：\(h_3[j]=h[3+jL]=h[3],\,h[67]\)，共 \(\lceil 71/64\rceil=2\) 个系数（\(j=0,1\)）。

**练习 2**：为什么说插值器的 `mul_add` 是「\(L\) 个小点积」而抽取器的 `mul_add` 是「1 个大点积」？

**答案**：插值器每个输出样本只累加 \(\lceil N/L\rceil\) 项（单相），但需要 \(L\) 个相位各算一次，共 \(L\) 个独立短和；抽取器每个输出样本要累加跨全部 \(M\) 相的 \(N\) 项，是单个长和。

---

### 4.2 filt_ppi 顶层：先 mul_add 后 commutator 的反向流水

#### 4.2.1 概念说明

4.1 讲了「为什么反过来」，本节讲「反过来之后，顶层怎么接、位宽怎么推」。两个要点：

- **双时钟**：插值器需要外部提供两个时钟——`i_clk`（慢，输入样本速率）与 `i_fclk`（快，输出样本速率，约定为 `i_clk` 的 \(L\) 倍）。`mul_add` 在慢域算完一组 \(L\) 个样本，`commutator` 在快域用 \(L\) 拍把它们轮流送出。
- **输出位宽公式**：因为每个输出样本只是**一条**短子滤波器的和（\(\lceil N/L\rceil\) 项），其位宽增长远小于抽取器那个「跨 \(M\) 相的大和」——公式形态因此与 PPD 不同。

#### 4.2.2 核心流程

顶层数据流可画成：

```
        i_clk (慢)                        i_fclk (快, = L × i_clk)
i_data ──▶ [ mul_add ] ──L 字并行──▶ [ commutator ] ──▶ o_data (每快拍 1 个样本)
                              └─ mul_add_2_comm ─┘              └─ o_sclk ─┐
                                                                          (输出有效)
相位对齐：若 gp_comm_phase≠0，在 o_data 上再串一级 shift_register（跑 i_fclk）
```

位宽推导（与 [u2-l1](u2-l1-fixed-point-bitwidth.md) 的乘加位宽增长规律一脉相承）：

- 单个乘积：\(B_{in}+B_{coeff}\)；
- 一条短子滤波器累加 \(\lceil N/L\rceil=c_{col}\) 项，**紧致**增长为 \(\lceil\log_2 c_{col}\rceil\)。

但 RTL 故意用了**保守上界**——直接加上项数 \(c_{col}\) 而非 \(\lceil\log_2 c_{col}\rceil\)：

\[
\texttt{gp\_odata\_width} = B_{in}+B_{coeff}+c_{col}
\]

这和 [u4-l3](u4-l3-cic-bitwidth-and-grm.md) 讲过的 Hogenauer 保守位宽是同一种工程取舍：多给几位符号扩展位，换取「绝不溢出」的保证，且因 GRM 用**同一公式**（见 4.2.3）而仍保比特真。

#### 4.2.3 源码精读

**① 顶层位宽公式**——注意它与 PPD 的形态差别：

[.drl_src_code/filt_ppi/rtl/filt_ppi.v:L15-L16](.drl_src_code/filt_ppi/rtl/filt_ppi.v#L15-L16)：

```verilog
parameter gp_odata_width = gp_idata_width+gp_coeff_width+(`DIV(gp_coeff_length,gp_interpolation_factor))
//                              B_in           + B_coeff       +   c_col  ( = ⌈N/L⌉ )
```

对照 PPD 顶层 [.drl_src_code/filt_ppd/rtl/filt_ppd.v:L16-L17](.drl_src_code/filt_ppd/rtl/filt_ppd.v#L16-L17)：

```verilog
parameter gp_odata_width = gp_idata_width+gp_coeff_width+$clog2(gp_decimation_factor)+$clog2(`DIV(gp_coeff_length,gp_decimation_factor))
//                              B_in + B_coeff +   ⌈log2 M⌉   +   ⌈log2 c_col⌉
```

差别正是对偶性的指纹：PPD 的输出是「跨 \(M\) 相的大和」，要把 \(M\) 条支路再求和，故多出 \(\lceil\log_2 M\rceil\) 这一段；PPI 的输出是「单相的小和」，\(M\)（即 \(L\)）这一维被**搬到外面成了并行样本数**，不再进入单个输出的累加，故位宽里没有 \(\lceil\log_2 L\rceil\)。换言之：

> PPD 把 \(L\)（即 \(M\)）这一维**折进加法树**；PPI 把这一维**折进并行输出**。这就是「反向数据流」在位宽公式上的投影。

**② GRM 用同一公式保证比特真**：gen_defines.m 生成的宏与 RTL 同构，[.drl_src_code/filt_ppi/octave/gen_defines.m:L15](.drl_src_code/filt_ppi/octave/gen_defines.m#L15)：

`` `define P_OUP_W (`P_INP_DATA_W+`P_COEFF_W+(`DIV(`P_COEFF_L,`P_INTERPOLATION))) ``

与顶层 `gp_odata_width` 一模一样。RTL 多分配的高位是符号扩展，GRM 的精确整数落在其低位，二者逐比特一致。

**③ 双时钟关系由测试台钉死**：[.drl_src_code/filt_ppi/sim/testbench/filt_ppi_tb.sv:L52-L68](.drl_src_code/filt_ppi/sim/testbench/filt_ppi_tb.sv#L52-L68)。关键三句：

```verilog
always f_clk = #(CLK_PERIOD/(`P_INTERPOLATION*2)) ~f_clk;   // 快时钟周期 = CLK_PERIOD/L
assign s_clk = (r_cnt<`P_INTERPOLATION/2) ? 1'b1 : 1'b0;    // 慢时钟周期 = CLK_PERIOD
forever #2 i_fclk = f_clk;                                   // i_fclk = 快
forever #2 i_clk  = s_clk;                                   // i_clk  = 慢 (= f_clk / L)
```

即 `i_fclk` 频率 = \(L\times\) `i_clk` 频率，正好让 `commutator` 用 \(L\) 个快拍发完 `mul_add` 在 1 个慢拍里算出的 \(L\) 个样本。

**④ 相位对齐放在输出侧**：当 `gp_comm_phase≠0` 时，顶层在 `commutator` **输出**后再串一级 `shift_register`（跑 `i_fclk`）：

[.drl_src_code/filt_ppi/rtl/filt_ppi.v:L60-L79](.drl_src_code/filt_ppi/rtl/filt_ppi.v#L60-L79)。这与 PPD 把相位对齐放在**输入**侧（`commutator` 内部，见 [u5-l2](u5-l2-commutator-decimation.md) 的 `gp_phase`）恰成镜像——因为插值器选的是「**输出相位**」，故延迟要加在输出通路上。

#### 4.2.4 代码实践

**实践目标**：手算对比 PPD/PPI 在相同 \(N\)、不同速率转换下的输出位宽，体会「\(L\) 这一维搬去并行」的效果。

**操作步骤**：

1. 取 \(B_{in}=8\)、\(B_{coeff}=16\)、\(N=53\)、速率因子 \(=30\)。
2. 按 PPI 公式算：`gp_odata_width = 8+16+⌈53/30⌉ = 8+16+2 = 26`。
3. 按 PPD 公式算（把 30 当作 \(M\)）：`gp_odata_width = 8+16+⌈log2 30⌉+⌈log2 ⌈53/30⌉⌉ = 8+16+5+2 = 31`。
4. 解释两者差 5 位的来源。

**预期结果**：差的正是 PPD 多出的 \(\lceil\log_2 M\rceil=5\) 位（跨相求和的增长）。PPI 没有这一段，因为它不为「跨相」分配位宽——\(L\) 个相是并行样本，不是同一个数里的累加项。**待本地验证**：可用 iverilog 分别 elaborate 两个顶层、用 `$display` 打印 `gp_odata_width` 核对。

#### 4.2.5 小练习与答案

**练习 1**：为什么插值器必须用两个外部时钟，而抽取器只用一个？

**答案**：抽取器输入快、输出慢，可用单快时钟 `i_clk` 加一个派生的慢脉冲 `s_clk` 同步；插值器输入慢、输出快，慢域 `mul_add` 与快域 `commutator` 分属不同速率，需要外部分别供给 `i_clk`（慢）与 `i_fclk`（快），约定 `i_fclk = L·i_clk`。

**练习 2**：PPI 的 `gp_odata_width` 用 `+c_col` 而非 `+⌈log2 c_col⌉`，会破坏比特真吗？

**答案**：不会。`c_col ≥ ⌈log2 c_col⌉`（对 \(c_{col}\ge2\) 恒成立），多出的高位全是符号扩展；GRM 用同一公式定义 `P_OUP_W`，精确整数落在低位，逐比特仍一致。

---

### 4.3 PPI commutator：用 r_idx 索引式串行输出

#### 4.3.1 概念说明

这是本讲的核心改造。回顾 PPD 版换向器（[u5-l2](u5-l2-commutator-decimation.md)）：它的输入是**串行单样本** `i_data`，用环形计数器把每拍到来的同一样本按相位**捕获**到 \(M\) 个槽里，然后**把整字 \(M\) 个样本并行输出**给 `mul_add`。一句话：**串入并出**。

PPI 版换向器的任务正好相反：它的输入是 `mul_add` 送来的**\(L\) 个样本的并行字**，它要在 \(L\) 个快拍里**每次挑出一个**样本送到输出端。一句话：**并入串出**。

为了「每次挑一个」，PPI 版引入了一个 PPD 版没有的新寄存器——**二进制索引计数器 `r_idx`**，它像多路选择器（MUX）的地址指针一样，逐拍递增，从 \(L\) 个捕获槽里选出当前要输出的那一个。

#### 4.3.2 核心流程

PPI 换向器在一个慢周期（= \(L\) 个快拍）内做三件事，彼此锁步：

```
复位后，每个快拍 i_fclk：
  ① 环形计数器 r_ring_cnt（一位热码）转一格 → 第 x 位为 1 时，
     把并行输入的第 x 个切片捕获进槽 x（结构镜像 PPD，复用代码）
  ② 二进制索引 r_idx 递增 0→1→…→L-1→0
  ③ 输出 = w_data 的第 r_idx 个切片   ← 关键：用 r_idx 当 MUX 地址
一个慢周期结束（r_ring_cnt 走完一圈），o_clk (=r_done) 拉高一个快拍，作为输出有效选通
```

数学上，第 \(t\) 个快拍输出的是并行字的第 \((t\bmod L)\) 个切片：

\[
\texttt{o\_data}[t] = \texttt{w\_data}\big[\,(r\_idx(t))\,\text{ 切片}\,\big],\qquad r\_idx(t)=t\bmod L
\]

这就把一个 \(L\times W\) 位的并行字在 \(L\) 拍内「摊平」成 \(L\) 个 \(W\) 位串行样本。

#### 4.3.3 源码精读

**① 端口与新增索引寄存器**：

[.drl_src_code/filt_ppi/rtl/commutator.v:L6-L30](.drl_src_code/filt_ppi/rtl/commutator.v#L6-L30)。注意三个与 PPD 版不同之处：输入 `i_data` 是**并行宽字**（`gp_interpolation_factor*gp_idata_width` 位），输出 `o_data` 是**单样本**（`gp_idata_width` 位）；并多了 `r_idx` 这个二进制计数器，其位宽用 `$clog2(L)+1`：

```verilog
localparam c_idx_width = $clog2(gp_interpolation_factor)+1;  // 索引位宽
reg     [c_idx_width-1:0] r_idx;                              // 新增：输出选择地址
```

**② 二进制索引计数器逐拍递增**（以 CCW 分支为例，即默认 `gp_ccw=1`）：

[.drl_src_code/filt_ppi/rtl/commutator.v:L111-L114](.drl_src_code/filt_ppi/rtl/commutator.v#L111-L114)：

```verilog
if (r_idx < gp_interpolation_factor-1)
  r_idx <= r_idx+1'b1;
else
  r_idx <= 'd0;           // 数满 L 归零，周而复始
```

CW 分支同样有这段（[L59-L62](.drl_src_code/filt_ppi/rtl/commutator.v#L59-L62)）。这是 PPD 版完全没有的逻辑——PPD 不需要索引，因为它输出整字。

**③ 输出选择：用 `r_idx` 当切片地址**——本节最关键的两行：

CCW：[.drl_src_code/filt_ppi/rtl/commutator.v:L133](.drl_src_code/filt_ppi/rtl/commutator.v#L133)

```verilog
assign o_data = (r_idx<gp_interpolation_factor) ? w_data[(r_idx+1)*gp_idata_width-1 -: gp_idata_width] : 'd0;
```

CW：[.drl_src_code/filt_ppi/rtl/commutator.v:L82](.drl_src_code/filt_ppi/rtl/commutator.v#L82)

```verilog
assign o_data = w_data[(gp_interpolation_factor-r_idx)*gp_idata_width-1 -: gp_idata_width];
```

两行都是 `w_data[ (关于 r_idx 的表达式) * W - 1 -: W ]`，即「**以 `r_idx` 为地址，从并行字里切出一个 \(W\) 位样本**」。CW 与 CCW 的索引表达式不同（一个从低位端、一个从高位端数），对应换向方向相反，但本质都是「索引式 MUX 输出」。对照 PPD 版的输出（[.drl_src_code/filt_ppd/rtl/commutator.v:L116](.drl_src_code/filt_ppd/rtl/commutator.v#L116) `assign o_data = w_data;`）——PPD 直接把整字搬出去，根本没有索引。这就是两版换向器最本质的一行差异。

**④ 捕获部分结构上镜像 PPD**：[.drl_src_code/filt_ppi/rtl/commutator.v:L120-L131](.drl_src_code/filt_ppi/rtl/commutator.v#L120-L131) 仍用 `r_ring_cnt[x]` 当捕获 `dff` 的时钟，逐切片把并行输入再寄存一遍。由于 `mul_add` 输出在整个慢周期内是稳定的，这里的捕获主要作用是「**为 `r_idx` 的 MUX 提供一个干净的逐槽寄存器组**」，同时让本模块在结构上与 PPD 版同构、便于复用 `dff` 原语与一位热码计数器写法。

#### 4.3.4 代码实践

**实践目标**：亲眼看到 `r_idx` 如何把一个并行字「摊平」成串行样本。

**操作步骤**（源码阅读型实践）：

1. 打开 [.drl_src_code/filt_ppi/rtl/commutator.v](.drl_src_code/filt_ppi/rtl/commutator.v)，聚焦 CCW 分支。
2. 假设 \(L=4\)、\(W=8\)，`mul_add` 送来的并行字 `i_data = {D3,D2,D1,D0}`（每个 \(D_x\) 为 8 位）。
3. 逐拍填表（`r_idx` 与 `o_data` 的对应）：

   | 快拍 | `r_idx` | `o_data` 取值（按 L133 的 CCW 公式） |
   | --- | --- | --- |
   | 0 | 0 | D0 |
   | 1 | 1 | D1 |
   | 2 | 2 | D2 |
   | 3 | 3 | D3 |

4. 验证 CW 公式（L82）在相同输入下给出的是 D3,D2,D1,D0（方向相反）。

**预期结果**：CCW 按 D0→D3 顺序串行输出，CW 按 D3→D0 顺序输出，每拍一个样本，\(L\) 拍发完一个并行字。这正对应 4.1.2 里「插值器输出端串行化」的角色。**待本地验证**：若环境有 iverilog，可把 `gp_interpolation_factor` 设小（如 4），给 `i_data` 喂一个固定并行字，dump VCD 观察 `r_idx` 与 `o_data` 波形是否与上表一致。

#### 4.3.5 小练习与答案

**练习 1**：为什么 PPI 版必须有 `r_idx`，而 PPD 版没有？

**答案**：PPI 版要把 \(L\) 个并行样本在 \(L\) 拍内**逐个**送出，需要一个地址指针轮询 \(L\) 个槽，故引入 `r_idx` 当 MUX 地址；PPD 版输出的是**整字**（\(M\) 个样本一起交给 `mul_add`），无需选择，故没有索引。

**练习 2**：`r_idx` 的位宽为何写成 `$clog2(L)+1` 而不是 `$clog2(L)`？

**答案**：多 1 位是安全冗余。`$clog2(L)` 对 2 的幂恰好够编 \(0\ldots L-1\)，但对**非 2 的幂**（如默认 \(L=30\)、64）需要向上取整，且复位初值被设为全 1（`{c_idx_width{1'b1}}`，见 [L44](.drl_src_code/filt_ppi/rtl/commutator.v#L44)/[L96](.drl_src_code/filt_ppi/rtl/commutator.v#L96)），多 1 位避免初值越界并简化比较 `r_idx < L-1`。

---

### 4.4 PPD 与 PPI 两个 commutator.v 的关键差异

#### 4.4.1 概念说明

两个文件都叫 `commutator.v`，连环形计数器写法都很像，极易混淆。本节把它们**并排**对比，给出三处最关键的差异，并回答本讲的核心问题：**为什么插值器需要索引式输出？**

一句话答案：因为插值是「1 进 \(L\) 出」，换向器必须**把一组并行结果摊成串行流**，索引式 MUX 是天然的「并→串」转换器；而抽取是「\(M\) 进 1 出」，换向器只需**把串行输入收成并行字**，整字直出即可，无需索引。

#### 4.4.2 核心流程：三处关键差异对照

| 差异点 | PPD 版 `commutator.v` | PPI 版 `commutator.v` |
| --- | --- | --- |
| **① 端口/参数** | 输入窄（单样本）、输出宽（\(M\) 字并行）；有 `gp_reg_oup`（可选输出寄存）、`gp_phase`（输入相位对齐） | 输入宽（\(L\) 字并行）、输出窄（单样本）；**无** `gp_reg_oup`、**无** `gp_phase`（相位对齐移到顶层输出侧，见 4.2.3④） |
| **② 输出选择逻辑** | `assign o_data = w_data;`（整字直出，可选再整字寄存） | `assign o_data = w_data[(r_idx±1)*W-1 -: W];`（**用 `r_idx` 索引切片**，逐拍选一个） |
| **③ 换向器角色** | 输入端**并行化**（串→并），配合 `mul_add` 做 1 次大点积 | 输出端**串行化**（并→串），把 `mul_add` 的 \(L\) 个结果轮流送出 |

另有三处次要但可观察的差异（阅读源码时值得留意）：

- **方向参数的接法**：PPD 顶层把 `gp_comm_ccw` 透传给换向器（[.drl_src_code/filt_ppd/rtl/filt_ppd.v:L32](.drl_src_code/filt_ppd/rtl/filt_ppd.v#L32) `.gp_ccw(gp_comm_ccw)`）；PPI 顶层却把换向器的 `gp_ccw` **硬编码为 `1'b1`**（[.drl_src_code/filt_ppi/rtl/filt_ppi.v:L48](.drl_src_code/filt_ppi/rtl/filt_ppi.v#L48) `.gp_ccw(1'b1)`），即插值换向器恒为 CCW，顶层 `gp_comm_ccw` 参数实际未作用于换向器（`gp_mul_ccw` 才传给 `mul_add`）。
- **CCW 分支的时钟沿**：PPI 版 CCW 环形计数器跑在 `negedge i_clk`（[.drl_src_code/filt_ppi/rtl/commutator.v:L91](.drl_src_code/filt_ppi/rtl/commutator.v#L91)），CW 跑 `posedge`（[L39](.drl_src_code/filt_ppi/rtl/commutator.v#L39)）；PPD 版 CW/CCW **都用 `posedge`**（[L42](.drl_src_code/filt_ppd/rtl/commutator.v#L42)/[L126](.drl_src_code/filt_ppd/rtl/commutator.v#L126)）。PPI 用半周期错沿，推测是为了让计数器更新与捕获/索引选择在快时钟的相位上对齐，**精确时序 rationale 待用波形本地确认**。
- **`done`/`o_clk` 来源相同**：两版都用一位热码「最后一拍」打一拍得 `r_done`，再 `assign o_clk = r_done`（PPI [L140](.drl_src_code/filt_ppi/rtl/commutator.v#L140)、PPD [L205](.drl_src_code/filt_ppd/rtl/commutator.v#L205)），频率都是 \(f_{clk}/L\)——这是两版**唯一形神皆同**的部分。

#### 4.4.3 源码精读：把三处差异落到的代码行

- **端口/参数差异**：PPD [.drl_src_code/filt_ppd/rtl/commutator.v:L6-L20](.drl_src_code/filt_ppd/rtl/commutator.v#L6-L20)（含 `gp_reg_oup`、`gp_phase`、宽输出）vs PPI [.drl_src_code/filt_ppi/rtl/commutator.v:L6-L18](.drl_src_code/filt_ppi/rtl/commutator.v#L6-L18)（宽输入、窄输出、多 `c_idx_width`/`r_idx`）。
- **输出选择差异**：PPD 整字输出 [.drl_src_code/filt_ppd/rtl/commutator.v:L181-L199](.drl_src_code/filt_ppd/rtl/commutator.v#L181-L199)（`gp_reg_oup` 决定是否再寄存整字）vs PPI 索引输出 [.drl_src_code/filt_ppi/rtl/commutator.v:L133](.drl_src_code/filt_ppi/rtl/commutator.v#L133)。
- **角色差异（顶层顺序）**：PPD「换向器→乘加」[.drl_src_code/filt_ppd/rtl/filt_ppd.v:L31-L61](.drl_src_code/filt_ppd/rtl/filt_ppd.v#L31-L61)（`commutator` 在前、`mul_add` 在后且用派生 `s_clk`）vs PPI「乘加→换向器」[.drl_src_code/filt_ppi/rtl/filt_ppi.v:L31-L58](.drl_src_code/filt_ppi/rtl/filt_ppi.v#L31-L58)（`mul_add` 在前、`commutator` 在后且用外部 `i_fclk`）。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：并排阅读两个 `commutator.v`，亲手归纳三处关键差异，并解释索引式输出的必要性。

**操作步骤**：

1. 分别打开 [.drl_src_code/filt_ppd/rtl/commutator.v](.drl_src_code/filt_ppd/rtl/commutator.v) 与 [.drl_src_code/filt_ppi/rtl/commutator.v](.drl_src_code/filt_ppi/rtl/commutator.v)。
2. 按 4.4.2 的三行表格，逐一在两个文件里找到对应代码行，**自己填写**端口宽度、输出赋值表达式、所在顶层位置。
3. 写一段话回答：「为什么插值器需要 `r_idx` 索引式输出，而抽取器直接输出整字？」提示从「每输入样本产出几个输出样本」切入。
4. （可选，需 iverilog）把两个换向器分别 elaborate（`iverilog -g2012 -DDUMP` 或直接用顶层 `-demo`/`-d` 流程），用 `$display` 打印各自的 `o_data` 位宽，验证「PPD 宽出、PPI 窄出」。

**预期结果**：PPD 的 `o_data` 位宽 = \(M\times W\)（整字），PPI 的 `o_data` 位宽 = \(W\)（单样本）。索引式输出的必要性：插值器「1 进 \(L\) 出」，必须把 `mul_add` 一次算出的 \(L\) 个并行样本在 \(L\) 个快拍里**逐个**送出，`r_idx` 索引 MUX 正是完成「并→串」的天然部件；抽取器「\(M\) 进 1 出」，换向器只需收集，无需选择，故整字直出。**步骤 4 的运行结果待本地验证**（环境需装有 iverilog + octave）。

#### 4.4.5 小练习与答案

**练习 1**：如果把 PPI 版换向器的输出改成 `assign o_data = w_data;`（像 PPD 那样整字直出），会发生什么？

**答案**：类型与位宽首先不匹配（PPI 的 `o_data` 只有 \(W\) 位，而 `w_data` 是 \(L\times W\) 位）；即便强改位宽，语义也错了——顶层期望每拍得到**一个**插值样本，整字直出会让后续无处接收 \(L\) 个并行样本，破坏「并→串」插值输出。

**练习 2**：PPI 版换向器既已有 `r_idx` 索引输出，为何还保留 `r_ring_cnt` 一位热码捕获？

**答案**：捕获部分把并行输入的每个切片再寄存进独立 `dff`，为 `r_idx` 的 MUX 提供干净、无毛刺的逐槽寄存器组；同时让本模块在结构上与 PPD 版同构，复用一位热码计数器与 `dff` 原语的成熟写法（见 4.3.3④）。

---

## 5. 综合实践

把本讲三块知识串起来，完成一次「**反向数据流追踪**」：

1. **起点**：从 [.drl_src_code/filt_ppi/octave/stimuli.m:L132](.drl_src_code/filt_ppi/octave/stimuli.m#L132) 的 GRM `yy=filter(b,1,upsample(data,L,p_comm_phase))` 出发，写下它对应的「零插值 + 长滤波」数据流图。
2. **翻译成多相**：按 4.1.1 把它改写为「\(L\) 条短子滤波器并行」，标出每条子滤波器的系数下标集 \(h[r+jL]\)。
3. **落到 RTL**：在 [.drl_src_code/filt_ppi/rtl/filt_ppi.v](.drl_src_code/filt_ppi/rtl/filt_ppi.v) 上画出 `mul_add`（慢 `i_clk`，产 \(L\) 字）→ `commutator`（快 `i_fclk`，按 `r_idx` 串行输出）的反向流水框图，标出打包线宽度 `L*gp_odata_width` 与两时钟频率比 \(1:L\)。
4. **对照抽取**：在同一张图上并列画出 `filt_ppd` 的正向流水（`commutator`→`mul_add`），用三种颜色标出 4.4.2 的三处差异（端口、输出选择、角色）。
5. **验证比特真**：说明 RTL 的保守位宽 `gp_odata_width = B_in+B_coeff+c_col`（[filt_ppi.v:L15-L16](.drl_src_code/filt_ppi/rtl/filt_ppi.v#L15-L16)）与 GRM 的 `P_OUP_W`（[gen_defines.m:L15](.drl_src_code/filt_ppi/octave/gen_defines.m#L15)）同公式，故输出逐比特一致。

**交付物**：一张含 GRM→多相→RTL 三层对应关系的手绘或文字框图，加一段 150 字的「为何数据流要反过来」的总结。若环境允许，跑一次 `./dsp_rtl_lib.sh -d`（指定 `filt_ppi`）或 `-demo`，记录插值回归的 PASSED/FAILED 作为比特真佐证；**命令运行结果待本地验证**。

## 6. 本讲小结

- **对偶原理**：插值是抽取的镜像。Noble 恒等式把「↑L → 滤波」改写成「多相滤波 → 并行展开」，于是数据流从 PPD 的 `commutator→mul_add` 翻转为 PPI 的 `mul_add→commutator`。
- **双时钟反向流水**：`filt_ppi` 用慢 `i_clk` 驱动 `mul_add` 一次算出 \(L\) 个并行样本，用快 `i_fclk`（= \(L\times\) 慢）驱动 `commutator` 在 \(L\) 拍内把它们逐个送出。
- **位宽公式的指纹**：PPI 输出 `B_in+B_coeff+c_col` 比 PPD 少了 \(\lceil\log_2 M\rceil\) 段——因为 \(L\) 这一维被「搬去并行输出」而非「折进加法树」；RTL 用保守上界 `+c_col`，靠 GRM 同公式保比特真。
- **核心改造：`r_idx` 索引输出**：PPI 换向器新增二进制索引计数器 `r_idx`，把并行字按 `w_data[(r_idx±1)*W-1 -: W]` 逐拍切片输出，实现「并→串」；PPD 无此机制，直接整字直出。
- **三处关键差异**：端口/参数（宽入窄出、无 `gp_reg_oup`/`gp_phase`）、输出选择（`r_idx` 索引 vs 整字）、换向器角色（串行化 vs 并行化）；另有 `gp_ccw` 硬编码、CCW 用 `negedge` 两处次要差异。
- **为何插值需要索引输出**：插值「1 进 \(L\) 出」，必须把一组并行结果摊成串行流，索引式 MUX 是天然「并→串」部件；抽取「\(M\) 进 1 出」只需收集，故整字直出即可。

## 7. 下一步学习建议

- **横向打通多速率族**：本讲的多相插值与 [u4-l2](u4-l2-cici-interpolation.md) 的 CIC 插值、[u2-l3](u2-l3-shift-register-and-upsample.md) 的 `upsample` 原语同属「升采样」家族，建议把它们的数据流并排画一张总图，体会「零插值（CIC/upsample）」与「多相短滤波（PPI）」两种实现升采样的思路差异。
- **进入验证方法学**：多相族至此讲完。下一站建议读 [u7-l1 比特真验证方法论](u7-l1-bittrue-verification.md)，用本讲反复出现的「GRM `filter(b,1,upsample(...))` → `defines_N.sv` 宏注入 → TB 逐样本比对」闭环，把零散的比特真知识固化为可复用方法。
- **二次开发**：若要自创多速率模块，参考 [u7-l3 dev 模式](u7-l3-dev-mode-scaffolding.md) 用 `-dev` 生成脚手架，再以 `filt_ppi` 为模板改造——重点复用 `dff`/`shift_register` 原语与「`r_idx` 索引输出」模式。
- **源码延伸阅读**：精读 [.drl_src_code/filt_ppi/rtl/mul_add.v](.drl_src_code/filt_ppi/rtl/mul_add.v) 末尾的系数矩阵注释块（[L243-L267](.drl_src_code/filt_ppi/rtl/mul_add.v#L243-L267)），它把 TF/DF × CW/CCW 四象限的系数铺砖方向画得很清楚，是理解四象限乘法编排的最佳抓手。
