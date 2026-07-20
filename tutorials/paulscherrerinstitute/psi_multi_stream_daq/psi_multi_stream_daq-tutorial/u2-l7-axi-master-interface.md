# AXI 主接口 psi_ms_daq_axi_if：DDR 写入封装

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 `psi_ms_daq_axi_if` 在整个 IP 核里的定位——它是 DMA 引擎与外部 DDR 之间的「写专用」封装层，把「地址+大小命令」和「数据流」这两条独立握手合并成一组标准 AXI4 写事务，并在写完后回送一个 `Done` 脉冲。
- 解释 `i_wrinfo_fifo` 为什么要把 `Cmd_Addr`（32 位）和 `Cmd_Size`（32 位）拼成 64 位再进 FIFO，以及它如何解耦「命令到达」与「命令被执行」的时间。
- 读懂 `psi_common_axi_master_full` 的例化，说清为什么本模块把 `impl_write_g` 设为 `true`、把 `impl_read_g` 设为 `false`，以及这两个开关各自省掉了哪部分电路。
- 解释 `Done <= DoneI or ErrorI` 这一行把「正常完成」与「写错误」合并成同一个脉冲的设计含义，并追踪 `Done` 如何变成状态机里的 `TfDone`，进而驱动 IRQ FIFO 生成中断。
- 沿着「`DmaMem_CmdVld` → 写命令 FIFO → `axi_master_full` → `M_Axi_WLast` → `M_Axi_BValid` → `Done`」这条主线，把一次 512 字节突发从入口到出口的信号路径画成一张图。

本讲是 u2-l6 的直接续篇。u2-l6 把 DMA 引擎内部的状态机与字节对齐讲到了「`Mem_CmdVld`/`Mem_DatVld` 这一拍发出」为止；本讲就接着这两根线往下走——命令和数据出了 DMA 之后，是谁把它们真正搬上 AXI 总线写进 DDR 的？答案就是本讲的主角 `psi_ms_daq_axi_if`（在顶层例化名为 `i_memif`）。本讲刻意不展开 `psi_common_axi_master_full` 这个第三方 IP 内部的 AXI 协议状态机细节（那是 `psi_common` 库的事），而是聚焦于「本模块如何**封装**它」。

## 2. 前置知识

进入端口表之前，先用通俗语言把本讲反复出现的几个概念讲清。

**AXI4 的五个独立通道**

AXI4 是 ARM 定义的一种总线协议，它把一次读写拆成**五个相互独立的通道**，每个通道都有一对 `Valid`/`Ready` 握手：

- **写地址通道（AW）**：主机告诉从机「我要往这个地址写」。
- **写数据通道（W）**：主机把数据一拍一拍送过去，最后一拍带 `WLast`。
- **写响应通道（B）**：从机写完后回一个 `BResp`，告诉主机「成了」或「出错了」。
- **读地址通道（AR）**：主机告诉从机「我要读这个地址」。
- **读数据通道（R）**：从机把数据一拍一拍送回，最后一拍带 `RLast`。

写一次数据只需要 AW、W、B 三个通道；读一次只需要 AR、R 两个通道。本模块**只写不读**，所以只用 AW、W、B，AR、R 全部接 `'0'`/`open` 挂掉。

> 名词提示：AXI 里常用「突发（burst）」指一次地址请求对应的一串连续数据拍。「beat（拍）」就是这串里的一个数据传输。

**`Valid`/`Ready` 握手**

AXI 每个通道都是主机拉 `Valid`、从机拉 `Ready`，**两者同一拍都为 1 才算一次有效传输**。本讲里你会看到三组这样的握手：命令握手（`Cmd_Vld`/`Cmd_Rdy`）、数据握手（`Dat_Vld`/`Dat_Rdy`）、以及 AXI 各通道的 `*Valid`/`*Ready`。

**「命令」和「数据」为什么要分两条线？**

DMA 引擎（u2-l6）发出的是**地址+大小**（「往哪写、写多少」），而真正的样本字节是另一条**数据线**流式送来的。这两条线在时间上不一定对齐：可能命令先到、数据还没攒够；也可能数据早就备好、命令还没下发。`psi_ms_daq_axi_if` 的核心职责之一，就是用一个 FIFO 把「先到的命令」缓存住，等数据和执行资源都就绪时再合并发出。

**为什么把第三方的 `psi_common_axi_master_full` 包一层？**

`psi_common_axi_master_full` 是 PSI 通用库里一个功能完整的 AXI 主接口 IP：它能做内部宽度到 AXI 宽度的转换、能按最大拍数拆分长突发、能管理多个在途（outstanding）事务。但它是一个**通用**组件，接口比较「裸」。`psi_ms_daq_axi_if` 这一层的存在，是为了把通用组件**裁剪、适配**成本 IP 核刚好需要的形状：只写、命令带大小、用一个命令信息 FIFO 缓冲、最后合并成一个 `Done` 信号。这样上层（DMA 引擎、状态机）就不必直接面对一堆 AXI 信号。

**承接 u2-l6 的结论**

