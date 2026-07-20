# 时钟同步等待、脉冲与选通生成

## 1. 本讲目标

学完本讲，你应该能够：

- 区分 psi_tb 里两类「等」：**按物理时间等**（[u4-l1](u4-l1-activity-check.md) 的 `CheckNoActivity` 已用过 `wait for`）与**按时钟沿等**（本讲主线 `rising_edge(Clk)`），并知道为什么 testbench 里几乎所有激励都要对齐到时钟沿。
- 说出 `PulseSig`、`ClockedWaitFor`、`WaitClockCycles`、`ClockedWaitTime` 四个过程各自的「等待条件」，并能预测它们让仿真时间前进多少。
- 用数学公式算出 `GenerateStrobe` 在给定 `freq_clock` / `freq_str` 下「多少个时钟周期产生一个选通脉冲」，并解释 `ceil` 带来的频率量化误差。
- 讲清 `WaitForValueStdlv` / `WaitForValueStdl` 的核心机制 `wait until <条件> for <超时>`——它既能在目标值出现时立刻返回、又能在超时后**主动打印 `###ERROR###`** 而不挂死，并把它和「无超时、可能永久挂起」的 `ClockedWaitFor` 对比。
- 写出一个最小 testbench：在 100 MHz 时钟下用 `GenerateStrobe` 产生 1 MHz 选通，再用 `WaitForValueStdlv` 等一个**永远不会到达**的值，亲眼在 Transcript 里看到超时报告。

## 2. 前置知识

本讲承接 [u4-l1 信号活动检查](u4-l1-activity-check.md) 与 [u3-l1 基础比较过程](u3-l1-compare-basic.md)，请先确认你已经了解：

- **`###ERROR###` 前缀与 CI 联动**（[u1-l3](u1-l3-simulation-and-ci.md)）：psi_tb 所有自检失败的消息都以 `###ERROR###: ` 开头，仿真结束后由 `run_check_errors "###ERROR###"` 扫描，命中即判 CI 失败。本讲的 `WaitForValueStdlv/Stdl` 在超时时正是打印这样一条消息。
- **`report ... severity error` 骨架**（[u3-l1](u3-l1-compare-basic.md)）：`severity error` **只打印、不中断**仿真。本讲的超时报告与 [u4-l1](u4-l1-activity-check.md) 的活动检查一样沿用这条骨架，所以一次仿真里可以连续触发多次超时，最后由 CI 统一判定。
- **`str` / `hstr` 字符串转换**（[u2-l1](u2-l1-txt-util-conversions.md)）：`str(slv)` 把向量转成「MSB 在左」的二进制串，`hstr(slv)` 转成十六进制（位数 = `ceil(bits/4)`）。`WaitForValueStdlv` 的诊断消息里 `[Expected <二进制>(0x<十六进制>)]` 正是用这两个函数拼出来的。
- **testbench 不可综合**（[u1-l1](u1-l1-project-overview.md)）：所以这里可以放心使用 `wait until rising_edge(Clk)`、`wait for <time>`、`while true loop` 这些只在仿真器里有意义的写法——`GenerateStrobe` 就是靠一个无限循环持续运行的。

一个需要先建立的直觉：**「检查」和「激励」是 testbench 的两条腿**。[u4-l1](u4-l1-activity-check.md) 讲的是「检查」（被动观察 DUT 行为），本讲讲的是「激励」（主动驱动信号、主动推进时间）。真实 testbench 里二者交替进行——「施加一个脉冲 → 等若干周期 → 检查输出 → 再等某个握手信号」。本讲的七个过程就是「激励与等待」这一侧的全部原语。

## 3. 本讲源码地图

本讲只涉及一个源文件，但它是 `activity_pkg` 里「激励与等待」那半边的全部内容：

| 文件 | 作用 |
| --- | --- |
| [hdl/psi_tb_activity_pkg.vhd](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd) | 信号活动检查与激励生成包。本讲取其中「激励与等待」的 7 个过程；活动检查类的 `CheckNoActivity` 等已在 [u4-l1](u4-l1-activity-check.md) 讲过。 |

和 psi_tb 一贯风格一致，本文件也是「声明 + 实现」成对组织：声明在 [hdl/psi_tb_activity_pkg.vhd:22-89](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L22-L89)，实现在 [hdl/psi_tb_activity_pkg.vhd:94-258](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L94-L258)。包头部 `use ieee.math_real.all`（[hdl/psi_tb_activity_pkg.vhd:13](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L13)）只是为了给 `GenerateStrobe` 提供 `ceil` 函数。在自己的 testbench 里引用方式如下：

```vhdl
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library work;
use work.psi_tb_txt_util.all;       -- 提供 print / str / hstr
use work.psi_tb_activity_pkg.all;   -- 提供本讲的 7 个过程（连带 compare 包）
```

本讲七个过程一览（`Prefix` 仅 `WaitForValue*` 两个有，默认均为 `###ERROR###: `）：

| 过程 | 一句话作用 | 是否含超时 | 典型场景 |
| --- | --- | --- | --- |
| `PulseSig` | 在时钟上拉高恰好 1 个周期 | 否 | 触发一次单周期启动信号 |
| `ClockedWaitFor` | 等到某信号等于指定值（对齐上升沿） | **否（可能永久挂起）** | 等握手信号 |
| `WaitClockCycles` | 等若干个上升沿 | 否 | 「等 10 拍再看」 |
| `ClockedWaitTime` | 等一段物理时间，再对齐到下一个上升沿 | 否 | 「至少等 500 ns，且回到时钟节拍」 |
| `GenerateStrobe` | 持续分频产生周期性选通脉冲 | 否（并发过程，永久运行） | 产生采样/使能节拍 |
| `WaitForValueStdlv` | 在超时内等向量等于期望值，否则报错 | **是** | 带保护的握手等待 |
| `WaitForValueStdl` | 同上，对象是单比特 | **是** | 带 `Timeout` 的 `ready` 等待 |

