# trainer.py：FlashAttention varlen 替换与 monkey patch

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释「为什么拼接（flatten/packing）训练必须替换注意力实现」——不替换会发生什么（样本间注意力泄漏）。
2. 逐行读懂 `flash_attention_forward`：它如何把 `attention_mask` 语义重载为 `cu_seqlens`，`max_seqlen` 如何由差分计算。
3. 说出 `qwen2vl_forward` 与 `qwen3vl_forward` 两套替换函数的结构差异（QK-Norm、RoPE 应用方式）。
4. 掌握 `replace_qwen2_vl_attention_class` 的 monkey patch 手法：跨 Qwen2-VL / 2.5-VL / 3-VL / 3-VL-MoE 四类模型、在运行时替换类方法与模块级掩码函数。
5. 看懂 `create_optimizer` 的多参数组改造：6 组参数如何按 decay × 模块归属划分，实现 merger / visual / LLM 分层学习率。

## 2. 前置知识

本讲是单元五源码链的第六环，直接承接 u5-l5 的两种 DataCollator。先用通俗语言补齐几个概念。

**拼接训练与它的代价**。上一讲我们看到：`data_flatten`（运行时把一个 batch 的样本沿序列维拼成一条）和 `data_packing`（离线装箱 + 桶内桶间两级拼接）都把 N 条样本拼成一条形如 `(1, ΣL)` 的长序列，好处是没有 padding 浪费、注意力计算量从 `B·L_max²` 降到 `ΣLᵢ²`。但注意力默认是**全局因果**的：序列里位置 i 的 token 可以看到所有 j ≤ i 的 token。拼接后，第 2 条样本的 token 能「看见」第 1 条样本的全部内容——包括第 1 条的答案。这就是**样本间注意力泄漏**：模型等于在「答案已经写在题目前面」的输入上训练，SFT 的监督信号被污染。

**FlashAttention 与 varlen 接口**。FlashAttention 是一种 IO 感知的精确注意力实现：不物化 `L×L` 的注意力矩阵，而是分块计算，显存从 \( O(L^2) \) 降到近似 \( O(L) \)，数学结果与标准注意力一致。它提供一个 **varlen（variable-length，变长）**接口 `flash_attn_varlen_func`，专门处理「一条维度里挤着好几条不等长序列」的场景，靠两个额外参数描述分段：

- `cu_seqlens`（cumulative sequence lengths，累计序列长度）：长度为「段数 + 1」的 int32 向量，首元素为 0。例如三段长度 `[12, 35, 28]`，则 `cu_seqlens = [0, 12, 47, 75]`，相邻两元素的差就是各段长度。
- `max_seqlen`：最长一段的长度，是 kernel 启动时的调度提示。

`causal=True` 时，kernel 把每段当独立序列做段内因果注意力，**段与段之间完全不交互**——注意力矩阵从「全局下三角」变成「块对角的下三角」，这正是堵住泄漏的钥匙。

**monkey patch（猴子补丁）**。Python 里类和模块都是可修改的对象。`Cls.forward = my_forward` 这类赋值会让该类**所有实例**（包括已创建的）调用新函数，且新函数第一个参数自动绑定实例（`self`）。本仓库大量使用这种手法在**不 fork transformers 的前提下**改写其行为——`trainer.py` 的模块底部、`train_qwen.py` 的 `import` 链都在做这件事。

**优化器参数组**。PyTorch 的 `AdamW` 接受参数组列表，每组可携带独立的 `lr` 与 `weight_decay`。这让「视觉塔用一个学习率、投影层用另一个学习率」成为可能。

**其他术语**：causal mask（因果掩码，禁止 token 看未来）；KV cache（推理时缓存的 Key/Value，训练关闭）；merger / visual / language_model（Qwen3-VL 的三个部件：视觉-语言投影层、ViT 视觉塔、语言模型，见 u5-l1 的 `set_model`）。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `qwen-vl-finetune/qwenvl/train/trainer.py` | 本讲主角：对 transformers 的补丁集合 | `flash_attention_forward`（L33-L108）、`qwen2vl_forward`（L111-L158）、`qwen3vl_forward`（L162-L200）、`return_mask`（L203-L212）、`replace_qwen2_vl_attention_class`（L215-L252）、`create_optimizer`（L316-L491）、模块底部的补丁语句（L494-L511） |
| `qwen-vl-finetune/qwenvl/train/train_qwen.py` | 训练入口、补丁的调用方 | L28 的 import、L141-L142 的条件打补丁、L182-L184 的诊断打印 |
| `qwen-vl-finetune/qwenvl/data/data_processor.py` | 上一讲的 collator，`cu_seqlens` 的**生产者** | L427（样本级长度）、L461-L471（packed 桶的子样本长度）、L610-L635（cumsum 拼装） |
| transformers 库内四个建模文件（`modeling_qwen2_vl.py` 等，不在本仓库） | 被替换对象 | `Qwen2VLAttention` 等注意力类、模块级 `create_causal_mask` |

记住分工口诀：**collator 生产 `cu_seqlens`，`trainer.py` 消费它**。生产者在上一讲，消费者在本讲。

## 4. 核心概念与源码讲解

### 4.1 flash_attention_forward：varlen 注意力核心实现

#### 4.1.1 概念说明

这个函数解决的问题是：**把上一讲 collator 产出的「语义重载的 attention_mask」正确地喂给 FlashAttention 的 varlen kernel**。

回顾 u5-l5 的关键结论：常规 collator 的 `attention_mask` 是 0/1 掩码（见 [qwen-vl-finetune/qwenvl/data/data_processor.py:L560](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L560) 的 `input_ids.ne(pad_token_id)`）；而 `FlattenedDataCollatorForSupervisedDataset` 的 `attention_mask` 是**首元素为 0 的累计段长向量**（即 `cu_seqlens`）。

