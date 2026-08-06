# 软件控制流：收发包与表更新

## 1. 本讲目标

本讲把 Unit 6 前三讲（软件架构 u6-l1、加密原语 u6-l2、网络栈与 CLI u6-l3）串成一条完整的**软件控制流主线**。读完本讲，你应当能够：

1. 讲清 CPU 经 `cpu_fifo` 收发一个 16 字节段的「10 步」流程，并能把它对应到 `ethernet.c` 里真实运行的循环。
2. 说明控制面为什么**只处理 WireGuard 握手报文、绝不参与线速用户数据转发**，并理解握手 → 派生传输密钥 → 部署到数据面表这条控制链的设计意图。
3. 看懂 CPU 在线速转发期间用 **FCR（流控寄存器）的 `pause`/`idle` 握手**做路由表/密钥表原子更新的完整时序，并写出对应的 HAL 调用序列。

> 本讲依赖 u6-l3（网络栈与 CLI）与 u3-l4（FCR 流控寄存器与原子更新）。建议先回顾这两讲的结论再继续。

## 2. 前置知识

- **控制面 vs 数据面（u2-l1）**：片上软 CPU（picoRV32）跑控制面，做低频但复杂的握手；纯 RTL 的 DPE（Data Plane Engine）跑数据面，做线速加解密转发。两者唯一桥梁是 CSR HAL。
- **cpu_fifo（u3-l3）**：把数据面 128 位 AXIS 链路对接到 CPU 的 32 位 CSR。每个 128 位 beat 被拆成 4 个 32 位 data 寄存器，TKEEP/TLAST/TUSER 并入 control 寄存器；AXIS 握手用 SystemRDL 的 `singlepulse` 触发位产生干净的单拍 TVALID/TREADY。命名陷阱：rx/tx 是**相对 DPE** 而言（rx = CPU→DPE 即 CPU 写；tx = DPE→CPU 即 CPU 读）。
- **FCR 原子更新（u3-l4）**：改路由表/密钥表前必须暂停 DPE。不能用 AXIS 的 `TREADY` stall（会撕裂在飞包），改用 FCR 的 `pause`（CPU 写、硬件读）+ `idle`（硬件写、CPU 读）做请求—应答握手，在**包边界**生效。
- **external 表（u3-l1、u4-l6）**：`routing_table` 与 `cryptokey_table` 在 SystemRDL 里标为 `external regfile`，PeakRDL 只生成地址译码与 `req/ack` 握手外壳，存储体由手写 `tdp_ram`（真双口 RAM）提供，A 口接 CSR、B 口留给数据面查表。

**一个必须先说清的现状（Phase1 PoC）**：本讲要讲的「收发包流程」和「FCR 表更新」两块**在当前 HEAD 已完整实现并随 bitstream 上板运行**；但「WireGuard 握手处理」这一块，源码里**只有 README 的概念架构和主循环里的一个 UDP 占位分支**，真正的 Noise 握手状态机尚未接入。本讲会如实标注每一块的「已实现 / 待接入」状态，绝不把设计意图说成已跑通的代码。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到什么 |
| --- | --- | --- |
| [2.sw/README.md](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/README.md) | 软件架构与控制流理论说明 | 收发包 10 步流程、170 Mbps 估算、FCR 8 步原子更新、WireGuard Agent 概念组件 |
| [2.sw/app/main.cpp](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp) | 固件主程序：CLI + 收发包主循环 + 表配置 | 主循环 `while(1)`、`config_routes`/`config_cryptokeys` 的 FCR+HAL 写表序列 |
| [2.sw/app/ethernet.c](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/ethernet.c) | 128 位 AXIS ↔ 32 位 CSR 收发包实现 | `eth_send_packet` / `eth_receive_packet`——10 步流程的真实落地 |
| [2.sw/app/ethernet.h](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/ethernet.h) | 包结构 `eth_raw_packet_t` 与 DPE 地址常量 | `dst/src/bypass` 元数据字段、`DPE_ADDR_*` 编码 |
| [2.sw/app/wireguard_libs.cpp](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/wireguard_libs.cpp) | C++ 标准库替代（裸机 malloc/new/delete） | 证明当前 HEAD **无** WireGuard Agent / Noise 握手代码 |
| [3.build/csr_build/csr.rdl](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl) | CSR 单一真源 | `cpu_fifo`（含 singlepulse 触发）、`dpe.fcr`、两条 external 表 |
| [1.hw/ip.dpe/dpe_multiplexer.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_multiplexer.sv) | DPE 入口轮询复用器 | `pause`/`is_idle` 端口与状态机——FCR 握手的硬件侧 |

