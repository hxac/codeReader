# ApplyTextfileContent / CheckTextfileContent / WriteTextfile

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清「整数列文本文件」这一位真（bit-true）仿真约定的具体格式：每行一个采样、空格分列、值用整数表示。
- 独立调用 `ApplyTextfileContent` 把一个文本文件逐行施加为时钟同步的激励（`Vld`/`Data`）。
- 独立调用 `CheckTextfileContent` 把 DUT 输出与期望文件逐列比对，并理解 `Tolerance`、`IgnoreLines`、`ErrorPrefix` 的作用。
- 用 `WriteTextfile` 把仿真结果连同表头写回文件，供 Python/MATLAB 后处理，并知道它默认输出为什么不能直接被 `Apply`/`Check` 读回。
- 理解 `PsiTextfile_SigOne` / `PsiTextfile_SigUnused` 这两个辅助信号存在的根本原因（VHDL 过程的 `signal` 形参不能留空），并能正确选用。

---

## 2. 前置知识

本讲假定你已经掌握：

- **testbench 不可综合**（见 u1-l1）。因此本包大量使用 `file` I/O、`wait until rising_edge(Clk)`、动态数组等只能在仿真中用的语言特性，这些都不会进 FPGA。
- **`std.textio` 的三件套**（见 u2-l2）：文件类型 `TEXT`、行缓冲指针 `line`、以及 `readline` / `writeline` / `read` / `write` 四个原语。本讲的所有文件读写都建立在它们之上。
- **`###ERROR###` 前缀契约**（见 u1-l3、u3-l1）：所有自检失败都打印以 `###ERROR###` 开头的消息，CI 末尾用 `run_check_errors "###ERROR###"` 扫描它。`CheckTextfileContent` 的默认 `ErrorPrefix` 就是 `"###ERROR###"`。
- **`hstr` 与 32 位十六进制显示**（见 u2-l1、u3-l1）：`CheckTextfileContent` 的错误消息会把整数经 `to_signed(x, 32)` 转成 32 位再 `hstr`，所以消息里的十六进制固定 8 位。
- **valid/ready 握手**：本讲三个过程都是围绕一对 `Vld`/`Rdy` 握手信号建模的，理解「生产者驱动 Vld、消费者驱动 Rdy、二者同时为 1 才成交」是关键。

> 名词速查：
> - **位真仿真（bit-true simulation）**：激励和期望都用真实数值（这里是整数）描述，逐采样比对，常用于数字信号处理链路的回归测试。
> - **采样（sample）**：文本文件中的一行，对应仿真时间上的一个数据点。
> - **列（column）**：一行中空格分隔的一个整数，对应一个信号通道。

---

## 3. 本讲源码地图

本讲只涉及一个核心源文件，外加它依赖的文本工具包：

| 文件 | 作用 | 本讲用到的东西 |
| --- | --- | --- |
| `hdl/psi_tb_textfile_pkg.vhd` | 文本文件驱动的位真仿真包，全部内容只此一文件 | `TextfileData_t`、辅助信号、`ApplyTextfileContent`、`CheckTextfileContent`、`WriteTextfile` |
| `hdl/psi_tb_txt_util.vhd` | 文本/数值转换底座（u2 已详解） | `hstr`（错误消息的十六进制显示）、`to_string(integer)`（写文件时的整数转字符串） |

> 编译提示：`psi_tb_textfile_pkg.vhd` **目前并不在** `sim/config.tcl` 的 `add_sources -tag src` 编译清单里（该清单只有 `txt_util`、`compare`、`activity`、`i2c` 四个包）。因此本包没有进 CI，也没有配套 testbench。要实际运行本讲的代码实践，你必须先把它加进编译清单（见第 4.2 节实践步骤）。这一点与 u1-l2 所述「AXI 与 textfile 因无注册 testbench 而未进 CI」一致。

永久链接基址（当前 HEAD `8ee9c06`）：

```
https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/
```

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，按「先看公共约定与类型，再依次走激励施加 → 输出比对 → 结果写回」的顺序。

### 4.1 文本文件整数格式约定、TextfileData_t 类型与辅助信号

#### 4.1.1 概念说明

很多 DSP 类的 DUT（滤波器、上下变频、控制环路……）需要用大量真实数值去做回归测试。手写 `wait` + 赋值的方式很快就会变得不可维护。`psi_tb_textfile_pkg` 给出一个极简约定：**把激励和期望都写成纯整数文本文件，一行一个采样，一列一个信号通道，列之间用空格分隔**。

包头的注释把这一约定写得非常明确，并直接给出了 Python 生成示例：

> [hdl/psi_tb_textfile_pkg.vhd:L7-L21](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L7-L21) — 文件头注释：约定「整数、每行一个采样、空格分列（**不是逗号**）」，并给出 `np.savetxt(..., fmt="%i")` 的生成范例。

一个合法的输入文件长这样（两列信号）：

```text
117 124 -45
111 -123 88
```

注意三点：

