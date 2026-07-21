# 比较损失的数学原理与候选归一化

## 1. 本讲目标

在上一讲（u3-l1）里，我们把 `CompareTrainer.compute_loss` 的「域损失 + 比较损失」两段结构与乘性合成的**操作流程**讲清楚了。但对比较损失，u3-l1 只停留在操作层面：「先算每个候选的平均对数似然，softmax 归一化，再用边距掩码挑出可学习候选对」。本讲专门拆开这个黑盒，把每一步的数学含义推透。

学完本讲，你应当能够：

1. 从 token 级 NLL 一路推到 `prod_normalized`，说清为什么 `exp(prod)/sum(exp(prod))` 等价于「模型对各候选的预测相对质量分」。
2. 用广播减法一次性构造 `diff`（预测差）与 `rw_diff`（真实差）两个成对差分矩阵，并解释它们的反对称性。
3. 说清 0.2 与 0.3 两个边距阈值各自的角色、`aval` 掩码如何挑选「可学习样本对」，以及为什么这本质上是一个**带边距的成对排序（pairwise hinge ranking）损失**。
4. 用 numpy 从零复现 `compare_loss`，直观看到 `aval` 如何随训练收缩。

> 本讲是纯数学 + numpy 复现，**不需要 GPU**，也不重复 u3-l1 已讲的「logits reshape、域损失、乘性合成」。必要时会引用 u3-l1 的结论作为前置。

## 2. 前置知识

- **token 级 NLL 与因果 LM shift**：语言模型在每个位置预测下一个 token，损失是 \(-\log p(y_t\mid y_{<t})\)；实现上把 logits 错一位（`[..., :-1, :]` 预测 `[..., 1:]`）。`CrossEntropyLoss` 默认 `ignore_index=-100`，对 `-100` 位置返回 0 损失。这些在 u2-l7、u3-l1 已建立。
- **多候选数据与 collator 输出**：一条指令配 N 个候选，collator 把它们拍平成 `(batch×cand, L)`，并产出 `scores (batch, cand)`（真实质量分，最后一候选为参考答案 `Score=1`）。u3-l1 已讲透 reshape，本讲直接消费 `logits (batch, cand, L, V)`、`labels (batch, cand, L)`、`attention_mask (batch, cand, L)`、`scores (batch, cand)`。
- **softmax 与几何平均**：softmax 把一组实数压成概率；当 \(\bar{\ell}\) 是平均对数似然时，\(\exp(\bar{\ell})\) 等于各 token 概率的几何平均（的开方级别）。
- **成对排序损失（pairwise ranking / hinge）**：给定一对样本 \((i,j)\)，若 i 应优于 j，则要求 \(\text{score}(i)-\text{score}(j)\ge\text{margin}\)；违反时给一个与「违反程度」成正比的惩罚，满足后惩罚归零。本讲的 `compare_loss` 正是这一族。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [train/mle_scoring.py](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py) | 本讲全部数学的落地代码，重点是 `CompareTrainer.get_comp_loss`（L179–L209）与 `compare_loss`（L211–L217）|
| [train/scoring_data_sample.json](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/scoring_data_sample.json) | 真实 `Score` 取值范围与多候选样例，供 numpy 复现取真实参数 |

本讲不引入新文件，只深读 `mle_scoring.py` 里两个最「数学」的函数。所有引用行号以当前 HEAD（`b284707`）为准。

## 4. 核心概念与源码讲解

按「候选得分怎么来」→「得分之间怎么比」→「哪些对值得罚」的顺序，拆三个最小模块。

### 4.1 get_comp_loss：从 token 级 NLL 到候选归一化得分

#### 4.1.1 概念说明

比较损失的前提是：**模型对每个候选要有一个「打分」**，且这个打分能在候选之间比较。`get_comp_loss` 的第一职责就是产生这个打分。

最自然的「模型对一段代码的打分」是**模型生成这段代码的对数似然**——模型越觉得这段代码「顺理成章」，似然越高，我们也就认为模型判断它质量越好。但不同候选长度不同，直接比总似然会偏向长候选，所以要按 token 归一化。`get_comp_loss` 干的就是这件事：算每个候选的（归一化）平均对数似然，再用 softmax 压成一个「相对得分」分布。

