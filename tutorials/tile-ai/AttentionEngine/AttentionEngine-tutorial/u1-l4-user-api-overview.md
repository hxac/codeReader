# 用户 API 全景：score_mod / mask_mod / online_func / CustomIO

## 1. 本讲目标

通过本讲，你将：

- 掌握 AttentionEngine 前端要求用户定义的四个组件——`score_mod`、`mask_mod`、`online_func`、`custom_fwd_inputs`（`CustomIO`）——各自的**函数签名**与**职责**。
- 理解这四个 Python 函数是如何「拼」成一次完整注意力计算的，即：

  \[
  \text{scores} = q @ k \;\rightarrow\; \text{mask\_mod} \;\rightarrow\; \text{score\_mod} \;\rightarrow\; \text{online\_func} \;\rightarrow\; o = p @ v
  \]

- 能够读懂项目自带的两个标准样例 `attn_script/mha.py`（因果 softmax 注意力）与 `attn_script/sigmoidattn.py`（sigmoid 注意力）。

本讲只讲**「用户要写什么」**，不深入「框架怎么把这些函数编译成 kernel」——后者属于第二、三单元的符号 IR 与降级机制。

## 2. 前置知识

在学习本讲前，请确保你已经理解（来自 u1-l1、u1-l2、u1-l3）：

- **AttentionEngine 是编译器**：用户用 Python 函数描述注意力逻辑，框架把它翻译成 GPU fused kernel。
- **`qkv_meta` 与 `meta_tensor`**：三个只含形状信息的占位张量，是编译阶段的唯一形状来源。
- **core 四层架构**：transform（符号 IR）/ codegen（发射）/ lower（降级）/ template（模板）。本讲的四个组件属于「用户输入」，它们会被 transform 层记录成符号 DAG。

本讲会用到几个直观概念：

- **逐元素（elementwise）变换**：对 scores 矩阵的**每一个元素**独立做同一件事，例如「乘以缩放系数」「过一个激活函数」。
- **行规约（row reduce）**：对 scores 某一**行**的所有元素做汇总（求最大、求和），得到一个标量，例如 softmax 分母。
- **在线算法（online algorithm）**：数据一块一块地到来，每来一块就用一个**递推公式**更新中间结果，不需要一次性看到全部数据。FlashAttention 的 online softmax 就是典型例子。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `docs/API.md` | 官方 API 说明，给出四个组件的签名与 `AttentionEngine` 构造函数原型。 |
| `attn_script/Readme.md` | 前端总览，用伪代码讲清四件套如何拼成注意力，并给出 online 算法模板。 |
| `attn_script/mha.py` | 标准因果 softmax 注意力样例，定义了 `score_mod`、`causal_mask`、`OnlineSoftmax`。 |
| `attn_script/sigmoidattn.py` | sigmoid 注意力样例，演示带 `CustomIO` 偏置输入与 `OnlineIdentity` 的组合。 |
| `attn_script/reluattn.py` | relu 注意力样例，用 `score.max(0)` 实现 relu，是本讲实践的参照实现。 |
| `attention_engine/attn_engine/attn_engine.py` | `OnlineFunc` 基类与 `AttentionEngine` 引擎入口所在。 |
| `attention_engine/core/transform/core.py` | `CustomIO`、`SymbolScalar`、`SymbolicArray` 的定义，是组件背后的符号类型。 |

## 4. 核心概念与源码讲解

按照最小模块，本讲分为四个部分：`score_mod` 与 `mask_mod`、`OnlineFunc` 抽象、`CustomIO` 自定义输入，以及四者如何拼成一次注意力。

### 4.1 score_mod 与 mask_mod：逐元素变换与布尔遮蔽

#### 4.1.1 概念说明

`score_mod` 和 `mask_mod` 都作用在「原始注意力分数」\(\text{scores} = q @ k\) 上，但分工不同：

