# 幅度平方与求和（re² + im² 的硬件实现）

## 1. 本讲目标

学完本讲你应该能够：

- 说清楚为什么 FFT 输出的复数 Re/Im 要被转成「幅度平方」re²+im²。
- 读懂 `Square` 与 `Sum` 两个模块如何把 10 位的复数分量计算成 21 位的幅度平方。
- 解释位宽为什么沿 10 → 20 → 21 一路膨胀。
- 在 TOP.v 里追踪出 `xk_re`/`xk_im` → 两个 `Square` → `Sum` → ram2 的完整组合逻辑通路。

## 2. 前置知识

本讲承接 u3-l1。FFT 核每次变换结束，会逐个频点（bin）吐出复数结果：实部 `xk_re[9:0]` 与虚部 `xk_im[9:0]`，它们都是有符号二进制补码。一个复数频点 \( X[k] = \mathrm{Re}[k] + j\cdot\mathrm{Im}[k] \) 同时携带「幅度」和「相位」两类信息。但示波器要显示的是每个频率分量的「强度」，相位通常不需要，所以我们要把复数变成一个非负的实数。

最常用的强度度量是幅度（magnitude）：

\[
|X[k]| = \sqrt{\mathrm{Re}[k]^2 + \mathrm{Im}[k]^2}
\]

注意里面有一个根号。本讲先算根号里面的部分——「幅度平方」（magnitude squared）：

\[
|X[k]|^2 = \mathrm{Re}[k]^2 + \mathrm{Im}[k]^2
\]

开根号的工作留给下一讲 u3-l3 的 `Root_square` 模块。本讲的 `Square` + `Sum` 只负责把两个 10 位分量算成 re²+im² 这个 21 位值，并存进 ram2。

> 名字提醒：本讲的 **`Square`（平方）≠ `Root_square`（开方）**。`Square` 是「平方」，把数变大；`Root_square` 是「开方」，把数压回去。两者名字相像，但在数据流里一前一后、方向相反。读 TOP.v 时千万不要把后面那组 `square_state1~5` 状态机当成「平方」用的——那是给开方模块配的时序（见 4.3 节）。

还需要两个硬件常识，它们是后面位宽推导的基础：

- 乘法器位宽翻倍：\( N \) 位 × \( N \) 位 → \( 2N \) 位输出。
- 加法器位宽 +1：\( N \) 位 + \( N \) 位 → \( N+1 \) 位输出（多一个进位位）。

## 3. 本讲源码地图

| 文件 | 模块 | 作用 |
| --- | --- | --- |
| `verilog files/Square.v` | `Square` | 封装 Xilinx 乘法器 IP `mult`，10×10→20 位，用来做平方 |
| `verilog files/Sum.v` | `Sum` | 封装 Xilinx 加法器 IP `adder`，20+20→21 位，用来求和 |
| `verilog files/TOP.v` | `TOP` | 例化 `sq_real` / `sq_im` / `adder`，把它们接在 FFT 输出与 ram2 之间 |
| `verilog files/SRAM.v` | `SRAM2` | 21 位双端口 RAM（ram2 / `ram_fft_20bit`），存幅度平方 |

> 命名陷阱（见 u1-l2 / u2-l2）：文件 `SRAM.v` 里装的模块叫 `SRAM2`（即 21 位的 ram2）；而文件 `ram2.v` 里装的模块反而叫 `SRAM`（即 10 位的 ram1）。文件名与模块名错位，读代码一律认 `module` 关键字后的名字。

## 4. 核心概念与源码讲解

### 4.1 Square：把实部 / 虚部平方（10×10 → 20 位）

#### 4.1.1 概念说明

「平方」就是把一个数自己乘自己：对实部 \( \mathrm{Re}^2 = \mathrm{Re}\times\mathrm{Re} \)，对虚部同理。这一步用乘法器实现。

