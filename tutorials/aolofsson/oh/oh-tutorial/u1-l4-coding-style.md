# Verilog 2005 与 OH! 编码规范

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 OH! 全库统一使用的语言标准（Verilog 2005）以及它对可综合性（synthesizable）的硬性约束。
- 读懂 `README.md` 的 **Coding Guide**，并把其中几十条规则归纳成「文件 / 命名 / 端口 / 赋值 / 复位」几类记忆。
- 打开任意一个 `stdlib/rtl/*.v` 文件，对照规范逐行理解它的写法为什么「长成那样」。
- 理解 OH! 的核心套路——**参数化（parameterization）**：用一个 `#(parameter ...)` 让同一个模块适配任意位宽，并用 `SYN/TYPE` 参数在同一份 RTL 里切换 soft（可综合）与 hard（ASIC 硬核）两种实现。
- 仿照 `oh_dffq.v` 的风格，独立写出一个带注释端口的参数化两输入与门。

## 2. 前置知识

在进入源码之前，先用大白话建立几个概念。本讲假设你已经读过 **u1-l1（项目总览）** 和 **u1-l3（仿真环境）**，知道：

- **HDL / Verilog**：硬件描述语言（Hardware Description Language）。Verilog 是其中一种，你写的「代码」描述的是一张电路图，而不是一段顺序执行的程序。
- **Verilog 2005**：即 IEEE 1364-2005 标准，是 Verilog 的一个稳定版本。OH! 全库锁定这一版（见 `README.md` 第 14 行 "written in standard Verilog (2005)"），编译时用 `iverilog -g2005` 对应（详见 u1-l3）。锁定版本的好处是：不依赖任何新语法，任何符合 2005 标准的综合工具/仿真器都能吃下去。
- **可综合性（synthesizable）**：一段 Verilog 能不能被综合工具「翻译」成真实的逻辑门。`always`、`assign`、`if/case` 这些可以；`#10` 延时、`initial`（多数情况）、`fork/join` 这些通常不可以。OH! 的设计文件**只能**用可综合构造（见下文「允许关键字表」）。
- **RTL**：Register Transfer Level，寄存器传输级。OH! 的 `stdlib/rtl/` 放的就是 RTL 级的可综合设计。
- **参数化**：给模块定义「可调旋钮」（参数 parameter），比如位宽 `N=8`。实例化时可以拧成 `N=32`，同一份代码复用到不同位宽。
- **soft / hard 双实现**：OH! 的一大架构特色（详见 u9-l1）。同一个功能，soft 版是可综合 RTL（如 `oh_dffq.v`），hard 版是绑定具体工艺库（PDK）的 ASIC 硬核（如 `asiclib` 里的 `asic_dffq.v`）。两者靠参数 `SYN/TYPE` 在同一份顶层里切换。

> 一句话直觉：**Verilog 2005 是 OH! 的「语法地基」，Coding Guide 是 OH! 的「家规」，参数化是 OH! 让「一块积木」变成「一整套积木」的手段。**

## 3. 本讲源码地图

本讲只动用最小的几个文件，它们都是「事实来源」：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/README.md#L110-L163) | 第 110–163 行的 **Coding Guide** 是 OH! 编码规范的总纲。 |
| [docs/verilog_reference.md](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/docs/verilog_reference.md) | Verilog 速查手册（标注 "WORK IN PROGRESS"），含保留关键字表、可综合构造表，是规范的「语法注脚」。 |
| [stdlib/rtl/oh_dffq.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_dffq.v) | 最简单的时序原语：无复位 D 触发器，演示「一文件一模块 + 参数化 + 非阻塞赋值」。 |
| [stdlib/rtl/oh_mux2.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_mux2.v) | 最简单的组合原语：2 选 1 选择器，演示「位宽显式标注 + 注释端口」。 |
| [stdlib/rtl/oh_and2.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_and2.v) | 两输入与门，演示多参数 + `SYN/TYPE` 的 soft/hard 切换写法（也是本讲综合实践的参照物）。 |
| [gpio/hdl/gpio_regmap.vh](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio_regmap.vh) | 寄存器映射头文件，演示 `include` + `ifndef` 头文件守卫约定。 |

