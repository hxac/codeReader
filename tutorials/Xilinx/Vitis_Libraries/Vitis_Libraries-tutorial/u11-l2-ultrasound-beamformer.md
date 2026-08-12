# 超声波束合成器：L1→L2→L3 组合

## 1. 本讲目标

本讲带你走进 `ultrasound`（超声）库——Vitis_Libraries 里一个高度垂直、且把「三层抽象」贯彻得最彻底的库。读完本讲你应该能够：

- 说清 `ultrasound` 库 L1/L2/L3 各自的产物，以及它们之间是**怎样一层一层组合**出来的。
- 认出 L1 那一组 NumPy 风格的向量/矩阵原语（`mulMM`/`sumVV`/`squareV`/`sqrtV`/`diffSV`/`norm_axis_1` 等），并理解它们为何是整个库的「乐高积木」。
- 读懂一个 L2 波束合成功能单元（如 `focusing_graph`、`delay_pw_graph`）如何把若干 L1 原语用 ADF 数据流串成一条流水线。
- 区分 L3 完整 Beamformer 的三种成像模式：**PW（平面波）**、**SA（合成孔径）**、**ScanLine（扫描线）**，以及它们各自复用了哪些 L2 单元。
- 自己动手画出「L1 原语 → L2 功能单元 → L3 Beamformer」的组合关系图。

## 2. 前置知识

本讲默认你已经学过：

- **u1-l3**：L1/L2/L3 三层抽象与 PL/AIE 两种范式（知道 L1 是原语、L2 是内核、L3 是应用流水线）。
- **u5-l3**：多内核流水线组合（知道 L3 的价值在于把多内核缝合成端到端流水线）。
- **u8-l1**：BLAS 三级抽象（知道「按运算对象分级」与「工程 L1/L2/L3 分层」不是一回事）。

下面用三段大白话补一点超声成像的领域常识，**不懂这些就读不懂代码在算什么**：

- **探头（probe）与阵元（transducer/element）**：超声探头是一排小喇叭（阵元），每个阵元既能发射（TX）也能接收（RX）声波。阵元的位置在代码里叫 `xdc_def_*`（XDC = transducer）。
- **RF 数据**：每个阵元接收到的回波是一串随时间采样的数值，叫射频（RF）数据。波束合成（beamforming）的 job，就是把这堆来自不同阵元、不同时刻的 RF 采样「对齐相加」，得到一条清晰的成像线——这是经典的**延时叠加（Delay-and-Sum, DAS）**。
- **三种成像模式的差别只在「怎么发射」**：平面波（PW）一次性照亮整片区域；合成孔径（SA）逐个阵元当「虚拟源」发射、收齐后再合成；扫描线（ScanLine）则是传统的一条线一条线聚焦发射。**接收端**的算法三者基本共用，所以你会在 L3 看到三种模式复用同一批 L2 单元。

两个贯穿全讲的物理量：

- 声速 \(c\)，库内常量 `SPEED_OF_SOUND = 1540` m/s（人体软组织近似值）。
- 延时 \(\tau = \text{距离}/c\)。波束合成的核心就是把各种距离换算成「采样点序号」再去 RF 数据里取值。

## 3. 本讲源码地图

| 文件 | 所属层 | 作用 |
|------|--------|------|
| [ultrasound/README.md](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/README.md) | 总览 | 一句话点明 L1/L2/L3 各自是什么 |
| [ultrasound/L1/include/kernels.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/kernels.hpp) | L1 | 全部 L1 向量/矩阵原语的模板声明（积木清单） |
| [ultrasound/L1/include/mulMM/mulMM.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/mulMM/mulMM.cpp) | L1 | 一个典型原语的实现（看 `aie::mul` 怎么用） |
| [ultrasound/L1/include/kernel_focusing.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/kernel_focusing.hpp) | L1 | 「整块式」聚焦内核 `kfun_foc` 的声明（另一条实现路线） |
| [ultrasound/L1/include/kernel_focusing/kernel_focusing.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/kernel_focusing/kernel_focusing.cpp) | L1 | `kfun_foc` 的实现，直接调 `aie::` intrinsic |
| [ultrasound/L1/include/kernel_apodization.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/kernel_apodization.hpp) | L1 | 变迹（apodization）内核声明 |
| [ultrasound/L1/include/kernel_delay.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/kernel_delay.hpp) | L1 | 延时（delay）内核声明 |
| [ultrasound/L1/include/kernel_interpolation.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/kernel_interpolation.hpp) | L1 | 插值（interpolation）内核声明 |
| [ultrasound/L2/include/focusing.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L2/include/focusing.hpp) | L2 | **功能图**：用 `diffSV→squareV→sumVV→sqrtV` 拼出聚焦 |
| [ultrasound/L2/include/delay_pw.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L2/include/delay_pw.hpp) | L2 | **功能图**：用 `diffMV→mulMM→sum_axis_1→divVS→diffVS` 拼出 PW 延时 |
| [ultrasound/L2/include/graph_focusing.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L2/include/graph_focusing.hpp) | L2 | **包装图**：把整块 `kfun_foc` 包成 ADF graph |
| [ultrasound/L3/tests/plane_wave/aie_graph/graph.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/tests/plane_wave/aie_graph/graph.cpp) | L3 | PW Beamformer：把 6 个 L2 图缝成一张大图 |
| [ultrasound/L3/include/plane_wave.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/include/plane_wave.hpp) | L3 | PW Beamformer 的主机控制（`init/run/end`） |
| [ultrasound/L3/tests/README.md](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/tests/README.md) | L3 | 如何跑一个 L3 用例（四档仿真） |

