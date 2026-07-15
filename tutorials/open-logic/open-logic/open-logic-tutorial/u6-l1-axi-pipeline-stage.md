# AXI 流水线阶段（olo_axi_pl_stage）

## 1. 本讲目标

本讲围绕 Open Logic axi 区域的第一个实体 `olo_axi_pl_stage` 展开。学完后你应当能够：

- 说清「为什么要在 AXI 接口里插一排寄存器」，以及它解决的是哪类时序问题。
- 看懂 `olo_axi_pl_stage` 如何把 AXI4 的五个通道分别打包、寄存、再拆包。
- 理解为什么同一个实体既能用于完整 AXI4，也能直接用于 AXI4-Lite。
- 把 `olo_axi_pl_stage` 与第 u2-l2 讲学过的 `olo_base_pl_stage` 串起来，理解「包装（wrapper）」这一复用模式。
- 独立实例化该实体并跑通仿真，验证「功能不变、延迟增加」。

## 2. 前置知识

在进入源码前，先建立两点直觉。本讲承接 u2-l2（`olo_base_pl_stage` 与 AXI-S 握手），如果你对「两进程法」「shadow 寄存器」「Ready 反压」还不熟，建议先复习那一讲。

### 2.1 AXI4 接口有五个独立通道

AXI4 是一种总线协议，一次读写不是一根线搞定，而是拆成五条**独立握手的通道**：

| 通道 | 方向（Slave→Master） | 作用 |
| :--- | :--- | :--- |
| AW（写地址） | 请求，Slave → Master | 给出「要写到哪个地址」 |
| W（写数据） | 请求，Slave → Master | 给出「写什么数据」 |
| B（写响应） | 响应，Master → Slave | 回报「写完了，成功否」 |
| AR（读地址） | 请求，Slave → Master | 给出「要从哪个地址读」 |
| R（读数据） | 响应，Master → Slave | 返回「读到的数据」 |

其中 AW/W/AR 是**请求通道**（从前级流向后级），B/R 是**响应通道**（从后级流回前级）。每条通道都有自己的 `Valid`/`Ready` 握手对，外加若干数据/控制信号。这点很重要：`olo_axi_pl_stage` 不是「整体寄存一拍」，而是「每条通道各自寄存」。

> 关键术语：**通道（channel）**是 AXI 里一组同步握手信号的集合；**Slave/Master** 在这里指本实体的左侧（S_ 前缀）和右侧（M_ 前缀）端口，不是真正的主从设备。

### 2.2 关键路径常常出在 Ready 上

很多人以为「寄存一拍」只是为了寄存数据。但在 AXI/AXI-Stream 里，更危险的是 **Ready 路径**。下游的 `Ready` 往往是**组合转发**的——例如它可能等于「FIFO 非满」或「一堆源的选择结果」。于是从 Master 一路 `Ready` 反传回 Slave 的这条组合链可能横跨很深，成为时序的**关键路径（critical path）**。

`olo_base_pl_stage`（u2-l2）的核心价值正是：**把 Ready 也寄存一拍**，从而切断这条组合长链，代价是引入 shadow 寄存器来避免反压瞬间丢数据。`olo_axi_pl_stage` 把这个能力直接套到 AXI 的五条通道上。

## 3. 本讲源码地图

| 文件 | 作用 |
| :--- | :--- |
| [src/axi/vhdl/olo_axi_pl_stage.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_pl_stage.vhd) | 本讲主角：AXI4 全接口流水线寄存器，五个 `block` 各管一条通道。 |
| [doc/axi/olo_axi_pl_stage.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/axi/olo_axi_pl_stage.md) | 官方文档，说明泛型、接口与「基于 olo_base_pl_stage」的架构。 |
| [src/base/vhdl/olo_base_pl_stage.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd) | 被复用的基础积木：单条 AXI-S 握手流水线寄存器（u2-l2 已详解）。 |
| [test/axi/olo_axi_pl_stage/olo_axi_pl_stage_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/axi/olo_axi_pl_stage/olo_axi_pl_stage_tb.vhd) | 配套测试台，用 AXI 主/从验证组件（VC）在各种反压下验证穿透正确性。 |

## 4. 核心概念与源码讲解

本讲拆为四个最小模块：时序优化用途、五通道信号分组寄存、AXI4-Lite 复用、与 base pl_stage 的关系。

