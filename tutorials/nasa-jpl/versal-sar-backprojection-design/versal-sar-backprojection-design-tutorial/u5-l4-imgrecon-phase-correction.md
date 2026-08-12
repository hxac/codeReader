# 图像重建(二)：相位校正与 sincos

## 1. 本讲目标

本讲是「图像重建内核」三讲的中篇。上一讲（u5-l3）我们已经把每个目标像素到天线的**差分距离** \(\Delta R\) 算了出来，并换算成 `rc_in` 缓冲里的采样索引。本讲要回答下一个问题：**拿到 \(\Delta R\) 之后，如何把它变成一个复数相位校正项，去「拨正」距离压缩样本的相位，使来自真实目标的回波在跨脉冲累加时同相增强？**

具体来说，学完本讲你应该能够：

1. 说清相位校正系数 `ph_corr_coef = (4·π·MIN_FREQ)/C` 的物理来源——它为何是「双程相位波数」。
2. 明白为什么这个系数乘上 \(\Delta R\) 后得到的相位角动辄高达成千上万弧度，**必须折叠**到 \([-\pi,\pi]\) 才能喂给 AIE 的 `sincos_complex`。
3. 逐行读懂源码里「用 `INV_TWO_PI` 相乘 + 减 0.5 + `to_fixed` 取整」实现 `floor`、再减去整数倍 \(2\pi\) 的折叠技巧，并解释末尾那一句 `aie::neg` 为何不可或缺。
4. 能在纸上对一个像 \(100\) 弧度这样的大角度手动走完整个折叠流程，并指出不做折叠会让 `sincos_complex` 饱和、从而毁掉整幅图像。

---

## 2. 前置知识

本讲默认你已经掌握以下内容（来自前序讲义）：

- **反投影与相干累加（u1-l1）**：反投影对每个像素、每个脉冲都算一次双程时延，相位校正后做相干累加，真实目标同相增强、他处抵消，最终聚焦成图像。
- **差分距离 \(\Delta R\)（u5-l3）**：`img_reconstruct_kern` 用 `aie::sub/mul_square/sqrt` 对 16 个像素并行算出像素到天线的几何距离，再减去场景中心参考距离 `r0`，得到 `differ_range_vec`。本讲正是要把这个 `differ_range_vec` 变成复数相位。
- **AIE 向量运算（u2-l2 / u5-l3）**：`aie::vector<float,16>` 的 SIMD 运算、`aie::mul`、`aie::to_fixed<int32>`（按四舍五入取整）、累加器 `.to_vector<float>(0)` 的写法。

两个本讲要新用到的复数与三角知识，先做最简说明：

- **复指数与相位**：一个复数相位校正项写作 \(e^{i\theta}=\cos\theta+i\sin\theta\)。把它乘到一个复数样本上，等于「保持幅度不变、把相位旋转 \(\theta\)」。AIE 用 `cfloat`（实部 + 虚部各一个 float）存这种复数。
- **三角函数的周期性**：\(\sin\)、\(\cos\) 以 \(2\pi\) 为周期，即 \(\sin(\theta)=\sin(\theta+2k\pi)\)。所以任意大角度都可以「减掉若干个 \(2\pi\)」化到一个等价的小角度，函数值不变。这是本讲折叠操作的全部数学基础。

---

## 3. 本讲源码地图

本讲只精读一个文件里、一段紧挨在一起的代码：

| 文件 | 作用 |
| --- | --- |
| [design/aie/backprojection.cc](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc) | 反投影三类内核的实现。本讲聚焦 `ImgReconstruct::img_reconstruct_kern` 中「**CALCULATE PHASE CORRECTION FOR IMAGE**」那一块。 |
| [design/common.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h) | 三域共享的雷达物理常数（`PI`、`TWO_PI`、`INV_TWO_PI`、`C`、`MIN_FREQ`），是相位系数的来源。 |
| [design/aie/custom_kernels.h](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h) | `ImgReconstruct` 类声明，相位校正的最终乘积会累加到成员 `m_img`。 |

