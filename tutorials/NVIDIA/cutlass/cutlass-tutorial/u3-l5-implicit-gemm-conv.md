# 隐式 GEMM 卷积

## 1. 本讲目标

本讲解决一个核心问题：**CUTLASS 是一个「矩阵乘法（GEMM）库」，那它凭什么也能高效地做卷积（Conv）？**

答案是「**隐式 GEMM（Implicit GEMM）**」——把一次卷积在数学上等价改写成一次矩阵乘法，从而**复用整套 CUTLASS 为 GEMM 打造的高性能流水线**（TMA 搬运、wgmma/UMMA 指令、warp-specialized 主循环、tile scheduler）。

学完本讲，你应当能够：

- 用「窗口加权求和 = 向量内积」一句话解释隐式 GEMM 的直觉，并写出 Fprop 的 A/B/C 映射表。
- 看懂 `Conv2dProblemSize`（2.x）与 `ConvProblemShape`（3.x）两套问题描述，以及二者如何把 NHWC/KRSC/NPQK 翻译成 GEMM 的 `(M,N,K)`。
- 说出「im2col」到底把哪一维展开了，以及 CUTLASS 3.x 如何把它折叠进 TMA 描述符、不再物化中间矩阵。
- 理解 `CollectiveConv` 与 GEMM 的 `CollectiveMma` 是同构的，卷积只是「换了数据搬运方式」的 GEMM。

本讲承接 [u2-l7 CUTLASS 3.x GEMM 通用模型](u2-l7-gemm-3x-universal-model.md)，把那里的 `kernel + collective + epilogue` 三段式套用到卷积上。

## 2. 前置知识

阅读本讲前，你需要先建立以下认知（来自前置讲义）：

- **GEMM 基本公式**：\(C = \alpha(A\cdot B) + \beta C\)，其中 A 是 M×K，B 是 K×N，C 是 M×N。这是 CUTLASS 的「母语」。
- **CuTe 的 Layout/Tensor 抽象**（u2-l1、u2-l2）：一个张量 = 数据指针 + 把坐标映射到下标的纯函数。理解这一点，你才能理解「im2col 改的是坐标映射函数，而不是真的去搬一份新数据」。
- **CUTLASS 3.x 的三段式**（u2-l7）：`kernel::GemmUniversal` 外壳 + `CollectiveMainloop`（搬 A/B 并做 MMA）+ `CollectiveEpilogue`（写回 D），靠 `dispatch_policy` 里的标签分派。
- **TMA 与 warp specialization**（u3-l1、u3-l2）：producer warp 发 TMA 把数据搬到共享内存，consumer warp group 发 wgmma 做乘加，靠异步流水线重叠。本讲你会看到这套机制原封不动地用在卷积上。

如果你对卷积本身的定义还生疏，只需记住一句话：**卷积是把一个小小的「滤波器（filter/weight）」在输入图像上滑动，每个位置做一次逐元素相乘再求和，得到一个输出值**。本讲要把这个「滑动求和」改写成「矩阵乘」。

## 3. 本讲源码地图

本讲涉及的关键文件（注意：规格里写的 `sm90_conv_impl.hpp` 在当前 HEAD **并不存在**，3.x 卷积 collective 的真实文件名是 `sm90_implicit_gemm_gmma_ss_warpspecialized.hpp`，本讲按真实文件讲解）：

| 文件 | 作用 |
| --- | --- |
| [include/cutlass/conv/convolution.h](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/conv/convolution.h) | 卷积层的「术语字典」：定义 `Operator`(Fprop/Dgrad/Wgrad/Deconv)、`Mode`、`IteratorAlgorithm` 等枚举，并在文件头给出 A/B/C ↔ Activation/Filter/Output 的映射表。 |
| [include/cutlass/conv/conv2d_problem_size.h](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/conv/conv2d_problem_size.h) | 2.x 风格的 `Conv2dProblemSize` 结构体，以及把 Conv2d 翻译成隐式 GEMM 的一组自由函数（`implicit_gemm_problem_size` 等）。 |
| [include/cutlass/conv/convnd_problem_shape.hpp](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/conv/convnd_problem_shape.hpp) | 3.x 风格的、与空间维数无关（rank-agnostic）的 `ConvProblemShape`，统一描述 2D/3D 卷积问题，内部推导 A/B/C 的形状与步长。 |
| [include/cutlass/conv/dispatch_policy.hpp](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/conv/dispatch_policy.hpp) | 卷积的 mainloop 策略标签（如 `MainloopSm90TmaGmmaWarpSpecializedImplicitGemm`），注意它**继承自 GEMM 的策略**。 |
| [include/cutlass/conv/collective/collective_conv.hpp](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/conv/collective/collective_conv.hpp) | `CollectiveConv` 主模板（恒报错的骨架）+ 按架构 `#include` 各特化文件，结构与 GEMM 的 `collective_mma.hpp` 完全对称。 |
| [include/cutlass/conv/collective/sm90_implicit_gemm_gmma_ss_warpspecialized.hpp](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/conv/collective/sm90_implicit_gemm_gmma_ss_warpspecialized.hpp) | SM90（Hopper）卷积 collective 真正的实现：复用 `PipelineTmaAsync` 与 wgmma，唯一区别是 A 用 `SM90_TMA_LOAD_IM2COL` 搬运。 |
| [include/cute/atom/copy_traits_sm90_im2col.hpp](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/copy_traits_sm90_im2col.hpp) | im2col 版 TMA 拷贝的 traits：把卷积坐标 `(c,[w,h,d],n,[s,r,t])` 折叠进 TMA 描述符，由硬件完成窗口采集。 |
| [examples/16_ampere_tensorop_conv2dfprop/ampere_tensorop_conv2dfprop.cu](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/16_ampere_tensorop_conv2dfprop/ampere_tensorop_conv2dfprop.cu) | 一个可运行的 2.x 风格 Conv2d Fprop 示例，适合作为实践蓝本。 |

