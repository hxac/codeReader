# Qwen3 模型结构详解

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `Qwen3ForCausalLM → Qwen3Model → Qwen3DecoderLayer` 这套嵌套结构里，每一层各自的职责。
- 画出**单个 token** 在一个 `Qwen3DecoderLayer` 内的完整前向路径：残差归一化 → 注意力 → 残差归一化 → MLP。
- 解释 Qwen3 区别于经典 Llama 的两个关键点：**QK-Norm**（对 q/k 在 `head_dim` 上做 RMSNorm）与 **显式 `head_dim`**（如 `head_dim=128` 而非 `hidden_size/num_heads`）。
- 说清 RoPE（旋转位置编码）在 attention 中的插入位置与「rotate-half」公式。
- 理解 `tie_word_embeddings` 与 `compute_logits` 的关系：权重绑定时，输出投影直接复用输入词表嵌入矩阵。

本讲只讲「模型结构」本身——也就是从 `hidden_states` 进、`hidden_states` 出的那条前向链路。至于注意力如何读写 paged KV cache、`Context` 如何传递元数据，已在 [u4-l2](./u4-l2-attention-triton-kernel.md) 与 [u4-l3](./u4-l3-context-metadata.md) 讲过，本讲会复用这些结论，不再重复。

## 2. 前置知识

在进入源码前，先用最朴素的方式建立几个直觉。

### 2.1 Transformer Decoder 大致长什么样

一个 decoder-only Transformer 由若干「**解码层（decoder layer）**」堆叠而成。每一层内部是两个子模块串联：

```text
输入 x
  ├─ 残差归一化  →  自注意力（Self-Attention）  ── 残差相加
  ├─ 残差归一化  →  前馈网络（MLP）            ── 残差相加
输出 y
```

「残差相加」是指把子模块的输出与它的输入相加（\(y = x + \text{SubLayer}(x)\)），让梯度可以走捷径。「归一化」通常用 **RMSNorm**，比 LayerNorm 少算一个均值，更快。

### 2.2 什么是 RMSNorm

RMSNorm 只用**均方根**来归一化，不减均值：

\[
\text{RMSNorm}(x) = \frac{x}{\sqrt{\dfrac{1}{d}\sum_{i=1}^{d} x_i^2 + \epsilon}} \odot \gamma
\]

其中 \(\gamma\) 是可学习的缩放权重（本讲里就是 `self.weight`），\(\odot\) 表示逐元素相乘。nano-vllm 还把「残差相加」与「RMSNorm」融合进同一个算子，后面会详细看。

### 2.3 什么是 RoPE（旋转位置编码）

注意力本身对位置无感——打乱 token 顺序结果不变。要让模型感知顺序，需要在 q、k 上注入位置信息。RoPE 的做法是：把 q/k 的维度两两配对，按位置 \(m\) 旋转一个角度：

\[
\theta_i = \text{base}^{-2i/d},\qquad f_{m,i} = m\,\theta_i
\]

\[
\begin{cases}
x_i' = x_i\cos f_{m,i} - x_{i+d/2}\sin f_{m,i}\\[2pt]
x_{i+d/2}' = x_{i+d/2}\cos f_{m,i} + x_i\sin f_{m,i}
\end{cases}
\]

直觉上：位置越靠后（\(m\) 越大），旋转角度越大；不同维度用不同频率 \(\theta_i\)，从而编码出区分性。RoPE 的好处是「相对位置」天然体现在 q·k 的内积里，且外推性好。

### 2.4 Qwen3 比经典 Llama 多了什么

两个值得注意的差异，本讲会反复提到：

1. **QK-Norm**：在算完 q、k 之后、做 RoPE 之前，对 q、k 各自在 `head_dim` 维度上再做一次 RMSNorm。这让训练更稳定。
2. **显式 `head_dim`**：Qwen3-0.6B 的 `head_dim=128`，但 `hidden_size/num_heads = 1024/16 = 64`。也就是说每个头的维度并不等于「隐藏宽度除以头数」，而是单独配置的。这会影响 q/k/v 投影的输出宽度。

> 说明：下文提到 Qwen3-0.6B 的具体数值（`hidden_size=1024`、`num_hidden_layers=28`、`num_attention_heads=16`、`num_key_value_heads=8`、`head_dim=128`、`intermediate_size=3072`、`vocab_size=151936`、`tie_word_embeddings=True`、`rope_theta=1000000`、`attention_bias=False`）均取自该模型 HF 仓库的 `config.json`，建议你在本地下载权重后对照 `config.json` 确认一次。

## 3. 本讲源码地图

本讲围绕 4 个核心文件展开，外加 2 个「接口侧」文件用于说明调用与权重绑定：

