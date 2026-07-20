# AXI 主机读取通路

## 1. 本讲目标

本讲聚焦 IP 核「主动去读别人」的那条通路——`m00_axi` AXI 主机接口。前面几讲我们已经知道：核心 FSM 在触发后要逐个读取 RegTable 里登记的寄存器地址，把读回的 32 位值送进 FIFO。但核心本身只懂一套**简化的握手接口（IPIC）**，并不直接说 AXI4 协议。真正把 IPIC 翻译成 AXI4 五通道信号、再驱动 `m00_axi` 物理引脚的，是 wrapper 里实例化的 `psi_common_axi_master_simple`。

读完本讲你应当能够：

- 说清楚 wrapper 中 `psi_common_axi_master_simple` 实例的每一个关键泛型（只读、单拍、最大打开事务数、数据 FIFO 深度）各自决定了什么。
- 把核心 FSM 的 `SetCmd_s`/`ApplyCmd_s` 两个状态，与 AXI 主机的 `CmdRd`（命令）、`RdDat`（读数据）两条 IPIC 通道对应起来，画出「发命令 → 等 Rdy → 收数据」的时序。
- 解释命令通道与数据通道是**解耦**的，以及 `AxiMaxOpenTrasactions_g` 和 `DataFifoDepth_g` 如何影响读取吞吐与背压。

## 2. 前置知识

- **AXI4 读通道**：一次 AXI4 读事务由两条通道组成——读地址通道 AR（`arvalid`/`arready`/`araddr`/`arlen`…）用来「下命令」，读数据通道 R（`rvalid`/`rready`/`rdata`/`rlast`…）用来「收数据」。主机先在 AR 上发出地址，从机稍后在 R 上把数据一拍拍送回，`rlast` 标记最后一个数据。
- **突发长度（burst length / beats）**：一次 AR 命令可以读取多个连续数据拍，`arlen` 字段编码拍数（实际拍数 = `arlen+1`）。本 IP 每次只读 1 个字，即「单拍事务」（`arlen=0`）。
- **在途事务（outstanding transactions）**：主机可以在还没收到上一笔数据时，就继续发下一笔 AR 命令。同时「已发命令、未收齐数据」的事务数叫在途事务数。在途事务越多，越能掩盖总线的往返延迟，但也需要更深的缓冲。
- **IPIC 握手**：核心与 wrapper 之间用的简化接口。一条 IPIC 通道由 `Vld`（有效）+ `Rdy`（就绪）这对握手信号构成：当 `Vld=1 且 Rdy=1` 的同一拍，一次传输完成。本讲涉及两条 IPIC 通道：`CmdRd`（命令读）和 `RdDat`（读数据）。
- **背压（backpressure）**：下游用 `Rdy=0` 告诉上游「我暂时接不下」，上游必须保持 `Vld=1` 的数据不动，等下游 `Rdy=1` 再完成传输。

> 提示：本讲是 u2-l3（核心 FSM）的后续。FSM 的五态、双进程方法、预读机制已在 u2-l3 讲过，本讲直接沿用，不再重复。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注什么 |
| --- | --- | --- |
| `hdl/axi_mm_reader_wrp.vhd` | AXI 接口边界（wrapper） | `psi_common_axi_master_simple` 的实例化、泛型取值、AR/R 引脚映射 |
| `hdl/axi_mm_reader.vhd` | 纯逻辑核心 | 核心 FSM 如何驱动 `CmdRd`、如何消费 `RdDat`、与 FIFO 的连接 |

一句话定位：**wrapper 负责「说 AXI」，核心负责「决定读什么、读到后怎么存」**。两者在 IPIC 通道上握手。

## 4. 核心概念与源码讲解

### 4.1 AXI 主机实例化与只读配置

#### 4.1.1 概念说明

`psi_common_axi_master_simple` 是 psi_common 库提供的「轻量 AXI 主机」IP。它的作用是把用户侧一套简单的 IPIC 命令/数据接口，转换成对外的 AXI4 读/写通道，并自带命令缓冲与数据 FIFO，把复杂的协议时序挡在内部。

