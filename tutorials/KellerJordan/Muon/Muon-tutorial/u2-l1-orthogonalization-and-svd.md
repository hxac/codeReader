# u2-l1 数学预热：梯度正交化与 SVD 的直觉

## 1. 本讲目标

学完本讲，你应该能够：

1. 会用 `torch.linalg.svd` 把一个梯度矩阵分解为 \(G = U\Sigma V^\top\)，并说清楚 \(U\)、\(\Sigma\)、\(V^\top\) 各自的几何含义。
2. 会手工构造正交化结果 \(UV^\top\)（把所有奇异值拉平为 1），并解释它为什么是「与原矩阵最近的正交矩阵」。
3. 分清 **Frobenius 范数**（总能量）与**谱范数**（最大拉伸倍数），理解把更新替换为正交矩阵后这两个量分别如何变化。
4. 解释为什么正交化更新对 Muon 的学习率语义友好——`lr` 的单位是「每次更新的谱范数」。
5. 读懂 `zeropower_via_newtonschulz5` 的 docstring：Muon 实际输出的不是精确的 \(UV^\top\)，而是 \(US'V^\top\)（\(S'\) 的对角元近似服从 Uniform(0.5, 1.5)），为下一讲的 Newton-Schulz 迭代建立「目标」。

本讲只做数学铺垫和数值验证，**不逐行推导 Newton-Schulz 迭代本身**（那是 u2-l2 的任务）。

## 2. 前置知识

本讲承接 u1-l3 的代码地图：`muon_update` 是一条「动量 → Nesterov → 卷积核展平 → **正交化** → 尺度缩放」的流水线。本讲 zoom 进其中的「正交化」一步，弄清它的目标到底是什么。

需要的基础概念（用通俗语言）：

- **矩阵乘法的直觉**：一个矩阵作用在向量上，可以同时完成「旋转」和「拉伸」。
- **正交矩阵**：满足 \(Q^\top Q = I\) 的方阵。它只旋转（可能带镜像），**不拉伸不压缩**——任何向量的长度经过它都不变。对非方阵，我们说「半正交」：高瘦矩阵（行数 ≥ 列数）列向量彼此正交且长度为 1，满足 \(Q^\top Q = I\)；矮胖矩阵则行向量正交，满足 \(QQ^\top = I\)。
- **奇异值**：把矩阵沿每一对正交方向轴的「拉伸强度」排成一列。不需要会手算，只需要建立「奇异值 = 各方向的增益」的图像。
- **范数**：衡量矩阵「多大」的标尺。本讲重点是比较两把尺子：Frobenius 范数（所有方向的总能量）和谱范数（最猛的一个方向拉了多少倍）。
- **torch 基础**：`torch.randn`、`@`（矩阵乘）、`.mT`（转置后两维）、`torch.linalg.svd`。这些 API 不属于本项目源码，示例会明确标注为「示例代码」。

一个有用的心理模型：神经网络隐藏层的权重矩阵 \(W\) 把激活 \(x\) 映射为 \(y = Wx\)。梯度下降要修改 \(W\)，即加上一个更新 \(\Delta W = -\text{lr} \cdot U\)。**更新矩阵 \(U\) 在各个方向上施加多大的改变，直接决定了这一步训练对这一层的影响**。Muon 的核心主张是：与其让梯度自然决定各方向的强度（有大有小、忽大忽小），不如把它们拉平。

## 3. 本讲源码地图

本讲涉及的真实源码只集中在 `muon.py`，外加一个 PyTorch 官方 API：

| 位置 | 作用 | 本讲怎么用它 |
| --- | --- | --- |
| [muon.py:L5-L31](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L5-L31) `zeropower_via_newtonschulz5` | 正交化的实际实现（Newton-Schulz 迭代） | 本讲只精读它的 **docstring 与输入输出约定**，迭代细节留给 u2-l2 |
| [muon.py:L34-L41](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L34-L41) `muon_update` | 更新流水线，正交化的调用方 | 只看第 39 行调用点，确认正交化在流水线中的位置 |
| [muon.py:L45-L64](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L45-L64) `Muon` 类 docstring | 项目对「正交化」和「lr 单位」的权威表述 | 4.2、4.3 节的语义依据 |
| `torch.linalg.svd` / `torch.linalg.svdvals` | PyTorch 官方 SVD 接口（**非本项目代码**） | 本讲所有数值实验的工具，示例代码标注 |

## 4. 核心概念与源码讲解

### 4.1 SVD：把矩阵分解成「旋转–拉伸–旋转」

#### 4.1.1 概念说明

