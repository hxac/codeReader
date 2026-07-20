# AXI4 主机：命令、burst 与数据流

## 1. 本讲目标

本讲聚焦数据面通路 `M00_AXI`：核心测试逻辑 `i_logic` 的高层用户接口，如何经 `psi_common_axi_master_simple` 这个 IP 实例，被翻译成通往被测存储器（如 DDR）的标准 AXI4 突发（burst）事务。

学完后你应当能够：

1. 说清 `CmdWr/CmdRd/WrDat/RdDat` 这组用户接口的信号含义与 valid/ready 握手约定，特别是 **Size 字段以 beat 为单位、而非字节**。
2. 解释 `AxiMaxBeats_g`、`AxiMaxOpenTrasactions_g`、`DataFifoDepth_g`、`UserTransactionSizeBits_g` 这几个 generic 各自约束什么、对吞吐与延迟有什么影响。
3. 亲手把寄存器里的字节级 `SIZE` 换算成核心下发的 beat 计数，再结合 master 的拆 burst 行为，推算一次测试在总线上会拆成几个 burst、每个 burst 多少 beat。
4. 说清 `Wr_Done/Wr_Error/Rd_Done/Rd_Error` 四个响应信号里，哪两个真正被核心 FSM 使用、如何触发不可恢复的 `AxiError_s` 状态。

本讲是 [u3-l1（wrapper 三实例架构）](u3-l1-wrapper-architecture.md) 与 [u3-l3（主状态机）](u3-l3-main-fsm.md) 的延续：u3-l1 告诉你 wrapper 里有一个 `i_master` 实例夹在核心逻辑与 `M00_AXI` 之间，u3-l3 告诉你 FSM 在 `WrCmd_s`/`RdCmd_s` 状态下达命令、在错误时跳进 `AxiError_s`。本讲把镜头推近到这个 `i_master` 实例本身，看清它的契约与参数。

## 2. 前置知识

阅读本讲前，你需要以下概念（不熟悉的术语下面都有解释）：

- **AXI4 通道**：一次 AXI4 传输分五个独立通道——写地址 `AW`、写数据 `W`、写响应 `B`、读地址 `AR`、读数据 `R`。每个通道各自有一对 `Valid/Ready` 握手信号，同周期都为高才算一拍成功传输。
- **beat（拍）**：数据通道上一拍时钟传输的一个数据字。一个 beat 携带的字节数 \( B = \text{AxiDataWidth}_g / 8 \)。本 IP 默认数据宽 64 位，即每 beat 8 字节；本讲后面也用到 32 位（每 beat 4 字节）的例子。
- **burst（突发）**：由一次 `AW`/`AR` 握手发起、随后连续传输的多个 beat 序列。AXI4 用 `AWLEN`/`ARLEN` 表示一个 burst 的 beat 数，且编码为 **beat 数 − 1**（即 `AWLEN=15` 表示 16 个 beat）。`AWSIZE`/`ARSIZE` 表示每 beat 的字节数（以 2 的幂编码）。
- **outstanding transaction（在途事务）**：已经发出地址握手、但尚未收到最终响应（写看 `B`、读看 `R` 的最后一拍）的 burst 数量。允许同时在途的 burst 数越多，流水线并行度越高、吞吐越大，但也需要更深的缓冲。
- **generic（类属参数）**：VHDL 实体在综合前确定的编译期常量，类似 C++ 的模板参数。本讲涉及的 generic 全部在 wrapper 层声明，下传给 master 实例。
- **two-process 设计法**：核心 `mem_test.vhd` 用「组合进程 `p_comb` 算下一拍 + 寄存进程 `p_reg` 抄写」的两进程写法，所有状态装在记录 `two_process_r` 里。详见 [u3-l2](u3-l2-core-entity-and-two-process.md)。

一个关键直觉：**核心逻辑只懂「从地址 A 开始，搬 Size 个 beat」这种高层语义，它不懂 AXI4 的五通道、不懂 AWLEN、不懂 burst 拆分**。把这些高层命令翻译成总线上合法的 AXI4 突发，全是 `i_master` 实例的职责。这层抽象让核心代码极简（整个搬运逻辑就是一个状态机 + 一个 beat 计数器），同时让 IP 能挂在任何标准 AXI4 存储器接口上。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [hdl/mem_test_wrapper.vhd](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd) | 顶层装配。声明 `i_master`（`psi_common_axi_master_simple`）实例及其 generic，把核心的高层用户接口接到 master，把 master 的 `M00_AXI` 引到顶层端口。 |
| [hdl/mem_test.vhd](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd) | 核心逻辑。在 `p_comb` 里驱动 `CmdWr/CmdRd/WrDat`、消费 `RdDat`、把字节级 `SIZE`/`ADDR` 换算成 beat 级命令，并监听 `Wr_Error/Rd_Error` 触发错误陷阱。 |
| [hdl/mem_test_pkg.vhd](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_pkg.vhd) | 寄存器地图与状态码常量。本讲用到 `C_STATUS_AXIERR` 等状态码，解释错误如何映射到对外 `STATUS` 寄存器。 |

