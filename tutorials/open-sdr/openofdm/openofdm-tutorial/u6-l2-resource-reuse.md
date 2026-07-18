# 模块复用与资源优化

## 1. 本讲目标

OpenOFDM 的目标平台是 USRP N210 上的 **Spartan 3A-DSP** FPGA——这是一块 2007 年水平的芯片，BRAM 块、DSP48A 乘法硬核、LUT 都很有限。要在这样紧凑的器件里塞下一条完整的 802.11 OFDM 解码流水线，就必须在「复制粘贴一份实例」和「让多个模块共用一份资源」之间精打细算。

本讲集中回答一个问题：

> 「`dot11.v` 顶层到底有哪些资源是被多个子模块共享的？它用了哪几种共享手法？每种手法各自牺牲了什么、换来了什么？」

学完本讲你应该能够：

- 说清三种 FPGA 资源共享范式——**实例复制**、**双口 RAM 并行共享**、**分时复用（MUX）**——各自的适用场景与代价。
- 读懂 [`dot11.v`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v) 中 `rot_lut` 如何用一块双口 BRAM **同时**服务 `sync_long` 与 `equalizer` 两个消费者。
- 读懂 `phase` 模块如何用 `state == S_SYNC_SHORT` 这一个条件，在 `sync_short` 与 `equalizer` 之间**分时复用**同一条相位计算流水线。
- 理解 [`delayT`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/delayT.v) 这个不起眼的延时原语，为何是让「共享」得以成立的基石——它负责把 strobe 信号「错相位」地对齐到多级流水线的输出。
- 评估「取消 phase 分时复用、改用两个实例」会额外吃掉多少 FPGA 资源，并理解作者为什么对 `rot_lut`/`phase` 选择共享、对 `complex_mult` 却选择复制。

本讲承接 [u2-l3 中心频偏估计与相位/旋转校正](u2-l3-frequency-offset-correction.md)（那里首次提到 `phase` 被 `sync_short`/`equalizer` 分时复用、`rot_lut` 被双口共享）与 [u4-l1 dot11 顶层状态机](u4-l1-dot11-statemachine.md)（那里建立了 `state` 状态码与各阶段的时间先后），把「资源共享」从一句结论展开成可量化的工程取舍。

## 2. 前置知识

### 2.1 FPGA 里什么是「贵的」

软件里多 new 一个对象几乎免费，硬件里多例化一个模块则要付出实实在在的硅片面积。在 Spartan 3A-DSP 上，资源大致分三档：

| 资源 | 相对成本 | 典型用途 |
|------|----------|----------|
| LUT / 触发器（FF） | 廉价但量大 | 普通组合逻辑、状态机、寄存器 |
| BRAM 块（18 Kb / 36 Kb） | 昂贵，整片几十块 | 大容量查找表、缓存（FFT 输入、LTS、双口 RAM） |
| DSP48A 硬核 | 最稀缺，整片几十个 | 乘法器、乘加器（复数乘、除法迭代） |

OpenOFDM 里最吃资源的两样恰好是 **大容量查找表（BRAM）** 和 **复数乘除（DSP48A）**。本讲的核心就是：作者如何用共享手法把这两样的实例数压到最小。

### 2.2 三种共享范式

把一份资源给多个消费者用，一共有三种思路，理解它们的区别是本讲的骨架：

1. **实例复制（Replication）**：每个消费者各例化一份。最简单、彼此独立、可并发使用；但资源线性增长。适合「便宜」或「需要同时用」的资源。

2. **双口 RAM 并行共享（Dual-Port Sharing）**：一块 BRAM 天生有两个独立读写端口，给两个消费者各分一个端口。**两个消费者可以同一拍并发读不同地址**，互不干扰，且几乎不多花资源——因为 BRAM 物理上就是双口的。适合「存储类」资源。

3. **分时复用（Time-Division Multiplexing）**：一份「计算型」资源（一条流水线、一个除法器），在不同时刻轮流服务不同消费者。需要一个 **MUX** 在输入端选择「现在喂谁的数据」，输出端再分发给对应消费者。前提是：**几个消费者在时间上不重叠**。适合「计算类」资源，是三种里最省资源但约束最强的。

> 关键术语：**实例化（instantiation）**、**双口 RAM（dual-port RAM）**、**分时复用（TDM）**、**MUX（多路选择器）**、**消费者（consumer）**。

### 2.3 为什么共享离不开「数据 + strobe」握手

分时复用有一个隐含前提：被共享的模块必须能「被动等待」，且它的输出必须自带「这一拍的数据有效」标记。这正是 OpenOFDM 全项目统一的 **「数据 + strobe」单向握手**（见 u1-l4、u3-l2）的用意：

- 每个模块的输入都配一个 `input_strobe`，没有 strobe 就不处理。
- 每个模块的输出都配一个 `output_strobe`，下游只在 strobe 有效时才采信数据。

