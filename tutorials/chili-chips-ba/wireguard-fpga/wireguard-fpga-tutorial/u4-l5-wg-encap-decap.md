# WireGuard 封装/解封装与加解密数据流

> 阅读前置：本讲承接 u4-l1（DPE 总体结构与 AXIS 元数据）与 u2-l1（控制面/数据面分区）。你应已了解 DPE 的「mux → 处理流水线 → demux」三段式骨架、`dpe_if` 的 128 位 AXIS 信号（`tdata/tkeep/tlast/tvalid/tready`）、侧带元数据 `tuser`（含 `src/dst/bypass_*`）与 `tid`（peer index），以及三重字节序（总线小端、网络头大端、WG 头小端）。

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清一个 WireGuard 数据包在 DPE 处理流水线里「**解封装 → 解密+认证 → 路由 → 加密 → 封装**」的完整走向，并把 README 的 55 步分析对应到具体 RTL 块。
2. 读懂三个真实模块的设计意图：`dpe_wg_disassembler`（逐拍分类包头并剥离外层头）、`dpe_wg_decryptor`（解密 + 认证门 + 重封装）、`dpe_wg_encryptor`（加密 + 重组新头）。
3. 理解 AEAD（ChaCha20-Poly1305）nonce、send counter、验证门（verify-before-forward）等关键安全机制在硬件里的落地方式。
4. **如实认清 Phase1 PoC 现状**：上述块在源码里已写好，但在 `top.filelist` 中被注释、由直通的 `dpe_dummy_switch` 顶替，因此当前 bitstream 实际跑通的是「固定交叉直通 + 软件桥接」，而非真加密隧道。

## 2. 前置知识

### 2.1 WireGuard 数据报文的结构

WireGuard 把「内层明文用户包」裹进一个 **UDP 报文**里传输，对外看就是一个普通的 UDP/IPv4/Ethernet 帧。一个 transport（数据）消息从外到内有四层：

| 层 | 字段 | 字节序 | 说明 |
|---|---|---|---|
| Ethernet | DA/SA/EtherType | 大端（网络序） | 物理寻址，EtherType=0x0800 表示 IPv4 |
| IP | 协议=0x11(UDP)、src/dst IP | 大端 | 网络层，外层用的是 peer 间的公网 IP |
| UDP | src/dst port、length、checksum | 大端 | 运载 WG 消息 |
| WireGuard | type(=4 数据)、peer_idx(rcv)、counter(cnt)、payload、auth tag | **小端** | WG 自有头，payload 已加密 |

关键点：**外层三头是大端（网络序），而 WG 头是小端**。这一点会在解封装器的常量比较里直接体现出来（见 4.1.3）。

### 2.2 AEAD：加密后认证

ChaCha20-Poly1305 是一种 AEAD（Authenticated Encryption with Associated Data）构造：

- **ChaCha20** 负责对称加密，把明文变成等长密文；
- **Poly1305** 负责认证，对「AAD ‖ 密文 ‖ 长度」计算一个 16 字节的认证标签（auth tag）附在密文末尾。

接收方必须**先收到完整密文与 tag、重算并比对 tag 通过后，才允许明文流出**——否则攻击者伪造的密文会污染下游。本讲的解密器用一道「验证门 + FIFO」严格实现这条安全纪律（见 4.2.3）。

WireGuard 的 96 位 ChaCha20 nonce 由一个单调递增的 **64 位 send counter** 高位补零得到：

\[
\text{nonce}_{96} = \{\,0_{32},\ \text{send\_cnt}_{64}\,\}
\]

收发双方各自维护计数器，保证同一密钥下每个 nonce 只用一次。本讲的加密器/解密器都把 `send_cnt` 当 nonce 的低位送进加密核，并在每包结束后 `+1` 回写（见 4.3.3）。

### 2.3 概念块 vs 真实 RTL：六合一为三

README 用 6 个概念块描述流水线：Header Parser（头解析）、Disassembler（解封装）、Decryptor（解密）、IP Lookup（路由查找）、Encryptor（加密）、Assembler（封装）。但在 `1.hw/ip.dpe/` 里**只有 3 个 WG 源文件**——概念块被合并了：

| README 概念块 | 实际对应的 RTL | 合并原因 |
|---|---|---|
| Header Parser + Disassembler | `dpe_wg_disassembler` | 逐拍分类包头时顺带完成头解析 |
| Decryptor | `dpe_wg_decryptor` | 自带「Verification Gate & Ethernet Re-wrap」 |
| Encryptor + Assembler | `dpe_wg_encryptor` | 在加密的同时**生成**全新的 Ethernet/IP/UDP/WG 外层头 |

记住这张对照表，后面 4.1～4.3 就是在逐一拆解这 3 个文件，4.4 解释它们为何当前没上线。

## 3. 本讲源码地图