| 文件 | 作用 |
| --- | --- |
| [nanovllm/models/qwen3.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py) | 本讲主角：`Qwen3Attention` / `Qwen3MLP` / `Qwen3DecoderLayer` / `Qwen3Model` / `Qwen3ForCausalLM` 全在这里。 |
| [nanovllm/layers/layernorm.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/layernorm.py) | `RMSNorm`：含普通归一化与「融合残差相加 + 归一化」两个分支。 |
| [nanovllm/layers/rotary_embedding.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/rotary_embedding.py) | `RotaryEmbedding`：预计算 cos/sin 缓存，对 q、k 施加 RoPE。 |
| [nanovllm/layers/activation.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/activation.py) | `SiluAndMul`：MLP 的门控激活。 |
| [nanovllm/layers/embed_head.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/embed_head.py) | `VocabParallelEmbedding` / `ParallelLMHead`：输入嵌入与输出投影，权重绑定的落点。 |
| [nanovllm/engine/model_runner.py](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py) | 构造模型、调用 `forward` 与 `compute_logits` 的上层。 |

读源码时请记住一条主线：`Qwen3ForCausalLM` 只是一个外壳，真正干活的是它内部的 `Qwen3Model`；`Qwen3Model` 是一堆 `Qwen3DecoderLayer` 的循环；每个 layer 里又是「注意力 + MLP」。所以阅读顺序建议**自顶向下**：先看外壳，再进循环，最后钻进 attention 和 MLP。

## 4. 核心概念与源码讲解

### 4.1 Qwen3ForCausalLM 与 Qwen3Model：整体骨架与权重绑定

#### 4.1.1 概念说明

`Qwen3ForCausalLM` 是暴露给 `ModelRunner` 的最外层类。它本身不做太多计算，主要承担三件事：

1. **持有 `Qwen3Model`**（骨干网络，产出 `hidden_states`）。
2. **持有一个 `ParallelLMHead`**（把 `hidden_states` 投影成词表上的 logits）。
3. **处理权重绑定**：若 `tie_word_embeddings=True`，让输出头的权重与输入嵌入矩阵共享同一块显存。

为什么要分 `ForCausalLM` 和 `Model` 两层？这是 HF Transformers 的惯例：`Model` 负责「编码到 hidden_states」，`ForCausalLM` 在其上加一个语言建模头（LM head）负责「hidden_states 到 logits」。nano-vllm 沿用了这个分层，方便对照 HF 的权重名做加载。

#### 4.1.2 核心流程

外层前向非常薄：

```text
forward(input_ids, positions):
    return model(input_ids, positions)        # 只到 hidden_states
```

注意：`forward` **不算 logits**，只返回 `hidden_states`。logits 的计算被单独拆到 `compute_logits` 方法里。`ModelRunner` 会根据情况决定何时调用 `compute_logits`（见 [nanovllm/engine/model_runner.py:L196-L198](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L196-L198)）：

```text
run_model(input_ids, positions, is_prefill):
    if is_prefill or enforce_eager or batch > 512:
        return model.compute_logits(model(input_ids, positions))   # 算完 hidden 立刻算 logits
    else:
        ... CUDA Graph 回放后, 再 compute_logits(graph_outputs) ...
```

之所以这样拆，是因为 decode 阶段要走 CUDA Graph（见 [u5-l1](./u5-l1-cuda-graph.md)），图内只录到 `hidden_states`，logits 在图外补算。

而 `Qwen3Model.forward` 才是真正的循环骨架：

```text
forward(input_ids, positions):
    hidden = embed_tokens(input_ids)          # 词表嵌入: (N) -> (N, hidden_size)
    residual = None
    for layer in layers:                       # 逐层堆叠
        hidden, residual = layer(positions, hidden, residual)
    hidden, _ = norm(hidden, residual)         # 最后一层的残差在这里收口
    return hidden
```

#### 4.1.3 源码精读

外层类与权重绑定见 [nanovllm/models/qwen3.py:L186-L216](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L186-L216)：

```python
class Qwen3ForCausalLM(nn.Module):
    packed_modules_mapping = { ... }                 # 见下文「权重加载」说明

    def __init__(self, config: Qwen3Config) -> None:
        super().__init__()
        self.model = Qwen3Model(config)
        self.lm_head = ParallelLMHead(config.vocab_size, config.hidden_size)
        if config.tie_word_embeddings:
            self.lm_head.weight.data = self.model.embed_tokens.weight.data
    ...
    def compute_logits(self, hidden_states):
        return self.lm_head(hidden_states)
```

几个要点：

