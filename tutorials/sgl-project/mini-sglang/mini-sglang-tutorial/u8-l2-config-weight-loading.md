# 模型配置解析与权重加载分片

## 1. 本讲目标

学完本讲后，你应该能够：

1. 说清 `ModelConfig.from_hf` 如何把各家（Llama / Qwen / Mistral / 多模态外壳）格式各异的 HuggingFace 配置，归一化成 Mini-SGLang 内部统一的 `ModelConfig`。
2. 解释 `_shard_tensor` 对 dim0（列并行）、dim1（行并行）、vocab（词表并行）三类权重采用的三种切分方式，以及 GQA 下 `num_kv_heads < tp_size` 时的「头复制」处理。
3. 复述 `_MERGE_GROUPS` 如何把 HF 里分散的 `q_proj/k_proj/v_proj`、`gate_proj/up_proj` 在加载时合并成运行时的 `qkv_proj`、`gate_up_proj`。
4. 描述 MoE 专家权重如何被 `_get_expert_stack_info` 按 `expert_idx` 堆叠成 `(num_experts, ...)` 的打包张量。

本讲是上一讲（u8-l1，BaseOP 与 Llama 模型结构）的直接续篇：那里讲了模型「骨架」怎么搭、`state_dict`/`load_state_dict` 怎么递归；本讲讲骨架里的权重「从哪来、怎么切、怎么拼」。

## 2. 前置知识

- **safetensors**：HuggingFace 标准的权重存储格式，按文件分片（`*.safetensors`），每个张量有名字（key），可按 key 流式读取单个张量而不必把整个文件载入内存。
- **HF config（`config.json`）**：描述模型结构的字典。不同家族字段名不同，例如 Llama 用 `num_attention_heads`，有的多模态模型把语言模型塞进 `text_config` 子对象里。
- **张量并行（TP）切分约定**（在 u9-l1 会系统讲，这里只需记住结论）：
  - **列并行（column-parallel）**：切输出的特征维（`weight` 的第 0 维，即 `out_features`）。`q/k/v/gate/up` 属于此类。
  - **行并行（row-parallel）**：切输入的特征维（`weight` 的第 1 维，即 `in_features`），输出需要在卡间 `all_reduce` 求和。`o_proj/down_proj` 属于此类。
  - **词表并行**：`embed_tokens/lm_head` 按词表行（dim0）切，但用向上取整保证不漏 token。
- **GQA（Grouped-Query Attention）**：query 头多、key/value 头少（`num_kv_heads < num_qo_heads`）。当 KV 头数少于 GPU 数时，一张卡分不到一个独立 KV 头，需要把同一个 KV 头复制到多张卡上。
- **MoE（Mixture of Experts）**：每个 token 只激活少数专家，但权重存了所有专家。运行时把 `(num_experts, ...)` 的打包权重整体喂给 fused kernel。

## 3. 本讲源码地图

本讲只涉及两个文件，它们是「配置」与「权重」这对搭档：

| 文件 | 作用 |
| --- | --- |
| [python/minisgl/models/config.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/config.py) | 定义 `RotaryConfig` 与 `ModelConfig` 两个 frozen dataclass；核心是类方法 `from_hf`，把 HF `PretrainedConfig` 翻译成内部统一配置。 |
| [python/minisgl/models/weight.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/weight.py) | 流式权重加载器 `load_weight`，以及三组切分/合并辅助：`_shard_tensor`、`_MERGE_GROUPS`/`_get_merge_info`、`_EXPERT_PATTERN`/`_get_expert_stack_info`。 |

调用关系（供承接）：[engine.py:52](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L52) 在 meta device 建好模型骨架后调用 `self.model.load_state_dict(self._load_weight_state_dict(config))`；而 `_load_weight_state_dict` 在 [engine.py:139-146](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L139-L146) 里把 `load_weight(...)` 的迭代器收成字典。也就是说，本讲讲的两个文件，是 Engine「meta 建图 + 权重替换」流程里权重那一半的来源。

## 4. 核心概念与源码讲解

### 4.1 ModelConfig.from_hf：把异构 HF config 统一化

#### 4.1.1 概念说明

不同模型家族的 `config.json` 字段名五花八门：

