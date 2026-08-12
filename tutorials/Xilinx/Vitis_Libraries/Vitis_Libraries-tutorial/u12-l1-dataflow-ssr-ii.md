# dataflow、SSR、datawidth 与 II 调优

## 1. 本讲目标

本讲是「性能与数据流架构」单元的第一讲。在前面（u3-l2 讲过 `pipeline`/`unroll`/`dataflow` 三条 pragma 的基本含义，u6-l3 讲过 vss_fft_ifft_1d 的端到端流程）的基础上，本讲把视角从「单个内核怎么写」抬升到「整条数据流为什么快、还能更快吗」。

学完后你应该能够：

- 用一个统一公式把 **DATAFLOW、SSR、datawidth、II** 四个旋钮串成同一张吞吐图。
- 说清 DATAFLOW 任务级流水与循环级 `pipeline` 的层次关系，以及为什么「稳态吞吐由最慢 stage 决定」。
- 解释 SSR 如何让吞吐近似线性扩展，并指出它在面积、端口、布线上的代价。
- 理解 datawidth（宽 beat 打包多个样本）如何在不增加并行路径的前提下放大带宽。
- 读懂综合报告里的 II、latency、资源三列，并判断 II>1 的常见根因。

## 2. 前置知识

本讲默认你已经掌握下面几个概念（若不熟，先看对应讲义）：

- **HLS 与 RTL**：Vitis 库的 PL 内核是 C++ 经高层综合（HLS）变成硬件的（见 u3-l1、u3-l2）。
- **hls::stream 与 end-flag**：内核之间靠单向 FIFO 流动数据，长度由一条伴生 `bool` 流标记（见 u3-l1、u3-l3）。
- **`#pragma HLS pipeline / unroll / dataflow`** 的基本语义（见 u3-l2）。
- **ADF 图与 PL↔AIE 边界**：AIE 阵列只认无地址的 AXI Stream，由 PL 侧 `mm2s/s2mm` 把 DDR 数据搬成流（见 u5-l2、u6-l3）。
- **II（Initiation Interval，启动间隔）**：相邻两次循环迭代之间相隔的时钟周期数，是 HLS 最关键的一个数字。

> 一个贯穿全讲的直觉：硬件加速的本质是用「面积换时间」。下面四个旋钮都是这句话的不同切面——你可以并更多路（SSR / unroll / dataflow）、把路修得更宽（datawidth）、让每条路跑得更密（II 调小），但每一项都要付面积、功耗或布线的代价。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `utils/L1/include/xf_utils_hw/stream_dup.hpp` | PL HLS 原语样本：一个循环里同时用 `pipeline II=1` 与 `unroll`，是观察 II 与函数级并行最简单的真实内核。 |
| `dsp/L1/include/aie/mixed_radix_fft.hpp` | AIE 单核 FFT 内核：用 `static_assert` 做编译期防御，揭示「单核算力」这一层。 |
| `dsp/L2/include/aie/vss_fft_ifft_1d_graph.hpp` | SSR 真正发生的地方：`TP_SSR` 同时决定并行核数组、PLIO 端口数组与每核窗口大小。 |
| `dsp/L2/include/vss/vss_fft_ifft_1d/aie.hpp` | 顶层图：`std::array<input_plio, SSR>` 直接展示 SSR → PLIO 流数量的映射。 |
| `dsp/L2/examples/vss_fft_ifft_1d/my_params.cfg` | 旋钮的「配置入口」：`SSR`、`POINT_SIZE`、`AIE_PLIO_WIDTH` 在这里写定。 |
| `dsp/L2/examples/vss_fft_ifft_1d/example.mk` | 把 `SSR` 透传成 PL 搬运器的 `-DNSTREAM=$(SSR)`，证明 PL 与 AIE 两侧的 SSR 必须对齐。 |
| `dsp/L1/tests/hw/mm2s/mm2s.h` | datawidth 的最具体样本：`NBITS=128` 位 PLIO 总线、`samplesPerRead=NBITS/DATAWIDTH`。 |

## 4. 核心概念与源码讲解

### 统一吞吐模型：四个旋钮其实是同一个公式

