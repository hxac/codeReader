# 网络栈与 CLI

## 1. 本讲目标

本讲聚焦运行在 picoRV32 软核上的「裸机网络栈」与「命令行接口（CLI）」。读完本讲，你应当能够：

- 看懂控制面固件如何不依赖任何操作系统、任何网络库，手写实现以太网帧的收发、ARP/ICMP/IP 解析与 Internet 校验和。
- 追踪一个从网口进入、被 CPU 处理、再发回网口的 ICMP Echo 请求在软件里的完整路径。
- 理解为什么这个项目要自带一份 `string_bare.c`（无 libc 字符串库），而不是直接用标准 C 库。
- 讲清在串口敲下 `config routes` 后，输入的「目的 IP / 掩码 / peer / 出口」是如何一步步落到硬件路由表（双口 RAM）里的，以及为什么这一步要先做 FCR 的 pause/idle 握手。

本讲承接 u6-l1（bare-metal 启动与内存映射）与 u6-l2（软件加密原语库），把目光从「启动与密码学」转向「网络收发与人机配置」。

## 2. 前置知识

在进入源码前，先用最朴素的方式建立几个直觉。

**裸机（bare-metal）是什么意思。** 这颗软核里没有 Linux、没有 lwIP、没有 `printf`、没有 `malloc`。`main()` 一返回，CPU 就在空转兜底。所有「看起来理所当然」的基础设施——字符串比较、内存拷贝、IP 地址解析、网络校验和——都得自己写。本讲的 `string_bare.c` 和 `network.c` 就是这种「自己造轮子」的典型。

**为什么控制面要自己处理 ARP/ICMP。** 数据面 DPE 只会线速转发与加解密，它不会「思考」。但 VPN 节点要能被 `ping` 通、要能回应 `arp` 请求，否则连最基本的连通性测试都做不了。这些低频却需要「按协议字段组织应答」的活，正适合交给灵活的控制面软件。所以 ARP、ICMP 这些「慢协议」在软件里实现，而用户数据流量则全程在 DPE 线速闭环（不进 CPU）。这正是 u2-l1 讲过的「控制面与数据面分离」在协议层面的具体体现。

**Internet 校验和（one's complement checksum）。** IP/ICMP/UDP/TCP 头里那个 16 位校验和，用的是一种很老的算法：把数据当成一串 16 位大端字，求和时把进位「绕回」最低位（end-around carry），最后取反。本讲的 `net_calculate_checksum()` 就是它的手写实现，后面会给出公式。

**CLI 与数据面共用一个主循环。** 这个固件是单线程、协作式的：收包处理与串口命令处理在同一个 `while(1)` 里轮流被检查。没有中断驱动的命令行——你在 `config routes` 里逐项填问卷时，收包是暂停的。这在「配置只在上线初做一次」的场景下完全够用。

承接本讲的关键术语（来自前序讲义）：CSR / HAL（u3）、`cpu_fifo` 的 128↔32 位拆分（u3-l3）、FCR 的 pause/idle 原子更新（u3-l4）、DPE 接口地址 0=CPU/1-4=eth/5-7=组播广播（u4-l1）、external regfile 由 `tdp_ram` 实现（u4-l6）。

## 3. 本讲源码地图

本讲涉及的关键文件，以及各自的作用：

| 文件 | 作用 |
|------|------|
| [2.sw/app/ethernet.c](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/ethernet.c) | 以太网帧与 `cpu_fifo` CSR 之间的底层收发：把 128 位 AXIS beat 装配/拆解成 `eth_raw_packet_t` 结构体 |
| [2.sw/app/ethernet.h](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/ethernet.h) | 定义 `eth_raw_packet_t`、`ETH_MAX_FRAME_LENGTH`、`DPE_ADDR_*` 接口地址常量 |
| [2.sw/app/network.c](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/network.c) | 最小网络协议栈：IP 解析、ARP 应答、ICMP Echo 应答、Internet 校验和 |
| [2.sw/app/network.h](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/network.h) | 各协议头结构体（Ethernet/ARP/IPv4/ICMP）与协议类型枚举 |
| [2.sw/app/string_bare.c](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/string_bare.c) | 无 libc 的字符串/内存工具：`memset/memcpy/memcmp/strlen/strcmp` + 数值解析 |
| [2.sw/app/uart.c](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/uart.c) | UART 字符级收发：`uart_send` / `uart_recv`（带回显、退格、行结束处理） |
| [2.sw/app/main.cpp](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp) | 主循环：收包分发（ARP/ICMP/UDP）、CLI 命令派发、`config routes`/`config cryptokeys` 写硬件表 |
| [6.test/README.md](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/6.test/README.md) | 两节点实验室配置示例：CLI 问卷的完整交互记录 |

---

## 4. 核心概念与源码讲解

### 4.1 以太网收发底层：CPU 与数据面的包级通道

#### 4.1.1 概念说明

控制面 CPU 并没有直接连到网线的 PHY/MAC 上——那是数据面 DPE 的地盘。CPU 与外界交换数据包，走的是 u3-l3 讲过的 `cpu_fifo`：一条把 128 位 AXIS 链路对接到 32 位 CSR 寄存器的「包级」通道。