---

## 4. 核心概念与源码讲解

### 4.1 收发包流程：CPU 经 cpu_fifo 的 10 步循环

#### 4.1.1 概念说明

控制面和数据面之间只有一条「包级」通道：`cpu_fifo`。它把数据面的 128 位 AXIS 链路对接到 CPU 的 32 位 CSR 总线。CPU 不能直接驱动 AXIS 的时序级握手（它和 AXIS 不在同一时钟节拍上对齐），于是设计上把每个 128 位 beat 拆成 CPU 能逐个读写的 32 位寄存器，并用 `singlepulse` 触发位产生单拍 TVALID/TREADY。

收发包一律以 **16 字节段（一个 beat）**为单位推进：CPU 先把 4 个 32 位 data 写好，再写 control（含 tkeep/tlast/tuser），最后写 trigger 触发一次 AXIS 握手；若不是包尾就回到开头处理下一个 16 字节段。这就是 README 所说的「10 步流程」——它是一个**每个 16 字节段重复一次**的小循环，而不是整包一次性完成。

#### 4.1.2 核心流程

**发送（CPU → Rx FIFO → DPE）**，每 16 字节段重复：

```
1. 读 cpu_fifo.rx.status.tready；为 1 才继续（drop-on-full，恒为 1）
2. 写 cpu_fifo.rx.data_31_0.tdata
3. 写 cpu_fifo.rx.data_63_32.tdata
4. 写 cpu_fifo.rx.data_95_64.tdata
5. 写 cpu_fifo.rx.data_127_96.tdata
6. 写 cpu_fifo.rx.control.tkeep（末 beat 才不全 1）
7. 写 cpu_fifo.rx.control.tlast（仅末 beat 为 1）
8. 写 cpu_fifo.rx.control.tuser_bypass_all
9. 写 cpu_fifo.rx.control.tuser_dst（1-4=eth, 7=广播）
10. 写 cpu_fifo.rx.trigger.tvalid = 1   ← singlepulse，写 1 后下一拍自动清零
11. 若 tlast==0，回到步骤 1
```

**接收（DPE → Tx FIFO → CPU）**结构对称，区别在数据方向与流控方向：读 `tx.status.tvalid`（store-and-forward，无包时为 0 就停），逐 beat 读 4 个 data 与 control，靠 `tkeep` 数低位连续 1 反推真实字节数，写 `trigger.tready=1` 完成本 beat 握手。

整段吞吐可估：每步约 1.5 条指令、每条 4 拍、10 步处理 128 位 →

\[
\text{吞吐} \approx \frac{80\,\text{MHz} \times 128\,\text{bit}}{10 \times 1.5 \times 4} \approx 170\,\text{Mbps}
\]

170 Mbps 远低于 1 Gbps 数据面，所以这个接口**只够承载稀疏的握手报文**——这正是软硬件分工成立的数学依据。

#### 4.1.3 源码精读

README 用文字列出了上述 10 步流程，并明确「rx/tx 是相对 DPE 而言」「字段须 8 位对齐避免 RMW」「singlepulse 触发 TVALID/TREADY」三件事，最后给出 170 Mbps 估算：