问题在于：transformers 的常规注意力路径不认识这种一维累计长度张量。无论掩码是全 1 还是标准 4-D 因果掩码，它都只会把 `(1, ΣL)` 的整行**当成一条序列**处理——段边界信息无处安放，泄漏必然发生。即使 `from_pretrained` 时传了 `attn_implementation="flash_attention_2"`（见 [qwen-vl-finetune/qwenvl/train/train_qwen.py:L92](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L92)），官方 FA2 路径对全 1 掩码也是「整行一条序列」。

所以本仓库的做法是：**整层替换注意力 forward**（4.2 节），让它直接调用这里手写的 `flash_attention_forward`，由后者把 `attention_mask` **当作 `cu_seqlens` 解释**并调用 `flash_attn_varlen_func`。

#### 4.1.2 核心流程

```text
输入: query/key/value 形如 (batch=1, heads, ΣL, head_dim)，attention_mask 是 1-D cu_seqlens
  1. 前置检查: output_attentions/head_mask 不支持则告警; 任一维为 0 则报错
  2. 转置: (batch, heads, seq, dim) → (batch, seq, heads, dim)   # FA2 约定
  3. squeeze(0): 去掉 batch 维 → (ΣL, heads, dim)                 # varlen 不接受 batch 维
  4. cu_seqlens = attention_mask                                   # 语义重载点!
  5. max_seqlen = max(cu_seqlens[i+1] - cu_seqlens[i])             # 相邻差分的最大值
  6. flash_attn_varlen_func(q, k, v, cu_seqlens, max_seqlen, causal=True)
  7. unsqueeze(0): 恢复 batch 维, 返回 (attn_output, None)
```

用数学语言对比两种掩码的差别。设拼接后总长 \( L \)，段边界 \( 0 = c_0 < c_1 < \dots < c_n = L \)：

\[ \text{全局因果（泄漏）}: \quad A_{ij} \neq 0 \iff j \le i, \quad \forall\, i,j \in [0, L) \]

\[ \text{varlen 分段因果（正确）}: \quad A_{ij} \neq 0 \iff j \le i \ \text{且}\ \exists s,\ i,j \in [c_s, c_{s+1}) \]

后者即 \( A = \mathrm{blockdiag}(T_1, T_2, \dots, T_n) \)，每个 \( T_s \) 是 \( L_s \times L_s \) 的下三角对应块的 softmax 注意力。段间块恒为 0，等价于各样本独立训练。

#### 4.1.3 源码精读

**入口与导入**。varlen kernel 来自 flash_attn 包（README 锁定的硬依赖，见 u4-l1）：