为什么用 softmax 而不是直接比平均对数似然？因为下一步 `compare_loss` 要把「预测得分」和「真实 `Score`（0~1）」放在同一尺度上比。softmax 把任意实数压成一组和为 1 的概率，正好与「4 个候选瓜分总量 1」对齐，便于在同一尺度上设阈值（0.3）。

#### 4.1.2 核心流程（数学推导）

固定一条指令（一个 `batch_id`），它有 \(C\) 个候选。对候选 \(i\)：

1. **token 级 NLL**。设响应段 token 为 \(y_1,\dots,y_T\)（指令段被 `-100` 屏蔽，不产生损失）。每个位置的条件 NLL：
   \[
   \ell_t = -\log p_\theta(y_t\mid y_{<t},\,\text{query})
   \]
   代码里 `CrossEntropyLoss(reduction="none")` 逐位置返回 \(\ell_t\)，指令/填充位返回 0。

2. **求和得总 NLL**。`loss.sum(dim=1)` 把响应段所有 \(\ell_t\) 加起来：
   \[
   \sum_{t=1}^{T}\ell_t = -\log p_\theta(\text{response}_i\mid\text{query})
   \]
   即整段响应的负对数似然。

3. **取负、除以长度得「归一化对数似然」** `prod`：
   \[
   \text{prod}_i = -\frac{\sum_t \ell_t}{L_{\text{total}}}
   = \frac{\log p_\theta(\text{response}_i\mid\text{query})}{L_{\text{total}}}
   \]
   这里 \(L_{\text{total}}\) 是 `mask.sum(-1)`，即**整条序列**（指令+响应+eos）的真实 token 数——分子只累加响应段（指令段贡献 0），分母却是全长。这是代码的一个细节：它不是严格的「每响应 token 平均」，而是用全长归一化。由于指令在同一条样本的所有候选间共享（长度恒定），这个常数偏置对「同一指令内排序」影响有限，但会改变不同长度候选之间的相对差距（详见 4.1.5 练习 3）。

4. **softmax 归一化**得预测相对得分 \(\hat{s}_i\)：
   \[
   \hat{s}_i = \frac{\exp(\text{prod}_i)}{\sum_{j=1}^{C}\exp(\text{prod}_j)}
   \]
   这正是代码里的 `prod_normalized`。

**几何直觉**：\(\exp(\text{prod}_i)=\exp\!\big(\tfrac{1}{L_{\text{total}}}\sum_t\log p(y_t)\big)=\big(\prod_t p(y_t)\big)^{1/L_{\text{total}}}\)，即响应段各 token 概率乘积按全长开根——可看作（以全长归一的）**几何平均似然**。几何平均对「个别极小概率 token」非常敏感：代码里若有一个 token 模型完全猜错，\(\prod_t p(y_t)\) 会被狠狠拉低，候选得分随之骤降。这恰好符合直觉——「出现一个胡乱 token」的代码质量更差。

**尺度不变性**：softmax 对整体平移不变（所有 \(\text{prod}\) 同加一个常数，\(\hat{s}\) 不变），所以只有候选间 \(\text{prod}\) 的**差**起作用：
\[
\frac{\hat{s}_i}{\hat{s}_j}=\exp(\text{prod}_i-\text{prod}_j)
\]
这个「差」正是下一节 `diff` 矩阵的基础。

#### 4.1.3 源码精读

逐候选算 NLL 并归一化的核心：

[train/mle_scoring.py:186-201](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L186-L201) —— 外层按 batch、内层按候选遍历；对每个候选算 token 级 NLL（错位 shift + `reduction="none"`），乘 mask 屏蔽填充后求和，取负除以 token 数得 `prod`；最后 `exp` 归一化成 `prod_normalized`。

关键几行单独看：

[train/mle_scoring.py:193-199](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L193-L199) —— token 级 NLL + mask + 求和 + 取负除以长度。注意 L198 的 `loss[:, -label.size(1):]` 是一个「保持全部列」的切片（`loss` 有 \(L-1\) 列、`label` 有 \(L\) 列，取后 \(L\) 列等于全留），功能上等价于 `loss.sum(dim=1)`，得到响应段总 NLL。

