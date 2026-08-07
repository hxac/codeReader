# 解复用器 dpe_demultiplexer

## 1. 本讲目标

本讲是 Unit 4 数据面引擎（DPE）的第三篇，承接 u4-l1（DPE 总体结构与 AXIS 元数据）与 u4-l2（轮询多路复用器）。多路复用器把 5 路输入「收拢」成一条流水线；本讲的**解复用器（demultiplexer）**则负责反向动作——把处理完的那一条流水线，按每个数据包携带的目的元数据 `tuser_dst`，**「分发」**到 CPU 与 4 个以太网口的发送 FIFO。

学完本讲你应当能够：

- 说清楚 demux 如何根据 `tuser_dst` 把一个 beat 路由到 0 个、1 个或多个出口；
- 解释为什么 demux 是**纯组合逻辑**而没有状态机，而与之对称的 mux 却需要一台 FSM；
- 理解广播（broadcast）与组播（multicast）时，背压（backpressure）是如何「合并」的——即一次握手必须等所有目的出口都 ready；
- 读懂 `tuser_dst` 取值 0~7 各自代表什么，以及 `MCAST_13`/`MCAST_24` 这两个组播地址命名的含义。

---

## 2. 前置知识

本讲假设你已经掌握以下内容（来自 u4-l1、u4-l2）：

- **DPE 的三段式骨架**：`多路复用器 mux → 处理流水线 → 解复用器 demux`，5 路输入、5 路输出。
- **dpe_if 接口**：一条 128 位 AXI-Stream 变体，靠 `tvalid`/`tready` 同拍为 1 完成 beat 握手；侧带信号 `tuser`（含 `bypass_all`/`bypass_stage`/`src[2:0]`/`dst[2:0]`）与 `tid`（peer index）携带路由元数据。
- **src 与 dst 的角色差异**：`src` 由 mux 按物理输入口强制盖写（权威字段）；`dst` 是流水线各级可改写的「建议目的」，**demux 最终只按 `dst` 分发**。
- **当前 Phase1 PoC 现状**：mux 与 demux 都真实在线，只有中间处理级被组合直通的 `dpe_dummy_switch` 顶替。

几个术语再温习一遍：

| 术语 | 含义 |
|------|------|
| **demux / 解复用器** | 1 路输入按元数据分发到 N 路输出 |
| **beat** | AXI-Stream 的一次数据传输（这里 128 位一次） |
| **背压 backpressure** | 下游用 `tready=0` 告诉上游「我暂时接不下」 |
| **skid buffer** | 一级寄存器切片，用来吸收背压、打断组合逻辑长路径 |
| **unicast / multicast / broadcast** | 单播（1 出口）/ 组播（多出口子集）/ 广播（全部出口） |

---

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [1.hw/ip.dpe/dpe_demultiplexer.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_demultiplexer.sv) | **本讲主角**：解复用器本体，纯组合分发 + 输入输出各一级 skid buffer |
| [1.hw/ip.infra/dpe_pkg.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/dpe_pkg.sv) | 地址常量包：`DPE_ADDR_CPU`/`DPE_ADDR_ETH_1..4`/`MCAST_13`/`MCAST_24`/`BCAST` |
| [1.hw/ip.infra/dpe_if.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/dpe_if.sv) | demux 使用的 128 位 AXIS 接口与 `m_axis`/`s_axis` modport |
| [1.hw/ip.infra/dpe_if_skid_buffer.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/dpe_if_skid_buffer.sv) | 包裹 demux 输入与全部 5 个输出的寄存器切片 |
| [1.hw/ip.dpe/dpe.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv) | 顶层把 demux 接在 `dpe_dummy_switch` 之后 |
| [1.hw/ip.dpe/dpe_multiplexer.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_multiplexer.sv) | 对称对照：mux 用 FSM 做轮询仲裁 |
| [4.sim/rtl/dpe_demultiplexer/tb.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/rtl/dpe_demultiplexer/tb.sv) | 专属测试台：依次发 6 个包（单播×5 + 广播×1）验证分发 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 目的元数据分发**：demux 怎么用 `tuser_dst` 给每个出口「开门」或「关门」。
- **4.2 demux 与 mux 的对称结构**：同样是 5 端口、同样裹 skid buffer，为何一个是组合、一个是状态机。
- **4.3 广播与组播处理**：一对多分发时，背压如何合并、握手如何收敛。

