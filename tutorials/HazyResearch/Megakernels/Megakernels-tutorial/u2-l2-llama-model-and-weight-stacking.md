# LlamaForCausalLM 模型定义与权重堆叠

> 本讲对应手册单元 U2·L2，承接 [U1·L2]（仓库结构与双视角架构）。建议先读完 U1·L2，知道"Python 生成指令、CUDA 执行指令"的整体分工后再进入本讲。本讲只看 Python 侧的"建模"那一半，不涉及 CUDA。

## 1. 本讲目标

学完本讲，你应当能够：

1. 用 PyTorch 视角读懂 [llama.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py) 里 Llama 的逐层结构：`RMSNorm` → `LlamaAttention`（q/k/v/o 投影 + RoPE + 注意力 + KV cache 追加）→ `LlamaMLP`（gate/up/down + SiLU），以及把多层串起来的 `LlamaBlock` 和输出端的 `LlamaLMHead`。
2. 说清 `LlamaBlock` 内部 **attention + mlp** 的数据流，以及每一步残差（residual）是在哪里加的。
3. 解释 `stack_params` 如何把每层的 `q_proj / k_proj / v_proj` 先 `cat` 成单个 `qkv` 权重，再把所有层 `stack` 成一张 `[num_layers, ...]` 的大张量；同时理解其它六组权重（o_proj、两个 layernorm、up/gate/down）的堆叠。
4. 说清**跨层堆叠权重为何能减少内核里的分支与循环开销**——这是连接本讲与 CUDA megakernel 的关键直觉。
5. 理解 `setup_caches` 如何构造一张跨层的 KV cache 大张量，并把每一层的 `self_attn.kv_cache` 指向它的切片。
6. 理解 `from_pretrained` 的完整流程：HF 配置加载 → 元设备建图 → safetensors 权重加载（含参数名重映射与 TP 切分）→ 堆叠 → 建 cache。

## 2. 前置知识

- **Transformer 解码层（decoder block）**：Llama 的每一层都是"先做自注意力（attention），再做前馈（MLP），各带一个残差连接"。本讲会把这行话逐字落到代码。
- **RMSNorm**：LayerNorm 的近亲，只做缩放、不减均值。Llama 全程用它替代 LayerNorm。后面会给公式。
- **RoPE（旋转位置编码）**：把位置信息"旋转"进 q、k 向量。本讲直接复用 HuggingFace 的 `LlamaRotaryEmbedding` 与 `apply_rotary_pos_emb`，不展开数学，只说"在哪一步调用"。
- **GQA（分组查询注意力）**：q 的头数（`num_attention_heads`）可以多于 k/v 的头数（`num_key_value_heads`），多个 q 头共享一组 k/v。Llama-3 系列普遍采用。代码里这两者可以是不同的数。
- **`nn.Linear` 的 weight 形状**：`nn.Linear(in, out).weight` 的形状是 `[out_features, in_features]`（注意是反过来），计算时 `y = x @ weight.T`。本讲讲堆叠时大量依赖这一点。
- **KV cache**：自回归生成时，历史 token 的 k/v 存起来复用，避免每步重算。Megakernels 把所有层的 KV cache 合成一张大张量统一管理。
- **safetensors**：HuggingFace 推广的权重存储格式，按名字存张量、可零拷贝映射。本讲的 `from_pretrained` 最终从 `.safetensors` 文件读权重。
- **张量并行（TP）**：把权重沿某个维度切到多张 GPU 上。本讲默认 `tp_size=1`（单卡），把 TP 当作"可选的高级开关"，只在 `from_pretrained` 一节简要提及，不作重点。

> 如果上面某些词还陌生，记住这句话即可：**本讲要回答的是"Llama 在 Python 里长什么样、它的权重如何被重新摆成一堆大张量交给 GPU"**。其余概念会随代码展开。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [megakernels/llama.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py) | **本讲主角**。定义 `RMSNorm`、`LlamaAttention`、`LlamaMLP`、`LlamaBlock`、`LlamaLMHead`、`LlamaModel`、`LlamaForCausalLM`，以及 `stack_params` / `setup_caches` / `from_pretrained` |
| [megakernels/model_types.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/model_types.py) | 定义在层与层之间流动的"行李箱" `BatchState`，以及携带 TP/编译/最大长度等标志的 `ExtraModelConfig` |
| [megakernels/utils.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/utils.py) | `load_safetensors_repo`：真正读 safetensors、支持 TP 切分的工具函数 |
| [megakernels/demos/latency/scheduler.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py) | 消费侧证据：`make_globals` 把 `model.stacked_params` 和 `model.stacked_kv_cache` 直接塞进 GPU 要用的 `Globals` |
| [megakernels/demos/latency/instructions.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/instructions.py) | 消费侧证据：每条指令里带一个 `layer_idx` 字段，megakernel 靠它去大张量里"按层取片" |

## 4. 核心概念与源码讲解

### 4.1 模型结构骨架：RMSNorm、LlamaBlock 与 LlamaLMHead

#### 4.1.1 概念说明

[llama.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py) 里用 PyTorch 把一个标准 Llama 解码器搭出来。整张图从下往上看是：

```
input_ids
   │  (LlamaEmbeddings: 查表)
   ▼
hidden_states ──► ┌─────────── LlamaBlock × num_layers ───────────┐
                  │  LlamaAttention (含自己的 RMSNorm + 残差)      │
                  │  LlamaMLP       (含自己的 RMSNorm + 残差)      │
                  └──────────────────────────────────────────────┘
   │  (循环 num_layers 次)
   ▼
LlamaLMHead (RMSNorm + lm_head) ──► logits ──► argmax ──► next_token
```