1. **分隔符是空格，不是逗号**。原因在 4.1.2 解释。
2. **允许负数**。因为下面用 `std.textio` 的整数 `read` 读取，它认负号。
3. **值是整数**。所以一条列就是一个 `integer`，范围受 VHDL `integer`（32 位有符号）限制：\(-2^{31} \sim 2^{31}-1\)。定点/浮点数据必须先在 Python 里量化成整数再写入。

#### 4.1.2 核心流程

为什么必须用空格？因为读取靠的是 `std.textio` 的整数 `read`：

- 整数 `read` 的行为是：跳过前导空白，读取可选正负号，再连续读数字，**遇到第一个非数字字符就停**。
- 如果列之间是空格，下一次 `read` 会跳过空格继续读下一列，正确。
- 如果列之间是逗号（如 `117,124`），第一次 `read` 读到 `117` 停在逗号前；第二次 `read` 一上来就是逗号（非空白、非数字），`is_string` 返回 `false`，读取失败、值未定义。

所以「空格分列」不是风格偏好，而是与 `read` 语义匹配的硬性要求。这条约束在本讲第 4.4 节会再次出现——`WriteTextfile` 默认 `spacer` 是 `" , "`（带逗号），它的输出因此**不能**直接被 `Apply`/`Check` 读回。

#### 4.1.3 源码精读

公共类型定义在包头：

> [hdl/psi_tb_textfile_pkg.vhd:L41-L43](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L41-L43) — `TextfileData_t` 是 `integer` 的非约束数组（一列一个元素）；`TextfileName_t` 是 `string` 的非约束数组，给 `WriteTextfile` 当列名。

```vhdl
type TextfileData_t  is array (natural range <>) of integer;
type TextfileName_t  is array (natural range <>) of string;
```

调用方声明 `signal Data : TextfileData_t(0 to 1)` 就得到一个两列容器，宽度在调用端决定、过程内部用 `Data'length` 自适应——这和 u5-l1 里 AXI 记录「未约束字段、使用端定宽」的思路一致。

接下来是本模块的关键：四个**包级辅助信号**。

> [hdl/psi_tb_textfile_pkg.vhd:L45-L49](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L45-L49) — 辅助信号定义。注释一句话点明了它们的用途：「为常量值或不用信号提供的占位——过程声明里的 signal 形参不允许留空」。

```vhdl
signal PsiTextfile_SigOne       : std_logic := '1';        -- 恒为 '1'，给 Rdy 当「永远就绪」
signal PsiTextfile_SigUnused    : std_logic;               -- 无初值，给 Rdy 当「丢弃」出口
signal PsiTextfile_SigUnusedVec : std_logic_vector(0 downto 0);
signal PsiTextfile_SigUnusedData: TextfileData_t(0 downto 0);
```

它们存在的根本原因是 **VHDL 语法**：在过程声明里，`signal xxx : in/out ...` 形参**不允许像普通 `in` 形参那样用默认值留空**，调用时也**不能省略**。可本包的三个过程为了通用性都带了 `Rdy` 这类握手形参——如果你根本不关心它，仍必须传「某个真实信号」进去。于是包里预先声明好这些占位信号：

- 想让某个 `in` 握手信号恒为某电平 → 传 `PsiTextfile_SigOne`（恒 `'1'`）。
- 想丢弃某个 `out` 信号 → 传 `PsiTextfile_SigUnused`（当垃圾桶）。

具体到本包：
- `ApplyTextfileContent` 的 `Rdy : in std_logic`（下游是否就绪）：DUT 输入永不反压时传 `PsiTextfile_SigOne`。
- `CheckTextfileContent` 的 `Rdy : out std_logic`（本检查器是否就绪）：不想接回这个信号时传 `PsiTextfile_SigUnused`。

> 注意：这些信号声明在**包**里（不是包体），所以你只要 `use work.psi_tb_textfile_pkg.all;` 就能直接引用它们。包级信号在 VHDL 里是静态、全局可见的，这正是「占位信号」这一惯用法的载体。

#### 4.1.4 代码实践（源码阅读型）

**目标**：确认你对类型与辅助信号的用法已经清楚，无需运行仿真。

1. 打开 [hdl/psi_tb_textfile_pkg.vhd:L41-L49](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L41-L49)。
2. 对照三个过程的形参表（[L52-L60](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L52-L60)、[L63-L72](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L63-L72)、[L75-L82](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L75-L82)），逐个标出哪些形参是 `signal ... in`、哪些是 `signal ... out`。
3. 在纸上画一张表：每个 `signal` 形参「不用时该传哪个辅助信号」。

**预期结果**：你会得到一张形如「`Apply.Rdy(in) → PsiTextfile_SigOne`」「`Check.Rdy(out) → PsiTextfile_SigUnused`」的对照表，后续两节直接照填即可。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `PsiTextfile_SigOne` 要给初值 `'1'`，而 `PsiTextfile_SigUnused` 不给初值？

**答案**：`SigOne` 的职责是「永远呈现就绪电平」，必须一上电就是 `'1'` 才能起到「`Apply` 的 `Rdy` 永远满足」的作用；而 `SigUnused` 是个垃圾桶，过程会把驱动值送进来、调用方根本不读，有没有初值无所谓，所以省略。

**练习 2**：一份文本文件内容是 `3,7,11`（逗号分隔），用 `ApplyTextfileContent` 读取一列会发生什么？

