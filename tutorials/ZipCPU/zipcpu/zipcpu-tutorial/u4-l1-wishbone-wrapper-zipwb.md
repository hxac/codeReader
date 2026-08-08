# Wishbone 封装 zipwb 与 zipbones

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `zipwb` 在 ZipCPU 封装层中的角色——它是把 `zipcore`、取指控制器、访存控制器和一条 Wishbone 总线「拼装」在一起的中间层。
- 解释为什么两条总线（取指、访存）会被合并成一条对外 Wishbone 出口，以及由谁来仲裁。
- 看懂 `wbdblpriarb`（双优先级仲裁器）的 `r_a_owner` 逻辑，判断「取指」和「访存」同时请求时谁优先。
- 区分「本地总线（local）」与「全局总线（global）」两套 `cyc/stb` 信号的作用。
- 说出 `zipbones` 顶层暴露了哪些 Wishbone 主端口信号，以及它为何被称为「最精简」封装。

本讲是第 4 单元（总线封装、系统整合与外设）的第一讲，回答一个具体问题：**CPU 内核 `zipcore` 本身不直接连总线，那指令和数据访问是怎么被拧成一条对外 Wishbone 总线的？**

## 2. 前置知识

本讲会反复用到前三讲（u3-l1、u3-l2、u3-l6）建立的几个事实，这里只做一句话回顾，不展开：

- **u3-l1（zipcore 结构）**：`zipcore` 是五级流水线内核，但它**不包含**取指缓存和访存控制器，只通过端口把取指请求、访存请求送出来；取指与访存模块实例化在外壳里。`zipwb` 正是「实例化它们」的那个外壳。
- **u3-l2（取指模块族）**：`prefetch`/`dblfetch`/`pffifo`/`pfcache` 四个取指模块 CPU 侧端口完全一致，由 `OPT_LGICACHE` 选档。
- **u3-l6（访存模块族）**：`memops`/`pipemem`/`dcache` 三个访存模块 CPU 侧端口几乎一致，由 `OPT_DCACHE`/`OPT_MEMPIPE` 选档。

如果你还不熟悉「Wishbone 主端口（master）」「`cyc`/`stb`/`ack`/`stall` 握手」这些基本信号，可以把它先简化理解为：主设备拉高 `cyc`（cycle，声明「我要用总线」）和 `stb`（strobe，声明「这一拍数据有效，请处理」），从设备回 `ack`（acknowledge，成功）或 `stall`（请等一等）。本讲重点是「两个主设备（取指、访存）如何共享一条总线」。

> 术语提示：Wishbone 是一种开源的片上总线协议。ZipCPU 的所有 32 位数据/地址访问都走 Wishbone。本讲里的「主设备（master）」指发起访问的一方（这里是 CPU 侧），「从设备（slave）」指响应的一方（外部 RAM、外设等）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [rtl/core/zipwb.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipwb.v) | 中间封装层：实例化 `zipcore` + 取指模块 + 访存模块，用 `wbdblpriarb` 把它们合并成单一 Wishbone 出口。本讲主角。 |
| [rtl/zipbones.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipbones.v) | 顶层封装之一：「光骨头」版，无任何片内外设，实例化 `zipwb` 并加一个调试从端口。 |
| [rtl/ex/wbdblpriarb.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/wbdblpriarb.v) | 双优先级仲裁器，`zipwb` 用它合并取指与访存。理解仲裁的核心就在这里。 |
| [rtl/core/memops.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/memops.v) | 单次访存模块。这里只看它如何区分本地/全局总线（`lcl_bus`），用来理解 `zipwb` 输出的两套 `cyc/stb`。 |

## 4. 核心概念与源码讲解

### 4.1 zipwb 的角色与子模块实例化

#### 4.1.1 概念说明

在前几讲里我们反复强调一个关键事实：**`zipcore` 自己不连总线**。它只通过两组端口把需求送出来——

- **取指接口**：内核告诉外壳「我要取 `pf_request_address` 这条指令」，外壳负责把指令字通过 `i_pf_instruction` 喂回去。
- **访存接口**：内核告诉外壳「我要按 `mem_op` 这种方式访问 `mem_cpu_addr`」，外壳负责发起总线交易并把结果 `i_mem_result` 喂回去。

那么谁来「喂」？谁来把这些请求翻译成 Wishbone 交易？答案就是 `zipwb`。它是夹在「纯计算内核 `zipcore`」和「外部 Wishbone 总线」之间的**胶水层**，做三件事：

1. 实例化 `zipcore`；
2. 实例化取指控制器（前讲的 prefetch/dblfetch/pffifo/pfcache 之一）；
3. 实例化访存控制器（前讲的 memops/pipemem/dcache 之一）；
4. 用一个仲裁器把取指和访存两条内部总线**合并成一条**对外 Wishbone 出口。

可以把 `zipwb` 想成一块「转接板」：内核侧是计算，总线侧是通信，转接板上焊了取指芯片、访存芯片和一个「二选一」开关。

#### 4.1.2 核心流程

`zipwb` 的数据流可以画成下面这样（箭头表示数据/请求方向）：

