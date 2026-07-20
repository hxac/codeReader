# 文件 I/O 与 print 重载

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `std.textio` 里的「三件套」——文件类型 `TEXT`、行缓冲 `line`、以及 `readline` / `writeline` / `read` / `write` 这四个原语——之间是如何配合的。
- 理解 `psi_tb_txt_util` 为什么在屏幕版 `print(text)` 之外，又额外提供了 `print(file ...)` 的重载，把同样的内容写进磁盘文件。
- 掌握 `str_read` 如何把文件里**一行变长文本**读进一个**定长字符串**，并知道它「先用空格清空、再逐字符读取、遇到行尾提前退出」的契约。
- 看懂 `str_write` 的逐字符实现，并能预判它在文件里产生的实际排版（一个值得亲自观察的细节）。
- 在一个最小 testbench 里同时向 Transcript 和磁盘文件写数据，并把写出的内容读回来核对。

本讲是 [u2-l1](u2-l1-txt-util-conversions.md) 的直接续篇：上一讲讲的是「如何把硬件值变成字符/字符串并打到屏幕」，本讲讲的是「如何把这些字符串进一步搬到文件里」。它也是后续 [u6 位真仿真（文本文件驱动）](u6-l1-textfile-driven-testing.md)的前置底座。

## 2. 前置知识

先回顾几个已经建立的概念：

- **testbench 只仿真、不可综合**（[u1-l1](u1-l1-project-overview.md)）。正因如此，psi_tb 可以放手使用 VHDL 的文件 I/O 能力——`std.textio` 本质上无法综合，但 testbench 永远不会进芯片，所以用得理直气壮。
- **Transcript 与 `output`**（[u2-l1](u2-l1-txt-util-conversions.md)）：屏幕版 `print` 里的 `writeline(output, ...)` 中，`output` 是 `std.textio` 预定义的一个 `TEXT` 文件，指向标准输出（ModelSim/GHDL 里就是 Transcript 窗口）。本讲要做的， essentially 是把「写到 `output`」换成「写到你自己声明的文件」。
- **`str` / `hstr` 拼字符串**（[u2-l1](u2-l1-txt-util-conversions.md)）：本讲的文件写入，写入的内容仍然是上一讲那些函数拼出来的字符串。换句话说，上一讲是「消息的字母表」，本讲是「消息的出口」。

一个对初学者很关键的小概念：VHDL 里**字符串 `string` 是定长的**（声明时就要确定长度，比如 `string(1 to 64)`），而文件里的**一行文本长度是任意的**。把「变长的一行」装进「定长的字符串」，正是 `str_read` 要解决的核心矛盾——记住这一点，后面的源码就好懂了。

## 3. 本讲源码地图

本讲只动一个文件，但它是整个库的文本底座：

| 文件 | 作用 |
| --- | --- |
| [hdl/psi_tb_txt_util.vhd](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd) | 文本工具包。本讲聚焦它的「file I/O」段：`str_read`、`print(file, string)`、`print(file, character)`、`str_write` 四个过程。 |

为了说明「这套文件 I/O 之后会被谁接走」，本讲还会顺带引用下游消费者：

| 文件 | 作用 |
| --- | --- |
| [hdl/psi_tb_textfile_pkg.vhd](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd) | 位真仿真用的文本文件驱动包。它同时 `use std.textio.all` 和 `use work.psi_tb_txt_util.all`，是本讲所讲能力的「上一层楼」。 |

引用方式与 [u2-l1](u2-l1-txt-util-conversions.md) 一致，只是用到文件 I/O 时通常还要再 `use std.textio.all`：

```vhdl
library IEEE;
use IEEE.std_logic_1164.all;
use IEEE.numeric_std.all;

library std;                       -- std.textio 必须先 library std
use std.textio.all;

library work;
use work.psi_tb_txt_util.all;
```

