# DPE 总体结构与 AXIS 元数据

## 1. 本讲目标

本讲是 Unit 4「数据面引擎 DPE」的开篇，带你站到 DPE（Data Plane Engine，数据面引擎）的入口处，先看懂它的「骨架」，再弄懂它用什么语言在模块之间说话。

读完本讲，你应当能够：

1. 画出 DPE 的 **多路复用器（mux）→ 流水线（pipeline）→ 解复用器（demux）** 三段式框架，并指出当前 HEAD（9887a3b3）实际跑的是哪一版「骨架」。
2. 读懂 `dpe_if` 这条 128 位 AXI-Stream（AXIS）「多芯电缆」的全部信号，并解释 `tvalid`/`tready` 握手如何完成一次 beat 传输。
3. 把一个数据包的 **元数据**（`TUSER` 的 `bypass_all`/`bypass_stage`/`src`/`dst` 四个字段，以及 `TID` 承载的 peer index）逐比特解码出来。
4. 说清一条关键而容易踩坑的细节：TDATA 总线是小端，Ethernet/IP/UDP 头是大端（网络字节序），而 WireGuard 头又是小端——三者并存于同一条总线上。

本讲只讲「骨架与语言」，不讲具体某一级（解封装、加解密、路由查找）的内部实现，那是 u4-l4 / u4-l5 / Unit 5 的任务。

---

## 2. 前置知识

在进入 DPE 之前，请确认你已经具备以下认知（这些都在前面的讲义里建立过，这里只做一句话回顾）：

- **HW/SW 分区**（u2-l1）：系统分成控制面（软 CPU 跑 WireGuard 协议）和数据面（RTL 线速转发 + 加解密）。DPE 就是那个「数据面」，用户数据全程在它内部闭环，不进 CPU。
- **三个时钟域**（u2-l3）：DPE 跑在绿色域——80 MHz、128 位宽，理论吞吐约 \(\,128 \times 80\text{M} = 10.24\text{ Gbps}\,\)；它通过跨域 FIFO 与 125 MHz@8 位的 MAC（蓝色域）和 80 MHz@32 位的 CPU/CSR（红色域）对接。
- **`dpe_if` 这条电缆**（u2-l2）：在 top.sv 里，`dpe_if` 是数据面的「多芯电缆」，5 进（`from_cpu` + `from_eth_1..4`）5 出（`to_cpu` + `to_eth_1..4`）。本讲会把它逐根信号拆开。
- **`cpu_fifo` 与 FCR**（u3-l3、u3-l4）：CPU 经 `cpu_fifo` 把 128 位 AXIS 拆成 32 位 CSR 收发握手包；改表前用 FCR（`pause`/`idle`）做原子更新。本讲你会看到 FCR 的 `pause`/`idle` 信号是如何接到 DPE 的 mux 上的。

如果你对 **AXI-Stream（AXIS）协议** 本身还陌生，下面这一小段是它最精简的说明：

> AXIS 是 Xilinx 定义的一种「单向数据流」握手协议，专门用来在 IP 核之间搬移变长数据（比如一个网络包）。它最核心的信号是：
> - `TVALID`：发送方声明「我这拍数据有效」。
> - `TREADY`：接收方声明「我准备好收了」。
> - **同一拍 `TVALID==1` 且 `TREADY==1`，这一拍（beat）的数据就成功传过去。**
> - `TDATA`：数据本体；`TKEEP`：字节使能（标记哪些字节有效）；`TLAST`：本拍是整个包的最后一拍。
> - `TUSER`/`TID`：两条「侧带（sideband）」通道，协议不规定它们的含义，留给设计者自己定义——DPE 正是用它们来携带路由指令的。

本讲会反复用到这条「同时为 1 才算数」的握手规则。

---

## 3. 本讲源码地图

本讲涉及的 4 个核心源码文件，加上 3 个用来印证「骨架现状」的辅助文件：

| 文件 | 作用 | 本讲用它讲什么 |
|------|------|----------------|
| [1.hw/ip.dpe/dpe.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv) | DPE 顶层，把 mux / 流水线 / demux 串起来 | DPE 总体框架 + PoC 现状 |
| [1.hw/ip.infra/dpe_if.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/dpe_if.sv) | DPE 数据面接口（128 位 AXIS）的定义 | AXIS 接口信号与 modport |
| [1.hw/ip.infra/dpe_pkg.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/dpe_pkg.sv) | DPE 公共常量包（地址编码） | src/dst 地址编码表 |
| [1.hw/README.md](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/README.md) | 硬件架构与数据流说明 | TUSER/TID 含义、字节序约定 |
| [1.hw/ip.dpe/dpe_dummy_switch.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_dummy_switch.sv) | PoC 阶段的「直通交换」占位 | 当前骨架实际行为 |
| [1.hw/ip.infra/dpe_if_skid_buffer.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/dpe_if_skid_buffer.sv) | 把 `dpe_if` 适配成标准 AXIS 的 skid buffer | TUSER 的物理打包方式 |
| [1.hw/top.filelist](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.filelist) | 综合时实际纳入的源文件清单 | 哪些模块被编译、哪些被注释 |

