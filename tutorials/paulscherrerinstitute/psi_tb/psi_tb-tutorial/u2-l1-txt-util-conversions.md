# 字符串与数值转换函数

## 1. 本讲目标

学完本讲，你应该能够：

- 理解 `psi_tb_txt_util` 在整个 psi_tb 库里的「底座」地位——后面所有比较检查、AXI/I2C BFM 的可读错误消息都依赖它。
- 掌握 `print` 如何把字符串送到仿真控制台（Transcript），以及为什么它的实现被 `--synopsys translate off` 包住。
- 用 `chr` / `str` / `hstr` 在 `std_logic`、`std_logic_vector` 与字符串之间互转，并清楚「最高位在左」这一统一约定。
- 用 `str(int; base)` 以任意进制（2–36）打印整数，并理解 `to_string` 系列重载为何要模仿 VHDL-2008 的内建函数。
- 用 `to_upper` / `to_lower` / `to_std_logic_vector` 做大小写转换和「字符串→向量」的反向解析。

## 2. 前置知识

在动手之前，先回忆几个上一讲（[u1-l1](u1-l1-project-overview.md)）建立的概念：

- **testbench 只用于仿真、不可综合**。正因为不进芯片，testbench 代码可以自由使用 VHDL 的全部语言能力，包括文件 I/O（`std.textio`）和字符串操作。本讲的 `print` 就用到了 `textio`。
- **Transcript**（[u1-l3](u1-l3-simulation-and-ci.md)）是 ModelSim/GHDL 里显示仿真标准输出的窗口。`print` 写出的内容就出现在这里。
- **`###ERROR###` 前缀**：比较检查过程在自检失败时拼出可读消息，这条消息最终就是由本讲的 `str`/`hstr`/`print` 拼接并打印出来的。可以理解为：本讲是「消息系统的字母表」。

一个关键的 VHDL 小知识：VHDL 里字符串 `string` 的下标范围**必须是 natural 且通常从 1 开始**，而 `std_logic_vector` 的下标可以是任意整数范围（比如 `11 downto 0` 或 `0 to 7`）。把两者互转时，必须显式处理这种「下标空间不一致」的问题——你会看到 `str(slv)` 正是为这件事而写。

## 3. 本讲源码地图

本讲只涉及一个文件，但它是整个库最常被 `use` 的文件：

| 文件 | 作用 |
| --- | --- |
| [hdl/psi_tb_txt_util.vhd](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd) | 文本工具包。提供 `print`、`chr`、`str`、`hstr`、`to_string`、`to_upper/lower`、`to_std_logic_vector` 以及文件 I/O（文件 I/O 留到 [u2-l2](u2-l2-txt-util-fileio.md) 讲）。 |

文件结构很规整：前半段（第 52–147 行）是 `package ... is` 声明（接口），后半段（第 152–583 行）是 `package body` 实现。本讲按「声明 + 实现」成对引用。

引用方式（和仓库里现有 testbench 一致，见 [testbench/psi_tb_i2c_pkg_tb.vhd:14-16](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L14-L16)）：

```vhdl
library IEEE;
use IEEE.std_logic_1164.all;
use IEEE.numeric_std.all;

library work;
use work.psi_tb_txt_util.all;
```

## 4. 核心概念与源码讲解

### 4.1 print / chr / str / hstr：把硬件信号变成可读文本

#### 4.1.1 概念说明

这是本讲最重要的一组工具，解决一个朴素的问题：**仿真时信号是 `std_logic` / `std_logic_vector`，但人眼和日志需要的是字符和字符串**。

- `print` —— 把字符串送到 Transcript（标准输出）。
- `chr` —— 单个值 → 单个字符。有两个重载：`std_logic → character` 和 `integer → character`（后者是任意进制转换的基石）。
- `str` —— 值 → 字符串。重载覆盖 `std_logic`、`std_logic_vector`（二进制）、`boolean`、`integer`、`integer + base`。
- `hstr` —— `std_logic_vector` / `unsigned` → 十六进制字符串。

它们是后续所有可读消息的「原料」。比如 `StdlvCompareStdlv` 在失败时会用 `hstr` 把期望值和实际值拼进 `###ERROR### ... Expected ... Received ...` 这类消息里。

#### 4.1.2 核心流程

`print` 的数据流非常直白：