## 4. 核心概念与源码讲解

### 4.1 隐式 GEMM 概念：把卷积翻译成矩阵乘

#### 4.1.1 概念说明

先看一段最朴素的卷积前向（Fprop）数学定义。设输入激活为 \(A\)（NHWC）、权重为 \(B\)（KRSC）、输出为 \(C\)（NPQK），则输出中一个元素为：

\[
C[n,p,q,k] \;=\; \sum_{r=0}^{R-1}\sum_{s=0}^{S-1}\sum_{c=0}^{C-1} A[n,\;p\cdot u_h - pad_h + r\cdot d_h,\; q\cdot u_w - pad_w + s\cdot d_w,\; c]\cdot B[k,r,s,c]
\]

其中 \(u\) 是步长（stride）、\(d\) 是膨胀（dilation）、\(pad\) 是补零。关键观察：**等号右边就是一个向量内积**——把 \(A\) 在窗口内的 \(R\cdot S\cdot C\) 个元素拉成一维向量 \(\tilde{a}\)，把对应权重 \(B[k,\cdot]\) 也拉成一维向量 \(\tilde{b}_k\)，那么 \(C[n,p,q,k] = \tilde{a}\cdot \tilde{b}_k\)。

于是整张输出可以写成矩阵乘：

\[
C_{(NPQ)\times K} \;=\; A_{\text{patch}}{}_{(NPQ)\times(RSC)} \cdot B_{\text{mat}}{}_{(RSC)\times K}
\]

这就是「**隐式 GEMM**」：把卷积看成一次 \((M,N,K)=(NPQ,\;K,\;RSC)\) 的矩阵乘。CUTLASS 的源码头注释把这张映射表写得清清楚楚——三个算子（Fprop 前向 / Dgrad 输入梯度 / Wgrad 权重梯度）各自把卷积张量映射到 GEMM 的 A、B、C：

```
___________________________________________________________________________
 ConvolutionalOperator |        A        |      B         |       C
___________________________________________________________________________
|       Fprop          |    Activation   |    Filter      |     Output    |
|       Dgrad          |     Output      |    Filter      |   Activation  |
|       Wgrad          |     Output      |  Activation    |     Filter    |
```