---

## 4. 核心概念与源码讲解

### 4.1 DPE 总体框架

#### 4.1.1 概念说明

DPE（Data Plane Engine）是整个 SoC 的「转发大脑」。从外部看，它有 **5 个输入端口**（CPU + 4 个以太网口）和 **5 个输出端口**（同样 5 个方向），任何进入数据面的包都要从某个输入进、某个输出出。

它的内部结构是一个经典的 **三段式**：

```
   5个输入              单条主线                 5个输出
 (CPU+4eth) ──► [ 多路复用器 mux ] ──► [ 流水线 pipeline ] ──► [ 解复用器 demux ] ──► (CPU+4eth)
                  把5路合成1路          做解析/查表/加解密        把1路按目的分回5路
```

- **多路复用器（mux）**：5 条输入线轮流（round-robin）把整个包送进单一主线。它同时承担 FCR 暂停（`pause`/`idle`）的入口。
- **流水线（pipeline）**：串接多个处理级——头解析、WG 解封装、解密、IP 路由查找、加密、WG 封装。各级都挂在同一条 128 位 AXIS 主线上，靠 `TUSER`/`TID` 侧带信号传递指令。
- **解复用器（demux）**：与 mux 对称，按包的目的元数据（`tuser_dst`）把包分发到 5 条输出线。

> **一个关键现状提醒（Phase1 PoC）**：当前 HEAD 中，流水线那一整段（IP 查找、WG 解封装/加解密/封装）**源码已经写好，但还没有被综合进去**。实际跑通的「流水线」是一个叫 `dpe_dummy_switch` 的占位模块，它做的是**固定交叉直通**（不做加解密、不查路由表）。本讲会如实标注这一点，让你既理解「目标架构」，又清楚「现在能跑什么」。详见 4.1.3。

#### 4.1.2 核心流程

一个包从输入到输出，在 DPE 里走过的路径：

1. **进入**：包从 `from_cpu` 或 `from_eth_1..4` 之一进入，存在各自的 store-and-forward FIFO 里，整包收齐后被 mux 选中。
2. **复用**：mux 按 CPU → eth1 → eth2 → eth3 → eth4 的顺序轮询，选到一个有完整包的输入后，逐 beat 把整包打到主线上；同时把 `tuser_src` 强制写成「这个包来自哪个物理口」（权威标注）。
3. **流水线处理**：主线上的包流经各级（当前 PoC 只过 `dummy_switch`）。每级可读取/改写 `tuser_dst`、`tid` 等元数据来指导转发。
4. **分用**：demux 读 `tuser_dst`，把包分发到对应的 `to_cpu`/`to_eth_x` 输出 FIFO；若 `tuser_dst` 是广播/组播地址，则同时发到多个输出。
5. **暂停**：当 CPU 要改路由表/密钥表时，先写 FCR.`pause=1`；mux 在送完当前整个包后进入 `IDLE` 并回报 `idle=1`，CPU 才安全改表。

整个过程中，**用户数据从不进入 CPU**——这正是「线速转发」的含义。

#### 4.1.3 源码精读

DPE 顶层 `dpe.sv` 的端口是典型的「5 进 5 出 + CSR」：CSR 口 `from_csr`/`to_csr` 接控制面，其余是 `dpe_if` 类型的数据口。

