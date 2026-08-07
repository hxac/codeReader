# ZipSystem 整合：核心+外设+调试

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `zipsystem` 顶层封装的**三层总线**（`gbl` 全局总线、`lcl` 本地总线、`sys` 系统总线）各自连接了什么、又在哪里汇合。
- 画出 CPU 取指/访存请求**经两级仲裁**到达外部总线或内部外设的完整路径。
- 列出挂在内部 `sys` 总线上的所有外设，并写出每个外设的**基地址**。
- 解释**调试端口**如何复用 `sys` 总线、又如何通过命令寄存器去 halt/step/复位 CPU。

本讲是第 4 单元「总线封装、系统整合与外设」的枢纽：上一讲（u4-l1）我们看到最精简的 `zipbones` 把 CPU 裸接一条 Wishbone；本讲把镜头拉到「开箱即用」的 `zipsystem`——它在同一个对外骨架里，把内核、一堆外设和一个调试从端口挂到一条内部总线上。

## 2. 前置知识

在进入本讲前，请确认你已理解（对应前置讲义）：

- **Wishbone 总线基础信号**：`cyc`/`stb`/`we`/`ack`/`stall`/`err` 的握手含义，以及主设备（master）与从设备（slave）之分（u4-l1）。
- **zipwb 是夹心层**：它实例化纯计算内核 `zipcore`、一个取指控制器、一个访存控制器，并用仲裁器把取指与访存合并成对外总线（u4-l1）。
- **流水线停顿机制**：`master_stall` 反压、写回级不可停顿等（u3-l7），这关系到外设返回的 `ack`/`stall` 如何影响 CPU。
- **中断与双寄存器组**：CPU 只有一条中断线，靠 supervisor/user 双寄存器组响应（u2-l5）。

两个本讲会用到的关键概念：

- **地址译码（address decode）**：一条总线上挂多个从设备时，主设备发出的地址需要被「翻译」成「选中哪个从设备」的选择信号，通常用地址的若干高位做比较。
- **总线仲裁（arbitration）**：一条总线同一时刻只能有一个主设备驱动；当多个主设备都想用时，需要一个仲裁器决定谁先走。

## 3. 本讲源码地图

本讲主要围绕两个文件：

| 文件 | 作用 |
| --- | --- |
| [rtl/zipsystem.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v) | 顶层封装。实例化 `zipwb`(CPU)、可选 `zipmmu`、`wbpriarbiter` 仲裁器，以及定时器/计数器/中断控制器/看门狗/DMA 等外设，把它们挂到内部 `sys` 总线上，并接入调试从端口。 |
| [doc/src/spec.tex](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex) | ISA 规范。其中 *ZipSystem Peripherals* 章定义了外设的 CPU 可见地址与寄存器位域，*ZipSystem Registers* 章定义了调试端口看到的寄存器地址。 |

辅证文件（点到即止，细节归各自的专门讲义）：

| 文件 | 作用 |
| --- | --- |
| [rtl/core/zipwb.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipwb.v) | CPU 夹心层，向 `zipsystem` 输出 `gbl`/`lcl` 两路总线。 |
| [rtl/core/memops.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/memops.v) | 访存控制器；地址高位 `0xFF` 即判定为「本地」访问。 |
| [rtl/ex/wbpriarbiter.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/wbpriarbiter.v) | 双主设备优先级仲裁器，合并 CPU 与 DMA 到外部总线。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. **4.1** ZipSystem 的角色与三层总线（建立全景地图）
2. **4.2** 内部总线仲裁：`sys` 总线复用与外部 `wbpriarbiter`
3. **4.3** 外设实例化与地址映射
4. **4.4** 调试端口的接入与 CPU 控制

### 4.1 ZipSystem 的角色与三层总线

#### 4.1.1 概念说明

上一讲的 `zipbones` 是「裸 CPU」：取指与访存合并成一条 Wishbone 出口，没有片内外设，也没有可用调试通路。`zipsystem` 回答的是另一个问题——**「能不能给我一个开箱即用的小系统？」** 它在同一个对外端口骨架上，额外做三件事：

1. 在 CPU 身边放一组**常用外设**（定时器、计数器、中断控制器、看门狗、DMA、可选 MMU），让简单项目不用自己搭就能跑。
2. 把这些外设挂到一条**内部总线**上，CPU 用普通访存指令就能读写它们（内存映射 I/O）。
3. 提供一个**调试从端口**，外部调试器可以经它暂停 CPU、读写寄存器、单步、甚至访问同样的外设。

为了把「CPU、外设、调试、外部主存」四类访问组织清楚，`zipsystem` 内部用三条带前缀的总线来区分用途（见文件头的注释 [rtl/zipsystem.v:40-60](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L40-L60)）：

- **`cpu` / `gbl`（global）**：CPU 看到的「外部」总线，通往片外主存（RAM/ROM/Flash），可经 MMU 翻译地址。**取指永远走这条线。**
- **`cpu` / `lcl`（local）**：CPU 看到的「本地」外设总线，即下面要讲的 `sys` 总线。只有数据访问命中 `0xFFxxxxxx` 段时才走这条线。
- **`sys`**：真正在 `zipsystem` 内部实现的那条总线，所有外设从端口都挂在这里。它有**两个主设备**：CPU 的 `lcl` 路径，和调试端口。

> 名称小贴士：`cpu_*` 前缀表示「这根线连到/来自 zipwb(CPU)」；`sys_*` 前缀表示「这是内部系统总线上的信号」；`ext_*` 表示「已经仲裁完、即将出门到片外的信号」。看懂这三个前缀，源码就读完一半。