本 IP 用它，且只用了它**读**的那一半：

- IP 的使命是「读寄存器」，从不写。所以实例把 `ImplRead_g => true`、`ImplWrite_g => false`，让主机只生成 AR（读地址）和 R（读数据）两条 AXI 通道，AW/W/B（写地址/写数据/写响应）三条通道根本不会被实现。
- 这也解释了为什么 wrapper 的实体端口里，`m00_axi` 一侧**只声明了 AR 和 R 两组信号**，没有任何写通道引脚。

#### 4.1.2 核心流程

主机实例化的配置流程可以归纳为四组选择：

1. **能力选择**：`ImplRead_g`/`ImplWrite_g` 决定生成哪些 AXI 通道 → 这里只读。
2. **位宽选择**：`AxiAddrWidth_g`/`AxiDataWidth_g` → 地址 32 位、数据 32 位。
3. **事务粒度选择**：`AxiMaxBeats_g => 1` 把每次 AXI 突发限制为 1 拍；用户命令侧 `UserTransactionSizeBits_g => 1` + `CmdRd_Size => "1"`，即每条命令请求一个字。
4. **缓冲选择**：`AxiMaxOpenTrasactions_g => 4` 允许最多 4 笔在途事务；`DataFifoDepth_g => 16` 给主机内部的数据 FIFO 设 16 深度。

伪代码概括：

```text
i_axim : psi_common_axi_master_simple
    读能力   = 开
    写能力   = 关          // 只读不写
    单拍     = 是           // AxiMaxBeats_g = 1
    在途事务 = 最多 4 笔
    数据FIFO = 16 深
    用户侧   = CmdRd(命令) + RdDat(数据) 两条 IPIC 通道
    总线侧   = AR + R 两条 AXI4 通道
```

#### 4.1.3 源码精读

实例化在 wrapper 中，名为 `i_axim`：

[hdl/axi_mm_reader_wrp.vhd:271-308](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L271-L308) —— `psi_common_axi_master_simple` 的实例 `i_axim`，泛型与端口映射。注意端口映射里只接了 AR（`M_Axi_Ar*`）和 R（`M_Axi_R*`）两组，没有任何 `M_Axi_Aw*`/`M_Axi_W*`/`M_Axi_B*` 写通道。

只读能力由这两个泛型决定：

[hdl/axi_mm_reader_wrp.vhd:279-280](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L279-L280) —— `ImplRead_g => true`、`ImplWrite_g => false`，主机只实现读通路。

单拍与用户命令粒度：

[hdl/axi_mm_reader_wrp.vhd:275-278](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L275-L278) —— `AxiMaxBeats_g => 1`（每笔 AXI 突发最多 1 拍）、`UserTransactionSizeBits_g => 1`、`DataFifoDepth_g => 16`。

[hdl/axi_mm_reader_wrp.vhd:286-290](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L286-L290) —— 用户命令侧端口：`CmdRd_Addr` 接核心给的地址、`CmdRd_Size => "1"` 是常量（每次请求一个字）、`CmdRd_LowLat => '0'`、`CmdRd_Vld/Rdy` 与核心握手。

「只读」这一点的另一处佐证在 wrapper 实体端口声明里——`m00_axi` 只有 AR 和 R：

[hdl/axi_mm_reader_wrp.vhd:98-113](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L98-L113) —— `m00_axi` 接口只声明了 `m00_axi_ar*`（读地址通道）和 `m00_axi_r*`（读数据通道），没有写通道。

把泛型整理成一张表：

| 泛型 | 取值 | 含义 |
| --- | --- | --- |
| `AxiAddrWidth_g` | 32 | 读地址位宽 |
| `AxiDataWidth_g` | 32 | 读数据位宽 |
| `AxiMaxBeats_g` | 1 | 每笔 AXI 突发最多 1 拍（单拍读） |
| `AxiMaxOpenTrasactions_g` | 4 | 最多 4 笔在途读事务 |
| `UserTransactionSizeBits_g` | 1 | 用户命令 `CmdRd_Size` 的位宽 |
| `DataFifoDepth_g` | 16 | 主机内部读数据 FIFO 深度 |
| `ImplRead_g` | true | 实现读通路 |
| `ImplWrite_g` | false | 不实现写通路（只读） |

