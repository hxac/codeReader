# 轮询多路复用器与暂停流控

## 1. 本讲目标

本讲聚焦 DPE（数据面引擎）的「入口大门」——`dpe_multiplexer`。它在 u4-l1 介绍的三段式骨架（mux → 流水线 → demux）里担任第一段，把 CPU 与 4 个以太网口共 5 路输入汇成一路送进处理流水线。读完本讲你应该能够：

- 说清 `dpe_multiplexer` 为什么用「按包轮询（per-packet round-robin）」仲裁 5 路输入，以及它的轮询顺序。
- 读懂它那台 11 状态的 FSM，尤其是 `pause`/`is_idle` 这对握手信号如何在**包边界**生效、绝不在包中间切换。
- 把这台 FSM 与 u3-l4 的 FCR 原子更新握手对应起来，理解为什么暂停必须落在 mux 而不是别处。
- 准确描述当前 HEAD 的 Phase1 PoC 现状：mux 与 demux 都是**真实在线**的，被 `dpe_dummy_switch` 替换掉的只是中间的处理级。

## 2. 前置知识

本讲默认你已经学过：

- **u4-l1**：DPE 的三段式骨架、`dpe_if` 这条 128 位 AXI-Stream 变体、`tuser` 里 `{bypass_all, bypass_stage, src, dst}` 元数据编码、`src` 是 mux 强制写入的「权威字段」、`dst` 是流水线各级可改写的「建议字段」。
- **u3-l4**：FCR（流控寄存器）只有 `pause`/`idle` 两比特，构成 CPU 与数据面之间的请求—应答握手；改路由表/密钥表前 CPU 先写 `pause=1`、轮询到 `idle=1` 才动手，改完写 `pause=0`。
- **AXI-Stream 握手**：`tvalid` 与 `tready` 同拍都为 1 才完成一个 beat（一次数据传输）；`tlast` 标记一个包的最后一拍。

几个本讲会用到的直觉：

- **仲裁（arbitration）**：多个数据源抢同一条出口时，需要一个裁判决定「现在让谁走」。常见裁判策略有固定优先级（某个口永远插队）、轮询（round-robin，轮流来，公平不饿死）、加权轮询（按权重分配带宽）。本讲的主角选的是**轮询**。
- **按包 vs 按 beat**：仲裁可以在每个 beat（每拍）都重新选一次输入，也可以选定一个包后**一口气把它发完**再换下一个。前者叫抢占式，会把不同包的字节交错混在一起；后者叫非抢占式（per-packet），保证一个包完整连续。AXIS 数据流几乎总是用后者，否则下游无法解析包边界。
- **背压（backpressure）**：下游用 `tready=0` 告诉上游「我暂时吃不下」。本讲的 mux 在背压时如何停留，是理解 FSM 转移条件的关键。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [1.hw/ip.dpe/dpe_multiplexer.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_multiplexer.sv) | 本讲主角。5 选 1 轮询仲裁器 + pause/idle 流控 FSM。 |
| [1.hw/ip.dpe/dpe.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv) | DPE 顶层。把 mux 的 `pause`/`is_idle` 接到 CSR 的 `fcr` 字段，并实例化 mux → dummy_switch → demux 三段。 |
| [1.hw/top.filelist](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.filelist) | 综合文件清单。看清哪些 DPE 文件真实编入、哪些被注释，是判断 PoC 现状的直接证据。 |
| [1.hw/ip.infra/dpe_pkg.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/dpe_pkg.sv) | `DPE_ADDR_CPU`/`DPE_ADDR_ETH_1..4` 等地址常量，mux 用它们强制写入 `src`。 |
| [1.hw/ip.dpe/dpe_dummy_switch.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_dummy_switch.sv) | Phase1 顶替中间处理级的「占位交叉开关」，理解 PoC 现状的关键。 |
| [4.sim/rtl/dpe_multiplexer/tb.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/rtl/dpe_multiplexer/tb.sv) | 专为本模块写的 Verilator 测试台，5 路并发发包 + 驱动 `pause`，是本讲代码实践的落脚点。 |

---

## 4. 核心概念与源码讲解

### 4.1 按包轮询仲裁

#### 4.1.1 概念说明

DPE 的处理流水线只有**一条**，而要喂它的输入有 **5 路**：1 路 CPU（控制面送来的 WireGuard 握手报文）+ 4 路以太网口（用户数据）。这就像高速公路 5 条匝道汇入 1 条主路，必须有个收费站（仲裁器）决定「此刻放哪条匝道的车」。

`dpe_multiplexer` 选择的是**轮询（round-robin）**：按固定顺序 CPU → eth1 → eth2 → eth3 → eth4 → CPU … 依次探头，哪路有包就让它走。轮询的好处是：