> 提示：本讲的相位校正块在内核里位于「差分距离/索引计算（u5-l3）」之后、「线性插值与累加（u5-l5）」之前。差分距离向量 `differ_range_vec` 在此处被**第二次复用**——第一次（u5-l3）用来算采样索引，第二次（本讲）用来算相位角。

---

## 4. 核心概念与源码讲解

### 4.1 相位校正系数 ph_corr_coef

#### 4.1.1 概念说明

雷达发射的信号碰到目标后返回，回波的相位里携带了「电磁波走了一个来回」的路径信息。设目标到天线的单程距离为 \(R\)，则双程路径为 \(2R\)。在载波频率 \(f_c\) 下，这段双程路径对应的相位为

\[
\phi = \frac{2\pi f_c}{c}\cdot (2R) = \frac{4\pi f_c}{c}\cdot R .
\]

其中的系数 \(\frac{4\pi f_c}{c}\) 就是**双程相位波数**：每走 1 米，相位变化多少弧度。

反投影并不直接用绝对距离 \(R\)，而是把一切参考到**场景中心** `r0`（即 slowtime 里第 4 个量 `ref_range`）。真正决定像素能否聚焦的是**差分距离** \(\Delta R = R - r_0\)。于是对齐相位的相位校正角就是

\[
\theta = \frac{4\pi f_c}{c}\cdot \Delta R .
\]

在仓库里，`f_c` 取的是频带的起始频率 `MIN_FREQ`（X 波段，约 9.288 GHz，对应 GOTCHA 数据集），所以源码把这个系数命名为 `ph_corr_coef` 并写成 \((4\cdot\pi\cdot\texttt{MIN\_FREQ})/C\)。

#### 4.1.2 核心流程

1. 在内核开头一次性算出常数 `ph_corr_coef`（标量，全程不变）。
2. 在 16 像素的主循环里，用 SIMD 乘法 `ph_corr_coef × differ_range_vec`，**同时**得到 16 个像素各自的相位角向量 `ph_corr_angle_vec`。
3. 这个角度向量接下来要交给 4.2 的折叠、4.3 的 `sincos_complex`，最终变成 16 个 `cfloat` 相位校正项。

#### 4.1.3 源码精读

系数在内核入口处算出（`C` 是光速，`MIN_FREQ` 是载频起始点）：

```cpp
// Initialize radar params
float ph_corr_coef = (4*PI*MIN_FREQ)/C;
```

[design/aie/backprojection.cc:89-90](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L89-L90) — 计算双程相位波数 `ph_corr_coef`，把差分距离换算成相位角。

这些常数都来自三域共享头 `common.h`：

```cpp
static constexpr float PI = 3.1415926535898;
static constexpr float TWO_PI = 6.2831853071796;
static constexpr float INV_TWO_PI = 0.1591549430919; // Used in AIE code only
...
static constexpr float C = 299792458.0;
static constexpr float MIN_FREQ = 9288080400.0;      // Used in AIE code only
```

