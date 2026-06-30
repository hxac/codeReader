# 异构网络设计实战

## 1. 本讲目标

本讲是「专家层」的综合实战篇。前面你已经分别学过单点的积木：路由骨干 `axi_xbar`、跨时钟域 `axi_cdc`、数据宽度转换 `axi_dw_converter`、ID 宽度转换 `axi_iw_converter`、总线隔离 `axi_isolate`。本讲不再重复讲解每个模块的内部实现，而是回答一个更难的问题：

> **如何把这五件积木背靠背拼成一张「真实异构片上网络」，并且不踩时序回路与死锁的坑？**

学完后你应当能够：

1. 识别一条 AXI 链路上存在的三类「不匹配」——时钟域、数据宽度、ID 宽度——并知道每类该用哪件积木去消化。
2. 给定一个含两个时钟域、64↔32 数据宽度、宽窄 ID 的两子网拓扑，能画出每个转换器与 CDC 的正确**摆放次序**。
3. 理解「为什么状态化的转换器（dw/iw/cdc/isolate）只能放在 `axi_xbar` 的**对外边界**，而不能塞进它的内部 cross 矩阵」——这是异构网络无死锁的核心边界条件。
4. 合理选择 `isolate` 的部署点，在上下电/复位流程里安全排空在途事务。

本讲默认你已经掌握 U6（xbar）、U8（cdc）、U11（dw_converter）、U10（iw_converter）、U7（isolate）的内部机制。

## 2. 前置知识

在动手拼网络之前，先用三段话把「为什么要拼」讲清楚。

### 2.1 什么是「异构」

一颗真实的 SoC 里，不同子模块往往「说不同的方言」：

- **CPU 核**跑在 1 GHz，数据总线 64 位，ID 8 位，发起完整 AXI4 突发甚至原子操作。
- **低速外设子系统**跑在 50 MHz，数据 32 位，只懂 AXI4-Lite 或窄 AXI4。
- **DRAM 控制器**跑在独立时钟，数据 64 位但 ID 只有 2 位、不支持突发回卷。

当这些子模块要互联时，它们之间的链路就同时存在三类不匹配。**异构网络的设计本质，就是在每条跨子网的链路上，按正确次序插入正确的转换器，把不匹配逐一消化掉。**

### 2.2 五件积木的分工

| 积木 | 消化的不匹配 | 是否状态化（含 FIFO/寄存器） | 在网络中的角色 |
|---|---|---|---|
| `axi_xbar` | 多主多从的**路由** | 内部**纯组合**（spill 只在对外边界） | 路由骨干 |
| `axi_cdc` | **时钟域** | 是（每通道一个 Gray FIFO） | 时钟域边界 |
| `axi_dw_converter` | **数据宽度** | 是（拆/合并突发） | 宽度适配 |
| `axi_iw_converter` | **ID 宽度** | 是（remap/serialize 含表/队列） | ID 适配 |
| `axi_isolate` | **电源/复位** 期间的安全 | 是（在途计数 + FSM） | 边界隔离门 |

记住一条贯穿全讲的线索：**只有 `axi_xbar` 内部是纯组合的，其余四件都是状态化的**。这条线索决定了它们各自的合法摆放位置。

### 2.3 一个关键术语：宽度契约（width contract）

转换器之所以能背靠背串联，是因为它们各自只改一类字段、对其它字段透明。例如 `axi_cdc` 不改任何字段只跨域，`axi_dw_converter` 只改数据/突发、不改 ID，`axi_iw_converter` 只改 ID、不改地址/数据。两两拼接时，**相邻积木在共享字段上的宽度必须相等**——这就是「宽度契约」。本讲的很多设计决策，本质都是在保证整条链路上的宽度契约处处成立。

## 3. 本讲源码地图

本讲只引用以下五个源文件（均为 `src/` 下可综合 RTL）：

| 文件 | 作用 | 本讲视角 |
|---|---|---|
| `src/axi_xbar.sv` | 全连接交叉开关，路由骨干 | 它的「纯组合内部 + spill 在边界」结构，决定了转换器只能挂在外面 |
| `src/axi_cdc.sv` | 跨时钟域，每通道一个 Gray FIFO | 它把网络切成两个时钟域，是时序约束的天然分界 |
| `src/axi_dw_converter.sv` | 数据宽度转换的统一入口 | 它的三路 `generate` 自动选 up/down/直通 |
| `src/axi_iw_converter.sv` | ID 宽度转换的统一入口 | 它的四路 `generate` 自动选 prepend/remap/serialize/直通 |
| `src/axi_isolate.sv` | 总线隔离，排空后静默 | 它是上下电/复位门控前的安全阀 |

