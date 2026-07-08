# 定点数运算与饱和保护

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚全库统一的**定点标度约定**：12bit 传感器、16bit 有符号运算、角度 `0~4095 ↔ 0~2π`、三角函数 `−1~+1 ↔ −16384~+16384`。
- 在不同模块之间**推算定点缩放关系**，例如看懂 Park 变换为什么取 `ide[31:16]`、这一刀切下来带来一个 \(1/4\) 的常数增益。
- 理解「移位加法近似 √3」「更宽中间位宽做饱和截断（`protect_add`/`protect_mul`）」这两种工程取舍的动机和代价。
- 领会贯穿全库的核心思想：**FOC 是线性系统，系数不必精确，常数增益与近似误差统统交给 PID 调参吸收**——从而看懂为何代码里那么多系数和教科书公式「对不上」。

本讲是专家层的「数学总复习」，它不引入新模块，而是把前面 Clark / Park / PI 三讲里散落的定点细节统一起来，回答一个最让初学者困惑的问题：*这些代码里的数字怎么都不按公式来，凭什么还能正常控电机？*

## 2. 前置知识

本讲假定你已经读过以下内容（术语不再重复解释）：

- **u2-l3 Clark 变换**：知道 `clark_tr` 把 ia/ib/ic 变成 iα/iβ，且公式被整体放大了 2 倍。
- **u2-l4 Park 变换与 sincos**：知道 `park_tr` 用旋转把 iα/iβ 变成 id/iq，`sincos` 把角度映射成 `−16384~+16384` 的定点三角函数。
- **u2-l5 PI 控制器**：知道 `pi_controller` 用 5 级流水线算 `value = Kp·e + Ki·Σe`，最后取 `value[31:16]`。

此外需要三个底层概念：

- **定点数 (fixed-point number)**：用整数来「假装」小数。约好一个隐含的小数点位置，比如约定「整数除以 \(2^{14}\) 才是真值」，那么整数 16384 就代表真值 \(1.0\)。Verilog 里没有小数点，所有定点数本质上都是整数，靠**约定**和**对齐**来解释。
- **有符号数的位宽与饱和 (saturation)**：两个 16 位有符号数相乘，结果最多 32 位；两个 32 位有符号数相加，结果可能需要 33 位才不溢出。一旦结果塞回原位宽却超出范围，就会「回绕 (wrap-around)」——一个本该很大的正数变成负数。**饱和**就是检测到超出范围时，强制钉在最大/最小值上，避免回绕。
- **线性系统 (linear system)**：若把所有系数同时乘以一个常数 \(k\)，输入输出关系只是整体被放大 \(k\) 倍，系统行为不变（只是「单位」变了）。这是后面「系数不必精确」的数学根基。

## 3. 本讲源码地图

本讲聚焦三个最小模块，外加一个把它们串起来的总论：

| 文件 | 模块 | 在本讲扮演的角色 |
|------|------|------------------|
| [RTL/foc/clark_tr.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/clark_tr.v) | `clark_tr` | 「移位加法近似 √3」与「放大 2 倍消 1/2」的样本 |
| [RTL/foc/park_tr.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/park_tr.v) | `park_tr` | 「乘法截断高位 `[31:16]`」带来 \(1/4\) 增益的样本 |
| [RTL/foc/pi_controller.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/pi_controller.v) | `pi_controller` | `protect_add`/`protect_mul` 饱和保护与积分限幅 |
| [RTL/foc/foc_top.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v) | `foc_top` | 串起三模块的连线、电流重构、角度换算的定点语境 |

辅助理解（非本讲重点，但会引用其约定）：

| 文件 | 说明 |
|------|------|
| [RTL/foc/sincos.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/sincos.v) | 给出「角度 0~4095 ↔ 0~2π、−1~1 ↔ −16384~16384」的定点约定来源 |
| [RTL/foc/cartesian2polar.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/cartesian2polar.v) | 综合实践表里会用到它的输入输出位宽 |

---

## 4. 核心概念与源码讲解

### 4.1 全库定点标度约定

#### 4.1.1 概念说明

Verilog 里没有浮点数（综合浮点单元代价极高、FPGA 上不划算），所以全库统一用**整数定点**。问题在于：电流、角度、三角函数三者的「单位」完全不同，却都要塞进有限的整数寄存器里。作者的做法是给每一类量约定一个隐含的缩放因子。只要你能在脑子里把这些约定列成一张表，就能在任何两个模块之间推算「乘出来以后小数点该落在哪」。

#### 4.1.2 核心流程

全库的定点标度可以归纳为四条约定：

1. **角度类**（电角度 ψ、机械角度 φ、初始角度 Φ、极坐标角度）：12 位**无符号**，`0~4095` 一圈，对应 \(0~2\pi\)。即 \(1024 \leftrightarrow 90°\)、\(2048 \leftrightarrow 180°\)、\(3072 \leftrightarrow 270°\)。
2. **电流 / 电压类**（ia/ib/ic、iα/iβ、id/iq、vd/vq、i_aim/iq_aim）：16 位**有符号**，单位是「原始 ADC/计数单位」，无明确的安培/伏特，其绝对刻度由 PID 吸收。
3. **三角函数**（sin_psi、cos_psi）：16 位**有符号**，\(-1~+1\) 映射到 \(-16384~+16384\)，缩放因子是 \(2^{14}\)。这条约定写在 sincos 与 park 的注释里。
4. **PI 增益**（Kp、Ki）：31 位（端口 `[30:0]`），是**无符号正数**，当作带定点小数分辨率的大整数，运行时可调。

