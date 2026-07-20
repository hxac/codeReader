# 位真仿真：用文本文件驱动激励与校验（ApplyTextfileContent / CheckTextfileContent / WriteTextfile）

## 1. 本讲目标

本讲讲解 psi_tb 中专门为「位真（bittrue）仿真」准备的文本文件驱动包 `psi_tb_textfile_pkg`。读完本讲你应当能够：

- 说清「每行一个采样、空格分列、整数取值」这一文件格式约定的由来与写法。
- 用 `ApplyTextfileContent` 把一个文本文件按行施加为 DUT 的激励（带 valid/ready 握手）。
- 用 `CheckTextfileContent` 把 DUT 输出逐行、逐列与期望文件比对，并读懂其 `###ERROR###` 报错信息。
- 用 `WriteTextfile` 把仿真结果连同表头写回磁盘，供 Python/MATLAB 再处理。
- 理解 `ClkPerSpl`、`MaxLines`、`IgnoreLines`、`Tolerance` 等关键参数的语义，以及 `PsiTextfile_SigOne` / `PsiTextfile_SigUnused` 等占位信号的用途。
- 用 Apply + Check 构建一条端到端的「激励→DUT→校验→导出」回路。

## 2. 前置知识

本讲默认你已经学过：

- **u2-l2 文件 I/O 与 print 重载**：std.textio 的 `TEXT` 文件类型、`line` 缓冲指针、`readline`/`writeline`/`read`/`write` 四个原语。本包不使用 `str_read`/`str_write`，而是直接调用 std.textio 的「泛型 `read`」读取整数，原因见 4.2。
- **u2-l1 字符串与数值转换**：本包在错误消息里复用 `hstr`（十六进制）和 `to_string`（整数）。
- **u3 / u4 的统一约定**：所有检查类过程都用 `assert ... report ... severity error`，错误消息以 `###ERROR###` 开头，被 CI 的 `run_check_errors "###ERROR###"` 捕获（见 u1-l3）。本讲的 `CheckTextfileContent` 沿用这一约定。

补充两个本讲要用到的概念：

