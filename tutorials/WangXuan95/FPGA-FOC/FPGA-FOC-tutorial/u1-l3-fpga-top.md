# 顶层模块 fpga_top.v

## 1. 本讲目标

学完本讲，你应该能够：

- 看懂 `fpga_top.v` 如何用 `altpll` 原语把开发板上的 50MHz 晶振变成 FOC 系统的主时钟 `clk`，并理解为什么这个时钟频率不能超过 40MHz。
- 读懂 `fpga_top.v` 对外的全部 IO 引脚（PWM、SPI、I2C、UART）分别接到什么外部硬件。
- 理解顶层是如何通过**例化（instantiate）**四个子模块，用 `wire` 把「角度传感器 → ADC → FOC 核心 → UART 监视」连成一条完整的数据通路的。
- 读懂示例中的**用户逻辑**：用一个 24bit 自增计数器 `cnt`，按 `cnt[23]` 让目标电流 `iq_aim` 在 +200 和 −200 之间交替，从而让电机一会正转一会反转。

本讲是整个学习手册的「接线图」：它把上一讲（u1-l2）里抽象的「粉色/蓝色/黄色」三个区域，落到一段真实的、可以综合的 Verilog 代码上。

## 2. 前置知识

在开始之前，你需要具备以下基础概念（不熟悉的话先回顾 u1-l1、u1-l2）：

- **模块例化（instantiation）**：Verilog 里在一个模块内部「调用」另一个模块，并把双方的端口用 `wire` 连起来的写法，类似于 C 语言里调用函数并把返回值接到变量上。
- **wire 与 reg**：`wire` 是「线」，只能用 `assign` 持续赋值或作为模块端口的连线；`reg` 是「寄存器」，在 `always` 块里被赋值，对应时钟沿更新的触发器。
- **PLL（锁相环）**：FPGA 内部的模拟硬核，能把输入的一路时钟倍频/分频成另一路频率的时钟，并且输出一个「锁定（locked）」信号表示输出时钟已经稳定。
- **复位信号 `rstn`**：低电平有效的复位（`n` 表示 active-low）。复位期间电路保持初始状态，复位释放后才开始正常工作。
- **系统框图的四大色块**（见 u1-l1）：粉色＝传感器外设控制器、蓝色＝FOC 固定算法、黄色＝用户自定义逻辑、橙色＝FPGA 外部硬件。

本讲只聚焦在 **顶层如何把各部分接线**，不深入任何子模块的内部算法（那是 u2/u3 各讲的任务）。

## 3. 本讲源码地图

本讲只涉及一个文件，但它承担了「工程顶层」的全部职责：

| 文件 | 作用 | 在系统框图中的角色 |
| :-- | :-- | :-- |
| [RTL/fpga_top.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v) | FPGA 工程的顶层模块：生成时钟、定义对外 IO、例化四个子模块、承载示例的用户逻辑 | 同时扮演「时钟源」「连线背板」「用户逻辑容器」三重角色 |

`fpga_top.v` 自身不实现任何 FOC 算法，它的价值在于**把四个独立模块像搭积木一样拼起来**：

- `altpll`（Altera 原语）：产生主时钟 —— 硬件相关。
- `i2c_register_read`：读 AS5600 角度 —— 粉色（硬件相关）。
- `adc_ad7928`：读 AD7928 三相电流 —— 粉色（硬件相关）。
- `foc_top`：FOC + SVPWM 核心算法 —— 蓝色（硬件无关，核心）。
- `uart_monitor`：串口打印 —— 黄色（用户逻辑）。

其中 `cnt` 计数器与 `iq_aim` 交替逻辑也属于黄色（用户逻辑）。

---

## 4. 核心概念与源码讲解

### 4.1 altpll：把 50MHz 晶振变成主时钟 clk

#### 4.1.1 概念说明

FPGA 板子上焊的晶振（本例是 50MHz）只提供一个「原始时钟」，但 FOC 系统想要一个**特定的主时钟频率** `clk`，原因有二：

1. **SVPWM 频率由 `clk` 决定**：SVPWM 频率 = `clk` 频率 / 2048。作者希望 SVPWM 正好是 18kHz（一个常见的、安静的电机调制频率），这就要求 `clk` ≈ 36.864MHz。
2. **整数分频凑波特率**：作者希望 UART 波特率（115200）、SVPWM 频率（18kHz）都能被 `clk` 整除，所以选了一个能被这些数整除的频率。

