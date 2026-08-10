# 蝶形单元的流水线：四级时序与乘法器复用

## 1. 本讲目标

本讲是「核心计算单元」单元的第三讲。上一讲（u2-l2）已经讲清了蝶形「算什么、端口长什么样、`x_nd/y_nd` 怎么握手」，但**刻意把逐拍时序留到了现在**。本讲就正面拆开 `butterfly.v` 里那一个 `always` 块，回答三个被上一讲悬置的问题：

1. STAGE 1~4 到底在每一个时钟拍做什么？那条 `x_nd_old[0..2]` 延迟链如何充当各级的「使能开关」？
2. 同一对乘积寄存器 `zbw_m1/zbw_m2` 是怎么被实部、虚部两组乘积「分时复用」的？为什么因此 **`x_nd` 不能连续两拍为 1**？
3. `zbw_im` 明明是 `W·XB` 的虚部，为什么还要再延迟一拍存成 `zbw_im_old`？`>>> (X_WDTH-2)` 和 `>>> 1` 两次右移各管什么？

学完本讲你应该能够：

- 画出单个蝶形从输入到输出 \(YA\)、\(YB\) 的四级流水线时间线，并说出每一拍哪个 `STAGE` 在工作。
- 解释「两个硬件乘法器如何在两拍内算出复数乘法需要的四个实数乘积」，从而理解资源节省与吞吐代价。
- 说清 `zbw_im`（wire）与 `zbw_im_old`（reg）的区别，以及定点乘法后两次右移的各自含义。

> 本讲只盯 `butterfly.v` 一个文件，全程围绕它那一个 `always @ (posedge clk)` 块展开。

## 2. 前置知识

本讲要用到的基础概念（多数已在 u2-l1、u2-l2 建立）：

- **蝶形定义**：\(YA = XA + W\cdot XB\)、\(YB = XA - W\cdot XB\)，复数乘法拆成实部「交叉相乘再相减」、虚部「交叉相乘再相加」。具体地，若 \(W=w_{re}+jw_{im}\)、\(XB=xb_{re}+jxb_{im}\)，则需要四个实数乘积：
  \[
  p_{re1}=xb_{re}\,w_{re},\quad p_{re2}=xb_{im}\,w_{im},\quad p_{im1}=xb_{re}\,w_{im},\quad p_{im2}=xb_{im}\,w_{re}
  \]
  其中 \(\text{Re}(W\cdot XB)=p_{re1}-p_{re2}\)、\(\text{Im}(W\cdot XB)=p_{im1}+p_{im2}\)。
- **定点尺度（来自 u2-l1）**：数据走 Q1.(width-1) 格式（\(1.0 \approx 2^{X\_WDTH-1}\)），旋转因子走 Q2.(width-2) 格式（\(1.0 = 2^{X\_WDTH-2}\)），两者位宽必须相等（`TF_WDTH == X_WDTH`）。
- **Verilog 非阻塞赋值 `<=`**：在一个时钟沿里，所有 `<=` 右边先用「沿之前」的旧值算出来，左边的新值要到**下一个时钟沿之前**才生效。这是本讲画时序图的根基——「本拍读到的」永远是上一拍写下的。
- **`>>>` 算术右移**：对 `signed`（有符号）寄存器，`>>>` 是「补符号位」的右移，正数补 0、负数补 1，相当于除以 2 的幂且保留符号。本模块所有相关寄存器都声明为 `signed`。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `butterfly.v` | 实现单步蝶形运算的 Verilog 模块 | 那一个 `always` 块里的 STAGE 1~4、`x_nd_old` 延迟链、`zbw_m1/zbw_m2` 乘积寄存器的复用、`zbw_im/zbw_im_old` 的区别、两次右移定标 |

本讲不再涉及端口定义与数学推导（那是 u2-l2 的内容），默认你已经能熟练拆 `w/xa/xb` 的实虚部、手算 \(W\cdot XB\)。

## 4. 核心概念与源码讲解

本讲把 `butterfly` 的流水线拆成四块：**四级流水线总览与使能链**、**逐级精读 STAGE 1~4**、**乘法器复用与「不能连续两拍」**、**定点右移定标**。

### 4.1 四级流水线总览：x_nd_old 延迟链如何充当各级使能

#### 4.1.1 概念说明

一个蝶形 \(XA \pm W\cdot XB\) 在数学上可以「一拍算完」，但 `butterfly` 模块在硬件上把它摊成了一条**四级流水线**：输入进来后，要连过四个时钟拍，\(YA\)、\(YB\) 才依次从 `y` 端冒出来。这样设计有两个动机：

1. **复用乘法器**：复数乘法需要 4 个实数乘积，但作者不想用 4 个乘法器一拍算完，而是用 2 个乘法器分两拍算（见 4.3）。文件头注释直接写明了这一点。

   详见 [butterfly.v:L11-L14](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L11-L14)（注释：输入频率不得超过每两拍一次，「希望借此少用一些乘法块」）。

2. **把数据流水化，方便上级 `dit` 连续喂数据**：只要每隔一拍喂一个新蝶形，多个蝶形就能在流水线里交叠前进，整体吞吐不浪费。

