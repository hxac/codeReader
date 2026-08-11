# 时序原语：触发器家族

> 所属单元：u2 stdlib 基础原语 · 依赖前置讲义：[u2-l1 组合逻辑原语](u2-l1-combinational-primitives.md)

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚「触发器（DFF）」和「锁存器（latch）」是什么，以及它们与上一讲组合逻辑的本质区别（**有记忆、有时钟**）。
- 破解 `stdlib` 中触发器家族的**命名密码**：`oh_dffq` / `oh_dffnq` / `oh_dffqn` / `oh_dffrq` / `oh_dffrqn` / `oh_dffsq` / `oh_dffsqn` 里每一个字母代表什么。
- 区分各 DFF 变体的**复位 / 置位语义**：异步低有效、复位值为 0（或反相版的全 1）、默认不复位。
- 理解一个**容易被忽略的真相**：这些原语的 `clk` / `nreset` 端口本身是 `[DW-1:0]` 向量，意味着「每个比特各自带时钟」。
- 读懂 `oh_reg0` / `oh_reg1` 这类**带写使能的寄存器**，并看到它们用「宏切换」与「参数切换」两种不同的 soft/hard 双实现写法。

本讲覆盖两个最小模块：**DFF 家族** 与 **锁存器**。

## 2. 前置知识

### 2.1 从组合逻辑到时序逻辑

上一讲（u2-l1）的所有模块输出都由一句 `assign` 当场算出，**没有记忆**。现实电路里我们常常需要「记住」一个值：计数器要记住当前计数值，CPU 的 PC 要记住下一条指令地址，UART 接收要逐位移入比特。这就需要**时序逻辑（sequential logic）**——输出取决于时钟沿到来时锁存的**历史状态**。

判断一个模块是不是时序逻辑，最直接的信号是：模块里出现了 `always @(posedge clk)` 或真正的寄存器（`reg` 在时钟沿下被赋值）。本讲所有模块都满足这一点。

### 2.2 D 触发器（DFF）的最小模型

D 触发器（D Flip-Flop）是数字电路里最常用的记忆单元，可以把它想象成一个「时钟打点采样器」：

```text
        ┌────────┐
   d ──▶│  DFF   │──▶ q
   clk──▶│(posedge)│
        └────────┘
```

它的行为只有一句话：**每个时钟上升沿，把 `d` 的值搬进 `q`，并在下一个上升沿到来之前保持不变**。用 Verilog 写就是：

```verilog
always @ (posedge clk)
  q <= d;
```

这里用 `<=`（非阻塞赋值）是 OH! 编码规范（u1-l4）的硬性要求——时序逻辑一律用非阻塞赋值，这样才能正确综合成一拍寄存器，而不是组合环。

### 2.3 复位（reset）与置位（set）

裸的 DFF 上电时 `q` 是**不确定**的（仿真里是 `x`，真实芯片里是随机值）。很多时候我们需要一个确定的初始状态，这就需要**复位**或**置位**：

- **复位（reset）**：把 `q` 强制清 0。
- **置位（set / preset）**：把 `q` 强制置 1。

复位/置位又分两种触发方式：

| 类型 | 敏感事件 | 特点 |
| --- | --- | --- |
| **异步（async）** | `posedge clk or negedge nreset` | 复位信号一动作就立刻生效，**不必等时钟**；常用于系统启动。 |
| **同步（sync）** | 只在 `posedge clk` 里判断复位 | 复位只在时钟沿生效，**逻辑更干净**，但时钟没跑就无法复位。 |

OH! 的 `stdlib` 触发器家族**几乎全部采用异步复位**（`nreset`，低有效），所以你会反复看到 `posedge clk or negedge nreset` 这种双敏感沿写法。注意：这里 `n` 前缀（如 `nreset` / `nset`）表示「低有效（active low）」——信号为 0 时触发动作，这是 u1-l4 讲过的命名约定。

### 2.4 命名密码预告

本讲最实用的技能是「看名字就知道这个触发器干什么」。先记住这个公式，后面会逐一验证：

