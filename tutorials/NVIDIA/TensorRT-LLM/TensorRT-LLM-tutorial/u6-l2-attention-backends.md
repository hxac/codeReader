# 注意力后端家族

## 1. 本讲目标

上一讲（u6-l1）我们把注意力拆成了「四层栈」，并得出一个关键结论：标准 `Attention(nn.Module)` 只是个**外壳**——它做 QKV 投影、TP/CP 切分、RoPE、输出投影，但**真正计算注意力分数和读写 KV cache 的活儿，全部交给 `self.attn.forward(...)`**。那么 `self.attn` 到底是什么？它有几种实现？又是怎么被选出来的？

本讲就打开这个黑盒。读完本讲，你应该能够：

1. 说出 TensorRT-LLM 有哪几个**注意力后端**（attention backend），它们各自的定位与差异。
2. 看懂 `AttentionBackend` 这个**统一抽象基类**定义的契约：`forward`、`AttentionMetadata`、`AttentionForwardArgs`、`support_*` 能力声明。
3. 追踪 `attn_backend` 配置字符串是如何**路由**到具体后端类的（含一条「正交」的 sparse 维度）。
4. 区分 `TrtllmAttention` 内部的**二级 FMHA 调度**（即便选了 TRTLLM 后端，真正跑的 kernel 还会再选一次）。
5. 判断在什么场景下应该选 FlashInfer，什么时候用 TRTLLM，什么时候会退回 vanilla。

## 2. 前置知识

- **后端（backend）**：一个具体注意力算法的实现载体，是一个继承 `AttentionBackend` 的类，每个 decoder 层各持有一个实例。
- **prefill（context）与 decode（generation）两阶段**：prefill 一次处理整段 prompt（算力密集，长序列），decode 每步只生成一个 token（带宽密集，极短 query）。同一批里这两类请求会**混在一起**（in-flight batching），后端必须能同时处理。
- **KV cache 的分页（paged）布局**：KV cache 按「块（page/block）」组织，详见 u7-l1。本讲只需知道：后端既要**写入新 KV**，又要**按页表读取历史 KV**。
- **FlashInfer / FlashAttention**：社区里两类高性能注意力 kernel 库。TensorRT-LLM 既把它们当作**可选后端**，也会在自家 TRTLLM 后端里**复用** FlashInfer 的部分 kernel。
- u6-l1 已建立的关键术语：`AttentionMetadata`（批次级、每步一份、所有层共享）、`AttentionForwardArgs`（每层现造）、模块层与后端层之间唯一正式数据通道是 `(q, k, v, metadata, forward_args)`。

## 3. 本讲源码地图

本讲涉及的文件都集中在 `tensorrt_llm/_torch/attention_backend/` 下：

| 文件 | 作用 |
|------|------|
| `interface.py` | 定义统一契约：`AttentionBackend` 基类、`AttentionMetadata`、`AttentionForwardArgs`、`AttentionInputType`、各类 Params。 |
| `utils.py` | **后端选择的中枢**：`get_attention_backend()` 把字符串路由到类，`create_attention()` 是工厂。 |
| `trtllm.py` | **默认后端** `TrtllmAttention` 及其 `TrtllmAttentionMetadata`。 |
| `flashinfer.py` | FlashInfer 后端 `FlashInferAttention` 及其 metadata。 |
| `vanilla.py` | 参考实现 `VanillaAttention`（基线，非生产）。 |
| `star_flashinfer.py` | Star Attention（FlashInfer 的一个变体后端）。 |
| `fmha/` 子包 | **TRTLLM 后端内部的二级 kernel 调度**：`Fmha` 基类、`registry.py`、`fallback.py`、`flashinfer_trtllm_gen.py` 等。 |
| `sparse/` 子包 | **稀疏注意力**的正交维度：按 algorithm 选 cache manager 与后端子类。 |
| `_torch/modules/attention.py` | 模块层 `Attention`，在 `__init__` 里调用 `get_attention_backend` / `create_attention`。 |
| `llmapi/llm_args.py` | `attn_backend` 字段与 `SparseAttentionConfig` 的定义。 |

## 4. 核心概念与源码讲解

### 4.1 后端接口：统一契约 AttentionBackend

#### 4.1.1 概念说明

要让「外壳 `Attention`」能无缝替换底层实现，就必须有一份**契约**：所有后端都长得一样、都能被同样地调用。这份契约就是 `AttentionBackend` 抽象基类，外加两个数据结构：

- `AttentionMetadata`：**批次级**的上下文信息（这步有哪些请求、各自多长、KV cache 在哪些页……），一步一份，全模型所有层共享同一个对象。
- `AttentionForwardArgs`：**每层、每次前向**的可选参数（mask 类型、output 缓冲、量化 scale、稀疏预测结果……）。

