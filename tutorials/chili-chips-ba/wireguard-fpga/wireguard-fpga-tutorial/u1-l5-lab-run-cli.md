# 实验室运行：上板、CLI 配置与端到端验证

## 1. 本讲目标

本讲是「上手篇」的收尾，也是把前几讲的知识第一次「跑起来」的一讲。读完本讲，你应该能够：

- 说出烧写 bitstream 之后，如何用一根 USB 线 + `minicom` 连上板子的 **CLI（命令行界面）**，并理解这个 CLI 其实跑在板内的 RISC-V 软 CPU 上。
- 看懂欢迎横幅、`help` 输出，以及 `config network` / `config routes` / `config cryptokeys` 这三条核心配置命令分别问你要哪些参数。
- 画出**两节点拓扑**，知道左/右两个 WireGuard 节点要怎么配置才能互通，并用 `ping` + Wireshark 抓包确认隧道是否真的加密。
- 解释一个最关键的安全细节：**为什么两个节点的加密密钥与解密密钥必须互为镜像**。

本讲只讲「怎么把板子配通、怎么验证」，不展开每条命令背后的寄存器实现（那是 U3 的事）。我们把 CLI 当成一个交互式问卷来读，逐个问题理解它在问什么。

## 2. 前置知识

- **bitstream（比特流）**：上一讲 u1-l4 产出的 `top.bit` 文件，烧进 FPGA 后决定电路怎么连。本讲假设你已经有了它。
- **UART（通用异步收发传输器）**：一种最简单的串口通信方式，一根线发、一根线收。Alinx AX7201 板上把 UART 做成了 **USB 虚拟串口**（CP2102 芯片），所以你用一根 USB 线就能既供电又通信。串口在 Linux 上通常表现为 `/dev/ttyUSB0`。
- **CLI（Command-Line Interface，命令行界面）**：一个「你敲一行命令、它回一段话」的文本交互界面。本项目的 CLI 由板内软 CPU 上的固件提供，通过 UART 传到你的电脑终端。
- **minicom**：Linux 下一款常用的串口终端程序，相当于「把屏幕和键盘接到板子的 UART 上」。
- **WireGuard**：一种现代 VPN 协议，用 UDP 承载，靠对称密钥（ChaCha20-Poly1305）给隧道里的流量加解密。本讲不深入协议本身，只需要知道「两个节点要配对密钥才能互加解密」。
- **MAC 地址 / IP 地址 / 子网掩码 / 网关**：网络基础概念。MAC 是网卡的物理地址（6 字节），IP 是网络层地址（4 字节），掩码决定一个网段有多大，网关是「去别的网段要先交给谁」。
- **镜像（mirror）**：本讲里指「左右两边的数据严格对称、互为对方的反面」，下面会详细解释。

如果你对 WireGuard 协议本身好奇，可以放到 U5（加密硬件）和 U6（软件控制面）再深入；本讲只把它当成「配对密钥就能加密通话」的黑盒。

## 3. 本讲源码地图

本讲围绕「上板 → 连 CLI → 配三张表 → ping 验证」这条操作链展开。主要参考文档与少量源码：

| 文件 | 作用 |
|------|------|
| [6.test/README.md](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/6.test/README.md) | 实验室测试主文档：拓扑图、minicom 连接、三条命令的完整配置实录、ping/Wireshark 验证 |
| [1.hw/README.md](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/README.md) | 硬件架构与数据流，含「HW/SW 协同」四节点示例拓扑与 55 步包处理分析 |
| [2.sw/app/main.cpp](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp) | CLI 固件主循环：欢迎横幅、命令分派、`config_network`/`config_routes`/`config_cryptokeys` 三个函数的实现 |
| [1.hw/ip.infra/dpe_pkg.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/dpe_pkg.sv) | DPE 内部地址常量（CPU/eth1-4/组播/广播），解释「Default interface」编号的含义 |
| [3.build/csr_build/csr.rdl](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl) | 三张表（network/routes/cryptokeys）的寄存器规格，命令问的每个参数都对应这里的一个字段 |
| [6.test/dump_packet.UART.py](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/6.test/dump_packet.UART.py) | 调试用脚本之一：通过 UART 特殊模式从 DMEM 抓出一个完整以太网包，辅助验证 |

> 小提示：链接里的 `9887a3b3…` 是当前 HEAD 的 commit 号，点开即定位到本讲对应版本。

## 4. 核心概念与源码讲解

### 4.1 上板与串口连接