**答案**：第一次 `read` 跳过前导空白读到 `3`，停在逗号前；第二次 `read` 直接撞上逗号（非空白非数字），`is_string` 为 `false`，读取失败，对应列的值未定义（很可能是 `integer'left`）。这就是包注释强调「空格而非逗号」的原因。

**练习 3**：`TextfileData_t(0 to 1)` 能装下多大范围的数？

**答案**：它是 `array of integer`，每个元素都是 VHDL `integer`，即 32 位有符号，范围 \(-2^{31} \sim 2^{31}-1\)，约 ±21.4 亿。超出该范围的定点数据必须先在 Python 里拆分或缩放。

---

### 4.2 ApplyTextfileContent — 从文件施加激励

#### 4.2.1 概念说明

`ApplyTextfileContent` 扮演的是**激励生产者**：它打开一个文本文件，逐行读出整数，按列填进 `Data` 数组，并在每个采样点抬一次 `Vld`，把数据「喂」给 DUT 输入。它和 DUT 之间通过一对握手信号交互：

- `Vld : out std_logic`（过程驱动）：本采样有效。
- `Data : out TextfileData_t`（过程驱动）：各列的整数激励。
- `Rdy : in std_logic`（DUT 驱动）：下游是否准备好接收。若你不关心，传 `PsiTextfile_SigOne`。

#### 4.2.2 核心流程

过程主体是一个「读一行 → 等握手 → 按节拍扩展」的循环，伪代码如下：

```text
file_open(fp, Filepath, read_mode)
wait until rising_edge(Clk)                # 先对齐到时钟
while not endfile(fp) 且 (MaxLines 未达上限):
    readline(fp, ln)
    if lineNr > IgnoreLines:               # 跳过文件头若干行
        Vld <= '1'
        for idx in 0..Data'length-1:       # 逐列读整数
            read(ln, Spl); Data(idx) <= Spl
        wait until rising_edge(Clk) and Rdy='1'   # 握手成交
        if ClkPerSpl > 1:                  # 一个采样占多个时钟周期
            if DataOnlyOnVld: Data <= 全 0
            Vld <= '0'
            for i in 0..ClkPerSpl-2: wait until rising_edge(Clk)
    lineNr += 1
Vld <= '0'
file_close(fp)
```

四个参数的语义：

- **`ClkPerSpl`**（默认 1）：一个采样持续多少个时钟周期。`=1` 时每个时钟吐一个采样；`>1` 时吐一个有效拍后 `Vld` 拉低 `ClkPerSpl-1` 拍，即降低采样率。
- **`MaxLines`**（默认 -1）：最多读多少行；`-1` 表示读到文件尾。
- **`IgnoreLines`**（默认 0）：跳过文件开头多少行（常用于跳过注释行/表头）。
- **`DataOnlyOnVld`**（默认 false）：仅当 `ClkPerSpl>1` 时有意义。`true` 表示无效拍里把 `Data` 清零；`false` 表示无效拍里保持上一个采样值。对「连续采样的组合逻辑」型 DUT 用 `true` 更安全，避免残留值串扰。

握手时机要点：`wait until rising_edge(Clk) and Rdy='1'` 意味着**只有当下游就绪的那个上升沿**才视为成交；若 `Rdy` 为 0，过程会原地等待，不消耗文件行——这保证采样不会被丢。

#### 4.2.3 源码精读

声明（注意所有信号形参都不能留空，故有 4.1 节的辅助信号）：

> [hdl/psi_tb_textfile_pkg.vhd:L52-L60](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L52-L60) — `ApplyTextfileContent` 形参表：`Clk/Rdy` 输入、`Vld/Data` 输出，外加 4 个有默认值的普通 `in` 参数。

实现里两段最关键。第一段：**逐列读取**：

> [hdl/psi_tb_textfile_pkg.vhd:L114-L120](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L114-L120) — 抬 `Vld`，用 `for idx in 0 to Data'length-1` 按列 `read(ln, Spl)` 并赋给 `Data(idx)`，随后 `wait until rising_edge(Clk) and Rdy='1'` 完成握手。

```vhdl
if lineNr > IgnoreLines then
   Vld <= '1';
   for idx in 0 to Data'length - 1 loop
      read(ln, Spl);
      Data(idx) <= Spl;
   end loop;
   wait until rising_edge(Clk) and Rdy = '1';
```

第二段：**降采样时的节拍扩展**：

> [hdl/psi_tb_textfile_pkg.vhd:L121-L129](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L121-L129) — `ClkPerSpl>1` 时，握手后按 `DataOnlyOnVld` 决定是否清零 `Data`，再拉低 `Vld` 并空等 `ClkPerSpl-2` 个上升沿（连同成交那一拍共 `ClkPerSpl` 拍）。

```vhdl
if ClkPerSpl > 1 then
   if DataOnlyOnVld then
      Data <= (Data'range => 0);
   end if;
   Vld <= '0';
   for i in 0 to ClkPerSpl - 2 loop
      wait until rising_edge(Clk);
   end loop;
end if;
```

