# fxp_div_pipe：把除法循环展开为流水线

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚**为什么 `fxp_div` 必须流水线化**：它的恢复余数主循环是 `WOI+WOF` 次迭代的纯组合串联，关键路径极长（README 也标注「单周期版时序不易收敛」），工程中应改用 `fxp_div_pipe`。
- 掌握本篇的核心新手法——**把 `for` 循环的每一次迭代映射成一级流水线**：用大小为 `WOI+WOF+1` 的数组 `sign/acc/divdp/divrp/res` 当作级间寄存器，下标就是流水线级号。
- 看懂主循环 [`RTL/fixedpoint.v:605-634`](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L605-L634) 里 `res[ii+1][WOF+WOI-1-ii] <= 1'b1` 的**逐位写商**与 `acc[ii+1] <= tmp` 的**级联累加余数**，并能把循环下标 `ii` 与单周期版的移位量 `shamt` 一一对应起来。
- 理解**独立的舍入级**（`roundedres`）与**最后的符号恢复/溢出饱和输出级**，从而推出总流水线级数 \(L = WOI+WOF+3\) 的构成（首位寄存 + 逐位求商 + 舍入 + 输出）。
- 能构造一个**延迟对齐为 \(WOI+WOF+3\) 拍**的自校验验证环境，用单周期 `fxp_div` 作黄金参考、逐拍比对 `fxp_div_pipe` 的 `out/overflow`，确认两者功能等价。

## 2. 前置知识

本讲是专家层第二篇，同时承接 [u2-l3（fxp_div 恢复余数法）](./u2-l3-div.md) 与 [u3-l1（流水线设计模式与 fxp_mul_pipe）](./u3-l1-mul-pipe.md)。你需要已经掌握：

- `fxp_div` 的四步流程：① 把被除数/除数取绝对值并记下 `sign`；② 用两个 `.ROUND(0)` 的 `fxp_zoom` 把操作数对齐到统一工作格式 `(WRI,WRF)`；③ 用恢复余数法主循环 `for(shamt=WOI-1; shamt>=-WOF; ...)` 逐位试探商；④ 做 round-to-nearest 舍入、按 `sign` 补回二进制补码符号、做上/下溢出饱和。
- 工作位宽的定义：`WRI = max(WOI+WIIB, WIIA)`、`WRF = max(WOF+WIFB, WIFA)`（[RTL/fixedpoint.v:414-415](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L414-L415)），取足够宽是为了移位试探时不丢精度。
- u3-l1 建立的**流水线统一约定**：流水线模块比单周期版多 `rstn/clk`、`out/overflow` 改 `reg`、用 `initial` 初始化输出、用 `always @(posedge clk or negedge rstn)` 异步复位同步加载、时序块用 `<=` 非阻塞赋值。
- 「读寄存器的时机」：在 `posedge` 处读到的是上一拍锁存的值（NBA 语义），这是延迟对齐推演的基础。

本讲要回答的新问题是：**`fxp_div` 里那个 `WOI+WOF` 次迭代的 `for` 循环是纯组合的，关键路径长到时序几乎无法收敛——怎么把它变成流水线？「用数组当级间寄存器」到底是一种什么套路？流水线级数又为什么恰好是 `WOI+WOF+3`？**

> **一句话直觉：** 单周期除法像「一个工人从最高位到最低位依次试商，一口气把 `WOI+WOF` 位全部试完才交货」——组合路径就是这 `WOI+WOF` 级串联；流水线化后变成「`WOI+WOF` 个工人排成一队，每个工人每拍只试一位商，然后把手里的半成品（部分商 + 余数 + 原始除数/被除数）递给下一个工人」——每位迭代变成一级，深度就是 `WOI+WOF`，再在首尾各补一级寄存，总级数便是 `WOI+WOF+3`。

## 3. 本讲源码地图

| 文件 | 作用 |
| :--- | :--- |
| [RTL/fixedpoint.v](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v) | 全部可综合模块所在。本讲主角是 `fxp_div_pipe`（[第 505–673 行](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L505-L673)），并把它与单周期基线 `fxp_div`（[第 388–497 行](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L388-L497)）逐段对照；被两版共用的组合前端 `fxp_zoom`（[第 22–94 行](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L22-L94)）已在 u1-l3 讲透，本讲只复用其结论。 |
| [SIM/tb_fxp_mul_div_pipe.v](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_fxp_mul_div_pipe.v) | 流水线乘除法 testbench：例化 `fxp_div_pipe`，逐拍 `$display` 并排打印软件 `a/b` 与硬件 `odiv`。本讲剖析其时钟/复位/激励约定，并指出它**只做目视对比、未做严格延迟对齐**——综合实践中我们要补一个自校验版本（见第 5 节）。 |
| [SIM/tb_fxp_mul_div_pipe_run_iverilog.bat](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_fxp_mul_div_pipe_run_iverilog.bat) | 一键编译运行：`iverilog -g2001 -o sim.out tb_fxp_mul_div_pipe.v ../RTL/fixedpoint.v && vvp -n sim.out`（testbench 与 RTL 必须同时参与编译）。 |

## 4. 核心概念与源码讲解

### 4.1 动机与级数推导：为什么是 WOI+WOF+3 级

#### 4.1.1 概念说明

`fxp_div` 是**纯组合逻辑**，它最致命的环节是主循环（[RTL/fixedpoint.v:459-471](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L459-L471)）：

```verilog
for(shamt=WOI-1; shamt>=-WOF; shamt=shamt-1) begin
    if(shamt>=0) acct = acc + (divr<<shamt);
    else         acct = acc + (divr>>(-shamt));
    if( acct <= divd ) begin acc = acct; out[WOF+shamt] = 1'b1; end
    else                   out[WOF+shamt] = 1'b0;
end
```