> 提示：`psi_common_axi_master_simple` 的实体定义不在本仓库，它属于外部依赖 `psi_common`（见 [u1-l2](u1-l2-repo-structure-and-dependencies.md)）。本讲只依据 wrapper 里如何实例化与连接它来推断其契约；对该 IP 内部状态机的精确描述以 `psi_common` 的文档为准。

## 4. 核心概念与源码讲解

### 4.1 `axi_master_simple` 实例化与 generics

#### 4.1.1 概念说明

`i_master` 是 wrapper 里的第二个实例（第一个是从机 `i_slave`，已在 [u4-l1](u4-l1-axi-lite-slave.md) 讲过）。它是一个 **AXI4 主机 IP**，对外提供一套简化的「用户接口」，对内生成标准 AXI4 信号。它的价值在于：

- 核心只需产生「地址 + 总 beat 数 + 数据流」这种高层命令，无需自己维护 AW/W/B/AR/R 五个通道、无需处理 burst 拆分、无需维护在途事务计数。
- master IP 内部把这些高层命令拆成总线上合法的 burst、维护 outstanding 计数、用 FIFO 缓冲数据、把 BRESP/RRESP 翻译成 `Wr_Error/Rd_Error`。

wrapper 通过一组 generic 把可配置参数从顶层透传给 master，让同一个 IP 能适配不同的数据宽、burst 上限和并发度。

#### 4.1.2 核心流程

master 实例化的数据流可以概括成下面这张「翻译」示意：

```text
   核心逻辑 i_logic                    i_master                     M00_AXI
   (高层语义)                    (psi_common_axi_master_simple)      (AXI4 总线)
 ┌───────────┐   CmdWr_Addr/Size/Vld  ┌──────────────┐  AW/W/B  ┌──────────┐
 │ WrCmd_s   │ ─────────────────────▶ │              │ ───────▶ │          │
 │ Write_s   │   WrDat_Data/Be/Vld    │  命令拆分     │          │  被测    │
 │ RdCmd_s   │ ─────────────────────▶ │  burst 生成   │          │  存储器  │
 │ Read_s    │   RdDat_Rdy            │  FIFO 缓冲    │  AR/R    │  (DDR)   │
 │           │ ◀───────────────────── │              │ ◀─────── │          │
 │           │   RdDat_Data/Vld       │              │          │          │
 │ 错误陷阱  │   Wr_Error/Rd_Error    │              │          │          │
 │ AxiError  │ ◀───────────────────── │              │          │          │
 └───────────┘                        └──────────────┘          └──────────┘
```

generic 的下传规则（详见 4.1.3）是：wrapper 顶层声明 `C_M00_AXI_*` 一组参数，在实例化时把它们映射到 master 的 `Axi*_g` generic 上。这些参数分为三类——地址/数据宽度、burst 与并发能力、内部缓冲深度。

#### 4.1.3 源码精读

先看 wrapper 顶层声明的 master 相关 generic：