`altpll` 是 **Altera Cyclone IV** 系列 FPGA 专用的锁相环原语（primitive）。它不是用 Verilog 写的，而是 FPGA 厂商提供的「黑盒」硬核 IP。这也是全库**唯一**一个与厂商绑死的部分——移植到 Xilinx 时要用 clock wizard 替换它（见 u4-l2）。

#### 4.1.2 核心流程

`altpll` 的工作可以概括为三步：

1. 接收输入时钟 `clk_50m`（50MHz）。
2. 内部 VCO（压控振荡器）倍频/分频，按参数算出输出频率。
3. 输出稳定后，把 `locked` 引脚拉高，表示「时钟已锁定，可以使用」。

输出频率由两个参数决定：

\[ f_{clk} = f_{in} \times \frac{\text{multiply\_by}}{\text{divide\_by}} = 50\,\text{MHz} \times \frac{73}{99} \approx 36.87\,\text{MHz} \]

> 说明：73/99 算出的精确值约 36.8687MHz，工程文档（README 与代码注释）为了凑「36864/2048=18」这个整除关系，把它记作 36.864MHz，两者差异极小（<0.02%），对控制无影响。

由这个 `clk` 衍生的关键节拍：

\[ f_{ctrl} = f_{SVPWM} = \frac{f_{clk}}{2048} \approx \frac{36864\,\text{kHz}}{2048} = 18\,\text{kHz} \quad (\text{周期} \approx 55.6\,\mu\text{s}) \]

这个 18kHz 就是「控制频率 ＝ 电流采样率 ＝ PID 更新率 ＝ SVPWM 占空比更新率」——全库统一的节拍。

#### 4.1.3 源码精读

PLL 的例化与参数在文件中部，核心是这两行：

[RTL/fpga_top.v:50-55](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L50-L55) —— 这是 altpll 的例化与 `defparam` 参数表，它把 50MHz 变成约 36.864MHz，并把锁定信号送给 `rstn`。

关键的端口连接（第 53 行，精简后）：

```verilog
wire [3:0] subwire0;
altpll u_altpll ( .inclk ( {1'b0, clk_50m} ), .clk ( {subwire0, clk} ), .locked ( rstn ), ... );
```

- `.inclk ( {1'b0, clk_50m} )`：把 50MHz 接到 PLL 的输入时钟（高位补 0，因为 `inclk` 是 2bit 端口，支持备用时钟）。
- `.clk ( {subwire0, clk} )`：输出时钟的高 4bit 丢给一个不用的 `subwire0`，最低位才是我们要的 `clk`。
- `.locked ( rstn )`：**PLL 锁定信号直接当系统复位**。上电瞬间 `rstn=0`，全系统保持复位；等 PLL 输出稳定后 `rstn=1`，系统才开始跑。这是一个很巧妙的设计——保证所有寄存器在时钟稳定前都不会乱动。

关键的频率参数（第 54 行 `defparam`）：

```verilog
u_altpll.clk0_divide_by   = 99,
u_altpll.clk0_multiply_by = 73,
u_altpll.inclk0_input_frequency = 20000,   // 单位 ps，20000ps=20ns，即 50MHz
u_altpll.intended_device_family    = "Cyclone IV E",
```

- `inclk0_input_frequency = 20000`：单位是皮秒（ps），20000ps = 20ns，对应 1/20ns = 50MHz，确认输入是 50MHz。
- `multiply_by=73, divide_by=99`：决定了输出频率 = 50 × 73/99 ≈ 36.87MHz。

**为什么 `clk` 不能超过 40MHz？** 这不是 PLL 的限制，而是 ADC 芯片的限制：`adc_ad7928.v` 内部对 `clk` 二分频产生 SPI 时钟 `spi_sck`，而 AD7928 芯片要求 `spi_sck ≤ 20MHz`，所以 `clk` 必须 ≤ 40MHz。这一点 README 的「时钟配置」一节也明确说明了。

> 拓展：第 55 行还有一句被注释掉的代码 `//assign rstn=1'b1; assign clk=clk_50m;`。这是「PLL 旁路」写法——做纯仿真、或者在没有 altpll 支持的工具链里，可以直接用 50MHz 当 `clk`、复位常拉高。本讲后续以使用 PLL 的版本为准。

