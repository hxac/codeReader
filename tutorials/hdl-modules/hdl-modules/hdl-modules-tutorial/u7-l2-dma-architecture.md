# DMA：流式写 DDR 的架构

## 1. 本讲目标

本讲剖析 hdl-modules 中的 `dma_axi_write_simple`——一个把 **AXI-Stream 流式数据搬进 DDR 内存**的 DMA（Direct Memory Access）核心，即业界常说的 **AXI DMA S2MM**（Stream-to-Memory-Mapped）。

学完后你应当能够：

- 说清 S2MM DMA 的数据通路：流输入 →（可选位宽转换）→ AXI 写通道 → DDR。
- 解释 `buffer_start/end/written/read` 四个地址如何构成一个 FPGA 写、CPU 读的**环形缓冲**，以及硬件/软件各推进哪一个。
- 看懂核心的 AXI 写状态机：它如何切分突发、如何做到「well-behaved」、为何每包固定 stall 一拍。
- 理解对齐错误、写错误、`write_done` 等状态如何通过寄存器文件与中断上报给 CPU。
- 读懂配套的 VUnit 仿真包与 testbench，并能据此设计一次验证实验。

本讲承接 u4-l1（FIFO 与 ready/valid 握手）、u5-l3（AXI 跨时钟域与通道 FIFO）、u6-l1（寄存器文件与中断寄存器）。它大量复用前面讲过的构建块：`width_conversion`、`handshake_merger/splitter`、`assign_last`、`event_aggregator`、`ring_buffer_write_simple`、`interrupt_register`。

## 2. 前置知识

### DMA 与 S2MM 是什么

**DMA（Direct Memory Access）**：让外设与内存之间直接搬数据，不必每拍都让 CPU 介入。CPU 只需「配置好起止地址、按一下启动」，硬件就自动把一批数据写进（或读出）内存，搬完再用中断通知 CPU。

**AXI DMA 的两个方向**（沿用 Xilinx/ARM 的术语）：

- **S2MM（Stream → Memory-Mapped）**：把 AXI-Stream 流（无地址、连续 beat）写进 **有地址** 的内存（如 DDR）。本讲核心就是 S2MM。
- **MM2S（Memory-Mapped → Stream）**：反过来，把内存数据读成流。本讲不涉及。

直观类比：流像一根「水龙头」持续出水（每拍一个 beat），DDR 像一个「大水缸」。DMA 就是中间的「自动灌水机器人」：你告诉它水缸从哪到哪、当前灌到哪了、CPU 喝到哪了，它就一桶一桶地把水灌进去，灌满一圈就回绕。

### 环形缓冲（Ring Buffer）的直觉

当 FPGA 持续产数据、CPU 异步消费数据时，最经典的结构是**环形缓冲**：

- 一块连续内存 `[start, end)`。
- 一个**写游标 `written`**：硬件每写完一段就往前推，到 `end` 回绕到 `start`。
- 一个**读游标 `read`**：CPU 每消费完一段就往前推，同样回绕。
- `[read, written)` 之间是「有效待消费数据」；`[written, read)` 之间是「空闲可写空间」。
- 必须保证硬件不写 CPU 还没读走的区域，CPU 不读硬件还没写的区域——靠 CPU 及时更新 `read`、硬件及时更新 `written` 来协同。

这套机制在 u6-l3 的 `ring_buffer_write_simple` 里已实现，本讲 DMA 直接实例化它，所以本讲重点在「DMA 如何用它」而非「它如何实现」。

### 本讲会用到的「积木」

| 积木 | 来自 | 作用 |
|------|------|------|
| `width_conversion` | common（u6-l2） | 流位宽 ≠ AXI 位宽时的宽窄转换 |
| `handshake_merger` / `handshake_splitter` | common（u6-l2） | 多路握手合一 / 一路分发多路 |
| `assign_last` | common | 在无 `last` 的流上按固定 beat 数生成 `last` |
| `event_aggregator` | common | 把高频事件聚合成低频事件（降低中断率） |
| `interrupt_register` | register_file（u6-l1） | 多源中断的粘滞/掩码/触发聚合 |
| `ring_buffer_write_simple` | ring_buffer（u6-l3） | 环形缓冲的地址/段管理 |
| `axi_lite_register_file` | register_file（u6-l1） | 把寄存器挂到 AXI-Lite 总线 |

## 3. 本讲源码地图

| 文件 | 作用 | 是否可综合 |
|------|------|-----------|
| [`modules/dma_axi_write_simple/src/dma_axi_write_simple.vhd`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/src/dma_axi_write_simple.vhd) | **DMA 核心**：流→位宽转换→AXI 写状态机→环形缓冲。注意：核心的寄存器接口是 record 形式，**不适合直接实例化**。 | 是 |
| [`modules/dma_axi_write_simple/src/dma_axi_write_simple_axi_lite.vhd`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/src/dma_axi_write_simple_axi_lite.vhd) | **用户实例化的顶层**：把核心 + AXI-Lite 寄存器文件拼到一起。 | 是 |
| [`modules/dma_axi_write_simple/sim/dma_axi_write_simple_sim_pkg.vhd`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/sim/dma_axi_write_simple_sim_pkg.vhd) | **仿真支持包**：`run_dma_axi_write_simple_test` 过程，模拟「CPU 消费 + 校验数据」。 | 否（仅仿真） |
| [`modules/dma_axi_write_simple/test/tb_dma_axi_write_simple.vhd`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/test/tb_dma_axi_write_simple.vhd) | VUnit testbench：随机化 generic、接 AXI-Stream master / AXI-Lite master / AXI slave BFM。 | 否 |
| [`modules/dma_axi_write_simple/regs_dma_axi_write_simple.toml`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/regs_dma_axi_write_simple.toml) | 寄存器清单（hdl-registers 输入），生成 VHDL 寄存器包与 C++ 驱动。 | 元数据 |
| [`modules/dma_axi_write_simple/module_dma_axi_write_simple.py`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/module_dma_axi_write_simple.py) | tsfpga Module：登记仿真配置、定义 netlist 资源回归断言。 | 元数据 |
| [`modules/dma_axi_write_simple/readme.rst`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/readme.rst) | 头注释式文档：包长、吞吐、AXI 行为、中断聚合等设计说明。 | 文档 |

