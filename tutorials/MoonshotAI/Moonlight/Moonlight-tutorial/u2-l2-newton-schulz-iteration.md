# Newton-Schulz 迭代：不依赖 SVD 的矩阵正交化

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 Muon 为什么要对更新做「正交化」，以及「矩阵零次幂」\( G^0 = UV^\top \) 的确切含义。
2. 逐行读懂 `zeropower_via_newtonschulz5`：转置、谱归一化、五次多项式迭代、再转置回来。
3. 解释系数 \((3.4445, -4.7750, 2.0315)\) 的设计取舍——为什么它故意「不精确收敛」，而最终输出是 \( US'V^\top \)（\( S'_{ii} \) 大致落在 0.5~1.5 之间）却依然对训练无害。
4. 理解三个工程手段各自解决什么问题：谱归一化防发散、bfloat16 省算力、`@torch.compile` 提速。
5. 亲手做一个数值实验：把 Newton-Schulz 的输出与 `torch.linalg.svd` 得到的精确 \( UV^\top \) 对比，并观察迭代步数对近似质量的影响。

本讲承接 [u2-l1 优化器参数分组](u2-l1-param-grouping.md)：上一讲划定了 84 个「进入 Muon 的二维参数」，本讲回答这些参数的更新究竟经历了什么变换。

## 2. 前置知识

本讲会用到几个线性代数与工程概念，先用通俗语言过一遍。

### 2.1 奇异值分解（SVD）

任意实矩阵 \( G \in \mathbb{R}^{m \times n} \) 都可以分解为

\[ G = U \Sigma V^\top \]

其中 \( U \)、\( V \) 的列是正交单位向量（奇异向量），\( \Sigma \) 是对角矩阵，对角线上的非负数 \( \sigma_1 \ge \sigma_2 \ge \dots \) 叫奇异值。直观理解：任何线性变换都能拆成「旋转 → 沿正交方向各自缩放 \( \sigma_i \) → 再旋转」。

### 2.2 两种范数

- **Frobenius 范数**：\( \|G\|_F = \sqrt{\sum_{i,j} G_{ij}^2} = \sqrt{\sum_i \sigma_i^2} \)，就是「把矩阵摊平当成一个长向量算欧氏长度」。PyTorch 中 `G.norm()` 默认算的就是它。
- **谱范数（spectral norm）**：\( \|G\|_2 = \sigma_{\max} \)，即最大的奇异值。

两者恒有 \( \|G\|_2 \le \|G\|_F \)。这个不等式在后面的「谱归一化」里至关重要：用较大的 Frobenius 范数做分母，一定能保证谱范数 ≤ 1。

### 2.3 标量牛顿迭代：一个类比

求数 \( x \) 的平方根倒数 \( 1/\sqrt{x} \)，可以用迭代 \( y \leftarrow \frac{y}{2}(3 - x y^2) \)，从初值出发几步就收敛。它的特点是**只用乘法和加法**、不需要除法迭代到机器精度。Newton-Schulz 迭代是这类思想在矩阵上的推广：用只含矩阵乘法的多项式迭代去逼近矩阵函数（这里是「零次幂」）。

### 2.4 bfloat16

bfloat16（BF16）用 1 位符号 + 8 位指数 + 7 位尾数。指数位与 float32 相同，所以**数值范围**和 float32 一样大；牺牲的是**精度**（尾数只有 7 位，约 2~3 位有效十进制）。对比 float16（5 位指数）：BF16 不容易溢出，特别适合深度学习里「范围比精度重要」的中间计算。

### 2.5 torch.compile

PyTorch 2.x 的编译器入口。被 `@torch.compile` 装饰的函数在**首次被调用时**会被追踪（trace）并编译成优化过的内核（比如把多个逐元素运算融合进矩阵乘法的 epilogue），**之后的调用**直接走编译产物，省去 Python 解释和大量小 kernel 的启动开销。代价是第一次调用有明显编译延迟。

## 3. 本讲源码地图

本讲全部源码都在同一个文件里，按下表定位：

| 位置 | 内容 |
| --- | --- |
| [examples/toy_train.py:L46-L48](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L46-L48) | 出处注释（改编自 KellerJordan/Muon）与 `@torch.compile` 装饰器 |
| [examples/toy_train.py:L49-L76](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L49-L76) | `zeropower_via_newtonschulz5` 本体，本讲的主角 |
| [examples/toy_train.py:L79-L104](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L79-L104) | `Muon` 类的 docstring，解释了「为什么用 NS 而不是 SVD」 |
| [examples/toy_train.py:L113](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L113) | `ns_steps=5`：迭代步数的默认值 |
| [examples/toy_train.py:L180-L194](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L180-L194) | `Muon.step` 中的调用点：梯度展平后传入本函数 |

## 4. 核心概念与源码讲解

### 4.1 正交化目标

#### 4.1.1 概念说明

Muon 的全名是 **Mu**ment**um** **O**rthogonalized by **N**ewton-**schulz（动量 + Newton-Schulz 正交化）。它内部先跑一个标准的带动量 SGD，得到「原始更新」\( g \)，然后做一步后处理：**用离 \( g \) 最近的正交矩阵替换 \( g \)**。

为什么值得这么做？先建立一个直觉：

