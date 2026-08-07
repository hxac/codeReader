# 两节点实验室端到端验证

## 1. 本讲目标

本讲是 Unit 8（系统集成）的收尾篇，把前面所有单元的成果——硬件 RTL、软件固件、CSR HAL、构建工具链、仿真测试台——汇聚到一次「真板子 + 真网线」的端到端验证上。读完本讲，你应当能够：

1. 按拓扑把两台 Alinx AX7201 板卡连起来，烧写 bitstream 并经 UART CLI 把两个节点配成一个 WireGuard VPN 隧道。
2. 用 `config network` / `config routes` / `config cryptokeys` 三类命令完成左、右节点的完整配置，并说清每条命令落到 CSR 的哪张表。
3. **讲清最关键的一点**：为什么左右节点的 `encrypt_key` 与 `decrypt_key` 必须「交叉镜像」——这是对称密码 ChaCha20-Poly1305 的硬性要求。
4. 用 `ping` 验证隧道连通，用 Wireshark 在网线上抓包确认载荷确已加密。
5. 把这次实网验证与 Unit 7 的仿真 PCAP 回放/录制验证对应起来，理解二者各自验证了哪一层。

> 现状提醒（承接 u2-l1、u4-l5、u8-l3）：当前 HEAD 处于 Phase1 PoC，bitstream 里实际综合的是直通模块 `dpe_dummy_switch`，数据面走「明文直通 + 软件桥接」。本讲描述的 CLI 配置（尤其是 cryptokeys 的密钥镜像）是**正确且必要**的，待 Unit 4/Unit 5 的加解密流水线上线后，这套配置无需改动即可驱动真正的加密隧道；当前 PoC 阶段抓到的「加密包」实为明文。

## 2. 前置知识

在动手前，请确认你已经理解下面这些概念（均在前序讲义建立）：

- **HW/SW 分区与 DPE**（u2-l1、u4-l1）：控制面软 CPU 跑 WireGuard 协议，数据面 DPE 做线速转发；二者经 CSR HAL 桥接。
- **DPE 接口地址编码**（u4-l1、u4-l3）：`dst` 字段 0=CPU、1-4=eth1-eth4、5=MCAST_13(eth1+eth3)、6=MCAST_24(eth2+eth4)、7=BCAST(全部)。
- **CSR 三类配置表**（u3-l1、u4-l6）：`network` 落到普通 CSR 寄存器；`routes` 落到 `routing_table`（external regfile，tdp_ram，64 条目）；`cryptokeys` 落到 `cryptokey_table`（external regfile，tdp_ram，64 条目）。
- **FCR 原子更新**（u3-l4、u6-l4）：改路由表/密钥表前，CPU 经 `dpe.fcr` 的 `pause`/`idle` 握手在包边界暂停数据面，再原子换表。
- **AEAD/ChaCha20-Poly1305**（u5-l1）：对称加密，加密与解密用**同一把** 256 位 key；96 位 nonce 由单调 send counter 派生。
- **CLI 上板流程**（u1-l5）：`minicom -D /dev/ttyUSB0` 连 CLI，三条问卷式命令交互配置。
- **仿真 PCAP 验证**（u7-l4、u7-l5）：`VUserMainPcap` 用 PCAP 回放/录制做端到端仿真验证。

如果上面任何一项不熟悉，建议先回看对应讲义。

## 3. 本讲源码地图

本讲以两份 README 为主线、一份仿真 C++ 与两份规格文件为佐证：

| 文件 | 作用 |
|------|------|
| `6.test/README.md` | **本讲主纲**：两节点拓扑、上板、三类 CLI 配置的完整交互记录、ping 与 Wireshark 验证。 |
| `1.hw/README.md` | 给出 4 节点示例拓扑与「55 步包处理」全流程，是理解隧道内 ICMP 包走向的权威说明。 |
| `4.sim/usercode/VUserMainPcap.cpp` | 仿真侧的 PCAP 回放（node1/3）与录制（node2/4），对应实网验证的「数字孪生」。 |
| `3.build/csr_build/csr.rdl` | `routing_table` 与 `cryptokey_table` 的字段规格真源，证明 CLI 配置项落地到哪些字段。 |
| `4.sim/tools/gen_udp_pcap.py` | 仿真用 UDP/IPv4 帧与 PCAP 生成器，其默认 MAC/IP 与 `VUserMainPcap` 的 node 定义成对匹配。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：先走一遍两节点的拓扑与三类配置（4.1），再聚焦最关键的密钥镜像关系（4.2），最后把实网验证与仿真 PCAP 验证对应起来（4.3）。

