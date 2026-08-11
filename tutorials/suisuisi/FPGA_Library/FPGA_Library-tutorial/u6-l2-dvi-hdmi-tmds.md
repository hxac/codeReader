# DVI/HDMI 输出与 TMDS 编码

> 所属单元：Unit 6 FPGA 图形与显示系统 · 依赖讲义：[u6-l1 显示时序与 VGA/720p 信号生成](u6-l1-display-timing.md)

## 1. 本讲目标

上一讲（u6-l1）我们用 `display_480p` / `display_720p` 生成了一组「逻辑视频信号」：行场同步 `hsync/vsync`、数据使能 `de`、以及每个像素的屏幕坐标 `sx/sy`。这些信号还停留在 FPGA 内部，是并行的、单端的数字量。要把它们真正送到显示器，必须解决两个工程难题：**怎么把并行像素变成能在长线上高速传输的串行差分信号**，以及**怎么保证接收端在没有共享时钟的情况下还能正确恢复数据**。

本讲围绕 projf 库的 TMDS（Transition-Minimized Differential Signaling，最小化跳变差分信号）实现，讲清从「逻辑像素」到「HDMI 线缆上的差分电平」的完整通路。读完本讲你应当能够：

- 说清 DVI/HDMI 为什么要把 8 位像素编码成 10 位，以及这多出来的 2 位各买到了什么好处；
- 读懂 [tmds_encoder_dvi.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/display/tmds_encoder_dvi.sv) 里「异或/异或非最小化跳变 + 计数器直流平衡」的两级编码流程；
- 看懂 Xilinx 7 系（XC7）用 `OSERDESE2` 串行化 + `OBUFDS` 差分输出、而 iCE40 用 `SB_IO` 驱动外置 DVI Pmod 这两种「最后一公里」实现为何不同；
- 跟踪一条完整的数据通路：时钟生成 → 显示时序 → TMDS 编码 → 10:1 串行化 → 差分引脚。

## 2. 前置知识

- **差分信号（Differential Signaling）**：用两根线（正端 `pin_p`、负端 `pin_n`）传同一信号的正反相，接收端看两者之差。它的抗共模干扰能力远强于单端信号，DVI/HDMI/USB/以太网都用它。
- **AC 耦合与直流平衡**：HDMI 链路在发送端和接收端之间串了耦合电容，隔直流。如果信号里 0 和 1 的数量长期不等（有直流偏置），电容会被充放电导致基线漂移、眼图劣化。所以 TMDS 必须「直流平衡」：让一段时间内传出去的 0 和 1 数量大致相等。
- **时钟数据恢复（CDR）**：DVI 每个颜色通道并不附带独立时钟，接收端靠信号自身的电平跳变「边沿」来对齐比特。因此跳变既不能太少（否则恢复不出时钟），也不能太多（否则电磁辐射 EMI 过大、功耗高）。
- **DDR 与串行化**：DDR（Double Data Rate）指数据在时钟的上升沿和下降沿都采样。把 10 位并行数据用 5 倍像素频率的时钟、在双沿上送出，等效速率就是 \(10 \times f_{\text{pix}}\)，正好把 10 位「摊」到一个像素周期里。这就是 10:1 串行化。

> 本讲用到上一讲 u6-l1 的 `de/hsync/vsync/sx/sy`，以及 u5-l1 讲过的「厂商相关原语放进平台子目录」的库组织纪律。

## 3. 本讲源码地图

| 文件 | 作用 | 平台 |
| --- | --- | --- |
| [lib/display/tmds_encoder_dvi.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/display/tmds_encoder_dvi.sv) | TMDS 单通道编码器：8 位像素 + 控制位 → 10 位 TMDS 码字 | 纯逻辑，平台中立 |
| [lib/display/xc7/dvi_generator.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/display/xc7/dvi_generator.sv) | 把 3 个颜色通道 + 1 个时钟通道组装起来 | XC7 |
| [lib/display/xc7/oserdes_10b.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/display/xc7/oserdes_10b.sv) | 10:1 输出串行化器（OSERDESE2 主从） | XC7 |
| [lib/display/xc7/tmds_out.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/display/xc7/tmds_out.sv) | 单端转差分输出（OBUFDS） | XC7 |
| [lib/clock/xc7/clock_480p.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/clock/xc7/clock_480p.sv) | 生成像素时钟与 5× 串行时钟（MMCM） | XC7 |
| [graphics/fpga-graphics/ice40/top_square.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/fpga-graphics/ice40/top_square.sv) | iCE40 顶层：用 `SB_IO` 把并行视频送给外置 DVI Pmod | iCE40 |
| [graphics/racing-the-beam/xc7-dvi/top_hello.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/racing-the-beam/xc7-dvi/top_hello.sv) | XC7 完整 DVI 信号链示例顶层 | XC7 |