注意循环计数：成交拍算第 1 拍，再额外等 `ClkPerSpl-2` 拍，下一轮循环开头的握手又算 1 拍，合计正好 `ClkPerSpl` 拍一个采样。

循环出口后还有一句收尾：

> [hdl/psi_tb_textfile_pkg.vhd:L133-L136](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L133-L136) — 文件读完（或达 `MaxLines`）后把 `Vld` 拉低并关闭文件，防止句柄泄漏。

#### 4.2.4 代码实践（最小调用示例·源码阅读型）

> 本节是「写出最小调用」的阅读型实践；完整的可运行 testbench 在第 5 节综合实践里给出。

**目标**：为「两列激励、每采样 1 拍、下游永不反压」的场景写出 `ApplyTextfileContent` 的调用骨架。

1. 假设 DUT 输入是两个整数 `a`、`b`。在 testbench 里声明：
   ```vhdl
   signal StimVld  : std_logic;
   signal StimData : TextfileData_t(0 to 1);   -- 两列：a, b
   ```
2. 在一个 `stim : process` 里写一行调用（`Rdy` 不关心 → 传 `PsiTextfile_SigOne`）：
   ```vhdl
   ApplyTextfileContent(
      Clk  => Clk,
      Rdy  => PsiTextfile_SigOne,        -- 永远就绪
      Vld  => StimVld,
      Data => StimData,
      Filepath => "stim.txt");
   ```
3. 把 `StimVld`/`StimData` 接到 DUT 输入；用一个时钟 `Clk`（例如 100 MHz）驱动。

**需要观察的现象**：仿真波形上，每来一个 `rising_edge(Clk)`，`StimVld` 出现一个单周期高脉冲，`StimData(0)`、`StimData(1)` 依次等于文件每一行的第 1、2 列；文件读完后 `StimVld` 恒为 0。

**预期结果**（待本地验证）：对一份 5 行的 `stim.txt`，应看到恰好 5 个 `StimVld` 脉冲。

> ⚠️ 运行前必须先把本包加入 `sim/config.tcl` 的 `add_sources -tag src` 列表（追加 `psi_tb_textfile_pkg.vhd \`），否则编译找不到它。文件路径是相对仿真器工作目录的，ModelSim 里通常相对于启动 `vsim` 的目录。

#### 4.2.5 小练习与答案

**练习 1**：把 `ClkPerSpl` 从 1 改成 4，`Vld` 波形会怎样变化？

**答案**：每个采样占 4 个时钟周期。第 1 拍 `Vld=1` 且 `Data` 为本行值并完成握手，随后 3 拍 `Vld=0`；第 5 拍再抬 `Vld` 给下一行。即有效脉冲之间的间隔从 1 拍变为 4 拍。

**练习 2**：若 DUT 输入端会在某些周期把 `Rdy` 拉低，`ApplyTextfileContent` 会丢采样吗？

**答案**：不会。握手语句 `wait until rising_edge(Clk) and Rdy='1'` 在 `Rdy=0` 时会原地等待，不进入下一行读取；只有 `Rdy=1` 的那个上升沿才成交并推进到下一行。代价是吞吐被下游限制。

**练习 3**：`DataOnlyOnVld => true` 与 `false` 在波形上的差别是什么？

**答案**：仅当 `ClkPerSpl>1` 时有差别。`true`：`Vld=0` 的那些拍里 `Data` 被清成全 0；`false`：`Vld=0` 的拍里 `Data` 保持上一个有效采样的值。前者适合不希望 DUT 看到残留值的场景。

---

### 4.3 CheckTextfileContent — 逐列比对 DUT 输出

#### 4.3.1 概念说明

`CheckTextfileContent` 是 `ApplyTextfileContent` 的镜像：它在**输出侧**扮演**消费者+裁判**。它打开一份「期望文件」，每当 DUT 输出一个有效采样（`Vld=1`），就读取期望文件同一行的各列、与 DUT 实际输出逐列比对，不一致就打印 `###ERROR###`。

方向值得强调（容易和 `Apply` 搞反）：

- `Vld : in std_logic`（DUT 驱动）：DUT 输出有效。
- `Data : in TextfileData_t`（DUT 驱动）：DUT 实际输出值。
- `Rdy : out std_logic`（本过程驱动）：检查器是否就绪。可以用来给 DUT 反压。

也就是说，`Apply` 的 `Rdy` 是 `in`，`Check` 的 `Rdy` 是 `out`——二者在握手里的角色正好互换。

#### 4.3.2 核心流程

```text
file_open(fp, Filepath, read_mode)
while not endfile(fp) 且 (MaxLines 未达上限):
    readline(fp, ln)
    if lineNr > IgnoreLines:
        Rdy <= '1'
        wait until rising_edge(Clk) and Vld='1'   # 等 DUT 给出有效输出
        for idx in 0..Data'length-1:              # 逐列比对
            read(ln, Spl)                         # 期望值
            Sig := Data(idx)                      # 实际值
            assert abs(Sig - Spl) <= Tolerance
              report ErrorPrefix & ": Wrong Sample, line=.. column=.."
                     & " --> Expected .. --> Received .."
              severity error
        if ClkPerSpl > 1:                         # 与 Apply 对称的降采样
            Rdy <= '0'
            for i in 0..ClkPerSpl-2: wait until rising_edge(Clk)
    lineNr += 1
Rdy <= '0'
file_close(fp)
```