任意实矩阵 \(G \in \mathbb{R}^{m \times n}\) 都可以做奇异值分解（SVD）：

\[ G = U \Sigma V^\top \]

其中：

- \(V^\top\) 先把输入空间**旋转**到一组最优方向（右奇异向量）；
- \(\Sigma\) 是对角矩阵，对角元 \(\sigma_1 \ge \sigma_2 \ge \dots \ge \sigma_{\min(m,n)} \ge 0\) 叫**奇异值**，表示沿每个方向的**拉伸倍数**；
- \(U\) 再把结果**旋转**到输出空间（左奇异向量）。

几何直觉：\(G\) 把单位球映成一个椭球，奇异值就是椭球的半轴长度。奇异值大 = 这个方向被强烈拉伸；奇异值接近 0 = 这个方向几乎被压扁。

对梯度矩阵做 SVD 的意义：它把「这批梯度里都装了些什么」拆成了一组互不干扰的方向 + 每个方向的强度。梯度通常是「歪的」——几个方向强、多数方向弱。正交化要处理的就是这些强度。

`muon.py` 的 docstring 直接使用了这个记号，见 4.1.3。

#### 4.1.2 核心流程

用 `torch.linalg.svd` 做分解并重构验证的流程：

```text
输入: G (m×n，本讲用 512×256)
1. U, S, Vh = torch.linalg.svd(G, full_matrices=False)
   # torch 返回的是 Vh = V 的转置；full_matrices=False 让 U 是 m×n 的「瘦」矩阵
2. 重构验证: G_rec = (U * S) @ Vh，检查 G_rec ≈ G
3. 奇异值都在 S 里（降序排列），可直接观察分布
```

一个容易踩的坑（也是必须用 `full_matrices=False` 的原因）：默认 `full_matrices=True` 时 `U` 是 512×512 的方阵，此时 `U @ Vh` 在维度上根本乘不起来（512×512 乘 256×256 不合法）；而 `full_matrices=False` 时 `U` 是 512×256、列向量正交，`U @ Vh` 才能直接构造出 4.2 节的 \(UV^\top\)。

#### 4.1.3 源码精读

SVD 的记号写在了 `zeropower_via_newtonschulz5` 的 docstring 结尾——这句话是整个函数「想算什么」的数学定义：

