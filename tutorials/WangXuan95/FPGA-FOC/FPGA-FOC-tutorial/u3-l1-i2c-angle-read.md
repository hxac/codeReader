# I2C 角度读取 i2c_register_read.v

## 1. 本讲目标

本讲是「传感器与通信外设」单元的第一篇。前面我们已经把 FOC 核心算法（蓝色部分）从角度 φ 一路追到了三相 PWM，但有一个关键输入一直是「黑盒」：**转子的机械角度 φ 到底是从哪里、用什么总线读进来的？** 本讲就打开这个黑盒。

读完本讲，你应当能够：

- 说清 I2C 总线的基本时序（START / 地址 / 寄存器 / 重复 START / 读 / STOP）以及 AS5600 磁编码器的角度寄存器结构。
- 看懂 `i2c_register_read.v` 如何用 `CLK_DIV` 分频出 SCL，并能用公式 \( f_{\text{SCL}} = f_{\text{clk}}/(4\cdot\text{CLK\_DIV}) \) 算出 SCL 频率。
- 解释 `sda_e` / `sda_o` 如何把一个 `inout` 引脚变成「开漏式」双向 SDA，以及 `send_shift` / `recv_shift` 如何串行移位收发。
- 把 `cnt` 状态机的每一段 cnt 区间对应到一次 I2C 读操作的某个阶段，并算出一次读取耗时多少个时钟周期。
- 看懂 `fpga_top.v` 里 `start=1'b1` 的「持续读」接法，以及为什么 `regout` 的高 4 位被丢弃、只取低 12 位作为 φ。

## 2. 前置知识

在进入源码前，先用通俗语言建立三个概念。

**(1) 为什么是 I2C？** AS5600 是一颗 12 位磁编码器，贴在电机轴上测量转子角度。它对外只暴露两根线：SCL（时钟）和 SDA（数据），这就是 **I2C 总线**。I2C 是「多主、多从、开漏」的两线串行总线：主机（FPGA）提供时钟 SCL，数据 SDA 由主机和从机分时驱动。一根线上可以挂多个从机，每个从机有唯一的 7 位地址，主机靠地址点名要和谁说话。

**(2) 什么是「开漏 (open-drain)」？** I2C 规定：**任何设备都只能把 SDA 拉低，或者松手（释放）**，绝不能主动往高推。高电平由总线上的上拉电阻把松手后的线拉回来。这样即使主机和从机「同时发言」，最坏也只是线被拉低，不会烧管子。在 FPGA 里，我们没有真正的开漏晶体管，但可以用一个三态缓冲模拟：给一个使能信号 `sda_e`，`sda_e=1` 时 FPGA 把自己想输出的值推上线，`sda_e=0` 时 FPGA 「松手」让线进入高阻 `1'bz`。这正是本模块 SDA 的实现方式。

**(3) AS5600 的角度寄存器。** AS5600 内部有一组寄存器，其中地址 `0x0E` 和 `0x0F` 合在一起存放 12 位的 **ANGLE**（角度）值：`0x0E` 是高字节（只有低 4 位有效，存放角度的第 11~8 位，高 4 位恒为 0），`0x0F` 是低字节（存放角度的第 7~0 位）。所以「从 `0x0E` 起连读 2 个字节」就能拿到一个 16 位数据，其低 12 位就是当前角度，高 4 位是 0。这一点直接决定了后面 `fpga_top.v` 为什么要丢弃高 4 位。

> 与 u2 系列的蓝色模块不同，本模块属于系统框图中的**粉色部分（硬件相关逻辑）**：它绑死了 AS5600 这颗具体的芯片。换型号就得改它，但核心 FOC 算法一行都不用动。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| :--- | :--- | :--- |
| [RTL/i2c_register_read.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/i2c_register_read.v) | 通用 I2C 读控制器（粉色外设） | 三个 parameter、SCL 分频、开漏 SDA、cnt 状态机、regout 输出 |
| [RTL/fpga_top.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v) | 工程顶层 | 例化 `i2c_register_read`，把 `regout` 拆成 `{i2c_trash, phi}`，`start=1'b1` 持续读 |

补充背景可参看 [README.md](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/README.md) 的「设计代码详解」一节，其中明确把 `i2c_register_read.v` 标注为「读取 AS5600 磁编码器」的硬件相关模块。

---

## 4. 核心概念与源码讲解

### 4.1 I2C 协议与 AS5600 角度寄存器

#### 4.1.1 概念说明

