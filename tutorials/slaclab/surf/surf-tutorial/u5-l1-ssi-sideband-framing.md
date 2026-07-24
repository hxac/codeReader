# SSI 侧带与帧边界（SsiPkg / SsiFifo）

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 **SSI（Simple Stream Interface）** 在 AXI-Stream 之上「多加了什么」：把 `SOF`（帧起始）、`EOF`（帧结束）、`EOFE`（错误帧结束）三个帧边界标志编码进 `TUSER` 侧带。
- 解释 `TDEST` 为什么在 SSI 里被当成**虚拟通道（VC, Virtual Channel）**，以及为什么 SSI 最多支持 16 个 VC。
- 用 `ssiAxiStreamConfig(...)` 构造一条 SSI 流配置，并手动标出一帧中每一拍 `TUSER` 的比特含义。
- 看懂 `SsiFifo` 如何用「入向过滤器 + AXI-Stream FIFO + 出向过滤器」三件套保持帧边界、并自动给畸形帧打上 `EOFE`。

本讲承接 [u4-l1 AXI-Stream 记录与配置](u4-l1-axistream-records.md)：那里讲的是「如何描述一条裸 AXI-Stream 流」，本讲讲的是「在这条流上叠加一层帧语义」。

## 2. 前置知识

### 2.1 为什么需要 SSI

裸 AXI-Stream 只有 `tLast` 一个帧边界信号：`tLast=1` 表示一帧的最后一拍。这对「逐拍搬运字节」够用，但在 SLAC 的数据采集场景里远远不够。真实需求是：

- 我要知道**一帧从哪一拍开始**（`SOF`），而不只是从哪一拍结束。
- 一个帧到了下游如果**中途出错**了，我希望用一个专门的「错误结束」标志（`EOFE`）告诉下游：「这帧别用了，丢掉」——它和正常的 `EOF` 不是一回事。
- 我希望在**同一条物理流**上同时承载多路逻辑数据（虚拟通道），用 `TDEST` 区分，而不是为每路数据拉一条独立的流。

SSI 就是把这三件事以「不增加新端口、只复用 AXI-Stream 已有侧带」的方式定义出来的一套约定。它的全称是 **Simple Stream Interface**，是 SURF 几乎所有流式协议（SRP、PGP、RSSI、packetizer…）的公共底座。

### 2.2 你需要先记住的 AXI-Stream 事实

来自 u4-l1，这里只复述要点，不重复推导：

- 一条 AXI-Stream 流折叠成 `AxiStreamMasterType`（生产方驱动 `tValid` + 数据与侧带）和 `AxiStreamSlaveType`（消费方驱动 `tReady`）两个记录。
- `AxiStreamConfigType` 是编译期常量，描述这条流的「真实形状」：`TDATA_BYTES_C`、`TUSER_BITS_C`、`TDEST_BITS_C`、`TKEEP_MODE_C`、`TUSER_MODE_C` 等。
- `TUSER` 在 SURF 里被当作「逐字节的侧带小数组」来用：可以按字节位置（`bytePos`）读写其中某一位，工具函数是 `axiStreamGetUserBit` / `axiStreamSetUserBit`。**SSI 正是靠这套工具把帧边界塞进 TUSER 的。**

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [protocols/ssi/rtl/SsiPkg.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiPkg.vhd) | SSI 的「单一事实来源」：定义 `SOF`/`EOFE` 在 TUSER 中的比特号、`SsiMasterType`/`SsiSlaveType` 记录、`ssiAxiStreamConfig()` 配置生成器、以及读写侧带的 `ssiGetUserSof`/`ssiGetUserEofe` 等函数。 |
| [protocols/ssi/rtl/SsiFifo.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiFifo.vhd) | 帧边界 FIFO：在 `AxiStreamFifoV2` 前后各套一层 SSI 帧过滤器，保证「只有合法成帧的数据」才出现在主端口。 |
| [protocols/ssi/rtl/SsiObFrameFilter.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiObFrameFilter.vhd) | 出向过滤器：给「重复 SOF」「帧中途 `TDEST` 变化」等畸形帧打上 `EOFE` 并提前截断，是理解 SsiFifo 行为的关键。 |
| [axi/axi-stream/rtl/AxiStreamPkg.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd) | 提供 `axiStreamGetUserBit`/`SetUserBit` 等底层 TUSER 访问函数，SSI 侧带编码建立其上。 |
| [protocols/README.md](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/README.md) | protocols 子树导航，明确要求「保留 `TUSER`/SOF/EOFE、VC 字段等侧带语义」。 |

---

## 4. 核心概念与源码讲解

### 4.1 SSI 侧带编码：SOF / EOF / EOFE 进 TUSER

#### 4.1.1 概念说明

SSI 的核心思想一句话：**不新增端口，把帧边界信号「嫁接」到 AXI-Stream 已有的侧带上。** 具体嫁接关系如下：

