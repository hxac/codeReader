# SRP：SLAC 寄存器协议（SRPv3）

## 1. 本讲目标

本讲讲解 SURF 的 **SLAC Register Protocol v3（SRPv3）**——一种把「本地 AXI-Lite 寄存器空间」序列化成「SSI 数据帧」、从而能经任意流式链路（PGP / 以太网 / RSSI …）被远端访问的协议。

学完后你应该能够：

- 说清 **为什么需要 SRP**：AXI-Lite 是片内并行总线，无法直接跨板/跨网；SRP 把寄存器读写事务打包成帧在流式链路上传输。
- 默写出 **SRPv3 的帧格式**：5 个 32 位头字 + 可选 payload + 1 个尾字，以及 READ / WRITE / POSTED_WRITE / NULL 四种操作码的语义。
- 看懂 [`SrpV3Core`](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/rtl/SrpV3Core.vhd) 的状态机如何解析请求帧、驱动 `srpReq`/`srpAck` 接口、再组装响应帧。
- 理解 [`SrpV3AxiLite`](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/rtl/SrpV3AxiLite.vhd) 如何把 SRP 直接落到本地 AXI-Lite 五通道上。
- 亲手画出一次 SRPv3 **WRITE** 在链路上的请求帧与响应帧，标出地址、数据、操作码字段。

## 2. 前置知识

本讲建立在你已经掌握以下三块内容之上（若不熟请先看对应讲义）：

- **AXI-Lite 记录类型（u3-l1）**：四个读写主/从记录、`AXI_RESP_OK_C` 等响应码、VALID/READY 握手。
- **AXI-Stream 记录与配置（u4-l1）**：`AxiStreamMasterType`/`AxiStreamSlaveType`、`tValid`/`tReady`/`tLast`/`tData`/`tKeep`/`tDest`。
- **SSI 侧带与帧边界（u5-l1）**：SSI 把 SOF（帧起始）、EOFE（错误帧结束）编码进 TUSER，EOF 复用 `tLast`，`tDest` 当虚拟通道（VC）。

先讲一个直觉问题：**为什么不能直接用 AXI-Lite 跨链路？**

AXI-Lite 是一组并行的、需要严格握手的多通道信号（AR/R/AW/W/B 五通道）。它只能在 FPGA 片内或同一块板卡的短线上走。如果你想从一台电脑、或经一根光纤（PGP）、或经以太网去读写另一片 FPGA 里的寄存器，并行总线是传不过去的——你必须把「读/写哪个地址、多长、什么数据」这件事**序列化成一串字节**，在串行数据流上传输。

SRPv3 就是这个「序列化」的约定：

- **请求端（发起方）**：把一次 AXI-Lite 读/写打包成一个 **SSI 请求帧**（带 SOF/EOF 帧边界）发出去。
- **响应端（执行方）**：收到帧后，在本地真实发起 AXI-Lite 事务，再把结果打包成一个 **SSI 响应帧**发回。

因此 SRP 是「寄存器事务」与「流式数据」之间的翻译层。它在 SURF 协议栈里位于 SSI（提供帧边界）之上、具体链路（PGP/Eth/RSSI）之下。

> 关键术语：**SRP**（SLAC Register Protocol）、**req/ack 接口**（Core 对外的请求/应答握手）、**posted write**（不需响应的「发射后不管」写）、**footer**（帧尾的状态字）。

## 3. 本讲源码地图

本讲涉及的关键文件（均在 `protocols/srp/` 下）：

| 文件 | 作用 |
|------|------|
| [rtl/SrpV3Pkg.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/rtl/SrpV3Pkg.vhd) | 协议常量（版本号、操作码）与请求/响应记录类型 `SrpV3ReqType`/`SrpV3AckType`。是协议的「数据字典」。 |
| [rtl/SrpV3Core.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/rtl/SrpV3Core.vhd) | **协议引擎**：解析 SSI 请求帧 → 驱动 `srpReq`/`srpAck` + 读/写数据流 → 组装响应帧。与后端总线无关。 |
| [rtl/SrpV3AxiLite.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/rtl/SrpV3AxiLite.vhd) | **AXI-Lite 适配器**：自包含地实现一套状态机，直接驱动本地 AXI-Lite 五通道（32 位对齐、32 位事务专用）。 |
| [wrappers/SrpV3AxiLiteWrapper.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/wrappers/SrpV3AxiLiteWrapper.vhd) | 扁平端口封装（给 cocotb / Vivado IP integrator 用）。 |
| [tests/protocols/srp/srp_test_utils.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/protocols/srp/srp_test_utils.py) | Python 帧构造器与校验器，是帧格式的「可执行规约」。 |

**一个容易混淆的点**：SRPv3 有多个落地实现，它们的关系是：

- `SrpV3Core`：通用协议引擎，对外只给 `srpReq`/`srpAck` + 写数据流 + 读数据流，**不知道**后端是哪种总线。
- `SrpV3Axi`：实例化 `SrpV3Core`，后端接 **AXI4**（支持非对齐、大事务）。
- `SrpV3AxiLiteFull`：`SrpV3Axi` + `AxiToAxiLite` 桥，是「经 Core」接到 AXI-Lite 的通用路径。
- `SrpV3AxiLite`：**不**实例化 Core，而是一套独立的、为 32 位对齐优化的状态机。本讲的重点之一就是它。