#### 4.1.4 代码实践

**实践目标**：亲手确认「只读」是贯穿三处一致的事实。

**操作步骤**：

1. 打开 `hdl/axi_mm_reader_wrp.vhd`，定位 `i_axim` 实例（第 271 行起）。
2. 在泛型映射里找到 `ImplRead_g`/`ImplWrite_g`，记录取值。
3. 在端口映射里数一数 `M_Axi_*` 引脚：是否只有 `M_Axi_Ar*` 和 `M_Axi_R*`？有没有 `M_Axi_Aw*`/`M_Axi_W*`/`M_Axi_B*`？
4. 再翻到实体端口声明（第 98–113 行），确认 `m00_axi` 同样只有 AR 和 R。

**需要观察的现象**：三处结论应当一致——`ImplWrite_g=false`、端口无写通道、实体无写引脚。

**预期结果**：在 Vivado 综合后的接口视图中，该 IP 的 `m00_axi` 只有读地址 + 读数据两组端口；用 `git grep -n "awvalid\|wvalid\|bvalid"` 在 `hdl/` 下找不到任何 `m00_axi` 写通道信号。

> 待本地验证：综合后端口列表的具体外观取决于 Vivado 版本，可在打包后的 IP Symbol 视图中确认。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `ImplWrite_g` 改成 `true`，wrapper 还需要做什么改动才算完整？

**参考答案**：实体需要补声明 `m00_axi` 的 AW/W/B 三组端口（`awaddr`…`bready`），实例端口映射要把对应的 `M_Axi_Aw*`/`M_Axi_W*`/`M_Axi_B*` 接出去，并需要一套用户侧写命令/写数据 IPIC 通道。本 IP 没有这些，所以保持 `false`。

**练习 2**：`AxiMaxBeats_g => 1` 对应 AXI 的 `arlen` 取多少？

**参考答案**：单拍事务即 `arlen=0`（AXI 规定实际拍数 = `arlen+1`，`arlen=0` 表示 1 拍）。

---

### 4.2 命令通道：核心如何下发 CmdRd

#### 4.2.1 概念说明

`CmdRd` 是 AXI 主机的「读命令」IPIC 通道，核心是它的使用者（master 是提供者）。核心每读完一个 RegTable 项，就在 `CmdRd` 上发一条命令：把要读的 32 位地址放上 `CmdRd_Addr`，拉高 `CmdRd_Vld`，等主机的 `CmdRd_Rdy` 握手。主机内部再把这条命令翻译成 AXI4 的 AR 通道事务。

这一节的精髓是核心 FSM 的 `SetCmd_s` → `ApplyCmd_s` 两态，以及它们和 RAM 预读的配合（u2-l3 已铺垫）。

#### 4.2.2 核心流程

核心遍历 RegTable、逐条发命令的循环：

```text
Idle_s      -- 等到 Start
ReadAddr_s  -- RamAddr 指向当前项，并预先 +1；读 RAM（B 口）
            -- 若 RamAddr = RegCount：全部发完，转 WaitDone_s
            -- 否则转 SetCmd_s
SetCmd_s    -- 把 RAM 输出 RamRegAddr 装上 CmdRd_Addr，CmdRd_Vld := 1
ApplyCmd_s  -- 等 CmdRd_Rdy = 1：
            --   CmdRd_Vld := 0，回 ReadAddr_s 取下一项
```

注意 `ReadAddr_s` 已经把 `RamAddr` 自增，再利用 RAM 一拍读延迟，使得到了 `SetCmd_s`，RAM B 口输出的 `RamRegAddr` 正好是「当前要读」的地址。于是 `SetCmd_s` 一拍就把命令准备好，不必单独等 RAM。

