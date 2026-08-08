# DMA 控制器：wbdmac 与 zipdma

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清「为什么需要 DMA」：让数据搬运脱离 CPU 的流水线，由独立硬件在总线上完成。
- 区分 ZipCPU 仓库里两套 DMA：legacy 的 `wbdmac`（仅 32 位、块缓冲搬运）和现行 `zipdma`（总线宽度无关、可处理非对齐、读/写地址与位宽独立可配）。
- 读懂 `wbdmac` 的七状态搬运状态机与四个配置寄存器。
- 画出 `zipdma` 顶层的子模块划分（ctrl / fsm / mm2s / gears / sfifo / s2mm / 仲裁器），并解释读通路（mm2s）与写通路（s2mm）为何要拆开。
- 用真实寄存器位定义，配置一次内存块搬运，并解释「源/目的地址独立自增」如何让 DMA 既能搬内存，也能接外设 FIFO。

> 一点必要的澄清：本讲的标题沿用了学习路线里的说法，把 `zipdma` 归为「AXI 上的 scatter-gather」。但读源码会发现：`zipdma` 对**外**仍是一个 **Wishbone** DMA（spec 也明确写「a Wishbone DMA controller … A separate AXI controller is scheduled for development」）；它只是在**内部**用了一套类似 AXI-Stream 的握手（`M_VALID/M_READY/M_DATA/M_BYTES/M_LAST`）把读侧和写侧解耦。而「scatter-gather」也并非完整的描述符链 DMA，而是靠「读/写地址与位宽各自独立」实现的一种类 scatter/gather 能力（例如一侧固定地址、另一侧自增）。本讲会忠于源码讲清这些。

## 2. 前置知识

- **DMA（Direct Memory Access）**：一块不经过 CPU、直接在总线主设备之间搬数据的硬件。CPU 只负责「下发一次搬运任务」，搬运期间的取指/访存停顿都交给 DMA。
- **总线主/从（master/slave）**：DMA 有两个方向相反的总线端口——**从端口**接收 CPU 的配置（写寄存器），**主端口**对外发起真实的读/写交易。这和第 u4-1、u4-2 讲里 `zipcore` 既是总线主、又被调试端口当从访问是一回事。
- **Wishbone 的半双工特性**：一次 Wishbone 交易要么读、要么写，不能同时进行。这决定了 DMA「先批量读进缓冲、再批量写出」的工作方式。
- **AXI-Stream 握手**：`VALID/READY` 双向握手，配 `DATA`、`LAST`（包尾）、`BYTES`（本拍有效字节数）。本讲里 `mm2s`/`s2mm` 内部用它，但顶层对外仍是 Wishbone。
- **可综合参数（综合期剪刀）**：第 u3 系列讲过 `OPT_*` 参数在综合期裁剪电路。本讲里 `wbdmac` 的 `LGMEMLEN`、`zipdma` 的 `BUS_WIDTH` 同理。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [rtl/peripherals/wbdmac.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/wbdmac.v) | **legacy** Wishbone DMA：32 位、内部块缓冲、七状态搬运状态机。仍保留供形式化验证与旧设计兼容，但已不在 ZipSystem 中实例化。 |
| [rtl/zipdma/zipdma.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipdma/zipdma.v) | **现行** Wishbone DMA 顶层：把 ctrl/fsm/mm2s/gears/sfifo/s2mm/仲裁器拼成一颗完整 DMA。被 ZipSystem 实例化。 |
| [rtl/zipdma/zipdma_ctrl.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipdma/zipdma_ctrl.v) | 寄存器/控制接口：解析 CPU 的 4 个寄存器读写，产出 `dma_request/abort/src/dst/length/inc/size/trigger/interrupt`。 |
| [rtl/zipdma/zipdma_fsm.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipdma/zipdma_fsm.v) | 顶层搬运调度：因为 Wishbone 半双工，把长搬运切成「读一包 → 写一包」交替。 |
| [rtl/zipdma/zipdma_mm2s.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipdma/zipdma_mm2s.v) | Memory→Stream 读通路：从总线读，对齐后以 AXI-Stream 形式吐出。 |
| [rtl/zipdma/zipdma_s2mm.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipdma/zipdma_s2mm.v) | Stream→Memory 写通路：吃进 AXI-Stream，写到总线。 |
| [rtl/zipsystem.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v) | 在 `OPT_DMA` 下实例化 `zipdma`，地址 `0xff000040`。 |
| [doc/src/spec.tex](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex) | `ZipDMA Controller` 一节是现行 DMA 的权威寄存器定义。 |
| [sim/zipsw/dmatest.c](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/zipsw/dmatest.c) | 配套 DMA 测试程序，是本讲实践任务的依据。 |

