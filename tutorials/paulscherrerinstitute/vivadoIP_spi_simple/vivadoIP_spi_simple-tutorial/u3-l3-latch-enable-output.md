# LE 锁存使能输出时序

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `spi_le`（Latch Enable，锁存使能）这个输出端口**是什么、给谁用、为什么需要它**，以及它与片选 `spi_cs_n` 的区别。
- 根据 `tb/top_tb.vhd` 里 `p_spi` 进程的两处自检断言，复述 LE 在「传输中」与「传输完成后」两种状态下的期望取值，并解释为什么是 `2**SlaveNr`。
- 画出一次 SPI 事务中 `spi_cs_n` 与 `spi_le` 的理想波形。
- 追踪 LE 从 SPI 引擎 → `spi_simple` → `spi_vivado_wrp` → 顶层端口 → testbench 的端到端传递路径，并识别当前 HEAD（开发中快照）里这条链上尚未接通的一环。

## 2. 前置知识

本讲默认你已经读过以下讲义：

- **u2-l2 spi_simple 核心架构与数据流**：知道 `spi_simple` 通过命令/响应 FIFO 把 AXI 总线与 SPI 引擎解耦，并例化了外部引擎 `psi_common_spi_master`。
- **u2-l4 SPI 主控时序与引擎集成**：知道 `spi_simple` 自身不移位，真正生成 `SCK`/`CS_n` 等物理时序的是 `psi_common_spi_master`；`spi_simple` 只做 generic 与端口的透传。
- **u2-l8 测试平台结构与 AXI/SPI 自检**：知道 `top_tb` 用 `p_control` 导演场景、用 `p_spi` 扮演 SPI 从机并自检，二者通过 `SlaveNr`/`SlaveTx`/`ExpectedSlaveRx` 等共享信号协作。

几个用到的术语先点一下：

- **LE / Latch Enable（锁存使能）**：很多 SPI 外设（DAC、LED 驱动、IO 扩展、移位寄存器如 74HC595）内部有两级寄存器——一级**移位寄存器**逐 bit 接收数据，一级**输出/保持寄存器**驱动外部引脚。LE（也叫 LAT、RCLK、XLAT）就是控制「把移位寄存器的内容搬进输出寄存器」的那根信号。这样输出端只在整帧收完后才一次性更新，而不是每收一个 bit 就跳变一次。
- **片选 CS_n**：低有效的从机选择，选中谁就和谁通信；传输期间一直为低。
- **`OnesVector(n)` / `ZerosVector(n)` / `to_uslv(val, n)`**：PSI 测试库（`psi_tb`/`psi_common`）提供的工具函数，分别生成 n 位全 1、n 位全 0、把整数转成 n 位无符号 `std_logic_vector`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `Changelog.md` | 记录 LE 输出在 1.3.0 版本被引入。 |
| `hdl/spi_simple.vhd` | 核心 RTL，声明 `SpiLe` 输出端口，并（按设计意图）把引擎的 LE 透传出去。 |
| `hdl/spi_vivado_wrp.vhd` | 顶层 wrapper，把核心的 `SpiLe` 连到顶层 `spi_le` 端口。 |
| `tb/top_tb.vhd` | 测试平台，在 `p_spi` 中对 `spi_le` 做两处断言式自检，定义了 LE 的期望行为。 |
| `component.xml` | IP-XACT 清单，声明顶层 `spi_le` 端口及其随 `SlaveCnt_g` 变化的位宽。 |
| `scripts/refactoring/alpha.json` | 重构用的端口改名映射表，记录了 `SpiLe` 应映射到引擎的 `spi_le_o`。 |

## 4. 核心概念与源码讲解

### 4.1 LE 输出语义与端口定义

#### 4.1.1 概念说明

先把直觉建立起来。SPI 通信本身只保证「逐 bit 把数据移进从机的移位寄存器」。但很多外设并不希望输出端随着每个 bit 抖动——比如一个 16 路 LED 驱动芯片，如果你边移位边让输出变化，LED 会闪。它需要的是：**移位期间输出保持不动，等整帧移完了，再用一个脉冲把数据「锁存」到输出寄存器**。