> 小提示：[Changelog.md:28](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/Changelog.md#L28) 把按周期等待的过程记作 `WaitForClockCycles`，但源码里实际声明的是 **`WaitClockCycles`**（[hdl/psi_tb_activity_pkg.vhd:60](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L60)）。这是 Changelog 与代码的命名出入，调用时以源码为准。

## 4. 核心概念与源码讲解

### 4.1 `PulseSig` 与 `ClockedWaitFor`：最简激励与时钟同步等待

#### 4.1.1 概念说明

这两个过程是「时钟域内激励/等待」的最小原语，代码都非常短，却最能体现 testbench 的基本节奏。

`PulseSig` 解决一个极常见的需求：**给某个信号打一个「恰好一个时钟周期宽」的高电平脉冲**。真实 FPGA 设计里大量控制信号都是单周期有效的（如 `start`、`valid`、`load`），testbench 要触发它们，就必须在某个上升沿把信号拉高、在下一个上升沿拉低。手写就是四个语句，`PulseSig` 把它封装成一个调用，并保证两个动作都对齐到 `Clk` 的上升沿。

`ClockedWaitFor` 解决的是**「等某个信号变成期望值」**的需求——而且要求在时钟上升沿上判定。典型场景是 DUT 输出一个 `Done` 握手，testbench 要等到 `Done='1'` 才继续。它和 `PulseSig` 共用同一种底层原语：`wait until rising_edge(Clk) and <条件>`。

#### 4.1.2 核心流程

```
PulseSig(Sig, Clk)：
   wait until rising_edge(Clk)   ← 对齐到第 N 个上升沿
   Sig <= '1'                    ← 拉高（下一 delta 生效）
   wait until rising_edge(Clk)   ← 对齐到第 N+1 个上升沿
   Sig <= '0'                    ← 拉低 → 整个脉冲恰好跨越 1 个周期

ClockedWaitFor(Val, Sig, Clk)：
   wait until rising_edge(Clk) and Sig = Val
   ↑ 任何一个信号变化都会唤醒本 wait，重新求值条件；
     只有「正好在上升沿 且 此时 Sig=Val」才真正通过。
```

需要建立两个关键直觉：

1. **信号赋值是「延迟」生效的**。`Sig <= '1'` 不会在执行瞬间改变 `Sig`，而是在当前仿真时刻的下一个 delta cycle 生效。所以「在上升沿处执行 `Sig <= '1'`」意味着 `Sig` 在该沿之后才变高——这恰好是我们想要的「干净的同步脉冲」。
2. **`wait until <expr>` 是电平敏感的**。VHDL 规定：表达式中任何信号发生变化都会唤醒 `wait`，醒来后重新求值整个表达式；为真才真正结束等待。所以 `wait until rising_edge(Clk) and Sig = Val` 并不是「只看时钟」，而是「`Clk` 或 `Sig` 任一变化都唤醒一次，但只有撞上上升沿且 `Sig=Val` 才放行」。

#### 4.1.3 源码精读

`PulseSig` 声明在 [hdl/psi_tb_activity_pkg.vhd:51-52](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L51-L52)，实现在 [hdl/psi_tb_activity_pkg.vhd:163-170](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L163-L170)：

```vhdl
procedure PulseSig(signal Sig : out std_logic;
                   signal Clk : in std_logic) is
begin
    wait until rising_edge(Clk);
    Sig <= '1';
    wait until rising_edge(Clk);
    Sig <= '0';
end procedure;
```

注意两点：第一，`Sig` 的模式是 **`out`**——过程会驱动它，所以调用时实参必须是一个 `out` 能驱动的信号（一般就是 testbench 里自己声明的 `signal`）。第二，它**没有参数控制脉冲宽度**，宽度恒为「一个时钟周期」；想要更宽的脉冲，需要在拉高后插入 `WaitClockCycles`（4.2 节）。

`ClockedWaitFor` 声明在 [hdl/psi_tb_activity_pkg.vhd:55-57](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L55-L57)，实现在 [hdl/psi_tb_activity_pkg.vhd:173-178](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L173-L178)：

```vhdl
procedure ClockedWaitFor(Val        : in std_logic;
                         signal Sig : in std_logic;
                         signal Clk : in std_logic) is
begin
    wait until rising_edge(Clk) and Sig = Val;
end procedure;
```

短短一行，但要特别留意它的**风险**：这个 `wait` **没有任何超时**。如果 `Sig` 永远不等于 `Val`，本 process 将**永久挂起**，仿真会一直「跑」下去但不结束，既不报错也不通过。这就是为什么 psi_tb 又另外提供了带超时的 `WaitForValueStdl`（4.4 节）——后者能在超时后主动打 `###ERROR###` 而不是沉默挂死。**经验法则**：等你**确信一定会发生**的事件用 `ClockedWaitFor`（更简洁）；等**可能不来**的事件务必用 `WaitForValue*`（更安全）。

#### 4.1.4 代码实践：用 `PulseSig` 触发，用 `ClockedWaitFor` 等回握

**实践目标**：演示最常见的 testbench 节奏——发一个单周期 `Start` 脉冲，等 DUT（这里用一个计数器模拟）回握 `Done`。

**操作步骤**（示例代码，非项目原有文件）：

```vhdl
-- 示例代码：PulseSig + ClockedWaitFor 的最小配合
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
library work;
use work.psi_tb_txt_util.all;
use work.psi_tb_activity_pkg.all;

entity pulse_wait_demo_tb is end entity;
architecture sim of pulse_wait_demo_tb is
    signal Clk   : std_logic := '0';
    signal Start : std_logic := '0';
    signal Done  : std_logic := '0';
begin
    Clk <= not Clk after 5 ns;   -- 100 MHz

    -- 假 DUT：收到 Start 后数 3 拍再拉高 Done
    process(Clk)
        variable n : integer := 0;
    begin
        if rising_edge(Clk) then
            if Start = '1' then
                n := 3;            -- 装入 3 拍延时
                Done <= '0';
            elsif n > 0 then
                n := n - 1;
                Done <= '0';
            else
                Done <= '1';       -- 计数到 0，回握
            end if;
        end if;
    end process;

    process
    begin
        PulseSig(Start, Clk);                      -- 触发一次（恰好 1 周期）
        print("Start 已发出，开始等 Done");
        ClockedWaitFor('1', Done, Clk);            -- 等到 Done='1'（对齐上升沿）
        print("收到 Done，仿真结束");
        wait;
    end process;
end architecture;
```

**需要观察的现象**：波形上 `Start` 恰好高电平 1 个时钟周期（10 ns）；`Done` 在 `Start` 之后约 3 个周期被拉高；`ClockedWaitFor` 返回后立刻打印「收到 Done」。

**预期结果**：`PulseSig` 与 `ClockedWaitFor` 一发一收，配合默契，Transcript 里**不会**出现 `###ERROR###`（这两个过程本来也不打错误消息）。**待本地验证**：把假 DUT 里的 `Done <= '1'` 改成永远 `'0'`，重新仿真——这次 `ClockedWaitFor` 会**永久挂起**，仿真不结束，直观体会「无超时等待」的风险，这正是 4.4 节要解决的问题。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `PulseSig` 里要写**两次** `wait until rising_edge(Clk)`，而不是一次 `wait` 后连续 `Sig <= '1'; Sig <= '0';`？

> **答案**：因为在同一时刻连续两条对同一信号的赋值，只有最后一条生效（后者覆盖前者），`Sig` 根本不会出现高电平。必须用两次 `wait` 把两个赋值分到**两个不同的上升沿**，信号才会真正经历「低→高→低」的跳变，形成一个完整脉冲。

**练习 2**：`ClockedWaitFor('1', Done, Clk)` 如果把时钟参数去掉，只写 `wait until Done = '1'`，行为会差在哪里？

> **答案**：去掉了 `rising_edge(Clk)` 就不再对齐时钟沿——`wait` 会在 `Done` 一变高（哪怕在时钟周期中段）就立刻返回。在同步设计里，这会让后续操作脱离时钟节拍，可能采样到亚稳态值。`ClockedWaitFor` 的价值就在于强制「在上升沿上判定」，保证后续代码看到的 `Done` 是经过时钟同步的稳定值。

---

### 4.2 `WaitClockCycles` 与 `ClockedWaitTime`：按周期 / 按时间等待

#### 4.2.1 概念说明

`PulseSig` 和 `ClockedWaitFor` 解决了「等一个事件」，但很多时候你只是想「**让时间往前走一段**」——比如「复位后再等 10 个周期让流水线冲满」「等至少 500 ns 让 PLL 锁定」。psi_tb 提供两个不同「计量单位」的等待过程：

- `WaitClockCycles(Cycles, Clk)`：以**时钟周期数**为单位。它数 `Cycles` 个上升沿，与你写 `for i in 0 to N-1 loop wait until rising_edge(Clk); end loop;` 等价。好处是与具体时钟频率无关——换时钟频率，等待的「逻辑拍数」不变。
- `ClockedWaitTime(Duration, Clk)`：以**物理时间**为单位。它先 `wait for Duration`，再补一个 `wait until rising_edge(Clk)` 把执行点对齐回时钟沿。好处是可以表达「至少 500 ns」这类与时钟周期不成整数倍的时间。

二者最关键的区别在**返回时刻**：`WaitClockCycles` 一定在某个上升沿返回；`ClockedWaitTime` 经过 `Duration` 后还要**再等**到下一个上升沿才返回，所以总等待时间略大于 `Duration`。

#### 4.2.2 核心流程

```
WaitClockCycles(Cycles, Clk)：
   for i in 0 to Cycles-1 loop
       wait until rising_edge(Clk);
   end loop;
   ── 返回点恰好在第 Cycles 个上升沿
   ── Cycles=0 时循环范围 0 to -1 为空，立即返回（不消耗时间）

ClockedWaitTime(Duration, Clk)：
   wait for Duration;             ── 走完一段物理时间
   wait until rising_edge(Clk);   ── 再对齐到下一个上升沿
   ── 返回点在「Duration 之后最近的那个上升沿」
```

把总等待时间写成公式（设时钟周期为 \(T_{\text{clk}}\)）：

\[
T_{\text{WaitClockCycles}} = \text{Cycles} \cdot T_{\text{clk}}
\]

\[
T_{\text{ClockedWaitTime}} \in (\,\text{Duration},\ \text{Duration}+T_{\text{clk}}\,]
\]

即 `ClockedWaitTime` 的总等待时间落在「`Duration`」到「`Duration` 加一个周期」的半开区间里——多出来的那一段就是把执行点拖回上升沿的「对齐时间」。

#### 4.2.3 源码精读

`WaitClockCycles` 声明在 [hdl/psi_tb_activity_pkg.vhd:60-61](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L60-L61)，实现在 [hdl/psi_tb_activity_pkg.vhd:181-187](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L181-L187)（注意实现体上方注释误写成了 `ClockedWaitFor`，但代码本身是 `WaitClockCycles`）：

```vhdl
procedure WaitClockCycles(Cycles     : in integer;
                          signal Clk : in std_logic) is
begin
    for i in 0 to Cycles-1 loop
        wait until rising_edge(Clk);
    end loop;
end procedure;
```

读这段代码有一个常被忽略的细节：**循环范围是 `0 to Cycles-1`**。当 `Cycles = 0` 时范围变成 `0 to -1`，是 VHDL 里的**空范围（null range）**，循环体一次都不执行，过程立即返回且不消耗任何仿真时间——这是合法且安全的行为。当 `Cycles` 为负时同样得到空范围（如 `Cycles = -1` → `0 to -2`），也不会出错，只是「什么都没等」。

`ClockedWaitTime` 声明在 [hdl/psi_tb_activity_pkg.vhd:64-65](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L64-L65)，实现在 [hdl/psi_tb_activity_pkg.vhd:190-195](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L190-L195)：

```vhdl
procedure ClockedWaitTime(Duration   : in time;
                          signal Clk : in std_logic) is
begin
    wait for Duration;
    wait until rising_edge(Clk);
end procedure;
```

只有两行，但要理解它存在的意义：单独 `wait for Duration` 会让返回时刻落在**任意相位**上（与时钟无关），后续如果紧接着采样 `Clk` 域的信号，可能采到周期中段的不稳定值。补上第二个 `wait` 就强制把执行点拉回最近的上升沿，保证接下来的读操作是「同步采样」。这就是它的名字里 `Clocked` 的含义——不是「按周期等」，而是「等完后对齐到时钟」。

> 小提示：这两个过程都不含超时、也不打 `###ERROR###`。它们只是「推进时间」的工具，不做事后检查——检查要靠 [u4-l1](u4-l1-activity-check.md) 的活动检查或 4.4 节的 `WaitForValue*`。

#### 4.2.4 代码实践：比较两种等待让仿真时间前进多少

**实践目标**：在同一时钟下，分别调用 `WaitClockCycles` 与 `ClockedWaitTime`，打印前后时刻，验证 4.2.2 节的两个公式。

**操作步骤**（示例代码，非项目原有文件）：

```vhdl
-- 示例代码：对比 WaitClockCycles 与 ClockedWaitTime 的时间推进
library ieee;
use ieee.std_logic_1164.all;
library work;
use work.psi_tb_txt_util.all;
use work.psi_tb_activity_pkg.all;

entity wait_compare_tb is end entity;
architecture sim of wait_compare_tb is
    signal Clk : std_logic := '0';
begin
    Clk <= not Clk after 5 ns;   -- 100 MHz，周期 10 ns

    process
    begin
        wait until rising_edge(Clk);          -- 对齐到 0 时刻之后的第 1 个沿
        print("起点 now = " & time'image(now));

        WaitClockCycles(10, Clk);             -- 期望前进 10×10ns = 100 ns
        print("WaitClockCycles(10) 后 now = " & time'image(now));

        ClockedWaitTime(25 ns, Clk);          -- 期望前进 25 ns ~ 35 ns
        print("ClockedWaitTime(25ns) 后 now = " & time'image(now));

        wait;
    end process;
end architecture;
```

**需要观察的现象**：

- `WaitClockCycles(10)` 之后，`now` 应比起点多出恰好 **100 ns**（10 个周期 × 10 ns）。
- `ClockedWaitTime(25 ns)` 之后，`now` 比上一次打印多出介于 **25 ns 到 35 ns** 之间的某个值——具体取决于调用时落在时钟的哪个相位。

**预期结果**：两组数值符合 4.2.2 节的两个公式。**待本地验证**：不同仿真器对 `now` 的精度（fs / ps / ns）显示不同，以你本机 Transcript 为准；关键是验证「`WaitClockCycles` 严格等于 `Cycles` 个周期」而「`ClockedWaitTime` 在 `Duration`~`Duration+T_clk` 之间」这两个**关系**成立。

#### 4.2.5 小练习与答案

**练习 1**：要让仿真「至少走 1 µs，然后回到时钟节拍」，100 MHz 下应该用 `WaitClockCycles(100, Clk)` 还是 `ClockedWaitTime(1 us, Clk)`？两者有差别吗？

> **答案**：100 MHz 下 100 个周期恰好 = 1 µs，所以两者总等待时间几乎相同。细微差别：`WaitClockCycles(100)` 一定在某个上升沿返回，总时间精确等于 1 µs；`ClockedWaitTime(1 us)` 先走满 1 µs 再对齐到下一个上升沿，若调用点恰在沿上则也精确 1 µs，否则可能多出最多一个周期。一般等「逻辑拍数」用前者，等「绝对时间下限」用后者。

**练习 2**：`WaitClockCycles(0, Clk)` 和 `WaitClockCycles(-3, Clk)` 各会发生什么？

> **答案**：两者都让循环范围变成空范围（分别是 `0 to -1` 与 `0 to -4`），循环体不执行，过程立即返回、不消耗仿真时间、不报错。所以 `Cycles=0` 是安全的「不等待」用法；负数虽不报错，但属于调用方失误，应在 testbench 里避免。

---

### 4.3 `GenerateStrobe`：分频产生周期性选通

#### 4.3.1 概念说明

前两节的过程都是「调用一次、等一次」，而 `GenerateStrobe` 是 psi_tb 里少有的**并发过程（concurrent procedure）**——它内部是一个 `while true loop`，一旦调用就**永久运行**，持续地在 `str` 上输出周期性的单周期脉冲。它解决的是 testbench 里另一类需求：**产生一个稳定的低频节拍信号**（采样脉冲、使能信号、模拟一个低速外设的读写节拍等）。

它的设计思路是一个标准的**计数器分频器**：用一个计数器数时钟周期，数到某个上限就输出一个脉冲并清零。关键在于「上限」是怎么算的——它由两个频率参数决定：参考时钟频率 `freq_clock` 和期望的选通频率 `freq_str`。这样调用方只需要给「我有多快的时钟」和「我要多快的脉冲」，过程自己算出分频比，不必手算周期数。

#### 4.3.2 核心流程

先看分频比是怎么定义的。设时钟频率为 \(f_{\text{clk}}\)、期望选通频率为 \(f_{\text{str}}\)，则**理论周期数**为：

\[
N_{\text{理论}} = \frac{f_{\text{clk}}}{f_{\text{str}}}
\]

但计数器长度必须是**整数**，源码用 `ceil` 向上取整：

\[
N = \left\lceil \frac{f_{\text{clk}}}{f_{\text{str}}} \right\rceil
\]

于是选通实际每 \(N\) 个时钟周期产生 1 个脉冲，**实际选通频率**为：

\[
f_{\text{str,实际}} = \frac{f_{\text{clk}}}{N} \leq f_{\text{str}}
\]

用 `ceil` 而非 `round` 或 `floor` 的效果是：**实际频率永远不高于期望**（脉冲永远来得不比期望更早），这在「采样」类场景里更安全——宁可慢一点，不能过采样。代价是当 \(f_{\text{clk}}/f_{\text{str}}\) 不是整数时会有量化误差，例如 100 MHz 时钟要 3 MHz 选通，\(N=\lceil 33.33\rceil=34\)，实际只有 \(100/34\approx 2.94\) MHz。

过程主体逻辑（复位优先）：

```
每个 rising_edge(clk)：
   if rst = rst_pol_g then          ── 复位有效
       count_v := 0
       str     <= '0'
   else
       if count_v /= N-1 then       ── 还没数到顶
           str     <= '0'
           count_v := count_v + 1
       else                         ── 数到顶：本拍输出脉冲并清零
           str     <= '1'
           count_v := 0
       end if
   end if
```

所以 `str` 每隔 \(N\) 个周期拉高恰好 1 拍，占空比为 \(1/N\)。

#### 4.3.3 源码精读

声明在 [hdl/psi_tb_activity_pkg.vhd:68-73](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L68-L73)：

```vhdl
procedure GenerateStrobe(freq_clock : in real      := 100.0E6;  -- in Hz
                         freq_str   : in real      := 1.0E6;    -- in Hz
                         rst_pol_g  : in std_logic := '1';      -- reset polarity
                         signal rst : in std_logic;
                         signal clk : in std_logic;
                         signal str : out std_logic);
```

注意四个默认值：时钟默认 **100 MHz**、选通默认 **1 MHz**、复位默认**高有效**（`rst_pol_g='1'` 表示 `rst` 为高时复位）。这意味着最简调用 `GenerateStrobe(rst=>Rst, clk=>Clk, str=>Str)` 就直接得到「100 MHz 时钟上的 1 MHz 选通」——正是本讲实践任务要的场景。

实现在 [hdl/psi_tb_activity_pkg.vhd:197-222](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L197-L222)：

```vhdl
procedure GenerateStrobe(...) is
    variable count_v : integer range 0 to (integer(ceil(freq_clock/freq_str))) := 0;
begin
    while true loop
        wait until rising_edge(clk);
        if rst = rst_pol_g then
            count_v := 0;
            str     <= '0';
        else
            if count_v /= integer(ceil(freq_clock/freq_str)) - 1 then
                str     <= '0';
                count_v := count_v + 1;
            else
                str     <= '1';
                count_v := 0;
            end if;
        end if;
    end loop;
end procedure;
```

读这段代码有三个要点：

1. **`count_v` 的范围用 `ceil(freq_clock/freq_str)` 表达**。这是 VHDL 的一个聪明写法——子类型范围本身可以是一个由参数决定的表达式。`integer(ceil(...))` 把实数 `ceil` 结果转回整数，得到本节记作 \(N\) 的「周期数上限」。本讲实践任务里 \(N = \lceil 100\text{E6}/1\text{E6}\rceil = 100\)。
2. **复位在每个上升沿都被采样**。注意 `if rst = rst_pol_g then` 写在 `while true loop` **内部**、紧跟 `wait until rising_edge(clk)`，所以复位是**持续检测**的——任何时候复位有效，下一个沿就把计数清零、`str` 拉低。这一点并非理所当然：[Changelog.md:41](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/Changelog.md#L41) 记载，2.2.0 版曾有个 bug——复位只在过程**调用时采样一次**，导致 `GenerateStrobe` 几乎总是卡在复位里出不来，后来才修成现在「每拍都查复位」的样子。
3. **它是并发过程**。因为有 `while true loop`，调用它会启动一个永不结束的 process。所以它必须像 `GenerateStrobe(rst=>Rst, clk=>Clk, str=>Str)` 这样作为**并发语句**直接写在 architecture 体里，而不能放在某个顺序 process 内部（否则那个 process 会卡死在里面再也回不来）。

#### 4.3.4 代码实践：在 100 MHz 下产生 1 MHz 选通并测量周期

**实践目标**：调用 `GenerateStrobe`（用全部默认值即可）产生 1 MHz 选通，在波形/Transcript 上验证两个脉冲之间恰好相隔 100 个时钟周期（1 µs）。

**操作步骤**（示例代码，非项目原有文件）：

```vhdl
-- 示例代码：GenerateStrobe 产生 1 MHz 选通
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
library work;
use work.psi_tb_txt_util.all;
use work.psi_tb_activity_pkg.all;

entity strobe_demo_tb is end entity;
architecture sim of strobe_demo_tb is
    signal Clk : std_logic := '0';
    signal Rst : std_logic := '1';
    signal Str : std_logic;
begin
    Clk <= not Clk after 5 ns;   -- 100 MHz

    -- 并发调用：用全部默认参数即得 100 MHz 时钟上的 1 MHz 选通
    GenerateStrobe(freq_clock => 100.0E6,
                   freq_str   => 1.0E6,
                   rst_pol_g  => '1',
                   rst        => Rst,
                   clk        => Clk,
                   str        => Str);

    process
    begin
        Rst <= '1';
        wait for 100 ns;
        Rst <= '0';
        wait for 5 us;           -- 让选通跑一会儿
        print("Str 期间应出现约 5 个 1 µs 周期的脉冲");
        wait;
    end process;
end architecture;
```

**需要观察的现象**：波形上 `Str` 在复位释放后开始，每 **1 µs** 出现一个高电平脉冲，每个脉冲宽 **1 个时钟周期**（10 ns），占空比约 1%。在 5 µs 窗口里大约能看到 5 个脉冲。

**预期结果**：脉冲间距 = \(N \cdot T_{\text{clk}} = 100 \times 10\,\text{ns} = 1\,\mu\text{s}\)，正好对应 1 MHz。**待本地验证**：把 `freq_str` 改成 `3.0E6`，重新测量脉冲间距——应观察到约 \(34 \times 10\,\text{ns} = 340\,\text{ns}\)（对应实际约 2.94 MHz，而非 3 MHz），直观感受 4.3.2 节说的 `ceil` 量化误差。

#### 4.3.5 小练习与答案

**练习 1**：用 50 MHz 时钟产生 1 MHz 选通，`count_v` 的上限和脉冲间距各是多少？

> **答案**：\(N = \lceil 50\text{E6}/1\text{E6}\rceil = 50\)，`count_v` 范围 `0 to 50`，判定阈值为 `N-1 = 49`。脉冲间距 = \(50 \times T_{\text{clk}}\)。50 MHz 时 \(T_{\text{clk}}=20\) ns，故间距 = 1000 ns = 1 µs，正好是 1 MHz。

**练习 2**：为什么 `GenerateStrobe` 必须写成并发过程（带 `while true loop`），而不能像 `PulseSig` 那样做成「调用一次产生一个脉冲」的顺序过程？

> **答案**：因为它要的是「**持续不断**的周期性脉冲」，是一个信号源而不是一次性行为。`while true loop` 让它像一个独立的 process 永久运行，与 testbench 主流程并行地输出节拍；若做成顺序过程，调用方就得自己写循环反复调用，既啰嗦又容易和别的并发 process 抢同步。把它封成并发过程，「一个调用 = 一个常驻信号源」，是更贴合用途的抽象。

---

### 4.4 `WaitForValueStdlv` / `WaitForValueStdl`：带超时地等待目标值并报告未达成

#### 4.4.1 概念说明

4.1 节的 `ClockedWaitFor` 有个明显风险：等不到就**永久挂起**。真实工程里这往往是 testbench 失败的最常见原因——DUT 出 bug 不回握，整个仿真就「卡住不结束」，CI 既看不到 `###ERROR###` 也看不到 `SIMULATIONS COMPLETED SUCCESSFULLY`，只能靠超时杀进程，排错非常痛苦。

`WaitForValueStdlv` / `WaitForValueStdl` 就是针对这个痛点的「带保险的等待」：它给等待加一个 **`Timeout`**，如果在超时内目标值出现就正常返回；如果超时到了目标值还没出现，它**不挂死**，而是主动打印一条带 `###ERROR###` 前缀的诊断消息后继续往下跑。这样一次仿真能跑完所有用例、收集到全部失败点，CI 也能靠 `###ERROR###` 给出明确的失败判定。

两个过程是同一逻辑的对象差异版：`Stdlv` 版等 `std_logic_vector`，`Stdl` 版等单个 `std_logic`。

#### 4.4.2 核心流程

核心是 VHDL 的 `wait ... for <超时>` 语法——**条件等待 + 超时上限**二合一：

```
wait until (ExpVal = Sig) for Timeout;
        │
        ├─ Timeout 内 ExpVal=Sig 出现 → 提前返回（正常）
        └─ Timeout 到了仍未出现 → 也返回（超时）
        │
        ▼
if ExpVal /= Sig then              ── 返回后再判一次：到底等到了没有？
    report Prefix & Msg &
           " Target state not reached" &
           " [Expected ..., Received ...]"
    severity error;                ── 超时未达成 → 打 ###ERROR###，但不中断
end if;
```

`wait until <条件> for <超时>` 的精确语义是：挂在等待队列上，直到**条件为真**或**超时到达**，**取先发生者**。无论哪种情况唤醒，都继续执行下一条语句。所以它绝不会永久挂起——最坏也只等 `Timeout` 这么久。唤醒后再用 `if ExpVal /= Sig` 判断到底是「条件满足」还是「超时」，后者才报错。

注意一个设计取舍：与 `ClockedWaitFor` 不同，`WaitForValue*` 的等待条件里**没有 `rising_edge(Clk)`**——它只看 `Sig` 的电平变化，是**电平敏感**而非时钟同步的。这意味着它可能在时钟周期中段返回。如果你需要「在时钟沿上确认达到目标值」，常见的做法是 `WaitForValue*` 之后再补一句 `wait until rising_edge(Clk)`。

#### 4.4.3 源码精读

`WaitForValueStdlv` 声明在 [hdl/psi_tb_activity_pkg.vhd:76-80](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L76-L80)，实现在 [hdl/psi_tb_activity_pkg.vhd:225-239](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L225-L239)：

```vhdl
procedure WaitForValueStdlv(signal Sig   : in std_logic_vector;
                            ExpVal       : in std_logic_vector;
                            Timeout      : in time;
                            Msg          : in string;
                            Prefix       : in string := "###ERROR###: ") is
begin
    wait until ExpVal = Sig for timeout;
    if ExpVal /= Sig then
        report  Prefix & Msg &
                " Target state not reached" &
                " [Expected " & str(ExpVal) & "(0x" & hstr(ExpVal) & ")" &
                ", Received " & str(Sig) & "(0x" & hstr(Sig) & ")" & "]"
                severity error;
    end if;
end procedure;
```

读这段代码有三个要点：

1. **用 `report ... severity error` 而非 `assert`**。因为判定已经由前面的 `if` 完成，这里只需要在「确认未达成」时无条件打消息，所以直接 `report`（等价于 `assert false report ... severity error`）。前缀 `Prefix` 默认 `###ERROR###: `，与 [u1-l3](u1-l3-simulation-and-ci.md) 的 CI 约定一致。
2. **诊断消息用 `str` + `hstr` 双格式**。`str(ExpVal)` 给二进制位串（[u2-l1](u2-l1-txt-util-conversions.md)），`hstr(ExpVal)` 给十六进制，两者都来自 `psi_tb_txt_util`。所以一条超时消息同时给出两种表示，读起来很直观，例如 `... [Expected 00000011(0x03), Received 00000101(0x05)]`。
3. **`Msg` 没有默认值**。注意 `Msg : in string`（无 `:= ""`），调用时**必须**提供，而 `Prefix` 有默认值。这是有意为之——超时几乎总是意味着出错，强制写一句 `Msg` 能让 Transcript 里的失败原因更明确。

`WaitForValueStdl` 声明在 [hdl/psi_tb_activity_pkg.vhd:83-87](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L83-L87)，实现在 [hdl/psi_tb_activity_pkg.vhd:242-256](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L242-L256)，结构完全对称，差别只在消息里没有十六进制（单比特无需 hex），用 `str(ExpVal)`（`std_logic` 重载，返回单个字符）：

```vhdl
procedure WaitForValueStdl(signal Sig   : in std_logic;
                           ExpVal       : in std_logic;
                           Timeout      : in time;
                           Msg          : in string;
                           Prefix       : in string := "###ERROR###: ") is
begin
    wait until ExpVal = Sig for timeout;
    if ExpVal /= Sig then
        report Prefix & msg &
               " Target state not reached" &
               " [Expected " & str(ExpVal) &
               ", Received " & str(Sig) & "]"
               severity error;
    end if;
end procedure;
```

**`ClockedWaitFor` vs `WaitForValueStdl` 对照**：

| 维度 | `ClockedWaitFor` | `WaitForValueStdl` |
| --- | --- | --- |
| 超时 | **无**（可能永久挂起） | **有**（`Timeout`） |
| 失败时行为 | 沉默挂死 | 打印 `###ERROR###` 后继续 |
| 时钟同步 | 是（含 `rising_edge(Clk)`） | 否（电平敏感，需自行补对齐） |
| 适用场景 | 等你**确信必发生**的事件 | 等**可能不来**的事件（带保护） |

#### 4.4.4 代码实践：触发一次超时，观察诊断消息

**实践目标**：用 `WaitForValueStdlv` 等一个**永远不会到达**的值，验证它在超时后打印形如 `###ERROR###: ... Target state not reached [Expected ...(0x...), Received ...(0x...)]` 的消息，并且仿真不挂死、继续往下跑。

**操作步骤**（示例代码，非项目原有文件）：

```vhdl
-- 示例代码：WaitForValueStdlv 的超时场景
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
library work;
use work.psi_tb_txt_util.all;
use work.psi_tb_activity_pkg.all;

entity waitvalue_timeout_tb is end entity;
architecture sim of waitvalue_timeout_tb is
    signal Flag : std_logic := '0';
    signal Bus_s : std_logic_vector(7 downto 0) := x"00";
begin
    process
    begin
        -- 场景：等 Flag 变 '1'，但 Flag 永远是 '0'，2 us 后必超时
        WaitForValueStdl(Flag, '1', 2 us, "等 Flag 拉高");
        print("【超时后】仿真没有挂死，继续执行到这里");

        -- 场景：等 Bus_s 等于 0xFF，但它一直是 0x00，5 us 后超时
        WaitForValueStdlv(Bus_s, x"FF", 5 us, "等 Bus 达到 0xFF");
        print("【第二次超时后】仍然继续执行");
        wait;
    end process;
end architecture;
```

**需要观察的现象**：两条 `WaitForValue*` 都在各自超时后返回，Transcript 里依次出现两条 `###ERROR###` 消息，随后两条 `print` 也正常打印——证明仿真既没挂死、又把两次失败都记录了下来。第二条消息形如：

```
###ERROR###: 等 Bus 达到 0xFF Target state not reached [Expected 11111111(0xFF), Received 00000000(0x00)]
```

**预期结果**：超时消息的前缀、`Target state not reached`、`[Expected ...(0x...), Received ...(0x...)]` 三段与 [hdl/psi_tb_activity_pkg.vhd:233-237](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L233-L237) 完全对应。**待本地验证**：把这个 TB 接到 PsiSim 的 `run.tcl`/`runGhdl.tcl` 跑一次，确认 Transcript 因出现 `###ERROR###` 被判为 CI 失败；再把两个期望值改成实际能到达的值（如 `Flag='0'`、`Bus_s=x"00"`），重跑应转为成功（无 `###ERROR###`，出现 `SIMULATIONS COMPLETED SUCCESSFULLY`）。

#### 4.4.5 小练习与答案

**练习 1**：把 `WaitForValueStdl(Sig, '1', 100 ns, "msg")` 改写成等价的 `wait` + `assert`/`report` 手写代码，应该怎么写？

> **答案**：
> ```vhdl
> wait until Sig = '1' for 100 ns;
> if Sig /= '1' then
>     report "###ERROR###: msg Target state not reached [Expected 1, Received " & str(Sig) & "]"
>     severity error;
> end if;
> ```
> 这正是过程体 [hdl/psi_tb_activity_pkg.vhd:248-255](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L248-L255) 的展开——可见它只是把这一通用模式封装成了带统一前缀和格式化消息的可复用过程。

**练习 2**：为什么 `WaitForValueStdlv` 的等待条件是 `ExpVal = Sig` 而不是 `rising_edge(Clk) and ExpVal = Sig`？这会带来什么影响？

> **答案**：因为它的定位是「带超时的电平等待」，重点是**尽快发现目标值出现并加超时保护**，而不是「在时钟沿上判定」。代价是它可能在时钟周期中段返回，后续若要同步采样需自己补 `wait until rising_edge(Clk)`。如果场景要求严格时钟同步且能接受「等不到就挂」，则用 `ClockedWaitFor`；若要求「带保护、防挂死」，就用 `WaitForValue*` 再手动对齐——两者各管一端。

---

## 5. 综合实践

把本讲的「选通生成」与「带超时等待」串起来，完成下面这个贯穿性任务（对应本讲规格里的实践要求）。

**任务**：在 **100 MHz** 时钟下用 `GenerateStrobe` 产生 **1 MHz** 选通，并用它驱动一个计数器；然后用 `WaitForValueStdlv` 等计数器达到某个值——一个**能等到**的值（验证正常路径），再等一个**等不到**的值（验证超时报告）。

**建议实现要点**（示例代码框架，请自行上机验证）：

```vhdl
-- 示例代码：GenerateStrobe + WaitForValueStdlv 综合
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
library work;
use work.psi_tb_txt_util.all;
use work.psi_tb_activity_pkg.all;

entity strobe_wait_tb is end entity;
architecture sim of strobe_wait_tb is
    signal Clk   : std_logic := '0';
    signal Rst   : std_logic := '1';
    signal Str   : std_logic;
    signal Cnt_s : std_logic_vector(7 downto 0) := (others => '0');
begin
    Clk <= not Clk after 5 ns;   -- 100 MHz

    -- 1) 产生 1 MHz 选通（每 100 个时钟周期 1 个脉冲）
    GenerateStrobe(freq_clock => 100.0E6,
                   freq_str   => 1.0E6,
                   rst_pol_g  => '1',
                   rst        => Rst,
                   clk        => Clk,
                   str        => Str);

    -- 2) 假 DUT：每收到一个选通，计数器 +1
    process(Clk)
    begin
        if rising_edge(Clk) then
            if Rst = '1' then
                Cnt_s <= (others => '0');
            elsif Str = '1' then
                Cnt_s <= std_logic_vector(unsigned(Cnt_s) + 1);
            end if;
        end if;
    end process;

    -- 3) 测试流程
    process
    begin
        Rst <= '1';
        wait for 100 ns;
        Rst <= '0';

        -- 场景 A：Cnt 达到 3。1 MHz 节拍下约 3 µs 达到，10 µs 内必成功
        WaitForValueStdlv(Cnt_s, std_logic_vector(to_unsigned(3, 8)),
                          10 us, "Cnt 达到 3");
        print("场景 A 通过：Cnt 已到 3");

        -- 场景 B：Cnt 达到 200。需要 200 µs，远超 5 us 超时 → 必超时
        WaitForValueStdlv(Cnt_s, std_logic_vector(to_unsigned(200, 8)),
                          5 us, "Cnt 达到 200（不可能）");
        print("场景 B 超时后继续，仿真未挂死");

        print("=== 综合实践结束 ===");
        wait;
    end process;
end architecture;
```

**验收标准**：

1. **场景 A 通过**：约 3 µs 后 `Cnt_s` 达到 3，`WaitForValueStdlv` 正常返回，**不**打印 `###ERROR###`，随后的 `print("场景 A 通过...")` 正常出现。
2. **场景 B 超时**：等满 5 µs 后 `Cnt_s` 仍远小于 200，Transcript 打印一条形如 `###ERROR###: Cnt 达到 200（不可能） Target state not reached [Expected 11001000(0xC8), Received ...(0x...)]` 的消息（`200` 的 8 位二进制是 `11001000`、十六进制是 `0xC8`），随后 `print("场景 B 超时后继续...")` 仍能执行——证明仿真没有挂死。
3. **波形核对**：`Str` 每 1 µs 出现一个 10 ns 宽的脉冲；`Cnt_s` 每收到一个脉冲加 1，符合「1 MHz 节拍」。
4. **CI 联动**（可选）：把该 TB 注册到 PsiSim（参考 [u1-l3](u1-l3-simulation-and-ci.md) 的 `create_tb_run`/`add_tb_run`），用 `run.tcl` 跑一次，确认因场景 B 的 `###ERROR###` 而判 CI 失败；再把场景 B 的期望值改成能到达的值（例如 `4`），重跑应转为成功。

**待本地验证**：上述计数节拍、超时时刻与 `Received` 字段的具体数值需在你本机的 ModelSim 或 GHDL 上实跑确认；不同仿真器对 `now` 显示精度、对 `wait ... for <timeout>` 与信号更新的交错细节可能略有差异，以实际 Transcript 与波形为准。

## 6. 本讲小结

- 本讲七个过程都是「**激励与等待**」原语，与 [u4-l1](u4-l1-activity-check.md) 的「检查」原语互补，共同构成 testbench 的两条腿。
- **`PulseSig`** 把信号拉高恰好 1 个时钟周期；**`ClockedWaitFor`** 等到某信号等于期望值（对齐上升沿），但**无超时、可能永久挂起**——只用于「确信必发生」的事件。
- **`WaitClockCycles`** 数 `Cycles` 个上升沿（`Cycles=0` 或负数时空循环立即返回）；**`ClockedWaitTime`** 先 `wait for Duration` 再对齐到下一个上升沿，总等待时间落在 \((\text{Duration},\ \text{Duration}+T_{\text{clk}}]\)。
- **`GenerateStrobe`** 是少有的**并发过程**，按 \(N=\lceil f_{\text{clk}}/f_{\text{str}}\rceil\) 分频产生周期性单周期选通，实际频率 \(f_{\text{clk}}/N\le f_{\text{str}}\)；复位每拍持续采样（[Changelog.md:41](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/Changelog.md#L41) 记录的早期 bug 已修）。
- **`WaitForValueStdlv` / `WaitForValueStdl`** 用 `wait until <条件> for <超时>` 实现「带保险的等待」：超时未达成则打印 `###ERROR###` 诊断消息（`Stdlv` 版用 `str`+`hstr` 给二进制+十六进制双格式）后继续，绝不挂死——是等「可能不来」的事件时的首选。
- **关键取舍**：要「时钟同步、能接受挂死」用 `ClockedWaitFor`；要「防挂死、自动报错」用 `WaitForValue*`（电平敏感，必要时自行补对齐）。

## 7. 下一步学习建议

- 本单元（u4）到此结束，你已经掌握了 psi_tb 的全部「检查 + 激励」基础。接下来进入 [u5 AXI 总线功能模型](u5-l1-axi-types-and-init.md)：那里会**大量复用**本讲的 `WaitForValue*`、`WaitClockCycles`、`ClockedWaitFor` 来实现 AXI 单次/突发事务的握手与节流（`VldLowCycles`/`RdyLowCycles`），可以说本讲是读 AXI BFM 源码的必备前置。
- 如果你想先看一个把这些等待原语「焊」进真实协议的例子，可以跳到 [u7 I2C 总线功能模型](u7-l1-i2c-overview-and-setup.md)：I2C 的位时序、START/STOP、时钟拉伸几乎全靠本讲的 `wait for`/`ClockedWaitTime` 思路实现，是「激励与等待」的完整实战。
- 想理解超时消息里 `str`/`hstr` 的更多细节（为什么 `hstr` 对超 32 位向量也安全），回顾 [u2-l1 字符串与数值转换函数](u2-l1-txt-util-conversions.md)；想理解 `###ERROR###` 如何被 CI 扫描，回顾 [u1-l3 仿真环境与 CI 构建流程](u1-l3-simulation-and-ci.md)。
