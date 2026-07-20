# 信号活动检查：CheckNoActivity / CheckLastActivity

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 VHDL 信号属性 `Sig'last_event` 的含义，并理解为什么它是「判断一段窗口内信号是否翻转」最自然的底层原语。
- 区分 `CheckNoActivity`（**先等 `IdleTime` 再查**）与 `CheckLastActivity`（**不等待，对当前时刻做快照**）这两种风格，知道各自适合什么场景。
- 掌握 `Level` 参数的两套语义：`std_logic` 版里是 `-1/0/1`（不查 / 期望低 / 期望高），`std_logic_vector` 版里是 `-1` 或一个「无符号整数期望值」。
- 看清这四个过程内部如何把「电平校验」复用委托给 [u3-l1](u3-l1-compare-basic.md) 讲过的 `StdlCompare` / `StdlvCompareInt`，从而自动获得 `###ERROR###` 前缀与 CI 失败联动。
- 写出一段最小 testbench：在 DUT 应当保持稳定的窗口里调用 `CheckNoActivityStlv`，并能预测当窗口内出现意外翻转时 Transcript 里会打印出哪一条 `###ERROR###` 消息。

## 2. 前置知识

本讲承接 [u3-l1 基础比较过程：整数、实数、std_logic](u3-l1-compare-basic.md)，请先确认你已经了解：

- **`assert ... report ... severity error` 的共通骨架**：psi_tb 的比较过程都用这个骨架，且默认消息前缀是 `###ERROR###: `。关键结论再强调一次：`severity error` **只打印、不中断**仿真，失败判定靠 CI 事后扫描 `###ERROR###` 子串（见 [u1-l3](u1-l3-simulation-and-ci.md)）。本讲的四个活动检查过程完全沿用这套骨架。
- **`StdlCompare` 与 `StdlvCompareInt`**：本讲的 `Level` 校验会直接调用它们。回忆两点：`StdlCompare(Level, Sig, ...)` 把整数 `0/1` 当成期望电平去比对单个 `std_logic`；`StdlvCompareInt(Level, Sig, Msg, IsSigned, Tolerance, ...)` 把向量解释成整数（由 `IsSigned` 决定有/无符号）再去比对，**消息里的十六进制固定按 32 位显示**。
- **`time'image(...)`**：把 `time` 类型值转成可读字符串（如 `time'image(100 ns)` 得到 `"100 ns"`）。本讲 `CheckLastActivity` 的诊断消息靠它把「实际空闲了多久」打印出来。
- **testbench 不可综合**（[u1-l1](u1-l1-project-overview.md)）：所以这里可以放心使用 `'last_event` 这种信号属性、`wait for <time>` 这种按物理时间挂起的语句——它们只在仿真器里有意义。

一个需要先建立的直觉：**「检查活动」和「检查电平」是两件不同的事**。

- 「检查活动」问的是：*这段时间里信号有没有变过？* —— 只关心有没有翻转，不关心它停在哪个值上。
- 「检查电平」问的是：*信号现在的值是不是等于某个期望值？* —— 只看当前快照，不看历史。

真实工程里这两件事经常要**一起**做：你往往既希望某根握手信号「在 100 ns 内不许抖动」，又希望它「在这 100 ns 里稳定在高电平」。psi_tb 把这两件事**解耦**成两段代码——活动检查用 `'last_event`，电平检查复用 compare 包——再用一个 `Level` 参数把它们串起来。理解这个解耦，就理解了本讲全部四个过程的设计。

## 3. 本讲源码地图

本讲涉及两个源文件，它们在编译链里前后相邻：

| 文件 | 作用 |
| --- | --- |
| [hdl/psi_tb_activity_pkg.vhd](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd) | 信号活动检查与激励生成包。本讲只取其中的 4 个过程：`CheckNoActivity`、`CheckNoActivityStlv`、`CheckLastActivity`、`CheckLastActivityStlv`（激励生成类的 `PulseSig`/`GenerateStrobe`/`WaitForValue*` 留给 [u4-l2](u4-l2-stimulus-and-wait.md)）。 |
| [hdl/psi_tb_compare_pkg.vhd](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd) | 比较与检查助手包。本讲只关心被复用的两个过程：`StdlCompare`（整数重载）和 `StdlvCompareInt`。 |

和 psi_tb 一贯风格一致，`activity_pkg` 也是「声明 + 实现」成对组织：声明在 [hdl/psi_tb_activity_pkg.vhd:22-89](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L22-L89)，实现在 [hdl/psi_tb_activity_pkg.vhd:94-258](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L94-L258)。

