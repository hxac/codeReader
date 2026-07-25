# Attention 模块与 MLA

> 本讲属于「注意力机制」单元（u6）的第一讲，承接 u3-l3（ModelEngine 与模型前向）。

## 1. 本讲目标

在 u3-l3 里，我们把模型前向里那块「黑盒」打开了一半：`PyTorchModelEngine.forward` 把请求翻译成 `inputs` 张量字典，再交给 `self.model.forward`。但模型 forward 内部，每一层 decoder layer 调用的那个 `Attention.forward(...)` / `MLA.forward(...)` 到底做了什么、它和谁配合、需要遵守什么契约——本讲就来回答这些问题。

学完后你应该能够：

1. 说清楚 `Attention(nn.Module)` 这个模块「外壳」的职责：它不只是算注意力分数，还负责 QKV 投影、TP/CP 切分、RoPE、输出投影等一整圈围绕「后端调用」的逻辑。
2. 区分**标准注意力**（`Attention`，MHA/GQA/MQA）与**多头潜注意力**（`MLA`）在输入和计算路径上的本质差异，理解 MLA 的「权重吸收（absorption）」技巧为何能省显存。
3. 掌握注意力栈赖以运转的 **metadata 契约**：`AttentionMetadata`（批次级状态）、`AttentionForwardArgs`（每次前向的可选项）、`KVCacheParams`（KV 缓存描述）三者各管什么、由谁创建。
4. 在动手改 `attention.py` / `mla.py` 之前，知道有哪些**必须遵守的契约**（出自 `ATTENTION_DEVELOPER_GUIDE.md`），避免踩坑。

## 2. 前置知识

本讲默认你已经读过 u3-l3，知道一次前向的输入是「拍平的 token 张量」（第一维是 token 总数，不是 `[batch, seq]`），且每个 decoder layer 会拿到一个 `attn_metadata` 对象。此外需要以下基础概念：

- **注意力（Attention）** 的核心公式。对于单头：
  \[
  \mathrm{Attn}(Q,K,V)=\mathrm{softmax}\!\left(\frac{QK^{\top}}{\sqrt{d_k}}\right)V
  \]
  其中 \(Q\) 是查询、\(K\) 是键、\(V\) 是值，\(d_k\) 是每个头的维度。多头注意力（MHA）就是把隐藏向量切成多个头并行算多组 \(Q/K/V\)。

- **GQA / MQA**：MHA 每个头都有独立的 \(K,V\)；**分组查询注意力（GQA）**让多个查询头**共享**一组 \(K,V\)（`num_key_value_heads < num_heads`）；**多查询注意力（MQA）**更极端，所有头共享**一组** \(K,V\)（`num_key_value_heads = 1`）。共享越多，KV 缓存越小，但表达力也越弱。代码里 `num_heads` 是查询头数，`num_key_value_heads` 是 KV 头数，二者之比 `num_key_value_groups` 就是共享比。

- **RoPE（旋转位置编码）**：把位置信息以旋转矩阵的形式叠加到 \(Q,K\) 上。它有「融合（fused）」和「非融合（unfused）」两种实现：融合指在注意力 kernel 内部顺手做 RoPE；非融合指在调用 kernel 之前先用一个单独的 `RotaryEmbedding` 模块做。代码里用 `rope_fusion` 开关控制。

- **KV 缓存（KV cache）**：解码阶段，前面生成过的 token 的 \(K,V\) 不必每步重算，而是缓存起来复用。TRT-LLM 的 KV 缓存是**分页（paged）**的——把缓存切成固定大小的块（block/page），按需分配，类似操作系统的虚拟内存分页。

- **低秩分解（low-rank）**：把一个大矩阵近似成两个小矩阵的乘积 \(W \approx A B\)，其中 \(A\) 是「瘦高」、\(B\) 是「矮宽」。MLA 正是用它把 KV 压缩到一个低维「潜变量」上，从而大幅缩小缓存。

> 术语提示：本讲反复出现的 `attn_metadata` 是一个**运行时对象**，每一步前向都由引擎创建一次，传给所有层；而 `AttentionForwardArgs` 是**每次前向、每层**都会构造的小包裹，承载输出 buffer、scale、mask 等即时信息。务必把这两者分开。

## 3. 本讲源码地图

本讲涉及的关键文件及其作用：

| 文件 | 作用 |
|---|---|
| [_torch/modules/attention.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/attention.py) | 标准 `Attention(nn.Module)` 模块：QKV 投影、RoPE、后端调用、输出投影，以及 Helix 上下文并行（CP）的公共辅助函数。 |
| [_torch/modules/mla.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/mla.py) | `MLA(nn.Module)` 模块：低秩 Q/KV 分解、权重吸收路径、DSA/DeepSeek-V4 稀疏 MLA 派发。 |
| [_torch/modules/ATTENTION_DEVELOPER_GUIDE.md](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/ATTENTION_DEVELOPER_GUIDE.md) | 注意力栈的开发契约与分层模型。**改 attention 前必读。** |
| [_torch/metadata.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/metadata.py) | KV 缓存描述：`KVCacheParams` 与 `CacheType`（LINEAR/PAGED/PER_TOKEN）。 |

> **关于「metadata 契约」的一个重要澄清**：注意力运行时真正消费的 `AttentionMetadata` 基类、`AttentionForwardArgs`、`AttentionBackend` 基类其实定义在 [_torch/attention_backend/interface.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/interface.py) 里（它 `from ..metadata import KVCacheParams` 把 KV 缓存描述接进来）。本讲的「metadata 契约」模块会同时涉及这两个文件——`metadata.py` 管 KV 缓存怎么描述，`interface.py` 管注意力这一步需要哪些状态。后端家族（TRTLLM/Vanilla/FlashInfer）本身则留到 u6-l2 详讲。

---

## 4. 核心概念与源码讲解

