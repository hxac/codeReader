# L2 AIE 内核全景（FFT/DDS/FIR/GeMM/排序/卷积）

## 1. 本讲目标

上一讲（u6-l1）我们看了 **PL 路线**（HLS→RTL）的 FFT。本讲把视角切到 **AIE 路线**，系统盘点 `dsp` 库在 AI Engine 上提供的内核家族，并建立一套「拿到任何一个 AIE 内核都能看懂、能找到测试、能读懂参数」的方法论。

学完本讲你应当能够：

- 说出 `dsp/L1/include/aie` 下 AIE 内核家族的主要类别（FFT/DDS/FIR/矩阵/排序/卷积）。
- 解释每个内核为何被拆成「**kernel + traits + utils**」三件式（外加一个运行时 `.cpp`），以及这套拆分和 AIE 编译流程的关系。
- 读懂内核头文件里的 `TT_`（类型）/`TP_`（参数）模板参数与 `static_assert` 防御性检查。
- 在 `dsp/L2/tests/aie` 下定位某个内核的测试目录，理解 `flow=aie` 的 UUT/REF 比对流程与 `aiesim`/`x86sim` 两档仿真。

> 本讲是「全景导览」，重在分类与组织方式；每个内核的端到端落地（PL 搬运 + AIE 图 + 主机控制）留到 u6-l3 的 `vss_fft_ifft_1d` 完整案例。

## 2. 前置知识

本讲假定你已具备以下认知（来自前置讲义，此处只做最小回顾）：

- **PL vs AIE 两条路线**（u1-l3）：PL 走 HLS→RTL，跑在 Alveo/Zynq；AIE 走 **ADF 数据流图**，由 `aiecompiler` 映射到 Versal 的 AI Engine 阵列，跑在 VCK190（AIE-1）/VEK280（AIE-ML/AIE-2）。
- **L1/L2/L3 三层**（u1-l3）：L1 是算法原语（本讲的内核头文件就在 `dsp/L1/include/aie`），L2 把原语包成可上板内核并配主机（图包装在 `dsp/L2/include/aie`，测试在 `dsp/L2/tests/aie`）。
- **大写 TARGET vs 小写 target**（u2-l3/u5-l1）：L1 PL 用大写 `TARGET`（csim/csynth…），L2 用小写 `target`（sw_emu/hw_emu/hw）；AIE 路线另有专属的 `x86sim`（功能仿真）与 `aiesim`（周期精确仿真）。

几个本讲要用到的 AIE 术语：

| 术语 | 含义 |
|------|------|
| **ADF 图**（graph） | 用 `adf::graph` 描述的内核与连接的拓扑，是 AIE 的「系统描述单位」 |
| **kernel** | 图里的一个计算节点，对应一段跑在单个 AIE 核上的 C++ 函数 |
| **window / stream** | AIE 两种数据交换方式：window 是带大小的 IO buffer（一批一批），stream 是点对点流（一个一个） |
| **cint16 / cint32 / cfloat** | AIE 原生复数类型（16/32 位整数复数、单精度浮点复数），来自 `adf.h` |
| **TP_CASC_LEN**（级联长度） | 把一个逻辑内核切到多个 AIE 核上级联执行，用核间流传递中间结果 |
| **SSR** | 多路并行（一个图里实例化多个 kernel），类比 u6-l1 PL FFT 的超采样率 |

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [dsp/L1/include/aie/mixed_radix_fft.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/include/aie/mixed_radix_fft.hpp) | 混合基数 FFT 内核类定义（防御性检查 + 构造函数） |
| [dsp/L1/include/aie/dds_mixer.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/include/aie/dds_mixer.hpp) | DDS（直接数字合成）+ 混频器内核类定义 |
| [dsp/L1/include/aie/dds_mixer_traits.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/include/aie/dds_mixer_traits.hpp) | DDS 混频器的 traits（按数据类型给出 lanes 数等内在属性） |
| [dsp/L1/include/aie/fir_sr_asym.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/include/aie/fir_sr_asym.hpp) | 单速率非对称 FIR 内核类定义（FIR 全家桶的代表） |
| [dsp/L1/include/aie/matrix_mult.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/include/aie/matrix_mult.hpp) | 矩阵乘（GeMM）内核类定义 |
| [dsp/L1/include/aie/bitonic_sort.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/include/aie/bitonic_sort.hpp) | 双调排序内核类定义 |
| [dsp/L1/include/aie/conv_corr.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/include/aie/conv_corr.hpp) | 卷积/相关内核类定义 |
| [dsp/L2/include/aie/mixed_radix_fft_graph.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/include/aie/mixed_radix_fft_graph.hpp) | 用户面向的 graph 包装：用 `kernel::create_object` 实例化内核并用 `connect` 连线 |
| [dsp/L2/tests/aie/mixed_radix_fft/description.json](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/tests/aie/mixed_radix_fft/description.json) | AIE 测试的元数据身份证（flow、平台白名单、UUT/REF、仿真档） |

> 约定：本讲中 `L1/include/aie` = `dsp/L1/include/aie`，`L2/tests/aie` = `dsp/L2/tests/aie`。

## 4. 核心概念与源码讲解

### 4.1 AIE 内核家族与「kernel + traits + utils」三件式组织

#### 4.1.1 概念说明

打开 `dsp/L1/include/aie` 会看到上百个 `.hpp`，但它们不是一盘散沙——每个内核通常由**三个同根名的头文件**组成，加上**一个运行时 `.cpp`**：

```
mixed_radix_fft.hpp           ← 内核类定义（模板参数 + static_assert 防御检查 + 构造函数）
mixed_radix_fft_traits.hpp    ← traits：描述「内在属性」（按类型给 lanes/cols 数等），无 intrinsics
mixed_radix_fft_utils.hpp     ← utils：构造期/编译期的通用辅助函数
（dsp/L1/src/aie/mixed_radix_fft.cpp）  ← 运行时函数，含 AIE intrinsics，不在 include 里
```

为什么非要这么拆？答案藏在 AIE 的两阶段编译里。`aiecompiler` 先做**图级编译**（解析 graph、分配核、连流），此时**还没有 intrinsics**；只有进入**核级编译**（把每个 kernel 的 C++ 编译成 AIE 机器码）时才用得到 intrinsics。因此：

