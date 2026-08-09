# util 工具 IP：FIFO、CDC、pack/unpack

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 `util_axis_fifo` 这条标准 AXI-Stream FIFO 的可配置项（深度、同步/异步、TLAST/TKEEP、almost 阈值），并理解它如何用格雷码地址指针 + `ad_mem` 实现安全的跨时钟域缓存。
- 区分 `util_cdc` 下的三个同步原语 `sync_bits` / `sync_gray` / `sync_event` 各自能安全搬运什么信号。
- 解释 `util_cpack2` 如何把多路窄通道「打包」成一路宽 AXIS，`util_upack2` 如何做反向「解包」，以及为何要先把通道数向上取整为 2 的幂。
- 说明 `util_wfifo` 与 `util_rfifo` 这对非对称（位宽转换）跨时钟域 FIFO 的方向差异，以及为什么采集链用 wfifo 关心溢出、回放链用 rfifo 关心欠溢。
- 读懂 `axi_dmac` 的 Makefile，指出它在三家厂商下分别「扁平嵌入」了哪些 util 源文件。

本讲是 [u5-l1（axi_dmac 深入）](u5-l1-axi-dmac.md) 的直接后续：axi_dmac 的 store-and-forward 缓冲、ID 跨时钟域同步，本质上就是由本讲的 util 工具 IP 搭起来的。本讲把这些「胶水零件」单独拆出来讲透。

## 2. 前置知识

阅读本讲前，建议你已经建立以下概念（在 u5-l1、u5-l2 中讲过）：

- **AXI-Stream（AXIS）握手**：一路数据用 `valid` / `ready` / `data` 三组信号传输，`valid && ready` 时一拍成交。`tlast` 标记一帧最后一个 beat，`tkeep` 标记该 beat 中哪些字节有效。
- **跨时钟域（CDC, Clock Domain Crossing）**：当数据从一个时钟域进入另一个无固定相位关系的时钟域时，直接连线会采样到亚稳态。常用做法是「两级触发器同步 + 格雷码指针」或「请求/应答握手」。
- **FIFO 指针用格雷码**：二进制地址每次自增可能多位同时翻转，跨域采样会读到错误中间值；格雷码保证相邻地址只有 1 位变化，再用 2 级 FF 同步就安全了。
- **ad_mem 双口 RAM**：library/common 下的厂商无关存储原语（u4-l4 已讲），有一个写口（`clka/wea/addra/dina`）和一个读口（`clkb/reb/addrb/doutb`），两个口可以接不同时钟。
- **数据转换器数据通路**：采集链是 `ADC → wfifo(跨时钟域) → cpack2(打包) → dmac → DDR`，回放链方向相反用 `upack2` 与 `rfifo`（见 u5-l2）。

如果你对 AXI 总线本身还不熟，可先回顾 [u4-l5（寄存器映射与 up_axi）](u4-l5-register-map-up-axi.md)。

一句话直觉：**util IP 就是数据通路上的「转接头 + 弹簧 + 变速箱」**——转接头负责接口形态转换（窄↔宽），弹簧负责吸收速率抖动（FIFO），变速箱负责对齐转速（CDC）。

## 3. 本讲源码地图

本讲涉及的关键文件及其作用：

| 文件 | 作用 |
|------|------|
| [library/util_axis_fifo/util_axis_fifo.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_axis_fifo/util_axis_fifo.v) | 可参数化的 AXI-Stream FIFO 顶层，含同步/异步、深度为 0 的退化分支 |
| [library/util_axis_fifo/util_axis_fifo_address_generator.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_axis_fifo/util_axis_fifo_address_generator.v) | FIFO 的读写地址发生器，用格雷码指针实现满/空与 almost 标志 |
| [library/util_cdc/sync_bits.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_cdc/sync_bits.v) | 单比特/格雷码位的 2 级 FF 同步器 |
| [library/util_cdc/sync_gray.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_cdc/sync_gray.v) | 格雷码计数器跨域同步（±1 变化） |
| [library/util_cdc/sync_event.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_cdc/sync_event.v) | 脉冲事件跨域传递，基于 toggle 握手 |
| [library/util_pack/util_cpack2/util_cpack2.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_pack/util_cpack2/util_cpack2.v) | 通道打包顶层：N 路窄通道 → 1 路宽 AXIS |
| [library/util_pack/util_cpack2/util_cpack2_impl.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_pack/util_cpack2/util_cpack2_impl.v) | 打包核心实现，含 interleave 与 pack_shell 例化 |
| [library/util_pack/util_pack_common/pack_shell.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_pack/util_pack_common/pack_shell.v) | 通用 pack/unpack 路由网络（`PACK` 参数控制方向） |
| [library/util_pack/util_upack2/util_upack2.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_pack/util_upack2/util_upack2.v) | 通道解包顶层：1 路宽 AXIS → N 路窄通道（cpack2 的反向） |
| [library/util_wfifo/util_wfifo.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_wfifo/util_wfifo.v) | 写侧非对称跨时钟域 FIFO（关注溢出 ovf），采集链用 |
| [library/util_rfifo/util_rfifo.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_rfifo/util_rfifo.v) | 读侧非对称跨时钟域 FIFO（关注欠溢 unf），回放链用 |
| [library/axi_dmac/Makefile](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/Makefile) | axi_dmac 的依赖声明，展示了它对 util 模块的引用方式 |

