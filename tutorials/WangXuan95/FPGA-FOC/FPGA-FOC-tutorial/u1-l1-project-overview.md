# 项目总览与 FOC 背景

> 本讲是 FPGA-FOC 学习手册的第一篇。它不假设你读过任何一行项目源码，只带你从「这个项目到底在做什么」开始，建立一张可以在后续讲义里反复对照的系统全景图。

---

## 1. 本讲目标

学完本讲后，你应该能够：

- 用一句话说清 **FPGA-FOC 项目要解决什么问题**：用 FPGA 实现电机磁场定向控制（FOC）的**电流环**，也就是做**扭矩控制**。
- 解释 **为什么用 FPGA 而不是 MCU** 来做 FOC（实时性、多路扩展）。
- 对照系统框图，认清 **四大功能块**：传感器控制器（粉）、FOC 固定算法（蓝）、用户自定义逻辑（黄）、外部硬件电路（橙），以及它们的职责边界。
- 说清外部硬件（AS5600 编码器、AD7928 ADC、电机驱动板、电机）与 FPGA 之间的**信号方向**。
- 记住项目的几个关键技术约定：**纯 Verilog、12bit 传感器、16bit 有符号计算、3 路 PWM + 1 路 EN**。

本讲**只看 README 和顶层端口**，不深入任何算法细节——那是第 2 单元的事。

---

## 2. 前置知识

为了读懂本讲，你最好先有以下基础（没有也没关系，下面会用通俗语言补一句）：

| 概念 | 一句话解释 |
| :--- | :--- |
| PMSM / BLDC | 永磁同步电机 / 无刷直流电机，转子是永磁铁，定子是三相线圈。 |
| PWM（脉宽调制） | 用一个高频方波的「高电平占比」来等效一个模拟电压。 |
| 扭矩（力矩） | 让电机转的「劲」，FOC 电流环直接控制的就是它。 |
| I2C / SPI / UART | 三种常见的串行通信协议，分别用来读编码器、读 ADC、和电脑串口通信。 |
| Verilog | 一种硬件描述语言，本项目的全部逻辑都用它写成。 |
| 直角坐标 / 极坐标 | 用 (x,y) 或 (ρ,θ) 表示一个二维矢量，FOC 里会在这两种坐标之间来回换算。 |

> 术语提示：后续会反复出现「电角度 ψ」「机械角度 φ」「d 轴 / q 轴」等词，本讲先用直觉解释，精确数学定义留到第 2 单元。

---

## 3. 本讲源码地图

本讲只涉及少量文件，重点是把「全景」看清楚：

| 文件 | 在本讲的作用 |
| :--- | :--- |
| [README.md](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md) | 项目说明书：系统框图、技术特点、参数表、FAQ。**本讲的主要依据**。 |
| [figures/diagram.png](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/figures/diagram.png) | 系统框图（README 中的「图1」），四大功能块的视觉来源。 |
| [RTL/fpga_top.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v) | FPGA 工程顶层模块。本讲**只看它的端口和连线**，用来验证信号方向。 |

> 提示：README 是中英双语的，本讲引用时优先指向中文段落（README 后半部分），方便对照阅读。

---

## 4. 核心概念与源码讲解

本讲按三个最小模块推进：先弄清「FOC 电流环要解决什么问题」，再拆「系统框图的四大功能块」（本讲核心），最后记「技术特点与关键约定」。

### 4.1 FOC 与电流环：本项目要解决什么问题

#### 4.1.1 概念说明

先建立一个直觉：**无刷电机有三相线圈，想让转子平稳、可控地转，就要让三相线圈里的电流形成一个「旋转的磁场」，并且让这个磁场始终「拽着」转子上的永磁铁。** 这件事可以用两种思路实现：