此外，第 5 节的综合实践会以 `test/tb_axi_cdc.sv` 作为「双时钟域 + 监控队列」的接线范本。

## 4. 核心概念与源码讲解

下面按「骨干 → 边界 → 适配器 → 隔离门」的顺序，逐个讲清每件积木在异构网络里的**角色、摆放约束与宽度契约**。

### 4.1 axi_xbar：路由骨干与组合原语

#### 4.1.1 概念说明

`axi_xbar` 是整张异构网络的「路由骨干」：它接收 `NoSlvPorts` 个 slave 端口、按全局地址映射把事务路由到 `NoMstPorts` 个 master 端口之一。它的设计哲学是**组合优于配置**——顶层只例化两类子模块，不堆逻辑。

在异构网络里，xbar 有两个**不可动摇**的性质，是后续所有摆放决策的出发点：

1. **内部纯组合**：demux 阵列与 mux 阵列之间是直连的，**不插任何状态化元件**（spill 寄存器只能开在对外端口，由 `LatencyMode` 控制）。
2. **master 端口 ID 比 slave 端口宽**：master 端 ID 宽度 = slave 端 ID 宽度 + ⌈log₂(NoSlvPorts)⌉，多出的高位是 mux 前置的来源端口标签，用于把响应路由回正确的源。

#### 4.1.2 核心流程

xbar 顶层的组合关系（伪代码）：

```
axi_xbar  =  axi_xbar_unmuxed( demux 阵列 + cross 矩阵 + err_slv )
          +  for 每个 master 端口: axi_mux          // NoMstPorts 个
```

关键事实：

- `axi_xbar_unmuxed` 内部：每个 slave 端口一个 demux（按地址译码选 master 端口），译码失败的那一路接 `axi_err_slv` 返回 DECERR。
- demux 与 mux 之间用 `axi_multicut` 摆成 cross 矩阵，**纯组合直连**。
- 每个方向（AW/AR/W/B/R）的跨 demux↔mux 路径上**没有任何 FIFO/寄存器**——这正是 u6-l3 证明的无死锁边界条件。

ID 宽度展开（接口外壳里）：

\[ \text{AxiIdWidthMstPorts} = \text{AxiIdWidthSlvPorts} + \lceil \log_2(\text{NoSlvPorts}) \rceil \]

#### 4.1.3 源码精读

xbar 顶层只做两件事：例化一个 `axi_xbar_unmuxed`，再例化 `NoMstPorts` 个 `axi_mux`。