那么「现在该执行第几级」由谁决定？答案是一条 `x_nd` 的延迟链 `x_nd_old[0..2]`。`x_nd` 是「本拍有新数据」的脉冲；把它延迟 1、2、3 拍，就分别得到「上一拍有新数据」「上上拍有新数据」「上上上拍有新数据」三个回声。**这四个信号（`x_nd` 本身 + 三个回声）正好一一对应 STAGE 1~4 的使能**：哪个信号本拍为 1，对应的那一级就在本拍末的时钟沿干活。

#### 4.1.2 核心流程

把一个有效输入（`x_nd=1`）记作「蝶形 A 的第 1 拍」，则它顺次经过：

```text
第1拍  x_nd        =1  → 触发 STAGE 1 ：锁存 W/XA/XB，启动实部两个乘积
第2拍  x_nd_old[0] =1  → 触发 STAGE 2 ：启动虚部两个乘积，算出 Re(W·XB)
第3拍  x_nd_old[1] =1  → 触发 STAGE 3 ：用 XA + W·XB 合成，输出 YA
第4拍  x_nd_old[2] =1  → 触发 STAGE 4 ：用 XA − W·XB 合成，输出 YB
```

关键点：`x_nd_old` 只是把 `x_nd` 一拍一拍往后挪，所以一个 `x_nd` 脉冲会自动在接下来的三拍里「点亮」STAGE 2、3、4，无需任何额外计数器。这就是「用延迟链当分布式使能」的写法。

> 约定：下文所有时序表里，**「第 C 拍」指第 C 个时钟周期内寄存器/信号的稳定取值**（仿真波形里第 C 周期那一格）；该拍末尾的上升沿按使能写入新值，**新值要到第 C+1 拍才出现**。这与 `<=` 非阻塞赋值的语义一致。

#### 4.1.3 源码精读

延迟链的声明与移位分别见 [butterfly.v:L77](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L77)（声明 `x_nd_old[2:0]` 三个寄存器）与 [butterfly.v:L108-L110](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L108-L110)（每拍无条件移位，把 `x_nd` 一路往后传）：

```verilog
x_nd_old[0] <= x_nd;
x_nd_old[1] <= x_nd_old[0];
x_nd_old[2] <= x_nd_old[1];
```

注意这三行**不在任何 `if` 里**——它们每拍都执行，是「背景计数器」。而各级 `STAGE` 则各自被 `x_nd` / `x_nd_old[0]` / `x_nd_old[1]` / `x_nd_old[2]` 之一门控（见 [butterfly.v:L115](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L115)、[butterfly.v:L130](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L130)、[butterfly.v:L144](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L144)、[butterfly.v:L160](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L160)）。于是「背景移位 + 四个门控 stage」就拼出了整条流水线。

复位逻辑见 [butterfly.v:L101-L104](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L101-L104)：`rst_n` 低有效异步复位，复位时只把 `y_nd` 清 0（让输出端处于「当前不是 \(YA\)」的安全态），其余寄存器不显式清零——它们会在第一个有效输入后被覆盖。

#### 4.1.4 代码实践

**数清「一个 `x_nd` 脉冲点亮几级 stage」。**

1. 实践目标：直观确认延迟链如何充当四级使能。
2. 操作步骤：打开 [butterfly.v:L99-L168](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L99-L168)，找到四个 `if (...)` 门控，分别记下它们的条件（`x_nd`、`x_nd_old[0]`、`x_nd_old[1]`、`x_nd_old[2]`）。
3. 需要观察的现象：假设 `x_nd` 在第 1 拍为 1、其余拍为 0，那么 `x_nd_old[0]` 在第 2 拍为 1、`x_nd_old[1]` 在第 3 拍为 1、`x_nd_old[2]` 在第 4 拍为 1。
4. 预期结果：四级的触发时序正好是第 1→2→3→4 拍，与你数出的「脉冲被延迟 0/1/2/3 拍」一一对应。
5. 待本地验证：若装了 iverilog，可写一个只给一次 `x_nd` 脉冲的最小 testbench，用 `$monitor` 打印 `x_nd_old` 逐拍值，确认上述回声。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `x_nd_old[0..2]` 的移位语句误删，STAGE 2~4 还能正常工作吗？
**答**：不能。STAGE 2~4 分别依赖 `x_nd_old[0..2]` 为 1 才触发；没有移位，这些回声恒为 0，第 2 拍之后什么都不会发生，\(YA/YB\) 永远算不出来。

**练习 2**：为什么 `x_nd_old` 的移位语句要放在 `if` 外面、每拍都执行？
**答**：因为它要忠实地把 `x_nd` 一路延迟，无论本拍有没有新数据。把它放在背景里无条件移位，才能保证一个 `x_nd` 脉冲在随后三拍准时「点亮」STAGE 2~4。

---

### 4.2 逐级精读 STAGE 1~4（含 zbw_im_old 的作用）

#### 4.2.1 概念说明

有了使能链，现在逐级看每一级到底写了哪些寄存器、为什么这么写。把 4.1.2 的四拍时间线和上一讲的手算公式对上：

