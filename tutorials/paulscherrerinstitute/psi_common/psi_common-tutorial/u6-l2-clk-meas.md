# 时钟测量 clk_meas

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清「用已知参考时钟测量另一个时钟频率」的**直接计数法**原理，并写出它的量化误差公式。
- 读懂 `psi_common_clk_meas` 的 generic 与端口，知道 `frequency_hz_o` 与 `vld_o` 分别在什么时刻有效。
- 跟踪一次完整测量在**主时钟域**与**测试时钟域**之间的往返流程，理解为何这里用「电平翻转 + 多级同步 + 边沿检测」而不用单周期选通。
- 估算一次测量的精度（绝对 / 相对），并解释饱和、停摆两种边界行为。
- 判断在真实 FPGA 系统中应当把该组件放在哪里（系统自检 / 上电健康检查）。

## 2. 前置知识

本讲默认你已经掌握以下内容（均为前置讲义已建立的概念）：

- **AXI-S / 选通概念**（u1-l4、u6-l1）：什么是「单周期宽的选通脉冲（strobe）」，以及「数到一个比值再翻转/拉脉冲」的倒计数套路。
- **CDC 与 toggle 同步法**（u5-l1）：跨时钟域传递稀疏事件时，先把它转成长期稳定的电平翻转，再用多级触发器同步器采样，最后在目标域做边沿检测还原。本讲的 `clk_meas` 把这一招用了**两次**，只是没有调用 `pulse_cc`，而是在实体内部就地实现。
- **多 bit 跨域的安全条件**（u4-l2、u5-l2）：跨域传一个多 bit 的数值，要么用格雷码指针（如异步 FIFO），要么用握手保证「读的时候数据已稳定」。本讲会看到后者的一个真实例子。

如果你对「频率计（frequency counter）」「闸门时间（gate time）」这类术语不熟悉，没关系，下一节会从零讲起。

## 3. 本讲源码地图

| 文件 | 作用 |
|:-----|:-----|
| [hdl/psi_common_clk_meas.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_clk_meas.vhd) | 被测组件本体，两个进程分别跑在主时钟域与测试时钟域 |
| [testbench/psi_common_clk_meas_tb/psi_common_clk_meas_tb.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_clk_meas_tb/psi_common_clk_meas_tb.vhd) | 自校验测试平台，覆盖正常 / 最大 / 停摆三种情形 |
| [hdl/psi_common_strobe_generator.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_strobe_generator.vhd) | 对照组件：用同样的「倒计数到比值」思路生成单周期选通，用来解释 `clk_meas` 为何改用「翻转」 |

> 提示：`clk_meas` **并不**例化 `strobe_generator` 或 `pulse_cc`，它把倒计数与 toggle-CDC 都写在了自己内部。这里列出这两个文件只是为了讲清原理与对照。

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：测频计数原理、接口与 generic、输出格式（含双时钟域往返流程与精度）、应用场景。

### 4.1 测频计数原理（直接计数法）

#### 4.1.1 概念说明

「测量一个时钟有多快」最朴素的想法是：**用一个已知准确的时间窗口去数被测时钟跳了多少下**。这就是**直接计数法（direct counting）**，也是电子实验室里频率计最经典的工作方式。

具体做法分三步：

1. 用一个已知频率的**参考时钟**（主时钟）生成一段固定长度的**闸门时间**（gate time）\(T_{\text{gate}}\)。
2. 在这段闸门时间内，数被测时钟（测试时钟）出现了多少个上升沿，得到计数值 \(N\)。
3. 因为闸门时间已知，被测频率就是 \(\;f_{\text{test}} = N / T_{\text{gate}}\)。

`psi_common_clk_meas` 把闸门时间取成**整整 1 秒**，于是公式退化成最干净的形式：

\[
f_{\text{test}}\,[\text{Hz}] = N
\]

即「1 秒内数到多少拍，频率就是多少赫兹」。

#### 4.1.2 核心流程与精度

整个测量在两个时钟域里协同完成，往返一次约 1 秒：