#### 4.1.1 概念说明

build 出 bitstream（上一讲 u1-l4）只是把电路「画」进了 FPGA。要真正跑起来，还要做两件事：

1. **给板子供电并烧写 bitstream**——用 `make -f MakefileHW program` 把 `top.bit` 灌进芯片（具体见 [3.build/README.md](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/README.md)）。
2. **连上 CLI**——板子通电后，板内的 RISC-V 软 CPU 会从 IMEM 启动，跑起固件（`2.sw/app/main.cpp`），并通过 UART 在你的电脑终端上打印一个欢迎横幅，然后等待命令。

关键认知：**CLI 不是跑在你电脑上的，而是跑在 FPGA 芯片内部的软 CPU 上**。你的电脑只是充当一块「远程屏幕 + 键盘」，通过 USB 串口把字符来回传。这正呼应了 u1-l1/u1-l3 讲过的「自包含（self-contained）」设计目标——板子不需要 PC 主机就能独立成一个 VPN 节点，PC 只在配置/调试时连一下。

#### 4.1.2 核心流程

上板连 CLI 的步骤：

```
  ┌─────────────┐   USB 线      ┌──────────────────────────┐
  │  你的 PC    │◄────────────▶│  Alinx AX7201 板         │
  │ minicom     │  /dev/ttyUSB0 │  ┌────────────────────┐  │
  │ (终端窗口)  │               │  │ RISC-V 软 CPU       │  │
  └─────────────┘               │  │ 跑 main.cpp 固件    │  │
                                │  │ → 打印横幅 → CLI    │  │
                                │  └────────────────────┘  │
                                └──────────────────────────┘
```

1. 用 USB 线把 PC 连到 AX7201 的 USB UART 口。
2. 在 PC 上找到串口设备名（Linux 一般是 `/dev/ttyUSB0`，Windows 是 `COMx`）。
3. 用串口终端程序打开它，波特率 **115200**（这是板子侧固死的设置）。
4. 给板子上电（或复位），终端里就会出现欢迎横幅和 `(wireguard-fpga)#` 提示符。

#### 4.1.3 源码精读