这四条约定一旦确立，跨模块的位宽对接就只是「对齐小数点」的算术题。

#### 4.1.3 源码精读

三角函数的定点约定，直接写在 `sincos` 的端口注释里——这是全库「\(-1~1 \leftrightarrow -16384~16384\)」的**权威出处**：

[RTL/foc/sincos.v:7-17](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/sincos.v#L7-L17) —— 注释写明「角度 0~π 被映射为 0~4095」「`-1~+1` 被映射为 `-16384~+16384`」，`o_sin/o_cos` 是 16 位有符号。

电流类信号的 16 位有符号约定，写在 `foc_top` 的端口声明里：

[RTL/foc/foc_top.v:38-42](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L38-L42) —— `id`、`iq`、`id_aim`、`iq_aim` 全是 `signed [15:0]`，注释强调「可正可负」。

PI 增益的 31 位无符号约定：

[RTL/foc/foc_top.v:23-24](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L23-L24) —— `Kp`、`Ki` 是 `[30:0]`（31 位无符号）。

#### 4.1.4 代码实践

**实践目标**：把上面四条约定固化为一张速查表，为后续每个模块的缩放推算打基础。

**操作步骤**：

1. 取一张纸或打开表格，列四行：角度类、电流/电压类、三角函数类、PI 增益类。
2. 每行填：位宽、有/无符号、数值范围、对应的物理含义、隐含缩放因子。
3. 重点标出三角函数行：范围 \(-16384~+16384\)、缩放因子 \(2^{14}\)，即「整数真值 \(=\) 寄存器值 \(/ 2^{14}\)」。

**预期结果**：得到一张类似下表的速查表（综合实践里你会把它扩展到每个模块的输入输出）：

| 量 | 位宽 | 符号 | 范围 | 缩放因子 |
|----|------|------|------|----------|
| 角度 ψ/φ | 12 | 无 | 0~4095 | \(2^{11} \leftrightarrow 2\pi\) |
| 电流/电压 | 16 | 有 | −32768~32767 | 原始单位（无固定小数点） |
| sin/cos | 16 | 有 | −16384~16384 | \(2^{14} \leftrightarrow 1.0\) |
| Kp/Ki | 31 | 无 | 0~\(2^{31}-1\) | 带定点小数的大整数 |

#### 4.1.5 小练习与答案

**练习 1**：若 `cos_psi` 寄存器里读出整数 8192，它代表真值多少？

**答案**：\(8192 / 2^{14} = 8192 / 16384 = 0.5\)，即 \(\cos\psi = 0.5\)（\(\psi = 60°\)）。

**练习 2**：为什么角度用 12 位无符号，而电流用 16 位有符号？

**答案**：角度天然是 \([0, 2\pi)\) 的非负量（一圈循环），用 12 位无符号正好 \(2^{12}=4096\) 个刻度、且能靠 12 位截断自动完成 mod 4096；电流可正可负（流入/流出电机），必须用有符号补码，且动态范围大，故用 16 位。

---

### 4.2 clark_tr：移位加法近似 √3 与「放大 2 倍消 1/2」

#### 4.2.1 概念说明

教科书上的**等幅 Clarke 变换**是：

\[
I_\alpha = I_a - \tfrac{1}{2}I_b - \tfrac{1}{2}I_c,\qquad
I_\beta = \tfrac{\sqrt{3}}{2}(I_b - I_c)
\]

直接照搬到整数硬件上有两个麻烦：一是系数 \(1/2\) 在整数除法里会**截断丢精度**；二是系数 \(\sqrt{3}/2\) 既不是整数也不是 2 的整数次幂，没法用一次乘法或移位精确得到。作者的解法分两步：(a) 把整个变换**整体乘以 2**，消掉 \(1/2\)；(b) 用「**符号扩展算术右移**」把 √3 拆成若干个 \(2\) 的整数次幂倒数之和来**逼近**。

#### 4.2.2 核心流程

放大 2 倍后的公式变成全整数（α 通路）和含 √3（β 通路）：

\[
2I_\alpha = 2I_a - I_b - I_c,\qquad
2I_\beta = \sqrt{3}\,(I_b - I_c)
\]

- α 通路：\(2I_a\) 是左移 1 位，减去 \(I_b+I_c\) 即可，全是整数加减，零误差。
- β 通路：令 `bmc = Ib − Ic`，需要算 \(\sqrt{3}\cdot\text{bmc}\)。把 √3 写成 9 个 \(2\) 的整数次幂倒数之和：

\[
\sqrt{3} \approx 1 + \tfrac12 + \tfrac18 + \tfrac1{16} + \tfrac1{32} + \tfrac1{128} + \tfrac1{256} + \tfrac1{1024} + \tfrac1{2048}
\]

每个 \(1/2^k\) 项就是「把 `bmc` 算术右移 \(k\) 位」。算术右移在 Verilog 里用「符号扩展 + 截取低位」实现：`$signed({{(k){bmc[15]}}, bmc[15:k]})` 把符号位 `bmc[15]` 复制 \(k\) 份拼到高位，再取 `bmc[15:k]`，等价于除以 \(2^k\) 且向负无穷取整（保留符号）。

#### 4.2.3 源码精读

α 通路的第一级流水线——左移和加减：

[RTL/foc/clark_tr.v:32-34](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/clark_tr.v#L32-L34) —— `ax2_s1 = i_ia<<1`（即 \(2I_a\)）、`bmc_s1 = i_ib - i_ic`、`bpc_s1 = i_ib + i_ic`，全是整数运算。

α 通路的第二级——一行完成 \(2I_\alpha = 2I_a - (I_b+I_c)\)：

[RTL/foc/clark_tr.v:43](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/clark_tr.v#L43) —— `ialpha_s2 = ax2_s1 - bpc_s1`。

β 通路的 9 项移位近似，分成 3 组寄存器（`i_beta1/2/3_s2`）：

[RTL/foc/clark_tr.v:44-52](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/clark_tr.v#L44-L52) —— 每一项 `$signed({{N{bmc_s1[15]}}, bmc_s1[15:k]})` 就是 `bmc_s1 >> k`（算术右移）。三组分别对应 \((1, 1/2, 1/8)\)、\((1/16, 1/32, 1/128)\)、\((1/256, 1/1024, 1/2048)\)。

最后把三组相加得到 \(2I_\beta\)：

[RTL/foc/clark_tr.v:63](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/clark_tr.v#L63) —— `o_ibeta <= i_beta1_s2 + i_beta2_s2 + i_beta3_s2`。

注意端口注释里刻意把输入范围限定在 `-8191~8191`：

[RTL/foc/clark_tr.v:13](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/clark_tr.v#L13) —— `// range -8191 ~ 8191`。因为 α 通路会把 \(2I_a\) 放大、再叠加三相，最坏情况约 \(4\times\) 幅值，限定输入到 \(±8191\) 正好让输出落在 16 位有符号 (\(±32768\)) 内不溢出。

#### 4.2.4 代码实践

**实践目标**：验证 9 项移位之和确实逼近 √3，并量化误差。

**操作步骤**：

1. 把第 4.2.3 节列出的 9 项系数写成二进制分数：\(1, 1/2, 1/8, 1/16, 1/32, 1/128, 1/256, 1/1024, 1/2048\)。
2. 求和 \(S = 1 + 0.5 + 0.125 + 0.0625 + 0.03125 + 0.0078125 + 0.00390625 + 0.0009765625 + 0.00048828125\)。
3. 与 \(\sqrt{3} = 1.7320508\ldots\) 比较，算相对误差。

**预期结果**：\(S = 1.73193359375\)，绝对误差约 \(1.17\times10^{-4}\)，相对误差约 \(0.0068\%\)。这个精度远高于 12 位 ADC 的量化噪声，被 PI 轻松吃掉（详见 4.5）。

> 注：若你在本地把 `bmc_s1` 设为某固定值（如 1000）跑仿真，`o_ibeta` 应接近 \(1000 \times 1.7319 \approx 1732\)，可用来眼检。

#### 4.2.5 小练习与答案

**练习 1**：为什么作者不直接写 `i_ib * 1732 >> 10`（用一次乘法近似 √3）？

**答案**：也可以，且精度更高。但作者选择「移位加法」可能是为了在某些没有高效硬件乘法器的小 FPGA 上避免乘法、或纯粹是风格选择。两种写法的常数增益误差最终都交给 PI 吸收，功能等价。

**练习 2**：若把 β 通路的最后一项 \(1/2048\)（`bmc_s1[15:11]`）删掉，相对误差会变多少？

**答案**：少掉 \(1/2048 \approx 4.88\times10^{-4}\)，和约变为 \(1.7314453125\)，相对误差升到约 \(0.035\%\)。仍然很小，说明这项主要是「锦上添花」。

---

### 4.3 park_tr：乘法截断 `[31:16]` 与 \(1/4\) 增益

#### 4.3.1 概念说明

Park 变换的公式是 \(I_d = I_\alpha\cos\psi + I_\beta\sin\psi\)、\(I_q = I_\beta\cos\psi - I_\alpha\sin\psi\)。它要做 4 次「电流 × 三角函数」的乘法。问题是：电流是 16 位有符号（单位记作 \(U\)），三角函数也是 16 位有符号但缩放因子是 \(2^{14}\)（因为 \(-1~1 \leftrightarrow -16384~16384\)）。两个 16 位数相乘得 32 位，**小数点（缩放因子）该怎么对齐？** 作者用一个统一的「取高 16 位」(`[31:16]`) 来收口，这一刀会带来一个固定的 \(1/4\) 增益。

#### 4.3.2 核心流程

按缩放因子追踪一次乘法：

- `i_ialpha`：整数，真值 \(=\) 寄存器值，缩放因子 \(1\)（单位 \(U\)）。
- `cos_psi`：整数，真值 \(=\) 寄存器值 \(/2^{14}\)，缩放因子 \(2^{14}\)。
- 乘积 `alpha_cos = i_ialpha * cos_psi`：32 位整数，缩放因子 \(1 \times 2^{14} = 2^{14}\)（单位 \(U\)）。
- 求和 `ide = alpha_cos + beta_sin`：仍缩放 \(2^{14}\)。
- 取高位 `o_id = ide[31:16]`：相当于除以 \(2^{16}\)，缩放因子变为 \(2^{14}/2^{16} = 2^{-2}\)。

所以最终：

\[
\text{id}_{\text{寄存器}} = \frac{I_\alpha\cos\psi + I_\beta\sin\psi}{4}
\]

即输出比「理论 id」整体缩小到 \(1/4\)。这是一个**常数增益**（与角度无关），完全可被 PI 的 Kp/Ki 补偿（PI 只要把增益调大 4 倍即可），对线性系统无害。

#### 4.3.3 源码精读

三角函数的缩放约定再次确认（park 内部声明的 wire 注释）：

[RTL/foc/park_tr.v:19](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/park_tr.v#L19) —— `wire signed [15:0] sin_psi, cos_psi; // -1~+1 is mapped to -16384~+16384`，缩放因子 \(2^{14}\)。

第一级流水线并行算 4 个 32 位乘积：

[RTL/foc/park_tr.v:42-45](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/park_tr.v#L42-L45) —— `alpha_cos = i_ialpha * cos_psi` 等，16×16→32 位乘法。

组合逻辑做加减得到 32 位的 `ide`/`iqe`：

[RTL/foc/park_tr.v:24-25](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/park_tr.v#L24-L25) —— `ide = alpha_cos + beta_sin`、`iqe = beta_cos - alpha_sin`。

关键的一刀——取高 16 位作为 16 位有符号输出：

[RTL/foc/park_tr.v:54-55](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/park_tr.v#L54-L55) —— `o_id <= ide[31:16]`、`o_iq <= iqe[31:16]`。这就是 \(1/4\) 增益的来源。

#### 4.3.4 代码实践

**实践目标**：用 tb_clark_park_tr 的波形验证「Park 把交流坍缩成直流」的同时，确认定点缩放关系。

**操作步骤**：

1. 按 u1-l4 的方法运行 `SIM/tb_clark_park_tr_run_iverilog.bat`，用 gtkwave 打开 `dump.vcd`。
2. 把 `i_ialpha`、`i_ibeta`、`id`、`iq` 设为 Signed Decimal → Analog。
3. 在波形稳定段（ψ 跟随 θ 之后），读一组同时刻的数值：记下 `i_ialpha`、`cos_psi`（若未 dump，可看 `u_sincos/o_cos`）和 `id`。
4. 手算 \(\text{id}_{\text{理论}} = (i_\alpha\cos\psi + i_\beta\sin\psi)\)，再算 \(\text{id}_{\text{理论}}/4\)，与波形里的 `id` 比较。

**预期结果**：波形里的 `id` 约等于手算的 \(\text{id}_{\text{理论}}/4\)，从而坐实 `[31:16]` 带来的 \(1/4\) 缩放。同时观察到 `id`/`iq` 从正弦波「坍缩」成接近常数（Park 解调成功），与 u2-l4 的结论一致。

> 待本地验证：具体数值取决于 testbench 给定的电流幅值与角度，重点看「比例关系」是否为 1∶4。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `[31:16]` 改成 `[31:15]`（取高 17 位）会怎样？

**答案**：输出会变成 17 位，无法塞进 16 位端口；若强行截成 16 位会破坏符号。更本质的是，取 `[31:16]` 是「干净地除以 \(2^{16}\)」，改成 17 位就引入了非 2 的整数次幂的缩放，失去定点对齐的便利。

**练习 2**：Park 的 \(1/4\) 增益和 Clark 的 \(2\) 倍增益叠加后，从 ia 到 id 总增益是多少？

**答案**：Clark 放大 \(2\) 倍、Park 缩小到 \(1/4\)，叠加为 \(2 \times 1/4 = 1/2\)。即 `id` 寄存器值约为「理论 id」的 \(1/2\)（再乘上旋转矩阵本身的系数）。这个固定的 \(1/2\)（以及电流重构链里的常数）统统由 PI 的 Kp/Ki 一并吸收。

---

### 4.4 pi_controller：`protect_add`/`protect_mul` 饱和保护

#### 4.4.1 概念说明

前两节处理的是「**系数不准**」的问题（误差交给 PI）。本节处理另一个更危险的问题：**运算溢出**。PI 控制器里要做两次乘法（`Kp·pdelta`、`Ki·Σpdelta`）和多次加法（积分累加、P+I 求和）。其中积分项 `idelta = Σpdelta` 会**持续累加**，只要误差长期不为零就可能无限增长；两个 32 位有符号数相加也可能超出 32 位范围。一旦溢出，补码会**回绕**——本该很大的正值变成负值，电机猛地反转，非常危险。

作者的解法是两个饱和函数：`protect_add`（饱和加法）和 `protect_mul`（饱和乘法），它们用**更宽的中间位宽**做运算，一旦检测到超出 32 位有符号范围 \([-2^{31}, 2^{31}-1]\)，就**钉死**在边界上。其中 `protect_add` 还兼任积分项的 **anti-windup（防积分饱和）**。

#### 4.4.2 核心流程

PI 的数据流（5 级流水线，详见 u2-l5）与饱和保护的接入点：

\[
\begin{aligned}
\text{pdelta} &= \text{i\_aim} - \text{i\_real} & &\text{(拍0，求误差)}\\
\text{kpdelta1} &= \text{protect\_mul}(\text{pdelta},\, \text{Kp}) & &\text{(拍1，比例项)}\\
\text{idelta} &= \text{protect\_add}(\text{idelta},\, \text{pdelta}) & &\text{(拍1，积分累加，带限幅)}\\
\text{kidelta} &= \text{protect\_mul}(\text{idelta},\, \text{Ki}) & &\text{(拍2，积分项)}\\
\text{kpidelta} &= \text{protect\_add}(\text{kpdelta},\, \text{kidelta}) & &\text{(拍3，P+I 求和)}\\
\text{value} &= \text{kpidelta},\quad \text{o\_value} = \text{value}[31:16] & &\text{(拍4，取高位输出)}
\end{aligned}
\]

两个饱和函数的共同套路：(a) 用更宽的中间变量算；(b) 与 \(\pm(2^{31}-1)\) 比较；(c) 超出则钉死，否则取低 32 位。

- `protect_add(a,b)`：`a,b` 都是 32 位有符号。先把它们符号扩展成 33 位再相加（两数之和最多需要 33 位），存进 `signed [32:0] y`；若 `y > 2^{31}-1` 返回 `2^{31}-1`，若 `y < -(2^{31}-1)` 返回 `-(2^{31}-1)`，否则返回 `y[31:0]`。
- `protect_mul(a,b)`：`a,b` 都是 32 位有符号，乘积最多 64 位，但这里用 `signed [56:0] y`（57 位）承接——这是历史遗留位宽（函数上方被注释掉的旧签名显示 `b` 原本是 `[24:0]` 即 25 位，\(32+25=57\)），如今两个操作数虽都升级到 32 位，但因实际数值（Kp/Ki 与误差的乘积）远达不到满量程，57 位仍足够不溢出。

#### 4.4.3 源码精读

输出取高位——注意 PI 的最终输出也是定点取高位，`o_value = value[31:16]`（除以 \(2^{16}\)），让 Kp/Ki 成为带定点小数分辨率的大整数增益：

[RTL/foc/pi_controller.v:25](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/pi_controller.v#L25) —— `assign o_value = value[31:16];`。

`protect_add`：33 位中间值、上下钉死在 \(\pm(2^{31}-1)\)：

[RTL/foc/pi_controller.v:28-42](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/pi_controller.v#L28-L42) —— 注意第 30 行 `reg signed [32:0] y;`（33 位），第 34 行把 `a`、`b` 符号扩展成 33 位再相加，第 35-40 行做三段式饱和判断。

`protect_mul`：57 位中间值（注释里保留的旧签名 `input signed [24:0] b` 解释了 57 = 32+25 的来历）：

[RTL/foc/pi_controller.v:45-59](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/pi_controller.v#L45-L59) —— 第 47 行 `reg signed [56:0] y;`（57 位），第 48 行注释 `input logic signed [24:0] b` 揭示历史位宽。

误差计算与符号扩展（把 16 位目标/实测符号扩展到 32 位再相减）：

[RTL/foc/pi_controller.v:87](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/pi_controller.v#L87) —— `pdelta <= $signed({{16{i_aim[15]}},i_aim}) - $signed({{16{i_real[15]}},i_real});`。

积分累加用 `protect_add`（这就是 anti-windup 的落点）：

[RTL/foc/pi_controller.v:99-100](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/pi_controller.v#L99-L100) —— `kpdelta1 <= protect_mul(pdelta, ...)`、`idelta <= protect_add(idelta, pdelta)`。`idelta` 是唯一跨控制周期保留的状态，靠 `protect_add` 限幅防止积分爆表。

P+I 求和也用 `protect_add`：

[RTL/foc/pi_controller.v:120](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/pi_controller.v#L120) —— `kpidelta <= protect_add(kpdelta, kidelta);`。

注意第 128 行的注释「modified at 20230609, now it is a standard PID」——最终输出不再做饱和，直接 `value <= kpidelta`（因为前面每一步都已饱和，`kpidelta` 必在 32 位范围内）：

[RTL/foc/pi_controller.v:128](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/pi_controller.v#L128) —— `value <= kpidelta;`，被注释掉的旧写法是 `protect_add(value, kpidelta)`（曾经的增量式 PID）。

在 `foc_top` 里被双例化为 d 轴和 q 轴两个 PI，共用同一对 Kp/Ki：

[RTL/foc/foc_top.v:160-170](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L160-L170) —— `u_id_pi` 例化，`i_Kp/i_Ki` 接顶层的 `Kp/Ki`，`o_value` 接 `vd`。

#### 4.4.4 代码实践

**实践目标**：用一个最小场景手算 `protect_add`/`protect_mul` 的行为，理解「钉死」如何避免回绕。

**操作步骤**：

1. **protect_add 手算**：设 `a = 32'sd2000000000`（约 \(2\times10^9\)）、`b = 32'sd2000000000`。真实和为 \(4\times10^9\)，超出 32 位有符号上界 \(2^{31}-1 \approx 2.147\times10^9\)。
   - 求和后 `y = 4000000000`，比较 `y > 2147483647` 成立 → 返回 `2147483647`（钉死）。
   - 若**没有**饱和、直接取 `y[31:0]`，则 \(4000000000 - 2^{32} = -294967296\)，变成负数——这就是回绕灾难。
2. **protect_mul 手算**：设 `pdelta = 1000`、`Kp = 100000`，乘积 \(10^8\)，远小于 \(2^{31}\)，函数原样返回 `100000000`。
3. 在脑中把 `idelta` 想象成「一直累加 pdelta」：只要误差长期同号，`idelta` 会一路增长，直到撞上 `protect_add` 的上界 \(2^{31}-1\) 就停下——这就是 anti-windup。

**预期结果**：你能向自己解释清楚「为什么 `protect_add` 的 33 位中间值能保证不溢出，而 57 位的 `protect_mul` 在当前参数下也安全」。

> 待本地验证：可写一个只例化 `pi_controller` 的小 testbench，给 `i_aim` 恒定、`i_real` 恒为 0，观察 `idelta`（用 `$dumpvars` 跟踪内部信号）是否在长期运行后稳定在上界附近而不回绕。

#### 4.4.5 小练习与答案

**练习 1**：`protect_add` 的中间值为什么是 33 位而不是 32 位？

**答案**：两个 32 位有符号数相加，结果范围是 \([-2^{32}+2,\ 2^{32}-2]\)，至少需要 33 位（含 1 位符号）才能完整表示而不丢失。32 位会直接回绕，就失去了「先算准、再判断」的前提。

**练习 2**：`protect_mul` 用 57 位中间值，两个 32 位输入相乘最坏需要 64 位，57 位会不会不够？

**答案**：理论上两个 32 位有符号满量程相乘需要 64 位（含符号 65 位），57 位确实放不下最坏情况。但本模块里被乘数 `pdelta`/`idelta` 是 16 位电流误差的衍生量、乘数 `Kp`/`Ki` 是 31 位无符号，实际乘积远低于 \(2^{57}\)，所以 57 位在当前参数下安全。这是「按实际值域设计位宽」的工程取舍——57 这个具体数字来自旧签名（\(32+25\)）的历史残留。

**练习 3**：为什么最终 `value <= kpidelta`（第 128 行）不再套一层 `protect_add`？

**答案**：因为 `kpidelta = protect_add(kpdelta, kidelta)`，而 `kpdelta`、`kidelta` 本身都是 `protect_mul` 的输出（已被钉在 32 位范围内），两个 32 位范围内之值经 `protect_add` 后 `kpidelta` 必仍在 32 位范围内，再套饱和是冗余。注释里那句「now it is a standard PID」也说明作者从「增量式（需对 value 累加并饱和）」改成了「位置式（直接赋值）」。

---

### 4.5 线性系统：系数不必精确，误差交给 PID

#### 4.5.1 概念说明

读到这里你可能有强烈的不适：Clark 公式放大了 2 倍、√3 是近似值、Park 切出了 \(1/4\) 增益、电流重构还残留未知常数 \(k\)（见 u2-l2）……这些「不精确」叠在一起，凭什么还能精确控电流？

答案是 FOC 电流环的**核心数学性质：它（在小信号、稳态意义下）是一个线性系统**。对线性系统而言：

- 所有这些常数增益（2 倍、\(1/4\)、\(k\)）会**乘在一起变成一个总的常数因子** \(G\)，作用在前向通道上。
- PI 控制器的闭环直流增益趋近于 \(1\)（只要 Kp/Ki 足够大且系统稳定），前向通道的常数因子 \(G\) 会被 PI 的 \(1/G\) 自动抵消。
- 换句话说：**你不需要知道 \(G\) 到底是多少，只要它是「常数」（不随角度、电流变化），调 Kp/Ki 就能把它消化掉。**

而非线性的部分（角度 mod 4096 的折叠、饱和保护的钉死）则被**严格保留**——这些是刻意设计的、不能用 PID 吸收的。

#### 4.5.2 核心流程

把整条前向通道的「线性常数增益」连起来：

\[
G = \underbrace{k_{\text{电流重构}}}_{\text{u2-l2 残留}} \times
\underbrace{2}_{\text{Clark 放大}} \times
\underbrace{\tfrac14}_{\text{Park 取高位}} \times
\underbrace{G_{\text{反Park/SVPWM}}}_{\text{后级常数}}
\]

这个 \(G\) 是个常数。闭环传递函数（简化）为：

\[
\frac{I_q}{I_{q,\text{aim}}} = \frac{G\,(K_p + K_i/s)}{1 + G\,(K_p + K_i/s)}
\]

当 \(s\to 0\)（稳态），开环增益 \(G\,(K_p + K_i/s) \to \infty\)（积分项 \(K_i/s\) 起作用），闭环增益趋于 1。所以**只要开环增益足够大，\(G\) 取什么值都不影响稳态精度**——这就是「系数不必精确」的数学根源。

而 `protect_add`/`protect_mul` 的饱和属于**非线性**，它只在异常（误差巨大）时才介入，正常工作时线性区运行，不影响上述结论。这正是 4.4 节的饱和保护与 4.5 节的线性思想能共存的原因。

#### 4.5.3 源码精读

PI 增益作为运行时可调的「大整数」，端口在顶层声明：

[RTL/foc/foc_top.v:23-24](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L23-L24) —— `Kp`、`Ki` 是 `[30:0]`，从外部输入，运行中可改。

PI 内部把 Kp/Ki 当作带定点小数分辨率的大整数（乘完后 `[31:16]` 取高位，等效于 Kp/Ki 可以表达很细的小数增益）：

[RTL/foc/pi_controller.v:99](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/pi_controller.v#L99) —— `kpdelta1 <= protect_mul(pdelta, $signed({1'h0, Kp0}));`，Kp0 是 31 位无符号，前置 0 拼成 32 位正数参与有符号乘法。

电流重构里残留的常数增益 \(k\)（来自 ADC 反向放大+偏置电路的未知比例），同样不精确计算，留给 PID：

[RTL/foc/foc_top.v:105](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L105) —— `ia <= $signed( {4'b0, adc_b} + {4'b0, adc_c} - {3'b0, adc_a, 1'b0} );`，注释 `// Ia = ADCb + ADCc - 2*ADCa`。这里只保留了 KCL 的结构，未补偿硬件的绝对增益。

角度换算把机械角度折算成电角度，靠 12 位截断自动完成 mod \(2\pi\)——这是**保留**的非线性（周期性），不能用 PID 吸收，必须精确：

[RTL/foc/foc_top.v:87](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L87) —— `psi <= {4'h0, POLE_PAIR} * (phi - init_phi);`，乘积高位被 12 位寄存器截断，天然 mod 4096。

#### 4.5.4 代码实践

**实践目标**：用「思想实验」验证「线性常数增益不影响稳态精度」。

**操作步骤**：

1. 假设你把 `clark_tr` 里 α 通路的 `ax2_s1 <= i_ia << 1`（放大 2 倍）改成 `ax2_s1 <= i_ia << 2`（放大 4 倍），即把 α 通路增益再翻倍。
2. 推断：iα、进而 id/iq 的数值都变成原来的 2 倍。
3. 问：电机还能稳定地控到目标电流吗？

**预期结果**：能。因为这只是把前向通道常数 \(G\) 翻倍，闭环稳态增益仍趋于 1；只是 Kp/Ki 的「最佳值」需要相应调整（粗略地约为原来的 \(1/2\)）才能保持同样的动态响应。这正是「系数不必精确、误差交给 PID」的直接体现。

> 待本地验证：在真实电机+ADC 平台上，若做此改动，只需重新整定 Kp/Ki 即可恢复控制效果；仿真层面因缺电机模型无法直接验证闭环。

**阅读型实践（推荐）**：通读 `pi_controller.v`，找出所有「能用 PID 吸收的线性常数」和「不能用 PID 吸收的非线性」。前者如 Kp/Ki 的绝对大小、Clark/Park 的增益；后者如 `protect_add`/`protect_mul` 的饱和、角度的 mod 折叠、`MAX_AMP` 对 PWM 占空比的硬限幅。理解这条界线，是看懂全库「为何到处不精确却仍能工作」的钥匙。

#### 4.5.5 小练习与答案

**练习 1**：如果 Clark 的 √3 近似误差（\(0.0068\%\)）不是常数，而是随角度剧烈变化的随机量，还能被 PID 吸收吗？

**答案**：能吸收一部分，但效果变差。PID 能完美吸收的是**不随信号变化的常数增益**；若误差随角度变化，相当于前向通道增益随角度波动，会引起电流畸变和转矩纹波。本项目的移位近似是「固定的系数」，属于常数增益，所以安全。

**练习 2**：为什么 `MAX_AMP`（限制 SVPWM 最大幅值）不能用「调大 Kp/Ki」来补偿？

**答案**：`MAX_AMP` 是**硬限幅**（饱和），属非线性。当电压矢量幅值达到 `MAX_AMP` 时，再增大 Kp/Ki 也无法让输出电压继续增长（被钉死），此时闭环失效、积分会 windup。所以 `MAX_AMP` 必须按物理量（最大力矩 vs 采样窗口）精确设定，不能交给 PID。

**练习 3**：用一句话概括「系数不必精确」适用的前提条件。

**答案**：只要误差是「作用在前向通道上的、不随信号变化的常数增益」，且系统在 PI 闭环下保持稳定且不进入饱和区，该常数增益就能被 Kp/Ki 的整定完全吸收。

---

## 5. 综合实践

把本讲四节的知识固化成一张**定点标度总表**。这是你后续阅读任何模块、做跨平台移植（见 u4-l2）时的速查工具。

**任务**：整理一张表，列出 Clark、Park、PI、cartesian2polar 四个模块的输入输出位宽与定点含义，并指出哪些系数被刻意放大或近似，分析其对控制的影响。

**操作步骤**：

1. 仿照下表格式，逐模块填写。位宽和符号直接从源码端口声明读取；定点含义结合本讲的四条约定推导。
2. 在「刻意放大/近似」列，把本讲讲到的都列上（Clark 放大 2 倍 + √3 移位近似、Park 取 `[31:16]` 的 \(1/4\)、PI 取 `value[31:16]`、电流重构的未知 \(k\)）。
3. 在「影响」列，逐一标注「常数增益，PID 吸收」或「非线性，不可吸收」。

**参考答案表（核心列）**：

| 模块 | 输入（位宽/符号/含义） | 输出（位宽/符号/含义） | 刻意放大/近似 | 影响 |
|------|------------------------|------------------------|---------------|------|
| `clark_tr` | ia/ib/ic：16bit 有符号，原始电流单位，范围 −8191~8191 | ialpha/ibeta：16bit 有符号，定子 αβ 电流 | 整体放大 2 倍（消 1/2）；β 通路 √3 用 9 项移位近似（误差 0.0068%） | 常数增益，PID 吸收 |
| `park_tr` | ialpha/ibeta：16bit 有符号；psi：12bit 无符号（0~4095↔0~2π） | id/iq：16bit 有符号，转子 dq 电流 | 乘积取 `[31:16]`，带来 \(1/4\) 增益；sin/cos 本身缩放 \(2^{14}\) | 常数增益，PID 吸收 |
| `pi_controller` | i_aim/i_real：16bit 有符号；Kp/Ki：31bit 无符号（带定点小数的大整数） | o_value：16bit 有符号（vd/vq） | 输出取 `value[31:16]`（除 \(2^{16}\)）；`protect_add`/`protect_mul` 饱和 | 取高位：常数，PID 自洽；饱和：非线性，异常时介入 |
| `cartesian2polar` | i_x/i_y：16bit 有符号（vd/vq） | o_rho/o_theta：12bit 无符号（幅值 / 角度 0~4095↔0~2π） | 幅值 ρ 用 \((1024+a)/1024\) 修正（a 查 ROM）；逐次逼近求 arctan | 幅值修正近似常数，PID 吸收；角度需精确 |

**进阶**：在表尾加一行「总前向增益 \(G\)」，把 Clark 的 2、Park 的 \(1/4\)、电流重构的 \(k\) 相乘，体会它们如何塌缩成一个常数。

**预期结果**：你能指着表里每一行说清「这个近似是常数还是非线性、能不能被 PID 吸收」。当这张表烂熟于心，你就真正读懂了全库的定点设计哲学。

---

## 6. 本讲小结

- 全库统一四条定点约定：角度 12bit 无符号（`0~4095 ↔ 0~2π`）、电流/电压 16bit 有符号、三角函数 16bit 有符号（`-1~1 ↔ -16384~16384`，缩放 \(2^{14}\)）、Kp/Ki 31bit 无符号大整数。
- `clark_tr` 把等幅 Clarke 整体放大 2 倍以消掉 \(1/2\)，再用 9 项「符号扩展算术右移」之和逼近 √3，相对误差仅 \(0.0068\%\)。
- `park_tr` 的 16×16 乘法得 32 位，用统一的 `ide[31:16]` 收口，带来固定的 \(1/4\) 增益——这是「取高位实现定点除法」的标准套路。
- `pi_controller` 用 `protect_add`（33 位中间值）和 `protect_mul`（57 位中间值，源自 \(32+25\) 旧签名）做饱和截断，`protect_add` 同时兼任积分项的 anti-windup。
- 输出取高位（`o_value = value[31:16]`）让 Kp/Ki 成为带定点小数分辨率的大整数增益，可在运行时调整。
- **核心思想**：FOC 电流环是线性系统，作用在前向通道上的常数增益（Clark 的 2、Park 的 1/4、电流重构的 \(k\)）都可被 PI 的整定吸收——这就是「代码里系数全不精确、却仍能精确控电流」的数学根基；而饱和、mod 折叠、`MAX_AMP` 硬限幅属非线性，必须精确处理。

## 7. 下一步学习建议

- **u4-l2 参数整定与跨平台移植**：本讲讲了「系数不必精确」，下一讲顺势讲「那 Kp/Ki 到底怎么整定」，以及把 `altpll` 换成别的平台 PLL、保证主时钟约 36.864MHz 的移植要点。你会用到本讲的定点速查表来推算参数。
- **u4-l3 仿真方法论与波形解读**：本讲的多个「手算验证」都可以用 gtkwave 的 Analog 显示眼检（如 Park 的 \(1/4\) 增益、Clark 的 √3 近似），下一讲系统总结如何为新模块写 testbench。
- **重读源码**：带着本讲的「常数 vs 非线性」二分法，回头扫一遍 `foc_top.v` 第 76-109 行的角度换算与电流重构，体会「哪些被刻意精确（mod 折叠）、哪些被刻意放任（绝对增益）」。