[design/common.h:48-54](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/common.h#L48-L54) — `PI`、`TWO_PI`、`INV_TWO_PI`、`C`、`MIN_FREQ` 五个常数；注释标明 `INV_TWO_PI` 与 `MIN_FREQ` 仅在 AIE 代码里使用，正是本讲折叠流程要用的。

代值估算一下这个系数有多大：

\[
\texttt{ph\_corr\_coef} = \frac{4\pi \times 9.288\times10^{9}}{2.998\times10^{8}} \approx 389.2\ \text{rad/m}.
\]

这是一个**极大**的数——每米差分距离对应约 389 弧度，即约 62 个 \(2\pi\) 周期。结合 u1-l4 里距离窗宽约 \(102\) m，场景边缘像素的 \(\Delta R\) 可达数十米，相位角轻松冲到**上万弧度**。这就直接引出下一节的问题：AIE 的三角函数根本吃不下这么大的输入。

#### 4.1.4 代码实践

**实践目标**：建立「差分距离 → 相位角」的量级直觉。

**操作步骤**：

1. 打开 `design/common.h`，确认 `C`、`MIN_FREQ`、`PI` 的值。
2. 用计算器或 Python 算出 `ph_corr_coef ≈ 389.2 rad/m`。
3. 取一个场景边缘像素，假设 \(\Delta R = 20\) m，算相位角 \(\theta = 389.2 \times 20 \approx 7784\) rad，再除以 \(2\pi\) 看它绕了多少圈。

**需要观察的现象**：即便 \(\Delta R\) 只有几米，\(\theta\) 也远超 \(\pi\)；这说明折叠不是「边缘情况」，而是**每个像素都必须做**的常规步骤。

**预期结果**：\(\Delta R=20\) m 时 \(\theta\approx7784\) rad，约 \(1238\) 个 \(2\pi\) 圈。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `MIN_FREQ` 改小一半，`ph_corr_coef` 会怎么变？折叠操作还需要吗？
**答案**：`ph_corr_coef` 减半（约 194.6 rad/m），但仍然远大于 \(\pi\)，所以折叠依然必须——只是每个像素要减的 \(2\pi\) 圈数变少。

**练习 2**：为什么系数里是 \(4\pi\) 而不是 \(2\pi\)？
**答案**：因为雷达波要**走一个来回**（发射→目标→接收），双程路径是 \(2R\)，所以相位前的因子是 \(\frac{2\pi f_c}{c}\times 2 = \frac{4\pi f_c}{c}\)。

---

### 4.2 把大相位角折叠进 \([-\pi,\pi]\)

#### 4.2.1 概念说明

AIE 的三角函数指令 `aie::sincos_complex` 有一个硬性限制：**输入角度必须在 \([-\pi,\pi]\) 之间**，否则输出会被钳位（saturate）到 \(\pm1\)。这一点项目文档写得很明确：

> the AI Engine API `sincos_complex` function only works if the domain is between \(-\pi\) and \(\pi\) … the output of the function will be saturated to -1 or 1 depending on if the input value is below \(-\pi\) or above \(\pi\) respectively.

（见 [doc/sections/implementation.tex:550-556](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/doc/sections/implementation.tex#L550-L556)。）

而 4.1 算出的 `ph_corr_angle_vec` 动辄上万弧度，直接喂进去会让所有像素的相位校正都变成同一个饱和值，整幅图像立刻报废。好在 \(\sin/\cos\) 以 \(2\pi\) 为周期，我们只要把大角度「减去若干个整 \(2\pi\)」，就能化到一个等价的小角度，三角函数值完全不变。

数学上，对任意角 \(\theta\)，令 \(N=\lfloor \theta/(2\pi)\rfloor\)，则

\[
\theta' = \theta - 2\pi N - \pi \in [-\pi,\pi),
\]

且 \(\theta' \equiv \theta - \pi \pmod{2\pi}\)。注意这里多减了一个 \(\pi\)，是为了把原本落在 \([0,2\pi)\) 的结果**居中**到对称区间 \([-\pi,\pi)\)；它带来的副作用（一个 \(-\pi\) 的相移）留到 4.3 用一句 `aie::neg` 修掉。

#### 4.2.2 核心流程

AIE 向量指令里**没有取模、也没有现成的 `floor`**，所以源码用一个经典技巧实现「减去整数倍 \(2\pi\)」：

```
对 16 个像素并行：
  q          = θ / (2π)              # 用乘 INV_TWO_PI 实现
  N          = floor(q)               # 用「减 0.5 + to_fixed 四舍五入」实现
  θ'         = θ - 2π·N - π           # 减去 N 个整圈，再居中
```

伪代码里的关键是 `N = floor(q)` 的实现。`aie::to_fixed<int32>` 默认按**四舍五入到最近整数**取整；先减 0.5 再取整，整体效果恰好等价于 `floor`：

\[
\texttt{to\_fixed}(q - 0.5) = \lfloor q \rfloor .
\]

> 这个「减 0.5 再 `to_fixed`」的小花招，和上一讲 u5-l3 里算插值下界索引 `low_idx_int_vec` 用的是**同一个写法**（见 [design/aie/backprojection.cc:145-147](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L145-L147)）。那里变量名 `low_idx`（插值下界）也印证了它的语义就是 `floor`。

#### 4.2.3 源码精读

折叠块紧跟在相位角计算之后，注释也写明了意图：

```cpp
//**** CALCULATE PHASE CORRECTION FOR IMAGE ****//
auto ph_corr_angle_vec = aie::mul(ph_corr_coef, differ_range_vec).to_vector<float>(0);

// Figure out the number of times 2*PI goes into ph_corr_angle_vec.
// Floor round to neg infinity by casting to int32, then back to float
// for later operations
auto num_pi_wrapped_acc = aie::mul(INV_TWO_PI, ph_corr_angle_vec);
auto num_pi_wrapped_floor_vec = aie::sub(num_pi_wrapped_acc, 0.5f).to_vector<float>(0);
auto num_pi_wrapped_int_vec = aie::to_fixed<int32>(num_pi_wrapped_floor_vec); // Round to nearest whole number
num_pi_wrapped_floor_vec = aie::to_float(num_pi_wrapped_int_vec);

// Scale down ph_corr_angle to be within valid domain for sin/cos
// operation (must be between -PI to PI; modulus doesn't exist)
auto scale_down_angle_acc = aie::negmul(TWO_PI, num_pi_wrapped_floor_vec);
scale_down_angle_acc = aie::sub(scale_down_angle_acc, PI);
ph_corr_angle_vec = aie::add(scale_down_angle_acc, ph_corr_angle_vec).to_vector<float>(0);
```

[design/aie/backprojection.cc:149-164](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L149-L164) — 相位角折叠：算出整数圈数 \(N\)，再从 \(\theta\) 里减去 \(2\pi N+\pi\)，把角度规整进 \([-\pi,\pi]\)。

逐行对应到 4.2.2 的伪代码：

| 源码行 | 作用 | 对应符号 |
| --- | --- | --- |
| `aie::mul(ph_corr_coef, differ_range_vec)` | 差分距离 × 相位系数 → 原始相位角（复用 u5-l3 的 `differ_range_vec`） | \(\theta\) |
| `aie::mul(INV_TWO_PI, ph_corr_angle_vec)` | 乘 \(1/(2\pi)\) → 圈数（浮点） | \(q=\theta/(2\pi)\) |
| `aie::sub(..., 0.5f)` + `aie::to_fixed<int32>` | 减 0.5 再四舍五入取整 → 整数圈数 | \(N=\lfloor q\rfloor\) |
| `aie::to_float(num_pi_wrapped_int_vec)` | 整数圈数转回浮点，供后续浮点乘法用 | \(N\)（float） |
| `aie::negmul(TWO_PI, N)` | 负向乘 \(2\pi\) → \(-2\pi N\) | \(-2\pi N\) |
| `aie::sub(..., PI)` | 再减 \(\pi\) → \(-2\pi N-\pi\) | 居中偏移 |
| `aie::add(..., ph_corr_angle_vec)` | 加回原始角 → 折叠后的角 | \(\theta'=\theta-2\pi N-\pi\) |

注意作者特意用 `negmul`（负乘）一步算出 \(-2\pi N\)，省掉一次取负；并选择 \([-\pi,\pi]\) 这个**对称区间**而不是 \([0,2\pi)\)，因为 `sincos_complex` 的有效域就是它。

#### 4.2.4 代码实践

**实践目标**：手动验证「减 0.5 + `to_fixed` = `floor`」这一技巧，并对一个大角度走完折叠。

**操作步骤**（纸笔即可）：

1. 取 \(\theta = 100\) rad。
2. 算 \(q=\theta/(2\pi)=100/6.283185\approx 15.9155\)。
3. 按源码方式算 \(N\)：\(q-0.5=15.4155\)，四舍五入到最近整数 \(=15\)。这正是 \(\lfloor 15.9155\rfloor=15\)。
4. 算折叠角 \(\theta'=\theta-2\pi N-\pi=100-2\pi\cdot15-\pi\approx 100-94.2478-3.1416\approx 2.6106\) rad。
5. 确认 \(2.6106\in[-\pi,\pi)\)（\(\pi\approx3.1416\)）。

**需要观察的现象**：步骤 3 里「减 0.5 再四舍五入」得到 15，与直接 `floor` 完全一致；步骤 4 的结果确实落在 \([-\pi,\pi)\) 内。

**预期结果**：\(\theta=100\) rad → \(N=15\) → \(\theta'\approx 2.6106\) rad（约 \(149.6^\circ\)）。

#### 4.2.5 小练习与答案

**练习 1**：取 \(\theta=-5.0\) rad，走一遍折叠，得到 \(\theta'=?\)。
**答案**：\(q=-5/2\pi\approx -0.7958\)；\(q-0.5=-1.2958\)，四舍五入 \(=-1=N\)；\(\theta'=-5-2\pi(-1)-\pi=-5+6.2832-3.1416\approx -1.8584\) rad，落在 \([-\pi,\pi)\) 内。✓

**练习 2**：如果源码忘了减那个 `0.5f`（即直接 `to_fixed(q)`），对 \(\theta=100\) 会得到什么 \(N\)？结果还正确吗？
**答案**：`to_fixed(15.9155)` 四舍五入 \(=16\)，而正确的 `floor` 应为 15。代入会得到 \(\theta'=100-2\pi\cdot16-\pi\approx -3.673\) rad，虽然仍落在 \([-\pi,\pi)\) 外（约 \(-3.673<-\pi\)），会触发饱和。可见那个 `0.5f` 不是装饰，而是把「四舍五入」校正为 `floor` 的关键。

**练习 3**：为什么作者用乘 `INV_TWO_PI` 而不是除 `TWO_PI`？
**答案**：AIE 向量单元没有高效的除法指令；乘以预先算好的倒数 \(1/(2\pi)\) 要快得多。`common.h` 注释也标明 `INV_TWO_PI` 是「Used in AIE code only」。

---

### 4.3 sincos_complex 的域限制、饱和与「取负还原」

#### 4.3.1 概念说明

折叠之后，角度向量 `ph_corr_angle_vec` 已经安全落在 \([-\pi,\pi]\)，可以喂给 `aie::sincos_complex` 了。这个函数对 16 个角度并行计算，返回一个「实部放 \(\cos\)、虚部放 \(\sin\)」的 `cfloat` 向量，即每个元素等于 \(e^{i\theta'}=\cos\theta'+i\sin\theta'\)。

但这里藏着一个**容易看漏的细节**。4.2 的折叠公式是 \(\theta'=\theta-2\pi N-\pi\)，多减了一个 \(\pi\)。由欧拉公式 \(e^{i(\theta-\pi)}=e^{i\theta}\cdot e^{-i\pi}=e^{i\theta}\cdot(-1)=-e^{i\theta}\)，所以

\[
\texttt{sincos\_complex}(\theta') = e^{i\theta'} = -e^{i\theta}.
\]

也就是说，为了让角度落进对称区间 \([-\pi,\pi]\) 而引入的 \(-\pi\) 偏移，会让结果整体多出一个负号。源码紧接着用一句 `aie::neg(ph_corr_vec)` 把这个负号抵消掉，最终拿到的才是真正想要的相位校正项 \(e^{i\theta}=e^{i\cdot\texttt{ph\_corr\_coef}\cdot\Delta R}\)。

> 为什么非要引入这个 \(-\pi\)、再多写一句 `neg`？因为 `sincos_complex` 的有效域恰好是对称的 \([-\pi,\pi]\)，而不是 \([0,2\pi)\)。若直接折叠到 \([0,2\pi)\)，超过 \(\pi\) 的那一半角度照样会饱和。所以「对称折叠 + 取负还原」是适配这条硬件限制的最省事写法。

另一个必须强调的点：**不做折叠会怎样？** 若把原始的 \(\theta\)（比如 100 rad）直接喂给 `sincos_complex`，因为 \(100>\pi\)，硬件会把输出钳位（饱和）。文档说的「saturated to 1」意味着无论输入多大，输出都被钉死成同一个值——于是**所有像素拿到完全相同的相位校正**，反投影的相位对齐彻底失效，相干累加变成乱码，图像无法聚焦。

#### 4.3.2 核心流程

```
θ'  ∈ [-π, π]                  # 4.2 的输出，安全域内
ph_corr_vec = sincos_complex(θ')   # 并行算 16 个 (cosθ', sinθ')，即 e^{iθ'} = -e^{iθ}
ph_corr_vec = neg(ph_corr_vec)     # 抵消 -π 偏移带来的负号 → e^{iθ}
# 结果：每个像素一个 cfloat 相位校正项 e^{i·ph_corr_coef·ΔR}
```

这个 `ph_corr_vec` 随后会被 u5-l5 的插值循环拿来与距离压缩样本相乘（`interp * ph_corr_vec.get(px_idx)`），再累加进 `m_img`。

#### 4.3.3 源码精读

```cpp
// Calculate the sin and cos of ph_corr_angle and store as a cfloat
// (cos in the real part, sin in the imaginary)
auto ph_corr_vec = aie::sincos_complex(ph_corr_angle_vec);
ph_corr_vec = aie::neg(ph_corr_vec);
auto ph_corr_real = aie::real(ph_corr_vec);
auto ph_corr_imag = aie::imag(ph_corr_vec);
```

[design/aie/backprojection.cc:166-171](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L166-L171) — 调用 `sincos_complex` 算出复数相位（实部 \(\cos\)、虚部 \(\sin\)），随即取负抵消折叠时的 \(-\pi\) 偏移。

对照代码注释：

- 第 167-168 行注释点明返回格式：**实部存 \(\cos\)、虚部存 \(\sin\)**，正好是 \(e^{i\theta'}\) 的复数表示。
- `aie::neg` 对整个 `cfloat` 向量取负（实部、虚部同时反号），把 \(-e^{i\theta}\) 还原成 \(e^{i\theta}\)。
- 拆出的 `ph_corr_real` / `ph_corr_imag` 在当前内核里其实**未被后续直接使用**（真正的消费在 u5-l5 通过 `ph_corr_vec.get(px_idx)` 完成），这里属于「为调试/扩展预拆」，但 `ph_corr_vec` 本身会进入下一步。

最终相位校正的归属：它会被插值后的距离压缩样本相乘，再累加到跨调用持久存在的成员 `m_img`：

```cpp
alignas(aie::vector_decl_align) cfloat m_img[(PULSES*RC_SAMPLES)/IMG_SOLVERS];
```

[design/aie/custom_kernels.h:40-41](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/custom_kernels.h#L40-L41) — 每个重建内核持有的累加缓冲 `m_img`；相位校正后的逐像素贡献会跨 602 个脉冲不断累加进来，直到末脉冲由 RTP 触发 dump。

#### 4.3.4 代码实践

**实践目标**：亲手验证「折叠 + 取负」确实还原出正确的 \(e^{i\theta}\)，并体会不做折叠的后果。

**操作步骤**（纸笔 + 可选 Python/NumPy 核对）：

1. 沿用 4.2 的 \(\theta=100\) rad，\(\theta'\approx 2.6106\) rad。
2. 算 \(e^{i\theta'}=\cos(2.6106)+i\sin(2.6106)\approx -0.8624+0.5060\,i\)。
3. 取负得 \(0.8624-0.5060\,i\)。
4. 直接算 \(e^{i\theta}=e^{i\cdot 100}=\cos(100)+i\sin(100)\approx 0.8624-0.5060\,i\)，与步骤 3 完全一致。✓
5. 反过来想：若**不折叠**，把 100 直接喂给 `sincos_complex`，按文档会被饱和到 1（即 \(1+0i\)），与正确值 \(0.8624-0.5060\,i\) 毫无关系。

可选 NumPy 核对：

```python
import numpy as np
theta = 100.0
TWO_PI = 6.2831853071796
N = np.floor(theta / TWO_PI)        # 15.0
theta_p = theta - TWO_PI*N - np.pi  # 2.6106...
folded = np.exp(1j*theta_p)         # -0.8624+0.5060j
corrected = -folded                 # 0.8624-0.5060j  == np.exp(1j*theta)
print(corrected, np.exp(1j*theta))
```

**需要观察的现象**：步骤 3 与步骤 4 的复数逐位相等；步骤 5 的饱和值与正确值截然不同。

**预期结果**：折叠+取负后 \(0.8624-0.5060\,i\)，等于 \(e^{i\cdot100}\)；不折叠则被钉死成饱和值，相位信息全丢。

#### 4.3.5 小练习与答案

**练习 1**：若删掉 `aie::neg(ph_corr_vec)` 这一句，最终相位校正会变成什么？图像会怎样？
**答案**：会变成 \(-e^{i\theta}\)，每个像素的相位都差了一个 \(\pi\)（整体反相）。相干累加时真实目标会变成**反相抵消**、本该抵消的地方反而增强，图像对比度反转、聚焦失败。

**练习 2**：为什么 `sincos_complex` 要把 \(\cos\) 放实部、\(\sin\) 放虚部，而不是反过来？
**答案**：因为这样返回的 `cfloat` 直接就是欧拉公式里的 \(e^{i\theta}=\cos\theta+i\sin\theta\)，作为一个复数可以直接与距离压缩样本（也是 `cfloat`）做复数乘法，完成「幅度不变、相位旋转 \(\theta\)」的相位校正，无需额外整理。

**练习 3**：折叠到 \([0,2\pi)\)（不减那个 \(\pi\)、也不取负）听起来更简洁，为什么本项目不这么做？
**答案**：因为 `sincos_complex` 的有效输入域是对称的 \([-\pi,\pi]\)；\([0,2\pi)\) 里凡是大于 \(\pi\) 的角度都会被钳位饱和。只有折叠到 \([-\pi,\pi]\) 才能保证所有像素都不触发饱和，而「减 \(\pi\) 居中 + 取负还原」就是为此付出的最小代价。

---

## 5. 综合实践

把本讲三块内容串起来，完成一次「**从差分距离到相位校正项**」的完整纸面推演，并对照源码核对每一步。

**任务**：给定一个像素，其差分距离 \(\Delta R = 0.008\) m（约 8 毫米，靠近场景中心的小差分距离）。

1. 用 `ph_corr_coef ≈ 389.2` rad/m 算原始相位角 \(\theta\)。
2. 按 4.2 的折叠流程算 \(N\) 与 \(\theta'\)，确认 \(\theta'\in[-\pi,\pi)\)。
3. 按 4.3 算 `sincos_complex(θ')` 再取负，得到最终复数相位校正项，并用 \(e^{i\theta}\) 直接验证。
4. 打开 [design/aie/backprojection.cc:149-171](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L149-L171)，把你手算的每一步对应到具体那一行源码上（`mul` → `INV_TWO_PI` → `sub 0.5` → `to_fixed` → `negmul` → `sub PI` → `add` → `sincos_complex` → `neg`）。
5. 回答收尾问题：为什么即便 \(\Delta R\) 只有 8 毫米，折叠仍然不可省略？把你的 \(\theta\) 与 \(\pi\) 比一下即可。

**参考答案**：
- \(\theta = 389.2\times0.008 \approx 3.1136\) rad（已经接近 \(\pi\)！）。
- \(q=3.1136/2\pi\approx0.4956\)；\(N=\lfloor0.4956\rfloor=0\)；\(\theta'=3.1136-0-\pi\approx -0.0280\) rad \(\in[-\pi,\pi)\) ✓。
- \(e^{i\theta'}=e^{-0.0280i}\approx0.9996-0.0280i\)；取负后 \(-0.9996+0.0280i\)… 但等等——这里 \(N=0\)，折叠公式 \(\theta'=\theta-2\pi\cdot0-\pi=\theta-\pi\)，取负后 \(-e^{i(\theta-\pi)}=-(-e^{i\theta})=e^{i\theta}=e^{i\cdot3.1136}\approx -0.9996+0.0280i\) ✓，与直接算 \(e^{i\cdot3.1136}\) 一致。
- 收尾：\(\theta\approx3.11\) rad 已几乎贴着 \(\pi\approx3.14\) 的边界；只要 \(\Delta R\) 稍大就会越界饱和，所以折叠对**每一个**像素都必不可少。

---

## 6. 本讲小结

- 相位校正系数 `ph_corr_coef = (4·π·MIN_FREQ)/C` 是双程相位波数，把差分距离 \(\Delta R\) 换算成相位角 \(\theta\)；其值约 \(389\) rad/m，量级极大。
- 由于 `aie::sincos_complex` 只接受 \([-\pi,\pi]\) 内的输入（越界会饱和到 \(\pm1\)），而 \(\theta\) 常高达上万弧度，**每个像素都必须折叠**。
- 折叠靠「乘 `INV_TWO_PI` → 减 0.5 → `to_fixed` 四舍五入」实现 `floor` 得到整数圈数 \(N\)，再算 \(\theta'=\theta-2\pi N-\pi\)，把角度规整进对称区间 \([-\pi,\pi)\)。
- 折叠时多减的那个 \(\pi\) 让 `sincos_complex(θ')` 多出一个负号，源码用一句 `aie::neg` 抵消，最终得到正确的 \(e^{i\theta}\)。
- 折叠 + 取负得到的 `ph_corr_vec` 将在下一讲（u5-l5）与插值后的距离压缩样本相乘并累加进 `m_img`。
- 「减 0.5 再 `to_fixed`」这一 `floor` 技巧与 u5-l3 的插值下界索引 `low_idx` 完全同源，是 AIE 向量代码里反复出现的固定写法。

---

## 7. 下一步学习建议

本讲止于「算出每个像素的复数相位校正项 `ph_corr_vec`」。接下来：

- **u5-l5（图像重建三：插值、累加与 SIMD/流水）** 会把 `ph_corr_vec` 与 u5-l3 算出的 `low_idx/high_idx` 线性插值结果相乘，累加进跨调用持久的 `m_img`，并在末脉冲由 RTP 触发 dump；同时讲解 `chess_prepare_for_pipelining` 的流水优化。建议接着读 [design/aie/backprojection.cc:173-213](https://github.com/nasa-jpl/versal-sar-backprojection-design/blob/79534466f6ae6a84894d14eff225b4f897e5d259/design/aie/backprojection.cc#L173-L213)。
- 想从更高层理解「相位校正之后图像怎么拼回 DDR」的读者，可以预习 u6-l1（PL 包路由器如何按 `m_id` 把 224 个核的乱序输出重排进连续内存）。
- 对 `sincos_complex` / `to_fixed` 等向量内联函数的精确语义感兴趣，可查阅 AMD AI Engine 文档（aie_api）中 `sincos_complex` 的输入域说明与 `to_fixed` 的默认四舍五入模式，以印证本讲的逐行推导。
