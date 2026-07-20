# 基础比较过程：整数、实数、std_logic

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `psi_tb_compare_pkg` 里每一个比较过程都遵循的「共通骨架」：`assert <条件> report <消息> severity error`，以及它们为什么统一用 `###ERROR###: ` 作为默认消息前缀。
- 掌握 `IntCompare` 与 `RealCompare` 的参数（特别是 `Tolerance` 容差），理解「容差带」为什么比严格相等更适合 testbench。
- 会用 `StdlCompare` 的两个重载（整数期望值 / `std_logic` 期望值）做单比特检查，并理解为什么单比特比较没有容差。
- 会用 `StdlvCompareInt`（向量对整数，带 `IsSigned`/`Tolerance`）和 `StdlvCompareStdlv`（向量对向量，位精确）做总线级检查，看懂它们各自的消息格式和适用场景。
- 在自己的 testbench 里写出一次「故意失败」的比较，并预测 Transcript 里出现的 `Expected ... Received ... Tolerance ...` 消息。

## 2. 前置知识

本讲承接 [u2-l1 字符串与数值转换函数](u2-l1-txt-util-conversions.md)，请先确认你已经了解：

- **`to_string` / `str` / `hstr`**：把整数、实数、`std_logic`、`std_logic_vector` 转成可读字符串的函数。本讲里所有错误消息里的 `Expected 100`、`0xFF`、`Received 0110...` 全是靠它们拼出来的。回顾一句话：`to_string(integer)` 输出十进制，`str(slv)` 输出二进制位串（MSB 在左），`hstr(slv)` 输出十六进制。
- **`###ERROR###` 前缀**（[u1-l3 仿真环境与 CI 构建流程](u1-l3-simulation-and-ci.md)）：psi_tb 全库统一的错误标记。CI 脚本 `run.tcl` 末尾的 `run_check_errors "###ERROR###"` 会扫描 Transcript，只要出现这个字符串就判定本次仿真「自检失败」。本讲的每一个比较过程，都把自己的默认消息前缀设成了 `###ERROR###: `，所以一次比较失败会自动变成一次 CI 失败——这正是这套机制的设计意图。
- **testbench 不可综合**（[u1-l1](u1-l1-project-overview.md)）：因此这里可以放心使用 `assert`/`report`、字符串拼接 `&`、`integer'image` 这些「只为仿真存在」的语言特性。

一个需要先澄清的 VHDL 基础：**`assert` 并不会停止仿真**。`assert <条件> report <消息> severity error` 的语义是「当条件为假时，把消息以 `error` 级别打印出来」。在 ModelSim / GHDL 的默认配置下，`severity error` 只打印消息、**不中断**仿真（只有 `severity failure` 才会默认中断）。这恰恰是 testbench 想要的：一次跑完，把所有不匹配的地方一次性都报出来，而不是在第一个错误处就停下。所以「比较过程检测到错误」与「仿真停下来」是两回事——前者靠 `###ERROR###` 文本，后者靠 CI 的事后扫描。

## 3. 本讲源码地图

本讲只涉及一个源文件，但它是 u4（activity）、u5（axi）、u7（i2c）几乎所有 BFM 的共同依赖：

| 文件 | 作用 |
| --- | --- |
| [hdl/psi_tb_compare_pkg.vhd](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd) | 比较与检查助手包。提供 `IntCompare`、`RealCompare`、`StdlCompare`（两个重载）、`StdlvCompareInt`、`StdlvCompareStdlv`，以及本讲暂不展开的 `SignCompare*` / `UsignCompare*`（留给 [u3-l2](u3-l2-compare-signed-unsigned.md)）和下标标注用的 `IndexString`。 |

文件结构是 psi_tb 的一贯风格：`package ... is`（第 20–99 行）放过程**声明**（接口/参数表），`package body`（第 104–301 行）放**实现**。本讲按「声明 + 实现」成对引用。