- **`score_mod`**：对 scores 做**数值上的逐元素变换**。最常见的例子是**缩放**（乘以 \(1/\sqrt{D}\)），也可以加偏置、过激活函数（sigmoid / relu / tanh）等。
- **`mask_mod`**：根据**下标**返回一个布尔值，决定某个位置是否要被遮蔽（典型如因果遮蔽：只允许 query 看到自己及之前的 key）。返回 `False` 的位置在计算时会被屏蔽。

为什么要分成两个？`attn_script/Readme.md` 解释得很直白：mask 本质上也能用 `score_mod` 实现（比如把屏蔽位置减一个很大的数），但**单独的 `mask_mod` 能让框架走更快的 block-mask 代码路径**，所以性能更好。这是「表达力 vs 性能」的一个工程取舍。

#### 4.1.2 核心流程

两者在注意力计算中的位置：

```
scores = q @ k            # 原始分数
scores = block_mask(scores)   # 由 mask_mod 生成的块级遮蔽
scores = score_mod(scores)    # 逐元素数值变换
p = online_func(scores)       # 交给在线算法（下一节）
```

`mask_mod` 的函数签名是 `(b, h, q_idx, kv_idx) -> Bool`，其中：

- `b`：batch 下标
- `h`：head 下标
- `q_idx`：query 序列下标
- `kv_idx`：key/value 序列下标

`score_mod` 的函数签名是 `(score, custom_fwd_inputs, b, h, q_idx, kv_idx) -> Tensor`，比 `mask_mod` 多了两个参数：`score`（当前分数张量）和 `custom_fwd_inputs`（额外输入，见 4.3）。

注意一个细节：对于 transformer 注意力，query/key 的缩放是**塞进 `score_mod` 里**做的（`return score * softmax_scale`），框架并没有单独暴露 `query_mod`/`key_mod`——那组接口属于线性注意力（u4-l1）。

#### 4.1.3 源码精读

先看 `mha.py` 里的两个组件：因果遮蔽与缩放。

