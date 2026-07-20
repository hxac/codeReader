# 输出模式与 FIFO/RegTable 存储

## 1. 本讲目标

本讲聚焦 `vivadoIP_axi_mm_reader` 核心 RTL 内部的两块存储资源，以及它们如何被两种输出模式复用。学完后你应当能够：

- 说清**读数据 FIFO**（`psi_common_sync_fifo`）的宽度为何是 `32+1`、深度为何是 `MaxRegCount_g*MinBuffers_g`，以及第 33 位 `Last` 是如何伴随数据进出 FIFO 的。
- 说清**配置表 RegTable** 为何用一块**双端口 RAM**（`psi_common_tdp_ram`）实现，A 口服务软件配置、B 口服务核心 FSM 遍历。
- 解释 wrapper 中 `g_axis` / `g_naxis` 两个 `generate` 块如何用**条件综合**在 AXIS（直出 `m_axis`）与 AXIMM（映射到 `RdData`/`RdLast` 寄存器）之间切换，并理解两种模式下 `AxiS_Rdy` 的不同接法。

本讲承接 u2-l2（寄存器映射）与 u2-l3（核心 FSM），把「软件视角的寄存器」与「FSM 的遍历动作」落到具体的存储元件上。

## 2. 前置知识

- **FIFO（先进先出队列）**：一种存储结构，数据按写入顺序读出。本 IP 用它做读回值的缓冲，让「硬件读寄存器」和「下游取数据」解耦。
- **AXI-Stream 握手**：`Vld`（发送方声明数据有效）+ `Rdy`（接收方声明准备好）同时为高时，一拍数据完成传递；`Last` 标记一帧的最后一个字。
- **双端口 RAM（True Dual-Port RAM, TDP RAM）**：有两个独立的访问口（A/B），各自带地址、读写控制与数据线，可同时被两端访问。本 IP 用一端给软件、一端给硬件核心。
- **`generate` 语句（条件综合）**：VHDL 在**综合阶段**根据 generic（常量）选择性地编译某段电路。条件不满足的块不会生成任何硬件——这是「同一份 RTL 派生出两种 IP」的关键。
- **RV（带副作用读）**：u2-l2 引入的概念，读某个寄存器会触发额外动作（这里是「弹出 FIFO 一项」）。

## 3. 本讲源码地图

| 文件 | 角色 |
|:-----|:-----|
| [hdl/axi_mm_reader.vhd](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd) | 纯逻辑核心。实例化 FIFO（`i_rdfifo`）与 RegTable RAM（`i_ram`），产生 `Last` 与 FIFO 写入节奏。 |
| [hdl/axi_mm_reader_wrp.vhd](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd) | AXI 边界 wrapper。`g_axis`/`g_naxis` 两个 generate 块在这里把核心的 `AxiS_*` 接到 `m_axis` 或映射到寄存器。 |
| [hdl/definitions_pkg.vhd](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/definitions_pkg.vhd) | 寄存器/位索引常量（`RegIdx_RdData_c` 等），generate 块按名字引用它们。 |
| [tb/top_tb.vhd](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd) | 自校验测试台。`CheckResultsAxiS`/`CheckResultsAxiMM` 两个过程演示两种模式下取数顺序的差别。 |

## 4. 核心概念与源码讲解

### 4.1 读数据 FIFO：psi_common_sync_fifo（含 Last 位）

#### 4.1.1 概念说明

核心经 `m00_axi` 把一批 32 位寄存器逐个读回后，结果不能「直接」交给下游，原因有三：

1. **节奏不一致**：AXI 主机读回的速度与下游（AXI-Stream 接收方或软件）取数的速度未必相同。
2. **软件抖动**：若下游是软件（Linux 等），响应延迟大且不确定，硬件必须能把多个读周期的数据暂存起来，等软件有空再取，否则会丢数据。
3. **包边界**：一次读周期产出一「包」数据，下游需要知道每一包在哪里结束。

因此核心在数据通路上放了一块**同步 FIFO** `psi_common_sync_fifo`，既做缓冲、又顺便携带包尾标记。这块 FIFO 是「输出模式无关」的——无论 AXIS 还是 AXIMM，读回值都先进同一块 FIFO，差异只在「FIFO 的输出口接到哪里」。