### 4.1 两节点拓扑与三类 CLI 配置

#### 4.1.1 概念说明

「两节点实验室」是 wireguard-fpga 的最小可验证形态：用两块 AX7201 板卡充当两个 WireGuard peer，中间用网线连成隧道，两端各挂一台主机（host A / host B）。隧道承载的是用户数据网段（本例 `192.168.0.0/24`），而两块板卡之间用另一个网段（本例 `192.168.1.0/24`）做外层传输。

每个节点都要做三类配置，恰好对应 CSR 里的三类存储：

- **`config network`**：配本节点的网络身份（IP、掩码、MAC、网关、默认接口），落到普通 CSR 寄存器。
- **`config routes`**：配「目的网段 → 哪个 peer → 从哪个接口发出」的转发条目，落到 `routing_table`（64 条目 external 表）。
- **`config cryptokeys`**：配每个 peer 的本端/远端身份与 256 位加解密密钥及收发计数器，落到 `cryptokey_table`（64 条目 external 表）。

后两张表是 external regfile，存储体是手写双口 RAM（见 u4-l6）；改表前固件会用 FCR 的 `pause`/`idle` 握手把数据面停在包边界，再原子写入（见 u3-l4、u6-l4）。

#### 4.1.2 核心流程

两节点从上电到隧道可用的流程：

```
1. 按拓扑图连线（两板之间、板与主机之间）
2. 各自烧写 bitstream（见 u8-l1 的 MakefileHW program 目标）
3. minicom -D /dev/ttyUSB0 连上左节点 CLI（115200，见 u1-l5）
4. 左节点：config network  → config routes → config cryptokeys
5. 切到右节点串口，重复第 4 步（注意：密钥要交叉镜像，见 4.2）
6. host A 执行 ping <host B 内网 IP>
7. 在两板之间的网线上用 Wireshark 抓包，确认载荷为密文
```

CLI 是问卷式交互：每条命令逐项提问，方括号里是默认值，直接回车即采用默认。下文 4.1.3 会逐段贴出真实交互记录。

#### 4.1.3 源码精读

**(a) 拓扑与上板。** `6.test/README.md` 开篇即给出测试拓扑图与上板要求：