- **开环**（比如六步换相、简易正弦驱动）：转子转到哪儿、电流怎么变，全靠猜，效率低、抖动大。
- **闭环 FOC（磁场定向控制）**：用传感器实时测出转子的角度，再用数学把三相电流「解耦」成两个独立可控的量——**d 轴电流**（决定磁化，一般压到 0）和 **q 轴电流**（决定扭矩）。这样就能像调旋钮一样精确控制电机的扭矩。

> 直觉：q 轴电流 ≈ 「油门」，d 轴电流 ≈ 「不必要的磁化，尽量归零」。

**只控制扭矩（即 q 轴电流）的这一层闭环，就叫「电流环」**。它是整个电机控制的最内环。本项目实现的**正是这个电流环**，README 把它描述为「一个完整的电流环，可以进行扭矩控制」。

那么为什么用 **FPGA**？FOC 对**传感器采样率**和**算力**都有要求（一个控制周期内要做大量乘加运算），而 FPGA 可以在硬件级别并行流水地完成这些运算，获得更好的**实时性**，也更方便做**多路扩展**和**多路反馈协同**。

#### 4.1.2 核心流程

一个 FOC 电流环的控制周期，本质是一个**反馈闭环**：

```text
        ┌──────────────── 测量 ────────────────┐
        ↓                                      │
  转子角度 φ ──┐                         三相 PWM → 电机
              ├──→ FOC 算法 ──→ 电压矢量 ──→ SVPWM
  三相电流 ────┘    (变换+PI)              ↑
        ↑                                      │
        └──────────────── 采样 ────────────────┘
```

把上图翻译成一句话：**测角度 + 测电流 → 算出该输出多大电压 → 用 PWM 把电压加到电机上 → 电机电流改变 → 下一周期再测。** 这个循环每个控制周期（本项目约 55μs，对应 18kHz）跑一遍。

涉及一点必要的数学：电角度（电气角度）和机械角度的关系是

\[
\psi = N_p \cdot \varphi
\]

其中 \(N_p\) 是电机**极对数**（本项目示例取 7），\(\varphi\) 是机械角度，\(\psi\) 是电角度。极对数的概念会在第 2 单元细讲，这里只要记住「电角度 = 极对数 × 机械角度」即可。

#### 4.1.3 源码精读

README 一开篇就把项目定位讲清楚了：