```python
loss = torch.nn.CrossEntropyLoss(reduction="none")(
    logit[..., :-1, :].contiguous().view(-1, logit.size(-1)),
    label[..., 1:].contiguous().view(-1),
).view(label.size(0), label.size(-1) - 1)
loss = loss * mask[..., 1:].contiguous()        # 屏蔽填充位
loss = loss[:, -label.size(1):].sum(dim=1)      # 响应段总 NLL（切片实际全留）
prod.append(-loss / mask.sum(-1))               # 归一化对数似然 prod
```

[train/mle_scoring.py:200-206](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L200-L206) —— 堆叠成 `prod_tensor (cand,)`，`exp` 归一化成 `prod_normalized`，再连同真实分送入 `compare_loss`。

```python
prod_tensor = torch.stack(prod)
prod_normalized = torch.exp(prod_tensor) / torch.sum(torch.exp(prod_tensor))
comp_loss = self.compare_loss(scores=prod_normalized, rw_scores=scores[batch_id].unsqueeze(0))
```

> 两个工程细节：① 这里 `torch.nn.CrossEntropyLoss(reduction="none")` 没有显式设 `ignore_index`，依赖默认 `-100` 跳过指令段；② `get_comp_loss` 外层逐 batch、内层逐候选的 Python 循环是串行的，候选多时较慢——这是 u3-l3 梯度切分顺带要优化的对象之一。

#### 4.1.4 代码实践

**目标**：亲手算一次 `prod_normalized`，验证它是和为 1 的分布，且只取决于 `prod` 的相对差。

**步骤**：

1. 用 numpy 构造 4 个候选的假归一化对数似然 `prod = [-2.0, -1.5, -3.0, -1.0]`（越接近 0 越「自然」，所以第 4 个候选模型最看好）。
2. 实现 `softmax(x)=exp(x)/sum(exp(x))`，打印 `prod_normalized`。
3. 把 `prod` 整体加 10，再算一次，对比两次结果是否相同（验证平移不变性）。

```python
# 示例代码：观察 prod_normalized 的归一化与平移不变性
import numpy as np
def softmax(x):
    e = np.exp(x - x.max())     # 减最大值防溢出，不改变结果
    return e / e.sum()
prod = np.array([-2.0, -1.5, -3.0, -1.0])
print(softmax(prod))            # 和为 1，第 4 项最大
print(softmax(prod + 10))       # 与上一行完全相同
```

**需要观察的现象**：两次输出逐位相同；第 4 个候选（`prod=-1.0` 最大）拿到最大的 `prod_normalized`，但其数值未必很大（4 个候选时最大也就零点几）。

**预期结果**：手算可得 `softmax([-2,-1.5,-3,-1])` 中各项分母为 \(\sum\exp=0.1353+0.2231+0.0498+0.3679=0.7761\)，于是 \(\hat{s}\approx[0.174,\,0.288,\,0.064,\,0.474]\)。这印证一个关键事实：**softmax 把候选预测得分压在 0~1、总和为 1 的窄区间里，即便「最看好」的候选也往往到不了 0.5**——这正是后面 0.3 阈值设得不大的原因。运行脚本可逐位复现。

#### 4.1.5 小练习与答案

1. **问**：为什么 `prod` 要取负号？如果不取负号直接 softmax 会怎样？
   **答**：NLL 越小越好，取负号变成「对数似然」越大越好，softmax 后「越好的候选」拿到越大的 \(\hat{s}\)。若不取负号，方向会反——最差的候选反而得分最高，后续排序全反。

2. **问**：为什么 \(\exp(\text{prod}_i)\) 可以理解为「几何平均似然」？
   **答**：\(\text{prod}_i=\tfrac{1}{L_{\text{total}}}\sum_t\log p(y_t)\)，所以 \(\exp(\text{prod}_i)=\big(\prod_t p(y_t)\big)^{1/L_{\text{total}}}\)，是各 token 概率乘积按全长开根。几何平均对「个别极小概率 token」敏感——代码里若有一个 token 模型完全猜错，几何平均会被狠狠拉低。