这套契约正是 u6-l1 说的「模块层 → 后端层唯一正式数据通道 `(q, k, v, metadata, forward_args)`」的官方定义。

#### 4.1.2 核心流程

一个后端实例的生命周期分三阶段（`docs/source/features/attention.md` 也正是这样描述的）：

1. **模型构造期**：每层 `Attention.__init__` 调 `create_attention(...)`，得到一个后端实例并挂到 `self.attn`。
2. **元数据准备期**（每步前向之前）：运行时填充 `AttentionMetadata` 的字段，再调 `metadata.prepare()` 把 KV cache 的页表、长度等就绪。
3. **单步前向期**（每层前向）：调 `self.attn.forward(q, k, v, metadata, forward_args)`，后端在这里写 KV cache、算注意力、返回输出。

后端还通过一组 `@classmethod` 声明自己的**能力**（`support_fused_rope` / `support_fused_qkv` / `support_mla` / `support_multi_item_scoring`），模块层据此决定是否把某些计算「下放」给后端融合执行。

#### 4.1.3 源码精读

先看基类本体。`AttentionBackend` 是一个泛型类，泛型参数 `TMetadata` 绑定它配套的 metadata 类型，并暴露一个类属性 `Metadata` 指向该类型：

[interface.py:1012-1096](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/interface.py#L1012-L1096) — `AttentionBackend` 基类。关键成员：

- `__init__` 记录 `layer_idx / num_heads / head_dim / num_kv_heads / quant_config`（[interface.py:1018-1040](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/interface.py#L1018-L1040)）。
- `forward(...)` 是**抽象方法**，签名固定为 `(q, k, v, metadata, forward_args, **kwargs)`，默认 `raise NotImplementedError`（[interface.py:1050-1072](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/interface.py#L1050-L1072)）。注释里写清了 q/k/v 的形状约定：第一维都是**拍平的 token 总数**（packed/varlen），不是 `[batch, seq]`。
- `support_fused_rope / support_fused_qkv / support_mla / support_multi_item_scoring` 默认全为 `False`（[interface.py:1074-1088](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/interface.py#L1074-L1088)），具体后端按需覆盖。

再看输入类型的枚举——它决定了后端走 context 路径还是 generation 路径：

[interface.py:54-57](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/interface.py#L54-L57) — `AttentionInputType`：`mixed`（context + generation 混批）、`context_only`、`generation_only`。注释提醒它必须与 C++ 侧 `cpp/tensorrt_llm/thop/attentionOp.cpp` 里的同名枚举保持同步——这是「Python 调度、C++ 加速」边界的又一个体现。

接着是每层前向参数容器：

[interface.py:910-984](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/interface.py#L910-L984) — `AttentionForwardArgs`。注意它把几十个「legacy 关键字参数」收敛成一个 dataclass，并提供了 `merge_attention_forward_args()`（[interface.py:991-1009](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/interface.py#L991-L1009)）做兼容：既可以显式传 `forward_args`，也可以用旧的 `**kwargs`，但二者不能混用，未知字段直接抛错。其中 `attention_input_type`、`attention_mask`、`latent_cache`（MLA 用）、`sparse_prediction`（稀疏用）、各种 `*_scale`（量化用）都是后端分支的重要开关。

> 小提示：`AttentionMetadata` 字段很多（[interface.py:60-638](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/interface.py#L60-L638)），但本讲你只需记住它「描述这一步的批」，细节（如 CUDA graph 专用副本、cross-attention 子 metadata）会在用到时再展开。

#### 4.1.4 代码实践（源码阅读型）

**目标**：确认「能力声明」如何在模块层影响行为。

1. 打开 `tensorrt_llm/_torch/modules/attention.py`，定位到 `__init__` 里读取后端能力并据此决定 RoPE 放在哪的一段（约 599–663 行）。
2. 阅读下面三行逻辑：
   - [attention.py:606-607](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/attention.py#L606-L607)：`attn_cls = get_attention_backend(self.attn_backend, sparse_params=sparse_params)`——先拿到**类**。
   - [attention.py:639-646](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/attention.py#L639-L646)：`if self.rope_fusion and not attn_cls.support_fused_rope(): ... self.rope_fusion = False`——如果后端不支持融合 RoPE，就把 `rope_fusion` 关掉、改为在模块层用 `RotaryEmbedding` 单独算。
   - [attention.py:664-669](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/modules/attention.py#L664-L669)：`self.attn = create_attention(self.attn_backend, ...)`——真正实例化后端。
3. **预期结果**：你能用自己的话说清「为什么换一个后端，RoPE 可能在不同地方计算」——因为模块层是用**类方法** `support_fused_rope()` 在 `__init__` 阶段做静态分流的。

> 待本地验证：若你有 GPU 环境，可分别用 `LLM(model=..., attn_backend="TRTLLM")` 与 `attn_backend="FLASHINFER"` 加载同一小模型，观察启动日志里 RoPE 相关路径是否不同（依赖具体模型与是否安装了 flashinfer）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `AttentionBackend` 要做成 `Generic[TMetadata]` 泛型，并用 `Metadata` 类属性绑定一个 metadata 子类？
**答案**：因为不同后端需要**不同**的批次级预计算量（如 TRTLLM 要 XQA 用的 `spec_decoding_generation_lengths`、FlashInfer 要 `plan()` 出来的 wrapper 句柄）。泛型 + `Metadata` 类属性让运行时在「未指定具体后端」时仍能通过 `Backend.Metadata` 反查出该用哪种 metadata 子类来构造，保证类型一致、字段自洽。

**练习 2**：`forward` 的 q/k/v 第一维为什么是「拍平的 token 总数」而不是 `[batch, seq]`？
**答案**：因为 in-flight batching 把一批里所有请求的 token **拼接**成一个一维张量（varlen/packed），再用 `cu_q_seqlens` 等累积偏移标记每段边界。这样不同长度请求可以共用一次 kernel launch，避免按 batch 维度开销。

---

### 4.2 后端选择中枢：从字符串到类

#### 4.2.1 概念说明

用户侧只需写一个字符串 `attn_backend="TRTLLM"`（或 `"FLASHINFER"` / `"VANILLA"`）。把这个字符串变成具体类的，就是 `get_attention_backend()`。这里存在**两条正交的选择轴**：

1. **主后端轴**：`VANILLA` / `TRTLLM` / `FLASHINFER` / `FLASHINFER_STAR_ATTENTION`。
2. **稀疏轴（可选）**：如果配了 `sparse_attention_config`，会按 sparse 的 `algorithm` 把主后端类**替换**成对应的稀疏子类（如 `RocketTrtllmAttention`）。

也就是说，「主后端 × 是否稀疏」共同决定最终实例化的类。

#### 4.2.2 核心流程

```
config.attn_backend (字符串)            ┐
config.sparse_attention_config (可选)   ┤→ Attention.__init__
                                        ┘
   │
   ├─ sparse_attn_cfg.to_sparse_params(...)  →  sparse_params (或 None)
   ├─ get_attention_backend(attn_backend, sparse_params)  →  后端【类】
   │      └─ 无 sparse: 直接返回 VanillaAttention / TrtllmAttention / FlashInferAttention
   │      └─ 有 sparse: 返回 get_*_sparse_attn_attention_backend(sparse_params)
   │      └─ FLASHINFER 不可用 / 未知名: 警告并退回 TrtllmAttention
   └─ create_attention(...)  →  后端【实例】(挂到 self.attn)
```

#### 4.2.3 源码精读

路由核心就一个函数：

[utils.py:18-41](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/utils.py#L18-L41) — `get_attention_backend(backend_name, sparse_params=None)`。要点：

- 先 `backend_name.upper()`，所以用户写 `"trtllm"` / `"Trtllm"` / `"TRTLLM"` 都行。
- `VANILLA` / `TRTLLM` 直接返回对应类（有 sparse 则返回 sparse 变体）。
- `FLASHINFER` 和 `FLASHINFER_STAR_ATTENTION` 都被 `IS_FLASHINFER_AVAILABLE` 守卫（[utils.py:31-38](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/utils.py#L31-L38)）——没装 flashinfer 就走不通。
- **任何不匹配都退回 TRTLLM 并打 warning**（[utils.py:40-41](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/utils.py#L40-L41)）。这就是「即便配错也不会崩，而是悄悄退回默认」的兜底逻辑。

工厂函数：

[utils.py:44-109](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/utils.py#L44-L109) — `create_attention(...)`。它先 `get_attention_backend(...)` 拿到类，再把 MLA、量化、RoPE、稀疏等参数打包，最终 `return attn_cls(layer_idx, num_heads, head_dim, num_kv_heads, **kwargs)` 实例化。注意 [utils.py:68-70](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/utils.py#L68-L70) 有个硬约束：`attention_chunk_size`（分块注意力）**只有 TRTLLM 支持**，配其它后端直接抛 `ValueError`。

配置侧的字段定义：

[llm_args.py:4905-4911](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/llmapi/llm_args.py#L4905-L4911) — `attn_backend` 字段，默认 `'TRTLLM'`，注释明确写道「Recognized values mirror get_attention_backend dispatch in .../utils.py」。这是一个很好的线索：**想知道某配置项的真实取值，去看它镜像的 dispatch 函数**。`status="beta"` 表示该字段本身仍处于 beta 成熟度。

包的对外导出（看到 `from tensorrt_llm._torch.attention_backend import ...` 能拿到什么）：

[attention_backend/__init__.py:1-25](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/__init__.py#L1-L25) — 注意 FlashInfer / Star 相关导出被 `if IS_FLASHINFER_AVAILABLE:` 守卫（[L19-L25](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/__init__.py#L19-L25)），没装 flashinfer 时连符号都不存在。

#### 4.2.4 代码实践（本讲主实践）

**目标**：亲手画出「`attn_backend` 配置值 → 实际后端类」的映射表，并回答「何时选 FlashInfer」。

**操作步骤**：

1. 打开 [utils.py:18-41](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/utils.py#L18-L41)。
2. 按函数体填写下表（参考答案见下方「预期结果」）。对每个分支，区分 `sparse_params is None` 与 `sparse_params is not None` 两种情形。

| 配置字符串 | 是否需 flashinfer | 无 sparse → 类 | 有 sparse → |
|------------|------------------|----------------|-------------|
| `VANILLA` | 否 | `VanillaAttention` | `get_vanilla_sparse_attn_attention_backend` |
| `TRTLLM` | 否 | `TrtllmAttention` | `get_trtllm_sparse_attn_attention_backend` |
| `FLASHINFER` | 是 | `FlashInferAttention` | `get_flashinfer_sparse_attn_attention_backend` |
| `FLASHINFER_STAR_ATTENTION` | 是 | `StarAttention` | （该分支不处理 sparse） |
| 其它/不可用 | — | 退回 `TrtllmAttention`（打 warning） | — |

3. 回答「何时选 FlashInfer」。结合官方文档 `docs/source/features/attention.md`（[attention.md:22-36](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/docs/source/features/attention.md#L22-L36)）总结：
   - **TRTLLM 是默认且推荐的生产后端**：支持 FlashInfer 的全部特性（FP8 KV cache、RoPE 融合等），并额外支持**融合 QKV 输入**与 **FP8 输出**，性能进一步优化。
   - **FlashInfer 是性能优化的备选**：支持 in-flight batching 与分页 KV cache，当你想用 FlashInfer 库自家的 kernel（如它对某些模型/形状调优更好），或 TRTLLM 的 FMHA kernel 暂不支持某组合时，可显式选它。
   - **VANILLA 仅供参考/基线**，不要用于生产。
   - 当心：如果没安装 flashinfer 却选了 `FLASHINFER`，会被静默退回 TRTLLM（看 warning）。

**预期结果**：一张与上表一致的映射表，以及一段「TRTLLM 默认；FlashInfer 作备选/特定场景；VANILLA 仅基线；配错静默退回 TRTLLM」的判断准则。

#### 4.2.5 小练习与答案

**练习**：用户传 `attn_backend="flashinfer"` 但环境里没装 flashinfer，会发生什么？是报错还是继续？
**答案**：不报错。`get_attention_backend` 里 `FLASHINFER` 分支带 `and IS_FLASHINFER_AVAILABLE` 条件不成立，落到末尾 `logger.warning("Falling back to TRTLLM attention backend")` 并返回 `TrtllmAttention`（[utils.py:40-41](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/utils.py#L40-L41)）。模型照常跑，只是用了 TRTLLM 后端——所以**部署时要留意日志里的这条 warning**，否则可能以为自己在用 FlashInfer 实则没有。

---

### 4.3 TRTLLM 后端（默认后端与二级 FMHA 调度）

#### 4.3.1 概念说明

`TrtllmAttention` 是默认后端，也是特性最全的。它的特别之处在于：**它的 `forward` 自己并不直接算注意力，而是再向下做一次「二级调度」**——在一组 `Fmha`（Fused Multi-Head Attention）库实现里，挑第一个声明「我支持当前这组输入」的去执行。

这意味着即便你始终选 `TRTLLM`，底层真正跑的 kernel 也可能不同：例如在 Blackwell（sm100+）上会优先走 `flashinfer_trtllm_gen`（复用 FlashInfer 的 trtllm-gen kernel），都不命中再走 `fallback`（走 C++ THOP 算子）。这一层可以通过环境变量 `TLLM_FMHA_LIBS` 调整。

#### 4.3.2 核心流程

```
TrtllmAttention.forward(q, k, v, metadata, forward_args)
   │
   ├─ merge_attention_forward_args: 把 kwargs 收敛进 forward_args
   ├─ 形状/不变量校验（fused_qkv vs unfused_kv vs cached_cross_kv）
   ├─ （可选）sparse 预测：sparse_kv_predict / sparse_attn_predict 填 sparse_prediction
   ├─ （可选）FlashMLA / Blackwell first_sparse 等元数据刷新
   ├─ （MLA / cross 等特殊路径的预处理）
   │
   └─ for fmha in self.fmha_libs:           # 二级调度
          if fmha.is_supported(q,k,v,metadata,forward_args):
              fmha.forward(q,k,v,metadata,forward_args); break
      else:
          raise RuntimeError("No TRT-LLM attention FMHA library supports this request.")
```

`self.fmha_libs` 在 `create_fmha_libs()` 里由 `get_enabled_fmha_lib_classes()` 决定，顺序就是命中优先级。

#### 4.3.3 源码精读

类定义与能力声明：

[trtllm.py:1192-1198](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/trtllm.py#L1192-L1198) — `class TrtllmAttention(AttentionBackend[TrtllmAttentionMetadata])`，绑定自己的 metadata 子类。

[trtllm.py:1788-1798](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/trtllm.py#L1788-L1798) — 覆盖能力：`support_fused_rope()=True`、`support_fused_qkv()=True`、`support_mla()=True`。所以 TRTLLM 允许把 RoPE 与 QKV 投影**融合进后端**算（对比 FlashInfer 默认不融合 RoPE）。

构造函数关键部分：

[trtllm.py:1200-1288](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/trtllm.py#L1200-L1288) — `__init__`。注意：
- 接收 `mla_params`，据此设 `self.is_mla_enable`（[trtllm.py:1233](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/trtllm.py#L1233)）。
- 预算 RoPE 常量 `rotary_inv_freq / rotary_cos_sin`（[trtllm.py:1259-1260](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/trtllm.py#L1259-L1260)）——因为支持融合 RoPE，所以常量在构造期就建好。
- 预留一个恒为 1.0 的 `kv_cache_scaling_factor`，注释解释这是为了给某些 XQA C++ kernel 保证「永远拿到合法指针」（[trtllm.py:1278-1282](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/trtllm.py#L1278-L1282)）——一个典型的「Python 侧兜底、满足 C++ 侧不变量」的例子。
- `self.fmha_libs: List[Fmha] = []`（[trtllm.py:1285](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/trtllm.py#L1285)），延迟到 `update_quant_config`→`create_fmha_libs` 填充。

forward 的二级调度尾部（最值得记的几行）：

[trtllm.py:1765-1774](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/trtllm.py#L1765-L1774) — 二级调度循环：`for fmha in self.fmha_libs: if fmha.is_supported(...): fmha.forward(...); break`，配 `for...else`，全不命中则抛「No TRT-LLM attention FMHA library supports this request.」。forward 入口在 [trtllm.py:1492-1506](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/trtllm.py#L1492-L1506)，中间一大段是各种预处理（[trtllm.py:1510-1764](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/trtllm.py#L1510-L1764)），本讲不展开。

二级调度的「候选库」从哪来：

[fmha/registry.py:26-43](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/fmha/registry.py#L26-L43) — `init_fmha_libs()` 返回有序字典，默认顺序为：`msa_sparse_gqa` → `flashinfer_trtllm_gen` → `fallback`。顺序即优先级。

[fmha/registry.py:46-87](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/fmha/registry.py#L46-L87) — 可用环境变量 `TLLM_FMHA_LIBS` 覆盖：支持精确列表，也支持 `+name` / `-name` 的增量增删（[registry.py:55-83](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/fmha/registry.py#L55-L83)）。`get_enabled_fmha_lib_classes()` 返回最终启用的类列表（[registry.py:86-87](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/fmha/registry.py#L86-L87)）。

二级调度的契约：

[fmha/interface.py:31-67](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/fmha/interface.py#L31-L67) — `Fmha` 抽象基类。三个核心方法：`is_available(cls, attn)`（类能否用于该后端实例）、`is_supported(q,k,v,metadata,forward_args)`（这次前向能否处理）、`forward(...)`（真正执行）。它用 `weakref` 持有宿主 `TrtllmAttention`（[fmha/interface.py:34-42](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/fmha/interface.py#L34-L42)），避免循环引用。

> 命名辨析：注意区分 **`TrtllmAttention`（一级后端类）** 与 **`Fmha` 子类（二级 kernel 实现）**。`flashinfer_trtllm_gen` 这个名字说明：即便主后端是 TRTLLM，二级 kernel 也可能复用 FlashInfer 提供的 trtllm-gen kernel——FlashInfer 既是「一个可选主后端」，又是「TRTLLM 后端内部的一个 kernel 来源」。

#### 4.3.4 代码实践（源码阅读型）

**目标**：验证「二级调度」的存在与顺序。

1. 读 [fmha/registry.py:35-39](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/fmha/registry.py#L35-L39)，记下默认顺序。
2. 读 [trtllm.py:1768-1774](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/trtllm.py#L1768-L1774) 的循环，确认它是「第一个 `is_supported` 为真的就执行并 break」。
3. **预期结果**：你能解释「为什么有时排查 TRTLLM 注意力问题，要看 `TLLM_FMHA_LIBS`」——因为同样标称 TRTLLM，实际命中的二级库可能不同。

#### 4.3.5 小练习与答案

**练习 1**：`TrtllmAttention.forward` 末尾的 `for...else` 中，`else` 何时执行？
**答案**：当 `self.fmha_libs` 里**没有任何一个** `is_supported(...)` 返回 `True` 时执行（Python 的 `for...else` 语义：循环未被 `break` 才进 `else`），此时抛 `RuntimeError`，表示当前输入组合没有任何已启用的 FMHA 库能处理。

**练习 2**：如何临时关掉 `flashinfer_trtllm_gen` 这个二级库做对比测试（不删源码）？
**答案**：设环境变量 `TLLM_FMHA_LIBS="-flashinfer_trtllm_gen"`（增量删除，见 [registry.py:62-74](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/fmha/registry.py#L62-L74)），重启进程即可；这是排查「是不是某个二级 kernel 引入回归」的标准手段。**待本地验证**：在 GPU 环境跑同一 workload 对比前后行为。

---

### 4.4 FlashInfer 后端与 sparse / vanilla 维度

#### 4.4.1 概念说明

**FlashInfer 后端**（`FlashInferAttention`）是性能优化的备选后端，直接调用 FlashInfer 库的 wrapper（`BatchPrefillWith*Wrapper`、`BatchDecodeWith*Wrapper` 等），按 fa2 / fa3 / cuDNN 等 kernel 家族 `plan()` 后 `run()`。它的 MLA 分两条路：context 用 ragged prefill + cache append，generation 用 paged decode。

**sparse 后端**不是「第五个主后端」，而是一条**正交维度**：当 `sparse_attention_config` 存在时，`get_attention_backend` 不返回裸的 `TrtllmAttention`，而是返回带稀疏能力的子类（如 `RocketTrtllmAttention`、`DSATrtllmAttention`）。稀疏算法还各自配一个**专用的 KV cache manager**（如 `RocketKVCacheManager`），因为稀疏注意力的缓存结构与稠密不同。

**vanilla**（`VanillaAttention`）是参考实现，主要用于 in-flight batching 与线性 KV cache 的基线对照，不做激进优化，**不推荐生产使用**。

#### 4.4.2 核心流程

FlashInfer 前向的分支（简化）：

```
FlashInferAttention.forward(q,k,v,metadata,forward_args)
   ├─ 解析 mask → attention_mask_type；准备 output 缓冲
   └─ forward_impl(...)
        ├─ if is_mla_enable: 分发到 _mla_forward_context / _mla_forward_paged_context / _mla_forward_generation
        ├─ if kv_cache_manager is None: ragged prefill（无 KV cache，encoder-only 风格）
        └─ else: 取分页 KV cache → _append_paged_kv_cache 写新 KV → 按 plan 出的 wrapper run()
```

sparse 选择流程：

```
sparse_attention_config.algorithm ∈ {rocket, dsa, deepseek_v4, skip_softmax, minimax_m3}
   ├─ get_sparse_attn_kv_cache_manager(cfg)  →  选 cache manager【类】
   └─ get_<base>_sparse_attn_attention_backend(sparse_params)  →  选后端【类】（按主后端 base 分派）
```

#### 4.4.3 源码精读

FlashInfer 后端类与能力：

[flashinfer.py:1642-1653](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/flashinfer.py#L1642-L1653) — `FlashInferAttention`，声明 `support_mla()=True`、`support_multi_item_scoring()=True`（**未**覆盖 `support_fused_rope`，故继承基类的 `False`——RoPE 默认在模块层算）。

[flashinfer.py:1655-1679](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/flashinfer.py#L1655-L1679) — `__init__`，从 kwargs 取 `flashinfer_backend`（默认 `"fa2"`），按 `mla_params` 设 MLA 维度。

forward 入口与 MLA 分发：

[flashinfer.py:2162-2223](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/flashinfer.py#L2162-L2223) — `forward`：解析 mask、按 MLA 与否决定 output 形状（MLA context 输出 `v_head_dim`、MLA generation 输出 `kv_lora_rank`，见 [flashinfer.py:2189-2196](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/flashinfer.py#L2189-L2196)），最终调 `forward_impl`。

[flashinfer.py:1930-1972](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/flashinfer.py#L1930-L1972) — `forward_impl` 的 MLA 分发段：有 latent_cache 且有 k/v → context（`_mla_forward_context`）；k/v 都为 None → 按 `has_cached_context` 走 paged context 或 generation（`_mla_forward_generation`）。非 MLA 路径在 [flashinfer.py:1977-2049](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/flashinfer.py#L1977-L2049)：无 KV cache manager 时走 ragged prefill（`metadata.plan(...)` 后 `wrapper.run(...)`，[flashinfer.py:1991-2019](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/flashinfer.py#L1991-L2019)），否则取分页 cache、`_append_paged_kv_cache` 写入（[flashinfer.py:2042-2049](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/flashinfer.py#L2042-L2049)）。注意 FlashInfer 的滑动窗口是「闭区间」，而 TRTLLM 是「开区间」，所以窗口大小要减 1（[flashinfer.py:2207-2209](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/flashinfer.py#L2207-L2209)）——一个跨后端移植时容易踩的坑。

稀疏维度的两个工厂：

[sparse/utils.py:21-42](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/sparse/utils.py#L21-L42) — `get_sparse_attn_kv_cache_manager(cfg)`：按 `algorithm` 选 cache manager 类——`rocket→RocketKVCacheManager`、`dsa→DSACacheManager`、`deepseek_v4→DeepseekV4CacheManager`、`skip_softmax→KVCacheManager`（复用稠密）、`minimax_m3→MiniMaxM3KVCacheManagerV2`。

[sparse/utils.py:75-100](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/sparse/utils.py#L75-L100) — `get_trtllm_sparse_attn_attention_backend(sparse_params)`：在 TRTLLM 主后端下，按 algorithm 返回 `RocketTrtllmAttention` / `DSATrtllmAttention` / `DeepseekV4TrtllmAttention`，`skip_softmax` 直接复用裸 `TrtllmAttention`，`minimax_m3` 走模型层覆盖（见注释 [sparse/utils.py:90-96](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/sparse/utils.py#L90-L96)）。vanilla / flashinfer 主后端下的稀疏分派见 [sparse/utils.py:62-72](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/sparse/utils.py#L62-L72) 与 [sparse/utils.py:103-109](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/sparse/utils.py#L103-L109)（注意 FlashInfer 主后端目前只支持 `minimax_m3` 一种稀疏算法，其它直接抛 `ValueError`）。

#### 4.4.4 代码实践（源码阅读型）

**目标**：理解 sparse 是「正交维度」而非新主后端。

1. 回到 [utils.py:27-30](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/utils.py#L27-L30)：`TRTLLM` 分支里，`sparse_params is not None` 时返回 `get_trtllm_sparse_attn_attention_backend(sparse_params)`，否则返回 `TrtllmAttention`。
2. 跟进 [sparse/utils.py:75-100](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/sparse/utils.py#L75-L100)，确认这些稀疏后端类名都以原主后端为「基」（如 `RocketTrtllmAttention` 仍是 TRTLLM 系）。
3. **预期结果**：你能画出一个二维表——行是主后端（TRTLLM/FlashInfer/Vanilla），列是 sparse algorithm（含「无」），格子是对应的最终类；空格表示「该组合不支持、抛错」。

#### 4.4.5 小练习与答案

**练习 1**：`skip_softmax` 算法在 TRTLLM 主后端下返回的后端类是什么？为什么？
**答案**：返回裸 `TrtllmAttention`（[sparse/utils.py:88-89](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/sparse/utils.py#L88-L89)）。因为 skip-softmax 不改变 KV cache 结构（其 cache manager 也是普通 `KVCacheManager`，见 [sparse/utils.py:35-36](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/sparse/utils.py#L35-L36)），它的稀疏性体现在「跳过部分 softmax 计算」，由 `SkipSoftmaxParams` 在前向里影响 kernel 行为，不需要换一个后端子类。

**练习 2**：为什么 FlashInfer 后端的滑动窗口要比 TRTLLM 减 1？
**答案**：语义约定不同——FlashInfer 的 sliding window 是**闭区间**（含端点），TRTLLM 的 `attention_window_size` 是**开区间**（不含）。为了一致行为，FlashInfer 后端在 [flashinfer.py:2207-2209](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/flashinfer.py#L2207-L2209) 把传入的窗口大小减 1。

---

## 5. 综合实践

把本讲三条主线（统一契约、一级路由、二级/正交调度）串起来，完成下面这个「后端侦探」任务：

**场景**：同事给你一段配置 `LLM(model=..., attn_backend="FLASHINFER", sparse_attention_config=RocketKVConfig(...))`，说「我跑的是 FlashInfer 的 RocketKV」。请你用本讲学到的源码判断这话是否成立，并预测实际会跑什么。

**步骤**：

1. 查 [utils.py:31-35](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/utils.py#L31-L35)：`FLASHINFER` + `sparse_params` 会调 `get_flashinfer_sparse_attn_attention_backend(sparse_params)`。
2. 查 [sparse/utils.py:103-109](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/sparse/utils.py#L103-L109)：FlashInfer 主后端的稀疏分派**只认 `minimax_m3`**，`rocket` 会落到 `raise ValueError(...)`。
3. 得出结论：这组配置**根本起不来**（抛 `ValueError`），同事的描述不成立。RocketKV 只在 `VANILLA` / `TRTLLM` 主后端下可用（见 [sparse/utils.py:62-72](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/sparse/utils.py#L62-L72) 与 [sparse/utils.py:75-100](https://github.com/NVIDIA/TensorRT-LLM/blob/cf44a1ccee7dd3381a7b4c49a8318c0c3ae4426b/tensorrt_llm/_torch/attention_backend/sparse/utils.py#L75-L100)）。
4. **进阶**：假设把主后端改成 `TRTLLM`，再画出此时「一级路由 → 稀疏子类 → 二级 FMHA」的完整命中链路（一级返回 `RocketTrtllmAttention`，其 forward 仍走 TRTLLM 的 `for fmha in self.fmha_libs` 二级调度）。

**预期结果**：一张能解释「为何该配置报错」的调用链，以及对「主后端 × 稀疏算法」二维能力矩阵的准确描述。

## 6. 本讲小结

- 注意力后端的**统一契约**是 `AttentionBackend`（抽象 `forward` + `support_*` 能力声明）+ `AttentionMetadata`（批次级）+ `AttentionForwardArgs`（每层级）；模块层与后端层唯一正式数据通道是 `(q, k, v, metadata, forward_args)`。
- **一级路由**在 `get_attention_backend()`：`VANILLA` / `TRTLLM` / `FLASHINFER` / `FLASHINFER_STAR_ATTENTION`，配错或 flashinfer 缺失会**静默退回 TRTLLM** 并打 warning；`create_attention()` 负责实例化。
- **TRTLLM 是默认且推荐的生产后端**，能力最全（融合 RoPE / 融合 QKV / MLA / FP8 输出），且内部还有一层**二级 FMHA 调度**（`msa_sparse_gqa → flashinfer_trtllm_gen → fallback`，可用 `TLLM_FMHA_LIBS` 调）。
- **FlashInfer** 是性能优化的备选后端，直接用 FlashInfer 库 wrapper，支持 MLA 与 multi-item scoring；选它要装 flashinfer，否则被退回。它的滑动窗口语义与 TRTLLM 差 1。
- **sparse 是正交维度**：`sparse_attention_config.algorithm`（`rocket`/`dsa`/`deepseek_v4`/`skip_softmax`/`minimax_m3`）同时决定「后端子类」与「专用 KV cache manager」，且不同主后端支持的 sparse 算法子集不同。
- **vanilla 仅供基线**，不推荐生产；排查注意力问题时要同时看 `attn_backend`（一级）与 `TLLM_FMHA_LIBS`（二级）。

## 7. 下一步学习建议

- **向「下」**：进入 u7-l1《分页 KV Cache 与 KVCacheManager》，看后端 `forward` 里写入/读取的「分页 KV cache」到底如何分配与回收，理解 `KVCacheParams` / `block_ids_per_seq` 的来源。
- **向「侧」**：阅读 `tensorrt_llm/_torch/modules/attention.py` 中 `Attention.__init__` 全貌，把「模块层如何根据后端能力分流 RoPE / QKV」补全；再读 `docs/source/features/attention.md` 的「Implement a New Attention Backend」一节，了解新增一个主后端需要实现哪些方法。
- **向「深」**（advanced）：如果你关心 Blackwell 上的 trtllm-gen kernel，可读 `fmha/flashinfer_trtllm_gen.py` 与 `fmha/fallback.py`，理解二级调度的具体命中条件——这将自然衔接 u10-l4《CUDA Graph 与 torch.compile / piecewise》。