[hdl/mem_test_wrapper.vhd:14-24](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd#L14-L24) —— 声明了 `C_M00_AXI_DATA_WIDTH`(默认 64)、`C_M00_AXI_ADDR_WIDTH`(默认 32)、`C_M00_AXI_MAX_BURST_SIZE`(默认 16)、`C_M00_AXI_MAX_OPEN_TRANS`(默认 2)。这四个就是后续透传给 master 的可配置项。

再看 master 实例的 generic 映射：

[hdl/mem_test_wrapper.vhd:226-237](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd#L226-L237) —— 逐项说明：

- `AxiAddrWidth_g => C_M00_AXI_ADDR_WIDTH`、`AxiDataWidth_g => C_M00_AXI_DATA_WIDTH`：总线的地址/数据位宽，同时也决定核心侧用户接口的位宽。
- `AxiMaxBeats_g => C_M00_AXI_MAX_BURST_SIZE`：**单个 burst 最多多少 beat**（默认 16）。这是 master 把高层命令拆 burst 的上限：一条命令若超过这么多 beat，会被拆成多个 burst。
- `AxiMaxOpenTrasactions_g => C_M00_AXI_MAX_OPEN_TRANS`：**同时在途的 burst 数上限**（默认 2）。允许 master 在前一个 burst 还没收完时，就发出下一个 burst 的地址，提高吞吐。
- `UserTransactionSizeBits_g => C_M00_AXI_ADDR_WIDTH`：用户命令 `Cmd*_Size` 字段的位宽，这里取与地址同宽（32 位），意味着一条命令最多能表达 \( 2^{32} \) 个 beat，远大于实际需要。
- `DataFifoDepth_g => 1024`：master 内部数据 FIFO 的深度（单位是 beat）。写数据先进 FIFO 再上总线，读数据下总线先进 FIFO 再交给核心。1024 beat 深度对常见测试规模绰绰有余。注意它是写死在 wrapper 里的常量，**不是**顶层 generic，使用者无法在 IP GUI 里改。
- `ImplRead_g => true`、`ImplWrite_g => true`：同时实例化读通路与写通路（这个 IP 两者都要）。
- `RamBehavior_g => "RBW"`：内部 RAM 用「读优先写」（Read-Before-Write）行为，是 `psi_common` 的一个实现选项。

generic 的影响可整理成下表：

| generic | 默认值 | 约束的对象 | 调大的影响 |
|---------|--------|-----------|-----------|
| `AxiMaxBeats_g` | 16 | 单 burst 最大 beat 数 | 单次突发更长、地址通道开销更小，但要求被测存储器支持长突发 |
| `AxiMaxOpenTrasactions_g` | 2 | 在途 burst 数 | 流水线更深、吞吐更高，但占用更多缓冲与 outstanding 资源 |
| `DataFifoDepth_g` | 1024 | 数据缓冲深度（beat） | 能吸收更深的读写抖动，但耗用更多 BRAM |
| `UserTransactionSizeBits_g` | = ADDR 宽 | 命令 Size 字段位宽 | 单条命令可表达的 beat 数更大 |

#### 4.1.4 代码实践

**实践目标**：确认 generic 的透传链，并理解 `UserTransactionSizeBits_g` 为什么取地址宽度。

**操作步骤**：

1. 打开 [hdl/mem_test_wrapper.vhd:226-237](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd#L226-L237)，把每一行 generic 映射抄成一张「master generic ← wrapper generic ← 默认值」对照表。
2. 自问：`UserTransactionSizeBits_g => C_M00_AXI_ADDR_WIDTH` 这一行为什么不直接写一个数字，而要绑定到地址宽度？提示：一条命令的 beat 数 × 每 beat 字节数 ≤ 可寻址空间，所以「Size 字段位宽 ≥ 地址位宽 − log2(B)」即可覆盖全空间，这里直接取地址宽度是一种宽松但安全的取值。
3. 再问：`DataFifoDepth_g => 1024` 是写死的常量，使用者想改怎么办？答案是要修改 wrapper 源码并重新封装 IP，不像前两项能在 IP GUI 上配置（封装与 GUI 在 [u5-l2](u5-l2-ip-packaging.md) 讲）。

**需要观察的现象**：你会看到地址/数据宽度、burst、outstanding 四项是从顶层 generic 透传的，而 FIFO 深度是硬编码的——这反映了设计者认为前三项是「使用者关心的集成参数」，而 FIFO 深度是「实现细节」。

**预期结果**：一张 7 行的 generic 映射表，并能口头解释每一项约束的是 master 的哪一面。

#### 4.1.5 小练习与答案

**练习 1**：把 `C_M00_AXI_MAX_OPEN_TRANS` 从 2 调到 8，对一次大批量写测试的吞吐通常有什么影响？为什么？

> **答案**：在途 burst 数上限提高，master 可以在被测存储器还在处理前几个 burst 时，继续发出后续 burst 的地址握手，流水线并行度更高，吞吐通常上升；代价是 master 内部需要更深的缓冲与更多的 outstanding 跟踪资源，且要求被测存储器/互联接受这么多在途事务。

**练习 2**：`AxiMaxBeats_g=16` 时，一个 burst 的 `AWLEN`（AXI4 编码）应该是多少？

> **答案**：AXI4 的 `AWLEN` 编码为「beat 数 − 1」，所以 16 beat 对应 `AWLEN = 15 = 0x0F`。master 会自动生成这个值，核心无需关心。

---

### 4.2 命令/数据用户接口

#### 4.2.1 概念说明

master 暴露给核心的用户接口分成四组，全部是 valid/ready 握手：

| 组别 | 信号 | 方向（相对核心） | 含义 |
|------|------|------------------|------|
| 写命令 | `CmdWr_Addr/Size/LowLat/Vld/Rdy` | 核心驱动 Addr/Size/LowLat/Vld，master 驱动 Rdy | 「从 Addr 起写 Size 个 beat」|
| 读命令 | `CmdRd_Addr/Size/LowLat/Vld/Rdy` | 同上 | 「从 Addr 起读 Size 个 beat」|
| 写数据 | `WrDat_Data/Be/Vld/Rdy` | 核心驱动，master 驱动 Rdy | 写命令的数据流，beat 数 = `CmdWr_Size` |
| 读数据 | `RdDat_Data/Vld/Rdy` | master 驱动 Data/Vld，核心驱动 Rdy | 读命令返回的数据流，beat 数 = `CmdRd_Size` |

两个最容易踩坑的点：

1. **`Cmd*_Size` 的单位是 beat，不是字节**。寄存器里的 `SIZE` 是字节数，核心下发前必须先除以每 beat 字节数 \( B \)。
2. **核心采用「注册式 valid」握手**：先把 `Vld` 拉起并保持，下一拍起每拍检查 `r.Xxx_Vld='1' and Xxx_Rdy='1'`，握手成功后才撤 `Vld`。这意味着 valid 至少持续两拍（这是 [u3-l3](u3-l3-main-fsm.md) 讲过的状态机推进方式）。

#### 4.2.2 核心流程

一次「写」命令的生命周期：

```text
WrCmd_s:  装填 CmdWr_Addr（清低位对齐）、CmdWr_Size（字节→beat 换算）
          拉起 CmdWr_Vld，PatternCnt 清零，InitPattern 播种第 0 拍
          等待 r.CmdWr_Vld='1' and CmdWr_Rdy='1'  →  进入 Write_s
Write_s:  拉起 WrDat_Vld，把 r.Pattern 当作 WrDat_Data 流式送出
          每次 WrDat_Vld='1' and WrDat_Rdy='1'  →  PatternCnt++、UpdatePattern 推进序列
          直到 PatternCnt = CmdWr_Size-1（最后一拍）→ 进入 RdCmd_s（或 Idle_s/回写）
```

读路径结构对称：`RdCmd_s` 装填命令，`Read_s` 消费 `RdDat`。

字节到 beat 的换算是核心代码里最值得精读的一行。设数据宽为 \( W = \text{AxiDataWidth}_g \) 位，则每 beat 字节数：

\[ B = \frac{W}{8} \]

`SIZE` 寄存器持有字节数 \( S_{\text{bytes}} \)，要换算成 beat 数：

\[ \text{CmdSize}_{\text{beats}} = \left\lfloor \frac{S_{\text{bytes}}}{B} \right\rfloor = S_{\text{bytes}} \;\gg\; \log_2 B \]

这正是源码里 `shift_right(..., log2(AxiDataWidth_g/8))` 的由来——除以 \( B \) 用右移实现，因为 \( B \) 恒为 2 的幂。同理，地址的低 \( \log_2 B \) 位被清零，保证地址按 beat 对齐。

#### 4.2.3 源码精读

先看核心实体声明中的 AXI 主机用户接口端口（方向以核心为本位）：

[hdl/mem_test.vhd:40-61](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L40-L61) —— 可见 `CmdWr_Addr/Size`、`CmdRd_Addr/Size` 都是 `AxiAddrWidth_g` 位宽，数据 `WrDat_Data/RdDat_Data` 是 `AxiDataWidth_g` 位宽，`WrDat_Be` 是 `AxiDataWidth_g/8` 位宽（每字节一个 byte-enable）。注意 `Wr_Done`/`Rd_Done` 也在这里声明（后面 4.3 会用到）。

再看命令装配与换算的关键三行（以写命令为例，读命令完全对称）：

[hdl/mem_test.vhd:224-234](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L224-L234) —— 这是 `WrCmd_s` 状态的核心。三件事：

- 第 225-226 行把 64 位 `RegAddr_v` 截到命令位宽，并把低 `log2(AxiDataWidth_g/8)` 位清零，确保地址按 beat 对齐（master 不支持子 beat 寻址）。
- 第 227 行 `v.CmdWr_Size := shift_right(RegSize_v(...), log2(AxiDataWidth_g/8));` 就是上面公式里的「字节 → beat」换算。
- 第 228-230 行拉起 `CmdWr_Vld`、把 `PatternCnt` 清零、用 `InitPattern_v := true` 让共享段播种第 0 拍 pattern。
- 第 231-234 行实现「注册式 valid」握手判定，成功后进入 `Write_s`。

读命令的对应代码在 [hdl/mem_test.vhd:256-266](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L256-L266)，结构与写命令逐行对称。

写数据流的产生在 `Write_s`：

[hdl/mem_test.vhd:237-253](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L237-L253) —— 核心把 `r.Pattern` 直接当作写数据（见 4.2.3 末尾的输出赋值），每握手一次就 `PatternCnt+1` 并 `UpdatePattern`，直到 `r.PatternCnt = r.CmdWr_Size-1` 判定最后一拍。

最后看输出赋值，理解数据流如何落到端口上：

[hdl/mem_test.vhd:364-375](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L364-L375) —— 关键两句：`WrDat_Data <= std_logic_vector(r.Pattern);`（写数据就是当前 pattern）和 `WrDat_Be <= (others => '1');`（全部 byte-enable 拉高，即整字写入，不做部分字节写入）。另外 `CmdWr_LowLat <= '0'; CmdRd_LowLat <= '0';` 把低延迟提示恒定拉低——核心选择不要求 master 走低延迟路径。

这些用户接口信号在 wrapper 里通过内部信号接到 master 实例：

[hdl/mem_test_wrapper.vhd:242-262](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd#L242-L262) —— master 的用户命令/数据端口与 wrapper 内部信号同名直连，而这些内部信号又由 `i_logic` 驱动（见 [hdl/mem_test_wrapper.vhd:321-337](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd#L321-L337)）。所以核心 ↔ master 是点对点直连，没有中间逻辑。

#### 4.2.4 代码实践

**实践目标**：亲眼跟踪一次「字节 SIZE → beat CmdWr_Size」的换算，并理解地址对齐。

**操作步骤**：

1. 假设 IP 被配置成 `AxiDataWidth_g = 32`（即每 beat 4 字节，\( \log_2 4 = 2 \)）。
2. 在 [hdl/mem_test.vhd:227](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L227) 处，把 `shift_right(RegSize_v(...), log2(AxiDataWidth_g/8))` 展开成等价表达式 `RegSize_v / (AxiDataWidth_g/8)`。
3. 设想软件往 `SIZE` 寄存器（`REG_SIZE_LO/HI`）写入 `0x100`（256 字节），手算 `CmdWr_Size`：`256 / 4 = 64`，即核心会向 master 下达一条「64 个 beat」的写命令。
4. 再设想软件往 `ADDR` 写 `0x1000_000E`（低 2 位非零），对照 [hdl/mem_test.vhd:225-226](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L225-L226)，说明 `CmdWr_Addr` 实际变成多少。答：低 2 位被清零，得到 `0x1000_000C`。

**需要观察的现象**：你会看到换算完全靠移位完成，没有除法器；地址总是向下对齐到 beat 边界。

**预期结果**：能写出 `CmdSize_beats = SIZE_bytes >> log2(AxiDataWidth_g/8)` 这个等式，并解释为什么用移位而非除法（综合出移位是零成本组合逻辑，除法器则昂贵）。

> 待本地验证：若你在 Modelsim 里用默认 64 位数据宽跑 [tb/top_tb.vhd](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/tb/top_tb.vhd)，可在波形里抓 `CmdWr_Size` 与软件写入的 `SIZE` 对比，确认 `CmdWr_Size = SIZE/8`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `WrDat_Be` 恒为全 1？如果改成部分字节写入，会对错误检测有什么影响？

> **答案**：内存测试要求每个 beat 写满整个字，再整字读回比对，所以 byte-enable 全 1。若部分字节不写，读回时这些字节的内容取决于存储器原有值，会导致正常存储器也「比对失败」，错误统计失去意义。

**练习 2**：核心用 `r.PatternCnt = r.CmdWr_Size-1` 判定最后一拍。为什么 master 侧的 `Wr_Done` 不用在这里？

> **答案**：`Wr_Done` 表示整条命令（可能含多个 burst）在总线上彻底完成（收到 B 响应），它滞后于数据流的最后一拍。核心要在「最后一个数据 beat 送出」时就推进状态机，所以必须用自己的 `PatternCnt` 本地计数，而不能等 `Wr_Done`。`Wr_Done` 的角色见 4.3。

---

### 4.3 响应信号与错误反馈

#### 4.3.1 概念说明

master 提供四个响应信号回送给核心：

| 信号 | 含义 |
|------|------|
| `Wr_Done` | 写命令全部 burst 完成（B 通道收到成功响应）|
| `Wr_Error` | 写命令收到出错的 B 响应（如 SLVERR/DECERR）|
| `Rd_Done` | 读命令全部 burst 完成（R 通道收完最后一拍且无错）|
| `Rd_Error` | 读命令收到出错的 R 响应 |

核心 FSM 对这四个信号的使用并不对称——这是一个值得注意的设计取舍：

- **`Wr_Error` / `Rd_Error` 被使用**：任何一个拉高，核心立刻跳进不可恢复的 `AxiError_s` 状态，对外 `STATUS` 报告 `C_STATUS_AXIERR`，只有硬件复位能退出。
- **`Wr_Done` / `Rd_Done` 实际未被 FSM 使用**：核心用本地 `PatternCnt` 数 beat 来判定何时推进状态，不依赖 master 的完成信号。这两个端口在实体上连着（信号名存在），但 `p_comb` 的敏感表与函数体都不引用它们。

这种「本地数 beat + 只信错误信号」的设计带来两个好处：一是状态推进不被总线往返延迟拖累（不必等 B 响应才开始读），二是错误仍能被可靠捕获。

#### 4.3.2 核心流程

错误反馈的控制流：

```text
每拍共享段（case 之后）:
   若 r.Fsm = AxiError_s  → 自锁 AxiError_s   (不可恢复)
   若 r.Fsm = IntError_s  → 自锁 IntError_s   (不可恢复)
   若 Wr_Error='1' 或 Rd_Error='1'  → v.Fsm := AxiError_s   (新错误陷阱)

对外报告:
   纯函数 FsmToInt(AxiError_s) = C_STATUS_AXIERR (= 3)
   该值每拍组合地写入 REG_STATUS，供 CPU 读取
```

「自锁」的含义：`AxiError_s`/`IntError_s` 是吸收态（absorbing state），一旦进入，每拍都把 `v.Fsm` 重新钉回自己，case 分支里只写 `null;`（什么都不做）。只有 `Rst='1'` 才能在 `p_reg` 里把 `Fsm` 复位回 `Idle_s`。

#### 4.3.3 源码精读

错误陷阱的两个吸收态：

[hdl/mem_test.vhd:298-306](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L298-L306) —— `AxiError_s` 与 `IntError_s` 分支都是 `null;`，表示进入后什么都不做。

case 之后的共享段实现「自锁 + 错误捕获」：

[hdl/mem_test.vhd:349-358](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L349-L358) —— 关键三段：

- 第 350-352 行：若当前已是 `IntError_s`，则保持 `IntError_s`。
- 第 353-355 行：若当前已是 `AxiError_s`，则保持 `AxiError_s`。
- 第 356-358 行：**只要 `Wr_Error='1'` 或 `Rd_Error='1'`，就把状态打成 `AxiError_s`**。这就是 master 总线错误反馈到核心 FSM 的唯一入口。

`STATUS` 寄存器每拍组合地反映 FSM 状态：

[hdl/mem_test.vhd:172-174](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L172-L174) —— `Reg_RData(REG_STATUS)` 由 `FsmToInt(r.Fsm)` 填充。

`FsmToInt` 把内部 7 个细状态映射成对外 6 个粗状态码，其中 `AxiError_s → C_STATUS_AXIERR`：

[hdl/mem_test.vhd:77-89](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L77-L89) —— 注意 `AxiError_s` 映射到码 3，`IntError_s` 映射到码 6，对应 [hdl/mem_test_pkg.vhd:56-63](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_pkg.vhd#L56-L63) 里的 `C_STATUS_AXIERR(3)` 与 `C_STATUS_INTERR(6)`。CPU 读到 3 就知道是总线报错，读到 6 就知道是核心内部错误（如非法 pattern），据此区分故障来源。

`p_reg` 的复位段说明只有复位能退出错误态：

[hdl/mem_test.vhd:380-393](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L380-L393) —— 复位时 `r.Fsm <= Idle_s`，且只清各握手信号，不碰统计字段（统计靠 START 流程软清零，详见 [u3-l2](u3-l2-core-entity-and-two-process.md)）。

最后确认 `Wr_Done/Rd_Done` 确实未被组合逻辑引用——看 `p_comb` 的敏感表：

[hdl/mem_test.vhd:127-128](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L127-L128) —— 敏表里只有 `Wr_Error, Rd_Error`，没有 `Wr_Done, Rd_Done`。这从代码层面证实了「Done 信号未参与状态机决策」。wrapper 里它们仍被连线（[hdl/mem_test_wrapper.vhd:264-267](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test_wrapper.vhd#L264-L267)），保留给将来可能的扩展使用。

#### 4.3.4 代码实践

**实践目标**：验证「只有 `Wr_Error/Rd_Error` 参与 FSM，`Wr_Done/Rd_Done` 不参与」这一结论。

**操作步骤**：

1. 打开 [hdl/mem_test.vhd](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd)，在全文搜索 `Wr_Done` 与 `Rd_Done`（用编辑器查找，不要改源码）。
2. 记录每次出现的位置。你会看到：实体端口声明（第 58、60 行）、wrapper 连线（在 wrapper 文件里），但 `p_comb`/`p_reg` 的进程体内**没有任何引用**。
3. 再搜索 `Wr_Error`、`Rd_Error`，对比出现位置——它们在第 127 行敏感表与第 356 行判断里都出现了。
4. 自问：如果将来想在「写完成后立即读回」之间插入一个等 `Wr_Done` 的状态，需要改哪几处？提示：在 `Fsm_t` 加一个状态、在敏感表加 `Wr_Done`、在该状态分支里写 `if Wr_Done='1' then v.Fsm := RdCmd_s;`。

**需要观察的现象**：`Done` 信号的引用计数为 0（进程体内），`Error` 信号的引用计数 ≥ 2。

**预期结果**：能口头陈述「核心靠本地数 beat 推进，靠 Error 信号捕获总线故障，Done 信号当前是预留」。

> 待本地验证：若跑仿真，可在 testbench 里故意让 AXI 仿真从机返回一个 `BRESP=SLVERR`（见 [u5-l1](u5-l1-testbench-and-axi-emulation.md) 的错误注入方法），观察核心 `STATUS` 是否跳到 `C_STATUS_AXIERR(3)` 且不再回到 `IDLE`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `AxiError_s` 设计成不可恢复（只有复位能退出），而不是出错后自动重试？

> **答案**：内存测试一旦遇到总线错误，通常意味着被测存储器子系统（地址译码、互联、控制器）出了结构性问题，重试大概率仍会失败且可能掩盖问题。停机并报错让 CPU/开发者介入排查，是更安全的选择。这也让错误统计（`ERRORS`、`FIRSTERR`）定格在故障时刻，便于诊断。

**练习 2**：`Wr_Error` 和 `Rd_Error` 用「或」关系触发同一个 `AxiError_s`。这样会丢失「是写错还是读错」的信息吗？

> **答案**：从 `STATUS` 寄存器看会丢失（都报 `C_STATUS_AXIERR`）。但结合运行模式可以反推：若用 `WRITEONLY` 模式触发，错误必来自写；若用 `READONLY` 或在写阶段已完成后再报错，多半来自读。需要精确定位时，可分别用这两种模式跑两次来区分。

---

## 5. 综合实践

把本讲三处要点（字节→beat 换算、burst 拆分、错误反馈）串成一个端到端推演。

**任务**：解释核心逻辑中 `CmdWr_Size` 为何要做 `shift_right(..., log2(AxiDataWidth_g/8))`，并结合 master 的 burst 行为，推算一次 `SIZE=0x100` 字节、数据宽 32 位的测试会发起多少个 burst、每个 burst 多少 beat。

**步骤 1 —— 解释 shift_right 的必要性**。

`SIZE` 寄存器持有的是**字节数**（软件按字节编程），而 master 的 `Cmd*_Size` 字段以 **beat** 为单位（master 按 beat 组帧 burst）。两者之间的换算系数就是每 beat 的字节数 \( B = \text{AxiDataWidth}_g / 8 \)。因为 \( B \) 恒为 2 的幂，除以 \( B \) 等价于右移 \( \log_2 B \) 位，于是有（见 [hdl/mem_test.vhd:227](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L227)）：

\[ \text{CmdWr\_Size} = \text{SIZE}_{\text{bytes}} \;\gg\; \log_2\!\left(\frac{\text{AxiDataWidth}_g}{8}\right) \]

用移位而非除法，是因为综合时移位是零成本的连线重排，而除法器是昂贵的组合逻辑。

**步骤 2 —— 代入数字算 beat 数**。

`SIZE = 0x100 = 256` 字节，`AxiDataWidth_g = 32` → \( B = 4 \) 字节/beat，\( \log_2 4 = 2 \)：

\[ \text{CmdWr\_Size} = 256 \gg 2 = 64 \;\text{beat} \]

所以核心向 master 下达一条「64 个 beat」的写命令（随后是一条对称的读命令）。

**步骤 3 —— 结合 master 的 burst 拆分**。

master 受 `AxiMaxBeats_g`（= `C_M00_AXI_MAX_BURST_SIZE`，默认 16）约束：单个 burst 最多 16 个 beat。它会把这条 64-beat 的命令拆成多个 burst 发上 `M00_AXI`：

\[ N_{\text{burst}} = \left\lceil \frac{64}{16} \right\rceil = 4 \;\text{个 burst} \]

每个 burst 恰好 16 beat（因为 64 是 16 的整数倍），对应 AXI4 的 `AWLEN = 16 - 1 = 15`。

**步骤 4 —— 连到错误反馈**。

这 4 个写 burst 中，只要任何一个的 B 响应被 master 翻译成 `Wr_Error='1'`，核心 [hdl/mem_test.vhd:356-358](https://github.com/paulscherrerinstitute/vivadoIP_mem_test/blob/756fa79f36c7360e4045c35e036a50eb5c3cc679/hdl/mem_test.vhd#L356-L358) 立刻把 FSM 钉到 `AxiError_s`，`STATUS` 报 `C_STATUS_AXIERR(3)`，测试停机。

**最终答案表**：

| 量 | 值 |
|----|----|
| `SIZE`（字节） | 256 (0x100) |
| 每 beat 字节数 \( B \) | 4 |
| `CmdWr_Size`（beat） | 64 |
| `AxiMaxBeats_g`（默认） | 16 |
| burst 数 | 4 |
| 每 burst beat 数 | 16 |
| 每 burst 的 `AWLEN` | 15 (0x0F) |

> 待本地验证：burst 的拆分粒度依赖 `psi_common_axi_master_simple` 的实现（在 `psi_common` 库中）。上表依据 generic 命名（`AxiMaxBeats_g`）与 AXI 标准推断；若在真实总线上抓 `m00_axi_awlen`，应看到 4 次握手、每次 `awlen=0x0F`。换用默认的 64 位数据宽时，`CmdWr_Size = 256/8 = 32` beat，被拆成 2 个 16-beat burst。

## 6. 本讲小结

- `i_master`（`psi_common_axi_master_simple`）是核心高层语义与 AXI4 总线之间的翻译层：核心只懂「地址 + beat 数 + 数据流」，master 负责五通道、burst 拆分、在途事务管理与数据缓冲。
- master 的关键 generic 通过 wrapper 顶层透传：`AxiMaxBeats_g` 约束单 burst 上限、`AxiMaxOpenTrasactions_g` 约束在途 burst 数、`DataFifoDepth_g` 约束缓冲深度（写死为 1024，不可在 GUI 配置）。
- 用户接口分四组（写命令/读命令/写数据/读数据），全部 valid/ready 握手；**`Cmd*_Size` 单位是 beat**，核心下发前用 `shift_right(SIZE, log2(AxiDataWidth_g/8))` 把字节数换算成 beat 数，并把地址低 `log2(B)` 位清零对齐。
- 写数据就是当前 pattern，`WrDat_Be` 全 1（整字写入）；`LowLat` 恒为 0。
- 四个响应信号里只有 `Wr_Error/Rd_Error` 被 FSM 使用：任一拉高即跳进不可恢复的 `AxiError_s`，对外报 `C_STATUS_AXIERR(3)`，只有复位能退出。
- `Wr_Done/Rd_Done` 当前未被 `p_comb` 引用（不在敏感表）；核心用本地 `PatternCnt` 数 beat 来推进状态，不依赖 master 的完成信号。

## 7. 下一步学习建议

- 想看「核心如何在一个测试周期里把这套主机接口用满」，可重读 [u3-l3（主状态机）](u3-l3-main-fsm.md)，对照本讲的命令/数据接口理解每个状态在总线上对应什么动作。
- 想验证 burst 拆分与错误注入的实际波形，进入 [u5-l1（仿真平台与 AXI 仿真过程）](u5-l1-testbench-and-axi-emulation.md)，看 `tb/top_tb.vhd` 里的 `p_axi` 进程如何仿真 AXI4 从机、注入 read 错误。
- 想了解这些 `C_M00_AXI_*` generic 如何在 Vivado IP GUI 里暴露给使用者，进入 [u5-l2（Vivado IP 封装流程）](u5-l2-ip-packaging.md)。
- 对 `psi_common_axi_master_simple` 的内部状态机、FIFO 实现与精确拆 burst 规则感兴趣的读者，建议到外部依赖 `psi_common` 仓库阅读其源码与文档（获取方式见 [u1-l2](u1-l2-repo-structure-and-dependencies.md)）。