- **STAGE 1（第 1 拍）**：锁存本拍输入的 \(W/XA/XB\)（存进 `ww_*`、`za_*[0]`、`zb_*`），并**启动实部需要的两个乘积** `xb_re*w_re`、`xb_im*w_im` 到 `zbw_m1/zbw_m2`。
- **STAGE 2（第 2 拍）**：用上一拍存好的 `zb_*`、`ww_*` **启动虚部需要的两个乘积** `zb_re*ww_im`、`zb_im*ww_re`（再次写入同一对 `zbw_m1/zbw_m2`！），同时把上一拍那对实部乘积右移相减，得到 \(\text{Re}(W\cdot XB)\) 存进 `zbw_re`。
- **STAGE 3（第 3 拍）**：用 \(XA + W\cdot XB\) 合成 \(YA\) 并输出（`y_nd<=1`），同时把 \(XA\) 再延迟一拍存进 `za_*[1]`（留给 \(YB\)），并把当前 wire 上的虚部 `zbw_im` 存进 `zbw_im_old`。
- **STAGE 4（第 4 拍）**：用 \(XA - W\cdot XB\) 合成 \(YB\) 并输出（`y_nd<=0`）。

这里有一个上一讲埋下的伏笔：`zbw_im` 是 **wire**（组合逻辑，直接由 `zbw_m1/zbw_m2` 算出），而 `zbw_re` 是 **reg**。这导致它们「有效期」不同，正是 `zbw_im_old` 存在的原因——4.2.3 会专门点破。

#### 4.2.2 核心流程

为便于跟踪，给四个乘积起符号名（以上标 A 标记这是蝶形 A 的乘积）：

\[
p_{re1}^A=xb_{re}\,w_{re},\;\; p_{re2}^A=xb_{im}\,w_{im},\;\; p_{im1}^A=xb_{re}\,w_{im},\;\; p_{im2}^A=xb_{im}\,w_{re}
\]

一个**孤立**蝶形 A（只有第 1 拍 `x_nd=1`）的关键寄存器逐拍取值如下（✗ 表示该 wire 此刻是无意义的「乘积混搭」，× 表示无关项，— 表示尚未被赋值）：

| 第 C 拍 | x_nd | x_nd_old[0:2] | 末沿触发 | za_re[0] | zb_re | ww_re | zbw_m1 | zbw_m2 | zbw_re | zbw_im(wire) | za_re[1] | zbw_im_old | y_nd | y | m_out |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 1 | 000 | STAGE1 | — | XBA_re | WA_re | p_re1_A | p_re2_A | — | ✗ | — | — | 0 | × | 0 |
| 2 | 0 | 100 | STAGE2 | XAA_re | XBA_re | WA_re | p_re1_A | p_re2_A | — | ✗ | — | — | 0 | × | 0 |
| 3 | 0 | 010 | STAGE3 | XAA_re | XBA_re | WA_re | p_im1_A | p_im2_A | Re(WA·XBA) | **Im(WA·XBA)** | — | — | 0 | × | 0 |
| 4 | 0 | 001 | STAGE4 | XAA_re | XBA_re | WA_re | p_im1_A | p_im2_A | Re(WA·XBA) | ✗ | XAA_re | **Im(WA·XBA)** | 1 | YA_A/2 | MA |
| 5 | 0 | 000 | — | XAA_re | XBA_re | WA_re | p_im1_A | p_im2_A | Re(WA·XBA) | ✗ | XAA_re | Im(WA·XBA) | 0 | YB_A/2 | × |

读这张表的两条要点：

- `zbw_m1/zbw_m2` 在第 2 拍还是 `p_re1_A/p_re2_A`（实部乘积），到第 3 拍才被 STAGE 2 改写成 `p_im1_A/p_im2_A`（虚部乘积）——**同一对寄存器先后装了两组不同的乘积**，这就是 4.3 要讲的复用。
- `zbw_im`（wire）**只在第 3 拍有意义**（因为只有第 3 拍 `zbw_m1/zbw_m2` 装的才是虚部乘积）。可 \(YB\) 要到第 4 拍（STAGE 4）才合成，那时 `zbw_im` 已经失效。所以 STAGE 3 必须把第 3 拍有效的虚部存进 `zbw_im_old`，供第 4 拍使用——这就是 `zbw_im_old` 唯一的存在意义。

#### 4.2.3 源码精读

**STAGE 1**（[butterfly.v:L114-L129](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L114-L129)）：锁存输入并启动实部两个乘积。注释直说「为计算 \(W\cdot XB\) 的实部做两次乘法」：

```verilog
za_re[0] <= xa_re;  za_im[0] <= xa_im;   // 锁存 XA
ww_re   <= w_re;    ww_im   <= w_im;      // 锁存 W
zb_re   <= xb_re;   zb_im   <= xb_im;     // 锁存 XB
zbw_m1  <= xb_re*w_re;                   // p_re1（实部用）
zbw_m2  <= xb_im*w_im;                   // p_re2（实部用）
```

**STAGE 2**（[butterfly.v:L130-L142](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L130-L142)）：把同一对 `zbw_m1/zbw_m2` 改写为虚部乘积，同时用「上一拍的实部乘积」算出 `zbw_re`。注意读到的 `zbw_m1/zbw_m2` 是**旧值**（实部乘积），而写进的是**新值**（虚部乘积），互不干扰：

```verilog
zbw_m1 <= zb_re*ww_im;   // p_im1（虚部用，复用同一寄存器）
zbw_m2 <= zb_im*ww_re;   // p_im2（虚部用，复用同一寄存器）
zbw_re <= (zbw_m1 >>> (X_WDTH-2)) - (zbw_m2 >>> (X_WDTH-2));  // Re(W·XB)
```

注释 [butterfly.v:L138-L140](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L138-L140) 还特别说明：因为 \(|W|\le1\)、\(|XB|\le1\)，所以 \(|W\cdot XB|\le1\)，相减不会溢出，`zbw_re` 用 `X_WDTH` 位就够装。