有了这套握手，`phase` 模块被 MUX 切到哪个消费者、什么时候切，都由 strobe 自然驱动——消费者没数据时 simply 不拉 strobe，共享模块就空转。**没有反压、没有握手往返**，分时复用才简单可靠。本讲第 4.3 节会看到，strobe 本身还要被 `delayT`「错相位」地延时，去匹配流水线内部的数据延迟。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [`verilog/dot11.v`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v) | 顶层「布线者」：所有共享实例都在这里例化，MUX 与输出分发也在这里用几行 wire 完成。 |
| [`verilog/coregen/rot_lut.v`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/rot_lut.v) | 旋转因子查找表的 Xilinx BRAM 封装，物理双口，是「并行共享」的载体。 |
| [`verilog/phase.v`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/phase.v) | 相位提取流水线（除法器 + atan 查表），是「分时复用」的被共享对象。 |
| [`verilog/usrp2/ram_2port.v`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/usrp2/ram_2port.v) | USRP 平台自带的双口 RAM 行为模型，理解 rot_lut 双口语义的参照。 |
| [`verilog/delayT.v`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/delayT.v) | 固定拍数延时链，让 strobe 与多级流水线数据对齐的「错相位」原语。 |

辅助参照（用于印证共享的必要性）：[`verilog/rotate.v`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rotate.v)（rot_lut 的两个消费者都通过它取旋转因子）、[`verilog/sync_short.v`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v)、[`verilog/sync_long.v`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_long.v)、[`verilog/equalizer.v`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v)（三者的端口暴露出谁在消费 rot_lut / phase）。

---

## 4. 核心概念与源码讲解

### 4.1 rot_lut：用一块双口 BRAM 并行服务两个消费者

#### 4.1.1 概念说明

`rot_lut` 是一张**旋转因子查找表**：输入一个定点相位地址，输出对应的 \((\cos\theta, \sin\theta)\)（拼成一个 32 位字，高 16 位 I、低 16 位 Q，见 [u2-l3](u2-l3-frequency-offset-correction.md) 与 [u5-l4](u5-l4-lut-generators.md)）。

需要这张表的消费者有两个，分别处于解码链路的不同阶段：

- **`sync_long`**：在 FFT **之前**，按粗频偏 \(\alpha_{ST}\) 逐样本旋转时域样本（CFO 校正），驱动它内部的 `rotate_inst`。
- **`equalizer`**：在 FFT **之后**，按导频残余频偏逐符号旋转整个 OFDM 符号（细 CFO 跟踪），驱动它内部的 `rotate_inst`。

`rotate.v` 模块本身只声明了 `rot_addr` / `rot_data` 两个端口（[rotate.v:16-17](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rotate.v#L16-L17)），它「不知道」表从哪来——表由上层 `dot11.v` 注入。于是 `dot11.v` 面对的选择是：给两个 `rotate_inst` 各配一块 `rot_lut`（复制），还是让它们共用一块（共享）？

作者选了**共享**，且用的是第 2.2 节里的第二种范式——**双口 RAM 并行共享**。原因在下一节揭晓。

#### 4.1.2 核心流程

`rot_lut` 的物理载体是一块 Xilinx **真双口（True Dual-Port）BRAM**：它有两个完全独立的端口 A 与 B，各自有自己的地址线与数据线，**同一拍可以读两个不同地址**，互不阻塞。于是共享的接线极其朴素：

```
                 ┌─────────────────────────────┐
sync_long_rot_addr ──► portA(addra) ──► doutA ──► sync_long_rot_data
                 │        rot_lut (BRAM)        │
   eq_rot_addr   ──► portB(addrb) ──► doutB ──►    eq_rot_data
                 └─────────────────────────────┘
```

- 端口 A 接 `sync_long` 的 `rot_addr`/`rot_data`。
- 端口 B 接 `equalizer` 的 `rot_addr`/`rot_data`。
- 两路读地址、两路读数据，各走各的线。

**为什么这里用双口共享而不是分时复用？** 因为 BRAM 天生就是双口的——用第二个端口「免费」，不额外占块。所以哪怕两个消费者在时间上偶尔重叠，双口也能并发服务；而分时复用在这里反而要额外引入 MUX 与调度约束，纯属自找麻烦。一句话：**对存储类资源，双口共享是无脑优选。**

> 关键术语：**真双口 RAM**、**端口 A / 端口 B**、**旋转因子**。

#### 4.1.3 源码精读

共享发生在 [`dot11.v`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v) 顶层，注释直白地写着 "Shared rotation LUT for sync_long and equalizer"：

[dot11.v:96-113](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L96-L113) —— 声明两路地址/数据线，例化**唯一一个** `rot_lut` 实例，A 口接 sync_long、B 口接 equalizer：

```verilog
////////////////////////////////////////////////////////////////////////////////
// Shared rotation LUT for sync_long and equalizer
////////////////////////////////////////////////////////////////////////////////
wire [`ROTATE_LUT_LEN_SHIFT-1:0] sync_long_rot_addr;
wire [31:0] sync_long_rot_data;