参数语义（与 `Apply` 同名的含义一致，新增两个）：

- **`ErrorPrefix`**（默认 `"###ERROR###"`）：失败消息前缀，直接对接 CI 的 `run_check_errors "###ERROR###"`。
- **`Tolerance`**（默认 0，`natural` 即非负）：容差带。判定条件是 `abs(实际 - 期望) <= Tolerance`，即接受区间为：

\[
\text{期望} - \text{Tolerance} \;\leq\; \text{实际} \;\leq\; \text{期望} + \text{Tolerance}
\]

`Tolerance=0` 即严格逐位相等。

#### 4.3.3 源码精读

判定与消息拼接是本过程的灵魂：

> [hdl/psi_tb_textfile_pkg.vhd:L171-L181](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L171-L181) — 逐列：`read` 取期望 `Spl`，读实际 `Sig := Data(idx)`，用 `abs(Sig-Spl) <= Tolerance` 判定；失败时拼出含 `line`/`column`/`Expected`/`Received` 的消息，`severity error`。

```vhdl
colNr := 0;
for idx in 0 to Data'length - 1 loop
   read(ln, Spl);
   Sig := Data(idx);
   assert abs(Sig - Spl) <= Tolerance
   report ErrorPrefix & ": Wrong Sample, line=" & integer'image(lineNr) &
        " column=" & integer'image(colNr) & LF &
        " --> Expected " & integer'image(Spl) & " [0x" &
        hstr(std_logic_vector(to_signed(Spl, 32))) & "]" & LF &
        " --> Received " & integer'image(Sig) & " [0x" &
        hstr(std_logic_vector(to_signed(Sig, 32))) & "]"
   severity error;
   colNr := colNr + 1;
end loop;
```

读这段时请回忆 u3-l1 的结论：**比较判定与错误消息是两条独立路径**。这里判定用的是 `integer` 全范围运算（正确），消息里的十六进制却固定走 `to_signed(x, 32)` → `hstr`（固定 8 位）。由于 `TextfileData_t` 本身就是 `integer`，这里 32 位显示恰好与数据类型匹配，不会出现 u3-l2 那种「>32 位被截断」的坑——但负数会以补码十六进制呈现（如 `-1` 显示 `0xFFFFFFFF`）。

`severity error` 的行为和 `compare_pkg` 完全一致（见 u3-l1）：**只打印，不中断仿真**，让一次跑完暴露所有不匹配，最终由 CI 扫 `###ERROR###` 子串判失败。`LF`（换行符）让消息分三行显示，便于人眼定位。

降采样节拍与 `Apply` 完全对称：

> [hdl/psi_tb_textfile_pkg.vhd:L183-L189](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L183-L189) — `ClkPerSpl>1` 时，比完一个采样后 `Rdy` 拉低 `ClkPerSpl-1` 拍，实现消费侧限速，与 `Apply` 的生产侧限速成对。

这种对称设计意味着：若 DUT 输入侧用 `Apply(..., ClkPerSpl=>N)`、输出侧用 `Check(..., ClkPerSpl=>N)`，两端节拍天然对齐，不会因速率失配而误报。

#### 4.3.4 代码实践（断言触发型）

**目标**：人为制造一次不匹配，观察错误消息格式。

1. 准备一份 `expected.txt`（单列）：
   ```text
   10
   20
   30
   ```
2. 让 DUT 输出故意偏离，例如直接用一个进程把 `DataOut(0)` 在三个有效拍里分别赋 `10`、`21`、`30`（第二拍差 1）。
3. 用 `Tolerance => 0` 调用：
   ```vhdl
   CheckTextfileContent(
      Clk  => Clk,
      Rdy  => PsiTextfile_SigUnused,   -- 不接回 Rdy
      Vld  => OutVld,
      Data => OutData,
      Filepath => "expected.txt",
      Tolerance => 0);
   ```

**需要观察的现象**：Transcript 里应出现一条 `###ERROR###: Wrong Sample, line=3 column=0`（注意 `lineNr` 是文件行号、`column` 从 0 起），随后两行 `--> Expected 20 [0x00000014]`、`--> Received 21 [0x00000015]`。

**预期结果**（待本地验证）：仅第 2 个采样报错一次；把 `Tolerance` 改成 `1` 后该错误消失（因为 \(|21-20|=1 \leq 1\)）。这也印证了 CI 会在 Transcript 中扫到 `###ERROR###` 而判失败。

#### 4.3.5 小练习与答案

**练习 1**：错误消息里 `column=0` 为什么从 0 开始，而 `line=3` 看起来像「第 3 行」？

**答案**：`colNr` 初值为 0、每列自增，所以列号从 0 起；`lineNr` 初值为 1、每读一行自增，所以行号是 1 基的文件行号。读消息时要注意这两个下标基底不同。

**练习 2**：期望值 `-1`、实际值 `-1`，`Tolerance=0`。消息里的十六进制会显示成什么？

