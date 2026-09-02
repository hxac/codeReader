# 前置数学：门控 Delta 规则与 KDA 的递推形式

> 本讲是单元一第 2 讲（u1-l2），承接 u1-l1 对 FlashKDA 项目定位的介绍。
> 在打开任何 CUDA 代码之前，我们先把 KDA（Kimi Delta Attention）的**数学骨架**搞清楚：
> 状态是什么、如何逐 token 递推、门控 `g` 和写入强度 `beta` 起什么作用、为什么要做 L2 归一化。
> 本讲的所有公式都能在 `tests/torch_ref.py`（项目的 bit-exact 参考实现）中找到一一对应的代码行。

## 1. 本讲目标

学完本讲，你应该能够：

1. **写出** KDA 的逐 token 状态递推公式与输出公式 \(o_t = q_t S_t^{\top}\)，并能解释「先擦后写」的 delta 修正发生在哪一步。
2. **理解** 门控构造 \(g_t = \text{lower\_bound} \cdot \sigma\big(e^{A\_log} \odot (g^{raw}_t + dt\_bias)\big)\) 中每个成分（`A_log`、`dt_bias`、`sigmoid`、`lower_bound`）的作用，以及为什么激活后的门控恒为负数。
3. **理解** `beta`（写入强度，取值 (0,1)）和 q/k 的 L2 归一化在 delta rule 中分别扮演的角色。
4. 为下一讲（u1-l3 构建运行）和单元二精读 `torch_ref.py` 的 chunk 化算法（u2-l1）打好数学基础。

## 2. 前置知识

### 2.1 从 softmax 注意力到线性注意力

- **softmax 注意力**每个 token 都要回头看序列里所有历史 token（\(O(T^2)\)），注意力权重由 \(\mathrm{softmax}(qk^{\top})\) 给出。
- **线性注意力**把「记忆」压缩成一个固定大小的**状态矩阵** \(S \in \mathbb{R}^{V \times K}\)：每来一个 token，用秩一外积 \(v_t k_t^{\top}\) 把 \((k_t, v_t)\) 这对信息**写进**状态；查询时用 \(q_t\) 从状态里**读出** \(o_t = q_t S^{\top}\)。计算量 \(O(T)\)、状态大小与序列长度无关，这正是 KDA 这类模型能做长上下文推理的原因。
- **Delta 规则（delta rule）**是对「写入」方式的改进：普通线性注意力「只加不擦」，新旧信息会在状态里互相污染；delta 规则在写入前，先把状态里**沿当前 key 方向的旧预测擦掉**，再写入新值与旧预测的**差值**（delta）。这就是上一讲总结里说的「先擦后写」。
- **门控（gating）**进一步给「擦除」加了遥控器：每个 token 先按通道对旧状态做一次衰减（遗忘），衰减系数由网络学出来的门控信号决定。

### 2.2 记号与张量布局

本讲推导全部使用**单 head、单条序列**的记号（多 head 只是按 `H` 维度各自独立地做同样的递推）：

| 符号 | 含义 | 形状 / 取值范围 |
|---|---|---|
| \(q_t, k_t\) | 查询 / 键向量（L2 归一化后） | \(\mathbb{R}^{D}\)，\(D=K=128\) |
| \(v_t\) | 值向量（不归一化） | \(\mathbb{R}^{D}\)，\(D=V=128\) |
| \(g_t\) | 激活后的门控（**逐通道**） | \(\mathbb{R}^{D}\)，每维取值 \((\text{lower\_bound}, 0)\) |
| \(\beta_t\) | 写入强度（sigmoid 激活后） | 标量，\((0, 1)\) |
| \(S_t\) | 递推状态 | \(\mathbb{R}^{V \times K}\)，行是 value 维、列是 key 维 |