---

## 4. 核心概念与源码讲解

### 4.1 AXIS FIFO 与跨时钟域（CDC）原语

#### 4.1.1 概念说明

`util_axis_fifo` 是 ADI 全仓复用最广的 AXI-Stream FIFO。它解决一个极其常见的问题：**两个 AXIS 模块的握手时序对不齐，或者干脆跑在不同的时钟上**。典型场景包括：

- 数据转换器的采样时钟（如 ad9361 的 `l_clk`）与 FPGA 内部数据通路时钟（`divclk`）不同源。
- DMA 的存储接口位宽（如 64 bit）与上下游位宽不一致，需要缓冲与位宽适配。
- 突发写、平滑读：上游一次来一簇数据，下游匀速消费。

它之所以「一个模块走天下」，关键在于可参数化：数据位宽、FIFO 地址深度、同步/异步时钟、是否带 TLAST/TKEEP 边带、几乎满/几乎空阈值，全部用参数开关控制。

跨时钟域的部分由独立的 `util_cdc` 库承担，提供三种同步原语，各管一类信号：

- `sync_bits`：2 级 FF 同步器，只能同步「任意一拍最多 1 位变化」的多位信号（典型就是格雷码指针）。
- `sync_gray`：专门同步格雷码计数器，内部做二进制↔格雷码转换。
- `sync_event`：把一个时钟域里的单周期脉冲「事件」搬到另一个时钟域，用 toggle 握手保证不丢脉冲。

#### 4.1.2 核心流程

`util_axis_fifo` 的核心流程可以概括为：

```text
写侧(s_axis_*) ──► [写地址指针 waddr] ──► 写入存储体(RAM)
                                            │
              格雷码 + 2级FF 同步 ──────────┤  (仅 ASYNC_CLK)
                                            │
读侧(m_axis_*) ◄── [读地址指针 raddr] ◄── 读出存储体(RAM)
```

关键设计点：

1. **满/空判定靠地址指针比较**：写指针追上读指针 → 满；读指针追上写指针 → 空。异步时钟下，指针要先转成格雷码、过 2 级 FF 同步到对岸再比较。若地址位宽为 \(n\)，FIFO 容量为 \(2^n\) 个存储字。
2. **首字直通（first-word-fall-through）**：只要 FIFO 里有数据，`m_axis_valid` 在没有 backpressure 时立刻拉高，不必等一次读操作完成才出现有效数据。
3. **TLAST/TKEEP 与 data 拼接存储**：边带信号和数据被拼成一个更宽的 `MEM_WORD` 一起写进 RAM，读出再拆开，保证边带和数据严格对齐、不串拍。
4. **`ADDRESS_WIDTH == 0` 退化**：深度为 0 时不是真正的 FIFO，而是一级流水寄存器，专门用于「只需要打一拍 + 跨域」的轻量场景。

#### 4.1.3 源码精读

先看 `util_axis_fifo` 的参数与端口。注意它有独立的写时钟 `s_axis_aclk` 和读时钟 `m_axis_aclk`，这是支持异步 FIFO 的前提：

