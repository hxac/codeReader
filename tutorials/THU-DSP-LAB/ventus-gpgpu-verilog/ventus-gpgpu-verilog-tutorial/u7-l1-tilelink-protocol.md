# TileLink 协议基础与操作码

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 Ventus GPGPU 中 **TileLink 风格接口**的 A/D 两个通道各自传输什么、如何握手；
- 列举项目里真实出现的 **TL 操作码**（GET / PUTFULL / PUTPART / FLUSH / ARITH / LOGIC，以及 L2 侧的 ACQUIRE / PROBE / GRANT 等）及其语义；
- 解释 `source` 字段为什么是一张**分级编码的「回信地址」**，并能算出 `A_SOURCE / D_SOURCE / CLUSTER_SOURCE / SOURCE_BITS` 的位宽；
- 理解 `mask` 与 `size` 字段如何描述一次突发的字节使能与大小。

本讲是单元 7（片上互联与 L2）的起点：后续 L2 Cache 架构（u7-l2）、cluster 到 L2 的互联（u7-l3）、AXI 适配器（u7-l4）都建立在本讲建立的「通道—操作码—source 路由」这套词汇之上。

## 2. 前置知识

### 2.1 什么是 TileLink

TileLink 是 RISC-V 生态常用的一套**片上总线协议**（由 SiFive 提出），用来连接 CPU 核、缓存、内存控制器等。它的核心思想是：

- 把一次事务拆成**请求（request）**与**响应（response）**两个方向；
- 用若干**通道（channel）**分别承载它们，每个通道都是一组带 `valid/ready` 握手的信号；
- 请求里携带一个 **`source` 标识**，响应原样带回，使响应能被**路由回**发起者。

标准 TileLink 有 A/B/C/D/E 五个通道。Ventus 出于简化，只使用了其中的 **A 通道（请求）** 与 **D 通道（响应）**——也就是「TileLink-UL/C 的精简子集」。这一点直接体现在顶层接口只有 `out_a_*` 与 `out_d_*` 两组信号。

> 名词解释：
> - **事务（transaction）**：一次完整的读或写，由请求 + 响应组成。
> - **主（master）/ 从（slave）**：发起请求的一方叫主，响应的一方叫从。L1 相对 L2 是主，L2 相对外部内存是主。
> - **beat / 突发（burst）**：一次事务的数据可能拆成多拍传送，每拍叫一个 beat。

### 2.2 本讲用到的规模参数（默认值）

本讲大量出现位宽推导，先固定一组**默认配置**（来自 `define.v` 顶部），后面所有数字都按它算：

| 参数 | 默认值 | 含义 |
|---|---|---|
| `NUM_CLUSTER` | 1 | cluster（簇）数 |
| `NUM_SM` | 2 | SM 核总数 |
| `NUM_SM_IN_CLUSTER` | 2 | 每 cluster 内 SM 数 |
| `NUM_CACHE_IN_SM` | 2 | 每 SM 内 L1 cache 数（icache + dcache） |
| `NUM_L2CACHE` | 1 | L2 cache 数 |
| `DCACHE_MSHRENTRY` | 4 | D-cache MSHR 主表项数 |
| `DCACHE_NSETS` | 32 | D-cache 组数 |
| `DCACHE_BLOCKWORDS` | 2 | 每个缓存块的「字」数 |

派生位宽（下文逐一推导）：

\[
\text{DCACHE\_ENTRY\_DEPTH}=\$\text{clog2}(4)=2,\quad
\text{DCACHE\_SETIDXBITS}=\$\text{clog2}(32)=5
\]

\[
\text{A\_SOURCE}=3+2+5=10,\quad
\text{D\_SOURCE}=1+10=11,\quad
\text{CLUSTER\_SOURCE}=11+1=12
\]

\[
\text{SOURCE\_BITS}=3+2+5+1+0+1=12
\]

（默认 `NUM_CLUSTER=1` 时 `$\text{clog2}(1)=0$`，所以 `CLUSTER_SOURCE` 与 `SOURCE_BITS` 恰好都等于 12。）

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [`src/define/define.v`](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v) | 全项目配置总开关。本讲关注其中三类宏：TL 操作码、source/通道位宽、L2 TL 操作码。 |
| [`src/gpgpu_top/GPGPU_top.v`](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v) | 顶层。声明 `out_a_*` / `out_d_*` 接口，并把 SM↔cluster↔L2 各级 `source` 连线串起来。 |
| [`src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v`](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v) | L1 D-cache。本讲用它说明 source 字段**最内层**（tag+entry+set）是如何拼出来的。 |
| [`src/gpgpu_top/sm2cluster_arb.v`](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm2cluster_arb.v) | SM→cluster 仲裁器。本讲用它说明 source 字段在**向外传递时如何被逐层「贴标签」**，以及响应如何据标签路由回去。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**A/D 双通道与握手**、**TL 操作码与 param**、**source 分级编码**、**mask 与 size**。