文件里和本讲相关的代码集中在两处：声明在 [hdl/psi_tb_txt_util.vhd:130-146](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L130-L146)，实现在 [hdl/psi_tb_txt_util.vhd:526-582](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L526-L582)（包体末尾的 `-- file I/O --` 段）。本讲按「声明 + 实现」成对引用。

## 4. 核心概念与源码讲解

### 4.1 std.textio 的三件套：TEXT、line、readline/writeline

#### 4.1.1 概念说明

VHDL 标准库 `std.textio` 提供了一套面向「文本行」的文件模型，核心是三样东西：

- **`TEXT`**：一种文件类型，可以理解为「一行一行字符串组成的文件」。`output`（标准输出 / Transcript）和 `input`（标准输入）是两个预定义的 `TEXT` 对象；我们自己也可以声明 `file f : TEXT;` 来代表一个磁盘文本文件。
- **`line`**：一个**指向字符串的指针**（`access string`），用来暂存「一行的内容」。它的大小是动态的，所以能装下任意长度的一行。
- **四个原语**：`readline(file, line)` 把文件里的一行读进 `line` 缓冲；`writeline(file, line)` 把 `line` 缓冲写进文件（并换行、释放缓冲）；`write(line, value)` 往 `line` 缓冲里追加内容；`read(line, value)` 从 `line` 缓冲里取出内容。

`psi_tb_txt_util` 顶部的 [hdl/psi_tb_txt_util.vhd:48](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L48) 就是 `use std.textio.all;`，把这四样东西引进来。本讲的所有过程都建立在这套模型之上。

#### 4.1.2 核心流程

「写」的流程是 `write` 累积 → `writeline` 落盘：

```
声明 variable l : line;            -- 一个空行缓冲
write(l, "某段文本");               -- 往缓冲里追加内容（可多次 write）
write(l, someSlv_as_string);       -- 继续追加
writeline(目标文件, l);             -- 把缓冲整体写出并换行，l 被释放
```

「读」的流程是 `readline` 取一整行 → `read` 逐个取值：

```
readline(源文件, l);                -- 把文件里一行整体读进 l
read(l, 某变量);                    -- 从 l 头部取一个值（按类型解析）
read(l, 某变量, ok);                -- 带状态版：取不到（行尾）时 ok=false，不报错
```

这里有两个要点初学者容易踩坑：

1. `line` 是指针，**用完一次 `writeline` 后就被释放**，所以每次写一行都要新建一个局部 `variable l : line;`，不要复用旧句柄。
2. 普通的 `read(l, v)` 在缓冲读空时会触发断言失败；带第三个布尔参数的 `read(l, v, ok)` 则在行尾平静地返回 `ok=false`。`str_read` 正是用这个「安静版」来安全地读完一整行的。

#### 4.1.3 源码精读

屏幕版 `print` 就是这套模型最朴素的例子，[hdl/psi_tb_txt_util.vhd:154-161](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L154-L161)：

```vhdl
procedure print(text: string) is
   variable msg_line: line;
begin
   --synopsys translate off
   write(msg_line, text);          -- 1) 字符串追加进行缓冲
   writeline(output, msg_line);    -- 2) 把整行写到 output（=Transcript），换行
   --synopsys translate on
end procedure print;
```

这段在 [u2-l1](u2-l1-txt-util-conversions.md) 已经细讲过，这里只强调一点：**把 `output` 换成任何你自己打开的 `TEXT` 文件，写法完全不变**。这正是后面 `print(file ...)` 重载的设计原型——它几乎就是把 `output` 换成一个形参 `out_file`。

#### 4.1.4 代码实践（源码阅读型）

**目标**：确认 `output` / `input` 是 `std.textio` 预定义的 `TEXT` 文件，理解 `print` 与「写文件」的等价性。

