# AXI 总线与 IO 互连

## 1. 本讲目标

学完本讲后，你应当能够：

- 说出 Nyuzi 如何用 **AXI4 主接口** 把 L2 缓存的缺失填充与脏行写回接到系统内存，并解释一次「16 拍突发」是怎么来的。
- 区分两条物理上完全独立的外部总线：**面向缓存的 AXI 内存总线**与**面向外设寄存器的非缓存 IO 总线**，并说明它们各自由谁驱动。
- 跟踪一次外设寄存器读写从 dcache 被识别为 IO 访问、进入 `io_request_queue`、经 `io_interconnect` 串行化发往 `io_bus`、再唤醒线程的完整路径。
- 解释为什么 IO 访问必须让线程「挂起 + 回滚重放」，而其它线程仍能继续占用流水线。

## 2. 前置知识

本讲建立在 u6-l1～u6-l3 的内存层次认知之上。回顾三件已学过的事：

1. **L1D 是每核私有、L2 是所有核共享**。L1 缺失会经 `l1_l2_interface` 发往 L2（见 u6-l2）。
2. **L2 缺失要向「系统内存」取数据**。u6-l3 里我们提过一句「缺失请求入 fill 队列，经 AXI4 以 16 拍突发取回整行」——本讲就把这个 AXI4 黑盒彻底打开。
3. **缓存行 = 64 字节 = 向量宽度**（`CACHE_LINE_BYTES = NUM_VECTOR_LANES * 4 = 64`，见 [defines.svh:296-297](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L296-L297)）。这个 64 字节是本讲所有「拆成多少拍」计算的起点。

本讲还会用到两个新概念：

- **AXI4（AMBA AXI）**：ARM 提出的一种片上总线协议。它把一次传输拆成 5 条独立的「通道」（写地址、写数据、写响应、读地址、读数据），各自有自己的握手信号，可以交错。本讲不要求你懂 AXI 全部细节，只要理解「主设备（master）发起、从设备（slave）应答、每条通道用 valid/ready 握手」即可。
- **MMIO（Memory-Mapped I/O）**：把外设寄存器映射到一段物理地址空间，CPU 用普通的 load/store 指令就能读写外设。Nyuzi 把 `0xffff0000～0xffffffff` 这段高 16 位全 1 的地址划给了外设（还记得 u1-l4 里 UART 寄存器在 `0xffff0048` 吗？就是落在这一段）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `hardware/core/defines.svh` | 定义 `axi4_interface`、`io_bus_interface` 与 `ioreq_packet_t`/`iorsp_packet_t` 两种数据包 |
| `hardware/core/l2_axi_bus_interface.sv` | L2 与系统内存之间的 AXI4 主设备：吃掉 L2 的缺失/写回请求，驱动 AXI 突发传输，再把填回的数据重新注入 L2 流水线 |
| `hardware/core/io_interconnect.sv` | IO 总线互连：把所有核的 IO 请求串行化（同一时刻只服务一个），驱动外部 `io_bus`，并把响应路由回正确的核 |
| `hardware/core/io_request_queue.sv` | 每核一个的 IO 请求队列：每线程一条表项，负责「发起请求→挂起线程→收到响应→唤醒线程重放」的两阶段回滚机制 |
| `hardware/core/dcache_data_stage.sv` | L1D 数据级：判定地址是否落在 IO 区，若是则把请求改道发往 `io_request_queue` 而不是缓存 |
| `hardware/core/nyuzi.sv` | 顶层：暴露 `axi_bus` 与 `io_bus` 两个外部接口，并实例化 `l2_cache`（内含 `l2_axi_bus_interface`）与 `io_interconnect` |

一句话定位：**AXI 总线是「内存的高速公路」，IO 总线是「外设的小路」，两者从顶层就分开，互不相干。**

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 AXI4 主接口**（讲内存总线）、**4.2 IO 互连**（讲外设总线的串行化与仲裁）、**4.3 IO 请求队列**（讲线程如何为一次 IO 访问挂起与唤醒）。

### 4.1 AXI4 主接口

#### 4.1.1 概念说明

`l2_axi_bus_interface` 是 Nyuzi 整个内存层次的「出口」。L2 命中就在片上 SRAM 里解决；一旦 L2 缺失，或者要把脏行（dirty line）写回内存，就轮到这个模块上场：它把缓存行级别的请求翻译成 AXI4 总线事务，去读写挂在 AXI 上的系统内存（在 FPGA 板上通常是 SDRAM 控制器），再把取回的整行重新塞回 L2 流水线。