```
调用 print("某段文本")
   → write(msg_line, text)      -- 把字符串追加进一个 line 缓冲
   → writeline(output, msg_line) -- 把整行写到 stdout(output)，并换行
   → Transcript 里出现一行文本
```

注意 `print` 是「整行输出」——每次调用都会换行，不需要自己加 `\n`。

`str(slv)` 把向量转成二进制字符串时，必须做**下标空间归一化**：向量的下标范围未知（可能是 `11 downto 0`），但结果字符串必须是 `1 .. N`。做法是用一个独立计数器 `r` 从 1 开始递增填充，沿 `slv'range` 遍历——因为 `downto` 向量的 `'range` 是从 MSB 走到 LSB，所以填充后 **第 1 个字符就是 MSB**，与人类书写二进制的习惯一致。

`hstr(slv)` 的流程稍复杂，分四步：

1. 算出十六进制位数：\(\text{hexlen} = \left\lceil \text{bits} / 4 \right\rceil\)。
2. 把向量零扩展进一个 68 位（16 个 nibble）的缓冲 `longslv`，使后续按 nibble 取位统一。
3. 从最低位 nibble 开始，每 4 位查表映射成一个十六进制字符。
4. 返回 `hex(1 to hexlen)`，仍保持 **MSB 在左**。

#### 4.1.3 源码精读

**`print(text)` —— 控制台输出的核心。**

[hdl/psi_tb_txt_util.vhd:154-161](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L154-L161) 用 `write` + `writeline(output, ...)` 把字符串打到 stdout：

```vhdl
procedure print(text: string) is
   variable msg_line: line;
begin
   --synopsys translate off
   write(msg_line, text);
   writeline(output, msg_line);
   --synopsys translate on
end procedure print;
```

两点要记住：

- `output` 是 `std.textio` 预定义的、指向标准输出的 `TEXT` 文件，ModelSim 里就是 Transcript。
- `--synopsys translate off / on` 是综合工具的编译指示，让综合器跳过这段代码——这正是 [u1-l1](u1-l1-project-overview.md) 讲过的「testbench 不可综合」的实物体现：`textio` 根本无法综合，但包里写了它也没关系，因为 psi_tb 永远不会被综合。

还有一个带开关的重载 [hdl/psi_tb_txt_util.vhd:164-169](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L164-L169)，便于做 debug 开关：`print(active => DbgOn, text => "...")`，只有 `active` 为真才输出。

**`chr(sl)` —— `std_logic` 九值到字符的一一映射。**

[hdl/psi_tb_txt_util.vhd:172-187](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L172-L187) 用 `case` 把 9 个 `std_logic` 值（`U X 0 1 Z W L H -`）分别映射成同名字符。

**`str(slv)` —— 向量转二进制串，注意下标归一化。**

[hdl/psi_tb_txt_util.vhd:201-211](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L201-L211)：

```vhdl
function str(slv: std_logic_vector) return string is
   variable result : string (1 to slv'length);
   variable r      : integer;
begin
   r:=1;
   for i in slv'range loop
       result(r) := chr(slv(i));
       r:=r+1;
   end loop;
   return result;
end function str;
```

注释（第 197–200 行）专门解释了为什么要这样写：字符串范围恒为 natural，而向量范围可能是任意整数范围。`r` 从 1 递增，保证结果字符串范围合法；遍历 `slv'range` 保证 MSB 先被写入 `result(1)`。

**`chr(int)` —— 整数到字符（任意进制的基石）。**

[hdl/psi_tb_txt_util.vhd:228-271](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L228-L271) 把 `0..9` 映射成 `'0'..'9'`，把 `10..35` 映射成 `'A'..'Z'`，其余返回 `'?'`。这意味着它最多支持 36 进制（0-9 + A-Z）。下一节的 `str(int; base)` 会反复调用它。

**`hstr(slv)` —— 向量转十六进制串。**

[hdl/psi_tb_txt_util.vhd:312-349](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L312-L349) 关键片段：

```vhdl
hexlen := (slv'left+1)/4;
if (slv'left+1) mod 4 /= 0 then
   hexlen := hexlen + 1;                       -- 向上取整 = ceil(bits/4)
end if;
longslv(slv'left downto 0) := slv;             -- 零扩展进 68 位缓冲
for i in (hexlen-1) downto 0 loop
    fourbit := longslv(((i*4)+3) downto (i*4)); -- 取第 i 个 nibble
    case fourbit is
        when "0000" => hex(hexlen-I) := '0';
        ...
        when "1010" => hex(hexlen-I) := 'A';
        ...
        when "ZZZZ" => hex(hexlen-I) := 'z';    -- 整个 nibble 都是 Z
        when "UUUU" => hex(hexlen-I) := 'u';
        when "XXXX" => hex(hexlen-I) := 'x';
        when others => hex(hexlen-I) := '?';    -- 混合 meta 值
    end case;
end loop;
return hex(1 to hexlen);
```

