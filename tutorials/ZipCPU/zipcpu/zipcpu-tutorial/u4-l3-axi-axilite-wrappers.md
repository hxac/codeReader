# 讲义：AXI 与 AXI-Lite 封装

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 `zipaxil`（AXI4-Lite）与 `zipaxi`（完整 AXI4）这两种顶层封装的端口组成，以及它们为何把**指令总线**与**数据总线**彻底分离。
- 解释 `axilfetch` 如何用单一参数 `FETCH_LIMIT` 在「单条取指 / 双字缓存 / FIFO 预取」三种策略间切换。
- 描述 `axilpipe` 如何通过一个深度为 16 的应答 FIFO 让多条访存请求「在途（outstanding）」，从而把连续访存压到接近 1 拍/条。
- 把握 AXI-Lite 版与完整 AXI 版在取指/访存/缓存模块上的对应关系，并理解 `axidcache` 为何需要 AXI 的突发（burst）能力。

本讲承接 [u4-l1 Wishbone 封装](u4-l1-wishbone-wrapper-zipwb.md)：在那里你已经看到两个 Wishbone 封装（`zipbones`/`zipsystem`）通过 `zipwb` 把取指与访存**仲裁合并成一条总线**；本讲则切换到「指令/数据总线天生分离」的 AXI 世界。

## 2. 前置知识

- **AXI 与 AXI-Lite 协议基础**。AXI4 用**五个独立通道**传输一笔数据：写地址（AW）、写数据（W）、写应答（B）、读地址（AR）、读数据（R）。AXI-Lite 是 AXI4 的精简子集，每笔交易固定传输一个数据字、**没有突发**（即没有 `AxLEN/AxSIZE/AxBURST/RLAST/WLAST` 等字段），地址/控制信号也更少。可以粗略理解为：AXI-Lite ≈ 「一次一个字的 AXI」。
- **主设备（Master）与从设备（Slave）**。本讲里的 `M_INSN_*`、`M_DATA_*` 都是 CPU 作为**主设备**向外发起的端口（CPU 主动读写内存）；`S_DBG_*` 是 CPU 作为**从设备**被调试器读写的端口。
- **在途请求（outstanding request）**。指「已经发出、但还没收到应答」的请求。一条总线同一时刻能容纳多少个在途请求，直接决定了它的吞吐上限。
- **综合期参数（`OPT_*`）是「剪刀」而非「开关」**。这一概念在 u3-l1 已建立：`generate if` 在综合时裁剪电路，参数关闭则对应硬件根本不生成。
- **取指/访存控制器是「夹心层」**。u3-l2、u3-l6 已说明：`zipcore` 内核本身不碰总线信号，取指和访存由内核之外的控制器模块承担，可像夹心饼干一样替换。

> 术语提示：本讲频繁出现 `ARVALID/ARREADY` 这类握手信号。AXI 规则是——**当且仅当 `VALID` 与 `READY` 同一拍都为 1，本次握手成功**。这是理解所有 AXI 模块时序的钥匙。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [rtl/zipaxil.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxil.v) | **AXI4-Lite 顶层封装**：实例化 `zipcore` + `axilfetch` +（`axilpipe` 或 `axilops`），对外暴露分离的指令/数据 AXI-Lite 主端口与一个 AXI-Lite 调试从端口。 |
| [rtl/zipaxi.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxi.v) | **完整 AXI4 顶层封装**：在外壳骨架上把取指升级为 `axiicache`/`axilfetch`、访存升级为 `axidcache`/`axipipe`/`axiops`，端口多了 ID/LEN/SIZE/BURST/LAST 等突发字段。 |
| [rtl/core/axilfetch.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/axilfetch.v) | **AXI-Lite 取指模块**：只读的指令预取器，用 `FETCH_LIMIT` 在无缓存/单字缓存/FIFO 三种模式间切换。 |
| [rtl/core/axilpipe.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/axilpipe.v) | **AXI-Lite 流水线访存模块**：允许最多 16 个在途请求，把连续访存流水化。 |
| [rtl/core/axidcache.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/axidcache.v) | **AXI4 数据缓存**：在 `axipipe` 之上加 Tag 缓存与写直达，miss 时用 AXI 突发整行填充。 |
| [rtl/zipbones.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipbones.v) | Wishbone 精简封装，本讲仅用作**对照**，凸显「单总线合并 vs 双总线分离」。 |

---

## 4. 核心概念与源码讲解

### 4.1 AXI-Lite 顶层 zipaxil：端口与「指令/数据总线分离」

#### 4.1.1 概念说明

