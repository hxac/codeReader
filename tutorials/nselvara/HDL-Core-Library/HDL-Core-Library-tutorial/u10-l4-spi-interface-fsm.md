# SPI 顶层接口：多片选状态机与 TX FIFO

## 1. 本讲目标

本讲是 SPI 子系统的收尾篇。前几讲我们分别拆解了 `spi_pkg`（u10-l1，四种模式与位序工具）、`spi_tx`（u10-l2，串行发送器）和 `spi_rx`（u10-l3，串行接收器）。但一个能用的 SPI 控制器，还要回答三个工程问题：

- 怎么把"待发送的一批字"缓存起来，让上层只管写、不管何时真正上线？
- 同时挂了多个从机芯片时，怎么**一次只选一片**、轮流服务？
- 如果想把**同一批数据广播**给多片从机，怎么做到不重新装载？

`spi_interface` 就是回答这三个问题的顶层模块。它用一条 **6 状态有限状态机（FSM）** 调度发送，用一块 **异步 FIFO** 缓存待发数据，并用 FIFO 的 `reset_read_pointer`（读指针重放）机制实现"逐片广播同一批数据"。

学完本讲，你应当能够：

1. 画出 `idle → fetch_data → wait_for_data → wait_for_acknowledge → reset_for_next_chip → wait_for_transfer_end` 六状态的流转关系，并说出每个状态做什么。
2. 读懂 `get_next_selected_chip` 如何在多个被选中的芯片里**按索引升序、一次挑出一片**。
3. 解释 `reset_read_pointer` 为什么能让第二片、第三片从机收到和第一片完全相同的数据。
4. 看懂 `tx_trigger` / `spi_busy` / `tx_fifo_write_blocked` 三个握手信号是如何与 FSM、TX FIFO 配合的。

---

## 2. 前置知识

在进入源码之前，先用通俗语言建立几个直觉。

### 2.1 什么是"顶层接口 / wrapper"

前面的 `spi_tx`、`spi_rx` 都是"叶子模块"——它们只管把一个字一位一位地搬上/搬下串行线。但真实使用中，上层逻辑不想直接去跟 `spi_tx` 的位计数器、握手信号打交道。`spi_interface` 就是一个**包装层（wrapper）**：它把 `spi_tx`、`spi_rx` 和一块 FIFO 组装在一起，对外暴露一组"我给你一个写端口和一个触发脉冲，你帮我发完并告诉我忙不忙"的简单接口。这与本库"同一实体多架构"的思想一脉相承——先把复杂功能拆成小叶子，再用一个顶层把它们缝起来。

### 2.2 为什么发送要用 FIFO

SPI 是**串行**协议：一个时钟周期只能搬一位，发完一个 8 位字要 8 个时钟周期。如果上层每准备好一个字就必须等前一个发完才能写下一个，上层就被严重拖慢。解决办法是放一块 FIFO 当缓冲：上层连续把多个字"倒"进 FIFO，FSM 再按自己的节奏一个个取出来发送。这样上层和发送器**解耦**。

这里用的 FIFO 是第 9 单元讲过的 **异步 FIFO（`fifo_async`）**，它本身支持读、写两个独立时钟域。`spi_interface` 例化的是其厂商无关的 `own_behavioural_async_fifo` 架构（见 u9-l3、u9-l4）。

### 2.3 多片选与"一次一片"

SPI 用一根 `chip_select_n`（低有效）线来选中某片从机。本模块支持最多 `SPI_CHIPS_AMOUNT` 片从机，因此 `spi_chip_select_n` 是一个向量，每一位对应一片。关键约束是：**同一时刻只能有一片从机的片选被拉低**，否则两片同时驱动 `serial_data_in` 会冲突。所以即便用户在 `selected_chips` 里同时置位了多片，FSM 也必须把它们**串行化**——先服务一片、发完，再服务下一片。

### 2.4 "广播重放"是什么意思

设想一个固件升级场景：你要把同一段 1 KB 数据发给 3 片相同的从机芯片。朴素做法是写 3 遍 FIFO。本模块的巧思是：**只写一遍 FIFO，发完第一片后把读指针"倒回起点"，第二片、第三片就能重读同一批数据**。这正是 u9-l4 讲过的 `reset_read_pointer` 机制的用武之地——它只清零读指针、保留写指针和 RAM 内容，于是数据可以被无损重读。

> ⚠️ 重要提醒（承接 u9-l4）：`reset_read_pointer` 的**真正无损重放**只在 `own_behavioural_async_fifo` 架构里成立。Xilinx `xpm_fifo_async` 版本里它只是"屏蔽读使能"（`rd_en => read_enable and not reset_read_pointer`），Intel `dcfifo` 版本则完全忽略它。`spi_interface` 恰好例化的是 `own_behavioural_async_fifo`，所以广播重放在仿真里是真实生效的。

---

## 3. 本讲源码地图

本讲涉及三个核心文件，外加波形脚本辅助观察：