#### 4.1.2 核心流程

数据流入侧（核心 → FIFO）：

- AXI 主机回流的每拍 `AxiM_RdDat_Data`（32 位）+ 一拍 `Last` 标志拼成 33 位写入 FIFO。
- 写入仅在 `AxiM_RdDat_Vld=1` 且 `Fifo_Rdy=1`（FIFO 未满）时发生。
- `Fifo_Rdy` 同时回灌给 AXI 主机做背压（`AxiM_RdDat_Rdy <= Fifo_Rdy`）。

数据读出侧（FIFO → 下游）：

- `OutVld=1` 表示 FIFO 有数据待取；下游拉高 `OutRdy`（即 `AxiS_Rdy`）取走一拍。
- 第 33 位 `OutData(32)` 复现当初写入的 `Last`，作为 `AxiS_Last` 输出。
- `OutLevel` 实时反映 FIFO 内的字数，接到 `AxiS_Level` 供水位查询。

`Last` 的产生时机是关键：组合进程在「当前正在被推入 FIFO 的那一拍恰好是本周期最后一个字」时拉高 `Last`，使标记与正确的数据字粘在一起进队列。

FIFO 深度由两个 generic 相乘决定：

\[
\text{Depth} = \text{MaxRegCount\_g} \times \text{MinBuffers\_g}
\]

默认配置 \(1024 \times 4 = 4096\) 个字，即最多缓冲 4 个满读周期。

#### 4.1.3 源码精读

FIFO 实例化在核心文件 [hdl/axi_mm_reader.vhd:226-247](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L226-L247)，关键在宽度与端口映射：

```vhdl
i_rdfifo : entity work.psi_common_sync_fifo
    generic map (
        Width_g  => 32+1,                          -- 32 位数据 + 1 位 Last
        Depth_g  => MaxRegCount_g*MinBuffers_g,    -- 可缓冲的读周期数
        ...
    )
    port map (
        InData(31 downto 0)  => AxiM_RdDat_Data,   -- 数据
        InData(32)           => Last,              -- 第 33 位 = 包尾标记
        InVld                => AxiM_RdDat_Vld,
        InRdy                => Fifo_Rdy,          -- 背压回灌
        OutData(31 downto 0) => AxiS_Data,
        OutData(32)          => AxiS_Last,         -- 标记随数据原样复出
        OutVld               => AxiS_Vld,
        OutRdy               => AxiS_Rdy,
        OutLevel             => AxiS_Level(log2ceil(MaxRegCount_g*MinBuffers_g) downto 0)
    );
```

- `Width_g => 32+1`：故意多 1 位来「夹带」`Last`，避免单独再开一条副作用通路。
- `InData(32) => Last` / `OutData(32) => AxiS_Last`：写入时把 `Last` 放进第 33 位，读出时从第 33 位取出——标记与数据同进同出、保序。
- `InRdy => Fifo_Rdy`：FIFO 满时 `Fifo_Rdy=0`，该信号在 [hdl/axi_mm_reader.vhd:182](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L182) 被赋给 `AxiM_RdDat_Rdy`，向 AXI 主机施加背压。

`Last` 的产生在组合进程里，[hdl/axi_mm_reader.vhd:168-172](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L168-L172)：

```vhdl
Last <= '0';
if r.DoneCnt = r.RegCount-1 then
    Last <= '1';
end if;
```

而 `DoneCnt` 的自增条件 [hdl/axi_mm_reader.vhd:164-166](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L164-L166) 是 `(AxiM_RdDat_Vld='1') and (Fifo_Rdy='1')`——**恰好是数据真正被推进 FIFO 的那一拍**。因此 `Last` 高电平与「最后一字的写入」发生在同一拍，标记不会错位。

`AxiS_Level` 的高位补零在 [hdl/axi_mm_reader.vhd:225](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L225)：FIFO 的 `OutLevel` 只提供 \(\log_2\lceil\text{Depth}\rceil+1\) 位，而 `AxiS_Level` 是 32 位寄存器，剩余高位必须显式置 0 才不会悬空。

#### 4.1.4 代码实践

**实践目标**：理解 FIFO 第 33 位 `Last` 的「写入—存储—读出」全程对应关系。

**操作步骤**：