[library/util_axis_fifo/util_axis_fifo.v:37-69](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_axis_fifo/util_axis_fifo.v#L37-L69) —— 声明 `DATA_WIDTH`、`ADDRESS_WIDTH`、`ASYNC_CLK`、`TLAST_EN`、`TKEEP_EN` 等参数，以及写侧（`s_axis_*`）与读侧（`m_axis_*`）两组端口，外加 `level`/`full`/`empty`/`almost_*` 状态输出。

边带拼接到一个宽存储字的逻辑在这里——`MEM_WORD` 的宽度随 `TLAST_EN`/`TKEEP_EN` 开关动态计算：

[library/util_axis_fifo/util_axis_fifo.v:71-74](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_axis_fifo/util_axis_fifo.v#L71-L74) —— `MEM_WORD` 三元表达式把 `tkeep`、`tlast` 按需拼到 `data` 前面，形成实际写入 RAM 的位宽。

接着是 `ADDRESS_WIDTH == 0` 的退化分支。当异步时，它用两个 `sync_bits` 分别把写地址指针同步到读时钟域、把读地址指针同步到写时钟域：

[library/util_axis_fifo/util_axis_fifo.v:90-106](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_axis_fifo/util_axis_fifo.v#L90-L106) —— 深度为 0 的异步分支里，例化 `sync_bits` 同步 1 位的读写指针；源码注释 `it's not a real FIFO, just a 1 stage pipeline` 说明这是「最迷你的跨域 FIFO」，只存一个数据。

真正有深度的 FIFO（`ADDRESS_WIDTH != 0`）则把指针管理交给专门的子模块，并把存储体二选一——异步用 `ad_mem`（强制推断为 BRAM，保证跨域读时序），同步用行为级 RAM（让综合器自行决定分布式或块 RAM）：

[library/util_axis_fifo/util_axis_fifo.v:260-281](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_axis_fifo/util_axis_fifo.v#L260-L281) —— 例化 `util_axis_fifo_address_generator`，由它产生 `s_axis_waddr`/`m_axis_raddr` 与 `full`/`empty`/`level`/`almost_*` 全部标志。

[library/util_axis_fifo/util_axis_fifo.v:306-327](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_axis_fifo/util_axis_fifo.v#L306-L327) —— `ASYNC_CLK==1` 时例化 `ad_mem` 作为双口 RAM，写口接 `s_axis_aclk`、读口接 `m_axis_aclk`，注释明确说明异步模式下无论请求多大都用 BRAM 以正确处理时钟跨越。

再看 CDC 原语本身。`sync_bits` 的注释直接点明了它的能力边界：

[library/util_cdc/sync_bits.v:36-58](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_cdc/sync_bits.v#L36-L58) —— 注释说明用「2 个 FF 串联」标准做法，并警告：虽可同步多位，但仅适用于「任一拍最多 1 位变化」的信号（如格雷计数器）；参数 `ASYNC_CLK=0` 时旁路，输出直接等于输入。

实现就是两级寄存器：

[library/util_cdc/sync_bits.v:60-78](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_cdc/sync_bits.v#L60-L78) —— `cdc_sync_stage1`/`cdc_sync_stage2` 两级打拍，`ASYNC_CLK==1` 时输出第二级，否则直接 `assign out_bits = in_bits`。

`sync_gray` 在此基础上加了二进制↔格雷码转换，专门搬计数器：

[library/util_cdc/sync_gray.v:36-58](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_cdc/sync_gray.v#L36-L58) —— 注释强调被同步的计数器在源域一拍内最多变化 ±1，这正是异步 FIFO 指针的约束。

`sync_event` 用于搬「脉冲」。它不能直接用 2 级 FF（脉冲可能被采样漏掉），所以采用 toggle 握手：源域每来一个事件就翻转一次电平，对岸检测到翻转边沿就输出一个脉冲。它内部恰恰复用了两个 `sync_bits`：

[library/util_cdc/sync_event.v:57-70](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_cdc/sync_event.v#L57-L70) —— 两个 `sync_bits` 分别负责「请求」与「应答」方向的同步，构成完整的握手闭环。

#### 4.1.4 代码实践

**实践目标**：理解异步 FIFO 中「写指针如何被读时钟域看见」。

**操作步骤（源码阅读型）**：

1. 打开 `library/util_axis_fifo/util_axis_fifo_address_generator.v`，找到它例化 `sync_gray` 或 `sync_bits` 的位置（搜索 `sync_gray` / `sync_bits`）。
2. 追踪：写侧用二进制计数器自增 → 转格雷码 → `sync_gray` 跨到读时钟域 → 读侧再转回二进制 → 与本地读指针比较得到 `level`/`empty`。

**需要观察的现象**：

- 写指针到读侧「看见」会延迟 2~3 拍，因此异步 FIFO 用「保守的满判定」——把同步延迟期间可能又写入的数据量算进去，宁可误报满（少写）也不能写溢出。
- `almost_full` / `almost_empty` 阈值参数（`ALMOST_FULL_THRESHOLD` / `ALMOST_EMPTY_THRESHOLD`）正是给这种保守余量调度的旋钮。

**预期结果**：你能用一段话讲清「为什么异步 FIFO 不会因为同步延迟而丢数据」。如果你本地装了仿真器，可参考 u5-l1 提到的 `library/axi_dmac/tb/` 风格写一个最小激励：`ASYNC_CLK=1` 时给两个不同频时钟，观察 `m_axis_valid` 相对 `s_axis_valid` 的延迟。若无法运行，标注「待本地验证」即可。

#### 4.1.5 小练习与答案

**练习 1**：`util_axis_fifo` 的 `ADDRESS_WIDTH` 设为 0 且 `ASYNC_CLK=1` 时，它还算 FIFO 吗？为什么还要保留这个分支？

> **答案**：它不是 FIFO，而是一级带跨域同步的流水寄存器（源码注释 `it's not a real FIFO, just a 1 stage pipeline`）。保留它是因为很多场景只需要「打一拍 + 过个时钟域」，用深 FIFO 是浪费资源；退化分支用极少的逻辑（一个寄存器 + 两个 `sync_bits`）就满足了需求。

**练习 2**：`sync_bits` 注释说「只能同步每拍最多 1 位变化的多位信号」。如果你硬把一个普通 4 位二进制计数器接到 `sync_bits`，会发生什么？

> **答案**：二进制计数器自增时可能多位同时翻转（如 `0111→1000` 全部 4 位都变），2 级 FF 同步后各位的延迟可能不一致，读侧会采样到中间错误值。正确做法是先转格雷码再用 `sync_gray`，或改用 `sync_event` 的握手方式。这正是异步 FIFO 指针用格雷码的根本原因。

---

### 4.2 cpack2 / upack2：通道的打包与解包

#### 4.2.1 概念说明

数据转换器通常一次产出多路窄通道——例如 ad9361 有 I0/Q0/I1/Q1 共 4 路、每路 16 bit。但 DMA 搬运时希望走宽总线（如 64 bit）以提高吞吐。`util_cpack2`（channel pack）就负责把 **N 路窄通道按拍拼成 1 路宽 AXIS 字**；`util_upack2`（channel unpack）做精确的反向操作。

这对模块的关键能力不仅仅是「位拼接」，还在于它能处理 **通道动态使能**：当软件只开了 2 路通道时，cpack2 不会在输出字里留下 2 个空洞，而是把有效的 2 路紧凑排列、并给出一个 `valid`/`keep` 掩码告诉下游「这一拍里哪些位置是真数据」。这背后是一个路由网络（routing network），而不是简单的位拼接。

#### 4.2.2 核心流程

以采集链中 4 路 16 bit 打包成 64 bit 为例：

```text
fifo_wr_data_0 [15:0]  ─┐
fifo_wr_data_1 [15:0]  ─┤  ad_perfect_shuffle(交错)
fifo_wr_data_2 [15:0]  ─┤  ──► pack_shell(路由网络, 按 enable 紧凑排列)
fifo_wr_data_3 [15:0]  ─┘         │
   + enable[3:0]                  ▼
                          packed_fifo_wr_data [63:0]
                          packed_fifo_wr_en / packed_sync
```

关键设计点：

1. **通道数向上取整为 2 的幂**：`NUM_OF_CHANNELS=4` 正好是 2 的幂没问题，但 `=6` 时内部会扩成 8 路、多余通道补零，这样路由网络才能用规则的 2:1 / 4:1 MUX 搭建。
2. **`INTERFACE_TYPE` 选择输出形态**：cpack2 既能输出标准 AXIS（`m_axis_*`），也能输出 ADI 自定义的 `fifo_wr` 接口（`packed_fifo_wr_*`），后者用于直连 axi_dmac 的 fifo_wr 通道。
3. **`pack_shell` 是 pack 与 upack 的共享核心**：同一个路由网络，`PACK=1` 时数据从多通道汇入宽总线（cpack），`PACK=0` 时反向（upack）。
4. **位重排不耗资源**：`ad_perfect_shuffle` 只是改连线顺序（`assign`），综合后不占任何 LUT。

#### 4.2.3 源码精读

先看 cpack2 如何把通道数取整为 2 的幂。注意端口宽度本身也用了 `2**$clog2(NUM_OF_CHANNELS)`，保证输出总线宽度对齐：

[library/util_pack/util_cpack2/util_cpack2.v:194-203](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_pack/util_cpack2/util_cpack2.v#L194-L203) —— `REAL_NUM_OF_CHANNELS` 用一串三元判断把任意通道数向上取整到 1/2/4/8/16/32/64；注释说明多余通道在内部补零。

64 个独立 `enable_*` / `fifo_wr_data_*` 端口被拼接成总线向量，再截取有效通道部分，交给核心实现：

[library/util_pack/util_cpack2/util_cpack2.v:224-288](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_pack/util_cpack2/util_cpack2.v#L224-L288) —— 把 `fifo_wr_data_0..63` 逐路拼成宽向量 `fifo_wr_data_s`，再 `assign fifo_wr_data = fifo_wr_data_s[0+:REAL_NUM_OF_CHANNELS*CHANNEL_DATA_WIDTH]` 截出有效通道。

在 `util_cpack2_impl` 里，`INTERFACE_TYPE` 决定数据走哪条出口。默认 `INTERFACE_TYPE=1` 时走 ADI 的 `packed_fifo_wr` 接口（这正是 fmcomms2 里直连 axi_dmac 的方式）：

[library/util_pack/util_cpack2/util_cpack2_impl.v:88-104](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_pack/util_cpack2/util_cpack2_impl.v#L88-L104) —— `INTERFACE_TYPE==1` 把输出送到 `packed_fifo_wr_*` 并屏蔽 `m_axis_*`；否则送标准 AXIS。注释也明确：cpack 本身不做 backpressure，溢出只会发生在下游。

进入 `pack_shell` 前，数据先经 `ad_perfect_shuffle` 做交错重排，让「通道×样点」的排列适配路由网络的输入顺序——这一步纯连线、零资源：

[library/util_pack/util_cpack2/util_cpack2_impl.v:122-133](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_pack/util_cpack2/util_cpack2_impl.v#L122-L133) —— 注释说明此模块只是重排数据向量里的比特顺序，不消耗任何 FPGA 资源。

真正的「紧凑排列」逻辑在 `pack_shell` 中。它维护一个 `rotate` 偏移和 `prefix_count`（每个通道之前有几个被禁用的通道），用一组 MUX 构成的路由网络把使能通道的数据搬到输出向量的前缀位置：

[library/util_pack/util_pack_common/pack_shell.v:152-165](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_pack/util_pack_common/pack_shell.v#L152-L165) —— 注释用具体例子解释 `rotate`：4 通道开 2 路时 `rotate` 在 0/2 间振荡；开 3 路时循环 0,3,2,1。

当 `PARALLEL_OR_SERIAL_N=1` 时，前缀和用并行树状加法器计算（最多 log2 级），用面积换时序；否则用串行累加。`PACK` 参数控制网络方向：

[library/util_pack/util_pack_common/pack_shell.v:451-479](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_pack/util_pack_common/pack_shell.v#L451-L479) —— 用 for 循环生成两级 `pack_network`（4:1 与 2:1 MUX 混搭），注释解释为何末级用 2:1、其余用 4:1 能兼顾级数与资源。

`util_upack2` 是 cpack2 的镜像。它把宽输入 `s_axis_data` 拆回 64 个窄输出端口，多余通道补零：

[library/util_pack/util_upack2/util_upack2.v:235-300](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_pack/util_upack2/util_upack2.v#L235-L300) —— `fifo_rd_data_s` 高位补零后，逐路 `assign fifo_rd_data_i = fifo_rd_data_s[...]` 切出每路输出；其内部例化的 `util_upack2_impl` 与 cpack 共享同一套 `pack_shell`（`PACK=0`）。

#### 4.2.4 代码实践（本讲指定实践）

**实践目标**：说清 cpack2/upack2 的「打包/解包」机制，并查清 axi_dmac 依赖了哪些 util 模块。

**操作步骤**：

1. 打开 `library/util_pack/util_cpack2/util_cpack2.v`，对照 L194-L203 的 `REAL_NUM_OF_CHANNELS` 与 L224-L288 的端口拼接逻辑，回答：如果 `NUM_OF_CHANNELS=3`、`SAMPLE_DATA_WIDTH=16`、`SAMPLES_PER_CHANNEL=1`，那么 `REAL_NUM_OF_CHANNELS`、`CHANNEL_DATA_WIDTH` 和输出 `m_axis_data` 的宽度分别是多少？
   - 参考答案：`REAL_NUM_OF_CHANNELS=4`，`CHANNEL_DATA_WIDTH=16`，`m_axis_data` 宽度 \(=2^{\lceil\log_2 3\rceil}\times 16 = 4\times 16 = 64\) bit。
2. 打开 `library/util_pack/util_upack2/util_upack2.v`，对比它与 cpack2 的端口方向：cpack2 的 `fifo_wr_data_*` 是 **input**、输出是 `m_axis_data`/`packed_fifo_wr_data`；upack2 的 `fifo_rd_data_*` 是 **output**、输入是 `s_axis_data`。据此用一段话描述数据流向的反转。
3. 打开 [library/axi_dmac/Makefile](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/Makefile)，找出 axi_dmac 引用的 util 模块：
   - Xilinx 侧（跨库引用）：`XILINX_LIB_DEPS += util_axis_fifo`、`util_cdc`（[L53-L54](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/Makefile#L53-L54)）。
   - Intel 侧（扁平嵌入源码）：`util_axis_fifo.v`、`util_axis_fifo_address_generator.v`、`sync_bits.v`、`sync_event.v`、`sync_gray.v`（[L58-L64](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/Makefile#L58-L64)）。
   - Lattice 侧（扁平嵌入源码）：在 Intel 基础上还多了 `../common/ad_mem.v`（[L66-L72](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/Makefile#L66-L72)）。

**需要观察的现象 / 预期结果**：

- 你应当能用一句话总结：「cpack2 把多路窄通道按 `enable` 紧凑拼成一路宽字，多余位置由 `keep`/`valid` 掩码标记；upack2 反向把宽字拆回多路。」
- 你应当发现：**axi_dmac 自身只直接依赖 `util_axis_fifo` 与 `util_cdc`**（这是它 store-and-forward 缓冲与 ID 跨域同步的零件，印证 u5-l1），而 `util_cpack2`/`util_upack2`/`util_wfifo`/`util_rfifo` 是在 **工程块设计脚本**（如 fmcomms2_bd.tcl）里被引用的，不在 dmac 的库依赖里。这是一个重要区分：dmac 提供「通道」，pack/wfifo/rfifo 负责「把数据转换器的多路数据整理进这条通道」。

> 说明：上述第 3 步的依赖清单直接来自 Makefile 源文件，可在本地用任意编辑器打开核对，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `util_cpack2` 要把 `NUM_OF_CHANNELS` 向上取整为 2 的幂，而不是直接处理任意通道数？

> **答案**：因为内部的路由网络（`pack_shell` → `pack_network`）是用 2:1 / 4:1 MUX 搭的规则结构，端口数必须是 2 的幂才能对称展开。把 3 路扩成 4 路、补一路恒零，能让同一个网络结构无差别复用，综合后多出的那路 MUX 输入被优化掉，几乎不增加资源。

**练习 2**：`util_cpack2_impl` 注释说「cpack 核心不做 backpressure，溢出只会在下游发生」。结合采集链，这句话的含义是什么？

> **答案**：cpack2 没有内部的满/空流控，它只要输入 `fifo_wr_en` 有效就处理；如果下游（axi_dmac 的 fifo_wr 端口）来不及收，溢出会通过 `packed_fifo_wr_overflow` 回报给上游（在 fmcomms2 里接到 wfifo 的 `dout_ovf`），由上游记录溢出计数。也就是说，背压责任不在 cpack，而在链路两端的 FIFO/DMA。

---

### 4.3 util_wfifo / util_rfifo：非对称跨时钟域 FIFO

#### 4.3.1 概念说明

`util_wfifo` 与 `util_rfifo` 是一对**带位宽转换的跨时钟域 FIFO**，二者结构几乎对称、用途相反。它们解决数据转换器数据通路里的两个具体问题：

- **跨时钟域**：ad9361 的数据在 `l_clk`（器件时钟）域，而 pack/dma 通路在 `divclk`（FPGA 分频时钟）域，必须安全跨越。
- **位宽适配**：器件侧位宽（如 16 bit）常常窄于 DMA 侧位宽（如 64 bit），需要在跨域的同时把多个窄字拼成宽字（或反向）。

为什么需要两个不同的模块？因为数据流的「危险方向」不同：

| 模块 | 用在 | 写口方向 | 关心的异常 | 报告信号 |
|------|------|----------|-----------|----------|
| `util_wfifo` | 采集链（ADC→DDR） | 器件时钟域**写入** | 写入快于读出 → **溢出** | `din_ovf` |
| `util_rfifo` | 回放链（DDR→DAC） | FIFO 读出送器件 | 读出快于写入 → **欠溢** | `dout_unf` |

记忆口诀（承接 u5-l2）：**采集用 wfifo 关注溢出，回放用 rfifo 关注欠溢**。

#### 4.3.2 核心流程

两个模块的核心都是「双口 RAM + 跨域请求握手」。以 wfifo（窄写宽读，`DOUT > DIN`）为例：

```text
din_clk 域:  DIN_DATA_WIDTH 窄字 ──► 每 M_MEM_RATIO 个字拼成 1 个宽字 ──► 写入 ad_mem
                                    写满一个块后翻转 req_t (toggle) ──────┐
                                                                         │ 跨域(3 级延迟)
dout_clk 域: 读到 req_t 翻转 ──► 按块读出宽字 ──► dout_data_*            ◄┘
```

其中位宽比 `M_MEM_RATIO = DOUT_DATA_WIDTH / DIN_DATA_WIDTH`，决定了「攒几个窄字才写一次宽字」。当比值为 1 时退化为纯跨域 FIFO（fmcomms2 里 DIN=DOUT=16 就是这种用法，只用它的跨域功能）。

关键设计点：

1. **请求用 toggle 握手跨域**：写侧攒满一块数据后翻转一次电平信号（而非发脉冲），对岸用「连续 3 拍异或」检测翻转，避免脉冲被跨域采样漏掉。这和 `sync_event` 的思路一致。
2. **存储体用 `ad_mem`**：等宽简单双口 RAM（同步写、寄存同步读），与 u4-l4 讲过的 `ad_mem` 同源；wfifo/rfifo 的 Makefile 在 Lattice 侧都会带上 `../common/ad_mem.v`。
3. **两侧独立地址发生器**：写侧管写地址与块计数，读侧管读地址与块计数，靠 toggle 对齐块的边界。

#### 4.3.3 源码精读

`util_wfifo` 的参数与端口定义了它的非对称性——注意写口（`din_*`）和读口（`dout_*`）各自有独立时钟、独立复位（且复位极性相反：写侧 `din_rst` 高有效、读侧 `dout_rstn` 低有效）：

[library/util_wfifo/util_wfifo.v:38-105](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_wfifo/util_wfifo.v#L38-L105) —— 参数含 `DIN_DATA_WIDTH`/`DOUT_DATA_WIDTH`/`DIN_ADDRESS_WIDTH`；写口报告 `din_ovf`、读口接收 `dout_ovf` 作为背压回传。

源码注释明确点出了带宽约束——读带宽必须大于写带宽：

[library/util_wfifo/util_wfifo.v:159-160](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_wfifo/util_wfifo.v#L159-L160) —— 注释 `read-bw > write-bw (equal will NOT work)`，说明此类 FIFO 假设读侧至少和写侧一样快，否则必然溢出。

位宽拼接逻辑用 generate 按 `M_MEM_RATIO` 分支：比值大于 1 时，每个窄字到来都累积进宽字的高位，攒满比值的个数才真正写一次 RAM：

[library/util_wfifo/util_wfifo.v:162-181](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_wfifo/util_wfifo.v#L162-L181) —— `M_MEM_RATIO==1` 时直接搬运；否则把新窄字拼到旧宽字高位，逐步攒出一个完整宽字。

存储体例化——经典的 `ad_mem` 双口 RAM，写口在 `din_clk`、读口在 `dout_clk`，正是跨时钟域的物理承载：

[library/util_wfifo/util_wfifo.v:330-341](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_wfifo/util_wfifo.v#L330-L341) —— `ad_mem` 例化，`clka=din_clk`/`clkb=dout_clk`，完成位宽转换后的数据就存在这里。

`util_rfifo` 是镜像版本——方向反过来：读口在 `dout_clk` 域面向器件，关心的是「读太快、写还没来」的欠溢：

[library/util_rfifo/util_rfifo.v:189-190](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_rfifo/util_rfifo.v#L189-L190) —— 注释 `read-bw > write-bw`，`dout_width >= din_width only`，同样要求读侧带宽不小于写侧。

[library/util_rfifo/util_rfifo.v:392-403](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_rfifo/util_rfifo.v#L392-L403) —— rfifo 同样以 `ad_mem` 为存储体，区别在于异常报告是 `dout_unf`（欠溢）而非 `din_ovf`。

真实用法见 fmcomms2 的块设计脚本，采集链把 wfifo 的写口接 ad9361 的 `l_clk` 域、读口接 `divclk` 域：

[projects/fmcomms2/common/fmcomms2_bd.tcl:94-115](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl#L94-L115) —— 例化 `util_wfifo util_ad9361_adc_fifo`，`din_clk` 接 `axi_ad9361/l_clk`、`dout_clk` 接 `util_ad9361_divclk/clk_out`，把 4 路 ADC 数据从器件时钟跨到 divclk，并把 `din_ovf` 接回 `axi_ad9361/adc_dovf` 报告溢出。

回放链则用 rfifo 反向跨越，把宽 FIFO 数据解回送进 ad9361 的 DAC 通道：

[projects/fmcomms2/common/fmcomms2_bd.tcl:158-178](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl#L158-L178) —— 例化 `util_rfifo axi_ad9361_dac_fifo`，方向与 wfifo 相反：`din_clk` 在 divclk 域、`dout_clk` 在 `l_clk` 域，`dout_unf` 接 `axi_ad9361/dac_dunf` 报告欠溢。

#### 4.3.4 代码实践

**实践目标**：搞清 wfifo 与 rfifo 的方向差异，避免在工程里用反。

**操作步骤**：

1. 对照上面两段 bd.tcl，画一张小表，分别记录 wfifo 与 rfifo 的 `din_clk` 接哪个时钟、`dout_clk` 接哪个时钟、异常信号（`*_ovf`/`*_unf`）接到 ad9361 的哪个寄存器位。
2. 回答：为什么采集链（ADC→DDR）天然容易溢出，而回放链（DDR→DAC）天然容易欠溢？
3. 观察 fmcomms2 这里 `DIN_DATA_WIDTH = DOUT_DATA_WIDTH = 16`（比值 1），说明此时 wfifo/rfifo 退化为「纯跨时钟域 FIFO」，位宽转换功能被关闭。

**预期结果**：

| | wfifo（采集） | rfifo（回放） |
|--|---------------|---------------|
| din_clk | `l_clk`（器件域） | `divclk`（FPGA 域） |
| dout_clk | `divclk`（FPGA 域） | `l_clk`（器件域） |
| 异常 | `din_ovf → adc_dovf`（溢出） | `dout_unf → dac_dunf`（欠溢） |

**解释**：采集链里，ADC 按自己的采样率持续产数据，如果 FPGA 侧（DMA/DDR）一时没跟上，FIFO 就会被写爆 → 溢出；回放链里，DAC 按自己的速率持续要数据，如果 DDR 侧一时没供上，FIFO 就被读空 → 欠溢。两种异常方向相反，所以需要两套报告机制、两个不同模块。

> 说明：本实践为源码阅读型，结论可直接从 bd.tcl 与模块端口对照得出，无需运行硬件。

#### 4.3.5 小练习与答案

**练习 1**：如果误把 `util_rfifo` 用在采集链（ADC 侧），会出现什么问题？

> **答案**：rfifo 的异常报告是 `dout_unf`（欠溢），方向针对「读口面向慢速消费者」的回放场景；采集链里溢出风险在写口（器件写得快），用 rfifo 就接不到正确的溢出信号（rfifo 没有面向器件写口的 `din_ovf`），软件将看不到采集中途的溢出，数据会无声丢失。

**练习 2**：wfifo 和 rfifo 都要求「read-bw > write-bw（相等也不行）」。这条约束在采集链里意味着什么？

> **答案**：意味着 DMA 把数据搬走的长期平均速率，必须严格大于 ADC 产生数据的速率。否则 FIFO 迟早被写满溢出——FIFO 只能吸收短时的速率波动，不能补偿长期的带宽不足。这也是为什么采集链的 DMA 通常配较大的 FIFO 深度与合适的 burst，并启用 `SYNC_TRANSFER_START` 来对齐起拍（见 u5-l1、u5-l2）。

---

## 5. 综合实践

把本讲三个模块串起来，追踪一条完整的 ADC 采集数据通路，并标注每段用了哪个 util 工具 IP。

**任务**：阅读 [projects/fmcomms2/common/fmcomms2_bd.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl) 的 ADC 段（约 L92-L154），画出从 `axi_ad9361` 的 ADC 输出到 `axi_ad9361_adc_dma` 的 fifo_wr 输入之间的完整框图，并在每一级标注：

1. 这一级是哪个 IP（`util_wfifo` / `util_cpack2`）。
2. 这一级的输入/输出时钟域（`l_clk` 还是 `divclk`）。
3. 这一级是否做了位宽转换或通道打包。
4. 异常（溢出/欠溢）信号接到哪里。

**参考答案（自检用）**：

```text
axi_ad9361 (l_clk, 4×16bit ADC)
   │  adc_enable/valid/data_i0..q1
   ▼
util_wfifo util_ad9361_adc_fifo        ← 跨时钟域: l_clk → divclk；位宽比 1（纯跨域）
   │  din_ovf → axi_ad9361/adc_dovf    ← 报告溢出
   ▼  dout_data_0..3 (divclk)
util_cpack2 util_ad9361_adc_pack        ← 打包: 4×16 → 64bit；INTERFACE_TYPE=1
   │  packed_fifo_wr_data[63:0] / packed_sync
   │  fifo_wr_overflow → wfifo/dout_ovf ← 背压回传
   ▼
axi_dmac axi_ad9361_adc_dma (fifo_wr 口) ← 接收打包后的宽字, 送往 PS DDR
```

进阶追问：DAC 回放链（`util_upack2` + `util_rfifo`）是对称的反向结构，请自行画出并对照 ADC 链验证「方向全部反过来、ovf 换成 unf」。

## 6. 本讲小结

- `util_axis_fifo` 是可参数化的 AXI-Stream FIFO：靠格雷码地址指针 + `ad_mem`（异步）或行为级 RAM（同步）实现，TLAST/TKEEP 边带与数据拼成宽字一起存储，`ADDRESS_WIDTH=0` 时退化为一级带跨域的流水寄存器。
- `util_cdc` 提供三种同步原语：`sync_bits`（2 级 FF，搬单比特/格雷位）、`sync_gray`（搬 ±1 变化的格雷计数器）、`sync_event`（toggle 握手搬脉冲事件）；选错原语会读到错误的中间值或丢脉冲。
- `util_cpack2` 把 N 路窄通道按 `enable` 紧凑打包成一路宽 AXIS（通道数先取整为 2 的幂，核心是 `pack_shell` 路由网络 + `ad_perfect_shuffle` 零资源位重排），`util_upack2` 是它的反向；二者靠 `INTERFACE_TYPE` 选择走标准 AXIS 还是 ADI 的 `fifo_wr` 接口。
- `util_wfifo` / `util_rfifo` 是带位宽转换的跨时钟域 FIFO，均以 `ad_mem` 为存储体、用 toggle 握手跨域；采集链用 wfifo 报告溢出（`din_ovf`），回放链用 rfifo 报告欠溢（`dout_unf`），方向不可混用。
- 在工程依赖层面：`axi_dmac` 库自身只依赖 `util_axis_fifo` 与 `util_cdc`；而 `util_cpack2`/`util_upack2`/`util_wfifo`/`util_rfifo` 是在工程块设计脚本里被引用、负责把数据转换器的多路数据整理进 DMA 通道的「胶水」。

## 7. 下一步学习建议

- **横向对照真实工程**：选一个 JESD204 类工程（如 adrv9009 或 ad9081）的 `*_bd.tcl`，观察它在数据通路里用了哪些 util IP——你会看到 cpack2/upack2 与 JESD204 transport 层（见 u6-l1）的级联方式。
- **深入 pack_shell 的路由网络**：若对「非 2 的幂通道如何紧凑排列」感兴趣，可读 `library/util_pack/util_pack_common/` 下的 `pack_network.v`、`pack_interconnect.v`、`pack_ctrl.v`，理解 `prefix_count` + `rotate` 如何驱动 MUX 网络。
- **回到 DMA 视角**：带着本讲对 FIFO/CDC 的理解重读 [u5-l1](u5-l1-axi-dmac.md) 的 `request_arb` 与 ID 跨时钟域部分，你会发现 axi_dmac 的 store-and-forward 缓冲正是 `util_axis_fifo` 的具体部署。
- **仿真验证**：`library/util_pack/tb/` 下有 pack/unpack 的测试平台，可作为编写自定义 CDC/FIFO 仿真激励的模板（与 u8-l1 仿真主题呼应）。