3. **问**：分子只累加响应段、分母却用全长，会带来什么偏差？
   **答**：等价于把「每响应 token 平均对数似然」再乘以 \(\tfrac{\text{响应长度}}{\text{全长}}\)。响应越短的候选，这个系数越小，`prod` 被压得越负。所幸同一条指令的指令段长度恒定，对「同一指令内 4 个候选的相对排序」影响是二阶的；但跨样本比较时需留意该归一化方式。

### 4.2 compare_loss：成对差分矩阵与带边距排序

#### 4.2.1 概念说明

有了预测得分 \(\hat{s}\)（4.1）和真实分 \(r\)（数据里的 `Score`），下一步是**让预测排序去逼近真实排序**。`compare_loss` 用的是**成对（pairwise）**思路：不要求预测分绝对等于真实分，只要求「凡是真实分明显更高的候选，预测分也要更高，且高出足够多」。

成对思路的核心工具是**差分矩阵**：把所有候选两两相减，得到一张 \(C\times C\) 的表，\((i,j)\) 位置就是「i 比 j 高多少」。预测有一张表（`diff`），真实有一张表（`rw_diff`），对照两张表就能找出「真实该赢、但预测赢得不够」的那些对，施以惩罚。代码用一行广播减法就把整张表算出来，这是 PyTorch 写「全对差分」的标准范式。

#### 4.2.2 核心流程（数学推导）

设候选数 \(C\)，预测得分向量 \(\hat{s}\in\mathbb{R}^C\)，真实分向量 \(r\in\mathbb{R}^C\)。

1. **广播减法构造差分矩阵**。代码用 `unsqueeze(1) - unsqueeze(-1)` 一次性算出全部两两差：
   \[
   D_{ij}=\hat{s}_i-\hat{s}_j,\qquad R_{ij}=r_i-r_j
   \]
   两者都是 \(C\times C\) 矩阵，且**反对称**：\(D_{ji}=-D_{ij}\)，对角线为 0。

2. **挑「可学习」的候选对**（掩码 `aval`）：
   \[
   \text{aval}_{ij}=\big(R_{ij}>0.2\big)\ \wedge\ \big(D_{ij}<0.3\big)
   \]
   - \(R_{ij}>0.2\)：真实分 i 比 j 高出**超过 0.2**——只有真实差距「够大」才值得施压（过滤掉真实分接近的噪声对）。
   - \(D_{ij}<0.3\)：预测分 i 比 j 高出**不足 0.3**——模型还没把这对分开到目标间距。

3. **带边距惩罚**：
   \[
   \mathcal{L}_{\text{comp}}=-\sum_{(i,j)\in\text{aval}}(D_{ij}-0.3)
   =\sum_{(i,j)\in\text{aval}}(0.3-D_{ij})
   \]
   在 `aval` 上 \(D_{ij}<0.3\)，故 \(0.3-D_{ij}>0\)，损失为正。最小化它会把 \(D_{ij}\) 往 0.3 推；一旦 \(D_{ij}\ge 0.3\)，该对离开 `aval`，惩罚归零。

这就是一个**带边距的成对排序损失**（pairwise hinge ranking loss）：对每个「i 真实优于 j 且差距 \(>0.2\)」的对，要求预测领先 \(\ge 0.3\)。

**方向正确性**：在 aval 上 \(\partial\mathcal{L}/\partial D_{ij}=-1\)，梯度下降会增大 \(D_{ij}\)，即抬高 \(\hat{s}_i\) 压低 \(\hat{s}_j\)——与「i 更好」一致。

**不重复计数**：由于 \(R\) 反对称，\(R_{ij}>0.2\) 与 \(R_{ji}>0.2\) 不可能同时成立，所以每个无序对至多有一个方向进入 `aval`，不会把同一对罚两遍。

#### 4.2.3 源码精读

差分矩阵与掩码就这几行：

[train/mle_scoring.py:211-217](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L211-L217) —— `new_scores` 把 \(\hat{s}\) 整理成 `(1, C)`；`unsqueeze(1) - unsqueeze(-1)` 用广播生成 \(C\times C\) 差分矩阵；`aval` 同时卡两个阈值；返回对被选中元素的带边距求和。