```
oh_dff [n?] [r|s?] q [n?]
        │     │      │
        │     │      └─ 末尾 n = 反相输出（输出 ~d）
        │     └──────── r = 异步复位，s = 异步置位
        └────────────── dff 后紧跟的 n = 下降沿触发时钟
```

也就是说，`n` 出现在**不同位置**含义完全不同：紧跟 `dff` 是「下降沿」，跟在 `r/s` 里是「低有效」，放在末尾 `qn` 是「反相输出」。这是初学者最容易看错的地方，也是本讲的精读重点。

## 3. 本讲源码地图

本讲涉及的关键文件（均在 `stdlib/rtl/` 下）：

| 文件 | 作用 | 关键点 |
| --- | --- | --- |
| `oh_dffq.v` | 最简 D 触发器（无复位） | 全家族的基线，只有 `q<=d` 一句 |
| `oh_dffrq.v` | 异步低有效复位，复位值 0 | `posedge clk or negedge nreset` |
| `oh_dffrqn.v` | 同上但**反相输出**，复位值全 1 | 输出 `qn`，复位写 `{DW{1'b1}}` |
| `oh_dffsq.v` | 异步低有效**置位**，置位值全 1 | 用 `nset`，置位写 `{DW{1'b1}}` |
| `oh_latq.v` | 高电平透明锁存器 | `always_latch` + `if(g)` |
| `oh_reg1.v` | 上升沿、带写使能的寄存器 | soft/hard 双实现（参数切换） |

为帮助理解命名，还会顺带对照这几个同族文件（非主讲，仅作佐证）：`oh_dffnq.v`（下降沿）、`oh_dffqn.v`（反相无复位）、`oh_dffsqn.v`（置位 + 反相）、`oh_latnq.v`（低电平透明锁存器）、`oh_reg0.v`（下降沿寄存器，宏切换）。

> 真实源码现状：这些触发器原语在仓库里**只有定义、没有被任何上层模块按名字例化**（可用 `grep -rn "oh_dffrq" stdlib/rtl/oh_dffrq.v` 之类验证）。它们更像一份「目录式」的标准单元清单：大模块（如 `gpio`、`elink`）往往直接写 `always @(posedge clk)` 内联，而不是去例化 `oh_dffrq`。理解它们的意义在于**建立统一的心智模型和命名规范**，而不是说你到处都会看见它们被调用。

## 4. 核心概念与源码讲解

### 4.1 DFF 家族：从最简触发器到复位/置位变体

#### 4.1.1 概念说明

「DFF 家族」指的是 `oh_dff*` 这一整组文件。它们解决的是同一个核心问题——**在时钟沿采样并保持一个值**——但在三个维度上做不同取舍：

1. **要不要初始值**：默认不复位（最省面积/功耗）vs. 提供异步复位/置位（需要确定初态）。
2. **输出极性**：正常 `q` vs. 反相 `qn`。
3. **时钟沿**：上升沿 vs. 下降沿（`oh_dffnq`）。

为什么默认不复位？因为复位逻辑会额外消耗门电路和布线资源，而且异步复位还伴随「复位释放时需要同步」的隐患。很多**数据通路的流水线**（移位寄存器、延迟线）根本不关心初值——数据流过几拍后自然就把不确定值冲刷掉了。所以 OH! 把**无复位的 `oh_dffq` 作为默认基线**，只在确实需要确定初态时才升级到 `oh_dffrq`（复位）/`oh_dffsq`（置位）。这是一种「能省则省」的设计哲学，呼应 u1-l1 讲的 *Make it simple*。

#### 4.1.2 核心流程

先看一张「命名 → 行为」对照表，这是本节最值得记住的东西：