可以验证这个依赖关系：`SrpV3Axi` 内部例化 `SrpV3Core`，`SrpV3AxiLiteFull` 再例化 `SrpV3Axi` 与 `AxiToAxiLite`（见 [SrpV3Axi.vhd:108](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/rtl/SrpV3Axi.vhd#L108) 与 [SrpV3AxiLiteFull.vhd:70](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/rtl/SrpV3AxiLiteFull.vhd#L70)、[:101](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/rtl/SrpV3AxiLiteFull.vhd#L101)）；而 `SrpV3AxiLite` 是平级的独立实现。

---

## 4. 核心概念与源码讲解

本讲的三个最小模块：**① SRPv3 请求/响应帧格式**、**② SrpV3Core 协议引擎**、**③ SrpV3AxiLite 适配器**。

### 4.1 SRPv3 请求/响应：把寄存器事务序列化成 SSI 帧

#### 4.1.1 概念说明

SRPv3 把一次寄存器事务表示成一帧 32 位字流。帧的总体结构是：

```
┌──────────────────────────────────────────────┐
│  请求帧 Request Frame                         │
│  ┌──────┬──────┬──────┬──────┬──────┐        │
│  │word0 │word1 │word2 │word3 │word4 │  头部   │  SOF 标在 word0
│  ├──────┴──────┴──────┴──────┴──────┤        │
│  │      payload（仅 WRITE 有）       │        │
│  └───────────────────────────────────┘        │
│  （请求帧没有 footer；tLast 标在最后一拍）     │
└──────────────────────────────────────────────┘

┌──────────────────────────────────────────────┐
│  响应帧 Response Frame                        │
│  ┌──────┬──────┬──────┬──────┬──────┐        │
│  │word0 │word1 │word2 │word3 │word4 │  头部   │  SOF 标在 word0
│  ├──────┴──────┴──────┴──────┴──────┤        │
│  │  payload（READ 回数据/WRITE 回显）│        │
│  ├───────────────────────────────────┤        │
│  │            footer word            │  尾部  │  tLast 标在此拍
│  └───────────────────────────────────┘        │
└──────────────────────────────────────────────┘
```

四种操作码决定帧的形状：

| 操作码 | 值 | 含义 | 请求帧 payload | 响应帧 |
|--------|----|----|----------------|--------|
| `SRP_READ_C` | `00` | 非投递读 | 无 | 头部 + 读回数据 + 尾部 |
| `SRP_WRITE_C` | `01` | 非投递写 | 写数据 | 头部 + 回显数据 + 尾部 |
| `SRP_POSTED_WRITE_C` | `10` | 投递写（fire-and-forget） | 写数据 | **无响应帧** |
| `SRP_NULL_C` | `11` | 空操作（心跳/探测） | 无 | 头部 + 尾部 |

> 「posted write 不产生任何响应帧」是一个关键行为，后文会从源码证明它。

#### 4.1.2 核心流程

**头部 5 个字的字段布局**（每字 32 位，统一适用于请求帧与响应帧）：

| 字 | 字段 | 位 | 说明 |
|----|------|----|----|
| word0 | version | [7:0] | 协议版本，固定 `0x03` |
| word0 | opCode | [9:8] | 操作码 |
| word0 | spare | [20:10] | 保留；其中 **bit14** 在 AxiLite 实现里被用作 `ignoreMemResp`（忽略从机错误响应） |
| word0 | prot | [23:21] | AXI 保护位（映射到 `arprot`/`awprot`） |
| word0 | timeoutSize | [31:24] | 超时阈值（单位是 100 ms 窗口的个数；0=不超时） |
| word1 | tid | [31:0] | 事务 ID，由发起方填写，响应原样回显，用于配对请求与响应 |
| word2 | addr | [31:0] | 目标地址低 32 位 |
| word3 | addr | [63:32] | 目标地址高 32 位 |
| word4 | reqSize | [31:0] | **= 字节数 − 1**（即最后一字节的索引） |

> **reqSize 的口径容易踩坑**：它不是「字节数」也不是「字数」，而是「字节计数减一」。例如要写 4 字节，`reqSize = 3`（`0x0000_0003`）。合法事务要求 `reqSize[1:0] = "11"`（字节数是 4 的整数倍）；Core 进一步要求写事务的 `reqSize[31:12] = 0`（写长度有上限）。内部把 `reqSize[31:2]` 当作「32 位字计数减一」来用（记作 `cntSize`）。

**尾部 footer word 的字段布局**（仅响应帧有）：

| 位 | 字段 | 含义 |
|----|------|------|
| [7:0] | memResp | 后端总线响应码（低 2 位是 AXI 的 `rresp`/`bresp`；高位是 SRP 自定义错误） |
| [8] | timeout | 是否发生超时 |
| [9] | eofe | 帧是否带 EOFE（链路层错误帧） |
| [10] | frameError | 帧结构错误（如 tLast 时机不对） |
| [11] | verMismatch | 版本号不匹配 |
| [12] | reqError / reqSizeError | 请求非法（长度/对齐/操作未启用） |
| [13] | timeout | 超时（与 bit8 冗余备份） |
| [31:14] | reserved | 0 |

一次 **WRITE（写 4 字节）** 的伪代码流程：

```
发起方:                                  执行方(SrpV3AxiLite):
  组装请求帧                               收帧 → 解析头部
  word0 = 0x03 | (WRITE<<8)               验证 opCode/addr/reqSize
  word1 = tid                              若非法 → 直接回 footer(置 reqError)
  word2 = addr[31:0]                       否则 → 在 AXI-Lite 上发 AW/W
  word3 = addr[63:32]                      等 B 响应
  word4 = 字节数-1                          逐字推进地址
  payload = 数据字                          组装响应帧
  发送(SOF 在 word0, tLast 在 payload)      word0..word4 回显
                                           payload 回显数据
                                           footer = 状态
                                           发送(SOF 在 word0, tLast 在 footer)
```

#### 4.1.3 源码精读

先看协议的「数据字典」`SrpV3Pkg.vhd`。版本号与四个操作码常量定义在此：

版本号与操作码定义——[protocols/srp/rtl/SrpV3Pkg.vhd:37-43](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/rtl/SrpV3Pkg.vhd#L37-L43)：`SRP_VERSION_C = x"03"`，四个操作码 `SRP_READ_C`/`SRP_WRITE_C`/`SRP_POSTED_WRITE_C`/`SRP_NULL_C` 分别为 `"00"/"01"/"10"/"11"`。

请求记录 `SrpV3ReqType`——[protocols/srp/rtl/SrpV3Pkg.vhd:45-64](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/rtl/SrpV3Pkg.vhd#L45-L64)：把帧头里的 `request`（握手脉冲）、`remVer`、`opCode`、`spare`、`prot`、`tid`、`addr`、`reqSize` 打包成一条记录，并配 `SRPV3_REQ_INIT_C` 全零初值。Core 解析完帧头后，就把结果填进这个记录交给下游。

响应记录 `SrpV3AckType`——[protocols/srp/rtl/SrpV3Pkg.vhd:66-73](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/rtl/SrpV3Pkg.vhd#L66-L73)：极简，只有 `done`（事务完成脉冲）与 `respCode`（8 位响应码）。下游做完一次 AXI 事务后，用它通知 Core。

帧格式的「可执行规约」在 Python 测试工具里，与上面的字段表一一对应。看 `srpv3_header`——[tests/protocols/srp/srp_test_utils.py:195-221](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/protocols/srp/srp_test_utils.py#L195-L221)：`word0` 把 version/opCode/spare/ignore_mem_resp/prot/timeout 按位拼起来；`req_size` 由 `byte_count - 1` 得到（见 [:45-47](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/protocols/srp/srp_test_utils.py#L45-L47)），印证了「reqSize = 字节数 − 1」。footer 各错误位的掩码定义在 [:26-30](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/protocols/srp/srp_test_utils.py#L26-L30)，与下文 Core 的 footer 字段完全对齐。

#### 4.1.4 代码实践

**实践目标**：亲手算出一次 SRPv3 WRITE 的请求帧与响应帧的每一个字，验证你理解了字段布局。

**操作步骤**：假定要向地址 `0x0000_0040` 写入 4 字节数据 `0xDEAD_BEEF`，事务号 `tid = 1`。

1. 算 reqSize：字节数 = 4，所以 `reqSize = 4 - 1 = 3 = 0x0000_0003`。
2. 算 word0：`version=0x03`，`opCode=SRP_WRITE_C=01`，其余为 0：
   `word0 = 0x03 | (0b01 << 8) = 0x0000_0103`。
3. 依次填 word1..word4 与 payload。
4. 标出 SOF/tLast 位置（SSI 侧带）。

**预期结果**（请求帧，SOF 标在 word0，`tLast` 标在 payload）：

```
请求帧（WRITE，4 字节）:
word0 = 0x0000_0103   ; version=0x03, opCode=01(WRITE)   <- SOF
word1 = 0x0000_0001   ; tid = 1
word2 = 0x0000_0040   ; addr[31:0]
word3 = 0x0000_0000   ; addr[63:32]
word4 = 0x0000_0003   ; reqSize = 字节数-1 = 3
word5 = 0xDEAD_BEEF   ; payload（写数据）                 <- tLast
```

**响应帧**（非投递写会回显数据，footer 全 0 表示成功，`tLast` 标在 footer）：

```
响应帧（WRITE 回显）:
word0 = 0x0000_0103   ; version+opCode 回显              <- SOF
word1 = 0x0000_0001   ; tid 回显
word2 = 0x0000_0040   ; addr 回显
word3 = 0x0000_0000
word4 = 0x0000_0003   ; reqSize 回显
word5 = 0xDEAD_BEEF   ; 回显的写数据
word6 = 0x0000_0000   ; footer: memResp=0(OK), 各错误位=0 <- tLast
```

你可以用仓库里的 Python 构造器自行核对（**示例代码**，非项目原有）：

```python
from tests.protocols.srp.srp_test_utils import SrpV3Request, srpv3_frame
req = SrpV3Request(opcode=0x1, tid=1, address=0x40, byte_count=4)
print([f"0x{w:08X}" for w in srpv3_frame(req, payload=[0xDEADBEEF])])
# 期望: ['0x00000103','0x00000001','0x00000040','0x00000000','0x00000003','0xDEADBEEF']
```

**需要观察的现象**：若把 `opcode` 改成 `0x2`（POSTED_WRITE），同样的请求帧发出去后，仿真里**收不到任何响应帧**（见 4.2.3 的源码证据）。

#### 4.1.5 小练习与答案

**练习 1**：要读 8 字节（2 个 32 位字），`reqSize` 应填多少？`cntSize`（即 `reqSize[31:2]`）又是多少？

> **答案**：字节数 = 8，`reqSize = 8 - 1 = 7 = 0x0000_0007`；`cntSize = 7 >> 2 = 1`，表示要搬 2 个字（计数从 0 到 1）。

**练习 2**：为什么 `reqSize` 用「字节数 − 1」而不是「字节数」？

> **答案**：这样 `reqSize` 直接就是「最后一字节的索引」，地址自增和长度判断可以直接拿它做比较（如 `cnt = reqSize[31:2]` 判断是否到最后一字）；同时要求 `reqSize[1:0] = "11"` 恰好等价于「字节数是 4 的整数倍」，一举两得。

---

### 4.2 SrpV3Core：协议引擎状态机（req/ack 接口）

#### 4.2.1 概念说明

`SrpV3Core` 是 SRPv3 的「协议大脑」，但它**故意不知道**后端总线长什么样。它对外的接口分三组：

- **上游（流式侧）**：`sAxisMaster/Slave`（收请求帧）、`mAxisMaster/Slave`（发响应帧）。
- **下游（事务侧）**：`srpReq`（`SrpV3ReqType`，告诉下游「要做一次什么事务」）、`srpAck`（`SrpV3AckType`，下游回报「做完了，响应码是这个」）。
- **数据旁路**：`srpWrMaster/Slave`（把写数据以 AXI-Stream 形式送给下游）、`srpRdMaster/Slave`（下游把读出的数据以 AXI-Stream 形式送回来）。

这种「流进、流出 + 一对 req/ack 握手」的割裂设计，让 Core 能复用在 AXI4、AXI-Lite、甚至自定义内存后端上——后端只要会消费 `srpReq` 并回 `srpAck` 即可。

#### 4.2.2 核心流程

Core 的主状态机（请求方向）：

```
            ┌─────────────────────────────────────────────┐
            │                                             ▼
 IDLE_S ──► HDR_REQ_S ──► HDR_RESP_S ──┬─► READ_S ───► WAIT_ACK_S ──► FOOTER_S ──► IDLE_S
   ▲         (逐字锁存     (校验+        └─► WRITE_S ──►      │           (组装
   │          5 字头)       发响应头)                          │            footer)
   │                                            (等 srpAck.done)
   └── BLOWOFF_RX_S / BLOWOFF_READ_DATA_S （异常时丢弃残留数据）
```

- **IDLE_S**：等请求帧的 SOF；若 FIFO 溢出或检测到上一笔残留读数据，先进 BLOWOFF 清场。
- **HDR_REQ_S**：按 `hdrCnt` 逐字把 word0..word4 锁存进 `srpReq` 记录，同时做帧结构检查（如 tLast 出现得太早）。
- **HDR_RESP_S**：一边发响应帧的头部 5 字，一边做语义校验（版本不匹配、reqSize 非法、操作未启用、地址未对齐……）；任一不过就置错误标志并直接跳 FOOTER_S；全过则置 `srpReq.request='1'` 正式发起事务，进入 READ_S 或 WRITE_S。
- **READ_S / WRITE_S**：搬运数据。READ 把下游送回的读数据转发到响应流；WRITE 把请求帧里的 payload 同时转发给下游 `srpWrMaster` 和回显到响应流。
- **WAIT_ACK_S**：等下游 `srpAck.done`，锁存 `respCode`。
- **FOOTER_S**：发 footer word（汇总所有错误标志），回到 IDLE_S。

它沿用了 u1-l5 的双进程骨架：`comb` 算次态、`seq` 打寄存器，并复用了 u2 的 `SsiFifo`/`AxiStreamFifoV2`/`AxiStreamResize` 做跨时钟域与位宽整形。

#### 4.2.3 源码精读

Core 内部把外部流的宽度统一规整成 32 位 SSI 流，这是它的「内部表示」——[protocols/srp/rtl/SrpV3Core.vhd:71-78](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/rtl/SrpV3Core.vhd#L71-L78)：`SRP_AXIS_CONFIG_C` 固定 `TDATA_BYTES_C=4`、`TUSER_MODE_C=TUSER_FIRST_LAST_C`（SSI 帧边界）、`TKEEP_MODE_C=TKEEP_COMP_C`。无论外部 AXI-Stream 有多宽，进 Core 前都先被 `SsiFifo`/`AxiStreamResize` 降成 4 字节。

状态机定义见 [SrpV3Core.vhd:80-89](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/rtl/SrpV3Core.vhd#L80-L89)。

**请求头锁存**——`HDR_REQ_S` 用 `hdrCnt` 逐字解析头部，[SrpV3Core.vhd:317-356](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/rtl/SrpV3Core.vhd#L317-L356)：`hdrCnt=0` 时从 `tData` 抽出 version/opCode/spare/prot/timeoutSize，`hdrCnt=1..3` 抽 tid 与 addr，`hdrCnt=4` 抽 reqSize，并据此判断 tLast 时机是否与操作码相符（READ/NULL 应在 word4 结束，WRITE 应还有 payload）。

**响应头构造与语义校验**——`HDR_RESP_S`，[SrpV3Core.vhd:372-469](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/rtl/SrpV3Core.vhd#L372-L469)。注意 [:364-368](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/rtl/SrpV3Core.vhd#L364-L368)：只有「非 posted write」才把响应头部的 `tValid` 拉高——这就是 posted write 不发响应帧的第一处证据。校验逻辑在 [:411-443](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/rtl/SrpV3Core.vhd#L411-L443)：写事务 `reqSize[31:12] /= 0` 置 `reqError`；非字节访问时 `reqSize[1:0] /= "11"` 或地址未对齐都置 `reqError`；`READ_EN_G`/`WRITE_EN_G` 关闭时对应操作码也置 `reqError`。

**posted write 在数据与尾部也被静音**——`WRITE_S` 的 [:541](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/rtl/SrpV3Core.vhd#L541) 行：`v.txMaster.tValid := toSl(r.srpReq.opCode /= SRP_POSTED_WRITE_C)`，即 posted write 不回显数据；`FOOTER_S` 的 [:628-631](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/rtl/SrpV3Core.vhd#L628-L631) 同样不为 posted write 发 footer。三处合起来证明：**posted write 不产生任何响应帧**。

**footer 字段汇总**——[SrpV3Core.vhd:633-641](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/rtl/SrpV3Core.vhd#L633-L641)：把 `memResp` 放 `[7:0]`，`timeout/eofe/frameError/verMismatch/reqError/timeout` 依次放 `[8..13]`，与 4.1.2 的 footer 表逐位对应。

**超时机制**——[SrpV3Core.vhd:69](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/rtl/SrpV3Core.vhd#L69) 定义 `TIMEOUT_C = getTimeRatio(SRP_CLK_FREQ_G, 10.0) - 1`，即一个 100 ms 窗口；READ_S/WRITE_S/WAIT_ACK_S 里每满一个窗口就把 `timeoutCnt` 加一，达到请求头里填的 `timeoutSize` 就置 `timeout` 并强切到 FOOTER_S（见 [:518-531](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/rtl/SrpV3Core.vhd#L518-L531)）。这能防止下游总线死锁把整个协议引擎卡死。

#### 4.2.4 代码实践

**实践目标**：跟踪 `srpReq`/`srpAck` 这对握手在 WRITE 期间的时序，理解 Core 与下游的分工。

**操作步骤**（源码阅读型实践）：

1. 打开 [SrpV3Core.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/rtl/SrpV3Core.vhd)，定位 `HDR_RESP_S` 末尾 [:448-453](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/rtl/SrpV3Core.vhd#L448-L453)：`v.srpReq.request := '1'` 并清零定时器——这是「Core 把事务交给下游」的时刻。
2. 跟到 `WAIT_ACK_S` [:591-596](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/rtl/SrpV3Core.vhd#L591-L596)：等 `srpAck.done='1'`，一旦到来就 `v.srpReq.request := '0'` 并锁存 `srpAck.respCode` 到 `memResp`。
3. 注意 [:599](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/rtl/SrpV3Core.vhd#L599)：要同时等 `request='0'` 与 `done='0'` 才进 FOOTER_S，确保握手干净收尾。

**需要观察的现象**：`srpReq.request` 在请求头校验通过后拉高，并在下游回 `done` 的同一拍被清零——这是一个标准的「请求-应答」往返。若你给下游接一个永远不回 `done` 的从机，超时机制会在 `timeoutSize` 个 100 ms 后强制收尾并在 footer 置 timeout 位。

**预期结果**：能画出 `srpReq.request` 与 `srpAck.done` 两根信号在一次 WRITE 期间的相对时序（request 先高，若干拍后 done 高一拍，request 随即落下）。完整波形**待本地仿真验证**（可用 `tests/protocols/srp/test_SrpV3Core.py` 跑）。

#### 4.2.5 小练习与答案

**练习 1**：Core 为什么把内部流统一降成 4 字节（32 位）宽？

> **答案**：SRP 协议本身按 32 位字定义头部与事务粒度（reqSize 以字节计、cntSize 以 32 位字计）。把内部表示固定成 4 字节，状态机的「一字一拍」逻辑最简单；外部任意宽度由入口的 `SsiFifo`/`AxiStreamResize` 负责整形。

**练习 2**：`BLOWOFF_READ_DATA_S` 状态（[:290-295](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/rtl/SrpV3Core.vhd#L290-L295)）存在的意义是什么？

> **答案**：若上一笔事务被中途打断（如超时），下游可能还在往 `srpRdMaster` 送残留读数据。下次进 IDLE_S 时若发现这些残留数据，必须先把它们丢弃到 `tLast`，否则会污染下一笔事务——这正是「清场」状态的作用。

---

### 4.3 SrpV3AxiLite：把 SRP 落到本地 AXI-Lite

#### 4.3.1 概念说明

`SrpV3AxiLite` 是面向「远端经链路来访问本地 AXI-Lite 寄存器」这一最常见场景的优化实现。它有两个特点：

1. **自包含**：不例化 `SrpV3Core`，而是自己写一套状态机，直接驱动 AXI-Lite 的 `mAxilReadMaster`/`mAxilWriteMaster` 五通道信号。省去了 Core 的 req/ack 中转，时序与资源更省。
2. **专门化**：只支持 32 位对齐地址与 32 位事务。文件头明确写道——[SrpV3AxiLite.vhd:8-11](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/rtl/SrpV3AxiLite.vhd#L8-L11)：若要非对齐地址或非 32 位事务，请改用 `SrpV3Axi` + `AxiToAxiLite`（即 `SrpV3AxiLiteFull`）。

它的上游仍是 SSI 流（收请求帧、发响应帧），下游直接是 AXI-Lite 主机接口。也就是说，它把「一段 SSI 流」变成了「本地 CPU 可见的 AXI-Lite 主机」——远端软件经链路发帧，就等于在本地发起 AXI-Lite 读写。

#### 4.3.2 核心流程

`SrpV3AxiLite` 的状态机（注意头部被拆成 `HDR_REQ0..3` 多个状态，每个状态锁存一个字）：

```
IDLE_S ─► HDR_REQ0_S ─► HDR_REQ1_S ─► HDR_REQ2_S ─► HDR_REQ3_S ─► HDR_RESP_S
                                                                   │
                                  ┌────────────────────────────────┤
                                  ▼                                ▼
                          AXIL_RD_REQ_S ◄──┐               AXIL_WR_REQ_S ◄──┐
                                  │        │                       │         │
                          AXIL_RD_RESP_S ───┘               AXIL_WR_RESP_S ──┘
                                  │  (逐字自增 addr)                │  (逐字自增 addr)
                                  ▼                                ▼
                              FOOTER_S ◄──────────────────────── FOOTER_S
```

- `HDR_REQ0..3_S`：把 word0（在 IDLE_S 里直接锁）、tid、addr 低、addr 高、reqSize 逐字存进本地寄存器。
- `HDR_RESP_S`：一边发响应头部，一边做校验；特别地，它会针对 AXI-Lite 的限制做额外检查（见下）。
- `AXIL_RD_REQ_S`/`AXIL_RD_RESP_S`：发起一次 AXI-Lite 读（`arvalid`/`rready`），收到 `rvalid` 后把 `rdata` 推进响应流，地址 `+1` 字（`addr[31:2]+1`），循环到搬完 `cntSize+1` 个字。
- `AXIL_WR_REQ_S`/`AXIL_WR_RESP_S`：发起一次 AXI-Lite 写（`awvalid`/`wvalid`/`bready`），收 `bvalid` 后地址 `+1` 字，循环。
- `FOOTER_S`：发尾部状态字。

#### 4.3.3 源码精读

本地常量与操作码别名——[SrpV3AxiLite.vhd:76-80](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/rtl/SrpV3AxiLite.vhd#L76-L80)（`NON_POSTED_READ_C`/`NON_POSTED_WRITE_C`/`POSTED_WRITE_C`/`NULL_C`，值与 Pkg 一致）。

**头部锁存**——IDLE_S 里第一拍就把 word0 拆开，[SrpV3AxiLite.vhd:366-378](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/rtl/SrpV3AxiLite.vhd#L366-L378)：注意 [:372](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/rtl/SrpV3AxiLite.vhd#L372) 把 `tData(14)` 解出来当 `ignoreMemResp`——这正是 4.1.2 提到的「spare 区 bit14 的特殊用途」。随后 `HDR_REQ0..3_S` 依次锁 tid/addr/reqSize。

**针对 AXI-Lite 的额外校验**——`HDR_RESP_S`，[SrpV3AxiLite.vhd:525-551](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/rtl/SrpV3AxiLite.vhd#L525-L551)：因为 AXI-Lite 只有 32 位地址空间，故 `addr[63:32] /= 0` 时把 `memResp(7)` 置 1（地址错误，[:532-537](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/rtl/SrpV3AxiLite.vhd#L532-L537)）；地址未 4 字节对齐置 `memResp(6)`（[:539-544](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/rtl/SrpV3AxiLite.vhd#L539-L544)）；reqSize 非 4 字节倍数置 `memResp(5)`（[:546-551](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/rtl/SrpV3AxiLite.vhd#L546-L551)）。这些 `memResp` 高位错误会直接跳 FOOTER_S，不在 AXI-Lite 上真正发事务。

**响应头回显能力位**——[SrpV3AxiLite.vhd:490-499](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/rtl/SrpV3AxiLite.vhd#L490-L499)：响应 word0 的 `[10..13]` 位回显本模块的能力（UnalignedAccess=0、MinAccessSize=0 表示仅 32 位、WriteEn/ReadEn），让发起方知道对端支持什么。

**AXI-Lite 读事务驱动**——`AXIL_RD_REQ_S` [:596-611](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/rtl/SrpV3AxiLite.vhd#L596-L611)：把 `addr[31:0]` 填进 `araddr`、`prot` 填进 `arprot`，拉高 `arvalid`/`rready`；随后 `AXIL_RD_RESP_S` [:613-661](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/rtl/SrpV3AxiLite.vhd#L613-L661) 收 `rvalid`，把 `rdata` 推进响应流，并在 [:655](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/rtl/SrpV3AxiLite.vhd#L655) 处 `addr[31:2]+1` 自增一个字。`ignoreMemResp='1'` 时即使 `rresp` 非法也照搬数据（填全 1），见 [:634-638](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/rtl/SrpV3AxiLite.vhd#L634-L638)。

**AXI-Lite 写事务驱动**——`AXIL_WR_REQ_S` [:678-719](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/rtl/SrpV3AxiLite.vhd#L678-L719)：把 payload 当拍数据同时写进 `wdata`、`awaddr` 设为当前地址，拉高 `awvalid`/`wvalid`/`bready`；`AXIL_WR_RESP_S` [:721-761](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/rtl/SrpV3AxiLite.vhd#L721-L761) 收 `bvalid`，锁 `bresp` 到 `memResp[1:0]`，再自增地址循环。

**帧长度保护**——入口挂了一个 `SsiFrameLimiter`，[SrpV3AxiLite.vhd:201-222](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/rtl/SrpV3AxiLite.vhd#L201-L222)：`FRAME_LIMIT_G = ceil(4116/TDATA_BYTES)`（20 字节头尾 + 4096 字节最大 payload），把超长/卡死的请求帧强制截断，防止恶意或异常帧耗尽 FIFO。

#### 4.3.4 代码实践

**实践目标**：用仓库自带的 cocotb 测试，端到端验证一次 WRITE 真的变成了本地 AXI-Lite 写。

**操作步骤**：

1. 阅读测试方法学头——[tests/protocols/srp/test_SrpV3AxiLite.py:11-22](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/protocols/srp/test_SrpV3AxiLite.py#L11-L22)：测试用 `cocotbext.axi.AxiLiteRam` 当本地 AXI-Lite 从机，往 wrapper 的 SSI 侧发 SRPv3 帧，再检查 RAM 内容与响应帧。
2. 看 wrapper 如何把扁平 SSI 端口的 TUSER 拼成 SOF/EOFE——[wrappers/SrpV3AxiLiteWrapper.vhd:98-99](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/srp/wrappers/SrpV3AxiLiteWrapper.vhd#L98-L99)：`ssiSetUserEofe(..., S_AXIS_TUSER(SSI_EOFE_C))`、`ssiSetUserSof(..., S_AXIS_TUSER(SSI_SOF_C))`，其中 `SSI_EOFE_C=0`、`SSI_SOF_C=1`（见 [SsiPkg.vhd:29-30](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiPkg.vhd#L29-L30)），即 TUSER 的 bit0=EOFE、bit1=SOF。
3. （可选）运行该回归：在仓库根目录先 `make MODULES=$PWD import` 生成源缓存，再 `. .venv/bin/activate && python -m pytest -q tests/protocols/srp/test_SrpV3AxiLite.py`。

**需要观察的现象**：

- 发一帧 WRITE 后，DUT 的 `M_AXIL_AWVALID`/`M_AXIL_WVALID` 会随之拉高，`M_AXIL_AWADDR` 等于请求帧里的 addr，`M_AXIL_WDATA` 等于 payload。
- `M_AXIS`（响应流）会回一帧，其 footer 的 `[7:0]`（memResp）在成功时为 0。
- 发一帧非法地址（如 `addr[63:32] /= 0`）的 WRITE，响应 footer 的 bit7（FOOTER_ADDRESS_ERROR）会被置 1，且 `M_AXIL_AWVALID` 根本不会拉高（校验阶段就被拦下）。

**预期结果**：测试通过即证明「SSI 帧 → AXI-Lite 事务 → 响应帧」的完整翻译正确。若你无法本地运行 GHDL/cocotb，可改为纯源码阅读：对照 `assert_srpv3_response`（[srp_test_utils.py:239-258](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/protocols/srp/srp_test_utils.py#L239-L258)）的断言，逐条说明它校验了响应头回显、payload 回显、footer 错误位、tdest 一致性。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `SrpV3AxiLite` 要求 `addr[63:32] = 0`，而 `SrpV3Core` 不做这个检查？

> **答案**：AXI-Lite 的地址宽度只有 32 位，无法表达 64 位地址，所以 AxiLite 适配器必须拒绝高 32 位非零的请求；而 Core 是总线无关的，64 位地址是合法字段，是否可用由具体后端（如 AXI4）决定。

**练习 2**：`ignoreMemResp`（spare 区 bit14）在什么场景下有用？

> **答案**：某些调试场景下，发起方只关心「把数据塞进某个地址」，不关心从机是否回 SLVERR/DECERR（比如写一个已知会应答错误的清除寄存器）。置 `ignoreMemResp=1` 后，即使 `rresp`/`bresp` 非法，模块也不置 `memResp` 错误位、照常推进，避免误报。

---

## 5. 综合实践

把三个模块串起来，做一个端到端的「远端读写本地寄存器」演练。

**任务**：假设你是远端软件，要通过一条 SSI 流读写一片 FPGA 的 AXI-Lite 寄存器。请完成下面整条链路：

1. **构造请求**：用 `tests/protocols/srp/srp_test_utils.py` 里的 `SrpV3Request` + `srpv3_frame`，构造「向地址 `0x0000_0000` 写 4 字节 `0xCAFEBABE`，tid=5」的请求帧，列出每个字的十六进制值。
2. **追踪入帧**：说明这帧进入 `SrpV3AxiLite` 后，依次经过 `SsiFrameLimiter` → `AxiStreamFifoV2`（RX_FIFO）→ 状态机 `IDLE_S→HDR_REQ0..3→HDR_RESP→AXIL_WR_REQ/RESP→FOOTER_S`，最终在 `M_AXIL_*` 上产生一次 AXI-Lite 写。
3. **追踪出帧**：说明响应帧如何由 `HDR_RESP_S`（发头）+`AXIL_WR_*`（回显数据）+`FOOTER_S`（发尾）拼出，再经 `TX_FIFO` 从 `M_AXIS` 送回。
4. **读回验证**：再构造一帧对同一地址的 READ，验证响应帧的 payload 是否等于刚写入的 `0xCAFEBABE`。
5. **异常注入**：把请求地址改成 64 位非零（如 `0x1_0000_0000`），预测响应 footer 的哪个 bit 会被置位，并解释为什么 `M_AXIL` 上不会出现任何事务。

**交付物**：一张表，列出 WRITE 请求帧、WRITE 响应帧、READ 请求帧、READ 响应帧的每个字；以及一句对异常情形的预测。

> 提示：4 字节写时 `reqSize=3`、`cntSize=0`（搬 1 个字）；地址自增发生在 `addr[31:2]+1`，对 4 字节单字事务恰好不增。

---

## 6. 本讲小结

- **SRPv3 的本质**是把 AXI-Lite 寄存器事务序列化成 SSI 帧，让寄存器空间能经任意流式链路被远端访问；它是「寄存器事务」与「流式数据」之间的翻译层。
- **帧格式**为「5 个 32 位头字 + 可选 payload + 1 个尾字」；头部含 version/opCode/tid/addr/reqSize，其中 **reqSize = 字节数 − 1**；尾部汇总 memResp 与各类错误标志。
- **四种操作码**：READ（回数据）、WRITE（回显数据）、**POSTED_WRITE（完全不发响应帧）**、NULL（仅头尾的心跳）。
- **`SrpV3Core`** 是总线无关的协议引擎，对外给 `srpReq`/`srpAck` + 读/写数据流，靠一套 `IDLE→HDR_REQ→HDR_RESP→READ/WRITE→WAIT_ACK→FOOTER` 状态机驱动，并带 100 ms×timeoutSize 的看门狗。
- **`SrpV3AxiLite`** 是面向 32 位对齐场景的自包含优化实现，不例化 Core，直接驱动 AXI-Lite 五通道，逐字自增地址搬运；并针对 AXI-Lite 的 32 位地址/对齐限制做了额外校验。
- **协议族关系**：`SrpV3AxiLite`（独立、32 位专用）与 `SrpV3AxiLiteFull`（= `SrpV3Axi` + `AxiToAxiLite`，经 Core 的通用路径）是两条并存的落地路线，按是否需要非对齐/大事务来选择。

## 7. 下一步学习建议

- **去看 SRP 的承载链路**：SRP 帧最终要跑在真实链路上。建议接着学 **u7 的 PGP / RSSI**：PGP 提供虚拟通道（VC）承载 SSI 流，RSSI 在其上加可靠重传——SRP 帧正是它们的典型载荷。
- **对比 SRPv0**：`protocols/srp/rtl/` 下还有更精简的 `SrpV0AxiLite`/`AxiLiteSrpV0`，读一遍能看清 SRP 从 v0 到 v3 演进了什么（如 64 位地址、prot、超时）。
- **回到 AXI-Lite 与 SSI 基础**：若对响应码、`axiSlaveRegister` 或 SOF/EOFE 还有陌生感，回看 **u3-l1/u3-l2** 与 **u5-l1**。
- **动手跑回归**：参照 **u9-l1/u9-l2** 的 cocotb 工具链，真正跑一次 `tests/protocols/srp/` 下的测试，用波形验证本讲描述的握手时序。