**STAGE 3**（[butterfly.v:L143-L158](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L143-L158)）：合成 \(YA\)，并把虚部从 wire「固化」进 `zbw_im_old`。`z1_re_big = za_re[0] + zbw_re`、`z1_im_big = za_im[0] + zbw_im` 是加宽 1 位的组合 wire（见 [butterfly.v:L90-L93](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L90-L93)），注释 [butterfly.v:L149-L150](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L149-L150) 也提示 \(XA\) 在「下一拍」还要被 \(YB\) 用到，所以这里顺手存进 `za_*[1]`：

```verilog
za_re[1] <= za_re[0];   za_im[1] <= za_im[0];   // XA 再延迟一拍，留给 YB
y_nd     <= 1'b1;                                // 当前 y 是 YA
y_re     <= z1_re_big >>> 1;                     // (XA_re + Re(W·XB)) / 2
y_im     <= z1_im_big >>> 1;
zbw_im_old <= zbw_im;                            // 把「只在第3拍有效」的虚部存下来
```

**STAGE 4**（[butterfly.v:L159-L166](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L159-L166)）：合成 \(YB\)。注意实部用 `zbw_re`（reg，第 3 拍后一直保持），虚部用 `zbw_im_old`（而非 `zbw_im`，因为 wire 此刻已失效）：

```verilog
y_nd <= 1'b0;                       // 当前 y 是 YB
y_re <= z2_re_big >>> 1;            // z2_re_big = za_re[1] - zbw_re
y_im <= z2_im_big >>> 1;            // z2_im_big = za_im[1] - zbw_im_old
```

`z2_*_big` 的定义在 [butterfly.v:L94-L97](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L94-L97)，实部用 `zbw_re`、虚部用 `zbw_im_old`——这一处「实部用 reg、虚部用 old」的不对称，正是 4.2.2 表格里 `zbw_im` 与 `zbw_im_old` 有效期不同的直接体现。

#### 4.2.4 代码实践

**在时序表里填出 \(YA\)、\(YB\) 的产出拍。**

1. 实践目标：把 4.2.2 的表格和 `y_nd` 的时序对上，确认输出节拍。
2. 操作步骤：对照 [butterfly.v:L153-L165](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L153-L165)，STAGE 3 在第 3 拍末把 `y_nd<=1`、`y<=YA/2`，STAGE 4 在第 4 拍末把 `y_nd<=0`、`y<=YB/2`。
3. 需要观察的现象：因为 `<=` 的结果要到下一拍才出现，所以 `y_nd=1`（即 \(YA\)）**在第 4 拍可见**，`y_nd=0`（即 \(YB\)）**在第 5 拍可见**。
4. 预期结果：与 4.2.2 表格第 4、5 行的 `y_nd`/`y` 列完全一致。
5. 待本地验证：无（纯源码阅读 + 推理）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `zbw_re` 是 reg，而 `zbw_im` 是 wire？
**答**：`zbw_re` 在 STAGE 2 里用 `<=` 显式寄存（相减的结果），所以是 reg，能跨拍保持。`zbw_im` 是用 `assign` 直接由 `zbw_m1/zbw_m2` 组合算出的 wire，它只在 `zbw_m1/zbw_m2` 装着虚部乘积的那一拍（第 3 拍）才反映真正的虚部，无法跨拍保持。

**练习 2**：如果删掉 `zbw_im_old <= zbw_im;` 并把 STAGE 4 的虚部改用 `zbw_im`，会发生什么？
**答**：第 4 拍时 `zbw_m1/zbw_m2` 装的是上一拍（第 3 拍末由 STAGE 2 写入）的虚部乘积——等等，仔细看：第 4 拍 `zbw_m1/zbw_m2` 仍是 `p_im1_A/p_im2_A`（孤立蝶形下第 4 拍没有新输入、STAGE 2 不触发），所以 `zbw_im` 在第 4 拍**恰好**还等于 Im(A)。但在**交叠**情况（见 4.3）下，第 4 拍 `zbw_m1/zbw_m2` 已被下一个蝶形的乘积覆盖，`zbw_im` 会变成无意义的混搭，\(YB\) 虚部就算错了。所以 `zbw_im_old` 是为「交叠流水」买的保险。

---

### 4.3 乘法器复用：两个乘法器如何算出四个乘积

#### 4.3.1 概念说明

这是本模块最巧妙、也最容易让人卡住的设计点。复数乘法 \(W\cdot XB\) 一共需要 **4 个实数乘积**（\(p_{re1},p_{re2},p_{im1},p_{im2}\)）。最暴力的做法是用 4 个硬件乘法器一拍全算出来。但 FPGA 上乘法器（DSP 单元）是稀缺资源，作者选择**只用 2 个乘法器，分两拍算完**：

- 第 1 拍（STAGE 1）：2 个乘法器同时算 \(p_{re1}=xb_{re}\,w_{re}\) 和 \(p_{re2}=xb_{im}\,w_{im}\)，结果存进 `zbw_m1/zbw_m2`。
- 第 2 拍（STAGE 2）：**同一个** 2 个乘法器再算 \(p_{im1}=zb_{re}\,ww_{im}\) 和 \(p_{im2}=zb_{im}\,ww_{re}\)，结果**覆盖**进同一对 `zbw_m1/zbw_m2`。