模块顶部的注释把这个职责说得很清楚：

```systemverilog
// L2 Bus Interface
// Receives L2 cache misses and writeback requests from the L2 pipeline and
// drives system memory interface to fulfill them. When fills complete,
// this reissues them to the L2 pipeline via the arbiter.
```

见 [l2_axi_bus_interface.sv:21-36](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_axi_bus_interface.sv#L21-L36)。注释还特意说明「我把所有总线逻辑集中在这个模块，是为了方便换成别的总线（比如 Wishbone）」——这也是为什么 Nyuzi 把 AXI 细节封在单独一个文件里的原因。

#### 4.1.2 核心流程

AXI4 有 5 条通道。这个模块只做两种事务：**写回（writeback）**和**填充（fill/read）**。

**写回事务**（把脏行写回内存）走 3 条通道：

```
STATE_IDLE
  → STATE_WRITE_ISSUE_ADDRESS   在 AW 通道给地址 + 突发长度
  → STATE_WRITE_TRANSFER        在 W 通道逐拍给数据，最后一拍拉 m_wlast
  → (等 B 通道 s_bvalid 写响应)
  → STATE_IDLE
```

**填充事务**（从内存读回整行）走 2 条通道：

```
STATE_IDLE
  → STATE_READ_ISSUE_ADDRESS    在 AR 通道给地址 + 突发长度
  → STATE_READ_TRANSFER         在 R 通道逐拍收数据，存进 fill_buffer
  → STATE_READ_COMPLETE         把填好的行重新注入 L2 流水线
  → STATE_IDLE
```

两条关键规则：

1. **写回优先于填充**。见 [l2_axi_bus_interface.sv:216-218](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_axi_bus_interface.sv#L216-L218) 的注释：如果先读后写，可能读到旧的脏数据；先写后读就避免了这个竞争。
2. **整行用一次突发（burst）传完**，不需要每拍都重新给地址。AXI 的 `len` 字段 = 拍数 − 1。

**突发长度怎么算？** 这是本模块最值得记的一个数：

\[
\text{BURST\_BEATS} = \frac{\text{CACHE\_LINE\_BITS}}{\text{AXI\_DATA\_WIDTH}} = \frac{512}{32} = 16
\]

也就是说，一个 64 字节的缓存行，按 AXI 数据宽度 32 位（4 字节）传，正好 16 拍。所以 `m_awlen = m_arlen = 16 - 1 = 15`。

#### 4.1.3 源码精读

**① 模块端口与 AXI 主设备角色。** 它声明自己是 `axi4_interface.master`，并向 L2 流水线提供「重新注入」的请求口：

[l2_axi_bus_interface.sv:42-66](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_axi_bus_interface.sv#L42-L66) —— 这段定义了模块的两组接口：左边 `axi_bus` 是对外接系统内存的 AXI4 主端口；右边一串 `l2bi_*` 信号是「把填回结果重新喂给 L2 仲裁级」的入口（`l2bi_request_valid`/`l2bi_request`/`l2bi_data_from_memory`）。

**② 突发长度等局部参数。**

[l2_axi_bus_interface.sv:85-92](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_axi_bus_interface.sv#L85-L92) —— 这里算出 `BURST_BEATS = CACHE_LINE_BITS / AXI_DATA_WIDTH = 16`、`BURST_OFFSET_WIDTH = $clog2(16) = 4`。`L2REQ_LATENCY = 4` 表示 L2 流水线在本模块之前还有 4 级，FIFO 的「快满」阈值据此提前反压，避免已在途的请求溢出（和 u6-l3 里讲的提前反压是同一个套路）。

**③ 把 AXI 长度字段设成「拍数 − 1」。**

[l2_axi_bus_interface.sv:182-190](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_axi_bus_interface.sv#L182-L190) —— `m_awlen = m_arlen = BURST_BEATS - 1`。这正是 AXI 规范 A3.4.1 的规定（注释里也引用了）。同时 `m_bready = 1` 表示始终准备好接收写响应。

**④ 写回优先 + 填充的两条状态机入口。**

[l2_axi_bus_interface.sv:214-247](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_axi_bus_interface.sv#L214-L247) —— `STATE_IDLE` 里先看 `writeback_pending`，再看 `fill_request_pending`，体现写回优先。填充分支里还有两个优化：若是「冲突缺失」（`l2bi_collided_miss`，即别的请求已经在替这一行取数据）或「整行 store」（`store_mask` 全 1），就跳过真正的总线读，直接走 `STATE_READ_COMPLETE` 让 L2 自己去 reconcile。

**⑤ 收数据进 fill_buffer、并给地址/数据线赋值。**

[l2_axi_bus_interface.sv:365-373](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_axi_bus_interface.sv#L365-L373) —— 读传输时把每拍 `s_rdata` 按 `burst_offset` 存进 `fill_buffer`；地址线把缓存行索引后面补 0（`{address, {CACHE_LINE_OFFSET_WIDTH{1'b0}}}`，即对齐到行首）；写数据按拍从 `writeback_lanes` 取出。`fill_buffer` 最终经 generate 块拍平成 `l2bi_data_from_memory`（一个完整 512 位缓存行）回送 L2。

**⑥ AXI4 接口本身的定义。**

[defines.svh:427-469](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L427-L469) —— 5 条通道各用一对 valid/ready 握手：写地址（`m_awvalid`/`s_awready`）、写数据（`m_wvalid`/`s_wready`，加一个 `m_wlast` 标末拍）、写响应（`s_bvalid`/`m_bready`）、读地址（`m_arvalid`/`s_arready`）、读数据（`s_rvalid`/`m_rready`）。`modport master/slave` 分别规定了主从两端各驱动哪些信号。

#### 4.1.4 代码实践

**实践目标：** 把「缓存行 → 16 拍突发」这条链路在源码里走一遍，理解突发长度不是写死的常数，而是由两个配置参数相除得到的。

**操作步骤：**

1. 打开 [config.svh](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/config.svh)，确认 `AXI_DATA_WIDTH = 32`。
2. 打开 [defines.svh:296-299](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L296-L299)，确认 `CACHE_LINE_BYTES = NUM_VECTOR_LANES * 4 = 64`，故 `CACHE_LINE_BITS = 512`。
3. 打开 [l2_axi_bus_interface.sv:91](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_axi_bus_interface.sv#L91)，看 `BURST_BEATS = CACHE_LINE_BITS / \`AXI_DATA_WIDTH`。
4. 打开 [l2_axi_bus_interface.sv:184-185](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_axi_bus_interface.sv#L184-L185)，看 `m_awlen = m_arlen = BURST_BEATS - 1`。

**需要观察的现象 / 预期结果：** 默认配置下 `BURST_BEATS = 16`、`m_awlen = m_arlen = 15`。如果你把 `AXI_DATA_WIDTH` 改成 64（假设 AXI 总线变宽到 64 位），重新计算应是 `BURST_BEATS = 8`、len = 7——这正是参数化带来的灵活性，无需改状态机本身。

> 待本地验证：若要确认波形，可构建 Verilator 模型 `nyuzi_vsim` 并跑一个会触发 L2 缺失的测试（如 `tests/core/cache`），用 `--vcd` 导出波形后观察 `m_arvalid`/`m_rvalid` 之间恰好 16 拍数据。

#### 4.1.5 小练习与答案

**练习 1：** 为什么写回（writeback）要优先于填充（fill）？

> **参考答案：** 若先发起填充读，可能在脏行尚未写回内存时读到旧值，造成数据竞争与不一致；先写后读保证读到的总是最新数据。见 [l2_axi_bus_interface.sv:216-218](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_axi_bus_interface.sv#L216-L218) 的注释。

**练习 2：** 如果某天 Nyuzi 把缓存行改成 128 字节（仍保持 `AXI_DATA_WIDTH=32`），`m_arlen` 会变成多少？

> **参考答案：** `CACHE_LINE_BITS = 1024`，`BURST_BEATS = 1024/32 = 32`，`m_arlen = 31`。注意 AXI4 单次突发上限是 256 拍，32 拍仍在合法范围内。

### 4.2 IO 互连

#### 4.2.1 概念说明

IO 总线跟内存总线是两套独立的东西。内存总线（AXI）只服务缓存：搬运整行、走突发、面向 SDRAM 这类块设备。**IO 总线则面向外设寄存器**——一次只读写一个 32 位字，**完全不经过缓存**，因为外设寄存器是「易变」的（状态随时变化，缓存它没有意义，甚至有害）。

`io_interconnect` 是这条 IO 小路的「交警」：它从所有核收集 IO 请求，**串行化**——同一时刻只把一个请求送上 `io_bus`——再把读回来的数据路由回正确的核和线程。之所以要串行化，是因为 `io_bus` 协议非常简单（下面会看到只有一组 read_en/write_en/address/data），没有 AXI 那种多通道并发能力，只能一次处理一笔。

#### 4.2.2 核心流程

IO 互连每完成一笔请求需要固定的若干拍，流程是：

```
任意核的 io_request_queue 拉高 ior_request_valid
        │
        ▼
rr_arbiter 在 NUM_CORES 个核之间轮询，选出一个 grant（独热码）
        │
        ▼  本拍：根据 grant 驱动 io_bus.read_en/write_en/address/write_data
        │         并把 grant 的核号/线程号存入 request_core/request_thread_idx
        ▼  下一拍：io_bus.read_data 出现（slave 一拍后才给读数据）
        │         request_sent <= 1
        ▼  再下一拍：ii_response_valid <= 1，带上 read_value 回送给所有核
```

响应是**广播**给所有核的（见顶层 [nyuzi.sv:131](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/nyuzi.sv#L131)，`ii_response` 接到每个 core），各核的 `io_request_queue` 自己用 `ii_response.core == CORE_ID` 判断「这笔响应是不是给我的」——这是 u6-l2 里 L2 响应同款的路由思路。

#### 4.2.3 源码精读

**① 模块端口：多核请求输入、单总线输出。**

[io_interconnect.sv:26-38](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/io_interconnect.sv#L26-L38) —— `ior_request_valid`/`ior_request` 是数组（每核一份请求），输出 `ii_response`/`ii_response_valid` 单份，外加一个 `io_bus.master`。这就是「多入一出 + 单响应广播」的拓扑。

**② 多核轮询仲裁。**

[io_interconnect.sv:55-85](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/io_interconnect.sv#L55-L85) —— 用 `generate` 区分单核与多核：多核时实例化 `rr_arbiter`（轮询仲裁器，与 u4-l3 里线程选择用的是同一类部件）和 `oh_to_idx`（独热转下标），选出 `grant_idx` 与 `grant_request`；单核时直接透传，省掉仲裁逻辑。注释提到 core ID 位宽被硬编码，原因是某些综合工具在 `NUM_CORES==1` 时对 `$clog2` 处理有问题（u3-l3 讲过）。

**③ 驱动 io_bus。**

[io_interconnect.sv:87-90](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/io_interconnect.sv#L87-L90) —— `write_en = grant && store`、`read_en = grant && !store`，二者互斥；地址和数据直接来自 `grant_request`。

**④ 响应时序（关键的两拍延迟）。**

[io_interconnect.sv:92-119](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/io_interconnect.sv#L92-L119) —— 第一个 `always_ff` 在有请求的那拍把核号、线程号、读数据锁存进 `ii_response`（注意 `io_bus.read_data` 此刻刚好出现，因为接口注释说「slave 在 read_en 后一拍给出 read_data」）；第二个 `always_ff` 用两级寄存器 `request_sent → ii_response_valid` 产生对齐的 valid。两者配合保证 valid 与数据同拍到达。

**⑤ io_bus_interface 的极简定义。**

[defines.svh:413-425](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L413-L425) —— 只有 5 根信号：`write_en`/`read_en`（互斥）、`address`、`write_data`、`read_data`。注释明确「read_en 拉高后，read_data 下一拍出现」。与 AXI 的 5 通道相比，这是面向慢速外设寄存器的极简总线。

**⑥ 请求/响应数据包。**

[defines.svh:394-407](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L394-L407) —— `ioreq_packet_t` 携带 `store`、`thread_idx`、`address`、`value`；`iorsp_packet_t` 携带 `core`、`thread_idx`、`read_value`。注意请求包里**没有 core 字段**（因为每核自己有一根请求线，谁拉高就是谁），但响应包里**有 core 字段**（因为响应是广播，接收方要据此认领）。

#### 4.2.4 代码实践

**实践目标：** 在单核配置下手动推演一次 IO 读的时序，看清 valid 与 read_data 的对齐关系。

**操作步骤：**

1. 读 [io_interconnect.sv:104-119](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/io_interconnect.sv#L104-L119) 的两级寄存器。
2. 假设 T 拍 `ior_request_valid[0]=1`，`grant_request` 是一个 load。
3. 推演：T 拍 `read_en` 被驱动、`request_sent` 待下一拍置 1；T+1 拍 `io_bus.read_data` 出现并被锁进 `ii_response.read_value`、`request_sent=1`；T+2 拍 `ii_response_valid=1`。

**需要观察的现象 / 预期结果：** 从请求被接受到响应 valid 拉高，固定 2 拍延迟（外设 slave 自身的读延迟另算，被接口规范折算进「read_data 下一拍出现」）。这也意味着 IO 读的最小代价就是这几拍，远比缓存的 1 拍命中慢。

> 待本地验证：以上为静态读码推演；若要在波形中确认，需要构建带 testbench 假外设的仿真并触发一次 MMIO 读（例如 UART 输出路径），观察 `ii_response_valid` 相对 `ior_request_valid` 的节拍差。

#### 4.2.5 小练习与答案

**练习 1：** 为什么请求包 `ioreq_packet_t` 没有 `core` 字段，而响应包 `iorsp_packet_t` 有？

> **参考答案：** 请求是「每核一根独立请求线」送给 `io_interconnect`，谁拉高就来自哪个核，无需在包里标；而响应是单根线**广播**回所有核，每个核需要用 `ii_response.core == CORE_ID` 判断是否属于自己的，所以必须带 core 字段。见 [defines.svh:394-407](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L394-L407) 与 [io_request_queue.sv:104](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/io_request_queue.sv#L104)。

**练习 2：** 如果把 `io_interconnect` 换成「同时能服务两个核」的并发互连，需要改动哪些假设？

> **参考答案：** 当前 `io_bus` 只有一组 read_en/write_en/address/data，物理上只能传一笔；要并发就得把 `io_bus` 改成多组（或改用类似 AXI 的多通道协议），并改造 `io_request_queue` 的响应认领逻辑。本讲暂不展开，说明 IO 总线「串行化」是协议层面的硬约束。

### 4.3 IO 请求队列

#### 4.3.1 概念说明

前面两个模块讲的是「总线本身」。`io_request_queue` 讲的是**线程这一侧**怎么跟慢速 IO 总线打交道。它是每个核内部的一个小模块（`core.sv` 里实例化，参数 `CORE_ID`），核心矛盾是：

> 流水线是非阻塞的（cache 缺失时切换到别的线程），但一次 IO 读的值要等好几拍才回来，**发起 IO 读的那个线程没法立刻拿到数据继续往下走**——存在真实的数据依赖。

Nyuzi 的解法和处理 cache 缺失一模一样：**回滚 + 挂起 + 唤醒重放**。线程第一次执行到 IO 指令时，发起请求、把自己挂起、回滚 PC 重取这条指令；等响应回来了，唤醒线程，让它把同一条指令**再执行一遍**——这一遍直接从队列里取到已经存好的读值，顺利写回，继续前进。

模块顶部注释点明了「总是阻塞线程直到事务完成」：

[io_request_queue.sv:21-24](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/io_request_queue.sv#L21-L24) —— 「Handles non-cacheable memory operations to memory mapped registers. These always block the thread until the transaction is complete.」

#### 4.3.2 核心流程

每个线程在队列里有一条表项 `pending_request[thread]`，含 `valid`、`request_sent`、`store`、`address`、`value`。整个生命周期分两阶段：

```
阶段①「发起」(第一次执行该 IO 指令)
  dcache 送来 dd_io_read_en, pending[thread].valid==0
    → valid <= 1，记录 address/value/store
    → ior_rollback_en <= 1          // 回滚，PC 重取本指令
    → ior_pending[thread] 置位       // 通知解码级：这个线程有未完成 IO，别让它乱跑
    → rr_arbiter 在本核各线程间选一个未发送的，经 ior_request 发往 io_interconnect
        → 被 grant 后 request_sent <= 1

阶段②「完成」(响应回来后被唤醒，第二次执行同一指令)
  io_interconnect 广播 ii_response，本核认领 (core==CORE_ID)
    → pending[thread].value <= ii_response.read_value
    → ior_wake_bitmap[thread] 置位   // 唤醒线程
  线程重放同一指令，dcache 再次送 dd_io_read_en, 此时 pending[thread].valid==1
    → valid <= 0                    // 请求完成，清掉表项
    → ior_rollback_en <= 0          // 不再回滚，这次正常写回
    → ior_read_value 提供 read 数据给写回级
```

为什么这套机制不会浪费周期？因为线程被挂起期间，`thread_select_stage` 会轮询到**别的就绪线程**继续发射指令（这正是 u4-l3 多线程调度的价值）。挂起的线程只在响应到达时才被唤醒。

#### 4.3.3 源码精读

**① 每线程一条表项。**

[io_request_queue.sv:58-64](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/io_request_queue.sv#L58-L64) —— `pending_request` 是一个 `THREADS_PER_CORE` 项的数组，每项是一个 packed struct。用线程号当索引，天然按线程隔离，无需额外的线程上下文保存。

**② 两阶段「发起/完成」的关键 always_ff。**

[io_request_queue.sv:79-118](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/io_request_queue.sv#L79-L118) —— 当 dcache 送来 `dd_io_write_en|dd_io_read_en` 且本线程表项 `valid==0`，走「Request initiated」分支（置 valid、记录信息）；若 `valid==1`，走「Request completed」分支（清 valid）。响应到达时（`ii_response_valid && core==CORE_ID && thread 匹配`）把 `read_value` 写进表项，并带断言「响应到达时表项必须是 pending 的」。被 grant 后 `request_sent<=1`。

**③ 回滚信号：只在「发起」那一拍拉高。**

[io_request_queue.sv:144-160](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/io_request_queue.sv#L144-L160) —— `ior_rollback_en` 在 `dd_io && !valid` 时置 1（发起），否则置 0（完成）。这个回滚信号最终汇入写回级的回滚仲裁，触发取指级刷新流水线、重取同一指令。注意它与 cache 缺失回滚共用同一套回滚基础设施，只是触发源不同。

**④ 挂起与唤醒位图。**

[io_request_queue.sv:74-77](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/io_request_queue.sv#L74-L77) —— `ior_pending[thread]` = 已发送未完成 或 本拍被选中发送；这个位图送给 `instruction_decode_stage`，让挂起线程的指令保持抑制状态（与中断替换指令类似的精确控制）。  
[io_request_queue.sv:130-135](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/io_request_queue.sv#L130-L135) —— `ior_wake_bitmap` 在本核收到响应时，用 `idx_to_oh` 把 `ii_response.thread_idx` 转成独热位图，送给 `thread_select_stage` 唤醒对应线程。这与 u6-l2 里 L2 响应唤醒 cache 缺失线程的 `wake_bitmap` 是同构机制。

**⑤ 本核内各线程轮询发送。**

[io_request_queue.sv:120-128](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/io_request_queue.sv#L120-L128) —— `send_request[thread]` = valid 且未发送；`rr_arbiter` 在本核各线程间轮询，选出一个送 `io_interconnect`。注意这是**两级仲裁**：先核间（io_interconnect），再核内线程间（io_request_queue）。

**⑥ dcache 如何判定并改道到 IO。**

[dcache_data_stage.sv:192](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L192) —— `addr_in_io_region = dt_request_paddr ==? 32'hffff????`，用 `==?` 通配比较：物理地址高 16 位为 `0xffff` 即外设区（低 16 位任意）。  
[dcache_data_stage.sv:203-204](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L203-L204) —— `io_access_req = memory_access_req && addr_in_io_region`；反之 `cached_access_req = memory_access_req && !addr_in_io_region`。一条 load/store 在数据级被判为「要么走缓存、要么走 IO」，二选一。  
[dcache_data_stage.sv:304-314](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L304-L314) —— 一旦是 IO 访问且无 TLB 缺失/fault，就拉 `dd_io_write_en`/`dd_io_read_en` 改道发往 `io_request_queue`，地址只取低 16 位（`{16'd0, dt_request_paddr[15:0]}`）。

#### 4.3.4 代码实践（本讲主任务）

**实践目标：** 完整叙述一次「外设寄存器写」从 dcache 到 io_bus 再到线程唤醒的全路径，并解释为什么 IO 访问必须让线程挂起等待响应。

**操作步骤：**

1. **触发点。** 在 [dcache_data_stage.sv:192](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L192) 确认：一条 `store_32` 到 `0xffff0048`（UART 数据寄存器，回忆 u1-l4）经 TLB 翻译后物理地址高 16 位是 `0xffff`，被判为 `addr_in_io_region`。
2. **改道。** 在 [dcache_data_stage.sv:304-307](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L304-L307) 确认：因为是 store 且非 load、无 tlb_miss/fault，拉 `dd_io_write_en`，把写值与低 16 位地址送给 `io_request_queue`。
3. **发起 + 回滚。** 在 [io_request_queue.sv:85-101](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/io_request_queue.sv#L85-L101) 看发起分支置 `valid=1`；在 [io_request_queue.sv:155-156](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/io_request_queue.sv#L155-L156) 看 `ior_rollback_en<=1`。线程被回滚并经 `ior_pending` 挂起。
4. **送总线。** 在 [io_request_queue.sv:120-142](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/io_request_queue.sv#L120-L142) 看本核线程仲裁后送出 `ior_request`；在 [io_interconnect.sv:87-90](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/io_interconnect.sv#L87-L90) 看核间仲裁后驱动 `io_bus.write_en`。
5. **响应与唤醒。** 外设写通常不需要回读值，但响应仍会返回：在 [io_interconnect.sv:116-118](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/io_interconnect.sv#L116-L118) 看 `ii_response_valid` 拉高；在 [io_request_queue.sv:134-135](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/io_request_queue.sv#L134-L135) 看 `ior_wake_bitmap` 唤醒线程。
6. **重放完成。** 线程重放同一条 store，这次 `valid==1`，在 [io_request_queue.sv:88-92](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/io_request_queue.sv#L88-L92) 走「Request completed」清 valid，`ior_rollback_en<=0`，指令正常完成。

**需要观察的现象 / 预期结果：** 同一条 store 指令在流水线里出现两次（回滚重放），中间隔着线程挂起窗口。

**为什么必须挂起？请用一句话解释：**

- IO 总线是**串行共享**的（所有核、所有线程的 IO 都要排队经过 `io_interconnect`），且外设响应**延迟不确定**（可能是几拍，也可能很久）；
- 发起 IO 读的线程**依赖读回来的值**才能继续（数据冒险），在值就绪前它什么也做不了；
- Nyuzi 选择**不阻塞流水线**，而是把该线程挂起、回滚重放，把流水线让给其他就绪线程，等响应到达再唤醒——这跟 cache 缺失的处理哲学完全一致（见 u6-l2 的「缺失—挂起—回填—唤醒」闭环）。

> 待本地验证：以上为源码阅读型推演。若要观察「同一条指令执行两次」，可在 `io_request_queue.sv` 的 `ior_rollback_en` 处临时加一行 `$display`（仿真用，非可综合，勿提交），跑一个会输出字符的程序（如 hello_world），统计每条 IO 访问触发的回滚次数。

#### 4.3.5 小练习与答案

**练习 1：** `io_request_queue` 用的「回滚重放」机制，和 u6-l2 里 L1 cache 缺失的处理有什么共同点？

> **参考答案：** 两者都是「检测到需要等待→把线程挂起（从调度位图摘出）→回滚 PC 重取同一指令→等响应到达→用 wake_bitmap 唤醒线程→重放指令取到结果」。区别只在触发源（cache 缺失 vs IO 访问）和等待对象（L2 响应 vs io_bus 响应）。底层都是为「非阻塞流水线 + 真实数据依赖」设计的统一模式。

**练习 2：** 为什么 `pending_request` 用线程号当数组下标，而不是像 L2 的 miss 队列那样用一个 CAM？

> **参考答案：** 每个核同一时刻每个线程最多只有一个未完成 IO（线程在发起 IO 后就被挂起，不可能再叠一个），所以「每线程一条表项」天然一一对应，用线程号直接索引即可，无需 CAM。L2 miss 队列要处理「多个线程缺失同一行」的合并去重，才需要按地址查找（CAM）。见 [io_request_queue.sv:58-64](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/io_request_queue.sv#L58-L64)。

## 5. 综合实践

**任务：画出 Nyuzi 两条外部总线的完整数据通路图，并标注一次 L2 缺失填充与一次 UART 写分别走哪条路。**

要求：

1. 在图中画出顶层 `nyuzi.sv` 暴露的两个外部接口 `axi_bus` 与 `io_bus`（见 [nyuzi.sv:32-33](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/nyuzi.sv#L32-L33)）。
2. 画出 `core → l1_l2_interface → l2_cache → l2_axi_bus_interface → axi_bus` 这条内存通路，并标出一次 L2 读缺失在这条路上「16 拍突发」发生的位置（[l2_axi_bus_interface.sv:278-287](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_axi_bus_interface.sv#L278-L287)）。
3. 画出 `core/dcache → io_request_queue → io_interconnect → io_bus` 这条 IO 通路（注意它**完全绕开 L2**），标出回滚点（[io_request_queue.sv:155-156](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/io_request_queue.sv#L155-L156)）与唤醒点（[io_request_queue.sv:134-135](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/io_request_queue.sv#L134-L135)）。
4. 用不同颜色/标注区分两条路，并在图旁写一句：**「普通内存访问走缓存→AXI；地址高 16 位为 0xffff 的访问绕过缓存走 IO 总线。」**

完成后再回答一个综合问题：如果一个外设寄存器被错误地映射到了非 `0xffff` 区段（即落进缓存区），会发生什么？提示：它会被当成普通缓存访问，读到的可能是缓存里的旧快照而不是寄存器当前值——这正是为什么 Nyuzi 用地址高位硬性切分两套路径。

## 6. 本讲小结

- Nyuzi 顶层暴露**两条相互独立的外部总线**：`axi_bus`（AXI4，服务缓存内存）和 `io_bus`（极简 5 信号总线，服务外设寄存器），二者从顶层就分开。
- `l2_axi_bus_interface` 把 L2 的缺失填充与脏行写回翻译成 AXI4 突发事务：默认配置下一个 64 字节缓存行 = 16 拍 × 32 位，`m_awlen = m_arlen = 15`；写回优先于填充以防读到旧数据。
- IO 区由地址高位判定：物理地址 `==? 32'hffff????` 即外设区（[dcache_data_stage.sv:192](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/dcache_data_stage.sv#L192)），落在此区的 load/store **绕过整个缓存**，改道进 `io_request_queue`。
- `io_interconnect` 把所有核的 IO 请求**串行化**（两级仲裁：核间 + 核内线程间），单笔服务，响应**广播**给所有核，由各核用 `core==CORE_ID` 认领。
- `io_request_queue` 用「每线程一条表项 + 回滚重放」处理 IO 的不确定延迟：发起时回滚并挂起线程，响应到达时唤醒线程重放指令取值——与 cache 缺失处理同构，且都不阻塞流水线（让位给其他线程）。
- AXI 接口高度参数化（`AXI_DATA_WIDTH`、缓存行大小都来自 config），改总线宽度只需改配置，状态机自动适配突发拍数。

## 7. 下一步学习建议

- **往验证方向走**：本讲的 AXI4 主接口最终在 FPGA 板上接的是 `axi_interconnect` 与 SDRAM/VGA 等真实外设，建议进入 u14（FPGA SoC 与外设）看 `hardware/fpga/common/axi_interconnect.sv` 如何作为 AXI **从设备**接住本讲讲的**主设备**发出的突发。
- **往软件方向走**：本讲的 IO 机制是 u1-l4 里 `printf→UART` 输出的硬件底座，可以回头看 `software/libs/libc` 的 `_write_uart` 如何把一个字符写进 `0xffff0048`，体会「软件写一个字 → 硬件走 io_request_queue → io_bus」的闭环。
- **往并发方向走**：本讲的「挂起 + 唤醒位图」是 Nyuzi 处理一切长延迟操作（cache 缺失、IO、sync）的统一范式，u10-l1（同步内存操作 LL/SC）和 u10-l2（多线程调度与挂起恢复）会把它推广到更复杂的并发场景。
- **源码延伸阅读**：想了解 AXI 主从两侧如何在 FPGA SoC 里互联，直接读 [hardware/fpga/common/axi_interconnect.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/fpga/common/axi_interconnect.sv)；想了解 `l2_axi_bus_interface` 依赖的「冲突缺失 CAM」，读 [hardware/core/l2_cache_pending_miss_cam.sv](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/l2_cache_pending_miss_cam.sv)。
