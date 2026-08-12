# 稠密矩阵分解 HLS 内核（solver L1）

## 1. 本讲目标

本讲进入 Vitis_Libraries 的 **solver（求解器）库**，聚焦它在 L1 层提供的「稠密矩阵分解」类 HLS 内核。读完本讲，你应当能够：

- 说清 solver 库提供了哪几类矩阵分解（Cholesky / QR / SVD），以及它们各自对输入矩阵的「性质要求」。
- 读懂 solver 内核的统一写法：**流式入口函数 + traits 类型结构体 + 多架构（ARCH）选择**。
- 理解「三角求解（back-substitute）」与「矩阵求逆」如何由分解内核组合而成。
- 区分 `float`、`ap_fixed` 与 `cfloat`（自定义浮点）三套数据类型变体。
- 在 `solver/L1/tests/hw/cholesky` 下跑通一个 L1 用例的 `csim`，并解释 PASS/FAIL 判据。

本讲承接 u3-l2（HLS pragma 如何映射硬件）与 u2-l3（HLS TARGET 流程与综合报告）。你会看到 `pipeline II=`、`ARRAY_PARTITION`、`DATAFLOW` 这些指令在真实数值内核里的用法，也会再次用到「目录即用例 + `make run TARGET=csim`」的 L1 流程。

## 2. 前置知识

在进入源码前，先用最朴素的语言回顾几个线性代数直觉。设 \(A\) 是一个 \(n\times n\) 方阵。

**为什么要「分解」？** 直接解方程 \(Ax=b\) 或求 \(A^{-1}\) 代价高且数值不稳。做法是先把 \(A\) 拆成「结构简单」的因子之积，再对因子求解。三种最常用的分解：

- **Cholesky 分解**：当 \(A\) 是**埃尔米特正定**（Hermitian positive definite，实数情形即对称正定）时，\(A = L L^{H}\)，\(L\) 是下三角，\(L^{H}\) 是 \(L\) 的共轭转置。关键性质：对角元 \(L_{jj}\) 恒为**实数**，且 \(A_{jj}-\sum_{k<j}|L_{jk}|^{2}\) 必须 \(\ge 0\) 才能开根——一旦为负，说明 \(A\) 不是正定的，分解失败。
- **QR 分解**：任意矩阵 \(A=QR\)，\(Q\) 是正交（或酉）矩阵，\(R\) 是上三角。不要求 \(A\) 有特殊结构，因此适用面最广，是求一般矩阵逆的首选。
- **SVD（奇异值分解）**：\(A = U\Sigma V^{H}\)，\(\Sigma\) 是对角（奇异值），\(U/V\) 是正交（酉）。solver 用 **Jacobi 方法**（反复做 \(2\times2\) 旋转把非对角能量「挤」到对角）实现，最稳但最贵。

**三角求解（back-substitution）**：一旦有了上三角 \(R\)，解 \(Rx=b\) 可以「从最后一行倒推」：\(x_n=b_n/R_{nn}\)，\(x_i=(b_i-\sum_{j>i}R_{ij}x_j)/R_{ii}\)。把 \(b\) 换成单位阵的各列，就得到 \(R^{-1}\)。这是求逆的最后一步。

**埃尔米特正定** 是本讲反复出现的词：实数情形就是「对称 + 所有特征值大于 0」。Cholesky 与「对称矩阵特征值」都依赖它。

> 关于 **LU 分解**：solver/README.md 在「Matrix decomposition」一节列出了 LU（含部分主元法），但在当前 HEAD 的 `solver/L1/include/hw/` 中**没有独立的 LU 头文件**（已逐一核对，仅 `grep` 在 `qrd.hpp`/`qrdfloat.hpp` 的注释里出现 "LU" 字样）。因此本讲对 LU 的具体 HLS 接口标注「待确认」，不臆造其 API；下面只讲源码中确实存在的 Cholesky/QR/SVD 内核。

## 3. 本讲源码地图

solver 库的 L1 头件全部集中在 `solver/L1/include/hw/`，本讲涉及的关键文件如下：

| 文件 | 作用 | 对应最小模块 |
|------|------|--------------|
| [solver/L1/include/hw/cholesky.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/hw/cholesky.hpp) | Cholesky 分解入口 `cholesky` + traits + 三种架构 | 4.1 |
| [solver/L1/include/hw/qrf.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/hw/qrf.hpp) | 通用 QR 分解入口 `qrf`（Givens 旋转，float/fixed/complex） | 4.1 |
| [solver/L1/include/hw/qrd.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/hw/qrd.hpp) | 高吞吐 cfloat QR（多计算单元 NCU，面向大矩阵） | 4.3 |
| [solver/L1/include/hw/svd.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/hw/svd.hpp) | SVD 入口 `svd`（Jacobi 方法，输出 S/U/V） | 4.1 |
| [solver/L1/include/hw/back_substitute.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/hw/back_substitute.hpp) | 三角求解 / 三角阵求逆 `backSubstitute` | 4.2 |
| [solver/L1/include/hw/cholesky_inverse.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/hw/cholesky_inverse.hpp) | 组合「Cholesky + 回代 + 矩阵乘」求正定阵的逆 | 4.2 |
| [solver/L1/include/hw/qr_inverse.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/hw/qr_inverse.hpp) | 组合「QR + 回代 + 矩阵乘」求一般阵的逆 | 4.2 |
| [solver/L1/include/hw/cholesky_cfloat.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/hw/cholesky_cfloat.hpp) | Cholesky 的 cfloat 变体 `cholesky_cfloat` | 4.3 |
| solver/L1/include/hw/xf_solver_L1.hpp | 汇总头，`#include` 上述所有内核，方便一次引入 | 4.4 |