也就是说，`zbw_m1` 在两拍里先后装了 \(p_{re1}\)、\(p_{im1}\)；`zbw_m2` 先后装了 \(p_{re2}\)、\(p_{im2}\)。**2 个乘法器 × 2 拍 = 4 个乘积**，省下一半乘法器。

代价是：一个蝶形至少要占 2 拍才能完成全部乘法，所以**新输入不能在下一拍就来**——否则 STAGE 1（要写新蝶形的 \(p_{re1},p_{re2}\)）和 STAGE 2（要写老蝶形的 \(p_{im1},p_{im2}\)）会**在同一拍争抢同一对 `zbw_m1/zbw_m2`**，后写的覆盖先写的，结果错乱。这正是端口注释里「输入不能连续两拍到达」的根本原因，也是那条 `$display("ERROR ...")` 想拦截的违约。

#### 4.3.2 核心流程

把两个蝶形 A、B **交叠**着喂进来（`x_nd` 序列 `1,0,1,0,0`，即每两拍一个新输入），看 `zbw_m1/zbw_m2` 如何被两路计算分时复用：

| 第 C 拍 | x_nd | 本拍末沿触发的 stage | zbw_m1 | zbw_m2 | 谁的乘积 | y_nd（下一拍可见） |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 1 | A:STAGE1 | p_re1_A | p_re2_A | A 的实部对 | — |
| 2 | 0 | A:STAGE2 | p_im1_A | p_im2_A | A 的虚部对 | — |
| 3 | 1 | B:STAGE1 ＋ A:STAGE3 | p_re1_B | p_re2_B | B 的实部对 | （A 的 \(YA\) 在第 4 拍出） |
| 4 | 0 | B:STAGE2 ＋ A:STAGE4 | p_im1_B | p_im2_B | B 的虚部对 | （A 的 \(YB\) 在第 5 拍出） |
| 5 | 0 | B:STAGE3 | p_im1_B | p_im2_B | 不变 | （B 的 \(YA\) 在第 6 拍出） |
| 6 | 0 | B:STAGE4 | p_im1_B | p_im2_B | 不变 | （B 的 \(YB\) 在第 7 拍出） |

读法：

- **第 3 拍是关键交叠点**：B 的 STAGE 1 要把 `zbw_m1/zbw_m2` 写成 B 的实部乘积，而 A 的 STAGE 2 此刻**不触发**（因为第 3 拍 `x_nd_old[0] = x_nd[第2拍] = 0`）。所以两者不冲突——STAGE 1 写寄存器，STAGE 3 只读寄存器（经 `zbw_im` wire 和 `zbw_re` reg）。
- 真正会冲突的是**第 3 拍里同时出现 `x_nd=1` 和 `x_nd_old[0]=1`**，即「连续两拍给 1」。那时 STAGE 1 和 STAGE 2 都想写 `zbw_m1/zbw_m2`，逻辑就崩了。
- 从「乘积归属」列可见，`zbw_m1` 的取值序列是 `p_re1_A → p_im1_A → p_re1_B → p_im1_B`——**实部、虚部、实部、虚部**，严格交替，2 个乘法器从未闲着，却服务了两个交叠的蝶形。

#### 4.3.3 源码精读

「不能连续两拍」的契约检查就在 STAGE 1 内部，见 [butterfly.v:L127-L128](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L127-L128)：若本拍 `x_nd=1` 且上一拍 `x_nd_old[0]=1`（即连续两拍都有新数据），就打印 ERROR：

```verilog
if (x_nd_old[0])
  $display("ERROR: BF got new data two steps in a row.");
```

乘积寄存器的声明见 [butterfly.v:L78-L80](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L78-L80)：`zbw_m1/zbw_m2` 都是 `2*X_WDTH` 位有符号——因为两个 `X_WDTH` 位数相乘，积需要 `2*X_WDTH` 位才装得下。它们被 STAGE 1（[butterfly.v:L125-L126](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L125-L126)，写实部对）和 STAGE 2（[butterfly.v:L134-L135](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L134-L135)，写虚部对）**两个地方**写入，这正是「复用」在源码里的直接证据——同一对寄存器被两段代码轮流写。

再回看文件头注释 [butterfly.v:L11-L14](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L11-L14)：作者明说「输入不得超过每两拍一次，希望借此少用乘法块」。整条 4.3 就是在兑现这句注释。

#### 4.3.4 代码实践（本讲主实践）

**手画两次有效输入（`x_nd = 1,0,1`）下关键寄存器的逐拍时序图。**

1. 实践目标：亲眼看出 `zbw_m1` 如何被两个蝶形、实虚两组乘积分时复用。
2. 给定输入（沿用 u2-l2 的数，便于核对）：
   - 蝶形 A：\(W=-j\)（\(w_{re}=0,w_{im}=-1\)），\(XA=1\)（\(xa_{re}=1,xa_{im}=0\)），\(XB=0.5+0.5j\)（\(xb_{re}=0.5,xb_{im}=0.5\)）。
   - 蝶形 B：\(W=1\)（\(w_{re}=1,w_{im}=0\)），\(XA=0.5\)（\(xa_{re}=0.5,xa_{im}=0\)），\(XB=0.25\)（\(xb_{re}=0.25,xb_{im}=0\)）。
   - 时序：第 1 拍送 A（`x_nd=1`），第 2 拍空（`x_nd=0`），第 3 拍送 B（`x_nd=1`），之后 `x_nd=0`。
