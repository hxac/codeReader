# util 工具 IP：FIFO、CDC、pack/unpack

## 1. 本讲目标

本讲聚焦 ADI HDL 数据通路中「不起眼但无处不在」的一类胶水 IP——`util_*` 系列工具模块。学完本讲，读者应该能够：

- 说清 `util_axis_fifo` 如何用一个 AXI-Stream FIFO 同时完成「弹性缓冲」与「跨时钟域（CDC）」两件事，并能区分它与 `util_cdc`（`sync_bits` / `sync_gray`）的关系。
- 解释 `util_cpack2` / `util_upack2` 如何把数据转换器输出的「多路窄通道（ENABLE/VALID/DATA）」与 DMA / 存储器期望的「一路宽总线（AXI-Stream）」互相转换。
- 区分 `util_wfifo` 与 `util_rfifo` 这一对「数据转换器风格 FIFO」分别用在采集（ADC）与回放（DAC）通路，并理解它们相对 `util_axis_fifo` 的差别。
- 对照 `library/axi_dmac/Makefile`，说明 axi_dmac 在 Xilinx / Intel / Lattice 三家下分别依赖了哪些 util 模块。

本讲承接 u5-l1（axi_dmac）。axi_dmac 负责把采样数据搬进/搬出 PS DDR，但它只是数据通路的「搬运工」；真正把数据转换器的窄接口适配成 DMA 能消费的宽接口、并把不同时钟域拼接起来的，正是本讲的 util IP。

## 2. 前置知识

阅读本讲前，建议先建立以下概念（部分已在前面讲义中出现）：

- **AXI-Stream（AXIS）协议**：用 `valid`/`ready` 握手、`data`/`tkeep`/`tlast` 携带载荷的一路流式接口。一次传输发生在 `valid && ready` 同时为高的时钟沿。
- **数据转换器通道接口（fifo_wr / fifo_rd 族）**：ADI 数据转换器 IP（如 axi_ad9361）并不直接出 AXIS，而是对每一路通道用三根线表达：`enable`（通道启用）、`valid`（本拍数据有效）、`data`（样本）。这是 u5-l2 讲过的「ENABLE/VALID/DATA 三线表达一路通道」。
- **跨时钟域（CDC, Clock Domain Crossing）**：当源端（写）与目的端（读）用不同时钟时，不能直接用一根多位总线传递，否则会采到亚稳态。常用做法是「两级寄存器同步 + 格雷码指针」。
- **FIFO 深度**：若地址位宽为 \(n\)，则 FIFO 容量为 \(2^n\) 个存储字。
- **ad_mem 双口 RAM**：library/common 下的厂商无关存储原语（u4-l4 已讲），有一个写口（clka/wea/addra/dina）和一个读口（clkb/reb/addrb/doutb），两个口可以接不同时钟。

一句话直觉：**util IP 就是数据通路上的「转接头 + 弹簧 + 变速箱」**——转接头负责接口形态转换（窄↔宽），弹簧负责吸收速率抖动（FIFO），变速箱负责对齐转速（CDC）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [library/util_axis_fifo/util_axis_fifo.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_axis_fifo/util_axis_fifo.v) | AXI-Stream FIFO 主体，支持同步/异步时钟、可选 tlast/tkeep、FWFT（首字直通）。 |
| [library/util_axis_fifo/util_axis_fifo_address_generator.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_axis_fifo/util_axis_fifo_address_generator.v) | 上面 FIFO 的地址/满空标志产生器，含格雷码 CDC。 |
| [library/util_cdc/sync_bits.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_cdc/sync_bits.v) | 单/多位「电平」同步器（两级寄存器）。 |
| [library/util_cdc/sync_gray.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_cdc/sync_gray.v) | 「计数器」同步器，用格雷码 + 多级寄存器安全跨域。 |
| [library/util_pack/util_cpack2/util_cpack2.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_pack/util_cpack2/util_cpack2.v) | 打包器：多路窄通道 → 一路宽 AXIS/fifo_wr。 |
| [library/util_pack/util_cpack2/util_cpack2_impl.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_pack/util_cpack2/util_cpack2_impl.v) | cpack2 的实现核，含交错、路由网络、 INTERFACE_TYPE 分支。 |
| [library/util_pack/util_pack_common/pack_shell.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_pack/util_pack_common/pack_shell.v) | cpack2/upack2 共用的「使能通道路由」状态机与多路选择网络。 |
| [library/util_pack/util_upack2/util_upack2.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_pack/util_upack2/util_upack2.v) | 解包器：一路宽 AXIS → 多路窄通道（cpack2 的反向）。 |
| [library/util_wfifo/util_wfifo.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_wfifo/util_wfifo.v) | 「写侧接数据转换器」的 FIFO，用于 ADC 采集通路。 |
| [library/util_rfifo/util_rfifo.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_rfifo/util_rfifo.v) | 「读侧接数据转换器」的 FIFO，用于 DAC 回放通路。 |
| [library/axi_dmac/Makefile](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/Makefile) | 演示一个真实 IP 如何在三个「依赖桶」里声明对 util 模块的依赖。 |
| [projects/fmcomms2/common/fmcomms2_bd.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl) | 把上述 util IP 串成完整采集/回放链的块设计脚本。 |