两个值得记住的细节：

- 这个实现假定向量是 `xxx downto 0` 风格（用 `slv'left` 当 MSB 下标）。对 `downto 0` 的向量没问题。
- meta 值只有「整个 nibble 都是同一个 meta 值」时才显示成小写 `z/u/x`；只要 nibble 里混杂了不同值，就显示 `?`。所以 `hstr` 不是严格的逐位还原，而是给 testbench 日志一个「大致可读」的十六进制。
- 还有一个 `unsigned` 重载 [hdl/psi_tb_txt_util.vhd:351-354](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L351-L354)，直接转成 `std_logic_vector` 后复用上面的实现。

#### 4.1.4 代码实践

**实践目标**：直观感受 `str` / `hstr` 的输出格式和「MSB 在左」约定。

**操作步骤**（源码阅读型 + 可选运行）：

1. 阅读上面的 `str(slv)` 和 `hstr(slv)` 源码，在脑子里跟踪 `x"FCA"`（12 位 `"111111001010"`）的转换。
2. 推断下列表达式的值：
   - `str(x"FCA")`（注意这里直接写字面量仅为推理，真实代码里要声明一个 12 位信号）
   - `hstr(x"FCA")`
3. 如果你想真正运行，把下面的「示例代码」存成一个最小 testbench（见第 5 节综合实践，那里给了完整可运行版本）。

**需要观察的现象**：

- `str` 输出 12 个字符 `111111001010`，最左是 MSB。
- `hstr` 输出 3 个字符 `FCA`，最左是最高位 nibble。

**预期结果**：二进制串长度 = 向量位宽；十六进制串长度 = \(\lceil \text{bits}/4 \rceil\)。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `print` 的实现要包在 `--synopsys translate off/on` 里？
**参考答案**：因为 `print` 用了 `std.textio` 的 `writeline(output, ...)`，这是仿真专用的 I/O，无法综合。psi_tb 是 testbench 库、永不被综合，但加上这对指示可以让综合工具在万一扫到它时直接跳过，避免报错。这呼应了 [u1-l1](u1-l1-project-overview.md) 讲的「testbench 不可综合」定位。

**练习 2**：一个 12 位向量，`str` 和 `hstr` 输出的字符串分别多长？
**参考答案**：`str` 输出 12 个字符（逐位二进制）；`hstr` 输出 \(\lceil 12/4 \rceil = 3\) 个十六进制字符。

**练习 3**：`hstr` 对 `"ZZ0Z"` 这个 nibble 会输出什么？为什么？
**参考答案**：输出 `?`。因为只有当整个 nibble 都是 `Z`（`"ZZZZ"`）时才输出小写 `z`；只要 nibble 内出现混合值，就落入 `when others => '?'` 分支。

---

### 4.2 str(int; base) 与 to_string 重载：整数与任意进制

#### 4.2.1 概念说明

这一组工具解决「**把整数/实数变成字符串**」的需求，是日志里打印数值的主力。

- `str(int)` —— 十进制字符串（最常用）。
- `str(int, base)` —— 任意进制字符串（2、16 都可以，最高 36）。
- `to_string(...)` —— 一组重载，覆盖 `integer`、`real`、`signed`、`unsigned`、`std_logic_vector`。

为什么要专门做一套 `to_string`？因为 **VHDL-93 没有 `to_string` 这个内建函数**（它是 VHDL-2008 才加进来的）。psi_tb 希望自己的代码在只支持 VHDL-93 的仿真器上也能用统一的名字 `to_string(x)`，于是自己在包里实现了一组同名重载。

#### 4.2.2 核心流程

`str(int, base)` 把整数转成 base 进制字符串，用的是经典的「除基取余」思路。对非负整数 \(N\)、基 \(B\)，第 \(p\) 位（从低位数起，\(p=0,1,2,\dots\)）的数字是：

\[
d_p = \left\lfloor \frac{N}{B^p} \right\rfloor \bmod B
\]