它在编译链里的位置见 [sim/config.tcl:28-33](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/config.tcl#L28-L33)：`psi_tb_compare_pkg.vhd` 排在 `psi_tb_txt_util.vhd` 之后编译，因为它 `use work.psi_tb_txt_util.all`（[hdl/psi_tb_compare_pkg.vhd:10-15](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L10-L15)）。在自己的 testbench 里引用方式与 [testbench/psi_tb_i2c_pkg_tb.vhd:14-16](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L14-L16) 一致：

```vhdl
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

library work;
use work.psi_tb_txt_util.all;    -- 提供 to_string/str/hstr
use work.psi_tb_compare_pkg.all; -- 提供 IntCompare 等
```

本讲覆盖的 5 个过程一览（`Prefix` 全部默认 `###ERROR###: `）：

| 过程 | 比较 | 容差 | 消息里额外显示 | 典型用途 |
| --- | --- | --- | --- | --- |
| `IntCompare` | int ↔ int | 有 | 十进制 | 计数器、整数结果 |
| `RealCompare` | real ↔ real | 有 | 十进制 | 浮点/定点结果对拍 |
| `StdlCompare`（int 重载） | `0/1` ↔ `std_logic` | 无 | 单字符 | 单比特握手信号 |
| `StdlCompare`（stdl 重载） | `std_logic` ↔ `std_logic` | 无 | 单字符 | 单比特握手信号 |
| `StdlvCompareInt` | int ↔ `std_logic_vector` | 有 | 十进制 + 十六进制 | 总线数据对整数 |
| `StdlvCompareStdlv` | slv ↔ slv | 无（位精确） | 二进制 + 十六进制 | 控制字段、掩码 |

## 4. 核心概念与源码讲解

### 4.1 assert/report/severity 与 ###ERROR### 前缀：所有比较过程的共通骨架

#### 4.1.1 概念说明

先抽象地看：一个 testbench 里「检查 DUT 输出是否等于期望值」的动作，本质上是同一个模板的反复抄写——

```
如果 Actual 不等于 Expected：
    在 Transcript 打印一条「期望是什么、实际是什么」的可读消息，
    并且这条消息要能被 CI 认出来。
```

如果每次都手写 VHDL 的 `assert ... report ... severity error`，你会重复做三件事：写比较条件、拼消息字符串、加 `###ERROR###` 前缀。`psi_tb_compare_pkg` 的全部价值，就是把这三件事**封装成一行过程调用**。理解了这一层，下面所有具体过程都只是「换一种数据类型、换一种比较条件」的变体。

这里有一个关键约定：**前缀是参数，不是写死的**。每个过程的最后一个参数都是

```vhdl
Prefix : in string := "###ERROR###: "
```

默认值就是 CI 要扫描的那个标记。但你可以传自己的前缀（比如 `"###WARNING###: "`）来区分「致命错误」和「可接受的告警」。本讲为了和 CI 对齐，全部沿用默认前缀。

#### 4.1.2 核心流程

一个比较过程内部只做两步：

1. **判断**：用布尔表达式检查 `Actual` 是否落在可接受范围内（带容差的过程判断「是否落在容差带里」，位精确的过程判断「是否完全相等」）。
2. **报告**：条件为假时，执行

   ```vhdl
   assert <条件为真才不报>
   report Prefix & Msg & " [Expected ..., Received ..., Tolerance ...]"
   severity error;
   ```

注意 `assert` 的条件方向：**条件为真 = 通过 = 不报**；条件为假 = 失败 = 打印 `report` 后面的字符串。所以代码里看到的是「正确的条件」，而不是「错误的条件」。

带容差的过程（`IntCompare`/`RealCompare`/`StdlvCompareInt`）用的判断是一个**容差带**。设期望值 \(E\)、实际值 \(A\)、容差 \(T\)，则通过条件是：

\[
A \in [\,E - T,\ E + T\,] \quad\Longleftrightarrow\quad (A \ge E - T)\ \wedge\ (A \le E + T)
\]

这正是源码里反复出现的 `(Actual >= Expected - Tolerance) and (Actual <= Expected + Tolerance)`。

#### 4.1.3 源码精读

`Prefix` 默认值的约定贯穿整个声明区，例如 `IntCompare` 的声明：

[hdl/psi_tb_compare_pkg.vhd:51-55](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L51-L55) —— 注意 `Tolerance` 和 `Prefix` 都有默认值，所以最简调用只需 `IntCompare(期望, 实际, "消息")`：

```vhdl
procedure IntCompare(Expected  : in integer;
                     Actual    : in integer;
                     Msg       : in string;
                     Tolerance : in integer := 0;
                     Prefix    : in string  := "###ERROR###: ");
```

声明区里能看到 `Prefix` 的默认值在每个过程上都一样，例如 [L31](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L31)、[L55](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L55)、[L62](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L62)——这就是「全库统一前缀」的物证。

> 一个容易被忽略的细节：`assert` 打印出来的那一行，除了 `report` 字符串本身，仿真器还会**额外**加上 `** Error:`（对应 `severity error`）和时间戳/迭代号前缀，例如 ModelSim 里大致是 `# ** Error: ###ERROR###: ... Time: 150 ns`。所以你在 Transcript 看到的实际行比 `report` 字符串要长一些，前面那截是仿真器加的，**不要误以为是 psi_tb 拼出来的**。CI 扫描的是子串 `###ERROR###`，所以这些前缀不影响判定。

#### 4.1.4 代码实践（阅读型）

本小节先做一个零代码的观察练习，建立对「前缀契约」的直觉。

1. **实践目标**：确认「比较过程失败 → Transcript 出现 `###ERROR###` → CI 判失败」这条链路的源头就在默认参数 `Prefix`。
2. **操作步骤**：
   - 打开 [hdl/psi_tb_compare_pkg.vhd](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd)，在声明区（第 20–99 行）数一下有多少个过程的 `Prefix` 默认值是 `"###ERROR###: "`。
   - 再打开 [sim/run.tcl](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/run.tcl)，找到末尾的 `run_check_errors "###ERROR###"`，确认它扫描的字符串与上面的默认前缀**完全一致**（包括结尾的冒号和空格的差异——注意扫描的是 `###ERROR###` 这个子串，不含 `: `）。
3. **需要观察的现象**：两个文件里的字符串能对上。
4. **预期结果**：你会看到声明区里 11 个过程（含本讲不展开的 `SignCompare*`/`UsignCompare*`）的 `Prefix` 默认值全部是 `"###ERROR###: "`，而 `run_check_errors` 扫的是 `"###ERROR###"` 子串——后者是前者的前缀，所以任何一次默认前缀的失败都会被命中。

#### 4.1.5 小练习与答案

**练习 1**：如果我希望某次比较失败时只打告警、**不**让 CI 判失败，应该怎么做？

**参考答案**：给该次调用显式传一个不含 `###ERROR###` 的 `Prefix`，例如 `IntCompare(100, actual, "soft check", 0, "###WARNING###: ")`。由于 `run_check_errors` 只扫 `###ERROR###` 子串，这条消息不会触发 CI 失败，但仍会出现在 Transcript 里供人查看。

**练习 2**：`assert (Actual >= Expected - Tolerance)` 里，如果条件计算结果为**真**，会发生什么？

**参考答案**：什么都不发生。`assert` 只在条件为假时才执行 `report`。条件为真代表「实际值落在容差带内」=检查通过，所以静默通过、不打印任何消息。

---

### 4.2 IntCompare 与 RealCompare：带容差的标量比较

#### 4.2.1 概念说明

`IntCompare` 和 `RealCompare` 是最简单的两个过程，分别比较两个 `integer` 和两个 `real`。它们解决的问题很朴素：**testbench 里最常见的检查就是「DUT 算出来的数，对不对」**。比如一个计数器读回的值、一个滤波器输出的定点结果转成的实数。

为什么需要**容差**？因为很多硬件计算（尤其是定点/浮点）存在舍入误差，期望值和实际值在 bit 级别几乎永远不相等，但在工程意义上「差不多」。如果用严格相等 `Actual = Expected`，会因为最后一两位的舍入差而频繁误报。容差允许你声明「差几个 LSB 以内都算对」。

#### 4.2.2 核心流程

两个过程的逻辑**完全同构**，只是数据类型不同：

```
IntCompare(E, A, Msg, T) / RealCompare(E, A, Msg, T):
  通过条件 := (A >= E - T) and (A <= E - T 的上界 E + T)
  若 不通过:
      打印: Prefix & Msg & " [Expected " & 文本(E) & ", Received " & 文本(A) & ", Tolerance " & 文本(T) & "]"
```

其中 `文本(...)` 在 `IntCompare` 里是 `to_string`（u2-l1 讲过，整数重载走十进制），在 `RealCompare` 里也是 `to_string`（实数重载内部用 `real'image`）。

注意消息格式是固定的三元组 `Expected / Received / Tolerance`，用方括号包起来——这是 psi_tb 所有带容差比较的统一排版，方便你用脚本批量解析 Transcript。

#### 4.2.3 源码精读

**`IntCompare` —— 整数带容差比较。**

[hdl/psi_tb_compare_pkg.vhd:197-209](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L197-L209) 用一条 `assert` 同时完成「判断」和「报告」，`to_string` 把三个整数拼进消息：

```vhdl
procedure IntCompare(Expected  : in integer;
                     Actual    : in integer;
                     Msg       : in string;
                     Tolerance : in integer := 0;
                     Prefix    : in string  := "###ERROR###: ") is
begin
  assert (Actual >= Expected - Tolerance) and (Actual <= Expected + Tolerance)
  report Prefix & Msg & 
            " [Expected " & to_string(Expected) & 
            ", Received " & to_string(Actual) & 
            ", Tolerance " & to_string(Tolerance) & "]"
  severity error;
end procedure;
```

读法：条件 `(Actual >= Expected - Tolerance) and (Actual <= Expected + Tolerance)` 正是容差带公式；为假时，`&` 把 6 段字符串拼成一条形如 `###ERROR###: <Msg> [Expected 100, Received 105, Tolerance 2]` 的消息。

**`RealCompare` —— 实数带容差比较。**

[hdl/psi_tb_compare_pkg.vhd:212-224](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L212-L224) 与 `IntCompare` 几乎逐字相同，差别只在类型（`real`）和 `Tolerance` 的默认值（`0.0`）：

```vhdl
procedure RealCompare(Expected  : in real;
                      Actual    : in real;
                      Msg       : in string;
                      Tolerance : in real   := 0.0;
                      Prefix    : in string := "###ERROR###: ") is
begin
  assert (Actual >= Expected - Tolerance) and (Actual <= Expected + Tolerance)
  report Prefix & Msg & 
            " [Expected " & to_string(Expected) & 
            ", Received " & to_string(Actual) & 
            ", Tolerance " & to_string(Tolerance) & "]"
  severity error;
end procedure;
```

> 小知识：实数的 `to_string` 实际是 `real'image(num)`（见 [hdl/psi_tb_txt_util.vhd:362-365](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L362-L365)）。`real'image` 的具体小数位数/科学计数法格式**由仿真器决定**（GHDL 和 ModelSim 输出可能不同），所以实数消息的精确外观属于「待本地验证」。

#### 4.2.4 代码实践

这是本讲的主实践：写一个最小 testbench，分别调用 `IntCompare`（带容差）和 `RealCompare`，**故意制造一次失败**，观察消息格式。

1. **实践目标**：亲手触发一次 `IntCompare` 和一次 `RealCompare` 的失败，确认 Transcript 出现 `[Expected ..., Received ..., Tolerance ...]`，并验证「在容差带内则静默通过」。
2. **操作步骤**：
   - 把下面的示例代码存成 `tb_compare_basic.vhd`（**这是示例代码，不在仓库里**），放进 `testbench/` 目录。
   - 在 [sim/config.tcl](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/config.tcl) 的 `add_sources "../testbench" {...} -tag tb` 列表里追加 `tb_compare_basic.vhd`，并仿照 [L41-L42](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/config.tcl#L41-L42) 用 `create_tb_run "tb_compare_basic"` + `add_tb_run` 注册一次运行。
   - 按 [u1-l3](u1-l3-simulation-and-ci.md) 的办法用 `sim/run.tcl`（ModelSim）或 `sim/runGhdl.tcl`（GHDL）跑这个 TB。

   ```vhdl
   -- 示例代码：本讲为演示而写，仓库中不存在
   library ieee;
   use ieee.std_logic_1164.all;
   use ieee.numeric_std.all;
   library work;
   use work.psi_tb_txt_util.all;
   use work.psi_tb_compare_pkg.all;

   entity tb_compare_basic is
   end entity;

   architecture sim of tb_compare_basic is
   begin
     process
     begin
       print("---- IntCompare 演示 ----");
       -- 通过：105 在 [98, 102] 之外 => 失败，会报错
       IntCompare(100, 105, "int 超出容差", 2);
       -- 通过：101 在 [98, 102] 之内 => 静默通过，不打印
       IntCompare(100, 101, "int 在容差内", 2);

       print("---- RealCompare 演示 ----");
       -- 失败：1.5 不在 [0.8, 1.2] 之内
       RealCompare(1.0, 1.5, "real 超出容差", 0.2);
       -- 通过：1.1 在 [0.8, 1.2] 之内
       RealCompare(1.0, 1.1, "real 在容差内", 0.2);

       print("---- 演示结束 ----");
       wait;   -- 与仓库 psi_tb_i2c_pkg_tb 一致，用 wait; 收尾
     end process;
   end architecture;
   ```

3. **需要观察的现象**：Transcript 里应出现 4 段 `print` 分节标记，以及**两条** `###ERROR###` 消息（来自两次故意失败），而两次「在容差内」的调用**不产生**任何 `###ERROR###`。
4. **预期结果**：根据源码可精确预测两条失败消息（冒号空格后的 `Msg` 是你传入的字符串）：

   ```
   ###ERROR###: int 超出容差 [Expected 100, Received 105, Tolerance 2]
   ###ERROR###: real 超出容差 [Expected <1.0 的 image>, Received <1.5 的 image>, Tolerance <0.2 的 image>]
   ```

   实数那一行的具体小数格式由仿真器决定（**待本地验证**），但 `[Expected ..., Received ..., Tolerance ...]` 的骨架完全确定。另外，由于这两条消息含 `###ERROR###`，CI（`run_check_errors`）会把本次运行判为失败——这正是「故意失败」的预期副作用。
5. **若无法运行**：在没有仿真器的环境下，可直接对照 [L197-L209](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L197-L209) 手工演算：把 `Expected=100, Actual=105, Tolerance=2` 代入 `report` 字符串的拼接，即可得到上面第一行的精确文本。

#### 4.2.5 小练习与答案

**练习 1**：调用 `IntCompare(100, 100, "x")`（不传 `Tolerance`）会发生什么？

**参考答案**：`Tolerance` 取默认值 `0`，容差带收缩为 `[100, 100]`，等价于严格相等 `Actual = Expected`。`100 = 100` 成立，条件为真，静默通过。

**练习 2**：把 `RealCompare` 的 `Tolerance` 设成一个负数（比如 `-0.1`）会怎样？

**参考答案**：容差带变成 `[E - (-0.1), E + (-0.1)] = [E+0.1, E-0.1]`，下界大于上界，**没有任何实数能同时满足** `A >= E+0.1` 且 `A <= E-0.1`。于是无论 `Actual` 是多少都会失败。过程没有对负容差做防御，调用方需自己保证 `Tolerance >= 0`。

**练习 3**：为什么 `IntCompare` 用 `to_string(Expected)` 而不直接用 VHDL 内建的 `integer'image(Expected)`？

**参考答案**：在本包里两者结果相同（`to_string(integer)` 内部就是 `str(int)` → 十进制）。选用 `to_string` 是为了和库的统一风格一致、并兼容那些 `integer'image` 行为不一致的老仿真器。顺带一提：`StdlvCompareInt` 里其实用的是 `integer'image`（见 4.4.3），说明库内部这两种写法并存——功能等价，属历史风格差异。

---

### 4.3 StdlCompare（两个重载）：单比特比较

#### 4.3.1 概念说明

`StdlCompare` 专门检查**单个 `std_logic` 信号**的电平。它有两个重载（overload），区别只在「期望值用什么类型给出」：

- 重载 A：期望值用 `integer range 0 to 1` 给出（`0` 表示低、`1` 表示高）——方便你在循环或条件分支里用整数驱动。
- 重载 B：期望值直接用 `std_logic` 给出——方便你拿另一个信号当期望值。

两个重载同名，VHDL 编译器根据实参类型自动选择。单比特比较**没有容差**概念：一个比特要么对、要么错，没有「差不多」。

#### 4.3.2 核心流程

```
StdlCompare(期望, Actual, Msg):
  把期望归一成 std_logic: 0/'0' -> '0', 其余 -> '1'
  通过条件 := (Actual = 期望的归一值)
  若 不通过:
      打印: Prefix & Msg & " [Expected " & str(期望) & ", Received " & str(Actual) & "]"
```

注意消息里用 `str(std_logic)`（u2-l1 讲过：把单个 `std_logic` 转成单字符，比如 `'1'`、`'Z'`、`'U'`）。所以即便 `Actual` 是个未初始化的 `'U'`，消息里也会如实显示 `Received U`，便于定位问题。

#### 4.3.3 源码精读

**重载 A：期望值是 `integer range 0 to 1`。**

[hdl/psi_tb_compare_pkg.vhd:159-175](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L159-L175) 先把整数期望值翻译成 `std_logic`，再做单比特比较：

```vhdl
procedure StdlCompare(Expected : in integer range 0 to 1;
                      Actual   : in std_logic;
                      Msg      : in string;
                      Prefix   : in string := "###ERROR###: ") is
  variable ExStdl_v : std_logic;
begin
  if Expected = 0 then
    ExStdl_v := '0';
  else
    ExStdl_v := '1';
  end if;
  assert Actual = ExStdl_v
  report Prefix & Msg & 
            " [Expected " & str(ExStdl_v) & 
            ", Received " & str(Actual) & "]"
  severity error;
end procedure;
```

读法：子类型 `integer range 0 to 1` 在编译期限制了 `Expected` 只能取 0 或 1；运行期再用 `if` 把它映射成 `'0'`/`'1'`。`str(ExStdl_v)` 产出单字符 `'0'` 或 `'1'` 进入消息。

**重载 B：期望值是 `std_logic`。**

[hdl/psi_tb_compare_pkg.vhd:178-194](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L178-L194) 结构相同，只是 `Expected` 直接是 `std_logic`：

```vhdl
procedure StdlCompare(Expected : in std_logic;
                      Actual   : in std_logic;
                      Msg      : in string;
                      Prefix   : in string := "###ERROR###: ") is
  variable ExStdl_v : std_logic;
begin
  if Expected = '0' then
    ExStdl_v := '0';
  else
    ExStdl_v := '1';
  end if;
  assert Actual = ExStdl_v
  report Prefix & Msg & 
          " [Expected " & str(ExStdl_v) & 
          ", Received " & str(Actual) & "]"
  severity error;
end procedure;
```

两个重载的声明分别在 [L40-L43](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L40-L43) 与 [L45-L48](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L45-L48)。重载 B 是 v2.5.0 才加入的（见 [Changelog.md:13](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/Changelog.md#L13)），解决的是「我想直接拿一个 `std_logic` 信号当期望值，不想先转成整数」的便利性需求。

> 复用链预告：`StdlCompare` 不只是给人用的，它也被 `psi_tb_activity_pkg` 内部复用。例如 [hdl/psi_tb_activity_pkg.vhd:108](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L108) 的 `CheckNoActivity` 在检查「信号在空闲窗口内是否保持期望电平」时，直接调用了 `StdlCompare(Level, Sig, ...)`。这条「compare → activity」的复用关系会在 [u4-l1](u4-l1-activity-check.md) 展开。

#### 4.3.4 代码实践

把下面的调用追加到 4.2.4 那个示例 testbench 的 `process` 里（`wait;` 之前），观察单比特比较的输出。

1. **实践目标**：触发 `StdlCompare` 两个重载各一次失败，确认消息是单字符形式的 `Expected 1, Received 0`。
2. **操作步骤**：在 process 里加入：

   ```vhdl
   -- 示例代码
   signal done : std_logic := '0';
   ...
   print("---- StdlCompare 演示 ----");
   StdlCompare(1, '0',   "整数期望重载：期望高，实际低");  -- 重载 A，失败
   StdlCompare('1', 'Z', "stdl期望重载：期望1，实际Z");    -- 重载 B，失败
   StdlCompare('0', '0', "应该静默通过");                  -- 通过
   ```

   （若用局部变量更顺手，把 `done` 换成 `variable` 并在调用前赋值即可。）
3. **需要观察的现象**：前两次调用各打印一条 `###ERROR###`，第三次无输出。
4. **预期结果**：

   ```
   ###ERROR###: 整数期望重载：期望高，实际低 [Expected 1, Received 0]
   ###ERROR###: stdl期望重载：期望1，实际Z [Expected 1, Received Z]
   ```

   第二行尤其值得注意：`Received Z` 说明 `str` 如实把高阻态显示了出来——这正是用 `str` 而不是直接判等后打印 `integer'image` 的好处。

#### 4.3.5 小练习与答案

**练习 1**：调用 `StdlCompare(1, 'H', "x")`，会报错吗？

**参考答案**：会报错。`'H'`（弱高）是 `std_logic` 的一个独立值，与 `'1'`（强高）**不相等**。`str('H')` 输出字符 `H`，所以消息是 `... [Expected 1, Received H]`。若你希望 `'H'` 和 `'1'` 视作相等，需要先用 `std_logic` 的解析函数（如 `to_X01`）归一化，再比较——`StdlCompare` 本身不做这种归一。

**练习 2**：为什么单比特比较没有 `Tolerance` 参数？

**参考答案**：容差是为「连续量/多位数值」设计的（差几个 LSB 仍算对）。单个比特只有 0/1 两态，不存在「差一点点」的中间地带，所以没有容差概念。这也是 `StdlCompare` 的参数表比 `IntCompare` 少一项的原因。

---

### 4.4 StdlvCompareInt 与 StdlvCompareStdlv：向量比较

#### 4.4.1 概念说明

DUT 的数据输出几乎都是 `std_logic_vector`（一条总线），而你在 testbench 里更愿意用整数来描述期望值（「我期望读到 42」）。`StdlvCompareInt` 解决的就是「**用一个整数期望值去检查一条向量实际值**」的场景，并且支持有符号/无符号两种解释、带容差。

`StdlvCompareStdlv` 则是「**向量对向量**」的位精确比较，不带容差。它适合检查那些「每一位都必须一模一样」的对象，比如 AXI 的 `bresp`/`rresp` 响应码、写选通 `wstrb`、控制字段——这些字段不是「数值」，谈容差没有意义，必须逐位相同。

两者消息格式不同：`StdlvCompareInt` 因为涉及整数，消息同时给出**十进制和十六进制**两种表示，方便人眼对照；`StdlvCompareStdlv` 给出**二进制和十六进制**两种表示。

#### 4.4.2 核心流程

**`StdlvCompareInt`** 的流程多了一步「类型转换 + 重新编码成 32 位用于显示」：

```
StdlvCompareInt(E, Actual, Msg, IsSigned, T):
  若 IsSigned:
      ActualInt := to_integer(signed(Actual))
      把 E 与 ActualInt 各自 to_signed(..., 32) 再转回 slv，得到 ExpectedStdlv32/ActualStdlv32（用于显示）
  否则:
      ActualInt := to_integer(unsigned(Actual))
      用 to_unsigned(..., 32) 得到两个 32 位显示向量
  通过条件 := (ActualInt >= E - T) and (ActualInt <= E + T)
  若 不通过:
      打印: Prefix & Msg & " [Expected " & image(E) & "(0x" & hstr(ExpectedStdlv32) & ")"
            & ", Received " & image(ActualInt) & "(0x" & hstr(ActualStdlv32) & ")"
            & ", Tolerance " & image(T) & "]"
```

注意显示用的 `ExpectedStdlv32_v` / `ActualStdlv32_v` **固定是 32 位**（见源码变量声明）。这意味着：

- 对窄向量（如 8 位），十六进制会显示成 32 位的符号/零扩展形式（例如 8 位有符号 `-5` 显示成 `0xFFFFFFFB`），反而更直观。
- 对**宽度超过 32 位、且数值超出 32 位整数范围**的向量，`to_integer` 会在第一步就溢出——这是 `StdlvCompareInt` 的已知边界，也是后来 v2.6.0 新增 `SignCompare2`（直接用 `hstr` 显示全宽向量）的动机，留给 [u3-l2](u3-l2-compare-signed-unsigned.md) 讲。

**`StdlvCompareStdlv`** 的流程：

```
StdlvCompareStdlv(Expected, Actual, Msg):
  把 Expected/Actual 各自赋给一个 downto-0 范围的 constant（范围归一化）
  通过条件 := (Actual = Expected)   -- 逐位相等，要求长度一致
  若 不通过:
      打印: Prefix & Msg & " [Expected " & str(Expected) & "(0x" & hstr(Expected) & ")"
            & ", Received " & str(Actual) & "(0x" & hstr(Actual) & "]" & "]"
```

#### 4.4.3 源码精读

**`StdlvCompareInt` —— 向量对整数（有符号/无符号可选，带容差）。**

[hdl/psi_tb_compare_pkg.vhd:113-140](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L113-L140) 先按 `IsSigned` 做转换并构造 32 位显示向量，再用容差带判断：

```vhdl
procedure StdlvCompareInt(Expected  : in integer;
                          Actual    : in std_logic_vector;
                          Msg       : in string;
                          IsSigned  : in boolean := true;
                          Tolerance : in integer := 0;
                          Prefix    : in string  := "###ERROR###: ") is
  variable ActualInt_v       : integer;
  variable ExpectedStdlv32_v : std_logic_vector(31 downto 0);
  variable ActualStdlv32_v   : std_logic_vector(31 downto 0);
begin
  -- Convert Input
  if IsSigned then
    ActualInt_v       := to_integer(signed(Actual));
    ExpectedStdlv32_v := std_logic_vector(to_signed(Expected, 32));
    ActualStdlv32_v   := std_logic_vector(to_signed(ActualInt_v, 32));
  else
    ActualInt_v       := to_integer(unsigned(Actual));
    ExpectedStdlv32_v := std_logic_vector(to_unsigned(Expected, 32));
    ActualStdlv32_v   := std_logic_vector(to_unsigned(ActualInt_v, 32));
  end if;
  -- Assertion
  assert (ActualInt_v >= Expected - Tolerance) and (ActualInt_v <= Expected + Tolerance)
  report Prefix & Msg & 
            " [Expected " & integer'image(Expected) & "(0x" & hstr(ExpectedStdlv32_v) & ")" &
            ", Received " & integer'image(ActualInt_v) & "(0x" & hstr(ActualStdlv32_v) & ")" &
            ", Tolerance " & integer'image(Tolerance) & "]"
  severity error;
end procedure;
```

读法要点：

- `IsSigned` 默认 `true`。检查无符号总线（如地址、计数器）时记得传 `IsSigned => false`，否则高位 `1` 会被当成负号，给出错误消息（这是初学者最常踩的坑）。
- 消息里同时有十进制（`integer'image`）和十六进制（`hstr(...32位向量)`），例如 `[Expected -5(0xFFFFFFFB), Received -5(0xFFFFFFFB), Tolerance 0]`。
- 这里用的 `integer'image`（而非 `to_string`），与 `IntCompare` 风格略不同，但功能等价。

**`StdlvCompareStdlv` —— 向量对向量（位精确，带范围归一化）。**

[hdl/psi_tb_compare_pkg.vhd:143-156](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L143-L156) 用两个 `constant` 把入参强制归一到 `downto 0` 范围，再比较：

```vhdl
procedure StdlvCompareStdlv(Expected : in std_logic_vector;
                            Actual   : in std_logic_vector;
                            Msg      : in string;
                            Prefix   : in string := "###ERROR###: ") is
  constant Expected_c : std_logic_vector(Expected'length - 1 downto 0) := Expected;
  constant Actual_c   : std_logic_vector(Actual'length - 1 downto 0)   := Actual;
begin
  -- Assertion
  assert Actual_c = Expected_c
  report Prefix & Msg & 
            " [Expected " & str(Expected_c) & "(0x" & hstr(Expected_c) & ")" &
            ", Received " & str(Actual_c) & "(0x" & hstr(Actual_c) & ")" & "]"
  severity error;
end procedure;
```

读法要点：

- `constant Expected_c : std_logic_vector(...'length - 1 downto 0) := Expected;` 这一步是**范围归一化**。VHDL 里 `std_logic_vector` 的 `=` 只要求两操作数长度相同、不要求范围方向一致，所以严格说 `assert Actual = Expected` 就能正确比较；但 `str`/`hstr` 在显示时要沿 `'range` 遍历，若入参是 `to` 方向（如 `0 to 7`），显示顺序会错乱。归一到 `downto 0` 保证显示「MSB 在左」。Changelog [1.2.0 条目](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/Changelog.md#L99)「TO was used (and not DOWNTO)」的 bugfix 修的正是这类显示问题。
- 消息同时给出二进制 `str(...)` 和十六进制 `hstr(...)`，没有 `Tolerance` 字段（位精确，无容差）。

> 复用链：`StdlvCompareStdlv` 和 `StdlvCompareInt` 是 `psi_tb_axi_pkg` 用得最多的两个检查。例如 [hdl/psi_tb_axi_pkg.vhd:549](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L549) 用 `StdlvCompareStdlv(xRESP_OKAY_c, sm.bresp, ...)` 校验 AXI 写响应码；[L871](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L871) 用 `StdlvCompareInt(AxAddr, ms.awaddr, ..., false)` 校验地址。这些会在 [u5](u5-l2-axi-single-transactions.md) 展开。同样，`SignCompareInt`/`UsignCompareInt` 内部就是一行 `StdlvCompareInt(...)` 转发（见 [L278-L283](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L278-L283) 与 [L293-L298](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L293-L298)），下一讲细讲。

#### 4.4.4 代码实践

把下面两组调用追加到示例 testbench，观察两种向量比较的消息差异。

1. **实践目标**：分别触发一次 `StdlvCompareInt` 失败和一次 `StdlvCompareStdlv` 失败，对照「十进制+十六进制」与「二进制+十六进制」两种消息排版；同时体会 `IsSigned` 的作用。
2. **操作步骤**：在 process 里加入（变量声明放在 `process` 的 `is` 与 `begin` 之间）：

   ```vhdl
   -- 示例代码
   variable data8  : std_logic_vector(7 downto 0);
   variable resp   : std_logic_vector(1 downto 0);
   ...
   print("---- StdlvCompareInt 演示 ----");
   data8 := "11111011";           -- 8 位有符号 = -5，无符号 = 251
   StdlvCompareInt(-5, data8, "有符号解释：应通过", true,  0);  -- 通过
   StdlvCompareInt(251, data8, "无符号解释：应通过", false, 0); -- 通过
   StdlvCompareInt(0, data8, "故意失败（无符号）", false, 0);   -- 失败

   print("---- StdlvCompareStdlv 演示 ----");
   resp := "10";                  -- 比如 AXI SLVERR
   StdlvCompareStdlv("00", resp, "期望OKAY，实际非OKAY");       -- 失败
   StdlvCompareStdlv("10", resp, "应通过");                     -- 通过
   ```

3. **需要观察的现象**：两条「应通过」调用无输出；两条「故意失败」调用各打印一条 `###ERROR###`，且消息格式不同。
4. **预期结果**（根据源码精确可推，`data8="11111011"` 的无符号值是 251、十六进制 `FB`；32 位零扩展为 `000000FB`）：

   ```
   ###ERROR###: 故意失败（无符号） [Expected 0(0x00000000), Received 251(0x000000FB), Tolerance 0]
   ###ERROR###: 期望OKAY，实际非OKAY [Expected 00(0x0), Received 10(0x2)]
   ```

   注意第一条消息里十六进制是 **32 位**显示（`0x000000FB`），这是 `StdlvCompareInt` 固定向量宽度的直接体现。第二条消息里二进制是 2 位 `10`、十六进制是 1 位 `2`，宽度跟随入参。
5. **若无法运行**：把 `data8` 与 `resp` 的值代入 [L113-L156](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L113-L156) 的转换与拼接，即可手工得到上面两行。

#### 4.4.5 小练习与答案

**练习 1**：对一个 8 位向量 `"10000000"`（最高位为 1）分别用 `IsSigned=true` 和 `IsSigned=false` 调 `StdlvCompareInt(128, actual, ..., ?, 0)`，哪个会通过？

**参考答案**：`IsSigned=false`（无符号）时 `to_integer(unsigned("10000000")) = 128`，等于期望值 128，通过。`IsSigned=true`（有符号）时 `to_integer(signed("10000000")) = -128`，不等于 128，失败，且消息会显示 `Received -128(0xFFFFFF80)`。可见 **`IsSigned` 选错会直接导致误报**。

**练习 2**：`StdlvCompareStdlv` 为什么不提供 `Tolerance` 参数？

**参考答案**：它面向「控制字段/响应码/掩码」这类非数值对象，正确性标准是「逐位完全一致」，没有「差几位也算对」的语义。若需要对向量做带容差的数值比较，应该用 `StdlvCompareInt`（先转成整数再套容差带）。

**练习 3**：为什么 `StdlvCompareStdlv` 要先把入参赋给 `downto 0` 范围的 `constant`？

**参考答案**：为了**显示正确**。VHDL 的向量 `=` 不依赖范围方向，比较本身不需要归一；但 `str`/`hstr` 是沿 `'range` 遍历生成字符串的，若入参是 `0 to N` 方向，生成的二进制串顺序会和「MSB 在左」的人类习惯相反，导致消息里 Expected/Received 看起来错位。归一到 `downto 0` 保证显示顺序统一。这是 Changelog 1.2.0 修过的历史 bug。

---

## 5. 综合实践

把本讲的 5 个过程串起来，模拟一次「读回一个 8 位寄存器并全方位校验」的场景。设计一个**只有 stim 过程、没有 DUT** 的最小 testbench（你用一个 `variable` 模拟「读回的值」即可），完成以下检查链：

1. 用 `IntCompare` 校验「寄存器地址索引」是否正确（带容差，模拟地址译码的微小偏差）。
2. 用 `StdlvCompareInt` 把读回的 8 位向量当成**无符号数**校验其数值（带容差）。
3. 用 `StdlvCompareStdlv` 校验该向量的**某些控制位**（比如最高位是否为 `0`）位精确等于期望。
4. 用 `StdlCompare` 校验一个「数据有效」标志位是否拉高。
5. 用 `RealCompare` 把读回值除以 255.0 归一化成 `[0,1]` 实数后，与期望的归一化值比较（带容差）。

要求：

- 在其中**故意把第 2 步的 `IsSigned` 选错**（传 `true`），观察会报一条「有符号解释」的错误消息，体会 `IsSigned` 误用的危害。
- 在 Transcript 里用 `print` 给每一步加分节标记（参考仓库 [testbench/psi_tb_i2c_pkg_tb.vhd](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd) 的 `print("Single Byte Read");` 风格）。
- 跑完后，对照本讲 4.2.4 / 4.4.4 给出的消息骨架，**手工预测**每一条 `###ERROR###` 的内容，再和 Transcript 实际输出比对。

> 提示：因为没有真实 DUT，所有「实际值」都用 `variable` 赋值模拟；重点是练习「选对过程、选对 `IsSigned`、预测消息」这三件事。若本地无仿真器，可只做「手工预测消息」这一步——把每个调用的参数代入对应源码的 `report` 字符串即可。

## 6. 本讲小结

- `psi_tb_compare_pkg` 的所有过程都遵循同一个骨架：`assert <通过条件> report Prefix & Msg & "[Expected ..., Received ...]" severity error`，`Prefix` 默认 `"###ERROR###: "`，与 CI 的 `run_check_errors "###ERROR###"` 构成契约。
- `assert` 的 `severity error` **只打印不中断**仿真，所以一次跑完能看到全部不匹配项；失败是否被 CI 捕获，靠的是消息里的 `###ERROR###` 子串，而非 severity 级别。
- `IntCompare` / `RealCompare` / `StdlvCompareInt` 支持**容差带** \(A \in [E-T, E+T]\)，适合数值类检查；`StdlCompare` / `StdlvCompareStdlv` 是**位精确**比较，无容差。
- `StdlCompare` 有两个重载（整数期望值 / `std_logic` 期望值），后者是 v2.5.0 加入的便利重载。
- `StdlvCompareInt` 的 `IsSigned` 决定向量如何解释，**选错会误报**；其消息里的十六进制固定是 32 位显示，这是它对 >32 位数据力不从心的根因（催生了下一讲的 `SignCompare2`）。
- 这些过程是 psi_tb 的「公共检查底座」：`activity_pkg` 复用 `StdlCompare`/`StdlvCompareInt`，`axi_pkg` 大量复用 `StdlvCompareStdlv`/`StdlvCompareInt`/`IntCompare`，`SignCompareInt`/`UsignCompareInt` 内部直接转发 `StdlvCompareInt`。

## 7. 下一步学习建议

- 接着学 [u3-l2 signed/unsigned 比较与容差、IndexString](u3-l2-compare-signed-unsigned.md)：那里讲 `SignCompare`、`SignCompare2`（用 `hstr` 解决 >32 位显示）、`UsignCompare`，以及它们如何复用本讲的 `StdlvCompareInt`，还有用于批量比较时标注下标的 `IndexString`。
- 之后进入 [u4 信号活动检查与激励生成](u4-l1-activity-check.md)，看 `CheckNoActivity` / `CheckLastActivity` 如何在内部调用本讲的 `StdlCompare` / `StdlvCompareInt`。
- 再到 [u5 AXI 总线功能模型](u5-l2-axi-single-transactions.md)，看 `axi_single_expect` 如何用 `IntCompare` 校验读回数据、`axi_apply_*` 如何用 `StdlvCompareStdlv`/`StdlCompare` 逐字段校验 AXI 协议。
- 想从设计约定层面理解「为什么全库统一用 `###ERROR###`」，可回看 [u1-l3 仿真环境与 CI 构建流程](u1-l3-simulation-and-ci.md) 的 `run_check_errors` 与 `ciFlow.py` 部分。