#### 4.1.4 代码实践

**实践目标**：亲手验证 PLL 参数与输入频率标注是否自洽。

**操作步骤**：

1. 打开 [RTL/fpga_top.v:54](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L54)。
2. 找到 `clk0_multiply_by` 和 `clk0_divide_by` 两个参数，记录它们的值。
3. 用公式 \( f_{out} = 50\,\text{MHz} \times \frac{\text{multiply\_by}}{\text{divide\_by}} \) 算出输出频率。
4. 找到 `inclk0_input_frequency`，把皮秒值换算成频率，验证它确实代表 50MHz 输入。

**需要观察的现象 / 预期结果**：

- `multiply_by=73`，`divide_by=99`，输出 ≈ 36.87MHz。
- `inclk0_input_frequency=20000`ps = 20ns → 50MHz，与 `clk_50m` 端口注释「连接 50MHz 晶振」一致。
- 算出的 36.87MHz 与注释里的「36.864MHz」基本相符，差异来自 73/99 无法精确等于 0.73728。

**待本地验证**：若你手头是 Xilinx/Vivado，本段无法直接综合（altpll 是 Altera 专属），需替换为 clock wizard；iverilog 仿真时可启用第 55 行的旁路 `assign` 来绕过 altpll。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `multiply_by` 改成 64、`divide_by` 改成 99，输出频率是多少？此时 SVPWM 频率是多少？是否仍满足 ≤40MHz？

**参考答案**：\( 50 \times 64/99 \approx 32.32\,\text{MHz} \)，SVPWM 频率 = 32320/2048 ≈ 15.78kHz；仍 ≤ 40MHz，可行（只是控制频率不再是整数 18kHz）。

**练习 2**：为什么作者不直接用 50MHz 当 `clk`，而要费事加一个 PLL？

**参考答案**：因为 50MHz 不能被 2048 整除得到一个「漂亮」的整数 SVPWM 频率（50000000/2048≈24414Hz），而且 50MHz 会让 SPI 时钟 = 25MHz > 20MHz，违反 AD7928 的规格。选 ≈36.864MHz 既能让 SVPWM=18kHz，又能让 SPI 时钟 ≈18.4MHz < 20MHz，一举两得。

---

### 4.2 fpga_top 的对外 IO 与内部连线

#### 4.2.1 概念说明

顶层模块的端口（`module` 后面括号里那一串 `input/output`）就是 FPGA 芯片**真实的物理引脚**。综合时，每个端口都会被约束到开发板上的一个具体引脚号（在 Quartus/Vivado 的引脚约束文件里配置）。

除了对外引脚，顶层还声明了一堆内部 `wire`，它们的作用是**在不同子模块之间牵线**——就像面包板上的跳线。理解本节的关键，是把每个引脚/连线对应到系统框图（u1-l1 的图1）里的某根信号。

#### 4.2.2 核心流程

顶层信号可以分为三组：

1. **对外 IO**：PWM（4 根）、SPI（4 根）、I2C（2 根）、UART（1 根）、时钟（1 根）。
2. **时钟与复位**：`clk`、`rstn`（由 PLL 产生）。
3. **内部数据连线**：`phi`（角度）、`sn_adc`/`en_adc`/`adc_value_*`（采样握手）、`id`/`iq`/`id_aim`/`iq_aim`（电流）、`en_idq`（电流有效脉冲）。

#### 4.2.3 源码精读

[RTL/fpga_top.v:11-28](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L11-L28) —— 这是 `fpga_top` 的全部对外端口。逐组对应外部硬件：

| 端口 | 方向 | 连接的外部硬件 | 说明 |
| :-- | :-- | :-- | :-- |
| `clk_50m` | input | 50MHz 晶振 | 唯一的时钟来源 |
| `pwm_en` | output | 电机驱动板 EN | `=0` 时 6 个 MOS 全关断 |
| `pwm_a/b/c` | output | 电机驱动板 3 相 PWM | `=1` 上桥臂导通，`=0` 下桥臂导通 |
| `spi_ss/sck/mosi` | output | AD7928 ADC | SPI 主机输出 |
| `spi_miso` | input | AD7928 ADC | SPI 主机输入 |
| `i2c_scl` | output | AS5600 磁编码器 | I2C 时钟 |
| `i2c_sda` | **inout** | AS5600 磁编码器 | I2C 双向数据（开漏） |
| `uart_tx` | output | UART 转 USB 模块 | 单向发送，**可不接** |