| 文件名 | 时钟沿 | 控制 | 输出 | 复位/置位时的值 |
| --- | --- | --- | --- | --- |
| `oh_dffq` | 上升沿 | 无 | `q = d` | ——（不确定） |
| `oh_dffnq` | **下降沿** | 无 | `q = d` | —— |
| `oh_dffqn` | 上升沿 | 无 | `qn = ~d`（反相） | —— |
| `oh_dffrq` | 上升沿 | `nreset` 复位 | `q` | `q = 0` |
| `oh_dffrqn` | 上升沿 | `nreset` 复位 | `qn = ~d` | `qn = 全1` |
| `oh_dffsq` | 上升沿 | `nset` 置位 | `q` | `q = 全1` |
| `oh_dffsqn` | 上升沿 | `nset` 置位 | `qn = ~d` | `qn = 0` |

把这张表的规律提炼成两条：

- **复位/置位的「目标值」总是和输出极性自洽**：复位（reset）的语义是「归零」，但对反相输出 `qn` 而言，「零」对应 `qn` 全 1。于是 `oh_dffrq` 复位到 `0`，而 `oh_dffrqn` 复位到 `{DW{1'b1}}`。置位（set）同理对称：`oh_dffsq` 置位到全 1，`oh_dffsqn` 置位到 `0`。
- **下降沿只有无复位那一款**：库里 `n`（下降沿）只出现在 `oh_dffnq`，没有 `oh_dffnrq` 之类的组合。需要下降沿 + 复位时，目前要自己组合，不能想当然地以为文件存在。

每个 DFF 的运行流程都极简，伪代码统一是：

```text
在 (posedge clk) 或 (复位/置位的有效沿):
  if (复位/置位生效):
      q <= 该极性下的「归零/置一」值
  else:
      q <= d   （反相版为 ~d）
```

#### 4.1.3 源码精读

**① 基线：`oh_dffq`（无复位上升沿 DFF）**