> **布局提示**：README 中 `initial_state`/`final_state` 的形状是 `[B, H, V, K]`（见
> [README.md:L102-L104](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/README.md#L102-L104)），
> 即状态矩阵是 \(V \times K\)（value 维在前）。本讲公式与此保持一致。

### 2.3 两个容易踩坑的点

1. **门控是逐通道（per-channel）的，不是每 token 一个标量**。从 API 表就能看出来：`g` 的形状是 `[B, T, H, K]`（每个 key 通道一个门控值），`dt_bias` 是 `[H, K]`（见 [README.md:L95-L101](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/README.md#L95-L101)）。这一点会让「把递推打包成矩阵乘」变得麻烦，是单元二 chunk 化算法里出现 `k_decayed/k_inv/k_restored` 一堆衰减变体的根本原因。
2. **`beta` 传的是激活前的 logits**。kernel 内部才做 sigmoid（见 [README.md:L96](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/README.md#L96)："Beta logits (pre-activation; sigmoid applied internally)"）。`g` 同理，传的是「激活前」的原始信号。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| `tests/torch_ref.py` | 项目的 bit-exact PyTorch 参考实现，逐操作复刻 kernel 数值行为 | `torch_ref` 主循环里的递推语义；门控激活；beta 激活；L2 归一化 |
| `tests/test_fwd.py` | 正确性测试；含 fp64 金标参考 | 输入张量如何构造、`lower_bound`/`scale` 取值；fp64 金标里最「干净」的门控公式 |
| `README.md` | 项目说明与 Kernel API 表 | 各输入张量的形状、dtype 与语义 |
| `docs/20260420-flashkda-v1-deep-dive.md` | 设计博客 | `lower_bound=-5` 与 `CHUNK=16` 的 bf16 表示范围论证 |

## 4. 核心概念与源码讲解

### 4.1 线性注意力状态递推：从「只加不擦」到 KDA 的「先擦后写」

#### 4.1.1 概念说明

线性注意力最朴素的递推是**只加不擦**：

\[
S_t = S_{t-1} + v_t k_t^{\top}, \qquad o_t = q_t S_t^{\top}
\]

状态是所有历史 \((k, v)\) 外积的累加和——一份「联想记忆」。问题在于：如果两个 token 的 key 高度相似（key 碰撞），它们的 value 会在状态里**叠加**而不是**替换**，读出来的是新旧信息的混合，越写越乱。

delta 规则的修复思路：写入 \(v_t\) 之前，先问状态「你沿着 \(k_t\) 这个方向现在预测出什么？」，把预测值 \(\tilde S_t k_t^{\top}\) 从状态里**擦掉**，只写入**残差**。再给整个写入乘一个强度系数 \(\beta_t \in (0,1)\)，并允许写入前先按通道遗忘一次（门控 \(g_t\)），就得到 KDA 的完整递推：

\[
\begin{aligned}
\text{遗忘：}\quad & \tilde S_t = S_{t-1}\, \mathrm{diag}\!\left(e^{g_t}\right) \\
\text{预测：}\quad & p_t = \tilde S_t\, k_t^{\top} \in \mathbb{R}^{V} \\
\text{求差：}\quad & u_t = \beta_t \left( v_t - p_t \right) \\
\text{写入：}\quad & S_t = \tilde S_t + u_t\, k_t^{\top} \\
\text{读出：}\quad & o_t = q_t S_t^{\top} \;=\; q_t \tilde S_t^{\top} + (q_t \cdot k_t)\, u_t
\end{aligned}
\]

几个关键观察：

- \(e^{g_t}\) 是**按通道**的列缩放：\(g_t \in (\text{lower\_bound}, 0)\) 恒为负，所以 \(e^{g_t[j]} \in (e^{\text{lower\_bound}}, 1)\) 恒小于 1——**只会遗忘，不会放大**，状态范数不会指数爆炸。
- 擦除的方向和写入的方向都是 \(k_t\)：擦掉的是「状态对 \(k_t\) 的响应」，写入的是「想要的值与响应之差」。若 \(k_t\) 与某个历史 \(k_c\) 完全相同（且已归一化、\(\beta=1\)、无门控），则 \(S_t k_t^{\top} = v_t\) **精确替换**旧值（4.1.4 实践会验证这一点）。
- 读出发生在写入之后（\(o_t = q_t S_t^{\top}\)，即当前 token 自己的贡献 \((q_t \cdot k_t) u_t\) 也计入输出）。这个约定与 `torch_ref` 中 `Mqk` 取**含对角线**的下三角一致。
- 输出**没有**做归一化分母（不除以 \(\sum \langle q, k\rangle\)），代码里从始至终没有除法——这是 KDA 这个 kernel 的定义，读 `torch_ref` 时不要自己脑补分母。

#### 4.1.2 核心流程

逐 token 串行流程（单 head）：

```text
初始化 S_0 = initial_state（或全零）
for t = 1 .. T:
    q_t, k_t ← L2normalize(q_t), L2normalize(k_t)      # 4.3 讲
    q_t ← scale · q_t                                   # scale 通常 1/√D
    g_t ← lower_bound · sigmoid(exp(A_log)·(g_raw_t + dt_bias))   # 4.2 讲，[D] 逐通道
    β_t ← sigmoid(beta_logit_t)                         # 标量 ∈ (0,1)

    S̃ ← S · diag(exp(g_t))            # 遗忘（按 key 通道缩放列）
    p ← S̃ k_t^T                       # 状态对当前 key 的预测        ∈ R^V
    u ← β_t · (v_t − p)                # delta：要修正的量            ∈ R^V
    S ← S̃ + u k_t^T                   # 写入（秩一更新）
    o_t ← q_t S^T                      # 读出                         ∈ R^V
```

**与 chunk 形式的对应（预告 u2-l1）**：`torch_ref` 实际按 `CHUNK=16` 分块计算，块内 16 个 token 的串行修正被打包成一次 \(16\times16\) 矩阵求逆 \((I+L)^{-1}\)。逐 token 量与块内代码的对照表：

| 逐 token 量 | 数学含义 | `torch_ref` 中的位置 |
|---|---|---|
| \(e^{\text{cum}_r}\)（\(\text{cum}_r=\sum_{i\le r} g_i\)，按通道） | 块首到 token r 的累积遗忘 | `g_cumsum = g_chunk.cumsum(dim=0)`（L208） |
| \(k_r \odot e^{\text{cum}_r}\) | 「从块首看」的读写方向 | `k_decayed`（L210） |
| \(S_{prev} \cdot \mathrm{diag}(e^{\text{cum}_r})\, k_r^{\top}\) | 状态对当前 key 的预测 \(p_r\) | `v_chunk - k_decayed @ state_slice.t()`（L228） |
| \(\beta_r (v_r - p_r)\) | 修正量 \(u_r\) | `v_chunk * beta_val_bf16`（L229，配合 L220 的 sigmoid） |
| \(\sum_r u_r \otimes (k_r \odot e^{g_{total}-\text{cum}_r})\) | 块内全部写入（换算到块尾） | `delta_s = k_restored.t() @ U`（L235） |
| \(o_r = q_r \tilde S_r^{\top} + \sum_{c\le r} M_{rq} u_c\) | 读出 | `_out = q_decayed @ state.t() + Mqk @ U`（L232-L233） |

其中 \(M_{rq}[r,c] = \langle q_r \odot e^{\text{cum}_r - \text{cum}_c},\, k_c\rangle\)（代码 L217 的 `Mqk`，含对角线）：token \(r\) 读出时，token \(c\) 写入的信息已经过 \(c+1..r\) 的按通道衰减。可以手工验证展开求和与逐 token 递推**逐项相等**——这正是 chunk 化算法正确性的来源，细节留给 u2-l1。

#### 4.1.3 源码精读

**① 递推的「心脏」：擦除 + 写入 + 读出**（[tests/torch_ref.py:L227-L241](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L227-L241)）：

```python
state_slice = work_state[seq_idx, h]                      # S_prev, [V, K]
v_chunk = v_chunk - torch.matmul(k_decayed, state_slice.t())  # ① 擦：减去状态预测 p
v_chunk = v_chunk * beta_val_bf16                         #    乘写入强度 β → 得 u

U = torch.matmul(INV, v_chunk)                            # ② 块内串行修正打包成矩阵求逆
_out = torch.matmul(q_decayed, state_slice.t())           # ③ 读：q 查询旧状态
_out = _out + torch.matmul(Mqk, U)                        #    加上块内各 token 的贡献

delta_s = torch.mm(k_restored.t(), U, out_dtype=torch.float32)  # ④ 写：Σ u⊗k_restored

work_state[seq_idx, h] = fp32_fma(delta_s, state_slice.to(torch.float32).t(),
                                  g_total_exp).to(torch.bfloat16).t()   # ⑤ 旧状态按通道衰减 + 写入
```

- 第 ① 步 `k_decayed @ state_slice.t()` 就是逐 token 公式里的 \(p_r\)：注意 `k_decayed` 带着 \(e^{\text{cum}_r}\) 衰减，等价于先衰减状态再读（\(S_{prev}\mathrm{diag}(e^{\text{cum}_r})\,k_r^{\top}\)）。
- 第 ⑤ 步一行同时完成「旧状态衰减」与「累加新写入」：`fp32_fma(c, a, b) = c + a*b`（见 [tests/torch_ref.py:L55-L59](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L55-L59)），`g_total_exp` 是 \(e^{g_{total}}\)（按通道 \([K]\)，广播到列上），最终 `+ delta_s`。数学上即：

\[
S_{chunk末} = S_{prev}\,\mathrm{diag}\!\left(e^{g_{total}}\right) + \sum_{r=1}^{16} u_r \otimes \big(k_r \odot e^{\,g_{total} - \text{cum}_r}\big)
\]

- **状态以 bf16 存储、更新用 fp32 FMA 完成**：`.to(torch.float32)` 提精度做乘加、`.to(torch.bfloat16)` 写回。这是 deep-dive 第 3 节的精度决策，本讲只需知道「存 bf16、算 fp32」。

**② 衰减变体家族**（[tests/torch_ref.py:L208-L215](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L208-L215)）：

```python
g_cumsum = g_chunk.cumsum(dim=0)            # cum_r：块内累积门控（按通道）
g_total = g_cumsum[-1:]                     # 整块的总门控
k_decayed  = k_chunk * exp(g_cumsum).bfloat16()   # k_r ⊙ e^{+cum_r}（从块首看）
q_decayed  = q_chunk * exp(g_cumsum).bfloat16() * scale_bf16
k_inv      = k_chunk * exp(-g_cumsum).bfloat16()  # k_r ⊙ e^{-cum_r}（时间反演）
k_restored = k_inv * exp(g_total).bfloat16()      # k_r ⊙ e^{g_total-cum_r}（从块尾看）
```

四个变体只是同一个 \(k_r\) 乘上不同区间的累积衰减，用途分别是：`k_decayed` 用于块首视角的读/擦（L228、L232），`k_inv` 用于构造块内两两内积（L216-L217：\(\langle k_r \odot e^{\text{cum}_r}, k_c \odot e^{-\text{cum}_c}\rangle = \langle k_r \odot e^{\text{cum}_r-\text{cum}_c}, k_c\rangle\)，把「相对衰减」变成一个内积），`k_restored` 用于块尾视角的写（L235）。**逐通道门控**正是需要这么多种变体的原因：若门控是标量，\(e^{\text{cum}_r - \text{cum}_c}\) 可以直接提出内积，不需要 `-cum_c` 反演。

**③ fp64 金标里最干净的公式**（[tests/test_fwd.py:L78-L83](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L78-L83)）：

```python
g_fp64 = g.clone().to(torch.float64) + dt_bias.to(torch.float64).unsqueeze(0).unsqueeze(0)
g_activated_fp64 = lower_bound * torch.sigmoid(torch.exp(A_log_fp64.view(1, 1, H, 1)) * g_fp64)
beta_activated_fp64 = torch.sigmoid(beta.clone().to(torch.float64))
```

测试代码与 FLA 库的 fp64 逐 token 实现 `fused_recurrent_kda` 对拍时，门控与 beta 就是按本讲公式直接写的——这是「无近似、无位级技巧」的语义版本，可与 `torch_ref` 里为 bit-exact 而扭曲过的写法互相印证。

#### 4.1.4 代码实践

**实践目标**：用一个可以在任何机器（纯 CPU）运行的最小例子，亲眼看到「只加不擦」与「先擦后写」的差别，验证 delta 规则的**精确替换**性质。

**操作步骤**：保存为 `FlashKDA-tutorial/practice/u1l2_delta_demo.py` 并运行（示例代码，仅依赖 PyTorch CPU）：

```python
import torch

D = 4
k1 = torch.tensor([0.8, 0.6, 0., 0.])   # 已是单位向量（0.8²+0.6²=1）
k2 = k1.clone()                          # key 碰撞：第二个 token 的 key 与第一个完全相同
v1 = torch.tensor([1., 0., 0., 0.])
v2 = torch.tensor([0., 2., 0., 0.])

# ---- 线性注意力：只加不擦（β 无关，无门控） ----
S_lin = torch.outer(v1, k1) + torch.outer(v2, k2)
print("linear  S k1^T =", (S_lin @ k1).tolist())

# ---- delta 规则：先擦后写（β=1，无门控） ----
S = torch.outer(v1, k1)                          # 第一个 token 写入
S = S + torch.outer(v2 - S @ k2, k2)             # 先减预测，再写残差
print("delta   S k1^T =", (S @ k1).tolist())
print("v2       =", v2.tolist())
```

**需要观察的现象**：线性注意力的读出是新旧 value 的**叠加**；delta 规则的读出**恰好等于新 value**。

**预期结果**（可手算验证）：

```text
linear  S k1^T = [1.0, 2.0, 0.0, 0.0]   # v1 + v2，新旧混叠
delta   S k1^T = [0.0, 2.0, 0.0, 0.0]   # == v2，精确替换
v2       = [0.0, 2.0, 0.0, 0.0]
```

推导：delta 路径 \(S k_2^{\top} = v_1 + (v_2 - v_1)(k_2\cdot k_1) = v_1 + (v_2 - v_1) = v_2\)，因为 \(k_2 \cdot k_1 = 1\)。

#### 4.1.5 小练习与答案

**练习 1**：把上面例子里的 \(\beta\) 从 1 改成 0.25（写入强度打两五折之后的四分之一），\(S k_2^{\top}\) 等于什么？

**答案**：\(u = 0.25\,(v_2 - v_1)\)，\(S k_2^{\top} = v_1 + 0.25(v_2 - v_1) = 0.75 v_1 + 0.25 v_2 = [0.75, 0.5, 0, 0]\)。即 \(\beta\) 控制「向新值收敛的比例」，\(\beta \to 0\) 时 token 几乎不写状态。

**练习 2**：写出无门控、无 \(\beta\)（\(\beta \equiv 1\)）时，两个不同 token（key 分别为 \(k_1, k_2\)，均单位向量）之后的 \(S k_2^{\top}\)。

**答案**：\(S = v_1 k_1^{\top} + (v_2 - (v_1 k_1^{\top}) k_2^{\top}) k_2^{\top}\)，故 \(S k_2^{\top} = v_1 (k_1 \cdot k_2) + v_2 - v_1(k_1 \cdot k_2) = v_2\)。只要 \(k_2\) 是单位向量，**当前 key 方向的预测总是精确等于刚写入的 value**，与历史无关——「擦除」恰好抵消了历史在该方向上的残留（\(\langle k_1,k_2\rangle\) 那一项）。若 \(k_2\) 模长不是 1，该结论不成立，这正是 4.3 要做 L2 归一化的原因之一。

**练习 3**：为什么说 KDA 的递推是 \(O(T)\) 而不是 \(O(T^2)\)？

**答案**：每个 token 只做常数次 \(D \times D\) 级别的向量-矩阵运算（一次列缩放、一次矩阵-向量乘、一次外积），状态大小 \(D^2\) 与 \(T\) 无关；softmax 注意力每个 token 要与全部历史 token 两两交互，故为 \(O(T^2)\)。

### 4.2 KDA 门控与 beta：可控遗忘与写入强度

#### 4.2.1 概念说明

门控决定「旧记忆按什么比例保留」，是逐通道向量：

\[
g_t \;=\; \ell \cdot \sigma\!\left(e^{a} \odot \big(g^{raw}_t + b\big)\right) \;\in\; (\ell,\, 0)^{D},
\qquad \ell = \text{lower\_bound} \le 0,\;\; a = A\_log,\;\; b = dt\_bias
\]

每个成分的直觉：

| 成分 | 形状 | 直觉 |
|---|---|---|
| \(g^{raw}_t\)（输入 `g`） | `[B,T,H,K]` bf16 | 网络为每个 token、每个 key 通道输出的**原始门控信号**（激活前） |
| \(b\)（`dt_bias`） | `[H,K]` fp32 | 每通道的**偏置**：把 sigmoid 的工作点挪到合理区间（类似 Mamba 的 \(\Delta t\) 偏置；测试里取值 ±4/±8，见 [tests/test_fwd.py:L55-L60](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L55-L60)） |
| \(a\)（`A_log`） | `[H]` fp32 | 每 head 的**斜率**：\(e^{a}\) 控制 sigmoid 过渡的陡峭程度——\(a\) 大则门控在「几乎不遗忘」与「接近下界遗忘」之间快速切换 |
| \(\sigma\)（sigmoid） | — | 把任意实数压到 (0,1)，保证门控是「比例」 |
| \(\ell\)（`lower_bound`） | 标量，\([-5, 0]\) | **遗忘下界**：\(g_t > \ell \Rightarrow e^{g_t} > e^{\ell}\)。单个 token 最多遗忘到 \(e^{-5} \approx 0.0067\)，防止一步清零造成的信息断崖与数值问题 |

由 \(\sigma(\cdot) \in (0,1)\)、\(\ell \le 0\)，立刻得到两条硬性质：

\[
\ell < g_t[j] < 0 \quad\Longrightarrow\quad e^{\ell} < e^{g_t[j]} < 1
\]

即**遗忘有界、永不放大**。这条性质还有一个下游红利：块内 16 个 token 的累积门控 \(\text{cum} \in (16\ell, 0) = (-80, 0)\)，于是 \(e^{\text{cum}} > e^{-80} \approx 1.8\times10^{-35}\)，仍在 bf16 的正常表示范围内（bf16 最小正常数约 \(1.18\times10^{-38}\)）；若用 CHUNK=64，最坏 \(\text{cum} \approx -320\)，\(e^{-320}\) 直接下溢为 0。这正是 deep-dive 选 `CHUNK=16` 的第一条理由（[docs/20260420-flashkda-v1-deep-dive.md:L16-L19](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/docs/20260420-flashkda-v1-deep-dive.md#L16-L19)）。\(e^{-80}\) 与 \(e^{-320}\) 的具体数值为本讲粗略验算，精确边界待本地验证。

`beta` 则是**写入强度**：\(\beta_t = \sigma(\beta^{logit}_t) \in (0,1)\)，缩放修正量 \(u_t\)。它同时在两个地方出现：

- \(u_t = \beta_t (v_t - p_t)\)：当前 token 写入多少残差；
- 块内修正矩阵 \(L[r,c] = \beta_r \langle k_r \odot e^{\text{cum}_r - \text{cum}_c}, k_c\rangle\)（代码 L222）：因为 \(u_r\) 被 \(\beta_r\) 缩放，token \(r\) 对更早 token 的「再修正」也随 \(\beta_r\) 缩放。

直觉总结：**`g` 管「忘多少」，`beta` 管「写多少」**，两者都是网络可以逐 token、（对 `g` 而言）逐通道学习的旋钮。

#### 4.2.2 核心流程

```text
门控激活（每个 token t，按通道 j）：
  x_j   = g_raw[t, j] + dt_bias[j]          # 平移工作点
  s_j   = sigmoid( exp(A_log) · x_j )       # 斜率放大 + 压到 (0,1)
  g_j   = lower_bound · s_j                 # 拉到 (lower_bound, 0)

beta 激活（每 token 一个标量）：
  β_t   = sigmoid( beta_logit_t )           # 压到 (0,1)

使用位置：
  遗忘：S̃ = S · diag(exp(g))                # g 参与指数
  写入：u  = β · (v − S̃ k^T)                # β 参与线性缩放
```

数值实现上的两个「硬件友好」细节（本讲了解即可，细节在 u3-l8）：

1. **指数用 exp2 实现**：\(e^{x} = 2^{x \log_2 e}\)。GPU 的 `ex2.approx.ftz` 指令远快于通用 `exp`，所以参考实现把 \(\log_2 e\) 预乘进常数（`LOG2E`，[tests/torch_ref.py:L44](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L44)；`fp32_ex2_ftz` 见 [L47-L52](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L47-L52)）。
2. **sigmoid 用 tanh 实现**：\(\sigma(x) = \tanh(x/2)/2 + 1/2\)，用 PTX 的 `tanh.approx.f32` 指令（[tests/torch_ref.py:L11-L19](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L11-L19)）。

#### 4.2.3 源码精读

**① `torch_ref` 中的门控激活**（[tests/torch_ref.py:L161-L169](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L161-L169)）：

```python
g = g.to(torch.float32) + dt_bias.unsqueeze(0)        # [BT, H, K]：加偏置
a_log_exp = fp32_ex2_ftz(A_log * LOG2E).unsqueeze(0).unsqueeze(-1)  # e^{A_log}，用 exp2 算
scale = lower_bound * LOG2E                            # ⚠ 把 lower_bound 与 log2(e) 一起折进系数
g = scale * sigmoid_ext.sigmoid_tanh_fp32(a_log_exp * g)
```

⚠ **阅读陷阱**：激活后这个 `g` 是「以 2 为底、且乘了 `lower_bound`」的量，即 \(g_{code} = (\ell \log_2 e)\,\sigma(\cdot)\)。此后所有指数都走 `fp32_ex2_ftz`（\(2^{x}\)），于是 \(2^{g_{code}} = e^{\ell\,\sigma(\cdot)}\)，与本讲的语义公式严格等价。`torch_ref` 把 \(\log_2 e\) 从「每个指数里」挪到「激活系数里」，纯是为了贴合 kernel 的指令选择——数学上没变。

**② `beta` 的激活与使用**（[tests/torch_ref.py:L219-L223](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L219-L223)）：

```python
beta_activated = sigmoid_ext.sigmoid_tanh_fp32(beta_chunk.to(torch.float32))  # σ(beta logits)
beta_val_bf16 = beta_activated.to(torch.bfloat16).unsqueeze(-1)
L = torch.tril(L, diagonal=-1) * beta_activated.unsqueeze(-1)   # β_r 乘进行内修正矩阵 L
...
v_chunk = v_chunk * beta_val_bf16                                # (L229) u = β·(v − p)
```

输入 `beta` 是 bf16 logits，先升 fp32 做 sigmoid，量化回 bf16 后作为逐行系数使用。注意 sigmoid 输出被量化成 bf16——这是 kernel 的真实行为，参考实现必须原样复刻。

**③ 测试里参数怎么取**（[tests/test_fwd.py:L222-L240](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L222-L240)）：

```python
B, T, H, D = 1, 8192, 96, 128
LOWER_BOUND = -5.0
g     = torch.randn((B, T, H, D), dtype=torch.bfloat16, device='cuda')   # 原始门控 ~ N(0,1)
beta  = torch.randn((B, T, H), dtype=torch.bfloat16, device='cuda')      # beta logits ~ N(0,1)
A_log = torch.rand(H, dtype=torch.float32, device='cuda')                # ∈ [0,1)
dt_bias = torch.rand(H, D, dtype=torch.float32, device='cuda')           # ∈ [0,1)
scale = 1.0 / math.sqrt(D)
```

#### 4.2.4 代码实践

**实践目标**：亲手验证门控值域 \((\text{lower\_bound}, 0)\) 与 `A_log` 斜率、`lower_bound` 下界的定量关系（纯 CPU，几秒完成）。

**操作步骤**：保存为 `FlashKDA-tutorial/practice/u1l2_gate_sweep.py` 并运行（示例代码）：

```python
import torch

def activate_gate(g_raw, A_log, dt_bias, lower_bound):
    """本讲公式（自然指数语义版），g_raw: [..., D]"""
    return lower_bound * torch.sigmoid(
        torch.exp(A_log) * (g_raw.float() + dt_bias.float()))

D, lb = 8, -5.0
dt_bias = torch.zeros(D)
g_raw = torch.linspace(-20, 20, 9)                      # 扫原始门控信号

for a_log in (-2.0, 0.0, 2.0):
    A_log = torch.tensor(a_log)
    g = activate_gate(g_raw, A_log, dt_bias, lb)
    print(f"A_log={a_log:+.1f}  gate range = ({g.min():+.4f}, {g.max():+.4f})")

# 换下界：观察值域随 lower_bound 等比缩放
for lb_i in (0.0, -1.0, -5.0):
    g = activate_gate(g_raw, torch.tensor(0.0), dt_bias, lb_i)
    print(f"lower_bound={lb_i:+.1f}  gate range = ({g.min():+.4f}, {g.max():+.4f})")
```

**需要观察的现象**：

1. 无论 `g_raw` 扫到多极端（±20），激活后的门控**始终严格落在** \((\text{lower\_bound}, 0)\) 内；
2. `A_log` 越大，门控从「接近 0」翻到「接近下界」的过渡越陡（过渡宽度量级 \(\sim 4/e^{a}\)）；
3. `lower_bound` 等比压缩整个值域；`lower_bound=0` 时门控恒为 0（完全不忘）。

**预期结果**：所有输出行满足 `lower_bound < gate < 0`；`A_log=2` 时大多数采样点已贴近两端的平带。具体打印数值待本地验证。

**延伸（可选，需 GPU）**：把上面公式与 `torch_ref` 的 exp2/tanh 路径对比——`from torch_ref import sigmoid_ext` 后逐点比较 `activate_gate(...)` 与 `scale * sigmoid_ext.sigmoid_tanh_fp32(a_log_exp * (g_raw + dt_bias))`（`scale=lower_bound*LOG2E`，再除回 `LOG2E` 换语义），两者差应只在 `tanh.approx` 与量化的 ulp 量级。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`lower_bound = 0` 时递推退化成什么？`lower_bound = -5` 呢？

**答案**：`lower_bound=0` ⟹ \(g_t \equiv 0\) ⟹ \(e^{g_t} = 1\)，遗忘步消失，退化为「无门控 delta 规则」\(S_t = S_{t-1} + u_t k_t^{\top}\)。`lower_bound=-5` 时每个通道每步最多衰减到 \(e^{-5} \approx 0.0067\)（约 0.7% 保留），是项目默认的最强遗忘（测试 `LOWER_BOUND = -5.0`，[tests/test_fwd.py:L225](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L225)）。

**练习 2**：为什么 `A_log` 要先过 `exp` 再乘，而不是直接用 `A_log` 当斜率？

**答案**：`A_log` 是**对数域参数化**。要求斜率 \(> 0\)（sigmoid 的自变量系数必须为正才保持单调、且保证 \(\sigma(0)=1/2\) 的工作点语义），用 \(e^{a}\) 参数化可让网络在优化中取任意实数值而斜率自动为正；同时对数域学习大/小斜率更均衡（斜率从 1 变 2 只需 \(a\) 从 0 到 \(\ln 2\)）。这与门控、beta 都传「激活前」信号的设计一致：kernel 负责最后的非线性。

**练习 3**：门控激活发生在 `torch_ref` 的哪个位置、在块循环的里面还是外面？为什么？

**答案**：在块循环**之前**、L2 归一化之后（[tests/torch_ref.py:L161-L169](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L161-L169)）。因为激活是逐 token、逐通道的逐元素运算，与分块无关，可以一次性对整个 `[BT, H, K]` 完成；块循环内只消费激活后的 `g`（做 `cumsum` 等块内聚合）。

### 4.3 L2 归一化：把读写方向放到单位球上

#### 4.3.1 概念说明

`torch_ref` 在一切计算之前，先把 `q` 和 `k` 沿最后一维做 L2 归一化（`v`、`g` 不做）：

\[
\hat k_t = k_t \cdot \mathrm{rsqrt}\!\left(\lVert k_t\rVert_2^2 + 10^{-6}\right), \qquad \hat q_t = \text{scale} \cdot q_t \cdot \mathrm{rsqrt}\!\left(\lVert q_t\rVert_2^2 + 10^{-6}\right)
\]

为什么必须归一化（尤其对 delta 规则）？

1. **替换性质依赖单位长度**。4.1.5 练习 2 已证明：\(S k_t^{\top}\) 精确等于刚写入的 \(v_t\) 的前提是 \(k_t \cdot k_t = 1\)。若 \(\lVert k_t\rVert \ne 1\)，擦除 \(S k_t^{\top} k_t^{\top}\) 会多擦或少擦，写入的残差错配，delta 规则的「可替换联想记忆」语义被破坏。
2. **内积变成余弦相似度**。归一化后 \(\langle \hat q, \hat k\rangle \in [-1, 1]\)，`Mqk`、`L` 的元素是有界量（再乘上 \(\beta \le 1\) 与衰减 \(\le 1\)）。这直接保证 \((I+L)\) 良态、求逆稳定（u3-l1 的前代换求逆正确性依赖 \(L\) 的元素不太大）。
3. **状态范数有界**。每步写入 \(u k^{\top}\) 的能量受 \(\lVert k\rVert = 1\) 控制，长序列下状态不会因个别大模长 key 而被单点主导。
4. **`q` 侧等价于 softmax 注意力的 \(1/\sqrt{d}\) 缩放**。测试默认 `scale = 1/√D`（[tests/test_fwd.py:L240](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L240)），与归一化一起把读出 \(o\) 的幅度稳定住。
5. **为什么 `v` 不归一化**：`v` 是被存储的「内容」，其幅度本身携带信息；`g` 不归一化：它是门控的原始信号，有自己的激活路径。

#### 4.3.2 核心流程

```text
L2 归一化（逐 token，fp32 计算）：
  1. 把 bf16 的 x 升到 fp32
  2. 求平方和 Σ x_j²（参考实现按特定顺序：先每 8 个一组 FMA，再 XOR 树归约 —— 为了与 kernel 的
     warp shuffle 归约 bit-exact 对齐）
  3. inv = rsqrt(Σ x_j² + 1e-6)      # 1e-6 防 0 向量除零
  4. x̂ = (x · inv) 量化回 bf16
```

「按特定顺序归约」是本讲的次要细节、u2-l7 的主线：GPU 上求和是分块的（每线程先算一段，再跨线程树归约），浮点加法又不满足结合律，所以参考实现必须**复制 kernel 的归约顺序**才能做到逐位相等（这是 `l2_normalize_kernel_match` 名字里 `match` 的含义）。

#### 4.3.3 源码精读

**① 归一化的调用点**（[tests/torch_ref.py:L158-L159](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L158-L159)）：

```python
q = l2_normalize_kernel_match(q)
k = l2_normalize_kernel_match(k)
```

只有 `q`、`k`；`v`、`g`、`beta` 原样通过。

**② 归一化的实现**（[tests/torch_ref.py:L62-L78](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L62-L78)）：

```python
def l2_normalize_kernel_match(x):
    """L2 normalize matching kernel's warp-shuffle tree reduction with FMA.
    x: [..., D] bf16, D must be 128."""
    x_f32 = x.float()
    groups = x_f32.reshape(*x_f32.shape[:-1], 16, 8)   # D=128 → 16 组 × 每组 8 元素
    partials = torch.zeros(*x_f32.shape[:-1], 16, dtype=torch.float32, device=x.device)
    for i in range(8):
        partials = fp32_fma(partials, groups[..., i], groups[..., i])   # 每组 FMA 累加
    for offset in [8, 4, 2, 1]:                        # XOR 树归约（对应 warp shuffle）
        indices = torch.arange(16, device=x.device) ^ offset
        partials = partials + partials[..., indices]
    inv_norm = torch.rsqrt(partials[..., 0:1] + 1e-6)   # ε=1e-6
    return (x_f32 * inv_norm).to(x.dtype)              # 量化回 bf16
```

- `16 组 × 8 元素` 正对应 D=128 与 kernel 里「每线程持 8 个元素、每 warp 32 线程（此处映射为 16 个 partial）」的划分；
- `fp32_fma`（[L55-L59](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L55-L59)）模拟单条融合乘加指令的舍入：先在 fp64 里算 \(c + a\cdot b\) 再一次性舍回 fp32，等价于硬件 FMA 的「中间积不舍入」；
- 最终 `(x_f32 * inv_norm).to(x.dtype)`：**归一化结果量化回 bf16**——kernel 后续所有 GEMM 都吃 bf16，这是必经的量化点。

**③ 测试输入的预处理**（[tests/test_fwd.py:L230-L231](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L230-L231)）：

```python
q = F.normalize(torch.randn(...), p=2, dim=-1).to(torch.bfloat16)
k = F.normalize(torch.randn(...), p=2, dim=-1).to(torch.bfloat16)
```

注意测试在**喂入之前**就用 `F.normalize` 预归一化了 `q/k`，而 `torch_ref`/kernel 内部还会再做一次（幂等：对单位向量再归一化近似不变，差异在 ε 与舍入量级）。这说明 kernel 把 L2 归一化视为**自身契约的一部分**（对应 FLA 的 `use_qk_l2norm_in_kernel=True`），无论输入是否已归一化，行为都确定。

#### 4.3.4 代码实践

**实践目标**：直观感受「归一化前后内积的值域差异」，理解为什么 \(\langle q, k\rangle\) 必须有界（纯 CPU）。

**操作步骤**：保存为 `FlashKDA-tutorial/practice/u1l2_norm_effect.py` 并运行（示例代码）：

```python
import torch
torch.manual_seed(0)

D = 128
q  = torch.randn(D)
ks = torch.randn(2000, D)
ks *= torch.exp(torch.randn(2000) * 1.5)          # 故意让 key 模长差异巨大（log-normal）

raw = q @ ks.T                                     # 未归一化的内积
qn, kn = q / q.norm(), ks / ks.norm(dim=-1, keepdim=True)
normed = qn @ kn.T                                 # 归一化后 = 余弦相似度

print(f"raw    |<q,k>|  max = {raw.abs().max():.2f}   （理论上无界，随模长增长）")
print(f"normed |<q,k>|  max = {normed.abs().max():.4f} （必 ≤ 1）")
print(f"normed keys 的模长全部 = {kn.norm(dim=-1).min():.6f} ~ {kn.norm(dim=-1).max():.6f}")
```

**需要观察的现象**：未归一化时最大内积可达几十甚至上百（取决于随机模长），归一化后严格不超过 1。

**预期结果**：`normed` 的最大绝对值 \(\le 1\)（2000 个随机方向里通常在 0.2~0.35 附近，接近 \(\sqrt{\ln n / D}\hbox{量级}\)）；`raw` 的最大值远大于 1。具体数值随 seed 变化，待本地验证。

**延伸思考**：把本实践与 4.1.5 练习 2 联系起来——若 `ks` 中某个向量模长为 5，用未归一化的它做 delta 写入，擦除步会多擦 5 倍，状态立即被该 token 主导。

#### 4.3.5 小练习与答案

**练习 1**：归一化里的 `+1e-6` 去掉会出什么问题？

**答案**：若某个 token 的 \(k\) 全零（或下溢为 0），\(\lVert k\rVert^2 = 0\)，`rsqrt(0) = +inf`，输出 NaN 并污染整个后续递推。`1e-6` 把零向量映射为全零输出而不是 NaN。对非零向量，`1e-6` 相对 \(\lVert k\rVert^2\)（随机向量约 128 量级）影响可忽略。

**练习 2**：为什么参考实现要费力气复刻「分组 FMA + 树归约」的求和顺序，而测试的金标参考（[tests/test_fwd.py:L78-L80](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L78-L80)）不用？

**答案**：两份代码的用途不同。`torch_ref` 用于 **exact match**（`torch.equal` 逐位比较，[tests/test_fwd.py:L260](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/test_fwd.py#L260)），浮点加法不满足结合律，归约顺序不同结果就可能差最后几个 ulp，所以必须逐位复刻 kernel 的顺序。金标参考用 fp64 逐 token 递推，只做**数值精度对照**（误差统计/画图），允许 ulp 差异，自然用最朴素的写法。

**练习 3**：如果只归一化 `k`、不归一化 `q`（也不乘 `scale`），递推的哪些性质保持、哪些破坏？

**答案**：保持：替换性质（只依赖 \(\lVert k\rVert=1\)）、状态范数有界、\((I+L)\) 良定（`L` 元素仍有界）。破坏：输出 \(o_t = q_t S_t^{\top}\) 的幅度随 \(\lVert q_t\rVert\) 无界波动，训练不稳定；`Mqk` 的元素不再有界（`L` 有界而 `Mqk` 无界），后续若复用同一套求逆/消元流程则数值风险不同。所以 q、k 都要归一化，q 再配 `scale`。

## 5. 综合实践

**任务**：不看 `torch_ref.py` 的向量化写法，用纯 PyTorch **双层 for 循环**（外层时间、内层 head）实现逐 token 的 KDA 递推，再与 `torch_ref`（项目的 chunk 化参考实现）对拍，验证两者在「允许数值误差」的意义下一致。

**为什么值得做**：你的循环版本就是本讲公式的直接翻译（\(O(T)\) 串行）；`torch_ref` 是同一数学在 CHUNK=16 上的矩阵化（u2-l1 的主角）。两者对得上，说明你真的理解了递推语义，也为读 chunk 算法建立了「标准答案」。

**操作步骤**：

1. 在装好 FlashKDA 环境（GPU + bf16 支持）的机器上，从仓库根目录运行下面的脚本（建议存为 `FlashKDA-tutorial/practice/u1l2_recurrent_loop.py`）：

```python
import sys, math, torch
sys.path.insert(0, "tests")                  # 让 torch_ref 可导入（会即时编译一个 CUDA 扩展）
from torch_ref import torch_ref

torch.manual_seed(0)
B, T, H, D = 1, 64, 2, 128                   # 题目规格
LOWER_BOUND, scale = -5.0, 1.0 / math.sqrt(D)
dev = "cuda"

# ---- 输入：与 tests/test_fwd.py 的构造方式一致 ----
q  = torch.randn(B, T, H, D, dtype=torch.bfloat16, device=dev)
k  = torch.randn(B, T, H, D, dtype=torch.bfloat16, device=dev)
v  = torch.randn(B, T, H, D, dtype=torch.bfloat16, device=dev)
g_raw     = torch.randn(B, T, H, D, dtype=torch.bfloat16, device=dev)  # 激活前门控
beta_log  = torch.randn(B, T, H, dtype=torch.bfloat16, device=dev)     # beta logits
A_log     = torch.rand(H, dtype=torch.float32, device=dev)
dt_bias   = torch.rand(H, D, dtype=torch.float32, device=dev)

# ---- 逐 token 双层循环实现（本讲公式的直译） ----
def kda_recurrent_loop(q, k, v, g_raw, beta_log, A_log, dt_bias, scale, lower_bound):
    B, T, H, D = q.shape
    qn = torch.nn.functional.normalize(q.float(), dim=-1)               # L2 归一化（fp32 近似）
    kn = torch.nn.functional.normalize(k.float(), dim=-1)
    gate = lower_bound * torch.sigmoid(                                 # 逐通道门控激活
        torch.exp(A_log).view(1, 1, H, 1) * (g_raw.float() + dt_bias.view(1, 1, H, D)))
    beta_act = torch.sigmoid(beta_log.float())                          # beta 激活

    S   = torch.zeros(B, H, D, D, dtype=torch.float32, device=q.device)  # 状态 [B,H,V,K]，fp32 累加
    out = torch.empty(B, T, H, D, dtype=torch.float32, device=q.device)
    for t in range(T):                          # 外层：时间步
        S = S * torch.exp(gate[:, t]).unsqueeze(2)      # S̃ = S · diag(e^{g_t})（按 key 通道缩列）
        for h in range(H):                      # 内层：head
            qh, kh, vh = qn[0, t, h] * scale, kn[0, t, h], v[0, t, h].float()
            p   = S[0, h] @ kh                  # 状态对当前 key 的预测      ∈ R^V
            u   = beta_act[0, t, h] * (vh - p)  # delta 修正量
            S[0, h] = S[0, h] + torch.outer(u, kh)       # 写入 u k^T
            out[0, t, h] = qh @ S[0, h].T               # 读出 o = q S^T
    return out, S

out_loop, S_loop = kda_recurrent_loop(q, k, v, g_raw, beta_log, A_log, dt_bias,
                                      scale, LOWER_BOUND)

# ---- torch_ref 对拍（chunk 化、bit-exact 风格的参考实现） ----
out_ref = torch.zeros_like(v)
final_ref = torch.zeros(B, H, D, D, dtype=torch.bfloat16, device=dev)
torch_ref(q, k, v, g_raw, beta_log, scale, out_ref,
          A_log=A_log, dt_bias=dt_bias, lower_bound=LOWER_BOUND,
          final_state=final_ref)
torch.cuda.synchronize()

def rel_err(a, b):
    return ((a.float() - b.float()).norm() / b.float().norm()).item()

print(f"out 形状: loop={tuple(out_loop.shape)}  ref={tuple(out_ref.shape)}")
print(f"out 均值/最大值: loop=({out_loop.mean():+.4f}, {out_loop.abs().max():.3f})  "
      f"ref=({out_ref.float().mean():+.4f}, {out_ref.float().abs().max():.3f})")
print(f"out 相对误差:      {rel_err(out_loop, out_ref):.4e}")
print(f"state 形状: loop={tuple(S_loop.shape)}  ref={tuple(final_ref.shape)}")
print(f"state 均值/最大值: loop=({S_loop.mean():+.4f}, {S_loop.abs().max():.3f})  "
      f"ref=({final_ref.float().mean():+.4f}, {final_ref.float().abs().max():.3f})")
print(f"state 相对误差:    {rel_err(S_loop, final_ref):.4e}")
```

2. 先跑通，再记录输出。

**需要观察的现象**：

- 两边的 `out` 形状同为 `[1, 64, 2, 128]`、状态形状同为 `[1, 2, 128, 128]`；
- 均值 / 绝对值最大值在同一数量级（随机输入下 `out` 的量级通常在 1 以内、状态在几十~几百量级，因为状态累加了 64 个外积并带 `scale=1/√128` 的读出缩放）；
- 相对误差在 bf16 精度水平（量级 \(10^{-2}\)），**不会**精确到 0——`torch_ref` 存 bf16 状态、用 exp2/tanh 近似与特定 FMA 顺序，而你的循环全程 fp32、用 `torch.exp`/`torch.sigmoid`。

**预期结果**：形状一致、统计量同量级、相对误差约 \(10^{-3}\sim10^{-2}\)（具体数值待本地验证；若出现数量级偏离或 NaN，优先检查：门控公式是否漏了 `lower_bound`、状态衰减是否乘在 key 通道维、`beta` 是否忘了 sigmoid）。

**扩展实验**（可选）：

1. 设 `LOWER_BOUND = 0.0` 并把 `T` 个 key 手工改成少量重复方向（如把 `k` 限制在前 8 个固定单位向量里）、`beta_log` 全设 +10（\(\beta\approx1\)），观察同方向 token 的输出是否呈现「后写覆盖先写」；
2. 给 `kda_recurrent_loop` 加 `initial_state` 参数（替换全零 `S` 的初始化），验证 `torch_ref` 传同样的 `initial_state` 时两边 `final_state` 仍然对得上。

## 6. 本讲小结

- KDA 的逐 token 递推：**遗忘** \(\tilde S_t = S_{t-1}\mathrm{diag}(e^{g_t})\) → **求差** \(u_t = \beta_t(v_t - \tilde S_t k_t^{\top})\) → **写入** \(S_t = \tilde S_t + u_t k_t^{\top}\) → **读出** \(o_t = q_t S_t^{\top}\)；「先擦后写」的 delta 修正使重复 key 的新值能精确替换旧值。
- 门控 \(g_t = \text{lower\_bound}\cdot\sigma(e^{A\_log}(g^{raw}_t + dt\_bias))\) **逐通道**取值于 \((\text{lower\_bound}, 0)\)：只会遗忘不会放大；`lower_bound=-5` 与 `CHUNK=16` 共同保证 \(e^{\mathrm{cumsum}}\) 不越出 bf16 范围。`torch_ref` 里的激活写法把 \(\log_2 e\) 折进系数以贴合 exp2 硬件指令，语义不变。
- `beta` 传激活前 logits，kernel 内做 sigmoid，作为**写入强度**缩放修正量 \(u_t\)（并通过 \(u_r\) 缩放块内修正矩阵 \(L\) 的行）。
- q/k 做 L2 归一化（+1e-6，fp32 计算、量化回 bf16）是 delta 规则替换性质与 \((I+L)\) 良态的前提；`v`、`g` 不归一化；`q` 另乘 `scale`（默认 \(1/\sqrt{D}\)）。
- `torch_ref` 的块内代码（`k_decayed/k_inv/k_restored`、`L/Mqk/INV`）是同一递推的 CHUNK=16 矩阵化打包，逐 token 公式可与其中每一步一一对应。

## 7. 下一步学习建议

- **下一讲（u1-l3）**：把环境搭起来——submodule、`FLASH_KDA_CUDA_ARCHS` 与 `tests/test.sh`，为后面所有需要 GPU 的实践做准备。
- **之后再读（u2-l1）**：带着本讲的对照表精读 `tests/torch_ref.py` 的完整 chunk 循环，重点搞清「块内 16 个 token 的串行修正如何被打包成 \((I+L)^{-1}\)」以及四个衰减变体的推导。
- **回看源码**：现在重读 [tests/torch_ref.py:L208-L241](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/tests/torch_ref.py#L208-L241)，确认每一行都能对应到本讲的一个公式；读不顺的地方就是 u2-l1 要解决的问题。
- **延伸阅读**：deep-dive 第 1 节（CHUNK=16 的选择）与本讲 4.2 的范围论证互为印证；第 3 节（bf16 状态 + fp32 更新）对应本讲看到的 `.to(torch.float32)` / `.to(torch.bfloat16)` 交替。