```python
def compare_loss(self, scores, rw_scores):
    cand = rw_scores.shape[1]
    new_scores = scores.reshape(-1, cand)
    diff = new_scores.unsqueeze(1) - new_scores.unsqueeze(-1)    # 预测差矩阵 D
    rw_diff = rw_scores.unsqueeze(1) - rw_scores.unsqueeze(-1)   # 真实差矩阵 R
    aval = torch.bitwise_and(rw_diff - 0.2 > 0, diff - 0.3 < 0)  # 可学习候选对
    return -(diff[aval] - 0.3).sum()
```

> 广播细节：`new_scores.unsqueeze(1)` 形状 `(1,1,C)`（列广播），`new_scores.unsqueeze(-1)` 形状 `(1,C,1)`（行广播），相减得到 `(1,C,C)`，元素 \((i,j)\) 正是 \(\hat{s}_i-\hat{s}_j\)。这是 PyTorch 写「全对差分」的标准一行写法，比双层 for 循环既快又简洁。

#### 4.2.4 代码实践

**目标**：用 numpy 复现 `compare_loss` 的核心三步，打印中间矩阵，直观看到 `aval` 如何挑对。完整带真实数据的脚本见第 5 节，这里先看核心：

```python
# 示例代码：compare_loss 的 numpy 核心
import numpy as np
def compare_loss_np(scores, rw_scores):
    diff = scores[:, None] - scores[None, :]           # 预测差 D (C,C)
    rw_diff = rw_scores[:, None] - rw_scores[None, :]  # 真实差 R (C,C)
    aval = (rw_diff - 0.2 > 0) & (diff - 0.3 < 0)      # 可学习对
    return -((diff[aval] - 0.3).sum()), aval
```

**需要观察的现象**：把一组「模型还没学好」的 `scores`（接近均匀，如 `[0.3,0.25,0.2,0.25]`）和真实分 `[0.4,0.35,0.0,1.0]` 喂进去，`aval` 会选中多个对、损失为正；把 `scores` 改成已学好（把最高真实分候选的预测分拉高到甩开其余），`aval` 中大部分对消失、损失接近 0。

**预期结果**：训练初期 `aval` 非空、损失 \(>0\)；模型把预测领先拉开到 0.3 后，`aval` 逐步变空——这就是「可学习边距」的自收缩行为。具体数值待本地用第 5 节脚本验证。

#### 4.2.5 小练习与答案

1. **问**：`diff` 和 `rw_diff` 为什么都是反对称的？这对损失有什么好处？
   **答**：因为它们由「同一个向量两两相减」得到，\(D_{ji}=-(D_{ij})\)。好处是每个无序对只在一个方向上满足 \(R_{ij}>0.2\)，所以 `aval` 不会把同一对罚两遍，损失不会翻倍。

2. **问**：`return -(diff[aval]-0.3).sum()` 里的负号能否去掉、把 `0.3-diff` 改写？
   **答**：等价。\(-(D_{ij}-0.3)=0.3-D_{ij}\)，二者完全相同。作者写成 `-(diff[aval]-0.3)` 只是写法选择，数值与梯度都一致。

3. **问**：如果某对真实分差正好是 0.15（\(R_{ij}=0.15<0.2\)），它会进入 `aval` 吗？为什么这样设计？
   **答**：不会，因为 \(R_{ij}>0.2\) 不成立。设计意图是：真实分差很小时，排序本身意义不大（可能就是评分噪声），硬要模型拉开反而引入噪声。0.2 是「真实差距显著性」的过滤门槛。

### 4.3 边距阈值 0.2 / 0.3 与 aval 掩码的设计

#### 4.3.1 概念说明

`compare_loss` 里有两个「魔数」：0.2 作用在**真实差** `rw_diff` 上，0.3 作用在**预测差** `diff` 上。它们不是同一个东西的两次截断，而是**两个不同尺度上的两个不同角色**：