1. 打开 [hdl/psi_tb_txt_util.vhd:44-48](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L44-L48)，确认 `std.textio` 是怎么被引入的。
2. 对比屏幕版 `print`（L154-161）和后面要讲的文件版 `print`（L554-560），找出**唯一**的实质差别（答案：`writeline` 的第一个参数从 `output` 变成形参 `out_file`）。

**预期结果**：你会确信「写屏幕」和「写文件」是同一套机制，只是目标 `TEXT` 不同。

#### 4.1.5 小练习与答案

**练习 1**：`line` 类型为什么必须是 `access string`（指针）而不是普通 `string`？
**参考答案**：因为文件里一行的长度是运行时才知道的，而 VHDL 的普通 `string` 在声明时就必须定长。用指针指向一块动态分配的字符串，才能装下任意长度的一行。

**练习 2**：为什么每次 `print` 都要新建一个 `variable msg_line: line;`，而不是声明一个全局的复用？
**参考答案**：`writeline` 在写出后会释放（deallo­cate）这个 `line` 缓冲。复用一个已经释放的指针会出问题；每次新建局部变量最安全，也最清晰。

---

### 4.2 str_read：从文件读取变长行

#### 4.2.1 概念说明

`str_read` 解决的是一个很常见的需求：**把文件里的一行文本读进一个定长字符串**。难点在于「文件里的行可能比你的字符串短，也可能长」。

它的契约是：

- 调用方先声明一个定长字符串作为「结果缓冲」，例如 `variable s : string(1 to 64);`。
- `str_read(file, s)` 会把文件里**当前行**的内容填进 `s` 的前若干个字符。
- 如果这一行比 `s` **短**，多余的位置保持为空格（因为它先整体清空成空格）。
- 如果这一行比 `s` **长**，多出来的部分被丢弃（本次只读到 `s'length` 个字符，剩余字符留在内部行缓冲里——但 `str_read` 不会再次消费它）。

