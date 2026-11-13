# u2-l2 · zeropower_via_newtonschulz5 逐行精读：五次 Newton-Schulz 迭代

## 1. 本讲目标

上一讲（u2-l1）我们已经建立了目标：把动量更新矩阵 \(G\) 正交化为 \(Q = UV^\top\)（其中 \(USV^\top = G\) 是 SVD），使更新的谱范数可控、各方向能量均衡。本讲就钻进这个目标的**具体实现**——`muon.py` 第 5–31 行的 `zeropower_via_newtonschulz5` 函数。

读完本讲，你应该能够：

1. **逐行解释**这个 27 行的函数：`bfloat16` 转换、`mT` 转置、谱范数归一化、循环内三次矩阵乘法、最后转回原形状。
2. **说清楚为什么不直接调 `torch.linalg.svd`**，而要用一个只含矩阵乘法的多项式迭代来逼近 \(UV^\top\)。
3. **从数学上分析**五次（quintic）迭代系数 \((3.4445, -4.7750, 2.0315)\) 的作用：它们只逐点改写奇异值、不改变奇异向量，且「零点斜率最大化」的取舍让输出并不是精确的 \(UV^\top\)。
4. **正确理解输出**：它实际是 \(US'V^\top\)，其中 \(S'\) 的对角元近似均匀分布在 \([0.5, 1.5]\)——这是 docstring 明说、且「完全不伤模型性能」的关键细节。
5. **从零重写**一个 `my_zeropower(G, steps)` 并与原实现对齐，再通过实验观察迭代步数与奇异值分布的关系。

## 2. 前置知识

承接 u2-l1 已建立的概念（这里只做一句话回顾，不再展开）：

- **SVD**：任意矩阵 \(G = USV^\top\)，\(U\)、\(V\) 是方向结构，奇异值 \(S\) 是各方向强度。
- **正交化**：取 \(Q = UV^\top\)，即把所有奇异值替换为 1。
- **两把尺子**：Frobenius 范数 \(\|X\|_F = \sqrt{\sum_{ij} X_{ij}^2}\)（总能量）；谱范数 \(\|X\|_2 = \sigma_{\max}\)（最大奇异值）。

本讲新增的基础概念：

| 概念 | 通俗解释 |
|---|---|
| 矩阵多项式迭代 | 只用「矩阵乘法、数乘、加法」组合出的函数，形如 \(X \leftarrow aX + b(XX^\top)X + c(XX^\top)^2X\)。GPU 张量核最擅长的就是矩阵乘法。 |
| 不动点迭代 | 反复套用同一个映射 \(x \leftarrow h(x)\)，希望它收敛到某个稳定值（不动点满足 \(h(x^\*) = x^\*\)）。 |
| Newton-Schulz 迭代 | 求「矩阵符号函数 / 极分解 / 逆平方根」的经典迭代家族（Higham《Functions of Matrices》有系统论述），特点是不需要解方程、不需要 SVD，只用矩阵乘法。 |
| bfloat16 | 「脑浮点」格式：1 位符号 + 8 位指数 + 7 位尾数。指数位与 float32 相同（动态范围大、不易溢出），尾数位少（相对精度约 \(2^{-8} \approx 0.4\%\)）。现代 GPU/TPU 上 bfloat16 矩阵乘远快于 float32。 |
| PyTorch 细节 | `X.mT` 是「矩阵转置」，交换**最后两个维度**，对 3 维以上（批量）输入也安全；`X.norm(dim=(-2, -1), keepdim=True)` 计算最后两维上的 **Frobenius 范数**并保留维度以便广播。 |

一个需要先想通的问题：为什么迭代能「只改奇异值、不动奇异向量」？设 \(X = USV^\top\)（取瘦 SVD，\(U\) 的列正交、\(V^\top V = I\)），则

\[
XX^\top = US^2U^\top, \qquad (XX^\top)^k X = U S^{2k+1} V^\top .
\]

即任何形如 \((XX^\top)^k X\) 的项都保持同样的 \(U\)、\(V^\top\) 骨架，只是把奇异值从 \(\sigma\) 换成 \(\sigma^{2k+1}\)。于是整个迭代等价于对每个奇异值**独立地**反复施加一个标量多项式

\[
h(\sigma) = a\sigma + b\sigma^3 + c\sigma^5,
\]

这就是本讲分析的主线。（注意：不能说 \(X^3 = US^3V^\top\)——这一般不成立；成立的是 \((XX^\top)X = US^3V^\top\) 这种 \(X\) 与 \(X^\top\) 交替出现的特定组合。）

## 3. 本讲源码地图

本讲的绝对主角只有 27 行，其余是为了建立上下文：

| 文件与位置 | 角色 |
|---|---|
| [muon.py:L5-L31](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L5-L31) | **本讲主角**：`zeropower_via_newtonschulz5` 全函数 |
| [muon.py:L34-L41](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L34-L41) | 唯一调用者 `muon_update`（下一讲 u2-l3 的主角），第 39 行把 `ns_steps` 传进来 |
| [muon.py:L91-L93](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L91-L93) | 更上层调用链：`Muon.step` 里 `muon_update(...)` → 施加更新 |
| [README.md:L44-L47](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L44-L47) | 超参建议：「momentum(0.95)、nesterov(True)、**ns_steps(5)** 默认即可，只需调 lr 和 weight_decay」 |

函数内部再切一刀（后面逐段精读）：

| 行号 | 内容 |
|---|---|
| L6–L14 | docstring：算法取舍与输出形态的自述 |
| L15 | `assert G.ndim >= 2`（支持批量输入） |
| L16 | 系数 \(a, b, c = (3.4445, -4.7750, 2.0315)\) |
| L17 | `X = G.bfloat16()` |
| L18–L19 | 行数 > 列数时先转置 |
| L21–L22 | 除以 Frobenius 范数 + 1e-7（保证谱范数 ≤ 1） |
| L23–L27 | 五次迭代循环（3 次矩阵乘/步） |
| L29–L31 | 转回原形状并返回 |