握手规则：`CmdRd_Vld=1 且 CmdRd_Rdy=1` 同一拍，命令被主机收下；核心随即在下一拍撤掉 `CmdRd_Vld` 并去取下一项。

#### 4.2.3 源码精读

核心实体的 AXI 主机端口，命令侧三个信号（核心是输出方 / 输入方各半）：

[hdl/axi_mm_reader.vhd:52-54](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L52-L54) —— `AxiM_CmdRd_Addr`/`AxiM_CmdRd_Vld`（核心输出）、`AxiM_CmdRd_Rdy`（核心输入）。

双进程 record 里专门为命令保留了两个字段（地址和有效）：

[hdl/axi_mm_reader.vhd:82-83](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L82-L83) —— record 内 `AxiM_CmdRd_Addr`、`AxiM_CmdRd_Vld` 两个寄存器字段。

`SetCmd_s` 装载命令：

[hdl/axi_mm_reader.vhd:137-140](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L137-L140) —— 把 RAM 输出 `RamRegAddr` 写入 `AxiM_CmdRd_Addr`，拉高 `AxiM_CmdRd_Vld`，转到 `ApplyCmd_s`。

`ApplyCmd_s` 等握手并耐受反压：

[hdl/axi_mm_reader.vhd:142-146](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L142-L146) —— 只有 `AxiM_CmdRd_Rdy='1'` 时才撤掉 `Vld` 并回到 `ReadAddr_s`；`Rdy=0` 期间 `Vld` 保持为 1，命令停在通道上等待。

最后把寄存器字段引到端口：

[hdl/axi_mm_reader.vhd:183-184](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L183-L184) —— `AxiM_CmdRd_Addr <= r.AxiM_CmdRd_Addr`、`AxiM_CmdRd_Vld <= r.AxiM_CmdRd_Vld`，把命令侧信号接到核心端口（再由 wrapper 连到主机的 `CmdRd_*`）。

#### 4.2.4 代码实践

**实践目标**：跟踪一次「软件配好 RegTable → 核心发出一条 CmdRd 命令」的完整路径，确认地址来源正确。

**操作步骤**：

1. 在 wrapper 中确认 `AxiM_CmdRd_Addr` 同时连到核心（[hdl/axi_mm_reader_wrp.vhd:332](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L332)）和主机（[hdl/axi_mm_reader_wrp.vhd:286](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L286)）——核心是输出方，主机是输入方。
2. 在核心里追溯 `AxiM_CmdRd_Addr` 的数据来源：`SetCmd_s` 里它来自 `RamRegAddr`。
3. 再追溯 `RamRegAddr` 来自 RAM B 口 `DoutB`，B 口地址是 `r.RamAddr`。
4. 最后确认 `RamAddr` 在 `ReadAddr_s` 自增、在 `Idle_s` 清零。

**需要观察的现象**：地址链路应当闭合——软件写 RegTable（经 `mem_*`）→ RAM A 口 → 存储 → B 口按 `RamAddr` 读出 `RamRegAddr` → 装上 `CmdRd_Addr` → 经 wrapper 直通到主机的 `CmdRd_Addr`。