回顾 u4-l1：Wishbone 封装 `zipbones` 对外只有**一组**主端口（`o_wb_cyc/o_wb_stb/...`），取指与访存被 `wbdblpriarbiter` 仲裁后挤进同一条总线。AXI 版本走的是另一条路——**哈佛式顶层**：指令主端口 `M_INSN_*` 与数据主端口 `M_DATA_*` 是两组物理上完全独立的 AXI 出口。

为什么 AXI 适合这样做？因为 AXI 的五通道结构天生允许「读地址」与「读数据」分离，把指令读通道和数据读/写通道各自独立，不会像 Wishbone 那样需要共用 `cyc/stb` 而被迫仲裁。代价是：外部互连（interconnect）要接两组主端口，引脚更多；收益是：指令流与数据流互不阻塞，且调试端口还能再独立成第三组 `S_DBG_*`。

#### 4.1.2 核心流程

`zipaxil` 内部三件事并行：

```text
                 ┌──────────── zipcore (纯计算内核，不碰总线) ─────────────┐
                 │  o_pf_*  (取指请求)            o_mem_* (访存请求)        │
                 └──────┬──────────────────────────────┬────────────────────┘
                        │                              │
                  ┌─────▼─────┐                  ┌─────▼──────┐
                  │ axilfetch │  (只读 AR/R)     │ axilpipe   │  (AW/W/B + AR/R)
                  │  指令预取 │   或 axilops     │  流水访存  │   或 axilops
                  └─────┬─────┘                  └─────┬──────┘
                        │                              │
                  M_INSN_AR/R (指令主端口)       M_DATA_*    (数据主端口，5 通道)

   另有 S_DBG_* (AXI-Lite 调试从端口) ── 经 skidbuffer ──> 内核调试寄存器
```

注意：**指令主端口只用到读通道（AR/R）**，因为取指永远是读；写通道（AW/W/B）在顶层被恒置 0。

#### 4.1.3 源码精读

**顶层参数与关键派生参数**。`OPT_LGICACHE`/`OPT_LGDCACHE` 是取指/数据缓存的「对数尺寸」参数，由此派生出三个决定内部选型的 localparam：