**答案**：`to_signed(-1, 32)` 的 32 位补码全 1，`hstr` 输出 `FFFFFFFF`，所以显示 `0xFFFFFFFF`。判定 `abs(-1 - (-1)) = 0 <= 0` 通过，不会真的打印这条消息——但若失败，负数期望/实际都会以补码十六进制呈现。

**练习 3**：`Check` 的 `Rdy` 是 `out`，若我不想给 DUT 输出反压，该传什么？

**答案**：传 `PsiTextfile_SigUnused`。过程内部仍会驱动它（在每个采样前后抬/拉），但驱动到这个「垃圾桶」信号上、调用方不读，等效于不反压；不过注意过程内部在 `ClkPerSpl>1` 时仍会按节拍把 `Rdy` 拉低，这会真实发生，只是无人在意。

---

### 4.4 WriteTextfile — 把结果连同表头写回文件

#### 4.4.1 概念说明

`WriteTextfile` 完成闭环的最后一环：**把 DUT 输出（或中间信号）按采样写回一个新的文本文件**，方便你拿 Python/MATLAB 做频谱、误差、波形等后处理。它的输出与输入约定略有不同：

- 第 1 行是**表头**：各列名字（来自 `Name` 数组），可选追加一列 `time_simulation`。
- 之后每行一个采样：各列用 `to_string(Data(j))` 写成十进制整数，列间用 `spacer` 分隔（默认 `" , "`），可选追加一列当前仿真时间 `now`。

它同样以 `Vld` 触发：每遇到一个 `rising_edge(Clk) and Vld='1'`，就写一行（第一行写表头，后续写数据）。

#### 4.4.2 核心流程

```text
while lineNr <= nb_data + 2:
    wait until rising_edge(Clk) and Vld='1'
    if 文件尚未打开: file_open(fp, Filepath, WRITE_MODE)   # 懒打开
    if lineNr == 1:                                         # 表头
        if time_sim=false: 写 Name(0) .. Name(n-1)，spacer 分隔
        else:              写 Name(0) .. Name(n-1) + "time_simulation"
    else:                                                   # 数据行
        if time_sim=false: 写 to_string(Data(0)) .. to_string(Data(n-1))
        else:              写 to_string(Data(0)) .. + now（仿真时间）
    writeline(fp, ln); lineNr += 1
file_close(fp)
```

参数：

- **`nb_data`**（常量）：意图上是「写多少个数据采样」。注意循环上界是 `nb_data + 2`（见 4.4.3 的边读边数提醒）。
- **`time_sim`**（默认 `true`）：是否追加仿真时间列。`true` 时表头多一列 `time_simulation`、每行多一列 `now`。
- **`Name`**：`TextfileName_t`，每列的名字。
- **`spacer`**（默认 `" , "`）：列分隔符。
- **`Filepath`**（默认 `/data/processing_data.txt`）：输出路径。

#### 4.4.3 源码精读

循环结构与懒打开：

> [hdl/psi_tb_textfile_pkg.vhd:L224-L228](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L224-L228) — 主循环 `while lineNr <= nb_data + 2`，每次先 `wait until rising_edge(Clk) and Vld='1'`，再在「文件未打开」时用 `file_open` 打开（懒打开，确保文件创建在第一个有效拍、而非 0 时刻）。

```vhdl
while lineNr <= nb_data + 2 loop
   wait until rising_edge(Clk) and Vld = '1';
   if file_status /= OPEN_OK then
      file_open(file_status, fp, Filepath, WRITE_MODE);
   end if;
```

表头分支（以 `time_sim=true` 为例）：

> [hdl/psi_tb_textfile_pkg.vhd:L242-L253](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L242-L253) — `time_sim=true` 时表头循环范围 `for j in 0 to Data'length`，比列数多迭代一次，最后那次（`j = Data'length`）写常量字符串 `"time_simulation"`，前面每次写 `Name(j)` 加 `spacer`。

数据行分支（以 `time_sim=true` 为例）：

> [hdl/psi_tb_textfile_pkg.vhd:L266-L277](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L266-L277) — 数据行同样多迭代一次，最后那次用 `write(ln, now)` 写当前仿真时间，前面每次写 `to_string(Data(j))` 加 `spacer`。

```vhdl
for j in 0 to Data'length loop
   if j = Data'length then
      write(ln, now);                 -- 仿真时间列
   else
      write(ln, to_string(Data(j)));  -- to_string(integer) -> 十进制字符串
      write(ln, spacer);
   end if;
end loop;
writeline(fp, ln);
```

这里的 `to_string(integer)` 来自 [hdl/psi_tb_txt_util.vhd:L357-L360](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L357-L360)，它内部就是 `str(int)`（十进制，见 u2-l1）。

> **边读边数（待本地验证）**：循环上界是 `nb_data + 2`。从 `lineNr=1`（表头）开始推：每次迭代 `lineNr` 自增 1，循环在 `lineNr` 从 `nb_data+2` 变为 `nb_data+3` 时退出。也就是说它实际写出 **1 行表头 + (nb_data+1) 行数据**，比 `nb_data` 多一行数据。如果你的下游脚本按精确行数解析，请把这多出来的一行算进去，或自行调整 `nb_data`。这点源码里没有注释说明，建议本地跑一次确认实际行数。