- 一个权重矩阵的梯度/动量的奇异值谱通常**很不均匀**——少数方向的 \( \sigma \) 很大，多数方向很小。
- 如果直接沿 \( g \ 更新，等于让「少数方向」主导这一步，多数方向几乎没被更新。
- 正交化把所有奇异值**拉平**，让更新在各个方向上获得可比的幅度，相当于强制「雨露均沾」，不偏科。

数学上，「矩阵零次幂」通过 SVD 定义：若 \( G = U\Sigma V^\top \)，则

\[ G^0 = U V^\top \]

即把所有奇异值替换为 1、保留奇异向量。对非方阵（比如 \( 1024 \times 4096 \) 的投影矩阵）这个定义同样成立，结果是一个「最近的正交矩阵」（按 Frobenius 范数意义下的极分解，\( UV^\top \) 恰是离 \( G \) 最近的正交矩阵）。

正交化还带来一个对后续讲义很重要的性质：输出 \( u = UV^\top \) 的谱范数恰好是 1，于是最终更新 \( -\text{lr} \cdot u \) 的谱范数**恰好等于 lr**。这就是 `Muon` docstring 里那句「The updates will have spectral norm of `lr`」的含义——学习率的物理意义变得非常干净，这是 [u2-l4 讲 RMS 缩放](u2-l4-update-rms-scaling.md)的基础。

#### 4.1.2 核心流程

本函数在整体流程中的位置（伪代码，只看 Muon 分支）：

```text
for 每个 Muon 参数 p:
    g = p.grad                        # 原始梯度
    动量更新: buf = momentum * buf + g
    g = Nesterov 形式的动量
    u = zeropower_via_newtonschulz5(g, ns_steps)   # ← 本讲：正交化
    adjusted_lr = 0.2 * sqrt(max(A,B)) * lr        # ← u2-l4 讲
    p.data *= (1 - lr * wd)                        # ← u2-l3 讲
    p.data += -adjusted_lr * u                     # 应用更新
```

注意本函数的输入**不是**原始梯度，而是动量；输出 \( u \) 随后被乘上调整过的学习率写回参数。本讲只聚焦中间这一步。

#### 4.1.3 源码精读

先看 `Muon` 类 docstring 中对这步后处理的定位：

> Muon internally runs standard SGD-momentum, and then performs an orthogonalization post-processing step, in which each 2D parameter's update is replaced with the nearest orthogonal matrix.

出处：[examples/toy_train.py:L83-L86](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L83-L86)，这段话同时点明了选择 NS 的理由——「it can be stably run in bfloat16 on the GPU」（可以在 GPU 上以 bfloat16 稳定运行），细节留到 4.4 节展开。

再看函数自己的 docstring，它提前剧透了本讲最重要的结论：

> This iteration therefore does not produce UV^T but rather something like US'V^T where S' is diagonal with S_{ii}' ~ Uniform(0.5, 1.5), which turns out not to hurt model performance at all relative to UV^T.

出处：[examples/toy_train.py:L50-L58](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L50-L58)。翻译过来：这个迭代产出的**不是**精确的 \( UV^\top \)，而是 \( US'V^\top \)，其中 \( S' \) 的对角元大致均匀分布在 0.5~1.5——但经验上这完全不损害模型性能。为什么「不精确」反而可接受，是 4.3 节的主题。

函数入口处有一个防御性断言：

```python
assert len(G.shape) == 2
```

出处：[examples/toy_train.py:L59](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L59)，要求输入必须是二维矩阵。这与 [examples/toy_train.py:L180-L181](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L180-L181) 配合：`Muon.step` 在调用前把高于二维的梯度 `view` 成 `(第 0 维, -1)` 的二维矩阵，保证断言恒成立。

真正调用它的一行：

```python
u = zeropower_via_newtonschulz5(g, steps=group["ns_steps"])
```

出处：[examples/toy_train.py:L194](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L194)，迭代步数来自优化器参数组里的 `ns_steps`，默认值 5 定义在 [examples/toy_train.py:L113](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L113)。

#### 4.1.4 代码实践

**实践目标**：用 `torch.linalg.svd` 算出「精确答案」\( UV^\top \)，直观感受正交化到底做了什么。

以下为**示例代码**（非项目源码），在仓库根目录以 `python` 交互式运行：

```python
import torch

torch.manual_seed(0)
G = torch.randn(4, 3)                    # 一个小矩阵，便于肉眼观察

U, S, Vh = torch.linalg.svd(G, full_matrices=False)
exact = U @ Vh                           # 精确的 G^0 = UV^T

print("G 的奇异值:        ", S)          # 参差不齐
print("UV^T 的奇异值:     ", torch.linalg.svdvals(exact))  # 应全部为 1
print("UV^T 是否正交(误差):", (exact.T @ exact - torch.eye(3)).norm())
```

**操作步骤**：
1. 在仓库根目录启动 Python（依赖已在 [u1-l2](u1-l2-run-first-training.md) 安装）。
2. 粘贴运行上述代码。
3. 把 `G` 换成 `torch.randn(4, 3) * torch.tensor([[10],[1],[0.1],[0.01]])` 之类的病态缩放矩阵，再观察 `exact` 的奇异值。

**需要观察的现象**：无论 `G` 的奇异值多么参差不齐，`exact = U @ Vh` 的奇异值全部为 1（浮点误差量级）；且 `exact` 满足 \( X^\top X = I \)。

**预期结果**：奇异值列表打印为 `[1.0, 1.0, 1.0]`（约 \( 10^{-7} \) 级误差）。这就是「正交化」的全部含义——只保留方向、抹平幅度。耗时对比如何，留到综合实践。

#### 4.1.5 小练习与答案

**练习 1**：一维参数（如 RMSNorm 的缩放向量）为什么不能做这种正交化？

**参考答案**：一个 \( 1 \times n \) 行向量只有一个奇异值 \( \|g\| \)，其 \( UV^\top \) 退化为单位长度的方向向量——正交化等价于「归一化」，只保留方向且幅度恒为 1。这对 embedding/lm_head 这类**按行稀疏**的梯度（多数行全零、个别行非零）是有害的：全零行的信息会被强行放大或丢失方向。这正是 [u2-l1](u2-l1-param-grouping.md) 中维度判据（`p.ndim >= 2`）与名称判据背后的数学原因。

**练习 2**：如果 \( G \) 本身已经是正交方阵（所有奇异值为 1），精确零次幂是什么？Newton-Schulz 的输出还会是 \( G \) 吗？

**参考答案**：精确答案是 \( G \) 本身（\( \Sigma = I \Rightarrow UV^\top = UIV^\top = G \)）。但 NS 的输出**不会精确等于** \( G \)：它先做谱归一化把幅度压小，再经 5 步迭代落回 0.7~1.1 的「带」内（见 4.3 节），所以输出约为 0.7~1.1 倍的 \( G \)——方向一致、幅度不精确。这正是 docstring 所说的「approximate」。

**练习 3**：docstring 说「The updates will have spectral norm of `lr`」（[examples/toy_train.py:L94](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L94)）。请解释这句话为什么成立（在不考虑 4.3 节近似的前提下）。

**参考答案**：更新为 \( -\text{lr} \cdot u \)，其中 \( u = UV^\top \) 的所有奇异值为 1，故 \( \|-\text{lr} \cdot u\|_2 = \text{lr} \cdot \|u\|_2 = \text{lr} \)。学习率直接就是单步更新的谱范数上限。

### 4.2 NS 五次迭代

#### 4.2.1 概念说明

问题：精确 SVD 在 GPU 上又慢又「娇气」（见 4.4 节），而训练中每一步都要对 84 个矩阵各做一次正交化。我们需要一个**只用矩阵乘法**的近似算法。

Newton-Schulz 迭代的思路：找一个关于 \( X \) 的多项式 \( P \)，反复执行

\[ X \leftarrow X \cdot \big(aI + b\,XX^\top + c\,(XX^\top)^2\big) \]

使得每个奇异值 \( \sigma \) 被映射为 \( \sigma \cdot p(\sigma^2) \)（其中 \( p(t) = a + bt + ct^2 \)），并且这个映射把 \( (0, 1] \) 内的所有 \( \sigma \) 都推向 1 附近。迭代 5 次后就停——不需要收敛判定，不需要除法，只有矩阵乘。

为什么映射是 \( \sigma \mapsto \sigma \, p(\sigma^2) \)：设 \( X \) 的奇异值分解为 \( U\Sigma V^\top \)，则 \( XX^\top = U\Sigma^2 U^\top \)，于是 \( aI + bXX^\top + c(XX^\top)^2 = U\,p(\Sigma^2)\,U^\top \)，整体乘积的奇异值即为 \( \sigma_i \, p(\sigma_i^2) \)。**奇异向量的方向全程保持不变**，被操作的只有奇异值——这就是「保方向、改幅度」的迭代。

#### 4.2.2 核心流程

完整流程（对应源码）：

```text
输入: 动量矩阵 G (m×n), 步数 steps
1. X ← G 转成 bfloat16
2. 若 m > n: X ← Xᵀ            # 统一成"行数 ≤ 列数"的形状, 让 Gram 矩阵更小
3. X ← X / (‖X‖_F + 1e-7)      # 谱归一化: 保证所有奇异值 ≤ 1
4. 重复 steps 次:
     A ← X Xᵀ                  # Gram 矩阵, 特征值 = σᵢ²
     B ← b·A + c·A²
     X ← a·X + B·X
5. 若第 2 步转置过: X ← Xᵀ
返回 X (bfloat16)
```

三个关键设计的动机：

- **转置（步骤 2）**：纯粹为了计算效率。若 \( m > n \)，则 \( A = XX^\top \) 是 \( m \times m \) 的大矩阵；转置后 \( A \) 变成 \( n \times n \)（取两维中较小者）。数学上完全等价：\( G \) 与 \( G^\top \) 共享奇异值，且 \( \mathrm{orth}(G^\top) = \mathrm{orth}(G)^\top \)，所以最后再转置回来即可。
- **谱归一化（步骤 3）**：迭代多项式只在奇异值 ≤ 1 的区间内表现良好（\( p(t) \) 的 \( ct^2 \) 项在 \( t \) 大时爆炸，见 4.3 节），所以必须先把谱压进安全区。注意除的是 **Frobenius 范数**——因为 \( \|X\|_2 \le \|X\|_F \)，用它做分母无需任何 SVD 就能保证谱范数 ≤ 1，代价是把奇异值压得比必要值更小一点（这无关紧要，迭代会放大回来）。`+ 1e-7` 防止全零梯度导致除零。
- **迭代体（步骤 4）**：形式是 \( X \leftarrow X(aI + bXX^\top + c(XX^\top)^2) \)，对奇异值的作用是五次多项式映射 \( \sigma \mapsto \sigma(a + b\sigma^2 + c\sigma^4) \)，故称 quintic（五次）迭代。

#### 4.2.3 源码精读

逐段精读函数本体（[examples/toy_train.py:L48-L76](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L48-L76)）：

```python
@torch.compile
def zeropower_via_newtonschulz5(G, steps):
```

出处：[examples/toy_train.py:L48-L49](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L48-L49)，`@torch.compile` 装饰整个函数以获得编译加速（4.4 节展开）。

```python
    a, b, c = (3.4445, -4.7750, 2.0315)
    X = G.bfloat16()
    if G.size(0) > G.size(1):
        X = X.T
```

出处：[examples/toy_train.py:L60-L63](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L60-L63)，这一段做了三件事：固定三个系数（4.3 节的主角）、把输入降为 bfloat16（4.4 节）、把「瘦高」矩阵转置成「矮胖」让后面的 Gram 矩阵取小维。

```python
    # Ensure spectral norm is at most 1
    X = X / (X.norm() + 1e-7)
```

出处：[examples/toy_train.py:L64-L65](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L64-L65)，如上所述，`X.norm()` 是 Frobenius 范数，是谱范数的上界，所以除完后谱范数**一定** ≤ 1，注释所言非虚，且没有引入任何 SVD。

```python
    for _ in range(steps):
        A = X @ X.T
        B = (
            b * A + c * A @ A
        )  # adapted from suggestion by @jxbz, @leloykun, and @YouJiacheng
        X = a * X + B @ X
```

出处：[examples/toy_train.py:L67-L72](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L67-L72)，迭代核心。注意 `steps` 由调用方传入（默认 5），循环体恰好实现 \( X \leftarrow X(aI + bA + cA^2) \)、\( A = XX^\top \)。行内注释标明这组系数来自社区的改进建议。

```python
    if G.size(0) > G.size(1):
        X = X.T
    return X
```

出处：[examples/toy_train.py:L74-L76](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L74-L76)，按开头的转置记录换回原始方向，返回 bfloat16 张量。它在 [examples/toy_train.py:L203](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L203) 通过 `p.data.add_(u, alpha=-adjusted_lr)` 写回 float32 参数，混合 dtype 由 PyTorch 的类型提升规则自动处理。

顺带一提，出处注释（[examples/toy_train.py:L46-L47](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L46-L47)）表明这段代码改编自 KellerJordan/Muon 仓库，Moonlight 论文是在其上做了权重衰减与 RMS 缩放两项改造（见 [u1-l1](u1-l1-project-overview.md)）。

#### 4.2.4 代码实践

**实践目标**：亲眼看到每次迭代如何把参差不齐的奇异值逐步「赶进」1 附近的窄带。

以下为**示例代码**（非项目源码）。思路：手动展开同一迭代，在每步之后插入 `svdvals` 观察点：

```python
import sys, torch
sys.path.append("examples")                       # 让 Python 找到 toy_train.py
from toy_train import zeropower_via_newtonschulz5 # 导入不会触发训练(有 __main__ 守卫)

def ns_trace(G, steps=5):
    """手动复现迭代过程, 每步打印奇异值范围(仅用于观察)。"""
    a, b, c = 3.4445, -4.7750, 2.0315
    X = G.bfloat16()
    if G.size(0) > G.size(1):
        X = X.T
    X = X / (X.norm() + 1e-7)
    for i in range(steps):
        sv = torch.linalg.svdvals(X.float())
        print(f"迭代 {i} 步后: min={sv.min():.3f}  max={sv.max():.3f}")
        A = X @ X.T
        B = b * A + c * A @ A
        X = a * X + B @ X
    return X

torch.manual_seed(0)
ns_trace(torch.randn(64, 32))
```

**操作步骤**：保存为 `ns_trace.py` 放在仓库根目录（或任意位置，保证 `sys.path` 指向 `examples/`），运行 `python ns_trace.py`。

**需要观察的现象**：初始奇异值跨度很大（归一化后大约从接近 0 到 0.3 左右）；每迭代一步，`min` 迅速上升、`max` 缓慢变化，最终两者都进入大约 0.7~1.2 的区间。

**预期结果**：约 3 步之后 `min` 与 `max` 就已贴近 1 附近；第 4、5 步只做微小调整，且**不会精确等于 1**（这正是 4.3 节要解释的「带内振荡」）。具体数值随随机种子与硬件（bfloat16 舍入）略有差异，精确结果待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么转置能把 Gram 矩阵 \( A = XX^\top \) 变小？

**参考答案**：若 \( X \) 是 \( n \times m \)（\( n \le m \)），则 \( XX^\top \) 是 \( n \times n \)；若不转置（\( m \times n \)，\( m > n \)），\( XX^\top \) 是 \( m \times m \)。转置保证 \( A \) 的边长取两维中的较小者，矩阵乘开销从 \( O(m^2 n) \) 量级降到 \( O(n^2 m) \)。对 \( 4096 \times 1024 \) 这样的投影矩阵，差距是 4 倍平方关系。

**练习 2**：\( A = XX^\top \) 的特征值与 \( X \) 的奇异值是什么关系？

**参考答案**：\( \lambda_i(A) = \sigma_i(X)^2 \)（非零特征值部分）。这正是迭代映射形如 \( \sigma \mapsto \sigma\,p(\sigma^2) \) 的原因：多项式 \( p \) 作用在 Gram 矩阵的特征值上，等价于作用在奇异值的平方上。

**练习 3**：一次迭代对奇异值的具体映射公式是什么？代入 \( \sigma = 0.5 \) 计算一次映射后的值。

**参考答案**：\( \sigma \mapsto \sigma(a + b\sigma^2 + c\sigma^4) \)。代入 \( \sigma=0.5 \)：\( p(0.25) = 3.4445 - 4.7750 \times 0.25 + 2.0315 \times 0.0625 = 3.4445 - 1.19375 + 0.12697 \approx 2.378 \)，故 \( \sigma' \approx 0.5 \times 2.378 = 1.189 \)——一步就从 0.5 被推到 1 附近略偏上的位置。

### 4.3 系数与收敛特性

#### 4.3.1 概念说明

系数 \( (a, b, c) = (3.4445, -4.7750, 2.0315) \) 不是随便选的。docstring（[examples/toy_train.py:L52-L55](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L52-L55)）给出的设计原则是：

> quintic iteration whose coefficients are selected to maximize the slope at zero. For the purpose of minimizing steps, it turns out to be empirically effective to keep increasing the slope at zero **even beyond the point where the iteration no longer converges all the way to one** on the interval.

两个 competing 的目标：

1. **精确收敛**：要求 \( p(1) = 1 \) 且映射在 1 附近是收缩的，这样奇异值单调收敛到 1——但收敛慢，需要更多迭代步。
2. **快速聚拢**：让零点处的斜率 \( p(0) = a \) 尽量大，小奇异值每步被猛烈放大，少数几步就能全部拉进 1 的邻域——代价是映射在区间内不再处处收缩，奇异值最终**停在 1 附近的一条带内**而不是精确等于 1。

作者选择了后者，因为训练只做 5 步迭代，「快」比「准」重要；而最终 \( S'_{ii} \sim \text{Uniform}(0.5, 1.5) \) 的幅度误差经验上不损害模型性能（相对精确的 \( UV^\top \)）。

#### 4.3.2 核心流程

把 \( p(t) = 3.4445 - 4.7750\,t + 2.0315\,t^2 \) 的关键量算出来（\( t = \sigma^2 \)）：

- **零点放大率**：\( p(0) = 3.4445 \)。很小的奇异值每步约放大 3.44 倍；5 步累计 \( 3.4445^5 \approx 485 \) 倍。这就是「小方向也能被雨露均沾」的来源。
- **单位点不再是不动点**：\( p(1) = 3.4445 - 4.7750 + 2.0315 = 0.701 < 1 \)，即 \( \sigma = 1 \) 会被映射到 \( 0.701 \)。
- **不动点**：解 \( p(\sigma^2) = 1 \)，即 \( 2.4445 - 4.7750t + 2.0315t^2 = 0 \)，得 \( t \approx 0.753 \) 或 \( t \approx 1.598 \)，即 \( \sigma^\* \approx 0.868 \) 或 \( \sigma^\* \approx 1.264 \)。
- **带内振荡**：不动点之间的区间映射是「过冲」的。手工推演一条轨迹（请用 4.2.4 的代码验证）：\( \sigma = 1.10 \to 0.705 \to 1.109 \to 0.706 \to \dots \) 奇异值在约 0.70 与 1.11 之间来回振荡而**不收敛**——这正是 docstring 所说「no longer converges all the way to one」的数学含义。
- **小奇异值的轨迹**（同样手工推演）：\( 0.01 \to 0.034 \to 0.118 \to 0.400 \to 1.093 \to 0.699 \)，5 步内从 0.01 被拉进带内。设计目标达成。

综合起来，5 步迭代后的输出近似 \( U S' V^\top \)，\( S'_{ii} \) 分布在约 0.7~1.3（docstring 的经验说法是 Uniform(0.5, 1.5)，含 bfloat16 舍入的影响）。

**为什么可以接受**：优化器需要的是「各方向更新量级可比」（在 2 倍以内），而不是严格相等。0.7~1.3 的带意味着最偏的方向也只有约 2 倍幅度差，远好于原始梯度动辄几个数量级的奇异值失衡。

**谱归一化的角色**在此完全明确：\( p(t) \) 的 \( ct^2 = 2.0315\,t^2 \) 项在 \( t \) 大时占主导并爆炸（例如 \( t = 4 \) 时 \( p = 3.4445 - 19.1 + 32.5 = 16.8 \)，奇异值一步放大 67 倍并持续发散）。谱归一化保证输入 \( \sigma \le 1 \)（即 \( t \le 1 \)），把迭代锁在安全区间内。

**步数的默认值**：`ns_steps=5`（[examples/toy_train.py:L113](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L113)），而类 docstring 的注释是「6 is probably always enough」（[examples/toy_train.py:L97](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L97)）——因为奇异值 3 步左右就进带，之后是带内微调，多做步数收益递减。

#### 4.3.3 源码精读

本节涉及的源码点汇总：

- 系数定义：`a, b, c = (3.4445, -4.7750, 2.0315)`，出处 [examples/toy_train.py:L60](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L60)，五个数字写死在函数内，没有暴露成可调参数。
- 设计原则与「不精确收敛」的自白：出处 [examples/toy_train.py:L50-L58](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L50-L58)，docstring 把「输出是 \( US'V^\top \) 而非 \( UV^\top \)」和「不损害性能」都写明了。
- 谱归一化（安全区保障）：出处 [examples/toy_train.py:L64-L65](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L64-L65)。
- 迭代体中系数的使用位置：出处 [examples/toy_train.py:L69-L72](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L69-L72)，\( b \) 乘一阶 Gram 项、\( c \) 乘平方项、\( a \) 是残余系数。

#### 4.3.4 代码实践

**实践目标**：量化「迭代步数 vs 近似质量」的关系，验证「5 步已饱和」。

以下为**示例代码**（非项目源码）：

```python
import sys, torch
sys.path.append("examples")
from toy_train import zeropower_via_newtonschulz5

torch.manual_seed(0)
G = torch.randn(64, 32)
U, S, Vh = torch.linalg.svd(G, full_matrices=False)
exact = U @ Vh                                        # 精确 UV^T

for steps in [1, 2, 3, 5, 8]:
    approx = zeropower_via_newtonschulz5(G, steps).float()
    sv = torch.linalg.svdvals(approx)
    band_err = (sv - 1).abs().max()                   # 奇异值偏离 1 的最大幅度
    orth_err = (approx.T @ approx - torch.eye(32)).norm() / 32  # 正交性误差(归一)
    dir_err = (approx - exact).norm() / exact.norm()  # 相对精确答案的偏差
    print(f"steps={steps}: 最大奇异值偏差={band_err:.3f} "
          f"正交误差={orth_err:.4f} 方向偏差={dir_err:.3f}")
```

**操作步骤**：运行脚本，记录五行输出；再换一个形状（如 `torch.randn(256, 64)`）重跑一次。

**需要观察的现象**：`steps=1` 时奇异值偏差很大；`steps` 增到 3 时显著改善；5 与 8 之间改善甚微（带内振荡已经饱和）。

**预期结果**：定性规律如上；由于最终输出落在 0.7~1.3 的带内，`band_err` 在 `steps>=3` 后应稳定在 0.3 左右而不再明显下降，`dir_err` 稳定在 0.1~0.2 量级——**这不是 bug，而是设计**。精确数值待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：如果要求迭代严格收敛（\( \sigma \to 1 \) 单调收敛），系数需要满足什么条件？这会带来什么代价？

**参考答案**：至少需要 \( p(1) = 1 \)（1 是不动点）且在 \( (0,1] \) 上 \( 0 < \sigma p(\sigma^2)/\sigma \) 的行为是收缩的（例如经典 Newton-Schulz 系数 \( (1.5, -0.5) \) 对应 \( p(t) = 1.5 - 0.5t \)）。代价是 \( p(0) \) 变小（1.5 而不是 3.44），小奇异值每步只放大 1.5 倍，达到同样聚拢效果需要远多于 5 步，抵消了「少步数」的工程收益。

**练习 2**：把 `ns_steps` 从 5 调到 50，输出会更接近 \( UV^\top \) 吗？

**参考答案**：不会。奇异值进入 0.7~1.3 带后在带内振荡（见 4.3.2 的轨迹推演），并不随步数增加收敛到 1；同时 bfloat16 舍入误差还会让它在带内缓慢漂移。耗时却线性增加（每次迭代 3 次矩阵乘）。docstring「6 is probably always enough」说的就是这个饱和现象。

**练习 3**：若删掉谱归一化那一行，直接对未归一化的 \( G \)（谱范数比如为 8）迭代，会发生什么？

**参考答案**：\( t = \sigma^2 \) 高达 64，\( p(64) \approx 2.0315 \times 4096 \approx 8323 \)（\( ct^2 \) 项主导），奇异值一步放大数万倍，随后指数式发散，很快溢出为 inf/NaN。谱归一化是把迭代锁进安全区（\( \sigma \le 1 \)）的前置条件。

### 4.4 bfloat16 与 torch.compile

#### 4.4.1 概念说明

选 Newton-Schulz 而不是「精确 SVD + 截断」，一半的原因是算法数学，另一半是这两个工程决定。

**bfloat16 的可行性**。`Muon` 类 docstring（[examples/toy_train.py:L85-L86](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L85-L86)）明说：选 NS 的优势是「it can be stably run in bfloat16 on the GPU」。原因有三：

1. 数值范围可控：谱归一化后 \( \|X\|_2 \le 1 \)，Gram 矩阵特征值 \( \le 1 \)，中间量 \( B \)、新 \( X \) 的量级被系数控制在 \( O(1) \)——既不会下溢也不会上溢，BF16 的大动态范围绰绰有余。
2. 算法是**固定步数**的多项式迭代：没有「迭代到收敛」的判据，单步舍入误差不会通过无限反馈放大；即使最终奇异值带因舍入略有移动（0.7~1.3 内），对训练无害。
3. 对比之下，SVD 类算法（QR 迭代/分治）依赖正交性累积与收敛判据，在 7 位尾数下小奇异值会严重失真，通常只能在 FP32 里做，速度慢且显存翻倍。

**torch.compile 的收益**。`@torch.compile`（[examples/toy_train.py:L48](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L48)）主要买来三样东西：

1. **算子融合**：`b * A`、`c * A @ A`、`a * X` 这些逐元素运算可以融合进矩阵乘法的 epilogue，减少显存读写往返。
2. **消除调用开销**：训练时每个 step 要对 84 个矩阵各调用一次本函数，每次 Python 解释加几十个 kernel launch 的固定开销相当可观；编译后走编译产物（配合 CUDA graph 类机制）能大幅摊薄。
3. **零代码改动**：装饰器对函数语义完全透明。

代价也要知道：**首次**对某个新输入形状调用时会触发编译（秒级延迟），所以换 `--hidden_size` 或遇到新的参数形状时，训练开头几步会明显变慢，之后恢复。

#### 4.4.2 核心流程

```text
import toy_train 时:    @torch.compile 只是登记, 不编译
第一次调用(某个形状):  触发 trace + 编译(秒级) → 生成融合内核
之后同形状调用:        直接执行编译产物(快)
出现新形状:            再次触发编译
```

关于形状：玩具 Qwen2（hidden=1024）的 Muon 参数形状集合很小——\( q/k/v/o \) 都是 \( 1024\times1024 \)、gate/up 是 \( 4864\times1024 \)、down 是 \( 1024\times4864 \)——所以只需编译少数几个特化版本。

#### 4.4.3 源码精读

- `@torch.compile` 装饰器：出处 [examples/toy_train.py:L48](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L48)，位于函数定义的紧上方。
- `X = G.bfloat16()`：出处 [examples/toy_train.py:L61](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L61)，入口处一次性降精度，此后所有迭代在 BF16 中进行，返回值也是 BF16。
- 「可在 BF16 稳定运行」的出处声明：[examples/toy_train.py:L85-L86](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L85-L86)。
- 出处与改编来源：[examples/toy_train.py:L46-L47](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L46-L47)，指向 KellerJordan/Muon 的 `muon.py`，可以对照阅读原始版本。

#### 4.4.4 代码实践

**实践目标**：量化 bfloat16 + torch.compile 相对朴素 float32 实现的精度差与速度差。

以下为**示例代码**（非项目源码）：

```python
import sys, time, torch
sys.path.append("examples")
from toy_train import zeropower_via_newtonschulz5   # 项目实现: bf16 + compile

def ns_float32(G, steps=5):
    """对照实现: float32 + 手写循环, 其余完全相同。"""
    a, b, c = (3.4445, -4.7750, 2.0315)
    X = G.clone()
    if G.size(0) > G.size(1):
        X = X.T
    X = X / (X.norm() + 1e-7)
    for _ in range(steps):
        A = X @ X.T
        B = b * A + c * A @ A
        X = a * X + B @ X
    if G.size(0) > G.size(1):
        X = X.T
    return X

torch.manual_seed(0)
G = torch.randn(64, 32)

# 预热: 触发 torch.compile 的首次编译(这一步会慢, 属预期)
_ = zeropower_via_newtonschulz5(G, 5)

u_bf16 = zeropower_via_newtonschulz5(G, 5).float()
u_fp32 = ns_float32(G, 5)
print("两种实现的相对差:", (u_bf16 - u_fp32).norm() / u_fp32.norm())

N = 200
t0 = time.perf_counter()
for _ in range(N): _ = ns_float32(G, 5)
t1 = time.perf_counter()
for _ in range(N): _ = zeropower_via_newtonschulz5(G, 5)
t2 = time.perf_counter()
print(f"float32 手写: {t1-t0:.3f}s   项目版本(bf16+compile): {t2-t1:.3f}s")
```

**操作步骤**：在仓库根目录运行（CPU 亦可，无 GPU 时结论见下）。注意必须先「预热」一次再计时，否则测到的是编译时间。

**需要观察的现象**：相对差在 \( 10^{-2} \) 量级（BF16 约 2~3 位有效数字的自然结果），奇异值带的位置基本一致；速度上项目版本在 GPU 上应明显更快。

**预期结果**：GPU 上（如 T4/A100）项目版本应数倍于手写 float32 版本；**纯 CPU 上 compile 的收益不确定甚至可能为负**（融合与调度优化主要面向 GPU），此点待本地验证，建议有条件时在 GPU 上复跑。

#### 4.4.5 小练习与答案

**练习 1**：为什么 Newton-Schulz 能在 bfloat16 下稳定，而精确 SVD 通常不能？

**参考答案**：NS 是固定 5 步、只含矩阵乘与数乘的多项式迭代，输入已做谱归一化使所有中间量量级 \( O(1) \)，单步 7 位尾数的舍入误差不会迭代放大成灾难；且算法输出本就允许奇异值有 0.7~1.3 的带状误差。SVD 则依赖正交化过程误差不累积、小奇异值排序精确，低尾数精度下小奇异值会严重失真，还可能影响收敛判定。

**练习 2**：训练日志里前几个 step 特别慢，之后变快，可能是什么原因？

**参考答案**：`@torch.compile` 按「首次遇到的输入形状」惰性编译。第一个 step 里 84 个矩阵的几种形状各触发一次编译（秒级）；同形状的后续调用走缓存。这也解释了为什么更换 `--hidden_size` 后开头的 step 又会慢一次。

**练习 3**：本函数中 `+ 1e-7` 去掉后，对什么输入会出问题？

**参考答案**：全零矩阵（或 Frobenius 范数恰为 0 的输入，例如尚未收到梯度的参数）会导致 `X / 0` 得到 NaN。训练初期 warmup 阶段梯度很小但一般非零，这个保护主要是防御零梯度/极小梯度的边界情况。

## 5. 综合实践

**任务**：完成本讲规格中的完整对比实验——构造一个 \( 64 \times 32 \) 随机矩阵，把 `zeropower_via_newtonschulz5` 的输出与 `torch.linalg.svd` 得到的 \( UV^\top \) 从三个维度（奇异值分布、正交性误差、耗时）对比，并扫描 `ns_steps` 观察近似质量变化。

以下为**示例代码**（非项目源码），综合了前面各节的片段：

```python
import sys, time, torch
sys.path.append("examples")
from toy_train import zeropower_via_newtonschulz5

def report(G, steps):
    U, S, Vh = torch.linalg.svd(G, full_matrices=False)
    exact = U @ Vh
    approx = zeropower_via_newtonschulz5(G, steps).float()

    sv = torch.linalg.svdvals(approx)
    orth = (approx.T @ approx - torch.eye(approx.shape[1])).norm().item()
    print(f"steps={steps}  奇异值范围[{sv.min():.3f}, {sv.max():.3f}]  "
          f"正交误差={orth:.4f}  "
          f"与UV^T相对差={(approx - exact).norm() / exact.norm():.3f}")

torch.manual_seed(42)
G = torch.randn(64, 32)

# 1) 近似质量 vs 迭代步数
for steps in [1, 3, 5, 8]:
    report(G, steps)

# 2) 耗时对比: NS(5步) vs 精确 SVD
_ = zeropower_via_newtonschulz5(G, 5)          # 预热编译, 不计时
N = 500
t0 = time.perf_counter()
for _ in range(N):
    _ = zeropower_via_newtonschulz5(G, 5)
t1 = time.perf_counter()
for _ in range(N):
    _ = torch.linalg.svd(G)
t2 = time.perf_counter()
print(f"NS(5步, bf16+compile): {(t1-t0)/N*1e3:.3f} ms/次   "
      f"精确SVD: {(t2-t1)/N*1e3:.3f} ms/次")

# 3) 用真实训练形状再测一次(可选, 更有代表性)
G2 = torch.randn(4864, 1024)                   # 对应 gate/up 投影的形状
report(G2, 5)
```

**操作步骤**：
1. 将脚本保存为 `Moonlight-tutorial/` 之外任意的临时脚本（例如仓库根目录 `ns_vs_svd.py`，注意不要写进 `examples/`），运行 `python ns_vs_svd.py`。
2. 记录第 1 部分四行输出与第 2 部分的耗时。
3. 有 GPU 的话在 GPU 上复跑（把 `G` 与 `G2` `.cuda()`），对比 CPU 结果。

**需要观察的现象**：
- `steps` 从 1 → 3 时奇异值范围迅速收窄到 1 附近；3 → 8 时几乎不再改善（带内饱和）。
- 正交误差始终很小（\( X^\top X \approx I \) 的相对误差量级远小于奇异值偏差）——**方向是对的，只是幅度不精确**。
- 耗时上 NS（5 步、BF16、编译后）应显著快于精确 SVD，且矩阵越大优势越明显。

**预期结果**：奇异值最终落在约 0.7~1.3 的带内而非精确为 1；`steps=1` 输出明显未聚拢；NS 与 SVD 的单次耗时比在 GPU 上通常有几倍差距。以上量级判断基于算法分析，精确数字（尤其耗时）依赖硬件，待本地验证。

**思考题**（选做）：把 `G` 换成秩 1 矩阵 `torch.outer(torch.randn(64), torch.randn(32))`，NS 输出的奇异值谱是什么样？这相当于「梯度只沿一个方向」的极端情形，正交化后会发生什么？（提示：秩 1 矩阵只有 1 个非零奇异值，归一化后为 1，位于安全区边界，输出应仍是把该方向单位化后的小幅修正，其余方向保持 0——正交化不会无中生有地创造方向。）

## 6. 本讲小结

- 「正交化」= 矩阵零次幂 \( G^0 = UV^\top \)：保留奇异向量、把所有奇异值拉平为 1，让更新在各方向上幅度可比，且更新谱范数恰好等于学习率。
- `zeropower_via_newtonschulz5` 用**只含矩阵乘**的五次多项式迭代逼近这一目标：转置使 Gram 矩阵取小维 → Frobenius 范数谱归一化（上界保证 \( \sigma \le 1 \)）→ 5 步迭代 → 转置回来。
- 系数 \((3.4445, -4.7750, 2.0315)\) 故意把零点斜率推大（小奇异值每步放大 3.44 倍），代价是迭代**不再收敛**而是在 0.7~1.3 的带内振荡，最终输出 \( US'V^\top \) 而非 \( UV^\top \)——经验上不损害训练性能。
- 谱归一化是安全锁：没有它，\( ct^2 \) 项会让大奇异值指数发散。
- bfloat16 可行（固定步数 + 量级可控的中间量），`@torch.compile` 提供算子融合与调用开销摊薄，两者共同让「每步对 84 个矩阵正交化」在 GPU 上可负担。

## 7. 下一步学习建议

本讲只处理了「正交化」这一个变换，而 `Muon.step` 里它只是一行。下一讲 [u2-l3 Muon.step 拆解（上）：动量、正交化与权重衰减](u2-l3-muon-step-momentum-decay.md) 将补齐前后文：动量缓冲如何惰性初始化、Nesterov 选项如何改写输入 \( g \)、以及 \( p.data.mul\_(1 - lr \cdot wd) \) 的解耦权重衰减。再往后，[u2-l4 更新 RMS 一致化](u2-l4-update-rms-scaling.md) 会解释本讲输出 \( u \) 的「带状奇异值」为什么与 \( 0.2\sqrt{\max(A,B)} \) 缩放配合后能让更新 RMS 稳定在 0.2。

延伸阅读：对照本函数改编来源 KellerJordan/Muon 的 `muon.py`（见 [examples/toy_train.py:L46-L47](https://github.com/MoonshotAI/Moonlight/blob/c2ad5b20c605086526a179d36901bfc41b52b44b/examples/toy_train.py#L46-L47) 的链接），比较两版系数与迭代结构的差异；论文 `Moonlight.pdf` 中关于 Muon 算法与 update RMS 的章节（具体章节号待确认）则从理论侧论证了「带状近似 + RMS 缩放」的合理性。