```text
主时钟域 clk_master_i              测试时钟域 clk_test_i
─────────────────────             ─────────────────────
倒数 master_frequency_g 拍
  └─ 每 1 s 翻转 Toggle1Hz_M ────► 3 级同步 + 边沿检测
                                       └─ 检测到 1 Hz 边沿
                                          · 把 CntrTest_T 锁存进 Result_T
                                          · 翻转 ResultToggle_T ───┐
                                                                    │
3 级同步 + 边沿检测 ◄──────────────────────────────────────────────┘
  └─ 读 Result_T → frequency_hz_o
     · 拉一拍 vld_o
（若 1 s 内没等到结果 → 输出 0，拉 vld_o）
```

**精度估算**是这个组件最该算清楚的一件事。闸门时间 1 秒、被测频率 \(f_{\text{test}}\)，则计数值：

\[
N = f_{\text{test}} \cdot T_{\text{gate}} = f_{\text{test}}
\]

但闸门的起止边界并不会恰好对齐被测时钟的上升沿，因此计数值存在最多 **±1 拍**的不确定性（数学科叫「±1 个字的量化误差」）。于是：

\[
\Delta f = \pm 1\ \text{Hz} \quad(\text{绝对分辨率})
\]

\[
\frac{\Delta f}{f_{\text{test}}} = \pm \frac{1}{N} = \pm \frac{1}{f_{\text{test}}}\quad(\text{相对误差})
\]

两点直觉结论：

- **绝对精度恒为 ±1 Hz**，与被测频率无关。
- **相对精度随被测频率升高而变好**：测 100 MHz 时相对误差仅 \(10^{-8}\)；测 1 kHz 时相对误差 \(10^{-3}\)（千分之一）；测 10 Hz 时相对误差 10%，已经不可用。

这正是直接计数法的固有特点——**测高频很准，测低频很差**。测低频应当改用「倒数法」（先测周期再取倒数），但 `clk_meas` 没有这么做，它面向的是 MHz 级 FPGA 时钟的健康检查。

此外还有一条重要前提：组件**假设主时钟频率完全准确**。若主时钟本身有相对偏差 \(\varepsilon\)（例如 ±100 ppm 的晶振），测量结果会带上同样的相对偏差。综合误差约为：

\[
\frac{\Delta f}{f_{\text{test}}} \approx \varepsilon + \frac{1}{N}
\]

#### 4.1.3 源码精读

闸门时间的生成本质上就是 u6-l1 里 `strobe_generator` 的「倒计数到比值」套路，只不过这里把「拉一拍脉冲」换成「翻转一个电平」。对照看：