3. 操作步骤：画一张 6 列（第 1~6 拍）× 若干行（`x_nd`、`za_re[0]`、`zb_re`、`ww_re`、`zbw_m1`、`zbw_m2`）的表，按 4.2.2 与 4.3.2 的规则逐拍填。
4. 需要观察的现象：`zbw_m1` 的取值序列应当是「A 的实部乘积 → A 的虚部乘积 → B 的实部乘积 → B 的虚部乘积」，且第 3 拍虽然 A、B 同时在流水线里，却互不干扰。
5. 预期结果（参考答案，关键几格）：
   - `zbw_m1`：第 1 拍 \(p_{re1}^A=xb_{re}\,w_{re}=0.5\cdot0=0\)；第 2 拍 \(p_{im1}^A=zb_{re}\,ww_{im}=0.5\cdot(-1)=-0.5\)；第 3 拍 \(p_{re1}^B=0.25\cdot1=0.25\)；第 4 拍 \(p_{im1}^B=0.25\cdot0=0\)。
   - `zbw_m2`：第 1 拍 \(p_{re2}^A=0.5\cdot(-1)=-0.5\)；第 2 拍 \(p_{im2}^A=0.5\cdot0=0\)；第 3 拍 \(p_{re2}^B=0\cdot0=0\)；第 4 拍 \(p_{im2}^B=0\cdot1=0\)。
   - `za_re[0]`：第 2 拍起为 A 的 \(1\)，第 4 拍起被 B 的 \(0.5\) 覆盖。
   - `ww_re`：第 2 拍起为 A 的 \(0\)，第 4 拍起为 B 的 \(1\)。
   - 由此 A 的 \(\text{Re}(W\cdot XB)=p_{re1}-p_{re2}=0-(-0.5)=0.5\)，\(\text{Im}=p_{im1}+p_{im2}=-0.5+0=-0.5\)，与 u2-l2 手算的 \(W\cdot XB=0.5-0.5j\) 完全吻合。
6. 待本地验证：上述用理想实数手算；若在硬件里跑，这些值会先按 u2-l1 的 `c_to_int`/`f_to_istr` 量化成定点整数，再经过 `>>> (X_WDTH-2)` 右移，应在量化误差内接近上述比例。

#### 4.3.5 小练习与答案

**练习 1**：用 4 个乘法器一拍算完 4 个乘积，和用 2 个乘法器两拍算完，各自的吞吐是多少？
**答**：4 乘法器方案可以每拍接一个新蝶形（吞吐 1 蝶形/拍），但多用 2 个乘法器；本模块的 2 乘法器方案每 2 拍才能接一个新蝶形（吞吐 1 蝶形/2 拍），省下一半乘法器。这是「面积换吞吐」的经典取舍。

**练习 2**：如果 `x_nd` 被错误地设成 `1,1,1,...`，第 2 拍会发生什么？
**答**：第 2 拍 `x_nd=1` 且 `x_nd_old[0]=1`，触发 `$display("ERROR ...")`；更严重的是 STAGE 1 与 STAGE 2 同时写 `zbw_m1/zbw_m2`，老蝶形的虚部乘积被新蝶形的实部乘积覆盖，`zbw_re` 算错，后续 \(YA/YB\) 全错。

**练习 3**：第 3 拍里 A 的 STAGE 3 和 B 的 STAGE 1 同时触发，为什么不会出问题？
**答**：因为 STAGE 3 只**读**寄存器（经 `z1_*_big` wire 合成 \(YA\)、把 `zbw_im` 存进 `zbw_im_old`），STAGE 1 只**写** `zbw_m1/zbw_m2` 等寄存器；读写不冲突。会冲突的是「两个写」（STAGE 1 + STAGE 2），那只在连续两拍给 1 时才出现。

---

### 4.4 定点右移定标：>>>(X_WDTH-2) 与 >>>1 各管什么

#### 4.4.1 概念说明

在 STAGE 2~4 里能看到两次不同含义的右移，初学者很容易混。它们解决的问题完全不同：

- **`>>> (X_WDTH-2)`：乘法后的「尺度还原」**。这是为了把「旋转因子 × 数据」的乘积从「乘出来的宽尺度」拉回「数据的正常尺度」。
- **`>>> 1`：每级防溢出的「除以 2」**。这是为了让 FFT 逐级累加时不爆掉，主动把每个蝶形的输出再缩小一半。

两者一个管「定点格式对齐」，一个管「动态范围防溢出」，互不相干。

#### 4.4.2 核心流程

**为什么是 `>>> (X_WDTH-2)`？** 由 u2-l1，旋转因子是 Q2.(width-2) 格式（\(1.0 = 2^{X\_WDTH-2}\)），数据是 Q1.(width-1) 格式（\(1.0 \approx 2^{X\_WDTH-1}\)）。两个定点数相乘，小数位数相加：乘积的小数位数为 \((X\_WDTH-2)+(X\_WDTH-1)\)。要把它还原成数据尺度（小数位数 \(X\_WDTH-1\)），需要右移：

\[
\bigl((X\_WDTH-2)+(X\_WDTH-1)\bigr) - (X\_WDTH-1) = X\_WDTH-2
\]

所以乘积要 `>>> (X_WDTH-2)` 才回到数据尺度。这就是 [butterfly.v:L84](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L84) 与 [butterfly.v:L141](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L141) 里那个 `>>> (X_WDTH-2)` 的来历。

