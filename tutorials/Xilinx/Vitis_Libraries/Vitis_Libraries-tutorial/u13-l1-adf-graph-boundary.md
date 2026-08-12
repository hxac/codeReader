# ADF 图、窗口/流与 PL↔AIE 边界

## 1. 本讲目标

本讲是「AIE 编程模型深入」单元的第一讲。在 u6-3 里我们跑通过了一个完整的 AIE 示例 `vss_fft_ifft_1d`，但当时把 AIE 图当成一个「黑盒函数」来调用——只关心 `init()/run()/end()`、参数配置与打包。本讲要打开这个黑盒，回答三个问题：

1. 一个 **ADF 图（graph）** 在源码层面到底由什么构成？`graph`、`kernel`、`connect` 三者如何组织？
2. AIE 内核之间交换数据有 **window（窗口）** 与 **stream（流）** 两套范式，它们的区别与适用场景是什么？
3. 数据如何跨越 **PL↔AIE 边界**？`mm2s`/`s2mm` 搬运器、`PLIO` 端口与 `GMIO` 各扮演什么角色？

学完后你应当能够：读懂任何一个 ADF 图的源码骨架、判断某内核用的是 window 还是 stream、并能用 `system.cfg` 的 `sc=`（stream connection）把 PL 内核接到 AIE 图上。

## 2. 前置知识

本讲假设你已经掌握：

- **L1/L2/L3 三层抽象**与 **PL/AIE 两种范式**（u1-l3）。
- **HLS 流式内核**模型：`hls::stream`、`ap_int`、`#pragma HLS DATAFLOW`（u3-1、u3-2）。
- **v++ 三段构建**：`-c` 编译、`-l` 链接、`--package` 打包，以及 `system.cfg` 里的 `nk`/`sp`/`sc`（u5-1）。
- **数据搬运器** `mm2s`/`s2mm` 的桥接角色（u5-2）。
- **vss_fft_ifft_1d 端到端流程**：AIE 图生命周期、参数驱动代码生成、`v++ --link --mode vss`（u6-3）。

几个本讲会用到的术语先统一一下：

- **ADF（Adaptive Data Flow）**：AMD 用来描述 AI Engine 应用的数据流图编程模型，对应的 C++ 类在 `<adf.h>` 里。
- **AIE 核（AI Engine tile）**：Versal 器件里的标量/vector 计算单元，每个核有本地存储与流交换开关。
- **PLIO**：AIE 图对外暴露的「物理流端口」，是 PL（可编程逻辑）与 AIE 之间的数据闸口。
- **GMIO**：另一种 AIE↔DDR 桥接机制，让 AIE 核不经 PL 内核、直接 DMA 访问 DDR。

> 提醒：本讲所有的「黑体」概念都对应到后面的真实源码行号，可以对照阅读。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [dsp/L2/include/vss/vss_fft_ifft_1d/aie.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/include/vss/vss_fft_ifft_1d/aie.hpp) | 顶层 ADF 图类 `tl_graph`：声明 PLIO 端口、实例化内部 FFT 图、用 `connect<>` 把 PLIO 连到内部图端口。 |
| [dsp/L2/include/vss/vss_fft_ifft_1d/aie.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/include/vss/vss_fft_ifft_1d/aie.cpp) | 仿真入口 `main()`：实例化 `tl_graph`，调用 `init()/run()/end()` 驱动整张图。 |
| [dsp/L2/include/vss/vss_fft_ifft_1d/vss_fft_ifft_1d_params.cfg](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/include/vss/vss_fft_ifft_1d/vss_fft_ifft_1d_params.cfg) | 应用参数：`SSR`、`AIE_PLIO_WIDTH`、`DATA_TYPE` 等，决定图的端口数与位宽。 |
| [dsp/L1/tests/hw/mm2s/mm2s.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/mm2s/mm2s.cpp) / [mm2s.h](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/mm2s/mm2s.h) | PL 侧「内存到流」搬运器：DDR(`m_axi`) → 重排 → 多路 AXI Stream(`axis`)。 |
| [dsp/L1/tests/hw/s2mm/s2mm.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/s2mm/s2mm.cpp) / [s2mm.h](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/s2mm/s2mm.h) | PL 侧「流到内存」搬运器：多路 AXI Stream → 重排 → DDR。 |
| [dsp/L2/examples/vss_fft_ifft_1d/system.cfg](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/system.cfg) | 系统连接配置：用 `nk`/`sp`/`sc` 把 PL 搬运器与 PL/AIE 内核拼成完整数据通路。 |
| [dsp/L1/include/aie/mixed_radix_fft.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/include/aie/mixed_radix_fft.hpp) | AIE FFT 内核——**window 范式**的样本（`input_buffer`/`output_buffer`）。 |
| [dsp/L1/include/aie/fir_sr_asym.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/include/aie/fir_sr_asym.hpp) | AIE FIR 内核——**stream 范式**的样本（`input_stream`/`output_stream`）。 |

---

## 4. 核心概念与源码讲解

### 4.1 ADF 图的构成：graph、kernel、connect

#### 4.1.1 概念说明

ADF 把一个 AIE 应用抽象成一张**有向数据流图**。这张图由三种东西组成：