> 一个关键区分：`tmds_encoder_dvi.sv` 是**平台中立的纯逻辑**，放在分区根目录；而 `oserdes_10b` / `tmds_out` 调用 Xilinx 硬核原语，必须放进 `xc7/` 子目录——这正是 u5-l1 讲过的「厂商中立纪律」。

---

## 4. 核心概念与源码讲解

### 4.1 TMDS 编码原理与 tmds_encoder_dvi

#### 4.1.1 概念说明

DVI 和 HDMI 的每一条颜色通道都是一条 TMDS 链路，每个像素周期送出一个 10 位码字。编码器接收 8 位像素（或消隐期的 2 位控制信号），输出 10 位。这「8→10」的额外开销并非浪费，它买到了三样东西：

1. **跳变最小化**：让连续比特之间的电平翻转尽量少，降低 EMI 与功耗，同时仍保证有足够跳变供 CDR 用；
2. **直流平衡**：让每个码字、以及码字之间累计的 0/1 数量趋于相等，适配 AC 耦合链路；
3. **控制字符**：在消隐期（`de=0`）传送固定模式的码字，用来承载 `hsync/vsync`，并为接收端提供「跳变特别丰富」的同步参考。

设计上的巧妙之处在于：跳变最小化用 1 位标志位（第 9 位）记录「我用了 XOR 还是 XNOR」，直流平衡再用 1 位标志位（第 10 位）记录「我有没有把整组取反」。解码端凭借这两位就能无损还原。因此 \(8 \text{ 位数据} \xrightarrow{\text{最小化}} 9 \text{ 位} \xrightarrow{\text{平衡}} 10 \text{ 位}\)，正好需要 10 位传输。

#### 4.1.2 核心流程

TMDS 编码分两级，全部在 `tmds_encoder_dvi` 内完成：

```text
输入 8 位 data_in
   │
   ├─ 第一级：最小化跳变（组合逻辑）
   │     1. 数 1 的个数 data_1s
   │     2. 决定用 XOR 还是 XNOR：use_xnor
   │     3. 链式 XOR/XNOR 生成 9 位 enc_qm[8:0]
   │        enc_qm[8] 记录「用的是 XNOR(0) 还是 XOR(1)」
   │
   ├─ 第二级：直流平衡（时序逻辑，每像素更新）
   │     4. 统计 enc_qm[7:0] 的 ones/zeros，算 balance = ones - zeros
   │     5. 结合上拍累计偏置 bias，决定是否对 8 位取反
   │     6. bit9 记录「是否取反」，拼成 10 位 tmds
   │
   └─ 消隐期分支（de==0）：直接输出 4 个固定控制字符之一
```

直流平衡的目标是把累计偏置约束在小区间内。设第 \(k\) 个码字自身的「1 比 0 多」个数为 \(b_k = \text{ones}_k - \text{zeros}_k\)，累计偏置记为 \(B_k\)。编码器通过「必要时整组取反」让 \(B_k\) 始终趋向 0：

\[ B_k = B_{k-1} \pm b_k \quad(\text{取反则 } b_k \text{ 变号}) \]

取反与否由 `bias` 与 `balance` 的符号决定（见 4.1.3）。

#### 4.1.3 源码精读

模块端口很简洁：一个像素时钟域，输入 8 位颜色、2 位控制、`de`，输出 10 位码字。