实现上分三步：

1. 先对原数取绝对值（处理负号），并循环除以 `base` 数出需要多少位（`len`）。
2. 从最低位开始，用 `chr(abs_int/power mod base)` 逐位填字符，`power` 每轮乘 `base`。
3. 若原数为负，在最前面补一个 `'-'`。

关于运算符优先级：表达式 `abs_int/power mod base` 在 VHDL 里，`/` 和 `mod` 同优先级、左结合，所以等价于 `(abs_int/power) mod base`，正是我们想要的。

`to_string` 各重载的实现策略：

| 重载 | 实现 | 输出含义 |
| --- | --- | --- |
| `to_string(integer)` | 调 `str(int)` | 十进制 |
| `to_string(real)` | `real'image(num)` | 实数的默认文本（依赖仿真器） |
| `to_string(signed)` | `integer'image(to_integer(num))` | **十进制**（有符号数值） |
| `to_string(unsigned)` | `integer'image(to_integer(num))` | **十进制**（无符号数值） |
| `to_string(std_logic_vector)` | 调 `str(num)` | **二进制**（逐位） |

⚠️ **一个容易踩坑的点**：`to_string(unsigned)` / `to_string(signed)` 返回的是**十进制数值**（先 `to_integer` 再 `integer'image`），而 `to_string(std_logic_vector)` 返回的是**二进制位串**。同样一段位模式，套不同类型会得到完全不同的字符串。日志里想要哪种表达，就要先转成对应类型再 `to_string`。

#### 4.2.3 源码精读

**`str(int, base)` —— 任意进制转换。**

[hdl/psi_tb_txt_util.vhd:275-303](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L275-L303)：

```vhdl
function str(int: integer; base: integer) return string is
   variable temp    : string(1 to 10);
   variable abs_int : integer;
   variable len     : integer := 1;
   variable power   : integer := 1;
begin
   abs_int := abs(int);                          -- 先处理负号
   -- 数位数：循环除以 base，统计需要多少字符
   num := abs_int;
   while num >= base loop
      len := len + 1;
      num := num / base;
   end loop;
   -- 从最低位向最高位填字符
   for i in len downto 1 loop
      temp(i) := chr(abs_int/power mod base);    -- 第 i 位数字
      power   := power * base;
   end loop;
   -- 负数补符号
   if int < 0 then
      return '-' & temp(1 to len);
   else
      return temp(1 to len);
   end if;
end function str;
```

`temp` 定长 10 字符，恰好够容纳 32 位整数的最大十进制位数（2147483647 是 10 位）；符号位用字符串拼接 `'-' & ...` 单独处理，不占 `temp`。

**`str(int)` —— 十进制的语法糖。**

[hdl/psi_tb_txt_util.vhd:306-309](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L306-L309) 直接转发到 `str(int, 10)`。

**`to_string` 系列重载。**

声明集中在 [hdl/psi_tb_txt_util.vhd:86-99](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L86-L99)，实现在 [hdl/psi_tb_txt_util.vhd:357-380](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L357-L380)。注意 `signed`/`unsigned` 两个重载都走 `to_integer` + `integer'image`：

```vhdl
function to_string(num : signed) return string is
begin
   return integer'image(to_integer(num));     -- 十进制数值
end function;

function to_string(num : unsigned) return string is
begin
   return integer'image(to_integer(num));     -- 十进制数值
end function;
```

而 `std_logic_vector` 重载走 `str`（二进制）：

```vhdl
function to_string(num : std_logic_vector) return string is
begin
   return str(num);                            -- 二进制位串
end function;
```

#### 4.2.4 代码实践

**实践目标**：体会同一数值用不同进制/类型打印的差异。

**操作步骤**（源码阅读型，可自行接入 testbench 验证）：

1. 阅读上面的 `str(int, base)` 源码，手动跟踪 `str(255, 16)`：
   - `len`：255 ≥ 16 → len=2；15 < 16 停。所以 len=2。
   - `i=2`：`chr(255/1 mod 16)` = `chr(15)` = `'F'`；power=16。
   - `i=1`：`chr(255/16 mod 16)` = `chr(15)` = `'F'`。
   - 结果 `"FF"`。
2. 跟踪 `str(255, 2)`：应当得到 8 位二进制 `"11111111"`。
3. 推断 `to_string(to_unsigned(255, 8))` 与 `to_string(std_logic_vector(to_unsigned(255,8)))` 的差异。