---

### 4.1 TileLink A 通道与 D 通道

#### 4.1.1 概念说明

Ventus 的对外接口只用两条通道：

- **A 通道（请求通道，out_a）**：主设备 → 从设备，发起一次读或写。携带操作码、地址、数据（写时）、掩码、大小、`source` 标识。
- **D 通道（响应通道，out_d）**：从设备 → 主设备，回送读数据或写完成应答。携带响应操作码、数据、`source` 标识。

注意方向：站在 GPGPU 顶层看，`out_a_*_o` 是**输出**（GPU 向外发请求），`out_d_*_i` 是**输入**（外部回响应）。这正是「L2 相对外部内存是主」的体现。

#### 4.1.2 核心流程

两条通道都使用 **valid/ready 握手**：只有当 `valid` 与 `ready` 同拍都为高时，这一拍的数据才被成功传送；否则保持不变。

```
主(A通道) ──valid+data──▶ 从
从        ──ready────────▶ 主
           (同拍 valid&ready => 一次传送完成)

从(D通道) ──valid+data──▶ 主
主        ──ready────────▶ 从
```

一条 A 请求的「身份」全靠 **`source` 字段**携带；D 响应把同一个 `source` 原样带回，主设备据此认领自己的请求。这就是 TileLink 能在单一接口上**并发处理多个在途事务**的关键——不同请求用不同 `source` 值区分。

#### 4.1.3 源码精读

顶层接口在 `GPGPU_top.v` 中用 `ifdef NO_CACHE` 分两种壳；带 L2 的正常模式（`gpgpu_axi_top` 只支持这种）暴露的是一组 TileLink 风格信号：

[GPGPU_top.v:78-94](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L78-L94) —— A/D 两组通道的字段清单（节选）：