## 4. 核心概念与源码讲解

### 4.1 L1 向量运算：NumPy 风格的 BLAS 积木

#### 4.1.1 概念说明

`ultrasound` 库的 L1 不是「一个完整的算法」，而是一盒**最小可复用的向量/矩阵运算**。它的设计目标在 [L1 README](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/README.md) 里说得很直白：在 NumPy 与 AI Engine 的 C++ SIMD API 之间建一个「最近映射」。也就是说，每个 L1 原语都对应一个你熟悉的 NumPy 写法——`absV` 就是 `np.abs`，`sumVV` 就是 `a + b`，`mulMM` 就是逐元素 `A * B`，`norm_axis_1` 就是按行求欧几里得范数 `np.linalg.norm(A, axis=1)`。

这盒积木分两组：

1. **逐元素运算**：`sumVV`/`sumMM`、`mulVV`/`mulMM`/`mulVS`、`diffVS`/`diffSV`/`diffMV`、`squareV`/`sqrtV`/`absV`/`reciprocalV`/`cosV`/`sign`、`divVSSpeedOfSound` 等。
2. **向量管理与生成**：`ones`（全 1 向量）、`outer`（外积，两向量拼成矩阵）、`tileVApo`（把一个向量铺成长矩阵）、`sum_axis_1`/`norm_axis_1`（按行规约）。

为什么要把运算拆得这么碎？因为 AIE 是 SIMD 处理器，每个原语恰好能映射成一条或几条向量 intrinsic，编译器好调度；而把复杂算法写成这些原语的**数据流组合**，就能让 AIE 阵列里的多个核同时流水起来（见 4.3）。

> 关键术语：**AIE**（AI Engine，Versal 里的向量处理器阵列）、**SIMD**（单指令多数据，一条指令同时处理一个向量）、**intrinsic**（直接对应硬件指令的 C++ 函数，如 `aie::mul`）、**`adf::input_buffer`/`output_buffer`**（ADF 图里 kernel 之间的数据缓冲，类似定向 FIFO）。

#### 4.1.2 核心流程

所有 L1 原语都长一个样：模板化、吃 `adf::input_buffer`、吐 `adf::output_buffer`、内部循环按 `VECDIM` 宽度做向量读写。以 `mulMM`（两矩阵逐元素相乘）为例：

```text
读: in1[i], in2[i]  ──aie::load_v──▶  op1, op2 (各 VECDIM 个元素)
算: res = aie::mul(op1, op2)            ← 一条 SIMD 指令完成 VECDIM 个乘法
写: out[i]  ◀──aie::store_v──  res
对 i 按 INCREMENT 步进，循环 LEN/INCREMENT 次
```

四个模板参数是理解所有原语的钥匙：

- `T`：元素类型（通常 `float`）。
- `LEN`：本次调用要处理的元素总数。
- `INCREMENT`：SIMD 一次推进的「逻辑步」，与已完成的迭代次数相关。
- `VECDIM`：一条 SIMD 指令处理的元素数（由类型决定，见 Xilinx UG1076）。

#### 4.1.3 源码精读

