# u2-l5 PI 控制器 pi_controller.v

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚 PI 控制器在 FOC 电流环里的位置和作用——它把 d/q 轴的**目标电流**与**实际电流**的偏差，转换成 d/q 轴的**电压指令**（Vd/Vq）。
- 对照源码逐拍追踪一次 `i_en` 脉冲，讲明白 `pdelta → kpdelta1/idelta → kpdelta/kidelta → kpidelta → value` 这 5 级流水线节拍里每一级在算什么。
- 写出 PI 的核心公式：\( e = i_{aim}-i_{real} \)、积分项 \( \Sigma e \)、输出 \( K_p e + K_i \Sigma e \)。
- 看懂 `protect_add` / `protect_mul` 两个饱和函数如何用更宽的中间位宽做截断，防止 32 位有符号运算溢出，并理解为什么 `protect_mul` 用 57 位中间值。
- 把 `pi_controller` 在 `foc_top.v` 里被双例化（d 轴一个、q 轴一个）的接线和定点标度关系讲清楚。

本讲承接 [u2-l4 Park 变换与 sincos 计算器](u2-l4-park-and-sincos.md)：Park 变换把交流电流坍缩成直流量 id/iq 后，就需要 PI 控制器去把这两个直流量“逼”到目标值上。

## 2. 前置知识

在进入源码前，先用最直白的话讲几个本讲要用到的概念。

**反馈控制与误差 (error)。** 你想让某个量（比如 q 轴电流 iq）等于目标值（iq_aim），但实际测到的是 iq。两者之差 \( e = i_{aim} - i_{real} \) 就是误差。控制器的工作就是根据误差大小，去推动一个执行量（这里是电压 Vq），让误差趋向 0。

**比例 (P, Proportional)。** 误差越大，输出就按比例给得越大：\( u_P = K_p \cdot e \)。比例控制的优点是反应快，缺点是只要还有误差就一直用力，容易过冲；而且对“持续的恒定误差”（稳态误差）单独用 P 往往消不掉。

**积分 (I, Integral)。** 把误差一直累加起来 \( \Sigma e \)，再乘一个系数：\( u_I = K_i \cdot \Sigma e \)。只要误差不为零，积分项就会一直涨（或一直降），直到把稳态误差压到零。它的缺点是累加太猛会**积分饱和 (integral windup)**，所以本模块用饱和函数来限幅。

**PI = P + I。** 两者相加就是 PI 输出：

\[
u[k] = K_p\,e[k] + K_i \sum_{i=0}^{k} e[i]
\]

注意它是离散的：每个控制周期累加一次误差。本模块“只有 P 和 I，没有 D（微分）”，所以 README 里把它称作 **PI 控制器（PID 没有 D）**。

**饱和 (saturation)。** 数字硬件里每个变量位宽有限。两个大数相加或相乘，结果可能超出位宽而**回绕 (wrap-around)**——一个本该是巨大正数的值突然变成负数，控制器就会反向猛冲，非常危险。饱和函数的做法是：用更宽的中间位宽算出完整结果，一旦超出目标位宽的范围，就**钳位**到最大/最小值，宁可“砍顶”也不要回绕。

**握手脉冲 `i_en` / `o_en`。** 这是全库统一的约定（见 [u2-l1](u2-l1-foc-top-overview.md)）：一个时钟周期的高电平脉冲表示“本拍数据有效”。`pi_controller` 的输入 `i_en` 在每个控制周期来一个脉冲，输出 `o_en` 在结果算好后也回一个脉冲。所有中间寄存器都靠这串逐级下传的脉冲来同步节拍。

**定点数与“取高位”。** 本项目大量使用“把小数当作整数算、最后取高位当作缩放”的定点技巧（见 [u2-l3](u2-l3-clark-transform.md)、[u2-l4](u2-l4-park-and-sincos.md)）。本模块最后用 `value[31:16]` 取高 16 位，等价于除以 \(2^{16}\)，这正是定点缩放的关键一步。

