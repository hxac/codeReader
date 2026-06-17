# Llama 模型结构重构

本讲义分析如何将 HuggingFace Llama 模型重构为 Megakernels 友好的格式，包括权重堆叠和缓存设置。

## 最小模块 1：模型组件分解

### 概念说明

模型组件分解是将标准的 HuggingFace Llama 模型架构重新组织为适合高性能内核执行的形式。标准 HuggingFace 实现虽然功能完整，但其权重布局和计算流程并不针对 kernel 融合和内存访问模式进行优化。通过重新组织模型结构，我们能够：

1. **优化内存访问模式**：将跨层的权重堆叠在一起，提高缓存利用率
2. **简化 kernel 融合**：为 kernel 融合提供统一的数据格式
3. **支持张量并行**：内建对分布式推理的支持

### 伪代码或流程

```
# 标准模型结构转换流程
function convert_hf_to_megakernels(hf_model):
    # 1. 分解模型组件
    model_components = {
        embeddings: hf_model.model.embed_tokens,
        layers: hf_model.model.layers,
        lm_head: hf_model.lm_head
    }
    
    # 2. 重命名层以匹配 Megakernels 约定
    for layer in model_components.layers:
        layer.self_attn.input_layernorm = layer.input_layernorm
        layer.mlp.input_layernorm = layer.post_attention_layernorm
    
    # 3. 堆叠参数
    stacked_params = stack_all_parameters(model_components.layers)
    
    # 4. 设置缓存
    setup_kv_cache(model)
    
    return restructured_model
```

### 原理分析

模型重构的核心原理是**数据局部性**和**计算融合**。在标准深度学习框架中，模型参数通常按照层为单位存储，这导致：

1. **内存访问不连续**：当执行逐层计算时，需要跳跃访问不同层的权重
2. **缓存利用率低**：CPU 缓存无法有效预取即将访问的数据
3. **kernel 启动开销**：每层都需要独立的 kernel 启动

通过将所有层的相同类型参数堆叠在一起，我们实现了：
- **连续内存布局**：所有层的 QKV 投影权重存储在连续内存中
- **批量 kernel 启动**：可以一次性处理多层计算
- **更好的向量化**：连续数据便于 SIMD 指令优化

### 代码实践

Megakernels 的模型重构从 `LlamaForCausalLM.from_pretrained()` 方法开始，该方法负责从 HuggingFace 格式加载并重构模型：

```python
@classmethod
def from_pretrained(
    cls,
    model_name_or_path: str,
    extra_config: ExtraModelConfig | None = None,
    device: DeviceType | None = None,
    dtype: torch.dtype | None = None,
):
    # 加载配置
    config: LlamaConfig = LlamaConfig.from_pretrained(model_name_or_path)
    
    # 创建空模型
    with init_empty_weights(include_buffers=False):
        model = cls(config, extra_config)
    
    # 从 HuggingFace 格式加载权重
    model.load_from_safetensors(model_path)
    
    # 应用关键重构步骤
    if extra_config.interleave_rope:
        model.model.interleave_rope()  # RoPE 重排
    
    model.stack_params()    # 权重堆叠
    model.setup_caches()    # KV 缓存设置
    
    return model
```

这段代码的关键在于调用顺序：先 RoPE 重排，再权重堆叠，最后设置缓存。这种顺序确保了后续步骤能够正确操作已重构的数据结构。

### 练习题

1. 为什么要在权重堆叠之前进行 RoPE 重排？
2. 模型组件分解过程中，哪些层会被合并？
3. 如果不使用 `init_empty_weights`，会发生什么问题？

### 答案

1. **答案**：RoPE 重排会修改 Q/K 投影权重的内存布局，如果先堆叠再重排，就需要重新处理所有堆叠的权重，效率低下。先重排再堆叠可以确保堆叠的是最终需要的布局。

2. **答案**：主要合并的是 `input_layernorm`（在 HuggingFace 中称为 `input_layernorm` 和 `post_attention_layernorm`），它们被重新映射到 Megakernels 的命名约定中。

3. **答案**：不使用 `init_empty_weights` 会导致在模型创建过程中分配大量未初始化的内存，增加内存峰值使用。该上下文管理器确保在创建模型结构时不分配实际权重内存，只在后续加载时才分配。

---

## 最小模块 2：权重堆叠策略

### 概念说明