wire [`ROTATE_LUT_LEN_SHIFT-1:0] eq_rot_addr;
wire [31:0] eq_rot_data;

rot_lut rot_lut_inst (
    .clka(clock),
    .addra(sync_long_rot_addr),
    .douta(sync_long_rot_data),

    .clkb(clock),
    .addrb(eq_rot_addr),
    .doutb(eq_rot_data)
);
```

注意 `.clka(clock)` 与 `.clkb(clock)` 都接同一个 100 MHz 时钟（`common_clk`），但 A、B 两套地址/数据线完全独立。两路消费者把各自 `rotate_inst` 的 `rot_addr`/`rot_data` 端口连到这两组线上：

- [sync_long.v:158-174](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_long.v#L158-L174)：`sync_long` 内的 `rotate_inst` 把 `rot_addr`/`rot_data` 作为模块端口引到顶层，最终接到 `sync_long_rot_addr`/`sync_long_rot_data`（在 [dot11.v:309-310](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L309-L310) 完成连接）。
- [equalizer.v:209-225](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L209-L225)：`equalizer` 内的 `rotate_inst` 同样引出端口，接到 `eq_rot_addr`/`eq_rot_data`（在 [dot11.v:337-338](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L337-L338) 完成连接）。

再看被共享的 `rot_lut` 本体——[rot_lut.v:40-54](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/rot_lut.v#L40-L54) 暴露出 A、B 两套独立的地址/数据端口，这正是「真双口」的直接体现：

```verilog
module rot_lut(clka, addra, douta, clkb, addrb, doutb);
input clka;
input [8 : 0] addra;     // 端口 A 地址：9 位 → 512 深
output [31 : 0] douta;
input clkb;
input [8 : 0] addrb;     // 端口 B 地址：9 位 → 512 深
output [31 : 0] doutb;
```

地址宽度 `[8:0]` = 9 位，对应 \(2^9=512\) 深的 BRAM（[rot_lut.v:88](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/coregen/rot_lut.v#L88) 的 `C_READ_DEPTH_A(512)` 印证）。这与 `common_defs.v` 中 `ROTATE_LUT_LEN_SHIFT = ATAN_LUT_SCALE_SHIFT = 9`（[common_defs.v:1-6](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/common_defs.v#L1-L6)）严格一致，也与 [u5-l4](u5-l4-lut-generators.md) 讲过的 `gen_rot_lut.py` 中 `SIZE = 2^ceil(log2(402)) = 512` 对得上：实际有意义的表项只有 \(\lfloor\pi/4\cdot512\rfloor=402\) 项，向上凑到 512 是为了填满一个 9 位地址空间的 BRAM。

**对比参照**：`usrp2/ram_2port.v`（[ram_2port.v:20-66](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/usrp2/ram_2port.v#L20-L66)）是项目里手写的另一种双口 RAM（用于 LTS 缓存、符号输入缓存等），它用两个 `always @(posedge clk)` 块分别描述 A、B 两口（[ram_2port.v:50-65](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/usrp2/ram_2port.v#L50-L65)），同样体现「两口独立」的语义，可帮助理解 rot_lut 的双口行为。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「一块 rot_lut、两个消费者、各自独立读」的布线，并理解双口共享相对复制的资源收益。

**操作步骤**：

1. 在 `dot11.v` 中用搜索定位 `rot_lut_inst`，确认全顶层**只有这一个**实例。
2. 分别打开 `sync_long.v` 与 `equalizer.v`，各搜 `rotate_inst`，确认两者都把 `rot_addr`/`rot_data` 作为模块端口引到了顶层（即它们都不自己例化 rot_lut，而是「等」顶层喂表）。
3. 在 `dot11.v` 里追 `sync_long_rot_addr` 与 `eq_rot_addr` 这两根线，确认它们分别连到 `sync_long_inst`（[dot11.v:309-310](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L309-L310)）与 `equalizer_inst`（[dot11.v:337-338](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L337-L338)）。

**需要观察的现象**：rot_lut 的两个端口地址来自两个完全不同的模块（sync_long 与 equalizer），但指向同一块 BRAM。

**预期结果**：你会得到一张「1 块 BRAM → 2 个消费者」的图。若改回复制方案（给每个 `rotate_inst` 各配一块 rot_lut），BRAM 占用翻倍——而因为 BRAM 物理双口，共享方案**没有任何并发损失**。

> 「待本地验证」：双口 BRAM 在 Spartan 3A-DSP 上具体占几个 18 Kb/36 Kb 块，需结合综合报告（map report）确认；本实践只验证 RTL 层的共享结构。

#### 4.1.5 小练习与答案

**练习 1**：rot_lut 的地址是 9 位，但实际有意义的相位只覆盖 \([0, \pi/4)\)，对应 402 项。剩下 \(512-402=110\) 个地址存放的是什么？为什么还要保留它们？

**参考答案**：高地址区是填充项（生成脚本 `gen_rot_lut.py` 只写到有意义的相位，余下补 0 或沿用默认）。保留它们是为了让表深度凑到 \(2^9=512\)，正好填满一个 9 位地址空间的标准 BRAM 块——BRAM 的深度必须是 2 的幂才好映射到物理块，浪费 110 项远比搞一张非 2 幂深的表划算。

**练习 2**：如果把 rot_lut 换成单口 BRAM（只有一个读端口），还能用现在的接法吗？会发生什么？

**参考答案**：不能。单口 BRAM 同一拍只能读一个地址，而 sync_long 与 equalizer 的 `rotate_inst` 可能（在时间重叠时）同时要读——会冲突。单口方案要么退化为分时复用（加 MUX 轮流读，且必须保证两消费者时间不重叠），要么复制两块表。这正是作者选用真双口 BRAM 的原因：它用第二个读端口「免费」消解了冲突。

---

### 4.2 phase：用 state 信号分时复用一条计算流水线

#### 4.2.1 概念说明

`phase` 模块把一个复数 \(I+jQ\) 映射成定点相位 \(\theta=\angle(I+jQ)\)（放大 \(2^9\) 倍，详见 [u2-l3](u2-l3-frequency-offset-correction.md)）。它内部是一条**计算流水线**，不是存储：

- 一个 **`divider`**（封装 Xilinx `div_gen_v3_0` 除法器 IP，[phase.v:64-75](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/phase.v#L64-L75)）做 `min/max` 求 atan 的输入比值；
- 一张 **`atan_lut`**（256 深 × 9 位单口 ROM，[phase.v:85-89](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/phase.v#L85-L89)）查 atan 值；
- 若干比较器与象限还原逻辑。

它的消费者也有两个：

- **`sync_short`**：对 STS 延迟自相关结果 `prod` 求 atan，得到**粗频偏** `phase_offset`。
- **`equalizer`**：对 4 个导频的加权和 `pilot_sum` 求 atan，得到**残余频偏** `pilot_phase`（细 CFO 跟踪）。

问题来了：`phase` 是计算流水线，**没有「第二个端口」可以像 rot_lut 那样并发**。除法器同一拍只能算一个数。于是这里用第 2.2 节的第三种范式——**分时复用（MUX）**。

#### 4.2.2 核心流程

分时复用的核心是：**确认两个消费者在时间上绝不重叠**，然后用一个 MUX 在它们之间切换输入，输出再广播给两者。

幸运的是，OpenOFDM 的状态机天然保证了这种时间不重叠：

- `sync_short` 只在 `state == S_SYNC_SHORT`（状态码 1）期间运行，是包到达后最早的一段。
- `equalizer` 的相位计算发生在它内部的 `S_CALC_FREQ_OFFSET`，对应顶层 `state` 已进入 `S_DECODE_SIGNAL` / `S_DECODE_DATA` 等更靠后的阶段——此时 `sync_short` 早已完成。

因此顶层用一个简单到极致的 MUX——**条件就是 `state == S_SYNC_SHORT` 本身**——来选择 `phase` 的输入来自谁：

```
                 sync_short 的 (i,q,stb) ──┐
                                         ├─ MUX(state==S_SYNC_SHORT) ──► phase_inst ──┬─► phase_out
                   equalizer 的 (i,q,stb) ──┘                                          ├─► 给 sync_short
                                                                                      └─► 给 equalizer
```

- 输入端：3 个三目运算符 `? :` 分别选 `in_i` / `in_q` / `in_stb`。
- 输出端：`phase_out` / `phase_out_stb` 直接 **`assign` 广播**给两路消费者——因为同一时刻只有一路在发 strobe，也只有一路会去采信输出。

> 关键术语：**分时复用（TDM）**、**输入 MUX**、**输出广播**、**时间不重叠**。

#### 4.2.3 源码精读

`dot11.v` 里这段 "Shared phase module" 同样有显式注释，是全项目最值得逐行读的「共享范例」：

[dot11.v:118-160](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L118-L160) —— 声明两路消费者的输入/输出线，用 `state == S_SYNC_SHORT` 做 MUX，例化**唯一一个** `phase` 实例：

```verilog
////////////////////////////////////////////////////////////////////////////////
// Shared phase module for sync_short and equalizer
////////////////////////////////////////////////////////////////////////////////
wire [31:0] sync_short_phase_in_i, sync_short_phase_in_q;
wire        sync_short_phase_in_stb;
wire [31:0] sync_short_phase_out;
wire        sync_short_phase_out_stb;

wire [31:0] eq_phase_in_i, eq_phase_in_q;
wire        eq_phase_in_stb;
wire [31:0] eq_phase_out;
wire        eq_phase_out_stb;

// ★ 输入端 MUX：按当前状态选择把谁的数据喂给共享 phase
wire[31:0] phase_in_i = state == S_SYNC_SHORT?
    sync_short_phase_in_i: eq_phase_in_i;
wire[31:0] phase_in_q = state == S_SYNC_SHORT?
    sync_short_phase_in_q: eq_phase_in_q;
wire       phase_in_stb = state == S_SYNC_SHORT?
    sync_short_phase_in_stb: eq_phase_in_stb;

wire [31:0] phase_out;
wire        phase_out_stb;

// ★ 输出端广播：同一根 phase_out 同时连给两路消费者
assign sync_short_phase_out = phase_out;
assign sync_short_phase_out_stb = phase_out_stb;
assign eq_phase_out = phase_out;
assign eq_phase_out_stb = phase_out_stb;

phase phase_inst (
    .clock(clock), .reset(reset), .enable(enable),
    .in_i(phase_in_i), .in_q(phase_in_q), .input_strobe(phase_in_stb),
    .phase(phase_out), .output_strobe(phase_out_stb)
);
```

这个 MUX 的正确性依赖一个事实：`sync_short_enable` 本身就是 `state == S_SYNC_SHORT`（[dot11.v:165](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L165) `wire sync_short_enable = state == S_SYNC_SHORT;`）。也就是说，「sync_short 被使能」与「MUX 选 sync_short」用的是**同一个判定**，两者天然同步——sync_short 不工作时它不会发 strobe，MUX 同时也切到了 equalizer 侧。这是非常干净的时序对齐。

**两路消费者如何用共享 phase**：

- `sync_short` 把它的延迟自相关 `prod` 经两个 `moving_avg`（窗口 64）平均后送到 `phase_in_i`/`phase_in_q`（[sync_short.v:157-176](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v#L157-L176)），并取回 `phase_out` 取反后右移 4 位得到 `phase_offset`（[sync_short.v:218](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v#L218)、[sync_short.v:234](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v#L234)）。
- `equalizer` 把 4 个导频乘信道后的加权和 `pilot_sum_i`/`pilot_sum_q` 送到 `phase_in_*`（[equalizer.v:113-114](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L113-L114)），在 `pilot_count==4` 时拉 `phase_in_stb`（[equalizer.v:449-453](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L449-L453)），并取回 `phase_out` 存为 `pilot_phase`（[equalizer.v:458-462](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L458-L462)）。

**共享省下了什么**：被共享的 `phase` 内部最贵的两样——一个 `div_gen_v3_0` 除法器 IP（[phase.v:64-75](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/phase.v#L64-L75)）和一张 256×9 的 `atan_lut` ROM（[phase.v:85-89](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/phase.v#L85-L89)，注意它是单口 `C_READ_DEPTH_A(256)`）。分时复用让这两个昂贵资源在整个设计中**只出现一次**，却同时服务了两条频偏估计通路。

#### 4.2.4 代码实践

**实践目标**：评估「取消 phase 分时复用、改用两个独立 phase 实例」的资源代价，并理解作者为何对 phase 选共享、对 complex_mult 却选复制。

**操作步骤**：

1. 在 `phase.v` 里盘点 `phase_inst` 内部用到的昂贵资源：一个 `divider`（→ `div_gen_v3_0`）、一个 `atan_lut`（256×9 ROM）。这是「一份 phase」的成本。
2. 在仓库内全局搜索 `phase phase_inst` 或 `phase_inst`，确认全顶层只有 [dot11.v:148](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L148) 这一个实例。
3. 作为对照，全局搜索 `complex_mult`（`complex_multiplier` IP 的封装），数一下全设计共有几个实例：`sync_short` 有 1 个 `delay_prod_inst`（[sync_short.v:118](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/sync_short.v#L118)）；`equalizer` 有 3 个（`pilot_inst`、`input_lts_prod_inst`、`lts_lts_prod_inst`，见 [equalizer.v:195-251](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/equalizer.v#L195-L251)）。

**需要观察的现象**：`phase` 全设计 1 份（共享），`complex_mult` 全设计多份（复制）。

**预期结果 / 资源账本**：

| 资源 | 现状 | 若取消 phase 分时复用（×2 实例）的增量 |
|------|------|------------------------------------------|
| `phase` 实例 | 1（共享） | +1 |
| 其内 `div_gen_v3_0` 除法器 IP | 1 | **+1 个除法器 IP**（主要成本） |
| 其内 `atan_lut`（256×9 单口 ROM） | 1 | +1 张小 ROM（不足一个完整 BRAM 块） |
| 顶层 MUX（3 个三目） | 需要 | 可去掉（省一点 LUT，可忽略） |

也就是说，取消 phase 的分时复用会**额外多吃一个除法器 IP**（外加一张小 ROM）。`div_gen_v3_0` 在 Spartan 3A-DSP 上是迭代式除法，会占用 DSP48A 硬核或较多 LUT（具体取决于 IP 配置的流水级数与并行度）。

> 「待本地验证」：精确的 DSP48A / LUT 增量需用 Xilinx 综合工具跑两版（共享版 vs 双实例版）对比 map 报告；iverilog 只能做行为仿真，给不出资源数。

**为何 complex_mult 反而选择复制？** 因为 `complex_mult` 的消费者们（sync_short 的延迟自相关、equalizer 的导频乘、信道乘、LTS 自乘）**在同一个 OFDM 符号处理期间需要并发使用**——它们在同几拍里都要算复数乘。时间重叠 → 无法分时复用；而单个 `complex_mult` 仅占 1 个 DSP48A（相对廉价），复制几份比设计一套复杂的调度更划算。这与 phase 形成鲜明对照：**「时间不重叠 + 资源昂贵」→ 共享；「时间重叠 + 资源相对廉价」→ 复制。**

#### 4.2.5 小练习与答案

**练习 1**：phase 的输出端用 `assign` 直接广播给 sync_short 和 equalizer 两路，为什么不会出问题（比如一个消费者读到属于另一个消费者的结果）？

**参考答案**：因为「数据 + strobe」握手。同一时刻只有一路消费者在发 `input_strobe`（由 `state==S_SYNC_SHORT` 的 MUX 保证），所以 phase 的 `output_strobe` 也只会在对应那一路的处理延迟后有效；另一路此时不拉 strobe，即便 `phase_out` 线上有值，它也不会采信。广播的是「裸数据线」，语义由各自的 strobe 把关。

**练习 2**：MUX 的判定用的是 `state == S_SYNC_SHORT`，而 equalizer 的 `phase_in_stb` 是在它内部 `S_CALC_FREQ_OFFSET` 才拉高。顶层 `state` 此时不等于 `S_SYNC_SHORT`，MUX 选的是 equalizer 侧——这两套状态机是怎么对上的？

**参考答案**：顶层 `dot11.v` 的 `state` 是「全局控制状态」，它走到 `S_DECODE_SIGNAL`/`S_DECODE_DATA` 时才会把 `equalizer_enable` 拉高（见 [dot11.v:524](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L524)），equalizer 内部子状态机才开始运转并最终进入它的 `S_CALC_FREQ_OFFSET`。所以「顶层 state ≠ S_SYNC_SHORT」与「equalizer 正在算频偏」在时间上是一致的——MUX 的判定与 equalizer 的使能同源，自然对齐。

---

### 4.3 delayT：让共享成立的「错相位」原语

#### 4.3.1 概念说明

共享模块（尤其像 `phase` 这种多级流水线）有一个绕不开的问题：**数据要在流水线里走若干拍才出结果，可「这一拍输入有效」的 strobe 信号却只是个 1 拍的脉冲。** 如果不处理，strobe 早就过去了，数据才姗姗来到输出端——两者错位，下游就会在错误的时刻采信。

[`delayT`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/delayT.v) 就是解决这个问题的「错相位」原语：它是一个**参数化的固定拍数延时链**，把一个信号（通常是 strobe，或一组需要对齐的数据）整体后移 N 拍，让「延时后的 strobe」与「延时后的数据」重新对齐。

它和 u3-l2 讲过的 `delay_sample`（按样本推进的延时）不同：`delayT` 是**每个时钟无条件移一位**，纯粹做周期级对齐，不关心 sample strobe。这使得它非常适合「把 strobe 拖到和某段流水线输出同相位」。

> 关键术语：**流水线延迟（pipeline latency）**、**strobe 对齐**、**错相位（phase staggering）**、**延时链（delay line）**。

#### 4.3.2 核心流程

`delayT` 的实现极其简单——一个深度为 `DELAY` 的移位寄存器：

```
data_in ──► [reg0] ──► [reg1] ──► ... ──► [reg DELAY-1] ──► data_out
```

每个上升沿，所有寄存器整体右移一位；`DELAY` 拍之后，输入出现在输出。把一个 strobe 喂进去，它就晚 `DELAY` 拍出来——正好匹配某段流水线的延迟拍数。

在共享场景里，`delayT` 的用法是：**被共享模块自带它的内部 delayT 对齐**，所以无论它被 MUX 切给哪个消费者，输出 strobe 永远与输出数据对齐——消费者拿到的是「即插即用」的、握手已对齐的模块。这是 `phase` 能被无痛分时复用的底层支撑。

#### 4.3.3 源码精读

[`delayT.v:1-32`](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/delayT.v#L1-L32) —— 全模块就是一个移位寄存器数组：

```verilog
module delayT #(parameter DATA_WIDTH = 32, parameter DELAY = 1) (
    input clock, input reset,
    input  [DATA_WIDTH-1:0] data_in,
    output [DATA_WIDTH-1:0] data_out
);
reg [DATA_WIDTH-1:0] ram[DELAY-1:0];
assign data_out = ram[DELAY-1];          // 取最末级
always @(posedge clock) begin
    if (reset) begin /* 清零 */ end
    else begin
        ram[0] <= data_in;
        for (i = 1; i < DELAY; i = i+1)   // 整体移位
            ram[i] <= ram[i-1];
    end
end
endmodule
```

它在共享模块内部「错相位」的几个典型用法，每个都对应一段已知延迟：

1. **phase.v：把象限信息延时 36 拍，匹配除法器延迟。** `phase` 内部的 `divider` 是 36 拍流水（[divider.v:24-29](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/divider.v#L24-L29) 的 `delayT #(.DELAY(36))` 印证）。于是算除法**之前**确定的「象限」`quadrant`，必须延时 36 拍才能与除法**之后**的 `atan_data` 同时出现——[phase.v:77-83](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/phase.v#L77-L83) 用 `delayT #(.DATA_WIDTH(3), .DELAY(36))` 做这件事。另有两组 `DELAY(2)` 的小 delayT（[phase.v:45-51](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/phase.v#L45-L51)、[phase.v:55-61](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/phase.v#L55-L61)）分别对齐 div 输入与最终输出的 strobe。

2. **complex_mult.v：strobe 延时 5 拍，匹配 IP 流水深度。** `complex_mult` 内部封装的 `complex_multiplier` IP 有固定的流水级数，于是把输入 `input_strobe` 用 `delayT #(.DELAY(5))` 后移 5 拍得到 `output_strobe`（[complex_mult.v:39-45](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/complex_mult.v#L39-L45)），与乘积数据同步。

3. **rotate.v：输入数据与 strobe 各延时 4 拍，匹配象限计算 + LUT 读。** `rotate` 在 4 拍里完成「折叠相位 → 查 rot_lut → 象限还原」，于是用两个 `delayT #(.DELAY(4))` 把输入样本和 strobe 同步后移（[rotate.v:53-67](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/rotate.v#L53-L67)）。

4. **dot11.v：顶层把 equalizer 输出整体延时 6 拍。** [dot11.v:347-353](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L347-L353) 例化了 `delayT #(.DATA_WIDTH(33), .DELAY(6))`，把 `{equalizer_out_strobe, equalizer_out}` 后移 6 拍，供 `S_HT_SIGNAL`（[dot11.v:643-646](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L643-L646)，做 90° 旋转识别 HT-SIG）与 `S_DECODE_DATA`（[dot11.v:785-787](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/dot11.v#L785-L787)）使用，而非延时版则用于 `S_DECODE_SIGNAL`/`S_DETECT_HT`。这是用 delayT 在「同一份 equalizer 输出」上制造出两种不同时序的副本，匹配下游不同处理路径。

> 「待本地验证」：`DELAY(6)` 这个具体拍数来自 equalizer 内部 rotate+normalize 相对直接输出多出的流水级差，精确推导需在波形上对比 `equalizer_out_strobe` 与下游采信时刻。

#### 4.3.4 代码实践

**实践目标**：亲手验证 delayT「把 strobe 错相位」的行为，并理解它在共享模块里的对齐作用。

**操作步骤**：

1. 读 [phase.v:77-83](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/phase.v#L77-L83)，确认 `quadrant` 走的是 `DELAY(36)` 的 delayT；再读 [divider.v:24-29](https://github.com/open-sdr/openofdm/blob/2f0e0ba95385affa426dc11eba6effab37b8778b/verilog/divider.v#L24-L29)，确认除法器输出 strobe 也是 `DELAY(36)`——两者相同不是巧合，而是刻意对齐。
2. 在 `dot11_tb.v` 仿真波形里（参考 [u5-l3](u5-l3-testbench.md) 跑仿真），把 `phase_inst` 的 `input_strobe` 与 `output_strobe` 都拉出来，测量它们之间的拍数差。
3. 思考实验：如果把 phase.v 里那个 `DELAY(36)` 改成 `DELAY(35)`，会发生什么？

**需要观察的现象**：`input_strobe` 与 `output_strobe` 之间有固定的拍数间隔（phase 总延迟约 40 拍 = 2 + 36 + 2）。

**预期结果**：strobe 间隔稳定。若把 `DELAY(36)` 改成 35，象限信息会**提前一拍**到达，与除法结果错位，`phase` 输出的相位会落在错误的象限还原分支上——`phase_offset` / `pilot_phase` 全错，下游频偏校正失效，整包解码失败（FCS 不过）。这正说明 delayT 的对齐拍数是「硬约束」，改一个常数就会沿调用链崩坏。

> 「待本地验证」：上述「改 35 即失败」是按流水线对齐原理推断的预期，建议本地改常数后用 `make simulate` + `test.py` 交叉验证确认（见 [u5-l2](u5-l2-cross-validation.md)）。

#### 4.3.5 小练习与答案

**练习 1**：`delayT` 和 u3-l2 讲过的 `delay_sample` 有什么本质区别？为什么 phase/rotate 内部用的是 delayT 而不是 delay_sample？

**参考答案**：`delayT` 每个时钟上升沿无条件移位一次，延时量是「时钟周期数」；`delay_sample` 只在 `input_strobe` 有效时才推进，延时量是「样本数」（受 5:1 采样节拍影响）。phase/rotate 内部要对齐的是**计算流水线的时钟周期延迟**（除法 36 拍、乘法 5 拍），与样本节拍无关，所以必须用按时钟推进的 delayT。

**练习 2**：`delayT` 的 `ram` 数组在综合后会映射成什么？大量使用会不会很贵？

**参考答案**：`DELAY` 较小时，`ram` 数组综合成一组触发器（FF），每个延迟拍占 `DATA_WIDTH` 个 FF。所以 `delayT #(.DATA_WIDTH(3), .DELAY(36))` 约占 \(3\times36=108\) 个 FF——廉价。只有当 `DATA_WIDTH×DELAY` 很大时才会考虑改用 BRAM 实现，但 OpenOFDM 里的 delayT 延时量都不大（最大 36），用 FF 足矣。

---

### 4.4 共享关系总图与资源账本

把前三节的事实汇总，OpenOFDM 顶层的「共享 vs 复制」全貌如下：

| 资源 | 消费者 | 共享手法 | 全设计实例数 | 选择的理由 |
|------|--------|----------|--------------|------------|
| `rot_lut`（512×32 BRAM） | sync_long、equalizer | **双口并行共享** | 1 | BRAM 物理双口，第二端口免费，无并发损失 |
| `phase`（divider+atan 流水线） | sync_short、equalizer | **分时复用（MUX by state）** | 1 | 计算型无法双口；两消费者时间不重叠 |
| `complex_mult`（DSP48A） | sync_short、equalizer×3 | **实例复制** | 4 | 两消费者需并发；单个仅占 1 DSP，复制更简单 |
| `divider`（除法器 IP） | phase（共享）、equalizer×2 | 混合：phase 内 1 个被共享，equalizer 另有 2 个 | 3 | equalizer 的两路除法需并发，无法共享 |

**两条设计规律**：

1. **存储类（BRAM）→ 优先双口共享**，因为第二个端口近乎免费。
2. **计算类（流水线/除法器）→ 看消费者是否时间重叠**：不重叠就分时复用（省一份），重叠就复制（除非资源紧张到必须设计调度）。

`delayT` 则是让以上所有共享成立的基础设施——它把每个共享模块的 strobe 与数据在内部就对齐好，使模块可以被「无知觉」地 MUX 或双口分发。

---

## 5. 综合实践

**任务**：为 OpenOFDM 顶层绘制一张「资源共享关系图」，并做一份「取消 phase 分时复用」的资源影响评估。

**步骤**：

1. 在 `dot11.v` 中遍历所有子模块实例（`power_trigger_inst`、`sync_short_inst`、`sync_long_inst`、`equalizer_inst`、`ofdm_decoder_inst`、`crc_inst`、`fcs_inst`、`rot_lut_inst`、`phase_inst`、`eq_delay_inst`），标注每个实例是「独占」还是「被共享」。
2. 对被共享的 `rot_lut_inst` 与 `phase_inst`，画出它们的两个消费者，以及共享手法（双口 / MUX）。
3. 全局搜索 `complex_mult`、`divider`、`ram_2port`，统计各自的实例总数，填入第 4.4 节的资源账本表。
4. 撰写一段评估：「如果把 `phase_inst` 改成两个独立实例（sync_short 与 equalizer 各一个），需要删除顶层 MUX、新增哪些 IP / ROM，预估增加多少资源」。

**预期产出**：

- 一张共享关系图（手绘或文本框图均可），清晰呈现「1 rot_lut → 2 消费者（双口）」「1 phase → 2 消费者（分时）」两条主线，以及 complex_mult 的复制分布。
- 一份资源影响清单：取消 phase 共享 → +1 个 `div_gen_v3_0` 除法器 IP + 1 张 256×9 `atan_lut` ROM − 3 个三目 MUX；净增主要是一个除法器（精确 DSP48A/LUT 数待综合确认）。

> 这是「源码阅读型实践」，不需要修改源码或跑综合；若条件允许，可把两版 RTL 各跑一次 Xilinx 综合，用 map 报告填充账本里的精确资源数。

---

## 6. 本讲小结

- FPGA 资源共享有三种范式：**实例复制**（最简单、可并发、费资源）、**双口 RAM 并行共享**（存储类优选、第二端口免费）、**分时复用 MUX**（计算类、要求消费者时间不重叠）。
- `rot_lut` 用**真双口 BRAM** 同时服务 `sync_long` 与 `equalizer`，A/B 两端口各接一个消费者，并发读不冲突，全设计仅 1 个实例。
- `phase` 模块是**计算流水线**（div_gen 除法器 + atan_lut），无法双口，故用 `state == S_SYNC_SHORT` 这一个条件做输入 MUX、输出广播，在 sync_short（粗 CFO）与 equalizer（导频细 CFO）之间分时复用——前提是两者在状态机时间上不重叠。
- 这套共享之所以无痛，靠的是全项目统一的「数据 + strobe」握手，以及 `delayT` 在模块内部把 strobe「错相位」对齐到流水线输出（如 phase 里 `DELAY(36)` 匹配除法器延迟）。
- 作者对昂贵且时间不重叠的资源（rot_lut、phase）选择共享，对廉价且需并发的资源（complex_mult）选择复制——这是资源约束下的清晰取舍。
- 取消 phase 的分时复用，净增约一个除法器 IP（外加一张小 ROM）；精确资源数需综合报告确认。

## 7. 下一步学习建议

- 阅读 [u6-l3 Xilinx IP core 与 coregen 依赖](u6-l3-xilinx-ip-coregen.md)，理解 `rot_lut`、`atan_lut`、`div_gen_v3_0`、`complex_multiplier` 这些被共享/复制的 IP 在 coregen 目录下是如何以 `.v` 行为模型 + `.xco` 配置 + `.ngc` 网表三件套存在的，以及它们如何被 `dot11_modules.list` 纳入编译。
- 结合 [u6-l4 USRP N210 集成](u6-l4-usrp-integration.md)，理解这些共享取舍最终是为了在 Spartan 3A-DSP 这块特定芯片上塞下整条解码链，以及上板时 `setting_reg` 配置总线如何在不增加资源的前提下让 host 调参。
- 若想量化本讲的资源账本，建议实际跑一次 Xilinx 综合工具（iverilog 给不出资源数），对比「共享版」与「双 phase 实例版」的 map 报告，把第 4.4 节表格里的「待确认」项填实。
- 进阶思考：phase 目前靠「状态机天然时间不重叠」实现安全分时复用。如果未来要支持连续流式解码（两个包背靠背、甚至同步与均衡时间重叠），这套共享还能成立吗？需要改成复制还是引入更复杂的仲裁？这是一个很好的架构推演练习。
