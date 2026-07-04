# 布尔与集合型距离及加权

> 本讲承接 [u4-l1 向量距离函数族](u4-l1-vector-distance-functions.md)。上一篇讲的是「两个数值向量有多远」（欧氏、余弦、马氏……），本讲把镜头对准一类完全不同的输入：**布尔向量**（每个分量只有真/假，常用来表示「某特征在不在」「某词出现没出现」）。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚为什么布尔向量需要一套**独立于数值度量**的距离函数；
- 用一张 **2×2 列联表（四个计数 $c_{TT},c_{TF},c_{FT},c_{FF}$）** 统一描述 `hamming`、`jaccard`、`dice`、`yule`、`rogerstanimoto`、`russellrao`、`sokalsneath` 这七个度量的公式，并指出它们对「双零 $(0,0)$」处理方式的根本差异；
- 读懂 `distance.py` 中两个计数引擎 `_nbool_correspond_all` / `_nbool_correspond_ft_tf`，特别是它们**如何在一份代码里同时支持无权与加权**；
- 理解 `_validate_hamming_kwargs` 这类校验器，以及 `MetricInfo` 里 `types=['bool']` 约束对 `pdist`/`cdist` 调用路径的保护作用；
- 会用权重 `w` 改写 `hamming`/`jaccard` 的结果，并能解释结果为什么变化。

## 2. 前置知识

### 2.1 什么是布尔向量与「列联表」

设想你在比较两篇文章的词频表，但只关心「词出现 / 不出现」，于是把每篇文章编码成一个 0/1 向量。比较两个这样的向量 $u,v$，逐位看只有四种组合：

| $u_k$ | $v_k$ | 记号 | 含义 |
|:---:|:---:|:---:|:---|
| 1 | 1 | $c_{TT}$（代码 `ntt`） | 两者都「在」 |
| 1 | 0 | $c_{TF}$（代码 `ntf`） | $u$ 在、$v$ 不在 |
| 0 | 1 | $c_{FT}$（代码 `nft`） | $u$ 不在、$v$ 在 |
| 0 | 0 | $c_{FF}$（代码 `nff`） | 两者都「不在」 |

> **命名约定（重要）**：代码里的 `nXY` 表示「$u=X$ 且 $v=Y$ 的位置数」，第一个字母始终是 $u$ 的取值，第二个字母是 $v$ 的取值。文档里的 $c_{ij}$ 同理：$i$ 是 $u$ 的值、$j$ 是 $v$ 的值。所以 `ntf` = $u{=}1,v{=}0$ = 文档里的 $c_{TF}$。

这四个数就是一张 **2×2 列联表（contingency table）**。本讲七个度量的全部区别，都只是「这四个数怎么组合进分子分母」。

### 2.2 为什么不能直接用欧氏距离

你当然可以对 0/1 向量算欧氏距离，结果就是 $\sqrt{c_{TF}+c_{FT}}$。但在生态学、信息检索、分类器评估等领域，人们更关心：

- **双零 $(0,0)$ 算不算「相似」？** 两个稀疏向量大量位置都是 0，若把双零也当成「一致」，相似度会被虚高（生态学里叫 «double-zero problem»）。`jaccard`/`dice` 因此**完全无视双零**。
- **匹配的正例权重多大？** `dice` 给 $c_{TT}$ 两倍权重，`russellrao` 只认 $c_{TT}$。

数值度量回答不了这些问题，于是有了下面这族专门的布尔/集合度量。

### 2.3 加权的直觉

很多时候每个位置的「分量」不一样重。比如比较两个分类器在混淆矩阵上的差异，四个格子分别是 TP/FN/FP/TN 的**计数**，自然就该用计数当权重。加权推广就是把 $c_{ij}:=\#\{k:u_k{=}i,v_k{=}j\}$ 换成加权求和：

\[
\tilde c_{ij}:=\sum_{1\le k\le n,\;u_k=i,\;v_k=j} w_k
\]

本讲所有度量都接受可选权重 `w`，源码里统一约定 $w_k\ge 0$（见 `_validate_weights`）。

## 3. 本讲源码地图

本讲只读一个文件，但分四个角色：

| 文件 | 角色 | 关键符号 |
|---|---|---|
| [distance.py](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.py) | 全部实现 | 七个度量函数 + 两个计数引擎 + 校验器 + `MetricInfo` 注册 |

具体落点：