这个脉冲就是 **LE（Latch Enable）**。所以 LE 和 CS_n 是两根职责完全不同的线：

| 信号 | 有效电平 | 作用对象 | 时机 |
| --- | --- | --- | --- |
| `spi_cs_n` | 低有效 | 选中某个从机开始通信 | 整个传输期间持续为低 |
| `spi_le` | 高有效（仅在被寻址的从机位） | 命令从机把移位寄存器锁存到输出寄存器 | 传输结束时才拉高 |

本 IP 的 `spi_le` 是**每从机一位**的总线（和 `spi_cs_n` 同宽），这样多从机系统里只有刚通信过的那颗芯片收到锁存脉冲，互不干扰。这恰好契合 1.3.0 版本的更新说明：

> [Changelog.md:1-3](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/Changelog.md#L1-L3) —— 1.3.0 版本新增了 LE 输出（Added LE output）。

#### 4.1.2 核心流程

一次完整 SPI 事务中，LE 的理想行为可以概括为两段：

1. **传输中（CS_n 有效、SCK 在翻转）**：`spi_le` = 全 0（低）。从机只管往移位寄存器里塞数据，输出保持不变。
2. **传输完成（CS_n 释放、回到全高）**：`spi_le` = `2**SlaveNr`，即只有被寻址从机对应的那一位置 1，其余为 0。从机在这一刻把数据锁存到输出寄存器。

把被寻址从机的编号记为 \( n \)（即 `SlaveNr`），LE 总线宽度为 \( S \)（即 `SlaveCnt_g`），则传输完成后 LE 的每一位取值为：

\[
\text{LE}_{\text{after}}[i] = \begin{cases} 1 & i = n \\ 0 & \text{otherwise} \end{cases}
\quad\Longleftrightarrow\quad
\text{LE}_{\text{after}} = 2^{n}
\]

例如 \( S=3 \)、\( n=1 \)，则 LE = 二进制 `010`。

#### 4.1.3 源码精读

LE 首先在核心实体里声明为**每从机一位**的输出（位宽 `SlaveCnt_g-1 downto 0`，与 `SpiCs_n` 完全一致）：

> [hdl/spi_simple.vhd:78](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L78) —— `SpiLe : out std_logic_vector(SlaveCnt_g-1 downto 0)`，核心对外暴露的锁存使能总线。

> [hdl/spi_vivado_wrp.vhd:54](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L54) —— 顶层 wrapper 同样声明 `spi_le : out std_logic_vector(SlaveCnt_g-1 downto 0)`，对外即 IP 的物理 LE 引脚。

在 IP-XACT 清单里，`spi_le` 端口的位宽是**依赖 `SlaveCnt_g` 的表达式**（左边界 = `SlaveCnt_g - 1`），所以用户在 Vivado 里改从机数时，LE 引脚宽度会自动跟着变：

> [component.xml:553-558](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/component.xml#L553-L558) —— `spi_le` 端口声明，`<spirit:left>` 的 `dependency` 为 `(MODELPARAM_VALUE.SlaveCnt_g) - 1`。

注意一个对照（承接 u3-l1）：与 `spi_tri` 不同，`spi_le` **没有** `PORT_ENABLEMENT` 条件——它不带 `TriWiresSpi_g` 之类的使能开关，是**始终存在**的永久输出。也就是说，无论是否启用 3-Wire SPI，这个 IP 对外都会暴露 `spi_le`。

#### 4.1.4 代码实践

1. **目标**：确认 `spi_le` 的位宽规则，并预测不同从机号下的 LE 值。
2. **步骤**：
   - 打开 [hdl/spi_simple.vhd:77-79](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L77-L79)，确认 `SpiCs_n` 与 `SpiLe` 位宽表达式完全相同。
   - 在 [tb/top_tb.vhd:60](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L60) 找到 `SlaveCnt_c : integer := 3`，据此推断仿真中 LE 总线宽 3 位。
3. **观察现象**：分别对 `SlaveNr = 0/1/2`，手算传输完成后 LE 的 3 位取值。
4. **预期结果**：`2**0=1 → "001"`、`2**1=2 → "010"`、`2**2=4 → "100"`，每种情况都只有被寻址从机位为 1。
5. 待本地验证：若你改 `SlaveCnt_c` 为 4，需重新核对这些位宽与取值。

#### 4.1.5 小练习与答案

**练习 1**：为什么 LE 要做成「每从机一位」的总线，而不是像 `spi_mosi` 那样单线？

**答案**：因为锁存是**对单颗芯片**的事件。多从机系统里，一次只寻址一颗，只有它需要在帧末锁存；做成总线就能让 IP 直接把锁存脉冲送到正确那颗，无需外部译码，也不会误触发其他从机的输出寄存器。

**练习 2**：如果某从机的 LE 是「下降沿锁存」而非「高电平锁存」，当前这种「传输结束后拉高」的实现是否还适用？

**答案**：仍可作为起点，但需要保证 LE 在帧末产生一个确定方向的沿。当前实现给出的是电平（结束时为高），沿敏感的器件会在 LE 上升沿锁存；若器件要求下降沿，则需要把空闲态取反或改为脉冲式输出。本 IP 当前不暴露 LE 极性 generic，属待确认的工程细节。

---

### 4.2 testbench 对 LE 的断言

#### 4.2.1 概念说明

在硬件项目里，**testbench 的自检断言往往就是行为规格本身**。`top_tb` 的 `p_spi` 进程扮演一颗 SPI 从机，它在收完一帧、并观察到 CS 释放后，对 `spi_le` 做了两处 `StdlvCompareStdlv` 比较来「定义」LE 应有的样子。我们要读懂这两处断言，就等于读懂了 LE 的规格。

#### 4.2.2 核心流程

`p_spi` 在主循环里先判断「是否有从机被选中」（CS 非全高 = 传输进行中），进入后逐 bit 收发，收完 8 bit 后：

```
收完最后一 bit
   │
   ├─ 断言①：此刻 spi_le 应为全 0（传输中为低）
   │
   ├─ wait until spi_cs_n = 全高（等 CS 释放）
   │
   └─ 断言②：此刻 spi_le 应为 2**SlaveNr（完成后被寻址位为高）
```

#### 4.2.3 源码精读

进入从机仿真分支的判据——CS 非全高即代表有传输：

> [tb/top_tb.vhd:336](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L336) —— `if spi_cs_n /= OnesVector(SlaveCnt_c) then`，`OnesVector` 即「全 1」，CS 不全高说明有从机被拉低选中。

逐 bit 移位循环结束后（传输数据已全部移入从机，但 CS 仍有效）的第一处 LE 断言——传输中 LE 必须为低：

> [tb/top_tb.vhd:388](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L388) —— `StdlvCompareStdlv(ZerosVector(SlaveCnt_c), spi_le, "LE is not low")`，期望 `spi_le` 等于 `SlaveCnt_c` 位全 0。

随后等待 CS 释放，再做第二处断言——完成后 LE 等于 `2**SlaveNr`：

> [tb/top_tb.vhd:390-391](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L390-L391) —— 先 `wait until spi_cs_n = OnesVector(SlaveCnt_c)`（等所有 CS 回到高，传输结束），再断言 `spi_le = to_uslv(2**SlaveNr, SlaveCnt_c)`。

这里 `to_uslv(2**SlaveNr, SlaveCnt_c)` 把整数 `2**SlaveNr` 转成 `SlaveCnt_c` 位无符号向量。`SlaveNr` 是 `p_control` 与 `p_spi` 之间的共享信号（由 `p_control` 在每个场景前赋值，如 [tb/top_tb.vhd:180](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L180) 的 `SlaveNr <= 1`），代表当前被寻址的从机号。所以断言的语义就是「只有刚通信过的那颗从机，其 LE 位在帧末被拉高」。

需要留意：这两处断言检查的是**两个关键时刻的取值**（最后一 bit 移完后、CS 释放后），并非对整段波形连续监视。换言之，testbench 校验的是 LE 状态机的**两个边界点**，而非每个时钟沿。

#### 4.2.4 代码实践（本讲核心实践）

1. **目标**：在 `p_spi` 中定位 LE 的两处比较，解释 `2**SlaveNr` 的由来，并画出一次事务的 CS_n/LE 理想波形。
2. **步骤**：
   - 打开 [tb/top_tb.vhd:350-392](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L350-L392)，找到 bit 移位循环、第 388 行与第 390-391 行。
   - 注意第 342-348 行的从机选择校验循环：它用 `SlaveNr` 决定哪个 `spi_cs_n(s)` 应为 0，与第 391 行用同一个 `SlaveNr` 算 `2**SlaveNr` 是一致的——都是「被寻址的那一位」。
   - 取第一个场景 `SlaveNr = 1`（[tb/top_tb.vhd:180-184](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L180-L184)），Mode 0、8 位、仅写，绘制波形。
3. **观察现象**：CS_n[1] 在传输期间为 0、结束后为 1；LE 全程低，直到 CS 释放后才变成 `010`。
4. **预期结果**（理想波形，`SlaveNr=1`，仅示意电平，非逐拍精确）：

   ```
            +-+ +-+ +-+ +-+ +-+ +-+ +-+ +-+
   SCK   ___| |_| |_| |_| |_| |_| |_| |_| |___        (8 个脉冲)
   CS_n[1]_____|____________________________|‾‾‾‾      传输中低，结束回高
   MOSI     < D7 >< D6 >< D5 >< D4 >< D3 >< D2 >< D1 >< D0 >
   LE     _______________________________________|‾1‾0‾   传输中全 0；CS 释放后 = 010
                                                      ^ 只在第 1 位拉高
   ```

   关键点：LE 的上升**滞后于**数据移位、与 **CS 释放对齐**——这正是「先移完、再锁存」的语义。
5. 待本地验证：波形里 SCK 的精确相位、CS 释放与 LE 拉高之间的拍数需在仿真中确认。

#### 4.2.5 小练习与答案

**练习 1**：如果把第 391 行的 `2**SlaveNr` 误写成 `SlaveNr`，对 `SlaveNr=2` 的场景会发生什么？

**答案**：`2**2 = 4 = "100"`，而 `SlaveNr = 2 = "010"`。期望值会从「第 2 位为 1」错成「第 1 位为 1」，断言会失败。这正说明 LE 是**按位寻址**的总线，必须用 `2**SlaveNr` 才能选中正确的从机位。

**练习 2**：两处 LE 断言之间为什么要插一句 `wait until spi_cs_n = OnesVector(SlaveCnt_c)`？

**答案**：因为 LE 的「拉高」事件以 **CS 释放**为触发条件。必须先等到所有 CS 回到高（传输确认结束），再去比较 LE，否则读到的是「传输中」的低电平，第二处断言必然失败。

---

### 4.3 LE 信号传递路径（含当前 HEAD 的实现现状）

#### 4.3.1 概念说明

承接 u2-l4 的结论：`spi_simple` 自己不产生时序，物理信号都来自引擎 `psi_common_spi_master`。所以 LE 的「端到端路径」应当是四段：

```
psi_common_spi_master.spi_le_o  ──►  spi_simple.SpiLe  ──►  wrapper.spi_le  ──►  顶层 IP 引脚 / tb.spi_le
        (真正生成 LE)                  (透传)                 (透传)               (自检/外设)
```

但我们要诚实地看源码：**在当前 HEAD（提交 `fda4db7`，标题 "DEVEL: 3-Wires SPI interface signal added"，一个开发中快照）下，这条链的第一段——从引擎到 `spi_simple`——并没有接通。** 这是阅读本讲时必须留意的一点。

#### 4.3.2 核心流程

理想链路与现状对照：

| 路径段 | 是否存在 | 证据 |
| --- | --- | --- |
| 引擎 `spi_le_o` 存在（作为 LE 的生产者） | 是（按设计） | 重构映射表把 `SpiLe` 映射到 `spi_le_o` |
| `spi_simple` 把引擎 LE 连到自己的 `SpiLe` 端口 | **否（当前 HEAD 缺失）** | 例化端口映射里没有 `spi_le_o => SpiLe` |
| `spi_simple.SpiLe` → wrapper 顶层 `spi_le` | 是 | wrapper 例化里 `SpiLe => spi_le` |
| wrapper `spi_le` → tb 信号 | 是 | DUT 例化 `spi_le => spi_le` |

#### 4.3.3 源码精读

wrapper 把核心的 `SpiLe` 连到顶层端口（这一段是通的）：

> [hdl/spi_vivado_wrp.vhd:263](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L263) —— `SpiLe => spi_le`，核心到 wrapper 的透传。

但在 `spi_simple` 内部例化 `psi_common_spi_master` 时，端口映射到 `spi_tri_o => SpiTri`、`spi_cs_n_o => SpiCs_n` 就结束了，**并没有 `spi_le_o => SpiLe`**：

> [hdl/spi_simple.vhd:285-299](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L285-L299) —— 引擎例化的端口映射，最后一项是 `spi_cs_n_o => SpiCs_n`，没有出现 `spi_le_o`；同时全文也找不到任何对 `SpiLe` 的赋值语句。

证据链的最后一环来自重构脚本里的改名映射表，它明确告诉我们引擎有 `spi_le_o` 端口、且本应连到 `SpiLe`：

> [scripts/refactoring/alpha.json:760-761](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/refactoring/alpha.json#L760-L761) —— 映射表里 `"SpiCs_n": "spi_cs_n_o"` 紧跟着 `"SpiLe": "spi_le_o"`，说明引擎的 LE 输出叫 `spi_le_o`，按设计意图应接到 `spi_simple` 的 `SpiLe`。

**结论与现状说明**：在当前 DEVEL 快照下，`SpiLe` 虽已声明为核心输出端口（4.1.3），并在 wrapper 一路透传到顶层（4.3.3 第一条），但 `spi_simple` 内部并未把引擎的 `spi_le_o` 接到它，`SpiLe` 实际处于**未被驱动（undriven）**的状态。因此要让 [tb/top_tb.vhd:388](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L388) 与 [tb/top_tb.vhd:391](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L391) 的两处 LE 断言通过，需要同时满足：所用 `psi_common_spi_master` 版本带 `spi_le_o` 输出，且在 `spi_simple.vhd` 的例化里补上 `spi_le_o => SpiLe` 这条映射。本讲把这一缺口标注为**开发中状态（待本地验证 / 待确认）**，而非回避——这正是阅读在制（WIP）仓库时的典型现象：端口与断言先就位、内部连线随后补齐。

> 说明：本讲只做**只读分析**，不会去修改 `hdl/spi_simple.vhd` 补这条线。识别出缺口本身就是本讲的实践目标之一。

#### 4.3.4 代码实践

1. **目标**：亲手把「端到端路径」走一遍，定位当前 HEAD 上缺失的那条连线。
2. **步骤**：
   - 从顶层端口出发：[hdl/spi_vivado_wrp.vhd:54](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L54)（`spi_le` 声明）→ [hdl/spi_vivado_wrp.vhd:263](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L263)（`SpiLe => spi_le`）。
   - 进到核心：[hdl/spi_simple.vhd:78](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L78)（`SpiLe` 端口声明）→ [hdl/spi_simple.vhd:285-299](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L285-L299)（引擎例化的端口映射）。
   - 用 [scripts/refactoring/alpha.json:760-761](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/refactoring/alpha.json#L760-L761) 确认引擎侧端口名 `spi_le_o`。
3. **观察现象**：你会发现在 `spi_simple` 内部，`SpiLe` 从声明处到例化处之间没有任何赋值或连线。
4. **预期结果**：路径在「引擎 → `spi_simple`」这一段断开；要让 4.2 的两处断言成立，需补一条 `spi_le_o => SpiLe`（属源码修改，本讲不做，仅识别）。
5. 待本地验证：补线后跑 `sim/run.tcl` 回归，观察第 388/391 行断言是否通过。

#### 4.3.5 小练习与答案

**练习 1**：为什么 wrapper 那一段（`SpiLe => spi_le`）是通的，而核心内部那段断了，却不影响 wrapper 综合？

**答案**：VHDL 里输出端口未驱动不会阻止综合——它会综合出一根悬空、取值为 `'U'`/`'Z'` 的输出。wrapper 只是把核心的 `SpiLe`（无论被没被驱动）连到顶层，所以 wrapper 层面「连线」是完整的；问题只在核心内部 `SpiLe` 没有驱动源。

**练习 2**：如果只看 `component.xml` 和 wrapper，能不能发现这个缺口？

**答案**：不能。`component.xml`（端口声明）和 wrapper（透传）都只描述「接口形状」，不反映核心内部是否真有驱动源。要发现缺口必须读 `spi_simple.vhd` 的架构体，看 `SpiLe` 是否出现在赋值左侧或例化端口映射里——这正是源码精读的价值。

---

## 5. 综合实践

把本讲的三条线索串起来，完成下面这个**只读分析**任务：

1. **画链路图**：在一张图上标出 LE 的端到端路径（引擎 `spi_le_o` → `spi_simple.SpiLe` → `wrapper.spi_le` → `tb.spi_le`），并在当前 HEAD 上断开的那一段打「×」。
2. **写期望值表**：对 `tb/top_tb.vhd` 里出现过的从机号（`SlaveNr = 0`、`1`），分别列出传输中与传输完成后 `spi_le`（3 位）的期望二进制值，并指出哪一处断言对应哪个值。
3. **解释 `2**SlaveNr`**：用一句话说明为什么是 `2**SlaveNr` 而不是 `SlaveNr`，并结合 [tb/top_tb.vhd:342-348](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L342-L348) 的从机选择逻辑佐证（那里同样用「第 `SlaveNr` 位」来寻址）。
4. **判断现状**：基于 4.3 的结论，预测在当前 HEAD 下直接跑回归，第 388/391 行断言会通过还是失败，并说明依据（`SpiLe` 是否有驱动源）。

> 完成后，你应当能向别人解释清楚：LE 是什么、testbench 如何定义它的行为、它如何从引擎流到顶层引脚，以及这个开发中快照里哪一环还没接好。

## 6. 本讲小结

- **LE 是锁存使能**：命令 SPI 从机在帧末把移位寄存器内容搬到输出寄存器；与持续低有效的 CS_n 不同，LE 在传输中为低、结束时才在被寻址从机位拉高。
- **每从机一位**：`spi_le` 位宽 `SlaveCnt_g-1 downto 0`，与 `spi_cs_n` 同宽；它是永久输出（无 `PORT_ENABLEMENT` 条件，区别于 `spi_tri`）。
- **testbench 即规格**：`top_tb` 在 `p_spi` 里用两处 `StdlvCompareStdlv` 定义了 LE——传输中全 0、CS 释放后等于 `2**SlaveNr`。
- **`2**SlaveNr` 的含义**：LE 是按位寻址的总线，只有被寻址从机的那一位为 1。
- **传递路径**：引擎 `spi_le_o` → `spi_simple.SpiLe` → `wrapper.spi_le` → 顶层。
- **诚实的现状**：当前 DEVEL HEAD（`fda4db7`）下，`spi_simple` 内部尚未把引擎的 `spi_le_o` 接到 `SpiLe`，该端口处于 undriven 状态；要满足断言需依赖带 `spi_le_o` 的引擎版本并补上这条映射（待本地验证）。

## 7. 下一步学习建议

- **u3-l2 3-Wire SPI 与三态/读写位扩展**：本讲提到 `spi_le` 是「永久输出」，而 `spi_tri` 是「条件使能输出」。下一讲正好详解 `spi_tri` 如何通过 `TriWiresSpi_g` + `PORT_ENABLEMENT` 控制是否在 IP 边界导出，可与本讲对照阅读，巩固「端口使能 vs. 端口宽度」两类参数化。
- **延伸阅读源码**：若想确认引擎侧 LE 的真实生成逻辑，可去依赖仓库 `psi_common` 里读 `psi_common_spi_master.vhd` 的 `spi_le_o` 实现（本仓库不含该源码，属外部依赖）。
- **动手验证**：在本地按 u1-l4 的方式跑 `sim/run.tcl`，重点观察 transcript 中第 388/391 行 LE 断言的结果，以验证本讲关于「当前 HEAD 缺口」的判断。