> 见 [include/cutlass/conv/convolution.h:54-61](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/conv/convolution.h#L54-L61)（这张表是整个卷积代码库的「罗塞塔石碑」，翻译一切 A/B/C 与 Activation/Filter/Output 的对应关系）。

**为什么叫「隐式」？** 因为经典的 im2col 做法（见 4.3）会**真的在显存里建一份** \((NPQ)\times(RSC)\) 的展开矩阵 \(A_{\text{patch}}\)，极其浪费显存；而「隐式」GEMM**只在数学上做这次展开，物理上不建中间矩阵**——展开所需的坐标重映射被折叠进数据搬运（2.x 的 tile 迭代器，或 3.x 的 TMA 描述符），边搬边展开。这是 CUTLASS 高效做卷积的核心技巧。

#### 4.1.2 核心流程

把一次 Fprop 卷积「翻译」成隐式 GEMM 的步骤：

1. **确定三个张量的 GEMM 角色**：Fprop 时，A=Activation(NHWC)、B=Filter(KRSC)、C=Output(NPQK)。
2. **把输出展平成 M 维**：\(M = N\cdot P\cdot Q\)，即每个输出像素对应矩阵的一行。
3. **把权重的输出通道作为 N 维**：\(N = K\)（注意：这里的 `K` 是滤波器**输出通道数**，与 GEMM 的归约维同名但含义不同，这是初学者最易混淆的点）。
4. **把「窗口 × 输入通道」展平成归约维**：\(K_{\text{gemm}} = R\cdot S\cdot C\)。
5. **套用 CUTLASS 的 GEMM 内核**：剩下的事（tile 切分、TMA 搬运、wgmma、epilogue）与普通 GEMM 完全相同。

用伪代码表达「一个输出元素 = 一次内积」：

```
for n in N, p in P, q in Q:        # 这些拼成 GEMM 的 M 维 (行)
  for k in K_filter:               # GEMM 的 N 维 (列)
    acc = 0
    for r in R, s in S, c in C:    # GEMM 的 K 归约维
        a = Activation[n, p*u - pad + r*d, q*u - pad + s*d, c]  # im2col 采集
        b = Filter[k, r, s, c]
        acc += a * b
    Output[n,p,q,k] = acc
```

把外两层循环看作「遍历输出矩阵的每个元素」，最内三层看作「沿归约维累加」，正是标准 GEMM 的三重循环。

#### 4.1.3 源码精读

`Operator` 枚举定义了卷积的三种（加 Deconv 共四种）计算方向：

```cpp
/// Convolutional operator
enum class Operator {
  kFprop,     // 前向：Activation * Filter -> Output
  kDgrad,     // 输入梯度：Output   * Filter -> Activation
  kWgrad,     // 权重梯度：Output   * Activation -> Filter
  kDeconv
};
```

> [include/cutlass/conv/convolution.h:88-93](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/conv/convolution.h#L88-L93)：三种算子对应训练时的前向与两个反向。本讲后续如无特别说明，**默认讨论 Fprop**。

把 Conv2d 翻译成隐式 GEMM 问题尺寸的核心函数是 `implicit_gemm_problem_size`，它的 Fprop 分支正是把上面的 \(M,N,K\) 公式直接写成代码：

```cpp
switch (conv_operator) {
case Operator::kFprop:
  return gemm::GemmCoord(
    problem_size.N * problem_size.P * problem_size.Q,   // M = NPQ
    problem_size.K,                                      // N = K (输出通道)
    problem_size.R * problem_size.S * problem_size.C / problem_size.groups  // K = RSC
  );
...
```

> [include/cutlass/conv/conv2d_problem_size.h:327-356](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/conv/conv2d_problem_size.h#L327-L356)：`implicit_gemm_problem_size`。同一个函数里 Dgrad/Wgrad 分支只是把 A/B/C 的角色换一下，对应 4.1.1 那张映射表的另外两行。

#### 4.1.4 代码实践

**实践目标**：亲手把一个 Fprop 卷积问题翻译成隐式 GEMM，验证你的手算与 CUTLASS 源码公式一致。

**操作步骤**：

1. 取一个具体问题：\(N=1, H=8, W=8, C=4, K=16, R=3, S=3\)，stride=1，对称 padding=1，dilation=1。
2. 先算输出空间尺寸 \(P,Q\)（公式见 4.2）。
3. 再套用 4.1.3 的公式算 \((M,N,K_{\text{gemm}})\)。
4. 打开 [include/cutlass/conv/conv2d_problem_size.h:332-338](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/conv/conv2d_problem_size.h#L332-L338)，确认你的 \(M,N,K\) 与代码逐项对应。

**需要观察的现象**：你会发现 GEMM 的归约维 \(K_{\text{gemm}}=R\cdot S\cdot C=36\)，**远大于**滤波器输出通道 \(K=16\)——这正是 im2col 把空间窗口「拉平」进归约维的结果（见 4.3）。

**预期结果**：\(P=Q=8\)，故 \(M=1\cdot8\cdot8=64\)、\(N=16\)、\(K_{\text{gemm}}=3\cdot3\cdot4=36\)，等价 GEMM 为 \((64,16,36)\)。

#### 4.1.5 小练习与答案

**练习 1**：Dgrad（输入梯度）时，GEMM 的 A/B/C 分别对应卷积里的哪三个张量？为什么 A 变成了 Output？
**答案**：Dgrad 的 A=Output（即前向的输出梯度，形状 NPQK）、B=Filter、C=Activation。因为反向求输入梯度时，是「输出梯度 × 权重」还原回输入空间的梯度，所以前向的「输出」在这里成了被乘的 A 矩阵，对应 [convolution.h:59](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/conv/convolution.h#L59) 的 Dgrad 行。

**练习 2**：为什么说卷积的 GEMM 归约维 \(K_{\text{gemm}}=RSC\) 通常比矩阵的 M、N 还关键？
**答案**：因为 MMA 指令沿 K 维做大量乘加，\(K_{\text{gemm}}\) 越长，单次 kernel 启动能摊销的开销越多、算力利用率越高；而 \(RSC\) 正是被 im2col 拉平的空间窗口维，体现了「把卷积变成一个 K 维很长的 GEMM」的价值。

### 4.2 NHWC 布局与 problem shape

#### 4.2.1 概念说明

要做隐式 GEMM，第一步是**精确描述卷积问题的全部尺寸**。CUTLASS 采用 **NHWC** 内存布局（而非科学计算常见的 NCHW）：

- **激活 Activation**：`NHWC` —— N(批)·H(高)·W(宽)·C(通道)，通道在最内层（连续），这与 Tensor Core 按通道连续读取的需求一致。
- **权重 Filter**：`KRSC` —— K(输出通道)·R(高)·S(宽)·C(输入通道)。
- **输出 Output**：`NPQK` —— N(批)·P(输出高)·Q(输出宽)·K(输出通道)。

除尺寸外，卷积还比 GEMM 多出几个**几何参数**：padding（补零，可上下非对称）、stride（滑动步长）、dilation（膨胀）、mode（卷积还是互相关）、以及实现相关的 split_k_slices（沿 K 拆分）和 groups（分组卷积）。

CUTLASS 提供两套问题描述结构：

- **2.x：`Conv2dProblemSize`** —— 一个简单的 POD 结构体，把上述参数全部列为 `int` 成员，并提供一组**自由函数**（`implicit_gemm_problem_size` 等）把问题翻译成 GEMM。
- **3.x：`ConvProblemShape`** —— 一个与空间维数无关（rank-agnostic）的结构体，统一描述 2D/3D，把 A/B/C 的形状与步长作为成员**内部推导**出来，便于和 CuTe 的 `Layout` 直接对接。

#### 4.2.2 核心流程

输出空间尺寸 \(P,Q\) 的计算公式（以 H 方向为例）：

\[
P \;=\; \left\lfloor \frac{H + pad_h^{\text{lower}} + pad_h^{\text{upper}} - (R-1)\cdot d_h - 1}{u_h} \right\rfloor + 1
\]

这是卷积的「经典几何」：输入尺寸 + 总 padding，减去膨胀后滤波器实际覆盖范围 \((R-1)d_h+1\)，再按步长向下取整，加 1 得到能滑动的位置数。

`ConvProblemShape` 内部用一个 lambda 计算每个空间维的输出尺寸：

```cpp
auto nzpqk_extent = [](int act_ext, int filter_ext, int pad_total, int dilation, int tstride) {
  return 1 + (act_ext + pad_total - ((filter_ext -1) * dilation + 1)) / tstride;
};
```

> [include/cutlass/conv/convnd_problem_shape.hpp:558-559](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/conv/convnd_problem_shape.hpp#L558-L559)：与上面公式完全一致（`pad_total = lower + upper`）。

推导出 xformed activation（即 NZPQK 形状的「变换后激活」）后，`ConvProblemShape` 按 Fprop/Dgrad/Wgrad 三种算子，把激活、滤波器、输出分配给 GEMM 的 A/B/C：

```cpp
// |              | Fprop  | Dgrad  | Wgrad |
// | ------       | ------ | ------ | ------|
// |   ShapeA     | NDHWC  | NZPQK  | NZPQK |
// |   ShapeB     | KTRSC  | KTRSC  | NDHWC |
// |   ShapeC     | NZPQK  | NDHWC  | KTRSC |
if constexpr (ConvOp == cutlass::conv::Operator::kFprop) {
  shape_A = shape_act;     shape_B = shape_flt;     shape_C = shape_xformed_act;
} ...
```

> [include/cutlass/conv/convnd_problem_shape.hpp:315-365](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/conv/convnd_problem_shape.hpp#L315-L365)：`set_shape_stride_ABC` 把卷积张量分配到 A/B/C，注释里的表与 4.1.1 的映射表一致（注意 3.x 用 NDHWC/NZPQK，比 2.x 多一个空间维 D）。

#### 4.2.3 源码精读

`Conv2dProblemSize` 是 2.x 的核心描述，成员一览：

```cpp
struct Conv2dProblemSize {
  // Conv2d strictly problem size parameters
  int N, H, W, C, P, Q, K, R, S;
  int pad_h, pad_w;
  int stride_h, stride_w;
  int dilation_h, dilation_w;
  Mode mode;
  // Conv2d implementation-related parameters
  int split_k_slices;
  int groups;
```

> [include/cutlass/conv/conv2d_problem_size.h:64-75](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/conv/conv2d_problem_size.h#L64-L75)：注意它把「严格几何参数」（N…dilation）与「实现相关参数」（split_k_slices、groups）分开。

它的构造函数之一会按公式自动算出 P、Q（当用户只给了输入/滤波器/padding/stride/dilation 时）：

```cpp
P = ((H + pad_h + padding[1] - R * dilation_h) / stride_h) + 1;
Q = ((W + pad_w + padding[3] - S * dilation_w) / stride_w) + 1;
```

> [include/cutlass/conv/conv2d_problem_size.h:174-177](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/conv/conv2d_problem_size.h#L174-L177)：`padding[1]`、`padding[3]` 分别是上 padding，所以这是支持**非对称 padding** 的版本。

3.x 的 `ConvProblemShape` 进一步把 A/B/C 的**逻辑 GEMM 形状**（`get_shape_A/B/C`）整理成 CuTe 风格的嵌套 `make_shape`，方便 collective 直接拼 Layout。以 `get_shape_A` 为例：

```cpp
// fprop: A extents array [N,D,H,W,C] -> ((W,H,D,N), (C))
if constexpr (ConvOp == conv::Operator::kFprop || ConvOp == conv::Operator::kDgrad) {
  return make_shape(
    cute::reverse(take<0, RankT - 1>(shape_A)),   // 空间+batch 维拼成 M
    shape_A[RankT - 1]);                           // 通道维 C 留作 K 的一部分
}
```

> [include/cutlass/conv/convnd_problem_shape.hpp:398-420](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/conv/convnd_problem_shape.hpp#L398-L420)：把 NHWC 的「外层 N,H,W」翻转后拼成 GEMM 的 M（即 NPQ），通道 C 单独留给归约维——这正是 4.1 那张映射表的代码化身。

#### 4.2.4 代码实践

**实践目标**：用 CUTLASS 自带的转换函数，在主机端把一个 `Conv2dProblemSize` 翻译成等价 GEMM 的 A/B/C 形状，并打印。

**操作步骤**（源码阅读型 + 可选运行）：

1. 阅读 [conv2d_problem_size.h:522-564](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/conv/conv2d_problem_size.h#L522-L564) 的三个函数 `implicit_gemm_tensor_{a,b,c}_extent`，确认 Fprop 时它们分别返回 activation/filter/output 的 extent。
2. （可选）写一段示例代码（**标注为示例代码，非项目原有**）：

   ```cpp
   // 示例代码
   using namespace cutlass::conv;
   Conv2dProblemSize ps(1,8,8,4, 16,3,3, 8,8, 1,1,2,2,2, 1,1, Mode::kCrossCorrelation);
   auto g = implicit_gemm_problem_size(Operator::kFprop, ps);
   std::cout << "M=" << g.m() << " N=" << g.n() << " K=" << g.k() << "\n";
   ```
3. 编译运行（需链接 CUTLASS 头文件，目标架构任意）。若无法本地运行，标注「待本地验证」。

**需要观察的现象**：输出的 `M N K` 应为 `64 16 36`（与 4.1.4 手算一致），其中 `implicit_gemm_tensor_a_extent` 返回的 A 形状仍是原始 NHWC `(1,8,8,4)`——再次印证「隐式」：A 物理形状没变，是 GEMM 的坐标映射在做 im2col 展开。

**预期结果**：M=64、N=16、K=36。

#### 4.2.5 小练习与答案

**练习 1**：为什么 CUTLASS 卷积默认用 NHWC 而不是 NCHW？
**答案**：因为 Tensor Core 的 MMA 指令要求参与运算的「通道」维在内存里连续，NHWC 把 C 放在最内层，正好满足向量化加载与 MMA 分块的对齐要求；NCHW 会让通道分散，需要额外转置。

**练习 2**：`Conv2dProblemSize` 里 `P,Q` 是用户给的吗？
**答案**：不一定。部分构造函数（如 [conv2d_problem_size.h:157-177](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/conv/conv2d_problem_size.h#L157-L177)）由用户给输入/滤波器/padding/stride/dilation，库**内部按公式算出 P、Q**；也有构造函数允许用户直接指定输出尺寸。两种入口对应「我想控制输出」与「我只想描述输入」两种用法。

### 4.3 im2col 拷贝：把窗口采集折叠进数据搬运

#### 4.3.1 概念说明

隐式 GEMM 的「隐式」二字，难点全在 4.1 那个 \((NPQ)\times(RSC)\) 的展开矩阵 \(A_{\text{patch}}\) **如何不物化就拿到数据**。这个展开动作业界叫 **im2col**（image to column）：把每个输出像素对应的那 \(R\cdot S\) 个窗口位置的 \(C\) 个通道「采集」出来，排成矩阵的一行。

经典 im2col 的做法是：**在显存里真的申请一块** \((NPQ)\times(RSC)\) 的缓冲区，先把激活按窗口拷进去，再对它跑普通 GEMM。简单粗暴，但缓冲区极大（比如 \(N=64,H=W=64,C=256,R=S=3\) 时，展开矩阵有上亿元素），而且多一次全量搬运。

CUTLASS 的做法是**边搬边展开**，不做全量物化：

- **2.x**：在 threadblock 的 **tile 访问迭代器**（`conv/threadblock/conv2d_fprop_activation_tile_access_iterator_*.h`）里，每搬一个 tile，就按「输出像素 (n,p,q) → 输入地址 (n, p·u−pad+r·d, q·u−pad+s·d, c)」的解析公式现算地址。这套地址计算支持 `Analytic`（处处正确、较慢）和 `Optimized`（针对 R,S≤32 优化）等多种算法。
- **3.x（Hopper）**：因为有了 TMA，CUTLASS 把 im2col 的坐标重映射**编码进 TMA 描述符**，由 TMA 硬件在搬运时自动完成窗口采集——文件 `copy_traits_sm90_im2col.hpp` 就是干这件事的，对应的 copy atom 叫 `SM90_TMA_LOAD_IM2COL`。

#### 4.3.2 核心流程

3.x 的 im2col TMA 把一个被搬运元素的坐标解释成一个四元组：

\[
(c,\;[w,h,d],\;n,\;[s,r,t])
\]

含义是：通道 \(c\)、空间位置 \((w,h,d)\)、batch \(n\)、以及滤波器空间偏移 \((s,r,t)\)。TMA 硬件根据这个坐标 + 描述符里编码的 padding/stride/dilation，**自动算出**真正的全局内存地址，并处理越界（OOB，落到 padding 区就补零）。于是「展开成 \((NPQ)\times(RSC)\) 矩阵」这件事，被分解成「沿 M 维遍历 (n,p,q)」×「沿 K 维遍历 (r,s,c)」，而两者的交叉寻址完全交给 TMA。

关键流程：

1. 主机端 `make_im2col_tma_copy` 用激活张量、smem Layout、CTA tile、以及 padding/stride/dilation，构造一个 im2col 版的 TMA 描述符 + `TiledCopy`。
2. 运行时 producer warp 发 `cp.async.bulk.tensor`（即 `SM90_TMA_LOAD_IM2COL`），坐标由 collective 给出。
3. 硬件完成采集、写共享内存、翻转 mbarrier（与 u3-l1、u3-l2 讲的普通 TMA 流水线一致）。
4. consumer warp group 用 wgmma 直接消费 smem 里的数据。

#### 4.3.3 源码精读

SM90 卷积 collective 在源码里明确标注了「哪个张量走 im2col」：

```cpp
// The tma load mode of wgrad is tiled for tensor A and im2col for tensor B
// while the tma load mode of fprop and dgrad kernel is im2col for tensor A and tiled for tensor B.
```

> [include/cutlass/conv/collective/sm90_implicit_gemm_gmma_ss_warpspecialized.hpp:121-122](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/conv/collective/sm90_implicit_gemm_gmma_ss_warpspecialized.hpp#L121-L122)：一句话点透 im2col 的归属——**Fprop 时需要对激活做窗口采集，所以 A（激活）走 im2col；B（权重）天然是 RSC 连续排布，普通 tiled TMA 即可**。

并用编译期常量与 `static_assert` 强约束搬运原子的类型：

```cpp
static constexpr bool is_im2col_A = detail::is_im2col_load<GmemTiledCopyA>::value;
static constexpr bool is_im2col_B = detail::is_im2col_load<GmemTiledCopyB>::value;
```

> [sm90_implicit_gemm_gmma_ss_warpspecialized.hpp:134-135](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/conv/collective/sm90_implicit_gemm_gmma_ss_warpspecialized.hpp#L134-L135)：A 必须是 `SM90_TMA_LOAD_IM2COL`、B 必须是普通 `SM90_TMA_LOAD`（Fprop 时），填错会编译失败。

构造 im2col TMA 时，要算出 padding 产生的「下角/上角」（决定哪些窗口位置会落到 padding 区、需要补零）：

```cpp
auto lower_corner_whd = detail::compute_lower_corner_whd(problem_shape);
auto upper_corner_whd = detail::compute_upper_corner_whd(problem_shape);
...
return make_im2col_tma_copy(
    GmemTiledCopyA{}, tensor_a, SmemLayoutA{}(_,_,_0{}),
    product_each(shape(SmemLayoutA{}(_,,_0{}))), size<1>(ClusterShape{}),
    shape(lower_corner_whd), shape(upper_corner_whd), /* padding/stride/dilation ... */);
```

> [sm90_implicit_gemm_gmma_ss_warpspecialized.hpp:179-203](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/conv/collective/sm90_implicit_gemm_gmma_ss_warpspecialized.hpp#L179-L203)：把卷积几何（角点、padding、stride、dilation）全塞进 `make_im2col_tma_copy`，让描述符「记住」如何做窗口采集。

最终，硬件发射端的 `copy_unpack` 把坐标解释成那个四元组 `(c,[w,h,d],n,[s,r,t])`：

```cpp
// Interpret the TMA IM2COL coordinate as  (c, ([w,h,d]), n, ([s,r,t]))
CUTE_STATIC_ASSERT_V(rank(src_coord_offset) == _4{});
```

> [include/cute/atom/copy_traits_sm90_im2col.hpp:73-75](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/copy_traits_sm90_im2col.hpp#L73-L75)：这条断言固定了坐标含义——通道、空间、batch、滤波器偏移四段，正是 im2col 的寻址本质。

> 💡 这里的两层 traits 设计（「不可执行」外层带描述符 + `.with(mbar)` 拼成「可执行」内层）与 u3-l2 讲的普通 TMA `Copy_Traits` 完全一致，只是坐标解释换成了卷积版。

#### 4.3.4 代码实践

**实践目标**：定位 im2col 在「2.x 迭代器」与「3.x TMA」两条路径上的落点，理解同一思想的两代实现。

**操作步骤**：

1. 在 2.x 路径，阅读 [include/cutlass/conv/threadblock/conv2d_fprop_activation_tile_access_iterator_analytic.h](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/conv/threadblock/conv2d_fprop_activation_tile_access_iterator_analytic.h)（用 Grep 搜 `im2col` 或 `pad_` 即可定位地址计算），观察它如何把 tile 内每个元素 (n,p,q,r,s,c) 翻译成激活的全局地址。
2. 在 3.x 路径，对照 [copy_traits_sm90_im2col.hpp:66-89](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/atom/copy_traits_sm90_im2col.hpp#L66-L89) 的 `copy_unpack`，确认它把坐标交给硬件而非软件现算地址。
3. 回答：两条路径都**没有**在显存里申请 \((NPQ)\times(RSC)\) 的缓冲区——这就是「隐式」。

**需要观察的现象**：2.x 迭代器的 `++operator` / `at()` 里能看到 `(p,r,stride,pad)` 的算术；3.x 的 `copy_unpack` 里只看到一个坐标四元组被传给 PTX 指令，没有任何地址算术。这正是「把软件寻址下放到硬件」的演进。

**预期结果**：能用自己的话说出「2.x 用软件迭代器做 im2col 采集；3.x 把采集规则编码进 TMA 描述符，由硬件完成」。

#### 4.3.5 小练习与答案

**练习 1**：Fprop 时，GEMM 的归约维 \(K_{\text{gemm}}=R\cdot S\cdot C\)。其中哪部分是 im2col「采集」来的？
**答案**：整个 \(R\cdot S\cdot C\) 都来自对激活的 im2col 展开——每个输出像素要聚合 \(R\cdot S\) 个空间窗口位置、每处 \(C\) 个通道。而 B（权重）天然按 \(KRSC\) 排布，把 \(RSC\) 视作行后就是连续的，无需 im2col 采集，所以 B 走普通 tiled TMA。

**练习 2**：既然 im2col 这么有用，为什么 CUTLASS 坚持用「隐式」而非经典物化版？
**答案**：物化 im2col 会多占 \((NPQ)\times(RSC)\) 这块巨大显存（常常比激活本身大一个数量级），且多一次全量读写。隐式 im2col 把展开融进搬运流水线，零额外显存、零额外搬运，是性能与显存的双重收益。

### 4.4 conv collective 复用 GEMM 主循环

#### 4.4.1 概念说明

讲到这里，你已经知道：卷积 = 一次 \((M,N,K)=(NPQ,K_{\text{out}},RSC)\) 的 GEMM，且 im2col 被折叠进 A 的搬运。那么 3.x 的卷积内核与 GEMM 内核到底有多少代码是共享的？

答案是：**几乎全部**。CUTLASS 3.x 把卷积 collective 设计成 GEMM collective 的「带卷积几何的兄弟」：

- `CollectiveConv` 对应 GEMM 的 `CollectiveMma`——同样是无状态主循环，模板参数同样是 `(DispatchPolicy, TileShape, ElementA, ElementB, TiledMma, TileTraitsA, TileTraitsB)`。
- 卷积的 mainloop 策略 **继承自 GEMM 的策略**：`KernelImplicitTmaWarpSpecializedSm90` 直接 `: cutlass::gemm::KernelTmaWarpSpecialized`。
- 主循环内部同样是 producer warp 发 TMA、consumer warp group 发 wgmma、`PipelineTmaAsync` 做多级缓冲同步（详见 u3-l1）。
- **唯一的差别**：A 的 TMA 搬运换成 im2col 版，并在算 tile 的坐标映射时多带一层卷积几何。

这就是「**复用关系**」：卷积不是另起炉灶，而是给 GEMM 流水线换了一个「会做窗口采集的搬运头」。

#### 4.4.2 核心流程

3.x 卷积 collective 的组装与 GEMM 几乎对称：

1. **策略分派**：`dispatch_policy.hpp` 定义 `MainloopSm90TmaGmmaWarpSpecializedImplicitGemm`，内嵌一个继承自 GEMM 的 `Schedule` 标签。`CollectiveConv` 的主模板恒报错，只有匹配这个策略的偏特化才编译通过。
2. **主循环**：偏特化里复用 `cutlass::PipelineTmaAsync<Stages>`，按 producer/consumer 分流（与 u2-l8、u2-l9 的 GEMM 主循环同构）。
3. **坐标转换**：进 mainloop 前，`get_problem_shape_MNKL` 把卷积的 `ConvProblemShape` 转成线性化的 \((M,N,K,L)\)，对 im2col 路径还要做额外线性化。
4. **kernel/epilogue**：再往上，`kernel/sm90_implicit_gemm_tma_warpspecialized.hpp` 与 `kernel/conv_universal.hpp`、`device/conv_universal_adapter.hpp` 复用 GEMM 的「kernel + adapter」外壳（对应 u2-l7 的 `GemmUniversal` / `GemmUniversalAdapter`）。

#### 4.4.3 源码精读

卷积 collective 的主模板与 GEMM 的 `collective_mma.hpp` 结构一模一样——主模板恒报错，靠 `#include` 把各架构偏特化挂上来：

```cpp
template <class DispatchPolicy, class TileShape, class ElementA, class ElementB,
          class TiledMma, class TileTraitsA, class TileTraitsB>
struct CollectiveConv {
  static_assert(cutlass::detail::dependent_false<ElementA>, "Could not find a mainloop specialization.");
};
...
#include "sm90_implicit_gemm_gmma_ss_warpspecialized.hpp"
#include "sm100_implicit_gemm_umma_warpspecialized.hpp"
```

> [include/cutlass/conv/collective/collective_conv.hpp:51-62](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/conv/collective/collective_conv.hpp#L51-L62)：与 GEMM 的 `CollectiveMma`（主模板报错 + 按架构 include 偏特化）完全对称，只是文件名换成 `sm90_implicit_gemm_*`。

最能体现「复用」的是策略继承——卷积的 schedule 标签直接继承自 GEMM：

```cpp
struct KernelImplicitTmaWarpSpecializedSm90 : cutlass::gemm::KernelTmaWarpSpecialized { };
```

> [include/cutlass/conv/dispatch_policy.hpp:53](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/conv/dispatch_policy.hpp#L53)：卷积的策略标签是 GEMM 策略 `KernelTmaWarpSpecialized` 的子类，意味着它「就是一个 GEMM warp-specialized 内核」，额外只携带卷积专属信息（ConvOp、空间维数、Stages）。

SM90 collective 偏特化的类型别名里，主循环流水线直接复用 GEMM 的：

```cpp
using MainloopPipeline = cutlass::PipelineTmaAsync<DispatchPolicy::Stages>;
...
using ProblemShape = ConvProblemShape<ConvOp, NumSpatialDimensions>;
```

> [sm90_implicit_gemm_gmma_ss_warpspecialized.hpp:101-106](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/conv/collective/sm90_implicit_gemm_gmma_ss_warpspecialized.hpp#L101-L106)：`MainloopPipeline` 就是 GEMM 在 u3-l1 里讲的 `PipelineTmaAsync`，`ProblemShape` 换成卷积版。同一套搬算重叠机制，换个 ProblemShape 就跑卷积。

进入主循环前，把卷积问题线性化成 GEMM 的 `(M,N,K,L)`：

```cpp
static constexpr auto
get_problem_shape_MNKL(ProblemShape const& problem_shape) {
  if constexpr (is_im2col_A || is_im2col_B) {
    return cutlass::conv::detail::get_linearized_problem_shape_MNKL(problem_shape);
  } else {
    return cutlass::conv::detail::get_transformed_problem_shape_MNKL(problem_shape);
  }
}
```

> [sm90_implicit_gemm_gmma_ss_warpspecialized.hpp:254-265](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/conv/collective/sm90_implicit_gemm_gmma_ss_warpspecialized.hpp#L254-L265)：这一步是「卷积→GEMM」的协议层翻译——之后主循环就拿这个 `(M,N,K,L)` 当普通 GEMM 来调度，再感知不到它是卷积。

> 💡 与 GEMM 一样，卷积也有自己的 `CollectiveBuilder`（[include/cutlass/conv/collective/collective_builder.hpp](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/conv/collective/collective_builder.hpp)），吃架构/类型/tile/策略，自动推断 `TiledMma`、TMA atom 与 smem 布局。它的工作方式与 u2-l8 的 GEMM `CollectiveBuilder` 同构。

#### 4.4.4 代码实践

**实践目标**：横向对照 CUTLASS 3.x 的卷积 collective 与 GEMM collective，确认「同构 + 换搬运头」的复用关系。

**操作步骤**：

1. 打开 [collective_conv.hpp:51-62](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/conv/collective/collective_conv.hpp#L51-L62)，再打开 GEMM 的 [include/cutlass/gemm/collective/collective_mma.hpp](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/gemm/collective/collective_mma.hpp)，对比二者的主模板声明（参数个数、顺序、`static_assert` 兜底）。
2. 打开 [dispatch_policy.hpp:53](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/conv/dispatch_policy.hpp#L53)，确认卷积 schedule 标签继承自 GEMM 的 `KernelTmaWarpSpecialized`。
3. 在 [sm90_implicit_gemm_gmma_ss_warpspecialized.hpp:101-104](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/conv/collective/sm90_implicit_gemm_gmma_ss_warpspecialized.hpp#L101-L104) 找到 `MainloopPipeline` 与 `PipelineState`，回忆 u3-l1 讲的双屏障模型——它们与 GEMM SM90 collective 里的是同一个类型。

**需要观察的现象**：你会发现卷积 collective 与 GEMM collective 的骨架几乎逐行对应，差异集中在 `is_im2col_A/B`、`get_tma_load_a_instance` 这类「搬运头」上，主循环与流水线原封不动。

**预期结果**：能画出一张表——「GEMM 部件 → 卷积里对应部件」（`CollectiveMma`→`CollectiveConv`、`KernelTmaWarpSpecialized`→被继承、`PipelineTmaAsync`→复用、普通 TMA A→im2col TMA A）。

#### 4.4.5 小练习与答案

**练习 1**：卷积的 `KernelImplicitTmaWarpSpecializedSm90` 为什么要继承 GEMM 的 `KernelTmaWarpSpecialized`？
**答案**：因为卷积内核的调度逻辑（warp specialization、cluster 启动、流水线 stage 数）与 GEMM 完全相同，继承后可直接被 u2-l7 讲的 `enable_if_t<is_base_of_v<Schedule,...>>` 内核偏特化选中，做到「卷积内核 = GEMM 内核 + 卷积搬运头」，零重复代码。

**练习 2**：`get_problem_shape_MNKL` 为什么对 im2col 路径要走 `get_linearized_problem_shape_MNKL`？
**答案**：im2col 把空间窗口折叠进归约维后，问题需要被线性化成一个扁平的 \((M,N,K,L)\)（L 是 batch/split-k 那一维），才能被当成普通 GEMM 调度；非 im2col 路径（如某些 wgrad 配置）只做坐标变换、不线性化即可。这是「卷积几何 → GEMM 几何」的最后一次形式转换。

## 5. 综合实践

把本讲四个最小模块串起来，完成一次「**给一个卷积，亲手翻译成隐式 GEMM 并在源码里走一遍**」的端到端练习。

**任务**：给定一个 2D 卷积 Fprop 问题——\(N=1, H=224, W=224, C=32, K=32, R=3, S=3\)，stride=1，对称 padding=1，dilation=1，groups=1。

**步骤**：

1. **算输出尺寸**：用 4.2 的公式算 \(P,Q\)。
2. **算等价 GEMM**：用 4.1 的公式算 \((M,N,K_{\text{gemm}})\)。
3. **定位 im2col 维**：指出 \(K_{\text{gemm}}\) 中哪部分来自 im2col 采集（4.3）。
4. **源码对账**：
   - 打开 [conv2d_problem_size.h:332-338](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/conv/conv2d_problem_size.h#L332-L338)，确认你的 \((M,N,K)\) 与代码一致；
   - 打开 [sm90_implicit_gemm_gmma_ss_warpspecialized.hpp:121-122](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/conv/collective/sm90_implicit_gemm_gmma_ss_warpspecialized.hpp#L121-L122) 确认 Fprop 时 A 走 im2col、B 走 tiled。
5. **（可选，需 SM80 GPU）运行示例**：编译运行 [examples/16_ampere_tensorop_conv2dfprop](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/16_ampere_tensorop_conv2dfprop/ampere_tensorop_conv2dfprop.cu)，命令为：

   ```bash
   cmake -B build -DCUTLASS_NVCC_ARCHS=80
   cmake --build build -j --target 16_ampere_tensorop_conv2dfprop
   ./build/examples/16_ampere_tensorop_conv2dfprop/16_ampere_tensorop_conv2dfprop \
       --n=1 --h=224 --w=224 --c=32 --k=32 --r=3 --s=3 --ref-check
   ```

   观察它打印的 GFLOP/s（其计算公式见 [ampere_tensorop_conv2dfprop.cu:472-479](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/examples/16_ampere_tensorop_conv2dfprop/ampere_tensorop_conv2dfprop.cu#L472-L479)，正是用 `output_size().product() * R*S*C` 数 FMA）。若无可用的 SM80 GPU，标注「待本地验证」。

**预期结果**：

- \(P=Q=224\)（same padding 下 3×3/stride1 保持尺寸）。
- \(M=1\cdot224\cdot224=50176\)、\(N=32\)、\(K_{\text{gemm}}=3\cdot3\cdot32=288\)。
- im2col 贡献的是整个 \(K_{\text{gemm}}=RSC=288\)，即激活的空间窗口 \(R\cdot S=9\) 与通道 \(C=32\) 的乘积。
- 示例若运行成功，`--ref-check` 会与主机参考实现逐元素比对并报告通过。

## 6. 本讲小结

- **卷积 = 隐式 GEMM**：一次 Fprop 卷积等价于 \((M,N,K)=(NPQ,\;K_{\text{out}},\;RSC)\) 的矩阵乘，映射表见 [convolution.h:54-61](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/conv/convolution.h#L54-L61)，公式见 [implicit_gemm_problem_size](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/conv/conv2d_problem_size.h#L327-L356)。
- **NHWC/KRSC/NPQK** 是 CUTLASS 卷积的标准布局，通道放最内层以适配 Tensor Core；2.x 用 `Conv2dProblemSize`，3.x 用与空间维数无关的 `ConvProblemShape` 自动推导 A/B/C。
- **im2col 把空间窗口（\(RSC\)）展开进归约维**，经典做法会物化巨大中间矩阵；CUTLASS 用「隐式」——2.x 在 tile 迭代器里现算地址，3.x 把采集规则编码进 `SM90_TMA_LOAD_IM2COL` 描述符，由 TMA 硬件完成，零额外显存。
- **Fprop 时 A（激活）走 im2col、B（权重）走普通 tiled TMA**，由 collective 的 `static_assert` 在编译期强约束（[sm90_implicit_gemm_gmma_ss_warpspecialized.hpp:121-135](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/conv/collective/sm90_implicit_gemm_gmma_ss_warpspecialized.hpp#L121-L135)）。
- **conv collective 与 GEMM collective 同构复用**：`CollectiveConv` 对应 `CollectiveMma`，卷积策略继承 GEMM 策略（[dispatch_policy.hpp:53](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/conv/dispatch_policy.hpp#L53)），主循环直接复用 `PipelineTmaAsync`——卷积只是「换了 im2col 搬运头」的 GEMM。

## 7. 下一步学习建议

- **深入 SM90 卷积主循环**：精读 [sm90_implicit_gemm_gmma_ss_warpspecialized.hpp](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/conv/collective/sm90_implicit_gemm_gmma_ss_warpspecialized.hpp) 的 `operator()` 主循环，结合 u3-l1 的 `PipelineTmaAsync` 双屏障模型，标注出 producer/consumer 的等待与释放点。
- **Blackwell 卷积**：阅读 [include/cutlass/conv/collective/sm100_implicit_gemm_umma_warpspecialized.hpp](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/conv/collective/sm100_implicit_gemm_umma_warpspecialized.hpp) 与示例 76，对照 u3-l7 的 SM100 GEMM，理解 UMMA/TMEM 如何同样被卷积复用。
- **卷积 kernel 外壳与调度器**：看 [include/cutlass/conv/kernel/sm90_implicit_gemm_tma_warpspecialized.hpp](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/conv/kernel/sm90_implicit_gemm_tma_warpspecialized.hpp) 与 `conv_universal.hpp`，理解卷积如何套用 u2-l7 讲的 `Universal` 外壳与 u3-l3 讲的 tile scheduler。
- **分组/深度可分离卷积**：留意 `ConvProblemShape` 里的 `groups` 字段与 `GroupMode`（[convolution.h:126-131](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/conv/convolution.h#L126-L131)），它是 u3-l4「Grouped GEMM」思想在卷积上的延伸。