## 3. 本讲源码地图

本讲只涉及两个文件：

| 文件 | 在本讲中的角色 |
|---|---|
| [RTL/foc/pi_controller.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/pi_controller.v) | PI 控制器本体：算误差、累加积分、做 P+I、饱和保护、5 级流水线。是本讲精读的对象。 |
| [RTL/foc/foc_top.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v) | 在 d 轴和 q 轴**各例化一个** `pi_controller`（`u_id_pi` 和 `u_iq_pi`），把 id/iq 与目标值转成 vd/vq。 |

回顾 [u2-l1](u2-l1-foc-top-overview.md) 的数据流：三相电流经 Clark、Park 变成直流量 id/iq 后，下一步就是交给本讲的 PI 控制器，算出电压指令 vd/vq，再经 cartesian2polar、反 Park、SVPWM 生成 PWM。PI 控制器是整条流水线里唯一带**状态（积分项）**的环节。

## 4. 核心概念与源码讲解

### 4.1 pi_controller：电流环的 PI 调节器

#### 4.1.1 概念说明

**它解决什么问题？** FOC 电流环的目标，是让 d 轴和 q 轴的实际电流（id、iq）紧紧跟随你给的目标电流（id_aim、iq_aim）。其中 iq_aim 决定扭矩大小和方向（[u1-l3](u1-l3-fpga-top.md) 里它在 ±200 间切换，电机就正反交替转），id_aim 一般设为 0（不弱磁）。但 Park 变换给出的 id/iq 只是“测量值”，光测量不能让电机听话——必须有一个控制器，根据“目标值 − 测量值”的偏差去生成电压指令 vd/vq，电压再经 SVPWM 变成驱动 MOS 管的 PWM，从而改变相电流，让偏差缩小。这个控制器就是 `pi_controller`。

**为什么是 PI 而不是 P 或 PID？** 单 P 控制器对持续的稳态误差无能为力（总留一点偏差），加上 I（积分）就能把稳态误差压到零，所以电流环这种要求“精确跟随”的场合几乎都用 PI。微分项 D 对噪声敏感、电流环采样本身已经够快，所以本模块不加 D。

**为什么用 FPGA 流水线实现？** 控制频率 = clk/2048（36.864 MHz 下约 18 kHz，每个控制周期约 55.6 µs，见 [u2-l1](u2-l1-foc-top-overview.md)）。PI 计算涉及乘法（Kp·e、Ki·Σe）和加法，如果在一个时钟周期内全做完，组合逻辑路径太长会拖低主频。本模块把它拆成 5 级流水线，每级只做一点点，让整条链能跑在几十 MHz 的时钟上，而 5 级延迟相对 2048 个时钟的控制周期几乎可以忽略。

**核心公式（离散 PI）：**

设第 k 个控制周期的误差为：

\[
e[k] = i_{aim}[k] - i_{real}[k]
\]

积分项是误差的累加和：

\[
\text{idelta}[k] = \sum_{i=0}^{k} e[i] = \text{idelta}[k-1] + e[k]
\]

最终输出（执行变量 = 电压指令）：

\[
\text{value}[k] = K_p \cdot e[k] + K_i \cdot \text{idelta}[k]
\]

再取高 16 位作为对外的 16 位有符号输出：

\[
o\_value = \text{value}[31:16] \;\approx\; \frac{K_p \cdot e + K_i \cdot \Sigma e}{2^{16}}
\]

这个除以 \(2^{16}\) 的缩放，就是把 Kp/Ki 当作“带小数位的定点增益”来用：用户给一个大整数 Kp，实际生效的比例是 Kp/65536，从而获得细粒度的调参分辨率。这与全库“取高位做定点缩放”的约定一致（Park 变换也是取 `[31:16]`）。

#### 4.1.2 核心流程