```verilog
  //AXI  （实为 TileLink 风格的 A/D 通道，后续由 axi4_adapter 转 AXI）
  output  [`NUM_L2CACHE-1:0]   out_a_valid_o   ,
  input   [`NUM_L2CACHE-1:0]   out_a_ready_i   ,
  output  [`NUM_L2CACHE*`OP_BITS-1:0]      out_a_opcode_o   ,
  output  [`NUM_L2CACHE*`SIZE_BITS-1:0]    out_a_size_o     ,
  output  [`NUM_L2CACHE*`SOURCE_BITS-1:0]  out_a_source_o   ,
  output  [`NUM_L2CACHE*`ADDRESS_BITS-1:0] out_a_address_o  ,
  output  [`NUM_L2CACHE*`MASK_BITS-1:0]    out_a_mask_o     ,
  output  [`NUM_L2CACHE*`DATA_BITS-1:0]    out_a_data_o     ,
  output  [`NUM_L2CACHE*3-1:0]             out_a_param_o    ,

  input   [`NUM_L2CACHE-1:0]   out_d_valid_i   ,
  output  [`NUM_L2CACHE-1:0]   out_d_ready_o   ,
  input   [`NUM_L2CACHE*`OP_BITS-1:0]      out_d_opcode_i   ,
  input   [`NUM_L2CACHE*`SIZE_BITS-1:0]    out_d_size_i     ,
  input   [`NUM_L2CACHE*`SOURCE_BITS-1:0]  out_d_source_i   ,
  input   [`NUM_L2CACHE*`DATA_BITS-1:0]    out_d_data_i     ,
  input   [`NUM_L2CACHE*3-1:0]             out_d_param_i
```

字段含义一览：

| 字段 | 位宽宏 | 方向 | 含义 |
|---|---|---|---|
| `valid/ready` | 1 | 双向 | 握手 |
| `opcode` | `OP_BITS=3` | A:出 / D:入 | 操作码，见 4.2 |
| `param` | 3 | A:出 / D:入 | 操作码的子参数，见 4.2 |
| `size` | `SIZE_BITS` | A:出 / D:入 | 一次 beat 的字节数 = \(2^{\text{size}}\) |
| `address` | `ADDRESS_BITS=32` | A:出 | 物理地址 |
| `data` | `DATA_BITS` | A:出 / D:入 | 数据负载 |
| `mask` | `MASK_BITS` | A:出 | 字节使能，见 4.4 |
| `source` | `SOURCE_BITS` | A:出 / D:入 | 「回信地址」，见 4.3 |

注意 A 通道有 `address/mask/data`，而 **D 通道没有 `address/mask`**——响应不需要再带地址，路由全靠 `source`。这是 A/D 非对称的体现。

#### 4.1.4 代码实践

**实践目标**：从顶层接口认清 A/D 两通道的字段与方向。

**操作步骤**：
1. 打开 [`GPGPU_top.v:78-94`](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L78-L94)。
2. 用笔把每个信号标成「A 通道输出 / A 通道输入 / D 通道输出 / D 通道输入」四类之一（提示：`out_a_*_o` 的 `_o` 表示输出，`out_d_*_i` 的 `_i` 表示输入）。
3. 数一数：A 通道有哪些字段是 D 通道没有的？

**需要观察的现象 / 预期结果**：A 通道独有 `address`、`mask`；D 通道独有……其实没有独有字段，它是 A 的子集 + 响应语义。A 的 `opcode` 取 `GET` 等请求码，D 的 `opcode` 取 `ACCESSACKDATA` 等响应码（详见 4.2）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 D 通道不需要 `mask` 字段？
> **答**：`mask` 是写给从设备的「哪些字节有效」，只在请求（写）时需要；响应回送的是整块读数据，主设备按请求时的 `mask` 自行取舍，故 D 通道无需再带。

**练习 2**：若 `out_a_valid_o=1` 但 `out_a_ready_i=0`，请求算发送成功了吗？
> **答**：不算。valid/ready 必须同拍为高才算完成一拍传送；此时主设备应保持 `valid` 与数据不变，等到 `ready` 拉高那一拍才成功。

---

### 4.2 TL 操作码（opcode）与 param

#### 4.2.1 概念说明

`opcode` 是 3 位（`OP_BITS=3`），区分事务类型。Ventus 里有两套操作码命名空间：

- **L1 → L2 方向**用的 `TLAOP_*`（在 `define.v` 的 `l1dcache_define` 段），值较小，是 D-cache 自己发出的请求码；
- **L2 内部 / 标准 TileLink 方向**用的 `PUTFULLDATA / GET / ACQUIREBLOCK / GRANTDATA …`（在 `tilelink interface opcode` 段），是标准 TileLink 的全集，源自 SiFive block-inclusivecache。

`param` 也是 3 位，是 `opcode` 的**子参数**，例如同样是 FLUSH，`param` 区分「刷脏回写」还是「直接作废」。

#### 4.2.2 核心流程

请求码与响应码是**配对**的：发某个 A 操作码，就期望收到对应的 D 操作码。下表是标准 TileLink 的对应关系（括号为 Ventus 实际用到的子集）：

| A 通道请求 opcode | → 期望的 D 通道响应 opcode | 用途 |
|---|---|---|
| `PUTFULLDATA`(0) / `PUTPARTIALDATA`(1) | `ACCESSACK`(0) | 写（全量 / 部分），回一个完成应答 |
| `ARITHMETICDATA`(2) / `LOGICALDATA`(3) | `ACCESSACKDATA`(1) | 原子读改写 |
| `GET`(4) | `ACCESSACKDATA`(1) | 读，回带数据 |
| `HINT`(5) | `HINTACK`(2) | 提示（如 flush） |
| `ACQUIREBLOCK`(6) / `ACQUIREPERM`(7) | `GRANT`/`GRANTDATA`(4/5) | 获取一块缓存（含一致性权限） |

D-cache 实际向外发的只有最基础的几种：**读缺失发 `GET`，写缺失发 `PUTPARTIALDATA`，冲刷发 `FLUSH`**。

#### 4.2.3 源码精读

**L1 D-cache 使用的请求码**（`TLAOP_*`），见 [define.v:271-285](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L271-L285)：

```verilog
`define TLAOP_GET          3'd4   // 读
`define TLAOP_PUTFULL      3'd0   // 全量写
`define TLAOP_PUTPART      3'd1   // 部分写（带 mask）
`define TLAOP_FLUSH        3'd5   // 冲刷/无效
`define TLAOP_ARITH        3'd2   // 原子算术
`define TLAOP_LOGIC        3'd3   // 原子逻辑
```

冲刷的 `param` 子参数见 [define.v:279-281](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L279-L281)：`TLAPARAM_FLUSH=0`（刷脏回写）、`TLAPARAM_INV=1`（直接无效，不回写）。

**D-cache 真正赋值的地方**——读缺失发 GET、写缺失发 PUTPART，见 [l1_dcache.v:777-787](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L777-L787)：

```verilog
assign write_miss_req_a_opcode = `TLAOP_PUTPART;  // 写缺失 => PutPartialData
assign write_miss_req_a_param  = 3'b000;          // 普通写
...
assign read_miss_req_a_opcode  = `TLAOP_GET;       // 读缺失 => Get
assign read_miss_req_a_param   = 3'b000;           // 普通读
```

**标准 TileLink 全集操作码**（L2 侧使用），见 [define.v:376-414](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L376-L414)。节选请求码与其期望响应：

```verilog
`define PUTFULLDATA    3'd0  // => AccessAck
`define PUTPARTIALDATA 3'd1  // => AccessAck
`define GET            3'd4  // => AccessAckData
`define ACQUIREBLOCK   3'd6  // => Grant[Data]
`define ACQUIREPERM    3'd7  // => Grant[Data]
// —— 响应码 ——
`define ACCESSACK      3'd0
`define ACCESSACKDATA  3'd1
`define GRANT          3'd4  // => GrantAck
`define GRANTDATA      3'd5  // => GrantAck
```

> 注意：项目里 A 请求码与某些 D 响应码**数值会复用**（例如 `GET=4` 与 `GRANT=4` 都是 `3'd4`），因为它们出现在不同通道、语义独立，靠「A 还是 D 通道」来区分。读代码时要看清楚信号属于 `a_opcode` 还是 `d_opcode`。

#### 4.2.4 代码实践

**实践目标**：把 D-cache 的三种典型请求与它们的 opcode/param 对应起来。

**操作步骤**：
1. 在 [l1_dcache.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v) 中搜索 `TLAOP_` 与 `a_opcode`，找出「读缺失、写缺失、脏替换回写、invalidate/flush」分别赋了哪个操作码。
2. 对照 [define.v:271-285](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L271-L281)，填出下表：

| 场景 | opcode 宏 | 数值 | param 含义 |
|---|---|---|---|
| 读缺失 | `TLAOP_GET` | 4 | 0=普通读 |
| 写缺失 | ? | ? | ? |
| 脏块替换回写 | ? | ? | ? |

**预期结果**：读缺失 `GET`、写缺失 `PUTPART`、脏替换回写用 `PUTFULL`（全量写回整块）。`param` 在普通读写时为 `3'b000`。

#### 4.2.5 小练习与答案

**练习 1**：`GET` 与 `PUTPARTIALDATA` 各自期望收到什么 D 通道响应？
> **答**：`GET` 期望 `ACCESSACKDATA`（带读数据）；`PUTPARTIALDATA` 期望 `ACCESSACK`（仅一个完成应答，不带数据）。

**练习 2**：`TLAPARAM_FLUSH` 与 `TLAPARAM_INV` 的区别是什么？
> **答**：`FLUSH`(0) 表示把脏数据先写回下级再让该块失效；`INV`(1) 表示直接作废（不回写）。workgroup 结束时 dcache 注入的 invalidate 请求用的是 `opcode=3/param=0`（参见 u6-l3）。

---

### 4.3 source 字段：分级编码的「回信地址」

> 这是本讲最重要的模块，也是本讲的代码实践任务所在。

#### 4.3.1 概念说明

当 L1 D-cache 向 L2 发出一个 `GET`，L2 处理完要把数据送回来——但 L2 面对的是**多个 SM、每个 SM 两个 cache、每个 cache 多个 MSHR 表项**同时在途的请求。它怎么知道这次响应该还给谁？

答案就是 **`source` 字段**：它是一张**逐层累加的「回信地址」**。请求每经过一级互联，就在 source 的高位**贴上自己这一级的编号**；响应回来时，每一级**剥掉对应的高位**，据此路由给下一级，直到回到发起的那个 MSHR 表项。

可以类比寄信：你在信封上写「3 栋 2 单元 501 室」，邮递员先看「3 栋」送到对应楼，再看「2 单元」，最后看「501 室」。source 就是这串地址，只不过每一级是在发送时**逐层补写**上去的。

#### 4.3.2 核心流程

source 字段从**最内层（L1 dcache）开始构造**，向外逐层加宽。各层位宽宏定义如下（见 [define.v:103-115](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L103-L115)）：

```verilog
`define DCACHE_ENTRY_DEPTH  $clog2(`DCACHE_MSHRENTRY)        // MSHR 主表项号位宽
`define NUM_CACHE_DEPTH     $clog2(`NUM_CACHE_IN_SM)         // cache 号位宽(icache/dcache)
`define NUM_CLUSTER_DEPTH   $clog2(`NUM_SM_IN_CLUSTER)       // SM 号位宽
`define D_SOURCE       (`NUM_CACHE_DEPTH+3+`DCACHE_ENTRY_DEPTH+`DCACHE_SETIDXBITS)
`define A_SOURCE       (3+`DCACHE_ENTRY_DEPTH+`DCACHE_SETIDXBITS)
`define CLUSTER_SOURCE (`D_SOURCE + `NUM_CLUSTER_DEPTH)
```

逐层结构（高位 → 低位）：

\[
\underbrace{\text{cluster\_id}}_{\text{clog2(NUM\_CLUSTER)}}
\; \underbrace{\text{sm\_in\_cluster}}_{\text{NUM\_CLUSTER\_DEPTH}}
\; \underbrace{\text{cache\_id}}_{\text{NUM\_CACHE\_DEPTH}}
\; \underbrace{\text{tag[3]}}_{3}
\; \underbrace{\text{mshr\_entry}}_{\text{DCACHE\_ENTRY\_DEPTH}}
\; \underbrace{\text{set\_idx}}_{\text{DCACHE\_SETIDXBITS}}
\]

各宏对应的覆盖范围：

| 宏 | 覆盖的位段 | 默认位宽 | 在哪一层用 |
|---|---|---|---|
| `A_SOURCE` | tag + entry + set | 3+2+5=10 | L1 dcache 内部（最内层） |
| `D_SOURCE` | cache_id + tag + entry + set | 1+10=11 | SM 对外（l1cache_arb 贴 cache_id） |
| `CLUSTER_SOURCE` | sm_id + cache_id + tag + entry + set | 1+11=12 | cluster 对外（sm2cluster_arb 贴 sm_id） |
| `SOURCE_BITS` | cluster_id + sm_id + cache_id + tag + entry + set | 12 | L2 入口（全地址） |

`SOURCE_BITS` 的精确定义见 [define.v:333](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L333)：

```verilog
`define SOURCE_BITS (3 + $clog2(`DCACHE_MSHRENTRY) + $clog2(`DCACHE_NSETS)
                       + $clog2(`L2CACHE_NUM_SM_IN_CLUSTER) + $clog2(`L2CACHE_NUM_CLUSTER) + 1)
```

逐项对应：`3`=tag、`clog2(MSHRENTRY)`=entry、`clog2(NSETS)`=set、`clog2(SM_IN_CLUSTER)`=sm_id、`clog2(CLUSTER)`=cluster_id、末尾 `+1`=cache_id（因 `NUM_CACHE_IN_SM=2` 故 clog2=1）。

#### 4.3.3 源码精读

**(1) 最内层：L1 dcache 拼出 A_SOURCE。**

读缺失时，dcache 把「3 位 tag + MSHR 表项号 + set 号」拼成 source，见 [l1_dcache.v:786](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L786)：

```verilog
// 读缺失：tag=3'b001, MSHR entry 号, set 号
assign read_miss_req_a_source = {3'b001, mshr_probe_out_a_source, core_req_setidx_st1};
```

其中 `mshr_probe_out_a_source` 是 MSHR 给出的**主表项号**（`$clog2(DCACHE_MSHRENTRY)` 位），`core_req_setidx_st1` 是缺失地址的 **set 索引**。3 位 tag `3'b001` 标记「这是读缺失」（写缺失/WSHR 则是 `3'b000`，见 [l1_dcache.v:1853](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L1853)）：

```verilog
// WSHR 写缺失：tag=3'b000, wshr 表项号, set 号
mem_req_st3_a_source <= {3'b000, wshr_pushedIdx, mem_req_setidx_st2};
```

> 写缺失的 source 为何要带 wshr 表项号？因为写缓冲（WSHR）也要靠响应认领自己的在途写回。读/写缺失的 tag 不同（001 vs 000），便于响应回来时区分该走 MSHR 还是 WSHR。

**(2) 第二层：l1cache_arb 贴 cache_id（A_SOURCE → D_SOURCE）。**

dcache 出口的 source 是 `A_SOURCE` 宽（10 位），而 SM 对外的 `mem_req_a_source` 是 `D_SOURCE` 宽（11 位）。多出来的最高 1 位是 **cache_id**（icache=1 / dcache=0），由 `l1cache_arb` 在请求时拼进高位、响应时按它解复用回对应 cache（细节见 u6-l3）。这一层使 L2 能区分响应是给 icache 还是 dcache。

**(3) 第三层：sm2cluster_arb 贴 sm_id（D_SOURCE → CLUSTER_SOURCE）。**

[sm2cluster_arb.v:90-91](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm2cluster_arb.v#L90-L91) 在请求出 cluster 时把「被选中的 SM 号」贴到 source 最高位：

```verilog
assign memReqBuf_in_source = (`NUM_SM_IN_CLUSTER==1) ? /*直通*/ ... :
       {in_valid_grant_bin, mem_req_vec_in_a_source_i[`D_SOURCE*(in_valid_grant_bin+1)-1-:`D_SOURCE]};
//                       ^^^^^^^^^^^^^^^^^^ 新增的 sm_id（NUM_CLUSTER_DEPTH 位）贴在高位
```

`in_valid_grant_bin` 就是本轮仲裁选中的 SM 在 cluster 内的编号。

**(4) 响应路由：逐层剥标签。**

响应沿原路返回，每一级用 source 的高位做路由、剥掉后送给下一级。[sm2cluster_arb.v:80-83](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm2cluster_arb.v#L80-L83) 用 source 的最高 `NUM_CLUSTER_DEPTH` 位选目标 SM，并把低 `D_SOURCE` 位送过去：

```verilog
// 响应：用 source 高位选 SM，低 D_SOURCE 位作为该 SM 收到的 source
assign mem_rsp_vec_out_d_source_o[`D_SOURCE*(i+1)-1-:`D_SOURCE] =
       (`NUM_SM_IN_CLUSTER==1) ? mem_rsp_in_source_i : mem_rsp_in_source_i[`D_SOURCE-1:0];
assign mem_rsp_vec_out_valid_o[i] =
       (`NUM_SM_IN_CLUSTER==1) ? mem_rsp_in_valid_i
       : (mem_rsp_in_source_i[`CLUSTER_SOURCE-1-:`NUM_CLUSTER_DEPTH]==i) && mem_rsp_in_valid_i;
```

即：响应 `source` 的最高位（sm_id）等于几，就送给第几个 SM；同时把这一位剥掉，使该 SM 收到的 source 重新变回 `D_SOURCE` 宽。l1cache_arb 再剥掉 cache_id，dcache 最终拿到 `A_SOURCE`，用其中的 entry 号唤醒对应的 MSHR 表项。

**整条往返链路**：

```
dcache 拼出 {tag, entry, set}            [A_SOURCE]
   │ +cache_id (l1cache_arb)              [D_SOURCE]
   │ +sm_id    (sm2cluster_arb)           [CLUSTER_SOURCE]
   │ (+cluster_id 经 l2_distribute/arb)   [SOURCE_BITS]  ──▶ L2
   │
L2 响应原样带回 source ◀───────────────── [SOURCE_BITS]
   │ 用 cluster_id 选 cluster
   │ 用 sm_id 选 SM          (sm2cluster_arb 剥高位)
   │ 用 cache_id 选 icache/dcache (l1cache_arb 剥高位)
   └ 用 {tag,entry,set} 唤醒 MSHR/WSHR 表项   [A_SOURCE]
```

#### 4.3.4 代码实践（本讲主任务）

**实践目标**：把 `SOURCE_BITS / A_SOURCE / D_SOURCE / CLUSTER_SOURCE` 四个计算式落实成具体数值，并解释一个 L1 读请求如何把「SM 号 / cache 号 / set / entry」打包进 source 以供 L2 路由。

**操作步骤**：
1. 打开 [define.v:103-115](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L103-L115) 与 [define.v:333](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L333)。
2. 按默认配置（`NUM_SM_IN_CLUSTER=2`、`NUM_CACHE_IN_SM=2`、`DCACHE_MSHRENTRY=4`、`DCACHE_NSETS=32`、`NUM_CLUSTER=1`）填表：

| 宏 | 表达式 | 数值 |
|---|---|---|
| `DCACHE_ENTRY_DEPTH` | `clog2(4)` | 2 |
| `DCACHE_SETIDXBITS` | `clog2(32)` | 5 |
| `A_SOURCE` | `3+2+5` | ? |
| `NUM_CACHE_DEPTH` | `clog2(2)` | ? |
| `D_SOURCE` | `1+10` | ? |
| `NUM_CLUSTER_DEPTH` | `clog2(2)` | ? |
| `CLUSTER_SOURCE` | `11+1` | ? |
| `SOURCE_BITS` | `3+2+5+1+0+1` | ? |

3. 用一张位域图（从高位到低位）画出 `SOURCE_BITS` 的 12 位布局，标注每段是哪个 id。
4. 构造一个例子：SM#1 的 dcache，读缺失命中 MSHR entry=2、set=9。写出它**进入 L2 时** source 字段各段的取值（提示：cluster_id=0、sm_id=1、cache_id=0、tag=001、entry=2、set=9）。
5. 说明 L2 响应回来时，`sm2cluster_arb` 怎么凭 source 把它路由回 SM#1。

**需要观察的现象 / 预期结果**：
- `A_SOURCE=10, NUM_CACHE_DEPTH=1, D_SOURCE=11, NUM_CLUSTER_DEPTH=1, CLUSTER_SOURCE=12, SOURCE_BITS=12`。
- 位域（高→低）：`cluster_id[0] | sm_id[1] | cache_id[1] | tag[3] | entry[2] | set[5]`。
- 上述例子：`cluster_id=0, sm_id=1, cache_id=0, tag=001, entry=10, set=01001`。L2 响应到达 `sm2cluster_arb` 时，它取最高 `NUM_CLUSTER_DEPTH=1` 位（=1）选中 SM#1，把剩余低 11 位（D_SOURCE）送给 SM#1。

> 「待本地验证」：若你把 `NUM_SM` 改为 4（`NUM_SM_IN_CLUSTER` 随之变 4），`NUM_CLUSTER_DEPTH` 会从 1 变为 2，`CLUSTER_SOURCE`/`SOURCE_BITS` 也会相应变宽——可在仿真时打印 `source` 的位宽确认。

#### 4.3.5 小练习与答案

**练习 1**：为什么 source 要在每一级「贴标签」而不是一开始就写全？
> **答**：因为 L1 dcache 根本不知道自己在哪个 SM、哪个 cluster——这些是**系统拓扑信息**，只有互联的每一级知道。让每级在请求经过时补写自己的 id，使 L1 模块保持「位置无关」，可复用、可例化到任意位置。

**练习 2**：读缺失与写缺失的 source tag 为何不同（001 vs 000）？
> **答**：读缺失要等 L2 回数据去**唤醒 MSHR 表项**；写缺失的写回由 **WSHR** 跟踪。tag 不同使响应回来时能区分该把数据交给 MSHR 还是 WSHR。

**练习 3**：若 `NUM_CLUSTER` 从 1 增到 2，`SOURCE_BITS` 会变吗？
> **答**：会。`$clog2(NUM_CLUSTER)` 从 0 变为 1，`SOURCE_BITS` 增加 1 位（用于 cluster_id）。而 `CLUSTER_SOURCE` 不含 cluster_id 段，所以二者在多 cluster 时不再相等。

---

### 4.4 mask 与 size：字节使能与突发大小

#### 4.4.1 概念说明

- **mask**：字节使能（byte enable），每一位对应 data 里的一个字节，为 1 表示该字节有效。用于「部分写」或子字访问。
- **size**：一次 beat 传送的字节数，以 \(2^{\text{size}}\) 计。`size=3` 表示 8 字节。

#### 4.4.2 核心流程

`MASK_BITS` 由「每 beat 字节数 / 最小可写粒度」决定，见 [define.v:341](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L341)：

```verilog
`define MASK_BITS (`L2CACHE_BEATBYTES / `L2CACHE_WRITEBYTES)
```

默认 `L2CACHE_BEATBYTES = DCACHE_BLOCKWORDS*4 = 2*4 = 8`，`L2CACHE_WRITEBYTES = 1`，故：

\[
\text{MASK\_BITS} = 8/1 = 8 \text{（位）}
\]

即一个 beat 是 8 字节（64 位）数据，配 8 位 mask。`SIZE_BITS = $clog2(L2CACHE_BEATBYTES) = $clog2(8) = 3`（[define.v:343](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L343)），`size` 字段取值 3。

#### 4.4.3 源码精读

D-cache 读缺失时整块读，mask 全 1（[l1_dcache.v:789](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v#L789)）：

```verilog
assign read_miss_req_a_mask = {(`DCACHE_BLOCKWORDS*`BYTESOFWORD){1'b1}};  // = 8'hFF
```

`DATA_BITS` 见 [define.v:339](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L339)：`DATA_BITS = L2CACHE_BEATBYTES*8 = 64`，与 8 位 mask 一一对应。

#### 4.4.4 代码实践

**实践目标**：确认 mask 位宽与 data 位宽、size 取值的数值关系。

**操作步骤**：
1. 在 [define.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v) 中找到 `MASK_BITS`、`DATA_BITS`、`SIZE_BITS`、`L2CACHE_BEATBYTES`、`L2CACHE_WRITEBYTES` 的定义。
2. 验证：`DATA_BITS == MASK_BITS * 8`、`2^SIZE == L2CACHE_BEATBYTES`。
3. 在 [l1_dcache.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/l1_dcache.v) 中搜索 `a_mask`，看读缺失（全 1）与写缺失（部分有效）的 mask 取值差异。

**预期结果**：`DATA_BITS=64`、`MASK_BITS=8`、`2^3=8=L2CACHE_BEATBYTES`，三者自洽。

#### 4.4.5 小练习与答案

**练习 1**：若 `L2CACHE_WRITEBYTES` 改为 2，`MASK_BITS` 变成多少？
> **答**：`8/2=4`。最小写粒度变大（2 字节），mask 每 1 位管 2 字节，故位数减半。

**练习 2**：读缺失为何 mask 全置 1？
> **答**：读缺失要取回整个缓存块（8 字节全有效），不存在「部分字节」的概念。

---

## 5. 综合实践

**任务**：跟踪一次 **SM#1 dcache 读缺失**的 source 字段，从 L1 一路填到 L2 入口，再描述响应如何原路返回。把结果填进下表并画出位域图。

假设条件（默认配置）：缺失地址的 `set=9`、命中 MSHR `entry=2`；该 SM 在 cluster 内编号为 1；dcache 的 cache_id=0；cluster_id=0。

| 字段 | cluster_id | sm_id | cache_id | tag | entry | set |
|---|---|---|---|---|---|---|
| 位宽 | 0 | 1 | 1 | 3 | 2 | 5 |
| 取值 | 0 | 1 | 0 | 001 | 10 | 01001 |

要求：

1. 写出 **A_SOURCE / D_SOURCE / CLUSTER_SOURCE / SOURCE_BITS** 四个数值（答案：10 / 11 / 12 / 12）。
2. 把上表拼成一个 12 位的 source 值（从高位到低位）。
3. 描述响应返回时：`sm2cluster_arb` 取最高 1 位（=1）选中 SM#1；`l1cache_arb` 取下 1 位（=0）选中 dcache；dcache 用 entry=2 唤醒 MSHR 表项 2。
4. 进阶思考：如果把这条请求改写成**写缺失**（tag 应改为什么？entry 段应改成什么来源？）。

> 参考答案：写缺失的 tag=`000`，entry 段不再是 MSHR 表项号而是 **WSHR 表项号**（`wshr_pushedIdx`），因为写回由 WSHR 跟踪。

完成本任务后，你应当能向别人讲清：「L2 凭什么把响应送回正确的那个 MSHR 表项」——这正是 TileLink source 字段存在的全部意义。

## 6. 本讲小结

- Ventus 只用 TileLink 的 **A 通道（请求）与 D 通道（响应）** 两条通道，均靠 `valid/ready` 握手；A 独有 `address/mask`，D 不带地址。
- `opcode`（3 位）+ `param`（3 位）描述事务类型；L1 用 `TLAOP_GET/PUTPART/PUTFULL/FLUSH`，L2 用标准 TileLink 的 `GET/ACQUIRE/GRANT…`，请求码与响应码成对配应。
- **`source` 是分级编码的「回信地址」**：L1 dcache 先填 `{tag, entry, set}`（A_SOURCE），l1cache_arb 贴 `cache_id`（→D_SOURCE），sm2cluster_arb 贴 `sm_id`（→CLUSTER_SOURCE），最外层贴 `cluster_id`（→SOURCE_BITS）；响应沿原路逐层剥标签路由回去。
- 默认配置下 `A_SOURCE=10 / D_SOURCE=11 / CLUSTER_SOURCE=12 / SOURCE_BITS=12`，位域从高到低为 `cluster_id | sm_id | cache_id | tag | entry | set`。
- `mask` 是字节使能（默认 8 位），`size` 是 \(2^{\text{size}}\) 字节的 beat 大小（默认 size=3 即 8 字节），二者与 `DATA_BITS=64` 自洽。
- 注意陷阱：A 请求码与 D 响应码**数值可能复用**（如 `GET=4` 与 `GRANT=4`），读代码务必区分信号属于 A 还是 D 通道。

## 7. 下一步学习建议

- **u7-l2（L2 Cache Scheduler 架构）**：本讲只到「请求到达 L2 入口」。下一讲进入 Scheduler 内部，看 sinkA/sourceA/sourceD/sinkD 各通道如何消费这些 A/D 信号、directory 如何查目录、MSHR 如何处理 ACQUIRE/PROBE/GRANT。
- **u7-l3（cluster 到 L2 的互联）**：本讲的 source 路由是「点到点」叙述，下一讲把 `sm2cluster_arb / l2_distribute / cluster_to_l2_arb` 三个互联模块的拓扑完整画出来。
- **u7-l4（AXI4 适配器）**：本讲的 `out_a/out_d` 是 TileLink 风格，下一讲看 `axi4_adapter` 如何把它们映射成标准 AXI4 的 AR/AW/W/R/B 通道。
- 建议同时重读 **u6-l3（L1 cache 仲裁）**：那里讲了 l1cache_arb 如何贴/剥 cache_id，是本讲 source 第二层的具体实现，二者互为印证。