[dpe.sv:43-58](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv#L43-L58) —— DPE 顶层端口：`from_csr`/`to_csr` 是控制面接口；5 个 `dpe_if.s_axis` 是输入，5 个 `dpe_if.m_axis` 是输出。

骨架由两条内部主线 `muxed_1`、`muxed_2` 串起三个实例：

[dpe.sv:63-92](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv#L63-L92) —— 5 输入汇入 `muxed_1`（mux），再经 `u_dpe_dummy_switch` 到 `muxed_2`，最后由 demux 分到 5 输出。FCR 的 `pause`/`idle` 接在 mux 上。

注意 mux 实例的端口连接，FCR 信号正是从这里进入数据面的：

[dpe.sv:67-76](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv#L67-L76) —— `.pause(from_csr.dpe.fcr.pause.value)`、`.is_idle(to_csr.dpe.fcr.idle.next)`。这正是 u3-l4 讲的 FCR 原子更新握手在数据面的落点。

而那个**真正的 IP 路由查找引擎 `dpe_egress_ip_lookup` 是被注释掉的**：

[dpe.sv:94-103](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv#L94-L103) —— 这段把 `muxed_1 → egress_ip_lookup → muxed_2` 的连接整体注释掉了，所以 `muxed_1` 实际是直接接进 `dpe_dummy_switch`（L79-82），跳过了路由查找。

`top.filelist` 印证了「哪些 DPE 文件真的被综合」：

[top.filelist:69-74](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.filelist#L69-L74) —— 实际编译的是 `dpe.sv`、`dpe_multiplexer.sv`、`dpe_demultiplexer.sv`、`dpe_dummy_switch.sv`；`dpe_wg_disassembler.sv`（WG 解封装）被注释。整个 WG 加解密/封装链同样未上线。

> 结论：当前 bitstream 的 DPE = **mux → dummy_switch（固定直通）→ demux**，外加两个由 CPU 经 CSR 读写、但**数据面尚未使用**的 `tdp_ram`（路由表 `u_routing_table` 8 位地址、密钥表 `u_cryptokey_table` 11 位地址，见 [dpe.sv:105-139](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv#L105-L139)）。这两张表如何被流水线真正查用，留到 u4-l4 / u4-l6 讲。

#### 4.1.4 代码实践

**实践目标**：用纸笔（或文本编辑器）画出当前 HEAD 的 DPE 实际数据通路，并标出每个包必经的模块。

**操作步骤**：

1. 打开 [dpe.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv)，找到三个实例 `u_dpe_multiplexer`、`u_dpe_dummy_switch`、`u_dpe_demultiplexer`。
2. 画出框图：`from_cpu/from_eth_1..4` → `muxed_1` → `dummy_switch` → `muxed_2` → `to_cpu/to_eth_1..4`。
3. 在 `u_dpe_egress`（L94-103）旁边标注「已注释」，在 `u_routing_table`/`u_cryptokey_table` 旁标注「已实例化，但 B 端未接线（数据面尚未查用）」。

**需要观察的现象**：你会清楚地看到「主线」上只有 dummy_switch 一个处理级，没有任何加解密或查表逻辑。

**预期结果**：得到一张与上面 4.1.1 框图一致的图，但「流水线」格子里写的是 `dpe_dummy_switch`，且路由/密钥表挂在主线之外。

> 待本地验证：如果你已能跑仿真（见 u7-l1），可在 `4.sim/rtl/dpe/` 下找到针对 DPE 的测试台，用波形确认 `muxed_1`→`muxed_2` 之间确实只经过 dummy_switch。

#### 4.1.5 小练习与答案

**练习 1**：当前 HEAD 的 DPE 主线上，一个用户数据包会经过加解密吗？为什么？

> **答案**：不会。主线上唯一的处理级是 `dpe_dummy_switch`，它只做固定交叉直通（不改载荷）。WG 解封装/加解密/封装的源码虽存在，但 `dpe.sv` 中 `u_egress` 被注释、`top.filelist` 中 `dpe_wg_disassembler.sv` 也被注释，所以这些级没有进入综合。

**练习 2**：FCR 的 `pause`/`idle` 接在哪个模块上？这和 u3-l4 讲的「在包边界暂停」有什么关系？

> **答案**：接在 `u_dpe_multiplexer` 上（[dpe.sv:67-76](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv#L67-L76)）。因为 mux 是整包送出的入口，在它这里暂停就能保证「当前包送完才停」，避免在包中间 stall 撕裂在飞包——这正是 u3-l4 强调的「不能用 AXIS 的 TREADY stall 改表」。

---

### 4.2 AXIS 接口

#### 4.2.1 概念说明

DPE 内部所有模块之间，都用同一种语言对话——`dpe_if`。你可以把它理解成一根「16 芯的扁平电缆」：每根芯是一个信号，两端用 modport（接口方向）规定谁发谁收。它本质上是 AXI-Stream（AXIS）协议的一个项目专用变体，只是把标准的 `TUSER`/`TID` 侧带信号拆成了几个有名字的字段，便于阅读。

为什么选 AXIS 而不是 AXI 内存映射总线？因为 DPE 搬的是「变长的网络包流」，不是「随机地址的寄存器读写」。AXIS 的「握手即过、不寻址」特性，正好适合把多个处理级像水管一样串起来——每级只管「收一拍、处理、吐一拍」，天然支持流水线化。

#### 4.2.2 核心流程

一次 beat（单拍数据）的成功传输，靠 `tvalid`/`tready` 这对握手信号：

```
        拍0    拍1    拍2    拍3
tvalid:  1      1      1      1
tready:  0      1      1      0     ← 接收方拍0、拍3没准备好
传输?     否     是     是     否
```

- **规则**：只有 `tvalid==1 && tready==1` 的那一拍，`tdata`（以及 `tkeep`/`tlast`/`tuser*`/`tid`）才被接收方取走。
- **整包边界**：`tlast==1` 标记这是整个包的最后一拍。mux 的轮询和 FCR 的暂停都以 `tlast` 为边界。
- **位宽**：`tdata` 是 128 位（16 字节），所以 `tkeep` 是 16 位（每字节 1 个有效位），对应绿色域的 128 位主线。
- **背压（backpressure）**：当 `tready==0` 时，发送方必须把数据「按住」不动。DPE 在每个接口边界插入了 skid buffer（缓冲寄存器）来吸收这种瞬时反压，避免组合逻辑回环。

吞吐核算：128 位 × 80 MHz = 10 240 Mbps ≈ **10 Gbps**，这正是 README 说的「DPE 绿色域约 10 Gbps」的来源；它远大于 4 × 1 Gbps = 4 Gbps 的线速下限，留出了充足余量。

#### 4.2.3 源码精读

[dpe_if.sv:43-58](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/dpe_if.sv#L43-L58) 定义了电缆里的全部信号：

| 信号 | 位宽 | 含义 |
|------|------|------|
| `tready` | 1 | 接收方准备好（接收方驱动） |
| `tvalid` | 1 | 本拍数据有效（发送方驱动） |
| `tdata` | 128 | 数据本体（16 字节，小端，见 4.4） |
| `tlast` | 1 | 本拍是包的最后一拍 |
| `tkeep` | 16 | 字节使能（每字节 1 位，标记哪些字节有效） |
| `tuser_bypass_all` | 1 | 侧带：整条流水线旁路 |
| `tuser_bypass_stage` | 1 | 侧带：跳过下一级 |
| `tuser_src` | 3 | 侧带：内部源地址 |
| `tuser_dst` | 3 | 侧带：内部目的地址 |
| `tid` | 8 | 侧带：peer index |

[dpe_if.sv:60-88](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/dpe_if.sv#L60-L88) 用两个 modport 把方向定死且互补：
- `m_axis`（master/发送方）：`tready` 是 `input`（听对方的），其余 `tvalid/tdata/...` 都是 `output`（自己驱动）。
- `s_axis`（slave/接收方）：`tready` 是 `output`（自己驱动），其余都是 `input`（听对方的）。

这就保证了一根电缆两端必然是一 `m_axis` 对一 `s_axis`，方向接反了综合期就会报错。

每个模块在输入/输出边界都插了一个 skid buffer，它把 `dpe_if` 的命名字段重新打包成外部 `verilog-axis` 库（`axis_register`）认的标准 AXIS 信号：

[dpe_if_skid_buffer.sv:47-76](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/dpe_if_skid_buffer.sv#L47-L76) —— `DATA_WIDTH=128`、`USER_WIDTH=8`、`ID_WIDTH=8`。它说明：项目里 `TUSER` 物理上是 **8 位**，`TID` 也是 **8 位**。这条 skid buffer 是 DPE 接口与「外部库标准 AXIS」之间的适配层。

#### 4.2.4 代码实践

**实践目标**：用源码确认 `tvalid/tready` 握手在 mux 中是如何驱动和采样的。

**操作步骤**：

1. 打开 [dpe_multiplexer.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_multiplexer.sv)。
2. 在 `R0,S0` 分支（[L175-186](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_multiplexer.sv#L175-L186)）找到三行：
   - `to_dpe_sbuff.tvalid = from_cpu.tvalid;`（把 CPU 的有效信号透传到主线）
   - `from_cpu.tready = to_dpe_sbuff.tready;`（把主线的就绪信号回灌给 CPU）
   - 这两行合起来就是「CPU 与主线之间的握手透传」。

**需要观察的现象**：注意 `tvalid` 和 `tready` 是**反向**连接的——`from_cpu` 是 `s_axis`（tready 输出、tvalid 输入），`to_dpe_sbuff` 也是 `s_axis`，所以 mux 在两者之间充当「中继」。

**预期结果**：你会看到 mux 不发明数据，只是把当前轮询到的输入口的 `tvalid/tdata/...` 原样搬上主线，并把主线的 `tready` 原样回灌，握手得以贯通。

#### 4.2.5 小练习与答案

**练习 1**：如果主线下游的某个模块把 `tready` 拉成 0 持续 5 拍，上游会发生什么？

> **答案**：由于 mux 把 `from_cpu.tready = to_dpe_sbuff.tready`，下游 `tready=0` 会一路反压回输入 FIFO，上游的 `tvalid` 和 `tdata` 必须保持不变直到 `tready` 重新为 1。这就是 AXIS 的「背压」机制；边界上的 skid buffer 用来吸收这种停顿，避免组合逻辑形成长回环。

**练习 2**：`tkeep` 为什么是 16 位而不是 128 位？

> **答案**：因为 `tkeep` 是「字节使能」，每字节 1 位。128 位 = 16 字节，所以 `tkeep` 是 16 位。它标记当前 beat 里哪些字节是有效数据（包尾不满 16 字节时，只有部分位为 1）。

---

### 4.3 TUSER/TID 元数据

#### 4.3.1 概念说明

光搬数据还不够——DPE 还得知道「这个包从哪来、要到哪去、要不要跳过某些处理级、属于哪个 WireGuard peer」。这些「指挥指令」如果不单独走线，就只能塞进数据包里，那样每个处理级都要解析包头，既慢又乱。

AXIS 协议为此预留了两条**侧带（sideband）通道**：`TUSER` 和 `TID`，协议本身不规定它们的含义。DPE 把它们用作「路由标签」，让指令随包一起流动，每一级都能零成本读取：

- **`TUSER`（项目拆成 4 个字段）**：
  - `bypass_all`：整条 DPE 流水线旁路，包直达 ETH/CPU（反之亦然）。
  - `bypass_stage`：跳过下一级（DPE 内部逐级使用）。
  - `src`：内部源地址（包从哪个口进来）。
  - `dst`：内部目的地址（包要送到哪个口）。
- **`TID`**：承载 peer index（peer 表查找的结果），用于选择加解密密钥。

注意一个设计要点：**`src` 是「权威」字段，由 mux 根据物理输入口强制写入**，不由输入包自己声明；而 **`dst` 是「建议」字段，可被流水线各级改写**（比如 IP 查找级根据目的 IP 决定真正的 `dst`）。

#### 4.3.2 核心流程

`src`/`dst` 都用 3 位编码，取值由 `dpe_pkg` 统一定义：

| 编码 | 常量名 | 含义 |
|------|--------|------|
| 0 | `DPE_ADDR_CPU` | CPU |
| 1 | `DPE_ADDR_ETH_1` | 以太网口 1 |
| 2 | `DPE_ADDR_ETH_2` | 以太网口 2 |
| 3 | `DPE_ADDR_ETH_3` | 以太网口 3 |
| 4 | `DPE_ADDR_ETH_4` | 以太网口 4 |
| 5 | `DPE_ADDR_MCAST_13` | 组播：eth1 + eth3 |
| 6 | `DPE_ADDR_MCAST_24` | 组播：eth2 + eth4 |
| 7 | `DPE_ADDR_BCAST` | 广播：CPU + 全部 eth |

一个包的 `TUSER` 物理上被打包成 8 位（见 4.2.3 的 skid buffer），位布局为：

```
位:  [7]      [6]          [5:3]    [2:0]
     bypass_all bypass_stage src      dst
```

即 `TUSER = {bypass_all, bypass_stage, src[2:0], dst[2:0]}`。

元数据的生命周期（随包逐 beat 流动，整包一致）：

1. **mux 赋 `src`**：包被某个输入口选中时，mux 把 `src` 写成该口的编码（CPU 的包 `src=0`，eth3 的包 `src=3`）。`dst` 暂时沿用输入包自带的值。
2. **流水线各级读/改 `dst`、`tid`**：IP 查找级根据目的 IP 查路由表，把命中条目的「目的接口」写进 `dst`、把「peer ID」写进 `tid`；后续加密级用 `tid` 选密钥。
3. **demux 读 `dst` 分发**：包到 demux 时，它只看 `dst`——是 0 就送 CPU，是 3 就送 eth3，是 7 就广播。
4. **`bypass_all`/`bypass_stage` 控制短路**：若 `bypass_all=1`，包跳过整条流水线，`dst` 不被改写、原样送达；`bypass_stage` 则在单级内部判断「这次要不要跳过我」。

#### 4.3.3 源码精读

[dpe_pkg.sv:45-54](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/dpe_pkg.sv#L45-L54) —— 8 个地址常量的唯一真源，整个 DPE 用 `import dpe_pkg::*;` 共享它们，避免「幻数（magic number）」散落。

README 对 `TUSER`/`TID` 的官方定义：

[README.md:44-49](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/README.md#L44-L49) —— `TUSER[7]=bypass_all`、`[6]=bypass_stage`、`[5:3]=src`、`[2:0]=dst`；`TID[7:0]` 承载 peer index。

mux 强制写 `src` 的证据（以 CPU 输入为例）：

[dpe_multiplexer.sv:177-185](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_multiplexer.sv#L177-L185) —— 在 `R0,S0` 分支里，`to_dpe_sbuff.tuser_src = DPE_ADDR_CPU;`，即来自 CPU 的包 `src` 被硬写成 0，而 `dst` 沿用 `from_cpu.tuser_dst`。其余 4 个 eth 分支同理分别写 1/2/3/4。

`TUSER` 的物理打包方式，由 skid buffer 印证：

[dpe_if_skid_buffer.sv:59-62](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/dpe_if_skid_buffer.sv#L59-L62) —— `.s_axis_tuser({inp.tuser_bypass_all, inp.tuser_bypass_stage, inp.tuser_src, inp.tuser_dst})`，拼接顺序与 README 的位布局完全吻合（`{bypass_all[7], bypass_stage[6], src[5:3], dst[2:0]}`）。

demux 如何按 `dst` 分发（以 CPU 输出为例）：

[dpe_demultiplexer.sv:69-87](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_demultiplexer.sv#L69-L87) —— 当 `tuser_dst == DPE_ADDR_CPU` 或 `DPE_ADDR_BCAST` 时，包被送到 `to_cpu`；否则该输出保持 `tvalid=0`。组播/广播的逻辑同理，见 [L88-163](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_demultiplexer.sv#L88-L163)。

`bypass_all`/`bypass_stage` 的实际效果，看 dummy_switch：

[dpe_dummy_switch.sv:84-116](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_dummy_switch.sv#L84-L116) —— 若两个 bypass 位都为 0，则按 `src` 查一张「固定交叉表」决定 `dst`（CPU→eth1、eth1→CPU、eth2→CPU、eth3→eth4、eth4→eth3）；否则 `dst` 原样透传。这就是「bypass 让指令原样穿透处理级」的实例。

#### 4.3.4 代码实践

**实践目标**：给定一个 `TUSER` 字节值，解码出它的 `bypass_all`/`bypass_stage`/`src`/`dst` 四个字段；再反向构造一个指定场景的 `TUSER` 字节，并对照源码验证。

**操作步骤**：

1. **解码**。给定 `TUSER = 0b0_0_011_100`（即 8'h1C = 十进制 28）：
   - `bypass_all` = bit[7] = `0`
   - `bypass_stage` = bit[6] = `0`
   - `src` = bit[5:3] = `011` = 3 → 查 `dpe_pkg` 得 `DPE_ADDR_ETH_3`（来自 eth3）
   - `dst` = bit[2:0] = `100` = 4 → `DPE_ADDR_ETH_4`（送到 eth4）
   - 结论：这是一个从 eth3 进、要送到 eth4 的包，不旁路。
2. **构造**。请构造一个「从 CPU 发出、要广播给所有口」的包的 `TUSER`：
   - `bypass_all=0`、`bypass_stage=0`、`src=0`(CPU)、`dst=7`(BCAST)
   - `TUSER = {0, 0, 000, 111} = 8'b0_0_000_111 = 8'h07`
3. **验证**。打开 [dpe_dummy_switch.sv:93-112](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_dummy_switch.sv#L93-L112)，对照练习 1 的包（`src=3`）：dummy_switch 会把它送到 `dst=4`（eth4），与你解码出的 `dst` 一致。

**需要观察的现象**：当 `src=3` 时，dummy_switch 的 `case` 命中 `DPE_ADDR_ETH_3` 分支，输出 `DPE_ADDR_ETH_4`，与你手算的 `dst=4` 吻合。

**预期结果**：你能不查表地把任意 `TUSER` 字节拆成四字段，也能把任意 (bypass, src, dst) 组合拼成一个字节，并与 `dpe_pkg` 常量、dummy_switch 的 `case` 表对得上。

#### 4.3.5 小练习与答案

**练习 1**：一个包的 `TUSER = 0x84`（8'b1000_0100）。请解码并判断它在 dummy_switch 里会怎么走。

> **答案**：`0x84 = 8'b1_0_000_100`。`bypass_all=1`、`bypass_stage=0`、`src=0`(CPU)、`dst=4`(eth4)。因为 `bypass_all=1`，dummy_switch 走 else 分支（[L113-115](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_dummy_switch.sv#L113-L115)），**不查固定交叉表**，`dst` 原样保持为 4，即直送 eth4。

**练习 2**：为什么 `src` 由 mux 写死，而不是让输入包自己声明？

> **答案**：因为「包从哪个物理口进来」是 mux 唯一能权威判定的事实——输入 FIFO 自己未必可信，且软件构造的 CPU 包也可能填错。mux 根据轮询到的物理通道强制写 `src`，下游所有级就能可靠地知道来源（例如 dummy_switch 用来查固定交叉表、路由查找用来做反向学习等）。这把「易错的声明」变成「硬件的观测」。

**练习 3**：`TID` 在当前 dummy_switch 版本里被用到了吗？

> **答案**：没有。`TID`（peer index）是给 IP 查找级填、给加密级选密钥用的（README L49）。当前 HEAD 没有这些级上线，所以 `TID` 虽在接口里定义了（[dpe_if.sv:57](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/dpe_if.sv#L57)），但在 dummy_switch 里只是原样透传，没有消费。它会在 u4-l4 / Unit 5 真正派上用场。

---

### 4.4 字节序约定

#### 4.4.1 概念说明

「字节序（endianness）」回答一个问题：一个多字节的值，在内存/总线里，**最低位字节放在最小的地址（小端）还是最大的地址（大端）**。网络世界里两种字节序长期共存，DPE 把它们压在同一条 128 位总线上，于是出现了「三套字节序并存」的微妙局面，这是头解析模块最容易出 bug 的地方。

DPE 里有三种字节序：

1. **TDATA 总线本身是小端（little-endian）**：在 128 位（16 字节）的一拍里，`tdata[7:0]` 是第 0 字节（最先到达/最低地址），`tdata[127:120]` 是第 15 字节。
2. **Ethernet / IP / UDP 头是大端（big-endian，即网络字节序）**：这些标准协议规定的字段（如目的 MAC、IP 地址、端口号）按「高位字节在前」排布。于是它们落在 TDATA 上时，字段的高位字节在更小的字节位置。
3. **WireGuard 头是小端（little-endian）**：WG 协议自己规定的字段排布，恰好与 TDATA 总线的小端一致，读起来更「顺」。

#### 4.4.2 核心流程

一个包在 128 位主线上的字节排布（以第 0 拍 `tdata[127:0]` 为例）：

```
字节位置(小端总线):  [0][1][2][3]...[15]
tdata 位段:        [7:0][15:8]...[127:120]
                     ↑
                  第0字节 = 最低地址 = 最先到达

Ethernet头(大端字段): 字段的高位字节放在更小的字节位置
   例: 目的MAC[47:40] 放在字节0, 目的MAC[7:0] 放在字节5
   例: IPv4目的地址, 高字节在前

WireGuard头(小端字段): 字段的低位字节放在更小的字节位置(与总线一致)
   例: WG的receiver_index 等字段, 低字节在前
```

处理级的「头解析器（Header Parser）」要做的就是：

1. 从 TDATA 的特定字节位置（小端地址）里，按大端规则把 Ethernet/IP/UDP 字段**重组**成数值（例如把 4 个字节拼成 32 位目的 IP 时，要反一反字节顺序）。
2. 对 WireGuard 头则按小端规则读（与总线一致，直接拼）。

数学上，对一个 4 字节大端字段 \(b_0 b_1 b_2 b_3\)（\(b_0\) 在最低字节位置），其数值为：

\[
\text{value} = b_0 \cdot 2^{24} + b_1 \cdot 2^{16} + b_2 \cdot 2^{8} + b_3
\]

而小端字段 \(b_0 b_1 b_2 b_3\)（\(b_0\) 同样在最低字节位置）的数值为：

\[
\text{value} = b_3 \cdot 2^{24} + b_2 \cdot 2^{16} + b_1 \cdot 2^{8} + b_0
\]

两者字节顺序正好相反，这正是 `header_parser` 等模块在做字节段拼接时必须小心的地方。

#### 4.4.3 源码精读

README 对字节序的权威说明：

[README.md:43](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/README.md#L43) —— 原文：数据传输按小端组织，但 Ethernet/IP/UDP 头的内部字段遵循大端（网络字节序）；WireGuard 头的字段则是小端。这条一句话是整个 DPE 字节序设计的总纲。

`tkeep` 的 16 位宽度印证了「128 位 = 16 字节、每字节一使能」的小端字节粒度：

[dpe_if.sv:52](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/dpe_if.sv#L52) —— `logic [15:0] tkeep;`，bit i 对应 `tdata` 的第 i 字节。

> 说明：当前 HEAD 上线的是 `dpe_dummy_switch`，它只做字节直通、不解析任何头字段，所以你在它身上看不到字节序处理代码。字节序的真正影响出现在 `header_parser`、`dpe_wg_disassembler` 等尚未上线的模块里——它们读 IP/WG 头时必须分别套用大端/小端规则。这里先建立概念，等 u4-l5 读到那些模块时你会再次遇到它。

#### 4.4.4 代码实践

**实践目标**：用一次手算，体会「同一个 4 字节序列，按大端和小端读出来是两个不同的数值」，从而理解头解析器为什么要区分对待。

**操作步骤**：

1. 假设以太网某帧的目的 IPv4 地址 4 字节，在 TDATA 第 0 拍的字节位置 16..19（紧接在以太网头之后）依次为 `0x0A 0x09 0x00 0x01`。
2. IP 头是大端（网络字节序），按上面的大端公式计算：
   \[
   \text{IP} = 0x0A\cdot2^{24} + 0x09\cdot2^{16} + 0x00\cdot2^{8} + 0x01 = \texttt{10.9.0.1}
   \]
   即 README 拓扑里的「WireGuard peer A」（[README.md:109](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/README.md#L109)）。
3. 现在假设这同样的 4 字节是一个 **WireGuard 头字段**（小端），重算：
   \[
   \text{value} = 0x01\cdot2^{24} + 0x00\cdot2^{16} + 0x09\cdot2^{8} + 0x0A = \texttt{0x0100090A}
   \]
   两者数值完全不同。

**需要观察的现象**：完全相同的 4 字节序列，只因协议规定的字节序不同，解读出的数值就差了字节顺序。

**预期结果**：你会直观地理解——为什么头解析器对 IP 字段和 WG 字段必须用不同的拼接逻辑。写错字节序，查表就会查到完全错误的目的，包就飞了。

> 待本地验证：若你读 u4-l5 的 `dpe_wg_disassembler` 源码，可对照确认它提取 WG 字段时用的是小端拼接。

#### 4.4.5 小练习与答案

**练习 1**：TDATA 总线是小端的，WireGuard 头也是小端的，这两者「方向一致」意味着什么？

> **答案**：意味着 WG 字段的低位字节天然落在 TDATA 的低位字节位置，头解析模块可以直接把连续字节按「低字节在低位」拼成数值，无需反转，读起来更直观、更不易错。

**练习 2**：为什么 Ethernet/IP/UDP 用大端（网络字节序）？

> **答案**：这是互联网协议的历史约定（RFC 规定网络字节序为大端），目的是让不同主机、不同 CPU 架构（有的本机小端、有的大端）在网上传输时有一个统一的无歧义格式。DPE 必须遵守这个外部约定，所以即便内部总线是小端，这些头字段在总线上仍以大端排布，解析时要手动调整字节顺序。

---

## 5. 综合实践

**任务**：跟踪一个「ICMP Echo Request 用户包」在当前 HEAD 的 DPE 里，从 eth2 进入、最终从 CPU 侧被软件桥接取走的全过程，重点写出每一拍它携带的 `TUSER` 元数据是如何变化的。这是把本讲四个最小模块串起来的综合训练。

**背景**：参考 README 的 55 步示例（[README.md:168-190](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/README.md#L168-L190)），但请注意——那是**目标架构**的描述（含加解密）；当前 PoC 实际跑的是 dummy_switch 直通。本任务按**当前实际**来跟踪。

**操作步骤**：

1. **进入 mux**：包从 `from_eth_2` 进入并整包存入 Rx FIFO。
   - mux 在 `R2` 轮到 eth2，逐 beat 把包打到主线。
   - mux 写元数据：`src = DPE_ADDR_ETH_2 = 2`（[dpe_multiplexer.sv:209](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_multiplexer.sv#L209)）；`dst` 沿用输入包自带值（假设为某个初值）；`bypass_all=0`、`bypass_stage=0`。
   - 此时的 `TUSER = {0, 0, 010, dst}`。
2. **经过 dummy_switch**：因 `bypass` 均为 0，走固定交叉表，`src=2`（eth2）→ `dst = DPE_ADDR_CPU = 0`（[dpe_dummy_switch.sv:100-102](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_dummy_switch.sv#L100-L102)）。
   - 此时的 `TUSER = {0, 0, 010, 000} = 8'h08`。
3. **到达 demux**：demux 读 `dst=0`（`DPE_ADDR_CPU`），把包送到 `to_cpu`（[dpe_demultiplexer.sv:69-73](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_demultiplexer.sv#L69-L73)），整包存入 CPU 侧 Tx FIFO。
4. **CPU 取走**：CPU 经 `cpu_fifo` 的 CSR 接口逐 beat 把包读进 RAM（u3-l3 的 10 步流程），交给软件做后续处理（PoC 阶段由软件桥接完成明文转发）。

**需要观察的现象**：整个过程中，元数据经历了 `src` 由 mux 写入、`dst` 由 dummy_switch 改写、最终被 demux 消费这三个阶段；`tid` 全程未被使用（因为没有加解密级上线）。

**预期结果**：你能画出一张表，列出该包在「mux 输入 / mux 输出 / dummy_switch 输出 / demux 输入」四个观测点上 `tuser_src`、`tuser_dst`、`bypass_all`、`bypass_stage`、`tid` 的取值，并解释每个变化是由哪个模块、依据什么规则造成的。这张表就是本讲全部要点的「活体总结」。

> 待本地验证：若能跑 `4.sim/rtl/dpe/` 的测试台，可在 `muxed_1`、`muxed_2` 两条内部主线上抓波形，核对 `tuser_*` 的取值与你手算的表一致。

---

## 6. 本讲小结

- **DPE 是三段式骨架**：5 输入 → 多路复用器（mux）→ 流水线 → 解复用器（demux）→ 5 输出；当前 HEAD 的「流水线」是占位的 `dpe_dummy_switch`（固定直通），真正的 IP 查找/WG 加解密链源码已写好但被注释，未进综合（Phase1 PoC）。
- **`dpe_if` 是 128 位小端 AXIS 变体**：靠 `tvalid`/`tready` 同拍为 1 完成握手；`tdata` 128 位、`tkeep` 16 位（字节使能）、`tlast` 标包尾；`m_axis`/`s_axis` 两个 modport 把收发方向定死。
- **`TUSER`/`TID` 是侧带路由标签**：`TUSER` 拆成 `bypass_all`/`bypass_stage`/`src`/`dst` 四字段，物理打包为 8 位 `{bypass_all, bypass_stage, src[2:0], dst[2:0]}`；`src/dst` 用 `dpe_pkg` 的 8 个常量编码（0=CPU、1-4=eth、5/6=组播、7=广播）；`TID` 承载 peer index。
- **`src` 是权威字段（mux 按物理口写死），`dst` 是建议字段（流水线各级可改写）**：demux 最终只按 `dst` 分发；`bypass_all` 让指令原样穿透处理级。
- **三种字节序并存**：TDATA 总线小端、Ethernet/IP/UDP 头大端（网络字节序）、WireGuard 头小端；头解析时必须按协议分别套用，否则字段重组出错。
- **FCR 暂停的落点在 mux**：`pause`/`idle` 接在 mux 上，保证「整包边界暂停」，这是 u3-l4 原子更新握手在数据面的物理入口。

---

## 7. 下一步学习建议

本讲建立了 DPE 的「骨架与语言」，后续讲义会逐级填充血肉：

1. **u4-l2 轮询多路复用器与暂停流控**：精读 `dpe_multiplexer` 的 42 状态 FSM（`IDLE`/`R0..R4`/`S0..S4`），看清 per-packet 轮询和 `pause`→`IDLE`→`idle` 的状态机细节。
2. **u4-l3 解复用器**：精读 `dpe_demultiplexer` 的广播/组播分发逻辑与背压「与」运算。
3. **u4-l4 TCAM 最长前缀路由查找**：进入当前被注释的 `dpe_egress_ip_lookup`/`dpe_route_mem`，看 `dst`/`tid` 是如何被路由查找结果填上的——你会回头理解本讲的 `TID` 到底怎么用。
4. **u4-l5 WireGuard 封装/解封装与加解密数据流**：进入同样被注释的 `dpe_wg_disassembler`/`encryptor`/`decryptor`，体会本讲讲的「字节序差异」在真实头解析中的体现。
5. **继续阅读源码的建议顺序**：先 `dpe_multiplexer.sv`、`dpe_demultiplexer.sv`（已上线，可结合仿真验证），再 `dpe_route_mem.sv`、`dpe_egress_ip_lookup.sv`，最后 `dpe_wg_*.sv`——由「已生效」到「待上线」，逐步把目标架构看全。