权重堆叠（Weight Stacking）是将多层相同类型的参数在新增的维度上堆叠，形成统一的张量。例如，将 32 层的 `q_proj` 权重从 32 个独立的 `[hidden_size, num_heads * head_dim]` 矩阵堆叠为一个 `[32, hidden_size, num_heads * head_dim]` 张量。

这种策略的优点包括：
- **内存连续性**：同类型权重在物理内存中连续存储
- **批量操作**：可以一次性对所有层执行相同的操作
- **kernel 融合友好**：便于 kernel 跨层操作

### 伪代码或流程

```
function stack_parameters(layers):
    # 1. 收集各层的同类型参数
    qkv_weights = []
    o_proj_weights = []
    
    for layer in layers:
        # QKV 投影权重需要特殊处理
        qkv = concat([
            layer.self_attn.q_proj.weight,
            layer.self_attn.k_proj.weight,
            layer.self_attn.v_proj.weight
        ], dim=0)
        qkv_weights.append(qkv)
        
        o_proj_weights.append(layer.self_attn.o_proj.weight)
    
    # 2. 堆叠为统一张量
    stacked_qkv = stack(qkv_weights, dim=0)
    stacked_o_proj = stack(o_proj_weights, dim=0)
    
    # 3. 更新原始权重引用
    for i, layer in enumerate(layers):
        layer.self_attn.q_proj.weight = stacked_qkv[i]
        layer.self_attn.o_proj.weight = stacked_o_proj[i]
    
    return stacked_qkv, stacked_o_proj
```

### 原理分析

权重堆叠的数学原理是**张量拼接与堆叠**。设有 \(L\) 层，每层 \(i\) 的 Q 投影权重为 \(W^{(i)}_Q \in \mathbb{R}^{d_{model} \times d_{head} \times n_{heads}}\)。

堆叠操作定义：
\[
\text{Stacked } W_Q = \text{stack}(W^{(0)}_Q, W^{(1)}_Q, \ldots, W^{(L-1)}_Q, \text{dim}=0)
\]

结果张量形状为 \(\mathbb{R}^{L \times d_{model} \times d_{head} \times n_{heads}}\)。

对于 QKV 权重的特殊处理，我们使用拼接（concat）：
\[
W^{(i)}_{QKV} = \text{concat}(W^{(i)}_Q, W^{(i)}_K, W^{(i)}_V, \text{dim}=0)
\]

这种布局的优势在于：
1. **空间局部性**：相邻层的权重在内存中相邻
2. **并行处理**：可以对所有层并行执行相同的 kernel
3. **缓存预取**：硬件预取器能更准确地预测访问模式

### 代码实践

权重堆叠的核心实现在 `stack_params()` 方法中：

```python
def stack_params(self):
    def stack_and_reassign(modules, prop: str):
        params = [getattr(m, prop) for m in modules]
        stacked = torch.stack(params, dim=0)
        for i, m in enumerate(modules):
            getattr(m, prop)[:] = stacked[i]
        return stacked

    layers: list[LlamaBlock] = self.model.layers
    
    # 堆叠 MLP 和注意力输出投影
    o_projs = [x.self_attn.o_proj for x in layers]
    stacked_o_proj = stack_and_reassign(o_projs, "weight")
    
    # 堆叠层归一化权重
    mlp_lns = [x.mlp.input_layernorm for x in layers]
    stacked_mlp_ln_weights = stack_and_reassign(mlp_lns, "weight")
    
    # QKV 投影需要特殊处理（先拼接再堆叠）
    qkv_weights = []
    for self_attn in [x.self_attn for x in layers]:
        cat_weight = torch.cat([
            self_attn.q_proj.weight,
            self_attn.k_proj.weight,
            self_attn.v_proj.weight,
        ], dim=0)
        qkv_weights.append(cat_weight)
    
    stacked_qkv_weights = torch.stack(qkv_weights, dim=0)
    
    # 重新分配堆叠后的权重
    for i, self_attn in enumerate([x.self_attn for x in layers]):
        qkv_weight = stacked_qkv_weights[i]
        q_weight, k_weight, v_weight = qkv_weight.split([
            self.config.num_attention_heads * self.config.head_dim,
            self.config.num_key_value_heads * self.config.head_dim,
            self.config.num_key_value_heads * self.config.head_dim,
        ], dim=0)
        
        self_attn.q_proj.weight[:] = q_weight
        self_attn.k_proj.weight[:] = k_weight
        self_attn.v_proj.weight[:] = v_weight
```