1. 打开 [hdl/axi_mm_reader.vhd:226-247](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L226-L247)，找到 `i_rdfifo`。
2. 对照 [hdl/axi_mm_reader.vhd:164-172](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L164-L172) 的 `DoneCnt` 自增与 `Last` 产生。
3. 在纸上画一个 4 字读周期（`RegCount=4`），逐拍列出 `DoneCnt`、`AxiM_RdDat_Data`、`Last`、FIFO 内容的演化。

**需要观察的现象**：

- `DoneCnt` 从 0 涨到 3，每涨一次恰有一字进 FIFO。
- `Last` 仅在 `DoneCnt=3`（即 `RegCount-1`）那一拍为 1，且该拍就是第 4 个字入 FIFO 的拍。
- 4 字全部入队后，FIFO 里只有最后一个字携带着 `Last=1`。

**预期结果**：读出端会按写入顺序得到 4 个字，只有第 4 个字伴随 `AxiS_Last=1`。这正是「包边界」的定义。结论可表述为：

> `InData(32)=Last` 在写入侧把「本字是否包尾」烧进 FIFO；`OutData(32)=AxiS_Last` 在读出侧原样复现。二者通过同一块 FIFO 存储保序对应，使包边界跨过缓冲仍不丢失。

> 待本地验证：可仿真 `tb/top_tb.vhd`，在波形上观察 `DoneCnt`、`Last`、`AxiS_Last` 三者的对齐关系。

#### 4.1.5 小练习与答案

**练习 1**：若把 `Width_g` 改成 32（去掉 Last 位）、并把 `Last` 改为单独一条输出线直连 `AxiS_Last`，会出什么问题？

**答案**：`Last` 与数据不再共享同一存储，FIFO 的缓冲与背压会让二者错位——例如数据被背压延后一拍入队，而 `Last` 仍按原拍发出，导致下游误判包尾。把 `Last` 放进 FIFO 数据位内才能保证标记随正确的字一起排队、一起出队。

**练习 2**：默认配置下（`MaxRegCount_g=1024`、`MinBuffers_g=4`），FIFO 容量是多少字？`OutLevel` 占几位？

**答案**：\(1024 \times 4 = 4096\) 字。表示 0..4096 需要 \(\log_2\lceil 4096\rceil+1 = 12+1 = 13\) 位，即 `OutLevel` 占 13 位（对应 `AxiS_Level(12 downto 0)`，高位补 0）。

---

### 4.2 RegTable 存储：psi_common_tdp_ram 双端口 RAM

#### 4.2.1 概念说明

RegTable（即软件视角的 `Addr[]` 配置表，见 u2-l2）存放「本周期要读哪些寄存器地址」。它有两个互不相干的访问者：

- **软件**（经 `s00_axi` 从机）：在 IP 禁用时写入这张表，配置每个槽位的 32 位目标地址。
- **硬件核心**（FSM）：在读周期中按顺序读取这张表，拿到地址后发给 AXI 主机。

用一个**单端口** RAM 会迫使两端分时复用、互相阻塞；用一块 **双端口 RAM**（`psi_common_tdp_ram`）则让两端各自独占一个口，互不干扰，这是最自然的选择。

#### 4.2.2 核心流程

- **A 口（软件侧）**：`AddrA=RegCfg_Idx`（来自 wrapper 解码后的内存地址），`WrA=RegCfg_Wr`（写使能），`DinA=RegCfg_WrReg`（待写地址值），`DoutA=RegCfg_RdReg`（软件回读）。
- **B 口（核心侧）**：`AddrB=r.RamAddr`（FSM 的读指针，见 u2-l3），`WrB='0'`（**只读**），`DoutB=RamRegAddr`（读出的地址，喂给命令通道）。
- 两口共用同一时钟 `Clk`（同步双端口）。
- RAM 有**一拍读延迟**：B 口地址变化后，下一拍 `RamRegAddr` 才更新。FSM 因此在 `ReadAddr_s` 提前自增 `RamAddr` 做预读（详见 u2-l3）。

#### 4.2.3 源码精读

双端口 RAM 实例化在 [hdl/axi_mm_reader.vhd:206-223](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L206-L223)：

