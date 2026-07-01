# 混频器、DDS 与复数乘法

## 1. 本讲目标

本讲是 DSP 基础模块的第二讲（承接 [u3-l1 CORDIC 核](u3-l1-cordic.md)）。学完后你应当能够：

- 说清楚 **相位累加器 `ph_acc`** 如何用「粗计数器 + 细残差计数器 + 模数」产生任意有理数频率的相位，并理解它为何能把 32 位控制拆成 20+12 位。
- 说清楚 **`rot_dds`** 如何把 `ph_acc` 的相位喂给 CORDIC，得到一对流水线化的正余弦本振（LO）。
- 读懂 **`mixer`** 实数混频器的流水线、`NORMALIZE`/非 `NORMALIZE` 两条分支，并能准确推算 `dwi/dwlo/davr/NUM_DROP_BITS` 这组参数如何决定输出位宽与截取窗口。
- 读懂 **`complex_mul`**（时分复用、IQ 交织）与 **`complex_mul_flat`**（并行、四乘法器）两种复数乘法器，理解它们的接口、吞吐量与饱和逻辑差异。
- 把「DDS 本振 → 实数混频 / 复数混频」这条 DSP 最基础的数据通路在脑子里串起来，为下一讲 [u3-l3 下变频与上变频](u3-l3-downconvert-upconvert.md)打基础。

## 2. 前置知识

本讲默认你已经学过 [u3-l1 CORDIC 核](u3-l1-cordic.md) 和 [u2-l1 基于 Make 的 HDL 仿真测试方法](u2-l1-make-hdl-testing.md)。在此基础上，再用通俗语言补几个概念：

- **DDS（Direct Digital Synthesis，直接数字频率合成）**：用「每个时钟给相位累加器加一个固定步长，再用查找表/CORDIC 把相位转成正余弦」的办法产生模拟正弦波。步长越大，输出频率越高。
- **本振（LO，Local Oscillator）**：混频时用来与信号相乘的那路「参考正弦/余弦」。`rot_dds` 就是 Bedrock 的 LO 发生器。
- **混频（mixing）**：信号乘以本振，把频谱搬移到另一个频率。实数混频用一个 LO（余弦）；复数（IQ）混频用一对正交的 sin/cos，可以无镜像地把信号搬到基带。
- **定点（fixed-point）**：FPGA 里没有浮点，所有数都是固定位宽的整数。一个 \(N\) 位有符号数表示 \([-2^{N-1},\,2^{N-1}-1]\)。乘法会让位宽翻倍，因此**截位/饱和**是定点 DSP 的核心烦恼——本讲的 `NUM_DROP_BITS`、`davr`、`SAT` 宏都在解决它。
- **有理数频率与非二进制模数**：理想 DDS 要求每拍相位增量是 \(2^N\) 的整数分之一，但真实系统（如 SSRF 的 8/11、Argonne 的 9/13）往往不是。`ph_acc` 用 Bresenham/可编程模数技巧精确合成这种「刁钻」频率。

> 命名提醒（来自 [u1-l4 RTL 编码规范](u1-l4-rtl-guidelines.md)）：`dsp/` 子系统有大量历史遗留的小写参数（如 `dwi/davr/dwlo`），这属于历史现状、不是规范变更，读代码时按字面理解即可。

## 3. 本讲源码地图

本讲涉及的真实源码如下，全部在 `dsp/` 目录：

| 文件 | 作用 | 是否有独立测试台 |
| --- | --- | --- |
| [dsp/ph_acc.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/ph_acc.v) | 相位累加器，DDS 的相位发生电路 | 无（经 `rot_dds_tb` 间接测试） |
| [dsp/rot_dds.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/rot_dds.v) | 相位旋转 DDS：`ph_acc` + CORDIC，输出 sin/cos | 有 `rot_dds_tb` |
| [dsp/mixer.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/mixer.v) | 实数混频器（ADC × LO） | **无独立测试台**（经 `cic_multichannel_tb` 间接测试） |
| [dsp/complex_mul.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/complex_mul.v) | 时分复用复数乘法器（IQ 交织，2 个乘法器，2 拍 1 个结果） | 有 `complex_mul_tb` |
| [dsp/complex_mul_flat.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/complex_mul_flat.v) | 并行复数乘法器（4 个乘法器，1 拍 1 个结果） | 有 `complex_mul_flat_tb` |