**为什么还要 `>>> 1`？** 一个 \(N\) 点 FFT 共有 \(\log_2 N\) 级，每级的蝶形做 \(XA \pm W\cdot XB\)。若不缩放，最坏情况下幅度逐级翻倍，\(\log_2 N\) 级后可能放大 \(N\) 倍，定点必然溢出。作者让每个蝶形输出再除以 2（`>>> 1`），\(\log_2 N\) 级合计正好缩小约 \(N\) 倍，把整体动态范围摁住——这就是 u1-l1 提到的「输出整体缩小约 \(N\) 倍」的来源。所以端口 `y` 给出的是理想 \(YA/2\)、\(YB/2\)（u2-l2 也提过这一点）。

#### 4.4.3 源码精读

乘积还原尺度的两处 `>>> (X_WDTH-2)`：

- 虚部 wire，[butterfly.v:L84](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L84)：
  ```verilog
  assign zbw_im = (zbw_m1 >>> (X_WDTH-2)) + (zbw_m2 >>> (X_WDTH-2));
  ```
- 实部 reg，[butterfly.v:L141](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L141)：
  ```verilog
  zbw_re <= (zbw_m1 >>> (X_WDTH-2)) - (zbw_m2 >>> (X_WDTH-2));
  ```

注意是「先各自右移、再加减」，而不是「先加减、再右移」——因为 `zbw_m1/zbw_m2` 是 `2*X_WDTH` 位，加减前若不各自缩回 `X_WDTH` 位，位宽会对不上。注释 [butterfly.v:L138-L140](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L138-L140) 还给出了一条数学保证：\(|W|\le1,|XB|\le1 \Rightarrow |W\cdot XB|\le1\)，所以还原尺度后的结果一定能装进 `X_WDTH` 位，相减不会溢出。

防溢出的 `>>> 1` 在最终输出处，[butterfly.v:L155-L156](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L155-L156)（\(YA\)）与 [butterfly.v:L164-L165](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L164-L165)（\(YB\)）：

```verilog
y_re <= z1_re_big >>> 1;   // YA 的实部，再除以 2
...
y_re <= z2_re_big >>> 1;   // YB 的实部，再除以 2
```

而 `z1_*_big`/`z2_*_big` 故意比 `X_WDTH` 多 1 位（`[X_WDTH:0]`，见 [butterfly.v:L90-L97](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L90-L97)），是为了让 \(XA \pm W\cdot XB\) 这次加减不丢最高位；注释 [butterfly.v:L86-L89](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L86-L89) 说得很明白：「不加宽就会丢高位」。

#### 4.4.4 代码实践

**手算一次「乘积还原」的位移量。**

1. 实践目标：验证 `>>> (X_WDTH-2)` 确实把乘积拉回数据尺度。
2. 操作步骤：取 `X_WDTH=16`。则旋转因子尺度 \(1.0=2^{14}\)，数据尺度 \(1.0\approx2^{15}\)。一个「旋转因子整数 × 数据整数」的乘积，小数位数为 \(14+15=29\)。
3. 需要观察的现象：要回到数据尺度（小数位数 15），需右移 \(29-15=14=X\_WDTH-2\) 位。
4. 预期结果：与源码里的 `>>> (X_WDTH-2)` 完全一致。
5. 待本地验证：可把一个 \(-1.0\) 的旋转因子（整数 \(-2^{14}\)）和一个 \(0.5\) 的数据（整数 \(2^{14}\)）相乘得 \(-2^{28}\)，再 `>>> 14` 得 \(-2^{14}\)，正好是 \(-0.5\) 在旋转因子尺度下的整数表示——验证了尺度还原正确。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `>>> (X_WDTH-2)` 误写成 `>>> (X_WDTH-1)`，结果会怎样？
**答**：多右移了 1 位，相当于把 \(W\cdot XB\) 整体再除以 2，\(YA/YB\) 的幅度会比正确值小一半（在已有的 `>>>1` 之外又少了一倍），FFT 输出幅度系统性偏小。

**练习 2**：`>>> 1` 是为了防溢出，那它引入了什么代价？
**答**：每级丢掉 1 位精度（最低位被移走），\(\log_2 N\) 级累计会引入额外的定点量化误差；好处是保证了整个 FFT 不会逐级溢出。这是「动态范围 vs 精度」的取舍，u4 单元的量化误差测试会专门检验它。

**练习 3**：为什么 `z1_re_big` 要比 `zbw_re` 多 1 位？
**答**：`z1_re_big = za_re[0] + zbw_re` 是两个 `X_WDTH` 位有符号数相加，结果可能多出 1 位进位/符号位；若不加宽就会丢最高位导致溢出，所以特意声明成 `[X_WDTH:0]`。

## 5. 综合实践

**把一个交叠蝶形的「数学—时序—定点」一条龙走通。**

沿用 4.3.4 的两个蝶形 A（\(W=-j, XA=1, XB=0.5+0.5j\)）和 B（\(W=1, XA=0.5, XB=0.25\)），时序为第 1 拍送 A、第 3 拍送 B。请按顺序完成：