- **0.2 = 真实差距的显著性门槛（real-gap significance）**。只有当真实质量分差距 \(>0.2\)，才认为「i 确实比 j 好」值得训练。它过滤掉真实分接近的对，避免在评分噪声上浪费梯度。
- **0.3 = 预测领先的目标准距（target separation）**。要求模型把预测概率差距推到至少 0.3。达到后该对「毕业」，梯度停止——这是 hinge 损失的典型「满足即停」。

两者一起构成**「可学习边距」机制**：训练初期预测接近均匀（\(D\approx 0\)），几乎所有 \(R>0.2\) 的对都满足 \(D<0.3\)，`aval` 很大、损失活跃；随着训练，预测领先被拉开到 0.3，对子逐个「毕业」离开 `aval`，损失自然回落。`aval` 的大小本身就是「还剩多少排序没学好」的实时指标。

#### 4.3.2 为什么是 0.2 和 0.3 这两个数（尺度分析）

两个阈值各自落在自己尺度的「合理工作区」：

- **真实分尺度**：数据里 `Score` 是 0~1 的归一化分（见 [train/scoring_data_sample.json](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/scoring_data_sample.json)，参考答案为 1，差的候选可低到 0.0~0.4）。0.2 的差距意味着「比如 0.6 vs 0.4」这种可感知的质量差，足以区分「能编译 vs 编译不过」。比 0.2 小的差（如 0.41 vs 0.40）更像评分噪声，不应硬排序。
- **预测分尺度**：`prod_normalized` 是 4 个候选的 softmax，和为 1。均匀时各项 0.25、两两差约 0；完全拉开时最强项趋近 1。0.3 的领先意味着「最强候选拿到约 0.5 以上、明显甩开其余」——既非苛刻（4 候选时可达，见 4.1.4 中最强项约 0.47 已接近），又足以体现「模型有把握」。设太大（如 0.9）会逼模型过度自信、训练不稳；设太小（如 0.05）则几乎没有排序压力。

两个数不相等是合理的——它们本就在不同尺度上，没有理由取同一个值。

#### 4.3.3 源码精读

阈值与掩码集中在一行：

[train/mle_scoring.py:216-217](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L216-L217) —— `rw_diff-0.2>0` 即真实差 \(>0.2\)；`diff-0.3<0` 即预测差 \(<0.3\)；两者按位与得到可学习候选对布尔掩码 `aval`，再对被选中元素做带边距求和。

```python
aval = torch.bitwise_and(rw_diff - 0.2 > 0, diff - 0.3 < 0)
return -(diff[aval] - 0.3).sum()
```

> 注意 `aval` 是**随训练动态变化**的布尔张量——它不是固定的标签，而是「当前模型还违反哪些排序约束」的实时快照。这与固定权重的 MSE 回归式打分很不同：它只对「当前还不够好」的部分施压，已经满足的约束自动松手。

#### 4.3.4 代码实践

**目标**：观察 `aval` 如何随预测得分改善而收缩，体会「可学习边距」的自适应。

**步骤**（接 4.2.4 的 numpy 函数）：

1. 固定真实分 `rw = [0.4, 0.35, 0.0, 1.0]`（第 4 候选是参考答案，最优）。
2. 构造三组预测分，模拟训练三个阶段：
   - 初期（均匀）：`s0 = [0.25, 0.25, 0.25, 0.25]`
   - 中期：`s1 = [0.20, 0.20, 0.10, 0.50]`
   - 后期（已学好）：`s2 = [0.10, 0.10, 0.05, 0.75]`
3. 对每组算 `compare_loss_np`，打印 `aval.sum()`（被选中的对数）和损失值。

**需要观察的现象**：`aval.sum()` 从大变小最终接近 0；损失同步下降。

**预期结果**：初期 `aval` 选中多对、损失最大；后期第 4 候选预测领先 \(>0.3\)，多数对「毕业」，损失趋近 0。具体数值待本地验证。

#### 4.3.5 小练习与答案

1. **问**：把 0.3 调大到 0.8，训练行为会怎样变化？
   **答**：`aval` 更难清空（需预测领先到 0.8 才「毕业」），排序压力持续更久、更强，模型被迫更自信地区分候选；但也更易训练不稳、且可能过拟合评分噪声。反之调小则排序压力弱、收敛快但排序学得不充分。