- Llama/Qwen 直接在顶层放 `rope_theta`；Mistral 把它藏在 `rope_scaling` 字典里。
- 有的多模态模型（如 Pixtral/Mistral3）把语言模型部分塞进 `text_config` 子对象，顶层只有视觉配置。
- 有的字段是可选的（`head_dim`、`tie_word_embeddings`、`num_key_value_heads`），有的模型干脆没有。

Mini-SGLang 的模型层（u8-l1 里的 `LlamaForCausalLM` 等）只认一套字段名——`ModelConfig`。`from_hf` 就是这座「翻译桥」：无论上游 HF config 长什么样，输出的 `ModelConfig` 字段集合是固定、强类型的。这把「适配异构 ckpt」的脏活全集中在一个类方法里，模型层代码因此可以保持干净。

#### 4.1.2 核心流程

`from_hf` 的处理可拆成三步：

1. **多模态外壳剥离**：若 config 带 `text_config`，下钻到 `config.text_config`，并把顶层缺失的 `architectures/rope_theta/rope_scaling` 补下来。
2. **字段兼容提取**：用 `getattr(config, 名字, 默认值)` 逐个取字段，缺失则用默认值兜底；少数字段有 fallback 链（如 `num_local_experts` → `num_experts`、`rope_theta` → `rope_scaling["rope_theta"]`）。
3. **组装返回**：构造 `RotaryConfig`（旋转位置编码子配置）并填入 `ModelConfig`。

伪代码：

```
def from_hf(config):
    if config 有 text_config:
        顶层 = config; config = config.text_config
        把顶层缺失的 architectures/rope_theta/rope_scaling 补到 config
    num_kv_heads = getattr(num_key_value_heads, 默认=num_attention_heads)
    head_dim = getattr(head_dim) 或 hidden_size // num_attention_heads
    rope_theta = getattr(rope_theta) 或 rope_scaling["rope_theta"]
    ... 其余字段同理
    return ModelConfig(..., rotary_config=RotaryConfig(...))
```

#### 4.1.3 源码精读

多模态外壳剥离——注意它只在「子 config 缺该字段且顶层有」时才补，避免覆盖子 config 自己的值：