| 文件 | 作用 |
| --- | --- |
| `ip/communication/spi/spi_interface.vhd` | 本讲主角。顶层接口，含 6 状态 FSM、片选译码、TX FIFO 与 spi_tx/spi_rx 例化。 |
| `ip/communication/spi/tb/tb_spi_interface.vhd` | 仿真验证。含 6 个测试用例，其中 `test_multi_chip_transfers`、`test_multi_word_fifo_multi_chip_streaming` 直接覆盖多片选广播。 |
| `ip/memories/fifo/fifo_async.vhd` | 被例化的异步 FIFO。本讲聚焦它的 `reset_read_pointer` 端口与 `own_behavioural_async_fifo` 中的读指针逻辑。 |
| `ip/communication/spi/tb/tb_spi_interface.do` | ModelSim/QuestaSim 波形脚本，已把 `state`、`current_chip_index_v`、`selected_chips_reg` 等 FSM 内部变量编入波形分组，是追踪状态机的现成工具。 |

前置依赖模块（本讲只引用其接口、不重复讲解内部）：`spi_tx`（u10-l2，提供 `tx_data_valid`/`tx_data_ack` 握手与 `tx_is_ongoing`）、`spi_rx`（u10-l3）、`fifo_async`（u9-l3/u9-l4）、`clock_enable`（u5-l1，被 spi_tx 内部用于门控 SPI 时钟）、`utils_pkg` 中的 `get_lowest_active_bit`（u3-l2）。

---

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：顶层架构、多片选 FSM、`get_next_selected_chip` 轮询、TX FIFO 集成与广播重放。

### 4.1 spi_interface 顶层架构：把三块积木缝起来

#### 4.1.1 概念说明

`spi_interface` 的实体（entity）是对外的稳定契约，而架构（architecture）`behavioural` 内部只做三件事：例化 `spi_tx`、例化 `spi_rx`、例化一块 `fifo_async`，再用一条 FSM 把它们连起来。它本身几乎不产生数据通路逻辑，是典型的"集成层 / 编排层"。

理解它的关键是先看清楚 entity 的类属（generic）和端口（port）划分成哪几组：

- **SPI 协议参数组**：`SPI_CLK_POLARITY`、`SPI_CLK_PHASE`、`DATA_WIDTH`、`MSB_FIRST_AND_NOT_LSB`、`CONTROLLER_AND_NOT_PERIPHERAL`，原样下传给 `spi_tx`/`spi_rx`。
- **多片选参数**：`SPI_CHIPS_AMOUNT`（从机片数），决定 `selected_chips` 与 `spi_chip_select_n` 向量宽度。
- **时钟门控参数**：`ENABLE_INTERNAL_CLOCK_GATING`、`USE_XILINX_CLK_GATE_AND_NOT_INTERNAL`，下传给 `spi_tx`（见 u5-l1、u10-l2）。
- **TX FIFO 参数**：`TX_FIFO_DEPTH_IN_BITS`，FIFO 容量位宽，深度 \( = 2^{\text{TX\_FIFO\_DEPTH\_IN\_BITS}} \)；注释标注"设为 0 表示单字模式"（即深度 1）。
- **端口分组**：复位/时钟、`selected_chips`、**TX FIFO 写接口**（`tx_fifo_write_clk`/`write_enable`/`write_data`/`write_blocked`/`full`/`empty`/`words_stored`）、**流控接口**（`tx_trigger`/`spi_busy`）、**SPI 物理引脚**（`spi_clk_out`/`serial_data_out`/`serial_data_in`/`spi_chip_select_n`）、**接收接口**（`rx_data`/`rx_data_valid`）。

注意一个细节：`spi_chip_select_n` 声明为 `inout`（双向）。这是为了让同一个模块既能当**控制器（主）**——片选由内部驱动；也能当**外设（从）**——片选由外部主控驱动。本讲聚焦主模式，此时它由架构内的 `chip_select` 组合进程驱动。

#### 4.1.2 核心流程

顶层的数据流可以这样描述（主模式发送一路数据为例）：

```text
        ┌─────────────┐  write_data/write_clk/write_enable
上层 ──▶│  TX FIFO    │──────────────┐
        │ (fifo_async)│              │ read_data
        └─────────────┘              ▼
         ▲ read_enable/reset   ┌──────────┐  serial_data_out
         │ read_pointer        │  spi_tx  │────────────────▶ 从机
         │ (由 FSM 驱动)        │          │  spi_clk_out ────▶ 从机
         │                     └──────────┘  spi_chip_select_n▶ 从机
         │                          ▲              │
         │                     tx_data_valid       │(inout)
         │                     tx_data_ack         ▼
         │                     tx_is_ongoing  ┌──────────┐
         │                          │         │  spi_rx  │◀─ serial_data_in
         │                          │         └──────────┘──▶ rx_data/valid
         │                          │
         └────────── FSM (调度三块积木的协同时序) ──────────┘
```

FSM 是唯一的"指挥官"，它决定何时从 FIFO 读一个字、何时把字交给 `spi_tx`、何时切换片选、何时重放读指针。`spi_tx` 和 `spi_rx` 一旦收到片选与时钟就自主完成位的搬移。

#### 4.1.3 源码精读

实体的类属与端口声明：