> This iteration therefore does not produce UV^T but rather something like US'V^T where S' is diagonal with \(S_{ii}' \sim \text{Uniform}(0.5, 1.5)\), which turns out not to hurt model performance at all relative to UV^T, where **USV^T = G is the SVD**.

见 [muon.py:L11-L13](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L11-L13)：这里明确写出 \(G = USV^\top\) 就是 SVD，而本函数的输出是「\(US'V^\top\)，\(S'\) 的对角元约在 0.5～1.5 之间」。

另外，函数入口有一行断言：

- [muon.py:L15](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L15)：`assert G.ndim >= 2`。正交化只对「矩阵」（含批量矩阵）有定义；一维参数（bias、增益）没有奇异值可言，这也呼应 u1-l1 讲过的「一维参数必须交给 AdamW」。

`torch.linalg.svd` 本身是 PyTorch API，不属于本项目，使用示例见下方实践（示例代码）。

#### 4.1.4 代码实践

1. **实践目标**：亲手分解一个随机梯度矩阵，观察奇异值「有大有小」的天然形态。
2. **操作步骤**：运行以下脚本（**示例代码**，需本地已安装 PyTorch）。

```python
import torch

torch.manual_seed(0)
G = torch.randn(512, 256)          # 模拟一张量的梯度矩阵

# full_matrices=False: U 是 512×256（列正交），Vh 是 256×256（正交）
U, S, Vh = torch.linalg.svd(G, full_matrices=False)

# 重构验证: (U * S) 把 U 的第 j 列乘以 S[j]
G_rec = (U * S) @ Vh
print("重构误差:", (G - G_rec).abs().max().item())

print("前 5 个奇异值:", S[:5].tolist())
print("后 5 个奇异值:", S[-5:].tolist())
```

3. **需要观察的现象**：重构误差应接近机器精度（10 的 -5 次方量级或更小，float32 下）；奇异值从大到小排布，**并不均匀**。
4. **预期结果**：对 512×256 的标准高斯随机矩阵，随机矩阵理论（Bai–Yin 定律）给出奇异值整体落在 \([\sqrt{m}-\sqrt{n},\ \sqrt{m}+\sqrt{n}] \approx [6.6,\ 38.6]\) 附近，即最大奇异值约 38、最小约 7，中间呈「圆顶」分布。具体数值**待本地验证**（理论是渐近值，会有随机浮动）。

#### 4.1.5 小练习与答案

**练习 1**：为什么奇异值都是非负的？
**答案**：奇异值定义为 \(\sigma_i = \sqrt{\lambda_i(G^\top G)}\)，而 \(G^\top G\) 是半正定矩阵，特征值 \(\lambda_i \ge 0\)，开根号后仍非负。

**练习 2**：只保留最大的 1 个奇异值重构 \(G\)（秩 1 近似），误差的 Frobenius 范数是多少？
**答案**：\(\| G - \sigma_1 u_1 v_1^\top \|_F = \sqrt{\sigma_2^2 + \dots + \sigma_{256}^2}\)。丢掉多少能量，误差就是多少——这正是 Eckart–Young 定理的结论：SVD 给出最优低秩近似。

**练习 3**：如果 \(G\) 本身就是一个正交矩阵（比如用 `torch.linalg.qr` 随机生成），它的奇异值长什么样？
**答案**：全是 1。正交矩阵「只旋转不拉伸」，所有方向的增益都是 1。这正是下一节正交化要造出来的东西。

### 4.2 梯度正交化：用 \(UV^\top\) 替换更新

#### 4.2.1 概念说明

所谓正交化，就是拿到 \(G = U\Sigma V^\top\) 之后，**丢掉 \(\Sigma\)，只留两个旋转**：

\[ Q = UV^\top \]

- 它保留了梯度的全部「方向结构」：哪个左奇异方向配哪个右奇异方向，完全不变。
- 它抹掉了全部「幅度信息」：所有奇异值一律变成 1。
- \(Q\) 的奇异值全是 1，所以 \(\|Q\|_2 = 1\)（谱范数恰为 1），\(\|Q\|_F = \sqrt{\min(m,n)}\)（Frobenius 范数恰为 \(\sqrt{256} = 16\)，对 512×256 的例子）。

对方阵而言，\(UV^\top\) 还是与 \(G\) 「最接近」的正交矩阵（Frobenius 范数意义下，即极分解 polar decomposition 的正交因子）。Muon 的 docstring 用的正是这个说法：每个二维参数的更新被替换为**最近的正交矩阵**（nearest orthogonal matrix），见 4.2.3。

为什么这合理？回到第 2 节的心理模型：更新矩阵的每个奇异方向对应「这次要往某组参数方向改多少」。原始梯度的奇异值可能相差几个数量级——少数方向垄断了整步更新，多数方向几乎没份。正交化之后：

- 每个方向分到的步长**完全相同**（都是 1），「各方向能量均衡」；
- 更新的谱范数**恒等于 1**，与梯度本身的尺度无关——这是 4.3 节 lr 语义的根基。

#### 4.2.2 核心流程

「精确正交化」（概念版，真正实现是 Newton-Schulz）：

```text
输入: 动量更新矩阵 M (m×n)
1. SVD: M = U Σ V^T
2. 丢弃 Σ: Q = U @ V^T
3. 输出 Q 作为这一步的更新（奇异值全部 = 1）
```

非方阵时的细节（实践里会验证）：

- 高瘦矩阵（\(m \ge n\)，本讲 512×256 的例子）：\(U\) 列正交，所以 \(Q^\top Q = I_{256}\) 成立，但 \(QQ^\top\) 是 512×512 的**投影矩阵**（秩 256），**不是**单位阵——这不是 bug，是半正交的定义使然。
- 矮胖矩阵则相反：\(QQ^\top = I_m\) 成立，\(Q^\top Q \ne I\)。

#### 4.2.3 源码精读

`Muon` 类的 docstring 用一句话定义了整个优化器，也定义了本讲的主题：

- [muon.py:L50-L53](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L50-L53)：Muon 内部先跑标准 SGD 动量，然后做一步**正交化后处理**——每个二维参数的更新被替换为**最近的正交矩阵**；为了高效正交化使用 Newton-Schulz 迭代，其优势是可以在 GPU 上以 bfloat16 稳定运行。

这段话包含三层信息，分别对应后续三讲：

1. 「先跑 SGD 动量，再正交化」→ u2-l3 精读 `muon_update` 的动量部分；
2. 「替换为最近的正交矩阵」→ **本讲**的数学目标；
3. 「用 Newton-Schulz 迭代高效实现」→ u2-l2（为什么不用精确 SVD：每一步、每一层都做 SVD 在 GPU 上太慢，且 bfloat16 下不稳定）。

正交化在流水线中的调用位置：

- [muon.py:L37-L39](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L37-L39)：卷积核（`ndim == 4`）先 `view` 成矩阵，然后第 39 行调用 `zeropower_via_newtonschulz5(update, steps=ns_steps)`。也就是说，**进入正交化的永远是二维矩阵**——这解释了为什么一维参数必须走 AdamW 分支。

- [muon.py:L55-L58](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L55-L58)：docstring 再次强调 Muon 只用于隐藏权重层；输入 embedding、输出层、增益和偏置用 AdamW；卷积权重可以「看作二维、折叠后三个维度」再用 Muon。

#### 4.2.4 代码实践

1. **实践目标**：亲手构造 \(Q = UV^\top\)，验证它的半正交性和「奇异值全 1」。
2. **操作步骤**（**示例代码**）：

```python
import torch

torch.manual_seed(0)
G = torch.randn(512, 256)
U, S, Vh = torch.linalg.svd(G, full_matrices=False)
Q = U @ Vh                      # 正交化：丢掉奇异值

print("Q^T Q 与 I 的误差:", (Q.mT @ Q - torch.eye(256)).norm().item())
print("Q Q^T 与 I 的误差:", (Q @ Q.mT - torch.eye(512)).norm().item())
print("Q 的奇异值范围:", torch.linalg.svdvals(Q).min().item(),
      torch.linalg.svdvals(Q).max().item())
```

3. **需要观察的现象**：`Q^T Q ≈ I`（误差在 1e-6 量级或更小）；但 `Q Q^T` 与 `I` 的误差**很大**；奇异值全部精确为 1。
4. **预期结果**：`Q Q^T` 不是单位阵——因为 Q 是 512×256 的半正交矩阵，`Q Q^T` 是秩 256 的投影矩阵。如果把 `G` 换成 256×256 的方阵，两个方向的检验都应通过。精确数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：设 \(G\) 的最小奇异值是 0.01。正交化后这个「弱方向」的增益变成多少？这意味着什么？
**答案**：变成 1。原本几乎不被更新的方向，现在获得和其他方向完全相同的步长。这就是「能量均衡」的字面含义——正交化会显著改变各方向的相对重要性（只保留方向配对关系，不留强弱信息）。

**练习 2**：为什么 embedding 矩阵不适合做正交化，而隐藏层权重适合？
**答案**：参考 [muon.py:L55-L56](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L55-L56) 的约束：embedding 的每一行（一个 token 的向量）在一步里只有极少数被梯度触及，把这种「稀疏、各行独立」的更新强行拉平成满秩正交矩阵没有意义；而隐藏层权重矩阵每步都被稠密使用，其更新的方向结构值得保留并均衡化。

**练习 3**：`Q = UV^T` 里如果某个奇异值为 0（G 不满秩），Q 还存在吗？还是半正交吗？
**答案**：存在。`torch.linalg.svd` 返回的 `U`（`full_matrices=False`）总是列正交的——即使对应奇异值为 0，LAPACK 也会补出正交基。所以 `Q` 仍是半正交矩阵，所有奇异值仍为 1（综合实践会验证这个反直觉现象）。

### 4.3 Frobenius 范数与谱范数：正交化前后变了什么

#### 4.3.1 概念说明

两把衡量矩阵大小的尺子，都可以用奇异值写出来：

\[ \|G\|_F = \sqrt{\textstyle\sum_{i,j} G_{ij}^2} = \sqrt{\sigma_1^2 + \dots + \sigma_r^2} \qquad (\text{总能量}) \]

\[ \|G\|_2 = \sigma_{\max}(G) \qquad (\text{最大拉伸倍数，谱范数}) \]

两者之间的夹逼关系：

\[ \|G\|_2 \;\le\; \|G\|_F \;\le\; \sqrt{r}\,\|G\|_2, \quad r = \min(m, n) \]

正交化前后的对比（以 \(m=512, n=256\) 的高斯随机矩阵为例）：

| 量 | 原始梯度 \(G\) | 正交化 \(Q = UV^\top\) |
| --- | --- | --- |
| 奇异值 | 分布在 \([6.6, 38.6]\) 附近的圆顶（随机矩阵理论） | 全部 = 1 |
| 谱范数 | ≈ 38.6（随梯度尺度线性变化） | 恰 = 1（**与梯度尺度无关**） |
| Frobenius 范数 | ≈ \(\sqrt{512 \times 256} \approx 362\) | 恰 = \(\sqrt{256} = 16\) |

为什么谱范数是理解 Muon 的关键？对一层线性映射 \(y = Wx\)，一步更新造成的输出变化满足：

\[ \|\Delta y\| \;=\; \|(W + \Delta W)x - Wx\| \;=\; \|\Delta W \, x\| \;\le\; \sigma_{\max}(\Delta W)\,\|x\| \]

即：**更新的谱范数直接封顶了「这一步对这层输出的最大扰动」**。正交化之后 \(\sigma_{\max}(Q)=1\)，于是 `lr` 的物理意义非常干净：lr 就是「每步允许这层函数最大变化多少」。这就是 docstring 里 lr 的单位——「每次更新的谱范数」。梯度再大再小，一步的影响都被 lr 锁死；相比之下，未正交化的 SGD 动量更新的谱范数会随梯度剧烈波动，同样的 lr 一步可能「迈大」一步可能「迈小」。

#### 4.3.2 核心流程

理解 Muon 里「谱范数受控」的两处设计（本讲只讲语义，逐行推导在 u2-l2/u2-l3）：

```text
zeropower_via_newtonschulz5 内部:
  X = X / (X 的 Frobenius 范数 + 1e-7)   # 因为 谱范数 ≤ Frobenius 范数 → 除完谱范数 ≤ 1
  → 迭代后输出奇异值 ≈ [0.5, 1.5]        # 谱范数 ≤ 1.5 左右，有界

muon_update 内部:
  update *= max(1, m/n) ** 0.5            # 形状补偿因子，让不同形状矩阵的更新 RMS 可比
```

#### 4.3.3 源码精读

三处关键源码：

- [muon.py:L21-L22](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L21-L22)：注释写的是「Ensure spectral norm is at most 1」（保证谱范数至多 1），但代码除的却是 **Frobenius 范数**（`X.norm(dim=(-2, -1))` 对最后两维求的就是 Frobenius 范数）。这两件事不矛盾：由 \(\|X\|_2 \le \|X\|_F\)，除以 Frobenius 范数后谱范数必然 ≤ 1。用 Frobenius 是因为它一次 `norm` 就能算出，比精确谱范数便宜得多——这是「用便宜的上界达到目的」的典型工程手法。

- [muon.py:L61](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L61)：`lr: The learning rate, in units of spectral norm per update.`——学习率的单位是**每次更新的谱范数**。这是理解 Muon 调参（u3-l4）的钥匙：Muon 组的 lr（默认 0.02）与 AdamW 组的 lr（默认 3e-4）量纲不同，不可直接比较，u1-l3 已强调过两者相差近两个数量级。

- [muon.py:L40](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L40)：`update *= max(1, update.size(-2) / update.size(-1))**0.5`——在正交化之后乘一个形状因子，让不同长宽比矩阵的更新 RMS 相互可比。它作用于 Frobenius/RMS 尺度，是 u2-l3 的主题，本讲只需知道它叠加在「谱范数 ≈ 1」这个基底之上。

#### 4.3.4 代码实践

1. **实践目标**：验证「正交化后更新的谱范数与梯度尺度无关」——这是 lr 语义稳定的直接原因。
2. **操作步骤**（**示例代码**，需先 `pip install git+https://github.com/KellerJordan/Muon`，见 u1-l2）：

```python
import torch
from muon import zeropower_via_newtonschulz5   # 项目导出的正交化函数

torch.manual_seed(0)
G = torch.randn(512, 256)

for scale in [0.01, 1.0, 100.0]:
    out = zeropower_via_newtonschulz5(G * scale, steps=5).float()
    s = torch.linalg.svdvals(out)
    print(f"scale={scale:>7}: 谱范数={s.max():.3f}  "
          f"奇异值范围=[{s.min():.3f}, {s.max():.3f}]  "
          f"Frobenius={out.norm():.2f}")
```

3. **需要观察的现象**：三行的谱范数、奇异值范围、Frobenius 范数**几乎完全相同**（bfloat16 舍入导致的微小差异除外）。
4. **预期结果**：谱范数都落在 1.5 左右以下（docstring 承诺奇异值 ∈ [0.5, 1.5]），且不随 `scale` 变化——因为 [muon.py:L22](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L22) 的归一化先除掉了全部尺度信息。对照组：换成 `G * scale` 直接当更新用，谱范数会随 `scale` 线性放大 10000 倍（0.01 → 100）。具体数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：512×256 的矩阵正交化后，Frobenius 范数是多少？不跑代码直接算。
**答案**：\(\sqrt{\min(m,n)} = \sqrt{256} = 16\)。因为 256 个奇异值全是 1，Frobenius 范数 = \(\sqrt{\sum \sigma_i^2} = \sqrt{256}\)。

**练习 2**：为什么 [muon.py:L21-L22](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L21-L22) 除以 Frobenius 范数就能「保证谱范数至多 1」？
**答案**：因为谱范数是「最大的一个奇异值」，Frobenius 范数是「所有奇异值的平方和开根」，部分永远不超过全体，\(\sigma_{\max} \le \|G\|_F\)。除以 Frobenius 范数后新的 Frobenius 范数 = 1，于是新的谱范数 ≤ 1。

**练习 3**：AdamW 的更新（逐元素约 ±1）的谱范数大概多大、可控吗？
**答案**：元素约为 ±O(1) 的随机符号矩阵，Frobenius 范数 ≈ \(\sqrt{mn}\)；谱范数取决于符号的「对齐程度」，介于 \(\sqrt{\max(m,n)}\)（随机符号）和 \(\sqrt{mn}\)（完全对齐成秩 1）之间，**没有一个由优化器保证的固定上界**。Muon 则把谱范数钉在 ≈1（至多 ~1.5）。

### 4.4 精确目标与实际输出：docstring 承诺的 \(US'V^\top\)

#### 4.4.1 概念说明

到这里，Muon 的「理想目标」已经清楚了：把动量更新替换为 \(UV^\top\)。但 [muon.py:L11-L13](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L11-L13) 诚实地说了：实际输出**不是** \(UV^\top\)，而是

\[ US'V^\top, \qquad S'_{ii} \sim \text{Uniform}(0.5,\ 1.5) \]

即：**方向结构（\(U\)、\(V\)）保留，奇异值不精确等于 1，而是散布在 0.5～1.5 之间的近似均匀分布**。docstring 还补了一句重要的经验结论：相对于精确的 \(UV^\top\)，这「一点都不伤害模型性能」（turns out not to hurt model performance at all）。

为什么允许这么糙？

- 精确 SVD 对每一步、每一层都跑一遍，在 GPU 上太慢，而且 bfloat16 下数值不稳（见 [muon.py:L52-L53](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L52-L53) 的动机陈述）。
- 关键性质「谱范数有界（≤ 1.5 左右）、各方向步长同量级」在 \(S' \in [0.5, 1.5]\) 时依然成立——lr 的谱范数语义没有被破坏。
- 为了把迭代压到默认 5 步，作者选择的系数甚至故意让零点附近斜率超过「能完全收敛」的临界点（[muon.py:L9-L10](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L9-L10)），代价就是奇异值停在 [0.5, 1.5] 而非全部到达 1。**加速与精度在这里被显式交易**，系数推导属于 u2-l2，数值实验属于 u4-l1。

一个反直觉的推论：**多迭代几步并不会让奇异值全部收敛到 1**——因为迭代映射在这组系数下没有稳定的不动点 1。这就是为什么本讲实践的对照实验用 `steps=10` 时，输出奇异值依然散布在 [0.5, 1.5] 附近而不是全 1。

#### 4.4.2 核心流程

本讲视角下的「目标 vs 实际」对照：

| | 奇异值 | 谱范数 | Frobenius 范数 | 计算成本 |
| --- | --- | --- | --- | --- |
| 精确正交化 \(UV^\top\) | 全部 = 1 | = 1 | = \(\sqrt{\min(m,n)}\) | 每步 SVD：GPU 上慢、bf16 不稳 |
| Newton-Schulz 5 步（实际） | ≈ Uniform(0.5, 1.5) | ≤ ~1.5 | ≈ \(\sqrt{r} \cdot\)（1 附近波动） | 每步 3 次矩阵乘，快且 bf16 稳定 |

两者共享的本质：**谱范数被锁在一个与梯度尺度无关的小区间里，方向结构保留**。这正是「lr 以谱范数为单位」所需的全部性质。

#### 4.4.3 源码精读

- [muon.py:L6-L14](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L6-L14)：完整 docstring。第一句给出函数的数学身份：「用 Newton-Schulz 迭代计算 \(G\) 的零次幂 / 正交化」——矩阵的「零次幂」就是把奇异值全部映到 1（\(\Sigma^0 = I\)），这正是 \(UV^\top\) 的另一个名字。

- [muon.py:L9-L10](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L9-L10)：选择五次迭代系数以最大化零点斜率，甚至「超过迭代不再完全收敛」的临界点也在所不惜——这是输出奇异值不精确为 1 的直接原因。

- [muon.py:L16](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L16)：三个系数 `a, b, c = (3.4445, -4.7750, 2.0315)`。u2-l2 会推导它们如何作用于奇异值；本讲只需记住「系数决定了奇异值最终停在 [0.5, 1.5]」。

- [muon.py:L24-L27](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L24-L27)：迭代本体 `A = X @ X.mT; B = b*A + c*A@A; X = a*X + B@X`。注意每个迭代步都是「\(X\) 的多项式乘 \(X\)」的形式——它能保持输入的奇异向量不变，只变换奇异值（这是实践 2 的理论依据），完整分析在 u2-l2。

- 返回值（[muon.py:L29-L31](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L29-L31)）：高瘦输入会先转置迭代、最后转置回来，输出形状与输入一致；由于 [muon.py:L17](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L17) 的 `X = G.bfloat16()`，**返回的张量是 bfloat16**——做数值测量前记得 `.float()`。

#### 4.4.4 代码实践（本讲主实践）

这是本讲规格中指定的完整验证实验。

1. **实践目标**：用数值实验同时验证 (a) \(UV^\top\) 是奇异值全 1 的半正交矩阵；(b) Newton-Schulz 输出 \(US'V^\top\) 的奇异值散布在 [0.5, 1.5]；(c) 两者在「谱范数有界、与梯度尺度无关」上等效。

2. **操作步骤**（**示例代码**）：

```python
import torch
from muon import zeropower_via_newtonschulz5

torch.manual_seed(0)
G = torch.randn(512, 256)

# --- (a) 精确正交化: Q = U V^T ---
U, S, Vh = torch.linalg.svd(G, full_matrices=False)
Q = U @ Vh
s_exact = torch.linalg.svdvals(Q)
print("精确正交化: 正交性误差 |Q^T Q - I| =",
      (Q.mT @ Q - torch.eye(256)).norm().item())
print(f"精确正交化: 奇异值 [{s_exact.min():.4f}, {s_exact.max():.4f}]"
      f"  Frobenius={Q.norm():.3f}  谱范数={s_exact.max():.4f}")

# --- (b) Newton-Schulz 输出: US'V^T ---
out = zeropower_via_newtonschulz5(G, steps=10).float()   # 注意转回 float32
s_ns = torch.linalg.svdvals(out)
print(f"NS(steps=10): 奇异值 [{s_ns.min():.4f}, {s_ns.max():.4f}]"
      f"  Frobenius={out.norm():.3f}  谱范数={s_ns.max():.4f}")

# --- (c) 奇异值分布对比: 分箱统计 ---
bins = [0, 0.5, 0.7, 0.9, 1.1, 1.3, 1.5, float("inf")]
hist = torch.histogram(s_ns, bins=torch.tensor(bins)).hist
print("NS 奇异值分箱计数:", hist.tolist())

# --- (d) 正交性对比: NS 输出的 Q^T Q 偏离 I 多远 ---
print("NS 输出 |Q^T Q - I| =",
      (out.mT @ out - torch.eye(256)).norm().item())
```

3. **需要观察的现象**：
   - (a) 精确版：正交性误差接近 0，奇异值全部为 1.0000，Frobenius = 16.000，谱范数 = 1.0000（这些是数学必然而非巧合）。
   - (b) NS 版：奇异值散布、Frobenius 范数在 \(\sqrt{256}=16\) 附近波动，谱范数大约不超过 1.5。
   - (c) 分箱直方图大致均匀铺在 [0.5, 1.5] 上（docstring 的承诺）。
   - (d) NS 输出的 `|Q^T Q - I|` 明显大于精确版——它不是严格正交矩阵，但「足够正交」。
4. **预期结果**：如上；同时注意 `steps=10` 时奇异值**并没有**全部收敛到 1（见 4.4.1 的解释）。各项具体数值**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：如果 Newton-Schulz 输出的是 \(US'V^\top\)，\(S'_{ii} \in [0.5, 1.5]\)，它的谱范数上界是多少？lr=0.02 时一步更新对层输出的最大扰动是多少？
**答案**：谱范数 = \(\max_i S'_{ii} \le 1.5\)。由 4.3 节的 \(\|\Delta y\| \le \sigma_{\max}(\Delta W)\|x\|\)，再考虑 [muon.py:L40](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L40) 的形状因子（512×256 时为 \(\sqrt{512/256}=\sqrt{2}\)），一步扰动的量级为 \(0.02 \times 1.5 \times \sqrt{2} \times \|x\| \approx 0.042\|x\|\)。

**练习 2**：`zeropower_via_newtonschulz5` 为什么叫「零次幂」？
**答案**：矩阵幂的经典定义 \(G^0 = I\) 对非方阵没有意义；推广的「零次幂」指对奇异值取 0 次幂：\(G = U\Sigma V^\top \mapsto U\Sigma^0 V^\top = UV^\top\)（\(\sigma^0 = 1\)，对 \(\sigma > 0\)）。正交化 = 奇异值的零次幂。

**练习 3**：`out.mT @ out` 为什么会偏离 \(I\)？偏离量与 \(S'\) 有什么关系？
**答案**：\((US'V^\top)^\top(US'V^\top) = V S'^2 V^\top\)，只有 \(S' = I\) 时才是 \(I\)。\(S'\) 的对角元在 [0.5, 1.5] 波动，\(S'^2\) 的对角元就在 [0.25, 2.25] 波动——偏离完全来自未拉平的奇异值。

## 5. 综合实践

**任务：搭建一个 30 行的「正交化实验室」，回答一个问题——梯度越「偏科」，正交化的改变越大吗？**

对同一形状（512×256）构造三种「梯度」：

1. **随机梯度**：`torch.randn(512, 256)`；
2. **低秩梯度**：`A = torch.randn(512, 32) @ torch.randn(32, 256)`（秩约 32）；
3. **单方向垄断梯度**：随机梯度加上 `50 * u @ v.mT`（u、v 是随机单位向量，模拟一个奇异值 ≈ 50 的方向）。

对每种梯度分别计算：

- 原始矩阵的奇异值范围、谱范数、Frobenius 范数；
- 精确正交化 \(Q = UV^\top\) 的 Frobenius 范数（理论值恒为 16，用于自检代码正确性）；
- Newton-Schulz 输出的奇异值范围与谱范数（记得 `.float()`）。

把结果整理成一张表，然后回答三个问题：

- 为什么低秩梯度的 Frobenius 范数远小于 16，正交化后却恒为 16？（提示：零奇异值的方向也被拉平为 1，见练习 4.2.5-3）
- 三种梯度的 Newton-Schulz 输出谱范数是否都被压进了 ≈1.5 以内？这是「lr 语义与梯度内容无关」的直接证据。
- 第 3 种「垄断梯度」正交化后，原来那个超强方向还特殊吗？（答案：不再特殊，只保留方向配对、丢掉强度。）

本实践全部可在 CPU 上完成；数值结果**待本地验证**，但表中的理论列（正交化后 Frobenius = 16、谱范数 = 1）可以先填。

## 6. 本讲小结

- 任意梯度矩阵可分解 \(G = U\Sigma V^\top\)（SVD）：\(U,V^\top\) 是方向结构，\(\Sigma\) 是各方向强度；`torch.linalg.svd(G, full_matrices=False)` 返回的 `U @ Vh` 直接给出正交化结果。
- **正交化 = 丢掉 \(\Sigma\)**：\(Q = UV^\top\) 的奇异值全为 1，保留方向配对、抹平强弱差异；对方阵它就是与 \(G\) 最近的正交矩阵（Muon docstring 的原话），对矩形矩阵是半正交矩阵。
- 两把尺子：Frobenius 范数 = 奇异值平方和开根（总能量），谱范数 = 最大奇异值（单方向最大拉伸）；正交化把前者钉在 \(\sqrt{\min(m,n)}\)、后者钉在 1。
- **lr 的单位是每次更新的谱范数**（[muon.py:L61](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L61)）：因为 \(\|\Delta y\| \le \sigma_{\max}(\Delta W)\|x\|\)，谱范数直接封顶一步训练对层输出的扰动。
- 实际实现不追求精确 \(UV^\top\)：Newton-Schulz 输出 \(US'V^\top\)，\(S'_{ii} \sim \text{Uniform}(0.5, 1.5)\)，谱范数依然有界、方向结构依然保留——用可控的精度损失换取速度和 bfloat16 稳定性。
- 即使增大 `steps`，输出奇异值也不会全部到 1——系数为了 5 步收敛故意「过冲」，这是下一讲的入口。

## 7. 下一步学习建议

下一讲 **u2-l2「zeropower_via_newtonschulz5 逐行精读」**将回答本讲悬置的所有问题：如何不调用 SVD、只用矩阵乘法迭代出近似正交矩阵；五次迭代 \(X \leftarrow aX + (bA + cA^2)X\)（[muon.py:L24-L27](https://github.com/KellerJordan/Muon/blob/f98f1cacc0263b04290753e32be8d498c1efc806/muon.py#L24-L27)）每一步对奇异值做了什么变换；系数 \((3.4445, -4.7750, 2.0315)\) 如何把奇异值送进 [0.5, 1.5]；以及为什么先转置、为什么用 bfloat16。

建议带着本讲的两个结论去读：迭代保持奇异向量不变（4.4.3）、归一化保证谱范数 ≤ 1（4.3.3）。之后 u2-l3 会回到 `muon_update`，把正交化嵌回完整的动量流水线。