注意 `i2c_sda` 是唯一的 `inout`（双向）端口，因为 I2C 协议要求主机和从机都能驱动同一根数据线（开漏+上拉）。这一点会在 u3-l1 详解。

[RTL/fpga_top.v:31-46](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L31-L46) —— 这是内部连线声明，理解它们是看懂后续数据通路的前提。几条最关键的线：

```verilog
wire        [11:0] phi;          // 机械角度，0~4095 对应 0~360°
wire               sn_adc;       // foc_top 命令 ADC「开始采样」的脉冲
wire               en_adc;       // ADC 通知 foc_top「结果已就绪」的脉冲
wire signed [15:0] id, iq;       // 实际 d/q 轴电流（有符号！）
wire signed [15:0] id_aim;       // d 轴目标电流（wire，常量 0）
reg  signed [15:0] iq_aim;       // q 轴目标电流（reg，由 always 驱动）
```

要点：

- `phi` 是 **12bit 无符号**（0~4095），对应 AS5600 的一圈机械角度。
- `id/iq/id_aim/iq_aim` 都是 **16bit 有符号**（`signed`），因为电流可正可负——这正是 u1-l1 提到的「16bit 有符号计算」约定。
- `id_aim` 是 `wire`（用 `assign` 接常量），`iq_aim` 是 `reg`（用 `always` 动态切换）。两者的声明方式不同，因为驱动方式不同。

#### 4.2.4 代码实践

**实践目标**：把每个端口映射到开发板上的真实引脚。

**操作步骤**：

1. 打开 [RTL/fpga_top.v:11-28](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L11-L28)。
2. 对照 README 的「引脚约束」一节（中文版在第 360-371 行），列出每个端口该接什么。
3. 数一数：本工程至少需要多少个 3.3V IO？

**预期结果**：I2C(2) + SPI(4) + PWM(3) + EN(1) + UART(1) + clk(1) = **12 个 IO**（README 说「至少 10 个」，因为 UART 可不接、clk 往往是固定专用引脚）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `spi_miso` 是 `input` 而 `spi_mosi` 是 `output`？

**参考答案**：从 FPGA 的视角看，MOSI（Master Out Slave In）是 FPGA 发给 ADC 的，所以是 output；MISO（Master In Slave Out）是 ADC 发回给 FPGA 的，所以是 input。命名是相对「主机（FPGA）」而言的。

**练习 2**：`id` 和 `iq` 为什么要声明成 `signed`，而 `phi` 不用？

**参考答案**：电流有正负（正代表一个方向，负代表反方向），必须用有符号数才能正确做加减和比较；`phi` 是 0~4095 的角度值，永远非负，用无符号即可。

---

### 4.3 四大子模块的例化与数据通路

#### 4.3.1 概念说明

`fpga_top.v` 的「主体」就是四个模块例化。每个例化做两件事：

1. **传参数**（`#(.PARAM(value))`）：在综合时把常量灌进去，比如 I2C 的从机地址、ADC 的通道映射、FOC 的极对数。
2. **连端口**（`.port(wire)`）：把模块的端口接到顶层的 `wire` 上。

把这些例化按数据流向画出来，就是一张完整的「角度进 → PWM 出」的数据通路图。本节不展开任何子模块内部，只看它们怎么被「拼」起来。

#### 4.3.2 核心流程

整个系统的数据流可以归纳成一个闭环：

```
        ┌──────────────────── foc_top (蓝色核心) ────────────────────┐
        │                                                            │
角度 phi│  ┌──────────────────────────────────────────────────────┐  │ pwm_a/b/c, pwm_en
────────┼─▶│ 角度换算 → 电流重构 → clark → park → PI → 反park → svpwm│──┼──────────────────▶
        │  └──────────────────────────────────────────────────────┘  │
        │     ▲ sn_adc(命令采样)            ▲ en_idq(电流有效)        │
        └─────┼────────────────────────────┼─────────────────────────┘
              │                            │ id, iq
              ▼                            ▼
        ┌──────────┐                 ┌─────────────┐
        │adc_ad7928│                 │ uart_monitor│ → uart_tx (串口)
        └──────────┘                 └─────────────┘
              ▲ adc_value_a/b/c
              │ en_adc(结果就绪)
        （受 sn_adc 触发，串行采 3 相）
```