它在编译链里的位置见 [sim/config.tcl:28-33](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/config.tcl#L28-L33)：`psi_tb_activity_pkg.vhd` 排在 `psi_tb_txt_util.vhd`、`psi_tb_compare_pkg.vhd` **之后**编译（第 31 行），因为它头部同时 `use work.psi_tb_txt_util.all` 与 `use work.psi_tb_compare_pkg.all`（[hdl/psi_tb_activity_pkg.vhd:15-17](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L15-L17)）——这就是「活动检查复用比较检查」在依赖关系上的体现。在自己的 testbench 里引用方式如下：

```vhdl
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library work;
use work.psi_tb_txt_util.all;      -- 提供 print / time'image 的底层支持
use work.psi_tb_activity_pkg.all;  -- 提供 CheckNoActivity 等（会自动连带 compare 包）
```

本讲四个过程一览（`Prefix` 全部默认 `###ERROR###: `）：

| 过程 | 作用对象 | 是否内部等待 | 典型场景 |
| --- | --- | --- | --- |
| `CheckNoActivity` | `std_logic` | **是**（`wait for IdleTime`） | 「接下来 `IdleTime` 内不许动」 |
| `CheckNoActivityStlv` | `std_logic_vector` | **是** | 总线在一段窗口内必须保持稳定 |
| `CheckLastActivity` | `std_logic` | **否**（即时快照） | 时间已被别处推进，只想断言「刚才一直没动」 |
| `CheckLastActivityStlv` | `std_logic_vector` | **否** | 例如 I2C 里校验「SCL 高电平期间 SDA 不许翻转」 |

## 4. 核心概念与源码讲解

### 4.1 `Sig'last_event` 信号属性：活动检查的底层机制

#### 4.1.1 概念说明

VHDL 给每个信号都附带了一组「信号属性（signal attribute）」，`'last_event` 是其中最常用的一个。它的返回值是一个 `time`，语义是：

> 从**当前仿真时刻**往回看，距离这个信号**上一次发生事件（event）**已经过去了多久。

所谓「事件」，指的是信号值真正发生了变化（例如 `'0'` 变 `'1'`）。几个关键推论：

- 如果信号**刚刚**翻转过，`Sig'last_event` 接近 `0 ns`。
- 如果信号**一直**没翻转（从仿真开始就是这个值），按 VHDL 规定 `'last_event` 返回 `time'high`（一个极大的值，可视为「无穷久」）。
- 对于数组信号（如 `std_logic_vector`），`'last_event` 反映的是「**任意一个比特**上一次发生事件」距今的时间——也就是「整条总线有没有任何一位动过」。这正是 `CheckNoActivityStlv` 直接拿它当判据的原因。

有了这个属性，「信号在过去 `T` 时间内是否一直空闲」就变成了一个简单的比较：只要在当前时刻判断

\[
\text{Sig'last\_event} \;\geq\; T
\]

成立，就说明在这段 `T` 窗口里信号没有发生过任何事件——它一直空闲。psi_tb 的全部活动检查逻辑都建立在这个不等式上。

#### 4.1.2 核心流程

```
当前时刻 now
  │
  ├── 回看：Sig 上一次发生事件是在 (now - Sig'last_event)
  │
  └── 判定：Sig'last_event >= IdleTime ？
            是 → 这段 IdleTime 窗口内信号「无活动」，检查通过
            否 → 信号在窗口内翻转过，触发 ###ERROR### 消息
```

需要注意一个常被忽略的细节：`'last_event` 是一个**只读快照**，它本身不会让仿真时间前进。要「让仿真真正走过 `IdleTime` 这段时间」再去看快照，必须显式 `wait for IdleTime`。这恰好对应了 `CheckNoActivity` 与 `CheckLastActivity` 的根本区别——前者自己负责 `wait`，后者假设调用方已经把时间推到位了。

#### 4.1.3 源码精读

四个过程对 `'last_event` 的使用方式完全一致，都是作为 `assert` 的通过条件。以 `CheckNoActivity` 为例：

[hdl/psi_tb_activity_pkg.vhd:102-106](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L102-L106) —— 先 `wait` 走过窗口，再用 `'last_event` 判定：

```vhdl
wait for IdleTime;
assert Sig'last_event >= IdleTime
report Prefix & Msg & "[Unexpected Activity]"
severity error;
```

这段代码做的事：让本 process 挂起 `IdleTime`；醒来后，若 `Sig'last_event < IdleTime`（说明窗口内有过翻转）则 `assert` 失败，打印 `###ERROR###: <Msg>[Unexpected Activity]`。

同样一行 `assert Sig'last_event >= IdleTime` 在另外三个过程里逐字出现：

- `CheckNoActivityStlv`：[hdl/psi_tb_activity_pkg.vhd:119-122](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L119-L122)（对象是 `std_logic_vector`，依赖 `'last_event` 对数组「任一比特动过即算事件」的语义）。
- `CheckLastActivity`：[hdl/psi_tb_activity_pkg.vhd:135-139](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L135-L139)（**没有** `wait`，纯快照，消息里还附带实际空闲时长）。
- `CheckLastActivityStlv`：[hdl/psi_tb_activity_pkg.vhd:152-156](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L152-L156)。

#### 4.1.4 代码实践：直接观察 `last_event` 的值

**实践目标**：用一个最小 testbench 把 `'last_event` 的数值打印出来，直观感受「翻转 → 接近 0」「长期不动 → 极大值」。

**操作步骤**（示例代码，非项目原有文件）：

```vhdl
-- 示例代码：观察 std_logic 的 'last_event
library ieee;
use ieee.std_logic_1164.all;
library work;
use work.psi_tb_txt_util.all;

entity last_event_demo_tb is end entity;
architecture sim of last_event_demo_tb is
    signal Sig : std_logic := '0';
begin
    process
    begin
        print("t=0  last_event = " & time'image(Sig'last_event));  -- 仿真开始即查：通常为 time'high
        wait for 50 ns;
        Sig <= '1';                -- 50 ns 处翻转
        wait for 10 ns;
        print("翻转后 10 ns last_event = " & time'image(Sig'last_event));  -- 期望 10 ns
        wait for 100 ns;
        print("再等 100 ns last_event = " & time'image(Sig'last_event));   -- 期望 110 ns
        wait;
    end process;
end architecture;
```

**需要观察的现象**：第二行打印应显示 `10 ns`（翻转发生在 50 ns，当前 60 ns，差 10 ns）；第三行应显示 `110 ns`。

**预期结果**：`'last_event` 随信号静止时间的增长而线性增长。**待本地验证**：不同仿真器（ModelSim / GHDL）对「仿真开始就查询」时 `'last_event` 的初始值可能呈现不同形式（`time'high` 或一个极大整数），以你本机实际 Transcript 为准。

#### 4.1.5 小练习与答案

**练习 1**：如果一个信号自仿真开始从未翻转，调用 `assert Sig'last_event >= 1 ns` 会通过吗？

> **答案**：会通过。因为此时 `'last_event` 返回 `time'high`（视为无穷久），显然 `>= 1 ns`。

**练习 2**：为什么 `CheckNoActivityStlv` 可以直接对一个 `std_logic_vector` 用 `'last_event`，而不用自己写一个「逐位比较」的循环？

> **答案**：因为对数组信号，`'last_event` 反映的是「任一比特上一次事件」距今的时间——也就是「总线有没有任何一位动过」。这恰好就是「总线无活动」的定义，所以一行 `assert` 即可，无需展开成逐位检查。

---

### 4.2 CheckNoActivity / CheckNoActivityStlv：先「等」再「查」

#### 4.2.1 概念说明

`CheckNoActivity` 的语义可以一句话概括：**「接下来的 `IdleTime` 时间内，信号不许动；时间一到我就检查。」** 它把「推进时间」和「事后核查」打包成一个调用，调用方一行代码就完成了一次「静默窗口校验」。

它解决的问题是 testbench 里极常见的一类断言——「复位释放后，ready 信号应当保持 500 ns 低电平」「一次 DMA 传输结束后，中断线应当至少 1 µs 不再翻转」。这类需求如果手写，要自己 `wait`、自己读 `'last_event`、自己拼错误消息；`CheckNoActivity` 把这些全封装好了，且失败消息带 `###ERROR###` 前缀，直接联动 CI。

`CheckNoActivityStlv` 是它的总线版：把对象从单比特换成 `std_logic_vector`，用来校验「整条数据/控制总线在窗口内必须保持稳定」。

#### 4.2.2 核心流程

```
调用 CheckNoActivity(Sig, IdleTime, Level, Msg, Prefix)
        │
        ▼
   wait for IdleTime          ← 本 process 真的挂起 IdleTime
        │
        ▼
   assert Sig'last_event >= IdleTime
        ├─ 成立 → 无活动，通过（不打消息）
        └─ 不成立 → 打印 Prefix & Msg & "[Unexpected Activity]"，severity error
        │
        ▼
   if Level /= -1 then        ← 可选的第二段：电平校验
        StdlCompare(Level, Sig, ...)   （std_logic 版）
        或 StdlvCompareInt(Level, Sig, ..., IsSigned=false, Tolerance=0, ...)  （stlv 版）
   end if
```

注意两个要点：第一，活动检查的 `assert` 与电平检查的 `StdlCompare` 是**两段独立**的检查，活动失败不会跳过电平检查（因为 `severity error` 不中断），所以一次窗口里「既翻转又电平错」会同时报两条消息。第二，`Level = -1` 是一个约定性的「跳过」值，下面专门讲。

#### 4.2.3 源码精读

先看声明。`CheckNoActivity` 的接口在 [hdl/psi_tb_activity_pkg.vhd:25-29](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L25-L29)：

```vhdl
procedure CheckNoActivity(signal Sig : in std_logic;
                          IdleTime   : in time;
                          Level      : in integer range -1 to 1; -- -1 = 不查, 0 = 低, 1 = 高
                          Msg        : in string := "";
                          Prefix     : in string := "###ERROR###: ");
```

注意 `Level` 的子类型范围是 `integer range -1 to 1`——三个合法取值 `-1/0/1`，注释直接写明含义。

实现体在 [hdl/psi_tb_activity_pkg.vhd:97-110](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L97-L110)：

```vhdl
procedure CheckNoActivity(...) is
begin
    wait for IdleTime;
    assert Sig'last_event >= IdleTime
    report Prefix & Msg & "[Unexpected Activity]"
    severity error;
    if Level /= -1 then
        StdlCompare(Level, Sig, "CheckNoActivity: " & Msg, Prefix);
    end if;
end procedure;
```

可以看到本讲 4.1 节的「底层机制」原样落地：`wait for IdleTime` 推进时间，`assert Sig'last_event >= IdleTime` 判活动，最后**条件**调用 `StdlCompare` 判电平。电平校验时消息前缀被改成 `"CheckNoActivity: " & Msg`，方便你在 Transcript 里一眼看出这条错误来自活动检查的第二段。

`CheckNoActivityStlv` 的结构完全对称，声明在 [hdl/psi_tb_activity_pkg.vhd:31-35](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L31-L35)、实现在 [hdl/psi_tb_activity_pkg.vhd:113-126](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L113-L126)。唯一不同在两处：

1. `Level` 的范围变成 `integer range -1 to integer'high`，注释写 **`otherwise interpreted unsigned`**——也就是除了 `-1`，其余值都被当成「总线应等于的无符号整数值」。这是 stlv 版与 stdl 版最大的语义差异，务必记住。
2. 电平校验改调用 `StdlvCompareInt(Level, Sig, "CheckNoActivityStlv: " & Msg, false, 0, Prefix)`，见 [hdl/psi_tb_activity_pkg.vhd:124](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L124)。注意它**硬编码**了 `IsSigned => false`（按无符号解释）和 `Tolerance => 0`（精确等于）。这意味着 `CheckNoActivityStlv(Sig, 100 ns, 5, ...)` 的电平段会要求 `Sig` 恰好等于无符号整数 `5`。

**`Level` 参数两套语义对照**：

| 过程版本 | `Level` 合法值 | 电平段调用 | 含义 |
| --- | --- | --- | --- |
| `CheckNoActivity` | `-1` / `0` / `1` | `StdlCompare(Level, Sig, ...)` | `-1` 不查；`0` 期望低；`1` 期望高 |
| `CheckNoActivityStlv` | `-1` / 任意非负整数 | `StdlvCompareInt(Level, Sig, ..., false, 0, ...)` | `-1` 不查；否则期望向量==该无符号整数 |

复用链一目了然：活动检查过程**自己只写 `'last_event` 那段**，电平段完全委托给 compare 包。所以你在 [u3-l1](u3-l1-compare-basic.md) 学到的 `StdlvCompareInt` 那条「消息里十六进制固定 32 位」的限制，在这里同样适用——如果用 `CheckNoActivityStlv` 校验一条很宽的总线并开了 `Level`，电平失败消息里的十六进制会被截到 32 位显示。需要位精确、可读的诊断时，更稳妥的做法是 `Level => -1` 只查活动，电平另用 `StdlvCompareStdlv` 单独查。

#### 4.2.4 代码实践：让 `CheckNoActivityStlv` 在意外翻转时报警

**实践目标**：写一段最小测试，演示 `CheckNoActivityStlv` 两种结果——窗口内确实稳定则**静默通过**；窗口内出现意外翻转则打印 `###ERROR###`。

**操作步骤**（示例代码，非项目原有文件）：

```vhdl
-- 示例代码：CheckNoActivityStlv 的通过 / 失败两种情形
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
library work;
use work.psi_tb_txt_util.all;
use work.psi_tb_activity_pkg.all;

entity activity_demo_tb is end entity;
architecture sim of activity_demo_tb is
    signal Data_s : std_logic_vector(7 downto 0) := (others => '0');
begin
    process
    begin
        -- 场景 1：Data_s 全程保持 0x00，应当静默通过
        print("=== 场景 1：期望空闲 100 ns，实际也空闲 ===");
        Data_s <= x"00";
        wait for 10 ns;
        CheckNoActivityStlv(Data_s, 100 ns, -1, "DUT 应保持稳定");
        -- ↑ 内部 wait 100 ns；窗口内无翻转 → 不打任何消息

        -- 场景 2：在窗口中段翻转，应当触发 ###ERROR###
        print("=== 场景 2：期望空闲 100 ns，但 50 ns 后翻转 ===");
        Data_s <= x"00";
        wait for 10 ns;
        Data_s <= x"AB" after 50 ns;   -- 50 ns 后意外翻转
        CheckNoActivityStlv(Data_s, 100 ns, -1, "DUT 应保持稳定");
        -- ↑ 内部 wait 100 ns；第 50 ns 处翻转 → last_event≈50 ns < 100 ns → 报错

        print("=== 演示结束 ===");
        wait;
    end process;
end architecture;
```

**需要观察的现象**：场景 1 之后 Transcript 里**不会**出现 `###ERROR###`；场景 2 之后会出现一行形如：

```
###ERROR###: DUT 应保持稳定[Unexpected Activity]
```

（因为这里 `Level => -1`，所以只有活动检查这一段，没有电平检查消息。）

**预期结果**：场景 2 的消息前缀正是 `###ERROR###: `，末尾是 `[Unexpected Activity]`——与 [hdl/psi_tb_activity_pkg.vhd:120-122](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L120-L122) 拼出的字符串完全一致。**待本地验证**：把这条 TB 接到 PsiSim 的 `run.tcl`/`runGhdl.tcl` 里跑一次，确认 Transcript 末尾因出现 `###ERROR###` 而被 `run_check_errors "###ERROR###"` 判为失败（这正是活动检查与 CI 联动的体现）。

**进阶观察**：把场景 2 的调用改成 `CheckNoActivityStlv(Data_s, 100 ns, 0, "DUT 应保持稳定")`（`Level=0`，期望窗口结束时总线等于 0）。因为翻转后 `Data_s = 0xAB`，你会**同时**看到两条消息：一条 `[Unexpected Activity]`，另一条来自 `StdlvCompareInt` 的 `[Expected 0(0x00000000), Received 171(0x000000AB), Tolerance 0]`（`0xAB` = 171）。这能直观验证「活动段 + 电平段」是两段独立检查。

#### 4.2.5 小练习与答案

**练习 1**：`CheckNoActivity(Rst, 500 ns, 0, "复位期")` 这一句完整表达了什么期望？

> **答案**：它期望「从调用开始的接下来 500 ns 内，`Rst` 不许翻转；且 500 ns 结束时 `Rst` 必须是低电平（`Level=0`）」。活动段由 `'last_event` 检查，电平段由 `StdlCompare(0, Rst, ...)` 检查。

**练习 2**：为什么 `CheckNoActivity` 内部用 `wait for IdleTime` 而不是 `wait until rising_edge(Clk)`？

> **答案**：因为它校验的是「一段**物理时间**窗口内无活动」，与有没有时钟无关（被测信号甚至可能不在某个时钟域里）。用 `wait for IdleTime` 直接按时间推进，最贴合「静默 `IdleTime`」的语义；时钟同步的等待是另一类需求，由 [u4-l2](u4-l2-stimulus-and-wait.md) 的 `WaitClockCycles`/`ClockedWaitTime` 等过程负责。

---

### 4.3 CheckLastActivity / CheckLastActivityStlv：即时「快照」

#### 4.3.1 概念说明

`CheckLastActivity` 与 `CheckNoActivity` 只差一个词（Last vs No），行为却完全不同：它**不等待**，调用瞬间就立刻检查「信号**到此刻为止**是否已经空闲了至少 `IdleTime`」。它是对 `'last_event` 的一次**纯快照**断言。

为什么要造一个「不等待」的版本？因为很多时候**时间已经被别的代码推进了**，你只想要一个事后断言。典型例子就在 psi_tb 自己的 I2C 包里：发一个 SCL 时钟脉冲时，代码已经用 `wait for ClkHalfPeriod` 把高电平时间走完了，接下来只想断言「在刚才这段高电平里 SDA 没有翻转」——这时再 `wait` 一次就错了，必须用快照版。

`CheckLastActivityStlv` 是它的总线版，同样不等待。

#### 4.3.2 核心流程

```
调用 CheckLastActivity(Sig, IdleTime, Level, Msg, Prefix)
        │
        ▼（注意：没有 wait！）
   assert Sig'last_event >= IdleTime
        ├─ 成立 → 通过
        └─ 不成立 → 打印：
              Prefix & Msg & "Unexpected activity, "
              & "[Expeced idle <IdleTime>, Actual idle <Sig'last_event>]"
        │
        ▼
   if Level /= -1 then
        StdlCompare(Level, Sig, ...) / StdlvCompareInt(...)
   end if
```

注意它的消息比 `CheckNoActivity` **多带了诊断信息**：会同时打印「期望空闲多久」和「实际空闲多久」。原因是快照版在任意时刻被调用，调用方不一定清楚当前状态，给出实际 `last_event` 能立刻定位「最近一次翻转发生在多久前」。

> 小提示：源码里这条消息的 `[Expeced idle ...` 少了一个 `t`（应为 `Expected`），见 [hdl/psi_tb_activity_pkg.vhd:137](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L137)。这是项目源码原样拼写，不是本讲打错；你在 Transcript 里 grep 时按 `Expeced` 才能命中。

#### 4.3.3 源码精读

声明在 [hdl/psi_tb_activity_pkg.vhd:38-42](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L38-L42)（`std_logic` 版）与 [hdl/psi_tb_activity_pkg.vhd:44-48](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L44-L48)（`std_logic_vector` 版），参数表与 `CheckNoActivity` 完全一致——区分两者**只看实现里有没有 `wait`**。

`CheckLastActivity` 实现在 [hdl/psi_tb_activity_pkg.vhd:129-143](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L129-L143)，对照 `CheckNoActivity` 看差异最清楚：

```vhdl
procedure CheckLastActivity(...) is
begin
    assert Sig'last_event >= IdleTime                            -- 注意：上面没有 wait for IdleTime
    report Prefix & Msg & "Unexpected activity, " &
                "[Expeced idle " & time'image(IdleTime) &
                ", Actual idle " & time'image(Sig'last_event) & "]"
    severity error;
    if Level /= -1 then
        StdlCompare(Level, Sig, "CheckLastActivity: " & Msg, Prefix);
    end if;
end procedure;
```

`CheckLastActivityStlv` 实现在 [hdl/psi_tb_activity_pkg.vhd:146-160](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L146-L160)，结构相同，电平段改用 `StdlvCompareInt(Level, Sig, ..., false, 0, Prefix)`（[hdl/psi_tb_activity_pkg.vhd:158](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L158)）。

**真实使用范例**：psi_tb 的 I2C 包正是 `CheckLastActivity`（快照版）的典型用户。在发送一个 SCL 脉冲时，它要校验「SCL 高电平期间 SDA 不许翻转」「SCL 高电平宽度足够」。相关代码在 [hdl/psi_tb_i2c_pkg.vhd:259](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L259) 与 [hdl/psi_tb_i2c_pkg.vhd:261](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L261)：

```vhdl
CheckLastActivity(Scl, ClkHalfPeriod*0.9, -1, GenMessageNoPrefix(..., "SCL high period too short ...", ...), Msg.Prefix);
CheckLastActivity(Sda, ClkHalfPeriod,      -1, GenMessageNoPrefix(..., "SDA not stable during SCL pulse ...", ...), Msg.Prefix);
```

读这两行能学到三件事：

1. **全部传 `Level => -1`**：I2C 这里只关心「线有没有翻转」，不关心停在哪个电平（开漏总线的电平由 `I2cPullup` 与驱动共同决定，另用 `LevelCheck` 校验），所以关掉电平段。
2. **`IdleTime` 用 `ClkHalfPeriod` 表达**：在已经 `wait for ClkHalfPeriod` 走完高电平之后，立刻断言「刚才这半周期里线没动过」——这正是快照版的用武之地，若换成 `CheckNoActivity` 会再多等半个周期，时序就错了。
3. **自定义 `Prefix` 与 `Msg`**：通过 `Msg.Prefix` 与 `GenMessageNoPrefix(...)` 拼出带功能名、位信息的可读消息，但前缀仍以 `###ERROR###` 开头，保证 CI 仍能捕获。

#### 4.3.4 代码实践：对比 `CheckNoActivity` 与 `CheckLastActivity` 的行为差

**实践目标**：亲手验证「等待版」与「快照版」在同一信号上的行为差异，理解为什么 I2C 包偏要选快照版。

**操作步骤**（示例代码，非项目原有文件）：

```vhdl
-- 示例代码：等待版 vs 快照版
library ieee;
use ieee.std_logic_1164.all;
library work;
use work.psi_tb_txt_util.all;
use work.psi_tb_activity_pkg.all;

entity snap_vs_wait_tb is end entity;
architecture sim of snap_vs_wait_tb is
    signal Flag : std_logic := '0';
begin
    process
    begin
        Flag <= '0';
        wait for 200 ns;                 -- Flag 已稳定 200 ns

        -- 快照版：立即检查「是否已空闲 >= 100 ns」→ 当前 last_event=200ns ≥100ns → 通过
        CheckLastActivity(Flag, 100 ns, -1, "快照检查");
        print("快照版调用返回，仿真时间几乎没前进");

        -- 等待版：会再 wait 100 ns，然后检查这段窗口内无活动
        CheckNoActivity(Flag, 100 ns, -1, "等待检查");
        print("等待版调用返回，仿真时间前进了 100 ns");

        wait;
    end process;
end architecture;
```

**需要观察的现象**：`CheckLastActivity` 调用前后，Transcript 里两条 `print` 之间的仿真时间几乎不变；而 `CheckNoActivity` 调用会让仿真时间明显前进约 100 ns（可在波形/时间标尺上看到）。两种调用在本例中都**不会**触发 `###ERROR###`。

**预期结果**：这直观说明二者用途——`CheckLastActivity` 是「时间已经走够，我只补一个断言」，`CheckNoActivity` 是「我还要让时间再走一段，并保证这段里没事」。**待本地验证**：在 `Flag` 已稳定的同一时刻，如果把 `CheckLastActivity(Flag, 100 ns, ...)` 的 `IdleTime` 改成 `500 ns`（大于已稳定的 200 ns），它就会触发 `###ERROR###`，且消息里会带 `[Expeced idle 500 ns, Actual idle 200 ns]` 的诊断信息。

#### 4.3.5 小练习与答案

**练习 1**：下面两段代码效果是否等价？

```vhdl
-- (A)
wait for 100 ns;
CheckLastActivity(Sig, 100 ns, -1, "chk");
-- (B)
CheckNoActivity(Sig, 100 ns, -1, "chk");
```

> **答案**：在「调用前 `Sig` 已经稳定」的前提下基本等价：两者都让仿真走过 100 ns 再断言窗口内无活动。差别在细节——(A) 先由你自己 `wait`，再由 `CheckLastActivity` 做快照（它内部不再 wait）；(B) 的 `wait` 封装在过程内部。若调用前 `Sig` 并未稳定，两者判据的「起点」一致（都是看从当前时刻往前的 `last_event`），结果仍相同。可读性上 (B) 更紧凑，推荐优先用 `CheckNoActivity`。

**练习 2**：为什么 I2C 包里校验「SDA 在 SCL 高电平期间稳定」必须用 `CheckLastActivity` 而不能用 `CheckNoActivity`？

> **答案**：因为「SCL 高电平期间」这段时间已经被前面的 `wait for ClkHalfPeriod` 走完了，此刻 SCL 即将被拉低。如果改用 `CheckNoActivity`，它会**再等 `ClkHalfPeriod`**，那段等待里 SCL 已经是低的，既破坏了 I2C 时序，校验的也不再是「高电平期间」的稳定性。快照版在「时间刚走完」的瞬间断言，才正确锁定高电平窗口。

---

## 5. 综合实践

把本讲三块知识（`'last_event` 机制、等待版 vs 快照版、`Level` 电平段）串起来，完成下面这个小任务。

**任务**：模拟一个简化的「DUT 输出握手」场景——有一个 8 位 `Done_s` 总线，复位时为 `0x00`；复位释放后它应当**保持 `0x00` 至少 200 ns**（这是被测协议要求的静默期），之后才允许变化。写一个 testbench，分别用本讲的过程验证这两条要求，并故意构造一次违规，观察 `###ERROR###`。

**建议实现要点**（示例代码框架，请自行补全并上机）：

```vhdl
-- 示例代码框架（请补全并上机验证）
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
library work;
use work.psi_tb_txt_util.all;
use work.psi_tb_activity_pkg.all;

entity handshake_tb is end entity;
architecture sim of handshake_tb is
    signal Done_s : std_logic_vector(7 downto 0) := x"00";
begin
    process
    begin
        -- 1) 复位释放后，要求 Done_s 静默 200 ns，且期间电平为 0x00
        --    用 CheckNoActivityStlv 一次完成「活动 + 电平」检查
        Done_s <= x"00";
        wait for 10 ns;
        CheckNoActivityStlv(Done_s, 200 ns, 0, "复位后静默期");
        -- ↑ Level=0：窗口结束时总线必须仍为 0x00

        -- 2) 静默期结束后，DUT 把 Done_s 拉到 0x01 并保持；
        --    用快照版断言「从拉高这一刻起的 100 ns 内不再翻转」
        Done_s <= x"01";
        wait for 100 ns;
        CheckLastActivityStlv(Done_s, 90 ns, 1, "握手电平稳定");
        -- ↑ Level=1：当前快照总线必须为 0x01

        -- 3) 故意违规：再要求静默 200 ns，但中途翻转
        Done_s <= x"01";
        wait for 10 ns;
        Done_s <= x"02" after 80 ns;
        CheckNoActivityStlv(Done_s, 200 ns, -1, "不应翻转的窗口");
        -- ↑ 期望在此看到 ###ERROR### ...[Unexpected Activity]

        print("=== 综合实践结束 ===");
        wait;
    end process;
end architecture;
```

**验收标准**：

1. 第 1、2 步**不应**产生 `###ERROR###`（如果你补全的时序正确）。
2. 第 3 步**应当**产生 `###ERROR###: 不应翻转的窗口[Unexpected Activity]`。
3. 把这个 TB 注册到 PsiSim（参考 [u1-l3](u1-l3-simulation-and-ci.md) 的 `create_tb_run`/`add_tb_run`），用 `run.tcl` 跑一遍，确认 CI 因 `###ERROR###` 而判为失败；然后把第 3 步的违规行注释掉再跑，确认 CI 转为成功（出现 `SIMULATIONS COMPLETED SUCCESSFULLY`）。这样就亲手验证了「活动检查 → `###ERROR###` → CI 判定」的完整链路。

**待本地验证**：上述时序与消息需在你本机的 ModelSim 或 GHDL 上实跑确认；不同仿真器对 `signal <= value after <time>` 与过程内 `wait` 的交错细节可能略有差异，以实际 Transcript 为准。

## 6. 本讲小结

- psi_tb 的全部「活动检查」都建立在一个 VHDL 信号属性上：**`Sig'last_event`**，它返回距信号上一次事件过去的时间；判定「`IdleTime` 内无活动」就是断言 `Sig'last_event >= IdleTime`。
- **`CheckNoActivity` / `CheckNoActivityStlv`** 内部先 `wait for IdleTime` 再查，适合「接下来这段时间不许动」；**`CheckLastActivity` / `CheckLastActivityStlv`** 不等待、做即时快照，适合「时间已被别处推进，我只补一个断言」。
- 两类版本都可选地附带**电平校验**，由 `Level` 参数控制：`std_logic` 版是 `-1/0/1`（不查/低/高），`std_logic_vector` 版是 `-1` 或一个无符号整数期望值；电平段**复用** compare 包的 `StdlCompare` / `StdlvCompareInt`。
- 活动段与电平段是**两段独立**检查（`severity error` 不中断），且消息都以 `###ERROR###: ` 开头，因此一次活动检查失败会自动变成一次 CI 失败。
- 真实工程范例见 I2C 包：[hdl/psi_tb_i2c_pkg.vhd:259](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L259)、[:261](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L261) 全部用快照版且 `Level => -1`，只校验「线有没有翻转」。

## 7. 下一步学习建议

- 继续本单元的 [u4-l2 时钟同步等待、脉冲与选通生成](u4-l2-stimulus-and-wait.md)：那里讲 `PulseSig`、`WaitClockCycles`、`ClockedWaitTime`、`GenerateStrobe`、`WaitForValueStdlv/Stdl`，补齐「激励生成」这半边，让你既能检查活动、也能主动施加激励。
- 如果你关心 I2C 那套对 `CheckLastActivity` 的真实用法，可以直接跳到 [u7 I2C 总线功能模型](u7-l1-i2c-overview-and-setup.md)，看 `GenMessageNoPrefix`、`LevelCheck` 与活动检查如何配合出完整的协议校验。
- 想深入理解被复用的电平检查底座，回顾 [u3-l1 基础比较过程](u3-l1-compare-basic.md) 中 `StdlvCompareInt` 的「32 位十六进制显示」限制，这会影响你在 `CheckNoActivityStlv` 里开启 `Level` 时的消息可读性。