```vhdl
i_ram : entity work.psi_common_tdp_ram
    generic map (
        Depth_g     => MaxRegCount_g,      -- 表最大项数
        Width_g     => AxiAddrWidth_g,     -- 每项 32 位地址
        Behavior_g  => RamBehavior_g
    )
    port map (
        ClkA   => Clk,  AddrA => RegCfg_Idx,  WrA => RegCfg_Wr,
        DinA   => RegCfg_WrReg,  DoutA => RegCfg_RdReg,        -- A 口：软件读写
        ClkB   => Clk,  AddrB => r.RamAddr,  WrB => '0',        -- B 口：核心只读
        DinB   => (others => '0'),  DoutB => RamRegAddr
    );
```

要点：

- `Depth_g => MaxRegCount_g`：表长等于「单周期最多读多少个寄存器」，运行时 `RegCount` 可小于该上限。
- `WrB => '0'`：核心永远不写表，只按 `RamAddr` 顺序读——表的所有权归软件，硬件只消费。
- `DoutB => RamRegAddr`：这个信号在组合进程 [hdl/axi_mm_reader.vhd:138](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L138) 的 `SetCmd_s` 态被装上 `AxiM_CmdRd_Addr`，成为发给 AXI 主机的读地址。

`RegCfg_*` 这些端口名是核心与 wrapper 之间的 **IPIC 接口**（见 u2-l5），wrapper 把 `mem_addr`/`mem_wdata`/`mem_wrena` 切片后接到这里。

#### 4.2.4 代码实践

**实践目标**：追踪「软件写一项 RegTable」与「核心读一项 RegTable」分别走哪个口。

**操作步骤**：

1. 在 [hdl/axi_mm_reader_wrp.vhd:328-331](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L328-L331) 找到 wrapper 把核心的 `RegCfg_Idx/RegCfg_WrReg/RegCfg_RdReg/RegCfg_Wr` 接到 `mem_addr(...)`/`mem_wdata`/`mem_rdata`/`mem_wrena` 的映射。
2. 回到 [hdl/axi_mm_reader.vhd:206-223](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L206-L223) 的 `i_ram`，确认软件走 A 口、核心走 B 口。
3. 在 [hdl/axi_mm_reader.vhd:128-140](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L128-L140) 观察核心如何在 `ReadAddr_s` 推进 `RamAddr`、在 `SetCmd_s` 把 `RamRegAddr` 装上命令通道。

**需要观察的现象**：

- 软件写：`mem_wrena=1` → `WrA=1`，`mem_wdata` 经 `DinA` 写入 `mem_addr` 指向的槽。
- 核心读：`RamAddr` 变化一拍后，`RamRegAddr`（`DoutB`）给出该槽地址，随即被 FSM 装上 `AxiM_CmdRd_Addr`。
- 两端操作互不阻塞：A 口写第 5 槽的同时，B 口完全可以读第 0 槽。

**预期结果**：RegTable 是一块「软件拥有、硬件消费」的只读（对硬件而言）查找表，双端口让配置与遍历并行无冲突。

#### 4.2.5 小练习与答案

**练习 1**：为什么 B 口的 `WrB` 恒为 `'0'`？若误接成 `'1'` 会怎样？

**答案**：核心只读地址表、从不改它，故 `WrB='0'`。若 `WrB='1'`，`DinB`（全 0）会随 FSM 遍历把每个槽写成 0，下一次读周期将读到全 0 地址，去访问地址 0 的寄存器——配置被硬件自身破坏。

**练习 2**：测试台 [tb/top_tb.vhd:266-268](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L266-L268) 用一个循环写满 14 项 RegTable，地址形如 `16#00AB0000#+16*i`。核心读回时，期望的 AR 地址是什么？

**答案**：FSM 按顺序读 RegTable[0..13]，发出的 AR 地址正是 `0x00AB0000, 0x00AB0010, …`（步进 16），与 [tb/top_tb.vhd:415](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L415) `axi_expect_ar(16#00AB0000#+16*i, …)` 的断言一致。

---

### 4.3 输出模式切换：g_axis / g_naxis generate 块

#### 4.3.1 概念说明

核心提供的输出信号是「模式无关」的 `AxiS_Vld/AxiS_Rdy/AxiS_Last/AxiS_Data/AxiS_Level`——一组 AXI-Stream 风格的握手。至于这组握手最终变成什么对外接口，由 wrapper 的 generic `Output_g` 决定，并靠两个互斥的 `generate` 块在综合时二选一：