实际读到的有效字符数为 \(\min(\text{lineLen},\ N)\)，其中 \(N = s\text{'length}\)。

#### 4.2.2 核心流程

`str_read` 的执行过程可以拆成三步：

```
1. readline(in_file, l)              -- 把文件当前行整体读进行缓冲 l
2. for i in res_string'range loop    -- 先把结果字符串「清成全空格」
      res_string(i) := ' ';
3. for i in res_string'range loop    -- 再逐字符从 l 取出，填进结果
      read(l, c, is_string);          --   安静版读取：取不到时 is_string=false
      res_string(i) := c;
      exit when not is_string;        --   行尾：提前结束
```

注意第 2 步「先清空」非常关键：它保证了**短行的尾部一定是空格**，而不是上一次调用残留的旧字符。第 3 步用带 `is_string` 的 `read`，所以走到行尾时不会断言失败，而是干净地退出循环。

#### 4.2.3 源码精读

**`str_read` 的声明**——它把文件对象作为 `file` 形参传入（[hdl/psi_tb_txt_util.vhd:134-135](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L134-L135)）：

```vhdl
procedure str_read(file in_file: TEXT; 
                   res_string: out string);
```

`file in_file: TEXT` 是 VHDL 的**文件形参**语法——调用方把自己打开的 `TEXT` 文件句柄传进来，过程内部对它的读写就作用在那个真实文件上。

**`str_read` 的实现**在 [hdl/psi_tb_txt_util.vhd:531-551](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L531-L551)：

```vhdl
procedure str_read(file in_file: TEXT; 
                   res_string: out string) is
   variable l         : line;
   variable c         : character;
   variable is_string : boolean;
begin
   readline(in_file, l);                       -- 读整行进行缓冲
   -- clear the contents of the result string
   for i in res_string'range loop
       res_string(i):=' ';                     -- 先清成全空格
   end loop;   
   for i in res_string'range loop
       read(l,c,is_string);                    -- 安静版：逐字符取
       res_string(i):=c;
       if not is_string then                   -- 行尾
          exit;
       end if;
   end loop; 
end procedure str_read;
```

几个要点：

- `readline(in_file, l)`（[L537](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L537)）一次读一整行进 `l`；`str_read` 因此是「按行」推进的，每调用一次，文件读指针前进一行。
- 清空循环（[L539-541](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L539-L541)）覆盖**整个** `res_string'range`，所以短行的尾巴一定是空格。
- 读取循环（[L544-550](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L544-L550)）用 `read(l, c, is_string)` 的三参数版；`is_string` 为 `false` 即行尾，立刻 `exit`。这正是「安全读完一行」的关键。

> 说明：仓库内目前没有直接调用 `str_read` 的 testbench（位真仿真包 `psi_tb_textfile_pkg` 走的是自己更专门的整数列读取实现，见 [u6](u6-l1-textfile-driven-testing.md)）。`str_read` 是 txt_util 暴露给用户、供自定义文件读取场景使用的低层工具——本讲的实践里我们会亲自用它。

#### 4.2.4 代码实践（参数观察型）

**目标**：体会「缓冲长度 N」对 `str_read` 结果的影响。

1. 准备一个 `data.txt`，第一行写 `Hello psi_tb`（11 个字符）。
2. 在 testbench 里分别声明 `variable s8 : string(1 to 8);` 和 `variable s64 : string(1 to 64);`，对同一行各调用一次 `str_read`（注意每读一次要重新 `file_open` 到 `read_mode`，或读两行）。
3. 把两个结果用屏幕版 `print` 打出来。

**需要观察的现象**：

- `s8` 里只会有前 8 个字符 `Hello ps`（被截断）。
- `s64` 里前 11 个字符是 `Hello psi_tb`，**第 12-64 位是空格**（因为先清空过）。

**预期结果**：你将直观看到 \(\min(\text{lineLen},\ N)\) 这条规则，以及「短行尾部填空格」的契约。若你没有本地仿真器，这一段标注为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `read(l, c, is_string)` 换成两参数的 `read(l, c)`，当某一行比缓冲短时会发生什么？
**参考答案**：两参数版 `read` 在缓冲读空（行尾）时会触发断言失败（仿真报错/停止）。三参数版通过 `is_string=false` 平静返回，这是 `str_read` 能安全读完任意短行的前提。

**练习 2**：为什么源码里「清空」和「读取」用的是**同一个** `res_string'range` 循环范围？
**参考答案**：因为要保证「最多读 N 个字符、且整段缓冲都被覆盖过」。清空用同一个范围确保短行尾部一定是空格（而不是旧数据），读取用同一个范围确保不会越界写 `res_string`。

**练习 3**：`str_read` 一次能读几行？
**参考答案**：一行。它内部只调用了一次 `readline`，每调用一次 `str_read`，文件读指针前进一行；要读多行就多次调用。

---

### 4.3 向文件写出的两条路：print(file) 重载与 str_write

#### 4.3.1 概念说明

把数据**写进文件**有两条路，都封装在 txt_util 里：

- **`print(file, string)`**：把一整段字符串当作**一行**写进文件，并在末尾换行。它和屏幕版 `print` 几乎一模一样，只是目标从 `output` 换成你传入的文件。
- **`print(file, character)`**：把**单个字符**当作一行写进文件（写一个字符就换行）。
- **`str_write(file, string)`**：逐字符地把一个字符串写进文件，遇到 `LF`（换行符）停止。

`print(file, string)` 是日常用得最多的：你要往报告文件里写一行日志，就调它。`str_write` 则更像一个「低层拼装」工具，行为有个值得注意的细节（见 4.3.2）。

#### 4.3.2 核心流程

`print(file, string)` 的流程和屏幕版 `print` 完全同构：

```
write(l, new_string);     -- 字符串追加进缓冲
writeline(out_file, l);   -- 整行写出并换行
```

`str_write` 的流程则值得画出来看清楚：

```
for i in new_string'range loop
   print(out_file, new_string(i));   -- 对每个字符调用「单字符版 print」
   exit when new_string(i) = LF;     -- 遇到换行符停止
end loop;
```

这里有一个**需要在实践里亲自观察的细节**：因为 `str_write` 对每个字符都调用 `print(file, character)`，而后者每次都会 `writeline`（写完即换行），所以 `str_write("ABC")` 在文件里实际会产生 **3 行**——`A`、`B`、`C` 各占一行，而不是一行里的 `ABC`。它在遇到字符串里的 `LF` 字符时才会停止。这不是笔误，而是源码的真实行为（[hdl/psi_tb_txt_util.vhd:573-582](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L573-L582)）；要写「一整行」请用 `print(file, string)`。

> 提示：日常记日志、写报告，**用 `print(file, string)`**；`str_write` 适用于需要逐字符控制、或天然以 `LF` 分段的特殊场景。本讲的综合实践也以 `print(file, string)` 为主。

#### 4.3.3 源码精读

**`print(file, string)`——写一行字符串**，[hdl/psi_tb_txt_util.vhd:554-560](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L554-L560)：

```vhdl
procedure print(file out_file: TEXT;
                new_string: in  string) is
   variable l: line;
begin
   write(l,new_string);
   writeline(out_file,l);
end procedure print;
```

把它和屏幕版 [L154-161](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L154-L161) 并排看：结构完全一致，只是 `writeline` 的目标从 `output` 变成了形参 `out_file`。

**`print(file, character)`——写一个字符就换行**，[hdl/psi_tb_txt_util.vhd:563-569](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L563-L569)：

```vhdl
procedure print(file out_file: TEXT;
                char: in  character) is
   variable l: line;
begin
   write(l,char);
   writeline(out_file,l);
end procedure print;
```

注意：哪怕只写一个字符，它也会 `writeline`，即**每个字符独占一行**。这个性质直接决定了 `str_write` 的排版。

**`str_write`——逐字符写直到 LF**，[hdl/psi_tb_txt_util.vhd:571-582](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L571-L582)：

```vhdl
-- appends contents of a string to a file until line feed occurs
-- (LF is considered to be the end of the string)
procedure str_write(file out_file: TEXT; 
                    new_string: in  string) is
begin
   for i in new_string'range loop
      print(out_file,new_string(i));      -- 每字符一次 writeline
      if new_string(i)=LF then            -- 遇到 LF 停止
         exit;
      end if;
   end loop;               
end str_write;
```

`LF` 是 `std.textio` 里定义的换行符常量（`character` 类型）。`str_write` 一旦在字符串里撞到 `LF` 就停止，不再写后续字符。

#### 4.3.4 代码实践（行为观察型）

**目标**：亲眼看到 `print(file, string)` 与 `str_write` 在文件里的不同排版。

1. 在一个 testbench 里 `file_open` 一个 `out.txt`（`write_mode`）。
2. 先 `print(reportFile, "LINE")`，再 `str_write(reportFile, "LINE")`，然后 `file_close`。
3. 仿真结束后打开 `out.txt`。

**需要观察的现象**：

- `print(reportFile, "LINE")` 产生 1 行：`LINE`。
- `str_write(reportFile, "LINE")` 产生 4 行：`L` / `I` / `N` / `E`，每个字符一行。

**预期结果**：文件总共 5 行（1 行 `LINE` + 4 行单字符）。如果你看到的就是这样，说明你理解了 `str_write` 的逐字符实现。无本地仿真器时标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `print(file, string)` 写 `ABC` 得到一行 `ABC`，而 `str_write` 写 `ABC` 得到三行？
**参考答案**：`print(file, string)` 只 `writeline` 一次，整段字符串作为一行；`str_write` 对每个字符调用 `print(file, character)`，而后者每次都 `writeline`，所以每个字符独占一行。

**练习 2**：`str_write` 会在什么情况下提前停止？
**参考答案**：当遍历到的字符等于 `LF`（`std.textio` 的换行符常量）时，执行 `exit`，停止写出后续字符。

**练习 3**：要往报告文件里写一行普通日志，应该选哪个过程？
**参考答案**：选 `print(file, string)`。它把整段字符串作为一行写出并换行，最符合「写一行日志」的语义。

---

## 5. 综合实践

把第 4 节的知识串起来：写一个最小 testbench，**把每个要打印的消息同时送到 Transcript 和磁盘文件 `report.txt`**，然后用 `str_read` 把写出的第一行**读回来**核对。这正好是本讲规格里要求的实践任务。

**实践目标**

- 同时用屏幕版 `print` 与文件版 `print(file, ...)`。
- 用 `file_open` / `file_close` 管理一个 `TEXT` 文件句柄。
- 用 `str_read` 把写出的内容读回，验证「写进去的 = 读出来的」。

**操作步骤**

1. 在仓库的 testbench 目录（或你的本地仿真工作目录）新建一个 TB 文件，内容如下（**示例代码**，非仓库原有文件）：

```vhdl
-- 示例代码：演示 psi_tb_txt_util 的文件 I/O
library IEEE;
use IEEE.std_logic_1164.all;
use IEEE.numeric_std.all;

library std;
use std.textio.all;

library work;
use work.psi_tb_txt_util.all;

entity txt_util_fileio_tb is
end entity txt_util_fileio_tb;

architecture sim of txt_util_fileio_tb is
begin
   p_main : process
      file     reportFile : TEXT;                       -- 文件句柄
      variable vSlv       : std_logic_vector(11 downto 0) := x"A3C";
      variable vBuf       : string(1 to 64);            -- str_read 的定长缓冲
      variable vLen       : integer;
   begin
      -- 1) 打开文件（write_mode 会清空已有内容）
      file_open(reportFile, "report.txt", write_mode);

      -- 2) 每条消息：屏幕 + 文件双写
      print("=== txt_util file I/O demo ===");
      print(reportFile, "=== txt_util file I/O demo ===");

      print("dec = " & str(to_integer(unsigned(vSlv))));
      print(reportFile, "dec = " & str(to_integer(unsigned(vSlv))));

      print("hex = 0x" & hstr(vSlv));
      print(reportFile, "hex = 0x" & hstr(vSlv));

      print("bin = " & str(vSlv));
      print(reportFile, "bin = " & str(vSlv));

      -- 3) 关闭写句柄
      file_close(reportFile);

      -- 4) 回读第一行，演示 str_read（去掉尾部空格填充）
      file_open(reportFile, "report.txt", read_mode);
      str_read(reportFile, vBuf);
      file_close(reportFile);

      vLen := vBuf'length;
      while vLen > 0 and then vBuf(vLen) = ' ' loop   -- 去掉尾部空格
         vLen := vLen - 1;
      end loop;
      print("READ BACK (" & str(vLen) & " chars): " & vBuf(1 to vLen));

      print("SIMULATIONS COMPLETED SUCCESSFULLY");
      wait;
   end process;
end architecture sim;
```

2. 按 [u1-l3](u1-l3-simulation-and-ci.md) 讲过的方式跑仿真（PsiSim 的 `run.tcl` 或 `runGhdl.tcl`），或直接在你的仿真器里编译运行。注意：需要先把 `psi_tb_txt_util` 编译进 `work` 库（它在 `sim/config.tcl` 的 `src` 段里，见 [u1-l2](u1-l2-repository-structure.md)）。

3. 仿真结束后，到仿真工作目录里打开生成的 `report.txt`。

**需要观察的现象**

- Transcript 里出现 7 行（3 条 `===/dec/hex/bin` + 对应的回读行 + 成功标记）。
- `report.txt` 里出现 4 行，内容和屏幕前 4 行一致：

  ```
  === txt_util file I/O demo ===
  dec = 2620
  hex = 0xA3C
  bin = 101000111100
  ```

- 回读那一行形如 `READ BACK (29 chars): === txt_util file I/O demo ===`——证明 `str_read` 把第一行正确读回（`29` 是 `=== txt_util file I/O demo ===` 的字符数；如果你的缓冲更短会被截断）。

**预期结果**

- `dec = 2620`（因为 `x"A3C"` = 12'hA3C = 2620）。
- `hex = 0xA3C`、`bin = 101000111100`。
- 回读内容与写入的第一行一致，证明「写 → 关闭 → 重新打开读 → str_read」这条链路通畅。
- 如果你没有本地仿真器， Transcript 与 `report.txt` 的确切内容请标注「待本地验证」；但代码逻辑、`str_read` 的尾部空格契约、`str_write` 的逐字符行为都可以通过阅读源码直接推断。

> 进阶（可选）：把第 4 步的回读改成**循环读完 `report.txt` 的全部 4 行**，每行 `str_read` 一次并打印；体会「每调用一次 `str_read` 文件指针前进一行」。

## 6. 本讲小结

- `std.textio` 的三件套是 `TEXT`（文本文件类型）、`line`（指向字符串的行缓冲指针）、以及 `readline`/`writeline`/`read`/`write` 四个原语；屏幕版 `print` 写的 `output` 本身就是一个预定义的 `TEXT`。
- `print(file, string)` 与屏幕版 `print` 几乎同构，唯一实质差别是 `writeline` 的目标从 `output` 换成了调用方传入的文件句柄——「写屏幕」和「写文件」是同一套机制。
- `str_read` 把文件里**一行变长文本**读进**定长字符串**：先整段清成空格，再用带 `is_string` 的「安静版」`read` 逐字符读取，行尾平静退出；有效字符数为 \(\min(\text{lineLen},\ N)\)，短行尾部为空格。
- `str_write` 逐字符写出，遇到 `LF` 停止；由于它对每个字符调用 `print(file, character)`（每字符即 `writeline`），所以会把字符串拆成「每字符一行」——要写整行请用 `print(file, string)`。
- 文件 I/O 是 testbench 专属能力的体现：`std.textio` 不可综合，但 psi_tb 永不被综合，所以可以放心使用。
- 这套低层文件 I/O 是 [u6 位真仿真](u6-l1-textfile-driven-testing.md) 的前置：`psi_tb_textfile_pkg` 同时 `use std.textio.all` 与 `use work.psi_tb_txt_util.all`（见 [hdl/psi_tb_textfile_pkg.vhd:30-34](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L30-L34)），在本讲之上搭建了整数列文本的施加/比对/导出。

## 7. 下一步学习建议

- 想看「文件 I/O 怎么被用来驱动一整个位真仿真」，接着学 [u6 文本文件驱动测试（psi_tb_textfile_pkg）](u6-l1-textfile-driven-testing.md)：它的 `ApplyTextfileContent` / `CheckTextfileContent` / `WriteTextfile` 正是建立在本讲讲的 `std.textio` 模型之上，把每行整数列施加到信号、再逐列比对、最后导出结果文件。
- 想看「拼出来的字符串怎么变成可读错误消息」，去学 [u3 比较与检查助手（psi_tb_compare_pkg）](u3-l1-compare-basic.md)：那里的 `IntCompare` / `StdlvCompareStdlv` 用上一讲的 `str`/`hstr` 拼消息、用本讲的 `print`（屏幕版）输出 `###ERROR### ...`。
- 如果你对「这些字符串最终怎么被 CI 当作通过/失败依据」感兴趣，回顾 [u1-l3](u1-l3-simulation-and-ci.md) 里 `run_check_errors "###ERROR###"` 与 `ciFlow.py` 的 Transcript 扫描——那里讲清了本讲写出的文本是如何被自动化判定的。