2. **问**：把 0.2 调小到 0.0 会怎样？
   **答**：任何真实分有差距的对（哪怕 0.01）都会进入 `aval`，模型会在评分噪声上也硬排序，引入大量噪声梯度。0.2 的作用正是挡住这些「不值得学」的近 ties。

3. **问**：`aval` 是布尔掩码、不可导，梯度怎么回传到模型参数？
   **答**：`aval` 只决定「哪些对参与求和」（选择，不参与求导）；真正可导的是被选中元素上的 `-(diff-0.3)`。`diff` 由 `prod_normalized` 经 softmax 与对数似然连到模型 logits，梯度沿这条链回传。掩码本身是固定的选择开关，不产生梯度。

## 5. 综合实践

**任务**：用 numpy 从零复现 `get_comp_loss` 的归一化步 + `compare_loss` 的完整数学，喂入「假归一化对数似然 + 真实 `Score`」，打印 `prod_normalized`、`diff`、`rw_diff`、`aval` 与最终损失，验证它与 `mle_scoring.py` 的逻辑一致，并直观看到 `aval` 如何挑选可学习样本对。

```python
# 示例代码：纯 numpy 复现比较损失的完整数学
# 运行：python explore_compare_loss.py
# 无需 GPU / transformers / 真实模型，只需 numpy
import numpy as np

def get_comp_scores_np(prod):
    """等价 get_comp_loss 的归一化步：softmax(prod) 得预测相对得分。"""
    e = np.exp(prod - prod.max())
    return e / e.sum()

def compare_loss_np(scores, rw_scores):
    """等价 compare_loss：返回 (loss, aval_mask, diff, rw_diff)。"""
    diff = scores[:, None] - scores[None, :]            # 预测差矩阵 D
    rw_diff = rw_scores[:, None] - rw_scores[None, :]   # 真实差矩阵 R
    aval = (rw_diff - 0.2 > 0) & (diff - 0.3 < 0)       # 可学习候选对
    loss = -((diff[aval] - 0.3).sum())
    return loss, aval, diff, rw_diff

# 取自 scoring_data_sample.json 第 1 条的真实 Score（4 个候选）
rw_scores = np.array([0.4038, 0.3555, 0.0023, 1.0])     # 第 4 个是参考答案

# 假设模型当前对 4 个候选的「归一化对数似然」（第 4 个最看好）
prod = np.array([-2.30, -2.40, -3.50, -1.20])

scores = get_comp_scores_np(prod)
loss, aval, diff, rw_diff = compare_loss_np(scores, rw_scores)

np.set_printoptions(precision=3, suppress=True)
print("prod_normalized =", scores, " sum =", round(scores.sum(), 4))
print("rw_diff (真实差) =\n", rw_diff)
print("diff   (预测差) =\n", diff)
print("aval   (可学习对, True=施压) =\n", aval)
print("被选中的有向对数 =", int(aval.sum()))
print("compare_loss =", round(float(loss), 4))
```

**操作步骤**：

1. 把脚本存为项目根目录的 `explore_compare_loss.py`（**仅供本地观察，不要提交、不要放进 `train/`**）。
2. 运行 `python explore_compare_loss.py`。
3. 逐项对照源码：
   - `prod_normalized` 各项 \(>0\) 且和为 1——对应 [train/mle_scoring.py:201](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L201)。
   - `rw_diff` 对角线为 0、反对称——对照 [train/mle_scoring.py:215](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L215)。
   - `diff` 同上——对照 [train/mle_scoring.py:214](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L214)。
   - `aval` 只在「真实差 \(>0.2\) 且预测差 \(<0.3\)」处为 True——对照 [train/mle_scoring.py:216](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L216)。
   - `compare_loss` = 被选中对上 \((0.3-\text{diff})\) 之和——对照 [train/mle_scoring.py:217](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L217)。
4. 把 `prod` 里第 4 个候选改得更负（如 `-1.20` → `-2.20`，模型不再最看好参考答案），重跑，观察 `aval` 增多、损失变大；再改回更看好（如 `-0.50`），观察 `aval` 收缩、损失变小。