---

## 4. 核心概念与源码讲解

### 4.1 wbdmac：legacy 块缓冲搬运状态机

#### 4.1.1 概念说明

`wbdmac` 是 ZipCPU 最早期的 DMA：**32 位、只支持对齐字访问**、内部用一块 SRAM 缓冲把「读」和「写」隔开。它的设计哲学是「先把一整块（最多 1kW=4kB）读进内部缓冲，再整块写出」。spec 里那段对 DMA 的总体描述（搬一个字约 8 拍、流水代价 \(14+2N\)）最早就是按它刻画的。

它现在已是 **legacy**：ZipSystem 实际用的是 `zipdma`（见 4.2）。但 `wbdmac` 仍在仓库里，原因有二——它是理解「DMA 状态机」最简单的范本，且它仍带完整的形式化证明（`bench/formal/wbdmac.sby`）。所以本讲用它当入门「最小 DMA」。

#### 4.1.2 核心流程

`wbdmac` 用四个 32 位寄存器配置（从端口 `i_swb_addr` 两根线选 0/1/2/3）：

| 从地址 | 寄存器 | 含义 |
| --- | --- | --- |
| 0 | 控制/状态 | 启停、自增开关、触发源、块长度等 |
| 1 | length | **总**搬运字数 |
| 2 | source addr | 读起始地址 |
| 3 | dest addr | 写起始地址 |

搬运由一个七状态机驱动（状态名见源码）：

```
IDLE → WAIT → READ_REQ → READ_ACK → PRE_WRITE → WRITE_REQ → WRITE_ACK
                          (读满一块)              (写空一块)
                                                              │
                                          cfg_len>1 ?  ←──────┘
                                            是 → 回 WAIT 读下一块
                                            否 → IDLE（拉 o_interrupt）
```

要点：

1. **IDLE** 锁存配置；CPU 写控制寄存器并满足启动条件后进入 **WAIT**。
2. **WAIT** 等 `trigger`（立即触发或外部中断触发）后进入读阶段。
3. **READ_REQ / READ_ACK**：先发读请求（`o_mwb_cyc/stb`，`o_mwb_we=0`），把一整块读进内部 `dma_mem`；`READ_ACK` 专门排空在途的读应答。
4. **PRE_WRITE**：把主端口地址切到 `cfg_waddr`（目的）。
5. **WRITE_REQ / WRITE_ACK**：把缓冲里的数据写出（`o_mwb_we=1`）。
6. 一块写完后，若总长还没搬完，回 `WAIT` 读下一块；搬完则回 `IDLE` 并拉一拍 `o_interrupt`。

注意「块长度」与「总长度」是两个量：总长度 = 一共要搬多少字；块长度（`transfer_len`）= 一次读进缓冲多少字。DMA 把总长度切成若干块依次搬。

#### 4.1.3 源码精读

**端口与参数**——从端口（CPU 配置）+ 主端口（对外 Wishbone）+ 中断：