### 4.1 时序优化用途：为什么要寄存整个 AXI 接口

#### 4.1.1 概念说明

把一段很长的组合逻辑用寄存器切成几段，是提升时钟频率的标准手段。AXI 接口尤其需要它，原因有二：

1. **信号数量极多**：一条 AXI4 接口动辄上百位（地址、数据、Len、Size、Burst、Cache、Prot、Qos、Region、Id、User……）。如果不寄存，这些信号会与下游逻辑直接组合，扇出大、路径长。
2. **Ready 是组合反传的**：如前置知识所述，AXI 的 `Ready` 常被异步转发，是关键路径的高发区。

`olo_axi_pl_stage` 的职责就是：**在 AXI 主从之间插入若干级寄存器，把数据、控制、Valid、Ready 全部寄存一遍**，从而把一条「很胖、且 Ready 反传」的接口彻底切断成两段。

#### 4.1.2 核心流程

- 接收一组泛型：地址/数据/Id/User 宽度与寄存级数 `Stages_g`。
- 对五条通道各实例化一个内部流水线寄存器，级数由 `Stages_g` 统一控制。
- 请求类通道（AW/W/AR）：数据与握手从 S_ 侧推进到 M_ 侧。
- 响应类通道（B/R）：数据与握手从 M_ 侧推进到 S_ 侧。
- 每条通道的 Ready 都被寄存，反压路径被切断；功能（协议行为）不变，代价是每条通道的穿透延迟增加 `Stages_g` 拍。

理想情况下（无反压），某条通道的有效信号在输出侧比输入侧晚出现 `Stages_g` 个时钟周期。对一次完整读事务而言，延迟会叠加在 AR（请求）和 R（响应）两条通道上。

#### 4.1.3 源码精读

实体的泛型定义里，前四个控制各信号宽度，`Stages_g` 控制寄存级数：