---

## 4. 核心概念与源码讲解

本讲按数据通路自下而上分三个最小模块：先讲承载握手与跨时钟域的 **AXIS FIFO + CDC 原语**（4.1），再讲窄↔宽形态转换的 **cpack2/upack2**（4.2），最后讲把数据转换器时钟域接入通路的 **wfifo/rfifo**（4.3）。

### 4.1 AXIS FIFO 与跨时钟域原语

#### 4.1.1 概念说明

`util_axis_fifo` 是一个标准的 AXI-Stream FIFO：输入侧（`s_axis_*`，source）和输出侧（`m_axis_*`，master）都用 AXIS 握手，中间用一块 RAM 做缓冲。它同时承担两个职责：

1. **弹性缓冲（弹簧）**：当上下游瞬时速率不一致时，FIFO 把多出来的数据暂存，避免反压丢数或拉空。
2. **跨时钟域（变速箱）**：写口（`s_axis_aclk`）和读口（`m_axis_aclk`）可以是两个完全独立的时钟。FIFO 内部用一块真双口 RAM 把两个时钟域隔开。

它之所以能做到安全的异步跨域，靠的是 `util_cdc` 这一族「同步原语」：

- `sync_bits`：同步「电平型」信号，标准做法是两级寄存器。
- `sync_gray`：同步「计数器型」多位信号，用格雷码保证每次只翻转一位。

> 直觉：跨时钟域的关键不是「加寄存器」，而是「保证被同步的信号在相邻两次采样间不会有多位同时翻转」。电平信号天然只有 0/1 一位；计数器必须先转成格雷码，才能满足这个条件。

#### 4.1.2 核心流程

异步 FIFO 的经典设计由「两个指针 + 跨域同步 + 满空比较」组成：

```
写侧(s_axis_aclk)            共享 RAM             读侧(m_axis_aclk)
   s_axis_valid ──┐         ┌──────────┐         ┌── m_axis_valid
   s_axis_ready ←─┤ 写口 ──→│  ad_mem  │←── 读口 ──┤→ m_axis_data
  waddr(二进制) ──┘         └──────────┘         └── raddr(二进制)
      │ b2g                                        │ b2g
      ↓ gray                                       ↓ gray
   [sync_gray] ──→ 读侧拿到 wptr_gray ──→ g2b ──→ 算 m_axis_empty
   读侧 rptr_gray ──→ [sync_gray] ──→ 写侧 ──→ g2b ──→ 算 s_axis_full
```

核心规则（地址比真实位宽多 1 位的「回绕位」）：

- **写指针** `waddr` 每写一拍加 1；**读指针** `raddr` 每读一拍加 1。两者都比地址位宽多 1 位，最高位是「回绕位」，用于区分「满」与「空」。
- 把指针转成**格雷码**后，再用 `sync_gray` 跨到对方时钟域。
- **满**：写指针与（同步过来的）读指针的高位不同、低位相同。
- **空**：读指针与（同步过来的）写指针完全相同。
- FIFO 深度为 \(2^{n}\)（\(n\) 为 `ADDRESS_WIDTH`）。

格雷码之所以安全，是因为二进制相邻两值在格雷码下恰好只差一位：

\[
g_{i} = b_{i+1} \oplus b_{i}, \qquad g_{\text{MSB}} = b_{\text{MSB}}
\]

这样跨域同步时，即便采样恰好落在翻转沿，最坏情况也只是采到「旧值或新值」二者之一，不会出现多位错乱。

`util_axis_fifo` 还实现了 **首字直通（FWFT, First-Word Fall-Through）**：FIFO 非空时 `m_axis_valid` 立刻拉高并保持，直到这一拍被 `m_axis_ready` 消费，用户不需要先发读请求再等一拍。

#### 4.1.3 源码精读

**（a）顶层参数与存储字宽。** `util_axis_fifo` 用一组参数描述 FIFO 形态：

