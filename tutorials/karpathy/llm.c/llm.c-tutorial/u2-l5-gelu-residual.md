# GELU 与残差连接

## 1. 本讲目标

本讲聚焦 GPT-2 MLP（多层感知机）块里两个「最小但极其关键」的算子：**GELU 激活函数** 与 **残差连接（residual connection）**。读完本讲，你应该能够：

- 说出 GELU 的精确公式与 tanh 近似公式，并解释为什么 GPT-2 选择 tanh 近似版。
- 读懂 `train_gpt2.c` 中 `gelu_forward` / `gelu_backward` 的逐行实现，并能手推 GELU 的导数。
- 读懂 `residual_forward` / `residual_backward`，并理解残差反向为什么是「梯度分流」而不是「梯度减半」。
- 把这两个算子放回 Transformer MLP 块的上下文里，看清它们如何配合 matmul（前向）和 matmul_backward（反向）一起工作。

本讲是「前向各层」单元的第五篇，承接 [u2-l1 编码层](u2-l1-encoder-layer.md)、[u2-l2 LayerNorm](u2-l2-layernorm-layer.md)、[u2-l3 MatMul](u2-l3-matmul-layer.md)。GELU 与残差是整个前向流程里最朴素的两段代码——朴素到只有几行循环——但它们恰好是理解「Transformer 为什么能训得深」「非线性从何而来」的切入点。

---

## 2. 前置知识

阅读本讲前，建议你已经掌握：

- **前向 / 反向的基本概念**：前向是输入经过若干算子得到输出（和 loss）；反向是用链式法则把 loss 对每个参数的梯度逐层倒推回去。参见 [u1-l3](u1-l3-cpu-reference-overview.md)。
- **逐元素算子（elementwise operator）**：对张量里每个元素独立做同一个运算，比如 `out[i] = f(inp[i])`。GELU 和残差都是逐元素算子，因此实现上都是一重循环。
- **链式法则**：若 `out = f(x)`，且下游已经算出了 `dout = ∂L/∂out`，那么 `dinp = ∂L/∂x = f'(x) · dout`。本讲的两个反向函数都只是这条公式的具体化。
- **`+=` 累加与每步清零**：llm.c 的反向统一用 `+=` 把梯度累加进梯度缓冲，并依赖每个训练步开头的 `gpt2_zero_grad` 把梯度清零。这一点在前几讲反复出现，本讲依然适用。
- **C 的行主序张量与 `B*T*C` 寻址**：多维张量被拍平成一维数组，三重循环按 `b, t, c` 顺序展开。GELU/残差不关心 `B, T, C` 的具体拆分，只关心元素总数 `N = B*T*C`（或 `B*T*4C`），所以它们干脆把整块当成 `N` 个元素的一维数组处理。

**本讲引入的术语**：

| 术语 | 含义 |
|------|------|
| GELU | Gaussian Error Linear Unit，高斯误差线性单元，一种平滑的激活函数 |
| tanh 近似 | 用 `tanh` 来近似 GELU 的解析式，避免计算昂贵的 `erf` 函数 |
| 残差连接（residual） | 把某一层的输入直接加到它的输出上：`out = x + sublayer(x)` |
| 梯度分流 | 残差反向时，一份 `dout` 被原样复制给两个分支，而不是各拿一半 |

---

## 3. 本讲源码地图

本讲只涉及一个源码文件，但会引用 PyTorch 参考实现做对照。

| 文件 | 作用 | 本讲用到哪部分 |
|------|------|----------------|
| `train_gpt2.c` | 纯 C/CPU 参考实现，每层算法写得最清楚 | `gelu_forward`、`gelu_backward`、`residual_forward`、`residual_backward`，以及它们在 `gpt2_forward` / `gpt2_backward` 里的调用点 |
| `train_gpt2.py` | PyTorch 参考实现（nanoGPT 风格），当正确性标尺 | `NewGELU` 类、`MLP` 类，用于对照 GELU 的公式 |

这两个算子在 CPU 参考里都是「教科书级」的短函数，分别位于：

- GELU：`gelu_forward`（408 行起）、`gelu_backward`（422 行起）
- 残差：`residual_forward`（436 行起）、`residual_backward`（442 行起）

它们在 MLP 块里的调用点：前向在 867–872 行，反向在 993–998 行。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 GELU 激活函数**：tanh 近似的前向与反向（含手推导数）。
- **4.2 残差连接**：前向逐元素相加，反向梯度分流。
- **4.3 在 Transformer MLP 块中的组装**：把 GELU 和残差放回上下文，看清一次完整的 MLP 前向与反向。

---

### 4.1 GELU 激活函数：tanh 近似的前向与反向

#### 4.1.1 概念说明

神经网络需要**非线性激活函数**，否则多层线性变换叠加后整体仍是一个线性变换，深度就失去意义。GPT-2 选用的激活函数是 **GELU（Gaussian Error Linear Unit，高斯误差线性单元）**。

GELU 的**精确数学定义**是：

\[
\mathrm{GELU}(x) = x \cdot \Phi(x)
\]