[olo_axi_pl_stage.vhd:29-35](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_pl_stage.vhd#L29-L35) —— 定义 `AddrWidth_g / DataWidth_g / IdWidth_g / UserWidth_g / Stages_g` 五个泛型，`Stages_g` 默认为 1。

文档直接点明了设计意图——「The component registers all signals of the interface」：

[olo_axi_pl_stage.md:17-20](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/axi/olo_axi_pl_stage.md#L17-L20) —— 说明本组件寄存接口的所有信号，且可用于 AXI4-Lite（把不用的信号留空即可）。

「寄存 Ready 以切断关键路径」这一动机来自底层 `olo_base_pl_stage` 的注释，`olo_axi_pl_stage` 只是把它套到五条通道上：

[olo_base_pl_stage.vhd:10-13](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd#L10-L13) —— 解释 Ready 常被异步转发、会形成长组合链，所以必须把包括 Ready 在内的所有信号双向寄存。

#### 4.1.4 代码实践

**实践目标**：用现成测试台确认「经过寄存后 AXI 功能不变」。

**操作步骤**：

1. 进入仿真目录（仓库根下）：
   ```bash
   cd sim
   ```
2. 只跑 `olo_axi_pl_stage` 测试台里的「单次写」用例（默认用 GHDL）：
   ```bash
   python run.py --ghdl "*olo_axi_pl_stage_tb*SingleWrite*"
   ```
3. 再跑「单次读」与「同时读写」两个用例：
   ```bash
   python run.py --ghdl "*olo_axi_pl_stage_tb*SingleRead*"
   python run.py --ghdl "*olo_axi_pl_stage_tb*ReadAndWrite*"
   ```

**需要观察的现象**：每个用例都应 **pass（通过）**。测试台在 `olo_axi_pl_stage_tb.vhd:121-144` 用主端 VC（`push_single_write` / `expect_single_read`）发起事务、从端 VC（`expect_single_write` / `push_single_read`）检查结果，二者数据完全一致即说明寄存器没有篡改任何 AXI 语义。

**预期结果**：所有用例 `Status: pass`。若未本地装好 GHDL/VUnit，则命令行为 **待本地验证**，但测试逻辑（VC 对拍）可从源码读出。

#### 4.1.5 小练习与答案

**练习 1**：如果不寄存 Ready、只寄存数据/Valid，反压路径会怎样？
**答案**：Ready 仍从下游组合反传到上游，关键路径没有被切断，时序优化目的落空——这正是 `olo_base_pl_stage` 要把 Ready 也寄存的原因。

**练习 2**：为什么 `olo_axi_pl_stage` 不直接整体寄存一拍，而要按通道分别寄存？
**答案**：AXI 五条通道独立握手、各自有不同的 Valid/Ready 时序与数据流向（请求 vs 响应），只能分别寄存；整体一刀切无法正确表达通道间的协议关系。

---

### 4.2 五通道信号分组与打包（AXI4 信号分组寄存）

#### 4.2.1 概念说明

`olo_axi_pl_stage` 的实现难点不是「寄存」本身，而是**如何把一条通道里十几个零散信号喂给同一个流水线寄存器**。`olo_base_pl_stage` 只接受一个 `In_Data : std_logic_vector`，所以必须先把一条通道的所有信号**拼接（pack）进一个大向量**，过完寄存器再**拆开（unpack）**回各个具名端口。

为此每条通道用一个 VHDL `block` 封装，内部用一组 `subtype ... range` 逐一分配每个信号在大向量里的比特位置。这样无论地址/数据/Id/User 宽度怎么变，打包向量的总宽都能自动算出来。

#### 4.2.2 核心流程

以写地址通道 AW 为例：

1. 用一连串 `subtype` 声明每个信号在大向量中的位段，累加得到最末段的上界即为向量总宽。
2. 把各 S_ 端口信号按位段塞进 `AwDataIn`。
3. 实例化一个 `olo_base_pl_stage`，`Width_g` 取 `AwDataIn'length`。
4. 从输出 `AwDataOut` 按同样位段拆回各 M_ 端口。
5. 请求通道（AW/W/AR）：`In_` 接 S_、`Out_` 接 M_；响应通道（B/R）反过来：`In_` 接 M_、`Out_` 接 S_。

位段宽度的计算本质是一串加法：每段宽度 = 该信号端口宽度（`S_AwXxx'length`），段上界 = 前一段上界 + 本段宽度。

#### 4.2.3 源码精读

AW 通道用 `subtype` 链计算各信号位段，从最末一个 `AwRegionRng_c` 的上界得到打包向量总宽：

[olo_axi_pl_stage.vhd:153-166](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_pl_stage.vhd#L153-L166) —— 用一串 `subtype ... is natural range ...` 为 AwProt/AwCache/AwLock/AwBurst/AwSize/AwLen/AwAddr/AwId/AwQos/AwUser/AwRegion 逐段分配比特位置，并据此声明打包向量 `AwDataIn/AwDataOut`。

把各端口塞进打包向量（注意单 bit 的 `AwLock` 用 `constant ... : natural` 记录其下标）：

[olo_axi_pl_stage.vhd:169-179](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_pl_stage.vhd#L169-L179) —— `AwDataIn(段) <= S_AwXxx`，完成请求侧（S_）到打包向量的映射。

实例化基础流水线寄存器（请求通道：`In_`→S_、`Out_`→M_）：

[olo_axi_pl_stage.vhd:182-197](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_pl_stage.vhd#L182-L197) —— 一个 `olo_base_pl_stage`，`Width_g` 取打包向量宽度，`UseReady_g => true`，`Stages_g => Stages_g`；`In_Valid=>S_AwValid`、`In_Ready=>S_AwReady`、`Out_Valid=>M_AwValid`、`Out_Ready=>M_AwReady`。

输出侧按相同位段拆回 M_ 端口：

[olo_axi_pl_stage.vhd:200-210](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_pl_stage.vhd#L200-L210) —— `M_AwXxx <= AwDataOut(段)`，完成打包向量到主侧端口的反映射。

响应通道方向相反，以写响应 B 为例（注意 `In_` 接 M_、`Out_` 接 S_）：

[olo_axi_pl_stage.vhd:263-283](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_pl_stage.vhd#L263-L283) —— B 通道把 `M_BId/M_BResp/M_BUser` 打包，过寄存器后拆到 `S_B*`；端口映射里 `In_Valid=>M_BValid`、`Out_Ready=>S_BReady`，证明响应是从 M 流向 S。

> 读数据通道 R 同理（`b_r` 块），把 `M_R*` 打包、过寄存器、拆到 `S_R*`，方向也是 M→S。AW/W/AR 三个块则是 S→M。**记住方向：请求通道向前（S→M），响应通道向后（M→S）。**

#### 4.2.4 代码实践

**实践目标**：通过阅读源码确认五条通道的方向。

**操作步骤**：

1. 打开 `src/axi/vhdl/olo_axi_pl_stage.vhd`，定位四个 `block`：`b_aw`（L153 起）、`b_w`（L214 起）、`b_b`（L255 起）、`b_ar`（L292 起）、`b_r`（L354 起）。
2. 对每个块，只看其中 `i_pl` 实例的端口映射，回答：`In_Valid` 接的是 `S_*` 还是 `M_*`？

**需要观察的现象 / 预期结果**：

| 通道 | `In_Valid` 来源 | 方向 |
| :--- | :--- | :--- |
| AW / W / AR | `S_*Valid` | 请求，S→M |
| B / R | `M_*Valid` | 响应，M→S |

**预期结果**：你能用一句话总结「请求通道 In 接 Slave、响应通道 In 接 Master」。这是源码阅读型实践，**无需运行**即可完成。

#### 4.2.5 小练习与答案

**练习 1**：为什么用 `subtype ... natural range` 而不是手写 ` downto ` 常量来标位段？
**答案**：因为各信号宽度由泛型决定（如 `S_AwAddr'length = AddrWidth_g`），用 `subtype` 引用 `'length` 可让位段随泛型自动伸缩，避免手算出错。

**练习 2**：如果把 `IdWidth_g` 设为 0，`AwIdRng_c` 会变成什么？
**答案**：`S_AwId'length = 0`，对应位段退化为空区间（上界 < 下界），对打包向量贡献 0 比特——这正是「不需要 Id 时自动省掉 Id 信号」的实现机理。

---

### 4.3 AXI4-Lite 复用：靠默认值把子集当全集用

#### 4.3.1 概念说明

AXI4-Lite 是 AXI4 的**精简子集**：每笔读写都是单拍（无突发），写地址/读地址通道只保留 `Addr`、`Prot`、`Valid`、`Ready`，去掉了 `Len/Size/Burst/Lock/Cache/Qos/Region/Id/User` 等信号；写数据通道没有 `Last`，但因为是单拍，逻辑上 `Last` 恒为 1。

`olo_axi_pl_stage` 是按**完整 AXI4** 声明端口的，却能直接当 AXI4-Lite 用，关键在于：**所有 AXI4 专有的可选端口都带默认值**。当你在 AXI4-Lite 场景下不去连接这些端口时，它们自动取默认值，而这些默认值恰好等价于「AXI4-Lite 单拍事务」的语义。

#### 4.3.2 核心流程

- 把不需要的 AXI4 信号端口**留空（不连接）**，让其取默认值。
- 默认值设计为「单拍、固定突发、无特权」：地址类附加信号全 0、`WLast`/`RLast` 默认 1、`Prot` 默认 0。
- 于是同一份 RTL 既覆盖完整 AXI4，也覆盖 AXI4-Lite，无需两套实体。

> 注意（承接 u1-l3）：默认值只在 **VHDL 实例化**时生效。若从 Verilog/SystemVerilog 实例化本实体，**不可依赖默认值**，所有输入端口都要显式赋值。

#### 4.3.3 源码精读

写地址通道里，所有 AXI4 专有信号都带默认值（`:= (others => '0')` 或 `'0'`）：

[olo_axi_pl_stage.vhd:43-55](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_pl_stage.vhd#L43-L55) —— `S_AwId/S_AwLen/S_AwSize/S_AwBurst/S_AwLock/S_AwCache/S_AwProt/S_AwQos/S_AwUser/S_AwRegion` 全部带默认值，留空即取默认。

两个「单拍语义」的关键默认值：

[olo_axi_pl_stage.vhd:61](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_pl_stage.vhd#L61) —— `S_WLast : in std_logic := '1'`，写数据 Last 默认 1，正好对应 AXI4-Lite 每次写都是单拍。

[olo_axi_pl_stage.vhd:140](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_pl_stage.vhd#L140) —— `M_RLast : in std_logic := '1'`，读数据 Last 默认 1，对应 AXI4-Lite 每次读都是单拍。

文档明确点出这一复用方式：

[olo_axi_pl_stage.md:19-20](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/axi/olo_axi_pl_stage.md#L19-L20) —— 「the component can be used for AXI4-Lite by simply leaving all unused signals unconnected」。

#### 4.3.4 代码实践

**实践目标**：手写一段把 `olo_axi_pl_stage` 当作 AXI4-Lite 流水线寄存器的实例化代码。

下面的代码是**示例代码**（非仓库原有文件），展示如何只连接 AXI4-Lite 存在的信号、其余靠默认值：

```vhdl
-- 示例代码：将 olo_axi_pl_stage 当作 AXI4-Lite 流水线寄存器
i_axil_pl : entity olo.olo_axi_pl_stage
    generic map (
        AddrWidth_g => 32,
        DataWidth_g => 32,
        Stages_g    => 1
        -- IdWidth_g / UserWidth_g 保持默认 0：不实例化 AXI4 专有的 Id/User
    )
    port map (
        Clk => Clk,
        Rst => Rst,

        -- 写地址（AXI4-Lite 只关心 Addr/Prot/Valid/Ready，其余 Aw* 留空取默认）
        S_AwAddr  => s_axil_awaddr,
        S_AwProt  => s_axil_awprot,
        S_AwValid => s_axil_awvalid,
        S_AwReady => s_axil_awready,
        M_AwAddr  => m_axil_awaddr,
        M_AwProt  => m_axil_awprot,
        M_AwValid => m_axil_awvalid,
        M_AwReady => m_axil_awready,

        -- 写数据（不连 WLast，默认 '1' 即单拍）
        S_WData   => s_axil_wdata,
        S_WStrb   => s_axil_wstrb,
        S_WValid  => s_axil_wvalid,
        S_WReady  => s_axil_wready,
        M_WData   => m_axil_wdata,
        M_WStrb   => m_axil_wstrb,
        M_WValid  => m_axil_wvalid,
        M_WReady  => m_axil_wready,

        -- 写响应
        S_BResp   => s_axil_bresp,  S_BValid => s_axil_bvalid, S_BReady => s_axil_bready,
        M_BResp   => m_axil_bresp,  M_BValid => m_axil_bvalid, M_BReady => m_axil_bready,

        -- 读地址
        S_ArAddr  => s_axil_araddr, S_ArProt => s_axil_arprot,
        S_ArValid => s_axil_arvalid, S_ArReady => s_axil_arready,
        M_ArAddr  => m_axil_araddr, M_ArProt => m_axil_arprot,
        M_ArValid => m_axil_arvalid, M_ArReady => m_axil_arready,

        -- 读数据（不连 RLast，默认 '1' 即单拍）
        S_RData   => s_axil_rdata,  S_RResp => s_axil_rresp,
        S_RValid  => s_axil_rvalid, S_RReady => s_axil_rready,
        M_RData   => m_axil_rdata,  M_RResp => m_axil_rresp,
        M_RValid  => m_axil_rvalid, M_RReady => m_axil_rready
        -- 所有 AwId/AwLen/AwSize/AwBurst/AwCache/AwQos/AwRegion/AwUser/Ar*同理 留空
    );
```

**需要观察的现象**：综合/编译时不应报「未连接端口」错误，因为它们都有默认值；功能上等效于一个 AXI4-Lite 的寄存器排。

**预期结果**：代码可编译通过；若在仿真中接入真实 AXI4-Lite 主从，单拍读写行为与不插寄存器时一致，仅延迟增加。该片段未在仓库中预置测试，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `S_WLast` 的默认值是 `'1'` 而不是 `'0'`？
**答案**：AXI4-Lite 每次写都是单拍，单拍事务的 `Last` 恒为 1，所以默认 `'1'` 才能让 AXI4-Lite 用户直接留空它。

**练习 2**：在 AXI4-Lite 场景下留空的 `S_AwLen`（默认全 0）对应 AXI4 的什么含义？
**答案**：`Len=0` 表示突发长度为 1（单拍传输），与 AXI4-Lite 单拍写语义一致——默认值被精心选成「AXI4-Lite 等价语义」。

---

### 4.4 与 base pl_stage 的关系：包装与级联展开

#### 4.4.1 概念说明

`olo_axi_pl_stage` 本身**不做任何寄存逻辑**，它是一个**包装器（wrapper）**：真正的寄存、反压、shadow 寄存器全部由 `olo_base_pl_stage`（u2-l2）实现。`olo_axi_pl_stage` 的全部工作就是「把 AXI 五条通道的信号打包 → 调用 base pl_stage → 拆包」。

这种「瘦包装 + 通用积木」的分层是 Open Logic 的典型复用模式：底层 `olo_base_pl_stage` 只懂一条 AXI-Stream 通道，上层 `olo_axi_pl_stage` 负责把「AXI4 这个胖协议」适配到底层那条窄接口上。

#### 4.4.2 核心流程

- `Stages_g` 透传给每个内部 `olo_base_pl_stage` 实例。
- `olo_base_pl_stage` 内部：若 `Stages_g > 0`，用 `for generate` 把 `Stages_g` 个单级寄存器串联；若 `Stages_g = 0`，退化为纯直通（组合穿越、延迟为 0）。
- 因此 `olo_axi_pl_stage` 的 `Stages_g` 同样支持 0（直通，等于不插寄存器但保留接口）和多级（更深缓冲、更彻底切断关键路径）。

#### 4.4.3 源码精读

文档直接说明本实体「基于 `olo_base_pl_stage`，五条通道各用一个」：

[olo_axi_pl_stage.md:51-53](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/axi/olo_axi_pl_stage.md#L51-L53) —— 明确架构：基于 `olo_base_pl_stage`，AXI4 的每条通道各放一个。

底层 `olo_base_pl_stage` 的泛型与级联生成：

[olo_base_pl_stage.vhd:33-38](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd#L33-L38) —— `Width_g / UseReady_g / Stages_g`，其中 `Stages_g : natural := 1`（注意是 `natural`，允许 0）。

[olo_base_pl_stage.vhd:88-117](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd#L88-L117) —— `Stages_g > 0` 时用 `for i in 0 to Stages_g-1 generate` 串联单级寄存器，把 `In_Valid/In_Data` 一路推到 `Out_*`。

[olo_base_pl_stage.vhd:120-124](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd#L120-L124) —— `Stages_g = 0` 时直接 `Out_Valid <= In_Valid` 等组合直通，无延迟。

回到 `olo_axi_pl_stage`，每条通道都把 `Stages_g` 透传：

[olo_axi_pl_stage.vhd:182-187](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/axi/vhdl/olo_axi_pl_stage.vhd#L182-L187) —— `Width_g => AwDataIn'length`、`UseReady_g => true`、`Stages_g => Stages_g`，把外层级数直接交给底层。

#### 4.4.4 代码实践

**实践目标**：通过修改 `Stages_g` 观察延迟变化，体会「级数由 base pl_stage 展开」。

**操作步骤**：

1. 在 `sim` 目录用不同 `Stages_g` 跑同一批用例（测试配置已在 `olo_axi.py` 中为 `Stages in [1, 4, 12]` 生成具名配置，见 [olo_axi.py:72-73](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_axi.py#L72-L73)）：
   ```bash
   cd sim
   python run.py --ghdl "*olo_axi_pl_stage_tb*Stages_g=1*SingleWrite*"
   python run.py --ghdl "*olo_axi_pl_stage_tb*Stages_g=4*SingleWrite*"
   ```
2. 想观察波形（GHDL 导出 VCD）时，可在仿真选项里加 `--gtkwave` 或导出波形后再看 `S_AwValid` 与 `M_AwValid` 的时间差。

**需要观察的现象**：不论 `Stages_g=1` 还是 `4`，单次写的对拍结果都应通过（功能不变）。但在波形里，`M_AwValid` 相对 `S_AwValid` 的延迟随 `Stages_g` 线性增长（理想无反压下约 `Stages_g` 拍）。

**预期结果**：两种配置均 `pass`；波形可见延迟差异。若未配置波形导出，则波形部分 **待本地验证**，但「配置通过」可由命令直接确认。

#### 4.4.5 小练习与答案

**练习 1**：`olo_axi_pl_stage` 的 `architecture` 里有任何一行 `if rising_edge(Clk)` 吗？
**答案**：没有。所有寄存逻辑都在底层 `olo_base_pl_stage`（及其单级实体）里，本实体只做打包/拆包/实例化，是纯结构化（structural）代码。

**练习 2**：把 `Stages_g` 设为 0，`olo_axi_pl_stage` 还能正常工作吗？
**答案**：能。底层 `olo_base_pl_stage` 的 `g_zero` 分支会把它退化为组合直通，等价于「保留 AXI 接口、但不插任何寄存器」，常用于占位或时序已满足时。

---

## 5. 综合实践

把四个模块串起来，完成本讲规格要求的综合任务：**实例化 `olo_axi_pl_stage` 寄存一段 AXI4-Lite 接口，仿真验证读写经过寄存后功能不变、且延迟增加一拍。**

建议步骤：

1. **复制现成测试台做改造**。仓库已提供 `olo_axi_pl_stage_tb.vhd`，它用 AXI 主/从 VC（`olo_test_axi_master_vc`、`olo_test_axi_slave_vc`）把 DUT 夹在中间，并在 [olo_axi_pl_stage_tb.vhd:121-144](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/test/axi/olo_axi_pl_stage/olo_axi_pl_stage_tb.vhd#L121-L144) 发起单次写/单次读/同时读写。先原样跑一遍确认环境可用：
   ```bash
   cd sim
   python run.py --ghdl "*olo_axi_pl_stage_tb*"
   ```
2. **改成 AXI4-Lite 视角**。参照 4.3.4 的示例代码，写一个只连接 AXI4-Lite 信号的最小顶层（或在 TB 里把 `IdWidth_g=0`、`UserWidth_g=0`、`Stages_g=1`，并不连接 AwLen/AwSize/.../WLast/RLast 等），证明同一实体能直接当 AXI4-Lite 寄存器。
3. **验证功能不变**。复用 TB 的 `SingleWrite` / `SingleRead` 用例：主端推入的地址与数据，必须与从端期望完全一致——确认寄存没有改变协议语义。
4. **验证延迟 +1**。在 `Stages_g=1` 下，于波形中测量 `S_AwValid` 上升沿到 `M_AwValid` 上升沿的间隔，预期约为 1 个 `Clk` 周期；再与 `Stages_g=2` 对比，确认延迟随级数增加。

**判据**：

- 功能层：`SingleWrite`、`SingleRead`、`ReadAndWrite` 全部 pass。
- 时序层：`Stages_g=1` 时 `M_*Valid` 比 `S_*Valid` 晚约 1 拍；级数翻倍延迟近似翻倍。
- 复用层：同一实体在 AXI4-Lite 接法下无需任何源码改动。

> 提示：完整事务的端到端延迟会同时叠加在请求通道（AW/AR）与响应通道（B/R）上，所以一次读事务的总延迟约 `2 × Stages_g` 拍，而不是 1 拍——别把这当成 bug。

## 6. 本讲小结

- `olo_axi_pl_stage` 是一个**纯结构化包装器**：自己不做寄存，靠五次实例化 `olo_base_pl_stage` 来寄存 AXI4 全接口。
- 它把 AXI4 的**五条通道分别寄存**：请求通道（AW/W/AR）方向 S→M，响应通道（B/R）方向 M→S；每条通道用 `subtype` 位段把十几个信号打包成一个大向量、过寄存器、再拆包。
- 寄存的意义不只是存数据，更关键是**把组合反传的 Ready 路径也寄存一拍**，切断 AXI 时序关键路径。
- 同一实体可直接用于 **AXI4-Lite**：所有 AXI4 专有端口都有默认值（地址附加信号全 0、`WLast`/`RLast` 默认 1），留空即等价于单拍 AXI4-Lite 语义。
- `Stages_g` 透传到底层：`>0` 时串联多级、`=0` 时退化为纯直通；延迟随级数线性增长，功能不变。
- 分层复用是 Open Logic 的典型模式：底层 `olo_base_pl_stage` 只懂一条 AXI-S 通道，上层负责协议适配。

## 7. 下一步学习建议

- **继续 axi 区域**：本讲是 u6 单元的第一篇。下一步建议学习 `olo_axi_lite_slave`（u6-l2），看 AXI4-Lite 从机如何把寄存器组挂到总线上——你会再次用到本讲的 AXI4-Lite 接口知识。
- **深入握手实现**：若想彻底搞懂「寄存 Ready 却不丢数据」的内部细节，回到 `olo_base_pl_stage` 的两进程法与 shadow 寄存器（u2-l2），并对照本讲 4.4 的调用关系。
- **看反压如何被测**：本讲测试台里 `PipelinedWrites-*_ready_delay`、`PipelinedRead-*_ready_delay` 用例（[olo_axi_pl_stage_tb.vhd:147-214](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/axi/olo_axi_pl_stage/olo_axi_pl_stage_tb.vhd#L147-L214)）专门在各级 Ready 上加延迟，是观察 shadow 寄存器生效的好材料，建议读一遍这些用例。