这个 `for` 循环在仿真里是「时间循环」，但在综合后会被**展开**成 `WOI+WOF` 级串联组合逻辑：每一级都要做一次「宽位加法 `acc+(divr<<shamt)` + 宽位比较 `acct<=divd` + 多路选择」，并且后一级的 `acc` 依赖前一级的 `acc`——于是 `WOI+WOF` 级首尾相连，构成一条极长的组合路径。再前面还串着取绝对值与 `fxp_zoom`、后面还串着舍入与溢出多路选择，整条关键路径非常长，\(f_{\max}\) 很难做上去。这正是 README 模块表里给除法标注「单周期版时序不易收敛」、并为其提供流水线版本的原因（[README.md:178](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/README.md#L178)）。

**流水线化的天然切入点**在于：循环的每一次迭代只依赖上一次迭代的 `acc`（和始终不变的 `divr`/`divd`），迭代之间是严格的线性依赖。这意味着可以把「第 `ii` 次迭代」原封不动地搬成「第 `ii+2` 级流水线寄存器」——每次迭代之间插一个寄存器即可，算法一字不改。

#### 4.1.2 核心流程

把 `fxp_div` 的执行过程沿「时间轴」切片，每一片对应一级流水线：

```
组合前端          第1级      第2..WOI+WOF+1级(逐位求商)     舍入级        输出级
|div|,|divr|  →  寄存divd   ii=0 试最高位商            round-to-nearest  补符号+
fxp_zoom对齐     寄存divr   ii=1 ...                  (roundedres)     溢出饱和
                 寄存sign   ii=WOI+WOF-1 试最低位商                    (out/overflow)
                            ↑每级都用数组下标 [ii]↔[ii+1] 做级间传递
```

逐级数下去，总流水线级数为：

\[
L = \underbrace{1}_{\text{首位输入寄存}} + \underbrace{(WOI+WOF)}_{\text{逐位求商，每位一级}} + \underbrace{1}_{\text{舍入级}} + \underbrace{1}_{\text{符号/溢出输出级}} = WOI+WOF+3
\]

这就是模块注释 `pipeline stage = WOI+WOF+3`（[RTL/fixedpoint.v:510](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L510)）的由来。输出延迟 \(L\) 拍，但每个时钟都能吞入一个新输入、吐出一个旧结果，**吞吐量仍为每拍一个（无气泡）**——这与 u3-l1 讲的流水线收益完全一致，只是这里级数从 2 变成了 \(WOI+WOF+3\)。

#### 4.1.3 源码精读

模块注释明确写出级数：

[RTL/fixedpoint.v:505-511](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L505-L511) —— `fxp_div_pipe` 的注释块，`Function: division`、`pipeline stage = WOI+WOF+3`。与单周期版 `fxp_div`（[第 388–395 行](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L388-L395)）的注释对照：后者多了 `not recommended due to the long critical path`，前者多了 `pipeline stage = WOI+WOF+3`——一句话点明了「为什么要有流水线版」与「流水线有多深」。

要体会这条组合路径有多长，回头看单周期 `fxp_div` 的主循环：

[RTL/fixedpoint.v:459-471](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L459-L471) —— `always @(*)` 里的 `for(shamt=...)` 循环，`acc` 在循环体里被反复更新（阻塞赋值 `=`），综合后是 `WOI+WOF` 级首尾相连的组合链。`fxp_div_pipe` 要做的，就是在这条链的**每一级之间都插一个寄存器**。

#### 4.1.4 代码实践

**实践目标：** 给定配置手算流水线级数与绝对延迟，建立对「除法流水线很深」的量级直觉。

**操作步骤：**

1. 打开 [SIM/tb_fxp_mul_div_pipe.v:16-21](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_fxp_mul_div_pipe.v#L16-L21)，记下官方配置 `WOI=24, WOF=17`，套用 \(L=WOI+WOF+3\) 算出 `L = 24+17+3 = 44` 级。
2. 由 [SIM/tb_fxp_mul_div_pipe.v:26](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_fxp_mul_div_pipe.v#L26) 的 `always #(10000) clk=~clk` 得周期 20ns（50MHz），算出单个结果从输入到输出的绝对延迟约 \(44 \times 20\text{ns} = 880\text{ns}\)。
3. 对照：若改成 `WOI=8,WOF=8` 的小配置，则 \(L=8+8+3=19\) 级。

**需要观察的现象：** 除法的流水线深度 \(WOI+WOF+3\) 远大于乘法的 2 级——位宽越宽，级数线性增长。这也是为什么官方 testbench 末尾要用 `repeat(WOI+WOF+8)`（[SIM/tb_fxp_mul_div_pipe.v:152](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_fxp_mul_div_pipe.v#L152)）来排空流水线。

**预期结果：** 官方配置下 `L=44`、单结果延迟约 880ns；但吞吐量仍是每 20ns 一个结果（无气泡）。具体仿真时间「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1：** 为什么除法的流水线级数与 `WOI+WOF` 线性相关，而乘法 `fxp_mul_pipe` 只有固定的 2 级？

> **答案：** 乘法用 FPGA 硬件乘法器一步算完全精度积，组合路径只有「乘法器 → fxp_zoom」两段，故 2 级即可。除法没有硬件除法器，靠「逐位试商」的迭代算法，试商位数 = `WOI+WOF`，每一位迭代之间是线性依赖，必须展开成 `WOI+WOF` 级，再加上首尾寄存与舍入/输出级，故与 `WOI+WOF` 线性相关。

**练习 2：** 一个 44 级流水线连续处理 10000 个除法，总耗时大约多少拍？平均吞吐率？

> **答案：** 总耗时约 \(10000+44=10044\) 拍；平均吞吐率 \(10000/10044\approx 0.9956\)，非常接近 1。数据量越大，44 拍启动延迟的相对开销越小——这就是「无气泡」的体现。

---

### 4.2 共用的组合前端：取绝对值 + fxp_zoom 对齐（与 fxp_div 一字不差）

#### 4.2.1 概念说明

`fxp_div_pipe` 的「数学内核」前端——**取绝对值、记符号、用两个 `fxp_zoom` 把操作数对齐到工作格式 `(WRI,WRF)`**——与单周期 `fxp_div` 完全相同。区别只在于实现风格：`fxp_div` 用 `always @(*)` 里的 `reg`（[第 427–431 行](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L427-L431)），而 `fxp_div_pipe` 把它改成 `wire` 连续赋值 + 组合 `fxp_zoom`，放在**第 1 级寄存器之前**作为组合前置。

为什么要这样安排？因为这部分逻辑（一次取反 + 一次 `fxp_zoom` 对齐）相对轻，不足以单独占一级流水线；把它放在第 1 级寄存器之前，让第 1 级寄存器去「吸收」这段组合延迟，是更经济的划分。这一手法与 `fxp_mul_pipe` 把乘法器放在第 1 级寄存器之前是同一个思路。

#### 4.2.2 核心流程

组合前端的三件事（与 `fxp_div` 完全一致）：

1. `udividend = |dividend|`、`udivisor = |divisor|`（负数取补码 `(~x)+1`）。
2. 两个 `.ROUND(0)` 的 `fxp_zoom` 把 `udividend/udivisor` 从各自输入格式对齐到统一工作格式 `(WRI,WRF)`，得到 `divd/divr`。`.ROUND(0)` 表示**只对齐不舍入**——因为工作位宽比输入宽（`WRI>=WIIA`、`WRF>=WIFA`），是小数位扩展/整数位扩展，本就精确，无需舍入。
3. `sign = dividend 符号位 ^ divisor 符号位`，把有符号除法归约为无符号除法，最后再按 `sign` 补回符号。

#### 4.2.3 源码精读

工作位宽与单周期版逐字相同：

[RTL/fixedpoint.v:532-533](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L532-L533) —— `localparam WRI = WOI+WIIB > WIIA ? WOI+WIIB : WIIA;` 与 `WRF = WOF+WIFB > WIFA ? WOF+WIFB : WIFA;`，与 `fxp_div` 的 [第 414–415 行](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L414-L415) 完全一致——说明工作位宽的推导在两版里是同一套。

取绝对值用 `wire` 连续赋值：

[RTL/fixedpoint.v:559-561](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L559-L561) —— `wire [WIIA+WIFA-1:0] udividend = dividend[WIIA+WIFA-1] ? (~dividend)+ONEA : dividend;` 以及 `udivisor`。注意它与 `fxp_div` 的 [第 427–431 行](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L427-L431) 表达式**完全相同**，只是从 `always @(*)` 里的 `reg =` 换成了 `wire =`。这里的 `sign` 没有立刻算成单独的 `wire`，而是推迟到第 1 级寄存器里与 `divd/divr` 一起锁存（见 4.3）。

两个对齐用的 `fxp_zoom`：

[RTL/fixedpoint.v:563-585](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L563-L585) —— `dividend_zoom` 与 `divisor_zoom`，参数 `(WIIA,WIFA)→(WRI,WRF)` 与 `(WIIB,WIFB)→(WRI,WRF)`，`.ROUND(0)`，与 `fxp_div` 的 [第 433–455 行](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L433-L455) 逐字相同。它们是纯组合的 `fxp_zoom`（u1-l3 讲过的 `always @(*)`），输出 `divd/divr` 两个 `wire`，作为第 1 级寄存器的数据源。

把两版的「组合前端」并排看，结论很清晰：**算法一字未改，只是 `always@(*) reg` 换成了 `wire` + 组合 `fxp_zoom`，整体被推到第 1 级寄存器之前。**

#### 4.2.4 代码实践

**实践目标：** 用「找相同」确认前端算法在两版里完全一致，强化「流水线改造不动算法」的认识。

**操作步骤：**

1. 并排打开 `fxp_div` 的 [第 414–455 行](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L414-L455)（工作位宽 + 取绝对值 + 两个 `fxp_zoom`）和 `fxp_div_pipe` 的 [第 532–585 行](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L532-L585)。
2. 逐项核对：`WRI/WRF` 表达式、`udividend/udivisor` 表达式、两个 `fxp_zoom` 的参数与端口连接——确认全部相同。

**需要观察的现象：** 唯一的差别是 `fxp_div` 把取绝对值写在 `always @(*)` 里（`reg` + `=`），`fxp_div_pipe` 写成 `wire` 连续赋值；两个 `fxp_zoom` 的例化则**完全相同**。

**预期结果：** 能口述「`fxp_div_pipe` 的组合前端就是把 `fxp_div` 的 `always@(*)` 取绝对值挪成 `wire`，两个 `fxp_zoom` 原样照搬」。

#### 4.2.5 小练习与答案

**练习 1：** 为什么前端这两个 `fxp_zoom` 都传 `.ROUND(0)`？

> **答案：** 因为工作位宽 `WRI>=WIIA`、`WRF>=WIFA`（同理对 B 路），是把操作数往更宽的格式搬，属于整数位/小数位**扩展**（补零或符号扩展），数值完全精确、没有截断，自然不需要舍入。`.ROUND(0)` 关掉舍入既省逻辑也避免误改数值。这与 `fxp_add` 输入侧的 `.ROUND(0)` 是同一个道理（见 u2-l1）。

**练习 2：** 组合前端被放在第 1 级寄存器**之前**，会不会让第 1 级的关键路径过长？

> **答案：** 会有一定影响（第 1 级关键路径 = 取反 + `fxp_zoom` 对齐 + 寄存器 setup），但远小于单周期版整条路径。因为 `fxp_zoom` 在扩展模式下（`WOI>=WII` 且 `WOF` 变宽）逻辑很轻（基本是补零与符号扩展，见 u1-l3 的 `generate` 分支），所以把这段组合前置是经济的划分。若时序仍紧张，也可再插一级寄存器把前端单独隔离，但本库选择了不过度切分。

---

### 4.3 级间寄存器数组与第 1 级：流水线的「骨架」

#### 4.3.1 概念说明

这是 `fxp_div_pipe` 区别于 `fxp_mul_pipe` 的**核心新手法：用数组当级间寄存器**。`fxp_mul_pipe` 只有 2 级，用单个 `reg`（`res`、`out`）就够了；而 `fxp_div_pipe` 有 \(WOI+WOF+3\) 级，逐级声明 `reg` 既繁琐又不通用，于是声明**大小为 `WOI+WOF+1` 的数组，下标就是流水线级号**——每个数组元素就是一级寄存器。

一共用到 5 个数组（[RTL/fixedpoint.v:538-542](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L538-L542)）：

| 数组 | 元素位宽 | 数组大小 | 携带的「半成品」 |
| :--- | :--- | :--- | :--- |
| `sign` | 1 bit | `[WOI+WOF:0]` | 结果符号（一路传到末级） |
| `acc` | `WRI+WRF` | `[WOI+WOF:0]` | 累加余数（每级更新） |
| `divdp` | `WRI+WRF` | `[WOI+WOF:0]` | 被除数流水（一路传，恒不变） |
| `divrp` | `WRI+WRF` | `[WOI+WOF:0]` | 除数流水（一路传，恒不变） |
| `res` | `WOI+WOF` | `[WOI+WOF:0]` | 部分商（每级多写一位） |

> 注意大小是 `WOI+WOF+1`（下标 `0..WOI+WOF`），而不是 `WOI+WOF+3`。因为数组只覆盖「第 1 级寄存 + 逐位求商」这 \(1+(WOI+WOF)\) 级；最后的**舍入级**与**输出级**用单独的标量 `reg`（`roundedres/rsign` 与 `out/overflow`）实现，不进数组。

这些数组都要满足两条：① 用 `initial` 给所有元素赋初值（避免仿真出现 `x`）；② 在 `always` 块里既支持复位清零、又支持每拍「下标 +1」级联移位。

#### 4.3.2 核心流程

第 1 级把组合前端的产物锁进下标 `[0]`：

```
组合前端:  divd, divr  (wire)           sign (现算)
              ↓ 第1级 @(posedge clk)
divdp[0] <= divd ;  divrp[0] <= divr ;  acc[0] <= 0 ;  res[0] <= 0 ;  sign[0] <= dividend符 ^ divisor符
```

后续每一级（下标 `ii → ii+1`）做两件事：① 把「不变的随路数据」（`divdp/divrp/sign`）原样下传；② 用上一级的 `acc[ii]/divrp[ii]/divdp[ii]` 算出本位的商，写进 `res[ii+1]` 的对应位，并更新 `acc[ii+1]`。这正是下一节 4.4 的主循环。

#### 4.3.3 源码精读

数组声明：

[RTL/fixedpoint.v:535-543](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L535-L543) —— `wire divd,divr` 是组合前端的输出；`roundedres/rsign` 是舍入级寄存器；`sign/acc/divdp/divrp` 是大小 `[WOI+WOF:0]` 的级间数组；`res` 是同大小的部分商数组；`ONEO` 是末级补码取反用的 1。这一段就是整条流水线的「存储骨架」。

`initial` 初始化所有数组元素：

[RTL/fixedpoint.v:547-554](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L547-L554) —— `initial for(ii=0; ii<=WOI+WOF; ii=ii+1) begin res[ii]=0; divrp[ii]=0; ... end`。这是 u3-l1 讲过的「`initial` 给上电初值」约定，但这里要遍历整个数组——否则仿真开头数组里全是 `x`，会沿着流水线一路传播，导致前若干拍输出全是 `x`。

第 1 级寄存器：

[RTL/fixedpoint.v:587-601](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L587-L601) —— `always @(posedge clk or negedge rstn)`：`~rstn` 时把 `res[0]/acc[0]/divdp[0]/divrp[0]/sign[0]` 全清零（复位）；否则 `divdp[0]<=divd; divrp[0]<=divr; acc[0]<=0; res[0]<=0; sign[0] <= dividend[WIIA+WIFA-1] ^ divisor[WIIB+WIFB-1];`。注意 `sign` 在这里才计算并锁存——它直接用**原始输入** `dividend/divisor` 的符号位异或，而不是用取绝对值后的值。这一级把组合前端的结果「冻结」进下标 `[0]`，从此数据进入逐位求商的流水线。

#### 4.3.4 代码实践

**实践目标：** 用层次名探针亲眼看见数据每拍在数组下标里「下移一级」。

**操作步骤：**

1. 复制 [SIM/tb_fxp_mul_div_pipe.v](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/SIM/tb_fxp_mul_div_pipe.v) 为学习用 testbench（**示例代码**，勿改原文件），在逐拍 `$display` 里追加一行层次名探针：

```verilog
// ===== 示例代码：在 tb 的 display always 块里追加（读者自行新建文件） =====
// $display("  [probe] divdp[0]=%h divdp[1]=%h divdp[2]=%h",
//          fxp_div_pipe_i.divdp[0], fxp_div_pipe_i.divdp[1], fxp_div_pipe_i.divdp[2]);
```

2. 跑仿真：`cd SIM && iverilog -g2001 -o sim.out tb_study.v ../RTL/fixedpoint.v && vvp -n sim.out`。
3. 锁定某组确定的输入，观察 `divdp[0]` 出现该被除数的那一拍，再数 `divdp[1]`、`divdp[2]` 各滞后几拍出现同一个值。

**需要观察的现象：** 同一个 `divd` 值依次出现在 `divdp[0]`、`divdp[1]`、`divdp[2]`……每级正好滞后 1 拍——这正是「数组下标 = 流水线级号」的直观体现，`divdp` 像一条移位寄存器在逐级下传不变的被除数。

**预期结果：** `divdp[k]` 比 `divdp[k+1]` 提前 1 拍反映同一个被除数。具体探针数值「待本地验证」，但相邻下标的节拍差恒为 1。

#### 4.3.5 小练习与答案

**练习 1：** 为什么级间数组的大小是 `WOI+WOF+1`，而总流水线级数却是 `WOI+WOF+3`？

> **答案：** 数组覆盖的是「第 1 级寄存 + 逐位求商」共 \(1+(WOI+WOF)\) 级（下标 `0..WOI+WOF`，共 `WOI+WOF+1` 个元素）。最后的舍入级与输出级没有用数组元素，而是用单独的标量寄存器 `roundedres/rsign`（舍入级）和 `out/overflow`（输出级）。所以数组大小 `WOI+WOF+1` 加上这两个标量级，总级数才是 `WOI+WOF+3`。

**练习 2：** 如果删掉 [第 547–554 行](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L547-L554) 的 `initial` 初始化，仿真会出现什么现象？

> **答案：** 仿真 \(t=0\) 时数组里全是 `x`。由于 `rstn` 复位只在开头持续 4 拍（见 testbench），复位期间会把数组清零；但 `initial` 还有一个作用是给「复位也覆盖不到的中间寄存器」兜底初值。更重要的是，标量寄存器 `roundedres/rsign/out/overflow` 依赖 `initial`（[第 530 行](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L530)）才不在开头出 `x`。删掉后，流水线打满前的若干拍输出会是 `x`，影响 testbench 头部的目视对比。

---

### 4.4 逐位主循环的流水线展开：ii ↔ shamt 一一映射

#### 4.4.1 概念说明（本讲核心）

这是本篇最关键的一节。`fxp_div` 的主循环 `for(shamt=WOI-1; shamt>=-WOF; ...)` 共 `WOI+WOF` 次迭代，每次：算候选 `acct = acc + (divr<<shamt 或 divr>>(-shamt))`；若 `acct <= divd` 则该位置 1 且 `acc=acct`，否则位置 0 且 `acc` 不变（恢复）。`fxp_div_pipe` 把这 `WOI+WOF` 次迭代展开成 `WOI+WOF` 级流水线，**每级处理一位商**。

映射关系是本节的灵魂：单周期版的循环下标 `shamt`（从 `WOI-1` 递减到 `-WOF`）与流水线版的数组下标 `ii`（从 `0` 递增到 `WOI+WOF-1`）一一对应，关系为：

\[
\text{shamt} = WOI-1-ii
\]

即 `ii=0` 对应最高位（`shamt=WOI-1`），`ii=WOI+WOF-1` 对应最低位（`shamt=-WOF`）。

#### 4.4.2 核心流程

把两版的逐位逻辑逐项对齐（这是本讲最重要的对照表）：

| 维度 | `fxp_div`（单周期，`shamt` 递减） | `fxp_div_pipe`（流水线，`ii` 递增） | 对应关系 |
| :--- | :--- | :--- | :--- |
| 循环范围 | `shamt = WOI-1 .. -WOF` | `ii = 0 .. WOI+WOF-1` | \(\text{shamt}=WOI-1-ii\) |
| 移位方向判别 | `shamt>=0` 左移 / 否则右移 | `ii<WOI` 左移 / 否则右移 | `shamt>=0 \Leftrightarrow ii<WOI` |
| 候选累加 | `acct = acc + (divr<<shamt)` 或 `>>(-shamt)` | `tmp = acc[ii] + (divrp[ii]<<(WOI-1-ii))` 或 `>>(1+ii-WOI)` | `WOI-1-ii=shamt`，`1+ii-WOI=-shamt` |
| 写商的位 | `out[WOF+shamt]` | `res[ii+1][WOF+WOI-1-ii]` | 同一下标 \(WOF+WOI-1-ii=WOF+\text{shamt}\) |
| 置位判定 | `acct <= divd` | `tmp < divdp[ii]` | **见 4.4.4 的深入思考** |
| 余数更新 | `acc = acct`（置位）/ 不变（恢复） | `acc[ii+1] <= tmp` / `<= acc[ii]` | 算法相同 |

关键技巧：`fxp_div_pipe` 在一个 `always @(posedge clk)` 里用 `for(ii=0; ii<WOI+WOF; ii=ii+1)` 看起来像「时间循环」，但**综合时它是空间展开**——因为循环体里每一级都用非阻塞 `<=` 把结果写到下标 `[ii+1]`、读下标 `[ii]`，综合器会把它展开成 `WOI+WOF` 级级联寄存器（一位商一级）。仿真里它也是「一次性描述了所有级」，每拍所有级同时推进。

#### 4.4.3 源码精读

主循环 always 块：

[RTL/fixedpoint.v:605-634](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L605-L634) —— 注释 `from 2nd to WOI+WOF+1 pipeline stages: calculate division`。复位分支（[第 607–614 行](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L607-L614)）用 `for` 把所有级 `res/divrp/divdp/acc/sign` 清零；正常分支（[第 615–633 行](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L615-L633)）的 `for(ii=0; ii<WOI+WOF; ii=ii+1)` 展开成 `WOI+WOF` 级。逐句解读循环体：

- [第 618–621 行](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L618-L621)：`res[ii+1]<=res[ii]; divdp[ii+1]<=divdp[ii]; divrp[ii+1]<=divrp[ii]; sign[ii+1]<=sign[ii];`——把「不变的随路数据」原样下传一级。`divdp/divrp` 在整个逐位过程中恒不变，只是逐级跟着走；`sign` 也一路传到末级。
- [第 622–625 行](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L622-L625)：算候选 `tmp`。`if(ii<WOI) tmp = acc[ii] + (divrp[ii]<<(WOI-1-ii)); else tmp = acc[ii] + (divrp[ii]>>(1+ii-WOI));`——这正是 `fxp_div` 的 `acct = acc + (divr<<shamt)` 或 `>>(-shamt)`，因为 `WOI-1-ii = shamt`、`1+ii-WOI = -shamt`。`tmp` 是用阻塞赋值 `=`（[第 603 行](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L603) 声明的组合临时变量），在时序块里作中间量用。
- [第 626–632 行](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L626-L632)：`if(tmp < divdp[ii]) begin acc[ii+1]<=tmp; res[ii+1][WOF+WOI-1-ii]<=1'b1; end else begin acc[ii+1]<=acc[ii]; res[ii+1][WOF+WOI-1-ii]<=1'b0; end`——**逐位写商**就靠 `res[ii+1][WOF+WOI-1-ii]`：第 `ii` 级把商写到部分商寄存器的第 `WOF+WOI-1-ii` 位（即 `WOF+shamt` 位），与 `fxp_div` 的 `out[WOF+shamt]` 完全一致。够「减」（`tmp<divdp[ii]`）则置 1 且余数累加，不够则置 0 且余数恢复（`acc[ii+1]<=acc[ii]`）。

把这一段与 `fxp_div` 的 [第 459–471 行](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L459-L471) 并排，算法逻辑逐句对得上，唯一差别是「时间迭代」变成了「空间展开 + 寄存器级联」。

#### 4.4.4 深入思考：`<` 与 `<=` 的差别，以及为什么仍然等价

细心的读者会发现一个**真实的差别**：`fxp_div` 的置位判定是 `acct <= divd`（[第 466 行](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L466)），而 `fxp_div_pipe` 是 `tmp < divdp[ii]`（[第 626 行](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L626)）——一个是「小于等于」，一个是「严格小于」。当 `tmp` **恰好等于** `divdp[ii]`（即整除情形，部分积正好够抵满被除数）时，两者会做出不同选择：

- `fxp_div`（`<=`）：置该位为 1，`acc` 正好到达 `divd`，余数为 0，随后舍入条件 `(divd-acc)=0` 不成立，**不触发舍入**。
- `fxp_div_pipe`（`<`）：**不置**该位，`acc` 保持不变；此后所有更低位都会被逐位置 1（因为剩余的余数总能被更小的位移项补上），形成一段「全 1 尾巴」，最终余数恰好等于 `divrp>>WOF`；舍入级判定「真值更靠近 ceil」成立，于是 `roundedres = res + 1`，**进位穿过全 1 尾巴正好把跳过的那一位重新置起来**。

举个最小例子：设 `divd == divr`（即商正好等于 1.0），取 `WOI=WOF=8`。`fxp_div` 在 `shamt=0` 处 `acct=divr==divd` 命中 `<=`，置 `out[8]=1`，得到 `0x0100`（即 1.0），余数 0，不舍入。`fxp_div_pipe` 在 `ii=7`（对应 `shamt=0`）处 `tmp==divd` 不满足 `<`，跳过 `res[8]`；随后 `ii=8..15` 把 `res[7..0]` 全置 1，得到 `res=0x00FF`；舍入级检测到应进位，`0x00FF + 1 = 0x0100`——**进位穿过全 1 尾巴把第 8 位顶起来，结果与 `fxp_div` 完全一致**。

> **结论：** `<` 与 `<=` 的差别只出现在「整除」这种精确命中情形，而舍入级（4.5）的「进位 +1」恰好把跳过的那一位重建回来。因此两版最终得到的 `out/overflow` 是**功能等价**的——这正是第 5 节综合实践要实证的核心命题。这也是为什么作者敢在 README 模块表里把两者列为同一运算的「单周期版」与「流水线版」。

#### 4.4.5 代码实践

**实践目标：** 手算一个「整除」用例在两版里的中间过程，验证舍入级确实把 `<` 跳过的位重建回来。

**操作步骤：**

1. 取 `WOI=WOF=8`、`WIIA=WIFA=WIIB=WIFB=8`，设 `dividend = divisor`（即 `divd=divr`，真商 = 1.0 = `0x0100`）。
2. 仿照 4.4.4，手算 `fxp_div`（`<=`）的主循环：只有 `shamt=0` 命中，`out[8]=1`，得 `0x0100`，余数 0，舍入不触发。
3. 手算 `fxp_div_pipe`（`<`）的主循环：`ii=7` 跳过 `res[8]`，`ii=8..15` 置 `res[7..0]=1`，得 `res[16]=0x00FF`，余数 `=divr>>8`。
4. 手算舍入级：`(acc + divr>>8 - divd) < (divd - acc)` 即 `0 < divr>>8` 成立，`roundedres = 0x00FF + 1 = 0x0100`。

**需要观察的现象：** 两版在主循环阶段的部分商不同（`0x0100` vs `0x00FF`），但经过舍入级后都收敛到 `0x0100`。

**预期结果：** 两版最终 `out` 均为 `0x0100`（1.0），`overflow=0`。可用第 5 节的自校验 testbench 实证（待本地验证）。

#### 4.4.6 小练习与答案

**练习 1：** `ii=0` 对应 `shamt` 等于多少？它写 `res` 的哪一位？为什么这一位代表「最高位商」？

> **答案：** `ii=0` 对应 `shamt = WOI-1-0 = WOI-1`，写到 `res[1][WOF+WOI-1]`，即 `res` 的最高有效位（`WOF+WOI-1`）。因为 `shamt=WOI-1` 时移位项是 `divr<<(WOI-1)`，是所有候选中最大的（代表商的最高位 2^(WOI-1)），从最大位开始试探正是恢复余数法「从高到低」的顺序。

**练习 2：** 主循环 always 块里既用了非阻塞 `<=`（如 `acc[ii+1]<=tmp`）又用了阻塞 `=`（`tmp = acc[ii]+...`），为什么不冲突？

> **答案：** 因为它们作用的对象不同。`tmp` 是一个纯组合的临时变量（[第 603 行](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L603) `reg [WRI+WRF-1:0] tmp;`），用阻塞 `=` 是为了在同一个时钟沿内「先算出 `tmp`，再拿 `tmp` 去决定寄存器写什么」，属于「在时序块里借用阻塞赋值做组合中间量」的常见写法；而真正要打进级间寄存器的 `acc[ii+1]/res[ii+1]/divdp[ii+1]` 等都用非阻塞 `<=`，保证它们读到的是**上一级本沿之前**的旧值、本沿末尾才统一更新——这正是流水线级联不窜位的关键。

---

### 4.5 舍入级与符号/溢出饱和输出级：最后两拍

#### 4.5.1 概念说明

主循环结束后，数据停在数组下标 `[WOI+WOF]`。最后两级把 `fxp_div` 里「舍入 + 补符号 + 溢出饱和」这三件事拆成两拍完成：

- **舍入级**（第 \(WOI+WOF+2\) 级）：读 `res[WOI+WOF]/acc[WOI+WOF]/divdp[WOI+WOF]/divrp[WOI+WOF]`，判定真值更靠近 floor 还是 ceil；若更靠近 ceil（且 `ROUND` 开启、且 `res` 非全 1 防回绕），则 `roundedres = res + 1`，否则 `roundedres = res`。同时把 `sign[WOI+WOF]` 传成 `rsign`。
- **输出级**（第 \(WOI+WOF+3\) 级）：按 `rsign` 给 `roundedres` 补回二进制补码符号（负数取反 +1），并做上溢出（正超限 → 钳到正最大）/下溢出（负超限 → 钳到负最小）饱和，写出最终的 `out/overflow`。

#### 4.5.2 核心流程

舍入判定的几何含义（与 `fxp_div` 完全一致，见 u2-l3）：比较真商到 floor 与 ceil 的距离，取更近者。设主循环结束后余数为 `divd - acc`，再加一个 `divr>>WOF`（即比最低位再低一位的位移项）就得到 ceil 对应的累计，判定式为：

\[
\bigl(\underbrace{acc + (divr \gg WOF) - divd}_{\text{ceil 距离}}\bigr) < \bigl(\underbrace{divd - acc}_{\text{floor 距离}}\bigr)
\]

成立则向 `res` 加 1（更靠近 ceil）。输出级再据 `rsign` 与 `roundedres` 的最高位判断是否溢出并补符号。

#### 4.5.3 源码精读

舍入级：

[RTL/fixedpoint.v:636-647](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L636-L647) —— `always @(posedge clk or negedge rstn)`：复位时 `roundedres<=0; rsign<=0;`；否则若 `ROUND && ~(&res[WOI+WOF]) && (acc[WOI+WOF]+(divrp[WOI+WOF]>>WOF)-divdp[WOI+WOF]) < (divdp[WOI+WOF]-acc[WOI+WOF])` 则 `roundedres <= res[WOI+WOF] + ONEO`，否则 `roundedres <= res[WOI+WOF]`，并把 `rsign <= sign[WOI+WOF]`。这段判定式与 `fxp_div` 的舍入（[第 473–477 行](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L473-L477)）逐字对应，只是把 `acct-divd<divd-acc` 换成了显式的三段式；`~(&res[WOI+WOF])` 等价于 `fxp_div` 的 `~(&out)`，防止 `res` 为全 1 时 `+1` 回绕。注意这一级还**承担着 4.4.4 所述的「进位重建」职责**——正是它把 `<` 跳过的那一位顶回来。

输出级（注释写作 `process roof and output`，`roof` 指「饱和钳位到上/下限」）：

[RTL/fixedpoint.v:649-671](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L649-L671) —— `always @(posedge clk or negedge rstn)`：复位时 `overflow<=0; out<=0;`；否则默认 `overflow<=0`，然后分 `rsign==1`（负结果）与 `rsign==0`（正结果）两支：

- 负结果：若 `roundedres` 最高位已为 1（说明幅值本就占了符号位），且低位非全 0，则**下溢出** `overflow<=1`，把 `out` 钳成负最小（最高位 1、其余 0）；否则正常取补码 `out <= (~roundedres)+ONEO`。这与 `fxp_div` 的 [第 480–487 行](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L480-L487) 对应。
- 正结果：若 `roundedres` 最高位为 1（正数占到了符号位，幅值超限），则**上溢出** `overflow<=1`，把 `out` 钳成正最大（最高位 0、其余全 1）；否则 `out <= roundedres`。这与 `fxp_div` 的 [第 488–494 行](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L488-L494) 对应。

至此数据走完了全部 \(WOI+WOF+3\) 级，`out/overflow` 稳定输出。

#### 4.5.4 代码实践

**实践目标：** 观察舍入级寄存器 `roundedres` 与最终输出 `out` 之间固定的 1 拍差。

**操作步骤：**

1. 在学习用 testbench 里追加层次名探针打印 `roundedres` 与 `out`：

```verilog
// ===== 示例代码：追加探针（读者自行新建文件） =====
// $display("  [probe] roundedres=%h  out=%h  overflow=%b",
//          fxp_div_pipe_i.roundedres, odiv, odivo);
```

2. 锁定一组会触发舍入（真商小数部分 > 0.5 ULP）的输入，观察 `roundedres` 是否等于「未舍入的部分商 +1」。
3. 数 `roundedres` 比 `out` 提前几拍稳定。

**需要观察的现象：** `roundedres` 比 `out` 提前 1 拍稳定（差最后那一拍输出级）；当输入使真商更靠近 ceil 时，能看到 `roundedres` 比主循环的部分商 `res[WOI+WOF]` 多了 1。

**预期结果：** `roundedres` 与 `out` 固定差 1 拍；在整除用例中能看到 `roundedres` 比部分商 `+1`（即 4.4.4 的进位重建）。具体数值「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1：** 为什么要把「舍入」与「符号/溢出饱和」拆成两级，而不是像 `fxp_div` 那样放在一个 `always @(*)` 里？

> **答案：** 纯粹为了**缩短关键路径**。`fxp_div` 是组合逻辑，把舍入加法、补码取反、溢出比较、饱和多路选择全堆在一个 `always @(*)` 里无所谓（反正整条路径已经很长）。但流水线版要把每一段都切短：舍入级只做「一次比较 + 一次加法 + 寄存」，输出级只做「补码取反 + 溢出比较 + 饱和选择 + 寄存」，每级关键路径都更短，\(f_{\max}\) 更高。这也是为什么总级数是 `WOI+WOF+3` 而不是 `WOI+WOF+2`——末尾专门留两级分别处理这两件事。

**练习 2：** 输出级里，正结果时为什么会判断 `roundedres[WOI+WOF-1]`（最高位）来决定上溢出？

> **答案：** `out` 是 `WOI+WOF` 位的**有符号**定点数，最高位是符号位。正结果的幅值本不该占用符号位；一旦 `roundedres` 最高位为 1，说明正幅值已经大到「顶」进了符号位（即 \(\geq 2^{WOI-1}\)，超出 `(WOI,WOF)` 格式能表示的正最大值），构成上溢出，于是把 `out` 钳到正最大（最高位 0、其余全 1）并置 `overflow=1`。这正是 u1-l2/u1-l3 讲过的「用数值范围而非位宽判溢出」的体现。

---

## 5. 综合实践

把本讲的「循环展开 + 数组级间寄存器 + 舍入/输出两级 + 延迟对齐」四条主线串成一个**自校验流式 testbench**。核心思路：用单周期 `fxp_div` 作黄金参考（它与软件数学等价、0 延迟），其输出经 \(L=WOI+WOF+3\) 级延迟线对齐到 `fxp_div_pipe` 的输出，逐拍比对 `out/overflow` 并统计通过/失败计数——一步同时验证「\(L\) 拍延迟对齐」「无气泡吞吐」与「两版功能等价（含 4.4.4 的 `<`/`<=` 重建）」。

**实践目标：** 每拍给两个 DUT 喂同一个新输入，黄金输出延迟 \(L\) 拍后与流水线输出比较，统计正确样本数，确认 `fail==0`。

**操作步骤：**

1. 在 `SIM/` 下新建学习用 testbench（**示例代码**，请勿修改 `RTL/fixedpoint.v` 或已有 testbench）。为让单周期黄金 `fxp_div` 的组合路径不至于太深、仿真更快，这里用较小的 `WOI=WOF=8` 配置（\(L=8+8+3=19\)）：

```verilog
// ============ 示例代码：自校验流式 testbench（读者自行新建，如 tb_div_pipe_stream.v） ============
`timescale 1ps/1ps
module tb_div_pipe_stream ();
localparam WIIA=8, WIFA=8, WIIB=8, WIFB=8, WOI=8, WOF=8;
localparam L = WOI+WOF+3;          // fxp_div_pipe 流水线级数 = 19

reg rstn = 1'b0;
reg clk  = 1'b1;
always #(10000) clk = ~clk;        // 50MHz
initial begin repeat(4) @(posedge clk); rstn<=1'b1; end

reg  [WIIA+WIFA-1:0] dividend = 0;
reg  [WIIB+WIFB-1:0] divisor  = 0;

// ---- 待测：流水线除法 ----
wire [WOI+WOF-1:0] out_pipe;
wire               ov_pipe;
fxp_div_pipe #(.WIIA(WIIA),.WIFA(WIFA),.WIIB(WIIB),.WIFB(WIFB),
               .WOI(WOI),.WOF(WOF),.ROUND(1)) dut_pipe (
    .rstn(rstn),.clk(clk),.dividend(dividend),.divisor(divisor),
    .out(out_pipe),.overflow(ov_pipe));

// ---- 黄金参考：单周期 fxp_div（组合逻辑，与软件等价）----
wire [WOI+WOF-1:0] out_gold;
wire               ov_gold;
fxp_div #(.WIIA(WIIA),.WIFA(WIFA),.WIIB(WIIB),.WIFB(WIFB),
          .WOI(WOI),.WOF(WOF),.ROUND(1)) dut_gold (
    .dividend(dividend),.divisor(divisor),
    .out(out_gold),.overflow(ov_gold));

// ---- 黄金输出延迟 L 拍，与流水线输出对齐（数组作移位寄存器）----
reg [WOI+WOF-1:0] gout [0:L-1];
reg               gov  [0:L-1];
integer k;
initial for(k=0;k<L;k=k+1) begin gout[k]=0; gov[k]=0; end
always @(posedge clk) begin
    gout[0] <= out_gold; gov[0] <= ov_gold;
    for(k=1;k<L;k=k+1) begin gout[k]<=gout[k-1]; gov[k]<=gov[k-1]; end
end
// gout[L-1] 即黄金结果延迟 L 拍后的值，与 out_pipe 同节拍对应同一输入

integer pass=0, fail=0, total=0, cyc=0;

// ---- 连续流式激励：每个时钟都换新输入（无气泡）----
always @(posedge clk)
    if(rstn) begin dividend <= $random; divisor <= $random; end

// ---- 对齐采样：流水线打满后逐拍比对 ----
always @(posedge clk) if(rstn) begin
    cyc <= cyc + 1;
    if(cyc >= L+2) begin                 // 跳过前几拍（延迟线尚未填满）
        total <= total + 1;
        if(out_pipe === gout[L-1] && ov_pipe === gov[L-1]) pass <= pass + 1;
        else begin fail <= fail + 1;
            $display("MISMATCH @cyc=%0d  pipe=%h_%b  gold=%h_%b",
                     cyc, out_pipe, ov_pipe, gout[L-1], gov[L-1]);
        end
    end
end

initial begin
    #2000000;                            // 跑足够长时间（远大于 L）
    $display("==== RESULT: total=%0d pass=%0d fail=%0d ====", total, pass, fail);
    if(fail==0) $display("==== PASS: fxp_div_pipe 与 fxp_div 功能等价, L=%0d 拍延迟对齐, 无气泡 ====", L);
    $finish;
end
endmodule
```

2. 编译运行（testbench 与 RTL 同时参与编译）：

```bash
cd SIM
iverilog -g2001 -o sim.out tb_div_pipe_stream.v ../RTL/fixedpoint.v && vvp -n sim.out
```

**需要观察的现象：**

- `total` 是一个较大的数（取决于 `#2000000` 的仿真时长与 20ns 周期，约几千到上万），`pass` 应**等于** `total`、`fail` 应为 **0**——这同时证明了「\(L\) 拍延迟对齐」「无气泡吞吐」与「两版功能等价（含整除情形下 `<`/`<=` 由舍入级重建）」。
- 由于每拍都喂新输入、每拍都有一次有效比较，`total` 之大本身就体现了「无气泡」。
- **反证级数**：把黄金延迟线深度从 `L` 改成 `L-1` 或 `L+1`（例如把比较对象从 `gout[L-1]` 换成 `gout[L-2]` 或再加一级），`fail` 会**暴增**——这反向证明延迟必须恰好是 \(WOI+WOF+3\) 拍。
- **反证舍进重建**：若刻意把舍入级条件改坏（例如临时把 DUT 的 `ROUND` 设为 0 而黄金保持 1，或反之），在整除用例附近可能出现 `MISMATCH`——这能帮助体会 4.4.4 所述「舍入级补偿 `<`/`<=`」的必要性。

**预期结果：** `fail==0` 且 `pass==total`，打印 `PASS` 行。若出现大量 `MISMATCH`，先检查：① 延迟线深度是否为 `L=WOI+WOF+3`；② 两个 DUT 的参数（尤其 `ROUND`）是否完全一致；③ 采样阈值 `cyc>=L+2` 是否足够（可适当调大）。具体 `total` 数值「待本地验证」，但 `fail` 应为 0。

**完成标志：** 你能不查源码说出「`fxp_div_pipe` 把 `fxp_div` 的 `WOI+WOF` 次迭代展开成了多少级、用了哪几个数组当级间寄存器、总级数为什么是 `WOI+WOF+3`、`ii` 与 `shamt` 如何对应」，并能解释「为什么主循环用 `<` 而单周期用 `<=`，两者却仍然功能等价」。

## 6. 本讲小结

- **动机**：`fxp_div` 的恢复余数主循环是 `WOI+WOF` 次迭代的纯组合串联，关键路径极长（README 标注「时序不易收敛」）；`fxp_div_pipe` 把每次迭代映射成一级流水线来切断长路径，用固定延迟换更高频率，吞吐量仍为每拍一个（无气泡）。
- **级数构成**：\(L = 1\)（首位输入寄存）\(+\;(WOI+WOF)\)（逐位求商，每位一级）\(+\;1\)（舍入级）\(+\;1\)（符号/溢出输出级）\(= WOI+WOF+3\)，与模块注释一致。
- **数组当级间寄存器**：用大小 `WOI+WOF+1` 的数组 `sign/acc/divdp/divrp/res` 作级间寄存器，下标即流水线级号；标量 `roundedres/rsign` 与 `out/overflow` 分别实现末尾的舍入级与输出级。
- **逐位展开**：`ii=0..WOI+WOF-1` 与单周期 `shamt=WOI-1..-WOF` 一一对应（\(\text{shamt}=WOI-1-ii\)）；`res[ii+1][WOF+WOI-1-ii]` 逐位写商，`acc[ii+1]` 级联累加余数，`divdp/divrp/sign` 一路下传不变。
- **`<` 与 `<=` 仍等价**：流水线主循环用 `tmp<divdp[ii]`（严格小于）、单周期用 `acct<=divd`（小于等于）；二者只在整除情形不同，而舍入级的「进位 +1」穿过全 1 尾巴正好重建跳过的位，故最终 `out/overflow` 功能等价。
- **验证方法学**：官方 testbench 是目视对比风格（`odiv` 滞后 `a/b` 共 \(L\) 行）；自校验做法是用单周期 `fxp_div` 作黄金参考，其输出延迟 \(L\) 拍后与 `fxp_div_pipe` 逐拍比对、统计通过/失败计数，一步验证「\(L\) 拍延迟对齐 + 无气泡 + 功能等价」。

## 7. 下一步学习建议

- 下一篇 [u3-l3（fxp_sqrt_pipe）](./u3-l3-sqrt-pipe.md) 把本讲的「循环展开 + 数组级间寄存器」套路用在一个同源的逐位算法上：逐位开方展开为 \(\lfloor WII/2\rfloor+WIF+2\) 级，级间寄存器数组 `sign/inu/resu/resu2` 的用法与本章的 `sign/acc/divdp/divrp/res` 如出一辙。建议先读 [RTL/fixedpoint.v:754-857](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L754-L857) 的 `fxp_sqrt_pipe`，对照本讲体会「同一种展开手法适配不同逐位算法」。
- 回看单周期基线 [fxp_div（RTL/fixedpoint.v:388-497）](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L388-L497)，并把它与 [fxp_div_pipe（第 505–673 行）](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L505-L673) 并排打开——两段代码的「算法 diff」几乎为 0，差别全在「时间迭代 → 空间展开 + 寄存器级联」，这是体会循环流水线化最直接的材料。
- 关于本讲用到的 testbench 技巧（`$signed*1.0/(1<<W)` 还原法、黄金模型 + 延迟线对齐、pass/fail 自校验、`iverilog -g2001` 编译），[u3-l6（仿真验证方法学）](./u3-l6-simulation-testbench.md) 会做系统性总结，并把单周期 testbench 与流水线 testbench 两种风格对照讲透。
- 若想继续横向对比「另一种把逐位算法深度流水线化」的对象，可看 [fxp2float_pipe（RTL/fixedpoint.v:932-1022）](https://github.com/WangXuan95/FPGA-FixedPoint/blob/35b175d6912f085c01f5b986e4f41c70a3807909/RTL/fixedpoint.v#L932-L1022)：它把定点转浮点的「逐位扫描」展开为 `WII+WIF+2` 级，同样是数组作级间寄存器，可作为本讲手法的第三种实例。