[rtl/peripherals/wbdmac.v:114-150](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/wbdmac.v#L114-L150) 定义了模块与两套端口。注意默认 `BUS_WIDTH=32`、`LGMEMLEN=10`，所以内部缓冲最多 \(2^{10}=1024\) 个字：

[rtl/peripherals/wbdmac.v:178](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/wbdmac.v#L178) —— `dma_mem` 就是那块把「读」和「写」隔开的内部 SRAM。

**七状态机定义**：

[rtl/peripherals/wbdmac.v:155-161](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/wbdmac.v#L155-L161) 列出全部七个状态编码。

**IDLE 里的配置锁存与启动判定**：

[rtl/peripherals/wbdmac.v:249-277](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/wbdmac.v#L249-L277) 是理解「怎么启动一次搬运」的关键。其中：

- `s_addr==2'b00` 分支解析控制字：`cfg_incs <= !s_data[29]`、`cfg_incd <= !s_data[28]`（位 29/28 为 1 表示**不**自增源/目的地址）；`cfg_dev_trigger <= s_data[14:10]`、`cfg_on_dev_trigger <= s_data[15]`（中断触发源）；块长度取低 `LGMEMLEN` 位。
- `s_addr==2'b10` 写源地址、`s_addr==2'b11` 写目的地址。
- `s_addr==2'b01` 的长度写在另一处：[rtl/peripherals/wbdmac.v:393-408](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/wbdmac.v#L393-L408)，每次写应答 `cfg_len` 自减，到 0 停。

> ⚠️ 文件头注释（约 29–61 行）对控制位与「启动魔数」的描述与实际代码已有出入（注释里写 `12'h3db`，代码里判的是 `s_data[27:16]==12'hfed`）。`wbdmac` 是 legacy，注释未同步。**以代码为准**，这也是为什么现行设计改用了 `zipdma`。

**主端口信号派生**——告诉总线「我在读还是在写」：

[rtl/peripherals/wbdmac.v:551-561](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/wbdmac.v#L551-L561) 显示 `o_mwb_cyc/stb/we` 完全由 `dma_state` 译出：`READ_REQ/READ_ACK` 期间 cyc 有效，仅 `READ_REQ` 发 stb；`PRE_WRITE/WRITE_REQ/WRITE_ACK` 期间 `o_mwb_we=1`。

**完成中断**：

[rtl/peripherals/wbdmac.v:379-387](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/wbdmac.v#L379-L387) ——「最后一块的最后一个写应答」或「搬运途中遇到总线错误」时拉 `o_interrupt`。

**寄存器回读**：

[rtl/peripherals/wbdmac.v:600-613](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/wbdmac.v#L600-L613) 让 CPU 能查状态：地址 0 回读 `{忙, 错, !incs, !incd, 0, nread, …}`，其中 `nread` 是「已读但未必已写」的字数，配合 `cfg_len` 可算出搬运进度。

#### 4.1.4 代码实践（源码阅读型）

**目标**：搞清触发一次 `wbdmac` 搬运要配哪些寄存器、按什么顺序。

**步骤**：

1. 打开 [rtl/peripherals/wbdmac.v:249-277](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/wbdmac.v#L249-L277)，确认四个寄存器的从地址编号。
2. 追踪启动条件：要让 `dma_state` 从 `IDLE` 进入 `WAIT`，控制字必须同时满足 `s_data[27:16]==12'hfed`、`s_data[31:30]==2'b00`、`cfg_len_nonzero`（长度非零）。
3. 追踪 `trigger` 的产生 [rtl/peripherals/wbdmac.v:621-629](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/wbdmac.v#L621-L629)：`cfg_on_dev_trigger==0` 时立即触发；为 1 时要等 `i_dev_ints[cfg_dev_trigger]`。
4. 写出配置序列（伪代码）：
   ```
   写 地址1 ← 总字数 N            ; length
   写 地址2 ← 源地址 src          ; source
   写 地址3 ← 目的地址 dst        ; dest
   写 地址0 ← {2'b00, 12'hfed, 自增/触发位, 块长度}  ; 启动
   ```

**需要观察的现象**：配置完成后 `dma_state` 应离开 `IDLE`；回读地址 0 的最高位（忙标志）在搬运期间为 1，搬完回 0 并伴随 `o_interrupt` 一拍。

**预期结果**：能列出「length / source / dest / control」四个寄存器，并指出 control 里位 29/28 控制源/目的自增、位 15/14:10 控制中断触发。具体魔数以代码为准（见上面 ⚠️）。运行层面：**待本地验证**（`wbdmac` 未挂进 ZipSystem，需自行搭建测试台或用 `bench/formal/wbdmac.sby` 做形式化检查）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `wbdmac` 要有 `READ_ACK` 和 `WRITE_ACK` 两个独立状态，而不是发完请求就直接进 PRE_WRITE？

**参考答案**：Wishbone 流水读会同时存在多个在途应答。`READ_REQ` 负责继续发新请求，但必须等所有已发出的读应答（`i_mwb_ack`）都被收进 `dma_mem` 后才能切到写，否则缓冲里数据不全。`READ_ACK` 专门用来「排空」这些在途应答（见状态机里 `last_read_ack` 的判定）。写侧同理。

**练习 2**：若想让 `wbdmac` 从一个外设 FIFO（固定地址）每次读一字、写入自增的内存缓冲，控制字该怎么设？

**参考答案**：源地址不自增 → 位 29 置 1（`cfg_incs = !bit29 = 0`）；目的地址自增 → 位 28 置 0；若要等外设「数据就绪」中断再读，置位 15 并在 14:10 填中断号。

---

### 4.2 zipdma 顶层与子模块划分

#### 4.2.1 概念说明

`zipdma` 是 Version 3 起替代 `wbdmac` 的「升级版 Wishbone DMA」。spec 给它的定位是：**处理非对齐传输、与总线宽度无关**（`BUS_WIDTH` 可参数化，在 ZipSystem 里按数据宽度 `DW` 实例化）。它解决了 `wbdmac` 的三个短板：只支持 32 位、只支持对齐字、读/写宽度不能分别配。

它的核心结构思想是**把「读通路」和「写通路」彻底拆成两个独立引擎，中间用 FIFO 解耦**：

- **mm2s**（Memory to Stream）：只读总线，吐 AXI-Stream。
- **s2mm**（Stream to Memory）：只写总线，吃 AXI-Stream。
- 中间一个 **sfifo** 吸收两边速率差，外加两组 **gears**（对齐/字节序齿轮）做数据重排。

因为读、写是两个独立引擎，所以**源侧和目的侧的「是否自增」与「每次搬几位」可以各自配置**——这正是它「类 scatter/gather」能力的来源（例如源侧固定地址读外设、目的侧自增写内存）。

#### 4.2.2 核心流程：顶层把八块拼起来

`zipdma` 顶层实例化了 8 个子模块，数据流如下：

```
                    ┌─────────── zipdma_ctrl ───────────┐
   CPU ──WB从端口──▶│ 寄存器解析，产出 request/src/dst/  │──▶ 控制/状态
                    │ len/inc/size/trigger/interrupt     │
                    └────────────┬───────────────────────┘
                                 ▼
                    ┌─────────── zipdma_fsm ────────────┐
                    │ 把长搬运切成「读包→写包」交替调度   │
                    │ S_IDLE→S_WAIT→S_READ→S_WRITE→...   │
                    └──┬──────────────────────────┬──────┘
                  mm2s_request              s2mm_request
                       ▼                          ▼
   WB读 ◀──〔mm2s〕── M(AXI-Stream) ──▶ rxgears ──▶ sfifo ──▶ txgears ──▶ S(AXI-Stream) ──▶〔s2mm〕──▶ WB写
                                                                       (两个引擎经 wbarbiter 共享一个 WB 主端口)
```

为什么读/写要拆开还要共享主端口？因为 Wishbone 半双工，`fsm` 让 mm2s 和 s2mm 分时占用同一个对外主端口（`wbarbiter` 做仲裁）。

`zipdma_fsm` 顶部的注释把这件事说得很直白：

[rtl/zipdma/zipdma_fsm.v:8-13](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipdma/zipdma_fsm.v#L8-L13) ——「Wishbone 一次只能读或写，所以大搬运要拆成读、写交替」。

#### 4.2.3 源码精读

**顶层端口**——从端口（配置）+ 主端口（对外 Wishbone）+ 中断，结构与 `wbdmac` 同形：

[rtl/zipdma/zipdma.v:39-83](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipdma/zipdma.v#L39-L83)。注意默认 `BUS_WIDTH=512`（模块作者按宽总线写），但实例化时会被覆盖。

**控制子模块 zipdma_ctrl**——解析 4 个寄存器（注意顺序与 `wbdmac` 不同：0=控制、1=源、2=目的、3=长度）：

[rtl/zipdma/zipdma.v:145-182](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipdma/zipdma.v#L145-L182) 实例化 `u_controller`。

控制寄存器的位定义在 ctrl 里组装成一个 `w_control_reg`：

[rtl/zipdma/zipdma_ctrl.v:113-128](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipdma/zipdma_ctrl.v#L113-L128)，与 spec 表完全对应：

| 位 | 含义 |
| --- | --- |
| 31 R | DMA 忙（`i_dma_busy`） |
| 30 R | 总线错误/中止（`r_err || i_dma_err`） |
| 29 R/W | 中断触发使能（`int_trigger`） |
| 28–24 R/W | 触发用中断号（`int_sel`） |
| 22 R/W | 置 1 → **不**自增目的地址（`!o_s2mm_inc`） |
| 21–20 R/W | 目的侧每次位宽（`o_s2mm_size`） |
| 18 R/W | 置 1 → **不**自增源地址（`!o_mm2s_inc`） |
| 17–16 R/W | 源侧每次位宽（`o_mm2s_size`） |
| 11–0 R/W | 中间搬运包长（`o_transferlen`，0 表最大） |

位宽枚举（spec `tbl:zipdma-size`，源码 `SZ_*`）：`2'b00`=整总线、`2'b01`=32 位、`2'b10`=16 位、`2'b11`=8 位，见 [rtl/zipdma/zipdma_mm2s.v:103-106](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipdma/zipdma_mm2s.v#L103-L106)。

**启动与中止**：spec 说「向位 31 写 0 启动」，代码里这发生在控制寄存器写译码中：

[rtl/zipdma/zipdma_ctrl.v:278-326](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipdma/zipdma_ctrl.v#L278-L326) —— 关键是 `if (!i_data[31] && (!r_err || i_data[30])) o_dma_request <= !r_zero_len;`（位 31 写 0 且无未清错误就发起）；中止则用魔数 `ABORT_KEY = 32'h41425254`（"ABRT"，见 [rtl/zipdma/zipdma_ctrl.v:47](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipdma/zipdma_ctrl.v#L47) 与第 309 行）。

**搬运调度 zipdma_fsm**——四状态切包：

[rtl/zipdma/zipdma_fsm.v:160-206](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipdma/zipdma_fsm.v#L160-L206)。`S_READ` 里等 mm2s 把一包读完、地址按 `i_mm2s_inc` 推进；`S_WRITE` 里等 s2mm 把这包写完、地址按 `i_s2mm_inc` 推进；剩余长度 `r_length` 扣完即结束。注意 `o_mm2s_transferlen`/`o_s2mm_transferlen` 都等于 `r_transferlen`（同一包长）：

[rtl/zipdma/zipdma_fsm.v:212-214](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipdma/zipdma_fsm.v#L212-L214)。

**读/写两条引擎**——顶层实例化：

- mm2s：[rtl/zipdma/zipdma.v:224-266](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipdma/zipdma.v#L224-L266)，主端口只读、副产 AXI-Stream。
- s2mm：[rtl/zipdma/zipdma.v:334-372](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipdma/zipdma.v#L334-L372)，吃 AXI-Stream、主端口只写。

**中间 FIFO 与齿轮**：rxgears [rtl/zipdma/zipdma.v:268-285](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipdma/zipdma.v#L268-L285)、sfifo [rtl/zipdma/zipdma.v:287-305](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipdma/zipdma.v#L287-L305)（`rx_ready=!sfifo_full`、`tx_valid=!sfifo_empty`，见 307–308 行）、txgears [rtl/zipdma/zipdma.v:310-332](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipdma/zipdma.v#L310-L332)。

**主端口仲裁**——读/写两个引擎共享一个对外 Wishbone 主端口：

[rtl/zipdma/zipdma.v:374-412](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipdma/zipdma.v#L374-L412)，`wbarbiter u_arbiter` 把 mm2s 的读主（A 路）与 s2mm 的写主（B 路）合一路输出，再连到顶层 `o_mwb_*`。

**集成进 ZipSystem**——`OPT_DMA` 下唯一被实例化的就是 `zipdma`（不是 `wbdmac`）：

[rtl/zipsystem.v:1199-1225](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipsystem.v#L1199-L1225)，基址 `0xff000040`（与 spec [doc/src/spec.tex:2926-2929](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L2926-L2929) 的 DMACTRL/DMALEN/DMASRC/DMADST 一致）。

#### 4.2.4 代码实践（可运行 + 阅读混合）

**目标**：用仓库自带的 `dmatest.c` 跑一次真实搬运，并对照源码确认寄存器顺序。

**步骤**：

1. 读 [sim/zipsw/dmatest.c:62-67](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/zipsw/dmatest.c#L62-L67)：`ZIPDMA` 结构体按 `d_ctrl/d_src/d_dst/d_len` 顺序排列，基址 `0xff000040`（第 69 行）。
2. 读搬运函数 [sim/zipsw/dmatest.c:101-123](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/zipsw/dmatest.c#L101-L123) `dma_memcpy`：先写 src/dst/len，最后写 `d_ctrl=DMACMD_MEMCPY`（=0，即位 31 写 0 启动）触发；然后 `while(BUSY) NOOP` 等完成；完成后 `CLEAR_DCACHE`（因为 DMA 改了内存，CPU 数据缓存已失效）。
3. 注意它读回 `d_ctrl` 判断 `ZIPDMA_BUSY=0x80000000` 与 `ZIPDMA_ERR=0x40000000`，与 4.2.3 的位定义一致。
4. 在仿真环境编译运行：参照第 u1-4 讲的 sim/verilator 流程，把 `dmatest` 作为待测 ELF 加载（配套检查模块 [sim/rtl/zipdma_check.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/rtl/zipdma_check.v) 提供 LFSR 参考数据）。

**需要观察的现象**：串口依次打印 `Basic MEMCPY( 8b/16b/32b/ BUS)` 各档位宽的 `PASS`；最后打印 `SUCCESS! All tests pass`。若某档打印 `FAIL` 或 `ERR: DMA transfer failed`，对照位定义检查是不是位宽/对齐设错。

**预期结果**：所有档位通过。若你无法本地构建工具链/仿真器，明确标注「待本地验证」，但应能解释每条 `PASS` 对应一次「配 src/dst/len → 写 ctrl 启动 → 等 BUSY 清零 → 校验数据」的完整往返。

#### 4.2.5 小练习与答案

**练习 1**：`zipdma` 的 4 个寄存器顺序（控制/源/目的/长度）和 `wbdmac`（控制/长度/源/目的）不同。从驱动编写角度，这种差异为什么必须留意？

**参考答案**：DMA 的从地址只有 2 根线（`i_swb_addr[1:0]`），同一个编号在两代 DMA 上指向不同寄存器。若把 `wbdmac` 的驱动搬到 `zipdma` 上，会把长度写进源地址、源地址写进长度，导致搬运长度错乱或地址非法。这就是为什么 `dmatest.c` 里 `ZIPDMA` 结构体字段顺序必须严格匹配 `zipdma_ctrl`。

**练习 2**：为什么 `zipdma` 要在 mm2s 与 s2mm 之间放一个 `sfifo`，而 `wbdmac` 不需要？

**参考答案**：`wbdmac` 是「整块读完再整块写」的同步块缓冲，读写不会同时发生，用一块 SRAM 即可。`zipdma` 把读、写拆成两个可并发引擎，两边速率不同（读侧可能突发回填、写侧可能被总线反压），`sfifo` 用来吸收速率差、解耦两边握手；`gears` 再做字节对齐与大小端重排。

---

### 4.3 mm2s 与 s2mm：读/写数据通路

#### 4.3.1 概念说明

mm2s 与 s2mm 是 `zipdma` 真正干活的两个引擎，对称但方向相反：

- **mm2s（读通路）**：Wishbone **只读**主设备 → 产出 AXI-Stream（`M_VALID/M_READY/M_DATA/M_BYTES/M_LAST`）。负责地址推进、字节使能（`o_rd_sel`）、非对齐与子字宽的数据移位对齐。
- **s2mm（写通路）**：消费 AXI-Stream（`S_VALID/S_READY/...`）→ Wishbone **只写**主设备。负责把流里的数据按地址、字节使能写回。

二者最显眼的特征是「方向被焊死」：mm2s 的 `o_rd_we` 恒为 0，s2mm 的 `o_wr_we` 恒为 1。这让仲裁器可以放心地让它们分时共享主端口。

#### 4.3.2 核心流程（以 mm2s 为例）

mm2s 收到 `i_request` 后锁存配置（`r_inc/r_size/r_transferlen/r_addr`），然后：

1. **发读请求**：拉 `o_rd_cyc/o_rd_stb`，按 `r_size` 计算本拍字节使能 `o_rd_sel` 与字节数 `rdstb_size`；若 `r_inc` 则地址推进。
2. **跟踪在途**：`wb_outstanding` 记录「已发请求 − 已收应答」的数量，扣完才撤 `o_rd_cyc`。
3. **收集应答**：每个 `i_rd_ack` 把 `i_rd_data` 经 `pre_shifted_data` 移位对齐后存入移位寄存器 `sreg`，同时累计 `fill`（当前攒了多少字节）。
4. **输出流**：当攒够一拍（`fill >= DW/8` 或包尾）就拉 `M_VALID`，把 `sreg` 作为 `M_DATA`、`fill` 作为 `M_BYTES` 吐出；最后一拍拉 `M_LAST`。
5. **结束**：`r_transferlen` 扣到 0 且最后一拍被下游 `M_READY` 接走后，`o_busy` 清零。

s2mm 是镜像：收 `S_VALID` → 算写地址/字节使能 → 发 `o_wr_cyc/o_wr_stb` → 跟踪在途 → `i_wr_ack` 回来扣 `wb_outstanding`。

关键点是**两侧的位宽与自增独立**：mm2s 用 `i_inc/i_size`（来自 `o_mm2s_inc/o_mm2s_size`），s2mm 用 `i_inc/i_size`（来自 `o_s2mm_inc/o_s2mm_size`）。于是「8 位外设 → 整总线内存」「整总线内存 → 32 位外设」这类跨宽度搬运都能做。

#### 4.3.3 源码精读

**方向焊死**——mm2s 只读、s2mm 只写：

[rtl/zipdma/zipdma_mm2s.v:135](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipdma/zipdma_mm2s.v#L135) `assign o_rd_we = 1'b0;`
[rtl/zipdma/zipdma_s2mm.v:117](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipdma/zipdma_s2mm.v#L117) `assign o_wr_we = 1'b1;`

**配置锁存**（mm2s）：

[rtl/zipdma/zipdma_mm2s.v:140-147](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipdma/zipdma_mm2s.v#L140-L147) 在 `!o_busy` 且（低功耗时需 `i_request`）时把 `i_inc/i_size/i_transferlen/i_addr` 抓进 `r_*`，之后整段搬运都用锁存值。

**主端口读写驱动与状态推进**（mm2s 核心 always 块）：

[rtl/zipdma/zipdma_mm2s.v:271-357](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipdma/zipdma_mm2s.v#L271-L357)。注意第 326–332 行：剩余长度 `rdstb_len` 还够就继续发 `o_rd_stb`，否则准备收尾；第 352–353 行：在途归零且不再发 stb 时撤 `o_rd_cyc`；第 355–356 行：最后一拍流走后清 `o_busy`。

**在途计数**（mm2s）：

[rtl/zipdma/zipdma_mm2s.v:628-640](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipdma/zipdma_mm2s.v#L628-L640) —— 经典的「发 +1、ack −1」在途计数，是它支持流水突发读的根基（在途请求可大于 1，与第 u3-6 讲 `pipemem` 的思路一致）。

**流输出与对齐**（mm2s）：

[rtl/zipdma/zipdma_mm2s.v:851-854](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipdma/zipdma_mm2s.v#L851-L854) 把 `m_valid/sreg/m_bytes/m_last` 接到对外流端口；其中 `sreg` 的移位对齐逻辑在 [rtl/zipdma/zipdma_mm2s.v:773-795](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipdma/zipdma_mm2s.v#L773-L795)（`pre_shifted_data` 按 `pre_shift` 做大端/小端移位）。

**位宽枚举**——两边共用同一套 `SZ_*`：

mm2s 见上文 [rtl/zipdma/zipdma_mm2s.v:103-106](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipdma/zipdma_mm2s.v#L103-L106)；s2mm 见 [rtl/zipdma/zipdma_s2mm.v:94-97](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipdma/zipdma_s2mm.v#L94-L97)。

#### 4.3.4 代码实践（源码阅读型）

**目标**：跟踪一次「整总线、自增」的内存拷贝在 mm2s/s2mm 里各走了哪几步。

**步骤**：

1. 假设 `r_size=SZ_BUS`（整总线）、`r_inc=1`、`r_transferlen=N`（字节）。
2. 在 [rtl/zipdma/zipdma_mm2s.v:271-357](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipdma/zipdma_mm2s.v#L271-L357) 里确认：每拍 `o_rd_sel` 全 1、地址每拍 `+DW/8`、`rdstb_len` 每拍扣 `DW/8`。
3. 在 [rtl/zipdma/zipdma_mm2s.v:727-739](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipdma/zipdma_mm2s.v#L727-L739) 确认 `m_valid` 在每个 `i_rd_ack` 后被拉起（整总线时一拍即满）。
4. 切到 s2mm [rtl/zipdma/zipdma_s2mm.v:117](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipdma/zipdma_s2mm.v#L117) 起的同名信号，确认它把流的 `S_DATA` 直接接到 `o_wr_data`、`S_BYTES` 接成 `o_wr_sel`。

**需要观察的现象**：整总线模式下，mm2s 每个读应答对应一拍 `M_VALID`，s2mm 每拍 `S_VALID` 对应一次写请求，链路近似「1 拍读 → 1 拍写」。

**预期结果**：能用一句话描述「读应答 → 移位对齐 → 进 FIFO → 出 FIFO → 写请求」的端到端路径。子字宽/非对齐情形（如 `SZ_BYTE`）则要额外解释 `o_rd_sel` 的逐字节移位与 `fill`/`m_bytes` 的非满拍处理——这部分较复杂，可作为进阶阅读。

#### 4.3.5 小练习与答案

**练习 1**：mm2s 的注释里特别标注 `M_READY // *MUST* be 1`（[rtl/zipdma/zipdma_mm2s.v:86](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipdma/zipdma_mm2s.v#L86)）。为什么 mm2s 不能容忍下游反压？

**参考答案**：下游是 `rxgears + sfifo`。FIFO 满会通过 `rx_ready = !sfifo_full` 反映到 `M_READY`；但 mm2s 自身的读请求一旦发出，总线应答就会回来，它没有地方暂存「下游不收」的数据。所以工程上靠把 FIFO 做得足够深（`LGMEMLEN` 决定）来保证 mm2s 输出时 FIFO 不会满，形式化证明里也直接 `assume(M_READY)`（见文件末 `// "Careless" assumptions` 段）。

**练习 2**：如果想让 DMA 从一个 8 位 UART 数据寄存器（固定地址）连续读到内存缓冲，`zipdma` 控制寄存器该怎么配？

**参考答案**：源侧位宽 `o_mm2s_size = 2'b11`（8 位）、源不自增 `o_mm2s_inc = 0`（即控制位 18 置 1）；目的侧位宽 `o_s2mm_size = 2'b00`（整总线）或按缓冲对齐选、目的自增 `o_s2mm_inc = 1`（位 22 置 0）；若要等 UART「数据就绪」再读，再配位 29 与 28:24 的中断号。这正是 spec 里 `32'h20070001` 那个例子的由来（[doc/src/spec.tex:3290-3298](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L3290-L3298)）。

---

## 5. 综合实践

**任务**：用 `zipdma` 实现「内存 → 内存」拷贝，并改造为「外设 FIFO → 内存」的 gather 搬运。

1. **搭环境**：按 u1-4 的方式准备 sim/verilator 与 `zip-gcc` 工具链；确认 `OPT_DMA` 打开的 ZipSystem（`zipsys_tb`）可用，DMA 基址 `0xff000040`。
2. **内存→内存**：参考 [sim/zipsw/dmatest.c:101-123](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/zipsw/dmatest.c#L101-L123) 的 `dma_memcpy`，写一段程序：在 `src` 数组填已知模式，配置 `d_src/d_dst/d_len`，写 `d_ctrl=0` 启动，`while(BUSY)` 等待，然后 `CLEAR_DCACHE` 并用 `memcmp` 校验 `dst`。
3. **改造为 gather**：把 `d_ctrl` 改成 `DMACMD_MEMCPY | ZIPDMA_SINC`（源不自增），让源地址固定指向一个外设寄存器（可用一个内存映射的计数器外设模拟），观察 `dst` 里是否被连续填入「同一地址反复读到的值」。对照 4.3.5 练习 2 解释位设置。
4. **读时序**：在 `zipdma.v` 顶层信号 `o_mwb_cyc/o_mwb_we` 上观察「先持续读、再持续写」的包交替（`zipdma_fsm` 的 `S_READ/S_WRITE`），验证 Wishbone 半双工特性。
5. **对照两代 DMA**：列出 `wbdmac` 与 `zipdma` 在「寄存器顺序、支持位宽、对齐要求、读/写是否独立配置」四点上的差异表。

**验收**：能跑通内存拷贝并打印校验通过；能画出从 `d_ctrl` 写 0 到 `o_interrupt` 拉起的完整信号路径（ctrl → fsm → mm2s → sfifo → s2mm → 仲裁器 → 对外主端口）。运行结果若受工具链限制，标注「待本地验证」。

## 6. 本讲小结

- ZipCPU 有两套 DMA：legacy `wbdmac`（32 位、块缓冲、七状态机，仅留作兼容与形式化验证）和现行 `zipdma`（总线宽度无关、可处理非对齐，是 ZipSystem 里 `OPT_DMA` 实际实例化的那一个）。
- 两者都靠「先读进缓冲/流、再写出」应对 Wishbone 的半双工特性；`zipdma` 进一步把读（mm2s）与写（s2mm）拆成两个独立引擎，中间用 `sfifo` 解耦。
- `zipdma` 顶层由 8 个子模块拼成：`ctrl`（寄存器）→ `fsm`（切包调度）→ `mm2s`/`rxgears`/`sfifo`/`txgears`/`s2mm`（数据通路）→ `wbarbiter`（主端口仲裁）。
- mm2s 是只读主、s2mm 是只写主（`o_rd_we≡0`、`o_wr_we≡1`），内部用 AXI-Stream 风格的 `VALID/READY/DATA/BYTES/LAST` 互联；对外仍是 Wishbone。
- 控制/状态位的关键约定：位 31=忙、位 30=错、位 22/18=目的/源不自增、位 21:20/17:16=目的/源位宽、位 11:0=中间包长；写位 31=0 启动，写 "ABRT" 魔数中止。
- 「类 scatter/gather」能力来自读/写两侧地址自增与位宽的独立配置——一侧固定地址接外设、另一侧自增接内存即可，但它不是描述符链式的完整 scatter-gather 引擎。

## 7. 下一步学习建议

- 顺着「数据通路」继续读 `rtl/zipdma/zipdma_rxgears.v` 与 `zipdma_txgears.v`，理解跨位宽/字节序的「齿轮」对齐细节。
- 读 `bench/formal/zipdma_mm2s.sby`、`zipdma_s2mm.sby`、`wbdmac.sby`，结合第 u5-2 讲的形式化验证体系，看 DMA 如何被证明满足 Wishbone 契约与字节级正确性。
- 回到第 u4-2 讲的 ZipSystem 地址映射，确认 DMA 主端口经 `wbpriarbiter` 与 CPU 争用外部总线的优先级关系（CPU 优先、DMA 蹭用），理解「DMA 拿到总线后中途不会被夺走」这一保证的意义。
- 进阶可对照 spec 里「A separate AXI controller is scheduled for development」的备注，思考把这套 mm2s/s2mm 流式架构搬到真 AXI 突发通道上需要改什么。
