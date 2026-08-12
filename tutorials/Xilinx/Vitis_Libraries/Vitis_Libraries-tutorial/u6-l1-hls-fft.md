# L1 HLS FFT（vitis_fft / 2dfft）

## 1. 本讲目标

dsp（数字信号处理）库是 Vitis_Libraries 里数值计算的主力库之一，而 FFT（快速傅里叶变换）又是 dsp 库里最经典、文档最完整的 HLS（高层综合）内核。本讲以 dsp 库的 L1 HLS FFT 为切入点，带你理解「一个真实的加速库 HLS 内核长什么样、怎么用、怎么测」。

读完本讲，你应该能够：

- 说出 dsp L1 的 FFT 实现在仓库里的目录结构（`vitis_fft` / `vitis_2dfft`，以及 fixed / float 两套）。
- 看懂 FFT 内核对外的流式接口 `xf::dsp::fft::fft<参数结构体>(in[R], out[R])`，并理解「参数结构体继承默认参数」这一 Vitis 库通用设计模式。
- 解释 SSR（Super Sample Rate，超采样率）为什么能线性提升吞吐，以及它如何把一维向量重排成 `[R][L/R]` 的二维流数组。
- 区分一维 FFT 与二维 FFT 的接口差别，理解二维 FFT 就是「行方向 + 列方向」两次一维 FFT 的组合。
- 独立跑通一个 L1 FFT 用例（`make run TARGET=csim`），并读懂它的 `description.json` / `Makefile` / `testbench` 三件套。

本讲承接 u3-l2（HLS pragma 如何映射硬件）建立的 pipeline / dataflow / unroll 心智模型，以及 u2-l3（HLS TARGET 流程）建立的大写 TARGET 阶梯。

## 2. 前置知识

在进入源码前，先用最朴素的语言把几个概念讲清楚。

**傅里叶变换与 FFT。** 离散傅里叶变换（DFT）把一段长度为 \(N\) 的复数序列 \(x[n]\) 变成另一段长度为 \(N\) 的复数序列 \(X[k]\)：

\[
X[k] = \sum_{n=0}^{N-1} x[n] \cdot e^{-j 2\pi k n / N}, \quad k=0,1,\dots,N-1
\]

直接按定义算是 \(O(N^2)\)；FFT（Fast Fourier Transform）利用 \(e^{-j2\pi/N}\) 的周期性把它降到 \(O(N\log N)\)。FFT 是几乎所有信号处理（滤波、频谱分析、卷积、压缩）的底层算子，所以它值得做成一个高度优化的硬件内核。

**蝶形（butterfly）与基（radix）。** FFT 由若干「蝶形」运算级联而成。一个 radix-\(R\)（基 \(R\)）蝶形一次吃进 \(R\) 个复数、吐出 \(R\) 个复数。如果 \(N = R^m\)，那么整棵 FFT 就是 \(m\) 级 radix-\(R\) 蝶形。本讲的 SSR 因子 \(R\) 同时扮演「基」和「每周期并行样本数」两个角色。

**HLS 与流式内核。** 复习 u3-l1/u3-l2：HLS 把 C++ 编译成 RTL；`hls::stream` 是单向 FIFO，强制顺序、单次读写，是映射 II=1 流水的关键。本讲的 FFT 内核的输入输出就是「一组并行的 `hls::stream`」。

**复数类型。** FFT 永远在复数域上算。Vitis 库用一个自带的 `complex_wrapper<T>`（行为类似 `std::complex<T>`）承载复数，其中 `T` 在 float 路线下是 `float`，在 fixed 路线下是 `ap_fixed<W,I>`（任意位宽定点数）。

**SSR（Super Sample Rate）。** 这是本讲的核心概念，先记住一句话：**SSR 就是「每个时钟周期并行吃进/吐出 R 个复数样本」**。普通 FFT 内核一周期处理 1 个样本，SSR 内核一周期处理 R 个，所以理论吞吐近似线性放大 R 倍。代价是面积（要复制 R 套运算通路）和对输入数据排布的硬性要求——这是 4.2 节的重点。

## 3. 本讲源码地图

本讲涉及的源码都在 `dsp/L1/` 下，分为「内核头件」和「测试用例」两大块：