另外还有一条独立的输入支路：`i2c_register_read` 持续从 AS5600 读出 `phi`，喂给 `foc_top`。

注意 `sn_adc` 与 `en_adc` 构成一对**握手脉冲**：`foc_top` 发 `sn_adc`（「请采样」），ADC 采样完后回 `en_adc`（「结果在这」）。这是全库反复出现的「单周期高电平脉冲握手」约定，u2-l1 会专门讲。

#### 4.3.3 源码精读

**(1) I2C 读角度** —— [RTL/fpga_top.v:60-73](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L60-L73)

```verilog
i2c_register_read #(
    .CLK_DIV      ( 16'd10 ),
    .SLAVE_ADDR   ( 7'h36  ),   // AS5600 的 I2C 地址
    .REGISTER_ADDR( 8'h0E  )    // 角度寄存器地址
) u_as5600_read (
    .start  ( 1'b1           ),  // 持续不断地读
    .scl    ( i2c_scl        ),
    .sda    ( i2c_sda        ),
    .regout ( {i2c_trash, phi} ) // 高 4 位丢弃，低 12 位 = phi
);
```

要点：`.start(1'b1)` 让模块**永不停歇地**轮询读取角度；`.regout({i2c_trash, phi})` 是一个位拼接——AS5600 一次回 16bit，但只有低 12bit 是有效角度，高 4bit 丢进 `i2c_trash`。这就是 `phi` 的来源。

**(2) ADC 读电流** —— [RTL/fpga_top.v:78-100](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L78-L100)

```verilog
adc_ad7928 #(
    .CH_CNT( 3'd2 ), .CH0(3'd1), .CH1(3'd2), .CH2(3'd3)
) u_adc_ad7928 (
    .i_sn_adc     ( sn_adc        ),  // foc_top 命令采样
    .o_en_adc     ( en_adc        ),  // 采样结束脉冲
    .o_adc_value0 ( adc_value_a   ),  // A 相
    .o_adc_value1 ( adc_value_b   ),  // B 相
    .o_adc_value2 ( adc_value_c   ),  // C 相
    // o_adc_value3..7 留空：忽略其余 5 路
);
```

要点：`CH_CNT=2` 表示「用到 CH0/CH1/CH2 这 3 个逻辑通道」（参数取 2 是因为内部编号从 0 计数到 CH_CNT，共 3 个）；`CH0=1` 表示逻辑通道 0 对应 AD7928 的物理通道 1（硬件上接 A 相电流）。`adc_value_a/b/c` 是 12bit ADC 原始值，会回送给 `foc_top`。

**(3) FOC 核心** —— [RTL/fpga_top.v:105-132](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L105-L132)

这是最关键的例化，参数也最多：

```verilog
foc_top #(
    .INIT_CYCLES  ( 16777216 ),  // 初始化时长 = 16777216/36864000 ≈ 0.45s
    .ANGLE_INV    ( 1'b0     ),  // 传感器没装反
    .POLE_PAIR    ( 8'd7     ),  // 电机极对数 = 7
    .MAX_AMP      ( 9'd384   ),  // SVPWM 最大振幅 = 384/512 = 75%
    .SAMPLE_DELAY ( 9'd120   )   // 采样延时 120 个 clk
) u_foc_top (
    .Kp ( 31'd300000 ), .Ki ( 31'd30000 ),  // PID 参数（运行时可调）
    .phi   ( phi          ),  // ← 角度输入（来自 I2C 模块）
    .sn_adc( sn_adc       ),  // → 命令 ADC 采样
    .en_adc( en_adc       ),  // ← ADC 结果就绪
    .adc_a ( adc_value_a  ),  // ← 三相电流
    .pwm_en( pwm_en       ),  // → 输出到驱动板
    .pwm_a ( pwm_a        ),
    .en_idq( en_idq       ),  // → 电流有效脉冲
    .id    ( id           ),  // → 实际 d 轴电流
    .iq    ( iq           ),  // → 实际 q 轴电流
    .id_aim( id_aim       ),  // ← 目标 d 轴电流（用户逻辑）
    .iq_aim( iq_aim       )   // ← 目标 q 轴电流（用户逻辑）
);
```