- [README.md:270-274](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md#L270-L274)：说明本项目是「基于 FPGA 的磁场定向控制，用于驱动 PMSM / BLDC」，并用一句话点出价值——**用 FPGA 实现 FOC 可以获得更好的实时性，并且更方便多路扩展和多路反馈协同**。这段同时说明本库实现的是「基于角度传感器的有感 FOC，即一个完整的电流环，可以进行扭矩控制」。

  对应的英文版定义在 [README.md:10-14](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md#L10-L14)，措辞是「implements a complete **current loop** which can perform **torque control**」。

- README 还给出了一个最直观的「行为级」证据：示例程序让电机**顺时针、逆时针交替**运行，这就是在反复切换 q 轴目标电流 `iq_aim` 的正负号，见 [README.md:306](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md#L306)。这段直接印证了「电流环 = 扭矩控制」。

- 此外，README 明确说 PI 控制器的参数 `Kp`、`Ki` 是 `foc_top` 的**输入端口**，可以在运行时调整，见 [README.md:387-393](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md#L387-L393)。这说明电流环是一个**可调参的闭环**，而不是死写死的逻辑。

#### 4.1.4 代码实践

**实践目标**：用 README 里的原话，建立「电流环 = 扭矩控制 = 控制 q 轴电流」的对应关系。

**操作步骤**：

1. 打开 [README.md](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md)，定位到中文「示例程序：让电机转起来」一节（约 [L304-L308](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md#L304-L308)）。
2. 找到「用串口监视电流环」一节里串口打印的 4 列数据说明（[README.md:403](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md#L403)），看清楚这 4 列分别是 d 轴实际值、d 轴目标值、q 轴实际值、q 轴目标值。
3. 观察其中一列数据：q 轴目标值在 `+200` 和 `-200` 之间跳变时，q 轴实际值能不能跟上。

**需要观察的现象**：示例串口输出里，q 轴实际值（第 3 列）会跟着 q 轴目标值（第 4 列）从 +200 跳到 -200，说明闭环生效。

**预期结果**：你能用自己的话回答「为什么电机一会儿正转一会儿反转」——因为用户逻辑在反复翻转 q 轴目标电流，而电流环让实际电流跟上了目标。

> 说明：本实践是**源码阅读型**，不需要运行硬件，只需阅读 README 文本即可。

#### 4.1.5 小练习与答案

**练习 1**：FOC 控制的是电机的哪个物理量？是位置、速度，还是扭矩？

> **参考答案**：是**扭矩**。本项目实现的是「电流环」，它直接控制的是 q 轴电流，而 q 轴电流决定扭矩。位置和速度是更外层的环（位置环 / 速度环），不在本库范围内，需要用户自己在外面包一层。

**练习 2**：为什么作者选择用 FPGA 而不是普通单片机来实现 FOC？

> **参考答案**：FOC 对采样率和算力要求高，FPGA 能用硬件流水线并行完成每个控制周期内的大量乘加运算，**实时性更好**；同时 FPGA 天然适合**多路扩展**（驱动多个电机）和**多路反馈协同**。

---

### 4.2 系统框图的四大功能块（本讲核心模块）

#### 4.2.1 概念说明

这是本讲最重要的模块。README 的「图1」把整个系统分成了**四种颜色**的区块，理解这四种颜色的职责边界，就等于拿到了全项目的导航地图：

| 颜色 | 名称 | 位置 | 职责 | 改动频率 |
| :--- | :--- | :--- | :--- | :--- |
| 🟪 粉色 | 传感器控制器 | FPGA 内 | 读角度传感器、读相电流 ADC（**硬件相关**） | 换型号才改 |
| 🟦 蓝色 | FOC 固定算法 | FPGA 内 | Clark/Park/PI/SVPWM 等核心数学链路（**硬件无关**） | 一般不动，是核心 |
| 🟨 黄色 | 用户自定义逻辑 | FPGA 内 | 决定电机怎么转、监视哪些变量 | 用户自由改 |
| 🟧 橙色 | 外部硬件电路 | FPGA 外 | 电机、电机驱动板、角度传感器、ADC | 硬件层面 |

> 直觉：**粉色和橙色是「和具体芯片绑死」的边角，蓝色是「算法内核」不动如山，黄色是「你的地盘你做主」。** 这种分层是作者刻意设计的封装边界。

#### 4.2.2 核心流程

把四大功能块和外部硬件按**数据流向**串起来，就是下面这张「文字流程图」（箭头表示信号方向）：

```text
【角度通路】
  AS5600(橙) ──I2C──▶ i2c_register_read(粉) ──phi 机械角度──▶ foc_top(蓝)

【电流通路】
  foc_top(蓝) ──sn_adc 脉冲──▶ adc_ad7928(粉) ──SPI──▶ AD7928(橙)
  AD7928(橙) ──SPI──▶ adc_ad7928(粉) ──adc_value_a/b/c + en_adc──▶ foc_top(蓝)

【驱动通路】
  foc_top(蓝) ──pwm_a/b/c, pwm_en──▶ 电机驱动板(橙) ──▶ 电机(橙)

【监视通路】
  foc_top(蓝) ──id/iq + en_idq──▶ uart_monitor(黄) ──uart_tx──▶ 电脑串口

【用户逻辑】
  用户逻辑(黄) ──id_aim/iq_aim 目标值──▶ foc_top(蓝)
```

要点速记：

- 角度（`phi`）和电流（`adc_value_*`）是**输入**到蓝色 FOC 核心；PWM 是蓝色核心的**输出**。
- `sn_adc` / `en_adc` 是粉色 ADC 控制器与蓝色核心之间的**握手脉冲**：核心说「该采样了」(sn_adc)，ADC 控制器采样完说「数据有效」(en_adc)。
- 黄色用户逻辑把**目标电流** `id_aim` / `iq_aim` 喂给蓝色核心，决定电机怎么转。

#### 4.2.3 源码精读

README 的「设计代码详解」一节是四大功能块定义的权威出处：

- [README.md:453-460](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md#L453-L460)：逐色说明四大块。重点三句——粉色是「传感器控制器，硬件相关，换型号要重写」；蓝色是「FOC 固定算法，硬件无关，是本库的核心代码」；黄色是「用户自定义逻辑，可改 user behavior 实现各种应用，也可改 uart_monitor 监视别的变量」。这段还强调：除 `altpll` 原语外，**全库纯 RTL，可移植到 Xilinx/Lattice**。

- [README.md:433-446](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md#L433-L446)：列出全部 12 个 `.v` 文件并标注「备注」列，其中明确把 `foc_top.v` 及其下的 clark/park/sincos/pi/cartesian2polar/svpwm/hold_detect 标为「固定功能，一般不需要改动」（蓝色），把 `uart_monitor.v` 标为「不需要的话可以移除」（黄色）。

把抽象的四大块落到具体连线上，顶层 [fpga_top.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v) 给出了最直接的证据：

- 端口定义见 [fpga_top.v:11-28](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L11-L28)，可以看到 `i2c_scl/i2c_sda`（接 AS5600）、`spi_*`（接 AD7928）、`pwm_a/b/c/pwm_en`（接驱动板）、`uart_tx`（接电脑）这些对外信号。
- 粉色块：I2C 读角度的例化在 [fpga_top.v:60-73](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L60-L73)，读出 12bit 机械角度 `phi`（见 [fpga_top.v:34](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L34)）；SPI 读 ADC 的例化在 [fpga_top.v:78-100](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L78-L100)，读出 `adc_value_a/b/c`。
- 蓝色块：FOC 核心例化在 [fpga_top.v:105-132](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L105-L132)，它同时接 `phi`、`adc_*`（输入）和 `pwm_*`（输出），正是数据流的交汇点。
- 黄色块：用户逻辑（让 `iq_aim` 顺逆交替）在 [fpga_top.v:136-154](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L136-L154)；UART 监视器例化在 [fpga_top.v:159-170](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L159-L170)。

> 这些行号在第 1 单元后续讲义（顶层模块、目录结构）会逐段精读，本讲你只要知道「四大块在顶层里各对应哪几行」即可。

#### 4.2.4 代码实践

> 这是本讲对应的核心实践任务。

**实践目标**：不看任何图，**用自己的话把系统框图画出来**，并标出每个外部硬件的位置和所有信号方向。

**操作步骤**：

1. 先**合上**本讲义，只看着 [figures/diagram.png](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/figures/diagram.png)（README 图1）观察 30 秒。
2. 在纸上（或任意画图工具）画一个大方框代表 **FPGA**，再在外面画四个小方框，分别写：**AS5600（角度传感器）**、**AD7928（ADC）**、**电机驱动板（含 MP6540）**、**电机**。
3. 在 FPGA 方框内，用三种颜色或三个区域标出：粉色（传感器控制器）、蓝色（FOC 固定算法）、黄色（用户逻辑）。
4. 用**带箭头的线**把信号连起来，每条线标注信号名和方向，至少要画出：
   - AS5600 →(I2C)→ 粉色 → `phi` → 蓝色
   - 蓝色 → `sn_adc` → 粉色 →(SPI)→ AD7928；AD7928 →(SPI)→ 粉色 → `adc_value_a/b/c` + `en_adc` → 蓝色
   - 蓝色 → `pwm_a/b/c`, `pwm_en` → 电机驱动板 → 电机
   - 黄色 → `id_aim/iq_aim` → 蓝色；蓝色 → `id/iq` → 黄色 → `uart_tx` → 电脑
5. 画完后，对照 [README.md:433-460](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md#L433-L460) 检查自己有没有漏掉某个块或画反方向。

**需要观察的现象**：你会发现自己画出的图和 README 图1 的拓扑一致——**FPGA 内部三块（粉/蓝/黄）+ 外部一块（橙）**，且角度、电流是流入蓝色的，PWM 是流出蓝色的。

**预期结果**：能独立画出一张包含四大功能块、四个外部硬件、至少 5 条带方向信号线的系统框图。

> 说明：这是**源码阅读 + 画图型实践**，无需运行任何工具。如果你愿意，可以把画好的图和 `figures/diagram.png` 做对比，找出自己遗漏的信号。

#### 4.2.5 小练习与答案

**练习 1**：如果把角度传感器从 AS5600 换成另一款 I2C 编码器，需要改哪个颜色的块？蓝色块要改吗？

> **参考答案**：只改**粉色块**（`i2c_register_read.v` 里的从机地址、寄存器地址等），以及橙色的外部硬件。**蓝色块完全不用动**——这正是「硬件相关 vs 硬件无关」分层的好处。

**练习 2**：`sn_adc` 和 `en_adc` 这对信号分别由谁发出、表示什么？

> **参考答案**：`sn_adc` 由**蓝色 FOC 核心**发出（[fpga_top.v:117](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L117) 标为 output），是一个单周期高电平脉冲，意思是「现在该采样三相电流了」；`en_adc` 由**粉色 ADC 控制器**发出（[fpga_top.v:91](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L91) 标为 output），意思是「三通道采样结束，结果已同步提交」。

**练习 3**：黄色用户逻辑里的 `iq_aim` 是输入到蓝色核心，还是从蓝色核心输出？

> **参考答案**：是**输入到**蓝色核心（在 [fpga_top.v:130](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L130) 里 `.iq_aim(iq_aim)` 对应 `foc_top` 的 input）。也就是说，用户逻辑产生「目标」，蓝色核心去「实现」这个目标。

---

### 4.3 技术特点与关键约定

#### 4.3.1 概念说明

在进入算法细节之前，先记住全项目统一的几个**技术约定**，它们会贯穿后续所有讲义：

1. **纯 Verilog（平台无关）**：除了 `fpga_top.v` 里调用了一个 Altera 专用的 `altpll` 锁相环原语，其它所有代码都是纯 RTL，可以无缝移植到 Xilinx、Lattice 等其它 FPGA。
2. **3 路 PWM + 1 路 EN**：`pwm_a/b/c` 三相 PWM，每相 PWM=1 时上桥臂 MOS 导通、PWM=0 时下桥臂导通；另有一路 `pwm_en`，当 EN=0 时 6 个 MOS 管全部关断。
3. **12bit 分辨率传感器**：角度传感器和相电流 ADC 都按 **12bit** 对接（取值 0~4095）。>12bit 的传感器要低位截断，<12bit 的要低位填充。
4. **16bit 有符号整数计算**：内部统一用 16bit 有符号数（`signed [15:0]`）做运算——因为传感器只有 12bit，16bit 计算裕量足够。
5. **统一节拍**：主时钟 `clk` 的频率决定一切——**控制频率 = clk / 2048**，且它同时等于电流采样率、PID 更新率、SVPWM 占空比更新率。示例 `clk = 36.864MHz`，所以控制频率 = 18kHz。

> 直觉：**12bit 进、16bit 算、PWM 出**，这三句话概括了数据在定点世界的位宽旅程，第 4 单元会专门讲定点约定。

#### 4.3.2 核心流程

这些约定如何串成一个可运行的系统：

```text
  50MHz 晶振 ──altpll──▶ clk=36.864MHz(主时钟)
                              │
                              ├──▶ 控制频率 = clk/2048 = 18kHz
                              │        (采样率 = PID 率 = SVPWM 率)
                              │
   12bit 角度/电流 ─▶ 16bit 有符号运算 ─▶ 3路PWM+EN ─▶ 电机
```

一个关键约束来自硬件：`adc_ad7928.v` 会把 `clk` 二分频生成 SPI 时钟 `spi_sck`，而 AD7928 芯片要求 SPI 时钟 ≤ 20MHz，所以**主时钟 `clk` 不能超过 40MHz**。

#### 4.3.3 源码精读

- 技术特点的权威列表见 [README.md:282-287](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md#L282-L287)（中文「技术特点」小节），逐条对应上面的 4 个约定（纯 RTL、3 PWM+EN、12bit、16bit 有符号）。

- 关于时钟和节拍，见 [README.md:354-358](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md#L354-L358)：明确「主时钟可取小于 40MHz 的任意值」「SVPWM 频率 = clk/2048」「选 36.864MHz 是为了凑出 18kHz 整数」，并解释了 40MHz 上限来自 ADC 的 SPI 时钟约束。

- 调参相关的 5 个关键参数表见 [README.md:379-385](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md#L379-L385)，本讲你只需大致认得这些名字（`INIT_CYCLES`、`ANGLE_INV`、`POLE_PAIR`、`MAX_AMP`、`SAMPLE_DELAY`），精确调参留到第 4 单元。这些参数在顶层例化处也能看到，见 [fpga_top.v:105-110](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L105-L110)。

#### 4.3.4 代码实践

**实践目标**：把「12bit 进、16bit 算、统一节拍」这三条约定在源码里定位到具体证据。

**操作步骤**：

1. 打开 [fpga_top.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v)，找到 `phi`、`adc_value_a/b/c` 的位宽声明（[fpga_top.v:34-40](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L34-L40)），确认它们都是 `[11:0]`（即 12bit）。
2. 找到 `id`、`iq`、`id_aim`、`iq_aim` 的声明（[fpga_top.v:43-46](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L43-L46)），确认它们是 `signed [15:0]`（即 16bit 有符号）。
3. 找到 `clk` 的注释（[fpga_top.v:32](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L32)），读一遍「控制频率 = 时钟频率 / 2048」以及括号里关于采样率/PID/SVPWM 三者相等的说明。

**需要观察的现象**：位宽声明和注释与 README 的技术特点完全一致——输入侧是 12bit，内部运算侧是 16bit 有符号，节拍统一为 clk/2048。

**预期结果**：你能指着具体行号说出「这里就是 12bit、这里就是 16bit 有符号、这里定义了 18kHz 节拍」。

> 说明：本实践为**源码阅读型**，无需运行。位宽和注释都是静态文本，可直接核对。

#### 4.3.5 小练习与答案

**练习 1**：为什么项目内部用 16bit 有符号数计算，而不是 32bit 或更高？

> **参考答案**：因为传感器只有 **12bit** 分辨率，数据本身的有效精度最多 12bit，16bit 有符号数已经留出了 4bit 的运算裕量，**够用且省资源**。用更宽的位宽纯属浪费 FPGA 的逻辑资源。

**练习 2**：如果换一块晶振是 100MHz 的开发板，主时钟 `clk` 能直接用 100MHz 吗？

> **参考答案**：**不能**。主时钟受 AD7928 的 SPI 时钟（≤20MHz）约束，**不能超过 40MHz**。正确做法是用 PLL 把 100MHz 分频/倍频到约 **36.864MHz**（或任意 <40MHz 的值），保持 SVPWM 频率 = clk/2048 为合理值（如 18kHz）。

**练习 3**：`pwm_en=0` 时电机会怎样？

> **参考答案**：`pwm_en` 是三相共用的使能信号，**EN=0 时 6 个 MOS 管全部关断**（见 [README.md:285](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md#L285)），电机失去驱动。这是紧急停机/上电初始化期间的安全态。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个**贯穿性任务**——「**口述一个完整控制周期**」：

> 假设电机正在按示例程序运行（顺逆交替转动），请你像给一个新人讲解一样，**用一段连贯的话**，描述**一个控制周期（约 55μs）内发生的事情**，要求覆盖以下要点：
>
> 1. 从 **AS5600** 读到什么、经过哪个粉色模块、以什么信号名进入蓝色核心；
> 2. 蓝色核心在什么时候、用什么信号通知 **AD7928** 该采样了；AD7928 采完三相电流后，结果如何回到蓝色核心；
> 3. 蓝色核心算完后，输出什么信号去**电机驱动板**，最终驱动电机；
> 4. 黄色用户逻辑在这个周期里提供了什么（`id_aim`/`iq_aim`），UART 又把什么发给电脑；
> 5. 整条链路里，哪些信号是 12bit、哪些是 16bit 有符号、节拍统一为多少。

**验收标准**：你的这段话应该让一个完全没读过源码的人，能大致复述出「测角度+测电流 → FOC 算电压 → PWM 驱动电机 → 串口回报」这条闭环，并说清四大功能块各自的分工。如果某一步你说不清楚，就回到 [4.2.3 源码精读](#423-源码精读) 对照行号补全。

> 说明：本任务是**口述/书写型综合实践**，不涉及运行工具，目的是把「全景图」内化为自己的表达。

---

## 6. 本讲小结

- **FPGA-FOC 项目**用 FPGA 实现电机的 **FOC 电流环（扭矩控制）**，是最内层的控制环；选择 FPGA 是为了更好的**实时性**和**多路扩展**能力。
- 系统框图把世界分成**四大功能块**：粉色（传感器控制器，硬件相关）、蓝色（FOC 固定算法，硬件无关，核心）、黄色（用户自定义逻辑）、橙色（FPGA 外部硬件）。
- **数据流**：角度（`phi`）和三相电流（`adc_value_*`）流入蓝色核心；蓝色核心输出 `pwm_a/b/c` 与 `pwm_en` 驱动电机；黄色逻辑提供目标电流 `id_aim/iq_aim`，并把实际 `id/iq` 通过 UART 回报电脑。
- **统一约定**：纯 Verilog（除 `altpll`）、12bit 传感器、16bit 有符号运算、3 路 PWM + 1 路 EN；主时钟 `clk < 40MHz`，控制频率 = clk/2048（示例 18kHz）。
- 顶层 [fpga_top.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v) 把四大块用具体连线实例化出来，是验证信号方向最直接的依据。

---

## 7. 下一步学习建议

你已经有了全景图，下一步建议按以下顺序深入：

1. **下一讲 [u1-l2 目录结构与模块层次](u1-l2-directory-and-hierarchy.md)**：把 RTL/ 下的 12 个 `.v` 文件逐一对应到四大功能块，搞清「平台无关」的边界到底画在哪里。
2. **[u1-l3 顶层模块 fpga_top.v](u1-l3-fpga-top.md)**：逐段精读顶层，看 PLL、各子模块例化和用户逻辑的具体写法——本讲里那些行号会在那里被完整展开。
3. **[u1-l4 用 iverilog 跑仿真](u1-l4-iverilog-simulation.md)**：动手跑两个 testbench，用 gtkwave 看正弦波和马鞍波，从波形层面直观感受 FOC 的数学链路。
4. 之后再进入**第 2 单元**，沿着 `foc_top.v` 的数据流逐模块拆解 Clark/Park/PI/SVPWM 等核心算法。

> 推荐先读的背景资料：README 末尾「参考资料」列出了多篇 FOC 入门文章与视频（[README.md:578-588](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md#L578-L588)），如果对 FOC 数学完全陌生，建议先看其中一两篇建立直觉。