```
                         ┌──────────── zipcore (五级流水线内核) ────────────┐
                         │  取指请求 ──┐                        ┌── 指令回填  │
                         │  访存请求 ──┤  (内核只发请求/收结果)   ├── 数据回填  │
                         └─────────────┘────────────────────────┘──────────┘
                                        │                                    │
                   ┌────────────────────┘                                    └───────────────────┐
                   ▼ (取指控制器: prefetch/dblfetch/pffifo/pfcache)              (访存控制器: memops/pipemem/dcache) ▼
                   ▼ 产生 pf_cyc/pf_stb/pf_addr ...                           产生 mem_cyc_gbl/lcl, mem_stb_gbl/lcl ...
                   │                                                                    │
                   └─────────────────────► wbdblpriarb (双优先级仲裁器) ◄────────────────────────┘
                                                  │ 合并
                                                  ▼
                            单一对外 Wishbone 主端口: o_wb_gbl_cyc/stb, o_wb_lcl_cyc/stb,
                            o_wb_we, o_wb_addr, o_wb_data, o_wb_sel  (+ i_wb_ack/stall/err/data)
```

要点：取指和访存是**两个独立的内部主设备**，它们不会直接连到对外管脚；先经过仲裁器二选一，再驱动同一组对外 Wishbone 信号。这就是「合并成单一 Wishbone 出口」的含义。

#### 4.1.3 源码精读