TRT-LLM 的注意力不是「一个函数」，而是一条**四层栈**（见 [ATTENTION_DEVELOPER_GUIDE.md:L35-L53](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/ATTENTION_DEVELOPER_GUIDE.md#L35-L53)）：

1. **模块层（module wrapper）**：`Attention` 或 `MLA`，负责围绕后端的逻辑。
2. **后端层（backend）**：由 `config.attn_backend` 选出的具体实现类（TRTLLM / Vanilla / FlashInfer）。
3. **运行时契约（metadata + buffers）**：metadata 子类型与每步 buffer。
4. **KV 缓存语义**：谁拥有缓存、什么布局、如何追加。

本讲的三个最小模块分别对应第 1 层的两类模块，以及把第 1、2 层粘起来的第 3 层契约。

### 4.1 Attention 模块（标准注意力外壳）

#### 4.1.1 概念说明

`Attention(nn.Module)` 是**绝大多数 decoder-only 模型**（Llama、Qwen、Mistral……）每一层里那个「注意力子层」。但它**不是**单纯的「算 \(\mathrm{softmax}(QK^\top)V\)」——那部分由**后端**（backend）干。模块层负责的是**围绕后端调用的一整圈逻辑**（见 [ATTENTION_DEVELOPER_GUIDE.md:L56-L93](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/ATTENTION_DEVELOPER_GUIDE.md#L56-L93)）：

- **QKV 投影**与**输出投影**：把隐藏向量映射成 \(Q,K,V\)，再把注意力输出映射回隐藏维度。
- **张量并行（TP）/上下文并行（CP）** 的切分与映射设置。
- **融合 / 拆分 QKV** 的处理：后端可能要求一次喂入融合好的 QKV 张量，也可能要求拆开的 Q、K、V。
- **可选的非融合 RoPE**、**可选的输出门控（output gate）**、**可选的 LoRA 注入**。
- 把 mask、sink、输出 buffer 等每步选项**收集进 `AttentionForwardArgs`**，连同 \(Q,K,V\) 和 metadata 一起喂给后端。

用一句话概括设计意图：**「模块层做投影与编排，后端做分数计算与 KV 缓存读写」**。所以判断一个新模型能不能用现有 `Attention`，先问「我需要的额外处理（Q/K 归一化、特殊缩放、门控等）能不能只靠模块层的扩展点解决，而不必动后端」。

#### 4.1.2 核心流程

模块层的高层数据流（出自 [ATTENTION_DEVELOPER_GUIDE.md:L72-L82](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/ATTENTION_DEVELOPER_GUIDE.md#L72-L82)）：

```text
hidden_states
  -> qkv_proj            # 融合 QKV 投影（一个 Linear）
  -> 可选 LoRA
  -> 可选 gate split
  -> 可选非融合 RoPE
  -> 融合/拆分 QKV 转换   # convert_qkv：按后端要求统一格式
  -> backend.forward(...) # self.attn.forward(...)，真正算注意力 + KV 缓存读写
  -> 可选 output gate
  -> o_proj              # 输出投影（含 TP all-reduce）
```

关键点：`convert_qkv` 是格式适配器——后端支持融合 QKV（`support_fused_qkv()`）就把 Q/K/V 拼回去，否则拆开。这样上层逻辑可以写成「拿到 qkv 后统一过一遍预处理」，而不必为每个后端写分支。

伪代码（简化）：

```python
def forward(position_ids, hidden_states, attn_metadata, ...):
    qkv = self.qkv_proj(hidden_states)          # 融合投影
    q, k, v, gate = self.preprocess_qkv(qkv, position_ids)  # RoPE + 拆分
    q, k, v = self.convert_qkv(q, k, v)         # 按后端要求融合或保持拆分
    attn_output = self.forward_impl(q, k, v, attn_metadata, ...)
    if self.attn_output_gate:
        attn_output = self.apply_output_gate(attn_output, gate)
    return self.o_proj(attn_output)             # 输出投影（含 all-reduce）
```

#### 4.1.3 源码精读

**类定义与构造**：`Attention` 是一个 `nn.Module`，构造时建好两个投影和一个后端实例。

- 类定义：[attention.py:L382-L383](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/attention.py#L382-L383) —— `class Attention(nn.Module)`。
- **融合 QKV 投影** `self.qkv_proj`：[attention.py:L545-L562](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/attention.py#L545-L562)。它用列并行（`TensorParallelMode.COLUMN`）的 `Linear`，并声明权重是 `FUSED_QKV_LINEAR` 融合格式，所以加载 HF checkpoint 时会把分离的 q/k/v 权重拼成一块。
- **输出投影** `self.o_proj`：[attention.py:L580-L596](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/attention.py#L580-L596)，行并行，负责跨 TP rank 的 all-reduce。
- **后端实例** `self.attn = create_attention(...)`：[attention.py:L664-L679](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/attention.py#L664-L679)。这里把 `config.attn_backend` 和（可选的）`sparse_params` 交给工厂函数，得到一个实现了 `AttentionBackend` 接口的对象。后端**不带可训练权重**，只有和量化相关的状态。
- 紧接着查询后端能力 `self.support_fused_qkv = self.attn.support_fused_qkv()`：[attention.py:L681](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/attention.py#L681)，决定后续 `convert_qkv` 的走向。

**前向入口** `forward`：[attention.py:L977-L1071](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/attention.py#L977-L1071)。核心几行（保留关键部分）：

```python
hidden_states = _helix_cp_allgather_input(...)        # Helix CP: 非首层先 allgather
qkv = self.qkv_proj(hidden_states)                    # 融合 QKV 投影
if bool(lora_params):                                 # 可选 LoRA
    ...
q, k, v, gate = self.preprocess_qkv(qkv, position_ids)  # 拆分 + RoPE
q, k, v = self.convert_qkv(q, k, v)                   # 适配后端格式
attn_output = self.forward_impl(q, k, v, attn_metadata, ...)  # 调后端
if self.attn_output_gate:
    attn_output = self.apply_output_gate(attn_output, gate)
attn_output = _helix_cp_output_projection(self.o_proj, ...)  # o_proj（含 all-reduce）
return attn_output
```

- `qkv = self.qkv_proj(hidden_states)`：[attention.py:L1012](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/attention.py#L1012)。
- 拆分 + RoPE：`self.preprocess_qkv`，[attention.py:L702-L729](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/attention.py#L702-L729)。它先按需切出 output gate，再调 `apply_rope`。
- 格式适配：`self.convert_qkv`，[attention.py:L738-L744](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/attention.py#L738-L744)——`k,v` 为 `None` 且后端不支持融合时拆开，反之拼合。
- 调后端：`self.forward_impl`，[attention.py:L1048-L1061](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/attention.py#L1048-L1061)。

**RoPE 扩展点** `apply_rope`：[attention.py:L1073-L1092](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/attention.py#L1073-L1092)。注意它只在 `rope_fusion=False`（即后端不做融合 RoPE）时才真正施加旋转，否则原样返回——把 RoPE 留给后端在 kernel 内部做。子类（如某些带 QK-norm 的模型）常覆盖 `apply_rope`，在里面顺带做归一化。

**真正调用后端**的地方在 `_attn_impl`：[attention.py:L791-L902](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/attention.py#L791-L902)。最关键的一行：

```python
attn_output = self.attn.forward(
    q, k, v, attn_metadata,
    forward_args=AttentionForwardArgs(out_scale=..., kv_scale_orig_quant=...,
                                      attention_mask=..., output=..., ...))
```

见 [attention.py:L876-L896](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/attention.py#L876-L896)。这里把所有「这次前向才有的即时张量」打包进 `AttentionForwardArgs`，连同 \(Q,K,V\) 和 `attn_metadata` 一并交给后端。**模块层与后端之间唯一的正式数据通道就是 `(q, k, v, metadata, forward_args)` 这一串**——这就是契约。

> 旁支：`forward_impl`（[attention.py:L904-L975](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/attention.py#L904-L975)）会在 `torch.compile` 场景下把整段注意力包成自定义算子 `trtllm::attn_custom_op_inplace`（[attention.py:L73-L115](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/attention.py#L73-L115)）以兼容 CUDA Graph 捕获，否则直接走 `_attn_impl`。这块留到 u10-l4（CUDA Graph）详讲，这里只要知道「殊途同归于 `self.attn.forward`」即可。

#### 4.1.4 代码实践

**实践目标**：在不运行模型的前提下，通过阅读源码把「模块层 → 后端」的调用边界画清楚，并验证模块层确实「不碰」真正的注意力分数计算。

**操作步骤**：

1. 打开 [attention.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/attention.py)，定位 `forward`（L977）。
2. 顺着调用链走一遍：`qkv_proj`（L1012）→ `preprocess_qkv`（L1038）→ `convert_qkv`（L1039）→ `forward_impl`（L1048）→ `_attn_impl`（L791）→ `self.attn.forward`（L876）。
3. 在 `_attn_impl` 里找：除了 `self.attn.forward(...)` 这一处，有没有**任何**地方真正做 \(QK^\top\) 或 softmax？预期：**没有**。模块层只做切片（`q[:num_tokens, :]`）、拼 `AttentionForwardArgs`、量化 scale 的挑选。
4. 用一句话记录：模块层在 `self.attn.forward` **之前**做了哪 5 件事、**之后**做了哪 2 件事（答案见下方小练习）。

**需要观察的现象**：模块层的逻辑全部是「张量搬运 + 投影 + 选项打包」，不含任何概率/归一化运算；所有重计算都在 `self.attn.forward` 内部（即后端）。

**预期结果**：你能画出一条清晰的「模块层（Python 编排）→ `self.attn.forward`（后端，可能 C++ kernel）」分界线，这正是 u2-l3 提出的「Python 调度、C++ 加速」在注意力上的具体落点。

> 本实践为**源码阅读型**，无需 GPU；运行结果「待本地验证」仅在你真跑模型时才需要。

#### 4.1.5 小练习与答案

**练习 1**：模块层的 `apply_rope` 在什么条件下才会真正施加 RoPE？为什么？

> **答案**：仅当 `self.rope_fusion` 为 `False`（即后端**不**支持融合 RoPE）时才施加，见 [attention.py:L1089-L1092](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/attention.py#L1089-L1092)。若后端支持融合 RoPE（`support_fused_rope()` 为真），RoPE 会在后端 kernel 内部与注意力一并完成，模块层就不重复做，省一次访存。

**练习 2**：`convert_qkv` 的作用是什么？它依赖哪个后端能力钩子？

> **答案**：它把 `(q, k, v)` 在「融合（三者拼成一张 QKV 张量，k/v 为 None）」与「拆分（三者各自独立）」两种格式间转换，使上层逻辑无须为每个后端写分支，见 [attention.py:L738-L744](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/attention.py#L738-L744)。它依赖后端能力钩子 `support_fused_qkv()`（在 [attention.py:L681](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/attention.py#L681) 缓存到 `self.support_fused_qkv`）。

**练习 3（对照实践步骤 4）**：模块层在 `self.attn.forward` 之前/之后分别做了哪些事？

> **答案**：**之前** 5 件事——CP allgather 输入、`qkv_proj` 融合投影、LoRA、`preprocess_qkv`（拆分+RoPE）、`convert_qkv`（格式适配）；**之后** 2 件事——可选 output gate、`o_proj` 输出投影（含 TP all-reduce）。真正的分数计算不在模块层。

---

### 4.2 MLA（多头潜注意力）

#### 4.2.1 概念说明

`MLA(nn.Module)` 是 DeepSeek 系（DeepSeek-V2/V3、Kimi-K2.5、GLM-5 等）采用的注意力。它和标准 `Attention` **共用同一套后端系统**，但模块层的投影逻辑完全不同（见 [ATTENTION_DEVELOPER_GUIDE.md:L95-L121](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/ATTENTION_DEVELOPER_GUIDE.md#L95-L121)）。

**它解决的核心问题是 KV 缓存太大**。标准 MHA 里，每个 token 要缓存 \(2 \times n_h \times d\) 个元素（K 和 V 各一份，每个头都有）。DeepSeek 的头数动辄上百（如 128 头），缓存会非常可观。MLA 的思路是**低秩压缩**：

1. 先用一个「下投影」`kv_a_proj_with_mqa` 把隐藏向量压成低维的**潜 KV** `compressed_kv`（维度 `kv_lora_rank`，如 512），外加一小段专门给 RoPE 用的 `k_pe`（维度 `qk_rope_head_dim`，如 64）。
2. 缓存里**只存这个压缩后的潜表示**，于是每 token 缓存量从 \(2 n_h d\) 降到约 `kv_lora_rank + qk_rope_head_dim`。
3. 需要真正算注意力时，再用「上投影」`kv_b_proj` 把潜 KV 解压回完整的 \(K,V\)。

但第 3 步有个性能陷阱：**解码阶段**每个 token 都要对**所有历史 token**算注意力，如果每次都把缓存里的潜 KV 解压成完整 K/V，开销很大。MLA 的妙招是**权重吸收（absorption）**：把上投影矩阵 \(W_{UK}\) 「吸收」进查询——即先算 \(Q' = Q_{\text{nope}} W_{UK}\)，让 \(Q'\) 生活在潜空间，于是注意力可以直接在潜空间里算 \(Q' \cdot c_{kv}^{\top}\)，**再也不必解压缓存里的 KV**。代价是每层多做两个小批量矩阵乘（BMM），换来解码时极小的 KV 读取代价。

> 两种投影布局：`MLA` 有 **non-lite**（`is_lite=False`，有独立的 Q 低秩压缩 `q_a_layernorm`/`q_b_proj`）与 **lite**（`is_lite=True`，无独立 Q 压缩）两种，见 [mla.py:L486-L490](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/mla.py#L486-L490)。`is_lite` 改变的是投影**结构**，不是小分支——所以测试时 lite 与 non-lite 要分别测。

#### 4.2.2 核心流程

MLA 在一个混合批次里要**分别处理 context（prefill）与 generation（decode）**，因为二者走的路径不同：

```text
hidden_states
  -> kv_a_proj_with_mqa   # 下投影：得 q(非lite) / compressed_kv / k_pe
  -> layernorm(q_a) / layernorm(kv_a)
  -> q_b_proj             # 上投影得到完整 Q（含 nope + rope 两段）
  --------
  context 路径（num_contexts>0）:
     -> kv_b_proj 展开潜 KV 成完整 K,V   # context 一次性展开更划算
     -> self.mha.forward(q, k, v, ...)   # 标准 MHA 后端
  generation 路径（num_generations>0）:
     -> 权重吸收: q_nope @ k_b_proj_trans => fused_q（潜空间）
     -> self.mqa.forward(fused_q, None, None, ...)  # MQA 后端，直接吃潜 KV 缓存
     -> 输出吸收: attn_out_latent @ v_b_proj => attn_output
  -> o_proj
```

为什么 context 走「展开」、generation 走「吸收」？因为 prefill 时序列长、算力密集，展开成完整 K/V 用标准注意力更高效；decode 时每步只有 1 个 token 但要扫全部历史，吸收路径省下的 KV 读取才是赢家。这是 MLA「双路径」设计的根本原因。

简化数学（generation 吸收路径）：

记压缩潜 KV 为 \(c\in\mathbb{R}^{d_c}\)，上投影 \(K = c\,W_{UK}\)（每个头）。标准做法是 \(Q K^\top = Q\,W_{UK}\,c^\top\)，先展开再乘。吸收做法是令 \(Q' = Q\,W_{UK}\)，则 \(Q K^\top = Q' c^\top\)，注意力在维度 \(d_c\) 上计算：

\[
\mathrm{Attn}_{\text{MLA}} = \mathrm{softmax}\!\left(\frac{Q'_{\text{nope}}\,c^\top + Q_{\text{pe}}\,k_{\text{pe}}^\top}{\sqrt{d_{\text{head}}}}\right),\qquad Q'_{\text{nope}} = Q_{\text{nope}} W_{UK}
\]

输出侧同理吸收 \(W_{UV}\)：注意力产出潜空间结果后再乘 \(W_{UV}^\top\) 还原到 value 空间。

#### 4.2.3 源码精读

**类定义与构造**：[mla.py:L410-L411](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/mla.py#L410-L411)。构造函数签名里那串维度参数正是 MLA 的命脉：

- `qk_nope_head_dim` / `qk_rope_head_dim`：每个 query/key 头里「不带 RoPE」与「带 RoPE」两段的维度，二者相加得 `qk_head_dim`，见 [mla.py:L471-L473](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/mla.py#L471-L473)。
- `q_lora_rank` / `kv_lora_rank`：Q、KV 的低秩压缩维度，见 [mla.py:L475-L476](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/mla.py#L475-L476)。

**关键投影与后端**（non-lite 分支）：

- 下投影 `kv_a_proj_with_mqa`：[mla.py:L595-L606](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/mla.py#L595-L606)，输出维度 `q_lora_rank + kv_lora_rank + qk_rope_head_dim`，一次切出 q、compressed_kv、k_pe 三段。
- Q 上投影 `q_b_proj`：[mla.py:L612-L625](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/mla.py#L612-L625)。
- KV 上投影 `kv_b_proj`（context 展开用）：[mla.py:L669-L682](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/mla.py#L669-L682)。
- **MQA 后端** `self.mqa = create_attention(..., num_kv_heads=1, is_mla_enable=True, ...)`：[mla.py:L775-L797](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/mla.py#L775-L797)。注意 `num_kv_heads=1`——MLA 在潜空间里等价于单 KV 头（所有 query 头共享一份潜缓存），并把 MLA 维度参数透传给后端。
- **可选的稠密 MHA 后端** `self.mha = create_attention(...)`：[mla.py:L848-L866](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/mla.py#L848-L866)，供非 DSA 模型的 context 路径与短序列优化使用；DSA/DeepSeek-V4 场景下为 `None`。

**前向入口** `forward`：[mla.py:L3056-L3063](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/mla.py#L3056-L3063)。签名与标准 `Attention` 类似（同样吃 `position_ids, hidden_states, attn_metadata`），但内部按模型变体派发到三条实现路径（见 [mla.py:L3070-L3150](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/mla.py#L3070-L3150)）：

- 标准 MLA → `forward_impl`（[mla.py:L1398](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/mla.py#L1398)）；
- DSA（DeepSeek 稀疏注意力）→ `forward_impl_with_dsa`（[mla.py:L1507](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/mla.py#L1507)）；
- DeepSeek-V4 → `forward_impl_with_deepseek_v4`（[mla.py:L1727](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/mla.py#L1727)）。

为兼容 `torch.compile` / CUDA Graph，这些实现都被包成自定义算子（如 `trtllm::mla_custom_op_inplace`，[mla.py:L159-L219](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/mla.py#L159-L219)），通过 `layer_idx_str` 从 `config.extra_attrs["mla_layers"]` 反查回模块实例（见 [mla.py:L130-L133](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/mla.py#L130-L133)）。

**下投影与切分**（`forward_impl` 内）：[mla.py:L1433-L1435](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/mla.py#L1433-L1435)——

```python
q, compressed_kv, k_pe = self.kv_a_proj_with_mqa(hidden_states).split(
    [self.q_lora_rank, self.kv_lora_rank, self.qk_rope_head_dim], -1)
```

随后按 `num_contexts` / `num_generations` 把批次切成两半，分别走 context 与 generation 路径（[mla.py:L1459-L1505](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/mla.py#L1459-L1505)）。

**context 展开路径** `forward_context_default`：[mla.py:L1994-L2039](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/mla.py#L1994-L2039)。这里老老实实用 `kv_b_proj` 把 `compressed_kv` 展开成完整 K/V，再调 `self.mha.forward(q, k, v, attn_metadata, forward_args=...)`——和标准注意力几乎一样，只是输入来源是「解压后的潜 KV」。

**generation 吸收路径** `forward_absorption_generation`：[mla.py:L2504-L2520](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/mla.py#L2504-L2520)。先把 Q 拆成 nope/pe 两段，然后做权重吸收的 BMM（BF16 分支见 [mla.py:L2597-L2617](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/mla.py#L2597-L2617)）：

```python
# q_nope [num_heads, num_tokens, qk_nope_head_dim]
#  x k_b_proj_trans [num_heads, kv_lora_rank, qk_nope_head_dim]
#  -> q_nope_out [num_heads, num_tokens, kv_lora_rank]  ← Q 被吸收进潜空间
self._bmm_bf16_out(q_nope_t, self.k_b_proj_trans,
                   self.k_b_proj_trans.transpose(1, 2), q_nope_out)
```

吸收后的 `fused_q` 拼上 RoPE 段，喂给 MQA 后端（[mla.py:L2659-L2681](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/mla.py#L2659-L2681)，经 `_attn_forward_gen` → `self.mqa.forward`）。注意它传给后端的是 `fused_q` 和 `None, None`（k/v 为 None）——因为 KV 直接从分页潜缓存里读，由后端内部处理。最后再做输出吸收 `attn_out_latent @ v_b_proj`（[mla.py:L2710-L2718](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/mla.py#L2710-L2718)）。

> MLA 的潜缓存语义和标准 KV 缓存不同：分页缓存里存的是**潜状态**而非分离的 K/V，因此 `kv_factor=1`（每 token 一个潜张量），而标准稠密注意力 `kv_factor=2`（K、V 分两平面）。详见 [ATTENTION_DEVELOPER_GUIDE.md:L229-L245](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/ATTENTION_DEVELOPER_GUIDE.md#L229-L245) 与 [L274-L283](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/ATTENTION_DEVELOPER_GUIDE.md#L274-L283)。这块涉及 KV cache manager（u7-l1），本讲点到为止。

#### 4.2.4 代码实践

**实践目标**：对比 `Attention` 与 `MLA` 的前向输入差异，亲手在源码里定位「吸收」发生的位置。

**操作步骤**：

1. 打开 `Attention.forward`（[attention.py:L977](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/attention.py#L977)）与 `MLA.forward`（[mla.py:L3056](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/mla.py#L3056)），对比**二者的输入张量**：标准注意力喂给后端的是显式 `(q, k, v)`；MLA 的 generation 路径喂给后端的是 `(fused_q, None, None)` + `latent_cache`（见 [mla.py:L2659-L2663](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/mla.py#L2659-L2663)）。
2. 在 `forward_absorption_generation`（[mla.py:L2504](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/mla.py#L2504)）里找到把 \(W_{UK}\) 吸收进 Q 的那个 BMM（[mla.py:L2606-L2617](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/mla.py#L2606-L2617)），确认它用到的权重是 `self.k_b_proj_trans`（`kv_b_proj` 转置后的视图）。
3. 对比 context 路径 `forward_context_default`（[mla.py:L2008](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/mla.py#L2008)）：这里**没有**吸收 BMM，而是直接 `kv = self.kv_b_proj(compressed_kv)` 展开成完整 K/V。

**需要观察的现象**：同一个 `MLA` 模块，context 与 generation 走**两条数学上等价但实现迥异**的路径——一个展开 KV，一个吸收进 Q。

**预期结果**：你能填出下表（答案见小练习 2）：

| 维度 | 标准 Attention | MLA（generation） |
|---|---|---|
| 喂给后端的 k/v | 显式张量 | `None`（用 `latent_cache`） |
| 缓存内容 | K、V 两平面 | 单个潜张量 |
| 解码时是否展开历史 KV | — | 否（已吸收） |

> 本实践为源码阅读型；运行结果「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：MLA 为什么在 generation 用「权重吸收」、在 context 用「展开」？

> **答案**：context（prefill）序列长、算力密集，展开成完整 K/V 走标准 MHA 更高效；generation（decode）每步只产 1 个 token 却要扫全部历史 KV，吸收路径把 \(W_{UK}\) 折进 Q，使注意力在低维潜空间计算，免去对历史缓存的反复展开，读写代价更低。

**练习 2（对照实践步骤 3 的表格）**：填空——标准 Attention 喂给后端的 k/v 是 ___；MLA generation 喂给后端的 k/v 是 ___。

> **答案**：标准 Attention 是**显式 `(k, v)` 张量**；MLA generation 是 **`None, None`**（KV 走 `latent_cache` 由后端从分页潜缓存读）。见 [attention.py:L876-L880](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/attention.py#L876-L880) 与 [mla.py:L2659-L2663](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/mla.py#L2659-L2663)。

**练习 3**：MLA 构造时给 MQA 后端传了 `num_kv_heads=1`，这和「多头」矛盾吗？

> **答案**：不矛盾。MLA 把所有 query 头的 KV 压成一个共享的潜表示，在潜空间里等价于**单 KV 头**（所有 query 头共享同一份潜缓存），所以后端按 `num_kv_heads=1` 创建；真正的「多头」体现在 query 侧（`num_heads_tp` 个查询头）和吸收后的 BMM 维度上。见 [mla.py:L775-L779](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/mla.py#L775-L779)。

---

### 4.3 metadata 契约

#### 4.3.1 概念说明

模块层和后端要协作，就得有一份**契约**规定「我给你什么、你保证什么」。TRT-LLM 把这份契约拆成三个对象（见 [ATTENTION_DEVELOPER_GUIDE.md:L186-L211](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/ATTENTION_DEVELOPER_GUIDE.md#L186-L211)）：

1. **`AttentionMetadata`**：**批次级、每步一份**的运行时状态。由引擎在每步前向创建一次，传给**所有层**共享。内容包括：这一步有几个请求、每个序列多长、哪些是 context 哪些是 generation、KV 缓存管理器、是否 CUDA Graph、并行 mapping 等。
2. **`AttentionForwardArgs`**：**每层、每次前向**的即时可选项。由模块层在调后端前现造一个，内容包括：输出 buffer、量化 scale、mask 类型、MLA 专用 buffer（`latent_cache`/`q_pe`/`cu_q_seqlens`）、稀疏输入等。
3. **`KVCacheParams`**（在 `metadata.py`）：描述 KV 缓存「怎么用、怎么分页」——是否用缓存、每序列缓存了多少 token、每序列的 block id 列表、最大注意力窗口、sink token 数等。

**为什么要把 metadata 拆成「批次级」和「每前向级」两层？** 因为批次级状态（序列长度、缓存管理器）对所有层都一样、每步才变一次，适合复用；而每前向的输出 buffer、scale 是逐层不同的，必须每次构造。混在一起既浪费又会引发别名（aliasing）bug。

> 命名陷阱：文件叫 `_torch/metadata.py`，但它**只**放 `KVCacheParams`/`CacheType`。真正的大头 `AttentionMetadata` 基类在 `attention_backend/interface.py`。`interface.py` 通过 `from ..metadata import KVCacheParams`（[interface.py:L24](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/interface.py#L24)）把二者接起来。

#### 4.3.2 核心流程

每一步前向，metadata 的生命周期是：

```text
引擎（PyExecutor/ModelEngine）每步:
  1. 调度器决定本步批次 -> 构造/更新 AttentionMetadata（seq_lens, num_contexts, kv_cache_manager, ...）
  2. attn_metadata.prepare()            # 钩子：准备 mamba 等
  3. for each decoder layer:
        module.forward(..., attn_metadata, ...):
           - 现造 AttentionForwardArgs(...)   # 每层一份
           - backend.forward(q,k,v, attn_metadata, forward_args=...)
  4. 步末 update / free resources（KV cache manager）
```

`AttentionMetadata` 上有一组**只读 property** 把原始字段派生成方便量：`num_contexts`、`num_generations`、`num_tokens`、`num_ctx_tokens`、`num_seqs`、`seq_lens_cuda` 等。MLA 与 Attention 都重度依赖这些派生量来切批次（context 段 vs generation 段）。

#### 4.3.3 源码精读

**KV 缓存描述**（`metadata.py`）：

- `KVCacheParams`：[metadata.py:L8-L31](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/metadata.py#L8-L31)。字段包括 `use_cache`、`num_cached_tokens_per_seq`、`block_ids_per_seq`（分页块 id 列表）、`host_max_attention_window_sizes`、`host_sink_token_length`、`num_extra_kv_tokens`。
- `CacheType`：[metadata.py:L34-L40](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/metadata.py#L34-L40)，枚举 `LINEAR / PAGED / PER_TOKEN`，注释里写明三种布局。

**批次级 metadata 基类** `AttentionMetadata`：[interface.py:L60-L61](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/interface.py#L60-L61)（`@dataclass(kw_only=True)`）。几个关键字段：

- `max_num_requests` / `max_num_tokens` / `max_num_sequences`：批次容量上限，[interface.py:L67-L71](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/interface.py#L67-L71)。
- `kv_cache_manager`：KV 缓存管理器（u7-l1 详讲），[interface.py:L73](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/interface.py#L73)。
- `kv_layout`：分页块的内存布局 `NHD` 或 `HND`，[interface.py:L79-L82](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/interface.py#L79-L82)。
- `seq_lens`（property，[interface.py:L216-L242](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/interface.py#L216-L242)）：每个序列的查询长度，setter 里会同步派生 `_seq_lens_cuda`、`num_ctx_tokens`、`num_generations`、`num_tokens`（`on_update`，[interface.py:L204-L214](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/interface.py#L204-L214)）。
- `num_contexts`（property，[interface.py:L244-L252](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/interface.py#L244-L252)）：批次里 context 阶段序列数；`num_tokens`（[interface.py:L321-L323](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/interface.py#L321-L323)）：本步 KV token 总数（MLA 用来切片）。
- `runtime_features`：`AttentionRuntimeFeatures`，标记 chunked prefill / cache reuse / 投机解码草稿 token 等（[interface.py:L41-L49](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/interface.py#L41-L49)）。
- `is_cuda_graph`：是否处于 CUDA Graph 回放（影响能否重新分配 buffer，[interface.py:L86-L87](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/interface.py#L86-L87)）。

**每前向可选项** `AttentionForwardArgs`：[interface.py:L910-L973](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/interface.py#L910-L973)。注意它**同时承载标准注意力和 MLA 的字段**——`output`/`out_scale`/`attention_mask` 给标准路径用，`latent_cache`/`q_pe`/`cu_q_seqlens`/`mla_bmm1_scale`/`quant_q_buffer` 给 MLA 用，`topk_indices`/`sparse_prediction` 给稀疏注意力用。这正是「一个契约管所有注意力变体」的体现。

合并函数 `merge_attention_forward_args`：[interface.py:L991-L1009](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/interface.py#L991-L1009)——它把**遗留的 `**kwargs`** 合并成显式 `AttentionForwardArgs`，并且**拒绝未知字段**、**禁止 `kwargs` 与显式 `forward_args` 并存**。这就是 [ATTENTION_DEVELOPER_GUIDE.md:L169-L172](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/ATTENTION_DEVELOPER_GUIDE.md#L169-L172) 说的「`**kwargs` 只是临时兼容路径」。

**后端基类** `AttentionBackend`：[interface.py:L1012-L1016](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/interface.py#L1012-L1016)（`Generic[TMetadata]`）。

- 抽象 `forward(q, k, v, metadata, forward_args=None, **kwargs)`：[interface.py:L1050-L1072](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/interface.py#L1050-L1072)，docstring 写明了 q/k/v 的形状契约。
- 三个**粗粒度能力钩子**：`support_fused_rope()`（[L1074-L1076](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/interface.py#L1074-L1076)）、`support_fused_qkv()`（[L1078-L1080](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/interface.py#L1078-L1080)）、`support_mla()`（[L1082-L1084](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/interface.py#L1082-L1084)）。注意指南反复强调它们是**粗检查**，不能证明所有路径都已实现（[ATTENTION_DEVELOPER_GUIDE.md:L173-L184](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/ATTENTION_DEVELOPER_GUIDE.md#L173-L184)）。
- `AttentionInputType` 枚举：[interface.py:L54-L57](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/interface.py#L54-L57)，`mixed / context_only / generation_only`——MLA 正是靠它告诉后端「这一批是 context 还是 generation」，从而走不同 kernel。

> 元数据子类与后端家族一一对应：`TrtllmAttentionMetadata` 配 `TrtllmAttention`，`VanillaAttentionMetadata` 配 `VanillaAttention`，`FlashInferAttentionMetadata` 配 `FlashInferAttention`（见 [ATTENTION_DEVELOPER_GUIDE.md:L135-L139](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/ATTENTION_DEVELOPER_GUIDE.md#L135-L139)）。**换后端往往要换 metadata 子类**——这点在 u6-l2 详讲。

#### 4.3.4 代码实践

**实践目标**：亲手从源码验证「metadata 契约 = 批次级状态 + 每前向可选项 + KV 缓存描述」三件套，并读懂一个派生量的计算。

**操作步骤**：

1. 打开 [interface.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/interface.py)，找到 `AttentionMetadata.on_update`（[L204-L214](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/interface.py#L204-L214)）。读懂它如何由 `seq_lens` 和 `num_contexts` 派生出 `_num_ctx_tokens`（前 `num_contexts` 个序列长度之和）、`_num_generations`（剩余序列数）、`_num_tokens`（KV 长度之和）。
2. 跳到 `Attention.forward`（[attention.py:L977](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/attention.py#L977)）与 `MLA.forward_impl`（[mla.py:L1398](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/mla.py#L1398)），统计它们各自**读了** metadata 上的哪些派生量（如 `num_tokens`、`num_contexts`、`num_generations`、`num_ctx_tokens`）。
3. 在 `_attn_impl`（[attention.py:L876](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/attention.py#L876)）里数一数：构造 `AttentionForwardArgs(...)` 时填了哪些字段。这就是「每前向可选项」的真实样子。

**需要观察的现象**：`num_tokens`/`num_contexts` 等量都**不是**在 metadata 构造时显式传入的，而是 setter 触发 `on_update` 自动算出来的——所以模块层可以放心当只读量用。

**预期结果**：你能解释「为什么 MLA 能用 `q[:num_ctx_tokens]`、`q[num_ctx_tokens:]` 把批次切成 context/generation 两半」——因为 `num_ctx_tokens` 由 metadata 的 `on_update` 自动维护。

> 本实践为源码阅读型；运行结果「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`AttentionMetadata` 和 `AttentionForwardArgs` 为什么分成两个对象？

> **答案**：`AttentionMetadata` 是**批次级、每步一份**的状态（序列长度、缓存管理器、并行配置），所有层共享、每步才更新；`AttentionForwardArgs` 是**每层、每次前向**的即时项（输出 buffer、scale、mask、MLA buffer），逐层不同。分开能复用批次状态、避免逐层重建大对象，也防止别名 bug。

**练习 2**：`merge_attention_forward_args` 会拒绝哪两种用法？

> **答案**：(a) 传入 `AttentionForwardArgs` 字段集合以外的未知 kwargs（报 `Unknown attention forward arguments`）；(b) 同时传入显式 `forward_args` 和遗留 `kwargs`（报 "not both"）。见 [interface.py:L997-L1007](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/interface.py#L997-L1007)。

**练习 3**：能力钩子 `support_mla()` 返回 `True` 是否就等于「该后端能跑 MLA」？

> **答案**：不等。它是**粗粒度**检查，只表明后端声明支持 MLA 形态，不证明所有必需算子/稀疏路径都已实现；仍需结合 metadata 子类、KV 缓存语义一起判断。见 [ATTENTION_DEVELOPER_GUIDE.md:L173-L184](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/ATTENTION_DEVELOPER_GUIDE.md#L173-L184)。

---

## 5. 综合实践

把三个最小模块串起来：**画一张「注意力四层栈」的实例图，并标注契约穿过各层的位置**。

任务：

1. 在一张图上画出四层：模块层（`Attention` 或 `MLA`）、后端层（`AttentionBackend` 子类）、运行时契约（metadata + forward_args）、KV 缓存（`KVCacheManager` + `KVCacheParams`）。
2. 用**两种颜色的箭头**分别标出：
   - **数据流**：`hidden_states` → 投影 → `(q,k,v)` 或 `(fused_q, latent_cache)` → 后端 → 输出 → `o_proj`。
   - **控制/契约流**：`attn_metadata`（批次级，贯穿所有层）与 `AttentionForwardArgs`（每层现造）如何注入后端。
3. 在图上标出 4 个「契约检查点」并各写一句守则（出自 [ATTENTION_DEVELOPER_GUIDE.md 第 7 节「Anti-Patterns」L383-L389](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/ATTENTION_DEVELOPER_GUIDE.md#L383-L389)）：
   - 不要把注意力工作当成「纯数学」——它还含 KV 缓存读写。
   - 不要把后端选择当成与 metadata 选择无关——换后端常要换 metadata 子类。
   - 不要把 KV 缓存语义当成小细节——MLA 潜缓存与标准 K/V 完全不同。
   - 不要在没查融合路径前就重复实现 RoPE。
4. 完成后，写一段 100 字以内的「修改 `attention.py` 前的自检清单」：先问四个第一遍 fit 问题（[ATTENTION_DEVELOPER_GUIDE.md:L298-L310](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/ATTENTION_DEVELOPER_GUIDE.md#L298-L310)）——模块层能否表达？后端能否执行？状态能否塞进现有 metadata？KV 行为能否留在当前分页模型内？

**预期产出**：一张图 + 一段自检清单。完成后，你应该能凭图向别人解释「为什么加一个新注意力，先动模块层扩展点、再动 TRTLLM 后端、最后才考虑新后端」（默认 bring-up 顺序见 [ATTENTION_DEVELOPER_GUIDE.md:L338-L352](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/ATTENTION_DEVELOPER_GUIDE.md#L338-L352)）。

> 本实践为源码阅读 + 设计型，无需 GPU；如需真机验证某条契约（例如换 `attn_backend` 观察 metadata 子类变化），运行结果「待本地验证」。

## 6. 本讲小结

- 注意力是**四层栈**：模块层（`Attention`/`MLA`）→ 后端层（`AttentionBackend` 子类）→ 运行时契约（metadata + forward_args）→ KV 缓存语义。判断「在哪一层动手」是所有注意力工作的第一步。
- `Attention(nn.Module)` 是围绕后端调用的**外壳**：它做 QKV 投影、RoPE、格式适配、输出投影，**不碰** softmax/QK^T；真正的分数计算与 KV 缓存读写在后端 `self.attn.forward(...)`。
- `MLA` 用**低秩压缩 + 权重吸收**把 KV 缓存从 \(2 n_h d\) 降到约 `kv_lora_rank + qk_rope_head_dim`；context 走展开路径、generation 走吸收路径，二者数学等价、实现迥异。
- metadata 契约是**三件套**：批次级 `AttentionMetadata`（每步一份、所有层共享）、每前向 `AttentionForwardArgs`（每层现造）、KV 缓存描述 `KVCacheParams`/`CacheType`（在 `metadata.py`）；`AttentionMetadata` 基类与 `AttentionForwardArgs` 都在 `attention_backend/interface.py`。
- 模块层与后端之间的**唯一正式数据通道**是 `(q, k, v, metadata, forward_args)`；`**kwargs` 只是临时兼容路径，会被合并并校验。
- 改 attention 前**必读** `ATTENTION_DEVELOPER_GUIDE.md`：先做四问 fit 检查，优先扩模块层扩展点、再扩 TRTLLM 后端、最后才考虑新后端。

## 7. 下一步学习建议

- **u6-l2 注意力后端家族**：本讲把后端当成「实现了 `AttentionBackend` 接口的对象」一笔带过。下一讲深入 TRTLLM / Vanilla / FlashInfer 三个后端的差异、`attn_backend` 配置如何路由、以及 sparse 后端——你会发现「换后端常要换 metadata 子类」的真正含义。
- **u7-l1 分页 KV Cache 与 KVCacheManager**：本讲提到的 `kv_cache_manager`、`kv_factor=1/2`、潜缓存语义，下一讲会从缓存侧完整展开，讲清块的分配/回收与 MLA 潜缓存的特殊处理。
- **u10-l4 CUDA Graph 与 torch.compile**：本讲多次出现「为兼容 CUDA Graph 包成自定义算子」，其原理留到那里拆解。
- 若想立刻看一个真实模型的注意力怎么接，可对照 [_torch/models/modeling_llama.py](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/models/modeling_llama.py)，复习 u5-l1 讲的「Config + ForCausalLM」范式里 `Attention` 是如何被 decoder layer 组装的。
