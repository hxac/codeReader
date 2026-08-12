# 双实现策略：soft vs hard

## 1. 本讲目标

本讲是第 9 单元（ASIC 实现、物理设计与工程规范）的第一讲，回答一个贯穿 OH! 全库的核心架构问题：**同一个功能，为什么有两套实现？它们怎么切换？**

学完后你应该能够：

- 说清 **soft（可综合 RTL）** 与 **hard（绑定工艺库的硬核/标准单元）** 两套实现各自的来源、动机与代价。
- 读懂 OH! 切换这两套实现的两种机制：`SYN/TYPE/SHAPE` 字符串参数 + `generate if`，以及 `CFG_ASIC` 编译期宏 + `` `ifdef ``。
- 解释 `scripts/build.sh` 里 `-DCFG_ASIC=0` 的真实含义，以及它和 `` `ifdef CFG_ASIC `` 之间的一个经典 Verilog「坑」。
- 理解为什么 OH! 的**设计文件不含 `timescale`、不含延时语句**——这直接服务于 soft/hard 可替换性。
- 识别 hard 实现里多出来的 BIST / 电源 / 修复类端口，并说明它们的物理用途。

本讲建立在 u2-l2（触发器家族）和 u3-l1（双口 RAM / 寄存器堆）之上：你已经认识 `oh_dffq`、`oh_dpram`、`oh_fifo_sync` 这些 soft 原语，现在我们要把它们翻到「背面」，看 ASIC 流程里对应的那一面。

---

## 2. 前置知识

在进入源码前，先用通俗语言把几个概念讲透。

**RTL（Register Transfer Level，寄存器传输级）**
用 `always`、`assign`、`if`、`case` 这类「行为描述」写电路。综合工具（如 Yosys、Design Compiler）能把它翻译成具体的逻辑门。OH! 里 `stdlib` 的 `.v` 文件几乎都是 RTL。

**ASIC（Application-Specific Integrated Circuit，专用集成电路）**
为某个用途专门流片的芯片。和 FPGA（可现场编程的通用芯片）相对。OH! 的目标是「同一份设计既能跑 FPGA 仿真、又能流片成 ASIC」。

**PDK（Process Design Kit，工艺设计包）**
晶圆厂（foundry，如台积电、联电）给你的「特定工艺（如 28nm）的元件库 + 设计规则」。PDK 里才有真正的标准单元（带真实延时、面积、漏电参数）。换一家厂、换一个工艺节点，PDK 就换了。

**标准单元（standard cell）**
PDK 提供的、版图固定好的基本积木，如 `DFFQX1`（一个 D 触发器）、`AND2X1`（两输入与门）。它们是「黑盒成品」，设计师只负责调用，不改内部。

**综合（synthesis）**
把 RTL 翻译成标准单元网表的过程。「可综合（synthesizable）」就是「综合工具吃得下去」——这是 OH! Coding Guide 的硬约束。

**soft 与 hard 的直觉类比**
把功能想象成「一堵墙」：

- **soft** = 用砖块（`always`/`assign`）现场砌出来的墙。任何综合工具、任何工艺都能砌，灵活但质量和面积由综合工具决定。
- **hard** = 厂家（foundry）按特定 PDK 预制好的成品墙板。又快又省面积、性能最优，但绑定具体工艺，换工艺就得换。

OH! 的核心取舍是：**同一个功能同时保留这两堵墙，用参数在编译期二选一。**

---

## 3. 本讲源码地图

本讲涉及的关键文件，分两组对照阅读：