[python/minisgl/models/config.py:42-47](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/config.py#L42-L47) —— 若带 `text_config` 则下钻，并把顶层缺失字段补齐。

字段兼容提取，每个字段都自带默认值，缺失也能跑通：

[python/minisgl/models/config.py:49-61](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/config.py#L49-L61) —— `num_kv_heads`/`head_dim`/`tie_word_embeddings`/`model_type`/MoE 系列/`rope_theta` 的兼容提取。其中 `rope_theta` 用 `or` 链兼容 Mistral（顶层 `getattr` 取不到就回退到 `rope_scaling["rope_theta"]`，[config.py:60-61](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/config.py#L60-L61)）。

组装返回，把零散字段收进 `RotaryConfig` 与 `ModelConfig`：

[python/minisgl/models/config.py:63-87](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/config.py#L63-L87) —— 构造 `RotaryConfig(head_dim, rotary_dim=head_dim, max_position, base=rope_theta, scaling)` 与完整 `ModelConfig`。

注意两个设计细节：

- `rotary_dim = head_dim`：Mini-SGLang 默认对整个 head_dim 做 RoPE，不支持部分旋转维度（若某模型需要部分旋转，这里需要扩展）。
- `is_moe` 属性 [config.py:36-38](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/config.py#L36-L38) 只看 `"moe" in model_type`。Qwen3-MoE 的 `model_type` 是 `qwen3_moe`，命中；dense 模型如 `llama`/`qwen3` 不命中。这个布尔值在后面 `load_weight` 里会决定是否走 expert 堆叠分支。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `from_hf` 对不同家族 config 的兼容性。

**操作步骤**（无需 GPU，纯 CPU 即可）：

1. 安装好 `transformers` 后，写一段示例脚本（**示例代码**，非项目原有）：

   ```python
   # 示例代码
   from transformers import AutoConfig
   from minisgl.models.config import ModelConfig

   cfg = AutoConfig.from_pretrained("Qwen/Qwen3-0.6B", trust_remote_code=True)
   mc = ModelConfig.from_hf(cfg)
   print(mc.num_layers, mc.num_qo_heads, mc.num_kv_heads, mc.head_dim)
   print(mc.rotary_config.base, mc.is_moe)
   ```

2. 把模型名换成 `meta-llama/Llama-3.2-1B` 再跑一次，对比 `num_kv_heads`（Llama-3.2 是 GQA，`num_kv_heads` 会明显小于 `num_qo_heads`）。

**需要观察的现象**：不同家族的 HF config 对象类型不同，但打印出的 `ModelConfig` 字段名、类型完全一致。

**预期结果**：Qwen3-0.6B 是 dense 模型，`is_moe=False`；GQA 模型的 `num_kv_heads < num_qo_heads`；`rotary_config.base` 即 `rope_theta`（如 1000000.0）。若环境无法联网下载，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `head_dim` 写成 `getattr(config, "head_dim", None) or config.hidden_size // config.num_attention_heads`？如果一个模型的 `head_dim` 恰好等于 0 会发生什么？

**参考答案**：`head_dim` 字段并非所有模型都有（早期 Llama 没有该字段，靠 `hidden_size // num_attention_heads` 推导）；用 `or` 兜底两种来源。若某模型 `head_dim=0`，`or` 会因为 0 是 falsy 而回退到除法分支——这是个隐含的边界条件，实际模型不会出现 0 维头。

**练习 2**：若一个多模态模型的 `text_config` 里已经自己带了 `rope_theta`，顶层也有一份不同的 `rope_theta`，`from_hf` 最终用哪份？

**参考答案**：用 `text_config` 自己的那份。[config.py:46](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/config.py#L46) 的条件是 `not getattr(config, attr, None)`，即「子 config 没有时才补」，已有则不覆盖。

### 4.2 _shard_tensor：按 TP 切分单个张量

#### 4.2.1 概念说明

TP 下每张卡只存自己那份权重。`_shard_tensor` 负责：给定一个完整的 HF 权重张量、当前 rank `r`、总卡数 `n`、KV 头数 `num_kv_heads`，算出「rank r 该拿哪一片」。它是纯函数，不碰磁盘、不做合并，只做切片。

关键在于不同权重的切分维度不同，对应 u8-l1 里不同的 Linear 类型：

| 权重后缀 | 切分维度 | 对应运行时层 | 含义 |
| --- | --- | --- | --- |
| `.q_proj/.k_proj/.v_proj/.gate_proj/.up_proj` | dim0（输出维） | 列并行 | 每卡拿一部分输出头/中间维 |
| `.o_proj/.down_proj` | dim1（输入维） | 行并行 | 每卡拿一部分输入，输出靠 all_reduce 合并 |
| `lm_head/embed_tokens` | dim0（词表维，向上取整） | 词表并行 | 每卡负责一段词表区间 |

#### 4.2.2 核心流程

```
def _shard_tensor(key, value, r, n, num_kv_heads):
    if key 是 q/k/v/gate/up (dim0 类):
        if key 是 k/v 且 num_kv_heads < n:   # GQA 头复制
            head_dim = shape[0] // num_kv_heads
            head_idx = r * num_kv_heads // n
            return value[head_idx*head_dim : (head_idx+1)*head_dim]
        else:
            return value.chunk(n, dim=0)[r]
    elif key 是 o/down (dim1 类):
        return value.chunk(n, dim=1)[r]
    elif key 是 lm_head/embed_tokens:
        per = ceil(vocab / n)
        return value[r*per : min((r+1)*per, vocab), :]
    else:
        return value            # 不切，整份复制（如 norm 权重）
```

GQA 复制的直觉：当 KV 头比卡还少（如 8 个 KV 头、16 张卡），没法每卡分一个独立头，于是把同一个 KV 头复制给相邻的几张卡。复制因子是 `n / num_kv_heads`：rank `r` 取第 `r * num_kv_heads // n` 个 KV 头。

#### 4.2.3 源码精读

切分规则表与 GQA 复制分支：

[python/minisgl/models/weight.py:34-52](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/weight.py#L34-L52) —— `_shard_tensor` 全文。注意 GQA 分支 [weight.py:37-41](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/weight.py#L37-L41)：只有 `.k_proj/.v_proj` 且 `num_kv_heads < n` 才走头复制；其余 dim0 权重走普通 `chunk(n, dim=0)`。

词表并行的向上取整——保证最后一张卡不越界、又覆盖全部 token：

[python/minisgl/models/weight.py:45-50](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/weight.py#L45-L50) —— `div_ceil(vocab, n)` 算每段大小，末段用 `min((r+1)*per, vocab)` 截断。

切分规则常量：

[python/minisgl/models/weight.py:13-14](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/weight.py#L13-L14) —— `_SPLIT_DIM_0`（列并行）/`_SPLIT_DIM_1`（行并行）后缀列表。

一致性核对：这里 GQA 复制给出的「每卡 1 个 KV 头」与运行时 `LinearQKVMerged` 里 `local_num_kv = div_even(num_kv_heads, tp_size, allow_replicate=True)`（[linear.py:83](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/linear.py#L83)）给出的 local 形状必须对齐——否则 `load_state_dict` 会因形状不符报错。这是「加载器」与「层定义」之间的一对隐形契约。

#### 4.2.4 代码实践

**实践目标**：手动验证 GQA 头复制的切片下标。

**操作步骤**：假设某模型 `num_kv_heads=8`、`head_dim=128`，`k_proj` 权重 `shape=(1024, hidden)`（即 `8*128` 行）。开 16 张卡（`n=16`）。

1. 按 [weight.py:38-41](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/weight.py#L38-L41) 手算 rank 0、1、2、3 各取哪段：
   - `head_dim = 1024 // 8 = 128`
   - rank 0：`head_idx = 0*8//16 = 0` → 行 `[0:128]`
   - rank 1：`head_idx = 1*8//16 = 0` → 行 `[0:128]`（与 rank 0 相同，即复制）
   - rank 2：`head_idx = 2*8//16 = 1` → 行 `[128:256]`
   - rank 3：`head_idx = 1` → 行 `[128:256]`
2. 对照结论：每 2 个相邻 rank 共享同一个 KV 头，复制因子 = `n/num_kv_heads = 2`。

**预期结果**：16 个 rank 共享 8 个 KV 头，每头被复制到 2 张卡。这保证 `all_reduce` 时（虽然 KV 不 all_reduce，但 query 的 attention 计算需要 KV 对齐）多卡结果正确。

**练习（无需运行，纯推导）**：若 `num_kv_heads=4`、`n=8`，rank 5 取第几个 KV 头？**答案**：`head_idx = 5*4//8 = 2`，取第 2 个 KV 头（rank 4、5 共享）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `.o_proj`（行并行）切 dim1 而不是 dim0？

**参考答案**：`o_proj` 把 attention 输出投影回 `hidden_size`。列并行已经在 `qkv` 那侧把 query 头分到了各卡，`o_proj` 的输入是「本卡的局部头输出」，因此要切输入维（dim1）；输出（hidden_size）每卡都算全量的一部分，靠 [linear.py:102-106](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/linear.py#L102-L106) 的 `all_reduce` 求和还原完整 hidden。

**练习 2**：`embed_tokens` 用 `div_ceil` 向上取整而非整除，这会带来什么副作用？

**参考答案**：词表大小 `vocab` 未必能被 `tp_size` 整除；向上取整保证不漏任何 token，但会让某些 rank 的词表区间有重叠（末段被截断）或多算几行。运行时 `VocabParallelEmbedding` 用各自的词表区间 mask 掉非本卡 token，所以重叠不影响正确性。

### 4.3 _MERGE_GROUPS：q/k/v → qkv_proj 合并

#### 4.3.1 概念说明

HF ckpt 里 attention 的投影是三个独立权重 `q_proj/k_proj/v_proj`，MLP 是两个 `gate_proj/up_proj`。但 Mini-SGLang 的运行时层（见 u8-l1 的 `RopeAttn`/`GatedMLP`）为了减少 kernel launch、提高访存效率，把它们合并成单个大矩阵：`qkv_proj`（q/k/v 拼接）和 `gate_up_proj`（gate/up 拼接）。

所以加载时要做一次「合并」：等同一组的几个分片都到齐后，按 dim0 拼起来，再以合并后的名字（`qkv_proj`/`gate_up_proj`）交给模型。这一步在 `_shard_tensor` **之后**进行——即先切分、再合并，保证合并后的张量正好是「本卡应得的 q/k/v 片段拼在一起」。

#### 4.3.2 核心流程

```
对每个读到的权重 key（已经 shard 过）:
    info = _get_merge_info(key)          # 属于某个合并组吗？
    if info is None:
        直接 yield (key, tensor)         # 不需要合并，如 o_proj、norm
    else:
        merged_key, slot, all_slots = info   # 如 (qkv_proj, "q", ("q","k","v"))
        merge_buf[merged_key][slot] = tensor # 先攒进缓冲
        if 还没集齐 all_slots: continue      # 等齐了再发
        parts = [merge_buf[merged_key][s] for s in all_slots]
        yield (merged_key, torch.cat(parts, dim=0))   # 按顺序拼接
```

合并顺序由 `_MERGE_GROUPS` 里的 slot 元组决定：qkv 是 `("q","k","v")`，gate_up 是 `("gate","up")`。最终 `qkv_proj` 的 dim0 排列是 `[q 的头, k 的头, v 的头]`，与运行时 `AttentionLayer.split` 的拆分顺序一致。

#### 4.3.3 源码精读

合并组定义——把后缀映射到「合并后后缀 + 槽位顺序」：

[python/minisgl/models/weight.py:17-23](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/weight.py#L17-L23) —— `_MERGE_GROUPS`：`q/k/v → qkv_proj`、`gate/up → gate_up_proj`。注意 `.k_proj` 也映射到 `qkv_proj`，因为 k 与 q、v 同组。

槽位名映射（把后缀转成短名 q/k/v/gate/up）：

[python/minisgl/models/weight.py:24-30](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/weight.py#L24-L30) —— `_SLOT_NAMES`。

查组合并信息：

[python/minisgl/models/weight.py:55-60](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/weight.py#L55-L60) —— `_get_merge_info`：命中则返回 `(合并后 key, slot, 全部槽位)`。

主循环里的合并缓冲与「集齐才发」逻辑：

[python/minisgl/models/weight.py:100-109](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/weight.py#L100-L109) —— 把分片攒进 `merge_buf`，三件到齐后 `torch.cat(parts, dim=0)` 发出 `qkv_proj`。

循环结束的完整性断言——若 ckpt 里缺了 q/k/v 某一个，缓冲里会留下半套，直接报错：

[python/minisgl/models/weight.py:123](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/weight.py#L123) —— `assert not merge_buf`，防止静默加载出残缺模型。

合并后的形状契约（与运行时层对齐）：合并后的 `qkv_proj` dim0 = `(local_qo + 2*local_kv) * head_dim`，正是 [linear.py:85-87](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/linear.py#L85-L87) `LinearQKVMerged` 算出的 `local_osize`。这一对契约由「先切后合」的顺序保证：每个槽位先被 `_shard_tensor` 切成本卡的份额，再拼起来。

#### 4.3.4 代码实践

**实践目标**：追踪 `q_proj/k_proj/v_proj` 三个 HF 权重如何变成运行时 `qkv_proj`。

**操作步骤**：以 `Qwen/Qwen3-0.6B`（假设 `num_qo_heads=16`、`num_kv_heads=8`、`head_dim=128`、`hidden_size=1024`，**具体数值以本地 ckpt 为准**），单卡（`tp=1`）：

1. 在 HF ckpt 里找到 `model.layers.0.self_attn.q_proj.weight`、`.k_proj.weight`、`.v_proj.weight`，三者 `shape` 分别约 `(2048,1024)`、`(1024,1024)`、`(1024,1024)`（即 `16*128`、`8*128`、`8*128` 行）。
2. 对照 [weight.py:36-42](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/weight.py#L36-L42)：`tp=1` 时 `chunk(1)` 返回整段，三个分片形状不变。
3. 对照 [weight.py:107-109](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/weight.py#L107-L109)：`torch.cat([q,k,v], dim=0)` 得到 `model.layers.0.self_attn.qkv_proj.weight`，shape = `(2048+1024+1024, 1024) = (4096, 1024)`。

**需要观察的现象**：合并后行数 = `(num_qo + 2*num_kv)*head_dim`；层内排列是 `[q 的 16 头 | k 的 8 头 | v 的 8 头]`。

**预期结果**：合并张量 shape 与 [linear.py:85](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/linear.py#L85) 的 `full_osize = (num_qo + 2*num_kv) * head_dim` 一致。若换 `tp=2`，每个槽位先被切成一半（q 变 1024 行、k/v 因 GQA 各 1024 行整段或半段），合并后行数也相应减半——**待本地用真实 ckpt 验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么合并必须在切分之后，而不能先把完整的 q/k/v 拼起来再切？

**参考答案**：因为 q、k、v 三段长度不同（q 是 `num_qo*head_dim`，k/v 是 `num_kv*head_dim`），且 GQA 下 k/v 的切分规则（头复制）与 q 不同（均匀 chunk）。若先拼后切，无法用一次 `chunk` 同时正确处理三段。先切再拼，每段用各自的规则切到本卡份额，拼出来天然正确。

**练习 2**：若一个新模型的 ckpt 里 attention 直接就叫 `qkv_proj`（已经是合并形态），`_MERGE_GROUPS` 还能工作吗？

**参考答案**：不能直接工作——`_get_merge_info` 靠后缀 `.q_proj/.k_proj/.v_proj` 命中，`.qkv_proj` 不在表里，会走「不需要合并」分支直接 yield。这时需要模型层也用 `qkv_proj` 命名（Mini-SGLang 的 `RopeAttn` 正是如此），名称对得上即可直接加载，无需合并。

### 4.4 expert 堆叠：MoE 专家权重的打包

#### 4.4.1 概念说明

MoE 模型的 HF ckpt 把每个专家的权重单独存：`model.layers.0.mlp.experts.0.gate_proj.weight`、`experts.1.gate_proj.weight`……共 `num_experts` 份。但 Mini-SGLang 的 fused MoE kernel（u10-l1 会详讲）需要的是一整块打包张量 `model.layers.0.mlp.experts.gate_up_proj`，形状 `(num_experts, 2*intermediate, hidden)`——即「专家维」堆在最前。

于是加载时要做两件事：

1. 先经过 4.3 的合并（把每个专家的 `gate_proj`+`up_proj` 合并成该专家的 `gate_up_proj`）。
2. 再把 `num_experts` 个专家的 `gate_up_proj` 沿新维度 0 `torch.stack` 成一块。

`down_proj` 不参与合并（它是行并行，在 `_SPLIT_DIM_1` 里），但同样要按专家堆叠。

#### 4.4.2 核心流程

```
对每个（已合并的）权重 out:
    if config.is_moe and (expert_info := _get_expert_stack_info(out[0])) is not None:
        packed_key, expert_idx = expert_info    # 如 (experts.gate_up_proj, 0)
        expert_buf[packed_key][expert_idx] = tensor
        if len(expert_buf[packed_key]) != num_experts: continue   # 专家没到齐
        experts = [slots[i] for i in range(num_experts)]
        yield (packed_key, torch.stack(experts, dim=0))
    else:
        yield out                                 # dense 权重直接发
```

专家下标通过正则 `_EXPERT_PATTERN` 从 key 里抠出来：`model.layers.0.mlp.experts.3.gate_up_proj.weight` → `prefix=model.layers.0.mlp.experts`、`idx=3`、`name=gate_up_proj`，打包成 `model.layers.0.mlp.experts.gate_up_proj`（去掉专家下标）。

#### 4.4.3 源码精读

专家 key 的正则解析：

[python/minisgl/models/weight.py:31](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/weight.py#L31) —— `_EXPERT_PATTERN` 捕获 `prefix`（到 `.experts`）、`idx`（专家编号）、`name`（权重名）。

把专家 key 映射到打包 key（去掉专家下标、去掉 `.weight` 后缀）：

[python/minisgl/models/weight.py:63-72](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/weight.py#L63-L72) —— `_get_expert_stack_info`，返回 `(packed_key, expert_idx)`。

主循环里堆叠缓冲与「集齐才发」：

[python/minisgl/models/weight.py:111-121](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/weight.py#L111-L121) —— 只有 `config.is_moe` 才走堆叠分支；按 `range(num_experts)` 顺序取出（保证专家顺序与下标对齐，不依赖字典插入序），`torch.stack(dim=0)` 打包。

专家完整性断言：

[python/minisgl/models/weight.py:124](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/weight.py#L124) —— `assert not expert_buf`，若 ckpt 缺专家会在此报错。

形状契约：堆叠后的 `experts.gate_up_proj` 形状 `(num_experts, 2*intermediate_per_partition, hidden)`，正是 [moe.py:34-38](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/moe.py#L34-L38) 里 `MoELayer.gate_up_proj` 预留的形状（第 0 维 `num_experts`）。注意这里的 `intermediate` 已经被 `_shard_tensor` 按 dim0 切过（gate/up 属于 `_SPLIT_DIM_0`），所以每卡只持有每个专家的「中间维切片」。

重要细节：堆叠发生在合并**之后**。[weight.py:111](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/weight.py#L111) 检查的是 `out[0]`（合并后的 key），所以 `experts.0.gate_proj`+`experts.0.up_proj` 先合成 `experts.0.gate_up_proj`，再与其它专家堆叠成 `experts.gate_up_proj`。

#### 4.4.4 代码实践

**实践目标**：理清 MoE 权重的两层变换（合并 + 堆叠）。

**操作步骤**（源码阅读型，无需 GPU）：以 `Qwen/Qwen3-30B-A3B`（MoE，假设 `num_experts=128`，**以本地为准**）：

1. 在 HF ckpt 里观察 `model.layers.0.mlp.experts.0.gate_proj.weight`、`.up_proj.weight`、`.down_proj.weight` 三类各 128 份。
2. 追踪单个专家 0：
   - `experts.0.gate_proj` + `experts.0.up_proj` 经 [weight.py:100-109](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/weight.py#L100-L109) 合并成 `experts.0.gate_up_proj`。
   - `experts.0.down_proj` 不合并，原样进入堆叠缓冲。
3. 追踪堆叠：128 个专家的 `gate_up_proj` 经 [weight.py:111-119](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/weight.py#L111-L119) `torch.stack(dim=0)` 成 `experts.gate_up_proj`，shape `(128, 2*intermediate, hidden)`。

**需要观察的现象**：最终模型 `state_dict` 里只有 `experts.gate_up_proj` 与 `experts.down_proj` 两个张量（专家维已合并进第 0 维），不再有 `experts.{i}.*`。

**预期结果**：与 [moe.py:34-43](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/moe.py#L34-L43) 的 `gate_up_proj`/`down_proj` 形状对齐；fused kernel 的 `w1=gate_up_proj`、`w2=down_proj` 直接可用。

#### 4.4.5 小练习与答案

**练习 1**：为什么堆叠用 `torch.stack`（新建一维）而不是 `torch.cat`？

**参考答案**：每个专家权重是 2D `(2*intermediate, hidden)`，要打包成 `(num_experts, 2*intermediate, hidden)` 的 3D 张量，需要在最前面新增「专家维」。`stack` 正是沿新维度拼接；若用 `cat(dim=0)` 会把专家维和中间维混在一起变成 `(num_experts*2*intermediate, hidden)`，形状就错了。

**练习 2**：`down_proj`（行并行）参与堆叠时，它的权重在 `_shard_tensor` 里走哪条分支？

**参考答案**：走 dim1 分支 [weight.py:43-44](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/weight.py#L43-L44)，切的是输入维（中间维 `intermediate`）。所以每卡持有的 `down_proj` 是 `(hidden, intermediate/tp)`，堆叠后 `(num_experts, hidden, intermediate/tp)`，与 [moe.py:39-43](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/moe.py#L39-L43) 的形状一致。

## 5. 综合实践

把本讲四个模块串起来，完整还原一条权重从 HF ckpt 到运行时张量的旅程。选一个 dense 模型（如 `Qwen/Qwen3-0.6B`）做单卡分析，再选一个 GQA + 多卡场景做推导：

**任务 A（dense，单卡）**：对 `model.layers.0.self_attn.q_proj.weight`：

1. 写下它在 HF ckpt 里的原始 key 与 shape。
2. 过 [config.py:41](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/config.py#L41) `from_hf` 拿到 `num_kv_heads/head_dim`。
3. 过 [weight.py:34-52](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/weight.py#L34-L52) `_shard_tensor`（`tp=1`，返回整段）。
4. 过 [weight.py:55-60](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/weight.py#L55-L60) 命中 `qkv_proj` 组，与 k、v 攒齐后 [weight.py:109](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/weight.py#L109) `cat` 成 `qkv_proj`。
5. 因为 `is_moe=False`，跳过堆叠，直接 yield。
6. 在 [engine.py:146](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L146) 被 `.to(dtype)` 收进字典，最终喂给 `RopeAttn.qkv_proj`。

**任务 B（GQA，多卡）**：同一个模型假设开 `tp=2`，且 `num_kv_heads=8`（不小于 tp_size）：

1. q_proj：`chunk(2, dim=0)[r]`，每卡拿一半 query 头。
2. k_proj/v_proj：因 `num_kv_heads(8) >= n(2)`，走普通 `chunk(2, dim=0)[r]`，每卡拿 4 个 KV 头。
3. 合并后每卡的 `qkv_proj` 行数 = `(num_qo/2 + 2*4)*head_dim`。

**任务 C（边界）**：若 `num_kv_heads=4`、`tp=8`，问 k_proj 走哪条分支、rank 0 和 rank 1 是否拿到相同数据？

**预期产出**：一份表格，列出每个权重「HF key → shard 后形状 → 合并/堆叠后 key → 运行时归属层 → 形状契约」。

> 提示：任务 C 中 `num_kv_heads(4) < n(8)`，k/v 走 GQA 复制分支 [weight.py:38-41](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/weight.py#L38-L41)，rank 0 与 rank 1 的 `head_idx` 都是 `0`，拿到相同的第 0 个 KV 头（复制因子 2）。

## 6. 本讲小结

- `ModelConfig.from_hf` 是「异构 HF config → 统一内部配置」的翻译桥，集中处理多模态 `text_config` 下钻、字段默认值、`rope_theta` 双来源（顶层 vs `rope_scaling` 字典）等兼容性脏活。
- `_shard_tensor` 按 TP 切单张量，三类规则：dim0（q/k/v/gate/up，列并行）、dim1（o/down，行并行）、vocab 向上取整（embed/lm_head）；GQA 下 `num_kv_heads < tp_size` 时对 k/v 走「头复制」。
- `_MERGE_GROUPS` 在切分**之后**把 q/k/v、gate/up 按 dim0 拼成 `qkv_proj`/`gate_up_proj`，顺序与运行时 `AttentionLayer.split` 对齐；缺片会被循环末尾的断言拦住。
- expert 堆叠在合并**之后**：`_get_expert_stack_info` 用正则抠出专家下标，把 `num_experts` 个专家 `torch.stack` 到新第 0 维，产出 fused MoE kernel 需要的 `(num_experts, ...)` 打包张量。
- 加载器与层定义之间靠「形状契约」绑定：`_shard_tensor` 给出的 local 形状必须等于 `LinearQKVMerged`/`LinearRowParallel`/`MoELayer` 里 `div_even` 算出的 local 形状，否则 `load_state_dict` 报错。
- 整个 `load_weight` 是流式的：一次只持有一个完整张量 + 小合并/堆叠缓冲，峰值内存远低于「全部载入再切」，适配大模型。

## 7. 下一步学习建议

- 想搞清切出来的权重在前向时如何通信还原？继续读 **u9-l1（张量并行 Linear 与分布式通信）**，它讲 `all_reduce`/`all_gather` 发生在哪、`LinearQKVMerged` 的 GQA 复制与 `div_even(allow_replicate=True)` 如何与本讲的 `_shard_tensor` 对账。
- 想看 fused MoE kernel 怎么消费本讲打包好的 `(num_experts, ...)` 张量？读 **u10-l1（MoE 后端 Fused MoE）**，它讲 `w1=gate_up_proj`、`w2=down_proj` 在两段 grouped GEMM 里的用法。
- 想接入一个本项目还没支持的新模型？读 **u10-l3（支持新模型架构）**，它会回到本讲的 `from_hf`/`_MERGE_GROUPS`/`_shard_tensor`，告诉你新模型的 config 字段与权重 key 映射要改哪里。
- 若想验证本讲的形状契约，可在真实 ckpt 上跑 `load_weight(model_path, device)` 并打印每个 yield 的 `(key, tensor.shape)`，与模型 `state_dict()` 的形状逐一比对——这是最直接的「阅读型实践」。