在看四个模块之前，先把它们装进同一个公式里。一条数据流的稳态吞吐量（samples/s）可以拆成四个因子的乘积：

\[
\text{吞吐量} \;=\; \underbrace{P}_{\text{并行路径数}} \;\times\; \underbrace{\frac{W_{\text{beat}}}{W_{\text{sample}}}}_{\text{每拍样本数}} \;\times\; \underbrace{\frac{1}{\mathrm{II}}}_{\text{每周期迭代数}} \;\times\; \underbrace{f_{\text{clk}}}_{\text{时钟频率}}
\]

- \(P\)：并行路径数，由 **SSR**、`unroll`、`dataflow` 共同撑大。
- \(W_{\text{beat}}/W_{\text{sample}}\)：一个 stream beat 里打包了几个样本，由 **datawidth** 决定。
- \(1/\mathrm{II}\)：每个周期每条路径能推进几次迭代，由 **II 调优**决定。
- \(f_{\text{clk}}\)：时钟频率，由平台与时序收敛决定。

本讲要讲的就是：**DATAFLOW 把 \(P\) 从「循环内」抬到「函数间」，SSR 把 \(P\) 从「1」扩到「N」，datawidth 放大每拍样本数，II 调优把 \(1/\mathrm{II}\) 推向 1。** 四个模块分别对应这四个因子。下面逐个拆开。

### 4.1 DATAFLOW 任务级流水

#### 4.1.1 概念说明

`#pragma HLS DATAFLOW` 作用在**函数/任务级**，而不是循环级。它把多个子函数用 `hls::stream` 串成一条流水线：第一个函数处理完第 0 帧就立刻把结果通过流喂给第二个函数，自己接着处理第 1 帧——多个帧在多个 stage 上同时前进。这和循环级 `pipeline`（同一函数内相邻迭代重叠）是两个层次，二者可以叠加。

要害有三点：

1. **函数级并行**：DATAFLOW 让「不同函数」并发，`pipeline` 让「同一函数的不同迭代」并发。前者是空间上的多条流水线，后者是时间上的一条流水线。
2. **稳态吞吐由最慢 stage 决定**：类比工厂流水线，整条线的产出速率卡在最慢那道工序。
3. **换的是面积**：所有 stage 必须同时存活（各自占一份硬件）才能并发，stage 越多面积越大。

#### 4.1.2 核心流程

DATAFLOW 要正常工作需满足三条纪律（任一破坏都会让综合器退化成顺序执行）：

- stage 之间**只能用 `hls::stream`（或 FIFO/PIPO）传递**数据，不能用普通变量。
- 每个流**单生产者、单消费者**，且顺序读写。
- 子函数内部不要再套 DATAFLOW 嵌套（嵌套语义受限）。

满足时，执行时间线变成（以三 stage A→B→C、处理 N 帧为例）：

```
帧号:    0     1     2     ...   N-1
stage A: |A0|  |A1|  |A2|  ...   |A(N-1)|
stage B:    |B0|  |B1|  |B2| ... |B(N-1)|
stage C:       |C0|  |C1|  |C2|...|C(N-1)|
```

填满流水后，每过一个最慢 stage 的延迟就产出一帧，端到端吞吐 ≈ \(1 / \max_i(\text{latency}_i)\)。

#### 4.1.3 源码精读

DATAFLOW 的样本在 u3-l3 已讲过 `axiToStream`/`streamToAxi`（突发读写与切分拼接两阶段经 DATAFLOW 并发）。这里看一个更朴素的对照：`streamDup` **本身**没有 DATAFLOW，它内部只有循环级 `pipeline`——这恰好说明「单函数原语用 pipeline，多函数组装才用 dataflow」。