- [2.sw/README.md:222-253](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/README.md#L222-L253) ——「Software Control Flow / Sending and receiving packets」整节，含发送与接收两套步骤及吞吐公式。

`ethernet.c` 把这套步骤落成真实代码。**发送**时把 dst/src/bypass 这组 control **先于循环写一次**，循环体内只重复 4 个 data + tkeep/tlast + trigger：

- [2.sw/app/ethernet.c:52-86](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/ethernet.c#L52-L86) ——`eth_send_packet`：先查 `status.tready()`，写 tuser，再 `while(1)` 逐 beat 写 4 个 data、按 `i<len` 决定 `tkeep/tlast`，写 `trigger.tvalid(1)`；末 beat 用 `0xFFFF >> empty` 收紧字节使能。

**接收**对称地查 `tx.status.tvalid()`，逐 beat 读 4 个 data 进 `packet->payload`，命中 `tlast` 后用「数 tkeep 低位连续 1」算出真实长度：

- [2.sw/app/ethernet.c:95-128](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/ethernet.c#L95-L128) ——`eth_receive_packet`：注意 `len` 在非末 beat 加 16，末 beat 用 `while(keep & 1){ len++; keep>>=1; }` 反推字节数，并受 `ETH_MAX_FRAME_LENGTH` 保护。

收发所用的包结构与 DPE 地址编码定义在头文件里，是上层 `main.cpp` 与下层 `ethernet.c` 的公共契约：

- [2.sw/app/ethernet.h:17-33](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/ethernet.h#L17-L33) ——`eth_raw_packet_t{dst,src,bypass_stage,bypass_all,len,payload[1536]}` 与 `DPE_ADDR_CPU=0`、`DPE_ADDR_ETH_1..4=1..4`、`DPE_ADDR_MCAST_13/24=5/6`、`DPE_ADDR_BCAST=7`。

这些寄存器的「形状」来自单一真源 `csr.rdl`。注意 `trigger.tvalid` 标了 `singlepulse = true`——这正是写 1 后硬件自动清零、产生单拍握手的关键（u3-l3）：

- [3.build/csr_build/csr.rdl:157-167](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl#L157-L167) ——`cpu_fifo.rx.trigger.tvalid` 字段，`desc` 写明「single pulse trigger」，`singlepulse = true`。
- [3.build/csr_build/csr.rdl:282-291](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl#L282-L291) ——接收侧对称的 `cpu_fifo.tx.trigger.tready`，同样 `singlepulse = true`。

最后看 `main.cpp` 的主循环如何把收发包接成一条「收 → 解析 → 应答」的协作式管线：

- [2.sw/app/main.cpp:817-881](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L817-L881) ——`while(1)`：`eth_receive_packet` 收包 → `net_parse_packet_header` 分发到 ARP/ICMP/UDP → 对 ARP/ICMP 构造应答并 `eth_send_packet` 回送。收不到包则熄灯，继续查 UART 的 CLI 命令。**单线程协作式、无中断、无 RTOS。**

> 注意 870-878 行的 `eth1↔eth2` 直通：收到 eth1 的包改成发往 eth2（反之亦然）并置 `bypass_all=1` 重发。这是 Phase1 PoC 的「软件桥接明文直通」——和硬件侧的 `dpe_dummy_switch` 对应（见 u4-l2、u4-l5）。

#### 4.1.4 代码实践

**目标**：验证「数 tkeep 低位连续 1」与「`0xFFFF >> empty` 收紧」是一对互逆运算。

**操作步骤**：

1. 打开 `2.sw/app/ethernet.c`，对照 `eth_send_packet` 末 beat 的 `empty = i - packet->len; tkeep = 0xFFFF >> empty`（76-77 行）与 `eth_receive_packet` 的 `while(keep & 1){ len++; keep >>= 1; }`（113-116 行）。
2. 取一个具体例子：假设一个 20 字节的包。它占 2 个 beat，末 beat 只有 4 个有效字节。
   - 发送侧：`i=32, len=20` → `empty=12` → `tkeep = 0xFFFF >> 12 = 0x000F`（低 4 位为 1）。
   - 接收侧：收到 `keep=0x000F` → 循环数 4 次 → `len` 由 16 涨到 20。

**需要观察的现象**：发送侧用 `empty` 把多余字节「砍掉」，接收侧用「数 1」把字节数「还原」。

**预期结果**：两边的 `len` 应当严格相等（20），证明收发对 tkeep 的处理互逆、不丢字节。如果你改动 `0xFFFF >> empty` 的移位方向或漏掉 `>>=1`，接收侧算出的长度就会错。

> 不需要上板即可完成这个纯推理练习；如要运行验证，需在主机 gcc 或 RV32 上加一段打印 `len` 的测试（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 CPU 收发包必须以 16 字节段为单位，而不能逐字节？

**参考答案**：因为 `cpu_fifo` 把数据面 128 位（16 字节）AXIS beat 拆成 4 个 32 位 CSR 寄存器，硬件侧的最小搬运粒度就是一个 beat。CPU 逐字节读写会触发不必要的读改写（RMW）且对不上 AXIS 的 beat 边界，所以软件也按 16 字节段对齐推进。

**练习 2**：README 列出发送流程有 11 个编号步骤，为什么又说「10 步」？

**参考答案**：「10 步」指**每个 16 字节段重复的核心动作**（写 4 个 data + control 若干字段 + trigger，约 10 次寄存器访问）；第 11 步「若非末 beat 回到步骤 1」是循环控制本身，不计入单段开销。170 Mbps 估算里用的「10 × 1.5 × 4」就是这个单段开销。

---

### 4.2 握手处理：WireGuard Agent 的设计意图与现实

#### 4.2.1 概念说明

WireGuard 用 Noise 协议（具体是 Noise_IKpsk2）做握手：双方交换临时公钥（经 u6-l2 的 curve25519/X25519 算 ECDH），再用 BLAKE2s/HKDF 派生出一对**传输密钥**（send/recv）和**发送/接收计数器**。握手报文走 UDP（WireGuard 固定用 UDP），是稀疏的低频流量。

按设计意图，控制面应当：

1. 在主循环里识别 UDP 报文（`net_parse_packet_header` 返回 `NET_PROTO_UDP`）。
2. 交给 **WireGuard Agent** 跑 Noise 状态机，完成握手、派生传输密钥。
3. 由 **Routing DB Updater** 把派生出的密钥经 HAL/CSR 写进数据面的 `cryptokey_table`，让线速加解密流水线（Unit 5）用新密钥转发后续用户数据。

关键分工：**控制面只处理握手，绝不参与线速用户数据转发**。用户数据全程在 DPE 的硬件流水线里闭环，CPU 既看不到也碰不着——这是 170 Mbps 软件接口不可能跑满 1G 数据面的必然结果。

#### 4.2.2 核心流程（设计意图）

```
       ┌─────────────┐  UDP(握手)   ┌──────────────────┐
eth ─→ │ cpu_fifo.tx │ ──────────→ │  主循环识别 UDP   │
       └─────────────┘              └────────┬─────────┘
                                             │
                                   ┌─────────▼─────────┐
                                   │ WireGuard Agent    │  curve25519 + blake2s
                                   │ Noise_IK 握手      │  + hkdf + chacha20poly1305
                                   └─────────┬─────────┘
                                             │ 派生 send/recv key + 计数器
                                   ┌─────────▼─────────┐
                                   │ Routing DB Updater│  FCR pause/idle 原子更新
                                   │ 写 cryptokey_table │  （见 4.3）
                                   └─────────┬─────────┘
                                             │
                                  数据面用新密钥线速转发用户数据（不经 CPU）
```

#### 4.2.3 源码精读（如实标注现状）

README 的概念架构图列出了 WireGuard Agent 及其周边组件，明确「Routing DB Updater 负责维护 cryptokey 路由表并通过 HAL/CSR 部署到数据面」「HAL/CSR Driver 是 DPE 寄存器读写抽象」：

- [2.sw/README.md:10-23](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/README.md#L10-L23) ——WireGuard Agent、Curve25519、ChaCha20-Poly1305、BLAKE2s、HKDF、Routing DB Updater、HAL/CSR Driver 等组件的概念职责。

**但是**，主循环里 UDP 分支当前只是个**占位日志**，没有任何握手处理：

- [2.sw/app/main.cpp:861-867](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L861-L867) ——`case NET_PROTO_UDP:` 仅在 `debug_enabled` 时打印 `<< NET_PROTO_UDP: <len>`，**没有调用任何 WireGuard Agent**。这就是握手处理应当挂载、却尚未接入的位置。

协议识别层目前只做到「认出 IP 协议号 17 = UDP」：

- [2.sw/app/network.c:123-136](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/network.c#L123-L136) ——`net_parse_packet_header`：按 ethertype 区分 ARP / IPv4，IPv4 内再按 protocol 区分 ICMP(1)/UDP(17)。WireGuard 握手会在 `return NET_PROTO_UDP`（132 行）这一支进入主循环。

再看「WireGuard C++ 标准库」这个文件，名字容易让人以为里面有握手实现，实际只有裸机堆分配：

- [2.sw/app/wireguard_libs.cpp:45-66](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/wireguard_libs.cpp#L45-L66) ——`#ifndef VPROC` 分支下只提供 `heap_memory[]`、`malloc()`、`operator new/delete`，超 32 KiB 触发 `ebreak`。**没有 Noise 状态机、没有 KMM（密钥管理报文）解析、没有握手收发函数。**

**结论（务必记住）**：握手所需的密码**原语**已齐备并在 warmup 里单独通过测试（u6-l2）：`curve25519.c`、`blake2s.c`、`hkdf.c`、`chacha20poly1305.c` 都在 `2.sw/app/` 下。但把它们编排成 Noise 握手状态机、并在主循环 UDP 分支里驱动的「胶水代码」**在当前 HEAD 不存在**。`main.cpp` 顶部那些 `test_*` 函数（59-223 行）只证明各原语能独立跑通，并不构成握手。因此「握手响应处理完成后写 cryptokey 表」这条链的**起点（握手）待接入，终点（FCR 写表）已就绪**——这正是 4.3 节和综合实践的落点。

#### 4.2.4 代码实践

**目标**：定位「握手应当挂在哪里」，并确认它当前确实缺失。

**操作步骤**：

1. 读 `2.sw/app/main.cpp` 的 `case NET_PROTO_UDP`（861-867 行），确认它只打印日志。
2. 对比同文件 `case NET_PROTO_ARP`（831-845 行）和 `case NET_PROTO_ICMP`（846-860 行）——它们都调用了 `net_process_*` 并 `eth_send_packet` 应答；而 UDP 分支没有对应处理。
3. 在仓库里搜索是否存在握手相关函数：

   ```
   grep -rn "noise\|handshake\|mac1\|mac2\|sender" 2.sw/app/
   ```

**需要观察的现象**：ARP/ICMP 有完整的「处理 + 回送」；UDP 分支为空；第 3 步搜索在 `2.sw/app/` 下应**几乎无命中**（仅注释或变量名偶发出现）。

**预期结果**：证实 Noise 握手状态机尚未接入，UDP 分支是预留的挂载点。

#### 4.2.5 小练习与答案

**练习 1**：既然握手代码还没写，为什么本讲仍要讲「握手处理」？

**参考答案**：因为它是控制流的**逻辑中段**——上接收发包（4.1），下接表更新（4.3）。理解设计意图（UDP → Noise 握手 → 派生密钥 → 写 cryptokey_table）才能明白 4.3 那段 FCR 写表代码「为什么而写」「将来被谁驱动」。讲清「起点待接入、终点已就绪」比假装整条链已跑通更诚实、也更有学习价值。

**练习 2**：为什么 WireGuard 用户数据包（已加密的运输报文）不该进 CPU？

**参考答案**：用户数据是线速流量，而 CPU 经 cpu_fifo 的接口吞吐上限约 170 Mbps，远低于 1G 数据面；且每包都进 CPU 会引入不可接受的延迟和抖动。设计上让加密用户数据全程在 DPE 硬件流水线里解密/路由/加密闭环，CPU 只处理稀疏的握手 UDP 报文——这正是软硬件分区的根本依据。

---

### 4.3 表更新与 FCR：在线速转发期间安全换表

#### 4.3.1 概念说明

握手派生出新密钥后，控制面要把它们写进数据面的 `cryptokey_table`（同理，CLI 改路由要写 `routing_table`）。但这两张表是 DPE 在线速转发时**实时查表**用的——如果 CPU 改了一半就被数据面读到，就会用「半成品表项」转发或加解密，行为不可预测。所以改表必须**原子**：要么数据面看到旧表，要么看到完整的新表，不能看到中间态。

如 u3-l4 所述，本项目放弃了昂贵的 WBR（影子寄存器，每比特 3 个触发器），改用 **FCR（Flow Control Register）** 做请求—应答握手：

- `pause`（CPU 写、硬件读）：CPU 置 1，请求 DPE 暂停。
- `idle`（硬件写、CPU 读）：DPE 真正停下后置 1，回报静止。

为什么不能用 AXIS 的 `TREADY` stall？因为 stall 是**逐拍局部反压**，会撕裂已经在流水线里的包；而 FCR 在**包边界**生效——当前正在处理的包必送完、各级 datapath 必排空，DPE 才进 IDLE 并抬高 `idle`。

#### 4.3.2 核心流程（FCR 8 步原子更新）

```
1. CPU 写 csr.dpe.fcr.pause = 1            ← 请求暂停
2. mux 收到 pause，服务完当前队列后进 PAUSED
3. 各级处理完在飞包、清空 datapath、关 TVALID → 进 IDLE
4. CPU 轮询 csr.dpe.fcr.idle，直到 == 1     ← 确认全链静止
5. CPU 多拍写入新表项（routing_table / cryptokey_table）
6. CPU 写 csr.dpe.fcr.pause = 0            ← 解除暂停
7. mux 恢复轮询，从下一队列开始收包
8. 数据面逐级回到 active，用新表转发后续包
```

因第 4 步等待的拍数随在飞包长度变化，**必须用 `while(!idle())` 轮询**，不能写死延时。

#### 4.3.3 源码精读

先看 FCR 的规格源头——两个字段，读写方向正好构成请求—应答：

- [3.build/csr_build/csr.rdl:507-525](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl#L507-L525) ——`dpe.fcr`：`pause[1:1]` 为 `sw=rw; hw=r`（CPU 写、硬件读），`idle[0:0]` 为 `sw=r; hw=w`（硬件写、CPU 读）。

再看硬件侧 `dpe_multiplexer` 如何兑现「包边界暂停」与「真静止才抬 idle」：

- [1.hw/ip.dpe/dpe_multiplexer.sv:44-45](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_multiplexer.sv#L44-L45) ——`input logic pause` / `output logic is_idle` 端口，即 FCR 两根线接到 mux。
- [1.hw/ip.dpe/dpe_multiplexer.sv:56-63](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_multiplexer.sv#L56-L63) ——状态机：`IDLE` + 每路一对 `Rk`（探头）/`Sk`（发送）态。`pause` 在各态都优先跳回 `IDLE`。
- [1.hw/ip.dpe/dpe_multiplexer.sv:83-89](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_multiplexer.sv#L83-L89) ——`IDLE` 态：`if (!pause) next_state = R0`（不暂停才开始轮询）；`R0` 态：`else if (pause) next_state = IDLE`（暂停优先）。注意 `Sk` 发送态会**先把当前包发到 tlast** 才回 IDLE（见 u4-l2），这就是「包边界暂停」。
- [1.hw/ip.dpe/dpe_multiplexer.sv:155-241](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_multiplexer.sv#L155-L241) ——`is_idle` 输出逻辑：默认 0，仅 `IDLE` 态且输出排空（`!to_dpe.tvalid`）时才为 1（171-172、241 行）。这保证 `idle==1` 时 mux 既不在发包、也没有待发数据。

软件侧这套握手被浓缩成**三行固定句式**：`pause(1)` → `while(!idle())` → …写表… → `pause(0)`。它在 `main.cpp` 里反复出现：

- 读表（只读也要暂停，保证读到一致快照）：[2.sw/app/main.cpp:394-404](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L394-L404) `show_routes`，`pause(1)` → `while(!idle())` → 遍历 64 项 → `pause(0)`。
- 写路由表：[2.sw/app/main.cpp:406-461](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L406-L461) `config_routes`，FCR 包住问卷与写表（409-410 行 `pause(1)/while(!idle())`，451-454 行写 4 字段，460 行 `pause(0)`）。
- 写密钥表：[2.sw/app/main.cpp:540-778](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L540-L778) `config_cryptokeys`，FCR 包住整段（543-544 行 `pause(1)/while(!idle())`，777 行 `pause(0)`）。

`config_cryptokeys` 里真正「部署一条密钥」的 HAL 写序列在第 737-762 行——这一段就是将来 WireGuard Agent 握手完成后要复用的**核心调用序列**：

- [2.sw/app/main.cpp:737-762](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L737-L762) ——逐字段写一条 cryptokey 表项：本地/远端 MAC、IP、port、id，加密密钥 8 个 32 位字（`encrypt_key_255_224`…`encrypt_key_31_0`），解密密钥 8 个字（`decrypt_key_*`）。每行形如 `csr->cryptokey_table->entry[i]->encrypt_key_255_224->key(val);`，是 u3-l2「层级指针访问 API」的典型用法。

可选的计数器清零也在 FCR 保护区内：

- [2.sw/app/main.cpp:764-771](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L764-L771) ——握手产生新密钥后通常要把 `send_cnt`/`recv_cnt` 清零（nonce 不复用，见 u4-l5 的 `send_cnt+1` 回写）。

README 把这套机制总结为 8 步并配图（含「已进入 DPE 的包必须按入队时规则处理完」这一反 stall 论证）：

- [2.sw/README.md:255-270](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/README.md#L255-L270) ——「Updating DPE registers and tables」整节，含 WBR 成本论证与 8 步流程。

#### 4.3.4 代码实践

**目标**：把「CLI 问卷驱动」的 `config_cryptokeys` 改写成「握手结果驱动」的密钥部署函数——即练习任务要求的「写出握手响应处理完成后，CPU 经 HAL 更新 cryptokey 表（含 FCR pause/idle）的调用序列」。

**操作步骤**：

1. 假设 WireGuard Agent 握手完成后，得到一个结构体（示例代码，非项目原有）：

   ```c
   // 示例代码：握手产物（项目里尚无此结构，4.2 节已说明）
   typedef struct {
       uint8_t  peer_idx;        // 写入 cryptokey_table->entry[peer_idx]
       uint8_t  local_mac[6];
       uint8_t  remote_mac[6];
       uint32_t local_ip, remote_ip;
       uint16_t local_port, remote_port;
       uint32_t local_id, remote_id;
       uint8_t  encrypt_key[32]; // Noise 派生的发送密钥
       uint8_t  decrypt_key[32]; // Noise 派生的接收密钥
   } wg_handshake_result_t;
   ```

2. 仿照 `config_cryptokeys`（main.cpp 540-778 行）写出部署函数骨架，把「问卷读入」替换成「从 `wg_handshake_result_t` 取值」，**FCR 三件套与 HAL 写序列保持不变**：

   ```c
   // 示例代码：待 WireGuard Agent 接入后实现
   void wg_deploy_cryptokey(volatile csr_vp_t* csr,
                            const wg_handshake_result_t* r) {
       // —— FCR 8 步之 1-4：原子区间开启 ——
       csr->dpe->fcr->pause(1);
       while (!csr->dpe->fcr->idle());

       uint32_t i = r->peer_idx;
       // 本地 / 远端身份（与 main.cpp:737-746 同形）
       csr->cryptokey_table->entry[i]->local_mac_47_32->mac(MAC_HI(r->local_mac));
       csr->cryptokey_table->entry[i]->local_mac_31_0 ->mac(MAC_LO(r->local_mac));
       csr->cryptokey_table->entry[i]->local_ip      ->ip(r->local_ip);
       csr->cryptokey_table->entry[i]->local_port    ->port(r->local_port);
       csr->cryptokey_table->entry[i]->local_id      ->id(r->local_id);
       csr->cryptokey_table->entry[i]->remote_mac_47_32->mac(MAC_HI(r->remote_mac));
       csr->cryptokey_table->entry[i]->remote_mac_31_0 ->mac(MAC_LO(r->remote_mac));
       csr->cryptokey_table->entry[i]->remote_ip     ->ip(r->remote_ip);
       csr->cryptokey_table->entry[i]->remote_port   ->port(r->remote_port);
       csr->cryptokey_table->entry[i]->remote_id     ->id(r->remote_id);

       // 加密密钥 8 字（与 main.cpp:747-754 同形）
       csr->cryptokey_table->entry[i]->encrypt_key_255_224->key(U32(r->encrypt_key, 0));
       /* ...encrypt_key_223_192 … encrypt_key_31_0… */

       // 解密密钥 8 字（与 main.cpp:755-762 同形）
       csr->cryptokey_table->entry[i]->decrypt_key_255_224->key(U32(r->decrypt_key, 0));
       /* ...decrypt_key_223_192 … decrypt_key_31_0… */

       // 新会话 → nonce 计数器清零（与 main.cpp:764-771 同形）
       csr->cryptokey_table->entry[i]->send_cnt_63_32->cnt(0);
       csr->cryptokey_table->entry[i]->send_cnt_31_0 ->cnt(0);
       csr->cryptokey_table->entry[i]->recv_cnt_63_32->cnt(0);
       csr->cryptokey_table->entry[i]->recv_cnt_31_0 ->cnt(0);

       // —— FCR 8 步之 6：原子区间关闭 ——
       csr->dpe->fcr->pause(0);
   }
   ```

3. 对照 [main.cpp:543-544](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L543-L544) 与 [main.cpp:777](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L777) 核对 FCR 句式是否一致。

**需要观察的现象**：FCR 的 `pause(1)/while(!idle())` … `pause(0)` 把整段写表「夹」在中间；所有 HAL 写都在 `idle==1` 之后、`pause(0)` 之前发生。

**预期结果**：你写出的函数与 `config_cryptokeys` 的 FCR 句式逐行一致，差别仅在数据来源（问卷 vs 握手产物）。这正是「起点（握手）待接入，终点（FCR 写表）已就绪」的体现——接入 Agent 时**写表代码可直接复用**。

> 因当前 HEAD 无握手代码，本实践为「源码阅读 + 改写型」设计，不要求上板运行（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`show_routes` 只是读表，为什么也要 `pause(1)/while(!idle())`？

**参考答案**：路由表有 64 项，CPU 逐项读需要很多拍。若不暂停，读到一半时数据面可能正在改写某项（或反之），会得到不一致的快照。用 FCR 把读区间也原子化，保证读到的是某一时刻的完整表。

**练习 2**：如果把 `while (!csr->dpe->fcr->idle())` 换成 `delay_ms(1)`（固定延时）会怎样？

**参考答案**：暂停到静止的拍数取决于当时在飞包的长度，是不确定的。固定延时要么太短（还没静止就开始写表 → 数据面读到半成品），要么太长（浪费吞吐）。必须用轮询确认 `idle==1`，因为 `idle` 是硬件对「各级已排空」的权威回报，而非时间估计。

**练习 3**：为什么 `pause` 是「包边界暂停」而不是「逐拍 stall」？

**参考答案**：一个已经进入 DPE 的包必须按它入队时生效的表项规则处理完，逐拍 stall（拉低 TREADY）会在包中途冻住、撕裂数据；FCR 让 mux 先把当前包发到 `tlast`、各级 datapath 排空，再进 IDLE，保证「换表前后」的包各自用各自时代的完整规则。

---

## 5. 综合实践

**任务**：画出从「eth1 收到一个 UDP 包」到「数据面用新密钥线速转发」的完整软件控制流时序，并标注每一块的「已实现 / 待接入」状态。

**要求**：

1. 用一张时序图（或编号步骤）串起 4.1 → 4.2 → 4.3 三块，至少包含：
   - `eth_receive_packet` 经 cpu_fifo.tx 的 10 步收包（4.1，**已实现**）；
   - `net_parse_packet_header` 识别出 `NET_PROTO_UDP`（4.2，**已实现**）；
   - WireGuard Agent 跑 Noise 握手、派生密钥（4.2，**待接入**）；
   - FCR `pause(1)/while(!idle())` → 写 `cryptokey_table` → `pause(0)`（4.3，**已实现**）；
   - 数据面硬件用新密钥线速转发后续用户数据，**不经 CPU**（u4-l5/u5，**待接入**）。
2. 在图上用两种颜色或标记区分「当前 bitstream 已跑通」与「源码已写好/待接入」。
3. 写一段话解释：为什么这条链的「中段（握手）」缺失，却不影响「两端（收发包、FCR 写表）」已经被验证可用？

**提示**：Phase1 PoC 的 bitstream 实跑的是 `main.cpp:870-878` 的 eth1↔eth2 明文直通 + CLI 配置（含 FCR 写表），即「两端」独立成立；中段握手是把它们连成真加密隧道的最后一块拼图。

> 本实践为源码阅读与设计型，无需上板（待本地验证）。

## 6. 本讲小结

- **收发包（4.1，已实现）**：CPU 经 `cpu_fifo` 以 16 字节段为单位收发，每段约 10 步 CSR 读写——写 4 个 32 位 data、写 control（tkeep/tlast/tuser）、写 `singlepulse` 的 `trigger.tvalid`。`ethernet.c` 的 `eth_send_packet`/`eth_receive_packet` 是其真实落地；接口吞吐约 170 Mbps，只够握手用。
- **握手处理（4.2，待接入）**：设计上 UDP 分支应驱动 WireGuard Agent 跑 Noise 握手、派生传输密钥。密码原语（curve25519/blake2s/hkdf/chacha20poly1305）已齐备并通过 warmup 测试，但主循环 UDP 分支当前只是占位日志，Noise 状态机尚未接入——**起点待接入**。
- **表更新与 FCR（4.3，已实现）**：改路由表/密钥表用 FCR 的 `pause(1)/while(!idle())…pause(0)` 三件套做原子更新，在包边界暂停、各级排空后才换表，避免数据面读到半成品。`config_cryptokeys`（main.cpp:737-762）就是握手完成后要复用的 HAL 写序列——**终点已就绪**。
- **主线结论**：控制面只处理握手、不参与线速转发；用户数据全程在 DPE 硬件流水线闭环。当前 HEAD 处于 Phase1 PoC，控制流链「两端已通、中段待接」，bitstream 实跑明文直通 + CLI 配置。

## 7. 下一步学习建议

- **向数据面延伸**：读 u4-l5（WireGuard 封装/解封装与加解密数据流）与 u4-l6（路由表与密钥表的 tdp_ram 实现），理解 4.3 写进 `cryptokey_table` 的密钥如何被 DPE 的 B 口读出、喂给加密流水线。
- **向加密核延伸**：读 Unit 5（ChaCha20-Poly1305 硬件），看握手派生的 256 位 key / 96 位 nonce 在硬件里如何产生密钥流与认证 tag。
- **向验证延伸**：读 u7-l1/u7-l2（仿真测试台与 VProc 协同仿真），学习如何在不依赖真实握手的情况下用 VProc 的 C++ 入口 `VUserMain0` 直接驱动 CSR、把 4.3 的 FCR 写表序列在仿真里跑通验证。
- **动手挑战**：在 `2.sw/app/main.cpp` 的 `NET_PROTO_UDP` 分支里，调用现有 `chacha20poly1305_*` 原语做一个最小「收一个 UDP 包 → 用硬编码密钥加密 → 回送」的演示，体会 4.2 缺失的中段应如何编写，以及它如何复用 4.1 的收发包与 4.3 的 FCR 写表。
