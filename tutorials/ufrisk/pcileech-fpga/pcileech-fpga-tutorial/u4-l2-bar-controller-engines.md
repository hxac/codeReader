# BAR PIO 控制器：读写引擎

## 1. 本讲目标

学完本讲后，你应当能够：

1. 说清 `pcileech_tlps128_bar_controller` 在 TLP 通路里的位置与职责——它把哪些 TLP 认作「打到我 BAR 上」的读/写请求，又是怎么把响应送回去的。
2. 解释 `in_is_rd` / `in_is_wr` 这两个标志如何用 TLP 首拍的 Fmt/Type 指纹 + BAR 命中位 + 包边界来识别一次读或写。
3. 画出读引擎 `bar_rdengine` 的四段流水线（入队 → 拆包 → 逐 DWORD 出请求 → 收响应拼 CplD），并理解 `rd_req_ctx` / `rd_rsp_ctx` 这对「上下文搭车」机制。
4. 画出写引擎 `bar_wrengine` 的状态机，理解它如何从 128 位流里逐拍拆出 32 位写、并正确生成字节使能 `wr_be`。
5. 说清「每 CLK 1 DWORD 读 + 1 DWORD 写」这条吞吐硬约束的来源，以及它为什么会带来**静默丢包**风险。

本讲是专家层第 2 篇，承接 u4-l1（配置空间影子）和 u3-l5（TLP 多路复用与位宽转换）。u4-l1 讲的是「配置空间」那一类 TLP（CfgRd/CfgWr）怎么被响应；本讲讲的是「BAR 内存空间」那一类 TLP（MRd/MWr）怎么被响应——两者是平行的两条本地应答通路，最终都汇入 u3-l5 讲过的 `sink_mux1`。

## 2. 前置知识

在进入源码前，先用三段话把背景补齐。已经熟悉的概念可以跳过。

### 2.1 什么是 BAR，为什么需要 PIO

PCIe 设备用 **BAR（Base Address Register）** 向主机声明「我有一段内存空间，你可以往这里读写」。主机枚举设备时，会为每个 BAR 分配一段物理地址；此后主机 CPU 对这段地址的任何 load/store，都会被 PCIe 铁律翻译成一个 **TLP**，顺着链路送到设备。

设备侧响应这种「主机主动来访问我的内存」有两种做法：

- **DMA**：设备自己往主机内存里搬数据（这是 pcileech-fpga 的主业，走的是另一条通路，不在本讲范围）。
- **PIO（Programmed I/O）/ BAR 访问**：主机 CPU 主动来读写设备的 BAR，设备需要「当场回答」——读到要返回数据（Completion with Data，简称 **CplD**），写到要回个无数据的完成包（**Cpl**）或干脆静默接受。

pcileech-fpga 想伪装成一个「真实」的 PCIe 设备（例如一张网卡、一个 NVMe 控制器），就必须让它的 BAR 看起来「有内容、能响应」。`bar_controller` 就是干这件事的。

### 2.2 TLP 首拍的指纹：Fmt/Type

这是 u3-l4 已经讲过的知识点，这里只回顾结论。一个 TLP 的第一个 32 位字（DW0）里：

- `tdata[31:29]` = **Fmt**（格式，3 位）：编码「3DW 头/4DW 头」×「带数据/不带数据」。
- `tdata[28:24]` = **Type**（类型，5 位）：编码 Memory / Config / Completion 等。

本讲只关心两类：

| 业务 | 方向 | Fmt（3DW 头/4DW 头） | Type | 对应请求 |
|------|------|----------------------|------|----------|
| 读 | 主机→设备，**不带数据** | `000`（3DW）或 `001`（4DW） | `00000`（Memory） | **MRd** |
| 写 | 主机→设备，**带数据** | `010`（3DW）或 `011`（4DW） | `00000`（Memory） | **MWr** |

代码里把 `tdata[31:25]`（即 `{Fmt, Type[4:1]}`，故意丢掉 `Type[0]`）当作一个 **7 位指纹**来快速比对——这点 u3-l4 已解释过原因（`Type[0]` 对 Memory 类无意义）。本讲会看到 `bar_controller` 用同一套指纹做请求识别。

### 2.3 IfAXIS128 契约速查

u3-l3 / u2-l1 讲过 `IfAXIS128`，这里只列本讲要用的字段（见 [pcileech_header.svh:165-194](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L165-L194)）：

| 信号 | 位宽 | 含义 |
|------|------|------|
| `tdata` | 128 | 一个拍里最多塞 4 个 DW（16 字节） |
| `tkeepdw` | 4 | 每个 DW 是否有效（类似 AXI-Stream 的 keep，但粒度是 DW） |
| `tvalid` | 1 | 本拍数据有效 |
| `tlast` | 1 | 本拍是包的最后一拍 |
| `tuser[0]` | 1 | 本拍是包的**第一**拍（first） |
| `tuser[1]` | 1 | 本拍是包的**最后**一拍（last，冗余于 tlast） |
| `tuser[8:2]` | 7 | **BAR 命中位图**：bit2=BAR0、bit3=BAR1、…、bit7=BAR5、bit8=Expansion ROM |
| `tready` / `has_data` | 1 | 反压握手（仅 `source`/`sink` modport 有；`*_lite` 无反压） |

一句话：`tuser[8:2]` 告诉你「这个 TLP 落在了哪个 BAR 上」，这正是 `bar_controller` 做分发的依据。

## 3. 本讲源码地图

本讲几乎全部围绕**一个文件**：

| 文件 | 作用 |
|------|------|
| [PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv) | 整个 BAR PIO 子系统：1 个控制器 + 读引擎 + 写引擎 + 3 个示例 BAR 实现，共 4 个 `module` 全在这个文件里。 |

为了看清它在系统里的位置，还需要两处「外部接线」：