- **graph（图）**：一个继承自 `adf::graph` 的 C++ 类，是整张图的「容器」。它的构造函数里完成所有「实例化内核 + 拉线」的工作。
- **kernel（内核）**：图里的一个计算节点，对应一个会被映射到某个 AIE 核上运行的 C++ 函数。用 `kernel::create_object` 或 `kernel::create` 创建，并用 `source(...)` 绑定到运行时函数。
- **connect（连接）**：图的「线」，描述数据从一个端口流向另一个端口。模板函数 `connect<>(src, dst)` 拉一条单向边。

一个形象比喻：graph 是一块面包板，kernel 是插在板上的芯片，connect 是面包板上的跳线，而 PLIO 是面包板边缘与外界（PL/DDR）相连的排针。

#### 4.1.2 核心流程

一个 ADF 图从源码到上板，经历三步：

```
1. 写 graph 类
   - 继承 adf::graph
   - 在构造函数里：实例化 kernel / 子图 → connect<> 拉线 → 注册 run 参数
2. 编译图（v++ -c --mode aie）
   - 把 graph 编成 libadf.a（含 AIE 指令、拓扑、存储分配）
3. 链接（v++ --link）
   - 把 libadf.a 与 PL 内核 XO 按 connectivity cfg 拼成 .xsa/.vss
   - sc= 把 PL 的 axis 端口与图边缘的 PLIO 焊到一起
```

图的运行生命周期由三个方法控制（与 u6-3 一致，这里强调它属于 graph 对象）：

- `init()`：分配核、加载代码、建立流通路；
- `run(n)`：启动 `n` 次迭代（`n` 个「免费帧」），内核开始消费输入；
- `end()`：释放、收尾。

#### 4.1.3 源码精读

`aie.hpp` 里的 `tl_graph` 是一个典型的「顶层图」。先看它的类声明：