QKV 权重的特殊处理是因为它们需要先在输出维度上拼接（Q+K+V），然后再在层维度上堆叠。这种"先 concat 后 stack"的策略确保了 kernel 融合时能够同时访问 Q、K、V 投影。

### 练习题

1. 为什么 QKV 权重需要先 concat 再 stack，而不是直接分别 stack？
2. 如果模型有 32 层，堆叠后的 `stacked_o_proj` 的形状是什么？
3. `stack_and_reassign` 函数中为什么要用 `[:]` 赋值？

### 答案

1. **答案**：因为 QKV 融合 kernel 需要同时访问 Q、K、V 三个投影权重。如果分别 stack，它们在内存中就是分离的；先 concat 再 stack 确保了同一层的 QKV 在内存中连续，便于 kernel 融合时一次性加载。

2. **答案**：假设 `hidden_size=4096`，则堆叠后的形状为 `[32, 4096, 4096]`（第一个 32 是层数，后面的 4096x4096 是每层 o_proj 的原始形状）。

3. **答案**：使用 `[:]` 是**原地赋值**，确保原始模块的权重引用更新为堆叠张量的对应切片，而不是创建新的参数对象。这对于保持 PyTorch 参数系统的正确性至关重要。

---

## 最小模块 3：KV 缓存设置

### 概念说明

KV 缓存（Key-Value Cache）是自回归推理中的关键优化技术。在生成每个 token 时，注意力机制需要计算当前查询与所有历史 key-value 对的注意力分数。KV 缓存避免了重复计算历史 token 的 K 和 V 投影，显著提升推理速度。

Megakernels 的 KV 缓存设置有两个特点：
1. **堆叠缓存**：所有层的 KV 缓存预分配在一个大张量中
2. **共享引用**：每层的缓存实际上是堆叠缓存的视图

### 伪代码或流程

```
function setup_kv_cache(model, config):
    # 1. 预分配堆叠的 KV 缓存张量
    k_cache = zeros([
        num_layers,
        max_batch_size,
        max_sequence_length,
        num_kv_heads,
        head_dim
    ])
    v_cache = clone(k_cache)
    
    # 2. 为每层分配缓存视图
    for layer_idx in range(num_layers):
        layer = model.layers[layer_idx]
        layer.kv_cache = (
            k_cache[layer_idx],    # 该层的 K 缓存视图
            v_cache[layer_idx]     # 该层的 V 缓存视图
        )
    
    return (k_cache, v_cache)
```

### 原理分析

KV 缓存的内存布局直接影响推理性能。设模型有 \(L\) 层，最大序列长度为 \(S_{max}\)，批大小为 \(B\)，KV 头数为 \(n_{KV}\)，头维度为 \(d_h\)。

堆叠缓存的内存形状：
\[
\text{KCache}, \text{VCache} \in \mathbb{R}^{L \times B \times S_{max} \times n_{KV} \times d_h}
\]

这种布局的优势：
1. **一次性分配**：避免推理过程中的内存碎片
2. **缓存局部性**：同层的 KV 对在内存中相邻
3. **核间通信优化**：便于分布式推理中的跨设备同步

在计算时，新的 KV 对通过索引写入缓存：
\[
\text{KCache}[:, \text{position_ids}, :, :, :] = \text{new\_keys}
\]

### 代码实践

KV 缓存设置的核心实现：

```python
def setup_caches(self):
    k_cache = torch.zeros((
        self.config.num_hidden_layers,          # 层数
        self.extra_config.max_batch_size,       # 批大小
        self.extra_config.max_len_override or self.config.max_position_embeddings,  # 序列长度
        self.config.num_key_value_heads,        # KV 头数
        self.config.head_dim,                   # 头维度
    ), device=self.device, dtype=self.dtype)
    
    v_cache = k_cache.clone()
    
    # 存储堆叠缓存引用
    self.stacked_kv_cache = (k_cache, v_cache)
    
    # 为每层分配缓存视图
    for layer_idx in range(self.config.num_hidden_layers):
        layer: LlamaBlock = self.model.layers[layer_idx]
        layer.self_attn.kv_cache = (
            self.stacked_kv_cache[0][layer_idx],  # 该层的 K 缓存
            self.stacked_kv_cache[1][layer_idx]   # 该层的 V 缓存
        )
```