其中 \(\Phi(x)\) 是标准正态分布的累积分布函数（CDF）：

\[
\Phi(x) = \frac{1}{2}\left[1 + \mathrm{erf}\left(\frac{x}{\sqrt{2}}\right)\right]
\]

直觉上：\(\Phi(x)\) 是一个从 0 平滑上升到 1 的 S 形曲线。当 \(x\) 很大（正），\(\Phi(x)\approx 1\)，于是 \(\mathrm{GELU}(x)\approx x\)，相当于「放行」；当 \(x\) 很负，\(\Phi(x)\approx 0\)，于是 \(\mathrm{GELU}(x)\approx 0\)，相当于「抑制」。所以 GELU 可以理解为「以概率 \(\Phi(x)\) 保留输入」的平滑开关，比 ReLU 的硬截断更柔和，处处可导，梯度也更平滑。

精确公式里有个 `erf`（误差函数），在 C 里要算它既慢又麻烦。工程上常用一个**tanh 近似**（即 GPT-2 论文里用的形式）：

\[
\mathrm{GELU}(x) \approx 0.5x\left[1 + \tanh\left(\sqrt{\frac{2}{\pi}}\left(x + 0.044715x^3\right)\right)\right]
\]

这个近似只用 `tanh` 和多项式，最大误差约 \(10^{-4}\) 量级，对训练精度毫无影响，所以 GPT-2 / BERT 系列都默认用它。llm.c 的 CPU 参考也是这个近似版。

**为什么是 0.044715 和 \(\sqrt{2/\pi}\)**：这组常数是通过最小化近似式与精确 `erf` 版之间的误差拟合出来的，是经验值，记下来即可。

#### 4.1.2 核心流程

记缩放因子 \(s=\sqrt{2/\pi}\)（代码里是宏 `GELU_SCALING_FACTOR`），令：

\[
g(x) = s\,(x + 0.044715\,x^3)
\]

则前向为：

\[
\mathrm{out}_i = 0.5\,x_i\,\bigl(1 + \tanh(g(x_i))\bigr)
\]

这是一个**逐元素**操作，循环跑 `N` 次即可。

反向要算 \(\mathrm{dinp}_i = \mathrm{GELU}'(x_i)\cdot \mathrm{dout}_i\)。关键在于求 \(\mathrm{GELU}'(x)\)。记 \(t=\tanh(g)\)，并对 \(f(x)=0.5x(1+\tanh(g(x)))\) 用乘积法则求导：

\[
f'(x) = 0.5\bigl(1 + \tanh(g)\bigr) \;+\; 0.5x\cdot\mathrm{sech}^2(g)\cdot g'(x)
\]

其中用到 \(\frac{d}{du}\tanh(u)=\mathrm{sech}^2(u)=1-\tanh^2(u)=1/\cosh^2(u)\)，而

\[
g'(x) = s\,(1 + 3\cdot 0.044715\,x^2)
\]

