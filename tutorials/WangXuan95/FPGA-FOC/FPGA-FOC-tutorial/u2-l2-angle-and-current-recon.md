# 角度换算与电流重构

## 1. 本讲目标

上一讲（u2-l1）我们俯瞰了 `foc_top.v` 的整条数据通路，知道了电流矢量要依次穿过 abc → αβ → dq → 转子极坐标 → 定子极坐标。但通路的两端还留着两个“黑盒”没打开：

- 一端是**角度**：传感器读进来的是机械角度 φ，而 Park 变换、反 Park 变换需要的是电角度 ψ。φ 怎么变成 ψ？
- 另一端是**电流**：ADC 读进来的只是三个 0~4095 的原始数字（ADCa/ADCb/ADCc），而 Clark 变换需要的是有正有负的三相电流 ia/ib/ic。原始 ADC 值怎么变成电流？

这两个转换都发生在 `foc_top.v` 自己写的两个 `always` 块里，是整个 FOC 数据流的“入口预处理”。本讲学完后你应该能够：

1. 说清机械角度 φ 与电角度 ψ 的关系 \(\psi=N\cdot(\varphi-\Phi)\)，理解极对数 N（参数 `POLE_PAIR`）的物理意义，以及 `ANGLE_INV` 如何处理传感器装反。
2. 推导出为什么 \(i_a = \mathrm{ADC}_b + \mathrm{ADC}_c - 2\cdot\mathrm{ADC}_a\)，看懂“反向放大+偏置”硬件与基尔霍夫电流定律（KCL）如何联手消掉未知的偏置电压。
3. 理解初始化阶段如何标定初始机械角度 Φ——靠“施加最大电压矢量把转子拽到电角度 0 处”来记录零点。

## 2. 前置知识

在进入源码前，先用三段通俗的话把物理背景补齐。

**机械角度 φ vs 电角度 ψ。** 电机转子转一圈，机械角度走 \(2\pi\)。但定子绕组里的电流波形在一个机械周期内会重复 N 次（N 是极对数），所以对“控制电流”这件事而言，真正有意义的是**电角度** ψ——它每 \(\frac{2\pi}{N}\) 机械角度就走完一个 \(2\pi\)。两者的关系是：

\[
\psi = N\cdot(\varphi - \Phi)
\]

其中 Φ 是“电角度为 0 时对应的机械角度”，也就是转子的零点参考。Φ 必须在通电后实地标定，不能瞎猜。本项目中 φ 和 ψ 都用 12bit 无符号数表示，0~4095 对应 0~\(2\pi\)，即 1024 对应 90°、2048 对应 180°、3072 对应 270°。

**为什么相电流采样要“反向放大+偏置”。** 电机的相电流 ia/ib/ic 是双极性的（有正有负，正代表电流从半桥流入电机）。但常用 ADC（包括本项目的 AD7928）只能采正电压（单极性）。所以电机驱动板上的采样-放大电路（本项目用 MP6540 内置的方案）会做一次“反向放大加偏置”，输出给 ADC 的电压满足：

\[
\mathrm{ADC}_a = -R\cdot i_a + V_\text{off},\quad
\mathrm{ADC}_b = -R\cdot i_b + V_\text{off},\quad
\mathrm{ADC}_c = -R\cdot i_c + V_\text{off}
\]

其中 R>0 是放大系数（跨阻），\(V_\text{off}\) 是偏置电压，保证 \(\mathrm{ADC}_a/\mathrm{ADC}_b/\mathrm{ADC}_c\) 落在 0~4095 的单极性范围内。难点是：R 和 \(V_\text{off}\) 都是模拟电路参数，FPGA 并不知道它们的确切值。本讲的核心技巧就是——**用 KCL 把 \(V_\text{off}\) 消掉**。

**KCL（基尔霍夫电流定律）。** 电机三相绕组星形连接，中点悬空，三相电流之和恒为 0：

\[
i_a + i_b + i_c = 0
\]

这是把三个独立的 ADC 读数“耦合”起来的关键约束，也是后面消去 \(V_\text{off}\) 的钥匙。

## 3. 本讲源码地图

本讲只看一个文件，但要把其中三个 `always` 块读透：

