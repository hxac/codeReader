# 位宽计算：clogb2 与 $clog2

## 1. 本讲目标

读完本讲，你应当能够：

- 说清楚仓库自带的 `clogb2()` 函数到底在算什么，并能手动追踪它的执行过程。
- 看懂 `clogb2.svh` 里那张 `clogb2` 与系统函数 `$clog2` 的对照表，并理解两者在 2 的幂次边界上差 1 的原因。
- 牢记一条核心等式：`clogb2(n) == $clog2(n+1)`。
- 在新设计里正确回答两个最常被搞混的问题——「寻址 DEPTH 个表项要几 bit」与「装下 0..DEPTH 的计数器要几 bit」，也就是何时用 `$clog2(DEPTH)`、何时用 `$clog2(DEPTH+1)`。

本讲是 u2 单元里最「数学」的一篇，但只用到对数和位运算的直觉，不涉及任何时序逻辑。

## 2. 前置知识

在进入正题前，先建立两个直觉。

**直觉一：表示一个数需要几 bit？**
一个无符号整数 \(N\)（\(N \ge 1\)）至少需要多少 bit 来表示？答案是 \(\lfloor \log_2 N \rfloor + 1\)。例如 \(8 = 1000_2\) 需要 4 bit，\(7 = 111_2\) 需要 3 bit。

**直觉二：在 N 个表项里选一个，需要几 bit 地址？**
要在 \(N\) 个表项（编号 \(0 \sim N-1\)）里任选一个，需要 \(\lceil \log_2 N \rceil\) bit。例如 8 个表项需要 3 bit 地址（\(000 \sim 111\)），7 个表项同样需要 3 bit（因为 \(2^2=4 < 7 \le 8=2^3\)）。

注意这两个问题「差一个 +1」：表示数值 \(N\) 本身，与在 \(N\) 个表项里选址，bit 数并不总相同。本讲的全部分歧都来自这里。

> 阅读提示：本讲出现的 `$clog2` 是 SystemVerilog 的**系统函数**（system function），始终用反引号包起来写；而 `clogb2` 是仓库自定义的**普通函数**（function）。不要把 `$` 当成数学符号。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [clogb2.svh](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/clogb2.svh) | 一个 Verilog-2001 风格的 `function integer clogb2`，以及它的使用说明、与 `$clog2` 的对照表。用 `\`include "clogb2.svh"` 引入。 |
| [true_single_port_write_first_ram.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/true_single_port_write_first_ram.sv) | 单口 RAM 模板。它的地址端口宽度用 `clogb2(RAM_DEPTH-1)` 计算——「地址位宽」用法的活样本。 |
| [fifo_single_clock_ram.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram.sv) | 单时钟 FIFO。它的计数器/指针宽度写作 `clogb2(DEPTH)+1`，是一段「能用但不精确」的历史写法，正好用来说明为什么作者劝你别再用 `clogb2`。 |
| [lifo.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/lifo.sv) / [preview_fifo.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/preview_fifo.sv) | 两个「现代写法」对照样本：分别用 `$clog2(DEPTH+1)` 算计数器宽度、`$clog2(DEPTH)` 算已用量宽度。 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：先搞懂 `clogb2` 函数本身（4.1），再认识现代标准 `$clog2` 并对比两者（4.2），最后落到工程师最常踩坑的「地址位宽 vs 计数位宽」的 +1 分界（4.3）。

### 4.1 clogb2 函数：它到底在算什么

#### 4.1.1 概念说明

`clogb2` 是仓库自带的工具函数，诞生于 Verilog-2001 年代——那时系统函数 `$clog2` 还没有被广泛支持、或实现得有 bug，于是大家手写一个等价函数凑合用。它的用途写在了文件头：

> Calculates counter width based on specified vector/RAM depth（根据给定的向量/RAM 深度计算计数器宽度）。