`mulMM` 的声明在积木清单 [ultrasound/L1/include/kernels.hpp:79-82](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/kernels.hpp#L79-L82)，注释写明「两矩阵逐元素相乘」。同文件里还能看到 [sumVV 声明 kernels.hpp:132-135](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/kernels.hpp#L132-L135)、[absV kernels.hpp:87-88](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/kernels.hpp#L87-L88)、[norm_axis_1 kernels.hpp:100-101](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/kernels.hpp#L100-L101)——都是同一套模板签名。常量声速定义在 [kernels.hpp:37](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/kernels.hpp#L37)（`#define SPEED_OF_SOUND 1540`）。

实现体短得可爱，[ultrasound/L1/include/mulMM/mulMM.cpp:23-47](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/mulMM/mulMM.cpp#L23-L47)：

```cpp
for (unsigned i = 0; i < LEN; i += INCREMENT) {
    op1 = aie::load_v<VECDIM>(p_in1);   // 一次读 VECDIM 个元素
    op2 = aie::load_v<VECDIM>(p_in2);
    res = aie::mul(op1, op2);            // 一条 SIMD 完成 VECDIM 个乘法
    aie::store_v(p_out, res);
    // 指针按字节前移 VECDIM*sizeof(T)
}
```

这就是「乐高积木」的样子：一个原语 = 一个 `for` + 一条 `aie::` 向量指令。其他原语（`sumVV`→`aie::add`、`squareV`→逐元素平方、`sqrtV`→`aie::sqrt`）都是同一套模板，只是中间那条 intrinsic 换了。

> 旁注：库内还有一组 `L1/include/l1-libraries_aieml/` 下的同名原语，是为更新的 **AIE-ML** 架构重写的版本；本讲走经典 AIE 的 `kernels.hpp` 路线，两者数学语义一致。

#### 4.1.4 代码实践

**目标**：用「读声明 + 读 README」的方式，快速识别三个原语的语义，验证它们确实只是 NumPy 的逐元素映射。

**步骤**：

1. 打开 [ultrasound/L1/README.md](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/README.md)，找到 `diffSV`、`norm_axis_1`、`outer` 三个小节。
2. 对照 [kernels.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/kernels.hpp) 里它们的声明签名。
3. 给每个原语写一句「等价 NumPy」。

**需要观察的现象**：三个原语的模板参数都是 `(T, LEN, INCREMENT, VECDIM)` 四件套（`equalS`/`lessOrEqualThanS` 多一个 `SCALAR`），输入输出全是 `adf::buffer`。

**预期结果**（供核对）：

- `diffSV(in1=scalar, in2=vector)`：标量减向量 → `scalar - in2`。
- `norm_axis_1(matrix)`：对矩阵按行求欧几里得范数，返回每行一个数 → `np.linalg.norm(M, axis=1)`，内部等价于 `sqrt(sum(square(M), axis=1))`（这条等式在 4.2 会原样出现）。
- `outer(v1, v2)`：两向量外积，结果是矩阵 → `np.outer(v1, v2)`。

如果你无法本地跑 AIE 仿真，这是「源码阅读型实践」，不需要运行即完成。

#### 4.1.5 小练习与答案

**练习 1**：`mulMM` 和数学课上的「矩阵乘法」 \(AB\) 是一回事吗？
**答案**：不是。`mulMM` 是**逐元素**乘（Hadamard 积），要求两矩阵同形；真正的矩阵乘要用 BLAS 的 GEMM（见 u8-l1）。注意别被名字骗了。

**练习 2**：`divVS` 的全名为什么是 `divVSSpeedOfSound`？它把向量除以的「标量」从哪来？
**答案**：它特化为「向量 ÷ 声速」，标量是硬编码的声速常量。看 [kernels.hpp:104](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/kernels.hpp#L104) 的声明与 README「divVS」小节——把距离换算成时间延时 \(\tau=d/c\) 正需要除以 \(c\)。

**练习 3**：为什么 `VECDIM` 要做成模板参数，而不是写死？
**答案**：因为 AIE 的 SIMD 宽度随元素类型变化（`float` 与 `cint16` 的每周期吞吐不同，见 UG1076）。做成模板参数，让上层按类型选对宽度，才能榨满每条向量指令。

---

### 4.2 L2 波束合成功能单元：用 L1 原语拼出 delay 与 focusing

#### 4.2.1 概念说明

L2 是「波束合成的功能单元」。L2 README [ultrasound/L2/README.md](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L2/README.md) 把它定义成「基于 L1 内核的 AIE 图」——也就是说，**一个 L2 功能单元 = 一张把若干 L1 原语缝起来的 ADF 数据流图**。这一层开始有「领域含义」了：

- **Image Points**：生成代表「要成像的那些点」的坐标矩阵。
- **Delay / Delay_PW**：算**发射延时**——声波从发射参考点到某个成像点所需时间。
- **Focusing / Focusing_SA**：算每个阵元到「变迹参考点」的距离，供动态变迹用。
- **Samples**：算**接收延时**并加上发射延时，定位每个阵元应在 RF 数据里取哪个采样。
- **Apodization / Apodization_SA**：为每个阵元算一个 Hanning 窗权重，压低旁瓣。
- **bSpline**：对 RF 数据做插值（Catmull-Rom），因为算出来的采样位置通常是分数。

注意一条贯穿全讲的等式——**Focusing 算的就是欧几里得范数**。每个阵元到参考点的距离：

\[
\|\mathbf{d}\| = \sqrt{(x_{\text{apo}}-x_{\text{xdc}})^2 + (z_{\text{apo}}-z_{\text{xdc}})^2}
\]

把它拆成 L1 原语就是：`diffSV`（相减）→ `squareV`（平方）→ `sumVV`（两轴相加）→ `sqrtV`（开方）。4.1.4 里那个 `norm_axis_1` 的等价式，在这里被手工展开成了一条流水线。

#### 4.2.2 核心流程

`focusing_graph`（聚焦功能图）的内部数据流：

```text
apo_ref_0 ─┐
           ├─▶ diffSV ─▶ squareV ─┐
xdc_def_0 ─┘                      ├─▶ sumVV ─▶ sqrtV ─▶ focusing_output
apo_ref_1 ─┐                      │
           ├─▶ diffSV ─▶ squareV ─┘
xdc_def_1 ─┘
```

这条链恰好就是 \( \sqrt{\Delta x^2 + \Delta z^2} \)。每个方框是**一个独立的 AIE kernel**（由 `adf::kernel::create(us::L1::xxx)` 创建），方框之间用 `adf::connect<>` 连成定向数据流，编译后会被映射到不同的 AIE 核上并行流水。

PW 发射延时 `delay_pw_graph` 的内部数据流则把「点到参考点的距离 ÷ 声速 − 起始时刻」展开为：

```text
image_points ─┐
              ├─▶ diffMV ─▶ mulMM(+tileV) ─▶ sum_axis_1 ─▶ divVS ─▶ diffVS ─▶ delay_to_PL
ref_point ────┘   (点-参考点)   (逐元素平方)     (Σx²+y²+z²)     (÷c)      (−t_start)
t_start ──────────────────────────────────────────────────────────────┘
```

对应公式：

\[
\tau_{\text{PW}} = \frac{\sqrt{(\mathbf{p}-\mathbf{r})\cdot(\mathbf{p}-\mathbf{r})}}{c} - t_{\text{start}}
\]

（库内 `mulMM` 配合 `tileVApo` 把差值逐元素平方，省去单独的 `squareV`。）

#### 4.2.3 源码精读

**`focusing_graph`**：[ultrasound/L2/include/focusing.hpp:41-46](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L2/include/focusing.hpp#L41-L46) 用一行一个 `kernel::create` 把六个 L1 原语实例化（两个 `diffSV`、两个 `squareV`、一个 `sumVV`、一个 `sqrtV`），并各指定 `source(...)` 指向对应 `.cpp`；随后 [focusing.hpp:56-93](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L2/include/focusing.hpp#L56-L93) 用一连串 `adf::connect<>` 把它们缝成 4.2.2 那张图，并用 `adf::dimensions(...)` 钉死每条边的向量长度（`DIM_VECTOR_`）。

**`delay_pw_graph`**：[ultrasound/L2/include/delay_pw.hpp:43-48](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L2/include/delay_pw.hpp#L43-L48) 实例化六个原语（`tileVApo`/`diffMV`/`mulMM`/`sum_axis_1`/`divVSSpeedOfSound`/`diffVSStreamOut`），连接关系在 [delay_pw.hpp:57-89](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L2/include/delay_pw.hpp#L57-L89)。注意这里矩阵边长用 `DIM_MATRIX_`（= `LENGTH * SPACE_DIMENSION`，对应 Nx4 的点矩阵），规约后变回 `DIM_VECTOR_`。

**另一条路线——「整块式」L2 包装图**：库内还有一组 `graph_*.hpp`（如 [graph_focusing.hpp:35-63](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L2/include/graph_focusing.hpp#L35-L63) 的 `graph_foc_wrapper`），它不拼 L1 原语，而是把**一整个手写优化、直接调 intrinsic 的 L1 内核** `kfun_foc` 包成单 kernel 的 ADF 图。其实现见 [kernel_focusing.cpp:97-130](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/kernel_focusing/kernel_focusing.cpp#L97-L130)，里面用 `aie::mul_square` + `aie::sqrt` 在单个核内一口气算完范数。**两条路线算的是同一个数学**（范数），区别是「多个小原语流水」vs「单个大内核」——前者易读易组合、后者更省核数。变迹、延时、插值同样各有一对：`kernel_apodization.hpp`/`kernel_delay.hpp`/`kernel_interpolation.hpp` 声明整块内核（如 [kernel_apodization.hpp:71-75](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/kernel_apodization.hpp#L71-L75) 的 `kfun_apodization_pre`、[kernel_delay.hpp:72-78](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/kernel_delay.hpp#L72-L78) 的 `kfun_UpdatingDelay_line_wrapper`），而 `apodization.hpp`/`delay*.hpp`/`samples.hpp` 则是拼原语的功能图。

#### 4.2.4 代码实践

**目标**：把 `focusing_graph` 的连接关系读成一张 DAG，并验证它就是欧几里得范数。

**步骤**：

1. 打开 [focusing.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L2/include/focusing.hpp)。
2. 从 4 个输入端口（`apo_ref_0`/`xdc_def_0`/`apo_ref_1`/`xdc_def_1`）出发，沿 `adf::connect<>` 一直追到输出端口 `focusing_output`。
3. 在纸上画出 4.2.2 那张图，标出每条边上跑的是哪个 kernel。

**需要观察的现象**：两路（X 轴与 Z 轴）是完全对称的子链，最后在 `sumVV` 处汇合。

**预期结果**：你画出的图与 4.2.2 完全一致；两路 `diffSV→squareV` 在 `sumVV` 汇合后开方，即 \(\sqrt{\Delta x^2+\Delta z^2}\)。这是「待本地验证」的阅读结论——只要连接读对了，结果必然如此。

#### 4.2.5 小练习与答案

**练习 1**：`focusing_graph` 把范数手工展开成四段，为什么不直接用现成的 `norm_axis_1`？
**答案**：可读性、可控性与流水的取舍。手工展开能让 X、Z 两轴各自在独立核上并行算平方、再汇合相加，编译器更容易把每段都跑到 `runtime<ratio>` 上限；而且聚焦的输入是两个分开的「参考点」和「阵元位置」向量，需要先 `diffSV` 相减，`norm_axis_1` 的单矩阵入口并不直接匹配。

**练习 2**：`delay_pw_graph` 里的 `tileVApo` 起什么作用？
**答案**：把标量/短向量「铺」成与点矩阵同形的矩阵，好让它和 `diffMV` 的输出做逐元素 `mulMM`（即逐元素平方）。它是 NumPy 里 `np.tile` 的对应物。

**练习 3**：`graph_foc_wrapper`（整块式）和 `focusing_graph`（拼原语式）哪个更省 AIE 核？
**答案**：`graph_foc_wrapper` 更省——它只创建 1 个 kernel（[graph_focusing.hpp:49-50](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L2/include/graph_focusing.hpp#L49-L50)）；而 `focusing_graph` 创建了 6 个（[focusing.hpp:41-46](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L2/include/focusing.hpp#L41-L46)）。代价是整块式可读性差、复用性低。

---

### 4.3 层层组合的机制：ADF 图怎么把积木拼成机器

#### 4.3.1 概念说明

「组合」不是抽象口号，它在代码里有具体载体——**ADF graph**。一个 `adf::graph` 类的构造函数里做三件事就能定义一段数据流：(1) `kernel::create(...)` 实例化内核，(2) `connect<>(src, dst)` 连边，(3) `dimensions(...)`/`runtime<ratio>` 配置。本库的精妙之处在于**同一套机制在两层都被复用**：

- **L1 → L2**：L2 功能图的「内核」就是 L1 原语（`us::L1::diffSV` 等）。
- **L2 → L3**：L3 大图的「子模块」就是 L2 功能图（`us::L2::focusing_graph` 等），一个 L2 图作为整体被 `connect` 进更大的图里。

这就是「层层组合」的字面含义：**图里套图**。L3 不再关心单个原语，它只看到 6 个 L2 黑盒，把它们的端口对接即可。

#### 4.3.2 核心流程

L3 的 PW Beamformer 用一张大图把 6 个 L2 功能图串起来（接收端是 Delay-and-Sum 的完整骨架）：

```text
┌─ imagePoints_graph ───▶ (Image Points 坐标) ──┐
│                                                │（喂给下游各单元）
├─ delay_pw_graph  ────▶ 发射延时 delay          │
├─ focusing_graph  ────▶ 变迹距离 focusing_out   │
├─ samples_graph   ────▶ 有效采样位置 samples    │   PL 侧 mm2s 灌 RF 数据
├─ apodization_graph ──▶ Hanning 权重 apodization│
└─ bSpline_graph   ────▶ 插值结果 C ─────────────┘   PL 侧 s2mm 收结果
```

每张 L2 子图各自内部又是 4.2 那种「若干 L1 原语连成的流水」。于是展开后，整张 PW 图里有几十个 L1 原语 kernel 在 AIE 阵列上同时流水——这正是把算法拆碎再组合的回报。

#### 4.3.3 源码精读

L3 大图的成员声明在 [ultrasound/L3/tests/plane_wave/aie_graph/graph.cpp:67-72](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/tests/plane_wave/aie_graph/graph.cpp#L67-L72)——一行一个 L2 图实例（`img`/`d`/`foc`/`sam`/`apo`/`interp`）。构造函数里把它们与 PLIO（PL↔AIE 边界的流端口）连起来，例如 Image Points 段 [graph.cpp:81-84](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/tests/plane_wave/aie_graph/graph.cpp#L81-L84)：`adf::connect<>(start_positions.out[0], img.start_positions)` 即「把 PL 输入流接到 L2 图 `img` 的端口」。这正是「图里套图」的连接点——L3 只对接 L2 图的端口，不触碰其内部原语。

边界由两类对象构成：`adf::input_plio`/`output_plio` 是 PL↔AIE 之间的 32 位流端口，文件名（如 `"data/start_positions.txt"`）既是仿真输入也是命名。AIE 阵列只认无地址的 AXI Stream，所以 PL 侧的 `mm2s`/`s2mm`（见 u5-l2）把 DDR 里的张量搬成流喂进来——这就是为什么 [graph.cpp:67-72](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/tests/plane_wave/aie_graph/graph.cpp#L67-L72) 里的数据全以 `.txt` 文件形式从 PLIO 进出。

主机端则用原生 XRT 的 AIE 专有能力驱动这张图：[ultrasound/L3/include/plane_wave.hpp:268-276](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/include/plane_wave.hpp#L268-L276) 的 `pw.init(); pw.run(ITER); pw.end();` 就是图的「装弹—发射—收工」三段生命周期（详见 u13-1/13-2 的 `xrt::graph`）。注意上板后由主机 `xrt::graph` 控制；而在 x86/aie 仿真下，则由 [graph.cpp:195-199](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/tests/plane_wave/aie_graph/graph.cpp#L195-L199) 里 `#if defined(__AIESIM__)||defined(__X86SIM__)` 保护的 `main()` 驱动同一张图——两套驱动共享同一个图定义。

#### 4.3.4 代码实践

**目标**：画出本讲最核心的「L1 原语 → L2 功能图 → L3 Beamformer」三层依赖图（这也是本讲的总实践任务）。

**步骤**：

1. 在 [kernels.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/kernels.hpp) 里挑出 `focusing_graph`/`delay_pw_graph` 用到的 L1 原语名（答案可在 4.2.3 找到）。
2. 分别列出 `focusing_graph` 与 `delay_pw_graph` 各自的「原语序列」。
3. 在 [graph.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/tests/plane_wave/aie_graph/graph.cpp) 第 67–72 行找出 PW 大图由哪 6 个 L2 图组成。
4. 画一张三层树图：根是 `plane_wave`，中间层是 6 个 L2 图，叶子是 L1 原语。

**需要观察的现象**：L3 层完全看不见 `mulMM`/`sumVV` 这些名字；L2 层看不见 `plane_wave`；每层只对接下一层的「端口」。

**预期结果**：得到一棵「1 个 L3 ← 6 个 L2 ←（若干）L1 原语」的依赖树。`focusing_graph` 的叶子是 `diffSV×2 / squareV×2 / sumVV / sqrtV`；`delay_pw_graph` 的叶子是 `tileVApo / diffMV / mulMM / sum_axis_1 / divVSSpeedOfSound / diffVSStreamOut`。这是阅读型实践，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：为什么 L3 大图里看不到任何 `aie::mul` 这种 intrinsic？
**答案**：分层隔离。intrinsic 全藏在 L1 原语的 `.cpp` 里；L2 只看原语端口，L3 只看 L2 图端口。换底层 SIMD 宽度（AIE→AIE-ML）只动 L1，上层无感。

**练习 2**：把 `focusing_graph` 从 L3 PW 里摘掉，系统会缺什么能力？
**答案**：缺「动态变迹距离」——`apodization_graph` 需要 `apo_distance_k`（每个阵元到参考点的距离）才能算 Hanning 窗权重，而 `focusing_graph` 正是产出 `apo_distance_k` 的单元（见 L3 README 的 Apodization 输入列表）。

**练习 3**：[graph.cpp:67-72](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/tests/plane_wave/aie_graph/graph.cpp#L67-L72) 的 L2 图对象为何用模板默认参数 `<>` 实例化？
**答案**：因为 `focusing_graph`/`delay_pw_graph` 等都把 `T`、`LENGTH_`、`SIMD_DEPTH_` 等设了默认值（见 [focusing.hpp:25-29](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L2/include/focusing.hpp#L25-L29)），而那些默认值又回指 [kernels.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/kernels.hpp) 里的全局宏（`LENGTH`/`SIMD_DEPTH`/`INCREMENT_VECTOR`）。所以 `<>` 不是「不配置」，而是「沿用库级默认配置」。

---

### 4.4 L3 Beamformer 的三种模式：PW / SA / ScanLine

#### 4.4.1 概念说明

L3 是「完整 Beamformer」。L3 README [ultrasound/L3/README.md](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/README.md) 把它定义成「由 L2 单元拼装的完整波束合成器」，并给出三种模式。三者的差别，**只在发射端（TX）怎么处理**：

| 模式 | 全名 | 发射模型 | 用的 Delay 图 | 额外组件 |
|------|------|----------|----------------|----------|
| **PW** | Plane Wave 平面波 | 一次发一个平面波，照亮整片 | `delay_pw`（简化发射延时） | 最精简，6 个 L2 单元 |
| **SA** | Synthetic Aperture 合成孔径 | 逐阵元当虚拟源发射 | `delay`（完整发射延时） | 多一组发射侧 `focusing_sa`+`apodization_sa` |
| **ScanLine** | 扫描线 | 逐线聚焦发射 | `delay`（完整发射延时） | 接收延时计算与 PW 不同 |

一句话记忆：**PW 用 `delay_pw`，SA 与 ScanLine 用 `delay`；SA 比别人多一套「发射侧变迹」**。这也是为什么 [L3/README.md](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/README.md) 里 SA 的输入输出列表里会多出 `focusing_output_tx` 和 `apodization_tx`——那是 SA 独有的发射侧动态变迹。

#### 4.4.2 核心流程

无论哪种模式，L3 Beamformer 的接收端都是 Delay-and-Sum 的同一条骨架：

```text
1. Image Points   : 生成要成像的点坐标矩阵
2. Delay          : 算发射延时（PW 用简化版，SA/ScanLine 用完整版）
3. Focusing       : 算每个阵元到变迹参考点的距离
4. Samples        : 算接收延时 + 发射延时 → 在 RF 数据里的采样位置
5. Apodization    : 算每个阵元的 Hanning 权重（SA 额外算发射侧权重）
6. bSpline 插值   : 在 RF 数据里按分数位置插值取值
   └─ 加权叠加 → 一条成像线
```

三种模式的「组合」差别就落在第 2 步选哪个 Delay 图、以及 SA 是否多挂一组发射侧 Focusing/Apodization。这是「同一盒 L2 积木，搭出三种成像方法」的典型体现。

#### 4.4.3 源码精读

**PW 模式**：4.3 已展开。大图 `plane_wave` 在 [graph.cpp:28-159](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/tests/plane_wave/aie_graph/graph.cpp#L28-L159)，含 `delay_pw_graph`（[graph.cpp:68](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/tests/plane_wave/aie_graph/graph.cpp#L68)）；主机驱动在 [plane_wave.hpp:87-92](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/include/plane_wave.hpp#L87-L92) 的 `plane_wave(...)` 函数，它把 24 路 PL 输入（`mm2s1..24`）灌进图、把 6 路 PL 输出（`s2mm1..6`）收回（[plane_wave.hpp:227-262](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/include/plane_wave.hpp#L227-L262)）。

**SA / ScanLine 模式**：L3 头文件 [scanline.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/include/scanline.hpp) 与 [synthetic_aperture.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/include/synthetic_aperture.hpp) 与 PW 同构（同样 `xrtDeviceOpen`→`load_xclbin`→`adf::registerXRT`→驱动 mm2s/s2mm/图），区别在底层挂的 Delay 图换成 `delay`（而非 `delay_pw`），SA 还多挂 `focusing_sa`/`apodization_sa`。三种模式的完整输入输出清单见 [L3/README.md](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/README.md)。

**配套参考模型**：L3 还带一套纯 C++ 的「黄金参考」实现 `L3/models/include/`（如 `us_op_focus.hpp`/`us_op_delay.hpp`/`us_op_apodization.hpp`），逐算子对标硬件结果，是判断 PASS/FAIL 的参照（与 u14-3 的对标思路一致）。

#### 4.4.4 代码实践

**目标**：跑通一个 L3 Beamformer 用例，亲眼看 AIE 图生命周期与四档仿真。

**步骤**（来自 [ultrasound/L3/tests/README.md:22-26](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/tests/README.md#L22-L26)）：

```bash
cd ultrasound/L3/tests/plane_wave   # 或 scanline
make help                           # 查看可用目标
make run TARGET=x86sim              # 最快：x86 功能仿真
make run TARGET=aiesim              # 周期精确的 AIE 仿真
```

前置环境（[L3/tests/README.md:4-15](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/tests/README.md#L4-L15)）：需 `source Vitis/2024.2/settings64.sh`、`source xrt/setup.sh`，并设好 `PLATFORM`（`xilinx_vck190_base_202420_1`）与嵌入式三件套 `SYSROOT`/`ROOTFS`/`K_IMAGE`。

**需要观察的现象**：x86sim 会从 `data/*.txt` 灌入 24 路输入、写出 6 路输出文件；`pw.init()/run(1)/end()` 三行日志会打印（[plane_wave.hpp:268-276](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/include/plane_wave.hpp#L268-L276)）。

**预期结果**：`make run TARGET=x86sim` 正常退出并生成输出文件。若本地无 Versal 工具链，**待本地验证**；可退化为「源码阅读型实践」——只读 `graph.cpp` 的 `main()`（[graph.cpp:168-203](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/tests/plane_wave/aie_graph/graph.cpp#L168-L203)），说明仿真下图的 `init/run/end` 由谁调用。

#### 4.4.5 小练习与答案

**练习 1**：SA 模式为什么比 PW 多一组发射侧 Apodization？
**答案**：SA 的虚拟源在发射时也等效一个「孔径」，需要对其做动态变迹以压低发射旁瓣；PW 是平面波、无明显发射孔径，故不需要。对应代码是 SA 专属的 `apodization_sa`/`focusing_sa` 图。

**练习 2**：README 里 ScanLine 说「与 PW 的差别在接收延时计算」，这与本讲强调的「三种模式差别在发射」矛盾吗？
**答案**：不矛盾。发射模型不同（聚焦线 vs 平面波）会反过来改变接收端的延时参考，所以 ScanLine 在接收延时（Samples/Delay）上也需要不同处理；但 L2 积木是同一盒，只是接线方式不同。「差别在发射」是根因，「接收延时不同」是表象。

**练习 3**：为什么 L3 Beamformer 的输入输出全是文件（`.txt`）？
**答案**：因为 AIE 仿真下数据经 PLIO 进出图，而 PLIO 在仿真里就接文件 I/O（[graph.cpp:76-79](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/tests/plane_wave/aie_graph/graph.cpp#L76-L79)）；上板时这些 PLIO 由 PL 侧 `mm2s`/`s2mm` 喂/收，文件名退化为流的逻辑名。

## 5. 综合实践

把本讲四节串起来，完成一张**完整的三层组合关系图**（本讲核心实践任务）：

1. **L1 叶子层**：从 [kernels.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/kernels.hpp) 列出 PW Beamformer 实际用到的全部 L1 原语（提示：综合 `delay_pw_graph` 与 `focusing_graph` 的成员，再翻 `samples.hpp`/`apodization.hpp`/`imagePoints.hpp`/`bSpline.hpp` 各用了哪些）。
2. **L2 中间层**：给每个 L2 功能图标出它消费的 L1 原语序列（如 `focusing_graph` = `diffSV→squareV→sumVV→sqrtV`）。
3. **L3 根层**：画出 PW 大图 `plane_wave` 如何把这 6 个 L2 图用 `adf::connect` 连起来（参考 [graph.cpp:81-157](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/tests/plane_wave/aie_graph/graph.cpp#L81-L157)）。
4. **旁注**：在图上单独标出「整块式」路线（`kfun_*` + `graph_*_wrapper`）与「拼原语式」路线（`*_graph`）的并存关系，说明 L3 的 PW 走的是哪一条。

**自检问题**：如果你把 `SPEED_OF_SOUND` 从 1540 改成另一个值（[kernels.hpp:37](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L1/include/kernels.hpp#L37)），你的图里哪条边会受影响？（答：所有用到 `divVSSpeedOfSound` 的 Delay 路径——延时 = 距离/声速。）

> 这一实践纯靠阅读完成，不需工具链；它的产出（一张三层依赖图）就是你掌握本讲的凭证。

## 6. 本讲小结

- `ultrasound` 把三层抽象贯彻到底：**L1 = NumPy 风格向量原语**（积木）、**L2 = 用 L1 原语拼出的波束合成功能图**（如聚焦=范数、PW 延时=距离÷声速）、**L3 = 用 L2 图缝成的完整 Beamformer**。
- L1 原语是高度规整的模板（`T/LEN/INCREMENT/VECDIM`），每个原语≈一个 `for`+一条 `aie::` 向量指令；声速 `SPEED_OF_SOUND=1540` 是贯穿全局的物理常量。
- L2 存在**两条并存路线**：「拼原语式」功能图（`focusing_graph`/`delay_pw_graph`，可读、可组合）与「整块式」包装图（`graph_foc_wrapper` 等，省核、直接调 intrinsic），二者数学等价。
- 「层层组合」的载体是 ADF graph：**图里套图**——L3 只见 L2 图端口、L2 只见 L1 原语端口，层间靠 `connect` 解耦，换底层 SIMD 不影响上层。
- L3 三种模式 PW/SA/ScanLine 复用同一盒 L2 积木，差别集中在发射端：PW 用 `delay_pw`，SA/ScanLine 用 `delay`，SA 额外挂发射侧 `focusing_sa`+`apodization_sa`。
- 跑 L3 用例用 `make run TARGET=x86sim/aiesim`（[L3/tests/README.md](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/ultrasound/L3/tests/README.md)），图的 `init/run/end` 在仿真下由 `main()` 驱动、上板下由主机 `xrt::graph` 驱动。

## 7. 下一步学习建议

- **横向对比 BLAS**（u8-l1）：`ultrasound` 的 L1 原语是「逐元素/规约」级，而 BLAS 的 L1 模块是「GEMM 脉动阵列」级，体会两种「L1」抽象粒度的差异。
- **纵向深入 AIE**（u13-1、u13-2）：本讲的 `adf::graph`/`connect`/PLIO 只是 ADF 编程模型的入口；下一站应系统学习 ADF 图的 window/stream、`xrt::graph` 主机控制与 SD 卡打包。
- **数据搬运**（u5-2）：本讲里 RF 数据从 DDR 经 `mm2s` 喂进 PLIO、结果经 `s2mm` 收回，这条 PL↔AIE 边界的细节值得单独学。
- **继续阅读**：把 `L3/models/include/us_op_*.hpp` 这套参考模型通读一遍，它是理解每个 L2 单元「应该算出什么」的最佳对照。