| 源码位置 | 作用 |
| :--- | :--- |
| [RTL/foc/foc_top.v:11-17](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L11-L17) | 模块参数定义，含 `INIT_CYCLES`/`ANGLE_INV`/`POLE_PAIR` 等，本讲关注后两者 |
| [RTL/foc/foc_top.v:26](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L26-L26) | 输入端口 `phi`（机械角度 φ，12bit） |
| [RTL/foc/foc_top.v:30](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L30-L30) | 输入端口 `adc_a/adc_b/adc_c`（三相 ADC 原始值，12bit） |
| [RTL/foc/foc_top.v:48-53](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L48-L53) | 内部寄存器 `init_phi`(Φ)、`psi`(ψ)、`en_iabc`、`ia/ib/ic` 的声明 |
| [RTL/foc/foc_top.v:76-88](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L76-L88) | **always 块①**：机械角度 φ → 电角度 ψ |
| [RTL/foc/foc_top.v:99-109](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L99-L109) | **always 块②**：ADC 原始值 → 三相电流 ia/ib/ic |
| [RTL/foc/foc_top.v:218-237](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L218-L237) | **always 块③**：初始化标定 Φ + 反 Park 变换 |
| [README.md:536-550](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md#L536-L550) | 作者 FAQ 中对 \(i_a = \mathrm{ADC}_b+\mathrm{ADC}_c-2\cdot\mathrm{ADC}_a\) 的官方推导 |

块①②是本讲的主体，块③用于回答“Φ 从哪来”——没有它，\(\psi=N(\varphi-\Phi)\) 里的 Φ 就没有来源。

## 4. 核心概念与源码讲解

### 4.1 机械角度到电角度的换算

#### 4.1.1 概念说明

FOC 的核心是把定子电流分解到转子的 d/q 轴上。d/q 轴是跟着转子一起转的，所以必须知道转子当前的电角度 ψ。但磁编码器（AS5600）只能告诉你转子的**机械**位置 φ。把 φ 换算成 ψ 就是这一段的任务。

换算公式：

\[
\psi = N\cdot(\varphi - \Phi)
\]

- N = `POLE_PAIR`，电机极对数，由电机型号决定（默认 7）。
- Φ = `init_phi`，电角度为 0 时对应的机械角度，初始化阶段标定（见 4.3）。
- 若传感器装反了（A→B→C→A 的旋转方向与 φ 增大的方向相反），取负：\(\psi = -N\cdot(\varphi-\Phi)\)，由参数 `ANGLE_INV` 选择。

一个关键直觉：ψ 只用 12bit 存储，而 \(N\cdot(\varphi-\Phi)\) 可能远大于 4095（比如 N=7、\(\varphi-\Phi=3000\) 时乘积=21000）。这看起来会“溢出”，但因为是角度（mod \(2\pi\)，即 mod 4096），**截断到低 12 位恰好就是模 4096 运算**，结果依然正确。这是定点角度运算最巧妙的一点。

#### 4.1.2 核心流程

- 每个时钟上升沿，若已初始化（`init_done=1`），用当前的 φ 和锁存的 Φ 计算乘积，截断到 12bit 写入 ψ。
- 若未初始化（`init_done=0`），ψ 被强制清 0（与所有子模块一起处于复位态）。
- `ANGLE_INV` 是编译期参数，用 `generate-if` 在综合时二选一地选择正向或反向公式，不消耗运行时资源。

伪代码：

```
always @(posedge clk):
    if (未初始化):  psi = 0
    else if (ANGLE_INV==1):  psi = (N * (Φ - φ)) mod 4096   // 反向
    else:                   psi = (N * (φ - Φ)) mod 4096   // 正向
```

#### 4.1.3 源码精读

整段用 `generate-if` 包住，按 `ANGLE_INV` 综合出两套之一，见 [RTL/foc/foc_top.v:76-88](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L76-L88)：这两段用 `generate-if` 区分传感器是否装反，注释里写清了公式。

传感器装反时（`ANGLE_INV=1`），取反向公式，关键一行在 [RTL/foc/foc_top.v:81](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L81-L81)：

```verilog
psi <= {4'h0, POLE_PAIR} * (init_phi - phi);  // ψ = -N * (φ - Φ)
```

传感器没装反时（默认 `ANGLE_INV=0`），取正向公式，见 [RTL/foc/foc_top.v:87](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L87-L87)：

```verilog
psi <= {4'h0, POLE_PAIR} * (phi - init_phi);  // ψ =  N * (φ - Φ)
```

几个要点：

- `POLE_PAIR` 是 8bit 参数（[RTL/foc/foc_top.v:15](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L15-L15)），`{4'h0, POLE_PAIR}` 把它零扩展到 12bit，与 `(phi - init_phi)`（也是 12bit）对齐做乘法。
- `phi`、`init_phi`、`psi` 都是 12bit（声明见 [RTL/foc/foc_top.v:48-50](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L48-L50)）。乘积赋给 12bit 的 `psi` 时自动截断高位——这正是我们想要的 mod 4096。
- 复位写法 `always @(posedge clk or negedge init_done) if(~init_done)`：把 `init_done` 当作低有效异步复位。`init_done=0`（初始化期间）时 ψ 被钳在 0；`init_done` 跳 1 后下一个时钟沿开始正常计算。这与所有子模块用 `.rstn(init_done)` 接同一根线（见 [RTL/foc/foc_top.v:120](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L120-L120) 等处）一致——初始化结束时全模块同步解复位。

> **为什么取负等价于 mod 4096 取补？** 当 `ANGLE_INV=1`，算的是 \(N(\Phi-\varphi)=-N(\varphi-\Phi)\)。在 mod 4096 下，\(-x \equiv 4096-x\)，而 12bit 二进制补码天然就是这个映射，所以直接把 `(init_phi-phi)` 喂进乘法再截断，就得到了方向取反后的电角度，无需额外处理。

#### 4.1.4 代码实践

**实践目标**：验证“截断到 12bit = 模 4096 运算”确实给出正确的电角度，并理解极对数如何让 ψ 在一个机械周期内循环 N 次。

**操作步骤**：

1. 假设 `POLE_PAIR=7`、`init_phi=0`（即 Φ=0）。
2. 让 φ 从 0 每步加 1，走到 4096（一个机械周期）。
3. 对每个 φ，手算 \(7\cdot\varphi\)，再对 4096 取模，得到 ψ。
4. 找出 φ=0、585、1170、4096 时 ψ 的值，并数一数 ψ 在 φ 走 0→4096 期间完整循环了几次。

**需要观察的现象**：ψ 在一个机械周期（0→4096）内应完整循环 7 次（因为 N=7，电周期是机械周期的 1/7）。

**预期结果**：

| φ | \(7\varphi\) | ψ = \(7\varphi \bmod 4096\) |
| :--- | :--- | :--- |
| 0 | 0 | 0 |
| 585 | 4095 | 4095（≈360°，即一个电周期末） |
| 586 | 4102 | 6（开始第 2 个电周期） |
| 1170 | 8190 | 4094（≈第 2 个电周期末） |
| 4096 | 28672 | 0（回到起点，7 个电周期整） |

手算可验证：\(7\times585 = 4095\)，正好是一个电周期（4096）的末尾；继续加 1 即溢出归零。这就是 12bit 截断的几何含义。**待本地验证**：若想用代码确认，可在 iverilog 里写一个只例化该 always 块的小 testbench，扫描 phi 观察 psi。

#### 4.1.5 小练习与答案

**练习 1**：若 `POLE_PAIR=14`，φ 走一个机械周期（0→4096），ψ 经历几个完整电周期？

**答案**：14 个。因为 \(\psi=N(\varphi-\Phi)\)，N=14 时 ψ 的变化率是机械角度的 14 倍，4096 的机械范围被映射成 14 个 4096 的电范围，截断后循环 14 次。

**练习 2**：`ANGLE_INV=1` 时代码算 \(N(\Phi-\varphi)\) 而非 \(N(\varphi-\Phi)\)，这等价于对电角度做了什么运算？为什么不用额外电路？

**答案**：等价于对 ψ 取负（方向反转）。在 mod 4096 下取负就是 \(4096-\psi\)，而 12bit 二进制补码天然实现这个映射，所以只需把减法方向换一下，再靠赋值时的位宽截断自动完成，无需额外硬件。

**练习 3**：为什么 `psi` 只用 12bit 寄存器就够了，而不需要 24bit 来存完整乘积？

**答案**：因为角度是 mod \(2\pi\) 的周期量，4096 对应 \(2\pi\)。乘积的高位只反映“转过了几个完整电周期”，对当前相位无影响；截断到低 12 位 = 模 4096 运算，给出的恰是当前电角度，所以 12bit 足够且正确。

---

### 4.2 三相电流重构：从 ADC 原始值到 ia/ib/ic

#### 4.2.1 概念说明

ADC 给出三个 0~4095 的数 `adc_a/adc_b/adc_c`。直接拿来当电流用不行——它们带了偏置 \(V_\text{off}\)，还被反向放大了（系数 −R），而且是单极性的。我们要还原出有正有负的 ia/ib/ic。

直接的做法是按 \(i_a=(V_\text{off}-\mathrm{ADC}_a)/R\) 逐相还原，但 R 和 \(V_\text{off}\) 是模拟参数，FPGA 不知道。**巧妙的做法**是利用 KCL（\(i_a+i_b+i_c=0\)）把三相互相联系起来，从而把未知的 \(V_\text{off}\) 用三个 ADC 读数自身表示出来，最终得到一个**只含 ADC 读数、不含任何模拟参数**的公式（除了一个被吸收掉的常数增益 k）。

#### 4.2.2 核心流程

数学推导（这是本讲的核心，与 [README.md:536-550](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md#L536-L550) 的官方 FAQ 一致）：

已知：

\[
\mathrm{ADC}_a = -R\,i_a + V_\text{off},\quad
\mathrm{ADC}_b = -R\,i_b + V_\text{off},\quad
\mathrm{ADC}_c = -R\,i_c + V_\text{off}
\]

\[
i_a + i_b + i_c = 0 \quad (\text{KCL})
\]

**第 1 步**：从三个 ADC 方程解出电流：

\[
i_a = \frac{V_\text{off}-\mathrm{ADC}_a}{R},\quad
i_b = \frac{V_\text{off}-\mathrm{ADC}_b}{R},\quad
i_c = \frac{V_\text{off}-\mathrm{ADC}_c}{R}
\]

**第 2 步**：三式相加，用 KCL 消掉电流：

\[
i_a+i_b+i_c = \frac{3V_\text{off} - \mathrm{ADC}_a - \mathrm{ADC}_b - \mathrm{ADC}_c}{R} = 0
\]

\[
\Longrightarrow\quad 3V_\text{off} = \mathrm{ADC}_a + \mathrm{ADC}_b + \mathrm{ADC}_c
\]

**第 3 步**：把 \(3V_\text{off}\) 代回 \(i_a\)：

\[
i_a = \frac{V_\text{off}-\mathrm{ADC}_a}{R}
     = \frac{3V_\text{off} - 3\,\mathrm{ADC}_a}{3R}
     = \frac{(\mathrm{ADC}_a+\mathrm{ADC}_b+\mathrm{ADC}_c) - 3\,\mathrm{ADC}_a}{3R}
     = \frac{\mathrm{ADC}_b + \mathrm{ADC}_c - 2\,\mathrm{ADC}_a}{3R}
\]

**第 4 步**：令 \(k=\frac{1}{3R}\)，则：

\[
i_a = k\cdot(\mathrm{ADC}_b + \mathrm{ADC}_c - 2\,\mathrm{ADC}_a)
\]

代码里取 \(k=1\)（即丢掉常数增益），直接算括号里的部分。这之所以可行，是因为 **FOC 是线性系统**：从 ia 到最终的电压矢量，每一级（Clark、Park、PI、SVPWM）都是线性运算，一个贯穿全链路的常数增益 k 完全可以被 PI 控制器的 `Kp/Ki` 吸收掉。这是本项目反复出现的工程哲学——“系数不必精确，能跑就行，误差交给 PID 调参消除”（README FAQ 亦多次强调）。

同理：

\[
i_b = k\cdot(\mathrm{ADC}_a + \mathrm{ADC}_c - 2\,\mathrm{ADC}_b),\quad
i_c = k\cdot(\mathrm{ADC}_a + \mathrm{ADC}_b - 2\,\mathrm{ADC}_c)
\]

数据流时序上，每当外部 ADC 采完一次三相（`en_adc` 拉一个时钟周期高电平），本块就更新一次 ia/ib/ic，并在同一拍把 `en_iabc` 拉高一个周期，通知下游 Clark 变换“数据有效”。

#### 4.2.3 源码精读

整个电流重构块见 [RTL/foc/foc_top.v:99-109](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L99-L109)：根据 KCL 从 ADC 原始值减去偏置、重构出三相电流，注释里直接写出三个公式。

核心三行在 [RTL/foc/foc_top.v:105-107](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L105-L107)：

```verilog
ia <= $signed( {4'b0, adc_b} + {4'b0, adc_c} - {3'b0, adc_a, 1'b0} );   // Ia = ADCb + ADCc - 2*ADCa
ib <= $signed( {4'b0, adc_a} + {4'b0, adc_c} - {3'b0, adc_b, 1'b0} );   // Ib = ADCa + ADCc - 2*ADCb
ic <= $signed( {4'b0, adc_a} + {4'b0, adc_b} - {3'b0, adc_c, 1'b0} );   // Ic = ADCa + ADCb - 2*ADCc
```

逐项拆解 ia 这一行（第 [105](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L105-L105) 行）的位宽技巧：

- `adc_a/adc_b/adc_c` 都是 12bit 无符号（0~4095），声明见 [RTL/foc/foc_top.v:30](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L30-L30)。
- `{4'b0, adc_b}` = 16bit，值仍为 adc_b（零扩展到 16bit，因为 ADC 值是无符号的，用零扩展而非符号扩展）。
- `{3'b0, adc_a, 1'b0}` = 16bit，值 = `adc_a << 1` = \(2\cdot\mathrm{ADC}_a\)（0~8190）。用拼接左移 1 位来表示“乘 2”，是纯连线、零硬件成本，且无符号歧义。
- 三个 16bit 操作数做加减，结果范围是 \([0+0-8190,\ 4095+4095-0] = [-8190,\ +8190]\)，恰好落在 16bit 有符号数 \([-32768,+32767]\) 范围内，不会溢出。
- `$signed(...)` 把 16bit 运算结果**重新解释**为二进制补码有符号数。当 `adc_b+adc_c < 2*adc_a` 时，无符号减法会“下溢”成一个大数（如 \(-8190 \to 65536-8190=57346\)），`$signed` 把它读回成 \(-8190\)——补码天然处理了负数。

握手时序见 [RTL/foc/foc_top.v:103-104](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L103-L104)：

```verilog
en_iabc <= en_adc;
if(en_adc) begin ia <= ...; ib <= ...; ic <= ...; end
```

`en_iabc` 把 `en_adc` 打一拍：当 `en_adc` 在第 N 拍为高，第 N+1 拍 `en_iabc` 为高，且 ia/ib/ic 也正是在第 N 拍末（即第 N+1 拍生效）更新——所以 `en_iabc` 脉冲与新鲜的 ia/ib/ic 同拍出现，完美对齐下游 Clark 的输入节拍（延续 u2-l1 讲的 `i_en/o_en` 单周期脉冲握手）。

复位与块①相同：`init_done=0` 时 `{en_iabc, ia, ib, ic} <= 0`（[RTL/foc/foc_top.v:100-101](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L100-L101)），初始化期间不产出无效电流。

#### 4.2.4 代码实践

**实践目标**：把 4.2.2 的推导与代码第 105 行逐字对照，确认代码实现的就是 \(i_a = \mathrm{ADC}_b+\mathrm{ADC}_c-2\,\mathrm{ADC}_a\)（差一个被吸收的常数 k），并验证位宽与补码能正确产生负电流。

**操作步骤**：

1. 打开 [RTL/foc/foc_top.v:105](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L105-L105)，把 `{4'b0, adc_b} + {4'b0, adc_c} - {3'b0, adc_a, 1'b0}` 翻译成数学式，确认它等于 \(\mathrm{ADC}_b+\mathrm{ADC}_c-2\,\mathrm{ADC}_a\)。
2. 对照 4.2.2 的四步推导，确认代码省略的正是最后的常数 \(k=1/(3R)\)。
3. 构造一个反例验证负电流：设 `adc_a=4000, adc_b=2048, adc_c=2048`（A 相电流为负，B/C 为正）。手算 \(i_a = 2048+2048-2\times4000 = -3856\)。
4. 用 16bit 补码验证：\(-3856\) 的 16bit 无符号表示 = \(65536-3856 = 61680\)；`$signed` 读回 = \(-3856\)。确认落在 \([-32768,+32767]\) 内，无溢出。
5. 同时验证 KCL 自洽：算 \(i_b = 4000+2048-2\times2048 = 1952\)，\(i_c = 4000+2048-2\times2048 = 1952\)，检查 \(i_a+i_b+i_c = -3856+1952+1952 = 48\)，本应为 0——这 48 的残差正来自“代码丢掉了 k 并用整数截断”，属于数值误差，会被 PID 吸收。

**需要观察的现象**：步骤 5 中三者之和应接近 0 但不严格为 0；这正是“系数不精确但不影响控制”的体现。

**预期结果**：代码第 105 行精确实现 \(i_a=\mathrm{ADC}_b+\mathrm{ADC}_c-2\,\mathrm{ADC}_a\)（常数增益 k 被省略）；16bit 补码能正确表示 \(-3856\) 这样的负电流。**待本地验证**：若在硬件上运行，可在 `uart_monitor` 里把 ia 打印出来，给 A 相施加已知方向的电流，观察符号是否正确。

#### 4.2.5 小练习与答案

**练习 1**：若 `adc_a=adc_b=adc_c=2048`（三相对称、零电流），算出的 ia/ib/ic 各是多少？这说明什么？

**答案**：\(i_a=2048+2048-2\times2048=0\)，同理 \(i_b=i_c=0\)。这说明零电流时三个 ADC 读数都等于偏置 \(V_\text{off}\)（此处 \(V_\text{off}=2048\)，即满量程中点），重构结果自然为 0，验证了公式的正确性。

**练习 2**：代码用 `{3'b0, adc_a, 1'b0}` 表示 \(2\cdot\mathrm{ADC}_a\)，为什么不用 `2*adc_a`？

**答案**：拼接左移 1 位是纯连线（不产生乘法器），硬件成本为零，且明确是无符号零扩展，没有位宽/符号歧义。写 `2*adc_a` 会调用乘法器、还要担心操作数位宽扩展与符号问题，既慢又易错。这是 RTL 里表达“乘 2 的幂”的推荐写法。

**练习 3**：仿照 ia 的推导，写出 ib 的完整推导，并与第 106 行对照。

**答案**：由对称性，把下标 a↔b 互换即得 \(i_b = \frac{\mathrm{ADC}_a+\mathrm{ADC}_c-2\,\mathrm{ADC}_b}{3R}=k\cdot(\mathrm{ADC}_a+\mathrm{ADC}_c-2\,\mathrm{ADC}_b)\)。代码第 [106](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L106-L106) 行 `{4'b0, adc_a} + {4'b0, adc_c} - {3'b0, adc_b, 1'b0}` 正是 \(\mathrm{ADC}_a+\mathrm{ADC}_c-2\,\mathrm{ADC}_b\)，一致。

---

### 4.3 初始机械角度 Φ 的标定

#### 4.3.1 概念说明

4.1 的公式 \(\psi=N(\varphi-\Phi)\) 里有个 Φ——电角度为 0 时对应的机械角度。它由电机的物理安装位置决定，每次上电都可能不同（转轴停在哪随机），所以**必须每次上电后实地标定**。

标定思路很直接：**主动施加一个已知方向的电压矢量，把转子“拽”到已知电角度，然后读此时的机械角度作为 Φ。** 具体地，初始化阶段让 SVPWM 输出幅值最大（`vs_rho=4095`）、角度为 0（`vs_theta=0`）的电压矢量。这会在定子上产生一个方向固定的强磁场，把转子磁极吸到与它对齐——此时转子的电角度就是 0。记录下这时的机械角度 φ，就是 Φ。

这段逻辑和反 Park 变换写在同一个 `always` 块里（[RTL/foc/foc_top.v:218-237](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L218-L237)），因为它们共同维护 `vs_rho/vs_theta` 这对寄存器：初始化时由标定逻辑驱动，初始化后由反 Park 驱动。

#### 4.3.2 核心流程

用一个计数器 `init_cnt` 从 0 数到 `INIT_CYCLES`（默认 16777216，36.864MHz 下约 0.45 秒）来界定初始化时长。

```
always @(posedge clk or negedge rstn):
    if (~rstn):                                  // 外部复位
        vs_rho, vs_theta, init_cnt, init_phi, init_done = 0
    else if (init_cnt <= INIT_CYCLES):           // 初始化阶段
        vs_rho  = 4095        // 最大幅值，强力拽转子
        vs_theta = 0          // 角度 0，让转子对齐到 ψ=0
        init_cnt = init_cnt + 1
        if (init_cnt == INIT_CYCLES):            // 最后一拍
            init_phi = phi      // 锁存当前机械角度作为 Φ
            init_done = 1       // 宣告初始化结束，同步解复位所有子模块
    else:                                        // 正常运行阶段（反 Park）
        vs_rho  = vr_rho        // 幅值旋转不变：Vsρ = Vrρ
        vs_theta = vr_theta + psi   // 角度加 ψ：Vsθ = Vrθ + ψ
```

要点：

- 初始化持续约 `INIT_CYCLES+1` 个时钟（条件用 `<=` 与 `==`），时长要足够让转子机械地转到位，故 `INIT_CYCLES` 不能太小（README 建议至少 0.45 秒）。
- `init_done` 在最后一拍置 1，下一拍生效；同时 `init_phi` 在同一拍锁存。由于块①②都以 `init_done` 为复位，它们在 `init_done` 跳 1 后的下一个时钟沿才开始用刚刚锁存的 Φ 计算 ψ/ia——时序自洽。
- 反 Park 部分与本讲关系不大（属于 u2-l6 内容），这里只点出它复用同一对寄存器：初始化结束后 `vs_rho/vs_theta` 改由 PI 输出的极坐标电压驱动。

#### 4.3.3 源码精读

完整块见 [RTL/foc/foc_top.v:218-237](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L218-L237)：注释分两段说明了“初始化标定 Φ”和“反 Park 变换”。

初始化阶段强制输出最大零角度矢量，见 [RTL/foc/foc_top.v:226-227](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L226-L227)：

```verilog
vs_rho <= 12'd4095;              //    初始化阶段令 Vsρ 取最大
vs_theta <= 12'd0;               //    初始化阶段令 Vsθ = 0
```

这是标定的“主动施力”动作：最大幅值 + 零角度 = 把转子拽到电角度 0。

最后一拍锁存 Φ 并宣告完成，见 [RTL/foc/foc_top.v:229-232](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L229-L232)：

```verilog
if(init_cnt==INIT_CYCLES) begin  // 若 init_cnt 计数变量 == INIT_CYCLES , 说明初始化即将完成
    init_phi <= phi;             //    记录当前机械角度φ 作为初始机械角度 Φ
    init_done <= 1'b1;           //    令 init_done = 1 ，指示初始化结束
end
```

`init_done=1` 同时是块①②和所有子模块（`clark_tr`/`park_tr`/`pi_controller`/`cartesian2polar`/`hold_detect`，见 [RTL/foc/foc_top.v:120](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L120-L120) 等处的 `.rstn(init_done)`）的解复位信号——所以 Φ 一旦锁存，整条 FOC 流水线就在同一拍同时苏醒，开始用 \(\psi=N(\varphi-\Phi)\) 计算电角度。

初始化结束后的反 Park，见 [RTL/foc/foc_top.v:234-235](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L234-L235)：

```verilog
vs_rho <= vr_rho;                //    反park变换。由于幅值的旋转不变性，Vsρ = Vrρ
vs_theta <= vr_theta + psi;      //    反park变换。由于转子极坐标系是定子极坐标系旋转 ψ 得来，所以 Vsθ = Vrθ + ψ
```

注意 `vs_theta` 用到了 ψ——也就是说，标定好 Φ → 算出 ψ → 反 Park 才能正确把转子坐标的电压转回定子坐标。三个 `always` 块在数据上首尾相扣。

#### 4.3.4 代码实践

**实践目标**：理解“最大幅值+零角度”为什么能标定零点，并算出 `INIT_CYCLES` 与初始化时长的关系。

**操作步骤**：

1. 阅读 [RTL/foc/foc_top.v:225-232](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L225-L232)，确认初始化期间 `vs_rho=4095, vs_theta=0` 持续了 `INIT_CYCLES+1` 拍。
2. 用默认 `INIT_CYCLES=16777216`、`clk=36.864MHz` 计算初始化时长：\(16777217 / 36864000 \approx 0.455\text{s}\)。
3. 思考：为什么用“最大幅值”而不是中等幅值？（提示：要克服转子静摩擦和惯性，可靠地对齐。）
4. 思考：如果 `INIT_CYCLES` 设成 1000（约 27µs），会发生什么？（转子根本来不及转，锁存的 Φ 是错的，电角度零点偏，FOC 失效。）

**需要观察的现象**：上电后电机会先“抖一下”并被吸到某个固定位置停留约 0.45 秒，然后才开始正反交替转动——前 0.45 秒就是标定 Φ 的过程。

**预期结果**：`init_done` 在上电约 0.45 秒后跳 1，`init_phi` 锁存了转子被吸到 ψ=0 时的机械角度。**待本地验证**：可在仿真/硬件上观察 `init_done` 信号何时拉高；但注意本项目的 SIM/ 下没有针对初始化的 testbench（缺电机模型），故这一步主要靠硬件观察或阅读理解。

#### 4.3.5 小练习与答案

**练习 1**：初始化阶段为什么令 `vs_rho=4095`（最大）而不是某个中间值？

**答案**：最大幅值产生最强的定子磁场，产生最大转矩去克服转子的静摩擦与惯性，可靠地把转子磁极吸到与电压矢量方向（θ=0）对齐，从而保证标定到的 Φ 准确。幅值太小可能拽不动转子，导致零点偏差。

**练习 2**：`init_phi` 在哪个时刻、什么条件下被锁存？为什么同时置 `init_done=1`？

**答案**：在 `init_cnt` 计数到等于 `INIT_CYCLES` 的那个时钟上升沿，`init_phi <= phi`，同时 `init_done <= 1'b1`。同时置 1 是因为 `init_done` 是块①②和所有子模块的解复位信号——Φ 锁存的同一拍解复位，下一拍就能用 Φ 算 ψ，时序自洽。

**练习 3**：如果电机带较大负载（转子很难被电压矢量拽动），`INIT_CYCLES` 应该调大还是调小？为什么？

**答案**：调大。负载大时转子从任意位置转到对齐位置需要更长时间，`INIT_CYCLES` 决定了给转子回归的窗口长度；窗口太短转子还没到位就锁存了 Φ，零点就不准。代价是上电到正常工作之间的等待变长。

## 5. 综合实践

**任务**：把本讲三个模块串起来，做一次“上电到第一次有效电流采样”的全链路手算与源码追踪。

假设条件：`POLE_PAIR=7`，`ANGLE_INV=0`，`INIT_CYCLES=16777216`，`clk=36.864MHz`，标定结束时读到的机械角度 `phi=1200`（即 Φ=1200），标定结束后某一控制周期读到 `phi=1300`、`adc_a=3000`、`adc_b=1800`、`adc_c=2200`。

要求：

1. **角度链**：根据 [RTL/foc/foc_top.v:87](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L87-L87) 计算 ψ。先算 \(7\times(1300-1200)=700\)，未超 4096，故 ψ=700（对应 \(700/4096\times360°\approx61.5°\)）。
2. **电流链**：根据 [RTL/foc/foc_top.v:105-107](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L105-L107) 计算：
   - \(i_a = 1800+2200-2\times3000 = -2000\)
   - \(i_b = 3000+2200-2\times1800 = 1600\)
   - \(i_c = 3000+1800-2\times2200 = 200\)
   - 验证 KCL：\(-2000+1600+200 = -200\)，残差来自丢掉的常数增益 k（理论应为 0，被 PID 吸收）。
3. **握手链**：追踪 `en_adc → en_iabc` 的节拍（[RTL/foc/foc_top.v:103](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/foc/foc_top.v#L103-L103)），说明 ia/ib/ic 与 `en_iabc` 同拍出现，随后进入 Clark 变换。
4. **反思**：回答“为什么 FPGA 不需要知道 R 和 \(V_\text{off}\) 也能算出电流？”——因为 KCL 把 \(V_\text{off}\) 用三个 ADC 读数表示出来了，剩下的常数 k 被线性系统的 PID 吸收。

**预期结果**：ψ=700，\(i_a=-2000, i_b=1600, i_c=200\)，三者之和近似为 0。这条链展示了从“两个 12bit 传感器原始读数”到“电角度 + 三相有符号电流”的完整入口预处理，正是 FOC 后续 Clark/Park/PI 的输入。

## 6. 本讲小结

- **角度换算**：电角度 \(\psi=N(\varphi-\Phi)\)，极对数 N=`POLE_PAIR` 决定电周期是机械周期的 1/N；`ANGLE_INV` 用 `generate-if` 选择是否取反以适配装反的传感器。ψ 用 12bit 存储，靠截断自动完成 mod 4096 的角度运算。
- **电流重构**：由“反向放大+偏置”硬件模型与 KCL 联立，推出 \(i_a=\mathrm{ADC}_b+\mathrm{ADC}_c-2\,\mathrm{ADC}_a\)，消去了未知的偏置 \(V_\text{off}\)；剩下的常数增益 k 被 FOC 的线性性 + PID 调参吸收。位宽上用 16bit 零扩展 + `$signed` 补码解释，正确产出有正有负的电流。
- **Φ 的标定**：初始化阶段用“最大幅值 + 零角度”的电压矢量把转子拽到 ψ=0，锁存此时的机械角度为 Φ，并置 `init_done=1` 同步解复位整条流水线。
- **三块首尾相扣**：块③标定 Φ → 块①用 Φ 算 ψ → 块②用 ADC 算 ia/ib/ic；ψ 又被块③的反 Park 用到（\(V_{s\theta}=V_{r\theta}+\psi\)）。三者共用 `init_done` 作为解复位信号，初始化结束时一齐苏醒。
- **工程哲学**：“系数不必精确，能跑就行”——多处刻意丢掉常数系数（如电流重构的 k、Clark 变换多乘的 2），靠 FOC 线性性 + PID 调参消除误差，这是本库贯穿始终的取舍。

## 7. 下一步学习建议

本讲把 FOC 的“入口预处理”讲清了：现在 ia/ib/ic 和 ψ 都已就绪。下一步沿着数据流向下走：

- **下一讲 u2-l3（Clark 变换）**：看 ia/ib/ic 如何变成定子直角坐标的 iα/iβ，重点是用移位加法近似 \(\sqrt{3}\) 的定点技巧——你会发现 Clark 公式里 \(I_\beta=\sqrt{3}(I_b-I_c)\) 中的 \(\sqrt{3}\) 也是被近似处理的，与本讲“丢系数”的思想一脉相承。
- **再下一讲 u2-l4（Park 变换 + sincos）**：看 iα/iβ 如何用 ψ 旋转变换到 dq 坐标系，以及 sincos 模块如何用查表算 sin/cos。
- **建议同步阅读**：README 的 [FAQ 段](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md#L536-L550)（536-559 行）还有对 Clark 系数、cartesian2polar 等的官方解释，与本讲的“系数哲学”互为印证，强烈建议对照读一遍。

如果你想在动手层面加深理解，可以尝试为本讲的“电流重构块”单独写一个 testbench：喂入三路模拟的 ADC 值（模拟 \(V_\text{off}=2048\)、不同相电流），用 gtkwave 看 ia/ib/ic 是否如预期出现正负号与 KCL 关系——这是 u4-l3 仿真方法论里会展开的练习。
