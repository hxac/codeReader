# mem_model 稀疏内存、PCAP 回放与逐模块测试台

## 1. 本讲目标

本讲是 Unit 7「仿真协同验证」的收尾篇。前面四讲（u7-l1～u7-l4）已经搭好了测试台骨架、VProc 虚拟处理器、rv32 ISS 和以太网 VIP。本讲把这套体系最后三块「胶水」补齐，让你能真正跑通端到端验证，并学会在 RTL 模块级做独立测试。

学完本讲你应该能够：

1. 说清 `mem_model` 稀疏内存在仿真里扮演的「跨域共享黑板」角色，以及它为什么用「按需分页」而不是一整块 BRAM。
2. 读懂 `VUserMainPcap` 这条端到端验证路径：用 PCAP 回放发送、用回调录制接收，并把 TX 与 RX 的时间戳对齐，从而测出真实的「开始到开始」延迟。
3. 看懂 `4.sim/rtl/` 下逐模块测试台的组织方式，区分「定向激励型」与「PCAP 驱动型」两种风格，并能独立编译运行其中一个。

## 2. 前置知识

本讲默认你已经学完 u7-l1～u7-l4，熟悉以下概念（不再重复展开）：

- **协同仿真（co-simulation）**：用户 C++ 程序经 DPI-C 与 Verilator 里的 HDL lock-step 推进，详见 u7-l1/u7-l2。
- **VProc 虚拟处理器**：`write`/`read`/`tick` 三件套，每个 node 有一个 `VUserMain<n>` 入口，详见 u7-l2。
- **udpIpPg 以太网 VIP**：`UdpVpSendRawEthFrame` 发帧、`registerUsrRxCbFunc` 注册接收回调、`UdpVpSendIdle` 发空闲符号保活，详见 u7-l4。
- **AXIS / dpe_if**：128 位数据面接口、`tvalid`/`tready` 握手、`tlast` 标包尾，详见 u4-l1。

补充两个本讲要用到的小概念：

