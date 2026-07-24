# 完整 AXI4 总线与 AxiRam

## 1. 本讲目标

学完本讲，你应该能够：

- 看懂 SURF 在 `AxiPkg.vhd` 里用四条 VHDL 记录（`AxiReadMasterType` / `AxiReadSlaveType` / `AxiWriteMasterType` / `AxiWriteSlaveType`）折叠出的 AXI4 五个通道（AR/R/AW/W/B），并说清「VALID 与数据归生产方、READY 归消费方」如何在记录里落地。
- 理解 AXI4 突发（burst）的关键字段 `arlen/awlen`、`arsize/awsize`、`wstrb` 的编码，以及 `getAxiLen` 如何把「字节数」换算成「传输次数 − 1」。
- 读懂 `AxiRam` 这个 AXI4 从机的写/读状态机，理解它如何用 `SimpleDualPortRam` 承载突发、用 `wstrb` 做字节写、用 `bresp/rresp` 上报对齐错误。
- 理解 `AxiToAxiLite` 这座「降级桥」如何把面向突发的 AXI4 拍扁成单拍 32 位的 AXI-Lite，以及它的硬性前提（32 位对齐、单拍事务）。
- 能运行 `tests/axi/axi4/test_AxiRam.py` 这条 cocotb 回归，并据此设计自己的突发写/读回验证。

## 2. 前置知识

本讲建立在 **u3-l1（AXI-Lite 记录类型与包）** 之上，下面几个概念请先确认已经掌握，本讲不会重复展开：

- **记录化总线**：SURF 把一条总线折叠成若干 VHDL `record`，端口只传记录而非几十根扁平线。AXI-Lite 用 `AxiLiteReadMasterType` 等四个记录，AXI4 沿用同一思路，只是记录里字段更多。
- **握手口诀**：「VALID 与数据归生产方，READY 归消费方」。AXI4 与 AXI-Lite 共用这一句，只是 AXI4 多出突发与多拍数据。
- **AXI 响应码**：2 位的 `AXI_RESP_OK_C="00"`、`AXI_RESP_SLVERR_C="10"`、`AXI_RESP_DECERR_C="11"`，定义在 `AxiLitePkg.vhd`（AXI4 直接复用，不另定义）。
- **双进程骨架**：`RegType` + `REG_INIT_C` + `comb`（算次态 `v`）+ `seq`（打寄存器 `r`），`AxiRam` 完全沿用这一写法。

此外补充两个 AXI4 相对 AXI-Lite 的新术语，后文会反复出现：

- **突发（burst）**：一次地址请求（AR 或 AW）后面跟着**多拍**数据（R 或 W），由 `arlen/awlen` 指定拍数。AXI-Lite 永远是「一拍」，AXI4 可以「一拍对多拍」。
- **写选通（write strobe，`wstrb`）**：每拍写数据每个字节配一位使能，1 表示该字节要写、0 表示保持。这让一次写可以只改一个字里的个别字节。AXI-Lite 用 4 位 `wstrb`（一个 32 位字），AXI4 用「数据字节宽度」位。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [axi/axi4/rtl/AxiPkg.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi4/rtl/AxiPkg.vhd) | AXI4 全部记录类型、`_INIT_C`/`_FORCE_C` 初值、`AxiConfigType` 配置，以及 `axiWriteMasterInit`/`axiReadMasterInit`/`getAxiLen`/`getAxiReadBytes` 等工具函数。本讲的「类型字典」。 |
| [axi/axi4/rtl/AxiRam.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi4/rtl/AxiRam.vhd) | AXI4 RAM 从机：内部一块双口 RAM + 写三态机 + 读三态机，演示完整的突发收/发、字节写、响应上报。 |
| [axi/bridge/rtl/AxiToAxiLite.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/bridge/rtl/AxiToAxiLite.vhd) | AXI4 → AXI-Lite 降级桥：纯组合映射 + 一小段 ID 跟踪寄存器，把宽总线收窄到 32 位单拍。 |
| axi/axi4/ip_integrator/AxiRamIpIntegrator.vhd | `AxiRam` 的扁平端口外壳，被 cocotb 回归当作 toplevel，把记录拆回标准 `S_AXI_*` 引脚。 |
| tests/axi/axi4/test_AxiRam.py | `AxiRam` 的 cocotb 回归：多拍写 → 读回 → 字节覆盖，全用真实 AXI 握手。 |
| tests/axi/bridge/test_AxiToAxiLite.py | `AxiToAxiLite` 的 cocotb 回归：64 位 AXI 写两个 32 位字，经桥落到 AXI-Lite RAM。 |

> 说明：`AxiRam` 的 RAM 后端实际复用 `base/ram` 的 `SimpleDualPortRam` / `SimpleDualPortRamXpm` / `SimpleDualPortRamAlteraMf`（详见 u2-l3）。本讲聚焦 AXI4 协议层，RAM 推断细节只点到为止。

---

## 4. 核心概念与源码讲解

### 4.1 AXI4 五通道与记录类型

#### 4.1.1 概念说明

