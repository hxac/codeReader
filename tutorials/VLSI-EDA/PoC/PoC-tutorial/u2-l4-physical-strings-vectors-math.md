# physical / strings / vectors / math 包

## 1. 本讲目标

学完本讲后，你应该能够：

- 用 `FREQ`、`TIME`、`BAUD`、`MEMORY` 等物理类型写出带单位的字面量（如 `50 MHz`、`10 ns`），并理解它们之间的换算关系。
- 调用 `to_time`、`to_freq`、`TimingToCycles`、`CyclesToDelay` 完成「频率 ↔ 周期 ↔ 时钟周期数」三角换算。
- 知道 `strings` 包提供的字符串/字符处理与格式化能力（`str_substr`、`str_format`、`to_digit_dec` 等）。
- 知道 `vectors` 包提供的定宽 `T_SLV_*`、向量之向量 `T_SLVV_*` 与二维矩阵 `T_SLM` 三套向量抽象及其互转函数。
- 理解 `math` 包提供的少量但实用的整数数学扩展（三角形数、组合数、最大公约数、最小公倍数）。

本讲承接 [u2-l1 公共包总览](u2-l1-common-packages-overview.md) 中「9 个公共包」的全景，从其中挑出 4 个彼此相关、且都依赖 `utils` 的包深入讲解。它们都不涉及厂商可移植性，是纯算法/纯数据类型的辅助层。

## 2. 前置知识

在进入源码前，先理解三个 VHDL 概念，本讲会反复用到。

### 2.1 物理类型（physical type）

VHDL 内置一个 `time` 类型，可以直接写 `10 ns`、`1 sec` 这样的**带单位字面量**。VHDL 允许我们用 `type ... is ... units ... end units` 自定义同样的「带单位类型」。例如 PoC 定义的 `FREQ`（频率）能让你写 `50 MHz`，`MEMORY`（存储容量）能让你写 `1 KiB`。

带单位的好处是**可读性 + 编译期量纲检查**：把一个 `FREQ` 赋给一个 `time` 变量会直接报错，而不是悄悄算错。

物理类型的底层是一个整数（位置值，position），`1 MHz` 在内部就是「1000 个 `1 kHz`」，即「1 000 000 个 `1 Hz`」。

### 2.2 子类型（subtype）与定宽数组

`subtype T_SLV_8 is std_logic_vector(7 downto 0);` 给一个常用宽度起个短名字。而 `type T_SLVV_8 is array(natural range <>) of T_SLV_8;` 定义「`T_SLV_8` 的一维数组」，也就是「一组 8 位向量」——这是 VHDL-1993 表达「二维数据」的标准做法（因为 1993 版不支持元素是无约束向量的数组）。

### 2.3 依赖与编译顺序

这 4 个包都 `use PoC.utils.all`，其中 `physical` 还额外依赖 `config`、`strings`，`vectors` 额外依赖 `strings`。pyIPCMI 的编译清单 `common.files` 给出了准确顺序：

```text
utils.vhdl  →  config.vhdl  →  math.vhdl  →  strings.vhdl  →  vectors.vhdl  →  physical.vhdl
```