模块靠**一串逐级下传的使能脉冲**驱动流水线。输入脉冲 `i_en` 每控制周期来一次，每过一个寄存器就晚一拍：

```
i_en → en1 → en2 → en3 → en4 → o_en     (使能脉冲逐级打拍，共 5 拍延迟)
```

每一拍触发一组互不依赖的计算。下面是 5 拍的伪代码（省略复位）：

```
// 拍 0 (i_en=1)：锁存参数，算误差
pdelta  ← i_aim − i_real            // 误差 e，16bit 有符号扩展到 32bit
Kp0     ← i_Kp
Ki0     ← i_Ki

// 拍 1 (en1=1)：算比例项 + 累加积分项（两者都只依赖 pdelta，可并行）
kpdelta1 ← protect_mul( pdelta , Kp0 )        // Kp·e
idelta   ← protect_add( idelta , pdelta )     // Σe  （积分累加）
Ki1      ← Ki0

// 拍 2 (en2=1)：把比例项对齐 + 算积分项的乘积
kpdelta  ← kpdelta1                            // Kp·e（寄存器对齐）
kidelta  ← protect_mul( idelta , Ki1 )         // Ki·Σe

// 拍 3 (en3=1)：P + I 求和
kpidelta ← protect_add( kpdelta , kidelta )   // Kp·e + Ki·Σe

// 拍 4 (en4=1)：锁存结果，并拉高 o_en
value    ← kpidelta
o_en     ← 1                                   // 与 value 同时有效
```

几点关键：

- **数据依赖决定了级数。** 比例通路要算两次乘（其实是一次乘 + 对齐）；积分通路要先累加再乘；两条通路在拍 3 才汇合求和，拍 4 才锁存。一共 5 拍（从 `i_en` 到 `o_en`）。
- **积分项 `idelta` 是唯一跨控制周期保留的状态。** 它在拍 1 每个 `en1` 累加一次 `pdelta`，控制环路的所有“记忆”都在这里。
- **`o_en` 与 `value` 同拍有效。** 下游（cartesian2polar）可以放心地用 `o_en` 当作“vd/vq 有效”的脉冲。
- **`protect_add` / `protect_mul` 包住每一次可能溢出的运算**（积分累加、两次乘法、最后求和），把结果钳位到 32 位有符号范围。

#### 4.1.3 源码精读

**端口定义** —— 注意 Kp/Ki 是 31 位、aim/real 是 16 位有符号、输出是 16 位有符号：