**另一个关键坑**：默认 `spacer => " , "` 带逗号，输出形如 `117 , 124 , 10 ns`。如 4.1.2 所述，逗号会让 `std.textio` 的整数 `read` 失败。所以**若你想让 `WriteTextfile` 的输出再次被 `Apply`/`Check` 读回（闭环自检），必须把 `spacer` 改成纯空格**，例如 `spacer => "   "`，并且 `time_sim => false`（去掉时间列，否则多出来的一列非整数也会破坏读取）。

#### 4.4.4 代码实践（最小写文件）

**目标**：把一段已知的 DUT 输出写成文件，肉眼核对格式。

1. 准备一个最小 DUT 输出：每拍 `DataOut(0)` 依次为 `5, 6, 7`，`OutVld` 同步拉高 3 拍。
2. 调用（关掉时间列、用纯空格 spacer，便于后续回读）：
   ```vhdl
   WriteTextfile(
      Clk     => Clk,
      Vld     => OutVld,
      Data    => DataOut,
      nb_data => 3,
      time_sim=> false,
      Name    => ("a"),                 -- TextfileName_t，单列
      spacer  => "   ",                 -- 纯空格，可被 Apply/Check 读回
      Filepath=> "out.txt");
   ```

**需要观察的现象**：仿真结束后工程目录下生成 `out.txt`。

**预期结果**（待本地验证）：文件内容为
```text
a
5
6
7
8
```
（第一行表头 `a`；随后 4 行数据，对应前述「比 `nb_data` 多一行」的现象）。若改回默认 `spacer => " , "`、`time_sim => true`，则会得到带逗号与时间列、首行多一个 `time_simulation` 的版本——这种格式适合 Python `np.loadtxt(..., delimiter=',')`，但**不能**被本包的 `Apply`/`Check` 读回。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `WriteTextfile` 用「懒打开」（在第一个 `Vld='1'` 才 `file_open`），而不是进过程就打开？

**答案**：这样文件的「首行时间戳」与第一个有效采样对齐，且若 `Vld` 一直不来（DUT 没产出），就不会创建空文件、也不会在 0 时刻留下无意义输出。`file_status` 初值设为 `MODE_ERROR`（非 `OPEN_OK`）正是为了强制第一次进入循环时触发打开。

**练习 2**：想把 `WriteTextfile` 输出再喂回 `CheckTextfileContent` 做闭环自检，至少要改哪两个参数？

**答案**：`spacer` 改成纯空格（如 `"   "`），`time_sim` 改成 `false`。前者避免逗号破坏整数 `read`，后者避免多出一列非整数（仿真时间）同样破坏读取。另外还要忽略 `CheckTextfileContent` 的表头行（用 `IgnoreLines => 1`）。

**练习 3**：`Name => ("a", "b")` 对应的 `Data` 应该是几列？

**答案**：两列。`Name` 与 `Data` 的列数应一致（表头循环 `for j in 0 to Data'length-1` 用 `Name(j)`），所以 `Name` 给两个名字，`Data` 就应是 `TextfileData_t(0 to 1)`。

---

## 5. 综合实践

把本讲四个模块串成一个端到端的位真回归测试。**DUT** 取一个极简的二元运算：输入两列 `a`、`b`，输出两列 `y0 = a + b`、`y1 = a - b`。

**步骤 1：用 Python 生成激励与期望文件**

```python
import numpy as np
a = np.array([10, 20, 30, 40, 50])
b = np.array([1, 2, 3, 4, 5])
stim     = np.column_stack((a, b))          # 输入两列
expected = np.column_stack((a + b, a - b))  # 期望输出两列
np.savetxt("stim.txt",     stim,     fmt="%i")
np.savetxt("expected.txt", expected, fmt="%i")
```

两份文件都是空格分列、纯整数，满足本包约定。

**步骤 2：写出 testbench 骨架**（示例代码，非项目原有代码）

```vhdl
-- 示例代码：最小位真 testbench
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use std.textio.all;
use work.psi_tb_textfile_pkg.all;

entity textfile_demo_tb is end;

architecture sim of textfile_demo_tb is
   signal Clk      : std_logic := '0';
   signal InVld    : std_logic;
   signal InData   : TextfileData_t(0 to 1);   -- a, b
   signal OutVld   : std_logic;
   signal OutData  : TextfileData_t(0 to 1);   -- a+b, a-b
begin
   -- 时钟
   Clk <= not Clk after 5 ns;

   -- DUT：寄存输入后做 a+b / a-b
   dut : block
      signal a_r, b_r : integer := 0;
   begin
      process(Clk)
      begin
         if rising_edge(Clk) then
            if InVld = '1' then
               a_r <= InData(0);
               b_r <= InData(1);
            end if;
         end if;
      end process;
      OutData(0) <= a_r + b_r;
      OutData(1) <= a_r - b_r;
      OutVld     <= InVld after 10 ns;          -- 简化：延迟一拍
   end block;

   -- 激励施加（生产者）
   stim : process
   begin
      ApplyTextfileContent(
         Clk => Clk, Rdy => PsiTextfile_SigOne,
         Vld => InVld, Data => InData,
         Filepath => "stim.txt");
      wait;
   end process;

   -- 输出比对（消费者+裁判）
   check : process
   begin
      CheckTextfileContent(
         Clk => Clk, Rdy => PsiTextfile_SigUnused,
         Vld => OutVld, Data => OutData,
         Filepath => "expected.txt",
         Tolerance => 0);
      wait;
   end process;

   -- 结果写回（闭环导出）
   wr : process
   begin
      WriteTextfile(
         Clk => Clk, Vld => OutVld, Data => OutData,
         nb_data => 5, time_sim => false,
         Name => ("sum", "diff"), spacer => "   ",
         Filepath => "out.txt");
      wait;
   end process;
end architecture;
```

