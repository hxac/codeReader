# DMA 引擎 axi_dmac 深入

## 1. 本讲目标

`axi_dmac`（AXI DMA Controller）是 ADI HDL 仓库中复用最广、也最核心的 IP 之一：几乎所有数据转换器（ADC/DAC、射频收发器）参考设计都靠它把采样数据搬进/搬出 PS 的 DDR。学完本讲，你应当能够：

- 说清 `axi_dmac` 的「寄存器面 + 数据面」两层结构，以及一次软件提交的传输如何被拆成若干 burst。
- 描述源端（src）、目的端（dest）各自的「请求—数据—响应」三段流水，并解释贯穿其中的 ID 标签机制与 store-and-forward 缓冲。
- 区分三种可插拔通道（AXI-MM / AXI-Stream / ADI-FIFO），并理解 2D 与 Scatter-Gather 两种高级传输模式如何在通用数据通路上叠加。
- 能阅读 `tb/` 下的仿真并亲手跑通一个最小 DMA 仿真。

## 2. 前置知识

本讲假设你已掌握 [u4-l5 寄存器映射与 up_axi](u4-l5-register-map-up-axi.md)（软件如何经 AXI4-Lite 读写 IP 寄存器）与 [u4-l2 Xilinx IP 打包](u4-l2-xilinx-ip-packaging.md)（一个 library 模块如何被打包成 IP）。在此之上补充三个本讲会用到的术语：

- **DMA（Direct Memory Access）**：一个不由 CPU 逐字搬运、自己能发起总线事务的硬件模块。CPU 只需「提交一次描述（从哪搬到哪、搬多少）」，DMA 在后台把数据搬完，再以中断或状态位通知 CPU。
- **AXI 总线族**：`m_axi`（AXI4 Memory-Mapped，带地址，按 burst 读写内存）、`axis`（AXI4-Stream，无地址，纯数据流）、以及 ADI 自定义的 `fifo` 接口（最简单的 valid/en 接口）。本讲的 DMA 三种通道正对应这三种接口。
- **beat 与 burst**：一个 beat 是一个时钟周期搬的数据宽度（如 64 bit）；一个 burst 是一组连续 beat（AXI4 一次最多 256 beat，AXI3 最多 16 beat）。一次「传输（transfer）」由若干 burst 组成。

承接前序讲义：寄存器侧的 `up_axi` 把 AXI4-Lite 翻译成内部寄存器读写；本讲的 `axi_dmac_regmap` 正是它的消费者——软件写 `SRC_ADDRESS`/`X_LENGTH` 等寄存器后置位 `TRANSFER_SUBMIT`，寄存器侧就会向数据面发出一个「DMA 请求」。

## 3. 本讲源码地图

`axi_dmac` 目录下文件很多，本讲聚焦下面这条主链路：

| 文件 | 角色 |
| --- | --- |
| [library/axi_dmac/axi_dmac.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac.v) | 顶层。只例化两个子模块：寄存器面 `axi_dmac_regmap` 与数据面 `axi_dmac_transfer`；并在参数里算好 burst 尺寸。 |
| [library/axi_dmac/axi_dmac_transfer.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_transfer.v) | 数据面骨架。按参数挂载 SG / 2D / Framelock 等可选级，最终把通用请求交给 `request_arb`。 |
| [library/axi_dmac/request_arb.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/request_arb.v) | 真正的数据通路核心。按 `DMA_TYPE_SRC/DEST` 选择源/目的通道，中间夹一个 store-and-forward 缓冲，并例化请求/响应管理器。 |
| [library/axi_dmac/data_mover.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/data_mover.v) | 通用「突发引擎」。给定一个描述符，按 beat 计数把源侧数据推向前方，并在末拍产生 `last`。被流式/FIFO 源通道复用。 |
| [library/axi_dmac/src_axi_mm.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/src_axi_mm.v) | 源端「AXI-MM 读」通道：发 AR、收 R，内部例化 `address_generator`。 |
| [library/axi_dmac/dest_axi_stream.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/dest_axi_stream.v) | 目的端「AXI-Stream 写」通道：把缓冲里的数据按 beat 推上 `m_axis`，内部例化 `response_generator`。 |
| [library/axi_dmac/dmac_sg.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/dmac_sg.v) | Scatter-Gather 子模块。用一条专用 AXI 读口从内存里取「描述符链」，再把每个描述符翻译成一次普通请求。 |
| [library/axi_dmac/request_generator.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/request_generator.v) / [response_generator.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/response_generator.v) / [response_handler.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/response_handler.v) | 请求生成器与两类响应回收器（生成式 / 处理式）。 |
| [library/axi_dmac/dmac_2d_transfer.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/dmac_2d_transfer.v) | 2D 模式：把一次二维传输拆成多行一维请求。 |
| [library/axi_dmac/inc_id.vh](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/inc_id.vh) | 跨时钟域安全的 ID 自增（格雷码）函数。 |

