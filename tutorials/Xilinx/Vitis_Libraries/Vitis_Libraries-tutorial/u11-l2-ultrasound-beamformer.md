# 超声波束合成器：L1→L2→L3 组合

## 1. 本讲目标

`ultrasound`（超声）库是 Vitis 加速库里一个「麻雀虽小、五脏俱全」的垂直领域库。它的特别之处在于：整个库的源码就是一张「从底层原语逐层组合成完整应用」的教科书式示例——这正是本手册反复强调的 L1→L2→L3 三层抽象。

学完本讲，你应当能够：

- 说清 `ultrasound` 库 L1、L2、L3 三层各自是什么，以及它们如何「层层组合」。
- 读懂 L1 的 numpy 风格 AIE 向量/矩阵运算（`mulMM`/`sumVV`/`absV`/`norm_axis_1` 等）及其 `aie_api` SIMD 实现。
- 读懂 L2 波束合成功能单元（`kernel_delay`/`kernel_apodization`/`kernel_focusing`/`kernel_interpolation`/`kernel_sample`）的「参数结构体 + 迭代器 + 向量化融合」写法，以及 L2 如何用 `adf::graph` 包装单个内核。
- 理解 L3 完整 Beamformer 的三种模式（PW/SA/Scanline）如何在一个 `adf::graph` 里实例化并连接多个 L2 子图。
- 亲手画出 L1→L2→L3 的组合依赖关系图。

## 2. 前置知识

本讲默认你已经读过（依赖讲义）：

- **u1-l3（L1/L2/L3 与 PL/AIE 范式）**：知道 L1 是原语、L2 是内核、L3 是应用流水线；知道 PL 走 HLS、AIE 走 ADF 数据流图。
- **u8-l1（BLAS 三级抽象与 GEMM）**：理解「按运算对象分级的 BLAS」（向量/矩阵-向量/矩阵-矩阵）概念，以及 L1 模块互相复用的思想。
- **u5-l3（多内核流水线组合）**：理解 L3 把多个内核缝合成端到端流水线、稳态吞吐由最慢 stage 决定的基本观念。

本讲会用到几个 AIE 专属术语，先做通俗铺垫：

| 术语 | 通俗解释 |
|------|----------|
| **AIE / AI Engine** | Versal 器件里的向量处理器阵列，每个核擅长 SIMD（单指令多数据）向量运算。 |
| **ADF 图（`adf::graph`）** | 用 C++ 类描述的数据流图：声明若干 `adf::kernel`（内核），再用 `adf::connect` 把它们的端口连起来，编译器把它映射到 AIE 阵列。 |
| **`adf::kernel::create(fn)`** | 在图里实例化一个内核，`fn` 是内核的 C++ 顶层函数。 |
| **`adf::source(k) = "x.cpp"`** | 告诉 AIE 编译器：内核 `k` 的实现源码在 `x.cpp`。 |
| **`adf::runtime<adf::ratio>(k) = 0.8`** | 该内核运行时间比例约束（占 AIE 时钟的 80%），用于约束调度。 |
| **PLIO（`adf::input_plio`/`output_plio`）** | PL 与 AIE 之间的 AXI-Stream 边界端口；仿真时它绑定到 `.txt` 文件，上板时绑定到 PL 的物理流。 |
| **`aie_api`** | AIE 的 C++ 向量 intrinsic 库，核心类型是 `aie::vector<T, VECDIM>`。 |
| **RTP（Run-Time Parameter）** | 运行时参数，通过 `adf::connect<adf::parameter>(..., async(...))` 异步喂给内核。 |

> 提示：`ultrasound` 是一个**纯 AIE 库**（所有内核都用 `adf.h` + `aie_api`，跑在 Versal VCK190 上）。本讲不涉及 PL HLS 的 `#pragma`，而是讲 ADF 图如何组织。这一点和 `motor_control`（u11-l1，纯 PL）正好形成对照。

## 3. 本讲源码地图

本讲涉及的关键文件，按 L1→L2→L3 自底向上排列：

| 文件 | 层 | 作用 |
|------|----|------|
| [ultrasound/README.md](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/README.md) | 总览 | 用三句话定义了 L1/L2/L3 各是什么。 |
| [ultrasound/L1/README.md](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/README.md) | L1 | 逐个列出 L1 向量/矩阵运算的语义（numpy 风格）。 |
| [ultrasound/L1/include/kernels.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/kernels.hpp) | L1 | **L1 全体函数声明 + 物理常量** 的聚合头文件。 |
| [ultrasound/L1/include/mulMM/mulMM.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/mulMM/mulMM.cpp) | L1 | 「矩阵对应元素相乘」的 `aie_api` 实现，最简单的 SIMD 样本。 |
| [ultrasound/L1/include/norm_axis_1/norm_axis_1.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/norm_axis_1/norm_axis_1.cpp) | L1 | 「逐行欧氏范数」实现，演示 `mul_square`+`reduce_add`+`sqrt`。 |
| [ultrasound/L1/include/kernel_delay.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/kernel_delay.hpp) | L2 接口 | 延迟内核的参数结构体与 `kfun_*` 函数声明。 |
| [ultrasound/L1/include/kernel_delay/kernel_delay.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/kernel_delay/kernel_delay.cpp) | L2 实现 | 延迟内核的向量化实现（融合多个 L1 等价运算）。 |
| [ultrasound/L1/include/kernel_apodization.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/kernel_apodization.hpp) | L2 接口 | 变迹（Hanning 窗）内核声明。 |
| [ultrasound/L1/include/kernel_focusing.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/kernel_focusing.hpp) | L2 接口 | 聚焦内核声明。 |
| [ultrasound/L1/include/kernel_interpolation.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/kernel_interpolation.hpp) | L2 接口 | 插值内核声明（B 样条）。 |
| [ultrasound/L1/include/kernel_sample.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/kernel_sample.hpp) | L2 接口 | 采样索引内核声明。 |
| [ultrasound/L2/include/graph_delay.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L2/include/graph_delay.hpp) | L2 图 | 把延迟内核包成一个 `adf::graph` 子图。 |
| [ultrasound/L2/include/graph_apodization.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L2/include/graph_apodization.hpp) | L2 图 | 变迹内核的图包装（pre + main 两个子图）。 |
| [ultrasound/L3/include/scanline.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/include/scanline.hpp) | L3 主机 | L3 Scanline 的主机端 XRT 控制代码。 |
| [ultrasound/L3/tests/scanline/aie_graph/graph.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/tests/scanline/aie_graph/graph.cpp) | L3 图 | **本讲核心**：实例化 6 个 L2 子图、用 PLIO 连接的完整 Beamformer ADF 图。 |
| [ultrasound/L3/README.md](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/README.md) | L3 | 三种 Beamformer（SA/PW/ScanLine）的输入输出说明。 |