在推理过程中，注意力计算函数使用缓存来存储新的 KV 对：

```python
def attention(
    query_states: Tensor,
    key_states: Tensor,
    value_states: Tensor,
    kv_cache: KV_Cache,
    position_ids: Tensor,
    seq_len: int,
) -> Tensor:
    k_cache, v_cache = kv_cache
    
    # 将新的 KV 对写入缓存
    k_cache[:, position_ids] = key_states
    v_cache[:, position_ids] = value_states
    
    # 使用缓存计算注意力
    k_for_sdpa = shape_for_sdpa(k_cache[:, :seq_len])
    v_for_sdpa = shape_for_sdpa(v_cache[:, :seq_len])
    
    attn_output = F.scaled_dot_product_attention(
        q_for_sdpa, k_for_sdpa, v_for_sdpa, is_causal=False, enable_gqa=True
    )
    return attn_output
```

这种设计确保了每次前向传播时，新的 KV 对被写入预分配的缓存中，而历史 KV 对被重用。

### 练习题

1. 为什么 K 缓存和 V 缓存要分别存储，而不是合并为一个张量？
2. 如果 `max_len_override` 设置为 2048，而 `max_position_embeddings` 是 4096，实际缓存大小是多少？
3. 在注意力计算中，为什么使用 `k_cache[:, :seq_len]` 而不是 `k_cache[:, :position_ids]`？

### 答案

1. **答案**：K 和 V 在注意力计算中有不同的用途：K 用于与 Q 计算注意力分数，V 用于加权求和。它们需要分别传递给 `scaled_dot_product_attention` 函数，且数据类型和内存布局要求不同。合并会增加索引复杂度。

2. **答案**：实际缓存大小由 `max_len_override`（2048）决定，因为它优先级更高。缓存的第三个维度是 2048，表示最多缓存 2048 个 token 的 KV 对。

3. **答案**：因为 `seq_len` 是"当前序列的有效长度"，而 `position_ids` 是"本次生成 token 的位置索引"。在 decode 阶段，`position_ids` 是单个位置（如 512），而 `seq_len` 是总序列长度（513）。注意力需要访问所有历史的 513 个位置，所以用 `:seq_len`。

---

## 最小模块 4：RoPE 重排

### 概念说明

旋转位置编码（Rotary Position Embedding, RoPE）是 Llama 等现代大模型使用的位置编码方式。标准 RoPE 假设头维度内部是连续排列的（dim0, dim1, dim2, ...），但某些硬件架构更支持"交错"布局（dim0, dim_half, dim1, dim_half+1, ...）。

Megakernels 的 RoPE 重排功能将权重和位置编码从连续布局转换为交错布局，以匹配特定硬件的内存访问模式。

### 伪代码或流程

```
function interleave_rope(model, config):
    head_dim = config.head_dim
    half_head_dim = head_dim // 2
    
    # 1. 生成交错索引
    indices = []
    for head in range(num_heads):
        base_offset = head * head_dim
        for i in range(half_head_dim):
            indices.append(base_offset + i)           # 前半部分
            indices.append(base_offset + half_head_dim + i)  # 后半部分
    
    # 2. 重排 RoPE 的 cos 和 sin
    one_head_indices = indices[:head_dim]
    model.rope_cos = model.rope_cos[..., one_head_indices]
    model.rope_sin = model.rope_sin[..., one_head_indices]
    
    # 3. 重排所有层的 Q/K 投影权重
    for layer in model.layers:
        layer.self_attn.q_proj.weight = layer.self_attn.q_proj.weight[indices]
        layer.self_attn.k_proj.weight = layer.self_attn.k_proj.weight[indices]
```

### 原理分析

RoPE 的交错重排基于**旋转算子**的性质。RoPE 将查询向量 \(q\) 和键向量 \(k\) 通过旋转矩阵变换：

\[
\text{RoPE}(q) = q \odot \cos(\theta) + \text{rotate}(q) \odot \sin(\theta)
\]

其中 \(\text{rotate}(q)\) 将向量后半部分取反并交换：
\[
\text{rotate}(q) = \begin{bmatrix} q_1 \\ -q_0 \\ q_3 \\ -q_2 \\ \ldots \end{bmatrix}
\]

