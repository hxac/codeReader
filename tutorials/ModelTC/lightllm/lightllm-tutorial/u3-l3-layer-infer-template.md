# 推理层模板与层推理

## 1. 本讲目标

本讲紧接 [u3-l2 prefill 与 decode 推理主流程](./u3-l2-prefill-decode-flow.md)。上一讲我们看到 `_context_forward` / `_token_forward` 会循环调用 `layer.context_forward(...)` / `layer.token_forward(...)`，并留下了一句话结论：**层模板 `context_forward` / `token_forward` 唯一区别是注意力核函数**。本讲要打开这个「层」的黑盒，回答三个问题：

1. 一层 transformer 内部到底按什么顺序算了什么（norm、注意力、残差、FFN）？
2. LightLLM 用什么手段让 **46+ 个模型族** 复用同一套层结构，每个模型只写少量代码？
3. prefill 与 decode 在「层」这一级是如何被同一套模板统一、又在哪里分叉？

读完本讲，你应该能够：

1. 画出三层（**embedding 层 / transformer 层 / post 层**）在整条前向中的位置与各自职责。
2. 说清楚 **模板方法模式（Template Method）** 在本框架里的落地：模板类写死骨架、子类只填钩子（hook）。
3. 逐行讲清 `TransformerLayerInferTpl` 的 `context_forward` / `token_forward` 两条对称骨架，并指出子类（如 Llama）必须覆写哪 7 个钩子方法。
4. 读懂注意力子流程 `_get_qkv → _post_cache_kv → 注意力核 → _get_o` 与 FFN 子流程，理解它们与 KV Cache 内存池（[u4-l1](./u4-l1-kv-cache-memory-manager.md)）的「写后读」关系。

---

## 2. 前置知识

### 2.1 一个 transformer 层内部算了什么

回顾 [u3-l1](./u3-l1-tp-part-base-model.md) 提到的「一个模型 = N 层 transformer 夹在 embedding 层与 post 层之间」。这里要细化到「一层 transformer 内部」。一层标准 transformer（Llama/Decoder-only 风格）的计算可以写成两段残差：

\[ h \leftarrow h + \text{Attn}\big(\text{Norm}_1(h)\big) \]
\[ h \leftarrow h + \text{FFN}\big(\text{Norm}_2(h)\big) \]

用人话说就是：

1. **注意力子段**：先对输入做一次归一化（`Norm_1`），送进自注意力（`Attn`），把结果用**残差连接**加回原输入。
2. **FFN 子段**：再对（残差后的）输入做一次归一化（`Norm_2`），送进前馈网络（`FFN`），再用**残差连接**加回去。

> 这里反复出现的「残差连接」就是 `x + f(x)`，对应代码里的 `input_embdings.add_(o)`（原地加）。本讲你会看到这两段残差被一字不差地写进模板。

### 2.2 模板方法模式（Template Method Pattern）

这是一个经典设计模式：**父类把「执行步骤的顺序」写死成一个骨架方法，每一步的具体实现留给子类去填**。被写死顺序的方法叫「骨架」（skeleton），子类必须实现、父类只占位的步骤叫「钩子」（hook）。

在 LightLLM 里：

- 骨架方法 = `context_forward` / `token_forward`（写死「norm → attn → 残差 → norm → ffn → 残差」的顺序），写在 `*Tpl` 模板类里。
- 钩子方法 = `_att_norm` / `_get_qkv` / `_ffn` 等（父类里 `raise Exception("need to impl")`，子类覆写）。

好处是：每个新模型只需填 7 个钩子，不用重写一遍层的骨架，46+ 模型族的层结构就自动统一了。

### 2.3 KV Cache 的「写后读」间接

[u3-l2](./u3-l2-prefill-decode-flow.md) 提到 KV Cache 是 token 级管理的。在本讲里你要理解一个关键细节：**注意力计算用的 K/V 不是当场算完直接传给核函数，而是先写进内存池、再从内存池读回来**。

- 「写」：模板里的 `_post_cache_kv` 把刚算出的新 K/V 拷进 `mem_manager`（KV 内存管理器）。
- 「读」：注意力核（如 Llama 的 `_context_attention_kernel`）通过 `mem_manager.get_att_input_params(layer_index)` 把这一层的 K/V 取出来再算。

这种「写后读」的间接，是为了让 **RadixCache 前缀复用**（[u4-l2](./u4-l2-radix-prefix-cache.md)）和 **多级 KV 缓存**（[u6-l4](./u6-l4-multi-level-kv-cache.md)）成为可能——K/V 一旦落进统一管理的池子，就能被复用、换出、回填。

---

## 3. 本讲源码地图

本讲围绕 `layer_infer/` 目录下的「模板」与一个具体模型（Llama）的「实现」展开，对照阅读。