1. **列使能**：写出第 1~6 拍的 `x_nd_old[0:2]` 取值，标出每拍触发的 STAGE（参考 4.1.2）。
2. **追乘积**：填出 `zbw_m1`、`zbw_m2` 在第 1~4 拍的取值，指出它们如何在 A 的实部对、A 的虚部对、B 的实部对、B 的虚部对之间切换（参考 4.3.4 的参考答案）。
3. **算 \(W\cdot XB\)**：用第 2 步的乘积，分别算出 A 的 `zbw_re`（减）和 `zbw_im`（加），确认 \(W_A\cdot XB_A = 0.5-0.5j\)。
4. **看有效期**：指出 `zbw_im`（wire）在哪一拍有效、为何 STAGE 3 必须把它存进 `zbw_im_old` 才能让 STAGE 4 算出 \(YB\) 的虚部。
5. **定标**：说明若 `X_WDTH=16`，乘积要 `>>> 14` 还原尺度、输出再 `>>> 1` 防溢出；并解释为何端口 `y` 给出的是理想 \(YA/2\)、\(YB/2\)。
6. **冲突边界**：解释为什么「第 3 拍同时跑 A 的 STAGE 3 和 B 的 STAGE 1」安全，而「连续两拍 `x_nd=1`」会触发 ERROR。

参考答案要点：

1. `x_nd_old[0:2]`：第1拍 `000`、第2拍 `100`、第3拍 `010`、第4拍 `101`、第5拍 `010`、第6拍 `001`；触发的 stage 见 4.3.2 表。
2. `zbw_m1`：`0 → -0.5 → 0.25 → 0`；`zbw_m2`：`-0.5 → 0 → 0 → 0`（依次为 A实/A虚/B实/B虚）。
3. A：`zbw_re = 0-(-0.5)=0.5`，`zbw_im = -0.5+0=-0.5`，即 \(0.5-0.5j\)。
4. `zbw_im` 仅在第 3 拍有效；STAGE 3 存 `zbw_im_old`，供第 4 拍 STAGE 4 算 \(YB\) 虚部（`z2_im_big = za_im[1] - zbw_im_old`）。
5. `X_WDTH=16` → 乘积 `>>>14` 还原、输出 `>>>1` 防溢出；端口 `y = YA/2`、`YB/2`，\(\log_2 N\) 级累计缩小约 \(N\) 倍。
6. STAGE 3 只读、STAGE 1 只写，读写不冲突；连续两拍 `x_nd=1` 会让 STAGE 1 与 STAGE 2 同时写 `zbw_m1/zbw_m2`，触发 [butterfly.v:L127-L128](https://github.com/benreynwar/fft-dit-fpga/blob/ff02d3d19ef4c74d4f672ab43645443173d41ae9/butterfly.v#L127-L128) 的 ERROR。

## 6. 本讲小结

- `butterfly` 把一个蝶形摊成 **四级流水线**，由 `x_nd` 及其三级延迟 `x_nd_old[0..2]` 分别充当 STAGE 1~4 的使能；一个 `x_nd` 脉冲会在随后三拍自动点亮 STAGE 2~4。
- STAGE 1 锁存 \(W/XA/XB\) 并启动实部两乘积；STAGE 2 启动虚部两乘积并算出 \(\text{Re}(W\cdot XB)\)；STAGE 3 合成 \(YA\)；STAGE 4 合成 \(YB\)。\(YA\) 在第 4 拍可见、\(YB\) 在第 5 拍可见。
- **乘法器复用**：同一对 `zbw_m1/zbw_m2` 先装实部乘积、再装虚部乘积，用 2 个乘法器分两拍算出 4 个乘积；为此新输入不得连续两拍到达，否则 STAGE 1 与 STAGE 2 争抢同一对寄存器，触发 `$display("ERROR ...")`。
- `zbw_re` 是 reg（跨拍保持），`zbw_im` 是 wire（只在虚部乘积在场那一拍有效），所以 STAGE 3 必须把虚部存进 `zbw_im_old`，STAGE 4 才能算出 \(YB\) 的虚部。
- 两次右移各司其职：`>>> (X_WDTH-2)` 把「旋转因子×数据」的乘积还原回数据尺度；`>>> 1` 是每级除以 2 防溢出，使端口 `y` 给出理想 \(YA/2\)、\(YB/2\)。

## 7. 下一步学习建议

到这里，FFT 的「计算原子」`butterfly` 已经被我们从数学、端口、流水线三个角度彻底拆完。下一站进入 **u3 单元《dit 主模块：数据与控制》**，重点看 `dit.v` 如何把成千上万个这样的小蝶形串成一条完整的 FFT：

- u3-l1 会讲 `dit` 的多级缓冲（`bufferin0/1`、`bufferX/Y`、`bufferout`）与双缓存翻转——你会看到 `butterfly` 的 `m_in/m_out` 旁路（u2-l2 讲过）在这里如何被用来给每个结果指路。
- u3-l2 会讲 `dit` 的四状态控制机（INIT/IDLE/CALC/SEND），它会按本讲学的「每两拍喂一个蝶形」的节奏去驱动 `x_nd`。
- u3-l3 会讲蝶形地址与旋转因子地址的位运算推导。

建议在进入 u3 前，先回头确认两件事：一是你能默写出本讲 4.2.2 的单蝶形时序表，二是你能解释清楚「为什么 `x_nd` 必须隔拍」。这两点是理解 `dit` 控制机为什么要那样设计 `x_nd` 节奏的前提。之后带着「`dit` 是如何把数据喂进这条四级流水线、又如何把结果收回来」的问题去读 `dit.v` 即可。