[dsp/L2/include/vss/vss_fft_ifft_1d/aie.hpp:52-61](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/include/vss/vss_fft_ifft_1d/aie.hpp#L52-L61) —— `tl_graph` 继承自 `adf::graph`，声明了 4 组 `input_plio`/`output_plio` 数组（每组 `SSR` 个），它们就是图边缘与 PL 相连的「排针」。

```cpp
class tl_graph : public graph {
   ...
    std::array<input_plio, SSR> back_i;
    std::array<output_plio, SSR> back_o;
    std::array<input_plio, SSR> front_i;
    std::array<output_plio, SSR> front_o;
```

构造函数里做两件事：(1) 实例化内部 FFT 子图；(2) 创建 PLIO 并把它们 `connect` 到子图端口。

[dsp/L2/include/vss/vss_fft_ifft_1d/aie.hpp:84-86](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/include/vss/vss_fft_ifft_1d/aie.hpp#L84-L86) —— 实例化由参数模板化的内部 FFT 图 `fftGraph`（真正的 kernel/连接都在它内部，`tl_graph` 只负责把 PLIO 接到它的端口）。

[dsp/L2/include/vss/vss_fft_ifft_1d/aie.hpp:96-97](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/include/vss/vss_fft_ifft_1d/aie.hpp#L96-L97) —— 这是最关键的「拉线」代码：先用 `input_plio::create(...)` 造一个名为 `PLIO_front_in_i`、宽度由 `aiePlioWidth` 决定的输入端口，再用 `connect<>(front_i[i].out[0], fftGraph.front_i[i])` 把这个 PLIO 的输出连到内部图的 `front_i[i]` 输入。

```cpp
front_i[i] = input_plio::create("PLIO_front_in_" + std::to_string(i), aiePlioWidth, filenameInFront);
connect<>(front_i[i].out[0], fftGraph.front_i[i]);
```

> 注意第三个参数 `filenameInFront`：仿真（aiesim/x86sim）下，PLIO 会从这个文件读输入；上板时它退化成物理流端口，文件名不再使用。这是「同一份图代码，仿真/上板两种行为」的关键。

PLIO 宽度在运行时根据参数二选一：

[dsp/L2/include/vss/vss_fft_ifft_1d/aie.hpp:83](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/include/vss/vss_fft_ifft_1d/aie.hpp#L83) —— `AIE_PLIO_WIDTH == 64 ? adf::plio_64_bits : adf::plio_128_bits`，本例 `AIE_PLIO_WIDTH=128`（见 params.cfg 第 21 行），所以每个端口 128 位。

图的生命周期则在 `aie.cpp` 里被驱动：

[dsp/L2/include/vss/vss_fft_ifft_1d/aie.cpp:20-27](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/include/vss/vss_fft_ifft_1d/aie.cpp#L20-L27) —— 全局对象 `fft_tb` 是 `tl_graph` 的实例；`main()` 只做三件事：`init()`、`run(NITER)`、`end()`。**这是仿真入口**；上板时同样的 `init/run/end` 由主机 XRT 的 `xrt::graph` 发起（见 u13-2）。

```cpp
xf::dsp::aie::top_level::tl_graph fft_tb;
int main(void) {
    fft_tb.init();
    fft_tb.run(NITER);
    fft_tb.end();
    return 0;
}
```

#### 4.1.4 代码实践

**实践目标**：在源码层面数清楚 `tl_graph` 一共有多少个对外端口，理解 SSR 如何决定端口数。

**操作步骤**：

1. 打开 `aie.hpp` 第 58–61 行，数出 4 个 `std::array<..., SSR>`（`front_i/front_o/back_i/back_o`）。
2. 打开 `vss_fft_ifft_1d_params.cfg` 第 20 行，确认 `SSR=4`。
3. 计算：4 组 × 4 个 = 共 16 个 PLIO 端口。

**需要观察的现象**：

- SSR 每加 1，每侧（front/back、输入/输出）各多 1 个 PLIO，端口总数随 SSR **线性增长**。
- 第 87–113 行的 `for (int i = 0; i < SSR; i++)` 循环体里，每次迭代 `create` 4 个 PLIO + 4 条 `connect`，正好对应 4 组端口。

**预期结果**：本例 SSR=4 → 16 个 PLIO 端口；若把 SSR 改成 2，端口数应降到 8（待本地验证，改动需同步 `example.mk` 里的 SSR，见 u6-3 的告诫）。

#### 4.1.5 小练习与答案

**练习 1**：`tl_graph` 里为什么没有直接出现 `kernel::create` 这样的语句？

<details><summary>参考答案</summary> 因为 `tl_graph` 是「顶层壳图」，真正的计算内核封装在内部子图 `fftGraph`（即 `vss_fft_ifft_1d_graph`）里。`tl_graph` 的职责只是把 16 个 PLIO 端口「转接」到内部图的端口上，属于一种包装（wrapper）模式，便于在不同 SSR/位宽下复用同一张内部图。</details>

**练习 2**：`connect<>(a, b)` 里 `a`、`b` 的方向是什么？

<details><summary>参考答案</summary> `connect<>(src, dst)` 是单向的，数据从 `src` 流向 `dst`。例如 `connect<>(front_i[i].out[0], fftGraph.front_i[i])` 表示数据从 PLIO 的 `out[0]` 流进内部图的 `front_i[i]` 输入端口。</details>

---

### 4.2 内核级数据交换：window 与 stream

#### 4.2.1 概念说明

AIE 内核是一个跑在某个核上的 C++ 函数。函数的「形参类型」决定了它与外界如何交换数据。ADF 提供两大类形参：

| 维度 | window（窗口 / buffer） | stream（流） |
| --- | --- | --- |
| 形参类型 | `input_buffer<T>` / `output_buffer<T>` | `input_stream<T>` / `output_stream<T>` |
| 交换单位 | 一**整块**样本（一个 window） | 逐**样本**（或逐向量） |
| 访问方式 | 指针/迭代器遍历，`window_readincr`/`window_writeincr` | `readincr()`/`writeincr()`，或流 intrinsic |
| 物理实现 | AIE 核本地数据存储（ping-pong 缓冲） | AIE 流开关（stream/cascade 互连） |
| 典型场景 | 块处理算法（FFT、矩阵） | 逐样本滤波、级联（FIR、累加链） |
| 跨核粒度 | 一次搬一大块，开销摊薄 | 一个样本一拍，适合流水 |

一句话直觉：**window 像「整箱送货」，stream 像「传送带逐件送货」**。前者适合一次处理一批的批处理内核，后者适合无界、连续的流式内核。

#### 4.2.2 核心流程

两种范式在图里的连接方式相同（都是 `connect<>`），区别在**内核函数签名**与**底层搬运机制**：

```
window 范式:
  PLIO/上游 ──connect──> input_buffer ──[内核读一整块]──> output_buffer ──connect──> 下游
  （ADF 自动在核本地存 ping-pong 两块缓冲，一块给内核读，一块同时由上游 DMA 写）

stream 范式:
  PLIO/上游 ──connect──> input_stream ──[内核 readincr 逐个]──> output_stream ──connect──> 下游
  （数据走 AIE 的流互连开关，不在本地存大缓冲）
```

window 的关键收益是**双缓冲**：当内核在处理第 N 块时，DMA 已经在后台往第 N+1 块写，隐藏了访存延迟；代价是要占用核本地存储。stream 的收益是**低延迟、少存储**，适合样本间有依赖或级联场景。

#### 4.2.3 源码精读

**window 范式样本——`mixed_radix_fft`：**

[dsp/L1/include/aie/mixed_radix_fft.hpp:275-276](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/include/aie/mixed_radix_fft.hpp#L275-L276) —— FFT 主内核 `mixed_radix_fftMain` 的两个形参都是 buffer：

```cpp
void mixed_radix_fftMain(input_buffer<TT_IN_DATA>& __restrict inWindow,
                         output_buffer<TT_OUT_DATA>& __restrict outWindow);
```

这正符合 FFT 的块处理本性：一次性吃进一整窗（`POINT_SIZE` 个样本）的输入、吐出一整窗的频域结果。`__restrict` 提示编译器输入输出不别名，便于向量化。

**stream 范式样本——`fir_sr_asym`：**

[dsp/L1/include/aie/fir_sr_asym.hpp:1220](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/include/aie/fir_sr_asym.hpp#L1220) —— 单输入单输出的 FIR 内核用流：

```cpp
void filterSingleKernelSingleIPSingleOP(input_stream<TT_DATA>* inStream, output_stream<TT_DATA>* outStream);
```

FIR 是典型的逐样本滤波：每读进一个样本就更新一次抽头延迟线并产出一个样本，天然契合 stream。同一文件第 1222–1249 行还提供了双输入/双输出（Dual IP/OP）变体，对应 AIE 核的两个流端口，用于加倍吞吐或做级联。

> 一个有用的判读规则：**看到 `input_buffer`/`output_buffer` 就是 window，看到 `input_stream`/`output_stream` 就是 stream**。这不只是风格差异，它会决定 AIE 编译器为该核分配本地存储还是流互连资源。

#### 4.2.4 代码实践

**实践目标**：在 dsp AIE 内核家族里做一次「window 还是 stream」的分类。

**操作步骤**：

1. 在 `dsp/L1/include/aie/` 下，对若干内核头文件搜索 `input_buffer|output_buffer`（window）与 `input_stream|output_stream`（stream）。
   - 示例命令（源码阅读型，不会改源码）：
     ```
     grep -l "input_buffer" dsp/L1/include/aie/*.hpp
     grep -l "input_stream" dsp/L1/include/aie/*.hpp
     ```
2. 把命中的文件分成两组：FFT/矩阵类（多半 window）与 FIR/DDS 类（多半 stream）。

**需要观察的现象**：

- `mixed_radix_fft.hpp`、`matrix_mult.hpp`（块处理）出现在 window 组；
- `fir_sr_asym.hpp`、`dds_mixer.hpp`（流式）出现在 stream 组。

**预期结果**：你会得到一张「块处理→window、流式→stream」的对照表。这条经验可直接外推到 vision、solver 等其他库的 AIE 内核（待本地验证具体归属）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 FFT 用 window 而不用 stream？

<details><summary>参考答案</summary> FFT 一次变换需要全部 N 个输入样本齐备后才能开始（蝶形运算有全局数据依赖），属于块处理。window 把一整块样本一次性送进核本地存储，内核可以随机访问、向量化处理；若用 stream 逐样本喂入，内核还得自己攒满 N 个样本才能开算，反而把缓冲压力转移给内核。故块处理选 window。</details>

**练习 2**：window 的 ping-pong 双缓冲是如何隐藏延迟的？

<reference answer> <details><summary>参考答案</summary> ADF 为每个 window 端口在核本地存分配两块缓冲。当内核在处理缓冲 A 时，上游 DMA 同时往缓冲 B 写下一块；下一次迭代两者互换。只要「内核处理一块的时间」≥「DMA 写一块的时间」，访存延迟就被完全隐藏，内核永远不用等数据。</details>

---

### 4.3 PL↔AIE 边界：PLIO 端口

#### 4.3.1 概念说明

AIE 阵列只认**无地址的 AXI Stream**（一串带数据的握手 beat），而 DDR/HBM 只能**按地址**访问。这两套协议之间有一道鸿沟。跨越这道鸿沟的位置就叫 **PL↔AIE 边界**，而边界上的「口岸」就是 **PLIO 端口**。

- **PLIO** 是 ADF 图在源码里声明的端口对象（`input_plio`/`output_plio`），它在物理上映射到 Versal 里 PL 与 AIE 阵列之间的物理流通道。
- 在 AIE 一侧，PLIO 直接连到某个 kernel 的 window/stream 端口；
- 在 PL 一侧，PLIO 连到一个 PL 内核（通常是 `mm2s`/`s2mm` 搬运器）的 `axis` 端口。

这条边界的「焊接」不是写在 C++ 里，而是写在**连接配置文件**（`system.cfg` 的 `[connectivity]` 段）里，用 `sc=`（stream connection）声明。

#### 4.3.2 核心流程

完整的「DDR → AIE → DDR」往返，跨两次边界：

```
              PL 侧                              AIE 侧
DDR ──m_axi──> mm2s ──axis──> [PLIO_in] ──> AIE 内核(window/stream)
                                                  │ (计算)
DDR <──m_axi── s2mm <──axis── [PLIO_out] <── AIE 内核(window/stream)
```

- **mm2s**（memory to stream）：把 DDR 的地址化数据，按 AIE 需要的并行度重排成多路 AXI Stream，推向 PLIO。
- **s2mm**（stream to memory）：把 AIE 吐出的多路流收回、重排成连续 DDR 布局，写回内存。
- **PLIO 宽度**（64 或 128 位）与 PLIO 数量（= SSR）共同决定边界带宽。

带宽可用一个简单公式估算。设 PLIO 位宽为 \(W\)、时钟频率为 \(f\)、单样本字节数为 \(s\)：

\[
\text{单端口带宽（字节/秒）} = \frac{W \cdot f}{8}, \qquad
\text{单端口样本率} = \frac{W \cdot f}{8s}
\]

本例 \(W=128\) 位、\(f=312.5\,\text{MHz}\)、`cint32` 样本 \(s=8\) 字节，则单端口样本率 \(= 128 \times 312.5\times10^6 / (8 \times 8) = 6.25\times10^8 = 625\,\text{Msamples/s}\)。SSR=4 时聚合 \(4 \times 625 = 2500\,\text{Msamples/s}\)（待本地验证，受核数/引脚约束）。

#### 4.3.3 源码精读

PLIO 在 AIE 一侧的声明已经在 4.1.3 看过（`aie.hpp` 第 58–61、96–97 行）。现在看 PL 一侧的搬运器，以及把它们焊起来的 `system.cfg`。

**PL 搬运器 `mm2s_wrapper` 的三段式接口：**

[dsp/L1/tests/hw/mm2s/mm2s.cpp:143-148](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/mm2s/mm2s.cpp#L143-L148) —— 这正是 u5-2 讲过的 AXI 三接口样本，集中体现了「地址世界↔流世界」的桥接：

```cpp
void mm2s_wrapper(TT_DATA mem[NITER][memSizeAct], TT_STREAM sig_o[NSTREAM_INT]) {
#pragma HLS interface m_axi port = mem bundle = gmem offset = slave depth = memSizeAct * NITER   // DDR 地址主端口
#pragma HLS interface axis port = sig_o                                                          // AIE 流端口
#pragma HLS interface s_axilite port = mem bundle = control                                      // 主机配置
#pragma HLS interface s_axilite port = return bundle = control
#pragma HLS DATAFLOW
```

- `m_axi`（`mem`）：带地址的 DDR 主端口，主机经它告诉 mm2s「从 DDR 哪里读」；
- `axis`（`sig_o`）：无地址的 AXI Stream，正是要与 PLIO 对接的那一路；
- `s_axilite`（`control`）：主机寄存器配置（u4-3 的 `set_arg` 最终落到这里）；
- `DATAFLOW`：让「DDR→BRAM」与「BRAM→流」两段并发，隐藏 DDR 延迟。

[dsp/L1/tests/hw/mm2s/mm2s.cpp:158-163](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/mm2s/mm2s.cpp#L158-L163) —— 单流时直接 `mm2s_str1`，多流时先 `load_buffer`（DDR→片上 BRAM 重排）再 `transmit`（BRAM→多路流），把连续 DDR 布局交错成 `NSTREAM_INT` 路并行流。

`mm2s.h` 给出了位宽与流数的全部定义：

[dsp/L1/tests/hw/mm2s/mm2s.h:76-82](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/mm2s/mm2s.h#L76-L82) —— `NBITS=128`（与 PLIO 宽度对齐）、`TT_DATA=ap_uint<128>`（一个 beat）、`samplesPerRead=NBITS/DATAWIDTH`（一个 beat 装几个样本）、`TT_SAMPLE=ap_uint<DATAWIDTH>`。

[dsp/L1/tests/hw/mm2s/mm2s.h:90](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/mm2s/mm2s.h#L90) —— `NSTREAM_INT`：输出流数，等于图侧的 SSR（`API_IO=1` 时翻倍为 `NSTREAM*2`，即 AIE1 的双流特性）。这就是「PL 流数 = AIE PLIO 数」的对齐依据。

**s2mm 是镜像**：接口同样是 `m_axi`/`axis`/`s_axilite`，只是流方向反过来——从 `axis` 收流、写到 `m_axi`：

[dsp/L1/tests/hw/s2mm/s2mm.cpp:120-127](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/s2mm/s2mm.cpp#L120-L127) —— `capture_streams`（流→BRAM）+ `read_buffer`（BRAM→DDR），把多路交错流还原成连续 DDR 布局。类型定义在 [s2mm.h:81-88](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/s2mm/s2mm.h#L81-L88)，与 mm2s 完全对称。

**把两侧焊起来的 `system.cfg`：**

[dsp/L2/examples/vss_fft_ifft_1d/system.cfg:10-11](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/system.cfg#L10-L11) —— `nk`（number of kernels）声明实例化 1 个 `mm2s_wrapper`（实例名 `mm2s`）与 1 个 `s2mm_wrapper`（实例名 `s2mm`）。

[dsp/L2/examples/vss_fft_ifft_1d/system.cfg:14](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/system.cfg#L14) 与 [:33](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/system.cfg#L33) —— `sp=`（streaming port）把 mm2s/s2mm 的 `mem`（m_axi）端口绑到 `LPDDR` bank，这正是 u4-3 主机 `group_id` 的源头。

[dsp/L2/examples/vss_fft_ifft_1d/system.cfg:23-26](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/system.cfg#L23-L26) —— **本讲最核心的边界焊点**：`sc =` 把 mm2s 的 4 路流输出 `mm2s.sig_o_0..3` 逐一接到 PL 转置核 `vss_fft_ifft_1d_front_transpose.sig_i_0..3`：

```
sc = mm2s.sig_o_0:vss_fft_ifft_1d_front_transpose.sig_i_0
sc = mm2s.sig_o_1:vss_fft_ifft_1d_front_transpose.sig_i_1
sc = mm2s.sig_o_2:vss_fft_ifft_1d_front_transpose.sig_i_2
sc = mm2s.sig_o_3:vss_fft_ifft_1d_front_transpose.sig_i_3
```

[dsp/L2/examples/vss_fft_ifft_1d/system.cfg:28-31](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/system.cfg#L28-L31) —— 反向：把后端转置核的 4 路流输出接到 s2mm 的 4 路输入。

> 完整路径其实是多跳的：`DDR → mm2s →(sc)→ front_transpose →(另一份生成的 connectivity cfg)→ AIE PLIO front_i → AIE FFT → AIE PLIO back_o →(sc)→ back_transpose →(sc)→ s2mm → DDR`。本例的 `system.cfg` 负责 PL↔PL 与 PL↔DDR 那几跳；转置核到 AIE PLIO 的那一跳，由构建脚本 `vss_fft_ifft_1d_con_gen.py` 生成的 `vss_fft_ifft_1d_connectivity.cfg` 描述（见 u6-3）。无论几跳，**跨越 PL↔AIE 边界的最后一跳，都是 `sc=` 把某个 PL 内核的 `axis` 端口与某个 PLIO 焊在一起**。

#### 4.3.4 代码实践

**实践目标**：在 `system.cfg` 上「数清楚」SSR=4 是如何体现为 4 条 `sc=` 的，并对应到 `aie.hpp` 的 PLIO 数量。

**操作步骤**：

1. 打开 `dsp/L2/examples/vss_fft_ifft_1d/system.cfg`，统计以 `sc = mm2s.sig_o_` 开头的行数（应为 4）和 `sc = .*s2mm.sig_i_` 的行数（应为 4）。
2. 回到 `aie.hpp` 第 87 行 `for (int i = 0; i < SSR; i++)`，确认每次循环建 4 个 PLIO。
3. 对照 `mm2s.h` 第 90 行 `NSTREAM_INT`，确认 mm2s 的输出流数 = SSR = PLIO 数。

**需要观察的现象**：

- 三处「4」自洽：`system.cfg` 的 4 条 `sc=` ↔ `aie.hpp` 的 4 个 PLIO ↔ `mm2s` 的 4 路输出流。
- 改 SSR 时这三处必须同步变化（否则会出现「PL 流数 ≠ AIE PLIO 数」的链接错误）。

**预期结果**：得到一张「`sc=` 行 ↔ PLIO 端口 ↔ mm2s 流路」三列对应表，证明边界两侧严格对齐。这是后续排查「PL↔AIE 端口数不匹配」类链接错误的基础（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 PL 侧用 `mm2s`/`s2mm`，而不能让 AIE 核直接读写 DDR？

<details><summary>参考答案</summary> AIE 核的计算端口只有无地址的流（stream/window），它本身没有「地址化访存」能力；而 DDR 是地址寻址的。`mm2s`/`s2mm` 是 PL（可编程逻辑）里的 HLS 内核，同时具备 `m_axi`（地址）与 `axis`（流）两种接口，专门做协议翻译与数据重排，是把「地址世界」与「流世界」接起来的桥。没有它，AIE 就够不着 DDR。</details>

**练习 2**：`sc = mm2s.sig_o_0:vss_fft_ifft_1d_front_transpose.sig_i_0` 里，两端点谁在 PL、谁在 AIE？

<details><summary>参考答案</summary> 两者其实都是 PL 内核：`mm2s` 是 PL 搬运器，`vss_fft_ifft_1d_front_transpose` 是 PL 转置核。这条 `sc=` 是 PL↔PL 的流连接。真正跨入 AIE 的是后续由生成脚本写出的、把转置核输出接到 AIE `PLIO_front_in_*` 的那条 `sc=`。这提醒我们：`sc=` 既可焊 PL↔PL，也可焊 PL↔AIE（PLIO），区分依据是端点对象类型。</details>

---

### 4.4 mm2s / s2mm 搬运器与 GMIO 对比

#### 4.4.1 概念说明

跨 PL↔AIE 边界把数据搬进搬出 DDR，主流有两种机制：

| 机制 | 谁来搬 | 是否经过 PL 内核 | 适用场景 |
| --- | --- | --- | --- |
| **PL 搬运器 mm2s/s2mm** | PL 里的 HLS 内核 | 是 | 需要在边界做数据重排/转置/分流（如 SSR 交错）；本仓库所有 AIE 示例采用此路 |
| **GMIO** | AIE 核自带的 DMA 引擎 | 否（直接 AIE↔DDR） | 简单线性访问、无需 PL 加工、想省 PL 资源 |

- **mm2s/s2mm 路线**：数据先到 PL，PL 内核可顺手做重排（如把连续 DDR 布局交错成 SSR 路流），再经 PLIO 进 AIE。优点是灵活、可定制（转置、合并、分流都在 PL 里做）；缺点是要消耗 PL 资源、且必须为每个 AIE 图都配一套搬运器。这正是 vss 示例「mm2s → AIE 图 → s2mm」夹心结构的由来。
- **GMIO 路线**：用 `adf::gmio` 在图里声明一个 GMIO 节点，`connect` 到某个 kernel 端口，AIE 编译器会自动生成 DMA 描述符，让 AIE 核直接发起 DDR 读写，不经过任何 PL 内核。优点是省 PL、配置简单；缺点是访问模式受限（通常是线性块），且不能在边界做重排。

> 工程选型一句话：**需要在边界加工数据 → mm2s/s2mm；只是线性喂入/收回 → GMIO**。

#### 4.4.2 核心流程

两种机制在源码与连接配置上的差别：

```
mm2s/s2mm 路线:
  graph 里: 声明 input_plio/output_plio, connect 到 kernel
  PL 里:    单独写/复用 mm2s.cpp、s2mm.cpp (HLS 内核)
  cfg 里:   nk 声明 mm2s/s2mm 实例；sc 把 mm2s.axis ↔ PLIO；sp 把 mm2s.m_axi ↔ DDR bank
  主机:     用 xrt::kernel 控制 mm2s/s2mm，用 xrt::graph 控制 AIE 图（两套控制对象）

GMIO 路线（本仓库示例未使用，此处为概念对照）:
  graph 里: 声明 gmio 节点, connect 到 kernel 端口
  PL 里:    无需 mm2s/s2mm 内核
  cfg 里:   无需为搬运器写 nk/sc；GMIO 的 DDR bank 由 gmio 声明中的参数指定
  主机:     用 xrt::bo + gmio 的 API 直接收发，控制对象更少
```

> 说明：GMIO 是 ADF 标准机制（`adf::gmio` / `gmio::create`），但本仓库的 AIE 示例统一走 mm2s/s2mm 路线，因此本仓库源码中**没有 GMIO 的实例**可引用；上表与上图为通用 ADF 知识的概念对照，便于读者在遇到 GMIO 例程时知道它与 mm2s/s2mm 的关系。如需 GMIO 的真实用例，请参考 AMD Vitis AIE 官方文档。

#### 4.4.3 源码精读

mm2s 的内部数据流——为什么要在 PL 里做「重排」：

[dsp/L1/tests/hw/mm2s/mm2s.cpp:30-34](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/mm2s/mm2s.cpp#L30-L34) —— `load_buffer` 把 DDR 里按地址连续存放的样本，按 SSR 交错规则写进 `buff[n][NSTREAM_INT][...]` 的二维片上缓冲。注释 `memSizeAct = 256; 2*memSizeAct/NSTREAM_INT = 128` 说明了「DDR 连续布局 → 多路分桶」的维度变换。

[dsp/L1/tests/hw/mm2s/mm2s.cpp:107-138](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/mm2s/mm2s.cpp#L107-L138) —— `transmit` 从二维缓冲逐路读出，按 `samplesPerRead` 把 2 或 4 个样本打包成一个 128 位 beat，写到对应的 `sig_o[ss]` 流。第 113 行 `#pragma HLS PIPELINE II=1` 保证每拍喂满所有 `NSTREAM_INT` 路。

这就是 mm2s 区别于 GMIO 的核心价值：**它在边界上完成了「连续 DDR ↔ SSR 交错多路流」的重排**，这正是 SSR FFT 图所要求的数据分布。GMIO 做不到这种任意重排，只能线性搬运。

s2mm 是对称的逆过程：

[dsp/L1/tests/hw/s2mm/s2mm.cpp:26-58](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/s2mm/s2mm.cpp#L26-L58) —— `capture_streams` 把多路交错流收进二维缓冲；[s2mm.cpp:64-102](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/s2mm/s2mm.cpp#L64-L102) —— `read_buffer` 把二维缓冲还原成连续 DDR 布局。

> 端到端看，`mm2s`+`s2mm` 这对搬运器与 `aie.hpp` 的 PLIO 是**一一绑定**的：mm2s 的 `NSTREAM_INT` 路输出 = 图的 `SSR` 个输入 PLIO。这是 SSR 参数「牵一发而动全身」的根因——改 SSR 要同时改 `mm2s.h`/`s2mm.h` 的 `NSTREAM`、`aie.hpp` 的 PLIO 数量、以及 `system.cfg`/生成 cfg 的 `sc=` 条数（u6-3 已强调过这一点）。

#### 4.4.4 代码实践

**实践目标**：以 SSR 为线索，画出「参数 → PL 流数 → PLIO 数 → `sc=` 条数」的一致性链路。

**操作步骤**：

1. 在 `vss_fft_ifft_1d_params.cfg` 第 20 行读出 `SSR=4`。
2. 在 `mm2s.h` 第 90 行读出 `NSTREAM_INT`（`API_IO=0` 时 = `NSTREAM` = SSR = 4）。
3. 在 `aie.hpp` 第 58–61 行数出每组 PLIO 数 = SSR = 4。
4. 在 `system.cfg` 第 23–26 行数出 mm2s 的 `sc=` 条数 = 4。

**需要观察的现象**：

- 这四处「4」环环相扣，构成 SSR 的完整表达。
- 若任意一处与其他不一致，`v++ --link` 会报端口数不匹配的错误。

**预期结果**：得到一条「`SSR` → `NSTREAM_INT` → PLIO 数 → `sc=` 数」的单值传播链，理解为何 SSR 是整个 AIE 系统的「主参数」。这也是把 SSR 从 4 改成 2 时必须同步检查的全部落点（待本地验证链接结果）。

#### 4.4.5 小练习与答案

**练习 1**：如果某 AIE 内核只需要线性地、不经任何重排地从 DDR 读一块数据，mm2s 与 GMIO 哪个更省资源？

<details><summary>参考答案</summary> GMIO 更省资源。GMIO 不需要在 PL 里实例化 mm2s HLS 内核（省 LUT/FF/BRAM 与一个 PL 时钟域），由 AIE 核自带的 DMA 直接搬。但代价是失去在边界做重排/转置/分流的能力。所以「不需要边界加工」是选 GMIO 的前提。</details>

**练习 2**：本仓库 vss 示例为什么坚持用 mm2s/s2mm 而不用 GMIO？

<reference answer> <details><summary>参考答案</summary> SSR FFT 要求输入按 SSR 路交错分布（每路喂一个 AIE 核的一个 PLIO），这是非线性重排。mm2s 的 `load_buffer`/`transmit` 正是为此而生：把连续 DDR 布局重排成 `NSTREAM_INT` 路交错流。GMIO 只能线性搬运、做不了这种交错分桶，故必须用 mm2s/s2mm。</details>

---

## 5. 综合实践

**任务**：为 `vss_fft_ifft_1d` 绘制一张完整的「PL↔AIE 边界数据通路图」，并在图上标注每一跳对应的源码位置。

**操作步骤**：

1. 从 [system.cfg](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/examples/vss_fft_ifft_1d/system.cfg) 的 `nk`/`sp`/`sc` 出发，列出参与系统的全部内核实例（mm2s、s2mm、front_transpose、back_transpose、AIE 图）。
2. 用箭头画出数据流：`DDR --m_axi--> mm2s --axis/sc--> front_transpose --PLIO--> AIE front_i --> (FFT) --> AIE back_o --PLIO--> back_transpose --axis/sc--> s2mm --m_axi--> DDR`。
3. 在每一跳旁标注它由哪一行代码/配置定义：
   - `mm2s.m_axi`：`mm2s.cpp:144`（`m_axi`）+ `system.cfg:14`（`sp`）；
   - `mm2s.axis`：`mm2s.cpp:145`（`axis`）；
   - `mm2s→transpose`：`system.cfg:23-26`（`sc`）；
   - `transpose→AIE PLIO`：生成 cfg（由 `vss_fft_ifft_1d_con_gen.py` 产出）；
   - `AIE PLIO`：`aie.hpp:58-61`（声明）、`aie.hpp:96-97`（create+connect）；
   - `AIE 内核 window`：`mixed_radix_fft.hpp:275-276`；
   - `transpose→s2mm`：`system.cfg:28-31`（`sc`）；
   - `s2mm.axis/m_axi`：`s2mm.cpp:123-124`、`system.cfg:33`（`sp`）。
4. 用一句话回答综合问题：**数据一共跨了几次 PL↔AIE 边界？分别在哪个 PLIO？**

**预期结果**：一张标注完整的通路图 + 答案「两次：进 AIE 在 `PLIO_front_in_*`，出 AIE 在 `PLIO_back_out_*`，各 SSR=4 个」（待本地验证板上报文）。

**延伸思考**：如果把 `AIE_PLIO_WIDTH` 从 128 改成 64（`aie.hpp:83`），边界带宽会如何变化？提示：套用 4.3.2 的公式，单端口带宽减半，要么接受吞吐减半，要么把 SSR 翻倍维持原带宽（但核数/引脚可能不够）。

## 6. 本讲小结

- **ADF 图 = graph + kernel + connect**：`tl_graph` 继承 `adf::graph`，在构造函数里实例化内部子图、用 `connect<>` 把 PLIO 转接到子图端口；图的生命周期由 `init()/run(n)/end()` 驱动。
- **PLIO 是图边缘的物理流端口**：`input_plio`/`output_plio` 数量由 SSR 决定，宽度由 `AIE_PLIO_WIDTH` 决定（64/128 位）；仿真时从文件读写，上板时映射为物理通道。
- **window vs stream 是内核级数据范式**：`input_buffer/output_buffer`（块处理，FFT/矩阵）对应 window 双缓冲；`input_stream/output_stream`（逐样本，FIR/DDS）对应流互连。判读依据就是形参类型。
- **PL↔AIE 边界靠 PLIO + sc= 焊接**：`mm2s`（DDR→流）与 `s2mm`（流→DDR）是 PL 侧的协议翻译器，同时具备 `m_axi`（地址）与 `axis`（流）接口；`system.cfg` 的 `sc=` 把它们的 `axis` 端口与 PLIO 连起来。
- **mm2s/s2mm 的核心价值是边界重排**：`load_buffer/transmit` 与 `capture_streams/read_buffer` 把连续 DDR 布局交错成 SSR 路流，这是 SSR 图必需的、GMIO 做不到的能力。
- **SSR 是贯穿三处的「主参数」**：`mm2s` 流数 = PLIO 数 = `sc=` 条数 = SSR，改 SSR 必须三处同步。

## 7. 下一步学习建议

- **u13-2 AIE 图主机控制与 SD 卡打包**：本讲的 `init/run/end` 在仿真下由 `aie.cpp` 的 `main()` 驱动；上板时则由主机的 `xrt::graph` 驱动。下一讲讲清 `xrt::graph` 的 `reset/run/end`、`system.cfg` 的完整连接描述，以及 `v++ --package` 如何把 AIE 图封进 SD 卡。
- **继续阅读源码**：建议精读 [vss_fft_ifft_1d_con_gen.py](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/include/vss/vss_fft_ifft_1d/vss_fft_ifft_1d_con_gen.py)，看它是如何根据 SSR 自动生成 PLIO↔转置核的 `sc=` 连接的——这是理解「图规模随 SSR 自动伸缩」的最佳入口。
- **横向对比**：回到 [fir_sr_asym.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/include/aie/fir_sr_asym.hpp) 的 `dsp/L2/tests/aie` 对应测试，观察一个纯 stream 内核的图是如何用 `kernel::create_object` + `connect` + `dimensions` 拼出来的，与本文 window 型 FFT 图对照，巩固「window vs stream」的判读能力。