| 文件 | 作用 | 本讲角色 |
| --- | --- | --- |
| `lightllm/common/basemodel/layer_infer/base_layer_infer.py` | 所有推理层的根基类，定义抽象 `context_forward`/`token_forward` 与 TPSP 通信钩子、显存池分配 | **继承链顶端** |
| `lightllm/common/basemodel/layer_infer/transformer_layer_infer.py` | 中间基类，记录 `layer_num_` | 中间层 |
| `lightllm/common/basemodel/layer_infer/pre_layer_infer.py` / `post_layer_infer.py` | embedding 层 / post 层的中间基类 | 中间层 |
| `lightllm/common/basemodel/layer_infer/template/transformer_layer_infer_template.py` | transformer 层模板（**写死骨架 + 7 个钩子**） | **主战场** |
| `lightllm/common/basemodel/layer_infer/template/pre_layer_infer_template.py` | embedding 层模板（薄模板） | 对照 |
| `lightllm/common/basemodel/layer_infer/template/post_layer_infer_template.py` | post 层模板（薄模板） | 对照 |
| `lightllm/models/llama/layer_infer/transformer_layer_infer.py` | Llama 对 7 个钩子的具体实现 | **模板的填法范例** |
| `lightllm/models/llama/layer_infer/pre_layer_infer.py` / `post_layer_infer.py` | Llama 的 embedding 层 / post 层实现 | 对照 |
| `lightllm/common/basemodel/basemodel.py` | 模型基类，`_context_forward`/`_token_forward` 循环调用各层 | 调用方（复用 [u3-l2](./u3-l2-prefill-decode-flow.md) 结论） |

---

## 4. 核心概念与源码讲解

### 4.1 三类推理层与模板方法模式

#### 4.1.1 概念说明

整条前向（`_context_forward` / `_token_forward`）由三类层串成（见 [u3-l2 的 4.4 节](./u3-l2-prefill-decode-flow.md)）：

```text
input_ids
   │  ① Pre 层（embedding 层）：token id → 向量
   ▼
input_embs
   │  ② Transformer 层 × N：norm → attn → 残差 → norm → ffn → 残差
   ▼
last_input_embs
   │  ③ Post 层：final_norm → lm_head → logits
   ▼
predict_logits
```

三类层职责截然不同，但它们共享同一种组织方式——**模板方法模式**：一个根基类 `BaseLayerInfer` 抽象出统一接口，三类层各有一条中间基类，再各有一个「模板类（`*Tpl`）」把可复用的骨架写死、把模型相关细节留成钩子。

#### 4.1.2 核心流程：继承层次

三类层的类继承层次如下（从根到模板到具体模型）：

```text
BaseLayerInfer                          ← 统一接口 + 通用工具（显存池、TPSP 通信）
├── PreLayerInfer                       ← embedding 层中间基类
│   └── PreLayerInferTpl                ← embedding 层模板（薄：仅 eps_ + _norm 钩子）
│       └── LlamaPreLayerInfer          ← 具体实现
├── PostLayerInfer                      ← post 层中间基类
│   └── PostLayerInferTpl               ← post 层模板（薄：eps_ + _norm/_slice 钩子）
│       └── LlamaPostLayerInfer         ← 具体实现
└── TransformerLayerInfer               ← transformer 层中间基类（带 layer_num_）
    └── TransformerLayerInferTpl        ← transformer 层模板（厚：写死两条骨架 + 7 钩子）
        └── LlamaTransformerLayerInfer  ← 具体实现（填 7 个钩子）
```

一个关键对比：**transformer 模板是「厚模板」，Pre/Post 模板是「薄模板」**。

- transformer 层要重复 N 次、且结构高度统一，所以模板把整条「norm→attn→残差→norm→ffn→残差」骨架写死，子类只填 7 个钩子——这正是模板方法模式的完整形态。
- embedding 层和 post 层各自只有一层、且模型间差异较大，所以模板只提供几个默认值（`eps_`、`vocab_size_`、`embed_dim_`）和个别钩子，骨架（`context_forward`/`token_forward`）干脆交给具体模型类直接实现。

#### 4.1.3 源码精读

