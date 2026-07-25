# 默认解码器模型实现

## 1. 本讲目标

上一讲（u2-l2）我们看到：`AutoModel.from_pretrained` 会按 `model_type` 查注册表，**查不到就回退到默认的 `CausalLM`**。本讲就钻进这个「默认 `CausalLM`」的内部，搞清楚它是怎么用 EdgeLLM 自己的自定义算子**从零搭出**一个标准 Transformer 解码器的。

学完后你应当能够：

1. 说清 `CausalLM` → `Transformer` → `DecoderLayer` → `Attention` / `MLP` 的组装层次，以及一次 forward 里算子的调用顺序。
2. 区分 `linear.py` 里 `FP16Linear` 与各类量化线性层（FP8 / NVFP4 / AWQ / GPTQ / INT8-SQ）在**权重布局**和**前向路径**上的本质区别，理解 `make_linear` 工厂如何按量化类型分发。
3. 解释 `ops.py` 里的 `torch.library.custom_op` 是「trace 期占位算子」，理解它为何返回零张量、为何要配 `register_fake`，以及它和最终 C++ 插件/算子的契约关系。
4. 理解一个核心设计取舍：**为什么从检查点权重直接构建 `nn.Module`，而不是去 trace HuggingFace 的 FX 图**。

## 2. 前置知识

- **检查点 / 权重 key**：HuggingFace 风格的 `safetensors` 里，每个权重张量都有一个字符串 key，例如 `model.layers.0.self_attn.q_proj.weight`。EdgeLLM 的模型类刻意让**子模块名与这些 key 一一对应**，加载时按名字对号入座（详见 u2-l4）。
- **GQA（Grouped-Query Attention）**：Query 头数 `num_attention_heads` 多于 KV 头数 `num_key_value_heads`，多个 Q 头共享同一组 K/V，省 KV 缓存显存。Llama / Qwen 系列普遍采用。
- **RoPE（旋转位置编码）**：本讲里 RoPE 不在 `Attention` 内部算，而是由上层把预算好的 `rope_rotary_cos_sin` 表传进来，最终在 `attention_plugin` 算子内部施加。
- **RMSNorm**：比 LayerNorm 少减一个均值，只做方差归一，公式见 4.1.2。
- **SwiGLU**：现代解码器 MLP 的标准激活，`down(silu(gate(x)) * up(x))`。
- **trace / 导出**：把一个 `nn.Module` 的前向过程记录成一张静态计算图（ONNX）。EdgeLLM 用 `torch.onnx.export(dynamo=True)` 导出，详见 u2-l5。
- **张量并行（TP）**：把一个大 Linear 的权重沿「输出维」（列并行）或「输入维」（行并行）切到多卡。本讲末尾会点到 `tp_mode`，深入留到运行时相关讲义。

如果你对「自定义算子为什么是占位符」这一点感到陌生，别急——4.3 会专门讲清楚，这是理解整个 Python 导出前端的钥匙。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲角色 |
| --- | --- | --- |
| `tensorrt_edgellm/models/default/modeling_default.py` | 默认解码器全部模块：`RMSNorm` / `Attention` / `MLP` / `DecoderLayer` / `Transformer` / `CausalLM`，外加 ONNX 导出包装 | 主角，4.1 全篇精读 |
| `tensorrt_edgellm/models/linear.py` | 一族线性层：`FP16Linear` 与 FP8/NVFP4/AWQ/GPTQ/INT8-SQ 量化层，TP 并行外壳，`make_linear` 工厂 | 4.2 精读，理解量化分支 |
| `tensorrt_edgellm/models/ops.py` | 一堆 `torch.library.custom_op` 占位算子（`attention_plugin`、`int4_groupwise_gemm`、`gather_nd` 等）+ 各自的 `register_fake` | 4.3 精读，理解 trace 期契约 |

辅助文件（只引用、不深入）：

- `tensorrt_edgellm/config.py` 中的 `ModelConfig`（u2-l1 已讲）与 `module_quant_type`（`config.py:393`），是 `make_linear` 决定用哪种线性的依据。
- `tensorrt_edgellm/model.py:203` 处的 `from .models.default.modeling_default import CausalLM`，即 u2-l2 里「回退默认类」的那一行。

## 4. 核心概念与源码讲解

### 4.1 CausalLM：默认解码器的总装

#### 4.1.1 概念说明

`CausalLM` 是 EdgeLLM 对「一个标准 decoder-only 语言模型」的实现。它的结构对学过 Llama/Qwen 的人会很眼熟：

```
CausalLM
├── model: Transformer
│   ├── embed_tokens   (nn.Embedding)
│   ├── layers: [DecoderLayer] × num_hidden_layers
│   │   ├── input_layernorm          (RMSNorm)
│   │   ├── self_attn: Attention     (GQA + 自定义 attention 算子)
│   │   ├── post_attention_layernorm (RMSNorm)
│   │   └── mlp: MLP                 (SwiGLU)
│   └── norm: RMSNorm               (最终的 final norm)
└── lm_head: Linear (hidden → vocab)
```

注意三个**刻意为之**的命名约定，它们决定了权重能否被正确加载：