**需要观察的现象 / 预期结果**：

- `str(255, 16)` → `"FF"`
- `str(255, 2)` → `"11111111"`
- `to_string(to_unsigned(255, 8))` → `"255"`（十进制，因为走 unsigned 重载）
- `to_string(std_logic_vector(to_unsigned(255, 8)))` → `"11111111"`（二进制，因为走 slv 重载）

同一组位模式、两种打印，差别来自重载选择。**待本地验证**：在你自己的仿真器上跑一遍，确认 `to_string(signed/unsigned)` 的输出确实是十进制。

#### 4.2.5 小练习与答案

**练习 1**：`str(-10, 16)` 的输出是什么？
**参考答案**：`abs(-10)=10`，`len=1`（10 < 16），`temp(1)=chr(10)='A'`；原数为负，所以返回 `'-' & "A"` = `"-A"`。

**练习 2**：为什么需要单独定义 `to_string`，而不是直接用 VHDL-2008 的内建版本？
**参考答案**：psi_tb 要兼容只支持 VHDL-93 的仿真器，而 `to_string` 是 VHDL-2008 才内建的。自己在包里定义这组同名重载，可以让库代码和用户 testbench 都用统一的 `to_string(x)` 写法，与 VHDL 标准版本无关。

**练习 3**：`to_string(x"FF")` 和 `to_string(unsigned(x"FF"))` 输出分别是什么？（假设 `x"FF"` 是 8 位）
**参考答案**：前者把 `x"FF"` 当 `std_logic_vector`，走 `str` → 二进制 `"11111111"`；后者当 `unsigned`，走 `to_integer` → 十进制 `"255"`。

---

### 4.3 to_upper / to_lower / to_std_logic_vector：大小写与反向转换

#### 4.3.1 概念说明

前两节都是「值 → 字符串」，这一节补两类工具：

- `to_upper` / `to_lower` —— 字符或字符串的大小写转换。在 AXI 包里把字符串解析成数值时很有用（比如允许 `"ff"` 和 `"FF"` 都被当作十六进制 ff）。
- `to_std_logic_vector` —— **字符串 → 向量**的反向解析，是 `str(slv)` 的逆操作。

#### 4.3.2 核心流程

`to_upper` / `to_lower` 用大 `case` 表逐字符映射，非字母字符原样返回。字符串版本就是对每个字符调用字符版本。

`to_std_logic_vector(s)` 关键在于**确定结果向量的范围和位序**：

1. 结果向量声明为 `std_logic_vector(s'high - s'low downto 0)`，长度等于字符串长度。
2. 用一个反向计数器 `k` 从最高下标开始递减，沿字符串 `'range` 遍历，把**第一个字符放进 MSB**。

这保证了它与 `str(slv)` 是一对互逆操作：`str` 把 MSB 放在字符串第 1 位，`to_std_logic_vector` 又把字符串第 1 位读回 MSB。两者共享「MSB 在左」的约定。

#### 4.3.3 源码精读

**`to_upper` / `to_lower`（字符版本）。**

[hdl/psi_tb_txt_util.vhd:387-420](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L387-L420)（to_upper）和 [hdl/psi_tb_txt_util.vhd:424-457](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L424-L457)（to_lower）都是 `case c is ... when others => 保持原值` 的逐项映射，非字母落入 `others` 原样返回。

**字符串版本。**

[hdl/psi_tb_txt_util.vhd:460-467](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L460-L467)（to_upper）对每个字符调用字符版：

```vhdl
function to_upper(s: string) return string is
   variable uppercase: string (s'range);
begin
   for i in s'range loop
       uppercase(i) := to_upper(s(i));
   end loop;
   return uppercase;
end to_upper;
```

注意结果字符串用了 `s'range` 作范围（保持与输入相同的下标空间），to_lower 同理（[hdl/psi_tb_txt_util.vhd:470-477](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L470-L477)）。

**`to_std_logic(c)` —— 字符到 `std_logic`。**

[hdl/psi_tb_txt_util.vhd:483-509](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L483-L509)：合法的 9 值字符（`U X 0 1 Z W L H -`）映射回对应 `std_logic`，**其他字符一律映射成 `'X'`**（`when others => sl := 'X'`）。这意味着它只能解析二进制文本，不能解析十六进制字母。

**`to_std_logic_vector(s)` —— 字符串到向量。**