AXI4 是 ARM AMBA 的「完整版」总线协议。与 AXI-Lite（每通道单拍、数据固定 32 位）相比，它把一次访问拆成**五个独立的通道**，彼此用 `VALID/READY` 握手、可并发：

| 通道 | 方向 | 作用 |
|------|------|------|
| **AR**（Read Address） | 主→从 | 主机给出读地址、突发长度等 |
| **R**（Read Data） | 从→主 | 从机回送读数据与 `rresp`，末拍带 `rlast` |
| **AW**（Write Address） | 主→从 | 主机给出写地址、突发长度等 |
| **W**（Write Data） | 主→从 | 主机逐拍送写数据 + `wstrb`，末拍带 `wlast` |
| **B**（Write Response） | 从→主 | 从机回送写结果 `bresp` |

读路径走 AR→R 两个通道；写路径走 AW→W→B 三个通道。这正是「读两通道、写三通道、合起来五通道」。

为什么 SURF 要用记录？因为五通道展开成扁平线有几十根（地址、ID、len、size、burst、prot、cache、qos、region、data、strb、resp、last、各 valid/ready…）。把它们按「读/写 × 主/从」切成四条记录后，端口干净、可整体传给过程，且任意两条同类总线类型一致、能直接相连——这与 AXI-Lite 的做法一脉相承（见 u3-l1）。

#### 4.1.2 核心流程

记录的归属口诀和 AXI-Lite 完全一致——**VALID 与数据归生产方，READY 归消费方**。把它套到五通道上：

- **AR 通道**：主机是生产方，所以 `arvalid/araddr/arlen/...` 在 `AxiReadMasterType`；从机是消费方，`arready` 在 `AxiReadSlaveType`。
- **R 通道**：从机是生产方，`rvalid/rdata/rlast/rresp/rid` 在 `AxiReadSlaveType`；主机是消费方，`rready` 在 `AxiReadMasterType`。
- **AW 通道**：`awvalid/awaddr/...` 在 `AxiWriteMasterType`；`awready` 在 `AxiWriteSlaveType`。
- **W 通道**：`wvalid/wdata/wstrb/wlast/wid` 在 `AxiWriteMasterType`；`wready` 在 `AxiWriteSlaveType`。
- **B 通道**：`bvalid/bresp/bid` 在 `AxiWriteSlaveType`（从机生产）；`bready` 在 `AxiWriteMasterType`（主机消费）。

如此四条记录的并集正好无重复、无遗漏地覆盖全部五通道。读/写各两个 `*_INIT_C`（全 0 的静止态）与 `*_FORCE_C`（把消费侧 READY 拉高的「永远就绪」态），后者常用于「空从机/空主机」占位。

握手的数学关系很简单：一次成功的搬运发生在 `VALID='1'` 且 `READY='1'` 的同一上升沿。一次突发里地址通道只握一次手（给出 `arlen/awlen`），随后数据通道连续握 \(N\) 次手，其中：

\[
N = \text{ARLEN} + 1 \quad(\text{即寄存器里存的是「次数}-1\text{」})
\]

每拍搬运的字节数由 `arsize/awsize` 编码：

\[
\text{每拍字节数} = 2^{\,\text{arsize}}, \qquad \text{arsize} = \log_2(\text{DATA\_BYTES})
\]

#### 4.1.3 源码精读