[tmds_encoder_dvi.sv:8-15](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/display/tmds_encoder_dvi.sv#L8-L15) 声明端口；其中 `data_in` 是颜色、`ctrl_in` 是控制信号、`de` 决定当前周期传数据还是传控制字符。

**第一级：最小化跳变。** 先数 1 的个数，再据此选 XOR 或 XNOR：

[tmds_encoder_dvi.sv:18-26](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/display/tmds_encoder_dvi.sv#L18-L26) 计算 `data_1s` 与 `use_xnor`。直觉是：当输入里 1 偏多时用 XNOR，偏少时用 XOR，使生成的序列里 0/1 数量可控。

```verilog
data_1s = data_in[0] + data_in[1] + ... + data_in[7];
use_xnor = (data_1s > 4'd4) || ((data_1s == 4'd4) && (data_in[0] == 0));
```

随后用链式 XOR/XNOR 生成 9 位 `enc_qm`——后一位是前一位与本位输入的 XOR（或 XNOR），这等价于「记录相邻比特之间是否发生跳变」，从而把跳变数压到最低：

[tmds_encoder_dvi.sv:29-39](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/display/tmds_encoder_dvi.sv#L29-L39)。第 9 位 `enc_qm[8]` 用 0/1 记录「用了 XNOR 还是 XOR」，供解码端还原。

**第二级：直流平衡。** 先算 `enc_qm[7:0]` 的 1/0 差：

[tmds_encoder_dvi.sv:42-50](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/display/tmds_encoder_dvi.sv#L42-L50)，得到带符号的 `balance = ones - zeros`（范围 −8…+8，所以用 5 位有符号数）。

接着是核心的时序逻辑——用 `bias` 寄存器记住累计偏置，分情况决定是否取反：

[tmds_encoder_dvi.sv:53-90](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/display/tmds_encoder_dvi.sv#L53-L90)。三段分支：

- **消隐期（`de==0`）**：直接输出控制字符（共 4 种，由 `ctrl_in` 选择），并把 `bias` 清零。控制字符是固定 10 位模式，**每个都恰好有 7 次跳变**，给接收端提供强同步参考：

```verilog
2'b00:   tmds <= 10'b1101010100;
2'b01:   tmds <= 10'b0010101011;
2'b10:   tmds <= 10'b0101010100;
default: tmds <= 10'b1010101011;
```

- **数据期，无历史偏置或本字已平衡（`bias==0 || balance==0`）**：按 `enc_qm[8]` 决定是否取反，并把本字的 `balance` 累计进 `bias`。
- **数据期，`bias` 与 `balance` 同号**（继续累积会更偏）：对 8 位**取反**抵消，`bit9=1`；
- **数据期，二者异号**（天然抵消）：**不取反**，`bit9=0`。

第 9 位 `enc_qm[8]` 与第 10 位（取反标志）一起随码字送出，解码端凭这两位即可还原原始 8 位。复位时输出等效于控制字符 `2'b00`，并把 `bias` 清零。

#### 4.1.4 代码实践

**实践目标**：手工走一遍 TMDS 编码，验证「8→9→10」两级流程，并解释为什么是 10 位。

**操作步骤**（纸笔即可，也可写个小 Python 复核）：

1. 取输入 `data_in = 8'b1010_0001`（两个 1）。
2. 数 1：`data_1s = 2`，因为 `data_1s < 4`，得 `use_xnor = 0`（用 XOR）。
3. 链式 XOR 生成 `enc_qm[8:0]`：`enc_qm[0]=1`，后续位是前一位异或当前输入位；`enc_qm[8]=1`（表示用了 XOR）。
4. 统计 `enc_qm[7:0]` 的 `ones/zeros`，算 `balance`；假设 `bias` 初值为 0，落到「`bias==0`」分支。
5. 由 `enc_qm[8]==1` 选 `{2'b01, enc_qm[7:0]}`，得到最终 10 位码字，并把 `balance` 累计进下一拍的 `bias`。

**需要观察的现象**：输出的 10 位里相邻比特跳变很少（最小化生效）；若连续编码多个全 0 或全 1 像素，`bias` 会在正负之间被「拉回」，码字会周期性地整组取反（直流平衡生效）。

**预期结果**：8 位像素被无损映射到 10 位，且第 9 位=最小化标志、第 10 位=取反标志。多余 2 位正是「最小化跳变」与「直流平衡」的代价。

**为什么需要 10 位？** 因为 8 位原始数据需要 1 位记录最小化方式（XOR/XNOR）→ 9 位，再需要 1 位记录是否取反以平衡直流 → 10 位。这两位不是冗余，而是换来 EMI 低、可时钟恢复、可 AC 耦合三大工程收益。

> 说明：本仓库未随附 TMDS 的解码器或对照测试台，上述手算结果**待本地用独立 TMDS 参考实现验证**。

#### 4.1.5 小练习与答案

**练习 1**：控制字符为什么固定有 7 次跳变，而数据字符「至多 5 次」？

**参考答案**：数据字符经最小化后跳变被压到很少（0…5 次）；而消隐期没有像素要传，正好用跳变最丰富的固定模式（7 次）给接收端 CDR 提供最稳的同步参考，同时这些模式本身也直流平衡。

**练习 2**：若取消第二级（不做直流平衡、只输出 9 位 `enc_qm`），系统还能工作吗？为什么？

**参考答案**：短时可能出图，但链路经 AC 耦合电容后基线会随数据内容漂移，长时间运行眼图闭合、误码飙升。直流平衡是 AC 耦合差分链路的硬性要求。

**练习 3**：`bias` 寄存器为什么用「有符号 5 位」？

**参考答案**：`balance = ones - zeros` 范围是 −8…+8，需要 5 位含符号位才能完整表示；`bias` 是它累加的结果，同样用有符号 5 位。

---

### 4.2 差分输出原语：OBUFDS / OSERDESE2 与 SB_IO

#### 4.2.1 概念说明

`tmds_encoder_dvi` 输出的是 10 位**并行**码字，还在像素时钟域（频率不高）。要在 HDMI 线缆上传送，需要两步硬件转换：

1. **串行化（Serialize）**：把 10 位并行 → 1 位高速串行，速率 \(10 \times f_{\text{pix}}\)；
2. **单端转差分**：把 FPGA 内部的单端信号转成线缆需要的差分对（`pin_p` / `pin_n`）。

这两步都依赖 FPGA 厂商的硬核原语，无法用通用 Verilog 实现——这也正是 projf 把它们放进平台子目录的原因。两种平台的做法差异很大：

- **Xilinx 7 系（XC7）**：片内完成全部工作。用 `OSERDESE2` 做 10:1 DDR 串行化，再用 `OBUFDS` 转成 TMDS 差分电平，直接驱动 HDMI 连接器的 3 对数据差分对 + 1 对时钟差分对。
- **Lattice iCE40**：projf 的 iCE40 例子**不在片内做 TMDS**，而是把并行的 RGB + 同步 + 时钟通过 `SB_IO`（iCE40 的可配置 I/O 单元）送到一块外置 **DVI Pmod**（上面有专用 TMDS 编码芯片），由 Pmod 完成编码与差分输出。

所以「差分输出实现差异」的本质是：XC7 把 TMDS 编码+串行化+差分全部吃在片内；iCE40 受限于硬核资源，把 TMDS 编码外移到 Pmod，片内只用 `SB_IO` 做寄存/DDR 输出。

#### 4.2.2 核心流程

**XC7 差分输出链**：

```text
10 位并行码字 ──► OSERDESE2 (DDR, 10:1) ──► 1 位高速串行 ──► OBUFDS ──► pin_p/pin_n
                  ↑ clk_pix_5x (5×)                              (TMDS_33 差分电平)
                  ↑ clk_pix   (1×, 作为 CLKDIV)
```

DDR 在 5× 时钟的两个边沿都送数据，等效 \(2 \times 5 = 10\) 倍速率，正好在一个像素周期内把 10 位全部推出。这就是为什么 `clock_480p` 要额外输出一个 5× 时钟（见 4.2.3）。

**iCE40 并行输出链**（projf 例子）：

```text
4 位 R/G/B + hsync/vsync/de ──► SB_IO (寄存输出) ──► DVI Pmod 引脚 ──► 片外 TMDS 编码芯片 ──► HDMI
clk_pix ──────────────────────► SB_IO (DDR 输出) ──► DVI Pmod 时钟引脚
```

#### 4.2.3 源码精读

**XC7 的 5× 时钟从哪来。** `clock_480p` 用一片 `MMCME2_BASE` 同时生成 1× 像素时钟和 5× 串行时钟，注释直接点明用途：

[clock_480p.sv:15](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/clock/xc7/clock_480p.sv#L15) 注释 `5x clock for 10:1 DDR SerDes`。[clock_480p.sv:30-57](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/clock/xc7/clock_480p.sv#L30-L57) 用 `CLKOUT0_DIVIDE_F=5` 出 5×、`CLKOUT1_DIVIDE=25` 出 1×，各自经 `BUFG` 走全局时钟网络。

**10:1 串行化器 `oserdes_10b`。** 单个 `OSERDESE2` 最多 8 位，要传 10 位需「主从两个」级联：

[oserdes_10b.sv:21-57](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/display/xc7/oserdes_10b.sv#L21-L57) 是 MASTER，吃 `data_in[7:0]`；[oserdes_10b.sv:59-95](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/display/xc7/oserdes_10b.sv#L59-L95) 是 SLAVE，吃 `data_in[9:8]`（接在 D3/D4），两者通过 `SHIFTOUT→SHIFTIN` 串成 10 位移位链。`.DATA_WIDTH(10)`、`.DATA_RATE_OQ("DDR")` 是关键参数：DDR + 5× 时钟 = 10 倍速。

**单端转差分 `tmds_out`。** 一句原语搞定：

[tmds_out.sv:16-17](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/display/xc7/tmds_out.sv#L16-L17) 例化 `OBUFDS`，电平标准 `TMDS_33`，把单端 `tmds` 拆成正负差分对 `pin_p/pin_n`。

**iCE40 的 `SB_IO` 用法。** projf 的 iCE40 顶层直接例化 Lattice 原语 `SB_IO`，把并行视频整组送出：

[top_square.sv:67-76](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/fpga-graphics/ice40/top_square.sv#L67-L76) 用 `PIN_TYPE=6'b010100`（寄存输出）把 `{hsync,vsync,de,R,G,B}` 在 `clk_pix` 上升沿同步送出；[top_square.sv:79-86](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/fpga-graphics/ice40/top_square.sv#L79-L86) 用 `PIN_TYPE=6'b010000`（DDR 输出），`D_OUT_0=0`、`D_OUT_1=1`，让时钟在双沿翻转，生成与像素同频的时钟给 Pmod。

> 库里还有一个 [lib/null/ice40/SB_IO.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/null/ice40/SB_IO.sv)，它是**空壳**，文件头明确写「For Verilator linting - don't include in synthesis」——只为了让 Verilator 静态检查不报错，综合时用的是 Lattice 工具自带的真原语。这是 u5-l1 讲过的 null 分区纪律。

#### 4.2.4 代码实践

**实践目标**：对照两种平台的「最后一公里」，弄清各自在哪一段把信号变成 TMDS 差分。

**操作步骤**：

1. 打开 [tmds_out.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/display/xc7/tmds_out.sv)，确认 XC7 用 `OBUFDS` 把单端转差分；再打开 [oserdes_10b.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/display/xc7/oserdes_10b.sv)，找到 MASTER/SLAVE 的 `SHIFTOUT/SHIFTIN` 互联点。
2. 打开 [top_square.sv (ice40)](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/fpga-graphics/ice40/top_square.sv)，确认 iCE40 顶层**没有**例化 `tmds_encoder_dvi`、`oserdes_10b`、`tmds_out` 中的任何一个，而是直接用 `SB_IO` 送并行信号。

**需要观察的现象**：XC7 路径里能找到「编码→串行化→差分」三段原语；iCE40 路径里这三段都缺失，取而代之的是一个通往外置 Pmod 的并行总线。

**预期结果**：你会得出结论——在本仓库中，**XC7 在片内生成真正的 TMDS 串行差分对；iCE40 把 TMDS 编码外包给 DVI Pmod，片内仅做寄存/DDR 并行输出**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 10:1 串行化用「5× 时钟 + DDR」而不是「10× 时钟 + SDR」？

**参考答案**：DDR 在双沿采样，5× 时钟即可达 10 倍有效速率；10× 时钟频率太高（720p 时像素时钟 74.25 MHz，10× 就是 742.5 MHz），布线与功耗都更难。projf 的 `clock_720p` 正是输出 74.25 MHz 与 371.25 MHz（=5×）一对。

**练习 2**：单个 `OSERDESE2` 最多 8 位，为什么 `oserdes_10b` 要用主从两个？

**参考答案**：TMDS 码字是 10 位，单个 OSERDESE2 数据宽度上限为 8（DDR 模式下），需主从级联、用移位链拼出 10 位宽度。MASTER 接低 8 位、SLAVE 接高 2 位。

**练习 3**：`lib/null/ice40/SB_IO.sv` 能否被综合进比特流？

**参考答案**：不能。它是给 Verilator lint 用的空壳，文件头明确标注「don't include in synthesis」。iCE40 综合时由 Lattice 工具提供真正的 `SB_IO` 原语。

---

### 4.3 DVI/HDMI 完整信号链：dvi_generator 与顶层例化

#### 4.3.1 概念说明

一条 DVI/HDMI 链路由 **3 个数据差分对 + 1 个时钟差分对**组成。三个数据通道分别传蓝、绿、红（习惯上通道 0 = 蓝，并附带 `hsync/vsync`）。`dvi_generator` 就是把这件事打包：例化 3 个 `tmds_encoder_dvi` 编码三色，再例化 4 个 `oserdes_10b`——三个用于数据，第四个用固定模式 `10'b0000011111` 串行化成 TMDS 时钟。

注意第四个串行器输入 `10'b0000011111`：5 个 0 接 5 个 1，经 10:1 串行化后就是一个周期 = 像素周期的方波，即 TMDS 时钟对。接收端用它作主时钟去采样三条数据线。

> DVI 与 HDMI 的关系：HDMI 在电气上与 DVI 完全兼容（都是 TMDS），区别在于 HDMI 在消隐期的数据岛里还能塞音频和辅助数据。projf 的 `tmds_encoder_dvi` **不带音频**（README 明确写 "HDMI compatible, but no audio"），所以严格说是 DVI 编码器，但能直接驱动绝大多数 HDMI 显示器。

#### 4.3.2 核心流程

完整的 XC7 DVI 信号链（以 720p 为例）：

```text
clk_100m ──► clock_720p ──┬─► clk_pix (74.25 MHz)  ──► display_720p → sx,sy,hsync,vsync,de
                           └─► clk_pix_5x (371.25 MHz)
                           └─► clk_pix_locked → 复位门控

像素颜色 R/G/B (8 位)
   │  (+ hsync/vsync 在通道 0 的 ctrl)
   ▼
dvi_generator
   ├─ tmds_encoder_dvi ×3  → 3× 10 位并行码字
   ├─ oserdes_10b ×3       → 3 路串行 TMDS
   └─ oserdes_10b ×1       → 1 路串行 TMDS 时钟 (输入 10'b0000011111)
   │
   ▼
tmds_out (OBUFDS) ×4  →  hdmi_tx_ch0/1/2_p/n + hdmi_tx_clk_p/n  → HDMI 连接器
```

#### 4.3.3 源码精读

**`dvi_generator` 的组装。** [dvi_generator.sv:8-23](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/display/xc7/dvi_generator.sv#L8-L23) 声明 3 路数据 + 3 路控制输入、4 路串行输出。

[dvi_generator.sv:27-52](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/display/xc7/dvi_generator.sv#L27-L52) 例化 3 个 `tmds_encoder_dvi`（编码三色）；[dvi_generator.sv:62-92](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/display/xc7/dvi_generator.sv#L62-L92) 例化 4 个 `oserdes_10b`。关键是第 4 个：

```verilog
oserdes_10b serialize_chc (
    .clk(clk_pix), .clk_hs(clk_pix_5x),
    .data_in(10'b0000011111),         // 固定模式 → 串行化成 TMDS 时钟
    .serial_out(tmds_clk_serial)
);
```

[dvi_generator.sv:86-92](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/display/xc7/dvi_generator.sv#L86-L92) 即时钟通道——同一个串行器喂固定码字，省去专门的时钟生成逻辑。

另外 [dvi_generator.sv:54-60](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/display/xc7/dvi_generator.sv#L54-L60) 例化了 `async_reset`（来自 u5-l5 讲过的 essential 区），给 OSERDESE2 提供「异步断言、同步释放」的复位——这是高速串行器稳定工作的必要条件。

**顶层把整条链接起来。** 以 [top_hello.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/racing-the-beam/xc7-dvi/top_hello.sv) 为例，它在一块 Nexys Video 上点亮一行字。三段关键连接：

[top_hello.sv:25-31](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/racing-the-beam/xc7-dvi/top_hello.sv#L25-L31) 生成像素时钟 + 5× 时钟；注意复位取 `!clk_pix_locked`——必须等 PLL 锁定后才放行下游，这正是 u5-l2 强调的纪律。

[top_hello.sv:95-104](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/racing-the-beam/xc7-dvi/top_hello.sv#L95-L104) 把 4 位颜色「复制」成 8 位（`{2{display_r}}`），因为本设计的颜色只有 4 位深度，而 TMDS 通道是 8 位的。

[top_hello.sv:108-123](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/racing-the-beam/xc7-dvi/top_hello.sv#L108-L123) 例化 `dvi_generator`，注意通道映射约定：**ch0=蓝(含 sync)、ch1=绿、ch2=红**：

```verilog
.data_in_ch0(dvi_b),
.data_in_ch1(dvi_g),
.data_in_ch2(dvi_r),
.ctrl_in_ch0({dvi_vsync, dvi_hsync}),  // 同步信号挂在通道 0
```

最后 [top_hello.sv:126-133](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/racing-the-beam/xc7-dvi/top_hello.sv#L126-L133) 用 4 个 `tmds_out`（OBUFDS）把 4 路串行信号转成差分，连到顶层 `hdmi_tx_*_p/n` 引脚，最终落到 FPGA 的 HDMI 连接器引脚上（具体引脚由约束文件 `.xdc` 绑定，本讲不展开）。

#### 4.3.4 代码实践

**实践目标**：跟踪一条像素从「计算出来」到「变成差分电平」的完整旅程。

**操作步骤**：

1. 在 [top_hello.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/racing-the-beam/xc7-dvi/top_hello.sv) 里找到 `display_r/g/b`（4 位）→ `dvi_r/g/b`（8 位）→ `dvi_generator` → `tmds_*_serial` → `tmds_out` → `hdmi_tx_*_p/n` 这条数据流。
2. 在 [dvi_generator.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/display/xc7/dvi_generator.sv) 里数清：有几个 `tmds_encoder_dvi`？几个 `oserdes_10b`？分别承担什么？
3. 回答：为什么时钟通道不需要 `tmds_encoder_dvi`？

**需要观察的现象**：4 个 `oserdes_10b` 中有 3 个的输入来自编码器输出，第 4 个输入是常量 `10'b0000011111`。

**预期结果**：3 个编码器 + 4 个串行器（3 数据 + 1 时钟）。时钟通道不需要编码器，因为它不传信息，只需一个稳定方波，固定码字串行化即可。**待本地验证**：若有 Nexys Video 开发板，可综合 `top_hello` 并在显示器上看到那行字；否则用 Vivado 仿真 `xc7/display_720p_tb.sv` 观察时序。

#### 4.3.5 小练习与答案

**练习 1**：`dvi_generator` 输出的 `tmds_clk_serial` 频率是多少（720p）？

**参考答案**：等于像素时钟 74.25 MHz。固定码字 `0000011111` 经 10:1 串行化后周期 = 10 个串行比特 = 1 个像素周期，所以时钟频率就是 \(f_{\text{pix}}\)。

**练习 2**：`hsync/vsync` 为什么只挂在通道 0（蓝色通道）？

**参考答案**：DVI 标准规定同步信号在消隐期经由通道 0 的 `ctrl_in[1:0]` 传送（映射成 4 个控制字符之一）。绿、红通道在消隐期 `ctrl` 恒为 `2'b00`，对应 `top_hello.sv` 中 `ctrl_in_ch1/ch2 = 2'b00`。

**练习 3**：如果把 `clock_720p` 的 `clk_pix_5x` 断开，`dvi_generator` 会怎样？

**参考答案**：`oserdes_10b` 失去高速串行时钟，无法把 10 位并/串转换，TMDS 数据与时钟都无法产生，屏幕黑屏或无法锁定。

---

## 5. 综合实践

**任务：画一张完整的 XC7 DVI 发送机框图，并指出 iCE40 路径在哪里分叉。**

1. 通读 [tmds_encoder_dvi.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/display/tmds_encoder_dvi.sv)、[dvi_generator.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/display/xc7/dvi_generator.sv)、[oserdes_10b.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/display/xc7/oserdes_10b.sv)、[tmds_out.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/display/xc7/tmds_out.sv) 与 [top_hello.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/racing-the-beam/xc7-dvi/top_hello.sv)。
2. 画出从 `clk_100m` 到 `hdmi_tx_*_p/n` 的完整方框图，标注：MMCM 出的两组时钟、`display_720p` 产生的 `de/hsync/vsync`、3 个编码器、4 个串行器（标出第 4 个的固定输入）、4 个 OBUFDS。
3. 在同一张图上画出 iCE40 分支（参考 [top_square.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/fpga-graphics/ice40/top_square.sv)）：用箭头标出「iCE40 在此处把并行信号送出片外，后续 TMDS 编码由 DVI Pmod 完成」的分叉点。
4. 用一段话总结：两种平台各自把 TMDS 编码、串行化、差分输出三件事放在哪里。

**预期产出**：一张清晰的对照框图 + 一段差异总结。这是本讲所有最小模块（TMDS 编码、差分原语、完整信号链）的串联检验。

## 6. 本讲小结

- DVI/HDMI 用 TMDS 把每个 8 位像素编码成 10 位：多出的 2 位分别记录「最小化跳变的 XOR/XNOR 方式」与「是否取反做直流平衡」，换来低 EMI、可时钟恢复、可 AC 耦合。
- [tmds_encoder_dvi.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/lib/display/tmds_encoder_dvi.sv) 是平台中立的纯逻辑编码器，分两级（组合最小化 + 时序直流平衡），并用 `bias` 寄存器累计偏置；消隐期输出 4 种固定控制字符（各 7 次跳变）承载 `hsync/vsync`。
- XC7 在片内完成「编码 → 10:1 DDR 串行化（OSERDESE2 主从）→ 差分输出（OBUFDS）」全链路，需要 MMCM 提供 1× 与 5× 一对时钟。
- iCE40 在本仓库的例子里不在片内做 TMDS，而是用 `SB_IO` 把并行视频 + 时钟送给外置 DVI Pmod，由片外芯片完成 TMDS 编码与差分输出。
- `dvi_generator` 把 3 个数据通道 + 1 个时钟通道（固定码字 `10'b0000011111`）打包；`top_hello.sv` 给出可上板的完整例化范例，通道约定为 ch0=蓝(含 sync)、ch1=绿、ch2=红。
- 复位必须等 PLL `locked` 后才放行（`rst_pix = !clk_pix_locked`），OSERDESE2 还需经 `async_reset` 做「异步断言、同步释放」——高速串行器对时钟与复位极为敏感。

## 7. 下一步学习建议

- **往下走（应用层）**：阅读 [graphics/framebuffers](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/framebuffers/README.md) 与 [graphics/hardware-sprites](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/graphics/hardware-sprites/README.md)，看像素颜色是如何由帧缓冲/精灵产生并喂给本讲的 DVI 通路（对应 u6-l4）。
- **横向读**：对照 [demos/mandelbrot/xc7-dvi/top_mandel.sv](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/ThreePart/projf-explore/demos/mandelbrot/xc7-dvi/top_mandel.sv)，体会不同 demo 复用同一条 DVI 信号链的方式。
- **深挖原理**：若想了解 HDMI 的音频与数据岛（本讲的 DVI 编码器不含），可查阅 DVI 1.0 与 HDMI 1.4 规范中的 TMDS 编码章节，与本讲的 `tmds_encoder_dvi` 对照。
- **下一讲**：u6-l3 将转入「绘图原语」（画线/画形状），把本讲打通的显示通路用来真正「画」出内容。