- **AXIS 模式**（`Output_g = "AXIS"`）：把 `AxiS_*` 直接连到对外端口 `m_axis_t*`，下游是一个 AXI-Stream 接收方。
- **AXIMM 模式**（`Output_g /= "AXIS"`，默认）：没有 `m_axis` 端口，而是把 FIFO 的数据与标记映射到寄存器空间的 `RdData`/`RdLast`，由软件经 `s00_axi` 读取。

`generate if` 是**综合期**判断：不满足条件的块完全不产生电路，所以两种 IP 在硅片上是不同的网表，只是共享同一份源码。这与 u1-l4 讲的「`m_axis` 端口仅在 `Output_g=="AXIS"` 时出现」的打包层条件互相对应——RTL generate、component.xml 条件、GUI 参数三层必须一致。

#### 4.3.2 核心流程

两种模式的取数控制（即「谁拉 `AxiS_Rdy` 来弹 FIFO」）截然不同：

```
AXIS 模式 (g_axis):
  AxiS_Rdy <= m_axis_tready        -- 下游 AXI-S 接收方控制弹出
  m_axis_tdata  <= AxiS_Data
  m_axis_tlast  <= AxiS_Last
  m_axis_tvalid <= AxiS_Vld

AXIMM 模式 (g_naxis):
  AxiS_Rdy <= reg_rd(RdData)       -- 软件读 RdData 寄存器时弹一个
  reg_rdata(RdData) <= AxiS_Data   -- 弹出的字作为读返回值
  reg_rdata(RdLast) <= AxiS_Last   -- Last 标志映射到 RdLast 寄存器
  m_axis_tvalid <= '0'             -- RTL 端口存在但保持静默
```

`Level` 寄存器在**两种模式**都映射（`reg_rdata(Level) <= AxiS_Level`），所以软件任何时候都能查 FIFO 水位。

#### 4.3.3 源码精读

AXIS 块在 [hdl/axi_mm_reader_wrp.vhd:170-176](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L170-L176)：

```vhdl
g_axis : if Output_g = "AXIS" generate
    m_axis_tdata  <= AxiS_Data;
    m_axis_tvalid <= AxiS_Vld;
    AxiS_Rdy      <= m_axis_tready;     -- 弹出由下游 stream 控制
    m_axis_tlast  <= AxiS_Last;
    reg_rdata(RegIdx_Level_c) <= AxiS_Level;
end generate;
```

AXIMM 块在 [hdl/axi_mm_reader_wrp.vhd:178-184](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L178-L184)：

```vhdl
g_naxis : if Output_g /= "AXIS" generate
    m_axis_tvalid <= '0';                                  -- 静默未用端口
    AxiS_Rdy      <= reg_rd(RegIdx_RdData_c);              -- 软件读 RdData 时弹一个
    reg_rdata(RegIdx_RdData_c)      <= AxiS_Data;          -- 弹出值作为 RdData 读返回
    reg_rdata(RegIdx_RdLast_c)(BitIdx_RdLast_c) <= AxiS_Last;  -- Last 标志 → RdLast.bit0
    reg_rdata(RegIdx_Level_c)       <= AxiS_Level;         -- 水位 → Level
end generate;
```