测试侧以 Cholesky 为例：

| 文件 | 作用 |
|------|------|
| [solver/L1/tests/hw/cholesky/kernel/kernel_cholesky.hpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/tests/hw/cholesky/kernel/kernel_cholesky.hpp) | DUT 的 `extern "C"` 声明 |
| [solver/L1/tests/hw/cholesky/kernel/kernel_cholesky_0.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/tests/hw/cholesky/kernel/kernel_cholesky_0.cpp) | DUT 实现：把模板 `cholesky` 实例化成固定签名 |
| [solver/L1/tests/hw/cholesky/host/test_cholesky.cpp](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/tests/hw/cholesky/host/test_cholesky.cpp) | testbench：读数据、调 DUT、与 LAPACK 参考比对 |
| solver/L1/tests/hw/cholesky/float_arch1/{description.json,Makefile} | 用例元数据与构建入口（`flow=hls`，`topfunction=kernel_cholesky_0`） |

---

## 4. 核心概念与源码讲解

solver 库的所有数值内核都遵循同一套「模板 + traits + 流式入口」的写法。掌握一个（Cholesky），其余（QR/SVD/back_substitute）就是同一模板的不同数学内核。本讲按四个最小模块展开：①分解家族 ②三角求解与求逆 ③cfloat 变体 ④L1 测试。

### 4.1 分解家族：Cholesky / QR / SVD

#### 4.1.1 概念说明

solver 把「矩阵分解」做成 header-only 的模板函数，对外暴露一个**流式入口**（输入输出都是 `hls::stream`，承接 u3-l1 的流式约定）。每个入口背后：

- 一个 **traits 结构体**：集中声明所有「中间变量的类型」与「架构开关」。这是定点数（`ap_fixed`）能正确综合的关键——乘法、累加、开根号、倒数都需要比输入更宽的位宽，traits 把这些位宽推导集中管理，并按数据类型做模板特化。
- 一个 **架构选择（ARCH）**：同一数学运算提供「省资源 / 低延迟 / 更低延迟」多套实现，由 `traits::ARCH` 在编译期二选一/三选一。这是 HLS 内核「面积 vs 性能」权衡的标准手法（见 u3-l2）。
- 一个 **返回码 / 奇异标志**：分解可能失败（Cholesky 遇到非正定、三角阵对角元为 0），失败信息通过返回值或输出参数传回主机。

Cholesky 的核心数学（实数下三角情形）：

\[
L_{jj}=\sqrt{\,A_{jj}-\sum_{k=0}^{j-1}L_{jk}^{2}\,},\qquad
L_{ij}=\frac{A_{ij}-\sum_{k=0}^{j-1}L_{ik}L_{jk}}{L_{jj}},\quad i>j
\]

被开方的量一旦为负 ⇒ \(A\) 非正定 ⇒ 失败。这就是「Cholesky 要求矩阵正定对称」在代码里的直接体现。

#### 4.1.2 核心流程

以 Cholesky 为例，入口函数 `cholesky` 的执行过程：

1. **读入**：从 `matrixAStrm` 按行把 \(A\) 读进片上二维数组 `A[RowsColsA][RowsColsA]`。
2. **分派**：调 `choleskyTop`，按 `traits::ARCH` 在 `choleskyBasic/choleskyAlt/choleskyAlt2` 三种架构间 `switch`。
3. **列循环**：对每一列 \(j\)，先算对角元 \(L_{jj}\)（含开根与正定性检查），再算该列下方所有非对角元 \(L_{ij}\)。
4. **回写**：把结果 \(L\) 按行写回 `matrixLStrm`。
5. 返回 0 成功 / 1 失败。

三种架构的取舍（来自头文件顶部注释）：

| 架构 | ARCH | 特点 |
|------|------|------|
| `choleskyBasic` | 0 | 资源最少，延迟最高 |
| `choleskyAlt` | 1（默认） | 用压缩存储 + 对角倒数，降延迟、增资源 |
| `choleskyAlt2` | 2 | 进一步用 `ARRAY_PARTITION` + `UNROLL`，最低延迟、最高资源 |

QR/SVD 走的是同一「流式入口 → traits 分派」骨架，只是内部数学不同：`qrf` 用 Givens 旋转逐列消元；`svd` 用 Jacobi 扫描反复施加 \(2\times2\) 旋转。