| 路径 | 作用 |
|------|------|
| `dsp/L1/README.md` | L1 模块总览，说明这些是「给硬件开发者的 HLS C++ 模块」。 |
| `dsp/L1/include/hw/vitis_fft/` | **一维 FFT 内核头件**，分 `fixed/` 与 `float/` 两套，是本讲主角。 |
| `dsp/L1/include/hw/vitis_fft/fixed/vt_fft.hpp` | fixed 路线的对外入口头（仅 re-export `hls_ssr_fft.hpp`）。 |
| `dsp/L1/include/hw/vitis_fft/fixed/vitis_fft/hls_ssr_fft.hpp` | SSR FFT 核心实现（含对外 `fft()` 函数），约 3460 行。 |
| `dsp/L1/include/hw/vitis_fft/fixed/vitis_fft/hls_ssr_fft_enums.hpp` | 枚举与默认参数结构体 `ssr_fft_default_params`。 |
| `dsp/L1/include/hw/vitis_2dfft/` | **二维 FFT 内核头件**，同样分 `fixed/` 与 `float/`。 |
| `dsp/L1/include/hw/vitis_2dfft/fixed/vitis_fft/hls_ssr_fft_2d.hpp` | 二维 FFT 的对外 `fft2d()` 函数。 |
| `dsp/L1/tests/hw/1dfft/` | 一维 FFT 的 L1 测试，`README.md` 解释 SSR 数据排布，`float|fixed/fft_1d_snr/` 下是各变体用例。 |
| `dsp/L1/tests/hw/2dfft/` | 二维 FFT 的 L1 测试，含 `2dfft_snr_tests/` 与 `impulse_test/`。 |
| `dsp/L1/tests/hw/ssr_fft/` | 一个较老、独立的 SSR FFT 测试目录（见 4.4 的提醒）。 |
| `dsp/L1/tests/common_float/verif/` | 跨用例共享的激励与黄金输出文件（如 `fftStimulusIn_L16.verif`）。 |

> 注意：`vitis_fft` 与 `vitis_2dfft` 是**两个独立的库目录**，各自有 fixed/float 两套；二维库内部会复用一维库的实现，所以它们的 `vitis_fft/` 子目录结构几乎一致，只是 `vitis_2dfft` 额外多了 `hls_ssr_fft_2d.hpp` 与 `hls_ssr_fft_matrix_commutors.hpp` 等二维专属文件。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：4.1 vitis_fft 内核与对外 API；4.2 SSR 并行；4.3 二维 FFT；4.4 L1 测试流程。

### 4.1 vitis_fft 内核：目录结构与对外 API

#### 4.1.1 概念说明

`vitis_fft` 是 dsp 库提供的一维 SSR FFT 内核库。它的核心设计有三点：

1. **头件即库**：整个内核就是一组 C++ 模板头件（header-only），使用者 `#include` 进来即可，没有要单独编译的 `.cpp`/`.a`。
2. **fixed / float 双路线**：`vitis_fft/fixed/` 和 `vitis_fft/float/` 是两份近乎同源的实现，区别只在数据类型（`ap_fixed` vs `float`）。这对应「定点（省资源、可控精度）」与「浮点（高精度、费资源）」两条硬件路线。
3. **「参数结构体」配置法**：内核的所有可调参数（FFT 长度 \(N\)、SSR 因子 \(R\)、缩放模式、输出顺序、变换方向……）都塞进一个 struct，调用时把这个 struct 当模板参数传进去。

#### 4.1.2 核心流程

调用一维 SSR FFT 的典型流程是「三步」：

1. `#include "vt_fft.hpp"`（`vt_fft.hpp` 只是把真正的 `hls_ssr_fft.hpp` re-export 出来）。
2. 定义自己的参数结构体，**继承库里的 `ssr_fft_default_params`**，只改写关心的几个成员（典型的就是 `N`、`R`、`scaling_mode`、`output_data_order`）。
3. 在顶层函数里调用 `xf::dsp::fft::fft<你的参数结构体>(输入流数组, 输出流数组)`。

这里的关键直觉是：**默认参数结构体是「基类」，用户参数结构体是「覆盖了若干静态常量的派生类」**。这是一种用 C++ 模板模拟「带默认值的命名参数」的经典手法——你不写的字段就沿用默认值，写过的字段就覆盖。

#### 4.1.3 源码精读

先看入口头 `vt_fft.hpp`，它极薄，只做一件事——把真正的实现头暴露出来：