#### 4.1.2 核心流程

一次 CPU 访问在 `zipsystem` 内部的分流，可以用下面的拓扑图概括：

```
                         ┌──────────────── zipsystem.v ────────────────┐
  外部中断 i_ext_int ──┐ │                                              │
                       │ │   ┌────────────────────┐      pic_interrupt  │
                       ▼ │   │  zipwb  (thecpu)   │◀────────────────────┘
   ┌──────────────┐    │ │   │  fetch + memop     │
   │ icontrol pic │◀───┼─┤   │  内部已仲裁 fetch  │   gbl_cyc/stb ─┐
   │ icontrol ctri│    │ │   │  与访存 (wbdblpriarb)│   lcl_cyc/stb ─┼─┐
   └──────┬───────┘    │ │   └────────────────────┘                │ │
          │            │ │            │                           │ │
   main/alt int vector │ │            ▼ (gbl)                      │ │ (lcl)
          │            │ │   ┌─────────────┐                      │ │
          └────────────┼─┤   │  zipmmu(opt)│── mmu_cyc/stb ──┐     │ │
                       │ │   └─────────────┘                 │     │ │
                       │ │                             ┌─────────────────┐
                       │ │   ┌──────────────┐          │  wbpriarbiter   │
                       │ │   │  DMA(opt)    │── dc_ ──▶│  dmacvcpu       │──▶ ext_cyc/stb
                       │ │   └──────────────┘   cyc    │  (CPU 优先 + DMA)│     │
                       │ │                          └─────────────────┘     │
                       │ │                                                   ▼
   外部 Wishbone 主端口 ◀───────────────────────────────────── o_wb_cyc/stb/...
                       │ │   ┌──────────────────────┐
                       │ │   │ sys 总线地址译码 sel_*│◀──── 调试端口(busdelay)┤
                       │ │   └──────────┬───────────┘    i_dbg_*             │
                       │ │   ┌──────────┴──────────────────────┐             │
                       │ │   ▼ PIC/APIC WDT TimerA/B/C Jiffies │             │
                       │ │     计数器×8  DMA(寄存器)  MMU(可选) │             │
                       │ │   └─────────────────────────────────┘             │
   调试端口 i_dbg_* ───┼─▶ (经 busdelay + dbg_addr[6:5] 选择 CTRL/CPU/SYS)   │
                         └──────────────────────────────────────────────────┘
```

数据流分两条主轴：

- **取指/外部数据**：`zipwb` 把取指与访存在内部合并后，输出 `gbl`（全局）一路。这一路先经可选 MMU，再进 `wbpriarbiter` 与 DMA 仲裁，最终成为对外 Wishbone 主端口 `o_wb_*`。
- **本地外设访问**：`zipwb` 同时输出 `lcl`（本地）一路，它直接进 `sys` 总线；`sys` 总线还接纳调试端口的访问。地址译码选中某个外设后返回数据。

一个关键事实：**取指从不走本地总线**。在 `zipwb` 的仲裁器里，预取（prefetch）只接 `gbl` 口，本地口恒为 0（详见 4.2.3）。所以 `0xFFxxxxxx` 段是「数据专用」的外设窗口，不会被当成指令去执行（除非你刻意把代码放到那里——但那不是正常用法）。

### 4.2 内部总线仲裁：sys 总线复用与外部 wbpriarbiter

#### 4.2.1 概念说明

`zipsystem` 里其实有**两个**仲裁/复用点，初学者容易把它们混为一谈：

1. **外部总线的仲裁（`wbpriarbiter`）**：CPU（经 MMU）和 DMA 两个主设备抢同一条对外 Wishbone，由 `wbpriarbiter` 仲裁。
2. **`sys` 总线的复用（多路选择）**：CPU 的本地访问和调试端口的访问都要用 `sys` 总线去读写外设。这里用的不是复杂仲裁器，而是一组 `assign` 做的**多路选择 + 优先级**：CPU 在用就 CPU 用，CPU 没用才轮到调试器。

为什么要分两种？因为它们的「冲突形态」不同。CPU 与 DMA 可能**同时、长时间**占用外部总线（DMA 搬一大块内存），需要一个状态机式的仲裁器保证公平与时序；而 CPU 与调试器对 `sys` 的占用是「CPU 几乎一直在用，调试器偶尔插一脚」，CPU 优先、调试器等 CPU 空隙即可，用简单选择就够，省逻辑。

#### 4.2.2 核心流程