先看记录定义。读主机记录把整个 AR 通道字段 + 读数据通道的 `rready` 收在一起：[AxiPkg.vhd:31-46](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi4/rtl/AxiPkg.vhd#L31-L46) ——注意 `arlen : slv(7 downto 0)` 恒为 8 位（最多 256 拍），`rready` 也在这里（主机消费读数据）。

对应的读从机记录则收 `arready` + 整个 R 通道：[AxiPkg.vhd:78-87](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi4/rtl/AxiPkg.vhd#L78-L87)，其中 `rdata` 直接声明为最大宽度 `AXI_MAX_DATA_WIDTH_C-1=1023` 位（[AxiPkg.vhd:25](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi4/rtl/AxiPkg.vhd#L25)），这是「全仓库统一最大宽度」策略，保证任意两条 AXI4 流类型相同可直接对接。

写侧结构对称：写主机 [AxiPkg.vhd:107-128](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi4/rtl/AxiPkg.vhd#L107-L128) 收 AW + W 通道字段 + `bready`（主机消费写响应），其中 `wstrb` 用最大 `AXI_MAX_WSTRB_WIDTH_C=128` 字节位（[AxiPkg.vhd:26](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi4/rtl/AxiPkg.vhd#L26)）；写从机 [AxiPkg.vhd:170-179](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi4/rtl/AxiPkg.vhd#L170-L179) 收 `awready/wready` + B 通道。

再看初值常量。`AXI_READ_MASTER_INIT_C` 全为 `'0'`（[AxiPkg.vhd:48-60](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi4/rtl/AxiPkg.vhd#L48-L60)），而 `AXI_READ_MASTER_FORCE_C` 唯一区别是 `rready => '1'`（[AxiPkg.vhd:61-73](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi4/rtl/AxiPkg.vhd#L61-L73)）。写从机的两个常量同理：`FORCE_C` 把 `awready/wready` 都拉高（[AxiPkg.vhd:187-192](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi4/rtl/AxiPkg.vhd#L187-L192)）。`FORCE_C` 的用途是给「不关心的对端」一个永远就绪的接口，避免 valid 卡死。

真实总线宽度不是 1024 位，由编译期 `AxiConfigType` 描述：[AxiPkg.vhd:212-217](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi4/rtl/AxiPkg.vhd#L212-L217) 有四个字段——`ADDR_WIDTH_C`、`DATA_BYTES_C`（每拍字节数，决定数据宽度）、`ID_BITS_C`、`LEN_BITS_C`（arlen/awlen 实际有效位宽，0~8）。`axiConfig(...)` 函数（[AxiPkg.vhd:219-224](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi4/rtl/AxiPkg.vhd#L219-L224)）给你一行构造它，默认 `DATA_BYTES_C=4`（32 位）、`LEN_BITS_C=4`（最多 16 拍）。

两个 `*Init` 函数把一份「合理初值」填进记录——读主机版 [AxiPkg.vhd:330-343](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi4/rtl/AxiPkg.vhd#L330-L343) 把 `arsize` 设成 `log2(DATA_BYTES_C)`、`arlen` 设成 `getAxiLen(cfg,4096)`、`arburst="01"`（INCR 递增突发）、`arcache="1111"`。这正是「按总线配置自动算好 size/len」的标准入口。

最关键的工具是 `getAxiLen`，它把「想搬多少字节」换算成「写进 arlen/awlen 的值」：[AxiPkg.vhd:350-361](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi4/rtl/AxiPkg.vhd#L350-L361)

```vhdl
return resize(toSlv(wordCount(burstBytes, axiConfig.DATA_BYTES_C)-1, axiConfig.LEN_BITS_C), 8);
```

其中 `wordCount(bytes, wordSize)` 是「向上取整除法」（[StdRtlPkg.vhd:803-811](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/StdRtlPkg.vhd#L803-L811)）。所以一次 `burstBytes` 字节的访问需要的拍数为：

\[
\text{拍数} = \left\lceil \frac{\text{burstBytes}}{\text{DATA\_BYTES\_C}} \right\rceil, \qquad
\text{AWLEN} = \text{拍数} - 1
\]

例如 64 位总线（`DATA_BYTES_C=8`）写 32 字节 → 拍数 \(=\lceil 32/8\rceil=4\) → `AWLEN=3`。带 `totalBytes/address` 参数的重载版（[AxiPkg.vhd:367-395](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi4/rtl/AxiPkg.vhd#L367-L395)）还会把突发限制在 4 KB 边界内（`max := 4096 - address(11 downto 0)`），这是 AXI 规范「单次突发不得跨 4 KB」的硬性要求。

#### 4.1.4 代码实践

**实践目标**：用纸笔（或脚本）验证 `getAxiLen` 与 `arsize` 的换算，建立「字节数 ↔ AWLEN/ARSIZE」的直觉。

**操作步骤**：

1. 打开 [AxiPkg.vhd:350-361](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi4/rtl/AxiPkg.vhd#L350-L361) 与 `axiReadMasterInit` 的 [AxiPkg.vhd:330-343](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi4/rtl/AxiPkg.vhd#L330-L343)。
2. 取一条配置 `axiConfig(ADDR_WIDTH_C=>16, DATA_BYTES_C=>8, ID_BITS_C=>4, LEN_BITS_C=>8)`。
3. 对 `burstBytes = 32`，手算 `wordCount(32,8)=4`，故 `arlen = 4-1 = 3`；`arsize = log2(8) = 3`（即 `011`，每拍 8 字节）。
4. 再算 `burstBytes = 24`：`wordCount(24,8)=3`，`arlen=2`。

**需要观察的现象**：`arlen` 永远比拍数少 1；`arsize` 是 2 的幂的对数，不是字节数本身。

**预期结果**：32 字节 → AWLEN=3、ARSIZE=3；24 字节 → AWLEN=2、ARSIZE=3。非整除时 `wordCount` 向上取整（例如 25 字节 → 拍数 4 → AWLEN=3）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `rready` 在 `AxiReadMasterType` 而不在 `AxiReadSlaveType`？
**答案**：读数据由从机生产、主机消费；READY 属于消费方，故 `rready` 归主机记录。这正是「VALID 与数据归生产方、READY 归消费方」。

**练习 2**：`AXI_READ_MASTER_INIT_C` 与 `AXI_READ_MASTER_FORCE_C` 唯一区别在哪？`FORCE_C` 何时有用？
**答案**：仅 `rready` 不同（INIT 为 '0'，FORCE 为 '1'）。当你有一个「不关心读数据」的占位主机时，用 `FORCE_C` 让它永远就绪，避免上游 valid 因无 ready 而死锁。

**练习 3**：一条 128 位（`DATA_BYTES_C=16`）、`LEN_BITS_C=8` 的总线，写 64 字节，`AWLEN` 与 `AWSIZE` 各是多少？
**答案**：拍数 \(=\lceil 64/16\rceil=4\)，`AWLEN=3`；`AWSIZE=log2(16)=4`（`100`，每拍 16 字节）。

---

### 4.2 AxiRam：突发读写与写选通

#### 4.2.1 概念说明

`AxiRam` 是 SURF 的通用 AXI4 RAM 从机：内部一块双口 RAM，外面套一对写/读状态机，把 AXI4 五通道翻译成「按地址逐拍读写存储体」。它是最小、最完整的 AXI4 从机范例，几乎每个工程都会用它做片上内存、描述符缓冲、DMA 落地缓冲。

它要做对四件事：

1. **突发接收**：地址通道只握一次手，记住 `awlen/arlen`，随后数据通道连收/连发 \(N\) 拍，每拍地址自增。
2. **字节写**：用 `wstrb` 决定每个字节写不写，支持「只改一个字里的一两个字节」。
3. **响应上报**：写完发一次 `bresp`，读每拍带 `rresp`；若 `wlast` 与 `awlen` 不一致（突发长度对不上），上报 `SLVERR`（`"10"`）。
4. **读流水**：块 RAM 有 1~2 拍读延迟，读状态机先用 `RD_PIPELINE_S` 把流水填满再吐数据。

#### 4.2.2 核心流程

地址换算先记住一条：RAM 字地址 = 字节地址右移 `OFFSET_C` 位，其中 `OFFSET_C = log2(DATA_BYTES_C)`（[AxiRam.vhd:47](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi4/rtl/AxiRam.vhd#L47)）。即丢掉字内字节偏移，剩 `ADDR_WIDTH_C = ADDR_WIDTH_G - OFFSET_C` 位字地址（[AxiRam.vhd:48](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi4/rtl/AxiRam.vhd#L48)）。

**写状态机**三态（[AxiRam.vhd:50-53](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi4/rtl/AxiRam.vhd#L50-L53)）：

```
WR_ADDR_S  -- 等 awvalid：握手、锁 awid/wrAddr(预减1)/awlen → WR_DATA_S
    │
WR_DATA_S  -- 每拍 wvalid：wready=1、wrAddr+1、写 wstrb/wrData、awlen-1
    │         wlast 或 awlen=0 时置 bvalid/bid
    │         wlast 且 awlen=0（对齐）→ bresp=00 → WR_ADDR_S
    │         否则（错位）            → bresp=10 → wlast?WR_ADDR_S:WR_BLOWOFF_S
    │
WR_BLOWOFF_S -- 错位后把剩余 W 数据「吹掉」（wready=1 但不写），wlast → WR_ADDR_S
```

注意 `wrAddr` 在 `WR_ADDR_S` 里**预减 1**（`v.wrAddr := awaddr(...) - 1`），因为 `WR_DATA_S` 每拍**先自增再写**（`v.wrAddr := r.wrAddr + 1`），这样第一拍写的就是请求地址，是个常见的「寄存输出换时序」技巧。

**读状态机**三态（[AxiRam.vhd:55-58](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi4/rtl/AxiRam.vhd#L55-L58)）：

```
RD_ADDR_S     -- 等 arvalid：握手、锁 arid/rdAddr、rdEn=11、arlen
    │           READ_LATENCY_G=0 → RD_DATA_S；否则 → RD_PIPELINE_S
    │
RD_PIPELINE_S -- rdEn=11、rdAddr+1，用 rdLat 计数把 RAM 读延迟流水填满 → RD_DATA_S
    │
RD_DATA_S     -- rdEn 默认 00（hold）；当 rvalid=0：rdEn=11、转发 rdData、rvalid=1、
                rdAddr+1、arlen-1；arlen=0 时 rlast=1 → RD_ADDR_S
```

读路径的 `rdEn` 是 2 位：`rdEn(0)` 给 RAM 的读使能、`rdEn(1)` 给输出寄存器使能，配合 `READ_LATENCY_G`（0/1/2）控制读延迟（见实体 [AxiRam.vhd:29](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi4/rtl/AxiRam.vhd#L29)）。

#### 4.2.3 源码精读

实体声明里 `AXI_CONFIG_G : AxiConfigType` 是唯一必填泛型（无默认值），把总线宽度带进来；端口就是两条记录对的从机侧：[AxiRam.vhd:24-41](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi4/rtl/AxiRam.vhd#L24-L41)。开头还有一条 elaboration 期断言：推断模式不允许 0 拍读延迟——[AxiRam.vhd:112-114](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi4/rtl/AxiRam.vhd#L112-L114)（`SimpleDualPortRam` 不支持组合读）。

RAM 后端三选一（[AxiRam.vhd:116-191](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi4/rtl/AxiRam.vhd#L116-L191)）：`SYNTH_MODE_G="xpm"`/`"altera_mf"`/`"inferred"` 分别例化 `SimpleDualPortRamXpm`/`...AlteraMf`/`SimpleDualPortRam`，三者都开 `BYTE_WR_EN_G=>true`、`BYTE_WIDTH_G=>8`，把 AXI 的 `wstrb` 直接喂给 RAM 的字节写使能。这就是 u2-l3 讲过的「字节写以每字节一位 weaByte 实现」在 AXI 层的复用。

写状态机 `WR_ADDR_S`：[AxiRam.vhd:214-229](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi4/rtl/AxiRam.vhd#L214-L229) 握手后锁 `wid`、把字地址**预减 1**、锁 `awlen`。核心的 `WR_DATA_S`：[AxiRam.vhd:231-266](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi4/rtl/AxiRam.vhd#L231-L266)——每拍把 `sAxiWriteMaster.wstrb(DATA_BYTES_C-1 downto 0)` 与 `wdata` 写进次态 `v.wstrb/v.wrData`，`awlen` 递减；当 `wlast` 或 `awlen=0` 时置 `bvalid/bid`，并用「`wlast` 且 `awlen=0`」判对齐：

```vhdl
if (sAxiWriteMaster.wlast = '1') and (r.awlen = 0) then
   v.sAxiWriteSlave.bresp := "00";   -- 对齐：OK
else
   v.sAxiWriteSlave.bresp := "10";   -- 错位：SLVERR，剩余进 BLOWOFF
```

写 RAM 的实际信号在 `comb` 末尾取自**现态 `r`**（[AxiRam.vhd:280-283](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi4/rtl/AxiRam.vhd#L280-L283)），`wrEn <= uOr(r.wstrb)`——只有至少一个字节使能才真正写，纯 0 的 `wstrb` 不触发写。

读状态机的 `RD_DATA_S`：[AxiRam.vhd:350-372](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi4/rtl/AxiRam.vhd#L350-L372) 在 `rvalid='0'`（主机还没收上一拍）时 `hold` 流水（`rdEn="00"`），否则转发 `rdData`、置 `rvalid`、`rdAddr+1`、`arlen-1`，`arlen=0` 时拉 `rlast` 回 `RD_ADDR_S`。注意读响应 `rresp` 走的是记录默认值（`AXI_READ_SLAVE_INIT_C` 里 `rresp=>"00"`，[AxiPkg.vhd:89-95](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi4/rtl/AxiPkg.vhd#L89-L95)），即 `AxiRam` 读永远回 OKAY。

整套逻辑包在标准双进程里：`comb` 算次态（[AxiRam.vhd:193-404](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi4/rtl/AxiRam.vhd#L193-L404)），同步复位在末尾 `if (axiRst='1') then v := REG_INIT_C`（[AxiRam.vhd:397-399](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi4/rtl/AxiRam.vhd#L397-L399)）；`seq` 只管上升沿打 `rin`（[AxiRam.vhd:406-411](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi4/rtl/AxiRam.vhd#L406-L411)）——与 u1-l5 的骨架完全一致（这里复位写死同步、极性固定为高，是 `AxiRam` 的简化选择）。

#### 4.2.4 代码实践

**实践目标**：跑通 `AxiRam` 的 cocotb 回归，观察一次多拍突发写 + 读回 + 字节覆盖，并核对 `BRESP/RRESP`。

**操作步骤**：

1. 先做 `ruckus` 源导入（生成 cocotb 用的源缓存，详见 u9-l1）：
   ```bash
   make MODULES=$PWD import
   ```
2. 单跑 `AxiRam` 回归：
   ```bash
   ./.venv/bin/python -m pytest -q tests/axi/axi4/test_AxiRam.py
   ```
3. 阅读测试 [test_AxiRam.py:53-79](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/axi/axi4/test_AxiRam.py#L53-L79)。它先写 24 字节 payload（在默认 64 位外壳下 = 3 拍，`AWLEN=2`）、读回比对，再在 `base_addr+9` 处覆盖 3 字节、再读回比对。`AxiMaster.write/read` 返回值带 `.resp`，断言它等于 `AxiResp.OKAY`。
4. （选做）把 payload 改成 32 字节（`bytes(range(0x10, 0x10+32))`），即为长度 4 的突发（`AWLEN=3`），重跑确认仍 OKAY。

**需要观察的现象**：写响应与读响应都应是 `OKAY`；读回字节与写入字节逐字节相等；字节覆盖后只有目标字节变化、其余不变（证明 `wstrb` 只改选中的字节）。

**预期结果**：两条 `assert ... == AxiResp.OKAY` 与两条 `assert bytes(...) == ...` 全部通过。若把测试里的 payload 长度调到非整除（如 25 字节），仍能跑通——`AxiMaster` 会自动多凑一拍并用 `wstrb` 屏蔽多余字节。

> 若本地尚未配好 GHDL + cocotb 环境，命令行现象标注为「待本地验证」；源码阅读部分（步骤 3）不受影响。

#### 4.2.5 小练习与答案

**练习 1**：`WR_ADDR_S` 里为什么要把 `wrAddr` 预减 1？
**答案**：因为 `WR_DATA_S` 每拍先 `v.wrAddr := r.wrAddr + 1` 再用 `r.wrAddr`（现态）寻址。预减 1 后，第一拍数据递增回请求地址，正好写到正确的首字。

**练习 2**：什么时候 `bresp` 会变成 `"10"`（SLVERR）？
**答案**：当数据末拍 `wlast='1'` 到来时 `awlen` 还没减到 0，或 `awlen` 已减到 0 但 `wlast` 还没来——即声明长度与实际数据拍数不一致。此时剩余数据进 `WR_BLOWOFF_S` 被丢弃。

**练习 3**：为什么 `AxiRam` 读路径的 `rresp` 永远是 OKAY？
**答案**：`AxiRam` 是一块真实 RAM，任意地址都能读、不会「解码失败」，故不存在 DECERR/SLVERR 的来源；读响应取记录初值 `rresp=>"00"` 即可。写路径则因长度错位才产生 SLVERR。

---

### 4.3 AxiToAxiLite：从 AXI4 降级到 AXI-Lite

#### 4.3.1 概念说明

很多控制/状态寄存器空间（如 u3 系列讲的 `AxiVersion`、`AxiDualPortRam`）是 AXI-Lite 从机——单拍、32 位。但 SoC 里跑的 DMA、CPU 往往只出 AXI4（突发、宽位）。`AxiToAxiLite` 就是这二者之间的**适配器**：把 AXI4 主机侧接到 AXI-Lite 从机侧。

它在文件头就钉死了前提（[AxiToAxiLite.vhd:6](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/bridge/rtl/AxiToAxiLite.vhd#L6)）：**只支持 32 位对齐地址、32 位事务**。也就是说它假设软件对 AXI-Lite 空间只发「单拍、单字」的访问，桥不需要拆/合突发，只做位宽与字段的重新映射。

#### 4.3.2 核心流程

桥几乎是纯组合映射，没有状态机：

- **写地址/写响应**：AW 通道的 `awaddr(31 downto 0)/awprot/awvalid` 与 `bready` 直连同名 AXI-Lite 信号；`awready/bvalid/wready` 反向直连。`bresp` 默认透传下游 AXI-Lite 响应，`EN_SLAVE_RESP_G=false` 时强制 OKAY。
- **写数据收窄**：AXI4 的 `wdata` 最宽 1024 位，AXI-Lite 只有 32 位。桥遍历所有 128 个字节通道，凡 `wstrb(i)='1'`，就把该字节「或」进对应的 32 位字位置，最终压成单个 32 位 `wdata`，并把 AXI-Lite 的 `wstrb` 固定为 `x"F"`（整字写）。
- **读地址**：`araddr(31 downto 0)/arprot/arvalid/rready` 直连。
- **读数据展宽**：AXI-Lite 回的 32 位 `rdata` 被复制到 AXI4 数据宽度的**每个 32 位字通道**，并把 `rlast` 恒置 `'1'`（AXI-Lite 本就是单拍，所以 AXI4 侧每读都是末拍）。
- **ID 跟踪**：唯一寄存的部分——在 AR/AW 握手成功那一拍，把 `arid/awid` 锁存到 `rid/bid`，让 AXI4 的 ID 体系不丢。

#### 4.3.3 源码精读

实体端口是「AXI4 从机侧 + AXI-Lite 主机侧」的标准对接：[AxiToAxiLite.vhd:27-47](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/bridge/rtl/AxiToAxiLite.vhd#L27-L47)，泛型 `EN_SLAVE_RESP_G`（[AxiToAxiLite.vhd:32](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/bridge/rtl/AxiToAxiLite.vhd#L32)）决定是否把下游 `bresp/rresp` 透传给 AXI4 侧。

写地址/响应直连：[AxiToAxiLite.vhd:53-62](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/bridge/rtl/AxiToAxiLite.vhd#L53-L62)，其中 `bresp <= axilWriteSlave.bresp when(EN_SLAVE_RESP_G) else AXI_RESP_OK_C`。读侧镜像：[AxiToAxiLite.vhd:64-72](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/bridge/rtl/AxiToAxiLite.vhd#L64-L72)，关键一行 `axiReadSlave.rlast <= '1'`——把 AXI-Lite 的单拍特性「伪装」成 AXI4 的「长度为 1 的突发」。

写数据收窄进程：[AxiToAxiLite.vhd:79-93](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/bridge/rtl/AxiToAxiLite.vhd#L79-L93)

```vhdl
for i in 0 to AXI_MAX_WSTRB_WIDTH_C-1 loop
   byte := (8*i) mod 32;
   if axiWriteMaster.wstrb(i) = '1' then
      wdata(byte+7 downto byte) := wdata(...) or axiWriteMaster.wdata(8*i+7 downto 8*i);
   end if;
end loop;
axilWriteMaster.wdata <= wdata;
axilWriteMaster.wstrb <= x"F";
```

它假设「只有某 32 位字内的字节使能会同时拉高」，把那 4 个字节「或」进同一个 32 位字；AXI-Lite 侧 `wstrb` 恒为 `x"F"`（整字写）。这正是文件头「Assumes only active 32 bits are asserted」的实现。

读数据展宽进程：[AxiToAxiLite.vhd:95-105](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/bridge/rtl/AxiToAxiLite.vhd#L95-L105) 把 32 位 `axilReadSlave.rdata` 复制到 AXI4 全宽的每个 32 位字通道。

ID 跟踪是唯一的时序逻辑，且同时支持同步/异步复位（用了 `RST_ASYNC_G`/`RST_POLARITY_G`，与 u1-l4 约定一致）：[AxiToAxiLite.vhd:108-126](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/bridge/rtl/AxiToAxiLite.vhd#L108-L126)——在 `arvalid & arready` 成立时锁 `rid <= arid`，`awvalid & awready` 成立时锁 `bid <= awid`。

#### 4.3.4 代码实践

**实践目标**：通过 `AxiToAxiLite` 回归，确认 64 位 AXI 写如何落到 32 位 AXI-Lite RAM，并理解读数据「复制到所有字通道」的现象。

**操作步骤**：

1. 阅读 [test_AxiToAxiLite.py:61-80](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/axi/bridge/test_AxiToAxiLite.py#L61-L80)：它向 `0x0020`、`0x0024` 各写一个 32 位字（`awid=5/6`），再从 AXI 读回，并直接读下游 AXI-Lite RAM（`axil_ram.read`）核对。
2. （选做）跑这条回归：`./.venv/bin/python -m pytest -q tests/axi/bridge/test_AxiToAxiLite.py`。
3. 跟踪一次 `tb.axi.write(0x0020, b"\x11\x22\x33\x44", awid=0x5)`：AXI4 侧 `wstrb` 只有低 4 字节为 1 → 收窄进程把这 4 字节或进 32 位 `wdata` → AXI-Lite RAM 在 `0x0020` 写下 `11 22 33 44`。ID 进程在 `awvalid & awready` 那拍把 `bid` 锁成 `0x5`。

**需要观察的现象**：下游 AXI-Lite RAM 的两个 32 位字分别等于写入值；从 `0x0020` 读 8 字节时，返回值是 `b"\x11\x22\x33\x44" * 2`（同一 32 位字被复制到高低两个字通道）。

**预期结果**：`axil_ram.read(0x0020,4)==b"\x11\x22\x33\x44"`、`bytes(low_read)==b"\x11\x22\x33\x44"*2`，两个响应均 OKAY。这条「读回数据被复制」正是 [AxiToAxiLite.vhd:95-105](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/bridge/rtl/AxiToAxiLite.vhd#L95-L105) 的直接体现。

> 命令现象「待本地验证」（依赖 GHDL + cocotb 环境）；源码跟踪（步骤 1、3）可独立完成。

#### 4.3.5 小练习与答案

**练习 1**：为什么桥把 `axiReadSlave.rlast` 恒置 `'1'`？
**答案**：AXI-Lite 每次读只回一拍 32 位数据。对 AXI4 主机而言，这一拍就是突发的唯一一拍，故 `rlast` 必须恒为 1，让主机知道「这一拍即末拍」。桥本质上把每次 AXI-Lite 读包装成「长度为 1 的 AXI4 突发」。

**练习 2**：如果软件对 AXI-Lite 空间发了一次真正的多拍 AXI4 写（`awlen>0`），桥会怎样？
**答案**：桥没有拆分突发的状态机，多拍数据会被 `wdata` 收窄逻辑反复覆盖到同一个 32 位字、`wstrb` 固定 `x"F"`，结果未定义。这正是文件头「only supports 32-bit ... transactions」的硬性前提——多拍突发必须先由别的模块拆成单拍。

**练习 3**：`EN_SLAVE_RESP_G=false` 有什么用？
**答案**：让 AXI4 侧的 `bresp/rresp` 恒为 OKAY，忽略下游 AXI-Lite 从机的真实响应。用于下游不会产生错误、或你想屏蔽错误让访问「永远成功」的场景。

---

## 5. 综合实践

**任务**：把本讲三个模块串起来，搭一个「AXI4 主机 → `AxiRam` 从机」的最小自检，并验证一次**长度为 4 的突发写再读回**。

**背景**：`test_AxiRam.py` 已经用 cocotb 的 `AxiMaster` + 扁平外壳 `AxiRamIpIntegrator` 搭好了这条路。外壳默认 `DATA_WIDTH_G=64`（即 `DATA_BYTES_C=8`），所以在它上面写 32 字节就是一次 4 拍突发（`AWLEN=3`）。

**操作步骤**：

1. 先做源导入：`make MODULES=$PWD import`（生成 `build/SRC_VHDL` 缓存，见 u9-l1）。
2. 复制一份 `tests/axi/axi4/test_AxiRam.py` 到本地实验目录，把 `burst_and_sparse_overwrite_test` 里的 payload 改成 32 字节：
   ```python
   base_addr = 0x0020
   payload = bytes(range(0x10, 0x10 + 32))   # 32B = 4 beats @64-bit
   ```
3. 跑你的实验用例（用 pytest 的 `-k` 选你的函数名，或直接改原文件重跑）：
   ```bash
   ./.venv/bin/python -m pytest -q tests/axi/axi4/test_AxiRam.py
   ```
4. 跟踪协议层（纸笔）：确认 `AxiMaster.write(0x0020, 32B)` 会驱动 `AWLEN=3`、`AWSIZE=3`、`AWBURST=01`（INCR）。对照本讲 4.1.2 的公式：拍数 \(=\lceil 32/8\rceil=4\)，`AWLEN=3`。
5. 跟踪从机层：在 [AxiRam.vhd WR_DATA_S](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi4/rtl/AxiRam.vhd#L231-L266) 里数 `awlen` 从 3 递减到 0 共 4 拍，第 4 拍 `wlast & awlen=0` 成立 → `bresp="00"`、回 `WR_ADDR_S`。
6. （延伸）把 `test_AxiToAxiLite.py` 也跑一遍，对照体会「AXI4 突发」与「AXI-Lite 单拍」在桥处的形状变化。

**需要观察的现象**：写响应 `BRESP=OKAY`、读响应 `RRESP=OKAY`；读回的 32 字节与写入逐字节相等。若在波形里看，应看到 AW 通道握手 1 次、W 通道握手 4 次、B 通道握手 1 次；AR 握手 1 次、R 握手 4 次（末拍 `rlast=1`）。

**预期结果**：4.2.4 的两条 OKAY 断言与字节相等断言全部通过；纸笔推导的 `AWLEN=3` 与状态机里 `awlen` 递减计数吻合。

> 若本地无 GHDL/cocotb 环境，步骤 2、3 的运行结果为「待本地验证」；步骤 4、5 的源码跟踪与公式推导可独立完成，是本综合实践的核心。

## 6. 本讲小结

- AXI4 把一次访问拆成 **AR/R/AW/W/B 五个独立握手通道**，读走 AR→R、写走 AW→W→B；SURF 用 `AxiReadMasterType`/`AxiReadSlaveType`/`AxiWriteMasterType`/`AxiWriteSlaveType` 四条记录折叠它们，沿用「VALID 与数据归生产方、READY 归消费方」。
- 突发由 `arlen/awlen`（存「次数−1」）、`arsize/awsize`（存 \(\log_2\) 字节数）描述；`getAxiLen` 用向上取整除法把字节数换算成 `A*LEN`，重载版还兼顾 4 KB 边界。
- `AxiRam` 是最小完整 AXI4 从机：写三态机（ADDR→DATA→BLOWOFF）+ 读三态机（ADDR→PIPELINE→DATA），用 `SimpleDualPortRam` 承载、用 `wstrb` 做字节写，长度错位时上报 `bresp="10"`。
- `wrAddr` 预减 1、寄存输出取现态 `r`、`wrEn=uOr(r.wstrb)` 等技巧，是「用双进程骨架实现 AXI 从机」的典型写法。
- `AxiToAxiLite` 是纯组合降级桥（仅 ID 跟踪寄存）：收窄 `wdata` 到 32 位、展宽 `rdata` 到全宽、`rlast` 恒 1，把 AXI4 单拍 32 位事务接到 AXI-Lite；硬性前提是 32 位对齐、单拍事务。
- `_INIT_C` 全 0 静止、`_FORCE_C` 把消费侧 READY 拉高，二者配合可优雅处理「空接口占位」。

## 7. 下一步学习建议

- **u8-l2（AXI-Stream DMA V2）**：本讲的 `AxiRam` 是「AXI4 内存从机」，下一讲把 AXI4 用作「DMA 写/读引擎的目标总线」，看 `AxiStreamDmaV2Write/Read` 如何把 AXI-Stream 帧落到 `AxiRam` 这样的内存里——你会再次见到 `awlen/wstrb/bresp` 的真实用法。
- **深读 `AxiPkg.vhd` 余下函数**：`getAxiLenProc`（[AxiPkg.vhd:401-443](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi4/rtl/AxiPkg.vhd#L401-L443)）把 `getAxiLen` 的长组合链拆成两拍以改善时序，是「时序优化」的好例子；`getAxiReadBytes`（[AxiPkg.vhd:446-469](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi4/rtl/AxiPkg.vhd#L446-L469)）反算一次读请求的字节数。
- **桥接全家桶**：`axi/bridge/rtl/` 下还有 `AxiLiteToDrp`（AXI-Lite 到 Xilinx DRP）、`IpBusToAxiLite`/`AxiLiteToIpBus`、`SlvArraytoAxiLite` 等，对照本讲的 `AxiToAxiLite` 看「记录化总线之间如何互相翻译」。
- **回归方法学**：本讲多次引用 `tests/axi/axi4/test_AxiRam.py` 与 `tests/axi/bridge/test_AxiToAxiLite.py`，其 cocotb + pytest + GHDL 的组织方式在 u9-l1、u9-l2 系统讲解，建议结合阅读。