辅助脚本（实践环节用到）：

| 文件 | 作用 |
| --- | --- |
| [dsp/tb_pycheck.py](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/tb_pycheck.py) | 含 `fraction_to_ph_acc()`，把有理频率换算成 `ph_acc` 的控制字 |
| [dsp/rules.mk](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/rules.mk) | `TEST_BENCH` 清单与各 `_check` 目标 |

> 关于「`mixer` 无独立测试台」：在 `dsp/rules.mk` 的 `TEST_BENCH` 列表里查不到 `mixer_tb`（见 [dsp/rules.mk:8](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/rules.mk#L8)）。这与 [u3-l1](u3-l1-cordic.md)/[u1-l3](u1-l3-directory-structure.md) 已经指出的事实一致——Bedrock 里并非每个模块都有独立 testbench，`mixer` 是被上层模块实例化后间接测试的（例如 [dsp/cic_multichannel_tb.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/cic_multichannel_tb.v) 第 138–157 行就实例化了 `mixer`）。所以 `make -C dsp mixer_check` 并不是一个存在的目标，本讲第 4.3 节的实践会改成「读源码 + 手算位宽」的源码阅读型实践。

## 4. 核心概念与源码讲解

### 4.1 ph_acc：相位累加器（DDS 的相位源）

#### 4.1.1 概念说明

DDS 的第一步是产生一个**每个时钟匀速增长的相位**。最朴素的办法是：维护一个 \(N\) 位累加器，每拍加一个步长 `phase_step`，溢出即自动取模，步长就决定频率。这要求频率是「二进制友好」的（\(2^N\) 的整数分之一）。

但 LLRF 系统里常见**非二进制**频率比，例如 SSRF 的 \(F_{IF}/F_s = 8/11\)、Argonne RIA 的 \(9/13\)。`ph_acc` 借鉴 AD9915 的「可编程模数模式」与 Bresenham 直线算法的思想：用一个**粗计数器**走大部分步长，把截断造成的**残差**累计到一个**细计数器**里，残差溢出时再补一个最小步长——从而**无长期相位漂移**地合成任意有理频率。

#### 4.1.2 核心流程

`ph_acc` 的控制字被故意拆成可塞进一个 32 位字的几段（见 [dsp/ph_acc.v:8-11](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/ph_acc.v#L8-L11)）：

- `phase_step_h`（20 位）：粗步长，整数相位增量。
- `phase_step_l`（12 位）：细步长，每拍累加到 12 位残差计数器。
- `modulo`（12 位）：残差计数器溢出时的补偿量；`0` 表示纯二进制模式。

每拍（`en` 有效时）：

1. 残差计数器 `phase_l` 累加 `phase_step_l`；若上拍产生了进位 `carry`，再加一个 `modulo`。
2. `phase_l` 的进位 `carry` 喂给下一拍的粗计数器。
3. 粗计数器 `phase_h` 累加 `phase_step_h + carry`。
4. 输出 `phase_acc = phase_h[19:1]`（取高 19 位，丢掉最低位）。

相位增量（每拍、单位为整圈的 \(2^{-20}\)）近似为：

\[
\Delta\phi \approx \text{phase\_step\_h} + \frac{\text{phase\_step\_l}}{2^{12}} \quad(\text{modulo}=0\text{ 时})
\]

注释里给出两个真实算例（见 [dsp/ph_acc.v:34-42](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/ph_acc.v#L34-L42)）：SSRF \(8/11\) 用 `phase_step_h=762600`，Argonne \(9/13\) 用 `phase_step_h=725937`。

#### 4.1.3 源码精读

模块端口——注意 32 位控制被拆成 20+12 位（[dsp/ph_acc.v:44-52](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/ph_acc.v#L44-L52)）：

```verilog
module ph_acc(
    input clk, input reset, input en,
    output [18:0] phase_acc,       // 输出相位字（19 位）
    input [19:0] phase_step_h,     // 粗步长
    input [11:0] phase_step_l,     // 细步长
    input [11:0] modulo            // 非二进制模数编码；0 表示二进制
);
```

核心累加逻辑——一行同时算进位和细计数器（[dsp/ph_acc.v:57-62](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/ph_acc.v#L57-L62)）：

```verilog
always @(posedge clk) if (en) begin
    {carry, phase_l} <= reset ? 13'b0 :
                        ((carry ? modulo : 12'b0) + phase_l + phase_step_l);
    phase_step_hp <= phase_step_h;
    reset1 <= reset;
    phase_h <= reset1 ? 20'b0 : (phase_h + phase_step_hp + carry);
end
```

`{carry, phase_l}` 把 12 位加法的进位拼进最高位，是 Verilog 里「免费拿到进位」的惯用法。`phase_step_hp`/`reset1` 是把 `phase_step_h`/`reset` 延一拍，使粗、细两级时序对齐。

输出取粗计数器高 19 位（[dsp/ph_acc.v:63](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/ph_acc.v#L63)）：

```verilog
assign phase_acc = phase_h[19:1];
```

#### 4.1.4 代码实践

1. **实践目标**：验证「有理频率 → `ph_acc` 控制字」的换算，理解三个控制字的含义。
2. **操作步骤**：
   - 阅读 [dsp/tb_pycheck.py:5-44](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/tb_pycheck.py#L5-L44) 的 `fraction_to_ph_acc((num, den))`，对照 [dsp/ph_acc.v:34-42](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/ph_acc.v#L34-L42) 的两个注释算例。
   - 手算验证 SSRF \(8/11\)：`phase_step_h = floor(2^20 * 8 / 11) = 762600`。
3. **需要观察的现象**：`fraction_to_ph_acc((8,11))` 应返回 `(762600, 2976, 4)`，与源码注释一致。
4. **预期结果**：三个数分别对应粗步长、细步长、模数。若不一致，检查你是否把 `modulo` 当成了数学模数——注释明确指出下载的 `modulo` 是「数学模数的二进制补码」（[dsp/ph_acc.v:32-34](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/ph_acc.v#L32-L34)）。
5. **运行命令**：直接调用该函数（注意 `tb_pycheck.py` 顶部 `import matplotlib`，导入即需要 matplotlib；若想避免依赖，可把 `fraction_to_ph_acc` 函数体抄出来单独运行）。运行结果**待本地验证**。

#### 4.1.5 小练习与答案

- **练习 1**：为什么把控制字拆成 20+12 位，而不是直接一个 32 位步长？
  - **答案**：为了在 32 位总线系统里**原子更新**整个频率控制字；同时 12 位细计数器 + 模数能精确合成非二进制有理频率（见 [dsp/ph_acc.v:8-11](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/ph_acc.v#L8-L11)）。
- **练习 2**：`phase_acc` 为什么是 `phase_h[19:1]` 而不是 `phase_h[19:0]`？
  - **答案**：粗计数器 `phase_h` 是 20 位，但输出给下游 CORDIC 的相位字是 19 位；丢掉最低位等价于把相位量化到 \(2^{-19}\) 圈，正好匹配 CORDIC 的 `phasein` 位宽。

---

### 4.2 rot_dds：相位旋转的直接数字频率合成

#### 4.2.1 概念说明

`ph_acc` 只产生「相位」。要把相位变成正余弦幅度，需要三角函数引擎——Bedrock 用的是上一讲的 **CORDIC**（[u3-l1](u3-l1-cordic.md)）。`rot_dds` 就是把两者拼起来的薄封装：`ph_acc` 喂相位 → CORDIC 旋转模式（`op=2'b00`，极坐标→直角坐标）输出 \((\cos,\sin)\)。这就是 Bedrock 的本振（LO）发生器。

回忆 CORDIC 的固有增益约 \(1.64676\)。为了让满量程旋转不溢出 18 位有符号输出，本振幅度要预先缩小这个倍数。

#### 4.2.2 核心流程

1. `ph_acc` 每拍产出 19 位相位 `phase_acc`。
2. 把固定幅度 `lo_amp`（\(\approx 2^{17}/1.64676\)）作为 CORDIC 的 \(x\) 输入、相位作为 `phasein`、`op=2'b00`（旋转模式）。
3. CORDIC 流水线输出 `cosa`、`sina`（均 18 位有符号）。

幅度定标关系：

\[
\text{lo\_amp} = \left\lfloor \frac{2^{17}}{1.64676} \right\rfloor = 79594
\]

源码取略小的 `79590` 留一点余量（[dsp/rot_dds.v:28-32](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/rot_dds.v#L28-L32)）。

#### 4.2.3 源码精读

`lo_amp` 参数与注释（[dsp/rot_dds.v:28-32](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/rot_dds.v#L28-L32)）：

```verilog
// 2^17/1.64676 = 79594, use a smaller value to keep CORDIC round-off
// from overflowing the output
parameter lo_amp = 18'd79590;
```

实例化 `ph_acc`（[dsp/rot_dds.v:34-41](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/rot_dds.v#L34-L41)）：

```verilog
wire [18:0] phase_acc;
ph_acc ph_acc_i (
  .clk(clk), .reset(reset), .en(1'b1),
  .phase_acc(phase_acc),
  .phase_step_h(phase_step_h),
  .phase_step_l(phase_step_l),
  .modulo(modulo)
);
```

把相位喂给 CORDIC（`cordicg_b22`，旋转模式 `opin=2'b00`，见 [dsp/rot_dds.v:44-48](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/rot_dds.v#L44-L48)）：

```verilog
cordicg_b22 #(.nstg(20), .width(18), .def_op(0)) trig(
    .clk(clk), .opin(2'b00),
    .xin(lo_amp), .yin(18'd0), .phasein(phase_acc),
    .xout(cosa), .yout(sina));
```

`cordicg_b22` 正是 [u3-l1](u3-l1-cordic.md) 讲过的、由 `cordicgx.py` 按 DPW=22 生成的全展开 CORDIC；这里 `opin=2'b00` 是旋转（极→直）模式。

#### 4.2.4 代码实践

1. **实践目标**：跑通 DDS，观察它真的合成了目标频率的正余弦。
2. **操作步骤**：`make -C dsp rot_dds_check`。该 testbench 用 Argonne \(9/13\) 的参数（`phase_step_h=725937, phase_step_l=945, modulo=1`），见 [dsp/rot_dds_tb.v:21-32](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/rot_dds_tb.v#L21-L32)。
3. **需要观察的现象**：仿真输出 `rms error = X.XXX bits`，然后打印 `PASS` 或 `FAIL`。
4. **预期结果**：testbench 把 `sina/cosa` 与软件 \(amp\cdot\sin/\cos(2\pi\cdot 9t/13)\) 比较，方差阈值 `variance/26 > 0.7` 判失败（[dsp/rot_dds_tb.v:40-57](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/rot_dds_tb.v#L40-L57)）。应看到 rms 误差在零点几 bit 量级并 `PASS`。运行结果**待本地验证**。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 `lo_amp` 用 79590 而不是理论值 79594？
  - **答案**：CORDIC 每级有舍入误差，用略小的幅度留余量，避免满量程旋转时 18 位输出溢出（[dsp/rot_dds.v:28-32](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/rot_dds.v#L28-L32)）。
- **练习 2**：把 `opin` 从 `2'b00` 改成 `2'b01`（向量模式）会发生什么？
  - **答案**：模块会从「给定幅度+相位求 (cos,sin)」变成「给定 (x,y) 求模长+幅角」，DDS 就不再是正余弦源了。`opin` 可逐拍变化是 CORDIC 的特性（见 [u3-l1](u3-l1-cordic.md)）。

---

### 4.3 mixer：实数混频器（ADC × LO）

#### 4.3.1 概念说明

`mixer` 做的是最基本的一件事：把 ADC 采样 `adcf` 与本振 `mult`（来自 `rot_dds` 的 sin 或 cos）相乘，完成实数混频（频谱搬移）。难点不在乘法本身，而在**定点截位**：两个有符号数相乘位宽会翻倍（`dwi+dwlo` 位），必须截回一个合理的输出位宽，同时尽量不丢有用精度、又不溢出。

`mixer` 用 `generate` 提供两条流水线分支：

- `NORMALIZE=1`：单级、带**四舍五入**的截位。
- `NORMALIZE=0`（默认）：多级流水线、靠 `NUM_DROP_BITS` 丢冗余符号位来换取低位精度。

#### 4.3.2 核心流程（默认分支）

1. 寄存输入 `adcf→adcf1`、`mult→mult1`（一级流水）。
2. 乘法 `mix_out_r <= adcf1 * mult1`（`dwi+dwlo` 位，又一级流水）。
3. 截位 `mix_out1 <= mix_out_r[高位:低位]`（再一级流水）。
4. 再寄存一拍 `mix_out2 <= mix_out1`，输出 `mixout = mix_out2`。

**位宽推算（关键）**：输出端口声明为 `signed [dwi+davr-1:0]`，所以

\[
\text{输出位宽} = \text{dwi} + \text{davr}
\]

默认参数下 \(=16+4=20\) 位。截取窗口为（[dsp/mixer.v:42](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/mixer.v#L42)）：

```verilog
mix_out1 <= mix_out_r[dwi+dwlo-NUM_DROP_BITS-1 : dwlo-davr-NUM_DROP_BITS];
```

这段切片的位宽恰好是 `dwi+davr`，与输出端口一致。由此得到两个重要结论：

- **`davr` 直接决定输出位宽**：`davr` 越大，输出越宽，保留下来的低位（保护/舍入）位越多。注释举例：下游 CIC 平均 64 倍时，有用信息增长 \(\sqrt{64}=8\approx 3\) 位，再加舍入余量共设 `davr=4`（[dsp/mixer.v:10-13](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/mixer.v#L10-L13)）。
- **`NUM_DROP_BITS` 不改变输出位宽，而是平移截取窗口**：乘积最高位通常是「冗余符号位」（因为本振永不为 \(-1\) 满量程负值）。每丢一个冗余符号位，窗口整体下移一位，就能多保留一位低位精度。`NUM_DROP_BITS=1` 是典型值（[dsp/mixer.v:5-8](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/mixer.v#L5-L8)）。

> 反直觉点：增大 `NUM_DROP_BITS` **不会**让输出变窄，它是在「假定乘积够小、冗余符号位安全」的前提下，把那一位省下来换成更低的 LSB。如果假设不成立（两个负满量程相乘），就会溢出。

#### 4.3.3 源码精读

参数与端口（[dsp/mixer.v:3-19](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/mixer.v#L3-L19)）：

```verilog
module mixer #(
   parameter NORMALIZE = 0,
   parameter NUM_DROP_BITS = 1,   // 输出端丢掉的位数（典型丢 1 位冗余符号位）
   parameter dwi       = 16,      // ADC 输入位宽
   parameter davr      = 4,       // 输出保留的保护位
   parameter dwlo      = 18       // 本振输入位宽
) (
   input                        clk,
   input  signed [dwi-1:0]      adcf,
   input  signed [dwlo-1:0]     mult,
   output signed [dwi+davr-1:0] mixout    // 输出位宽 = dwi+davr
);
```

`NORMALIZE` 分支：截位时加上 `mix_out_w[dwlo-davr-1]` 做**四舍五入**（[dsp/mixer.v:28-36](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/mixer.v#L28-L36)）：

```verilog
if (NORMALIZE==1) begin : g_normalize
   ...
   mixout_r <= mix_out_w[dwi+dwlo-1:dwlo-davr] + mix_out_w[dwlo-davr-1];
end
```

默认分支的截位（[dsp/mixer.v:37-46](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/mixer.v#L37-L46)）：

```verilog
else begin : ng_normalize
   always @(posedge clk) begin
      adcf1     <= adcf;
      mult1     <= mult;
      mix_out_r <= adcf1 * mult1;  // 内部乘法，流水一级
      mix_out1  <= mix_out_r[dwi+dwlo-NUM_DROP_BITS-1:dwlo-davr-NUM_DROP_BITS];
      mix_out2  <= mix_out1;        // 再流水一级
   end
   assign mixout = mix_out2;
end
```

#### 4.3.4 代码实践（源码阅读型）

> 说明：本仓库**没有** `mixer_tb.v`，`mixer_check` 不是有效目标（见第 3 节）。因此本实践是「读源码 + 手算」。

1. **实践目标**：说清 `NUM_DROP_BITS` 与 `davr` 对输出位宽与截取窗口的影响。
2. **操作步骤**：
   - 读 [dsp/mixer.v:42](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/mixer.v#L42) 的截位表达式。
   - 用默认参数（`dwi=16, dwlo=18, davr=4, NUM_DROP_BITS=1`）手算：乘积 `mix_out_r` 是 \(16+18=34\) 位；截取窗口为 `[16+18-1-1 : 18-4-1] = [32:13]`，共 \(32-13+1=20\) 位。
   - 把 `davr` 改成 `6`：窗口变 `[32:11]`，输出位宽 \(16+6=22\) 位。
   - 把 `NUM_DROP_BITS` 改成 `2`（仍 `davr=4`）：窗口变 `[31:12]`，输出位宽仍 20 位，但整体下移一位、多保留一位低位。
3. **需要观察的现象**：输出位宽只随 `davr` 变；`NUM_DROP_BITS` 只移动窗口、不改位宽。
4. **预期结果**：与上面的手算一致。若想看真实波形，可参考 `mixer` 被实例化的地方——[dsp/cic_multichannel_tb.v:138-157](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/cic_multichannel_tb.v#L138-L157) 或封装 [dsp/iq_mixer_multichannel.v:28-50](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/iq_mixer_multichannel.v#L28-L50)，跑 `make -C dsp cic_multichannel_check` 间接验证。
5. **若想直接测 `mixer`**：可自行写一个最小 testbench（**示例代码**，不在仓库中），实例化 `mixer` 喂固定 `adcf`、`mult`，断言输出等于期望截位值；改 `NUM_DROP_BITS` 看是否仍匹配。

#### 4.3.5 小练习与答案

- **练习 1**：默认参数下输出位宽是多少？`mix_out_r` 总位宽是多少？
  - **答案**：输出 \(dwi+davr=20\) 位；`mix_out_r` 是 \(dwi+dwlo=34\) 位（[dsp/mixer.v:24](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/mixer.v#L24)）。
- **练习 2**：把 `NUM_DROP_BITS` 从 1 改到 0，输出会变得更「安全」还是更「精确」？
  - **答案**：更安全（保留完整符号位、不易溢出），但少保留一位低位精度。`NUM_DROP_BITS` 是「精度 vs 溢出风险」的权衡旋钮。

---

### 4.4 complex_mul：复数乘法（两种实现）

复数乘法是把 IQ 信号做相位/幅度旋转的核心运算。Bedrock 给出两种实现：**时分复用**的 `complex_mul`（省乘法器、吞吐减半）和**并行**的 `complex_mul_flat`（四乘法器、全速）。两者数学相同：

\[
(a+jb)(c+jd) = (ac-bd) + j(ad+bc)
\]

下面先讲共享的 `complex_mul`，再讲 `complex_mul_flat` 并对比。

#### 4.4.1 概念说明

`complex_mul` 的设计前提是：IQ 数据在时间上**交织**成一条流（先 I 后 Q），`x` 端口依次承载 \(a\)、\(b\)，`y` 端口依次承载 \(c\)、\(d\)，`iq` 标志当前是 I 还是 Q。这样只需 **2 个乘法器**轮流算 \(ac\)、\(bd\)、\(ad\)、\(bc\)，但**每两个时钟才出一个复数结果**。输入输出假定定标到 \([-1,1)\)（18 位有符号，MSB 为符号位，相当于 17 位小数）。

由于 \((1+j)(1-j)=2\) 这类极端输入会超出 \([-1,1)\)，模块对结果做**饱和（saturation）**。`SAT` 宏（[dsp/complex_mul.v:70](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/complex_mul.v#L70)）的语义是：若高位是合法的符号扩展（全 0 或全 1），就取低位；否则钳位到最大/最小值。

#### 4.4.2 核心流程

1. 用 `iq_sr` 移位寄存器跟踪「当前数据对应几拍前的 iq」。
2. 第一拍：`prod1 <= x*y`（算 \(ac\) 或 \(ad\) 等）。
3. 利用 `m2mux = iq_sr[1] ? x2 : x`，把「上一对的另一个操作数」取出来算 `prod2`，这样两个乘法器交替覆盖四个乘积项。
4. `sumi <= prod1_d - prod1`、`sumq <= prod2_d + prod2 + 1`：实部相减、虚部相加；`+1` 把平均误差偏置降到 \(-1/4\) 结果位（[dsp/complex_mul.v:49-52](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/complex_mul.v#L49-L52)）。
5. 按 `iq_sr[3]` 在 `sumi`/`sumq` 间选择，`SAT` 饱和到 `dw` 位输出。
6. 结果相对输入延迟 **4 拍**（`gate_out` 是 `gate_in` 延迟 4 拍，用来对齐数据流，[dsp/complex_mul.v:22-25](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/complex_mul.v#L22-L25)）。

#### 4.4.3 源码精读

端口——注意 `x`/`y`/`z` 都是「时分交织的 I/Q」（[dsp/complex_mul.v:27-38](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/complex_mul.v#L27-L38)）：

```verilog
module complex_mul #( parameter dw = 18 ) (
    input clk, input gate_in,
    input signed [dw-1:0] x,   // 交织的 a/b
    input signed [dw-1:0] y,   // 交织的 c/d
    input iq,                  // 高=I, 低=Q
    output signed [dw-1:0] z,
    output signed [(2*dw)-1:0] z_all,  // 无舍入误差的全精度副本
    output gate_out
);
```

两个乘法器交替工作（[dsp/complex_mul.v:57-68](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/complex_mul.v#L57-L68)）：

```verilog
wire signed [dw-1:0] m2mux = iq_sr[1] ? x2 : x;
always @(posedge clk) begin
    x1 <= x;  x2 <= x1;  y1 <= y;
    prod1 <= x*y;
    prod2 <= m2mux * y1;
    prod1_d <= prod1;  prod2_d <= prod2;
    sumi <= prod1_d - prod1;
    sumq <= prod2_d + prod2 + 1;
end
```

饱和与输出（`SAT` 宏在 [dsp/complex_mul.v:70](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/complex_mul.v#L70) 定义，[dsp/complex_mul.v:71-81](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/complex_mul.v#L71-L81) 使用）：

```verilog
wire signed [(dw+1):0] zsel = mux[(2*dw)-1:(dw-2)];
always @(posedge clk) begin
    zr <= `SAT(zsel, dw+1, dw);   // 饱和到 dw 位
    mux_r <= mux;
end
assign z = zr[dw:1];
assign z_all = mux_r;             // 同时给一份无舍入的全精度结果
```

`complex_mul_flat` 的并行版本——四个乘积项各用一个乘法器（[dsp/complex_mul_flat.v:32-43](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/complex_mul_flat.v#L32-L43)）：

```verilog
always @(posedge clk) begin
    AC <= x_I * y_I;  BD <= x_Q * y_Q;
    AD <= x_I * y_Q;  BC <= x_Q * y_I;
    z_I_all_i <= AC - BD;
    z_Q_all_i <= AD + BC + 1;
    I_small <= `SAT(z_I_sel, 19, 18);
    Q_small <= `SAT(z_Q_sel, 19, 18);
end
```

两者对比：

| 维度 | `complex_mul` | `complex_mul_flat` |
| --- | --- | --- |
| 接口 | IQ **时分交织**（`x/y/z` 各一路 + `iq` 标志） | IQ **并行**（`x_I/x_Q/y_I/y_Q/z_I/z_Q`） |
| 乘法器数 | 2 | 4（AC/BD/AD/BC 各一） |
| 吞吐 | 每 2 拍 1 个复数结果 | 每 1 拍 1 个复数结果 |
| 延迟 | 4 拍（`gate_sr[3]`） | 3 拍（`gate_sr[2]`，见 [dsp/complex_mul_flat.v:55-57](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/complex_mul_flat.v#L55-L57)） |
| 适用 | 乘法器紧张、数据本就交织 | 要全速、端口够用 |

#### 4.4.4 代码实践

1. **实践目标**：跑通两种复乘的自检，理解交织与并行的差异。
2. **操作步骤**：
   - `make -C dsp complex_mul_check`
   - `make -C dsp complex_mul_flat_check`
3. **需要观察的现象**：两个目标各打印 `PASS`/`FAIL`，并打出逐拍的实际值、参考值与误差。
4. **预期结果**：
   - `complex_mul_tb` 在软件里同步计算参考 \(ac-bd\)、\(ad+bc\)，与 DUT 输出比较，容差 \(\pm 2^{dw-1}\)（[dsp/complex_mul_tb.v:55-65](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/complex_mul_tb.v#L55-L65)），应 `PASS`。
   - `complex_mul_flat_tb` 用 `cc%4==0` 作 `gate_in`、随机 IQ，比较 `z_I/z_Q` 与参考（[dsp/complex_mul_flat_tb.v:29-65](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/complex_mul_flat_tb.v#L29-L65)），应 `PASS`。
   - 运行结果**待本地验证**。
5. **改参数观察**：把 `complex_mul_tb` 里的 `parameter dw = 16`（[dsp/complex_mul_tb.v:25](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/complex_mul_tb.v#L25)）改成 18，重跑 `complex_mul_check`，看是否仍 `PASS`（注意 `complex_mul_flat` 端口固定 18 位，不能这样改）。

#### 4.4.5 小练习与答案

- **练习 1**：`complex_mul` 为什么 `sumq` 里有个看似奇怪的 `+1`？
  - **答案**：把截断的平均误差偏置从 \(-1/2\) 结果位降到 \(-1/4\) 结果位，减小系统性的直流偏差（[dsp/complex_mul.v:49-52](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/complex_mul.v#L49-L52)）。
- **练习 2**：什么情况下 `SAT` 宏会真的把结果钳位？
  - **答案**：当乘积的高位不是合法符号扩展时——典型是两个接近满量程负值的数相乘，结果超出 \([-1,1)\)（如 \((1+j)(1-j)=2\)），见 [dsp/complex_mul.v:14-16](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/complex_mul.v#L14-L16)。

---

## 5. 综合实践：搭一条「DDS 本振 → IQ 混频」的数据通路

把本讲四个模块串起来，画一张完整的数据通路框图，并做位宽预算：

1. **画图**：`rot_dds`（由 `ph_acc`+CORDIC 构成）同时输出 `cosa`、`sina` 两路 18 位本振。ADC 数据 `adcf`（16 位）分两路：一路与 `cosa` 进 `mixer` 得 I 路，一路与 `sina` 进 `mixer` 得 Q 路（这正是 [dsp/iq_mixer_multichannel.v](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/iq_mixer_multichannel.v) 的做法，见其 [第 26-52 行](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/iq_mixer_multichannel.v#L26-L52)）。
2. **位宽预算**：写出 `mixer` 默认参数下 I/Q 输出位宽（\(dwi+davr=20\) 位）。
3. **接复乘**：若要把这对 (I,Q) 与某个复数参考相乘做相位旋转，应选 `complex_mul`（交织）还是 `complex_mul_flat`（并行）？说明理由。
4. **验证**：跑 `make -C dsp rot_dds_check complex_mul_check complex_mul_flat_check` 与 `make -C dsp cic_multichannel_check`（后者间接用到了 `mixer`），全部应 `PASS`。

> 参考答案要点：第 2 问 20 位；第 3 问——若 I/Q 已经是两条并行流，用 `complex_mul_flat` 全速更直接；若数据已被交织成一条流，用 `complex_mul` 省乘法器。这条「DDS→混频→复乘」链正是下一讲 [u3-l3 下变频/上变频](u3-l3-downconvert-upconvert.md)的骨架。

## 6. 本讲小结

- `ph_acc` 用「20 位粗计数器 + 12 位细残差计数器 + 模数」精确合成任意有理频率的相位，控制字被刻意拆成可塞进 32 位总线的几段，便于原子更新。
- `rot_dds = ph_acc + CORDIC`（旋转模式），是 Bedrock 的本振发生器；`lo_amp≈2^17/1.64676` 用来抵消 CORDIC 固有增益、避免溢出。
- `mixer` 是定点实数混频器；**输出位宽 \(=dwi+davr\)**，`davr` 直接调输出位宽，`NUM_DROP_BITS` 只平移截取窗口（用冗余符号位换低位精度），不改位宽。
- `complex_mul`（IQ 交织、2 乘法器、2 拍 1 结果、4 拍延迟）与 `complex_mul_flat`（IQ 并行、4 乘法器、1 拍 1 结果、3 拍延迟）是同一复乘的两种资源/吞吐权衡，均带 `SAT` 饱和与 `+1` 偏置校正。
- 本仓库没有 `mixer_tb`，`mixer` 经 `cic_multichannel_tb` 间接测试；`rot_dds`/`complex_mul`/`complex_mul_flat` 都有独立 `_check` 目标可直接跑。
- 定点截位与饱和是贯穿本讲的主题：乘法翻倍位宽后，必须在「精度」与「溢出风险」之间取舍。

## 7. 下一步学习建议

- 下一讲 [u3-l3 下变频与上变频](u3-l3-downconvert-upconvert.md) 会把本讲的 DDS、mixer、复乘组装成完整的 IQ 下变频（`fdownconvert`）与上变频（`ssb_out`）链路，并解释「为何输出是半速率 IQ」。
- 想深入了解滤波与速率变换，接着读 [u3-l4 CIC、FIR/IIR 滤波器与抽取插值](u3-l4-filters.md)，其中 CIC 正好接在 `mixer` 之后做平均抽取。
- 想看本振的另一种实现，可读 `dsp/lo_lut/`（查表法 LO）与 `dsp/second_if_out.v`。
- 建议顺手通读 [dsp/README.md](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/README.md) 的「Digital down-conversion」一节，把本讲模块放进整条 DSP 链的上下文里。