辅助但重要的文件：`address_generator.v`（把地址/长度翻译成 AXI 的 AR/AW 突发）、`axi_dmac_burst_memory.v`（store-and-forward 缓冲）、`axi_dmac_reset_manager.v`（跨时钟域优雅停机与复位）、`splitter.v`（一发多收握手扇出）。

---

## 4. 核心概念与源码讲解

### 4.1 axi_dmac 顶层架构：寄存器面 + 数据面

#### 4.1.1 概念说明

把 `axi_dmac` 想成一个「搬运公司」，它有前后两个柜台：

- **寄存器面（控制面）**：对外的 `s_axi`（AXI4-Lite）窗口。CPU 在这里填表单——源地址、目的地址、长度、是否循环等，然后按一下「提交」(`TRANSFER_SUBMIT`)。填好的表单被打包成一个**DMA 请求（request）**，经一个简单的 `request_valid/request_ready` 握手递给后方。
- **数据面（传输面）**：拿到请求后，自己用 `m_src_axi`（读内存）/`s_axis`（接数据流）/`m_dest_axi`（写内存）/`m_axis`（发数据流）等接口把数据真正搬完，搬完后再产生一个**响应（response）**回寄存器面，触发 `TRANSFER_DONE` 状态位与中断。

顶层 `axi_dmac.v` 几乎不含逻辑，只做两件事：例化这两面，并在参数阶段把「一次能搬多少」算清楚。这种「瘦顶层 + 两个清晰子模块」的切分，是把一个复杂 DMA 拆成可读、可测、可裁剪（SG/2D/Framelock 全是可选 `generate`）结构的关键。

#### 4.1.2 核心流程

一次「内存 → 数据流」的搬运，从软件视角经历：

1. CPU 写寄存器（`SRC_ADDRESS`、`X_LENGTH` 等），置 `TRANSFER_SUBMIT=1`。
2. `axi_dmac_regmap` 把这组寄存器打包成一个请求，`request_valid` 拉高。
3. 数据面 `axi_dmac_transfer` 接受请求，按 burst 上限把它**拆成多个 burst**，逐个执行。
4. 源通道发起读/接收数据，数据进入 **store-and-forward 缓冲**；目的通道从缓冲取数据并发送/写入。
5. 每个 burst 完成后，目的侧回收一个带 ID 的响应；所有 burst 的响应都回来后，寄存器面置 `TRANSFER_DONE`。

关键尺寸（都在顶层用 `localparam` 算定，**综合期固定**）：

\[ \text{BEATS\_PER\_BURST\_LIMIT} = \begin{cases} 16 & \text{AXI3} \\ 256 & \text{AXI4} \\ 1024 & \text{非 AXI 接口} \end{cases} \]

\[ \text{BYTES\_PER\_BURST\_LIMIT} = \min(\text{src 侧},\ \text{dest 侧}) \]

\[ \text{REAL\_MAX\_BYTES\_PER\_BURST} = \min(\text{MAX\_BYTES\_PER\_BURST},\ \text{BYTES\_PER\_BURST\_LIMIT}) \]

store-and-forward 缓冲的字节数为：

\[ \text{BufferSize} = \text{FIFO\_SIZE} \times \text{MAX\_BYTES\_PER\_BURST} \]

\[ \text{ID\_WIDTH} = \lceil \log_2(\text{FIFO\_SIZE} \times 2) \rceil \]

最后一个公式决定了「同时在缓冲里在途的 burst 数」上限——这正是源、目的两侧能解耦异步时钟的根基。

#### 4.1.3 源码精读

**顶层的全部例化只有两处**。寄存器面：

