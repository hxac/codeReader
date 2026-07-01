# Packet Badger：以太网/IP/UDP 响应核

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 **Packet Badger**（以下简称 Badger）在 Bedrock 里扮演的角色：一个纯硬件、线速千兆、**只响应不主动发起**流量的以太网/IP/UDP 协议处理核。
- 画出 Badger 的整条数据通路：`scanner`（收包解析）→ `construct`（回包头部合成）→ `xformer`（数据复用）→ client 插件，理解 `rtefi_center` 如何把这几级串成完整 Rx/Tx 流水线。
- 复述输入包的校验规则：ARP / ICMP / UDP 各自被检查了哪些字段、哪些字段被刻意忽略。
- 掌握 **最多 8 个 UDP 端口插件**的机制：`udp_port_cam` 如何做端口匹配，client 接口的 4 进 1 出信号约定是什么。
- 看懂 `mem_gateway`（u4-l2 的 localbus 网关）和 `spi_flash`（SPI Flash 读写）为何能直接作为 client「插」进 Badger，从而把 FPGA 内部寄存器/Flash 暴露给网络。

本讲承接 u2-l2（localbus）与 u2-l3（寄存器映射），并直接连接 u4-l2 的 `mem_gateway`——后者正是 Badger 的一个标准 client 插件。

## 2. 前置知识

阅读本讲前，最好已经具备以下概念（不熟也没关系，下面会用通俗语言再点一遍）：

- **以太网帧结构**：一帧由「前导码 + SFD（起始定界符）+ 目的 MAC + 源 MAC + EtherType + 载荷 + CRC32」组成。Badger 在 GMII 接口上按字节（octet）逐拍接收。
- **ARP / IP / ICMP / UDP**：四层网络协议。ARP 把 IP 解析成 MAC；IP 是网络层；ICMP 的 echo 即日常的 `ping`；UDP 是无连接的传输层，靠「目的端口号」区分服务。
- **CAM（Content-Addressable Memory，内容寻址存储器）**：普通 RAM 是「给地址、取内容」；CAM 是「给内容、查它存在哪个地址」。Badger 用它判断「收到的 UDP 目的端口是不是我关心的那 8 个之一」。
- **localbus 与 LASS**（u2-l2、u4-l2）：Bedrock 自研的无握手片上总线，及其网络化版本「轻量地址空间序列化」。`mem_gateway` 正是把 localbus 桥接到 UDP 的 client。
- **CRC32 / 反码校验和（ones' complement checksum）**：以太网用 CRC32 校验整帧完整性；IP/ICMP 头用 16 位反码求和校验。Badger 既要在收方向**校验**它们，又要在发方向**重算** IP 头校验和。

关键直觉：Badger 不是跑协议栈的软核 CPU，而是**一组流水化的硬件状态机**。它的全部优势（线速、小面积、低延迟）和全部约束（client 必须在固定拍数内应答）都源于这一点。

## 3. 本讲源码地图

本讲涉及的关键文件全部位于 `badger/` 目录，自测代码位于 `badger/tests/`：

| 文件 | 作用 |
| --- | --- |
| `badger/README.md` | 权威说明：定位、Functionality 校验规则、client 数量、资源占用、live 测试方法 |
| `badger/scanner.v` | **收包扫描器**：前导码/SFD 帧同步、按字节校验 ARP/IP/ICMP/UDP 模式、CRC32、UDP 端口 CAM，输出 `status_vec` 分类结果 |
| `badger/construct.v` | **回包头部合成**：读 DPRAM 里的收包字节，查 `construct_tx_table` 生成以太网/ARP/IP 头模板，并重算 IP 头校验和 |
| `badger/rtefi_center.v` | **顶层流水线**：把 scanner→pbuf_writer→DPRAM→construct→xformer→ethernet_crc_add 串起来，并容纳 MAC/IP/端口配置存储 |
| `badger/udp_port_cam.v` | **UDP 端口匹配**：16 拍巡检式 CAM，把目的端口映射成 0~7 的 `port_p` 索引 |
| `badger/xformer.v` | **数据复用器**：按 `udp_sel` 把选通扇出到对应 client，并把 client 的回包数据选回发送通路 |
| `badger/spi_flash.v` | **client 插件示例**：把 UDP 包桥接到 SPI Flash 的读写引擎 |
| `badger/mem_gateway.v` | **client 插件示例**：把 UDP 包桥接到 localbus（即 u4-l2 的主角） |

> 命名小提示：`rtefi` 是这套 Rx/Tx 流水线的内部代号（README 的框图文件就叫 `doc/rtefi.svg`）；`scanner` 内部实例以 `a_scan`、`b_write`、`c_construct`、`d_xform`、`e_crc` 命名，字母顺序即数据流顺序。

## 4. 核心概念与源码讲解

### 4.1 scanner：输入包的逐字节扫描与分类

#### 4.1.1 概念说明

`scanner` 是 Badger 的「门卫」。GMII PHY 每个时钟送来 1 字节（`eth_in`）和一个有效标志（`eth_in_s`），`scanner` 必须：