**根基类 `BaseLayerInfer`**（[`base_layer_infer.py:13-98`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/base_layer_infer.py#L13-L98)）定义统一接口与通用工具：

- 构造函数（[`base_layer_infer.py:14-16`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/base_layer_infer.py#L14-L16)）记录当前 rank 与 TP 世界大小 `tp_rank_` / `tp_world_size_`（张量并行相关，见 [u3-l4](./u3-l4-weights-and-tp-split.md)）。
- 抽象接口 `context_forward` / `token_forward`（[`base_layer_infer.py:18-22`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/base_layer_infer.py#L18-L22)）和 TPSP 用的 `overlap_tpsp_context_forward` / `overlap_tpsp_token_forward`（[`base_layer_infer.py:33-51`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/base_layer_infer.py#L33-L51)）都是 `raise Exception("need to impl")`——根基类只立规矩、不干活。
- 通用工具 `alloc_tensor`（[`base_layer_infer.py:24-31`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/base_layer_infer.py#L24-L31)）从全局显存池 `g_cache_manager` 分配张量，所有层都复用它，避免到处 `torch.empty` 制造显存碎片。
- TPSP 通信钩子 `_tpsp_allgather` / `_tpsp_reduce` / `_tpsp_sp_split`（[`base_layer_infer.py:53-98`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/base_layer_infer.py#L53-L98)）：仅在 `--enable_tpsp_mix_mode` 下真正通信，否则是直通（直接返回输入）。本讲把它们当成「进层切、出层聚」的对称操作即可，细节留到 [u6-l2](./u6-l2-microbatch-overlap-tpsp.md)。

**三个中间基类**都极薄。`TransformerLayerInfer`（[`transformer_layer_infer.py:4-11`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/transformer_layer_infer.py#L4-L11)）只多存两个字段：`layer_num_`（这是第几层）与 `network_config_`（模型 config）；`PreLayerInfer` / `PostLayerInfer` 同理只存 `network_config_`。

**三个模板类**：

- 薄模板 `PreLayerInferTpl`（[`pre_layer_infer_template.py:5-14`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/template/pre_layer_infer_template.py#L5-L14)）只有构造里设 `eps_ = 1e-5` 和一个 `_norm` 钩子（raise）。它**没有**写 `context_forward` / `token_forward`，所以 Llama 的 embedding 层直接在子类实现这两个方法（见 4.1.4）。

- 薄模板 `PostLayerInferTpl`（[`post_layer_infer_template.py:6-20`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/template/post_layer_infer_template.py#L6-L20)）设 `eps_`、`vocab_size_`、`embed_dim_`，并留两个钩子：`_norm`（final 归一化）与 `_slice_get_last_input`（决定取哪些行算 logits）。同样不写骨架。

- 厚模板 `TransformerLayerInferTpl` 是本讲主角，下一节（4.2）展开。

#### 4.1.4 代码实践

**实践目标**：亲手验证「厚模板 vs 薄模板」的分工差异，并区分骨架方法与钩子方法。

**操作步骤**：

1. 打开三个模板文件，分别统计它们定义了哪些「带具体实现的方法」（骨架）与哪些 `raise Exception("need to impl")` 的方法（钩子）。
2. 对 `PreLayerInferTpl`（[`pre_layer_infer_template.py:5-14`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/template/pre_layer_infer_template.py#L5-L14)）确认它**没有**实现 `context_forward` / `token_forward`；再去 Llama 的 [`pre_layer_infer.py:17-28`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_infer/pre_layer_infer.py#L17-L28) 看具体实现——embedding 查表 + 多卡 `all_reduce`，逻辑很短，难怪模板没东西可抽。
3. 对 `PostLayerInferTpl`（[`post_layer_infer_template.py:6-20`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/template/post_layer_infer_template.py#L6-L20)）确认同样不写骨架；再去 Llama 的 [`post_layer_infer.py:61-96`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_infer/post_layer_infer.py#L61-L96) 看 `_token_forward` / `token_forward` 的实现，注意 post 层把「取最后若干行 → final_norm → lm_head → 多卡 all_gather 拼 logits」整条逻辑都写在子类里。

**需要观察的现象**：transformer 模板「厚」到把整条层骨架写死；而 Pre/Post 模板「薄」到几乎只有默认值。这种不对称对应「重复 N 次的结构值得抽骨架、只出现一次的层直接写」这一工程取舍。

**预期结果**：能口头复述——「transformer 层的骨架在模板里、钩子在子类里；embedding 层与 post 层的骨架本身就在子类里、模板只给默认值」。

> 待本地验证：可在 `TransformerLayerInferTpl` 的 `context_forward` 入口加日志打印 `self.layer_num_` 与 `input_embdings.shape`，确认它会被 N 层实例各调用一次。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `TransformerLayerInfer`（中间基类）要单独存一个 `layer_num_`，而 `PreLayerInfer` / `PostLayerInfer` 不需要？

**参考答案**：transformer 层有 N 层、共享同一套推理类，必须用 `layer_num_` 区分「当前是第几层」（写/读 KV Cache 时要按层索引 `layer_index=self.layer_num_`，见 [`transformer_layer_infer_template.py:38`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/template/transformer_layer_infer_template.py#L38)）；而 embedding 层和 post 层全局只有一层，不需要层号。

**练习 2**：根基类 `BaseLayerInfer` 里的 `alloc_tensor` 为什么不直接用 `torch.empty`，而要走 `g_cache_manager`？

**参考答案**：推理期间层内会大量分配中间张量（FFN 的 gate_up 输出、注意力的 o 等）。集中走显存池 `g_cache_manager`（[`base_layer_infer.py:24-31`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/base_layer_infer.py#L24-L31)）可以统一回收、复用显存块，减少碎片与频繁分配开销，对 decode 这种高频路径尤其重要。

---

### 4.2 Transformer 层骨架：`context_forward` 与 `token_forward`

#### 4.2.1 概念说明

`TransformerLayerInferTpl`（[`transformer_layer_infer_template.py:11-147`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/template/transformer_layer_infer_template.py#L11-L147)）是模板方法模式的完整落地。它做了两件事：

1. **写死两条对称骨架**：`context_forward`（服务 prefill）与 `token_forward`（服务 decode），两者都是「norm → 注意力 → 残差 → norm → ffn → 残差」，**唯一差别是调用哪个注意力子流程**。
2. **声明 7 个钩子**（全部 `raise`），由子类（如 Llama）填具体计算。

构造函数（[`transformer_layer_infer_template.py:14-24`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/template/transformer_layer_infer_template.py#L14-L24)）把所有维度参数初始化为 `-1`（`eps_`、`tp_q/k/v/o_head_num_`、`head_dim_`、`embed_dim_`），并注释 `# need to set by subclass`——这些都是子类在 `__init__` 里根据 config 覆盖的。

#### 4.2.2 核心流程

两条骨架的伪代码（完全对称）：

```text
context_forward(h, state, w):          token_forward(h, state, w):
  a = _att_norm(h)                       a = _att_norm(h)
  o = context_attention_forward(a)       o = token_attention_forward(a)   ← 唯一不同
  h.add_(o)                              h.add_(o)
  b = _ffn_norm(h)                       b = _ffn_norm(h)
  f = _ffn(b)                            f = _ffn(b)
  h.add_(f)                              h.add_(f)
  return h                               return h
```

而 `context_attention_forward` 与 `token_attention_forward` 内部也几乎一样：

```text
context_attention_forward(a):           token_attention_forward(a):
  q, kv = _get_qkv(a)                     q, kv = _get_qkv(a)
  _post_cache_kv(kv)        # 写 KV 池     _post_cache_kv(kv)        # 写 KV 池
  o = context_attention_kernel(q, kv)     o = token_attention_kernel(q)  ← 唯一不同
  o = _get_o(o)                           o = _get_o(o)
  return o                                return o
```

可见 prefill 与 decode 在「层」这一级的分叉点，被一路下推到了**注意力核函数**那一个调用：prefill 用 `_context_attention_kernel`（一段 token 互相 attend），decode 用 `_token_attention_kernel`（1 个新 token 对全历史 KV attend）。这正是 [u3-l2](./u3-l2-prefill-decode-flow.md) 那句结论的逐层验证。

#### 4.2.3 源码精读

**两条骨架**（[`transformer_layer_infer_template.py:67-99`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/template/transformer_layer_infer_template.py#L67-L99)），逐行对照 2.1 节的两段残差公式：

```python
def context_forward(self, input_embdings, infer_state, layer_weight):
    input1 = self._att_norm(input_embdings, infer_state, layer_weight)   # Norm_1（钩子）
    o = self.context_attention_forward(input1, infer_state, layer_weight)# Attn（子骨架）
    input_embdings.add_(o.view(-1, self.embed_dim_))                     # 残差 1: h += Attn(Norm1(h))
    input1 = self._ffn_norm(input_embdings, infer_state, layer_weight)   # Norm_2（钩子）
    ffn_out = self._ffn(input1, infer_state, layer_weight)               # FFN（钩子）
    input_embdings.add_(ffn_out.view(-1, self.embed_dim_))               # 残差 2: h += FFN(Norm2(h))
    return input_embdings
```

`token_forward`（[`transformer_layer_infer_template.py:89-99`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/template/transformer_layer_infer_template.py#L89-L99)）与之一一对应，只把 `context_attention_forward` 换成 `token_attention_forward`。两条骨架用到的 4 个钩子：`_att_norm`、`_ffn_norm`、`_ffn`，加上注意力子流程里的 `_get_qkv`、注意力核、`_get_o`。

**注意力子骨架**（[`transformer_layer_infer_template.py:56-65` 与 `80-87`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/template/transformer_layer_infer_template.py#L56-L87)）也高度对称：

- `context_attention_forward`：`_get_qkv` → `_post_cache_kv` → `_context_attention_wrapper_run` → `_get_o`。
- `token_attention_forward`：`_get_qkv` → `_post_cache_kv` → `_token_attention_kernel` → `_get_o`。

两者都先算 Q/K/V（`_get_qkv`），都把新 K/V 写进 KV 内存池（`_post_cache_kv`），最后都做输出投影（`_get_o`）。区别只在中间那一步：

- prefill 走 `_context_attention_wrapper_run`（[`transformer_layer_infer_template.py:101-147`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/template/transformer_layer_infer_template.py#L101-L147)）——它内部判断 `if torch.cuda.is_current_stream_capturing()`：录制 prefill CUDA Graph 时做「分段录制」的图管线处理，否则直接调 `_context_attention_kernel`。之所以 prefill 需要这层 wrapper、而 decode 不需要，是因为 **prefill CUDA Graph 是「逐注意力核」精细录制的，而 decode CUDA Graph 是把整条 `_token_forward` 一起录制的**（见 [u6-l1](./u6-l1-cuda-graph.md)）。本节只需知道这层 wrapper 的存在，细节留到 u6-l1。
- decode 直接调 `_token_attention_kernel(q, infer_state, layer_weight)`——注意它**不传 `kv` 参数**，因为 decode 的注意力核会从 KV 内存池读**全历史** K/V（详见 4.3.3）。

**`_post_cache_kv` 是少数「非钩子」的具体方法**（[`transformer_layer_infer_template.py:35-42`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/template/transformer_layer_infer_template.py#L35-L42)）：它把刚算出的 `cache_kv` 通过 `mem_manager.operator.copy_kv_to_mem_manager(layer_index=self.layer_num_, mem_index=infer_state.mem_index, kv=cache_kv)` 写进 KV 内存池。所有模型这一步都一样（都是「把新 K/V 落进池子」），所以模板把它写死、不做成钩子。这正是 2.3 节说的「写」动作。

**子类要填的 7 个钩子**（[`transformer_layer_infer_template.py:26-54`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/template/transformer_layer_infer_template.py#L26-L54)）：

| 钩子 | 作用 | 属于哪段残差 |
| --- | --- | --- |
| `_att_norm` | 注意力前归一化（Norm_1） | 注意力子段 |
| `_get_qkv` | Q/K/V 投影 + 旋转位置编码 | 注意力子段 |
| `_context_attention_kernel` | prefill 注意力核 | 注意力子段（prefill） |
| `_token_attention_kernel` | decode 注意力核 | 注意力子段（decode） |
| `_get_o` | 注意力输出投影（O 矩阵） | 注意力子段 |
| `_ffn_norm` | FFN 前归一化（Norm_2） | FFN 子段 |
| `_ffn` | 前馈网络 | FFN 子段 |

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：以 `transformer_layer_infer_template.py` 为例，说明 `context_forward` 与 `token_attention_forward` 如何分别服务 prefill 和 decode，并指出子类需要覆写哪些钩子方法。

**操作步骤**：

1. 打开 [`transformer_layer_infer_template.py:67-99`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/template/transformer_layer_infer_template.py#L67-L99)，并排对比 `context_forward` 与 `token_forward`：圈出**唯一不同**的一行——`o = self.context_attention_forward(...)` vs `o = self.token_attention_forward(...)`。这就是 prefill 与 decode 在「层」级的全部差异。
2. 再往下一层，对比 `context_attention_forward`（[`L56-65`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/template/transformer_layer_infer_template.py#L56-L65)）与 `token_attention_forward`（[`L80-87`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/template/transformer_layer_infer_template.py#L80-L87)）：前三步（`_get_qkv` / `_post_cache_kv`）和最后一步（`_get_o`）完全相同，差别只在中间——一个调 `_context_attention_wrapper_run`、一个调 `_token_attention_kernel`。
3. 数清楚子类必须覆写的钩子：在 [`L26-54`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/template/transformer_layer_infer_template.py#L26-L54) 里所有 `raise Exception("need to impl")` 的方法，共 7 个（见上表）。再去 Llama 的 [`transformer_layer_infer.py`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_infer/transformer_layer_infer.py) 逐个核对，确认这 7 个钩子在 Llama 里都被覆写、而 `context_forward` / `token_forward` 两条骨架 Llama **完全没有重写**（Llama 只继承、复用模板的骨架）。
4. 在 `basemodel.py` 的调用方确认配对关系：`_context_forward` 的层循环调 `layer.context_forward`（[`basemodel.py:671`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L671)），`_token_forward` 的层循环调 `layer.token_forward`（[`basemodel.py:727`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L727)）。两条调用链分别对应 prefill 与 decode。

**需要观察的现象**：prefill 的整条层路径是 `_context_forward → context_forward → context_attention_forward → _context_attention_kernel`；decode 的整条层路径是 `_token_forward → token_forward → token_attention_forward → _token_attention_kernel`。两条路径直到「最末端的注意力核」才分叉，前面所有结构（norm、残差、QKV、写 KV 池、输出投影、FFN）都完全相同。

**预期结果**：能给出一句完整回答——「`context_forward` 与 `token_forward` 是写死在模板里的对称骨架，prefill 走前者、decode 走后者；它们只在注意力子流程里分叉到不同核函数；子类（如 Llama）只需覆写 `_att_norm`/`_ffn_norm`/`_get_qkv`/`_context_attention_kernel`/`_token_attention_kernel`/`_get_o`/`_ffn` 这 7 个钩子，骨架本身直接继承复用」。

> 待本地验证：在 `context_forward` 与 `token_forward` 的首行各加一行日志打印 `self.layer_num_` 与阶段标识，发一次请求观察 prefill 期间只触发 `context_forward`、decode 期间只触发 `token_forward`。

#### 4.2.5 小练习与答案

**练习 1**：如果某新模型的注意力前/FFN 前用的是不同的归一化（比如 LayerNorm 而非 RMSNorm），需要改动模板的骨架吗？

**参考答案**：不需要。归一化的具体实现是钩子 `_att_norm` / `_ffn_norm`，骨架只调钩子、不关心钩子内部是 RMSNorm 还是 LayerNorm。新模型只需在子类覆写这两个钩子、调用对应权重（如 `layer_weight.att_norm_weight_(...)`）即可，骨架那行 `input1 = self._att_norm(...)` 一字不改。

**练习 2**：为什么 `_post_cache_kv` 不做成钩子、而是模板里写死的具体方法？

**参考答案**：因为「把新 K/V 拷进 KV 内存池」这件事对所有模型都一样——都是调用 `mem_manager.operator.copy_kv_to_mem_manager(layer_index, mem_index, kv)`（[`transformer_layer_infer_template.py:35-42`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/template/transformer_layer_infer_template.py#L35-L42)）。没有模型间差异，就无须做成钩子；做成钩子反而让每个子类重复写同一段代码，违背模板方法模式的初衷。

---

### 4.3 注意力与 FFN 子流程

#### 4.3.1 概念说明

骨架决定了「顺序」，钩子决定了「具体怎么算」。本节用 Llama 的实现把注意力子流程和 FFN 子流程的「填法」讲透。

**注意力子流程**要完成标准注意力：

\[ \text{Attn}(x) = W_O \cdot \big( \text{softmax}\big( QK^T / \sqrt{d_k} \big) V \big), \quad Q=xW_Q,\ K=xW_K,\ V=xW_V \]

其中还要叠加旋转位置编码（RoPE）。在 LightLLM 里它被拆成 4 个钩子：

1. `_get_qkv`：算出 Q、K、V，并给 Q/K 套上 RoPE。
2. `_post_cache_kv`：把新 K/V 写进内存池（模板写死，非钩子）。
3. 注意力核（`_context_attention_kernel` / `_token_attention_kernel`）：从内存池读回 K/V，算 softmax(QK^T)V。
4. `_get_o`：输出投影 W_O。

**FFN 子流程**对 Llama（SwiGLU 激活）是：

\[ \text{FFN}(x) = W_{\text{down}} \big( \text{SiLU}(x W_{\text{gate}}) \odot (x W_{\text{up}}) \big) \]

其中 `gate` 与 `up` 两个投影通常融合成一个 `gate_up_proj`，算完再用 `silu_and_mul` 拆开相乘。

#### 4.3.2 核心流程

```text
注意力子流程（以 prefill 为例，Llama 实现）:
  _get_qkv(a):
    a = _tpsp_allgather(a)          # (TPSP) 进层先聚合
    q  = q_proj.mm(a)
    kv = kv_proj.mm(a)              # q,kv 投影
    rotary_emb_fwd(q, k, cos, sin)  # 给 q,k 套 RoPE
    return q, kv
  _post_cache_kv(kv):               # 模板写死：写进 mem_manager 池
  _context_attention_kernel(q):     # 注意力核
    _k, _v = mem_manager.get_att_input_params(layer_index)  # 从池里读回 K/V
    o = prefill_att_state.prefill_att(q, _k, _v, alloc_func)
  _get_o(o):
    o = o_proj.mm(o)
    o = _tpsp_reduce(o)             # (TPSP) 出层 reduce

FFN 子流程（Llama 实现）:
  _ffn(b):
    b = _tpsp_allgather(b)
    ffn2 = _ffn_tp(b)
    ffn2 = _tpsp_reduce(ffn2)
  _ffn_tp(b):
    up_gate = gate_up_proj.mm(b)    # 融合的 gate+up 投影
    silu_and_mul_fwd(up_gate, ffn1) # SiLU(gate) * up
    ffn2 = down_proj.mm(ffn1)
```

#### 4.3.3 源码精读（Llama 的钩子填法）

Llama 的 `LlamaTransformerLayerInfer`（[`llama/layer_infer/transformer_layer_infer.py:16-166`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_infer/transformer_layer_infer.py#L16-L166)）逐个填了模板的 7 个钩子。

**构造函数与维度**（[`L19-29`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_infer/transformer_layer_infer.py#L19-L29)）：把模板里那批 `-1` 的字段按 config 填成真值——`tp_q_head_num_`、`tp_k/v_head_num_`（按 `tp_world_size_` 切分，GQA/MQA 用 `max(...,1)` 保底）、`head_dim_`、`embed_dim_`，并取 `eps_ = config["rms_norm_eps"]`。末尾调 `_bind_func()`。

**`_bind_func` 与 `partial`**（[`L31-38`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_infer/transformer_layer_infer.py#L31-L38)）：用 `functools.partial` 把 `_att_norm` / `_ffn_norm` 这两个钩子方法绑定到实例属性上。这是一种「把钩子装配成实例属性」的写法——模型可以在实例层面灵活替换某个钩子的实现（部分模型会用它切换不同 norm 实现），同时避免在热路径上反复做方法解析。

**两个归一化钩子**（[`L69-77`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_infer/transformer_layer_infer.py#L69-L77)）：`_att_norm` / `_ffn_norm` 都委托给权重对象的 RMSNorm：`layer_weight.att_norm_weight_(input, eps, alloc_func)`。注意归一化的具体计算「藏」在权重类里（`att_norm_weight_` 是一个可调用对象），推理层只负责调它。

**`_get_qkv` 钩子**（[`L79-97`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_infer/transformer_layer_infer.py#L79-L97)）：先 `_tpsp_allgather`（TPSP 下聚合），再分别做 `q_proj.mm` 与 `kv_proj.mm`（K/V 融合在一次 `kv_proj` 投影里，再 view 拆开），然后调 triton kernel `rotary_emb_fwd` 给 Q 与 K 套旋转位置编码。可选地，DP prefill 均衡时做 `_all_to_all_unbalance_get` 重排。注意：**RoPE 只作用于 Q 和 K，不作用于 V**——这是 RoPE 的标准做法。

**注意力核钩子**——这是 prefill/decode 分叉的真正落点：

- `_context_attention_kernel`（[`L40-56`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_infer/transformer_layer_infer.py#L40-L56)）：先 `_k, _v = infer_state.mem_manager.get_att_input_params(layer_index=self.layer_num_)` **从内存池读回这一层的 K/V**，再把 q reshape，调 `infer_state.prefill_att_state.prefill_att(q, _k, _v, alloc_func)`。`prefill_att_state` 来自注意力后端（[u3-l5](./u3-l5-attention-backends.md)），负责 prefill 的一段 token 互相 attend。
- `_token_attention_kernel`（[`L58-67`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_infer/transformer_layer_infer.py#L58-L67)）：同样从内存池读 K/V，但调 `infer_state.decode_att_state.decode_att(q, _k, _v, alloc_func)`——decode 注意力让 1 个新 token 对**全历史** K/V attend。两个核的差异完全封装在注意力后端里，推理层只是「读池 + 调后端」。

> 这里正是 2.3 节「写后读」的闭环：`_post_cache_kv` 把新 K/V **写**进 `mem_manager`（按 `layer_index`），两个注意力核再用 `get_att_input_params(layer_index)` **读**回来。K/V 一旦进池，就能被 RadixCache 复用、被多级缓存换出（[u4-l2](./u4-l2-radix-prefix-cache.md) / [u6-l4](./u6-l4-multi-level-kv-cache.md)）。

**`_get_o` 钩子**（[`L99-109`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_infer/transformer_layer_infer.py#L99-L109)）：reshape 后 `o_proj.mm` 做输出投影，再 `_tpsp_reduce`（TPSP 下 reduce-scatter、普通 TP 下 all-reduce）——多卡并行的注意力结果必须 reduce 才能得到正确输出。

**FFN 钩子 `_ffn` 与 `_ffn_tp`**（[`L111-129`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_infer/transformer_layer_infer.py#L111-L129)）：`_ffn` 包一层 TPSP allgather/reduce；`_ffn_tp` 是真正的计算：`gate_up_proj.mm`（融合的 gate+up）→ triton kernel `silu_and_mul_fwd`（SiLU(gate)⊙up）→ `down_proj.mm`。这就是 SwiGLU FFN。

注意 Llama 还实现了 `overlap_tpsp_token_forward` / `overlap_tpsp_context_forward`（[`L143-165`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_infer/transformer_layer_infer.py#L143-L165)），它们就是把两个 infer_state 各跑一次 `token_forward` / `context_forward`，服务于 microbatch overlap（[u6-l2](./u6-l2-microbatch-overlap-tpsp.md)），本讲了解存在即可。

#### 4.3.4 代码实践

**实践目标**：追踪「K/V 写入内存池后再被注意力核读回」这条闭环，并核对 FFN 的 SwiGLU 三段计算。

**操作步骤**：

1. 在模板 [`_post_cache_kv`（L35-42）](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/template/transformer_layer_infer_template.py#L35-L42) 里看到「写」：`mem_manager.operator.copy_kv_to_mem_manager(layer_index=self.layer_num_, ...)`。
2. 在 Llama [`_context_attention_kernel`（L40-47）](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_infer/transformer_layer_infer.py#L40-L47) 里看到「读」：`_k, _v = infer_state.mem_manager.get_att_input_params(layer_index=self.layer_num_)`。确认两处用的是**同一个 `layer_index`**——写第 L 层、就读第 L 层。
3. 对照 FFN：[`_ffn_tp`（L118-129）](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_infer/transformer_layer_infer.py#L118-L129) 是三步 `gate_up_proj.mm → silu_and_mul_fwd → down_proj.mm`，对照 4.3.1 的 SwiGLU 公式，确认 `silu_and_mul_fwd` 干的就是 \(\text{SiLU}(\text{gate}) \odot \text{up}\)。

**需要观察的现象**：注意力核函数本身（`prefill_att` / `decode_att`）不在推理层文件里，而在注意力后端（`prefill_att_state` / `decode_att_state`）；推理层只做「读池 → 调后端」。这说明「层推理」与「注意力算子」是解耦的。

**预期结果**：能解释清楚——「`_get_qkv` 算出 Q/K/V 并套 RoPE；`_post_cache_kv` 把 K/V 写进内存池；注意力核再从内存池按 `layer_index` 读回 K/V 算注意力；FFN 是融合的 gate_up + silu_and_mul + down 三段」。并能指出 MoE 模型（[u5-l4](./u5-l4-moe-model-infer.md)）会覆写 `_ffn` 钩子、把这里的普通 FFN 换成带专家路由的 fused_moe。

> 待本地验证：若想确认 RoPE 只作用于 Q/K，可在 `rotary_emb_fwd` 调用处前后分别打印 Q、K、V 的哈希值，观察只有 Q、K 被改、V 不变。

#### 4.3.5 小练习与答案

**练习 1**：注意力核明明是 `_get_qkv` 算出来的 `cache_kv`，为什么核函数里又要从 `mem_manager.get_att_input_params` 重新读 K/V，而不直接用传进来的 `kv`？

**参考答案**：因为 decode 阶段的注意力需要「1 个新 token 对**全历史** K/V attend」，而全历史 K/V 不只是本轮算的这点——还包括此前 prefill 与历史 decode 写进池子的全部 K/V，以及可能从前缀缓存（RadixCache）复用来的 K/V。所以核函数必须从统一管理的内存池读「完整」K/V，而不能只用本轮 `_get_qkv` 产出的那段。（注：Llama 的 prefill 核 `_context_attention_kernel` 虽签名带 `kv` 参数，但实现里同样从池里读、忽略该参数，保持两条核的读取方式一致。）

**练习 2**：MoE 模型（如 Mixtral/Deepseek）相比 Llama，主要会覆写哪个钩子？为什么其余钩子基本不变？

**参考答案**：主要覆写 `_ffn` 钩子——把 Llama 的普通 SwiGLU FFN 换成带「专家路由（top-k gating）」的 MoE FFN（底层用 `fused_moe` triton kernel，见 [u5-l4](./u5-l4-moe-model-infer.md)）。其余钩子（`_att_norm`、`_ffn_norm`、`_get_qkv`、注意力核、`_get_o`）大多不变，因为 MoE 与普通模型的差异集中在 FFN 子段，注意力子段结构相同——这正是模板方法模式「只换变化的那个钩子」的价值。

---

## 5. 综合实践

把本讲三类层 + 模板方法 + 注意力/FFN 串成一个「填模板」的动手任务。

**任务**：假设你要给一个结构近似 Llama 的新模型（比如某 GQA + SwiGLU + RoPE 的模型）写 transformer 层推理，按模板方法模式的指引，列出「能直接继承复用的」与「必须自己写的」各是什么，并定位到源码。

**步骤**：

1. **直接继承复用（不写）**：`context_forward` / `token_forward` / `context_attention_forward` / `token_attention_forward` 四条骨架，以及 `_post_cache_kv`（写 KV 池）和根基类的 `alloc_tensor` / TPSP 钩子。确认它们的源头：[`transformer_layer_infer_template.py:35-99`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/template/transformer_layer_infer_template.py#L35-L99)。
2. **必须自己写（7 个钩子）**：仿照 Llama 的 [`transformer_layer_infer.py`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_infer/transformer_layer_infer.py) 填 `_att_norm` / `_ffn_norm` / `_get_qkv` / `_context_attention_kernel` / `_token_attention_kernel` / `_get_o` / `_ffn`，并在 `__init__` 里把 `eps_`、`tp_q/k/v/o_head_num_`、`head_dim_`、`embed_dim_` 按新模型的 config 填好。
3. **回答三个判断题**（口头即可）：
   - 新模型若用 LayerNorm 而非 RMSNorm，要改骨架吗？（答：不改，覆写 `_att_norm`/`_ffn_norm` 即可。）
   - 新模型若是 MoE，要改哪些？（答：主要覆写 `_ffn`。）
   - 新模型若想换注意力算子（如用 flashinfer 而非 triton），要改推理层吗？（答：不用改推理层，注意力核只是「读池 + 调后端」，换后端由注意力后端机制处理，见 [u3-l5](./u3-l5-attention-backends.md)。）

**进阶**：用一张表把「骨架方法」「具体工具方法」「钩子方法」三类在 `TransformerLayerInferTpl` 里分别列出，标注每个方法的「定义者（模板）」与「覆写者（Llama）」，体会「骨架不重写、钩子全覆写」的分工。

> 待本地验证：可对照 `docs/EN/source/models/add_new_model.md` 的官方指南（[u5-l3](./u5-l3-add-new-model.md) 会详细展开），确认你列出的「必须自己写」清单与官方建议一致。

---

## 6. 本讲小结

- 整条前向由三类层串成：**embedding 层（Pre）→ N 层 transformer（Transformer）→ post 层（Post）**，分别负责 token 向量化、norm→attn→残差→norm→ffn→残差、final_norm+lm_head 出 logits。
- 三类层都用**模板方法模式**：根基类 `BaseLayerInfer` 立统一接口（`context_forward`/`token_forward` 抽象、`alloc_tensor`/TPSP 工具），`*Tpl` 模板类写骨架、留钩子，具体模型类填钩子。
- transformer 模板是「厚模板」——写死两条对称骨架 [`context_forward`/`token_forward`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/template/transformer_layer_infer_template.py#L67-L99)；Pre/Post 模板是「薄模板」——只给默认值，骨架由具体模型直接实现。
- prefill 与 decode 在「层」级完全统一，只在注意力子流程分叉到不同核：prefill 走 `_context_attention_kernel`、decode 走 `_token_attention_kernel`，其余（norm、残差、QKV、写 KV 池、输出投影、FFN）全部相同。
- 子类（如 Llama）只需覆写 7 个钩子（`_att_norm`/`_ffn_norm`/`_get_qkv`/两个注意力核/`_get_o`/`_ffn`），骨架直接继承复用；`_post_cache_kv` 因全模型一致被模板写死。
- K/V 走「写后读」闭环：`_post_cache_kv` 把新 K/V 按层号写进 `mem_manager` 池，注意力核再按层号 `get_att_input_params` 读回——这统一了「本轮算的」「历史写的」「前缀缓存复用的」三类 K/V，为 RadixCache 与多级缓存奠基。

---

## 7. 下一步学习建议

- 想知道 K/V 内存池 `mem_manager` 内部如何分配/回收槽位、`get_att_input_params` 如何按层取回——请读 [u4-l1 KV Cache 内存管理](./u4-l1-kv-cache-memory-manager.md)。
- 想理解 prefill/decode 末尾 logits 如何变成最终 token（top-k/top-p 与惩罚）——请读 [u3-l6 采样与后处理](./u3-l6-sampling-postprocess.md)。
- 想搞懂本讲反复出现的「注意力后端」`prefill_att_state`/`decode_att_state` 如何按优先级自动选择 fa3/flashinfer/triton——请读 [u3-l5 注意力后端机制](./u3-l5-attention-backends.md)。
- 想看 MoE 模型如何覆写 `_ffn` 钩子、MLA 如何改变注意力核——请读 [u5-l4 MoE 模型推理](./u5-l4-moe-model-infer.md) 与 [u5-l5 MLA 注意力实现](./u5-l5-mla-attention.md)。
- 想理解 `_context_attention_wrapper_run` 里 `torch.cuda.is_current_stream_capturing()` 那段图管线——请读 [u6-l1 CUDA Graph 捕获与重放](./u6-l1-cuda-graph.md)。