| 文件 | 作用 | 本讲用来讲 |
|------|------|-----------|
| [stdlib/rtl/oh_dffq.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_dffq.v) | soft：参数化无复位 D 触发器 | soft RTL 长什么样 |
| [asiclib/hdl/asic_dffq.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/asiclib/hdl/asic_dffq.v) | hard：同功能的黄金模型 | hard 单元长什么样 |
| [asiclib/hdl/asic_dffrq.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/asiclib/hdl/asic_dffrq.v) | hard：带复位的 D 触发器黄金模型 | hard 家族命名一致性 |
| [asiclib/README.md](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/asiclib/README.md) | asiclib 定位说明 | hard 库的设计契约 |
| [stdlib/rtl/oh_clockgate.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_clockgate.v) | 门控时钟，含 `SYN` 切换 | 字符串参数切换机制（最干净样例） |
| [stdlib/rtl/oh_dpram.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_dpram.v) | 双口 RAM，含 `TARGET`/`SHAPE` 切换 | 存储宏的参数切换 |
| [stdlib/rtl/oh_fifo_sync.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_fifo_sync.v) | 同步 FIFO，含 BIST/power/repair 端口 | hard 多出来的物理端口 |
| [stdlib/rtl/oh_reg0.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_reg0.v) | 采样寄存器，用 `` `ifdef CFG_ASIC `` 切换 | 宏切换机制 |
| [scripts/build.sh](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/scripts/build.sh) | 仿真编译脚本 | `-DCFG_ASIC=0` 的真实效果 |
| [README.md](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/README.md) | 项目设计/编码规范 | 「无 timescale、无延时、只用可综合结构」三条规矩 |

> 阅读提醒（贯穿全手册的原则）：**代码是事实，README/脚本可能滞后。** 本讲会指出几处脚本与 RTL 不一致的地方，一律以 RTL 文本为准。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**soft RTL**、**hard 单元**、**参数切换**。

### 4.1 soft RTL：stdlib 的可综合模型

#### 4.1.1 概念说明

soft 实现就是「用综合工具吃得下去的 RTL 写出来的功能」。它的特点是：

- **参数化**：位宽、深度等用 `#(parameter ...)` 暴露，一份代码顶任意规模。
- **可综合**：只用工具认识的关键字（`assign`、`always`、`if`、`case`、`generate`、`for`…），不写 `#延时`、不写 `initial`（设计文件里）。
- **工艺无关**：不依赖任何 PDK，换厂换节点都不用改。

OH! 把所有 soft 原语集中放在 `stdlib/rtl/`。`oh_dffq`（无复位 D 触发器）是其中最简单、也是 u2-l2 已学过的样板。

#### 4.1.2 核心流程

一个 soft 时序原语的「流程」其实就是综合器怎么理解它：

1. 解析 `always @(posedge clk) q <= d;`
2. 识别出「上升沿触发的寄存器」语义
3. 在目标工艺里映射成若干个 D 触发器标准单元（每个比特一个）
4. 位宽 `DW` 决定映射出几个

也就是说，**soft RTL 自己不是物理元件，它是「待综合的描述」**。真正变成晶体管是综合工具的事。

#### 4.1.3 源码精读

`oh_dffq` 的全部实现只有几行，但信息量很大：