## 4. 核心概念与源码讲解

### 4.1 用矩阵多项式逼近「零次幂」：为什么不直接用 SVD

#### 4.1.1 概念说明

我们想要的目标 \(UV^\top\) 有个漂亮的代数身份。对满秩方阵 \(G = USV^\top\)：

\[
G\,(G^\top G)^{-1/2} = USV^\top \cdot VS^{-1}V^\top = UV^\top .
\]

也就是说，「正交化」等于把 \(G\) 的奇异值全部变成 1——**相当于给矩阵取了「零次幂」**（每个奇异值 \(\sigma \to \sigma^0 = 1\)），这就是函数名里 `zeropower` 的含义。

问题是怎么算。有两条路：

- **直接 SVD**：`torch.linalg.svd` 一次到位。缺点：SVD 的计算量常数远大于几次矩阵乘法，且它在数值上不适合 bfloat16（SVD 这类「分解类」算法对精度敏感、无法充分利用张量核），在训练循环里每个参数每一步都要做一次，太贵。
- **矩阵多项式迭代**：只用矩阵乘法。矩阵乘法恰好是 GPU 上最便宜、最稳定、bfloat16 支持最好的运算，而且训练框架的反向传播本身就在疯狂做矩阵乘，互不干扰。

于是本函数选择：先做一个保守的归一化把所有奇异值压进安全区间（4.2 节），然后反复施加一个精心挑选的多项式映射（4.3 节），让奇异值快速奔向 1 附近。