| 文件 | 角色 | 是否当前综合 |
|---|---|---|
| [1.hw/ip.dpe/dpe_wg_disassembler.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_wg_disassembler.sv) | 解封装：逐拍分类、剥离外层头、把 WG 元数据塞进 `tuser` | ❌（filelist 中被注释） |
| [1.hw/ip.dpe/dpe_wg_decryptor.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_wg_decryptor.sv) | 解密 + 认证门 + 以太网重封装 | ❌（同上） |
| [1.hw/ip.dpe/dpe_wg_encryptor.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_wg_encryptor.sv) | 加密 + 重组新外层头 | ❌（同上） |
| [1.hw/top.filelist](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.filelist) | 设计文件清单：决定哪些块进综合 | ✅ |
| [1.hw/ip.dpe/dpe.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv) | DPE 顶层：实际只例化 dummy_switch + 两个 tdp_ram | ✅ |
| [1.hw/ip.dpe/dpe_dummy_switch.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_dummy_switch.sv) | Phase1 占位直通开关 | ✅ |
| [1.hw/README.md](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/README.md) | 含 55 步包处理全流程分析 | —（文档） |

> 注：WG 块本身依赖 [1.hw/ip.dpe/dpe_egress_ip_lookup.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_egress_ip_lookup.sv)（路由查找，u4-l4 已讲）与两个 `tdp_ram`（路由表/密钥表，u4-l6）。本讲聚焦加解密/封装链本身。

## 4. 核心概念与源码讲解

### 4.1 WG 解封装：`dpe_wg_disassembler`

#### 4.1.1 概念说明

解封装器位于**接收方向**（数据流入本 peer）。它的职责是：

- **逐 beat 诊断**进来的包到底是不是 IPv4-over-UDP-over-WireGuard 的 transport 数据包；
- 如果**不是**（比如 ARP、ICMP、或 WG 握手报文），就走 `BYPASS` 通路原样放行，不做任何处理；
- 如果**是** WG 数据包，则**剥离外层 Ethernet/IP/UDP/WG 头**，只把加密 payload 往后送，同时把从 WG 头里提取的关键元数据（接收计数器 `rcv`、发送方计数 `cnt`、尾部 6 字节 `enp` 等）打包进侧带 `tuser`，供下游解密器使用。

模块头部的 ASCII 框图准确表达了这一思路：一条输入流进 FSMD（带数据通路的有限状态机），FSMD 的判别结果在输出端用一个 MUX 在「正常流水线输出」和「改写后的 payload + 元数据」之间二选一。