- **校验基座**：`_validate_vector`、`_validate_weights`（每个度量入口都先调它们）。
- **计数引擎**：`_nbool_correspond_all`（算全四计数）、`_nbool_correspond_ft_tf`（只算两个不一致计数）。
- **七个度量**：`hamming`、`jaccard`、`dice`、`yule`、`rogerstanimoto`、`russellrao`、`sokalsneath`。
- **注册表**：`MetricInfo` 与 `_METRIC_INFOS`，决定 `pdist`/`cdist` 字符串调度时如何选类型、做校验。

测试佐证取自 [tests/test_distance.py](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/tests/test_distance.py)。

## 4. 核心概念与源码讲解

### 4.1 列联表四计数 $c_{ij}$：布尔度量的共同语言

#### 4.1.1 概念说明

七个布尔度量看似公式各异，其实都建立在那张 2×2 列联表上。只要把 $u,v$ 压成四个非负整数 $c_{TT},c_{TF},c_{FT},c_{FF}$，所有公式都是它们的四则运算。理解本讲最高效的办法，就是**先把四计数算对，再套公式**。

四计数满足恒等式 $c_{TT}+c_{TF}+c_{FT}+c_{FF}=n$（向量长度）。后续你会看到：

- `hamming` 的分母就是 $n$，分子是 $c_{TF}+c_{FT}$；
- `jaccard`/`dice`/`sokalsneath` 的分母里**不含** $c_{FF}$（无视双零）；
- `russellrao` 反其道而行，把 $(0,0)$ 也当成「不一致」；
- `yule`/`rogerstanimoto` 把 $c_{FF}$ 显式写进公式。

#### 4.1.2 核心流程

给定等长布尔向量 $u,v$（可选权重 $w$），手算四计数的流程：

```text
对每个位置 k:
    若 u[k]==1 且 v[k]==1:  c_TT += w[k]  (无权时 w[k]=1)
    若 u[k]==1 且 v[k]==0:  c_TF += w[k]
    若 u[k]==0 且 v[k]==1:  c_FT += w[k]
    若 u[k]==0 且 v[k]==0:  c_FF += w[k]
```

用集合语言更直观：把 $u,v$ 看成两个集合（1 的位置集合）$U,V$，则

\[
c_{TT}=|U\cap V|,\quad c_{TF}=|U\setminus V|,\quad c_{FT}=|V\setminus U|,\quad c_{FF}=n-|U\cup V|.
\]

#### 4.1.3 源码精读

每个度量函数的第一行都是把输入交给 `_validate_vector`：