交错布局的索引模式为：
\[
\text{indices}[i] = \begin{cases}
i/2 & \text{如果 } i \text{ 是偶数} \\
\text{half\_dim} + (i-1)/2 & \text{如果 } i \text{ 是奇数}
\end{cases}
\]

这种布局使得相邻的两个元素（\(i\) 和 \(i+1\)）正好是旋转操作需要的一对。

### 代码实践

RoPE 重排的实现：

```python
def interleave_rope(self):
    # 1. 生成交错索引
    indices_for_q_list = []
    half_head_dim = self.config.head_dim // 2
    
    for n in range(self.config.num_attention_heads):
        offset = n * self.config.head_dim
        for i in range(half_head_dim):
            indices_for_q_list.append(i + offset)                    # 前半部分
            indices_for_q_list.append(i + half_head_dim + offset)     # 后半部分
    
    indices_for_q = torch.tensor(indices_for_q_list, device=self.rope_cos.device)
    one_head_indices = indices_for_q[:self.config.head_dim]
    
    # 2. 重排 RoPE 的 cos 和 sin
    self.rope_cos = self.rope_cos[..., one_head_indices]
    self.rope_sin = self.rope_sin[..., one_head_indices]
    
    # 3. 重排 K 的索引（可能比 Q 小，因为可能有 GQA）
    indices_for_k = indices_for_q[:self.config.head_dim * self.config.num_key_value_heads]
    
    # 4. 重排所有层的 Q/K 投影权重
    for mod in self.modules():
        if isinstance(mod, LlamaAttention):
            mod.q_proj.weight[:] = mod.q_proj.weight[indices_for_q]
            mod.k_proj.weight[:] = mod.k_proj.weight[indices_for_k]
```

在前向传播中，如果启用了交错 RoPE，会使用特殊的 `apply_rotary_pos_emb_interleaved` 函数：

```python
def apply_rotary_pos_emb_interleaved(q, k, cos, sin, unsqueeze_dim=1):
    cos = cos.unsqueeze(unsqueeze_dim)
    sin = sin.unsqueeze(unsqueeze_dim)
    q_embed = (q * cos) + (rotate_half_interleaved(q) * sin)
    k_embed = (k * cos) + (rotate_half_interleaved(k) * sin)
    return q_embed, k_embed

def rotate_half_interleaved(x):
    x1 = x[..., ::2]    # 取偶数索引
    x2 = x[..., 1::2]   # 取奇数索引
    new_x1 = -x2        # 前半部分取反
    new_x2 = x1         # 后半部分直接复制
    stacked = torch.stack((new_x1, new_x2), dim=-1)
    return stacked.view_as(x)
```

### 练习题

1. 为什么 V 投影权重不需要重排？
2. 如果 `head_dim=128`，交错后的 `indices_for_q` 的前 8 个元素是什么？
3. `rotate_half_interleaved` 函数中，为什么用 `::2` 和 `1::2` 而不是显式的循环？

### 答案

1. **答案**：因为 RoPE 只应用于 Query 和 Key，Value 不参与位置编码。Value 直接用于加权求和，其值不依赖于相对位置信息，所以不需要重排。

2. **答案**：前 8 个元素是 `[0, 64, 1, 65, 2, 66, 3, 67]`。模式是：对于每个 \(i\)（从 0 到 3），先添加 \(i\)（前半部分的第 i 个），再添加 \(64+i\)（后半部分的第 i 个）。

3. **答案**：`::2` 和 `1::2` 是 NumPy/PyTorch 的**切片语法**，等价于向量化操作，比显式循环更高效。`x[..., ::2]` 表示取最后一个维度的偶数索引（0, 2, 4, ...），`x[..., 1::2]` 取奇数索引（1, 3, 5, ...）。这种写法利用了底层 C 实现，性能更好且代码更简洁。

---

## 总结

本讲义介绍了 Megakernels 中 Llama 模型的四个关键重构步骤：

1. **模型组件分解**：将 HuggingFace 格式重新组织为 Megakernels 友好的组件
2. **权重堆叠策略**：将多层参数堆叠为统一张量，优化内存访问
3. **KV 缓存设置**：预分配堆叠的 KV 缓存，提升推理效率
4. **RoPE 重排**：支持交错布局以匹配特定硬件模式

这些重构为后续的 kernel 融合和高性能推理奠定了基础。