**步骤 3：编译与运行**

1. 把 `psi_tb_textfile_pkg.vhd` 加进 `sim/config.tcl` 的 `add_sources -tag src`，并为该 TB `create_tb_run`（参考 u1-l3、u8-l1）。
2. 用 `sim/run.tcl`（ModelSim）或 `sim/runGhdl.tcl`（GHDL）跑仿真（见 u1-l3）。把 `stim.txt`、`expected.txt` 放在仿真器工作目录。

**步骤 4：观察与闭环**

- Transcript 里**不应**出现 `###ERROR###`（因为 DUT 与期望完全一致）。
- 工程目录下生成 `out.txt`，内容是 `sum`、`diff` 两列数据（首行表头）。
- **闭环自检**：把 `out.txt` 的表头去掉后存为 `out_data.txt`，再写一个 `CheckTextfileContent(..., Filepath => "out_data.txt", IgnoreLines => 0)` 去比对 DUT 输出——应同样无报错，从而验证「写出去的能读回来」。

> ⚠️ 注意 `dut` 块里 `OutVld <= InVld after 10 ns` 只是为演示把输出对齐到寄存后那一拍；真实 DUT 的流水延迟不同时，需要相应调整 `expected.txt` 的对齐（或给 `Apply`/`Check` 不同的 `ClkPerSpl`）。`after 10 ns` 这种写法在纯 RTL 里不可综合，但本讲是 testbench，允许使用（见 u1-l1）。

---

## 6. 本讲小结

- `psi_tb_textfile_pkg` 用一个极简约定驱动位真仿真：**文本文件、每行一个采样、空格分列、值为整数**；空格而非逗号是 `std.textio` 整数 `read` 的硬性要求。
- `ApplyTextfileContent` 是**激励生产者**，逐行读文件、按列填 `Data`、每个采样抬一次 `Vld`，靠 `wait until rising_edge(Clk) and Rdy='1'` 与下游握手；`ClkPerSpl`/`MaxLines`/`IgnoreLines`/`DataOnlyOnVld` 控制节拍与范围。
- `CheckTextfileContent` 是**输出侧消费者+裁判**，方向与 `Apply` 镜像（`Vld`/`Data` 为 `in`、`Rdy` 为 `out`），用 `abs(实际-期望) <= Tolerance` 判定，失败时打印带 `line`/`column`/`Expected`/`Received` 的 `###ERROR###` 消息、`severity error` 不中断。
- `WriteTextfile` 把结果连同表头写回文件供 Python/MATLAB 处理；它默认 `spacer` 带逗号、可加仿真时间列，**因此输出默认不能直接被 `Apply`/`Check` 读回**——闭环自检需改用纯空格 `spacer` 并关时间列。
- `PsiTextfile_SigOne`（恒 `'1'`）/`PsiTextfile_SigUnused`（垃圾桶）存在的根本原因是 **VHDL 过程的 `signal` 形参不能留空**，调用方必须传一个真实信号占位。
- 本包**未注册进 `config.tcl` 的 CI 编译清单**，要实际运行必须先手动加入；它依赖 `psi_tb_txt_util`（`hstr`、`to_string`）。

---

## 7. 下一步学习建议

- **横向对照**：回到 u3（compare_pkg）和 u4（activity_pkg），体会「文本文件驱动」与「单点 `IntCompare`/`CheckNoActivity`」两类检查的分工——前者适合海量数值回归，后者适合握手时序与协议电平。
- **看一个真实 testbench 的组织**：阅读 `testbench/psi_tb_i2c_pkg_tb.vhd`（u7-l4），观察 master/slave 双进程并发对拍的组织方式，本讲 `stim`/`check` 两个并发进程的结构是它的简化版。
- **CI 接入**：进 u8-l1，学习如何把本讲新增的 testbench 正式注册进 `config.tcl`（`add_sources` + `create_tb_run`），让位真回归也纳入 CI 的 `###ERROR###` 双重检查。
- **进阶扩展**：若你的数据是定点而非整数，可在 `psi_common` 的定点包基础上仿照本包写一个「定点列」版本（包注释里 `TextfileFormat_t` 的注释痕迹正是当年与 `PsiFix` 耦合、后又解耦的遗迹，见 [L42](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L42) 与 [L201-L207](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L201-L207) 的 TAG/TODO 注释）。
