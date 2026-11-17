# 核心组件：前馈网络、LayerNorm、RoPE 与 LM Head

> 承接 [u3-l2](u3-l2-transformer-config-and-presets.md)：上一讲我们把 `TransformerConfig` 当作「图纸」整体看过了一遍，其中 `block.feed_forward`、`block.layer_norm`、`block.attention.rope`、`lm_head` 这些字段当时一笔带过。本讲就把这些「积木」逐个拆开，看它们内部的实现与配置项。

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 SwiGLU/GeGLU 门控前馈的公式、三个权重矩阵的分工，以及如何用一个字段在两者之间切换。
2. 区分 LayerNorm 与 RMSNorm 的数学差异，理解 `weight_shape`、`output_scale` 这两个不常见但很关键的设计。
3. 解释 RoPE 的逆频率表如何计算、缓存如何命中，以及 PI / Stepwise / YaRN 等上下文扩展策略各自改动了频谱的哪一段。
4. 弄清 `LMHead` 从隐状态到 loss 的完整路径，以及 `tie_word_embeddings=True` 权重共享的实现方式与参数量影响。
5. 能为 `FeedForward` 写一个自定义变体，并让它被 `TransformerConfig` 选中、正确前向。

## 2. 前置知识

本讲假设你已学完单元一、二和 [u3-l1](u3-l1-transformer-architecture.md)、[u3-l2](u3-l2-transformer-config-and-presets.md)。在此之上，补充几个通俗概念：

- **门控前馈（gated FFN）**：经典 Transformer 的前馈是 `W2(act(W1x))`，两层矩阵一个激活。「门控」版本多加一条支路：一条支路算出「内容」，另一条支路算出「开关」，两者逐元素相乘。内容乘以开关，等于让网络自己学会「这部分信息要不要传下去」，这就是 GLU（Gated Linear Unit）家族。用 SiLU 做开关叫 SwiGLU，用 GELU 叫 GeGLU。
- **归一化层**：把每个位置的向量调整到稳定的数值范围。LayerNorm 先减均值再除标准差；RMSNorm 省去减均值，只除均方根，少一次统计、更快，是现代 LLM 的主流（OLMo 2/3 都用它）。
- **旋转位置编码（RoPE）**：不额外占参数，而是把每个注意力头里的向量两两配对，按 token 位置 \(m\) 旋转一个角度。两个 token 的 Q·K 内积只依赖相对位置 \(m_i - m_j\)，因此天然编码相对距离。
- **权重共享（weight tying）**：输入 embedding 表（词表 → 向量）和输出 lm_head 矩阵（向量 → 词表）形状相同，可以共用同一份参数，省下 `d_model × vocab_size` 个参数。
- **logits 与交叉熵**：lm_head 输出的未归一化分数叫 logits；对 logits 做 softmax 后与目标 token 求交叉熵，就是语言模型的 loss。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/olmo_core/nn/feed_forward.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/feed_forward.py) | 门控前馈 `FeedForward`（SwiGLU/GeGLU）与 nGPT 变体 `NormalizedFeedForward` |
| [src/olmo_core/nn/layer_norm.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/layer_norm.py) | 归一化层家族：`LayerNorm`、`RMSNorm`、各内核/兼容变体、`L2Norm`，含新增的 `output_scale` |
| [src/olmo_core/nn/rope.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/rope.py) | 旋转位置编码 `RotaryEmbedding` 家族与 RoPE 缩放（ABF/PI/Stepwise/YaRN）配置 |
| [src/olmo_core/nn/lm_head.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/lm_head.py) | 语言模型头 `LMHead`：投影、交叉熵、z-loss、fused loss、TP 接入 |
| [src/olmo_core/nn/embedding.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/embedding.py) | `SplitVocabEmbedding`：词表按「基础 + 扩展」拆成两个参数的多模态 embedding 表 |
| [src/olmo_core/nn/transformer/block.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/block.py) | 组件接线处：block 构造时 build 各组件（u3-l1 已讲，本讲只引用） |
| [src/olmo_core/nn/transformer/model.py](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/model.py) | 顶层接线：embedding、lm_head 的 build 与权重共享 `_tie_weights` |
| [src/test/nn/](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/nn) | `feed_forward_test.py`、`layer_norm_test.py`、`rope_test.py`、`lm_head_test.py` 等镜像测试 |

## 4. 核心概念与源码讲解

### 4.1 FeedForward：门控前馈（SwiGLU / GeGLU）

#### 4.1.1 概念说明

前馈层（FFN）占 Transformer 参数量的大头（通常约 2/3），是每个 block 里「逐位置独立计算」的部分——它不看其他 token，只对当前位置的向量做一次「放大 → 非线性 → 缩回」。OLMo-core 只提供**门控**实现：SwiGLU（OLMo 2/3 默认）与 GeGLU，公式为

\[
\mathrm{FFN}(x) = W_2\big(\sigma(W_1 x) \odot W_3 x\big)
\]

其中 \(\sigma\) 是激活函数（SiLU 或 GELU-tanh），\(\odot\) 是逐元素乘，\(W_1, W_3: d_{model} \to hidden\)，\(W_2: hidden \to d_{model}\)。\(W_1x\) 是「内容」，\(W_3x\) 是「门」，两者相乘后由 \(W_2\) 投回原维度。

#### 4.1.2 核心流程