还有一点值得强调：这个迭代是**「结构性精确」的**——只要输入 \(G = USV^\top\)，任意多步迭代之后输出仍然是 \(U S' V^\top\) 的形式（同样的 \(U\)、\(V^\top\)，只有中间的对角阵在变）。这一点可以归纳证明：归一化是除以一个标量，不改变 \(U, V\)；每步迭代是若干个 \((XX^\top)^k X\) 项的线性组合，也不改变 \(U, V\)。所以「近似」只发生在**奇异值的大小**上，不发生在方向结构上。

#### 4.1.2 核心流程

```text
zeropower_via_newtonschulz5(G, steps):
    目标: 输出 ≈ G 的正交化 UV^T（其中 USV^T = G 的 SVD）

    1. X ← G.bfloat16()                     # 低精度，换速度
    2. 若 行数 > 列数: X ← X^T               # 保证 Gram 矩阵 XX^T 是较小的方阵
    3. X ← X / (‖X‖_F + 1e-7)               # 所有奇异值压到 ≤ 1
    4. 重复 steps 次:                        # 对奇异值逐点施加 h(σ)=aσ+bσ³+cσ⁵
         A ← X X^T
         B ← b·A + c·A²
         X ← a·X + B·X
    5. 若第 2 步转置过: X ← X^T              # 转回原形状
    返回 X
```

从数学上看，第 4 步对每个奇异值做的事情是反复施加 \(h(\sigma) = a\sigma + b\sigma^3 + c\sigma^5\)（推导见 4.3 节），输出是 \(U\,h^{\circ steps}(\tilde S)\,V^\top\)，其中 \(\tilde S\) 是归一化后的奇异值、\(h^{\circ k}\) 表示 \(h\) 复合 \(k\) 次。

#### 4.1.3 源码精读

先看 docstring——它是作者对算法取舍的「自述」，信息量很大：

> [muon.py:L6-L14](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L6-L14) —— 用 Newton-Schulz 迭代计算 \(G\) 的「零次幂 / 正交化」。选用一个**五次（quintic）迭代**，其系数按「最大化零点处斜率」挑选。为了把步数降到最少，经验上即使把零点斜率加大到**超过迭代能处处收敛到 1 的临界点**也更划算。因此这个迭代产出的不是 \(UV^\top\)，而是近似的 \(US'V^\top\)，其中 \(S'\) 的对角元约服从 \(\mathrm{Uniform}(0.5, 1.5)\)——实践证明相对 \(UV^\top\) **完全不伤模型性能**。

这段话拆开就是本讲 4.3、4.4 两节的提纲：

- 「五次迭代 + 零点斜率最大化」→ 4.3 节的系数分析；
- 「不再处处收敛到 1 → 输出 \(US'V^\top\)」→ 4.4 节的输出真相。

函数签名与第一行检查：

```python
def zeropower_via_newtonschulz5(G, steps: int):
```

> [muon.py:L5](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L5) —— 函数接收梯度矩阵 `G` 和迭代步数 `steps`（`muon_update` 在 [muon.py:L39](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L39) 以默认 `ns_steps=5` 调用）。

```python
assert G.ndim >= 2
```

> [muon.py:L15](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L15) —— 只断言「至少 2 维」而不要求「恰好 2 维」：配合后文 `mT` 和 `norm(dim=(-2,-1))` 的写法，整个函数天然支持**批量**输入（形状 `(..., m, n)`）。行内注释说明批量版本由 @scottjmaddox 实现、由 @YouJiacheng 用在了 speedrun 纪录中。在本仓库内部，`muon_update` 传入的总是 2 维（卷积核会先 `view` 成 2 维），批量能力是留给外部项目用的。

#### 4.1.4 代码实践：验证「奇异向量不动，只有奇异值在动」

**实践目标**：亲手确认 4.1.1 的论断——NS 迭代输出与输入共享同一对奇异向量，即输出确实形如 \(US'V^\top\)。

**操作步骤**（示例代码，可直接保存为脚本运行）：

```python
# 示例代码
import torch
from muon import zeropower_via_newtonschulz5

torch.manual_seed(0)
G = torch.randn(256, 256)

U, S, Vh = torch.linalg.svd(G)                  # G = U @ diag(S) @ Vh
X = zeropower_via_newtonschulz5(G, steps=5).float()

M = U.T @ X @ Vh.T                              # 若 X = U S' V^T，则 M 应为对角阵
diag = torch.diagonal(M)
off = M - torch.diag(diag)
print("对角元范围:", diag.min().item(), diag.max().item())
print("非对角元最大绝对值:", off.abs().max().item())
```

**需要观察的现象**：`非对角元最大绝对值` 应当远小于 1（比如比 `diag.max()` 小若干个数量级）。

**预期结果**：`M` 近似为对角阵，说明 NS 输出的左右奇异向量与 `G` 完全对齐，输出确实是 \(US'V^\top\) 的形式；对角元（即 \(S'\)）大致落在 \([0.5, 1.5]\) 附近。（具体数值待本地验证——本讲义撰写环境未运行 PyTorch，无法替你给出实测数字。）

#### 4.1.5 小练习与答案

**练习 1**：证明 \((XX^\top)X = US^3V^\top\)，其中 \(X = USV^\top\) 且 \(V^\top V = I\)（瘦 SVD）。

**答案**：\(XX^\top = U S V^\top V S U^\top = US^2U^\top\)（用到 \(V^\top V = I\)）；再左乘 \(X\)：\((XX^\top)X = US^2U^\top U S V^\top = US^3V^\top\)（用到 \(U^\top U = I\)）。同理 \((XX^\top)^2X = US^5V^\top\)。□

**练习 2**：为什么这个函数叫 `zeropower`（零次幂）？

**答案**：它把每个奇异值 \(\sigma\) 变为约 \(\sigma^0 = 1\)，相当于计算 \(G(G^\top G)^{-1/2}\)——矩阵的「零次幂」。对满秩方阵代入 SVD 即得 \(G(G^\top G)^{-1/2} = UV^\top\)。

**练习 3**：`assert G.ndim >= 2` 意味着 3 维输入（比如形状 `(8, 64, 128)`）也能通过。这在本仓库里会被用到吗？

**答案**：本仓库不会——`muon_update` 在 [muon.py:L37-L39](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L37-L39) 中把卷积核 `view` 成 2 维后传入。批量支持（`mT`、`norm(dim=(-2,-1))` 都只作用于最后两维）是给外部加速实现（如 modded-nanogpt 中一次正交化一批参数）预留的能力。

---

### 4.2 预处理三步：bfloat16、转置、谱范数归一化

#### 4.2.1 概念说明

迭代开始前有三行预处理，各自解决一个问题：

**（1）`X = G.bfloat16()` —— 精度降级换速度与稳定性。**
bfloat16 与 float32 共享 8 位指数（动态范围相同，梯度再大也不易溢出），但尾数只有 7 位（相对精度约 0.4%）。NS 迭代之所以敢用低精度，是因为它是「收敛到不动点」的迭代：每一步把奇异值往 1 附近拉，个别舍入误差不会被放大，只会让最终结果停留在 1 附近的窄带里——而 0.4% 的精度损失对优化器更新来说毫无影响。反过来，SVD 这类分解算法在低精度下可能彻底失效，这是「多项式迭代 vs SVD」的另一个实际理由。

**（2）行数 > 列数时先转置 —— 让 Gram 矩阵成为较小的那个方阵。**
设 \(X\) 是 \(m \times n\)。迭代的核心开销是 \(A = XX^\top\)，它是 \(m \times m\) 的方阵；若改用 \(X^\top X\) 则是 \(n \times n\)。把矩阵统一成「行 ≤ 列」（\(m \le n\)）后，\(XX^\top\) 是两者中较小的一个，矩阵乘代价从 \(O(n^2m)\) 量级降到 \(O(m^2n)\) 量级。神经网络隐藏层权重典型形状是 `fan_out × fan_in` 且 `fan_in` 更大（如 512×1024、256×1024），正好受益。至于正确性：正交化和转置可以交换——\(\mathrm{orth}(G^\top) = (UV^\top)^\top = VU^\top = (\mathrm{orth}(G))^\top\)——所以先转置、算完再转回来，结果不变。

**（3）除以 Frobenius 范数 —— 把所有奇异值压进安全区间。**
多项式 \(h\) 的系数是按「奇异值大约在 \([0, 1]\) 内」设计的（4.3 节），而真实梯度矩阵的尺度千差万别。这里利用不等式

\[
\|X\|_2 \;\le\; \|X\|_F \qquad (\text{最大奇异值} \le \sqrt{\text{所有元素平方和}})
\]

除以**Frobenius 范数**这个谱范数的上界，一次性保证归一化后每个奇异值都 \(\le 1\)——这正是源码注释「Ensure spectral norm is at most 1」的含义。注意这是**保守**的归一化：除数可能比真正的谱范数大不少，归一化后的奇异值会比 1 小很多也没关系，后续迭代会把它们拉回来。末尾的 `+ 1e-7` 防止全零矩阵导致 0/0。

#### 4.2.2 核心流程

三行预处理的数学表达：

\[
X_0 \;=\; \frac{\mathrm{bf16}(G)}{\big\|\mathrm{bf16}(G)\big\|_F + 10^{-7}},
\qquad \text{（若 } G \text{ 行多于列，先对 } G \text{ 取转置，最终再转回）}
\]

效果：\(\|X_0\|_2 \le 1\)，且 \(X_0 = U\tilde S V^\top\)，\(\tilde S = S / (\|G\|_F + 10^{-7})\)，奇异向量与 \(G\) 完全一致。

一个值得记住的退化行为：若 \(G\) 是全零矩阵，则 \(X_0 = 0\)，而 \(h(0) = 0\)，于是输出也是全零。「正交化一个零矩阵」本无意义，返回零是安全的优雅退化（也依赖 `+1e-7` 避免除零）。

#### 4.2.3 源码精读

```python
a, b, c = (3.4445, -4.7750,  2.0315)
X = G.bfloat16()
if G.size(-2) > G.size(-1):
    X = X.mT
```

> [muon.py:L16-L19](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L16-L19) —— 第 16 行定义五次迭代系数（4.3 节详析）。第 17 行把输入降到 bfloat16。第 18–19 行：`size(-2)`/`size(-1)` 是最后两个维度（行/列数）；行数 > 列数时用 `mT` 转置成「宽矩阵」，保证后续 \(A = XX^\top\) 是较小的方阵。

```python
# Ensure spectral norm is at most 1
X = X / (X.norm(dim=(-2, -1), keepdim=True) + 1e-7)
```

> [muon.py:L21-L22](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L21-L22) —— `X.norm(dim=(-2, -1))` 计算最后两维的 Frobenius 范数；`keepdim=True` 让结果形状保持 `(..., 1, 1)`，除法通过广播作用于每个元素。除以 Frobenius 范数（谱范数的上界）+ 极小量 1e-7，确保所有奇异值 ≤ 1，落入多项式设计的安全区间。

```python
if G.size(-2) > G.size(-1):
    X = X.mT
return X
```

> [muon.py:L29-L31](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L29-L31) —— 如果开头转置过，这里转回原形状（由正交化与转置的可交换性保证正确）。**注意返回值 `X` 是 bfloat16 张量**（输入为 float32 时），这个小细节会一路传给 `muon_update` 的尺度缩放和最终 `p.add_`。

#### 4.2.4 代码实践：转置分支等价性 + 不归一化的后果

**实践目标**：验证「高瘦矩阵与它的转置给出互为转置的结果」，以及直观感受「不归一化会爆炸」。

**操作步骤**（示例代码）：

```python
# 示例代码
import torch
from muon import zeropower_via_newtonschulz5

torch.manual_seed(0)
G1 = torch.randn(1024, 256)          # 高瘦：走 L18-L19 转置分支
G2 = G1.T                            # 宽矩阵：不转置

X1 = zeropower_via_newtonschulz5(G1, steps=5)
X2 = zeropower_via_newtonschulz5(G2, steps=5)
print("转置分支等价性 |X1.mT - X2| 的最大绝对误差:",
      (X1.mT.float() - X2.float()).abs().max().item())

# 反面实验：跳过归一化直接迭代（手动复现循环体）
a, b, c = 3.4445, -4.7750, 2.0315
X = G1.bfloat16()
for i in range(3):
    A = X @ X.mT
    B = b * A + c * A @ A
    X = a * X + B @ X
    print(f"无归一化第 {i+1} 步, 最大绝对值 = {X.abs().max().item()}")
```

**需要观察的现象**：第一行打印的误差；反面实验中元素最大绝对值逐飞膨胀。

**预期结果**：
- 等价性：误差应为 **0**。原因可以严格推出——高瘦分支内部先转置成宽矩阵 \(W\)，而宽分支的输入恰好也是同一个 \(W\)（`bfloat16()` 与转置逐元素可交换），两边执行逐位相同的计算（待本地验证）。
- 反面实验：`randn(1024, 256)` 的最大奇异值约 \(2\sqrt{256} = 32\)，代入 \(h\) 后最大奇异值飙到约 \(10^7\) 量级，再迭代一步 \(\sigma^5 \sim 10^{35}\) 触及 bfloat16 上限（约 \(3.4\times10^{38}\)），预期 2–3 步内出现 `inf`（具体在第几步待本地验证）。这正是归一化不可省的原因。

#### 4.2.5 小练习与答案

**练习 1**：为什么除以 Frobenius 范数就能保证谱范数 ≤ 1？

**答案**：因为 \(\|X\|_2 = \sigma_{\max} \le \sqrt{\sum_i \sigma_i^2} = \|X\|_F\)（Frobenius 范数是所有奇异值的 \(L_2\) 范数）。除以一个不小于 \(\sigma_{\max}\) 的数，商的谱范数必然 ≤ 1。

**练习 2**：如果 `G` 是全零矩阵，这个函数返回什么？哪一行保证了不产生 NaN？

**答案**：返回全零矩阵。因为 \(\|X\|_F = 0\) 时，`X / (0 + 1e-7) = 0`，且 \(h(0)=0\) 使迭代保持为零。[muon.py:L22](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L22) 里的 `+ 1e-7` 避免了 0/0 产生 NaN。

**练习 3**：为什么「转置成宽矩阵」能省计算？以形状 `(1024, 256)` 的输入为例说明。

**答案**：迭代核心是 \(A = XX^\top\)。直接对高瘦 \(1024\times256\) 算，\(A\) 是 \(1024\times1024\)；先转置成 \(256\times1024\)，\(A\) 只有 \(256\times256\)。矩阵乘加法的浮点运算量约从 \(1024^2 \times 256\) 降到 \(256^2 \times 1024\)，缩小为原来的 1/4，每步迭代三次矩阵乘都受益。

---

### 4.3 五次迭代循环：\(X \leftarrow aX + (bA + cA^2)X\) 的计算图与系数设计

#### 4.3.1 概念说明

循环体只有三行，每步三次矩阵乘法：

```text
A ← X X^T          # m×m Gram 矩阵
B ← b·A + c·A@A    # A 的二次多项式
X ← a·X + B@X      # 合并回 X
```

**它为什么等价于对奇异值施加 \(h(\sigma) = a\sigma + b\sigma^3 + c\sigma^5\)？** 设 \(X = USV^\top\)，则 \(A = US^2U^\top\)，于是

\[
X_{\text{new}} = aX + bAX + cA^2X
= U\left(aS + bS^3 + cS^5\right)V^\top ,
\]

即每个奇异值独立地经历 \(\sigma \mapsto h(\sigma)\)，奇异向量原封不动。这就是 4.1 节说的「结构性精确」。

**经典 Newton-Schulz 是三次的。**教科书版本（求极分解/矩阵符号函数的 Newton-Schulz 迭代）相当于

\[
h_3(\sigma) = \tfrac{3}{2}\sigma - \tfrac{1}{2}\sigma^3, \qquad h_3'(0) = 1.5,
\]

它对 \(\sigma \in (0, \sqrt{3})\) 单调收敛到 1——「处处收敛」，但零点斜率只有 1.5：很小的奇异值每步只放大 1.5 倍，5 步总共 \(1.5^5 \approx 7.6\) 倍。

**Muon 换成五次多项式，把零点斜率拉到 3.4445。** \(h'(\sigma) = a + 3b\sigma^2 + 5c\sigma^4\)，在零点处 \(h'(0) = a = 3.4445\)。对足够小的 \(\sigma\)（非线性项可忽略），每步放大 \(\approx 3.4445\) 倍，5 步共

\[
3.4445^5 \approx 485 \;\text{倍},
\]

于是 \(\sigma \gtrsim 1/485 \approx 0.002\) 的方向都能在 5 步内被推到 1 附近——这就是 docstring「**为了把步数降到最少而最大化零点斜率**」的含义：正交化一批参数的耗时正比于迭代步数（每步 3 次矩阵乘），步数从 16+ 降到 5，就是实打实的加速。

**代价：超出收敛临界点。**斜率不能白拿。可以手工验证两个事实（用 \(\sigma=1\) 代入）：

- \(h(1) = 3.4445 - 4.7750 + 2.0315 = 0.701 \ne 1\)——奇异值为 1 的方向第一步会被压到 0.7 左右；
- 令 \(h(\sigma^\*) = \sigma^\*\)，即解 \(2.4445 - 4.7750\,\sigma^2 + 2.0315\,\sigma^4 = 0\)（\(\sigma>0\)，令 \(t=\sigma^2\) 解二次方程），得不动点 \(\sigma^\* \approx 0.868\) 与 \(\sigma^\* \approx 1.264\)。

也就是说在 \([0,1]\) 上迭代并不处处收敛到 1，而是在两个不动点附近来回游走——这正是 docstring 承认的「不再处处收敛到 1」，其后果（输出 \(US'V^\top\)、\(S'\sim\mathrm{Uniform}(0.5,1.5)\)）在 4.4 节展开。系数微调的余地不大：这是一组在 speedrun 社区（@jxbz、@leorykun、@YouJiacheng，见 [muon.py:L26](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L26) 行内注释）中被反复实验出的经验值。

最后算一笔账：每步 3 次矩阵乘、默认 5 步共 15 次（外加一次归一化），全部是 bfloat16 张量乘。相比每个参数每步做一次 SVD，这便宜得多，也稳定得多。

#### 4.3.2 核心流程

奇异值视角下的完整迭代过程：

\[
\tilde\sigma_i = \frac{\sigma_i}{\|G\|_F + 10^{-7}}
\;\;\xrightarrow[\text{每步}]{h}\;\;
h^{\circ 5}(\tilde\sigma_i) \in \text{约 } [0.5,\, 1.5],
\qquad
h(\sigma) = 3.4445\,\sigma - 4.7750\,\sigma^3 + 2.0315\,\sigma^5 .
\]

标量轨迹示例（以下为**手工代入系数的计算结果**，读者应在实践中用脚本核对）：

| 初始 \(\sigma_0\) | 迭代轨迹（每步后的值） |
|---|---|
| 0.01 | 0.034 → 0.118 → 0.400 → 1.093 → 0.698 （每步约 ×3.44 放大，第 4 步冲过头再回落） |
| 0.1 | 0.340 → 0.992 → 0.707 → 1.117 → 0.719 |
| 0.5 | 1.189 → 0.896 → 0.825 → 0.937 → 0.766 |
| 1.0 | 0.701 → 1.114 → 0.721 → 1.090 → 0.696 （在两个不动点之间振荡） |

规律：**小奇异值被逐级放大直至进入 [0.7, 1.2] 附近的振荡带；本来就接近 1 的值则在振荡带内来回弹**。5 步之后，所有初始奇异值都被「拉平」到一个与初值几乎无关的窄带里——这正是「正交化」想要的效果（只是带子没有精确收在 1 上）。

#### 4.3.3 源码精读

```python
    # Perform the NS iterations
    for _ in range(steps):
        A = X @ X.mT
        B = b * A + c * A @ A # quintic computation strategy adapted from suggestion by @jxbz, @leorykun, and @YouJiacheng
        X = a * X + B @ X
```

> [muon.py:L23-L27](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L23-L27) —— 五次迭代循环的全部内容。第 25 行 `A = X @ X.mT` 算 Gram 矩阵（第 1 次矩阵乘）；第 26 行 `B = b*A + c*A@A` 组装 \(A\) 的二次多项式（第 2 次矩阵乘 `A @ A`），行内注释标明五次策略来源；第 27 行 `X = a*X + B @ X` 完成合并（第 3 次矩阵乘 `B @ X`）。三行合起来即 \(X \leftarrow U(aS+bS^3+cS^5)V^\top\)。注意代码写法刻意**复用** \(A\)（先算 `B` 再一次性乘 `X`），而不是把 \(aX + bAX + cA^2X\) 拆成三组独立矩阵乘——这是 3 次矩阵乘封顶的关键。

顺带把系数行再放回视野：

```python
a, b, c = (3.4445, -4.7750,  2.0315)
```

> [muon.py:L16](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L16) —— 五次迭代的三系数：保证 \(h(0)=0\)（零奇异值不动）、\(h'(0)=a=3.4445\)（小奇异值每步放大 3.44 倍）、\(h\) 为奇函数结构（与 \((XX^\top)^kX\) 的奇数次幂结构匹配）。

#### 4.3.4 代码实践：把 h(σ) 当普通函数研究

**实践目标**：脱离矩阵，直接在标量层面观察五次映射与经典三次映射的差别，亲手复现 4.3.2 的轨迹表。

**操作步骤**（示例代码，纯 CPU、秒级完成）：

```python
# 示例代码
a, b, c = 3.4445, -4.7750, 2.0315
h5 = lambda s: a*s + b*s**3 + c*s**5          # Muon 的五次映射
h3 = lambda s: 1.5*s - 0.5*s**3               # 经典三次 Newton-Schulz

for s0 in [0.01, 0.1, 0.5, 1.0]:
    s5 = s3 = s0
    traj5, traj3 = [s0], [s0]
    for _ in range(5):
        s5, s3 = h5(s5), h3(s3)
        traj5.append(round(s5, 3)); traj3.append(round(s3, 3))
    print(f"σ0={s0:<5} 五次: {traj5}")
    print(f"        三次: {traj3}")

print("h5'(0) =", a, "  5步总放大 ≈", round(a**5, 1))
print("h3'(0) = 1.5  5步总放大 ≈", round(1.5**5, 1))
```

**需要观察的现象**：五次映射下 \(\sigma_0=0.01\) 五步内到达 1 附近；三次映射下 \(\sigma_0=0.01\) 五步后仍然很小（\(0.01 \times 7.6 \approx 0.076\)）；五次映射在 \([0.7, 1.2]\) 内振荡而非收敛到 1。

**预期结果**：轨迹应与 4.3.2 的表格一致（该表即由此系数手工算出，待本地验证）；两种映射的 5 步总放大约为 485 倍 vs 7.6 倍——这就是「五次 + 大零点斜率」换步数的直观体现。

#### 4.3.5 小练习与答案

**练习 1**：循环体内有几次矩阵乘法？分别是哪三次？

**答案**：3 次：`X @ X.mT`（算 \(A\)）、`A @ A`（算 \(A^2\)）、`B @ X`（合并施加）。5 步共 15 次。

**练习 2**：计算 \(h'(0)\)，并解释为什么作者要把它设计成 3.4445 而不是经典三次迭代的 1.5。

**答案**：\(h'(\sigma) = a + 3b\sigma^2 + 5c\sigma^4\)，故 \(h'(0) = a = 3.4445\)。小奇异值每步约放大 \(h'(0)\) 倍；要覆盖典型梯度矩阵中很小的奇异值（比如 0.002 量级），斜率 1.5 需要 ~16 步，斜率 3.4445 只需 5 步。步数直接决定每次正交化的矩阵乘次数，所以值得用「不处处收敛」换。

**练习 3**：为什么迭代中用的是 \(XX^\top\)（\(m \times m\)）而不是 \(X^\top X\)（\(n \times n\)）？

**答案**：因为预处理已把 \(X\) 统一成行 ≤ 列（\(m \le n\)）的宽矩阵，\(XX^\top\) 是较小的 Gram 矩阵，构成循环的 `A` 与 `A @ A` 都在小方阵上进行，计算更省。两者在数学上都可行（对应的 \(h\) 相同），选择纯出于效率。

---

### 4.4 输出的真相：\(US'V^\top\)（\(S' \approx \mathrm{Uniform}(0.5, 1.5)\)）与「不完美但够用」

#### 4.4.1 概念说明

把 4.1–4.3 拼起来，这个函数的输出**不是**精确的 \(UV^\top\)，而是

\[
X_{\text{out}} = U S' V^\top, \qquad S'_{ii} \approx \mathrm{Uniform}(0.5,\, 1.5)\ \text{（经验观测）}.
\]

两半要分开评价：

- **结构（\(U\)、\(V^\top\) 不变）是精确的**——可以归纳证明：归一化除以标量、每步迭代都是 \((XX^\top)^kX\) 型项的组合，都不触碰奇异向量。4.1.4 的实践就是在验证这一点。
- **数值（\(S'\) 的分布）是经验性的**——「约 \(\mathrm{Uniform}(0.5,1.5)\)」是 docstring 对典型输入的观测总结，不是定理。它的成因正是 4.3 节：系数把零点斜率推过了「处处收敛到 1」的临界点，5 步之后各奇异值停在 1 附近的振荡带里。

**为什么不伤效果？** 回顾 u2-l1 建立的 Muon 学习率语义：lr 的单位是「每次更新的谱范数」。要做到这一点，优化器只需要输出满足两件事：

1. **方向配对保留**——梯度指向哪个 \((u_i, v_i)\) 方向组合，更新就沿哪个方向走（精确成立）；
2. **谱范数量级可控**——奇异值被拉平到 \(O(1)\) 的窄带，\(\|X_{\text{out}}\|_2\) 有界，一步更新对层输出的最大扰动由 lr 封顶（近似成立：奇异值在 0.5–1.5 内波动，相当于各方向更新幅度有 ±50% 的相对扰动）。

第 2 条不要求精确等于 1。实践证明（speedrun 纪录、后续大规模训练）这点扰动完全无损，而它换来的是每次正交化从 SVD 降为 15 次 bfloat16 矩阵乘。这是一个非常典型的**工程取舍**：放弃无意义的精确，把预算花在迭代步数上。

还有两个隐藏细节值得点名：

- **输出 dtype 是 bfloat16**（输入 float32 时）。下游 `muon_update` 的尺度缩放（下一讲）和 `p.add_(update, alpha=-lr)` 都兼容这个低精度更新。
- **函数本身无状态、无随机性**：不维护任何缓冲区、不依赖任何全局量，同样输入必得同样输出——这让它易于单测和复现（也意味着 4.2.4 的「误差恰为 0」实验是可能的）。

#### 4.4.2 核心流程

输入输出全景（把整条流水线串起来）：

```text
G (float32, m×n, 尺度任意)
   │ ① bfloat16 + （行>列则转置）
   │ ② ÷ (Frobenius范数 + 1e-7)         → 所有奇异值 ≤ 1
   │ ③ h(σ)=aσ+bσ³+cσ⁵ 迭代 5 次        → 奇异值进入 ~[0.5,1.5]
   │ ④ （若转置过则转回）
   ▼
X_out = U S' V^T (bfloat16, 形状与 G 相同, S'≈Uniform(0.5,1.5))
   └─ 下游: muon_update 第 40 行乘尺度因子 max(1, m/n)**0.5（下一讲）
```

与上一讲的连接：\(S'\) 近似 \(\mathrm{Uniform}(0.5,1.5)\) 解释了 u2-l1 遗留的问题——为什么「即使增加迭代步数，输出奇异值也不会全部精确等于 1」：不是迭代不收敛，而是系数**故意**选在了不处处收敛到 1 的区域。

#### 4.4.3 源码精读

docstring 中关于输出形态的三句话：

```python
    This iteration therefore does not produce UV^T but rather something like US'V^T
    where S' is diagonal with S_{ii}' ~ Uniform(0.5, 1.5), which turns out not to hurt model
    performance at all relative to UV^T, where USV^T = G is the SVD.
```

> [muon.py:L11-L13](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L11-L13) —— 作者明确交代：输出是 \(US'V^\top\) 而非 \(UV^\top\)，\(S'\) 对角元近似 \(\mathrm{Uniform}(0.5,1.5)\)，且相对 \(UV^\top\) 完全不伤模型性能。

收尾两行（4.2.3 已逐行读过，这里只强调它们在输出语义中的角色）：

```python
    if G.size(-2) > G.size(-1):
        X = X.mT
    return X
```

> [muon.py:L29-L31](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L29-L31) —— 把开头的转置还原，保证 `return X` 的形状与输入 `G` 完全一致、方向结构与 \(G\) 的 SVD 对齐；返回的是 bfloat16 张量。

下游第一站（预告下一讲）：

```python
    update = zeropower_via_newtonschulz5(update, steps=ns_steps)
    update *= max(1, update.size(-2) / update.size(-1))**0.5
```

> [muon.py:L39-L40](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L39-L40) —— `muon_update` 在这里调用本函数（`ns_steps` 默认 5），随后用尺度因子把不同长宽比矩阵的更新 RMS 拉齐——那是 u2-l3 的主题。

#### 4.4.4 代码实践：测一测 S′ 的分布

**实践目标**：用随机矩阵样本验证（或质疑）docstring 的「\(S'_{ii} \sim \mathrm{Uniform}(0.5, 1.5)\)」说法。

**操作步骤**（示例代码）：

```python
# 示例代码
import torch
from muon import zeropower_via_newtonschulz5

torch.manual_seed(0)
svs = []
for _ in range(64):
    G = torch.randn(128, 128)
    X = zeropower_via_newtonschulz5(G, steps=5).float()
    svs.append(torch.linalg.svdvals(X))        # 输出的奇异值 = S' 的对角元
svs = torch.stack(svs).flatten()

hist = torch.histc(svs, bins=10, min=0.0, max=2.0)
centers = [round(0.1 + 0.2 * i, 1) for i in range(10)]
for c, n in zip(centers, hist):
    print(f"[{c:.1f}, {c+0.2:.1f}): {int(n)}")
print(f"min={svs.min():.4f}  max={svs.max():.4f}  mean={svs.mean():.4f}")
```

**需要观察的现象**：直方图——奇异值质量集中在哪里，是否大致均匀铺满 \([0.5, 1.5]\)，有没有超出这个区间的离群值。

**预期结果**：绝大多数奇异值落在 \([0.5, 1.5]\)，均值在 1 附近；分布形状是否严格均匀取决于输入分布，docstring 的说法是对 `randn` 类输入的经验概括（具体直方图待本地验证）。若你换了输入（比如秩亏矩阵、强重尾梯度），分布会变化——这本身就是个值得记录的观察。

#### 4.4.5 小练习与答案

**练习 1**：「输出 = \(US'V^\top\)」这句话里，哪一半是精确的、哪一半是经验性的？

**答案**：结构精确——\(U\)、\(V^\top\) 与输入 SVD 完全一致（归纳可证，归一化与 \((XX^\top)^kX\) 型项都不改变奇异向量）；\(S'\sim\mathrm{Uniform}(0.5,1.5)\) 是对典型输入的经验观测，随输入分布会有出入。

**练习 2**：为什么 \(S'\) 的对角元不全是 1 也「不伤模型性能」？

**答案**：Muon 依赖的是「方向配对保留」（精确）和「谱范数量级可控」（奇异值在 \(O(1)\) 窄带内）。奇异值在 0.5–1.5 波动只相当于各方向更新幅度 ±50% 的相对扰动，而它换来了 15 次 bfloat16 矩阵乘的极低成本；speedrun 纪录与大规模训练实证了这笔交易是无损的（[muon.py:L12-L13](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L12-L13)）。

**练习 3**：把 `steps` 从 5 调到 50，输出会变成精确的 \(UV^\top\) 吗？

**答案**：不会。系数选在了「不处处收敛到 1」的区域，奇异值只会继续在两个不动点（约 0.87 与 1.26）之间的振荡带里游走，并不会随步数趋于全 1。增加步数既浪费计算，也不会更接近 \(UV^\top\)。

---

## 5. 综合实践

**任务：从零重写 `my_zeropower(G, steps)`，与官方实现对齐，并研究步数—奇异值关系。**

这是本讲的验收实验：不看原函数（可以回头看 4.1.2 的伪代码流程），只凭理解写出一个等价实现，然后用同一个随机矩阵做两件事——(a) 与 `muon.zeropower_via_newtonschulz5` 对比最大绝对误差；(b) 打印 `steps=1,2,3,5,10` 时输出矩阵的最大/最小奇异值。

**参考实现与测试脚本**（示例代码；请先自己写，写完再对照）：

```python
# 示例代码：practice_u2l2.py
import torch
from muon import zeropower_via_newtonschulz5

def my_zeropower(G, steps: int = 5):
    """凭理解重写的 Newton-Schulz 正交化（quintic 版本）。"""
    assert G.ndim >= 2
    a, b, c = 3.4445, -4.7750, 2.0315
    X = G.bfloat16()
    transposed = G.size(-2) > G.size(-1)
    if transposed:
        X = X.mT                                   # 行 ≤ 列，Gram 矩阵更小
    X = X / (X.norm(dim=(-2, -1), keepdim=True) + 1e-7)   # 谱范数 ≤ 1
    for _ in range(steps):
        A = X @ X.mT                               # 3 次矩阵乘 / 步
        B = b * A + c * A @ A
        X = a * X + B @ X
    if transposed:
        X = X.mT                                   # 转回原形状
    return X

# (a) 与官方实现对齐
torch.manual_seed(0)
G = torch.randn(256, 256)
err = (my_zeropower(G, steps=5).float()
       - zeropower_via_newtonschulz5(G, steps=5).float()).abs().max()
print(f"my_zeropower vs 官方实现 最大绝对误差: {err.item()}")

# (b) 步数—奇异值关系
for s in [1, 2, 3, 5, 10]:
    sv = torch.linalg.svdvals(
        zeropower_via_newtonschulz5(G, steps=s).float())
    print(f"steps={s:>2}: 最小奇异值={sv.min():.4f}  最大奇异值={sv.max():.4f}")
```

**需要观察的现象与预期结果**：

1. **对齐误差**：若你的实现与官方版本算子逐个一致（同样的 dtype、顺序、结合方式），误差应**恰好为 0**——函数无状态且确定；若结合方式略有不同（比如把 `B @ X` 拆开算），误差非零但应远小于 0.1（bfloat16 相对精度约 0.4%，矩阵元素量级约 \(1/\sqrt{256} = 0.06\)，逐元素绝对误差应在 1e-3 量级）（待本地验证）。
2. **步数—奇异值**：`steps=1` 时奇异值范围应很宽（一部分方向还远未拉平，结合 4.3.2 的标量轨迹想象：归一化后的 \(\tilde\sigma\) 从接近 0 到 1 都有，一次 \(h\) 之后仍横跨约 \([0, h(1)]\cup\) 放大带）；随步数增大，最小奇异值被逐步抬升、范围收窄；到 `steps=5` 与 `steps=10` 时范围都应大致落在 \([0.5, 1.5]\) 内，且 **10 步并不会比 5 步更接近「全 1」**——印证 4.4.5 练习 3 的结论（具体数值待本地验证）。
3. **顺手做一个形状冒烟测试**：把 `G` 换成 `torch.randn(64, 3, 7, 7)` 的 `view(len(G), -1)` 结果（模拟卷积核）和 `torch.randn(1024, 256)`（高瘦，走转置分支），确认 `my_zeropower` 输出形状与输入一致。

**通过标准**：对齐误差 ≤ 1e-2（理想为 0）；能用自己的话说出「为什么 steps=10 不比 steps=5 更正交」。

## 6. 本讲小结

- `zeropower_via_newtonschulz5`（[muon.py:L5-L31](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L5-L31)）用**只含矩阵乘法的多项式迭代**逼近 \(UV^\top\)，避开 SVD 的高开销与 bfloat16 不兼容；每步 3 次矩阵乘，默认 5 步。
- 预处理三件套：`bfloat16` 降精度（不动点迭代天然抗舍入误差）、行多于列先转置（让 \(XX^\top\) 是较小方阵）、除以 Frobenius 范数 + 1e-7（\(\|X\|_2 \le \|X\|_F\) 保证奇异值全部 ≤ 1）。
- 迭代 \(X \leftarrow aX + (bA + cA^2)X\)（\(A = XX^\top\)）在奇异值层面等价于反复施加 \(h(\sigma)=3.4445\sigma - 4.7750\sigma^3 + 2.0315\sigma^5\)，**奇异向量始终不变**。
- 系数按「零点斜率最大化」选取：\(h'(0)=3.4445\)，5 步把小奇异值放大约 485 倍；代价是超出「处处收敛到 1」的临界点——这是有意的取舍（换更少步数）。
- 输出是 \(US'V^\top\) 而非 \(UV^\top\)，\(S'\) 对角元经验上约 \(\mathrm{Uniform}(0.5,1.5)\)；方向配对精确保留 + 谱范数量级可控，足以支撑 Muon 的「lr 以谱范数为单位」语义，实证无损。
- 函数无状态、确定性、返回 bfloat16——这些性质让它在下一讲被 `muon_update` 安全地当作纯函数调用。

## 7. 下一步学习建议

- **下一讲（u2-l3）**：精读 [muon.py:L34-L41](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L34-L41) 的 `muon_update`——本函数的唯一调用者。你将看到动量缓冲 `lerp_`、Nesterov 写法、卷积核 `view` 成矩阵，以及 `update *= max(1, m/n)**0.5` 尺度因子如何与「lr 以谱范数为单位」配合。
- **复习建议**：若 4.3 节的奇异值推导不熟，回到 u2-l1 重做一遍 SVD 分解练习；确认自己能独立推出 \((XX^\top)X = US^3V^\top\)。
- **延伸阅读**：README 的 [Hyperparameter tuning](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/README.md#L44-L47) 段落说明 `ns_steps=5` 默认值在实践中很少需要调整；学完 u4-l1 后可回来做「改系数/改步数」的对照实验。
- **源码预习**：提前浏览 [muon.py:L99-L127](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L99-L127)（`SingleDeviceMuon.step`），找到它调用 `muon_update` 的那一行，为 u2-l5 做准备。
