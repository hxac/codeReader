# HW/SW 分区：控制面与数据面

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 wireguard-fpga 为什么要把系统拆成「控制面（软件）」和「数据面（硬件）」两层，以及这条边界画在哪里。
- 区分两类网络流量：低频、稀疏的**握手控制流量**，和线速、大批量的**用户数据流量**，并知道它们各自走哪条路径。
- 认识 **CSR HAL** 是两个平面之间唯一的桥梁，能在 `top.sv` 里指出 `to_csr`/`from_csr` 这对总线如何同时连到 CPU 子系统、`cpu_fifo` 和 DPE。
- 理解当前 HEAD 处于 Phase1 PoC：完整的 WG 处理链已写好，但 `top.filelist` 里编入的是直通的 `dpe_dummy_switch`。

本讲是 Unit 2（SoC 硬件架构）的入口篇。它承接 u1-l2 建立的「六大子系统」地图，把 `1.hw`（数据面 RTL）和 `2.sw`（控制面固件）这两大主干**为什么**要分开、又**怎么**连起来讲透。后续 u2-l2 讲顶层模块、u2-l4 讲总线、Unit 3 整个单元讲 CSR 细节，都会反复用到本讲建立的两层心智模型。

## 2. 前置知识

在进入源码前，先用大白话建立几个概念。如果你已读过 u1-l1、u1-l2，下面的术语会更顺。

### 2.1 什么是「面」（plane）

在网络设备里，「面」指的是处理数据的**职责层**，不是物理位置：

- **控制面（control plane）**：负责「下决定」——建连接、管密钥、维护路由表。它处理的是**低频但需要复杂逻辑**的事，比如 WireGuard 握手。在本项目里它是一段跑在软 CPU 上的 C 程序。
- **数据面（data plane）**：负责「搬数据」——把每个到达的数据包快速转发、加密、解密。它处理的是**高频、重复、规则固定**的事。在本项目里它是纯 RTL 硬件流水线。

一个生活类比：控制面像**红绿灯控制器**（偶尔改一次配时），数据面像**路口的车流**（每秒上千辆通过）。把两者分开，是因为它们对「快」和「灵活」的要求完全冲突——软件灵活但慢，硬件快但不灵活。

### 2.2 为什么软件跑不到线速

线速（wire speed）指「物理链路能跑多快，设备就能处理多快，不丢包」。本项目有 4 个 1Gbps 网口，所以数据面至少要能处理 4Gbps。软件在软 CPU（约 80MHz 的 picoRV32）上做 ChaCha20-Poly1305 加解密，远远达不到这个速率；而专用硬件流水线可以。这就是数据面必须是 RTL 的根本原因。

反过来，WireGuard 握手消息「在连接初始化时发，之后每隔几分钟做一次密钥轮换」，频率极低（见 `1.hw/README.md` 第 36 行的说明）。这种稀疏流量用一个慢 CPU 处理绰绰有余，没必要做成昂贵的硬件状态机。这就是控制面可以是软件的根本原因。

> **直觉总结**：把「慢但灵活」的活留给软件 CPU，把「快但死板」的活交给硬件 RTL。两者各做各的擅长事。

### 2.3 两个面怎么对话：CSR 与 HAL

两个面既然分开，就必须有个通信通道。本项目用的是一对经典概念：

- **CSR（Control and Status Registers，控制与状态寄存器）**：一组 CPU 能读写的寄存器，硬件也盯着它们。CPU 写一个寄存器值 = 给硬件下指令；CPU 读一个寄存器值 = 查硬件状态。
- **HAL（Hardware Abstraction Layer，硬件抽象层）**：软件侧的一层封装，把「读写某个 CSR 地址」包装成好记的 C 函数（如 `csr.dpe.fcr.pause = 1`），让固件代码不必记裸地址。

关键设计决策：这对 CSR **不是手写的**，而是从一份 SystemRDL 规格文件（`csr.rdl`）用 PeakRDL **自动生成**的——同一份规格同时产出硬件 RTL 和软件 HAL。这是本项目「单一真源（single source of truth）」的核心，将在 Unit 3 深入讲。本讲你只需记住：**CSR HAL 是两个平面之间唯一的桥梁。**

### 2.4 几个会反复出现的缩写