- **公平、不饿死**：只要每路都给机会，没有任何一路会被永久阻塞。
- **极简单**：不需要维护优先级表或权重，一颗状态机即可。
- **适合均匀负载**：4 个网口流量地位平等，没有谁该长期插队。

而且它是**按包（per-packet）非抢占**的：一旦决定服务某一路，就把它这整个包（直到 `tlast`）发完，再轮到下一路。这样每个包在输出流里都是连续的一段，下游 parser 才能正确识别包头包尾。

#### 4.1.2 核心流程

mux 的轮询可以用下面这个循环描述（伪代码）：

```
state = IDLE
loop:
    if state == IDLE and not pause:
        state = R0                      # 从 CPU 开始轮询

    if state in {R0..R4}:               # 正在探头看第 k 路输入
        k = state 的编号
        if pause:
            state = IDLE                # 包间空隙遇暂停，立刻停
        elif input[k].tvalid and downstream.tready:
            state = Sk                  # 这路有包且下游能收 → 开始发送
        elif (not input[k].tvalid) and downstream.tready:
            state = R((k+1) mod 5)      # 这路没包 → 跳到下一路

    if state in {S0..S4}:               # 正在发送第 k 路的整包
        k = state 的编号
        if input[k].tlast and input[k].tvalid and downstream.tready:
            state = pause ? IDLE : R((k+1) mod 5)   # 包发完，看是否暂停
        # 否则继续留在 Sk，把本包剩余 beat 发完
```

几个关键点先记在脑子里：

1. **顺序固定**：CPU（R0）永远是每轮起点，随后 eth1→eth2→eth3→eth4，eth4 之后回 CPU。
2. **`src` 字段在此刻被「盖章」**：mux 一旦选中某路，就把输出 `tuser_src` 强制写成该路的地址常量（`DPE_ADDR_CPU` 等）。这正是 u4-l1 说的「`src` 是 mux 写入的权威字段」。
3. **`dst` 直通**：输入自带的 `tuser_dst` 原样透传，留给下游处理级去改写。

> 关于公平性的一个简单量化：在一轮完整轮询里，每路输入都至少被探查一次。因此任一就绪包从到达到开始被服务的等待时间，上界约为「当前正在发送的那个包的剩余长度 + 排在它前面的几路包的总长度」。这正是按包轮询的代价——长包会推迟短包——但在 1Gbps、典型 MTU 下完全可接受。

#### 4.1.3 源码精读

**端口**：`pause`/`is_idle` 是流控握手，5 个 `dpe_if.s_axis` 是输入，1 个 `dpe_if.m_axis to_dpe` 是汇合后的输出。