[distance.py:L289-L294](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.py#L289-L294) —— `_validate_vector` 用 `np.asarray(..., order='c')` 把输入转成 C 连续的一维数组，并强制 `ndim==1`（否则抛 `ValueError`）。这就是为什么本族函数都只吃一维向量、不接受批量（批量走 `pdist`/`cdist`）。

[distance.py:L297-L301](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.py#L297-L301) —— `_validate_weights` 在 `_validate_vector` 之上多一条校验：权重**必须非负**（`np.any(w < 0)` 即报错）。这是全族对权重的唯一硬约束。

四计数本身的代码实现放在 4.4 精读，这里先建立「四计数是一切之源」的视图。

#### 4.1.4 代码实践

**目标**：手算一组布尔向量的四计数，并列成列联表，为 4.2/4.3 套公式做准备。

**操作**：把下面这段「示例代码」存为 `contingency.py` 运行（你也可以纯手算）。

```python
# 示例代码：手算 2x2 列联表四计数
import numpy as np

u = np.array([1, 1, 0, 0, 1, 0])
v = np.array([1, 0, 0, 1, 1, 1])

ntt = int(np.sum((u == 1) & (v == 1)))   # 位置 0,4
ntf = int(np.sum((u == 1) & (v == 0)))   # 位置 1
nft = int(np.sum((u == 0) & (v == 1)))   # 位置 3,5
nff = int(np.sum((u == 0) & (v == 0)))   # 位置 2

print(f"ntt={ntt} ntf={ntf} nft={nft} nff={nff}  sum={ntt+ntf+nft+nff}")
```

**预期结果（按公式手算）**：`ntt=2 ntf=1 nft=2 nff=1  sum=6`。请把这组数记下来——4.2 和 4.3 的所有数值都从它推出。运行结果若与此不符，说明你对 `ntf`/`nft` 的方向理解反了（参见 2.1 的命名约定）。

#### 4.1.5 小练习与答案

**练习 1**　若 $u=v=[1,1,0]$，四个计数分别是多少？

**答**：$c_{TT}=2,c_{TF}=0,c_{FT}=0,c_{FF}=1$。

**练习 2**　有人把 `nft` 理解成「$u=0,v=1$」，错在哪？

**答**：错在把第一个字母当成 $v$。代码命名是 `nXY` = $u{=}X$ 且 $v{=}Y$，所以 `nft` 是 $u{=}0(\text{f}),v{=}1(\text{t})$，对应文档里的 $c_{FT}$。方向一旦搞反，`yule` 这种 $c_{TF}\cdot c_{FT}$ 对称的公式还好，但理解列联表就会乱。

---

### 4.2 hamming / jaccard / dice：差异占比与集合度量三件套

#### 4.2.1 概念说明

这三个是日常用得最多的布尔度量，都刻画「不一致占比」，但对 $(0,0)$ 态度不同：

- **`hamming`** ——「不一致位置占比」。它**不限于布尔**，对任意向量都按 $u_k\neq v_k$ 数差异，$(0,0)$ 和 $(1,1)$ 都算一致。
- **`jaccard`** ——集合意义上的「交并比」距离。**完全无视双零**：分母只有 $c_{TT}+c_{TF}+c_{FT}$。满足三角不等式，是真正的度量。自 1.15.0 起，非 0/1 数值会先转布尔（非零即真）再算。
- **`dice`** ——和 jaccard 同属「无视双零」一族，但分母给 $c_{TT}$ **两倍权重**，对共同正例更宽容。

三者的公式（用四计数表达）：

\[
\text{hamming}=\frac{c_{TF}+c_{FT}}{n},\qquad
\text{jaccard}=\frac{c_{TF}+c_{FT}}{c_{TT}+c_{TF}+c_{FT}},\qquad
\text{dice}=\frac{c_{TF}+c_{FT}}{2c_{TT}+c_{TF}+c_{FT}}.
\]

> 注意 `jaccard`/`dice` 的分母都不含 $c_{FF}$，这正是它们「集合度量」的本质——双零不提供相似性信息。

#### 4.2.2 核心流程

- `hamming(u,v,w)`：先算布尔差异掩码 `u_ne_v = (u != v)`。无权时直接 `np.mean(u_ne_v)`；有权时把权重**归一化到和为 1** 再做点积 `np.dot(u_ne_v, w/w.sum())`。
- `jaccard(u,v,w)`：用「非零」语义。`unequal = XOR(u!=0, v!=0)`（恰好一个非零），`nonzero = OR(u!=0, v!=0)`（至少一个非零）。返回 `unequal.sum()/nonzero.sum()`；若 `nonzero.sum()==0`（双零向量）返回 0。
- `dice(u,v,w)`：需要 $c_{TT}$（内联算）+ 两个不一致计数（调 `_nbool_correspond_ft_tf`），套 $\frac{c_{TF}+c_{FT}}{2c_{TT}+c_{TF}+c_{FT}}$。

加权时，`jaccard`/`dice` 的计数都被 $w_k$ 缩放（见 4.4），公式形式不变。

#### 4.2.3 源码精读

[distance.py:L720-L775](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.py#L720-L775) —— `hamming` 函数体。关键 6 行：

```python
u_ne_v = u != v
if w is not None:
    w = _validate_weights(w)
    ...
    w = w / w.sum()          # 权重归一化到和为 1
    return np.dot(u_ne_v, w) # 加权差异占比
return np.mean(u_ne_v)       # 无权：直接求均值
```

无权分支 `np.mean(u_ne_v)` 正是 $(c_{TF}+c_{FT})/n$；加权分支把 $w$ 归一化后点乘差异掩码，等价于 $\sum_k w_k[u_k\neq v_k]/\sum_k w_k$。注意 `hamming` 是本族里**唯一对非布尔数值也有明确定义**的（它只问「等不等」，如 `hamming([1,0,0],[2,0,0])==1/3`）。

[distance.py:L778-L897](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.py#L778-L897) —— `jaccard` 函数体，核心三行：

```python
unequal = np.bitwise_xor(u != 0, v != 0)   # 恰一个非零 → 分子
nonzero = np.bitwise_or(u != 0, v != 0)    # 至少一个非零 → 分母
...
return (a / b) if b != 0 else np.float64(0)
```

`u != 0` 这一步把任意数值先二值化（非零即真），所以 1.15.0 之后 `jaccard([1,0,0],[2,0,0])` 会把 2 当真值，得到 0（两向量视为相同），这与 `hamming` 截然不同。分母为 0（两向量全零）时返回 0，对应文档「both zero → dissimilarity 0」的约定。

[distance.py:L1343-L1405](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.py#L1343-L1405) —— `dice` 函数体。它先内联算 $c_{TT}$（带 `bool` 快路径），再调 `_nbool_correspond_ft_tf` 拿 $(c_{FT},c_{TF})$，最后：

```python
return float((ntf + nft) / np.array(2.0 * ntt + ntf + nft))
```

分母里 $2c_{TT}$ 是 dice 区别于 jaccard 的唯一一处。**注意**：`dice` 不做 `u!=0` 二值化，所以喂非布尔数值会得到无意义甚至负的结果——文档示例 `dice([1,0,0],[2,0,0])` 返回 `-0.333...` 就是这个原因（$c_{TT}=1\cdot2=2$ 把分母顶大、$c_{TF}=-1$ 把分子顶负）。这正是 4.4 要讲的 `types=['bool']` 约束想避免的陷阱。

#### 4.2.4 代码实践

**目标**：用 4.1 的同一对向量 $u=[1,1,0,0,1,0],\;v=[1,0,0,1,1,1]$（四计数 $c_{TT}=2,c_{TF}=1,c_{FT}=2,c_{FF}=1,n=6$），实测 `hamming`/`jaccard`/`dice` 并与手算对照。

**操作**：

```python
# 示例代码
from scipy.spatial import distance as d
u = [1, 1, 0, 0, 1, 0]
v = [1, 0, 0, 1, 1, 1]
print("hamming :", d.hamming(u, v))
print("jaccard :", d.jaccard(u, v))
print("dice    :", d.dice(u, v))
```

**预期结果（按公式手算，待本地验证）**：

| 度量 | 公式代入 | 期望值 |
|---|---|---|
| hamming | $(1+2)/6$ | `0.5` |
| jaccard | $(1+2)/(2+1+2)$ | `0.6` |
| dice | $(1+2)/(2\cdot2+1+2)=3/7$ | `0.4285714...` |

**现象观察**：三者分子相同（都是不一致计数 3），分母依次变小（6 → 5 → 7）。`dice` 分母最大是因为 $2c_{TT}=4$ 把共同正例算了两次。若三者数值不按此顺序，检查你的四计数方向。

#### 4.2.5 小练习与答案

**练习 1**　为什么对同一对稀疏布尔向量，`jaccard` 通常大于 `hamming`？

**答**：因为 `jaccard` 分母剔除了 $c_{FF}$（$n\to c_{TT}+c_{TF}+c_{FT}$），分母变小、分子不变，比值变大。稀疏向量双零多，剔除后差异占比被「放大」。

**练习 2**　`jaccard([1,0,0],[2,0,0])` 在 1.15.0 之后返回什么？为什么和 `hamming([1,0,0],[2,0,0])` 不同？

**答**：返回 `0.0`。因为 `jaccard` 先用 `u != 0` 把 1 和 2 都当成「真」，两向量二值化后完全相同。而 `hamming` 只比「等不等」，第一分量 $1\neq2$ 算一次差异，返回 $1/3$。

---

### 4.3 yule / rogerstanimoto / russellrao / sokalsneath：把负匹配也纳入考量

#### 4.3.1 概念说明

这一组的公式更「绕」，但只要盯住四计数就清晰了。共同点是它们要么显式用到 $c_{FF}$，要么对 $(0,0)$ 有特殊处理：

\[
\text{yule}=\frac{2\,c_{TF}c_{FT}}{c_{TT}c_{FF}+c_{TF}c_{FT}},\qquad
\text{rogerstanimoto}=\frac{2(c_{TF}+c_{FT})}{c_{TT}+c_{FF}+2(c_{TF}+c_{FT})},
\]
\[
\text{russellrao}=\frac{n-c_{TT}}{n},\qquad
\text{sokalsneath}=\frac{2(c_{TF}+c_{FT})}{c_{TT}+2(c_{TF}+c_{FT})}.
\]

逐个解读：

- **`yule`** ——分子是两个方向不一致计数的乘积 $c_{TF}c_{FT}$，分母还乘上 $c_{TT}c_{FF}$（一致的正负匹配）。它对「正相关」敏感：若 $u,v$ 高度一致（$c_{TF}$ 或 $c_{FT}$ 有一为 0），返回 0。
- **`rogerstanimoto`** ——把不一致计数**翻倍** $R=2(c_{TF}+c_{FT})$ 作分子，分母是全部四计数之和（$c_{TT}+c_{FF}+R$）。和 `hamming` 是近亲，但对不一致「加倍惩罚」。
- **`russellrao`** ——只认 $c_{TT}$ 为一致，**其余所有位置（包括双零 $(0,0)$）都算不一致**，所以是 $(n-c_{TT})/n$。最「严苛」的一个。
- **`sokalsneath`** ——和 `rogerstanimoto` 同样把不一致翻倍 $R$，但分母剔除 $c_{FF}$（$c_{TT}+R$），属「无视双零」一族。对全假向量未定义，会抛 `ValueError`。

#### 4.3.2 核心流程

- `yule` / `rogerstanimoto`：调 `_nbool_correspond_all` 拿全四计数，套公式。
- `russellrao`：**不调**任何 helper，内联算 $c_{TT}$，用 $n=\text{len}(u)$（有权时 $n=w.\text{sum}()$），返回 $(n-c_{TT})/n$。
- `sokalsneath`：内联算 $c_{TT}$ + 调 `_nbool_correspond_ft_tf` 拿 $(c_{FT},c_{TF})$；分母为 0（全假）时抛 `ValueError`。

四者都支持权重 `w`：`yule`/`rogerstanimoto` 经 `_nbool_correspond_all` 的加权路径；`russellrao` 把 $n$ 换成 $w.\text{sum}()$、$c_{TT}$ 换成 $\sum w_k u_k v_k$；`sokalsneath` 同理。

#### 4.3.3 源码精读

[distance.py:L1293-L1340](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.py#L1293-L1340) —— `yule` 函数体：

```python
(nff, nft, ntf, ntt) = _nbool_correspond_all(u, v, w=w)
half_R = ntf * nft
if half_R == 0:
    return 0.0
else:
    return float(2.0 * half_R / (ntt * nff + half_R))
```

注意它把 $R/2=c_{TF}c_{FT}$ 单独存成 `half_R`，既当早退条件（任一方向无不一致即为 0），又复用到分母，写得很紧凑。

[distance.py:L1408-L1455](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.py#L1408-L1455) —— `rogerstanimoto` 函数体：

```python
(nff, nft, ntf, ntt) = _nbool_correspond_all(u, v, w=w)
return float(2.0 * (ntf + nft)) / float(ntt + nff + (2.0 * (ntf + nft)))
```

分母是四计数全和，分子是不一致翻倍——可看成「加权版 hamming」。

[distance.py:L1458-L1512](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.py#L1458-L1512) —— `russellrao` 函数体。三分支（bool 无权 / 数值无权 / 有权）分别算 $c_{TT}$ 与 $n$：

```python
if u.dtype == v.dtype == bool and w is None:
    ntt = (u & v).sum();  n = float(len(u))
elif w is None:
    ntt = (u * v).sum();  n = float(len(u))
else:
    w = _validate_weights(w)
    ntt = (u * v * w).sum();  n = w.sum()
return float(n - ntt) / n
```

注意有权分支 $n=w.\text{sum}()$，所以加权 russellrao 仍是「加权不一致占总权重比」。

[distance.py:L1515-L1572](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.py#L1515-L1572) —— `sokalsneath` 函数体。唯一带显式异常的一个：

```python
denom = np.array(ntt + 2.0 * (ntf + nft))
if not denom.any():
    raise ValueError('Sokal-Sneath dissimilarity is not defined for '
                     'vectors that are entirely false.')
return float(2.0 * (ntf + nft)) / denom
```

全假向量 $c_{TT}=c_{TF}=c_{FT}=0$，分母为 0，故报错。这条边界由回归测试 [tests/test_distance.py:L1827-L1830](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/tests/test_distance.py#L1827-L1830) 钉住（ticket #876）。

#### 4.3.4 代码实践

**目标**：在同一对向量上把四个度量算全，体会「双零 $(0,0)$」如何分流这些公式。仍用 $c_{TT}=2,c_{TF}=1,c_{FT}=2,c_{FF}=1,n=6$。

**操作**：

```python
# 示例代码
from scipy.spatial import distance as d
u = [1, 1, 0, 0, 1, 0]
v = [1, 0, 0, 1, 1, 1]
for name in ["yule", "rogerstanimoto", "russellrao", "sokalsneath"]:
    print(f"{name:16s}:", getattr(d, name)(u, v))
```

**预期结果（按公式手算，待本地验证）**：

| 度量 | 公式代入 | 期望值 |
|---|---|---|
| yule | $2\cdot1\cdot2/(2\cdot1+1\cdot2)=4/4$ | `1.0` |
| rogerstanimoto | $2(1+2)/(2+1+2(1+2))=6/9$ | `0.6666666...` |
| russellrao | $(6-2)/6$ | `0.6666666...` |
| sokalsneath | $2(1+2)/(2+2(1+2))=6/8$ | `0.75` |

**现象观察**：`russellrao` 把唯一的 $(0,0)$（位置 2）也当成不一致，所以它和「无视双零」的 `jaccard`(0.6) 拉开差距（0.667）。再用全假向量 `[0,0,0],[0,0,0]` 喂 `sokalsneath`，应触发 `ValueError`。

#### 4.3.5 小练习与答案

**练习 1**　对完全相同的两向量 $u=v$，这四个度量分别返回什么？

**答**：$c_{TF}=c_{FT}=0$。`yule` 走 `half_R==0` 早退返回 `0.0`；`rogerstanimoto` 分子为 0 返回 `0.0`；`sokalsneath` 分子为 0 返回 `0.0`（分母 $c_{TT}\neq0$，不报错）；`russellrao` 返回 $(n-c_{TT})/n=(n-n)/n=0$。四者都为 0——一致即距离 0。

**练习 2**　为什么 `sokalsneath([False,False,False],[False,False,False])` 会抛异常，而 `russellrao` 不会？

**答**：全假时 $c_{TT}=0$。`sokalsneath` 分母 $c_{TT}+2(c_{TF}+c_{FT})=0$，数学上未定义，源码据此抛 `ValueError`；`russellrao` 分母是 $n$（与 $c_{TT}$ 无关），返回 $(n-0)/n=1.0$，定义良好。

---

### 4.4 计数引擎、加权实现与校验注册

#### 4.4.1 概念说明

4.2/4.3 里反复出现的两个 `_nbool_correspond_*` 函数，是本族的**计数引擎**：把「算四计数」这件事抽出来复用，并顺手解决加权。两个版本的区别只是返回多少个计数：

- `_nbool_correspond_all`：返回 `(nff, nft, ntf, ntt)` 全四计数——`yule`、`rogerstanimoto` 用（它们要 $c_{FF}$ 或 $c_{TT}$）。
- `_nbool_correspond_ft_tf`：只返回 `(nft, ntf)` 两个不一致计数——`dice`、`sokalsneath` 用（它们只需不一致计数，$c_{TT}$ 各自内联算）。

之所以拆两个，是**省算**：只算两个计数比算四个快，热路径上能少几次扫数组。

本模块还讲两件「工程性」的事：一是 `MetricInfo` 注册表用 `types=['bool']` 把布尔度量在 `pdist`/`cdist` 入口强制转 bool，避免 4.2 提到的「喂数值得到负值」陷阱；二是 `_validate_hamming_kwargs` 这类校验器如何在批量路径上校验权重。

#### 4.4.2 核心流程

`_nbool_correspond_all(u, v, w)` 的内部分支：

```text
若 u,v 都是 bool 且无权重:        # 快路径：位运算
    not_u, not_v = ~u, ~v
    用 & 统计 nff/nft/ntf/ntt
否则:                              # 慢路径：数值化 + 可选加权
    u, v = u.astype(数值), v.astype(数值)
    not_u, not_v = 1 - u, 1 - v
    若有权重 w:
        not_u = w * not_u          # 关键：把 w 只吸收进 u 侧
        u     = w * u
    nff = (not_u * not_v).sum()
    nft = (not_u * v    ).sum()
    ntf = (u     * not_v).sum()
    ntt = (u     * v    ).sum()
```

**加权的小技巧**：权重 $w_k$ 只乘到 $u$ 和 $\text{not}_u$ 上（即 $u$ 侧），$v$ 与 $\text{not}_v$ 不动。由于每个乘积里恰好含一个 $u$ 侧因子，结果自动等于 $w_k$ 乘以该格的无权计数——一份代码同时服务无权与加权。

注册侧：`pdist(X, 'yule')` 走 `MetricInfo` 调度，`types=['bool']` 触发 `_convert_to_type` 把 $X$ 转 bool，再调对应 `pdist_yule` 后端。

#### 4.4.3 源码精读

[distance.py:L142-L163](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.py#L142-L163) —— `_nbool_correspond_all`，全族计数核心。bool 快路径用按位 `&`/`~`（最省、最快）；慢路径里加权那段把 $w$ 吸进 $u$ 侧，是上面「小技巧」的落点。

[distance.py:L166-L183](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.py#L166-L183) —— `_nbool_correspond_ft_tf`，只算 `nft, ntf`，结构与 `_all` 完全平行，只是省掉 `nff`、`ntt`。

[distance.py:L215-L223](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.py#L215-L223) —— `_validate_hamming_kwargs`，注册给 `hamming` 的批量校验器。它把缺失的 `w` 补成 `ones(n)`（让 C 后端总能拿到显式权重数组），并校验长度与非负：

```python
w = kwargs.get('w', np.ones((n,), dtype='double'))
if w.ndim != 1 or w.shape[0] != n:
    raise ValueError(...)
kwargs['w'] = _validate_weights(w)
```

注意它和 [distance.py:L202-L212](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.py#L202-L212) `_validate_weight_with_size` 的区别：后者 `w is None` 时直接返回（不塞回 kwargs），前者总是塞回一个显式数组——因为 hamming 的 C 后端需要一个权重向量。

[distance.py:L1636-L1655](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.py#L1636-L1655) —— `MetricInfo` 数据类字段。其中 `types` 决定 `pdist`/`cdist` 允许的输入类型。

看几个布尔度量的注册条目：

- [distance.py:L1702-L1709](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.py#L1702-L1709) `dice`：`types=['bool']`。
- [distance.py:L1717-L1725](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.py#L1717-L1725) `hamming`：`types=['double', 'bool']`、`validator=_validate_hamming_kwargs`，且别名 `aka` 含 `'matching'`（`matching` 是 `hamming` 的同义词）。
- [distance.py:L1726-L1733](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.py#L1726-L1733) `jaccard`：`types=['double', 'bool']`。
- [distance.py:L1757-L1764](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.py#L1757-L1764) `rogerstanimoto`、[L1765-L1772](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.py#L1765-L1772) `russellrao`、[L1781-L1788](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.py#L1781-L1788) `sokalsneath`、[L1796-L1803](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.py#L1796-L1803) `yule`：都是 `types=['bool']`。

> **保护机制**：直接调 `dice([1,0,0],[2,0,0])` 会得到 `-0.333`（4.2 讲过的陷阱），但走 `pdist(X,'dice')` 时 `types=['bool']` 会先把 $X$ 转 bool，于是 `2` 变 `True`，结果合理。这就是「单独函数给最大自由（也最易踩坑）、批量入口加类型护栏」的分层设计。

#### 4.4.4 代码实践

**目标**：亲手验证两件事——(a) 加权如何改变 `hamming`；(b) `_nbool_correspond_all` 的「$w$ 吸进 $u$ 侧」技巧确实算出加权四计数。

**操作**：

```python
# 示例代码
import numpy as np
from scipy.spatial import distance as d

u = [1, 1, 0, 0, 1, 0]
v = [1, 0, 0, 1, 1, 1]

# (a) 无权 vs 加权 hamming
w = [10, 1, 1, 1, 1, 1]   # 给「一致」的位置 0 放大权重
print("hamming 无权:", d.hamming(u, v))
print("hamming 加权:", d.hamming(u, v, w))

# (b) 直接调用计数引擎（私有 API，仅供观察）
from scipy.spatial.distance import _nbool_correspond_all
print("加权四计数:", _nbool_correspond_all(
    np.array(u), np.array(v), w=np.array(w, dtype=float)))
```

**预期结果（按公式手算，待本地验证）**：

- (a) 无权 `hamming` = $3/6=0.5$。加权：不一致位置是 1、3、5（权重 1、1、1），总权重 $10+1+1+1+1+1=15$，故 $3/15=0.2$。加权把重权压在「一致」的位置 0 上，差异占比被稀释 $0.5\to0.2$。
- (b) 加权四计数应为：把每格的无权计数乘以该位置的 $w$ 后求和——$c_{TT}$ 在位置 0($w{=}10$)、4($w{=}1$) → $11$；$c_{TF}$ 在位置 1($w{=}1$) → $1$；$c_{FT}$ 在位置 3、5($w{=}1,1$) → $2$；$c_{FF}$ 在位置 2($w{=}1$) → $1$。即 `(nff=1, nft=2, ntf=1, ntt=11)`。

> 私有函数 `_nbool_correspond_all` 以 `_` 开头，随时可能改动，这里仅用于理解原理，**不要写进生产代码**。

#### 4.4.5 小练习与答案

**练习 1**　若把权重改成 $w=[1,10,1,1,1,1]$（重权压在「不一致」的位置 1），加权 `hamming` 会变大还是变小？

**答**：变大。不一致位置 1 现在权重 10，分子 $\sum w_k[u_k\neq v_k]=10+1+1=12$，分母 $\sum w_k=15$，$12/15=0.8 > 0.5$。加权让你能「放大」关心的位置。

**练习 2**　`_validate_hamming_kwargs` 为什么在 `w is None` 时仍塞一个 `ones(n)` 回 kwargs，而 `_validate_weight_with_size` 却直接返回？

**答**：`hamming` 的 C/pybind 批量后端需要一个显式权重向量才能统一计算，所以校验器必须补一个默认 `ones`；`_validate_weight_with_size` 服务的是那些「无权时后端走另一条无 `w` 参数的路径」的度量，缺省即等价于不传 `w`，故直接返回。两者对应后端对 `w` 的不同要求。

---

## 5. 综合实践

**任务**：用「混淆矩阵当权重」一次性比较两个分类器。

场景：分类器 A、B 在一个数据集上的 2×2 混淆矩阵给出了 TP、FN、FP、TN 四个计数。把四个计数编码成 4 维布尔向量，再用计数当权重，就能把整张混淆矩阵压成一个「分类器间相异度」。

**操作**：

```python
# 示例代码：混淆矩阵 → 加权布尔距离
from scipy.spatial import distance as d

# 四个位置分别代表 (TP, FN, FP, TN)
# A: TP 处为 1，FN/FP 处取决于该格归属；这里用经典编码
u = [1, 1, 0, 0]   # 代表「正类」位置：TP, FN 为 1；FP, TN 为 0
v = [1, 0, 1, 0]   # 代表「预测为正」位置：TP, FP 为 1；FN, TN 为 0
w = [31, 41, 59, 26]  # TP=31, FN=41, FP=59, TN=26（取自官方 jaccard 文档示例）

print("weighted jaccard:", d.jaccard(u, v, w))
print("weighted dice   :", d.dice(u, v, w))
print("weighted hamming:", d.hamming(u, v, w))
```

**要求**：

1. 先手算加权四计数 $\tilde c_{ij}$（提示：$\tilde c_{TT}=31,\tilde c_{TF}=41,\tilde c_{FT}=59,\tilde c_{FF}=26$），再套 4.2 的公式算出三个值。
2. 验证 `weighted jaccard` 是否等于 $(41+59)/(31+41+59)=100/131\approx0.7634$（这是 [distance.py:L778-L897](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.py#L778-L897) `jaccard` 文档里的官方示例值）。
3. 思考：为什么这里用 `jaccard`/`dice`（无视双零 TN）比用 `russellrao`（把 TN 也当不一致）更合理？把 `russellrao(u,v,w)` 也算出来对比。

这个练习把本讲三件事串起来：四计数列联表（4.1）→ 公式选择（4.2/4.3）→ 加权实现（4.4）。

## 6. 本讲小结

- 布尔度量的全部信息集中在 2×2 列联表四计数 $c_{TT},c_{TF},c_{FT},c_{FF}$（代码 `ntt/ntf/nft/nff`，命名「前 $u$ 后 $v$」）；先算对四计数，公式就只是四则运算。
- `hamming` 是唯一对非布尔数值也良定义的（数「等不等」）；`jaccard` 自 1.15.0 起把数值二值化；`dice`/`yule`/`rogerstanimoto`/`russellrao`/`sokalsneath` 喂数值会得到无意义/负值。
- 对「双零 $(0,0)$」的态度是核心分水岭：`jaccard`/`dice`/`sokalsneath` 无视它；`hamming`/`rogerstanimoto`/`yule` 纳入；`russellrao` 甚至把它当不一致。
- 两个计数引擎 `_nbool_correspond_all`（全四）/`_nbool_correspond_ft_tf`（仅不一致）复用计数逻辑，靠「把 $w$ 吸进 $u$ 侧」一份代码同时支持加权。
- `_validate_hamming_kwargs` 等校验器在批量路径补默认权重、校验非负；`MetricInfo.types=['bool']` 在 `pdist`/`cdist` 入口强制转 bool，是防止「喂数值得负值」的护栏。
- 加权让 `hamming`/`jaccard` 能按位置重要性伸缩：重权压在一致位置→距离变小，压在不一致位置→距离变大。

## 7. 下一步学习建议

- 想看这些度量如何被**批量**调用，进入 [u4-l3 pdist、cdist 与 squareform](u4-l3-pdist-cdist-squareform.md)：`MetricInfo`/`_METRIC_ALIAS` 如何把字符串 `'jaccard'` 调度到 `pdist_jaccard` 后端，正是本讲 4.4 注册表的下游。
- 想了解 C++/pybind 后端如何模板化实现这些布尔度量，预留 [u9-l2 C++/pybind 距离后端](u9-l2-distance-cpp-pybind-backend.md)。
- 若你对「无视双零」背后的生态学动机感兴趣，可先读 `distance.py` 里 `jaccard`/`dice` 的文档注释（[L778](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.py#L778)、[L1343](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/spatial/distance.py#L1343)）给出的参考文献，再回头看本讲的列联表会更有体感。