[qwen-vl-finetune/qwenvl/train/trainer.py:L1-L9](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py#L1-L9)
导入 `flash_attn_varlen_func`（L4）与 transformers 的 `Trainer`、`Cache`、`FlashAttentionKwargs`（仅用于 `**kwargs` 类型标注）等。L10-L27 还从 transformers 四个建模文件导入了 Qwen2-VL / 2.5-VL / 3-VL / 3-VL-MoE 的模型类，供模块底部打补丁用（见 4.3 节）。

**前置检查**：

[qwen-vl-finetune/qwenvl/train/trainer.py:L45-L59](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py#L45-L59)
若调用方要求 `output_attentions=True` 或传了 `head_mask`，只打一条告警（FlashAttention 不产出注意力权重）；若 q/k/v 任一维度为 0 直接抛 `ValueError`（varlen kernel 不接受空段之外的空张量）。

**转置与 dtype 处理**：

[qwen-vl-finetune/qwenvl/train/trainer.py:L60-L80](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py#L60-L80)
先转置成 FA2 约定的 `(batch, seqlen, head, dim)`（L62-L64）。L67-L80 处理一个 PEFT 常见坑：LoRA 训练时常把 LayerNorm 保存在 float32，导致隐状态被悄悄抬成 fp32；这里探测 autocast / 量化 / 首个 Linear 的 dtype，把目标 dtype 记到 `target_dtype`，供必要时回 cast（该分支服务于与上游实现的兼容，varlen 调用本身对输入 dtype 是透明的）。

**语义重载点与 max_seqlen 计算**——本函数最核心的 12 行：

[qwen-vl-finetune/qwenvl/train/trainer.py:L82-L93](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py#L82-L93)
- L82-L84：`squeeze(0)` 去掉 batch 维。拼接路径的 batch **恒为 1**（collator 沿 `dim=1` 拼接，见 [qwen-vl-finetune/qwenvl/data/data_processor.py:L626-L628](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L626-L628)），所以这一步是安全的；varlen kernel 的输入就是无 batch 的 `(ΣL, heads, dim)`。
- L85：`cu_seqlens = attention_mask`——**整个设计的支点**。生产者（collator）与消费者（本函数）约定了同一个非标准语义，中间所有 transformers 代码都不许碰这个张量（由 4.3 节的 `return_mask` 保证）。
- L87-L93：`max_seqlen` 取 `cu_seqlens` 相邻差分的最大值，包在 `torch.no_grad()` 里，最后 `.item()` 转成 Python 数（kernel 启动参数需要标量）。它是「最长一段」而非总长。

**varlen 调用与返回**：

[qwen-vl-finetune/qwenvl/train/trainer.py:L95-L108](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py#L95-L108)
`cu_seqlens_q == cu_seqlens_k`（自注意力，q/k 段划分一致）、`causal=True`（段内因果）。输出 `unsqueeze(0)` 恢复 batch 维，第二返回值为 `None`（无注意力权重）。两个阅读提示：① 函数签名接收的 `dropout` / `scaling` / `sliding_window` / `softcap`（L39-L42）在这条 varlen 调用里**均未透传**——flash-attn kernel 默认按 \( 1/\sqrt{d} \) 缩放，与这些模型的 `self.scaling` 一致，注意力 dropout 在此路径下实际为 0；② L52 的 `seq_len = query.shape[2]` 之后没有被使用，`max_seqlen` 完全由 `cu_seqlens` 差分得出，读码时不要被它误导。

**cu_seqlens 的生产端**（承接 u5-l5，本讲的输入契约）：

[qwen-vl-finetune/qwenvl/data/data_processor.py:L615-L625](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L615-L625)
collator 用 `itertools.chain` 把每个实例的段长列表摊平（flatten 路径每实例贡献 1 个长度——见 L427 的 `data_dict["attention_mask"] = [seq_len]`；packing 路径每个桶贡献多个子样本长度——见 [L464-L466](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L464-L466)），再 `[0] + 长度列表` 前插 0、`cumsum` 成 int32 张量。这就是 L85 拿到的 `cu_seqlens`：**flatten 的段边界是原始样本，packing 的段边界精确到桶内每个子样本**。

#### 4.1.4 代码实践：用纯 Python 复现 cu_seqlens 与 max_seqlen

1. **实践目标**：不看框架、亲手算一遍 `cu_seqlens` 与 `max_seqlen`，确认你理解 L85-L93 的语义。
2. **操作步骤**：新建 `cu_seqlens_sim.py`（放在仓库外的个人目录均可，不要改动仓库源码），写入以下**示例代码**：

   ```python
   # cu_seqlens_sim.py —— 复现 Flattened collator 与 trainer.py 的两步计算（示例代码）
   def build_cu_seqlens(seg_lens):
       """等价于 data_processor.py L624-L625: [0] + 段长列表 再累计求和"""
       cu = [0]
       for L in seg_lens:
           cu.append(cu[-1] + L)
       return cu

   def max_seqlen(cu):
       """等价于 trainer.py L88-L93: 相邻差分的最大值"""
       return max(cu[i + 1] - cu[i] for i in range(len(cu) - 1))

   cu = build_cu_seqlens([12, 35, 28])   # 假设 batch 内 3 条样本长度分别为 12/35/28
   print("cu_seqlens =", cu)
   print("max_seqlen =", max_seqlen(cu))
   assert cu == [0, 12, 47, 75] and max_seqlen(cu) == 35
   ```

   运行 `python cu_seqlens_sim.py`。
3. **需要观察的现象**：三段长度如何被「前插 0 + 累加」编码成 4 个边界值；`max_seqlen` 为何取 35 而不是总数 75。
4. **预期结果**：输出 `cu_seqlens = [0, 12, 47, 75]` 与 `max_seqlen = 35`，断言通过（纯 Python，结果确定）。再回头对照 [data_processor.py:L624-L625](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L624-L625)（`dtype=torch.int32`）与 [trainer.py:L87-L93](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py#L87-L93)，确认逻辑一致。
5. 本实践不依赖 GPU 与任何第三方库，可直接本地验证。

#### 4.1.5 小练习与答案

**练习 1**：batch 内 4 段长度 `[20, 8, 33, 8]`，写出 `cu_seqlens` 与 `max_seqlen`。
答案：`cu_seqlens = [0, 20, 28, 61, 69]`，`max_seqlen = 33`。

**练习 2**：`max_seqlen` 为什么不直接用 query 的总长度 69？
答案：它的语义是「最长**一段**的长度」，是 varlen kernel 的调度提示（决定分块与并行切分），kernel 按段循环、段间不交互。传总长 69 通常仍能算对（作为宽松上界），但属于无谓高估，影响 kernel 调度效率；仓库取真实最大值，正确且最紧。

**练习 3**：为什么 L82-L84 敢直接 `squeeze(0)`？
答案：varlen 接口不接受 batch 维；而拼接路径的 batch 恒为 1（collator 沿 `dim=1` 拼接所有实例），squeeze 掉的必然是那个 1，分段信息已全部由 `cu_seqlens` 表达。

### 4.2 qwen2vl_forward 与 qwen3vl_forward：两套替换用整层 forward

#### 4.2.1 概念说明

只替换「注意力 kernel 入口」不够——transformers 的 `Attention.forward` 是一个完整流程：q/k/v 投影 → RoPE → KV cache 更新 → 调用注意力实现 → 输出投影。要让 `flash_attention_forward`（带 cu_seqlens 语义）被调用，必须替换**整个 forward**。

`trainer.py` 提供了两套替身，按模型代际分工：

- `qwen2vl_forward`：服务 Qwen2-VL 与 Qwen2.5-VL 的注意力类；
- `qwen3vl_forward`：服务 Qwen3-VL 与 Qwen3-VL-MoE 的文本注意力类。

两套不能混用，因为两代模型的注意力结构有实质差异（这正是 Qwen3 的架构更新落到训练侧的样子）。

#### 4.2.2 核心流程

```text
qwen2vl_forward(self, hidden_states, attention_mask=cu_seqlens, position_embeddings, ...):
  1. q/k/v 投影 → view(bsz, q_len, heads, head_dim) → transpose 成 (bsz, heads, q_len, dim)
  2. apply_multimodal_rotary_pos_emb(q, k, cos, sin, mrope_section)   # 三段式 MRoPE
  3. 若有 past_key_values: 更新 KV cache（训练时 use_cache=False，走不到）
  4. flash_attention_forward(self, q, k, v, attention_mask, ...)      # 进入 4.1 的 varlen 路径
  5. reshape → o_proj → 返回 (attn_output, attn_weights=None)

qwen3vl_forward: 同骨架，差异在第 1、2 步 ——
  1' q/k 投影后接 q_norm/k_norm（QK-Norm）
  2' apply_rotary_pos_emb(q, k, cos, sin)（无 mrope_section 参数）
```

#### 4.2.3 源码精读

**qwen2vl_forward 主体**：

[qwen-vl-finetune/qwenvl/train/trainer.py:L124-L137](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py#L124-L137)
L126-L128 三投影；L130-L132 整形转置；L134-L137 应用**多模态 RoPE**，`self.rope_scaling["mrope_section"]` 告诉函数如何把每个头的旋转维切成 t/h/w 三段——这是 Qwen2/2.5-VL 三段式 MRoPE 在注意力层的落点（三代 MRoPE 的坐标生产端见 u5-l4 的 rope2d.py）。

[qwen-vl-finetune/qwenvl/train/trainer.py:L139-L158](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py#L139-L158)
L139-L141 保留 KV cache 更新分支（兼容推理场景；训练时 [train_qwen.py:L143](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L143) 已 `use_cache=False`）；L143-L154 把 `attention_mask`（此刻已是 cu_seqlens）原样传给 `flash_attention_forward`；L156-L158 输出投影后返回。注意 L152 顺手传了 `position_ids=position_ids`（注释 "pass positions for FA2"），但在 4.1 的实现里并未被消费。

**qwen3vl_forward 主体**：

[qwen-vl-finetune/qwenvl/train/trainer.py:L172-L180](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py#L172-L180)
两处代际差异都在这里：L175-L177 在 q/k 投影后接 `self.q_norm` / `self.k_norm`——Qwen3 家族的 **QK-Norm**（对 query/key 做 RMSNorm，稳定大模型训练），Qwen2/2.5-VL 没有；L180 调用的是不带 `mrope_section` 的 `apply_rotary_pos_emb`——Qwen3-VL 的 Interleaved-MRoPE 采用交错式频率分配，cos/sin 在模型层构造时已经处理完，注意力层无需再切三段（承接 u1-l1 与 u5-l4 的架构背景）。

[qwen-vl-finetune/qwenvl/train/trainer.py:L187-L200](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py#L187-L200)
与 qwen2vl 版对称：同一个 `flash_attention_forward`、同样的 o_proj 收尾。两套 forward 的差异全部集中在「进入注意力之前的准备工作」，varlen 调用完全共享。

另外注意 L111 与 L162 的装饰器 `@deprecate_kwarg("past_key_value", new_name="past_key_values", version="4.58")`：兼容旧参数名的警告转换，是照抄 transformers 上游风格的防御性写法。

#### 4.2.4 代码实践：两套 forward 差异对照表

1. **实践目标**：通过逐行对照，把两套替换 forward 的结构差异固化成一张表。
2. **操作步骤**：打开 [trainer.py:L111-L158](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py#L111-L158) 与 [trainer.py:L162-L200](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py#L162-L200)，完成下表（已给出第一行作示范）：

   | 对比项 | qwen2vl_forward | qwen3vl_forward |
   | --- | --- | --- |
   | QK-Norm（q_norm/k_norm） | 无 | 有（L175-L176） |
   | RoPE 函数 | ？ | ？ |
   | RoPE 额外参数 | ？ | 无 |
   | KV cache 更新分支 | ？ | ？ |
   | 最终调用的注意力函数 | ？ | ？ |

3. **需要观察的现象**：两套函数的骨架高度对称，差异集中在投影后、注意力前的少数几行。
4. **预期结果**（源码阅读型，答案确定）：RoPE 函数分别为 `apply_multimodal_rotary_pos_emb` 与 `apply_rotary_pos_emb`；额外参数分别为 `self.rope_scaling["mrope_section"]` 与无；KV cache 分支两者都保留（L139-L141 / L182-L185）；注意力函数两者都是 `flash_attention_forward`（L143 / L187）。
5. 本实践为源码阅读型，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：`q_norm` / `k_norm` 是什么？为什么 Qwen3-VL 需要而 Qwen2-VL 不需要？
答案：对 Query/Key 做 RMSNorm（QK-Norm），Qwen3 家族引入的结构，用于稳定大规模训练的注意力分布；Qwen2/2.5-VL 没有该层，所以两套 forward 不能混用——把 qwen2vl_forward 装到 Qwen3-VL 上会因为模块缺少/多余属性而出错。

**练习 2**：为什么训练时 `use_cache=False`（train_qwen.py L143），forward 里还保留 `past_key_values` 分支？
答案：保持与 transformers Attention 接口的签名兼容（BC），微调产物要能直接用于推理；推理路径带 KV cache 时该分支是必需的。

**练习 3**：`apply_multimodal_rotary_pos_emb` 的 `mrope_section` 从哪来？
答案：模型 config 的 `rope_scaling` 字典（加载时挂在注意力模块的 `self.rope_scaling` 上），内容是每个头 128 个旋转维里 t/h/w 三段各自的维数；u5-l4 讲过三代 MRoPE 坐标的生产，这里是「坐标如何被消费」。

### 4.3 monkey patch 替换：replace_qwen2_vl_attention_class 与 return_mask

#### 4.3.1 概念说明

有了替身函数，还需要在运行时把它们「装」进 transformers 的类里。`replace_qwen2_vl_attention_class` 就是安装器：对四代模型各做两件事——替换注意力类的 `forward`、把模块级 `create_causal_mask` 换成恒等函数 `return_mask`。

第二件事容易被忽略但**必不可少**：新版 transformers 的建模代码会在注意力计算前调用模块级 `create_causal_mask(...)` 构建 4-D 浮点掩码。如果放任它运行，它会按「0/1 padding 掩码」的语义去解释我们的一维 int32 `cu_seqlens` 张量——要么广播直接报错，要么生成语义错误的掩码。`return_mask` 恒等返回第三个位置参数（`attention_mask`），让 `cu_seqlens` **原样穿透**到注意力层，进入 4.1 的消费端。

命名提示：函数名里的 `qwen2_vl` 是历史遗留——它实际替换的是**全部四代**模型的注意力类。

#### 4.3.2 核心流程

```text
replace_qwen2_vl_attention_class():
  对 Qwen2-VL:   Qwen2VLAttention.forward        = qwen2vl_forward
                 create_causal_mask               = return_mask
                 create_sliding_window_causal_mask = return_mask
  对 Qwen2.5-VL: Qwen2_5_VLAttention.forward      = qwen2vl_forward（同上两个掩码函数）
  对 Qwen3-VL:   Qwen3VLTextAttention.forward     = qwen3vl_forward + create_causal_mask
  对 Qwen3-MoE:  Qwen3VLMoeTextAttention.forward  = qwen3vl_forward + create_causal_mask

调用时机（train_qwen.py L141-L142）:
  if data_args.data_flatten or data_args.data_packing:
      replace_qwen2_vl_attention_class()     # 只在拼接训练时才替换!
```

#### 4.3.3 源码精读

**恒等短路函数**：

[qwen-vl-finetune/qwenvl/train/trainer.py:L203-L212](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py#L203-L212)
`return_mask` 按位置接收 `create_causal_mask` 的全部参数（config、input_embeds、attention_mask、cache_position、past_key_values、position_ids），却只做一件事：`return attention_mask`。它是一个「接口形状兼容、行为恒等」的替身——transformers 的调用点毫无感知，而 4-D 掩码构建被彻底短路。

**安装器本体**：

[qwen-vl-finetune/qwenvl/train/trainer.py:L215-L228](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py#L215-L228)
Qwen2-VL 段：`Cls.forward = qwen2vl_forward` 是普通的类属性赋值——Python 描述符协议让函数第一个参数自动绑定实例，所以替身里的 `self` 就是各注意力模块自身；同时替换 `create_causal_mask` 与 `create_sliding_window_causal_mask`（Qwen2/2.5 建模文件里有滑动窗口掩码函数，一并短路）。

[qwen-vl-finetune/qwenvl/train/trainer.py:L229-L252](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py#L229-L252)
随后对 Qwen2.5-VL（复用 `qwen2vl_forward`）、Qwen3-VL 与 Qwen3-VL-MoE（复用 `qwen3vl_forward`）做同样操作；后两者只替换 `create_causal_mask`，没有滑动窗口版本。四个注意力类的替换分别位于 L220-L222、L230-L232、L240-L242、L247-L249。

**触发时机**（调用侧）：

[qwen-vl-finetune/qwenvl/train/train_qwen.py:L141-L143](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L141-L143)
补丁是**条件触发**的：只有 `data_flatten` 或 `data_packing` 开启（即使用拼接 collator）才替换注意力；常规 padding 训练走 transformers 原生 FA2 路径（0/1 掩码语义正确），不必替换。L143 紧接着关掉 `use_cache`（u5-l1 讲过梯度检查点的配套要求）。另外注意 [train_qwen.py:L28](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L28) 的 `from trainer import ...`：torchrun 以脚本方式执行 train_qwen.py 时，脚本所在目录会进入 `sys.path`，所以能按裸模块名 `trainer` 导入。

**import 即生效的模块级补丁**：

[qwen-vl-finetune/qwenvl/train/trainer.py:L494-L511](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py#L494-L511)
文件末尾还有一批**不带函数包装**的赋值语句：L495 `Trainer.create_optimizer = create_optimizer`（4.4 节主角），以及 L497-L511 把 `print_trainable_parameters` / `print_trainable_parameters_visual` 挂到四代模型共 8 个类上。这些语句在**模块被 import 的瞬间执行**——也就是说 train_qwen.py 第 28 行的那行 import，除了拿到 `replace_qwen2_vl_attention_class`，还顺手改写了进程内所有 `transformers.Trainer` 的优化器构建行为。两份诊断打印函数定义在 [L255-L313](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py#L255-L313)，调用点在 [train_qwen.py:L182-L184](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L182-L184)（非 LoRA 分支、rank 0 时打印 `model.visual` 与 `model.model` 的可训练状态；L182 直接调用 `torch.distributed.get_rank()`，这也是「单卡也必须 torchrun 启动」的原因之一，承接 u5-l1）。

区分两类补丁的生效时机，是读这个文件的关键：

| 补丁 | 生效时机 | 是否可选 |
| --- | --- | --- |
| `Trainer.create_optimizer` 替换 | `import trainer` 时立即 | 无条件 |
| 8 个类的 `print_trainable_parameters` 挂载 | `import trainer` 时立即 | 无条件 |
| 四代注意力 forward + 掩码函数替换 | `train()` 内显式调用 | 仅 `data_flatten or data_packing` 时 |

#### 4.3.4 代码实践：验证 monkey patch 的机制与效果

1. **实践目标**：分两层验证——机制层（类属性赋值如何影响实例行为）与效果层（trainer.py 的补丁是否真的装上了）。
2. **操作步骤**：
   - 第一层（任何 Python 环境可跑），新建 `patch_mechanism.py`（**示例代码**）：

     ```python
     # patch_mechanism.py —— 最小 monkey patch 演示（示例代码）
     class Attention:
         def forward(self, x):
             return "original"

     def patched_forward(self, x):   # 注意第一个参数：绑定后就是实例自己
         return "patched"

     a = Attention()
     print("before:", a.forward(1))
     Attention.forward = patched_forward   # 类属性赋值，影响所有实例（含已创建的）
     print("after: ", a.forward(1))
     ```

   - 第二层（需按 u4-l1 的 README 清单装好 transformers≥4.57 与 flash-attn，在 `qwen-vl-finetune` 目录下运行），新建 `check_patch.py`（**示例代码**）：

     ```python
     # check_patch.py —— 验证 trainer.py 的补丁是否生效（示例代码）
     import transformers
     from transformers.models.qwen3_vl import modeling_qwen3_vl as m

     import sys, pathlib
     sys.path.append(str(pathlib.Path("qwenvl/train").resolve()))
     import trainer   # 仅 import，即触发 L494-L511 的模块级补丁

     print("forward replaced:", m.Qwen3VLTextAttention.forward is trainer.qwen3vl_forward)
     print("mask short-circuited:", m.create_causal_mask is trainer.return_mask)
     print("optimizer replaced:", transformers.Trainer.create_optimizer is trainer.create_optimizer)
     # 注意: 此时尚未调用 replace_qwen2_vl_attention_class()，
     # 上面第 1、2 行若为 True，说明有其他代码已替换过；预期应为 False。
     trainer.replace_qwen2_vl_attention_class()
     print("after install, forward replaced:", m.Qwen3VLTextAttention.forward is trainer.qwen3vl_forward)
     print("after install, mask short-circuited:", m.create_causal_mask is trainer.return_mask)
     ```

3. **需要观察的现象**：机制层，实例 `a` 未重建但行为变了；效果层，`import trainer` 后优化器补丁已生效，而注意力补丁要等显式调用后才生效——对应 4.3.3 的表格。
4. **预期结果**：机制层输出 `before: original` / `after: patched`（纯 Python，确定）；效果层前三行预期 `False / False / True`，调用后两行预期 `True / True`。效果层依赖具体的 transformers / flash-attn 安装，**待本地验证**（若 import 报错，先核对 README 版本清单）。
5. 若暂无 GPU 环境，只跑第一层即可完成本实践的核心观察。

#### 4.3.5 小练习与答案

**练习 1**：`replace_qwen2_vl_attention_class` 实际替换了哪些类？
答案：4 个注意力类——`Qwen2VLAttention`、`Qwen2_5_VLAttention`（用 `qwen2vl_forward`）、`Qwen3VLTextAttention`、`Qwen3VLMoeTextAttention`（用 `qwen3vl_forward`）；另替换 Qwen2/2.5 的 `create_causal_mask` + `create_sliding_window_causal_mask`、Qwen3/MoE 的 `create_causal_mask`。函数名只提 qwen2_vl 是历史遗留。

**练习 2**：如果只替换 forward、不替换 `create_causal_mask`，会发生什么？
答案：transformers 原版掩码函数会按 0/1 padding 语义解释一维 int32 的 `cu_seqlens`，轻则形状广播报错，重则生成语义错误的 4-D 掩码；`return_mask` 的恒等返回让 cu_seqlens 原样穿透到注意力层，生产者-消费者契约才闭环。

**练习 3**：为什么注意力替换要写成函数、由 train_qwen.py 条件调用，而 `Trainer.create_optimizer` 替换直接写在模块级？
答案：注意力替换会改变掩码语义，只有拼接训练（flatten/packing）才能配套；常规 padding 训练必须保留原生行为，所以要按需开启。优化器分组对所有训练方式都无害（不传分层学习率时退化为标准两组），import 即装没有副作用。

### 4.4 create_optimizer：多参数组优化器改造

#### 4.4.1 概念说明

微调多模态模型时，不同部件的「学习率需求」不同：视觉塔（visual）是预训练好的感知特征，通常要小学习率轻调；merger（视觉-语言投影）参数少、任务相关性强，可给更大的学习率；语言模型主体用全局学习率。这就是**分层学习率**（layer-wise LR），实现方式是把参数按归属切成若干组，每组带独立 `lr` 交给 AdamW。

对应 u5-l2 讲过的两个参数：`mm_projector_lr`（merger 组）与 `vision_tower_lr`（visual 组）。本节看它们在 trainer 侧如何落地——并验证 u5-l2 留下的那个坑：**`vision_tower_lr` 嵌套在 `mm_projector_lr` 的判断里，只传前者不生效**。

#### 4.4.2 核心流程

```text
create_optimizer(self):                       # 替换 Trainer 同名方法
  若 self.optimizer 已存在: 直接返回（支持外部注入）
  decay_parameters = get_decay_parameter_names(model) 再剔除含 "bias" 的名字
  projector_parameters = 名字含 "merger" 的参数
  vision_tower_parameters = 名字含 "visual" 的参数
  分组:
    mm_projector_lr 有值 且 vision_tower_lr 有值 → 6 组:
        {decay, no_decay} × {其余, vision_tower, projector}
    仅 mm_projector_lr 有值 → 4 组（无 tower 拆分）
    都没有 → 标准 2 组（等价 HF 原生行为）
  每组: weight_decay = args.weight_decay（no_decay 组为 0），lr 缺省则用优化器默认
  optimizer_cls(**groups) 构建并返回
```

分组矩阵（6 组情形）：

| 组 | 参数条件 | weight_decay | lr |
| --- | --- | --- | --- |
| 1 | decay、非 merger、非 visual | `args.weight_decay` | 默认（`learning_rate`） |
| 2 | decay、非 merger、visual | `args.weight_decay` | `vision_tower_lr` |
| 3 | no-decay、非 merger、非 visual | 0.0 | 默认 |
| 4 | no-decay、非 merger、visual | 0.0 | `vision_tower_lr` |
| 5 | decay、merger | `args.weight_decay` | `mm_projector_lr` |
| 6 | no-decay、merger | 0.0 | `mm_projector_lr` |

#### 4.4.3 源码精读

**名称匹配与 decay 集合**：

[qwen-vl-finetune/qwenvl/train/trainer.py:L316-L330](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py#L316-L330)
L320 的 `if self.optimizer is None` 允许外部预置优化器（直接返回）；L321-L322 用 HF Trainer 的 `get_decay_parameter_names` 拿到「该做权重衰减」的名字集合，再显式剔除含 `bias` 的名字（双保险）；L324-L326 以子串 `"merger"` 圈出投影层参数，L328-L330 以子串 `"visual"` 圈出视觉塔参数——这两个子串与 [train_qwen.py:L69/L76](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L67-L80) 的 `model.visual` / `model.visual.merger` 访问路径一致。

**六组构造（两开关齐开）**：

[qwen-vl-finetune/qwenvl/train/trainer.py:L331-L412](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py#L331-L412)
每个字典一组，靠 `n in / not in projector_parameters / vision_tower_parameters` 与 `n in decay_parameters` 的组合筛选。**注意一个精妙的优先级**：merger 物理上位于 visual 内部（`model.visual.merger.xxx`），名字同时含 `"visual"` 与 `"merger"`。代码在 tower 组的条件里显式写了 `n not in projector_parameters`（L339、L366），保证 merger 参数优先落入 projector 组、不会被 tower 组抢走；而 projector 组（L386-L411）只看 `n in projector_parameters`。选组谓词的书写顺序实现了「投影层 > 视觉塔 > 其余」的优先级。

**嵌套分支与退化路径**：

[qwen-vl-finetune/qwenvl/train/trainer.py:L323-L465](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py#L323-L465)
外层 `if` 判断 `mm_projector_lr`（L323），**内层**才判断 `vision_tower_lr`（L327）——这就是 u5-l2 指出的坑的源码铁证：只传 `vision_tower_lr` 时代码直接走 L466 的 else，退化为标准两组，tower 学习率被静默忽略。L414-L465 是「只传 projector」的 4 组版本；L466-L484 是都不传的标准 2 组（与 HF 原生行为一致）。

**组装优化器**：

[qwen-vl-finetune/qwenvl/train/trainer.py:L486-L495](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py#L486-L495)
`Trainer.get_optimizer_cls_and_kwargs(self.args)` 按训练参数选出优化器类（默认 AdamW）与 kwargs（含全局 `lr`、`betas`）；没有写 `lr` 键的参数组自动落到这个全局 lr。实例化后赋给 `self.optimizer` 返回。而这一改造能被用上，靠的是 L495 的 `Trainer.create_optimizer = create_optimizer`——HF Trainer 训练循环调用 `self.create_optimizer()` 时就会进入我们的版本（其生效时机见 4.3 节的表格）。

与 LoRA 的关系（承接 u5-l2、延伸到 u5-l7）：LoRA 分支里基座模型全部 `requires_grad=False`，各组的 `p.requires_grad` 过滤条件会把冻结参数尽数排除；LoRA 适配器参数名（`lora_A/lora_B`）不含 `merger`/`visual` 子串，全部落入「其余」组，用全局学习率。

#### 4.4.4 代码实践：用分组谓词给参数名分类

1. **实践目标**：脱离 PyTorch，用纯 Python 复现「参数名 → 6 组」的判定逻辑，验证 merger 优先于 visual。
2. **操作步骤**：新建 `group_sim.py`（**示例代码**；参数名以真实 checkpoint 的 `model.named_parameters()` 为准，下列为典型形态）：

   ```python
   # group_sim.py —— 复现 create_optimizer 的分组谓词（示例代码）
   def group_of(name, mm_projector_lr=None, vision_tower_lr=None):
       decay = ("bias" not in name) and ("norm" not in name)  # 简化版 decay 判定
       is_projector = "merger" in name
       is_tower = "visual" in name
       if mm_projector_lr and vision_tower_lr:
           bucket = "projector" if is_projector else ("tower" if is_tower else "rest")
       elif mm_projector_lr:
           bucket = "projector" if is_projector else "rest"
       else:
           bucket = "rest"          # 嵌套陷阱: 只传 vision_tower_lr 也落这里!
       return bucket, ("decay" if decay else "no_decay")

   names = [
       "model.visual.blocks.0.attn.qkv.weight",
       "model.visual.merger.mlpr.0.weight",
       "model.language_model.layers.0.self_attn.q_proj.weight",
       "model.visual.blocks.0.attn.qkv.bias",
   ]
   for n in names:
       print(f"{n:55s} -> {group_of(n, mm_projector_lr=1e-5, vision_tower_lr=1e-6)}")
   print("只传 tower:", group_of(names[0], vision_tower_lr=1e-6))
   ```

3. **需要观察的现象**：第 2 个名字同时含 `visual` 与 `merger`，最终归入哪组；第 4 个名字（bias）decay 判定如何变化；最后一行「只传 tower」的结果。
4. **预期结果**（纯 Python，确定）：四个名字分别得到 `("tower","decay")`、`("projector","decay")`、`("rest","decay")`、`("tower","no_decay")`；最后一行为 `("rest","decay")`——证明只传 `vision_tower_lr` 不生效，与 L323/L327 的嵌套结构互相印证。若想看真实参数名，可在加载模型后打印 `[(n, p.requires_grad) for n, p in model.named_parameters()][:20]`（需 GPU 环境，**待本地验证**）。
5. 本实践核心部分无需任何依赖，可直接本地验证。

#### 4.4.5 小练习与答案

**练习 1**：名为 `model.visual.merger.mlpr.0.weight` 的参数落在哪个组？为什么不会被 `visual` 规则抢走？
答案：projector 组（lr = `mm_projector_lr`）。它虽然含 `"visual"`，但 tower 组的条件显式要求 `n not in projector_parameters`（L339/L366），投影层判定优先。

**练习 2**：六组里的 no-decay 组（weight_decay=0.0）服务什么参数？
答案：各类 bias 与 norm 权重等不应做权重衰减的参数——`get_decay_parameter_names` 集合之外（或被 L322 的 bias 过滤剔除）的名字。

**练习 3**：不传任何分层学习率时，这个被替换的 `create_optimizer` 与 HF 原生行为有何差别？
答案：走 L466-L484 的 else 分支，构造标准两组（decay / no-decay），与原生行为等价；差别只在多了一次 L322 的 bias 显式过滤（通常与原生集合判定结果一致）。也就是说这个替换对普通训练是「无害」的，分层能力按需启用。

## 5. 综合实践

把本讲四个模块串起来，制作一份 **《trainer.py 注释版 + 注意力泄漏演示》** 学习笔记。

**任务背景**：下一个同事要接手 flatten/packing 训练链路，你要用一份带逐行中文注释的代码 + 一段泄漏演示，让他 10 分钟看懂「collator 产出的张量如何被消费、不替换会出什么事」。

**步骤**：

1. 在个人笔记目录（仓库外或未跟踪目录，**切勿改动仓库源码**）新建 `trainer_annotated.py`，从 [trainer.py:L33-L108](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py#L33-L108) 复制 `flash_attention_forward`、从 [trainer.py:L215-L252](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/trainer.py#L215-L252) 复制 `replace_qwen2_vl_attention_class`，为**每一行**（不是每个函数）写中文注释。注释必须覆盖三个必答问题：
   - `cu_seqlens` 从哪来：引用 [data_processor.py:L615-L625](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L615-L625)——collator 把 `[0] + 各段长度` cumsum 成 int32 张量，经 `batch["attention_mask"]` 传递（L633）；flatten 路径的段 = 原始样本，packing 路径的段 = 桶内子样本。
   - `max_seqlen` 怎么算：相邻差分取最大再 `.item()`，语义是最长一段而非总长。
   - `causal=True` 的段内语义：块对角下三角，段间为零。
2. 在同一笔记里补一段**泄漏伪代码**（参考答案版，可直接抄录后按自己的话改写）：

   ```text
   场景: 样本 A 长 3、样本 B 长 2，被拼成一行 [a0 a1 a2 b0 b1]，cu_seqlens = [0, 3, 5]

   【未替换: 标准 causal 注意力（全局下三角）】
   allowed[i][j] = (j <= i)
   → b0(i=3) 能看到 a2/a1/a0; B 的每个 token 都读到了 A 的内容
   → 后果: B 的 labels 梯度流经 A 的 KV, 等价于「答案写在题目前面」, 监督信号被污染

   【已替换: flash_attn_varlen_func 按 cu_seqlens 分段】
   for (start, end) in [(0,3), (3,5)]:          # 段由 cu_seqlens 相邻对给出
       for i in range(start, end):
           for j in range(start, end):
               allowed[i][j] = (j <= i)          # 段内下三角; 段间恒为 False
   → b0 只能看到 b0; 与 A、B 各自独立训练完全等价
   ```

3. 用 4.1.4 的 `cu_seqlens_sim.py` 给同事现场演示一遍 `[12, 35, 28]` → `[0, 12, 47, 75]` / `35`。
4. 自检清单（全部能口头回答才算完成）：
   - [ ] 能指出 L85 是语义重载点（`cu_seqlens = attention_mask`）；
   - [ ] 能解释 `max_seqlen` 为何是 35 而非 75；
   - [ ] 能说出两套 forward 的两处结构差异（QK-Norm、RoPE 应用方式）；
   - [ ] 能解释 `return_mask` 为什么必须存在；
   - [ ] 能说出 merger 参数为何不会掉进 vision tower 组；
   - [ ] 能写出注意力补丁的触发条件（`data_flatten or data_packing`）。

**预期结果**：产出一份 150 行以内、每行有注释的 `trainer_annotated.py` 与一段泄漏伪代码；六项自检全部通过。整个实践为源码阅读型，不需要 GPU；4.1.4 的演示脚本可当场运行验证。

## 6. 本讲小结

- 拼接训练（flatten/packing）必须配套替换注意力：默认全局因果掩码会让后一样本看到前一样本的答案（注意力泄漏），`flash_attn_varlen_func` 按 `cu_seqlens` 分段做段内因果注意力，把注意力矩阵变成块对角下三角。
- `flash_attention_forward`（L33-L108）是消费端核心：`squeeze(0)` 去掉恒为 1 的 batch 维，把 `attention_mask` 语义重载为 `cu_seqlens`（L85），`max_seqlen` 由相邻差分最大值得出（L87-L93）。
- 两套整层替身按代际分工：`qwen2vl_forward`（无 QK-Norm、`apply_multimodal_rotary_pos_emb` + `mrope_section`）与 `qwen3vl_forward`（有 QK-Norm、交错式 MRoPE 直接 `apply_rotary_pos_emb`），最终都汇入同一个 varlen 调用。
- `replace_qwen2_vl_attention_class`（L215-L252，名字是历史遗留）跨四代模型替换 4 个注意力类的 `forward`，并用恒等函数 `return_mask` 短路 `create_causal_mask`，让 cu_seqlens 原样穿透；补丁仅在 `data_flatten or data_packing` 时由 [train_qwen.py:L141-L142](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/train/train_qwen.py#L141-L142) 触发，而 `Trainer.create_optimizer` 替换在 `import trainer` 时即生效（L495）。
- `create_optimizer`（L316-L491）按「decay × {其余, visual, merger}」最多构造 6 个参数组实现分层学习率；merger 判定优先于 visual（tower 组条件显式排除 projector），且 `vision_tower_lr` 嵌套在 `mm_projector_lr` 判断内——只传前者会被静默忽略。

## 7. 下一步学习建议

- **u5-l7（LoRA 微调与分层学习率）**：本讲 `create_optimizer` 的使用方——带上 `mm_projector_lr` / `vision_tower_lr` 跑 `sft_30a3b_lora.sh`，观察 `p.requires_grad` 过滤如何与 LoRA 冻结协同；同时留意 `flash_attention_forward` L67-L80 那段为 PEFT fp32 LayerNorm 准备的 dtype 处理。
- **u5-l8（DeepSpeed 配置与多规格脚本）**：注意力替换与优化器分组在不同 ZeRO 档位下的行为一致性；MoE 模型只能用 zero2 的限制。
- **延伸阅读**（仓库外）：flash-attn 库的 `flash_attn_varlen_func` 文档与源码（`cu_seqlens`/`max_seqlen` 的 kernel 语义）；transformers 的 `modeling_flash_attention_utils.py`（官方 FA2 集成如何从 0/1 掩码推导 varlen 输入，对比理解本仓库为何选择整层替换）。
- **回顾**：如果对 `cu_seqlens` 的生产端还有疑问，回到 u5-l5 重读 `FlattenedDataCollatorForSupervisedDataset`（[data_processor.py:L604-L635](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L604-L635)）与 `_get_packed_item`（[L442-L472](https://github.com/QwenLM/Qwen3-VL/blob/96588727e44c78b25ba03ea03b8e12f7e64fd0da/qwen-vl-finetune/qwenvl/data/data_processor.py#L442-L472)）。