[library/util_axis_fifo/util_axis_fifo.v:37-69](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_axis_fifo/util_axis_fifo.v#L37-L69) —— 声明 `DATA_WIDTH`、`ADDRESS_WIDTH`（决定深度）、`ASYNC_CLK`（异步/同步开关）、`TLAST_EN`/`TKEEP_EN`（是否随路存储边界信号）、`REMOVE_NULL_BEAT_EN`（丢弃全空字节拍）等。

一个巧妙之处是「存储字宽」会随可选信号动态拼接：

[library/util_axis_fifo/util_axis_fifo.v:71-74](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_axis_fifo/util_axis_fifo.v#L71-L74) —— `MEM_WORD` 在 `DATA_WIDTH` 基础上按需拼入 `tkeep` 与 `tlast`，这样把边带信号和数据一起存进同一个 RAM 字，省去单独的存储资源。

**（b）零深度退化与真 FIFO 两条分支。** 顶层用一个 `generate` 把实现分成两种：

[library/util_axis_fifo/util_axis_fifo.v:79-167](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_axis_fifo/util_axis_fifo.v#L79-L167) —— 当 `ADDRESS_WIDTH == 0` 时退化成「1 级流水线」（zerodeep），不是真正的 FIFO；它在异步模式下用两个 1 位的 `sync_bits` 同步写/读地址的翻转位（L90-106），用单寄存器存数据。

[library/util_axis_fifo/util_axis_fifo.v:228-360](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_axis_fifo/util_axis_fifo.v#L228-L360) —— `ADDRESS_WIDTH != 0` 才是真正的 FIFO，例化 `util_axis_fifo_address_generator` 产生地址与满空标志，并按 `ASYNC_CLK` 选择存储实现。

**（c）异步用 ad_mem，同步用行为 RAM。** 这正是「弹性缓冲 + 跨域」的落点：

[library/util_axis_fifo/util_axis_fifo.v:306-325](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_axis_fifo/util_axis_fifo.v#L306-L325) —— 异步时钟下，例化 library/common 的 `ad_mem` 真双口 BRAM：写口接 `s_axis_aclk`，读口接 `m_axis_aclk`，两个时钟域被这块 RAM 物理隔开。注释明确说明异步模式「无论请求多深都用 BRAM，以正确处理跨时钟域」。

[library/util_axis_fifo/util_axis_fifo.v:328-358](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_axis_fifo/util_axis_fifo.v#L328-L358) —— 同步时钟下，直接用行为级 RAM（`reg [...] ram[0:2**ADDRESS_WIDTH-1]`），让综合器自行推断分布式或块 RAM。

**（d）满空标志与格雷码 CDC。** 地址产生器是异步 FIFO 的灵魂：

[library/util_axis_fifo/util_axis_fifo_address_generator.v:152-170](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_axis_fifo/util_axis_fifo_address_generator.v#L152-L170) —— 在各自时钟域内用「(本侧指针 − 对侧同步指针)」算填充量：写侧据此判 `s_axis_full`/`s_axis_almost_full`，读侧据此判 `m_axis_empty`/`m_axis_valid`。指针比地址多 1 位，正是前述「回绕位」设计。

[library/util_axis_fifo/util_axis_fifo_address_generator.v:116-142](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_axis_fifo/util_axis_fifo_address_generator.v#L116-L142) —— 异步模式下，两个方向的指针各用一个 `sync_gray` 跨域；同步模式下则直接 `assign` 穿透（`m_axis_waddr_reg = s_axis_waddr_reg`），零开销。

**（e）两个 CDC 原语的差别。** 这是初学者最容易混淆的地方，务必对照源码看清楚：

[library/util_cdc/sync_bits.v:36-78](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_cdc/sync_bits.v#L36-L78) —— `sync_bits` 的头部注释点明：它用「2 个串联寄存器」同步，**虽然支持多位，但仅适用于「每个时钟周期最多翻转 1 位」的信号**（例如格雷计数器）；若 `ASYNC_CLK=0` 则直接旁路。

[library/util_cdc/sync_gray.v:36-111](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_cdc/sync_gray.v#L36-L111) —— `sync_gray` 面向「普通二进制计数器」：源域先用 `b2g` 转成格雷码（L91-97），目的域用两级寄存器同步（L105-106）后再 `g2b` 转回二进制（L107）。注释强调计数器每拍变化不得超过 ±1。

> 一句话总结：要同步「一个电平」用 `sync_bits`；要同步「一个会递增的计数器」用 `sync_gray`（它内部帮你做了二进制↔格雷码转换）。

#### 4.1.4 代码实践

**实践目标**：直观验证 FIFO 深度公式与异步/同步分支的选择。

**操作步骤（源码阅读 + 参数推演）**：

1. 打开 `util_axis_fifo.v`，找到参数 `ADDRESS_WIDTH`（L39）。假设某工程例化时设 `ADDRESS_WIDTH = 5`、`DATA_WIDTH = 64`、`ASYNC_CLK = 1`。
2. 计算预期深度：\(2^{5} = 32\) 个存储字，每字含 `tkeep` 时为 `MEM_WORD = 64 + 8 = 72` 位。
3. 跟着 `generate` 判断：因为 `ADDRESS_WIDTH != 0` 且 `ASYNC_CLK == 1`，综合器会走 L306-325 的 `ad_mem` 分支（BRAM），而不是 L328 的行为 RAM 分支。
4. 打开 `util_axis_fifo_address_generator.v` 的 L116-142，确认此时两个 `sync_gray` 都被例化；如果把 `ASYNC_CLK` 改成 0，这两段会被 `assign` 旁路。

**需要观察的现象 / 预期结果**：FIFO 容量 = `2^ADDRESS_WIDTH`；`ASYNC_CLK` 这个开关同时决定了「用 BRAM 还是行为 RAM」与「是否例化 CDC 同步器」——这两者是耦合的，因为异步跨域必须用双口 BRAM 隔离两个时钟。

> 本步骤为参数推演，无需上板；若要实测，可在 `library/util_axis_fifo` 下编写一个最小 testbench 驱动 `s_axis_*`、观察 `m_axis_level` 随填充量变化。**待本地验证**综合后实际推断的 RAM 类型。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `sync_bits` 的注释强调「最多 1 位变化」，却仍提供一个 `NUM_OF_BITS` 参数来同步多位？

**参考答案**：因为像「格雷计数器」这种多位总线，虽然位宽大于 1，但任意两个相邻值之间只有 1 位不同，因此每一根线都满足「单 bit 跨域」的安全条件，可以分别用两级寄存器同步。`NUM_OF_BITS` 只是把多个这样的单 bit 同步器打包在一起，并不代表它能同步任意多位总线。

**练习 2**：`util_axis_fifo` 在 `ASYNC_CLK=1` 与 `ASYNC_CLK=0` 下的存储实现有何不同？为什么？

**参考答案**：异步下例化 `ad_mem` 双口 BRAM，因为写口与读口时钟相互独立，必须用双口存储器在物理上隔离两个时钟域；同步下用行为级单时钟 RAM，让综合器自行推断分布式或块 RAM，省去双口的额外约束。

---

### 4.2 cpack2 / upack2：通道的打包与解包

#### 4.2.1 概念说明

数据转换器 IP（如 axi_ad9361）按「通道」产出/消费数据：4 路收发，每路一个 16 位样本，用 `enable`/`valid`/`data` 三线表达。但 DMA 引擎和 PS DDR 希望一次性搬一个**宽字**（如 64 位），以提升总线效率。于是需要一个「转接头」：

- **打包（pack）**：把 N 路窄样本拼成 1 个宽字。`util_cpack2` 用于 **ADC 采集**：把数据转换器送来的多路窄通道压成一路宽 AXIS/fifo_wr，喂给 DMA。
- **解包（unpack）**：打包的逆过程。`util_upack2` 用于 **DAC 回放**：把 DMA 送来的一路宽 AXIS 拆成多路窄通道，喂给数据转换器。

两者共享同一套路由逻辑（`pack_shell`），区别只在数据流向。

#### 4.2.2 核心流程

cpack2 的处理流程：

```
N 路 fifo_wr_data_* (每路 SAMPLE_DATA_WIDTH 位)
        │
        ▼
 ① REAL_NUM_OF_CHANNELS: 向上取整到 2 的幂（1/2/4/8/16/32/64）
        │
        ▼
 ② ad_perfect_shuffle: 把「通道分组」重排成「样本交错」（纯连线，0 资源）
        │
        ▼
 ③ pack_shell 路由网络: 只挑 enable=1 的通道，紧凑拼接
        │
        ▼
 ④ 输出一拍宽字:
      INTERFACE_TYPE=1 → packed_fifo_wr_data (喂 axi_dmac 的 fifo_wr 口)
      INTERFACE_TYPE=0 → m_axis_data (标准 AXIS)
```

关键设计点：

- **通道数向上取整到 2 的幂**：内部用 `REAL_NUM_OF_CHANNELS`，多出来的通道补 0，便于用规则的多路选择网络实现。
- **只搬运使能的通道**：`enable` 位决定哪些通道有效，路由网络会把有效通道紧凑地拼到输出宽字的低位（这要求一个「前缀和 + 旋转」的控制网络，由 `pack_shell` 实现）。
- **输出宽字位宽**：\(2^{\lceil\log_2 N\rceil} \times \text{SAMPLE\_DATA\_WIDTH} \times \text{SAMPLES\_PER\_CHANNEL}\)。
- **无反压**：cpack2 自身假设输出总能被消费（见源码注释），下游拥塞时通过 `overflow` 信号逐级回传。

upack2 是镜像：输入一拍宽 AXIS，按通道切片输出 N 路 `fifo_rd_data_*`。

#### 4.2.3 源码精读

**（a）cpack2 顶层：取整 + 转交实现核。**

[library/util_pack/util_cpack2/util_cpack2.v:38-44](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_pack/util_cpack2/util_cpack2.v#L38-L44) —— 参数：`NUM_OF_CHANNELS`、`SAMPLES_PER_CHANNEL`、`SAMPLE_DATA_WIDTH`、`INTERFACE_TYPE`。

[library/util_pack/util_cpack2/util_cpack2.v:183](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_pack/util_cpack2/util_cpack2.v#L183) —— 输出宽字位宽公式 `2**$clog2(NUM_OF_CHANNELS)*SAMPLE_DATA_WIDTH*SAMPLES_PER_CHANNEL`，即取整到 2 的幂再乘以每通道位宽。以 fmcomms2 的 4 通道 × 16 位为例，输出为 64 位。

[library/util_pack/util_cpack2/util_cpack2.v:198-203](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_pack/util_cpack2/util_cpack2.v#L198-L203) —— `REAL_NUM_OF_CHANNELS` 用嵌套三目运算把 `NUM_OF_CHANNELS` 向上取整到 1/2/4/8/16/32/64，多出的通道在内部补零。

[library/util_pack/util_cpack2/util_cpack2.v:290-315](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_pack/util_cpack2/util_cpack2.v#L290-L315) —— 顶层把 64 路输入按 `REAL_NUM_OF_CHANNELS` 截取后，例化 `util_cpack2_impl` 做真正的工作。

**（b）实现核：INTERFACE_TYPE 决定输出形态。**

[library/util_pack/util_cpack2/util_cpack2_impl.v:88-104](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_pack/util_cpack2/util_cpack2_impl.v#L88-L104) —— `INTERFACE_TYPE==1` 时输出走 `packed_fifo_wr_*`（与 axi_dmac 的 `fifo_wr` 口对接，fmcomms2 即此模式）；否则走标准 `m_axis_*`（带 `m_axis_keep` 全 1）。两种形态共享同一份打包数据，只是引出到不同端口。

[library/util_pack/util_cpack2/util_cpack2_impl.v:115-120](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_pack/util_cpack2/util_cpack2_impl.v#L115-L120) —— 注释明确「cpack 核本身没有反压，溢出只会发生在下游」，并把本侧 `fifo_wr_overflow` 直连到下游回传的 `packed_fifo_wr_overflow`。

[library/util_pack/util_cpack2/util_cpack2_impl.v:127-133](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_pack/util_cpack2/util_cpack2_impl.v#L127-L133) —— `ad_perfect_shuffle` 把「按通道分组」的数据重排成「按样本交错」的内部布局，注释说明这只是改连线、不耗资源。

[library/util_pack/util_cpack2/util_cpack2_impl.v:135-153](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_pack/util_cpack2/util_cpack2_impl.v#L135-L153) —— 例化 `pack_shell`（`PACK=1`）执行使能通道的路由拼接。

**（c）pack_shell：使能通道的路由网络（cpack/upack 共用）。**

[library/util_pack/util_pack_common/pack_shell.v:127-139](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_pack/util_pack_common/pack_shell.v#L127-L139) —— 单通道时退化成直通，无需任何控制。

[library/util_pack/util_pack_common/pack_shell.v:439-479](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_pack/util_pack_common/pack_shell.v#L439-L479) —— 多通道时，用一个由 4:1 与 2:1 多路器混合搭成的路由网络（`pack_network`），配合 `rotate`/`prefix_count` 控制信号，把使能通道紧凑拼到输出。注释解释了为什么混合使用两种多路器（4:1 网络级数减半、延迟更短）。

**（d）upack2：cpack2 的反向。**

[library/util_pack/util_upack2/util_upack2.v:38-43](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_pack/util_upack2/util_upack2.v#L38-L43) —— 参数与 cpack2 对称。

[library/util_pack/util_upack2/util_upack2.v:183](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_pack/util_upack2/util_upack2.v#L183) —— 输入侧 `s_axis_data` 是同一宽字公式。

[library/util_pack/util_upack2/util_upack2.v:191-196](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_pack/util_upack2/util_upack2.v#L191-L196) —— 同样的取整逻辑；之后例化 `util_upack2_impl`（内部同样用 `pack_shell`，但 `PACK=0`）。

[library/util_pack/util_upack2/util_upack2.v:235-300](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_pack/util_upack2/util_upack2.v#L235-L300) —— 把内部宽字按通道切片成 `fifo_rd_data_0..63`，高位补零到 64 路。

#### 4.2.4 代码实践

**实践目标**：动手验证 cpack2/upack2 的「窄↔宽」转换，并回答 axi_dmac 的 util 依赖。

**操作步骤**：

1. 读 `util_cpack2.v` 的 L183 与 L198-203：以 fmcomms2 配置（`NUM_OF_CHANNELS=4`、`SAMPLE_DATA_WIDTH=16`、`SAMPLES_PER_CHANNEL=1`）手算输出位宽，应得 64 位。
2. 读 `util_cpack2_impl.v` 的 L88-104，回答：fmcomms2 中 cpack2 默认 `INTERFACE_TYPE` 是多少（见顶层默认值 L43）？因此它输出到 `packed_fifo_wr_*` 还是 `m_axis_*`？
3. 读 `util_upack2.v` 的 L181-183，确认 upack2 的 `s_axis_data` 与 cpack2 的 `m_axis/packed_fifo_wr_data` 位宽公式一致——这正是二者能「背靠背」配对的原因。
4. 打开 `library/axi_dmac/Makefile`，回答本模块依赖了哪些 util：

[library/axi_dmac/Makefile:53-54](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/Makefile#L53-L54) —— Xilinx 侧通过 `XILINX_LIB_DEPS` 引用 `util_axis_fifo` 与 `util_cdc`（以「已打包 IP」形式跨库引用，见 u4-l1）。

[library/axi_dmac/Makefile:58-64](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/Makefile#L58-L64) —— Intel 侧通过 `INTEL_DEPS` **扁平嵌入** `util_axis_fifo.v`、`util_axis_fifo_address_generator.v`、`sync_bits.v`、`sync_event.v`、`sync_gray.v`。

[library/axi_dmac/Makefile:66-72](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/Makefile#L66-L72) —— Lattice 侧的 `LATTICE_DEPS` 在 Intel 基础上还多了 `../common/ad_mem.v`（因为 Lattice 的 util_axis_fifo 依赖它）。

**预期结果**：cpack2 把 4×16 打包成 64 位；axi_dmac 在 Xilinx 下「引用」util_axis_fifo + util_cdc 两个库 IP，在 Intel/Lattice 下「嵌入」对应的 .v 源文件（Lattice 还多带一个 ad_mem）。注意 axi_dmac **并不依赖 cpack2/upack2**——后两者由工程的块设计单独例化、与 DMA 并列连线（见 4.3 与综合实践）。

#### 4.2.5 小练习与答案

**练习 1**：如果 `NUM_OF_CHANNELS=6`、`SAMPLE_DATA_WIDTH=16`、`SAMPLES_PER_CHANNEL=1`，cpack2 输出宽字是多少位？内部实际处理几个通道？

**参考答案**：取整到 2 的幂得 `REAL_NUM_OF_CHANNELS=8`，输出位宽 \(8 \times 16 = 128\) 位；内部按 8 个通道处理，多出的 2 个通道补零（对应未使能通道）。

**练习 2**：cpack2 的 `fifo_wr_overflow` 是输入还是输出？它如何与下游联动？

**参考答案**：从顶层端口方向看 `fifo_wr_overflow` 是 output，但实现核里它被直接赋值为下游回传的 `packed_fifo_wr_overflow`（input）。也就是说 cpack2 本身不产生溢出，只是把下游 DMA 的溢出信号「透传」回上游，让上游（如 wfifo、数据转换器）知道该丢数或报告。

---

### 4.3 wfifo / rfifo：数据转换器时钟域的 FIFO

#### 4.3.1 概念说明

`util_axis_fifo` 处理的是 **AXIS 接口**的跨域与缓冲。但数据转换器那一侧用的是 **ENABLE/VALID/DATA 通道接口**（fifo_wr/fifo_rd 族），且常常需要把窄的数据转换器位宽拼宽后再进存储。ADI 为此提供了一对专用 FIFO：

- **`util_wfifo`**：用在 **ADC 采集通路**。数据转换器在 **写侧（din）**：数据从 `din_clk`（数据转换器时钟，如 ad9361 的 `l_clk`）写入，从 `dout_clk`（DMA 域，如 `divclk`）读出。命名里的 **w** 表示「数据转换器在 write 侧」。
- **`util_rfifo`**：用在 **DAC 回放通路**。数据转换器在 **读侧（dout）**：数据从 `din_clk`（DMA 域）写入，从 `dout_clk`（数据转换器时钟）读出。命名里的 **r** 表示「数据转换器在 read 侧」。

两者都基于 `ad_mem` 双口 RAM，既能做 **CDC**，又能做 **总线宽度变换**（`DIN_DATA_WIDTH` 与 `DOUT_DATA_WIDTH` 可不同），并各自报告一类异常：wfifo 关注 **溢出（ovf）**，rfifo 关注 **欠溢（unf）**——因为采集链怕「来不及读导致写满溢出」，回放链怕「来不及写导致读空欠溢」。

#### 4.3.2 核心流程

wfifo 的简化模型（ADC 采集）：

```
din_clk 域(数据转换器, 窄):              dout_clk 域(DMA, 宽):
  din_valid/din_data ──→ [宽度拼装:        ──→ [读控制:      ──→ dout_valid/dout_data
  (M_MEM_RATIO 个窄字     din_wdata]          dout_req_t 握手    (喂给 cpack2)
   拼成 1 个宽字)          ↓                   跨域同步)
                        ad_mem 双口 RAM
  ←── din_ovf (溢出回传给数据转换器)      ←── dout_ovf (来自下游 cpack2)
```

rfifo 是镜像（DAC 回放），方向反过来：DMA 域写宽字、数据转换器域读窄字，报告 `dout_unf`。

关键点：

- **M_MEM_RATIO = DOUT_DATA_WIDTH / DIN_DATA_WIDTH**：宽度比。每攒够 `M_MEM_RATIO` 个窄字才写一次宽字。
- **握手跨域**：用一个在两域间翻转的电平 `req_t`（toggle）通知对方「有一批数据可读/已写好」，这正是 `sync_bits` 能胜任的「单 bit 电平」跨域。
- **读带宽 ≥ 写带宽**：源码注释强调，该设计假设读侧比写侧快（或等宽），不做满空反压，而是用 ovf/unf 标志告知异常。

#### 4.3.3 源码精读

**（a）wfifo 的参数与宽度比。**

[library/util_wfifo/util_wfifo.v:38-44](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_wfifo/util_wfifo.v#L38-L44) —— 参数：通道数、`DIN_DATA_WIDTH`、`DOUT_DATA_WIDTH`、`DIN_ADDRESS_WIDTH`。

[library/util_wfifo/util_wfifo.v:107](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_wfifo/util_wfifo.v#L107) —— `M_MEM_RATIO = DOUT_DATA_WIDTH/DIN_DATA_WIDTH` 决定每几个窄字拼一个宽字。

[library/util_wfifo/util_wfifo.v:159-160](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_wfifo/util_wfifo.v#L159-L160) —— 注释点明约束：「读带宽 > 写带宽」「仅支持 dout_width ≥ din_width」。

[library/util_wfifo/util_wfifo.v:162-181](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_wfifo/util_wfifo.v#L162-L181) —— 当 `M_MEM_RATIO>1` 时，用移位寄存器把 `M_MEM_RATIO` 个窄字拼成宽字 `din_wdata`；等宽（`M_MEM_RATIO==1`）时直接透传。

[library/util_wfifo/util_wfifo.v:330-341](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_wfifo/util_wfifo.v#L330-L341) —— 用 `ad_mem` 双口 RAM 跨域：写口接 `din_clk`，读口接 `dout_clk`。

**（b）rfifo 的镜像结构。**

[library/util_rfifo/util_rfifo.v:38-44](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_rfifo/util_rfifo.v#L38-L44) —— 参数与 wfifo 同名同义。

[library/util_rfifo/util_rfifo.v:123](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_rfifo/util_rfifo.v#L123) —— 同样的 `M_MEM_RATIO` 定义。

[library/util_rfifo/util_rfifo.v:189-190](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_rfifo/util_rfifo.v#L189-L190) —— 注释同样强调「读带宽 ≥ 写带宽、仅 dout_width ≥ din_width」，因为 rfifo 的 din 是 DMA 侧（宽）、dout 是数据转换器侧（窄），宽→窄天然满足读快写慢。

[library/util_rfifo/util_rfifo.v:392-403](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/util_rfifo/util_rfifo.v#L392-L403) —— 同样用 `ad_mem` 双口 RAM 做跨域存储。

> wfifo 与 rfifo 的差异不在「内部结构」，而在「谁接数据转换器」：wfifo 的 din 接数据转换器（采集，怕溢出 ovf），rfifo 的 dout 接数据转换器（回放，怕欠溢 unf）。两者都依赖 `ad_mem` 与 `sync_bits` 风格的握手跨域。

#### 4.3.4 代码实践

**实践目标**：在真实工程中定位 wfifo/rfifo，确认它们的「数据转换器侧」与「DMA 侧」分别接哪个时钟。

**操作步骤**：打开 `projects/fmcomms2/common/fmcomms2_bd.tcl`，逐行追踪采集与回放两条链。

1. **采集链（ADC）—— wfifo**：

[projects/fmcomms2/common/fmcomms2_bd.tcl:94-98](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl#L94-L98) —— 例化 `util_wfifo util_ad9361_adc_fifo`，4 通道、16 位进 16 位出。

[projects/fmcomms2/common/fmcomms2_bd.tcl:99-115](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl#L99-L115) —— `din_clk` 接 `axi_ad9361/l_clk`（数据转换器时钟），`dout_clk` 接 `util_ad9361_divclk/clk_out`（DMA 域）；数据转换器的 `adc_enable/valid/data_i0..q1` 接到 wfifo 的 `din_*`，证实「数据转换器在写侧」。`din_ovf` 回连到 `axi_ad9361/adc_dovf`，把溢出报告给数据转换器。

2. **采集链——cpack2 + DMA**：

[projects/fmcomms2/common/fmcomms2_bd.tcl:119-133](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl#L119-L133) —— wfifo 的 `dout_data_$i` 喂给 `util_cpack2` 的 `fifo_wr_data_$i`，cpack2 再把 4×16 打包成 64 位 `packed_fifo_wr` 喂给 axi_dmac。

3. **回放链（DAC）—— rfifo + upack2**：

[projects/fmcomms2/common/fmcomms2_bd.tcl:158-178](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl#L158-L178) —— 例化 `util_rfifo`，`dout_clk` 接 `axi_ad9361/l_clk`（数据转换器侧），`din_clk` 接 `divclk`（DMA 域）；rfifo 的 `dout_data_$i` 喂给数据转换器的 `dac_data_*`，证实「数据转换器在读侧」。`dout_unf` 回连 `axi_ad9361/dac_dunf`。

[projects/fmcomms2/common/fmcomms2_bd.tcl:182-197](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl#L182-L197) —— DMA 的 `m_axis` 喂给 `util_upack2`（64 位拆成 4×16），再喂给 rfifo 的 `din_*`。

**需要观察的现象 / 预期结果**：整条采集链为 `axi_ad9361(l_clk) → wfifo(l_clk→divclk) → cpack2(4×16→64) → axi_dmac(→AXI-MM→DDR)`；回放链反向 `DDR → axi_dmac(AXIS 64) → upack2(64→4×16) → rfifo(divclk→l_clk) → axi_ad9361`。时钟域跨越被收敛在 wfifo 与 rfifo 这两处，cpack2/upack2 始终工作在 `divclk` 域。这与 u5-l2 给出的级联模板完全一致。

#### 4.3.5 小练习与答案

**练习 1**：为什么 fmcomms2 里 wfifo 的 `DIN_DATA_WIDTH` 与 `DOUT_DATA_WIDTH` 都设成 16（等宽），而不是直接用 wfifo 做宽度变换？

**参考答案**：因为该工程的「窄→宽」打包由下游的 `util_cpack2`（4×16→64）完成；wfifo 在这里只承担「l_clk → divclk」的跨时钟域职责，因此设成等宽即可。wfifo 的宽度变换能力是为别的拓扑（如直接把窄数据转换器位宽拼宽进存储）准备的。

**练习 2**：采集链用 wfifo 报告 `ovf`（溢出），回放链用 rfifo 报告 `unf`（欠溢）。为什么两者关注的方向相反？

**参考答案**：采集链是「数据转换器→DMA」，若 DMA 来不及读、FIFO 写满，新的采样无处可存就会溢出（ovf），所以要把溢出回传给数据转换器；回放链是「DMA→数据转换器」，若 DMA 来不及写、FIFO 被读空，数据转换器仍要求数就会读到无效数据（欠溢 unf），所以要把欠溢回传。两者都对应各自通路「最可能出问题」的那个方向。

---

## 5. 综合实践

**任务**：把本讲三类 util IP 串起来，画出 fmcomms2 工程从 RF 采样到 PS DDR、再从 DDR 回放的完整数据流向，并标注每一段的接口类型与时钟域。

**操作步骤**：

1. 重新通读 `projects/fmcomms2/common/fmcomms2_bd.tcl` 的 L88-217，把其中出现的 IP 实例列成一张表：`util_ad9361_divclk`、`util_wfifo`、`util_cpack2`、`axi_dmac`（ADC）、`util_rfifo`、`util_upack2`、`axi_dmac`（DAC）。
2. 为每个 IP 标注：① 它工作在哪个时钟域（`l_clk` 还是 `divclk`）；② 它的输入/输出是「通道接口（enable/valid/data 或 fifo_wr/fifo_rd）」还是「AXIS（valid/ready/data）」还是「AXI-MM」。
3. 画出两条链的框图：

   - **采集**：`axi_ad9361 ADC (l_clk, 4×16 通道) → util_wfifo (CDC l_clk→divclk) → util_cpack2 (4×16→64, fifo_wr) → axi_dmac (divclk, fifo_wr→AXI-MM) → PS DDR`
   - **回放**：`PS DDR → axi_dmac (divclk, AXI-MM→AXIS 64) → util_upack2 (64→4×16) → util_rfifo (CDC divclk→l_clk) → axi_ad9361 DAC (l_clk, 4×16 通道)`

4. 在框图上用不同颜色（或标记）标出「时钟域边界」——应当只有两处：wfifo 与 rfifo。这解释了 u5-l2 所说的「整链靠 util_ad9361_divclk 把跨时钟域压缩到这两处」。
5. 追踪异常信号回路：采集链的 `ovf` 如何从 DMA 一路回到数据转换器（`adc_dovf`）；回放链的 `unf` 如何回到数据转换器（`dac_dunf`）。

**预期结果**：你会得到一张清晰展示「转接头（cpack/upack）+ 弹簧/CDC（wfifo/rfifo、axis_fifo）」分工的图。核心结论：util IP 把数据转换器的「多路窄、独立时钟」世界，翻译成了 DMA/PS 的「单路宽、统一时钟」世界，让 axi_dmac 只需面对标准的 AXIS/AXI-MM 接口。**待本地验证**：若有 Vivado 环境，打开 fmcomms2/zcu102 的块设计，对照上述框图核对每个 IP 的实际参数与连线。

## 6. 本讲小结

- `util_axis_fifo` 是一个 AXI-Stream FIFO，同时承担弹性缓冲与跨时钟域：异步时用 `ad_mem` 双口 BRAM 隔离两个时钟，同步时用行为 RAM；满空标志由「回绕位指针 + 格雷码 CDC」计算，并提供 FWFT。
- `util_cdc` 是 CDC 原语族：`sync_bits` 用两级寄存器同步「最多翻转 1 位」的电平/格雷信号；`sync_gray` 面向普通二进制计数器，内部完成二进制↔格雷码转换后再多级同步。
- `util_cpack2` / `util_upack2` 是窄↔宽形态转换器：cpack2 把数据转换器的多路窄通道（ENABLE/VALID/DATA）紧凑打包成一路宽 AXIS/fifo_wr（ADC 采集），upack2 是其反向（DAC 回放）；通道数向上取整到 2 的幂，共享 `pack_shell` 路由网络。
- `util_wfifo` / `util_rfifo` 是「数据转换器风格」FIFO：wfifo 用于采集（数据转换器在写侧，报 ovf），rfifo 用于回放（数据转换器在读侧，报 unf），均基于 `ad_mem` 做 CDC 与可选宽度变换。
- 在真实工程 fmcomms2 中，采集链为 `ad9361 → wfifo → cpack2 → dmac → DDR`，回放链反向；时钟域跨越被收敛在 wfifo 与 rfifo 两处。
- `axi_dmac` 在 Xilinx 下以「库 IP」形式引用 `util_axis_fifo` + `util_cdc`，在 Intel/Lattice 下「扁平嵌入」对应 .v 源文件（Lattice 还多带 `ad_mem`）；它不依赖 cpack2/upack2（后者由块设计单独例化）。

## 7. 下一步学习建议

- **横向迁移到 JESD204 数据转换器**：u6-l1 将讲解 JESD204 框架。那里的 `tpl_adc` / `tpl_dac` 传输层同样会接 cpack2/upack2 与 dmac，本讲的级联模板可直接套用，对照阅读可加深理解。
- **深入 CDC 细节**：若对亚稳态与格雷码感兴趣，可继续阅读 `util_cdc/sync_event.v` 与 `sync_data.v`，它们处理「脉冲事件」与「多位数据」的跨域，是 `sync_bits`/`sync_gray` 的补充。
- **回看 axi_dmac 内部**：带着本讲对 `util_axis_fifo` 的理解，重读 u5-l1 中 axi_dmac 的 store-and-forward 缓冲与 burst 级 ID 跨域，你会发现 DMA 内部大量复用了本讲这些原语。
- **动手实践**：参考 `library/util_pack/tb/` 下的 `cpack_tb.v` / `upack_tb.v` / `underflow_tb.v`，跑一个最小仿真，观察打包/解包与欠溢行为（仿真流程参见 u8-l1）。