有两点"和教科书略不同、但很重要"的设计：

1. **RMSNorm 被收进了 Attention 和 MLP 各自内部**。标准 HF Llama 里，每个 block 有 `input_layernorm`（进 attention 前）和 `post_attention_layernorm`（进 MLP 前）两个 norm，归 block 管；这里把它们分别搬进了 `LlamaAttention.input_layernorm` 和 `LlamaMLP.input_layernorm`。所以残差相加也发生在各自模块内部。
2. **模块之间不直接传张量，而是传一个 `BatchState`"行李箱"**。每个 `forward` 收一个 `BatchState`、改里面的字段（主要是 `hidden_states`）、再把它原样传出去。这样调度器（scheduler）可以很自然地把"一层"当成"一步"来编排。

#### 4.1.2 核心流程：一个 LlamaBlock 内部发生了什么

以 `LlamaBlock.forward` 为入口（伪代码）：

```
def LlamaBlock.forward(batch_state):
    batch_state = self.self_attn(batch_state)   # 内部已做 norm + q/k/v + RoPE + attn + o_proj + 残差
    batch_state = self.mlp(batch_state)         # 内部已做 norm + gate/up + SiLU + down + 残差
    return batch_state
```

**Attention 子流程**（`LlamaAttention.forward`）：

1. 保存 `residual = inp`。
2. `hidden = self.input_layernorm(inp)` —— RMSNorm。
3. （TP>1 时）`all_gather` 把隐藏态在层间拼接，保证后续投影拿到完整 hidden。
4. `q = q_proj(hidden)`、`k = k_proj(hidden)`、`v = v_proj(hidden)`，并 reshape 成多头。
5. 应用 RoPE 到 q、k。
6. 调 `attention(...)`：把当前 k、v **追加写进 KV cache**，再用 `F.scaled_dot_product_attention`（GQA、prefill 时 causal、decode 时不 causal）算注意力输出。
7. `o = o_proj(attn_output)`，再 `reduce_scatter`（TP>1 时）。
8. `hidden = residual + o`，写回 `batch_state.hidden_states`。

**MLP 子流程**（`LlamaMLP.forward`）：

1. `residual = inp`。
2. `hidden = self.input_layernorm(inp)`。
3. `up = up_proj(hidden)`、`gate = gate_proj(hidden)`、`prod = F.silu(gate) * up`、`down = down_proj(prod)`。
4. `reduce_scatter`（TP>1 时）。
5. `hidden = residual + down`，写回。

MLP 里 `gate` 与 `up` 是经典 SwiGLU：用 `gate` 过 SiLU 激活后逐元素乘 `up`，再经 `down` 投回隐藏维度。

> RMSNorm 的数学定义（代码 [llama.py:38-47](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L38-L47) 完全对应）：
>
> \[ \mathrm{RMSNorm}(x) = w \odot \frac{x}{\sqrt{\dfrac{1}{d}\sum_{i=1}^{d} x_i^2 + \varepsilon}} \]
>
> 其中 \(w\) 是可学习缩放（`self.weight`），\(\varepsilon\) 是 `config.rms_norm_eps`，\(d\) 是隐藏维数。注意代码先转 fp32 算方差再转回原精度，这是数值稳定性的常见做法。

#### 4.1.3 源码精读

**(a) RMSNorm** —— [megakernels/llama.py:29-47](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L29-L47)

这段几乎逐行照搬 HF 的 `LlamaRMSNorm`：转 fp32 → 算均方 → 乘 `rsqrt(均方+eps)` → 乘 `weight`。`self.weight` 初始化为全 1（第 36 行）。

**(b) LlamaAttention 的投影定义** —— [megakernels/llama.py:209-228](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L209-L228)

这里声明了四个 `nn.Linear(..., bias=False)`：`q_proj`（hidden→q 总维）、`k_proj`/`v_proj`（hidden→kv 总维，维度更小，对应 GQA）、`o_proj`（投影回 hidden）。注意都 `bias=False`，这是 Llama 的特点。`num_attention_heads` 与 `num_kv_heads` 都已按 `tp_size` 缩放（[llama.py:202-207](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L202-L207)），`tp_size=1` 时就是全局头数。

**(c) Attention 前向：投影→RoPE→注意力→残差** —— [megakernels/llama.py:250-296](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L250-L296)

第 250-252 行算 q/k/v；第 267-273 行根据 `interleave_rope` 选择 RoPE 变体后套到 q、k；第 278-285 行调 `attention(...)`；第 289 行 `o_proj`；第 293 行 `residual + o_proj` 落地残差。

**(d) KV cache 的追加与读取** —— [megakernels/llama.py:100-133](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L100-L133)

`attention()` 第 102-103 行 `k_cache[:, position_ids] = key_states` 是"追加"——把当前 k、v 按 `position_ids` 写进 cache 对应槽位。prefill（`new_tok_seq_len > 1`）时直接用刚写入的 k/v 做 causal 注意力；decode（`=1`）时则读 `k_cache[:, :seq_len]` 整段历史做非 causal 注意力。`enable_gqa=True` 让 SDPA 自动处理 q 头多于 kv 头的情况。

**(e) LlamaMLP** —— [megakernels/llama.py:328-348](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L328-L348)