- 类定义和构造函数**必须**能被图级编译看到（要分配内存、算尺寸）→ 放在 `.hpp`，但**不能含 intrinsics**。
- 含 intrinsics 的运行时函数**不能**进图级编译 → 单独放 `L1/src/aie/*.cpp`。
- traits/utils 是构造函数的依赖，同样**不能含 intrinsics**。

`dds_mixer.hpp` 的文件头注释把这条规则写得很直白：

> The constructor definition is held in this class because this class must be accessible to **graph level aie compilation**. The main runtime ddsMix function is captured **elsewhere** as it contains **aie intrinsics which are not included in aie graph level compilation**.

`dds_mixer_traits.hpp` 也强调自己「干净」：

> this file does not contain any vector types or intrinsics since it is required for construction and therefore must be suitable for **the aie compiler graph-level compilation**.

#### 4.1.2 核心流程

一个 AIE 内核从「头文件」到「跑起来」的链路：

```text
L1/include/aie/<kernel>.hpp        内核类（你设置的 TT_/TP_ 模板参数在此被 static_assert 检查）
        │  （被图级编译引用）
        ▼
L2/include/aie/<kernel>_graph.hpp  graph 包装：kernel::create_object<...>() 实例化
        │                            connect<>(in, kernel.in[0]);  dimensions(...) = {size};
        ▼
L2/tests/aie/<kernel>/test.cpp     测试图：实例化 graph，挂 input_plio/output_plio（文件 IO）
        │
        ▼
aiecompiler → libadf.a             （AIE 图编译产物）
        │
        ▼
aiesimulator / x86simulator        两档仿真，产出 uut_output.txt
        │  同时跑 REF 参考图 → ref_output.txt
        ▼
diff（带 DIFF_TOLERANCE 误差阈值）  判 PASS/FAIL
```

**命名约定**（全库一致，务必记住）：

- `TT_` 前缀 = **T**emplate **T**ype（类型形参），如 `TT_DATA`、`TT_COEFF`、`TT_TWIDDLE`。
- `TP_` 前缀 = **T**emplate **P**arameter（非类型形参，通常是 `unsigned int`/`bool`），如 `TP_POINT_SIZE`、`TP_FIR_LEN`、`TP_CASC_LEN`。
- 命名空间层层嵌套：`xf::dsp::aie::<族>::<具体内核>`，例如 `xf::dsp::aie::fft::mixed_radix_fft`、`xf::dsp::aie::blas::matrix_mult`。

#### 4.1.3 源码精读

内核家族按功能大致分成几类（依据 `dsp/L1/include/aie` 实际文件）：

| 类别 | 代表内核 |
|------|----------|
| FFT / 频域 | `mixed_radix_fft`、`fft_ifft_dit_1ch`、`fft_r2comb`、`fft_window`、`dft`、`widget_2ch_real_fft` |
| 变频 / DDS | `dds_mixer`（含 LUT 变体 `dds_mixer_lut`） |
| FIR 滤波 | `fir_sr_asym`、`fir_sr_sym`、`fir_decimate_*`（asym/sym/hb/hb_asym）、`fir_interpolate_*`（asym/hb/hb_asym/fract_asym）、`fir_resampler`、`fir_tdm` |
| 矩阵 / 线性代数 | `matrix_mult`（GeMM）、`matrix_vector_mul`、`hadamard`、`kronecker`、`outer_tensor`、`euclidean_distance` |
| 排序 | `bitonic_sort`、`merge_sort` |
| 卷积 / 相关 | `conv_corr` |
| 其它 | `cumsum`、`func_approx`、`sample_delay`、`widget_api_cast`（window↔stream 转换）、`widget_real2complex` |

**三件式组织** 以 `dds_mixer` 为例看 traits 如何「描述内在属性」。[dds_mixer_traits.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/include/aie/dds_mixer_traits.hpp) 用一组模板特化给出「每种数据类型、每周期并行几路」：

```cpp
// dds_mixer_traits.hpp:33-46  （traits 不含 intrinsics，可在图级编译期求值）
template <> constexpr int ddsMulVecScalarLanes<cint16, USE_INBUILT_SINCOS>() { return 8; };
template <> constexpr int ddsMulVecScalarLanes<cint32, USE_INBUILT_SINCOS>() { return 4; };
template <> constexpr int ddsMulVecScalarLanes<cfloat,  USE_INBUILT_SINCOS>() { return 4; };
```