| 缩写 | 全称 | 一句话解释 |
|------|------|-----------|
| SoC | System on Chip | 片上系统，把 CPU、内存、外设都做进一颗芯片 |
| DPE | Data Plane Engine | 数据面引擎，本项目自研的数据面 RTL 总称 |
| RTL | Register Transfer Level | 寄存器传输级，即 Verilog/SystemVerilog 描述的硬件 |
| AXIS | AXI4-Stream | 一种标准的「数据流」接口，用 TVALID/TREADY 握手 |
| FIFO | First In First Out | 先进先出队列，这里用来缓存整个数据包 |
| FCR | Flow Control Register | 流控寄存器，用来让 CPU 优雅地暂停数据面 |
| RMW | Read-Modify-Write | 读-改-写，一种低效的寄存器访问模式 |

## 3. 本讲源码地图

本讲涉及的文件不多，但每一个都直指「两层架构」的某个侧面：

| 文件 | 在本讲的作用 |
|------|------------|
| [README.md](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/README.md) | 顶层 README，给出官方的 HW/SW 分区图和「控制流量 vs 数据流量」定义 |
| [1.hw/README.md](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/README.md) | 硬件 README，列出数据面全部 IP 核，并用 55 步 walkthrough 演示两层如何协作 |
| [1.hw/top.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv) | 顶层模块，**眼见为实**地展示 CPU 子系统、CSR、cpu_fifo、DPE 如何在同一文件里连线 |
| [1.hw/ip.dpe/dpe.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv) | 数据面引擎本体，看它怎么从 CSR 读 `pause`、写 `idle`，并持有路由/密钥两张表 |
| [1.hw/ip.infra/cpu_fifo.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/cpu_fifo.sv) | 控制面与数据面之间的**包级桥梁**，把 128 位 AXIS 拆成 32 位 CSR 寄存器 |
| [1.hw/top.filelist](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.filelist) | 文件清单，证明当前 PoC 编入的是 `dpe_dummy_switch` 而非完整 WG 链 |

阅读建议：先读本讲第 4 节建立概念，再打开 `top.sv` 对照——它是验证「两层架构」最直接的证据。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，正好对应标题里的三层：**控制面职责**（4.1）、**数据面职责**（4.2）、**CSR HAL 桥梁**（4.3）。

### 4.1 控制面职责

#### 4.1.1 概念说明

控制面是系统的「大脑」，本质是一段**跑在片上软 CPU 里的 C 固件**。它的核心是一个叫 **WireGuard Agent** 的组件，负责执行 WireGuard 协议的握手流程（基于 Noise 协议框架）。围绕它还有一圈加密与工具组件：

- **Curve25519**：椭圆曲线 Diffie-Hellman（ECDH），两节点用它协商出共享密钥。
- **ChaCha20-Poly1305 / XChaCha20-Poly1305**：AEAD 加解密，用于握手消息里的静态密钥与 nonce。
- **BLAKE2s / HKDF**：哈希与密钥派生，Noise 协议的基石。
- **RNG / Timer / RTC**：随机数、定时器（rekey/retry/keepalive）、实时时钟（生成 TAI64N 时间戳）。
- **Routing DB Updater**：维护 cryptokey 路由表，并通过 HAL 把表**下发到数据面**。
- **CLI**：基于 UART 的命令行，让管理员配置本节点（IP、peer、密钥等）。

最重要的一条原则：**控制面只处理低频的握手与管理流量，绝不参与线速的用户数据转发。** 这条原则在项目执行计划里写得非常明确——见 README 的 Take3 条目：「SW must not participate in the bulk datapath transfers」（软件不得参与批量数据面传输），「SW may however intercept the low-frequency management packets」（但可以拦截低频管理包）。

#### 4.1.2 核心流程

控制面在系统里主要做三类事，画成伪代码：

```
# 1. 发起/响应握手（低频，几分钟一次）
on need_to_connect(peer):
    packet = WireGuardAgent.build_handshake_init(peer)   # 用 Curve25519/HKDF/BLAKE2s
    cpu_fifo_send(packet, dst=eth_out)                   # 经桥梁送数据面 → 外网
    # ... 对端回 Response，CPU 收到后：
    WireGuardAgent.process_handshake_response(resp)
    RoutingDBUpdater.install_keys_and_routes(...)        # 算出新会话密钥
    csr_write_tables(...)                                 # 经 HAL 下发到数据面

# 2. 周期性维护
on timer_fire():
    WireGuardAgent.rekey_or_keepalive()                  # 密钥轮换/保活

# 3. 响应管理配置
on cli_command(cmd):
    parse_and_apply(cmd)                                 # config network/routes/cryptokeys
```

注意第 1 类里的 `cpu_fifo_send` 和 `csr_write_tables`：这两步是控制面**触碰数据面的唯一方式**，都经过 CSR HAL。握手包本身也要先进入数据面的流水线（再由 MAC 发到网线上），因为物理出口在数据面那边。

#### 4.1.3 源码精读

