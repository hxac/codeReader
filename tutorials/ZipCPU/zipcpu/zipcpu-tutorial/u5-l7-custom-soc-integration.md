# 二次开发：把 ZipCPU 集成进自定义 SoC

## 1. 本讲目标

本讲是《ZipCPU 学习手册》收官篇。前面二十四讲我们分别读懂了 ISA、内核流水线、总线封装与外设；这一讲要把它们「拼成一块芯片」。

学完本讲，你应当能够：

- 理解「地址译码（address decode）」如何把一个 32 位地址变成「选中某一个从设备」的一根独热（one-hot）信号线。
- 理解「总线互连（crossbar）」如何让多个主设备（master）在多个从设备（slave）之间并发通信而不打架。
- 读懂 ZipCPU 的 AXI-Lite 顶层 `zipaxil` 对外暴露了哪些主端口与从端口，以及为什么它「不自带总线互连」。
- 在软件侧（`board.h`）定义一套与硬件一致的地址映射，并解释当地址映射对不上时程序为什么会崩。
- 用仓库自带的「积木」（`addrdecode`/`wbxbar`/`memdev`/`wbscope` 等）规划一个最小 SoC 的地址表与互连结构，并在模拟器中验证。

## 2. 前置知识

在开始之前，请确认你理解以下概念（前序讲义已建立）：

- **软核 CPU（soft core）**：用 Verilog 描述、可综合进 FPGA 的 CPU，本身只是一块「会算术、会跳转」的电路，要靠总线才能访问存储器和外设。
- **主设备 / 从设备（master / slave）**：主动发起一次总线交易的叫主设备（如 CPU、DMA）；被动响应的叫从设备（如 RAM、UART）。
- **Wishbone 与 AXI-Lite 两种总线协议**：ZipCPU 同时提供 Wishbone 顶层（`zipbones`/`zipsystem`）与 AXI-Lite 顶层（`zipaxil`）。本讲的互连积木里，`wbxbar`/`memdev`/`wbscope` 是 **Wishbone** 接口；`zipaxil` 是 **AXI-Lite** 接口。两种协议的握手信号不同，这是本讲最容易踩坑的地方，第 4.4 节会专门讲这个「协议边界」。
- **地址映射（address map）**：把 32 位地址空间划分成若干区间，每个区间对应一个从设备。这是 SoC 设计的「宪法」。
- **综合期参数 `OPT_*`**：作为 Verilog `parameter` 配合 `generate if`，在综合时决定电路是否生成（见 u5-l6）。

> 本讲依赖 u4-l2（ZipSystem 整合）与 u4-l3（AXI / AXI-Lite 封装）。建议先回顾这两讲对 `zipaxil` 端口与 `zipsystem` 内部总线的描述。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 | 属于哪一类 |
|------|------|-----------|
| [rtl/zipaxil.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxil.v) | ZipCPU 的 AXI-Lite 顶层封装，对外暴露分离的指令主端口、数据主端口与调试从端口 | CPU 顶层 |
| [sim/rtl/addrdecode.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/rtl/addrdecode.v) | 地址译码器：把地址变成独热的从设备选中信号 | 互连积木（Wishbone） |
| [sim/rtl/wbxbar.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/rtl/wbxbar.v) | Wishbone 交叉互连：N 个主设备 × M 个从设备，内部复用 `addrdecode` | 互连积木（Wishbone） |
| [sim/rtl/wbscope.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/rtl/wbscope.v) | 总线访问的逻辑分析仪（记录波形、触发后供 CPU 回读） | 调试外设（Wishbone） |
| [sim/zipsw/board.h](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/zipsw/board.h) | 软件侧的板级地址映射与外设结构体定义 | 软件 |
| [bench/rtl/memdev.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/rtl/memdev.v) | 一个最简单的 Wishbone 片上存储器从设备（可作为 RAM/ROM） | 互连积木（Wishbone） |

> 注意路径：互连积木与调试外设位于 `sim/rtl/` 与 `bench/rtl/`，属于「仿真/参考设计」目录，不是 CPU 核心的一部分。`zipaxil` 才是 `rtl/` 下真正可综合的 CPU 顶层。

## 4. 核心概念与源码讲解

### 4.1 地址译码器 addrdecode：把地址变成一根选中线

#### 4.1.1 概念说明

假设你有一块 SoC，里面有 RAM、ROM、UART、定时器四个从设备。CPU 在总线上给出一个地址 `0x02000000`，怎么知道这一笔访问该送给 UART 而不是 RAM？

答案就是「地址译码器」。它做一件事：**根据地址的高位，判断这笔访问属于哪个从设备，并拉高对应的那一根「选中」线**（独热码：同一时刻只有一根线为 1）。这就像邮编分拣：看邮编前几位就能决定信件发往哪个城市。

`addrdecode` 不关心数据怎么走、不关心读写时序，它只回答一个问题——「这个地址归谁管？」。所以它非常小、非常快（组合逻辑一拍出结果），是所有总线互连的底层零件。

#### 4.1.2 核心流程

`addrdecode` 的核心是「地址比较 + 独热输出」：

```
对每一个从设备 k（k = 0..NS-1）：
    若 (i_addr XOR SLAVE_ADDR[k]) AND SLAVE_MASK[k] == 0：
        且该从设备被允许访问（ACCESS_ALLOWED[k]）：
            则 prerequest[k] = 1      // 地址命中
把命中的 prerequest 与「本次是否有效 i_valid」相与，得到 request[]
输出 o_decode = request（独热码，外部据此驱动各从设备）
若没有任何从设备命中（request 全 0）且 i_valid：
    request[NS] = 1                   // 「无命中」伪从设备 → 产生总线错误
```

