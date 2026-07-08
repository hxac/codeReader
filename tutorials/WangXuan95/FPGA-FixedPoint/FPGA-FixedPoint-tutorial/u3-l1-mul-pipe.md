# 流水线设计模式与 fxp_mul_pipe

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚**为什么要把单周期组合逻辑改造成流水线**：关键路径过长导致时钟频率上不去，流水线通过在段间插寄存器来切断长路径，用「固定延迟」换「更高频率」。
- 掌握本库**流水线模块的统一接口约定**：相比单周期版多出 `rstn`/`clk` 两个端口、`out`/`overflow` 由 `wire` 改为 `reg`、用 `initial` 初始化输出、用 `always @(posedge clk or negedge rstn)` 做异步复位同步加载。
- 逐段读懂 [`fxp_mul_pipe`](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L326-L380) 的 **2 级流水线**：第 1 级把乘积 `res` 寄存一拍，中间的组合逻辑 `fxp_zoom` 算出 `outc`，第 2 级把 `outc` 再寄存一拍——`fxp_zoom` 本身不变，只是被两段寄存器夹在中间。
- 理解流水线模块与对应单周期模块在**功能上完全等价**，只是给输出引入了固定 2 拍延迟；以及「**无气泡**」——每个时钟都能吃进一个新输入、吐出一个旧结果。
- 能根据 latency 把输入与输出在时间轴上对齐，写出**逐拍自校验**的流式 testbench，用通过/失败计数验证「2 拍延迟 + 无吞吐损失」。

## 2. 前置知识

本讲是专家层的第一篇，承接 [u2-l2（fxp_mul 单周期乘法）](./u2-l2-mul.md)。你需要已经掌握：

- 定点数值 = 有符号补码码值 ÷ \(2^{W_F}\)，以及全库统一参数 `WIIA/WIFA`、`WIIB/WIFB`、`WOI/WOF`、`ROUND` 的含义。
- `fxp_mul` 的两段式结构：`$signed(ina)*$signed(inb)` 得到全精度积 `res`（位宽 `WRI=WIIA+WIIB`、`WRF=WIFA+WIFB`），再由唯一的 `fxp_zoom` 收敛到 `(WOI,WOF)` 并做舍入与溢出饱和。
- `fxp_zoom` 是全库的位宽搬运工，内部全部是 `always @(*)` 组合逻辑（[RTL/fixedpoint.v:22-94](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L22-L94)）。

本讲要回答的新问题是：**`fxp_mul` 是纯组合逻辑，为什么还要专门做一个 `fxp_mul_pipe`？多出来的 `clk`/`rstn` 和两段 `always` 块到底在干什么？** 为此需要先建立两个底层概念——**关键路径**与**流水线**。

> **一句话直觉：** 组合逻辑像一条「单条长流水线」，一笔订单要从头走到尾才能出货，订单之间只能排队；插入寄存器后变成「多级流水线」，每一级只做一小段活，每个时钟都能吞新订单、出旧货——延迟变长，但吞吐量和可跑的时钟频率都上去了。

## 3. 本讲源码地图

| 文件 | 作用 |
| :--- | :--- |
| [RTL/fixedpoint.v](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v) | 全部可综合模块所在。本讲主角是 `fxp_mul_pipe`（第 326–380 行），并把它与单周期基线 `fxp_mul`（第 278–310 行）逐行对照；中间夹着的组合逻辑 `fxp_zoom`（第 22–94 行）已在 u1-l3 讲透，本讲只复用其结论。 |
| [SIM/tb_fxp_mul_div_pipe.v](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_fxp_mul_div_pipe.v) | 流水线乘除法 testbench：含 50MHz 时钟、`rstn` 复位释放、`test` 任务、逐拍 `$display` 对比 SW-result 与 HW-result。本讲剖析它的时钟/复位约定，并指出其「目视对比」并未做严格延迟对齐——综合实践中我们会补上一个自校验版本。 |
| [SIM/tb_fxp_mul_div_pipe_run_iverilog.bat](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_fxp_mul_div_pipe_run_iverilog.bat) | 一键编译运行脚本：`iverilog -g2001 -o sim.out tb_fxp_mul_div_pipe.v ../RTL/fixedpoint.v && vvp -n sim.out`（testbench 与 RTL 必须同时参与编译）。 |

## 4. 核心概念与源码讲解

### 4.1 为什么需要流水线：单周期瓶颈与流水线思想

#### 4.1.1 概念说明

`fxp_mul` 是**纯组合逻辑（combinational logic）**：输入一变，输出经过一段逻辑门延迟后立刻跟着变，中间没有任何时钟、没有寄存器。一个模块从输入到输出所经过的最长门延迟路径，叫做**关键路径（critical path）**。系统的最高时钟频率（\(f_{\max}\)）被关键路径倒过来决定：

\[
f_{\max}\approx \frac{1}{T_{\text{critical}}}
\]

`fxp_mul` 的关键路径是「**乘法器 → fxp_zoom（舍入加法 + 溢出比较 + 多路选择）**」一整条。其中乘法器本身就是 FPGA 上最重的运算单元之一，再串联一段 `fxp_zoom` 的比较与选择逻辑，整条路径很长，\(f_{\max}\) 很难做高。当你的设计需要跑在较高时钟下、或这条乘法处于时序紧张的支路上时，单周期版本就成了瓶颈。