1. 在比特流里认出以太网帧的起点（前导码 `55 55 ...` 加 SFD `d5`）。
2. 给收到的每个字节编号（`pack_cnt`），因为 ARP/IP/UDP 各字段在第几字节是固定不变的——只要记住「这是第几字节」，就能用一张「模板 ROM」逐字节比对。
3. 并行运行 ARP、IP、ICMP、UDP 四个模式匹配器，外加 CRC32 校验和 UDP 端口 CAM。
4. 在帧结束的那一拍，把所有判定结果浓缩成一个 8 位的 `status_vec`：它的低 2 位 `category` 直接告诉下游「这是 UDP(3) / ICMP(2) / ARP(1) / 忽略(0)」。

为什么把校验做成「逐字节模板比对」而不是先缓存整包再解析？因为 Badger 要线速：帧数据一边进、一边判、一边写进包缓冲 DPRAM，等帧结束的同一拍结论就出来了，零等待。

#### 4.1.2 核心流程

```
eth_in / eth_in_s (GMII, 每拍 1 字节)
        │
        ▼
┌─ 帧状态机 (h_idle/h_preamble/h_data/h_drop) ─┐
│  认 55..55 d5 前导+SFD，统计帧间间隔 IFG≥10  │
└──────────────────────────────────────────────┘
        │ h_data 期间
        ▼
   pack_cnt++           ← 字节序号（也是模板地址）
        │
   ┌────┼────┬────┬────┬────────┐
   ▼    ▼    ▼    ▼    ▼        ▼
 arp_  ip_  icmp_ udp_ cksum_  crc8e_guts
 patt  patt patt patt chk      (CRC32)
   │    │    │    │    │        │
   └────┴────┴────┴────┴────────┘
        │  末拍 final_octet
        ▼
   status_vec = {port_p, pass_ip, pass_ethmac, crc_zero, category}
```

帧状态机只用了 4 个 one-hot 状态，关键逻辑是「在 `h_idle` 看到 `55` 就进 `h_preamble`，在 `h_preamble` 看到 `d5` 且帧间间隔足够就进 `h_data`」：