[utils/L1/include/xf_utils_hw/stream_dup.hpp:92-108](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/stream_dup.hpp#L92-L108) 这段是 `streamDup` 的主循环：第 94 行 `#pragma HLS pipeline II = 1` 给整个 `while` 循环定下 II=1，循环内的 `for` 用第 99 行的 `#pragma HLS unroll` 完全展开成 `_NStrm` 路并发写。注意它是「一个函数内的循环级并行」，不是任务级流水。

把多个这样的原语**组装**起来时才轮到 DATAFLOW：例如 `axiToStream` 里「DDR 突发读」与「beat 拆分成窄流」是两个子函数，靠 DATAFLOW 让读下一块 DDR 的同时把上一块拆出去——这是任务级并发，\(P\) 因此从 1 变成 stage 数。本讲的要点是：**当你在更上层（如 vss 图）把多个内核串起来时，PL 侧的 DATAFLOW 与 AIE 侧的图连接是同一思想在两种范式里的投影。**

#### 4.1.4 代码实践

> 实践目标：用真实源码体会「单函数 pipeline」与「多函数 dataflow」的边界。

1. 打开 [utils/L1/include/xf_utils_hw/stream_dup.hpp:87-108](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/stream_dup.hpp#L87-L108)，确认 `streamDup` 体内只有 `pipeline`+`unroll`，没有 `DATAFLOW`。
2. 打开 u3-l3 提到的 `axiToStream`（`utils/L1/include/xf_utils_hw/axi_to_stream.hpp`），找到它的 `DATAFLOW` pragma，列出它串了哪几个子函数。
3. 画出两种结构对照图：`streamDup` 是「一条循环流水线」，`axiToStream` 是「多条函数流水线」。

需要观察的现象：`streamDup` 的吞吐受限于它单条循环的 II（这里是 1）；`axiToStream` 的吞吐受限于读 DDR 与拆 beat 两者中较慢的一个。预期结果：理解到 DATAFLOW 把瓶颈从「单函数 II」转移到「最慢 stage 的延迟」。**待本地验证**：若你有 Vitis 环境，可对两者分别跑 `csynth`，在报告里比较它们是否出现 `dataflow` 相关的 loop/pipeline 条目。

#### 4.1.5 小练习与答案

- **练习 1**：如果 DATAFLOW 的某个 stage 比其他 stage 慢 3 倍，整条流水线再增加更多 stage 能提升稳态吞吐吗？
  - **答案**：不能。稳态吞吐由最慢 stage 决定（\(\max_i\)），加更多 stage 只会增加面积和填满时间，不会抬高速率；要提速必须先把最慢 stage 拆细或并行化。
- **练习 2**：为什么 DATAFLOW 要求 stage 间只能用 `hls::stream`？
  - **答案**：因为要让多个 stage 同时处理不同帧，stage 间数据必须是可以「边读边写」的顺序通道；普通变量是单值存储，无法表达多帧在途的时序关系，综合器只能退化为顺序执行。

### 4.2 SSR 并行扩展

#### 4.2.1 概念说明

SSR 在 dsp 库里指 **Super Sample Rate / 并行流水路径数**（在 vss/FFT 语境里就是 `TP_SSR`，见下文）。它的核心思想是：与其让一条路径吃下全部数据，不如把数据切成 \(P\) 份，用 \(P\) 条完全并行的路径同时处理，吞吐近似线性扩展——这正是统一公式里把 \(P\) 从 1 放大到 N 的手段。

注意 SSR 与 `unroll` 的区别：`unroll` 是单内核内部把循环展开成多份；SSR 是在**图级别**实例化多份内核、多份端口，是更粗粒度的、跨 AIE 核（或跨 PL 模块）的空间并行。SSR 翻倍 → 内核数、PLIO 端口数、PL 搬运器的流数都翻倍。

#### 4.2.2 核心流程

SSR 在 vss FFT 系统里的传导链（从配置到硬件）：

1. `my_params.cfg` 写 `SSR=4`。
2. 代码生成把 `SSR` 注入 AIE 图模板参数 `TP_SSR`。
3. 图里 `frontFFTGraph[TP_SSR]`、`backFFTGraph[TP_SSR]`、`m_fftTwRotKernels[TP_SSR]` 各实例化 SSR 份内核。
4. 顶层图 `std::array<input_plio, SSR>` 创建 SSR 个 PLIO 端口。
5. PL 侧 `example.mk` 把 `-DNSTREAM=$(SSR)` 传给 `mm2s/s2mm`，于是搬运器产出/消费 SSR 路并行流，正好接上 AIE 的 SSR 个 PLIO。

整条链两侧的 SSR **必须相等**，否则 PL 产出的流数与 AIE 消费的端口数对不上——这是 u6-l3 提过的「两套参数源必须同步」陷阱的具体体现。

#### 4.2.3 源码精读

先看 SSR 在图里如何放大并行核与端口。`vss_fft_ifft_1d_graph` 的文档对 `TP_SSR` 有一句直白的定义：

[dsp/L2/include/aie/vss_fft_ifft_1d_graph.hpp:119-121](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/include/aie/vss_fft_ifft_1d_graph.hpp#L119-L121) ——「`TP_SSR` 描述实现将被切分成的并行计算路径数，以提高性能。更高的 SSR 对应更高的性能。」

紧接着 SSR 直接决定三组数组的大小：

[dsp/L2/include/aie/vss_fft_ifft_1d_graph.hpp:177-185](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/include/aie/vss_fft_ifft_1d_graph.hpp#L177-L185) 第 177 行 `kernel m_fftTwRotKernels[TP_SSR];`，第 179–185 行 `port_array<input, TP_SSR> front_i;` 等四组端口数组——SSR 翻倍，核与端口都翻倍。

并行路径还把每份的工作量缩小为 1/SSR：

[dsp/L2/include/aie/vss_fft_ifft_1d_graph.hpp:206-209](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/include/aie/vss_fft_ifft_1d_graph.hpp#L206-L209) 第 206 行 `kWindowSizeRaw1 = (kPtSizeD2Ceil * kPtSizeD1) / TP_SSR;` ——每个前级 FFT 核只处理总点数的 1/SSR，所以路径数 ×2 与每路径工作量 ÷2 抵消后，稳态吞吐 ×2。

再看 SSR 如何传导到 PL 端口。顶层图按 SSR 循环建 PLIO：

[dsp/L2/include/vss/vss_fft_ifft_1d/aie.hpp:58-61](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/include/vss/vss_fft_ifft_1d/aie.hpp#L58-L61) 第 58–61 行四组 `std::array<..., SSR>`，分别是前/后级的输入/输出 PLIO。完整 FFT+IFFT 系统共有 \(4\times\mathrm{SSR}\) 个 PLIO 端口。

最后看 PL 搬运器怎么对齐：

[dsp/L2/examples/vss_fft_ifft_1d/example.mk:19-23](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/example.mk#L19-L23) 第 20 行 `SSR := 4` 是硬编码变量（与 `my_params.cfg` 的 `SSR=4` 是两套独立来源）；[dsp/L2/examples/vss_fft_ifft_1d/example.mk:32-33](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/example.mk#L32-L33) 用 `-DNSTREAM=$(SSR)` 把它传给 `mm2s/s2mm`，于是搬运器内部 `sig_o[NSTREAM_INT]` 即 SSR 路流（参见 `mm2s.cpp` 第 91 行 `TT_STREAM sig_o[NSTREAM_INT]`）。

#### 4.2.4 代码实践

> 实践目标：定量分析把 SSR 从 2 改到 4 对流数量与理论吞吐的影响，并指出代价。

已知（全部来自上面读到的源码）：平台时钟 `freqhz=312500000`（见 `my_params.cfg` 第 3 行），`AIE_PLIO_WIDTH=128`（第 24 行），`DATA_TYPE=cint32`（第 13 行，一个 cint32 = 64 bit）。

1. **每条 PLIO 的样本率**：128-bit PLIO 每拍装 `128/64 = 2` 个 cint32，每秒 `2 × 312.5 MHz = 625 M` 样本/路。
2. **SSR=2**：聚合吞吐 \(2 \times 625\,\text{M} = 1.25\,\text{Gsamples/s}\)；PLIO 端口数 \(4 \times 2 = 8\)；前级 FFT 核 2 份、后级 2 份、旋转核 2 份。
3. **SSR=4**（仓库默认）：聚合吞吐 \(4 \times 625\,\text{M} = 2.5\,\text{Gsamples/s}\)；PLIO 端口数 \(4 \times 4 = 16\)；各级核均 4 份。
4. **结论**：SSR 2→4，理论吞吐翻倍；代价是 PLIO 端口、AIE 核、PL 搬运器流数全部翻倍，面积近似翻倍，布线压力上升可能压低 \(f_{\text{clk}}\)。
5. **上限**：AIE 核数与 PLIO 数量受器件约束（VCK190 的 AI Engine 核与 PLIO 引脚有限），SSR 不能无限增大；并且 `vss_fft_ifft_1d_graph.hpp` 第 244–247 行的 `static_assert` 会对「点数与 SSR 的非法组合」直接拒绝编译。

需要观察的现象：若真的改 `SSR`，必须同时改 `example.mk` 第 20 行的 `SSR :=` 与 `my_params.cfg` 第 23 行的 `SSR=` 两处。预期结果：两侧一致时链接通过；不一致时 PL 流数与 AIE 端口数错配，链接报错。**待本地验证**：完整 AIE 构建需要 Vitis + aietools + 目标平台，本环境无法运行。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 SSR 能让吞吐「近似线性」而不是「严格线性」扩展？
  - **答案**：理论上路径数 ×2、每路径工作量 ÷2，吞吐应 ×2；但路径数增多带来更多 PLIO 与核间连接，布线拥塞可能降低可达成时钟频率 \(f_{\text{clk}}\)，于是实际吞吐略低于 2×。此外 SSR 还要求点数能被整除分解，不是所有取值都合法。
- **练习 2**：把 SSR 翻倍但没有同步修改 `example.mk` 的 `SSR :=`，会发生什么？
  - **答案**：AIE 图一侧按新 SSR 建出更多 PLIO 端口，而 PL 搬运器仍按旧 `NSTREAM` 只产出旘认数量的流，二者在 `system.cfg` 链接时端口数不匹配，链接阶段报错。

### 4.3 datawidth 位宽取舍

#### 4.3.1 概念说明

datawidth 指**一条流/一个端口的 beat 位宽**。在不增加并行路径数的前提下，把每个 beat 加宽，让一个 beat 打包多个样本，就能直接放大「每拍样本数」\(W_{\text{beat}}/W_{\text{sample}}\)——这是统一公式里和 SSR、II 并列的第三个旋钮。

它和 SSR 的区别在于：SSR 是「多开几条窄路」，datawidth 是「把一条路修宽」。两者都能提高吞吐，但代价不同：

- 加宽 datawidth：主要吃更宽的存储端口和布线，单条路内部仍是顺序处理。
- 增大 SSR：吃更多份硬件与更多端口，但每条路可以保持窄而简单。

#### 4.3.2 核心流程

在 PL↔AIE 边界上，datawidth 由器件 PLIO 总线宽度钉死。vss 系统的 PLIO 总线是 128 bit @ 312.5 MHz。于是「一个样本多宽」决定了「一个 beat 装几个样本」：

\[
\text{samplesPerRead} \;=\; \frac{\text{NBITS}}{\text{DATAWIDTH}} \;=\; \frac{W_{\text{beat}}}{W_{\text{sample}}}
\]

选 cint32（64 bit）作样本时，`samplesPerRead = 128/64 = 2`，即每个 PLIO beat 携带 2 个样本；若换成 cint16（32 bit），同样的 128-bit beat 能装 4 个样本，每路吞吐再翻倍——但 AIE 核要做 16-bit 运算，数学精度与可表达动态范围也变了，这是精度-带宽的连带取舍。

#### 4.3.3 源码精读

datawidth 最具体的样本在 PL 搬运器 `mm2s.h`：

[dsp/L1/tests/hw/mm2s/mm2s.h:76-82](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/mm2s/mm2s.h#L76-L82) 第 76 行 `static constexpr unsigned NBITS = 128;`（注释：「PL 侧 PLIO 总线宽度 @ 312.5 MHz」）；第 77 行 `TT_DATA = ap_uint<NBITS>`（宽 beat 类型，注释说「等于两个 cint32 样本」）；第 81 行 `samplesPerRead = NBITS / DATAWIDTH;`（每拍样本数）；第 82 行 `TT_SAMPLE = ap_uint<DATAWIDTH>`（单样本类型）。

AIE 侧的 PLIO 宽度则由配置项选择：

[dsp/L2/include/vss/vss_fft_ifft_1d/aie.hpp:83](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/include/vss/vss_fft_ifft_1d/aie.hpp#L83) 第 83 行 `adf::plio_type aiePlioWidth = AIE_PLIO_WIDTH == 64 ? adf::plio_64_bits : adf::plio_128_bits;` ——`my_params.cfg` 里 `AIE_PLIO_WIDTH=128`，所以这里选 128-bit PLIO，与上面 `NBITS=128` 两侧对齐。

> 把 4.2 与 4.3 合起来看：vss 每条 PLIO = `samplesPerRead(2)` × `f_clk(312.5M)` = 625 Msamples/s，再乘 SSR 路数得聚合吞吐。这正是统一公式 \(P \times (W_{\text{beat}}/W_{\text{sample}}) \times f_{\text{clk}}\)（这里 II 视为 1）的直接代入。

#### 4.3.4 代码实践

> 实践目标：体会样本位宽如何反向决定每路吞吐。

1. 读 [dsp/L1/tests/hw/mm2s/mm2s.h:76-82](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/mm2s/mm2s.h#L76-L82)，确认 `NBITS=128`、`DATAWIDTH` 由命令行宏注入（`example.mk` 第 23 行 `DATAWIDTH := 64`）。
2. 计算：若把样本类型从 cint32（64 bit）换成 cint16（32 bit），即 `DATAWIDTH=32`，则 `samplesPerRead = 128/32 = 4`，每路吞吐从 625 M 升到 1.25 G samples/s。
3. 读 `mixed_radix_fft.hpp` 第 78–86 行的 `static_assert`，确认 cint16 与 cint32/cfloat 各自合法的 `TT_TWIDDLE` 组合不同——即加宽/变窄样本类型会牵动整条类型组合约束。

需要观察的现象：datawidth 加宽让单路吞吐上升，但要求存储端口、AXI 总线、AIE 向量寄存器都匹配更宽的位宽。预期结果：在位宽允许的范围内，加宽 datawidth 是「不增路径就涨吞吐」的廉价手段，但受器件最大端口宽度与类型合法性约束。**待本地验证**。

#### 4.3.5 小练习与答案

- **练习 1**：为什么不直接把 datawidth 设到无穷宽？
  - **答案**：器件的存储端口、PLIO 引脚、AIE 向量寄存器都有最大位宽上限；加宽还会增加布线拥塞、拉低时钟频率，且更宽的 beat 要求样本类型与运算单元同步变宽（类型组合受 `static_assert` 约束）。收益与代价在某点之后逆转。
- **练习 2**：datawidth 翻倍与 SSR 翻倍，哪个对「单核硬件」改动更小？
  - **答案**：datawidth 翻倍（在器件允许范围内）通常改动更小——它主要是加宽既有端口与向量，核数不变；SSR 翻倍则要复制整份内核与端口，面积与连接增长更显著。

### 4.4 II 调优

#### 4.4.1 概念说明

II（Initiation Interval）是「相邻两次迭代启动之间相隔的周期数」。吞吐与 II 成反比：II=1 表示每周期都能启动一次迭代（满流水），II=2 则吞吐减半。II 调优的目标就是把统一公式里的 \(1/\mathrm{II}\) 推到 1。

II>1（称为 II 违例）几乎都来自三类根因：

1. **存储端口竞争**：一个数组默认只有 1–2 个读写端口，循环里要一周期读 N 次就被端口数卡住。解法是 `#pragma HLS array_partition` 把数组拆开（见 u3-l2）。
2. **迭代间数据依赖**：本次迭代要用到上次迭代刚算出的值（carried dependency），组合逻辑无法在一周期内闭合。解法是重构算法、加寄存器切断依赖，或接受较高 II。
3. **资源限额**：DSP/LUT/BRAM 超过单周期可放置量，综合器被迫串行化部分运算。

#### 4.4.2 核心流程

调优循环一般是：

1. 在 `#pragma HLS pipeline II=1` 处声明目标 II。
2. 跑 `csynth`，打开以 top 函数命名的报告（如 `dut0_csynth.rpt`），读 「II」「Latency」「BRAM/DSP/FF/LUT」三组数。
3. 若实际 II > 目标：报告的 *Schedule Viewer* 会指出是哪个操作/端口/依赖拖慢，据此加 `array_partition`、拆循环、切断依赖或放宽 II。
4. 重新综合直到 II 达标或确认无法达标（再增面积不划算）。

要点：II 写的是「期望」，综合器给的是「实际」；二者之差就是优化空间。

#### 4.4.3 源码精读

最简单的 II 样本仍是 `streamDup`：

[utils/L1/include/xf_utils_hw/stream_dup.hpp:92-103](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/stream_dup.hpp#L92-L103) 第 94 行 `#pragma HLS pipeline II = 1` 把循环目标钉在 1。它为何能轻松达到 II=1？因为循环体只做：读一条 `e_istrm`、读一条 `istrm`、把同一 `tmp` 写到 `_NStrm` 条输出流（第 98–102 行，由 `unroll` 展开成并发写）。这里没有数组多端口竞争（流的端口足够），也没有跨迭代依赖，资源（纯拷贝、无算术）极轻，所以 II=1 几乎免费达成——这也解释了 u2-l3 里「streamDup 预计 DSP=0、II=1」的预判。

对比：[dsp/L1/include/aie/mixed_radix_fft.hpp:78-92](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/include/aie/mixed_radix_fft.hpp#L78-L92) AIE FFT 单核里有大量 `static_assert` 把「点数必须是 2/3/5 的倍数」「窗口大小必须是点数整数倍」等约束前移到编译期——这些约束正是为了让 FFT 各级能整齐分块、避免运行期动态判断打断流水，从而维持高吞吐（AIE 侧的「II」体现为 `runtime<ratio>` 与核的周期预算）。两类内核的共同点是：**II 不是一个事后补救的数字，而是靠算法结构（流式、无依赖、整齐分块）在源头就让 II=1 容易达成。**

#### 4.4.4 代码实践

> 实践目标：学会从综合报告定位 II 与瓶颈（源码阅读 + 报告阅读型实践）。

1. 打开 [utils/L1/include/xf_utils_hw/stream_dup.hpp:92-108](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/utils/L1/include/xf_utils_hw/stream_dup.hpp#L92-L108)，把第 94 行的 `II = 1` 改成 `II = 2`，预测：根据统一公式 \(1/\mathrm{II}\)，吞吐应减半。
2. 在 `stream_dup` 用例目录跑 `make run TARGET=csynth`，打开生成的 `test.prj/<top>_csynth.rpt`，记录实际的 II、Latency、BRAM/LUT/FF/DSP。
3. 对照 II=1 与 II=2 两份报告，确认 Latency 近似翻倍、资源基本不变——这正是「II 影响吞吐/延迟、不一定影响面积」的体现。

需要观察的现象：人为把目标 II 放宽到 2，综合器会让循环每两周期才接受一次新输入，单帧延迟翻倍。预期结果：报告里 *Achieved II* 跟随你的设置变为 2，吞吐减半。**待本地验证**：本环境无 Vitis，无法实际跑 `csynth`，以上为基于源码与 HLS 规则的预判。

#### 4.4.5 小练习与答案

- **练习 1**：一个循环目标 II=1，综合后实际 II=2，报告指出瓶颈是「array read port」。如何处理？
  - **答案**：该数组每周期被读多次但端口不足，用 `#pragma HLS array_partition variable=arr cyclic factor=N`（或 `block`）把数组拆成多份，让每个分片有自己的端口，从而满足单周期多读，通常能把 II 拉回 1（代价是更多 BRAM）。
- **练习 2**：如果 II 违例来自真正的迭代间数据依赖（本次要用上次结果），还能靠加资源解决吗？
  - **答案**：通常不能。迭代间依赖是算法层面的时序闭合问题，加端口/拆数组无效；需要重构算法（如预计算、解耦递推）或在依赖链上插入寄存器让逻辑跨周期闭合，有时只能接受较高 II。

## 5. 综合实践

把四个旋钮串成一个完整的吞吐预算练习，全部基于本仓库的真实配置（`my_params.cfg` + `mm2s.h` + `vss_fft_ifft_1d_graph.hpp`）：

> 任务：为 vss_fft_ifft_1d 填一张「吞吐预算表」，并评估三种扩容方案。

基线（仓库默认）：`POINT_SIZE=4096`、`SSR=4`、`AIE_PLIO_WIDTH=128`、`DATA_TYPE=cint32`（64 bit）、`freqhz=312.5 MHz`。

1. **填表**：
   - 每路每拍样本数 `samplesPerRead = 128/64 = 2`。
   - 每路吞吐 = \(2 \times 312.5\,\text{M} = 625\,\text{Msamples/s}\)。
   - 聚合吞吐 = \(P \times 2 \times 312.5\,\text{M}\)，其中 \(P = \mathrm{SSR}\)。
   - PLIO 端口数 = \(4 \times \mathrm{SSR}\)。
2. **方案 A：SSR 4→8**。聚合吞吐 2.5G→5.0 G（×2）；PLIO 16→32；核数 ×2。代价：面积翻倍、布线压力大、受 VCK190 PLIO/核上限约束。
3. **方案 B：datawidth 不变但换 cint16 样本**（`DATAWIDTH` 64→32）。`samplesPerRead` 2→4，每路吞吐 625M→1.25G，聚合 2.5G→5.0G（×2）但 PLIO/核数不变。代价：16-bit 精度与动态范围下降，且要满足 `mixed_radix_fft.hpp` 第 81–86 行的 cint16/cint16 类型组合约束。
4. **方案 C：维持 SSR=4，靠算法把某 stage 的 II 从 2 降到 1**。吞吐 ×2（仅对该瓶颈 stage 生效）。代价：需 `array_partition`/重构，面积略增。

要求：写出三种方案各自的统一公式代入、理论吞吐、主要代价，并说明在「面积受限」「精度受限」「时序受限」三种工程约束下分别该选哪个方案。

> 这个练习把 DATAFLOW（最慢 stage 决定整线）、SSR（放大 \(P\)）、datawidth（放大每拍样本数）、II（放大每周期迭代数）四个模块汇到同一张决策表——这正是性能工程师面对真实数据流时的日常工作。

## 6. 本讲小结

- 四个旋钮共享一个公式：\(\text{吞吐} = P \times (W_{\text{beat}}/W_{\text{sample}}) \times (1/\mathrm{II}) \times f_{\text{clk}}\)。
- **DATAFLOW** 把并行从循环级抬到函数级，稳态吞吐由最慢 stage 决定，代价是所有 stage 同时占面积；`streamDup` 只有循环级 `pipeline`，多函数组装才用 `dataflow`。
- **SSR** 在图级别实例化多份核与端口，吞吐近似线性扩展；vss 系统里 `TP_SSR` 同时驱动 AIE 核数组、PLIO 端口数组与 PL 搬运器 `NSTREAM`，两侧必须相等，PLIO 总数 = \(4\times\mathrm{SSR}\)。
- **datawidth** 把单个 beat 加宽以打包更多样本，vss 的 128-bit PLIO 装 2 个 cint32，每路 625 Msamples/s；加宽受器件端口宽度与类型组合 `static_assert` 约束。
- **II** 决定每周期迭代数，II=1 是目标；II>1 多由存储端口竞争、迭代间依赖、资源限额引起，靠 `array_partition`、重构算法或放宽 II 处理。
- 四者都遵循「面积换时间」：选哪个旋钮取决于当前是面积、精度还是时序受限。

## 7. 下一步学习建议

- **下一讲 u12-l2（资源/时序：URAM、HBM/DDR 分区与报告）**：本讲的吞吐公式假设数据供得上；下一讲讲「存储侧」——URAM 缓存与 DDR/HBM 多 bank 分区如何把带宽喂给这些并行路径，以及如何从实现报告定位资源与时序瓶颈。
- **横向延伸**：回头对比 u6-l2 的 AIE `TP_CASC_LEN`（级联）——那是另一种放大 \(P\) 的维度（级联并行），与 SSR（空间并行）正交，可以叠加。
- **源码深读**：想看 DATAFLOW 在多 stage 系统里的真实用法，可读 `utils/L1/include/xf_utils_hw/axi_to_stream.hpp`；想看 SSR 上限与非法组合的编译期拦截，可读 `dsp/L2/include/aie/vss_fft_ifft_1d_graph.hpp` 第 244–247 行的 `static_assert`。