[zipaxil.v:305-L309](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxil.v#L305-L309) 定义了 `OPT_PIPELINED_BUS_ACCESS`、`OPT_MEMPIPE`、`OPT_DCACHE`、`FETCH_LIMIT` 四个派生值——它们是后续 `generate if` 选哪个访存/取指模块的依据。

```verilog
localparam [0:0] OPT_PIPELINED_BUS_ACCESS = (OPT_PIPELINED)&&(OPT_LGDCACHE > 1);
localparam [0:0] OPT_MEMPIPE = OPT_PIPELINED_BUS_ACCESS;
localparam [0:0] OPT_DCACHE = (OPT_LGDCACHE > 4);

localparam FETCH_LIMIT = (OPT_LGICACHE < 4) ? (1 << OPT_LGICACHE) : 16;
```

含义：只有当 `OPT_LGDCACHE > 4`（数据缓存 > 16 字节量级）时才启用 `axidcache`（注意 `axidcache` 是 AXI 版，在 `zipaxi` 里用；`zipaxil` 走的是 `axilpipe`/`axilops` 路径）。`FETCH_LIMIT` 把取指缓存尺寸折算成「最多允许多少个在途请求」，交给 `axilfetch`。

**指令主端口（只读）**。[zipaxil.v:L141-L164](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxil.v#L141-L164) 声明了 `M_INSN_*`，可以看到它包含 AW/W/B 通道（被注释为 `coverage_off`），但这些写通道随后被恒置 0：

[zipaxil.v:972-L981](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxil.v#L972-L981) 把指令端口的写通道全部接死、`M_INSN_BREADY` 恒为 1，证明「取指只读」。

```verilog
assign	M_INSN_AWVALID = 0;
assign	M_INSN_WVALID = 0;
assign	M_INSN_WDATA  = 0;
assign	M_INSN_WSTRB  = 0;
assign	M_INSN_BREADY = 1'b1;
```

**数据主端口（全 5 通道）**。[zipaxil.v:L166-L194](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxil.v#L166-L194) 的 `M_DATA_*` 则 AW/W/B/AR/R 五通道齐全——这就是「指令/数据总线分离」在端口表上的直接体现：两组端口，一组只读、一组全能。

**调试从端口**。[zipaxil.v:L105-L135](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxil.v#L105-L135) 的 `S_DBG_*` 是一组 AXI-Lite **从**端口，供调试器 halt/step/读写 CPU 寄存器；它经 `skidbuffer` 缓冲后路由到内核调试逻辑（与 u4-l2 讨论的调试命令寄存器机制一致）。

#### 4.1.4 代码实践

**实践目标**：亲手确认「`zipaxil` 把指令和数据分成两组独立端口，而 `zipbones` 只有一组」。

**操作步骤**：

1. 打开 [zipaxil.v:L138-L195](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxil.v#L138-L195)，分别数 `M_INSN_*` 和 `M_DATA_*` 的端口数量。
2. 打开 [zipbones.v:L113-L121](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipbones.v#L113-L121)，看它的主端口只有 `o_wb_cyc/o_wb_stb/o_wb_we/o_wb_addr/o_wb_data/o_wb_sel` 一组。

**需要观察的现象**：

- `zipaxil` 有 **3 组** AXI 类端口（`M_INSN_*` 指令主、`M_DATA_*` 数据主、`S_DBG_*` 调试从）；
- `zipbones` 只有 **1 组** Wishbone 主端口 + 1 组调试从端口，指令和访存共用同一组 `cyc/stb`。

**预期结果**：你能用一句话总结——「AXI-Lite 封装把 I 和 D 在顶层就拆成两条物理总线，Wishbone 封装则把它们合并成一条、靠内部仲裁分时复用」。

> 该实践为源码阅读型，无需运行；若想运行对照，可参见 u1-l4 的 Verilator 流程分别综合 `zipaxil_tb` 与 `zipbones_tb`。

#### 4.1.5 小练习与答案

**练习 1**：`zipaxil` 的指令主端口为什么把 `M_INSN_AWVALID/M_INSN_WVALID` 恒置 0？
**答案**：取指永远是读操作，永远不会向指令存储器写数据，因此写地址/写数据通道无需使用，置 0 既节省互连资源、也向外部从设备明确声明「我不写」。

**练习 2**：`zipaxil` 默认 `OPT_LGICACHE=0`、`OPT_LGDCACHE=0`，此时 `FETCH_LIMIT` 和 `OPT_DCACHE` 各是多少？
**答案**：`FETCH_LIMIT=(0<4)?(1<<0):16 = 1`；`OPT_DCACHE=(0>4)=0`（即默认不开数据缓存）。

---

### 4.2 axilfetch 取指模块：FETCH_LIMIT 三种取指模式

#### 4.2.1 概念说明

`axilfetch` 是 AXI-Lite 侧的指令预取器，端口形状和 u3-l2 讲过的 Wishbone 取指族（`prefetch`/`dblfetch`/`pfcache`）对应——它把 CPU 的「给我 PC=X 处的指令」请求，翻译成 AXI-Lite 的读地址（AR）/读数据（R）握手。

它最巧妙的设计是：**只用一个参数 `FETCH_LIMIT`（最大在途请求数），就在三种截然不同的取指策略间自动切换**。这让同一个模块既能当最朴素的「一条一条取」，也能当带缓冲的预取器。这与 Wishbone 侧要写四个独立模块（`prefetch/dblfetch/pffifo/pfcache`）形成对比——AXI-Lite 侧把它收编进了一个可参数化的模块。

#### 4.2.2 核心流程

`axilfetch` 只用 AR/R 两个通道（只读）。其核心是一个「请求-缓冲-交付」的循环：

```text
   CPU 要 PC → 若缓冲空/未命中 → 发 ARVALID(ARADDR=PC) ─┐
                                                         │ ARREADY 握手
                                                         ▼
                                              outstanding++
                                              (在途请求计数)
                                                         │
                              总线返回 RVALID/RDATA ─────┘
                                  │
                                  ▼
                         写入内部缓冲(无/单字/FIFO)
                                  │
                                  ▼
                    CPU i_ready 时，按 o_pc 顺序交付 o_insn
```

关键约束是**节流（throttle）**：在途请求加上缓冲里的数据，不得超过 `FETCH_LIMIT`，否则暂停发新 AR。`new_pc`（分支跳转）会触发 `flush`，把在途但已无用的请求清掉。

#### 4.2.3 源码精读

**三种模式的分流入口**。[axilfetch.v:L325-L349](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/axilfetch.v#L325-L349) 用一个三级 `generate if / else if / else` 把 `FETCH_LIMIT` 映射到三种实现：

```verilog
generate if (FETCH_LIMIT <= 1)
begin : NOCACHE           // 模式一：无缓存，单条取指
   ...
end else if (FETCH_LIMIT == 2)
begin : DBLFETCH          // 模式二：单字缓存（双取）
   ...
end else begin : FIFO_FETCH   // 模式三：FIFO 预取
   ...
end endgenerate
```

| `FETCH_LIMIT` | 模式名 | 内部结构 | 行为 |
|---|---|---|---|
| `≤ 1` | `NOCACHE` | 无任何缓存寄存器 | 每次最多 1 个在途请求，取一条用一条，最朴素 |
| `== 2` | `DBLFETCH` | 1 个 `cache_valid`+`cache_data` 寄存器 | 可多缓存 1 个字（双取），对短顺序流略有益 |
| `> 2`（4/8/16） | `FIFO_FETCH` | 一个 `sfifo`（深度 `LGFLEN=$clog2(FETCH_LIMIT)`） | 多个字入队，连续指令近 0 等待，对循环最优 |

**模式一 NOCACHE 细节**。[axilfetch.v:L331-L337](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/axilfetch.v#L331-L337) 中 `fifo_rd`、`fifo_data` 直接由总线返回值驱动，没有寄存器暂存——本质是把 R 通道直通给 CPU。

**模式二 DBLFETCH 细节**。[axilfetch.v:L368-L379](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/axilfetch.v#L368-L379) 维护一个单字缓存 `cache_valid/cache_data`：当 CPU 还在消费当前字、总线又返回了下一个字时，先把下一字锁存进缓存，等 CPU 要时再交付。

**模式三 FIFO_FETCH 细节**。[axilfetch.v:L416-L441](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/axilfetch.v#L416-L441) 实例化一个 `sfifo`（这是 u4-l4 会讲到的通用 FIFO），把返回的指令字排队，CPU 按序出队。

**节流逻辑**。[axilfetch.v:L227-L232](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/axilfetch.v#L227-L232) 是三种模式共用的「别发太多」闸门：

```verilog
if (fill + (M_AXI_ARVALID ? 1:0)
        + ((o_valid &&(!i_ready || out_fill > 1)) ? 1:0)
        >= FETCH_LIMIT)
    M_AXI_ARVALID <= 1'b0;
```

即「已发未回 + 正在发 + 尚未交付」三者之和达到 `FETCH_LIMIT` 就停止发新请求。

**指令访问的 PROT 标识**。[axilfetch.v:124](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/axilfetch.v#L124) 把 `M_AXI_ARPROT` 恒置 `3'b100`，按 AXI 规范这表示「非特权、安全、**指令**访问」——与下面 `axilpipe` 的数据访问 PROT 形成对照。

#### 4.2.4 代码实践

**实践目标**：验证 `FETCH_LIMIT` 三档分别走哪个 `generate` 分支，并理解其对吞吐的影响。

**操作步骤**：

1. 在 [axilfetch.v:L85-L93](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/axilfetch.v#L85-L93) 确认 `LGDEPTH=$clog2(FETCH_LIMIT)+4`、`LGFIFO=$clog2(FETCH_LIMIT)`。
2. 对照下表，把 `FETCH_LIMIT` 取 1/2/16 时分别落到哪个分支、用到的存储资源填出来。

**需要观察的现象 / 预期结果**：

| `FETCH_LIMIT` | 命中分支 | 缓冲资源 | 对一段连续 N 条指令的取指拍数（粗估，总线每拍返回一字时） |
|---|---|---|---|
| 1 | `NOCACHE` | 无 | ≈ N（基本逐条等待） |
| 2 | `DBLFETCH` | 1 个字寄存器 | ≈ N/2 量级改善（预取 1 字） |
| 16 | `FIFO_FETCH` | 16 深度 `sfifo` | 接近 1 拍/条（缓冲填满后连续交付） |

> 表中拍数为原理性估算，**待本地验证**：实际取决于总线延迟与 CPU 消费速度，可用 u5-l3 的 `pfcache_tb` 风格测试台在 Verilator 下测量。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `FETCH_LIMIT` 不直接等于 `OPT_LGICACHE`，而是 `(OPT_LGICACHE<4)?(1<<OPT_LGICACHE):16`？
**答案**：`OPT_LGICACHE` 是「对数尺寸」；`1<<OPT_LGICACHE` 才是真正的字数。上限封顶 16 是为了避免 AXI-Lite（无突发）下在途请求过多导致面积/复杂度失控——更大的指令缓存应改用完整 AXI 的 `axiicache`（见 4.4）。

**练习 2**：`new_pc`（分支跳转）发生时，已经在 FIFO 里但还没交付的指令怎么办？
**答案**：`fifo_reset = i_cpu_reset || i_clear_cache || i_new_pc`（[axilfetch.v:117](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/axilfetch.v#L117)），同时 `flushing` 机制会把仍在途的总线应答排空丢弃，确保跳转后从新 PC 重新取指。

---

### 4.3 axilpipe 流水线访存：让多条访存在途

#### 4.3.1 概念说明

`axilpipe` 是 AXI-Lite 侧的**数据**访存控制器。模块头注释一句话点明了它与兄弟模块 `axilops` 的差别：

> 「Unlike the axilops core, this one will permit multiple requests to be outstanding at any given time.」（[axilpipe.v:L7-L9](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/axilpipe.v#L7-L9)）

这与 u3-l6 讲过的 Wishbone 侧 `memops`（单笔，最多 1 个在途）→ `pipemem`（多笔在途）的演进完全对应。`axilops`=「AXI-Lite 版 memops」，`axilpipe`=「AXI-Lite 版 pipemem」。`zipaxil` 在 `OPT_PIPELINED_BUS_ACCESS` 为真时选 `axilpipe`，否则退回 `axilops`。

#### 4.3.2 核心流程

`axilpipe` 的灵魂是一个深度为 \(2^{\text{LGPIPE}}=2^4=16\) 的**应答 FIFO**，用来把「请求顺序」与「应答顺序」解耦：

```text
CPU 发 i_stb(读/写) ──> 立即发 AW/W 或 AR（不等上一笔回来）
                          │
                          ▼
                 beats_outstanding++   （在途计数，上限 16）
                          │
                  ┌───────┴────────┐
                  │把这次请求的元信│  (目标寄存器号、操作类型、地址低位)
                  │息压入应答 FIFO │  压入 fifo_data[wraddr]
                  └───────┬────────┘
                          │
        总线陆续返回 BVALID(写) / RVALID(读)
                          │
                          ▼
                 beats_outstanding-- ， rdaddr++
                 从 FIFO 读出对应元信息，把 RDATA 拼对齐后交还 CPU
                          │
                          ▼
                   o_valid/o_result/o_wreg
```

只要 FIFO 没满（`beats_outstanding < 16`），CPU 就能不停顿地连续发访存请求——这就是流水线访存把连续访问压到约 1 拍/条的原理。当 FIFO 接近满，`o_pipe_stalled` 拉高反压 CPU。

#### 4.3.3 源码精读

**FIFO 深度与宽度**。[axilpipe.v:128-L130](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/axilpipe.v#L128-L130) 定义 `LGPIPE=4`（深度 16）和 FIFO 槽位宽度 `FIFO_WIDTH`。FIFO 本体是一个寄存器数组：

[axilpipe.v:150](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/axilpipe.v#L150) 声明 `fifo_data[0:((1<<LGPIPE)-1)]`，即 16 个槽。

**在途计数 `beats_outstanding`**。[axilpipe.v:L400-L424](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/axilpipe.v#L400-L424) 用一个大 `casez` 精确计算每拍的在途数变化（AW 握手 +1、W 握手视情况、AR 握手 +1、B/R 返回 −1）。

**反压 `o_pipe_stalled`**。[axilpipe.v:L375-L384](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/axilpipe.v#L375-L384) 把「FIFO 将满」或「正在 flush」或「某通道 VALID 但没 READY」汇总成对 CPU 的反压：

```verilog
always @(*)
begin
    o_pipe_stalled = r_pipe_stalled || r_flushing;
    if (M_AXI_AWVALID && (!M_AXI_AWREADY || misaligned_aw_request))
        o_pipe_stalled = 1;
    ...
end
```

**应答 FIFO 的写入与读出**。[axilpipe.v:L797-L807](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/axilpipe.v#L797-L807) 在每次 AR/W 握手时，把「是否读、目标寄存器号、操作类型、是否未对齐、地址低位」打包写入 `fifo_data[wraddr]`；总线返回时按 `rdaddr` 读出，用以把 `RDATA` 正确地对齐、符号扩展后交还 CPU（[axilpipe.v:L836-L929](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/axilpipe.v#L836-L929)）。

**数据访问的 PROT 标识**。[axilpipe.v:955-L956](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/axilpipe.v#L955-L956) 把 `M_AXI_AWPROT/M_AXI_ARPROT` 置 `3'b000`（数据访问），与 `axilfetch` 的 `3'b100`（指令访问）互补——这也正是 AXI 允许 I/D 分离的协议依据之一。

**永远就绪**。[axilpipe.v:949-L950](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/axilpipe.v#L949-L950) 中 `M_AXI_RREADY=1; M_AXI_BREADY=1;`，说明模块自己随时能接住应答，靠内部 FIFO 兜底，不会因为来不及处理而阻塞总线。

#### 4.3.4 代码实践

**实践目标**：用一个连续 4 次 `LW`（加载字）的访问序列，对比 `axilops`（单笔）与 `axilpipe`（流水）的时钟开销来源。

**操作步骤**：

1. 设想 CPU 顺序发出 4 次读：`LW R1,[R2]; LW R3,[R2+4]; LW R4,[R2+8]; LW R5,[R2+12]`。
2. 在 [axilpipe.v:L400-L424](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/axilpipe.v#L400-L424) 跟踪：第 2、3、4 次请求发出时，`beats_outstanding` 是否仍 < 16（若是，则 CPU 无需停顿）。
3. 对照 `axilops`（在 `zipaxil.v` 的 `BARE_MEM` 分支 [zipaxil.v:L1080-L1143](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxil.v#L1080-L1143)），它每笔必须等 `o_busy` 落下才能发下一笔。

**需要观察的现象**：

- `axilpipe` 下，4 次 AR 可以在总线 ARREADY 跟得上的前提下连续发出，4 次 R 返回后再依次交付；
- `axilops` 下，第 2 次 `LW` 必须等第 1 次完全结束（`o_valid` + `o_busy=0`）才能发出。

**预期结果**：`axilpipe` 节省的时钟来自「请求与应答重叠」+「不打散连续访问」；粗略地，4 次访问 `axilpipe` 约 \(4 + \text{latency}\) 拍，`axilops` 约 \(4 \times \text{latency}\) 拍（`latency` 为单笔往返延迟）。具体数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`axilpipe` 的应答 FIFO 为什么存的是「请求元信息」而不是「数据」？
**答案**：因为数据要等总线返回 `RVALID` 时才有；FIFO 的作用是记住「这一笔应答该交给哪个寄存器、按什么宽度对齐」，让返回的 `RDATA` 能正确归位。它存的是「待办事项」而非「结果」。

**练习 2**：FIFO 满了会怎样？
**答案**：`o_pipe_stalled` 拉高（[axilpipe.v:L301-L308](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/axilpipe.v#L301-L308)），反压 CPU 停止发新请求，等总线消化掉一些在途请求、FIFO 腾出空位后再继续。

---

### 4.4 从 AXI-Lite 到完整 AXI：zipaxi 与 axidcache

#### 4.4.1 概念说明

`zipaxil` 用的是 AXI-Lite，每笔只能搬一个字。一旦要上**数据缓存**（miss 时需一次读回整条缓存行）或要更高带宽，单字传输就不够了——这需要 AXI4 的**突发（burst）**能力：一笔地址（AR）配上 `ARLEN` 个数据（R），一次性读回一整行。

`zipaxi` 就是为此而生：它在 `zipaxil` 的同一套外壳骨架上，把端口扩成完整 AXI4（增加 `AxID/AxLEN/AxSIZE/AxBURST/RLAST/WLAST` 等字段），并把内部模块升级为支持突发的版本——取指用 `axiicache`，访存用 `axidcache`/`axipipe`/`axiops`。

#### 4.4.2 核心流程

`zipaxi` 内部的取指与访存各自由综合期参数三选一：

```text
取指：  OPT_LGICACHE > 4 ?  axiicache   (AXI 突发 + Tag 缓存)
                           ──────────────────────────────
        否则              axilfetch   (退回 AXI-Lite 风格，仅用 AR/R 单字)

访存：  OPT_DCACHE ?         axidcache   (AXI 突发 + Tag 数据缓存)
        else if (PIPELINED && OPT_LGDCACHE>0) ?  axipipe  (AXI 突发流水)
        else                axiops      (AXI 单笔)
```

注意：`zipaxi` 默认 `OPT_LGICACHE=0`、`OPT_LGDCACHE=0`，此时取指走 `axilfetch`（FETCH_LIMIT=1）、访存走 `axiops`——即「完整 AXI 端口，但按单笔用」。要发挥 AXI 突发优势，需要把缓存参数调大。

#### 4.4.3 源码精读

**`zipaxi` 比 `zipaxil` 多出的端口字段**。[zipaxi.v:L148-L193](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxi.v#L148-L193) 的 `M_INSN_*` 比 `zipaxil` 多出 `M_INSN_AWID/AWLEN/AWSIZE/AWBURST/AWLOCK/AWCACHE/AWQOS`、`M_INSN_WLAST`、`M_INSN_BID`、`M_INSN_ARID/ARLEN/ARSIZE/ARBURST/ARLOCK/ARCACHE/ARQOS`、`M_INSN_RID/RLAST`——这些都是突发相关的控制字段。

**新增的顶层参数**。[zipaxi.v:L58-L83](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxi.v#L58-L83) 引入了 `C_AXI_ID_WIDTH`、`INSN_ID`/`DATA_ID`（给两路主端口打不同 ID，便于互连区分）、`OPT_WRAP`（是否用 AXI WRAP 突发，让关键字优先返回）、`LGILINESZ`/`OPT_LGDLINESZ`（指令/数据缓存行尺寸）。

**取指三选一**。[zipaxi.v:L885-L998](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxi.v#L885-L998)：`OPT_LGICACHE > 4` 时实例化 `axiicache`（带 Tag 的指令缓存，miss 时整行突发）；否则实例化 `axilfetch`（与 `zipaxil` 同款），并把多出来的 AXI 突发字段填上单笔常量（如 [zipaxi.v:L982-L990](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxi.v#L982-L990) 把 `M_INSN_ARLEN=0`、`ARBURST=2'b01` 即 INCR）。

**访存三选一**。[zipaxi.v:L1057-L1343](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxi.v#L1057-L1343)：`OPT_DCACHE` 真→`axidcache`；否则 `OPT_PIPELINED_BUS_ACCESS && OPT_LGDCACHE>0`→`axipipe`（AXI 版流水访存）；否则 `axiops`（AXI 版单笔）。

**`axidcache` 的缓存结构**。[axidcache.v:L216-L218](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/axidcache.v#L216-L218) 用三个数组实现直接映射缓存：`cache_valid`（有效位）、`cache_tag[]`（标签）、`cache_mem[]`（数据）。其状态机有四态 `DC_IDLE/DC_WRITE/DC_READS/DC_READC`（[axidcache.v:L100-L103](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/axidcache.v#L100-L103)），miss 时进入 `DC_READC` 用 AXI 突发整行填充；`OPT_WRAP` 打开时可让 CPU 所需的那个字先于整行读完返回（关键字优先），降低 miss 延迟。

**派生尺寸参数**。[axidcache.v:L108-L110](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/axidcache.v#L108-L110) 由 `LGCACHELEN`/`LGNLINES` 推出 `CS`（缓存字数对数）、`LS`（每行字数对数）、`TW`（标签位宽），是理解命中/缺失译码的钥匙。

#### 4.4.4 代码实践

**实践目标**：理解「为什么数据缓存必须用完整 AXI 而非 AXI-Lite」。

**操作步骤**：

1. 假设 `axidcache` 配成 32 字节/行（`LS=3`，即每行 8 个 32 位字）。一次 miss 需要从内存读回 8 个字。
2. 在 [axidcache.v:L179-L181](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/axidcache.v#L179-L181) 看到 `M_AXI_ARLEN/ARSIZE/ARBURST`——这些字段正是用来声明「这一笔要连续读 8 个字」。
3. 想象如果把它降级成 AXI-Lite（没有 `ARLEN`）：要填一行就得发 8 笔独立的 AR、等 8 次 ARREADY 握手，地址开销巨大。

**需要观察的现象**：AXI 突发下，1 次 AR 握手 + 8 次 R 返回即可填满一行；AXI-Lite 下则需要 8 次 AR 握手 + 8 次 R 返回。

**预期结果**：你能解释「`axidcache` 只存在于 `zipaxi` 而不存在于 `zipaxil`」的原因——AXI-Lite 缺乏突发能力，无法高效支撑缓存行填充；`zipaxil` 最多只能用 `axilpipe` 做无缓存的流水访存。

#### 4.4.5 小练习与答案

**练习 1**：`zipaxi` 默认参数下（`OPT_LGICACHE=0, OPT_LGDCACHE=0`）实际用的是哪两个内部模块？
**答案**：取指用 `axilfetch`（`OPT_LGICACHE` 不大于 4，走 else 分支），访存用 `axiops`（`OPT_DCACHE=0` 且 `OPT_LGDCACHE` 不大于 0，走 `BARE_MEM`）。即端口是完整 AXI，但内部按单笔模式工作。

**练习 2**：`OPT_WRAP` 打开后对 miss 延迟有什么影响？
**答案**：AXI WRAP（回绕）突发允许从 CPU 所需地址开始返回数据，再回绕填充整行，于是「关键字」可以先于行内其它字返回，CPU 能更早拿到所需指令/数据继续执行，降低可见的 miss 停顿。

---

## 5. 综合实践

**任务**：为下面的需求选择正确的 ZipCPU 顶层封装与内部取指/访存模块，并说明理由。

> 需求：你要把 ZipCPU 接入一个**已有 AXI4 互连**的 SoC，主存带宽充裕；你希望指令循环尽量快（要指令缓存）、数据访问要缓存且支持突发回填，但不需要片内定时器/看门狗等外设。

请回答：

1. 选 `zipsystem` / `zipbones` / `zipaxil` / `zipaxi` 中的哪一个？为什么？
2. 选定后，取指模块会是 `axilfetch` / `axiicache` 中的哪个？需要把哪个参数调到多大？
3. 访存模块会是 `axiops` / `axipipe` / `axidcache` 中的哪个？受哪些参数控制？
4. 若把同一份设计改用到只有 AXI-Lite 的互连上，访存模块会退化为哪个？还能用数据缓存吗？

**参考思路**：

1. 选 `zipaxi`——需要 AXI4 突发且不要片内外设（`zipbones` 是 Wishbone；`zipsystem` 带 peripherals 且是 Wishbone；`zipaxil` 无突发）。
2. 取指用 `axiicache`，需 `OPT_LGICACHE > 4`（参见 [zipaxi.v:L885-L886](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxi.v#L885-L886)）。
3. 访存用 `axidcache`，需 `OPT_DCACHE=(OPT_LGDCACHE>4)` 为真（参见 [zipaxi.v:L1057-L1058](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxi.v#L1057-L1058)），即把 `OPT_LGDCACHE` 设到大于 4。
4. 改用 AXI-Lite 后应选 `zipaxil`，访存退化为 `axilpipe`（流水）或 `axilops`（单笔）；**不能**用 `axidcache`，因为 AXI-Lite 无突发，无法高效回填缓存行。

## 6. 本讲小结

- `zipaxil`（AXI-Lite）与 `zipaxi`（AXI4）都在顶层把**指令主端口**（`M_INSN_*`，只读）与**数据主端口**（`M_DATA_*`，全通道）物理分离，外加一个独立的 AXI-Lite 调试从端口（`S_DBG_*`），这与 Wishbone 封装「合并单总线」的做法截然不同。
- `axilfetch` 用单一参数 `FETCH_LIMIT` 在三档间切换：`≤1`→`NOCACHE`（单条取指）、`==2`→`DBLFETCH`（单字缓存）、`>2`→`FIFO_FETCH`（`sfifo` 预取）；节流逻辑保证在途请求不超过 `FETCH_LIMIT`。
- `axilpipe` 用一个深度 16 的应答 FIFO 让多条访存请求同时在途，把连续访存压到接近 1 拍/条，靠 `o_pipe_stalled` 在 FIFO 将满时反压 CPU；它是 `axilops`（单笔）的流水升级版。
- `zipaxi` 通过扩出 `AxLEN/ID/LAST` 等突发字段，把取指升级为 `axiicache`、访存升级为 `axidcache`；`axidcache` 用直接映射（`cache_valid/cache_tag/cache_mem`）+ AXI 突发整行回填，`OPT_WRAP` 可让关键字优先返回。
- 默认参数下两种封装都按「单笔」工作；要发挥 AXI 优势必须调大 `OPT_LGICACHE/OPT_LGDCACHE`，这正体现了「`OPT_*` 是综合期剪刀」的一贯设计哲学。

## 7. 下一步学习建议

- **继续往下读调试与系统整合**：本讲的 `S_DBG_*` 调试从端口逻辑（halt/step/reset 命令寄存器）将在 [u4-l2 ZipSystem 整合](u4-l2-zipsystem-integration.md) 与 [u5-l1 调试接口](u5-l1-debug-interface-port.md) 中从「系统地址映射」和「调试协议」两个角度深入展开，建议接着读 u4-l2、u4-l4。
- **想搞清 FIFO 与 skidbuffer**：本讲反复出现的 `sfifo`、`skidbuffer` 是 [u4-l4 总线支持模块 rtl/ex](u4-l4-bus-support-modules.md) 的主角，那里会讲清「背压缓冲」的共性。
- **想做形式化验证**：`axilfetch.v`/`axilpipe.v` 文件后半段大量 `faxil_master`/`fmem` 断言，是 [u5-l2 形式化验证体系](u5-l2-formal-verification.md) 的绝佳案例，可在学完本讲后带着「这些模块怎么被证明正确」的问题去读。
- **想把 CPU 用进自己的 SoC**：本讲是 [u5-l7 自定义 SoC 集成](u5-l7-custom-soc-integration.md) 的直接前置，那里会把 `zipaxil` 与地址译码、互连组合成一个完整可仿真的最小系统。