辅助参照（本讲会引用）：`stdlib/rtl/oh_dffrq.v`（带复位版触发器，演示低有效复位）、`stdlib/rtl/oh_mux.v`（多参数 N:M 选择器）、`elink/hdl/elink_constants.vh`（常量头文件）。

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：**4.1 编码规范**、**4.2 参数化（与 soft/hard 取舍）**。

### 4.1 编码规范

#### 4.1.1 概念说明

OH! 是一个由 150+ 模块、25000+ 行 Verilog 组成的库（见 `README.md` 第 14 行）。要让这么多文件「长得像同一个人写的」、并且都能被各种工具稳定吃下去，就必须有一份强约束的家规。这份家规就是 `README.md` 的 **Coding Guide**（[README.md:L110-L163](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/README.md#L110-L163)）。

它解决三个问题：

1. **一致性**：命名、缩进、注释风格统一，读任意一个文件都没有认知切换成本。
2. **可综合性**：限定只能用一小撮「安全」的关键字，保证设计文件能被综合，而不只是能仿真。
3. **可复用与可移植**：强制参数化、强制按名连接（connection by name），让模块能在不同位宽、不同工艺之间复用。

#### 4.1.2 核心流程

Coding Guide 的几十条规则可以归并成五类来记：

| 类别 | 关键规则（摘自 README Coding Guide） |
|------|----------------------------------------|
| **文件组织** | 一文件一模块（one module per file）；`.v` 放设计、`.vh` 放头文件；设计文件里**不写 timescale、不写延时**（只允许在 testbench 里写）；用 `include` 引入常量，并用 `` `ifndef `` 保证只包含一次。 |
| **命名** | 信号名全小写；参数/常量/宏全大写；超过 4 位的数值用下划线分组（如 `8'h1100_1100`）；用通用名 `nreset/clk/din/dout/en/rd/wr/addr`；generate 块用 `g0,g1`，块内实例用 `i<name>`。 |
| **端口与注释** | 每行只写一个 `input/output`；每个端口都要注释；端口名/注释对齐成列；只允许单行 `//` 注释，禁止 `/* */`。 |
| **赋值与时序** | 时序逻辑一律用非阻塞 `<=`；用 `` y down to x `` 向量（即 `[DW-1:0]`）；每条语句都显式标位宽 `a[7:0] = b[7:0]`；case 必须有 `default`；**禁止 `casex`**（用 `casez`）；用低有效复位（active low reset）；不要无谓复位。 |
| **实例化与参数** | 始终按名连接；参数传递用 `#(.DW(DW))`，**禁止** `mux3 #(32) U2(...)` 这种位置式、**禁止 `defparam`**；尽量参数化但别过度。 |

此外，Coding Guide 末尾给出一张**允许关键字白名单**（[README.md:L162](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/README.md#L162)）：

> `assign, always, input, output, wire, reg, module, endmodule, if/else, case, casez, ~, |, &, ^, ==, >>, <<, >, <, ?, posedge, negedge, generate, for(...), begin, end, $signed`

这意味着 OH! 的设计文件实际上只用 Verilog 的一个很小的、确定可综合的子集。与之配套，`docs/verilog_reference.md` 的「Verilog Synthesis Constructs」一节（[docs/verilog_reference.md:L909-L965](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/docs/verilog_reference.md#L909-L965)）把构造分成了 Fully Supported / Partially Supported / Ignored / Unsupported 四档，是判断「这段代码能不能综合」的速查表。

一条贯穿全局的「执行流程」可以概括为伪代码：

```text
写一个 OH! 模块时：
  1. 新建 文件名 == 模块名.v            # one module per file
  2. 顶部用 //##### 框 写 Function/Copyright/License 注释
  3. module 模块名 #(parameter 大写名 = 默认值)  # 参数化
       ( 端口列表，每行一个，每个加注释 );
  4. 体内：wire/reg 在最前面声明
  5. 组合逻辑用 assign（显式位宽）；时序用 always @(posedge clk) + 非阻塞 <=
  6. case 一定带 default；不用 casex；复位用低有效 nreset
  7. endmodule
```

#### 4.1.3 源码精读

**(a) 最小时序原语 `oh_dffq.v`** —— 一文件一模块、参数化、非阻塞赋值的样板：

[stdlib/rtl/oh_dffq.v:L8-L18](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_dffq.v#L8-L18) 定义了一个无复位 D 触发器。要点逐条对照规范：

```verilog
module oh_dffq #(parameter DW = 1) // array width
   (
    input [DW-1:0] 	d,
    input [DW-1:0] 	clk,
    output reg [DW-1:0] q
    );

   always @ (posedge clk)
     q <= d;
```

- 文件名 `oh_dffq.v` == 模块名 `oh_dffq`，满足「一文件一模块」。
- `#(parameter DW = 1)`：位宽是参数（大写 `DW`），默认 1，可实例化成任意宽度——这就是参数化。
- 端口 `d/clk/q` 全小写、每行一个、带 `[DW-1:0]` 显式位宽，方向用 `` y down to x ``。
- 时序逻辑用 `always @(posedge clk)` + 非阻塞 `q <= d`，完全符合 Coding Guide。
- 顶部 [stdlib/rtl/oh_dffq.v:L1-L6](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_dffq.v#L1-L6) 用 `//####` 框写了 Function / Copyright / License 注释（单行 `//`，没有 `/* */`）。

> 小提示：这里连 `clk` 也是 `[DW-1:0]` 的——这是 OH! 的一个特色写法，让「一组触发器」可以各自带独立时钟（方便逐位门控时钟，见 u2-l3）。对于普通单时钟用法，把所有位接同一个时钟即可。

**(b) 最小组合原语 `oh_mux2.v`** —— 显式位宽 + 端口注释：

[stdlib/rtl/oh_mux2.v:L8-L21](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_mux2.v#L8-L21) 是一个 2 选 1 的 one-hot 选择器：

```verilog
module oh_mux2 #(parameter N = 1 ) // width of mux
   (
    input 	    sel1,
    input 	    sel0,
    input [N-1:0]  in1,
    input [N-1:0]  in0,
    output [N-1:0] out  //selected data output
    );

   assign out[N-1:0] = ({(N){sel0}} & in0[N-1:0] |
                        {(N){sel1}} & in1[N-1:0]);
```

- 参数 `N` 控制数据位宽；`sel1/sel0` 是单比特 one-hot 选择信号。
- 组合逻辑用 `assign`，且**每一处都显式标位宽** `out[N-1:0]`、`in0[N-1:0]`——这正是 Coding Guide 里 "Use vector sizes in every statement" 的体现。
- 用 `{(N){sel0}}` 把 1 位选择信号「复制」N 份，再与输入按位与——这是 OH! 写参数化 mux 的通用手法。
- 输出端口 `out` 行尾带注释 `//selected data output`，满足「Comment every module port」。

**(c) 低有效复位 `oh_dffrq.v`** —— 复位语义的样板：

[stdlib/rtl/oh_dffrq.v:L9-L23](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_dffrq.v#L9-L23) 在 `oh_dffq` 基础上加了**异步、低有效**复位：

```verilog
   always @ (posedge clk or negedge nreset)
     if(!nreset)
       q <= 'b0;
     else
       q <= d;
```

- 复位信号叫 `nreset`（前缀 `n` 表示 active low 低有效），满足「Use active low reset」。
- 敏感列表里 `posedge clk or negedge nreset` + `if(!nreset)` 是异步低有效复位的标准写法。
- 复位值是 `'b0`，满足 Design Guide 的 "Make reset values 0"（[README.md:L106-L107](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/README.md#L106-L107)）。注意 `oh_dffq` 故意**不**带复位——对应 Coding Guide 的 "Avoid redundant resets / Only reset register if absolutely necessary"。

**(d) 头文件守卫 `gpio_regmap.vh`** —— `include` + `ifndef` 约定：

[gpio/hdl/gpio_regmap.vh:L1-L16](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio_regmap.vh#L1-L16) 用宏定义了一组寄存器地址常量：

```verilog
`ifndef GPIO_REGMAP_VH_
 `define GPIO_REGMAP_VH_
 `define GPIO_DIR      4'h0  // set direction of pin
 ...
`endif
```

- 用 `` `ifndef GPIO_REGMAP_VH_ / `define GPIO_REGMAP_VH_ / `endif `` 三件套保证头文件即使被多次 `include` 也只展开一次——这正是 Coding Guide 第 139 行 "Use `ifndef _CONSTANTS_V to include file only once" 的落地。
- 所有宏名 `GPIO_DIR/GPIO_IN/...` 全大写，值用 `4'h0` 这种显式位宽写法。寄存器映射的细节会在 u6-l1 详讲，这里只需看到「.vh + ifndef + 大写宏」的家规即可。同样的写法也出现在常量头文件 [elink/hdl/elink_constants.vh:L1-L6](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/elink_constants.vh#L1-L6)。

#### 4.1.4 代码实践

**实践目标**：用肉眼「验收」一个 OH! 文件是否符合 Coding Guide。

**操作步骤**：

1. 打开 [stdlib/rtl/oh_mux2.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_mux2.v)。
2. 对照本讲 4.1.2 的五类规则表，逐条勾选：文件名是否等于模块名？端口是否每行一个？是否显式标位宽？信号是否小写、参数是否大写？组合逻辑是否用 `assign`？
3. 再打开任意一个你没读过的文件，例如 `stdlib/rtl/oh_and4.v` 或 `stdlib/rtl/oh_or4.v`，做同样的勾选。

**需要观察的现象**：你会发现这些文件「长得几乎一模一样」——同样的 `//####` 注释框、同样的 `#(parameter ...)`、同样的显式位宽。这就是 Coding Guide 的威力。

**预期结果**：能用自己的话指出每个文件对应了 Coding Guide 的哪几条规则。

> 说明：本实践为「源码阅读型实践」，不需要运行仿真。

#### 4.1.5 小练习与答案

**练习 1**：Coding Guide 规定设计文件里「不写 timescale、不写延时」。请说出这条规定的目的。

> **答案**：timescale 和延时（如 `#10`）是**仿真专用**的构造，综合工具会忽略或报错。把它们隔离在 testbench 里，可以保证 `rtl/` 下的设计文件是「纯可综合」的，仿真和综合看到的是同一份逻辑，避免「仿得过、综不出」的假象。

**练习 2**：Coding Guide 说「禁止 `casex`，用 `casez`」。两者区别是什么？

> **答案**：`casez` 把高阻 `z` 当作 don't-care，而 `casex` 把 `x` 和 `z` 都当作 don't-care（参见 [docs/verilog_reference.md:L537-L538](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/docs/verilog_reference.md#L537-L538)）。因为 `x` 在仿真里代表「未初始化/未知」，`casex` 容易在仿真中意外匹配到含 `x` 的信号，掩盖 bug；`casez` 更安全，因此 OH! 只允许 `casez`。

**练习 3**：为什么 Coding Guide 要求「实例化时始终按名连接（connection by name）」而不是按顺序连接？

> **答案**：按名连接（`.d(d), .clk(clk)`）不依赖端口声明的顺序；将来有人调整端口顺序或插入新端口，按名连接的代码不会悄悄接错线。按顺序连接则会在端口顺序变化时产生「静默错连」的严重 bug。

---

### 4.2 参数化（与 soft/hard 取舍）

#### 4.2.1 概念说明

如果说 Coding Guide 决定了 OH! 文件的「长相」，那么**参数化**决定了 OH! 模块的「复用能力」。

一个朴素的问题是：写一个 8 位加法器，再写一个 16 位加法器，要写两份代码吗？OH! 的答案是：**只写一份，把位宽做成参数**。这样 `oh_add` 既能当 8 位用，也能当 32 位用——「一块积木」拧一拧旋钮就变成「一整套积木」。

更进一步，OH! 把参数化用到了第二层：**用 `SYN` 和 `TYPE` 两个参数在同一份顶层里切换 soft（可综合 RTL）与 hard（ASIC 硬核）两种实现**。这是 OH! 「同一功能、两套实现」架构（u9-l1）的关键开关：仿真和 FPGA 用 soft 版（`SYN="TRUE"`，走 `assign`/`always`），流片用 hard 版（`SYN="FALSE"`，实例化工艺库的 `asic_*` 单元）。

#### 4.2.2 核心流程

OH! 的参数化套路可以归纳成三层旋钮：

```text
第 1 层：尺寸参数（位宽/路数）
   #(parameter N = 1)          # 数据位宽，如 oh_mux2 的 N
   #(parameter DW = 1)         # 数组宽度，如 oh_dffq 的 DW
   #(parameter M = 2)          # 路数，如 oh_mux 的输入向量个数

第 2 层：实现参数（soft/hard 切换）
   #(parameter SYN  = "TRUE",  # "TRUE"=可综合RTL,  "FALSE"=实例化硬核
     parameter TYPE = "DEFAULT") # 硬核的具体工艺类型

第 3 层：generate if (SYN == "TRUE") ... else ... endgenerate
   TRUE  分支：写 assign / always（soft，可综合）
   FALSE 分支：实例化 asic_xxx #(.TYPE(TYPE)) 硬核（hard，绑工艺库）
```

参数在实例化时**按名**传递，禁止位置式、禁止 `defparam`：

```verilog
// ✅ OH! 推荐写法：按名传参 + 按名连接
oh_mux #(.N(8), .M(4)) i_mux (.out(out), .sel(sel), .in(in));

// ❌ Coding Guide 明确禁止的写法
oh_mux #(8, 4) i_mux (out, sel, in);   // 位置式，容易错
defparam i_mux.N = 8;                  // 禁用 defparam
```

关于 soft/hard 的「切换成本」，可以用一个简单的取舍式理解：

\[
\text{实现选择} =
\begin{cases}
\text{soft (SYN="TRUE")} & \text{可综合、可仿真、可上 FPGA，但面积/功耗不是最优} \\
\text{hard (SYN="FALSE")} & \text{面积/功耗/速度最优，但绑定具体工艺库（PDK）}
\end{cases}
\]

> 注：上面的公式只是表达「二选一」的取舍关系，不是数值公式。具体的 `SYN/TYPE` 取值含义以源码注释和 `asiclib` 实现为准（详见 u9-l1、u9-l2）。

#### 4.2.3 源码精读

**(a) 单参数：`oh_mux2` / `oh_dffq`**

前面 4.1.3 已经看过：`oh_mux2 #(parameter N = 1)` 和 `oh_dffq #(parameter DW = 1)` 都只有一个尺寸参数。这是「最小参数化」——只把位宽做成可调。

**(b) 多参数 + soft/hard 切换：`oh_and2.v`**

[stdlib/rtl/oh_and2.v:L8-L33](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_and2.v#L8-L33) 是本讲最重要的范例，它把三层旋钮全用上了：

```verilog
module oh_and2  #(parameter N  = 1,        // block width
                  parameter SYN  = "TRUE",    // synthesizable
                  parameter TYPE = "DEFAULT"  // implementation type
                  )
   (
    input [N-1:0]  a,
    input [N-1:0]  b,
    output [N-1:0] z
    );

   generate
      if(SYN == "TRUE")  begin
         assign z = a & b;            // soft：可综合 RTL
      end
      else begin
         oh_and2 #(.TYPE(TYPE))       // hard：递归实例化（绑工艺）
         oh_and2 (/*AUTOINST*/
                  .z(z[N-1:0]), .a(a[N-1:0]), .b(b[N-1:0]));
      end
   endgenerate
```

- `N` 是第 1 层（位宽）；`SYN/TYPE` 是第 2 层（实现切换）。
- `generate if(SYN=="TRUE")` 是第 3 层：`TRUE` 分支用一行 `assign z = a & b;` 给出 soft 实现；`FALSE` 分支走 hard（此处递归指向自身，配合 `asiclib` 的替换机制——真实硬核单元在 `asiclib` 里，如 `asic_and2`，详见 u9-l2）。
- `/*AUTOINST*/` 注释是给 Emacs verilog-mode 用的自动连线标记，实例化时按名连接 `.z(z)`、`.a(a)`，参数按名传 `#(.TYPE(TYPE))`——完全符合 Coding Guide 的实例化规范。
- 注意 `SYN/TYPE` 是**字符串参数**（`"TRUE"`/`"DEFAULT"`），这是 OH! 用来在 `generate if` 里做编译期分支判断的常见手法。

**(c) 更复杂的多参数：`oh_mux.v`**

[stdlib/rtl/oh_mux.v:L8-L41](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_mux.v#L8-L41) 是一个 N 位宽、M 路输入的 one-hot 选择器，参数更多，但套路一致：

```verilog
module oh_mux
  #(parameter N   = 32,        // vector width
    parameter M   = 2,         // number of vectors
    parameter SYN  = "TRUE",    // synthesizable (or not)
    parameter TYPE = "DEFAULT"  // implementation type
    )
   ...
   generate
      if(SYN == "TRUE") begin
         reg [N-1:0]     mux;
         integer         i;
         always @* begin
            mux[N-1:0] = 'b0;
            for(i=0;i<M;i=i+1)
              mux[N-1:0] = mux[N-1:0] | {(N){sel[i]}} & in[((i+1)*N-1)-:N];
         end
         assign out[N-1:0] = mux[N-1:0];
      end
      else begin
         asic_mux #(.TYPE(TYPE), .N(N))     // hard：实例化 asic_mux
         asic_mux(.out(out), .sel(sel[N-1:0]), .in(in[N-1:0]));
      end
   endgenerate
```

这里能同时看到 Coding Guide 的多条规则一起生效：

- `for` 循环减少重复（Coding Guide："Use for loops to reduce bloat"），且循环变量是 `integer i`（部分支持构造，要求边界静态、只用 `+/-` 索引，见 [docs/verilog_reference.md:L945-L948](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/docs/verilog_reference.md#L945-L948)）。
- `in[((i+1)*N-1)-:N]` 是 Verilog 的「变址部分选择」（indexed part-select），从某 bit 起向下取 N 位——这是参数化切片的标准写法。
- `FALSE` 分支实例化的是 `asic_mux`（hard 单元），按名传参 `#(.TYPE(TYPE), .N(N))`、按名连接端口。这正是 soft/hard 双实现的具象：同一个 `oh_mux`，soft 走 `always`，hard 走 `asic_mux`。

> 与编译选项的关系：u1-l3 提到仿真编译命令里有 `-DCFG_ASIC=0`。`CFG_ASIC` 是**模块外**的全局宏，用来在更顶层选择「整个库走 soft 还是 hard」；而每个模块内部的 `SYN/TYPE` 是**模块内**的局部参数。两者层次不同，本讲只关注后者，前者在 u9-l1 详讲。

#### 4.2.4 代码实践

**实践目标**：亲手写一个参数化的两输入与门，体会 `#(parameter ...)` + 注释端口的套路。

**操作步骤**：

1. 新建一个练习文件（例如 `my_and2.v`，放在你自己的工作目录，**不要**放进 OH! 源码目录以免污染仓库）。注意：OH! 仓库里**已经存在**官方的 [stdlib/rtl/oh_and2.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_and2.v)，所以请把文件名起成 `my_and2` 之类，避免重名。
2. 仿照 `oh_dffq.v` 的注释框 + `oh_mux2.v` 的端口注释风格，写一个**只带位宽参数 `N`**（先不碰 `SYN/TYPE`）的两输入与门。
3. 写完后，与官方 `oh_and2.v` 对比：你的「单参数极简版」和官方的「三参数 soft/hard 版」差在哪。

**需要观察的现象**：你的模块应当只有 `a/b/z` 三个端口，每个端口都有注释；位宽由 `N` 控制；输出用 `assign z = a & b;`。

**预期结果**：参考答案如下（这是**示例代码**，不是 OH! 仓库原有文件）：

```verilog
//#############################################################################
//# Function: 2-input AND gate, parameterized width (practice for u1-l4)      #
//# License:  MIT                                                             #
//#############################################################################

module my_and2 #(parameter N = 1)   // block width
   (
    input [N-1:0]  a,   // first input
    input [N-1:0]  b,   // second input
    output [N-1:0] z    // a & b
    );

   assign z[N-1:0] = a[N-1:0] & b[N-1:0];

endmodule
```

对照官方 [stdlib/rtl/oh_and2.v:L18-L32](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_and2.v#L18-L32)：官方版多了 `generate if(SYN=="TRUE")` 的分支和 hard 递归实例化——那就是 soft/hard 双实现的「第二层旋钮」。你的极简版相当于永远走 soft 分支。

> 运行验证（可选）：想本地跑一下，可按 u1-l3 的三步流程，给你的 `my_and2` 配一个 dut 包装和 `.emf` 测试。若暂无环境，本实践作为「源码阅读 + 手写对照型实践」即可，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 OH! 用字符串参数 `"TRUE"/"FALSE"` 而不是整数 `0/1` 来做 `SYN` 开关？

> **答案**：字符串参数在 `generate if(SYN=="TRUE")` 里做**编译期**字符串比较，语义自解释（`"TRUE"` 一眼看出是「可综合」），可读性优于魔法数字。同时字符串不会被综合工具当成真实电路信号，是纯粹的「配置旋钮」。注意：字符串参数是 Verilog 2001 起支持的特性，属于 OH! 锁定的 Verilog 2005 范围内。

**练习 2**：`oh_mux.v` 里 `FALSE` 分支实例化了 `asic_mux`。如果你只装了 iverilog、没有 ASIC 工艺库，直接综合 `oh_mux`（`SYN="FALSE"`）会发生什么？

> **答案**：仿真器/综合器会找不到 `asic_mux` 模块定义而报错（除非 `asiclib` 在库搜索路径 `-y` 里，见 u1-l3 的 `libs.cmd`）。这正是 soft/hard 分离的意义：日常仿真和 FPGA 一律用 `SYN="TRUE"`（soft，纯 RTL，自包含），只有真正做 ASIC 流片、备齐工艺库时才切到 `FALSE`。

**练习 3**：Coding Guide 既说「尽量参数化」，又说「Parametrize as much as possible but not more」。这两句话怎么统一理解？

> **答案**：该参数化的「尺寸」（位宽、路数、深度）一定要参数化，这是复用的核心；但不要为了想象中的「灵活性」把每个常量都做成参数，否则实例化时参数列表臃肿、默认值难记、易传错。「尽可能参数化，但别过度」——参数的数量应当刚好覆盖真实的复用需求。

---

## 5. 综合实践

把本讲两块内容（编码规范 + 参数化）串起来，完成下面这个综合小任务。

**任务**：为 OH! 「新增」一个最简 IP——一个参数化的 2 输入或门 `my_or2`，要求严格符合 Coding Guide，并体现 soft/hard 参数化的思路。

**要求**：

1. **文件组织**：文件名 `my_or2.v` == 模块名 `my_or2`；顶部用 `//####` 框写 Function / License 注释；只用单行 `//` 注释。
2. **端口**：三个端口 `a/b/z`，每个端口加注释，每行一个，显式标 `[N-1:0]` 位宽。
3. **参数化**：至少有位宽参数 `N`（进阶：再加 `SYN/TYPE` 两个参数，模仿 `oh_and2.v` 的 `generate if/else` 双分支）。
4. **赋值**：soft 分支用 `assign z[N-1:0] = a[N-1:0] | b[N-1:0];`；注意是按位或 `|`（不是逻辑或 `||`）。
5. **自查**：对照 4.1.2 的五类规则表，逐条勾选你的文件是否合规。

**参考答案（示例代码，非仓库原有文件，进阶版含 soft/hard 分支）**：

```verilog
//#############################################################################
//# Function: 2-input OR gate, parameterized width (u1-l4 comprehensive task) #
//# License:  MIT                                                             #
//#############################################################################

module my_or2 #(parameter N    = 1,        // block width
                parameter SYN  = "TRUE",    // synthesizable
                parameter TYPE = "DEFAULT"  // implementation type
                )
   (
    input [N-1:0]  a,   // first input
    input [N-1:0]  b,   // second input
    output [N-1:0] z    // a | b
    );

   generate
      if (SYN == "TRUE") begin
         assign z[N-1:0] = a[N-1:0] | b[N-1:0];   // soft
      end
      else begin
         asic_or2 #(.TYPE(TYPE), .N(N))            // hard（需 asiclib 支持）
         asic_or2 (/*AUTOINST*/
                   .z (z[N-1:0]),
                   .a (a[N-1:0]),
                   .b (b[N-1:0]));
      end
   endgenerate

endmodule
```

做完后，把它和官方 [stdlib/rtl/oh_and2.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_and2.v) 并排对比：结构应该几乎一致，只是把「与 `&`」换成了「或 `|`」。如果你能写出这份代码并解释每一行对应 Coding Guide 的哪一条，本讲就过关了。

> 提示：仓库中 `stdlib/rtl/` 下已有官方的 `oh_or4.v`、`oh_nor2.v` 等门电路，写完后可以打开它们对照检查自己的风格是否一致。

## 6. 本讲小结

- OH! 全库锁定 **Verilog 2005**（`iverilog -g2005` 对应），设计文件只用一小撮确定可综合的关键字（见 [README.md:L162](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/README.md#L162) 的白名单）。
- **Coding Guide**（[README.md:L110-L163](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/README.md#L110-L163)）是 OH! 的家规，可归并为「文件组织 / 命名 / 端口注释 / 赋值时序 / 实例化参数」五类；核心要点：一文件一模块、小写信号大写参数、显式位宽、非阻塞赋值、低有效复位、按名连接、case 带 default、禁用 casex。
- 最小样板是 `oh_dffq.v`（时序、无复位）与 `oh_mux2.v`（组合），它们示范了「参数化 + 显式位宽 + 注释端口」的最朴素写法。
- **参数化**是 OH! 复用的核心：`#(parameter N=...)` 让位宽可调；`SYN/TYPE` 两个参数用 `generate if(SYN=="TRUE")` 在同一份顶层里切换 **soft（可综合 RTL）** 与 **hard（ASIC 硬核 `asic_*`）** 两种实现。
- 头文件用 `.vh` + `` `ifndef / `define / `endif `` 守卫，常量/寄存器地址都用大写宏定义（如 [gpio_regmap.vh](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/gpio/hdl/gpio_regmap.vh)）。
- 读 OH! 源码的通用方法：先看顶部 `//####` 注释框（功能 + License），再看 `#(parameter ...)` 的参数表（这是模块的「旋钮」），最后看 `generate if(SYN==...)` 判断当前走的是 soft 还是 hard 分支。

## 7. 下一步学习建议

- **本讲打下的基础如何承接后续**：你现在能读懂任意一个 `stdlib/rtl/*.v` 的「骨架」。下一单元（u2）就开始逐族拆解这些原语——建议从 **u2-l2（时序原语：触发器家族）** 读起，因为它直接建立在 `oh_dffq` / `oh_dffrq` 之上，会展开 DFF/锁存器/带复位置位的完整家族。
- **如果你想立刻练手参数化**：浏览 `stdlib/rtl/` 目录（本讲列出过它的文件清单），挑 `oh_or4.v`、`oh_nand2.v`、`oh_xnor2.v` 这类小文件，验证它们是否都遵循 `oh_and2.v` 的 `SYN/TYPE` 双分支套路。
- **如果你对 soft/hard 双实现好奇**：可以提前扫一眼 `asiclib/hdl/asic_dffq.v`，对比 `oh_dffq.v`，初步感受 hard 版多出来的端口——完整的对比留到 **u9-l1（soft vs hard）** 和 **u9-l2（asiclib 标准单元库）**。
- **关于可综合性**：想更系统地判断「某段 Verilog 能不能综合」，参考 [docs/verilog_reference.md 的 Synthesis Constructs 一节](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/docs/verilog_reference.md#L909-L965)，它把构造分成 Fully / Partially / Ignored / Unsupported 四档。