- **`lm_head.weight.data = embed_tokens.weight.data`**（[L202-L203](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L202-L203)）：这是「权重绑定」的关键。`.data =` 让两者共享底层显存——之后修改嵌入矩阵，输出头自动同步。Qwen3-0.6B 的 `tie_word_embeddings=True`，所以走这条分支。
- **`ParallelLMHead` 继承自 `VocabParallelEmbedding`**（[nanovllm/layers/embed_head.py:L45-L66](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/embed_head.py#L45-L66)）：输出投影本质就是「嵌入矩阵的转置乘法」（`F.linear(x, self.weight)`），所以它能复用同一份权重。这也解释了为什么权重绑定只需一行赋值。
- **`packed_modules_mapping`**（[L187-L193](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L187-L193)）：这是给权重加载用的，把 HF checkpoint 里分开的 `q_proj/k_proj/v_proj` 映射到合并后的 `qkv_proj`，`gate_proj/up_proj` 映射到 `gate_up_proj`。**它不影响前向**，前向看不到这个字典；细节留到 [u5-l4](./u5-l4-weight-loading.md)。

骨干网络见 [nanovllm/models/qwen3.py:L162-L183](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L162-L183)：

```python
class Qwen3Model(nn.Module):
    def __init__(self, config):
        self.embed_tokens = VocabParallelEmbedding(config.vocab_size, config.hidden_size)
        self.layers = nn.ModuleList([Qwen3DecoderLayer(config) for _ in range(config.num_hidden_layers)])
        self.norm = RMSNorm(config.hidden_size, eps=config.rms_norm_eps)

    def forward(self, input_ids, positions):
        hidden_states = self.embed_tokens(input_ids)
        residual = None
        for layer in self.layers:
            hidden_states, residual = layer(positions, hidden_states, residual)
        hidden_states, _ = self.norm(hidden_states, residual)
        return hidden_states
```

注意最后那行 `self.norm(hidden_states, residual)`——它带两个参数，触发的是 RMSNorm 的「融合残差相加 + 归一化」分支（见 4.2）。这说明：**最后一层 MLP 的输出并没有在 layer 内部加回残差**，而是被推迟到这里，与最终归一化一起做。这是有意的内存带宽优化，下一节会把这条线索彻底讲透。

#### 4.1.4 代码实践

**实践目标**：确认权重绑定确实生效。

**操作步骤**（需 GPU 与已加载的模型，若本地暂无环境可只做源码阅读）：

1. 在 [nanovllm/engine/model_runner.py:L31-L32](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/engine/model_runner.py#L31-L32) 之后，临时插入一行打印（运行后请还原，不要提交）：
   ```python
   print("tied:", self.model.lm_head.weight.data_ptr() == self.model.model.embed_tokens.weight.data_ptr())
   ```
2. 用 Qwen3-0.6B 跑一次 `example.py`。

**需要观察的现象 / 预期结果**：打印应为 `tied: True`，说明输出头与输入嵌入指向同一块显存（`data_ptr` 相同）。若改为 `tie_word_embeddings=False` 的模型，则应为 `False`。

> 若本地无 GPU 环境，可改为阅读 [L202-L203](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L202-L203) 并说明：`.data =` 赋值在 PyTorch 中是「共享存储」而非拷贝，因此后续对任一矩阵的 in-place 修改会同时影响另一个。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Qwen3ForCausalLM.forward` 不直接返回 logits，而要单独提供 `compute_logits`？

**参考答案**：因为 decode 阶段要把前向录制进 CUDA Graph，而 logits 计算（涉及 `gather` 与采样）不适合或不需要进图。把 `hidden_states` 与 logits 解耦后，图内只录到 `hidden_states`，logits 在 `graph.replay()` 之后用 `compute_logits` 补算（见 `run_model` 的两个分支）。

**练习 2**：权重绑定（`lm_head.weight = embed_tokens.weight`）为什么在功能上是合理的？

**参考答案**：语言建模的输出投影是「hidden → 词表每个词的得分」，数学上是 \(W^\top h\)；而输入嵌入是「词 → hidden」，是查表 \(W[\text{token}]\)，二者用的是同一个形状为 `(vocab, hidden)` 的矩阵 \(W\)。 tying 假设「能编码成 hidden 的方向」与「能解码出该词的方向」互为转置，从而共享参数、减少显存并常能带来正则化效果。

---

### 4.2 Qwen3DecoderLayer：残差结构与融合 RMSNorm

#### 4.2.1 概念说明

`Qwen3DecoderLayer` 是构成 Transformer 的标准积木。它内部有四个组件：

- `self_attn`（`Qwen3Attention`）
- `mlp`（`Qwen3MLP`）
- `input_layernorm`（在 attention 之前）
- `post_attention_layernorm`（在 attention 之后、MLP 之前）

本节最值得吃透的不是「有哪些组件」，而是 nano-vllm 用的**融合残差归一化（fused add + RMSNorm）**写法。它把「子模块输出 + 残差」这一步加法，从子模块后面挪到了**下一个 norm 里**一起算，从而省掉一次显存往返。

#### 4.2.2 核心流程

一个 layer 的前向（伪代码，省略 `positions`）：

```text
forward(hidden, residual):
    # ① 输入归一化（attention 之前）
    if residual is None:                      # 第一层：残差就是嵌入本身
        hidden, residual = input_layernorm(hidden), hidden
    else:                                     # 后续层：把上一层的 mlp 输出加回残差, 再归一化
        hidden, residual = input_layernorm(hidden, residual)
    # ② 自注意力
    hidden = self_attn(positions, hidden)
    # ③ 后归一化（attention 之后），同时把 attn 输出加回残差
    hidden, residual = post_attention_layernorm(hidden, residual)
    # ④ MLP（注意：mlp 输出本层不加残差, 留给下一层 input_layernorm 处理）
    hidden = mlp(hidden)
    return hidden, residual
```

关键观察：**「加残差」和「归一化」永远成对发生在两个 norm 函数里**。attention 与 mlp 自己只负责「归一化后的变换」，不碰残差。这让残差通道始终只维护一份「未归一化的累加和」，符合 Pre-Norm Transformer 的数学定义：

\[
h_{\text{attn}} = x + \text{Attn}(\text{Norm}(x))
\]
\[
h_{\text{mlp}} = h_{\text{attn}} + \text{MLP}(\text{Norm}(h_{\text{attn}}))
\]

只不过这里的加法被融合进了下一次 `Norm`。

#### 4.2.3 源码精读

构造见 [nanovllm/models/qwen3.py:L120-L144](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L120-L144)，前向见 [L146-L159](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L146-L159)：

```python
def forward(self, positions, hidden_states, residual):
    if residual is None:
        hidden_states, residual = self.input_layernorm(hidden_states), hidden_states
    else:
        hidden_states, residual = self.input_layernorm(hidden_states, residual)
    hidden_states = self.self_attn(positions, hidden_states)
    hidden_states, residual = self.post_attention_layernorm(hidden_states, residual)
    hidden_states = self.mlp(hidden_states)
    return hidden_states, residual
```

`RMSNorm.forward` 根据「是否传 `residual`」分流到两个 `@torch.compile` 算子，见 [nanovllm/layers/layernorm.py:L42-L50](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/layernorm.py#L42-L50)：

```python
def forward(self, x, residual=None):
    if residual is None:
        return self.rms_forward(x)              # 只归一化
    else:
        return self.add_rms_forward(x, residual)  # 先 x += residual, 再归一化
```

两个分支的实现对照看最清楚。**纯归一化** [L16-L26](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/layernorm.py#L16-L26)：

```python
x = x.float()                                   # 提到 fp32 算, 数值更稳
var = x.pow(2).mean(dim=-1, keepdim=True)
x.mul_(torch.rsqrt(var + self.eps))             # in-place: x *= 1/sqrt(var+eps)
x = x.to(orig_dtype).mul_(self.weight)          # 回到原精度, 再乘缩放
```

**融合残差相加 + 归一化** [L28-L40](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/layernorm.py#L28-L40)：

```python
x = x.float().add_(residual.float())            # 关键: 先把残差加进来
residual = x.to(orig_dtype)                      # 把"加完残差、未归一化"的值存回 residual
var = x.pow(2).mean(dim=-1, keepdim=True)
x.mul_(torch.rsqrt(var + self.eps))
x = x.to(orig_dtype).mul_(self.weight)
return x, residual                               # 返回归一化结果 + 更新后的残差
```

两个细节决定了整条链路的正确性：

1. **`add_rms_forward` 同时返回更新后的 `residual`**。这个 `residual = x + 残差`（未归一化）会被传到下一个 norm 继续累加。于是残差通道一路「滚雪球」：\(r \leftarrow r + \text{SubLayer}_i(\dots)\)。
2. **`mlp` 输出在本层不加残差**。看 [L158-L159](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L158-L159)：`hidden_states = self.mlp(hidden_states)` 后直接 `return`，`residual` 是 `attn 输出 + 旧残差`。这个「mlp 输出 + residual」的加法被推迟到**下一层的 `input_layernorm`**；最后一层没有下一层，于是由 `Qwen3Model.forward` 末尾的 `self.norm(hidden_states, residual)` 收口（见 4.1.3）。

> 数值上全程在 fp32 做加法与归一化（`x.float()`），最后回到模型精度（`orig_dtype`，通常是 bf16），这与 HF 实现一致，保证低精度训练/推理下的稳定性。

#### 4.2.4 代码实践

**实践目标**：手工模拟两个 layer 的残差通道，验证「延迟相加」的正确性。

**操作步骤**（纯纸笔 / 可选 Python，无需 GPU）：

1. 设输入嵌入 \(e\)。第一层 `residual=None`，则：
   - `hidden1_in = Norm(e)`，`residual = e`。
   - `attn1 = Attn(hidden1_in)`。
   - 经 `post_attention_layernorm(attn1, e)` 得 `hidden1_mid = Norm(attn1 + e)`，`residual = attn1 + e`。
   - `mlp1 = MLP(hidden1_mid)`，返回 `(mlp1, attn1 + e)`。
2. 第二层 `input_layernorm(mlp1, attn1+e)` 得 `hidden2_in = Norm(mlp1 + attn1 + e)`，`residual = mlp1 + attn1 + e`。
3. 把上述展开式与标准 Pre-Norm 公式 \(e + \text{Attn}(\dots) + \text{MLP}(\dots)\) 对照。

**预期结果**：你会看到残差 `residual` 在每经过一次 `add_rms_forward` 后就累加一个子模块的贡献，与标准公式完全吻合；最后一层的 MLP 贡献则由 `Qwen3Model` 末尾的 `self.norm` 补上。这解释了为什么 layer 内部「故意」不给 mlp 加残差。

#### 4.2.5 小练习与答案

**练习 1**：为什么第一层要走 `residual is None` 的特殊分支，而不能直接用 `add_rms_forward`？

**参考答案**：第一层的输入是嵌入 \(e\)，在此之前没有任何子模块输出可加。若强行调 `add_rms_forward(e, None)` 会出错。特殊分支把 `residual` 初始化为 \(e\) 本身，于是从第二层起残差通道里就有了「累加和」可以继续滚。

**练习 2**：如果把 `Qwen3Model.forward` 末尾的 `self.norm(hidden_states, residual)` 改成 `self.norm(hidden_states)`（不传 residual），会发生什么？

**参考答案**：最后一层 MLP 的输出会被归一化，但**不会被加回残差**，相当于丢掉了最后一个残差连接。模型的数值输出会整体错误。这正是为什么末尾 norm 必须带 `residual` 走 `add_rms_forward`。

---

### 4.3 Qwen3Attention：QK-Norm、RoPE 与注意力

#### 4.3.1 概念说明

`Qwen3Attention` 是本讲信息量最大的一节。它的前向可以拆成 5 步：

1. **q/k/v 投影**：`qkv_proj` 一次性算出 q、k、v（三段拼接在一个张量里，再 `split`）。
2. **QK-Norm**：对 q、k 在 `head_dim` 维度各做一次 RMSNorm（Qwen3 特色）。
3. **RoPE**：按 `positions` 给 q、k 注入位置编码。
4. **注意力**：交给 [u4-l2](./u4-l2-attention-triton-kernel.md) 讲过的 `Attention` 层（含 paged KV cache 读写）。
5. **输出投影**：`o_proj` 把多头结果投回 `hidden_size`。

它**不含任何可训练的「注意力分数」参数**（没有 \(W_q/W_k/W_v\) 之外的东西），缩放因子 `scaling = head_dim ** -0.5` 是个常数。

#### 4.3.2 核心流程

```text
forward(positions, hidden):                       # hidden: (N, hidden_size)
    qkv = qkv_proj(hidden)                         # (N, q_size + 2*kv_size)
    q, k, v = qkv.split([q_size, kv_size, kv_size], dim=-1)
    q = q.view(-1, num_heads, head_dim)            # (N, num_heads, head_dim)
    k = k.view(-1, num_kv_heads, head_dim)         # (N, num_kv_heads, head_dim)
    v = v.view(-1, num_kv_heads, head_dim)
    if not qkv_bias:                               # Qwen3: attention_bias=False, 走这里
        q = q_norm(q)                              # 在 head_dim 上归一化
        k = k_norm(k)
    q, k = rotary_emb(positions, q, k)             # RoPE
    o = attn(q, k, v)                              # (N, num_heads, head_dim)
    return o_proj(o.flatten(1, -1))                # (N, hidden_size)
```

这里的 GQA（Grouped-Query Attention）值得点一下：`num_kv_heads < num_heads`（Qwen3-0.6B 是 8 个 kv 头对 16 个 query 头），每 2 个 query 头共享 1 组 kv。`Attention` 层与 flash-attn 内部处理这种共享，本层只需把 q/k/v 按各自头数 reshape 好即可。

#### 4.3.3 源码精读

构造与头数切分见 [nanovllm/models/qwen3.py:L14-L70](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L14-L70)，几个要点：

```python
self.num_heads = self.total_num_heads // tp_size          # 每个 rank 持有的 query 头
self.num_kv_heads = self.total_num_kv_heads // tp_size
self.head_dim = head_dim or hidden_size // self.total_num_heads   # Qwen3: 显式 128
self.q_size = self.num_heads * self.head_dim
self.kv_size = self.num_kv_heads * self.head_dim
self.scaling = self.head_dim ** -0.5
```

- **`head_dim` 显式优先**：`head_dim or ...` 表示配置里给了 `head_dim` 就用它。Qwen3-0.6B 给的是 128，于是 `q_size = 16*128 = 2048`，比 `hidden_size=1024` 还大——这正是 4.2 节提到的「显式 head_dim」带来的宽度变化。
- **`qkv_proj` 与 `o_proj`**（[L42-L53](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L42-L53)）：`qkv_proj` 是列并行（把 q/k/v 投影按输出维切分到各 rank），`o_proj` 是行并行（输入维切分、输出全量，配 all-reduce）。切分机制留到 [u4-l5](./u4-l5-parallel-linear-tp.md)。
- **QK-Norm 的开关**（[L68-L70](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L68-L70)）：

```python
if not self.qkv_bias:
    self.q_norm = RMSNorm(self.head_dim, eps=rms_norm_eps)
    self.k_norm = RMSNorm(self.head_dim, eps=rms_norm_eps)
```

  注意归一化宽度是 `head_dim`（不是 `hidden_size`），且作用在 reshape 成 `(N, num_heads, head_dim)` 之后的最后一维——也就是「每个头内部」归一化。开关条件 `if not self.qkv_bias` 把 QK-Norm 与「不带 qkv bias」绑定：Qwen3 的 `attention_bias=False`，所以会创建这两个 norm；带 bias 的变体则不创建，前向里 `if not self.qkv_bias` 同样会跳过归一化，两边自洽。

前向见 [L72-L88](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L72-L88)。RoPE 部分（[L85](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L85)）调用 `self.rotary_emb(positions, q, k)`，实现在 [nanovllm/layers/rotary_embedding.py:L37-L48](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/rotary_embedding.py#L37-L48)：

```python
@torch.compile
def forward(self, positions, query, key):
    cos_sin = self.cos_sin_cache[positions]       # 按 positions 查表
    cos, sin = cos_sin.chunk(2, dim=-1)
    query = apply_rotary_emb(query, cos, sin)
    key = apply_rotary_emb(key, cos, sin)
    return query, key
```

其中 cos/sin 缓存在构造时一次性算好，见 [L19-L35](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/rotary_embedding.py#L19-L35)：

```python
inv_freq = 1.0 / (base ** (torch.arange(0, rotary_dim, 2) / rotary_dim))   # 即 θ_i
t = torch.arange(max_position_embeddings)
freqs = torch.einsum("i,j -> ij", t, inv_freq)                              # 即 f_{m,i} = m·θ_i
cache = torch.cat((freqs.cos(), freqs.sin()), dim=-1).unsqueeze_(1)
```

「rotate-half」公式对应 [L6-L14](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/rotary_embedding.py#L6-L14)：

```python
x1, x2 = torch.chunk(x.float(), 2, dim=-1)     # 把 head_dim 拆成前后两半
y1 = x1 * cos - x2 * sin
y2 = x2 * cos + x1 * sin
return torch.cat((y1, y2), dim=-1).to(x.dtype)
```

这正是 2.3 节给出的公式：前半段减去旋转项，后半段加上旋转项。`x.float()` 同样是为了在 fp32 做三角函数运算。

> 一个工程细节：`get_rope` 用了 `@lru_cache(1)`（[L51-L59](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/rotary_embedding.py#L51-L59)），意味着同一组 `(head_size, rotary_dim, max_position, base)` 参数会**复用同一个 `RotaryEmbedding` 实例**。由于 28 层 attention 的 RoPE 参数完全相同，整个模型实际只创建了一份 cos/sin 缓存，省显存。

注意力本体 `self.attn` 不在本层展开——它消费 `Context` 里的 `slot_mapping` 写 K/V、按 `is_prefill` 选 varlen 或 paged 路径，详见 [u4-l2](./u4-l2-attention-triton-kernel.md)。本层只负责「把 q/k/v 准备好喂给它」。

#### 4.3.4 代码实践

**实践目标**：核对 QK-Norm 的作用维度与 RoPE 的形状保持特性。

**操作步骤**（源码阅读 + 可选本地验证）：

1. 在 [qwen3.py:L82-L85](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L82-L85) 处阅读 `q_norm`/`k_norm`/`rotary_emb` 三步，标注张量形状。
2. （可选，需 GPU）在 `q = self.q_norm(q)` 前后各插一行 `print(q.shape, q.float().mean(dim=-1).std().item())`，观察归一化前后 `head_dim` 维的分布变化。

**需要观察的现象 / 预期结果**：

- `q_norm` 输入输出形状都是 `(N, num_heads, head_dim)`——**RoPE 与 QK-Norm 都不改变形状**。
- 归一化后，`head_dim` 维的「均方根」应接近 1（乘以 `weight` 前），因为 RMSNorm 把方差归一到 1。

> 若本地无环境：直接从代码推导——`RMSNorm(self.head_dim)` 在最后一维归一化，而 `q` 的最后一维正是 `head_dim`，故每个 (token, head) 独立归一化。

#### 4.3.5 小练习与答案

**练习 1**：QK-Norm 为什么作用在 `head_dim` 维而不是 `hidden_size` 维？

**参考答案**：因为 q、k 已被 reshape 成 `(N, num_heads, head_dim)`，每个头是独立的「方向」。在 `head_dim` 上归一化等价于「对每个头的每个 token 独立做 RMSNorm」，目的是控制每个头的尺度，防止某些头的 q/k 范数过大导致注意力 softmax 饱和。若作用在 `hidden_size` 会混淆不同头的信息，失去意义。

**练习 2**：`scaling = head_dim ** -0.5` 用在哪？为什么需要它？

**参考答案**：它作为 `softmax_scale` 传给 flash-attn（见 `Attention.forward` 的 `softmax_scale=self.scale`）。注意力分数 \(q\cdot k\) 的量级会随 `head_dim` 增大而增大，若不缩放会使 softmax 进入饱和区、梯度消失。乘以 \(1/\sqrt{d}\) 把分数方差控制在 1 附近，与原始 Transformer 的 \(\frac{1}{\sqrt{d_k}}\) 同理。

---

### 4.4 Qwen3MLP：门控前馈网络

#### 4.4.1 概念说明

`Qwen3MLP` 是 decoder layer 里的前馈子模块，结构是经典的 **SwiGLU 门控**：

\[
\text{output} = \big(\text{SiLU}(W_{\text{gate}}\,x)\odot (W_{\text{up}}\,x)\big)\,W_{\text{down}}
\]

其中 \(\text{SiLU}(x)=x\,\sigma(x)\)。直觉上：`gate_proj` 决定「哪些通道该通过」，`up_proj` 提供「通过的内容」，两者逐元素相乘后再用 `down_proj` 投回 `hidden_size`。这种门控比普通的两层 MLP 表达力更强。

#### 4.4.2 核心流程

```text
forward(x):                                # x: (N, hidden_size)
    gate_up = gate_up_proj(x)               # (N, 2*intermediate_size), gate 与 up 拼接
    x = act_fn(gate_up)                     # 拿后半乘前半的 SiLU
    x = down_proj(x)                        # (N, hidden_size)
    return x
```

`gate_up_proj` 是把 `gate_proj` 和 `up_proj` 合并成一个 `MergedColumnParallelLinear`（输出维翻倍），再在激活里 `chunk(2)` 拆开。合并是为了减少一次矩阵乘的 kernel launch 开销。

#### 4.4.3 源码精读

构造见 [nanovllm/models/qwen3.py:L91-L111](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L91-L111)，前向见 [L113-L117](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L113-L117)：

```python
self.gate_up_proj = MergedColumnParallelLinear(
    hidden_size,
    [intermediate_size] * 2,          # 两段同样大小: gate 和 up
    bias=False,
)
self.down_proj = RowParallelLinear(intermediate_size, hidden_size, bias=False)
assert hidden_act == "silu"
self.act_fn = SiluAndMul()
```

激活 `SiluAndMul` 见 [nanovllm/layers/activation.py:L6-L11](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/layers/activation.py#L6-L11)：

```python
@torch.compile
def forward(self, x):
    x, y = x.chunk(2, -1)          # 前半 = gate, 后半 = up
    return F.silu(x) * y
```

对照公式：`x`（前半）对应 \(W_{\text{gate}}\,x\) 经 SiLU，`y`（后半）对应 \(W_{\text{up}}\,x\)，二者逐元素相乘。`@torch.compile` 把「chunk + silu + 乘」融合成单个 kernel，省掉中间张量的显存读写（详见 [u5-l2](./u5-l2-torch-compile-fusion.md)）。

> 形状上（tp_size=1）：输入 `(N, 1024)` → `gate_up` `(N, 2*3072=6144)` → 激活后 `(N, 3072)` → `down_proj` `(N, 1024)`。注意激活把宽度从 `2*intermediate` 砍回 `intermediate`，因为门控相乘把两段合一。

#### 4.4.4 代码实践

**实践目标**：验证 `SiluAndMul` 的语义。

**操作步骤**（纯 CPU / 无需模型，可直接跑）：

```python
# 示例代码: 独立验证 SiluAndMul 的行为, 与项目运行环境无关
import torch, torch.nn.functional as F
gate_up = torch.tensor([[1.0, 2.0, 3.0, 4.0]])   # 1 行 4 列, 视作 gate=[1,2] up=[3,4]
gate, up = gate_up.chunk(2, -1)
out = F.silu(gate) * up
# F.silu(1)=0.7311, F.silu(2)=1.7616
print(out)   # 预期 ≈ [[0.7311*3, 1.7616*4]] = [[2.1932, 7.0464]]
```

**需要观察的现象 / 预期结果**：输出第一列约 2.19，第二列约 7.05，与手算一致。这说明 `gate` 通道的值经 SiLU 后被「门控」缩放（负值会被压到接近 0），再乘以 `up` 通道。

**待本地验证**：上述数值可在任意带 PyTorch 的 CPU 环境运行核对。

#### 4.4.5 小练习与答案

**练习 1**：为什么把 `gate_proj` 和 `up_proj` 合并成一个 `gate_up_proj`，而不是分开两个线性层？

**参考答案**：合并后只需启动一次矩阵乘 kernel（而不是两次），减少 kernel launch 开销；同时权重在显存里连续存放，访存更友好。代价只是激活前多一次 `chunk`，非常划算。这也是 `packed_modules_mapping` 里要把 HF 的 `gate_proj/up_proj` 映射到 `gate_up_proj` 的原因。

**练习 2**：SiLU 与 ReLU 作为门控激活的主要区别是什么？

**参考答案**：ReLU 在负数处硬性截断为 0，梯度在负区间恒为 0；SiLU（\(x\sigma(x)\)）在 0 附近平滑、对小的负值保留少量信息且梯度处处非零，训练更平稳。门控网络尤其受益于这种平滑性。

## 5. 综合实践：跟踪一个 token 的完整前向

把本讲四个模块串起来。**任务**：以 Qwen3-0.6B、`tensor_parallel_size=1`、对**单条长度为 \(L\) 的 prompt 做 prefill** 为场景，跟踪这 \(L\) 个 token 从输入到 `hidden_states` 输出的完整路径，标注每一步张量形状。

约定记号：\(L\) = prompt 长度，\(H=1024\)，\(n_q=16\)，\(n_{kv}=8\)，\(d=128\)，\(I=3072\)，\(V=151936\)。

**操作步骤**：

1. 阅读 [qwen3.py:L173-L183](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L173-L183)（`Qwen3Model.forward`）→ [L146-L159](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L146-L159)（`DecoderLayer.forward`）→ [L72-L88](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L72-L88)（`Attention`）→ [L113-L117](https://github.com/GeeeekExplorer/nano-vllm/blob/bb823b3e06983d71485a8e1f23715ebd87d98ef8/nanovllm/models/qwen3.py#L113-L117)（`MLP`）。
2. 在一张表里写出每一步形状。下面是**参考答案**（待本地验证）：

| 阶段 | 表达式 | 形状 |
| --- | --- | --- |
| 输入 `input_ids` | — | \((L,)\) |
| `embed_tokens(input_ids)` | 查表 | \((L, H)=(L, 1024)\) |
| 进入第 1 层 `input_layernorm`（首层分支） | Norm | \((L, 1024)\) |
| `qkv_proj(hidden)` | 列并行投影 | \((L,\; q\_size+2\,kv\_size)=(L, 2048+1024+1024)=(L,4096)\) |
| `split` 后 q / k / v | 切三段 | q: \((L,2048)\)；k,v: \((L,1024)\) |
| reshape q / k / v | 按头拆 | q: \((L,16,128)\)；k,v: \((L,8,128)\) |
| `q_norm(q)` / `k_norm(k)` | head_dim 上 RMSNorm | \((L,16,128)\) / \((L,8,128)\) |
| `rotary_emb(positions, q, k)` | RoPE | 同上 |
| `attn(q,k,v)` | 注意力（含 KV cache 写入） | \((L,16,128)\) |
| `o.flatten(1,-1)` | 多头拼接 | \((L,2048)\) |
| `o_proj(...)` | 行并行投影 | \((L,1024)\) |
| `post_attention_layernorm(attn_out, residual)` | 融合 add+norm | \((L,1024)\) |
| `gate_up_proj(x)` | 合并门控投影 | \((L, 2I)=(L,6144)\) |
| `act_fn(...)` | SiluAndMul | \((L, I)=(L,3072)\) |
| `down_proj(...)` | 投回 hidden | \((L,1024)\) |
| 下一层 `input_layernorm(mlp_out, residual)` | 融合 add+norm | \((L,1024)\) |
| ……循环 28 层…… | | |
| 末层后 `self.norm(hidden, residual)` | 最终融合 add+norm | \((L,1024)\) |
| `compute_logits` → `lm_head` | \(W^\top h\) | \((L, V)=(L,151936)\) |

3. （可选，需 GPU）在 `Qwen3DecoderLayer.forward` 入口与 `self.mlp` 之后各打印一次 `hidden_states.shape` 与 `residual.shape`，跑一条短 prompt，核对上表。

**需要观察的现象 / 预期结果**：所有「中间表示」的宽度都在 `hidden_size=1024` 上流转，只有 q/k/v 投影与 MLP 中间层会临时变宽；残差 `residual` 始终是 `(L, 1024)` 且每个子模块的贡献都被累加进去；最终 `compute_logits` 把宽度撑到词表大小 `V`。

**待本地验证**：表中具体数值依赖 Qwen3-0.6B 的 `config.json`，请下载权重后对照确认；形状符号关系（哪个维度是 \(n_q\)、哪个是 \(d\)）则可直接从源码推出，与具体配置无关。

## 6. 本讲小结

- **三层嵌套**：`Qwen3ForCausalLM`（外壳 + LM 头）包裹 `Qwen3Model`（嵌入 + 28 层 decoder + 最终 norm），`Qwen3Model` 再循环调用 `Qwen3DecoderLayer`。
- **残差被融合进归一化**：`RMSNorm` 有两个 `@torch.compile` 分支，`add_rms_forward` 同时做「加残差」与「归一化」；子模块（attention/mlp）自己不碰残差，每层 MLP 的残差相加被推迟到下一个 norm，最后一层由 `Qwen3Model` 末尾的 `self.norm(hidden, residual)` 收口。
- **Qwen3 attention = qkv 投影 + QK-Norm + RoPE + 注意力 + 输出投影**；QK-Norm 在 `head_dim` 维对每个头独立归一化，且仅在 `qkv_bias=False`（即 Qwen3 默认）时启用。
- **显式 `head_dim=128`** 使 q/k/v 投影输出比 `hidden_size` 更宽（`q_size=2048`），这是 Qwen3 区别于「head_dim=hidden/heads」模型的关键。
- **RoPE** 用「rotate-half」公式注入位置，cos/sin 缓存预计算并通过 `lru_cache` 全模型共享一份。
- **MLP 是 SwiGLU 门控**：合并的 `gate_up_proj` 经 `SiluAndMul`（\(\text{SiLU}(\text{gate})\odot\text{up}\)）再 `down_proj` 投回；`tie_word_embeddings=True` 时 `lm_head` 与 `embed_tokens` 共享同一块权重。

## 7. 下一步学习建议

本讲只讲了「模型结构」与「数值前向」，刻意回避了两块更深的机制：

1. **并行线性层如何切分权重**：`qkv_proj`/`o_proj`/`gate_up_proj`/`down_proj` 在 `tensor_parallel_size>1` 时到底怎么切、各 rank 持有哪些行/列——这正是 [u4-l5 张量并行线性层与权重分片](./u4-l5-parallel-linear-tp.md) 的主题。
2. **这些层的权重是怎么从 safetensors 装进来的**：`packed_modules_mapping` 把 HF 分开的 `q_proj/k_proj/v_proj` 合进 `qkv_proj` 的映射细节，见 [u5-l4 safetensors 权重加载](./u5-l4-weight-loading.md)。

此外，本讲遇到的若干 `@torch.compile`（RMSNorm、RoPE、SiluAndMul）会在 [u5-l2 torch.compile 算子融合](./u5-l2-torch-compile-fusion.md) 统一讲它们的融合收益。建议下一讲先读 u4-l5，把「模型结构 + 并行切分」配齐，再进入 u5 的优化主题。