把 \(s\) 提出来，最终的反向局部梯度（即 \(\mathrm{GELU}'(x)\)）为：

\[
f'(x) = 0.5(1+\tanh_{\text{out}}) \;+\; x\cdot 0.5\cdot\mathrm{sech}_{\text{out}}\cdot s\cdot(1 + 3\cdot 0.044715\,x^2)
\]

伪代码：

```text
gelu_forward(out, inp, N):
    for i in 0..N:
        x = inp[i]
        cube = 0.044715 * x^3
        out[i] = 0.5 * x * (1 + tanh(s * (x + cube)))

gelu_backward(dinp, inp, dout, N):
    for i in 0..N:
        x = inp[i]
        tanh_arg = s * (x + 0.044715 * x^3)
        tanh_out = tanh(tanh_arg)
        sech_out = 1 / cosh(tanh_arg)^2      # = 1 - tanh_out^2 也行
        local_grad = 0.5*(1+tanh_out) + x*0.5*sech_out*s*(1 + 3*0.044715*x^2)
        dinp[i] += local_grad * dout[i]        # 注意是 +=
```

注意三点：

1. 反向需要用到**前向的输入** `inp`（即 `l_fch`），所以前向必须把 `fch` 这块激活缓存住，留给反向用。
2. 反向用 `+=`，依赖 `gpt2_zero_grad` 清零。
3. 代码里用 `sech_out = 1/(cosh*cosh)` 而不是 `1 - tanh_out^2`，两者数学等价，作者选了前者。

#### 4.1.3 源码精读

先看前向。`GELU_SCALING_FACTOR` 宏定义在前向函数上一行，值正是 \(\sqrt{2/\pi}\)：

宏定义与前向：[train_gpt2.c:407-415](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L407-L415)

```c
#define GELU_SCALING_FACTOR sqrtf(2.0f / M_PI)
void gelu_forward(float* out, float* inp, int N) {
    // (approximate) GeLU elementwise non-linearity in the MLP block of Transformer
    for (int i = 0; i < N; i++) {
        float x = inp[i];
        float cube = 0.044715f * x * x * x;
        out[i] = 0.5f * x * (1.0f + tanhf(GELU_SCALING_FACTOR * (x + cube)));
    }
}
```

这段代码逐元素计算 \(0.5x(1+\tanh(s(x+0.044715x^3)))\)。`cube` 是 \(0.044715x^3\)，循环只跑一次（`N` 个元素），是「逐元素算子」的典型形态。

再看反向。反向被一段编译指示包裹，原因是 issue #168 发现 `-Ofast` 优化会破坏 GELU 的数值，所以专门为它关闭激进浮点优化：

反向（含 float_control 指示）：[train_gpt2.c:417-434](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L417-L434)

```c
// we want to use -Ofast optimization, but sadly GeLU breaks, so disable this flag just for it (#168)
#pragma float_control(precise, on, push)
#if defined(__GNUC__) && !defined(__clang__)
__attribute__((optimize("no-finite-math-only")))
#endif
void gelu_backward(float* dinp, float* inp, float* dout, int N) {
    for (int i = 0; i < N; i++) {
        float x = inp[i];
        float cube = 0.044715f * x * x * x;
        float tanh_arg = GELU_SCALING_FACTOR * (x + cube);
        float tanh_out = tanhf(tanh_arg);
        float coshf_out = coshf(tanh_arg);
        float sech_out = 1.0f / (coshf_out * coshf_out);
        float local_grad = 0.5f * (1.0f + tanh_out) + x * 0.5f * sech_out * GELU_SCALING_FACTOR * (1.0f + 3.0f * 0.044715f * x * x);
        dinp[i] += local_grad * dout[i];
    }
}
#pragma float_control(pop)
```

逐行对应 4.1.2 里的推导：

- `tanh_arg` 即 \(g(x)=s(x+0.044715x^3)\)。
- `tanh_out` 即 \(\tanh(g)\)。
- `sech_out` 即 \(\mathrm{sech}^2(g)=1/\cosh^2(g)\)，用 `coshf` 算出。
- `local_grad` 的两部分正好对应乘积法则的两项：`0.5*(1+tanh_out)` 是「外层乘 \(x\)」的那支，`x*0.5*sech_out*s*(1+3*0.044715*x^2)` 是「\(0.5x\) 乘进去后乘 \(\tanh\) 的导数再乘 \(g'(x)\)」的那支。
- 最后一行 `dinp[i] += local_grad * dout[i]` 完成链式法则并累加。

> 这段 `#pragma` / `__attribute__` 的存在说明一个现实细节：作者本来想对整个文件开 `-Ofast`（一种会放松浮点严格性的激进取速选项）来加速 CPU 版，但 GELU 的反向在 `coshf/sech` 组合下会算出错误数值，于是只对这一个函数局部恢复严格浮点模式。这是「数值正确性 vs 速度」的一次真实取舍。

对照 PyTorch 参考实现 `NewGELU`，公式一字不差：

PyTorch 参考的 GELU：[train_gpt2.py:40-43](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.py#L40-L43)

```python
class NewGELU(nn.Module):
    ...
    def forward(self, x):
        return 0.5 * x * (1.0 + torch.tanh(math.sqrt(2.0 / math.pi) * (input + 0.044715 * torch.pow(input, 3.0))))
```

可见 C 版的 tanh 近似与 PyTorch 参考完全一致——这就是为什么两套实现能跑出几乎相同的 loss（参见 [u3-l4 正确性测试](u3-l4-correctness-test.md)）。

#### 4.1.4 代码实践

**实践目标**：亲手实现 `gelu_forward` 的 tanh 近似，并在 \(x=-2,0,2\) 三点上与精确 GELU 比较，确认误差在可接受范围。

**操作步骤**：

1. 新建一个最小 C 程序（这是**示例代码**，不是项目原有文件，不要放进仓库）：

   ```c
   #include <stdio.h>
   #include <math.h>

   // 精确 GELU: x * Phi(x), Phi 用 erf 表示
   double gelu_exact(double x) {
       return 0.5 * x * (1.0 + erf(x / sqrt(2.0)));
   }

   // tanh 近似 GELU（与 train_gpt2.c 一致）
   double gelu_tanh(double x) {
       const double s = sqrt(2.0 / M_PI);
       double cube = 0.044715 * x * x * x;
       return 0.5 * x * (1.0 + tanh(s * (x + cube)));
   }

   int main(void) {
       double xs[3] = {-2.0, 0.0, 2.0};
       for (int i = 0; i < 3; i++) {
           double x = xs[i];
           printf("x=%+4.1f  exact=%+.8f  tanh=%+.8f  diff=%+.2e\n",
                  x, gelu_exact(x), gelu_tanh(x), gelu_tanh(x) - gelu_exact(x));
       }
       return 0;
   }
   ```

2. 编译运行：`gcc gelu_cmp.c -lm -o gelu_cmp && ./gelu_cmp`。

**预期结果（手算估值，请以本地运行为准）**：

| x | 精确 GELU \(x\Phi(x)\) | tanh 近似 | 差值 |
|---|---|---|---|
| -2 | -0.04550026 | -0.04547410 | 约 -2.6e-5 |
| 0 | 0 | 0 | 0 |
| 2 | 1.95449974 | 1.95452605 | 约 +2.6e-5 |

差值在 \(10^{-5}\) 量级，对 fp32 训练完全可以忽略。

**需要观察的现象**：

- \(x=0\) 时两者都精确为 0（tanh(0)=0，精确版 \(\Phi(0)=0.5\) 得 0）。
- \(x\) 为正时近似略偏大，为负时（除 0 外）略偏小，但绝对误差极小。
- 如果你的 `diff` 出现了 \(10^{-2}\) 以上的量级，多半是公式抄错（比如把 `0.044715` 写成 `0.44715`，或漏乘缩放因子）。

> 说明：以上数值是手工代入估算的结果，**待本地验证**——请以你实际运行程序的输出为准。

#### 4.1.5 小练习与答案

**练习 1**：把 `sech_out = 1.0f / (coshf_out * coshf_out)` 改写成用 `tanh_out` 的等价形式，并解释为什么两者相等。

**参考答案**：`sech_out = 1.0f - tanh_out * tanh_out;`。因为双曲恒等式 \(\mathrm{sech}^2(u) = 1 - \tanh^2(u)\)，两者在数学上完全等价。代码作者选择用 `coshf` 是实现细节，不影响正确性。

**练习 2**：`gelu_backward` 的参数里有 `inp`（前向输入）却没有 `out`（前向输出），而有些算子（如 layernorm）的反向会缓存前向输出。为什么 GELU 反向只需要 `inp`？

**参考答案**：因为 `GELU'(x)` 的表达式只依赖 \(x\) 本身（以及由 \(x\) 算出的 `tanh_arg`），不依赖前向输出 `out`。所以缓存前向输入 `inp`（即 `l_fch`）就足够了，不需要额外缓存 `out`。

**练习 3**：`gelu_backward` 最后一行是 `dinp[i] += ...` 而不是 `=`。如果不小心写成 `=`，在 llm.c 的当前架构下会发生什么？

**参考答案**：会覆盖掉之前累加进 `dinp` 的梯度，导致梯度丢失、训练出错。llm.c 的设计是「反向全部用 `+=`，每步开头 `gpt2_zero_grad` 清零」，因此 `=` 会破坏这一约定。不过对 GELU 这个具体算子，`dinp`（`dl_fch`）只会被这一个 backward 写入，所以单看本算子改成 `=` 似乎无害——但坚持 `+=` 是为了和全文件风格一致、并兼容未来可能的复用。

---

### 4.2 残差连接：前向相加与反向梯度分流

#### 4.2.1 概念说明

**残差连接（residual connection）** 是让「深层网络能训得动」的关键技巧，源自 ResNet。它的核心思想极其简单：与其让一层把输入变换成输出，不如让这层只学「输入需要被修正多少」，再把修正量加回输入：

\[
\mathrm{out} = \mathrm{inp1} + \mathrm{inp2}
\]

在 GPT-2 的语境里，`inp1` 通常是**残差主流**（前面所有层累加下来的表示），`inp2` 是某个子层（注意力或 MLP）的输出。这样信息可以「抄近道」直通到网络深处，梯度也能沿这条捷径反传回去，从而缓解深层网络的梯度消失。

llm.c 把这个相加操作直接实现成 `residual_forward`：一个逐元素加法，`N` 次循环。

**本讲最关键的直觉——梯度分流，不是梯度减半**：很多人初学时会误以为「输出是两个输入相加，所以反向时梯度也要平均分给两边，各拿一半」。**这是错的**。正确的理解是：

\[
\frac{\partial\,\mathrm{out}_i}{\partial\,\mathrm{inp1}_i} = 1,\qquad
\frac{\partial\,\mathrm{out}_i}{\partial\,\mathrm{inp2}_i} = 1
\`

由链式法则，`dinp1 = dout * 1 = dout`，`dinp2 = dout * 1 = dout`。也就是说，**一份 `dout` 被原样复制成两份，分别流给两个分支，每边都拿到完整的梯度**。这就像一条河流到一个分叉口分成两条，但水量不是减半——每条支流都得到了和干流一样多的水（这只是个比喻，物理上不守恒，但数学上就是这样）。「分流」二字指的就是「梯度同时流向两边」，而非「被均分」。

#### 4.2.2 核心流程

前向：

\[
\mathrm{out}_i = \mathrm{inp1}_i + \mathrm{inp2}_i,\quad i=0,\dots,N-1
\]

反向（链式法则，两个偏导都是 1）：

\[
\mathrm{dinp1}_i \mathrel{+}= \mathrm{dout}_i,\qquad
\mathrm{dinp2}_i \mathrel{+}= \mathrm{dout}_i
\]

伪代码：

```text
residual_forward(out, inp1, inp2, N):
    for i in 0..N:
        out[i] = inp1[i] + inp2[i]

residual_backward(dinp1, dinp2, dout, N):
    for i in 0..N:
        dinp1[i] += dout[i]
        dinp2[i] += dout[i]
```

注意：反向**不需要前向输入**，也不需要前向输出——它纯粹是「把 `dout` 拷给两边」。这是本讲两个算子里最「无状态」的一个。

#### 4.2.3 源码精读

前向：[train_gpt2.c:436-440](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L436-L440)

```c
void residual_forward(float* out, float* inp1, float* inp2, int N) {
    for (int i = 0; i < N; i++) {
        out[i] = inp1[i] + inp2[i];
    }
}
```

逐元素相加，无可解释。

反向：[train_gpt2.c:442-447](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L442-L447)

```c
void residual_backward(float* dinp1, float* dinp2, float* dout, int N) {
    for (int i = 0; i < N; i++) {
        dinp1[i] += dout[i];
        dinp2[i] += dout[i];
    }
}
```

这正是 4.2.1 里说的「梯度分流」：同一个 `dout[i]` 被 `+=` 进 `dinp1[i]` 和 `dinp2[i]`，两边都拿到完整的梯度，没有任何除以 2。两处都用 `+=`，依赖 `gpt2_zero_grad` 清零——尤其重要，因为 `dinp1`（残差主流的梯度 `dresidual`）在反向过程中会被**多个算子反复累加**（见 4.3.3）。

> 为什么「相加」能让深层网络训得动？从反向代码看得很直白：`dout` 原封不动地传给 `dinp1`（主流），意味着无论经过多少层残差，梯度都能沿主流一路 `+=` 回到最初的 embedding 层，不会被层层缩放衰减。这就是残差连接「打通梯度高速公路」的本质——而它的实现，仅仅是这一行 `dinp1[i] += dout[i]`。

#### 4.2.4 代码实践

**实践目标**：用一个 5 元素的最小例子，亲眼确认 `residual_backward` 把 `dout` 原样复制给两侧（而不是减半），并解释为什么。

**操作步骤**：

1. 准备前向输入与上游梯度（**示例代码**）：

   ```c
   #include <stdio.h>
   int main(void) {
       float inp1[5] = {1, 2, 3, 4, 5};
       float inp2[5] = {10, 20, 30, 40, 50};
       float out[5];
       // 前向 out = inp1 + inp2
       for (int i = 0; i < 5; i++) out[i] = inp1[i] + inp2[i];

       float dout[5] = {0.1f, 0.2f, 0.3f, 0.4f, 0.5f};
       float dinp1[5] = {0}, dinp2[5] = {0};
       // 反向（与 train_gpt2.c 一致）
       for (int i = 0; i < 5; i++) {
           dinp1[i] += dout[i];
           dinp2[i] += dout[i];
       }
       for (int i = 0; i < 5; i++)
           printf("i=%d out=%.1f dinp1=%.2f dinp2=%.2f\n", i, out[i], dinp1[i], dinp2[i]);
       return 0;
   }
   ```

2. 编译运行观察输出。

**预期结果**：

```
i=0 out=11.0 dinp1=0.10 dinp2=0.10
i=1 out=22.0 dinp2=0.20 ...
```

每个位置上 `dinp1[i] == dinp2[i] == dout[i]`，没有减半。

**需要解释的问题**：为什么 `residual_backward` 直接把 `dout` 复制给 `dinp1` 和 `dinp2`？

**参考答案**：因为前向是 `out = inp1 + inp2`，两个输入对输出的偏导都是 1。由链式法则 \(\mathrm{dinp}_k = \frac{\partial L}{\partial\,\mathrm{out}}\cdot\frac{\partial\,\mathrm{out}}{\partial\,\mathrm{inp}_k}=\mathrm{dout}\cdot 1=\mathrm{dout}\)，所以两边都各拿到一份完整的 `dout`，而不是各拿一半。「相加」在反向对应「复制」，而非「均分」。

> 说明：以上是手写示例，**待本地验证**，请以实际编译运行输出为准。

#### 4.2.5 小练习与答案

**练习 1**：如果把残差前向改成 `out = 0.5*inp1 + 0.5*inp2`（一种缩放残差），反向应该怎么改？这对梯度流量有什么影响？

**参考答案**：反向变成 `dinp1[i] += 0.5f * dout[i]; dinp2[i] += 0.5f * dout[i];`（因为偏导变成 0.5）。这时梯度确实被「减半」了——这会让深层网络的梯度信号变弱。这正好说明原版「不缩放」的相加为何更利于梯度流动。注意 PyTorch 参考里 `MLP` 的 `c_proj` 带了一个 `LLMC_RESIDUAL_SCALE_FLAG`，那是 CUDA 主线里为了数值稳定性对残差分支做的缩放，与本讲 CPU 版的朴素相加不同（详见后续 CUDA 单元）。

**练习 2**：`residual_backward` 为什么用 `+=` 而不是 `=`？请结合 `dinp1` 在 MLP 块反向中的角色说明。

**参考答案**：因为残差主流的梯度（`dinp1`，常写作 `dresidual`）在一个 Transformer 块的反向里会被多次累加——它先接收来自 `residual3` 的分流，再接收来自 `ln2` 反向的结果，再接收来自 `ln1` 反向的结果（见 4.3.3）。如果用 `=`，后面的累加会冲掉前面的梯度。用 `+=` 配合 `gpt2_zero_grad` 的清零，才能正确地把这些梯度汇总到主流上。

---

### 4.3 在 Transformer MLP 块中的组装

#### 4.3.1 概念说明

GELU 和残差不会孤立存在——它们是 GPT-2 每一个 Transformer 块里 MLP 子层的两块拼图。理解它们如何被 `gpt2_forward` / `gpt2_backward` 串起来，才算真正读懂本讲的两个算子。

GPT-2 一个 Transformer 块的前向顺序（pre-norm 结构）是：

\[
\begin{aligned}
x_1 &= x_0 + \mathrm{Attn}(\mathrm{LN}_1(x_0)) && \text{（注意力残差，本讲 }x_1\text{ 即 residual2)} \\
x_2 &= x_1 + \mathrm{MLP}(\mathrm{LN}_2(x_1)) && \text{（MLP 残差，本讲 }x_2\text{ 即 residual3)}
\end{aligned}
\]

其中 MLP 子层内部是：

\[
\mathrm{MLP}(h) = \mathrm{fcproj}\bigl(\mathrm{GELU}(\mathrm{fch}(h))\bigr)
\]

也就是说，`fch`（升维到 4C）→ **GELU** → `fcproj`（降回 C）→ 与输入相加（**残差**）。GELU 是 MLP 唯一的非线性来源，残差是这个子层与主干的连接点。

引入两个本模块用到的尺寸：`4*C` 是 MLP 隐藏层宽度（GPT-2 124M 里 C=768，所以是 3072）；MLP 的 GELU 作用在 `B*T*4*C` 个元素上，残差作用在 `B*T*C` 个元素上。

#### 4.3.2 核心流程

MLP 子层的前向数据流（取自 `gpt2_forward` 单层循环体内）：

```text
l_ln2  = LayerNorm(l_residual2)                 # 归一化
l_fch  = matmul(l_ln2, fcw)                      # 升维: C -> 4C
l_fch_gelu = gelu_forward(l_fch)                 # GELU 非线性    (本讲 4.1)
l_fcproj = matmul(l_fch_gelu, fcprojw)           # 降维: 4C -> C
l_residual3 = residual_forward(l_residual2, l_fcproj)  # 残差相加  (本讲 4.2)
```

对应反向（取自 `gpt2_backward` 单层循环体内，顺序与前向严格相反）：

```text
residual_backward(dl_residual2, dl_fcproj, dl_residual3)  # 残差分流
matmul_backward(dl_fch_gelu, ..., l_fcproj, ...)          # fcproj 反向
gelu_backward(dl_fch, l_fch, dl_fch_gelu)                 # GELU 反向
matmul_backward(dl_ln2, ..., l_fch, ...)                  # fch 反向
layernorm_backward(dl_residual2, ...)                     # ln2 反向（结果再累加进 dl_residual2）
```

注意反向里 `dl_residual2` 这个梯度缓冲被**写了两次**：一次是 `residual_backward` 把 `dl_residual3` 分流进来，一次是 `layernorm_backward` 把 ln2 的输入梯度累加进来——这正是 4.2.5 练习 2 所说的「主流梯度被多次累加」，也解释了为什么反向必须用 `+=`。

#### 4.3.3 源码精读

先看前向里 MLP 子层的三行（含两条本讲算子的调用）：

MLP 前向中的 GELU 与残差：[train_gpt2.c:867-872](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L867-L872)

```c
residual_forward(l_residual2, residual, l_attproj, B*T*C);   // 注意力残差
layernorm_forward(l_ln2, l_ln2_mean, l_ln2_rstd, l_residual2, l_ln2w, l_ln2b, B, T, C);
matmul_forward(l_fch, l_ln2, l_fcw, l_fcb, B, T, C, 4*C);    // 升维
gelu_forward(l_fch_gelu, l_fch, B*T*4*C);                    // ← 本讲 GELU
matmul_forward(l_fcproj, l_fch_gelu, l_fcprojw, l_fcprojb, B, T, 4*C, C);  // 降维
residual_forward(l_residual3, l_residual2, l_fcproj, B*T*C); // ← 本讲残差(MLP)
```

要点：

- `gelu_forward(l_fch_gelu, l_fch, B*T*4*C)`：作用在 `4*C` 宽度的隐藏层上，元素数是 `B*T*4*C`。输出写到独立的 `l_fch_gelu` 缓冲，而 `l_fch`（前向输入）保留下来供反向用。
- `residual_forward(l_residual3, l_residual2, l_fcproj, B*T*C)`：把 MLP 子层输出 `l_fcproj` 加回主干 `l_residual2`，得到本块输出 `l_residual3`。元素数是 `B*T*C`（注意是 C 不是 4C，因为 fcproj 已经降维回来了）。
- 第 867 行还有一条注意力子层的残差 `residual_forward(l_residual2, residual, l_attproj, ...)`，把注意力输出加回主干——本块里残差连接一共出现两次（注意力一次、MLP 一次）。

再看反向里的对应三行（与前向严格镜像，顺序倒过来）：

MLP 反向中的残差与 GELU：[train_gpt2.c:993-998](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L993-L998)

```c
residual_backward(dl_residual2, dl_fcproj, dl_residual3, B*T*C);           // MLP 残差分流
matmul_backward(dl_fch_gelu, dl_fcprojw, dl_fcprojb, dl_fcproj, l_fch_gelu, l_fcprojw, B, T, 4*C, C);
gelu_backward(dl_fch, l_fch, dl_fch_gelu, B*T*4*C);                        // ← GELU 反向
matmul_backward(dl_ln2, dl_fcw, dl_fcb, dl_fch, l_ln2, l_fcw, B, T, C, 4*C);
layernorm_backward(dl_residual2, dl_ln2w, dl_ln2b, dl_ln2, l_residual2, l_ln2w, l_ln2_mean, l_ln2_rstd, B, T, C);
residual_backward(dresidual, dl_attproj, dl_residual2, B*T*C);             // 注意力残差分流
```

对照要点：

- 第 993 行 `residual_backward(dl_residual2, dl_fcproj, dl_residual3, ...)` 是第 872 行前向的逆：把 `dl_residual3` 分流给 `dl_residual2`（主干）和 `dl_fcproj`（MLP 子层内部）。这正是 4.2 讲的「梯度分流」。
- 第 995 行 `gelu_backward(dl_fch, l_fch, dl_fch_gelu, ...)` 是第 870 行前向的逆：用前向缓存的 `l_fch` 算 GELU 导数，把 `dl_fch_gelu` 反传成 `dl_fch`。
- 第 998 行 `residual_backward(dresidual, dl_attproj, dl_residual2, ...)` 是第 867 行前向的逆：把 `dl_residual2` 进一步分流给 `dresidual`（本块输入，即上一块的输出）和 `dl_attproj`。
- 注意 `dl_residual2` 在 993 行被写入（来自 `dl_residual3` 分流），又在 997 行被 `layernorm_backward` 作为输出参数**再次累加**——这正是它必须用 `+=` 的原因。

> 一个观察：前向里 GELU 在「升维之后、降维之前」出现恰好一次；残差在「注意力之后」和「MLP 之后」各出现一次。把握住这两个出现点，你就能在任何 Transformer 实现里迅速定位 GELU 和残差的位置。

#### 4.3.4 代码实践

**实践目标**：跟踪一次 MLP 子层的前向-反向，画出数据流图，确认 GELU 的输入 `l_fch` 是「前向缓存、反向复用」的关键张量。

**操作步骤（源码阅读型实践）**：

1. 打开 [train_gpt2.c 的 gpt2_forward](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L867-L872)，在 868–872 行旁标注每行产出的张量名：`l_ln2 → l_fch → l_fch_gelu → l_fcproj → l_residual3`。
2. 打开 [train_gpt2.c 的 gpt2_backward](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L993-L998)，把 993–998 行的梯度张量名倒序标出：`dl_residual3 → dl_fcproj + dl_residual2 → dl_fch_gelu → dl_fch → dl_ln2 + (累加进 dl_residual2)`。
3. 回答：反向第 995 行的 `gelu_backward` 用到了前向的哪个张量？它在前向里是哪一行产出的？

**需要观察的现象**：

- 反向 GELU 依赖前向的 `l_fch`（第 869 行 `matmul_forward(l_fch, ...)` 产出）。这印证了 4.1.5 练习 2 的结论：GELU 反向只缓存输入、不缓存输出。
- 反向里 `dl_residual2` 被两个不同算子（993 行 residual、997 行 layernorm）写入，因此必须 `+=`。

**预期结果**：你能画出一条从前向 `l_fch` 到反向 `dl_fch` 的对称链条，并指出 GELU 是这条链条上的非线性环节；残差则是链条上「把主干梯度接出去又接回来」的十字路口。

#### 4.3.5 小练习与答案

**练习 1**：GPT-2 124M 有 12 层 Transformer 块，每块里 GELU 作用在 `B*T*4*C` 个元素上（C=768）。假设 B=4, T=32，一次前向里 GELU 总共处理多少个元素？

**参考答案**：\(12 \times B \times T \times 4C = 12 \times 4 \times 32 \times 4 \times 768 = 12 \times 4 \times 32 \times 3072 = 4{,}718{,}592\) 个元素（约 470 万）。注意 `4*C` 是 MLP 隐藏层宽度，GELU 只在 MLP 里出现，每层一次。

**练习 2**：如果删掉 `gelu_forward`（让 `l_fch_gelu = l_fch`），模型会退化成什么？为什么这通常不可取？

**参考答案**：MLP 会退化成两个线性 matmul 的复合 `fcproj(fch(h))`，仍然是线性变换，叠在残差上等于「主干加一个线性修正」。整个网络失去逐元素非线性，表达能力大幅下降，基本无法学好语言建模。这正说明 GELU 是 MLP 唯一的非线性来源。

---

## 5. 综合实践

把本讲的两个算子串起来，完成一次「手写最小 MLP 子层」的前向与反向。

**任务**：用 C 写一个独立的小程序，实现一个退化的 MLP 子层（去掉 LayerNorm 和偏置以保持简单）：

```text
给定 h_in (C),  fch: (4C,C),  fcproj: (C,4C)
前向:
    hidden = fch @ h_in          # 长度 4C
    act    = gelu(hidden)        # 本讲 4.1
    out    = fcproj @ act        # 长度 C
    out    = out + h_in          # 残差, 本讲 4.2

给定 dout (C), 反推 dfch, dfcproj, dh_in:
    d(fcproj_add) = dout         # 残差分流: 主路
    d(fcproj_out) = dout         # 残差分流: 子路
    dact = fcproj^T @ d(fcproj_out)
    dhidden = gelu'(hidden) * dact
    dfch = dhidden ⊗ h_in
    dh_in_sub = fch^T @ dhidden
    dh_in = dh_in_sub + d(fcproj_add)   # 两路梯度汇合
```

**操作步骤**：

1. 取 C=4，随机初始化 `h_in`、`fch`、`fcproj`，手写前向得到 `out`。
2. 设 `dout = [1,1,1,1]`，按上面的反向伪代码算出 `dfch`、`dfcproj`、`dh_in`。
3. 用**有限差分**验证：把 `h_in[0]` 增加 `1e-5`，重新前向算 `out` 的变化量之和，它应当约等于 `dh_in[0] * 1e-5`。同理验证 `dfch`、`dfcproj` 的某个元素。
4. 把 GELU 的实现直接抄自 [train_gpt2.c:408-415](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L408-L415)，把残差实现抄自 [train_gpt2.c:436-447](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.c#L436-L447)。

**预期结果**：有限差分与解析梯度在 `1e-4` 的相对误差内一致。这一步是「自己给自己当 u3-l4 正确性测试」——一旦手写反向能通过有限差分，你就真正吃透了 GELU 反向的导数推导和残差的梯度分流。

> 说明：综合实践没有项目现成的可运行入口，属于「源码阅读 + 手写验证」型任务，数值结果**待本地验证**。

---

## 6. 本讲小结

- **GELU** 是 GPT-2 的逐元素非线性激活，llm.c 用 tanh 近似式 \(0.5x(1+\tanh(\sqrt{2/\pi}(x+0.044715x^3)))\) 实现，与 PyTorch 参考 `NewGELU` 完全一致，误差约 \(10^{-5}\)。
- **GELU 反向**通过乘积法则手推出 `local_grad = 0.5(1+tanh) + x·0.5·sech²·s·(1+3·0.044715·x²)`，只需缓存前向输入 `inp`，不需要前向输出。
- gelu_backward 外层的 `#pragma float_control` 是为了规避 `-Ofast` 优化破坏 GELU 数值（issue #168）的现实细节。
- **残差连接**前向是逐元素相加 `out = inp1 + inp2`；反向是「梯度分流」——一份 `dout` 原样复制给两侧，每边都拿完整梯度，而非减半。
- 残差反向用 `+=` 是因为残差主流梯度（`dresidual`）会被多个算子反复累加，依赖 `gpt2_zero_grad` 清零。
- 在 Transformer 块里，GELU 出现在 MLP 子层的「升维之后、降维之前」（每块一次），残差出现在「注意力之后」和「MLP 之后」（每块两次）。

---

## 7. 下一步学习建议

本讲把 MLP 子层里的非线性和残差讲透了，但还差最后一块前向拼图——把 logits 变成概率并算 loss 的 **Softmax 与交叉熵**。下一篇：

- **[u2-l6 Softmax、CrossEntropy 与融合反向](u2-l6-softmax-crossentropy.md)**：讲解数值稳定的 softmax、交叉熵损失，以及把 `(probs - onehot)/N` 一步算出的融合反向技巧。它是前向的收尾，也是反向的起点。

读完 u2-l6 后，所有单层算子就齐了，接下来：

- **[u2-l7 前向组装：gpt2_forward](u2-l7-forward-assembly.md)**：把 encoder、layernorm、matmul、attention、GELU、残差、softmax 串成完整的 12 层 Transformer 前向，你会看到本讲的 `gelu_forward` / `residual_forward` 在整个前向流程里的精确位置。
- 之后进入 **Unit 3 反向组装**（[u3-l1 gpt2_backward](u3-l1-backward-assembly.md)），你会看到本讲的 `gelu_backward` / `residual_backward` 如何以与前向镜像的顺序倒着跑一遍。

如果想提前看 GPU 上这两个算子怎么做，可以跳到 **[u5-l4 各层 CUDA kernel](u5-l4-cuda-layer-kernels.md)**——其中 `llmc/gelu.cuh` 会把本讲的逐元素 GELU 改写成 CUDA kernel，每个线程处理一个元素。