`zipwb` 的模块参数和端口定义在 [rtl/core/zipwb.v:101-171](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipwb.v#L101-L171)。注意它的对外 Wishbone 输出**分成两组**：全局总线 `o_wb_gbl_cyc`/`o_wb_gbl_stb` 和本地总线 `o_wb_lcl_cyc`/`o_wb_lcl_stb`（[L151-L153](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipwb.v#L151-L153)）——这一点我们到 4.4 节再细讲。

`zipcore` 的实例化在 [rtl/core/zipwb.v:229-302](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipwb.v#L229-L302)，名为 `core`。注意它把取指请求/回填、访存请求/回填都接到内核对应端口上（[L269-L291](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipwb.v#L269-L291)），但**总线信号（`o_wb_*`）完全没有进 `zipcore`**——这印证了「内核不碰总线」。

取指控制器的「四选一」在 [rtl/core/zipwb.v:319-453](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipwb.v#L319-L453)，完全复刻 u3-l2 讲过的阈值：

- `OPT_LGICACHE <= 1` → `prefetch`（单条取指）
- `OPT_LGICACHE <= 2` → `dblfetch`（双取）
- `OPT_LGICACHE <= 6` → `pffifo`（FIFO 预取）
- 其它（默认 `OPT_LGICACHE=12`）→ `pfcache`（指令缓存）

访存控制器的「三选一」在 [rtl/core/zipwb.v:461-571](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipwb.v#L461-L571)，复刻 u3-l6 讲过的选型。选型由两个 localparam 决定（[L175-L177](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipwb.v#L175-L177)）：

```verilog
localparam [0:0] OPT_DCACHE = (OPT_LGDCACHE > 2);
localparam [0:0] OPT_PIPELINED_BUS_ACCESS = (OPT_PIPELINED);
localparam [0:0] OPT_MEMPIPE = OPT_PIPELINED_BUS_ACCESS;
```

- `OPT_DCACHE` 为真 → `dcache`（数据缓存）
- 否则 `OPT_MEMPIPE` 为真 → `pipemem`（流水线访存）
- 都关 → `memops`（单次访存）

注意取指模块的 `i_clk` 用的是外部 `i_clk`，而访存模块的 `i_clk` 用的是 `cpu_clock`（[L481](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipwb.v#L481)、[L517](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipwb.v#L517)）。当 `OPT_CLKGATE` 关闭（默认）时 `cpu_clock = i_clk`（[L700](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipwb.v#L700)），两者相同；开启时钟门控时访存跟着内核一起被节流。这是低功耗选项，初学可先忽略。

#### 4.1.4 代码实践

**实践目标**：确认 `zipwb` 内部确实实例化了「一个内核 + 一个取指 + 一个访存」，且总线信号不进内核。

**操作步骤**：

1. 打开 `rtl/core/zipwb.v`，定位 `zipcore #(...) core (`（约 L229）。
2. 在该实例的端口连接里查找：是否有任何 `o_wb_*` 或 `i_wb_*` 信号接到 `core`？预期：**没有**。
3. 定位三个 `generate` 块：取指（L319 的 `generate if (OPT_LGICACHE <= 1)`）、访存（L461 的 `generate if (OPT_DCACHE)`）、仲裁（L581 的 `generate if (OPT_PIPELINED)`）。

**需要观察的现象**：`core` 实例只连接 `pf_*`（取指）和 `mem_*`（访存）线网，与 Wishbone 总线毫无关系；三个 `generate` 块各自只保留一个分支被综合（这是综合期「剪刀」，见 u3-l1）。

**预期结果**：你会清楚地看到「内核 = 纯计算」「取指/访存 = 外壳里的芯片」「仲裁器 = 二选一开关」三层分离。这正解释了为何同一份 `zipcore` 能配 Wishbone、AXI4-Lite、AXI4 等不同总线（u1-l3）——换外壳即可，内核不动。

#### 4.1.5 小练习与答案

**练习 1**：`zipwb` 里实例化了几个 `zipcore`？取指控制器和访存控制器各实例化了几个？
**答案**：1 个 `zipcore`；取指控制器和访存控制器**各 1 个**（由 `generate` 根据参数在多选一中挑一个实例化，不是同时存在多个）。

**练习 2**：如果把 `OPT_LGDCACHE` 设为 0、`OPT_PIPELINED` 设为 0，`zipwb` 会选哪个访存模块？
**答案**：`OPT_DCACHE = (0>2) = 0`，`OPT_MEMPIPE = OPT_PIPELINED = 0`，于是落到 `else` 分支选 `memops`（单次访存）。

---

### 4.2 双优先级总线仲裁器 wbdblpriarb

> 这是本讲的核心最小模块。前面把「为什么要合并」讲清楚了，这里讲「具体怎么合并、谁先谁后」。

#### 4.2.1 概念说明

取指控制器和访存控制器是两个独立主设备，但对外只有一条 Wishbone 总线。当两者在同一时刻都想用总线时，必须有仲裁器（arbiter）决定「这一拍让谁发」。`zipwb` 用的仲裁器叫 `wbdblpriarb`——「**w**ish**b**one **dbl**（double）**pri**（priority）**arb**iter」，即「双通道优先级仲裁器」。

它的「双通道」是它最特别的地方。普通仲裁器只有一对 `cyc/stb`；而 `wbdblpriarb` 每个输入主设备都有**两对** `cyc/stb`：`cyc_a/stb_a` 和 `cyc_b/stb_b`。这两对分别对应「全局总线」和「本地总线」（详见 4.4 节）。这样做是为了把「这条访问属于本地还是全局」的地址判断**提前一拍**完成，从而缓解时序压力。源码注释把来龙去脉讲得很清楚（[rtl/ex/wbdblpriarb.v:6-37](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/wbdblpriarb.v#L6-L37)）：原本外设要在一个时钟内同时判断「是不是本地总线」和「是不是指我」，逻辑路径太长、跑不到目标频率；拆成两对信号后，本地/全局判断上一拍就做好了，外设这一拍只需要判断「是不是指我」。

「优先级」则体现在一个叫 `r_a_owner` 的寄存器上：端口 A 是高优先级方，B 是低优先级方。

#### 4.2.2 核心流程

仲裁器的全部精髓就一个寄存器 `r_a_owner`（「A 是不是当前总线拥有者」）和一段三行逻辑：

```
每一拍（posedge）：
  若 reset                    → r_a_owner = 1   (复位后默认 A 拥有)
  若 B 完全没请求( !b_cyc_a && !b_cyc_b )  → r_a_owner = 1   (B 闲着, 总线还给 A)
  若 A 没请求( !a_cyc_a && !a_cyc_b ) 且 B 在 strobe → r_a_owner = 0   (A 闲着才让给 B)
  否则                        → 保持不变
```

把这段读出来就是一条规则：**A 是「常驻」拥有者，B 只有在 A 完全空闲时才能抢到总线；一旦 B 闲下来，总线立刻归还 A**。所以「i_a 端口 = 优先方」，B 是「见缝插针」的一方。

输出则是一个简单的多路选择：当 `r_a_owner=1`，对外信号取自 A 端口；否则取自 B 端口。对没拥有总线的那一方，仲裁器回 `stall=1`（让它等），回 `ack=0`/`err=0`（不把从设备的应答错送给它）。

#### 4.2.3 源码精读

`r_a_owner` 的优先级逻辑在 [rtl/ex/wbdblpriarb.v:150-196](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/wbdblpriarb.v#L150-L196)，关键三行：

```verilog
initial	r_a_owner = 1'b1;                       // 复位/初值: A 拥有
always @(posedge i_clk)
if (i_reset)
    r_a_owner <= 1'b1;
else if ((!i_b_cyc_a)&&(!i_b_cyc_b))          // B 没请求 → 归 A
    r_a_owner <= 1'b1;
else if ((!i_a_cyc_a)&&(!i_a_cyc_b)           // A 没请求 且 B 在 strobe → 让给 B
        &&((i_b_stb_a)||(i_b_stb_b)))
    r_a_owner <= 1'b0;
```

输出的多路选择在 [rtl/ex/wbdblpriarb.v:206-253](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/wbdblpriarb.v#L206-L253)：`o_cyc_a/o_cyc_b/o_stb_a/o_stb_b/o_we/o_adr/o_dat/o_sel` 全部按 `r_a_owner` 二选一；返回侧 `o_a_ack/o_b_ack`、`o_a_stall/o_b_stall`、`o_a_err/o_b_err` 只把真正的 `i_ack/i_stall/i_err` 送给当前拥有者，另一方被屏蔽（`ack=0`、`stall=1`、`err=0`）。

那么 `zipwb` 是怎么「接线」的？这是判断「谁优先」的关键。仲裁器实例 `pformem` 在 [rtl/core/zipwb.v:581-665](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipwb.v#L581-L665)，它用 `generate` 给出**两种接法**：

- **`OPT_PIPELINED` 为真 → `PRIORITY_DATA` 分支**（[L582-L627](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipwb.v#L582-L627)）：把**访存**接在优先端口 `i_a_*`（源码注释明写 `// Memory access to the arbiter, priority position`，见 [L593](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipwb.v#L593)），**取指**接在低优先端口 `i_b_*`。
- **`OPT_PIPELINED` 为假 → `PRIORITY_PREFETCH` 分支**（[L629-L664](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipwb.v#L629-L664)）：反过来，把**取指**接在优先端口 `i_a_*`（注释 `// Prefetch access to the arbiter, priority position`，见 [L639](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipwb.v#L639)），**访存**接在低优先端口 `i_b_*`。

所以「两条访问同时发生时谁优先」的答案是：

| CPU 配置 | 优先端口 `i_a` 接的是 | 同时请求时优先 |
| --- | --- | --- |
| `OPT_PIPELINED=1`（默认） | 访存（数据） | **数据访问优先** |
| `OPT_PIPELINED=0`（非流水线） | 取指（指令） | **指令取指优先** |

设计直觉：流水线 CPU 更怕数据冒险造成的停顿，所以让数据访问优先抢总线、尽快把 load 结果送回流水线；非流水线的简单 CPU 更怕取指断流，所以让取指优先。注意优先是「A 空闲时才让给 B」式的抢占，不是严格轮流。

还有一个值得一看的优化：取指永远不会写总线（`pf_we` 恒为常量、`pf_data` 不变），所以 `zipwb` 让取指端口和访存端口**共用同一条写数据线 `mem_data`**（[L615/L644](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipwb.v#L612-L616)），省一组连线、缓解时序与 LUT。源码注释在 [L600-L611](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipwb.v#L600-L611) 解释了这一点。

#### 4.2.4 代码实践

**实践目标**：亲手判定「默认配置下，取指与访存同时请求时谁先得到总线」。

**操作步骤**：

1. 打开 `rtl/core/zipwb.v`，找到 L581 的 `generate if (OPT_PIPELINED)`。
2. 确认默认走的是 `PRIORITY_DATA`（`begin : PRIORITY_DATA`，L582）。
3. 在该实例里，找到接在 `i_a_*`（`i_a_cyc_a`/`i_a_stb_a`/`i_a_adr`…）上的信号名——它们应该是 `mem_*`（访存）。
4. 找到接在 `i_b_*` 上的信号——应该是 `pf_*`（取指）。
5. 翻到 `rtl/ex/wbdblpriarb.v` 的 `r_a_owner` 逻辑（L191-L195），确认「A 空闲才让给 B」。

**需要观察的现象**：`PRIORITY_DATA` 里访存连 `i_a`、取指连 `i_b`；`PRIORITY_PREFETCH` 里正好相反。

**预期结果**：你能写出一句话结论——「默认 `OPT_PIPELINED=1` 时，数据访问（访存）是优先方；仅当数据访问完全空闲时，取指才能占用总线。」这正是本讲练习任务要求回答的问题。

#### 4.2.5 小练习与答案

**练习 1**：`wbdblpriarb` 的「优先」是严格固定 A 永远优先，还是 A 空闲时让给 B？
**答案**：是「A 常驻、A 空闲时才让给 B」。具体看 `r_a_owner`：B 不请求→归 A；A 不请求且 B 在 strobe→给 B。B 一旦闲下来总线立刻还给 A。

**练习 2**：为什么仲裁器每个主设备要有两对 `cyc/stb`（`_a` 和 `_b`），而不是一对？
**答案**：把「这条访问属于本地总线还是全局总线」的地址判断提前一拍做完，用 `_a`/`_b` 两对信号携带这个结果。这样下游外设在当前拍只需判断「是不是指我」，时序路径更短，能跑到更高频率（见 wbdblpriarb.v 开头注释）。

**练习 3**：在默认配置下，若取指和一次 load 同时发生，谁的 `ack` 会先到？
**答案**：数据访问（load）优先。取指会收到 `o_b_stall=1` 被压住，等 load 的整笔交易（cyc 拉低）结束后，总线归还取指，取指才拿到 `ack`。

---

### 4.3 本地总线与全局总线的分流

#### 4.3.1 概念说明

回看 4.1.3 提到的一个细节：`zipwb` 对外有**两组** Wishbone 输出——`o_wb_gbl_*`（global，全局/外部总线）和 `o_wb_lcl_*`（local，本地总线）。这是 ZipCPU 的一个重要约定（u3-l6 已建立）：

- CPU 访问的地址空间被切成两段。
- 落在普通地址段的访问走**全局总线**，接到片外 RAM、外部外设等。
- 落在 `0xFFxxxxxx` 段（最高字节为 `0xFF`）的访问走**本地总线**，这是为 ZipSystem 的片内外设（定时器、中断控制器、看门狗等，见 u4-l2/u4-l5）保留的「就近」地址段。

为什么要在 CPU 出口就分成本地/全局？因为本地外设离 CPU 很近、要求低延迟，而全局总线可能要走很长的片上互连甚至出芯片。把它们分成两条独立的 `cyc/stb`，可以让本地外设快速响应，也方便上层（ZipSystem）分别处理。注意：**取指永远走全局总线**（指令通常在 ROM/RAM 里），所以取指控制器只产生一对 `pf_cyc/pf_stb`，不区分本地/全局；只有访存控制器会产生本地/全局两对信号。

#### 4.3.2 核心流程

访存控制器内部用一个简单的地址比较来判断走哪条总线（以 `memops.v` 为例）：

```
if (WITH_LOCAL_BUS 开启 且 地址最高字节 == 0xFF)
    → 本地总线: 走 o_wb_cyc_lcl / o_wb_stb_lcl
else
    → 全局总线: 走 o_wb_cyc_gbl / o_wb_stb_gbl
```

随后 `zipwb` 把访存的 `(cyc_gbl, cyc_lcl)`、取指的 `pf_cyc` 一起送进 `wbdblpriarb`。仲裁器同样有「双通道」输出：`o_cyc_a`（= 对外全局 `o_wb_gbl_cyc`）和 `o_cyc_b`（= 对外本地 `o_wb_lcl_cyc`）。任一拍里 `o_cyc_a` 与 `o_cyc_b` 互斥——要么这笔访问是全局的，要么是本地的，不会同时。

> 注意：仲裁器「双通道」里的 `_a/_b`（全局/本地）和 4.2 节「主设备 A/B」（取指/访存）是**两个不同维度**，别混淆。前者描述「这笔交易属于哪条总线」，后者描述「这次总线归哪个主设备」。

#### 4.3.3 源码精读

本地总线的判定在 [rtl/core/memops.v:143](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/memops.v#L143)：

```verilog
assign lcl_bus = (WITH_LOCAL_BUS)&&(i_addr[31:24]==8'hff);
assign lcl_stb = (i_stb)&&( lcl_bus)&&(!misaligned);
assign gbl_stb = (i_stb)&&(!lcl_bus)&&(!misaligned);
```

——最高字节 `0xFF` 即本地，否则全局；`WITH_LOCAL_BUS` 是开关。`pipemem`/`dcache` 用的是同名参数 `WITH_LOCAL_BUS`/`OPT_LOCAL_BUS`，规则一致（u3-l6）。

`zipwb` 把这些信号接进仲裁器时，访存端同时提供了本地和全局两路：`i_a_cyc_a(mem_cyc_gbl)`、`i_a_cyc_b(mem_cyc_lcl)`（PRIORITY_DATA 分支，[L594-L595](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipwb.v#L594-L595)）；而取指端把本地那路恒置 0：`i_b_cyc_b(1'b0)`（[L612](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipwb.v#L612)），表示取指不碰本地总线。

仲裁器输出再分别改名成对外的 `o_wb_gbl_*` 和 `o_wb_lcl_*`（[L620-L623](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipwb.v#L620-L623)）。注意 `o_wb_we/o_wb_addr/o_wb_data/o_wb_sel` 是**共用**的——无论这笔访问走全局还是本地，地址、数据、字节使能都走同一组线，由 `_gbl_cyc`/`_lcl_cyc` 告诉下游「这条地址/数据现在是给谁的」。

#### 4.3.4 代码实践

**实践目标**：搞清一次本地外设访问（如读 `0xFFFF_F000` 处的定时器寄存器）在 `zipwb` 里走的信号路径。

**操作步骤**：

1. 在 `rtl/core/memops.v:143` 确认 `0xFFxxxxxx` → `lcl_bus=1`。
2. 在 `rtl/core/zipwb.v` 的 `PRIORITY_DATA` 分支（L582 起）追：`mem_cyc_lcl` → `i_a_cyc_b` → 仲裁器 → `o_cyc_b` → `o_wb_lcl_cyc`。
3. 对比一次普通 RAM 读（如 `0x1000_0000`）：`lcl_bus=0` → `mem_cyc_gbl` → `i_a_cyc_a` → `o_cyc_a` → `o_wb_gbl_cyc`。

**需要观察的现象**：同一组 `o_wb_addr/o_wb_data` 被复用，靠 `o_wb_gbl_cyc` 与 `o_wb_lcl_cyc` 谁为高来区分下游接收者。

**预期结果**：你能说出「本地访问拉高 `o_wb_lcl_cyc`，全局访问拉高 `o_wb_gbl_cyc`，二者互斥；取指只会拉高 `o_wb_gbl_cyc`」。

**待本地验证**：若你要在仿真里直接观察这两根线的电平切换，需要构造一段访问 `0xFFxxxxxx` 的测试程序并用波形查看（见第 5 节综合实践的可选步骤）。

#### 4.3.5 小练习与答案

**练习 1**：取指访问会拉高 `o_wb_lcl_cyc` 吗？为什么？
**答案**：不会。取指控制器只产生 `pf_cyc/pf_stb`，接在仲裁器的全局通道（`i_b_cyc_a`），本地通道被恒置 `1'b0`（`i_b_cyc_b(1'b0)`）。指令默认放在全局地址段。

**练习 2**：为什么 `o_wb_addr`/`o_wb_data` 不分本地/全局两套，而 `cyc/stb` 要分？
**答案**：地址、数据、字节使能与「属于哪条总线」无关，复用可省管脚和连线；而 `cyc/stb` 必须分开，是为了让下游（本地外设 vs 全局互连）**各自独立地**判断「这笔交易是不是给我的、何时开始」，从而把地址译码的关键路径拆短、提升频率。

---

### 4.4 zipbones：最精简的顶层封装

#### 4.4.1 概念说明

`zipwb` 还不是「顶层」——它对外暴露的是 `o_wb_gbl_*`/`o_wb_lcl_*` 这样的「半成品」信号，而且没有调试控制逻辑（HALT/STEP/RESET 等命令的解析）。真正能直接拿来用的、有完整对外端口的模块是四个顶层封装之一（u1-l3）。其中 `zipbones` 是**最精简**的一个：它的设计目标用源码注释一句话概括——「实现一个**没有任何外设**的 ZipSystem，你要的任何外设都得在模块外自己搭」（[rtl/zipbones.v:7-9](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipbones.v#L7-L9)）。

`zipbones` 做的事：

1. 实例化一个 `zipwb`（即「内核 + 取指 + 访存 + 仲裁」整套）；
2. 关掉本地总线（`WITH_LOCAL_BUS(0)`）——既然没有片内外设，本地总线段就没人响应；
3. 套一层**调试控制器**：解析外部调试从端口的 HALT/STEP/RESET/CLEAR_CACHE/CATCH 命令，转成 `zipwb` 的 `i_halt/i_clear_cache` 等信号；
4. 把 `zipwb` 的全局 Wishbone 信号改名为干净的对外主端口。

#### 4.4.2 核心流程

```
外部调试主设备 ──(i_dbg_*)──► zipbones 调试控制器 ──(i_halt/cpu_reset/...)──►
                                                                         │
外部 Wishbone 从设备 ◄──(o_wb_*)── zipwb (WITH_LOCAL_BUS=0) ◄──实例── zipbones
                                                                         │
                                          本地总线信号 cpu_lcl_cyc ──► 折成 i_wb_err (见 4.4.3)
```

调试控制器把调试从端口（一组 Wishbone slave 信号）解析成命令位：`HALT_BIT`、`STEP_BIT`、`RESET_BIT`、`CLEAR_CACHE_BIT`、`CATCH_BIT`（[rtl/zipbones.v:252-256](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipbones.v#L252-L256)）。这些命令驱动 `cmd_halt`/`cmd_step`/`cmd_reset`/`cmd_clear_cache`，再送给 `zipwb`（调试接口的完整讲解在 u5-l1）。

#### 4.4.3 源码精读

`zipbones` 的对外 Wishbone **主端口**就这一组（[rtl/zipbones.v:113-122](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipbones.v#L113-L122)）：

| 方向 | 信号 | 含义 |
| --- | --- | --- |
| output | `o_wb_cyc` | 声明总线周期（cycle） |
| output | `o_wb_stb` | 选通（strobe），本拍数据有效 |
| output | `o_wb_we` | 写使能（1=写，0=读） |
| output | `o_wb_addr [PAW-1:0]` | 字地址（`PAW=ADDRESS_WIDTH-2`，见下） |
| output | `o_wb_data [BUS_WIDTH-1:0]` | 写数据 |
| output | `o_wb_sel [BUS_WIDTH/8-1:0]` | 字节使能（选哪些字节） |
| input | `i_wb_stall` | 从设备反压（请等） |
| input | `i_wb_ack` | 从设备应答（成功） |
| input | `i_wb_data [BUS_WIDTH-1:0]` | 读回数据 |
| input | `i_wb_err` | 总线错误 |

这就是一个标准的 Wishbone 主端口（经典 `cyc/stb/we/addr/data/sel` + `ack/stall/err/idata`）。另外还有一个调试**从端口**（[L127-L136](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipbones.v#L127-L136)）：`i_dbg_cyc/i_dbg_stb/i_dbg_we/i_dbg_addr[5:0]/i_dbg_data` 和 `o_dbg_ack/o_dbg_stall/o_dbg_data`。

`zipwb` 的实例化在 [rtl/core/zipwb.v 的调用方 rtl/zipbones.v:610-672](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipbones.v#L610-L672)。两个关键细节：

1. **关掉本地总线**：`.WITH_LOCAL_BUS(0)`（[L634](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipbones.v#L634)）。这意味着访存控制器内部 `lcl_bus` 恒为 0，所有访问都走全局总线，`cpu_lcl_cyc`/`cpu_lcl_stb` 不会真正被拉高。

2. **把本地总线信号折叠成错误**（[L662](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipbones.v#L662)）：
   ```verilog
   .i_wb_err((i_wb_err)||(cpu_lcl_cyc)),
   ```
   `zipwb` 的本地总线输出 `o_wb_lcl_cyc`（在 `zipbones` 里改名 `cpu_lcl_cyc`）**没有**被引到顶层管脚，而是被并进了对 `zipwb` 的 `i_wb_err` 输入。也就是说：万一（理论上不该发生的）一笔本地访问冒出来，`zipwb` 会立刻收到一个总线错误，从而触发异常。这是「无外设」承诺的安全兜底——本地地址段无人应答，理应报错。

3. **全局信号改名为干净的主端口**：`.o_wb_gbl_cyc(o_wb_cyc)`、`.o_wb_gbl_stb(o_wb_stb)`（[L654-L655](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipbones.v#L654-L655)）。`zipwb` 对外的 `o_wb_gbl_*` 在 `zipbones` 顶层被去掉 `gbl_` 后缀，成为唯一的 Wishbone 主端口。

地址宽度也要注意：`zipbones` 的 `ADDRESS_WIDTH` 默认 32（**字节**地址），而 `PAW = ADDRESS_WIDTH - $clog2(BUS_WIDTH/8) = 32 - 2 = 30`（**字**地址，[L99](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipbones.v#L99)）。它传给 `zipwb` 的 `ADDRESS_WIDTH` 也是字宽 `ADDRESS_WIDTH-2`（[L613](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipbones.v#L613)）。所以 Wishbone 总线上的 `o_wb_addr` 是 30 位字地址，字节选择由 `o_wb_sel` 承担——这是 Wishbone 的常见用法。

#### 4.4.4 代码实践

**实践目标**：列出 `zipbones` 暴露的 Wishbone 主端口信号，并验证「本地总线被关掉且被折成错误」。

**操作步骤**：

1. 打开 `rtl/zipbones.v`，在 module 端口表（L110-L233）里找出所有 `o_wb_*` 和 `i_wb_*`，抄下信号名与位宽。
2. 定位 `zipwb` 实例（L610），确认 `.WITH_LOCAL_BUS(0)`（L634）。
3. 定位 `.i_wb_err((i_wb_err)||(cpu_lcl_cyc))`（L662），理解它的兜底含义。
4. 对照 `rtl/zipsystem.v`（u4-l2 会详讲），看后者多了哪些端口（中断、定时器、看门狗、DMA……）——那些就是 `zipbones` 砍掉的「外设」。

**需要观察的现象**：`zipbones` 顶层只有一组 Wishbone 主端口 + 一组调试从端口 + 时钟/复位/中断线，没有任何定时器/计数器/看门狗/DMA 端口。

**预期结果**：你能写一份对比表——`zipbones` 主端口信号清单（见上表）vs `zipsystem` 多出来的外设端口。结论：`zipbones` 适合「外设自己搭」的项目（面积最小），`zipsystem` 适合「开箱即用」。

**可选验证（待本地验证）**：若已按 u1-l4 构建 `sim/verilator`，可用 `-DZIPBONES` 宏编译 `zipbones_tb`（`zipcpu_tb.cpp` 靠该宏切换外壳），运行 `make stest` 观察一个简单程序在该封装下跑通。这能确认上述端口足以支撑一次完整取指→执行→访存→HALT 流程。

#### 4.4.5 小练习与答案

**练习 1**：`zipbones` 顶层为什么没有 `o_wb_lcl_cyc` 这样的本地总线管脚？
**答案**：`zipbones` 把 `WITH_LOCAL_BUS` 设为 0，没有片内外设，本地总线无人响应；所以本地访问的 `cpu_lcl_cyc` 不引出管脚，而是折进 `zipwb` 的 `i_wb_err`，让任何（不该发生的）本地访问直接报总线错误。

**练习 2**：`zipbones` 的 `o_wb_addr` 是字节地址还是字地址？多少位？
**答案**：字地址，默认 30 位（`PAW=32-2`）。字节选择由 4 位 `o_wb_sel` 承担。传给 `zipwb` 的 `ADDRESS_WIDTH` 也是去掉低 2 位的字宽。

**练习 3**：`zipbones` 相比 `zipsystem`，砍掉了什么、保留了什么？
**答案**：砍掉了所有片内外设（定时器、中断控制器、看门狗、计数器、Jiffies、DMA、性能计数器等）和本地总线；保留了 `zipcore`+取指+访存+仲裁（即 `zipwb`）和调试从端口、Wishbone 主端口、复位/中断线。这是「面积最小」与「开箱即用」的取舍。

---

## 5. 综合实践

**任务**：画出 `zipbones` 内部从 `zipcore` 到对外 Wishbone 总线的完整数据通路，并解释一次「数据 load」与一次「取指」是如何在同一个仲裁器汇合、最终从同一个管脚出去的。

建议按以下步骤完成（源码阅读型实践）：

1. **画内核侧**：从 [rtl/zipbones.v:610](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipbones.v#L610) 的 `zipwb thecpu` 实例入手，标出 `zipcore`（[zipwb.v:229](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipwb.v#L229)）发出的取指请求线和访存请求线。

2. **画控制器侧**：标注取指控制器（[zipwb.v:319-453](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipwb.v#L319-L453)）和访存控制器（[zipwb.v:461-571](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipwb.v#L461-L571)）各自产生的 Wishbone 信号（取指：`pf_cyc/pf_stb`；访存：`mem_cyc_gbl/mem_cyc_lcl` 等）。

3. **画仲裁器**：画出 `wbdblpriarb pformem`（[zipwb.v:584](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipwb.v#L584)），标清默认 `OPT_PIPELINED=1` 下：访存 = `i_a`（优先）、取指 = `i_b`（次级），并依据 [wbdblpriarb.v:191-195](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/wbdblpriarb.v#L191-L195) 写出「A 空闲才让 B」的判定。

4. **画对外侧**：从仲裁器输出 `o_cyc_a/o_cyc_b/...` 改名为 `o_wb_gbl_*/o_wb_lcl_*`（[zipwb.v:620-623](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipwb.v#L620-L623)），再到 `zipbones` 顶层 `o_wb_cyc/o_wb_stb/...`（[zipbones.v:113-122](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipbones.v#L113-L122)），注意 `cpu_lcl_cyc` 被折进 `i_wb_err`（[zipbones.v:662](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipbones.v#L662)）。

5. **回答三个问题**（写在图旁边）：
   - 默认配置下取指与 load 同时发生，谁先拿到总线？（答：load，访存优先。）
   - 一次访问 `0xFFxxxxxx` 的 load 在 `zipbones` 里会怎样？（答：`WITH_LOCAL_BUS=0` 使 `lcl_bus=0`，本应走全局；若极端情况下 `cpu_lcl_cyc` 仍被拉高，会被折成 `i_wb_err` 报总线错误。）
   - 为什么对外只有一组 `addr/data/sel` 却有两组 `cyc/stb`？（答：地址数据复用，靠 `gbl/lcl cyc` 区分下游；分两组是为缩短地址译码关键路径。）

**预期结果**：一张清晰的「内核→取指/访存→仲裁→对外主端口」框图 + 三段文字回答。如果你能把这张图和 u3-l1（流水线）、u3-l6（访存模块族）的图连起来，就真正理解了 ZipCPU 从「纯计算内核」到「可挂总线的软核」的完整装配过程。

> 想在仿真里眼见为实（待本地验证）：按 u1-l4 用 `sim/verilator` 的 `zipbones_tb` 跑一段既有取指又有访存的程序，用 `--trace` 生成波形，对照 `pf_cyc`、`mem_cyc_gbl`、`o_wb_cyc`、`r_a_owner`（在 `wbdblpriarb` 内）观察仲裁器的实际切换。

## 6. 本讲小结

- `zipwb` 是「内核 `zipcore`」与「外部总线」之间的胶水层：实例化内核 + 一个取指控制器（`OPT_LGICACHE` 四选一）+ 一个访存控制器（`OPT_DCACHE/OPT_MEMPIPE` 三选一）+ 一个仲裁器，把两条内部总线合并成一条对外 Wishbone 出口。内核本身不碰总线。
- 仲裁器 `wbdblpriarb` 的核心是 `r_a_owner` 寄存器：端口 A 是常驻优先方，B 只在 A 空闲时才抢到总线，B 一闲立刻归还 A。
- 在 `zipwb` 里默认 `OPT_PIPELINED=1` 走 `PRIORITY_DATA`：**访存（数据）优先、取指次级**；非流水线配置则反过来取指优先。取指端口与访存端口共用写数据线以省资源。
- `zipwb` 对外有两套 `cyc/stb`：`o_wb_gbl_*`（全局/外部）和 `o_wb_lcl_*`（本地，`0xFFxxxxxx` 段，供 ZipSystem 片内外设用）；地址/数据/字节使能复用一组。取指永远走全局。
- `zipbones` 是最精简顶层：实例化 `zipwb` 并关掉本地总线（`WITH_LOCAL_BUS(0)`），把本地访问折成总线错误，套一层调试控制器，对外暴露一组干净的 Wishbone 主端口（`o_wb_cyc/stb/we/addr/data/sel` + `i_wb_stall/ack/data/err`）和一组调试从端口。
- `zipbones` 适合「外设自搭、面积优先」的项目；要开箱即用的片内外设，改用 `zipsystem`（u4-l2）。

## 7. 下一步学习建议

- **u4-l2（ZipSystem 整合）**：看 `zipsystem.v` 如何在 `zipwb` 同款骨架上挂上定时器、中断控制器、看门狗、DMA、计数器，并把本地总线段真正接起来——正好补上本讲「`zipbones` 砍掉的那部分」。
- **u4-l3（AXI 与 AXI-Lite 封装）**：对比 `zipaxil.v`/`zipaxi.v` 如何**不经过 `wbdblpriarb`**、而是让指令总线与数据总线**分离**——这是与本讲「合并成单口」截然不同的另一种封装思路。
- **u4-l4（总线支持模块 rtl/ex）**：深入 `wbdblpriarb` 所在的 `rtl/ex` 目录，看 `wbpriarbiter`、`sfifo`、`skidbuffer`、`fwb_master/slave` 等总线辅助件，理解仲裁与形式化属性的全貌。
- **u5-l1（调试接口）**：本讲提到的 `zipbones` 调试控制器（HALT/STEP/RESET/CATCH 命令解析）将在那里展开。
- 复习建议：学完 u4-l2 后回来重看本讲的 `WITH_LOCAL_BUS(0)`，你会更明白「为什么 `zipbones` 敢把本地总线折成错误」——因为 `zipsystem` 才是本地总线真正的用武之地。