[hdl/psi_tb_txt_util.vhd:513-523](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L513-L523)：

```vhdl
function to_std_logic_vector(s: string) return std_logic_vector is
   variable slv : std_logic_vector(s'high - s'low downto 0);
   variable k   : integer;
begin
   k := s'high - s'low;
   for i in s'range loop
       slv(k) := to_std_logic(s(i));   -- 第一个字符 -> MSB
       k      := k - 1;
   end loop;
   return slv;
end function to_std_logic_vector;
```

`k` 从最高下标起递减，所以字符串第 1 个字符（最左）成为 MSB——与 `str(slv)` 正好相反相成。

#### 4.3.4 代码实践

**实践目标**：验证 `str` 与 `to_std_logic_vector` 的互逆性，并看清 `to_std_logic` 对非法字符的处理。

**操作步骤**（源码阅读型）：

1. 跟踪 `to_std_logic_vector("1010")`：
   - `slv` 范围 `s'high-s'low downto 0` = `3 downto 0`。
   - `k=3`：`slv(3) := to_std_logic('1')` → `'1'`；`k=2`。
   - 依次：`slv(2)='0'`、`slv(1)='1'`、`slv(0)='0'`。
   - 结果 `"1010"`（MSB 在左），等于 `x"A"`。
2. 推断 `to_std_logic_vector("1F")`：`'F'` 不是合法 `std_logic` 字符 → `to_std_logic('F')` 返回 `'X'`，所以结果是 `"1X"`。

**需要观察的现象 / 预期结果**：

- `to_std_logic_vector("1010")` = `x"A"`。
- `str(to_std_logic_vector("1010"))` = `"1010"`（互逆成立）。
- `to_std_logic_vector("1F")` = `"1X"`（非法字符变 `'X'`，**不是**十六进制解析）。
- `to_upper("abc42")` = `"ABC42"`，数字和非字母原样保留。

**待本地验证**：在仿真器里对上面表达式做 `print(to_string(...))` 之类断言，确认结果。

#### 4.3.5 小练习与答案

**练习 1**：`to_std_logic_vector("Z01H")` 的结果是什么？
**参考答案**：`'Z'`、`'0'`、`'1'`、`'H'` 都是合法 `std_logic` 字符，按 MSB 在左填充，结果向量为 `"Z01H"`。

**练习 2**：为什么说 `str(slv)` 和 `to_std_logic_vector(s)` 是互逆操作？
**参考答案**：两者都遵循「MSB 在左」约定——`str` 沿 `slv'range`（MSB→LSB）填入 `result(1..)`，`to_std_logic_vector` 把字符串第 1 个字符放进 MSB。所以对只含 `'0'/'1'` 的向量，`to_std_logic_vector(str(slv))` 还原回原向量。

**练习 3**：能不能用 `to_std_logic_vector("FF")` 来解析十六进制？
**参考答案**：不能。`to_std_logic` 只认 `0/1/Z/...` 这些 `std_logic` 字符，字母 `F` 会被当成非法字符返回 `'X'`。十六进制字符串解析需要别的函数（AXI 包里的 `hex_string_to_unsigned` 之类，留到 [u5-l1](u5-l1-axi-types-and-init.md) 讲）。

---

## 5. 综合实践

把本讲三组工具串起来，写一个最小 testbench：声明一个 12 位 `std_logic_vector`，分别用 `str`、`hstr`、`to_string` 打印它的二进制、十六进制、十进制表示。

> 下面的 testbench 是**示例代码**，不在 psi_tb 仓库里（仓库目前只有 `psi_tb_i2c_pkg_tb.vhd` 一个 testbench）。你可以新建文件加入工程。

```vhdl
-- 示例代码：txt_util 转换演示 testbench
library IEEE;
use IEEE.std_logic_1164.all;
use IEEE.numeric_std.all;

library work;
use work.psi_tb_txt_util.all;

entity txt_util_demo_tb is
end entity txt_util_demo_tb;

architecture sim of txt_util_demo_tb is
   -- 12 位向量 = x"FCA" = "111111001010" = 十进制 4042
   signal slv12 : std_logic_vector(11 downto 0) := x"FCA";
begin
   process
   begin
      print("=== txt_util demo ===");

      -- 二进制：str(slv) 与 to_string(slv) 都输出逐位二进制（MSB 在左）
      print("binary   str       : " & str(slv12));
      print("binary   to_string : " & to_string(slv12));

      -- 十六进制：hstr，位数 = ceil(12/4) = 3
      print("hex      hstr      : " & hstr(slv12));

      -- 十进制：先转 unsigned 再 to_string（走 unsigned 重载 => 十进制数值）
      print("decimal  to_string : " & to_string(to_integer(unsigned(slv12))));

      -- 任意进制演示：把 4042 分别按 2 / 16 进制打印
      print("4042 base 2  : " & str(4042, 2));
      print("4042 base 16 : " & str(4042, 16));

      -- 大小写演示
      print("upper('fca') : " & to_upper("fca"));

      wait;
   end process;
end architecture sim;
```