| SSI 语义 | 复用的 AXI-Stream 信号 | 说明 |
|----------|------------------------|------|
| `SOF`（Start Of Frame，帧起始） | `TUSER` 的某个比特 | 标记一帧的第一拍 |
| `EOF`（End Of Frame，帧正常结束） | `tLast` | **直接复用 AXI-Stream 的 `tLast`**，不另设 |
| `EOFE`（EOF Error，错误结束） | `TUSER` 的另一个比特 | 标记「这帧坏掉了，下游应丢弃」 |
| 虚拟通道号 | `TDEST` | 见 4.2 节 |

注意一个常被忽略的点：**`EOF` 不占 TUSER 比特，它就是 `tLast`。** SSI 只往 TUSER 里塞了两个新比特：`SOF` 和 `EOFE`。这从包常量看得很清楚：

[SsiPkg.vhd:29-35](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiPkg.vhd#L29-L35) 定义了这两个比特在「每个字节的 TUSER 小段」里的编号，以及 SSI 默认的侧带宽度：

```vhdl
constant SSI_EOFE_C : natural := 0;   -- EOFE 占每个字节 TUSER 小段的第 0 位
constant SSI_SOF_C  : natural := 1;   -- SOF  占每个字节 TUSER 小段的第 1 位

constant SSI_TUSER_BITS_C : positive := 2;  -- SSI 每字节只用 2 位 TUSER：{SOF, EOFE}
constant SSI_TDEST_BITS_C : positive := 4;  -- TDEST 4 位 => 16 个虚拟通道
constant SSI_TID_BITS_C   : natural  := 0;  -- SSI 不使用 TID
constant SSI_TSTRB_EN_C   : boolean  := false; -- SSI 不启用 TSTRB
```

也就是说，在 SSI 的每个字节对应的 TUSER 小段（2 位）里：

| 比特 | 含义 |
|------|------|
| bit 0 (`SSI_EOFE_C`) | EOFE |
| bit 1 (`SSI_SOF_C`)  | SOF  |

#### 4.1.2 核心流程：SOF 放「首字节」、EOFE 放「末字节」

AXI-Stream 一拍数据可能有多个字节（比如 16 字节），而 `TUSER` 是「逐字节」的小数组。SSI 规定：

- **`SOF` 只看帧第一拍的「首字节」TUSER**（`bytePos = 0`）。
- **`EOFE` 只看帧最后一拍的「末有效字节」TUSER**（`bytePos = -1`，即由 `tKeep` 解析出的最后一个有效字节）。

这种「首字节存 SOF、末字节存 EOFE」的布局对应 `AxiStreamConfigType.TUSER_MODE_C = TUSER_FIRST_LAST_C`（SSI 的默认模式，见 4.2.2）。它的好处是：一帧里只有首拍需要关心 SOF、只有末拍需要关心 EOFE，中间拍的 TUSER 可以全为 0，节省了侧带带宽。

读写流程用两个函数封装（注意它们传给底层函数的 `bytePos` 不同）：

```text
读 SOF : ssiGetUserSof(cfg, m)  -> axiStreamGetUserBit(cfg, m, SSI_SOF_C,  bytePos=0)   // 首字节
读 EOFE: ssiGetUserEofe(cfg, m) -> axiStreamGetUserBit(cfg, m, SSI_EOFE_C, bytePos=-1)  // 末字节(默认)
写 SOF : ssiSetUserSof(cfg, m, v)  -> axiStreamSetUserBit(cfg, m, SSI_SOF_C,  v, bytePos=0)
写 EOFE: ssiSetUserEofe(cfg, m, v) -> axiStreamSetUserBit(cfg, m, SSI_EOFE_C, v, bytePos=-1)
```

底层 `axiStreamGetUserBit` 如何定位那一位？它先把 `bytePos`（或 `-1` 表示的末字节）换算成字节索引 `pos`，再按下式取位：

\[ \text{bitIndex} = \text{TUSER\_BITS\_C} \times \text{pos} + \text{bitPos} \]

即「每个字节占 `TUSER_BITS_C` 位，先定位到字节、再在字节内偏移 `bitPos`」。

#### 4.1.3 源码精读

**两个读函数的 `bytePos` 差异**是本节最关键的一行代码。[SsiPkg.vhd:169-177](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiPkg.vhd#L169-L177) 的 `ssiGetUserEofe` 不传 `bytePos`，因此走默认值 `-1`（末字节）：

```vhdl
function ssiGetUserEofe (...) return sl is
begin
   ret := axiStreamGetUserBit(axisConfig, axisMaster, SSI_EOFE_C);  -- bytePos 默认 -1 = 末字节
   return ret;
end function;
```

而 [SsiPkg.vhd:280-296](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiPkg.vhd#L280-L296) 的 `ssiGetUserSof` / `ssiSetUserSof` 显式传 `0`（首字节）：

```vhdl
function ssiGetUserSof (...) return sl is
begin
   ret := axiStreamGetUserBit(axisConfig, axisMaster, SSI_SOF_C, 0);  -- bytePos = 0 = 首字节
   return ret;
end function;

procedure ssiSetUserSof (...) is
begin
   axiStreamSetUserBit(axisConfig, axisMaster, SSI_SOF_C, sof, 0);   -- 写进首字节
end procedure;
```

底层位定位在 [AxiStreamPkg.vhd:277-290](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L277-L290) 与 [AxiStreamPkg.vhd:313-327](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L313-L327)：

```vhdl
-- axiStreamGetUserBit: 取出 bytePos 对应字节的 TUSER 小段，再索引 bitPos
user := axiStreamGetuserField(axisConfig, axisMaster, bytePos);
return(user(bitPos));

-- axiStreamSetUserBit: 直接算出物理位号并赋值
pos := axiStreamGetUserPos(axisConfig, axisMaster, bytePos);
axisMaster.tUser((axisConfig.TUSER_BITS_C*pos) + bitPos) := bitValue;
```

其中 `axiStreamGetUserPos` 在 `bytePos = -1` 时，用 `getTKeep(...)` 解析出**最后一个有效字节**的索引（[AxiStreamPkg.vhd:221-243](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L221-L243)）。这就是「EOFE 跟着末有效字节走」的实现机制——即使最后一拍只有部分字节有效，EOFE 也能被正确放到那个字节上。

**一个常用的「强制错误帧」常量**。[SsiPkg.vhd:37-45](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiPkg.vhd#L37-L45) 定义了 `SSI_MASTER_FORCE_EOFE_C`：它把 `tValid='1'`、`tLast='1'`（EOF）、`tUser` 全 1（于是 SOF 与 EOFE 同时拉高）打包好，下游模块遇到致命错误时直接把这个常量推进流，就能向下游广播一个「错误结束帧」：

```vhdl
constant SSI_MASTER_FORCE_EOFE_C : AxiStreamMasterType := (
   tValid => '1', tLast => '1',       -- EOF
   tUser  => (others => '1'), ...);   -- EOFE（同时 SOF 也=1，表示「单拍错误帧」）
```

#### 4.1.4 代码实践

**实践目标**：亲手验证「SOF 在首字节、EOFE 在末字节」的布局。

**操作步骤（源码阅读型，无需仿真）**：

1. 打开 [SsiPkg.vhd:122-143](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiPkg.vhd#L122-L143)，确认 `ssiGetUserSof`/`ssiSetUserSof` 与 `ssiGetUserEofe`/`ssiSetUserEofe` 这四个声明的参数表。
2. 跳到它们的函数体（[SsiPkg.vhd:169-177](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiPkg.vhd#L169-L177) 与 [SsiPkg.vhd:280-296](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiPkg.vhd#L280-L296)），对比两者调用 `axiStreamGetUserBit`/`SetUserBit` 时传的 `bytePos`。
3. 打开 [AxiStreamPkg.vhd:313-327](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L313-L327)，写下 `pos` 与物理位号的换算式。

**需要观察的现象 / 预期结果**：

- `ssiGetUserSof` 传 `bytePos=0`（首字节），`ssiGetUserEofe` 不传 `bytePos`（走默认 `-1`=末字节）。
- 物理位号 = `TUSER_BITS_C × pos + bitPos`；对 SSI（`TUSER_BITS_C=2`），首字节 SOF 在位 1，末字节 EOFE 在位 `2×(末字节索引)+0`。
- 结论：**同一帧里 SOF 只可能在首拍出现、EOFE 只可能在末拍出现**，这与「帧边界」语义完全吻合。

> 待本地验证：若你有 GHDL/cocotb 环境，可写一个最小 TB，构造一帧后用 `ssiGetUserSof`/`ssiGetUserEofe` 读回，断言二者分别为 1 的拍位与设计一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么 SSI 不为 `EOF` 单独分配一个 TUSER 比特？
**答案**：因为 AXI-Stream 已经有 `tLast` 精确表达「帧的最后一拍」，SSI 直接复用 `tLast` 作为正常 `EOF`，只新增 `SOF` 与 `EOFE` 两个 TUSER 比特即可。

**练习 2**：`SSI_MASTER_FORCE_EOFE_C` 把 `tUser` 设成全 `'1'`，此时这一拍的 SOF 和 EOFE 分别是多少？为什么这样设计是安全的？
**答案**：SOF=1、EOFE=1。它表示一个「单拍即结束」的错误帧（既是一帧的开始也是结束），下游收到后只需识别 EOFE=1 即可丢弃整帧，SOF=1 不会造成歧义，因为这个帧只有一拍。

---

### 4.2 虚拟通道 TDEST 与 ssiAxiStreamConfig

#### 4.2.1 概念说明：TDEST = 虚拟通道（VC）

一条 SSI 物理流上可能同时承载多路逻辑数据。SSI 的做法是：**把 AXI-Stream 的 `TDEST` 当成虚拟通道号**。一帧从 SOF 到 EOF 期间 `TDEST` 必须保持不变，整帧归属于同一个 VC；下游可以按 `TDEST` 把不同 VC 的帧分发到不同处理路径。

`TDEST` 在 SSI 中固定为 **4 位**（`SSI_TDEST_BITS_C = 4`），所以一条 SSI 流最多支持

\[ 2^{4} = 16 \text{ 个虚拟通道（VC0\,..\,VC15）} \]

这与 PGP、SRP 等上层协议的「VC」概念一脉相承——后面学 PGP 时你会看到，PGP 的虚拟通道路由正是把目标 VC 号放进 SSI 的 `TDEST`。

> 重要约束（贯穿全 SSI 子树）：**SSI 不支持「交错 TDEST」（interleaved tDEST）**。即同一时刻流里只能有一帧在传输，不允许「VC0 的帧还没 EOF，就插入 VC1 的帧」。这也是 [SsiFifo.vhd:10](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiFifo.vhd#L10) 文件头注释明确写出的限制。

#### 4.2.2 核心流程：ssiAxiStreamConfig 一次生成完整配置

构造一条 SSI 流，只需调用 `ssiAxiStreamConfig(dataBytes => ...)`。它把 SSI 的全部约定（`TUSER_BITS_C=2`、`TDEST_BITS_C=4`、`TID_BITS_C=0`、`TSTRB_EN_C=false`、`TUSER_MODE_C=TUSER_FIRST_LAST_C`）一次性填进一个 `AxiStreamConfigType`，避免每个模块手填时出错。

```text
ssiAxiStreamConfig(dataBytes, tKeepMode, tUserMode, tDestBits, tUserBits, tIdBits)
   -> AxiStreamConfigType:
        TDATA_BYTES_C = dataBytes          // 用户指定（如 4/8/16）
        TUSER_BITS_C  = tUserBits  (默认 2 = SSI_TUSER_BITS_C)
        TDEST_BITS_C  = tDestBits  (默认 4 = SSI_TDEST_BITS_C => 16 VC)
        TID_BITS_C    = tIdBits    (默认 0)
        TKEEP_MODE_C  = tKeepMode  (默认 TKEEP_COMP_C)
        TSTRB_EN_C    = false                // SSI 不用 TSTRB
        TUSER_MODE_C  = tUserMode  (默认 TUSER_FIRST_LAST_C)  // 首末字节各存一份侧带
```

其中 `TUSER_FIRST_LAST_C` 模式来自 [AxiStreamPkg.vhd:78](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamPkg.vhd#L78) 定义的四选一枚举（`TUSER_NORMAL_C / TUSER_FIRST_LAST_C / TUSER_LAST_C / TUSER_NONE_C`）。它决定了「TUSER 侧带只在首字节和末字节各存一份有效副本」，正是 4.1 节 SOF/EOFE 布局的前提。

#### 4.2.3 源码精读

**配置生成器函数体**在 [SsiPkg.vhd:149-167](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiPkg.vhd#L149-L167)，每一行注释都点明了对应字段：

```vhdl
ret.TDATA_BYTES_C := dataBytes;       -- 用户可配的数据宽度
ret.TUSER_BITS_C  := tUserBits;       -- 2 个 TUSER 位: EOFE, SOF
ret.TDEST_BITS_C  := tDestBits;       -- 4 位 TDEST 用于 VC
ret.TID_BITS_C    := tIdBits;         -- 可选 TID
ret.TKEEP_MODE_C  := tKeepMode;
ret.TSTRB_EN_C    := SSI_TSTRB_EN_C;  -- SSI 不支持 TSTRB
ret.TUSER_MODE_C  := tUserMode;       -- 侧带只在 last(及 first) 拍有效
```

注意 `TSTRB_EN_C` 被硬编码为 `SSI_TSTRB_EN_C = false`（不接受用户覆盖）——SSI 明确不使用 `TSTRB`，这是协议约定。

**默认配置常量** [SsiPkg.vhd:60](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiPkg.vhd#L60)：

```vhdl
constant SSI_CONFIG_INIT_C : AxiStreamConfigType := ssiAxiStreamConfig(16);  -- 默认 16 字节(128 位)数据
```

**SSI 自己的便捷记录**。除了直接复用 AXI-Stream 记录，SsiPkg 还提供了一对更「人类友好」的记录 [SsiPkg.vhd:65-81](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiPkg.vhd#L65-L81)，把 `SOF`/`EOF`/`EOFE` 暴露成显式字段，并在它们与 `AxiStreamMasterType` 之间用 `ssi2AxisMaster` / `axis2SsiMaster` 互转（[SsiPkg.vhd:180-237](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiPkg.vhd#L180-L237)）。这样上层模块可以用 `m.sof := '1'` 这种直观写法，由转换函数自动把 `sof` 写进 TUSER 首字节、把 `eof` 映射到 `tLast`：

```vhdl
type SsiMasterType is record
   valid : sl; ... dest : slv(SSI_TDEST_BITS_C-1 downto 0);
   packed : sl;  sof : sl;  eof : sl;  eofe : sl;   -- 显式的帧边界字段
end record;
```

#### 4.2.4 代码实践

**实践目标**：用 `ssiAxiStreamConfig` 构造一条 32 位（4 字节）SSI 流，写出它每一拍的 TUSER 比特布局。

**操作步骤**：

1. 调用 `ssiAxiStreamConfig(dataBytes => 4)`（其余参数取默认）。得到：
   - `TDATA_BYTES_C = 4`，`TDEST_BITS_C = 4`（16 个 VC），`TUSER_BITS_C = 2`，`TUSER_MODE_C = TUSER_FIRST_LAST_C`。
2. 因 `TUSER_MODE_C = TUSER_FIRST_LAST_C`，**只有首字节与末有效字节**各存一份 2 位侧带。
3. 对一帧 3 拍（word0=SOF, word1=中间, word2=EOF 且 EOFE=1），按下表标注每一拍 TUSER 的「首字节小段」与「末字节小段」。

**预期结果（参考答案表）**：每字节小段内 bit0=EOFE、bit1=SOF。

| 拍 | tLast(EOF) | 首字节 TUSER {bit1 SOF, bit0 EOFE} | 末有效字节 TUSER {bit1 SOF, bit0 EOFE} | 说明 |
|----|------------|------------------------------------|----------------------------------------|------|
| word0 | 0 | **SOF=1**, EOFE=0 | SOF=0, EOFE=0 | 帧起始：SOF 只在首字节置位 |
| word1 | 0 | SOF=0, EOFE=0 | SOF=0, EOFE=0 | 中间拍：侧带全 0 |
| word2 | 1 | SOF=0, EOFE=0 | SOF=0, **EOFE=1** | 帧结束：EOF=tLast=1，EOFE 只在末字节置位 |

> 说明：在 `TUSER_FIRST_LAST_C` 模式下，物理 `tUser` 向量里「首字节副本」位于字节索引 0 处、「末字节副本」位于由 `tKeep` 决定的末有效字节索引处（见 4.1.3 的位号公式）。对 4 字节满宽拍，末有效字节索引 = 3。

**需要观察的现象**：SOF 在 word0 的首字节、且仅此一处为 1；EOFE 在 word2 的末字节、且仅此一处为 1；中间拍没有任何侧带被置位。这正是 SSI 「首字节存 SOF、末字节存 EOFE」的可视化结果。

> 待本地验证：上表为依据源码推导的预期布局；若在仿真中用 `ssiGetUserSof/ssiGetUserEofe` 逐拍读回，应与此表一致。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `ssiAxiStreamConfig` 的 `tDestBits` 改成 3，一条 SSI 流最多能有多少个 VC？
**答案**：\(2^{3} = 8\) 个 VC。`TDEST` 位数直接决定 VC 数量，默认 4 位即 16 个 VC。

**练习 2**：为什么 SSI 把 `TSTRB_EN_C` 硬编码为 `false`、不接受用户在 `ssiAxiStreamConfig` 里覆盖？
**答案**：SSI 协议只用 `TKEEP` 表达「末拍哪些字节有效」，不需要 `TSTRB`（区分「无效字节」是空隙还是无效数据）。固定关闭 `TSTRB` 能让所有 SSI 模块的侧带语义保持一致，避免个别模块开启后破坏帧边界约定。

---

### 4.3 帧边界 FIFO：SsiFifo 与入/出帧过滤器

#### 4.3.1 概念说明

直接用 `AxiStreamFifoV2` 缓冲 SSI 流会有问题：FIFO 只负责搬数据，**它不检查帧是否「合法成帧」**。如果上游送来一个没有 SOF 的残帧、或者在帧中间突然改了 `TDEST`，这些畸形数据会原样漏到下游，污染后续协议层。

`SsiFifo` 的职责就是：**在 AXI-Stream FIFO 的前后各加一道「SSI 帧过滤器」**，保证只有合法成帧的数据出现在主端口。它的结构是经典三明治：

```text
                          sAxisClk 域                                 (可选跨域)        mAxisClk 域
  sAxisMaster ─>[ SsiIbFrameFilter ]─> rx ─>[ AxiStreamFifoV2 ]─> tx ─>[ SsiObFrameFilter ]─> ob ─>(sync/async)─> mAxisMaster
  （入向过滤）                      （缓冲 + 可选帧级门控）            （出向过滤：给畸形帧打 EOFE / 丢弃）
```

两道过滤器的分工：

- **入向过滤器 `SsiIbFrameFilter`**：在数据进 FIFO 前做第一道清理（配合 `SLAVE_READY_EN_G` 做流控，并统计 `sAxisDropWord/DropFrame`）。
- **出向过滤器 `SsiObFrameFilter`**：在数据出 FIFO 后做「帧整形」——这是本节重点。它会把三类畸形情况统一处理成「带 EOFE 的提前 EOF」或「整帧丢弃」。

#### 4.3.2 核心流程：出向过滤器的状态机

`SsiObFrameFilter` 用三状态机保证帧边界干净（[SsiObFrameFilter.vhd:55-58](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiObFrameFilter.vhd#L55-L58) 定义 `IDLE_S / BLOWOFF_S / MOVE_S`）：

```text
IDLE_S (等 SOF):
  来了一拍且带 SOF  -> 正常：开始搬运 -> MOVE_S（若非 EOF）
  来了一拍但无 SOF  -> 残帧：丢弃整帧 -> BLOWOFF_S（吹掉到 EOF 为止）
  (VALID_THOLD_G=0 时，若 FIFO 缓存的末拍带 EOFE，也按残帧丢弃)

MOVE_S (正常搬运帧体):
  收到 EOF(tLast=1)               -> 正常结束 -> IDLE_S
  帧中途又出现 SOF (重复 SOF)     -> 畸形：立即把当前拍改成 EOF + EOFE=1 -> IDLE_S
  帧中途 TDEST 改变 (交错 VC)      -> 畸形：立即把当前拍改成 EOF + EOFE=1 -> IDLE_S

BLOWOFF_S (吹掉残帧):
  持续丢弃直到遇到 EOF -> IDLE_S
```

关键点：出向过滤器**不悄悄改数据**，而是把「重复 SOF」「帧中途换 VC」这两种最危险的畸形（会破坏下游 VC 路由与帧同步）**强行截断成一个带 EOFE 的 EOF**，让下游能明确知道「这帧坏了」。文件头注释把这三条规则列得很清楚（[SsiObFrameFilter.vhd:6-10](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiObFrameFilter.vhd#L6-L10)）。

#### 4.3.3 源码精读

**SsiFifo 的三件套实例化**。[SsiFifo.vhd:139-159](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiFifo.vhd#L139-L159) 是入向过滤器，[SsiFifo.vhd:164-204](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiFifo.vhd#L164-L204) 是中间的 `AxiStreamFifoV2`（注意它的 `mTLastTUser` 输出接到出向过滤器），[SsiFifo.vhd:291-311](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiFifo.vhd#L291-L311) 是出向过滤器。三者共用 `SLAVE_AXI_CONFIG_G`。

**TUSER 宽度的硬性断言**。三个 SSI 模块都要求 TUSER 至少 2 位（即 SOF+EOFE 两个比特），[SsiFifo.vhd:130-134](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiFifo.vhd#L130-L134) 在 elaboration 阶段拦截非法配置：

```vhdl
assert (SLAVE_AXI_CONFIG_G.TUSER_BITS_C >= 2)
   report "SsiFifo:  SLAVE_AXI_CONFIG_G.TUSER_BITS_C must be >= 2" severity failure;
```

这就是为什么应该用 `ssiAxiStreamConfig` 而不是手填配置——它能保证 `TUSER_BITS_C` 至少为 2。

**EOFE 如何穿过 FIFO**。当 `VALID_THOLD_G = 0`（帧级门控，见 u4-l2）时，FIFO 要等到一整帧收齐才输出，此时末拍的 TUSER 会被单独存进一个「tLast 旁路 FIFO」并以 `mTLastTUser` 引出（[SsiFifo.vhd:204](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiFifo.vhd#L204)）。出向过滤器从这个 8 位向量里取回 EOFE（[SsiObFrameFilter.vhd:112-118](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiObFrameFilter.vhd#L112-L118)）：

```vhdl
if (VALID_THOLD_G = 0) then
   v.eofe := sTLastTUser(SSI_EOFE_C);   -- 从末拍 TUSER 旁路取回 EOFE
else
   v.eofe := '0';
end if;
```

**畸形帧的 EOFE 打标**。出向过滤器在 `MOVE_S` 状态检测到「重复 SOF」或「帧中途 `TDEST` 变化」时，把当前拍改造成错误 EOF（[SsiObFrameFilter.vhd:191-210](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiObFrameFilter.vhd#L191-L210)）：

```vhdl
-- 检测 SSI 成帧错误（重复 SOF 或交错帧）
if (v.sof = '1') or (r.tDest /= sAxisMaster.tDest) then
   v.master.tLast := '1';                              -- 强行截断成 EOF
   ssiSetUserEofe(AXIS_CONFIG_G, v.master, '1');        -- 打上 EOFE
   ssiSetUserSof (AXIS_CONFIG_G, v.master, '0');        -- 清掉 SOF
   ...
   v.state := IDLE_S;
end if;
```

注意这里**直接调用了 4.1 节讲的 `ssiSetUserEofe`/`ssiSetUserSof`**——侧带编码函数既是「写」的入口，也是过滤器「改写畸形帧」的工具，二者是同一套机制。

**VALID_THOLD_G ≠ 1 时的防死锁**。帧级门控（`VALID_THOLD_G` 为 0 或 >1）下，如果 FIFO 满了但下游不来读，整个 FIFO 会锁死。`SsiFifo` 用一个 `WAIT_S → MON_S` 小状态机做看门狗：检测到「FIFO 满 且 输出长期无效」并超时后，脉冲复位 FIFO 解锁（[SsiFifo.vhd:217-286](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiFifo.vhd#L217-L286)），并通过 `lockupRstEvent` 上报；而 `VALID_THOLD_G = 1`（逐拍输出，默认）则直接走 `fifoRst <= sAxisRst` 的简单路径（[SsiFifo.vhd:209-212](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiFifo.vhd#L209-L212)）。

**同步/异步两种输出跨域**。`SsiFifo` 末尾按 `GEN_SYNC_FIFO_G` 二选一：异步时再串一个浅 `AxiStreamFifoV2`（4 深分布式 RAM）跨时钟域（[SsiFifo.vhd:316-344](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiFifo.vhd#L316-L344)），同步时直接用 `AxiStreamGearbox` 做宽度整形（[SsiFifo.vhd:349-370](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiFifo.vhd#L349-L370)）。注意主 FIFO 内部恒为同步（`GEN_SYNC_FIFO_G => true`，[SsiFifo.vhd:175](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiFifo.vhd#L175)），跨域交给这个外部小 FIFO，分工清晰。

#### 4.3.4 代码实践

**实践目标**：通过阅读状态机，预测「重复 SOF」畸形帧在 `SsiFifo` 输出端的样子。

**操作步骤（源码阅读型）**：

1. 打开 [SsiObFrameFilter.vhd:121-214](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiObFrameFilter.vhd#L121-L214)，聚焦 `MOVE_S` 分支（第 175-212 行）。
2. 假设上游送来这样一帧（畸形）：`word0(SOF=1) → word1(SOF=1，重复 SOF) → word2(EOF=1)`。
3. 跟踪 `r.tDest`、`v.sof`、`ssiSetUserEofe` 的执行，画出过滤器输出端实际看到的帧。

**预期结果**：

- `word0`：正常通过，输出带 `SOF=1`，状态进入 `MOVE_S`，记录 `tDest`。
- `word1`：在 `MOVE_S` 中检测到 `v.sof=1`（重复 SOF）→ 立即把**这一拍**改造成 `tLast=1` + `EOFE=1` + `SOF=0`，状态回 `IDLE_S`。也就是说，原 3 拍帧被截断成一个「word0 + 错误结束的 word1」的 2 拍帧，且 word1 带 EOFE。
- `word2`：此时已在 `IDLE_S`，又遇到一个没有 SOF 的拍 → 被当成残帧丢弃（`BLOWOFF_S`），直到它自己的 `tLast=1` 结束。

**需要观察的现象**：畸形输入被过滤器「规整」成下游可见的合法帧序列——任何破坏「一帧只有一个 SOF、`TDEST` 中途不变」的数据，都会被强行截断成带 `EOFE` 的 EOF。下游只需检查 EOFE 即可知道发生了成帧错误。

> 待本地验证：以上为依据状态机源码的推演；可在 cocotb 里构造该激励，断言输出端 `word1` 的 EOFE=1。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `SsiFifo` 在 `VALID_THOLD_G = 1` 时不死锁，而在 `VALID_THOLD_G = 0` 时需要专门的防死锁状态机？
**答案**：`VALID_THOLD_G = 1` 是逐拍输出，只要下游 `tReady` 正常，FIFO 不会长期满；而 `VALID_THOLD_G = 0` 是帧级门控，必须收齐整帧才输出，若下游不来读、FIFO 满了就会卡死，所以需要看门狗检测「满且长期无输出」并复位 FIFO 解锁。

**练习 2**：`SsiObFrameFilter` 检测到「帧中途 `TDEST` 变化」时为什么不直接放行，而要截断成 EOFE？
**答案**：因为 SSI 不支持交错 `TDEST`（见 4.2.1）。帧中途换 VC 会让下游的按 `TDEST` 路由逻辑错乱（一帧被误判成属于两个 VC），所以必须立即截断、用 EOFE 告知下游丢弃，保证每帧 `TDEST` 唯一。

---

## 5. 综合实践

**任务**：把本讲三个模块串起来，设计一条最小的 SSI 数据通路，并预测每一拍的侧带。

1. **定义流配置**：用 `ssiAxiStreamConfig(dataBytes => 4)` 得到一条 32 位 SSI 流。确认它的 `TDEST_BITS_C = 4`（16 个 VC）、`TUSER_BITS_C = 2`、`TUSER_MODE_C = TUSER_FIRST_LAST_C`。
2. **构造一帧正常数据**：3 拍，VC=2（即 `TDEST = "0010"`），其中 word0 是 SOF、word2 是正常 EOF。
   - 写出每一拍的 `tValid / tLast / TDEST` 以及「首字节 TUSER」「末字节 TUSER」的 `{SOF,EOFE}` 取值。
3. **构造一帧错误数据**：让最后一拍 word2 的 EOFE=1（模拟上游用 `SSI_MASTER_FORCE_EOFE_C` 之外的途径报错）。
   - 用 `ssiSetUserEofe(cfg, m, '1')` 标注它会被写到哪个字节、哪一位。
4. **接入 SsiFifo**：把上述两帧依次送进 `SsiFifo`（`VALID_THOLD_G = 1`）。
   - 预测：正常帧原样通过；错误帧的 EOFE 会被保留到输出端（因为出向过滤器只在「重复 SOF / 换 VC」时才改写 EOFE，外部已置好的 EOFE 在 `VALID_THOLD_G = 1` 时透传）。
5. **验收点**：能说清「SOF 在首字节位 1、EOFE 在末字节位 0、`TDEST` 是 VC 号、`tLast` 是 EOF」这四件事分别由哪个源码函数/常量负责。

> 待本地验证：步骤 4 的「外部 EOFE 在 `VALID_THOLD_G = 1` 时透传」这一结论建议在 cocotb 中实测确认；本步骤给出的是依据 [SsiObFrameFilter.vhd:112-118](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/protocols/ssi/rtl/SsiObFrameFilter.vhd#L112-L118)（`VALID_THOLD_G /= 0` 时 `v.eofe := '0'`，即不主动干预）的推演。

## 6. 本讲小结

- **SSI = AXI-Stream + 帧语义**：在 `TUSER` 里新增 `SOF`（位 1）与 `EOFE`（位 0）两个比特，`EOF` 直接复用 `tLast`，不新增端口。
- **首末字节布局**：`SOF` 只写在帧首拍的首字节（`bytePos=0`），`EOFE` 只写在末拍的末有效字节（`bytePos=-1`，由 `tKeep` 解析），对应 `TUSER_FIRST_LAST_C` 模式；位号公式为 `TUSER_BITS_C × pos + bitPos`。
- **TDEST 即虚拟通道**：SSI 固定 `TDEST` 为 4 位，一条流最多 16 个 VC；**不支持交错 `TDEST`**——一帧期间 `TDEST` 必须恒定。
- **`ssiAxiStreamConfig`** 一次性填好 SSI 全部约定（含 `TSTRB_EN_C=false`），并用 `SSI_CONFIG_INIT_C` 给出 16 字节默认配置。
- **SsiFifo = 入过滤器 + AxiStreamFifoV2 + 出过滤器**：出向 `SsiObFrameFilter` 把「重复 SOF」「帧中途换 VC」等畸形帧强行截断成带 `EOFE` 的 EOF，`VALID_THOLD_G ≠ 1` 时还自带防死锁看门狗。
- **侧带函数是一切的基础**：`ssiGetUserSof/ssiGetUserEofe/ssiSetUserSof/ssiSetUserEofe` 既是用户读写侧带的入口，也是 `SsiObFrameFilter` 改写畸形帧的工具，二者共用同一套机制。

## 7. 下一步学习建议

- **[u5-l2 SSI 测试码型与帧过滤](u5-l2-ssi-prbs-filters.md)**：用 `SsiPrbsTx/Rx` 收发带 SOF/EOF 的 PRBS 帧，亲手制造一帧错误并观察 EOFE；`SsiFrameLimiter` 会展示如何按帧长限流。
- **回头巩固 u4-l1 / u4-l2**：如果对 `AxiStreamConfigType.TUSER_MODE_C` 的四种模式、或 `AxiStreamFifoV2` 的 `VALID_THOLD_G` 帧级门控还不熟，建议先复习，因为本讲的「首末字节布局」和「防死锁」都建立在其上。
- **向协议层延伸**：学完本讲后，[u5-l3 SRP](u5-l3-srp-register-protocol.md) 会展示 SRPv3 如何把寄存器事务封装成 SSI 帧、用 VC 承载请求/响应；[u7-l1 PGP](u7-l1-pgp-family-overview.md) 会展示 PGP 如何用 SSI 的 `TDEST` 做虚拟通道路由——这两篇都会重度依赖本讲建立的「VC = TDEST」「SOF/EOFE 侧带」概念。