1. 内层 Transformer 存在属性 `self.model` 上，所以它所有参数都带 `model.` 前缀，正好匹配检查点里的 `model.embed_tokens.weight`、`model.layers.0.*`。
2. 每个 `DecoderLayer` 的子模块名 `self_attn` / `mlp` / `input_layernorm` / `post_attention_layernorm` 与检查点 key 完全一致（见模块顶部注释 [modeling_default.py:31-36](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/default/modeling_default.py#L31-L36)）。
3. `Attention` 内部的 `q_proj/k_proj/v_proj/o_proj` 也是按检查点名命名的（[modeling_default.py:219-251](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/default/modeling_default.py#L219-L251)）。

> **为什么从权重直接构建，而不是 trace HF 模型？**
> HuggingFace 的 `forward` 里充斥着动态控制流、Python 层 if/else、与 EdgeLLM 运行时无关的临时算子。直接 trace 它，得到的图既臃肿又不可控，很难保证每个节点都能 lowering 到 EdgeLLM 的 C++ 插件/算子。
> EdgeLLM 的做法是**自己用一套「受控算子」重写解码器**：每个关键计算（attention、量化 GEMM、KV cache 更新）都对应一个 `ops.py` 里的自定义算子，这些算子最终会被翻译成 C++ 运行时认识的 ONNX 节点。这样导出的图是**确定、最小、且与运行时一一对应**的。代价是要手写一遍 forward，但换来了对最终引擎的完全掌控。

#### 4.1.2 核心流程

先看 `CausalLM.forward` 的输入输出约定（来自模块顶部 docstring [modeling_default.py:20-29](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/default/modeling_default.py#L20-L29)）：

```
输入:
  inputs_embeds        [batch, seq_len, hidden_size]            float16
  past_key_values      每个注意力层一个 [batch, 2, num_kv_heads, past_len, head_dim]
  rope_rotary_cos_sin  [batch, max_pos, rotary_dim]             float32
  context_lengths      [batch]                                  int32
  kvcache_start_index  [batch]                                  int32
  last_token_ids       [batch, num_tokens]                      int64
输出:
  logits               [batch, seq_len, vocab_size]             float32
  present_key_values   每层更新后的 KV 缓存
```

整体前向流程（伪代码）：

```
CausalLM.forward:
  hidden_states, present_kv, all_hidden = Transformer.forward(...)
  # 只取需要的 token 位置送进 lm_head（GatherND）
  selected = trt::gather_nd(hidden_states, last_token_ids)
  logits   = lm_head(selected).to(float32)
  return logits, present_kv
```

`Transformer.forward` 把 `inputs_embeds` 依次过每一层：

```
Transformer.forward:
  h = inputs_embeds
  for layer in layers:
      h, present_kv_i = layer(h, past_kv_i, rope, ctx_lens, kvcache_start, ...)
      # 多模态 deepstack：前若干层会把视觉 embedding 加到 h 上（纯 LLM 不触发）
      if 有 deepstack_embeds:  h = h + deepstack_embeds[i]
  return norm(h), 所有 present_kv, (可选 all_hidden)
```

`DecoderLayer.forward` 是标准的「残差 + 子层」结构：

```
DecoderLayer.forward:
  residual = h
  attn_out, present_kv = self_attn(input_layernorm(h), ...)
  h = residual + attn_out                  # 第一个残差

  residual = h
  h = residual + mlp(post_attention_layernorm(h))   # 第二个残差
  return h, present_kv
```

`Attention.forward` 是本讲的算子密集区：

```
Attention.forward:
  q = q_proj(h); k = k_proj(h); v = v_proj(h)
  if has_qk_norm:  q = q_norm(q); k = k_norm(k)     # 按头做 RMSNorm
  attn_out, present_kv = attention_plugin(          # ★ 自定义算子
      q, k, v, past_kv, ctx_lens, rope, kvcache_start, ...)
  attn_out = attn_out.reshape([batch, seq, num_heads*head_dim])
  return o_proj(attn_out), present_kv
```

`MLP.forward` 是 SwiGLU：

\[ \text{SwiGLU}(x) = W_{\text{down}}\Big(\text{SiLU}(W_{\text{gate}}\,x) \odot W_{\text{up}}\,x\Big) \]

其中 \(\text{SiLU}(x)=x\cdot\sigma(x)\)，\(\odot\) 是逐元素乘。

而 `RMSNorm` 的数学定义是：

\[ \text{RMSNorm}(x) = \gamma \cdot \frac{x}{\sqrt{\dfrac{1}{H}\sum_{i=1}^{H} x_i^2 + \epsilon}} \]

注意实现里先把输入升到 `float32` 算方差、再降回原 dtype，最后乘以可学习缩放 \(\gamma\)，这是数值稳定的常规做法。

#### 4.1.3 源码精读

**RMSNorm** —— 注意它先升 fp32、再降回，乘 `weight`：

[modeling_default.py:176-183](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/default/modeling_default.py#L176-L183) —— `RMSNorm.forward`：升精度算方差 → `rsqrt` → 降回原 dtype → 乘缩放。

```python
hidden_states = hidden_states.to(torch.float32)
variance = hidden_states.pow(2).mean(-1, keepdim=True)
hidden_states = hidden_states * torch.rsqrt(variance + self.variance_epsilon)
hidden_states = hidden_states.to(input_dtype)
return self.weight.to(input_dtype) * hidden_states
```

**Attention 的 QKV 投影与 GQA** —— `num_key_value_heads` 可以小于 `num_attention_heads`，K/V 投影的输出维按 KV 头数算：

[modeling_default.py:221-251](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/default/modeling_default.py#L221-L251) —— `q_proj` 输出 `num_attention_heads*head_dim`，`k_proj/v_proj` 输出 `num_key_value_heads*head_dim`，`o_proj` 把多头输出合回 `hidden_size`。注意它们都通过 `make_linear(...)` 构造，而不是直接 `nn.Linear`——这是量化能逐层生效的关键（见 4.2）。

**FP8 KV 缓存的 scale** —— 当 `config.quant.kv_cache_quant == "fp8"` 时，会在 q/k/v 投影模块上挂三个 scale buffer：

[modeling_default.py:242-245](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/default/modeling_default.py#L242-L245) —— 注册 `q_scale/k_scale/v_scale`。注释说明这些 scale 来自检查点里的 `...{q,k,v}_proj.{q,k,v}_scale`，是 KV 缓存量化专用，**不属于** `FP8Linear` 自身的 per-tensor 权重/激活 scale。

**Attention.forward 里对自定义算子的调用** —— 这是整层最关键的一步：

[modeling_default.py:308-317](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/default/modeling_default.py#L308-L317) —— `attention_plugin(...)` 一次性完成 RoPE、KV cache 写入、注意力得分、输出 reshape，返回 `[batch, seq, num_q_heads, head_size]` 和更新后的 `present_key_value`。

kwargs 里有几个布尔开关决定算子的「模式」（普通 / FP8-KV / EAGLE 树形注意力），见 [modeling_default.py:289-306](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/default/modeling_default.py#L289-L306)。注释强调：`qkv_scales` 必须显式传 `[1.0,1.0,1.0]`，否则 `torch.export` 会把默认值从 FX 图里剥掉，导致 ONNX 翻译失败——这是自定义算子导出的一个真实坑。

**MLP（SwiGLU）**：

[modeling_default.py:355-358](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/default/modeling_default.py#L355-L358) —— `down_proj( F.silu(gate_proj(x)) * up_proj(x) )`，三个投影同样是 `make_linear` 产出。

**DecoderLayer 的双残差**：

[modeling_default.py:392-406](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/default/modeling_default.py#L392-L406) —— 先对 `input_layernorm(h)` 做注意力再加残差；再对 `post_attention_layernorm(h)` 做 MLP 再加残差。和公式完全对应。

**Transformer 的层循环与多模态挂点**：

[modeling_default.py:459-480](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/default/modeling_default.py#L459-L480) —— 逐层 forward，收集每层更新后的 KV；并在 `layer_index < len(deepstack_embeds)` 时把视觉 embedding 加进残差流（纯 LLM 时 `deepstack_embeds` 为空，不触发）。这说明**默认解码器天然为多模态留了接入点**，多模态细节留到 u6。

**CausalLM 的 lm_head 与 token 选择**：

[modeling_default.py:746-749](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba233c9315b89d8e47d6/tensorrt_edgellm/models/default/modeling_default.py#L746-L749) —— 用 `torch.ops.trt.gather_nd` 按 `last_token_ids` 抽取需要的 token 位置，再过 `lm_head`。注释说明这是为了让 ONNX 导出产生原生 `GatherND(batch_dims=1)` 而非 `GatherElements`，因为 TRT 对 GatherND 有原生支持。

[modeling_default.py:526-536](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/default/modeling_default.py#L526-L536) —— `CausalLM.__init__`：`self.model = Transformer(config)`、`self.lm_head = make_linear(..., module_name="lm_head")`。`module_name` 会被 `module_quant_type` 用来决定 `lm_head` 该用什么精度（例如 backbone 用 NVFP4、lm_head 仍可用 FP16）。

**权重绑定（tie_weights）**：

[modeling_default.py:538-557](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/default/modeling_default.py#L538-L557) —— 当 `tie_word_embeddings=True` 时，把 `embed_tokens.weight` **克隆**（而非共享）到 `lm_head.weight`。注释强调必须克隆、不能共享，否则 ONNX 导出会出错；且若 `lm_head` 已是量化模块（如 FP8）则跳过，因为量化模块自带权重。

#### 4.1.4 代码实践

> 实践目标：定位「一层 Transformer」的 forward 组装，画出每一层用到的算子调用顺序。

这是**源码阅读型实践**（无需 GPU）。

1. **操作步骤**：
   - 打开 [modeling_default.py:382-408](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/default/modeling_default.py#L382-L408)（`DecoderLayer.forward`）。
   - 顺着它进入 `self.self_attn`（[L260-322](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/default/modeling_default.py#L260-L322)）和 `self.mlp`（[L355-358](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/default/modeling_default.py#L355-L358)）。
2. **需要观察的现象**：一层解码器内部，标准算子（`F.linear`、`F.silu`、加法残差、RMSNorm）与 EdgeLLM 自定义算子（`attention_plugin`）是**混合出现**的。
3. **画出调用顺序**（参考答案，可直接核对）：

   ```
   DecoderLayer.forward(h):
     1. input_layernorm(h)            -> RMSNorm: pow/mean/rsqrt/mul  (标准算子)
     2. self_attn:
        2a. q_proj / k_proj / v_proj  -> make_linear 产出 (FP16Linear 或量化层)
        2b. (可选) q_norm / k_norm    -> RMSNorm (按 head_dim)
        2c. attention_plugin(...)     -> ★自定义算子: RoPE + KV写入 + 注意力
        2d. o_proj                    -> make_linear
     3. h = residual + attn_out        (加法残差)
     4. post_attention_layernorm(h)   -> RMSNorm
     5. mlp:
        5a. gate_proj / up_proj       -> make_linear
        5b. F.silu(gate) * up         (SiLU + 逐元素乘)
        5c. down_proj                 -> make_linear
     6. h = residual + mlp_out        (加法残差)
   ```

4. **预期结果**：你会发现除了 `attention_plugin` 这一个自定义算子外，其余都是 PyTorch 标准算子——这正是 EdgeLLM 「只把必须 lowering 到专用 kernel 的部分做成自定义算子，其余让 TRT 自行处理」的体现。

> 待本地验证（可选）：若你装好了 `tensorrt_edgellm`，可用 `ModelConfig.from_pretrained` 读一个真实 Qwen/Llama 检查点，再 `CausalLM(config)` 打印模型结构，对照上面每层的子模块名是否与检查点 key 一致。本讲不假定你已运行此步。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `embed_tokens` 是 `nn.Embedding`，而 `lm_head` 却走 `make_linear`？

> **答案**：`embed_tokens` 的权重布局是标准 Embedding 表（`[vocab, hidden]`），不需要量化 GEMM 专用路径，直接 `nn.Embedding` 即可，且它的权重常常与 `lm_head` 绑定（`tie_weights`）。而 `lm_head` 需要支持「backbone 量化但 lm_head 不量化」等组合（由 `module_quant_type("lm_head", config)` 决定），所以必须走 `make_linear` 才能按需切换成 FP16/FP8/INT4 等不同线性层。

**练习 2**：`Attention.forward` 里 `attn_output` 在送进 `o_proj` 前做了一次 `reshape`（[L319-320](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/default/modeling_default.py#L319-L320)），为什么？

> **答案**：`attention_plugin` 返回的是 4-D `[batch, seq_len, num_heads, head_dim]`，而 `o_proj` 是一个普通 Linear，期望输入最后一维是 `num_heads*head_dim`。所以要把后两维拍平。这也呼应了 4.3 里「自定义算子返回什么形状，由它自己的 `register_fake` 决定」。

**练习 3**：`tie_weights` 里用 `detach().clone()` 而不是直接赋值 `embed_tokens.weight`，目的是什么？

> **答案**：让 `lm_head.weight` 和 `embed_tokens.weight` 成为**两个独立的张量**。若共享同一份内存，ONNX 导出时二者会被当作同一节点，破坏导出图的独立性；克隆后二者各自独立，导出才能正确产出 `lm_head` 对应的 MatMul 节点。

---

### 4.2 linear：统一线性层与量化分支

#### 4.2.1 概念说明

`linear.py` 解决一个问题：**同一个 `make_linear(...)` 调用，如何根据该模块的量化类型，返回结构各异的线性层？**

答案是一个**两级分发**：

1. `module_quant_type(module_name, config)` 先算出这个具体模块（如 `lm_head`、`layers.0.self_attn.q_proj`）到底用什么量化格式（`fp16` / `fp8` / `nvfp4` / `int4_awq` / `int4_gptq` / `int8_sq` / ...）。这是 u2-l1 讲过的「某个 Linear 最终精度的唯一真相来源」。
2. `make_linear` 再按这个结果 `if/elif` 选出对应的类实例化。

不同格式线性层的差异，本质是**两件事**：

- **权重张量的内存布局不同**：FP16 是 `[out,in]` 的 fp16；FP8 是 `[out,in]` 的 fp8 + 标量 scale；AWQ 是列打包的 `qweight [in, out//8]` int32；GPTQ 是行打包的 `qweight [in//8, out]` int32；NVFP4 是每字节 packed 两个 fp4 nibble。布局概览见模块顶部注释 [linear.py:21-26](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/linear.py#L21-L26)。
- **前向路径不同**：有的走「DQ 权重 → 标准 MatMul」（让 TRT 做 Q-DQ-MatMul 融合），有的走「专用量化 GEMM 算子」（如 INT4 的 `int4_groupwise_gemm`）。

#### 4.2.2 核心流程

`make_linear` 的决策流程（伪代码）：

```
make_linear(config, in, out, bias, module_name, tp_mode):
  qt = module_quant_type(module_name, config)     # 唯一真相
  if qt == nvfp4:        # NVFP4 走"组合式"设计
      method = NVFP4LinearMethod(group_size)
      return Replicated/Column/Row ParallelLinear(..., method)   按 tp_mode
  if qt == fp16:  layer = FP16Linear(in, out, bias)
  elif qt == fp8:  layer = FP8Linear(...)
  elif qt == mxfp8: layer = MXFP8Linear(...)
  elif qt == int4_awq:          layer = AWQLinear(...)
  elif qt == int4_awq_modelopt: layer = ModelOptAWQPrepackedLinear(...)
  elif qt == int4_gptq:         layer = GPTQLinear(...)
  elif qt == int8_sq:           layer = INT8SQLinear(...)
  layer.tp_mode = tp_mode (仅 tp_size>1 时生效)
  return layer
```

注意 `module_quant_type` 永远返回一个**具体的量化字符串**（如 `fp8`），绝不会返回 `mixed_precision`——混合精度的逐层差异已经在它内部展平了（u2-l1）。

`FP16Linear` 的前向就是标准 `F.linear`：

\[ y = x W^\top + b \]

而量化层大致分两类前向模式：

- **「DQ + 标准 MatMul」型**（FP8 / MXFP8 / INT8-SQ）：先把激活和权重都 dequantize 回 fp16，再调用 `F.linear`。这样导出图里是标准 ONNX `QuantizeLinear`/`DequantizeLinear` + `MatMul`，TRT 会在编译期把它们融合成高效的量化 GEMM。INT8-SQ 的模式见 [linear.py:666-671](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/linear.py#L666-L671) 注释里的图示。
- **「专用 GEMM 算子」型**（AWQ / GPTQ / NVFP4）：权重保持压缩布局（int32/int8/packed-fp4），直接喂给专用算子（`int4_groupwise_gemm`、`fused_nvfp4_gemm_allreduce`），由 TRT 插件在压缩域里做矩阵乘。

#### 4.2.3 源码精读

**FP16Linear —— 最朴素的对照基准**：

[linear.py:144-171](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/linear.py#L144-L171) —— 权重 `nn.Parameter(torch.empty(out, in, dtype=float16), requires_grad=False)`，前向 `_require_fp16_input` 校验后直接 `F.linear`。注意它**要求输入必须是 fp16**（不支持 bf16/fp32），这是全包的一致约定。

```python
def forward(self, hidden_states):
    _require_fp16_input(hidden_states, "FP16Linear")
    bias = self.bias if self.bias is not None else None
    return F.linear(hidden_states, self.weight, bias)
```

**FP8Linear —— DQ + MatMul 型的典型**：

[linear.py:210-218](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/linear.py#L210-L218) —— 激活先 `fp8_quantize` 再 `fp8_dequantize`（导出为 `QuantizeLinear`+`DequantizeLinear`），权重 `fp8_dequantize` 回 fp16，最后 `F.linear`。`weight_scale`/`input_scale` 用 0-d 标量（`shape=[]`）以匹配 ModelOpt 的统一检查点布局（[linear.py:201-204](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/linear.py#L201-L204)）。

**AWQLinear —— 专用 GEMM 型 + 列打包**：

[linear.py:484-514](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/linear.py#L484-L514) —— 权重以 `qweight [in, out//8]` int32 存储（8 个 int4 打包进一个 int32），前向调用 `int4_groupwise_gemm`，把压缩权重、group scale 直接交给算子。注释点明 loader 之后会把布局 repack 成 `[out//2, in]` int8（插件要求的 swizzled 布局）。

**NVFP4 —— 组合式设计（LinearMethodBase）**：

[linear.py:226-293](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/linear.py#L226-L293) —— NVFP4 没有自己的 `NVFP4Linear` 类，而是用 `NVFP4LinearMethod` 描述「buffer 怎么分配、前向怎么算、TP 怎么切」，再由 `ColumnParallelLinear` / `RowParallelLinear` / `ReplicatedLinear` 这三个外壳类组合进来。这是给将来扩展量化格式留的「方法对象」模式：`LinearMethodBase`（[linear.py:106-136](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/linear.py#L106-L136)）定义了 `create_weights` / `apply` / `apply_linear_allreduce` / `shardable_attrs` 四个抽象方法。

**ColumnParallelLinear / RowParallelLinear —— TP 外壳**：

[linear.py:301-340](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/linear.py#L301-L340) —— 列并行沿 dim0 切输出通道、前向无需通信；`tp_split_dim` 告诉 loader 哪些 buffer 要切。

[linear.py:363-394](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/linear.py#L363-L394) —— 行并行沿 dim1 切输入通道、前向需要 AllReduce（`apply_linear_allreduce`）。注意 `RowParallelLinear.forward` 调的是 `apply_linear_allreduce`，而列并行调的是 `apply`——所以 NVFP4 行并行会走融合了 GEMM+AllReduce 的 `fused_nvfp4_gemm_allreduce` 算子。

**make_linear 工厂**：

[linear.py:713-785](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/linear.py#L713-L785) —— 先 `module_quant_type` 取精度，再按 `if/elif` 选类。NVFP4 单独走组合式分支；其余量化类型各自实例化具体类；最后给非 NVFP4 的层打上 `tp_mode` 标签，让 loader 知道加载时是否要切分（[linear.py:783-784](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/linear.py#L783-L784)）。

#### 4.2.4 代码实践

> 实践目标：对比 `FP16Linear` 与量化线性层在 `linear.py` 中的区别。

1. **操作步骤**：
   - 读 [FP16Linear.forward](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/linear.py#L168-L171)（3 行）。
   - 读 [FP8Linear.forward](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/linear.py#L210-L218)（DQ + MatMul 型）。
   - 读 [AWQLinear.forward](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/linear.py#L502-L514)（专用 GEMM 型）。
   - 读 [INT8SQLinear 的布局注释](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/linear.py#L657-L672)（给出了完整的 ONNX Q-DQ-MatMul 模式图）。
2. **需要观察的现象**：四类线性层的「权重 buffer 名字」「权重 dtype/形状」「前向最后一行调用」都不同。
3. **填写下表（参考答案）**：

   | 类 | 权重 buffer | 权重 dtype/形状 | 前向落点 |
   | --- | --- | --- | --- |
   | `FP16Linear` | `weight` | fp16 `[out,in]` | `F.linear` |
   | `FP8Linear` | `weight` + `weight_scale` + `input_scale` | fp8 `[out,in]` + fp16 标量 | DQ 后 `F.linear` |
   | `AWQLinear` | `qweight`/`qzeros`/`scales` | int32 `[in,out//8]` 等 | `int4_groupwise_gemm` |
   | `INT8SQLinear` | `weight`/`weight_scale`/`input_scale`/`pre_quant_scale` | int8 `[out,in]` + fp32 `[out]` | QDQ 后 `F.linear`（可被 TRT 融合） |

4. **预期结果**：你能归纳出两类前向模式——「DQ+MatMul」（FP8/MXFP8/INT8-SQ，依赖 TRT 融合）与「专用量化 GEMM」（AWQ/GPTQ/NVFP4，依赖自定义算子）。

#### 4.2.5 小练习与答案

**练习 1**：`FP16Linear` 和 `FP8Linear` 的前向**最后一行都是 `F.linear`**，那它们的 ONNX 图区别在哪？

> **答案**：区别在 `F.linear` **之前**。FP8 在权重和激活上各插了 `DequantizeLinear`（权重）/`QuantizeLinear+DequantizeLinear`（激活），所以导出图是 `Q-DQ-MatMul` 结构，TRT 编译时能融合成 FP8 GEMM；FP16 则是裸 `MatMul`。

**练习 2**：为什么 NVFP4 不像别的格式那样写一个 `NVFP4Linear` 类，而要用 `NVFP4LinearMethod` + 并行外壳的组合？

> **答案**：因为 NVFP4 要同时支持「列并行（无通信）」和「行并行（带 AllReduce）」两种 TP 模式，且行并行有专用的融合算子 `fused_nvfp4_gemm_allreduce`。把「量化逻辑」抽成 `LinearMethodBase`、把「TP 通信策略」留给 `Column/RowParallelLinear` 外壳，两者正交组合，避免为每种「量化×并行」组合都写一个类。这是可扩展性的设计。

**练习 3**：所有量化线性层的 `forward` 第一行都是 `_require_fp16_input(...)`，为什么统一要求 fp16 输入？

> **答案**：整个 EdgeLLM 解码器的残差流（hidden_states）默认在 fp16 上流转（见 `CausalLM` 的输入约定）。量化发生在每个 Linear **内部**（激活动态量化、权重反量化），对外暴露的接口仍是 fp16。统一校验能在开发期尽早发现「某处混入 bf16/fp32」的错误，而不是等导出或运行时才报难以定位的形状/精度错。

---

### 4.3 ops：trace 期占位自定义算子

#### 4.3.1 概念说明

`ops.py` 是理解整个 Python 导出前端的**钥匙**。它定义了几十个自定义算子，但它们有一个反直觉的共同点：

> **这些算子的 Python 函数体在导出时根本不会被执行**——它们只负责「告诉导出器：这里有一个算子，它的输入输出形状是这样」，真正的计算由 C++ 运行时的 TRT 插件/算子完成。

这是用 `torch.library.custom_op` 实现的标准手法，模块 docstring 说得很直白 [ops.py:15-22](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/ops.py#L15-L22)：

> Each op is a trace-time dummy (returns zero tensors of the correct shape/dtype) paired with a `register_fake` for shape propagation in the dynamo exporter.
> Domains `trt::` / `trt_edgellm::` map to ONNX nodes consumed by the TensorRT plugin runtime.

每个自定义算子由两部分组成：

1. **`@custom_op` 装饰的函数体**：eager 模式下被调用时返回**正确形状、值为零**的张量（少数算子有真实的 eager 实现，用于数值校验 golden，见下）。
2. **`@xxx.register_fake`**：dynamo 导出时被调用来做**形状传播**——导出器只关心形状/dtype，不执行函数体。

导出阶段，这些 `trt::` / `trt_edgellm::` 域的算子会被 u2-l5 的 `dynamo_translations.py` 翻译成对应的 ONNX 节点，最终由 C++ 插件消费。**算子名（如 `trt::attention_plugin`）就是 Python 侧与 C++ 侧之间的契约**。

#### 4.3.2 核心流程

以最核心的 `attention_plugin` 为例，它的生命周期是：

```
建模期 (modeling_default.py):
  Attention.forward 调用 attention_plugin(q,k,v,...)   # 返回零张量(形状正确)

导出期 (torch.onnx.export dynamo=True):
  导出器遇到 attention_plugin -> 调 register_fake 做形状传播
  -> dynamo_translations.py 把它翻译成 ONNX 的 AttentionPlugin 节点

运行期 (C++ runtime):
  TRT 引擎执行 AttentionPlugin -> 调用 cpp/plugins 里的真实 CUDA kernel
```

`attention_plugin` 是一个**统一算子**，用一个签名覆盖四种模式（见其 docstring 的特征矩阵 [ops.py:107-117](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/ops.py#L107-L117)）：

| 模式 | enable_tree_attention | enable_fp8_kv_cache |
| --- | --- | --- |
| 普通 | False | False |
| FP8 KV 缓存 | False | True (qkv_scales 已设) |
| EAGLE 树形注意力 | True | False |
| EAGLE + FP8 KV | True | True (qkv_scales 已设) |

四种模式都映射到同一个 TRT `AttentionPlugin`，靠布尔开关区分——这就是为什么 `Attention.forward`（4.1）里要传那一堆 kwargs。

**一个重要细节：为什么有些算子的 eager 体不是零？**

像 `fp8_quantize` / `nvfp4_dequantize` / `mxfp8_*` / `int8_sq_*` 这些算子，eager 体**真的实现了 fake-quant 计算**（见 [ops.py:355-378](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/ops.py#L355-L378) 的 FP8 helpers）。注释解释：这是给「数值校验 golden」用的——当用 PyTorch eager 跑模型得到一份参考输出时，需要这些算子真正算出量化后的值，而不是零。但**导出时**依然走 `register_fake`、发射 ONNX 节点，eager 体不影响导出（见 [ops.py:386-391](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/ops.py#L386-L391)）。这是「同一份算子，两种用途」的精巧设计。

#### 4.3.3 源码精读

**attention_plugin 的定义**：

[ops.py:82-157](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/ops.py#L82-L157) —— `@torch.library.custom_op("trt::attention_plugin", mutates_args=())`。函数体（[L142-157](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/ops.py#L142-L157)）只是用输入形状算出两个零张量：`attn_output [batch, seq, num_q_heads, head_size]` 和 `present_key_value [batch, 2, num_kv_heads, past_len+seq_len, head_dim]`。注意 `past_len + seq_len`——这告诉导出器「KV 缓存长度会增长」，正是 prefill→decode 的形状契约。

**register_fake**：

[ops.py:160-193](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/ops.py#L160-L193) —— 用 `torch.empty(...)`（未初始化）返回同形状张量。dynamo 导出时调用它做形状传播，函数体被跳过。这是「占位算子」的标准写法。

**int4_groupwise_gemm（专用量化 GEMM）**：

[ops.py:690-713](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/ops.py#L690-L713) —— AWQ/GPTQ 线性层（4.2）前向最终调的就是它。注释写明输入约定：`qweight [out//2, in]` int8（swizzled 插件布局）、`scales [in//group_size, out]` fp16。返回 `[*, out]` 零张量。

**gather_nd（token 选择）**：

[ops.py:975-1001](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/ops.py#L975-L1001) —— `CausalLM.forward` 里 `last_token_ids` 选 token 用的就是它（4.1.3）。导出为 `GatherND(batch_dims=1)`，等价于 `value[b, indices[b,t], :]`。

**FP8 eager helper（数值 golden 用）**：

[ops.py:355-378](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/ops.py#L355-L378) —— `_fp8_quantize_eager` 真的做了 `x/scale → 饱和到 E4M3 最大值 448 → 转 fp8 → 转 fp16`。常量 `_FP8_E4M3_MAX = 448.0`（[ops.py:352](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/ops.py#L352)）对应 `float8_e4m3fn` 没有无穷大、溢出会变 NaN，所以要先饱和。这是为了让 golden 匹配硬件 kernel 的行为。

**算子域的命名约定**：

通读文件会发现两类命名空间：
- `trt::` —— 期望最终落到 **TRT 原生节点**或标准 ONNX 算子（如 `trt::fp8_quantize` → `QuantizeLinear`，`trt::attention_onnx` → TRT 原生 Attention）。
- `trt_edgellm::` —— 落到 **EdgeLLM 自定义插件**（如 `trt_edgellm::int4_moe_plugin`、`trt_edgellm::gated_delta_net`）。

这个域名是给 u2-l5 的翻译规则做路由用的：看到 `trt_edgellm::` 就知道要去找对应的 C++ 插件。

#### 4.3.4 代码实践

> 实践目标：体会「同一个算子名，建模期返回零、导出期只看形状」的契约。

1. **操作步骤**：
   - 打开 [attention_plugin 定义](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/ops.py#L82-L157)，确认它的函数体只 `torch.zeros(...)`。
   - 打开对应的 [register_fake](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/ops.py#L160-L193)，确认它 `torch.empty(...)` 同形状。
   - 再对比 [fp8_quantize](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/ops.py#L381-L396)：它的函数体**真的算了**（调 `_fp8_quantize_eager`）。
2. **需要观察的现象**：`attention_plugin` 的体是「假」的（零），`fp8_quantize` 的体是「真」的（fake-quant）。
3. **预期结果 / 结论**：你应能说出——
   - 「假体」算子（attention、gemm、moe、gather_nd 等）：纯导出占位，运行期由 C++ 算真值。
   - 「真体」算子（fp8/nvfp4/mxfp8/int8_sq 的 qdq）：既能在 eager 模式下当数值 golden，又能导出成 ONNX 节点；导出时不走体。
4. **跟踪调用链（选做）**：从 [Attention.forward 的 attention_plugin 调用](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/default/modeling_default.py#L308-L317) 出发 → 进入 `ops.py` 的 `attention_plugin` → 想象它在 u2-l5 的 `dynamo_translations.py` 里被翻译 → 最终在 u8 的 C++ `attentionPlugin` 里执行。这条链跨越了 Python 建模、ONNX 翻译、C++ 运行三段，是整个项目的主干。

> 待本地验证（可选）：在装好 `torch` 的环境里 `import tensorrt_edgellm.models.ops as ops`，构造小张量后 `ops.attention_plugin(q,k,v,past,...)` 直接调用，会得到**全零**输出——这正是「占位」的直接证据。本讲不假定你已运行。

#### 4.3.5 小练习与答案

**练习 1**：既然 `attention_plugin` 的函数体返回零，那为什么模型在 eager 模式下跑不出有意义的结果？

> **答案**：因为它本来就是**只为导出**设计的占位算子。eager 模式下它的输出是零，所以整模型 eager 前向得到的 logits 也是无意义的。只有导出成 ONNX、编译成 TRT engine 后，C++ 的 `AttentionPlugin` 才会填上真实计算。需要 eager 数值校验的场景，使用的是另一类「真体」算子（如 fp8 qdq）。

**练习 2**：`register_fake` 和 `@custom_op` 函数体，导出器到底用哪个？

> **答案**：dynamo 导出器用 `register_fake` 做**形状/dtype 传播**（决定后续算子的输入形状），**不执行** `@custom_op` 函数体。函数体只在 eager 直接调用时才跑。所以「形状对不对」看 `register_fake`，「导出成什么节点」看翻译规则，「真值」看 C++ 运行时。

**练习 3**：为什么 `attention_plugin` 把「普通/FP8-KV/EAGLE树形」四种模式合并成一个算子，而不是写四个？

> **答案**：减少 ONNX 图里算子种类和翻译规则的爆炸。一个统一算子 + 几个布尔属性（`enable_tree_attention` / `enable_fp8_kv_cache`）就能让翻译规则只写一份，C++ 侧也只维护一个 `AttentionPlugin` 内部按属性分支。这也是为什么 `Attention.forward` 要**显式**传所有布尔开关（注释 [ops.py:119-124](https://github.com/NVIDIA/TensorRT-Edge-LLM/blob/7f061f21f0a581ba234a1e233c9315b89d8e47d6/tensorrt_edgellm/models/ops.py#L119-L124) 警告：带默认值的 kwarg 会被 `torch.export` 从图里剥掉，必须设为必填）。

---

## 5. 综合实践

把三个最小模块串起来，做一次「**追踪一个 Linear 从配置到算子的完整一生**」。

**场景**：假设某检查点的 backbone 是 NVFP4 量化、但 `lm_head` 不量化；某层的 `self_attn.q_proj` 是列并行 NVFP4、`o_proj` 是行并行 NVFP4；`mlp.gate_proj` 是列并行 NVFP4。请按下面的步骤把链路画出来。

1. **配置侧**（承接 u2-l1/u2-l2）：
   - `ModelConfig.from_pretrained(...)` 读出架构与 `QuantConfig`。
   - 确认 `module_quant_type("layers.0.self_attn.q_proj", config)` 返回 `nvfp4`，而 `module_quant_type("lm_head", config)` 返回 `fp16`。
2. **建模侧**（本讲 4.1 + 4.2）：
   - `CausalLM.__init__` → `Transformer.__init__` → 每个 `DecoderLayer.__init__` → `Attention.__init__`。
   - `q_proj = make_linear(..., tp_mode=TPMode.COL)` → 因为 `qt==nvfp4` 且列并行 → 返回 `ColumnParallelLinear` + `NVFP4LinearMethod`。
   - `o_proj = make_linear(..., tp_mode=TPMode.ROW)` → 返回 `RowParallelLinear`，前向走 `apply_linear_allreduce`（融合 GEMM+AllReduce）。
   - `lm_head = make_linear(..., module_name="lm_head")` → `qt==fp16` → 返回 `FP16Linear`。
3. **算子侧**（本讲 4.3）：
   - `q_proj.forward` → `NVFP4LinearMethod.apply` → `nvfp4_act_qdq` + `nvfp4_dequantize` + `F.linear`（或行并行的 `fused_nvfp4_gemm_allreduce`）。
   - 这几个 `trt::`/`trt_edgellm::` 算子在导出期由 `register_fake` 做形状传播，运行期由 C++ 插件算真值。
4. **产出**：画一张图，标出 `q_proj` 的权重布局（`weight [out, in//2]` int8 packed fp4）、前向经过的算子序列、以及每个算子最终落到哪类 C++ 实现上。

**验收标准**（自检）：
- 能说清为什么 `q_proj` 是 `ColumnParallelLinear` 而 `o_proj` 是 `RowParallelLinear`（提示：投影后的维度切分方向不同，对应 Q 头切分 / 输出合回）。
- 能指出 `attention_plugin` 是这一层里**唯一**的非标准算子（其余都是 `F.linear`/`F.silu`/加法/RMSNorm）。
- 能解释「导出时函数体不执行」为什么不影响最终推理正确性。

> 说明：本综合实践为源码阅读型，无需 GPU；若要真正验证数值，需走完 export→build→inference 三步（u1-l5 / u4 / u5）。

## 6. 本讲小结

- `CausalLM` 是 EdgeLLM 对标准 decoder-only 模型的**手写实现**：`Transformer`（embed_tokens + N×DecoderLayer + norm）+ `lm_head`，子模块名刻意与检查点 key 对齐以便按名加载。
- 一层 `DecoderLayer` = 两个残差块：`Attention`（GQA + 自定义 `attention_plugin`）+ `MLP`（SwiGLU），Norm 一律用 `RMSNorm`。
- `make_linear` 是量化的统一入口：先由 `module_quant_type` 决定该模块的精度，再 `if/elif` 分发到 `FP16Linear` / `FP8Linear` / `AWQLinear` / `GPTQLinear` / `INT8SQLinear` 或 NVFP4 的组合式（`LinearMethodBase` + 列/行并行外壳）。
- 量化线性层分两种前向模式：「DQ + 标准 MatMul」（依赖 TRT Q-DQ-MatMul 融合）与「专用量化 GEMM」（调 `int4_groupwise_gemm` / `fused_nvfp4_gemm_allreduce`）。
- `ops.py` 的自定义算子是 **trace 期占位**：函数体返回零张量（或数值 golden）、`register_fake` 负责形状传播，真正的计算交给 C++ 插件；这是「从权重直接构建而非 trace HF 图」这条设计路线的基石。

## 7. 下一步学习建议

- **u2-l4（检查点加载与权重重排）**：本讲反复强调「子模块名与 key 对齐」，下一步就去看 loader 如何把 `safetensors` 里的权重按名字、按 TP 分片、按量化格式 repack 进这些线性层的 buffer。
- **u2-l5（ONNX 导出）**：本讲只讲到「自定义算子会被翻译成 ONNX 节点」，下一讲去看 `dynamo_translations.py` 具体怎么把 `trt::attention_plugin` / `trt_edgellm::int4_groupwise_gemm` 翻译成 ONNX 节点，以及 `onnx_custom_schemas.py` 如何注册 schema。
- **u3（量化）**：想深入理解 `FP8Linear` / `AWQLinear` / `NVFP4LinearMethod` 各自的权重布局是怎么被「量化阶段」产出的，去读 `quantization/` 包。
- **u8（插件与算子）**：想知道 `attention_plugin` / `int4_groupwise_gemm` 在 C++ 侧到底怎么算，去看 `cpp/plugins` 与 `cpp/kernels`。