**流水线（pipeline）** 的解决思路非常朴素：在长组合路径中间**插入寄存器**，把它切成若干短段。寄存器像一个「水闸」，每个时钟沿把上一段的结果锁存下来，作为下一段的输入。这样：

- 关键路径从「整条长路径」缩短为「最长的一小段」，\(f_{\max}\) 显著提升。
- 代价是输出要等若干拍后才出现，这就是**延迟（latency）**。
- 好处是每个时钟都能吞入一个新输入、同时吐出一个旧结果，**吞吐量（throughput）** 仍是每拍一个，没有损失。

#### 4.1.2 核心流程

把一条组合逻辑 `out = f(in)` 切成 \(L\) 段 \(f = f_L\circ\cdots\circ f_1\)，段间插入寄存器：

```
in ─▶ [ f1 ] ─▶ reg ─▶ [ f2 ] ─▶ reg ─▶ ... ─▶ [ fL ] ─▶ reg ─▶ out
        ↑每个时钟沿，各级 reg 同时锁存上一级的输出
```

设每段延迟约 \(t_s\)，则：

\[
\text{关键路径} \approx t_s \quad(\text{从一段的输入到下一段的 reg 输入})
\]

\[
\text{延迟 } L \text{（拍）}, \qquad \text{吞吐量} = 1\text{ 结果/拍（无气泡）}
\]

对 \(N\) 个连续输入，总耗时约 \(N+L\) 拍，平均吞吐率：

\[
\text{平均吞吐率} = \frac{N}{N+L} \xrightarrow{N\to\infty} 1
\]

> **关键洞察：** 流水线**不减少**单个数据的处理时间（反而多了 \(L\) 拍延迟），它提升的是**单位时间能处理的数据总量**。这就好比洗衣服：单周期是「一台洗衣机从头洗到干」，流水线是「洗衣机→烘干机→叠衣机」级联，虽然每件衣服要依次走完三台机器，但三台机器可以同时各处理一件，整体流量大增。

#### 4.1.3 源码精读

先看单周期基线 `fxp_mul` 的关键路径长在哪里：

[RTL/fixedpoint.v:296](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L296) —— `wire signed [WRI+WRF-1:0] res = $signed(ina) * $signed(inb);`。这是一条**连续赋值语句**（`wire` + `=`），纯组合：`res` 在输入变化的瞬间就开始重新计算，没有时钟参与。

[RTL/fixedpoint.v:298-308](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L298-L308) —— 紧接着的 `fxp_zoom res_zoom` 也是纯组合（其内部是 `always @(*)`）。于是从 `ina/inb` 到 `out` 的整条路径是「乘法 → 舍入 → 溢出比较 → 饱和选择」一气呵成，中间没有任何寄存器打断——这就是 `fxp_mul` 的关键路径。

`fxp_mul_pipe` 要做的，就是**在这条长路径的天然中点（乘法器输出 `res` 处）插一刀**：把 `res` 变成寄存器，再在 `fxp_zoom` 输出后再插一刀，切成「乘法」「位宽收敛」两级。

#### 4.1.4 代码实践

**实践目标：** 在 testbench 里认清时钟与复位约定，并算出本配置下的中间位宽，为后续读懂流水线分段做准备。

**操作步骤：**