涉及的常量在 [hdl/definitions_pkg.vhd:25-31](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/definitions_pkg.vhd#L25-L31)：`RegIdx_RdData_c=2`、`RegIdx_RdLast_c=3`、`BitIdx_RdLast_c=0`、`RegIdx_Level_c=4`。

关键点：

- `reg_rd` 是 AXI 从机 ipif 输出的 **one-hot 读选通**向量（每位对应一个寄存器，见 u2-l5）。当软件对地址 `0x08`（`RegIdx_RdData_c*4`）发起一次读时，`reg_rd(2)` 会在那一拍为 1。
- FIFO 弹出一项的条件是 `OutVld=1` 且 `OutRdy=1`。`AxiS_Rdy <= reg_rd(RegIdx_RdData_c)` 使「软件读一次 RdData」与「FIFO 弹一项」精确对应——这正是 u2-l2 所说的 **RV（带副作用读）**。
- `reg_rdata(RegIdx_RdLast_c)(BitIdx_RdLast_c) <= AxiS_Last` 把包尾标记放进 `RdLast` 寄存器的 bit0；注意读 `RdLast` **不会**触发 `reg_rd(RdData_c)`，所以它是「只看不弹」的 peek。
- `m_axis_tvalid <= '0'`：在 AXIMM 模式下，wrapper 实体里仍声明了 `m_axis_*` 端口（[hdl/axi_mm_reader_wrp.vhd:118-121](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L118-L121)），此处在 RTL 层把它静默；而在 IP 打包层该端口被条件隐藏（u1-l4），最终用户看不到它。

#### 4.3.4 代码实践

**实践目标**：解释 AXIMM 模式下 `AxiS_Rdy <= reg_rd(RegIdx_RdData_c)` 的含义，并验证「先读 RdLast、再读 RdData」的取数约定。

**操作步骤**：

1. 阅读 [hdl/axi_mm_reader_wrp.vhd:178-184](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L178-L184)，确认 `AxiS_Rdy` 由 `reg_rd(RegIdx_RdData_c)` 驱动。
2. 对照 AXI 从机 ipif 的输出含义（u2-l5）：`reg_rd(i)` 在软件读第 `i` 个寄存器的那一拍为 1。
3. 打开测试台 [tb/top_tb.vhd:113-133](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L113-L133) 的 `CheckResultsAxiMM` 过程，注意它对每个字的读取顺序。

**需要观察的现象**：

- `CheckResultsAxiMM` 对每个字先 `axi_single_expect(RegIdx_RdLast_c*4, …)`（[tb/top_tb.vhd:130](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L130)），再 `axi_single_expect(RegIdx_RdData_c*4, …)`（[tb/top_tb.vhd:131](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L131)）。
- 若颠倒顺序（先读 `RdData` 再读 `RdLast`），第一次读 `RdData` 就已经弹出一项，随后读 `RdLast` 看到的是**下一个**字（不是刚取走的字）的标记。

**预期结果**：因为读 `RdData` 会弹 FIFO（`AxiS_Rdy` 被那一拍的 `reg_rd(RdData)` 拉高），而读 `RdLast` 只 peek 不弹，所以软件必须**先**读 `RdLast` 判断当前字是否包尾、**再**读 `RdData` 取走该字。可表述为：

> 在 AXIMM 模式下，`AxiS_Rdy` 接到 `reg_rd(RegIdx_RdData_c)`，使得「软件发起一次 RdData 读」等价于「FIFO 弹出一项」。`RdData` 是 RV（带副作用读），`RdLast` 是普通 R（只读不弹），故取数顺序必须是先 `RdLast` 后 `RdData`。

> 待本地验证：在测试台 AXIMM 配置下（`OutputType_g="AXIMM"`）运行仿真，故意把 `CheckResultsAxiMM` 中两行交换顺序，观察 `Last` 断言与 `Data` 断言的失败现象。

#### 4.3.5 小练习与答案

**练习 1**：在 AXIS 模式下，`AxiS_Rdy` 接的是 `m_axis_tready`。若下游接收方长期不拉 `m_axis_tready`，会发生什么？

**答案**：FIFO 的 `OutRdy` 长期为 0，数据堆在 FIFO 里；当 FIFO 堆满，`Fifo_Rdy`（即 `InRdy`）变 0，经 `AxiM_RdDat_Rdy` 向 AXI 主机反压，主机暂停回流数据。这就是测试台「背压」用例（[tb/top_tb.vhd:324-355](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L324-L355)）验证的链路。

**练习 2**：为什么 `Level` 寄存器在两个 generate 块里**都**映射了 `reg_rdata(RegIdx_Level_c) <= AxiS_Level`？

**答案**：`Level` 反映 FIFO 水位，无论哪种输出模式软件都需要它——AXIS 模式下用于监控背压，AXIMM 模式下用于轮询「是否有完整包可取」（见 `CheckResultsAxiMM` 中 `axi_single_read(RegIdx_Level_c*4, …)` 循环等待 `x>0`，[tb/top_tb.vhd:122-127](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L122-L127)）。所以它是两种模式共享的状态口。

## 5. 综合实践

**任务**：用测试台的真实配置，把本讲三个模块串起来计算并验证。

测试台 [tb/top_tb.vhd:157-165](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L157-L165) 把 DUT 配为 `MaxRegCount_g=16`、`MinBuffers_g=2`，并在 [tb/top_tb.vhd:265](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L265) 写入 `RegCnt=14`。「缓冲双读」用例（[tb/top_tb.vhd:279-291](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L279-L291)）连续发两次 Trig。

请完成：

1. **算容量**：此配置下 FIFO 深度与 `OutLevel` 有效位宽各是多少？（答：\(16\times2=32\) 字；\(\log_2\lceil32\rceil+1=5+1=6\) 位。）
2. **画数据流**：两次 Trig 各读 14 个寄存器，共 28 个字进 FIFO。标出哪些字携带 `Last=1`（答：第 14、28 个字）。
3. **核对水位**：解释 [tb/top_tb.vhd:288](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L288) 为何断言 `Level=14*2=28`。
4. **对比两种取数**：
   - AXIS 下用 `CheckResultsAxiS`（[tb/top_tb.vhd:96-111](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L96-L111)）：拉高 `m_axis_tready`，循环 14 次在 `vld=1` 时比对 `data`，并在 `i=13` 比对 `last=1`。
   - AXIMM 下用 `CheckResultsAxiMM`（[tb/top_tb.vhd:113-133](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L113-L133)）：先轮询 `Level>0`，再「先 RdLast 后 RdData」取一字。
5. **总结一句**：同一块 FIFO、同一份核心 RTL，仅靠 `g_axis`/`g_naxis` 与 `AxiS_Rdy` 的接法差异，就派生出「流式直出」与「寄存器映射」两种 IP。

> 待本地验证：分别在 `OutputType_g="AXIS"` 与 `"AXIMM"` 下跑 `sim/run.tcl`，对照两次波形的 FIFO `InData(32)` 与 `OutData(32)`，确认 `Last` 标记的写入与读出完全对齐。

## 6. 本讲小结

- 读回值统一进一块 `psi_common_sync_fifo`：宽度 `32+1`（多 1 位夹带 `Last`），深度 `MaxRegCount_g*MinBuffers_g`，`Last` 在最后一字入队的那一拍由组合逻辑产生，与数据同拍、同位存储。
- `InData(32)=Last` / `OutData(32)=AxiS_Last` 让包尾标记随数据一起排队、一起出队，跨缓冲不丢失；`Fifo_Rdy` 回灌成 AXI 主机背压。
- RegTable 用一块 `psi_common_tdp_ram`：A 口软件配置（`RegCfg_Idx/WrReg/Wr`），B 口核心只读遍历（`r.RamAddr`、`WrB='0'`），两口并行无冲突。
- 输出模式靠 wrapper 中 `g_axis`/`g_naxis` 两个互斥 `generate` 块在综合期二选一：AXIS 直连 `m_axis`、`AxiS_Rdy<=m_axis_tready`；AXIMM 把 FIFO 映射到 `RdData`/`RdLast`、`AxiS_Rdy<=reg_rd(RdData)`。
- AXIMM 下 `RdData` 是 RV（读即弹），`RdLast` 是普通 R（只 peek），故软件必须「先读 RdLast、再读 RdData」；`Level` 在两种模式都可用。
- RTL generate、component.xml 端口条件、GUI 参数三层必须保持一致，才能让同一份源码正确派生出两种 IP。

## 7. 下一步学习建议

- **u3-l1（C 软件驱动）**：从软件侧验证本讲的「先 RdLast 后 RdData」约定如何被 `ReadFifoPacket` 等 API 封装，以及错误码 `FifoIsEmpty`/`NoCompletePacketInFifo` 如何对应 FIFO 水位与 `Last` 标记。
- **u3-l2（测试台架构）**：深入 `CheckResultsAxiS`/`CheckResultsAxiMM` 背后的激励/响应双进程握手，理解 `OutputType_g` 如何让同一测试台跑两遍覆盖两种模式。
- **延伸阅读**：阅读 `psi_common` 库中的 `psi_common_sync_fifo` 与 `psi_common_tdp_ram` 源码，对照本讲用到的 `Width_g/Depth_g/OutLevel` 与 `Depth_g/Width_g/Behavior_g` 参数，理解底层 RAM 行为（`RBW`/`WBR`）对一拍读延迟的影响。
