# 位宽计算：clogb2 与 $clog2

## 1. 本讲目标

本讲只解决一个看似极小、却最容易在 FIFO/RAM 设计里踩坑的问题：**给定一个存储深度 DEPTH，地址指针和元素计数器分别需要多少位？**

学完后你应该能够：

- 说清楚「地址指针位宽」与「计数器位宽」差在哪里、为什么差一位。
- 看懂仓库里 `clogb2()` 函数的算法，并能手算 `clogb2(N)`。
- 看懂 `clogb2` 与系统函数 `$clog2` 在边界值（尤其是 2 的幂）上的差异表。
- 在新设计里正确写出 `$clog2(DEPTH)`（地址）与 `$clog2(DEPTH+1)`（计数），并理解仓库里旧写法 `clogb2(DEPTH-1)` / `clogb2(DEPTH)+1` 与之的等价（或差异）关系。

## 2. 前置知识

本讲依赖 u1-l2 里建立的两个概念：

- **参数化端口** `#(parameter ...)`：仓库里几乎所有位宽都不是写死的常数，而是由参数算出来的。本讲的「位宽」本身就是一种「在编译期由参数算出来的常数」。
- **模块内部的 function**：Verilog 的 `function` 可以在模块内部定义、在编译期被求值，用来给端口/局部变量算位宽。`clogb2` 就是一个这样的函数。

你还需要两个最基础的数学直觉：

- 一个 \(w\) 位的无符号数能表示 \(0 \sim 2^{w}-1\) 共 \(2^{w}\) 个不同的值；反过来，要表示 \(M\) 个不同的值，需要 \(w = \lceil \log_2 M \rceil\) 位。这就是「向上取整的对数」，记作 ceil(log2)。
- 在硬件里，「能表示多少个值」直接决定要分配几位寄存器。多一位是浪费，少一位是溢出 bug。

> 名词速查：**位宽（width）** = 一个信号有多少 bit；**深度（depth）** = 一个 RAM/FIFO 能存多少个元素；**指针（pointer）** = 用来索引 RAM 里第几个元素的地址计数器。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [clogb2.svh](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/clogb2.svh) | 一个用 Verilog-2001 语法手写的「ceil(log2)」函数，以及它与 `$clog2` 的差异表和使用建议。注意扩展名是 `.svh`，习惯上表示「被 include 的头文件」。 |
| [true_single_port_write_first_ram.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/true_single_port_write_first_ram.sv) | 单口 RAM 模板。它的地址端口 `addra` 用 `clogb2(RAM_DEPTH-1)` 算位宽 —— 这是「地址位宽」的典型例子。 |
| [fifo_single_clock_ram.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram.sv) | 单时钟 FIFO。它的计数器位宽 `DEPTH_W = clogb2(DEPTH)+1` —— 这是「计数器位宽」的典型例子。 |

这三个文件正好串起本讲的全部内容：一个函数 + 它的两种用法。

## 4. 核心概念与源码讲解

### 4.1 clogb2 函数：手写的 ceil(log2)

#### 4.1.1 概念说明

很多场合需要在编译期算「几位够用」：

- 一个 1024 深的 RAM，地址线要几根？\( \lceil \log_2 1024 \rceil = 10 \) 根。
- 一个最多计到 8 的计数器，要几位？能表示 \(0 \sim 8\) 共 9 个值，\( \lceil \log_2 9 \rceil = 4 \) 位。

在 Verilog-2001 年代，语言里**还没有**内置的 ceil(log2) 系统函数，或者某些工具的实现是错的。于是工程师们手写了一个等价函数，起名 `clogb2`（ceiling LOG Base 2）。仓库把它单独放在 `clogb2.svh`，哪个模块要用就在模块内部 `\`include` 进来。

`clogb2` 解决的问题就是：**给我一个正整数 N，告诉我表示 \(0 \sim N\) 需要几位。**

#### 4.1.2 核心流程

`clogb2` 的算法非常朴素：**不断把输入右移 1 位，数一共移了几次才变成 0**。移的次数就是答案。