- 构造：三个 `nn.Linear`，形状分别为 `(d_model→hidden)`、`(hidden→d_model)`、`(d_model→hidden)`。
- 前向：`w2( act(w1(x)) * w3(x) )`，输入输出形状都是 `(*, d_model)`。
- 参数量：\(3 \cdot d_{model} \cdot hidden\)，加 bias 再加 \(2 \cdot hidden + d_{model}\)。注意门控 FFN 比「同参数量」的经典 FFN 窄，所以 OLMo 系列的 `hidden_size` 通常取约 \( \tfrac{8}{3} d_{model} \) 而不是经典 \(4 d_{model}\)。
- 并行：`apply_tp` 把 w1/w3 按列切、w2 按行切，接入张量并行（本讲只认形状，细节留到单元七）。

#### 4.1.3 源码精读

**激活函数枚举**——silu 对应 SwiGLU，gelu_tanh 对应 GeGLU，`build()` 把枚举翻译成真正的可调用函数：

- [feed_forward.py:L29-L50](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/feed_forward.py#L29-L50)：`ActivationFunction` 定义两个成员与 `build()` 分发。注意「SwiGLU 还是 GeGLU」不是两个类，只是同一个 `FeedForward` 的不同激活配置。

**Config 与参数量预算**——`hidden_size` 是唯一必填字段，`name` 选实现，`activation` 选激活：

- [feed_forward.py:L69-L105](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/feed_forward.py#L69-L105)：`FeedForwardConfig`，`num_params(d_model)` 按 \(3dh\)（+bias）预算参数，normalized 变体另加两个 `hidden` 长度的缩放向量。这是 u3-l2 讲过的「`num_params` 无需实例化即可预算参数量」在组件层的实现。

**build() 分发**——注意这里的多态机制：

- [feed_forward.py:L107-L139](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/feed_forward.py#L107-L139)：`build()` 把 config 字段转成 kwargs，再按 `name` 走 if/elif 实例化 `FeedForward` 或 `NormalizedFeedForward`。**对比 u2-l2**：feed_forward 家族用的**不是** Registrable 装饰器注册表，而是「`name` 枚举 + build() 手写分发」。这意味着新增一个官方实现需要改库源码（在 `FeedForwardType` 和这里各加一支）；不改源码接入自定义变体的方式见第 5 节。

**FeedForward 本体**：

- [feed_forward.py:L147-L163](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/feed_forward.py#L147-L163)：构造函数。`activation.build()` 在构造期就把枚举变成函数，前向里不再做字符串判断。
- [feed_forward.py:L165-L171](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/feed_forward.py#L165-L171)：前向只有一行 `self.w2(self.activation_fn(self.w1(x)) * self.w3(x))`，正是上面公式。
- [feed_forward.py:L206-L209](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/feed_forward.py#L206-L209)：`num_flops_per_token` 按「每参数 6 FLOPs（前向 2 次乘加 × 前向+反向 3 份）」估算。

**nGPT 变体**（了解即可）：

- [feed_forward.py:L212-L256](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/feed_forward.py#L212-L256)：`NormalizedFeedForward` 在两个门支路上各乘一个可学习缩放 `sw1/sw3`，且每步优化后要把权重矩阵 L2 归一化（`normalize_matrices`），属于 nGPT 的「超球面」训练范式。

**接线位置**：block 构造时调用 `feed_forward.build(d_model=d_model, ...)`：

- [block.py:L139-L140](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/block.py#L139-L140)：`TransformerBlock` 中 `self.feed_forward` 与 `self.feed_forward_norm` 的构建。
- [transformer/config.py:L1646](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/config.py#L1646)：`llama2` 预设里 `FeedForwardConfig(hidden_size=..., bias=False)` 的典型用法——OLMo 系 FFN 不带 bias。

#### 4.1.4 代码实践

**实践目标**：验证「SwiGLU → GeGLU 只改一个字段」。

1. 操作步骤：先跑官方测试确认环境可用（该测试无 gpu 标记，纯 CPU 可跑）：

   ```bash
   pytest -v src/test/nn/feed_forward_test.py -k activations
   ```

   然后运行下面这段**示例代码**（可直接放进 Python 解释器）：

   ```python
   import torch
   from olmo_core.nn.feed_forward import FeedForward, ActivationFunction

   d_model, hidden = 128, 256
   torch.manual_seed(0)
   swiglu = FeedForward(d_model=d_model, hidden_size=hidden, activation=ActivationFunction.silu)
   geglu = FeedForward(d_model=d_model, hidden_size=hidden, activation=ActivationFunction.gelu_tanh)
   geglu.load_state_dict(swiglu.state_dict())  # 同权重、不同激活

   x = torch.randn(2, 8, d_model)
   print("swiglu:", swiglu(x).shape, "geglu:", geglu(x).shape)
   print("输出不同:", not torch.allclose(swiglu(x), geglu(x)))
   ```

2. 需要观察的现象：两个输出形状都是 `torch.Size([2, 8, 128])`，但数值不同。
3. 预期结果：形状一致（激活函数不改变形状）；数值不同（激活不同）。参数名完全一致（`w1/w2/w3`），所以 `load_state_dict` 能直接复用权重。
4. 该测试文件对应断言见 [feed_forward_test.py:L99-L119](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/nn/feed_forward_test.py#L99-L119)。示例代码的运行输出：待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`FeedForwardConfig(hidden_size=1024, bias=False)` 在 `d_model=512` 时参数量是多少？

答案：\(3 \times 512 \times 1024 = 1{,}572{,}864\)。依据 [feed_forward.py:L97-L99](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/feed_forward.py#L97-L99)，`bias=False` 时不加偏置项。

**练习 2**：为什么门控 FFN 的 `hidden_size` 通常取 \( \tfrac{8}{3} d_{model} \) 附近而不是 \(4 d_{model}\)？

答案：经典 FFN 参数量是 \(2dh\)，门控 FFN 是 \(3dh\)。若想保持总参数量相当，门控版的 \(h\) 应取经典版的 \(2/3\)，即 \(4d \times 2/3 = \tfrac{8}{3}d\)。库里的实际做法见 [transformer/config.py:L1733-L1737](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/config.py#L1733-L1737)：`hidden_size = int(8 * d_model / 3)` 再向上取整到 256 的倍数；`num_params` 的公式（L97）就是做这类预算的工具。

**练习 3**：`NormalizedFeedForward` 为什么禁止 `gelu_tanh` 激活？

答案：[feed_forward.py:L127-L133](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/feed_forward.py#L127-L133) 与 L227-L230 明确只允许 `silu`——nGPT 范式的数学推导基于 SiLU 门控，换激活会破坏其归一化性质，所以构造期直接抛 `OLMoConfigurationError`。

### 4.2 LayerNorm 家族：LayerNorm、RMSNorm 与 output_scale

#### 4.2.1 概念说明

归一化层负责把残差流上每个位置的向量拉回稳定尺度，是深层网络可训练的关键。LayerNorm 与 RMSNorm 的公式分别为

\[
\mathrm{LN}(x) = \frac{x - \mu}{\sqrt{\sigma^2 + \epsilon}} \odot w + b,
\qquad
\mathrm{RMSNorm}(x) = \frac{x}{\sqrt{\tfrac{1}{d}\sum_i x_i^2 + \epsilon}} \odot w + b
\]

RMSNorm 不减均值，统计量少算一项，更快且实践中效果相当。本讲还要讲两个 OLMo-core 特有的设计：

- **`weight_shape` 与 `normalized_shape` 解耦**：统计量永远在最后 `size` 维上算，但仿射增益的形状可以不同，典型用途是 QK-norm 里「每个头一套增益」。
- **`output_scale`（本轮新增）**：一个常数乘子，**折进**仿射增益里（前向变成 \((c \cdot w) x\)），而不是在输出上额外乘一次。它服务于 CompletedP 的按深度残差缩放——把分支乘数折进 norm 后，残差相加处就不用每次前向都乘 alpha。

#### 4.2.2 核心流程

- 配置层：`LayerNormConfig(name=...)` 经 `build(size, weight_shape=...)` 分发到 7 个实现之一。
- 前向（以 LayerNorm 为例）：关 autocast → 可选升 fp32 → `F.layer_norm`（或手动广播仿射）→ 转回原 dtype。
- `output_scale` 生效路径：`_affine()` 在每次前向把 `weight`/`bias` 乘上 `output_scale`，等价于改了增益但**不改动参数本身**，因此梯度也按 \(c\) 缩放。

#### 4.2.3 源码精读

**实现枚举与 Config**：

- [layer_norm.py:L26-L58](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/layer_norm.py#L26-L58)：`LayerNormType` 列出全部 7 种实现：`default`（LayerNorm）、`rms`、`qwen_rms`、`nemotron_rms`（两个 HF 兼容变体）、`cute_rms`/`fused_rms`（CuTe/triton 内核）、`l2_norm`（nGPT 用）。
- [layer_norm.py:L98-L143](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/layer_norm.py#L98-L143)：`build()` 的 if/elif 分发，与 FeedForward 相同的「name 枚举」模式；`weight_shape` 参数从这里传入。

**LayerNorm 本体**：

- [layer_norm.py:L146-L188](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/layer_norm.py#L146-L188)：docstring 详细解释了 `normalized_shape`（统计范围，恒为 `(size,)`）与 `weight_shape`（增益形状，可广播）的区别，并给出 QK-norm 的例子：输入 `(batch, seq, n_heads, head_dim)` 时取 `size=head_dim, weight_shape=(n_heads, head_dim)` 即每头独立增益。
- [layer_norm.py:L190-L231](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/layer_norm.py#L190-L231)：构造函数。末尾 `self.output_scale = 1.0` 是本轮新增的常数乘子（默认 1 表示关闭）。
- [layer_norm.py:L233-L247](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/layer_norm.py#L233-L247)：`supports_output_scale()` 只有当「有仿射增益」且「前向就是基类 LayerNorm/RMSNorm 的前向」才返回 True——内核版（triton/CuTE）和 L2Norm 不支持，因为增益折算要发生在自家前向里；`_affine()` 是实际乘 `output_scale` 的地方。
- [layer_norm.py:L257-L280](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/layer_norm.py#L257-L280)：前向。若 `weight_shape != normalized_shape`，`F.layer_norm` 无法直接吃非匹配权重，就先归一化再手动广播乘增益。

**RMSNorm 与兼容变体**：

- [layer_norm.py:L283-L310](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/layer_norm.py#L283-L310)：`RMSNorm.forward`——均方、rsqrt、乘 `_affine` 后的增益。OLMo 2/3 的 block 用的就是它（`bias=False`）。
- [layer_norm.py:L313-L358](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/layer_norm.py#L313-L358)：`QwenRMSNorm` / `NemotronRMSNorm` 与 `RMSNorm` 的区别只在**舍入顺序**（先转回输入 dtype 再乘权重 vs 方差恒在 fp32 算）——为了让本地实现与对应 HF 模型逐位对齐，便于权重互转。
- [layer_norm.py:L415-L475](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/layer_norm.py#L415-L475)：`FusedRMSNorm` 用 flash-attn 的 triton 内核，构造期即声明能力边界（只支持 affine、full precision、一维权重，否则 `NotImplementedError`）。
- [layer_norm.py:L478-L494](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/layer_norm.py#L478-L494)：`L2Norm` 无参数、只做 L2 归一化，供 nGPT 使用。

**output_scale 的消费者**（为什么需要它）：

- [ddp/block.py:L1519-L1529](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/ddp/block.py#L1519-L1529)：`_fold_into_norm`——当残差分支乘数 \(\alpha \ne 1\) 且 norm 支持时，把它折进 `norm.output_scale`，返回 1 给残差相加处。调用点在 [ddp/block.py:L597-L607](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/ddp/block.py#L597-L607)：非 pre-norm 布局下每个分支末端的 norm 正好是折叠点。CompletedP 设置的 `residual_multiplier` 最终就走这条路（机制详见 u10-l8）。

#### 4.2.4 代码实践

**实践目标**：亲手验证 `output_scale` 的语义（输出按 \(c\) 缩放、梯度按 \(c^2\) 缩放）。

1. 操作步骤（**示例代码**，对照官方测试 [layer_norm_test.py:L80-L102](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/nn/layer_norm_test.py#L80-L102) 编写）：

   ```python
   import torch
   from olmo_core.nn.layer_norm import LayerNormConfig, LayerNormType

   cfg = LayerNormConfig(name=LayerNormType.rms, bias=False)
   norm = cfg.build(size=16); scaled = cfg.build(size=16)
   scaled.load_state_dict(norm.state_dict())
   assert scaled.supports_output_scale()
   scaled.output_scale = 0.3

   x = torch.randn(2, 4, 16)
   y, y_s = norm(x), scaled(x)
   print("输出缩放:", torch.allclose(y_s, 0.3 * y))
   y.square().sum().backward(); y_s.square().sum().backward()
   print("梯度缩放:", torch.allclose(scaled.weight.grad, 0.09 * norm.weight.grad))
   ```

2. 需要观察的现象：两个 print 都应为 True。
3. 预期结果：\((c\,y)^2\) 对增益 \(w\) 的导数是 \(c^2\) 倍（\(c=0.3\) 时即 0.09 倍）——这正说明 `output_scale` 折进的是增益本身而非事后乘法（事后乘常数不改参数梯度）。
4. 运行输出：待本地验证（逻辑与官方测试逐行对应）。

#### 4.2.5 小练习与答案

**练习 1**：QK-norm 想让每个注意力头有独立的增益，`build()` 该怎么传参？

答案：`size=head_dim`（统计量按头算），`weight_shape=(n_heads, head_dim)`（增益按头独立）。见 [layer_norm.py:L164-L169](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/layer_norm.py#L164-L169) 的说明与真实调用 [attention/__init__.py:L761-L769](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/__init__.py#L761-L769)。

**练习 2**：`RMSNorm(size=16, elementwise_affine=False).supports_output_scale()` 为什么是 False？

答案：没有可学习增益就无处可折——`supports_output_scale` 要求 `weight is not None` 且前向是基类实现（[layer_norm.py:L233-L240](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/layer_norm.py#L233-L240)）。官方断言见 [layer_norm_test.py:L105-L108](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/nn/layer_norm_test.py#L105-L108)。

**练习 3**：既然 `RMSNorm` 更快，仓库为什么保留 `QwenRMSNorm`、`NemotronRMSNorm` 这种「数学上几乎等价」的变体？

答案：为了与 HuggingFace 上的对应模型**逐位对齐**：fp32/bf16 下乘法与类型转换的先后顺序不同会产生微小数值差，转换 checkpoint 或对照评测时会被放大。这两个变体精确复刻了 HF 的舍入顺序（见 L313-L358 的 docstring）。

### 4.3 RotaryEmbedding：旋转位置编码与上下文扩展

#### 4.3.1 概念说明

RoPE 不引入可学习参数。它为每个注意力头预计算一张「角度表」：把头维度 \(d\) 两两配对，第 \(i\) 对的旋转角是 \(\theta^{-2i/d}\) 的频率乘以位置 \(m\)。直观理解：

- 低维对（\(i\) 小）频率高——波长短，编码「相邻 token 的精细顺序」；
- 高维对（\(i\) 大）频率低——波长长，编码「远距离的粗略相对位置」。

上下文扩展（PI/YaRN/Stepwise）都是在这张频率表上做手脚：把低频部分的频率除以 `factor`，等价于把波长拉长，使训练时只见过 8k 位置的模型能外推到更长序列。

#### 4.3.2 核心流程

```
compute_inv_freqs:  inv_freq_i = 1 / θ^(2i/d)          # (d/2,) 频率表
                      ↓ (可选 scaling 改写频率表)
_get_rotary_embedding:
    freqs  = seq ⊗ inv_freq                             # (seq_len, d/2) 每位置每频率的角度
    pos    = cat(freqs, freqs)                          # (seq_len, d)
    pos_sin / pos_cos = pos.sin() / pos.cos()           # 缓存进 BufferCache
forward(q, k):
    取出缓存的 sin/cos（不足长度则重算）
    q ← q·cos + rotate_half(q)·sin ；k 同理              # 逐对旋转
```

旋转定义：把向量按对拆成 \(x_1, x_2\)，\(\mathrm{rotate\_half} = (-x_2, x_1)\)，于是 \((x_1 \cos\theta - x_2 \sin\theta,\ x_1 \sin\theta + x_2 \cos\theta)\) 正是二维旋转矩阵。

#### 4.3.3 源码精读

**频率表**：

- [rope.py:L48-L50](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/rope.py#L48-L50)：`compute_inv_freqs` 一行实现 \(1/\theta^{2i/d}\)，所有实现与所有缩放策略共用。

**Config**：

- [rope.py:L293-L352](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/rope.py#L293-L352)：`RoPEConfig`：`name`（default/fused/complex）、`theta`（默认 500_000，OLMo 3 用大 theta 换更长原生上下文）、`full_precision`、`scaling`（缩放配置）、`partial_rotary_factor`（只旋转前一部分维度，Qwen3.5 用 0.25）。`build(head_size, cache)` 分发。

**基类与缓存命名空间**：

- [rope.py:L367-L404](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/rope.py#L367-L404)：`RotaryEmbeddingBase` 固定两个抽象方法 `warmup_cache` / `get_buffers`；注意 L388-L390 把 `theta/partial/scaling` 编进缓存命名空间——不同 RoPE 配置共用一个 `BufferCache` 也不会互相污染。

**默认实现**：

- [rope.py:L428-L468](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/rope.py#L428-L468)：`_get_rotary_embedding`——缓存命中（长度够、设备对）直接切片返回；未命中则重算整张 `(seq_len, head_size)` 的 sin/cos 表并写缓存。scaling 在这里介入：先拿到（可能被改写的）`inv_freq` 和注意力缩放因子。
- [rope.py:L470-L484](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/rope.py#L470-L484)：`_rotate_half` 与 `_apply_rotary_pos_emb`——后者处理 `partial_rotary_factor < 1` 时「旋转前半、直通后半」的拼接。
- [rope.py:L486-L603](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/rope.py#L486-L603)：`forward(q, k, ...)`。支持三种定位方式：默认连续位置、`start_pos`（增量解码时 q 从第 `start_pos` 个位置开始）、`cu_doc_lens`（packed 输入中每个文档的位置各自从 0 开始，u3-l3 提过的 varlen 场景）、`position_ids`（分支打包等显式逐 token 位置）。互斥检查在 L518-L525。

**缩放策略家族**（都在改同一张频率表）：

| 策略 | 思路 | 代码 |
| --- | --- | --- |
| ABF | 直接换更大的 base frequency `new_theta` | [rope.py:L72-L93](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/rope.py#L72-L93) |
| PI | 所有频率统一除以 `factor`（线性插值位置） | [rope.py:L96-L129](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/rope.py#L96-L129) |
| Stepwise | 高频不动、低频全除、中频平滑过渡（Llama 3.1 方案） | [rope.py:L132-L209](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/rope.py#L132-L209) |
| YaRN | 插值/外推两套频谱按 ramp 混合 + 注意力 logit 缩放 | [rope.py:L212-L290](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/rope.py#L212-L290) |

每个缩放 config 都实现 `compute_scaled_inv_freq()`（返回新频率表 + 注意力缩放因子）与 `to_hf_config()`（导出为 HF 的 `rope_scaling` 字段），这是「训练用 OLMo-core、推理用 HF」互通的桥梁。

**其他实现**：

- [rope.py:L606-L731](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/rope.py#L606-L731)：`FusedRotaryEmbedding` 用 flash-attn 的 triton 内核直接对 `qkv` 原地旋转（注意其 sin/cos 表不 concat，形状是 `(seq_len, head_size//2)`）。
- [rope.py:L734-L892](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/rope.py#L734-L892)：`ComplexRotaryEmbedding` 把每对维度看成复数、用复乘实现旋转，不支持 scaling。

**接线位置**：

- [attention/__init__.py:L779](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/attention/__init__.py#L779)：注意力层构造时 `rope.build(self.head_dim, cache=cache)`——RoPE 是注意力（u3-l3 讲过的 SequenceMixer 插槽）的组成部分，不是独立层。

#### 4.3.4 代码实践

**实践目标**：直接观察频率表与 sin/cos 缓存的形状，把「公式 → 张量」对上号。

1. 操作步骤（**示例代码**，纯 CPU）：

   ```python
   import torch
   from olmo_core.nn.rope import RoPEConfig, compute_inv_freqs

   inv_freq = compute_inv_freqs(theta=500_000, dim=64, device=torch.device("cpu"))
   print("inv_freq:", inv_freq.shape, "首元素:", inv_freq[0].item())

   rope = RoPEConfig(theta=500_000).build(head_size=64)
   rope.warmup_cache(max_seq_len=32, device=torch.device("cpu"))
   bufs = rope.get_buffers(max_seq_len=32, device=torch.device("cpu"))
   print("pos_sin:", bufs.pos_sin.shape)
   ```

2. 需要观察的现象：`inv_freq` 形状 `(32,)`（64/2 对频率），首元素是 1.0（\(i=0\) 时 \(\theta^0=1\)）；`pos_sin` 形状 `(32, 64)`（`cat(freqs, freqs)` 把每对角度复制两份）。
3. 预期结果：形状与 L457-L460 的推导一致；再次调用 `get_buffers` 不重算（缓存命中路径 L434-L447）。
4. 运行输出：待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`theta` 从 10_000 提到 500_000，频率表怎么变？

答案：`inv_freq_i = θ^{-2i/d}`，θ 变大则除 0（i=0）外的所有频率都变小，整表波长变长——模型对远距离的「分辨率」变粗，但能覆盖更长上下文而不越出训练见过的角度范围。OLMo 3 的大 theta 即此目的。

**练习 2**：增量解码时只输入 1 个新 token 的 q，RoPE 怎么知道它在第 1000 个位置？

答案：通过 `start_pos` 参数：`seq_len_needed = start_pos + k_len`，q 从 `pos_sin[q_abs_start : q_abs_start+q_len]` 取角度（[rope.py:L540](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/rope.py#L540) 与 L592-L598）。

**练习 3**：`StepwiseRoPEScalingConfig` 里 `low_freq_proportion=0.0`、`high_freq_proportion=0.25` 意味着什么？

答案：频谱中波长最长的 0% 额外放宽（低频门槛即 `old_context_len`），波长最短的 25% 划为高频保持不变，中间带线性过渡（L159-L199）。这正是 Llama 3.1 官方参数的含义。

### 4.4 LMHead 与 Embedding：从隐状态到 logits

#### 4.4.1 概念说明

`LMHead` 是模型的「出口」：把最后一层的隐状态 \(h \in \mathbb{R}^{d_{model}}\) 投影成词表上的 logits，并在训练时直接算好交叉熵 loss。它看似只是一个 `nn.Linear`，实际上打包了五件事：

1. 可选的最终归一化（`layer_norm` 字段，OLMo 2/3 都有——模型末端 RMSNorm）。
2. logits 裁剪（`logits_to_keep` / `response_logits_only`：推理只要最后几个位置、SFT 只要 response 位置的 logits，省显存）。
3. 交叉熵 + 可选 z-loss（对 logit 平方的正则，稳定训练）。
4. 两种 loss 实现：默认 PyTorch；`fused_linear` 用 Liger-Kernel 把线性投影与 loss 融合，**不物化整张 logits 张量**（词表 10 万+ 时显存收益巨大）。
5. TP/CP 下的 loss 归约（本讲只提位置，细节在单元七）。

**Embedding 侧**值得单独认识的是 `SplitVocabEmbedding`：把词表拆成「基础 + 扩展」两块独立参数，服务 Molmo2 多模态——128 个图像特殊 token 单独一块，可单独解冻/冻结；权重共享时 lm_head 只覆盖基础词表。

#### 4.4.2 核心流程

```
forward(x, labels=None):
    h = norm(x)                         # 可选最终归一化
    h, labels = _select_hidden_for_logits(...)   # logits_to_keep / response 裁剪
    若 labels 为 None:  return w_out(h)          # 纯 logits（推理）
    否则:
        default:        logits = w_out(h) → cross_entropy_loss(logits, labels, z_loss…)
        fused_linear:   fused_linear_cross_entropy_loss(h, w_out.weight, …)  # 不物化 logits
    return LMOutputWithLoss(logits?, loss, ce_loss, z_loss)
```

权重共享（`tie_word_embeddings=True`）：模型构造后执行 `lm_head.w_out.weight = embeddings.weight`，两处指向同一张量；`num_params` 相应扣掉 \(d_{model} \times vocab\)。

#### 4.4.3 源码精读

**Config 与参数量**：

- [lm_head.py:L117-L157](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/lm_head.py#L117-L157)：`LMHeadConfig`：`name`（default/normalized）、`layer_norm`（嵌套一个 `LayerNormConfig`，4.2 学的知识直接复用）、`bias`、`loss_implementation`、`fp32_output`。`num_params(d_model, vocab_size)` 把 norm、投影矩阵、bias、normalized 缩放向量全部计入。
- [lm_head.py:L159-L189](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/lm_head.py#L159-L189)：`build()` 分发，同样捕获 `TypeError` 转成语义清晰的 `OLMoConfigurationError`。

**LMHead 本体**：

- [lm_head.py:L203-L233](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/lm_head.py#L203-L233)：构造函数。`self.norm` 由嵌套 `LayerNormConfig.build` 而来；`w_out` 是 `(d_model → vocab_size)` 的线性层。
- [lm_head.py:L45-L81](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/lm_head.py#L45-L81)：`_select_hidden_for_logits`——`logits_to_keep`（int 取最后 N 个位置；tensor 取任意指定位置）与 `response_logits_only`（按 mask 挑出 response 行并压平）的裁剪逻辑，二者互斥。
- [lm_head.py:L295-L349](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/lm_head.py#L295-L349)：`forward` 主干：norm → 裁剪 → 两种 loss 实现。注意 default 分支也用 `get_local_tensor(...)` 包裹输入——这是为了 TP 下拿到本地分片，单元七再展开。
- [lm_head.py:L192-L200](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/lm_head.py#L192-L200)：返回类型 `LMOutputWithLoss`：`loss`（要优化的）、`ce_loss`/`z_loss`（仅日志）、`logits`（可置 None 省显存）。
- [lm_head.py:L381-L426](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/lm_head.py#L381-L426)：`_finalize_loss`——按 `loss_reduction` 整形，并在 TP/CP 开启时用 DTensor 完成跨 rank 归约与 `loss_div_factor` 修正。

**normalized 变体**（了解即可）：

- [lm_head.py:L492-L527](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/lm_head.py#L492-L527)：`NormalizedLMHead` 给每个 logit 乘可学习缩放 `sz`（nGPT 风格），每步之后同样要归一化权重矩阵。

**SplitVocabEmbedding**：

- [embedding.py:L12-L78](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/embedding.py#L12-L78)：两个参数 `weight`（基础词表）与 `extra_weight`（扩展 token），forward 时 `F.embedding(input, torch.cat([weight, extra_weight]))`。docstring 给出两个动机：独立冻结（`requires_grad_(False)` 只冻基础块）、基础宽度共享头。

**权重共享**：

- [transformer/config.py:L362](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/config.py#L362)：`TransformerConfig.tie_word_embeddings: bool = False`——OLMo 2/3 的预设默认**不共享**（大模型共享收益小且限制多）。
- [transformer/model.py:L181-L187](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/model.py#L181-L187)：模型构造末尾按开关调用 `_tie_weights()`。
- [transformer/model.py:L215-L222](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/model.py#L215-L222)：`_tie_weights` 直接把 `lm_head.w_out.weight` 指向 `embeddings.weight`；**若 lm_head 带 bias 则拒绝**（共享了矩阵也共享不了偏置）。
- [transformer/config.py:L535-L537](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/config.py#L535-L537)：`num_params` 在共享时扣掉一份 \(d_{model} \times vocab\)，预算与实际严格一致。

#### 4.4.4 代码实践

**实践目标**：验证权重共享对参数量的影响，以及共享后两处权重确实是同一个张量。

1. 操作步骤（**示例代码**，纯 CPU）：

   ```python
   from olmo_core.nn.transformer.config import TransformerConfig

   base = TransformerConfig.olmo2_14M(vocab_size=1000)
   untied_params = base.num_params
   base.tie_word_embeddings = True
   tied_params = base.num_params
   print("节省参数:", untied_params - tied_params, "= d_model * vocab:",
         base.d_model * base.vocab_size)

   model = base.build()
   print("共享生效:", model.lm_head.w_out.weight is model.embeddings.weight)
   ```

2. 需要观察的现象：差额恰好等于 `d_model * vocab_size`；`is` 判断为 True（同一对象，不是拷贝）。
3. 预期结果：与 [config.py:L535-L537](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/config.py#L535-L537) 的扣减逻辑和 [model.py:L215-L222](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/model.py#L215-L222) 的赋值一致。注意：若 lm_head 带 bias，`build()` 会抛 `OLMoConfigurationError`——olmo2 预设的 `LMHeadConfig(bias=False)`（[config.py:L1680](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/config.py#L1680)）天然满足前提。
4. 运行输出：待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`logits_to_keep=5` 与 `response_logits_only=True` 分别适合什么场景？

答案：前者适合生成——只需最后几个位置（或某个指定位置）的 logits（[lm_head.py:L279](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/lm_head.py#L279)）；后者适合 SFT/微调——prompt 部分（`labels == ignore_index`）不参与 loss，也就不需要它的 logits（L280-L282）。二者互斥（L56-L59）。

**练习 2**：`loss_implementation="fused_linear"` 为什么省显存？代价是什么？

答案：默认实现先物化 `(B*S, vocab_size)` 的 logits 张量再算交叉熵，词表大时这是显存大头；fused 版把线性投影与 loss 融合（Liger-Kernel），不物化 logits（[lm_head.py:L329-L341](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/lm_head.py#L329-L341)）。代价是拿不到 logits 张量——`return_logits=True` 会报错（L353-L356）。

**练习 3**：为什么 `SplitVocabEmbedding` 把基础块命名为 `weight`、扩展块命名为 `extra_weight`，而不是合成一个大表？

答案：命名决定了 `lm_head.w_out.weight = embeddings.weight` 这条共享链绑到谁——绑 `weight` 后共享头只覆盖基础词表，图像 token 只做输入永不作为预测目标，省去对 logits 列做 mask（[embedding.py:L21-L30](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/embedding.py#L21-L30)）；拆开还让「冻结预训练词表、只训新 token」变成一行 `requires_grad_(False)`。

## 5. 综合实践

**任务**：实现一个自定义 FeedForward 变体（ReGLU——用 ReLU 做门控），接入 `TransformerConfig` 并验证前向形状正确。同时澄清规格里「注册进 Registrable 体系」的真实机制：如 4.1.3 所述，feed_forward 家族的多态是 **`name` 枚举 + `build()` 分发**，不是 u2-l2 的装饰器注册表；官方留的零改库接入点就是「子类化 Config、覆写 `build()`」。

### 第一步：用内置字段切换 GeGLU（零代码）

```python
import torch
from olmo_core.nn.transformer.config import TransformerConfig
from olmo_core.nn.feed_forward import ActivationFunction

config = TransformerConfig.olmo2_14M(vocab_size=1000)
config.block.feed_forward.activation = ActivationFunction.gelu_tanh   # SwiGLU → GeGLU
model = config.build()
ffn = model.blocks["0"].feed_forward
print(type(ffn).__name__)                      # FeedForward（类没变）
print(ffn(x := torch.randn(2, 8, model.d_model)).shape)  # torch.Size([2, 8, d_model])
```

olmo2 系列预设产出的 `config.block` 是单一 `TransformerBlockConfig`（[transformer/config.py:L349](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/config.py#L349)），其 `feed_forward` 字段（L183）可直接改。

### 第二步：自定义 ReGLU 变体（示例代码）

```python
from dataclasses import dataclass
import torch.nn.functional as F
from olmo_core.nn.feed_forward import FeedForward, FeedForwardConfig
from olmo_core.nn.transformer.config import TransformerConfig

class ReGLUFeedForward(FeedForward):
    """门控换 ReLU：w2( relu(w1 x) * w3 x )。继承 w1/w2/w3 与 apply_tp 等全部设施。"""
    def forward(self, x):
        return self.w2(F.relu(self.w1(x)) * self.w3(x))

@dataclass
class ReGLUFeedForwardConfig(FeedForwardConfig):
    def build(self, d_model: int, *, dtype=None, init_device: str = "cpu"):
        kwargs = self.as_dict(exclude_none=True)
        kwargs.pop("name"); kwargs.pop("activation")   # 这两项对 ReGLU 无意义
        if "dtype" in kwargs:                          # DType 枚举 → torch.dtype
            kwargs["dtype"] = kwargs["dtype"].as_pt()
        return ReGLUFeedForward(d_model=d_model, init_device=init_device, **kwargs)

config = TransformerConfig.olmo2_14M(vocab_size=1000)
config.block.feed_forward = ReGLUFeedForwardConfig(
    hidden_size=config.block.feed_forward.hidden_size, bias=False,
)
model = config.build()
ffn = model.blocks["0"].feed_forward
assert isinstance(ffn, ReGLUFeedForward)
out = ffn(torch.randn(2, 8, model.d_model))
print("前向形状:", out.shape)          # 期望 torch.Size([2, 8, d_model])
print("完整模型前向:", model(torch.randint(0, 1000, (2, 8))).loss.shape)
```

### 第三步：验证与观察

1. `isinstance` 断言通过——你的变体确实被 `TransformerConfig` 选中（block 构造走 [block.py:L139](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/transformer/block.py#L139) 的 `feed_forward.build(...)`，即你覆写的 `build`）。
2. 逐层前向形状不变：`(*, d_model)` 进、`(*, d_model)` 出。
3. `model(...)` 能算出 loss——说明权重初始化（u3-l6 会讲的 `init` 按参数名 `w1/w2/w3` 处理）与 checkpoint 命名对继承的子类同样生效。
4. 运行结果：待本地验证（以上为示例代码，逻辑依据源码推导）。

### 两个诚实的注意事项

- **序列化边界**：`ReGLUFeedForwardConfig` 落盘时 `as_config_dict()` 会写入指向你这个类的 `_CLASS_`（u2-l1 讲过的机制），`from_dict` 恢复时要求该类可被 import——定义在脚本/`__main__` 里的类无法跨进程反序列化；若要进训练管线落盘，请把类放进一个可导入的模块。
- **想成为「官方」实现怎么办**：在 `FeedForwardType` 加枚举值并在 `FeedForwardConfig.build()` 加一支分发（[feed_forward.py:L53-L66](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/olmo_core/nn/feed_forward.py#L53-L66)、L124-L135）。这是改库操作，本教程只要求「零改库接入」，即上面的子类方案。

## 6. 本讲小结

- 门控前馈只有一行核心公式 `w2(act(w1 x) * w3 x)`；SwiGLU/GeGLU 之别只是 `ActivationFunction` 一个字段，参数量预算 \(3dh\)（+bias）由 `num_params` 提供。
- feed_forward / layer_norm / rope / lm_head 四族共享「`name` 枚举 + `build()` if/elif 分发」模式——与 u2-l2 的 Registrable 注册表是两种不同的多态方案，自定义组件的零改库入口是「子类化 Config 覆写 `build()`」。
- LayerNorm 家族 7 个实现按需选择：`RMSNorm` 是 OLMo 主力，Qwen/Nemotron 变体为 HF 逐位对齐，内核版声明能力边界；`weight_shape` 解耦统计范围与增益形状（QK-norm 每头增益），新增的 `output_scale` 把常数乘子折进增益、由 CompletedP 的残差缩放消费。
- RoPE 一切围绕一张 `(seq_len, head_size)` 的 sin/cos 表：`compute_inv_freqs` 定频率、`BufferCache` 按配置命名空间缓存、scaling 家族（ABF/PI/Stepwise/YaRN）分别以不同策略改写频率表以扩展上下文，并可通过 `to_hf_config()` 与 HF 互通。
- `LMHead` 打包了最终归一化、logits 裁剪、交叉熵/z-loss、fused loss 与 TP 归约；`tie_word_embeddings` 通过直接共享张量实现，要求 lm_head 无 bias，参数量恰好省 `d_model × vocab_size`。
- `SplitVocabEmbedding` 用「基础 + 扩展」两块参数服务多模态：可独立冻结，且共享头天然只覆盖基础词表。

## 7. 下一步学习建议

- **下一讲 [u3-l5](u3-l5-mixture-of-experts.md)**：MoE——把本讲的 `FeedForward` 换成「路由 + 多专家」的结构，你会看到 `FeedForwardConfig` 在 MoE 里以 `Router`/专家 MLP 的形式再度出现。
- **回看 [u3-l3](u3-l3-attention-and-backends.md)**：本讲的 `RoPEConfig` 与 `LayerNormConfig(weight_shape=...)` 正是注意力层 QK-norm 与位置编码的配置入口，两讲对照阅读收益最大。
- **延伸阅读源码**：`src/olmo_core/nn/functional.py` 中的 `cross_entropy_loss` / `fused_linear_cross_entropy_loss`（lm_head 的 loss 底层）；`src/olmo_core/nn/buffer_cache.py`（RoPE 缓存的实现）；`src/olmo_core/nn/ddp/block.py` 的 `_fold_into_norm`（output_scale 的完整消费链，通往 u10-l8 的 CompletedP）。
- 若你完成了第 5 节的 ReGLU 实践，建议继续思考：如何给它加 dropout？如何写一个镜像 `test_feed_forward_activations` 的 pytest 测试（对照 [feed_forward_test.py:L99-L119](https://github.com/allenai/OLMo-core/blob/704625ad94a7c8bc93f9bea7a0b865b9a0c3d44e/src/test/nn/feed_forward_test.py#L99-L119) 的断言风格）？这会为 u10-l6 的测试规范实践热身。