需要特别理解的是：`Square` 模块本身并不是「只能做平方」的专用电路——它封装的是一个**通用乘法器** \( a\times b \)。TOP 只是把它的 `a` 和 `b` 都接成同一个信号（`xk_re` 或 `xk_im`），于是 \( a\times b \) 就退化成了平方 \( a^2 \)。这是一种常见的复用技巧：用一个通用乘法器，靠接线方式实现平方。

为什么用 Xilinx IP 而不是直接写 `a*a`？因为 Artix-7 内部的 DSP48 乘法单元由 `mult` IP 高效映射，比用通用逻辑综合出来的乘法器更快、更省资源。`Square` 就是对这个 IP 的一层「改名壳」，自身没有任何运算逻辑。

#### 4.1.2 核心流程

- 输入：`a[9:0]`、`b[9:0]`（两个 10 位操作数）。
- 操作：`p = a × b`（由 `mult` IP 完成）。
- 输出：`p[19:0]`（20 位乘积）。
- TOP 中例化两份：`sq_real` 把 `xk_re` 平方得到 `in1_s`；`sq_im` 把 `xk_im` 平方得到 `in2_s`。

位宽规律：\( N\times N \to 2N \)，所以 10 → 20。

#### 4.1.3 源码精读

[`Square` 模块端口与 `mult` 例化（Square.v:L8-L17）](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/Square.v#L8-L17) —— 把 10 位 `a`、`b` 接进 Xilinx `mult` IP，输出 20 位 `p`。

要点：

- 输入 `[9:0] a, b`、输出 `[19:0] p`，正好体现 10×10→20。
- `mult your_instance_name (.a(a), .b(b), .p(p))` 只是端口转接，`Square` 模块内一行运算逻辑都没有。

[两个 `Square` 例化（TOP.v:L172-L178）](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L172-L178) —— TOP 把 `xk_re` 和 `xk_im` 分别平方。注意 `a` 与 `b` 接的是同一个信号：

- `sq_real`：`.a(xk_re), .b(xk_re), .p(in1_s)` → \( \text{in1\_s} = \text{xk\_re}^2 \)
- `sq_im`：`.a(xk_im), .b(xk_im), .p(in2_s)` → \( \text{in2\_s} = \text{xk\_im}^2 \)

> 关于有符号（重要推断）：FFT 输出是有符号补码（见 u3-l1）。例如十进制 −1 在 10 位补码里是 `10'b1111111111`。如果 `mult` 配成「无符号」，它会把 −1 当成 1023，算出 \( 1023^2 \)（一个错误的大数）；只有配成「有符号」才会得到 \( (-1)^2 = 1 \)。所以为了保证 re² 正确，`mult` IP 必须是**有符号模式**。这个模式开关本身藏在二进制 IP 工程里（待本地确认），但从「结果必须正确」可以反推出它一定是有符号的。

#### 4.1.4 代码实践

实践目标：确认 `Square` 的「平方」是靠 `a`、`b` 接同一信号实现的，并体会 10→20 的位宽。

操作步骤：

1. 打开 `Square.v`，确认它是一个通用乘法器 \( a\times b \)，没有任何「平方」特化逻辑。
2. 打开 TOP.v 第 172–178 行，确认 `sq_real` 与 `sq_im` 的 `.a` 和 `.b` 都接同一个信号。
3. 在纸上把 `xk_re = 10'b0000000010`（十进制 +2）和 `xk_re = 10'b1111111111`（十进制 −1，补码）分别代入，**假设 `mult` 是有符号模式**，算出 `in1_s` 的值。

需要观察的现象 / 预期结果：

- +2 的平方 = 4，`in1_s = 20'b...00000000000000000100`。
- −1 的平方 = 1，`in1_s = 1`。
- 反例对照：如果 `mult` 被错配成无符号，−1 会被当成 1023，平方变成 1046529——据此体会「有符号模式」的必要性。
- 待本地验证：在 Vivado 里打开 `mult` IP 的配置对话框，确认其 signed/unsigned 选项（二进制工程包内，本讲义无法直接读取）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Square` 的输出是 20 位而不是 10 位？
**答案**：两个 10 位数相乘，乘积最多需要 20 位才能无溢出表示（\( N\times N \to 2N \)）。

**练习 2**：如果只想要 re²，硬件上能不能只例化一个 `Square`？
**答案**：单个 `Square` 实例就足以算一个平方（它本质就是乘法器）。但本设计需要**同时**算 re² 和 im²，所以要两个实例并行计算，结果再交给 `Sum` 相加。

---

### 4.2 Sum：把两个平方加起来（20+20 → 21 位）

#### 4.2.1 概念说明

两个平方算出来后（`in1_s = re²`、`in2_s = im²`），要把它们相加得到 re²+im²，这就是幅度平方。这一步用加法器。`Sum` 模块封装 Xilinx `adder` IP，做 \( a+b \)。

为什么输出位宽是 21？两个 20 位数相加最多会产生一个最高位进位，所以结果需要 21 位（\( N+N \to N+1 \)）。这 21 位正好对应 ram2 的数据宽度，也是 u2-l2 里 ram2 选 21 位的根本原因。

#### 4.2.2 核心流程

- 输入：`a[19:0]`（= `in1_s` = re²）、`b[19:0]`（= `in2_s` = im²）。
- 操作：`s = a + b`（由 `adder` IP 完成）。
- 输出：`s[20:0]`（21 位和），接到 ram2 的 `data_in`。

位宽规律：\( N+N \to N+1 \)，所以 20 → 21。

#### 4.2.3 源码精读

[`Sum` 模块端口与 `adder` 例化（Sum.v:L8-L17）](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/Sum.v#L8-L17) —— 20 位 `a`、`b` 进 Xilinx `adder` IP，输出 21 位 `s`。

[`Sum` 例化（TOP.v:L180-L182）](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L180-L182)，配合 [`in1_s`/`in2_s`/`sum` 连线声明（TOP.v:L66-L68）](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L66-L68) —— `adder` 把两个平方加成 `sum`。

[`out_fft` 连线声明（TOP.v:L64）](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L64) 的注释直接写明 `out_fft = xk_re^2 + xk_im^2`，是作者对这条链路数学含义的官方说明（`out_fft` 是 ram2 的读端口，与写进 ram2 的 `sum` 是同一个 21 位内容）。

#### 4.2.4 代码实践

实践目标：把 `in1_s`、`in2_s`、`sum` 三个连线的位宽与来源对上。

操作步骤：

1. 在 TOP.v L66-L68 找到三条连线的声明，记下位宽。
2. 在 L180-L182 确认 `adder` 的 `.a`/`.b`/`.s` 分别接 `in1_s`/`in2_s`/`sum`。
3. 画出 `in1_s(20) + in2_s(20) = sum(21)` 的位宽关系。

预期结果：`sum` 是 21 位，比两个 20 位输入多 1 位进位。

#### 4.2.5 小练习与答案

**练习 1**：为什么加法器结果是 21 位而不是 20 位？
**答案**：两个 20 位数相加可能产生最高位进位，需要多 1 位（\( N+N \to N+1 \)）。

**练习 2**：re² 和 im² 都是非负数，这里的加法需要担心溢出吗？
**答案**：从实际取值看，FFT 输出被 `scale_sch` 缩放到 10 位有符号（量级 ±512），\( \text{re}^2+\text{im}^2 \) 最大约 \( 2\times 512^2 = 524288 \)（约 20 位），21 位绰绰有余；21 位是 IP 的标准加法宽度，留了余量。

---

### 4.3 端到端：组合逻辑链与 ram2 写入

#### 4.3.1 概念说明

把 4.1、4.2 串起来：FFT 每吐出一个频点的 `xk_re`/`xk_im`，组合逻辑就立刻算出对应的 re²+im²，并写进 ram2。这条链的最关键特征是——**它是纯组合逻辑，没有时钟、没有专属状态机**。它像一根「水管」：FFT 在一头放水（复数），ram2 在另一头接水（幅度平方），中间的 `Square`+`Sum` 自动完成转换。

> 再次提醒：TOP 里那组 `square_state` / `square_state2~5` 状态机是给 `Root_square`（开方）配的流水线节拍，**不是**给本讲的「平方求和」用的。本讲的链路发生在 `fft_write_state` 期间，靠 `we2` 打开 ram2 写使能即可，不需要额外状态。

#### 4.3.2 核心流程

数据通路（同一周期内组合完成）：

```
FFT 输出
  xk_re[9:0] ──► Square(sq_real) ──► in1_s[19:0] (=re²) ┐
                                                          ├─► Sum(adder) ──► sum[20:0] ──► ram2.data_in
  xk_im[9:0] ──► Square(sq_im)   ──► in2_s[19:0] (=im²) ┘
```

写时序（在 `fft_state` / `fft_write_state`，承接 u3-l1）：

1. `fft_state` 拉高 `we2`（ram2 写使能）并启动 FFT。
2. FFT 逐频点卸载输出，`index_out` 既是 FFT 的输出频点号，也直接当 ram2 的写地址。
3. 每个频点的 `sum` 被 ram2 在 `clk` 上升沿锁存到 `mem[index_out]`。
4. `index_out` 走到 1022 时，`fft_write_state` 关掉 `we2`，结束写入，随后进入 `square_state`（开方阶段）。

#### 4.3.3 源码精读

[ram2（`SRAM2`）例化（TOP.v:L137-L142）](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L137-L142) —— 21 位 RAM，`.addr(index_out)`（写地址来自 FFT 输出计数器）、`.data_in(sum)`（数据来自平方求和链）、`.we(we2)`。

[`fft_state` / `fft_write_state`（TOP.v:L339-L354）](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L339-L354) —— `fft_state` 打开 `we2` 启动写入；`fft_write_state` 在 `index_out` 到 1022 时关闭 `we2`，转入开方阶段。

> 重要推断（零延迟组合）：ram2 的写地址直接用 `index_out`、写数据直接用 `sum`，二者之间没有任何延迟对齐逻辑。这只有在 `mult` 与 `adder` 都配置成**零延迟纯组合**时才成立——否则 `sum` 会比 `index_out` 慢几拍，导致写错地址。IP 的延迟级数在二进制工程里（待本地确认），但从「TOP 里没有任何延迟补偿」这一点，可以反推出这两个 IP 是零延迟配置的。这是从硬件连接读出设计意图的好例子。

> 时钟域小记（待深入）：FFT 跑在 `clk_100`（100 MHz），ram2 跑在 `clk`（200 MHz），`Square`+`Sum` 这条组合链横跨两个时钟域。详细的跨域分析见专家层讲义；本讲只需知道这条链是组合的、能在两域间透传。

#### 4.3.4 代码实践（本讲主实践）

实践目标：追踪 `xk_re`/`xk_im` → ram2 的完整通路，并解释 10→21 的位宽膨胀。

操作步骤：

1. 在 TOP.v 中找到 `xk_re`、`xk_im` 的声明（L60-L61，各 10 位）。
2. 顺着它们进入 `sq_real`、`sq_im`（L172-L178），写出 `in1_s`、`in2_s` 的位宽（各 20 位）与含义。
3. 再进入 `adder`（L180-L182），写出 `sum` 的位宽（21 位）与含义。
4. 确认 `sum` 接到 ram2 的 `data_in`（L140），写地址用 `index_out`（L138）。
5. 填写下面这张位宽表。

| 阶段 | 信号 | 位宽 | 含义 |
| --- | --- | --- | --- |
| FFT 输出 | `xk_re` / `xk_im` | 10 | 复数实部 / 虚部（有符号补码） |
| 平方后 | `in1_s` / `in2_s` | 20 | re² / im² |
| 求和后 | `sum` | 21 | re² + im²（幅度平方） |
| ram2 存储 | `mem[index_out]` | 21 | 幅度平方谱 |

需要观察的现象 / 预期结果：位宽沿 10 → 20 → 21 膨胀。原因是乘法翻倍（10→20）、加法进位（20→21）。这也是 ram2 数据宽度选 21 位的根本原因。

#### 4.3.5 小练习与答案

**练习 1**：如果省掉 `Sum`（直接把 `in1_s` 存进 ram2），会丢失什么信息？
**答案**：会丢掉虚部分量 im²，只剩 re²，不再是真正的幅度（幅度需要实部与虚部共同决定）。即便本工程 FFT 的虚部输入恒为 0，变换后 `xk_im` 输出仍可能非零，所以仍需要 im²。

**练习 2**：为什么这条平方求和链不需要状态机？
**答案**：平方与求和都是组合运算，FFT 一给出 `xk_re`/`xk_im`，结果立刻可用。只要在 `fft_write_state` 用 `we2` 打开 ram2 写使能，让组合结果流进去即可，无需逐拍控制。

## 5. 综合实践

把一个具体频点「走」一遍。假设某个频点 FFT 输出 `xk_re = +20`、`xk_im = -15`（均为 10 位有符号补码）：

1. 用 `Square` 算 re² = 400、im² = 225。
2. 用 `Sum` 算 re²+im² = 625。
3. 625 只需 10 位二进制（`20'b...0100111001`）即可表示，远小于 21 位上限——说明 21 位宽度留有很大余量，这个余量来自乘法/加法 IP 的标准位宽约定，而非实际数值需要。
4. 在 TOP.v 里标出这个 625 会写到 ram2 的哪个地址（答案：地址 = 该频点的 `index_out` 值），并预言它后续会被 `Root_square` 开方成 25（\( \sqrt{625}=25 \)），再进 ram3。

通过这个例子，把「复数 → 平方 → 求和 → 存 ram2 → 开方 → 存 ram3」整条线串起来，体会 `Square`+`Sum` 这一段在链路中的位置：它把 FFT 的复数输出转成实数幅度平方，为下一步开方做准备。

## 6. 本讲小结

- FFT 输出复数 \( X[k]=\mathrm{Re}+j\cdot\mathrm{Im} \)；要得到「强度」需算幅度平方 re²+im²，开根号留给下一讲 u3-l3。
- `Square` 封装 Xilinx `mult` 乘法器 IP，10×10→20 位；TOP 用两个实例分别平方实部与虚部（`a`、`b` 接同一信号实现平方）。
- `Sum` 封装 Xilinx `adder` 加法器 IP，20+20→21 位；把两个平方加成 `sum`。
- 位宽膨胀规律：乘法翻倍（10→20）、加法进位（20→21），这正是 ram2 选 21 位的原因。
- 整条链是纯组合逻辑，发生在 `fft_write_state`（`we2` 打开期间）；`index_out` 既当频点号又当 ram2 写地址。
- `Square`（平方，本讲）≠ `Root_square`（开方，下一讲 u3-l3），名字相像但方向相反。

## 7. 下一步学习建议

下一讲 **u3-l3** 讲 `Root_square`（开方）：它把 ram2 里的 21 位幅度平方开根号，得到 10 位幅度 \( |X| \) 存入 ram3，并带有 8 拍流水线延迟与 `sqr_rdy` 握手。建议先把本讲的位宽曲线（10→20→21）记牢，再去理解开方如何把位宽「压回」10 位。同时可以开始通读 TOP.v 的 `square_state` / `square_state2~5`，看主状态机如何配合开方的流水线节拍。