[library/axi_dmac/axi_dmac.v:479-515](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac.v#L479-L515) — 例化 `i_regmap`，把 `s_axi` 信号、控制位（`ctrl_enable/ctrl_pause/ctrl_hwdesc`）和请求/响应接口接到数据面。

数据面：

[library/axi_dmac/axi_dmac.v:582-619](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac.v#L582-L619) — 例化 `i_transfer`（`axi_dmac_transfer`），接收来自 regmap 的请求，输出三条 AXI 主口（`m_dest_axi` / `m_src_axi` / `m_sg_axi`）与流式/FIFO 接口。

注意一个常被忽视的细节：**顶层声明了 src/dest/sg 三条主口的完整读写信号，但永远只会用到「读」或「写」其一**。例如 dest 侧恒为写，所以它的读通道在顶层被显式置 0：

[library/axi_dmac/axi_dmac.v:764-766](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac.v#L764-L766) — `m_dest_axi_arvalid = 1'b0` 等，把 dest 侧「未用的读口」绑死。同理 src 侧的写口、sg 侧的写口也都置 0（L778–L794、L796–L812）。这是为了让 IP 的端口集合在不同 `DMA_TYPE_*` 配置下保持稳定。

burst 尺寸的推导：

[library/axi_dmac/axi_dmac.v:366-392](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac.v#L366-L392) — 注释说明「AXI3 最多 16 拍、AXI4 最多 256 拍、非 AXI 取 1024 作上限」，并据此算出 `BYTES_PER_BURST_LIMIT` 与 `REAL_MAX_BYTES_PER_BURST`。注意 L390–L392 的 `min` 逻辑：用户参数 `MAX_BYTES_PER_BURST` 会被接口能力**钳制**到更小值。

三种通道类型常量：

[library/axi_dmac/axi_dmac.v:325-330](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac.v#L325-L330) — `DMA_TYPE_AXI_MM=0 / DMA_TYPE_AXI_STREAM=1 / DMA_TYPE_FIFO=2`，以及 `HAS_DEST_ADDR`/`HAS_SRC_ADDR`（只有 MM 类型才有地址）。这两个常量决定了 `request_arb` 里 `generate` 分支的选择。

#### 4.1.4 代码实践

**目标**：确认「瘦顶层」结构，并理解参数如何决定可搬运的最大突发。

1. 打开 `axi_dmac.v`，数一数 `endmodule` 之前一共例化了几个子模块（答案应是 2：`i_regmap`、`i_transfer`）。
2. 在 L40–L91 的参数表里找到 `DMA_TYPE_SRC` 与 `DMA_TYPE_DEST` 的默认值（`2` 与 `0`），据此判断**默认配置**是哪种搬运方向。
3. 用上面的公式手算：若 `MAX_BYTES_PER_BURST=128`、`DMA_DATA_WIDTH_SRC=64`、src/dest 均为 AXI4，那么 `REAL_MAX_BYTES_PER_BURST` 是多少？一次 `X_LENGTH=400` 字节的传输会被拆成几个 burst？

**预期结果**：默认 `DMA_TYPE_SRC=2`(FIFO)、`DMA_TYPE_DEST=0`(MM)，即「数据流 → 内存」（典型的 ADC 采集入 DDR）。AXI4 下 `BYTES_PER_BURST_LIMIT = 256*8 = 2048`，钳制 `MAX_BYTES_PER_BURST=128`，故 `REAL_MAX_BYTES_PER_BURST=128`；400 字节 ÷ 128 = 3.125 → 4 个 burst（前 3 个满、最后一个部分）。**待本地验证**：可在仿真里观察 AR 端口发出的 burst 个数。

#### 4.1.5 小练习与答案

**练习 1**：为什么顶层要把 dest 的读口、src 的写口都置成常量 0，而不是干脆不声明这些端口？
> **答**：端口表在 IP 打包后（component.xml / hw.tcl）是固定的，不同 `DMA_TYPE_*` 配置共用同一份端口声明；置 0 比条件声明更便于打包工具与上层块设计处理。

**练习 2**：`ID_WIDTH` 由 `FIFO_SIZE` 决定（L357–L363），背后的物理含义是什么？
> **答**：`ID_WIDTH` 是「同时在途的 burst 数」的二进制位数（容量为 `FIFO_SIZE*2`）。缓冲容量 = `FIFO_SIZE` 个 burst，但流水线里源侧可能已发出下一批，所以 ID 空间留了 2 倍裕量，确保标签不会回绕重叠。

---

### 4.2 data_mover 与请求/响应管理：突发引擎与 ID 流转

#### 4.2.1 概念说明

数据面内部有一套贯穿始终的「ID 标签」机制，用来在源、缓冲、目的三个环节之间追踪每一个 burst——尤其在 src 与 dest 处于**不同时钟域**时，ID 是唯一能可靠关联「这个数据块对应哪个请求」的手段。围绕它有四个角色：

- **request_generator（请求生成器）**：把一次传输（含若干 burst）拆开，为每个 burst 分配一个 ID，逐个发给源通道。它维护一个 `burst_count`，减到 0 即传输结束（`eot`）。
- **data_mover（突发引擎）**：源侧（流式/FIFO）每收到一个带 ID 的描述符，就按 beat 计数把数据推向缓冲，在末拍产生 `last`，并把 ID 递增交给下一个 burst。
- **address_generator（地址生成器）**：源/目的 MM 侧用它把「地址 + 长度」翻译成一条 AXI 突发（AR/AW 通道），同样以 ID 计数推进。
- **response_generator / response_handler（响应回收器）**：目的侧每完成一个 burst，回收一个带 ID 的响应。MM 目的侧真正有 AXI `B` 响应可消费，用 `response_handler`；流式/FIFO 目的侧没有外部响应，用 `response_generator`「凭空」生成 `RESP_OKAY`。

一个微妙但关键的点：**ID 用格雷码自增**。因为 ID 要跨时钟域（src_clk → dest_clk 等），多位二进制同时翻转会被同步器采样成毛刺；格雷码每次只翻一位，配合 `sync_bits` 两级同步即安全。

#### 4.2.2 核心流程

源、目的两侧各自跑一条「请求—数据—响应」流水，中间用 store-and-forward 缓冲解耦：

```
请求面(regmap)
   │  request (src/dest addr, x_length, last, ...)
   ▼
request_generator ── 分配 ID ──► 源通道(src_axi_mm / src_axi_stream / src_fifo_inf)
   │  每个 burst 一个 ID                      │ 发 AR/收 R  或  收 axis/fifo
   │                                          ▼
   │                              axi_dmac_burst_memory (store-and-forward, 按 ID 存取)
   │                                          │
   │                                          ▼
   │                              目的通道(dest_axi_mm / dest_axi_stream / dest_fifo_inf)
   │  response (带 ID) ◄── 回收 ID ─────────── │ 写内存  或  发 axis/fifo
   ▼
response_manager ── 汇总 ──► regmap (TRANSFER_DONE / IRQ)
```

- request_generator 的 `request_id` 单调递增（发给源），目的侧的 `response_id` 也单调递增（回收）；当某次传输的最后一个 burst 响应被回收，即产生 `eot`。
- 源侧还有一个**节流器**（`src_throttled_request_id`），确保「已发出但尚未进缓冲」的 burst 数不超过缓冲容量，防止溢出。
- 源侧若为流式接口，可能比编程长度**提前**拉 `TLAST`（部分传输）；此时 `data_mover` 的 `ALLOW_ABORT` 逻辑会发出 `rewind`，让 request_generator 回退 ID 并通知响应管理器「这一段提前结束了」。

#### 4.2.3 源码精读

**请求生成器状态机**：

[library/axi_dmac/request_generator.v:73-77](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/request_generator.v#L73-L77) — 五状态：`IDLE → GEN_ID →（REWIND_ID/CONSUME/WAIT_LAST）`。`GEN_ID` 状态逐个发 ID：

[library/axi_dmac/request_generator.v:106-110](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/request_generator.v#L106-L110) — `eot = burst_count == 0`；`incr_en` 判断「目的侧还没追上（response_id != id_next）且使能」，避免源侧跑得太快冲爆缓冲。`burst_count` 在 [L112-L120](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/request_generator.v#L112-L120) 中从 `req_burst_count` 载入并逐拍递减。注意注释（[L92-L97](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/request_generator.v#L92-L97)）：这里只数 burst 数，最后一个 burst 的零头由 `address_generator`/`data_mover` 处理。

**ID 自增用格雷码**：

[library/axi_dmac/inc_id.vh:62-67](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/inc_id.vh#L62-L67) — `inc_id = b2g(g2b(id) + 1)`：先把格雷码转二进制、加一、再转回格雷码。配合 `request_arb` 里的 `sync_bits #(.ASYNC_CLK)` 把 ID 安全拍到另一时钟域。

**突发引擎 data_mover**（在源侧流式/FIFO 通道里被复用）：

[library/axi_dmac/data_mover.v:107-120](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/data_mover.v#L107-L120) — `s_axi_ready` 仅在「有待发 burst 且处于 active 且未 abort 且 sync 已对齐」时拉高；`m_axi_valid` 直接透传，`m_axi_last` 在计满或提前 tlast 时产生。本质是一个带 beat 计数的状态机：

[library/axi_dmac/data_mover.v:200-215](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/data_mover.v#L200-L215) — `req_ready` 的三段条件（末拍、空闲待命、abort 后等 rewind），以及 `beat_counter` 计满即 `last_eot`。`has_sync`（[L114](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/data_mover.v#L114)）实现 `SYNC_TRANSFER_START`：在收到带 sync 标志的 beat 之前丢弃数据。

**响应回收器（生成式）**：

[library/axi_dmac/response_generator.v:62-84](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/response_generator.v#L62-L84) — `resp_valid = request_id != response_id && enabled`：只要请求侧已经推进而自己还没追上，就持续吐 `RESP_OKAY` 响应；每收一个响应 `response_id` 自增。用于没有真实 AXI 响应的流式/FIFO 目的侧。

**响应回收器（处理式）**：

[library/axi_dmac/response_handler.v:66-91](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/response_handler.v#L66-L91) — 这是给 **MM 目的侧**用的：`active = id != request_id`，`bready/resp_valid` 在 active 时跟随 AXI `B` 通道，每收一个 `B` 响应 `id` 自增。它把外部真实的 `bresp` 透传出去（[L66](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/response_handler.v#L66)）。

最后所有响应在 `request_arb` 里汇入 `axi_dmac_response_manager`，由它把「目的侧的逐 burst 响应」翻译回「寄存器面期望的逐传输响应」（带 `measured_burst_length`、`partial` 等信息），并管理 `TRANSFER_DONE` 对应的 transfer ID。

#### 4.2.4 代码实践

**目标**：跟踪一个 burst 的 ID 生命周期。

1. 在 `request_arb.v` 里搜索 `request_id`、`response_id`，找到 `i_req_gen`（[L1144](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/request_arb.v#L1144)）和 `i_response_manager`（[L1175](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/request_arb.v#L1175)）的例化，确认它们共用同一对 `request_id`/`response_id`。
2. 注意 [request_arb.v:898-905](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/request_arb.v#L898-L905)：`request_id` 经 `sync_bits` 拍到 `src_clk` 域得到 `src_request_id`；[L945-L952](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/request_arb.v#L945-L952) 把 `dest_response_id` 拍到 `req_clk` 域得到 `response_id`。这就是「跨时钟域追踪 ID」的实物。
3. 阅读节流器 [request_arb.v:933-943](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/request_arb.v#L933-L943)，解释它如何依据 `src_throttled_request_id` 与 `src_data_request_id` 限制源侧在途 burst 数。

**预期结果**：你能画出 `request_id → sync_bits → src_request_id → src_throttled_request_id →（源通道发数据，data_mover 用 id）→ 缓冲 → 目的通道回收 → dest_response_id → sync_bits → response_id → response_manager` 这条 ID 闭环。**待本地验证**：在仿真波形里观察这三个 id 信号是否单调递增、目的侧追上源侧即 `eot`。

#### 4.2.5 小练习与答案

**练习 1**：`data_mover` 被哪些通道复用？为什么 `src_axi_mm` 不用它？
> **答**：被 `src_axi_stream` 与 `src_fifo_inf` 复用（流式/FIFO 源需要按 beat 计数并产生 last）。`src_axi_mm` 走 AXI 读，burst 长度由 `address_generator` 通过 AR 通道直接告诉从机，数据由 `rdata/rlast` 驱动，不需要 data_mover 再计 beat。

**练习 2**：`response_generator` 和 `response_handler` 何时分别使用？
> **答**：目的侧为 MM 时，有真实的 AXI `B` 响应可消费，用 `response_handler`（透传 `bresp`）；目的侧为流式/FIFO 时没有外部响应，用 `response_generator` 自行生成 `RESP_OKAY` 以维持「每个 burst 一个响应」的统一接口。

**练习 3**：为什么 ID 自增要用格雷码？
> **答**：ID 要在 src/dest/req 等异步时钟域之间传递；多位二进制同时翻转经两级同步器可能被采样成非法中间值，而格雷码每次只变一位，可被 `sync_bits` 安全地单比特同步。

---

### 4.3 src/dest 通道：可插拔接口与 2D/SG 模式

#### 4.3.1 概念说明

`axi_dmac` 最大的设计弹性来自「源端三选一 × 目的端三选一」的可插拔通道。三种类型由 `DMA_TYPE_SRC` / `DMA_TYPE_DEST`（`0=MM, 1=Stream, 2=FIFO`）在综合期选定，`request_arb` 用 `generate if` 只例化被选中的一种、其余接 0：

| 通道 | 接口 | 典型用途 |
| --- | --- | --- |
| `src_axi_mm` / `dest_axi_mm` | AXI4 MM（带地址、AR/AW/R/W/B） | 内存 ↔ 内存、PS DDR 读写 |
| `src_axi_stream` / `dest_axi_stream` | AXI4-Stream（无地址、TVALID/TREADY/TLAST） | 接 ADC 数据流、发 DAC 数据流 |
| `src_fifo_inf` / `dest_fifo_inf` | ADI FIFO（valid/en） | 最简、固定速率的 FIFO 器件 |

两两组合就涵盖了 ADI 参考设计里的全部搬运方向：`Stream→MM`（ADC 采集入 DDR）、`MM→Stream`（DDR 回放给 DAC）、`FIFO→MM`、`MM→MM`（内存拷贝）等。

在「选好源/目的通道」之上，DMA 还支持两种把传输「放大」的模式：

- **2D 传输**：一次提交描述一个矩形（`X_LENGTH` × `Y_LENGTH` 行，带行间距 `STRIDE`）。常用于视频/帧缓存——每行连续、行间有 padding。
- **Scatter-Gather（SG）**：软件在内存里预先排好一张「描述符链」，DMA 用一条专用读口 `m_sg_axi` 逐个取描述符并执行，从而把多段不连续内存拼成一次逻辑传输，CPU 一次提交即可。

二者都是**在通用数据通路之前**加一级「请求展开器」：2D 把一次二维请求展开成多行一维请求；SG 把一条描述符链展开成多个一维请求。展开后的请求格式与普通请求完全一致，下游 `request_arb` 无感知。

#### 4.3.2 核心流程

**src_axi_mm（AXI 读源）**：

1. 收到一个带地址与「末 burst 长度」的描述符。
2. `address_generator` 把地址、长度翻译成一条 AXI AR 突发（`arvalid/araddr/arlen/arsize/arburst`），`arlen` 在非末 burst 取满、末 burst 取零头。
3. 从机返回 `rdata`，`m_axi_rready` 恒为 1（缓冲侧保证有空间），每个 `rlast` 让 `id` 自增。
4. 数据按 `fifo_valid/fifo_data/fifo_last` 推入 store-and-forward 缓冲。

**dest_axi_stream（AXI-Stream 写目的）**：

1. 收到一个描述符（带 `xlast`、`islast`）。
2. 从缓冲读数据，按 beat 驱动 `m_axis_valid/m_axis_data`，受 `m_axis_ready` 反压。
3. 末拍产生 `m_axis_last`；每个 burst 完成后 `id` 自增并产生一个响应。

**dmac_2d_transfer（2D 展开）**：

1. 锁存 `dest/src_address`、`x_length`、`y_length`、stride。
2. 每发一行（一维请求），地址 += stride、`y_length--`。
3. `y_length == 0` 时该 2D 请求结束；用 `req_id/eot_id` 配对判断「哪一行的完成响应对应整次 2D 的 eot」。

**dmac_sg（SG 描述符取回）**：一个四状态机 `IDLE → SEND_ADDR → RECV_DESC → DESC_READY`：

1. `IDLE`：等待软件提交的「首描述符地址」（经一条 async FIFO 从 req 域进 sg 域）。
2. `SEND_ADDR`：发 AR（`arlen=5`，即 6 拍）读一个描述符。
3. `RECV_DESC`：逐拍接收 6 个 64-bit 字，按固定布局解析：`{id,flags}`、`dest_addr`、`src_addr`、`next_desc_addr`、`{x_len,y_len}`、`{dst_stride,src_stride}`。
4. `DESC_READY`：把解析出的描述符作为普通请求发往下游；若 `flags[0]`（last）置位则链结束回到 `IDLE`，否则用 `next_desc_addr` 继续 `SEND_ADDR` 取下一个。

#### 4.3.3 源码精读

**src_axi_mm 的核心**：地址生成 + 恒定 rready。

[library/axi_dmac/src_axi_mm.v:147-183](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/src_axi_mm.v#L147-L183) — 例化 `i_addr_gen`，把 `req_address` / `req_last_burst_length` 翻译成 `m_axi_ar*`。

[library/axi_dmac/src_axi_mm.v:195-207](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/src_axi_mm.v#L195-L207) — 注释（[L203-L207](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/src_axi_mm.v#L203-L207)）解释「不会在请求前收到数据、不会在缓冲满后还发请求，所以 `m_axi_rready` 恒为 1」。`id` 在每个 `rlast` 自增（[L195-L201](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/src_axi_mm.v#L195-L201)）。`req_valid` 经 [L131-L145](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/src_axi_mm.v#L131-L145) 的 `splitter` 一发三（burst-length 反馈、地址生成、bl_valid）。

**address_generator 的尺寸推导**（理解 burst 如何对齐 AXI）：

[library/axi_dmac/address_generator.v:86-98](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/address_generator.v#L86-L98) — `size` 由数据宽度映射（64bit→`3'b011`）；`burst=2'b01`（INCR）。末 burst 用 `last_burst_len`、其余用 `MAX_LENGTH`（[L121-L130](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/address_generator.v#L121-L130)）；地址按 `MAX_BEATS_PER_BURST` 递增（[L132-L138](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/address_generator.v#L132-L138)）。

**dest_axi_stream 的核心**：从缓冲取数驱动 axis。

[library/axi_dmac/dest_axi_stream.v:102-109](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/dest_axi_stream.v#L102-L109) — `m_axis_valid = fifo_valid & active & has_sync`，`fifo_ready = m_axis_ready & active & has_sync`（数据流反压直连），`m_axis_last = req_xlast_d & fifo_last & data_eot`（只有「本段是末段」且「缓冲里是该 burst 末拍」才拉 TLAST）。每个 burst 末拍 `id` 自增（[L146-L152](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/dest_axi_stream.v#L146-L152)），并由内嵌的 `response_generator`（[L162-L179](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/dest_axi_stream.v#L162-L179)）产生响应。

**2D 展开的地址步进**：

[library/axi_dmac/dmac_2d_transfer.v:164-182](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/dmac_2d_transfer.v#L164-L182) — 每发一行，`dest_address += dest_stride`、`src_address += src_stride`、`y_length--`；`out_last = (y_length == 0)`（[L111](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/dmac_2d_transfer.v#L111)）。`DMA_2D_TLAST_MODE`（[L200](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/dmac_2d_transfer.v#L200)）选择 TLAST 是「帧末」还是「行末」。

**SG 描述符状态机与字段解析**：

[library/axi_dmac/dmac_sg.v:101-104](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/dmac_sg.v#L101-L104) — 四状态定义。`arlen='h5`（[L154](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/dmac_sg.v#L154)）即 6 拍描述符。

[library/axi_dmac/dmac_sg.v:204-222](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/dmac_sg.v#L204-L222) — 按 `hwdesc_counter` 解析 6 拍：beat0 取 `id`(63:32) 与 `flags`(1:0)；beat1/2/3 取 dest/src/next 描述符地址；beat4 取 `x_length`(63:32)/`y_length`(31:0)；beat5 取 `dst_stride`/`src_stride`。这与官方文档「Descriptor Structure」一一对应。

[library/axi_dmac/dmac_sg.v:227-263](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/dmac_sg.v#L227-L263) — 状态转移：`DESC_READY` 收到 `fetch_ready` 后，若 `flags & MASK_LAST_HWDESC`（bit0）则回 `IDLE`，否则继续 `SEND_ADDR` 取下一个描述符。

解析好的描述符经一条 async FIFO（[L265-L296](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/dmac_sg.v#L265-L296)）送回 req 域；在 `axi_dmac_transfer` 里，当 `ctrl_hwdesc`（即 `CONTROL.HWDESC`）置位时，SG 输出取代普通寄存器请求进入下游（[axi_dmac_transfer.v:568-L576](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_transfer.v#L568-L576)）。

#### 4.3.4 代码实践

**目标**：跑通一个真实的 DMA 仿真，直观看到「AXI-MM 源 → FIFO 目的」的搬运，并对比两种通道。

仓库自带可直接运行的仿真 `tb/dma_read_tb`，它直接例化 `axi_dmac_transfer`，配置为 `DMA_TYPE_SRC=0`(MM 读)、`DMA_TYPE_DEST=2`(FIFO 出)：

[library/axi_dmac/tb/dma_read_tb.v:105-153](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/tb/dma_read_tb.v#L105-L153) — 例化 `transfer`，把 `m_axi_ar*`/`m_axi_r*` 接到 `axi_read_slave`（模拟内存），`fifo_rd_*` 接到读取校验逻辑；`req_length` 每次 `+4` 不断提交新传输。

操作步骤（默认用 Icarus Verilog，无需 GUI）：

1. 安装 `iverilog`（如 `apt install iverilog`）。
2. 进入测试目录并执行自带脚本：
   ```bash
   cd library/axi_dmac/tb
   ./dma_read_tb        # 默认 MODE=gui、SIMULATOR 未设 → 走 run_tb.sh 的 icarus 分支
   ```
   脚本 [dma_read_tb](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/tb/dma_read_tb) 列出了全部源文件并 `source ../../common/tb/run_tb.sh`；该脚本（[run_tb.sh](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/common/tb/run_tb.sh)）支持 `SIMULATOR=modelsim|xsim|xcelium`，默认 Icarus。
3. 观察 `vcd/` 下生成的 VCD 波形：`arvalid` 与 `rlast` 的个数应等于「传输字节数 ÷ burst 字节数」；`fifo_rd_valid` 每次出现的 `fifo_rd_dout` 应与 `axi_read_slave` 注入的数据吻合（测试台在 [L155-L186](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/tb/dma_read_tb.v#L155-L186) 做逐拍比对，不一致会置 `failed`）。

**需要观察的现象**：AR 通道按 burst 发出、R 通道按 burst 回来、FIFO 输出端数据顺序与内存一致、`req_ready` 在传输结束（`eot`）后再次拉高接受下一次请求。

**对比思考**：本例 src 是 MM（需要 `address_generator` 发 AR），dest 是 FIFO（用 `dest_fifo_inf` + `response_generator`）。把 `DMA_TYPE_DEST` 想象成 `1`（Stream），则 dest 通道会换成 `dest_axi_stream`——这正是真实参考设计里「ADC 采样经 axis 进入、DMA 写入 DDR」的反向情形。**待本地验证**：若你的环境无 `iverilog`，可改用 `SIMULATOR=xsim MODE=batch ./dma_read_tb`（需 Vivado）。

#### 4.3.5 小练习与答案

**练习 1**：`src_axi_mm` 与 `dest_axi_stream` 分别适用于什么场景？
> **答**：`src_axi_mm` 适用于「从带地址的总线（如 PS DDR、内存映射外设）读数据」，由 `address_generator` 发 AR、收 R；`dest_axi_stream` 适用于「把数据以无地址流的形式送给下游（如 DAC 数据通路、axi_ad9361 的发送侧）」，靠 `m_axis_valid/ready/last` 握手。二者组合 `MM→Stream` 就是典型的「DDR 回放给射频发送」。

**练习 2**：SG 的描述符链怎么终止？`flags` 的两位各代表什么？
> **答**：当某个描述符的 `flags[0]`（`MASK_LAST_HWDESC`）置 1 时，`dmac_sg` 在 `DESC_READY` 态回到 `IDLE`，链结束（[dmac_sg.v:250-L260](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/dmac_sg.v#L250-L260)）。`flags[0]` 表示「这是最后一个描述符」，`flags[1]`（`MASK_EOT_IRQ`）表示「该段搬完后产生一次结束中断」。

**练习 3**：为什么说 2D 和 SG 是「在通用通路之前加一级展开器」，而不是改写 `request_arb`？
> **答**：因为展开后输出的请求格式（地址、长度、stride、last…）与普通请求完全一致，`axi_dmac_transfer` 用 `ctrl_hwdesc`/`DMA_2D_TRANSFER` 在 `generate` 里二选一路由（[L568-L576](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_transfer.v#L568-L576)、[L520-L561](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/axi_dmac_transfer.v#L520-L561)），下游 `request_arb` 与源/目的通道完全无感知。这把「搬一条」的复杂逻辑与「搬多段」的编排逻辑彻底解耦。

---

## 5. 综合实践

把本讲三节串起来，完成一次「全链路数据通路」梳理：

1. **画图**：在一张图上画出从软件 `TRANSFER_SUBMIT` 到 `TRANSFER_DONE` 的完整通路，至少标注这些节点：`axi_dmac_regmap`（请求打包）→（可选 SG/2D 展开）→ `request_generator`（分配 ID）→ 源通道（`src_axi_mm` 的 `address_generator` 或 `data_mover`）→ `axi_dmac_burst_memory`（缓冲）→ 目的通道（`dest_axi_stream` 或 `dest_axi_mm` 的 `response_handler`）→ `response_manager`（汇总）→ regmap（置 `TRANSFER_DONE`/IRQ）。用箭头标出 `request_id`、`response_id` 的流向与它们跨时钟域的位置（`sync_bits`）。
2. **对号入座**：打开任意一个真实参考设计，例如 `projects/fmcomms2/common/fmcomms2_bd.tcl`，找到其中例化的 `axi_dmac`。根据它接的上下游（ADC 的 `axis` 输出？PS 的 `m_axi`？），判断它的 `DMA_TYPE_SRC` 与 `DMA_TYPE_DEST` 各应配成什么，并说明它属于「采集入 DDR」还是「DDR 回放」。
3. **量化**：假设该工程的 ADC 通道经 `util_cpack2` 打包后位宽为某值（在 bd.tcl 里读 `adi_ip_instance` 的 `DMA_DATA_WIDTH_SRC`），若 `MAX_BYTES_PER_BURST=128`、`FIFO_SIZE=8`，用本讲公式算出 store-and-forward 缓冲的字节数与 ID 位宽。
4. **自检**：用 4.3.4 的 `dma_read_tb` 实跑（或读源码）验证你对 burst 拆分的预测。

**预期产出**：一张数据通路图 + 一段对该参考设计中 `axi_dmac` 配置与缓冲尺寸的说明。这一步是把「读单个 IP」和「读懂整板设计」连接起来的关键练习。

## 6. 本讲小结

- `axi_dmac` 顶层是「瘦壳」：只例化寄存器面 `axi_dmac_regmap` 与数据面 `axi_dmac_transfer`，burst 尺寸等关键常量在综合期用 `localparam` 算定。
- 数据面以 `request_arb` 为核心：源通道（MM/Stream/FIFO 三选一）把数据送入 store-and-forward 缓冲，目的通道（同样三选一）从缓冲取数发出，二者靠 burst 级 ID 解耦。
- ID 用格雷码自增（`inc_id.vh`）经 `sync_bits` 跨时钟域；`request_generator` 发 ID、`response_generator`/`response_handler` 回收 ID，形成闭环。
- `data_mover` 是流式/FIFO 源侧复用的「突发引擎」，按 beat 计数并在末拍产生 `last`，还支持 `SYNC_TRANSFER_START` 与提前 TLAST 的 `rewind`。
- 2D 与 SG 都是「请求展开器」：2D 把矩形展开成多行、SG 把描述符链展开成多段，展开后的请求格式统一，下游无感知。
- 仿真入口 `tb/dma_read_tb` 直接例化 `axi_dmac_transfer`，用 Icarus/Vivado/ModelSim 均可跑通，是验证理解的捷径。

## 7. 下一步学习建议

- 继续 [u5-l2 数据转换器 IP 模式](u5-l2-data-converter-ip.md)：看 `axi_ad9361` 这类 IP 如何把 ADC 采样打成 axis 喂给本讲的 `axi_dmac`，把「采集 → cpack2 → dmac → DDR」整条链路补全。
- 阅读 [u5-l3 util 工具 IP](u5-l3-util-ips.md)：本讲反复出现的 `util_axis_fifo`（SG/请求侧的 async FIFO）、`util_cdc`（`sync_bits`/`sync_event`）正是来自这一族工具 IP，理解它们能让你更顺地读懂 `request_arb` 的跨时钟域处理。
- 进阶可读 `axi_dmac_burst_memory.v`（store-and-forward 缓冲如何按 ID 寻址、如何处理宽窄位宽不对称）与 `axi_dmac_reset_manager.v`（跨时钟域优雅停机），它们是本讲点到即止的两块「深水区」。
- 想了解软件如何驱动本 IP，可对照 [u4-l5](u4-l5-register-map-up-axi.md) 提到的 regmap，阅读 no-OS 的 `drivers/axi_core/axi_dmac/axi_dmac.c`，把「写哪些寄存器、按什么顺序」与本讲的「请求打包」对应起来。