可以清楚看到 `foc_top` 既是「消费者」也是「生产者」：它消费 `phi`、`en_adc`、`adc_*`、`id_aim`、`iq_aim`，生产 `sn_adc`、`pwm_*`、`en_idq`、`id`、`iq`。这些参数的物理含义会在 u4-l2 详细讲。

**(4) UART 监视** —— [RTL/fpga_top.v:159-170](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L159-L170)

```verilog
uart_monitor #(
    .CLK_DIV( 16'd320 )   // 36864000/320 = 115200 波特率
) u_uart_monitor (
    .i_en   ( en_idq ),    // 每来一个电流有效脉冲，就发一帧
    .i_val0 ( id     ),    // 打印 4 个数：id, id_aim, iq, iq_aim
    .i_val1 ( id_aim ),
    .i_val2 ( iq     ),
    .i_val3 ( iq_aim ),
    .o_uart_tx( uart_tx )
);
```

要点：`.i_en(en_idq)` 把 UART 的发送节拍绑在 `foc_top` 的电流更新节拍上——每个控制周期（≈55µs）打印一行 `id, id_aim, iq, iq_aim` 四列十进制数。`CLK_DIV=320` 让波特率正好 115200。

#### 4.3.4 代码实践

**实践目标**：从源码里「数」出 ADC 采样的握手回路。

**操作步骤**：

1. 打开 [RTL/fpga_top.v:90-91](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L90-L91)（ADC 模块的 `i_sn_adc`/`o_en_adc`）。
2. 再打开 [RTL/fpga_top.v:117-118](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L117-L118)（`foc_top` 的 `sn_adc`/`en_adc`）。
3. 确认它们连的是**同一根 wire** `sn_adc` / `en_adc`（见第 36-37 行声明）。

**需要观察的现象 / 预期结果**：`foc_top` 的 `sn_adc`（output）和 `adc_ad7928` 的 `i_sn_adc`（input）共享 wire `sn_adc`；`adc_ad7928` 的 `o_en_adc`（output）和 `foc_top` 的 `en_adc`（input）共享 wire `en_adc`。回路闭合：命令出去、结果回来。

**待本地验证**：`sn_adc` 脉冲到 `en_adc` 脉冲的时间差（即 ADC 串行采 3 相的耗时）必须小于「采样窗口」长度，否则会采到错的电流——这个约束在 FAQ 里有详细推导（u3-l2 会用到）。

#### 4.3.5 小练习与答案

**练习 1**：如果换成一台极对数为 14 的电机，要改哪一处？

**参考答案**：把第 108 行 `.POLE_PAIR(8'd7)` 改成 `.POLE_PAIR(8'd14)`。极对数决定了机械角度到电角度的换算（电角度 = 极对数 × 机械角度），见 u2-l2。

**练习 2**：`foc_top` 的 `id_aim`/`iq_aim` 是 input，那它们由谁驱动？

**参考答案**：由 `fpga_top` 里的**用户逻辑**驱动（`id_aim` 用 `assign` 接 0，`iq_aim` 用 `always` 块在 ±200 间切换）。这正是「黄色用户逻辑」给「蓝色核心」下指令的接口——下一节详述。

---

### 4.4 用户逻辑：让 iq_aim 顺逆交替

#### 4.4.1 概念说明

`foc_top` 是「固定功能」的核心，它只认两个目标：`id_aim`（d 轴目标电流）和 `iq_aim`（q 轴目标电流）。**到底给什么目标值，由用户逻辑决定**——这就是系统框图里的黄色区域。

本仓库的示例行为非常简单：让 `iq_aim` 在 +200 和 −200 之间周期性切换，于是电机的扭矩（q 轴电流）一会正一会负，表现为一会正转一会反转。`id_aim` 则恒为 0（不做弱磁控制）。

理解这段代码，你就理解了「如何在这个 FOC 平台上写自己的应用」——本质上就是**改写这两个目标值的产生方式**。

#### 4.4.2 核心流程

用户逻辑靠一个 24bit 自由计数器 `cnt` 实现节拍：

1. `cnt` 每个时钟周期 `+1`，不断自增。
2. 取它的最高位 `cnt[23]`：
   - 当 `cnt[23]=0`（前半周期）→ `iq_aim = -200`。
   - 当 `cnt[23]=1`（后半周期）→ `iq_aim = +200`。
3. `cnt` 计满 2²⁴ 后回绕，`cnt[23]` 随之翻转，于是 `iq_aim` 自动交替。

切换周期可算：