`ethernet.c` 就站在 `cpu_fifo` 之上，向网络栈提供两个看起来很普通的函数：`eth_send_packet()` 和 `eth_receive_packet()`。它们的输入输出是一个 `eth_raw_packet_t` 结构体——一个带元数据（dst/src/bypass 标志）和最多 1536 字节载荷的「一帧」。底层的 128 位 beat 拆装、`singlepulse` 触发 TVALID/TREADY、字节使能（tkeep）等细节，都被这两个函数封装掉了。

> 命名陷阱（承接 u3-l3）：`cpu_fifo` 的 rx/tx 是相对 **DPE** 而言的。`rx` = CPU→DPE（CPU 往里写，即「发送」），`tx` = DPE→CPU（CPU 从里读，即「接收」）。下面读代码时务必带着这个方向感。

#### 4.1.2 核心流程

`eth_raw_packet_t` 的结构是理解两个函数的前提（[ethernet.h:L17-L24](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/ethernet.h#L17-L24)）：

- `dst`/`src`：3 位的 DPE 接口地址（0=CPU、1-4=eth1-4、5/6 组播、7 广播），对应 AXIS 的 `tuser_dst`/`tuser_src`。
- `bypass_stage`/`bypass_all`：路由旁路标志，对应 `tuser_bypass_stage`/`tuser_bypass_all`。
- `len`：帧的实际字节数。
- `payload[1536]`：帧的原始字节。

**发送（CPU→DPE）流程**（[ethernet.c:L52-L86](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/ethernet.c#L52-L86)）：

1. 先读 `cpu_fifo->rx->status->tready()`，确认 DPE 侧有空间，否则直接返回 0（不阻塞）。
2. 把 `dst/src/bypass_*` 四个元数据写进 `cpu_fifo->rx->control` 的各字段。
3. 以 16 字节为单位循环：每轮把 payload 里 4 个 32 位字写进 `data_31_0/data_63_32/data_95_64/data_127_96` 四个寄存器。
4. 中间 beat：`tkeep=0xFFFF`、`tlast=0`，触发 `tvalid(1)`（singlepulse 产生一拍 TVALID）。
5. 末尾 beat：按 `empty = i - len` 算出末 beat 多读了几个字节，`tkeep = 0xFFFF >> empty` 收紧字节使能，`tlast=1`，触发 `tvalid(1)` 后返回。

**接收（DPE→CPU）流程**（[ethernet.c:L95-L128](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/ethernet.c#L95-L128)）：

1. 先读 `cpu_fifo->tx->status->tvalid()`，无数据则返回 0。
2. 读出 `tuser_src` 记进 `packet->src`（包是从哪个口进来的）。
3. 循环读 4 个 32 位字拼回 payload；每轮触发 `tready(1)`。
4. 遇到 `tlast=1` 时，读 `tkeep`，用「数末尾连续 0」的方式反推真实长度（`while (keep & 1) { len++; keep >>= 1; }`），触发最后一次 `tready(1)` 后返回。
5. 越界保护：写 payload 时带 `if (len <= ETH_MAX_FRAME_LENGTH - 16)` 判断，超大帧只读到边界，不溢出缓冲。

#### 4.1.3 源码精读

发送时元数据与首 beat 的写入（注意 rx = 发送方向）：

```c
csr->cpu_fifo->rx->control->tuser_dst(packet->dst);
csr->cpu_fifo->rx->control->tuser_src(packet->src);
// ...
while (1) {
   csr->cpu_fifo->rx->data_31_0->tdata(*((uint32_t*)(packet->payload + i)));
   csr->cpu_fifo->rx->data_63_32->tdata(*((uint32_t*)(packet->payload + i + 4)));
   csr->cpu_fifo->rx->data_95_64->tdata(*((uint32_t*)(packet->payload + i + 8)));
   csr->cpu_fifo->rx->data_127_96->tdata(*((uint32_t*)(packet->payload + i + 12)));
```
> [2.sw/app/ethernet.c:L58-L67](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/ethernet.c#L58-L67)：把结构体里的 `payload` 当成字节数组，每次强转读 4 个 32 位字，正好填满一个 128 位 beat。这正是 u3-l3 讲的「128↔32 位宽拆分」在软件侧的体现。

末 beat 的字节使能收紧：

```c
empty = i - packet->len;
csr->cpu_fifo->rx->control->tkeep(0xFFFF >> empty);
csr->cpu_fifo->rx->control->tlast(1);
csr->cpu_fifo->rx->trigger->tvalid(1);
```
> [2.sw/app/ethernet.c:L76-L79](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/ethernet.c#L76-L79)：`empty` 是末 beat 里「不属于本帧」的尾巴字节数。把 `0xFFFF` 右移这么多位，等于把那几个字节的使能关掉，再置 `tlast`，硬件就知道这一拍既是包尾、又只用到前 `16-empty` 个字节。

接收时由 tkeep 反推长度：

```c
keep = csr->cpu_fifo->tx->control->tkeep();
while (keep & 1) {
    len++;
    keep >>= 1;
}
```
> [2.sw/app/ethernet.c:L112-L116](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/ethernet.c#L112-L116)：tkeep 的每一个「1」代表一个有效字节。从最低位往上数连续多少个 1，就是末 beat 的有效字节数；加上之前整 beat 累计的 `len`，得到全帧长度。这是发送端收紧操作的逆运算。

#### 4.1.4 代码实践

**实践目标**：亲手验证 tkeep 与长度的互逆关系。

**操作步骤**：

1. 打开 [2.sw/app/ethernet.c:L76-L79](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/ethernet.c#L76-L79) 与 [L112-L116](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/ethernet.c#L112-L116)。
2. 假设要发送一个 `len = 60` 字节的帧。`i` 从 0 每次 +16，走到 `i=64` 时 `64 >= 60` 进入末 beat 分支，`empty = 64 - 60 = 4`。
3. 计算 `tkeep = 0xFFFF >> 4 = 0x0FFF`（低 12 位为 1）。
4. 模拟接收端：`keep = 0x0FFF`，数连续低位 1 的个数：`0x0FFF & 1` 循环 12 次，得末 beat 12 字节；`len` 在此前已累加到 `48`，故全帧 `48 + 12 = 60`。

**需要观察的现象**：发送的 `len` 与接收端反推的 `len` 完全相等，证明这对收紧/展开互为逆运算。

**预期结果**：60 → tkeep 0x0FFF → 反推回 60。换 `len=64` 试，`empty=0`、`tkeep=0xFFFF`、反推 16、`48+16=64`，自洽。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `eth_send_packet` 在 `tready()` 为假时直接返回 0，而不是死等？

**参考答案**：主循环是协作式的，死等会让整个固件（包括 CLI）卡死。返回 0 让调用者决定是否丢弃或重试，把策略留给上层。

**练习 2**：接收函数里 `if (len <= ETH_MAX_FRAME_LENGTH - 16)` 这个判断保护的是什么？

**参考答案**：保护 `payload[1536]` 缓冲不被超大帧写溢出。即使硬件送来一个恶意巨帧，软件最多把前 1520 字节写进缓冲，后续 beat 仍继续消耗并最终应答 `tready`，但不写入内存。

---

### 4.2 最小网络协议栈：ARP / ICMP / IP 解析与校验和

#### 4.2.1 概念说明

`network.c` 是一个极其精简的网络栈，只实现 VPN 节点维持基本可达性所必需的三件事：

- **IP 解析**：把 `"192.168.1.98"` 这样的点分十进制字符串变成 4 字节 IP（CLI 配置时大量使用）。
- **ARP**：回应「谁是这个 IP」的请求，让同网段设备能找到本节点的 MAC。
- **ICMP Echo**：回应 `ping`，这是最直接的连通性验证手段。

它不实现 UDP/ICMP 以外的转发逻辑，也不维护路由——线速转发由 DPE 负责。这里只处理「目标是本机控制面」的慢协议报文。

协议头用「逐字节」的 `uint8_t` 数组描述（见 [network.h:L41-L79](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/network.h#L41-L79)），因为这些字段在以太网线上就是**大端（网络字节序）**排列的。用字节数组而非 `uint16_t/uint32_t`，能彻底回避软核的字节序问题——线上字节顺序即内存字节顺序。

#### 4.2.2 核心流程

**协议识别** `net_parse_packet_header()`（[network.c:L123-L136](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/network.c#L123-L136)）按层级逐层判定：

1. 看 Ethernet 头的 ethertype：`0x0806` → ARP；`0x0800` 且 IP 版本为 4 → 进 IPv4 判定。
2. 在 IPv4 里看 protocol 字段：`1` → ICMP；`17` → UDP。
3. 其余 → `NET_PROTO_UNKNOWN`。

**Internet 校验和** `net_calculate_checksum()`（[network.c:L91-L114](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/network.c#L91-L114)）实现了 RFC 1071 的端到端校验和。把数据看成 16 位大端字 \(w_k\)，求和时做「进位绕回」，最后取反：

\[
\text{sum} = \sum_{k} w_k \pmod{2^{16}-1}
\]

进位绕回的含义是：每当和超过 \(2^{16}-1\)，就把超出的一位 1 加回最低位。最终校验和：

\[
\text{checksum} = \sim\text{sum} \ \&\ \texttt{0xFFFF}
\]

实现里每加完一个字就即时绕回一次（`if (sum > 0xFFFF) sum = (sum & 0xFFFF) + 1;`），保证累加器始终 ≤ 0xFFFF。奇数字节的情况单独处理：把最后一个字节左移 8 位当成低字节为 0 的字。

**ARP 应答** `net_process_arp()`（[network.c:L146-L195](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/network.c#L146-L195)）的流程：

1. 校验硬件类型（以太网 0x0001）、协议类型（IPv4 0x0800）、目标 IP 是否等于本机 IP。任一不符返回 0。
2. 若是请求（oper=0x0001）：组装应答——把请求方的 MAC/IP 填进应答的目标字段，把本机 MAC/IP 填进应答的源字段，oper 改为 0x0002（reply），置 `tx->dst = rx->src`（原路返回）、`bypass_all = 1`（绕过 DPE 处理流水线）、`len = 42`（ARP 包固定长度）。

**ICMP Echo 应答** `net_process_icmp()`（[network.c:L204-L263](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/network.c#L204-L263)）的流程：

1. 校验目标 IP 是本机，且 ICMP type=0x08（Echo Request）、code=0x00。
2. 组装应答：交换二层/三层源目的地址；IPv4 头 `ttl=0xFF`、`protocol=1`；ICMP type 改为 0x00（Echo Reply），原样保留 `id` 与 `sequence`，原样拷贝请求里的载荷。
3. 重算 IPv4 头校验和（20 字节）与 ICMP 校验和（覆盖 ICMP 头+载荷）。
4. 同样 `tx->dst = rx->src`、`bypass_all = 1`。

> 关键点：所有 CPU 生成的应答都置 `bypass_all = 1`。这是因为这些应答是「已经处理完的裸以太网帧」，不应再进 DPE 的 WG 加解密流水线，否则会被错误地封装/加密。`bypass_all` 让它们原样穿透处理级、直接送到目的口（承接 u4-l1 的 TUSER 编码）。

#### 4.2.3 源码精读

协议识别的层级判定：

```c
if (hdr_eth->ethertype[0] == 0x08 && hdr_eth->ethertype[1] == 0x06) {
   return NET_PROTO_ARP;
} else if (hdr_eth->ethertype[0] == 0x08 && hdr_eth->ethertype[1] == 0x00
        && hdr_ipv4->version == 4) {
   if (hdr_ipv4->protocol == 1)      return NET_PROTO_ICMP;
   else if (hdr_ipv4->protocol == 17) return NET_PROTO_UDP;
}
return NET_PROTO_UNKNOWN;
```
> [2.sw/app/network.c:L126-L135](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/network.c#L126-L135)：ethertype 存成 2 字节数组，`[0]=0x08,[1]=0x06` 即大端的 0x0806（ARP）。注意它直接按字节比，不依赖 CPU 字节序。

校验和的逐字累加与进位绕回：

```c
for (i = 0; i + 1 < len; i += 2) {
   uint16_t word = ((uint16_t)data[i] << 8) | data[i + 1];
   sum += word;
   if (sum > 0xFFFF) {
      sum = (sum & 0xFFFF) + 1;
   }
}
return (uint16_t)(~sum & 0xFFFF);
```
> [2.sw/app/network.c:L95-L113](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/network.c#L95-L113)：两个字节拼成大端 16 位字，累加并即时绕回，最后取反。这正是前述公式的直译。

ICMP 应答里重算两个校验和：

```c
checksum = net_calculate_checksum((uint8_t*)(tx_packet->payload + sizeof(net_hdr_eth_t)), 20);
tx_hdr_ipv4->check[0] = (checksum >> 8) & 0xFF;
tx_hdr_ipv4->check[1] = checksum & 0xFF;
// ... 组装 ICMP 头与载荷 ...
checksum = net_calculate_checksum((uint8_t*)tx_hdr_icmp, rx_packet->len - 34);
tx_hdr_icmp->checksum[0] = (checksum >> 8) & 0xFF;
tx_hdr_icmp->checksum[1] = checksum & 0xFF;
```
> [2.sw/app/network.c:L243-L258](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/network.c#L243-L258)：IPv4 校验和只覆盖 20 字节头（跳过 14 字节 Ethernet 头）；ICMP 校验和覆盖 ICMP 头加全部载荷。算出的 16 位值按大端拆成两个字节写回。`34 = 14(Ethernet) + 20(IPv4)`，所以 `rx_packet->len - 34` 就是 ICMP 段的总长。

#### 4.2.4 代码实践

**实践目标**：手工验算一个最小 IPv4 头的校验和，确认 `net_calculate_checksum` 的行为。

**操作步骤**：

1. 构造一个 20 字节的全 0 IPv4 头，仅把版本/IHL 字节设为 `0x45`、`tot_len` 设为 `0x00 0x14`（20）、protocol 设为 `0x01`，其余为 0，校验和字段先填 0。
2. 按 16 位大端字求和：`0x4500 + 0x0014 + 0x0000 + ... + 0x0001 + ...`（其余为 0 的字省略）。
3. 对和取反，得到应有的校验和。
4. 对照 `net_calculate_checksum` 的返回值应与之相同。

**预期结果**：典型 `0x4500` 开头的 20 字节 IPv4 头，按此法算出的校验和与 Wireshark 显示一致。若你不确定具体数值，标注「待本地验证」后用 Wireshark 抓一个真实 ping 包对照。

#### 4.2.5 小练习与答案

**练习 1**：为什么 ARP/ICMP 应答里都要写 `tx_packet->dst = rx_packet->src`？

**参考答案**：`src` 记录的是这帧从哪个 DPE 接口进来（由 mux 盖写，是权威字段，见 u4-l1/u4-l2）。应答要原路发回，所以把目的设为来源接口，demux 就会把它送回同一个网口。

**练习 2**：`net_process_icmp` 里 ICMP 校验和的长度为什么是 `rx_packet->len - 34` 而不是固定 8 字节？

**参考答案**：ICMP Echo Request 除了 8 字节头（type/code/checksum/id/sequence）还携带可变长载荷（如 Linux ping 默认 56 字节）。校验和必须覆盖头 + 载荷，故长度 = 全帧长度 − Ethernet(14) − IPv4(20) = 全帧 − 34。

---

### 4.3 无 libc 字符串库 string_bare

#### 4.3.1 概念说明

在普通 C 程序里，`memcpy`、`strlen`、`strcmp` 是标准库（libc）自带的。但这个固件是裸机链接的——它不链接 glibc/newlib，因此这些符号根本不存在。如果代码里用了 `memcmp` 而没有提供实现，链接器会报「undefined reference」。

`string_bare.c` 就是为此而存在的「迷你 libc」：用最朴素的方式手写实现了一批内存与字符串函数，外加 CLI 专用的数值解析函数。它还有一个工程上的好处：避免引入 libc 后潜在的 `malloc`/`printf` 依赖与代码体积膨胀，符合 u6-l1 讲过的「无 OS、无 libc、无动态分配」约束。

> 旁证：[2.sw/app/string_bare.c:L45](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/string_bare.c#L45) 的 `memset` 带了 `__attribute__((used))`，正是为了防止链接器在看似「没人调用」时把它优化掉——因为它很可能被编译器隐式生成调用（比如结构体初始化会被 lowering 成 `memset`）。

#### 4.3.2 核心流程

这批函数逻辑都很直白：

- `memset/memcpy/memcmp/strlen`：逐字节循环，与教科书实现一致。
- `strcmp`：复用 `memcmp`，长度取 `strlen(s1)`。
- `str_parse_uint32`：十进制逐位累加 `val = val*10 + (c - '0')`，带 `min/max` 范围校验，遇非法字符或越界返回 0。
- `str_parse_hex`：十六进制逐位累加 `val = (val << 4) | nibble`，支持大小写，同样带范围校验。

一个值得注意的细节：所有解析函数都以 `'\n'` 或 `'\0'` 作为输入串终止符，而不是只认 `'\0'`。这是因为 CLI 读进来的串以换行结尾（见 4.4 节 `uart_recv` 的行为），省去一步去尾操作。

#### 4.3.3 源码精读

十六进制解析（CLI 配置 MAC/密钥时大量使用）：

```c
uint8_t str_parse_hex(const char* str, uint32_t* value, uint32_t min, uint32_t max) {
   uint32_t val = 0;
   if (*str == '\n' || *str == '\0') return 0;
   while (*str != '\n' && *str != '\0') {
      if (*str >= '0' && *str <= '9')       val = (val << 4) | (*str - '0');
      else if (*str >= 'A' && *str <= 'F')  val = (val << 4) | (*str - 'A' + 10);
      else if (*str >= 'a' && *str <= 'f')  val = (val << 4) | (*str - 'a' + 10);
      else return 0;
      str++;
   }
   if (val < min || val > max) return 0;
   *value = val;
   return 1;
}
```
> [2.sw/app/string_bare.c:L128-L150](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/string_bare.c#L128-L150)：每读一个 hex 字符就把已累计的值左移 4 位再或上新 nibble。`min/max` 让调用方能限定（如 MAC 高 16 位限定 `0..0xFFFF`）。返回 1 成功、0 失败的约定贯穿所有解析函数。

IP 地址解析（位于 network.c，但同属「无 scanf 的手写解析」家族）：

```c
while (*str != '\n' && *str != '\0') {
   if (*str >= '0' && *str <= '9') {
      value = value * 10 + (*str - '0');
      if (value > 255) return 0;
   } else if (*str == '.') {
      temp_ip[part++] = (uint8_t)value;
      value = 0;
   } else return 0;
   str++;
}
```
> [2.sw/app/network.c:L57-L73](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/network.c#L57-L73)：注释明说「write function without using scanf sscanf from stdlib」。每遇一个点就把当前累计的十进制值存为一个 octet，最终要求恰好 3 个点（4 段）。任何 octet > 255 或字符非法都判失败。

#### 4.3.4 代码实践

**实践目标**：体会「无 libc」对工程的真实影响。

**操作步骤**：

1. 在 [2.sw/app/main.cpp](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp) 里搜索 `memcmp`、`memcpy`、`strcmp`、`strlen` 的调用点，确认它们都被用到。
2. 设想：如果删掉 `string_bare.c` 不参与编译，这些调用会怎样？
3. 对照构建脚本（u1-l4 讲过 `MakefileSW`），确认 `string_bare.c` 在源文件清单里、而链接命令里没有 `-lc`。

**预期结果**：会发现网络栈、CLI 派发、crypto 测试都依赖这批函数；去掉它们将产生大量 undefined reference。这正是手写 `string_bare` 的根本理由。

#### 4.3.5 小练习与答案

**练习 1**：`strcmp(s1, s2)` 的实现用 `strlen(s1)` 作为比较长度，这在什么情况下可能不符合标准 `strcmp` 的语义？

**参考答案**：若 `s2` 比 `s1` 短且 `s2` 是 `s1` 的前缀（如 `s1="abc"`、`s2="ab"`），标准 `strcmp` 应返回非 0（因为会在 `s2` 的终止符处发现差异），但这里的实现只比了 `s1` 的长度 3 字节，会把越界的 `s2[2]`（即 `s2` 的终止符后的内容）也拿去比，行为不可预期。在本项目里 CLI 命令串都以 `'\n'` 结尾且长度匹配，所以不触发该边角问题——这是一个「够用但不严格」的实现。

**练习 2**：为什么 `str_parse_hex` 同时支持大写和小写 A-F？

**参考答案**：CLI 用户可能输 `CCBACA01` 也可能输 `ccbaca01`，两种都应被接受，提升交互友好度。

---

### 4.4 CLI 命令派发与硬件表写入

#### 4.4.1 概念说明

CLI 是操作员与节点交互的唯一界面，跑在 UART 上（u2-l5 讲过 UART 的双用途：字符 CLI 与二进制特殊模式；这里只用字符模式）。它提供两类命令：

- **show / config 系列**：查看或修改网络配置（`network`）、路由表（`routes`）、密钥表（`cryptokeys`）。
- **test 系列**：跑密码学自检（ChaCha20-Poly1305、BLAKE2s、Curve25519、RNG、timer，承接 u6-l2）。

CLI 的实现极其朴素：没有词法分析器、没有命令树，就是一个长长的 `if (strcmp(...)) ... else if (...)` 链。配置类命令采用「逐项问卷」交互：每提示一项当前值作默认，用户回车表示沿用、或输入新值。这正是 [6.test/README.md](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/6.test/README.md) 里那些 `IP address [192.168.1.98]:` 交互的来源。

本模块的重头戏是 `config routes`：它把人填的「目的 IP/掩码/peer/出口」翻译成硬件路由表里的一个条目。路由表在 SystemRDL 里声明为 `external regfile`（[csr.rdl:L527-L579](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl#L527-L579)），物理上由 `tdp_ram` 实现（u4-l6），CPU 通过 HAL 指针 `csr->routing_table->entry[i]->...` 访问。改表前必须先做 FCR 的 pause/idle 握手（u3-l4），防止数据面读到「改了一半」的脏条目。

#### 4.4.2 核心流程

**主循环**（[main.cpp:L817-L939](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L817-L939)）每轮做两件事：

1. **收包处理**：`eth_receive_packet` 取一帧 → `net_parse_packet_header` 识别协议 → 按 ARP/ICMP/UDP/UNKNOWN 分发处理。当前 Phase1 还有一段 eth1↔eth2 的软件直通桥（后面讲）。
2. **CLI 处理**：`uart_recv` 取一行命令 → `strcmp` 链派发 → 执行对应函数。

**`config routes` 写硬件表的完整流程**（[main.cpp:L406-L461](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L406-L461)）：

1. **FCR 暂停**：`csr->dpe->fcr->pause(1)`，然后 `while (!csr->dpe->fcr->idle())` 死等数据面排空、进入静止。这是 u3-l4 讲的 8 步原子更新握手的「请求—应答」阶段。
2. **读当前值作默认**：逐字段 `csr->routing_table->entry[idx]->ip->ip()` 读回现有条目，显示成 `[当前值]:` 提示。
3. **逐项问卷**：依次读 entry index、目的 IP、掩码、peer index、目的接口，用 `str_parse_uint32` / `net_str_parse_ip` 解析。
4. **字节序打包**：把 4 字节 IP/掩码按「首字节为最高位」打包成 `uint32_t`（`uip = (ip[0]<<24)|(ip[1]<<16)|(ip[2]<<8)|ip[3]`），这是网络字节序在 `uint32` 里的表示。
5. **写硬件表**：通过 HAL 把 4 个字段写回：`csr->routing_table->entry[idx]->ip->ip(uip)`、`mask->mask(umask)`、`peer_idx->peer_idx(peer_idx)`、`dst->dst(dst)`。每次调用底层是一次 CSR 总线写，经地址译码命中 `tdp_ram` 的 A 口（u4-l6）。
6. **FCR 恢复**：`csr->dpe->fcr->pause(0)`，数据面恢复转发。

> 字节序一致性：HAL 侧用「首 octet 在最高位」存 IP，读出来时再用 `(uip>>24)&0xFF` 还原首字节。软件、HAL、SystemRDL 三方对 IP 的 32 位打包约定完全一致，避免了大端/小端混淆。

#### 4.4.3 源码精读

CLI 派发的 `strcmp` 链（节选）：

```c
if (strcmp(uart_rx_data, "show routes\n") == 0) {
   show_routes(csr);
} else if (strcmp(uart_rx_data, "config routes\n") == 0) {
   config_routes(csr);
} else if (strcmp(uart_rx_data, "config cryptokeys\n") == 0) {
   config_cryptokeys(csr);
}
```
> [2.sw/app/main.cpp:L900-L907](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L900-L907)：注意比较串都以 `'\n'` 结尾——因为 `uart_recv` 把回车统一翻译成了 `'\n'`（见 [uart.c:L200-L206](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/uart.c#L200-L206)）。这就是为什么 4.3 里所有解析函数都要认 `'\n'` 终止符。

`config_routes` 的 FCR 握手 + 硬件写入：

```c
void config_routes(volatile csr_vp_t* csr) {
   csr->dpe->fcr->pause(1);
   while (!csr->dpe->fcr->idle());
   // ... 逐项问卷，解析得到 ip/mask/peer_idx/dst ...
   uip  = ((uint32_t)ip[0] << 24) | ((uint32_t)ip[1] << 16)
        | ((uint32_t)ip[2] << 8)  | (uint32_t)ip[3];
   umask = ((uint32_t)mask[0] << 24) | ((uint32_t)mask[1] << 16)
        | ((uint32_t)mask[2] << 8)  | (uint32_t)mask[3];
   csr->routing_table->entry[entry_idx]->ip->ip(uip);
   csr->routing_table->entry[entry_idx]->mask->mask(umask);
   csr->routing_table->entry[entry_idx]->peer_idx->peer_idx(peer_idx);
   csr->routing_table->entry[entry_idx]->dst->dst(dst);
   // ... 显示更新后的条目 ...
   csr->dpe->fcr->pause(0);
}
```
> [2.sw/app/main.cpp:L409-L460](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L409-L460)：开头 pause/idle、结尾 pause(0) 把整个问卷与写表过程包成一个原子区间。写表的 4 行 HAL 调用，每一行对应路由表条目的一个字段（与 [csr.rdl:L535-L577](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl#L535-L577) 声明的 ip/mask/peer_idx/dst 一一对应）。

收包分发主结构（节选）：

```c
if (eth_receive_packet(csr, &eth_packet_rx)) {
   csr->gpio->led2(1);
   net_protocol_t protocol = net_parse_packet_header(&eth_packet_rx);
   switch (protocol) {
      case NET_PROTO_ARP:
         if (net_process_arp(&net_config, &net_arp_cache, &eth_packet_rx, &eth_packet_tx))
            eth_send_packet(csr, &eth_packet_tx);
         break;
      case NET_PROTO_ICMP:
         if (net_process_icmp(&net_config, &eth_packet_rx, &eth_packet_tx))
            eth_send_packet(csr, &eth_packet_tx);
         break;
      case NET_PROTO_UDP:
         break; // 当前仅识别，未处理（WireGuard 握手的挂载点）
   }
   // ... eth1<->eth2 软件直通桥 ...
}
```
> [2.sw/app/main.cpp:L819-L878](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L819-L878)：收包 → 解析 → 处理 → 回送，是一条完整的软件往返链路。`NET_PROTO_UDP` 分支目前只做 debug 打印、没有实际处理——这正是未来 WireGuard 握手 Agent（基于 UDP）应当挂载的位置；当前 Phase1 尚未在此接入。

#### 4.4.4 代码实践

**实践目标**：把 `config routes` 的「问卷输入」与「硬件写入」对应起来。

**操作步骤**：

1. 打开 [6.test/README.md](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/6.test/README.md) 里左节点 `config routes` 的交互记录（约第 73-83 行）：
   - Entry index `0`
   - Destination IP `192.168.0.0`
   - Subnet mask `255.255.255.0`
   - Peer index `1`
   - Destination interface `6`
2. 对照 [main.cpp:L449-L454](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L449-L454)，把 `192.168.0.0` 手工打包成 `uip`：`(192<<24)|(168<<16)|(0<<8)|0 = 0xC0A80000`。
3. 确认这 4 个值分别写进了 `routing_table.entry[0]` 的 `ip/mask/peer_idx/dst` 字段。
4. 注意 `dst=6` 对应 `MCAST_24`（eth2+eth4，见 [ethernet.h:L31](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/ethernet.h#L31)）——这正是 README 里显示的 `[..2.4]` 标注的来源。

**预期结果**：能把 CLI 的每一行输入，对应到一条 HAL 写语句、再到路由表条目里的一个字段。`dst=6` 与 `[..2.4]` 的对应关系能自圆其说。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `config_routes` 在做问卷（读 UART）之前就先 `pause(1)`，而不是等问完要写表时才暂停数据面？

**参考答案**：因为问卷期间操作员可能要敲好几秒，若数据面在此期间还在转发，就有可能与即将写入的新条目产生竞争。先 pause→idle 让数据面彻底静止，再从容问卷、写表，保证整个「读—改—写」区间对数据面是原子的。代价是配置期间线速转发会暂停——但配置只在上线初偶发，可接受。

**练习 2**：`show_routes` 也做了 `pause(1)/while(!idle)/.../pause(0)`（[main.cpp:L395-L403](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L395-L403)），读表为什么也要暂停数据面？

**参考答案**：路由表是 external regfile，由 `tdp_ram` 实现，A 口（CSR/CPU）与 B 口（数据面查表）共享存储。读期间若不暂停，数据面可能正在改写或并发访问同一表项，读到的可能是过渡态。暂停保证读到一致快照。

---

## 5. 综合实践

把本讲三块知识串起来，完成下面这个「**ICMP Echo 在软件里的完整往返 + 一条路由的配置落地**」追踪任务。

**背景**：你在左节点的串口终端上，先 `config routes` 配了一条路由，然后在主机 A 上 `ping` 节点本身（目的 IP 是节点的 network IP）。请回答两个问题。

**任务 A：追踪 ICMP Echo 的软件往返路径。**

请按顺序列出从「帧到达网口」到「应答帧离开网口」经过的每一个函数与关键操作，至少覆盖：

1. 帧如何从网口进入 CPU（经 DPE 哪些环节、最后落到哪个 FIFO）。
2. `main` 里哪个函数把它读进 `eth_packet_rx`，哪个函数识别出它是 ICMP。
3. `net_process_icmp` 做了哪几个关键变换（源/目的交换、type 改写、校验和重算、bypass/dst 设置）。
4. 应答帧如何经 `eth_send_packet` 写回 FIFO，并经 DPE 送回原网口。

**任务 B：解释 `config routes` 如何写入硬件路由表。**

参照 [main.cpp:L406-L461](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L406-L461) 与 u4-l6，说明：

1. 为什么开头要 `pause(1)` + 等 `idle`，结尾才 `pause(0)`。
2. 用户输入的 4 项分别写到 `tdp_ram` 的哪 4 个字段。
3. 这些 HAL 写调用是如何经 CSR 地址译码命中双口 RAM 的 A 口的。

**操作建议**：在源码里用注释笔（或纸笔）给上述每一步标上行号；若手边有上板条件，可在 CLI 里先 `debug` 打开调试模式，再 ping，观察 `<< NET_PROTO_ICMP` 与 `>> NET_PROTO_ICMP` 的日志，与你的追踪一一对照。无上板条件时，标注「待本地验证」并完成纯源码层面的追踪即可。

## 6. 本讲小结

- `ethernet.c` 把 `cpu_fifo` 的 128 位 AXIS beat 收发封装成 `eth_send_packet`/`eth_receive_packet`，向网络栈暴露带元数据的 `eth_raw_packet_t`；发送用 `tkeep = 0xFFFF >> empty` 收紧末 beat，接收用数低位连续 1 的方式反推长度，二者互为逆运算。
- `network.c` 是一个极简裸机网络栈，只实现 IP 解析、ARP 应答、ICMP Echo 应答与 Internet 校验和（逐字累加 + 进位绕回 + 取反）；所有 CPU 生成的应答都置 `bypass_all=1`、`dst=src`，原路绕过 DPE 处理流水线送回。
- 协议头用 `uint8_t` 字节数组按网络大端序描述，回避软核字节序问题；IP 与 HAL 之间用「首 octet 在最高位」的 `uint32` 打包/解包，软件、HAL、SystemRDL 三方约定一致。
- `string_bare.c` 是为「无 libc 裸机链接」而生的迷你字符串/内存库，外加十进制/十六进制/IPv4 解析；所有解析都以 `'\n'`/`'\0'` 终止，呼应 `uart_recv` 把回车翻译成 `'\n'` 的约定。
- CLI 用一条 `strcmp` 链派发命令，`config routes`/`config cryptokeys` 以逐项问卷交互，写硬件表前后用 FCR 的 `pause/idle` 包成原子区间，经 HAL 指针写到 `tdp_ram` 的 A 口。
- 主循环是单线程协作式：收包处理（ARP/ICMP/UDP 分发 + Phase1 的 eth1↔eth2 软件直通桥）与 CLI 处理轮流被轮询；UDP 分支当前仅识别未处理，是 WireGuard 握手 Agent 的预留挂载点。

## 7. 下一步学习建议

- **走向完整的控制流**：本讲的收发包与表更新是「半成品」控制流。下一讲 **u6-l4（软件控制流：收发包与表更新）** 会把 `cpu_fifo` 的 10 步收发流程、WireGuard 握手 Agent、以及 KMM/路由更新经 HAL 写表的完整调用序列串成一条主线，建议紧接着读。
- **补齐握手处理**：本讲看到 UDP 分支尚未接入握手处理。可预先浏览 [2.sw/app/wireguard_libs.cpp](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/wireguard_libs.cpp) 与 [wireguard_libs.h](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/wireguard_libs.h)，理解 WireGuard Agent 的数据结构，为 u6-l4 做准备。
- **回看硬件侧**：想确认本讲写的 `routing_table`/`cryptokey_table` 在硬件里如何被数据面读用，可回看 u4-l4（TCAM 路由查找）与 u4-l6（tdp_ram 实现）；想确认 `bypass_all`/`dst` 如何被 demux 解释，可回看 u4-l1/u4-l3。
- **加密对端**：若你对 CLI 里 `config cryptokeys` 配的 256 位加解密密钥最终喂给了什么硬件感兴趣，可跳到 Unit 5（ChaCha20-Poly1305 硬件）与 u5-l1（AEAD 原理）。