`i2c_register_read.v` 是一个**通用 I2C 读控制器**：它的工作就是「向某个 I2C 从机的某个寄存器地址，读出 2 个字节」。它本身并不硬编码「AS5600」这三个字——芯片型号完全靠三个 parameter 决定：

[RTL/i2c_register_read.v:9-22](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/i2c_register_read.v#L9-L22) 定义了模块的端口与三个关键参数：`CLK_DIV`（SCL 分频系数）、`SLAVE_ADDR`（从机 7 位地址，默认 `7'h36`）、`REGISTER_ADDR`（要读的寄存器地址，默认 `8'h0E`）。`7'h36` 正是 AS5600 的 I2C 地址，`8'h0E` 正是上面说的 ANGLE 高字节地址。

这个模块解决的问题是：**FPGA 怎样用两根线，按 I2C 协议的时序，把 AS5600 里的 12 位角度搬出来。**

#### 4.1.2 核心流程

一次「读某个寄存器」的标准 I2C 时序（也叫 *random read*）分 8 个阶段：

```text
1. START          : SCL 为高时，SDA 由高→低（起始位）
2. 发送 SLAVE_ADDR + W(写=0)  : 8 bit，从机回 ACK
3. 发送 REGISTER_ADDR         : 8 bit，告诉从机「我要读这个寄存器」，从机回 ACK
4. Repeated START : 再来一个起始位，准备切换为读方向
5. 发送 SLAVE_ADDR + R(读=1)  : 8 bit，从机回 ACK
6. 读 byte1 (寄存器地址的内容) : 8 bit，主机回 ACK（表示「还要读」）
7. 读 byte2 (下一个地址的内容) : 8 bit，主机回 NACK（表示「读够了」）
8. STOP           : SCL 为高时，SDA 由低→高（停止位）
```

对应到 AS5600：阶段 3 写入 `0x0E`，阶段 6 读出 `0x0E` 的内容（角度高字节），阶段 7 读出 `0x0F` 的内容（角度低字节）。拼起来就是 12 位角度。

#### 4.1.3 源码精读

阶段 3「写入寄存器地址」和阶段 1、5「装载地址字节」在代码里体现为对 `send_shift` 的赋值：

- [RTL/i2c_register_read.v:74](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/i2c_register_read.v#L74)：`send_shift <= {SLAVE_ADDR, 1'b0};` —— 把 7 位从机地址拼上 1 位写标志 `0`，准备发送「地址 + 写」字节。
- [RTL/i2c_register_read.v:81](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/i2c_register_read.v#L81)：`send_shift <= REGISTER_ADDR;` —— 装载寄存器地址 `0x0E`，准备发送。
- [RTL/i2c_register_read.v:102](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/i2c_register_read.v#L102)：`send_shift <= {SLAVE_ADDR, 1'b1};` —— 重复 START 后，把地址拼上读标志 `1`，准备发送「地址 + 读」字节。

这三处赋值说明：模块把「写哪颗芯片、读写哪个寄存器」完全参数化了，换芯片只改 parameter 即可。

#### 4.1.4 代码实践

**实践目标**：确认 AS5600 的寄存器布局与本模块读取策略一致。

**操作步骤**（源码阅读型）：

1. 打开 AS5600 数据手册，查 `ANGLE` 寄存器，确认其地址为 `0x0E`（高字节）/ `0x0F`（低字节），且高字节的高 4 位为 0、低 4 位才是角度的 11~8 位。
2. 对照 [RTL/i2c_register_read.v:12](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/i2c_register_read.v#L12) 的 `REGISTER_ADDR = 8'h0E`，确认模块是从 ANGLE 高字节起读。
3. 思考：连读 2 字节后，第 1 字节（`0x0E`）放在 16 位结果的哪 8 位？第 2 字节（`0x0F`）放在哪 8 位？（提示见 4.3）

**需要观察的现象 / 预期结果**：第 1 字节在高 8 位、第 2 字节在低 8 位；高 8 位中只有低 4 位有角度信息，所以整个 16 位结果的高 4 位恒为 0——这正是后面 `fpga_top` 丢弃高 4 位的依据。

> 本实践为数据手册核对，无需运行；若手头无手册，可暂记结论，标注「待本地验证寄存器位域」。

#### 4.1.5 小练习与答案

**Q1**：I2C 的 START 和 STOP 条件分别是什么？为什么必须「SCL 为高」时才能产生？  
**答**：START 是 SCL 为高时 SDA 由高到低；STOP 是 SCL 为高时 SDA 由低到高。SCL 为高时 SDA 的跳变才被识别为控制信号（起/停），而 SCL 为高时 SDA 的稳定电平才是有效数据位。

**Q2**：为什么读两个字节后，主机要在最后一个字节回 NACK 而不是 ACK？  
**答**：ACK 表示「我还要继续读」，从机会接着发下一字节；NACK 表示「读完了」，从机释放 SDA，主机随后才能发出 STOP。所以读完最后一个字节必须回 NACK 来干净地结束传输。

---

### 4.2 SCL 时钟分频与 epoch 节拍

#### 4.2.1 概念说明

I2C 是**同步串行**总线，主机必须提供 SCL 时钟，但 SCL 频率不能太高——AS5600 要求 SCL 不超过 1 MHz。FPGA 的主时钟 `clk`（本工程 36.864 MHz）远高于此，所以必须**分频**。本模块用一个叫 `epoch` 的「节拍脉冲」把高速 `clk` 切成一段段低速节拍：所有 I2C 时序都按 `epoch` 节拍推进，而不是按 `clk` 推进。

#### 4.2.2 核心流程

`epoch` 的产生逻辑是一个简单的计数分频器：每个 `clk` 上升沿 `clkcnt` 自增；数到 `CLK_DIV-1` 时产生 1 拍 `epoch` 脉冲并清零。也就是说：

\[
\text{epoch 的周期} = \text{CLK\_DIV} \text{ 个 clk}
\]

而 SCL 由 `scl <= cnt[1]` 产生（见后文状态机），`cnt` 每个 `epoch` 自增 1，`cnt[1]` 每 2 个 `epoch` 翻转一次，因此 SCL 的一个完整周期（高+低）= 4 个 `epoch`：

\[
f_{\text{SCL}} = \frac{f_{\text{clk}}}{4 \times \text{CLK\_DIV}}
\]

```text
clk:    ─┐_┌─┐_┌─┐_┌─┐_┌─ ... （每 CLK_DIV 个 clk 产生 1 个 epoch）
epoch:           ┊         ┊         ┊         ┊
cnt:            0    →    1    →    2    →    3    → 0 ...
cnt[1]:         0         0         1         1      ← scl
                 └──低──┴──高──┘
                 ←─ scl 的一个完整周期 = 4 个 epoch ─→
```

`CLK_DIV` 默认为 `16`（模块声明 [L10](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/i2c_register_read.v#L10)）。注意 `fpga_top.v` 实际例化时把它改成了 `16'd10`，这一点我们留到 4.4 讨论。

#### 4.2.3 源码精读

- [RTL/i2c_register_read.v:24](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/i2c_register_read.v#L24)：`localparam [15:0] CLK_DIV_PARSED = CLK_DIV>16'd0 ? CLK_DIV-16'd1 : 16'd0;` —— 把 `CLK_DIV` 减 1 作为计数终点，处理了 `CLK_DIV=0` 的边界。
- [RTL/i2c_register_read.v:36-48](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/i2c_register_read.v#L36-L48)：`clkcnt` 计数到 `CLK_DIV_PARSED` 时拉高 `epoch` 一拍并清零，否则 `epoch=0`、`clkcnt` 自增。这段就是上面的分频器。
- [RTL/i2c_register_read.v:76](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/i2c_register_read.v#L76)（及多处 `scl <= cnt[1];`）：SCL 直接取 `cnt` 的第 1 位，正是上面公式的来源。

#### 4.2.4 代码实践

**实践目标**：亲手算出 SCL 频率，验证它落在 AS5600 的 1 MHz 上限内。

**操作步骤**（计算型）：

1. 取模块**默认** `CLK_DIV = 16`、`f_clk = 36.864 \text{MHz}`，代入公式：

   \[
   f_{\text{SCL}} = \frac{36.864\,\text{MHz}}{4 \times 16} = \frac{36.864}{64}\,\text{MHz} = 0.576\,\text{MHz} = 576\,\text{kHz}
   \]

2. 再算 `fpga_top.v` 实际部署的 `CLK_DIV = 10`：

   \[
   f_{\text{SCL}} = \frac{36.864}{4 \times 10}\,\text{MHz} = \frac{36.864}{40}\,\text{MHz} \approx 0.9216\,\text{MHz} \approx 922\,\text{kHz}
   \]

3. 对照 [RTL/fpga_top.v:61](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L61) 的注释 `scl频率 = clk频率 / (4*CLK_DIV)`，核对你的计算与注释一致。

**预期结果**：默认参数 576 kHz、实际部署 922 kHz，两者都 < 1 MHz，满足 AS5600 要求；922 kHz 比 576 kHz 更接近上限，读取更快（见 4.4 的耗时计算）。

#### 4.2.5 小练习与答案

**Q1**：若把 `CLK_DIV` 设为 `9`，SCL 频率是多少？还满足 ≤1 MHz 吗？  
**答**：\( 36.864/(4\times9)=1.024\,\text{MHz} \)，略超 1 MHz，**不满足** AS5600 要求，应避免。这也是 `fpga_top.v` 取 `10` 而非更小值的原因。

**Q2**：为什么不直接用 `clk` 当 SCL？  
**答**：`clk` 是 36.864 MHz，远超 AS5600 的 1 MHz 上限，从机根本来不及响应；必须分频。

---

### 4.3 开漏 SDA 与 cnt 状态机的完整读时序

> 这是本讲最核心的一节。开漏 SDA 决定「怎么在两根线上不冲突地收发」，cnt 状态机决定「在哪个节拍做什么」。两者合起来就是完整的 I2C 读时序。

#### 4.3.1 概念说明

I2C 总线上 SDA 是双向的（既可能主机驱动，也可能从机驱动）。本模块用一个三态缓冲把 `inout sda` 变成「需要时驱动、不需要时松手」的开漏式引脚。**发送**时，主机把要发的位（MSB 先发）逐位移到 SDA 上；**接收**或**等 ACK** 时，主机松手，由从机驱动 SDA，主机在合适的节拍采样。整个「何时发地址、何时发寄存器号、何时松手读」由一个 8 位计数器 `cnt` 编码——它每来一个 `epoch` 加 1，不同的 `cnt` 区间执行不同动作，跑完一遍就完成一次读取。

#### 4.3.2 核心流程

**开漏 SDA 与移位收发**：

```text
发送一位 (写阶段):
    sda_e = 1            // FPGA 驱动 SDA
    sda_o = send_shift 的最高位 (MSB)
    {sda_o, send_shift} <= {send_shift, 1'b1}   // 左移，下一位上到 MSB；低位补 1
接收一位 (读阶段):
    sda_e = 0            // FPGA 松手 (高阻)，让从机驱动 SDA
    recv_shift <= {recv_shift[14:0], sda}       // 左移，把采样到的 sda 塞进 LSB
等 ACK:
    sda_e = 0            // 松手，让从机把 SDA 拉低表示应答
```

注意接收用的是**左移 + 塞 LSB**：先收到的位最终停在高位，后收到的位停在低位。所以读完 16 位后，第 1 字节（`0x0E`）落在 `recv_shift[15:8]`，第 2 字节（`0x0F`）落在 `recv_shift[7:0]`——这印证了 4.1 的结论。

**cnt 状态机的阶段划分**（下表的 `cnt` 是 `epoch` 节拍数，不是 `clk` 数）：

| cnt 区间 | epoch 数 | I2C 阶段 | 关键动作 |
| :--- | :---: | :--- | :--- |
| `0`（`ready`） | — | 空闲 | `scl/sda` 释放为高；若 `start` 则 `cnt←1` |
| `1~3` | 3 | START + 装载「地址+写」 | `sda_o←0`，`send_shift←{SLAVE_ADDR,0}` |
| `4~36` | 33 | 发送 8 位「地址+写」字节 | `scl←cnt[1]`，`cnt[1:0]==01` 时左移发送 |
| `37~39` | 3 | 装 REGISTER_ADDR + 等 ACK | `send_shift←REGISTER_ADDR`，`sda_e←0` |
| `40~72` | 33 | 发送 8 位寄存器地址字节 | 同上移位发送 |
| `73~76` | 4 | 等 ACK | `sda_e←0` |
| `77~83` | 7 | 重复 START | 先 `sda_e/sda_o←1`，再 `sda_o←0`，装 `{SLAVE_ADDR,1}` |
| `84~116` | 33 | 发送 8 位「地址+读」字节 | 移位发送 |
| `117~120` | 4 | 等 ACK | `sda_e←0` |
| `121~152` | 32 | 读 byte1（8 位） | `sda_e←0`，`cnt[1:0]==11` 时左移采样 |
| `153~156` | 4 | 主机回 ACK | `sda_e←1, sda_o←0`（拉低=ACK） |
| `157~188` | 32 | 读 byte2（8 位） | 同上采样 |
| `189~192` | 4 | 主机回 NACK | `sda_e←1, sda_o←1`（不拉低=NACK） |
| `193~203` | 11 | STOP + 锁存结果 | `sda_o` 先低后高产生 STOP；`regout←recv_shift` |
| `≥204` | — | 完成 | `done←1`，下一拍 `cnt←0` 回到 `ready` |

整条链路合计约 **204 个 epoch**，对应一次完整读取。

#### 4.3.3 源码精读

**开漏 SDA 的实现**（仅两行）：

[RTL/i2c_register_read.v:26-28](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/i2c_register_read.v#L26-L28)：

```verilog
reg  sda_e, sda_o;
assign sda = sda_e ? sda_o : 1'bz;
```

`sda_e=1` 时把 `sda_o` 推上线；`sda_e=0` 时输出高阻 `1'bz`（松手）。这就是用 Verilog 三态描述开漏 I2C 引脚的标准写法。

**发送移位**（MSB 先发）：

[RTL/i2c_register_read.v:77-79](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/i2c_register_read.v#L77-L79)：当 `cnt[1:0]==2'b01`（每个 4-epoch 周期里的固定一拍）时执行 `{sda_o, send_shift} <= {send_shift, 1'b1};`，把当前 MSB 送到 `sda_o`，剩余位左移、低位补 1。8 个这样的节拍正好发完一字节。

**接收移位**（左移 + 塞 LSB）：

[RTL/i2c_register_read.v:114-115](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/i2c_register_read.v#L114-L115)（读 byte1）和 [RTL/i2c_register_read.v:123-124](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/i2c_register_read.v#L123-L124)（读 byte2）：当 `cnt[1:0]==2'b11` 时执行 `recv_shift <= {recv_shift[14:0], sda};`，把采样到的 `sda` 塞进 LSB。

**ACK / NACK**：

[RTL/i2c_register_read.v:116-119](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/i2c_register_read.v#L116-L119) 主机回 ACK（`sda_e←1, sda_o←0`，拉低）；[RTL/i2c_register_read.v:125-128](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/i2c_register_read.v#L125-L128) 主机回 NACK（`sda_e←1, sda_o←1`，不拉低）。

**STOP 与结果锁存**：

[RTL/i2c_register_read.v:135-137](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/i2c_register_read.v#L135-L137)：`sda_o←1` 产生 STOP，同时 `regout <= recv_shift;` 把读到的 16 位结果锁存到输出端口。

**完成与握手信号**：

[RTL/i2c_register_read.v:50](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/i2c_register_read.v#L50) 定义 `ready = (cnt==0)`，即「处于空闲、可以接受新一次 `start`」。[RTL/i2c_register_read.v:61-68](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/i2c_register_read.v#L61-L68)：`ready` 时若 `start` 有效则 `cnt←1` 启动；`done` 有效时清 `done`、`cnt←0` 回到空闲——形成 `ready → 运行 → done → ready` 的循环。

#### 4.3.4 代码实践

**实践目标**：把状态机的阶段表落到源码上，并算出一次读取的墙钟耗时。

**操作步骤**（源码阅读 + 计算型）：

1. 打开 [RTL/i2c_register_read.v:69-141](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/i2c_register_read.v#L69-L141)，对照上面的 cnt 阶段表，逐个 `else if(cnt< ...)` 分支确认：每一段 `cnt` 区间对应哪个 I2C 阶段。
2. 数一下读阶段：`121~152` 是 32 个 epoch（8 位 × 每 4 个 epoch 采 1 位），`157~188` 也是 32 个 epoch，合计读 16 位——和「读 2 字节」吻合。
3. 算一次读取耗时（以 `fpga_top.v` 实际部署的 `CLK_DIV=10` 为例）：

   \[
   T_{\text{read}} \approx 204 \times \text{CLK\_DIV} / f_{\text{clk}} = 204 \times 10 / 36.864\,\text{M} \approx 55.3\,\mu\text{s}
   \]

   而一个 FOC 控制周期是 `clk/2048 ≈ 55.6 µs`。可见「持续读」模式下，φ 大约每个控制周期就被刷新一次。

**需要观察的现象 / 预期结果**：源码分支边界与阶段表一一对应；一次读取约 2040 个 `clk`，与一个控制周期相当。

> 「约 204 epoch」是按阶段表累加的近似值，实际可能有 1~2 拍缓冲差异，精确数字「待本地仿真确认」。

#### 4.3.5 小练习与答案

**Q1**：为什么发送用 `{send_shift, 1'b1}` 左移、低位补 1，而不是补 0？  
**答**：低位补 1 表示「移完后让 SDA 默认处于释放/高状态」，避免在不应驱动时把 SDA 拉低造成总线误动作；真正要发 0 的位来自原 `send_shift` 的高位，补什么到最低位不影响已发出的那些位。

**Q2**：读 byte1 后主机回 ACK、读 byte2 后回 NACK，如果两个都回 ACK 会怎样？  
**答**：从机会接着发第 3 个字节（`0x10` 的内容），而本模块只锁存 16 位、之后直接进 STOP 时序，时序会错位。所以「最后字节必须 NACK」是协议硬要求。

**Q3**：`done` 信号高电平持续大约多少个 `clk`？为什么 `fpga_top.v` 可以不接它？  
**答**：`done` 在 `cnt≥204` 那个 epoch 被置 1，到下一个 epoch 被 `else if(done)` 清 0，所以约持续一个 epoch 周期 = `CLK_DIV` 个 `clk`。`fpga_top.v` 用 `start=1'b1` 持续触发，靠 `regout` 连续输出 φ 即可，不需要靠 `done` 做握手，所以悬空。

---

### 4.4 fpga_top 例化与 12 位机械角度 φ 的提取

#### 4.4.1 概念说明

`i2c_register_read` 是通用控制器，真正把它「用起来」读 AS5600 的是 `fpga_top.v`。这里要做三件事：① 给三个 parameter 赋实际值；② 决定「什么时候读」——本工程选择「永远在读」(`start=1'b1`)，让 φ 持续刷新；③ 把 16 位 `regout` 拆成「丢弃的高 4 位」和「真正要的 12 位角度 φ」。

#### 4.4.2 核心流程

```text
              ┌──────────────── i2c_register_read (u_as5600_read) ────────────────┐
start=1'b1 ──▶│ ready? → start → 跑完 ~204 epoch → done → 回 ready → 立刻再 start │── 持续循环
              └──────────────────────────────────────────────────────────────────┘
                                                   │ regout[15:0]
                                                   ▼
                              { i2c_trash[3:0] , phi[11:0] }
                                 ↑ 高4位丢弃        ↑ 送入 foc_top 作为 φ
```

因为 `start` 恒为 1，模块每次 `done` 回到 `ready` 后立刻发起新一次读取，形成「读完就再读」的连续刷新，φ 引脚上始终是最新角度。

#### 4.4.3 源码精读

[RTL/fpga_top.v:58-73](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L58-L73) 是完整的例化代码，关键点逐条说明：

- [RTL/fpga_top.v:61](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L61)：`.CLK_DIV(16'd10)` —— 注意这里**覆盖**了模块默认的 `16'd16`，取 `10`，使 SCL≈922 kHz（更接近 1 MHz 上限，读取更快，约每控制周期刷新一次 φ）。注释里也写明了公式 `scl频率 = clk频率 / (4*CLK_DIV)`。
- [RTL/fpga_top.v:62-63](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L62-L63)：`.SLAVE_ADDR(7'h36)`、`.REGISTER_ADDR(8'h0E)` —— AS5600 的地址与 ANGLE 高字节地址。
- [RTL/fpga_top.v:69](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L69)：`.start(1'b1)` —— 持续读。
- [RTL/fpga_top.v:70-71](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L70-L71)：`.ready()`、`.done()` 悬空——持续读模式下用不到握手。
- [RTL/fpga_top.v:59](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L59) 与 [RTL/fpga_top.v:72](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L72)：`wire [3:0] i2c_trash;` 配合 `.regout({i2c_trash, phi})` —— 16 位 `regout` 拼接成「高 4 位 `i2c_trash` + 低 12 位 `phi`」。因为 AS5600 高字节的高 4 位恒为 0，所以 `i2c_trash` 永远是 0，被丢弃；`phi`（[L34](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L34) 声明为 `wire [11:0]`）正是 0~4095 的 12 位机械角度，随后送入 `foc_top` 作为 φ（见 [L116](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L116)）。

> 一个常被忽略的细节：`phi` 是 `wire`（由 `regout` 的位拼接连续驱动），而 `i2c_trash` 也是 `wire`，二者合起来只是把模块输出「按位切开」，没有引入任何寄存器——φ 就是模块内部 `recv_shift` 锁存值的低 12 位的实时映射。

#### 4.4.4 代码实践

**实践目标**：把传感器换成另一颗 I2C 编码器时，知道改哪几个地方。

**操作步骤**（设计分析型）：

假设要把 AS5600 换成一颗 **14 位** 的 I2C 磁编码器（地址 `0x40`，角度寄存器起始于 `0x12`，同样「高字节高 2 位为 0」）。请列出需要改动的地方：

1. **`SLAVE_ADDR`**：`7'h36` → `7'h40`（[fpga_top.v:62](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L62)）。
2. **`REGISTER_ADDR`**：`8'h0E` → `8'h12`（[fpga_top.v:63](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v#L63)）。
3. **位宽处理**：FOC 核心要求 12 位 φ（见 README「>12bit 的传感器需低位截断」）。若新传感器是 14 位连读 2 字节，则 `regout` 低 14 位是角度，需取其中的低 12 位（即丢弃 2 位精度）。可改 `i2c_trash` 为更宽的丢弃位，或在 `phi` 上再截断，使送入 `foc_top` 的仍是 12 位。
4. **`CLK_DIV`**：若新芯片 SCL 上限更低（如 400 kHz），需把 `CLK_DIV` 调大（如 `CLK_DIV=24` → SCL≈384 kHz）；若上限也是 1 MHz，保持 `10` 即可。

**需要观察的现象 / 预期结果**：能明确区分「必改项（地址类 parameter）」「位宽适配项」「可选项（CLK_DIV）」，并理解只要满足「持续输出一个 12 位角度」的接口约定，蓝色 FOC 核心完全不用动。

#### 4.4.5 小练习与答案

**Q1**：`i2c_trash` 这个名字暗示它永远是 0。如果我们读到的 `regout` 高 4 位不是 0（比如换了一颗高位也带数据的芯片），这条 `{i2c_trash, phi}` 接线还能直接用吗？  
**答**：不能直接用——高 4 位会被丢进 `i2c_trash` 而丢失。需要改成读 3 字节或调整拼接，保证 `phi` 拿到完整的低 12 位角度。

**Q2**：为什么 `fpga_top.v` 把 `CLK_DIV` 从默认 16 改成 10？改成 5 行不行？  
**答**：改成 10 让 SCL≈922 kHz，读取更快（约每控制周期刷新一次 φ），且仍 <1 MHz；改成 5 会让 SCL≈1.84 MHz，超过 AS5600 的 1 MHz 上限，不行。

**Q3**：`phi` 是 `wire` 还是 `reg`？由谁驱动？  
**答**：`phi` 是 `wire`，由 `regout` 的位拼接 `{i2c_trash, phi}` 隐式连续驱动（`i2c_register_read` 的输出端口 `regout` 是 `reg`，但拼接给 `phi` 的部分是连线），所以 `phi` 实时反映模块锁存的最新角度。

---

## 5. 综合实践

**任务**：为 `i2c_register_read.v` 写一个最小的 testbench，在波形里验证「给定一个虚拟从机，主机能正确发出 START + 地址 + 寄存器号 + 重复 START + 读，并把回送的两字节拼成 `regout`」。

> ⚠️ 本项目 `SIM/` 目录里**没有** I2C 的 testbench（只仿真了 clark/park 和 svpwm），所以下面是一段**示例代码（非项目原有）**，仅用于学习验证，不是仓库自带的文件。

**示例代码（testbench，非项目原有）**：

```verilog
// 文件名建议：tb_i2c_read.v （示例代码，本项目未提供，需自行创建于 SIM/ 目录外或自行管理）
`timescale 1ns/1ps
module tb_i2c_read;                     // 示例代码
    reg clk = 0; reg rstn = 0; reg start = 0;
    wire scl; wire sda;
    wire [15:0] regout;
    reg  sda_drive = 1'bz;              // 模拟从机驱动 SDA

    // 例化 DUT，用小 CLK_DIV 加速仿真
    i2c_register_read #( .CLK_DIV(4), .SLAVE_ADDR(7'h36), .REGISTER_ADDR(8'h0E) )
        u_dut ( .rstn(rstn), .clk(clk), .scl(scl), .sda(sda),
                .start(start), .ready(), .done(), .regout(regout) );

    // 简化：把 sda 当作线与，从机只在需要时拉低
    assign sda = sda_drive;             // 示例：真实从机模型更复杂，此处仅占位

    always #10 clk = ~clk;              // 50MHz 仿真时钟

    initial begin
        // 此 testbench 仅示范激励框架：完整地从机应答模型需自行实现
        // （在每个 SCL 上升沿按协议回 ACK / 数据位）
        #100 rstn = 1;
        #100 start = 1;                 // 触发一次读取
        #200000;                        // 等待足够长时间（>204 epoch）
        $display("regout = %h", regout);
        $finish;
    end
endmodule
```

**你要完成的事**：

1. 把上面的示例 testbench 补全为一个**真正会回 ACK 和数据位的从机模型**（提示：用 `always @(posedge scl)` 检测主机发来的字节，在第 9 个位回 `0`（ACK）；在读阶段按预设角度逐位驱动 `sda`）。
2. 编译运行（参考 u1-l4 的 iverilog 流程）：`iverilog -g2001 -o sim.vvp SIM/tb_i2c_read.v RTL/i2c_register_read.v` 然后 `vvp -n sim.vvp`。
3. 在 gtkwave 里观察 `scl`、`sda`、`cnt`、`send_shift`、`recv_shift`、`regout`，验证：阶段表里每个 `cnt` 区间的动作与波形一致；`regout` 最终等于你预设的两字节拼接。
4. 把预设角度设成 `12'hABC`（高字节 `0x0A`、低字节 `0xBC`），确认 `regout = 16'h0ABC`，从而验证「高 4 位为 0、低 12 位是角度」的提取逻辑。

**预期结果**：波形里能看到完整的 START/地址/寄存器/重复 START/读两字节/STOP 序列；`regout=0x0ABC`，低 12 位 `0xABC` 正是角度。如果从机模型没写对，`regout` 会异常——这反过来帮你确认对协议的理解。

> 若你不熟悉从机模型编写，可先只做到第 3 步的「主机发送侧」波形观察（不需要从机回数据也能看到主机正确发出 START、地址、寄存器号、重复 START），这也已经覆盖了本讲 80% 的知识点。完整从机模型「待本地实现验证」。

---

## 6. 本讲小结

- `i2c_register_read.v` 是一个**通用 I2C 读控制器**，靠 `SLAVE_ADDR`/`REGISTER_ADDR`/`CLK_DIV` 三个 parameter 适配具体芯片；在本工程里配置成读 AS5600（地址 `0x36`、寄存器 `0x0E`）的 12 位角度。
- SCL 由 `CLK_DIV` 分频产生，频率 \( f_{\text{SCL}} = f_{\text{clk}}/(4\cdot\text{CLK\_DIV}) \)；默认 `CLK_DIV=16` 对应 576 kHz，`fpga_top.v` 实际取 `10` 对应约 922 kHz，都满足 AS5600 ≤1 MHz 的要求。
- SDA 用 `sda_e`/`sda_o` + `assign sda = sda_e ? sda_o : 1'bz` 实现开漏式双向引脚；发送用 `send_shift` 左移 MSB 先发，接收用 `recv_shift` 左移塞 LSB。
- 一次完整读取由 8 位计数器 `cnt` 编码，跑过 START→地址+写→寄存器号→重复 START→地址+读→读 byte1(ACK)→读 byte2(NACK)→STOP 全流程，约 204 个 epoch，`done` 后 `regout` 锁存 16 位结果。
- `fpga_top.v` 用 `start=1'b1` 实现「持续读」，φ 每个控制周期（约 55 µs）刷新一次；`regout` 经 `{i2c_trash, phi}` 拆分，高 4 位（AS5600 恒为 0）丢弃，低 12 位作为机械角度 φ 送入 `foc_top`。
- 本模块是**粉色硬件相关逻辑**：换传感器型号只改这里的 parameter/位宽，蓝色 FOC 核心算法完全不用动——这就是良好封装带来的可移植性。

## 7. 下一步学习建议

- φ 已经读进来了，下一个粉色外设是 **AD7928 SPI ADC**（[RTL/adc_ad7928.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/adc_ad7928.v)），它读的是三相电流。建议进入 **u3-l2 SPI ADC 读取 adc_ad7928.v**，对照本讲的「外设控制器 + 握手脉冲」思路，看 SPI 版本是如何用 `sn_adc`/`en_adc` 脉冲与 `foc_top` 配合的。
- 想把整套外设时序串起来看，可重读 [RTL/fpga_top.v](https://github.com/WangXuan95/FPGA-FOC/blob/3816c6c08f0cab4ff61e9ce9ff829c9f25b62cb3/RTL/fpga_top.v) 第 58~100 行，把 I2C（角度输入）与 SPI（电流输入）两条通路并排对比。
- 对 I2C 协议细节感兴趣的读者，可结合 AS5600 与 AD7928 的数据手册，对照波形理解「开漏 + 上拉」「地址 + 读写位」「ACK/NACK」这些通用总线概念——它们不限于本工程。