\[ T_{half} = \frac{2^{24}}{f_{clk}} = \frac{16777216}{36864000} \approx 0.455\,\text{s} \]

也就是大约每 0.45 秒切换一次方向，正反转各持续约 0.45 秒。

#### 4.4.3 源码精读

[RTL/fpga_top.v:136-154](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L136-L154) —— 这是示例的全部用户逻辑。分三块：

**(1) 24bit 自由计数器**（第 136-141 行）：

```verilog
reg [23:0] cnt;
always @ (posedge clk or negedge rstn)
    if(~rstn) cnt <= 24'd0;
    else      cnt <= cnt + 24'd1;
```

这是最经典的「自由跑」计数器：复位清零，否则每拍 `+1`。它给整个用户逻辑提供一个「时间基准」。

**(2) id_aim 恒 0**（第 144 行）：

```verilog
assign id_aim = $signed(16'd0);   // d 轴目标恒为 0
```

`$signed(16'd0)` 把无符号常量 0 显式标注为有符号 16bit，与 `id_aim` 的 `wire signed [15:0]` 类型匹配。d 轴电流为 0 意味着「不弱磁、不产生多余的磁通」，这是 FOC 的标准做法。

**(3) iq_aim 顺逆交替**（第 146-154 行）：

```verilog
always @ (posedge clk or negedge rstn)
    if(~rstn)
        iq_aim <= $signed(16'd0);
    else begin
        if(cnt[23]) iq_aim <=  $signed(16'd200);   // 正向扭矩
        else        iq_aim <= -$signed(16'd200);   // 反向扭矩
    end
```

注意 `$signed(16'd200)` 和 `-$signed(16'd200)`：前者是 +200，后者是对 +200 取负（在 16bit 补码下就是 0xFF38）。`iq_aim` 的符号决定了扭矩方向，于是电机正反交替。

> 小提示：`id_aim` 用 `assign`（因为它是常量），`iq_aim` 用 `always`（因为它要随 `cnt` 变化）。这解释了为什么第 45 行 `id_aim` 声明成 `wire`，而第 46 行 `iq_aim` 声明成 `reg`——**Verilog 里 wire/reg 的选择完全取决于驱动方式，与综合出的是线还是寄存器无关**。

#### 4.4.4 代码实践

**实践目标**：亲手算出电机的正反转周期，并尝试修改它。

**操作步骤**：

1. 打开 [RTL/fpga_top.v:136-154](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L136-L154)。
2. 用 \( T = 2^{24}/f_{clk} \) 算出一次方向切换的时间。
3. 思考：如果想让切换更慢（比如每 2 秒切一次），该把计数器改成多少 bit？或保持 24bit 但改用 `cnt[22]` 会怎样？

**需要观察的现象 / 预期结果**：

- 当前 `cnt[23]` → 每约 0.455s 切一次方向，整周期 ≈ 0.91s。
- 若改用 `cnt[22]`，则每约 0.227s 切一次（周期减半，切换更快）。
- 若想每 2s 切一次：\( 2 = 2^n / 36864000 \Rightarrow 2^n = 73728000 \Rightarrow n \approx 26 \)，用 26bit 计数器取 `cnt[25]` 即可（示例代码，未在仓库中验证）。

**待本地验证**：上述「改为 26bit」属于示例修改，仓库默认代码是 24bit；若你在硬件上修改需重新综合烧录。

#### 4.4.5 小练习与答案

**练习 1**：为什么用 `cnt[23]`（最高位）而不是 `cnt[0]`（最低位）来切换方向？

**参考答案**：`cnt[0]` 每个时钟周期都翻转（频率 = clk/2 ≈ 18MHz），电机根本来不及响应，且会让扭矩在 MHz 级抖动；`cnt[23]` 是最高位，翻转频率 = clk/2²⁴ ≈ 2.2Hz，才给出人眼可见的、约 0.45s 一次的正反转。

**练习 2**：把 `iq_aim` 的 ±200 改成 ±100，电机的运行会有什么变化？

**参考答案**：目标电流减半，意味着目标扭矩减半，电机会转得更「没劲」（加速度更小），但正反交替的节拍不变（因为 `cnt` 没改）。这是「改一个数就能调扭矩」的最直接例子。

**练习 3**：如果想让电机**始终正转**而不交替，最少改哪一行？