[RTL/foc/pi_controller.v:L9-L19](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/pi_controller.v#L9-L19) —— 定义了 `rstn/clk/i_en/i_Kp/i_Ki/i_aim/i_real` 输入与 `o_en/o_value` 输出；`i_Kp`、`i_Ki` 是 31 位（`[30:0]`），`i_aim`、`i_real`、`o_value` 是 16 位有符号（`signed [15:0]`）。

[RTL/foc/pi_controller.v:L21-L25](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/pi_controller.v#L21-L25) —— 声明内部寄存器：使能链 `en1..en4`、运算寄存器 `pdelta/idelta/kpdelta1/kpdelta/kidelta/kpidelta/value`（全部 32 位有符号）、参数寄存器 `Kp0/Ki0/Ki1`（31 位）；`assign o_value = value[31:16]` 就是前面说的“取高 16 位 = 除以 2¹⁶”的定点缩放。

**饱和函数 `protect_add`** —— 两个 32 位有符号数相加，先用 33 位算全值，再钳位回 32 位：

[RTL/foc/pi_controller.v:L28-L42](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/pi_controller.v#L28-L42) —— 关键是 `y = $signed({a[31],a}) + $signed({b[31],b})`：用 `{a[31],a}` 把 32 位有符号数符号扩展成 33 位，相加得到完整和（两个 32 位有符号相加最多需要 33 位）；随后判断 `y` 是否超过 32 位有符号范围（`0x7fffffff` = +2147483647），超了就钳到最大/最小值，否则取低 32 位。这样无论怎么加都不会回绕。

> 小细节：负向钳位用的是 `-$signed(33'h7fffffff)` = −2147483647，而不是 32 位有符号的最小值 −2147483648，差 1 的不对称在控制里完全可以忽略。

**饱和函数 `protect_mul`** —— 两个 32 位有符号数相乘，用 57 位中间值保存，再钳位：

[RTL/foc/pi_controller.v:L45-L59](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/pi_controller.v#L45-L59) —— `reg signed [56:0] y` 是 57 位有符号中间变量；`y = a * b` 后同样按 `0x7fffffff` 钳位到 32 位有符号范围。**为什么是 57 位？** 见 [4.1.4 代码实践](#414-代码实践) 的追踪题，这里先给结论：57 = 32 + 25，对应被注释掉的旧签名 `input logic signed [24:0] b`（第 48 行）——设计最初假设增益 b 是 25 位有符号，32 位的被乘数 a 乘以 25 位的 b，乘积最多 57 位，刚好放得下。当前代码把 b 放宽到了 32 位，但实际调参时 Kp/Ki 与误差都远小于满量程，乘积远不会触及 57 位上限，配合末尾的饱和截断已经足够——这正是该项目“数值够用即可、误差由 PID 整定吸收”的一贯取舍（见 README FAQ：“这种系数问题可以通过 PID 调参来消除”）。

**使能脉冲流水线** —— 一句话把 `i_en` 打 5 拍：

[RTL/foc/pi_controller.v:L62-L75](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/pi_controller.v#L62-L75) —— `en1<=i_en; en2<=en1; en3<=en2; en4<=en3; o_en<=en4`，复位时全部清零。这就是 4.1.2 里那条使能链的硬件实现。

**拍 0（`i_en`）：锁存参数 + 算误差 `pdelta`：**

[RTL/foc/pi_controller.v:L78-L89](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/pi_controller.v#L78-L89) —— `pdelta <= $signed({{16{i_aim[15]}},i_aim}) - $signed({{16{i_real[15]}},i_real})`：用复制符号位 `{16{...}}` 把 16 位有符号 `i_aim`/`i_real` 符号扩展成 32 位再相减，得到 32 位有符号误差 `pdelta = i_aim - i_real`；同时把 `i_Kp`/`i_Ki` 锁进 `Kp0`/`Ki0`（每个控制周期采样一次增益，支持运行时调参）。

**拍 1（`en1`）：比例项 `kpdelta1` + 积分累加 `idelta`（并行）：**

[RTL/foc/pi_controller.v:L91-L102](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/pi_controller.v#L91-L102) —— `kpdelta1 <= protect_mul(pdelta, $signed({1'h0, Kp0}))` 算 \(K_p \cdot e\)（注意 `{1'h0, Kp0}` 强制最高位为 0，把 31 位无符号增益当成 32 位**非负**有符号数参与乘法）；`idelta <= protect_add(idelta, pdelta)` 把本拍误差累加进积分项 \( \Sigma e \)。两者都只依赖拍 0 的 `pdelta`，所以可以放在同一拍并行。`protect_add` 在这里兼任**积分限幅**，防止堵转时积分项无限增长导致 windup。

**拍 2（`en2`）：对齐比例项 + 算积分乘积 `kidelta`：**

[RTL/foc/pi_controller.v:L104-L113](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/pi_controller.v#L104-L113) —— `kpdelta <= kpdelta1` 只是把上拍的比例项寄存器对齐一拍；`kidelta <= protect_mul(idelta, $signed({1'h0, Ki1}))` 算 \(K_i \cdot \Sigma e\)。注意这里用的是 `Ki1`（拍 1 已经把 `Ki0` 接力到 `Ki1`），保证增益与积分项在时间上对齐。

**拍 3（`en3`）：P + I 求和 `kpidelta`：**

[RTL/foc/pi_controller.v:L115-L121](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/pi_controller.v#L115-L121) —— `kpidelta <= protect_add(kpdelta, kidelta)`，即 \( K_p e + K_i \Sigma e \)，两条通路在此汇合。`protect_add` 保证求和不会溢出。

**拍 4（`en4`）：锁存最终结果 `value`：**

[RTL/foc/pi_controller.v:L123-L130](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/pi_controller.v#L123-L130) —— `value <= kpidelta`。注意第 128 行注释 `// modified at 20230609, now it is a stardard PID` 和第 129 行被注释掉的 `value <= protect_add(value, kpidelta)`：旧版本会让 `value` 自己也累加（那会变成另一种控制器），现版本每个周期直接用 `kpidelta` 覆盖，是标准 PI。配合 [L25](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/pi_controller.v#L25) 的 `assign o_value = value[31:16]`，输出取高 16 位。

**在 `foc_top.v` 中被双例化** —— d 轴、q 轴各一个，结构完全对称：

[RTL/foc/foc_top.v:L160-L170](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L160-L170) —— `u_id_pi`：`i_en` 接 Park 变换输出的 `en_idq` 脉冲，`i_aim=id_aim`、`i_real=id`，输出 `o_value` 接 `vd`（d 轴电压）。`.o_en()` 悬空——d 轴 PI 的完成脉冲下游不需要。

[RTL/foc/foc_top.v:L180-L190](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L180-L190) —— `u_iq_pi`：与 d 轴完全对称，`i_aim=iq_aim`、`i_real=iq`，输出接 `vq`（q 轴电压）。

两个 PI 共用同一对 `Kp`/`Ki`（[RTL/foc/foc_top.v:L23-L24](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L23-L24)），它们是 `foc_top` 的 31 位输入端口，README 明确说“可以在运行时调整”。算出的 `vd`/`vq` 随后送进 `cartesian2polar` 转成极坐标，进入下一讲 [u2-l6](u2-l6-cartesian2polar-and-invpark.md)。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**（SIM/ 目录里没有 pi_controller 的 testbench，整模块需要电机模型才能仿真，见 [u1-l4](u1-l4-iverilog-simulation.md)）。任务是亲手追踪一次 `i_en` 脉冲穿过整条流水线的过程。

**实践目标：** 把 `pdelta / kpdelta1 / idelta / kpdelta / kidelta / kpidelta / value` 这 7 个寄存器在 `i_en` 到来后 5 个时钟周期里的更新顺序画清楚，并解释 `protect_mul` 为何用 57 位中间值。

**操作步骤：**

1. 打开 [RTL/foc/pi_controller.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/pi_controller.v)，对照下面这张追踪表，逐行核对每个 `always` 块。
2. 假设第 T0 个时钟上升沿 `i_en=1`，且 `i_aim=200`、`i_real=50`、`i_Kp`/`i_Ki` 为某组增益。按下表追踪（“更新后”指该上升沿非阻塞赋值生效后的新值）：

| 上升沿 | 触发的使能 | 本拍计算动作（关键寄存器更新后） |
|---|---|---|
| T0 | `i_en=1` | `pdelta = 200−50 = 150`；`Kp0=i_Kp`，`Ki0=i_Ki`；同时 `en1` 置 1 |
| T1 | `en1=1` | `kpdelta1 = protect_mul(pdelta, Kp0)` = \(K_p \cdot 150\)；`idelta = protect_add(idelta_old, 150)`；`Ki1=Ki0`；`en2` 置 1 |
| T2 | `en2=1` | `kpdelta = kpdelta1`（比例项对齐）；`kidelta = protect_mul(idelta, Ki1)` = \(K_i \cdot \Sigma e\)；`en3` 置 1 |
| T3 | `en3=1` | `kpidelta = protect_add(kpdelta, kidelta)` = \(K_p e + K_i \Sigma e\)；`en4` 置 1 |
| T4 | `en4=1` | `value = kpidelta`；`o_en` 置 1（与 `value` 同拍有效） |

3. **观察现象（预期结果）：** 从 `i_en` 脉冲（T0）到 `o_en` 脉冲（T4 之后一拍）共经历 **5 个时钟周期** 的延迟。`o_value` 与 `o_en` 同时有效，下游可以安全采样。注意第 T1 拍 `kpdelta1`（比例乘）和 `idelta`（积分累加）在同一个 `always` 块里、同一个时钟沿完成——因为它们都只依赖 T0 锁存的 `pdelta`，互不依赖，所以并行计算省了一级流水线。
4. **解释 57 位中间值：** 翻到 [第 45–59 行 protect_mul](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/pi_controller.v#L45-L59)，再看第 48 行被注释掉的旧签名 `input logic signed [24:0] b`。结论：**57 = 32 + 25**。被乘数 a（`pdelta`/`idelta`，32 位有符号）乘以最初假设为 25 位有符号的增益 b，乘积最多需要 32+25=57 位才能完整表示。当前代码虽然把 b 放宽成了 32 位（`{1'h0, Kp0}` 强制为非负 32 位），但实际 Kp/Ki 是远小于 \(2^{25}\) 的小增益、误差也远小于 32 位满量程，真实乘积远不到 57 位上限；即便偶尔偏大，末尾的饱和逻辑也会把它钳到 32 位范围。所以 57 位是“历史沿袭 + 实际够用”的选择。
5. **进阶（可选）：** 仿照 [u4-l3](u4-l3-simulation-methodology.md) 的思路，给 `pi_controller` 单独写一个 testbench：`i_aim` 给恒定值、`i_real` 用 `sincos` 或正弦表模拟，观察 `o_value` 是否朝误差减小的方向变化。本步**待本地验证**（需要你自行搭建 iverilog 仿真）。

#### 4.1.5 小练习与答案

**练习 1：** 如果把输出改成 `assign o_value = value[15:0]`（取低 16 位），PI 控制器还能正常工作吗？为什么？

> **答：** 不能正常工作。`value` 的高 16 位才是 \(K_p e + K_i \Sigma e\) 经“除以 2¹⁶”缩放后的有效结果，低 16 位是被丢弃的低位/小数部分。取低 16 位会丢掉主要量级，输出的电压指令几乎无意义，也与下游 cartesian2polar 对 16 位有符号输入的定点约定不符。

**练习 2：** 电机堵转、iq 长期达不到 iq_aim 时，积分项 `idelta` 会怎样？`protect_add` 在这里起什么作用？

> **答：** `idelta` 会持续累加 `pdelta`，越积越大，出现**积分饱和 (windup)**。`protect_add` 把累加结果钳位在 32 位有符号范围（约 ±2.15×10⁹），防止积分项溢出回绕成相反符号导致控制器反向猛冲。这是一种简易的**积分限幅 (anti-windup)**。

**练习 3：** 为什么把“算比例项 `kpdelta1`”和“累加积分项 `idelta`”放在同一拍（`en1`）而不是分成两级？

> **答：** 因为两者都只依赖拍 0（`i_en`）锁存的 `pdelta`，彼此没有数据依赖，可以在同一个时钟沿、用不同的赋值语句并行完成。合并到一级能省掉一拍流水线延迟，缩短控制环路的总延迟——在实时控制里每一拍都值得省。

## 5. 综合实践

把本讲的知识串起来，做一个“纸面调参 + 接线核对”的小任务。

**任务：** 假设你要让本电流环的响应更“硬”（稳态误差更小、跟随更紧），但又不能因为积分太强而震荡。请完成：

1. **指出该调哪个参数。** 在 [foc_top.v 的 Kp/Ki 端口](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L23-L24) 中，想让稳态误差更小应增大 `Ki`（积分项把残余误差压零的力度更强）；想让初始响应更快应增大 `Kp`。两者都要避免过大导致震荡。
2. **核对数据通路。** 在 [foc_top.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v) 里找到 `u_id_pi` 与 `u_iq_pi` 两个例化（[L160-L170](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L170)、[L180-L190](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L180)），确认它们的 `i_en` 都来自 Park 变换的完成脉冲 `en_idq`，`i_real` 分别是 `id`/`iq`，输出分别接 `vd`/`vq`。画出 `en_idq → (PI, 5 拍) → vd/vq → cartesian2polar → …` 这条链路。
3. **算一笔延迟账。** 已知控制周期 = clk/2048（约 2048 个时钟）。PI 占 5 个时钟，Clark 占 3 个，Park 占 2 个（见 [u2-l1](u2-l1-foc-top-overview.md)）。算一算整条“采样 → PWM 更新”链路的总延迟大约占控制周期的百分之几，体会为什么作者说运算链只占控制周期的很小一部分。
4. **观察验证。** 若有硬件，按 README 的方法用串口（[uart_monitor](u3-l3-uart-monitor-and-user-logic.md)）观察 id/iq 对 iq_aim 阶跃（+200↔−200）的跟随情况，微调 Kp/Ki 直到跟随又快又不过冲。本步**待本地验证**。

## 6. 本讲小结

- `pi_controller` 是 FOC 电流环里唯一带状态的环节：根据 \( e = i_{aim} - i_{real} \) 算误差，累加成积分项 \( \Sigma e \)，输出 \( K_p e + K_i \Sigma e \) 作为电压指令 vd/vq。
- 它是 5 级流水线，靠 `i_en → en1 → en2 → en3 → en4 → o_en` 的使能脉冲逐级下传；比例乘与积分累加在 `en1` 拍并行完成，P+I 在 `en3` 拍汇合，`en4` 拍锁存 `value` 并拉高 `o_en`。
- 输出取 `value[31:16]`，等价于除以 \(2^{16}\) 的定点缩放，使 Kp/Ki 成为带小数分辨率的大整数增益，可在运行时调整。
- `protect_add` 用 33 位中间值保护加法、`protect_mul` 用 57 位中间值保护乘法，统一钳位到 32 位有符号范围，防止溢出回绕；`protect_add` 还兼任积分项的 anti-windup 限幅。
- `foc_top` 在 d 轴、q 轴各例化一个 `pi_controller`（`u_id_pi`/`u_iq_pi`），共用同一对 Kp/Ki，输出 vd/vq 送入 cartesian2polar。
- 本模块再次体现“数值不必精确、误差由 PID 整定吸收”的工程哲学（57 位中间值、系数缩放均可由调参消化）。

## 7. 下一步学习建议

- 下一讲 [u2-l6 cartesian2polar 与反 Park 变换](u2-l6-cartesian2polar-and-invpark.md) 会接住本模块的输出 vd/vq，把它从直角坐标转成极坐标 (Vrρ, Vrθ)，再由 `foc_top` 里的反 Park 旋转到定子极坐标。建议先复习极坐标 \( (\rho, \theta) \) 与直角坐标的关系。
- 想深入定点与饱和的全库约定，可跳读 [u4-l1 定点数运算与饱和保护](u4-l1-fixed-point-and-saturation.md)，它会把 Clark/Park/PI/cartesian2polar 的位宽与缩放关系汇总成一张表。
- 想亲手给 PI 写 testbench、用 gtkwave 看动态响应，参考 [u4-l3 仿真方法论与波形解读](u4-l3-simulation-methodology.md)。
- 若关心 Kp/Ki 怎么选、换电机/换板子要改哪些参数，参考 [u4-l2 参数整定与跨平台移植](u4-l2-parameter-tuning-and-porting.md)。