为什么这对？把 N 写成二进制，它有 \(k = \lfloor \log_2 N \rfloor + 1\) 位。每右移一位就丢掉最低位、整体缩小一半，正好要移 \(k\) 次才归零。所以：

\[
\text{clogb2}(N) = \begin{cases} 0 & N = 0 \\ \lfloor \log_2 N \rfloor + 1 & N \geq 1 \end{cases} = \lceil \log_2(N+1) \rceil
\]

注意末尾那个等式：`clogb2(N)` 恰好等于「表示 \(0 \sim N\)（共 \(N+1\) 个值）所需的位数」\( \lceil \log_2(N+1) \rceil \)。这个等价关系是后面区分「地址位宽」和「计数器位宽」的关键，请先记住。

手算两个例子：

- `clogb2(7)`：7 → 3 → 1 → 0，移了 3 次，结果是 3。（表示 0~7，3 位够。）
- `clogb2(8)`：8 → 4 → 2 → 1 → 0，移了 4 次，结果是 4。（表示 0~8，需要 4 位 —— 注意不是 3！）

#### 4.1.3 源码精读

函数本体只有 4 行，先把循环展开看清楚：

[clogb2.svh:L52-L59](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/clogb2.svh#L52-L59) — 这就是 `clogb2` 的全部实现：用函数自身的返回值 `clogb2` 当计数器，只要 `depth>0` 就右移一位、计数器加一。

```verilog
function integer clogb2;
  input [31:0] depth;
  for( clogb2=0; depth>0; clogb2=clogb2+1 ) begin
    depth = depth >> 1;
  end
endfunction
```

几个值得注意的写法：

- `function integer clogb2;` 是 Verilog-2001 的老式函数声明（ANSI 风格会用 `function automatic integer clogb2(input [31:0] depth);`）。返回值类型是 `integer`，函数名同时是承载返回值的隐式变量。
- `input [31:0] depth;` 把入参单独声明在函数体里，也是老语法。
- `for` 循环里 `clogb2=clogb2+1` 直接累加函数名变量 —— 循环结束时它的值就是返回值。
- 当 `depth==0` 时循环体一次都不执行，`clogb2` 保持初值 0，所以 `clogb2(0)=0`。

文件头部的 INFO 注释明确给出了这个函数的历史定位和「别用于新设计」的警告：

[clogb2.svh:L11-L23](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/clogb2.svh#L11-L23) — 作者直接写明：`clogb2()` 是 Verilog-2001 时代的过时技术，新设计应改用 `$clog2(DEPTH)` 做地址指针、`$clog2(DEPTH+1)` 做计数器。这正是本讲要把你推向的结论。

#### 4.1.4 代码实践

实践目标：亲手验证 `clogb2` 的「移位计数」算法，确认它真的等于 \(\lceil \log_2(N+1) \rceil\)。

操作步骤（纯手算，无需工具）：

1. 对 \(N = 1, 4, 7, 8, 16\)，按「右移到 0 数次数」的方法算 `clogb2(N)`。
2. 再用公式 \(\lceil \log_2(N+1) \rceil\) 算一遍，对照是否一致。

需要观察的现象 / 预期结果：

| N | 移位轨迹 | 移位次数 = clogb2(N) | \(\lceil \log_2(N+1) \rceil\) |
|---|----------|----------------------|-------------------------------|
| 1 | 1→0 | 1 | \(\lceil\log_2 2\rceil=1\) |
| 4 | 4→2→1→0 | 3 | \(\lceil\log_2 5\rceil=3\) |
| 7 | 7→3→1→0 | 3 | \(\lceil\log_2 8\rceil=3\) |
| 8 | 8→4→2→1→0 | 4 | \(\lceil\log_2 9\rceil=4\) |
| 16| 16→8→4→2→1→0 | 5 | \(\lceil\log_2 17\rceil=5\) |

两列应当完全相同。重点体会：**N=8 时结果是 4 而不是 3**，因为要表示 0~8（9 个值），3 位（最多到 7）不够。

> 说明：上表是按算法规则推导的预期值，未在本地实跑；你可以在第 5 节的综合实践里用仿真器打印 `clogb2()` 做最终确认。

#### 4.1.5 小练习与答案

**练习 1**：`clogb2(0)` 等于多少？为什么？

**答案**：等于 0。因为 `depth>0` 一开始就为假，循环不执行，函数名变量保持初值 0。

**练习 2**：要表示 \(0 \sim 255\) 的计数值，用 `clogb2` 该怎么写？结果是几？

**答案**：写 `clogb2(255)`，结果是 8（255→127→63→…→1→0 共 8 次移位）。它等价于 \(\lceil\log_2 256\rceil = 8\)。

---

### 4.2 $clog2 系统函数：语言内置的 ceil(log2)

#### 4.2.1 概念说明

从 Verilog-2005 / SystemVerilog 开始，语言内置了系统函数 `$clog2(N)`，语义是严格的：

\[
\$clog2(N) = \lceil \log_2 N \rceil
\]

它回答的问题是：**要区分 \(N\) 个不同的值，需要几位。** 注意它和 `clogb2` 的入参含义微妙不同：

- `$clog2(N)` = 表示 \(N\) 个值（即 \(0 \sim N-1\)）的位数 = \(\lceil\log_2 N\rceil\)。
- `clogb2(N)` = 表示 \(0 \sim N\)（共 \(N+1\) 个值）的位数 = \(\lceil\log_2(N+1)\rceil\)。

所以同样传入 8，`$clog2(8)=3`（区分 8 个值 0~7），而 `clogb2(8)=4`（表示 0~8）。这是两者最直观的差别，也是初学者最容易被绊倒的地方。

#### 4.2.2 核心流程

`$clog2` 不需要 include 任何文件，是编译器自带的，可直接出现在端口声明、`localparam`、`initial` 等任何需要常量表达式的地方。常用对照：

\[
\$clog2(1)=0,\quad \$clog2(2)=1,\quad \$clog2(3)=2,\quad \$clog2(4)=2,\quad \$clog2(8)=3,\quad \$clog2(9)=4,\quad \$clog2(16)=4
\]

规律：当 \(N\) 是 2 的幂时，`$clog2(N) = \log_2 N`；否则向上取整到下一个整数。

#### 4.2.3 源码精读

`clogb2.svh` 里专门用一张表把两者逐值对照，这是本讲最重要的一张表：

[clogb2.svh:L25-L43](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/clogb2.svh#L25-L43) — `$clog2` 与 `clogb2` 在 0~16 上的逐值对照表。请重点看每一行「2 的幂」的位置：`$clog2(2)=1 / clogb2(2)=2`、`$clog2(4)=2 / clogb2(4)=3`、`$clog2(8)=3 / clogb2(8)=4`、`$clog2(16)=4 / clogb2(16)=5` —— 在这些点上两者正好差 1。

把这张表横过来看，能读出两条本讲的核心等价式（对 \(DEPTH \geq 1\) 成立）：

\[
\$clog2(DEPTH) = \text{clogb2}(DEPTH-1) \qquad\text{（地址位宽）}
\]

\[
\$clog2(DEPTH+1) = \text{clogb2}(DEPTH) \qquad\text{（计数器位宽）}
\]

> 自行核验：取 DEPTH=8。地址位宽：`$clog2(8)=3`，`clogb2(7)=3`，相等。计数器位宽：`$clog2(9)=4`，`clogb2(8)=4`，相等。两条都对。

仓库里也有直接用 `$clog2` 的真实例子，不在 `.svh` 而在 testbench 里：[fifo_single_clock_ram_tb.sv:L207](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram_tb.sv#L207)，给 Altera SCFIFO 的 `LPM_WIDTHU`（已用字数计数器宽度）传 `.LPM_WIDTHU( $clog2(8) )`，行尾注释也写明 `/// CEIL(LOG2(LPM_NUMWORDS))`，正好印证了 `$clog2` = ceil(log2) 的语义。

#### 4.2.4 代码实践

实践目标：在仿真器里直接打印 `$clog2`，体会它和 `clogb2` 在 2 的幂处差 1。

操作步骤：

1. 新建一个最小 testbench（示例代码，非仓库原有文件），在 `initial` 里用 `$display` 打印 `$clog2(d)`：

```verilog
`timescale 1ns / 1ps
module clog2_only_tb();           // 示例代码
  integer d;
  initial begin
    for (d=1; d<=16; d=d+1)
      $display("d=%0d  $clog2(d)=%0d", d, $clog2(d));
    $finish;
  end
endmodule
```

2. 用 iverilog 编译运行（`$clog2` 需 `-g2012`）：

```bash
iverilog -g2012 clog2_only_tb.sv -o clog2_only_tb.vvp
vvp clog2_only_tb.vvp
```

需要观察的现象 / 预期结果：重点看 `d=8` 时 `$clog2(8)=3`，而上一节 `clogb2(8)=4` —— 同样是「8」，两个函数给出不同答案，因为它们问的问题不同。

> 说明：以上为预期输出，未在本地实跑，请自行运行确认。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `$clog2(8)=3` 而 `clogb2(8)=4`？

**答案**：`$clog2(8)` 问「区分 8 个值（0~7）要几位」，3 位够（0~7）。`clogb2(8)` 问「表示 0~8（9 个值）要几位」，3 位只到 7 不够，需要 4 位。

**练习 2**：用 `$clog2` 写出「表示 0~100 要几位」的表达式并算出结果。

**答案**：`$clog2(101)`，结果为 7（\(2^6=64 < 101 \le 128=2^7\)）。

---

### 4.3 地址位宽 vs 计数器位宽：差一位的根源

#### 4.3.1 概念说明

这是本讲的落点，也是 FIFO/RAM 设计里最高频的 off-by-one 错误源。同样是「一个深度为 DEPTH 的存储」，有两种位宽需求，它们差一位：

| 角色 | 取值范围 | 不同值的个数 | 所需位宽（现代写法） | 旧写法（仓库） |
|------|----------|--------------|----------------------|----------------|
| **地址指针** `wr_addr`/`rd_addr` | \(0 \sim DEPTH-1\) | DEPTH | `$clog2(DEPTH)` | `clogb2(DEPTH-1)` |
| **元素计数器** `cnt` | \(0 \sim DEPTH\) | DEPTH+1 | `$clog2(DEPTH+1)` | `clogb2(DEPTH)` |

为什么计数器要多一个值、多可能一位？因为计数器不仅要能表示「空（0）」和「存了一些」，还要能表示**「满（= DEPTH）」**。地址指针永远在 \(0 \sim DEPTH-1\) 之间转，碰不到 DEPTH；而计数器必须能取到 DEPTH 这个值，否则「满」判断无法成立。

直觉记法：**地址管「下标」，最大到 DEPTH−1；计数管「个数」，最大到 DEPTH。所以计数器永远比地址多一个要表示的值，公式里就多一个 +1。**

#### 4.3.2 核心流程

把上面的逻辑套到两条等价式上，就得到从旧写法到新写法的迁移规则：

1. 看到旧代码 `clogb2(DEPTH-1)` 用于端口位宽 → 这是**地址**，改成 `$clog2(DEPTH)`。
2. 看到旧代码 `clogb2(DEPTH)+1` 或 `clogb2(DEPTH)` 用于计数 → 这是**计数器**，改成 `$clog2(DEPTH+1)`。
3. 特别注意：仓库 FIFO 里写的是 `clogb2(DEPTH)+1`，由于 `clogb2(DEPTH) = $clog2(DEPTH+1)`，所以它等于 `$clog2(DEPTH+1) + 1`，比严格所需的 `$clog2(DEPTH+1)` 还**多了一位** —— 多余但安全（不会溢出，只是浪费一两根寄存器）。这是旧写法「偏保守」的体现，也是作者在 `clogb2.svh` 里建议大家改用 `$clog2` 的现实原因之一。

以 DEPTH=8 为例把三种位宽并排放，差别一目了然：

\[
\underbrace{\$clog2(8)=3}_{\text{地址，严格}}
\quad
\underbrace{\$clog2(9)=4}_{\text{计数器，严格}}
\quad
\underbrace{\text{clogb2}(8)+1=5}_{\text{仓库 FIFO 实际，多一位}}
\]

#### 4.3.3 源码精读

先看**地址位宽**的例子 —— 单口 RAM 的地址端口：

[true_single_port_write_first_ram.sv:L41](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/true_single_port_write_first_ram.sv#L41) — `input [clogb2(RAM_DEPTH-1)-1:0] addra`，地址位宽 = `clogb2(RAM_DEPTH-1)`。RAM_DEPTH=8 时，`clogb2(7)=3`，地址是 `[2:0]`，正好索引 0~7。按本讲等价式，它就是 `$clog2(RAM_DEPTH)`。

再看**计数器位宽**的例子 —— 单时钟 FIFO：

[fifo_single_clock_ram.sv:L57-L59](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram.sv#L57-L59) — `DEPTH_W = clogb2(DEPTH)+1`，注释写明这是「elements counter width, extra bit to store fifo full state」（元素计数器位宽，多一位用来存「满」状态）。这就是计数器位宽的旧写法。

```verilog
DEPTH = 8,                 // max elements count == DEPTH, DEPTH MUST be power of 2
DEPTH_W = clogb2(DEPTH)+1, // elements counter width, extra bit to store
                           // "fifo full" state, see cnt[] variable comments
```

随后 `DEPTH_W` 被同时用作计数器和读/写指针的位宽：

[fifo_single_clock_ram.sv:L82](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram.sv#L82) — `output logic [DEPTH_W-1:0] cnt`，元素计数器 `cnt` 用 `DEPTH_W` 位，需要能取到 `DEPTH` 来表示「满」。对应的满判断在 [fifo_single_clock_ram.sv:L165-L167](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram.sv#L165-L167)：`full = ( cnt == DEPTH )`。正因为 `cnt` 要能等于 DEPTH，它的位宽必须按「计数器」而非「地址」来算。

最后注意 include 的位置 —— 两个模块都把 `\`include "clogb2.svh"` 放在**模块内部、`endmodule` 之前**，因为函数必须定义在某个模块里：

[fifo_single_clock_ram.sv:L183](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram.sv#L183) 与 [true_single_port_write_first_ram.sv:L93](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/true_single_port_write_first_ram.sv#L93) —— 两者都是在 `endmodule` 前一行 include。`.svh` 头注释里也写明「Function should be instantiated inside a module」。这也是为什么 `clogb2` 不像 `$clog2` 那样能随用随写：它得先被某个模块「收留」。

> 关于 FIFO 指针/计数/满空判断的完整工作机制（环形回绕、同时读写仲裁等）本讲不展开，那是 u4-l2「单时钟 FIFO」的主题。本讲只聚焦「位宽怎么算」。

#### 4.3.4 代码实践

实践目标：拿真实源码做一次「位宽审计」，把旧写法翻译成现代 `$clog2` 写法。

操作步骤：

1. 打开 [true_single_port_write_first_ram.sv:L41](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/true_single_port_write_first_ram.sv#L41)，对 `RAM_DEPTH = 8/16/1024` 三个值，分别手算 `clogb2(RAM_DEPTH-1)` 和等价的 `$clog2(RAM_DEPTH)`，确认两者一致。
2. 打开 [fifo_single_clock_ram.sv:L57-L59](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram.sv#L57-L59)，对 `DEPTH = 8/16`，手算仓库实际位宽 `clogb2(DEPTH)+1` 与严格位宽 `$clog2(DEPTH+1)`，确认前者比后者大 1。

需要观察的现象 / 预期结果：

| 模块 | 参数 | 旧写法计算 | 结果 | 现代等价 | 是否严格 |
|------|------|-----------|------|---------|----------|
| RAM 地址 | RAM_DEPTH=8 | clogb2(7) | 3 | `$clog2(8)=3` | 严格相等 |
| RAM 地址 | RAM_DEPTH=1024 | clogb2(1023) | 10 | `$clog2(1024)=10` | 严格相等 |
| FIFO 计数 | DEPTH=8 | clogb2(8)+1 | 5 | `$clog2(9)=4` | 多 1 位 |
| FIFO 计数 | DEPTH=16 | clogb2(16)+1 | 6 | `$clog2(17)=5` | 多 1 位 |

结论：RAM 的地址位宽旧写法与现代写法**完全相等**；FIFO 的计数器位宽旧写法**多一位**（安全但浪费）。这就是为什么新设计应该直接用 `$clog2`。

#### 4.3.5 小练习与答案

**练习 1**：一个 DEPTH=8 的 FIFO，`cnt` 严格需要几位？仓库实际给了几位？

**答案**：严格需要 `$clog2(8+1)=$clog2(9)=4` 位（表示 0~8）。仓库用 `clogb2(8)+1=5` 位，多了 1 位。

**练习 2**：如果错把地址位宽写成 `$clog2(DEPTH+1)`（多算了 1 位），地址还能正确索引 0~DEPTH-1 吗？有什么副作用？

**答案**：能正确索引。地址值仍由指针产生、被约束在 0~DEPTH-1，多出的高位只是恒为 0，功能正确但浪费了地址线/寄存器，综合时可能多出不必要的逻辑或引起位宽对接警告。反向错误（少算一位，把计数器写成 `$clog2(DEPTH)`）才是致命的：`cnt` 无法表示 DEPTH，「满」永远判不出来。

## 5. 综合实践

把本讲三节串起来：**手算 + 仿真对照，做出一张 DEPTH=1~16 的位宽总表**，一次性看清 `clogb2`、`$clog2(DEPTH)`、`$clog2(DEPTH+1)` 三者的关系。

### 5.1 任务描述

1. 先**纯手算**填写下表的「手算」三列（不要先看程序输出）。
2. 再写一个 testbench，用 `$display` 把 `clogb2(DEPTH)`、`$clog2(DEPTH)`、`$clog2(DEPTH+1)` 打印出来，填入「仿真」三列。
3. 对照手算与仿真是否一致，并验证两条核心等价式：`$clog2(DEPTH) == clogb2(DEPTH-1)`、`$clog2(DEPTH+1) == clogb2(DEPTH)`。

### 5.2 参考答案表（预期值）

| DEPTH | 手算/仿真: clogb2(DEPTH) | $clog2(DEPTH)【地址】 | $clog2(DEPTH+1)【计数器】 | clogb2(DEPTH−1)【=地址】 |
|------:|:---:|:---:|:---:|:---:|
| 1  | 1 | 0 | 1 | 0 |
| 2  | 2 | 1 | 2 | 1 |
| 3  | 2 | 2 | 2 | 2 |
| 4  | 3 | 2 | 3 | 2 |
| 5  | 3 | 3 | 3 | 3 |
| 6  | 3 | 3 | 3 | 3 |
| 7  | 3 | 3 | 3 | 3 |
| 8  | 4 | 3 | 4 | 3 |
| 9  | 4 | 4 | 4 | 4 |
| 10 | 4 | 4 | 4 | 4 |
| 11 | 4 | 4 | 4 | 4 |
| 12 | 4 | 4 | 4 | 4 |
| 13 | 4 | 4 | 4 | 4 |
| 14 | 4 | 4 | 4 | 4 |
| 15 | 4 | 4 | 4 | 4 |
| 16 | 5 | 4 | 5 | 4 |

读这张表的两个要点：

- 第 2 列 `clogb2(DEPTH)` 与第 4 列 `$clog2(DEPTH+1)` **逐行相同** —— 这就是「计数器位宽」的两种等价写法。
- 第 3 列 `$clog2(DEPTH)` 与第 5 列 `clogb2(DEPTH−1)` **逐行相同** —— 这就是「地址位宽」的两种等价写法。
- 在 DEPTH=2/4/8/16 这些 2 的幂行，`clogb2(DEPTH)` 比 `$clog2(DEPTH)` 正好大 1。

### 5.3 可直接运行的 testbench（示例代码）

```verilog
`timescale 1ns / 1ps
//
// clogb2_width_tb.sv   (示例代码，非仓库原有文件)
// 对照打印 clogb2() 与 $clog2()，验证位宽计算的等价关系
//
module clogb2_width_tb();

  integer d;

  initial begin
    $display("DEPTH | clogb2(DEPTH) | $clog2(DEPTH) [addr] | $clog2(DEPTH+1) [cnt] | clogb2(DEPTH-1)");
    for (d = 1; d <= 16; d = d + 1) begin
      $display("%4d  |    %0d           |       %0d              |       %0d            |       %0d",
               d, clogb2(d), $clog2(d), $clog2(d+1), clogb2(d-1));
    end
    $finish;
  end

  // 仓库约定：clogb2 必须被 include 在某个模块内部
  `include "clogb2.svh"

endmodule
```

编译运行（需把仓库根目录加入 include 搜索路径，以便找到 `clogb2.svh`）：

```bash
iverilog -g2012 -I . clogb2_width_tb.sv -o clogb2_width_tb.vvp
vvp clogb2_width_tb.vvp
```

需要观察的现象 / 预期结果：终端打印的 16 行数值应与 5.2 表格完全一致。重点确认 `$clog2(DEPTH+1)` 列 == `clogb2(DEPTH)` 列、`$clog2(DEPTH)` 列 == `clogb2(DEPTH-1)` 列。

> 说明：上述表格与命令均为预期/推导结果，未在本地实跑。若你的 iverilog 版本对 `$clog2` 支持有差异，可改用 ModelSim/Vivado 仿真器，或把 `$clog2(d)` 临时替换成手算常数来隔离问题。

## 6. 本讲小结

- **位宽问题的本质是 ceil(log2)**：要表示 \(M\) 个不同的值，需要 \(\lceil\log_2 M\rceil\) 位。`clogb2` 和 `$clog2` 都是算这个的，只是入参含义差一位。
- **`clogb2(N) = \(\lceil\log_2(N+1)\rceil\)**：手写的「移位计数」函数，表示 \(0 \sim N\) 需要几位；属于 Verilog-2001 时代的过时技术，仓库自己也不推荐用于新设计。
- **`$clog2(N) = \(\lceil\log_2 N\rceil\)**：语言内置系统函数，表示区分 \(N\) 个值（\(0 \sim N-1\)）需要几位；是新设计的首选。
- **地址位宽 vs 计数器位宽差一位**：地址指针只到 DEPTH−1，用 `$clog2(DEPTH)`；计数器要到 DEPTH（表示「满」），用 `$clog2(DEPTH+1)`。这一位之差是 FIFO 最常见的 bug 源。
- **两条等价式**：`$clog2(DEPTH) == clogb2(DEPTH-1)`（地址），`$clog2(DEPTH+1) == clogb2(DEPTH)`（计数器）。仓库 FIFO 写成 `clogb2(DEPTH)+1`，比严格所需的 `$clog2(DEPTH+1)` 还多一位，安全但略浪费。
- **include 位置**：`clogb2.svh` 必须被 include 在某个模块内部（仓库统一放在 `endmodule` 前），这是 function 与系统函数 `$clog2` 在使用上的根本差别。

## 7. 下一步学习建议

本讲的位宽计算是存储类模块的「地基」，接下来顺理成章进入存储与 FIFO 单元：

- **u4-l1 RAM/ROM 模板**：直接承接本讲的「地址位宽」，看 `true_single_port_write_first_ram.sv` 如何用 `clogb2(RAM_DEPTH-1)` 定地址、用 `(* ramstyle *)` 推断 block RAM、用 `$readmemh` 初始化内容。
- **u4-l2 单时钟 FIFO**：直接承接本讲的「计数器位宽」，深入 [fifo_single_clock_ram.sv](https://github.com/pConst/basic_verilog/blob/2654273b2c4e556a98e806f3a19b52c9b3c74614/fifo_single_clock_ram.sv) 的环形指针、`cnt` 满/空判断、同时读写仲裁，看 `DEPTH_W` 那一位「满状态」到底怎么用。
- 想了解更多 `clogb2` 的历史背景，可阅读 `clogb2.svh` 注释里给出的 Cliff Cummings 经典论文链接（HDLCON 2001），它详细解释了 Verilog-2001 时代为什么需要手写这个函数。