[badger/scanner.v:79-120](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/scanner.v#L79-L120) —— 以太网帧同步状态机，识别 `55 55 d5` 前导码与 SFD，并强制帧间间隔 IFG≥10 拍。

`pack_cnt` 是整个扫描器的「脊椎」。它不仅当字节计数器，还直接被当作查 MAC/IP 配置存储的地址——因为我们的 MAC/IP 存放在一个 16 字节的小 RAM 里，`scanner` 在收包过程中按 `pack_cnt` 顺序读出自己的 MAC/IP，与帧里对应位置的字节比对：

[badger/scanner.v:163-182](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/scanner.v#L163-L182) —— `pack_cnt` 字节计数器，以及「在 octet 0-5 比对目的 MAC、30-33 比对目的 IP、38-41 比对 ARP 里的目的 IP」的地址译码。

#### 4.1.3 源码精读

`status_vec` 的位定义写在文件顶部的注释里，是理解整个扫描器输出的钥匙：

[badger/scanner.v:1-22](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/scanner.v#L1-L22) —— `status_vec` 位定义：bit0-1 是 `category`（3=UDP/2=ICMP/1=ARP/0=其它），bit2 是 CRC32 通过，bit3 是目的 MAC 命中，bit4 是合法 IP，bit5-7 是 CAM 给出的 UDP 虚拟端口号。

四个协议匹配器实例化得很规整，端口几乎一致（`cnt`、`data`、`pass`），可由参数 `handle_arp`/`handle_icmp` 选择是否实例化：

[badger/scanner.v:184-206](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/scanner.v#L184-L206) —— ARP/IP/ICMP/UDP 四个 `*_patt` 模式匹配器与 IP 头校验和检查器 `cksum_chk` 的并行实例化。

以 ARP 为例看「模板比对」是怎么做的。ARP 字段从以太网帧第 12 字节开始，`arp_patt` 用 `case(cnt[3:0])` 在指定字节位置放上期望值，凡 `want` 区间内的字节不匹配就把 `pass_r` 拉低：

[badger/scanner.v:327-360](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/scanner.v#L327-L360) —— `arp_patt`：核对 EtherType=0x0806、硬件类型=以太网(1)、协议类型=IP(0x0800)、硬件地址长 6、协议地址长 4、opcode=1(请求)。

同样地，`ip_patt` 核对 EtherType=0x0800、版本/IHL=0x45（IPv4、无选项）、分片标志为 0、TTL 非零、IP 头校验和正确：

[badger/scanner.v:364-439](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/scanner.v#L364-L439) —— `ip_patt`：IPv4 头模板比对、TTL 归零检测、IP 头反码校验和。

`udp_patt` 有两处值得注意的「反环路」设计：拒绝源端口 < 1024 的包（`reject_low`），拒绝目的端口为 0 的包（`discard_port0`）。这正是 README 反复强调的、为抵御「Loop DoS（CVE-2024-2169）」而做的预防：

[badger/scanner.v:475-526](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/scanner.v#L475-L526) —— `udp_patt`：核对 IP 协议号=17(UDP)，并拒绝源端口<1024 与目的端口=0 以防 UDP 回声环路。

最后，所有判定在末拍汇集成 `category` 与 `port_p`。注意它们是「与」关系：一个 UDP 包要 `pass`，必须同时满足「单播源 MAC ∧ CRC 正确 ∧ 目的 MAC 命中 ∧ IP 头合法 ∧ 目的 IP 命中 ∧ IP 长度自洽 ∧ UDP 模式匹配 ∧ UDP 长度自洽 ∧ 端口 CAM 命中」：

[badger/scanner.v:243-253](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/scanner.v#L243-L253) —— 汇总逻辑：`category` 与 `port_p` 的生成，以及 `status_vec`、`status_valid`、`pack_len` 的输出。

#### 4.1.4 代码实践

实践目标：亲眼看到 `scanner` 把一帧字节流分类成 `category`。

操作步骤：

1. 进入测试目录：`cd badger/tests`
2. 编译并运行扫描器自检：`make scanner_check`
3. 若想看波形：`make scanner.vcd && make scanner_view`（后者需要 gtkwave）

需要观察的现象：`scanner_check` 会用一帧预先生成的以太网包（`arp3.dat`/`icmp3.dat`/`udp3.dat` 之类）驱动 `scanner`，仿真结束后与 `.gold` 黄金文件比对。

预期结果：终端打印 `PASS`。若打开波形，关注 `status_valid` 拉高的那一拍 `status_vec[1:0]` 的值——对不同输入包应分别看到 1（ARP）、2（ICMP）、3（UDP）。

> 待本地验证：具体 `.dat` 文件名与 `scanner_tb.v` 实际驱动的包类型以本地 `badger/tests/scanner_tb.v` 为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `arp_patt` 只检查「目的 IP 匹配」，却不检查 ARP 包里嵌入的源 MAC 和源 IP？

**参考答案**：因为 ARP 响应只需要把请求者的 IP→MAC 映射「反过来」填进响应即可，源信息是否合法不影响我们生成正确响应；而且 README 明确写「No checks on embedded source IP or MAC」。放宽这些检查能让硬件更简单，只要目的 IP 是我，就值得回应。

**练习 2**：`category` 用 2 位编码，取值 0/1/2/3 分别对应什么？为什么要用「优先级选择」（`pass_udp ? 3 : pass_icmp ? 2 : ...`）而不是独立标志位？

**参考答案**：0=忽略、1=ARP、2=ICMP、3=UDP。用优先级选择是因为一个包不可能同时属于多类（UDP 包必然也是合法 IP 包，但语义上它就是 UDP），下游 `construct` 只需要一个确定性的「这一帧属于哪一类」结论来选回包模板。

### 4.2 construct 与 rtefi_center：回包合成与完整流水线

#### 4.2.1 概念说明

`scanner` 只解决「这帧要不要回、回哪一类」的问题。真正组装回包字节流的是 `construct`，而把收发两侧所有模块串成一条完整 Rx→Tx 通路的是 `rtefi_center`。

`construct` 的核心难题是：回包的以太网/IP 头里，有些字节直接来自收包（交换源/目的 MAC、源/目的 IP、源/目的端口），有些字节是固定模板（如 EtherType、版本号），还有一个字节是**必须现算的 IP 头校验和**。`construct` 用一张由 `tx_gen.py` 生成的查找表 `construct_tx_table`，按 `(category, pc)`（包类别 + 包内字节序号）查表，决定「这一拍回包字节该从哪里取」——取值来源只有四种：收包缓冲、配置存储、模板、或算出来的校验和。

`rtefi_center` 则是「装配车间」。收方向把字节写进一块「1 个 MTU 大小的 DPRAM」（每字 9 位，第 9 位标记帧起始 SOF）；发方向 `construct` 读这块 DPRAM 重组回包。由于收发可能跑在不同时钟（`rx_clk`/`tx_clk`），写指针用**格雷码**跨域传递，`construct` 再把格雷码转回二进制得到读地址。

#### 4.2.2 核心流程

`rtefi_center` 的实例化顺序就是数据流顺序（实例名前缀 a/b/c/d/e）：

```
GMII Rx ──► a_scan(scanner)
              │ 字节流 + status_vec
              ▼
           b_write(pbuf_writer) ──► 1-MTU DPRAM (9 bit, bit8=SOF)
                                          ▲ 读 (tx_clk, 格雷码跨域地址)
           c_construct(construct) ───────┘
              │ eth_data_out + pc + category + udp_sel
              ▼
           d_xform(xformer) ──► client 回包数据选回
              │
              ▼
           e_crc(ethernet_crc_add) ──► GMII Tx
```

`construct` 内部对每一拍回包字节的处理：

```
读 (category, pc) → 查 construct_tx_table → 得到 (out, chk_in, fp_offset, template)
   out 选字节来源：0=收包缓冲  1=配置存储  2=模板  3=校验和占位
   chk_in 选「参与 IP 校验和累加」的字节来源
fp_offset 决定「这一拍读 DPRAM 的哪个相对地址」
末拍把累加好的 IP 头校验和填进 out==3 的占位
```

跨域读地址的关键公式（格雷码转二进制）：

\[ \text{binary}[i] = \text{gray}[i] \oplus \text{binary}[i+1] \]

这正是 `construct.v` 里 `new_state = gray_l ^ {1'b0, new_state[paw-1:1]}` 这一行做的事——最高位不变，其余每位是「格雷码当前位异或上一位二进制结果」。

#### 4.2.3 源码精读

`construct` 的端口与参数。注意 `p_offset`（包偏移）是收发两侧地址对齐的关键常量，要留出足够余量吸收收发频率偏差：

[badger/construct.v:12-40](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/construct.v#L12-L40) —— `construct` 模块声明：`paw`（包地址宽度，默认 11）、`p_offset`，以及给 `xformer` 的 `category`/`udp_sel`/`eth_data_out` 输出。

格雷码跨域同步与「写指针前进是否健康」的故障检测。`xdomain_fault` 会在写指针每拍增量不是 0/1/2 时报警——这是收发时钟失配或丢数的信号：

[badger/construct.v:42-62](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/construct.v#L42-L62) —— 收包写指针的格雷码同步、格雷转二进制，以及 `xdomain_fault` 跨域健康检查。

回包字节序号 `pc_r` 与状态捕获：`pc_r==3` 那拍从 DPRAM 读出收包的 `status_vec`（因为收包缓冲里前几个字节是 SOF 标记 + 状态），从此 `construct` 就知道这一帧该按哪类回包：

[badger/construct.v:81-119](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/construct.v#L81-L119) —— 回包字节计数器 `pc_r`、从 DPRAM 捕获 `status_vec`、以及发数据选通 `o_strobe`/`p_strobe` 的起停。

查表得到「这一拍字节从哪来」+ DPRAM 读地址合成。`addr = fp + 符号扩展(fp_offset)` 把帧指针与相对偏移相加：

[badger/construct.v:127-133](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/construct.v#L127-L133) —— `construct_tx_table` 查表（地址为 `{category, pc_r}`），输出多路选择控制 `out`/`chk_in` 与 DPRAM 读地址。

IP 头校验和重算。`ones_chksum` 是反码求和累加器，`chksum_zero` 在包开头清零、`chksum_gate` 在 IP 头范围内有效，最后把结果填回 `out==2'b11` 的占位字节：

[badger/construct.v:155-174](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/construct.v#L155-L174) —— IP 头校验和累加器、字节来源多路选择，以及末拍用校验和结果覆写占位。

再看顶层 `rtefi_center` 如何把这一切串起来。它的参数表暴露了所有「综合期可定、运行期可改」的配置，包括 IP、MAC、以及 8 个 UDP 端口号（`udp_port0..7`，默认从 801 起顺序编号，0 表示禁用该端口）：

[badger/rtefi_center.v:15-98](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/rtefi_center.v#L15-L98) —— `rtefi_center` 端口与参数：`ip`/`mac`/`udp_port0..7`，以及 GMII Rx/Tx、配置口、client 接口（`len_c`/`raw_l`/`raw_s`/`idata`/`mux_data_in`）。

`a_scan` 与 `b_write` 的实例化。注意那块 1-MTU DPRAM 是直接写在 `rtefi_center` 里的 `reg [8:0] pbuf[0:(1<<paw)-1]`，第 9 位专门用来标记 SOF：

[badger/rtefi_center.v:133-157](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/rtefi_center.v#L133-L157) —— `pbuf_writer` 实例与 9 位包缓冲 DPRAM（无写使能，每拍都写）。

`c_construct` 与 `d_xform` 的实例化。`xformer` 把 client 的回包数据通过 `mux_data_in`（7 个 client 各 8 位，共 56 位）选回发送通路：

[badger/rtefi_center.v:159-189](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/rtefi_center.v#L159-L189) —— `construct` 与 `xformer` 的实例化，`idata` 直通给 client，`mux_data_in` 汇聚 client 输出。

MAC/IP 与 UDP 端口号的配置存储。这两块 16×8 RAM 既能在 `config_clk` 域被 localbus 写（运行期改 IP/端口），又能在 `rx_clk`/`tx_clk` 域被读：

[badger/rtefi_center.v:258-298](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/rtefi_center.v#L258-L298) —— MAC/IP 配置 RAM 与 UDP 端口号 RAM，及其初值（参数 `ip`/`mac`/`udp_port*`）的装入。

#### 4.2.4 代码实践

实践目标：跑通整条 Rx/Tx 流水线，验证 Badger 能对一帧 UDP 包回出正确的响应。

操作步骤：

1. `cd badger/tests`
2. `make rtefi_pipe_check`（这是 `rtefi_center` 的端到端自检）
3. 想看完整回包波形：`make rtefi_pipe.vcd`

需要观察的现象：`rtefi_pipe_check` 把预生成的收包喂给整条流水线，把发出的字节流与 `.gold` 黄金文件 `cmp` 比对。

预期结果：打印 `PASS`。

> 待本地验证：若提示缺工具（如 iverilog 未装），按 u1-l2 的依赖清单补齐后重试。

#### 4.2.5 小练习与答案

**练习 1**：为什么包缓冲 DPRAM 用 9 位（多出 1 位），而不是恰好 8 位存数据？

**参考答案**：第 9 位（`pbuf_out[8]`）是 SOF（帧起始）标记位。`construct` 需要在发包时重新定位「收包从 DPRAM 哪里开始」，靠的就是这个标记位；它不属于以太网数据本身，所以单独用一位旁路标记。

**练习 2**：`rtefi_center` 同时声明了 `rx_clk` 和 `tx_clk` 两个时钟。如果实际工程里它们接同一个时钟，代码里有专门优化吗？

**参考答案**：有。`construct.v` 用 `ifdef COMMON_CLOCKS` 区分：共时钟时 `fp = state + p_offset` 直接算；不共时钟时则要维护一个 `fp_r` 寄存器并容忍最多 100 ppm 的频偏，逻辑更复杂。

### 4.3 udp_port_cam：UDP 目的端口匹配

#### 4.3.1 概念说明

当一个 UDP 包通过 `scanner` 的 IP/UDP 合法性检查后，还剩最后一个问题：**它的目的端口是不是 Badger 关心的 8 个端口之一？是的话，属于第几号 client？**

这件事用「内容寻址存储器（CAM）」来做。但全并行 CAM 很费逻辑（注释里说运行期可配的并行比较器要约 217 个 LUT，几乎是核心 `rtefi_pipe` 的一半）。`udp_port_cam` 用了一个聪明的折中：把 8 个 16 位端口号（拆成 16 个 8 位半字）存进一块 16×8 的分布式 RAM，然后在 16 拍里**巡检**一遍，逐个比较。这样 LUT 数降到约 32。

这个折中能成立，靠的是以太网最小帧长 64 字节的硬性保证——即便是「无载荷的最短 UDP 包（8 字节）」，后面也会被 PHY 补足 18 字节填充，CAM 有充裕的 16 拍时间在响应前完成匹配。

#### 4.3.2 核心流程

```
port_s 拉高时：锁存输入的 2 个字节（目的端口的低/高半字）
            启动 port_cnt 从 0 计到 15
每拍：读配置 RAM 的第 port_cnt 项 (pno_d)，与锁存的输入半字比较
     若相等且是「第二个半字」拍，记录命中 port_p = port_cnt/2，置 port_h
16 拍走完：置 port_v（结果有效）
下个 port_s：清零，重新开始
```

时序预算可写成不等式。设输入端口的两个字节在第 N、N+1 拍到达，CAM 需要 16 拍完成巡检：

\[ T_{\text{available}} \geq 16 \cdot T_{\text{clk}} \]

而对最短合法以太网帧，剩余填充字节提供的时间远超此值，故安全。

#### 4.3.3 源码精读

模块端口。`naw=3` 即 3 位地址宽度（对应 8 个端口），`port_p` 是命中端口的 0~7 索引，`port_h` 是「命中」标志，`port_v` 是「结果已就绪」时序标志：

[badger/udp_port_cam.v:32-47](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/udp_port_cam.v#L32-L47) —— `udp_port_cam` 端口：`port_s` 选通、`data` 输入、配置 RAM 口、命中索引与命中标志。

输入锁存与 16 拍巡检计数器。`port_in1`/`port_in2` 把连续两拍的目的端口半字存下来，`port_cnt` 在 `port_s` 后从 0 走到 15：

[badger/udp_port_cam.v:49-61](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/udp_port_cam.v#L49-L61) —— 输入半字锁存与巡检计数器，`pno_a` 给出每拍要读的配置 RAM 地址。

比较与命中锁存。`equal = port_in1 == pno_d` 每拍都比较，但只有「`equal` 与上一拍的 `eq_hold` 都真、且 `port_cnt[0]` 为 1（即第二个半字也对上）」时才记录一次命中，并把 `port_cnt[naw:1]` 作为端口索引：

[badger/udp_port_cam.v:63-84](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/udp_port_cam.v#L63-L84) —— 逐拍比较、高低半字都匹配时锁存 `port_p`/`port_h`，巡检结束置 `port_v`。

最后在 `scanner` 里，`port_h` 被并进 `pass_udp`，`port_p0` 被并进 `status_vec` 的高 3 位（即 `udp_sel`），从而告诉 `xformer`「这一帧的回包数据该由第几号 client 提供」：

[badger/scanner.v:214-222](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/scanner.v#L214-L222) —— `scanner` 实例化 `udp_port_cam`，在 `pack_cnt==36`（目的端口字节到达）时拉 `udp_port_stb` 启动匹配。

#### 4.3.4 代码实践

实践目标：单独验证端口匹配逻辑，观察 `port_h`/`port_p` 的时序。

操作步骤：

1. `cd badger/tests`
2. `make udp_port_cam_check`（这是 `all` 默认目标之一，纯逻辑、无网络依赖）

需要观察的现象：测试台向 CAM 喂入一个目的端口号，等待若干拍后检查 `port_p` 是否等于配置里该端口对应的索引、`port_h` 是否为 1。

预期结果：打印 `PASS`。若打开 `udp_port_cam.gtkw` 波形，能看到 `port_cnt` 从 0 走到 15、命中那拍 `port_h` 拉高的过程。

#### 4.3.5 小练习与答案

**练习 1**：为什么 CAM 用「16 拍巡检」而不是「8 路并行比较」？

**参考答案**：纯并行比较器在「端口号运行期可配」时会占用约 217 个 LUT（要存 8 个 16 位数并比较），几乎是核心流水线的一半面积；而 16 拍巡检把同一套比较器分时复用，面积约 32 LUT，且因以太网最小帧长 64 字节的填充保证，时序上完全来得及。

**练习 2**：如果配置里同一个端口号被写进了两个端口槽位（如 `udp_port1` 和 `udp_port2` 都设成 801），CAM 会返回哪个索引？

**参考答案**：返回**先匹配到**的那个（索引较小的槽位），因为代码条件里有 `~port_v_r`——一旦首次命中锁存了 `port_p_r`，后续再匹配也不会覆盖。所以配置时应避免端口重复。

### 4.4 client 插件接口：把 Badger 变成可扩展的服务平台

#### 4.4.1 概念说明

Badger 真正强大的地方不是自带 ARP/ICMP/UDP echo，而是它定义了一套**简洁的 client 插件接口**：任何一个用户模块，只要实现「4 进 1 出」的几根信号，就能挂到一个 UDP 端口上，成为网络可访问的服务。README 说最多支持 8 个 client，其中 **0 号端口被内部实现为 UDP echo**（直接把收到的 UDP 载荷原样回去），其余 1~7 号留给用户插件。

这套接口的信号约定（文档见 `doc/clients.eps`，源码里反复注释 `client interface with RTEFI, see doc/clients.eps`）是：

| 方向 | 信号 | 位宽 | 含义 |
| --- | --- | --- | --- |
| 主机→client | `idata` | 8 | 输入数据字节流（收包的 UDP 载荷） |
| 主机→client | `raw_s` | 1 | 短选通（有效载荷字节，不含头/CRC） |
| 主机→client | `raw_l` | 1 | 长选通（含前导与 CRC 的完整帧选通） |
| 主机→client | `len_c` | 11 | UDP 载荷剩余字节数（递减计数器） |
| client→主机 | `odata` | 8 | 回包数据字节流 |

**关键约束**：client 必须在**固定的、综合期已知的拍数**（参数 `n_lat`）内把 `idata` 处理成 `odata`。因为 `xformer` 正是用一个 `n_lat` 级的 `reg_delay` 把选通延迟同样拍数来对齐收发的——这正是 u4-l2 讲过的「固定延迟读」哲学在网络层的再现：没有握手，靠延迟对齐。

`mem_gateway`（u4-l2）和 `spi_flash` 是两个标准 client 实例：前者把 UDP 包桥接成 localbus 读/写周期（于是网络能直接读写 FPGA 寄存器），后者把 UDP 包桥接成对 SPI Flash 的编程命令。

#### 4.4.2 核心流程

```
xformer 拿到 udp_sel(0~7)
   │
   ├─ mask = 1 << udp_sel            // one-hot
   ├─ raw_l[7:1] = mask[7:1] & eth_strobe_long   // 选中的那一位才有长选通
   ├─ raw_s[7:1] = mask[7:1] & pdata_down        // 选中的那一位才有短选通
   └─ len_c、idata 广播给所有 client（client 自己靠 raw_s 决定是否消费）

各 client 在固定 n_lat 拍后回 odata
   │
   ▼
xformer: odata = mux_data_in2[8*udp_sel +: 8]    // 按 udp_sel 选一个 client 的输出
        （udp_sel==0 时选内部 echo 副本 odata1）
```

#### 4.4.3 源码精读

`xformer` 把选通扇出给 7 个 client（0 号是内部 echo，不需要外部 client）。`mask` 是 one-hot，只有被 `udp_sel` 选中的那一位的 `raw_l`/`raw_s` 才会被拉高：

[badger/xformer.v:87-92](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/xformer.v#L87-L92) —— `xformer` 按 `udp_sel` 生成 one-hot `mask`，把长/短选通扇出到对应 client 的位。

回包数据多路选择。`mux_data_in2` 把 7 个外部 client 的输出与 1 个内部 echo 副本拼成 8 路，按 `udp_sel` 选一路作为最终 `odata`。对非 0 号 UDP 端口，回包里的 UDP 校验和会被强制置零（因为内容变了、硬件懒得重算）：

[badger/xformer.v:106-113](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/xformer.v#L106-L113) —— 8 路输出多路选择：UDP 校验和位强制置零、其余按 `udp_sel` 选 client 或 echo 副本。

看一个真实 client——`spi_flash`——的接口声明，它的前 5 个端口严格符合上面的「4 进 1 出」约定：

[badger/spi_flash.v:1-19](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/spi_flash.v#L1-L19) —— `spi_flash` 模块声明：client 接口（`len_c`/`idata`/`raw_l`/`raw_s`/`odata`）+ SPI 物理引脚。

`spi_flash` 如何把 RTEFI 选通适配成自己的读写时序。它把收到的 UDP 载荷第一字节当作命令码（`0x52`=写 Flash 缓冲），后续字节存入 `rx_mem`，等 SPI 引擎处理后把回包字节写进 `tx_mem`，再按选通顺序回吐：

[badger/spi_flash.v:66-90](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/spi_flash.v#L66-L90) —— `spi_flash` 把 `raw_s` 移位对齐、按字节写 `rx_mem`、用首字节 `0x52` 解码写命令。

最后，回包数据用一个 `reg_delay` 延迟 `n_lat-1` 拍输出，使 `odata` 与 `xformer` 期望的固定延迟精确对齐：

[badger/spi_flash.v:131-132](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/spi_flash.v#L131-L132) —— `reg_delay` 把 client 输出延迟 `n_lat-1` 拍，对齐 `xformer` 的延迟预算。

作为对照，`mem_gateway`（u4-l2 的主角）的端口表头同样是这套 client 接口，外加 localbus 信号——这就是「网络 UDP 包 → localbus 寄存器读写」全链路的接合点：

[badger/mem_gateway.v:39-62](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/mem_gateway.v#L39-L62) —— `mem_gateway` 端口：上半是 client 接口，下半是 localbus（`addr`/`control_*`/`data_out`/`data_in`）。

#### 4.4.4 代码实践（本讲主实践）

这是本讲规格指定的实践任务，分两步。

**第一步：整理 ARP/ICMP/UDP 的校验字段表。**

操作步骤：

1. 打开 [badger/README.md](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/README.md) 的 `## Functionality` 一节（第 67 行起）。
2. 对照本讲 4.1.3 引用的 `arp_patt`/`ip_patt`/`icmp_patt`/`udp_patt` 源码，逐条核对 README 列出的检查项是否都能在代码里找到对应。

预期结果：你应该能填出类似下表（答案见本节末尾）：

| 协议 | 被检查的字段 | 刻意不检查的字段 |
| --- | --- | --- |
| ARP | EtherType=0x0806；硬件类型=以太网；协议类型=IP；地址长度；opcode=请求；**目的 IP=我** | 嵌入的源 IP、源 MAC；以太网头目的 MAC（通常广播） |
| ICMP | （依赖 IP）IP 协议号=1；ICMP type=8；code=0；**ICMP 校验和正确** | — |
| UDP | （依赖 IP）IP 协议号=17；**源端口≥1024**；**目的端口命中 client 且≠0**；UDP 长度自洽 | **UDP 校验和（不检查）** |

**第二步：说明新增一个自定义 UDP 服务要实现什么。**

操作步骤：

1. 决定一个端口号（建议 < 1024 以防环路，README 强烈建议）。
2. 写一个新模块（可仿照 `badger/hello.v` 这个最简 client），端口表头照搬 `spi_flash.v` 的前 5 个信号：`input [10:0] len_c; input [7:0] idata; input raw_l; input raw_s; output [7:0] odata;`。
3. 在模块里用 `raw_s` 选通把 `idata` 累加/转存，并在 `n_lat` 拍后从 `odata` 回吐（用 `reg_delay` 对齐，与 `spi_flash.v:131` 同款）。
4. 把它的 `odata` 接进顶层 `mux_data_in` 的对应位，把对应 `raw_l`/`raw_s`/`len_c`/`idata` 接上，并把该端口号写进 `rtefi_center` 的 `udp_port1..7` 之一。

需要观察的现象：当主机向该端口发 UDP 包时，`xformer` 的 `udp_sel` 会选中你的 client，你的 `odata` 字节流会被组装进回包。

预期结果：你的服务能回出 UDP 响应。端到端可用 `badger/tests/hello_check`（最简 echo client `hello.v` 的自检）验证接口接法是否正确：

```bash
cd badger/tests
make hello_check
```

> 待本地验证：`hello.v` 是官方提供的最小 client 样板，新增服务时优先模仿它的端口表与延迟对齐方式。

#### 4.4.5 小练习与答案

**练习 1**：为什么 0 号端口不需要外部 client？

**参考答案**：因为 UDP echo（把收到的载荷原样返回）是最常见的需求，且实现极简——`xformer` 内部直接把 `idata` 延迟若干拍当作 `odata1`（echo 副本），当 `udp_sel==0` 时多路选择器就选这一路，省掉一个外部模块。

**练习 2**：client 必须遵守的「固定延迟」约束（`n_lat`）如果被违反，会怎样？

**参考答案**：`xformer` 用一个 `n_lat` 级的 `reg_delay` 把发包选通延迟同样拍数，与 client 的 `odata` 对齐。若 client 的实际处理延迟与 `n_lat` 不符，回包字节就会错位——轻则数据错乱，重则回包长度与 UDP 长度字段不匹配。所以 client 必须用 `reg_delay` 之类手段把自己的输出精确对齐到 `n_lat` 拍（参考 `spi_flash.v:131`）。

**练习 3**：为什么 `xformer` 对非 0 号 UDP 端口的回包，要把 UDP 校验和强制置零？

**参考答案**：因为 client 改写了 UDP 载荷内容，原校验和失效；而 Badger 不愿为 UDP 重算校验和（UDP 校验和可选，置零表示「不校验」），所以 `scanner` 收方向也「不检查 UDP 校验和」，二者对称，合规且最省硬件。

## 5. 综合实践

把本讲四块知识串起来：从「一帧 UDP 包到达」到「某个 localbus 寄存器被读写」的完整端到端通路。

任务：画出下面这条链路的完整数据通路与控制信号流，并标注每一段由哪个模块负责、信号如何在 `rtefi_center` 的实例间流转：

```
主机发出 UDP 包 (目的端口 803，LASS 协议载荷)
   │ GMII Rx
   ▼
scanner: 字节级校验 + udp_port_cam 命中 803 → udp_sel
   │ 字节流写入 9-bit DPRAM (SOF 标记)
   ▼
construct: 读 DPRAM、合成以太网/IP/UDP 回包头
   │ category=3(UDP)、udp_sel 送给 xformer
   ▼
xformer: raw_s/raw_l/len_c/idata 扇出到 mem_gateway(client)
   │
   ▼
mem_gateway: 解 LASS → localbus 读/写周期 (addr/control_*/data_out/data_in)
   │ 固定 read_pipe_len 拍后回 odata
   ▼
xformer 选回 → ethernet_crc_add → GMII Tx → 主机收到回包
```

具体动手：

1. 阅读 [badger/README.md](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/README.md) 的 `## Example "live" test run` 一节（第 136 行起），它给出了一条真实的 `nc` 命令，向 `192.168.7.4:803` 发 LASS 读请求，经 `mem_gateway` 读 `fake_config_romx.v` 里的内容。
2. 在 `badger/tests` 下尝试 `make mem_gateway_check`，验证 client 接口与 localbus 桥接的正确性。
3. 对照 `rtefi_center.v` 的实例化（4.2.3），在自己的笔记里把 `a_scan`/`b_write`/`c_construct`/`d_xform` 与外部 `mem_gateway` 用线连起来，标出 `status_vec`、`udp_sel`、`mux_data_in`、`idata`、`raw_s` 这几组关键信号在哪两段实例之间流过。

预期结果：你能用一句话回答「为什么从 UDP 包到 localbus 寄存器读写，全程没有任何握手机制、却依然正确」——因为 `scanner` 给出确定的 `udp_sel`、`construct` 给出确定的回包骨架、`mem_gateway` 在确定的 `read_pipe_len` 拍后回数据，三处的「确定性/固定延迟」叠加，替代了握手。

> 待本地验证：`make mem_gateway_check` 与 live 测试需要 iverilog；live 测试还需 root 配置 TAP 网卡，初学者先跑 `mem_gateway_check` 即可。

## 6. 本讲小结

- **Badger 是纯硬件的以太网/IP/UDP 响应核**：线速千兆、只响应不主动发起流量，靠 `scanner` 逐字节扫描 + 模板比对完成收包校验与分类。
- **`status_vec` 是扫描器的总输出**：低 2 位 `category`（UDP/ICMP/ARP/忽略）+ 高 3 位 `udp_sel`（命中的 client 端口号），一拍给出全部分类结论。
- **`construct` + `rtefi_center` 组成回包流水线**：收包进 9 位 DPRAM，发包端查 `construct_tx_table` 决定每拍字节来源（收包/配置/模板/校验和），并现算 IP 头校验和；收发用格雷码跨域。
- **`udp_port_cam` 用 16 拍巡检代替并行 CAM**：以约 32 LUT 实现最多 8 个运行期可配端口的匹配，靠以太网最小帧长保证时序充裕。
- **client 插件接口是「4 进 1 出 + 固定延迟」**：`idata/raw_s/raw_l/len_c` 进、`odata` 出，`n_lat` 拍内应答；`mem_gateway`、`spi_flash`、`hello` 都是这个接口的标准实现。
- **ARP/ICMP/UDP 校验有取有舍**：严格检查目的 IP/MAC、协议号、长度自洽；刻意不查 ARP 源信息、UDP 校验和，以简化硬件并抵御回声环路（拒绝源端口<1024）。

## 7. 下一步学习建议

- **u5-l1（serial_io）**：Badger 挂在 GMII 上，但实际 PHY 多是 RGMII/SGMII/MGT。下一单元的 `gmii_to_rgmii` 等适配层正是 Badger 与物理链路之间的桥；8b/10b 编解码也是理解高速串行的前置。
- **u4-l3（jit_rad）**：本讲的 `mem_gateway` 是「固定延迟读」的 client 代表；u4-l3 的 `jit_rad` 则解决「跨域读回」难题，二者对照能加深对「为何 Badger 要求固定延迟」的理解。
- **继续阅读源码**：`badger/tests/rtefi_pipe_tb.v` 是整条流水线的端到端测试台；`badger/tests/client_sub.v` 是 client 接口主机侧的参考实现，读懂它你就能自己写驱动 Badger 的软件。形式化方面，可结合 u6-l1 的 `cdc_snitch` 看 Badger 内部 `reg_tech_cdc` 的 CDC 锚点是如何被检查的。