- **位真仿真（bittrue simulation）**：指对 DSP/信号处理链路做「数值级精确」的仿真——激励和响应都用真实的整数样本（往往代表定点数解码后的整数值），逐采样比对，确保 RTL 与算法模型（Python/MATLAB）完全一致。
- **valid/ready 握手**：与 AXI4-Stream 同源的生产者/消费者协议。生产者抬 `Vld` 表示「数据有效」，消费者抬 `Rdy` 表示「我准备好收」，二者在同一个时钟上升沿同时为 1 时完成一次数据交接。本包的三个过程都围绕这一握手组织。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [hdl/psi_tb_textfile_pkg.vhd](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd) | 本讲核心。定义整数列文本文件的格式约定、`TextfileData_t` 类型、占位信号，以及三个过程 `ApplyTextfileContent` / `CheckTextfileContent` / `WriteTextfile`。 |
| [hdl/psi_tb_txt_util.vhd](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd) | 被引用的底座包，提供错误消息里用到的 `hstr`（[L312-L349](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L312-L349)）与 `to_string`（[L357-L360](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L357-L360)）。 |
| [sim/config.tcl](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/config.tcl) | PsiSim 编译清单。注意：`psi_tb_textfile_pkg.vhd` **不在**其中（见 [config.tcl:L28-L33](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/config.tcl#L28-L33)），本仓库也没有它的 testbench，示例位于外部项目 `psi_fix`（见 Changelog）。 |

> 重要提示：因为该包未进入 CI 编译清单，本仓库内**没有可直接运行的示例**。本讲给出的 testbench 均为「示例代码」，需你自行把它加入 `config.tcl` 的 `-tag src` 列表、注册一个 TB run 后再运行（见综合实践）。运行结果相关的结论一律标注「待本地验证」。

## 4. 核心概念与源码讲解

先看文件头对格式的约定，它是一切的基础：

[hdl/psi_tb_textfile_pkg.vhd:L7-L21](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L7-L21) —— 文件头注释明确：信号值以**整数**表示，一列对应一个信号，列之间用**空格**分隔（**不是逗号**），并直接给出了 Python 生成示例 `np.savetxt("test.txt", np.column_stack((a, b)), fmt="%i")`。

这一点会在 4.2 解释「为什么必须是空格」。

### 4.1 数据类型与占位信号（TextfileData_t / PsiTextfile_SigOne / PsiTextfile_SigUnused）

#### 4.1.1 概念说明

文本文件里的一行有若干列，每一列对应一个信号。为了在过程参数里「一次传递一整行所有列的数据」，需要一个「整数数组」类型；又因为不同 testbench 的列数不同，这个数组必须是**未约束（unconstrained）**的，由调用方在声明信号时指定宽度。

此外，VHDL 有一个语法限制：**过程声明里的 `signal` 形参不能被留空（open）**。但实际工程里，我们常常并不需要 ready 握手（DUT 永远 ready，或消费者永远接收）。为了在不使用握手时也能把过程调用「填满」，包里预定义了几个占位信号，让你传一个「无人关心」的信号进去。

#### 4.1.2 核心流程

类型与占位信号的声明见 [hdl/psi_tb_textfile_pkg.vhd:L41-L49](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L41-L49)：

```text
TextfileData_t          : array(natural range <>) of integer   -- 一行各列的整数值
TextfileName_t          : array(natural range <>) of string    -- WriteTextfile 的列名
PsiTextfile_SigOne      : std_logic := '1'                     -- 恒为 '1'，填给「输入侧不需要 Rdy」的 in 形参
PsiTextfile_SigUnused   : std_logic                            -- 无人驱动的悬空信号，填给「输出侧不需要 Rdy」的 out 形参
PsiTextfile_SigUnusedVec / PsiTextfile_SigUnusedData           -- 向量/数据版的占位信号
```

使用逻辑：

1. 在 testbench 里声明 `signal myData : TextfileData_t(0 to N-1);`，`N` = 列数。
2. 调用 `ApplyTextfileContent` 时，若 DUT 输入侧不回压（永远 ready），把 `Rdy` 形参填成 `PsiTextfile_SigOne`。
3. 调用 `CheckTextfileContent` 时，若不需要把消费者的 ready 反馈给 DUT，把 `Rdy`（out 形参）填成 `PsiTextfile_SigUnused`。

#### 4.1.3 源码精读

[文件路径:L41-L49](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L41-L49) 声明了类型与四个占位信号。注意第 45 行的注释直接点明了设计动机：

> `-- Signal definitions to pass for constant values or unused signals (signals are not allowed to be left open in procedure declarations)`

即这些信号存在的唯一理由，就是绕开 VHDL「signal 形参不可留空」的语法限制。它们本身没有功能意义：`PsiTextfile_SigOne` 恒为 `'1'`，其余几个永远不被任何逻辑读取或驱动。

#### 4.1.4 代码实践

**目标**：掌握 `TextfileData_t` 的声明方式与占位信号的选用。

**步骤**（源码阅读型，无需运行）：

1. 在 [config.tcl:L28-L33](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/config.tcl#L28-L33) 确认 `psi_tb_textfile_pkg.vhd` 不在编译清单里。
2. 阅读声明 [L41-L49](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L41-L49)，记住四个占位信号各自的方向与默认值。
3. 写一个示例声明（示例代码，非项目原有）：

```vhdl
-- 一行有 2 列（两个信号）的激励
signal stimData : TextfileData_t(0 to 1);
```

**需要观察的现象**：`TextfileData_t` 的下标范围由你自定义（`0 to 1` 或 `0 downto ...` 都可），过程内部用 `Data'length` 自适应列数（见 4.2.3 的 `for idx in 0 to Data'length - 1 loop`）。

**预期结果**：能正确说出「输入侧不握手填 `PsiTextfile_SigOne`、输出侧不握手填 `PsiTextfile_SigUnused`」。

#### 4.1.5 小练习与答案

**练习 1**：为什么不能用一个普通的 `integer` 参数代替 `TextfileData_t`？
**答案**：因为一行有多个列、对应多个信号；`integer` 是标量，无法一次承载一整行。`TextfileData_t` 是未约束数组，调用方按列数声明宽度，过程内用 `Data'length` 自适应。

**练习 2**：`PsiTextfile_SigOne` 为什么初始化为 `'1'` 而不是 `'0'`？
**答案**：它专用于填给 `ApplyTextfileContent` 的 `Rdy : in std_logic` 形参（DUT 输入侧的 ready）。填 `'1'` 表示「DUT 永远 ready」，于是过程里的 `wait until rising_edge(Clk) and Rdy='1'` 退化为「等到下一个上升沿」，握手立刻成立。若填 `'0'`，过程会永久挂死。

---

### 4.2 ApplyTextfileContent：把文本文件施加为激励

#### 4.2.1 概念说明

`ApplyTextfileContent` 是**生产者（producer）**：它逐行读入文本文件，把每一行的各列整数装进 `Data` 数组，并按 valid/ready 握手把数据「喂」给 DUT 输入。它负责 DUT **输入侧**的时序：何时给数据、何时抬 `Vld`、何时根据 `Rdy` 节流。

它是位真仿真的「激励源」——你只要用 Python/MATLAB 生成一份整数样本文件，就能驱动整个 RTL 仿真，而不必在 VHDL 里手写一长串 `wait` 和赋值。

#### 4.2.2 核心流程

过程体见 [hdl/psi_tb_textfile_pkg.vhd:L92-L137](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L92-L137)。伪代码如下：

```text
file_open(fp, Filepath, read_mode)
wait until rising_edge(Clk)                       -- 先对齐到时钟
while (未到文件尾) 且 (未超过 MaxLines 或 MaxLines<0):
    readline(fp, ln)
    if lineNr > IgnoreLines:                      -- 跳过头部注释行
        Vld <= '1'
        for idx in 0 to Data'length-1:
            read(ln, Spl)                         -- 用 std.textio 的整数 read
            Data(idx) <= Spl
        wait until rising_edge(Clk) and Rdy='1'   -- 握手
        if ClkPerSpl > 1:                         -- 一个采样跨多个时钟周期
            if DataOnlyOnVld: Data <= (others => 0)   -- Vld 拉低期间清零
            Vld <= '0'
            等待 ClkPerSpl-2 个上升沿             -- 制造采样间隙
    lineNr := lineNr + 1
Vld <= '0'
file_close(fp)
```

握手时序（以一个采样为单位）：

| ClkPerSpl | Vld 抬高周期数 | 间隙（Vld=0）周期数 | 每采样总周期数 |
| --- | --- | --- | --- |
| 1 | 每拍都为 1 | 0 | 1（每时钟一个采样） |
| 3 | 1 | 2 | 3（每 3 拍一个采样） |

即 `ClkPerSpl` 就是「一个采样占据多少个时钟周期」，对应一个降采样/多周期处理的 DUT。

**为什么列必须用空格分隔？** 因为过程用 std.textio 的泛型 `read`（[L117](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L117) `read(ln, Spl)`，`Spl: integer`）解析整数。std.textio 的整数 `read` 会跳过前导空白、读取可选符号与数字、在第一个非数字字符处停止——它**依赖空白作为列分隔符**。若是逗号分隔，逗号会残留在行缓冲里，导致后续列读取失败。这正是 u2-l2 里 `str_read`（逐字符读原始文本）与本处「读整数」的根本差异：本包不需要 `str_read`，因为它要的是数值而非字符串。

#### 4.2.3 源码精读

- [L107](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L107) `file_open(fp, Filepath, read_mode)`：以只读方式打开激励文件。
- [L111](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L111) 主循环条件：`(not endfile(fp)) and ((lineNr <= MaxLines) or (MaxLines < 0))`。`MaxLines` 默认 `-1` 表示「读到文件尾」；给正数则只读前若干行。
- [L114-L119](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L114-L119) 抬高 `Vld`、逐列 `read` 并装入 `Data` 数组。
- [L120](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L120) `wait until rising_edge(Clk) and Rdy = '1'`：valid/ready 握手点。DUT 通过 `Rdy` 回压时，这里会等待。
- [L121-L129](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L121-L129) `ClkPerSpl > 1` 分支：拉低 `Vld` 并等待 `ClkPerSpl-2` 个上升沿补足采样周期；`DataOnlyOnVld = true` 时还会在 `Vld` 低期间把 `Data` 清零（避免 DUT 误用陈旧数据，对应 Changelog「added option to invalidate data when Vld low」）。

#### 4.2.4 代码实践

**目标**：用 Python 生成一份两列激励文件，用 `ApplyTextfileContent` 施加，观察波形里 `Vld`/`Data` 的时序。

**步骤**：

1. 用 Python 生成激励文件 `stim.txt`（示例代码）：

```python
import numpy as np
a = np.linspace(100, 200, 10)      # 第 1 列
b = np.linspace(-50, 40, 10)       # 第 2 列
np.savetxt("stim.txt", np.column_stack((a, b)), fmt="%i")
```

生成的文件形如（空格分列、整数取值）：

```text
100 -50
111 -40
122 -30
...
```

2. 在 testbench 里声明信号并调用过程（示例代码）：

```vhdl
signal Clk      : std_logic := '0';
signal inVld    : std_logic;
signal inData   : TextfileData_t(0 to 1);   -- 两列
...
-- DUT 输入侧不回压，Rdy 填 PsiTextfile_SigOne；每采样 1 拍
ApplyTextfileContent(
    Clk  => Clk,
    Rdy  => PsiTextfile_SigOne,
    Vld  => inVld,
    Data => inData,
    Filepath => "stim.txt",
    ClkPerSpl => 1);
```

3. 把 `psi_tb_textfile_pkg.vhd` 加入 `config.tcl` 的 `-tag src` 列表后用 `run.tcl` 运行。

**需要观察的现象**：`inVld` 在每个上升沿为 `1`，`inData(0)`/`inData(1)` 依次取 `100,-50`、`111,-40`、…；文件读完后 `inVld` 归零。

**预期结果**：波形中 `inData` 随时间呈现与文件一致的整数序列。**待本地验证**（取决于仿真器对相对路径的解析，必要时用绝对路径）。

#### 4.2.5 小练习与答案

**练习 1**：把 `ClkPerSpl` 从 1 改成 3，`inVld` 的波形会发生什么变化？
**答案**：`inVld` 变成「每 3 个时钟周期里只有第 1 拍为 1，其余 2 拍为 0」，即每 3 拍送出一个采样；`inData` 每 3 拍更新一次（若 `DataOnlyOnVld=true`，则 `Vld=0` 期间 `inData` 被清零）。

**练习 2**：如果误把 `stim.txt` 写成逗号分隔（`100,-50`），会发生什么？
**答案**：第 1 列 `read(ln, Spl)` 读到 `100` 后停在逗号处；第 2 列再次 `read` 时，行缓冲下一个字符是逗号而非空白，整数 `read` 失败，`Spl` 保持上次值（或仿真器默认 0），导致第 2 列及之后全部错位。这就是格式必须是空格分隔的直接原因。

**练习 3**：`IgnoreLines` 有什么用？
**答案**：跳过文件头部的若干行（如注释行、列名行）。`lineNr <= IgnoreLines` 的行只 `readline` 丢弃、不施加，从 `lineNr > IgnoreLines` 起才作为有效采样输出。

---

### 4.3 CheckTextfileContent：逐列比对 DUT 输出

#### 4.3.1 概念说明

`CheckTextfileContent` 是**消费者（consumer）兼校验者**：它在 DUT **输出侧**抬 `Rdy`，等待 DUT 抬 `Vld`，握手成功后把 DUT 送来的 `Data` 与期望文件里对应行列的整数比对，不匹配则打印 `###ERROR###` 诊断信息。

它与 `ApplyTextfileContent` 是镜像关系：一个驱动输入、一个校验输出，各自管理自己那一侧的握手。两者配合即可构成「激励→DUT→校验」的完整回路。

#### 4.3.2 核心流程

过程体见 [hdl/psi_tb_textfile_pkg.vhd:L140-L199](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L140-L199)。伪代码：

```text
file_open(fp, Filepath, read_mode)
while (未到文件尾) 且 (未超过 MaxLines 或 MaxLines<0):
    readline(fp, ln)
    if lineNr > IgnoreLines:
        Rdy <= '1'
        wait until rising_edge(Clk) and Vld='1'    -- 等待 DUT 给出有效样本
        colNr := 0
        for idx in 0 to Data'length-1:
            read(ln, Spl_期望)
            Sig_实际 := Data(idx)
            assert abs(Sig_实际 - Spl_期望) <= Tolerance
                report ErrorPrefix & ": Wrong Sample, line=.. column=.." & 期望 & 实际
                severity error
            colNr := colNr + 1
        if ClkPerSpl > 1:
            Rdy <= '0'
            等待 ClkPerSpl-2 个上升沿
    lineNr := lineNr + 1
Rdy <= '0'
file_close(fp)
```

容差判定为：

\[
\text{Received} \in [\,\text{Expected}-T,\ \text{Expected}+T\,]
\]

即 `abs(Received - Expected) <= Tolerance` 才算通过。`Tolerance` 默认 0（精确相等）。

#### 4.3.3 源码精读

- [L167-L168](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L167-L168) 抬高 `Rdy` 并在握手点 `wait until rising_edge(Clk) and Vld='1'` 等待 DUT 输出有效样本。
- [L172-L181](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L172-L181) 逐列读取期望值 `Spl`、取实际值 `Sig := Data(idx)`，做容差比较。
- [L175-L179](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L175-L179) 是关键断言：

```vhdl
assert abs(Sig - Spl) <= Tolerance
report ErrorPrefix & ": Wrong Sample, line=" & integer'image(lineNr) &
       " column=" & integer'image(colNr) & LF &
       " --> Expected " & integer'image(Spl)  & " [0x" & hstr(std_logic_vector(to_signed(Spl, 32))) & "]" & LF &
       " --> Received " & integer'image(Sig)  & " [0x" & hstr(std_logic_vector(to_signed(Sig, 32))) & "]"
severity error;
```

要点：

1. `ErrorPrefix` 默认 `"###ERROR###"`（[L69](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L69) 声明处），与 CI 的 `run_check_errors "###ERROR###"` 契约一致——一次失配即自动判 CI 失败。
2. 消息里同时给出十进制（`integer'image`）与 32 位十六进制（`hstr(std_logic_vector(to_signed(..., 32)))`），方便对照波形里的二进制电平。注意十六进制固定为 **32 位**（与 u3 讲到的 `integer` 32 位天花板同源），所以文本文件里的整数值不能超过 32 位有符号整数范围 `[-2^31, 2^31-1]`。
3. `severity error` 只打印、不中断仿真，与 u3/u4 全库一致——一次跑完能看到所有失配点。

#### 4.3.4 代码实践

**目标**：故意制造一次失配，观察 `CheckTextfileContent` 的报错格式。

**步骤**：

1. 准备一份「期望文件」`expected.txt`，内容与 DUT 实际输出**故意有 1 处不同**，例如 DUT 实际送 `122` 而期望文件写 `999`。
2. 在 DUT 输出侧调用（示例代码）：

```vhdl
signal outVld  : std_logic;
signal outData : TextfileData_t(0 to 1);
...
CheckTextfileContent(
    Clk    => Clk,
    Rdy    => PsiTextfile_SigUnused,   -- 不把消费者 ready 反馈给 DUT
    Vld    => outVld,
    Data   => outData,
    Filepath => "expected.txt",
    ClkPerSpl => 1,
    Tolerance => 0);
```

3. 运行仿真，查看 Transcript。

**需要观察的现象**：Transcript 中出现以 `###ERROR###: Wrong Sample, line=N column=K` 开头的三行消息，给出 Expected/Received 的十进制与十六进制。

**预期结果**：`line`、`column` 指向失配位置；`Received` 反映 DUT 实际值（`122`），`Expected` 反映期望值（`999`）。**待本地验证**（具体行号列号取决于你设置失配的位置）。

#### 4.3.5 小练习与答案

**练习 1**：把 `Tolerance` 设为 10，期望值 `120`、实际值 `125`，会不会报错？
**答案**：不会。`abs(125 - 120) = 5 <= 10`，落在容差带 \([110, 130]\) 内，断言通过、不打印。

**练习 2**：为什么说 `CheckTextfileContent` 与 `ApplyTextfileContent` 是「镜像」？
**答案**：二者都按 valid/ready 握手推进，但驱动方向相反——Apply 是生产者，`Vld` 为 out、`Rdy` 为 in；Check 是消费者，`Rdy` 为 out、`Vld` 为 in。它们各自管理 DUT 一侧的握手，时序参数 `ClkPerSpl` 也须各自匹配该侧的实际采样率。

**练习 3**：消息里的十六进制为什么固定 32 位？对一个超过 32 位的数据会怎样？
**答案**：因为消息用 `to_signed(Value, 32)` 再 `hstr`。整数值本身受 VHDL `integer` 是 32 位有符号的约束（也是 v3.0.0 把数据格式改为 integer 以支持 GHDL 的副作用）；超过 32 位范围的值在文件里就无法正确表达，这是该包的固有限制。

---

### 4.4 WriteTextfile：把仿真结果写回磁盘

#### 4.4.1 概念说明

`WriteTextfile` 把 DUT 输出（或仿真中任一 `TextfileData_t` 信号）连同列名表头写回磁盘文件，供 Python/MATLAB 离线分析或绘图。它是位真仿真的「出口」：与 Apply（入口）、Check（在线校验）互补，当你想保留全量样本做后处理时使用。

源码注释（[L201-L206](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L201-L206)）说明了它的历史：早期版本依赖 `psi_fix`，会在第 2 行打印定点格式、并用 `PsiFixtoReal` 显示实数；为消除对 `psi_fix` 的强依赖，本包被改成「只写整数」的纯文本版本。

#### 4.4.2 核心流程

过程体见 [hdl/psi_tb_textfile_pkg.vhd:L208-L282](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L208-L282)。伪代码：

```text
while lineNr <= nb_data + 2:
    wait until rising_edge(Clk) and Vld='1'        -- 等 DUT 给出有效样本
    if 文件未打开: file_open(fp, Filepath, WRITE_MODE)   -- 懒打开
    if lineNr == 1:                                 -- 第 1 行：表头
        写出 Name(0..N-1)，用 spacer 连接
        若 time_sim=true: 末尾追加 "time_simulation"
    else:                                            -- 数据行
        写出 to_string(Data(0..N-1))，用 spacer 连接
        若 time_sim=true: 末尾追加 now（仿真时刻）
    lineNr := lineNr + 1
file_close(fp)
```

三个要特别注意的细节：

1. **懒打开**：文件在第一个 `Vld='1'` 到来时才创建（[L226-L228](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L226-L228)），避免在 DUT 还未产出数据时就建空文件。
2. **spacer 默认是 `" , "`（逗号）**（[L81](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L81)），与 Apply/Check 要求的「空格分隔」**不一致**。若你想把本过程写出的文件再喂回 `CheckTextfileContent`（做循环比对），**必须**把 `spacer` 显式覆盖为 `" "`（纯空格），否则逗号会让整数 `read` 解析失败（见 4.2.2）。
3. **循环边界 `nb_data + 2`**（[L224](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L224)）：经源码追踪，实际写出 **1 行表头 + (nb_data+1) 行数据**（`nb_data+2` 是早期「表头 2 行」版本的遗留计数，第 2 行定点格式已被移除但计数未改）。若要精确控制数据行数，**待本地验证**后据此换算 `nb_data`。

`time_sim` 参数：`true`（默认）时每行末尾追加仿真时刻 `now`、表头加一列 `time_simulation`；`false` 时只写数据列。`Name` 是 `TextfileName_t`（字符串数组），给每一列取名。

#### 4.4.3 源码精读

- [L217-L221](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L217-L221) 变量：`file_status` 初值 `MODE_ERROR`（用于懒打开判定），`time_simu` 常量字符串 `"time_simulation"`。
- [L224-L228](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L224-L228) 循环与懒打开。
- [L230-L253](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L230-L253) 表头行：根据 `time_sim` 决定是否追加 `time_simulation` 列。
- [L254-L278](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L254-L278) 数据行：用 `to_string(Data(j))`（[txt_util:L357-L360](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L357-L360) 的整数重载）转成十进制字符串，`time_sim=true` 时末尾用 `write(ln, now)` 写仿真时刻。
- [L281](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L281) 循环结束后 `file_close`。

注意默认 `Filepath` 是 `"/data/processing_data.txt"`（[L82](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L82)）——一个 Linux 绝对路径，多数机器上不存在，实际使用时**务必显式传入**自己的路径。

#### 4.4.4 代码实践

**目标**：用 `WriteTextfile` 导出 DUT 输出，并对比不同 `spacer` 写出文件的差异。

**步骤**：

1. 在 DUT 输出侧（与 4.3 同一组 `outVld`/`outData`）调用（示例代码）：

```vhdl
-- 为支持「写出后再喂回 Check」，spacer 显式用空格
WriteTextfile(
    Clk      => Clk,
    Vld      => outVld,
    Data     => outData,
    nb_data  => 9,
    time_sim => false,
    Name     => ("col0", "col1"),     -- TextfileName_t，与 Data 列数一致
    spacer   => " ",
    Filepath => "out.txt");
```

2. 运行后用文本编辑器打开 `out.txt`。
3. 把 `spacer` 改回默认 `" , "`、`time_sim` 改为 `true`，再跑一次，对比文件内容。

**需要观察的现象**：
- `spacer => " "`、`time_sim => false`：第 1 行是列名（空格分隔），其后是纯整数数据（空格分隔），可被 `CheckTextfileContent` 直接读取。
- 默认 `spacer => " , "`、`time_sim => true`：每行末尾多一列仿真时刻，列间用逗号，适合人眼阅读或 Python `np.loadtxt(delimiter=',')` 处理。

**预期结果**：两种 spacer 产出的文件结构如上。**待本地验证**数据行数（见 4.4.2 第 3 点关于 `nb_data+2` 的说明）。

#### 4.4.5 小练习与答案

**练习 1**：为什么说「把 `WriteTextfile` 的输出直接喂回 `CheckTextfileContent`」需要小心 spacer？
**答案**：`WriteTextfile` 默认 `spacer=" , "`（逗号），而 `CheckTextfileContent` 用 std.textio 整数 `read` 解析、要求空格分隔。逗号会卡住整数解析。循环比对时必须把 `spacer` 覆盖为 `" "`。

**练习 2**：`file_status` 初值设为 `MODE_ERROR` 起什么作用？
**答案**：它让 `if file_status /= OPEN_OK` 在第一次有效样本到来时为真，从而触发 `file_open(WRITE_MODE)`，实现「懒打开」——避免在 DUT 还没产出数据时就创建文件，也保证文件只在真正有数据时才落盘。

**练习 3**：`time_sim=true` 时，每行末尾多出来的那一列是什么？
**答案**：当前仿真时刻 `now`（VHDL 内建 `time` 类型，由 `write(ln, now)` 输出），表头对应列为 `"time_simulation"`。便于把仿真结果按时间轴绘图。

---

## 5. 综合实践

把 4.2 ~ 4.4 串成一条端到端回路：用 Python 生成两列整数激励 → `ApplyTextfileContent` 施加 → 一个最小 DUT → `CheckTextfileContent` 校验 → `WriteTextfile` 导出 → 再把导出文件喂回 Check 做循环比对。

**目标**：完整跑通「激励→DUT→校验→导出→再校验」，确认 Transcript 中无 `###ERROR###`。

**步骤**：

1. 用 Python 生成激励 `stim.txt`（示例代码）：

```python
import numpy as np
a = np.linspace(0, 90, 10)
b = np.linspace(100, 10, 10)
np.savetxt("stim.txt", np.column_stack((a, b)), fmt="%i")
```

2. 写一个最小「寄存一拍」的 passthrough DUT（示例代码），避免组合直通的 delta-cycle 时序歧义：

```vhdl
process(Clk) is
begin
    if rising_edge(Clk) then
        outVld  <= inVld;
        outData <= inData;          -- 原样转发，便于用同一份文件做期望
    end if;
end process;
```

3. 在 testbench 里用两个并发 process 分别施加与校验（示例代码）：

```vhdl
-- 激励侧（生产者）
ApplyTextfileContent(Clk => Clk, Rdy => PsiTextfile_SigOne,
    Vld => inVld, Data => inData, Filepath => "stim.txt", ClkPerSpl => 1);

-- 校验侧（消费者）：用同一份 stim.txt 当期望，验证 passthrough
CheckTextfileContent(Clk => Clk, Rdy => PsiTextfile_SigUnused,
    Vld => outVld, Data => outData, Filepath => "stim.txt",
    ClkPerSpl => 1, Tolerance => 0);

-- 导出侧：spacer 用空格，便于回喂
WriteTextfile(Clk => Clk, Vld => outVld, Data => outData,
    nb_data => 9, time_sim => false, Name => ("col0", "col1"),
    spacer => " ", Filepath => "out.txt");
```

4. 把 `psi_tb_textfile_pkg.vhd` 与本 TB 加入 `sim/config.tcl`（`-tag src` 加包、`-tag tb` 加 TB、`create_tb_run`/`add_tb_run` 注册运行），用 `sim/run.tcl`（ModelSim）或 `sim/runGhdl.tcl`（GHDL）跑仿真。
5. 仿真结束后，把 `out.txt` 复制为 `roundtrip_expected.txt`，改写第二个 TB 用它做期望文件，再次 `CheckTextfileContent`，验证导出值与原始一致。

**需要观察的现象**：

- 第一次仿真 Transcript 中**不出现** `###ERROR###`（passthrough，期望=实际）。
- `out.txt` 第 1 行为 `col0 col1`，其后为与 `stim.txt` 一致（因寄存一拍而整体延后一个采样）的整数行。
- 第二次「回喂」仿真同样无 `###ERROR###`。

**预期结果**：端到端回路成立，两轮仿真均无错误；`out.txt` 内容可被 `CheckTextfileContent` 正确解析（因为 `spacer => " "`）。**待本地验证**：寄存一拍带来的样本对齐、`nb_data+2` 实际写出的数据行数、以及你的仿真器对相对路径的处理（必要时改用绝对路径）。

**排错提示**：若出现 `###ERROR###: Wrong Sample` 且整体偏移一个采样，多半是 passthrough 的寄存延迟导致期望与实际错位——可把期望文件也相应延迟一行，或改用组合直通 DUT 并接受 delta-cycle 风险；这类时序对齐属于本包使用时的常见调试点。

## 6. 本讲小结

- `psi_tb_textfile_pkg` 服务于位真仿真，约定文件为「每行一个采样、空格分列、整数取值」；这一格式由 std.textio 的整数 `read` 决定（空格是天然分隔符），Python 用 `np.savetxt(..., fmt="%i")` 即可生成。
- `TextfileData_t`（未约束整数数组）承载一行的各列；`PsiTextfile_SigOne`/`PsiTextfile_SigUnused` 等占位信号用于绕开 VHDL「signal 形参不可留空」的限制，在不需要 ready 握手时填入。
- `ApplyTextfileContent` 是输入侧生产者，按 valid/ready 握手把文件内容喂给 DUT，`ClkPerSpl` 控制每采样占多少时钟周期，`IgnoreLines`/`MaxLines` 控制读取范围。
- `CheckTextfileContent` 是输出侧消费者兼校验者，握手后逐列做 `abs(Received-Expected) <= Tolerance` 容差比较，失配时打印带十进制与 32 位十六进制的 `###ERROR###` 消息，自动联动 CI 判定。
- `WriteTextfile` 是出口，懒打开文件、写表头与数据行，支持 `time_sim` 追加仿真时刻；注意默认 spacer 是逗号、与输入侧空格约定不一致，循环比对时须覆盖为空格。
- 该包未进入 `config.tcl` 的 CI 编译清单、本仓库内无示例 TB（示例在外部 `psi_fix` 项目），整数取值受 32 位有符号范围限制——这些是使用时必须知道的前提。

## 7. 下一步学习建议

- **横向对比 AXI BFM**（u5）：本讲的 valid/ready 握手与 AXI4-Stream 同源；学完 u5 的 `axi_single_*`/`axi_apply_*` 后，可对比「文本文件驱动」与「过程调用驱动」两种施加激励方式的取舍。
- **深入 I2C BFM 实战**（u7）：u7-l4 的 `psi_tb_i2c_pkg_tb` 是本仓库内唯一完整的多进程对拍 testbench 范例，可作为你为本包编写 testbench 时的结构参考（master/slave 双 process、`print` 分节、`###ERROR###` 自检）。
- **二次开发**（u8-l2）：若你的数据超过 32 位或需要定点/实数格式，可仿照本包的约定（统一前缀、`assert ... severity error`、`TextfileData_t` 思路）扩展一个支持 `signed`/`real` 的文本驱动包；也可参考源码注释（[L201-L206](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L201-L206)）了解它从 `psi_fix` 依赖中解耦的历史，理解「为消除强依赖而简化数据格式」的设计动机。