> 一个容易混淆的点：`kernel_*.hpp` 和 `kernel_*/` 目录虽然物理上位于 `L1/include/` 下，但它们语义上属于 **L2 功能单元**。仓库把 L2 的内核函数声明与实现也放在了 `L1/include` 里（历史原因），而把 L2 的 **图包装** 放在 `L2/include/`。判断层级要看语义（函数做什么），不要只看目录。

## 4. 核心概念与源码讲解

### 4.1 L1 向量/BLAS 运算：numpy 风格的 AIE 原语

#### 4.1.1 概念说明

超声波束合成的底层计算，本质上是一堆「向量和矩阵的逐元素运算 + 归约」：两个向量逐元素相乘（`mulVV`）、两个矩阵逐元素相乘（`mulMM`）、逐元素求绝对值（`absV`）、逐行求欧氏范数（`norm_axis_1`）、向量与标量运算（`mulVS`/`diffSV`）、生成全 1 向量（`ones`）、外积（`outer`）等等。

`ultrasound` 的设计者把这些运算做成了 **一组 numpy 风格的 AIE 内核**，让上层算法可以用接近 numpy 的思维来组合。L1 README 一句话说清楚了意图（[ultrasound/L1/README.md:6](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/README.md#L6)）：目标是「在 NumPy 与 AI Engine 的 C++ SIMD API 之间建立尽可能贴近的映射」，分两大类——逐元素运算、向量创建与变形。

顶层 README 则把三层关系一句话锁定（[ultrasound/README.md:4-7](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/README.md#L4-L7)）：L1 是「simple BLAS operation」，L2 是「由 L1 组合得到的 Beamformer 功能单元」，L3 是「使用上述全部内容的完整 Beamformer」。

#### 4.1.2 核心流程

每个 L1 内核的形态高度统一，是一个模板函数：

```text
template <typename T, LEN, INCREMENT, VECDIM>
void 某运算(input_buffer<T>& in..., output_buffer<T>& out);
```

- `T`：元素类型（通常是 `float`/`cfloat`）。
- `LEN`：本次要处理的元素总数。
- `VECDIM`：SIMD 向量宽度（一次处理几个元素），由 AIE 架构与类型决定（参见 Xilinx UG1076）。
- `INCREMENT`：每次迭代 SIMD 推进的步长，与 `LEN`/`VECDIM` 配合切分循环。

执行流程是经典的 SIMD 循环：

```text
for (i = 0; i < LEN; i += INCREMENT):
    op = aie::load_v<VECDIM>(ptr)   // 从 buffer 装载一个向量
    res = 某个 aie:: 向量运算(op)    // 在 SIMD 通道里并行算
    aie::store_v(ptr_out, res)       // 存回输出 buffer
```

#### 4.1.3 源码精读

**`kernels.hpp`** 是 L1 的「目录页」。它在文件顶部集中定义了一组物理常量（声速、F 数、π、采样率等），然后用一长串模板函数声明列出全部 L1 原语。下面是其中几个与本讲最相关的声明（[kernels.hpp:80-101](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/kernels.hpp#L80-L101)）：

```cpp
// 矩阵对应元素相乘
template <typename T, const unsigned int LEN, const unsigned int INCREMENT, const unsigned VECDIM>
void mulMM(adf::input_buffer<T>& __restrict in1,
           adf::input_buffer<T>& __restrict in2,
           adf::output_buffer<T>& out);

// 逐行欧氏范数：每行返回一个标量
template <typename T, const unsigned int LEN, const unsigned int INCREMENT, const unsigned VECDIM>
void norm_axis_1(adf::input_buffer<T>& __restrict in1, adf::output_buffer<T>& __restrict out);
```

物理常量集中在 [kernels.hpp:26-50](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/kernels.hpp#L26-L50)，例如 `SPEED_OF_SOUND 1540`、`F_NUMBER 2`、`SPACE_DIMENSION 4`（坐标用 4 维齐次坐标 x/y/z/1），它们是上层波束合成公式的共享参数。

最简单的 SIMD 样本是 `mulMM`，整个实现不到 20 行（[mulMM.cpp:23-47](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/mulMM/mulMM.cpp#L23-L47)）：

```cpp
template <typename T, const unsigned int LEN, const unsigned int INCREMENT, const unsigned VECDIM>
void mulMM(adf::input_buffer<T>& __restrict in1, ...) {
    aie::vector<T, VECDIM> op1 = aie::zeros<T, VECDIM>();
    aie::vector<T, VECDIM> op2 = aie::zeros<T, VECDIM>();
    aie::vector<T, VECDIM> res = aie::zeros<T, VECDIM>();
    for (unsigned i = 0; i < LEN; i += INCREMENT) {
        op1 = aie::load_v<VECDIM>(p_in1);   // 装载一个 VECDIM 维向量
        op2 = aie::load_v<VECDIM>(p_in2);
        res = aie::mul(op1, op2);            // 逐元素相乘（SIMD 并行）
        aie::store_v(p_out, res);            // 存回
        ...
    }
};
```

这段代码就是「`C = A * B`（逐元素）」的 numpy 对应物：`aie::vector` ↔ numpy 数组，`aie::mul` ↔ `*`，`aie::load_v/store_v` ↔ 内存读写。

稍微复杂一点的是 `norm_axis_1`（逐行欧氏范数）。它演示了「平方 → 归约 → 开方」的三步组合（[norm_axis_1.cpp:34-42](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/norm_axis_1/norm_axis_1.cpp#L34-L42)）：

```cpp
for (unsigned i = 0; i < LEN; i += INCREMENT) {
    op1 = aie::load_v<VECDIM>(p_in);
    pow2 = aie::mul_square(op1);            // 逐元素平方
    res  = aie::sqrt(aie::reduce_add(pow2)); // 横向求和后开方
    *outIter++ = res;                        // 每行输出一个标量
}
```

数学上，对一个长度为 \(n\) 的行向量 \(\mathbf{x}\)，欧氏范数是：

\[
\|\mathbf{x}\|_2 = \sqrt{\sum_{i=1}^{n} x_i^2}
\]

这正是「逐元素平方 → `reduce_add` 归约 → `sqrt`」三步。这一步在波束合成里反复出现——计算「像点到换能器/焦点的距离」本质上就是欧氏范数。

> numpy 对照表（帮助你建立直觉）：
> | L1 内核 | numpy 等价 | 说明 |
> |--------|-----------|------|
> | `mulVV`/`mulMM` | `a * b` | 逐元素乘 |
> | `sumVV`/`sumMM` | `a + b` | 逐元素加 |
> | `absV` | `np.abs(a)` | 逐元素绝对值 |
> | `sqrtV`/`squareV` | `np.sqrt`/`a**2` | 逐元素开方/平方 |
> | `norm_axis_1` | `np.linalg.norm(a, axis=1)` | 逐行欧氏范数 |
> | `sum_axis_1` | `np.sum(a, axis=1)` | 逐行求和 |
> | `outer` | `np.outer(a, b)` | 外积 |
> | `ones`/`tileVApo` | `np.ones`/`np.tile` | 生成/铺贴向量 |

#### 4.1.4 代码实践

**实践目标**：用眼睛「跑」一遍 L1 向量运算，建立 numpy↔aie_api 的直觉。

**操作步骤**：

1. 打开 [mulMM.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/mulMM/mulMM.cpp)，确认它的循环体只有 `load → mul → store` 三步。
2. 打开 [norm_axis_1.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/norm_axis_1/norm_axis_1.cpp)，对照上面的公式，指出哪一行对应「平方」、哪一行对应「归约」、哪一行对应「开方」。
3. 在 [L1/README.md](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/README.md) 里挑 3 个尚未读源码的内核（如 `reciprocalV`、`sign`、`outer`），先读它的文字描述，再用一句话写出它的 numpy 等价。

**需要观察的现象**：所有 L1 实现的结构都极其相似——都是 `for (i+=INCREMENT)` 循环里 `load_v → 某个 aie:: 运算 → store_v`。这种高度同构正是「numpy 风格原语」的特征。

**预期结果**：你能闭眼复述 L1 内核的「三段式循环」骨架，并能对每个内核给出 numpy 等价表达式。

#### 4.1.5 小练习与答案

**练习 1**：`norm_axis_1` 输入是一个矩阵（多行），为什么输出长度比输入短得多？
**参考答案**：因为它「逐行」归约——每一行（若干个 SIMD 向量）最终只 `reduce_add` 成一个标量再开方，输出每行一个值，所以输出是一个向量（行数个元素），远小于输入的「行数 × 列数」。

**练习 2**：`mul_square(op1)` 和 `mul(op1, op1)` 在数学上等价，为什么库要单独提供 `mul_square`？
**参考答案**：AIE 硬件有专门的平方指令（`mul_square`），只需读一个操作数、占用更少寄存器与数据通路；写成 `mul(op1,op1)` 虽然结果相同，却要装载/广播两次同一向量，效率更低。这是「numpy 等价」与「硬件实现」分开的原因。

---

### 4.2 L2 波束合成 kernel：参数结构体 + 迭代器 + 向量化融合

#### 4.2.1 概念说明

L2 是「Beamformer 的功能单元」。一个完整的超声波束合成器通常包含这些步骤：

1. **Image Points（像点生成）**：根据发射起点与方向，生成要成像的一组空间点。
2. **Delay（延迟计算）**：对每个像点、每个换能器，算出回波到达的「时间延迟」。
3. **Focusing（聚焦）**：算出每个换能器到参考点的「聚焦距离」。
4. **Apodization（变迹）**：根据 F 数（F-number）给每个换能器加权（典型是 Hanning 窗），抑制旁瓣。
5. **Sample（采样）**：由延迟换算出应在 RF 数据向量里取哪些采样索引。
6. **Interpolation（插值）**：对采样点做 B 样条插值，得到最终成像值。

`ultrasound` 把这 6 步各自做成一个 L2 内核（`kernel_imagepoints`/`kernel_delay`/`kernel_focusing`/`kernel_apodization`/`kernel_sample`/`kernel_interpolation`）。它们位于 `L1/include/kernel_*.hpp`（接口）与 `L1/include/kernel_*/`（实现）。

#### 4.2.2 核心流程

所有 L2 内核遵循同一套写法，由「三件套」组成：

1. **参数结构体 `para_*_t`**：把该内核需要的几何/物理参数（参考点、焦点、声速倒数、F 数等）和「三重迭代器」（`iter_line`/`iter_element`/`iter_seg`）打包在一起。迭代器表示当前在处理「第几条线 / 第几个换能器 / 第几段深度」。
2. **`update()` 方法**：一个三重嵌套计数器——段（seg）先走，走完一轮推进 element，element 走完推进 line。这就是把「线 × 元 × 段」的三维循环压进了一个一维的流式调用序列里。
3. **`kfun_*` 顶层函数**：真正的计算。它读 `input_buffer`、按 `VECDIM` 向量化处理、写 `output_buffer`。`_wrapper` 版本负责把运行时参数（RTP）加载进结构体、调用核心函数、再调用 `update()` 推进迭代器。

关键认知（也是本讲最重要的源码事实）：**L2 内核「概念上」是 L1 运算的组合，但「实现上」是把等价的向量运算融合（fuse）进了一个向量化循环，直接调用 `aie_api`（`aie::mul`/`aie::broadcast`/`aie::sqrt`/...），而不是逐个调用 L1 的 `mulMM`/`norm_axis_1` 函数。** 原因很现实：如果把每个 L1 原语都做成独立 AIE 内核再用流串起来，数据要反复进出内核、经过流 FIFO，开销远大于把运算融合到一个循环里。所以「L2 = L1 的组合」是**算法语义层面**的组合，不是**源码调用层面**的组合。

以 Delay（延迟）为例，单个像点的接收延迟数学上是：

\[
\begin{aligned}
s_1 &= \big|\,(x - x_\text{ref})\,\Delta_x + (z - z_\text{ref})\,\Delta_z\,\big| - d_0 \\
s_2 &= \sigma\sqrt{(x - x_\text{foc})^2 + (z - z_\text{foc})^2} + d_1,\quad \sigma = \text{sign}(-s_1) \\
\text{delay} &= s_2 / c - t_\text{start}
\end{aligned}
\]

其中 \(x,z\) 是像点坐标，\(x_\text{ref},z_\text{ref}\) 是发射参考点，\(x_\text{foc},z_\text{foc}\) 是焦点，\(\Delta_x,\Delta_z\) 是发射方向的 tile 步长，\(c\) 是声速，\(d_0,d_1\) 是距离偏移。这条公式里同时用到了「差（diff）」「乘（mul）」「绝对值（abs）」「平方和开方（norm）」「符号选择（sign/select）」——正是 L1 那一组原语。

#### 4.2.3 源码精读

先看接口。`kernel_delay.hpp` 定义了延迟内核的参数结构体与 `kfun_*` 声明。`para_delay_t` 把几何参数与三重迭代器打包（[kernel_delay.hpp:29-65](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/kernel_delay.hpp#L29-L65)）：

```cpp
template <class T>
struct para_delay_t {
    T tx_ref_point_x, tx_ref_point_z;   // 发射参考点
    T focal_point_x, focal_point_z;     // 焦点
    T t_start, inverse_speed_of_sound;  // 起始时间、声速倒数
    ...
    int32 iter_line, iter_element, iter_seg;  // 三重迭代器
    int32 num_line, num_element, num_seg;     // 三维的总量
    void update() {                          // 三重计数器推进
        if (iter_seg != num_seg - 1) iter_seg++;
        else { iter_seg = 0;
               if (iter_element != num_element - 1) iter_element++;
               else { iter_element = 0; iter_line++; } }
    }
};
```

这个 `update()` 在每个内核家族里都长得几乎一样（`kernel_apodization.hpp`、`kernel_focusing.hpp`、`kernel_interpolation.hpp`、`kernel_sample.hpp` 里都有同名同形的 `update()`），是识别「L2 内核」的可靠标志。

`kfun_UpdatingDelay_line_wrapper` 是顶层入口，它把传进来的运行时参数数组强转成 `para_delay_t*`、加载 `t_start`、调用核心函数、再 `update()`（[kernel_delay.cpp:131-143](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/kernel_delay/kernel_delay.cpp#L131-L143)）：

```cpp
void kfun_UpdatingDelay_line_wrapper(...) {
    para_delay_t<T>* p_const = (para_delay_t<T>*)para_const;  // 参数数组 → 结构体
    load_delay_rtp<T, NUM_LINE_t>(p_const, para_t_start);     // 加载运行时参数
    kfun_UpdatingDelay_line<T,...>(out_delay, p_const, in_img_x, in_img_z);  // 核心计算
    p_const->print();
    p_const->update();                                         // 推进迭代器
}
```

核心函数 `kfun_UpdatingDelay_line` 的向量化实现，正是上面那条延迟公式的 SIMD 版本。注意它**直接用 `aie::` intrinsic**，把「差、乘、绝对值、平方和、开方、符号选择」全部融合进一个 `chess_prepare_for_pipelining` 循环（[kernel_delay.cpp:101-128](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/kernel_delay/kernel_delay.cpp#L101-L128)）：

```cpp
for (int n = 0; n < p_const->num_dep_seg; n += VECDIM_delay_t) chess_prepare_for_pipelining {
    v_img_x = aie::load_v<VECDIM_delay_t>(p_in_delay_img_x);   // 装载一批像点 x
    v_img_z = aie::load_v<VECDIM_delay_t>(p_in_delay_img_z);   // 装载一批像点 z

    // DIM_X：差 → 乘；到焦点的差 → 平方
    v_diff1 = v_img_x - v_ref_x;
    acc_sample1 = aie::mul(v_diff1, v_til_x);
    v_diff2 = v_img_x - v_foc_x;
    acc_sample2 = aie::mul(v_diff2, v_diff2);
    // DIM_Z：同上
    ...
    acc_sample1 = aie::abs(acc_sample1) - v_dis_0;
    auto msk_lt = acc_sample1 < v_zeros;          // 符号判定
    auto v_sqrt = aie::sqrt(acc_sample2);         // 平方和开方 = 距离
    v_sign = aie::select(v_sqrt, aie::neg(v_sqrt), msk_lt);  // 按符号选正/负
    acc_sample2 = v_sign + v_dis_1;
    v_delay = aie::mul(acc_sample2, v_speed) - v_start;       // ×声速倒数 − t_start
    aie::store_v(p_out_delay, v_delay);
}
```

每个标量参数（如 `focal_point_x`）先用 `aie::broadcast` 扩展成同维向量（[kernel_delay.cpp:83-93](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/kernel_delay/kernel_delay.cpp#L83-L93)），这样标量与向量的运算就能在 SIMD 通道里对齐。

> 对照参考：文件开头有一段被注释掉的「非向量化版本」`fun_UpdatingDelay_line`（[kernel_delay.cpp:30-66](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/kernel_delay/kernel_delay.cpp#L30-L66)），它用标量 `for` 逐点算同一条公式。把它与向量化版本并排读，是理解「L1 原语如何被融合成 L2 向量循环」的最佳途径。

同样的「参数结构体 + `update()` + `kfun_*`」模式也出现在：变迹 `kernel_apodization.hpp`（`para_Apodization`，[L28-59](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/kernel_apodization.hpp#L28-L59)）、聚焦 `kernel_focusing.hpp`（`para_foc_t`，[L27-60](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/kernel_focusing.hpp#L27-L60)）、插值 `kernel_interpolation.hpp`（`para_Interpolation`，[L28-62](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/kernel_interpolation.hpp#L28-L62)）、采样 `kernel_sample.hpp`（`para_sample_t`，[L29-60](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/kernel_sample.hpp#L29-L60)）。变迹还分两个子函数：`kfun_apodization_pre`（算到参考点的距离倒数 invD）和 `kfun_apodization_main`（用 invD 与 F 数生成 Hanning 窗）。

#### 4.2.4 代码实践

**实践目标**：验证「L2 内核融合了 L1 等价运算」这一论断。

**操作步骤**：

1. 打开 [kernel_delay.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/kernel_delay/kernel_delay.cpp) 的向量化循环（101–128 行）。
2. 在循环体里找出与下列 L1 原语等价的 `aie::` 调用：
   - 对应 `mulMM`/`mulVV`（逐元素乘）→ 找 `aie::mul(...)`。
   - 对应 `absV`（绝对值）→ 找 `aie::abs(...)`。
   - 对应 `norm_axis_1`（平方 + 归约 + 开方）→ 找 `aie::mul_square` 或 `aie::mul(v,v)` + `aie::sqrt`。
   - 对应 `sign`（符号）→ 找 `aie::select(...)` + `aie::neg(...)`。
3. 在仓库根目录用搜索确认：`kernel_delay.cpp` 里**没有**出现 `us::L1::mulMM`、`us::L1::norm_axis_1` 这类对 L1 函数的调用。

**需要观察的现象**：你会看到一长串 `aie::mul`/`aie::abs`/`aie::sqrt`/`aie::select`，但找不到任何对 `us::L1::` 命名空间下 BLAS 函数的直接调用。

**预期结果**：由此得出结论——L2 把 L1 的语义「内联融合」进了向量化循环，而非逐个调用 L1 内核。

> 待本地验证：如果你已搭建好 Vitis/AIE 环境，可在该文件所在目录尝试 `make` 编译某个 L2 用例（见 4.4 节的构建命令），观察 AIE 编译器是否把该循环调度成单个高吞吐内核（II=1）。若无环境，本实践作为「源码阅读型实践」完成即可。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `para_delay_t` 里要同时存 `iter_*`（当前值）和 `num_*`（总量）？
**参考答案**：因为内核被设计成「每次调用处理一段（seg）」，需要知道当前进度（`iter_*`）才能加载对应的运行时参数（如 `t_start` 取自第 `iter_line` 条线），也需要知道总量（`num_*`）才能在 `update()` 里判断何时进位、何时回到 0。

**练习 2**：`aie::broadcast<T, VECDIM>(scalar)` 的作用是什么？为什么每个标量参数都要先 broadcast？
**参考答案**：它把一个标量复制成 `VECDIM` 维的向量（每个通道都是该标量）。因为 SIMD 运算要求两边维度一致，标量参数（如焦点坐标）必须先广播成向量，才能与像点向量做逐元素运算。

**练习 3**：`kfun_*_shell` 这一类「壳」函数（如 `kfun_UpdatingDelay_line_wrapper_shell`）只调用 `print()/update()` 而不算任何东西，它有什么用？
**参考答案**：它是用来**调试图连接**的——当你只想验证 ADF 图的端口/连线是否接对、不想跑真实计算时，用宏 `_USING_SHELL_` 切换到 shell 版本，可以极大加快仿真迭代。L2 图包装里正是用 `#ifdef _USING_SHELL_` 在两种内核间切换。

---

### 4.3 L2 graph 包装与三段式连接；L3 Beamformer 三种模式

> 本节把「L2 如何被包成图」与「L3 如何把多个 L2 图拼成完整 Beamformer」合并讲解，因为后者直接复用前者的连接约定。

#### 4.3.1 概念说明

光有一个 `kfun_*` 函数还不能上 AIE 阵列——必须把它包进一个 `adf::graph` 子图，声明它的输入/输出端口、指定源文件、设定运行比例。这就是 `L2/include/graph_*.hpp` 的职责。`graph_delay.hpp` 定义 `delay_graph_wrapper`，`graph_apodization.hpp` 定义 `apodi_pre_graph`/`apodi_main_graph`，等等。

L3 再把这些 L2 子图当作「积木」，在一个更大的 `adf::graph`（如 `scanline`）里实例化若干个、把端口连起来，构成完整 Beamformer。L3 README 指明有三种 Beamformer（[L3/README.md:6-7](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/README.md#L6-L7)）：SA（合成孔径）、PW（平面波）、ScanLine（扫描线，PW 的变体，区别在接收延迟计算）。

#### 4.3.2 核心流程

**L2 图包装的三段式**（每个 `graph_*.hpp` 都长这样）：

```text
1. kernel 定义：  k = adf::kernel::create(us::L1::kfun_*_wrapper<...>);
2. 源文件绑定：   adf::source(k) = "kernel_xxx/kernel_xxx.cpp";
3. 连接 + 比例：   adf::connect<>(port, k.in[i]) / adf::connect<>(k.out[0], port);
                   adf::runtime<adf::ratio>(k) = 0.8;
```

**L3 Beamformer 的组合**：在一个 `adf::graph` 子类里——

```text
1. #include 各 L2 头文件（imagePoints/delay/focusing/samples/apodization/bSpline）;
2. 声明 6 个 L2 子图成员（img/d/foc/sam/apo/interp）;
3. 为每个子图创建若干 input_plio / output_plio（PL↔AIE 边界）;
4. 用 adf::connect 把 PLIO 与子图端口一一接上;
5. 全局实例化一个图对象 scan；仿真时由 main() 的 init/run/end 驱动。
```

三种模式的差异：PW 与 ScanLine 共用同一套「发射单方向」结构，ScanLine 改了接收延迟（Delay）的算法；SA（合成孔径）则多了一条「发射侧」的聚焦/变迹链路（因此 SA 的输入端口表更长，含 `apo_ref_*_tx`、`focusing_output_tx` 等「发射侧」量）。

#### 4.3.3 源码精读

先看一个 L2 子图。`graph_delay.hpp` 把延迟内核包成 `delay_graph_wrapper`，三段式清晰可见（[graph_delay.hpp:35-59](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/../L2/include/graph_delay.hpp) 实际位于 L2/include/graph_delay.hpp）：

```cpp
delay_graph_wrapper() {
    // 1. kernel 定义（可在 shell/真实内核间切换）
    delay_kernel = adf::kernel::create(
        us::L1::kfun_UpdatingDelay_line_wrapper<T, NUM_LINE_t, ...>);
    // 2. 源文件
    adf::source(delay_kernel) = "kernel_delay/kernel_delay.cpp";
    // 3. 连接：参数走 async RTP，数据走普通流
    adf::connect<adf::parameter>(para_const,   async(delay_kernel.in[2]));
    adf::connect<adf::parameter>(para_t_start, async(delay_kernel.in[3]));
    adf::connect<>(img_x, delay_kernel.in[0]);
    adf::connect<>(img_z, delay_kernel.in[1]);
    adf::connect<>(delay_kernel.out[0], delay);
    adf::runtime<adf::ratio>(delay_kernel) = 0.8;
}
```

注意两类连接的区别：`adf::connect<adf::parameter>(..., async(...))` 是**异步运行时参数（RTP）**，用来传 `para_const`/`para_t_start` 这种每次调用都可能变的几何参数；`adf::connect<>(...)` 是普通**数据流**，传像点坐标与延迟结果。

变迹的 L2 包装更复杂一点，因为它分两段：`apodi_pre_graph`（算 invD）和 `apodi_main_graph`（算 Hanning 窗），各自包一个内核（[graph_apodization.hpp:37-111](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L2/include/graph_apodization.hpp#L37-L111)）。

**L3 组合的核心文件是 `graph.cpp`**。`scanline` 图类把 6 个 L2 子图声明为成员（[graph.cpp:75-80](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/tests/scanline/aie_graph/graph.cpp#L75-L80)）：

```cpp
us::L2::imagePoints_graph<> img   = us::L2::imagePoints_graph<>(KERNEL_RATIO_SCANLINE);
us::L2::delay_graph<>       d     = us::L2::delay_graph<>(KERNEL_RATIO_SCANLINE);
us::L2::focusing_graph<>    foc   = us::L2::focusing_graph<>(KERNEL_RATIO_SCANLINE);
us::L2::samples_graph<>     sam   = us::L2::samples_graph<>(KERNEL_RATIO_SCANLINE);
us::L2::apodization_graph<> apo   = us::L2::apodization_graph<>(KERNEL_RATIO_SCANLINE);
us::L2::bSpline_graph<>     interp= us::L2::bSpline_graph<>(KERNEL_RATIO_SCANLINE);
```

构造函数里，为每个子图创建 PLIO 边界端口，再用 `adf::connect` 把 PLIO 与子图端口一一接上。以 DELAY 段为例（[graph.cpp:95-117](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/tests/scanline/aie_graph/graph.cpp#L95-L117)）：

```cpp
image_points_from_PL = adf::input_plio::create("image_points_from_PL",
                                               adf::plio_32_bits, "data/image_points.txt");
...
adf::connect<>(image_points_from_PL.out[0],   d.image_points_from_PL);
adf::connect<>(tx_def_ref_point.out[0],       d.tx_def_ref_point);
adf::connect<>(tx_def_focal_point.out[0],     d.tx_def_focal_point);
adf::connect<>(t_start.out[0],                d.t_start);
adf::connect<>(d.delay_to_PL, delay_to_PL.in[0]);   // 结果流回 PL
```

`adf::plio_32_bits` 表示每个 PLIO 32 比特宽（一个 `float`）。仿真时 `input_plio` 从 `data/*.txt` 读、`output_plio` 写到 `data/*.txt`；上板时它们对应 PL 侧 mm2s/s2mm 的 AXI-Stream。

最后两行是图的「激活」与「仿真入口」（[graph.cpp:185-197](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/tests/scanline/aie_graph/graph.cpp#L185-L197)）：

```cpp
us::L3::scanline scan;                  // 全局图对象
#if defined(__AIESIM__) || defined(__X86SIM__)
int main(void) {
    scan.init(); scan.run(1); scan.end();   // 仿真：init/run/end 三段式
    return 0;
}
#endif
```

注意一个重要的架构事实：在这份 `scanline` 测试图里，**6 个 L2 子图彼此并不直接互连**，而是每个都各自接一组 PLIO——即每个功能单元的输入从 PL（进而 DDR）来、输出回到 PL（进而 DDR）去。完整的波束合成流水线（像点→延迟→采样→聚焦→变迹→插值）是由**主机 + PL 搬运器**在外层编排的，数据在功能单元之间经由 PL/DDR 中转。这是 `ultrasound` 当前实现的工程选择，理解这一点比记住「流水线是直连」更重要。

上板时，主机端用 XRT 控制 AIE 图与 PL 搬运器。L3 主机代码 `scanline.hpp` 里可以看到标准的图生命周期控制（[scanline.hpp:271-278](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/include/scanline.hpp#L271-L278)）：

```cpp
scan.init();   std::cout << "graph init" << std::endl;
scan.run(ITER); std::cout << "graph run"  << std::endl;
scan.end();    std::cout << "graph end"   << std::endl;
```

主机同时在 PL 侧用 `xrtPLKernelOpen` 打开 28 个 `mm2s`（喂输入）和 6 个 `s2mm`（收输出）搬运内核（[scanline.hpp:230-265](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/include/scanline.hpp#L230-L265)），把输入 BO 同步到设备、启动搬运、跑图、等搬运结束、再把输出 BO 同步回来。这与 u4-l2/u4-l3 讲的 `xrt::device/kernel/run/bo` 是同一套机制（只是这里用底层 C 句柄 `xrtDeviceHandle`/`xrtKernelHandle`）。

#### 4.3.4 代码实践

**实践目标**：把「L2 图包装的三段式」与「L3 实例化 6 个 L2 子图」两件事在源码里对上号。

**操作步骤**：

1. 打开 [graph_delay.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L2/include/graph_delay.hpp)，在构造函数里标出三段式：`kernel::create` / `source` / `connect + runtime`。
2. 打开 [graph.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/tests/scanline/aie_graph/graph.cpp)，把 75–80 行的 6 个 L2 子图成员，与构造函数里对应的连接段（IMAGEPOINTS / DELAY / FOCUSING / SAMPLES / APODIZATION / INTERPOLATOR 注释块）一一对应。
3. 数一数：`scanline` 图一共声明了几个 `input_plio`、几个 `output_plio`？它们分别对应 L3 README 里 ScanLine 的哪些 Graph Inputs/Outputs？

**需要观察的现象**：每个 L2 子图都「自带一组 PLIO」，6 个子图之间没有 `adf::connect<>(子图A.out, 子图B.in)` 这样的直接互连。

**预期结果**：得出结论——L3 的「组合」在当前实现里是「在同一个 ADF 图里放置 6 个 L2 功能单元，各自经 PLIO 与 PL/DDR 交互」，而非 AIE 内部直连的紧耦合流水线。

#### 4.3.5 小练习与答案

**练习 1**：`adf::connect<adf::parameter>(p, async(k.in[i]))` 与 `adf::connect<>(p, k.in[i])` 有何区别？分别用来传什么？
**参考答案**：前者是异步运行时参数（RTP），传每次调用都可能变化的标量/小结构体（如几何参数 `para_const`、`t_start`），不在数据流主路径上；后者是普通数据流连接，传批量像点坐标、延迟结果等真正的「数据」。

**练习 2**：PW 与 ScanLine 两种 Beamformer 的主要区别在哪？
**参考答案**：据 L3 README，ScanLine 是 PW 的变体，区别在**接收延迟（Delay）的计算方式**；其余功能单元（像点、聚焦、采样、变迹、插值）结构相同。因此两者的输入端口表几乎一样，只是 Delay 相关输入（如 `tx_def_delay_distance`）语义不同。

**练习 3**：SA（合成孔径）的输入端口表为什么比 PW/ScanLine 长得多？
**参考答案**：因为 SA 既要在发射侧也做聚焦与变迹，所以多了一整条「发射侧」链路（`apo_ref_*_tx`、`apodization_reference_tx`、`apo_distance_k_tx`、`focusing_output_tx`、`apodization_tx` 等），输入近乎翻倍。

---

### 4.4 层层组合：L1→L2→L3 依赖关系与综合实践

#### 4.4.1 概念说明

把前三个模块串起来，`ultrasound` 的三层组合关系可以画成一张清晰的依赖图。这张图也是本讲的综合主线。

#### 4.4.2 核心流程

```text
┌─────────────────────────── L1：numpy 风格 AIE 原语 ───────────────────────────┐
│  ones / outer / tileV            （向量生成与变形）                              │
│  mulMM / mulVV / mulVS           （逐元素乘）                                    │
│  sumMM / sumVV / sumVS           （逐元素加）                                    │
│  diffMV / diffSV / diffVS        （逐元素差）                                    │
│  absV / squareV / sqrtV / sign   （逐元素一元）                                  │
│  norm_axis_1 / sum_axis_1        （逐行归约）                                    │
│  reciprocalV / cosV              （逐元素函数）                                  │
└                              （语义上被组合 ↓）                                  ─┘
                                   │ 概念组合（实现上融合为向量化循环，直接用 aie::）
                                   ▼
┌─────────────────────────── L2：Beamformer 功能单元 ──────────────────────────┐
│  kernel_imagepoints  像点生成        kernel_sample      采样索引              │
│  kernel_delay        延迟计算        kernel_apodization 变迹（pre+main）      │
│  kernel_focusing     聚焦距离        kernel_interpolation B 样条插值          │
│  每个内核 = para_*_t 结构体 + update() 三重迭代器 + kfun_* 向量化循环           │
│  每个内核被 graph_*.hpp 包成 adf::graph 子图（kernel::create + source + connect）│
└                              （被实例化、连接 ↓）                              ─┘
                                   │ 在一个 L3 adf::graph 里实例化 6 个 L2 子图
                                   ▼
┌─────────────────────────── L3：完整 Beamformer ──────────────────────────────┐
│  scanline / plane_wave（PW）/ synthetic_aperture（SA）                          │
│  在一个图里实例化 imagePoints/delay/focusing/samples/apodization/bSpline 6 子图 │
│  各子图经 PLIO ↔ PL(mm2s/s2mm) ↔ DDR，由主机 XRT 编排                          │
│  主机：load_xclbin → 配输入/输出 BO → 启动 28 mm2s + 6 s2mm → scan.init/run/end │
└──────────────────────────────────────────────────────────────────────────────┘
```

一句话总结这条链：**L1 提供 numpy 风格的向量/矩阵原语 → L2 把这些原语的语义融合进波束合成各功能单元的向量化内核、并包成子图 → L3 在一个图里实例化 6 个 L2 子图、经 PLIO 与主机/PL 协同构成完整 Beamformer。**

#### 4.4.3 源码精读（构建与运行入口）

L3 scanline 测试的 `description.json` 把三层都串进了同一份构建描述（[scanline/description.json:5-7](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/tests/scanline/description.json#L5-L7)）：`"flow": "system"`、平台白名单 `vck190`。它声明了两类容器：

- **AIE 容器（`aiecontainers`）**：以 `graph.cpp` 为入口，编译成 `libadf.a`（[description.json:61-72](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/tests/scanline/description.json#L61-L72)）——这就是 L3 的 ADF 图。
- **PL 容器（`containers`）**：28 个 `mm2s` + 6 个 `s2mm` PL 内核（[description.json:73-284](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/tests/scanline/description.json#L73-L284)）——这就是搬运层。

主机编译时同时引入三层 include 路径（[description.json:42-46](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/tests/scanline/description.json#L42-L46)）：`L1/include`、`L2/include`、`L3/include`、以及测试自己的 `aie_graph` 目录——三层共享同一套头文件。

测试目标与平台（[description.json:295-301](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/tests/scanline/description.json#L295-L301)）：`vitis_aie_sim`、`vitis_aie_x86sim`、`vitis_hw_emu`、`vitis_hw_build`，即 AIE 专有的两档仿真（aiesim 周期精确、x86sim 功能快速）+ hw_emu + hw。

Makefile 给出了标准用法（[scanline/Makefile:22-24](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/tests/scanline/Makefile#L22-L24)）：

```bash
make all  TARGET=<aiesim/x86sim/hw_emu/hw> PLATFORM=<FPGA platform>
make run  TARGET=<aiesim/x86sim/hw_emu/hw> PLATFORM=<FPGA platform>
```

默认 `TARGET ?= aiesim`、`PLATFORM ?= vck190`（[Makefile:52-62](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/tests/scanline/Makefile#L52-L62)）。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：梳理 `ultrasound/L1/include` 里 `kernel_*`（L2 功能单元）与底层向量运算（`mulMM`/`sumVV`/`absV`/`norm_axis_1` 等 L1 原语）的依赖，亲手画出 L1→L2（→L3）的组合关系图。

**操作步骤**：

1. **列 L1 清单**：浏览 [L1/include](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/kernels.hpp)，把所有「非 `kernel_*`」的 L1 向量/矩阵原语列成一张表（名字 + numpy 等价），可参考 [L1/README.md](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/README.md) 的描述。
2. **列 L2 清单**：列出 6 个 `kernel_*` 功能单元（imagepoints/delay/focusing/sample/apodization/interpolation），逐个打开其 `kernel_*.hpp`，记下它的 `para_*_t` 里有哪些几何参数。
3. **建立依赖**：对每个 L2 内核，打开它的实现 `.cpp`（如 [kernel_delay.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/kernel_delay/kernel_delay.cpp)），在向量化循环里找出用到的 `aie::` 运算，把它们映射回等价的 L1 原语。例如 Delay 用到 `aie::mul`→`mulMM/mulVV`、`aie::abs`→`absV`、`aie::sqrt`+平方→`norm_axis_1`、`aie::select`+`aie::neg`→`sign`。
4. **画图**：用你顺手的工具（纸笔、Mermaid、draw.io）画出三栏图：左栏 L1 原语、中栏 L2 功能单元、右栏 L3 Beamformer，用箭头表示「语义组合」关系（注意标注：实现上是融合，不是函数调用）。
5. **补 L3**：在右栏画出 `scanline` 图实例化的 6 个 L2 子图，并标注它们各自经 PLIO 与 PL/DDR 交互（而非互连）。

**需要观察的现象**：同一个 L1 原语（如「逐元素乘」对应的 `aie::mul`）会被多个 L2 内核复用（Delay、Focusing、Apodization 都用到乘法）；而 L2 内核之间在 AIE 图里没有直接数据流连接。

**预期结果**：你得到一张三栏的 L1→L2→L3 依赖关系图，并能口头解释「为什么 L2 是融合实现而非逐个调用 L1」。

> 待本地验证：若有 Vitis 2022.2+ 与 VCK190 平台，可执行 `cd ultrasound/L3/tests/scanline && make run TARGET=x86sim PLATFORM=vck190`，观察 x86 仿真是否跑通并产出 `data/*.txt` 输出文件（这是最快的功能验证档）。若无环境，本实践作为「源码阅读 + 画图」型实践完成。

#### 4.4.5 小练习与答案

**练习 1**：如果要把「逐元素乘」这个 L1 原语单独做成 AIE 内核再用流串进 Delay 内核，相比现在的融合实现，主要劣势是什么？
**参考答案**：数据要额外进出一次内核、经过流 FIFO，带来延迟与吞吐损失；还会多占用 AIE 核与流路由资源。融合实现把乘法直接嵌在 Delay 的向量化循环里，数据留在寄存器里，吞吐最高。

**练习 2**：在 `scanline` 图里，Delay 的输出 `delay_to_PL` 最终被谁消费？
**参考答案**：在当前 `scanline` 测试图里，`delay_to_PL` 流回 PL（写出到 `data/delay_to_PL.txt`），再由主机的 Sample 阶段经另一个 PLIO（`delay_from_PL`）读入。即 Delay 与 Sample 之间是通过 PL/DDR 中转，而非 AIE 直连。

**练习 3**：`description.json` 里为什么要把 AIE 图（`aiecontainers`）和 PL 内核（`containers`）分开声明？
**参考答案**：因为两者走不同的编译后端——AIE 图由 AIE 编译器编成 `libadf.a`（映射到 AI Engine 阵列），PL 内核由 v++ 编成 XO 再链接；它们在 `system.cfg` 里通过 AXI-Stream（PLIO）对接。分开声明让两套工具链各自拿到正确的输入。

## 5. 综合实践

把本讲全部知识串起来，完成下面这个「最小波束合成阅读报告」：

1. **选一个 L2 功能单元**（推荐 Delay 或 Apodization），完整阅读它的 `kernel_*.hpp`（接口）+ `kernel_*/.cpp`（实现）+ `graph_*.hpp`（图包装）三件套。
2. **写出它的数据流契约**：输入有哪几路 buffer、各代表什么几何量？输出是什么？哪些参数走 RTP？
3. **分解它的向量循环**：把它的 `aie::` 运算序列翻译回数学公式，并标注每一步对应哪个 L1 原语。
4. **追踪它在 L3 scanline 图里的位置**：在 [graph.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/tests/scanline/aie_graph/graph.cpp) 里找到它的实例化与 PLIO 连接，确认它的输入来自哪些 `.txt`、输出写到哪个 `.txt`。
5. **回答**：如果把这个功能单元从「融合实现」改成「逐个调用 L1 原语内核」，会损失什么？如果在 L3 把它与前一个功能单元改成 AIE 直连（而不是经 PLIO），需要改动哪些地方？

完成这份报告，你就把「L1 原语 → L2 功能单元 → L3 Beamformer」的层层组合真正走了一遍。

## 6. 本讲小结

- `ultrasound` 是一个**纯 AIE** 垂直库，三层结构是「层层组合」的范本：L1 是 numpy 风格的向量/矩阵原语，L2 是波束合成功能单元，L3 是完整 Beamformer。
- **L1 原语**高度同构：`for (i+=INCREMENT)` 循环里 `aie::load_v → 某个 aie:: 运算 → aie::store_v`；`mulMM`/`norm_axis_1` 是最典型的两个样本，可一一对应 numpy。
- **L2 内核**遵循「`para_*_t` 参数结构体 + `update()` 三重迭代器 + `kfun_*` 向量化循环」三件套；关键事实是它在**语义上**组合 L1 原语，在**实现上**把等价运算融合进单个向量化循环直接调用 `aie_api`，而非逐个调用 L1 函数。
- **L2 子图**用三段式 `kernel::create + source + connect(+runtime ratio)` 把内核包成 `adf::graph`，区分数据流连接与异步 RTP 参数连接。
- **L3 Beamformer** 有 SA/PW/ScanLine 三种模式；在 `scanline` 测试图里，6 个 L2 子图被实例化到同一个 ADF 图，但**各自经 PLIO 与 PL/DDR 交互、彼此不直接互连**，流水线由主机 + PL 搬运器编排。
- 构建入口是 L3 测试目录的 `description.json`（`flow=system`，声明 `aiecontainers` 与 PL `containers`）与 Makefile（`TARGET=aiesim/x86sim/hw_emu/hw`，`PLATFORM=vck190`）。

## 7. 下一步学习建议

- **AIE 编程模型深入（u13-l1/u13-l2）**：本讲只用到 `adf::graph`/`kernel`/`connect`/`PLIO` 的最基本用法。要理解 `runtime<ratio>`、`async` RTP、PLIO↔AXI-Stream 的边界细节，以及主机端 `xrt::graph` 的 `init/run/end` 控制，请接着读 u13 单元（它会用 dsp 库的 vss 示例更系统地讲 ADF 图）。
- **想动手改参数**：可以尝试在 `scanline` 测试目录修改 `system.cfg` 或 `data/*.txt` 输入，跑 `x86sim` 观察输出变化（需本地 Vitis 环境）。
- **对照其他 AIE 库**：把本讲的 `kernel_*` 三件套与 dsp 库的「kernel+traits+utils」四件套（u6-l2）对照阅读，体会同一个 AIE 范式在不同领域库里的组织差异。
- **L3 流水线组合（u5-l3）**：本讲的 L3 是「PLIO 松耦合」组合；u5-l3 讲的 vision/blas L3 则是「片上 DATAFLOW 紧耦合」组合。把两者对照，能看清 AIE 与 PL 两条路线在「应用流水线」组织方式上的根本不同。