u2-l6 已经讲清：DMA 引擎在 `Done_s`/`Cmd_s` 状态里发出 `Mem_CmdAddr`/`Mem_CmdSize`/`Mem_CmdVld` 和 `Mem_DatData`/`Mem_DatVld`，发完一条命令后就回到 `Idle_s` 等下一条。本讲直接复用这条结论——DMA 发出的 `Mem_*` 信号，在顶层（`psi_ms_daq_axi.vhd`）里就改名为 `DmaMem_*`，原封不动地连进了 `i_memif`（即本讲的 `psi_ms_daq_axi_if`）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [hdl/psi_ms_daq_axi_if.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi_if.vhd) | 本讲主角：内存写接口封装层。实体声明（L20–L83）、`i_wrinfo_fifo` 命令信息 FIFO（L109–L124）、`psi_common_axi_master_full` 例化（L129–L207）、`Done <= DoneI or ErrorI`（L209）全在这一个文件里。 |
| [hdl/psi_ms_daq_axi.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd) | 顶层。本讲引用它看 `i_memif` 如何被例化（L424–L477）、`MaxOpenCommands_g => max(2, Streams_g)` 的用意与注释（L430）、`Done` 如何接到状态机的 `TfDone`（L445 与 L384），以及顶层 `sync_apc_reg` 进程把 `M_Axi_AwCache`/`AwProt` 直接驱动、而 `axi_if` 侧对应端口留 `open` 的细节（L211–L226、L451–L452）。 |
| [hdl/psi_ms_daq_daq_sm.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd) | 控制状态机。本讲只引用 `TfDone` 的接收端：端口声明（L63）、`TfDoneReg` 寄存（L244）、`TfDoneCnt` 递增与配合 IRQ FIFO 出队生成中断（L571–L578），用来定位 `Done` 在中断链路里的位置。 |
| [tb/psi_ms_daq_axi/psi_ms_daq_axi_tb_pkg.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_axi/psi_ms_daq_axi_tb_pkg.vhd) | 顶层 testbench 辅助包。本讲用它说明「`psi_ms_daq_axi_if` 没有独立 TB，端到端写行为在顶层 TB 的共享内存 `Memory` 数组里校验」（L32–L33）。 |

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：先看实体的端口与 generic，再看命令信息 FIFO，再看 `axi_master_full` 的例化（含 `impl_write_g`/`impl_read_g` 的取舍），最后看 `Done` 的合成与它在 `TfDone`/IRQ 链路里的位置。

### 4.1 实体端口与 generic：写专用接口的形状

#### 4.1.1 概念说明

`psi_ms_daq_axi_if` 对外暴露三类信号：

1. **控制信号**：`Clk`、`Rst_n`（注意是低有效的复位，模块内部会取反成高有效）。
2. **用户侧写接口**：一组「写命令」(`Cmd_Addr`/`Cmd_Size`/`Cmd_Vld`/`Cmd_Rdy`)、一组「写数据」(`Dat_Data`/`Dat_Vld`/`Dat_Rdy`)、一个「完成」脉冲 `Done`。
3. **AXI4 主侧接口**：完整的 AW、W、B、AR、R 五个通道——但本模块只真正用 AW、W、B，AR、R 是为了匹配 `axi_master_full` 的端口而存在、实际接死。

这套形状清晰地告诉读者：**本模块是一个「命令+数据进、AXI 写事务出、Done 回报」的单向写通道**。

#### 4.1.2 核心流程

从用户侧到 AXI 侧，数据与控制流的总体走向：

```
用户侧                          内部                        AXI 主侧
─────────────────────────────────────────────────────────────────────
Cmd_Addr/Cmd_Size ─┐
                    ├─> [i_wrinfo_fifo] ─> cmd_wr_addr/size ─┐
Cmd_Vld/Cmd_Rdy ────┘                                        │
                                                            ├─> [axi_master_full] ─┬─> M_Axi_AW*
Dat_Data ──────────────────────────────────────────────────┤                        ├─> M_Axi_W* (含 WLast)
Dat_Vld/Dat_Rdy ───────────────────────────────────────────┘                        ├─> M_Axi_B* (响应输入)
                                                                                   └─> wr_done_o/wr_error_o ─> Done
```

要点：

- **命令与数据走两条独立的用户侧握手**，进模块后才在 `axi_master_full` 里被合并成一组 AXI 写事务。
- **`Done` 是回报**，不是握手。它在 `axi_master_full` 收到 B 通道响应（`BValid`/`BResp`）后才拉高一个脉冲。
- **AR、R 通道**在实体里只是「占位端口」，实际不传任何有意义的数据（见 4.3）。

#### 4.1.3 源码精读