**预期结果**：核心发出的每一条命令地址，等于软件事先写入 RegTable 对应项的 32 位地址。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ApplyCmd_s` 必须等 `CmdRd_Rdy`，而不是发完就走？

**参考答案**：IPIC 是 `Vld/Rdy` 握手。`CmdRd_Vld=1 且 CmdRd_Rdy=1` 同一拍事务才算完成。若不等 `Rdy` 就撤掉 `Vld`，主机可能没收到这条命令，导致漏读。`ApplyCmd_s` 的等待正是为了耐受主机端的反压。

**练习 2**：核心发出命令的最小周期是几拍（假设主机 `CmdRd_Rdy` 一直为 1）？

**参考答案**：`ReadAddr_s` → `SetCmd_s` → `ApplyCmd_s`（握手成功）→ 回 `ReadAddr_s`，约 3 拍发一条命令（RAM 预读使其不必额外等待）。

---

### 4.3 数据通道：RdDat 回流、DoneCnt 与背压

#### 4.3.1 概念说明

`RdDat` 是 AXI 主机的「读数据」IPIC 通道，方向与 `CmdRd` 相反：主机把读回来的 32 位数据放上 `RdDat_Data`、拉高 `RdDat_Vld`，等核心的 `RdDat_Rdy` 握手。主机内部已经把 AXI4 R 通道（含 `rlast`、可能多拍）整理成这条简单的数据流。

本 IP 的关键设计选择是：**核心几乎不自己背压**。它把 `AxiM_RdDat_Rdy` 直接接成内部 FIFO 的 `Fifo_Rdy`——FIFO 能收，核心就收；FIFO 不能收，核心才反压。而 FIFO 深度是 `MaxRegCount_g*MinBuffers_g`，足够缓存放若干个完整读周期，因此正常运行时核心几乎总是「就绪」，读通路不会被下游卡住。

#### 4.3.2 核心流程

读数据回流 + 计数：

```text
主机发 RdDat_Vld=1, RdDat_Data=<值>
        |
        v
核心: AxiM_RdDat_Rdy <= Fifo_Rdy     -- 能不能收看 FIFO
        |
当 (RdDat_Vld=1 且 Fifo_Rdy=1) 这一拍:
        - 数据 + Last 位写入 FIFO（InData）
        - DoneCnt := DoneCnt + 1        -- 收齐计数
        |
当 DoneCnt = RegCount:                 -- 全部收齐
        - WaitDone_s 置 DoneIrq=1, 回 Idle_s