模块声明与关键参数（`Cfg` 配置结构体、`ATOPs`、`Connectivity` 矩阵、各类通道类型）见 [src/axi_xbar.sv:18-91](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv#L18-L91)，中文说明：这是 xbar 的对外契约，`Cfg` 收口了几乎所有参数，`Connectivity` 矩阵可剪裁「哪个 slave 能到哪个 master」的链路。

「1 个 unmuxed + NoMstPorts 个 mux」的组合见 [src/axi_xbar.sv:97-155](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv#L97-L155)，中文说明：`i_xbar_unmuxed` 把每路 demux 的输出汇成 `mst_reqs`/`mst_resps` 二维数组，`gen_mst_port_mux` 循环为每个 master 端口配一个 mux；**整段没有任何在 demux 与 mux 之间插入的寄存器**。

master 端 ID 宽度展开的真正定义在接口外壳里：[src/axi_xbar.sv:183-186](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv#L183-L186)，中文说明：`AxiIdWidthMstPorts = Cfg.AxiIdWidthSlvPorts + $clog2(Cfg.NoSlvPorts)`，并据此派生 `id_mst_t` 与 `id_slv_t` 两套类型——这就是宽度契约在 xbar 内部的体现。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：亲手确认「xbar 内部 demux↔mux 之间无状态化元件」这一无死锁前提。
2. **操作步骤**：
   - 打开 [src/axi_xbar.sv:97-155](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_xbar.sv#L97-L155)。
   - 数清楚：顶层实例化了**几**个 `axi_xbar_unmuxed`、**几**个 `axi_mux`？mux 的 spill 开关来自 `Cfg.LatencyMode` 的哪些位？
   - 进入 `axi_xbar_unmuxed`（`src/axi_xbar_unmuxed.sv`）查看 demux 与 cross 矩阵之间的连线。
3. **需要观察的现象**：从 demux 输出到 mux 输入，整条路径上**找不到** `axi_fifo` / `axi_cdc` / `spill_register` 之外的缓冲（cross 矩阵是 `axi_multicut`，`NoCuts` 由 `Cfg.PipelineStages` 决定，但默认边界是 0）。
4. **预期结果**：你会得出结论——**任何带 FIFO 的转换器都不能塞进 xbar 内部**，只能挂在它的对外 master/slave 端口上。这是本讲最重要的结论之一。
5. 待本地验证（如需在源码里追踪 cross 矩阵细节）。

#### 4.1.5 小练习与答案

**练习 1**：一个 2 slave × 3 master 的 xbar，master 端 ID 比 slave 端宽几位？  
**答**：⌈log₂(NoSlvPorts)⌉ = ⌈log₂(2)⌉ = **1 位**。

**练习 2**：如果想在两个 master 端口之间共享一个窄 ID 的下游外设，能否把一个 `axi_iw_converter` 直接插在 xbar 内部某个 mux 之前？为什么？  
**答**：**不能**。`axi_iw_converter` 是状态化的（remap 含映射表、serialize 含队列），插进 xbar 内部会破坏 u6-l3 证明的「demux↔mux 之间纯组合」无死锁前提。它只能挂在 xbar 的**对外 master 端口**上，位于 mux 之后。

---

### 4.2 axi_cdc：时钟域边界

#### 4.2.1 概念说明

`axi_cdc` 是网络的**时钟域边界**：它的 push 端在源时钟域、pop 端在目的时钟域。它为五条 AXI 通道各实例化一个 Gray 编码的异步 FIFO，把整张网络天然地切成两半——两侧各自有自己的时钟、复位与时序约束。

在异构网络里，CDC 是**所有时序约束的分界线**：跨过它的路径都是异步路径（用 `(* async *)` 标注），必须在 SDC/约束文件里**单独、显式**地约束。这是它和其它转换器最大的区别。

#### 4.2.2 核心流程

CDC 的组合关系：

```
axi_cdc  =  axi_cdc_src( 源域, 5 个写指针/数据端口 )
         +  axi_cdc_dst( 目的域, 5 个读指针/数据端口 )
中间用  async_data_*  数组 +  async_*_wptr/rptr  异步互联
```

源码注释反复强调一条铁律——**每条 AXI 通道必须正确约束三条路径**（指针跨域同步、数据相对指针的稳定窗口、复位释放）。Gray 编码保证同一时刻只有一比特变化，从而把多比特指针的跨域采样变成「至多多滞後一拍」的安全问题。

两个容量参数：

- `LogDepth`：FIFO 深度 = 2^LogDepth，决定能吸收多少跨域 burst。
- `SyncStages`：指针同步寄存器级数（默认 2），级数越多越抗亚稳态但延迟越大。

#### 4.2.3 源码精读

CDC 的参数与双时钟域端口布局见 [src/axi_cdc.sv:24-47](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cdc.sv#L24-L47)，中文说明：`src_*` 一组信号由 `src_clk_i` 驱动、`dst_*` 一组由 `dst_clk_i` 驱动，`LogDepth`/`SyncStages` 是两个关键容量参数。

「必须约束三条路径」的警告直接写在模块头注释里：[src/axi_cdc.sv:19-23](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cdc.sv#L19-L23)，中文说明：作者明确提醒每通道要约束三条路径，并指向 `cdc_fifo_gray` 的头注释。

源域子模块例化与异步端口标注见 [src/axi_cdc.sv:60-90](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cdc.sv#L60-L90)，目的域子模块见 [src/axi_cdc.sv:92-122](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_cdc.sv#L92-L122)，中文说明：两侧之间用 `(* async *)` 标注的数据阵列与指针互联，这正是综合工具识别「异步路径、不要做普通时序检查」的依据。

#### 4.2.4 代码实践（跟踪 + 设计型）

1. **实践目标**：理解 CDC 如何把网络切成两个时序域，并学会布置它。
2. **操作步骤**：
   - 阅读范本 `test/tb_axi_cdc.sv`，看它如何用**两个** `clk_rst_gen` 产生 `TCLK_UPSTREAM=10ns` 与 `TCLK_DOWNSTREAM=3ns` 两个不同周期的时钟（[test/tb_axi_cdc.sv:50-64](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_cdc.sv#L50-L64)）。
   - 看 `axi_cdc_intf` 如何把上游 `upstream`（上游时钟）接到下游 `downstream`（下游时钟）：[test/tb_axi_cdc.sv:118-131](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_cdc.sv#L118-L131)。
   - 注意：上游用一个 `axi_rand_master`、下游用一个 `axi_rand_slave`，两侧各自跑在自己的时钟域。
3. **需要观察的现象**：master 与 slave 的 TA/TT 各自相对**自己的**时钟周期定义（`TA_UPSTREAM = TCLK_UPSTREAM*1/4`、`TA_DOWNSTREAM = TCLK_DOWNSTREAM*1/4`）。
4. **预期结果**：你应能说出——CDC 两侧的验证组件**必须各用各的时钟域**，监控队列也要分别在两个 `posedge` 上采样。
5. **设计思考**：若要在 master 与 CDC 之间再插一个 `axi_dw_converter`（64→32），它该用哪个时钟？**答：用上游时钟**——因为宽度转换发生在跨域**之前**，否则转换器的状态机会跨在两个域上，无法综合。（待本地验证组合后无死锁。）

#### 4.2.5 小练习与答案

**练习 1**：为什么 CDC 的两个子模块要拆成 `axi_cdc_src` 和 `axi_cdc_dst` 分别放在两侧时钟域，而不是合成一个模块？  
**答**：拆开后，每个子模块**整体落在单一时钟域**，便于分别综合、分别约束；中间的异步数据阵列与指针可以集中标注 `(* async *)` 并在约束文件里统一设为 false path / max delay。合成一个模块会让综合器难以区分两域寄存器。

**练习 2**：`LogDepth=1` 和 `LogDepth=3` 的 FIFO 深度分别是多少？跨一个突发长度 8 的写，哪个更安全？  
**答**：深度分别是 2 和 8。突发长度 8 时 `LogDepth=3`（深 8）能整段吸收，`LogDepth=1`（深 2）会在突发期间频繁反压上游。

---

### 4.3 axi_dw_converter：数据宽度适配

#### 4.3.1 概念说明

`axi_dw_converter` 是数据宽度不匹配的统一消化器。给定 slave 端宽度 `AxiSlvPortDataWidth` 和 master 端宽度 `AxiMstPortDataWidth`，它在综合期自动三选一：

- 相等 → **直通**（零开销 `assign`）。
- master 更宽 → **upsizer**（多拍窄数据合并成少拍宽数据）。
- master 更窄 → **downsizer**（一拍宽数据拆成多拍窄数据）。

在异构网络里，它的角色是**宽度适配**。注意两点工程事实：

1. 它**是状态化的**（拆/合并突发需要缓冲与计数），所以和 CDC 一样只能放在 xbar 的对外边界，不能塞进 cross 矩阵。
2. 它**不改 ID、不改地址宽度**，只改数据相关字段——因此与 `axi_iw_converter`、`axi_cdc` 字段正交，可以串联。

#### 4.3.2 核心流程

三路 `generate` 的分发逻辑（伪代码）：

```
if (MstData == SlvData)        assign 直通;
else if (MstData > SlvData)    例化 axi_dw_upsizer;
else /* MstData < SlvData */   例化 axi_dw_downsizer;
```

容量参数 `AxiMaxReads` 决定其内部能缓存的在途读事务数，影响反压行为。源码头注释还提醒两条限制（upsizer 不支持 WRAP 突发、downsizer 不支持 len≠0 的 FIXED 突发），异构设计里若下游是这类转换器，上游应避免发这类突发。

#### 4.3.3 源码精读

三路分发见 [src/axi_dw_converter.sv:46-109](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_converter.sv#L46-L109)，中文说明：

- 直通分支 [src/axi_dw_converter.sv:46-49](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_converter.sv#L46-L49)：`assign mst_req_o = slv_req_i`，纯组合零开销。
- upsize 分支 [src/axi_dw_converter.sv:51-79](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_converter.sv#L51-L79)。
- downsize 分支 [src/axi_dw_converter.sv:81-109](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_converter.sv#L81-L109)。

两条限制写在文件头注释：[src/axi_dw_converter.sv:14-16](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_dw_converter.sv#L14-L16)，中文说明：upsizer 遇 WRAP 会回 SLVERR、downsizer 遇 len≠0 的 FIXED 会出错——异构网络里这是上游 burst 生成器必须避开的雷区。

#### 4.3.4 代码实践（设计型）

1. **实践目标**：把宽度转换器正确布置在跨时钟域链路上。
2. **场景**：CPU 子网（64 位、上游时钟）要访问外设子网（32 位、下游时钟）。
3. **操作步骤**：在纸上画出两种摆法并比较：
   - **摆法 A**：`CPU → dw_downsizer(64→32) → cdc → 外设`。dw 用上游时钟。
   - **摆法 B**：`CPU → cdc → dw_downsizer(64→32) → 外设`。dw 用下游时钟。
4. **需要观察的现象 / 预期结果**：两种**都能正确工作**（dw 在哪个域都合法，只要它整体落在单一域）。但摆法 A 让「跨域的数据量更少」（拆完再跨，FIFO 传的是 32 位），摆法 B 让「跨域的突发更短」（先跨 64 位再拆）。
5. **结论**：宽度转换与时钟域跨越**互不冲突**，次序选择取决于你更想省 CDC FIFO 的位宽（选 A）还是深度（选 B）。这是一个真实的工程权衡，**待本地综合后据面积/时序定夺**。

#### 4.3.5 小练习与答案

**练习 1**：`AxiSlvPortDataWidth == AxiMstPortDataWidth` 时，`axi_dw_converter` 综合出多少逻辑？  
**答**：零逻辑——直通分支就是两句 `assign`，等宽链路零开销。

**练习 2**：能否把 `axi_dw_converter` 插在 `axi_xbar` 内部、某个 mux 的输入侧以适配一个窄 master？  
**答**：**不能**。它是状态化的，会破坏 xbar 内部「demux↔mux 纯组合」的无死锁前提。正确做法是把它挂在那个 master 端口**之外**（mux 之后），即 `xbar.mst_port[i] → dw_converter → 下游`。

---

### 4.4 axi_iw_converter：ID 宽度适配

#### 4.4.1 概念说明

`axi_iw_converter` 是 ID 宽度不匹配的统一消化器。给定 slave 端 `AxiSlvPortIdWidth` 和 master 端 `AxiMstPortIdWidth`，它在综合期自动**四**选一（比 dw 多一路）：

- 相等 → **直通**。
- master 更宽 → **prepend**（ID 高位补 0，用 `axi_id_prepend`）。
- master 更窄、且 slave 端唯一 ID 数 ≤ 2^master 宽 → **remap**（建一张映射表，用 `axi_id_remap`，不同 ID 仍可独立重排）。
- master 更窄、且 slave 端唯一 ID 数 > 2^master 宽 → **serialize**（部分 ID 被迫串行化，用 `axi_id_serialize`，会牺牲并发）。

它在异构网络里最关键的作用，是**消化 xbar 出口处天然产生的 ID 宽度差**。回忆 4.1：xbar 的 master 端 ID 比 slave 端宽 ⌈log₂(NoSlvPorts)⌉ 位。当你把一个窄 ID 的下游外设接到 xbar 的某个 master 端口时，链路上就出现了「xbar 出口宽 ID → 外设窄 ID」的不匹配，必须用 `axi_iw_converter`（走 remap 或 serialize 分支）来消化。

#### 4.4.2 核心流程

四路分发（伪代码）：

```
if (MstId < SlvId):
    if (SlvMaxUniqIds <= 2^MstId):   axi_id_remap      // 仍可独立重排
    else:                             axi_id_serialize  // 被迫串行化
else if (MstId > SlvId):              axi_id_prepend    // 高位补 0
else:                                 assign 直通
```

关键容量参数：

- `AxiSlvPortMaxUniqIds`：slave 端预期同时在途的不同 ID 数上限——它决定走 remap 还是 serialize。
- `AxiSlvPortMaxTxnsPerId` / `AxiSlvPortMaxTxns`：每 ID 在途数 / 总在途数，决定内部表/队列深度。

注意它**不改地址、不改数据宽度**——所以可以与 dw_converter、cdc 自由串联，只要各自负责的字段宽度契约满足。

#### 4.4.3 源码精读

四路分发是本模块的核心：[src/axi_iw_converter.sv:127-220](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_iw_converter.sv#L127-L220)，中文说明：

- 降宽 + remap 分支 [src/axi_iw_converter.sv:127-145](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_iw_converter.sv#L127-L145)：条件 `AxiSlvPortMaxUniqIds <= 2**AxiMstPortIdWidth`。
- 降宽 + serialize 分支 [src/axi_iw_converter.sv:146-168](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_iw_converter.sv#L146-L168)：唯一 ID 太多，被迫串行化。
- 升宽（prepend）分支 [src/axi_iw_converter.sv:169-216](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_iw_converter.sv#L169-L216)：复用 `axi_id_prepend` 把 `'0` 前置进高位。
- 等宽直通分支 [src/axi_iw_converter.sv:217-220](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_iw_converter.sv#L217-L220)：两句 `assign`。

模块头注释对「降宽两选项」的工程含义解释得很清楚：[src/axi_iw_converter.sv:18-43](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_iw_converter.sv#L18-L43)，中文说明：唯一 ID 少于 master 容量时仍可独立重排（高并发），多于时只能串行化（牺牲并发换正确性）。

#### 4.4.4 代码实践（设计型）

1. **实践目标**：学会用容量参数 `AxiSlvPortMaxUniqIds` 控制 remap/serialize 的分支选择。
2. **场景**：一个 4 slave 的 xbar，master 端 ID = 8 位（= slave 端 6 位 + ⌈log₂4⌉=2 位）。要把它的一个 master 端口接到一个 **2 位 ID** 的 DRAM 控制器。
3. **操作步骤**：在纸上推演——slave 端 ID 宽 8 位，master（DRAM）端 ID 宽 2 位，`2^MstId = 4`。
   - 若 CPU 最多同时用 3 个不同 ID：`AxiSlvPortMaxUniqIds=3 ≤ 4` → 走 **remap**，三个 ID 仍可独立重排，吞吐基本无损。
   - 若 CPU 最多同时用 8 个不同 ID：`AxiSlvPortMaxUniqIds=8 > 4` → 走 **serialize**，部分事务被串行化，吞吐下降。
4. **需要观察的现象 / 预期结果**：你能根据「在途唯一 ID 数 vs 2^窄ID宽」预判分支，并据此告诉架构师：要么加宽 DRAM 的 ID 端口，要么限制 CPU 的并发 ID 数。
5. **结论**：`AxiSlvPortMaxUniqIds` 是面积与吞吐的旋钮；选错不会错（仍然正确），但会白白牺牲并发。**待本地仿真确认反压行为。**

#### 4.4.5 小练习与答案

**练习 1**：xbar 的 master 端口要接一个等宽 ID 的下游，还需要 `axi_iw_converter` 吗？  
**答**：可以不要（直通分支零开销），但放一个等宽 `axi_iw_converter` 也只综合出两句 `assign`，无副作用，可作为占位以便日后改宽。

**练习 2**：为什么降宽时优先走 remap 而非 serialize？  
**答**：remap 在「唯一 ID 数 ≤ 2^窄宽」时仍让不同 ID **彼此独立、可乱序**，保留并发；serialize 会把多个 slave 端 ID 强制排到同一 master ID 上、彼此保序，牺牲并发。remap 是无损的，serialize 是有损的。

---

### 4.5 axi_isolate：边界隔离门

#### 4.5.1 概念说明

`axi_isolate` 是网络的**安全阀**。它的两个握手信号 `isolate_i` / `isolated_o` 构成一个优雅排空协议：

1. 上游拉高 `isolate_i`，请求隔离。
2. 模块**继续把已经在途的事务跑完**（graceful drain），但不再接收新事务。
3. 所有在途事务都收到响应后，`isolated_o` 拉高——此时 master 端输出全部静默为 `'0`，可以安全地对该域下电/复位。

`TerminateTransaction` 参数决定隔离期间新到事务的命运：置 0 则**无限阻塞**直到解隔离；置 1 则**返回错误响应**（数据填魔数 `1501A7ED`，「isolated」的 hexspeak）。

在异构网络里，isolate 的典型部署点是**时钟域/电源域的边界、紧贴 CDC 的上游侧**：下电前先 isolate 排空，确保 CDC FIFO 与下游没有残留事务，再安全断电。

#### 4.5.2 核心流程

isolate 的排空状态机（伪代码，读写各一份）：

```
Normal  --isolate_i-->  Drain  --计数归零-->  Isolate  --!isolate_i-->  Normal
                            ↑ 拒收新请求          ↑ 输出全静默
```

关键机制：

- 用 `pending_aw/ar/w` 三个计数器跟踪在途事务（AW 握手 +1、B 握手 −1；AR 握手 +1、R 最后一拍 −1）。
- ATOP 原子读改写无 AR 却有 R，所以在 AW 握手时向 AR 计数器 **inject +1**，防止 R 通道下溢。
- `isolated_o = (写状态机==Isolate) && (读状态机==Isolate)`，两侧都排空才算真正隔离。

#### 4.5.3 源码精读

模块头注释对排空协议与两种 `TerminateTransaction` 行为的完整说明见 [src/axi_isolate.sv:19-39](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_isolate.sv#L19-L39)，中文说明：isolate_i 触发排空、isolated_o 表示已静默、TerminateTransaction=1 时返回 `1501A7ED` 错误响应。

参数与端口（`NumPending` 决定可跟踪在途数上限）见 [src/axi_isolate.sv:40-77](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_isolate.sv#L40-L77)。

`TerminateTransaction=1` 时内部用一个 `axi_demux` + `axi_err_slv` 把隔离期间的新事务引到错误从端：[src/axi_isolate.sv:94-148](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_isolate.sv#L94-L148)，中文说明：demux 的 select 由 `isolated_o` 驱动，隔离时新事务走 err_slv 回 DECERR + 魔数。

排空状态机（Normal/Hold/Drain/Isolate 四态）与 ATOP inject 见 [src/axi_isolate.sv:263-388](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_isolate.sv#L263-L388)；`isolated_o` 的与逻辑见 [src/axi_isolate.sv:388](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_isolate.sv#L388)，中文说明：读写状态机都进入 Isolate 才宣告隔离完成。

#### 4.5.4 代码实践（源码阅读 + 推理型）

1. **实践目标**：确认 isolate 能与 ATOP、CDC 共存，并理解它的部署点。
2. **操作步骤**：
   - 阅读 [src/axi_isolate.sv:219-246](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_isolate.sv#L219-L246)，确认 AW 握手时若 `aw.atop[ATOP_R_RESP]` 为 1，会向 AR 计数器 inject +1。
   - 思考：如果 isolate 部署在 CDC 上游侧、且上游会发 ATOP，排空能否正确？**答**：能——inject 抵消了原子写未来在 R 通道的 pop，计数不会下溢，`isolated_o` 会在 B 和 R 都排空后才拉高。
3. **需要观察的现象 / 预期结果**：你会得出 isolate 的部署准则——**放在要被下电/复位的那一侧、紧贴 CDC 之前**，让排空过程把 CDC FIFO 与下游残留事务一并清空。
4. **预期结果**：`TerminateTransaction=0` 时隔离期间新事务挂起（适合计划内维护），`=1` 时新事务立即拿错误响应（适合故障隔离）。
5. 待本地验证（可参考 `test/tb_axi_isolate.sv` 构造隔离场景）。

#### 4.5.5 小练习与答案

**练习 1**：`isolated_o` 拉高后，master 端口的输出是什么？  
**答**：全部静默为 `'0``（valid、payload 都为 0），对下游呈现「无源」，可安全下电。

**练习 2**：为什么 isolate 要部署在 CDC **上游**而不是下游？  
**答**：上游 isolate 能让排空过程把已经 push 进 CDC FIFO 的事务**继续 pop 完成**，保证 FIFO 被排空、下游无残留；若放下游，上游仍可能往已下电的 FIFO 里 push，造成丢事务或亚稳态。

---

## 5. 综合实践

现在把五件积木拼成一张完整的异构网络。这是本讲的核心任务。

### 5.1 设计需求

设计一个含**两个时钟域**的两子网互联：

- **子网 A（CPU 域，上游时钟 1 GHz 等效）**：CPU 主端，64 位数据、6 位 ID，发起 AXI4 突发，可能发 ATOP。
- **子网 B（外设域，下游时钟 50 MHz 等效）**：一个窄外设子系统，32 位数据、2 位 ID、不支持长突发。

两个子网通过一条公共链路互连。

### 5.2 框图（请在纸上画出并标注）

推荐的拓扑如下（`→` 表示数据流向，从 master 到 slave）：

```
                    子网 A (上游时钟)                          子网 B (下游时钟)
   ┌─────────┐   ┌──────────┐   ┌──────┐   ┌─────┐   ┌──────────┐   ┌──────────┐
   │  CPU    │──>│ xbar_A   │──>│isolate│──>│ CDC │──>│dw_downsizer│──>│iw_converter│──> 外设(32b/2b ID)
   │ 64b/6bID│   │ (路由骨干)│   │(安全阀)│   │(跨域)│   │ (64→32)  │   │(宽ID→2bID) │
   └─────────┘   └──────────┘   └──────┘   └─────┘   └──────────┘   └──────────┘
        其它master        其它slave端口                                                       其它外设
```

每个积木的**摆放次序**与理由（这是综合实践要写清楚的部分）：

| 位置 | 积木 | 摆放理由 |
|---|---|---|
| ① xbar_A | `axi_xbar` | 路由骨干，挂在子网 A 内部，**纯组合**，是整条链路的起点 |
| ② isolate | `axi_isolate` | 紧贴 xbar 出口、**在 CDC 之前**（上游时钟域），下电前先排空，把 CDC FIFO 一并清空 |
| ③ CDC | `axi_cdc` | 时钟域边界，把链路切成上下游两个时序域，三条异步路径需单独约束 |
| ④ dw_downsizer | `axi_dw_converter` | 放在 CDC **之后**（下游时钟域），把 64→32；下游用下游时钟 |
| ⑤ iw_converter | `axi_iw_converter` | 放在最末端、紧贴外设，把宽 ID（含 xbar 加的路由位）remap 成 2 位 ID |

### 5.3 宽度契约自检表（关键！）

沿链路逐段检查每个字段宽度，确认契约处处成立：

| 链路段 | 数据宽度 | ID 宽度 | 时钟域 |
|---|---|---|---|
| CPU → xbar_A.slv | 64 | 6 | 上游 |
| xbar_A.mst → isolate | 64 | 6 + ⌈log₂NoSlvPorts⌉ | 上游 |
| isolate → CDC.src | 64 | 同上 | 上游 |
| CDC.dst → dw_downsizer | 64 | 同上 | **下游** |
| dw_downsizer → iw_converter | **32** | 同上 | 下游 |
| iw_converter → 外设 | 32 | **2** | 下游 |

要点：

- **ID 宽度只被 iw_converter 改一次**（在最末端）；它前面的所有段共享同一个宽 ID，符合宽度契约。
- **数据宽度只被 dw_converter 改一次**；它后面的段都是 32 位。
- **时钟域只被 CDC 切一次**；dw_converter 落在下游域、isolate 落在上游域，各自整体在单一域内，可综合。

### 5.4 无死锁边界自检

对照 4.1 的核心结论做三选一确认：

1. xbar_A 内部有没有插任何 dw/iw/cdc/isolate？**没有**——它们全在 xbar 对外端口之外。✅
2. CDC 与 dw/iw/isolate 有没有跨在两个时钟域上？**没有**——每个都整体落在单一域。✅
3. isolate 有没有挡住 CDC 的排空？**没有**——isolate 在 CDC 上游，排空时 CDC FIFO 仍可正常 pop。✅

只要这三条满足，这张异构网络就处在 u6-l3 证明的无死锁边界内。

### 5.5 动手验证建议（待本地验证）

若有仿真环境，可参照 `test/tb_axi_cdc.sv` 的双时钟域骨架，把上面的链路逐步搭起来：

1. 先只搭 `rand_master → CDC → rand_slave`，确认跨域读写无误（这正是 `tb_axi_cdc.sv` 已验证的）。
2. 在下游侧加 `dw_downsizer`，把 master 的 64 位、slave 的 32 位接好，用 scoreboard 验证拆拍正确。
3. 再在末端加 `iw_converter`（降宽 + remap），验证响应按正确 ID 回送。
4. 最后在上游侧加 `isolate`，构造一次隔离：发若干在途事务后拉 `isolate_i`，观察 `isolated_o` 是否在所有 B/R 都返回后才拉高。

由于本环境无法运行仿真，上述步骤标注为**待本地验证**；但其拓扑与接线依据均来自本讲引用的真实源码。

## 6. 本讲小结

- 异构网络的三类不匹配（时钟域、数据宽度、ID 宽度）分别由 `axi_cdc`、`axi_dw_converter`、`axi_iw_converter` 消化，`axi_xbar` 做路由骨干、`axi_isolate` 做边界安全阀。
- **核心边界条件**：只有 `axi_xbar` 内部是纯组合的；所有状态化转换器（cdc/dw/iw/isolate）只能挂在 xbar 的**对外端口**上，不能塞进 cross 矩阵——这是无死锁的前提。
- `axi_xbar` 的 master 端 ID 必然比 slave 端宽 ⌈log₂(NoSlvPorts)⌉ 位，这个「天然 ID 宽度差」是下游常需配 `axi_iw_converter` 的根本原因。
- 转换器之间字段正交（cdc 不改字段、dw 只改数据、iw 只改 ID），可以自由串联，但必须保证**宽度契约**在每段链路上都成立。
- CDC 是时序约束的天然分界：每个转换器要**整体落在单一时钟域**，宽度转换与时钟域跨越互不冲突，次序是位宽/深度的工程权衡。
- `axi_isolate` 应部署在被下电/复位那一侧、**紧贴 CDC 上游**，靠排空计数器（含 ATOP inject）保证 `isolated_o` 拉高时链路真正干净。

## 7. 下一步学习建议

- **验证方法学**：本讲的综合拓扑如何用 `rand_master → DUT → sim_mem + scoreboard` 自检，详见 u16-l1（定向随机验证方法学），可把第 5.5 节的逐步搭建流程升级为带随机种子的回归。
- **更激进的拓扑**：若想让多个 xbar 背靠背级联成 mesh/fat-tree，且两侧 ID 等宽，可学 u15-l3 的 `axi_xp`（它内置 id_remap 压回原宽）；若要做存储 bank 交错，看 `axi_interleaved_xbar`。
- **时序与 EDA 兼容**：本讲反复强调的「内部纯组合、转换器挂边界」如何落到 `LatencyMode`/`FallThrough` 选择与多 EDA 工具约束，详见 u16-l3。
- **贡献与 CI**：当你把自己拼的异构网络模块贡献回上游时，要过的编译/lint/仿真/综合四道关，详见 u16-l4。