## 4. 核心概念与源码讲解

### 4.1 S2MM 的定位与「简单」哲学

#### 4.1.1 概念说明

`dma_axi_write_simple` 的头一句话点明了它的定位与取舍：

> This module contains a simple AXI Direct Memory Access (DMA) component for streaming data from FPGA to DDR memory. ... The implementation is optimized for **very low resource usage** and **maximum AXI throughput**.

它把优化目标定死在两条：**资源极省**、**AXI 吞吐极高**。为了这两条，它**故意**砍掉了一堆通用 DMA 常见的能力（见 doc 概述）：

1. 只能往**连续的环形缓冲**写，不支持 scatter-gather（离散地址收集）。
2. 不支持字节 strobe / 窄传输（narrow burst），所有地址必须按 AXI 数据宽度对齐。
3. **包长是编译期常量**，运行时不能改，**不支持写半包/清半包**。
4. 包长必须是 **2 的幂**。

这些「限制」正是它又小又快的根因——很多通用 DMA 的面积都花在「运行时处理任意包长、任意地址、任意对齐」的复杂控制上，而这个核心把这些全部固化为编译期参数，省下的逻辑就成了它的核心竞争力。头注释里专门强调（`dma_axi_write_simple.vhd` 第 24–29 行）：包长是编译期参数，不支持写/清半包，这是「simple nature」的核心。

#### 4.1.2 核心流程

核心实体的端口可以一眼看清它的「三面」：

```text
            ┌─────────────────────────── dma_axi_write_simple ───────────────────────────┐
 stream ───►│ stream_ready/valid/data                                                   │
   (AXIS)   │                                                                          │──► AXI 写通道 (AW/W/B) ──► DDR
            │   regs_up / regs_down (record 形式的寄存器)   interrupt                   │
            └──────────────────────────────────────────────────────────────────────────┘
```

- **流面**：标准 AXI-Stream 式 `ready/valid/data`（承接 u2-l1 的握手约定）。注意：输入流**没有 `last`**——包边界是核心按 `packet_length_beats` 自己数出来的。
- **AXI 写面**：`axi_write_m2s`/`axi_write_s2m`，即 AXI 的 AW（写地址）/ W（写数据）/ B（写响应）三组通道（承接 u5-l2/u5-l3）。
- **寄存器面**：`regs_up`（核心→总线，如 `buffer_written_address`、中断状态）、`regs_down`（总线→核心，如 `buffer_start_address`、`config.enable`、`interrupt_mask`）。注意这里是 record 形式，对应 u6-l1 讲过的寄存器文件搬运，**不是裸总线**，所以才需要顶层（4.4 节）再套一个 `axi_lite_register_file`。

#### 4.1.3 源码精读