```

`Last` 是组合逻辑判据：当 `DoneCnt = RegCount-1` 时为 1，标记本周期最后一个字。这个 `Last` 与数据同拍写入 FIFO 的第 33 位（`InData(32)`），作为输出侧的末尾标记。

命令侧和数据侧**彼此独立**：核心可以一边在 `SetCmd_s`/`ApplyCmd_s` 发命令，一边 `DoneCnt` 在累加收到的数据。这正是 `AxiMaxOpenTrasactions_g=4` 能发挥作用的根本原因——主机允许有几笔命令在途，数据稍后回流也不会堵死命令通道。

#### 4.3.3 源码精读

核心实体的读数据端口：

[hdl/axi_mm_reader.vhd:55-57](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L55-L57) —— `AxiM_RdDat_Data`/`AxiM_RdDat_Vld`（核心输入）、`AxiM_RdDat_Rdy`（核心输出）。

核心「就绪」就是 FIFO 就绪：

[hdl/axi_mm_reader.vhd:182](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L182) —— `AxiM_RdDat_Rdy <= Fifo_Rdy`，核心对读数据的背压完全委托给内部 FIFO。

收齐计数（在 `p_comb` 里，与 FSM 并列）：

[hdl/axi_mm_reader.vhd:161-166](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L161-L166) —— 周期开始时 `DoneCnt := 0`；每当 `AxiM_RdDat_Vld=1 且 Fifo_Rdy=1`，`DoneCnt` 加 1。计的是真正「被 FIFO 收下」的数据拍数。

`WaitDone_s` 等收齐：

[hdl/axi_mm_reader.vhd:148-152](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L148-L152) —— 当 `DoneCnt = RegCount`，发一拍 `DoneIrq` 并回 `Idle_s`。

FIFO 的连接（数据 + Last 位同进同出）：

[hdl/axi_mm_reader.vhd:226-247](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L226-L247) —— 读数据 FIFO（`psi_common_sync_fifo`）：`InData(31:0) <= AxiM_RdDat_Data`、`InData(32) <= Last`、`InVld <= AxiM_RdDat_Vld`、`InRdy => Fifo_Rdy`；输出侧 `OutData(31:0) => AxiS_Data`、`OutData(32) => AxiS_Last`。深度 `Depth_g => MaxRegCount_g*MinBuffers_g`、宽度 `Width_g => 32+1`（多 1 位存 Last）。

`Last` 的组合判据：

[hdl/axi_mm_reader.vhd:168-172](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L168-L172) —— `r.DoneCnt = r.RegCount-1` 时 `Last<='1'`，与该拍数据一同进 FIFO 的第 33 位。

#### 4.3.4 代码实践

**实践目标**：验证「数据通道背压 = FIFO 背压」这一设计，并理解为何正常运行时几乎不背压。

**操作步骤**：

1. 读 [hdl/axi_mm_reader.vhd:182](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L182)，确认 `AxiM_RdDat_Rdy` 的唯一来源是 `Fifo_Rdy`。
2. 读 FIFO 实例的 `InRdy => Fifo_Rdy` 与 `OutRdy => AxiS_Rdy`，理解 `Fifo_Rdy` 何时为 0：仅当 FIFO 满。
3. 计算 FIFO 容量：默认 `MaxRegCount_g*MinBuffers_g = 1024*4 = 4096` 个字（见 [hdl/axi_mm_reader.vhd:229](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L229)）。
4. 对照 `AxiMaxOpenTrasactions_g=4`（最多 4 笔在途、每笔 1 拍），算出同时在途数据上限。

**需要观察的现象**：FIFO 容量远大于同时在途数据量。

**预期结果**：同时在途数据最多 4 拍，而 FIFO 能放 4096 字；只要输出侧（AXIS 下游或软件读 RdData）不是长时间完全停滞，FIFO 不会满，`AxiM_RdDat_Rdy` 几乎一直为 1，读通路不被背压。

#### 4.3.5 小练习与答案

**练习 1**：`DoneCnt` 在什么条件下才加 1？为什么不是只要 `AxiM_RdDat_Vld=1` 就加？

**参考答案**：条件是 `AxiM_RdDat_Vld=1 且 Fifo_Rdy=1`（[hdl/axi_mm_reader.vhd:164-165](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L164-L165)）。因为只有同时满足 `Rdy=1` 这一拍，数据才真正被 FIFO 收下。若 `Fifo_Rdy=0`，数据没进 FIFO，自然不能计为「已收到」。

**练习 2**：为什么把 `AxiM_RdDat_Rdy` 直接接 `Fifo_Rdy` 是合理的？

**参考答案**：核心对读回的数据没有别的处理（不丢弃、不重排），唯一的归宿就是 FIFO。所以「核心能否接收」完全等价于「FIFO 能否接收」，直接用 `Fifo_Rdy` 即可，无需额外缓冲或控制。

---

## 5. 综合实践

把命令通道和数据通道串起来，做一次端到端的时序与吞吐分析。这是本讲的核心实践。

### 5.1 描述一次「发命令 → 等 Rdy → 收数据」的时序

**实践目标**：把 `SetCmd_s`/`ApplyCmd_s` 与 `RdDat` 回流对应到逐拍时序。

**操作步骤**：

1. 设定一个最小场景：`RegCount=2`（本周期读 2 个寄存器），假设主机 `CmdRd_Rdy` 一直为 1，读数据经过 `L` 拍总线延迟后回流。
2. 在纸上画出以下信号逐拍变化（伪波形）：
   - `r.Fsm`（状态）
   - `r.RamAddr`
   - `AxiM_CmdRd_Vld` / `AxiM_CmdRd_Rdy`
   - `AxiM_CmdRd_Addr`
   - `AxiM_RdDat_Vld` / `AxiM_RdDat_Rdy`
   - `r.DoneCnt`
3. 参考（主机就绪、`L=3` 拍延迟）：

   | 拍 | Fsm | RamAddr | CmdRd_Vld | CmdRd_Addr | RdDat_Vld | DoneCnt | 说明 |
   | --- | --- | --- | --- | --- | --- | --- | --- |
   | 0 | ReadAddr | 0→1 | 0 | — | 0 | 0 | 判 0≠2，预增，读 RAM[0] |
   | 1 | SetCmd | 1 | 0→1 | RAM[0] | 0 | 0 | 装载第 1 条命令地址 |
   | 2 | ApplyCmd | 1 | 1→0 | RAM[0] | 0 | 0 | 握手成功，命令 1 发出 |
   | 3 | ReadAddr | 1→2 | 0 | — | 0 | 0 | 判 1≠2，预增，读 RAM[1] |
   | 4 | SetCmd | 2 | 0→1 | RAM[1] | 0 | 0 | 装载第 2 条命令地址 |
   | 5 | ApplyCmd | 2 | 1→0 | RAM[1] | 0 | 0 | 命令 2 发出 |
   | 6 | WaitDone | 2 | 0 | — | 0 | 0 | 命令全发完，等数据 |
   | … | WaitDone | 2 | 0 | — | 0 | 0 | 等总线返回 |
   | 2+L | WaitDone | 2 | 0 | — | 1 | 0→1 | 数据 1 到，入 FIFO |
   | … | WaitDone | 2 | 0 | — | 1 | 1→2 | 数据 2 到，DoneCnt=2=RegCount |
   | 次 | Idle | 2 | 0 | — | 0 | 2 | DoneIrq 一拍 |

   > 上表为「主机即时握手 + 固定延迟」的理想时序，仅用于理解状态与通道的对应关系，**待本地验证**：实际 `CmdRd_Rdy` 拉高的节拍、`RdDat` 回流的延迟与间隔，取决于 AXI 从机 BFM 的应答模型，应以仿真波形为准。

4. 在 `tb/top_tb.vhd` 里找到读从机的应答延迟配置，把真实 `L` 代入重画一版。

**需要观察的现象**：`SetCmd_s` 把地址装上、`ApplyCmd_s` 等到 `CmdRd_Rdy` 才算「命令发出」；数据则可能在所有命令发完之后才陆续回流（`WaitDone_s` 期间 `DoneCnt` 递增）。

**预期结果**：命令侧与数据侧在时间上完全解耦——命令早在拍 5 就发完，数据到拍 `2+L` 才开始到；`WaitDone_s` 只看 `DoneCnt` 是否收齐，不关心命令何时发完。

### 5.2 解释 `DataFifoDepth_g` 与 `AxiMaxOpenTrasactions_g` 对吞吐与背压的影响

**实践目标**：把两个泛型的工程含义讲清楚。

**操作步骤与分析要点**：

1. **`AxiMaxOpenTrasactions_g => 4`（在途事务数）——决定吞吐与延迟掩盖能力**。
   - 它允许主机在收到任何 R 数据之前，最多发出 4 笔 AR 命令。命令侧的流水因此不被单笔往返延迟卡住。
   - 若总线往返延迟为 `L` 拍、命令每 `I` 拍发一条，则在途事务数至少需要 \(\lceil L / I \rceil\) 才能让命令侧不空转。本 IP 命令侧约 3 拍一条（见 4.2.5），4 笔在途可掩盖约 \[ 4 \times 3 = 12 \text{ 拍} \] 量级的往返延迟。
   - 调大它→更耐高延迟总线、吞吐更高；代价是主机内部要更多命令缓冲、更多资源。

2. **`DataFifoDepth_g => 16`（主机内部数据 FIFO）——决定主机侧是否溢出**。
   - 主机把 AXI R 通道收下的数据先存进这个 FIFO，再按 `RdDat` 交给核心。FIFO 必须能容纳「已发命令但还没被核心读走」的最大数据量。
   - 在途事务最多 4 笔、每笔 1 拍，所以同时在途数据上限为 \[ 4 \times 1 = 4 \leq 16 \]，16 深 FIFO 绰绰有余，主机内部不会因「在途数据」溢出。
   - 调小它（比如 < 在途事务数）→可能溢出、丢数据；调大→更抗核心侧背压，但费资源。

3. **核心侧的「真背压」在哪里**：核心自己几乎不背压（`AxiM_RdDat_Rdy<=Fifo_Rdy`），真正的大缓冲是核心内 `MaxRegCount_g*MinBuffers_g` 深的 FIFO（默认 4096 字）。只有当下游（AXIS 的 `m_axis_tready`，或 AXIMM 下软件停止读 `RdData`）长时间不消费，把核心 FIFO 灌满，`Fifo_Rdy` 才会变 0，反压才经 `AxiM_RdDat_Rdy` 一路传到 AXI 主机的 R 通道（`m00_axi_rready=0`），进而可能让主机停止从总线取数。

4. **一句话总结背压链路**：
   ```text
   下游不取数 → 核心FIFO满(Fifo_Rdy=0) → AxiM_RdDat_Rdy=0
              → 主机内部DataFifo停止向核心送 → 主机R通道反压(m00_axi_rready=0)
              → 总线暂停返回数据
   ```
   而 `AxiMaxOpenTrasactions_g` 决定这条反压生效前，主机还能「预取」多少笔；`DataFifoDepth_g` 决定主机内部缓冲多少拍。两者共同决定了「下游停滞」到「总线真的停」之间的缓冲余量。

**预期结果**：能用自己的话讲清——`AxiMaxOpenTrasactions_g` 主要影响**吞吐/延迟掩盖**，`DataFifoDepth_g` 主要影响**主机内部抗溢出与背压传递**；而整条通路的「大水库」其实是核心那个深 FIFO，所以正常运行时读通路几乎不被背压。

> 待本地验证：上述「3 拍一条命令」「4 笔掩盖约 12 拍延迟」是基于核心 FSM 的理想估算；真实综合后关键路径可能让命令侧周期略长，应以时序报告和仿真波形校准。

## 6. 本讲小结

- wrapper 用一个 `psi_common_axi_master_simple` 实例 `i_axim` 把核心的 IPIC 接口翻译成 AXI4 通道；实例只接 AR 和 R 两组引脚，对应 `ImplRead_g=true`/`ImplWrite_g=false` 的**只读**配置。
- 每个寄存器是一次**单拍读事务**：`AxiMaxBeats_g=>1`、`CmdRd_Size=>"1"`；最多 `AxiMaxOpenTrasactions_g=>4` 笔在途事务；主机内部 `DataFifoDepth_g=>16` 的数据 FIFO。
- 命令通道：核心在 `SetCmd_s` 把 RAM 预读出的地址装上 `CmdRd_Addr` 并拉高 `Vld`，在 `ApplyCmd_s` 等 `CmdRd_Rdy` 握手——这是命令侧的 `Vld/Rdy` 握手，耐受反压。
- 数据通道：主机经 `RdDat` 回流数据，核心用 `AxiM_RdDat_Rdy <= Fifo_Rdy` 把背压完全委托给内部 FIFO；每收下一拍 `DoneCnt+1`，收齐到 `RegCount` 发 `DoneIrq`。
- 命令侧与数据侧**解耦**：这正是「在途事务」能掩盖总线延迟的根本；同时在途数据远小于主机 FIFO 与核心 FIFO 容量，故读通路正常几乎不背压。
- 背压链路是：下游不取数 → 核心 FIFO 满 → `AxiM_RdDat_Rdy=0` → 主机 R 通道反压。

## 7. 下一步学习建议

- 接着读 **u2-l7（输出模式与 FIFO/RegTable 存储）**：本讲只讲到「数据进了核心 FIFO」，FIFO 的输出侧（`AxiS_*`）如何按 AXIS / AXIMM 两种模式交出去、`AxiS_Level` 与 `Level` 寄存器怎么报水位，是自然的下一步。
- 若想看真实时序，建议配合 **u3-l2（测试台架构与用例）**：`tb/top_tb.vhd` 里的 AXI 读从机 BFM 会给出具体的应答延迟，可用来验证本讲 5.1 节的理想时序表。
- 想深入 `psi_common_axi_master_simple` 内部（命令 FIFO、DataFifo、AR/R 通道状态机）的读者，可去 psi_common 库阅读其源码；本仓库只把它当黑盒使用，故细节留待那里。