先看官方对控制面组件的总览。`2.sw/README.md` 列出了 WireGuard Agent 及其全部辅助组件（Curve25519、BLAKE2s、HKDF、RNG、Timer、Routing DB Updater、CLI、HAL/CSR Driver 等）：

> [2.sw/README.md:10-23](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/README.md#L10-L23) —— 软件控制面的组件清单，居中的是 WireGuard Agent，周围一圈是加密原语与 Routing DB Updater、HAL/CSR Driver。

再确认「软件不碰批量数据」这条原则的出处。README 在「Recognized Challenges」里把 HW/SW 划分列为头号挑战，并说明本项目虽不用外接 PC，但仍有一颗片上 RISC-V CPU，带着复杂的硬件接口和显著的控制软件成分：

> [README.md:94-95](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/README.md#L94-L95) —— HW/SW 分区被列为第一项挑战：片上 RISC-V CPU 控制着线速数据面的主干。

然后看 `1.hw/README.md` 第 36 行——这是「为什么控制面可以用慢 CPU」最直接的论证：握手流量稀疏，所以控制面与数据面之间**不用 DMA**，而是让 CPU 直接通过 CSR 接口操作 Tx/Rx FIFO：

> [1.hw/README.md:36](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/README.md#L36) —— 红色时钟域涵盖整个 CSR 与外设；由于握手只在建连和几分钟一次的密钥轮换时出现，作者选择**不上 DMA**，用 CPU 直接经 CSR 喂 FIFO。

最后在 `top.sv` 里找到控制面的物理实体——`soc_cpu` 实例（picoRV32 软核），它带 64KB 指令 RAM 和 64KB 数据 RAM：

> [1.hw/top.sv:170-180](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L170-L180) —— `soc_cpu u_cpu` 即控制面的软 CPU，其 `bus` 口作为主设备（MST）挂在 SoC 总线上。

#### 4.1.4 代码实践

**实践目标**：在源码里验证「控制面 = 软 CPU + 一组 C 组件」，并定位它对外唯一的接口。

**操作步骤**：

1. 打开 `2.sw/README.md`，把第 10–23 行列出的组件抄成一张表，标注每个组件属于「握手协议」「加密原语」「工具」还是「下发/配置」。
2. 打开 `1.hw/top.sv`，定位 `soc_cpu u_cpu`（约第 170 行），确认它的端口里只有一个 `bus`（MST）和 4 个 `imem_*` 信号（用于在线烧写程序）——**它没有任何直连以太网的线**。
3. 顺着 `bus_cpu` 这根线往下看，找到 `soc_fabric`（第 183 行），看 CPU 主口被译码到哪几个从口。

**需要观察的现象**：

- CPU 的所有外部访问都汇聚成一捆 `soc_if bus`，没有旁路。
- 控制面想碰数据面，只能经 `bus → fabric → bus_csr → soc_csr` 这条路。

**预期结果**：你会清楚地看到，控制面是一颗「只挂在总线上的 CPU」，它和数据面的唯一物理接触点就是 CSR。

**待本地验证**：若你已构建过固件（见 u1-l4），可在 `2.sw/app/` 下找到 `wireguard_libs.cpp`、`main.cpp` 等源文件，确认 WireGuard Agent 的实现确实在软件侧。

#### 4.1.5 小练习与答案

**练习 1**：为什么作者敢「不上 DMA」？用一句话和一个数字回答。
> **答**：因为握手流量稀疏（几分钟一次），CPU 直接经 CSR 喂 FIFO 即可；`2.sw/README.md` 第 253 行估算该接口上限约 **170Mbps**，远低于 1Gbps 数据面，但处理握手绰绰有余。

**练习 2**：如果有一天要把 WireGuard 握手改成「每秒上千次」，这套设计会出什么问题？
> **答**：控制面的 ~170Mbps CSR-FIFO 接口会成为瓶颈，CPU 来不及收发握手包；届时要么上 DMA，要么把部分握手逻辑也搬进硬件。

---

### 4.2 数据面职责

#### 4.2.1 概念说明

数据面是系统的「肌肉」，是纯 RTL 硬件，项目里统称 **DPE（Data Plane Engine）**。它的工作只有一件：**对外网到来的每个用户数据包，以线速完成路由查找 + 加密/解密 + 转发**。它完全不知道 WireGuard 握手协议长什么样——握手是控制面的事，数据面只负责在隧道建立后，用控制面下发好的密钥去加解密用户流量。

`1.hw/README.md` 按数据包在网络里的流动方向，列出了数据面的全部 IP 核（共十余个）。把它们归一下类，就能看清数据面的三层结构：

| 层 | IP 核 | 职责 |
|----|-------|------|
| **以太网接入层** | PHY Controller、1G MAC、Rx/Tx FIFO | 配 PHY、跑 1G 以太网协议（成帧/FCS）、跨时钟域与位宽转换、存整包 |
| **DPE 流水线** | 轮询 Mux、Header Parser、WG Disassembler、Decryptor、IP Lookup、Encryptor、WG Assembler、Demux | 拆 WG 头→解密→查路由→加密→装 WG 头 |
| **加密核** | ChaCha20-Poly1305 Encryptor/Decryptor | AEAD 加解密与认证（被上面流水线调用） |

#### 4.2.2 核心流程

数据面对一个**用户数据包**的处理链（即 `1.hw/README.md` 55 步 walkthrough 的后半段，第 36–55 步）：

```
[外网] → PHY → 1G MAC(算FCS) → Rx FIFO(存整包)
   → 轮询Mux(注入流水线)
   → Header Parser(提取目的IP/协议类型)
   → WG Disassembler(剥WG/UDP/IP头, 取密文)
   → ChaCha20-Poly1305 Decryptor(解密+验tag)
   → IP Lookup(按源IP查cryptokey表, 决定收/拒)
   → Demux(按目的送Tx FIFO)
   → 1G MAC → PHY → [外网/内网]
```

对**反向**的用户包（本节点要发出去的明文），链是对称的：IP Lookup 选 peer → Encryptor 加密加 tag → WG Assembler 装 WG/UDP/IP/Eth 头 → Demux → MAC → PHY。

关键数字（来自 `1.hw/README.md` 第 38 行）：4 个 1G 口要求加密核至少处理 **4Gbps**；对只看包头不看载荷的 IP Lookup，更关键的是包速率，最坏情况（64 字节小包）每口 1,488,096 pps，4 口合计约 **6 Mpps**。

> **⚠️ Phase1 PoC 现状（重要）**：上面这条完整的 WG 解封装→解密→路由→加密→封装链，源码里**已经写好**（`dpe_wg_disassembler.sv`、`dpe_wg_encryptor.sv` 等），但当前 HEAD 的 `top.filelist` **没有编入它们**，而是编入了直通的 `dpe_dummy_switch`。也就是说，此刻上板的 bitstream 里数据面是「明文直通 + 软件桥接」，不是真加密隧道。这是 u1-l1 讲过的 Phase1 PoC 定位的具体体现。完整的 WG 链将在 Phase2 上线。

#### 4.2.3 源码精读

先看官方对数据面结构的总述——硬件架构「遵循 HW/SW 分区，由两个域组成：控制面的软 CPU 和数据面的 RTL」，两域经 CSR HAL 连接：

> [1.hw/README.md:10-12](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/README.md#L10-L12) —— 一句话点明两层架构，以及 CSR HAL 是连接器。

数据面的 IP 核清单（按数据流向）：

> [1.hw/README.md:14-24](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/README.md#L14-L24) —— 从 PHY Controller 一路列到 Tx FIFO，覆盖接入层与 DPE 流水线全部组件。

线速指标的推导（为什么必须 4Gbps、6Mpps）：

> [1.hw/README.md:38](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/README.md#L38) —— 4×1Gbps 要求加密核至少 4Gbps；64 字节小包最坏情况下 IP Lookup 需 ~6Mpps。

数据面的物理实体——`top.sv` 里的 `dpe u_dpe` 实例。注意它的端口：5 路 `from_*`（CPU + 4 eth）输入、5 路 `to_*` 输出，外加 `from_csr`/`to_csr`：

> [1.hw/top.sv:243-256](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L243-L256) —— DPE 实例，`from_csr`/`to_csr` 是它与控制面的唯一纽带，`from_cpu`/`to_cpu` 是它与控制面包级的纽带。

进入 DPE 内部看 Phase1 PoC 的证据。`dpe.sv` 把多路复用器→（被注释的 egress）→`dpe_dummy_switch`→解复用器串起来：

> [1.hw/ip.dpe/dpe.sv:67-92](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv#L67-L92) —— `dpe_multiplexer` → `dpe_dummy_switch`（直通）→ `dpe_demultiplexer`；中间本应是 `dpe_egress_ip_lookup`（第 95–103 行）被整段注释。

`top.filelist` 同样只编入 dummy switch，注释掉了 WG 解封装器：

> [1.hw/top.filelist:70-74](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.filelist#L70-L74) —— DPE 文件清单：`dpe.sv`、`dpe_multiplexer.sv`、`dpe_demultiplexer.sv`、`dpe_dummy_switch.sv` 编入；`dpe_wg_disassembler.sv` 被注释。

最后看一眼完整的「数据包穿越数据面」的官方 55 步叙事里的关键几步（用户数据 ICMP Echo 在对端被解密转发的阶段）：

> [1.hw/README.md:181-190](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/README.md#L181-L190) —— 第 46–55 步：MAC 收帧→Rx FIFO→Mux→Header Parser→WG Disassembler→Decryptor 验 tag→IP Lookup 查 cryptokey 表决定收/拒→Demux→Tx FIFO→MAC→发出。

#### 4.2.4 代码实践

**实践目标**：亲手在源码里确认「完整 WG 链已写好但未上线」这一 PoC 现状。

**操作步骤**：

1. 打开 `1.hw/top.filelist`，找到「DPE」段（约第 69–74 行）。
2. 数一数：哪些 `.sv` 文件是**未注释**（真正编入）的？哪些是**被注释**（写好但未用）的？
3. 用 `ls 1.hw/ip.dpe/`（或 Glob `1.hw/ip.dpe/*.sv`）列出该目录下实际存在的所有源文件。
4. 对比：被注释的文件（如 `dpe_wg_disassembler.sv`、`dpe_wg_encryptor.sv`）在磁盘上是否存在？

**需要观察的现象**：

- 编入的只有 `dpe.sv`、`dpe_multiplexer.sv`、`dpe_demultiplexer.sv`、`dpe_dummy_switch.sv`。
- 磁盘上还存在 `dpe_wg_disassembler.sv`、`dpe_wg_encryptor.sv`、`dpe_wg_decryptor.sv`、`dpe_egress_ip_lookup.sv` 等文件，但它们没进 filelist。

**预期结果**：你会得到一张「已实现 vs 已上线」的对照表，直观看到 Phase1 PoC 的边界——数据面的骨架（mux/switch/demux）在线，WG 加解密与路由查找的肉还没挂上去。

**待本地验证**：目录下的确切文件列表以你本地 `ls` 结果为准；本讲引用的文件名来自 `top.filelist` 注释行，确认存在。

#### 4.2.5 小练习与答案

**练习 1**：数据面的「线速」要求是多少？分别从数据速率和包速率两个角度回答。
> **答**：数据速率至少 4Gbps（4×1Gbps）；包速率最坏约 6Mpps（4×1,488,096 pps，针对 64 字节小包的 IP Lookup）。

**练习 2**：当前 HEAD 的 bitstream 里，一个用户数据包经过 DPE 时实际被加解密了吗？为什么？
> **答**：没有。因为 `top.filelist` 编入的是 `dpe_dummy_switch`（直通），WG 解封装/加解密/路由查找的源文件虽存在但被注释未编入，所以此刻是明文直通。

---

### 4.3 CSR HAL 桥梁

#### 4.3.1 概念说明

两个面既然分开，就必须有桥梁。本项目里这座桥就是 **CSR + HAL**，而且它是**唯一**的桥——控制面和数据面之间没有任何其它直接连线。理解这一点是看懂整个 SoC 的钥匙。

这座桥承担三类通信：

1. **包级通信**：握手包要进/出数据面（因为物理网口在数据面那边）。这由 `cpu_fifo` 完成——它把 128 位的 AXIS 数据流拆成 CPU 能处理的 32 位 CSR 寄存器。
2. **表级通信**：控制面算好密钥和路由后，要把它们写进数据面查找用的表（`routing_table`、`cryptokey_table`）。这两张表是 **external regfile**，在 RTL 里用双口 RAM（`tdp_ram`）实现：A 口给 CPU 经 CSR 读写，B 口给数据面查找。
3. **控制级通信**：改表前要让数据面**优雅暂停**（不能改到一半被数据面读到半成品）。这用一对 FCR（流控寄存器）信号 `pause`/`idle` 完成。

「HAL」是软件侧的称呼——它把裸 CSR 地址包装成 `csr.dpe.fcr.pause = 1` 这样的 C 表达式。这份 HAL 和硬件侧的 `csr.sv` 是从同一份 `csr.rdl` 自动生成的（Unit 3 详讲），所以两边永远对得上。

#### 4.3.2 核心流程

把三类通信画成一张数据流：

```
                 ┌─────────── 控制面（软件, 80MHz/32bit）───────────┐
                 │  WireGuard Agent / Routing DB Updater / CLI      │
                 │        （经 HAL 读写 CSR，HAL=csr_hw.h）          │
                 └───────────────────────┬──────────────────────────┘
                                         │ 唯一桥梁：CSR 总线
                          to_csr ────────┼──────── from_csr
                          (csr__in_t)     │      (csr__out_t)
                                         │
        ┌────────────────────────────────┼────────────────────────────────┐
        │  ① soc_csr  ② cpu_fifo         │         ③ DPE                   │ 数据面
        │   (寄存器    (包级桥:           │    (pause/idle, routing_table,  │ (硬件)
        │    解码)      128b AXIS↔32b CSR)│     cryptokey_table 都是 CSR)   │
        └────────────────────────────────┴────────────────────────────────┘
```

三类通信的握手要点：

- **包级**：`cpu_fifo` 用 `singlepulse` 的 TVALID/TREADY 触发一次 128 位传送，CPU 不必和 AXIS 时钟对齐（详见 u3-l3）。
- **表级**：CPU 写表走 `req`/`ack` 握手，`tdp_ram` A 口落盘（详见 u4-l6）。
- **控制级（FCR）**：CPU 写 `pause=1` → 数据面 Mux 处理完当前包后进 PAUSED、置 `idle=1` → CPU 轮询到 `idle=1` 才安全改表 → 改完写 `pause=0` 恢复。这是**原子更新**，防止数据面读到半成品表项（详见 u3-l4）。

为什么不能直接用 AXI 的 TREADY 反压（stall）来暂停？因为「已经进入流水线的包必须按它进入时生效的规则处理完」，简单 stall 会破坏这个不变量。所以需要一个能在包**边界**处暂停的优雅机制——这就是 FCR。

#### 4.3.3 源码精读

先在 `top.sv` 里看到这座桥的物理实体——一对总线 `to_csr` / `from_csr`：

> [1.hw/top.sv:150-151](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L150-L151) —— 声明 `csr_pkg::csr__in_t to_csr` 与 `csr_pkg::csr__out_t from_csr`，这是贯穿全片的 CSR 总线对。

`to_csr`/`from_csr` 被**三处**共享，证明它确实是公共桥梁。第一处是 `soc_csr`（CSR 的硬件解码器，CPU 经总线访问它）：

> [1.hw/top.sv:199-203](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L199-L203) —— `soc_csr u_soc_csr` 把 `hwif_in(to_csr)` / `hwif_out(from_csr)` 接出来，CPU 经 `bus_csr` 访问它。

第二处是 `cpu_fifo`（包级桥），它同时接 `from_csr`/`to_csr` 和 `from_cpu`/`to_cpu` 两对 dpe_if：

> [1.hw/top.sv:225-230](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L225-L230) —— `cpu_fifo u_cpu_fifo` 一侧连 CSR 总线、一侧连数据面的 dpe_if，正是「包级桥梁」。

第三处是 DPE 本体（表级 + 控制级桥）：

> [1.hw/top.sv:243-247](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L243-L247) —— `dpe u_dpe` 也接 `from_csr`/`to_csr`，数据面通过它读 `pause`、写 `idle`、暴露两张表。

深入 `cpu_fifo.sv` 看「包级桥」如何把 128 位 AXIS 拆成 32 位 CSR。它实例化一个 `axis_fifo`，输出端把 128 位 `tdata` 拼接成 4 个 32 位 CSR 字段（`data_31_0` … `data_127_96`）：

> [1.hw/ip.infra/cpu_fifo.sv:51-90](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/cpu_fifo.sv#L51-L90) —— `axis_fifo` 的 `m_axis_tdata` 被拼接映射到 `to_csr.cpu_fifo.tx.data_127_96/.../data_31_0.tdata.next`，`tvalid/tready/tlast/tuser` 也分别映射到对应 CSR 字段。这就是 128 位 AXIS 与 32 位 CSR 的接合处。

再看「表级桥」——`dpe.sv` 用两个 `tdp_ram` 实例化 `routing_table`（8 位地址）和 `cryptokey_table`（11 位地址），A 口全接 CSR 的 `req`/`addr`/`wr_data`/`rd_data`/`ack`：

> [1.hw/ip.dpe/dpe.sv:105-121](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv#L105-L121) —— `u_routing_table` 的 A 口接 `from_csr.routing_table.*` / `to_csr.routing_table.*`，CPU 经 CSR 写表、数据面经 B 口查找（B 口当前固定未用，待 WG 链上线后接入）。

最后看「控制级桥」——FCR 的 `pause`/`idle` 如何在 DPE 多路复用器与 CSR 之间连接：

> [1.hw/ip.dpe/dpe.sv:67-69](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv#L67-L69) —— `dpe_multiplexer` 的 `pause` 来自 `from_csr.dpe.fcr.pause.value`，`is_idle` 回写到 `to_csr.dpe.fcr.idle.next`。CPU 写 pause、轮询 idle，构成原子更新握手。

`dpe_multiplexer` 的端口定义印证了这对信号：

> [1.hw/ip.dpe/dpe_multiplexer.sv:43-53](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_multiplexer.sv#L43-L53) —— Mux 顶层就是 `input pause` / `output is_idle` 加 5 路 dpe_if，pause/idle 是它对外唯一的控制观测点。

CPU 侧执行 FCR 原子更新的 8 步流程，写在 `2.sw/README.md`：

> [2.sw/README.md:256-268](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/README.md#L256-L268) —— FCR 原子更新：CPU 写 `pause=1` → 轮询 `idle=1` → 改表 → 写 `pause=0`，并解释了为何不能用 AXI 的 TREADY stall。

#### 4.3.4 代码实践

**实践目标**：在 `top.sv` 里追踪 `to_csr`/`from_csr` 这对总线，证明它被 CPU、`cpu_fifo`、DPE 三方共享，是唯一的桥。

**操作步骤**：

1. 打开 `1.hw/top.sv`，用查找定位所有出现 `to_csr` 和 `from_csr` 的行。
2. 给每一处标注它属于哪个实例（`soc_csr`、`cpu_fifo`、`dpe`，以及 GPIO/UART 等其它 CSR 使用者）。
3. 画出这三处与 `to_csr`/`from_csr` 的连接关系图（谁读 `from_csr`、谁写 `to_csr`）。
4. 思考：为什么所有 CSR 使用者都**共享同一对** `to_csr`/`from_csr`，而不是各自拉一组线？

**需要观察的现象**：

- `from_csr` 是「硬件输出给所有人看的现状」（如 `from_csr.dpe.fcr.pause.value`、`from_csr.gpio.led1.value`），像广播。
- `to_csr` 是「各方写回硬件的下一拍值」（如 `to_csr.dpe.fcr.idle.next`、`to_csr.gpio.key1.next`），像多源汇总。
- `soc_csr` 是这对总线的「扇入/扇出枢纽」。

**预期结果**：你会得到一张以 `soc_csr` 为中心、`to_csr`/`from_csr` 为双向上联线的星形图，清晰看到 CSR 是所有模块的共同总线。

**待本地验证**：`to_csr`/`from_csr` 的具体字段（如 `dpe.fcr.pause`）由 PeakRDL 生成的 `csr_pkg.sv` 定义，可在 `3.build/csr_build/generated-files/csr_pkg.sv` 里核对（详见 Unit 3）。

#### 4.3.5 小练习与答案

**练习 1**：为什么改路由表前必须先 `pause`，而不能直接写？
> **答**：因为若数据面正在按旧表处理一个包，CPU 直接改表会让数据面读到「半新半旧」的表项，产生不可预测行为。FCR 的 pause/idle 握手保证改表发生在包边界、数据面完全空闲时。

**练习 2**：`cpu_fifo` 为什么要做 128 位到 32 位的拆分？
> **答**：因为数据面用 128 位 AXIS（为了线速），而 CPU 的数据总线只有 32 位。`cpu_fifo` 把一个 128 位 beat 拆成 4 个 32 位 CSR 寄存器，让 CPU 用多次 32 位读写凑出一个 128 位段。

**练习 3**：如果让你给「桥梁」加一条新功能——CPU 查询 DPE 当前处理的包计数——它会落在 CSR 的哪一类通信里？
> **答**：属于控制级/状态通信（只读状态寄存器）。在 `csr.rdl` 里加一个 `counter` 字段，PeakRDL 会同时生成 RTL（DPE 写 `to_csr.dpe.counter.next`）和 HAL（CPU 读 `csr.dpe.counter.value`），无需新拉线。

## 5. 综合实践

本讲的实践任务是**画一张贯穿控制面与数据面的数据流框图**，把三类通信和两类流量都标出来。这是把本讲三个模块串起来的最佳方式。

**实践目标**：用一张图证明你已经分清了「控制流量 vs 数据流量」和「包级/表级/控制级三类桥梁」。

**操作步骤**：

1. 在纸或绘图工具上，画三个大框：左边「控制面（软件 CPU）」、中间「CSR HAL 桥梁」、右边「数据面（DPE）」、上下各画一个「外网/以太网」。
2. 用**两种颜色的箭头**分别画两类流量：
   - **握手控制流量**（稀疏）：CPU → `cpu_fifo`(from_cpu) → DPE 的 Mux → Demux → MAC → 外网。参考 `1.hw/README.md` 第 124–131 步（peer A 发 Handshake Initiation）。注意：握手包在 DPE 里被 `bypass`（Header Parser 识别出是握手消息后，解封装/加解密都直通）。
   - **用户数据流量**（线速）：外网 → MAC → Rx FIFO → Mux → Header Parser → WG Disassembler → Decryptor → IP Lookup → Encryptor → WG Assembler → Demux → MAC → 外网。参考 `1.hw/README.md` 第 168–190 步。
3. 在「CSR HAL 桥梁」框里标出三个子通道，各配一个真实源码引用：
   - 包级：`cpu_fifo`（`top.sv:225-230`）
   - 表级：`routing_table`/`cryptokey_table` 的 `tdp_ram`（`dpe.sv:105-139`）
   - 控制级：FCR `pause`/`idle`（`dpe.sv:67-69`）
4. 在图上用虚线圈出「当前 PoC 实际跑通的部分」（mux→dummy_switch→demux 明文直通），并注明完整的 WG 解封装/加解密链虽已写好但未编入。

**需要观察的现象**：

- 握手流量**两次**穿越桥梁（CPU→DPE 发出、DPE→CPU 接收处理），因为它要在控制面被 WireGuard Agent 处理。
- 用户数据流量**不进入**控制面，全程在数据面右侧闭环——这正是「软件不碰批量数据」的体现。
- 两类流量在 DPE 入口共用同一条 mux→pipeline 骨架，靠 Header Parser 提取的元数据（消息类型）决定走 bypass 还是走完整加密链。

**预期结果**：一张图同时回答了三个问题——控制面干什么（握手+管表）、数据面干什么（线速加解密转发）、两者怎么连（CSR HAL 的包/表/控制三通道）。如果这张图能不查文档画出来，本讲就达标了。

**待本地验证**：图里 WG 处理链的完整版本目前是「已实现未上线」，实际 PoC 行为以 `dpe_dummy_switch` 直通为准。

## 6. 本讲小结

- wireguard-fpga 采用经典的**两层架构**：控制面（软 CPU picoRV32 上的 C 固件）管握手与路由，数据面（纯 RTL 的 DPE）管线速加解密转发。两者分离的依据是「软件灵活但慢、硬件快但死板」。
- 系统里有两类流量：**握手控制流量**（稀疏，几分钟一次，必须进控制面被 WireGuard Agent 处理）和**用户数据流量**（线速 4Gbps/6Mpps，全程在数据面闭环，不进 CPU）。
- 两个面之间**唯一的桥梁**是 CSR HAL：一份 `csr.rdl` 经 PeakRDL 自动生成硬件 RTL 和软件 HAL，永远对得上。
- 这座桥承担三类通信——**包级**（`cpu_fifo` 把 128 位 AXIS 拆成 32 位 CSR）、**表级**（`routing_table`/`cryptokey_table` 用 `tdp_ram` 双口 RAM）、**控制级**（FCR `pause`/`idle` 做原子更新）。
- 在 `top.sv` 里，`to_csr`/`from_csr` 这对总线被 `soc_csr`、`cpu_fifo`、`dpe`（及 GPIO/UART）共享，是眼见为实的「公共桥梁」。
- **Phase1 PoC 现状**：完整的 WG 解封装/加解密/路由查找链源码已写好，但 `top.filelist` 编入的是直通的 `dpe_dummy_switch`，当前 bitstream 为明文直通 + 软件桥接。

## 7. 下一步学习建议

本讲建立的是「两层 + 一桥」的心智模型，后续讲义会逐一钻进每个细节：

- **u2-l2（top.sv 顶层模块）**：把本讲在 `top.sv` 里追踪的实例化关系讲全，看清 `clk_rst_gen → CPU 子系统 → DPE → ethernet_mac/phy` 的完整层次。
- **u2-l3（时钟复位与三个时钟域）**：解释为什么控制面是 80MHz/32bit、数据面接入是 125MHz/8bit、流水线是 80MHz/128bit，以及跨域 FIFO 的必要性。
- **u2-l4（SoC 互联 fabric）**：钻进 `soc_fabric`，看 CPU 主口如何译码到 uart/dmem/csr 三个从口。
- **Unit 3（CSR——软硬件的唯一桥梁）**：本讲只是点出 CSR 是桥；Unit 3 用 4 篇讲义彻底讲透 SystemRDL 规格、PeakRDL 生成、`cpu_fifo` 的 128↔32 拆分、FCR 原子更新——是理解全系统的钥匙，强烈建议接着读 u3-l1。

建议你现在就打开 `1.hw/top.sv`，对照本讲第 4.3 节把 `to_csr`/`from_csr` 的三处连接亲手标一遍，再进入 u2-l2。