测试文档给出的连接命令（[6.test/README.md:16-20](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/6.test/README.md#L16-L20)）：

```
minicom -D /dev/ttyUSB0
```

连上后，固件先做一次「硬件身份校验」，再打印横幅、启动网络、给出提示符（[2.sw/app/main.cpp:790-815](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L790-L815)）。关键几行：

```c
   // Check hardware ID
   if (csr->hw_id->VENDOR() != 0xCCBA ||
       csr->hw_id->PRODUCT() != 0xCACA) {
      uart_send(csr, "\r\nHardware ID mismatch! Halting...\r\n");
      ...
   }
   // Display banner
   uart_send(csr, "\r\n==========================================\r\n");
   uart_send(csr, "          WireGuard FPGA v");
   ...
   // Boot sequence
   uart_send(csr, "Booting up...\r\n");
   init_network(csr, &net_config);
   show_network(csr, &net_config);
   // CLI prompt
   uart_send(csr, "\r\nType 'help' to display commands.\r\n");
   uart_send(csr, "(wireguard-fpga)# ");
```

注意几点：

- 它会先读硬件 ID 寄存器，确认厂商号 `0xCCBA`、产品号 `0xCACA`（Chili.CHIPS*ba 的签名）。对不上就 `ebreak` 停机——这是防止固件烧错板子的一道保险。
- 启动时调 `init_network` + `show_network`，把默认网络配置（IP/掩码/MAC/网关/默认接口）打印出来，方便你确认初始状态。这就是 README 里那张 CLI 截图（`wireguard_cli.png`）的来历（[6.test/README.md:22-26](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/6.test/README.md#L22-L26)）。
- 波特率 115200 不是 minicom 默认值，必须显式设置。调试脚本里写得很清楚（[6.test/dump_packet.UART.py:30](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/6.test/dump_packet.UART.py#L30)）：`ComPort.baudrate = 115200  # set Baud rate to 115200, fixed on FPGA side`。

> 进阶提示：UART 在本项目是「双用途」的——既能当 CLI 字符终端，也能切到二进制「特殊模式」在线烧写 IMEM、读写 DMEM/CSR（详见 u2-l5）。本讲只用它的字符 CLI 模式。

#### 4.1.4 代码实践

**实践目标**：确认 CLI 固件在启动时打印了什么、在等什么。

**操作步骤**：

1. 打开 [2.sw/app/main.cpp](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp)，定位 L790-815 的启动序列。
2. 数一数：从硬件 ID 校验到打出 `(wireguard-fpga)#` 提示符，固件按顺序做了哪几件事。
3. 打开 [6.test/README.md](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/6.test/README.md)，找到 L19 的 `minicom -D /dev/ttyUSB0` 与 L30 提到的 115200 波特率。

**需要观察的现象**：固件先把硬件签名读出来比对，再打横幅，再初始化并回显网络配置，最后才进入「等待命令」的主循环。

**预期结果**：你会理解 CLI 提示符 `(wireguard-fpga)#` 是固件主动打印的，而不是 minicom 的功能；板子复位一次，这段话就会重新打印一次。

**待本地验证**：若有板子，连上 minicom 后按一次复位键，应能在终端里看到横幅与 `Network configuration:` 块逐行出现。

#### 4.1.5 小练习与答案

**练习**：连上 minicom 后什么都没显示，可能的原因有哪些？至少列出两条。

> **参考答案**：(1) 波特率不是 115200（板子侧固定 115200，minicom 默认常是 9600/38400）；(2) 串口设备名不对（不是 `/dev/ttyUSB0`，或当前用户无权限访问，需加入 `dialout` 组）；(3) 板子没上电或没复位——固件只在启动时打印一次横幅，连上之后才上电才会触发打印；(4) USB 线只供电不传数据。

---

### 4.2 CLI 配置命令：network / routes / cryptokeys

#### 4.2.1 概念说明

连上 CLI 后，输入 `help` 会列出全部命令（[2.sw/app/main.cpp:918-933](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L918-L933)）。其中最核心的是三组「`show` 查 / `config` 改」命令，对应节点要配的三张表：

| 命令 | 作用 | 背后的表 |
|------|------|----------|
| `config network` | 配置本节点的网络身份（IP/掩码/MAC/网关/默认接口） | `net_config`（软件结构体，初始化 PHY/MAC 用） |
| `config routes` | 配置路由表的一条表项：目的网段走哪个 peer、从哪个接口发出 | `routing_table`（64 条，CSR external 表） |
| `config cryptokeys` | 配置一个 WireGuard peer 的全部参数：本地/远端地址、加解密密钥 | `cryptokey_table`（64 条，CSR external 表） |

每条 `config` 命令本质上是一个**交互式问卷**：固件逐个打印带默认值 `[...]` 的提示，你直接回车就采用默认值，敲新值就覆盖。这种方式对串口终端很友好——不需要记参数顺序。

> 名词解释：**external 表**。U1-l4 提过 CSR 是软硬件的桥梁。普通 CSR 寄存器小小的、数量少；但路由表/密钥表动辄几十上百条、每条几十字节，不适合做成普通寄存器，于是 SystemRDL 用 `external regfile` 声明它们，在 RTL 里落地成一块**双口 RAM**（见 [3.build/csr_build/csr.rdl:527-533](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl#L527-L533)）。软件经 CSR 写入，硬件数据面直接读 RAM 查找。这块的细节留到 u4-l6。

#### 4.2.2 核心流程

三条命令的问卷要点（以 README 里左节点的实录为准）：

**① `config network`**（[6.test/README.md:33-47](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/6.test/README.md#L33-L47)）：

| 提示项 | 含义 | 左节点示例 |
|--------|------|-----------|
| IP address | 本节点网口 IP | `192.168.1.98` |
| Subnet mask | 子网掩码 | `255.255.255.0` |
| Generate new MAC? | 是否随机生成 MAC 后两字节 | `y` → `CC:BA:CA:CA:BD:AF` |
| Default gateway | 默认网关 | `192.168.1.254` |
| Default interface (0-7) | 默认出口接口编号 | `1`（即 eth1） |

「Default interface」的编号 0-7 对应 DPE 内部地址（[1.hw/ip.infra/dpe_pkg.sv:46-50](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/dpe_pkg.sv#L46-L50)）：0=CPU、1=eth1、2=eth2、3=eth3、4=eth4，5/6/7 是组播/广播组。填 `1` 表示本节点自身产生的流量默认从 eth1 发出。

**② `config routes`**（[6.test/README.md:73-82](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/6.test/README.md#L73-L82)）：

| 提示项 | 含义 | 左节点示例 |
|--------|------|-----------|
| Entry index (0-63) | 写第几条路由（共 64 条） | `0` |
| Destination IP / Subnet mask | 目的网段 | `192.168.0.0 / 255.255.255.0` |
| Peer index (0-63) | 命中后归属哪个 WG peer | `1` |
| Destination interface (0-7) | 从哪个接口发出 | `6`（组播 eth2+eth4，见下面映射） |

「Destination interface」的 0-7 同样映射到 DPE 地址，但 `show_routes` 会把它翻译成直观的「哪些口亮灯」记号。固件里的 `switch (dst)` 把数字映射成字符串（[2.sw/app/main.cpp:364-389](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L364-L389)）：

| dst | 记号 | 含义 |
|-----|------|------|
| 0 | `[0....]` | 送 CPU |
| 1 / 2 / 3 / 4 | `[.1...]` / `[..2..]` / `[...3.]` / `[....4]` | 单发 eth1/2/3/4 |
| 5 | `[.1.3.]` | 组播 eth1+eth3 |
| 6 | `[..2.4]` | 组播 eth2+eth4 |
| 7 | `[.1234]` | 广播全部 4 个以太网口 |

所以左节点填 `6` 即 `[..2.4]`，表示这一类包从 eth2 和 eth4 同时发出。

**③ `config cryptokeys`**（[6.test/README.md:88-124](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/6.test/README.md#L88-L124)）：这一条问得最多，因为它要完整描述「我是谁、对方是谁、用什么密钥」。要点：

- **Entry index (1-63)**：注意从 1 开始（0 留空），共 64 条。
- **Local / Remote 的 MAC、IP、port、ID**：成对出现，描述本端与对端的端点身份。端口固定 `51820`（WireGuard 标准端口）。
- **Encryption key（8 段 × 8 hex 数字）**：加密用的密钥，256 位 = 32 字节，分 8 次每次输 4 字节。
- **Decryption key（8 段）**：解密用的密钥，同样是 256 位。
- **Reset send/recv counters?**：是否清零收发计数器（重置会话状态）。

这里最关键、也最容易配错的就是**加密密钥与解密密钥的关系**——它是本讲综合实践的核心，下面 4.3 和第 5 节会展开。

#### 4.2.3 源码精读

命令分派是个简单的 `if-else` 字符串比较链（[2.sw/app/main.cpp:884-937](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L884-L937)）：

```c
      uart_rx_len = uart_recv(csr, uart_rx_data);
      if (uart_rx_len) {
         if (strcmp(uart_rx_data, "test chacha20poly1305\n") == 0) { ... }
         ...
         } else if (strcmp(uart_rx_data, "config network\n") == 0) {
            config_network(csr, &net_config);
         } else if (strcmp(uart_rx_data, "config routes\n") == 0) {
            config_routes(csr);
         } ...
            uart_send(csr, "(wireguard-fpga)# ");
      }
```

注意每条命令末尾要带 `\n`（回车），分派靠 `strcmp` 精确匹配；匹配不上且长度 >1 就回 `Unknown command`。处理完一条后会重新打印提示符。

`config_network` 是最直观的「问一句、读一句、存一句」问卷（[2.sw/app/main.cpp:301-341](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L301-L341)）。以 IP 那一项为例：

```c
   uart_send(csr, "  IP address [");
   uart_send_ip(csr, config->ip);     // 方括号里打印当前值（默认值）
   uart_send(csr, "]: ");
   while (!uart_recv(csr, uart_rx_data));
   net_str_parse_ip(uart_rx_data, &config->ip);  // 解析输入，空串则保留默认
```

其中「生成新 MAC」时会调用随机数源取两个字节当 MAC 后缀（[2.sw/app/main.cpp:320-324](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L320-L324)）——这就是 README 里回车 `y` 后 MAC 变成 `CC:BA:CA:CA:BD:AF` 的来历（前 4 字节是固定厂头 `CC:BA:CA:CA`，后 2 字节随机）。

`config_routes` 与 `config_cryptokeys` 多了一个关键动作——**改表前先暂停数据面**（[2.sw/app/main.cpp:406-410](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L406-L410)、[2.sw/app/main.cpp:543-544](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L543-L544)）：

```c
void config_routes(volatile csr_vp_t* csr) {
   ...
   csr->dpe->fcr->pause(1);          // ① 置 pause=1，请求数据面停下来
   while (!csr->dpe->fcr->idle());   // ② 轮询 idle，等它真的停下来
   ...   // ③ 安全地改路由表
   csr->dpe->fcr->pause(0);          // ④ 改完，恢复数据面
}
```

这四步就是 U3 会讲的 **FCR（Flow Control Register）原子更新握手**：数据面正在线速跑，如果贸然改它的查找表，可能出现「改到一半」的不一致状态；所以先 pause→等 idle→改→恢复。本讲你只需记住「改路由/密钥表时，CLI 会自动暂停数据面」。

#### 4.2.4 代码实践

**实践目标**：把三条命令的「问题清单」与背后的字段对上号。

**操作步骤**：

1. 打开 [6.test/README.md:33-124](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/6.test/README.md#L33-L124)，把左节点 `config network` / `config routes` / `config cryptokeys` 三段交互里**每一个提示行**摘出来。
2. 打开 [2.sw/app/main.cpp](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp)，在 `config_network`（L301）、`config_routes`（L406）、`config_cryptokeys`（L540）里找到每个提示对应的 `uart_send` 与解析调用。
3. 打开 [3.build/csr_build/csr.rdl](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl)，在 `routing_table`（L527）与 `cryptokey_table`（L585 起）里找到 `ip` / `mask` / `peer_idx` / `dst` 与 `encrypt_key_*` / `decrypt_key_*` 字段。

**需要观察的现象**：CLI 每问一个问题，背后都对应 `csr.rdl` 里一个具体字段；固件读/写这个字段就是经 HAL 指针链 `csr->routing_table->entry[i]->ip->ip(...)`（见 [2.sw/app/main.cpp:451-454](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L451-L454)）。

**预期结果**：你会建立「CLI 提示 ↔ RDL 字段 ↔ HAL 指针」三者的对应，理解每条命令最终都是在写 CSR 寄存器/RAM。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `config cryptokeys` 的 Entry index 提示是 `(1-63)`，而 `config routes` 是 `(0-63)`？

> **参考答案**：cryptokey 表第 0 项被保留（不用于真实 peer），所以可写的是 1-63；routing 表从 0 开始用满 64 项。这与底层表大小一致（都是 64 条），只是起点约定不同（见 [2.sw/app/main.cpp:547-550](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L547-L550) 的 `str_parse_uint32(..., 1, 63)` 与 [2.sw/app/main.cpp:413-416](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L413-L416) 的 `..., 0, 63`）。

**练习 2**：改路由表时，如果省掉 `csr->dpe->fcr->pause(1)` 那一步直接写表，会出什么风险？

> **参考答案**：数据面可能正好在读这条表项做路由查找，写入是分多次写多个字段（ip/mask/peer_idx/dst）完成的，中途会出现「半新半旧」的表项，导致包被错误转发或命中错误的 peer。所以必须先 pause 等数据面 idle，保证「读不到半成品」。这就是 U3 要讲的 FCR 原子更新。

---

### 4.3 两节点验证拓扑与加密隧道确认

#### 4.3.1 概念说明

配好三张表只是「单节点就绪」。要验证 WireGuard 隧道是否真的工作，至少需要**两个节点**，让它们互相加密通话。`6.test/README.md` 顶部给出了一张测试拓扑图（[6.test/README.md:7-14](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/6.test/README.md#L7-L14)），两个 AX7201 节点用以太网线连起来，各自配成左/右节点，背后各挂一台主机（host A / host B）。从 host A `ping` host B，包要穿过两个 FPGA 节点之间的加密隧道。

`1.hw/README.md` 还给了一个更完整的四节点示例拓扑（[1.hw/README.md:106-113](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/README.md#L106-L113)），用来逐步讲解一个包从握手到加密传输的全过程（55 步）。本讲只取其中「两个 peer + 两台主机」的最小验证场景。

本节要解决的核心问题：**为什么左节点的「加密密钥」必须等于右节点的「解密密钥」，反之亦然？**

#### 4.3.2 核心流程

两节点拓扑与密钥流向：

```
   host A                  左节点 (peer A)                右节点 (peer B)                  host B
  192.168.0.1   ──明文──▶ ┌─────────────────┐   ──加密隧道──▶ ┌─────────────────┐  ──明文──▶ 192.168.0.2
                            │ 加密用 encKey_L  │   (UDP/WG)     │ 解密用 encKey_L  │
                            │ 解密用 encKey_R  │ ◀───────────── │ 加密用 encKey_R  │
                            └─────────────────┘                └─────────────────┘
                              192.168.1.98                         192.168.1.99
```

读图要点：

- 左节点**发**包时用 `encKey_L` 加密 → 这些密文到达右节点，右节点必须用**同一个** `encKey_L` 来解密。所以：**左节点的 Encryption key = 右节点的 Decryption key**。
- 反方向同理：右节点用 `encKey_R` 加密 → 左节点用 `encKey_R` 解密。所以：**右节点的 Encryption key = 左节点的 Decryption key**。
- 把两张表对照看，左/右的 enc/dec 恰好**交叉互换**——这就是「互为镜像」。

把 README 里的真实值填进去核对（[6.test/README.md:119-120](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/6.test/README.md#L119-L120) 与 [6.test/README.md:175-176](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/6.test/README.md#L175-L176)）：

| | 左节点 | 右节点 |
|---|--------|--------|
| Encryption key | `0x0123..CDEF`（重复 4 次） | `0xFEDC..3210`（重复 4 次） |
| Decryption key | `0xFEDC..3210`（重复 4 次） | `0x0123..CDEF`（重复 4 次） |

可见左 enc = 右 dec = `0x0123..`，左 dec = 右 enc = `0xFEDC..`，完美交叉。Local/Remote 的 MAC、IP、ID 同样成对互换（左的 Remote 即右的 Local）。

> 注意：这组 `0123.../FEDC...` 是 README 用的**演示密钥**，仅用于验证链路打通。真实场景里这两个 256 位密钥应由 WireGuard 握手协商产生（见 U6 软件控制面）。本讲之所以能直接手填静态密钥做 PoC 验证，是因为当前固件提供了 `config cryptokeys` 这个「手动配对」入口。

验证手段分两层：

1. **功能层**：在 host A 上 `ping 192.168.0.2`（host B），能收到 reply 即隧道通了（[6.test/README.md:184-186](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/6.test/README.md#L184-L186)）。
2. **加密层**：在两节点之间的链路上用 Wireshark 抓包，确认看到的是 **WireGuard 加密数据包**（UDP 端口 51820，载荷不可读），而不是明文 ICMP（[6.test/README.md:188-192](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/6.test/README.md#L188-L192)）。只有抓包确认「中间链路是密文」才算真正验证了加密隧道。

#### 4.3.3 源码精读

`1.hw/README.md` 用 5 个阶段、55 步完整刻画了一个包穿过系统的全过程（[1.hw/README.md:115-120](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/README.md#L115-L120) 概览，正文 L122-190 逐步展开）。与本讲验证目标最相关的两段：

- 左节点加密发出（L168-177）：用户 ICMP 包从 eth2 进，Header Parser 识别类型，IP Lookup 定位到目标 peer 与出口接口，**ChaCha20-Poly1305 Encryptor 用对端密钥加密并加认证 tag**，再封装成 WG/UDP/IP/Eth 包从 eth1 发向右节点。
- 右节点解密投递（L181-190）：右节点 eth1 收到，Disassembler 拆出密文，**Decryptor 解密并验证 tag**，IP Lookup 按源 IP 查 cryptokey 表决定接受/拒绝，最后从 eth2 送给 host B。

这两段正好对应 4.3.2 里「左 enc / 右 dec」的密钥配对关系——加密端用的密钥，必须等于解密端用来解密的密钥。

> **关于当前 HEAD 的现状（Phase1 PoC）**：与 u1-l2/u1-l4 一致，本讲也要如实说明——当前 HEAD 编入设计的是直通的 `dpe_dummy_switch`，完整的 WG 解封装/加解密/路由查找链已写好但被 `top.filelist` 注释、尚未上线。因此在**当前这次实际烧写的 bitstream** 上，软件层面做了简化的桥接来演示链路连通：固件主循环里对从 eth1/eth2 收到的包直接改目的地址、置 `bypass_all=1` 转发出去（[2.sw/app/main.cpp:870-878](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/2.sw/app/main.cpp#L870-L878)）：
>
> ```c
>          if (eth_packet_rx.src == DPE_ADDR_ETH_1) {
>             eth_packet_rx.dst = DPE_ADDR_ETH_2;
>             eth_packet_rx.bypass_all = 1;
>             eth_send_packet(csr, &eth_packet_rx);
>          } else if (eth_packet_rx.src == DPE_ADDR_ETH_2) {
>             ...
>          }
> ```
>
> 也就是说，README 描述的三条 `config` 命令与镜像密钥配对**是设计目标下的完整流程**（CLI 与表结构都已就绪、可用），但「ping 抓包确认加密」这一步在当前 PoC bitstream 上会表现为**明文直通**而非真正的 WG 加密。等加密流水线切回上线（见 U4/U5），同一套 CLI 配置就会驱动真正的加密隧道。这一点请你在实测时心里有数。

#### 4.3.4 代码实践

**实践目标**：把左/右节点的镜像关系手工核对一遍，确认「交叉相等」。

**操作步骤**：

1. 打开 [6.test/README.md](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/6.test/README.md)，分别找到左节点（L88-124）与右节点（L144-180）的 `config cryptokeys` 录。
2. 列两张表，各取 Local/Remote 的 MAC、IP、ID，以及 Encryption/Decryption key。
3. 逐项验证：左的 Remote == 右的 Local？左 enc == 右 dec？左 dec == 右 enc？

**需要观察的现象**：两节点的成对字段恰好交叉相等；任何一个字段没镜像（比如把左 enc 错填成右 enc），握手/解密就会失败。

**预期结果**：你会得出——**镜像不是可选项，而是对称密码体制的硬性要求**：A 加密用的密钥必须等于 B 解密用的密钥，反方向亦然。

**待本地验证**：若有双板，故意把右节点的 Decryption key 填错（不同于左节点 Encryption key），再 ping，应观察不到 reply，从而反向印证镜像的必要性。

#### 4.3.5 小练习与答案

**练习 1**：如果左节点和右节点都把 Encryption key 和 Decryption key 填成同一个值（比如都填 `0x0123..`），还能互通吗？

> **参考答案**：单向能通、反向不通。左 enc=`0123`、右 dec 也需=`0123` 才能解左发来的包——若右 dec 填了 `0123` 则这一向通；但右 enc 也=`0123` 时，左 dec 必须=`0123` 才能解右发来的包，可左 dec 若也是 `0123` 则反向也通。所以「两侧 enc/dec 全填同一个值」在这种特殊情况下恰好双向都能通，但**这等于退化为单密钥**，失去了 WireGuard 每方向独立密钥的设计意义；正常应配两个不同的方向密钥并交叉镜像。

**练习 2**：为什么验证加密隧道必须「抓包」，光 ping 通不够？

> **参考答案**：ping 通只能证明「包从 A 到了 B」，不能证明「中间链路是密文」。如果数据面其实是明文直通（如当前 PoC 的 `bypass_all` 桥接），ping 也会通，但根本没有加密。只有用 Wireshark 在两节点之间的物理链路上抓包，确认看到的是 WireGuard 加密数据包（UDP/51820，载荷密文），才能证明隧道确实在加密。这正是 README 配那张 Wireshark 截图（[6.test/README.md:188-192](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/6.test/README.md#L188-L192)）的原因。

---

## 5. 综合实践

**任务**（本讲指定的主实践）：按两节点拓扑写出左/右节点的完整 CLI 配置脚本，并解释为何两节点的加密/解密密钥必须互为镜像。

**操作步骤**：

1. 假设拓扑如下（IP 自拟，保持成对镜像即可）：
   - 左节点：节点 IP `192.168.1.98`，default interface `1`；身后 host A 为 `192.168.0.1`。
   - 右节点：节点 IP `192.168.1.99`，default interface `1`；身后 host B 为 `192.168.0.2`。
   - 两节点之间的 WG 链路走 UDP/51820。
2. 为**左节点**写一份按顺序敲入的 CLI 脚本，依次覆盖：`config network` → `config routes` → `config cryptokeys`，参考 [6.test/README.md:33-124](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/6.test/README.md#L33-L124)。
3. 为**右节点**写一份对应脚本，参考 [6.test/README.md:129-180](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/6.test/README.md#L129-L180)。
4. 在脚本旁画一张「字段镜像对照表」，证明左右成对字段交叉相等。

**左节点脚本骨架（示例，非项目原有代码）**：

```
config network
192.168.1.98        # IP
255.255.255.0       # mask
y                   # gen MAC
192.168.1.254       # gateway
1                   # default interface = eth1

config routes
0                   # entry index
192.168.0.0         # 目的网段（host 所在网）
255.255.255.0       # mask
1                   # peer index
6                   # dst interface (eth2+eth4)

config cryptokeys
1                   # entry index
CCBACACABDAF        # local MAC
192.168.1.98        # local IP
51820               # local port
CCBACA01            # local ID
CCBACACAFA89        # remote MAC（= 右节点 local MAC）
192.168.1.99        # remote IP
51820               # remote port
CCBACA02            # remote ID（= 右节点 local ID）
01234567 89ABCDEF 01234567 89ABCDEF 01234567 89ABCDEF 01234567 89ABCDEF  # enc key
FEDCBA98 76543210 FEDCBA98 76543210 FEDCBA98 76543210 FEDCBA98 76543210  # dec key
y                   # reset counters
```

**镜像对照表**（核心答案）：

| 字段 | 左节点 | 右节点 | 镜像关系 |
|------|--------|--------|----------|
| Local MAC | `CCBACACABDAF` | `CCBACACAFA89` | 左 Local = 右 Remote |
| Local IP | `192.168.1.98` | `192.168.1.99` | 左 Local = 右 Remote |
| Local ID | `CCBACA01` | `CCBACA02` | 左 Local = 右 Remote |
| Encryption key | `0x0123..CDEF` | `0xFEDC..3210` | **左 enc = 右 dec** |
| Decryption key | `0xFEDC..3210` | `0x0123..CDEF` | **左 dec = 右 enc** |

**为什么必须镜像**：WireGuard 用对称密码（ChaCha20-Poly1305）——加密和解密用的是同一把密钥。左节点发出去的密文是用「左 enc」加密的，右节点只有拿「同一把密钥」当 dec 才能解开，所以左 enc 必须等于右 dec；反方向同理。任何一边填反、填错，对应方向就解不开，ping 就丢包。这就是「互为镜像」的物理含义——它不是人为约定，而是对称加解密的数学必然。

> 待本地验证：在真实双板上按此脚本配通后，从 host A `ping 192.168.0.2`，并在节点间链路抓包；当前 Phase1 PoC bitstream 会表现为明文直通（见 4.3.3），待加密流水线上线后同脚本即驱动真加密隧道。

## 6. 本讲小结

- 上板后用 `minicom -D /dev/ttyUSB0`（**115200 波特率**）连上的 CLI，跑在板内 RISC-V 软 CPU 上；固件启动时先校验硬件 ID（`0xCCBA/0xCACA`），再打横幅、初始化网络、给出 `(wireguard-fpga)#` 提示符。
- 三组核心命令：`config network` 配本节点身份（IP/掩码/MAC/网关/默认接口）、`config routes` 配路由表（目的网段→peer→出口接口）、`config cryptokeys` 配 WG peer（本地/远端身份 + 加解密 256 位密钥）。每条命令都是带默认值 `[...]` 的交互问卷。
- `config routes` / `config cryptokeys` 改表前会先 `fcr->pause(1)` 等到 `idle`、改完再 `pause(0)`——这是 FCR 原子更新握手，防止数据面读到「半新半旧」的表项（U3 详讲）。
- 「Default interface」与「Destination interface」的 0-7 编号对应 DPE 内部地址：0=CPU、1-4=eth1-4、5/6=组播、7=广播；固件会把数字翻译成 `[.1.3.]` 之类的直观记号。
- 两节点验证拓扑里，**左右节点的加解密密钥必须交叉镜像**：左 enc = 右 dec、左 dec = 右 enc，Local/Remote 字段也成对互换——这是对称密码体制的硬性要求。
- 验证加密隧道要「ping 通 + Wireshark 抓包确认密文」双管齐下；当前 Phase1 PoC bitstream 用 `dpe_dummy_switch` + 软件桥接直通，同一套 CLI 配置在加密流水线上线后才会驱动真正的加密隧道。

## 7. 下一步学习建议

- 想理解 CLI 每个字段最终写到哪个寄存器？→ 进 U3：[3.build/csr_build/csr.rdl](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl) 里 `routing_table` / `cryptokey_table` 的 SystemRDL 定义是单一真源，u3-l1 系统讲解语法。
- 想知道「pause/idle」原子更新在硬件侧怎么实现？→ u3-l4（FCR 流控寄存器与原子更新）会拆解 DPE 的暂停握手。
- 想看 55 步包处理的全貌？→ 重读 [1.hw/README.md:106-190](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/README.md#L106-L190)，U4（数据面引擎）会逐块讲解 Header Parser / Encryptor / Decryptor。
- 想用脚本自动抓包验证？→ 看 [6.test/dump_packet.UART.py](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/6.test/dump_packet.UART.py) 与 [6.test/README.md:194-201](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/6.test/README.md#L194-L201) 的测试脚本清单，U7 会讲仿真协同验证体系。
- 至此 U1 入门层完结：你已经掌握了项目定位、目录、硬件平台、构建流程与实验室运行。U2 将带你进入 SoC 硬件架构，从 `top.sv` 顶层模块开始拆解芯片内部。