#### 4.1.3 源码精读

**(a) Cholesky 入口 `cholesky`** —— 读流 → 片上数组 → 调 `choleskyTop` → 写流。这是所有 solver 内核的标准外壳：

[_solver/L1/include/hw/cholesky.hpp:702-728_](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/hw/cholesky.hpp#L702-L728)：`cholesky` 的流式入口。注意 `#pragma HLS PIPELINE` 包住读写循环，把矩阵传输流水化；`ret = choleskyTop<...>(A, L)` 是真正的计算。

**(b) 架构分派 `choleskyTop`** —— 编译期 `switch`，零运行时代价：

[_solver/L1/include/hw/cholesky.hpp:673-685_](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/hw/cholesky.hpp#L673-L685)：`choleskyTop` 按 `CholeskyTraits::ARCH` 选择 Basic/Alt/Alt2。由于 `ARCH` 是编译期常量，HLS 会裁掉其余分支。

**(c) 正定性检查 `cholesky_sqrt_op`** —— 「矩阵性质要求」的代码化身：

[_solver/L1/include/hw/cholesky.hpp:213-223_](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/hw/cholesky.hpp#L213-L223)：当被开方的数 `< 0` 时，置结果为 0 并**返回 1**。这个 1 一路冒泡到 `cholesky` 的返回值，成为主机判断「输入非正定」的依据。另有复数特化版本（213-253），利用「对角元恒为实数」只开实部平方根，省掉昂贵的复数开方。

**(d) traits 把位宽推导集中管理** —— 看 `ap_fixed` 特化如何放大位宽：

[_solver/L1/include/hw/cholesky.hpp:97-129_](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/hw/cholesky.hpp#L97-L129)：`PROD_T` 是两倍输入宽（乘积）、`ACCUM_T` 再加 \(\lceil\log_2 N\rceil\) 位（累加）、`DIAG_T` 承接开根结果。默认 traits（44-61）对 `float` 不放大，因为浮点位宽固定；定点才需要。`ARCH`/`INNER_II`/`UNROLL_FACTOR` 也定义在这里。

**(e) 内层循环的 `pipeline II`** —— 真正决定吞吐的指令，承接 u3-l2：

[_solver/L1/include/hw/cholesky.hpp:366-378_](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/hw/cholesky.hpp#L366-L378)：`choleskyBasic` 的最内层 `sum_loop`，`#pragma HLS PIPELINE II = CholeskyTraits::INNER_II`（默认 II=1）。II=1 意味着每个周期累加一个乘积项。

**(f) QR 入口 `qrf`** —— 同骨架，数学换为 Givens 旋转：

[_solver/L1/include/hw/qrf.hpp:862-884_](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/hw/qrf.hpp#L862-L884)：`qrf` 同样按 `QRF_TRAITS::ARCH` 在 `qrf_basic/qrf_alt` 间分派；模板参数 `TransposedQ` 控制 \(Q\) 是否以转置形式输出（求逆时需要 \(Q^H\)，故置 true）。

**(g) SVD 入口 `svd`** —— 输出三路流（奇异值 + 左/右奇异向量）：

[_solver/L1/include/hw/svd.hpp:1534-1574_](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/hw/svd.hpp#L1534-L1574)：`svd` 内部调 `svdTop`（由 `svdPairs` 反复施加 `svd2x2` 旋转构成 Jacobi 扫描）。注意它对 `matrixSStrm/matrixUStrm/matrixVStrm` 三路输出分别流水写回。

#### 4.1.4 代码实践（源码阅读型）

**目标**：用源码回答「Cholesky 的三种架构各自用了哪些 HLS 优化手段」。

**步骤**：
1. 打开 `solver/L1/include/hw/cholesky.hpp`，分别定位 `choleskyBasic`（295 行起）、`choleskyAlt`（412 行起）、`choleskyAlt2`（515 行起）。
2. 统计每个架构里的 `#pragma HLS`：`choleskyAlt` 用了一维压缩存储 `L_internal[(N*N-N)/2]` 与「对角倒数」技巧（避免每次 off-diagonal 都做除法，见 498-503 行）；`choleskyAlt2` 额外用了 `ARRAY_PARTITION`（537-542 行）与 `UNROLL FACTOR`（592 行）。
3. 回答：从 Basic→Alt→Alt2，资源（DSP/BRAM）如何变化？延迟（latency）如何变化？

**预期结果**：Alt 相比 Basic 用更复杂的存储与一个额外倒数计算换取更短的关键路径；Alt2 用分区+展开进一步压低延迟，代价是近似线性增长的 DSP。这些是**待本地验证**的预判（需跑 `csynth` 看报告，见 4.4）。

#### 4.1.5 小练习与答案

**练习 1**：`cholesky` 的返回值在什么情况下为 1？主机据此能推断出输入矩阵的什么性质？
**答**：当任一对角开方 `cholesky_sqrt_op` 收到负数时返回 1，意味着输入矩阵**不是埃尔米特/对称正定**。

**练习 2**：为什么 traits 对 `float` 不放大位宽，对 `ap_fixed` 却要放大？
**答**：浮点数的指数自动伸缩，位宽固定；定点数乘法位数翻倍、累加还要再加 \(\lceil\log_2 N\rceil\) 位，否则会溢出/截断，故需 traits 显式声明更宽的中间类型。

**练习 3**：`qrf` 的模板参数 `TransposedD`/`TransposedQ` 为何在「求逆」场景下要置 true？（提示：看 4.2 的 `qrInverse`。）
**答**：QR 求逆需要 \(A^{-1}=R^{-1}Q^{H}\)，让 `qrf` 直接产出转置（即 \(Q^H\)）形式，可省掉一次显式共轭转置。

---

### 4.2 三角求解与矩阵求逆

#### 4.2.1 概念说明

「分解」本身不直接给方程的解或逆矩阵，它只产出结构化的因子。要得到 \(A^{-1}\)，还需要两步：

1. **三角阵求逆**：对上三角 \(R\) 求 \(R^{-1}\)。这本质是「解 \(RX=I\)」，用 `backSubstitute`（回代）完成。
2. **矩阵乘**：把因子拼回。例如 QR 求逆：\(A^{-1}=R^{-1}Q^{H}\)；Cholesky 求逆：\(A^{-1}=U^{-1}U^{-H}\)（\(U\) 是 Cholesky 的上三角因子）。

solver 把这两步也做成独立的 L1 内核（`backSubstitute`），再用 `choleskyInverse` / `qrInverse` 这两个「组合入口」把分解+回代+矩阵乘串起来。组合入口用 `#pragma HLS DATAFLOW`（见 u3-l2）让三段任务级流水并发，这正是「L2/L3 多内核流水」在 L1 层的微缩版。

注意一个贯穿 trick：分解（Cholesky/QR）的对角元恒为**实数**，所以回代里「取对角倒数」可以用**实数除法器**而非复数除法，省资源（`back_substitute.hpp` 149-167 行的注释与实现专门强调这一点）。

#### 4.2.2 核心流程

**`backSubstitute` 流程**（以上三角 \(R\) 为输入）：
1. 读 \(R\) 进片上数组。
2. 对每个对角元 \(R_{ii}\) 算倒数 `1/R_ii`；若倒数为 0 ⇒ 矩阵奇异，置 `is_singular=1`。
3. 按列回代：先解最后一列（只有对角元），再逐列向左，每列用到前面已解出的列。
4. 写回 \(B=R^{-1}\)，输出 `is_singular`。

**`qrInverse` 组合流程**（DATAFLOW 三段）：
1. `qrf<TRANSPOSED_Q=true>(A → Q, R)`：分解，产出转置 \(Q\)。
2. `backSubstitute(R → R^{-1}, singular)`：三角求逆。
3. `matrixMultiply(R^{-1}, Q → A^{-1})`：拼回，\(A^{-1}=R^{-1}\cdot Q^H\)。

`choleskyInverse` 同构，只是第 1 段换成 `cholesky<LOWER_TRIANGULAR=false>(A → U)`（取上三角 \(U\)），第 3 段乘法是 \(A^{-1}=U^{-1}\cdot U^{-H}\)（第二操作数共轭转置）。

#### 4.2.3 源码精读

**(a) `backSubstitute` 流式入口** —— 与 `cholesky` 完全同构的外壳：

[_solver/L1/include/hw/back_substitute.hpp:374-397_](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/hw/back_substitute.hpp#L374-L397)：读流→数组→`backSubstituteTop`→写流。注意 `is_singular` 是**输出参数**（引用），用于把「矩阵奇异」回报给调用者。

**(b) 奇异性检测** —— 对角倒数为 0 即奇异：

[_solver/L1/include/hw/back_substitute.hpp:214-220_](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/hw/back_substitute.hpp#L214-L220)：`backSubstituteBasic` 里，算完 `diag_recip` 后判断其虚实部是否均为 0；若是，置 `is_singular=1`。内层 `back_substitute_k` 循环同样带 `PIPELINE II=INNER_II`（235 行）。

**(c) `qrInverse` 组合入口（DATAFLOW）** —— 三段任务级流水：

[_solver/L1/include/hw/qr_inverse.hpp:105-129_](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/hw/qr_inverse.hpp#L105-L129)：`#pragma HLS DATAFLOW` 包住 `qrf → backSubstitute → matrixMultiply` 三段，段间用 `hls::stream`（`matrixQStrm/matrixRStrm/matrixInverseRStrm`，均带 `depth=16`）连接。注释 `A^-1 = R^-1*Qt`（125 行）点明数学关系。`A_singular` 由 `backSubstitute` 透传出来。

**(d) `choleskyInverse` 组合入口** —— 与 `qrInverse` 同构：

[_solver/L1/include/hw/cholesky_inverse.hpp:218-242_](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/hw/cholesky_inverse.hpp#L218-L242)：`cholesky → backSubstitute → matrixMultiply`。这里 `cholesky` 的返回值（正定性失败标志）赋给 `cholesky_success`（230 行），第 3 段乘法用 `ConjugateTranspose`（240 行）实现 \(U^{-H}\)。traits 把三段的精度分开声明（`CHOLESKY_OUT`/`BACK_SUBSTITUTE_OUT`）。

#### 4.2.4 代码实践（源码阅读型）

**目标**：理解「实数除法器」trick 如何省资源。

**步骤**：
1. 读 `back_substitute.hpp` 的 `back_substitute_recip` 复数重载（149-167 行）。
2. 注意它取 `ONE.real() / x.real()`——只除实部，虚部直接置 0。
3. 对照注释（143-148 行）：分解的对角元恒为实数，故复数情形也只需实数除法。

**预期结果**：复数 `backSubstitute` 的对角倒数用 1 个实数除法器而非 1 个复数除法器（复数除法通常要多个乘加+1 次实除），DSP 占用显著降低。**待本地验证**：可在 `csynth` 报告里对比 DSP 数。

#### 4.2.5 小练习与答案

**练习 1**：`qrInverse` 和 `choleskyInverse` 都用了 `DATAFLOW`。如果删掉这个 pragma，三段会怎样执行？
**答**：三段退化为顺序执行（先算完 `qrf` 再算 `backSubstitute`……），端到端 latency 变为三段之和，失去任务级并发的吞吐收益（复习 u3-l2 的 dataflow）。

**练习 2**：`choleskyInverse` 同时输出 `cholesky_success` 和内部 `U_singular`，两者各代表什么？
**答**：`cholesky_success` 来自 Cholesky 分解，=1 表示输入**非正定**；`U_singular` 来自回代，=1 表示三角因子的对角出现 0（矩阵奇异、不可逆）。前者是「输入性质不满足」，后者是「数值上不可逆」。

**练习 3**：为什么 `qrInverse` 能用于一般矩阵，而 `choleskyInverse` 只能用于正定对称阵？
**答**：QR 分解对任意矩阵成立，故 `qrInverse` 通用；Cholesky 分解要求埃尔米特正定，故 `choleskyInverse` 受此约束，但正定条件下它通常更快/更省资源。

---

### 4.3 cfloat 变体与高性能 QR

#### 4.3.1 概念说明

solver 的数值内核支持三类数据类型：

- **`float` / `std::complex<float>`**：标准 IEEE 单精度，走 traits 默认特化（不放大位宽）。
- **`ap_fixed` / 复数定点**：任意位宽定点，走 traits 的定点特化（放大中间位宽，见 4.1）。
- **`cfloat`（Xilinx 自定义浮点）**：这是 HLS 专用的浮点类型，位宽可定制（如 `cfloat<23,1>` 表 23 位尾数、1 位符号相关字段），综合后比 IEEE float 更省资源、更适合 FPGA。solver 为它单独提供 `_cfloat` 后缀的变体内核。

对「大矩阵 + 高吞吐」场景，solver 还提供基于 cfloat 的 **`qrd`**：它用 **NCU（Number of Compute Units，多计算单元）** 把矩阵按行切分到多个并行处理单元，每个单元处理一个行块，靠 ping-pong 缓冲与 `ARRAY_PARTITION` 拉满吞吐，面向 \(1024\times256\) 这类大矩阵。这与 `qrf`（单单元、Givens、面向中小矩阵）形成互补。

此外还有 `block_cholesky_cfloat`（分块 Cholesky，把大矩阵切成 \(B\times B\) 块做 in-place 分解），适合装不下整矩阵的大规模问题。

#### 4.3.2 核心流程

**`cholesky_cfloat` 流程**（与 `cholesky` 同骨架，但数学内核独立实现）：
1. 从流读入 `matA[DIM][DIM]`。
2. 调 `cholesky_cfloat_core` 做 **in-place** 分解（结果直接写回 `matA` 的下三角）。
3. 按 `LowerTriangularL` 选择输出下三角，或输出上三角（共轭转置形式）。

**`qrd_cfloat_core` 流程**（多计算单元）：
1. 输入 `dataA[NCU][RowsA/NCU][ColsA]`——行维度被切成 NCU 个块。
2. 对每一列 \(k\)，调 `qrd_col_dataflow_wrapper_vector2` 做一次「投影 + 归一化 + 更新」。
3. 用 `proj[2]/dataK[2]/norm[2]` 双缓冲 ping-pong，让相邻两列的计算重叠。
4. 输出 \(R\) 流，\(Q\) in-place 写回 `dataA`。

#### 4.3.3 源码精读

**(a) `cholesky_cfloat` 流式入口**：

[_solver/L1/include/hw/cholesky_cfloat.hpp:142-173_](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/hw/cholesky_cfloat.hpp#L142-L173)：注意它**没有 traits/ARCH 分派**，直接调 `cholesky_cfloat_core`（87 行起）做 in-place 分解；`LowerTriangularL=false` 时通过 `std::conj(matA[c][r])` 输出上三角共轭（169 行）。相比 `cholesky`，它更紧凑，专为 cfloat 优化。

**(b) `qrd_cfloat_core` 多计算单元与分区**：

[_solver/L1/include/hw/qrd.hpp:665-700_](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/hw/qrd.hpp#L665-L700)：`ARRAY_PARTITION ... complete dim=1` 把 NCU 维完全分区（669 行），让 NCU 个计算单元并行；`cyclic factor=UnrollSize_t/NCU dim=2`（670 行）进一步切行块；`ping_pong_flag`（685 行）+ `proj/dataK/norm` 的双份缓冲（672-683 行）让列间计算重叠。其文档（641-664 行）明确这是「Level 1: high throughput version for Complex Float QR 1024*256」。

**(c) `qrd` 的对外顶层**：

[_solver/L1/include/hw/qrd.hpp:719-721_](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/include/hw/qrd.hpp#L719-L721)：`qrd_ip_ncu_top` 由 `PowNCU` 推导出 `NCU_t`，再把一维指针 `A` 视作二维 `[NCU][RowsA/NCU][ColsA]` 调 `qrd_cfloat_core`。它是大矩阵 cfloat QR 的对外入口。

#### 4.3.4 代码实践（源码阅读型）

**目标**：对比 `cholesky`（float/通用）与 `cholesky_cfloat`（cfloat 专用）的代码结构差异。

**步骤**：
1. 打开 `cholesky.hpp` 与 `cholesky_cfloat.hpp` 并排。
2. 数一数：`cholesky` 有几个 traits 特化、几个 ARCH？`cholesky_cfloat` 有没有 traits？
3. 看 `cholesky_cfloat` 是否支持复数（提示：169 行用了 `std::conj`）。

**预期结果**：`cholesky` 重在「类型/架构通用性」（多 traits 特化 + 3 架构），`cholesky_cfloat` 重在「为 cfloat 做定点优化、结构精简」。两者数学等价，选型取决于数据类型与资源目标。

#### 4.3.5 小练习与答案

**练习 1**：`qrd` 的 `NCU` 参数（如 32）在硬件上对应什么？
**答**：32 个并行计算单元，每个处理 `RowsA/32` 行；靠 `ARRAY_PARTITION complete dim=1` 让它们同时工作，吞吐随 NCU 近似线性扩展（复习 u3-l2 的 unroll/array_partition）。

**练习 2**：`cholesky_cfloat` 为何不再需要 traits？
**答**：cfloat 的位宽在类型自身里固定（如 `cfloat<W,I>`），不像 `ap_fixed` 那样需要为「乘积/累加/开根」逐级推导中间位宽，故可省掉 traits 的位宽声明，结构更直接。

**练习 3**：什么时候选 `qrf`，什么时候选 `qrd`？
**答**：中小矩阵、需 float/fixed/复数多种类型 → `qrf`（通用、Givens）；大矩阵（如 1024×256）、cfloat、追求极致吞吐 → `qrd`（多 CU、ping-pong）。

---

### 4.4 L1 测试：以 Cholesky 为例跑通 csim

#### 4.4.1 概念说明

solver 的 L1 用例沿用 u2-l2/u2-l3 确立的「目录即用例」约定，但目录层次比 utils 稍多。以 `solver/L1/tests/hw/cholesky` 为例：

```
cholesky/
├── kernel/                      # DUT（被综合的顶层）
│   ├── kernel_cholesky.hpp      # extern "C" DUT 声明
│   └── kernel_cholesky_0.cpp    # DUT 实现：实例化 xf::solver::cholesky
├── host/                        # testbench（仅仿真）
│   ├── test_cholesky.hpp        # 类型 traits、误差阈值定义
│   └── test_cholesky.cpp        # main：读数据→调 DUT→比对
├── datas/                       # 预生成的测试矩阵（A 与 LAPACK 参考 L）
├── float_arch1/                 # 一个「配置点」：float + ARCH1
│   ├── description.json         # 元数据：flow/topfunction/cflags/平台
│   ├── Makefile                 # make run TARGET=csim 入口
│   ├── hls_config.tmpl          # hls_config.cfg 模板
│   └── dut_type.hpp             # 本配置点的数据类型定义
├── fixed_arch0/ fixed_arch1/ ... # 其它「类型×架构」配置点
└── complex_float_arch0/ ...
```

关键约定：**一个「数学内核」对应多个「配置点子目录」**（`float_arch1`、`fixed_arch0`、`complex_float_arch2`…），每个子目录用 `-D` 宏钉死数据类型与架构（如 `-DMATRIX_DIM=3 -DSEL_ARCH=1`），共享同一份 `kernel/` 与 `host/`。这样新增一个「类型×架构」组合只需加一个配置点，不必复制内核代码。

#### 4.4.2 核心流程

1. **构建**：在某个配置点目录（如 `float_arch1`）执行 `make run TARGET=csim`，Makefile（与 utils 同源）把它翻译成 `vitis-run --mode hls`（csim 不走 `v++ -c`，复习 u2-l3）。
2. **testbench 运行**：`main` 从 `datas/` 读入预生成矩阵 \(A\) 与 LAPACK 参考结果 \(L_{\text{expected}}\)，把 \(A\) 写进流，调 DUT `kernel_cholesky_0`，取回 \(L\)。
3. **判据**：把 DUT 的 \(L\) 与参考比对，计算「误差比」`DUT_ratio`；若 `DUT_ratio > ratio_threshold`（默认 30.0）则 FAIL。
4. **输出**：打印 `TB:Pass` 或 `TB:Fail`，返回码 0/1。

注意：浮点/定点分解**不是 bit 精确**的（HLS 用有限位宽，LAPACK 用 double），所以判据是「误差比是否超过阈值」而非「完全相等」，这与 u6-l1 FFT 的 SNR 判据同源。

#### 4.4.3 源码精读

**(a) DUT 声明（`extern "C"`）**：

[_solver/L1/tests/hw/cholesky/kernel/kernel_cholesky.hpp:24-26_](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/tests/hw/cholesky/kernel/kernel_cholesky.hpp#L24-L26)：`DIM`/`LOWER_TRIANGULAR` 由宏 `MATRIX_DIM`/`MATRIX_LOWER_TRIANGULAR` 钉死，`extern "C"` 关闭 name mangling，函数名 `kernel_cholesky_0` 与 `description.json` 的 `topfunction` 对齐。

**(b) DUT 实现：实例化模板内核**：

[_solver/L1/tests/hw/cholesky/kernel/kernel_cholesky_0.cpp:20-28_](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/tests/hw/cholesky/kernel/kernel_cholesky_0.cpp#L20-L28)：定义 `my_cholesky_traits` 继承 `choleskyTraits` 并覆盖 `ARCH=SEL_ARCH`；`kernel_cholesky_0` 直接调 `xf::solver::cholesky<...>`，返回其成功/失败码。这正是 u3-l1 讲的「把模板内核包成固定签名 DUT」。

**(c) testbench 主流程**：

[_solver/L1/tests/hw/cholesky/host/test_cholesky.cpp:183-199_](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/tests/hw/cholesky/host/test_cholesky.cpp#L183-L199)：把 \(A\) 写进 `matrixAStrm`，调 `kernel_cholesky_0(matrixAStrm, matrixLStrm)`，再从 `matrixLStrm` 读回 \(L\)。`main` 开头（37-48 行）解析命令行：`-num_tests`、`-ratio_threshold`、`-mat_type` 等。

**(d) PASS/FAIL 判据与输出**：

[_solver/L1/tests/hw/cholesky/host/test_cholesky.cpp:271-275_](https://github.com/Xilinx/Vitis_Libraries/blob/629b2c979f65561f07e4e87b860f306cb480895e/solver/L1/tests/hw/cholesky/host/test_cholesky.cpp#L271-L275)：`DUT_ratio > ratio_threshold` ⇒ `pass_fail=1`。最终在 337-341 行打印 `TB:Pass`/`TB:Fail` 并返回。默认阈值 30.0 在 44 行定义（注释说明取自 LAPACK 测试集 `s/c/xtest.in`）。

#### 4.4.4 代码实践（完整可运行）

**目标**：在 `solver/L1/tests/hw/cholesky/float_arch1` 跑通 Cholesky 的 csim，并验证「非正定矩阵会让内核返回失败码」。

**前置**：已按 u2-l1 `source` 好 Vitis/XRT 环境，`vitis-run --version` 可用。

**步骤**：
1. 进入配置点目录：
   ```bash
   cd solver/L1/tests/hw/cholesky/float_arch1
   ```
2. 跑软件仿真（csim 不综合，最快）：
   ```bash
   make run TARGET=csim PLATFORM='vck190.*'
   ```
   （`PLATFORM` 仅用于 `check_part` 推导 FPGA part；csim 实际不上板，若没有平台可改用 `XPART=xczu19eg-ffvc1760-2-i-e` 之类直接给 part。）
3. 在输出尾部找 `TB:Pass` 或 `TB:Fail`。
4. **观察矩阵性质要求**：打开 `host/test_cholesky.cpp`，确认 `kernel_cholesky_0` 的返回值（成功/失败码）会被 `main` 捕获（DUT 返回 1 即「开方遇到负数」，对应输入非正定）。`datas/` 下的输入都是 LAPACK 生成的正定矩阵，故正常情形应 `TB:Pass`。

**需要观察的现象**：
- 终端打印 `RESULTS_TABLE,...` 与 `SUMMARY_TABLE,...`，最后是 `TB:Pass`。
- 若把 `datas/float/` 下的输入矩阵手动改成一个非正定矩阵（如对角填负数），重跑应看到 Cholesky 返回 1，并在 `debug>0` 时打印 `ERROR: Trying to find the square root of a negative number`（cholesky.hpp:340）。

**预期结果**：正定输入 → `TB:Pass`，返回码 0；非正定输入 → DUT 返回 1（分解失败）。

> 说明：本机环境不一定安装了 Vitis，故上述运行结果标注为**待本地验证**。若无工具链，可退化为「源码阅读型实践」：跟随 (a)–(d) 的行号通读 DUT 与 testbench，口述数据从 `datas/` 经流进入内核、再回到比对函数的完整路径。

#### 4.4.5 小练习与答案

**练习 1**：为什么 solver 要为同一个 Cholesky 内核提供 `float_arch0/1/2`、`fixed_arch0/1/2`、`complex_*` 等十几个配置点？
**答**：Cholesky 是模板，数据类型（float/fixed/complex）与架构（ARCH 0/1/2）是两个正交维度；每个配置点用宏钉死一组组合，既能逐组验证功能与资源，又共享同一份 `kernel/`+`host/` 代码，避免重复。

**练习 2**：csim 阶段能否得到 II、latency、资源估计？若不能，该用哪个 TARGET？
**答**：不能。csim 只验证功能（C 仿真）。要得到 II/latency/资源需跑 `csynth`（复习 u2-l3），对应 `make run TARGET=csynth`。

**练习 3**：testbench 用「误差比阈值」而非「bit 精确」判 PASS/FAIL，原因是什么？
**答**：HLS 内核用有限位宽（float 或 ap_fixed），LAPACK 参考用 double，二者本就不可能 bit 精确；只要数值误差在可控阈值内即视为功能正确，与 FFT 的 SNR 判据同思路。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个小调研任务：

**任务：绘制 solver 稠密分解的「内核→组合→数据类型」三视图地图。**

1. **分解视图**：列出 `solver/L1/include/hw/` 下所有「分解类」入口函数（`cholesky`、`qrf`、`qrd`/`qrd_ip_ncu_top`、`svd`、以及 `cholesky_cfloat`、`block_cholesky_cfloat`），各写一行「数学关系 + 输入矩阵要求」。
2. **组合视图**：画出 `choleskyInverse` 与 `qrInverse` 的三段 DATAFLOW 数据流图（分解 → 回代 → 矩阵乘），标出每段调用的子内核与段间 stream。
3. **数据类型视图**：用一个表格，对每个内核勾选它支持的数据类型（float / ap_fixed / complex / cfloat），并注明是否走 traits 特化。

完成后，你应当能用一句话回答：「给定一个一般实矩阵，要求其逆，应走哪条调用链？给定一个对称正定矩阵呢？」
（参考答案：一般矩阵 → `qrInverse`（`qrf` + `backSubstitute` + `matrixMultiply`）；对称正定矩阵 → `choleskyInverse`（`cholesky` + `backSubstitute` + `matrixMultiply`），更快更省。）

## 6. 本讲小结

- solver 库的 L1 稠密分解内核遵循统一写法：**流式入口 + traits 类型结构体 + ARCH 架构选择 + 失败返回码**。
- **Cholesky** 要求埃尔米特/对称正定，其「正定性检查」就实现在 `cholesky_sqrt_op` 对负数开方返回 1；**QR**（`qrf`/`qrd`）适用于一般矩阵；**SVD**（`svd`，Jacobi）输出 S/U/V 三路。（README 提到的 LU 在当前 L1 头件中未见独立实现，待确认。）
- **三角求解 `backSubstitute`** 是求逆的关键拼图；`choleskyInverse`/`qrInverse` 用 `DATAFLOW` 把「分解 → 回代 → 矩阵乘」串成任务级流水。
- 分解对角元恒为实数这一数学性质，被用来把复数回代的「对角倒数」降级为实数除法，省 DSP。
- 数据类型分三条线：`float`（默认 traits）、`ap_fixed`（traits 放大位宽）、`cfloat`（专用变体如 `cholesky_cfloat`、多 CU 的 `qrd`）。
- L1 测试用「配置点子目录 × 共享 kernel/host」组织，csim 以「误差比阈值」判 PASS/FAIL，不是 bit 精确。

## 7. 下一步学习建议

- **进入 AIE 路线**：本讲全是 PL（HLS）内核。solver 同样有 AIE 实现，见下一讲 u7-l2（solver AIE 内核与 L2 基准），阅读 `solver/L1/include/aie/cholesky.hpp`、`qrd.hpp`、`qrd_kernel.hpp`，对比 PL 与 AIE 两套分解实现的差异。
- **深入性能**：跑一次 `csynth`（u2-l3）看 Cholesky 三种架构的 II/latency/DSP，验证 4.1.4 的预判；再结合 u12-l1（dataflow/SSR/II 调优）理解 `qrd` 的 NCU 如何线性扩展吞吐。
- **横向对照**：对照 u6-l1（DSP 库的 HLS FFT）与 u8-l1（BLAS 的 GEMM），体会 Vitis 各库「流式入口 + traits + 多架构」这一共通设计范式。
- **阅读参考**：`solver/README.md` 的 benchmark 链接给出 Alveo U200/U250 上的实测性能，可作为「这些 HLS 内核最终跑多快」的对照。
