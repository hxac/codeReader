# asiclib 标准单元库

## 1. 本讲目标

本讲是第 9 单元（ASIC 实现、物理设计与工程规范）的第二讲，承接 [u9-l1 双实现策略：soft vs hard](u9-l1-soft-hard-duality.md)。在上一讲里，我们把「同一功能为何有两套实现、如何用 `SYN`/`CFG_ASIC` 切换」讲清楚了。本讲要打开 hard 这一侧的黑盒，逐类认识 `asiclib` 里的标准单元（standard cell）。

学完本讲你应该能够：

- 说出 `asiclib` 是什么、它和 `stdlib` 的关系，理解「黄金模型（golden model）」契约与 `PROP` 参数的作用。
- 破解 ASIC 标准单元的命名规律（`and2`/`nand2`/`ao21`/`aoi22`/`dffrq`/`sdffrq`…），看名字就能猜出功能。
- 讲清集成门控时钟单元 ICG（`asic_clkicgand`）为什么能产生**无毛刺**的门控时钟，并把它和 `stdlib` 的 `oh_clockgate` 对应起来。
- 认识低功耗与物理填充类单元：电源开关 `asic_header`/`asic_footer`、隔离单元 `asic_isohi`/`asic_isolo`、保持单元 `asic_keeper`、电源钳位 `asic_tiehi`/`asic_tielo`、去耦电容 `asic_decap`、天线二极管 `asic_antenna`。
- 把 hard 侧的时序/同步单元（`asic_dffrq`/`asic_sdffrq`/`asic_rsync`/`asic_dsync`）与 `stdlib` 侧的原语一一对应。

> 全程阅读原则不变：**代码与协议文件才是事实**。`asiclib` 里同样存在命名漂移与占位桩（例如 `oh_clockgate` 的 hard 分支引用了一个并不存在的 `asic_clockgate`），我们会逐一指出，一律以 RTL 文本为准。

## 2. 前置知识

### 2.1 什么是标准单元库

把一块 ASIC 想象成用乐高搭起来的城市。综合工具（如 Design Compiler、Genus）不会从零「捏」每一个晶体管，而是从一个由晶圆厂（foundry）提供的**标准单元库**里挑现成的积木：与门、触发器、多路选择器……每个单元的高度、电源线位置、驱动强度都规整对齐，方便像拼砖一样一行行排成「行（row）」。这个库绑死在某个工艺（PDK，Process Design Kit）上——28 nm 的库不能用到 40 nm 上。

`asiclib` 就是 OH! 给这类标准单元写的**Verilog 行为模型集合**。

### 2.2 黄金模型（golden model）契约