### 4.1 目的元数据分发：按 tuser_dst 选出口

#### 4.1.1 概念说明

解复用器要回答的核心问题只有一个：**「眼前这个 beat 该送到哪个（些）出口？」**

答案不在某个状态机里，而是直接写在数据包自己携带的元数据 `tuser_dst` 上。回顾 u4-l1：流水线各级（在完整设计里是 IP 路由查找；在当前 PoC 里是 `dpe_dummy_switch`）会改写 `tuser_dst`，把它当作「建议目的」。demux 是这条建议的**最终执行者**——它不评判、不修改任何元数据，只忠实按 `tuser_dst` 把门打开。

这套设计的好处是：**分发决策与处理逻辑完全解耦**。无论上游用什么算法决定目的（查路由表、查 dummy_switch 固定表、或保留原始 dst），demux 一概不需要改动，只要 `tuser_dst` 的编码约定不变。

#### 4.1.2 核心流程

先看地址编码。`dpe_pkg` 用 3 位（0~7）定义了 8 个目的地址：

| 取值 | 常量 | 含义 | demux 出口 |
|------|------|------|-----------|
| 0 | `DPE_ADDR_CPU` | CPU | to_cpu |
| 1 | `DPE_ADDR_ETH_1` | 以太网口 1 | to_eth_1 |
| 2 | `DPE_ADDR_ETH_2` | 以太网口 2 | to_eth_2 |
| 3 | `DPE_ADDR_ETH_3` | 以太网口 3 | to_eth_3 |
| 4 | `DPE_ADDR_ETH_4` | 以太网口 4 | to_eth_4 |
| 5 | `DPE_ADDR_MCAST_13` | 组播：口 1 + 口 3 | to_eth_1, to_eth_3 |
| 6 | `DPE_ADDR_MCAST_24` | 组播：口 2 + 口 4 | to_eth_2, to_eth_4 |
| 7 | `DPE_ADDR_BCAST` | 广播：全部 5 个出口 | to_cpu + to_eth_1..4 |

注意 `MCAST_13`/`MCAST_24` 的命名直接点出了成员：**奇数口组（1、3）**与**偶数口组（2、4）**，CPU 不参与组播，只有广播才把 CPU 也算进去。

demux 的执行流程可以概括成一句：**「对每个出口，独立判断 `tuser_dst` 是否落在自己的成员集合里；落在里面就把 beat 原样复制过去，否则把该出口的 `tvalid` 拉低。」**

用伪代码描述单个出口 k 的判定：

```
若 (tuser_dst ∈ 成员集合(k)):
    to_k.tvalid = from_dpe.tvalid
    to_k.tdata  = from_dpe.tdata     // 连同 tkeep/tlast/全部 tuser 字段一并复制
否则:
    to_k.tvalid = 0                  // 该出口「安静」
```

关键点：这是一个**逐 beat、纯组合**的判定。同一个 beat 可以同时打开多个出口（组播/广播），也可以只开一个（单播），决策完全由当前 beat 的 `tuser_dst` 决定，与历史无关。

#### 4.1.3 源码精读

先看模块端口。demux 有 1 个 `s_axis` 输入和 5 个 `m_axis` 输出，与 mux 的「5 个 `s_axis` 输入、1 个 `m_axis` 输出」正好镜像：