generic 列表给出了本模块全部可调参数（[psi_ms_daq_axi_if.vhd:21-30](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi_if.vhd#L21-L30)）：

```vhdl
generic(
  IntDataWidth_g          : positive                 := 64;   -- 内部字宽（与 DMA/输入一致）
  AxiDataWidth_g          : natural range 64 to 1024 := 64;   -- AXI 数据位宽（可宽于内部）
  AxiMaxBeats_g           : natural range 1 to 256   := 256;  -- 单次突发最大拍数
  AxiMaxOpenTrasactions_g : natural range 1 to 8     := 8;    -- 在途事务数（拼写 Trasactions 同源码）
  MaxOpenCommands_g       : positive                 := 16;   -- 命令信息 FIFO 深度
  DataFifoDepth_g         : natural                  := 1024; -- axi_master_full 内部数据 FIFO 深度
  AxiFifoDepth_g          : natural                  := 1024; -- axi_master_full 内部 AXI FIFO 深度
  RamBehavior_g           : string                   := "RBW" -- 先读后写
);
```

> 名词提示：`IntDataWidth_g` 是本 IP 核内部统一的「字宽」（u2-l1、u2-l5 已讲，默认 64 位）；`AxiDataWidth_g` 是真正打到 DDR 上的 AXI 数据位宽，可以比内部宽（例如 128/256/512），由 `axi_master_full` 做宽度转换。`AxiMaxBeats_g` 限制单次突发不能超过多少拍，超过就会被拆成多次。

端口里，用户侧写命令与写数据分得很清楚（[psi_ms_daq_axi_if.vhd:35-44](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi_if.vhd#L35-L44)）：

```vhdl
-- Write Command
Cmd_Addr      : in  std_logic_vector(31 downto 0);
Cmd_Size      : in  std_logic_vector(31 downto 0);
Cmd_Vld       : in  std_logic;
Cmd_Rdy       : out std_logic;
-- Write Data
Dat_Data      : in  std_logic_vector(IntDataWidth_g - 1 downto 0);
Dat_Vld       : in  std_logic;
Dat_Rdy       : out std_logic;
-- Response
Done          : out std_logic;
```

AXI 主侧则把五个通道的信号全部列出（[L46-L82](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi_if.vhd#L46-L82)）。注意读相关通道（AR、R）的输入端口都带了默认值 `:= '0'`，这是 VHDL 里「端口可悬空」的惯用写法，方便在不需要读时直接留空：

```vhdl
M_Axi_ArReady : in  std_logic := '0';
M_Axi_RData   : in  std_logic_vector(AxiDataWidth_g - 1 downto 0) := (others => '0');
M_Axi_RResp   : in  std_logic_vector(1 downto 0) := (others => '0');
M_Axi_RLast   : in  std_logic := '0';
M_Axi_RValid  : in  std_logic := '0';
```

这些默认值印证了「读通道在本模块里完全不参与工作」。

#### 4.1.4 代码实践

**实践目标**：把实体的「写专用」形状记牢，并验证读通道确实被挂死。

**操作步骤**：

1. 打开 [hdl/psi_ms_daq_axi_if.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi_if.vhd)，从 L46 到 L82 逐行扫描端口。
2. 用笔把端口分成三组：①AW（写地址）、②W（写数据）、③B（写响应）；再分两组：④AR（读地址）、⑤R（读数据）。
3. 对照 4.3 节的例化代码，看 AR、R 这两组端口最终被接到什么信号上。

**需要观察的现象**：AW/W/B 三组端口都接到了 `axi_master_full` 的对应输出/输入；AR、R 两组要么接 `(others => '0')`、要么接 `open`。

**预期结果**：你会确认本模块对外虽声明了「完整的 AXI 主端口」，但读通道是死端口，真正活动的只有写通道。

> 待本地验证：若你在 Modelsim 里把 `psi_ms_daq_axi_if` 单独例化做最小仿真，可观察到 `M_Axi_ArValid` 始终为 `'0'`、`M_Axi_RReady` 始终为 `'0'`。

#### 4.1.5 小练习与答案

**练习 1**：`IntDataWidth_g` 和 `AxiDataWidth_g` 都默认 64，但它们是两个独立 generic。请说出当 `AxiDataWidth_g = 128` 而 `IntDataWidth_g = 64` 时，模块内部会发生什么转换。

**参考答案**：`axi_master_full` 会做**宽度上转换**——把两个 64 位的内部字拼成一个 128 位的 AXI 拍发出去；命令里声明的 `Cmd_Size`（字节数）不变，但打到总线上的每拍数据变宽、拍数变少。

**练习 2**：端口 `M_Axi_RData` 的声明带 `:= (others => '0')`，而 `M_Axi_AwAddr` 没有。为什么？

**参考答案**：`M_Axi_RData`/`RResp`/`RLast`/`RValid`/`ArReady` 都是**输入**端口，给默认值是为了在顶层不连读通道时也能编译通过（悬空输入取默认 `'0'`）；`M_Axi_AwAddr` 是**输出**端口，输出端口由模块驱动、不需要默认值。

### 4.2 命令信息 FIFO：缓存「往哪写、写多少」

#### 4.2.1 概念说明

DMA 引擎发出一条命令时，`Cmd_Addr`（32 位目标地址）和 `Cmd_Size`（32 位字节数）是**同时**给出、且只在握手那一拍有效的。但 `axi_master_full` 不一定能在那一拍立刻开始执行——它可能正在处理上一条事务、或者数据还没到。如果命令只活一拍却没人接，就会丢命令。

解决办法是一个**命令信息 FIFO（`i_wrinfo_fifo`）**：把 32 位地址 + 32 位大小拼成一个 64 位的「信息字」存进 FIFO。命令到达即入队（`Cmd_Vld`/`Cmd_Rdy` 就是这个 FIFO 的写握手），执行端按自己的节奏从 FIFO 另一头取。这样命令与执行就被解耦了。

> 名词提示：`psi_common_sync_fifo` 是 PSI 通用库里的「同步 FIFO」——读写同一个时钟（本模块里所有逻辑都在 `M_Axi_Aclk` 这一个时钟域，所以用同步版本，跨时钟域已在输入逻辑那侧做完了）。`ram_style_g => "distributed"` 指明用分布式 RAM（查找表）实现，而不是块 RAM（BRAM）。

#### 4.2.2 核心流程

命令信息 FIFO 在数据流中的位置：

```
Cmd_Addr (32) ┐
              ├─> 拼成 64 位 InfoFifoIn ─> [i_wrinfo_fifo] ─> 64 位 InfoFifoOut ─> 拆回
Cmd_Size (32) ┘                                                              ├─ WrCmdFifo_Addr ─┐
                                                                             └─ WrCmdFifo_Size ─┴─> axi_master_full
```

关键点：

- **拼接**用 `subtype` 切片，地址放低位、大小放高位，拼出 64 位。
- **FIFO 深度**由 `MaxOpenCommands_g` 决定，即「最多允许多少条命令排队」。
- **读出后再拆开**成 `WrCmdFifo_Addr`/`WrCmdFifo_Size`，喂给 `axi_master_full` 的写命令口。
- FIFO 的读握手（`WrCmdFifo_Vld`/`WrCmdFifo_Rdy`）就是 `axi_master_full` 的 `cmd_wr_vld_i`/`cmd_wr_rdy_o`，由 master 自己控制节奏。

#### 4.2.3 源码精读

首先定义两个切片子类型，把 64 位信息字切成「地址段」和「大小段」（[psi_ms_daq_axi_if.vhd:91-95](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi_if.vhd#L91-L95)）：

```vhdl
subtype CommandAddrRng_c is natural range 31 downto 0;   -- 地址放 31..0
subtype CommandSizeRng_c is natural range 63 downto 32;  -- 大小放 63..32
constant WrCmdWidth_c : integer := CommandSizeRng_c'high + 1;  -- = 64
```

入口处把 `Cmd_Addr`/`Cmd_Size` 拼进 `InfoFifoIn`（[L106-L107](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi_if.vhd#L106-L107)）：

```vhdl
InfoFifoIn(CommandAddrRng_c) <= Cmd_Addr;
InfoFifoIn(CommandSizeRng_c) <= Cmd_Size;
```

FIFO 本体的例化（[L109-L124](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi_if.vhd#L109-L124)）——注意它的写口就是用户侧的 `Cmd_Vld`/`Cmd_Rdy`：

```vhdl
i_wrinfo_fifo : entity work.psi_common_sync_fifo
  generic map(
    width_g     => WrCmdWidth_c,          -- 64 位
    depth_g     => MaxOpenCommands_g,     -- 深度 = 最大在途命令数
    ram_style_g => "distributed"
  )
  port map(
    clk_i => Clk,
    rst_i => Rst,
    dat_i => InfoFifoIn,
    vld_i => Cmd_Vld,                     -- 用户侧命令写握手
    rdy_o => Cmd_Rdy,
    dat_o => InfoFifoOut,
    vld_o => WrCmdFifo_Vld,               -- master 侧命令读握手
    rdy_i => WrCmdFifo_Rdy
  );
```

出口处再把 64 位拆回两段（[L126-L127](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi_if.vhd#L126-L127)）：

```vhdl
WrCmdFifo_Addr <= InfoFifoOut(CommandAddrRng_c);
WrCmdFifo_Size <= InfoFifoOut(CommandSizeRng_c);
```

一个细节值得留意：这个 FIFO **只存地址和大小，不存数据**。数据是另一条线（`Dat_*`）直接进 `axi_master_full` 的。FIFO 队列保证「第 N 条命令」与「第 N 段数据」在 master 内部按到达顺序对齐。

#### 4.2.4 代码实践

**实践目标**：验证「命令信息 FIFO 只缓存地址+大小，数据另走一线」。

**操作步骤**：

1. 在 [hdl/psi_ms_daq_axi_if.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi_if.vhd) 中找到 `i_wrinfo_fifo`（L109）。
2. 确认 `dat_i` 的来源里**没有任何 `Dat_Data`**，只有 `Cmd_Addr`/`Cmd_Size`。
3. 再找到 4.3 节的 `axi_master_full` 例化，确认 `wr_dat_i` 直接接的是 `Dat_Data`（L159），不经过这个 FIFO。

**需要观察的现象**：命令路径上有 FIFO 缓冲，数据路径上是直连。

**预期结果**：两条线在 `axi_master_full` 内部才汇合。这意味着即使 `Cmd_Vld` 拉起来时数据尚未就绪，命令也会先安全地入队，不会丢失。

#### 4.2.5 小练习与答案

**练习 1**：假设 `MaxOpenCommands_g = 16`，DMA 引擎在数据还没被消费的情况下连续发了 17 条命令，第 17 条会发生什么？

**参考答案**：前 16 条进入 `i_wrinfo_fifo` 排队；第 17 条发出时 FIFO 已满，`Cmd_Rdy` 会被 FIFO 拉低，DMA 引擎的 `Mem_CmdRdy=0`，命令握手无法完成，DMA 引擎会**停在自己的 `Cmd_s` 状态**等待（参考 u2-l6：`Cmd_s` 在 `Mem_CmdRdy=1` 或无内存命令时才离开）。命令不会丢，只是被反压。

**练习 2**：为什么这个 FIFO 用 `"distributed"`（分布式 RAM）而不是块 RAM？

**参考答案**：FIFO 深度由 `MaxOpenCommands_g` 决定，在顶层实际取值是 `max(2, Streams_g)`（见 4.4.3），通常很小（个位数到几十）。小容量 FIFO 用查找表实现的分布式 RAM 更省资源；块 RAM 有固定的最小容量（如 36 Kb），给这么小的 FIFO 反而浪费。

### 4.3 `psi_common_axi_master_full` 例化：只写不读的取舍

#### 4.3.1 概念说明

真正把数据搬上 AXI 总线的是 PSI 通用库里的 `psi_common_axi_master_full`。它承担三件本模块自己不做的重活：

1. **宽度转换**：把 `IntDataWidth_g` 的内部字转成 `AxiDataWidth_g` 的 AXI 拍（可能上转换也可能下转换）。
2. **突发拆分**：一次用户命令的 `Size` 如果超过 `AxiMaxBeats_g` 能表达的拍数，自动拆成多个 AXI 突发。
3. **在途事务管理**：允许最多 `AxiMaxOpenTrasactions_g` 个事务同时在总线上未完成（outstanding），提高吞吐。

本模块通过两个 generic 开关告诉它「我只要写的部分」：

- `impl_write_g => true`：实现写通道（AW、W、B 及相关 FIFO、状态机）。
- `impl_read_g => false`：**不**实现读通道（AR、R）。读相关的输入端口在例化里全部接死（`cmd_rd_vld_i => '0'`、`rd_rdy_i => '0'`），输出端口接 `open`。

> 为什么本 IP 核只写不读？因为它是一个**数据采集**核——它的职责是把多路流数据**搬进** DDR，供 CPU（如 Zynq PS）经 CPU 自己的 AXI 端口去读。DAQ 路径上根本没有「从 DDR 读回数据」的需求。实现读通道只会白白增加 FIFO 和状态机，所以关掉。这就是代码实践任务第一问的答案。

#### 4.3.2 核心流程

`axi_master_full` 在本模块里的输入输出关系：

```
来自 i_wrinfo_fifo           来自用户数据线              输出到 AXI
────────────────────────────────────────────────────────────────────
cmd_wr_addr_i ─┐              wr_dat_i ──┐               m_axi_aw* (AW 通道)
cmd_wr_size_i ─┤              wr_vld_i ──┤               m_axi_w*  (W 通道，含 wlast)
cmd_wr_vld_i ──┤              wr_rdy_o ──┘               m_axi_b*  (B 通道，输入)
cmd_wr_rdy_o ──┘   低延迟模式: cmd_wr_low_lat_i='0'       ─────────────────────
                                                  wr_done_o ──> DoneI ─┐
                                                  wr_error_o ─> ErrorI ─┴─> Done
```

要点：

- **写命令口**直接接 FIFO 输出（`WrCmdFifo_*`），节奏由 master 控制。
- **写数据口**直接接用户 `Dat_*`，不经过任何 FIFO（master 内部有自己的数据 FIFO，深度 `DataFifoDepth_g`）。
- **`cmd_wr_low_lat_i => '0'`** 表示关掉低延迟模式（用默认吞吐优先的行为）。
- **响应**从 master 的 `wr_done_o`（正常完成）和 `wr_error_o`（B 通道报错）取出。

#### 4.3.3 源码精读

例化的 generic 部分清楚展示了「只写不读」（[psi_ms_daq_axi_if.vhd:129-141](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi_if.vhd#L129-L141)）：

```vhdl
i_axi : entity work.psi_common_axi_master_full
  generic map(
    axi_addr_width_g             => 32,
    axi_data_width_g             => AxiDataWidth_g,
    axi_max_beats_g              => AxiMaxBeats_g,
    axi_max_open_trasactions_g   => AxiMaxOpenTrasactions_g,
    user_transaction_size_bits_g => 32,            -- 用户 Size 字段 32 位
    data_fifo_depth_g            => DataFifoDepth_g,
    data_width_g                 => IntDataWidth_g,
    impl_read_g                  => false,         -- 关键：不实现读
    impl_write_g                 => true,          -- 关键：只实现写
    ram_behavior_g               => RamBehavior_g
  )
```

端口映射里，写命令、写数据、写响应都正常连，读相关全部挂死（[L142-L207](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi_if.vhd#L142-L207)）。摘录关键几段：

写命令（来自 FIFO）：

```vhdl
cmd_wr_addr_i    => WrCmdFifo_Addr,
cmd_wr_size_i    => WrCmdFifo_Size,
cmd_wr_low_lat_i => '0',
cmd_wr_vld_i     => WrCmdFifo_Vld,
cmd_wr_rdy_o     => WrCmdFifo_Rdy,
```

写数据（来自用户直连）：

```vhdl
wr_dat_i         => Dat_Data,
wr_vld_i         => Dat_Vld,
wr_rdy_o         => Dat_Rdy,
```

读命令与读数据全部接死或留空（[L152-L157](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi_if.vhd#L152-L157)、[L162-L165](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi_if.vhd#L162-L165)）：

```vhdl
cmd_rd_addr_i    => (others => '0'),
cmd_rd_size_o    => (others => '0'),
cmd_rd_low_lat_i => '0',
cmd_rd_vld_i     => '0',
cmd_rd_rdy_o     => open,
...
rd_dat_o         => open,
rd_vld_o         => open,
rd_rdy_i         => '0',
```

写响应——这是 `Done` 的真正来源（[L166-L170](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi_if.vhd#L166-L170)）：

```vhdl
wr_done_o        => DoneI,
wr_error_o       => ErrorI,
rd_done_o        => open,
rd_error_o       => open,
```

后面 AW/W/B/AR/R 五个 AXI 通道的端口一一对应连出（[L171-L206](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi_if.vhd#L171-L206)）。AR/R 虽然也连了，但因为 `impl_read_g=false`，master 内部根本不驱动 `m_axi_arvalid`/`m_axi_rready`，这些连线实际上没有有效数据。

#### 4.3.4 代码实践

**实践目标**：亲手确认读通道被「裁掉」、写通道被「保留」。

**操作步骤**：

1. 打开 [hdl/psi_ms_daq_axi_if.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi_if.vhd)，定位 `i_axi` 例化（L129）。
2. 在 generic map 里找到 `impl_read_g` 和 `impl_write_g` 两行，记下它们的值。
3. 在 port map 里数一数：`cmd_rd_*` 和 `rd_*` 端口分别接了什么；`cmd_wr_*` 和 `wr_*` 端口分别接了什么。

**需要观察的现象**：读侧端口要么是 `(others => '0')`、要么是 `'0'`、要么是 `open`；写侧端口全部接到本模块的有效信号（`WrCmdFifo_*`、`Dat_*`、`DoneI`、`ErrorI`、`M_Axi_*`）。

**预期结果**：从 RTL 代码层面证实「本模块是写专用接口」，`impl_read_g => false` 不是一句空话——它对应着 master 内部整块读逻辑被综合工具删除。

#### 4.3.5 小练习与答案

**练习 1**：如果某天想把本模块改造成「也能从 DDR 读数据回灌」，最少要改哪几处？

**参考答案**：①generic 把 `impl_read_g` 改成 `true`；②在 `cmd_rd_*` 端口接上真实的读命令源（地址+大小+握手）；③在 `rd_dat_o`/`rd_vld_o`/`rd_rdy_i` 接上真实的读数据接收端；④实体的 AR、R 通道端口要从「带默认值」改成「真正连接外部 AXI 从机」。但这与本 IP 核「只采集、只写」的定位相悖，实际不会这么做。

**练习 2**：`cmd_wr_low_lat_i => '0'` 这一行如果改成 `'1'`，从名字推测会发生什么？

**参考答案**：从命名「low latency（低延迟）」推测，置 `'1'` 会让 master 在收到写命令后以更低延迟（更少缓冲拍数）发起 AXI 写地址，代价可能是吞吐略降或对反压更敏感。本模块选 `'0'` 用默认行为。**具体行为细节属于 `psi_common_axi_master_full` 内部，待本地验证或查阅 psi_common 文档确认。**

### 4.4 `Done` 的合成与它在 `TfDone`/IRQ 链路里的位置

#### 4.4.1 概念说明

`axi_master_full` 用两个信号回报一次写事务的结局：`wr_done_o`（正常完成，B 通道返回 OKAY）和 `wr_error_o`（出错，B 通道返回 SLVERR/DECERR 之类）。本模块用一行代码把两者合成对外的单一脉冲 `Done`：

```vhdl
Done <= DoneI or ErrorI;
```

这是一个**有意的合并**：从上层（DMA 引擎、状态机）的视角看，无论这次 DDR 写是成功还是失败，事务都「结束了」，状态机都应该往下推进（推进窗口指针、可能产生中断）。把成功与错误合并成 `Done`，意味着**本 IP 核不把 DDR 写错误当作可恢复事件单独上报**，而是当作「这次传输完成」处理。

> 设计观察（非贬义）：`ErrorI` 被 `or` 进 `Done` 后，错误信息本身在本模块内**没有单独导出**。若系统需要感知 DDR 写错误，需要从别的途径（例如外部 AXI 互联的监控）获取。本讲只陈述这一事实，不臆断其在具体部署中的处理方式。

`Done` 出了本模块后，在顶层被改名为 `MemSm_Done`，并接到控制状态机的 `TfDone` 输入。`TfDone` 是「transfer done（传输完成）」的缩写，是 IRQ（中断）生成链路的起点之一：每来一个 `TfDone` 脉冲，状态机就把它计数进 `TfDoneCnt`，再配合 IRQ FIFO 出队来生成对 CPU 的中断。这条链路的完整展开是 u4-l1 的主题，本讲只定位 `Done` 在其中的位置。

#### 4.4.2 核心流程

`Done` 脉冲从中产生到驱动中断的链路：

```
axi_master_full 收到 B 通道响应
        │
        ├─ 正常  ─> wr_done_o  = DoneI ─┐
        └─ 出错  ─> wr_error_o = ErrorI ┴─> (DoneI or ErrorI) ─> Done (本模块对外)
                                                                          │
                                                            顶层改名 MemSm_Done
                                                                          │
                                                          接到状态机 TfDone 输入
                                                                          │
                                              TfDoneReg 寄存一拍 ─> TfDoneCnt++
                                                                          │
                                              TfDoneCnt>0 且 IRQ FIFO 非空 ─> 出队 ─> StrIrq/StrLastWin ─> Irq
```

要点：

- **`Done` 是单拍脉冲**，由 `wr_done_o`/`wr_error_o` 的脉冲性质决定。
- **合并后错误被「吞」进完成信号**，状态机不再区分。
- **`TfDone` 不是直接产生中断**，而是先计入 `TfDoneCnt`，再与 IRQ FIFO 配合（见下文源码引用）。

#### 4.4.3 源码精读

`Done` 的合成就在架构体末尾（[psi_ms_daq_axi_if.vhd:209](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi_if.vhd#L209)）：

```vhdl
Done <= DoneI or ErrorI;
```

顶层的例化把本模块的 `Done` 接成 `MemSm_Done`（[psi_ms_daq_axi.vhd:445](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L445)）：

```vhdl
Done          => MemSm_Done,
```

而 `MemSm_Done` 又被接到状态机的 `TfDone` 输入（[psi_ms_daq_axi.vhd:384](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L384)）：

```vhdl
TfDone       => MemSm_Done,
```

状态机内部，`TfDone` 首先被寄存一拍成 `TfDoneReg`（[hdl/psi_ms_daq_daq_sm.vhd:244](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L244)）：

```vhdl
v.TfDoneReg     := TfDone;
```

然后在每拍的处理逻辑里，若上一拍 `TfDoneReg=1` 就把 `TfDoneCnt` 加 1，并在这个计数非零、IRQ FIFO 也不空时出队一个 IRQ（[L571-L578](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L571-L578)）：

```vhdl
if r.TfDoneReg = '1' then
  v.TfDoneCnt := std_logic_vector(unsigned(r.TfDoneCnt) + 1);
end if;
...
if (unsigned(r.TfDoneCnt) /= 0) and (IrqFifoEmpty = '0') then
  ...  -- 从 IRQ FIFO 出队，生成 StrIrq/StrLastWin
  v.TfDoneCnt := std_logic_vector(unsigned(v.TfDoneCnt) - 1);
end if;
```

> 这就是为什么说 `Done` 是「中断链路的起点之一」：它驱动 `TfDoneCnt`，而 `TfDoneCnt` 又是 IRQ FIFO 出队的「节拍器」——保证每完成一次传输、IRQ FIFO 里也确实有一条记录时，才真正向 CPU 发一个中断。完整的中断机制（含 IrqFifo、丢失中断修复）在 u4-l1 展开。

**顺带一提：顶层 Cache/Prot 信号的「绕过」**。在顶层例化里，`axi_if` 的 `M_Axi_AwCache`/`M_Axi_AwProt`/`M_Axi_ArCache`/`M_Axi_ArProt` 四个端口被留成 `open`（[psi_ms_daq_axi.vhd:451-452](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L451-L452) 与 [L468-L469](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L468-L469)）：

```vhdl
M_Axi_AwCache => open, --M_Axi_AwCache
M_Axi_AwProt  => open, --M_Axi_AwProt
...
M_Axi_ArCache => open, --M_Axi_ArCache
M_Axi_ArProt  => open, --M_Axi_ArProt
```

取而代之的是，顶层真正的 `M_Axi_AwCache`/`AwProt`/`ArCache`/`ArProt` 输出由一个 `sync_apc_reg` 进程驱动（[L211-L226](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L211-L226)），其源头是寄存器接口 `i_reg` 输出的 `AWCache(0)`/`AWProt(0)` 等（即 ACPCFG 寄存器，经两拍同步后驱动）。这说明 AXI 写地址通道的 Cache/Prot **不**由 `axi_if`/`master_full` 控制，而是由顶层独立、可软件配置地驱动。这是 feature/se32 引入的增强，完整讲解属于 u4-l4，本讲只标注这一连线事实，避免读者误以为 `axi_if` 端口表里的 `M_Axi_AwCache` 就是最终输出。

### 4.4.4 代码实践

**实践目标**：沿着 `Done` 这条线，确认它确实通向中断链路；并看清顶层 `max(2, Streams_g)` 这个取值的用意。

**操作步骤**：

1. 在 [hdl/psi_ms_daq_axi_if.vhd:209](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi_if.vhd#L209) 看到 `Done <= DoneI or ErrorI`。
2. 跳到顶层 [hdl/psi_ms_daq_axi.vhd:445](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L445)，确认 `Done => MemSm_Done`。
3. 再跳到 [L384](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L384)，确认 `TfDone => MemSm_Done`。
4. 打开 [hdl/psi_ms_daq_daq_sm.vhd](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd)，看 L244 与 L571-L578，确认 `TfDone` 如何变成 `TfDoneCnt` 递增与 IRQ FIFO 出队。
5. 回到顶层 [L424-L430](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L424-L430)，读 `i_memif` 的 generic 映射里 `MaxOpenCommands_g => max(2, Streams_g)` 这一行及其注释。

顶层那段注释原文是（[L430](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L430)）：

```vhdl
MaxOpenCommands_g => max(2, Streams_g), -- ISE tools implement memory as FFs for one stream.
                     -- Reason is unkown, so we always implement two streams for resource
                     -- optimization reasons.
```

**需要观察的现象**：`Done` 单线贯穿 `axi_if → MemSm_Done → TfDone → TfDoneCnt → IRQ FIFO 出队 → StrIrq`。`MaxOpenCommands_g` 取了 `max(2, Streams_g)`。

**预期结果**：

- 关于 `Done`：它就是「一次 DDR 写事务收尾」的事件，状态机用它来计数完成次数、节拍 IRQ 出队。
- 关于 `max(2, Streams_g)` 的用意，结合源码注释可以得出两层含义：
  1. **下限 2**：老版本 Xilinx ISE 综合工具在「命令信息 FIFO 深度为 1（单流）」时，会把本应用 RAM 实现的存储误综合成触发器（FF），浪费资源。注释坦承「原因不明」，于是用 `max(2, ...)` 强制深度至少为 2，规避这个综合怪象、让工具正确推断 RAM。
  2. **取 `Streams_g`**：命令信息 FIFO 的深度至少要等于流数，因为状态机可能为多个流各下发一条在途命令（u2-l5 已讲「每流最多一条在途命令」），FIFO 得为每路至少留一格。
  
  两者取 `max`，同时满足「≥2（规避综合问题）」与「≥Streams_g（容量够用）」。

> 注意：注释里的 `max(2, Streams_g)` 修正了 FIFO 深度，与 `axi_master_full` 内部的 `AxiMaxOpenTrasactions_g`（在途事务数，顶层取 `AxiMaxOpenTrasactions_g`，默认 8）是**两个不同的容量参数**——前者管命令信息 FIFO 的排队深度，后者管 AXI 总线上同时未完成的事务数。不要混淆。

#### 4.4.5 小练习与答案

**练习 1**：如果 `axi_master_full` 的 `wr_error_o` 在某拍拉了一个脉冲（DDR 写返回了错误响应），上层状态机会怎么反应？

**参考答案**：因为 `Done <= DoneI or ErrorI`，`ErrorI` 的脉冲会令 `Done` 也产生一个脉冲，进而变成 `TfDone`。状态机不区分这次是成功还是失败，照样 `TfDoneCnt++`、照样可能出队 IRQ。也就是说，**DDR 写错误在本 IP 核内被当作「传输完成」处理**，不会被单独上报或重试。

**练习 2**：为什么 `TfDone` 要先寄存一拍成 `TfDoneReg`，再据 `TfDoneReg` 去递增 `TfDoneCnt`，而不是直接用 `TfDone` 递增？

**参考答案**：把外部进来的脉冲先打一拍寄存，是常见的「边沿/脉冲同步与去毛刺」做法——`TfDone` 来自另一个模块（`axi_if`），虽然同在 `M_Axi_Aclk` 时钟域，但先寄存一拍可以保证 `TfDoneCnt` 的递增逻辑用的是稳定的、本状态机时序域内的信号，降低组合路径深度与时序约束难度。从 L244（`v.TfDoneReg := TfDone`）和 L571（`if r.TfDoneReg = '1'`）可见，递增用的是 `r.TfDoneReg`（已寄存的值）。

**练习 3**：顶层把 `M_Axi_AwCache => open`，那写到 DDR 的实际 Cache 属性由谁决定？

**参考答案**：由顶层的 `sync_apc_reg` 进程驱动（来自寄存器接口的 ACPCFG 寄存器，经两拍同步）。`axi_if` 内部 `master_full` 默认输出的 Cache 值被丢弃（端口悬空）。所以软件可以通过 ACPCFG 寄存器配置 DDR 写的缓存行为（如 Write-Through/Write-Back/Non-cacheable）。**完整机制在 u4-l4 讲解。**

## 5. 综合实践

把本讲四个模块串起来，完成代码实践任务里的「追踪一次 512 字节突发」。

**场景设定**：默认 generic（`IntDataWidth_g = 64`、`AxiDataWidth_g = 64`、`AxiMaxBeats_g = 256`），DMA 引擎对某一流发了一条 `Cmd_Addr = 0x0000_1000`、`Cmd_Size = 512`（字节）的写命令，随后 512 字节样本数据连续到达。

**任务**：画出从 `DmaMem_CmdVld` 到 `Done` 的完整信号路径，标注每一站发生的事，并回答三个问题。

**操作步骤**：

1. **命令入队**。在顶层 [hdl/psi_ms_daq_axi.vhd:438-441](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L438-L441) 看到 `DmaMem_CmdAddr/Size/Vld/Rdy` 连到 `axi_if` 的 `Cmd_*`。进 `axi_if` 后，`Cmd_Addr`/`Cmd_Size` 被拼成 64 位 `InfoFifoIn` 写入 `i_wrinfo_fifo`（[axi_if L106-L124](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi_if.vhd#L106-L124)）。`Cmd_Vld` 就是 FIFO 写口 `vld_i`。
2. **命令出队**。`axi_master_full` 准备好时拉 `cmd_wr_rdy_o`（即 `WrCmdFifo_Rdy`），FIFO 吐出 64 位 `InfoFifoOut`，拆回 `WrCmdFifo_Addr=0x0000_1000`、`WrCmdFifo_Size=512`（[L126-L151](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi_if.vhd#L126-L151)）。
3. **数据直连**。512 字节样本经 `Dat_Data`/`Dat_Vld` 直接连到 master 的 `wr_dat_i`/`wr_vld_i`（[L159-L161](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi_if.vhd#L159-L161)），不经过命令 FIFO。
4. **AXI 写事务**。master 因 `AxiDataWidth_g=IntDataWidth_g=64`，无需宽度转换；512 字节 ÷ 8 字节/拍 = 64 拍，`AxiMaxBeats_g=256` 足够，故**单次突发**完成：`M_Axi_AwAddr=0x0000_1000`、`M_Axi_AwLen=63`（64 拍减 1）、`M_Axi_WValid` 连续 64 拍、最后一拍 `M_Axi_WLast=1`（[L171-L186](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi_if.vhd#L171-L186)）。
5. **响应与完成**。DDR 控制器回 B 通道：`M_Axi_BValid=1`、`M_Axi_BResp=OKAY`（[L187-L190](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi_if.vhd#L187-L190)）。master 据此拉 `wr_done_o=DoneI` 一拍（[L167](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi_if.vhd#L167)）。
6. **合成 Done**。`Done <= DoneI or ErrorI`（[L209](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi_if.vhd#L209)）输出一拍 `Done`，顶层改名 `MemSm_Done`，接到状态机 `TfDone`（[顶层 L384/L445](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_axi.vhd#L384)），最终使 `TfDoneCnt` 加 1（[daq_sm L571-L572](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/hdl/psi_ms_daq_daq_sm.vhd#L571-L572)）。

**回答三个问题**：

- **为什么 `psi_ms_daq_axi_if` 只实现写通道？**——因为本 IP 核是数据采集核，只把流数据写进 DDR，从不从 DDR 读回；CPU 经自己的 AXI 端口读 DDR。读通道无用，故 `impl_read_g=false` 省掉读侧 FIFO 与状态机。
- **512 字节突发的拍数与 `AwLen`？**——64 拍、`AwLen=63`（默认位宽下，一次突发即可，无需拆分）。
- **`MaxOpenCommands_g => max(2, Streams_g)` 的用意？**——①保证命令信息 FIFO 深度 ≥ 流数（每路在途命令有格可放）；②下限 2 规避老版 ISE 把深度为 1 的 FIFO 误综合成触发器的资源浪费（源码注释明示）。

**预期结果**：你能把上面六步画成一张单线的信号流程图，并指出「数据线」与「命令线」在 `axi_master_full` 内部才汇合、`Done` 在 B 通道响应后才产生、`Done` 又是 IRQ 链路的节拍起点。

> 待本地验证：上述拍数与 `AwLen` 的具体值，可在顶层 testbench [tb/psi_ms_daq_axi](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_axi/psi_ms_daq_axi_tb.vhd) 的波形里直接观测 `M_Axi_AwLen`、`M_Axi_WLast`、`M_Axi_BValid` 与 `Done` 的时序关系来确认。`psi_ms_daq_axi_if` 没有独立 testbench，其写行为通过顶层 TB 的共享字节数组 `Memory`（[psi_ms_daq_axi_tb_pkg.vhd:32-33](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/tb/psi_ms_daq_axi/psi_ms_daq_axi_tb_pkg.vhd#L32-L33)）端到端校验。

## 6. 本讲小结

- `psi_ms_daq_axi_if` 是 DMA 引擎与 DDR 之间的**写专用封装层**：把「地址+大小命令」和「数据流」两条独立握手，合并成一组 AXI4 写事务（AW/W/B），完成后回送一个 `Done` 脉冲。
- `i_wrinfo_fifo` 把 32 位 `Cmd_Addr` + 32 位 `Cmd_Size` 拼成 64 位信息字排队（`psi_common_sync_fifo`，深度 `MaxOpenCommands_g`，分布式 RAM），解耦命令到达与执行；**数据不进这个 FIFO**，直连 master。
- 本模块例化 `psi_common_axi_master_full` 时 `impl_write_g=true`、`impl_read_g=false`——只保留写通道（含宽度转换、突发拆分、在途事务管理），裁掉读通道，符合「采集核只写 DDR」的定位。
- `Done <= DoneI or ErrorI` 把正常完成与 DDR 写错误合并成同一脉冲：本 IP 核不单独上报写错误，而是把它当作「传输完成」交给上层推进。
- `Done` 经顶层改名 `MemSm_Done`、接到状态机 `TfDone`，驱动 `TfDoneCnt` 递增并与 IRQ FIFO 出队配合生成中断——`Done` 是 IRQ 链路的节拍起点（完整机制见 u4-l1）。
- 顶层 `MaxOpenCommands_g => max(2, Streams_g)`：既保证 FIFO 深度 ≥ 流数，又用下限 2 规避老版 ISE 把深度 1 的 FIFO 误综合成触发器的资源浪费（源码注释明示，原因彼时未明）。

## 7. 下一步学习建议

到这里，**第二单元「数据通路核心模块」已经走完整条主线**：u2-l1 的公共类型 → u2-l2~u2-l4 的输入逻辑 → u2-l5~u2-l6 的 DMA 引擎 → 本讲 u2-l7 的 AXI 主接口。一条样本从 `Str_Data` 进入、经过触发/超时/拼字/DMA，最终落到 DDR，全链路已经讲完。

接下来建议进入**第三单元「控制状态机与上下文存储」**，从 [u3-l1 控制状态机总览与优先级仲裁](psi_multi_stream_daq-tutorial/u3-l1-sm-overview-arbitration.md) 开始。状态机是整个 IP 的「大脑」，它决定「什么时候、给哪一路、发多大的 DMA 命令」——也就是本讲里 `Cmd_Addr`/`Cmd_Size` 的真正来源。

如果你想先放深一层、专门研究本讲末尾提到的两个「绕过/增强」细节，可以跳到第四单元：[u4-l1 中断生成机制与 IRQ FIFO](psi_multi_stream_daq-tutorial/u4-l1-irq-generation-fifo.md)（展开 `TfDone`→`TfDoneCnt`→IRQ FIFO 的完整链路与丢失中断修复）、以及 [u4-l4 AXI 缓存控制寄存器与可参数化内部数据宽度](psi_multi_stream_daq-tutorial/u4-l4-axi-cache-intdatawidth.md)（展开本讲提到的 `ACPCFG`/`sync_apc_reg` 与 `IntDataWidth_g` generic 化）。

继续阅读源码时，推荐结合 `psi_common` 库里 `psi_common_axi_master_full` 和 `psi_common_sync_fifo` 的实现，把本讲当作「封装层」往下再挖一层。