[spi_interface.vhd:34-72](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_interface.vhd#L34-L72) — entity 声明。注意 `SPI_CHIPS_AMOUNT` 决定 `selected_chips`/`spi_chip_select_n` 的宽度，`TX_FIFO_DEPTH_IN_BITS` 决定 FIFO 深度，`spi_chip_select_n` 是 `inout`。

架构开头的状态类型与内部信号定义：

[spi_interface.vhd:74-96](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_interface.vhd#L74-L96) — 定义 `state_t` 六状态枚举、`state`/`current_chip_index` 状态寄存器，以及 FSM 与 `spi_tx`、FIFO 之间的一组内部握手信号（`tx_data_valid`、`tx_data`、`tx_data_ack`、`tx_fifo_read_enable`、`tx_fifo_reset_read_pointer`、`spi_chip_select_n_internal` 等）。

三块积木的例化：

[spi_interface.vhd:193-229](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_interface.vhd#L193-L229) — `spi_tx_inst` 与 `spi_rx_inst`。二者共享 `spi_chip_select_n_internal`（同一片选）与 `spi_clk_in`（同一时钟），构成全双工。`spi_tx` 的 `tx_data`/`tx_data_valid`/`tx_data_ack`/`tx_is_ongoing` 全部连到 FSM 内部信号。

[spi_interface.vhd:231-249](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_interface.vhd#L231-L249) — `fifo_async_inst`，**显式选定 `own_behavioural_async_fifo` 架构**。读时钟接 `spi_clk_in`，写时钟接外部 `tx_fifo_write_clk`；`reset_read_pointer` 接到 FSM 的 `tx_fifo_reset_read_pointer`。这一行选定自研架构，正是广播重放能真实生效的前提。

#### 4.1.4 代码实践

1. **实践目标**：在不运行仿真的前提下，凭源码画出 `spi_interface` 的"对外端口 → 内部积木"连线表。
2. **操作步骤**：打开 `spi_interface.vhd`，对照 entity 的 port 列表与 architecture 体里的三个例化（`spi_tx_inst`、`spi_rx_inst`、`fifo_async_inst`），逐个端口填写"它来自/去往哪里"。
3. **需要观察的现象**：哪些 entity 端口直接连到积木（透传），哪些经过 FSM 调度（如 `tx_data_valid` 由 FSM 产生），哪些是 FSM 独有（如 `spi_busy`）。
4. **预期结果**：应得出类似结论——`tx_fifo_write_*` 一组直连 FIFO 的写口；`serial_data_out`/`spi_clk_out` 直连 `spi_tx` 输出；`rx_data`/`rx_data_valid` 直连 `spi_rx` 输出；而 `spi_busy`、`tx_fifo_read_enable`、`tx_fifo_reset_read_pointer`、`tx_data_valid` 只在 FSM 内部出现，是"调度信号"。
5. 结论性判断，无需本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`spi_interface` 例化 FIFO 时为什么必须写 `entity work.fifo_async(own_behavioural_async_fifo)`，而不是让综合器自己选架构？

**参考答案**：因为"广播重放"依赖 `reset_read_pointer` 真正回卷读指针，而只有 `own_behavioural_async_fifo` 架构实现了真正的读指针清零；Xilinx/Intel 版本对该信号的处理不同（屏蔽读 / 忽略）。显式选定架构能保证仿真行为与设计意图一致。直接例化语法 `entity work.xxx(arch_name)` 正是 u2-l1 讲过的"选定具体架构"用法。

**练习 2**：`spi_chip_select_n` 为什么是 `inout` 而不是 `out`？

**参考答案**：为了让同一份接口既能工作在控制器（主）模式——片选由内部 `chip_select` 进程驱动输出，也能工作在外设（从）模式——片选由外部主控驱动、本模块只采样。`inout` 配合内部是否驱动（主模式下驱动、从模式下高阻）来实现复用。

---

### 4.2 多片选 FSM：六状态调度机

#### 4.2.1 概念说明

`fsm` 进程是 `spi_interface` 的大脑。它是一条对 `spi_clk_in` 敏感的同步时序进程，内部用 `case state is` 实现 6 状态机。它的职责可以用一句话概括：**逐字地从 FIFO 取数据、逐字地交给 spi_tx 发送，并在 FIFO 取空后决定"换下一片从机重放"还是"结束回到 idle"**。

六个状态是：

| 状态 | 一句话职责 |
| --- | --- |
| `idle` | 空闲。等待 `tx_trigger` 与"至少一片被选中且 FIFO 非空"。 |
| `fetch_data` | 从 FIFO 取一个字（拉一拍 `read_enable`）。若 FIFO 已空，决定换片或结束。 |
| `wait_for_data` | 等 FIFO 读出数据有效（`read_data_valid`），把它锁存为 `tx_data`。 |
| `wait_for_acknowledge` | 把 `tx_data_valid` 保持为高，等 `spi_tx` 回 `tx_data_ack` 表示已接收。 |
| `reset_for_next_chip` | 切换到下一片从机，并拉 `reset_read_pointer` 让 FIFO 读指针回卷以重放。 |
| `wait_for_transfer_end` | FIFO 已空但 `spi_tx` 上一字还没发完，等它发完再回 `idle`。 |

#### 4.2.2 核心流程

先看进程顶部"每拍默认赋值"的套路，这是读懂 FSM 的钥匙：

[spi_interface.vhd:117-127](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_interface.vhd#L117-L127) — 每个 `rising_edge(spi_clk_in)` 先把 `tx_data_valid`、`tx_fifo_read_enable`、`tx_fifo_reset_read_pointer` 默认置 `'0'`，把 `spi_busy` 默认置 `'1'`。随后各状态只在需要时把某个信号"拉高一拍"。这种"默认低、按需拉高"的写法保证了脉冲信号是单拍脉冲，避免多次触发。

复位分支 `rst_n='0'` 把 `tx_fifo_write_blocked<='0'`、`state<=idle`、`current_chip_index<=0`。注意复位时 `spi_busy` 取默认值 `'1'`（即复位期间报"忙"），这与测试台 `test_reset_behavior` 中"复位期间 `spi_busy` 应为 1"的断言一致。

单芯片、多字发送的主循环（不含换片）状态流转如下：

```text
        tx_trigger=1 且 FIFO 非空 且 找到片
  idle ──────────────────────────────────▶ fetch_data
   ▲                                         │ read_enable=1
   │                                         ▼
   │ no tx_is_ongoing                   wait_for_data
   │                                         │ read_data_valid=1
   │                                         │ tx_data<=read_data, tx_data_valid=1
   │                                         ▼
   │                                   wait_for_acknowledge
   │      (tx_data_ack=1) ◀──────────────────┘ tx_data_valid 保持高
   │                  │
   │                  ▼
   │             fetch_data  ──(FIFO 空)──▶ 见 4.2 换片分支
   └──────────────── ▲
       spi_busy=0       每个 fetch_data 取一个字，循环直到 FIFO 空
```

`fetch_data` 在 FIFO 已空时的"换片或结束"分支是本状态机最精妙的部分：

[spi_interface.vhd:143-156](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_interface.vhd#L143-L156) — 当 `tx_fifo_empty='1'`：调用 `get_next_selected_chip` 找下一片；若找到 → `reset_for_next_chip`；若没下一片且当前没有正在发送的字（`not tx_is_ongoing`）→ `idle`（全部结束）；若没下一片但 `spi_tx` 仍在发最后一个字 → `wait_for_transfer_end`，等它发完再回 `idle`。

这里有一个**关键并发细节**值得强调：FIFO 取空 ≠ `spi_tx` 已经发完。FSM 把一个字交给 `spi_tx`（`wait_for_acknowledge` 收到 ack 后回到 `fetch_data`）后，`spi_tx` 还需要 `DATA_WIDTH` 个 SPI 时钟周期才能把这一字逐位移完。`tx_is_ongoing` 信号（由 `spi_tx` 输出，见 [spi_tx.vhd:104](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_tx.vhd#L104)）正是用来表达"最后一字还没移完"。所以 `wait_for_transfer_end` 的存在，是为了避免在最后一字的位串行还没上线时就把片选拉高、提前结束传输。

#### 4.2.3 源码精读

六状态的完整 case 分支：

[spi_interface.vhd:128-180](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_interface.vhd#L128-L180) — `idle` 把 `spi_busy<='0'`、`tx_fifo_write_blocked<='0'`，并在 `tx_trigger and not tx_fifo_empty` 时锁定 `selected_chips_reg`、调 `get_next_selected_chip` 拿到首片、置 `tx_fifo_write_blocked<='1'` 后跳 `fetch_data`。其余状态职责如上节所述。`when others => state <= idle` 是防御性兜底。

`wait_for_data` 锁存数据：

[spi_interface.vhd:157-162](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_interface.vhd#L157-L162) — 等 `tx_fifo_read_data_valid` 一拍，把 `tx_fifo_read_data` 赋给 `tx_data` 并拉 `tx_data_valid`，进入握手等待。

`wait_for_acknowledge` 保持握手：

[spi_interface.vhd:163-167](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_interface.vhd#L163-L167) — 在整个状态内 `tx_data_valid<='1'`（持续保持，而非单拍），直到收到 `tx_data_ack` 才回到 `fetch_data`。这是"电平握手"：数据持续有效，直到发送方确认接收。

`reset_for_next_chip` 换片并重放：

[spi_interface.vhd:168-173](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_interface.vhd#L168-L173) — 拉 `tx_fifo_reset_read_pointer<='1'` 一拍，同时更新 `current_chip_index`，并在 FIFO 非空时回到 `fetch_data` 开始重放。详见 4.4 节。

`wait_for_transfer_end` 收尾：

[spi_interface.vhd:174-177](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_interface.vhd#L174-L177) — 只等 `not tx_is_ongoing` 即回 `idle`。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：用源码确认"`spi_busy` 在何时为 0、何时为 1"。
2. **操作步骤**：在 `spi_interface.vhd` 中搜索所有对 `spi_busy` 的赋值。注意进程顶部 `spi_busy <= '1'`（[L121](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_interface.vhd#L121)）是默认值，只有 `idle` 状态把它覆盖为 `'0'`（[L130](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_interface.vhd#L130)）。
3. **需要观察的现象**：`spi_busy` 实际上等价于"FSM 不在 idle"。复位期间（reset 分支没覆盖 `spi_busy`，取默认 `'1'`）也为 1。
4. **预期结果**：得出"`spi_busy='0'` 当且仅当 `state=idle` 且非复位"。这解释了测试台为何用 `wait until spi_busy` / `wait until not spi_busy` 作为传输起止的同步点。
5. 结论性判断，无需本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `wait_for_acknowledge` 里要把 `tx_data_valid` 设成电平（整段保持高），而 `fetch_data` 里 `tx_fifo_read_enable` 是单拍脉冲？

**参考答案**：二者握手对象不同。`tx_fifo_read_enable` 是对 FIFO 的"读一次"命令，FIFO 在下一个读时钟沿响应一次，所以只需一拍脉冲；若持续拉高会连续读出多个字。`tx_data_valid` 是与 `spi_tx` 的就绪/有效握手，`spi_tx` 在 `tx_data_valid` 高且自身未在发送时才会捕获数据（见 `spi_tx` 的 `if (?? tx_data_valid) and not tx_started`），因此必须把数据"稳定地摆在那里"直到对方回 `tx_data_ack`，故用电平。

**练习 2**：如果删除 `wait_for_transfer_end` 状态，直接在 `fetch_data` 的"FIFO 空且无下一片"分支跳 `idle`，会有什么后果？

**参考答案**：当最后一字刚被 `spi_tx` 接收（ack 已回）但 8 位串行数据尚未全部上线时，FSM 就会回到 `idle`、拉高片选、令 `spi_busy='0'`。结果是最后一字的尾部几位会被截断（片选提前撤销），从机收到的最后一个字不完整。`wait_for_transfer_end` 正是堵这个漏洞。

---

### 4.3 get_next_selected_chip：一次只选一片

#### 4.3.1 概念说明

`get_next_selected_chip` 是 FSM 进程内部的一个 `impure function`，解决的问题很明确：给定一个"哪些片被选中"的位图 `selected_chips`（某位为 1 表示该位对应的从机要被服务），以及"当前已经服务到哪片"的游标 `current_chip_index_v`，返回**下一片应当服务的芯片索引**，找不到时返回 `CHIP_INDEX_OUT_OF_RANGE`（一个越界哨兵值）。

它的两个关键性质是：

1. **严格升序**：芯片按索引从小到大依次被服务。
2. **一次返回一片**：调用一次只会让游标前进到下一个被选中的位置，绝不跳过中间未选中的片去"批量选"。

"哨兵值" `CHIP_INDEX_OUT_OF_RANGE` 被定义为 `selected_chips'length`（即片总数，比最大合法索引大 1），用来表示"没有更多被选中的片了"。

#### 4.3.2 核心流程

函数逻辑分两支：

```text
get_next_selected_chip:
  若 current_chip_index_v 已越界（≥ CHIP_INDEX_OUT_OF_RANGE，即"还没开始"）:
      返回 get_lowest_active_bit(selected_chips)   # 直接定位最低位的选中片
  否则（已经在服务某片，要找下一片）:
      循环：current_chip_index_v += 1
           直到 (越界) 或 (selected_chips_reg[该位] = 1)
      返回 current_chip_index_v
```

举例：`selected_chips = "00010101"`（片 0、2、4 被选中，`SPI_CHIPS_AMOUNT=8`，`CHIP_INDEX_OUT_OF_RANGE=8`）。

- 首次调用（游标越界）：走第一支，`get_lowest_active_bit` 返回 0 → 服务片 0。
- 发完片 0 后再次调用（游标=0）：走第二支，循环加到 1（未选中）、2（选中）→ 返回 2 → 服务片 2。
- 再调用（游标=2）：循环到 3（未选中）、4（选中）→ 返回 4 → 服务片 4。
- 再调用（游标=4）：循环到 5、6、7 都未选中、加到 8 越界 → 返回 8 = `CHIP_INDEX_OUT_OF_RANGE` → FSM 据此判定"全部服务完"，结束。

#### 4.3.3 源码精读

哨兵与游标信号定义：

[spi_interface.vhd:75-76](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_interface.vhd#L75-L76) — `CHIP_INDEX_OUT_OF_RANGE := selected_chips'length`；`current_chip_index` 范围 `0 to CHIP_INDEX_OUT_OF_RANGE`，恰好能容纳哨兵值。

函数体：

[spi_interface.vhd:103-115](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_interface.vhd#L103-L115) — 第一支直接 `return get_lowest_active_bit(selected_chips)`；第二支用一个 `for` 循环递增游标，循环出口条件 `exit when current_chip_index_v >= CHIP_INDEX_OUT_OF_RANGE or (?? selected_chips_reg(current_chip_index_v))`。`(?? ...)` 是 VHDL-2008 的条件运算符，把 `std_ulogic` 转 `boolean`，此处意为"该位为 1"。

注意它是 `impure function`，因为它读取了外部信号 `selected_chips`（第一支）与进程变量 `selected_chips_reg`（第二支）。`get_lowest_active_bit` 来自 `utils_pkg` 子模块（见 u3-l2），返回向量中最低有效位的索引。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：手动模拟 `get_next_selected_chip` 在一组 `selected_chips` 上的连续返回值。
2. **操作步骤**：取 `selected_chips = "10000101"`（`SPI_CHIPS_AMOUNT=8`，片 0、2、7 被选中）。逐次"调用"函数，按 4.3.2 的规则手算每次返回值。
3. **需要观察的现象**：服务顺序应是 0 → 2 → 7 → 越界（=8）。注意片 7 之后游标加到 8 立即越界，不会停留在 7。
4. **预期结果**：返回序列 `[0, 2, 7, 8]`，其中 8 触发结束。
5. 结论性判断，无需本地验证。

#### 4.3.5 小练习与答案

**练习 1**：函数第一支用 `selected_chips`（输入信号），第二支用 `selected_chips_reg`（锁存副本）。这两个值会不会不一致？为什么作者在 `idle` 里要先做一次 `selected_chips_reg := selected_chips`？

**参考答案**：在 `idle` 触发时刻，`selected_chips_reg := selected_chips` 把当时的输入快照进变量，之后整个广播周期内 FSM 都用这份快照（第二支）来决定后续芯片，即便用户中途改了 `selected_chips` 也不影响本次广播。第一支用的是实时 `selected_chips`，但它紧接着那条赋值执行，值相同。这样做是为了让"本次广播服务哪些片"在触发瞬间被冻结，保证确定性。

**练习 2**：为什么函数要声明为 `impure`？

**参考答案**：因为它读取了进程外的信号 `selected_chips` 与进程内的变量 `selected_chip_index_v`/`selected_chips_reg`——即它的返回值不仅取决于形参，还取决于外部/进程状态。VHDL 规定此类函数必须声明 `impure`。`pure` 函数的返回值只能依赖其参数。

---

### 4.4 TX FIFO 集成与 reset_read_pointer 广播重放

#### 4.4.1 概念说明

本模块最值得学习的设计是"**装载一次、广播多片**"。它把两个机制粘合在一起：

- **TX FIFO 写阻塞**：广播一旦开始（`idle` 触发），`tx_fifo_write_blocked` 拉高，封锁 FIFO 写口，保证重放期间数据不被篡改。
- **读指针重放**：每服务完一片，FSM 进入 `reset_for_next_chip`，拉一拍 `reset_read_pointer`，FIFO 把读指针清零（写指针与 RAM 内容不动），于是下一片从地址 0 重新读出同样的一批字。

这两条合起来，就实现了"用户写一遍 FIFO、多片从机各收到一份完整副本"的广播语义。

#### 4.4.2 核心流程

把第 4.2 节的"单芯片循环"和换片分支合起来，多片广播的完整时序是：

```text
idle ──(trigger, FIFO 非空, 片0)─▶ fetch_data ─┐
   ▲                                            │ 取字→wait_for_data→wait_for_ack→fetch_data
   │                                            │ ... 逐字直到 FIFO 空
   │                                            ▼
   │          (get_next_selected_chip → 片2)  reset_for_next_chip
   │                                            │ reset_read_pointer=1 (一拍)
   │                                            │ current_chip_index←片2
   │                                            │ FIFO 非空 → fetch_data
   │                                            ▼
   │                                   fetch_data ── 从地址0重放所有字给片2
   │                                            ⋮  (对片4、片7...同样重放)
   │
   └── (无下一片 且 not tx_is_ongoing) ── wait_for_transfer_end / idle
```

关键时序点：

- `tx_fifo_write_blocked` 在 `idle` 触发瞬间置 1（[L139](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_interface.vhd#L139)），直到回到 `idle` 才清 0（[L131](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_interface.vhd#L131)）。期间 FIFO 写使能被强制为 0。

- `reset_read_pointer` 是一拍脉冲，由 `reset_for_next_chip` 产生（[L169](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_interface.vhd#L169)）。FIFO 的读指针逻辑在**下一个读时钟沿**把它清零（`read_clk` 即 `spi_clk_in`）。

- 切换 `current_chip_index`（[L170](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_interface.vhd#L170)）让 `chip_select` 组合进程把片选指向新片（见下）。

#### 4.4.3 源码精读

FIFO 写使能的门控逻辑：

[spi_interface.vhd:191](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_interface.vhd#L191) — `tx_fifo_write_enable_internal <= rst_n and not tx_fifo_full and not tx_fifo_write_blocked and tx_fifo_write_enable`。四个条件相与：非复位、未满、未被广播阻塞、用户确实在写。广播期间第二项 `not tx_fifo_write_blocked` 为假，写口关闭。

片选译码（决定哪一片的片选被拉低）：

[spi_interface.vhd:185-189](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_interface.vhd#L185-L189) — 组合进程：先把整个 `spi_chip_select_n` 向量赋全 `'1'`（全部不选），再单独把 `spi_chip_select_n(current_chip_index)` 覆盖为 `spi_chip_select_n_internal`（来自 `spi_tx`：传输中为 `'0'`，空闲为 `'1'`）。VHDL 信号"后赋值胜出"的语义保证只有当前片随 `spi_tx` 动作，其余片恒为 `'1'`。这正是"一次只选一片"在硬件上的落地。

异步 FIFO 的读指针重放逻辑（`own_behavioural_async_fifo` 架构内）：

[fifo_async.vhd:241-256](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L241-L256) — `read_pointer_logic` 进程在 `rising_edge(read_clk)` 时：若 `reset_read_pointer` 为真，则把 `read_pointer_binary`/`read_pointer_gray` 清零（**注意是 `elsif`，与正常读互斥**）；否则正常 `read_enable` 时指针 +1。清零只动读指针，`write_pointer_binary` 与 RAM 内容不变，所以数据可重读。`empty` 标志（[L284](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L284)）由两格雷码指针比较得出，读指针回卷后会重新变"非空"。

`reset_read_pointer` 端口声明（默认 0，便于不接时安全）：

[fifo_async.vhd:36-37](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L36-L37) — 注释明确写"Reset read pointer for data replay (keeps write pointer intact)"，与 `spi_interface` 的用法一一对应。

#### 4.4.4 代码实践

1. **实践目标**：在测试台里构造一个"两片广播"场景，确认第二片收到的数据与第一片完全一致（即重放生效）。
2. **操作步骤**：
   - 打开 `tb_spi_interface.vhd`，定位常量区 [L51-L56](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_interface.vhd#L51-L56)（`CHIP_COUNT=8`、`DATA_WIDTH=8`、模式 0、MSB first）。
   - 参考已有的 `test_multi_word_fifo_multi_chip_streaming` 用例 [L492-L546](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_interface.vhd#L492-L546)：它选中片 0/2/4（`selected_chips <= (0=>'1',2=>'1',4=>'1',others=>'0')`，[L507](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_interface.vhd#L507)），每片写 5 个字，触发后用 `spi_monitor` 收集所有上线数据进 `received_queue` 再比对。
   - 新增一个最小用例：选中恰好两片（如 `(0=>'1',3=>'1',others=>'0')`），向 FIFO 写 3 个已知值（如 `0x11`、`0x22`、`0x33`），拉一拍 `tx_trigger`，开启 `loopback_enabled` 以便回采。在 `test_suite` 循环里 `elsif run("test_two_chip_replay")` 注册它（参考 [L553-L569](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_interface.vhd#L553-L569) 的注册结构）。
3. **需要观察的现象**：用配套的 `tb_spi_interface.do` 加载波形，它已经把 `/tb_spi_interface/DUT/state`（FSM 状态）、`/.../DUT/fsm/current_chip_index_v`（当前片游标）、`/.../DUT/fsm/selected_chips_reg`（选中片快照）、`tx_fifo_reset_read_pointer`、`tx_fifo_read_enable`、`spi_chip_select_n` 全部编入 `fsm` 与 `internal tx_fifo` 分组（见 [tb_spi_interface.do:22-40](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_interface.do#L22-L40)）。
4. **预期结果**：应能观察到 `state` 经历 `idle→fetch_data→wait_for_data→wait_for_acknowledge→（循环）→reset_for_next_chip→fetch_data→…→idle`；`current_chip_index_v` 先停在 0、在 `reset_for_next_chip` 跳到 3；`spi_chip_select_n` 先只有 bit0 为 0、之后只有 bit3 为 0；`tx_fifo_reset_read_pointer` 在换片瞬间出现一拍高电平。回采队列里两片各自收到 `0x11,0x22,0x33`（共 6 字）。
5. **运行方式**：按 u1-l3 的方式用 `test_runner.py` 运行；若本地无 EDA 工具，则按 4.4.5 的方式做纯源码追踪，标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：如果改用 `xilinx_behavioural_async_fifo` 架构例化 FIFO，广播重放会发生什么？

**参考答案**：Xilinx 架构里 `reset_read_pointer` 被接成 `rd_en => read_enable and not reset_read_pointer`（[fifo_async.vhd:103](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L103)），即"重放期间只是暂停读"，并不真正回卷读指针。结果是第二片只能收到第一片尚未读完的残余，广播语义被破坏。这正是 `spi_interface` 显式选定 `own_behavioural_async_fifo` 的根本原因。

**练习 2**：广播期间 `tx_fifo_write_blocked='1'`。如果允许用户在广播中途继续写 FIFO，会出什么问题？

**参考答案**：重放依赖"写指针冻结、读指针回卷"。若广播途中写口仍开放，新写入的数据会追加在原数据之后；当读指针回卷到 0 重读时，新数据会被当作"本批广播内容"一起重放给后续从机，导致数据错位、且不同从机收到的内容不一致。写阻塞保证了"本批广播内容"在触发瞬间就被冻结。

**练习 3**：`reset_read_pointer` 在 FIFO 的 `read_pointer_logic` 里是 `if reset_read_pointer ... elsif read_enable ...`（互斥）。为什么不能让二者同拍同时发生？

**参考答案**：回卷和正常前进是两种相反的指针操作，同拍同时生效会让指针状态不确定（先 +1 再清零，或先清零再 +1，结果不同）。用 `elsif` 互斥、并让 `reset_for_next_chip` 那拍不发 `read_enable`（FSM 顶部默认 `tx_fifo_read_enable<='0'`），保证了"先清零、下一拍再从 0 读"的确定性两拍序列。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成一次"两片从机广播"的全链路追踪与状态转移时序图绘制。

**任务**：在 `tb_spi_interface.vhd` 中新增用例 `test_two_chip_broadcast_trace`，做以下事，并最终画一张状态转移时序图。

1. **设置场景**（参考 4.4.4 的步骤）：复位后选中两片（`selected_chips <= (0=>'1',3=>'1',others=>'0')`），向 FIFO 写入两个固定字 `0xA5`、`0x3C`，拉一拍 `tx_trigger`，开启 `loopback_enabled <= true`。
2. **追踪 FSM**：用 `tb_spi_interface.do` 加载波形，重点观察 `DUT/state`、`DUT/fsm/current_chip_index_v`、`tx_fifo_reset_read_pointer`、`tx_fifo_read_enable`、`tx_data_valid`、`tx_data_ack`、`spi_chip_select_n`、`tx_fifo_words_stored`。
3. **画时序图**：以 SPI 时钟 `spi_clk_in` 为横轴，逐拍标注以下事件，至少覆盖到第二片发完回到 `idle`：
   - `idle` 中 `tx_trigger` 与 `tx_fifo_write_blocked` 拉高的时刻；
   - `fetch_data → wait_for_data → wait_for_acknowledge` 三拍把 `0xA5` 交给 `spi_tx`；
   - 第二个字 `0x3C` 同样的三拍循环；
   - FIFO 取空后进入 `reset_for_next_chip`，标出 `reset_read_pointer` 的单拍脉冲与 `current_chip_index_v` 从 0 跳到 3 的时刻；
   - 第二片重放 `0xA5`、`0x3C`；
   - 再次取空、无下一片、`wait_for_transfer_end`（等 `tx_is_ongoing` 变 0）、回 `idle`、`spi_busy` 变 0。
4. **校验数据**：借助测试台已有的 `spi_monitor` 进程（[L127-L161](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_interface.vhd#L127-L161)）把上线数据收集进 `received_queue`，断言两片各收到 `0xA5`、`0x3C`（即共 4 字，且前两字与后两字相同）。

**预期结论**：

- 第一片（bit0）与第二片（bit3）先后被独占选中，`spi_chip_select_n` 任意时刻至多一位为 0。
- `reset_read_pointer` 仅在换片瞬间出现一拍，且与 `read_enable` 互斥。
- `tx_fifo_words_stored` 在服务第一片期间递减到 0；换片重放后又恢复为 2（因读指针回卷、`words_stored = 同步后的写指针 − 读指针`，见 [fifo_async.vhd:217-226](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/fifo/fifo_async.vhd#L217-L226)），第二片期间再次递减到 0。

> 若本地暂无 NVC/ModelSim，可改为纯源码追踪：对照 `fsm` 进程的 `case` 分支，手推每个状态出口条件，画出同样的事件序列，并标注「待本地验证」。

---

## 6. 本讲小结

- `spi_interface` 是 SPI 子系统的**集成层**：用一条 FSM 把 `spi_tx`、`spi_rx`、`fifo_async` 三块积木缝合成一个"写 FIFO + 单脉冲触发 + 等忙结束"的简单对外接口。
- FSM 的 6 个状态（`idle` / `fetch_data` / `wait_for_data` / `wait_for_acknowledge` / `reset_for_next_chip` / `wait_for_transfer_end`）严格分工：前四个负责"逐字取数与握手发送"，`reset_for_next_chip` 负责换片重放，`wait_for_transfer_end` 负责等最后一字串行移位完成、避免截断。
- `get_next_selected_chip` 用"首调 `get_lowest_active_bit`、后续逐位上扫"的策略，保证多片按索引升序、**一次只选一片**地被服务；`selected_chips_reg` 在触发瞬间冻结选中片快照，使整次广播确定。
- 广播重放靠两个机制：`tx_fifo_write_blocked` 在广播期间封锁 FIFO 写口，`reset_read_pointer` 在换片瞬间回卷读指针（写指针与 RAM 不动），于是同一批数据被无损重读给每一片从机。
- 该重放语义**只在 `own_behavioural_async_fifo` 架构里真实成立**，所以 `spi_interface` 显式选定该架构；移植到 Xilinx/Intel FIFO 封装时广播行为会退化（屏蔽读 / 忽略），需重新评估。
- `spi_busy` 等价于"非 idle"（复位期间也为 1），是上层判断传输起止的同步信号；`spi_chip_select_n` 是 `inout`，兼顾主/从模式。

---

## 7. 下一步学习建议

本讲结束后，SPI 子系统（第 10 单元）已全部讲完。建议：

1. **横向对照验证方法学**：本讲的测试台 `tb_spi_interface.vhd` 同时用到了 OSVVM 随机化（`random.RandSlv`）、VUnit 队列（`queue_t`/`push`/`pop_std_ulogic_vector`）与 `check_equal`，以及一个独立的 `spi_monitor` 进程做参考模型收数。这正是 u11-l1（VUnit 测试台结构）与 u11-l2（OSVVM 随机化与断言）的鲜活样本，建议带着本讲对 FSM 的理解去读第 11 单元，理解"如何把一个 FSM 设计用随机激励全覆盖"。

2. **补全 SPI 模式覆盖**：注意本库的 SPI 测试台目前**只验证了模式 0**（`SPI_CLK_POLARITY='0'`、`SPI_CLK_PHASE='0'`，见 [tb_spi_interface.vhd:51-52](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_interface.vhd#L51-L52)），且 u10-l1 已指出 `spi_pkg` 的 `rx_active_edge` 在模式 2/3 与标准表可能相反。可作为一个进阶练习：把测试台参数化到四种模式，复跑 `test_two_chip_broadcast_trace`，定位哪些模式会失败。

3. **回顾异步 FIFO 全貌**：本讲频繁引用 `reset_read_pointer` 与 `words_stored`。若对它们的满空判定、格雷码指针跨域同步仍有疑问，建议重读 u9-l3（异步 FIFO 与格雷码指针）和 u9-l4（满空标志与读指针重放），把"存储底座 → 指针同步 → 重放机制 → 顶层调度"这条链路彻底打通。

4. **继续阅读源码**：以 `spi_interface.vhd` 的 FSM 为模板，尝试自己写一个"只广播不重放、每片发不同数据"的变体（例如去掉 `reset_read_pointer`、让 FIFO 连续读），对比两份设计的 FIFO 用量与行为差异，加深对本讲"装载一次、广播多片"这一设计取舍的理解。