[vt_fft.hpp 仅 re-export 实现头](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/include/hw/vitis_fft/fixed/vt_fft.hpp#L20-L25) —— 这就是用户唯一需要 include 的文件。

默认参数结构体定义在 `hls_ssr_fft_enums.hpp`，它和几个枚举紧挨在一起，是理解所有参数含义的「字典」：

[枚举与 ssr_fft_default_params](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/include/hw/vitis_fft/fixed/vitis_fft/hls_ssr_fft_enums.hpp#L71-L89)。逐行对照：

- `scaling_mode_enum`：三种缩放模式。`SSR_FFT_NO_SCALING`（不缩放，逐级位增长）、`SSR_FFT_GROW_TO_MAX_WIDTH`（位增长到 DSP48 乘法器输入上限 27 位即止）、`SSR_FFT_SCALE`（每级缩放，无净增长）。
- `fft_output_order_enum`：`SSR_FFT_NATURAL`（自然序输出，内部多做一次重排）、`SSR_FFT_DIGIT_REVERSED_TRANSPOSED`（位反序/转置输出，省资源但顺序需要外部处理）。
- `transform_direction_enum`：`FORWARD_TRANSFORM`（FFT）/`REVERSE_TRANSFORM`（IFFT，逆变换）。
- `ssr_fft_default_params`：默认 \(N=1024\)、\(R=4\)、不缩放、自然序、正变换。这就是「你不写就生效」的默认值。

对外 API 是 `hls_ssr_fft.hpp` 末尾的 `fft()` 模板函数，注释里把整套默认参数也复述了一遍，函数签名很关键：

[对外 fft() 函数与默认参数说明](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/include/hw/vitis_fft/fixed/vitis_fft/hls_ssr_fft.hpp#L3284-L3355)。读这段代码可以看到：

- 模板参数 `<typename ssr_fft_param_struct, typename T_in>`——前者是你定义的参数结构体，后者是输入复数类型（由你传入的流自动推导，不必显式写）。
- 输入 `hls::stream<T_in> fftInStrm[R]`、输出 `hls::stream<...OutType> fftOutStrm[R]`——**R 条并行的流**，这正是 SSR 的体现（4.2 详解）。
- 函数体把参数结构体里的静态常量一一取出，做一次运行期参数校验 `checkFFTparams`，再用两条编译期断言强制「\(R\) 必须是 2 的幂、\(N\) 必须是 2 的幂」：

[强制 R 与 N 为 2 的幂](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/include/hw/vitis_fft/fixed/vitis_fft/hls_ssr_fft.hpp#L3342-L3345)。

最后它根据「\(N\) 是否为 \(R\) 的整数幂」「\(N\) 是否小于 \(R^2\)」选择一个 `FFTWrapper` 内部实现并调用。这说明库内部对「规整长度」和「一般长度」做了分支处理，但**对外 API 始终是同一个 `fft()`**，使用者无需关心。

#### 4.1.4 代码实践（阅读型）

**目标**：把「参数结构体 → 对外 API」这条链用源码自己走一遍。

**步骤**：

1. 打开 `dsp/L1/include/hw/vitis_fft/fixed/vitis_fft/hls_ssr_fft_enums.hpp`，把 `ssr_fft_default_params` 里每个字段抄下来。
2. 打开 `dsp/L1/tests/hw/1dfft/float/fft_1d_snr/testsfft_natural_order/ssr_fft_r2_l16/src/hls_ssr_fft_data_path.hpp`，看它如何定义一个继承默认参数、只改 `N`/`R`/`scaling_mode`/`output_data_order` 的 `ssr_fft_params`：

[用例里继承默认参数定义 ssr_fft_params](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/1dfft/float/fft_1d_snr/testsfft_natural_order/ssr_fft_r2_l16/src/hls_ssr_fft_data_path.hpp#L77-L103)。

3. 对照阅读：这个用例覆盖了哪些字段、保留了哪些默认值？

**需要观察的现象**：用例只显式写了 `N`、`R`、`scaling_mode`、`twiddle_table_word_length`、`twiddle_table_intger_part_length`、`output_data_order`、`default_t_instanceID`；而 `transform_direction`、`butterfly_rnd_mode` 没写，因此沿用默认的 `FORWARD_TRANSFORM`、`TRN`。

**预期结果**：你能不查文档说出该用例「\(N=16, R=2\)，输出自然序，正向 FFT」。

#### 4.1.5 小练习与答案

**练习 1**：如果我想要一个「逆变换（IFFT）」的内核，应该改哪个字段？
**答**：在自定义参数结构体里加 `static const transform_direction_enum transform_direction = REVERSE_TRANSFORM;` 覆盖默认的 `FORWARD_TRANSFORM`，其余调用方式不变。

**练习 2**：`SSR_FFT_NATURAL` 与 `SSR_FFT_DIGIT_REVERSED_TRANSPOSED` 各自的取舍是什么？
**答**：自然序输出对外更直观（输出顺序与教科书一致），但内核要多做一次 digit-reversed 重排、费资源；位反序输出省掉这次重排、更省资源，但输出顺序被打乱，需要使用者在内核外自行重排。

---

### 4.2 SSR 并行：超采样率如何提升吞吐

#### 4.2.1 概念说明

SSR（Super Sample Rate，超采样率）是这个 FFT 内核之所以「快」的根本。普通流式 FFT 每个时钟周期吞吐 1 个复数样本；SSR FFT 每个时钟周期吞吐 \(R\) 个复数样本。因此若时钟频率为 \(f_{clk}\)、II=1，则：

\[
\text{吞吐} = R \times f_{clk} \quad \text{(样本/秒)}
\]

处理一帧 \(N\) 点所需周期数近似为：

\[
\text{延迟} \approx \frac{N}{R} + \text{流水线填充深度} \quad \text{(周期)}
\]

\(R\) 越大，吞吐越高、单帧延迟越短，但代价是面积（需要 \(R\) 套并行运算通路）和对输入数据排布的硬性约束。

#### 4.2.2 核心流程：数据的二维重排

SSR 的「副作用」是：一维输入向量必须先重排成一个 \(R \times (N/R)\) 的二维数组（或等价的 \(R\) 条流），让内核每周期从「每条流各取 1 个」凑齐 \(R\) 个并行样本。仓库自带的 README 用一个 16 点、SSR=2 的例子讲得很清楚：

[1dfft README 解释 SSR 数据排布](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/1dfft/README.md#L1-L31)。

把它的例子抽象成公式：一维输入 \(x[0\dots N{-}1]\) 按列优先填入 `inData[row][col]`：

\[
\texttt{inData}[j][i] = x[i \cdot R + j], \quad j\in[0,R),\ i\in[0,N/R)
\]

即第 \(j\) 条流（行 \(j\)）拿到的，是原向量里「下标 \(\bmod R = j\)」的那些样本。输出同理按相同排布。这样每周期从 \(R\) 条流各读 1 个，恰好凑出 \(R\) 个样本。

内核内部如何把「\(R\) 条窄流」聚成「一个周期 \(R\) 个样本」？靠一个 `SuperSampleContainer<R,T>` 的宽容器，由 `castArrayS2Streaming` 完成装配：

[castArrayS2Streaming：把 R 条流每周期聚成一个 super sample](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/include/hw/vitis_fft/fixed/vitis_fft/hls_ssr_fft.hpp#L70-L84)。注意外层循环 `PIPELINE II=1 rewind`、内层 `UNROLL` 把 \(R\) 路读取展开——这就是「每周期 \(R\) 个样本」在代码里的落点。

#### 4.2.3 核心流程：单级蝶形内核与递归级联

SSR FFT 的运算主体是「蝶形级（butterfly stage）」。单级内核 `fftStageKernelS2S` 每周期读一个 super sample（\(R\) 个样本）、做 radix-\(R\) 蝶形、乘旋转因子、再写回一个 super sample：

[fftStageKernelS2S：单级蝶形内核，II=1、对 R 展开](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/include/hw/vitis_fft/fixed/vitis_fft/hls_ssr_fft.hpp#L149-L190)。看这段能复习 u3-l2 的三个 pragma：`PIPELINE II=1 rewind`（决定吞吐）、对 `R` 的 `UNROLL`（决定并行度）、以及隐含的 dataflow（级与级之间靠 `hls::stream` 串接）。

多个蝶形级如何拼成完整 FFT？靠递归模板类 `FFTStageClassS2S`，每一级「算蝶形 → 数据换位（data commutor）→ 递归进入下一级」，级间用 `hls::stream` 连接、整段打 `#pragma HLS dataflow`：

[FFTStageClassS2S::fftStage 用 dataflow 串起级间流水](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/include/hw/vitis_fft/fixed/vitis_fft/hls_ssr_fft.hpp#L497-L541)。这就是「任务级流水」：前一级在算第 \(k\) 帧时，后一级在算第 \(k{-}1\) 帧，端到端吞吐由最慢一级决定（复习 u3-l2/u5-l3）。

> 这部分代码量很大（全文件约 3460 行），不必逐行读。抓住「`fft()` → `FFTWrapper` → 递归 `FFTStageClassS2S` → `fftStageKernelS2S` 蝶形」这条主链即可。

#### 4.2.4 代码实践（阅读 + 推演型）

**目标**：用两个真实用例定量理解「SSR 决定并行度」。

**步骤**：

1. 看 `r2_l16` 用例的 `fft_size.hpp`：`SSR_FFT_L=16, SSR_FFT_R=2`：

[r2_l16 的 N 与 R](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/1dfft/float/fft_1d_snr/testsfft_natural_order/ssr_fft_r2_l16/src/fft_size.hpp#L18-L22)。

2. 同目录树下的另一个用例 `testsfft_natural_order/ssr_fft_r16_l4096`（同名文件里是 `SSR_FFT_L=4096, SSR_FFT_R=16`）。
3. 推演：`r2_l16` 每周期 2 个样本、需要 \(16/2=8\) 个数据周期；`r16_l4096` 每周期 16 个样本、需要 \(4096/16=256\) 个数据周期。

**需要观察的现象 / 预期结果**：把下表填出来——

| 用例 | \(N\) | \(R\) (SSR) | 并行流条数 | 每帧纯数据周期数 \(\approx N/R\) |
|------|------|------|-----------|------|
| `ssr_fft_r2_l16` | 16 | 2 | 2 | 8 |
| `ssr_fft_r16_l4096` | 4096 | 16 | 16 | 256 |

**结论**：SSR \(R\) 既等于「并行流条数」，也等于「每周期样本数」；翻倍 \(R\) 近似翻倍吞吐、减半单帧延迟，但要求输入按 \(R\) 路交织排布、且消耗约 \(R\) 倍运算面积。实际能否跑出线性加速比，待本地 csynth/cosim 验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么不直接把 \(R\) 设成最大值（例如 \(R=N\)）？
**答**：因为面积和布线代价随 \(R\) 近似线性甚至超线性增长——\(R\) 套并行蝶形、\(R\) 条流、更宽的 super sample 都要占资源；同时 \(N\) 必须能被 \(R\) 整除且二者都是 2 的幂。工程上要在「目标吞吐」与「资源/时序收敛」之间折中。

**练习 2**：`SuperSampleContainer<R,T>` 的作用是什么？
**答**：它是一个把 \(R\) 个 `T` 打包在一起的宽容器，代表「一个周期内并行的 \(R\) 个样本」。它让内核主循环每周期读写「一个 super sample」而非 \(R\) 次单独读写，便于 HLS 综合成满足 II=1 的宽通路。

---

### 4.3 二维 FFT：行/列两级一维 FFT 的组合

#### 4.3.1 概念说明

二维 FFT 用于图像、阵列信号等二维数据的频域分析。它的数学定义是对二维序列先沿一个轴、再沿另一个轴各做一次一维 FFT：

\[
X[k_1,k_2] = \sum_{n_1}\sum_{n_2} x[n_1,n_2]\, e^{-j2\pi k_1 n_1/N_1}\, e^{-j2\pi k_2 n_2/N_2}
\]

Vitis 的实现思路正是「分两趟」：先对矩阵的每一行做一维 FFT，再对（行 FFT 结果矩阵的）每一列做一维 FFT。这与一维 SSR FFT 完全同构，只是**外层多了一层「行核 + 列核」的编排**。

#### 4.3.2 核心流程

二维库 `vitis_2dfft` 在一维库基础上新增了 `fft2d()`。它的接口与一维的 `fft()` 有两点显著不同：

- **宽流接口**：输入输出是「存储器宽度（memWidth）的宽流」，一条宽流里打包了 `memWidth` 个复数，便于对接 DDR/HBM 的宽 AXI 端口（与 u3-l3 讲的 axi↔stream 位宽适配一脉相承）。
- **两套参数结构体**：一套给「行方向」一维核（`t_ssrFFTParamsRowProc`），一套给「列方向」一维核（`t_ssrFFTParamsColProc`）。

`fft2d` 内部构造一个 `FFT2d` 对象，由它的 `fft2dProc` 完成实际的「行 FFT → 矩阵换位 → 列 FFT」数据流。

#### 4.3.3 源码精读

`fft2d()` 的对外签名与文档（参数含义）在这里：

[fft2d() 对外函数与模板参数说明](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/include/hw/vitis_2dfft/fixed/vitis_fft/hls_ssr_fft_2d.hpp#L284-L325)。模板参数包括：宽流并行度 `t_memWidth`、行/列数、行/列方向的 1D 核数、行/列各自的 SSR FFT 参数结构体、行/列实例 ID 偏移、元素类型。

一个最小二维用例（16×16）如何配置这些参数，看测试头件最直观：

[2dfft 用例配置 FFTParams（行）与 FFTParams2（列）](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/2dfft/float/2dfft_snr_tests/test_16x16/src/top_2d_fft_test.hpp#L44-L55)。可以看到行核与列核都是 \(N=16, R=4\)、`SSR_FFT_SCALE`、正向变换——二维库把两套一维参数结构体拼到了一起。

顶层函数 `top_fft2d` 把这些常量传给 `fft2d`：

[top_fft2d 调用 fft2d](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/2dfft/float/2dfft_snr_tests/test_16x16/src/top_2d_fft_test.cpp#L19-L52)。注意它的输入输出是 `MemWideIFStreamTypeIn/Out`（宽流），且带 `ap_ctrl_none`——这是一个自由运行的流式内核，靠流的「数据到来/被取走」驱动，而非靠寄存器握手启动。

二维 FFT 的输入输出排布约定见 README：

[2dfft README 说明排布](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/2dfft/README.md#L1-L3)。要点是：SSR 的交织由二维模块内部处理，外部输入矩阵的顺序与传统二维 FFT 相同。

#### 4.3.4 代码实践（阅读型）

**目标**：理清「二维 = 行 FFT + 列 FFT」的参数对应关系。

**步骤**：打开 `top_2d_fft_test.hpp`，回答：宽流 `k_memWidth` 等于几个复数？`k_numOfKernels` 怎么由 `k_memWidth` 和 `k_fftKernelRadix` 算出来？行核与列核的 \(N\)、\(R\) 分别是多少？

**预期结果**（由源码常量推得）：`k_memWidthBits=512`，每个 `complex<float>` 占 8 字节 = 64 位，故 `k_memWidth = 512/64 = 8`（一条宽流 8 个复数）；`k_fftKernelRadix=4`；`k_numOfKernels = 8/4 = 2`（行/列方向各 2 个 1D 核）；行、列核均为 \(N=16, R=4\)。这些值的实际硬件效果待本地综合验证。

#### 4.3.5 小练习与答案

**练习**：为什么二维核需要「行实例 ID 偏移」和「列实例 ID 偏移」两个不同的偏移量？
**答**：SSR FFT 内核用「实例 ID」给内部的静态存储（如旋转因子表、twiddle ROM）生成唯一命名，避免同一设计里多个 FFT 实例的静态资源撞名。二维核一次实例化了很多个一维行核与一维列核，需要用不同区段的实例 ID 偏移保证行/列核群各自内部唯一、且彼此不冲突。

---

### 4.4 L1 测试流程：跑通一个一维 FFT 用例

#### 4.4.1 概念说明

dsp 库的 L1 FFT 测试沿用 u2-l2/u2-l3 建立的「目录即用例 + 大写 TARGET」约定：每个用例目录里有一套 `Makefile` / `description.json` / `hls_config.tmpl`（或 `run_hls.tcl`）加上 `src/` 源码，用 `make run TARGET=csim` 即可跑纯软件仿真。FFT 用例的特色是：**测试台自带一个「双精度浮点参考模型」，用 SNR（信噪比）和误差样本比例两道阈值判定 PASS/FAIL**——因为定点/浮点硬件结果不是 bit 精确，必须用统计指标比对。

#### 4.4.2 核心流程

一维 FFT 的 SNR 测试目录树长这样（以 float 为例）：

```
dsp/L1/tests/hw/1dfft/float/fft_1d_snr/
├── template_src/                      # 共享模板源（main.cpp / data_path 等）
├── testsfft_natural_order/            # 自然序输出
│   ├── ssr_fft_r2_l16/                # R=2, L=16
│   └── ssr_fft_r16_l4096/             # R=16, L=4096
└── testsfft_digit_reversed_order/     # 位反序输出（dro）
    ├── ssr_fft_dro_r2_l16/
    └── ssr_fft_dro_r16_l4096/
```

**用例命名规则**：`ssr_fft[_dro]_r{R}_l{L}`，其中 `_dro` 表示 digit reversed order（位反序输出），`r{R}` 是 SSR/基，`l{L}` 是 FFT 长度。fixed 路线下的目录结构完全对称。

一个用例的标准文件分工：

- `src/fft_size.hpp`：只定义两个宏 `SSR_FFT_L`（长度）和 `SSR_FFT_R`（SSR/基），是「换参数」的开关。
- `src/hls_ssr_fft_data_path.hpp`：include `vt_fft.hpp`，定义参数结构体与复数类型。
- `src/main.cpp`：一人分饰两角——顶层 `fft_top`（带 `#pragma HLS TOP`，会被综合）和 `main` 测试台（被 `#ifndef __SYNTHESIS__` 包裹，仅仿真）。
- `description.json`：用例元数据（flow、平台白名单、顶层函数、时钟、各阶段资源/时间限额）。
- `Makefile`：把 `make run TARGET=<csim|csynth|...>` 翻译成 `vitis-run --mode hls`。

判定逻辑：`main` 读取仓库共享的激励文件（如 `fftStimulusIn_L16.verif`）和黄金输出（`fftGoldenOut_L16.verif`），把同一帧数据分别喂给「双精度参考模型」和「待测单精度/定点模型」，计算 SNR，并用 `DEBUG_CONSTANTS.hpp` 里的两个阈值（`MAX_PERCENT_ERROR_IN_SAMPLE`、`MAX_ALLOWED_PERCENTAGE_OF_SAMPLES_IN_ERROR`）判定通过与否。

#### 4.4.3 源码精读

先看顶层封装与对外 API 的对接点（`main.cpp` 的前几行）：

[fft_top：把 fft<ssr_fft_params> 包成顶层函数](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/1dfft/float/fft_1d_snr/testsfft_natural_order/ssr_fft_r2_l16/src/main.cpp#L29-L36)。`fft_top` 用 `hls::stream<T>[SSR_FFT_R]` 接口包住 `xf::dsp::fft::fft<ssr_fft_params>(...)`——这就是「把模板库函数钉成一个 extern 风格顶层、供 HLS 综合」的标准手法（复习 u3-l1 的 DUT 封装）。

再看测试台如何按 SSR 排布往 R 条流里喂数据（`main.cpp` 中段）：

[main 按 SSR 列优先排布写入 R 条流](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/1dfft/float/fft_1d_snr/testsfft_natural_order/ssr_fft_r2_l16/src/main.cpp#L111-L121)。注意双重循环：外层 `i` 走列、内层 `j` 走行，把一维 `din_file[i*R+j]` 写进第 `j` 条流——正是 4.2 推导的 \( \texttt{inData}[j][i]=x[iR+j] \) 排布。

用例元数据（`description.json`）声明了 flow、平台、顶层函数、时钟：

[description.json 关键字段](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/1dfft/float/fft_1d_snr/testsfft_natural_order/ssr_fft_r2_l16/description.json#L1-L14)。要点：`flow=hls`、`platform_allowlist=["vck190"]`、`topfunction=fft_top`、`clock=3.3`。这告诉 CI（复习 u1-l2 的 Jenkinsfile）这个用例走 HLS 流程、只在 vck190 上跑、综合顶层是 `fft_top`。

Makefile 的核心两段（复习 u2-l3）：`all` 阶段在非 csim 时调用 `v++ -c --mode hls` 综合，`run` 阶段调用 `vitis-run --mode hls --<TARGET>` 仿真：

[Makefile 的 all/run 目标](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/tests/hw/1dfft/float/fft_1d_snr/testsfft_natural_order/ssr_fft_r2_l16/Makefile#L178-L187)。

#### 4.4.4 代码实践（运行型，本讲主实践）

**目标**：跑通 `ssr_fft_r2_l16` 的 csim，记录输入/输出位宽与点数，并解释 SSR 对并行度的影响。

**前置条件**：先按 u2-l1 的方法 `source` 好 Vitis 与 XRT 环境脚本，确保 `vitis-run`、`v++`、`vivado` 可用。本用例 `description.json` 默认平台是 `xilinx_vck190_base_202610_1`；csim 是纯软件仿真，主要需要一个 part 号，可用 `PLATFORM` 指向 vck190 平台，或直接用 `XPART` 指定一个 Versal part。

**操作步骤**：

```bash
cd dsp/L1/tests/hw/1dfft/float/fft_1d_snr/testsfft_natural_order/ssr_fft_r2_l16
make run TARGET=csim PLATFORM=<你的 vck190 平台名或 .xpfm 路径>
# 或用 XPART 跳过平台解析：
# make run TARGET=csim XPART=<Versal part 号>
```

**需要观察的现象**：

1. csim 会在工作目录 `hls/` 下编译并执行 `main`，终端打印若干段「Verification Messages」，最后给出 SNR（dB）与 `PASSED` / `FAILED`。
2. 因为这是 float 路线，输入/输出复数类型是 `complex<float>`：实部、虚部各 32 位，合计 **每样本 64 位**；FFT 点数 **\(N=16\)**；SSR **\(R=2\)**，即 **2 条并行流**。
3. （对照 fixed 路线的同名用例）fixed 路线输入是 `complex<ap_fixed<27,10>>`，每样本 54 位；输出类型由 `ssr_fft_output_type` 按缩放模式推导。

**预期结果**：csim 结束应打印类似 `OVERL ALL Simulation was SUCCESSFULL Done with L=16 R=2` 与一个正的 SNR（dB）值，并以返回码 0 退出。具体 SNR 数值与是否 PASS **待本地验证**（取决于本机 Vitis 版本与编译环境）。

**解释 SSR 如何影响并行度**：本用例 \(R=2\)，内核每周期从 2 条流各取 1 个样本、共处理 2 个复数，16 点 FFT 的纯数据搬运只需 \(16/2=8\) 个有效周期；若改跑 `ssr_fft_r16_l4096`（\(R=16\)），则每周期处理 16 个样本，理论吞吐再翻 8 倍（相对 \(R=2\)），但需要 16 条流、约 8 倍运算面积。

> **关于 `dsp/L1/tests/hw/ssr_fft/` 目录的提醒**：仓库里还有一个独立的 `ssr_fft/` 测试目录，但它与上面规范的 `1dfft` 用例**不是一回事**——它的 `test_ssr_fft.cpp` include 了一个该目录里并不存在的 `ssr_fft.h`，且其 `description.json` 内容陈旧（名为 "IFFT Back Transpose Test"、引用了不存在的 `test_ifft_dma_snk.cpp`、且 `"disable": true`）。因此该目录**不会在 CI 中运行、也无法按现状直接 `make run`**。学习与实践请以上面的 `1dfft/fft_1d_snr/...` 规范用例为准，把 `ssr_fft/` 视为历史遗留。

#### 4.4.5 小练习与答案

**练习 1**：为什么 FFT 测试用 SNR 而不是逐 bit 比对？
**答**：因为 FFT 尤其是定点实现涉及旋转因子量化、蝶形位增长与缩放舍入，输出不是 bit 精确的；逐 bit 比对几乎必然失败。SNR 把「整体误差能量」压缩成一个标量（dB），配合「误差样本比例上限」能稳健地判定「精度是否达标」。

**练习 2**：要把这个 16 点用例改成 1024 点、SSR=4，最少改哪些地方？
**答**：最简单是新建一个用例目录，把 `src/fft_size.hpp` 改成 `SSR_FFT_L=1024`、`SSR_FFT_R=4`，并确保 `common_float/verif/` 下有对应的 `fftStimulusIn_L1024.verif` / `fftGoldenOut_L1024.verif`（仓库里确实提供了一组从 L2 到 L32768 的黄金文件）；`description.json` / `hls_config.tmpl` 里引用的文件名也要相应改成 `_L1024`。

---

## 5. 综合实践

**任务**：把「换一个 SSR 参数」的全流程自己走一遍，验证你对 SSR、参数结构体、用例三件套的理解。

1. **复制用例**：把 `dsp/L1/tests/hw/1dfft/float/fft_1d_snr/testsfft_natural_order/ssr_fft_r2_l16/` 整个拷成一个新目录（例如 `my_ssr_fft_r4_l16`）。注意：这是你自己的学习副本，**不要修改仓库原始用例**；如需落盘请放在本讲义目录或你的沙箱里。
2. **改参数**：在新目录的 `src/fft_size.hpp` 里把 `SSR_FFT_R` 从 2 改成 4（`SSR_FFT_L` 保持 16）。注意 \(N=16\)、\(R=4\) 仍满足「都是 2 的幂且 \(N\) 是 \(R\) 的整数幂」。
3. **推演**：动手前先写下你预期的并行流条数（应为 4）、每帧纯数据周期数（应为 \(16/4=4\)）、以及测试台里 `main` 的双重循环是否还能正确把一维 `din_file` 按 \(R=4\) 交织写入 4 条流（提示：循环用的是 `SSR_FFT_R`，所以会自动适配）。
4. **运行**：`make run TARGET=csim`（注意 `description.json` 里的 `name`/`project` 字段可顺手改成新名字，避免与原用例工程目录冲突）。观察 SNR 是否仍 PASS。
5. **反思**：把 SSR 从 2 提到 4 后，csim 的功能结果应几乎不变（SNR 仍高），但若进一步跑 `TARGET=csynth`，对比两份综合报告里的资源（尤其 DSP48、LUT）与 latency，应能看到面积上升、单帧周期下降——这就定量印证了「SSR 用面积换吞吐」。

> 如果没有 Vitis 环境，步骤 1–3 仍可完成（纯文件编辑与推演），步骤 4–5 标注为「待本地验证」。

## 6. 本讲小结

- dsp L1 的 FFT 由两个独立库承载：一维 `vitis_fft`、二维 `vitis_2dfft`，各自有 fixed/float 双路线，全部是 header-only 模板。
- 对外 API 是 `xf::dsp::fft::fft<参数结构体>(in[R], out[R])`；配置靠「继承 `ssr_fft_default_params`、覆盖若干静态常量」这一通用模式，默认 \(N=1024, R=4\)、自然序、正变换。
- SSR（超采样率）= 每周期并行 \(R\) 个样本，理论吞吐 \(R\times f_{clk}\)；代价是输入必须按 `inData[j][i]=x[iR+j]` 重排成 \(R\) 条流，并消耗约 \(R\) 倍运算面积。\(R\) 与 \(N\) 都被断言强制为 2 的幂。
- 内部结构主链：`fft()` → `FFTWrapper` → 递归 `FFTStageClassS2S`（级间 dataflow）→ `fftStageKernelS2S`（单级 II=1 蝶形），把 u3-l2 的 pipeline/unroll/dataflow 全部落到一处。
- 二维 FFT = 行方向一维 FFT + 列方向一维 FFT，对外 `fft2d<memWidth,...>(wideIn, wideOut)` 用宽流接口、两套 SSR 参数结构体。
- L1 用例遵循「目录即用例」：`fft_size.hpp`（\(L,R\)）+ `hls_ssr_fft_data_path.hpp`（参数结构体）+ `main.cpp`（顶层 `fft_top` 与 SNR 测试台）+ `description.json`/`Makefile`；用 `make run TARGET=csim` 跑软件仿真，PASS/FAIL 靠 SNR 与误差样本比例两道阈值判定。

## 7. 下一步学习建议

- **进入 AIE 路线的 FFT**：本讲只讲了 PL（HLS）路线的 FFT。下一讲 u6-l2 会转向 dsp 的 AIE（AI Engine）内核家族（mixed_radix_fft、FIR、GeMM 等），那里 FFT 用 ADF 数据流图实现，编程模型与 HLS 完全不同。
- **读端到端 AIE 示例**：u6-l3 的 `vss_fft_ifft_1d` 会把 PL 数据搬运（mm2s/s2mm）与 AIE FFT 图串成完整系统，承接本讲的「FFT 内核」与 u5-l2 的「数据搬运器」。
- **继续深挖 HLS 细节**：若你对硬件实现感兴趣，可精读 `hls_ssr_fft_streaming_data_commutor.hpp`（级间数据换位）与 `hls_ssr_fft_twiddle_table.hpp`（旋转因子 ROM 生成），体会 SSR FFT 如何在「保持 II=1」与「级间数据重排」之间做架构取舍。
- **性能调优方向**：u12-l1 会从工程角度系统讲 dataflow、SSR、datawidth、II 的联合调优，本讲的 SSR 翻倍实验就是那里的一个最小缩影。