[1.hw/ip.dpe/dpe_multiplexer.sv:43-53](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_multiplexer.sv#L43-L53) —— 声明 `pause` 输入、`is_idle` 输出，以及 `from_cpu`/`from_eth_1..4` 五个从口和 `to_dpe` 主口。

**状态枚举**：一台 11 状态 FSM。`IDLE` 是停机态；每路输入对应一对状态——`Rk`（Ready，探头/就绪态）和 `Sk`（Sending，发送态）。

[1.hw/ip.dpe/dpe_multiplexer.sv:56-63](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_multiplexer.sv#L56-L63) —— 定义 `IDLE` 与 `R0/S0 … R4/S4` 共 11 个状态。

**轮询转移（以 CPU 即 R0/S0 为例）**：

[1.hw/ip.dpe/dpe_multiplexer.sv:87-97](https://github.com/chili-chips-ba-wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_multiplexer.sv#L87-L97) —— 在 `R0`：若 CPU 有包且下游能收，进 `S0` 发送；若遇 `pause`，回 `IDLE`；若 CPU 无包，跳到 `R1`。在 `S0`：只有当 `tlast && tvalid && tready`（整包发完）才离开，去 `pause ? IDLE : R1`。

eth1~eth4 的 `R1/S1 … R4/S4` 结构完全对称，只是输入换成对应的 `from_eth_k`、目的状态换成 `R(k+1)`。最后一站 eth4 会回绕到 CPU：

[1.hw/ip.dpe/dpe_multiplexer.sv:135-145](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_multiplexer.sv#L135-L145) —— `R4` 无包时跳回 `R0`（轮询闭环）；`S4` 发完包后去 `pause ? IDLE : R0`。这就是「CPU→eth1→eth2→eth3→eth4→CPU」的轮询环。

> 注意 `Rk → R(k+1)` 的跳转条件里也带了 `to_dpe_sbuff.tready`。也就是说：当下游背压（`tready=0`）时，即便当前输入没有包，mux 也不会空转着轮换，而是停在原地等下游恢复。这把「轮询推进」和「真正可用的传输机会」绑定，行为更确定。

**输出按状态多路选择**：组合逻辑里，`unique case (state)` 决定把哪一路输入透传到内部总线 `to_dpe_sbuff`，并强制盖写 `tuser_src`。

[1.hw/ip.dpe/dpe_multiplexer.sv:175-186](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_multiplexer.sv#L175-L186) —— 在 `R0/S0` 态把 `from_cpu` 的数据接出去，并把 `tuser_src` 强制写成 `DPE_ADDR_CPU`，`tuser_dst` 取 `from_cpu.tuser_dst`，`from_cpu.tready` 跟随下游 ready。

其余四路（`R1/S1 … R4/S4`）的输出块完全同构，只是 `src` 分别盖成 `DPE_ADDR_ETH_1 … DPE_ADDR_ETH_4`。地址常量定义在：

[1.hw/ip.infra/dpe_pkg.sv:46-53](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/dpe_pkg.sv#L46-L53) —— `DPE_ADDR_CPU=0`、`DPE_ADDR_ETH_1..4=1..4`，另有 `5/6` 组播、`7` 广播（组播/广播编码不在本讲主角范围内，由 demux 侧使用）。

最后，内部总线经一个 skid buffer 落到真正的输出端口 `to_dpe`，吸收下游的瞬时背压：

[1.hw/ip.dpe/dpe_multiplexer.sv:246-249](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_multiplexer.sv#L246-L249) —— 实例化 `dpe_if_skid_buffer`，把 `to_dpe_sbuff` 接到 `to_dpe`。该 skid buffer 是 1 拍深的 `axis_register`（见 u4-l1），这对 4.2 节理解 `is_idle` 很重要。

#### 4.1.4 代码实践

**实践目标**：在源码里亲手标出轮询顺序与转移条件，把「按包轮询」从概念落到具体行号。

**操作步骤**：

1. 打开 `1.hw/ip.dpe/dpe_multiplexer.sv`，定位 4.1.3 引用的状态枚举与转移逻辑。
2. 在 `R0`/`R1`/`R2`/`R3`/`R4` 五段转移逻辑旁各批注一句「对应输入 = ?，无包时跳到 ?」。
3. 圈出每段 `Sk` 离开条件里的 `tlast`，确认 mux 只在**包尾**才切换输入——这就是「按包非抢占」。
4. 打开测试台 `4.sim/rtl/dpe_multiplexer/tb.sv`，对照下面「预期结果」。

**需要观察的现象 / 预期结果**：

- 批注后，轮询环应为：`R0(CPU)→R1(eth1)→R2(eth2)→R3(eth3)→R4(eth4)→R0…`。
- 测试台 `fork … join` 同时在 5 路输入各发一个包（长度分别是 6/4/5/4/4 拍，见 [tb.sv:66-72](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/rtl/dpe_multiplexer/tb.sv#L66-L72)）。因为是非抢占按包轮询，输出端会按 CPU、eth1、eth2、eth3、eth4 的顺序收到 5 个**完整**的包，每个包的字节连续不被交错。监控进程会在日志里打印 5 次 `Packet received with N words`（见 [tb.sv:250-261](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/rtl/dpe_multiplexer/tb.sv#L250-L261)）。

如果想真正跑一遍，可执行（需要本机装有 Verilator）：

```bash
make -C 4.sim/rtl/dpe_multiplexer sim
```

> 该 Makefile 用 Verilator 编译 `top.filelist` + `tb.sv` 并执行，日志写到 `output/sim.log`（参见 `4.sim/rtl/dpe_multiplexer/Makefile` 的 `sim` 目标）。能否跑通取决于本地工具链，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：若 5 路输入同时各到一个包，输出端第一个被完整送出的是哪一路？为什么？

**参考答案**：是 CPU（`from_cpu`）。因为 `IDLE` 解除暂停后必定先进 `R0`（CPU），且轮询每轮都从 `R0` 开始，CPU 在每一轮都享有「首先被探头」的位置；这是一种轻微但固定的优先倾向。

**练习 2**：把 `Sk → R(k+1)` 离开条件里的 `tlast` 去掉（假设写成只要 `tvalid && tready` 就切换），会对下游解析造成什么后果？

**参考答案**：mux 会在一个包还没发完（没到 `tlast`）时就跳去服务下一路，导致同一个包的字节流被另一路插断、多个包的 beat 在输出里交错。下游靠 `tlast` 划分包边界的 parser 会彻底混乱，无法还原完整包。这正是必须「按包非抢占」的原因。

**练习 3**：`Rk → R(k+1)`（跳过无包的输入）的条件为什么也要求 `to_dpe_sbuff.tready`？

**参考答案**：把「轮询推进」与「存在可用传输机会」绑定。当下游背压（`tready=0`）时，无论换到哪一路都传不出数据，于是 mux 选择停在原地、不空转轮换，等到下游恢复再推进，使仲裁行为与实际流量机会一致、更易推理。

---

### 4.2 pause/idle 状态机：在包边界优雅暂停

#### 4.2.1 概念说明

u3-l4 已经讲过：CPU 要在线速转发期间改路由表/密钥表，必须先让数据面「停下来」再动手，否则数据面会读到改了一半的脏表项。`pause`（CPU 写、硬件读）发起暂停请求，`idle`（硬件写、CPU 读）回报「已静止」，构成请求—应答握手。

本讲要回答的核心问题是：**这个暂停该落在系统的哪个位置、以什么粒度执行？**

答案是：**落在 mux，粒度是「包」。** 理由有二：

1. **mux 是单点汇流口**。5 路输入都只能从 mux 进入流水线，只要 mux 不再放新包进来，整条流水线最终会被排空。把闸门装在入口，一处控制全局。
2. **绝不能用 AXIS 的 `tready=0`（stall）来做暂停**。`tready` 是**逐拍（per-beat）**反压：你随时可以把它拉低，但那会**撕裂正在飞的包**——包的前半截已经进了流水线，后半截被堵在 mux 里，下游处理级拿到的是一个半截包。这对加密/路由这种有状态的流水线是灾难。

所以 mux 的做法是：收到 `pause` 后，**先把当前正在发的那个包发完**（等到 `tlast`），然后才进入 `IDLE` 不再放新包；`IDLE` 且输出已排空时，才把 `is_idle` 拉高告诉 CPU「安全了」。

#### 4.2.2 核心流程

把 u3-l4 的 8 步原子更新握手映射到 mux 的状态上：

```
CPU 侧 (固件)                      mux 侧 (FSM)
-------------                      -----------
1. csr->dpe->fcr->pause(1)  ──►   pause=1 到来
                                   若正在发某包(Sk): 继续发完到 tlast
2. (轮询) while(!idle())     ◄──   包发完后 S_k → IDLE；IDLE 且 to_dpe.tvalid==0 → is_idle=1
3. 改 routing/cryptokey 表         此时 mux 停在 IDLE，不再放新包进流水线
4. csr->dpe->fcr->pause(0)  ──►   pause=0 到来
                                   IDLE → R0，恢复轮询放行
```

关键的三条 FSM 规则（对应源码）：

- **包间遇暂停，立刻停**：在 `Rk`（探头态，此刻没有包在飞）检测到 `pause`，直接 `→ IDLE`。
- **包中遇暂停，发完再停**：在 `Sk`（发送态）即便 `pause=1`，也必须等到 `tlast` 才 `→ IDLE`，保证当前包完整。
- **`is_idle` 的精确定义**：只有在 `IDLE` 态 **且** 真实输出端口 `to_dpe.tvalid==0` 时，`is_idle` 才为 1。第二个条件是为了等 skid buffer 里可能残留的最后一拍也排空。

#### 4.2.3 源码精读

**入口/出口接线**：在 DPE 顶层，`pause` 来自 CSR 的 `fcr.pause`，`is_idle` 回写给 CSR 的 `fcr.idle`——这正是 u3-l4 那对比特。

[1.hw/ip.dpe/dpe.sv:67-76](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv#L67-L76) —— 实例化 `dpe_multiplexer`，`.pause(from_csr.dpe.fcr.pause.value)`、`.is_idle(to_csr.dpe.fcr.idle.next)`。`.value` 是 PeakRDL 生成的硬件读端口、`.next` 是硬件写端口（参见 u3-l1/u3-l2）。

**`pause` 在转移逻辑里的三处落点**（以 CPU 段为例，其余对称）：

[1.hw/ip.dpe/dpe_multiplexer.sv:83-97](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_multiplexer.sv#L83-L97) ——
- `IDLE`：`if (!pause) next_state = R0;`（暂停时停在 IDLE，恢复才放行）。
- `R0`：`else if (pause) next_state = IDLE;`（包间空隙遇暂停，立刻停）。
- `S0`：包尾时 `next_state = pause ? IDLE : R1;`（**包发完后**才看暂停——这就是「包边界暂停」的精髓）。

**`is_idle` 的输出定义**：

[1.hw/ip.dpe/dpe_multiplexer.sv:171-173](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_multiplexer.sv#L171-L173) —— 在 `IDLE` 态：`is_idle = !to_dpe.tvalid;`。其余 `R/S` 态一律 `is_idle = 1'b0`（默认赋值见 [L155](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_multiplexer.sv#L155)）。

> 为什么用 `to_dpe.tvalid`（skid buffer **之后**的端口）而不是 `to_dpe_sbuff.tvalid`（skid buffer 之前）？因为 mux 进入 `IDLE` 后会立刻停止驱动 `to_dpe_sbuff.tvalid=0`，但 1 拍深的 skid buffer 里可能还残留着上一个包的最后一拍，正摆在 `to_dpe` 上等下游取走。CPU 必须等到这拍也排空（`to_dpe.tvalid==0`）才能认为数据面真正静止。这是 `is_idle` 设计里最容易被忽略、却最关键的一拍。

把 `pause` 与 `is_idle` 合起来看，整条流控通路是：CPU 写 `fcr.pause` → CSR 译码出 `from_csr.dpe.fcr.pause.value` → mux FSM 据此在包边界停 → mux 把 `is_idle` 经 `to_csr.dpe.fcr.idle.next` 写回 CSR → CPU 读 `fcr.idle`。一条完整的请求—应答闭环。

#### 4.2.4 代码实践

**实践目标**：用时序图把「写 pause=1 → 检测 idle=1」期间 mux 内部的状态变化讲清楚，并验证包不被撕裂。

**操作步骤**：

1. 打开测试台 `4.sim/rtl/dpe_multiplexer/tb.sv`，找到驱动 `pause` 的两行：
   - [tb.sv:233](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/rtl/dpe_multiplexer/tb.sv#L233) `pause = 1;`
   - [tb.sv:244](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/rtl/dpe_multiplexer/tb.sv#L244) `pause = 0;`
2. 手画一张时序图，时间轴覆盖 `pause` 拉高前后若干拍，包含信号：`pause`、`state`（FSM）、`to_dpe_sbuff.tvalid`、`to_dpe.tvalid`、`is_idle`、当前输入的 `tlast`。
3. 标注：`pause=1` 到来时若 `state==Sk`，FSM 停留到 `tlast` 才进 `IDLE`；进入 `IDLE` 后再等 `to_dpe.tvalid` 排空，`is_idle` 才升高。

**需要观察的现象 / 预期结果**：

- `is_idle` **不会**在 `pause` 拉高的同一拍就升高，而是要等：当前包发完（`tlast`）→ 进 `IDLE` → skid buffer 排空（`to_dpe.tvalid==0`）。存在一个取决于「在飞包长度」的延迟。
- 在 `pause=1` 期间，输出端**不会出现**半个包：每个被打印的 `Packet received with N words` 都对应一个完整包。
- `pause=0` 后，FSM 从 `IDLE → R0` 恢复轮询，被积压的后续包开始依次送出。

> 若运行 `make -C 4.sim/rtl/dpe_multiplexer sim`，可在波形（`wave.fst`）里直接量出 `pause` 上升沿到 `is_idle` 上升沿的拍数，验证上述延迟。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么不能用「把 mux 输出的 `tready` 强行拉低」来实现暂停？

**参考答案**：`tready` 是逐拍反压，拉低会立刻堵住当前 beat。若此刻一个包只发了一半，前半截已在下游流水线里、后半截堵在 mux，下游就拿到一个被撕裂的半包，破坏加密/路由的有状态处理。FCR 的 `pause` 改为在**包边界**生效（`Sk` 等 `tlast` 才停），保证每个包要么完整通过、要么完整不被放入，不存在中间态。

**练习 2**：假设去掉 `is_idle = !to_dpe.tvalid` 里的 `!to_dpe.tvalid`，改成只要进 `IDLE` 就拉高 `is_idle`，会有什么风险？

**参考答案**：mux 进 `IDLE` 时，1 拍深 skid buffer 里可能还残留上一包的最后一拍（`to_dpe.tvalid` 仍为 1）。若此时就报 `idle=1`，CPU 会以为数据面已完全静止而开始改表，但那一拍数据其实还在通往下游的路上，可能在表更新瞬间被基于旧表项处理，造成不一致。`!to_dpe.tvalid` 这一项正是为了等这残留拍排空。

**练习 3**：`is_idle` 只反映 mux 自身输出排空，是否就能保证整条 DPE 流水线（mux → 处理级 → demux）都已静止？

**参考答案**：在当前 Phase1 PoC 里，mux 之后是组合直通的 `dpe_dummy_switch` + skid buffer，没有深层缓冲，所以 mux 排空后下游很快也排空，`is_idle` 近似反映全链路静止。但在未来完整的处理级（多级流水线 + 内部 FIFO，见 u4-l4/u4-l5）上线后，`is_idle` 仅凭 mux 输出已排空**不足以**保证下游各级都静止——完整的原子更新握手还需要考虑下游处理级的排空时序。这是一个在 PoC 阶段成立、上线后需要复核的假设（详见 4.3 节现状与 u3-l4）。

---

### 4.3 PoC 现状：dummy_switch 顶替了中间处理级

#### 4.3.1 概念说明

理解本讲的最后一个关键，是分清「**哪些是真的在线、哪些是占位**」。当前 HEAD 处于 Phase1 PoC，一个常见的误读是「整个 DPE 都是 dummy」。事实并非如此：

- **mux（本讲主角）与 demux 都是真实在线的**——它们的轮询仲裁、流控握手、地址盖写都是生效的真逻辑，综合进 bitstream。
- **被替换的只是「中间处理级」**。完整的处理级本应是 `dpe_egress_ip_lookup`（TCAM 路由查找，u4-l4）+ 后续 WireGuard 解封装/解密/加密/封装链（u4-l5）。这些源码都已写好，但在当前 HEAD 被**注释掉**，由一个组合直通模块 `dpe_dummy_switch` 顶替。
- `dpe_dummy_switch` 不是「不转发」，而是「**按固定规则交叉直通转发**」：根据 mux 盖好的 `src` 字段，硬查一张固定的对应表来填 `dst`，把包从约定好的出口送出去，全程明文、不做路由查找、不做加解密。

为什么这么做？Phase1 的目标是先把「**管线与配套机制**」打通——时钟域、FIFO、mux/demux 仲裁、CSR 表读写、FCR 原子握手、两节点 CLI——再叠上最难的加密流水线。用 dummy_switch 顶住中间，可以用同一套 CLI 配置和测试拓扑先验证「包能不能从对的口进、对的口出」，把基础设施风险与加密风险解耦。这一点 u1-l5、u4-l1 都已提及，本讲给出源码层面的直接证据。

#### 4.3.2 核心流程

`dpe_dummy_switch` 的转发逻辑（伪代码）：

```
if bypass_all or bypass_stage:        # 旁路：原样透传 dst
    outp.dst = inp.dst
else:
    case inp.src:                     # 否则按 src 硬查固定映射
        CPU   -> dst = ETH_1
        ETH_1 -> dst = CPU
        ETH_2 -> dst = CPU
        ETH_3 -> dst = ETH_4
        ETH_4 -> dst = ETH_3
outp.bypass_stage = 0                 # 一律清掉 bypass_stage
outp 的 tdata/tkeep/tlast/src 原样透传
```

注意它依赖的正是 4.1 节 mux 盖好的 `src`：mux 决定 `src=CPU`，dummy_switch 据此把 `dst` 设成 `ETH_1`。两段模块通过 `tuser_src`/`tuser_dst` 这对元数据默契配合。这也解释了 u4-l1 为什么强调「`src` 是权威字段」——它是后续所有转发判决（无论现在 dummy 还是将来真路由查找）的输入。

#### 4.3.3 源码精读

**证据一：top.filelist 里谁被编入、谁被注释。**

[1.hw/top.filelist:69-74](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.filelist#L69-L74) —— `dpe.sv`、`dpe_multiplexer.sv`、`dpe_demultiplexer.sv`、`dpe_dummy_switch.sv` 四个**未注释**（编入综合）；而 `dpe_wg_disassembler.sv` 前有 `#`（**注释**，未编入）。mux 与 demux 在列，dummy_switch 在列，真正的 WG 处理链不在列。

**证据二：dpe.sv 里真实的处理级被注释、由 dummy_switch 顶替。**

[1.hw/ip.dpe/dpe.sv:78-92](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv#L78-L92) —— 实例化 `dpe_dummy_switch`，输入接 mux 的输出 `muxed_1`、输出喂给 demux 的输入 `muxed_2`，占据「中间处理级」的位置。

[1.hw/ip.dpe/dpe.sv:94-103](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv#L94-L103) —— **被注释的** `dpe_egress_ip_lookup`（TCAM 路由查找）。它的端口注释显示：本应接 `muxed_1→muxed_2` 与 `routing_table` hwif，正是 dummy_switch 现在占的位置。注释掉它 = 路由查找尚未上线。

> 顺带一提：同文件里 `routing_table` 与 `cryptokey_table` 两块 `tdp_ram`（[dpe.sv:105-139](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv#L105-L139)）**是真实在线的**，CPU 经 CSR 能读写它们（u4-l6）。也就是说，表已经能配、能存，只是当前还没有处理级去查它们——这正是「基础设施先行、加密路由后上」的体现。

**证据三：dummy_switch 的固定交叉映射。**

[1.hw/ip.dpe/dpe_dummy_switch.sv:84-116](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_dummy_switch.sv#L84-L116) —— 组合 `always_comb`：未旁路时按 `inp.tuser_src` 查表设 `outp_sbuff.tuser_dst`（CPU→ETH1、ETH1→CPU、ETH2→CPU、ETH3↔ETH4），并把 `bypass_stage` 清 0；旁路时原样透传 `dst`。文件顶部 [L42-L69](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_dummy_switch.sv#L42-L69) 的 ASCII 拓扑图画的正是这张固定交叉表。

#### 4.3.4 代码实践

**实践目标**：把「mux 在线、demux 在线、中间是 dummy」这一现状用自己的核查坐实，并解释为什么 mux 的 `src` 盖写对 dummy_switch 不可或缺。

**操作步骤**：

1. 在 `1.hw/top.filelist` 的 DPE 段（4.3.3 证据一）逐行标注「编入」或「注释」。
2. 在 `1.hw/ip.dpe/dpe.sv` 画出三段数据流：`from_* → u_dpe_multiplexer → muxed_1 → u_dpe_dummy_switch → muxed_2 → u_dpe_demultiplexer → to_*`。
3. 思考：若 mux 不强制盖写 `src`、而是透传输入自带的 `src`，dummy_switch 的固定映射会出什么问题？

**需要观察的现象 / 预期结果**：

- 标注后能清楚看到：编入的是 mux/demux/dummy_switch，注释的是 wg 处理链；路由/密钥表的 RAM 在线但暂无处理级查询。
- 第 3 步结论：dummy_switch 完全靠 `src` 决定 `dst`。若 `src` 不可信（比如各输入自行填写、可能填错或填 0），dummy_switch 会把包送到错误出口。正因为 mux 在入口用物理端口号**权威地**盖好 `src`，dummy_switch（以及将来的真路由查找）才能信任它。这印证了 u4-l1「`src` 是 mux 强制写入的权威字段」的设计意图。

#### 4.3.5 小练习与答案

**练习 1**：在当前 HEAD 下，从 CPU 发出的一个包，最终会从哪个口出去？依据是哪段代码？

**参考答案**：从 `ETH_1` 出去。路径：CPU 包经 mux 盖 `src=DPE_ADDR_CPU` → dummy_switch 查表 `DPE_ADDR_CPU → dst=DPE_ADDR_ETH_1`（[dpe_dummy_switch.sv:94-96](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_dummy_switch.sv#L94-L96)）→ demux 按 `dst=ETH_1` 分发到 `to_eth_1`。这是固定硬编码，与路由表无关。

**练习 2**：`dpe_egress_ip_lookup` 被注释、`dpe_dummy_switch` 在线，这一事实对 u3-l4 的 FCR 原子更新握手有什么影响？

**参考答案**：由于中间是组合直通的 dummy_switch，mux 排空后下游几乎立刻排空，所以 4.2 节里「`is_idle` 升高即可安全改表」的假设在 PoC 下成立。但等 `dpe_egress_ip_lookup` 及 WG 加密链上线后，中间会出现多级流水线和内部 FIFO，`is_idle`（仅看 mux 输出）就不再等于全链路静止，原子更新握手需要相应增强。也就是说，FCR 握手机制本身是对的，但其「充分性」依赖当前浅流水线，上线后需复核。

**练习 3**：`dpe_dummy_switch` 里 `outp_sbuff.tuser_bypass_stage = 1'b0;`（[L90](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_dummy_switch.sv#L90)）这一句起什么作用？

**参考答案**：它把输出的 `bypass_stage` 强制清 0。`bypass_stage` 本是用来告诉后续处理级「跳过本级的某个阶段」（见 u4-l1）。dummy_switch 作为占位级，自身没有任何阶段可跳过，于是把该位置零，避免把上游可能遗留的 `bypass_stage=1` 误传给 demux 造成误判。这是一种「占位模块自我清洁元数据」的稳健写法。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「**端到端跟踪 + 现状核查**」小任务：

**背景**：两节点拓扑下，左节点的 CPU 想发一个 WireGuard 握手报文到右节点（经 eth1 物理口出去）。

**任务**：

1. **仲裁段**：写出这个 CPU 包在 `dpe_multiplexer` 里经过的状态序列（从 `IDLE` 起，到包尾止），并指出在哪一拍 `tuser_src` 被盖成 `DPE_ADDR_CPU`。
2. **流控段**：假设 CPU 在这个包刚发出第一拍后立刻写 `pause=1`（要改密钥表）。画出 FSM 在 `pause=1` 期间停留在哪个状态、何时才进 `IDLE`、`is_idle` 何时升高。确认：这个 CPU 包**没有被撕裂**，完整地发出了 6 拍（`tlast` 在第 6 拍）。
3. **现状段**：这个包出了 mux 后，经谁处理、`dst` 被设成什么、最终从哪个物理口出去？引用 `dpe.sv` 与 `dpe_dummy_switch.sv` 的具体行号说明。
4. **核查**：在 `top.filelist` 里圈出本任务涉及的、真实编入综合的模块清单，确认 `dpe_wg_disassembler.sv` 不在其中。

**预期结果（自检）**：

1. 序列：`IDLE → R0 → S0 → S0 → …(共 6 拍)… → S0`（包尾 `tlast`）；`tuser_src` 在进入 `R0/S0` 输出有效的那一刻即被盖成 `DPE_ADDR_CPU`（[dpe_multiplexer.sv:183](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_multiplexer.sv#L183)）。
2. `pause=1` 到来时 FSM 在 `S0`；因离开 `S0` 必须等 `tlast`，所以它会**留在 S0 把剩余拍发完**，第 6 拍 `tlast` 后才 `S0 → IDLE`；进 `IDLE` 后等 `to_dpe.tvalid` 排空，`is_idle` 升高。CPU 包完整发出，未被撕裂。
3. 经 `dpe_dummy_switch`（[dpe.sv:79-82](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv#L79-L82)），`src=CPU` 查表得 `dst=ETH_1`（[dpe_dummy_switch.sv:94-96](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_dummy_switch.sv#L94-L96)），demux 据此从 `to_eth_1` 物理口发出。
4. 编入清单含 `dpe.sv`/`dpe_multiplexer.sv`/`dpe_demultiplexer.sv`/`dpe_dummy_switch.sv`（[top.filelist:70-73](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.filelist#L70-L73)）；`dpe_wg_disassembler.sv` 被注释（[top.filelist:74](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.filelist#L74)）。

---

## 6. 本讲小结

- `dpe_multiplexer` 是 DPE 的入口大门，用**按包轮询**仲裁 CPU + 4 个以太网口共 5 路输入，固定顺序 CPU→eth1→eth2→eth3→eth4→CPU，非抢占（一个包发完才换路）。
- 它是一台 11 状态 FSM（`IDLE` + 每路一对 `Rk`/`Sk`），`Rk` 探头、`Sk` 发送；选中某路时**强制盖写** `tuser_src` 为该路地址常量，`tuser_dst` 原样透传。
- `pause`/`is_idle` 构成 u3-l4 的 FCR 握手在 mux 的落地：`pause` 在**包边界**生效（`Sk` 必等 `tlast` 才停），绝不用 `tready` stall 撕裂在飞包；`is_idle` 仅在 `IDLE` 且 `to_dpe.tvalid==0`（含 skid buffer 残留排空）时才升高。
- mux 是 5 路汇流的**单点闸门**，装在此处即可一处控制全局流入，是原子更新的天然卡口。
- 当前 HEAD 的 Phase1 现状：**mux 与 demux 真实在线**，被 `dpe_dummy_switch`（按 `src` 硬查固定映射填 `dst` 的组合直通级）顶替的是**中间处理级**（`dpe_egress_ip_lookup` 与 WG 加解密链被注释，见 `top.filelist` 与 `dpe.sv`）。
- 因此 4.2 节「`is_idle` 即代表全链路静止」只在当前浅流水线（dummy_switch）下近似成立；待路由查找与加密链上线后需复核原子握手的充分性。

## 7. 下一步学习建议

- **横向对称**：下一篇 **u4-l3 解复用器 `dpe_demultiplexer`** 讲 mux 的镜像——按 `tuser_dst` 把一路分发到 5 路。读完两篇你就掌握了 DPE 的「外壳」。
- **向下深挖流控**：回看 **u3-l4 FCR 流控寄存器与原子更新**，把本讲的 FSM 行为和 CPU 侧 8 步握手对照，理解软硬件如何协同避免脏读。
- **向前看处理级**：本讲反复提到的「中间处理级」在 **u4-l4 TCAM 最长前缀路由查找** 和 **u4-l5 WireGuard 封装/解封装与加解密数据流** 中正式上线，届时可重新评估 `is_idle` 与全链路静止的关系。
- **动手验证**：本讲的 `4.sim/rtl/dpe_multiplexer/` 测试台已并发发包并驱动 `pause`，是观察轮询与暂停最直接的实验台；若本地有 Verilator，`make -C 4.sim/rtl/dpe_multiplexer sim` 即可跑起来对照波形。