**参考答案**：把第 146-154 行的 `always` 块换成 `assign iq_aim = $signed(16'd200);`（或任何正值常量）即可。

---

## 5. 综合实践

**任务**：在 `fpga_top.v` 中完整画出两条数据通路——「角度进」与「电流出」。

**步骤**：

1. **角度通路（AS5600 → foc_top）**：
   - 起点：`i2c_register_read` 的 `.regout`，见 [RTL/fpga_top.v:72](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L72)，它把结果接到 wire `phi`（声明在第 34 行）。
   - 终点：`foc_top` 的 `.phi(phi)`，见 [RTL/fpga_top.v:116](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L116)。
   - 请在纸上画出：`u_as5600_read.regout → phi → u_foc_top.phi`。

2. **电流通路（foc_top → uart_monitor）**：
   - 起点：`foc_top` 的 `.id(id)` 和 `.iq(iq)`，见 [RTL/fpga_top.v:127-128](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L127-L128)，还有节拍脉冲 `.en_idq(en_idq)`（第 126 行）。
   - 中间 wire：`id`、`iq`、`en_idq`（声明在第 42-44 行）。
   - 终点：`uart_monitor` 的 `.i_en(en_idq)`、`.i_val0(id)`、`.i_val2(iq)`，见 [RTL/fpga_top.v:164-168](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L164-L168)。
   - 请画出：`u_foc_top.{id,iq,en_idq} → {id,iq,en_idq} → u_uart_monitor.{i_val0,i_val2,i_en} → uart_tx`。

3. ** bonus **：再画出 ADC 的握手回路（`sn_adc` 出、`en_adc` 回、`adc_value_a/b/c` 回），验证它和角度通路是两条独立的支路。

**预期结果**：你应该得到一张以 `foc_top` 为中心的「X 型」框图——左边和下边是输入（角度、电流），右边是输出（PWM），下边还分出一路到 UART。这张图就是后续 u2 单元逐模块精读时的「导航地图」。

## 6. 本讲小结

- `fpga_top.v` 是工程顶层，自身不含 FOC 算法，只负责**生成时钟、定义 IO、例化子模块、承载用户逻辑**四件事。
- `altpll` 用 `multiply_by=73, divide_by=99` 把 50MHz 变成约 36.864MHz 的主时钟；其 `locked` 信号直接当系统复位 `rstn`，保证时钟稳定前系统不乱跑。
- 主时钟频率不能超 40MHz，根因是 AD7928 要求 SPI 时钟 ≤20MHz，而 SPI 时钟由 `clk` 二分频得到。
- 顶层用一组 `wire` 把四个子模块拼成数据通路：`i2c_register_read` 供角度 `phi`，`adc_ad7928` 经 `sn_adc`/`en_adc` 握手供三相电流，`foc_top` 输出 PWM 和 `id`/`iq`，`uart_monitor` 把 `id`/`iq` 打印出来。
- 示例用户逻辑用一个 24bit 计数器 `cnt`，按 `cnt[23]` 让 `iq_aim` 在 ±200 间切换（约每 0.45 秒一次），`id_aim` 恒 0——这就是「让电机一会正转一会反转」的全部代码。
- `wire` 还是 `reg` 取决于驱动方式（`assign` vs `always`），与最终综合出的是连线还是触发器无关；这是看懂本文件类型声明的关键。

## 7. 下一步学习建议

本讲只看了「外壳」，没有进入任何子模块内部。接下来的学习路径：

- **u1-l4（用 iverilog 跑仿真）**：先学会用 iverilog + gtkwave 看波形，因为后面 u2 各讲都建议你边读代码边对照仿真波形。
- **u2-l1（foc_top 全景与控制环路）**：正式进入蓝色核心，从 `foc_top.v` 的内部信号流俯瞰整个电流环——本讲里那些 `phi`、`adc_*`、`id`/`iq` 进去之后到底经历了什么。
- **u3-l1 / u3-l2（I2C / SPI 外设）**：如果你更想先搞懂「角度和电流是怎么从芯片里读出来的」，可以跳到 u3 单元看 `i2c_register_read` 和 `adc_ad7928` 的内部时序。
- **延伸阅读**：README 的「调参」「FAQ」两节（中文版在第 375-575 行）与本讲高度相关，尤其 FAQ 里关于 ADC 采样窗口的推导，是理解 `sn_adc`/`en_adc` 握手时序约束的最佳材料。