[psi_common_strobe_generator.vhd:30-31](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_strobe_generator.vhd#L30-L31) —— `strobe_generator` 用 `ratio_c = ceil(f_clk/f_strobe)` 当计数上限，数满拉一拍 `vld_o`：

```vhdl
constant ratio_c : integer := integer(ceil(freq_clock_g / freq_strobe_g));
signal count     : integer range 0 to ratio_c := 0;
```

`clk_meas` 用同一个思想、但计数上限直接钉死为主时钟频率（即 1 秒的周期数），到 0 时翻转而不是拉脉冲：

[psi_common_clk_meas.vhd:32-33](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_clk_meas.vhd#L32-L33) —— 主域的 1 Hz 计数器与翻转信号：

```vhdl
signal Cntr1Hz_M   : integer range 0 to master_frequency_g - 1;
signal Toggle1Hz_M : std_logic;
```

[psi_common_clk_meas.vhd:64-74](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_clk_meas.vhd#L64-L74) —— 倒数到 0 即翻转 `Toggle1Hz_M`，得到周期正好 1 秒的「门控电平」：

```vhdl
if Cntr1Hz_M = 0 then
  Cntr1Hz_M   <= master_frequency_g - 1;
  Toggle1Hz_M <= not Toggle1Hz_M;   -- 每 1 s 翻转一次
  ...
else
  Cntr1Hz_M <= Cntr1Hz_M - 1;
end if;
```

为何用「翻转电平」而不是 `strobe_generator` 那样的「单周期脉冲」？因为这条 1 Hz 信号要从主时钟域**跨到测试时钟域**。如果被测时钟比主时钟慢（甚至停摆），一个只有 1 个主时钟周期宽的脉冲几乎必然被采样漏掉——这正是 u5-l1 `pulse_cc` 要解决的脉冲跨域问题。把信号做成「翻转后长期保持」的电平，再用多级同步器慢慢采样，就能在任何频率比下可靠地传过去。

#### 4.1.4 代码实践

1. **目标**：用精度公式估算不同被测频率下的测量误差。
2. **步骤**：在纸上（或计算器里）对下表三个频率分别算出 \(N\)、绝对误差、相对误差。
3. **观察**：相对误差随频率下降而恶化的趋势。
4. **预期结果**：

   | 被测频率 | \(N\)（1 s 内计数） | 绝对误差 | 相对误差 |
   |:--|--:|--:|--:|
   | 100 MHz | 100 000 000 | ±1 Hz | \(1\times10^{-8}\) |
   | 1 MHz | 1 000 000 | ±1 Hz | \(1\times10^{-6}\) |
   | 1 kHz | 1 000 | ±1 Hz | \(1\times10^{-3}\) |

5. 此为纯计算型实践，无需运行仿真即可确认。

#### 4.1.5 小练习与答案

**练习 1**：把闸门时间从 1 秒缩短到 0.1 秒，对 1 MHz 被测时钟的相对误差有何影响？

> **答案**：0.1 秒内 \(N = 100\,000\)，相对误差 = \(1/N = 10^{-5}\)，比 1 秒时差 10 倍。闸门越短、分辨率越差，但更新越快——这是「精度 vs 速度」的固有取舍。

**练习 2**：为什么 `clk_meas` 不适合测量一个 50 Hz 的慢时钟？

> **答案**：1 秒内只能数到 50 拍，相对误差 \(1/50 = 2\%\)；而且 50 Hz 远慢于主时钟，1 Hz 门控边沿未必能被测试域同步器采到，可能直接判为 0。

### 4.2 接口与 generic

#### 4.2.1 概念说明

`clk_meas` 的接口刻意做得极简：一个已知频率的主时钟、一个被测时钟、一个复位，输出就是 32 位频率值加一个有效脉冲。它的定位是「系统里的一个**自检传感器**」，而不是数据通路组件，因此没有任何 AXI-S 数据握手。

#### 4.2.2 核心流程

三个 generic 决定了组件的全部行为：

| generic | 类型 / 默认 | 含义 |
|:--|:--|:--|
| `master_frequency_g` | positive / 125 000 000 | 主时钟频率，单位 Hz。**必须与真实主时钟一致**，闸门时间靠它定义 |
| `max_meas_frequency_g` | positive / 250 000 000 | 被测频率上限，同时是测试域计数器的饱和值（防溢出） |
| `rst_pol_g` | std_logic / '1' | 复位极性 |

注意一个容易踩的坑：`max_meas_frequency_g` 不只是「最大量程」，它还**直接定义了测试域计数器 `CntrTest_T` 的整数范围**（见 4.3.3），所以它必须 ≥ 你想测到的最高频率，否则计数器会溢出。

#### 4.2.3 源码精读

[psi_common_clk_meas.vhd:16-25](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_clk_meas.vhd#L16-L25) —— 实体声明，generic 与端口一一对应：

```vhdl
entity psi_common_clk_meas is
  generic( master_frequency_g    : positive := 125000000;
           max_meas_frequency_g  : positive := 250000000;
           rst_pol_g             : std_logic:= '1');
  port(   clk_master_i           : in  std_logic;
          rst_i                  : in  std_logic;
          frequency_hz_o         : out std_logic_vector(31 downto 0);
          vld_o                  : out std_logic;
          clk_test_i             : in  std_logic);
end entity;
```

几个值得记住的接口约定：

- `frequency_hz_o` 是 **32 位无符号**、单位 Hz、**同步于主时钟** `clk_master_i`。
- `vld_o` 是主时钟域里的**单周期脉冲**，每次更新频率值时拉高一拍。
- 复位 `rst_i` 同步于主时钟；复位极性由 `rst_pol_g` 决定（注意测试域进程里用的是 `if rst_i = rst_pol_g`，而主域进程直接用 `if rst_i = '1'`——两个进程对复位的写法略有差异，使用低有效复位时需留意，见 4.4 的练习）。

#### 4.2.4 代码实践

1. **目标**：从测试平台的 generic 反推被测场景，验证你对接口的理解。
2. **步骤**：打开测试平台，读它的 generic 与时钟设置。
3. **观察**：TB 没有用默认的 125 MHz，而是把频率缩小了 1000 倍。
4. **预期结果**：TB 中 `master_frequency_g = 125_000`（125 kHz），主时钟进程频率也是 125 kHz；这样 1 秒闸门只需 125 000 拍仿真，把仿真时间压到可接受范围。这是仿真「慢时钟组件」的常见手法——**generic 与真实时钟按同一比例缩小，1 Hz 闸门的物理含义不变**。

   参见 [psi_common_clk_meas_tb.vhd:34-35](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_clk_meas_tb/psi_common_clk_meas_tb.vhd#L34-L35) 与 [psi_common_clk_meas_tb.vhd:87-95](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_clk_meas_tb/psi_common_clk_meas_tb.vhd#L87-L95)。

5. 你能否预测：若把 TB 的 `master_frequency_g` 改成 1 000 000（1 MHz）而主时钟进程仍是 125 kHz，`frequency_hz_o` 会偏大还是偏小？（待本地验证，提示：闸门实际变长了 8 倍。）

#### 4.2.5 小练习与答案

**练习 1**：要测量一个最高 200 MHz 的时钟，`max_meas_frequency_g` 至少设多少？

> **答案**：≥ 200 000 000。建议留余量设 250 000 000（默认值），因为它是计数器整数范围上限，低于被测频率会饱和到错误的最大值。

**练习 2**：`frequency_hz_o` 为什么是 32 位？

> **答案**：\(2^{32} \approx 4.29\times10^{9}\)，足以覆盖任何 realistic 的 FPGA 时钟频率（GHz 级），同时 32 位是 AXI / 寄存器总线一次能搬的标准字宽，方便把结果直接挂到寄存器映射里给软件读。

### 4.3 输出格式：双时钟域往返流程与精度行为

> 这一节是全讲的「主菜」。`frequency_hz_o` 与 `vld_o` 的含义，只有在搞清那次「主域 → 测试域 → 主域」的往返之后才能真正理解。

#### 4.3.1 概念说明

输出 `frequency_hz_o` 是一个 32 位无符号的 Hz 数值，`vld_o` 是它的更新脉冲。但因为测量值是在**测试时钟域**里数出来的，要把它安全地交回**主时钟域**输出，组件用了两个方向相反的「电平翻转 + 3 级同步 + 边沿检测」握手——本质就是把 u5-l1 的 toggle-CDC 思想用了两遍：

- **1 Hz 门控**：主域 → 测试域（告诉测试域「开始/结束一秒」）。
- **结果就绪**：测试域 → 主域（告诉主域「新结果出来了，可以读了」）。

第二个握手还顺带解决了一个**多 bit 跨域**的安全问题：测得的计数值 `Result_T` 是一个多 bit 整数，跨域读它必须保证「读的时候它已经稳定」。这里的握手天然满足这一点——主域只有在检测到「结果就绪」翻转之后才去读 `Result_T`，而那时 `Result_T` 早已稳定多拍。所以这里**不需要格雷码**（与异步 FIFO 指针不同），靠的是握手。

#### 4.3.2 核心流程

一次完整测量的往返，按时间顺序：

1. **主域**：`Cntr1Hz_M` 倒数 `master_frequency_g` 拍到 0 → 翻转 `Toggle1Hz_M`，置 `AwaitResult_M := 1`（「我想要一个新结果」）。
2. **跨域（门控）**：`Toggle1Hz_M` 在测试域经 3 级同步器 `Toggle1HzSync_T`，再做边沿检测。
3. **测试域**：检测到 1 Hz 边沿的当拍——把累计计数 `CntrTest_T` 锁进 `Result_T`，把计数器复位成 1（「触发边沿本身算第一拍」），翻转 `ResultToggle_T` 通知主域。
4. **跨域（结果）**：`ResultToggle_T` 在主域经 3 级同步器 `ResultToggleSync_M`，再做边沿检测。
5. **主域**：检测到结果翻转 → 读 `Result_T`、转成 32 位输出到 `frequency_hz_o`、拉一拍 `vld_o`、清 `AwaitResult_M`。
6. **超时分支**：若下一个 1 Hz 到来时 `AwaitResult_M` 仍为 1（测试域一秒没回结果，说明被测时钟停摆或太慢）→ 输出 `frequency_hz_o := 0` 并拉一拍 `vld_o`。

整个往返由两次边沿检测串联，因此 `vld_o` 的更新节奏约为**每秒一次**，再叠加几个时钟的 CDC 延迟。

#### 4.3.3 源码精读

**测试域：数边沿 + 锁存结果 + 饱和保护**

[psi_common_clk_meas.vhd:39-42](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_clk_meas.vhd#L39-L42) —— 测试域信号：

```vhdl
signal Toggle1HzSync_T : std_logic_vector(2 downto 0);
signal CntrTest_T      : integer range 0 to max_meas_frequency_g;
signal Result_T        : integer range 0 to max_meas_frequency_g;
signal ResultToggle_T  : std_logic;
```

[psi_common_clk_meas.vhd:90-111](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_clk_meas.vhd#L90-L111) —— 测试域进程。注意 `Toggle1HzSync_T` 是 3 级移位同步，比较第 2、1 级做边沿检测；锁存与计数器复位在同一拍完成；否则计数器自增，但用 `if CntrTest_T /= max_meas_frequency_g` 做饱和防溢出：

```vhdl
-- 同步 1 Hz 门控（3 级移位）
Toggle1HzSync_T <= Toggle1HzSync_T(1 downto 0) & Toggle1Hz_M;

-- 检测到 1 Hz 边沿：锁存结果、复位计数器、通知主域
if Toggle1HzSync_T(2) /= Toggle1HzSync_T(1) then
  CntrTest_T     <= 1;            -- 触发边沿本身算第一拍
  Result_T       <= CntrTest_T;
  ResultToggle_T <= not ResultToggle_T;
-- 否则计数，且饱和防溢出
elsif CntrTest_T /= max_meas_frequency_g then
  CntrTest_T <= CntrTest_T + 1;
end if;
```

**主域：发请求、收结果、超时判零**

[psi_common_clk_meas.vhd:76-82](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_clk_meas.vhd#L76-L82) —— 主域同步结果翻转（`ResultToggleSync_M` 同样是 3 级 + 边沿检测），检测到变化就读 `Result_T` 输出。注意这里**直接读测试域信号 `Result_T`**——多 bit 跨域之所以安全，是因为握手保证了读时数据已稳定：

```vhdl
ResultToggleSync_M <= ResultToggleSync_M(1 downto 0) & ResultToggle_T;
if ResultToggleSync_M(2) /= ResultToggleSync_M(1) then
  frequency_hz_o <= std_logic_vector(to_unsigned(Result_T, 32));
  AwaitResult_M  <= '0';
  vld_o          <= '1';
end if;
```

[psi_common_clk_meas.vhd:64-74](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_clk_meas.vhd#L64-L74) —— 同一个请求块里还藏着超时逻辑：若新一轮 1 Hz 到来时 `AwaitResult_M` 仍为 1，说明上一秒没收到结果，直接输出 0 并拉 `vld_o`：

```vhdl
Toggle1Hz_M   <= not Toggle1Hz_M;
AwaitResult_M <= '1';
if AwaitResult_M = '1' then
  frequency_hz_o <= (others => '0');
  vld_o          <= '1';
end if;
```

**测试平台验证三种输出形态**

[psi_common_clk_meas_tb.vhd:129-150](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_clk_meas_tb/psi_common_clk_meas_tb.vhd#L129-L150) —— TB 分四段，依次验证正常值、饱和、停摆、恢复正常。每段等 2 秒（确保至少一个完整测量周期），再等到 `vld_o='1'` 比对：

```vhdl
-- 正常：101.35 kHz → 读 101_350，容差 ±50
StdlvCompareInt(101_350, frequency_hz_o, "Wrong Frequency", false, 50);
-- 超量程：400 kHz > max 250 kHz → 饱和读 250_000
StdlvCompareInt(250_000, frequency_hz_o, "Wrong Maximum", false, 50);
-- 停摆：0.1 Hz → 超时读 0
StdlvCompareInt(0, frequency_hz_o, "Wrong Minimum", false, 0);
-- 恢复：52.123 kHz → 读 52_123
StdlvCompareInt(52_123, frequency_hz_o, "Correct Frequency at End", false, 50);
```

`StdlvCompareInt` 的第 5 个参数是容差（多数为 50），正对应 ±1 拍量化误差再留些裕量；唯独「停摆读 0」容差为 0，因为这种情形期望严格为 0。

#### 4.3.4 代码实践

1. **目标**：亲手跟踪一次「结果就绪」往返，验证多 bit 跨域为何安全。
2. **步骤**：在测试平台上把 `MeasFrequency` 初值保持 101.35e3 跑仿真（参照 [TB:54](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_clk_meas_tb/psi_common_clk_meas_tb.vhd#L54) 与 [TB:123-131](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_clk_meas_tb/psi_common_clk_meas_tb.vhd#L123-L131)）。
3. **观察**：在波形上同时看 `Toggle1Hz_M`（主域）、`Toggle1HzSync_T`（测试域）、`CntrTest_T`、`Result_T`、`ResultToggle_T`、`ResultToggleSync_M`（主域）、`frequency_hz_o`、`vld_o`。
4. **预期结果**：
   - 测试域检测到 1 Hz 边沿那一拍，`Result_T` 与 `ResultToggle_T` 同时更新；
   - 几个主时钟周期后，主域 `ResultToggleSync_M` 检出翻转，`frequency_hz_o` 跳到 `Result_T` 的值，`vld_o` 拉高一拍；
   - 读 `Result_T` 的时刻距离它上次更新已远超 1 拍，故多 bit 跨域无撕裂。
5. 仿真在普通行为级下应能通过 `###ERROR###` 自检（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么读 `Result_T` 不需要像异步 FIFO 指针那样用格雷码？

> **答案**：异步 FIFO 的指针**每个时钟都在变**，跨域读必须保证同一时刻各 bit 一致，故用格雷码。这里 `Result_T` 只在 1 Hz 边沿那一拍更新，之后整整 1 秒不变；主域靠 `ResultToggle_T` 握手确认「数据已稳定」后才读，读窗口落在稳定期内，所以用普通二进制 + 握手即可。

**练习 2**：若被测时钟频率刚好等于 `max_meas_frequency_g`，计数器会溢出吗？

> **答案**：不会。`elsif CntrTest_T /= max_meas_frequency_g then CntrTest_T <= CntrTest_T + 1;` 这一行在到达上限后停止自增（饱和）。但被测频率若**超过**上限，结果会被钳制在 `max_meas_frequency_g`，读数失真（TB 中 400 kHz 输入读出 250 000 即是此现象）。

### 4.4 应用场景

#### 4.4.1 概念说明

`clk_meas` 不是数据通路里的组件，而是一个**系统自检 / 健康监测传感器**。它的典型舞台是带处理子系统（PS，如 Zynq）的 SoC FPGA：主时钟 `clk_master_i` 来自 PS 提供的已知频率时钟，而 PL 侧由 MMCM/PLL 生成的各种工作时钟是否真的配对了频率，就用它来逐一核对。组件描述里写得很直白：用它「verify if other clocks are set the correct frequency」。

#### 4.4.2 核心流程（部署思路）

在真实系统里使用 `clk_meas` 的典型接法：

1. 选一个**最可信的时钟**当 `clk_master_i`（通常是 PS 送来、由板载高精度晶振驱动的固定频率时钟），把它的频率填进 `master_frequency_g`。
2. 把每个待监测的 PLL 输出接到一个 `clk_meas` 实例的 `clk_test_i`（一个实例测一个时钟；多时钟就例化多个）。
3. 把各实例的 `frequency_hz_o` 挂到 AXI / AXI-Lite 寄存器映射（参见 u9-l5 的从机），让软件定期读取。
4. 软件把读到的值与期望频率比较（留 ±1 Hz 或更宽的工程裕量），不一致就报警——实现「上电自检」或「运行期时钟健康监测」。

#### 4.4.3 源码精读

组件描述开宗明义，强调「假设主时钟完全准确」这一前提：

[psi_common_clk_meas.vhd:9-11](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_clk_meas.vhd#L9-L11)：

```vhdl
-- This entity measures the frequency of a clock under the assumption that
-- the frequency of the main-clock is exactly correct.
```

文档进一步点明它的用途——「主时钟来自 PS，用它验证其它时钟是否配对了频率」：

[doc/files/psi_common_clk_meas.md:11-13](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/files/psi_common_clk_meas.md#L11-L13)（节选）：

> This entity measures the frequency of a clock under the assumption that the frequency of the main-clock is exactly correct. Generally the system clock comes from PS, the block is useful to verify if other clock are set the correct frequency.

#### 4.4.4 代码实践

1. **目标**：画出一个最小自检部署草图。
2. **步骤**：在纸上画出「PS → `clk_master_i`(125 MHz)」+「MMCM 输出 50 MHz → `clk_test_i`」+「`frequency_hz_o` → AXI-Lite 寄存器 → CPU 读取」的连接框图。
3. **观察**：哪个时钟是「基准」、哪个是「被怀疑对象」、结果怎么送到软件。
4. **预期结果**：CPU 读到约 50 000 000（±1 Hz）；若 MMCM 配错成 100 MHz，读到约 100 000 000，软件据此报错。
5. 这是设计型实践，无需运行（待本地在真实工程中集成验证）。

#### 4.4.5 小练习与答案

**练习 1**：如果系统里只有 125 MHz 一个时钟可用，能否用 `clk_meas` 测它自己？

> **答案**：没有意义——把同一个时钟接到 `clk_master_i` 和 `clk_test_i`，测出来的永远等于 `master_frequency_g` 本身，无法发现该时钟实际偏了多少。必须有一个**独立的、可信的**基准。

**练习 2**：为什么主时钟域进程写 `if rst_i = '1'`，而测试域进程写 `if rst_i = rst_pol_g`？把 `rst_pol_g` 设成 `'0'`（低有效复位）会怎样？

> **答案**：主域复位只在 `rst_i='1'` 时生效，与 `rst_pol_g` 无关；测试域则按 `rst_pol_g` 解释极性。因此当 `rst_pol_g='0'`（低有效）时，主域对复位的处理与 generic 不一致——这是源码里的一处不对称，使用低有效复位前应仔细核对行为（待确认是否影响你的工程，建议默认仍用高有效 `'1'`）。

## 5. 综合实践：用 100 MHz 参考测量一个未知时钟并估算精度

把本讲内容串起来，完成下面这个端到端小任务。

**任务背景**：你有一块板子，主时钟 `clk_master_i` 是一个可信的 **100 MHz** 晶振时钟；PL 侧 MMCM 配置声称输出了 **50 MHz** 的工作时钟，但你怀疑配置有误，想用 `clk_meas` 自检。

**第 1 步——例化与参数**（示例代码，非项目原有）：

```vhdl
-- 示例代码：100 MHz 主时钟，最高测到 200 MHz
i_clkcheck : entity work.psi_common_clk_meas
  generic map(
    master_frequency_g   => 100_000_000,   -- 主时钟 100 MHz
    max_meas_frequency_g => 200_000_000,   -- 量程上限 200 MHz
    rst_pol_g            => '1'
  )
  port map(
    clk_master_i   => clk_100m,             -- 可信基准
    rst_i          => rst,
    clk_test_i     => clk_mmcm_out,         -- 被怀疑的 MMCM 输出
    frequency_hz_o => freq_meas,            -- 32 位 Hz 读数
    vld_o          => freq_meas_vld
  );
```

**第 2 步——预测读数与精度**（纯计算）：

- 闸门 = 1 s，被测 50 MHz ⇒ 期望 `frequency_hz_o` ≈ **50 000 000**。
- 绝对误差 ±1 Hz；相对误差 \(1/5\times10^{7} = 2\times10^{-8}\)。
- 若 MMCM 实际配错成 100 MHz，读数 ≈ **100 000 000**，一眼就能发现。
- 若 MMCM 输出停摆（锁相失败），1 秒后读数 = **0** 并拉 `vld_o`（超时分支）。

**第 3 步——用缩小比例的 TB 验证**（避免 1 秒闸门带来过长仿真）：

仿照真实 TB 的做法，把所有频率同比例缩小 1000 倍（主时钟与两个 generic 都用 kHz 量级），这样 1 秒闸门只需 100 000 拍仿真，而读数关系不变。运行 TB（待本地验证）后应看到 `vld_o` 约每秒一拍，读数稳定在期望值附近 ±1。

**第 4 步——回答精度问题**：

- 组件能分辨的最小频率差是 **1 Hz**（被测频率无关）。
- 主时钟若有 ±50 ppm 误差，50 MHz 读数会带上 ±2500 Hz 的系统偏差——所以「测得准不准，最终取决于基准时钟准不准」。

## 6. 本讲小结

- `clk_meas` 用**直接计数法**测频：在主时钟生成的 **1 秒闸门**内数被测时钟的上升沿，计数值即频率（Hz）。
- 闸门由「倒数 `master_frequency_g` 拍到 0 即翻转」生成——与 u6-l1 `strobe_generator` 同源，但用**翻转电平**而非单周期脉冲，以便可靠跨域。
- 组件内部用了**两个方向相反的 toggle-CDC 握手**（3 级同步 + 边沿检测）：1 Hz 门控（主→测试）与结果就绪（测试→主），把 u5-l1 的套路就地实现两遍。
- 多 bit 计数值 `Result_T` 跨域读回**无需格雷码**，靠握手保证「读时已稳定」——与异步 FIFO 指针的格雷码方案形成对照。
- 精度为**绝对 ±1 Hz**、相对 \(1/f_{\text{test}}\)，主时钟的相对误差会透传；计数器在 `max_meas_frequency_g` 饱和，被测时钟停摆时通过超时机制输出 0。
- 定位是**系统自检传感器**：用可信主时钟核对其它时钟是否配对了频率，结果常经 AXI 寄存器送给软件。

## 7. 下一步学习建议

- 想看「频率/周期生成」的另一面，可回到 **u6-l1** 深读 `strobe_generator` 与 `tickgenerator`，对比「生成节拍」与「测量频率」这对镜像问题。
- 想深入理解本讲两个 toggle-CDC 握手的源头，务必精读 **u5-l1（pulse_cc）**与 **u5-l2（simple_cc/status_cc/bit_cc）**，那里系统地讲了脉冲/状态/位跨越的选型。
- 想把 `frequency_hz_o` 真正送到 CPU，进入 **u9-l5（axi_slave_ipif / axilite_slave_ipif）**，学习如何把一个 32 位结果挂到寄存器映射。
- 若你对「测低频也想高精度」感兴趣，可自行思考如何用倒数法（先测周期再取倒数）改造本组件——这是一个很好的二次开发练习。