[1.hw/ip.dpe/dpe_wg_disassembler.sv:39-69](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_wg_disassembler.sv#L39-L69) 是模块说明与端口，可以看到它对外还输出 `wg_rcv`（接收计数，32 位）并接受 `wg_idx`/`wg_idx_match`（用于核对本 peer 是否是预期接收者），以及给 FCR 流控用的 `fcr_idle`。

#### 4.1.2 核心流程

FSM 共 11 个状态，可分三类：

```text
IDLE ──(beat0 是 IPv4?)──> HEADER_1 ──(beat1 协议是 UDP?)──> HEADER_2
                                                            │
              ┌────────── 不是就进 BYPASS_1→2→3 直到 tlast ──┘(任何一步不符)
              ▼
        HEADER_2 ──(beat2 WG type==4 数据?)──> PAYLOAD_1 → 2 → 3 → 4 →(5)
                                                     │
                                          输出端 MUX 改写 tdata/tuser
```

1. **IDLE**：看 beat0（前 16 字节）。若 EtherType 是 IPv4 且 IP 版本是 4，进 `HEADER_1`；否则进 `BYPASS_1`。
2. **HEADER_1**：看 beat1。若 IP 协议字段是 UDP，进 `HEADER_2`；否则 bypass。
3. **HEADER_2**：看 beat2。若 4 字节 WG type 字段等于 transport 数据（4），进 `PAYLOAD_1` 并锁存接收计数 `wg_rcv[15:0]`；否则 bypass。
4. **PAYLOAD_1/2**：锁存 `wg_rcv[31:16]` 与 64 位 `wg_cnt`（发送方计数，作为 nonce 依据）。
5. **PAYLOAD_3/4/5**：在尾部捕获 `wg_enp`（末尾若干字节），并经输出 MUX 把加密 payload 与元数据送出。`PAYLOAD_5` 用于处理「最后一拍需要拼接」的边界情况。

输出 MUX 只在 `PAYLOAD_5` 态改写 `tdata`/`tuser`，其余态（含所有 BYPASS）直接透传流水线输出。

#### 4.1.3 源码精读

**(a) 三步诊断与字节序陷阱。** 这是最值得细看的地方：

[1.hw/ip.dpe/dpe_wg_disassembler.sv:74-77](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_wg_disassembler.sv#L74-L77) 定义了诊断常量。注意它们看起来都「字节被换过位置」：

```systemverilog
localparam PROT_IP   = 16'h0008;   // 不是 0x0800！
localparam PROT_IPv4 = 4'h4;
localparam PROT_UDP  = 8'h11;
localparam PROT_WG   = 32'h00000004;  // WG 数据消息 type=4
```

为什么 EtherType 是 `16'h0008` 而不是 `16'h0800`？因为 128 位 AXIS 总线是**小端**，而 Ethernet/IP/UDP 头是**大端（网络序）**。0x0800 在线序上是「先 0x08 后 0x00」两个字节，落进小端总线后被读成 16 位值就成了 `0x0008`。同理 IP 协议号 0x11 仍是 `8'h11`（单字节无所谓序）。而 WG 头是**小端**，所以它的 type 字段 4 直接就是 `32'h00000004`——这正是 u4-l1 提到的「三重字节序并存」的活样本。

对应的比较逻辑在 [dpe_wg_disassembler.sv:126-149](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_wg_disassembler.sv#L126-L149)：beat0 比 `tdata[111:96]`、beat1 比 `tdata[63:56]`、beat2 比 `tdata[111:80]`。

**(b) 元数据打包进 tuser。** PAYLOAD_1 锁存接收计数与发送方计数：

[dpe_wg_disassembler.sv:169-175](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_wg_disassembler.sv#L169-L175) 把 `wg_rcv[31:16]` 与 64 位 `wg_cnt` 落入寄存器。

输出端在 `PAYLOAD_5` 把这些值拼进 `tuser`（这里输出端 `tuser` 被拓宽到 128 位 `OUTP_TUSER_WIDTH`）：

[dpe_wg_disassembler.sv:233-243](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_wg_disassembler.sv#L233-L243) 把 `wg_rcv_reg` 塞进 `tuser[127:96]`、`wg_cnt_reg` 塞进 `tuser[95:32]`，并置 `tuser[5]=1` 作为「我是 WG 数据包」的标记，同时改写 `tdata` 把头部字节挪走、把末尾 `wg_enp_reg` 补到正确位置。

**(c) 流水线寄存器与 skid buffer。** 输入经 depth=3 的 `axis_pipeline_register`（[dpe_wg_disassembler.sv:258-282](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_wg_disassembler.sv#L258-L282)）打拍，给 FSM 留出「先看头、后处理体」的时间窗口；输出经 depth=1 的 skid buffer（`axis_register`）吸收背压。

> ⚠️ **一个值得你本地复核的细节**：本模块的状态寄存器声明为 `state_reg`/`state_next`（[L94](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_wg_disassembler.sv#L94)），但输出 MUX 与 `fcr_idle` 却引用了一个未声明的 `state`（[L224](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_wg_disassembler.sv#L224)、[L255](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_wg_disassembler.sv#L255)）。这种命名不一致说明该块仍是**在制品（work-in-progress）**，与其在 filelist 中被注释的现状吻合。这也提醒读者：本讲引用的是「设计意图」，而非「已验证可综合」的行为。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：理解三步诊断的字节序。
2. **步骤**：打开 `dpe_wg_disassembler.sv`，定位 IDLE/HEADER_1/HEADER_2 三个状态；对每个比较，在纸上写出「该字段在线序上的原始值」与「落进小端总线后的值」。
3. **观察**：EtherType `0x0800 → 0x0008`；WG type `4 → 0x00000004`（不变，因为 WG 头本就是小端）。
4. **预期结果**：你能解释为何同一个「字节序」会让 IP 常量看起来被翻转、而 WG 常量不被翻转。
5. 待本地验证：若你把模块解开注释尝试综合，确认上面 `state`/`state_reg` 的命名问题是否会被工具报错。

#### 4.1.5 小练习与答案

**Q1**：为什么解封装器对 ARP 报文要原样放行而不是丢弃？
**A**：ARP 是建立以太网邻居关系的必要控制帧，不属于 WG 隧道载荷；放行它让设备能正常完成地址解析，否则连握手包都发不出去。

**Q2**：`wg_cnt`（64 位）从 WG 头里被锁存下来，它在下游会被用作什么？
**A**：它是发送方的 nonce 计数器，下游解密器会把它（连同 32 位 0）拼成 ChaCha20 的 96 位 nonce 来解密本包。

**Q3**：如果 beat2 的 WG type 不是 4（比如是握手消息 type=1），FSM 走哪条路？
**A**：进 `BYPASS_3`，整包原样透传——握手报文要交给控制面 CPU 处理，数据面不解密。

---

### 4.2 WG 解密与认证：`dpe_wg_decryptor`

#### 4.2.1 概念说明

解密器接收解封装器送来的「加密 payload + 元数据」，做三件事：

1. 调用 ChaCha20-Poly1305 解密核，把密文还原成明文，并由 Poly1305 重算 tag 与包尾 tag 比对，给出 `is_verified` 信号；
2. **验证门**：明文先写进一个 FIFO，但 FIFO 的读出（即对外的 `outp.tvalid`）被 `packet_authorized` 闸住——只有整包 tag 校验通过才开闸；校验失败则 `DROP_PACKET`，清空 FIFO，**绝不让未认证明文流出芯片**；
3. **以太网重封装**：与加密器对称，它在解密的同时**重新生成**一套 Ethernet/IP/UDP/WG 外层头（用本 peer 的 local/remote 地址），把解出的明文用户包重新打包，便于继续在 DPE 里转发。

#### 4.2.2 核心流程

状态机比加密器多一个 `DROP_PACKET`（见 [dpe_wg_decryptor.sv:109-120](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_wg_decryptor.sv#L109-L120)）：

```text
IDLE ──> WAIT_RAM(读密钥表) ──> HEADER_1..4(喂解密核 + 生成新头)
                                       │
                                       ▼
                                    PAYLOAD ──> PAYLOAD_LAST
                                       │              │
                                       │       is_verified? ── 是 ──> IDLE(开闸放行)
                                       │              │
                                       │              否 ──> DROP_PACKET(清 FIFO) ──> IDLE
```

- `WAIT_RAM`：按 `peer_idx`（来自 `tid`）从 `cryptokey_table` 读出本 peer 的 local/remote 地址、`decrypt_key`、`send_cnt` 等；
- `HEADER_1..4`：一边把密文喂进解密核（`to_decrypt`），一边用读到的地址字段生成新的 Ethernet/IP/UDP/WG 头写入输出 FIFO；
- `PAYLOAD`/`PAYLOAD_LAST`：持续把解密核吐出的明文拼进 FIFO，直到 `from_decrypt.tlast`；同时 `send_cnt+1` 回写；
- `PAYLOAD_LAST`：判 `is_verified`——通过则回 IDLE（开闸），否则进 `DROP_PACKET`。

#### 4.2.3 源码精读

**(a) 验证门 + FIFO——本模块的安全灵魂。** [dpe_wg_decryptor.sv:48-83](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_wg_decryptor.sv#L48-L83) 定义了一个简单的同步 FIFO（`sync_fifo`，参数 WIDTH=159、DEPTH=512）。FIFO 宽度 159 位 = 128(`tdata`) + 16(`tkeep`) + 1(`tlast`) + 3(`tuser_dst`) + 3(`tuser_src`) + 8(`tid`)，把一整拍的全部 AXIS 字段打包存起来（[L156-L167](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_wg_decryptor.sv#L156-L167)）。

闸门逻辑在 [dpe_wg_decryptor.sv:192-200](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_wg_decryptor.sv#L192-L200)：

```systemverilog
if (state == PAYLOAD_LAST && is_verified)  packet_authorized <= 1'b1;   // 整包验过才开闸
else if (outp.tlast && outp.tready)        packet_authorized <= 1'b0;   // 放完一包后关闸
```

对外输出则完全由 FIFO + 授权位驱动（[dpe_wg_decryptor.sv:461-467](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_wg_decryptor.sv#L461-L467)）：`outp.tvalid = !fifo_empty && packet_authorized`。这意味着即使明文已经躺在 FIFO 里，只要 tag 没验过，下游就看不到一个字节。

校验失败时，`DROP_PACKET` 把 `fifo_clear` 拉高一拍（[dpe_wg_decryptor.sv:343-347](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_wg_decryptor.sv#L343-L347)），`sync_fifo` 的复位端 `rst(inp.rst || fifo_clear)`（[L182](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_wg_decryptor.sv#L182)）随之清空整包——伪造/篡改的密文被静默丢弃。

**(b) 解密核例化。** [dpe_wg_decryptor.sv:471-488](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_wg_decryptor.sv#L471-L488) 例化 `chacha20poly1305_decrypt`，关键连接：

- `.key(decrypt_key)`——来自密钥表，是本 peer 的解密密钥；
- `.nonce({32'd0, send_cnt})`——nonce 由 send counter 高位补零构成（即上文的公式）；
- `.m_verified(is_verified)`——核回传的认证结果，正是闸门的依据。

> 这里的 `chacha20poly1305_decrypt`/`_encrypt` 核由 PipelineC/Pypeline 生成，是 Unit 5 的主角。本讲只把它当成一个「输入密文 AXIS、输出明文 AXIS + 1 位 verified」的黑盒。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：理解「先缓存、后放行」的时序。
2. **步骤**：沿 `sync_fifo` 的写口（`wr_en = muxed.tvalid && !fifo_full`）与读口（`rd_en = outp.tready && packet_authorized`）画一张时序：密文进 → 明文进 FIFO → tag 到 → `is_verified` → 开闸 → 明文出。
3. **观察**：明文进入 FIFO 的时刻 **早于** tag 校验完成的时刻；FIFO 的深度（512）必须够装下「最长包在等待 tag 期间累积的明文 beat 数」。
4. **预期结果**：你能解释为何 `packet_authorized` 必须是「整包粒度」而非「逐 beat」——因为 tag 只能对整包计算，中途放行任何一拍都违背 AEAD 语义。
5. 待本地验证：核算 512 深是否覆盖最大 MTU（约 1500 字节 ≈ 94 个 128 位 beat）下的存储需求。

#### 4.2.5 小练习与答案

**Q1**：为何解密器要在解密的同时重新生成一套外层 Ethernet/IP 头？
**A**：因为解出的明文用户包要继续在 DPE 里转发到正确的用户网口，它需要一套新的、属于本侧网络的二层/三层头；这也是模块名里「Ethernet Re-wrap」的含义。

**Q2**：`is_verified` 在 `PAYLOAD_LAST` 才被判定，那在此之前 FIFO 已经写了不少明文——这会泄露吗？
**A**：不会。FIFO 读口被 `packet_authorized` 闸住，而该位只在 `PAYLOAD_LAST && is_verified` 时才置 1；校验失败则 `fifo_clear` 清空，明文从不暴露给下游。

**Q3**：解密器例化核时 `aad=0, aad_len=0`，这意味着什么？
**A**：本实现不使用 AEAD 的 AAD（相关数据）字段，只对密文本身做加密与认证；这是 WireGuard transport 消息的简化处理。

---

### 4.3 WG 加密与封装：`dpe_wg_encryptor`

#### 4.3.1 概念说明

加密器位于**发送方向**（本 peer 把内层明文用户包发出去）。它一身二任：

- **加密**：用本 peer 的加密密钥与 send counter 调 ChaCha20-Poly1305 加密核，给明文加密并追加 16 字节 auth tag；
- **封装（Assembler）**：在密文前面**拼接全新的 Ethernet/IP/UDP/WG 外层头**（用 local/remote 地址填充），组装成完整的可上线帧。

所以 README 里的「Encryptor（步骤 41）+ Assembler（步骤 42）」在这一份文件里一气呵成。

#### 4.3.2 核心流程

状态机（[dpe_wg_encryptor.sv:64-74](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_wg_encryptor.sv#L64-L74)）：

```text
IDLE ──(bypass?)──> BYPASS(透传)
   │
   └─(正常)─> WAIT_RAM(按 tid 读密钥表)
                 │
                 ▼
        HEADER_1(发 Eth头 + 算长度) → HEADER_2(IP头) → HEADER_3(UDP/WG头)
                 │                                          │
                 └──────── 喂加密核(to_encrypt) ────────────┘
                                                 │
                                                 ▼
                          HEADER_4 → PAYLOAD → PAYLOAD_LAST(发密文+tag, send_cnt+1)
```

每个 HEADER 态同时干两件事：向加密核喂一拍明文（`to_encrypt`），并向输出写一拍**新头**（`muxed`）。这样头与密文在输出端按正确字节顺序首尾相接。`PAYLOAD_LAST` 发出末尾含 auth tag 的那一拍（`tkeep=16'h03FF`，即低 10 字节有效：16 字节明文成 16 字节密文，再加... 见源码），并把 `send_cnt+1` 标记为待回写。

#### 4.3.3 源码精读

**(a) 按 peer 查密钥表。** [dpe_wg_encryptor.sv:43-61](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_wg_encryptor.sv#L43-L61) 的端口里，`ram_peer_idx` 是输出（告诉密钥表 RAM「我要查哪个 peer」），其余 `ram_local_mac/ip/port`、`ram_remote_*`、`ram_encrypt_key`、`ram_send_cnt` 是输入（RAM 回送的数据）。IDLE 态把 `inp.tid` 锁为 `peer_idx`（[L185](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_wg_encryptor.sv#L185)），WAIT_RAM 态把 RAM 回送值全部锁存（[dpe_wg_encryptor.sv:197-216](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_wg_encryptor.sv#L197-L216)）。这正是 u4-l6 讲的 cryptokey_table 双口 RAM 的 B 口用法。

**(b) 长度字段计算。** 新头的 IP/UDP 长度字段要从内层包长度推算。HEADER_1 态（[dpe_wg_encryptor.sv:218-230](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_wg_encryptor.sv#L218-L230)）从内层 IP 头还原出原始长度，再加上外层封装开销：

```systemverilog
packet_ip_len_next  = {<内层IP总长度>} + 16'd76;  // 加 Eth(14)+IP(20)+UDP(8)+WG头... 
packet_udp_len_next = {<内层IP总长度>} + 16'd56;
```

（常数 76/56 反映了封装引入的固定字节开销，含 WG 头与 16 字节 tag 的分摊。）

**(c) 四拍新头逐拍生成。** 输出逻辑里 HEADER_1/2/3/4 各拼出一拍外层头，注释清楚标注了每段字段：

- [dpe_wg_encryptor.sv:334-350](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_wg_encryptor.sv#L334-L350)：Ethernet（DA=remote_mac、SA=local_mac、EtherType=0x0800）；
- [dpe_wg_encryptor.sv:352-368](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_wg_encryptor.sv#L352-L368)：IP（src=local_ip、dst=remote_ip、prot=0x11、TTL=64...）；
- [dpe_wg_encryptor.sv:370-386](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_wg_encryptor.sv#L370-L386)：UDP（sport=local_port、dport=remote_port）+ WG type(=4) + WG receiver id；
- [dpe_wg_encryptor.sv:388-404](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_wg_encryptor.sv#L388-L404)：WG 计数器（`send_cnt`）+ 加密核吐出的首拍密文。

**(d) nonce 与计数器回写。** 加密核例化（[dpe_wg_encryptor.sv:463-479](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_wg_encryptor.sv#L463-L479)）同样用 `.nonce({32'd0, send_cnt})`。包尾（`PAYLOAD` 态见 `from_encrypt.tlast`，[dpe_wg_encryptor.sv:280-285](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_wg_encryptor.sv#L280-L285)）执行：

```systemverilog
new_send_cnt_next   = send_cnt + 1;
update_send_cnt_next = 1'b1;   // 通知密钥表 RAM 回写新计数
```

`ram_new_send_cnt`/`ram_update_send_cnt` 就是回写接口，保证下一包用新 nonce，满足「nonce 不复用」。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：看清「加密」与「封装」如何并行。
2. **步骤**：对照 HEADER_1 态的输出块（[L334-L350](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_wg_encryptor.sv#L334-L350)），同时看它对加密核的 `to_encrypt.*` 赋值。
3. **观察**：同一拍里，`to_encrypt` 在喂明文进加密核，`muxed` 在输出 Ethernet 头——两条数据流并行，靠 FSM 节拍对齐。
4. **预期结果**：你能解释为什么加密器不需要像解密器那样的大 FIFO——因为它**先发头、后发密文**，密文产出即可立即送出，没有「等待验证」的约束。
5. 待本地验证：跟踪 `tkeep[15:14]==2'b00` 这个条件（[dpe_wg_encryptor.sv:371](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_wg_encryptor.sv#L371)附近）如何处理内层包不足整拍的尾部对齐。

#### 4.3.5 小练习与答案

**Q1**：加密器在每个包结束后把 `send_cnt+1` 回写，如果回写失败（计数器没递增）会怎样？
**A**：下一包会复用同一个 nonce。在 ChaCha20-Poly1305 下，同一密钥+同一 nonce 加密两个不同明文会泄露明文异或，是致命的安全破坏——所以回写路径必须可靠。

**Q2**：为什么加密器的 `IDLE`/`BYPASS` 态把输入原样透传？
**A**：非用户数据（如握手报文由 CPU 生成、或需 bypass 的管理流量）不该被加密封装；bypass 让它们绕过加解密核直达 demux。

**Q3**：加密器与解密器在「FIFO」上的差异根源是什么？
**A**：解密有「先出明文、后知 tag」的时序错配，必须缓存待验；加密没有这个约束（密文产出即合法），所以无需验证 FIFO。

---

### 4.4 PoC 现状：filelist 注释与 dummy_switch 直通

#### 4.4.1 概念说明

前面三节讲的都是「设计意图」。但**当前 HEAD（Phase1 PoC）实际综合进 bitstream 的并不是这套 WG 链**。决定「哪些块进综合」的唯一开关是 `top.filelist`：被注释的文件不参与编译，其模块即便源码完美也不会出现在芯片里。

#### 4.4.2 核心流程

打开 [1.hw/top.filelist:69-74](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.filelist#L69-L74)，DPE 段只列了 4 行：

```text
${HW_SRC}/ip.dpe/dpe.sv
${HW_SRC}/ip.dpe/dpe_multiplexer.sv
${HW_SRC}/ip.dpe/dpe_demultiplexer.sv
${HW_SRC}/ip.dpe/dpe_dummy_switch.sv          # ← 上线
#${HW_SRC}/ip.dpe/dpe_wg_disassembler.sv      # ← 注释掉
```

也就是说：mux 与 demux 真实在线，但中间的处理级是占位的 `dpe_dummy_switch`，解封装器（连同依赖它的解密器/加密器）被注释。注意 filelist 里**根本没列** `dpe_wg_encryptor.sv`/`dpe_wg_decryptor.sv`/`dpe_egress_ip_lookup.sv`——它们连「被注释」的资格都还没有，纯属待集成。

#### 4.4.3 源码精读

**(a) dpe.sv 实际例化了什么。** [1.hw/ip.dpe/dpe.sv:66-92](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv#L66-L92) 显示，处理级只有 `dpe_dummy_switch` 一个实例（[L78-L82](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv#L78-L82)），而本该是路由查找的 `dpe_egress_ip_lookup` 是**注释块**（[L95-L103](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv#L95-L103)）。

**(b) dummy_switch 做的事。** [dpe_dummy_switch.sv:84-116](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe_dummy_switch.sv#L84-L116) 是一张**固定交叉表**，按 `tuser_src` 硬查目的（CPU↔ETH1、ETH2→CPU、ETH3↔ETH4），既不解析任何包头、也不做任何加解密：

```systemverilog
DPE_ADDR_CPU:   outp_sbuff.tuser_dst = DPE_ADDR_ETH_1;
DPE_ADDR_ETH_1: outp_sbuff.tuser_dst = DPE_ADDR_CPU;
DPE_ADDR_ETH_3: outp_sbuff.tuser_dst = DPE_ADDR_ETH_4;
...
```

**(c) 密钥表的 B 口悬空。** [dpe.sv:123-135](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv#L123-L135) 的 `cryptokey_table` 是双口 RAM，A 口接 CSR（CPU 读写），但 **B 口（数据面查表用）`din_b('0)`、`dout_b()` 全悬空**——因为查它的加密器/解密器根本没上线。这正是 u4-l6 提到「B 端预留给数据面查找」的当前空置状态。

**综上**，当前 bitstream 的数据面真实行为是：`mux → dummy_switch（固定转发，明文）→ demux`，配 CPU 在软件里做握手与桥接（承接 u1-l5、u2-l1 的结论）。README 55 步里的步骤 4-5、12-13、21-22、29-30、39-42、49-52 在硬件里**一条都没真正跑通**。

#### 4.4.4 代码实践（源码阅读型）

1. **目标**：用证据链坐实「哪些上线、哪些没上线」。
2. **步骤**：
   - 在 `top.filelist` 里数 DPE 段共有几行未注释、几行注释；
   - 在 `dpe.sv` 里数实际例化的处理级模块，确认 `dpe_egress_ip_lookup` 是注释；
   - 在 `dpe.sv` 里确认 `cryptokey_table` 的 B 口未接任何数据面模块。
3. **观察**：三处证据互相印证——filelist 注释 → dpe.sv 不例化 → RAM B 口悬空。
4. **预期结果**：你能向别人证明「当前芯片不做任何 WG 加解密，只是个带固定路由的明文交换 + 软件握手」。
5. 待本地验证：若解开 filelist 注释并补上 encryptor/decryptor/egress_ip_lookup，需同步修复 4.1.3 末尾的 `state`/`state_reg` 命名问题，否则综合会报错。

#### 4.4.5 小练习与答案

**Q1**：为什么 PoC 阶段宁可放一个 dummy_switch 也不直接把 WG 链注释成「断开」？
**A**：dummy_switch 提供一条**可验证的明文通路**，让 mux/demux、CPU FIFO、CLI、两节点拓扑等周边设施能先行端到端跑通，为后续接入真加密链打好可测的基础（即 u1-l5 所述「明文直通 + 软件桥接」）。

**Q2**：要让 WG 链上线，最小改动集包含哪些？
**A**：①解开 filelist 对 disassembler 的注释，并补登 encryptor/decryptor/egress_ip_lookup；②修复 disassembler 的 `state` 命名；③在 `dpe.sv` 用真处理级替换 dummy_switch，并把 cryptokey_table 的 B 口连到 encryptor/decryptor；④确保 CSR 侧的密钥/路由表写时序与 FCR 握手正确。

**Q3**：dummy_switch 用 `tuser_src` 决定 `tuser_dst`，而真路由查找（u4-l4）用什么决定？
**A**：真路由查找用**目的 IP 地址**经 TCAM 最长前缀匹配决定 `tuser_dst`，并把命中的 peer 写进 `tid`——与 dummy_switch 仅凭物理入口 `src` 硬查是本质不同的。

## 5. 综合实践：把 README 55 步对应到 RTL 块

把 `1.hw/README.md` 的 55 步包处理分析（[L115-L190](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/README.md#L115-L190)）做成一张「步骤 → 概念块 → 实际 RTL → 当前是否跑通」的对照表。聚焦最能体现加解密/封装链的两组：**A→B 加密隧穿（步骤 36-45）** 与 **B 侧解隧验（步骤 46-55）**。

参考答案（请先自己填再对照）：

| 步骤 | README 描述（摘要） | 概念块 | 实际 RTL | 当前跑通？ |
|---|---|---|---|---|
| 38 | 多路复用器注入流水线 | Mux | `dpe_multiplexer` | ✅ |
| 39 | 头解析(ICMP)，解封装器/解密器**直通** | Header Parser/Disassembler/Decryptor | `dpe_wg_disassembler`（bypass 分支）/无独立解密器对明文 | ❌ 由 dummy_switch 顶替 |
| 40 | 路由查找定 peer 与出口 | IP Lookup | `dpe_egress_ip_lookup` | ❌（注释） |
| 41 | 加密 + 加 auth tag | Encryptor | `dpe_wg_encryptor` | ❌（未集成） |
| 42 | 加 WG/UDP/IP/Eth 头 | Assembler | `dpe_wg_encryptor`（HEADER 态） | ❌（未集成） |
| 43-45 | demux → MAC → 上线 | Demux/MAC | `dpe_demultiplexer`/MAC | ✅（demux）/✅（MAC） |
| 49 | 头解析(WG type)，补元数据 | Header Parser | `dpe_wg_disassembler` | ❌ 由 dummy_switch 顶替 |
| 50 | 解封装器剥外层、取加密 payload | Disassembler | `dpe_wg_disassembler`（PAYLOAD 态） | ❌（未集成） |
| 51 | 解密 + 验 tag 后放行 | Decryptor | `dpe_wg_decryptor`（验证门+FIFO） | ❌（未集成） |
| 52 | 对解出明文查 cryptokey 路由表 | IP Lookup | `dpe_egress_ip_lookup` | ❌（注释） |
| 53-55 | demux → MAC → 用户主机 | Demux/MAC | `dpe_demultiplexer`/MAC | ✅ |

**结论**：55 步里真正在当前 bitstream 跑通的只有 mux/demux/MAC 与 CPU 桥接（步骤 38、43-45 的 demux/MAC 部分、53-55 的 demux/MAC 部分）；凡是涉及头解析、加解密、路由查表、封装的步骤（39-42、49-52）**全部由 dummy_switch 的固定明文转发顶替**，真正的加解密隧道要等 filelist 解注释、模块集成完成后才生效。

## 6. 本讲小结

- WireGuard 数据包的处理链是 **解封装 → 解密+认证 → 路由 → 加密 → 封装**，分别由 `dpe_wg_disassembler`、`dpe_wg_decryptor`、`dpe_egress_ip_lookup`（u4-l4）、`dpe_wg_encryptor` 承担；README 的 6 个概念块在 RTL 里合并为 3 个 WG 文件。
- **解封装器**逐 beat 用常量诊断包头（注意 EtherType `0x0800` 在小端总线上读成 `0x0008` 的字节序陷阱），剥离外层头并把 WG 计数器塞进 `tuser`；非 WG/握手报文走 bypass。
- **解密器**用「验证门 + 512 深 FIFO」实现 AEAD 的 verify-before-forward：明文先进 FIFO，整包 tag 校验通过才开闸放行，失败则 `fifo_clear` 静默丢弃，绝不泄露未认证明文。
- **加密器**一身二任：并行地喂明文进 ChaCha20-Poly1305 核、又逐拍生成新 Ethernet/IP/UDP/WG 头；包尾把 `send_cnt+1` 回写，保证 nonce 不复用（nonce = `{32'd0, send_cnt}`）。
- 加解密核的 key/nonce/peer 地址都来自 `cryptokey_table`（双口 RAM，A 口接 CSR、B 口预留给数据面）。
- **Phase1 PoC 现状**：三个 WG 块在源码里已写好但被 `top.filelist` 注释或未登记，`dpe.sv` 实际例化的是固定明文转发的 `dpe_dummy_switch`，密钥表 B 口悬空——当前 bitstream 是「明文直通 + 软件握手桥接」，真加密隧道尚未上线。

## 7. 下一步学习建议

- **向内深入加解密核**：本讲把 `chacha20poly1305_encrypt/decrypt` 当黑盒。下一站进入 Unit 5，先读 [u5-l1](u5-l1-aead-chacha-poly-theory.md) 搞懂 AEAD/ChaCha20-Poly1305 的 RFC8439 原理，再看 [u5-l3](u5-l3-encrypt-datapath.md)/[u5-l4](u5-l4-decrypt-verify-datapath.md) 里 PipelineC 生成的核内部数据流——你会发现 4.2.3 的「验证门」对应核里的 `poly1305_verify` + `wait_to_verify`。
- **补齐路由查找**：本讲的 WG 链依赖 `dpe_egress_ip_lookup`（步骤 40/52）。回头读 u4-l4 的 TCAM 最长前缀匹配，理解它如何把 `tuser_dst` 与 `tid` 喂给本讲的加密器。
- **表与 RAM**：cryptokey_table 的双口 RAM 细节见 u4-l6；CPU 如何经 CSR/FCR 往里写密钥见 u3-l4 与 u6-l4。
- **想动手**：试着解开 `top.filelist` 注释、修复 disassembler 的 `state` 命名、在 `dpe.sv` 里把 dummy_switch 换成 disassembler→egress_ip_lookup→(encrypt/decrypt) 链（仅做接线、用仿真 `4.sim/rtl/dpe` 下的逐模块测试台验证），体会从 PoC 到真隧道的集成工作量。