通俗讲：**输入一个「深度」`depth`，返回一个整数，代表需要的 bit 数。** 但它具体返回的是「表示 `depth` 这个数值本身所需的 bit 数」，这一点很关键，正是 4.3 节 +1 困惑的根源。

#### 4.1.2 核心流程

函数体只有一个 `for` 循环：不断把 `depth` 右移一位，每移一次计数器 `clogb2` 加 1，直到 `depth` 被移成 0。伪代码如下：

```
function clogb2(depth):
    result = 0
    while depth > 0:
        depth = depth >> 1     # 右移一位
        result = result + 1
    return result
```

追踪 `depth = 8` 的执行过程：

| 迭代 | 进入时 depth | 右移后 depth | result |
| --- | --- | --- | --- |
| 1 | 8 (1000) | 4 (100) | 1 |
| 2 | 4 (100) | 2 (10) | 2 |
| 3 | 2 (10) | 1 (1) | 3 |
| 4 | 1 (1) | 0 | 4 |

循环结束时 `result = 4`，正好是「把 8 这个数右移到 0 需要的次数」，也正好是 8 的二进制 `1000` 的位数。

写成数学式，对 \(n \ge 1\)：

\[
\text{clogb2}(n) \;=\; \lfloor \log_2 n \rfloor + 1
\]

而 \(n = 0\) 时循环一次都不执行，返回 0。

#### 4.1.3 源码精读

函数本体只有 8 行：