内核类再把 traits 的结果拿来算循环次数、缓冲尺寸——见 [dds_mixer.hpp:70-71](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/include/aie/dds_mixer.hpp#L70-L71)：

```cpp
static constexpr unsigned int m_kNumLanes = ddsMulVecScalarLanes<TT_DATA, USE_INBUILT_SINCOS>();
static constexpr unsigned int m_kDOutEachLoop = m_kNumLanes;
```

**graph 包装层** 是用户真正实例化的东西。原始内核类模板参数又多又有默认值，直接用很笨重；`L2/include/aie/<kernel>_graph.hpp` 把它包成一个「填几个参数就能用」的 graph，内部用 `kernel::create_object` 建核、用 `connect` 连线。以 [mixed_radix_fft_graph.hpp:847-868](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/include/aie/mixed_radix_fft_graph.hpp#L847-L868) 为例：

```cpp
m_mixed_radix_fftKernels[0] = kernel::create_object<
    mixed_radix_fft<TT_DATA, TT_TWIDDLE, /*TP_...*/>>(/*构造实参*/);
// 把图的输入端口连到内核的输入
connect<>(in[0], m_mixed_radix_fftKernels[0].in[0]);
dimensions(m_mixed_radix_fftKernels[0].in[0]) = {TP_WINDOW_VSIZE};
// 把内核输出连回图的输出端口
connect<>(m_mixed_radix_fftKernels[0].out[0], out[0]);
dimensions(m_mixed_radix_fftKernels[0].out[0]) = {TP_WINDOW_VSIZE};
```

这段揭示了 AIE 图的三件套写法：`kernel::create_object` 建核、`connect<>` 连流、`dimensions(...)={n}` 声明每条 window 流的元素数。

#### 4.1.4 代码实践（测试目录导览）

**实践目标**：建立「按内核名找测试目录」的肌肉记忆，并读懂 AIE 测试的元数据。

**操作步骤**：

1. 列出所有 AIE 内核测试目录（每个目录 = 一个可独立 make 的用例，呼应 u2-l2 的「目录即用例」）：

   ```bash
   ls -1 dsp/L2/tests/aie/
   ```

   你会看到与本讲家族表一一对应的目录：`mixed_radix_fft`、`dds_mixer`、`fir_sr_asym`、`matrix_mult`、`bitonic_sort`、`conv_corr`、`cumsum`、`hadamard`、`kronecker` 等，外加 `common`（共享脚本）。

2. 进入任一目录，对比「文件构成」。例如 `mixed_radix_fft`：

   ```bash
   ls -1 dsp/L2/tests/aie/mixed_radix_fft/
   # Makefile  description.json  helper.mk  multi_params.json
   # output_post_proc.tcl  test.cpp  test.hpp  utils.mk  uut_static_config.h
   ```

3. 打开 [description.json](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/tests/aie/mixed_radix_fft/description.json)，定位三个关键字段（行号以源码为准）：
   - `"flow": "aie"`（L5）——区别于 PL 的 `flow: hls`/`flow: system`，告诉 CI 这是个 AIE 用例。
   - `"platform_allowlist": ["vck190","vek280"]`（L6-9）——只能在 Versal AIE 板上跑。
   - `"param_set": ["*_aie1_*"]`（vck190，L19-21）/ `["*_aie2_*"]`（vek280，L34-36）——**按 AIE 变体筛选参数组合**：AIE-1 的组合跑 VCK190，AIE-ML 的组合跑 VEK280。

**需要观察的现象**：AIE 测试目录的文件构成（`description.json` + `multi_params.json` + `test.cpp/hpp` + 若干 `.mk`）与 u2-l2 的 PL L1 用例（`test.cpp` + `Makefile` + `description.json` + `hls_config.tmpl`）形似而神不同——这里没有 `hls_config.tmpl`，多了 `multi_params.json`（参数组合表）和 `helper.mk`。

**预期结果**：你能用一句话说清「AIE 测试用 `flow=aie`、靠 `param_set` 按 AIE 变体挑参数、在 `aiesim`/`x86sim` 两档上跑」。

> 待本地验证：以上目录列表与字段位置基于当前 HEAD 实测；具体可跑的参数组合数随 `multi_params.json` 内容变化。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `dds_mixer.cpp`（运行时函数）放在 `dsp/L1/src/aie/` 而不是和 `dds_mixer.hpp` 一起放在 `dsp/L1/include/aie/`？

> **答**：因为运行时函数含 AIE intrinsics，而图级编译（graph-level）看不到 intrinsics；类定义和构造函数必须能被图级编译访问，所以放在 `include` 的 `.hpp` 里、且不能含 intrinsics，含 intrinsics 的运行时代码只能分开存放。

**练习 2**：`TT_DATA` 和 `TP_POINT_SIZE` 前缀分别表示什么？

> **答**：`TT_` = 模板**类型**形参（这里是数据类型，如 `cint16`）；`TP_` = 模板**非类型**形参（这里是 `unsigned int` 的点数）。

---

### 4.2 频域与变频：mixed_radix_fft 与 dds_mixer

#### 4.2.1 概念说明

**mixed_radix_fft（混合基数 FFT）** 是 AIE 路线的招牌 FFT。它的核心卖点是：点数不必是 2 的整数幂，只要能分解成 **2、3、5** 的乘积即可（如 1024=2¹⁰、1920=2⁶·3·5、6400=2⁶·10²）。这对通信、雷达等非 2 幂点数场景非常关键，而上一讲 u6-l1 的 PL HLS FFT 主要面向 2 幂点数。

**dds_mixer（直接数字合成器 + 混频器）** 用来产生/搬移频率：DDS 用相位累加器 + sin/cos 查找表生成一本振复信号，混频器把它与输入相乘，实现上下变频（频率搬移）。这是无线接收机里「数字下变频（DDC）」的核心模块。

#### 4.2.2 核心流程

**mixed_radix_fft 的基数分解**。设点数 \(N\)，内核把 \(N\) 分解为 2/3/4/5 各基数的若干级，总点数满足：

\[
N = 2^{r_2}\cdot 3^{r_3}\cdot 5^{r_5}, \quad r_k\ge 0
\]

内核头文件在编译期用 `fnGetNumStages<N, radix>()` 数出每种基数有几级，进而构造级间数据流。注意基数 4 = 2²，代码用 `TP_POINT_SIZE >> (2*m_kR4Stages)` 把 R4 占掉的因子从 R2 计数里扣掉，避免重复计数。

**dds_mixer 的处理**。每周期并行 `m_kNumLanes` 路（由 traits 按数据类型给出），循环 `TP_INPUT_WINDOW_VSIZE / m_kNumLanes` 次；模式由 `TP_MIXER_MODE` 决定（0=纯 DDS 只输出本振、1=单输入混频、2=双输入混频）。

#### 4.2.3 源码精读

**mixed_radix_fft 的模板参数与防御检查**。[mixed_radix_fft.hpp:57-68](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/include/aie/mixed_radix_fft.hpp#L57-L68) 给出参数列表：

```cpp
template <typename TT_IN_DATA, typename TT_OUT_DATA, typename TT_TWIDDLE,
          unsigned int TP_POINT_SIZE,   // FFT 点数 N
          unsigned int TP_FFT_NIFFT,    // 1=正变换 0=逆变换
          unsigned int TP_SHIFT,        // 输出右移位数（定点缩放）
          unsigned int TP_RND, unsigned int TP_SAT,       // 舍入/饱和模式
          unsigned int TP_WINDOW_VSIZE, // 一次处理多少样本（须是 POINT_SIZE 的倍数）
          unsigned int TP_START_RANK, unsigned int TP_END_RANK,
          unsigned int TP_DYN_PT_SIZE>  // 是否支持运行时动态点数
class kernel_MixedRadixFFTClass { ... };
```

紧接着是一长串 `static_assert` 做**编译期护栏**——这正是把「参数误用」前移到编译瞬间的体现（呼应 u3-l2 的静态断言）。节选 [mixed_radix_fft.hpp:78-108](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/include/aie/mixed_radix_fft.hpp#L78-L108)：

```cpp
// 数据/旋转因子类型组合必须合法：cint16/cint16、cint32/cint16、cint32/cint32、cfloat/cfloat
static_assert((std::is_same<TT_IN_DATA,cint16>::value && ...) || ...,
              "ERROR: TT_DATA/TT_TWIDDLE combination is illegal.");
static_assert(TP_POINT_SIZE % kMinPtSizeGranularity == 0,
              "ERROR. TP_POINT_SIZE must be a multiple of 8 on AIE1 and 16 on AIE2");
static_assert(TP_WINDOW_VSIZE % TP_POINT_SIZE == 0, "...");
// 点数只能含因子 2、3、5
static_assert(m_kR5factor*m_kR3factor*m_kR2factor*m_kR4factor == TP_POINT_SIZE,
              "ERROR: TP_POINT_SIZE must be a multiple of 2, 3 and 5 only");
```

注意 `kMinPtSizeGranularity` 随 `__FFT_R4_IMPL__`（AIE 代际宏）取 8 或 16——同一份源码用条件编译适配 AIE-1 与 AIE-ML。

基数级数在编译期由 `fnGetNumStages` 算出，见 [mixed_radix_fft.hpp:94-110](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/include/aie/mixed_radix_fft.hpp#L94-L110)：

```cpp
static constexpr int m_kR5Stages = fnGetNumStages<TP_POINT_SIZE, 5, TT_TWIDDLE>();
static constexpr int m_kR3Stages = fnGetNumStages<TP_POINT_SIZE, 3, TT_TWIDDLE>();
static constexpr int m_kR4Stages = fnGetNumStages<TP_POINT_SIZE, 4, TT_TWIDDLE>();
static constexpr int m_kR2Stages = fnGetNumStages<(TP_POINT_SIZE >> (2*m_kR4Stages)), 2, TT_TWIDDLE>();
static constexpr int m_kTotalStages = m_kR5Stages + m_kR3Stages + m_kR2Stages + m_kR4Stages;
```

**dds_mixer 的模板参数与模式**。[dds_mixer.hpp:56-65](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/include/aie/dds_mixer.hpp#L56-L65)：

```cpp
template <typename TT_DATA,
          unsigned int TP_INPUT_WINDOW_VSIZE,
          unsigned int TP_MIXER_MODE,        // 0/1/2：纯DDS、单输入混频、双输入混频
          unsigned int TP_USE_PHASE_RELOAD,  // 是否运行时重载相位
          unsigned int TP_API = IO_API::WINDOW,   // window 或 stream
          unsigned int TP_SC_MODE = USE_INBUILT_SINCOS, // 内建 sin/cos 还是 LUT
          unsigned int TP_NUM_LUTS = 1, unsigned int TP_RND = 0, unsigned int TP_SAT = 1>
class kernelDdsMixerClass { ... };
```

混频器输入路数由模式推导（[dds_mixer.hpp:80-81](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/include/aie/dds_mixer.hpp#L80-L81)）：

```cpp
static constexpr unsigned int m_kNumMixerInputs =
    (TP_MIXER_MODE == MIXER_MODE_2) ? 2 : (TP_MIXER_MODE == MIXER_MODE_1) ? 1 : 0;
```

#### 4.2.4 代码实践

**实践目标**：把「读模板参数 → 读 static_assert」走一遍，体会防御性检查如何把错误前移。

**操作步骤**：

1. 打开 `mixed_radix_fft.hpp` 第 78–108 行，数一数共有多少条 `static_assert`，并按「类型合法 / 范围合法 / 倍数关系 / 因子分解」给它们分类。
2. 回答：如果把 `TP_POINT_SIZE` 设成 7（质数，非 2/3/5），哪一条 `static_assert` 会先报错？

**预期结果**：第 1 步应看到类型组合、`TP_FFT_NIFFT∈{0,1}`、`TP_SHIFT∈[0,60]`、点数粒度、窗口倍数、因子分解等多类检查；第 2 步会触发最后一条「must be a multiple of 2, 3 and 5 only」。**待本地验证**：实际编译错误文本以本地 `aiecompiler` 输出为准。

#### 4.2.5 小练习与答案

**练习 1**：`mixed_radix_fft` 相比 u6-l1 的 PL HLS FFT，最大的点数灵活性体现在哪？

> **答**：点数只需是 2、3、5 的乘积即可，不限定为 2 的整数幂，因此支持如 1920、6400 等通信常用非 2 幂点数。

**练习 2**：`TP_MIXER_MODE=0` 时 `dds_mixer` 有几个数据输入？为什么？

> **答**：0 个数据输入。因为 mode 0 是「纯 DDS」，只输出内部生成的本振信号，不对任何外部输入做混频，故 `m_kNumMixerInputs=0`。

---

### 4.3 FIR 滤波器全家桶与级联/SSR

#### 4.3.1 概念说明

FIR（有限脉冲响应）滤波是 DSP 里最基础的运算。`dsp` 库为 AIE 提供了一整套 FIR，按**速率变化**和**对称性**两个维度分类，内核名即说明其特性：

| 命名段 | 含义 |
|--------|------|
| `sr` | single rate，单速率（输入输出同速率） |
| `decimate` | 抽取（降采样，输出比输入少） |
| `interpolate` | 插值（升采样，输出比输入多） |
| `sym` / `asym` | 系数对称 / 非对称（对称可减半乘法量） |
| `hb` | half-band，半带（特殊的对称抽取/插值，每两个输出有一个为零） |
| `fract` | fractional，分数倍速率变换 |
| `resampler` | 重采样器（任意速率比） |
| `tdm` | time-division multiplexed，时分复用（多通道共享一个滤波器） |

因此 `fir_decimate_hb_asym` = 「非对称半带抽取 FIR」。组合命名让你一眼看到内核的速率/对称属性。

#### 4.3.2 核心流程

FIR 本质是卷积 \(y[n]=\sum_{k=0}^{L-1} h[k]\cdot x[n-k]\)。AIE 的向量 intrinsics 按「递增下标」做乘加，但卷积要求系数反向作用，所以**构造函数里把系数数组逆序一次**，运行时就能直接喂给 intrinsics。

当滤波器长度 `TP_FIR_LEN` 超过单个 AIE 核的处理能力，或为了提高吞吐，内核提供两种扩展：

- **级联（cascade）`TP_CASC_LEN`**：把一个长 FIR 切成 `TP_CASC_LEN` 段，每段跑一个核，段间用 AIE 专有的级联流（cascade stream）传递累加中间值，`TP_KERNEL_POSITION` 指明本核是第几段。
- **SSR / 多路并行**：在 graph 里实例化多个 kernel，每路处理一部分数据，类比 u6-l1 的超采样率。

#### 4.3.3 源码精读

**fir_sr_asym 的模板参数**——FIR 全家桶里最典型的一个（[dsp/L1/include/aie/fir_sr_asym.hpp:58-79](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/include/aie/fir_sr_asym.hpp#L58-L79)）：

```cpp
template <typename TT_DATA, typename TT_COEFF,
          unsigned int TP_FIR_LEN,          // 滤波器长度 L
          unsigned int TP_SHIFT,            // 输出右移
          unsigned int TP_RND,
          unsigned int TP_INPUT_WINDOW_VSIZE,
          bool TP_CASC_IN = CASC_IN_FALSE,  // 是否接收上游级联累加
          bool TP_CASC_OUT = CASC_OUT_FALSE,// 是否把累加传给下游
          unsigned int TP_FIR_RANGE_LEN = TP_FIR_LEN,
          unsigned int TP_KERNEL_POSITION = 0,  // 级联中的位置
          unsigned int TP_CASC_LEN = 1,         // 级联长度
          unsigned int TP_USE_COEFF_RELOAD = 0, // 运行时重载系数
          unsigned int TP_NUM_OUTPUTS = 1,
          unsigned int TP_DUAL_IP = 0,
          unsigned int TP_API = 0,              // 0=window 1=stream
          /* ...系数相位相关参数... */
          unsigned int TP_SAT = 1>
class fir_sr_asym;
```

**系数逆序**——一个体现「kernel 与参考模型分工」的好例子。`fir_sr_asym.hpp` 文件头注释（[第 38-44 行](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/include/aie/fir_sr_asym.hpp#L38-L44)）写道：

> the AIE intrinsics operate on increasing indices, but in a conventional FIR there is a convolution of data and coefficients. So as to achieve the impulse response ... the coefficient array has to be **reversed** ... This reversal is performed in the **constructor**. To avoid common-mode errors the **reference model performs this reversal at run-time**.

也就是说：设备内核在构造期翻转系数一次（固化进对象），而参考模型（REF）在运行期翻转——两边各做一次，避免「两边都忘了翻转」这种共性错误（common-mode error）。这正是 4.4 节 UUT/REF 比对能成立的基础。

**级联分段** 由 traits 里的 `fnFirRange/fnFirRangeRem` 决定每段核处理几个抽头，见 [fir_sr_asym.hpp:81-90](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/include/aie/fir_sr_asym.hpp#L81-L90) 的 `_tl`（template-list）包装类，它按 `TP_KERNEL_POSITION` 和 `TP_CASC_LEN` 算出本段长度 `firRangeLen`。

#### 4.3.4 代码实践

**实践目标**：通过内核名解码其功能，并理解级联如何切分长滤波器。

**操作步骤**：

1. 把下列内核名「翻译」成中文功能描述：`fir_interpolate_sym`、`fir_decimate_hb`、`fir_resampler`、`fir_tdm`。
2. 在 `dsp/L2/tests/aie/` 下确认这些内核都有对应测试目录（`ls -1 dsp/L2/tests/aie/ | grep fir`）。

**预期结果**：`fir_interpolate_sym`=对称插值 FIR；`fir_decimate_hb`=半带抽取 FIR；`fir_resampler`=任意比特率重采样器；`fir_tdm`=时分复用多通道 FIR。测试目录应一一对应。

#### 4.3.5 小练习与答案

**练习 1**：为什么 FIR 内核要把系数在构造期逆序？

> **答**：AIE 的卷积/乘加 intrinsics 按下标递增方式访问数据，而数学上 FIR 是系数与延迟线数据的卷积（系数需「反向」作用）。把系数逆序后，运行时可直接用 intrinsics 的递增访问得到正确的脉冲响应。

**练习 2**：`TP_CASC_LEN=4`、`TP_KERNEL_POSITION=2` 代表什么？

> **答**：这个长 FIR 被级联切成 4 段（4 个 AIE 核），当前这个核是第 3 段（位置 2，从 0 起算），它从前一段的级联流接收部分和，再把自己这段的乘加结果经级联流传给下一段。

---

### 4.4 矩阵乘法 GeMM（matrix_mult）

#### 4.4.1 概念说明

`matrix_mult` 是 AIE 上的**通用矩阵乘**（GeMM，General Matrix Multiply）：计算 \(C_{m\times n} = A_{m\times k} \cdot B_{k\times n}\)。它是 BLAS（u8-l1）和很多机器学习内核的基础积木。AIE 的向量核擅长「小块矩阵乘 + 累加」，所以 `matrix_mult` 的核心思路是 **tiling（分块）**：把大矩阵切成适配 AIE 向量寄存器的小块，逐块乘累加，再拼回。

#### 4.4.2 核心流程

```
1. 根据 TT_DATA_A / TT_DATA_B 的类型组合，查 traits 得到 tiling 方案
   tilingScheme = (Atile, ABtile, Btile) —— A 的行块、公共维块、B 的列块大小
2. static_assert 检查三个维度都是对应 tile 的整数倍
3. 运行时：for 每个 (Atile×ABtile) 的 A 块 × (ABtile×Btile) 的 B 块 → 累加到 C 块
4. 可选 tiling/untiling 包装：把行主序/列主序的输入重排成 AIE 友好的块布局
```

AIE 矩阵乘的吞吐关键就在 tiling 方案——块太小喂不满向量单元，块太大装不下寄存器。

#### 4.4.3 源码精读

**matrix_mult 的模板参数**。[matrix_mult.hpp:226-246](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/include/aie/matrix_mult.hpp#L226-L246)：

```cpp
template <typename TT_DATA_A, typename TT_DATA_B, typename TT_OUT_DATA,
          unsigned int TP_DIM_A,    // A 的行数 m
          unsigned int TP_DIM_AB,   // 公共维 k
          unsigned int TP_DIM_B,    // B 的列数 n
          unsigned int TP_SHIFT, unsigned int TP_RND, unsigned int TP_SAT = 1,
          unsigned int TP_DIM_A_LEADING = ROW_MAJOR,   // A 的主序
          unsigned int TP_DIM_B_LEADING = COL_MAJOR,   // B 的主序
          unsigned int TP_DIM_OUT_LEADING = ROW_MAJOR,
          unsigned int TP_INPUT_WINDOW_VSIZE_A = TP_DIM_A*TP_DIM_AB,
          unsigned int TP_INPUT_WINDOW_VSIZE_B = TP_DIM_B*TP_DIM_AB,
          bool TP_CASC_IN = CASC_IN_FALSE, bool TP_CASC_OUT = CASC_OUT_FALSE,
          /* ...range/position/casc_len... */
          unsigned int TP_CASC_LEN = 1>
class matrix_mult : public kernelMatMultClass<...> { ... };
```

注意几个有别于 FFT/FIR 的特色：`TT_DATA_A`/`TT_DATA_B`/`TT_OUT_DATA` **三者可不同**（如 int16 输入、cint16 输出）；`TP_DIM_*_LEADING` 控制**矩阵存储主序**（行主序/列主序），因为 A/B 两个输入可以各自有不同的布局。

**tiling 方案的编译期计算与校验**。[matrix_mult.hpp:199-211](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/include/aie/matrix_mult.hpp#L199-L211)：

```cpp
template <unsigned Atile, unsigned ABtile, unsigned Btile>
static bool constexpr tilingSchemeMultiples() {
    static_assert(TP_DIM_A  % Atile  == 0, "Error: TP_DIM_A is not a multiple of the tiling scheme.");
    static_assert(TP_DIM_B  % Btile  == 0, "Error: TP_DIM_B is not a multiple of the tiling scheme.");
    static_assert(TP_DIM_AB % ABtile == 0, "Error: TP_DIM_AB is not a multiple of the tiling scheme.");
    return (TP_DIM_A % Atile == 0) && (TP_DIM_B % Btile == 0) && (TP_DIM_AB % ABtile == 0);
}
static constexpr tilingStruct tilingScheme = getTilingScheme();
static_assert((tilingScheme.Atile > 1 || tilingScheme.ABtile > 1 || tilingScheme.Btile > 1),
              "ERROR: There are no supported Matrix Multiplication Modes for this data type combination");
```

这说明 tiling 方案是**按数据类型组合从 traits 查表得到的**（`fnTilingScheme<TT_DATA_A, TT_DATA_B>()`），不是用户直接指定；用户的职责是保证三个维度是 tile 的整数倍。

**参数组合示例**（来自 [multi_params.json](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/tests/aie/matrix_mult/multi_params.json) 第一组 `test_0_tool_canary_aie`）：`T_DATA_A=cint16, T_DATA_B=cint16, P_DIM_A=32, P_DIM_AB=64, P_DIM_B=32`，即做 \(C_{32\times 32} = A_{32\times 64}\cdot B_{64\times 32}\)。

#### 4.4.4 代码实践

**实践目标**：读懂 matrix_mult 测试的一组参数组合代表什么运算。

**操作步骤**：

1. 打开 `dsp/L2/tests/aie/matrix_mult/multi_params.json`，找到第二组（`test_1_..._hw_checkin`）。
2. 读出 `T_DATA_A`、`T_DATA_B`、`DATA_OUT_TYPE`、`P_DIM_A`、`P_DIM_AB`、`P_DIM_B`、`UUT_SSR`、`P_CASC_LEN` 这几个字段，写出它对应的矩阵乘形状与并行配置。

**预期结果**：应为 `int16 × cint16 → cint16`，形状约 \(A_{32\times 16}\cdot B_{16\times 16}=C_{32\times 16}\)，`UUT_SSR=2`（两路并行）、`P_CASC_LEN=1`（单段不级联）。**待本地验证**：以本地 `multi_params.json` 实际内容为准。

#### 4.4.5 小练习与答案

**练习 1**：`matrix_mult` 为什么需要 `TP_DIM_A_LEADING`（主序）参数？

> **答**：矩阵可以按行主序或列主序存储在内存里，A、B 两个输入可能布局不同。AIE 的分块乘法需要知道主序才能正确地切出 (Atile×ABtile) 块；指定主序让内核按实际内存布局寻址。

**练习 2**：tiling 方案是用户指定的吗？

> **答**：不是。tiling 方案 `(Atile, ABtile, Btile)` 由 traits 按 `TT_DATA_A/TT_DATA_B` 类型组合查表决定；用户的职责是让 `TP_DIM_A/TP_DIM_AB/TP_DIM_B` 成为对应 tile 尺寸的整数倍，否则 `static_assert` 报错。

---

### 4.5 排序、卷积与 AIE 测试体系

#### 4.5.1 概念说明

**bitonic_sort（双调排序）**：一种适合硬件并行的排序网络。给定长度 \(N=2^k\) 的序列，双调排序用 \(k(k-1)/2\) 级比较-交换，每级可并行执行，非常契合 AIE 的向量核。`merge_sort` 则是归并排序的 AIE 实现。排序的 `TP_DIM` 指定待排序序列长度。

**conv_corr（卷积/相关）**：统一处理一维卷积与相关，由 `TP_FUNCT_TYPE` 选择「卷积」还是「相关」，`TP_COMPUTE_MODE` 选择计算方式（直接/频域）。它和 FIR 一样是「滑动乘加」，但卷积的两个输入都是数据（FIR 的一个输入是固定系数），所以更通用。

本节同时收尾「**AIE 测试体系**」——因为排序/卷积这类「输入输出有明确对错」的内核，最能体现 AIE 测试的 **UUT/REF 比对** 思路。

#### 4.5.2 核心流程

**AIE 测试的 UUT/REF 双图比对**（贯穿所有 AIE 内核）：

```text
对 multi_params.json 里的每一组参数组合 P：
  1. tb_gen.py 按参数 P 生成 uut_config.h（具体化的模板实参）
  2. aiecompiler 编译两个图：
        UUT 图  = 用设备内核（含 intrinsics，跑在 AIE 上）
        REF 图  = 用参考 C++ 模型（纯标量参考实现）
  3. 同时生成随机输入数据
  4. aiesim/x86sim 分别跑 UUT 与 REF → uut_output.txt / ref_output.txt
  5. diff 比较，误差在 DIFF_TOLERANCE（相对）/CC_TOLERANCE（绝对）内则 PASS
```

两档仿真分工：`x86sim` 是**功能仿真**（在 x86 上直接跑核函数，快，验证算法对不对）；`aiesim` 是**周期精确仿真**（模拟 AIE 阵列时序，慢，验证时序/吞吐）。CI 里常用 `x86sim` 做快速回归，`aiesim` 做质量门禁。

#### 4.5.3 源码精读

**bitonic_sort 的模板参数**。[bitonic_sort.hpp:57-63](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/include/aie/bitonic_sort.hpp#L57-L63)：

```cpp
template <typename TT_DATA,
          unsigned int TP_DIM,         // 序列长度（须为 2 的幂）
          unsigned int TP_NUM_FRAMES,  // 一次处理几帧
          unsigned int TP_ASCENDING,   // 1=升序 0=降序
          unsigned int TP_CASC_LEN,    // 把排序网络级联到多个核
          unsigned int TP_CASC_IDX>    // 本核负责第几段
class bitonic_sort { ... };
```

级联在这里的语义与 FIR 不同：双调排序是**排序网络的级**，`kNumStages` 个级可沿级联方向分配到 `TP_CASC_LEN` 个核上，见 [bitonic_sort.hpp:71-75](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/include/aie/bitonic_sort.hpp#L71-L75)：

```cpp
static constexpr unsigned int kNumStages = getNumStages<TP_DIM>();   // log2(TP_DIM)*(...)/2
static constexpr unsigned int kNumCascStages = kNumStages / TP_CASC_LEN;
static constexpr int kRemainder = kNumStages % TP_CASC_LEN;
static constexpr unsigned int kFirstStage = ...; // 本核从第几级开始
```

**conv_corr 的模板参数**。[conv_corr.hpp:57-72](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L1/include/aie/conv_corr.hpp#L57-L72)：

```cpp
template <typename TT_DATA_F, typename TT_DATA_G, typename TT_DATA_OUT,
          unsigned int TP_FUNCT_TYPE,    // 卷积 or 相关
          unsigned int TP_COMPUTE_MODE,  // 计算方式
          unsigned int TP_F_LEN,         // f 的长度
          unsigned int TP_G_LEN,         // g 的长度
          unsigned int TP_SHIFT, unsigned int TP_API, unsigned int TP_RND, unsigned int TP_SAT,
          unsigned int TP_NUM_FRAMES, unsigned int TP_CASC_LEN, unsigned int TP_PHASES,
          unsigned int TP_KERNEL_POSITION, unsigned int TP_PH_POSITION,
          bool TP_CASC_IN = CASC_IN_FALSE, bool TP_CASC_OUT = CASC_OUT_FALSE, ...>
```

注意 `TT_DATA_F`/`TT_DATA_G`/`TT_DATA_OUT` 三个数据类型——两个输入序列可以类型不同，这是它比 FIR（一个数据一个系数）更通用的体现。

**UUT/REF 比对在 description.json 里的体现**。以 [dds_mixer/description.json](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/tests/aie/dds_mixer/description.json) 为例（矩阵、排序、卷积的结构完全一样）：

- `"flow": "aie"`（[L5](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/tests/aie/dds_mixer/description.json#L5)）。
- 平台白名单与 AIE 变体筛选：`"platform_allowlist": ["vck190"]`（[L6-8](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/tests/aie/dds_mixer/description.json#L6-L8)）、`"param_set": ["*_aie1_*"]`（[L21-23](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/tests/aie/dds_mixer/description.json#L21-L23)）。
- UUT 与 REF 的内核名/图名（[L116-119](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/tests/aie/dds_mixer/description.json#L116-L119)）：`UUT_KERNEL=dds_mixer`、`REF_KERNEL=dds_mixer_ref`、`UUT_GRAPH=dds_mixer_graph`、`REF_GRAPH=dds_mixer_ref_graph`。
- 误差阈值 `DIFF_TOLERANCE=4`（[L120](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/tests/aie/dds_mixer/description.json#L120)）——因为定点/浮点非 bit 精确，用相对误差门限判 PASS/FAIL（呼应 u6-l1 的 SNR 判定思路）。
- 测试台自动生成（[L161-166](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/dsp/L2/tests/aie/dds_mixer/description.json#L161-L166)）：`uut_config.h` 由 `tb_gen.py` 的 `generate_testbench` 从 `multi_params.json` 生成——这就是「参数组合 → 具体测试台」的桥梁。

#### 4.5.4 代码实践（本讲主实践）

**实践目标**：在 `dsp/L2/tests/aie` 下选一个内核（`dds_mixer` 或 `matrix_mult`），描述其目录与构建入口，并列出该内核 traits 头文件里的模板参数。以下以 **dds_mixer** 为示范，再请你照做 **matrix_mult**。

**操作步骤**：

1. **目录与构建入口**：

   ```bash
   ls -1 dsp/L2/tests/aie/dds_mixer/
   # Makefile  description.json  helper.mk  multi_params.json  test.cpp  test.hpp  utils.mk  uut_static_config.h
   ```

   - `description.json`：上面精读过的「身份证」，CI 与工具链据此识别 flow/平台/参数。
   - `multi_params.json`：参数组合表，每组 = 一次 `make` 配置。
   - `test.cpp`：`main()`，实例化 `testcase::test_graph`（即 `dds_mixer_tb`），由 `aiecompiler` 编译成 `libadf.a`。
   - `test.hpp`：测试图类，挂 `input_plio`/`output_plio`（仿真时用文件 IO 模拟 PL 搬运）。
   - `Makefile` / `helper.mk` / `utils.mk`：构建入口，`make run TARGET=x86sim` 跑功能仿真。

2. **构建入口命令**（AIE 测试用小写 `TARGET`，区别于 L1 PL 的大写 `TARGET`）：

   ```bash
   cd dsp/L2/tests/aie/dds_mixer
   make run TARGET=x86sim        # 功能仿真（快）
   make run TARGET=aiesim        # 周期精确仿真（慢，含时序）
   ```

3. **列 traits 模板参数**。打开 `dsp/L1/include/aie/dds_mixer_traits.hpp` 与 `dds_mixer.hpp`，对照内核类的模板形参表（4.2.3 节已列），归纳 dds_mixer 的模板参数：

   | 类别 | 参数 | 含义 |
   |------|------|------|
   | 类型 TT_ | `TT_DATA` | 数据类型（cint16/cint32/cfloat） |
   | 尺寸 TP_ | `TP_INPUT_WINDOW_VSIZE` | 输入窗口样本数 |
   | 功能 TP_ | `TP_MIXER_MODE` | 0/1/2：纯 DDS / 单输入混频 / 双输入混频 |
   | 功能 TP_ | `TP_USE_PHASE_RELOAD` | 是否运行时重载相位 |
   | 接口 TP_ | `TP_API` | window 或 stream |
   | 实现 TP_ | `TP_SC_MODE` | 内建 sin/cos 还是 LUT |
   | 实现 TP_ | `TP_NUM_LUTS` | LUT 数量 |
   | 精度 TP_ | `TP_RND` / `TP_SAT` | 舍入模式 / 饱和模式 |

4. **自行照做 matrix_mult**：打开 `dsp/L2/tests/aie/matrix_mult/` 与 `dsp/L1/include/aie/matrix_mult.hpp`，仿照上表列出其模板参数（参考 4.4.3 节）。

**需要观察的现象**：`dds_mixer` 测试只允许 `vck190`（AIE-1），而 `matrix_mult`、`mixed_radix_fft` 测试同时允许 `vck190` 和 `vek280`——说明后两者已适配 AIE-ML。

**预期结果**：你能用一张表说清「dds_mixer 测试的文件构成 + 构建命令 + 模板参数分类」，并独立完成 matrix_mult 的同款分析。**待本地验证**：实际能否 `make run TARGET=x86sim` 取决于本地是否装好 Vitis + AIE 工具链与平台。

#### 4.5.5 小练习与答案

**练习 1**：`x86sim` 和 `aiesim` 各验什么？为什么 CI 通常两个都跑？

> **答**：`x86sim` 是功能仿真，在 x86 上直接跑核函数，快，验证算法正确性；`aiesim` 是周期精确仿真，模拟 AIE 阵列时序，慢，验证时序与吞吐。CI 用 x86sim 做快速回归筛算法 bug，用 aiesim 做质量门禁防时序回归。

**练习 2**：UUT 图和 REF 图有什么区别？为什么定点内核要靠 REF 比对而不是直接比 bit？

> **答**：UUT 用设备内核（含 AIE intrinsics，定点/向量化实现），REF 用纯标量 C++ 参考模型。定点运算因舍入/饱和非 bit 精确，故用 `DIFF_TOLERANCE` 相对误差门限判 PASS/FAIL，而不是逐位比较。

---

## 5. 综合实践

**任务**：为 `dsp` AIE 内核家族制作一张「内核速查卡」，并自选一个内核走通「源码 → 测试」的导航链路。

1. **家族速查卡**：浏览 `ls -1 dsp/L1/include/aie/*.hpp`，把内核按本讲的五大类（FFT/DDS/FIR/矩阵/排序卷积，外加「其它」）填入一张表，每类列 2-3 个代表内核名，并标注其 `flow` 都是 `aie`、目标平台为 vck190/vek280。

2. **三件式核对**：任选一个内核（如 `bitonic_sort`），确认它在 `dsp/L1/include/aie/` 下是否同时存在 `<kernel>.hpp`、`<kernel>_traits.hpp`、`<kernel>_utils.hpp`，再在 `dsp/L1/src/aie/` 下找对应的运行时 `<kernel>.cpp`，验证 4.1 节的「三件式 + 运行时分离」组织。

3. **测试导航**：为该内核在 `dsp/L2/tests/aie/` 下找到同名测试目录，打开 `description.json`，找出：`flow`、`platform_allowlist`、`UUT_KERNEL`/`REF_KERNEL`、`DIFF_TOLERANCE`、`generators`（测试台生成器）这五个字段，用一句话解释每个字段的含义。

4. **参数解码**：打开该测试的 `multi_params.json`，挑一组参数，对照内核头文件的 `TT_`/`TP_` 模板参数，写出这组参数让内核执行什么具体运算。

**预期产出**：一张家族速查卡 + 一条「内核头文件 → traits → graph 包装 → 测试目录 → 参数组合」的完整导航路径。**待本地验证**：具体参数组合与可跑性以本地仓库与工具链为准。

## 6. 本讲小结

- `dsp` 的 AIE 内核家族分五大类：FFT/频域、DDS/变频、FIR 全家桶、矩阵/线性代数、排序/卷积，外加 widget 等辅助内核。
- 每个内核遵循**「kernel + traits + utils」三件式**：类定义放 `.hpp`（含 `static_assert` 防御检查、构造函数，不含 intrinsics），运行时函数放 `dsp/L1/src/aie/*.cpp`（含 intrinsics），这套拆分源于 AIE 的「图级编译（无 intrinsics）→ 核级编译（有 intrinsics）」两阶段。
- 命名约定：`TT_` = 类型形参，`TP_` = 非类型形参；用户实际实例化的是 `dsp/L2/include/aie/*_graph.hpp` 里的 graph 包装，内部用 `kernel::create_object` + `connect` + `dimensions` 建图。
- `matrix_mult`（GeMM）的核心是按数据类型查表得到的 **tiling 方案**，用户的职责是让矩阵维度是 tile 的整数倍；`TP_CASC_LEN` 把长运算级联到多个 AIE 核。
- AIE 测试在 `dsp/L2/tests/aie`，`flow=aie`、按 `param_set` 的 `*_aie1_*`/`*_aie2_*` 给 vck190/vek280 挑参数，用 **UUT/REF 双图 + DIFF_TOLERANCE** 比对，分 `x86sim`（功能）与 `aiesim`（周期精确）两档。

## 7. 下一步学习建议

- **u6-l3（端到端 AIE 示例：vss_fft_ifft_1d）**：本讲只到「单内核 + 测试图」，下一讲把 PL 搬运（mm2s/s2mm）、AIE 图（`aie.cpp/aie.hpp`）、参数配置（`my_params.cfg`）和主机控制串成完整系统，是 AIE 路线的收束。
- **u13-l1（ADF 图、窗口/流与 PL↔AIE 边界）**：想深入 graph 的 `connect`/`window`/`stream` 与 PLIO/GMIO 边界机制，可跳到专家层的 AIE 编程模型讲义。
- **自行阅读建议**：挑一个本讲提到但未展开的内核（如 `fir_resampler` 或 `merge_sort`），按本讲的方法论（三件式 → graph → 测试 → 参数）独立做一次源码导航，巩固「拿到任何 AIE 内核都能看懂」的能力。