`up/gate/down` 三个无偏置线性层；第 340 行 `F.silu(gate) * up` 就是 SwiGLU 的核心；第 345 行加残差。

**(f) LlamaBlock 串联** —— [megakernels/llama.py:351-366](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L351-L366)

`LlamaBlock` 只是把 `self_attn` 和 `mlp` 串起来，各自吃 `batch_state` 吐 `batch_state`。它本身没有可学参数——参数全在两个子模块里（含各自的 `input_layernorm`）。

**(g) LlamaLMHead：最后一步出 token** —— [megakernels/llama.py:369-403](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L369-L403)

`input_norm`（RMSNorm）→ `lm_head`（hidden→vocab）→ `argmax(dim=-1)` 直接得到 `next_token_ids`，写进 `batch_state.output_ids`。注意它把"出 logits"和"取 argmax"合在一步，因为本框架只关心下一个 token，不需要完整概率分布。

**(h) BatchState 行李箱** —— [megakernels/model_types.py:20-39](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/model_types.py#L20-L39)

一个 dataclass，核心字段是 `hidden_states`（各层间传递的隐藏态）、`input_ids`、`position_ids`、`seq_len`、`position_embeddings`（RoPE 的 cos/sin），以及一批 KV-cache 相关的索引张量。`__post_init__` 里若没给 `seq_len` 就用 `input_ids.shape[1]` 兜底。每个子模块的 forward 都"原地改它"，正是 4.1.1 说的"传行李箱"风格。

#### 4.1.4 代码实践：在纸上跑一遍 `LlamaBlock.forward`（源码阅读型）

1. **实践目标**：不运行代码，仅靠阅读，说清一个 token 经过一层 `LlamaBlock` 时，残差分别加在哪两个地方、`hidden_states` 字段如何被改写。
2. **操作步骤**：
   1. 打开 [llama.py:363-366](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L363-L366)，确认 `LlamaBlock.forward` 先调 `self_attn` 再调 `mlp`，二者都返回同一个 `batch_state`。
   2. 在 [llama.py:293](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L293) 找到第一个残差：`residual + o_proj`，确认它写回 `batch_state.hidden_states`（第 295 行）。
   3. 在 [llama.py:345](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L345) 找到第二个残差：`inp + down`，确认同样写回 `batch_state.hidden_states`（第 347 行）。
3. **需要观察的现象**：你能指出"进入 attention 前的 hidden"和"attention 出口、MLP 入口"是同一个张量（`residual` 来自它），MLP 又在它的基础上再加一次残差。
4. **预期结果**：用一句话描述："一层的输出 = `hidden + o_proj(RMSNorm(hidden) 经注意力) + down_proj(SiLU(gate_proj(RMSNorm(hidden'))) * up_proj(...))`，其中 `hidden'` 是 attention 之后的中间结果"。两处残差对应 [llama.py:293](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L293) 与 [llama.py:345](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L345)。
5. **说明**：本实践为源码阅读型，无需 GPU。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 `k_proj`/`v_proj` 的输出维度（`num_kv_heads * head_dim`）通常小于 `q_proj`（`num_attention_heads * head_dim`）？
  - **答案**：因为采用 GQA，多个 q 头共享同一组 k/v 头，所以 `num_key_value_heads ≤ num_attention_heads`。对应代码 [llama.py:209-223](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L209-L223)，`attention()` 里用 `enable_gqa=True` 让 SDPA 自动广播。
- **练习 2**：`LlamaBlock` 自己有可学习参数吗？
  - **答案**：没有。它只持有 `self_attn` 和 `mlp` 两个子模块，可学参数（四个投影、两个 RMSNorm、三个 MLP 线性）都在子模块里。见 [llama.py:360-361](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L360-L361)。

---

### 4.2 stack_params：把权重堆叠成单张大张量

#### 4.2.1 概念说明

到这一节之前，模型权重还是"标准 PyTorch 摆法"：每层各有一份 `q_proj/k_proj/v_proj/o_proj`、两个 layernorm、`up/gate/down`，共约 7 组 × `num_layers` 份独立小张量。这种摆法对**训练**很友好，但对 Megakernels 的 **GPU megakernel** 不友好。

原因要回到 U1·L2 讲过的架构：megakernel 是"逐条执行指令"的虚拟机，它通过 pybind11 的 `bind_kernel` **按固定顺序**接收一串全局张量指针。如果每层权重都是独立张量，那么：

- 要么给 `bind_kernel` 传 `num_layers × 7` 个指针（数量随模型变，"插针脚定义"无法固定）；
- 要么内核里维护一张"层号→权重指针"的查找表（额外间接寻址，破坏"固定接线"的简洁）；
- 要么内核里写 `if layer_idx == 0 ... else if ...` 的分支（warp 分支发散，性能杀手）。

`stack_params` 的做法是：**把这 `num_layers` 份同类权重沿第 0 维 `stack` 成一张 `[num_layers, ...]` 的大张量**，再把 q/k/v 三者进一步 `cat` 成一个 `qkv`。这样 7 组权重就变成 **7 张大张量**（数量固定，与层数无关），megakernel 只要 7 个固定指针即可。

`StackedParams` 这个 dataclass（[llama.py:490-498](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L490-L498)）正是这 7 张大张量的容器。

#### 4.2.2 核心流程：qkv 的 cat 与跨层 stack

`stack_params` 内部对一个权重组，统一的套路是 `stack_and_reassign`：

```
def stack_and_reassign(modules, prop):
    params = [getattr(m, prop) for m in modules]   # 取出每层该权重
    stacked = torch.stack(params, dim=0)            # 沿第0维堆成 [num_layers, ...]
    for i, m in enumerate(modules):
        getattr(m, prop)[:] = stacked[i]            # 原地把值写回（保持与stacked一致）
    return stacked
```

- `torch.stack` 会**新建**一张大张量（拷贝）。
- 后面的 `[:] = stacked[i]` 是"原地赋值"：把堆叠后的值再拷回各层原本的参数存储。**这一步不会改变各层参数指向的存储**，只是保证"标准 PyTorch 前向（`mode=torch`）"和"堆叠后的值"完全一致——于是同一份模型既能当参考实现跑，又能把 `stacked_params` 喂给内核。

对 q/k/v 特别处理，因为内核希望它们**融合成一个投影**：

```
for 每层 self_attn:
    cat_weight = torch.cat([q_proj.weight, k_proj.weight, v_proj.weight], dim=0)
    qkv_weights.append(cat_weight)
stacked_qkv_weights = torch.stack(qkv_weights, dim=0)        # [L, (Nq+2Nkv)*head_dim, hidden]
# 再 split 回 q/k/v，原地写回，保持一致
```

最终的形状（设 `head_dim = d`、`num_attention_heads = Nq`、`num_key_value_heads = Nkv`、`hidden_size = H`、`num_hidden_layers = L`，且 `tp_size=1`）：

| 堆叠后字段 | 形状 |
| --- | --- |
| `qkv_proj` | `[L, (Nq + 2·Nkv)·d, H]` |
| `o_proj` | `[L, Nq·d, H]` |
| `attn_ln_weight` | `[L, H]` |
| `mlp_ln_weight` | `[L, H]` |
| `up_proj` | `[L, intermediate_size, H]` |
| `gate_proj` | `[L, intermediate_size, H]` |
| `down_proj` | `[L, H, intermediate_size]` |

注意第 0 维永远是 `num_hidden_layers`——这正是内核"按 `layer_idx` 取片"所依赖的维度。

> 关于 qkv 的 `cat` 与回 `split`：[llama.py:756-763](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L756-L763) 用 `config.num_attention_heads` / `config.num_key_value_heads`（全局头数）作 split 尺寸。在默认 `tp_size=1` 时，全局头数与各层投影实际头数相等，split 干净对齐；`tp_size>1` 的切分在 `from_pretrained` 的加载阶段（见 4.4）就已按 `tp_map` 完成，本讲把 TP 当作高级开关，先聚焦单卡情形。

#### 4.2.3 源码精读

**(a) StackedParams 容器** —— [megakernels/llama.py:490-498](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L490-L498)

7 个字段，与上面表格一一对应。`from_pretrained` 末尾调 `stack_params` 后，模型就多了一个 `self.stacked_params` 属性。

**(b) stack_and_reassign 工具函数** —— [megakernels/llama.py:714-719](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L714-L719)

`stack` + 原地回写的标准套路。被 o_proj、两个 layernorm、up/gate/down 共 6 组复用。

**(c) 六组普通权重的堆叠** —— [megakernels/llama.py:721-738](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L721-L738)

先从 `self.model.layers` 抽出每层的 `self_attn` 与 `mlp`，再分别收集 `o_proj`、两个 `input_layernorm`、`up/gate/down`，逐组 `stack_and_reassign`。

**(d) qkv 的融合堆叠** —— [megakernels/llama.py:740-767](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L740-L767)

第 742-749 行对每层 `cat([q, k, v], dim=0)`；第 752 行跨层 `stack`；第 754-767 行再 `split` 回写。这是本讲的核心代码段。

**(e) 组装 StackedParams** —— [megakernels/llama.py:769-777](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L769-L777)

把 7 张大张量打包成 `self.stacked_params`。

**(f) 消费侧证据①：scheduler 直接取用** —— [megakernels/demos/latency/scheduler.py:49-75](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L49-L75)

`make_globals` 里 `stacked_params = model.stacked_params`，然后把 `stacked_params.qkv_proj`、`stacked_params.o_proj`、…、`stacked_params.down_proj` 直接当作 `Globals` 的字段。注意这里的字段名（如 `qkv_proj_weights`）就是之后 pybind11 `bind_kernel` 按**固定顺序**传给 megakernel 的那些张量——层数再多，也只是每张大张量的第 0 维变长，**字段数量永远固定为 7**。

**(g) 消费侧证据②：指令里的 layer_idx** —— [megakernels/demos/latency/instructions.py:32-44](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/instructions.py#L32-L44)

`LayerNorm_QKV_MatVecRopeAppend` 这条指令带一个 `layer_idx: int` 字段。megakernel 执行它时，就是拿这个 `layer_idx` 去 `qkv_proj_weights[layer_idx]` 取当前层的 qkv 权重。**因为所有层都堆在一张大张量里，"取第几层"退化成一次整型索引，没有任何 `if/switch` 分支**——这正是堆叠带来的核心收益。

#### 4.2.4 代码实践：追踪 q/k/v 如何被 cat 成单个 qkv 权重

1. **实践目标**：亲手在纸上把 `q_proj/k_proj/v_proj` 三块权重"cat 再 stack"后的形状算出来，并对照消费侧确认形状自洽。
2. **操作步骤**：
   1. 假设一个**示意性配置**（仅作演示，非任何真实模型）：`hidden_size = H = 8`，`head_dim = d = 2`，`num_attention_heads = Nq = 4`（故 q 总维 = 4×2 = 8），`num_key_value_heads = Nkv = 2`（故 k/v 总维 = 2×2 = 4），`num_hidden_layers = L = 3`，`intermediate_size = 12`。
   2. 在 [llama.py:742-749](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L742-L749) 模拟 `cat([q.weight(8×8), k.weight(4×8), v.weight(4×8)], dim=0)`，得到单层 qkv 形状 `(8+4+4, 8) = (16, 8)`。
   3. 在 [llama.py:752](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L752) 模拟跨 3 层 `stack(dim=0)`，得到 `stacked_qkv_weights` 形状 `(3, 16, 8)`。
   4. 在 [llama.py:756-763](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L756-L763) 验证 split 尺寸 `[Nq·d=8, Nkv·d=4, Nkv·d=4]` 之和 = 16，正好等于第 1 维，能干净切回 q/k/v。
3. **需要观察的现象**：第 0 维（`3`）= 层数，第 1 维（`16`）= q+k+v 融合后的输出维，第 2 维（`8`）= hidden。`layer_idx ∈ {0,1,2}` 各取一片就是一层完整的 qkv 权重。
4. **预期结果**：你能画出 `stacked_qkv_weights` 的三维形状并说清"切第 0 维得一层、切第 1 维前 8 行是 q、中 4 行是 k、后 4 行是 v"。
5. **可选运行验证（待本地验证）**：若有本地 PyTorch 环境（无需 GPU、无需下载模型），可用如下示例代码核对形状直觉：

   ```python
   # 示例代码（非项目原有代码）：用随机小张量复现 stack_params 对 qkv 的处理
   import torch
   L, Nq, Nkv, d, H = 3, 4, 2, 2, 8
   q = [torch.randn(Nq * d, H) for _ in range(L)]
   k = [torch.randn(Nkv * d, H) for _ in range(L)]
   v = [torch.randn(Nkv * d, H) for _ in range(L)]
   qkv = torch.stack([torch.cat([q[i], k[i], v[i]], dim=0) for i in range(L)], dim=0)
   print(qkv.shape)        # 预期 torch.Size([3, 16, 8])
   q0, k0, v0 = qkv[0].split([Nq*d, Nkv*d, Nkv*d], dim=0)
   print(q0.shape, k0.shape, v0.shape)  # 预期 [8,8] [4,8] [4,8]
   ```

6. **说明**：上面的形状推导不依赖运行即可完成；可运行片段标注为"示例代码"，运行结果待本地验证。

#### 4.2.5 小练习与答案

- **练习 1**：`stack_and_reassign` 里 `getattr(m, prop)[:] = stacked[i]` 这一步能否省掉？为什么作者还是写了它？
  - **答案**：从"喂给内核"的角度可以省（内核只用 `stacked_params`）。但作者保留它，是为了让模型的**标准 PyTorch 前向**（`mode=torch` 参考路径，走 [llama.py:250-252](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L250-L252) 的 `q_proj` 等）在 `stack_params` 之后仍得到与堆叠值一致的结果，便于正确性比对。注意它是原地赋值，**不改变**各层参数的存储指针。
- **练习 2**：如果不做 q/k/v 的 `cat` 融合，而是只做跨层 `stack`，内核会有什么不同？
  - **答案**：内核就得维护 `q_proj_weights`、`k_proj_weights`、`v_proj_weights` 三张大张量（或三次独立投影），多一次"取 k、取 v、再算"的调度与访存。融合成 `qkv` 后，一次投影就能同时产出 q/k/v，减少指令数与访存次数——这也是 `LayerNorm_QKV_MatVecRopeAppend`（[instructions.py:32-44](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/instructions.py#L32-L44)）名字里把 "QKV" 写在一起的原因。

---

### 4.3 setup_caches：构建跨层 KV cache

#### 4.3.1 概念说明

`attention()` 函数（4.1.3(d)）依赖一个 `kv_cache` 来存历史 k/v。问题是：**每层的 cache 放在哪？** 最朴素的办法是每层各自 `torch.zeros(...)` 一块。Megakernels 的做法和权重堆叠同理——**把所有层的 cache 也合成一张大张量**，第 0 维是层号，每层用其切片。

这样做有两个好处，和 4.2 一脉相承：

1. megakernel 只要 **2 个固定指针**（`k_cache`、`v_cache`），层数再多也不变。
2. 所有层的 cache 在显存里**连续排布**，loader warp 可以按层号做规整的批量搬运。

#### 4.3.2 核心流程

`setup_caches` 做两件事：

1. 分配 `k_cache` 与 `v_cache` 两张大张量（形状见下），互为 `clone`，组成 `self.stacked_kv_cache`。
2. 遍历每一层，把 `self.stacked_kv_cache[0][layer_idx]` / `[1][layer_idx]` 这个**切片**赋给 `layer.self_attn.kv_cache`。于是 4.1.3(d) 里 `attention()` 写的 `self.kv_cache`，本质是在写大张量里属于本层的那一片——**切片与大张量共享存储**。

cache 形状（`tp_size=1`，设最大序列长 `S`、最大批 `B`）：

\[ \text{k\_cache.shape} = [\,L,\ B,\ S,\ N_{kv},\ d\,] \]

其中 `S = max_len_override or max_position_embeddings`（见 [llama.py:557-558](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L557-L558)），`B = max_batch_size`，`Nkv = num_key_value_heads`，`d = head_dim`。第 0 维 `L` 是层数——和权重的堆叠维度一致。

#### 4.3.3 源码精读

**(a) 分配大张量** —— [megakernels/llama.py:552-567](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L552-L567)

`k_cache` 的 5 个维度依次是 `num_hidden_layers / max_batch_size / (max_len_override or max_position_embeddings) / num_key_value_heads / head_dim`。`v_cache = k_cache.clone()`。二者打包成 `self.stacked_kv_cache`。

**(b) 把切片指派给每层** —— [megakernels/llama.py:569-574](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L569-L574)

循环 `layer_idx`，令 `layer.self_attn.kv_cache = (stacked_kv_cache[0][layer_idx], stacked_kv_cache[1][layer_idx])`。注意这是切片赋值，不是拷贝——之后 `attention()` 里 `k_cache[:, position_ids] = ...` 直接写进大张量对应层。

**(c) 消费侧证据** —— [megakernels/demos/latency/scheduler.py:74-75](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L74-L75)

`make_globals` 把 `model.stacked_kv_cache[0]` / `[1]` 直接当 `Globals.k_cache` / `v_cache`——和 `stacked_params` 一样，是 pybind11 固定接线的两个槽位。

#### 4.3.4 代码实践：核对 cache 切片与 attention 的写入目标

1. **实践目标**：确认"每层的 `self.kv_cache`"与"`self.stacked_kv_cache[layer_idx]`"指向同一块显存。
2. **操作步骤**：
   1. 读 [llama.py:569-574](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L569-L574)，确认 `kv_cache` 被赋成大张量的切片（`stacked_kv_cache[0][layer_idx]`）。
   2. 读 [llama.py:102-103](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L102-L103)，确认 `attention()` 是**原地写** `k_cache[:, position_ids] = key_states`。
   3. 结合这两点推断：第 `layer_idx` 层 decode 时写入的 k/v，落在了 `stacked_kv_cache[*][layer_idx]` 里。
3. **需要观察的现象**：你能讲清"层的视角看是自己私有的 cache，全局视角看是大张量的一片，二者共享存储"。
4. **预期结果**：一句话——`setup_caches` 之后，每层 `self.kv_cache` 是 `stacked_kv_cache[layer_idx]` 的视图，写它等于写大张量。
5. **说明**：源码阅读型实践，无需 GPU。

#### 4.3.5 小练习与答案

- **练习 1**：`max_len_override`（来自 `ExtraModelConfig`）有什么用？
  - **答案**：让用户**覆盖** cache 的最大长度，而不必受 `config.max_position_embeddings` 限制。见 [llama.py:557-558](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L557-L558) 的 `or` 短路：设了 override 就用它，否则退回 `max_position_embeddings`。`ExtraModelConfig.max_len_override` 默认 `None`（[model_types.py:58](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/model_types.py#L58)）。
- **练习 2**：为什么 cache 第 0 维是层数、而不是把 batch 放最前？
  - **答案**：为了让"取一层"是一次连续切片（`cache[layer_idx]`），与权重的 `stacked_params[*][layer_idx]` 取法对齐，方便 megakernel 按 `layer_idx` 统一索引；层数维度放最前也更利于按层连续搬运。

---

### 4.4 from_pretrained：safetensors 加载与初始化全流程

#### 4.4.1 概念说明

`from_pretrained` 是"把一个 HuggingFace 上的 Llama 拉下来、摆成 Megakernels 想要的样子"的总入口。它把前面三节的能力串起来：建图 → 加载权重 → 堆叠 → 建 cache。理解它就理解了"一个模型从下载到可被 megakernel 使用"的完整链条。

它要处理两个"名字不匹配"的麻烦：

1. **本项目的参数名 ≠ HF 标准名**。例如本项目把 attention 的 norm 放进了 `self_attn.input_layernorm`，HF 里它叫 `input_layernorm`（但挂在 block 下）；MLP 的 norm 本项目叫 `mlp.input_layernorm`，HF 里叫 `post_attention_layernorm`。所以加载时要做一张"本项目名 → HF 名"的映射表。
2. **TP 切分**。`tp_size>1` 时，部分权重要在加载时沿指定维切下本卡那份。本项目用一张 `tp_map`（参数名→切分维）声明哪些权重切、沿哪维切。

#### 4.4.2 核心流程：from_pretrained 的 12 步

```
1. 读 HF LlamaConfig（含 rope_scaling 覆盖）
2. 确定 dtype（默认 config.torch_dtype）
3. init_empty_weights() 上下文里建模型骨架（不分配真实显存，元设备）
4. 设置 model.dtype / device
5. 解析模型路径：本地路径存在就用，否则 huggingface_hub.snapshot_download 下载 *.safetensors + *.json
6. model.load_from_safetensors(model_path)   ← 真正读权重（含改名映射 + TP 切分）
7. model.to(device)（注意：只搬设备，不改 dtype，见下方注释）
8. requires_grad_(False)                      ← 推理，关掉梯度
9. 若 interleave_rope：model.model.interleave_rope()
10. model.stack_params()                       ← 4.2
11. model.setup_caches()                       ← 4.3
12. return model
```

第 6 步 `load_from_safetensors` 内部又分三小步：`make_name_to_hf_name()`（建改名表）→ `load_safetensors_repo(...)`（读文件，按 `tp_map` 切分）→ `load_state_dict(assign=True, strict=True)`（严格装入，缺一不可）。

> **关于第 7 步"不转 dtype"**：代码 [llama.py:621-628](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L621-L628) 有一段重要注释——如果调 `model.to(device, dtype)` 会把 RoPE 里的 `inv_freq` 缓冲也转成 fp16，破坏精度；HF 的加载流程特意保留它为 fp32。所以这里只 `model.to(device=device)`，dtype 由前面加载时按参数逐个处理。

#### 4.4.3 源码精读

**(a) from_pretrained 主干** —— [megakernels/llama.py:583-638](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L583-L638)

第 594 行 `LlamaConfig.from_pretrained` 读配置；第 601-605 行在 `init_empty_weights` 里建图；第 609-617 行解析路径（本地优先，否则 `snapshot_download` 只拉 `*.safetensors` 与 `*.json`）；第 619 行加载；第 635-636 行收尾做 `stack_params` 与 `setup_caches`。

**(b) 元设备建图** —— [megakernels/llama.py:601-607](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L601-L607)

`with init_empty_weights(...)`（来自 `accelerate`）让 `nn.Linear` 等在**元设备**上创建，不占显存；真正显存在第 6 步 `load_from_safetensors` 里由 `load_state_dict(assign=True)` 直接"挂"上读到的张量。这样避免"先全零分配再覆盖"的二次内存峰值。

**(c) 改名表** —— [megakernels/llama.py:640-663](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L640-L663)

`make_name_to_hf_name` 把本项目参数名映射到 HF 名。关键几条：`self_attn.input_layernorm.weight` → HF `input_layernorm.weight`；`mlp.input_layernorm.weight` → HF `post_attention_layernorm.weight`（名字都变了）；`lm_head.input_norm.weight` → HF `model.norm.weight`；`embed_tokens.embed_tokens.weight` → HF `model.embed_tokens.weight`。还处理 `tie_word_embeddings`（词嵌入与输出头共享权重）的两种情况。

**(d) TP 切分表** —— [megakernels/llama.py:665-691](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L665-L691)

`make_tp_map` 声明：`q/k/v/up/gate_proj.weight` 沿 dim=0 切（行并行，即按输出维切，每个 TP rank 算一部分输出）；`o/down_proj.weight` 沿 dim=1 切（列并行，即按输入维切，各 rank 算一部分输入再相加）。这是张量并行的标准套路。`tp_size=1` 时这张表不影响结果（整块取走）。

**(e) 真正读文件** —— [megakernels/llama.py:693-711](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L693-L711)

`load_from_safetensors` 先建改名表与"需要的 HF 名集合"，调 `load_safetensors_repo(...)`（[utils.py:34](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/utils.py#L34) 起）读盘并按 `tp_map` 切分，再把结果按改名表"翻"回本项目名，最后 `load_state_dict(..., assign=True, strict=True)`。

**(f) load_safetensors_repo 细节** —— [megakernels/utils.py:34-68](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/utils.py#L34-L68)

优先找单文件 `model.safetensors`；找不到就读 `model.safetensors.index.json` 的 `weight_map`，按 `include_parameters` 过滤出真正需要打开的那几个分片文件（大模型常拆成多片）。`tp_size>1` 时用 `f.get_slice(k)` 按切分维只读本 rank 的那一块，避免读整张再切。

#### 4.4.4 代码实践：追踪一条权重的"改名—切分—装入"路径

1. **实践目标**：以第 0 层 MLP 的 norm 为例，看清它在 HF safetensors 里的名字、在本项目里的名字，以及它如何被装入。
2. **操作步骤**：
   1. 在 [llama.py:649-651](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L649-L651) 找到映射：本项目 `model.layers.0.mlp.input_layernorm.weight` → HF `model.layers.0.post_attention_layernorm.weight`。
   2. 在 [llama.py:700-707](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L700-L707) 确认 `load_safetensors_repo` 用 `include_parameters = 所有 HF 名` 来过滤，并按 `make_tp_map()` 切分。注意 layernorm 的 weight **不在** `tp_map` 里（[llama.py:665-691](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L665-L691) 只列了投影权重），所以 norm 不被切分，每卡拿完整一份。
   3. 在 [llama.py:709-711](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L709-L711) 确认最终 `{本项目名: HF张量}` 经 `load_state_dict(assign=True, strict=True)` 装入——`strict=True` 意味着名字必须严丝合缝，多一个少一个都会报错。
3. **需要观察的现象**：你能复述"HF 的 `post_attention_layernorm.weight` 经改名表翻成本项目 `mlp.input_layernorm.weight`，不被 TP 切分，原样装入"。
4. **预期结果**：画出一条 `safetensors 文件 → weight_map 定位 → load_safetensors_repo 读张量 → 改名 → load_state_dict` 的链路。
5. **说明**：源码阅读型实践。若想真正运行 `from_pretrained`，需能访问 HuggingFace 下载对应 Llama 权重（见 `scripts/generate.py`、`scripts/diff_test.py` 的用法），结果待本地验证。

#### 4.4.5 小练习与答案

- **练习 1**：为什么用 `init_empty_weights` 建图，而不是直接 `cls(config, extra_config)`？
  - **答案**：直接建图会给所有 `nn.Linear` 分配全零显存，随后又被 `load_state_dict(assign=True)` 覆盖，造成一次无谓的内存峰值。`init_empty_weights` 在元设备上建图不占显存，加载时由 `assign=True` 直接挂上读到的真实张量。见 [llama.py:601-605](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L601-L605)。
- **练习 2**：`stack_params` 和 `setup_caches` 为什么必须放在 `from_pretrained` **末尾**、在权重加载**之后**？
  - **答案**：二者都依赖"权重/形状已经正确就位"。`stack_params` 要 `cat/stack` 真实的 q/k/v 等权重；`setup_caches` 要按已确定的 `num_hidden_layers`、`num_key_value_heads`、`head_dim` 分配 cache 并把切片指派给各层。顺序反了就会堆到空权重或未就绪的层上。见 [llama.py:635-636](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L635-L636)。

## 5. 综合实践：解释"跨层堆叠为何能减少内核分支与循环开销"

把本讲内容串起来，完成规格要求的核心任务：**讲清 `stack_params` 把权重堆叠后，为什么 megakernel 能少写分支、少跑循环**。

**任务**：阅读以下三处源码，写一段 150 字左右的解释，覆盖三个要点：①堆叠前后"指针数量"的变化；②`layer_idx` 如何把"选层"变成一次整型索引；③堆叠对访存连续性的好处。

1. 堆叠的产出：[llama.py:769-777](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L769-L777)（7 张大张量，第 0 维是层数）。
2. 固定接线的消费侧：[demos/latency/scheduler.py:63-75](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L63-L75)（无论多少层，`Globals` 的权重/KV 字段数量恒定）。
3. 用 `layer_idx` 取片的指令：[demos/latency/instructions.py:32-44](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/instructions.py#L32-L44)（指令自带 `layer_idx`）。

**参考答案要点**：

- **指针数量**：堆叠前，每层 7 组权重各一份，外加每层 KV cache，共约 `(7L + 2L)` 个独立张量；堆叠后只剩 **7 个权重大张量 + 2 个 cache 大张量 = 9 个**，与层数 `L` 无关。pybind11 的 `bind_kernel` 因此能用一套固定的"针脚定义"对接任意层数的模型。
- **去分支**：内核执行一条带 `layer_idx` 的指令时，"取本层 qkv 权重"就是 `qkv_proj_weights[layer_idx]`——一次整型索引，**没有 `if layer == …` 的 switch、没有层号→指针的查找表**。warp 里所有线程走同一条取数路径，没有分支发散。
- **访存连续**：所有层同类权重在第 0 维连续排布，loader warp 可以按 `layer_idx` 做规整的偏移取数；KV cache 同理（4.3）。连续访问对 GPU 显存带宽友好。

**自检**：如果你能在解释里同时点出"指针数从 O(L) 降到 O(1)"、"layer_idx 把分支变成索引"、"第 0 维连续利于搬运"这三点，就达成了本讲的核心目标。

## 6. 本讲小结

- [llama.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py) 用标准 PyTorch 搭出 Llama：`RMSNorm` + `LlamaAttention`（q/k/v/o + RoPE + SDPA + KV cache 追加）+ `LlamaMLP`（gate/up/down + SiLU），`LlamaBlock` 把二者串联，`LlamaLMHead` 在末端出 token。
- 残差相加发生在 **Attention 内部**（[llama.py:293](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L293)）与 **MLP 内部**（[llama.py:345](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L345)）；层与层之间靠 `BatchState`（[model_types.py:20-39](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/model_types.py#L20-L39)）传递 `hidden_states`。
- `stack_params` 把每层 `q/k/v` 先 `cat` 成 qkv、再跨层 `stack`，连同 o_proj、两个 layernorm、up/gate/down 共 7 组，统一堆成第 0 维为层数的大张量（[llama.py:713-777](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L713-L777)）。
- 堆叠的收益：megakernel 的全局张量指针数从 O(层数) 降到固定 9 个；"选哪一层"从分支/查表退化成 `layer_idx` 的一次整型索引；同类权重在显存连续，利于批量搬运。
- `setup_caches` 把所有层 KV cache 合成两张 `[L, B, S, Nkv, d]` 大张量，每层 `self_attn.kv_cache` 指向其切片（[llama.py:552-574](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L552-L574)）。
- `from_pretrained` 串起全流程：读 HF 配置 → 元设备建图 → `load_from_safetensors`（改名表 + TP 切分 + 严格装入）→ 只搬设备不改 dtype → `stack_params` → `setup_caches`（[llama.py:583-638](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L583-L638)）。

## 7. 下一步学习建议

- **看消费侧**：精读 [demos/latency/scheduler.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py) 的 `make_globals` 与 `build`，看 `stacked_params` / `stacked_kv_cache` 如何被翻译成一串带 `layer_idx` 的指令——这是把本讲的"模型"接到 U1·L2 "指令"的桥梁。
- **进调度器**：进入"指令与调度"专题，读 [megakernels/scheduler.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py) 与 [demos/latency/instructions.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/instructions.py)，理解一个 `LlamaBlock` 如何被拆成 `LayerNorm_QKV_MatVecRopeAppend` / `PartialAttention` / `O_ProjResidual` / `LayerNormDoubleMatVecSiLU` 等指令。
- **对照运行**：把 `mode=torch`（走本讲的 PyTorch 前向）与 `mode=mk`（走堆叠后的 megakernel）并排跑（见 [scripts/generate.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py) 与 [scripts/diff_test.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/diff_test.py)），验证"堆叠前后数值一致"——这正是 `stack_and_reassign` 里那次 `[:] =` 回写存在的意义。
- **TP 进阶**：若关心多卡，重读 [llama.py:665-691](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L665-L691) 的 `make_tp_map` 与 [llama.py:50-87](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/llama.py#L50-L87) 的 `all_gather` / `reduce_scatter`，理解行/列并行在投影前后的收发时机。