[clogb2.sv:L52-L59](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/clogb2.svh#L52-L59) —— `clogb2` 函数定义。`function integer clogb2` 声明返回类型为 `integer`；`input [31:0] depth` 是它的形参（注意这是 Verilog-2001 的非 ANSI 写法，端口在函数体内声明）；`for` 循环把 `clogb2` 这个**与函数同名的变量**当作累加器，从 0 开始，每次右移 `depth` 并自增。

```verilog
function integer clogb2;
  input [31:0] depth;

  for( clogb2=0; depth>0; clogb2=clogb2+1 ) begin
    depth = depth >> 1;
  end

endfunction
```

> 细节：Verilog 规定函数体内可以有一个与函数同名的变量，对它赋值就是「设置返回值」。所以 `for( clogb2=0; ...; clogb2=clogb2+1 )` 既初始化了返回值，又在循环里累加它。

它通过 `\`include "clogb2.svh"` 被塞进使用方模块的末尾，例如 [true_single_port_write_first_ram.sv:L93](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/true_single_port_write_first_ram.sv#L93) 和 [fifo_single_clock_ram.sv:L183](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram.sv#L183)。

#### 4.1.4 代码实践（手算追踪）

**实践目标**：不用仿真器，先用纸笔验证 `clogb2` 的行为，建立「它返回的是表示数值本身的位数」的直觉。

**操作步骤**：

1. 对下表的每个 `depth`，写出二进制，数其位数。
2. 再按 4.1.2 的循环逻辑，手算 `clogb2(depth)`。
3. 比较两列是否相等。

| depth | 二进制 | 位数 | 手算 clogb2(depth) |
| --- | --- | --- | --- |
| 1 | 1 | 1 | ? |
| 4 | 100 | 3 | ? |
| 7 | 111 | 3 | ? |
| 8 | 1000 | 4 | ? |
| 16 | 10000 | 5 | ? |

**预期结果**：两列完全一致，分别是 1、3、3、4、5。

**需要观察的现象**：注意 `depth=7` 和 `depth=8`——前者是 3 位，后者是 4 位，`clogb2` 在这里跳变了。这个「在 2 的幂次边界跳变」的行为，正是 4.2 节对照表里 `clogb2` 与 `$clog2` 错位的来源。

#### 4.1.5 小练习与答案

**练习 1**：`clogb2(0)` 等于多少？为什么？

> **答案**：等于 0。因为循环条件是 `depth>0`，输入 0 时循环体一次都不执行，`clogb2` 保持初值 0。

**练习 2**：不参考对照表，推算 `clogb2(13)`。

> **答案**：\(13 = 1101_2\)，是 4 位数；按循环：13→6→3→1→0 共右移 4 次，结果 4。两种方式都得 4。

---

### 4.2 $clog2 系统函数：现代标准做法

#### 4.2.1 概念说明

`$clog2` 是 IEEE 1364-2005 起标准化、SystemVerilog（IEEE 1800）内建的**系统函数**。它无需 `\`include`，所有主流综合器（Quartus、Vivado、 Gowin）和仿真器（iverilog `-g2012`、ModelSim、Xcelium）都支持。

它的定义是「log₂ n 向上取整」，对 \(n \ge 1\)：

\[
\text{\$clog2}(n) \;=\; \lceil \log_2 n \rceil
\]

并规定 `$clog2(0) = 0`。语义上，它回答的是「**在 n 个表项里任选一个，需要几 bit 地址**」：8 个表项要 3 bit，1 个表项要 0 bit（只有一个候选，不需要地址）。

正因为 `$clog2` 语义清晰、无需 include，[clogb2.sv:L16-L17](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/clogb2.svh#L16-L17) 明确写道：**别再用 `clogb2` 做新设计**（"don't use clogb2() for new designs!"）。

#### 4.2.2 核心流程（对照表）

文件里给出了一张 `clogb2` 与 `$clog2` 的逐值对照表。读懂它，是本讲的核心。

| depth | `$clog2(depth)` | `clogb2(depth)` |
 | ---: | ---: | ---: |
 | 0 | 0 | 0 |
 | 1 | 0 | 1 |
 | 2 | 1 | 2 |
 | 3 | 2 | 2 |
 | 4 | 2 | 3 |
 | 5 | 3 | 3 |
 | 6 | 3 | 3 |
 | 7 | 3 | 3 |
 | 8 | 3 | 4 |
 | 9 | 4 | 4 |
 | … | … | … |
 | 15 | 4 | 4 |
 | 16 | 4 | 5 |

这张表透露三个事实：

1. **`clogb2(depth) \ge $clog2(depth)` 恒成立**，且 `clogb2` 总是「更大或相等」。
2. **在 2 的幂次处两者差 1**：`depth=1,2,4,8,16` 时 `clogb2` 比 `$clog2` 多 1。
3. **把 `$clog2` 的输入加 1，就和 `clogb2` 完全相同**：例如 `$clog2(8+1) = $clog2(9) = 4 = clogb2(8)`。

由此得到贯穿本讲的核心等式：

\[
\boxed{\;\text{clogb2}(n) \;=\; \text{\$clog2}(n+1)\;}
\]

这条等式对所有 \(n \ge 0\) 成立。**记住它，就能在两套写法之间随时换算。**

#### 4.2.3 源码精读

对照表与使用建议都在文件头注释里：

[clogb2.sv:L25-L43](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/clogb2.svh#L25-L43) —— `clogb2` 与 `$clog2` 的逐值对照表（即上表的来源）。

[clogb2.sv:L18-L22](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/clogb2.svh#L18-L22) —— 作者给新设计的两条规则：地址指针用 `$clog2(DEPTH)`，计数器用 `$clog2(DEPTH+1)`（4.3 节详解）。

仓库里较新的模块已经普遍改用 `$clog2`，例如：

[lifo.sv:L75](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/lifo.sv#L75) —— `localparam DEPTH_W = $clog2(DEPTH+1);`，用现代写法声明 LIFO 的元素计数器宽度，干净利落。

[preview_fifo.sv:L49](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/preview_fifo.sv#L49) —— `USED_W = $clog2(DEPTH)`，用 `$clog2(DEPTH)`（注意没有 +1）声明一个用于寻址/已用量指示的宽度。

#### 4.2.4 代码实践（testbench 打印对照表）

**实践目标**：用仿真器同时调用 `$clog2` 和 `clogb2`，把 `DEPTH=1..16` 三列数值（`$clog2(DEPTH)`、`$clog2(DEPTH+1)`、`clogb2(DEPTH)`）打印出来，亲眼验证 4.2.2 的核心等式。这正是本讲规格里要求的实践任务。

**操作步骤**：

1. 在仓库根目录新建下面的 testbench（**示例代码**，非项目原有文件）。注意为了让 testbench 能调用 `clogb2`，必须在 module 内 `\`include "clogb2.svh"`，和 [fifo_single_clock_ram.sv:L183](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram.sv#L183) 的做法一致。

```verilog
// 示例代码：clogb2_width_tb.sv（需放在仓库根目录，便于 include 找到 clogb2.svh）
`timescale 1ns / 1ps

module clogb2_width_tb;

  integer d;
  integer c2_d, c2_dp1, cb2;

  initial begin
    $display(" d | $clog2(d) | $clog2(d+1) | clogb2(d) | clogb2(d)==$clog2(d+1)?");
    for (d = 1; d <= 16; d = d + 1) begin
      c2_d   = $clog2(d);
      c2_dp1 = $clog2(d + 1);
      cb2    = clogb2(d);
      $display("%2d |   %2d      |   %2d        |   %2d      | %s",
               d, c2_d, c2_dp1, cb2, (cb2 == c2_dp1) ? "YES" : "no");
    end
    $finish;
  end

  `include "clogb2.svh"   // 引入 clogb2 函数

endmodule
```

2. 用 iverilog 编译并运行（参考 [scripts/iverilog_compile.bat](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/scripts/iverilog_compile.bat) 的 `-g2012` 选项）：

```bash
iverilog -g2012 -o clogb2_tb.vvp clogb2_width_tb.sv
vvp clogb2_tb.vvp
```

**预期结果**：终端打印 16 行，最后一列恒为 `YES`，证明 `clogb2(d) == $clog2(d+1)`。前三列应与 4.2.2 的对照表完全吻合（例如 `d=8` 行为 `3 | 4 | 4 | YES`）。

**需要观察的现象**：

- 最后一列全为 `YES`——核心等式成立。
- 第二列 `$clog2(d)` 在 `d=8` 时仍是 3，第三列 `$clog2(d+1)` 在 `d=8` 时已是 4——这正是「地址位宽」与「计数位宽」的分界，下一节详述。

> 待本地验证：不同仿真器对 `$clog2(0)` 的返回与 `$display` 中 `%s` 配合三元运算符的格式化细节可能略有差异；若 `%s` 报错，可把三元结果换成 `$display` 的条件分支打印。

#### 4.2.5 小练习与答案

**练习 1**：用核心等式，把 `clogb2(100)` 换算成 `$clog2(?)`。

> **答案**：`clogb2(100) = $clog2(101)`。验证：\(64 < 101 \le 128\)，所以 \(\lceil \log_2 101 \rceil = 7\)；而 \(100 = 1100100_2\) 是 7 位数，`clogb2(100)` 也是 7，一致。

**练习 2**：为什么 `$clog2(1)` 等于 0 而不是 1？

> **答案**：`$clog2` 回答「在 n 个表项里选址需要几 bit」。只有 1 个表项时无需任何地址位（它就是唯一的那一个），所以返回 0。

---

### 4.3 地址位宽 vs 计数位宽：那个 +1 的分界

#### 4.3.1 概念说明

这是工程里最容易写错的地方。同样是「深度 DEPTH」，两种用途需要的 bit 数不同：

- **地址指针**（如 RAM 的 `addr`、FIFO 的读写指针）：取值范围是 \(0 \sim \text{DEPTH}-1\)，所以位宽 = `$clog2(DEPTH)`。
- **计数器**（如 FIFO/LIFO 的元素个数 `cnt`）：取值范围是 \(0 \sim \text{DEPTH}\)（**含两端**，满时正好等于 DEPTH），所以位宽 = `$clog2(DEPTH+1)`。

地址最大只到 DEPTH−1，计数器却要能装下 DEPTH 本身——这就是「+1」的来历。

换算到 `clogb2`（套用核心等式）：

| 用途 | 取值范围 | 正确位宽 | 用 clogb2 表达 |
| --- | --- | --- | --- |
| 地址指针 | 0 .. DEPTH−1 | `$clog2(DEPTH)` | `clogb2(DEPTH-1)` |
| 计数器 | 0 .. DEPTH | `$clog2(DEPTH+1)` | `clogb2(DEPTH)` |

> 一句话防错：**`clogb2(DEPTH)` 不是地址位宽，而是计数位宽。** 想用 `clogb2` 写地址位宽，得写 `clogb2(DEPTH-1)`。

#### 4.3.2 核心流程

给定一个深度 DEPTH，按下面决策树选位宽：

```
我要这个宽度做什么？
├─ 索引存储器表项（地址/指针）   →  位宽 = $clog2(DEPTH)      [= clogb2(DEPTH-1)]
└─ 记录已用元素个数（计数器）     →  位宽 = $clog2(DEPTH+1)    [= clogb2(DEPTH)]
```

举例 DEPTH = 8：

- 地址需要 `$clog2(8) = 3` bit（`000~111`，索引 8 个表项）。
- 计数器需要 `$clog2(9) = 4` bit（要能表示 0..8，而 8 = `1000` 需要 4 bit）。

#### 4.3.3 源码精读

仓库里同时存在「老写法」和「新写法」两个活样本，对照阅读最有收获。

**地址位宽（老写法，但结果正确）**：

[true_single_port_write_first_ram.sv:L41](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/true_single_port_write_first_ram.sv#L41) —— 单口 RAM 的地址端口声明为 `input [clogb2(RAM_DEPTH-1)-1:0] addra`。这里故意传 `RAM_DEPTH-1`：因为 `clogb2(DEPTH-1) = $clog2(DEPTH)`，正好得到正确的地址位宽。双口 RAM [true_dual_port_write_first_2_clock_ram.sv:L48](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/true_dual_port_write_first_2_clock_ram.sv#L48) 的 `addra`/`addrb` 也是同一写法。

**计数位宽（老写法，结果略宽）**：

[fifo_single_clock_ram.sv:L58](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram.sv#L58) —— FIFO 把计数器和指针统一声明为 `DEPTH_W = clogb2(DEPTH)+1`。以 DEPTH=8 为例：`clogb2(8)+1 = 4+1 = 5` bit。但「装 0..8 的计数器」严格只需 `$clog2(9) = 4` bit，「索引 8 个表项的地址」只需 3 bit——所以 5 bit **比实际需要多出了 1 位**。代码能正常工作，是因为 `cnt` 通过显式比较 `==DEPTH`（满）和 `=='0`（空）来判断状态、指针通过 `inc_ptr` 里显式判断 `==DEPTH-1` 来回绕（见 [fifo_single_clock_ram.sv:L165-L181](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram.sv#L165-L181)），多出来的高位不参与运算。这正是一个绝佳的反面教材：用 `clogb2` 推算位宽很容易「差一」，虽然此处因冗余而无害，但作者仍把它标注为不推荐。

**计数位宽（新写法，推荐）**：

[lifo.sv:L75](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/lifo.sv#L75) —— `localparam DEPTH_W = $clog2(DEPTH+1);`。直接、精确地表达了「计数器要装 0..DEPTH」的需求，没有任何冗余，意图一目了然。这就是 [clogb2.sv:L21-L22](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/clogb2.svh#L21-L22) 推荐的写法。

#### 4.3.4 代码实践（选对位宽）

**实践目标**：自己设计一个参数化 ROM，分别用「地址位宽」和「计数位宽」两类端口，练一下 +1 的取舍。

**操作步骤**：

1. 新建一个 `my_rom.sv`（**示例代码**），声明一个 `DEPTH` 表项的 ROM，端口按本节规则命名：

```verilog
// 示例代码：my_rom.sv
module my_rom #( parameter
  DEPTH   = 10,                       // 10 个表项（非 2 的幂，更能考验位宽）
  DATA_W  = 8
)(
  input  [$clog2(DEPTH)-1:0]    addr, // 地址：0..DEPTH-1，用 $clog2(DEPTH)
  output [DATA_W-1:0]           q
);
  logic [DATA_W-1:0] mem [0:DEPTH-1];
  assign q = mem[addr];
endmodule
```

2. 在心里回答：若 DEPTH=10，`addr` 几 bit？若再要一个「已编程表项数」计数器（0..DEPTH），几 bit？

**预期结果**：

- `addr` 宽度 = `$clog2(10) = 4` bit（\(2^3=8 < 10 \le 16=2^4\)，需 4 bit 索引 0..9）。
- 计数器宽度 = `$clog2(11) = 4` bit（0..10，10=`1010` 需 4 bit；恰好这里与地址同为 4 bit，但语义不同）。

**需要观察的现象**：把 `DEPTH` 改成 8 试试——`addr` 变成 `$clog2(8)=3` bit，而计数器仍是 `$clog2(9)=4` bit，此时两者差 1，能清楚看到「+1」的效果。

#### 4.3.5 小练习与答案

**练习 1**：DEPTH=8 的 FIFO，用 `clogb2(DEPTH)+1`（仓库老写法）得到的 `DEPTH_W` 是多少？严格的计数器最小位宽是多少？多出几位？

> **答案**：`clogb2(8)+1 = 5` bit；严格计数器位宽 = `$clog2(9) = 4` bit；多出 1 位。

**练习 2**：为什么 RAM 模板写 `clogb2(RAM_DEPTH-1)` 而不是 `clogb2(RAM_DEPTH)` 作为地址位宽？

> **答案**：地址只需索引 0..RAM_DEPTH−1，位宽 = `$clog2(RAM_DEPTH)` = `clogb2(RAM_DEPTH-1)`。若误写成 `clogb2(RAM_DEPTH)`，会得到 `$clog2(RAM_DEPTH+1)`，多出 1 位（在 RAM_DEPTH 为 2 的幂时尤为明显）。

**练习 3**：一个计数器要能装下 0..100，最小位宽是多少？分别用 `$clog2` 和 `clogb2` 写出来。

> **答案**：`$clog2(101) = 7` bit；等价的 `clogb2` 写法是 `clogb2(100) = 7`。

---

## 5. 综合实践

把本讲三个模块串起来：写一个**自校验** testbench，既打印对照表、又用断言验证核心等式，还能对任意 DEPTH 给出「地址位宽」和「计数位宽」的建议。

**实践目标**：用一条 testbench 一次性确认 (a) `clogb2(d) == $clog2(d+1)` 对所有 d 成立；(b) 对每个 DEPTH，地址位宽 = `$clog2(DEPTH)`、计数位宽 = `$clog2(DEPTH+1)`。

**操作步骤**：

1. 在仓库根目录新建 `clogb2_selfcheck_tb.sv`（**示例代码**），内容如下：

```verilog
// 示例代码：clogb2_selfcheck_tb.sv
`timescale 1ns / 1ps

module clogb2_selfcheck_tb;

  integer d;
  integer errors = 0;

  initial begin
    $display(" d | addr_w=$clog2(d) | cnt_w=$clog2(d+1) | clogb2(d) | self-check");
    for (d = 1; d <= 16; d = d + 1) begin
      // (a) 核心等式校验
      if (clogb2(d) != $clog2(d + 1)) begin
        $display("  MISMATCH at d=%0d: clogb2=%0d  $clog2(d+1)=%0d",
                 d, clogb2(d), $clog2(d+1));
        errors = errors + 1;
      end
      // (b) 打印地址位宽与计数位宽
      $display("%2d |      %2d          |      %2d           |   %2d      | %s",
               d, $clog2(d), $clog2(d+1), clogb2(d),
               (clogb2(d) == $clog2(d+1)) ? "ok" : "FAIL");
    end

    if (errors == 0)
      $display("ALL CHECKS PASSED");
    else
      $display("FAILED with %0d errors", errors);

    $finish;
  end

  `include "clogb2.svh"

endmodule
```

2. 编译运行（iverilog）：

```bash
iverilog -g2012 -o sc.vvp clogb2_selfcheck_tb.sv
vvp sc.vvp
```

3. 阅读输出，确认最后一行是 `ALL CHECKS PASSED`。

**需要观察的现象**：

- `addr_w` 列在 d=1 时为 0（只有 1 个表项，不需要地址），在 d=2 时跳到 1，d=4 时跳到 2，d=8 时跳到 3——即每跨过一个 2 的幂才 +1。
- `cnt_w` 列始终比 `addr_w` 「领先一档」，因为它要装下 DEPTH 本身。
- 任何一行都不应出现 `FAIL`。

**预期结果**：终端打印 `ALL CHECKS PASSED`，且 `addr_w`/`cnt_w`/`clogb2(d)` 三列符合本讲对照表。

> 待本地验证：`iverilog` 对 `$clog2` 的支持需 `-g2012`；若用更老的工具链，可能需要退回 `clogb2` 函数——这也正是仓库保留 `clogb2.svh` 的历史原因。

## 6. 本讲小结

- `clogb2(n)` 数的是「把 n 右移到 0 需要几次」，等于「表示数值 n 本身的位数」\(\lfloor \log_2 n \rfloor + 1\)。
- `$clog2(n)` 是系统函数，等于 \(\lceil \log_2 n \rceil\)，回答「n 个表项需要几 bit 地址」，无需 include，是新设计的首选。
- **核心等式**：`clogb2(n) == $clog2(n+1)`，记住它就能在两套写法间自由换算。
- **地址位宽**（索引 0..DEPTH−1）= `$clog2(DEPTH)` = `clogb2(DEPTH-1)`；**计数位宽**（装 0..DEPTH）= `$clog2(DEPTH+1)` = `clogb2(DEPTH)`。
- 仓库 RAM 模板用 `clogb2(RAM_DEPTH-1)` 写地址位宽（结果正确）；FIFO 老写法 `clogb2(DEPTH)+1` 比严格需要多 1 位（因冗余而无害）；`lifo.sv` 用 `$clog2(DEPTH+1)` 是推荐的现代写法。
- 作者明确建议：新设计别再用 `clogb2`，改用语义清晰的 `$clog2`，并据用途选 `DEPTH` 还是 `DEPTH+1`。

## 7. 下一步学习建议

- 顺着存储器主线进入 **u4-l1（RAM/ROM 模板）**：本讲提到的 `true_single_port_write_first_ram.sv`、`true_dual_port_write_first_2_clock_ram.sv` 的 `ramstyle` 属性、`$readmemh` 初始化与 write-first 语义将在那里完整展开。
- 进入 **u4-l2（单时钟 FIFO）**：看 `fifo_single_clock_ram.sv` 如何用 `DEPTH_W` 位的 `w_ptr/r_ptr/cnt` 配合 `inc_ptr` 实现环形缓冲与满空判断，本讲的位宽知识是读懂那段指针逻辑的前提。
- 想深入了解 `clogb2` 注释里引用的 Verilog-2001 位宽计算原始资料，可阅读 [clogb2.sv:L9](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/clogb2.svh#L9) 提到的 Cummings HDLCON2001 文章（sunburst-design.com）。