1. 打开 [SIM/tb_fxp_mul_div_pipe.v:24-27](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_fxp_mul_div_pipe.v#L24-L27)，看清时钟与复位：`clk` 初值 `1'b1`、`always #(10000) clk=~clk`（半周期 10000ps，即周期 20ns → 50MHz）；`rstn` 初值 `1'b0`，经过 4 个上升沿后释放为 `1`。
2. 看 [SIM/tb_fxp_mul_div_pipe.v:16-21](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_fxp_mul_div_pipe.v#L16-L21) 的配置 `WIIA=12,WIFA=20,WIIB=14,WIFB=18,WOI=24,WOF=17`，手算 `WRI=12+14=26`、`WRF=20+18=38`，全精度积 `res` 共 `26+38=64` 位。

**需要观察的现象：** 时钟周期 20ns 是一个固定的「节拍器」；`rstn` 低电平有效的复位在仿真开头先持续若干拍，让流水线各级寄存器先清零再开始工作。

**预期结果：** 周期 20ns（50MHz）；`res` 为 64 位有符号数。可在仿真时用 `$display` 打印 `$time` 验证时钟节拍（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1：** 为什么说「插入寄存器能提高 \(f_{\max}\)」？提高的代价是什么？

> **答案：** 寄存器把一条长组合路径切成若干短段，关键路径从「整条」缩短为「最长的一小段」，于是 \(f_{\max}\approx 1/t_s\) 提高。代价是输出延迟 \(L\) 拍才出现（latency 增加），且多了寄存器面积；但吞吐量仍为每拍一个，不亏。

**练习 2：** 若一个流水线有 3 级、连续处理 1000 个数据，总耗时大约多少拍？平均吞吐率是多少？

> **答案：** 总耗时约 \(1000+3=1003\) 拍；平均吞吐率 \(1000/1003\approx 0.997\)，非常接近 1——这就是「无气泡」的体现：数据量越大，启动延迟的相对开销越小。

---

### 4.2 流水线模块的统一接口与时钟复位约定

#### 4.2.1 概念说明

本库所有流水线模块（`fxp_mul_pipe`、`fxp_div_pipe`、`fxp_sqrt_pipe`、`fxp2float_pipe`、`float2fxp_pipe`）都遵守同一套接口约定，掌握这一个就能举一反三。与单周期版相比，流水线版的端口和声明有 4 处统一变化：

1. **多两个端口** `rstn`（低有效异步复位）和 `clk`（时钟）。
2. **输出由 `wire` 改为 `reg`**：因为输出现在由 `always` 块里的寄存器驱动，而不是组合线网。
3. **`initial` 初始化输出**：让寄存器在仿真 \(t=0\)（第一个时钟沿到来前）就有确定值 `0`，避免输出在开头出现 `x`。
4. **`always @(posedge clk or negedge rstn)`**：异步复位、同步加载——复位沿到来立刻清零，正常时每个上升沿锁存新值。

理解这 4 点后，看任何一个 `_pipe` 模块都只是「单周期版 + 这套时钟复位包装」。

#### 4.2.2 核心流程

异步复位同步加载的 always 模板：

```
always @(posedge clk or negedge rstn)   // 时钟上升沿 或 复位下降沿 都触发
    if(~rstn)          // 复位优先级最高：rstn 一变低，立刻生效（异步）
        reg <= 0;       // 各级寄存器清零
    else               // 正常工作：每个 clk 上升沿锁存新值（同步）
        reg <= 下级输入;
```

这里有两个 Verilog 细节对初学者很关键：

- **`<=`（非阻塞赋值）**：在 `always @(posedge clk)` 里必须用 `<=`。所有 RHS 先求值、再统一更新，保证多级寄存器「同时」锁存上一级的旧值，不会出现级联窜位。若误用 `=`（阻塞赋值），数据会在一个时钟沿内连续窜过好几级，流水线就乱了。
- **读寄存器的时机**：在某个 `posedge` 处，另一段 `always` 读到的 `out` 是**上一拍**锁存进去的值（NBA 语义：本沿的赋值要到时间步末尾才生效）。这条规则是下一节「延迟对齐」推演的基础。

#### 4.2.3 源码精读

`fxp_mul_pipe` 的端口声明完美体现了约定 1、2：

[RTL/fixedpoint.v:335-341](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L335-L341) —— 比单周期 `fxp_mul` 多了 `input wire rstn, clk`（[RTL/fixedpoint.v:287-290](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L287-L290) 里没有这两个端口），并把 `out`、`overflow` 从 `output wire` 改成了 `output reg`——因为它们马上要由寄存器驱动。

约定 3 的 `initial`：

[RTL/fixedpoint.v:343](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L343) —— `initial {out, overflow} = 0;`。把两个输出寄存器拼成临时向量整体赋 0，等价于 `out=0; overflow=0;`。这样仿真一启动输出就是确定的 0，而不是 `x`。注意 `res` 本身在声明处也带了初值（[第 351 行](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L351) `reg signed [...] res = 0;`），同样是这个目的。

约定 4 的复位模板，在本模块里出现两次（第 1、2 级各一次）：

[RTL/fixedpoint.v:353-357](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L353-L357) —— 第 1 级寄存器 `res`：`posedge clk or negedge rstn` 触发，`~rstn` 时 `res<=0`，否则 `res <= $signed(ina)*$signed(inb)`。

[RTL/fixedpoint.v:371-378](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L371-L378) —— 第 2 级寄存器 `out/overflow`：同样的触发与复位模板，正常时锁存中间组合结果 `outc/overflowc`。

把单周期版与流水线版的差异整理成表：

| 维度 | `fxp_mul`（单周期） | `fxp_mul_pipe`（2 级流水线） |
| :--- | :--- | :--- |
| 时钟/复位端口 | 无 | **有 `rstn`、`clk`** |
| `out`/`overflow` 类型 | `wire`（组合驱动） | **`reg`（寄存器驱动）** |
| `res` 类型 | `wire signed`（连续赋值） | **`reg signed`（posedge 锁存）** |
| 中间结果 `outc` | 无（直连 `out`） | **有 `wire outc`（fxp_zoom 组合输出）** |
| `initial` 初始化 | 无 | **`initial {out,overflow}=0;`** |
| 计算位置 | 全组合 | 2 级 `always @(posedge clk or negedge rstn)` |
| 输出延迟 | 0 拍 | **2 拍** |

#### 4.2.4 代码实践

**实践目标：** 用「找不同」的方式，把 `fxp_mul` 到 `fxp_mul_pipe` 的结构变化逐条点出来，固化对统一约定的记忆。

**操作步骤：**

1. 并排打开 [RTL/fixedpoint.v:278-310](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L278-L310)（`fxp_mul`）和 [RTL/fixedpoint.v:326-380](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L326-L380)（`fxp_mul_pipe`）。
2. 逐项核对上表的 7 行差异，确认每一处都能在源码里指到具体行。
3. 注意一个**相同点**：`localparam WRI=WIIA+WIIB; WRF=WIFA+WIFB;` 与 `fxp_zoom res_zoom` 的例化参数在两个模块里**一模一样**（[第 293-294、298-308 行](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L293-L308) vs [第 345-346、359-369 行](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L345-L369)）——说明流水线改造**没有动算法本身**，只在外围加了时钟/寄存器。

**需要观察的现象：** 两者的「数学内核」（乘法 + `fxp_zoom`）完全一致，区别只在 `res` 是 `wire` 还是 `reg`、以及输出有没有再寄存一拍。

**预期结果：** 能口述「把 `fxp_mul` 改成 `fxp_mul_pipe` 只需 4 步：加 `rstn/clk`、`out` 改 `reg`、`res` 从连续赋值改成 `posedge` 锁存、`fxp_zoom` 的输出再寄存一拍」。

#### 4.2.5 小练习与答案

**练习 1：** 为什么 `fxp_mul_pipe` 里的 `always` 块必须用 `<=` 而不能用 `=`？

> **答案：** 这是时序逻辑（`always @(posedge clk)`）的标准要求。`<=`（非阻塞）保证所有寄存器的 RHS 先求值、再在本时间步末尾统一更新，使多级寄存器在同一沿「同时」锁存上一级的**旧**值。若用 `=`（阻塞），求值与赋值立即发生，数据可能在一个沿内连续窜过多级，破坏流水线的节拍对齐。

**练习 2：** `initial {out, overflow} = 0;` 和 `always` 块里的 `if(~rstn) out<=0;` 都把输出清零，二者是否重复？

> **答案：** 不完全重复，作用阶段不同。`initial` 只在仿真 \(t=0\) 执行一次，让寄存器在**第一个时钟沿到来之前**就有确定值，避免开头输出 `x`（综合时通常被当作复位初值或被忽略）；`always` 里的 `if(~rstn)` 则是**运行期**的异步复位，任何时候 `rstn` 拉低都立刻清零。两者互补：一个管「上电初值」，一个管「运行中复位」。

---

### 4.3 fxp_mul_pipe 两级流水线精读

#### 4.3.1 概念说明

`fxp_mul_pipe` 是本库**最简单的流水线模块**——只有 2 级，是理解后续 `fxp_div_pipe`（\(WOI+WOF+3\) 级）、`fxp_sqrt_pipe`（\(\lfloor WII/2\rfloor+WIF+2\) 级）等复杂流水线的入门范本。它的两级划分天然落在「乘法器」与「`fxp_zoom`」之间：

- **第 1 级**：把 `$signed(ina)*$signed(inb)` 的结果锁存进 `res`（切断乘法器这条最长路径）。
- **中间组合层**：`fxp_zoom` 对 `res` 做位宽收敛、舍入与溢出饱和，得到组合输出 `outc`——它本身**没有寄存器**，仍是 u1-l3 讲过的那套 `always @(*)` 组合逻辑。
- **第 2 级**：把 `outc`（和 `overflowc`）再锁存一拍，形成最终输出 `out`/`overflow`。

关键认识：**夹在两级寄存器之间的 `fxp_zoom` 一字未改**。流水线改造只是「在组合逻辑的首尾各加一个寄存器」，并不触碰中间的算法。

#### 4.3.2 核心流程

数据流结构（注意 `fxp_zoom` 是组合、夹在两段寄存器之间）：

```
          ┌──────────────────────┐  res(reg)  ┌──────────┐ outc(wire) ┌──────────────────────┐  out(reg)
ina ─────▶│ $signed(ina) *       │───────────▶│ fxp_zoom │───────────▶│                      │─────▶ out
inb ─────▶│ $signed(inb)         │            │ (组合逻辑)│            │                      │─────▶ overflow
          │ ← 第1级 @(posedge clk)│            │ WRI,WRF  │            │ ← 第2级 @(posedge clk)│
          │   或 negedge rstn     │            │  →WOI,WOF│            │   或 negedge rstn     │
          └──────────────────────┘            └──────────┘            └──────────────────────┘
                   ↑ clk, rstn                                            ↑ clk, rstn
```

逐拍跟踪一个输入 \(A\times B\)（设 `ina=A` 在 cycle \(k\) 期间有效，即在第 \(k\) 个上升沿由激励用 `<=` 写入）：

```
cycle  :      k          k+1           k+2          k+3
ina    :    [ A ] ─────────────────────────────────────────
res(reg):    ... ──▶ at posedge k+1 采样到 ina=A ──▶ [ A*B ] ─────
outc(组合):                       ──▶ fxp_zoom(A*B) ──────
out(reg):                                 ──▶ at posedge k+2 采样到 outc ──▶ [ result(A) ]
读 out :                                                    ↑ posedge k+3 读到 result(A)
```

结论：**输入在第 \(k\) 拍写入，输出在第 \(k+2\) 拍稳定为对应结果**——延迟 \(L=2\) 拍。这正是模块注释 `pipeline stage = 2` 的含义。

#### 4.3.3 源码精读

逐段拆解 `fxp_mul_pipe`（[RTL/fixedpoint.v:326-380](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L326-L380)）：

**位宽参数与中间线网声明：**

[RTL/fixedpoint.v:345-351](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L345-L351) —— `WRI/WRF` 与单周期版完全相同；`outc/overflowc` 是 `fxp_zoom` 的组合输出线网（`wire`），`res` 是第 1 级寄存器（`reg signed`，带初值 0）。注意 `res` 现在是 `reg`，与 `fxp_mul` 里的 `wire signed [...] res = ...` 形成对照——这是「组合→寄存」的核心改写。

**第 1 级寄存器（锁存乘积）：**

[RTL/fixedpoint.v:353-357](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L353-L357) —— 异步复位同步加载。复位时 `res<=0`；否则每个上升沿把 `$signed(ina)*$signed(inb)` 锁进 `res`。这一级切断了乘法器这条最长路径。两个 `$signed()` 依然缺一不可，原因同 u2-l2：负数必须按补码相乘。

**中间组合层（fxp_zoom，未改动）：**

[RTL/fixedpoint.v:359-369](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L359-L369) —— `res_zoom` 例化，参数与 `fxp_mul` 里的 `res_zoom` **逐字相同**：`(WII=WRI, WIF=WRF) → (WOI, WOF)`，透传 `ROUND`。唯一区别是它的输出接到 `outc/overflowc` 这两个中间线网，而不是直接驱动模块输出——因为输出还要再寄存一拍。`$unsigned(res)` 把 `signed reg` 转成 `fxp_zoom` 入口所需的普通线网，比特内容不变（u2-l2 已解释）。

**第 2 级寄存器（锁存最终输出）：**

[RTL/fixedpoint.v:371-378](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L371-L378) —— 同样的异步复位同步加载模板。复位时 `out<=0; overflow<=1'b0;`；否则每个上升沿把组合层的 `outc/overflowc` 锁进 `out/overflow`。这一级把 `fxp_zoom` 的组合延迟也隔离开，使第 2 级关键路径仅剩 `fxp_zoom` 一段。

把三段串起来看，**唯一被「寄存器化」的是数据通路的首尾两个节点**（`res` 与 `out`），中间的 `fxp_zoom` 原封不动——这就是本库流水线改造的最小、最典型形态。

#### 4.3.4 代码实践

**实践目标：** 用「单点探针」亲眼看见 `res` 与 `out` 的 2 拍节拍差。

**操作步骤：**

1. 复制 [SIM/tb_fxp_mul_div_pipe.v](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_fxp_mul_div_pipe.v) 为一个学习用 testbench（**示例代码**，勿改原文件），在例化 `fxp_mul_pipe_i` 后，用层次化路径把第 1 级寄存器 `res` 也引出来观察。最简单的做法是把 DUT 内部信号通过层次名打印：

```verilog
// ============ 示例代码：在 tb_fxp_mul_div_pipe.v 基础上加探针（读者自行新建文件） ============
// 假设例化名仍为 fxp_mul_pipe_i，可在 display 的 always 块里追加一行：
// $display("  [probe] res=%h out=%h", fxp_mul_pipe_i.res, omul);
```

2. 跑仿真：`iverilog -g2001 -o sim.out tb_study.v ../RTL/fixedpoint.v && vvp -n sim.out`。
3. 在打印里找一组确定的输入（例如第一个 `test('ha09b63b3,'h1d320443)`），数一数 `res` 出现非零乘积的拍数与 `out` 出现对应结果的拍数之差。

**需要观察的现象：** 同一个输入的乘积先出现在 `res`（第 1 级输出），再过一拍才出现在 `out`（第 2 级输出）——两级之间正好差 1 拍，加上输入到 `res` 的 1 拍，输入到 `out` 共 2 拍。

**预期结果：** `res` 比 `out` 提前 1 拍反映同一输入的结果；输入激励写入后第 2 拍 `out` 才稳定。具体探针数值「待本地验证」，但节拍差应为固定的 2。

#### 4.3.5 小练习与答案

**练习 1：** `fxp_mul_pipe` 的 `fxp_zoom` 例化与 `fxp_mul` 的有何不同？为什么？

> **答案：** 参数与算法完全相同，唯一区别是输出连接对象：`fxp_mul` 里 `fxp_zoom` 直接驱动模块输出 `out`（`wire`）；`fxp_mul_pipe` 里 `fxp_zoom` 驱动中间线网 `outc`，再由第 2 级寄存器把 `outc` 锁存为 `out`。因为流水线需要在组合输出后再加一级寄存器，所以 `fxp_zoom` 不能再直连最终输出。

**练习 2：** 如果只插第 1 级寄存器（锁存 `res`）而**不插**第 2 级（让 `fxp_zoom` 直连 `out`），延迟会变成多少？这样做有什么缺点？

> **答案：** 延迟变为 1 拍。缺点是第 2 级关键路径仍然是完整的 `fxp_zoom`（舍入加法 + 溢出比较 + 饱和选择），时序改善不如 2 级彻底；而且输出变成纯组合（对 `res` 而言），扇出和毛刺特性更差。本库选择 2 级是为了把乘法与位宽收敛分别隔离，关键路径更短、\(f_{\max}\) 更高。

---

### 4.4 延迟对齐与无气泡：流水线验证方法学

#### 4.4.1 概念说明

验证流水线模块比验证单周期模块多两个要点：

1. **延迟对齐（latency alignment）**：输出滞后输入 \(L\) 拍。若像单周期那样直接拿「当前输入算出的软件期望」和「当前输出」比，必然错位——软件期望对应第 \(k\) 拍输入，硬件输出却还是第 \(k-L\) 拍的结果。必须把软件参考**也延迟 \(L\) 拍**，或把硬件输出**回溯到 \(L\) 拍前的输入**，才能对齐比较。
2. **无气泡吞吐（no-bubble throughput）**：要证明每个时钟都能处理一个新数据，而不是「给一个输入、等 \(L\) 拍、再给下一个」。做法是**连续流式激励**——每拍都喂新输入，然后逐拍比对，统计通过/失败计数；若通过数 ≈ 总采样数，则吞吐无损。

本库的官方 testbench [tb_fxp_mul_div_pipe.v](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_fxp_mul_div_pipe.v) 采用的是**目视对比**风格：它确实每拍喂新输入（体现无气泡），但 `$display` 把「当前输入的软件参考」与「当前硬件输出」并排打印，**没有做延迟对齐**——读者要在输出文本里自己数「HW-result 比 SW-result 滞后 2 行」来确认延迟。综合实践中，我们会补一个**自校验**版本：用「黄金模型延迟对齐 + 通过/失败计数」一步到位地同时验证「2 拍延迟」和「无气泡」。

#### 4.4.2 核心流程

**对齐技巧：黄金模型 + 延迟线。** 把单周期 `fxp_mul`（组合逻辑，与软件数学完全等价）当作「黄金参考」，和待测 `fxp_mul_pipe` 喂**完全相同**的输入。由于 `fxp_mul` 是 0 延迟、`fxp_mul_pipe` 是 2 拍延迟，只要把黄金输出**用一个 2 级移位寄存器延迟 2 拍**，就能与流水线输出在时间上对齐：

```
                          ┌─ fxp_mul (黄金, 组合, 0延迟) ─ out_gold ─▶ [reg] ─▶ [reg] ─▶ gold_d1
ina/inb ──┬──────────────▶│                                                    ↑ 延迟2拍
          │                └──────────────────────────────────────────────────┐
          │                                                                     │
          └─▶ fxp_mul_pipe (待测, 2级流水) ────────────────────────────── out_pipe ─▶ 比较 out_pipe == gold_d1 ?
```

**为什么对齐？** 在 4.3.2 的时间轴基础上可严格推出：在任意上升沿 \(m\)（流水线打满后）读到 `out_pipe` 对应第 \(m-3\) 拍写入的输入；而 `gold_d1`（2 级延迟线，读黄金组合输出）在同一沿也恰好对应第 \(m-3\) 拍写入的输入。两者逐拍对齐、逐拍可比。**无气泡**则体现在：每拍都喂新输入、每拍都有一次有效比较，通过数应等于总采样数。

> **为何多延迟 1 拍（延迟线深度 2，但读出对齐到 \(m-3\)）？** 因为「读寄存器」本身滞后一拍——在 `posedge` 处读到的是上一拍锁存的值（见 4.2.2）。黄金组合输出 `out_gold` 对应当前输入（第 \(m-1\) 拍写入），经 2 级寄存器后读出对应第 \(m-3\) 拍写入的输入，正好匹配 `out_pipe` 的读出时刻。

#### 4.4.3 源码精读

剖析官方 testbench 的时钟、复位、激励与打印：

[SIM/tb_fxp_mul_div_pipe.v:24-27](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_fxp_mul_div_pipe.v#L24-L27) —— 50MHz 时钟与 `rstn` 复位释放：`repeat(4) @(posedge clk); rstn<=1'b1;` 表示先复位 4 拍再释放，让流水线各级先清零。

[SIM/tb_fxp_mul_div_pipe.v:38-53](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_fxp_mul_div_pipe.v#L38-L53) —— `fxp_mul_pipe` 的例化，配置 `WIIA=12,WIFA=20,WIIB=14,WIFB=18,WOI=24,WOF=17,ROUND=1`，端口比单周期版多接了 `rstn`、`clk`。

[SIM/tb_fxp_mul_div_pipe.v:74-82](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_fxp_mul_div_pipe.v#L74-L82) —— `test` 任务：`@(posedge clk); ina<=_ina; inb<=_inb;` 每调用一次推进一拍并喂入新输入。配合主激励块里的连续 `test(...)` 调用，形成**每拍一新输入**的流式激励（无气泡）。

[SIM/tb_fxp_mul_div_pipe.v:85-99](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_fxp_mul_div_pipe.v#L85-L99) —— 逐拍 `$display`：用 `($signed(ina)*1.0)/(1<<WIFA)` 等把**当前**输入与**当前**输出还原成浮点并排打印，溢出时追加 `(o)`。注意这里的 SW-result（由当前 `ina/inb` 算出）与 HW-result（当前的 `omul`）**并未做延迟对齐**——这正是本讲综合实践要补强的点：在打印文本里，HW-result 列会比 SW-result 列滞后 2 行。

[SIM/tb_fxp_mul_div_pipe.v:102-155](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_fxp_mul_div_pipe.v#L102-L155) —— 主激励：复位释放后连续调用约 48 次 `test`（每拍一新输入），再 `repeat(WOI+WOF+8)` 喂零把流水线排空，最后 `$finish`。`WOI+WOF+8` 的排空长度是为了让最后几个输入的结果也能流过 `fxp_div_pipe`（它有 \(WOI+WOF+3\) 级，比乘法深得多）。

#### 4.4.4 代码实践

**实践目标：** 跑通官方 testbench，**目视确认 2 拍延迟**（HW-result 比 SW-result 滞后 2 行）与无气泡流式激励。

**操作步骤：**

1. 在 `SIM/` 目录下运行官方脚本（Linux 下把 `.bat` 里的命令直接执行即可）：

```bash
cd SIM
iverilog -g2001 -o sim.out tb_fxp_mul_div_pipe.v ../RTL/fixedpoint.v && vvp -n sim.out | head -40
```

2. 在输出的若干行里，锁定某一行 `N` 的 SW-result 值（由该行 `a`、`b` 算出），然后往下数到第 `N+2` 行，看该行的 HW-result（`omul` 列）是否等于行 `N` 的 SW-result。

**需要观察的现象：** 由于流水线 2 拍延迟，`omul`（HW-result）列整体比 `a*b`（SW-result）列**滞后 2 行**——第 `N` 行的输入乘积，出现在第 `N+2` 行的 `omul` 里。同时每行都有不同的 `a`、`b`，说明每拍都在喂新输入（无气泡）。

**预期结果：** 在打印文本中能找到「行 N 的 SW-result == 行 N+2 的 HW-result」的对应关系，从而目视确认 latency=2。具体数值「待本地验证」，但滞后行数应为固定的 2。

#### 4.4.5 小练习与答案

**练习 1：** 为什么不能直接拿「当前输入的软件乘积」和「当前的 `omul`」做相等比较？

> **答案：** 因为 `omul` 是 2 级流水线输出，滞后输入 2 拍。当前 `omul` 对应的是 2 拍前写入的输入，而「当前输入的软件乘积」对应的是当前输入——两者错位 2 拍，直接比较必然不等。必须把软件参考也延迟 2 拍（或把硬件输出回溯到 2 拍前的输入）才能对齐。

**练习 2：** 官方 testbench 末尾为什么要 `repeat(WOI+WOF+8) test(0,0)` 再 `$finish`？

> **答案：** 为了把流水线里**还在飞行中**的数据冲刷出来。最后一个有效输入写入后，它的结果要等若干拍才到达输出；如果不排空就直接 `$finish`，尾部几个结果会被截断看不到。长度取 `WOI+WOF+8` 是为了同时覆盖最深的 `fxp_div_pipe`（\(WOI+WOF+3\) 级），保证两个 DUT 的尾部结果都能流尽。对 `fxp_mul_pipe` 而言其实只需排空 2 拍即可。

---

## 5. 综合实践

把本讲的「接口约定 + 两级划分 + 延迟对齐 + 无气泡」四条主线串成一个**自校验流式 testbench**。核心思路见 4.4.2：用单周期 `fxp_mul` 作黄金参考，其输出经 2 级延迟线对齐到 `fxp_mul_pipe` 的输出，逐拍比对并统计通过/失败计数。

**实践目标：** 每拍给 `fxp_mul_pipe` 喂一个新随机输入，用软件（黄金 `fxp_mul`）记录历史，2 拍后比对，统计正确样本数，验证「2 级延迟对齐 + 无气泡吞吐」。

**操作步骤：**

1. 在 `SIM/` 下新建学习用 testbench（**示例代码**，请勿修改 `RTL/fixedpoint.v` 或已有 testbench），内容如下：

```verilog
// ============ 示例代码：自校验流式 testbench（读者自行新建文件，如 tb_mul_pipe_stream.v） ============
`timescale 1ps/1ps
module tb_mul_pipe_stream ();
// 配置与官方 tb_fxp_mul_div_pipe.v 一致
localparam WIIA=12, WIFA=20, WIIB=14, WIFB=18, WOI=24, WOF=17;
localparam LATENCY = 2;                      // fxp_mul_pipe 流水线级数

reg rstn = 1'b0;
reg clk  = 1'b1;
always #(10000) clk = ~clk;                  // 50MHz
initial begin repeat(4) @(posedge clk); rstn<=1'b1; end

reg  [WIIA+WIFA-1:0] ina = 0;
reg  [WIIB+WIFB-1:0] inb = 0;

// ---- 待测：2 级流水线乘法 ----
wire [WOI+WOF-1:0] out_pipe;
wire               ov_pipe;
fxp_mul_pipe #(.WIIA(WIIA),.WIFA(WIFA),.WIIB(WIIB),.WIFB(WIFB),
               .WOI(WOI),.WOF(WOF),.ROUND(1)) dut_pipe (
    .rstn(rstn),.clk(clk),.ina(ina),.inb(inb),.out(out_pipe),.overflow(ov_pipe));

// ---- 黄金参考：单周期 fxp_mul（组合逻辑，软件等价）----
wire [WOI+WOF-1:0] out_gold;
wire               ov_gold;
fxp_mul #(.WIIA(WIIA),.WIFA(WIFA),.WIIB(WIIB),.WIFB(WIFB),
          .WOI(WOI),.WOF(WOF),.ROUND(1)) dut_gold (
    .ina(ina),.inb(inb),.out(out_gold),.overflow(ov_gold));

// ---- 黄金输出延迟 LATENCY 拍，与流水线输出对齐 ----
reg [WOI+WOF-1:0] gold_d0, gold_d1;
reg               gov_d0,  gov_d1;
always @(posedge clk) begin
    gold_d0 <= out_gold; gov_d0 <= ov_gold;
    gold_d1 <= gold_d0;  gov_d1  <= gov_d0;
end

integer pass=0, fail=0, total=0, cyc=0;

// ---- 连续流式激励：每个时钟都换新随机输入（无气泡）----
always @(posedge clk)
    if(rstn) begin ina <= $random; inb <= $random; end

// ---- 对齐采样：流水线打满后逐拍比对 ----
always @(posedge clk) if(rstn) begin
    cyc <= cyc + 1;
    if(cyc >= LATENCY+2) begin                 // 跳过前几拍（延迟线尚未填满）
        total <= total + 1;
        if(out_pipe === gold_d1 && ov_pipe === gov_d1) pass <= pass + 1;
        else begin fail <= fail + 1;
            $display("MISMATCH @cyc=%0d  pipe=%h_%b  gold=%h_%b", cyc, out_pipe, ov_pipe, gold_d1, gov_d1);
        end
    end
end

initial begin
    #3000000;                                  // 跑足够长时间（远大于 LATENCY）
    $display("==== RESULT: total=%0d pass=%0d fail=%0d ====", total, pass, fail);
    if(fail==0) $display("==== PASS: fxp_mul_pipe 与 fxp_mul 功能等价, 2 拍延迟对齐, 无气泡 ====");
    $finish;
end
endmodule
```

2. 编译运行（testbench 与 RTL 同时参与编译）：

```bash
cd SIM
iverilog -g2001 -o sim.out tb_mul_pipe_stream.v ../RTL/fixedpoint.v && vvp -n sim.out
```

**需要观察的现象：**

- `total` 是一个很大的数（几万到十几万，取决于 `#3000000` 的仿真时长与 20ns 周期），`pass` 应**等于** `total`、`fail` 应为 **0**（或仅有个位数由前几拍边界引起，可适当增大 `LATENCY+2` 的阈值消除）。
- 由于每拍都喂新输入且每拍都有一次有效比较，`total` 之大本身就证明了「无气泡」——吞吐量达到每拍一个结果。
- 若把黄金延迟线从 2 级改成 1 级或 3 级（即 `gold_d1` 改用 `gold_d0` 或再加一级），`fail` 会**暴增**——这反向证明了延迟必须是恰好 2 拍。

**预期结果：** `fail==0` 且 `pass==total`，打印 `PASS` 行。若出现大量 `MISMATCH`，先检查延迟线深度是否为 2、两个 DUT 的参数是否完全一致。具体 `total` 数值「待本地验证」，但 `fail` 应为 0。

**完成标志：** 你能不查源码说出「`fxp_mul_pipe` 比 `fxp_mul` 多了什么、延迟几拍、为什么无气泡」，并能解释「为什么黄金输出要延迟 2 拍才能与流水线输出对齐」。

## 6. 本讲小结

- **动机**：单周期 `fxp_mul` 的关键路径是「乘法器 → fxp_zoom」一整条组合链，\(f_{\max}\) 受限；流水线在段间插寄存器切断长路径，用固定延迟换更高频率，吞吐量仍为每拍一个（无气泡）。
- **统一接口约定**：流水线模块比单周期版多 `rstn`/`clk`、`out`/`overflow` 改 `reg`、`initial` 初始化输出、`always @(posedge clk or negedge rstn)` 异步复位同步加载、时序块用 `<=`——这套约定适用于全库所有 `_pipe` 模块。
- **两级划分**：`fxp_mul_pipe` 第 1 级把 `$signed(ina)*$signed(inb)` 锁进 `res`，中间 `fxp_zoom`（**一字未改**）算出组合 `outc`，第 2 级把 `outc` 再锁一拍；`fxp_zoom` 夹在两段寄存器之间。
- **功能等价**：流水线版与单周期版数学内核完全相同（`WRI/WRF`、`fxp_zoom` 例化参数逐字一致），只是输出引入固定 **2 拍延迟**。
- **验证方法学**：官方 testbench 是「目视对比」风格（HW-result 滞后 SW-result 2 行）；自校验做法是用单周期 `fxp_mul` 作黄金参考，其输出延迟 2 拍后与 `fxp_mul_pipe` 逐拍比对、统计通过/失败计数，一步验证「2 拍延迟对齐 + 无气泡吞吐」。
- **读寄存器的时机**：在 `posedge` 处读到的是上一拍锁存的值（NBA 语义），这是延迟对齐推演的基础，也是为什么黄金延迟线深度取 2 即可与流水线输出对齐。

## 7. 下一步学习建议

- 下一篇 [u3-l2（fxp_div_pipe）](./u3-l2-div-pipe.md) 会把本讲建立的流水线套路用在一个**远比乘法复杂**的对象上：把单周期 `fxp_div` 里 `WOI+WOF` 次迭代的 `for` 循环展开成 \(WOI+WOF+3\) 级流水线，用数组 `acc/res/divdp/divrp/sign` 作级间寄存器。建议先读 [RTL/fixedpoint.v:505-673](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L505-L673) 的 `fxp_div_pipe`，对照本讲的「2 级是最简形态」体会「把循环每一位迭代映射成一拍」的展开手法。
- 若想继续横向对比逐位算法的流水线化，可看 [u3-l3（fxp_sqrt_pipe）](./u3-l3-sqrt-pipe.md)：逐位开方展开为 \(\lfloor WII/2\rfloor+WIF+2\) 级，级间寄存器数组 `sign/inu/resu/resu2` 的用法与 `fxp_div_pipe` 如出一辙。
- 关于本讲用到的 testbench 技巧（`$signed*1.0/(1<<W)` 还原法、`iverilog -g2001` 编译、`$dumpvars` 波形、pass/fail 自校验），[u3-l6（仿真验证方法学）](./u3-l6-simulation-testbench.md) 会做系统性总结，并把单周期 testbench 与流水线 testbench 两种风格对照讲透。
- 阅读建议：把 `fxp_mul`（[RTL/fixedpoint.v:278-310](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L278-L310)）与 `fxp_mul_pipe`（[RTL/fixedpoint.v:326-380](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L326-L380)）并排打开，是体会「组合→流水线」最小改造范本最直接的方式——两段代码的 diff 恰好就是本讲 4.2 那张对照表。