[stdlib/rtl/oh_dffq.v:8-18](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_dffq.v#L8-L18)：定义了全家族最简形态。注意端口声明里 `d`、`clk`、`q` **全部是 `[DW-1:0]` 向量**，连 `clk` 都不例外。

核心只有一句（[oh_dffq.v:15-16](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_dffq.v#L15-L16)）：

```verilog
always @ (posedge clk)
  q <= d;
```

这里有一个**反直觉但极其重要**的设计：`clk` 是 `[DW-1:0]` 向量。在 Verilog 里 `always @(posedge clk)` 对向量 `clk` 的含义是「**任意一比特出现上升沿就触发**」，于是 `oh_dffq #(.DW(8))` 实际例化出 **8 个互相独立的 1 比特触发器，每个比特各自有自己的时钟**。这不是「一个 8 位寄存器」，而是「8 个单比特寄存器打包」。这一点会直接决定后面实践任务怎么接线，务必先记住。

**② 异步复位版：`oh_dffrq`（复位值 0）**

[stdlib/rtl/oh_dffrq.v:9-23](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_dffrq.v#L9-L23)：在基线上加了一个异步低有效复位端口 `nreset`（同样 `[DW-1:0]`）。

精读敏感列表与复位分支（[oh_dffrq.v:17-21](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_dffrq.v#L17-L21)）：

```verilog
always @ (posedge clk or negedge nreset)
  if(!nreset)
    q <= 'b0;
  else
    q <= d;
```

两点解读：

- `posedge clk or negedge nreset` 是异步复位的标志写法：`nreset` 出现在敏感列表里，意味着它**不需要等时钟**，一旦拉低（下降沿）立刻把 `q` 清零。这是「异步」二字的实现根源。
- `if(!nreset)` 把「低有效」翻译成代码：`nreset==0` 时复位，复位值是 `'b0`。

**③ 反相复位版：`oh_dffrqn`（复位值全 1）**

[stdlib/rtl/oh_dffrqn.v:8-22](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_dffrqn.v#L8-L22)：名字末尾的 `n` 表示反相输出 `qn`。

看复位值是如何随极性翻转的（[oh_dffrqn.v:16-20](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_dffrqn.v#L16-L20)）：

```verilog
always @ (posedge clk or negedge nreset)
  if(!nreset)
    qn <= {DW{1'b1}};   // 复位到全 1，而不是 0
  else
    qn <= ~d;           // 正常输出反相
```

`{DW{1'b1}}` 是 u2-l1 讲过的「复制拼接」模式：把 `1'b1` 复制 `DW` 份拼成 `DW` 位全 1。这里完美印证了 4.1.2 的规律——反相输出的「归零」就是全 1。

**④ 异步置位版：`oh_dffsq`（置位值全 1）**

[stdlib/rtl/oh_dffsq.v:8-22](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_dffsq.v#L8-L22)：把 `r`（reset）换成 `s`（set），把端口名换成 `nset`。

[oh_dffsq.v:16-20](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_dffsq.v#L16-L20)：

```verilog
always @ (posedge clk or negedge nset)
  if(!nset)
    q <= {DW{1'b1}};   // 置位到全 1
  else
    q <= d;
```

置位与复位在结构上**完全对称**，只是初始值相反、端口名不同。理解了一个，另一个就是镜像。

#### 4.1.4 代码实践

**实践目标**：亲手用 `oh_dffrqn` 搭一个「带异步低有效复位的 8 位寄存器」，再写一份等价的内联 `always` 版本对比，体会「例化原语」与「直接写 always」的差别，并理解 `clk` 是向量这一坑点。

**操作步骤**

第 1 步：注意端口宽度陷阱。`oh_dffrqn` 的 `clk` 与 `nreset` 都是 `[DW-1:0]`。当你设 `.DW(8)` 时，它们各占 8 位。要把「1 位系统时钟」接到「8 位时钟端口」上，必须**把同一个时钟广播到 8 位**，复位同理。下面是**示例代码**（非项目原有代码，请自己新建文件验证）：

```verilog
// 示例代码：my_reg8.v —— 用 oh_dffrqn 例化一个 8 位寄存器
module my_reg8 (
   input        clk,
   input        nreset,
   input  [7:0] din,
   output [7:0] dout_n,   // oh_dffrqn 给出的是反相输出
   output [7:0] dout      // 再反相一次，得到正常的 8 位寄存器输出
);
   // 把 1 位 clk / nreset 广播到 8 位端口
   wire [7:0] clk_w   = {8{clk}};
   wire [7:0] nreset_w = {8{nreset}};

   oh_dffrqn #(.DW(8)) u_reg (
      .d     (din),
      .clk   (clk_w),
      .nreset(nreset_w),
      .qn    (dout_n)
   );

   assign dout = ~dout_n;   // 反相回来 = 正常寄存器输出
endmodule
```

> 你也可以直接选 `oh_dffrq`（非反相、复位值 0），那样 `q` 就是要的输出，省掉最后一句 `~`。本实践刻意用 `oh_dffrqn`，是为了让你切身感受「名字末尾的 n」带来的极性差异。

第 2 步：写一份**等价的内联 always 版本**对比（同样是示例代码）：

```verilog
// 示例代码：my_reg8_inline.v —— 直接写 always，不例化原语
module my_reg8_inline (
   input        clk,
   input        nreset,
   input  [7:0] din,
   output reg [7:0] dout
);
   always @ (posedge clk or negedge nreset)
      if (!nreset) dout <= 8'b0;
      else         dout <= din;
endmodule
```

第 3 步（可选）：写一个最小 testbench 用 iverilog 验证（示例代码）。注意 `stdlib/dv/` 当前**没有**这些触发器的现成测试，所以需要你自己写：

```verilog
// 示例代码：tb_my_reg8.v
`timescale 1ns/1ps
module tb_my_reg8;
   reg        clk = 0, nreset = 0;
   reg  [7:0] din = 0;
   wire [7:0] dout, dout_n;
   my_reg8 dut (.clk(clk), .nreset(nreset), .din(din),
                 .dout(dout), .dout_n(dout_n));

   always #5 clk = ~clk;          // 10ns 周期
   initial begin
      nreset = 0; din = 8'hAB;    // 复位期间给一个非零值
      #12 nreset = 1;             // 释放复位
      #10 din = 8'h3C;            // 改输入
      #10 din = 8'hFF;
      #10 $finish;
   end
   initial $dumpvars(0, tb_my_reg8);
endmodule
```

编译运行（前提：把 `oh_dffrqn.v`、`my_reg8.v`、`tb_my_reg8.v` 放同目录，参考 u1-l3 的 iverilog 用法）：

```bash
iverilog -g2005 -o sim my_reg8.v oh_dffrqn.v tb_my_reg8.v
vvp sim
```

**需要观察的现象**

1. **复位阶段**：`nreset=0` 期间，`dout` 是多少？由于 `oh_dffrqn` 复位到全 1，`dout_n` 应为 `8'hFF`，`dout`（再取反）应为 `8'h00`——**与输入 `din` 无关**，这就是「确定初态」。
2. **释放复位后的第一拍**：`nreset` 拉高后，要等下一个 `posedge clk`，`dout` 才采样 `din`。
3. **反相关系**：任意时刻 `dout == ~dout_n` 是否成立？

**预期结果**

- 复位期间 `dout == 8'h00`、`dout_n == 8'hFF`。
- `nreset` 释放后，每个上升沿 `dout` 更新为上一拍的 `din`（注意非阻塞赋值带来的「一拍延迟」）。
- `dout == ~dout_n` 始终成立。

> 如果本地没装 iverilog，或对时序不确定，请标注「待本地验证」并先在纸上按事件推演一遍。**不要假装已经跑过命令**。

**例化 vs 内联的对比小结**

| 维度 | 例化 `oh_dffrqn` | 内联 `always` |
| --- | --- | --- |
| 复位值 | 全 1（`qn`），需手动 `~` 还原 | 自由设定（这里是 0） |
| 端口宽度坑 | `clk`/`nreset` 是向量，需 `{8{...}}` 广播 | 单 bit，无此问题 |
| 可读性 | 命名即语义，但极性需查手册 | 行为一目了然 |
| 与 ASIC 流程 | 未来可替换为 hard 单元 | 需综合工具自行映射 |

关键教训：**例化原语前必须先读它的复位值与输出极性**，不能凭名字想当然——`oh_dffrqn` 复位到全 1，与你脑子里的「复位清零」直觉相反。

#### 4.1.5 小练习与答案

**练习 1**：如果要把一个 4 位计数器的状态寄存器初始化为 `4'b0`，应该选 `oh_dffq`、`oh_dffrq`、`oh_dffsq` 中的哪一个？为什么？

> **参考答案**：选 `oh_dffrq`。它带异步低有效复位且复位值为 0，正好满足「上电清零」。`oh_dffq` 没有复位（初态不确定），`oh_dffsq` 是置位（初态为全 1），都不符合「清零」需求。

**练习 2**：`oh_dffsqn` 复位/置位时输出 `qn` 是 0 还是全 1？请先按 4.1.2 的规律推导，再去 [源码](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_dffsqn.v#L16-L20) 核对。

> **参考答案**：`oh_dffsqn` = 置位（`s`）+ 反相输出（末尾 `n`）。置位的目标是「让正常输出 `q` 为全 1」，而 `qn` 是 `q` 的反相，所以 `qn` 复位/置位到 `0`。源码 `qn <= 'b0` 印证了这一点。

**练习 3**：为什么 `oh_dffrq` 的 `always` 敏感列表里要写 `negedge nreset`，而不是只在 `posedge clk` 里判断 `nreset`？两者分别对应什么复位类型？

> **参考答案**：把 `negedge nreset` 写进敏感列表，意味着 `nreset` 一旦出现下降沿就立即触发进程，不必等待时钟——这是**异步复位**。如果只在 `posedge clk` 内部判断 `if(!nreset)`，则复位动作只有在时钟沿才会发生——那是**同步复位**。OH! 的 DFF 家族统一采用异步复位。

### 4.2 锁存器与带使能的寄存器

#### 4.2.1 概念说明

除了边沿触发的 DFF，`stdlib` 还提供两类时序单元：

- **锁存器（latch）**：电平敏感（不是边沿敏感）。当使能信号有效时「透明」——输出实时跟随输入；使能无效时「锁住」上一次的值。优点是面积小、功耗低；缺点是容易产生组合环和时序分析困难，**现代设计一般慎用**，但理解它对读老代码、读 ASIC 库很重要。
- **带写使能的寄存器（`oh_reg0` / `oh_reg1`）**：在普通 DFF 之上加一个 `en` 使能——只有 `en=1` 时才在时钟沿更新，否则保持。这正是 CPU 里「带写使能的寄存器堆」、外设里「只在写选通时更新的配置寄存器」的典型形态。

这两个模块还各自展示了 OH! 的另一面：**soft/hard 双实现的不同切换写法**（参数切换 vs. 宏切换），呼应 u1-l4。

#### 4.2.2 核心流程

**锁存器的行为**（以高电平透明锁存器为例）：

```text
当 g == 1（透明）: q <= d        // 输出实时跟随输入
当 g == 0（锁存）: q 保持不变    // 锁定 g 下降沿前最后的 d
```

**带使能寄存器的行为**（`oh_reg1`，上升沿）：

```text
在 (posedge clk) 或 (negedge nreset):
  if (nreset == 0): out <= 0          // 异步复位
  else if (en):     out <= in         // 只有使能时才更新
  else:             out 保持不变      // 否则锁定
```

注意 `else if(en)` 之外什么也不写，`out_reg` 自然保持——这是实现「写使能」的标准技巧。

#### 4.2.3 源码精读

**① 高电平透明锁存器：`oh_latq`**

[stdlib/rtl/oh_latq.v:8-19](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_latq.v#L8-L19)：用 `always_latch` 实现一个最简透明锁存器。

[oh_latq.v:15-17](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_latq.v#L15-L17)：

```verilog
always_latch
  if(g)
    q <= d;
```

三个看点：

- `always_latch` 是 Verilog 2001 起的关键字（Verilog 2005 沿用），等价于 `always @*` 但**明确告诉综合器「我就是要一个锁存器」**，避免它报 warning。
- `if(g) q <= d;` 没有 `else` 分支——正是这种「条件不完整」的赋值，让综合器推断出锁存器（`g==0` 时保持）。
- 端口 `g` 同样是 `[DW-1:0]` 向量，沿用家族的「每比特独立」设计。对应的低电平透明版见 [oh_latnq.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_latnq.v#L15-L17)，把 `if(g)` 换成 `if(!gn)`。

**② 带写使能的上升沿寄存器：`oh_reg1`**

[stdlib/rtl/oh_reg1.v:8-45](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_reg1.v#L8-L45)：是本节最有「工程味」的文件，因为它把 soft/hard 双实现、写使能、参数化三件事揉在一起。

先看 soft 分支（[oh_reg1.v:22-30](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_reg1.v#L22-L30)）：

```verilog
if(SYN == "TRUE") begin
   reg [N-1:0] out_reg;
   always @ (posedge clk or negedge nreset)
     if(!nreset)        out_reg[N-1:0] <= 'b0;
     else if(en)        out_reg[N-1:0] <= in[N-1:0];
   assign out[N-1:0] = out_reg[N-1:0];
end
```

- 参数 `SYN` 是字符串（`"TRUE"` / 其他），用 `generate if(SYN=="TRUE")` 在**编译期**选择 soft 还是 hard——这是 u1-l4 讲的「参数切换」机制。对比 `oh_dffq` 这类「纯 soft」文件，`oh_reg1` 留好了 ASIC 替换口子。
- `else if(en)` 实现「写使能」：`en=0` 时 `out_reg` 不赋值即保持。
- 内部用内部 `reg out_reg` + `assign out = out_reg`，把 `out` 做成 `wire` 输出（不能直接给 `output reg` 又同时在 hard 分支用 `assign`，所以拆开）。

再看 hard 分支（[oh_reg1.v:31-43](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_reg1.v#L31-L43)）：逐比特例化 `asic_reg1`，把 soft 的 `always` 换成硬核单元。这正是 u9-l1 将深入讲的「hard 实现」雏形。

**③ 带写使能的下降沿寄存器：`oh_reg0`（附带一个真实坑）**

[stdlib/rtl/oh_reg0.v:8-30](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_reg0.v#L8-L30)：与 `oh_reg1` 对称，但是下降沿，并且切换 soft/hard 的方式不同。

[oh_reg0.v:15-28](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_reg0.v#L15-L28)：

```verilog
`ifdef CFG_ASIC
   asic_reg0 ireg [N-1:0] (...);     // hard：宏切换
`else
   always @ (negedge clk or negedge nreset) ...  // soft
`endif
```

两个值得留意的**真实源码现状**（读源码要诚实）：

1. **模块名与文件名不一致**：文件叫 `oh_reg0.v`，但 [第 8 行](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_reg0.v#L8) 声明的模块名是 `ohr_reg0`（多了个 `r`）。这违反了 u1-l4 讲的「一文件一模块、名字与文件对应」约定。用 `iverilog -y` 按文件名查找时这种不一致会坑到你，遇到时以模块名为准。
2. **切换机制不同**：`oh_reg1` 用参数 `SYN`，`oh_reg0` 用宏 `CFG_ASIC`（对应 u1-l3 讲的 `iverilog -DCFG_ASIC=...`）。同一家族里两种写法并存，说明 OH! 的规范在演化中，读代码时要看清楚当前文件用的是哪一种。

#### 4.2.4 代码实践

**实践目标**：通过阅读和改参数，直观感受「锁存器（电平敏感）」与「寄存器（边沿敏感 + 写使能）」在行为上的差别。

**操作步骤**

1. 打开 [oh_latq.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_latq.v#L15-L17) 与 [oh_reg1.v 的 soft 分支](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_reg1.v#L22-L30)，并排对照。
2. 在纸上分别画出 `g` / `en` 保持为 1 期间、以及从 1→0 之后，输出对输入的响应。
3. （可选）写一个最小 testbench，把同一个 `d`/`in` 和同一个方波使能分别接到 `oh_latq` 与 `oh_reg1`，用 gtkwave 看波形差异。

**需要观察的现象**

- **锁存器**：`g=1` 期间，`q` **实时跟随** `d`（哪怕没有时钟沿）；`g` 一变 0，`q` 锁死在 `d` 最后一次的值。
- **寄存器**：`en=1` 时，`out` 也**只在时钟上升沿**更新一次；`en=0` 时即使时钟继续翻转，`out` 也纹丝不动。

**预期结果**：在 `g`/`en` 都为 1 的同一个时钟周期内，锁存器的 `q` 会随 `d` 的中间变化而抖动，而寄存器的 `out` 只在该周期上升沿采样一次、周期内保持稳定。这正是「电平敏感 vs 边沿敏感」的本质区别，也是工程上**优先用寄存器、慎用锁存器**的原因。

> 若不做仿真，请标注「待本地验证」，并把上述现象用文字+时序草图推演一遍。

#### 4.2.5 小练习与答案

**练习 1**：`oh_latq` 里为什么没有 `else` 分支？删掉 `always_latch` 换成 `always @(*)` 会改变行为吗？

> **参考答案**：没有 `else` 意味着 `g==0` 时不对 `q` 赋值，综合器因此推断「保持」——也就是锁存器。换成 `always @(*)` 行为等价（同样会推断出锁存器），但 `always_latch` 更明确地表达了设计意图，并让 lint 工具知道「这是有意为之」，从而不报「意外生成锁存器」的告警。

**练习 2**：`oh_reg1` 的 soft 分支里，`out` 为什么不直接声明成 `output reg`，而要拆成内部 `out_reg` + `assign out`？

> **参考答案**：因为 `out` 在 hard 分支里是用 `asic_reg1 ... .out(out[i])` 例化驱动的，需要它是 `wire`；而在 soft 分支里它由 `assign out = out_reg` 驱动，同样是 `wire`。把状态保存在内部 `out_reg`、对外统一用 `wire out`，可以让两个 `generate` 分支的对外接口一致、避免类型冲突。

**练习 3**：仓库里 `oh_reg0.v` 的模块名实际叫什么？它用哪种方式（参数还是宏）切换 soft/hard？

> **参考答案**：模块名实际是 `ohr_reg0`（文件名 `oh_reg0`，二者不一致）。它用宏 `` `ifdef CFG_ASIC`` 切换，而 `oh_reg1` 用参数 `SYN=="TRUE"` 切换——同家族两种写法并存。

## 5. 综合实践

把本讲知识串起来，设计一个「带异步复位的 4 级移位寄存器」：

- 输入 `din`（1 位）、`clk`（1 位）、`nreset`（1 位）。
- 输出 `dout`（1 位，第 4 级的值）。
- 要求：**只用 `stdlib` 原语例化**实现（不写 `always`），并在复位时各级清零。

提示与要求：

1. 选哪个原语作为每一级？为什么不能选 `oh_dffq`？（答：需要确定初态，应选 `oh_dffrq`，复位值 0。）
2. 由于 `oh_dffrq` 的 `clk`/`nreset` 是 `[DW-1:0]`，单级用 `.DW(1)` 时，`clk` 端口是 1 位，可以直接连 1 位的系统时钟，无需广播。请据此画出 4 级级联的例化图。
3. 写完后，把你的例化版与「一个内联 `always` + 4 位移位」版本对比代码行数和可读性。
4. （可选）用 u1-l3 的 iverilog 流程跑一遍，观察复位期间 `dout` 是否立刻为 0（不等时钟），以及释放复位后数据是否逐拍右移。

> 这是一个把「命名密码 + 复位语义 + 向量时钟端口 + 例化 vs 内联」四件事一次打通的任务。完成它，你就真正掌握了 `stdlib` 的 DFF 家族。

## 6. 本讲小结

- `stdlib` 的 DFF 家族用**位置命名的字母**编码行为：`dff` 后的 `n` = 下降沿，`r`/`s` = 异步复位/置位，末尾 `n` = 反相输出，端口前缀 `n`（如 `nreset`）= 低有效。
- **默认不复位**（`oh_dffq`）是基线，复位会增面积、引出同步隐患；需要确定初态才升级到 `oh_dffrq`（复位值 0）/`oh_dffsq`（置位值全 1）。
- **复位值随输出极性自洽**：反相版 `oh_dffrqn` 复位到全 1，置位反相版 `oh_dffsqn` 置位到 0——不能凭直觉，要查源码。
- 一个关键设计细节：`d`/`clk`/`nreset`/`q` **都是 `[DW-1:0]` 向量**，意味着例化出的是「每比特各自带时钟」的一组独立触发器，接线时要广播时钟/复位。
- **锁存器**（`oh_latq`，电平敏感，`always_latch`）与**带使能寄存器**（`oh_reg1`，边沿敏感 + `en`）是两类不同的时序单元；`oh_reg0`/`oh_reg1` 还展示了宏切换 vs 参数切换两种 soft/hard 双实现写法。
- 读源码要诚实：`oh_reg0.v` 里模块名实际是 `ohr_reg0`，文件名与模块名不一致是真实存在的坑。

## 7. 下一步学习建议

- **下一讲 u2-l3（时钟控制原语）**：会用到本讲的 DFF 去搭门控时钟、分频器、时钟切换，重点是「毛刺」与无毛刺切换，建议先把 DFF 家族记牢。
- **后续 u2-l4（跨时钟域同步）**：`oh_dsync` / `oh_rsync` 本质就是「把本讲的 DFF 串成 N 级」，理解了 `oh_dffrq` 的异步复位，同步器的复位行为就一目了然。
- **延伸阅读**：想了解 hard 一侧的对应物，可先扫一眼 `asiclib/hdl/asic_dffq.v`（u9-l1 会精读），对照体会「soft 的 `always` 如何被 hard 的标准单元替换」。