关键在于比较公式 `(i_addr ^ SLAVE_ADDR[k]) & SLAVE_MASK[k] == 0`：

- `SLAVE_ADDR[k]` 是第 k 个从设备的「基地址特征」。
- `SLAVE_MASK[k]` 是「哪些位需要比较」的掩码。
- `& MASK` 先把不关心的位（掩码为 0 的位）清零，再比较——这样地址低位（块内偏移）不参与译码，只有高位（块标识）参与。

数学上，对从设备 k，其地址区间是所有满足下列条件的地址集合：

\[
\text{Addr}_k = \{\, a \mid (a \oplus \text{SLAVE\_ADDR}_k)\ \&\ \text{SLAVE\_MASK}_k = 0 \,\}
\]

若 `SLAVE_MASK[k]` 有 m 个为 1 的位，则该区间大小为 \(2^{32-m}\) 字节。

#### 4.1.3 源码精读

**参数与端口**。`addrdecode` 用两个打包数组 `SLAVE_ADDR` 和 `SLAVE_MASK`（每个从设备一段 `AW` 位）描述全部从设备的地址特征，`NS` 是从设备个数：

[addrdecode.v:L46-L61](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/rtl/addrdecode.v#L46-L61) — 默认地址表。注意这是一种「高位切分」的经典写法：从设备 7..2 各用最高 3 位做标识（把地址空间切成 8 等份），从设备 1..0 各用最高 4 位（把其中一份再切成 2 份）。我们稍后在 4.3 节会把这张表换算成具体地址。

**译码比较**。这段是整个模块的灵魂——一行公式同时完成「按掩码比较」与「访问权限检查」：

[addrdecode.v:L118-L123](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/rtl/addrdecode.v#L118-L123) — `prerequest[iM] = ((i_addr ^ SLAVE_ADDR[iM]) & SLAVE_MASK[iM]) == 0) && ACCESS_ALLOWED[iM]`。这就是 4.1.2 节那个公式的 Verilog 实现。

**「无命中」伪从设备**。当一笔有效访问谁都没命中，`addrdecode` 会拉高第 `NS` 根线（注意是 `request[NS:0]`，比从设备数多一位），下游据此返回总线错误（bus error），而不是让访问石沉大海：

[addrdecode.v:L168-L192](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/rtl/addrdecode.v#L168-L192) — `request[NS]` 在 `i_valid && (prerequest == 0)` 时置 1，代表「请求了一个不存在的从设备，应当返回 bus error」。

**输出**。在组合输出模式下，`o_decode` 直接等于 `request`，地址与数据原样透传：

[addrdecode.v:L249-L259](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/rtl/addrdecode.v#L249-L259) — `o_decode = request`，是独热码；形式化断言要求它永远满足 `$onehot0`（最多一位为 1，见 [L355-L377](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/rtl/addrdecode.v#L355-L377)）。

> 还有个实用参数 `ACCESS_ALLOWED`：它是一个按从设备编号的位掩码，可以单独禁止某从设备的某方向访问（例如把一段 RAM 配成只读，写它就会触发 bus error）。本讲暂不深入。

#### 4.1.4 代码实践

**实践目标**：手工验证 `addrdecode` 的默认地址表，理解「掩码切分地址空间」。

**操作步骤**：

1. 打开 [addrdecode.v:L46-L61](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/rtl/addrdecode.v#L46-L61)。
2. 注意 Verilog 拼接 `{ }` 中**最左边的项对应最高位、也对应最高从设备编号**（`NS-1`）。把 8 个从设备的标识位填入下表（已给出前两个）：

   | 从设备 k | 标识位 | 掩码有效位 | 起始地址 | 区间大小 |
   |---------|--------|-----------|---------|---------|
   | 7 | `111` | 高 3 位 | 0xE0000000 | 256 MiB |
   | 6 | `110` | 高 3 位 | 0xC0000000 | 256 MiB |
   | 5 | `101` | 高 3 位 | ? | 256 MiB |
   | 4 | `100` | 高 3 位 | ? | 256 MiB |
   | 3 | `011` | 高 3 位 | ? | 256 MiB |
   | 2 | `010` | 高 3 位 | ? | 256 MiB |
   | 1 | `0010` | 高 4 位 | 0x20000000 | 128 MiB |
   | 0 | `0000` | 高 4 位 | 0x00000000 | 128 MiB |

3. 用 4.1.2 节的公式验证：地址 `0x02000000`（高 4 位 = `0000`）应命中从设备 0；地址 `0x20000000`（高 4 位 = `0010`）应命中从设备 1；地址 `0x10000000`（高 4 位 = `0001`）**任何从设备都不命中**，会落到 `request[NS]` 产生 bus error。

**需要观察的现象**：你会发现默认地址表刻意把 `0x00000000–0x1FFFFFFF` 这 512 MiB 切成了两半（`0000` 与 `0010`），中间夹着的 `0001`（即 `0x10000000–0x1FFFFFFF`）成了「没人认领」的空洞。

**预期结果**：手工换算与公式验证一致。注意 4.1.3 节最后一项——`0x10000000` 正好是 Verilator 测试台 `zipaxil_tb.cpp` 里的 `RAMBASE`（`1<<28`），这解释了为什么官方 axil 测试台**没有用 wbxbar 默认地址表**，而是直接把 CPU 接到内存模型（见 4.4 节）。

**若无法运行验证**：可只做纸面推导，结论一致即可。

#### 4.1.5 小练习与答案

**练习 1**：如果想让 `0x10000000` 起的 128 MiB 也归某个从设备，该改哪个参数？怎么改？

> **答案**：给从设备 1 把掩码从「高 4 位」改成「高 3 位」、标识改成 `001`，即让 `0x20000000` 那一档扩到 `0x20000000–0x3FFFFFFF`；或者新增一个从设备、设置 `SLAVE_ADDR = 0x10000000` 且 `SLAVE_MASK` 高 4 位为 `1111`。本质都是「让某个从设备的标识位匹配 `0001`」。

**练习 2**：`request[NS]`（第 NS 位）被拉高意味着什么？为什么 `addrdecode` 要专门留这一位？

> **答案**：表示一笔有效访问谁都没命中（地址落在「无人区」）。下游互连据此返回 bus error，避免访问悬空、CPU 永久等待。它把「地址错误」变成了一个可被 CPU 捕获的异常（见 u2-l5 的总线错异常位）。

**练习 3**：`o_decode` 为什么必须是独热码（`$onehot0`）？

> **答案**：若两个从设备同时被选中，两个从设备会同时驱动同一组总线数据线，造成电气冲突与数据错乱。地址表设计正确时，区间互斥，自然独热；形式化属性（[L355-L377](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/rtl/addrdecode.v#L355-L377)）会强制证明这一点。

---

### 4.2 总线互连 wbxbar：多主多从的十字路口

#### 4.2.1 概念说明

`addrdecode` 只能处理「一个主设备对多个从设备」。但真实的 SoC 往往有多个主设备：CPU 要访问内存，DMA 也要搬运数据，调试端口也要读写寄存器。如果两个主设备同时发起访问怎么办？

`wbxbar`（Wishbone crossbar）就是一个「十字路口」式的总线互连：它有 `NM` 个主设备端口、`NS` 个从设备端口，允许**任意主设备访问任意从设备**，并且尽量让「去往不同从设备的两笔访问」并行进行，只有当两个主设备抢同一个从设备时才需要仲裁。

它本身是一个 Wishbone B4 流水线规范 compliant 的互联。本节虽然讲的是 Wishbone 版本，但它内部正是用 4.1 节的 `addrdecode` 搭起来的——理解了它，你就理解了所有「crossbar」类互连的骨架。

#### 4.2.2 核心流程

`wbxbar` 在每个主设备端口一侧做三件事，再把结果仲裁到从设备端口：

```
对每个主设备 N：
  1. skidbuffer：把主设备的请求「打一拍」缓冲，吸收从设备侧的反压
  2. addrdecode：对缓冲后的地址做译码，得到 request[N][0..NS]（命中的从设备号）
  3. 仲裁：与「请求/授权」矩阵配合，决定主设备 N 这一拍能否拿到目标从设备

授权规则（priority + 通道占用）：
  - request[N][M]：主设备 N 想访问从设备 M
  - requested[N][M]：编号比 N 小的主设备是否也在抢 M（优先级来源）
  - grant[N][M]：主设备 N 被授权访问从设备 M
  - mgrant[N]：主设备 N 拿到了某个从设备通道
  - sgrant[M]：从设备 M 当前归某个主设备（记录是哪一个：sindex[M]）

在途交易计数：
  - 每个主设备维护 lclpending（已发未应答的笔数）
  - mempty / mnearfull / mfull 三档水位，mfull 时反压该主设备
  - LGMAXBURST 限制最大在途数，防止 crossbar「中途换道」

返回路径：
  - 从设备的 ack/数据按 sindex[M] 路由回正在占用它的那个主设备
  - 命中「无从设备」（grant[N][NS]）或超时 → 返回 bus error
```

性能特性（作者在文件头注明）：**每拍可完成一笔交易（吞吐），最小延迟约 3 拍**。

#### 4.2.3 源码精读

**性能与用法说明**。作者在文件头直接写明了「一拍一笔、三拍最小延迟」的指标和「设 NM/NS/SLAVE_ADDR/SLAVE_MASK 即可」的用法：

[wbxbar.v:L10-L24](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/rtl/wbxbar.v#L10-L24) — 性能指标与用法。这就是你集成 `wbxbar` 时最先要看的「说明书」。

**关键参数**。`NM`（主设备数）、`NS`（从设备数）、`SLAVE_ADDR`/`SLAVE_MASK`（直接下传给内部的 `addrdecode`）、`LGMAXBURST`（最大在途笔数的对数）、`OPT_TIMEOUT`（从设备无响应的超时阈值）：

[wbxbar.v:L83-L120](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/rtl/wbxbar.v#L83-L120) — 参数定义。注意 `OPT_TIMEOUT` 默认为 0（关闭），开启后超时会返回 bus error；`OPT_STARVATION_TIMEOUT` 则在「主设备长期拿不到总线」时也返回 bus error。

**「伪从设备」记录错误通道**。`LGNS = clog2(NS+1)`——加 1 就是为了容纳那个「无从设备命中」的错误通道（索引 NS）：

[wbxbar.v:L182-L184](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/rtl/wbxbar.v#L182-L184) — 注释说明这个「plus one」的伪从设备用于产生 bus error。

**每个主设备 = skidbuffer + addrdecode**。这段是「wbxbar 由 addrdecode 搭成」的直接证据，也是 4.1 节与 4.2 节的连接点：

[wbxbar.v:L256-L304](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/rtl/wbxbar.v#L256-L304) — 对主设备 N 先实例化 `skidbuffer`（缓冲请求、吸收反压），再实例化 `addrdecode`（译码），输出 `request[N]`。

**优先级仲裁**。`requested[N][M]` 逐级传递「编号更小的主设备是否在抢 M」，从而让低编号主设备天然优先：

[wbxbar.v:L328-L352](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/rtl/wbxbar.v#L328-L352) — 仲裁的优先级来源。注意它是「基础优先级仲裁」，低编号主设备先得。

**在途交易计数**。`lclpending` 记录每个主设备已发但未收到 ack/err 的笔数，`mempty/mnearfull/mfull` 是三档水位，`mfull` 时强制该主设备停顿（防止突发超过 `LGMAXBURST` 上限）：

[wbxbar.v:L999-L1032](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/rtl/wbxbar.v#L999-L1032) — 在途计数。`2'b10`（发了未应答）则 `+1`，`2'b01`（收到应答）则 `-1`。

**超时保护**。`OPT_TIMEOUT > 0` 时，若某主设备挂起的交易长期无响应，计数器耗尽即返回 bus error，避免 SoC 因某个坏外设而整体死锁：

[wbxbar.v:L1034-L1067](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/rtl/wbxbar.v#L1034-L1067) — 超时逻辑。

> **形式化验证**：`wbxbar` 末尾有一段相当长的 `FORMAL` 块（[L1404-L1717](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/rtl/wbxbar.v#L1404-L1717)），用「任意主设备对任意从设备任意地址读写任意值，都能正确读到写入的值」这条契约（`special_master`/`special_address`/`special_value`）来证明互连不会丢数据、不会串台。这正是 u5-l2 介绍的 SymbiYosys 方法学在互连上的应用。

#### 4.2.4 代码实践

**实践目标**：把 `wbxbar` 当成黑盒，学会配置它的参数。

**操作步骤**：

1. 假设你要做一个 2 主（CPU + DMA）× 4 从（RAM、ROM、UART、定时器）的 SoC。
2. 阅读文件头 [wbxbar.v:L17-L24](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/rtl/wbxbar.v#L17-L24) 的用法说明。
3. 写出实例化时的参数（这是「示例代码」，不在仓库中实际存在）：

   ```verilog
   // 示例代码：2 主 × 4 从的 Wishbone 互连
   wbxbar #(
       .NM(2),          // 主设备：CPU、DMA
       .NS(4),          // 从设备：RAM、ROM、UART、Timer
       .AW(32), .DW(32),
       .SLAVE_ADDR({ 32'hFF00_0000,  // 从设备 3：Timer  (高 8 位)
                     32'h0F00_0000,  // 从设备 2：UART   (高位自定掩码)
                     32'h0000_0000,  // 从设备 1：ROM
                     32'h1000_0000 });// 从设备 0：RAM
       .SLAVE_MASK({ 32'hFF00_0000, 32'h0F00_0000,
                     32'hF000_0000, 32'hF000_0000 }),
       .LGMAXBURST(6),  // 最多 63 笔在途
       .OPT_TIMEOUT(1023) // 超时保护
   ) mybus(...);
   ```

4. 对照 4.1 节，确认 `SLAVE_ADDR` 与 `SLAVE_MASK` 的位数是 `NS*AW = 4*32 = 128` 位，且拼接顺序是从设备编号**从高到低**（最左是 NS-1）。

**需要观察的现象**：当你把 `SLAVE_ADDR`/`SLAVE_MASK` 拼错位数或拼反顺序时，地址会命中错误的从设备（串台），表现就是「写 UART 却改了 RAM」。

**预期结果**：参数拼对后，CPU 访问 `0x10000000` 命中 RAM、访问 `0x0F000000` 命中 UART。**待本地验证**（需要 Verilator 或 iverilog 环境，将此实例化与从设备模型连起来跑）。

#### 4.2.5 小练习与答案

**练习 1**：两个主设备同时访问**不同**从设备时，`wbxbar` 会停顿其中一个吗？

> **答案**：不会。这正是 crossbar 相对「共享总线」的优势——只要目标从设备不同，两笔交易可并行进行（各自的 `grant` 互不冲突）。只有抢同一从设备时才仲裁。

**练习 2**：`mfull[N]` 拉高时会发生什么？为什么要这么设计？

> **答案**：`mfull` 表示主设备 N 的在途交易数已达上限（`LGMAXBURST`），此时强制该主设备停顿（`m_stall[N]=1`）。这是为了防止一个主设备发出超过互连/从设备容纳能力的长突发，导致它在 crossbar 里「中途换道」破坏数据一致性。

**练习 3**：为什么 `wbxbar` 在每个主设备入口都先放一个 `skidbuffer`？

> **答案**：AXI 注册输出或流水线从设备会带来 1 拍的应答延迟，没有 `skidbuffer` 的话这 1 拍停顿会一路反压回主设备。`skidbuffer`（见 u4-l4）把这 1 拍吸收掉，让互连在背压下仍能维持吞吐。

---

### 4.3 CPU 顶层 zipaxil 与软件地址映射 board.h

#### 4.3.1 概念说明

`zipaxil` 是 ZipCPU 的 AXI-Lite 顶层。你需要先建立一个关键认知：**`zipaxil` 自己不包含总线互连、不包含存储器、不包含外设**。它只是把内核的指令接口、数据接口、调试接口分别封装成三组对外端口，等「外面」（也就是你设计的 SoC）把它们连到存储器和外设上。

这与 u4-l3 讲的内容一致：`zipaxil` 把指令主端口 `M_INSN_*` 与数据主端口 `M_DATA_*` **物理分离**，外加一个调试从端口 `S_DBG_*`。换句话说，`zipaxil` 是 SoC 里「最核心的那块砖」，但它不是整栋楼。

而 `board.h` 是这块砖的「软件使用说明书」——它用 C 语言的 `volatile` 指针告诉程序「RAM 在哪个地址、UART 在哪个地址、调试寄存器在哪个地址」。**硬件地址映射（你给 wbxbar 设的 SLAVE_ADDR）与软件地址映射（board.h）必须一一对应**，否则程序访问的地址落不到正确的硬件上。

#### 4.3.2 核心流程

一个 ZipCPU 程序从「发起到产生效果」的全链路：

```
C 代码 printf("Hello")
   ↓ zip-gcc 编译
机器指令（含对 UART 寄存器的 SW）
   ↓ 加载到 RAM（地址由 board.h / board.ld 决定）
CPU 取指（M_INSN_* 主端口）→ 译码 → 执行到那条 SW
   ↓ 数据写（M_DATA_* 主端口）发出地址（如 0x02000000）
SoC 互连：addrdecode 判断 0x02000000 → 选中 UART
   ↓
UART 从设备接收数据字节 → 串口输出字符
```

这条链路里只要任何一段的「地址」对不上（链接脚本、board.h、硬件 SLAVE_ADDR 三者不一致），程序就会写到错误的地方，表现常常是「跑飞」「总线错误」「没有输出」。

#### 4.3.3 源码精读

**`zipaxil` 的参数**。这里你能看到 u5-l6 讲过的全部 `OPT_*` 综合期参数（`OPT_LGICACHE`/`OPT_LGDCACHE`/`OPT_MPY`/`OPT_DIV`/`OPT_CIS`/`OPT_USERMODE`…）以及 `RESET_ADDRESS`、`START_HALTED`：

[zipaxil.v:L51-L87](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxil.v#L51-L87) — 顶层参数。注意 `OPT_LGICACHE=0`/`OPT_LGDCACHE=0` 默认关闭缓存，`RESET_ADDRESS` 默认全 0（每次集成都应覆盖为你的 ROM/启动地址）。

**`zipaxil` 的对外端口**。三组：调试**从**端口 `S_DBG_*`（被外部调试器访问）、指令**主**端口 `M_INSN_*`（CPU 取指令）、数据**主**端口 `M_DATA_*`（CPU 读写数据）。注意指令端口是只读的（取指不写）：

[zipaxil.v:L105-L195](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/zipaxil.v#L105-L195) — 三组 AXI-Lite 端口。指令总线只有 AR/R 通道（读），数据与调试总线五通道齐全。这就是 u4-l3 所说的「指令/数据总线物理分离」。

**`board.h` 的外设结构体**。`CONSOLE`（UART）有 4 个寄存器，`SCOPE`（逻辑分析仪）有 2 个：

[board.h:L85-L101](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/zipsw/board.h#L85-L101) — `CONSOLE`（`u_setup`/`u_fifo`/`u_rx`/`u_tx`）与 `SCOPE`（`s_ctrl`/`s_data`）。这些字段顺序必须与硬件寄存器排列一致。

**`board.h` 的地址指针**。用 `volatile` 指针把固定地址绑定到结构体——这就是「软件地址映射」的实体：

[board.h:L129-L134](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/zipsw/board.h#L129-L134) — `_scope = 0x01000000`、`_uart = 0x02000000`、`_smp = 0x03000000`、`_axilp = 0xff000000`。其中 `_axilp` 指向 ZipSystem 风格的外设寄存器组（定时器/PIC/看门狗等，见 u4-l2/u4-l5），与 u4-l2 介绍的 `0xff000000` 段一致。

**UART 状态位的位掩码宏**。程序靠这些宏查询「发送忙/空闲」：

[board.h:L91-L93](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/zipsw/board.h#L91-L93) — `_uart_txbusy`/`_uart_txidle`。这承接 u1-l4 里 `_outbyte` 轮询发送的链路。

> **重要提醒（准确性）**：`board.h` 是一份「模板/示例」板级头文件。它与官方 Verilator 测试台 `zipaxil_tb.cpp` 的实际地址**并不完全相同**——例如 `zipaxil_tb.cpp` 把 RAM 放在 `RAMBASE = 1<<28 = 0x10000000`（[zipcpu_tb.cpp:L152-L155](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/zipcpu_tb.cpp#L152-L155) 同一套常量体系），而 `board.h` 的 `_scope` 占着 `0x01000000`。所以**当你构建自己的 SoC 时，必须让 `board.h`/`board.ld` 与你自己 SoC 的硬件地址表一致**，而不能照抄官方文件。

#### 4.3.4 代码实践

**实践目标**：建立「硬件地址 ↔ 软件地址」的一致性意识。

**操作步骤**：

1. 打开 [board.h:L129-L134](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/zipsw/board.h#L129-L134)。
2. 列出软件侧地址表：`_scope=0x01000000`、`_uart=0x02000000`、`_smp=0x03000000`、`_axilp=0xff000000`。
3. 假设你的硬件 `wbxbar` 把从设备 0 设为 RAM（`0x00000000` 起）、从设备 1 设为 ROM、UART 接在某一段。问：为了让 `printf` 经 `_uart->u_tx` 输出字符，硬件互连必须在哪个地址区间选中 UART？
4. 把 `CONSOLE` 结构体（[L85-L89](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/zipsw/board.h#L85-L89)）的 `u_tx` 字段偏移算出来：`u_tx` 是第 4 个 `unsigned`，偏移 = `3*4 = 12 = 0xC` 字节。所以「写一个字符到 UART」实际是写到地址 `0x02000000 + 0xC = 0x0200000C`。

**需要观察的现象**：如果你在硬件里把 UART 译码到了 `0x04000000`，但 `board.h` 仍写 `_uart = 0x02000000`，那么 `_uart->u_tx = 'H'` 这行 C 代码会写到 `0x02000000+0xC`——一个没有 UART 的地址，结果要么静默丢失、要么触发 bus error。

**预期结果**：能复述「软件指针地址 = 硬件译码地址」这一对应关系，并知道改硬件时必须同步改 `board.h`。**待本地验证**（在真实集成里，地址不一致的典型现象就是 u1-l4 提到的 `TEST BOMBED` 或断言失败）。

#### 4.3.5 小练习与答案

**练习 1**：`zipaxil` 的指令主端口为什么只有 AR/R 两个通道（读通道），没有 AW/W/B（写通道）？

> **答案**：取指是只读操作——CPU 永远不会「写指令内存」（自修改代码也是通过数据端口写）。所以指令总线省掉写通道，节省资源。数据端口与调试端口则五通道齐全。

**练习 2**：`board.h` 里 `_scope`、`_uart` 都是 `volatile`，为什么必须是 `volatile`？

> **答案**：这些指针指向硬件寄存器，其值会被硬件随时改变（如 UART 的 FIFO 状态位），且每次读写都有硬件副作用。`volatile` 阻止编译器把读写优化掉或缓存到寄存器里，保证每次访问都真正打到总线上。

**练习 3**：`RESET_ADDRESS` 参数与 `board.h` 有什么关系？

> **答案**：`RESET_ADDRESS` 是 CPU 复位后取第一条指令的地址，它必须落在「存放启动代码的 ROM/RAM」所在的那段地址上（也就是 `board.ld` 里 `.start` 段 / `_rom` 符号所在区间）。如果 `RESET_ADDRESS` 指向一个没有存储器或没有初始化的地址，CPU 一上电就会取到垃圾指令或触发 bus error。

---

### 4.4 把积木拼起来：最小 SoC 的互连、外设与协议边界

#### 4.4.1 概念说明

前三节我们分别学了译码器（4.1）、互连（4.2）和 CPU 顶层 + 地址映射（4.3）。现在要把它们拼成一块完整的 SoC。但拼之前必须直面一个**协议边界**问题——这是本讲最重要的实战提醒：

- `zipaxil` 的端口是 **AXI-Lite**（五通道：AW/W/B/AR/R，单笔传输）。
- 本讲的 `wbxbar`、`memdev`、`wbscope` 全是 **Wishbone** 接口。
- 两种协议的握手信号完全不同，**不能直接相连**。

仓库为此提供了两条自洽的集成路线：

| 路线 | CPU 顶层 | 互连 | 存储器 | UART | 调试分析 |
|------|---------|------|--------|------|---------|
| **A. 纯 Wishbone SoC** | `zipbones`（u4-l1） | `wbxbar` | `memdev` | WBUART（外置） | `wbscope` |
| **B. 纯 AXI-Lite SoC** | `zipaxil` | `axilxbar`（同源 AXI-Lite crossbar） | AXI-Lite RAM | `axilcon` | `axilscope` |

`axilxbar`（[sim/rtl/axilxbar.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/rtl/axilxbar.v)）的设计目标同样是「每拍一笔交易」，且它内部同样复用了 `addrdecode` 这个译码原语——所以你在 4.1 节学到的地址译码知识可以无缝迁移。`axilcon`（[sim/rtl/axilcon.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/rtl/axilcon.v)）是一个 AXI-Lite 控制台（UART）口，与 WBUART 接口一致。

> 仓库**没有**提供现成的 AXI-Lite↔Wishbone 桥（只有 `axi2axilite`/`axilite2axi` 这类 AXI↔AXI-Lite 宽窄桥）。所以最干净的工程做法是「整条链路用同一种协议」，不要混接。本讲其余部分以「积木组合」的视角讲解，并指出官方测试台的实际做法。

#### 4.4.2 核心流程

搭建一个最小可运行 SoC 的步骤：

```
1. 定地址表：ROM/RAM/UART/Scope 各分配一段互不重叠的地址
2. 选协议：CPU 是 AXI-Lite 还是 Wishbone？据此选互连族
3. 连互连：CPU 主端口 → crossbar 主端口；crossbar 从端口 → 各从设备
4. 挂从设备：ROM/RAM 用 memdev（或 AXI-Lite RAM）、UART、可选 wbscope
5. 对齐软件：把 board.h / board.ld 的地址改成与第 1 步一致
6. 设启动：RESET_ADDRESS 指向 ROM 里 _start 所在地址
7. 仿真验证：用 zip-gcc 编译 hello.c → 加载 → 看是否输出 + HALT 成功
```

#### 4.4.3 源码精读

**存储器从设备 memdev**。这是你能挂到 Wishbone 互连上的最简单 RAM/ROM，单笔流水线访问：

[memdev.v:L46-L63](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/rtl/memdev.v#L46-L63) — 参数 `LGMEMSZ`（地址位数，决定容量）、`OPT_ROM`（只读）、`HEXFILE`（初始化文件，可用于 ROM）；端口是标准 Wishbone 从端口（`i_wb_cyc/stb/we/addr/data/sel` → `o_wb_ack/data`）。

**逻辑分析仪 wbscope**。调试自定义 SoC 时，把要观察的内部信号接到 `i_data`，触发后 CPU 可回读一段波形——这是「没有真实示波器时的救命工具」：

[wbscope.v:L7-L16](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/rtl/wbscope.v#L7-L16) — 用途说明：环形缓冲 + 触发 + 触发后停若干拍，再由 CPU 经总线逐字回读。

[wbscope.v:L18-L31](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/rtl/wbscope.v#L18-L31) — 工作流程。它同时是个 Wishbone 从设备，软件侧对应 `board.h` 里的 `SCOPE` 结构体与 `SCOPE_*` 控制位（[board.h:L117-L123](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/zipsw/board.h#L117-L123)）。

**官方测试台的真实做法**。`zipaxil_tb.cpp` 没有用 crossbar，而是把 `zipaxil` 的指令/数据主端口各直连到一个 C++ 内存模型 `axilmemsim`（RAMBASE=`0x10000000`）：

[sim/verilator/zipaxil_tb.cpp:L67](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/zipaxil_tb.cpp#L67)（include `axilmemsim.h`）、常量见 `zipcpu_tb.cpp:L152-L155`（`RAMBASE=1<<28`）。这是一种「单从设备、无互连」的最简验证环境——用来验证 CPU 本身，而不是验证 SoC 互连。你要做自定义 SoC，才需要把 crossbar 加回去。

#### 4.4.4 代码实践（本讲综合实践见第 5 节）

**实践目标**：在纸上完成一个最小 Wishbone SoC 的「积木拼装清单」。

**操作步骤**：

1. 选路线 A（纯 Wishbone）：CPU 用 `zipbones`，互连用 `wbxbar`。
2. 定地址表（示例，与 4.2.4 一致）：

   | 从设备 | 地址区间 | 大小 | 模块 |
   |--------|---------|------|------|
   | RAM | 0x10000000–0x1FFFFFFF | 256 MiB（实际按 `LGMEMSZ` 取） | `memdev` |
   | ROM | 0x00000000–0x0FFFFFFF | 256 MiB（实际按需） | `memdev OPT_ROM=1` |
   | UART | 0x02000000 | 16 B（4 寄存器） | WBUART |
   | Scope | 0x01000000 | 8 B | `wbscope` |

3. 写出连接关系（示例代码）：
   - `zipbones` 的对外 Wishbone **主**端口 → `wbxbar` 的 `i_m*`（主设备 0）。
   - `wbxbar` 的 `o_s*[0..3]`（从设备端口）→ 分别接 `memdev`(RAM)、`memdev`(ROM)、WBUART、`wbscope`。
   - 调试从端口单独引出（不经过 `wbxbar`，由外部调试器/测试台驱动）。
4. 在 `board.h` 把 `_uart` 设为 `0x02000000`、`_scope` 设为 `0x01000000`，与上表对齐；在 `board.ld` 把 `_ram_image_start` 设为 `0x10000000`、`RESET_ADDRESS` 设为 `0x00000000`（ROM）。

**需要观察的现象**：用 `zip-gcc` 编译 `hello.c`、加载到 ROM/RAM、运行后，UART 应输出 `Hello`，最后 CPU 进入 HALT。

**预期结果**：能在纸上画出「CPU → wbxbar → {RAM/ROM/UART/Scope}」的拓扑，并能让 `board.h`/`board.ld`/`SLAVE_ADDR` 三者地址一致。完整 RTL 跑通属于**待本地验证**——本仓库未直接提供这个「zipbones+wbxbar+memdev+UART」的现成顶层，需要你按上述清单自行实例化（这正是「二次开发」的含义）。

#### 4.4.5 小练习与答案

**练习 1**：能不能把 `zipaxil`（AXI-Lite）的主端口直接接到 `wbxbar`（Wishbone）的主端口上？为什么？

> **答案**：不能。两者握手信号完全不同（AXI-Lite 是五通道 AW/W/B/AR/R，Wishbone 是 cyc/stb/ack/stall）。直接相连会导致信号语义错乱、永远收不到应答。正确做法是整条链路统一协议：要么用 `zipbones` + `wbxbar`（Wishbone），要么用 `zipaxil` + `axilxbar`（AXI-Lite）。

**练习 2**：为什么把 `memdev` 配成 `OPT_ROM=1` 后还要给它一个 `HEXFILE`？

> **答案**：ROM 不可写，其内容必须在综合/初始化时固化。`HEXFILE` 在初始化阶段把启动代码（`_start`、Bootloader）烧进 ROM 数组，CPU 复位后从 `RESET_ADDRESS`（落在 ROM）取到的才是真正的指令而不是全 0。

**练习 3**：调试一个自定义 SoC 时，`wbscope` 相比「用 printf 打印」有什么独特优势？

> **答案**：`printf` 本身要占用 CPU、走总线、依赖 UART，无法观测 CPU 卡死或总线死锁时的内部状态；`wbscope` 是纯硬件环形缓冲，独立于 CPU 运行，能在 CPU/总线异常时记录关键信号波形供事后分析——这正是「逻辑分析仪」的用途。

---

## 5. 综合实践：设计并验证一个最小 SoC

把本讲四个模块串起来，完成下面这个贯穿任务。

### 任务

设计一个最小 SoC：CPU 用 `zipaxil`，互连用 crossbar（`axilxbar`），地址译码把 ROM / RAM / UART 映射到不同地址段；写出完整的地址映射表，并在 `sim/zipsw/board.h` 中对应配置，最后说明如何用 Verilator 跑通 `hello.c`。

### 步骤

1. **选协议族**。因为指定用 `zipaxil`（AXI-Lite），所以互连选 `axilxbar`、UART 选 `axilcon`、RAM 选 AXI-Lite 内存模型（或参考 `axilmemsim`）。**说清楚为什么不能用 `wbxbar`**（见 4.4.5 练习 1）。

2. **定地址表**。参考 4.4.4，设计如下（示例）：

   | 从设备 | 基地址 | 大小 | 软件符号 |
   |--------|--------|------|---------|
   | ROM（启动代码） | 0x00000000 | 由 `LGMEMSZ` 定 | `_rom` / `RESET_ADDRESS` |
   | RAM（程序/数据） | 0x10000000 | 由 `LGMEMSZ` 定 | `_ram_image_start` |
   | UART（控制台） | 0x02000000 | 16 B | `_uart` |
   | （可选）Scope | 0x01000000 | 8 B | `_scope` |

   注意：`0x00000000` 段与 `0x10000000` 段在官方默认 `wbxbar` 地址表里分别对应从设备 0 与「空洞」（见 4.1.4），所以你**必须自定义** `SLAVE_ADDR`/`SLAVE_MASK` 让 ROM 与 RAM 各自命中、且 `0x10000000` 不再是无人认领的空洞。

3. **配置 `addrdecode`/`axilxbar`**。按第 2 步的表写出 `SLAVE_ADDR` 与 `SLAVE_MASK`（位数 = NS×32，拼接顺序从高编号到低编号）。验证 `0x10000000` 命中 RAM、`0x00000000` 命中 ROM、`0x0200000C`（`_uart->u_tx`）命中 UART。

4. **改 `board.h`**。把 `_uart` 改为 `0x02000000`、`_scope` 改为 `0x01000000`（若用了 Scope），其余外设指针按需增删。改 `board.ld` 使 `_ram_image_start = 0x10000000`、`_rom` 指向 `0x00000000`。

5. **设 CPU 参数**。`zipaxil` 的 `RESET_ADDRESS = 0x00000000`（指向 ROM 里 `_start`）。初次集成建议 `START_HALTED=1`、`OPT_DBGPORT=1`，以便用调试端口加载程序并单步（见 u5-l1、u5-l3）。

6. **仿真验证**。
   - 用 `zip-gcc -T board.ld` 编译 `sim/zipsw/hello.c`（参考 u1-l4）。
   - 参考 `sim/verilator/zipaxil_tb.cpp` 的结构：通过调试端口复位、设 PC=`0x00000000`、放行，再 `tick()` 循环。
   - 成功标志：串口输出 `Hello`，CPU 最终 HALT（u1-l4、u5-l3 讲过 HALT=成功、BUSY=失败）。

### 需要观察的现象

- 地址表拼错时（如 RAM 与 UART 区间重叠），现象是「写 UART 数据却破坏了 RAM」或触发 bus error。
- `board.h` 与硬件不一致时，`printf` 无输出或 `TEST BOMBED`。
- `RESET_ADDRESS` 指错时，CPU 一上电就取到全 0（在 ZipCPU 里被解释为某条指令）或 bus error。

### 预期结果

能在纸上给出：一张地址映射表、对应的 `addrdecode`/`axilxbar` 参数、修改后的 `board.h` 关键行、以及用 Verilator 跑 `hello.c` 的步骤清单。完整 RTL 闭环属于**待本地验证**（仓库未直接提供该顶层，需按本讲清单自行拼装——这正是「二次开发」的练习目的）。

> 提示：第一次跑通前，强烈建议先把 SoC 简化成「CPU + 单个 RAM、无 UART」，用 `zipcpu_tb.cpp` 那样的「HALT 即成功」判定先验证互连与地址映射正确，再逐个加入 UART、Scope，缩小排查范围。

## 6. 本讲小结

- **地址译码 `addrdecode`** 是一切总线互连的底层零件，用 `(addr ^ BASE) & MASK == 0` 把地址变成独热的从设备选中线；多出的一位 `request[NS]` 专门表示「无命中 → bus error」。
- **总线互连 `wbxbar`** 是 NM 主设备 × NS 从设备的 Wishbone crossbar，内部由 `skidbuffer` + `addrdecode` + 优先级仲裁 + 在途交易计数（`mempty/mfull`）+ 可选超时构成；不同从设备的访问可并行，同从设备才仲裁。
- **`zipaxil`** 是 AXI-Lite CPU 顶层，对外暴露分离的指令主端口（只读）、数据主端口（五通道）与调试从端口；它本身不含互连/存储/外设，是 SoC 里待连接的核心砖块。
- **`board.h`** 是软件侧地址映射，用 `volatile` 指针把外设绑定到固定地址；**硬件地址表（`SLAVE_ADDR`）与软件地址表（`board.h`/`board.ld`）必须一致**，否则程序跑飞或 bus error。
- **协议边界**：`zipaxil` 是 AXI-Lite，`wbxbar`/`memdev`/`wbscope` 是 Wishbone，不能直连；自洽路线是「`zipaxil`+`axilxbar`+`axilcon`」或「`zipbones`+`wbxbar`+WBUART」，避免混接。
- 官方 `zipaxil_tb.cpp` 走的是「CPU 直连单内存模型」的最简验证，**没有用 crossbar**；自定义 SoC 才需要把 crossbar 与多个从设备加回去——这正是二次开发的核心工作。

## 7. 下一步学习建议

- **动手拼一个真实 SoC**：按第 5 节的清单，用 `zipbones` + `wbxbar` + `memdev` + WBUART 实例化一个 Wishbone SoC 顶层，再用 Verilator 跑通 `hello.c`。这是把本手册全部知识内化的最佳方式。
- **深入外设**：参考 u4-l5/u4-l6，把 `icontrol`（中断控制器）、`ziptimer`（定时器）、`wbdmac`（DMA）挂到你的 SoC 上，练习中断与 DMA 传输。
- **形式化验证你的互连**：参考 u5-l2，用 `fwb_master`/`fwb_slave` 给你的 crossbar 与从设备写 `.sby` 证明，确保地址表无重叠、数据不串台。
- **阅读关联源码**：`sim/rtl/axilxbar.v`（AXI-Lite crossbar，对比 `wbxbar`）、`sim/rtl/axilcon.v`（AXI-Lite UART）、`rtl/zipbones.v`（Wishbone 顶层，对比 `zipaxil`），理解同一套设计思想在两种协议下的实现差异。
- **走向真实 FPGA**：综合一个最小配置（关 FPU/DCACHE/USERMODE，见 u5-l6）的 ZipCPU SoC 到一块 FPGA 板，把 UART 接到真实串口或 USB-串口，完成从「读源码」到「点亮硬件」的闭环。