> [6.test/README.md:7-14](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/6.test/README.md#L7-L14) — 按拓扑连好 Alinx AX7201 板卡后，按构建流程烧写 FPGA；并展示测试拓扑图（两节点 + 两台主机）。

接着用串口终端连 CLI：

> [6.test/README.md:16-20](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/6.test/README.md#L16-L20) — AX7201 的 USB UART 即 WireGuard CLI，用 `minicom -D /dev/ttyUSB0` 连接；复位后会打印欢迎信息。

**(b) 左节点 `config network`。** 配本端身份，IP `192.168.1.98`、默认接口 `1`，并生成新 MAC `CC:BA:CA:CA:BD:AF`（右节点稍后要用到这个 MAC）：

> [6.test/README.md:30-48](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/6.test/README.md#L30-L48) — 左节点网络配置交互记录，IP `192.168.1.98`、MAC `CC:BA:CA:CA:BD:AF`、默认接口 `1`。

**(c) 右节点 `config network`。** 顺手先把右节点也配上，IP `192.168.1.99`、MAC `CC:BA:CA:CA:FA:89`，这样左节点配 cryptokeys 时就能填入对端 MAC：

> [6.test/README.md:50-68](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/6.test/README.md#L50-L68) — 右节点网络配置，IP `192.168.1.99`、MAC `CC:BA:CA:CA:FA:89`。

**(d) 左节点 `config routes`。** 配一条路由：目的网段 `192.168.0.0/24` → peer index `1` → 目的接口 `6`：

> [6.test/README.md:70-83](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/6.test/README.md#L70-L83) — 左节点路由表条目 0：`IP 192.168.0.0, Mask 255.255.255.0, Peer 1, Dst interface 6`。

这里的四个字段直接对应 `csr.rdl` 里 `routing_table` 的四个寄存器——目的 IP、掩码、peer 索引（6 位，0-63）、目的接口（3 位 `dst`）：

> [3.build/csr_build/csr.rdl:527-577](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl#L527-L577) — `routing_table` external regfile 的 `ip` / `mask` / `peer_idx[5:0]` / `dst[2:0]` 四个字段定义。

`dst=6` 按 dpe_pkg 编码是 `MCAST_24`（eth2+eth4），交互记录里显示的 `Destination interface: 6 [..2.4]` 正是用 `[..2.4]` 解码出 eth2、eth4 两个成员口（编码规则见 u4-l3）。需要说明的是，右节点那条路由条目交互中，行首显示的接口号与方括号解码存在一处不一致（输入 `2` 但表头仍显示 `6`，方括号 `[..2..]` 才反映真实成员 eth2），这处表头数字疑似 README 复制遗留，**待本地验证**以板子实际回显为准。

**(e) 左节点 `config cryptokeys`。** 这是最长的一条，逐项填本端/远端身份与 256 位加解密密钥：

> [6.test/README.md:85-124](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/6.test/README.md#L85-L124) — 左节点 cryptokey 条目 1 的完整交互。本端 `192.168.1.98 / CCBACA01`，远端 `192.168.1.99 / CCBACA02`，加密密钥 `0123…CDEF`×4，解密密钥 `FEDC…3210`×4，并复位收发计数器。

这些填入项同样在 `csr.rdl` 的 `cryptokey_table` 里有逐字段对应，包括 8 个 32 位字拼成的 256 位 `encrypt_key`、8 个字拼成的 256 位 `decrypt_key`：

> [3.build/csr_build/csr.rdl:703-789](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl#L703-L789) — `encrypt_key_255_224` … `encrypt_key_31_0` 共 8 个 32 位寄存器，组成 256 位加密密钥（`sw=rw; hw=r`，CPU 写、硬件读）。
>
> [3.build/csr_build/csr.rdl:791-877](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl#L791-L877) — `decrypt_key_255_224` … `decrypt_key_31_0` 共 8 个 32 位寄存器，组成 256 位解密密钥。

最后两项 `Reset send/recv counters? (y/n)` 选 `y`，对应把 `send_cnt` / `recv_cnt` 清零：

> [3.build/csr_build/csr.rdl:879-925](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/csr_build/csr.rdl#L879-L925) — `send_cnt_63_32/31_0` 与 `recv_cnt_63_32/31_0`，注意它们是 `sw=rw; hw=rw; we;`——CPU 与数据面都能读写，`send_cnt` 由加密器每发一包自增（用于派生 nonce），`recv_cnt` 用于接收侧防重放。

**(f) 右节点的 routes 与 cryptokeys。** 右节点路由条目把目的接口设为 `2`（eth2），cryptokeys 则把本端/远端身份对调，密钥也交叉（详见 4.2）：

> [6.test/README.md:126-180](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/6.test/README.md#L126-L180) — 右节点路由表条目（`dst=2`）与 cryptokey 条目 1 的完整交互，密钥与左节点交叉。

#### 4.1.4 代码实践

**实践目标**：建立拓扑与配置的全局心智图，确认三类命令各落到哪张 CSR 表。

**操作步骤**：

1. 打开 `6.test/README.md`，把左、右节点各执行过的命令按 `network → routes → cryptokeys` 整理成两张表。
2. 打开 `3.build/csr_build/csr.rdl`，在 `routing_table`（@0x0400）与 `cryptokey_table`（@0x2000）里找出每个 CLI 问卷项对应的字段名（如 CLI 的 "Peer index" 对应 `peer_idx[5:0]`）。
3. 标注哪些字段是 `sw=rw; hw=r`（CPU 写、硬件读的配置类），哪些是 `sw=rw; hw=rw; we;`（CPU 与硬件都能改的状态类，如计数器）。

**需要观察的现象**：你会看到 `encrypt_key`/`decrypt_key`/`local_*`/`remote_*`/`ip`/`mask` 全是配置类（硬件只读），唯独 `send_cnt`/`recv_cnt` 是双向可写的状态类。

**预期结果**：每一条 CLI 问卷项都能在 `csr.rdl` 里找到一一对应的字段，证明「CLI 配置 → HAL → external regfile → tdp_ram」这条链是闭合的（具体 HAL 调用序列见 u6-l4）。

> 若手边没有板子，本实践为「源码阅读型」，无需运行命令即可完成对照。

#### 4.1.5 小练习与答案

**练习 1**：左节点 `config routes` 里 "Destination interface (0-7)" 填的是 `6`，这个 `6` 在数据面里代表什么？

**参考答案**：按 dpe_pkg 编码，`dst=6` 是 `MCAST_24`，即把包复制发往 eth2 与 eth4 两个口（组播），CLI 回显的 `[..2.4]` 正是这两个成员口的解码。

**练习 2**：为什么 `send_cnt` / `recv_cnt` 的 RDL 属性是 `sw=rw; hw=rw; we;`，而不是像 `encrypt_key` 那样 `sw=rw; hw=r`？

**参考答案**：加密密钥是静态配置，只有 CPU 写、数据面读；而发送计数器要由数据面的加密器在每发一包后自增（用来派生下一包的 nonce），接收计数器要由解密器更新用于防重放，所以硬件也要能写。`we`（write-enable）保证 CPU 与硬件不会同时写冲突。CPU 在配置时复位它们，是为了让 nonce 从 0 重新开始，避免与历史包的 nonce 冲突。

---

### 4.2 加解密密钥的交叉镜像

#### 4.2.1 概念说明

这是整个两节点配置里**最容易配错、也最关键**的一环。WireGuard 数据面用 ChaCha20-Poly1305 做对称加密（见 u5-l1）。对称加密的特点是：**加密和解密用同一把密钥**。因此一个方向上的「加密密钥」，必须等于对端同一方向上的「解密密钥」。

每个节点在自己的 `cryptokey_table` 里同时存了两把 256 位密钥：

- `encrypt_key`：本节点**发出**数据时加密用的密钥。
- `decrypt_key`：本节点**收到**数据时解密用的密钥。

要让两节点互通，必须满足：

\[
\text{Left.encrypt\_key} = \text{Right.decrypt\_key}
\]

\[
\text{Left.decrypt\_key} = \text{Right.encrypt\_key}
\]

即两把密钥在左右节点之间**交叉镜像**。这正是本讲代码实践任务要验证的关系。

#### 4.2.2 核心流程

推理链（从「左节点发一个包给右节点」出发）：

```
左节点发出包 P：
  P = ChaCha20-Poly1305-Encrypt( key = Left.encrypt_key, plaintext, nonce )

包 P 经网线到达右节点，右节点要还原明文：
  plaintext = ChaCha20-Poly1305-Decrypt( key = Right.decrypt_key, P, nonce )

因为对称加密要求加解密同密钥：
  => Left.encrypt_key 必须等于 Right.decrypt_key   ……(结论一)

对称地，右节点发给左节点的包：
  => Right.encrypt_key 必须等于 Left.decrypt_key   ……(结论二)

两个结论合起来就是「交叉镜像」。
```

注意 nonce（96 位）由单调 `send_cnt` 派生（承接 u4-l5：`nonce = {32'd0, send_cnt}`），所以配置时复位计数器、让两侧都从 0 开始，才能保证 nonce 对得上。

#### 4.2.3 源码精读

把 `6.test/README.md` 里左右节点的 cryptokeys 回显并排，交叉关系一目了然：

> [6.test/README.md:117-123](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/6.test/README.md#L117-L123) — 左节点：`Encryption key: 0x0123456789ABCDEF…CDEF`、`Decryption key: 0xFEDCBA9876543210…3210`、Send/Recv counter 均为 `0`。
>
> [6.test/README.md:173-179](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/6.test/README.md#L173-L179) — 右节点：`Encryption key: 0xFEDCBA9876543210…3210`、`Decryption key: 0x0123456789ABCDEF…CDEF`、Send/Recv counter 均为 `0`。

整理成对照表：

| 节点 | encrypt_key（发出加密用） | decrypt_key（收到解密用） |
|------|---------------------------|---------------------------|
| Left  (192.168.1.98) | `01234567 89ABCDEF` ×4 | `FEDCBA98 76543210` ×4 |
| Right (192.168.1.99) | `FEDCBA98 76543210` ×4 | `01234567 89ABCDEF` ×4 |

可见：

- `Left.encrypt_key`（`0123…CDEF`）== `Right.decrypt_key`（`0123…CDEF`）✓ 满足结论一
- `Left.decrypt_key`（`FEDC…3210`）== `Right.encrypt_key`（`FEDC…3210`）✓ 满足结论二

这两把 256 位密钥在硬件里就是 `cryptokey_table` 的 16 个 32 位字（8 个 `encrypt_key_*` + 8 个 `decrypt_key_*`），加密器/解密器按 peer index（`tid`）从双口 RAM 的 B 口读取（见 u4-l5、u4-l6）。本端/远端身份字段（MAC/IP/port/ID）也成镜像：左节点的 `remote_*` 恰是右节点的 `local_*`，反之亦然——这保证封装出的外层以太网/IP/UDP 头能被对端正确识别。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：亲手验证左右节点 cryptokeys 的交叉镜像关系，并解释为何必须如此。

**操作步骤**：

1. 从 `6.test/README.md` 抄出左节点（L117-L123）与右节点（L173-L179）的 `encrypt_key` 与 `decrypt_key`。
2. 填下面这张对应表：

   | 关系 | 左节点字段 | 值 | 应等于 | 右节点字段 | 值 | 是否相等 |
   |------|-----------|----|--------|-----------|----|---------|
   | 左发→右收 | Left.encrypt_key | `0123…CDEF`×4 | | Right.decrypt_key | `0123…CDEF`×4 | ✓ |
   | 右发→左收 | Left.decrypt_key | `FEDC…3210`×4 | | Right.encrypt_key | `FEDC…3210`×4 | ✓ |

3. 用一句话解释：为何 `Left.encrypt_key` 必须等于 `Right.decrypt_key`？

**需要观察的现象**：两把密钥在左右节点间恰好交叉，没有任何一组是「左加密 == 右加密」（那样会导致双方都能加密却无人能解密）。

**预期结果（参考答案）**：因为 ChaCha20-Poly1305 是对称加密，加解密同密钥。左节点用 `Left.encrypt_key` 加密发出的包，到达右节点后必须用同一把密钥解密，而右节点解密用的是 `Right.decrypt_key`，故二者必须相等。反向同理。若配成「左加密 == 右加密」，则两个方向都无法解密，隧道完全不通；若只配对了一半，则单向通、反向丢包。

> 进阶思考：把左节点的 `encrypt_key` 与 `decrypt_key` 故意填成同一个值（即不做交叉），会发生什么？答：左节点发出用 K 加密的包，右节点用 K 解密成功（因为右的 decrypt 也被设成 K）；但右节点回包用 K 加密，左节点若 decrypt 也是 K 则也能解——表面上「能通」，但两个方向共用一把密钥，丧失了 WireGuard 每方向独立密钥的设计意图，且 nonce 计数器空间在两个方向上重叠，存在重放风险。生产环境必须严格按交叉镜像配置两把独立密钥。

#### 4.2.5 小练习与答案

**练习 1**：如果把右节点的 `encrypt_key` 误填成和左节点的 `encrypt_key` 一样（即 `0123…CDEF`），ping 会通吗？

**参考答案**：不会双向通。左→右方向：左用 `0123…` 加密，右的 `decrypt_key` 也是 `0123…`，能解密，这一向通；但右→左方向：右用 `0123…`（被误填的 encrypt）加密，左的 `decrypt_key` 是 `FEDC…`，解不开，回包全部丢弃。表现为 ping 只有去程、无回程（request 见不到 reply）。

**练习 2**：配置时为什么要选 `Reset send/recv counters? (y/n) [n]: y`？

**参考答案**：nonce 由 `send_cnt` 派生，复位计数器让两侧都从 nonce=0 开始。若不复位而沿用旧值，可能与之前会话用过的 nonce 重叠，违反「同一密钥下 nonce 不得复用」的要求，既会破坏 ChaCha20-Poly1305 的安全性，也可能让对端的防重放窗口（`recv_cnt`）直接丢弃新包。

---

### 4.3 端到端验证：ping、Wireshark 与仿真 PCAP 对照

#### 4.3.1 概念说明

配置完成后，验证分两层：

- **功能层**：host A `ping` host B 的内网 IP，能收到 ICMP Echo Reply，说明隧道双向连通。
- **加密层**：在两块板卡之间的网线上抓包，确认抓到的是 WireGuard 密文（UDP/51820 承载、载荷不可读），而不是明文 ICMP——这证明用户数据确实被封装加密了。

同时，这套实网验证与 Unit 7 的仿真 PCAP 验证是一对「数字孪生」：仿真用 `VUserMainPcap` 把合成的 UDP 帧回放进 node1 的 GMII、在 node2 录制收到的帧，用来回归测试数据面路径与延迟；实网则用真 ping + Wireshark 做最终验收。两者共享同一条 DPE 硬件路径，只是激励来源与观测手段不同。

> 再次提醒：当前 PoC bitstream 综合的是 `dpe_dummy_switch`（明文直通），所以现阶段实网抓包看到的载荷其实是明文；真正的密文要等 Unit 4/5 加解密流水线上线。本节描述的是目标行为与验证方法。

#### 4.3.2 核心流程

**实网验证流程**：

```
1. host A: ping 192.168.0.2   (host B 的内网 IP)
2. ICMP Echo Request 经左节点进入隧道：
   左节点 IP 查表 → 命中 peer 1 → ChaCha20-Poly1305 加密 → 封 WG/UDP/IP/Eth 头 → 从 eth 口发出
3. 在两板间的网线上 Wireshark 抓包：看到 UDP/51820 的 WG 密文包
4. 右节点收到 → 解封装 → Poly1305 验 tag → ChaCha20 解密 → 还原 ICMP → 发给 host B
5. host B 回 Echo Reply，反向走同一隧道
6. host A 收到 Reply，ping 通
```

`1.hw/README.md` 的「HW/SW Working Together」一节用 4 节点示例拓扑 + 55 步详细描述了这条链路，其中加密与封装在第 41-42 步、解密与验签在第 51 步。

**仿真验证流程（对照）**：

```
1. gen_udp_pcap.py 生成 test_udp_rand.pcap（合成 UDP/IPv4 帧）
2. VUserMainPcap 的 node1 回放该 PCAP → 注入 DPE 的 GMII
3. node2 录制收到的帧 → 写 node2_out.pcap
4. 比较 tx_node1.pcap 与 node2_out.pcap，测「开始到开始」延迟
```

#### 4.3.3 源码精读

**(a) 实网 ping 与 Wireshark。** `6.test/README.md` 给出验证命令与抓包截图说明：

> [6.test/README.md:182-192](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/6.test/README.md#L182-L192) — 从 host A `ping 192.168.0.2`；并展示在节点 `192.168.1.1` 上抓到的第一个加密包（ICMP Echo Request 已被封装成 WireGuard 密文）。

**(b) 55 步包处理里的加密/解密环节。** `1.hw/README.md` 用真实 Wireshark 录包拆解了包在系统里的全程走向，加密+封装与解密+验签两个关键环节：

> [1.hw/README.md:106-122](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/README.md#L106-L122) — 4 节点示例拓扑与包处理的几个阶段（握手发起/响应、数据加密隧道传输、解封装解密）。
>
> [1.hw/README.md:173-174](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/README.md#L173-L174) — 数据向：加密器据目标 peer 与对应密钥加密并加认证 tag，封装器再套 WG/UDP/IP/Eth 头发出。
>
> [1.hw/README.md:186-187](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/README.md#L186-L187) — 解密器解密、验 tag 通过后才转发；其中加密密钥/解密密钥正是从 `cryptokey_table` 按 peer 取出（即 4.2 配置的那两把）。

注意：这 55 步描述的是**目标行为**；当前 PoC 因 `dpe_dummy_switch` 在线，加密/解密/查表各级实际被旁路，包以明文直通。

**(c) 仿真侧的 PCAP 回放/录制。** `VUserMainPcap.cpp` 用 4 个 VProc node 模拟两对收发：node1 回放 PCAP 给 node2 录制、node3 回放给 node4 录制。node 定义里硬编码了与 `gen_udp_pcap.py` 默认值成对匹配的 MAC/IP：

> [4.sim/usercode/VUserMainPcap.cpp:73-87](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/usercode/VUserMainPcap.cpp#L73-L87) — node1/node2 的 MAC（`D8:9E:F3:88:7E:C3` / `90:32:4B:07:0B:D1`）与 IP（`192.168.25.8` / `192.168.152.1`），与 PCAP 生成器默认值一致。
>
> [4.sim/usercode/VUserMainPcap.cpp:97-127](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/usercode/VUserMainPcap.cpp#L97-L127) — `VUserMain1`：读 `PCAP_IN_1`（默认 `./tools/test_udp_rand.pcap`）逐帧回放进 node1 的 GMII，同时把发出的帧录到 `tx_node1.pcap`。
>
> [4.sim/usercode/VUserMainPcap.cpp:132-156](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/usercode/VUserMainPcap.cpp#L132-L156) — `VUserMain2`：注册接收回调，把 node2 收到的帧录到 `node2_out.pcap`，并持续发 idle 推进时间（承接 u7-5「接收搭发送便车」）。

**(d) PCAP 生成器。** `gen_udp_pcap.py` 构造完整的以太网+IP+UDP 帧（含 IP 校验和、UDP 伪首部校验和），写出纳秒级时间戳的 PCAP：

> [4.sim/tools/gen_udp_pcap.py:45-60](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/tools/gen_udp_pcap.py#L45-L60) — `build_udp_frame`：拼 `dst_mac + src_mac + 0x0800` 以太网头、带校验和的 IPv4 头、带伪首部校验和的 UDP 头与载荷。
>
> [4.sim/tools/gen_udp_pcap.py:76-100](https://github.com/chili-chips-ba-wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/4.sim/tools/gen_udp_pcap.py#L76-L100) — 默认参数：src `D8:9E:F3:88:7E:C3@192.168.25.8` → dst `90:32:4B:07:0B:D1@192.168.152.1`，5 帧、64 字节载荷、1ms 间隔，输出 `./tools/test_udp_rand.pcap`。

#### 4.3.4 代码实践

**实践目标**：建立「实网验证」与「仿真 PCAP 验证」的对应关系，理解二者各覆盖哪一层。

**操作步骤**：

1. 列出两种验证的激励、观测、覆盖层对照表（见下）。
2. 若有仿真环境：运行 `python3 4.sim/tools/gen_udp_pcap.py` 生成 PCAP，再跑 `VUserMainPcap` 仿真，比较 `output/tx_node1.pcap`（发出）与 `output/node2_out.pcap`（收到）。
3. 若有板子：按 4.1 配好两节点，host A `ping 192.168.0.2`，在网线上 Wireshark 抓 UDP/51820 流量。

**对照表（示例答案）**：

| 维度 | 仿真 PCAP 验证 | 实网两节点验证 |
|------|---------------|---------------|
| 激励来源 | `gen_udp_pcap.py` 合成 UDP 帧，node1 回放 | host A 真实 `ping` ICMP |
| 观测手段 | node2 录制 `node2_out.pcap` | 网线上 Wireshark 抓包 |
| 覆盖层 | DPE 数据面路径（mux/MAC/demux）+ 延迟 | 端到端：主机→隧道→主机，含加密 |
| 当前 PoC 状态 | 明文直通（dummy_switch） | 明文直通（dummy_switch），加密待上线 |
| 主要价值 | 数据面回归测试、延迟测量 | 最终验收、真加密隧道确认 |

**需要观察的现象**：仿真里 `tx_node1.pcap` 与 `node2_out.pcap` 的帧内容一致（除时间戳外），延迟为「开始到开始」的固定值；实网里 ping 有回包、Wireshark 看到 UDP/51820 流量。

**预期结果**：两条路径验证的是同一条 DPE 硬件通路。仿真快、可重复、用于日常回归；实网慢、需硬件、用于发布前验收。当前阶段两者都走明文直通，等加解密流水线上线后，实网抓包才会真正呈现密文。

> 若无仿真/板子环境，本实践退化为「源码阅读型」：只完成第 1 步对照表即可。

#### 4.3.5 小练习与答案

**练习 1**：为什么说仿真 PCAP 验证是实网验证的「数字孪生」，但又不能完全替代实网？

**参考答案**：两者激励同一套 DPE 硬件路径（仿真里 DPE 是真实 RTL，只是 PHY/网线用 BFM 代替），所以仿真能回归数据面逻辑与延迟，是数字孪生。但仿真无法覆盖真实 PHY 自协商、真实网线抖动、真板卡时序收敛（PnR 后）以及真 RISC-V 固件在真硬件上的运行，这些只有实网才能暴露，故不能完全替代。

**练习 2**：Wireshark 在两板之间网线上抓到的包，源/目的 UDP 端口应是多少？为什么？

**参考答案**：应是 `51820`（WireGuard 标准端口）。因为 cryptokey 配置里 `Local port` 与 `Remote port` 都填的 `51820`，封装器套 UDP 头时即用此端口（见左节点 L93/L97、右节点 L149/L153）。这也正是 Wireshark 能识别为 WireGuard 流量的依据。

## 5. 综合实践

把本讲三个模块串成一个完整任务：**为两节点实验室编写一份「配置与验证手册」**。

要求：

1. **画拓扑图**：标出 host A、左板（`192.168.1.98` / `CCBACA01`）、右板（`192.168.1.99` / `CCBACA02`）、host B，以及两板间的外层网段与隧道承载的内层网段 `192.168.0.0/24`。
2. **列配置脚本**：分别为左、右节点写出完整的 `config network` / `config routes` / `config cryptokeys` 应答序列（可直接基于 `6.test/README.md` 的交互记录）。
3. **标注密钥镜像**：在你的脚本旁用箭头标出 `Left.encrypt_key ↔ Right.decrypt_key` 与 `Left.decrypt_key ↔ Right.encrypt_key` 的交叉关系，并写一句解释。
4. **写验证步骤**：给出 `ping` 命令与 Wireshark 抓包过滤条件（如 `udp.port == 51820`），说明预期看到的现象与当前 PoC 的明文直通现状。
5. **加仿真对照**：补一段说明，指出对应的仿真验证用 `gen_udp_pcap.py` + `VUserMainPcap`，并说明二者覆盖层的差异。

完成后，你应当能用这份手册向一个没读过源码的同事讲清「两块板怎么连、怎么配、为什么密钥要交叉、怎么验证通了、仿真又验证了什么」。

## 6. 本讲小结

- 两节点实验室是 wireguard-fpga 的最小可验证形态：两块 AX7201 当两个 WireGuard peer，中间网线连隧道，两端各挂主机，用 `ping` + Wireshark 验收。
- 每个节点经 UART CLI 做三类配置：`config network`（身份）、`config routes`（转发，落 `routing_table`）、`config cryptokeys`（peer 与密钥，落 `cryptokey_table`），后两者是 external regfile + tdp_ram，改表前经 FCR 原子握手。
- **核心结论**：左右节点的 `encrypt_key` 与 `decrypt_key` 必须交叉镜像（`Left.encrypt == Right.decrypt` 且 `Left.decrypt == Right.encrypt`），这是 ChaCha20-Poly1305 对称加密的硬性要求；配错会导致单向不通或全不通。
- 收发计数器 `send_cnt`/`recv_cnt` 是 CPU 与硬件双向可写的状态字段，配置时复位以让 nonce 从 0 开始。
- 实网验证（ping + Wireshark）与仿真验证（`gen_udp_pcap.py` + `VUserMainPcap`）是同一 DPE 路径的数字孪生：仿真做日常回归与延迟测量，实网做最终验收。
- 现状：当前 Phase1 PoC bitstream 综合 `dpe_dummy_switch`（明文直通），本讲描述的密钥镜像配置正确且必要，待 Unit 4/5 加解密流水线上线后即驱动真加密隧道，无需改配置。

## 7. 下一步学习建议

至此 Unit 8 与整本手册的 39 篇讲义已走完。建议的后续方向：

- **回到源码做加解密上线实验**：对照 u4-l5（WG 封装/解封装）与 u5-l3/u5-l4（加解密 datapath），尝试在 `top.filelist` 里把 `dpe_dummy_switch` 换回真正的 WG 处理链与 PipelineC/Pypeline 加密核（见 u5-l2、u5-l6），重新综合，再按本讲流程做一次真加密的两节点验证。
- **扩展到多节点/多 peer**：本讲只配了 1 个 peer（条目 1）和 1 条路由（条目 0）。可尝试配多个 peer、多条路由，验证 `routing_table` 的最长前缀查找（u4-l4）与 `cryptokey_table` 按 `tid` 取密钥的行为。
- **用仿真做回归基线**：在改任何数据面代码前，先跑一遍 `VUserMainPcap` 录下 `node2_out.pcap` 作为黄金向量；改完再跑一次比对，确保不回归（方法见 u7-l5 的 PCAP 驱动型测试台）。
- **通读「55 步包处理」**：`1.hw/README.md` 的 55 步是理解整机协作的最佳材料，建议结合 u2-l2（top.sv）逐实例对照，把每一步落到具体硬件模块。
- **关注上游**：项目处于 Phase1 PoC 持续演进中，定期 `git log` 跟踪 `top.filelist`、`csr.rdl` 与 PipelineC/Pypeline 产物的变化，及时回到相关讲义的 `update`/`rebuild`。