[stdlib/rtl/oh_dffq.v:8-18](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_dffq.v#L8-L18) —— soft 的参数化无复位 D 触发器。注意三点：① 端口 `d/clk/q` 都是 `[DW-1:0]` 向量，参数 `DW` 决定宽度；② 没有复位端口（默认基线，省面积/功耗，见 u2-l2）；③ 没有任何 `timescale`、没有任何 `#` 延时。

```verilog
module oh_dffq #(parameter DW = 1) // array width
   (
    input [DW-1:0]     d,
    input [DW-1:0]     clk,
    output reg [DW-1:0] q
    );
   always @ (posedge clk)
     q <= d;
endmodule
```

这里 `clk` 也是 `[DW-1:0]`——它例化出的是「每个比特各自带一个时钟脚」的一组独立触发器，接线时必须把时钟广播到每一比特（见 u2-l2 的提醒）。

「无 timescale、无延时」不是 `oh_dffq` 一家的事，而是全库规矩。我们用搜索验证：`stdlib/rtl/` 与 `asiclib/hdl/` 两个设计目录里 **`timescale` 出现次数为 0**。规矩写在 README 的 Coding Guide：

[README.md:140-141](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/README.md#L140-L141) —— 「No timescales in design files (only in testbench)」「No delay statements in design」。

[README.md:159](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/README.md#L159) —— 「Only use synthesizable constructs」。紧接其后的第 162 行还列出了一份允许关键字白名单（`assign, always, ... generate, for(...), posedge, negedge, $signed` 等）。

> 为什么设计文件不许写 `timescale` 和延时？因为延时是「某个 PDK 在某个工艺角下的物理事实」，写在工艺无关的 soft 里就会污染 soft/hard 可替换性——你一旦把 `#2ns` 写进 RTL，换工艺、换工具、换仿真器都会失真。延时只允许出现在 testbench 里（那里是「观察」电路，不是「定义」电路）。

#### 4.1.4 代码实践

**实践目标**：亲手确认「soft RTL 是工艺无关的描述」。

**操作步骤**：

1. 打开 [stdlib/rtl/oh_dffq.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_dffq.v)，确认它不含 `timescale`、不含 `#`、不引用任何 `asic_*` 单元。
2. 用搜索工具在整个 `stdlib/rtl/` 目录搜 `timescale`（例如 `Grep` pattern `timescale`，path `stdlib/rtl`）。
3. 再在 `asiclib/hdl/` 搜一次。

**需要观察的现象**：两处都返回 0 匹配。

**预期结果**：验证「设计文件无 timescale」是全库统一遵守的硬规矩，而非个别文件的习惯。

> 若无法运行搜索，明确标注「待本地验证」；但本讲写作时已执行该搜索，结果为 0。

#### 4.1.5 小练习与答案

**练习 1**：`oh_dffq` 没有 `nreset` 端口，复位时 `q` 会是什么值？
**答案**：不确定（X，未知态）。这正是 u2-l2 讲的「默认基线省复位」——需要确定初态时应改用 `oh_dffrq`（复位到 0）或 `oh_dffsq`（置位到 1）。

**练习 2**：如果把 `DW` 设为 8，综合后会得到几个 D 触发器？
**答案**：8 个（每个比特一个独立触发器），共用同一套时钟/数据语义。

---

### 4.2 hard 单元：asiclib 的黄金模型与 PDK 绑定

#### 4.2.1 概念说明

hard 实现是「绑定特定 PDK 的物理单元」。在 OH! 里，`asiclib/hdl/*.v` 是这些物理单元的 **golden model（黄金模型）**——一份用 RTL 写的、描述该单元**逻辑功能**的参考实现。真正的物理实现由 PDK/晶圆厂提供，`asiclib` 的职责是：**黄金模型定义「正确的逻辑行为」，任何 hard-coded 实现都必须逐比特复刻它**。

这一点写在 asiclib 的 README 里，是 hard 库的设计契约：

[asiclib/README.md:4-7](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/asiclib/README.md#L4-L7) —— 四条契约：① 「low level asic cells hard-coded to a specific PDK」；② 「golden model … A hard coded implementation must implement the logical functionality exactly」（黄金模型，物理实现必须逐字复刻逻辑）；③ 「linked in at compile time based on the foundry」（按晶圆厂在编译期链接）；④ 「The cells do not have any dependancies」（单元之间无依赖，可独立替换）。

注意第 ④ 条「无依赖」非常关键：hard 单元不互相调用、不 `include` 别人，这让 PDK 可以逐个单元替换而不牵一发动全身。

#### 4.2.2 核心流程

asiclib 里 hard 单元的命名与 stdlib 一一对应（`oh_dffq` ↔ `asic_dffq`、`oh_dffrq` ↔ `asic_dffrq`），但有两点本质区别：

1. **固定单位宽**：hard 单元是「标准单元」，物理上一个单元就是 1 比特（或固定宽度），所以**没有 `DW` 参数**。要做 8 比特寄存器，就得例化 8 个 `asic_dffq`。
2. **PROP 参数代替 DW**：所有 hard 单元都带 `#(parameter PROP = "DEFAULT")`。`PROP` 不是位宽，而是**工艺属性**（给 PDK 传递驱动强度/阈值/延时角等元信息），soft 单元没有这个东西。

切换流程（编译期）：上层模块例化时若选择 hard 分支，就把 `oh_dffq` 换成 `asic_dffq`；综合/流片时，再用 PDK 里真正的物理单元网表替换掉 `asic_dffq` 这个「占位+黄金模型」。

#### 4.2.3 源码精读

`asic_dffq` 与 `oh_dffq` 的功能完全相同（上升沿采样），但形态是 hard 单元的样子：

[asiclib/hdl/asic_dffq.v:7-16](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/asiclib/hdl/asic_dffq.v#L7-L16) —— hard 的 D 触发器黄金模型。对比 `oh_dffq`：① 参数是 `PROP`（工艺属性），不是 `DW`；② `d/clk/q` 都是单比特，不是向量；③ `always` 逻辑一字不差。

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

带复位的版本 `asic_dffrq` 同样遵循这套形态，命名也和 `oh_dffrq` 对齐：

[asiclib/hdl/asic_dffrq.v:8-21](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/asiclib/hdl/asic_dffrq.v#L8-L21) —— hard 带异步低有效复位的 D 触发器。注意复位值写死为 `1'b0`（`if(!nreset) q <= 1'b0;`），与 u2-l2 讲的「复位值随极性自洽、默认复位到 0」一致。

不是所有 hard 单元都这么「平淡」。有的会暴露物理意图：

[asiclib/hdl/asic_header.v:8-17](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/asiclib/hdl/asic_header.v#L8-L17) —— 电源头开关（power header），用 `pmos` 晶体管原语实现：`sleep=1` 时关断电源域。这是只有 hard/物理层才有的东西（综合工具一般不会从一个 `pmos` 原语综合出电源开关，它代表的是 PDK 里的固定结构）。

[asiclib/hdl/asic_keeper.v:7-11](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/asiclib/hdl/asic_keeper.v#L7-L11) —— 电荷保持单元（keeper），模块体是空的（功能由 PDK 提供），这里仅作占位与接口声明。

> **重要澄清**：`asic_dffq` 这类**逻辑/时序 hard 单元并没有 BIST/power/repair 端口**——那些端口属于**存储宏**（大块 SRAM），不属于单个触发器。BIST/power/repair 出现在 `oh_fifo_sync`/`oh_dpram` 的接口上，见 4.3 节。

#### 4.2.4 代码实践

**实践目标**：验证「hard 单元 = 固定单位宽 + PROP 参数 + 与 soft 同逻辑」。

**操作步骤**：

1. 并排打开 [asiclib/hdl/asic_dffq.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/asiclib/hdl/asic_dffq.v) 与 [stdlib/rtl/oh_dffq.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_dffq.v)。
2. 列一张三列对照表：参数、端口宽度、`always` 体。
3. 在 `asiclib/hdl/` 里搜 `PROP`，看是不是每个单元都带这个参数。

**需要观察的现象**：两份文件 `always` 体逐字相同；区别只在参数名（`PROP` vs `DW`）与端口宽度（1 位 vs `[DW-1:0]`）。

**预期结果**：确认「hard 是 soft 的单位宽、工艺属性化镜像」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 hard 单元是 1 比特、soft 是参数化 `DW` 位？
**答案**：标准单元物理上就是 1 比特的成品（PDK 给的固定积木），无法「拉宽」；soft 是描述，综合器自然会按 `DW` 复制出 `DW` 个标准单元。所以「参数化」这件事在 soft 层做一次即可，hard 层保持最小单位。

**练习 2**：`asic_keeper` 模块体是空的，它有意义吗？
**答案**：有意义。它是接口占位 + 黄金模型（此处功能为「保持电荷」，由 PDK 提供实际结构），让上层可以在 RTL 里例化它，编译期再用 PDK 物理网表替换。空体代表「行为由 PDK 决定，这里只声明端口契约」。

---

### 4.3 参数切换：在编译期二选一

#### 4.3.1 概念说明

soft 和 hard 都摆好了，谁来决定用哪一个？OH! 给了**两种编译期切换机制**，这是本讲最核心的内容：

- **机制 A：字符串参数 + `generate if`**。模块自己声明 `SYN`（或 `TARGET`）、`TYPE`、`SHAPE` 等字符串参数，用 `generate if(SYN=="TRUE")` 在**例化点**选分支。每个实例可独立选，粒度细。
- **机制 B：编译期宏 `CFG_ASIC` + `` `ifdef ``**。用命令行 `-DCFG_ASIC` 全局定义一个宏，文件内用 `` `ifdef CFG_ASIC ... `else ... `endif `` 选分支。全局生效，粒度粗。

此外 hard 实现独有的 `SHAPE` 参数控制**存储宏的版图长宽比**（square/tall/wide），是给物理实现用的版图 hint，soft 分支忽略它。

#### 4.3.2 核心流程

**机制 A（局部、字符串参数）** 的标准范式：

```
generate
   if (SYN == "TRUE") begin : g_soft
      // 用 stdlib 原语（always/assign）现场搭
   end
   else begin : g_hard
      // 例化 asic_* 硬核
   end
endgenerate
```

**机制 B（全局、宏）** 的标准范式：

```
`ifdef CFG_ASIC
   asic_xxx ...   // hard
`else
   // soft RTL
`endif
```

两者各管一类文件：`oh_clockgate`、`oh_dpram`、`oh_fifo_sync` 用机制 A；`oh_reg0`、pad 单元用机制 B。

#### 4.3.3 源码精读

**机制 A 最干净的样例：`oh_clockgate`（门控时钟）。** 它的 soft 分支用 `oh_lat0` 锁存器搭 ICG（集成门控时钟，回顾 u2-l3），hard 分支例化 `asic_clockgate`：

[stdlib/rtl/oh_clockgate.v:8-46](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_clockgate.v#L8-L46) —— 顶部声明 `SYN`/`TYPE` 两个字符串参数（第 9-10 行），`generate if(SYN=="TRUE")`（第 20 行）选 soft、`else`（第 36 行）选 hard。注意 hard 分支把 `TYPE` 透传给 `asic_clockgate`，这正是「`TYPE` 给 PDK 传工艺类型」的用途。

```verilog
generate
   if(SYN == "TRUE") begin
      // ... soft: 用 oh_lat0 搭 ICG ...
      assign eclk = clk & en_sh;
   end
   else begin
      asic_clockgate #(.TYPE(TYPE)) asic_clockgate (...);
   end
endgenerate
```

**机制 A 用于存储：`oh_dpram`（双口 RAM）。** 存储宏多了一个 `SHAPE` 版图参数，并且字符串参数名叫 `TARGET` 而不是 `SYN`：

[stdlib/rtl/oh_dpram.v:8-39](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_dpram.v#L8-L39) —— 参数 `TARGET`（默认 `"DEFAULT"`）+ `SHAPE`（`"SQUARE"`）。端口区除了功能口，还有一组 **BIST 接口**（第 26-32 行：`bist_en/bist_we/bist_wem/bist_addr/bist_din`）和一组 **Power/repair 接口**（第 33-38 行：`shutdown/vss/vdd/vddio/memconfig/memrepair`，注释明确写「hard macro only」）。这就是 hard 实现多出来的物理端口。

[stdlib/rtl/oh_dpram.v:41-76](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_dpram.v#L41-L76) —— `generate if(TARGET=="DEFAULT")` 选 soft（用 `reg` 数组综合出 RAM），`else` 选 hard（例化 `asic_memory_dp`，且**端口全空接 `asic_memory_dp ()`，是个桩**）。注意 hard 分支引用的 `asic_memory_dp` 在 `asiclib` 里**根本没有定义**（本讲已搜证：`asiclib/hdl/` 下无任何 `asic_memory*`）——存储硬宏要由 PDK/SRAM 编译器提供，仓库里只留了占位。

**BIST/power/repair 端口一览（这是本讲实践任务的核心）**，看 `oh_fifo_sync` 的端口表最清楚：

[stdlib/rtl/oh_fifo_sync.v:34-47](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_fifo_sync.v#L34-L47) —— 同步 FIFO 的 BIST 与 Power/repair 端口段。逐个用途：

| 端口 | 类别 | 用途 |
|------|------|------|
| `bist_en` | BIST | 内建自测试（Built-In Self-Test）总使能，高电平时存储宏切到测试模式 |
| `bist_we` / `bist_wem` | BIST | 测试写使能（全局/逐位），测试机通过它直接灌数据进存储阵列 |
| `bist_addr` | BIST | 测试访问地址 |
| `bist_din` / `bist_dout` | BIST | 测试数据入/出 |
| `shutdown` | Power | 关断存储阵列电源（低功耗/电源域下电） |
| `vss` / `vdd` / `vddio` | Power | 地 / 阵列核心电源 / 外围 IO 电源（多电源域分别供电） |
| `memconfig` | 配置 | 通用存储配置位（如冗余/旁路模式） |
| `memrepair` | 修复 | 存储修复向量（用冗余行列替换坏点，提升良率） |

> 这些端口在 soft 分支里基本不接（综合出的 `reg` 数组用不上 BIST/修复），只在 hard 存储宏上有物理意义。所以它们是「hard 才需要的物理接口」的典型范例。

**机制 B 的样例：`oh_reg0`（采样寄存器）。** 它不用字符串参数，而用 `` `ifdef CFG_ASIC ``：

[stdlib/rtl/oh_reg0.v:15-28](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_reg0.v#L15-L28) —— `` `ifdef CFG_ASIC `` 选 hard（例化 `asic_reg0`），`` `else `` 选 soft（`always @(negedge clk)` 的 RTL）。注意 `asic_reg0` 在 `asiclib` 里同样**未定义**（已搜证），是 PDK 占位。

**那 `-DCFG_ASIC=0` 到底什么意思？** 看 build.sh：

[scripts/build.sh:15-19](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/scripts/build.sh#L15-L19) —— 仿真编译命令。`-DCFG_ASIC=0` 在命令行定义宏 `CFG_ASIC`。

```bash
iverilog -g2005 -DTARGET_SIM=1 -DCFG_ASIC=0 -f $OH_HOME/scripts/libs.cmd -o dut.bin $1
```

这里有一个**经典 Verilog「坑」，务必讲清楚**：

- 命令行 `-DCFG_ASIC=0` 等价于 `` `define CFG_ASIC 0 ``，它的作用是**定义宏 `CFG_ASIC`**（替换文本是 `0`）。
- 而 `` `ifdef CFG_ASIC ``（见 IEEE 1364-2005 §19.4 条件编译）**只判断宏「是否被定义」，完全不看它的值**。
- 所以 `-DCFG_ASIC=0` 反而会让 `oh_reg0` 里 `` `ifdef CFG_ASIC `` 分支**为真**，从而选 hard（`asic_reg0`）分支——那个 `=0` 的「0」根本没被 ifdef 理会。

换句话说，「`=0` 表示关闭 ASIC 模式」是**字面意图**，但用裸 `` `ifdef `` 实现时，`0` 不等于「未定义」。要真正得到 soft 分支，应当**不定义** `CFG_ASIC`（或改用判断宏值的写法）。这是「文档/脚本意图 vs. RTL 实际行为」落差的又一例。**结论待本地仿真进一步确认**（建议用 iverilog 分别跑「`-DCFG_ASIC=0`」「不带该选项」两种情形，观察 `oh_reg0` 选了哪一支）。

> 好在仿真真正关心的 `oh_fifo_sync`/`oh_dpram` 等模块用的是**机制 A（字符串参数）**，它们的 `SYN`/`TARGET` 默认 `"TRUE"`/`"DEFAULT"`，**不受 `-DCFG_ASIC` 影响**，所以仿真里这些模块总是走 soft。`-DCFG_ASIC` 主要影响少数用机制 B 的文件（`oh_reg0`、pad 单元）。

**最后一处现实落差（参数名漂移）**：`oh_fifo_sync` 例化 `oh_dpram` 时传的是 `.SYN(SYN) .TYPE(TYPE)`，但 `oh_dpram` 的参数叫 `TARGET`，并没有 `SYN`/`TYPE`：

[stdlib/rtl/oh_fifo_sync.v:111-116](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_fifo_sync.v#L111-L116) —— 例化 `oh_dpram` 时按名传 `.SYN(SYN) .TYPE(TYPE) .SHAPE(SHAPE)`，与 `oh_dpram` 实际参数（`TARGET`/`SHAPE`）对不上（u3-l1 已指出）。按名传一个不存在的参数，多数工具会报警告或错误。阅读时以两份文件的实际参数名为准。

#### 4.3.4 代码实践

**实践目标**：定位 hard 实现多出来的 BIST/power/repair 端口，并说明用途——同时纠正一个常见误解。

**操作步骤**：

1. 打开 [asiclib/hdl/asic_dffq.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/asiclib/hdl/asic_dffq.v)，**仔细数它的端口**。
2. 打开 [stdlib/rtl/oh_fifo_sync.v:34-47](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_fifo_sync.v#L34-L47)，找到 BIST 段和 Power/repair 段。
3. 回答：BIST/power/repair 端口在 hard 触发器（`asic_dffq`）上有吗？在哪里才有？

**需要观察的现象**：
- `asic_dffq` 只有 `d/clk/q` 三个功能端口，**没有任何** BIST/power/repair 端口。
- BIST/power/repair 端口出现在 `oh_fifo_sync`（以及 `oh_dpram`）的**存储宏接口**上。

**预期结果（关键结论，纠正任务描述里的假设）**：
> hard **触发器**并不比 soft 触发器多 BIST/power/repair 端口；触发器的 hard/soft 差异是「`PROP`+单位宽 vs `DW`+参数化」（见 4.2）。**BIST/power/repair 是存储宏（SRAM/FIFO）才有的物理端口**，因为只有大块存储阵列才需要内建自测、电源域关断、冗余修复来保证良率与低功耗。这一点常被误记为「hard 单元普遍多这些端口」，实际只对存储类成立。

4. 进阶（可选）：在 `oh_fifo_sync` 里把每个 BIST/power/repair 端口的「物理用途」填进 4.3.3 节那张表，对照注释核对。

**如果无法运行**：标注「待本地验证」，但表格结论来自源码注释，可直接据源码确认。

#### 4.3.5 小练习与答案

**练习 1**：`oh_clockgate` 里 `SYN="TRUE"` 和 `SYN="FALSE"` 分别选哪一支？默认值是哪个？
**答案**：`"TRUE"` 选 soft（`oh_lat0` 搭 ICG），`"FALSE"` 选 hard（`asic_clockgate`）。默认 `SYN="TRUE"`，即默认 soft、可综合。切换是通过**例化点的参数**完成的，与全局 `-DCFG_ASIC` 无关。

**练习 2**：`oh_dpram` 的 `SHAPE="TALL"` 对 soft 分支有影响吗？
**答案**：没有。`SHAPE` 是 hard 存储宏的版图长宽比 hint（tall/wide/square，影响物理 floorplan），soft 分支（`reg` 数组综合）忽略它。

**练习 3**：`-DCFG_ASIC=0` 会让 `oh_reg0` 选 soft 还是 hard？为什么？
**答案**：会让 `` `ifdef CFG_ASIC `` **为真**，从而选 hard（`asic_reg0`）分支。因为 `` `ifdef `` 只看「宏是否定义」，`-DCFG_ASIC=0` 定义了宏（值为 `0`），`0` 这个值不被 ifdef 理会。要选 soft 必须不定义该宏。**待本地仿真确认。**

---

## 5. 综合实践

把本讲三个最小模块串起来，做一个「穿越 soft/hard 两侧」的源码追踪任务。

**任务**：以一个 8 位寄存器为例，分别画出它在 soft 世界和 hard 世界里的「装配方式」，并标出切换开关在哪一行。

**步骤**：

1. **soft 侧**：要做一个 8 位、无复位的寄存器，只需例化一个 `oh_dffq #(.DW(8))`（[stdlib/rtl/oh_dffq.v:8](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_dffq.v#L8)）。综合器会自动铺出 8 个标准单元。
2. **hard 侧**：同一个 8 位寄存器若走 hard，需要例化 **8 个** `asic_dffq`（[asiclib/hdl/asic_dffq.v:7](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/asiclib/hdl/asic_dffq.v#L7)），每个 1 比特——因为标准单元不可拉宽。流片时这 8 个 `asic_dffq` 再被 PDK 的真实 D 触发器网表替换。
3. **切换开关**：如果包在一个带 `SYN` 参数的封装里，开关就是一处 `generate if(SYN=="TRUE")`（范式见 [stdlib/rtl/oh_clockgate.v:20](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_clockgate.v#L20)）；如果用宏，开关就是一处 `` `ifdef CFG_ASIC ``（范式见 [stdlib/rtl/oh_reg0.v:15](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_reg0.v#L15)）。
4. **交付物**：画一张两栏对照图（左 soft / 右 hard），标出：参数（`DW` vs `PROP`）、例化数量（1 个参数化 vs 8 个单位宽）、切换开关行号、是否带 BIST/power/repair 端口（触发器：否）。
5. **反思题**：若这个寄存器换成「8 深度 ×32 位的 FIFO」，hard 侧会多出哪些端口？为什么？（答：会多出 `oh_fifo_sync` 那套 BIST/power/repair 端口，因为存储阵列才需要自测/关断/修复。）

这个任务把「soft 参数化 vs hard 单位宽」「两种切换机制」「BIST/power/repair 只属于存储」三条主线一次打通。

---

## 6. 本讲小结

- **soft/hard 是同一功能的两种实现来源**：soft（`stdlib`）是工艺无关、参数化、可综合的 RTL 描述；hard（`asiclib`）是绑定特定 PDK 的黄金模型，固定单位宽、带 `PROP` 工艺属性参数。
- **黄金模型契约**：`asiclib/README.md` 规定 hard 单元是逻辑黄金模型，物理实现须逐字复刻，单元间无依赖、按晶圆厂在编译期链接。
- **触发器的 soft/hard 差异是 `DW`+参数化 vs `PROP`+单位宽**，**不是** BIST/power/repair 端口——后者只属于存储宏。
- **两种切换机制**：机制 A 用 `SYN`/`TARGET`/`SHAPE` 字符串参数 + `generate if`（`oh_clockgate`/`oh_dpram`/`oh_fifo_sync`，局部、按实例）；机制 B 用 `CFG_ASIC` 宏 + `` `ifdef ``（`oh_reg0`/pad，全局）。
- **设计文件无 `timescale`、无延时、只用可综合结构**——这是让 soft/hard 可干净替换的全库铁律（已搜证两目录 `timescale` 计数为 0）。
- **现实落差**：`-DCFG_ASIC=0` 因 `` `ifdef `` 只判「是否定义」而令 hard 分支为真（`0` 不等于「未定义」，待本地确认）；`oh_fifo_sync` 传给 `oh_dpram` 的 `SYN/TYPE` 参数名与 `TARGET` 对不上；`asic_memory_dp`/`asic_reg0` 在仓库里无定义、是 PDK 占位桩——阅读一律以 RTL 为准。

---

## 7. 下一步学习建议

- **本单元往下**：第 u9-l2 讲将系统概览 `asiclib` 的标准单元族（组合门、ICG 门控时钟、`asic_header/footer` 电源开关、keeper、tiecell），把你在这里见到的 `asic_dffq`/`asic_header`/`asic_keeper` 放大家族里看全。
- **横向对照**：回头重读 u2-l3（`oh_clockgate` 的 ICG 原理）和 u3-l1（`oh_dpram` 的 `TARGET`/`SHAPE`），现在你能看懂它们 soft/hard 两支的全部含义了。
- **物理收敛**：u9-l4 会讲 `padring` 与芯片顶层集成，把「单元级 soft/hard」上升到「芯片级电源域与 pad 环」，届时 `vdd/vddio/shutdown` 这些端口会在顶层被真正连起来。
- **动手验证宏语义**：用 iverilog 对一个含 `` `ifdef CFG_ASIC `` 的小模块分别跑「`-DCFG_ASIC=0`」与「不定义」，亲眼看选了哪一支，把本讲的「ifdef 坑」坐实。