- **稀疏内存（sparse memory）**：不预先分配整个地址空间，而是访问到某个地址时才为它所在的「页」分配存储。好处是 4 GB（甚至更大）地址空间只占实际用到的几 KB。
- **PCAP 文件**：libpcap/Wireshark 通用的抓包格式。文件头里有个魔数（magic number）标明字节序与时间精度（微秒或纳秒），后面跟一条条 `{时间戳, 长度, 帧字节}` 记录。本讲里它既是仿真输入，也是仿真输出，因而能和 Wireshark、真实网卡抓包互相对照。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [4.sim/models/cosim/README.md](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/cosim/README.md) | VProc + mem_model 协同仿真组件的总说明，讲清「同一块内存被多个 VProc 与 HDL 共享」这一核心思想。 |
| [4.sim/models/cosim/mem_model.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/cosim/mem_model.sv) | mem_model 的 SystemVerilog 顶层壳，定义 `MEM_MODEL_SV` 后 include 真正的实现。 |
| [4.sim/models/cosim/f_mem_model.v](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/cosim/f_mem_model.v) | mem_model 的 Verilog 实现，提供寄存器/突发读/突发写/SRAM 写四种端口，全部经 DPI 落到同一个 C 稀疏模型。 |
| [4.sim/usercode/VUserMainPcap.cpp](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/usercode/VUserMainPcap.cpp) | PCAP 回放/录制的 4 个 node 入口：node1/3 回放、node2/4 录制。 |
| [4.sim/usercode/PcapReplay.h](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/usercode/PcapReplay.h) | 回放/录制引擎：读 PCAP→发帧→打 TX 时间戳；收帧回调→算 RX 时间戳→写 PCAP。 |
| [4.sim/usercode/PcapIO.h](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/usercode/PcapIO.h) | 极简 PCAP 读写器，自动识别 4 种魔数（大小端 × 微秒/纳秒）。 |
| [4.sim/tools/gen_udp_pcap.py](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/tools/gen_udp_pcap.py) | 离线生成测试用 UDP/IPv4 PCAP 的脚本，带正确 IP/UDP 校验和。 |
| [4.sim/rtl/dpe/tb.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/rtl/dpe/tb.sv) | DPE 顶层定向激励测试台：5 路并行喂包、监测出口。 |
| [4.sim/rtl/dpe_wg_encryptor/tb.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/rtl/dpe_wg_encryptor/tb.sv) | WG 加密器 PCAP 驱动测试台：读明文 PCAP→加密→写密文 PCAP。 |
| [4.sim/rtl/README.md](https://github.com/chili-chips-ba-wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/rtl/README.md) | 逐模块测试台目录的一句话说明。 |
| [4.sim/rtl/dpe/Makefile](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/rtl/dpe/Makefile) | 逐模块测试台的标准 Verilator Makefile 模板。 |

---

## 4. 核心概念与源码讲解

### 4.1 mem_model 稀疏内存：仿真里的跨域共享黑板

#### 4.1.1 概念说明

u7-l2～u7-l4 里反复出现一个需求：「C++ 用户代码」和「HDL 测试台逻辑」要交换一块共同的数据。例如：

- `soc_cpu.VPROC` 跑的 rv32 ISS 把 RISC-V 程序映像放进 IMEM；
- `bfm_phy_mdio` 把 MDIO 寄存器映射成一段内存，让 C++ 能查、HDL 能响应；
- 以太网 VIP 想把预先生成好的帧缓冲交给 HDL。

如果每次交换都要手写一组 DPI 函数或一组 FIFO，代码会很碎。`mem_model` 的做法是**提供一块「共享黑板」**：一块用 C 实现的稀疏内存，C++ 端用 API 读写，HDL 端用 `mem_model` 组件读写，而**所有实例背后访问的是同一块存储**。于是「跨域传数据」就退化成「往同一个地址写、从同一个地址读」。

为什么是「稀疏」？因为完整的 4 GB（乃至更大）地址空间如果一次性分配，仿真会爆内存；而仿真实际只用得到其中零星几段（IMEM、DMEM、MDIO 寄存器）。稀疏模型**按需分页**：只有访问到的地址才为它所在的页分配物理存储，访问没到的地址永远不占内存。

#### 4.1.2 核心流程

`mem_model` 在两个世界里各有一副面孔，但指向同一个 C 模型：

```text
          ┌──────────────── C++ 用户代码 (VProc node) ────────────────┐
          │  WriteRamWord(addr, data, le, node=0)                      │
          │  ReadRamWord (addr,    le, node=0)  ──┐                    │
          └──────────────────────────────────────┼────────────────────┘
                                                 │  直接调用 C 函数
                                                 ▼
          ┌──────────────── HDL 测试台 ──────────┐      ┌──────────────────┐
          │ mem_model 组件 (可例化任意多个)        │ ───▶ │  C 稀疏内存模型    │
          │   - 寄存器从口: address/write/read     │ DPI  │  按需分页、单一副本 │
          │   - 突发读从口: rx_address/rx_read     │ ───▶ │  全实例共享        │
          │   - 突发写从口: tx_address/tx_write    │      └──────────────────┘
          │   - SRAM 写口 : wr_port_*              │            ▲
          └──────────────────────────────────────┘            │
               同一个 HDL 模块可被 soc_cpu、bfm_phy_mdio ┘  全部访问同一空间
```

要点有三：

1. **C++ 侧直通**：用户代码调 `WriteRamWord`/`ReadRamWord` 等 API，不经仿真时间推进、不经 HDL 总线，直接读写那块 C 内存。
2. **HDL 侧经 DPI**：`mem_model` 组件把 Avalon 风格的端口翻译成对同一个 C 模型的调用（`MemRead`/`MemWrite`），这一步才占用仿真时钟节拍。
3. **多实例单副本**：HDL 里例化多少个 `mem_model` 都行，它们全部、连同所有 VProc node 的 API 调用，看到的是**同一份**存储。这就是「共享黑板」。

一个容易踩的命名坑：API 里的 `node` 参数**不是** VProc 的 node 号，而是「地址空间编号」，本项目里恒为 0（即唯一的共享空间）。绝大多数情况下它和 VProc node 恰好相等，但语义上是两回事。

#### 4.1.3 源码精读

先看总说明如何定性这块组件——它是把多个 VProc 与测试台逻辑「紧紧绑在一起」的同一块内存：

[4.sim/models/cosim/README.md:10](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/cosim/README.md#L10) 指出：运行程序的内存能访问 mem_model 的空间，HDL 逻辑也能用一个 HDL 组件访问同一地址空间，以太网 VIP 上跑的代码也共用它——「这些组件因此被紧密地绑在一起」。

稀疏与按需分页的特性，见主 README 的 mem_model 专节：

[4.sim/README.md:418](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/README.md#L418) 说明它是「按需分页以限制实际所需内存」的 C 稀疏模型，并能被任意多个 `mem_model` HDL 组件与所有 VProc 共享同一空间。

C++ 侧的 API 长这样（节选），注意每个函数都要显式传 `node`（本项目恒为 0）和大小端标志 `little_endian`：

[4.sim/README.md:150-153](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/README.md#L150-L153) 给出 `WriteRamWord`/`ReadRamWord` 等函数原型，是 C 接口、无默认参数。

HDL 侧的组件端口则是 Avalon 风格的四套口。`mem_model.sv` 只是个薄壳，靠 `MEM_MODEL_SV` 宏切到 DPI-C 版本，然后 include 真正的实现：

[4.sim/models/cosim/mem_model.sv:31-39](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/cosim/mem_model.sv#L31-L39) 定义 `MEM_MODEL_SV` 后 include `f_mem_model.v`，从而选 DPI 而非 PLI。

真正的实现在 `f_mem_model.v`，开头就把「稀疏 + 动态分页」讲明了：

[4.sim/models/cosim/f_mem_model.v:14-18](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/cosim/f_mem_model.v#L14-L18) 描述：这是围绕 C 内存模型的 Verilog 外壳，「随着地址被访问而动态分配内存块」。（注：HDL 端口宽度为 32 位地址，底层 C 模型支持的地址空间更大；端口的 32 位地址足以覆盖本项目 IMEM/DMEM/MDIO 等所有实际用途。）

它对外暴露四组端口，覆盖了「单字寄存器、连续突发读、连续突发写、独立 SRAM 写口」四种访问风格：

[4.sim/models/cosim/f_mem_model.v:52-98](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/cosim/f_mem_model.v#L52-L98) 是模块端口声明，含两个参数 `EN_READ_QUEUE`/`REG_READ_OVERLAP`，以及寄存器从口、突发读从口、突发写从口、SRAM 写口。

无论哪组端口，最终都收敛到两个宏，由 `MEM_MODEL_SV` 决定它们是 DPI 函数还是 PLI 系统任务：

[4.sim/models/cosim/f_mem_model.v:40-48](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/cosim/f_mem_model.v#L40-L48) 把 `MEMREAD`/`MEMWRITE` 在 SV 模式下定义为 `MemRead`/`MemWrite`（DPI-C），否则为 `$memread`/`$memwrite`（PLI）。

以最简单的「单字寄存器读写」为例，看这两个宏怎么用：

[4.sim/models/cosim/f_mem_model.v:222-237](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/models/cosim/f_mem_model.v#L222-L237) 当 `read` 有效时调 `MEMREAD(address, readdata, byteenable)` 并拉高 `readdatavalid`；`@(negedge clk)` 后半拍，当 `write` 有效时调 `MEMWRITE(address, writedata, byteenable)`。

> 这段代码有个非标准的写法：`always @(posedge clk …)` 块内插了一句 `@(negedge clk);`（第 232 行），把「读在前半拍、写在后半拍」隔开，避免同拍读写竞争。这是该模型为时序清晰而采用的手法，符合其作为仿真模型的定位（不可综合）。

#### 4.1.4 代码实践：用 API 在 C++ 与 HDL 间传一个字

1. **实践目标**：亲手验证「C++ 写、HDL 读」走的是同一块内存。
2. **操作步骤**：
   - 在 `4.sim/usercode/` 下复制 `VUserMain0.cpp` 为一个新文件（例如 `VUserMain0_mem.cpp`），在其中用 `WriteRamWord(0x00010000, 0x900dc0de, /*le*/1, /*node*/0)` 往某个地址写一个标记字。
   - 在测试台 HDL 里例化一个 `mem_model`，把它配置成对一个已知地址做单字读，在仿真启动后几拍打印 `readdata`。
   - 用 `make -f MakefileVProc.mk USER_C=VUserMain0_mem.cpp run` 构建。
3. **观察现象**：HDL 侧打印出的 `readdata` 应等于 `0x900dc0de`。
4. **预期结果**：证明 C++ API 与 HDL 组件共享同一存储——这正是 mem_model 的全部价值。
5. 本地若无 Verilator/VProc 环境则**待本地验证**；可退化为纯源码阅读：在 `f_mem_model.v` 里追踪一次 `read` 请求如何经 `MEMREAD` 落到 C 模型，再对照 `4.sim/README.md` 第 150 行的 `WriteRamWord` 写入路径，确认二者地址空间一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么 mem_model 用「稀疏 + 按需分页」，而不是直接 `reg [31:0] mem[0:N]` 声明一整块？

> **答**：仿真里地址空间巨大（GB 级），但实际只用到零星几段；一次性声明整块会让仿真器试图分配天文数字的存储而崩溃或极慢。按需分页只在访问到时才为该页分配物理内存，把占用压到实际用量。

**练习 2**：API 里的 `node` 参数和 VProc 的 node 号是一回事吗？本项目里它通常取什么值？

> **答**：不是。API 的 `node` 是「地址空间编号」，用于支持多套独立内存空间；VProc 的 node 是「虚拟处理器编号」。本项目只用一个共享空间，故该参数恒为 0。

---

### 4.2 PCAP 回放/录制：端到端以太网验证

#### 4.2.1 概念说明

u7-l4 解决了「仿真里怎么发/收单个 UDP 帧」。但真实的 WireGuard 验证往往要喂**一串**带时间间隔的帧，并精确测量 DUT 的转发延迟与丢包。手写循环调 `UdpVpSendRawEthFrame` 既繁琐又难复现。

业界惯用 **PCAP 文件**做这件事：它就是 Wireshark/`tcpdump` 抓包的格式，一条记录 = 一个时间戳 + 一帧字节。于是验证流程变成：

- **回放（replay）**：读一个输入 PCAP，按其中帧的时间间隔，逐帧「喂」给 DUT 的以太网口（DUT 的 RX）。
- **录制（record）**：用接收回调捕获 DUT 转发出来的帧（DUT 的 TX），打上时间戳，写进输出 PCAP。
- **对照**：把输出 PCAP 用 Wireshark 打开，与输入 PCAP 比时间差，就得到「开始到开始」的延迟；与真实网卡抓包比，就验证了仿真与实网的一致性。

这套思路把仿真验证拉到了和真实抓包同样的「语言」，是 u8-l4 两节点实网验证的仿真对应物。

#### 4.2.2 核心流程

整套 PCAP 回放/录制由四个 `VUserMain<n>` 入口承担，构成两条独立的 1→2、3→4 数据流（都默认回放同一个 PCAP 文件）：

```text
  node1 (VUserMain1) : 读 PCAP ──按Δt发idle──▶ UdpVpSendRawEthFrame ──▶ eth1(DUT RX)
                          │ 记录每帧的绝对发送时刻 push 进 g_tx_times_node2 队列
                          ▼
                       [ DUT 转发 ]
                          │
  node2 (VUserMain2) : 收帧回调 ◀── eth2(DUT TX)
                          │ 读 TICKS 寄存器算 RX 绝对时刻
                          │ 弹出 g_tx_times_node2.front() 当作该帧的 TX 时刻
                          │ RX时刻 − 帧持续时长 − TX时刻 ≈ 开始到开始延迟
                          ▼
                       写 node2_out.pcap（RX 录制）
```

时间对齐的关键是把 **GMII 时钟周期**当作统一时间单位。千兆以太网 MAC 时钟 125 MHz，一个 tick = 8 ns：

\[ T_{\text{tick}} = \frac{1}{125\,\text{MHz}} = 8\,\text{ns} \]

发送侧：PCAP 里相邻帧的时间差 Δt（纳秒）被换算成要插多少个 idle tick，从而精确复现原始节奏；每发一帧，把它的「绝对发送时刻」压进一个共享队列。

接收侧：回调读 VProc 的 `TICKS_ADDR` 寄存器拿到当前 tick 计数（32 位，需处理回绕），乘 8 ns 得 RX 绝对时刻；再从队列头部弹出对应的 TX 时刻，二者之差（再扣除一帧在网线上持续的时间、并下限到 `min_latency_ns`）就是这一帧的「开始到开始」延迟。

#### 4.2.3 源码精读

**入口分配**——四个 node 各司其职，注释里点明了「回放 vs 录制」的分工：

[4.sim/usercode/VUserMainPcap.cpp:39-41](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/usercode/VUserMainPcap.cpp#L39-L41) 文件描述：这是一个「经 udpIpPg node 回放任意 PCAP 流、并把收到的 UDP 流录回 PCAP」的 VProc 应用。

node 定义（MAC/IP/UDP 端口）与初始 idle 预留在这里：

[4.sim/usercode/VUserMainPcap.cpp:73-92](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/usercode/VUserMainPcap.cpp#L73-L92) 定义四个 node 的地址与一个很小的初始 idle（让系统先稳定）。

**node1 回放**：构造 `udpIpPg` 对象、读环境变量拿到输入 PCAP 路径、打开 TX dump 与 merge 文件，然后调引擎 `ReplayPcap`：

[4.sim/usercode/VUserMainPcap.cpp:97-127](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/usercode/VUserMainPcap.cpp#L97-L127) `VUserMain1` 的主体；第 106 行用 `GetEnvOr("PCAP_IN_1", "./tools/test_udp_rand.pcap")` 取输入文件，第 118-126 行把回放引擎、TX 时间戳队列、merge logger 一起传进去。

**node2 录制**：打开输出 PCAP writer，把上下文（node_id、TX 时间戳队列指针、地址偏移、最小延迟）塞进 `RxRecorderCtx`，注册回调，然后死循环发 idle「保活」并让出时间给回调：

[4.sim/usercode/VUserMainPcap.cpp:132-156](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/usercode/VUserMainPcap.cpp#L132-L156) `VUserMain2` 注册 `RegisterRecorder`，第 154-155 行 `while (true) pUdp.UdpVpSendIdle(1);` 是关键——接收是「搭发送便车」（承接 u7-l4），必须有 node 在持续推进时间，回调才会被触发。

> node3/node4 的结构与 node1/node2 完全对称（第 161-219 行），只是走 eth3→eth4 这条第二条流，复用同一个默认 PCAP 文件。

**回放引擎**（`PcapReplay.h`）逐帧处理。先看时间节奏：把相邻帧纳秒差换算成 tick 数，不足的差值用 idle 补齐，精确复现 PCAP 节奏：

[4.sim/usercode/PcapReplay.h:404-421](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/usercode/PcapReplay.h#L404-L421) 计算本帧相对首帧的 `delta_ns` 与目标 `target_ticks`，若还没等待够就补发 `UdpVpSendIdle(1)`——这是「按 PCAP 时间间隔回放」的核心。

发送前可按需重写 MAC/IP/UDP 头，让 DUT 接受这些帧；之后把 preamble、FCS 拼齐，调原始发帧 API 送出：

[4.sim/usercode/PcapReplay.h:472-499](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/usercode/PcapReplay.h#L472-L499) 拼装前导码（7×0x55 + 0xD5）与 FCS（CRC32），第 499 行 `pUdp.UdpVpSendRawEthFrame(frame.data(), len)` 真正把帧打到 GMII 上。

每发一帧，把绝对发送时刻压进共享队列，供接收侧对齐：

[4.sim/usercode/PcapReplay.h:467-470](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/usercode/PcapReplay.h#L467-L470) 加锁把 `tx_abs_ns` 推进 `tx_times` 队列尾。

**录制回调**（接收侧）做三件事：读 tick 计数算 RX 时刻、弹出对应 TX 时刻、扣帧时长算「开始」时刻并写下限幅：

[4.sim/usercode/PcapReplay.h:510-549](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/usercode/PcapReplay.h#L510-L549) 第 516 行 `VRead(udpVProc::TICKS_ADDR, &ticks, …)` 读当前 tick；第 521-525 行处理 32 位回绕拼成 64 位；第 525-526 行 `ts_ns_abs = rx_ticks64 * GMII_TICK_NS`；第 528-535 行从队列头弹 TX 时刻；第 537-549 行算 `min_ts`、扣帧时长、下限幅，得到写入 PCAP 的「帧开始」时间戳。

帧在网线上持续的 tick 数（含前导码、FCS、帧间空闲）由这个函数估算，是扣减项的来源：

[4.sim/usercode/PcapReplay.h:366-376](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/usercode/PcapReplay.h#L366-L376) `WireTicksFromFrameSize` = 帧体字节数 +（可选）8 前导 +（可选）4 FCS + 1 帧间 idle。

**PCAP 文件格式**由 `PcapIO.h` 处理。关键在魔数自动识别——同一个 reader 能读大小端、微秒或纳秒四种 PCAP：

[4.sim/usercode/PcapIO.h:120-155](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/usercode/PcapIO.h#L120-L155) `detect_endian_and_res` 按 4 个已知魔数（`0xa1b2c3d4`/`0xd4c3b2a1`/`0xa1b23c4d`/`0x4d3cb2a1`）判定是否字节交换、时间分辨率是微秒还是纳秒。

写侧固定用纳秒、小端、network=1（Ethernet）落盘，便于和仿真里的纳秒时间戳一致：

[4.sim/usercode/PcapIO.h:195-215](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/usercode/PcapIO.h#L195-L215) `Writer::open` 写全局头，magic 在纳秒模式下取 `0xa1b23c4d`，`network=1` 表示 Ethernet。

**测试 PCAP 生成脚本** `gen_udp_pcap.py` 离线构造合法 UDP/IPv4 帧。它的魔数与上面 writer 一致（`MAGIC_NS = 0xA1B23C4D`，第 9 行），并自己算 IP/UDP 校验和：

[4.sim/tools/gen_udp_pcap.py:45-60](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/tools/gen_udp_pcap.py#L45-L60) `build_udp_frame` 拼 eth+ipv4+udp+payload，中途调 `ipv4_checksum`/`udp_checksum` 填正确校验和。

[4.sim/tools/gen_udp_pcap.py:62-74](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/tools/gen_udp_pcap.py#L62-L74) `write_pcap` 写纳秒全局头后，按指定 `interval_ns` 间隔逐帧落盘。

默认参数（源/目 MAC、IP、端口）正好对应 `VUserMainPcap` 里的 NODE1/NODE2：

[4.sim/tools/gen_udp_pcap.py:76-100](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/tools/gen_udp_pcap.py#L76-L100) 默认 `--src-mac D8:9E:F3:88:7E:C3`、`--dst-mac 90:32:4B:07:0B:D1` 等，与 NODE1_MAC_ADDR/NODE2_MAC_ADDR 一致。

最后，主 README 给出了一条「从生成到运行到看延迟」的完整快速入门：

[4.sim/README.md:527-537](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/README.md#L527-L537) 三步：`gen_udp_pcap.py` 生成 → `make … UDP_C=VUserMainPcap.cpp BUILD=ISS run` 运行 → 在 Wireshark 里把时间显示设为「Seconds Since Epoch」，TX→RX 的开始到开始延迟应与 `wave.fst` 里 GTKWave 量到的一致。

#### 4.2.4 代码实践：生成 PCAP → 跑仿真 → 量延迟

1. **实践目标**：亲手走一遍「PCAP 回放 → 录制 → 测延迟」的端到端流程。
2. **操作步骤**：
   - 生成测试 PCAP（注意要在 `4.sim/` 下执行，使相对路径 `./tools/…` 生效）：
     ```bash
     cd 4.sim
     python tools/gen_udp_pcap.py --frames 5 --interval-us 500 \
          --out ./tools/test_udp_rand.pcap
     ```
   - 构建并运行回放/录制仿真（用 `UDP_C=VUserMainPcap.cpp` 替换默认以太网用户码，并用 ISS 跑 node0）：
     ```bash
     make -f MakefileVProc.mk clean
     make -f MakefileVProc.mk UDP_C=VUserMainPcap.cpp BUILD=ISS run
     ```
   - 用 Wireshark 打开 `./output/node2_out.pcap` 与 `./output/merge_node2.pcap`，把时间显示设为「Seconds Since Epoch」或「Date and Time of Day」。
3. **观察现象**：node2 的输出 PCAP 里应出现 5 帧录制结果；每帧的 RX「帧开始」时间戳减去对应 TX 时刻，就是 DUT 转发该帧的「开始到开始」延迟。
4. **预期结果**：这个延迟值应当与在 `wave.fst`（GTKWave/Surfer）里用游标量到的 TX 起始与 RX 起始之差一致；改变 `--interval-us` 只挪动帧间距，单帧延迟应基本不变。
5. 若本机没有 Verilator/VProc/ISS 环境，**待本地验证**；可退化为源码阅读实践：在 `PcapReplay.h` 的 `RxRecordCallback`（第 510-549 行）里，用纸笔跟踪一个「TX 时刻 = 1000 ns、帧长 64 字节、RX tick 读到 250」的例子，算出最终写入 PCAP 的时间戳，验证你对延迟扣减逻辑的理解。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `VUserMain2`（录制 node）要在结尾 `while (true) UdpVpSendIdle(1);`，而不是直接 `return`？

> **答**：因为接收是「搭发送便车」（u7-l4）——只有在某个 node 持续调发送类 API 推进仿真时间时，GMII 上的输入才会被采样、接收回调才会被触发。若 node2 直接返回，它就不再推进时间，DUT 发往 eth2 的帧永远不会被采样录制。

**练习 2**：`RxRecordCallback` 把写入 PCAP 的时间戳设成「RX 时刻 − 一帧的持续时长」，为什么不直接用 RX 时刻？

> **答**：RX 时刻是「帧结束/被采样」的时刻，而要和 TX 侧的「帧开始」对齐比较延迟，就得把 RX 也回退到「帧开始」。扣除一帧在网线上的持续时长（`WireTicksFromFrameSize × 8ns`）正是为此；这样 TX 帧开始与 RX 帧开始之差才是真正的转发延迟。

**练习 3**：`PcapIO.h` 为什么要识别 4 种魔数？

> **答**：PCAP 的魔数同时编码了两件事——是否字节交换（大小端）与时间精度（微秒/纳秒）。抓包机与本机字节序可能相反，老抓包用微秒、新抓包用纳秒，4 种组合各对应一个魔数，识别后才能正确还原时间戳与数据。

---

### 4.3 逐模块测试台：脱离 VProc 的 RTL 级自测

#### 4.3.1 概念说明

u7-l1 的系统级测试台验证的是整个 `top`，优点是真，缺点是慢——每次跑都要拉起 VProc、mem_model、四个以太网 BFM。但开发某个 RTL 模块（如多路复用器、WG 加密器）时，你只想快速验证**这一个模块**的行为，不想等整个系统。

`4.sim/rtl/` 目录就是为这种「单模块自测」准备的——目录名 `rtl` 配上其 README 的一句话说明：

[4.sim/rtl/README.md:7](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/rtl/README.md#L7) 「Simulation scripts for individual RTLs or smaller subsystems」（针对单个 RTL 或较小子系统的仿真脚本）。

它下面每个子目录就是一个独立测试台，自带 `tb.sv` + `Makefile` + `versimSV.cpp`（Verilator C++ harness），部分还带 `tb.filelist` 与波形配置。它们**不依赖 VProc/mem_model**，用纯 Verilator `--timing` 跑，迭代很快。

#### 4.3.2 核心流程

逐模块测试台分两种激励风格：

```text
                       4.sim/rtl/<module>/
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                                           ▼
 (a) 定向激励型                          (b) PCAP 驱动型
 dpe / dpe_multiplexer /                dpe_wg_encryptor /
 dpe_demultiplexer /                    pcap_reader_writer /
 gmii2gmii_test                         chacha20poly1305_encrypt
        │                                           │
 在 initial/fork 里手写                用 dpe_pcapreader 读入 PCAP 当激励
 几个手搓包 → 看输出                    DUT 处理 → dpe_pcapwriter 写出 PCAP
        │                                           │
        └──────────── 都用同一套 Verilator Makefile ─┘
                      make -C 4.sim/rtl/<module> sim
```

- **定向激励型**：在 `initial`/`fork` 块里手工写几个小包（常常就 4-6 字节），并行打到多个输入口，再用 `always` 块监测输出口。适合验证仲裁、路由、握手这类控制逻辑。`dpe`、`dpe_multiplexer`、`dpe_demultiplexer`、`gmii2gmii_test` 属于这类。
- **PCAP 驱动型**：用现成的 `dpe_pcapreader` 组件把一个 PCAP 文件读成 AXIS 流喂给 DUT，再用 `dpe_pcapwriter` 把 DUT 输出录成另一个 PCAP。激励与期望都是真实的 PCAP，可直接用 Wireshark/脚本比对。`dpe_wg_encryptor`、`pcap_reader_writer`、`chacha20poly1305_encrypt` 属于这类。

二者共用一套标准 Makefile（以 `dpe/Makefile` 为模板），流程都是 Verilator 编译 → 生成可执行 → 运行 → 看波形。

#### 4.3.3 源码精读

**定向激励型代表：DPE 顶层测试台** `4.sim/rtl/dpe/tb.sv`。它例化完整的 `dpe` 模块，声明 5 进 5 出共 10 个 `dpe_if`：

[4.sim/rtl/dpe/tb.sv:59-68](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/rtl/dpe/tb.sv#L59-L68) 声明 `from_cpu`/`from_eth_1..4` 与 `to_cpu`/`to_eth_1..4` 共 10 个 dpe_if 接口。

DUT 例化时把端口一一接上，注意它直接例化 `dpe`（数据面引擎顶层），测试的是 mux→流水线→demux 整条数据通路：

[4.sim/rtl/dpe/tb.sv:86-99](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/rtl/dpe/tb.sv#L86-L99) 例化 `dpe DUT (...)`，接全部 5 对 AXIS 与 `from_csr`/`to_csr`。

激励用 `fork … join` 并行驱动 5 路输入，每路发一个小包并各自指定 `tuser_dst`（目的地址），`while (!tready) @(posedge clk)` 做握手等待：

[4.sim/rtl/dpe/tb.sv:159-249](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/rtl/dpe/tb.sv#L159-L249) 五个并行 `begin … end` 分别驱动 CPU/eth1/eth2/eth3/eth4，分别设 `tuser_dst = DPE_ADDR_ETH_1`、`DPE_ADDR_CPU`、`DPE_ADDR_ETH_3`、`DPE_ADDR_ETH_2`、`DPE_ADDR_BCAST`，覆盖单播、交叉、广播。

监测侧用 `always @(posedge clk)` 统计每个出口收到的 beat 数，在 `tlast` 时打印「Packet received with N words at port K」：

[4.sim/rtl/dpe/tb.sv:322-365](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/rtl/dpe/tb.sv#L322-L365) 监测进程，逐口在 `tvalid && tready` 时累加，遇 `tlast` 打印并清零。

**PCAP 驱动型代表：WG 加密器测试台** `4.sim/rtl/dpe_wg_encryptor/tb.sv`。它把「读明文 PCAP → 加密 → 写密文 PCAP」三件用三个组件串起来，几乎不用手写激励：

[4.sim/rtl/dpe_wg_encryptor/tb.sv:57-63](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/rtl/dpe_wg_encryptor/tb.sv#L57-L63) `dpe_pcapreader` 例化，参数 `PCAP_FILENAME="../plaintext_128.pcap"`，把读出的 AXIS 接到 `to_encryptor`。

中间是被测的加密器，密钥/地址等 RAM 端口直接用字面量常量驱动（这是模块级测试台的典型简化——把外部双口 RAM 用常量代替）：

[4.sim/rtl/dpe_wg_encryptor/tb.sv:65-83](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/rtl/dpe_wg_encryptor/tb.sv#L65-L83) 例化 `dpe_wg_encryptor`，本地/远端 MAC/IP/端口、256 位 `ram_encrypt_key`、`ram_send_cnt` 全部用常量绑定。

最后用 `dpe_pcapwriter` 把加密器输出录成 `ciphertext_128.pcap`：

[4.sim/rtl/dpe_wg_encryptor/tb.sv:85-89](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/rtl/dpe_wg_encryptor/tb.sv#L85-L89) `dpe_pcapwriter` 例化，`PCAP_FILENAME="../ciphertext_128.pcap"`，输入接 `from_encryptor`。

目录里还自带多种长度的明文/密文 PCAP（`plaintext_64/65/66/77/78/79/128/142/1506.pcap`、`ciphertext_128/1506.pcap`、`decrypted_loopback.pcap`），覆盖从最小包到超大包的边界用例，是「用 PCAP 当黄金向量」的好例子。

还有一个最纯粹的「PCAP 读→写回环」测试台 `pcap_reader_writer`，用来单独验证 reader/writer 组件本身（不经任何 DUT）：

[4.sim/rtl/pcap_reader_writer/tb.sv:55-66](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/rtl/pcap_reader_writer/tb.sv#L55-L66) `dpe_pcapreader(../test1.pcap)` 的输出直接喂给 `dpe_pcapwriter(../test2.pcap)`，纯回环，用来给 reader/writer「自测」。

**统一构建方式**——以 `dpe/Makefile` 为模板，每个模块目录都用同一套 Verilator 命令，关键在它仍然引用 `1.hw/top.filelist` 拉取设计源，但用本目录的 `tb.sv` 当顶层、`versimSV.cpp` 当 C++ harness：

[4.sim/rtl/dpe/Makefile:26-43](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/rtl/dpe/Makefile#L26-L43) `compile` 目标调 `verilator --cc --timing --trace-fst … -f ${HW_SRC}/top.filelist tb.sv --top-module tb`，`+define+SIM_ONLY` 关掉不可综合的行为模型差异。

[4.sim/rtl/dpe/Makefile:46-49](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/rtl/dpe/Makefile#L46-L49) `sim` 目标编出 `Vtb` 并 `./Vtb | tee sim.log`，完全脱离 VProc。

> 这种「每个模块一个自洽目录 + 统一 Makefile」的组织，让你在改某个 RTL 后能在几十秒内得到该模块的回归反馈，而不必跑整套系统仿真——这是大型 FPGA 项目保持开发节奏的关键工程实践。

#### 4.3.4 代码实践：跑一个逐模块测试台

1. **实践目标**：脱离 VProc，独立编译并运行 DPE 顶层定向激励测试台。
2. **操作步骤**：
   ```bash
   cd 4.sim/rtl/dpe
   make sim          # 等价于 make clean; make compile; 运行 Vtb
   ```
3. **观察现象**：终端 `sim.log` 里应依次打印 5 条形如 `Packet received with N words at port K` 的信息，对应 5 路输入按各自 `tuser_dst` 被分发到正确出口；最后 `Stimulus completed successfully` 并 `$finish`。
4. **预期结果**：因为第 5 路（eth4）设了 `DPE_ADDR_BCAST`（广播），你应能在多个出口都看到它那份；其余单播包各只出现在指定出口。可对照 `tb.sv` 第 159-249 行的 `tuser_dst` 设置核对分发是否正确。`make wave-alt`（用 Surfer）或 `make wave`（用 GTKWave）可打开 `wave.fst` 看波形。
5. 若本机无 Verilator，**待本地验证**；可退化为源码阅读实践：通读 `dpe/tb.sv` 第 256-313 行的「输出 ready 控制」进程，解释为何它要周期性拉低再拉高 `tready`（提示：制造背压，验证 DPE 在下游不ready时是否正确保数据不丢——这正呼应 u4-l2 讲过的「不能用 tready stall 撕包」）。

#### 4.3.5 小练习与答案

**练习 1**：逐模块测试台与 u7-l1 的系统级测试台，核心区别是什么？为什么开发期更愿意用前者？

> **答**：系统级测试台拉起整个 `top` + VProc + mem_model + 四个以太网 BFM，验证最真但编译慢、定位难；逐模块测试台只例化被测模块（常把外部 RAM 用常量代替），用纯 Verilator `--timing` 跑，编译快、激励聚焦、定位容易。开发期用它快速回归单个模块，系统级留给集成验证。

**练习 2**：`dpe_wg_encryptor/tb.sv` 里为什么把 `ram_encrypt_key`、`ram_local_mac` 等端口直接用字面量常量绑定，而不是接一个真的 RAM？

> **答**：这是模块级测试台的典型简化。本测试台的目的是验证加密器在给定密钥/地址下的 datapath 行为，不是验证密钥表 RAM（那是 u4-l6 的 tdp_ram，自有别的测试）。用常量代替外部 RAM，既消除了无关变量、又免去了再例化一个 RAM 模型的开销。

**练习 3**：`pcap_reader_writer/tb.sv` 不接任何 DUT，把 reader 直接连到 writer，它的意义是什么？

> **答**：这是 reader/writer 组件本身的「自测/金丝雀」。若回环后写出的 `test2.pcap` 与读入的 `test1.pcap` 内容一致，就证明 AXIS↔PCAP 的读写组件自身正确，之后在 `dpe_wg_encryptor` 这类测试台里就可以信任它作为激励源与期望接收方。

---

## 5. 综合实践

把本讲三块内容串成一个端到端小任务：**用 PCAP 驱动验证一段 AXIS 数据通路，并在仿真与「文件」两个层面对照结果。**

任务步骤：

1. **准备输入**：用 `4.sim/tools/gen_udp_pcap.py` 生成一个 5 帧、间隔 500 µs 的测试 PCAP（注意它产出的虽是 UDP/IPv4 帧，但在本任务里我们只把它当作「一串字节帧」来用）。
2. **系统级跑法**：在 `4.sim/` 下执行 `make -f MakefileVProc.mk UDP_C=VUserMainPcap.cpp BUILD=ISS run`，观察 `output/node2_out.pcap`、`merge_node2.pcap`。打开 Wireshark，量出 TX→RX 的「开始到开始」延迟，记下这个数。
3. **模块级跑法**：进入 `4.sim/rtl/pcap_reader_writer`，`make sim`，比较读入的 `test1.pcap` 与写出的 `test2.pcap` 是否逐字节一致；再进入 `4.sim/rtl/dpe` 跑 `make sim`，核对 5 路定向激励的分发结果。
4. **串联思考（回答下列问题，作为本讲掌握度的自检）**：
   - 步骤 2 里，node2 收到的帧「内容」经过了 mem_model 吗？为什么？（提示：以太网 VIP 的帧走的是 GMII 时序与 `UdpVpSendRawEthFrame`，而非 mem_model；mem_model 在这条路径上服务的是 MDIO 寄存器与 ISS 的 IMEM，不要把两条「跨域通道」混淆。）
   - 步骤 3 的回环测试里，数据完全没有经过任何 C++/DPI，纯 HDL；这说明 AXIS↔PCAP 的转换（`dpe_pcapreader/writer`）是纯 RTL 实现。结合步骤 2 的 C++ 版 `PcapIO.h`/`PcapReplay.h`，你能说清「系统级用 C++ 读写 PCAP」与「模块级用 RTL 读写 PCAP」这两套 PCAP 处理的区别吗？
   - 把步骤 2 量到的延迟，与你在 `wave.fst` 里用游标量到的同一帧 TX 起始↔RX 起始之差比较，应当一致。若不一致，最可能出错的环节是哪个？（提示：回看 4.2.3 里 `RxRecordCallback` 的「扣帧时长」「下限幅」两步。）

若本机不具备仿真环境，上述步骤 2/3 标注**待本地验证**，但步骤 4 的三问可以完全基于本讲源码阅读作答。

## 6. 本讲小结

- **mem_model 是仿真的「跨域共享黑板」**：一块 C 实现的稀疏、按需分页内存，C++ 端用 `WriteRamWord`/`ReadRamWord` 直通访问、HDL 端用 `mem_model` 组件经 DPI 访问，所有实例与所有 VProc node 共享同一副本——这就是「跨域传数据 = 同址读写」。
- **mem_model 端口覆盖四种访问风格**：寄存器单字、突发读、突发写、SRAM 写口，全部收敛到 `MEMREAD`/`MEMWRITE` 两个宏；它是仿真模型（不可综合，含 `@(negedge clk)` 等非标准写法）。
- **PCAP 回放/录制把验证拉到 Wireshark 的「语言」**：`VUserMainPcap` 的 node1/3 用 `ReplayPcap` 按 PCAP 时间间隔回放，node2/4 用回调录制；以 GMII 的 8 ns tick 为统一时间单位，对齐 TX/RX 时间戳即可测「开始到开始」延迟。
- **接收是「搭发送便车」**：录制 node 必须持续 `UdpVpSendIdle` 推进时间，否则回调永不触发——这是 u7-l4 设计在 PCAP 流水线里的直接体现。
- **PCAP 文件靠魔数自描述**：`PcapIO.h` 用 4 种魔数识别大小端 × 微秒/纳秒；`gen_udp_pcap.py` 生成纳秒小端 Ethernet PCAP，默认 MAC/IP 与 NODE1/NODE2 对齐。
- **逐模块测试台是开发期的快迭代利器**：`4.sim/rtl/<module>/` 各自独立、脱离 VProc、共用一套 Verilator Makefile；分「定向激励型」与「PCAP 驱动型」两种风格，后者用 `dpe_pcapreader/writer` 把 PCAP 当黄金向量。

## 7. 下一步学习建议

- **走向实网**：本讲的 PCAP 验证是「仿真里的端到端」，下一步请读 u8-l4「两节点实验室端到端验证」，看同一套 PCAP 思路如何在两块真实 AX7201 板卡上用 Wireshark 抓包重现，并对照仿真延迟与实网延迟。
- **吃透被测模块**：本讲把 `dpe`、`dpe_wg_encryptor` 当黑盒用了。若想理解它们内部，回到 Unit 4：u4-l2（多路复用器）、u4-l3（解复用器）、u4-l5（WG 封装/解封装与加解密数据流）；加密核内部见 Unit 5。
- **继续读源码**：挑一个还没展开的逐模块测试台（如 `chacha20poly1305_encrypt` 或 `gmii2gmii_test`），对照其 `tb.sv` 与本讲的两种风格归类，并尝试用 `make sim` 跑通；这是把「读测试台」变成「会写测试台」的最短路径。