详见 [src/common/common.files:11-17](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/common.files#L11-L17)（注释已标注每个包的职责，`math` 排在 `strings`/`vectors`/`physical` 之前，因为它只依赖 `utils`）。

> 提示：上一讲 [u2-l2 utils 包](u2-l2-utils-package.md) 讲过的 `ite`、`imin`、`imax`、`div_ceil`、`log2ceilnz`、`T_ROUNDING_STYLE` 在本讲会被大量复用，不熟悉时可回看。

## 3. 本讲源码地图

| 文件 | 作用 | 依赖 |
|------|------|------|
| [src/common/physical.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/physical.vhdl) | 自定义物理类型 `FREQ`/`BAUD`/`MEMORY` 及频率↔周期↔周期数换算 | `config`、`utils`、`strings` |
| [src/common/strings.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/strings.vhdl) | 字符/字符串处理、进制定义、格式化、定宽填充符 `C_POC_NUL` | `config`、`utils` |
| [src/common/vectors.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/vectors.vhdl) | 定宽 `T_SLV_*`、向量之向量 `T_SLVV_*`、二维矩阵 `T_SLM` 及互转 | `utils`、`strings` |
| [src/common/math.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/math.vhdl) | 少量整数数学扩展：图形数、组合数、gcd、lcm | `utils` |
| [src/common/common.files](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/common.files) | pyIPCMI 编译清单，定义上述包的编译顺序 | — |

---

## 4. 核心概念与源码讲解

### 4.1 物理类型包 physical

#### 4.1.1 概念说明

`physical` 包解决一个工程里反复出现的琐碎问题：**把人脑里的「100 MHz」「10 ns」「115200 baud」「4 KiB」与硬件里的「时钟周期数 / 整数计数值」互相翻译**。

举个例子：你要在 100 MHz 时钟下产生一个 115200 baud 的 UART 波特率，需要算「分频计数器最大值」。如果直接写常数 `868`，代码读起来毫无含义；而写成 `TimingToCycles(TIME_UNIT_INTERVAL, CLOCK_FREQ)`，含义就一目了然——并且当时钟从 100 MHz 换成 50 MHz 时，常量会自动重算。

为此 PoC 定义了三个自定义物理类型，加上 VHDL 内置的 `time`，覆盖四类「带量纲」的量：

| 类型 | 含义 | 单位链 | 典型字面量 |
|------|------|--------|-----------|
| `time`（内置） | 时间/周期/延迟 | fs→ps→ns→us→ms→sec | `10 ns`、`1 us` |
| `FREQ` | 频率 | Hz→kHz→MHz→GHz | `50 MHz`、`100 MHz` |
| `BAUD` | 波特率（符号率） | Bd→kBd→MBd→GBd | `115200 Bd` |
| `MEMORY` | 存储容量（二进制） | Byte→KiB→MiB→GiB | `4 KiB` |

> 命名约定（见文件头 [physical.vhdl:16-22](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/physical.vhdl#L16-L22)）：`t`=time、`p`=period、`d`=delay、`f`=frequency、`br`=baud rate、`vec`=vector。函数前缀就来自这套约定，例如 `fmin`=频率取小、`tsum`=时间求和。

#### 4.1.2 核心流程

频率与周期是一对倒数关系：

\[
T = \frac{1}{f}, \qquad f = \frac{1}{T}
\]

例如 50 MHz 的周期：

\[
T = \frac{1}{50 \times 10^{6}\,\text{Hz}} = 20 \times 10^{-9}\,\text{s} = 20\,\text{ns}
\]

而「把一段延迟用某个时钟实现，需要多少个周期」就是延迟除以时钟周期，再按取整方式收敛成整数：

\[
N = \left\lceil \frac{\text{Timing}}{T_{\text{clk}}} \right\rceil
\]

`physical` 包把这条链路封装成三个方向互转的函数族：

```text
        to_time / 1 / f
   FREQ ─────────────────────► time   (频率 → 周期)
   time ◄───────────────────── FREQ   to_freq / 1 / T

   time ──TimingToCycles────► natural  (延迟 + 时钟 → 周期数)
   natural ──CyclesToDelay────► time   (周期数 + 时钟 → 实际延迟)
```

`TimingToCycles` 的设计意图是：核的 generic 用 `time`/`FREQ` 描述「人类意图」（如「消抖时间 10 ms」「刷新率 100 Hz」），在 elaboration 阶段换算成某个具体时钟下的计数器宽度。

#### 4.1.3 源码精读

**(1) 三个物理类型的定义**

```vhdl
type FREQ is range 0 to integer'high units
    Hz;
    kHz = 1000 Hz;
    MHz = 1000 kHz;
    GHz = 1000 MHz;
end units;
```

见 [physical.vhdl:68-73](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/physical.vhdl#L68-L73)。`BAUD`（[L75-L80](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/physical.vhdl#L75-L80)）与 `MEMORY`（[L82-L87](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/physical.vhdl#L82-L87)）结构完全一致。注意 `MEMORY` 用 1024 进制（`KiB = 1024 Byte`），区别于 `FREQ`/`BAUD` 的 1000 进制。

另外还定义了 4 个向量类型用于聚合运算（[L90-L93](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/physical.vhdl#L90-L93)）：`T_TIMEVEC`、`T_FREQVEC`、`T_BAUDVEC`、`T_MEMVEC`，配合后面的 `tmin/tmax/tsum` 等向量版函数使用。

**(2) 频率 → 周期：`to_time`**

```vhdl
function to_time(f : FREQ) return time is
    variable res : time;
begin
    res := div(1000 MHz, f) * 1 ns;
    ...
    return res;
end function;
```

见 [physical.vhdl:293-301](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/physical.vhdl#L293-L301)。它没有直接写 `1 sec / f`，而是写成 `(1000 MHz / f) * 1 ns`——这是为了**把中间结果控制在 32 位整数范围内**，绕开 Altera Quartus 对 `time` 类型 64 位整数表示的限制（见 [L245-L252](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/physical.vhdl#L245-L252) 的注释）。`div` 是一个返回 `real` 的「安全除法」，下一小节解释。

**(3) 跨类型除法：`div` 与最小时间分辨率**

```vhdl
function div(a : time; b : time) return REAL is
    constant MTRIS : time := MinimalTimeResolutionInSimulation;
    ...
```

见 [physical.vhdl:240-274](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/physical.vhdl#L240-L274)。这里有个**仿真器兼容性陷阱**：VHDL 标准规定最小时间分辨率（MTR）是 1 fs，但很多仿真器默认 MTR 是 1 ps 甚至 1 ns。如果直接 `a / b`，当 `a` 小于 MTR 时会被「截断成 0」导致除零。

所以包里先用 `MinimalTimeResolutionInSimulation` 探测当前仿真器的实际 MTR（[L227-L236](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/physical.vhdl#L227-L236)），再把 `a`、`b` 按数量级分段预缩放成 `real`，最后做浮点除法。分段逻辑见 [L253-L271](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/physical.vhdl#L253-L271)。

> 这是 PoC 反复出现的工程哲学：**一份源码要在 XST、Quartus、Vivado、QuestaSim、GHDL 上都跑通**，所以代码里到处是带版本/厂商注释的 workaround。

**(4) 跨类型运算符重载：`/` 与 `*`**

```vhdl
function "/"(x : real; t : time) return FREQ is   -- 1.0 / 10 ns  → 100 MHz
function "/"(x : real; f : FREQ) return time is    -- 1.0 / 50 MHz → 20 ns
function "*"(t : time; f : FREQ) return real is    -- 10 ns * 50 MHz → 0.5（无量纲周期数）
```

见 [physical.vhdl:390-405](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/physical.vhdl#L390-L405)。第三条 `time * FREQ → real` 特别有用：它直接告诉你「这段延迟相当于几个时钟周期」，结果是个纯实数（如 `0.5` 表示半个周期）。

**(5) 延迟 → 周期数：`TimingToCycles`**

```vhdl
function TimingToCycles(Timing : time; Clock_Frequency : FREQ;
                        RoundingStyle : T_ROUNDING_STYLE := ROUND_UP) return natural is
begin
    return TimingToCycles(Timing, to_time(Clock_Frequency), RoundingStyle);
end function;
```

见 [physical.vhdl:930-933](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/physical.vhdl#L930-L933)（接受 `FREQ` 的重载，内部转调 `time` 版本）。真正干活的 `time` 版本在 [L892-L928](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/physical.vhdl#L892-L928)：

```vhdl
res_real := div(Timing, Clock_Period);     -- 实数周期数，如 0.5
case RoundingStyle is
    when ROUND_TO_NEAREST => res_nat := natural(round(res_real));
    when ROUND_UP         => res_nat := natural(ceil(res_real));
    when ROUND_DOWN       => res_nat := natural(floor(res_real));
    ...
end case;
```

默认 `ROUND_UP`：哪怕只需要 0.5 个周期，也得向上取整成 1 个周期（硬件计数器必须是整数）。`T_ROUNDING_STYLE` 这个枚举来自 `utils` 包（见 u2-l2）。

**真实使用**：UART 的波特率分频 [uart_bclk.vhdl:67](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/uart/uart_bclk.vhdl#L67) 用 `TimingToCycles(TIME_UNIT_INTERVAL, CLOCK_FREQ)`；消抖核 [io_Debounce.vhdl:72](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/io_Debounce.vhdl#L72) 用 `TimingToCycles(BOUNCE_TIME, CLOCK_FREQ)`；七段数码管刷新 [io_7SegmentMux_BCD.vhdl:71](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/io_7SegmentMux_BCD.vhdl#L71) 用 `TimingToCycles(to_time(REFRESH_RATE), CLOCK_FREQ)`——注意它先用 `to_time` 把 `FREQ` 类型的刷新率换成 `time` 再传入。

**(6) 美观打印：`to_string`**

```vhdl
function to_string(f : FREQ; precision : natural) return string is
    ...
    if (f < 1 kHz) then unit(1 to 2) := "Hz"; ...
    elsif (f < 1 MHz) then unit := "kHz"; ...
```

见 [physical.vhdl:976-995](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/physical.vhdl#L976-L995)。它自动选择最合适的单位并按指定精度格式化，例如把 `50000000 Hz` 打印成 `50.000 MHz`。`time`/`BAUD`/`MEMORY` 版本结构相同（[L946-L974](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/physical.vhdl#L946-L974)、[L997-L1016](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/physical.vhdl#L997-L1016)、[L1018-L1037](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/physical.vhdl#L1018-L1037)）。

#### 4.1.4 代码实践

**实践目标**：用 `FREQ`/`time` 类型与 `to_time`、`*`、`TimingToCycles` 验证「50 MHz 与 10 ns 的周期关系」。

**操作步骤**（以下为**示例代码**，不是项目原有文件，请存成独立 `.vhdl` 用 GHDL/仿真器跑）：

```vhdl
-- 示例代码：frequency_relationship_tb.vhdl
library IEEE;
use     IEEE.std_logic_1164.all;
use     IEEE.math_real.all;

library PoC;
use     PoC.physical.all;

entity frequency_relationship_tb is
end entity;

architecture tb of frequency_relationship_tb is
    constant CLK_FREQ   : FREQ := 50 MHz;        -- 50 MHz 频率
    constant DELAY      : time := 10 ns;          -- 10 ns 延迟
    constant PERIOD     : time := to_time(CLK_FREQ);              -- 期望 20 ns
    constant HALF_CYC   : real := DELAY * CLK_FREQ;               -- 期望 0.5
    constant CYC_NEEDED : natural := TimingToCycles(DELAY, CLK_FREQ);  -- 期望 1
begin
    process
    begin
        report "周期 to_time(50 MHz) = " & to_string(PERIOD, 3);          -- 20.000 ns
        report "10 ns * 50 MHz       = " & str_format(HALF_CYC, 3);        -- 0.500
        report "TimingToCycles       = " & integer'image(CYC_NEEDED);     -- 1
        report "反向 to_freq(20 ns)   = " & to_string(to_freq(PERIOD), 3);-- 50.000 MHz
        wait;
    end process;
end architecture;
```

**需要观察的现象**：

1. `to_time(50 MHz)` 应输出 `20.000 ns`（验证 \(T = 1/f\)）。
2. `10 ns * 50 MHz` 应输出 `0.500`（10 ns 正好是 50 MHz 周期的一半）。
3. `TimingToCycles(10 ns, 50 MHz)` 应输出 `1`（半个周期向上取整为 1 个周期）。
4. `to_freq(20 ns)` 应回到 `50.000 MHz`，验证换算是可逆的。

**预期结果**：四条 `report` 分别打印 `20.000 ns`、`0.500`、`1`、`50.000 MHz`。若你的仿真器 MTR 不是 1 fs，`div` 内部的 `MinimalTimeResolutionInSimulation` 会自动适配，结果应不受影响。

> ⚠️ **待本地验证**：以上未在本机实际运行，需用 GHDL（仓库 `README` 提到 GHDL 0.31 已测）或 QuestaSim 编译 PoC 公共包后再 elaboration 本测试台。`physical` 包**不被 Xilinx 14.7 之前的 XST 支持**（见 [physical.vhdl:25](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/physical.vhdl#L25) 的 ATTENTION 注释）。

#### 4.1.5 小练习与答案

**练习 1**：把 `CLK_FREQ` 改成 `100 MHz`，`DELAY` 保持 `10 ns`，`TimingToCycles` 的结果会变成多少？`DELAY * CLK_FREQ` 呢？

**答案**：100 MHz 周期 = 10 ns，所以 `10 ns * 100 MHz = 1.000`（恰好 1 个周期），`TimingToCycles = 1`（`ceil(1.0)=1`）。

**练习 2**：`div(time, time)` 为什么要先调用 `MinimalTimeResolutionInSimulation`，而不是直接写 `real(a) / real(b)`？

**答案**：VHDL 的 `time` 内部是 64 位整数，但很多仿真器（如 iSim、xSim 默认 MTR=1 ps）会截断小于 MTR 的时间值。直接相除在 `a` 小于 MTR 时会被当成 0，引发除零或结果错误；先探测 MTR 再分段预缩放成 `real` 才能保证跨仿真器一致。

**练习 3**：`Memory` 容量 `1 GiB` 等于多少 `Byte`？为什么用 1024 而不是 1000？

**答案**：`1 GiB = 1024 MiB = 1024² KiB = 1024³ Byte = 1 073 741 824 Byte`。用 1024 是因为存储容量传统上按二进制（2¹⁰）编址，区别于频率/波特率的十进制 1000。

---

### 4.2 字符串与向量辅助包 strings / vectors

`strings` 与 `vectors` 解决的是「报告信息怎么拼」「二维数据怎么搬」这类问题。前者大量被 `physical` 的 `to_string` 复用，后者是后续 FIFO、cache、总线等核处理「一组字」的基础工具。

#### 4.2.1 概念说明

**strings 包**提供四类能力：

1. **字符判定与转换**：`chr_isDigit`、`chr_isHexDigit`、`chr_toLower`、`chr_toUpper` 等。
2. **进制定义与解析**：`to_digit_dec('7') → 7`、`to_natural_hex("FF") → 255`，统一处理二/八/十/十六进制，失败返回 `-1`。
3. **格式化**：`str_format(3.14159, 2) → "3.14"`（VHDL-2008 之前 `real` 没有好用的 `to_string`）、`to_string(slv, 'h')` 把向量格式化成十六进制。
4. **字符串操作**：`str_substr`、`str_trim`、`str_replace`、`str_pos`（查找）、`str_imatch`（大小写无关比较）。

它还有一个关键常量 `C_POC_NUL`——这是 PoC 全库用来「填充/终止定长字符串」的字符。

**vectors 包**提供三套向量抽象：

| 抽象 | 含义 | 示例 |
|------|------|------|
| `T_SLV_8` 等 | 定宽 `std_logic_vector` 子类型 | 一个 8 位数据 |
| `T_SLVV_8` 等 | 「`T_SLV_8` 的数组」=向量之向量 | 一组 8 位数据（如 FIFO 存储阵列） |
| `T_SLM` | `std_logic` 的二维数组（矩阵） | 把上面那组数据看成「行×列」的位矩阵 |

三者可以通过 `to_slv` / `to_slvv_*` / `to_slm` 互相转换。`T_SLM` 还重载了 `and`/`or`/`xor` 等布尔运算符，可以像操作普通向量一样操作整个矩阵。

#### 4.2.2 核心流程

**为什么需要 `C_POC_NUL`？** 定长 VHDL 字符串（如 `string(1 to 8)`）必须填满 8 个字符，但实际内容可能只有 3 个。PoC 用一个「填充符」标记「有效内容到此为止」，类似 C 字符串的 `\0`，但不用真正的 `NUL`：

```text
真实内容 "abc"  →  存成 "abc`````"  （用 ` 把后面填满）
                       ↑ 有效内容
```

**为什么需要 `T_SLVV_*` 与 `T_SLM` 两套？** 因为同一组「N 个 M 位字」有两种自然视角：

- 按**字**操作（取第 i 个字、反转字的顺序）→ 用 `T_SLVV_M`。
- 按**位矩阵**操作（取某列、对整个矩阵做 `and`）→ 用 `T_SLM`。

两者可通过 `to_slm` / `to_slvv_*` 互转，按当前任务选最顺手的视角。

#### 4.2.3 源码精读

**(1) strings：填充符 `C_POC_NUL`**

```vhdl
constant C_POC_NUL : character := ite((SYNTHESIS_TOOL /= SYNTHESIS_TOOL_ALTERA_QUARTUS2), NUL, '`');
```

见 [strings.vhdl:57](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/strings.vhdl#L57)。注释 [L48-L56](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/strings.vhdl#L48-L56) 解释了为什么：Altera Quartus-II 见到 `NUL`（`character'val(0)`）会崩溃，所以检测到 Quartus 时改用反引号 `` ` ``。这是又一个「按综合工具切换实现」的例子（`SYNTHESIS_TOOL` 枚举来自 `config` 包）。

**(2) strings：实数格式化 `str_format`**

```vhdl
function str_format(Value : REAL; precision : natural := 3) return string is
    constant int  : integer := integer(floor(val));
    constant frac : integer := integer(round((val - real(int)) * 10.0**precision));
    ...
```

见 [strings.vhdl:391-403](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/strings.vhdl#L391-L403)。它手动把整数部分与小数部分分别转成字符串再拼接，并处理了小数部分四舍五入导致的进位（`overflow`）。`physical` 包所有 `to_string` 的精度控制最终都落到这里。

**(3) strings：字符到数字 `to_digit_dec`**

```vhdl
function to_digit_dec(chr : character) return T_DIGIT_DEC is
begin
    if chr_isDigit(chr) then
        return character'pos(chr) - CHARACTER'pos('0');
    else
        return -1;
    end if;
end function;
```

见 [strings.vhdl:528-535](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/strings.vhdl#L528-L535)。用字符的 ASCII 位置值相减把 `'7'` 变成 `7`，非法字符返回 `-1`。`to_digit_hex`（[L538-L545](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/strings.vhdl#L538-L545)）在此基础上扩展了 `a-f`/`A-F`。

**(4) strings：子串 `str_substr`**

```vhdl
-- str_substr("Hello World.", 7, 0) => "World."   从位置 7 到末尾
-- str_substr("Hello World.", 7, 5) => "World"     从位置 7 取 5 个字符
-- str_substr("Hello World.", 0,-3) => "Hello Wo."  到距右边界 3 处
```

见 [strings.vhdl:886-919](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/strings.vhdl#L886-L919)。它支持「负 start = 从右边界起算」「负 length = 到距右边界 length 处」两种便捷语义，比标准库的切片更友好。

**(5) vectors：二维矩阵 `T_SLM`**

```vhdl
type T_SLM is array(natural range <>, natural range <>) of std_logic;
```

见 [vectors.vhdl:79](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/vectors.vhdl#L79)。紧跟其后的注释（[L80-L93](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/vectors.vhdl#L80-L93)）给出两条**重要使用约束**：

1. 矩阵信号**必须用 `'Z'` 初始化**，否则仿真结果不正确：
   ```vhdl
   signal myMatrix : T_SLM(3 downto 0, 7 downto 0) := (others => (others => 'Z'));
   ```
2. **Xilinx iSim 的 bug**：`myMatrix'range(2)` 总是返回 `myMatrix'range(1)`，所以源码里到处用 `'high(2)`/`'low(2)`/`'length(2)` 代替 `'range(2)` 作为 workaround。

**(6) vectors：矩阵布尔运算符**

```vhdl
function "and"(a, b : t_slm) return t_slm is
    variable bb, res : t_slm(a'range(1), a'range(2));
begin
    ...
    res(i, j) := a(i, j) and bb(i, j);
```

见 [vectors.vhdl:308-318](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/vectors.vhdl#L308-L318)。整套 `not/and/or/xor/nand/nor/xnor` 都重载了（[L297-L357](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/vectors.vhdl#L297-L357)），可以一句 `mask and matrix` 完成逐位掩码。

**(7) vectors：扁平向量 ↔ 矩阵 `to_slm`**

```vhdl
function to_slm(slv : std_logic_vector; ROWS : positive; COLS : positive) return T_SLM is
    variable slm : T_SLM(ROWS - 1 downto 0, COLS - 1 downto 0);
begin
    for i in 0 to ROWS - 1 loop
        for j in 0 to COLS - 1 loop
            slm(i, j) := slv((i * COLS) + j);   -- 行主序展开
```

见 [vectors.vhdl:753-762](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/vectors.vhdl#L753-L762)。把一维 `slv` 按「`COLS` 位一行」重排成矩阵。反向把矩阵压扁回向量用 `to_slv(slm)`（[L518-L527](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/vectors.vhdl#L518-L527)）。

**(8) vectors：矩阵的字符串视图 `to_string_bin`**

```vhdl
function to_string_bin(slm : T_SLM; groups : positive := 4; format : character := 'h') return string is
    ...
    for i in slm'low(1) to slm'high(1) loop
        for j in slm'high(2) downto slm'low(2) loop
            Result(Writer) := to_char(slm(i, j));
```

见 [vectors.vhdl:1000-1025](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/vectors.vhdl#L1000-L1025)。它把矩阵按「一行一行、每 4 位一组」打印出来，是调试 FIFO/缓存内部存储阵列时的利器。

#### 4.2.4 代码实践

**实践目标**：用 `strings` 把一个十六进制字符串解析成整数，再用 `vectors` 把扁平向量重排成矩阵并打印。

**操作步骤**（**示例代码**，非项目原有文件）：

```vhdl
-- 示例代码：strings_vectors_tb.vhdl
library IEEE;
use     IEEE.std_logic_1164.all;

library PoC;
use     PoC.strings.all;
use     PoC.vectors.all;

entity strings_vectors_tb is end entity;

architecture tb of strings_vectors_tb is
    -- 1) 把 "DEADBEEF" 解析成整数（十六进制）
    constant VAL : integer := to_natural_hex("DEADBEEF");   -- 期望 3735928559

    -- 2) 把 16 位扁平向量重排成 2 行 8 列矩阵
    constant FLAT : std_logic_vector(0 to 15) := x"ABCD";   -- 注意此处为示意写法
    constant MAT  : T_SLM := to_slm(FLAT, 2, 8);            -- 2 行 × 8 列
begin
    process
    begin
        report "hex DEADBEEF = " & integer'image(VAL);          -- 3735928559
        report "digit 'F'    = " & integer'image(to_digit_hex('F'));  -- 15
        report "str_format π = " & str_format(3.14159, 2);       -- 3.14
        report "矩阵视图: " & LF & to_string(MAT, 4, 'b');        -- 2 行二进制
        wait;
    end process;
end architecture;
```

**需要观察的现象**：

1. `to_natural_hex("DEADBEEF")` 解析出十进制 `3735928559`。
2. `to_digit_hex('F')` 返回 `15`，`to_digit_hex('G')` 会返回 `-1`（非法）。
3. `str_format(3.14159, 2)` 产出 `3.14`。
4. `to_string(MAT, 4, 'b')` 把矩阵按每行 8 位、4 位一组打印成两行。

**预期结果**：四条 `report` 分别打印 `3735928559`、`15`、`3.14`，以及两行二进制矩阵视图（每行形如 `1010 1011` / `1100 1101`）。

> ⚠️ **待本地验证**：`x"ABCD"` 与 `std_logic_vector(0 to 15)` 的方向组合在严格类型检查下可能需要调整成 `downto`；矩阵打印的精确换行取决于仿真器对 `LF` 的处理。请以本地仿真器实际输出为准。

#### 4.2.5 小练习与答案

**练习 1**：`str_substr("Hello World.", 7, 5)` 和 `str_substr("Hello World.", 0, -3)` 各返回什么？

**答案**：前者从位置 7 取 5 个字符 = `"World"`；后者从开头取到距右边界 3 处 = `"Hello Wo."`（去掉了末尾的 `rld`，注意末尾还会含一个 `.`）。

**练习 2**：`C_POC_NUL` 在 Quartus-II 下是什么字符？为什么？

**答案**：是反引号 `` ` ``。因为 Quartus-II 遇到 `NUL`（`character'val(0)`）会崩溃，PoC 通过 `ite` 检测到 `SYNTHESIS_TOOL = SYNTHESIS_TOOL_ALTERA_QUARTUS2` 时改用 `` ` `` 作为填充/终止符。

**练习 3**：给定一个 `T_SLM` 信号，为什么注释要求必须用 `'Z'` 初始化？

**答案**：注释（[vectors.vhdl:81-82](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/vectors.vhdl#L81-L82)）指出 iSim/vSim/GHDL/gtkwave 在矩阵未初始化为 `'Z'` 时仿真结果不正确；用 `'Z'` 可以让未赋值位在波形里清晰可辨，避免与 `'U'`/`'X'` 混淆。

---

### 4.3 数学扩展包 math

#### 4.3.1 概念说明

`math` 包很小，只提供 6 个函数，都是标准库没有但硬件描述偶尔用得上的整数数学：

- **图形数（figurate numbers）**：`squareNumber`、`cubicNumber`、`triangularNumber`。
- **组合数**：`binomialCoefficient`（\( \binom{N}{K} \)）。
- **数论**：`greatestCommonDivisor`（gcd）、`leastCommonMultiple`（lcm）。

它主要服务于 elaboration 阶段的常量推导（比如算地址译码需要的某种规律数、算两个时钟周期求公共节拍的最小公倍数），而不是综合成实际硬件电路。

#### 4.3.2 核心流程

三类数的数学定义：

\[
\text{square}(N) = N^2, \quad \text{cube}(N) = N^3
\]

\[
\text{triangular}(N) = 1 + 2 + \dots + N = \frac{N(N+1)}{2}
\]

组合数：

\[
\binom{N}{K} = \frac{N!}{K!\,(N-K)!} = \prod_{i=1}^{K} \frac{N+1-i}{i}
\]

最大公约数（欧几里得算法）：

\[
\gcd(N_1, N_2) = \gcd(N_2,\; N_1 \bmod N_2), \quad \gcd(a, 0) = a
\]

最小公倍数与 gcd 的关系：

\[
\text{lcm}(N_1, N_2) = \frac{N_1 \times N_2}{\gcd(N_1, N_2)}
\]

#### 4.3.3 源码精读

**(1) 三角形数**

```vhdl
function triangularNumber(N : natural) return natural is
begin
    return (N * (N + 1) / 2);
end function;
```

见 [math.vhdl:67-71](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/math.vhdl#L67-L71)。例如 `triangularNumber(4) = 4×5/2 = 10`（即 1+2+3+4）。

**(2) 组合数（不直接算阶乘，避免溢出）**

```vhdl
function binomialCoefficient(N : positive; K : positive) return positive is
    variable Result : positive;
begin
    Result := 1;
    for i in 1 to K loop
        Result := Result * (((N + 1) - i) / i);
    end loop;
    return Result;
end function;
```

见 [math.vhdl:74-82](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/math.vhdl#L74-L82)。它用连乘形式 \(\prod (N+1-i)/i\) 一边乘一边除，比先算三个阶乘再相除更不容易在中间步骤溢出。

> ⚠️ 注意：由于每轮做**整数除法** `(N+1-i)/i`，结果在某些输入下并不严格等于数学组合数（例如 `binomialCoefficient(5,2)`：第 1 轮 `5/1=5`，第 2 轮 `4/2=2`，`5*2=10` ✓ 正确；但 `binomialCoefficient(6,4)` 会出现整除截断问题）。**使用前务必按你的输入验证**，或改写为先乘后除的写法。这是一个值得在阅读源码时留意的细节。

**(3) 最大公约数（欧几里得算法）**

```vhdl
function greatestCommonDivisor(N1 : positive; N2 : positive) return positive is
    variable M1 : positive;
    variable M2 : natural;
    variable Remainer : natural;
begin
    M1 := imax(N1, N2);     -- 大者做被除数
    M2 := imin(N1, N2);     -- 小者做除数
    while M2 /= 0 loop
        Remainer := M1 mod M2;
        M1 := M2;
        M2 := Remainer;
    end loop;
    return M1;
end function;
```

见 [math.vhdl:85-98](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/math.vhdl#L85-L98)。经典的辗转相除，先用 `imax`/`imin`（来自 `utils`）保证被除数不小于除数。变量名 `Remainer` 是源码里的拼写（应为 Remainder），保持原样引用。

**(4) 最小公倍数**

```vhdl
function leastCommonMultiple(N1 : positive; N2 : positive) return positive is
begin
    return ((N1 * N2) / greatestCommonDivisor(N1, N2));
end function;
```

见 [math.vhdl:101-104](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/math.vhdl#L101-L104)。直接复用 gcd，套用 \(\text{lcm} = N_1 N_2 / \gcd\) 公式。

#### 4.3.4 代码实践

**实践目标**：验证 gcd / lcm 互为校验，并手算 `triangularNumber`。

**操作步骤**（**示例代码**，非项目原有文件）：

```vhdl
-- 示例代码：math_tb.vhdl
library PoC;
use     PoC.math.all;

entity math_tb is end entity;

architecture tb of math_tb is
    constant G : positive := greatestCommonDivisor(12, 18);   -- 期望 6
    constant L : positive := leastCommonMultiple(12, 18);     -- 期望 36
    constant T : natural  := triangularNumber(4);             -- 期望 10
begin
    process
    begin
        report "gcd(12,18) = " & integer'image(G);   -- 6
        report "lcm(12,18) = " & integer'image(L);   -- 36
        report "12*18 = " & integer'image(12*18) & "  gcd*lcm = " & integer'image(G*L);  -- 都=216
        report "triangular(4) = " & integer'image(T);  -- 10
        wait;
    end process;
end architecture;
```

**需要观察的现象**：

1. `gcd(12,18) = 6`，`lcm(12,18) = 36`。
2. **校验恒等式**：`12*18 = 216` 恰好等于 `gcd*lcm = 6*36 = 216`，这印证了 \(\gcd \times \text{lcm} = N_1 \times N_2\)。
3. `triangular(4) = 10`。

**预期结果**：三条核心 `report` 打印 `6`、`36`、`10`，恒等式校验两侧都为 `216`。

> ⚠️ **待本地验证**：组合数 `binomialCoefficient` 因整数除法截断，建议你自己加几条 `report binomialCoefficient(...)` 与数学值对照后再用于设计。

#### 4.3.5 小练习与答案

**练习 1**：用 `greatestCommonDivisor` 求 `gcd(1071, 462)`，手算验证。

**答案**：`1071 mod 462 = 147` → `462 mod 147 = 21` → `147 mod 21 = 0`，所以 `gcd = 21`。

**练习 2**：`leastCommonMultiple` 为什么写成 `(N1*N2)/gcd` 而不是直接循环找最小公倍数？

**答案**：因为 \(\text{lcm}\) 与 \(\gcd\) 有恒等式 \(N_1 \times N_2 = \gcd \times \text{lcm}\)，先求 gcd（欧几里得算法 \(O(\log \min(N_1,N_2))\)）再一次乘除，远快于暴力枚举，且代码更简洁。

**练习 3**：`math` 包只 `use PoC.utils.all`（见 [math.vhdl:36](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/math.vhdl#L36)），它在 `greatestCommonDivisor` 里用到了 `utils` 的哪些函数？

**答案**：用到了 `imax` 和 `imin`（用来保证 `M1 ≥ M2`），这两个函数在 [u2-l2 utils 包](u2-l2-utils-package.md) 讲过。

---

## 5. 综合实践

把四个包串起来，完成一个「**时钟规划器**」常量推导任务（**示例代码**，非项目原有文件）：

> 假设你有一个运行在 `125 MHz` 的时钟域，需要：①产生每 `5 us` 一次的采样脉冲；②与一个 `50 MHz` 的慢时钟找「公共节拍」（最小公倍数周期）。请用 `physical` 与 `math` 算出：采样脉冲的计数器宽度（周期数）、公共节拍对应的周期数。

```vhdl
-- 示例代码：clock_planner_tb.vhdl
library IEEE;
use     IEEE.math_real.all;
library PoC;
use     PoC.physical.all;
use     PoC.math.all;
use     PoC.strings.all;

entity clock_planner_tb is end entity;

architecture tb of clock_planner_tb is
    constant CLK_FREQ     : FREQ    := 125 MHz;
    constant SAMPLE_DELTA : time    := 5 us;

    -- ① 采样脉冲需要的时钟周期数（向上取整）
    constant SAMPLE_CYCLES: natural := TimingToCycles(SAMPLE_DELTA, CLK_FREQ);

    -- ② 两个时钟的周期与公共节拍
    constant T_FAST       : time    := to_time(125 MHz);   -- 8 ns
    constant T_SLOW       : time    := to_time(50 MHz);    -- 20 ns
    -- 用周期数（整数）求 lcm：8 个 1ns 与 20 个 1ns 的 lcm
    constant LCM_CYCLES   : positive:= leastCommonMultiple(8, 20);  -- 40 ns 节拍
begin
    process
    begin
        report "采样脉冲周期数 = " & integer'image(SAMPLE_CYCLES);   -- 期望 625
        report "快时钟周期     = " & to_string(T_FAST, 3);           -- 8.000 ns
        report "慢时钟周期     = " & to_string(T_SLOW, 3);           -- 20.000 ns
        report "公共节拍       = " & integer'image(LCM_CYCLES) & " * 1ns";  -- 40 * 1ns
        report "格式化校验     = " & str_format(real(SAMPLE_CYCLES) * 8.0, 1) & " ns";  -- 5000.0 ns
        wait;
    end process;
end architecture;
```

**串联要点**：

- `physical` 提供 `FREQ` 字面量、`to_time`、`TimingToCycles` 与 `to_string`，把「频率/延迟」翻译成「周期数」并漂亮地打印。
- `math` 提供 `leastCommonMultiple`，把「两个周期求公共节拍」变成整数 lcm 问题。
- `strings` 提供 `str_format`，把实数结果格式化进报告。
- `vectors` 虽未在本例直接出现，但若你要把 625 个采样缓存成二维阵列，就会用到 `T_SLM`/`T_SLVV_*`。

**预期结果**：`625`、`8.000 ns`、`20.000 ns`、`40 * 1ns`、`5000.0 ns`。

> ⚠️ **待本地验证**：`TimingToCycles(5 us, 125 MHz)` 中 5 us = 5000 ns，÷8 ns = 625.0，`ceil` 后为 625，逻辑无误，但仍建议本地 elaboration 确认报告输出。

---

## 6. 本讲小结

- `physical` 包定义了 `FREQ`/`BAUD`/`MEMORY` 三种带单位的物理类型，让你写 `50 MHz`、`1 KiB` 这样的字面量，并在 elaboration 阶段与 `time` 互转。
- 频率↔周期↔周期数三角换算的核心是 `to_time`、`to_freq`、`TimingToCycles`/`CyclesToDelay`，后者被 `io_Debounce`、`uart_bclk` 等大量核用来把 generic 里的「人话时间」换成计数器宽度。
- `div(time,time)` 通过 `MinimalTimeResolutionInSimulation` 适配各仿真器的最小时间分辨率，是 PoC「跨工具可移植」哲学的典型体现。
- `strings` 包补齐了标准库缺失的字符串/格式化能力，`C_POC_NUL` 用 `ite` 按综合工具切换填充符以绕开 Quartus 的崩溃 bug。
- `vectors` 包用 `T_SLV_*`/`T_SLVV_*`/`T_SLM` 三套抽象表达「定宽位串 / 一组字 / 二维位矩阵」，并提供完整的互转与矩阵布尔运算，注意 `T_SLM` 必须用 `'Z'` 初始化。
- `math` 包提供三角形数、组合数、gcd、lcm 等少量整数扩展，主要用于常量推导；使用 `binomialCoefficient` 时需留意整数除法截断。

## 7. 下一步学习建议

- 本讲结束后，[u2 公共包与配置机制] 单元就只剩 [u2-l5 components 包](u2-l5-components-primitives.md) 一讲：它讲解 `components.vhdl` 如何把触发器（`ffdre`/`ffse`）等可综合原语封装成函数。建议接着学，因为它会把本讲的 `T_ROUNDING_STYLE`、`T_POLARITY` 等 `utils` 类型实际用起来。
- 想看 `physical` 包的真实消费场景，可直接读 [src/io/io_Debounce.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/io_Debounce.vhdl) 与 [src/io/uart/uart_bclk.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/io/uart/uart_bclk.vhdl)，看 `TimingToCycles` 如何出现在常量声明里。
- 想深入 `T_SLM` 矩阵的工程用法，可等到 [u3-l4 FIFO 家族](u3-l4-fifo-family.md) 与 [u5-l3 cache 子系统](u5-l3-cache-subsystem.md)，那里会用矩阵组织存储阵列。
- 进入第 3 单元前，确保你理解了 `config` 包（[u2-l3](u2-l3-config-mechanism.md)）派生出的 `VENDOR`/`DEVICE_INFO`——后续每个 IP 核的厂商可移植分支都依赖它。