| 文件 / 位置 | 作用 |
|------|------|
| [pcileech_pcie_tlp_a7.sv:35-42](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_tlp_a7.sv#L35-L42) | 在 TLP 顶层例化 `bar_controller`：输入接 `tlps_rx`，输出接 `tlps_bar_rsp`，使能位来自 `dshadow2fifo.bar_en`。 |
| [pcileech_header.svh:165-194](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L165-L194) | `IfAXIS128` 接口定义（见 2.3）。 |

文件内 4 个 module 的行号地图：

| module | 行号 | 一句话职责 |
|--------|------|-----------|
| `pcileech_tlps128_bar_controller` | [42-243](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L42-L243) | 顶层壳：识别读/写、例化读/写引擎、例化 7 个 BAR、把 7 路 BAR 读响应复用回读引擎。 |
| `pcileech_tlps128_bar_wrengine` | [255-387](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L255-L387) | 写引擎：128 位 TLP 流 → 32 位写请求（带字节使能）。 |
| `pcileech_tlps128_bar_rdengine` | [395-668](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L395-L668) | 读引擎：32 位读请求 ← MRd TLP，再把读回数据拼成 CplD TLP。 |
| 3 个 `pcileech_bar_impl_*` | [678-793](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L678-L793) | 可插拔示例 BAR：`none` / `loopaddr` / `zerowrite4k`。 |

## 4. 核心概念与源码讲解

### 4.1 BAR PIO 控制器总览：请求识别、引擎分流与 BAR 响应复用

#### 4.1.1 概念说明

`pcileech_tlps128_bar_controller` 是一个**分发壳（wrapper）**。它本身不做任何数据搬移，只做三件事：

1. **认人**：看着 `tlps_in` 流过来的每一拍，判断「这一拍/这一个包是不是打到 BAR 上的读或写」。
2. **分流**：把读请求喂给读引擎，把写请求喂给写引擎——两者并行、互不干扰。
3. **汇合**：读引擎发出去的 `rd_req_*` 会被 7 个 BAR 实现之一接走并回送 `rd_rsp_*`；控制器再把这 7 路响应复用成一路交还给读引擎。

它对外只有两个 AXIS 端口（[42-49 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L42-L49)）：输入 `tlps_in`（`sink_lite`，无反压接收）和输出 `tlps_out`（`source`，带回压的 CplD 应答流）。注意输入是 `sink_lite`——意味着上游（`tlps_rx`）保证不会因为 BAR 控制器而停顿，反过来说，BAR 控制器**必须自己消化掉所有进来的数据**，消化不了就丢（见 4.3.1 的丢包机制）。

> 为什么输入无反压？因为 `tlps_rx` 是从 PCIe 硬核经位宽转换来的实时流（u3-l1、u3-l5），硬核不会为一个慢吞吞的 BAR 响应器停下来。所以这套设计选择「宁可丢包也不卡链路」。

#### 4.1.2 核心流程

```
                 tlps_in (128b AXIS, sink_lite, 来自 tlps_rx)
                          │
          ┌───────────────┼────────────────┐
          │ (每拍组合判定)                  │
          │  in_is_first = tuser[0]         │
          │  in_is_bar   = bar_en & hit     │
          │  in_is_rd    = first&last&指纹  │
          │  in_is_wr    = first&ready&指纹 │
          │               (或 in_is_wr_last)│
          ▼                                ▼
   ┌──────────────┐                 ┌──────────────┐
   │  读引擎       │                 │  写引擎       │
   │  rdengine    │                 │  wrengine    │
   │              │                 │              │
   │ rd_req_* ────┼─┐      ┌────────┤ wr_bar/addr/ │
   │              │ │      │        │ be/data/valid│
   │ rd_rsp_* ◄───┼─┼──┐   │        └──────┬───────┘
   └──────┬───────┘ │  │   │               │
          │         │  │   │               ▼
          ▼         │  │   │      (wr_* 广播给 7 个 BAR,
      tlps_out      │  │   │       仅 bar[i]=1 的有效)
      (CplD 应答)   │  │   │
                    ▼  ▼   ▼
              ┌──────────────────────────┐
              │  7 个 BAR 实现 (i_bar0..6) │
              │  bar0=zerowrite4k         │
              │  bar1=loopaddr            │
              │  bar2..6=none             │
              └──────────────────────────┘
```

读/写两个引擎**共用同一个 `tlps_in`**：每一拍数据会被两个引擎同时「看」到，但每个引擎内部都有一个 `tlps_in_valid` 门控（分别由 `in_is_rd`、`in_is_wr` 限定，见 [90 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L90) 与 [107 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L107)），只有被判定为「属于自己」的拍才会真正被各自引擎的入队 FIFO 收下。

#### 4.1.3 源码精读

**(a) 请求识别：4 个标志位**

[56-69 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L56-L69) 是本模块的「大脑」：

```systemverilog
wire in_is_first = tlps_in.tuser[0];                              // 本拍是包首
wire in_is_bar   = bar_en && (tlps_in.tuser[8:2] != 0);           // BAR 命中且功能开启
wire in_is_rd    = (in_is_first && tlps_in.tlast &&
                    ((tlps_in.tdata[31:25] == 7'b0000000) ||      // MRd, 3DW 头, 无数据
                     (tlps_in.tdata[31:25] == 7'b0010000) ||      // MRd, 4DW 头, 无数据
                     (tlps_in.tdata[31:24] == 8'b00000010)));     // 额外读变体
wire in_is_wr    = in_is_wr_last ||
                   (in_is_first && in_is_wr_ready &&
                    ((tlps_in.tdata[31:25] == 7'b0100000) ||      // MWr, 3DW 头, 带数据
                     (tlps_in.tdata[31:25] == 7'b0110000) ||      // MWr, 4DW 头, 带数据
                     (tlps_in.tdata[31:24] == 8'b01000010)));     // 额外写变体
```

三个关键设计点：

1. **`in_is_bar` 同时卡了 `bar_en` 与 BAR 命中**。`bar_en` 来自主机寄存器 `rw` 经 `dshadow2fifo`（u2-l5、u3-l3），主机没开启 BAR 功能时，整个控制器对任何 TLP 都「视而不见」。`tuser[8:2] != 0` 表示至少命中了 BAR0~BAR5 或 Expansion ROM 中的一个——这正是 u3-l1 里 `tlps128_src64` 从硬核 BAR hit 位搬过来的字段。

2. **读请求要求 `in_is_first && tlps_in.tlast` 同时成立**。为什么？因为 MRd 是「纯头、无数据」的请求：3DW 头 = 12 字节、4DW 头 = 16 字节，**一拍 128 位（16 字节）装得下整个请求**。所以「首拍即末拍」是读请求的天然特征。这也意味着：如果某个读请求因为某种原因被拆成了多拍（理论上不该发生），`bar_controller` 是认不出来的。

3. **写请求用 `in_is_wr_last` 跨拍保持**。写请求带数据，可能跨多拍。`in_is_wr` 在首拍被置位后，由 [63-69 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L63-L69) 的 `in_is_wr_last` 寄存器一直保持到包尾：

```systemverilog
always @ ( posedge clk )
    if ( rst )        in_is_wr_last <= 0;
    else if ( tlps_in.tvalid )
        in_is_wr_last <= !tlps_in.tlast && in_is_wr;   // 还没到末拍就锁存
```

指纹对照表（结合 2.2 节的 Fmt/Type 定义）：

| 标志 | 代码指纹 | Fmt | Type | 解读 |
|------|----------|-----|------|------|
| `in_is_rd` | `7'b0000000` | `000` | `0000x` | MRd，3DW 头 |
| `in_is_rd` | `7'b0010000` | `001` | `0000x` | MRd，4DW 头 |
| `in_is_wr` | `7'b0100000` | `010` | `0000x` | MWr，3DW 头 |
| `in_is_wr` | `7'b0110000` | `011` | `0000x` | MWr，4DW 头 |

> 第三条 `8'b00000010` / `8'b01000010` 是代码额外列出的请求变体匹配（罕见类型），主干就是上表四项。重点是**机制**：拿首拍的 Fmt/Type 指纹做一次组合比对，不存状态、不跨拍。

**(b) 引擎例化：把 valid 当成门控**

读引擎例化见 [84-100 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L84-L100)，写引擎见 [102-115 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L102-L115)。两者都把 `.tlps_in(tlps_in)` 整条流接进去，但各自带一个独立的 `tlps_in_valid` 输入：

```systemverilog
// 读引擎只收「是 BAR 命中的读」
.tlps_in_valid ( tlps_in.tvalid && in_is_bar && in_is_rd )
// 写引擎只收「是 BAR 命中的写」
.tlps_in_valid ( tlps_in.tvalid && in_is_bar && in_is_wr )
```

注意写引擎还多了一个 `tlps_in_ready` 输出（接到 `in_is_wr_ready`），回流给 `in_is_wr` 的判定——这是为了在写 FIFO 快满时**不再把后续拍认作新写的首拍**，避免把一个还没收完的写包拦腰切断（详见 4.3.3）。

**(c) BAR 读响应的 7 选 1 复用**

[117-135 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L117-L135) 把 7 个 BAR 的 `bar_rsp_*` 优先级复用成一路 `rd_rsp_*` 交还读引擎：

```systemverilog
assign rd_rsp_ctx = bar_rsp_valid[0] ? bar_rsp_ctx[0] :
                    bar_rsp_valid[1] ? bar_rsp_ctx[1] : ... ;
assign rd_rsp_valid = bar_rsp_valid[0] || ... || bar_rsp_valid[6];
```

因为读请求 `rd_req_valid` 在下发时已经带了 `rd_req_bar` 位图（[541 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L541)），7 个 BAR 实现各自用 `rd_req_valid && rd_req_bar[i]` 做门控（[146 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L146)），所以同一时刻一般只有一个 BAR 会拉高 `bar_rsp_valid[i]`。这个 mux 本质上是「7 取第一个有效者」。

**(d) 7 个 BAR 的例化与广播式接线**

[137-240 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L137-L240) 例化了 7 个 BAR：`i_bar0`=`zerowrite4k`、`i_bar1`=`loopaddr`、`i_bar2..5`=`none`、`i_bar6_optrom`=`none`。它们的接线方式完全一致——`wr_*` 和 `rd_req_*` 是**广播**给所有 7 个 BAR 的，每个 BAR 自己用 `wr_bar[i]` / `rd_req_bar[i]` 这一比特决定「这次是不是给我的」：

```systemverilog
.wr_valid     ( wr_valid && wr_bar[0]         ),   // 只有命中 BAR0 时才有效
.rd_req_valid ( rd_req_valid && rd_req_bar[0] ),
```

这是典型的「地址译码在叶子节点做」的总线风格：控制器不挑货，把货全摆上架，每个 BAR 自己看条码（`bar[i]`）决定拿不拿。

> 这 7 个槽位与 `IfAXIS128.tuser[8:2]` 的 BAR 编码一一对应：`bar[0]`↔BAR0(`tuser[2]`)、…、`bar[6]`↔Expansion ROM(`tuser[8]`)。所以 `i_bar6_optrom` 这个名字暗示它原本是为 Option ROM 预留的槽。

#### 4.1.4 代码实践：读文件头注释，盘点三类示例 BAR

> 这是本讲规格指定的实践任务，属于「源码阅读型实践」——不需要硬件，只需读代码。

**实践目标**：把 `bar_controller` 文件头的「使用须知」和文件末尾的 3 个示例实现对照阅读，建立「控制器契约 vs. 实现自由度」的直觉。

**操作步骤**：

1. 打开 [pcileech_tlps128_bar_controller.sv:1-37](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L1-L37)，逐条读 `Considerations` 列表（12-24 行）。
2. 跳到 3 个实现模块，读它们各自文件头注释与代码：
   - `pcileech_bar_impl_none`：[678-700](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L678-L700)
   - `pcileech_bar_impl_loopaddr`：[710-741](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L710-L741)
   - `pcileech_bar_impl_zerowrite4k`：[749-793](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L749-L793)
3. 自己画一张表，列出每个实现的：读返回什么、写支持吗、读延迟（CLK）、用什么存储。

**参考答案（应观察到的现象）**：

| 实现 | 读返回 | 写 | 读延迟 | 存储 |
|------|--------|----|--------|------|
| `none` | 永不响应（`rd_rsp_valid` 恒 0） | 丢弃 | N/A | 无 |
| `loopaddr` | 把读地址本身当数据返回 | 不支持（接了 `wr_*` 但不用） | 2 CLK | 无（纯组合+寄存器） |
| `zerowrite4k` | 返回 BRAM 里 4KB 内容 | 支持（按 `wr_be` 逐字节写） | 2 CLK（BRAM 双周期） | 4KB 双口 BRAM，`.coe` 初始化 |

4. **回答关键问题：为什么所有用户实现核心必须具有相同的读返回延迟？**

   答案分两层：
   - **直接原因**：读引擎在 [第 3 段](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L521-L583)「每 CLK 1 个 DWORD」地连续发 `rd_req_valid`，第 4 段则按**固定节拍**收 `rd_rsp_valid` 并把它们 4 个一组拼进 128 位 CplD。它假定「请求发出后第 N 拍必有响应回来」，请求与响应靠**时序对齐**配对，而不是靠查表。
   - **后果**：如果 BAR0 延迟 2 拍、BAR1 延迟 3 拍，那么一次跨越不同 BAR 的读（或两次紧邻的读落在不同 BAR 上）会让响应流出现「错位/气泡」，第 4 段的拼装状态机就会把数据塞错格子，拼出错误的 CplD——这就是文件头 [17-18 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L17-L18) 所说的「undefined behavior」。

   因此 `loopaddr` 和 `zerowrite4k` 都被刻意做成 **2 CLK 延迟**（见各自注释 `Latency = 2CLKs`），保证可互换；而 `none` 干脆不响应，只能用在「确定不会有流量打到这个 BAR」的占位槽上（所以 `bar2..6` 都填 `none` 是安全的，因为 `tuser[8:2]` 命中它们时通常意味着主机在访问一个未实现的 BAR，本就该没有应答）。

**预期结果**：你能用自己的话讲清「控制器规定了接口和时序契约，实现只能在契约内换存储/换返回值，不能换延迟」。如果讲不清，回头再看一遍第 4 段的拼装逻辑（[609-638 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L609-L638)）。

#### 4.1.5 小练习与答案

**Q1**：把 `i_bar1` 从 `loopaddr` 换成 `none`，会对功能产生什么影响？主机访问 BAR1 时会观察到什么？

> **答**：BAR1 将不再返回任何 CplD。主机 CPU 读 BAR1 地址时，请求 TLP 进入读引擎，但 `none` 永不拉高 `rd_rsp_valid`，于是不会产生完成包。主机端会因等待 Completion 超时而 abort 该访问（Linux 下通常是读到全 `0xff` 或触发 AER）。读引擎内部因 [583-585 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L583-L585) 的 `rd3_enable` / 请求节流，请求会停留在流水线里直到被后续请求挤掉——属于预期的「静默无响应」。

**Q2**：`in_is_rd` 为什么不检查 `in_is_wr_last`，而 `in_is_wr` 要检查 `in_is_wr_ready`？

> **答**：读请求一定只有一拍（首拍即末拍），不需要跨拍状态，所以 `in_is_rd` 纯组合判定即可。写请求可能跨多拍，第一拍要用 `in_is_wr_ready`（写 FIFO 未满）做握手，避免在 FIFO 满时还把一个多拍写包的开头认作有效写——否则后续拍收不下，就会产生半截写包。`in_is_wr_last` 则负责把首拍的判定延续到包的中间和末拍。

---

### 4.2 读引擎 bar_rdengine：四段流水线与 CplD 拼装

#### 4.2.1 概念说明

读引擎要解决的核心矛盾是：**主机一次 MRd 可能请求很长一段数据（最多 1024 DW = 4KB），但 PCIe 完成包（CplD）有最大载荷限制，且 BAR 实现每拍只能吐 1 个 DWORD。** 所以读引擎必须做三件转化：

1. **解析**：从 MRd 头里取出长度、地址、Requester ID、tag、BAR 号。
2. **拆分**：把一个超长请求拆成多个 ≤128 DW 的 CplD 子包（遵守最大载荷）。
3. **节拍化 + 拼装**：把每个子包拆成「每拍 1 个 DWORD」的读请求序列发给 BAR；等 BAR 按固定延迟把数据送回来，再把这一个个 32 位 DWORD 重新**4 个一组**拼回 128 位 AXIS 拍，加上 CplD 头，输出。

整个引擎因此被设计成 **4 段流水线**，段与段之间各有一个 FIFO 缓冲（注释 [414、444、521、587 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L413-L414)把每段标得很清楚）。

#### 4.2.2 核心流程

```
段1 入队:  MRd 首拍 → 解析出 {dwlen,bar,tag,reqid,addr} → FIFO(rd1)
            （每个 MRd 只产生 1 条记录，因为读请求只有一拍）
              │
段2 拆包:  1 条记录 → 可能拆成多条「≤32DW 的 CplD 子包」记录 → FIFO(rd2)
            （状态机 state2：REQDATA / PROCESSING，循环吐子包）
              │
段3 节拍:  1 条子包记录 → 展开成 N 个「单 DWORD 读请求」→ rd_req_*
            （状态机 state3：每拍减 1 个 dwlen、加 1 个 dwaddr）
              │              ▲
              ▼              │ rd_rsp_* (BAR 按固定延迟回送)
段4 拼装:  收到的 32 位 DWORD → 4 个一组拼成 128 位 CplD → FIFO(rdrsp) → tlps_out
            （含 CplD 头生成、字节序倒换、包计数）
```

一个精妙之处：`rd_req_ctx`（88 位）和 `rd_rsp_ctx`（88 位）这对信号里**搭便车携带了完整的请求上下文**（tag、Requester ID、字节计数、首/末标志、地址）。BAR 实现只是把它原样寄存两拍再吐回来（见 `loopaddr` 的 [732-738 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L732-L738)）。这样读引擎**不需要单独的 tag RAM**去配对请求和响应——上下文随数据一起旅行，回来时自带身份证。

#### 4.2.3 源码精读

**段 1：解析入队（[413-442 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L413-L442)）**

```systemverilog
wire [10:0] rd1_in_dwlen = (tlps_in.tdata[9:0] == 0) ? 11'd1024 : {1'b0, tlps_in.tdata[9:0]};
wire [6:0]  rd1_in_bar   = tlps_in.tuser[8:2];
wire [15:0] rd1_in_reqid = tlps_in.tdata[63:48];
wire [7:0]  rd1_in_tag   = tlps_in.tdata[47:40];
wire [31:0] rd1_in_addr  = { ((tlps_in.tdata[31:29]==3'b000) ? tlps_in.tdata[95:66]
                                                              : tlps_in.tdata[127:98]), 2'b00 };
```

字段都来自 MRd 头：长度字段 `tdata[9:0]`（注意 `==0` 时按规范解释成 1024 DW，即最大读长度）、tag、Requester ID（谁发的，CplD 要回给它）。地址提取与文件头 [19-21 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L19-L21) 的说明一致：3DW 头取 DW2、4DW 头取 DW3 的低 32 位（**BAR 不支持 4GB 以上地址**），末尾补 `2'b00` 把 DW 地址转成字节地址。整理成 74 位打包后写入 `fifo_74_74_clk1_bar_rd1`。

**段 2：大请求拆分（[444-519 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L444-L519)）**

这一段处理「一次 MRd 请求 4KB，但单个 CplD 最多 128 DW（512 字节）」的拆分。状态机 `state2` 在 `REQDATA` 与 `PROCESSING` 间循环：

```systemverilog
// 首个子包长度：把起始地址对齐到 32DW 边界后的剩余
wire [4:0] rd2_pkt1_dwlen_pre = ((rd1_out_addr5 + rd1_out_dwlen5 > 6'h20) || ...) ? (6'h20 - rd1_out_addr5) : rd1_out_dwlen5;
wire       rd2_pkt1_large     = (rd1_out_dwlen > 32) || (rd1_out_dwlen != rd2_pkt1_dwlen);
```

关键常量是 `6'h20` = 32 DW = 128 字节——这是它选定的单包上限（一个保守值，低于典型 Max Payload）。若总长 > 32 DW 或不能一次装下，就置 `rd2_pkt1_large`，进入 `PROCESSING` 状态，每轮从剩余长度 `rd2_total_dwlen` 里切掉 32 DW，更新字节计数 `rd2_pkt2[85:74]` 和基址 `rd2_pkt2[11:0]`，直到切完。每个子包都带完整的 `{bc, dwlen, ctx}`，成为段 3 的一个工作单元。

> 字节计数（`bc`）遵守 PCIe 规则：即便拆成多个 CplD，每个的 Byte Count 字段反映的也是「**整个原始请求**还剩多少字节未传」，而不是本子包的字节数。这就是 [459 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L459) `rd2_pkt1_bc = rd1_out_dwlen << 2` 用**原始总长**、而 [488 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L488) `rd2_pkt2[85:74] <= rd2_pkt1_dwlen_next << 2` 用**剩余量**的原因。

**段 3：逐 DWORD 展开读请求（[521-585 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L521-L585)）**

把一个子包（比如 32 DW）展开成 32 个连续的「单 DWORD 读请求」。状态机 `state3` 每拍：`dwlen--`、`dwaddr++`、更新 `first`/`last` 标志：

```systemverilog
`S3_ENGINE_PROCESSING: begin
    rd3_process_first <= 1'b0;                       // 首拍之后都不是 first
    rd3_process_last  <= rd3_process_next_last;      // 还剩 2 个时就预告 last
    rd3_process_dwlen <= rd3_process_dwlen - 1;
    rd3_process_dwaddr<= rd3_process_dwaddr + 1;
    ...
end
```

输出给 BAR 的就是 [540-543 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L540-L543)：

```systemverilog
assign rd_req_ctx   = { rd3_process_first, rd3_process_last, rd3_process_data };
assign rd_req_bar   = rd3_process_data[62:56];
assign rd_req_addr  = { rd3_process_data[31:12], rd3_process_dwaddr, 2'b00 };
assign rd_req_valid = rd3_process_valid;
```

注意 `rd_req_addr` 是把**基地址**（来自原始 MRd）和**本 DWORD 的偏移**（`rd3_process_dwaddr`）拼起来——所以每个 DWORD 读请求都自带完整地址，BAR 实现不需要自己维护指针。

**段 4：响应拼装成 CplD（[587-666 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L587-L666)）**

这是最精巧的一段，干三件事：

**(i) 解包上下文**（[591-599 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L591-L599)）：从回送的 `rd_rsp_ctx` 里把 first/last/dwlen/bc/reqid/tag/lowaddr 全取出来——这些就是段 1 解析、段 2 拆分时塞进去的原始字段，现在原样回来了。

**(ii) 字节序倒换**（[600 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L600)）：

```systemverilog
wire [31:0] rd_rsp_data_bs = { rd_rsp_data[7:0], rd_rsp_data[15:8], rd_rsp_data[23:16], rd_rsp_data[31:24] };
```

`_bs` = byte-swap。PCIe 链路上是**小端字节序**，而 BRAM/寄存器里通常按自然位序存，所以发出去前要把 4 字节的顺序反过来（和 `pcileech_header.svh` 里的 `_bs32` 宏同源）。

**(iii) 32→128 位拼装 + CplD 头**（[609-638 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L609-L638)）：每收到一个「first」响应，就开一个新 CplD，前 3 个 DW 写头（Fmt/Type/length、Completer ID + bc、Requester ID + tag + lower_addr），第 4 个 DW 开始填数据；后续响应按 `tkeepdw` 的空位依次填入，攒满 4 个 DW（`tkeepdw[3]` 置位）就输出一拍：

```systemverilog
tdata[31:0]  <= { 22'b0100101000000000000000, rd_rsp_dwlen };   // Fmt=010(CplD,3DW,带数据), Type=01010(Completion)
tdata[63:32] <= { pcie_id[7:0], pcie_id[15:8], 4'b0, rd_rsp_bc };
tdata[95:64] <= { rd_rsp_reqid, rd_rsp_tag, 1'b0, rd_rsp_lowaddr };
```

这里能清楚看到 CplD 头的 Fmt=`010`、Type=`01010`（Completion），与 u3-l4 的指纹表完全对得上——`0100101` 即 CplD。

**(iv) 输出 FIFO + 包计数**（[641-666 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L641-L666)）：拼好的 134 位（1 first + 1 last + 4 tkeepdw + 128 tdata）写入 `fifo_134_134_clk1_bar_rdrsp`，输出到 `tlps_out`。`pkt_count` 统计「整包个数」，驱动 `has_data`，让下游 `sink_mux1`（u3-l5）知道这里有完整包可取——这与 `src_fifo` 的整包指示设计同构。

#### 4.2.4 代码实践：追踪一次 4KB 读的旅程

**实践目标**：用一个具体数值（主机读 4096 字节）走一遍段 1→段 4，验证你对拆分和拼装的理解。

**操作步骤**：

1. 假设主机发来一个 MRd：`dwlen = 1024`（即 4096 字节），`tag = 0x07`，命中 BAR0，起始地址 `0x0000_0000`。
2. 在段 1（[416-426 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L416-L426)）写出 `rd1_in_data` 各字段的值。
3. 在段 2（[454-457 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L454-L457)）判断 `rd2_pkt1_large` 是否为真、第一个子包长度是多少。
4. 推算段 2 一共会切出几个子包、每个子包多少 DW。
5. 在段 4 推算：一共会发出几个 CplD TLP？第一个 CplD 的 `bc`（字节计数）字段是多少？

**需要观察的现象 / 预期结果**：

1. 段 1：`rd1_in_dwlen = 1024`、`rd1_in_tag = 0x07`、`rd1_in_addr = 0x0`、`rd1_in_bar = 0b0000010`（仅 bit2=BAR0）。
2. 段 2：`rd1_out_dwlen(1024) > 32`，故 `rd2_pkt1_large = 1`；首包 `rd2_pkt1_dwlen_pre = 32 - 0 = 32`（起始地址 4KB 页内偏移为 0），首包 32 DW。
3. 总长 1024 DW ÷ 32 DW/包 = **32 个子包**，每包 32 DW。
4. 段 4：共 32 个 CplD。**第一个 CplD 的 `bc` 字段 = 1024 × 4 = 4096 字节**（原始请求总量），不是 128 字节——这就是上面强调的 PCIe Byte Count 规则。后续每个 CplD 的 `bc` 递减：4096 → 3968 → … → 128，最后一个 CplD `bc=128`。

> 待本地验证：以上数值推演基于源码逻辑，若你想在真实硬件上确认，可在 `bar_controller` 顶层用 ILA 抓 `rd_req_valid`/`rd_req_addr` 与 `tlps_out.tvalid`/`tlast` 的波形，数一次 4KB 读产生的 CplD 个数与每个的 bc。

#### 4.2.5 小练习与答案

**Q1**：段 2 的单包上限为什么定 32 DW（`6'h20`），而不是直接用 4 DW（一拍 128 位）？

> **答**：32 DW = 128 字节是一个兼顾「遵守 PCIe Max Payload（通常 ≥128 字节）」与「减少 CplD 头开销」的折中。若定成 4 DW，每个 CplD 都要带 3DW 头，有效载荷只占 4/(4+3)≈57%，太浪费；定得过大又会撞 Max Payload 限制。32 DW 是个保守的安全值。

**Q2**：如果 BAR 实现的读延迟从 2 CLK 改成 4 CLK（但所有 BAR 一起改），段 4 的拼装还能正确工作吗？

> **答**：能。段 4 的拼装状态机（[615-638 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L615-L638)）靠 `rd_rsp_valid` 的电平驱动、按 `tkeepdw` 空位填数据，并不假设「请求后第几拍回来」——只要响应是**连续均匀**地回来（延迟固定，中间不夹气泡），拼装就正确。所以文件头强调的是「**所有 BAR 必须 same latency**」，而不是「必须是 2 CLK」。2 CLK 只是示例实现的选择。

---

### 4.3 写引擎 bar_wrengine：状态机与字节使能

#### 4.3.1 概念说明

写引擎处理的是 MWr TLP。它的难点和读引擎**镜像相反**：

- 读引擎是「**拉**」模型：自己掌控节拍，每拍主动发一个读请求，不会溢出。
- 写引擎是「**推**」模型：数据由 `tlps_in` 流推过来，每拍最多 16 字节（128 位），但 BAR 写端口每拍只能吃 4 字节（32 位）。**入快出慢，必然要排队。**

所以写引擎的核心是一个 **FIFO 缓冲 + 状态机**：先进 FIFO 缓冲 2048 字节，再由状态机慢慢拆成 32 位写输出。文件头 [247-253 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L247-L253) 把这个吞吐约束和丢包风险说得很直白：

> Input flow rate is 16bytes/CLK (max). Output flow rate is 4bytes/CLK. If write engine overflows incoming TLP is completely discarded silently.

这就是本讲学习目标里「潜在丢包风险」的来源。

#### 4.3.2 核心流程

```
tlps_in (128b, 仅 in_is_wr 的拍才进)
   │  打包成 141 位 {tuser[8:0], tkeepdw[3:0], tdata[127:0]}
   ▼
┌────────────────────────────┐
│ fifo_141_141_clk1_bar_wr   │   ← 2048 字节缓冲（深度按 141 位算）
│   (tlps_in_ready=prog_empty)│      prog_empty 用作反压信号回给 in_is_wr
└─────────────┬──────────────┘
              │ f_tdata/f_tkeepdw/f_tuser/f_tvalid
              ▼
┌────────────────────────────┐
│ 7 态状态机                  │
│  IDLE → FIRST →            │
│   (4DW_REQDATA) →          │   ← 区分 3DW 头(直接 TX3) / 4DW 头(多取一拍)
│   TX0 → TX1 → TX2 → TX3   │   ← 每态输出 1 个 DW，tkeepdw 决定跳哪
└─────────────┬──────────────┘
              ▼
   wr_bar / wr_addr / wr_be / wr_data / wr_valid  （广播给 7 个 BAR）
```

状态机的精髓是：**128 位里最多 4 个 DW，但要分 4 拍输出**，所以用 `TX0..TX3` 四个状态依次吐出每个 DW；`tkeepdw` 指示哪些 DW 有效，据此决定要不要跳过、以及包尾落在哪。

#### 4.3.3 源码精读

**(a) 入队 FIFO 与反压（[285-296 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L285-L296)）**

```systemverilog
fifo_141_141_clk1_bar_wr i_fifo (...);
    .wr_en      ( tlps_in_valid ),                       // 只写「是 BAR 写」的拍
    .din        ( {tlps_in.tuser[8:0], tlps_in.tkeepdw, tlps_in.tdata} ),
    .prog_empty ( tlps_in_ready ),                       // 快空了 → 可以再收
    ...
```

`tlps_in_ready` 接到控制器顶层的 `in_is_wr_ready`（[108 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L108)），回流进 `in_is_wr` 判定。`prog_empty` 是 FIFO 的「可编程空」阈值——当 FIFO 里数据少到阈值以下时拉高，表示「我饿，快喂我」。注意 `full` 端口悬空不接（[290 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L290)），意味着**写满后新数据会被静默丢弃**，不会有错误冒泡——这就是「silently discarded」。

> 141 = 9(tuser) + 4(tkeepdw) + 128(tdata)。FIFO 名字里的数字正是这个打包宽度。

**(b) 读使能的精细调度（[308-313 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L308-L313)）**

```systemverilog
assign f_rd_en = (state == `S_ENGINE_IDLE) ||
                 (state == `S_ENGINE_4DW_REQDATA) ||
                 (state == `S_ENGINE_TX3) ||
                 ((state == `S_ENGINE_TX2 && !tkeepdw[3])) ||
                 ((state == `S_ENGINE_TX1 && !tkeepdw[2])) ||
                 ((state == `S_ENGINE_TX0 && !f_tkeepdw[1]));
```

这段决定「什么时候从 FIFO 再取一拍」。核心思想：**当本拍里剩下的 DW 还够当前及后续 TX 状态处理时，就提前取下一拍**。比如在 `TX0` 时，如果本拍的 `tkeepdw[1]`（第 2 个 DW）无效，说明本拍只有 1 个 DW 要输出，`TX0` 处理完就该进 `FIRST` 取新包了，所以此时要把新数据读出来备好。这种「看 tkeepdw 跳状态」的设计避免了无谓的空拍。

**(c) 状态机主体（[315-385 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L315-L385)）**

`FIRST` 状态（[329-350 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L329-L350)）解析 MWr 头：

```systemverilog
wr_bar   <= f_tuser[8:2];                 // BAR 号来自 tuser
be_first <= f_tdata[35:32];               // 首 DW 字节使能（MWr 头里的 BE 字段）
be_last  <= f_tdata[39:36];               // 末 DW 字节使能
if ( f_tdata[31:29] == 8'b010 ) begin       // Fmt=010 → 3DW 头, 带数据
    addr <= { f_tdata[95:66], 2'b00 };      // 地址在 DW2
    state <= `S_ENGINE_TX3;                 // 数据从本拍 DW3([127:96])开始
end
else if ( f_tdata[31:29] == 8'b011 ) begin  // Fmt=011 → 4DW 头, 带数据
    addr <= { f_tdata[127:98], 2'b00 };     // 地址在 DW3(低32位)
    state <= `S_ENGINE_4DW_REQDATA;         // 数据在下一拍
end
```

注意 3DW 头时数据从**当前拍**的 DW3 开始（直接进 `TX3` 取 `[127:96]`），4DW 头时头占满了整个第一拍，数据在**下一拍**（先进 `4DW_REQDATA` 取下一拍再进 `TX0`）。

`TX0..TX3` 状态（[354-384 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L354-L384)）每个吐一个 DW，并据 `tkeepdw` 决定下一态：

```systemverilog
`S_ENGINE_TX1: begin
    addr   <= addr + 4;
    wr_data <= { tdata[32+00+:8], tdata[32+08+:8], tdata[32+16+:8], tdata[32+24+:8] };  // 又一次字节倒序
    wr_be   <= first_dw ? be_first : (tkeepdw[2] ? 4'hf : be_last);                      // ★字节使能生成
    state   <= tkeepdw[2] ? `S_ENGINE_TX2 : `S_ENGINE_FIRST;                             // 看下个 DW 在不在这拍
end
```

**(d) 字节使能 `wr_be` 的生成规则（最关键）**

PCIe 写允许「只写一个 DWORD 里的某几个字节」，由 MWr 头里的 **First/Last Byte Enable** 字段指定。但 PCIe 规则很严：**只有包的第一个 DWORD 和最后一个 DWORD 可以有部分字节使能，中间的 DWORD 必须全 4 字节有效**。`wr_be` 的生成完美体现这条规则：

```systemverilog
wr_be <= first_dw ? be_first                       // 第一个 DWORD：用首字节使能
                  : (tkeepdw[2] ? 4'hf : be_last); // 中间(4'hf 全使能) / 末尾(be_last)
```

三种情况：

| 条件 | `wr_be` 取值 | 含义 |
|------|-------------|------|
| `first_dw`（本 DW 是包的第一个 DW） | `be_first` | 用 MWr 头里的 First BE |
| 中间 DW（`tkeepdw[2]` 仍有效，后面还有） | `4'hf` | 全 4 字节都写 |
| 末尾 DW（后面没了） | `be_last` | 用 MWr 头里的 Last BE |

`wr_be` 直接接到 `zerowrite4k` 的 BRAM 写使能端口（[785 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L785) `.wea(wr_be)`），实现**按字节选择性写入**——这就是 `zerowrite4k` 名字里 "write4k" 能精准改字节的原因。

**(e) 字节倒序（又一次）**

注意 [359、366、373、380 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L359-L359) 拼装 `wr_data` 时都做了字节倒序（`{tdata[0+:8], tdata[8+:8], ...}`）。和段 4 的 `rd_rsp_data_bs` 道理一样：PCIe 流是小端，BRAM 是自然序，写之前要倒回来。读引擎在出口倒一次，写引擎在入口倒一次，二者对称。

#### 4.3.4 代码实践：预测一个 3DW 头小写的输出序列

**实践目标**：用一个最小例子（主机写 1 个 DW 到 BAR0）验证对 `FIRST→TX3` 路径与 `wr_be` 规则的理解。

**操作步骤**：

1. 假设主机发来一个 3DW 头、长度=1 DW 的 MWr，命中 BAR0，地址 `0x100`，数据 `0xAABBCCDD`，First BE = `4'b1111`，Last BE = `4'b0000`。
2. 因为长度=1，这个唯一的 DW **既是首也是末**。对照 [361 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L361) 的 `wr_be` 公式，推算 `wr_be` 到底取 `be_first` 还是 `be_last` 还是 `4'hf`。
3. 推算 `wr_addr`、`wr_data`（注意倒序）的值。
4. 思考：如果 First BE 改成 `4'b0011`（只写低 2 字节），`wr_be` 会变成什么？BRAM 里只有哪几个字节会被改？

**预期结果**：

1. 长度=1 时，`FIRST` 状态判定 Fmt=`010`（3DW 头带数据），进 `TX3`（[340 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L340)），`addr = {tdata[95:66], 2'b00} = 0x100`。
2. 在 `TX3`（[378-383 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L378-L383)）：`first_dw` 此时仍为 1（因为这是包首），所以 `wr_be <= first_dw ? be_first : ... = be_first = 4'b1111`。
3. `wr_data = {tdata[96+0+:8], ...}` 倒序后，`0xAABBCCDD` → `0xDDCCBBAA`（写到 BRAM 后读出来又会倒一次，最终逻辑值还是 `0xAABBCCDD`）。
4. 若 First BE = `4'b0011`：`wr_be = 4'b0011`，只有最低 2 字节（对应数据 `0xCC 0xDD`）被写入 BRAM 的对应位置，高 2 字节保持原值（如初始 0 则仍是 0）。

> 待本地验证：以上为基于源码的推演。若要实测，可在 `zerowrite4k` 的 BRAM 输出端抓 `doutb`，对比一次「全 BE 写」与「部分 BE 写」后的读回值差异。

#### 4.3.5 小练习与答案

**Q1**：写引擎为什么不像读引擎那样有「拆包」段？一个超大的 MWr（比如 4KB）进来会怎样？

> **答**：写是「推」模型，数据按到达的顺序逐拍进 FIFO、逐拍被状态机消费，**天然就是流式处理**，不需要像读那样先整理解析再拆分。但代价是 FIFO 深度有限（2048 字节）：如果一个 MWr 突发长度远超 2048 字节且输出端来不及排空，FIFO 写满后**整个后续 TLP 被静默丢弃**（`full` 端口未接，无错误反馈）。这是写引擎固有的丢包风险点。

**Q2**：`prog_empty` 接到 `tlps_in_ready` 而不是 `full` 接到反压，这有什么实际区别？

> **答**：`prog_empty` 是「**快要**空」的预警阈值，把它当 `ready` 用，意味着「FIFO 比较空时才允许控制器把下一拍认作新写包的首拍」——这是一种**提前刹车**，给状态机留出排空余量，避免在 FIFO 接近满时还接纳新包。但它不能阻止「当前正在收的多拍写包」的后续拍进来（那些由 `in_is_wr_last` 锁存，不受 `ready` 控制）。所以一旦遇到超长单包，仍可能溢出。`full` 信号未被使用，说明设计者接受了「满则丢」的语义，而不是「满则停链路」。

---

## 5. 综合实践：挂载一个自定义 BAR 核心

把本讲三个最小模块（控制器、读引擎、写引擎）和 u4-l1（配置空间影子）串起来，做一个完整的「设备仿真」小任务。

**任务**：实现一个最小自定义 BAR 核心 `pcileech_bar_impl_const32`——对任何读请求都返回固定 32 位常量 `0xDEADBEEF`，写请求忽略；要求读延迟必须与 `zerowrite4k`/`loopaddr` 一致（2 CLK）。

**步骤**：

1. **理解契约**：先看 [710-741 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L710-L741) 的 `loopaddr`——它就是「2 拍延迟、把输入原样转输出」的最简模板，你的实现应直接照搬它的寄存器级数。
2. **写模块**（示例代码，非项目原有）：

   ```systemverilog
   // 示例代码：固定返回 0xDEADBEEF 的 BAR 实现，延迟 2 CLK（与 loopaddr 对齐）
   module pcileech_bar_impl_const32(
       input               rst, clk,
       input  [31:0]       wr_addr, input [3:0] wr_be, input [31:0] wr_data, input wr_valid,
       input  [87:0]       rd_req_ctx, input [31:0] rd_req_addr, input rd_req_valid,
       output bit [87:0]   rd_rsp_ctx, output bit [31:0] rd_rsp_data, output bit rd_rsp_valid
   );
       bit [87:0] rd_req_ctx_1;   // 第 1 拍寄存
       bit        rd_req_valid_1;
       always @ ( posedge clk ) begin
           rd_req_ctx_1   <= rd_req_ctx;          // 拍 1：锁存上下文
           rd_req_valid_1 <= rd_req_valid;
           rd_rsp_ctx     <= rd_req_ctx_1;        // 拍 2：上下文原样回送
           rd_rsp_data    <= 32'hDEADBEEF;        // 拍 2：固定数据（注意读引擎段4会再倒序）
           rd_rsp_valid   <= rd_req_valid_1;
       end
   endmodule
   ```

3. **挂载**：把 [167-180 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L167-L180) 的 `i_bar2`（当前是 `pcileech_bar_impl_none`）替换成你的 `pcileech_bar_impl_const32`——这正是文件头 [22-24 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L22-L24) 说的「DO edit pcileech_tlps128_bar_controller (to swap bar implementations)」。端口信号名与 [168-180 行](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_tlps128_bar_controller.sv#L167-L180) 完全一致，无需改控制器其它部分。
4. **前提**：要让 BAR2 真正被访问到，还需在 Vivado PCIe 核的 GUI 里给 BAR2 分配空间（u4-l4 会讲），并通过主机命令把 `bar_en`（`rw` 控制位）置 1。
5. **验证思路**：上板后用 Linux 命令读 BAR2 映射的地址，应读到 `0xDEADBEEF`；写入后读回应仍为 `0xDEADBEEF`（因为写被忽略）。若读到的是 `0xEFBEADDE`，说明你多倒了一次字节序（读引擎段 4 已经做过 `_bs` 倒换，BAR 实现里不要再倒）。

> 待本地验证：本实践需要 FPGA 硬件与 Vivado 环境。无硬件时，可把步骤 1-3 作为「源码修改练习」完成，并在脑中/纸上推演读引擎段 4 对 `0xDEADBEEF` 的字节倒换结果。

## 6. 本讲小结

- `pcileech_tlps128_bar_controller` 是一个**分发壳**：用 `in_is_rd`/`in_is_wr`（首拍 Fmt/Type 指纹 + BAR 命中位 + 包边界）识别 MRd/MWr，把读喂给读引擎、写喂给写引擎，并把 7 个 BAR 的读响应优先级复用回读引擎。
- **读引擎是 4 段流水线**：解析入队 → 大请求拆成 ≤32DW 子包 → 逐 DWORD 展开读请求 → 收响应按固定节拍拼成 CplD；靠 `rd_req_ctx`/`rd_rsp_ctx`「上下文搭车」免除了独立的 tag 配对 RAM。
- **写引擎是 FIFO 缓冲 + 7 态机**：128 位流进、32 位写出，`FIRST` 区分 3DW/4DW 头，`TX0..TX3` 逐 DW 输出；`wr_be` 严格遵循「首/末 DW 用 BE、中间全使能」的 PCIe 规则。
- 吞吐硬约束：**每 CLK 最多 1 DWORD 读 + 1 DWORD 写**。读引擎自定节拍不会溢出；写引擎入快出慢，FIFO 满→**静默丢包**（`full` 未接）。
- 所有用户 BAR 实现核心**必须读延迟相同**（示例都是 2 CLK），否则读引擎段 4 的固定节拍拼装会错位、产生未定义行为——这是 `loopaddr` 与 `zerowrite4k` 都标 `Latency = 2CLKs` 的根本原因。
- 三个示例 BAR：`none`（占位、不响应）、`loopaddr`（回环地址、只读、无存储）、`zerowrite4k`（4KB 双口 BRAM、`.coe` 初始化、可按字节写）。

## 7. 下一步学习建议

- **u4-l3（BAR 示例实现与 .coe 初始化）** 会深入 `zerowrite4k` 背后的 `bram_bar_zero4k` IP 与 `.coe` 文件格式，讲清「4KB 初始值从哪来、怎么改」，是本讲 4.3 节存储细节的自然延伸。
- **u4-l4（设备身份定制：VID/PID/DSN/Class Code）** 会把本讲的 BAR 仿真与 u4-l1 的配置空间影子合在一起，讨论「改哪些字段、改了 lspci 能看到什么」，建议学完 u4-l3 后连读。
- 若想从系统角度回顾 BAR 响应是怎么被送上链路的，可重读 **u3-l5** 的 `sink_mux1`——本讲的 `tlps_out` 正是它的 `tlps_in2`（bar_rsp），优先级排在 cfg_rsp 之后。
- 对吞吐约束感兴趣、想理解「为什么 x1 的 USB3 速率是瓶颈」的读者，可跳到 **u6-l3（高级主题：LTSSM、链路状态、性能与调试）**。