[dpe_demultiplexer.sv:43-50](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_demultiplexer.sv#L43-L50) —— 端口：1 路 `from_dpe` 输入，5 路 `to_cpu`/`to_eth_1..4` 输出。

地址常量定义在包里：

[dpe_pkg.sv:46-53](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/dpe_pkg.sv#L46-L53) —— 8 个 `localparam`，3 位编码，是 demux 一切判定的依据。

分发核心是一个 `always_comb` 块，里面是 5 段几乎对称的 `if-else`，每段对应一个出口。以**单播到 ETH_2**为例（这正是本讲的实践任务）：

[dpe_demultiplexer.sv:107-125](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_demultiplexer.sv#L107-L125) —— `to_eth_2` 分支：当 `tuser_dst` 等于 `DPE_ADDR_ETH_2`、`DPE_ADDR_MCAST_24` 或 `DPE_ADDR_BCAST` 时，把输入 beat 原样复制到 `to_eth_2_sbuff`；否则把 `tvalid` 拉到 0。

读这段代码要抓住三个细节：

1. **判定条件是一个「或」**：`tuser_dst == ETH_2 || tuser_dst == MCAST_24 || tuser_dst == BCAST`。这正是上表里 to_eth_2 的「成员集合」——单播自己（2）、偶数组播（6）、广播（7）。
2. **复制是全字段透传**：`tdata`/`tkeep`/`tlast`/`tuser_bypass_all`/`tuser_bypass_stage`/`tuser_src`/`tuser_dst` 全部照抄，demux **不改写任何元数据**。
3. **关门时要清零**：`else` 分支把 `tvalid=0` 且把数据相关字段清零，避免下游 skid buffer 锁存到脏数据。

其余 4 个出口的写法与此完全同构，只是各自的「成员集合」不同（见 4.1.2 的表）。例如 CPU 出口只认 `DPE_ADDR_CPU` 与 `DPE_ADDR_BCAST`：

[dpe_demultiplexer.sv:69-87](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_demultiplexer.sv#L69-L87) —— CPU 分支：成员集合 = {CPU, BCAST}，不含任何组播。

> 一处值得注意的实现细节（**待本地验证**）：这段组合逻辑把 8 个字段复制到每个出口的内部接口，但**没有显式驱动 `tid`**。输入侧 skid buffer 的 `from_dpe_sbuff.tid` 是有效的，却被组合块忽略了，于是各 `to_X_sbuff.tid` 处于未驱动状态。在当前 PoC（中间是 `dpe_dummy_switch`、加解密链未上线）里这无害，因为 peer index `tid` 只有在（已写好但被注释的）加解密级才用于密钥查找。等 U4-l5 / U5 的加密流水线接入后，如果下游需要从 demux 输出读 `tid`，这里需要补一行透传。这是阅读真实源码时容易漏掉的点，建议你在本地仿真里确认 `tid` 是否被下游使用。

#### 4.1.4 代码实践

**实践目标**：追踪一个 `tuser_dst = 2`（`DPE_ADDR_ETH_2`）的包，从 demux 输入一直到 `to_eth_2` 输出的完整路径。

**操作步骤**（源码阅读型实践，基于真实测试台）：

1. 打开 [4.sim/rtl/dpe_demultiplexer/tb.sv:141-156](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/rtl/dpe_demultiplexer/tb.sv#L141-L156)。这是测试台发送的「packet 2」，它把 `from_dpe.tuser_dst = DPE_ADDR_ETH_2`（第 147 行），载荷为 `{0x15,0x16,0x17,0x18,0x19}`，共 5 个 beat，末 beat 置 `tlast`。
2. 跟着这个包在 demux 内部走一遍：
   - 进入输入 skid buffer `u_dpe_if_skid_buffer_from_dpe`（[dpe_demultiplexer.sv:167-170](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_demultiplexer.sv#L167-L170)），一级寄存器后到达 `from_dpe_sbuff`。
   - 组合块判定：`from_dpe_sbuff.tuser_dst == DPE_ADDR_ETH_2` 命中 [第 107 行的 if 条件](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_demultiplexer.sv#L107)，于是 `to_eth_2_sbuff.tvalid/tdata/...` 被赋值；与此同时，其余 4 个出口的 `if`（CPU/ETH_1/ETH_3/ETH_4）都不命中，它们的 `tvalid` 被拉 0。
   - `to_eth_2_sbuff` 经输出 skid buffer `u_dpe_if_skid_buffer_to_eth_2`（[dpe_demultiplexer.sv:182-185](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_demultiplexer.sv#L182-L185)）到达模块出口 `to_eth_2`。
3. 打开测试台的 monitor [tb.sv:300-307](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/rtl/dpe_demultiplexer/tb.sv#L300-L307)，确认它统计的是 `to_eth_2.tvalid && to_eth_2.tready` 的 beat 数，并在 `tlast` 时打印「Packet received ... at port 2」。

**需要观察的现象**：仿真应只在该包传输期间，`to_eth_2` 上出现 5 个有效 beat；其余 4 个出口在此期间保持 `tvalid=0`。

**预期结果**：监视器打印一行 `Packet received with 5 words at port 2`，且 port 0/1/3/4 不会因为 packet 2 多出任何 beat（这些端口的计数来自它们各自的包）。**实际运行结果待本地验证**（需在 `4.sim/rtl/dpe_demultiplexer/` 下用 Verilator 或其它仿真器编译运行 `tb.sv`）。

#### 4.1.5 小练习与答案

**练习 1**：一个 `tuser_dst = DPE_ADDR_ETH_3`（值为 3）的 beat 进入 demux，哪些出口的 `tvalid` 会被拉高？

**参考答案**：只有 `to_eth_3`。看 [dpe_demultiplexer.sv:126](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_demultiplexer.sv#L126)，ETH_3 分支的成员集合是 {ETH_3, MCAST_13, BCAST}，单播值 3 命中第一项；其余分支的条件都不含 3。

**练习 2**：为什么 demux 在「关门」时（else 分支）要把 `tdata`/`tkeep` 等也清零，而不只是把 `tvalid=0`？

**参考答案**：下游是 `axis_register`（skid buffer），它会在 `tvalid/tready` 握手成功的那一拍把输入锁存进寄存器。虽然 `tvalid=0` 时不会被锁存，但留下未驱动或残留的旧值属于不良的 RTL 卫生习惯（仿真里可能出现 X 传播、综合时序/功耗也更差）。统一在 else 里清零是防御性写法，保证「未开门」的出口输出确定性全 0。

---

### 4.2 demux 与 mux 的对称结构

#### 4.2.1 概念说明

demux 与 mux 是一对「镜像」模块：mux 把 5 路输入收成 1 路，demux 把 1 路分发到 5 路输出；端口数量对称、都用 `dpe_if`、都裹 skid buffer。但它们解决的问题是**不同类型**的，因此内部实现形态截然不同——这是本模块最值得品味的架构取舍。

- **mux 面对的是「调度」问题**：5 路输入随时可能同时来包，必须决定「先服务谁」。这本质上需要一个**策略**（本项目选 per-packet 轮询）和**记忆**（轮到谁了、当前包发完没有），所以必然是状态机。
- **demux 面对的是「译码」问题**：1 路输入的每个 beat，目的完全由它自己的 `tuser_dst` 决定，不需要在多个候选里做选择，也不需要记住历史。所以 demux 是**纯组合**的。

一句话总结：**N 选 1 需要仲裁（有状态），1 分到 N 只需译码（无状态）**。

#### 4.2.2 核心流程

两者的对称与不对称可以用下表对照：

| 维度 | mux（多路复用器） | demux（解复用器） |
|------|------------------|------------------|
| 端口方向 | 5 个 `s_axis` → 1 个 `m_axis` | 1 个 `s_axis` → 5 个 `m_axis` |
| 决策依据 | FSM 状态 + 各路 `tvalid`（仲裁） | 当前 beat 的 `tuser_dst`（译码） |
| 是否有状态 | **有**，11 状态 FSM | **无**，纯 `always_comb` |
| 是否改写 `src` | **是**，强制盖写为物理入口地址 | 否，原样透传 |
| 是否改写 `dst` | 否，原样透传 | 否，原样透传 |
| 与 FCR 关系 | 接 `pause`/`is_idle`，负责整包边界暂停 | 不直接参与 FCR |
| skid buffer | 仅输出 1 级（输入直读各路 FIFO） | 输入 1 级 + 每个输出各 1 级 |

最后一行（skid buffer 数量）的差异也源于此：mux 的 5 路输入各自已经有上游 FIFO 缓冲，所以 mux 只在自己的输出加 1 级；demux 的输入是单条流水线、输出要扇出到 5 个 FIFO，所以输入加 1 级（隔离上游组合路径）、每个输出再加 1 级（隔离下游），共 6 级。

#### 4.2.3 源码精读

mux 的状态机——11 个状态（IDLE + 每路一对 Rk/Sk）：

[dpe_multiplexer.sv:56-63](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_multiplexer.sv#L56-L63) —— `state_t` 枚举：`IDLE` 加上每路输入的一对「探头态 Rk / 发送态 Sk」。

对比之下，demux 全文没有一个寄存器、没有 `always_ff`、没有状态枚举，核心就是 [dpe_demultiplexer.sv:68-164](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_demultiplexer.sv#L68-L164) 这一个 `always_comb`。这就是「译码无需状态」的直接体现。

再看一个体现「职责对称、方向相反」的细节——对 `src` 字段的处理。mux 在选中某路时会**强制盖写** `tuser_src`：

[dpe_multiplexer.sv:209](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_multiplexer.sv#L209) —— mux 把 `to_dpe_sbuff.tuser_src = DPE_ADDR_ETH_2`，即「无论包自己声称从哪来，我按物理入口盖写为权威值」。

而 demux 对 `src` 与 `dst` 一视同仁地原样透传（见 4.1.3 里 ETH_2 分支复制 `tuser_src`/`tuser_dst` 的两行），从不盖写。这印证了 u4-l1 的结论：**`src` 是 mux 写入的权威字段，`dst` 是流水线写、demux 读的建议字段**；demux 是 `dst` 的消费者，不是生产者。

最后看 skid buffer 的包裹方式。demux 用 6 个 `dpe_if_skid_buffer` 实例，把组合核心完全「夹」在中间：

[dpe_demultiplexer.sv:167-195](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_demultiplexer.sv#L167-L195) —— 1 个输入 skid buffer（`from_dpe`→`from_dpe_sbuff`）+ 5 个输出 skid buffer（`to_X_sbuff`→`to_X`）。组合逻辑只读写 `*_sbuff` 这组内部接口，对外端口全由 skid buffer 驱动/采样。

这种「组合核心 + 输入输出寄存器切片」是 AXI-Stream 风格里非常标准的时序隔离手法：组合译码逻辑不直接暴露到长互连上，每一拍都有寄存器打断路径，利于满足 80 MHz 的时序约束。

#### 4.2.4 代码实践

**实践目标**：通过对比阅读，亲手验证「mux 有状态、demux 无状态」这一结论，并理解各自的 skid buffer 布局。

**操作步骤**（源码阅读型实践）：

1. 在 [dpe_multiplexer.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_multiplexer.sv) 里统计 `always_ff` 与 `always_comb` 的数量、`state_t` 的状态数、skid buffer 实例数。
2. 在 [dpe_demultiplexer.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_demultiplexer.sv) 里做同样统计。
3. 填出下表（答案见「预期结果」）：

   | 项 | mux | demux |
   |----|-----|-------|
   | `always_ff` 数 | ? | ? |
   | 状态枚举 | ? 个状态 | ? 个状态 |
   | skid buffer 实例数 | ? | ? |

**需要观察的现象**：mux 文件里有明显的 FSM 三段式（状态寄存器、转移逻辑、输出逻辑）；demux 文件里只有「背压 assign + 一个 always_comb + skid buffer 实例」三块，没有任何寄存器。

**预期结果**：mux 有 1 个 `always_ff`（状态寄存器）、2 个 `always_comb`（转移 + 输出）、11 状态枚举、1 个 skid buffer；demux 有 0 个 `always_ff`、1 个 `always_comb`、无状态枚举、6 个 skid buffer。这一对比直观说明「译码比调度简单得多，省下了一整台状态机」。

#### 4.2.5 小练习与答案

**练习 1**：如果要把 demux 改成「严格按输入顺序、每次只允许一个出口收包」的轮流分发（类似 mux 的轮询），需要加什么？

**参考答案**：需要引入一台状态机来记录「当前轮到哪个出口」，并在每个包的边界（`tlast`）推进轮询指针。因为此时分发不再由 `tuser_dst` 单拍决定，而要服从一个跨拍的全局策略——这正是 mux 必须用 FSM 的原因。换句话说，一旦从「译码」退化成「调度」，无状态的组合逻辑就不够了。

**练习 2**：为什么 mux 不需要像 demux 那样在每个输入都加一级 skid buffer？

**参考答案**：mux 的 5 路输入各自来自上游的接收 FIFO（参见 u4-l2 / top.sv 里 eth 收发路径），这些 FIFO 已经提供了足够的缓冲与寄存器隔离；mux 只需在自己的单条输出加 1 级 skid buffer 打断到流水线的路径即可。demux 的输入是单条流水线（无 FIFO）、输出扇出到 5 个下游 FIFO，所以输入要加 1 级隔离上游、每个输出再加 1 级隔离下游。skid buffer 的数量取决于「上下游已经提供了多少缓冲」，而不是固定的。

---

### 4.3 广播与组播：一对多分发与背压合并

#### 4.3.1 概念说明

单播很直观：一个 beat 进、一个出口出。但当 `tuser_dst` 是 `MCAST_13`、`MCAST_24` 或 `BCAST` 时，**同一个 beat 要被复制到多个出口**。这带来一个握手难题：

AXI-Stream 的握手是「`tvalid` 与 `tready` 同拍为 1 才算传输一次」。如果同一个 beat 要同时发给 eth_1 和 eth_3（组播 13），而 eth_1 这拍 ready、eth_3 这拍不 ready，怎么办？

demux 的答案是：**这一拍谁都不能算成功，要等到所有「应当收到此 beat」的出口都 ready，上游才被允许推进**。也就是说，组播/广播把多个下游的 `tready` **「与」**起来，合并成上游的一根 `tready`。这是「木桶效应」——最慢的那个目的出口决定整组播的推进速度。

#### 4.3.2 核心流程

设上游输入 ready 信号为 \( \text{tready}_{in} \)，各出口成员判定函数为 \( M_k(\text{dst}) \)（当 `dst` 属于出口 k 的成员集合时为 1）。则背压合并可写成：

\[
\text{tready}_{in} \;=\; \bigwedge_{k \in \{\text{cpu},\text{eth1..4}\}} \Big( \neg\, M_k(\text{dst}) \;\lor\; \text{tready}_k \Big)
\]

用人话说：对每个出口 k，**「要么这个包本来就不该去 k，要么 k 必须准备好接收」**；所有出口都满足这一条，上游才能推进一拍。

同时，各出口自己的 `tvalid` 仍然按 4.1 的译码独立产生：

\[
\text{tvalid}_k \;=\; M_k(\text{dst}) \;\land\; \text{tvalid}_{in}
\]

于是对一次广播 beat：5 个出口同时拉高 `tvalid`，但只有当 5 个 `tready` 全为 1 时，这一拍才在 5 个出口上同时完成传输——同一份 `tdata` 被「扇出复制」到 5 个下游 FIFO。这一拍若没成，下一拍原样重试，直到全部就绪。

各成员集合回顾（由 4.1.2 的表与源码共同确定）：

- **BCAST(7)**：cpu + eth1 + eth2 + eth3 + eth4（全部 5 个）→ 需 5 个 ready 全 1。
- **MCAST_13(5)**：eth1 + eth3 → 需这 2 个 ready 全 1（cpu/eth2/eth4 不参与，其 ready 任意）。
- **MCAST_24(6)**：eth2 + eth4 → 需这 2 个 ready 全 1。

#### 4.3.3 源码精读

背压合并就一行连续赋值，但它是 demux 里最「烧脑」的一行：

[dpe_demultiplexer.sv:61-65](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_demultiplexer.sv#L61-L65) —— 计算 `from_dpe_sbuff.tready`：5 个括号项相「与」，每一项形如「(dst 不是某出口的成员) 或 (该出口 ready)」。

拆开看其中一个出口的项，比如 to_eth_2：

```
((dst != DPE_ADDR_ETH_2 && dst != DPE_ADDR_MCAST_24 && dst != DPE_ADDR_BCAST) | to_eth_2_sbuff.tready)
```

前半句「`dst` 不是 to_eth_2 的成员」对应公式里的 \( \neg M_k(\text{dst}) \)；后半句「to_eth_2_sbuff ready」对应 \( \text{tready}_k \)。两者求「或」，再与其它 4 个出口的同样项求「与」——正是 4.3.2 的公式。

注意一个容易看错的点：括号里的 `&&` 是判断「不属于成员集合」（多个互斥条件同时成立），而括号之间用 `|` 与 `&` 交替：括号内最后用 `|` 连接「不属于 | 已就绪」，括号之间用 `&`（行尾）连接 5 个出口的要求。优先级上 `&` 高于 `|`，所以作者在每个括号里显式用括号包好「不属于」的合取，避免歧义。

把这一行与 4.1.3 的 `always_comb` 对照，会发现二者用的「成员集合」**完全一致**：to_eth_2 的背压条件里列出的正是 `{ETH_2, MCAST_24, BCAST}`，与译码块 [第 107 行](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_demultiplexer.sv#L107) 的判定条件一字不差。这种「分发条件」与「背压条件」同源的写法，保证了「开门的出口一定被纳入背压合并」——不会出现「beat 被译码送到某出口、但该出口没被算进 tready」的撕裂。

最后看真实测试台如何验证广播。测试台的 packet 5 把 `tuser_dst = DPE_ADDR_BCAST`：

[tb.sv:192-208](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/rtl/dpe_demultiplexer/tb.sv#L192-L208) —— 发送一个广播包（5 个 beat），载荷 `{0x33,...,0x37}`。

它的 monitor（[tb.sv:282-325](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/rtl/dpe_demultiplexer/tb.sv#L282-L325)）会在 port 0~4 各打印一次「Packet received ... at port N」，证明同一个广播包被 5 个出口各收一份。

#### 4.3.4 代码实践

**实践目标**：亲手验证「组播 13 的背压只由 eth1 与 eth3 决定，与其余出口无关」。

**操作步骤**（源码阅读 + 修改型实践）：

1. 复制测试台 [tb.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/rtl/dpe_demultiplexer/tb.sv)，把其中一个包的 `from_dpe.tuser_dst` 从 `DPE_ADDR_ETH_2` 改成 `DPE_ADDR_MCAST_13`（值为 5）。
2. 在「Outputs ready control process」（[tb.sv:217-273](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/rtl/dpe_demultiplexer/tb.sv#L217-L273)）里，人为制造一段「`to_eth_1.tready=0` 且 `to_eth_3.tready=1`」的区间，同时让 `to_cpu`/`to_eth_2`/`to_eth_4` 都 ready。
3. 在这段时间发送这个组播包。

**需要观察的现象**：即使 cpu/eth2/eth4 都 ready，只要 eth1 不 ready，`from_dpe.tready` 就保持 0，上游 `while (!from_dpe.tready)` 循环会原地等待，beat 不推进。一旦 eth1 也拉高 ready，这一拍才同时在 eth1 与 eth3 上完成。

**预期结果**：组播包的每个 beat 都被 eth1 与 eth3 各收一份（monitor 在 port 1 和 port 3 各打印一次），cpu/eth2/eth4 不收；推进时机完全由 eth1 与 eth3 中较慢的那个决定。**实际运行结果待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：一次广播（BCAST）传输中，如果 to_cpu 长时间不 ready，会发生什么？对线速转发的吞吐有何影响？

**参考答案**：根据 [第 61 行](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_demultiplexer.sv#L61) 第一个括号项，`to_cpu_sbuff.tready=0` 会让整条 `from_dpe_sbuff.tready` 为 0，于是即便 4 个以太网口都已 ready，这一 beat 也无法推进——广播被 CPU 接口「卡住」。这正是广播的代价：它的有效吞吐等于**最慢目的出口**的吞吐。在本项目里 CPU 接口只有约 170 Mbps（见 u3-l3），所以广播流量若频繁发生，会被 CPU 接口拖累。这也是为什么真实网络栈里广播/组播通常只用于稀疏的控制帧。

**练习 2**：`MCAST_13` 与 `MCAST_24` 的成员为什么这样划分（奇偶口分组），而不是 {1,2} 与 {3,4}？

**参考答案**：源码本身没有给出文字解释（**待确认**），但从拓扑角度可以合理推测：这是一种让任意两个「对」互相组播的编码——把 4 个口分成两组（奇/偶），使得一次组播恰好覆盖「另一半」端口的一种对称划分。结合 `dpe_dummy_switch` 的固定映射（CPU↔ETH_1、ETH_2↔CPU、ETH_3↔ETH_4，见 [dpe_dummy_switch.sv:93-108](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_dummy_switch.sv#L93-L108)），组播/广播主要服务于将来的多 peer 场景（同一报文扇出到多个 WireGuard 对端）。具体分组是否对应某种物理冗余拓扑，需结合硬件连线（u1-l3）确认。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「全目的地址」的分发推演。

**任务**：以测试台 [tb.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/rtl/dpe_demultiplexer/tb.sv) 的 6 个包为脚本，画出一张「输入 beat → 各出口 `tvalid`」的真值表，并标注每个包的背压由哪些出口决定。

**步骤**：

1. 列出 6 个包及其 `tuser_dst`：packet 0=CPU、1=ETH_1、2=ETH_2、3=ETH_3、4=ETH_4、5=BCAST（见 [tb.sv:66-73](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/rtl/dpe_demultiplexer/tb.sv#L66-L73) 的注释与各发送段）。
2. 对每个包，依据 [dpe_demultiplexer.sv:68-164](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_demultiplexer.sv#L68-L164) 写出 5 个出口的 `tvalid` 取值（1 或 0）。
3. 对每个包，依据 [dpe_demultiplexer.sv:61-65](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_demultiplexer.sv#L61-L65) 指出「哪些出口的 ready 会影响上游推进」。
4. （可选，**待本地验证**）在仿真器里跑这个测试台，对照 monitor 打印的「port N 收到 word 数」，核对与你画的真值表一致。

**参考答案骨架**（包 → 命中出口 → 背压决定者）：

| 包 | tuser_dst | 命中的出口(tvalid=1) | 背压决定者 |
|----|-----------|--------------------|-----------|
| 0 | CPU(0) | to_cpu | to_cpu |
| 1 | ETH_1(1) | to_eth_1 | to_eth_1 |
| 2 | ETH_2(2) | to_eth_2 | to_eth_2 |
| 3 | ETH_3(3) | to_eth_3 | to_eth_3 |
| 4 | ETH_4(4) | to_eth_4 | to_eth_4 |
| 5 | BCAST(7) | to_cpu + to_eth_1..4（全部） | 全部 5 个出口 |

这张表把「单播=单出口+单点背压」「广播=全出口+全员背压」两种极端，以及中间的组播，统一在同一个译码/背压模型下——这就是 demux 的全部工作机制。

---

## 6. 本讲小结

- demux 是 DPE 的「出口调度员」：按每个 beat 的 `tuser_dst`，把处理完的数据**纯组合**地分发到 CPU 与 4 个以太网口，自身不改写任何元数据。
- 地址编码 0~7 覆盖单播（CPU/eth1-4）、两组组播（`MCAST_13`=eth1+eth3、`MCAST_24`=eth2+eth4）与广播（`BCAST`=全部 5 个）。
- 与对称的 mux 相比，demux **没有状态机**——因为「1 分到 N」是译码问题，而 mux「N 选 1」是调度问题才需要 FSM；二者的 skid buffer 布局也因此不同（demux 共 6 级，mux 仅 1 级输出）。
- 组播/广播时，背压把所有「该收此 beat」的出口的 `tready` **「与」**起来，合并成上游一根 `tready`；分发条件与背压条件同源，避免握手撕裂。
- 当前 Phase1 PoC 中 demux 真实在线，上游接的是直通的 `dpe_dummy_switch`；它透传 8 个字段但未显式驱动 `tid`，待加解密链上线后需复核。
- demux 用 `tuser_dst`（流水线各级写、demux 读的「建议目的」）做最终分发，与 mux 用物理入口盖写 `tuser_src`（「权威来源」）形成对照。

---

## 7. 下一步学习建议

- **u4-l4 TCAM 最长前缀路由查找**：本讲里 `tuser_dst` 一直是「别人写好、demux 照办」。下一讲进入真正**产生** `tuser_dst` 的地方——`dpe_egress_ip_lookup` 如何用并行 CAM 比较做最长前缀匹配，从而决定一个包该去哪个出口。
- **u4-l5 WireGuard 封装/解封装与加解密数据流**：理解在完整设计里，demux 之前那一长串 disassembler/decryptor/encryptor/assembler 如何串起来，以及当前它们为何被 `dpe_dummy_switch` 顶替。
- **回看 u4-l2**：把本讲的「demux 无状态」与 u4-l2 的「mux 11 状态 FSM + FCR pause/idle」并列复习，巩固「调度 vs 译码」这条架构主线。
- **动手验证**：若本地已配好 Verilator/仿真环境，运行 `4.sim/rtl/dpe_demultiplexer/tb.sv`，亲手观察 6 个包在 5 个出口上的分发波形，特别是广播包的 5 路同时 `tvalid` 与木桶式背压。