**外部总线仲裁（CPU ↔ DMA）**：`wbpriarbiter` 是一个**双主设备优先级仲裁器**，其规则在文件头写得很清楚（[rtl/ex/wbpriarbiter.v:14-22](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/wbpriarbiter.v#L14-L22)）：

1. 无人请求时，端口 A（CPU）默认拥有总线，零延迟直通。
2. 若 B（DMA）请求且总线空闲，则 B 获得总线。
3. 授权持续到拥有者撤销 `cyc`。
4. 一旦 `cyc` 撤销，总线归还给 A。

也就是说 A 是「常驻优先」，B 只在 A 不用时「蹭」一下。`zipsystem` 里 A 接 MMU（即 CPU 路径），B 接 DMA：

```
端口 A (优先) = MMU 输出 (来自 CPU 的 gbl 路)
端口 B        = DMA 输出 (dc_cyc/dc_stb)
合并输出      = ext_cyc/ext_stb  → 对外 Wishbone 主端口
```

**`sys` 总线复用（CPU ↔ 调试器）**：规则是「CPU 优先，CPU 不用时让调试器用」。注释里说得直白：调试器要访问 `sys` 总线，必须同时满足「CPU 没在用本地总线」等条件（[rtl/zipsystem.v:1668-1682](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L1668-L1682)）。

#### 4.2.3 源码精读

先看外部总线的仲裁实例。`wbpriarbiter` 的实例名是 `dmacvcpu`（意为「DMA 与 CPU 之间的仲裁」），端口 A 接 MMU、端口 B 接 DMA、合并出 `ext_*`：

[rtl/zipsystem.v:1825-1840](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L1825-L1840)

```verilog
wbpriarbiter #(
    .DW(BUS_WIDTH),
    .AW(PAW)
) dmacvcpu(
    i_clk,
    mmu_cyc, mmu_stb, mmu_we, mmu_addr, mmu_data, mmu_sel,   // A: CPU 路(经MMU)
        mmu_stall, mmu_ack, mmu_err,
    dc_cyc, dc_stb, dc_we, dc_addr, dc_data, dc_sel,          // B: DMA
        dc_stall, dc_ack, dc_err,
    ext_cyc, ext_stb, ext_we, ext_addr, ext_odata, ext_sel,   // 合并: 对外总线
        ext_stall, ext_ack, ext_err
);
```

当 `OPT_DMA` 关闭时，`dc_cyc`/`dc_stb` 被恒置 0（[rtl/zipsystem.v:1241-1242](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L1241-L1242)），DMA 永不请求，仲裁器退化为「CPU 直通」。

再看 `sys` 总线复用。下面四行 `assign` 把 CPU 本地访问与调试器访问「二选一」地送上 `sys` 总线，CPU 优先（条件 `cpu_lcl_cyc` 在前）：

[rtl/zipsystem.v:1683-1690](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L1683-L1690)

```verilog
assign sys_cyc = (cpu_lcl_cyc)||(dbg_cyc);
assign sys_stb = (cpu_lcl_cyc)
                ? (cpu_lcl_stb)
                : ((dbg_stb)&&(dbg_addr[6:5]==DBG_ADDR_SYS));
assign sys_we  = (cpu_lcl_cyc) ? cpu_we : dbg_we;
assign sys_addr= (cpu_lcl_cyc) ? cpu_addr[7:0] : { 3'h0, dbg_addr[4:0]};
assign sys_data= (cpu_lcl_cyc) ? cpu_data[DBG_WIDTH-1:0] : dbg_idata;
```

注意两点：① CPU 来时，`sys_addr` 取 `cpu_addr[7:0]`（8 位字地址）；调试器来时，地址来自 `dbg_addr[4:0]` 并补 3 个 0——也就是说**调试器只能访问 `sys` 地址空间的低 32 个字**，恰好覆盖所有外设（MMU 除外，详见 4.3）。② 调试器只有当 `dbg_addr[6:5]==DBG_ADDR_SYS`（即 `2'b10`）时才走 `sys`；另外两个区段 `2'b00`/`2'b01` 分别是控制寄存器与 CPU 内部寄存器（4.4 详述）。

最后确认「取指不走本地」。在 `zipwb` 的仲裁器实例里，预取口（端口 B）的本地请求被硬接成 0：

[rtl/core/zipwb.v:612-613](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipwb.v#L612-L613)

```verilog
.i_b_cyc_a(pf_cyc), .i_b_cyc_b(1'b0),   // 预取只走 gbl，lcl 恒 0
    .i_b_stb_a(pf_stb), .i_b_stb_b(1'b0),
```

而数据访存控制器则按地址高位决定走哪路，命中 `0xFF` 即本地（[rtl/core/memops.v:143](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/memops.v#L143)）：

```verilog
assign lcl_bus = (WITH_LOCAL_BUS)&&(i_addr[31:24]==8'hff);
```

#### 4.2.4 代码实践

**实践目标**：用源码确认「CPU 优先于 DMA」这一仲裁规则，并理解 DMA 被反压时的现象。

**操作步骤**（源码阅读型实践）：

1. 打开 [rtl/ex/wbpriarbiter.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/ex/wbpriarbiter.v)，找到决定「当前拥有者」的寄存器（通常名为 `r_owner` 之类）的赋值逻辑。
2. 回答：当 CPU（A）正在发起一次长突发、DMA（B）此刻也拉高 `cyc`，会发生什么？DMA 的 `stall` 会被怎样驱动？
3. 在 [rtl/zipsystem.v:1825](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L1825) 处确认：A 端口连的是 `mmu_*`，B 端口连的是 `dc_*`。

**需要观察的现象 / 预期结果**：CPU 在用时 DMA 必须等；CPU 一撤销 `cyc`，DMA 立刻获得总线。由于 CPU 看到的 `mmu_stall` 在 DMA 占用时会被拉高，CPU 流水线随之停顿——这正是 u3-l7 讲的 `master_stall` 的一个来源。

> 待本地验证：若你有 Verilator 环境，可在 DMA 搬运期间让 CPU 也密集访存，对比开启/关闭 `OPT_DMA` 时 CPU 的指令周期计数差异。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `sys` 总线没有用 `wbpriarbiter`，而外部总线用了？

> **参考答案**：`sys` 总线的两个主设备（CPU、调试器）访问模式高度不对称——CPU 几乎一直可能在使用，调试器只在 CPU 暂停或空隙时偶发访问，用一组 `assign` 做「CPU 优先」的多路选择就够，省下一个状态机和寄存器。外部总线则不同，CPU 与 DMA 都可能长时间、突发式占用，必须用带「持续授权直到 cyc 撤销」语义的 `wbpriarbiter` 来保证双方都能推进、时序可预期。

**练习 2**：若把 `OPT_DMA` 设为 0，`wbpriarbiter dmacvcpu` 还会存在吗？外部总线还能正常工作吗？

> **参考答案**：`wbpriarbiter` 这个实例本身仍存在（它不在 `generate if(OPT_DMA)` 内）。但 DMA 侧的 `dc_cyc`/`dc_stb` 被 [rtl/zipsystem.v:1241-1242](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L1241-L1242) 恒置 0，B 端口永不请求，仲裁器退化为「A 直通」，CPU 路径照常出门到外部总线。

### 4.3 外设实例化与地址映射

#### 4.3.1 概念说明

`sys` 总线上挂着一系列「内存映射外设」：CPU 用一条普通的 `LW`/`SW` 指令，把地址指向 `0xFFxxxxxx` 段，就能读写它们的控制/状态寄存器。每种外设占一小段地址，由**地址译码**把地址翻译成「选中信号」`sel_xxx`。

规范在 *ZipSystem Peripherals* 章给出 CPU 可见的外设地址表（[doc/src/spec.tex:2908-2932](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L2908-L2932)）。需要特别留意一个**地址换算**关系：CPU 发出的是 32 位字节地址，而 `sys` 总线上用的是 8 位**字地址**（`sys_addr = cpu_addr[7:0]`，见 4.2.3）。二者关系为：

\[
\text{CPU 字节地址} = 0xFF000000 + (\text{sys\_addr}) \times 4
\]

外设清单与地址（字节地址对照 `sys_addr`）如下：

| 外设 | `sys_addr`（字） | CPU 字节地址 | 说明 | 译码信号 |
| --- | --- | --- | --- | --- |
| PIC（主中断控制器） | `0x00` | `0xff000000` | 合并多路外部中断为单线 | `sel_pic` |
| WDT（看门狗定时器） | `0x01` | `0xff000004` | 超时触发**复位**（非中断） | `sel_watchdog` |
| WBU（总线看门狗） | `0x02` | `0xff000008` | 总线超时触发**总线错**，记录出错地址 | `sel_bus_watchdog` |
| APIC（辅中断控制器） | `0x03` | `0xff00000c` | 第二组中断（计数器/外部） | `sel_apic` |
| Timer A / B / C | `0x04`/`0x05`/`0x06` | `0xff000010`/`14`/`18` | 三个倒计数定时器，到 0 触发中断 | `sel_timer`（按 `addr[1:0]`） |
| Jiffies | `0x07` | `0xff00001c` | 正计数「滴答」计数器，比较中断 | `sel_timer`（`addr[1:0]==11`） |
| 主性能计数器 ×4 | `0x08–0x0b` | `0xff000020–2c` | 任务时钟/访存停顿/预取停顿/指令计数 | `sel_counter`（按 `addr[2:0]`） |
| 用户性能计数器 ×4 | `0x0c–0x0f` | `0xff000030–3c` | 仅用户模式计数的对应四项 | `sel_counter` |
| DMA 控制寄存器 ×4 | `0x10–0x13` | `0xff000040–4c` | DMA 命令/长度/源地址/目的地址 | `sel_dmac`（按 `addr[1:0]`） |
| MMU（可选） | `0x80` | `0xff000200` | 地址翻译（仅 CPU 可访问，调试端口访问不到） | `sel_mmus`（`addr[7]`） |

> 名称对照：spec 表里把总线看门狗记作 `WBU`（[doc/src/spec.tex:2912](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L2912)），源码里对应 `BUSWATCHDOG`/`sel_bus_watchdog`。

#### 4.3.2 核心流程

外设的「读返回」要汇回 `sys` 总线。由于外设不止一个，需要一个**读多路选择器**：用本次访问命中的译码信号 `sel_xxx` 生成一个 3 位索引 `ack_idx`，再由它选通对应外设的 `ack`/`data` 返回给 `sys_ack`/`sys_idata`。

外设的「中断」也要汇成 CPU 的单线。`zipsystem` 把各外设的中断输出拼成两个向量：`main_int_vector`（进主 PIC）与 `alt_int_vector`（进辅 PIC）。两个 PIC 各自合并后输出 `pic_interrupt`/`ctri_int`，其中 `pic_interrupt` 最终作为 CPU 的唯一中断输入 `i_interrupt`。

外设的「时钟使能」大多受 `cmd_halt` 控制（调试器暂停 CPU 时，定时器/计数器也一并暂停），例如 `.i_ce(!cmd_halt)`。

#### 4.3.3 源码精读

**地址译码**：8 个 `sel_*` 信号由 `sys_addr` 与各外设基址比较得到，集中在一段 `assign`：

[rtl/zipsystem.v:658-665](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L658-L665)

```verilog
assign sel_pic         = (sys_stb)&&(sys_addr == INTCTRL);       // 0x00
assign sel_watchdog    = (sys_stb)&&(sys_addr == WATCHDOG);     // 0x01
assign sel_bus_watchdog= (sys_stb)&&(sys_addr == BUSWATCHDOG);  // 0x02
assign sel_apic        = (sys_stb)&&(sys_addr == CTRINT);       // 0x03
assign sel_timer       = (sys_stb)&&(sys_addr[7:2]==TIMER_A[7:2]);   // 0x04-07
assign sel_counter     = (sys_stb)&&(sys_addr[7:3]==MSTR_TASK_CTR[7:3]); // 0x08-0f
assign sel_dmac        = (sys_stb)&&(sys_addr[7:4] ==DMAC_ADDR[7:4]);  // 0x10-1f
assign sel_mmus        = (sys_stb)&&(sys_addr[7]);              // 0x80-ff (MMU)
```

对应的基址常数定义在同一处 localparam（[rtl/zipsystem.v:350-374](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L350-L374)），并附有每个译码命中位对应的中断向量位号注释（如 `TIMER_A = 8'h4 // Sets IVEC[4]`），与外设的中断输出一一对应。

注意译码宽度的「层级」：单地址外设（PIC/WDT/WBU/APIC）用 `==` 全等比较；占 4 字的定时器/Jiffies 用 `[7:2]` 比较、再用 `[1:0]` 区分具体哪一个；占 4 字的 DMA 用 `[7:4]` 比较、内部用 `[1:0]` 选寄存器；MMU 单独用最高位 `[7]`。

**返回值多路选择**：`w_ack_idx` 把命中的 `sel_*` 编成 3 位索引，`ack_idx` 在 `sys_stb` 时锁存，再用 `case` 选通返回：

[rtl/zipsystem.v:1719-1734](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L1719-L1734)

```verilog
always @(posedge i_clk) begin
    case(ack_idx)
    3'h0: { sys_ack, sys_idata } <= { mmus_ack, mmus_data };
    3'h1: { sys_ack, sys_idata } <= { last_sys_stb,  wdt_data  };
    3'h2: { sys_ack, sys_idata } <= { last_sys_stb,  wdbus_data };
    3'h3: { sys_ack, sys_idata } <= { last_sys_stb,  ctri_data };  // APIC
    3'h4: { sys_ack, sys_idata } <= { last_sys_stb,  tmr_data };   // Timer/Jiffies
    3'h5: { sys_ack, sys_idata } <= { last_sys_stb,  actr_data };  // 计数器
    3'h6: { sys_ack, sys_idata } <= { dmac_ack, dmac_data };       // DMA
    3'h7: { sys_ack, sys_idata } <= { last_sys_stb,  pic_data };   // PIC
    endcase
    if (i_reset || !sys_cyc)
        sys_ack <= 1'b0;
end
```

（`w_ack_idx` 的编码见 [rtl/zipsystem.v:1739-1750](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L1739-L1750)。）注意 DMA 的 `ack` 直接用 `dmac_ack`（多周期），而简单外设用 `last_sys_stb`（单拍应答）。

**外设实例**（举几个典型）：

- **看门狗** `u_watchdog` 其实复用了通用定时器 `ziptimer`，只是把中断输出命名为 `wdt_reset`，接到复位逻辑：

[rtl/zipsystem.v:960-974](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L960-L974)

```verilog
ziptimer #(.BW(32),.VW(31),.RELOADABLE(0)) u_watchdog (
    .i_clk(i_clk), .i_reset(cpu_reset),
    .i_ce(!cmd_halt),
    .i_wb_cyc(sys_cyc),
    .i_wb_stb((sys_stb)&&(sel_watchdog)),
    ...
    .o_int(wdt_reset)   // 注意: 名为 reset, 不是普通中断
);
```

- **定时器 A/B/C 与 Jiffies** 是四个并列实例，共享 `sel_timer`，靠 `sys_addr[1:0]` 区分，各自输出独立中断 `tma_int`/`tmb_int`/`tmc_int`/`jif_int`（[rtl/zipsystem.v:1355-1410](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L1355-L1410)）。

- **DMA** 用 AXI 风格的 `zipdma`（含 scatter-gather），在 `generate if(OPT_DMA)` 内实例化；它的从端口接 `sys` 总线（寄存器配置），主端口 `dc_*` 接到 4.2 讲的外部仲裁器（[rtl/zipsystem.v:1196-1225](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L1196-L1225)）。DMA 细节归 u4-l6。

- **中断向量装配**：6 个内部中断（辅 PIC、3 个定时器、Jiffies、DMA 完成）拼成 `main_int_vector` 的低 6 位，外部中断 `i_ext_int` 拼高位：

[rtl/zipsystem.v:550-558](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L550-L558)

```verilog
assign main_int_vector[5:0] = { ctri_int, tma_int, tmb_int, tmc_int, jif_int, dmac_int };
```

- **主 PIC** `icontrol pic` 把 `main_int_vector` 合并成单线 `pic_interrupt`（最多 15 路输入），它就是喂给 CPU 的 `i_interrupt`（[rtl/zipsystem.v:1420-1451](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L1420-L1451)）。

> 规范对照：spec *Performance Counters* 节解释了 8 个计数器各数什么（时钟、访存停顿、预取停顿、指令数；分主/用户两组），*Bus Watchdog* 节说明总线看门狗是硬件配置、不可改、超时发总线错并记录出错地址（[doc/src/spec.tex:3178-3190](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L3178-L3190)）。DMA 控制寄存器细节见 spec 的 *ZipDMA Controller* 节（[doc/src/spec.tex:3245](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L3245)）。

#### 4.3.4 代码实践

**实践目标**：把「CPU 字节地址 → `sys_addr` → 命中的 `sel_*` → 外设」这条链走一遍。

**操作步骤**（源码阅读 + 推演）：

1. 假设 CPU 执行 `LW R1,0xFF000010`（读 Timer A）。先算出 `sys_addr = (0xFF000010 - 0xFF000000) >> 2 = 0x04`。
2. 在 [rtl/zipsystem.v:658-665](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L658-L665) 判断哪个 `sel_*` 为真。对 `0x04`：`sel_timer` 比较 `sys_addr[7:2]==TIMER_A[7:2]`（`TIMER_A=0x04`，`[7:2]=0x01`）成立 → `sel_timer=1`。
3. 跟到定时器实例 [rtl/zipsystem.v:1355-1365](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L1355-L1365)，确认 Timer A 的 `i_wb_stb` 条件是 `(sys_stb)&&(sel_timer)&&(sys_addr[1:0]==2'b00)`，对 `0x04` 成立。
4. 返回时根据 [rtl/zipsystem.v:1719-1734](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L1719-L1734) 的 `case`，`sel_timer` → `ack_idx=4` → 选通 `tmr_data`，而 `tmr_data` 又由 `sys_addr[1:0]` 在 `tma/tmb/tmc/jif` 间选择（[rtl/zipsystem.v:1694-1704](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L1694-L1704)）。

**预期结果**：读 `0xFF000010` 拿到 Timer A 的当前值；读 `0xFF00001c`（`sys_addr=0x07`，`[1:0]=11`）拿到 Jiffies。整个链路无歧义，每个字节地址唯一映射到一个外设寄存器。

#### 4.3.5 小练习与答案

**练习 1**：CPU 想读 DMA 的「传输长度」寄存器，应该用哪个字节地址？对应 `sys_addr` 和 `sel_dmac` 子地址分别是多少？

> **参考答案**：DMA 寄存器区基址 `0xff000040`（`sys_addr=0x10`）。spec 把四个寄存器顺序列为 DMA 控制状态/长度/源/目的（[doc/src/spec.tex:2926-2929](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L2926-L2929)），故「长度」在 `0xff000044`，即 `sys_addr=0x11`。`sel_dmac` 比较 `[7:4]`，对 `0x11` 成立；DMA 内部用 `sys_addr[1:0]==2'b01`（[rtl/zipsystem.v:1209](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L1209)）选中长度寄存器。

**练习 2**：为什么调试端口访问不到 MMU？

> **参考答案**：调试端口走 `sys` 总线时，`sys_addr = {3'h0, dbg_addr[4:0]}`，最大只能到 `0x1f`（[rtl/zipsystem.v:1689](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L1689)）；而 MMU 的 `sel_mmus` 要求 `sys_addr[7]==1`（即 `0x80` 以上）。所以 MMU 只能被 CPU 访问，调试端口访问不到——源码文件头注释也明确写了「MMU ... is not available via debug bus」（[rtl/zipsystem.v:120](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L120)）。

### 4.4 调试端口的接入与 CPU 控制

#### 4.4.1 概念说明

`zipsystem` 对外有一组调试从端口（`i_dbg_*`/`o_dbg_*`）。外部调试器（如 `zipdbg`）通过它做四件事：① 读 CPU 状态、② 暂停/单步/复位 CPU、③ 读写 CPU 内部 32 个寄存器、④ 读写 `sys` 总线上的外设。

调试端口的 7 位地址 `dbg_addr` 被它的最高两位 `dbg_addr[6:5]` 切成三个区段（[rtl/zipsystem.v:409-411](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L409-L411)）：

| `dbg_addr[6:5]` | 名称 | 访问对象 |
| --- | --- | --- |
| `2'b00` | `DBG_ADDR_CTRL` | 控制与状态寄存器（HALT/STEP/RESET/清缓存/CATCH） |
| `2'b01` | `DBG_ADDR_CPU` | CPU 内部寄存器（sR0–sPC、uR0–uPC 共 32+ 个） |
| `2'b10` | `DBG_ADDR_SYS` | `sys` 总线上的外设（复用 4.3 的地址空间） |

规范在 *Debug Register Addressing* / *ZipSystem Registers* 两章给出了调试端口看到的完整寄存器表（[doc/src/spec.tex:2731-2767](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L2731-L2767)、[doc/src/spec.tex:2859-2896](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L2859-L2896)）：先是 CPU 寄存器区（sR0 起，到 uPC），接着是 ZipSystem 外设区（PIC/WDT/…/DMA），让调试器能读取整个系统状态。注意调试表用的是**字节地址**（如 PIC=256、WDT=260…），换算到 `dbg_addr` 字地址需要除以 4（256/4=64=0x40，恰落在 `DBG_ADDR_SYS` 段）。

> 本讲只讲调试端口「如何接入 `zipsystem`、如何控制 CPU」。完整的调试协议、寄存器位域细节、以及 `zipdbg` 调试器主流程，归 u5-l1「调试接口与调试端口寄存器」。

#### 4.4.2 核心流程

调试器对 CPU 的控制走的是「**命令寄存器**」路径：调试器写 `DBG_ADDR_CTRL` 区段 → 解析出 `halt_request`/`step_request`/`reset_request`/`clear_cache_request` 等脉冲 → 驱动内部命令寄存器 `cmd_halt`/`cmd_step`/`cmd_reset`/`cmd_clear_cache` → 这些 `cmd_*` 再去驱动 `zipwb`(CPU) 的调试输入（`i_halt`/`i_clear_cache`/调试读写口）与外设的 `i_ce`。

一条「暂停 → 读寄存器 → 单步 → 恢复」的典型序列是：

1. 写控制寄存器，置 HALT 位 → `cmd_halt` 拉高 → CPU 的 `i_halt` 生效 → CPU 停在当前指令。
2. 等 CPU 真正停下（`cpu_has_halted`），再对 `DBG_ADDR_CPU` 区段发起读，读回 sR0…uPC。
3. 写控制寄存器置 STEP 位（同时清 HALT）→ `cmd_step` 走一个周期后 CPU 再次停下。
4. 写控制寄存器清 HALT 位（释放）→ `cmd_halt` 撤销 → CPU 继续运行。

调试器对**外设**的访问则走 `DBG_ADDR_SYS` 段，复用 4.2 的 `sys` 总线多路选择——所以调试器看到的 PIC/WDT 等寄存器与 CPU 看到的是同一份。

#### 4.4.3 源码精读

**命令位定义**：控制寄存器各位的位号定义在 localparam（[rtl/zipsystem.v:390-394](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L390-L394)）：

```verilog
localparam HALT_BIT = 0,
          STEP_BIT = 2,
          RESET_BIT = 3,
          CLEAR_CACHE_BIT = 4,
          CATCH_BIT = 5;
```

**请求脉冲**：当调试器写控制寄存器（`dbg_cmd_write`）时，按字节选通生成各请求脉冲：

[rtl/zipsystem.v:681-693](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L681-L693)

```verilog
assign dbg_cmd_write = (dbg_stb)&&(dbg_we)
                  && (dbg_addr[6:5] == DBG_ADDR_CTRL);
assign reset_request = dbg_cmd_write && dbg_cmd_strb[RESET_BIT/8];
assign release_request = dbg_cmd_write && dbg_cmd_strb[HALT_BIT/8];
assign halt_request   = dbg_cmd_write && dbg_cmd_strb[HALT_BIT/8];
assign step_request   = dbg_cmd_write && dbg_cmd_strb[STEP_BIT/8];
```

**`cmd_halt` 状态机**：这是 CPU 暂停/恢复的核心。它在多种条件下置位（写 HALT、写 CPU 寄存器后、单步完一拍后、清缓存、异常捕获），仅在「CPU 已完全停下且收到释放/单步请求」时清零：

[rtl/zipsystem.v:754-800](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L754-L800)（节选关键分支）

```verilog
// 释放: 必须 CPU 已停 + 收到 release/step 请求
if (!cmd_write && cpu_has_halted && dbg_cmd_write
        && (release_request || step_request))
    cmd_halt <= 1'b0;
...
// 暂停原因之一: 调试器请求 halt
if (dbg_cmd_write && halt_request && !step_request)
    cmd_halt <= 1'b1;
// 暂停原因之二: 写 CPU 寄存器前要先停
if (dbg_cpu_write)
    cmd_halt <= 1'b1;
// 暂停原因之三: 单步完一拍后自动停
if (cmd_step && !step_request)
    cmd_halt <= 1'b1;
```

`cmd_halt` 最终驱动 CPU 的 `i_halt`，并作为多数外设的 `i_ce` 反相（`!cmd_halt`），见 [rtl/zipsystem.v:857-858](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L857-L858)。看门狗复位、看门狗总线错误等也会强制 `cmd_reset`（[rtl/zipsystem.v:740-749](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L740-L749)）。

**调试端口时序打拍**：为满足时序，调试端口先过一个 `busdelay` 打一拍（`DELAY_DBG_BUS` 默认开），再进入内部 `dbg_*` 信号（[rtl/zipsystem.v:615-633](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L615-L633)）。

**调试返回多路选择**：读返回时，按 `dbg_addr[6:5]` 的锁存值在「CPU 寄存器」「控制状态」「`sys` 外设」三者间选通：

[rtl/zipsystem.v:1799-1806](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L1799-L1806)

```verilog
casez(dbg_pre_addr)
DBG_ADDR_CPU:  dbg_odata <= cpu_dbg_data;   // CPU 内部寄存器
DBG_ADDR_CTRL: dbg_odata <= dbg_cpu_status; // 控制与状态
default:       dbg_odata <= sys_idata;      // sys 外设(DBG_ADDR_SYS)
endcase
```

#### 4.4.4 代码实践

**实践目标**：把「写控制寄存器 → `cmd_*` 变化 → CPU 反应」这条控制链在源码里走通，并对照规范列出调试器要发的写操作。

**操作步骤**（源码阅读型实践）：

1. 假设调试器要「暂停 CPU」。根据 [rtl/zipsystem.v:390-394](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L390-L394)，HALT 位在第 0 位，对应字节 0。因此写操作应为：`dbg_cyc=1, dbg_stb=1, dbg_we=1, dbg_addr[6:5]=00(CTRL), dbg_data 的字节 0 置 1`。
2. 跟到 [rtl/zipsystem.v:681-693](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L681-L693)：`halt_request` 脉冲拉高一拍。
3. 跟到 [rtl/zipsystem.v:781-782](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L781-L782)：`cmd_halt <= 1'b1`。
4. 跟到 [rtl/zipsystem.v:857-858](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L857-L858)：`cpu_halt = cmd_halt`，送入 `zipwb` 的 `i_halt`，CPU 进入暂停。

**需要观察的现象 / 预期结果**：暂停后，CPU 报告 `cpu_has_halted`；此时调试器可安全地读 `DBG_ADDR_CPU` 区段（[rtl/zipsystem.v:1802](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L1802)）拿到 R0..R15。若未等真正停下就读，可能读到中间态（这正是 [rtl/zipsystem.v:1680-1681](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L1680-L1681) 注释警告的「results may not be what he expects」）。

> 待本地验证：实际驱动调试端口的代码（发 Wishbone 写、读回结果）在 `sw/zipdbg/zipdbg.cpp`，u5-l1 会带读。

#### 4.4.5 小练习与答案

**练习 1**：为什么调试器「写 CPU 内部寄存器」之前必须先暂停 CPU？

> **参考答案**：因为「写 CPU 寄存器」会触发 `dbg_cpu_write`，而 [rtl/zipsystem.v:785-786](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L785-L786) 中 `dbg_cpu_write` 会强制 `cmd_halt <= 1`。换言之，硬件层面对「改寄存器」强制要求 CPU 停下——若 CPU 正在运行，贸然改它的寄存器会破坏正在执行的指令语义，故必须先暂停、再改、再恢复。

**练习 2**：调试器读 `sys` 外设时，会和 CPU 抢同一条 `sys` 总线。谁优先？怎么保证不出错？

> **参考答案**：CPU 优先（4.2 的多路选择把 `cpu_lcl_cyc` 放在条件前）。`dbg_stall` 会在「调试器想访问 `sys` 但 CPU 正在用本地总线」时被拉高（[rtl/zipsystem.v:1810](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L1810)），让调试器等待。实践中调试器通常在 CPU 已暂停（`cpu_lcl_cyc` 基本不活跃）时才访问外设，因此冲突很少发生。

## 5. 综合实践

把本讲四条线索串起来：**画出 `zipsystem` 的内部总线拓扑，并给出每个外设的基地址与译码逻辑。**

请完成下列产出（一张图 + 一张表 + 一段说明）：

1. **拓扑图**：参照 4.1.2，自己重画一张，至少标注：
   - `zipwb`(thecpu) 输出的 `gbl` 与 `lcl` 两路；
   - `gbl` 经 MMU、再经 `wbpriarbiter dmacvcpu`（与 DMA 仲裁）后成为 `o_wb_*`；
   - `lcl` 与调试端口（经 `busdelay`）汇入 `sys` 总线；
   - `sys` 总线经 `sel_*` 译码挂上 PIC/APIC/WDT/WBU/Timer×3/Jiffies/计数器×8/DMA/MMU；
   - `pic_interrupt` 回送到 CPU 的 `i_interrupt`。
2. **地址映射表**：把 4.3.1 的表补全——对每个外设给出 `sys_addr`（字地址）、CPU 字节地址、命中的 `sel_*`、对应源码行号。
3. **跟踪一条访问**：自选一个地址（如「写看门狗 `0xff000004` 喂狗」或「读 Jiffies `0xff00001c`」），写明它从 CPU 发出到拿到返回数据，依次经过哪些信号、哪些源码行。

**自检要点**：

- 你的图里，取指路径是否**只**走 `gbl`？（若是，说明你理解了 4.2.3。）
- DMA 的主端口（`dc_*`）接到了哪里？（应接到 `wbpriarbiter` 的 B 端口。）
- 调试端口访问 MMU 会怎样？（应答：访问不到，见练习 4.3.5-2。）

> 待本地验证：如果已按 u1-l4 跑通过 `zipsys_tb`，可以在测试台里手动往 `0xff000004`（看门狗）写一个值，再用调试端口读 `DBG_ADDR_SYS` 段对应地址，确认读到同样的值——这能验证「CPU 与调试器看到同一份外设」。

## 6. 本讲小结

- `zipsystem` = `zipwb`(CPU) + 一组片内外设 + 一个调试从端口，三者挂在内部 `sys` 总线上，并经仲裁接出对外 Wishbone 主端口。
- 内部有三条总线前缀：`gbl`（外部/取指）、`lcl`（本地外设，仅数据访问命中 `0xFFxxxxxx`）、`sys`（真正实现的外设总线）。
- 仲裁有两处：外部总线用 `wbpriarbiter dmacvcpu`（CPU 优先、DMA 蹭用）；`sys` 总线用简单多路选择（CPU 优先、调试器等空隙）。
- 取指**从不**走本地总线，本地段 `0xFFxxxxxx` 是数据专用的外设窗口；CPU 字节地址与 `sys` 字地址的关系是 \( \text{字节地址} = 0xFF000000 + \text{sys\_addr}\times 4 \)。
- 外设经 `sel_*` 译码选中，读返回由 `ack_idx` 多路选择；中断经 `main/alt_int_vector` 汇入主/辅 PIC，再合并成单线 `pic_interrupt` 喂给 CPU。
- 调试端口用 `dbg_addr[6:5]` 切成 CTRL/CPU/SYS 三段：CTRL 段经命令寄存器 `cmd_halt/cmd_step/cmd_reset/...` 控制 CPU；SYS 段复用 `sys` 总线访问外设；CPU 段读写内部寄存器（改前强制暂停）。

## 7. 下一步学习建议

- **各外设内部原理**：本讲只讲「外设怎么挂上来」。定时器/计数器/Jiffies/看门狗/中断控制器的寄存器位域与工作细节，见 u4-l5；DMA 的搬运状态机见 u4-l6；MMU 见 u4-l7。
- **总线辅助模块**：`wbpriarbiter`、`busdelay`、`sfifo`、`skidbuffer` 以及形式化用的 `fwb_master/slave` 属性封装，见 u4-l4。
- **调试协议细节**：完整调试端口寄存器表、`zipdbg` 调试器主流程，见 u5-l1。
- **自定义 SoC**：把 `zipsystem` 换成你自己的地址译码 + 总线互连 + 外设组合，见 u5-l7。