**需要观察的现象（含一组手算预期值）**：

给定上面的输入，手算可得：分母 \(\sum\exp(\text{prod})=0.1003+0.0907+0.0302+0.3012=0.5224\)，于是
\[
\text{prod\_normalized}\approx[0.192,\,0.174,\,0.058,\,0.577]
\]
对应差分矩阵里，参考答案（候选 3）对其余三者的预测差分别为 \(0.385,\,0.403,\,0.519\)，**均 \(>0.3\)**——所以参考答案相关的三对 \((3,0),(3,1),(3,2)\) 虽然真实差都 \(>0.2\)，却因预测已经分得够开而**不进入** `aval`。真正被选中的是中段两对 \((0,2)\) 与 \((1,2)\)：它们的真实差（\(0.40,\,0.35\)）\(>0.2\)，但预测差（\(0.134,\,0.116\)）\(<0.3\)。最终
\[
\text{compare\_loss}=(0.3-0.134)+(0.3-0.116)\approx 0.350
\]

- `prod_normalized` 第 4 项最大（参考答案），但只到约 0.58。
- `aval` 的 True 只出现在中段两对，参考答案相关对已「毕业」。
- 当模型把第 4 候选预测分进一步拉高（`prod` 更接近 0），中段对的预测差也可能越过 0.3，`aval` 继续收缩。

**预期结果**：脚本输出与上述手算自洽；调整 `prod` 可复现「训练初期损失大、`aval` 多；训练后期损失小、`aval` 空」的趋势。运行脚本可逐位确认。

> 进阶：把 `prod` 换成真实训练里 `get_comp_loss` 打印的 `prod_tensor`（取消 [train/mle_scoring.py:202-205](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L202-L205) 的注释 print），即可用本脚本核对真实模型每一步的比较损失，无需手算。

## 6. 本讲小结

- `get_comp_loss` 把每个候选的 token 级 NLL 求和取负、除以序列全长，得「归一化对数似然」`prod`；再 `exp` 归一化成 `prod_normalized`——一个和为 1 的预测相对得分分布，本质是模型对各候选「（以全长归一的）几何平均似然」的 softmax。
- `compare_loss` 用 `unsqueeze` 广播减法一次性构造预测差矩阵 `diff` 与真实差矩阵 `rw_diff`，两者都反对称，保证每个无序对至多罚一次。
- 两个阈值角色不同：**0.2** 卡在真实差上（显著性门槛，过滤评分噪声的近 ties）；**0.3** 卡在预测差上（目标准距，达到即「毕业」停梯度）。两者构成「可学习边距」机制：`aval` 随训练自动收缩。
- 损失 \(\mathcal{L}_{\text{comp}}=-\sum_{(i,j)\in\text{aval}}(D_{ij}-0.3)\) 是带边距的成对 hinge 排序损失；掩码 `aval` 只做选择不参与求导，梯度沿 softmax→对数似然→logits 回传，方向正确（推高更好候选的预测分）。
- 0.2 与 0.3 不相等是合理的：它们分属「真实分尺度」与「softmax 预测分尺度」两个不同量纲，各自落在合理工作区。

## 7. 下一步学习建议

- **u3-l3 梯度切分显存优化 `mle_scoring_grad_split.py`**：本讲的 `get_comp_loss` 用 Python 逐候选循环算 NLL、且整批候选一次性前向，显存压力大。下一讲看它如何覆写 `training_step`，把一次多候选前向拆成逐候选前向 + 表征梯度，让 `per_device_train_batch_size=1` 也能训练——比较损失的数学**完全不变**，变的只是「何时反传、反传多少」。
- **回头对照 u3-l1**：现在再读 [train/mle_scoring.py:219-256](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/train/mle_scoring.py#L219-L256) 的 `compute_loss`，应能一眼看清「域损失保底、比较损失纠排序、乘性合成」三件事各自的数学来源，而不再停留在操作描述。
- **扩展阅读**：本讲的成对 hinge 排序损失与推荐系统/信息检索里的 RankNet、pairwise hinge loss 同源；可对照阅读相关资料，理解「listwise / pairwise / pointwise」三种排序损失谱系中本方法的位置。