核心实体的 generic 与端口声明见 [`dma_axi_write_simple.vhd:166-208`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/src/dma_axi_write_simple.vhd#L166-L208)。几个关键 generic：

- `packet_length_beats`：以**流 beat** 计的包长，决定了每攒多少数据触发一次 `write_done`。
- `stream_data_width` / `axi_data_width`：两者可以不等，核心会自动插位宽转换。
- `enable_axi3`：兼容 AXI3（最大突发 16 拍）而非 AXI4（256 拍）。
- `write_done_aggregate_count/ticks`：中断聚合参数（4.5 节）。

入口处一连串 `assert ... severity failure` 把「simple」的约束钉死在精化期，违反就直接编译失败，绝不把非法配置带进仿真/综合，见 [`dma_axi_write_simple.vhd:235-258`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/src/dma_axi_write_simple.vhd#L235-L258)。例如：

- `stream_data_width` 与 `axi_data_width` 之一必须整除另一个（位宽比是整数拍）。
- `packet_length_beats` 必须是 2 的幂（便于用丢低位做取模）。
- 包长换算成 AXI 字节后，必须是 AXI 数据宽度的整数倍，且是 2 的幂个 AXI beat。

这套断言是「simple」哲学的编译期护栏：把所有运行期歧义在综合前消灭。

#### 4.1.4 代码实践

**目标**：从 generic 与断言读懂「simple」把哪些自由度固化了。

**步骤**：

1. 打开 [`dma_axi_write_simple.vhd:215-219`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/src/dma_axi_write_simple.vhd#L215-L219)，找到三个常量：`packet_length_bytes`、`packet_length_axi_beats`，理解「流 beat → 字节 → AXI beat」的换算链。
2. 对照 [`dma_axi_write_simple.vhd:248-258`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/src/dma_axi_write_simple.vhd#L248-L258) 的四条断言，回答：如果我设 `packet_length_beats=3`（非 2 的幂）会怎样？设 `stream_data_width=24, axi_data_width=32`（不能整除）会怎样？

**预期**：综合/精化期即报 `severity failure`，进程不会启动。**待本地验证**（需 VUnit/GHDL 等环境）。

#### 4.1.5 小练习与答案

**练习 1**：为什么核心坚持「包长必须是 2 的幂」？
**答案**：因为环形缓冲的地址回绕、段计数都要做「取模 / 除法」。2 的幂时，取模 = 丢低位、除法 = 右移，硬件零成本（无除法器），这正是面积优先的体现（与 u4-l1 FIFO 指针、u6-l3 ring buffer 同源）。

**练习 2**：readme 说 `packet_length_beats=1` 有「特化的优化实现」，代价是什么？
**答案**：见 4.2.4 节——它省掉了多拍状态机，但每个数据 beat 都会变成一次独立的 AXI 突发（每拍一次 AW + W + B），AXI 事务开销极大，「内存性能很差」。资源回归里它的逻辑级数也最高（16 级，见 4.5 节）。

---

### 4.2 AXI 写数据通路：宽度转换、突发切分与 well-behaved 时序

#### 4.2.1 概念说明

这一节是核心的「心脏」：把流数据变成 AXI 写事务。它要回答三个问题：

1. **位宽不一样怎么办** → 插一个 `width_conversion`。
2. **一个包比一次 AXI 最大突发还长怎么办** → 突发切分（burst splitting）。
3. **怎样才算一个「乖」的 AXI master** → 不发空包的 AW、用最长突发、BREADY 常高。

理解「well-behaved AXI master」很关键（承接 u5-l2 的同款概念）。AXI 协议允许 master 先发 AW 再慢慢给 W，但下游的 interconnect/slave 不一定喜欢「AW 发了却迟迟等不到数据」的 master。这个核心选择了一条保守而高效的策略。

#### 4.2.2 核心流程

整条写通路可拆成三段：

```text
stream ──► [width_conversion（可选）] ──► axi_ready/valid/data
                                              │
              ┌─────────────── axi_block ──────┘
              ▼
   ┌─ get_num_axi_bursts_per_packet（精化期算出切几段）
   │
   ├─ ring_buffer_write_simple：每段给一个 segment_address
   │
   └─ 两条 generate 路径二选一：
        ├─ packet_length_axi_beats = 1  → handshake_merger + splitter（单拍优化）
        └─ 否则                         → wait_for_start_condition/let_data_pass 状态机
```

**位宽转换**：仅当 `stream_data_width /= axi_data_width` 才实例化 `common.width_conversion`；否则三根线直连（零资源）。见 [`dma_axi_write_simple.vhd:322-350`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/src/dma_axi_write_simple.vhd#L322-L350)。

**突发切分**（精化期纯函数 `get_num_axi_bursts_per_packet`，[`dma_axi_write_simple.vhd:355-380`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/src/dma_axi_write_simple.vhd#L355-L380)）：

- 取 AXI 最大突发长度（AXI4 = 256 拍，AXI3 = 16 拍，由 `axi_pkg.get_max_burst_length_beats` 给出）。
- 若整包 ≤ 一个最大突发 → `num_axi_bursts_per_packet = 1`。
- 否则要求包长是「最大突发长度」的整数倍，否则 `severity failure`，并算出段数。
- 进而 `axi_burst_length_beats = packet_length_axi_beats / num_axi_bursts_per_packet`，即每段突发多长。

**AW/W/B 的静态部分**（每拍都一样，故在进程外直接赋值），[`dma_axi_write_simple.vhd:472-481`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/src/dma_axi_write_simple.vhd#L472-L481)：

- `aw.len` = 每段突发长度 − 1（AXI 的 len 是「拍数−1」）。
- `aw.size` = AXI 数据宽度的字节数幂（`to_size`）。
- `aw.burst` = INCR（递增突发）。
- `w.strb` = 全有效（`to_strb`，因为不支持窄传输/字节屏蔽）。
- `b.ready` 恒为 `'1'`（BREADY 常高，well-behaved 第 3 条）。

动态部分（`aw.valid`/`aw.addr`/`w.valid`/`w.last`）由下面的状态机或单拍路径产生。

#### 4.2.3 源码精读：多拍状态机（通用路径）

通用路径用一个两态机管理 AW 与 W，[`dma_axi_write_simple.vhd:534-606`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/src/dma_axi_write_simple.vhd#L534-L606)。状态：

- **`wait_for_start_condition`**：等待三个条件同时成立才发 AW：
  1. `axi_valid`——已有流数据（避免「AW 发了却没数据」的 ill-behaved 行为，这正是 well-behaved 第 2 条）；
  2. `segment_valid`——环形缓冲给了一个可用地址（缓冲未满、已 enable）；
  3. `not axi_write_m2s.aw.valid`——上一笔 AW 已被收走。
  
  条件满足时：拉高 `aw.valid`、锁存 `segment_address` 到 `aw.addr`、弹出一个 segment（`segment_ready<='1'`）、切到 `let_data_pass`。

- **`let_data_pass`**：让 W 数据通过，直到本段突发的最后一拍（`axi_last`，由 `assign_last` 按 `axi_burst_length_beats` 数出来）握手完成，再回到 `wait_for_start_condition`。

关键赋值（注意「省关键路径」的写法，[`dma_axi_write_simple.vhd:601-604`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/src/dma_axi_write_simple.vhd#L601-L604)）：

```vhdl
axi_write_m2s.w.valid <= axi_valid and to_sl(state = let_data_pass);
axi_write_m2s.w.last  <= axi_last;
axi_ready <= axi_write_s2m.w.ready and to_sl(state = let_data_pass);
```

注释里特意说明：用「不被 state 门控的 ready/valid」来算状态转移，是为了**省一点关键路径**——典型的时序优化手法。

**每包一拍 stall 的来源**：从 `let_data_pass` 回到 `wait_for_start_condition` 后，必须重新等 AW 被收走、再发下一笔 AW，这中间至少消耗一拍，于是每包输入流会 stall 一个时钟周期（假设 AWREADY/WREADY 都高）。这正是 readme「one-cycle overhead per packet」的物理根因（见 [`readme.rst:60-79`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/readme.rst#L60-L79)）。包长为 \(P\) 拍时的吞吐占比约为：

\[
\text{throughput} \approx \frac{P}{P+1}
\]

包越长，这一拍开销占比越小——这也是 readme 建议「把 packet_length_beats 提到最大突发长度以改善内存性能」的原因。

#### 4.2.4 源码精读：单拍优化路径

当 `packet_length_axi_beats = 1`（每个包只有一个 AXI beat）时，核心走一条**特化**路径，省掉整个状态机，见 [`dma_axi_write_simple.vhd:486-529`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/src/dma_axi_write_simple.vhd#L486-L529)：

- 用 `handshake_merger` 把「segment（地址）」和「axi（数据）」两路握手**合并**成一路（两路同时有效才往下走）。
- 用 `handshake_splitter` 把合并后的一路**分发**成 AW 和 W 两路（同时拉高 `aw.valid` 与 `w.valid`）。
- 因为包只有一拍，`w.last <= '1'` 永远成立。

代价（见 readme 与 4.5 资源数）：每个数据 beat 都触发一次完整的 AW+W+B 事务，AXI 地址开销极大，内存性能很差；但它把状态机压成了纯组合的 merge/split，逻辑级数反而最高。这印证了 hdl-modules 一贯的「用 generic 在面积/性能间二选一」风格。

#### 4.2.5 源码精读：ring_buffer 如何喂地址

地址来自 `ring_buffer_write_simple`，实例化见 [`dma_axi_write_simple.vhd:414-440`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/src/dma_axi_write_simple.vhd#L414-L440)。两个关键 generic 揭示了「段」与「包」的关系：

- `segment_length_bytes => axi_burst_length_bytes`：**每段 = 一次 AXI 突发**。每弹出一个 segment，就是一次突发的起始地址。
- `segments_per_packet => num_axi_bursts_per_packet`：攒够这么多段（= 一个包的全部突发）后，才更新一次 `buffer_written_address`。

`segment_ready/valid/address` 是 AXI-Stream 式握手——状态机每发一次 AW 就弹一个 segment、取走它的地址。`write_done` 由 `assign_last` 标出「本包的最后一段」后产生（[`dma_axi_write_simple.vhd:454-468`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/src/dma_axi_write_simple.vhd#L454-L468)），用来告诉 ring buffer「一整包写完了，推进 written」。

#### 4.2.6 代码实践

**目标**：跟踪一次「长包」从流到 AXI 的完整路径，理解突发切分。

**步骤**：

1. 读 [`dma_axi_write_simple.vhd:355-385`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/src/dma_axi_write_simple.vhd#L355-L385) 的 `get_num_axi_bursts_per_packet`。
2. 假设 `enable_axi3=false`（AXI4，最大突发 256）、`axi_data_width=64`、`packet_length_beats` 对应 `packet_length_axi_beats=2048`。手算：`num_axi_bursts_per_packet`、`axi_burst_length_beats`、`aw.len` 各是多少？
3. 对照 [`module_dma_axi_write_simple.py:62-67`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/module_dma_axi_write_simple.py#L62-L67) 的资源数：`packet_length_beats=2048` 对应 132 LUT / 218 FF / 逻辑级数 11。

**预期**：`num_axi_bursts_per_packet = 2048/256 = 8`，`axi_burst_length_beats = 256`，`aw.len = 255`。一个包拆成 8 段满长突发，每段 256 拍。

#### 4.2.7 小练习与答案

**练习 1**：为什么状态机要求「发 AW 之前先确认 `axi_valid` 有数据」？
**答案**：否则就是「先发地址、数据迟迟不来」的 ill-behaved master，会霸占下游 interconnect 的仲裁、降低整系统吞吐（readme「W channel block」一节专门讨论了这个反模式，并建议必要时用 `axi_write_throttle` 缓解）。

**练习 2**：单拍路径用 `handshake_merger` + `handshake_splitter`，等效于实现了什么 AXI 行为？
**答案**：等效于「AW 与 W 同拍一起发、且数据已就绪才发」——把 AW/W 握手绑成原子操作，因为是单拍包，`w.last` 恒为 1，一笔 AW 配一笔 W 即完事。

---

### 4.3 环形缓冲地址管理：buffer_start/end/written/read

#### 4.3.1 概念说明

四个地址寄存器是 DMA 与 CPU 之间的「契约」，它们在 toml 里有精确的语义定义（[`regs_dma_axi_write_simple.toml:79-156`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/regs_dma_axi_write_simple.toml#L79-L156)）：

| 寄存器 | 模式 | 谁推进 | 语义 |
|--------|------|--------|------|
| `buffer_start_address` | `w`（CPU 写） | CPU 配置一次 | 缓冲首字节地址，必须按包长对齐 |
| `buffer_end_address` | `w`（CPU 写） | CPU 配置一次 | **末字节之后**的地址（半开区间），必须对齐 |
| `buffer_written_address` | `r`（CPU 读） | **硬件**持续更新 | 已写入数据的末尾；`[read, written)` 为有效数据 |
| `buffer_read_address` | `w`（CPU 写） | **CPU**消费后更新 | CPU 已消费到的位置；`[written, read)` 为空闲可写 |

几个关键不变量（来自 `buffer_written_address` 的描述，[`toml:113-134`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/regs_dma_axi_write_simple.toml#L113-L134)）：

- `written == read` 表示缓冲**空**；二者不同表示有有效数据。
- `written` **永远不会等于 `end`**：写到最后一个字节后会回绕到 `start`（半开区间 + 回绕）。
- 有效字节数恒为「突发长度（字节）」的整数倍——因为硬件按段（= 突发）推进 `written`。
- 所有地址必须按包长（字节）对齐，否则触发对齐错误中断（4.5 节）。

#### 4.3.2 核心流程

缓冲里的字节数（待消费量）在一个回绕周期内可表示为：

\[
\text{pending} = (\text{written} - \text{read}) \bmod (\text{end} - \text{start})
\]

硬件只推进 `written`，CPU 只推进 `read`。二者的协同节奏：

```text
硬件侧：                       CPU 侧：
 写完一整包                     读 written，发现 written != read
 ──► written 前进一步           ──► 消费 [read, written) 的数据
                                ──► 把 read 写成 written（释放空间）
 缓冲满？                       缓冲空？
 ──► stream stall               ──► 等下一次 written 更新
```

注意 toml 对 `buffer_read_address` 的强调（[`toml:138-156`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/regs_dma_axi_write_simple.toml#L138-L156)）：CPU 把 `read` 写成 `written` 之前，**必须已经消费完或拷贝走**这段数据——因为一旦写回，这块区域立刻又可能被硬件覆盖。

#### 4.3.3 源码精读

核心里这四个地址是纯连线：CPU 写进 `regs_down`，硬件读出喂给 ring buffer；ring buffer 算出的 `buffer_written_address` 喂回 `regs_up` 供 CPU 读。见 [`dma_axi_write_simple.vhd:442-450`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/src/dma_axi_write_simple.vhd#L442-L450)：

```vhdl
buffer_start_address <= u_unsigned(regs_down.buffer_start_address(...));
buffer_end_address   <= u_unsigned(regs_down.buffer_end_address(...));
buffer_read_address  <= u_unsigned(regs_down.buffer_read_address(...));
regs_up.buffer_written_address(...) <= std_ulogic_vector(buffer_written_address);
```

真正干活的是 `ring_buffer_write_simple`（u6-l3 已详解）：它丢低位做对齐、用两个回绕游标、按 AXI-Stream 握手发段、`write_done` 推进写指针，并用「永远空一格」区分满与空。对齐检查也在它内部完成，结果通过 `status`（`ring_buffer_write_simple_status_t`）里的 `start/end/read_address_unaligned` 位回传，供 4.5 节的中断逻辑使用（[`dma_axi_write_simple.vhd:225-227, 439`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/src/dma_axi_write_simple.vhd#L225-L227)）。

缓冲满时，`segment_valid` 不会拉高（环形缓冲不发段），状态机就停在 `wait_for_start_condition`，于是 `stream_ready` 拉低、流被 stall——这就是 readme 所说「缓冲满则流 stall，CPU 更新 `read` 后两拍恢复」（[`readme.rst:77-79`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/readme.rst#L77-L79)）。

#### 4.3.4 代码实践

**目标**：在仿真里观察 `written` 的推进与回绕。这是本讲**主实践**的子任务，详见第 5 节。这里先做源码阅读准备：

1. 读仿真包 [`dma_axi_write_simple_sim_pkg.vhd:91-104`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/sim/dma_axi_write_simple_sim_pkg.vhd#L91-L104)，看测试如何配置 `start/end/read` 三个地址：`end` 用 `last_address(buf)+1`（半开区间），`read` 初值设为 `start`。
2. 注意 `allocate(..., alignment=>packet_length_bytes, ...)` 强制缓冲按包长对齐分配，正好满足 toml 的对齐要求。

**预期**：能讲清「为什么 end 是 last+1 而不是 last」——因为环形缓冲用半开区间，`written` 永不等于 `end`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `written` 永远不等于 `end`？
**答案**：半开区间 `[start, end)` 下，写到「end−1」这一字节后，下一步回绕到 `start`。若允许 `written=end`，就无法区分「缓冲满」和「缓冲空」（两者都是 written==read 的相对关系会歧义）。这正是 u6-l3「永远空一格」约定的体现。

**练习 2**：CPU 把 `read` 写回前，为什么必须先消费完数据？
**答案**：写回 `read` 等于声明「这块空间我不用了」，硬件立刻可能往里写新数据。若 CPU 还没拷走就写回，数据会被覆盖丢失。

---

### 4.4 AXI-Lite 控制顶层：寄存器文件与核心的集成

#### 4.4.1 概念说明

核心实体 `dma_axi_write_simple` 的寄存器面是 **record**（`regs_up`/`regs_down`），不是裸 AXI-Lite 总线——这方便了核心内部用字段名直接引用（如 `regs_down.config.enable`），但**不能直接挂到 CPU 总线上**。所以项目提供了一个顶层 `dma_axi_write_simple_axi_lite`，它把核心与一个 AXI-Lite 寄存器文件拼到一起，对外暴露标准的 `regs_m2s`/`regs_s2m`（AXI-Lite）总线。这就是为什么 readme 和核心头注释都强调：「This entity is not suitable for instantiation in a user design, use instead `dma_axi_write_simple_axi_lite`」。

#### 4.4.2 核心流程

顶层只做两件实例化、一组连线，零自定义逻辑：

```text
   AXI-Lite 总线 (regs_m2s/regs_s2m)
            │
   ┌────────┴─────────┐
   │ register_file    │  ← hdl-registers 按 toml 生成
   │   _axi_lite      │
   └────────┬─────────┘
       regs_up │ regs_down (record)
            │
   ┌────────┴─────────┐
   │ dma_axi_write_   │  ← 4.2/4.3 节讲的核心
   │   simple (core)  │
   └──────────────────┘
```

寄存器文件本身（`dma_axi_write_simple_register_file_axi_lite`）是 hdl-registers 从 toml 自动生成的（u6-l1 的 `axi_lite_register_file` 参数化产物），不在仓库源码里手写——使用 tsfpga 时会自动生成并保持同步（见 [`doc/dma_axi_write_simple.rst:36-77`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/doc/dma_axi_write_simple.rst#L36-L77) 的说明）。

#### 4.4.3 源码精读

顶层架构体只有约 40 行，全部是实例化与端口映射，见 [`dma_axi_write_simple_axi_lite.vhd:56-103`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/src/dma_axi_write_simple_axi_lite.vhd#L56-L103)：

- 核心实例（[`L64-L87`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/src/dma_axi_write_simple_axi_lite.vhd#L64-L87)）：generic 全部透传，寄存器面用中间信号 `regs_up`/`regs_down` 连接。
- 寄存器文件实例（[`L91-L101`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/src/dma_axi_write_simple_axi_lite.vhd#L91-L101)）：一端接 AXI-Lite 总线，另一端接同一个 `regs_up`/`regs_down`。

顶层 generic（[`L30-L39`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/src/dma_axi_write_simple_axi_lite.vhd#L30-L39)）给 `enable_axi3`、`write_done_aggregate_count/ticks` 设了默认值（`false`、`1`、`1`），方便用户按最常见配置直接实例化。

这种「核心用 record、顶层加总线壳」的分层，是 hdl-modules 里寄存器密集型 IP 的通用模式：核心内部用字段名写代码更清晰可读，对外则由生成的寄存器文件负责总线协议细节。

#### 4.4.4 代码实践

**目标**：看清「谁连谁」，并理解 record 接口 vs 总线接口的分工。

**步骤**：

1. 对照 [`dma_axi_write_simple_axi_lite.vhd:64-101`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/src/dma_axi_write_simple_axi_lite.vhd#L64-L101)，列出 `regs_up`/`regs_down` 这对中间信号的两端各连到哪个实体的哪个端口。
2. 回答：如果我想直接实例化核心 `dma_axi_write_simple`（不走顶层），我需要自己提供什么？

**预期**：`regs_up`/`regs_down` 一端连核心，一端连生成的寄存器文件；若直接用核心，需自行把 record 转成某种总线（等价于自己写一个寄存器文件壳）。

#### 4.4.5 小练习与答案

**练习**：为什么核心不直接暴露 AXI-Lite 端口，而要用 record 中转？
**答案**：用 record（字段名）写核心逻辑（如 `if regs_down.config.enable then`）比用裸总线地址读写清晰得多，也更不易错；总线协议（握手、地址译码、响应码）由生成的寄存器文件集中处理，二者解耦，核心可读、总线壳可复用。

---

### 4.5 中断、状态上报与仿真验证

#### 4.5.1 概念说明

DMA 需要把「发生了什么」告诉 CPU，途径有二：CPU 轮询 `interrupt_status` 寄存器，或开中断等 `interrupt` 信号拉高。需要上报的事件有五类（见 toml `interrupt_status`，[`toml:1-42`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/regs_dma_axi_write_simple.toml#L1-L42)）：

- `write_done`：一整包写完。
- `write_error`：DDR 写响应 `BRESP != OKAY`。
- `start/end/read_address_unaligned_error`：三个地址未按包长对齐。

这些事件由 u6-l1 的 `interrupt_register` 聚合：每个事件置一个粘滞 `status` 位，CPU 写 1 清除；`interrupt_mask` 门控后归约成单比特 `interrupt` 输出。

此外，`write_done` 可能非常频繁（包小、数据率高时每包都触发），会打爆 CPU 中断。核心用 `event_aggregator`（承接 u6-l2 同源思路）把多个 `write_done` 聚合成一个稀疏事件。

#### 4.5.2 核心流程

```text
写完一包 ──► event_aggregator（可选聚合）──► interrupt_status.write_done (粘滞位)
BRESP错   ─────────────────────────────────► interrupt_status.write_error
环形缓冲  ──► status.*_unaligned ──────────► interrupt_status.*_unaligned_error
                                                │
                              interrupt_register │ (mask 门控 + 粘滞)
                                                ▼
                                            interrupt (单比特输出)
CPU：读 interrupt_status 看事件；写 1 清除；写 interrupt_mask 屏蔽
```

注意「写错误」是组合出来的（[`dma_axi_write_simple.vhd:282-286`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/src/dma_axi_write_simple.vhd#L282-L286)）：当 `b.ready and b.valid and b.resp /= axi_resp_okay` 同时成立时置位。三个对齐错误位则直接取自 `ring_buffer_status`（[`L288-L298`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/src/dma_axi_write_simple.vhd#L288-L298)）。

#### 4.5.3 源码精读

中断块整体见 [`dma_axi_write_simple.vhd:262-319`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/src/dma_axi_write_simple.vhd#L262-L319)：

- `interrupt_register` 实例（[`L268-L278`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/src/dma_axi_write_simple.vhd#L268-L278)）：`sources` = 各事件位，`mask` = `regs_down.interrupt_mask`，`clear` = CPU 写回的 `interrupt_status`，输出 `status` 与单比特 `trigger`（即对外的 `interrupt`）。
- `event_aggregator` 实例（[`L307-L317`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/src/dma_axi_write_simple.vhd#L307-L317)）：generic 取默认值（1/1）时是直通；调大后按 `write_done_aggregate_count`（事件数）和 `write_done_aggregate_ticks`（时钟拍数）聚合。

资源回归（[`module_dma_axi_write_simple.py:78-88`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/module_dma_axi_write_simple.py#L78-L88)）显示，开启聚合（`count=512, ticks=262144`）相对不开（同 packet 的 132 LUT/218 FF）增加到 156 LUT/247 FF——聚合逻辑的面积代价可量化、且纳入 CI 回归断言，这正是把「中断聚合」做成 generic 开关的设计意图。

#### 4.5.4 源码精读：仿真支持包

仿真包 `dma_axi_write_simple_sim_pkg` 提供两个重载的 `run_dma_axi_write_simple_test`（声明见 [`sim_pkg:36-57`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/sim/dma_axi_write_simple_sim_pkg.vhd#L36-L57)）：一个把 DUT 写入的每个字节推进队列（不校验，留给调用方），另一个直接逐字节比对参考数据。

过程体模拟了 CPU 的完整工作流（[`sim_pkg:77-147`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/sim/dma_axi_write_simple_sim_pkg.vhd#L77-L147)）：

1. 在 VUnit 的 `memory` 模型里按 `packet_length_bytes` 对齐分配缓冲（先塞 3 字节 padding，故意从非零地址开始）。
2. 通过 AXI-Lite 写 `start`/`end`(`last+1`)/`read`(`start`) 三个地址。
3. 写 `config.enable='1'` 启动 DMA。
4. 循环：读 `written` → 消费 `[read, written)` 的字节（带 12.5% 概率提前停止，模拟 CPU 落后）→ 把 `read` 写回 → 直到搬完 `receive_num_bytes` 字节。
5. 第二个重载在搬完后逐字节 `check_equal` 比对参考数组（[`sim_pkg:174-183`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/sim/dma_axi_write_simple_sim_pkg.vhd#L174-L183)），并断言队列恰好清空。

testbench（[`tb_dma_axi_write_simple.vhd`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/test/tb_dma_axi_write_simple.vhd)）把这套接起来：随机化 `address_width`/位宽/`enable_axi3`/包长（[`L50-L102`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/test/tb_dma_axi_write_simple.vhd#L50-L102)），用 `axi_stream_master` BFM 喂流（[`L205-L218`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/test/tb_dma_axi_write_simple.vhd#L205-L218)）、`axi_lite_master` BFM 配寄存器（[`L222-L228`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/test/tb_dma_axi_write_simple.vhd#L222-L228)）、`axi_write_slave` BFM 当模拟 DDR（带随机 stall，[`L232-L245`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/test/tb_dma_axi_write_simple.vhd#L232-L245)）。Python 侧用 `self.add_vunit_config(test, count=8)` 跑 8 组随机配置（[`module_dma_axi_write_simple.py:23-29`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/module_dma_axi_write_simple.py#L23-L29)）。

#### 4.5.5 代码实践

**目标**：跑通现成的随机化回归测试，观察事件流。

**步骤**：

1. 按 u1-l3 装好 VUnit（`vunit-hdl`）与 tsfpga，配好 `PYTHONPATH` 指向仓库根。
2. 运行（**待本地验证**，命令取决于你装的是 GHDL 还是 Vivado 仿真器）：

   ```bash
   python tools/simulate.py --enable-preprocessing --verbose dma_axi_write_simple.tb_dma_axi_write_simple
   ```

3. 在波形里盯 `interrupt`、`regs_up.interrupt_status`、`buffer_written_address`，观察：每写完一包 → `write_done` 粘滞位置位 → 若 mask 开了则 `interrupt` 拉高 → CPU 清除后又落低。

**预期**：8 组随机配置全部通过 `check_expected_was_written(memory)`（[`tb:198`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/test/tb_dma_axi_write_simple.vhd#L198)），即 DUT 写进模拟 DDR 的数据与参考数据逐字节一致。**待本地验证**。

#### 4.5.6 小练习与答案

**练习 1**：为什么 `write_error` 用 `b.resp /= axi_resp_okay` 而不是直接看某个 error 信号？
**答案**：AXI 写事务的成功/失败由 **B 通道的响应码 `BRESP`** 表达（OKAY=成功，SLVERR/DECERR=从机错误）。DMA 只是发起写，真正的错误信息编码在 BRESP 里，所以核心译码 BRESP 来判定写错误。

**练习 2**：把 `write_done_aggregate_count/ticks` 都设为 1（默认）时，`event_aggregator` 表现如何？
**答案**：直通——每个 `write_done` 事件原样输出（注释明说「is a passthrough if generics are at default value」）。只有把两个值都调大，才真正按事件数/拍数聚合，降低中断率。

---

## 5. 综合实践

**任务**：基于现成 testbench，设计一次「最小可观察」的 DMA 写入实验，亲手验证「流进 → DDR 出 → written 推进 → 回绕」的完整闭环。

**操作步骤**：

1. **读通验证流程**：先读 [`sim_pkg:63-148`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/sim/dma_axi_write_simple_sim_pkg.vhd#L63-L148) 的 `run_dma_axi_write_simple_test`，画出「配置地址 → enable → 轮询 written → 消费 → 写回 read」的时序。

2. **固定一组小配置**：参照 [`module_dma_axi_write_simple.py:62-67`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/module_dma_axi_write_simple.py#L62-L67)，挑 `address_width=29, stream_data_width=64, axi_data_width=64, packet_length_beats=16`，即每包 16 拍 ×8 字节 = 128 字节，单个 AXI 突发即可写完（`num_axi_bursts_per_packet=1`）。手算 `aw.len` 应为 15。

3. **缩小缓冲制造回绕**：令 `buffer_size_packets=2`（缓冲 = 2 包 = 256 字节），`test_data_num_bytes` 设为缓冲的 3 倍（参考 [`tb:154-158`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/test/tb_dma_axi_write_simple.vhd#L154-L158) 的做法），强制 `written` 至少回绕一圈。

4. **跑仿真并观察**（**待本地验证**）：在波形里核对：
   - `config.enable` 拉高前 `stream_ready` 恒为 0（toml 承诺的「enable 前 stall」）；
   - 每包写完（B 握手 + `is_last_burst_in_packet`）后 `buffer_written_address` 前进 128 字节；
   - 写到 `end` 后 `written` 回绕到 `start`，永不等于 `end`；
   - 流每包 stall 一拍（`stream_ready` 每包有一个周期的低电平）；
   - 若故意把 `start` 设成非 128 字节对齐的地址，应看到 `start_address_unaligned_error` 置位（可在 testbench 里临时改 `allocate` 的 `alignment` 或手写一个错地址做反例实验）。

5. **数据校验**：确认仿真末尾 `check_expected_was_written(memory)` 与 `run_dma_axi_write_simple_test` 的逐字节 `check_equal` 全部通过。

**预期结果**：DDR 模型里的数据与喂入的参考流逐字节一致；`written` 单调推进并正确回绕；CPU 模拟消费后写回 `read`，缓冲可被反复写入。整个闭环证明 S2MM 通路、环形缓冲协议、中断/状态上报三件事都正确协同。

> 说明：本实践依赖 VUnit + 一个 VHDL 仿真器（GHDL/NVC/ModelSim/Vivado xSIM）+ tsfpga 的寄存器代码生成。若本地暂无环境，可先把上述步骤 1–3 当作「源码阅读 + 纸面推演」完成，把每个观察点的预期值写下来，待具备环境时再逐条核对。

## 6. 本讲小结

- `dma_axi_write_simple` 是 **S2MM** DMA：把 AXI-Stream 流写进 DDR 的连续环形缓冲，优化目标是「资源极省 + AXI 吞吐极高」，靠把包长、对齐、位宽比等固化成编译期约束来实现。
- 数据通路 = （可选）`width_conversion` → AXI 写状态机 → `ring_buffer_write_simple`；状态机用 `wait_for_start_condition`/`let_data_pass` 两态，保证「有数据才发 AW、用最长突发、BREADY 常高」的 well-behaved 行为，代价是每包固定 stall 一拍。
- 长包会被精化期函数 `get_num_axi_bursts_per_packet` 切成多个满长突发；单拍包走 `handshake_merger`+`handshake_splitter` 的特化路径，省状态机但内存性能差。
- 四个地址 `start/end/written/read` 构成 FPGA 写、CPU 读的环形缓冲契约：硬件推进 `written`、CPU 推进 `read`，`written` 永不等于 `end`（半开区间 + 回绕）。
- 顶层 `dma_axi_write_simple_axi_lite` 给 record 接口的核心套上 hdl-registers 生成的 AXI-Lite 寄存器文件壳，是用户该实例化的版本。
- 五类事件（`write_done`/`write_error`/三个对齐错误）经 `interrupt_register` 粘滞+掩码聚合成单比特 `interrupt`，`write_done` 还可经 `event_aggregator` 降频；全部状态可被 CPU 轮询或中断消费。

## 7. 下一步学习建议

- **下一讲 u7-l3** 会接着讲 [`regs_dma_axi_write_simple.toml`](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/regs_dma_axi_write_simple.toml) 如何经 hdl-registers 生成 VHDL 寄存器包与 C++ 头，以及 `cpp/` 下的 zero-copy C++ 驱动如何消费 `written`/`read` 地址——直接呼应本讲的环形缓冲契约。
- 若想加深对「积木」的理解，回看 u6-l2 的 `width_conversion`/`handshake_merger/splitter`、u6-l3 的 `ring_buffer_write_simple`、u6-l1 的 `interrupt_register`——本讲几乎每个实例化都能在那里找到原理详解。
- 若关心 AXI 性能调优，阅读 readme 的「AXI/data throughput」与「W channel block」两节，并结合 u5-l2 的 `axi_write_throttle`，理解何时需要给 DMA 下游加节流。
- 想动手扩展？可在 fork 里尝试加一个 MM2S（内存→流）方向的同类核心，复用本讲的环形缓冲与中断框架，作为综合练习。