**预期 Transcript 输出**（待本地验证）：

```
=== txt_util demo ===
binary   str       : 111111001010
binary   to_string : 111111001010
hex      hstr      : FCA
decimal  to_string : 4042
4042 base 2  : 111111001010
4042 base 16 : FCA
upper('fca') : FCA
```

注意三个对照点，它们正好覆盖本讲的三个最小模块：

1. `str(slv12)` 与 `to_string(slv12)` 都输出二进制（`to_string` 的 slv 重载就是转发给 `str`）。
2. `hstr(slv12)` 输出 3 位十六进制 `FCA`。
3. 同一个 `x"FCA"`，转成 `unsigned` 后 `to_string` 给出**十进制 4042**——和二进制输出形成鲜明对比，体现了 4.2 节强调的「重载决定语义」。

**如何运行**（参考 [u1-l3](u1-l3-simulation-and-ci.md) 的 PsiSim 流程）：

1. 把上面的文件存为 `testbench/txt_util_demo_tb.vhd`。
2. 在 [sim/config.tcl](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/config.tcl) 里仿照现有 testbench 的注册方式，把文件加入 `tb` tag，并用 `create_tb_run` / `add_tb_run` 注册一次仿真（具体命令请对照 config.tcl 中 i2c tb 的写法）。
3. 按 [u1-l3](u1-l3-simulation-and-ci.md) 执行 `sim/run.tcl`（ModelSim）或 `sim/runGhdl.tcl`（GHDL），在 Transcript 中核对上面 7 行输出。

如果暂时不方便跑仿真，也可以纯做源码阅读：对照第 4 节的源码精读，逐行推理每条 `print` 的输出，再与「预期输出」比对。

## 6. 本讲小结

- `print` 用 `write` + `writeline(output, ...)` 把字符串送到 Transcript；实现包在 `--synopsys translate off/on` 里，正是「testbench 不可综合」的体现。
- `chr` / `str` 处理 `std_logic` 与 `std_logic_vector` 到字符/字符串的转换；`str(slv)` 显式做了下标归一化，保证 **MSB 在左**。
- `hstr` 把向量转成十六进制串，位数 \(\lceil \text{bits}/4 \rceil\)；混合 meta 值的 nibble 显示为 `?`，仅整 nibble 同值时才显示 `z/u/x`。
- `str(int, base)` 用「除基取余」支持 2–36 进制，`chr(int)` 是其基石；`str(int)` 是十进制的语法糖。
- `to_string` 系列是为了兼容 VHDL-93 而自造的同名重载；**注意 `to_string(unsigned/signed)` 给十进制数值，`to_string(std_logic_vector)` 给二进制位串**。
- `to_upper` / `to_lower` 做大小写；`to_std_logic_vector` 是 `str` 的逆操作，但只能解析 `0/1/Z/...` 字符，字母会被当成 `'X'`。

## 7. 下一步学习建议

- 想看 `print` 怎么写进**文件**、`str_read` 怎么从文件读变长行，请接着学 [u2-l2 文件 I/O 与 print 重载](u2-l2-txt-util-fileio.md)——它是位真仿真（u6）的前置。
- 想看本讲的 `str`/`hstr` 是怎么被拼进 `###ERROR###` 错误消息的，直接跳到 [u3-l1 基础比较过程](u3-l1-compare-basic.md)，你会看到 `StdlvCompareStdlv` 等过程如何复用这些转换函数。
- 如果你对 AXI 包里「十六进制字符串解析成 64 位 unsigned」更感兴趣，可以预习 [u5-l1 AXI 类型与字符串转换](u5-l1-axi-types-and-init.md)，那是 `to_std_logic_vector` 解决不了的、更大位宽的字符串→数值场景。