真实的标准单元有三套描述：版图（layout，GDS）、SPICE 网表（模拟仿真用）、Verilog 模型（数字仿真用）。OH! 仓库里只能放 Verilog 那一份，它扮演**黄金模型**的角色——晶圆厂或设计公司做硬核实现时，**必须逐字复刻这份 Verilog 的逻辑功能**，否则仿真与硅片行为对不上。这是 [asiclib/README.md:1-8](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/asiclib/README.md#L1-L8) 四条说明的核心，我们稍后会逐条对照。

### 2.3 低功耗设计的几个关键词

`asiclib` 里有一批单元是为**电源域（power domain）**管理服务的，背后对应 UPF/CPF 这类低功耗意图描述。先记住四个词：

- **Power switch（电源开关）**：用 MOS 管做开关，关掉某块电路的 VDD（电源）或 VSS（地），让它彻底断电省漏电。断 VDD 的叫 **header**（PMOS），断 VSS 的叫 **footer**（NMOS）。
- **Isolation（隔离）**：一块电路断电后，它的输出会悬空成未知值 `x`，会污染还通着电的邻居。隔离单元在断电前把输出**钳位**到一个安全值（钳到 1 或钳到 0）。
- **Retention / Keeper（保持）**：断电时还想留住某些关键寄存器的值，用保持单元把电荷锁住。
- **Tie / Decap / Antenna**：物理填充用的辅助单元——钳位固定电平、提供去耦电容、泄放天线效应电荷。

本讲会把这些概念和 `asiclib` 里的具体单元一一对应。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `asiclib/README.md` | asiclib 的四条总纲：绑定 PDK、黄金模型、编译期链接、无依赖。 |
| `asiclib/hdl/asic_and2.v`、`asic_nand2.v`、`asic_ao21.v`、`asic_aoi22.v` | 组合门代表，演示命名规律。 |
| `asiclib/hdl/asic_dffq.v`、`asic_dffrq.v`、`asic_sdffrq.v`、`asic_latq.v` | 时序单元：触发器、带扫描的触发器、锁存器。 |
| `asiclib/hdl/asic_clkicgand.v`、`asic_clkicgor.v` | 集成门控时钟（ICG），本讲重点。 |
| `asiclib/hdl/asic_header.v`、`asic_footer.v` | 电源开关（PMOS/NMOS）。 |
| `asiclib/hdl/asic_isohi.v`、`asic_isolo.v`、`asic_keeper.v`、`asic_tiehi.v`、`asic_tielo.v`、`asic_decap.v`、`asic_antenna.v` | 低功耗与物理填充单元。 |
| `asiclib/hdl/asic_rsync.v`、`asic_dsync.v` | 复位/数据同步器，对应 `stdlib` 的 `oh_rsync`/`oh_dsync`。 |
| `stdlib/rtl/oh_clockgate.v`、`oh_dffq.v` | soft 侧对照，用于理解 soft↔hard 对应关系。 |

`asiclib/hdl/` 下一共有 **110 个** `.v` 文件，本讲只精读其中有代表性的十几个，其余都遵循同一套规律。

## 4. 核心概念与源码讲解

### 4.1 黄金模型：asiclib 是绑定 PDK 的标准单元库

#### 4.1.1 概念说明

`stdlib` 是「可综合、工艺无关、参数化」的 RTL——它描述**行为**，由综合工具映射成标准单元。`asiclib` 走另一条路：它直接**就是标准单元本身的行为模型**，每一个 `.v` 文件对应晶圆厂库里一个具体的物理单元，绑定在特定 PDK 上。

二者的关键差别（承接 [u9-l1](u9-l1-soft-hard-duality.md)）：

| 维度 | `stdlib`（soft） | `asiclib`（hard） |
| --- | --- | --- |
| 位宽 | 参数化 `DW`，可例化成任意位宽 | **单位宽**，一个单元只处理 1 比特 |
| 参数 | `DW`、`SYN`、`TYPE`… | 只有 `PROP = "DEFAULT"`（工艺属性） |
| 依赖 | 会例化别的 `oh_*`/`asic_*` | **无任何依赖**，自包含 |
| 用途 | 综合 | 编译期按 foundry 链接进来做仿真 |

#### 4.1.2 核心流程

`asiclib` 在设计流程里是这样被使用的：

1. **写 RTL** 时用 `stdlib` 的 `oh_*` 原语（参数化、好读）。
2. **综合** 时，工具把 `oh_*` 展开成网表，网表里全是某个 PDK 的标准单元。
3. **做 hard 仿真**时，用 `asiclib` 的黄金模型替换/补齐这些标准单元的behavior，因为它精确反映了硅片行为（含时序、功耗注释，虽然 OH! 仓库里只放了功能模型）。

黄金模型契约要求：物理实现必须**精确复刻** `.v` 里的逻辑功能——这就是为什么这些文件写得如此干净、如此「笨」。它们不是为了优雅，而是为了**可逐字对照**。

#### 4.1.3 源码精读

先看总纲 [asiclib/README.md:1-8](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/asiclib/README.md#L1-L8)，四条要点逐字对照：

- `ASICLIB is a library of low level asic cells hard-coded to a specific PDK.` —— 绑定 PDK。
- `The hdl/*.v files represent the golden model for the library.` —— 黄金模型。
- `The library is meant to be linked in at compile time based on the foundry being targeted.` —— 编译期按 foundry 链接。
- `The cells do not have any dependancies.` —— 单元无依赖（自包含）。

最简单的组合门 [asic_and2.v:7-15](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/asiclib/hdl/asic_and2.v#L7-L15) 展示了 hard 单元的标准长相：

```verilog
module asic_and2 #(parameter PROP = "DEFAULT")  (
   input  a,
   input  b,
   output z
   );
   assign z = a & b;
endmodule
```

注意三点：单比特输入输出（`a`、`b`、`z` 都是 1 位）、唯一的参数是 `PROP`、一行 `assign` 完事、不例化任何别的模块。

和 soft 侧对比最直观的是触发器。hard 的 [asic_dffq.v:7-16](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/asiclib/hdl/asic_dffq.v#L7-L16)：

```verilog
module asic_dffq #(parameter PROP = "DEFAULT")   (
    input      d,
    input      clk,
    output reg q
    );
   always @ (posedge clk)
     q <= d;
endmodule
```

而 soft 的 [oh_dffq.v:8-18](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_dffq.v#L8-L18)：

```verilog
module oh_dffq #(parameter DW = 1) // array width
   (
    input [DW-1:0] 	d,
    input [DW-1:0] 	clk,
    output reg [DW-1:0] q
    );
   always @ (posedge clk)
     q <= d;
endmodule
```

差别一目了然：`oh_dffq` 用 `DW` 参数化，例化 `DW=8` 就是 8 个触发器；`asic_dffq` 永远只是 1 个触发器，因为物理上一个标准单元就是 1 比特。**多比特寄存器在 hard 侧靠「例化很多个 1 比特单元」实现**，而不是靠参数。

#### 4.1.4 代码实践

**实践目标**：亲手验证「hard 单元是单位宽、无依赖、只有 PROP 参数」这一规律。

**操作步骤**：

1. 在仓库根目录用 glob 列出全部 hard 单元：观察 `asiclib/hdl/*.v` 共 110 个文件。
2. 任选 5 个组合门（如 `asic_and2.v`、`asic_or2.v`、`asic_xor2.v`、`asic_nand2.v`、`asic_nor2.v`），确认它们的端口都是单比特 `a/b/z`、唯一参数都是 `PROP`、体内都没有例化别的模块。
3. 再任选 5 个时序单元（如 `asic_dffq.v`、`asic_dffrq.v`、`asic_latq.v`），同样确认。

**需要观察的现象**：所有这些文件的「骨架」高度一致——模块声明带 `#(parameter PROP = "DEFAULT")`、端口单比特、`assign` 或单段 `always`、`endmodule`。

**预期结果**：你会得出结论——`asiclib` 的每一项都是一个自包含的 1 比特标准单元，可逐字作为黄金模型交给 foundry 实现。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `asic_and2` 不像 `oh_and2` 那样支持参数化位宽 `DW`？

**参考答案**：因为一个物理标准单元就是 1 比特。要做一个 8 位与门，在 hard 侧是例化 8 个 `asic_and2`，而不是让一个单元变宽。参数化属于综合前的 RTL 抽象（soft 侧），落到标准单元就是「数量」而非「位宽」。

**练习 2**：`PROP = "DEFAULT"` 这个参数有什么用？删掉它会影响功能吗？

**参考答案**：功能上完全不影响（它不出现在任何逻辑表达式里）。它的作用是给后端工具留一个**工艺属性注入点**，用来携带单元的时序/功耗/版图属性字符串。黄金模型层面它是占位符。

---

### 4.2 标准单元命名规律与主要家族

#### 4.2.1 概念说明

工业界标准单元库（如 TSMC 的 ` tcbn28...`、Synopsys 的 `NBX`）的命名像一串密码，但 OH! 用了一套**贴近逻辑表达式**的简化命名，读出名字就能还原功能。掌握这套规律，110 个文件就能「批量理解」。

#### 4.2.2 核心流程：命名解码规则

把单元名从左到右拆成几段：

1. **前缀 `asic_`**：表示这是 `asiclib` 的 hard 单元（区别于 `oh_`）。
2. **功能段**：用接近布尔代数的缩写描述逻辑。
3. **后缀**：表示变体（复位 `r`、置位 `s`、扫描 `s`、反相输出 `n`、下降沿 `n`）。

常见功能段速查：

| 缩写 | 含义 | 例子 |
| --- | --- | --- |
| `and2/and3/and4` | 2/3/4 输入与门 | `asic_and2` |
| `nand2` | 与非 | `asic_nand2` |
| `or2`、`nor2`、`xor2`、`xnor2` | 或、或非、异或、同或 | `asic_or2` |
| `inv` | 反相器（非门） | `asic_inv` |
| `buf` | 同相缓冲器 | `asic_buf` |
| `ao21` | And-Or：2 输入与 + 1 输入或 | `asic_ao21` |
| `aoi22` | And-Or-Invert：两路 2 输入与，相或后取反 | `asic_aoi22` |
| `oa21`/`oai22` | Or-And / Or-And-Invert（与上面对偶） | `asic_oai22` |
| `mux2`/`mux4` | 多路选择器 | `asic_mux2` |
| `dmux2` | 解复用（1 进多出） | `asic_dmux2` |
| `dffq` | 上升沿 D 触发器，Q 输出 | `asic_dffq` |
| `dffrq`/`dffsq` | 带异步复位 / 置位的 DFF | `asic_dffrq` |
| `sdffrq` | **scan** DFF（带扫描输入） | `asic_sdffrq` |
| `latq` | 透明锁存器 | `asic_latq` |
| `clk*` | 时钟树专用单元（高驱动、平衡相位） | `asic_clkbuf` |

**复合门名字的数字编码**：`ao22` 读作「2 组、每组 2 输入的与，再或」。第一个 `2` 是组数，第二个 `2` 是每组输入数。所以 `aoi22` 是 \(\lnot\big((a_0 a_1)+(b_0 b_1)\big)\)。`ao21` 则是「1 组 2 输入与 + 1 个或输入」。

后缀（时序单元）解码，承接 [u2-l2 时序原语](u2-l2-sequential-flops.md) 的命名密码：

- 中间的 `r` = 异步复位（reset），`s` = 异步置位（set）。
- 开头的 `s`（在 `dff` 之前，即 `sdff`）= scan（扫描）。
- 结尾的 `n` = 反相输出（输出 Q-bar）。例如 `dffrqn` = 带复位 + 反相输出；`dffnq` = 下降沿 + Q 输出。

#### 4.2.3 源码精读

看几个代表性复合门。`asic_ao21` —— And-Or（[asic_ao21.v:7-16](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/asiclib/hdl/asic_ao21.v#L7-L16)）：

```verilog
module asic_ao21 #(...)  (
   input a0, input a1, input b0,
   output z );
   assign z = (a0 & a1) | b0;
```

`asic_aoi22` —— And-Or-Invert（[asic_aoi22.v:7-17](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/asiclib/hdl/asic_aoi22.v#L7-L17)），布尔表达即：

\[
z = \lnot\big((a_0 \land a_1) \lor (b_0 \land b_1)\big)
\]

```verilog
   assign z = ~((a0 & a1) | (b0 & b1));
```

> 为什么 ASIC 偏爱 `aoi`/`oai` 这类「带末级反相」的复合门？因为 CMOS 工艺里，末级反相器天然存在、驱动能力强，用「或非/与非 + 反相」结构比纯与门/或门面积更小、速度更快。综合工具会自动把 `a&b` 这类逻辑映射成 `aoi`+反相。

再看时序单元的变体。带复位的触发器 [asic_dffrq.v:8-21](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/asiclib/hdl/asic_dffrq.v#L8-L21)：

```verilog
module asic_dffrq #(...) (
    input d, input clk, input nreset,
    output reg q );
   always @ (posedge clk or negedge nreset)
     if(!nreset) q <= 1'b0;
     else        q <= d;
```

`negedge nreset` 是**异步**低有效复位，和 `stdlib` 的 `oh_dffrq` 语义完全一致——这就是黄金模型要复刻的功能。

**带扫描的触发器**是 hard 侧独有的类别，soft 侧没有对应。`asic_sdffrq`（[asic_sdffrq.v:9-24](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/asiclib/hdl/asic_sdffrq.v#L9-L24)）：

```verilog
module asic_sdffrq #(...) (
    input d, input si, input se, input clk, input nreset,
    output reg q );
   always @ (posedge clk or negedge nreset)
     if(!nreset)     q <= 1'b0;
     else            q <= se ? si : d;
```

多出 `si`（scan in，扫描串入）和 `se`（scan enable，扫描使能）。当 `se=1` 时，触发器不采正常数据 `d`，而是采上一级移过来的 `si`——把全片所有触发器串成一条长移位寄存器（**扫描链 scan chain**），这是芯片测试（DFT，Design-for-Test）的基础：测试机通过扫描链把任意激励「移」进芯片、再把响应「移」出来检查。`stdlib` 不需要它，因为扫描链是制造测试手段，只在真实硅片上有意义。

组合侧再补两个：多路选择器 [asic_mux2.v:7-16](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/asiclib/hdl/asic_mux2.v#L7-L16) 用 `assign z = (d0 & ~s) | (d1 & s);`（与 `oh_mux2` 的 one-hot 思路不同，这里直接写布尔式，便于映射成传输门）；三态缓冲器 [asic_tbuf.v:7-15](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/asiclib/hdl/asic_tbuf.v#L7-L15) 用 `assign z = oe ? a : 1'bz;`（`1'bz` 是高阻，三态 IO 必备）。

#### 4.2.4 代码实践

**实践目标**：靠命名规律「盲猜」单元功能，再开文件验证。

**操作步骤**：

1. 先不看源码，只看文件名，猜这 6 个单元的布尔表达式：`asic_ao31`、`asic_oai21`、`asic_dffsqn`、`asic_sdffrqn`、`asic_clknand2`、`asic_xnor2`。
2. 逐一打开对应 `.v` 文件，核对 `assign` 或 `always` 的实现是否与你猜的一致。

**需要观察的现象**：`ao31` = 「1 组 3 输入与 + 1 个或输入」；`oai21` = 「或-与-非」；`dffsqn` = 带异步置位 + 反相输出的 DFF；`sdffrqn` = 带扫描 + 带复位 + 反相输出；`clknand2` = 时钟树用的 2 输入与非；`xnor2` = 同或。

**预期结果**：你的猜测与源码功能表达式一致。少数名字（如 `oai21` 的输入数）若拿不准，以源码为准。

#### 4.2.5 小练习与答案

**练习 1**：写出 `asic_oai22` 的布尔表达式。

**参考答案**：Or-And-Invert，2 组每组 2 输入先或、再与、最后取反：

\[
z = \lnot\big((a_0 \lor a_1) \land (b_0 \lor b_1)\big)
\]

**练习 2**：`asic_sdffrq` 比 `asic_dffrq` 多了哪两个端口？它们什么时候起作用？

**参考答案**：多了 `si`（scan in）和 `se`（scan enable）。在测试模式下 `se=1`，触发器采 `si` 而非 `d`，从而把芯片内触发器串成扫描链供制造测试用；正常功能模式下 `se=0`，退化为普通 `dffrq`。

---

### 4.3 ICG：集成门控时钟单元（本讲重点）

#### 4.3.1 概念说明

**门控时钟（clock gating）**是最有效的低功耗手段之一：当某块电路暂时不工作时，把送给它的时钟关掉，触发器不翻转，动态功耗立刻降下来。这承接 [u2-l3 时钟控制原语](u2-l3-clock-primitives.md) 讲过的 `oh_clockgate`。

但门控时钟有个致命陷阱——**毛刺（glitch）**。最朴素的想法是 `assign eclk = clk & en;`，可 `en` 是个数据信号，随时可能翻转。如果 `en` 恰好在 `clk` 高电平期间从 1 掉到 0，`clk & en` 就会削出半个窄脉冲（runt pulse），这个毛刺会被当成一个假时钟沿，让下游触发器乱翻。

正确做法是**集成门控时钟单元 ICG（Integrated Clock Gating）**：用一个**负电平透明锁存器**把 `en` 在时钟低电平段「抓稳」，确保 `en` 只在 `clk=0` 时变化、在 `clk=1` 整段高电平里保持稳定。`asiclib` 提供了 `asic_clkicgand`（AND 型）和 `asic_clkicgor`（OR 型）两个 ICG 单元。

#### 4.3.2 核心流程

`asic_clkicgand` 内部其实就两件事：

1. **抓稳使能**：一个电平敏感锁存器 `en_stable`，当时钟为低（`~clk`）时透明，把 `en | te` 采进来；当时钟为高时保持，冻结住。
2. **门控输出**：`eclk = clk & en_stable`。

因为 `en_stable` 在整个 `clk=1` 高电平段都不变，`clk & en_stable` 要么完整透传一个高电平、要么完全关断，绝不会削出半截。

门控输出关系式：

\[
\text{eclk} = \text{clk} \land \text{en\_stable}, \qquad
\text{en\_stable 更新当且仅当 } \text{clk}=0
\]

`te`（test enable）是测试旁路：测试时强制 `te=1`，让时钟永不被门控关断，保证扫描链能正常移位。

#### 4.3.3 源码精读

[asic_clkicgand.v:7-22](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/asiclib/hdl/asic_clkicgand.v#L7-L22)：

```verilog
module asic_clkicgand #(parameter PROP = "DEFAULT")  (
   input  clk,   // clock input
   input  te,    // test enable
   input  en,    // enable (from positive edge FF)
   output eclk   // enabled clock output
   );
   reg en_stable;
   always @ (clk or en or te)
     if (~clk)
       en_stable <= en | te;
   assign eclk = clk & en_stable;
endmodule
```

逐行读：

- `always @ (clk or en or te)` 是电平敏感（不是边沿），只要 `clk/en/te` 任一变化就求值——这正是锁存器行为。
- `if (~clk) en_stable <= en | te;`：**只在 clk 为低时**把 `en|te` 写进 `en_stable`；clk 为高时这条不执行，`en_stable` 保持原值（锁存）。
- `assign eclk = clk & en_stable;`：高电平段用稳定的 `en_stable` 相与。

对照 soft 侧的 `oh_clockgate`（[oh_clockgate.v:8-46](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_clockgate.v#L8-L46)），soft 分支用 `oh_lat0`（低电平透明锁存器）实现**完全相同的逻辑**——`en_sl = en | te`，锁存后 `eclk = clk & en_sh`。二者是同一思想的 soft/hard 双实现，这正是 [u9-l1](u9-l1-soft-hard-duality.md) 所说的 soft↔hard 对应。

OR 型 ICG [asic_clkicgor.v:7-22](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/asiclib/hdl/asic_clkicgor.v#L7-L22) 是对偶版：`if (clk) en_stable <= en | te;`（在 clk 高电平抓稳），`assign eclk = clk | ~en_stable;`。它用于「默认开、按需关」的时钟网络，原理与 AND 型镜像。

> ⚠️ **命名漂移警示（重要）**：soft 侧 `oh_clockgate` 的 hard 分支（[oh_clockgate.v:36-44](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_clockgate.v#L36-L44)）例化的模块名叫 `asic_clockgate`，但 `asiclib/hdl/` 里**根本没有 `asic_clockgate.v`**——真正实现这个功能的 hard 单元是 `asic_clkicgand`。这是本仓库典型的命名漂移：soft 想调 hard，名字却对不上，导致 hard 分支无法直接编译。结论：**要把 `oh_clockgate` 跑在 hard 模式，需手动把 `asic_clockgate` 桥接到 `asic_clkicgand`**（二者端口一致）。这条会作为本讲的主实践任务。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：在 `asiclib` 中找出 `oh_clockgate` 对应的 hard 单元，说明 `asic_clkicgand` 如何实现无毛刺门控，并发现并处理命名漂移。

**操作步骤**：

1. 打开 [stdlib/rtl/oh_clockgate.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_clockgate.v)，定位 hard 分支（`SYN != "TRUE"`），记录它例化的模块名（`asic_clockgate`）。
2. 在 `asiclib/hdl/` 中查找 `asic_clockgate.v` —— 你会确认它不存在；再按功能/端口（`clk/te/en/eclk`）去找真正的单元，定位到 `asic_clkicgand.v`。
3. 对照两边的端口表，确认 `asic_clkicgand` 与 `oh_clockgate` 声明的端口一一对应（clk/te/en/eclk 完全一致），所以它是正确的 hard 替身。
4. **写一个最小对比 testbench（示例代码，非项目原有文件）**，同时驱动 `en` 在 `clk` 高电平期间翻转，对比「朴素门控」与 `asic_clkicgand` 的输出：

```verilog
// 示例代码：glitch 对比（仅供理解，非仓库文件）
module tb_icg;
  reg clk=0, en, te=0;
  wire eclk_naive, eclk_icg;
  always #5 clk = ~clk;            // 100MHz 时钟

  // 朴素门控：有毛刺风险
  assign eclk_naive = clk & en;

  // 正确 ICG
  asic_clkicgand icg (.clk(clk), .te(te), .en(en), .eclk(eclk_icg));

  initial begin
    en = 1;
    #12 en = 0;   // 故意在 clk 高电平段拉低 en
    #8  en = 1;
    #20 $finish;
  end
endmodule
```

5. 用 `scripts/build.sh` 同款命令编译（`iverilog -g2005`），再用 `gtkwave` 看波形。

**需要观察的现象**：

- `eclk_naive` 在 `en` 高电平拉低的瞬间出现一个被截断的窄脉冲（runt pulse / glitch）。
- `eclk_icg` 在那次翻转**不会**立即响应，要等到下一个 `clk` 低电平段 `en_stable` 才更新，故 `eclk` 只输出完整周期、被干净关断，没有半截脉冲。

**预期结果**：`asic_clkicgand` 输出的 `eclk` 严格由完整时钟周期组成，证明负电平透明锁存器消除了门控毛刺。

**若无法本地运行**：明确标注「待本地验证」。即便不仿真，仅从 [asic_clkicgand.v:16-20](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/asiclib/hdl/asic_clkicgand.v#L16-L20) 的 `if(~clk)` 条件也能推断出 `en_stable` 只在低电平更新，从而在逻辑上保证无毛刺。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `asic_clkicgand` 里的锁存条件改成 `if (clk)`（在 clk 高电平抓 en），还能保证无毛刺吗？为什么？

**参考答案**：不能。改成 `if(clk)` 后，`en_stable` 在 clk 高电平段会跟随 `en` 变化，`clk & en_stable` 就可能在 `en` 翻转瞬间削出毛刺——退化成朴素门控。无毛刺的关键是 `en_stable` 在 clk 高电平整段保持不变，因此必须用 `~clk`（低电平透明）。

**练习 2**：`te`（test enable）为什么必须用 `en | te` 而不是单独控制？

**参考答案**：`te` 是测试旁路，要求 `te=1` 时时钟强制透传、永不被门控。把它和 `en` 相或，只要 `te=1`，`en_stable` 就恒为 1，`eclk=clk`，扫描移位不受功能使能影响；`te=0` 时退化为正常门控。

---

### 4.4 低功耗与物理填充单元

#### 4.4.1 概念说明

除逻辑门和触发器外，一颗真实芯片还需要一批**服务于电源与物理**的单元。它们大多没有「逻辑功能」可言——其价值在版图、电气特性或可靠性，`asiclib` 给它们写的 `.v` 主要是**接口契约**（端口名 + 注释），体内的开关级连接只是示意。这一类单元对应 §2.3 提到的电源域管理概念。

| 单元 | 作用 | 为什么需要 |
| --- | --- | --- |
| `asic_header` | PMOS 电源开关，断/通 VDD | 关电源省漏电 |
| `asic_footer` | NMOS 电源开关，断/通 VSS | 关地省漏电 |
| `asic_isohi` | 隔离到 1（钳高） | 断电域输出钳到安全值 1 |
| `asic_isolo` | 隔离到 0（钳低） | 断电域输出钳到安全值 0 |
| `asic_keeper` | 电荷保持 | 状态保留 |
| `asic_tiehi`/`asic_tielo` | 钳位固定电平 | 给常 1/常 0 输入提供专用单元（不直接接电源/地，避免驱动不稳） |
| `asic_decap` | 去耦电容 | 抑制电源网络瞬时压降 |
| `asic_antenna` | 天线二极管 | 泄放制造工艺的天线效应电荷，防击穿 |

#### 4.4.2 核心流程：上下电与隔离序列

电源域切换有一套严格时序，简化如下：

1. **准备下电**：先把该域所有输出用 `asic_isohi`/`asic_isolo` 钳到安全值（`iso=1`），防止断电后输出悬空成 `x` 污染邻居。
2. **断电**：`asic_header` 的 `sleep=1`（或 `asic_footer` 的 `nsleep=0`）关断电源/地，该域停止工作。
3. **（可选）保持**：关键寄存器靠 `asic_keeper` 保住电荷，上电后还能恢复。
4. **上电恢复**：先恢复电源，再解除隔离（`iso=0`），电路重新正常驱动总线。

> 物理上，header 用 PMOS 是因为 PMOS 上拉到 VDD 干净（传强 1）；footer 用 NMOS 是因为 NMOS 下拉到 VSS 干净（传强 0）。这是模拟电路「PMOS 传高、NMOS 传低」的常识在电源开关上的体现。

#### 4.4.3 源码精读

**电源开关**——header 用 PMOS（[asic_header.v:8-17](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/asiclib/hdl/asic_header.v#L8-L17)）：

```verilog
module asic_header #(...) (
    input  sleep,   // 1 = disabled vdd
    input  vddin,   // input supply
    output vddout   // gated output supply
    );
   // Primitive Device
   pmos m0 (vddout, vssin, sleep); //d,s,g
```

footer 用 NMOS（[asic_footer.v:8-17](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/asiclib/hdl/asic_footer.v#L8-L17)）：

```verilog
module asic_footer #(...) (
    input  nsleep,  // 0 = disabled ground
    input  vssin,   // input supply
    output vssout   // gated output supply
    );
   nmos m0 (vddout, vddin, nsleep); //d,s,g
```

这两段用了 Verilog 的**开关级原语** `pmos`/`nmos`（`(d, s, g)` 分别是漏、源、栅），属模拟/开关级行为，不可综合——印证了这类单元的 `.v` 只是**带注释的接口占位**，真实实现由 PDK 的版图提供。

> ⚠️ **现实落差**：`asic_header` 的 `pmos` 引用了 `vssin`（模块端口里没有，端口是 `sleep/vddin/vddout`），`asic_footer` 的 `nmos` 引用了 `vddout/vddin`（端口里也没有，端口是 `nsleep/vssin/vssout`）。这是占位桩里的内部不一致，**并非可综合的黄金逻辑**。对电源开关这类物理单元，应以端口名 + 注释（`sleep=1 断 VDD` 等）作为契约，物理实现以 PDK 为准。读源码须留意此类历史遗留。

**隔离单元**用纯组合表达钳位语义——钳高（[asic_isohi.v:8-18](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/asiclib/hdl/asic_isohi.v#L8-L18)）：

```verilog
module asic_isohi #(...) (
    input iso, input in,
    output out );      // out = iso | in
   assign out = iso | in;
```

钳低 `asic_isolo`（[asic_isolo.v:8-18](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/asiclib/hdl/asic_isolo.v#L8-L18)）：`assign out = ~iso & in;`。隔离激活时（`iso=1`）：`isohi` 输出恒 1、`isolo` 输出恒 0，恰好把断电域的输出钳到安全电平；隔离关闭时（`iso=0`）二者都透传 `in`，恢复正常。

**固定电平** `asic_tiehi`（[asic_tiehi.v:7-13](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/asiclib/hdl/asic_tiehi.v#L7-L13)）：`assign z = 1'b1;`；`asic_tielo`：`assign z = 1'b0;`。虽然逻辑上等于直接接电源/地，但真实芯片用专用 tie 单元（而非直连），是为了避免直连带来的驱动不稳和设计规则违例。

**纯物理填充单元**则连逻辑体都没有。`asic_keeper`（[asic_keeper.v:7-11](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/asiclib/hdl/asic_keeper.v#L7-L11)）只有一个 `inout z`、空体；`asic_decap`（[asic_decap.v:7-12](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/asiclib/hdl/asic_decap.v#L7-L12)）只有 `vss/vdd` 电源端口、空体；`asic_antenna`（[asic_antenna.v:7-12](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/asiclib/hdl/asic_antenna.v#L7-L12)）同理。空体正是它们「功能在物理层、不在 RTL」的标志——黄金模型层面只需声明它的存在与端口，行为交由 SPICE/版图描述。

#### 4.4.4 代码实践

**实践目标**：把低功耗单元的逻辑/接口契约整理成一张可操作的「钳位真值表」。

**操作步骤**：

1. 打开 `asic_isohi.v` 和 `asic_isolo.v`，列出 `iso` 与 `in` 在 `00/01/10/11` 四种组合下的 `out`。
2. 用这张表回答：要给一个断电域的低有效复位线做隔离（断电后希望它保持安全值 0），该选 `isohi` 还是 `isolo`？反过来要保持 1 呢？
3. 在 `asiclib/hdl/` 中找出所有「空体单元」（体内无 `assign`/`always`），确认它们都是物理填充类。

**需要观察的现象**：`isohi` 在 `iso=1` 时输出恒 1；`isolo` 在 `iso=1` 时输出恒 0；`iso=0` 时都透传。

**预期结果**：保持安全值 0 用 `asic_isolo`，保持安全值 1 用 `asic_isohi`；空体单元集合为 `{asic_keeper, asic_decap, asic_antenna}`（及任何你发现的其它空体）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 header 用 PMOS、footer 用 NMOS，而不是反过来？

**参考答案**：PMOS 导通时把源漏拉向 VDD（传强 1），适合串在 VDD 路径上做电源开关（header）；NMOS 导通时把源漏拉向 VSS（传强 0），适合串在 VSS 路径上（footer）。反接会导致导通电平弱、压降大，电源开关不合格。

**练习 2**：`asic_decap`、`asic_antenna`、`asic_keeper` 的 `.v` 文件为什么是空体？

**参考答案**：它们的功能是纯物理/模拟的（去耦电容储能、二极管泄放电荷、反馈电路保持电荷），无法用 RTL 逻辑表达。`.v` 只为数字仿真声明端口与存在性，真实行为由 SPICE/版图提供，所以体为空。

---

### 4.5 时序与同步的 hard 单元：与 stdlib 对应

#### 4.5.1 概念说明

`asiclib` 还提供了与 `stdlib` 时序/同步原语一一对应的 hard 单元。理解这条对应关系，就能在 soft↔hard 之间自如切换。本节串起 [u2-l2 时序原语](u2-l2-sequential-flops.md) 和 [u2-l4 跨时钟域同步](u2-l4-cdc-synchronizers.md) 的知识。

关键差别仍是「单位宽 + PROP 参数」：`asic_dffrq` 是 1 比特，`oh_dffrq` 可参数化；但**逻辑语义完全相同**——这正是黄金模型契约。

#### 4.5.2 核心流程

同步器（synchronizer）解决跨时钟域的亚稳态问题，承接 [u2-l4](u2-l4-cdc-synchronizers.md) 的结论：串多级触发器给亚稳态留出塌缩时间。`asic_dsync`（数据同步）与 `asic_rsync`（复位同步）都用 `SYNCPIPE=2` 两级流水，复位同步遵循「异步生效、同步释放」。

#### 4.5.3 源码精读

数据同步器 [asic_dsync.v:8-26](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/asiclib/hdl/asic_dsync.v#L8-L26)：

```verilog
module asic_dsync #(...) (
    input clk, input nreset, input in,
    output out );
   localparam SYNCPIPE=2;
   reg [SYNCPIPE-1:0] sync_pipe;
   always @ (posedge clk or negedge nreset)
     if(!nreset) sync_pipe[SYNCPIPE-1:0] <= 'b0;
     else        sync_pipe[SYNCPIPE-1:0] <= {sync_pipe[SYNCPIPE-1:0],in};
   assign out = sync_pipe[SYNCPIPE-1];
```

复位同步器 [asic_rsync.v:8-24](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/asiclib/hdl/asic_rsync.v#L8-L24) 结构几乎相同，差别在于它移位进来的是常量 `1'b1`：

```verilog
   always @ (posedge clk or negedge nrst_in)
     if(!nrst_in) sync_pipe[SYNCPIPE-1:0] <= 'b0;   // 异步生效：复位立即拉低
     else         sync_pipe[SYNCPIPE-1:0] <= {sync_pipe[SYNCPIPE-2:0],1'b1}; // 同步释放
   assign nrst_out = sync_pipe[SYNCPIPE-1];
```

`nrst_in` 一拉低，`nrst_out` 立刻变 0（异步生效，不等时钟）；`nrst_in` 释放后，`1'b1` 要经过两级触发器才传到 `nrst_out`（同步释放，保证复位撤销时刻与时钟沿对齐，避免部分触发器脱复位、部分还没脱）。

这和 `stdlib` 的 `oh_dsync`/`oh_rsync` 在**逻辑上等价**——soft 版多了 `SYNCPIPE`/`DELAY` 等参数和仿真用的随机延迟注入，但核心是同一条两级移位寄存器。差别只在：hard 版单位宽、不可参数化、带 `PROP`。

时序侧的 [asic_dffrq.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/asiclib/hdl/asic_dffrq.v#L8-L21) 与 `oh_dffrq` 同语义；锁存器 [asic_latq.v:7-17](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/asiclib/hdl/asic_latq.v#L7-L17)（`always @(clk or d) if(clk) q<=d;` 高电平透明）与 `oh_latq` 同语义。

#### 4.5.4 代码实践

**实践目标**：建立 soft↔hard 同步单元的对照表，验证语义等价。

**操作步骤**：

1. 并排打开 `stdlib/rtl/oh_dsync.v` 与 `asiclib/hdl/asic_dsync.v`，比较二者的 `always` 块。
2. 同样比较 `oh_rsync.v` 与 `asic_rsync.v`。
3. 列出 soft 版多出来的东西（参数、仿真延迟注入等），确认核心移位逻辑一致。

**需要观察的现象**：两者都用两级 `sync_pipe`、都异步复位、都从最高位输出；soft 版多了可配置项。

**预期结果**：当 `SYNCPIPE=2` 且不注入仿真延迟时，`asic_dsync` 与 `oh_dsync(in[0])` 的行为位等价——证明 hard 是 soft 单比特情形下的黄金实现。

#### 4.5.5 小练习与答案

**练习 1**：`asic_rsync` 的注释写「async assert, sync deassert」，对应代码里哪两段？

**参考答案**：`async assert` 对应 `negedge nrst_in` 进异步复位、立即 `sync_pipe <= 0`（`nrst_out` 立刻有效）；`sync deassert` 对应 else 分支里把 `1'b1` 经两级移位才送到 `nrst_out`（复位撤销与时钟同步）。

**练习 2**：要用 `asic_dsync` 同步一个 8 位总线，该怎么办？

**参考答案**：例化 8 个 `asic_dsync`，每位各一个——因为 hard 单元是单位宽的。这正是 `stdlib` 参数化（`oh_dsync` 例化 `DW=8`）在 hard 侧的展开形式。

---

## 5. 综合实践

把本讲的三类知识（标准单元命名、ICG、低功耗单元）串起来，完成一个**「门控时钟 + 电源域隔离」的最小 hard 仿真模块**（示例代码，非项目原有文件）：

**任务**：

1. 用 `asic_clkicgand` 给一个由 `asic_dffrq` 构成的 4 位寄存器做门控时钟：当 `en=0` 时寄存器不翻转（省功耗）。
2. 给该寄存器的输出各接一个 `asic_isolo`，用 `iso` 信号控制：模块「下电」时 `iso=1`，输出钳到 0。
3. 写一段激励：先正常写数（`en=1`，观察寄存器翻转），再把 `en` 在 clk 高电平期间拉低（观察门控无毛刺、寄存器冻结），最后拉高 `iso`（观察输出被钳到 0，即使寄存器内部还有值）。
4. 用 `scripts/build.sh` 同款命令（`iverilog -g2005 -y asiclib/hdl`）编译，`gtkwave` 看波形。

**示例骨架**：

```verilog
// 示例代码：综合实践（非仓库文件）
module gated_reg #(parameter PROP="DEFAULT") (
    input clk, input nreset, input en,
    input iso, input [3:0] d,
    output [3:0] q_iso
);
    wire eclk;
    wire [3:0] q;

    asic_clkicgand icg (.clk(clk), .te(1'b0), .en(en), .eclk(eclk));

    genvar i; generate
      for (i=0; i<4; i=i+1) begin : rg
        asic_dffrq ff (.d(d[i]), .clk(eclk), .nreset(nreset), .q(q[i]));
        asic_isolo iso_i (.iso(iso), .in(q[i]), .out(q_iso[i]));
      end
    endgenerate
endmodule
```

**需要观察的现象**：

- `en` 高电平拉低期间，`eclk` 无毛刺、寄存器停止翻转。
- `iso=1` 后 `q_iso` 立刻变 0，与寄存器内部 `q` 解耦。
- `nreset` 拉低时寄存器立即清零（异步复位）。

**预期结果**：一个行为正确、体现 ICG 无毛刺与电源域隔离钳位的小模块。若本地 iverilog 不可用，标注「待本地验证」，并仅凭源码逻辑推导各信号关系。

## 6. 本讲小结

- `asiclib` 是**绑定 PDK 的标准单元黄金模型库**：每个 `.v` 对应一个物理单元，单位宽、只有 `PROP` 参数、无依赖，物理实现须逐字复刻其逻辑（[README](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/asiclib/README.md#L1-L8)）。
- 命名即密码：`ao21`/`aoi22`/`oai22` 用「与/或/非 + 数字」描述复合门；`dffrq`/`sdffrq` 用 `r/s/s/n` 后缀描述复位/置位/扫描/反相变体；`clk*` 是时钟树专用单元。
- **ICG 集成门控时钟**用负电平透明锁存器在 `clk=0` 抓稳使能，使 `eclk=clk & en_stable` 只产生完整脉冲，是消除门控毛刺的标准手段（`asic_clkicgand`）；OR 型 `asic_clkicgor` 是其对偶。
- **低功耗与物理单元**服务于电源域：`asic_header`/`asic_footer`（PMOS/NMOS 电源开关）、`asic_isohi`/`asic_isolo`（钳 1/钳 0 隔离）、`asic_keeper`（保持）、`asic_tiehi`/`asic_tielo`（钳位）、`asic_decap`（去耦）、`asic_antenna`（天线）；其中物理类的 `.v` 多为带注释的接口占位，真实行为在版图/SPICE。
- hard 时序/同步单元（`asic_dffrq`/`asic_sdffrq`/`asic_latq`/`asic_rsync`/`asic_dsync`）与 `stdlib` 原语**逻辑等价**，差别仅在单位宽与 `PROP` 参数；扫描单元（`sdff*`）是 hard 侧独有，服务于制造测试（DFT）。
- **现实落差**：`oh_clockgate` 的 hard 分支例化的 `asic_clockgate` 并不存在，真正的单元是 `asic_clkicgand`；`asic_header`/`asic_footer` 的开关级连线引用了未声明的网络名——均为占位/命名漂移，以源码文本与 PDK 实现为准。

## 7. 下一步学习建议

- 下一讲 [u9-l3 stdcells 晶体管级与 xilibs 仿真模型](u9-l3-transistor-and-xilibs.md) 会下沉到**晶体管级** `.sv` 单元（`oh_nmos`/`oh_pmos`/`oh_nand2`），让你看清 `asiclib` 里 `pmos`/`nmos` 原语背后的真实 CMOS 结构，并讲解 `xilibs` 如何为 FPGA 仿真提供厂商原语模型。
- 之后 [u9-l4 padring 与芯片顶层集成](u9-l4-padring-chip-integration.md) 会把这些单元拼到芯片焊盘环里，看电源域如何在顶层（north/south/east/west）落地——本讲的 `asic_header`/`asic_isolo` 正是 padring 电源管理的积木。
- 建议动手：把 §5 综合实践跑通后，尝试只看文件名批量归类 `asiclib/hdl/` 的全部 110 个单元（组合门 / 时序 / 时钟 / 低功耗 / 物理填充），用本讲的命名规律自检。