[attn_script/mha.py:17-18](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py#L17-L18) 定义了 `causal_mask`：query 下标 `q_idx` 必须 ≥ key 下标 `kv_idx`，即「只能看过去」，这是标准的因果注意力遮蔽。

[attn_script/mha.py:23-25](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py#L23-L25) 定义了 `score_mod`：把 scores 乘以缩放系数 `softmax_scale = 1/D**0.5`。

再看 `sigmoidattn.py`，它的 `score_mod` 更丰富——先加偏置，再过 sigmoid：

[attn_script/sigmoidattn.py:14-18](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/sigmoidattn.py#L14-L18) 用 `((score*0.5).tanh() + 1) * 0.5` 计算了一个 tanh 近似的 sigmoid，并从 `custom_fwd_inputs` 取出偏置 `softmax_bias` 相加。

最后是官方签名，[docs/API.md:50-63](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/docs/API.md#L50-L63) 给出 `score_mod` 的参数说明，[docs/API.md:67-78](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/docs/API.md#L67-L78) 给出 `mask_mod` 的参数说明。

#### 4.1.4 代码实践

**实践目标**：理解 `score_mod` 如何承载不同的逐元素变换。

**操作步骤**（源码阅读型，无需 GPU）：

1. 打开 `attn_script/mha.py` 与 `attn_script/sigmoidattn.py`，并排对比两者的 `score_mod`。
2. 打开 `attn_script/reluattn.py`，阅读它的 `score_mod`：

   [attn_script/reluattn.py:14-17](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/reluattn.py#L14-L17) 先做缩放 `score * scores_scale`，再执行 `score = score.max(0)`。

3. 思考：`score.max(0)` 在这里是什么意思？

**需要观察的现象**：注意 `score.max(0)` **不是**「沿第 0 轴求最大值（规约）」，而是「与标量 0 逐元素取最大」，即 \(\max(\text{score}, 0)\)，这正是 relu 的定义。原因在于框架的符号类型里，`max(other)` 被实现成逐元素算子（`self.op(Max, [other])`），而真正的行规约用的是 `get_reduce("max")`——两者在 [attention_engine/core/transform/core.py:246-247](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L246-L247) 与 [attention_engine/core/transform/core.py:262-273](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L262-L273) 分别定义，务必区分。

**预期结果**：你能用一句话说清 `mha.py` 的 `score_mod` 做了「缩放」，`sigmoidattn.py` 做了「加偏置 + sigmoid」，`reluattn.py` 做了「缩放 + relu」。三者的函数签名完全一致，区别只在函数体。

#### 4.1.5 小练习与答案

**练习 1**：为什么几乎所有注意力都要在 `score_mod` 里把 scores 乘以 \(1/\sqrt{D}\)？

> **答案**：\(q @ k\) 的内积会随 head 维度 \(D\) 增大而方差变大，导致 softmax 落到饱和区、梯度消失。乘以 \(1/\sqrt{D}\) 把方差缩回 1 附近，这是经典的 scaled dot-product attention。

**练习 2**：`mask_mod` 返回 `False` 的位置，最终会被怎样处理？

> **答案**：这些位置在 attention 分数中被屏蔽（等价于不参与 softmax 的归一化与加权和）。框架把 `mask_mod` 编译成块级 `block_mask`，比在 `score_mod` 里手工写「减大数」更高效（详见 u2-l8 的 mask 机制）。

### 4.2 OnlineFunc 抽象：行级在线算法的四个方法

#### 4.2.1 概念说明

`OnlineFunc` 是四个组件里**最抽象、也最关键**的一个。它描述的是「如何把 scores 变成最终的概率 \(p\) 并和 \(v\) 累积成输出 \(o\)」，但用的是**在线（分块）算法**——不必一次性物化整张 \(N \times N\) 的 scores 矩阵。

为什么需要在线算法？以 softmax 注意力为例，分母 \(\sum_j \exp(s_j)\) 需要看到整行。如果在 GPU 上真的物化整张 scores，显存和访存都吃不消。online softmax 的诀窍是：**逐块累加**一个「当前行的最大值 \(m\)」和「当前未归一化的指数和 \(r\)」，每来一个新的 KV 块就用递推公式更新它们，并对已经累积的 \(o\) 做一次重缩放（rescale）。这样全程只需要 \(O(\text{block})\) 的中间状态。

框架预置了几种 `OnlineFunc`：

- `OnlineSoftmax`（mha.py 用）：标准 online softmax。
- `OnlineIdentity`（sigmoidattn.py、reluattn.py 用）：恒等，\(p = \text{scores}\)，不做归一化——因为 sigmoid/relu 本身就是逐元素的，不需要行规约。
- 用户也可继承基类自定义。

#### 4.2.2 核心流程

一个 `OnlineFunc` 子类需要实现四个方法（外加 `__init__` 声明状态）：

| 方法 | 何时调用 | 作用 |
| --- | --- | --- |
| `__init__` | 编译期 | 声明 `online_rowscales`（中间状态）与 `final_rowscales`（最终状态，供反向用）。 |
| `online_fwd(scores, online_rowscales, b, h, q_idx)` | 前向每个 KV 块 | 用当前块 scores 更新中间状态，返回 `(scores, new_rowscales, o_scale)`，其中 `o_scale` 用于重缩放已累积的 \(o\)。 |
| `online_fwd_epilogue(o, online_rowscales, b, h, q_idx)` | 前向收尾 | 用中间状态把 \(o\) 归一化，并把最终状态存进 `final_rowscales`。 |
| `forward(scores, final_rowscales, b, h, q_idx, kv_idx)` | 反向重算 | 在反向时由保存的 `final_rowscales` 重算 \(p\)。 |
| `backward(dp, scores, final_rowscales, doosum, ...)` | 反向 | 由上游梯度 `dp` 算出 `dscores`。 |

对 online softmax，单个 KV 块的递推关系为（设当前块分数为 \(s\)、历史最大值 \(m\)、历史指数和 \(r\)）：

\[
m_{\text{new}} = \max\bigl(m,\; \max_j s_j\bigr)
\]

\[
\text{o\_scale} = \exp(m - m_{\text{new}})
\]

\[
r_{\text{new}} = r \cdot \text{o\_scale} + \sum_j \exp(s_j - m_{\text{new}})
\]

每块的输出累积为 \(o \leftarrow o \cdot \text{o\_scale} + \exp(s - m_{\text{new}}) \cdot v\)。全部块处理完后，收尾（epilogue）做归一化并保存 log-sum-exp：

\[
o \leftarrow o / r, \qquad \text{lse} = \log(r) + m
\]

这里 `lse`（log-sum-exp）就是 `final_rowscales`，它在反向时用来重新归一化，避免重复计算。

#### 4.2.3 源码精读

`mha.py` 的 `OnlineSoftmax` 是理解在线算法的最佳入口。

先看状态声明：[attn_script/mha.py:27-44](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py#L27-L44)。`online_rowscales` 里 `m` 初始化为 \(-\infty\)（`Var("-inf")`）、`r` 初始化为 `0.0`；`final_rowscales` 里 `lse` 初始化为 `0.0`。注意这些初值都是符号对象 `SymbolScalar`，不是普通浮点数——它们会被记录进符号 IR。

接着看核心递推 [attn_script/mha.py:47-63](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py#L47-L63)，逐行对应上面的公式：`m_new = m.max(scores.get_reduce("max"))` 是新的最大值；`scale_tmp = (m - m_new).exp()` 即 `o_scale`；`r = r*scale_tmp + scores.get_reduce("sum")` 是新的指数和。

收尾 [attn_script/mha.py:65-72](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py#L65-L72) 做归一化 `o / r` 并保存 `lse = log(r) + m`。

反向的两段：[attn_script/mha.py:74-78](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py#L74-L78) 用 `lse` 重算 \(p = \exp(s - \text{lse})\)；[attn_script/mha.py:80-84](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py#L80-L84) 由 `dp` 算出 `dscores = (dp - dppsum) * scores`（softmax 反向的经典式子，其中 `dppsum` 是 `doosum`，即「下游梯度按行求和」）。

对比一下「什么都不做」的 `OnlineIdentity`，[attn_script/sigmoidattn.py:34-49](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/sigmoidattn.py#L34-L49)：它的 `online_rowscales` 与 `final_rowscales` 都是**空字典**，`online_fwd` 直接返回 `o_scale = 1`，`online_fwd_epilogue` 原样返回 \(o\)。这就是「无行规约」的在线路径——\(p\) 就等于 scores 本身，\(o\) 直接累积 \(\text{scores} \cdot v\)，无需任何重缩放或归一化。

这四个方法的基类原型在 [attention_engine/attn_engine/attn_engine.py:16-92](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L16-L92)（`__init__` 在 [L30-L45](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L30-L45)，`online_fwd` 在 [L47-L61](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/attn_engine/attn_engine.py#L47-L61)），官方说明见 [docs/API.md:32-48](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/docs/API.md#L32-L48)。

> 重要约束：`attn_script/Readme.md` 在「Limitation」一节明确指出，`online_func` **不支持自动微分**，所以 `forward` 和 `backward` 两个方法必须由用户手写；且反向**只计算 q/k/v 的梯度**，不包含 custom input 张量的梯度。这是后续实践对齐时要注意的边界（详见 u5-l7）。

#### 4.2.4 代码实践

**实践目标**：理解 online softmax 递推中每一步的符号含义。

**操作步骤**（源码阅读 + 符号结构观察，无需 GPU）：

1. 打开 `attn_script/mha.py` 第 47–63 行的 `online_fwd`。
2. 对照本节公式，把代码里这五行的「数学含义」写在旁边：

   - `m_new = m.max(scores.get_reduce("max"))`
   - `scale_tmp = (m - m_new).exp()`
   - `r = r * scale_tmp`
   - `scores = (scores - m_new).exp()`
   - `r = r + scores.get_reduce("sum")`

3. 思考：为什么先 `r = r * scale_tmp` 再加新的 `sum`？如果反过来会怎样？

**需要观察的现象**：注意 `m.max(...)` 是「标量 m 与一个规约结果取最大」，而 `scores.get_reduce("max")` 才是「scores 这一行求最大」。前者是逐元素 max，后者是行规约——这和 4.1 里 `score.max(0)` 的区分是同一套机制。

**预期结果**：你能解释「先用 `scale_tmp` 重缩放历史 `r`，是为了让历史累积值与当前块统一到新的最大值 \(m_{\text{new}}\) 基准下」；若反过来先加再缩放，`r` 的两部分会处在不同基准，数值就错了。

#### 4.2.5 小练习与答案

**练习 1**：`OnlineSoftmax` 与 `OnlineIdentity` 的 `online_rowscales` 有何本质区别？

> **答案**：`OnlineSoftmax` 需要跨块维护行最大值 `m` 和指数和 `r`（两个状态），因为 softmax 要做行规约；`OnlineIdentity` 的 `online_rowscales` 是空字典，因为它不做任何行级归一化，\(p\) 就等于 scores。

**练习 2**：`final_rowscales["lse"]` 是在哪一步产生、又在哪一步被消费的？

> **答案**：在 `online_fwd_epilogue` 中由 `log(r) + m` 产生并保存；在反向的 `forward` 方法中作为 `final_rowscales` 传入，用来重算 \(p = \exp(s - \text{lse})\)。

### 4.3 CustomIO：自定义输入张量

#### 4.3.1 概念说明

`CustomIO` 用来声明那些**不属于 q/k/v、但需要参与注意力计算**的额外输入张量。典型场景：

- 可学习的 attention bias（`softmax_bias`），形状通常是 `(1,)` 或 `(num_heads,)`，每个 head 一个偏置。
- 缩放系数 `softmax_scale`（虽然 mha.py 里用了 Python 全局变量，但也可以声明成 CustomIO 传入）。

它的作用是把「编译期声明形状」和「运行期传具体张量」解耦：编译时你在 `CustomIO({"名字": 形状})` 里声明，框架据此生成 kernel 参数；运行时你把真实张量通过 `mod(q, k, v, custom_inputs=[...])` 传进去。

#### 4.3.2 核心流程

使用 `CustomIO` 分三步：

```
# 1. 编译期：声明有哪些额外张量、各自形状
custom_fwd_inputs = CustomIO({"softmax_bias": (1,)})

# 2. 编译期：在 score_mod 里按名字取用
def score_mod(score, custom_fwd_inputs, b, h, q_idx, kv_idx):
    bias = custom_fwd_inputs.input_tensors["softmax_bias"]
    return score + bias

# 3. 运行期：把真实张量传进去
output = mod(q, k, v, custom_inputs=[bias_tensor])
```

`CustomIO` 内部把每个声明的张量包装成 `SymbolicTensor`，存进 `self.input_tensors` 字典（键是名字）。`score_mod` 通过 `custom_fwd_inputs.input_tensors["名字"]` 访问。即使是「没有额外输入」，也要传一个空的 `CustomIO()`。

#### 4.3.3 源码精读

`CustomIO` 类的定义很短，[attention_engine/core/transform/core.py:331-349](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attention_engine/core/transform/core.py#L331-L349)：构造函数遍历传入的 `input_tensors` 字典，把每个 `(名字, 形状)` 包装成 `SymbolicTensor(名字, 形状)` 存起来。

看实际用法，`sigmoidattn.py` 声明了一个偏置：[attn_script/sigmoidattn.py:51-53](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/sigmoidattn.py#L51-L53) 用 `CustomIO({"softmax_bias": (1,)})`，形状 `(1,)` 表示一个标量偏置。然后在 `score_mod` 里 [attn_script/sigmoidattn.py:15](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/sigmoidattn.py#L15) 取出并相加。

对比 `mha.py`：[attn_script/mha.py:103-105](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py#L103-L105) 用了**空的** `CustomIO({})`（注意里面被注释掉的 `"softmax_scale"` 行——作者本可以把缩放系数做成 CustomIO，但最终选择用 Python 全局变量）。这说明：缩放系数这类「编译期就固定」的常量，可以直接用 Python 变量写死；只有需要运行期动态传入的张量，才必须走 `CustomIO`。

#### 4.3.4 代码实践

**实践目标**：把 `mha.py` 的缩放系数从 Python 全局变量改造成 `CustomIO`。

**操作步骤**：

1. 复制 `attn_script/mha.py` 为 `mha_custom_scale.py`（**示例文件，不要提交到原仓库**）。
2. 把 `custom_fwd_inputs = CustomIO({})` 改成：

   ```python
   custom_fwd_inputs = CustomIO({"softmax_scale": (1,)})
   ```

3. 把 `score_mod` 改成从 `custom_fwd_inputs` 取缩放系数：

   ```python
   def score_mod(score, custom_fwd_inputs, b, h, q_idx, kv_idx):
       softmax_scale = custom_fwd_inputs.input_tensors["softmax_scale"]
       return score * softmax_scale
   ```

4. 构造 `mod` 后，调用时传入真实张量：`mod(q, k, v, custom_inputs=[torch.tensor([1.0/D**0.5], ...)])`。

**需要观察的现象**：改造后，`score_mod` 的签名不变，但它依赖的缩放系数现在来自「运行期张量」而非「编译期常量」。

**预期结果**：`score_mod` 逻辑等价，但缩放系数可在每次前向调用时动态变化（例如不同 head 用不同缩放）。**实际运行（含 TileLang 编译）需要 u1-l2 搭建的 GPU 环境，运行结果待本地验证。**

#### 4.3.5 小练习与答案

**练习 1**：为什么 `mha.py` 的 `softmax_scale` 可以不放进 `CustomIO`，而 `sigmoidattn.py` 的 `softmax_bias` 必须放进 `CustomIO`？

> **答案**：`softmax_scale` 在编译期就固定（只取决于 head 维度 D），可用 Python 全局变量直接写进 kernel；`softmax_bias` 通常是可学习参数，每次前向的值都可能变化，必须作为运行期张量通过 `CustomIO` 传入。

**练习 2**：如果某次注意力完全不需要额外输入，`custom_fwd_inputs` 能不能省略不传？

> **答案**：不能。`AttentionEngine` 的构造签名要求传入 `custom_fwd_inputs`，没有额外输入时也要传一个空的 `CustomIO()`（如 mha.py 所做）。

### 4.4 四件套如何拼成一次注意力计算

#### 4.4.1 概念说明

前三个模块分别讲了四个组件，这一节把它们「拼」起来，让你建立完整的心智模型：用户写的四个 Python 函数，到底是如何对应到一次注意力前向 + 反向的。

关键认知：AttentionEngine 的计算结构是固定的（scores → mask → score_mod → online → 累积 o），用户能定制的只是这条流水线上的四个「插槽」。

#### 4.4.2 核心流程

前向（每个 KV 块循环）：

```
scores = q @ k                       # GEMM 1
scores = block_mask(scores)          # 由 mask_mod 生成
scores = score_mod(scores)           # 逐元素变换（含 custom_fwd_inputs）
scores, rowscales, o_scale = online_func.online_fwd(scores, rowscales)
o = o * o_scale                      # 重缩放历史累积
o = o + scores @ v                   # GEMM 2，累积
# 所有块结束：
o, final_rowscales = online_func.online_fwd_epilogue(o, rowscales)
```

反向（用保存的 `final_rowscales` 重算）：

```
scores = q @ k → block_mask → score_mod
scores = online_func.forward(scores, final_rowscales)   # 重算 p
dv = scores^T @ do
dp = do @ v^T
dscores = online_func.backward(dp, scores, final_rowscales, doosum)
dk = dscores^T @ q
dq = dscores @ k
```

这套伪代码几乎逐字来自 [attn_script/Readme.md](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/Readme.md)：前向模板见 [Readme.md:44-65](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/Readme.md#L44-L65)，反向模板见 [Readme.md:68-77](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/Readme.md#L68-L77)。

#### 4.4.3 源码精读

`attn_script/Readme.md` 的总览 [Readme.md:19-31](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/Readme.md#L19-L31) 用一段精简伪代码点明了四件套的相对位置（注意它额外画出了 `query_mod`/`key_mod`/`value_mod`，那是线性注意力的接口；transformer 注意力里 q/k 的缩放被吸收进 `score_mod`）。

回到真实样例，`mha.py` 把四个组件组装进 `AttentionEngine`：[attn_script/mha.py:109-117](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py#L109-L117)。`sigmoidattn.py` 的组装几乎一模一样，只是把 `OnlineSoftmax` 换成 `OnlineIdentity`、`score_mod` 不同、并带了 `CustomIO({"softmax_bias": (1,)})`：[attn_script/sigmoidattn.py:64-68](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/sigmoidattn.py#L64-L68)。

构造函数的官方原型在 [docs/API.md:12-18](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/docs/API.md#L12-L18)，运行期调用方式在 [docs/API.md:22-30](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/docs/API.md#L22-L30)。

#### 4.4.4 代码实践

**实践目标**：在脑中（或纸上）跑通一次 `mha.py` 的完整调用。

**操作步骤**：

1. 打开 `attn_script/mha.py`，从 `__main__` 块 [L86-L120](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/mha.py#L86-L120) 开始。
2. 列出构造 `AttentionEngine` 时传入的四个组件分别对应哪段代码：`qkv_meta`（L97-L101）、`custom_fwd_inputs`（L103-L105）、`score_mod`（L23）、`mask_mod`（L17）、`online_func`（L107 实例化的 `OnlineSoftmax`）。
3. 把本节的「前向流程」伪代码逐行对应到这些组件：哪一行用了 `causal_mask`？哪一行用了 `score_mod`？`online_fwd` 在哪？

**需要观察的现象**：你会看到四个组件是**正交**的——换掉其中任何一个，其余三个都不用改。

**预期结果**：你能说出「把 `OnlineSoftmax` 换成 `OnlineIdentity`、`score_mod` 换成 sigmoid 版本，mha.py 就变成了 sigmoidattn.py」。

#### 4.4.5 小练习与答案

**练习 1**：在反向流程中，`score_mod` 出现在哪一步？为什么反向还需要它？

> **答案**：反向里 `scores = q@k → block_mask → score_mod` 出现在重算 scores 的步骤。因为反向需要重新计算 \(p\)（以及 `dscores`），而 scores 的定义本身就包含 `score_mod` 的逐元素变换，所以反向必须重新执行一遍 `score_mod`（这部分由符号自动微分处理，见 u2-l5）。

**练习 2**：如果两个注意力只差 `mask_mod`（一个因果、一个全连接），它们的 `online_func` 可以共用吗？

> **答案**：可以。`online_func` 只关心 scores 的**数值**如何归一化/累积，不关心哪些位置被遮蔽——遮蔽是 `mask_mod`/`block_mask` 的事。所以同一个 `OnlineSoftmax` 既能配因果 mask，也能配全连接 mask。

## 5. 综合实践

**实践任务**：在 `sigmoidattn.py` 的基础上，把 `score_mod` 里的 sigmoid 逐元素变换改写成 relu，并说明此时应该搭配哪种 `OnlineFunc`、会走哪条 online 路径。

**为什么做这个**：它同时触及 `score_mod`（4.1）、`OnlineFunc`（4.2）和 `CustomIO`（4.3，需要决定是否保留偏置），是把本讲三个模块串起来的综合练习。项目里恰好有 `attn_script/reluattn.py` 作为参照答案。

**操作步骤**：

1. 复制 `attn_script/sigmoidattn.py` 为 `reluattn_from_sigmoid.py`（**示例文件**）。
2. 修改 `score_mod`，把 sigmoid 换成 relu。语义上 relu 是 \(\max(x, 0)\)，在框架里可以直接写成逐元素 max：

   ```python
   def score_mod(score, custom_fwd_inputs, b, h, q_idx, kv_idx):
       score = score + custom_fwd_inputs.input_tensors["softmax_bias"]
       score = score.max(0)      # relu = max(x, 0)，逐元素（非规约）
       return score
   ```

   （若想保留偏置就保留 `CustomIO({"softmax_bias": (1,)})`；若不要偏置可简化为空 `CustomIO()` 并去掉加法——这与 `reluattn.py` 一致。）

3. 把 `online_func=OnlineIdentity()` 保留（sigmoidattn 已经在用 `OnlineIdentity`，relu 同样不需要行规约，所以**继续用 `OnlineIdentity`**，**不能**用 `OnlineSoftmax`）。
4. 构造 `mod` 并用 `from benchmark.bench_utils import do_bench_reluattn` 做正确性对齐（参照 [attn_script/reluattn.py:77-78](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/reluattn.py#L77-L78)）。

**应搭配的 `OnlineFunc` 与 online 路径**：因为 relu 是**逐元素**变换，\(p = \text{relu}(\text{scores})\) 已经就是最终概率，**不需要任何行规约或归一化**，所以搭配 `OnlineIdentity`。它走的 online 路径是「恒等路径」：

- `online_fwd` 返回 `o_scale = 1`，scores 原样返回；
- \(o\) 直接累积 \(\text{scores} \cdot v\)，不做重缩放；
- `online_fwd_epilogue` 原样返回 \(o\)，`final_rowscales` 为空；
- 反向 `backward` 直接返回 `dp`（relu 的导数是 0/1，已被符号自动微分处理）。

这与 sigmoidattn 走的是**同一条** online 路径——两者都属于「逐元素变换 + Identity」这一类。

**需要观察的现象**：把你的 `score_mod` 与官方 [attn_script/reluattn.py:14-17](https://github.com/tile-ai/AttentionEngine/blob/b7088e28f5ba083b81d641861c4c1ef703616e21/attn_script/reluattn.py#L14-L17) 对照，核心逻辑应一致（缩放 + `max(0)`）。

**预期结果**：生成一个 relu attention kernel，前向输出 \(o = \text{relu}(qk^T) \cdot v\)，与 PyTorch 参考实现数值对齐。**完整运行（编译 + bench）依赖 u1-l2 的 GPU 环境，运行结果待本地验证。**

## 6. 本讲小结

- 用户通过**四个组件**描述一次自定义注意力：`score_mod`（逐元素数值变换）、`mask_mod`（下标布尔遮蔽）、`online_func`（行级在线算法）、`custom_fwd_inputs`（`CustomIO`，额外输入张量）。
- 它们在流水线中的位置是固定的：`scores = q@k → block_mask(mask_mod) → score_mod → online_func → o = p@v`。
- `OnlineFunc` 是最关键也最抽象的组件，需手写 `__init__`/`online_fwd`/`online_fwd_epilogue`/`forward`/`backward` 五个方法；它**不支持自动微分**，且反向只算 q/k/v 梯度。
- 区分两类在线路径：`OnlineSoftmax`（带 `m`/`r`/`lse` 状态，需要行规约）与 `OnlineIdentity`（空状态，逐元素变换用）。
- `CustomIO` 解耦「编译期声明形状」与「运行期传张量」；没有额外输入也要传空的 `CustomIO()`。
- 四个组件**正交**：换掉其一，其余不用改——这正是 mha.py 与 sigmoidattn.py 只差几个组件的原因。

## 7. 下一步学习建议

本讲只讲了「用户要写什么」，没有讲「框架如何把这四个 Python 函数编译成 kernel」。接下来：

- **进入第二单元**：从 `u2-l1 符号表示基础：Node 图与算子` 开始，理解 transform 层如何把 `score_mod`/`online_func` 里的运算记录成符号 DAG。这是理解整个编译链的起点。
- 特别地，`u2-l2 SymbolScalar / Symbolic array` 会深入讲本讲反复出现的 `SymbolScalar`、`.exp()`、`.max()`、`.get_reduce("max")` 到底是什么——本讲把它们当「会记录成符号的黑盒」用了，下一讲会打开这个黑盒。
- 如果你想先看到「四件套拼出来的 kernel 长什么样」，可以跳读 `u5-l6 测试与调试技巧`里导出生成代码的方法，但理解细节仍建议按第二单元顺序学习。
