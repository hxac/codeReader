# 如何新增模型支持

## 1. 本讲目标

LightLLM 官方宣称「一行命令、一个目录即可支持一个新模型」，这句话的底气来自它在 `lightllm/common/basemodel` 里抽象出的一套「插槽 + 模板」模型骨架。本讲要把这套骨架彻底打开，让你学完后能够：

1. 说清楚新增一个模型**到底要新建哪些文件、继承哪些基类/模板**，并能列出一张完整的「文件 → 职责 → 父类」对照表。
2. 理解**模板复用**的本质：模板类把 prefill/decode 的对称骨架写死，只留若干 `need to impl` 的钩子，子类填钩子即可，从而把 46+ 模型族的重复代码降到最低。
3. 掌握**注册 → import → 分发 → 校验**的完整闭环：装饰器登记、`config.json` 的 `model_type` 查表匹配、`_verify_params` 参数断言、权重加载后的 `verify_load` 检查，以及项目里实际的回归验证方式。
4. 动手为一个结构类似 llama 的新模型写出**最小骨架代码**（不必可运行），把本讲知识串起来。

本讲承接 [u5-l2 以 Llama 为例理解完整模型实现](./u5-l2-llama-model-walkthrough.md)，那里我们已经把 llama 的六个插槽填法讲透；本讲反过来问：**如果我是一个全新模型，这六个插槽、这些文件从哪里来、怎么填最快？**

## 2. 前置知识

在动手之前，请确认你已经理解下面几个概念（都来自前序讲义，这里只做最简回顾）：

- **TpPartBaseModel 与六个插槽**（[u3-l1](./u3-l1-tp-part-base-model.md)）：基类声明六个默认为 `None` 的类属性——两个权重类、三个推理类、一个推理状态类；子类只填插槽、几乎不写 `__init__`。`TpPart` 表示每张 GPU 只持有张量并行的「一片」。
- **三层推理层结构**（[u3-l3](./u3-l3-layer-infer-template.md)）：一次前向 = embedding 层（Pre）→ N 层 transformer → post 层（final_norm + lm_head 出 logits）。
- **权重的两层结构**（[u3-l4](./u3-l4-weights-and-tp-split.md)）：外层 `TransformerLayerWeight`/`PreAndPostLayerWeight` 只是容器；内层「元权重」（`ROWMMWeight`/`COLMMWeight`/`KVROWNMMWeight`/`EmbeddingWeight` 等）才是真正存储、按 TP 切分、校验的最小单元。
- **模型注册机制**（[u5-l1](./u5-l1-model-registry.md)）：`@ModelRegistry("xxx")` 装饰器在 import 期把模型类登记进以 `model_type` 为键的表；`get_model` 按 `config.json` 查表分发。

> 关键直觉：在 LightLLM 里，「新增模型」几乎不是写新逻辑，而是**把新模型的算子填进既有骨架的钩子里**。骨架越能复用，你要写的代码越少。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `docs/EN/source/models/add_new_model.md` | 官方「如何新增模型」指南，给出概念框架与一个 bloom 完整示例（部分写法已随代码演进而过时，本讲会指出差异） |
| `lightllm/common/basemodel/basemodel.py` | 模型基类 `TpPartBaseModel`，包含一条写死的初始化流水线与若干可覆写的 `_init_*` 钩子 |
| `lightllm/common/basemodel/layer_infer/template/transformer_layer_infer_template.py` | transformer 层推理「厚模板」：写死 prefill/decode 对称骨架，留 7 个钩子 |
| `lightllm/common/basemodel/layer_infer/template/pre_layer_infer_template.py` | embedding 层「薄模板」，只留 `_norm` 钩子 |
| `lightllm/common/basemodel/layer_infer/template/post_layer_infer_template.py` | post 层「薄模板」，留 `_norm` 与 `_slice_get_last_input` 钩子 |
| `lightllm/models/registry.py` | 注册中心 `_ModelRegistries`、`@ModelRegistry` 装饰器、`get_model` 分发 |
| `lightllm/models/__init__.py` | 逐行 import 各 `model.py`，触发装饰器登记 |
| `lightllm/models/llama/model.py` | llama 模型类：注册 + 填六个插槽 + `_init_custom` 算 RoPE（现代写法范例） |
| `lightllm/models/llama/layer_infer/transformer_layer_infer.py` | llama transformer 推理：只覆写模板的 7 个钩子 |
| `lightllm/models/llama/layer_weights/transformer_layer_weight.py` | llama transformer 权重：用元权重组装（现代写法范例） |
| `lightllm/models/llama/infer_struct.py` | llama 推理状态：在 `init_some_extra_state` 里现算 cos/sin |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**新增模型流程**、**模板复用**、**注册与校验**。

### 4.1 新增模型流程

#### 4.1.1 概念说明

「新增一个模型」在 LightLLM 里的字面含义是：在 `lightllm/models/` 下新建一个**以模型 `model_type` 命名的目录**，里面按固定的「三件套」范式放好若干文件，框架就能自动发现并加载它。这套范式是 [u1-l3](./u1-l3-repo-structure.md) 讲过的「一模型一目录」约定，本讲把它落到可操作的文件清单上。

一个模型目录之所以叫「三件套」，是因为它主要由三块构成：

1. **model.py**：模型总装类，继承 `TpPartBaseModel`，负责注册 + 填六个插槽 + 模型特有的初始化钩子。这是整个模型的入口。
2. **layer_weights/****：权重类，决定如何从 HuggingFace 文件读入并按 TP 切分。
3. **layer_infer/****：推理类，决定 prefill/decode 各层如何前向计算。

此外还有两个**可选**但常见的文件：

- **infer_struct.py**：推理状态类，仅当模型需要在层间传递特殊状态（如 llama 的 RoPE cos/sin）时才需要；不需要时直接用默认 `InferStateInfo`。
- **triton_kernel/**：模型专属的高性能算子（如 llama 的 `rotary_emb`、`silu_and_mul`、`token_attention_nopad_att1`）。这是性能关键路径下沉的地方。

> 名词解释——**`model_type`**：HuggingFace `config.json` 里的 `"model_type"` 字段（如 `"llama"`、`"bloom"`、`"qwen2"`）。它既是注册表的键，也是 LightLLM 选模型的依据，因此新模型目录名应与之一致。

#### 4.1.2 核心流程

新增一个结构类似 llama 的模型，标准流程是五步：

```text
1. 在 lightllm/models/<model_type>/ 下建目录与 __init__.py
2. 写 layer_weights/ 两个权重类（继承 PreAndPostLayerWeight、TransformerLayerWeight）
3. 写 layer_infer/ 三个推理类（继承 Pre/Post/Transformer 的 *Tpl 模板，填钩子）
4. 写 model.py（@ModelRegistry 注册 + 填六个插槽 + 必要时覆写 _init_* 钩子）
   └─ 4a. 若有特殊层间状态，再写 infer_struct.py 并把 infer_state_class 指向它
5. 在 lightllm/models/__init__.py 末尾加一行 import，触发注册
```

理解这条流程的关键，是先看清「骨架已经写好了什么、留给你写什么」。下面两张表把「继承谁」和「骨架替你做了什么」讲清楚。

**表一：要新建的文件与继承的父类**

| 文件 | 类 | 继承的父类/模板 | 你要做什么 |
| --- | --- | --- | --- |
| `model.py` | `XTpPartModel` | `TpPartBaseModel` | 注册 + 填六个插槽 + 个性化 `_init_*` |
| `layer_weights/pre_and_post_layer_weight.py` | `XPreAndPostLayerWeight` | `PreAndPostLayerWeight` | 声明 embedding/lm_head/norm 的元权重 |
| `layer_weights/transformer_layer_weight.py` | `XTransformerLayerWeight` | `TransformerLayerWeight` | 声明 qkv/o/ffn/norm 的元权重 |
| `layer_infer/pre_layer_infer.py` | `XPreLayerInfer` | `PreLayerInferTpl` | 覆写 `context_forward`/`token_forward`（多数可直接用默认） |
| `layer_infer/post_layer_infer.py` | `XPostLayerInfer` | `PostLayerInferTpl` | 覆写 `_norm`/`_slice_get_last_input` |
| `layer_infer/transformer_layer_infer.py` | `XTransformerLayerInfer` | `TransformerLayerInferTpl` | 覆写 7 个钩子（见 4.2） |
| `infer_struct.py`（可选） | `XInferStateInfo` | `InferStateInfo` | 覆写 `init_some_extra_state` 附加状态 |

**表二：骨架（基类/模板）已经替你做的事**

| 骨架 | 已经替你写好的部分 | 留给你的钩子 |
| --- | --- | --- |
| `TpPartBaseModel.__init__` | 一条近 20 步的初始化流水线（读 config、建权重/内存/推理层、加载、CUDA Graph、warmup…） | `_init_config`/`_verify_params`/`_init_mem_manager`/`_init_custom` 等 |
| `TransformerLayerInferTpl` | prefill/decode 两条对称残差骨架（含 KV 写回 `_post_cache_kv`） | `_att_norm`/`_ffn_norm`/`_get_qkv`/两个注意力核/`_get_o`/`_ffn` |
| `PreLayerInferTpl` / `PostLayerInferTpl` | 接口与默认值 | `_norm`（Pre/Post）、`_slice_get_last_input`（Post） |

#### 4.1.3 源码精读

**(a) 一模型一目录的真实样貌：llama 目录**

先用 `git ls-files` 看 llama 模型目录到底由哪些文件构成，这是「三件套」范式最可靠的证据：

[lightllm/models/llama/ 目录文件清单](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama) —— 由 `git ls-files lightllm/models/llama/` 可得：

```text
lightllm/models/llama/__init__.py
lightllm/models/llama/infer_struct.py                 # 可选：RoPE 状态
lightllm/models/llama/model.py                        # 入口：注册 + 插槽
lightllm/models/llama/layer_infer/post_layer_infer.py
lightllm/models/llama/layer_infer/pre_layer_infer.py
lightllm/models/llama/layer_infer/transformer_layer_infer.py
lightllm/models/llama/layer_weights/pre_and_post_layer_weight.py
lightllm/models/llama/layer_weights/transformer_layer_weight.py
lightllm/models/llama/triton_kernel/rotary_emb.py     # 可选：专属算子
lightllm/models/llama/triton_kernel/silu_and_mul.py
lightllm/models/llama/triton_kernel/token_attention_nopad_att1.py
lightllm/models/llama/yarn_rotary_utils.py            # 可选：辅助工具
```

可以看到「`model.py` + `layer_infer/`（三个推理类）+ `layer_weights/`（两个权重类）」就是核心三件套；`infer_struct.py` 和 `triton_kernel/` 是模型按需添加的部分。新建一个结构相似的模型，文件清单几乎是一一对应的。

**(b) 入口 model.py：注册 + 填插槽**

[lightllm/models/llama/model.py:21-37](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/model.py#L21-L37) —— llama 的入口极其简短，`@ModelRegistry("llama")` 完成注册，类体只是把六个插槽填上具体类，`__init__` 直接 `super().__init__(kvargs)`：

```python
@ModelRegistry("llama")
class LlamaTpPartModel(TpPartBaseModel):
    # weight class
    pre_and_post_weight_class = LlamaPreAndPostLayerWeight
    transformer_weight_class = LlamaTransformerLayerWeight
    # infer class
    pre_layer_infer_class = LlamaPreLayerInfer
    post_layer_infer_class = LlamaPostLayerInfer
    transformer_layer_infer_class = LlamaTransformerLayerInfer
    # infer state class
    infer_state_class = LlamaInferStateInfo

    def __init__(self, kvargs):
        super().__init__(kvargs)
        return
```

注意：现在的构造函数签名是 `__init__(self, kvargs)`（一个字典），而不是官方指南里展示的老式 `__init__(self, tp_rank, world_size, weight_dir, ...)` 多参数写法——这是 `add_new_model.md` 与当前代码的一处**明显差异**，照搬指南的老签名会直接报错。新模型统一走 `kvargs`。

**(c) 可覆写的初始化钩子在流水线里的位置**

`TpPartBaseModel.__init__` 是一条写死的流水线，你新增模型时主要靠覆写其中的几个 `_init_*` 钩子来注入个性：

[lightllm/common/basemodel/basemodel.py:104-120](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L104-L120) —— 注意 `_init_custom` 在 `_load_hf_weights` **之前**被调用，所以它适合「为算子准备常量」（如 RoPE 表），而不是「读权重」：

```python
self._init_config()       # 钩子①：读 config.json、字段名归一化（常覆写）
self._verify_must()
self._verify_params()     # 钩子②：参数断言（常覆写）
self._init_quant()
self._init_weights()      # 建权重空壳
self._init_req_manager()
self._init_mem_manager()  # 钩子③：内存管理器（GQA/MLA 等会覆写）
...
self._init_infer_layer()
self._init_some_value()
self._init_custom()       # 钩子④：模型个性化初始化（如 RoPE）
self._load_hf_weights()   # 真正读 .safetensors
```

llama 正是覆写了①②③④这四个钩子：[lightllm/models/llama/model.py:39-55](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/model.py#L39-L55) 的 `_init_config`（补 `num_key_value_heads`）与 `_verify_params`（断言 KV 头数能被 TP 整除），[lightllm/models/llama/model.py:57-68](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/model.py#L57-L68) 的 `_init_mem_manager`（按 KV 头数建内存管理器），以及 [lightllm/models/llama/model.py:70-99](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/model.py#L70-L99) 的 `_init_custom`（按 `rope_scaling.rope_type` 分发到五种 RoPE 初始化）。

**(d) 权重组装的现代写法：元权重**

[lightllm/models/llama/layer_weights/transformer_layer_weight.py:36-60](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_weights/transformer_layer_weight.py#L36-L60) —— 新写法不再手写切片，而是先声明「HF 权重名」：

```python
def _init_weight_names(self):
    self._q_weight_name = f"model.layers.{self.layer_num_}.self_attn.q_proj.weight"
    self._k_weight_name = f"model.layers.{self.layer_num_}.self_attn.k_proj.weight"
    self._v_weight_name = f"model.layers.{self.layer_num_}.self_attn.v_proj.weight"
    ...
    self._gate_weight_name = f"model.layers.{self.layer_num_}.mlp.gate_proj.weight"
    self._up_weight_name   = f"model.layers.{self.layer_num_}.mlp.up_proj.weight"
    self._down_weight_name = f"model.layers.{self.layer_num_}.mlp.down_proj.weight"
```

[lightllm/models/llama/layer_weights/transformer_layer_weight.py:62-111](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_weights/transformer_layer_weight.py#L62-L111) —— 再把名字绑到「元权重」上，由元权重自己负责 TP 切分与校验：

```python
self.q_proj = ROWMMWeight(in_dim, out_dims=[q_out_dim], weight_names=self._q_weight_name, ...)       # 列并行，无需 all-reduce
self.kv_proj = KVROWNMMWeight(in_dim, kv_head_num, head_dim,
                              weight_names=[self._k_weight_name, self._v_weight_name], ...)          # k/v 融合
self.o_proj = COLMMWeight(in_dim, out_dims=[out_dim], weight_names=self._o_weight_name, ...)         # 行并行，需 all-reduce
self.gate_up_proj = ROWMMWeight(self.n_embed, [self.n_inter, self.n_inter],
                                weight_names=[self._gate_weight_name, self._up_weight_name], ...)    # gate/up 融合
self.down_proj = COLMMWeight(self.n_inter, [self.n_embed], weight_names=self._down_weight_name, ...)
```

> 这是与官方指南最大的一处演进：指南里 bloom 的 `load_hf_weights` 用大量手写 `split_n_embed * self.tp_rank_ : ...` 切片来处理 TP，而现代写法把这些细节封装进了 `ROWMMWeight`/`COLMMWeight`/`KVROWNMMWeight` 元权重（见 [u3-l4](./u3-l4-weights-and-tp-split.md)），新模型只要「填名字 + 选元权重类型」即可，几乎不写切片代码。

#### 4.1.4 代码实践

**实践目标**：用源码阅读的方式，确认「新增模型 = 复制一份 llama 目录并改名」这一直觉。

**操作步骤**：

1. 在仓库根目录执行 `git ls-files lightllm/models/llama/` 与 `git ls-files lightllm/models/bloom/`，对比两个模型的文件清单差异。
2. 打开 [lightllm/models/bloom/model.py:11-43](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/bloom/model.py#L11-L43)，观察 bloom 与 llama 的 `model.py` 在「注册 + 填插槽」结构上是否同构。
3. 注意 bloom 额外覆写了 `_init_att_backend`，强制使用 `TritonAttBackend`（因为 bloom 的 alibi 位置编码不被 fa3/flashinfer 默认支持）——这说明**新模型若用了非标准位置编码/注意力，常需要在 model.py 里覆写注意力后端**。

**需要观察的现象**：

- llama 与 bloom 的 `model.py` 主体结构几乎一模一样，差别只在「填的是哪个类」「覆写了哪些钩子」。
- bloom 目录**没有** `infer_struct.py`，因为 bloom 不需要特殊的层间状态（用默认 `InferStateInfo`），印证了该文件是可选的。

**预期结果**：你会得出结论——新增模型的工作量与「该模型偏离标准 transformer 结构的程度」成正比；越是标准结构，越能直接复用 llama 的目录骨架。

> 待本地验证：第 2、3 步涉及的具体行号建议你亲自打开文件核对，因为不同提交间行号会漂移。

#### 4.1.5 小练习与答案

**练习 1**：如果一个新模型的 FFN 不是 SwiGLU（gate/up/down）而是普通的两层 MLP（intermediate/dense），你在 `transformer_layer_weight.py` 里要改哪一处？

**参考答案**：改 `_init_ffn` 里的元权重声明——不再用融合的 `gate_up_proj`（`ROWMMWeight` 带两个名字），而是声明一个单独的列并行 `ffn_1_weight`（`ROWMMWeight`）和一个行并行 `ffn_2_weight`（`COLMMWeight`）；同时 `_init_weight_names` 里把 HF 权重名改成对应的 `mlp.dense_h_to_4h` / `mlp.dense_4h_to_h`（bloom 就是这种结构，可对照官方指南示例）。推理侧的 `_ffn` 钩子也要相应改成 gelu + 两层 matmul。

**练习 2**：为什么 `_init_custom` 必须在 `_load_hf_weights` 之前执行？

**参考答案**：`_init_custom` 的典型用途是预计算推理要用的常量（如 llama 的 RoPE cos/sin 表），它只依赖 `config.json` 里的超参（`rope_theta`、`max_position_embeddings` 等），不依赖权重张量本身。而 `_load_hf_weights` 才从 `.safetensors` 读入真实权重。把 `_init_custom` 放在加载之前，既能保证算子常量就绪，也避免在加载大权重后再触发额外的显存峰值。

### 4.2 模板复用

#### 4.2.1 概念说明

如果说 4.1 讲的是「要写哪些文件」，4.2 讲的就是「每个文件里**少写什么**」。LightLLM 用经典的**模板方法模式（Template Method）**来压降重复代码：

- 模板类（`*Tpl`）写死算法骨架（不可变的执行顺序）。
- 骨架里调用若干**钩子方法**，在模板里被声明为 `raise Exception("need to impl")`。
- 具体模型子类只覆写钩子，骨架自动复用。

transformer 层的模板 `TransformerLayerInferTpl` 是「厚模板」（骨架重、钩子多），pre/post 层模板是「薄模板」（骨架轻、钩子少）。这是 LightLLM 能用相似代码量支持 46+ 模型族的核心原因。

> 名词解释——**钩子（hook）**：模板预先留出的、要求子类实现的方法空位。模板在固定位置调用它，子类填什么逻辑，模板就执行什么逻辑。

#### 4.2.2 核心流程

transformer 层一次前向（以 prefill 为例）的骨架调用顺序是固定的：

```text
context_forward(emb, infer_state, layer_weight):        # 模板写死的骨架
  ├─ input1 = _att_norm(emb, ...)        # 钩子①：注意力前归一化
  ├─ o = context_attention_forward(input1, ...)
  │    ├─ q, cache_kv = _get_qkv(...)    # 钩子②：Q/K/V 投影（含 RoPE）
  │    ├─ _post_cache_kv(cache_kv, ...)  # 模板实现：把 K/V 写回 mem_manager
  │    ├─ o = _context_attention_kernel(...)  # 钩子③：prefill 注意力核
  │    └─ o = _get_o(o, ...)             # 钩子④：输出投影
  ├─ emb += o                            # 第一段残差（模板写死）
  ├─ input1 = _ffn_norm(emb, ...)        # 钩子⑤：FFN 前归一化
  ├─ ffn_out = _ffn(input1, ...)         # 钩子⑥：前馈网络
  └─ emb += ffn_out                      # 第二段残差（模板写死）
```

decode 阶段的 `token_forward` 骨构完全对称，唯一不同是注意力走 `_token_attention_kernel`（让 1 个新 token 复用全历史 KV）。**两条残差、两次归一化、KV 写回全由模板包办**；子类只需填 ①~⑥ 这 6 类钩子（注意力核算 2 个，共 7 个）。

#### 4.2.3 源码精读

**(a) 模板写死的骨架与留空的钩子**

[lightllm/common/basemodel/layer_infer/template/transformer_layer_infer_template.py:26-54](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/template/transformer_layer_infer_template.py#L26-L54) —— 模板里把 6 类钩子都声明为「必须由子类实现」：

```python
def _att_norm(self, input, infer_state, layer_weight) -> torch.Tensor:
    raise Exception("need to impl")
def _ffn_norm(self, input, infer_state, layer_weight) -> torch.Tensor:
    raise Exception("need to impl")
def _get_qkv(self, input, infer_state, layer_weight) -> Tuple[torch.Tensor, torch.Tensor]:
    raise Exception("need to impl")
def _context_attention_kernel(self, q, kv, infer_state, layer_weight, out=None) -> torch.Tensor:
    raise Exception("need to impl")
def _token_attention_kernel(self, q, infer_state, layer_weight, out=None) -> torch.Tensor:
    raise Exception("need to impl")
def _get_o(self, input, infer_state, layer_weight) -> torch.Tensor:
    raise Exception("need to impl")
def _ffn(self, input, infer_state, layer_weight) -> torch.Tensor:
    raise Exception("need to impl")
```

而 `_post_cache_kv` 模板**自己实现了**——它把算出的 K/V 拷进 `mem_manager` 的池里，这是「写后读」KV 闭环的「写」半边：

[lightllm/common/basemodel/layer_infer/template/transformer_layer_infer_template.py:35-42](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/template/transformer_layer_infer_template.py#L35-L42) —— 子类无需关心 KV 怎么落池：

```python
def _post_cache_kv(self, cache_kv, infer_state, layer_weight):
    mem_manager = infer_state.mem_manager
    mem_manager.operator.copy_kv_to_mem_manager(
        layer_index=self.layer_num_,
        mem_index=infer_state.mem_index,
        kv=cache_kv,
    )
```

**(b) 模板写死的两条残差骨架**

[lightllm/common/basemodel/layer_infer/template/transformer_layer_infer_template.py:67-99](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/template/transformer_layer_infer_template.py#L67-L99) —— prefill 的 `context_forward` 与 decode 的 `token_forward` 结构对称，残差拼接由模板完成：

```python
def context_forward(self, input_embdings, infer_state, layer_weight):
    input1 = self._att_norm(input_embdings, infer_state, layer_weight)
    o = self.context_attention_forward(input1, infer_state, layer_weight)
    input_embdings.add_(o.view(-1, self.embed_dim_))      # 第一段残差
    input1 = self._ffn_norm(input_embdings, infer_state, layer_weight)
    ffn_out = self._ffn(input1, infer_state, layer_weight)
    input_embdings.add_(ffn_out.view(-1, self.embed_dim_)) # 第二段残差
    return input_embdings
```

**(c) 子类如何填钩子：llama transformer 推理**

[lightllm/models/llama/layer_infer/transformer_layer_infer.py:16-29](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_infer/transformer_layer_infer.py#L16-L29) —— llama 子类在 `__init__` 里设好头数/dim，然后只覆写钩子：

```python
class LlamaTransformerLayerInfer(TransformerLayerInferTpl):
    def __init__(self, layer_num, network_config):
        super().__init__(layer_num, network_config)
        self.eps_ = network_config["rms_norm_eps"]
        self.tp_q_head_num_ = network_config["num_attention_heads"] // self.tp_world_size_
        self.tp_k_head_num_ = max(network_config["num_key_value_heads"] // self.tp_world_size_, 1)
        ...
```

[lightllm/models/llama/layer_infer/transformer_layer_infer.py:40-67](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_infer/transformer_layer_infer.py#L40-L67) —— 注意力核钩子把活儿委托给注意力后端状态对象（`prefill_att_state.prefill_att` / `decode_att_state.decode_att`），子类自己不写注意力数学，这正是 [u3-l5](./u3-l5-attention-backends.md) 注意力后端抽象的价值：

```python
def _context_attention_kernel(self, q, kv, infer_state, layer_weight):
    _k, _v = infer_state.mem_manager.get_att_input_params(layer_index=self.layer_num_)  # 读回历史 KV
    _q = q.view(-1, self.tp_q_head_num_, self.head_dim_)
    o_tensor = infer_state.prefill_att_state.prefill_att(q=_q, k=_k, v=_v, alloc_func=self.alloc_tensor)
    return o_tensor.view(q.shape)
```

[lightllm/models/llama/layer_infer/transformer_layer_infer.py:79-97](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_infer/transformer_layer_infer.py#L79-L97) —— `_get_qkv` 钩子做 Q/KV 投影 + RoPE 旋转（仅 q、k，v 不旋转）：

```python
def _get_qkv(self, input, infer_state, layer_weight):
    input = self._tpsp_allgather(input, infer_state)
    q = layer_weight.q_proj.mm(input)
    cache_kv = layer_weight.kv_proj.mm(input).view(-1, (self.tp_k_head_num_ + self.tp_v_head_num_), self.head_dim_)
    rotary_emb_fwd(q.view(-1, self.tp_q_head_num_, self.head_dim_),
                   cache_kv[:, 0 : self.tp_k_head_num_, :],
                   infer_state.position_cos, infer_state.position_sin)
    return q, cache_kv
```

> 复用结论：llama 的 transformer 推理类**只覆写了模板的 7 个钩子**，没有重写 `context_forward`/`token_forward` 骨架，也没有自己写 KV 落池。新模型若结构接近 llama，直接拷贝这个文件改名、再微调钩子即可。

#### 4.2.4 代码实践

**实践目标**：通过对比模板的「钩子声明」与子类的「钩子实现」，验证模板复用的完整性——确认子类确实没有漏填钩子。

**操作步骤**：

1. 在 [transformer_layer_infer_template.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/template/transformer_layer_infer_template.py) 里数出所有 `raise Exception("need to impl")` 的方法名。
2. 在 [llama/layer_infer/transformer_layer_infer.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_infer/transformer_layer_infer.py) 里用编辑器搜索这些方法名，确认 llama 是否逐个给出了实现。
3. 思考：如果你想加一个「在注意力之后、残差之前」插入额外算子的新模型，模板方法模式的局限在哪里？

**需要观察的现象**：模板要求实现的 7 个钩子，llama 全部给出了实现；模板里**已实现**的 `_post_cache_kv`，llama 没有覆写（直接复用）。

**预期结果**：你会确认「子类 = 模板骨架 + 7 个钩子实现」，没有任何隐藏的必填项；同时也发现模板方法模式的局限——**执行顺序被骨架锁死**，若新模型要在非钩子位置插入算子（如在外层加一层自定义残差），就只能整体覆写 `context_forward`/`token_forward`，无法用钩子表达。

> 待本地验证：步骤 2 的「逐个搜索」建议你亲手做一遍，统计实际命中的方法数。

#### 4.2.5 小练习与答案

**练习 1**：模板的 `_context_attention_wrapper_run`（[transformer_layer_infer_template.py:101-147](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/template/transformer_layer_infer_template.py#L101-L147)）里有一段「`torch.cuda.is_current_stream_capturing()`」分支，它在解决什么问题？新增模型时你需要关心它吗？

**参考答案**：它在处理 **CUDA Graph 捕获** 场景：当处于图捕获（warmup 录图）时，注意力核的输入张量需要做「地址无关化」（`tensor_to_no_ref_tensor`）并在一个临时图里先探出输出形状/dtype，再挂接到真实的图录制流程。这是 prefill CUDA Graph 与注意力核协作的底层 plumbing。**新增模型时通常不需要关心**——只要你正确实现了 `_context_attention_kernel` 钩子并返回形状正确的 `o_tensor`，模板会自动处理图捕获适配。只有在注意力核输出形状依赖运行期数据等极端情况下才需要介入。

**练习 2**：pre/post 层模板为什么是「薄模板」？它们各自留了哪个钩子？

**参考答案**：因为 embedding 层和 post 层的逻辑相对简单、变体少，骨架能复用的部分有限，所以模板只提供接口与默认值。`PreLayerInferTpl` 只留 `_norm`（embedding 后的归一化，部分模型没有则可不实现）；`PostLayerInferTpl` 留 `_norm`（final_norm）与 `_slice_get_last_input`（从 hidden 里取出每个请求最后一个 token 的 hidden state 用于算 logits）。可见 [pre_layer_infer_template.py:13-14](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/template/pre_layer_infer_template.py#L13-L14) 与 [post_layer_infer_template.py:16-20](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_infer/template/post_layer_infer_template.py#L16-L20)。

### 4.3 注册与校验

#### 4.3.1 概念说明

写完文件不等于能用——还差**注册**让框架发现它、**校验**保证它能在当前配置下正确运行。这两步构成「写代码 → 跑起来 → 跑对」闭环的后半段。

- **注册**：靠 `@ModelRegistry("model_type")` 装饰器。装饰器本身是副作用——它把类塞进一张表。而触发装饰器的，是 `lightllm/models/__init__.py` 里那行 `from ...model import ...`。换句话说，**「被注册」=「被 import」**；忘了在 `__init__.py` 加 import，模型就不会出现在表里。
- **校验**：分三层。① 启动期 `_verify_params` 用 `assert` 拦截配置不兼容（如 KV 头数不能被 TP 整除）；② 加载期元权重的 `verify_load` 检查每个权重是否真的读到了（缺权重会被标记）；③ 运行期通过 `skills/test_model/` 的回归脚本（lm_eval 评测精度）确认正确性。

> 名词解释——**条件分发**：当同一个 `model_type` 下注册了多个候选类时（如 reward 模型与普通 LLM 共用一种结构），用 `condition`（一个 `dict → bool` 的谓词）来消歧。详见 [u5-l1](./u5-l1-model-registry.md)。

#### 4.3.2 核心流程

```text
启动 api_server
  └─ ModeBackend.init_model → get_model(model_cfg, kvargs)
       ├─ model_cfg 来自 config.json，取 model_type
       ├─ 查注册表 self._registry[model_type] → 候选列表
       ├─ 用 condition 过滤：0 命中→报不支持；1 命中→用；多命中→只留带 condition 的再断言恰剩 1
       └─ 实例化 model_class(kvargs)
            └─ TpPartBaseModel.__init__ 流水线
                 ├─ _init_config（字段名归一化）
                 ├─ _verify_params（assert 配置兼容性）   ← 校验①
                 ├─ _init_weights → 各元权重 verify_load  ← 校验②
                 └─ ... warmup / cuda graph ...
跑通后
  └─ skills/test_model/<scenario>/SKILL.md 用 lm_eval 评测精度  ← 校验③
```

#### 4.3.3 源码精读

**(a) 装饰器：副作用式登记**

[lightllm/models/registry.py:23-45](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/registry.py#L23-L45) —— `ModelRegistry` 实际是 `_ModelRegistries` 的实例，被「调用」时返回一个 `decorator`，该 decorator 把类追加到以 `model_type` 为键的列表里，原类原样返回：

```python
class _ModelRegistries:
    def __init__(self):
        self._registry: Dict[str, List[ModelConfig]] = collections.defaultdict(list)

    def __call__(self, model_type, is_multimodal=False, condition=None):
        def decorator(model_class):
            model_types = [model_type] if isinstance(model_type, str) else model_type
            for mt in model_types:
                self._registry[mt].append(
                    ModelConfig(model_class=model_class, is_multimodal=is_multimodal, condition=condition)
                )
            return model_class
        return decorator
```

要点：`model_type` 可传字符串或列表（一个类多别名，如多种命名指向同一实现）；表用 `defaultdict(list)` 允许同名多候选（给条件分发留余地）。

**(b) import 触发登记**

[lightllm/models/__init__.py:1-9](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/__init__.py#L1-L9) —— 文件顶部逐行 import 各模型的 `model.py`，import 动作执行模块顶层代码，从而触发 `@ModelRegistry` 装饰器：

```python
from lightllm.models.mixtral.model import MixtralTpPartModel
from lightllm.models.bloom.model import BloomTpPartModel
from lightllm.models.llama.model import LlamaTpPartModel
from lightllm.models.starcoder.model import StarcoderTpPartModel
...
```

> 新增模型后，**必须**在这里加一行 `from lightllm.models.<new>.model import <New>TpPartModel`，否则装饰器永不执行、模型永不注册——这是最容易遗漏的一步。

**(c) 分发：查表 + 条件消歧**

[lightllm/models/registry.py:47-68](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/registry.py#L47-L68) —— `get_model` 按 `config.json` 的 `model_type` 查表，用 `condition` 过滤候选：

```python
def get_model(self, model_cfg, model_kvargs):
    model_type = model_cfg.get("model_type", "")
    configs = self._registry.get(model_type, [])
    matches = []
    for cfg in configs:
        if cfg.condition is None or cfg.condition(model_cfg):   # 无条件默认入选；有条件则需为真
            matches.append(cfg)
    if len(matches) == 0:
        raise ValueError(f"Model type {model_type} is not supported.")
    if len(matches) > 1:
        matches = [m for m in matches if m.condition is not None]  # 多命中时丢弃无条件默认，只留特例
    assert len(matches) == 1, "..."   # 必须唯一可定
    model = matches[0].model_class(model_kvargs)
    return model, matches[0].is_multimodal
```

新模型若注册名（`model_type`）与已有模型冲突，就必须提供互斥的 `condition`，否则会触发最后的 `assert`。

**(d) 校验①：参数断言**

[lightllm/models/llama/model.py:51-55](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/model.py#L51-L55) —— llama 在 `_verify_params` 里把「会导致切分错乱」的配置提前拦截：

```python
def _verify_params(self):
    assert self.load_way in ["HF", "DS"], "llama only supports HF and DS format to load Now!"
    assert self.config["num_key_value_heads"] % self.tp_world_size_ == 0
    assert self.config["num_attention_heads"] % self.tp_world_size_ == 0
    return
```

新模型应仿照这里，把「本模型对 TP/数据类型/加载格式」的硬性要求写成 `assert`，让不兼容的配置在启动早期就失败，而不是在推理时出莫名其妙的形状错误。

**(e) 校验②：权重加载完整性**

权重的 `verify_load` 机制在 [u3-l4](./u3-l4-weights-and-tp-split.md) 已详述：每个元权重加载后置 `load_ok` 标志，外层容器遍历检查，缺权重会被暴露。新模型只要正确声明了 `_init_weight_names`（HF 权重名），这套校验自动生效，无需额外代码。

**(f) 校验③：精度回归（项目实际做法）**

LightLLM 没有针对单个模型的 pytest 单元测试，而是用 `skills/test_model/` 下的一组 **SKILL** 做端到端回归：拉起 `api_server` → 用 `lm_eval` 跑 GSM8K 等基准 → 比对精度。例如 [skills/test_model/qwen3-8b-gsm8k-scenarios/SKILL.md](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/skills/test_model/qwen3-8b-gsm8k-scenarios/SKILL.md) 描述了同一模型在七种服务配置（基线、FP8、TP-SP、CPU cache…）下的回归流程。新增模型后，最直接的「跑对」验证就是仿照它写一个最小回归：启动服务 + `curl /generate` 看输出，或用 `lm_eval` 评一个小数据集。

#### 4.3.4 代码实践

**实践目标**：亲手验证「删掉 import → 模型消失」的注册机制，理解 import 与注册的等价性。

**操作步骤**（**只读实验，不要真改源码**）：

1. 阅读确认 [lightllm/models/__init__.py:3](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/__init__.py#L3) 有 `from lightllm.models.llama.model import LlamaTpPartModel`。
2. 阅读确认 [lightllm/models/llama/model.py:21](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/model.py#L21) 有 `@ModelRegistry("llama")`。
3. **思想实验**（不实际执行）：如果注释掉第 1 步那行 import，再启动 `python -m lightllm.server.api_server --model_dir <llama权重>`，会在哪一步、以什么报错失败？
4. 对照 [registry.py:56-57](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/registry.py#L56-L57) 的 `raise ValueError(f"Model type {model_type} is not supported.")` 确认你的判断。

**需要观察的现象**：报错发生在 `get_model` 查表时——因为装饰器没被触发，注册表里没有 `"llama"` 键，`matches` 为空，抛 `ValueError`。

**预期结果**：你会确认「注册是 import 的副作用」，从而记住新增模型的最后一步（在 `__init__.py` 加 import）是不可省的。

> 待本地验证：第 3 步的思想实验结论建议有条件时在隔离环境里实际复现一次。

#### 4.3.5 小练习与答案

**练习 1**：新模型注册名想用 `"llama"`（与已有 llama 同名），但它其实是一个 reward 模型变体。装饰器该怎么写？分发时会经历什么？

**参考答案**：装饰器要带 `condition`：`@ModelRegistry("llama", condition=is_reward_model())`（`is_reward_model()` 见 [registry.py:113-115](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/registry.py#L113-L115)，按 `architectures` 含 `RewardModel` 判定）。分发时：普通 llama（无 condition）与本 reward 变体（有 condition）都会进入 `matches`，于是 `len(matches) > 1`，进入「只保留带 condition 的」分支——若当前 config 是 reward 架构，则 reward 候选 condition 为真被保留、普通 llama 因无 condition 被丢弃，恰剩 1 个，正常实例化；若当前 config 不是 reward 架构，reward 候选 condition 为假不会进入 `matches`，只剩普通 llama，也正常。前提是两个候选的 condition 互斥，否则触发 `assert len(matches) == 1`。

**练习 2**：`_verify_params` 里的 `assert` 失败与 `get_model` 抛 `ValueError`，两者在「错误 surfaced 的时机」上有什么区别？为什么这很重要？

**参考答案**：`get_model` 的 `ValueError` 发生在**模型类还没实例化**时（表里查不到），属于「框架不认识这个模型」；`_verify_params` 的 `assert` 发生在**实例化流水线中**（类已找到、正在初始化），属于「框架认识它，但当前配置不兼容」。时机区别很重要：前者意味着你可能忘了注册/import 或 `model_type` 写错，后者意味着模型本身支持、只是 `--tp` 等参数选得不合法。清晰的分层报错能让你快速定位问题出在「注册」还是「配置」。

## 5. 综合实践

**任务**：为一个结构类似 llama 的新模型（假设它叫 `myllama`，与 llama 的唯一区别是 FFN 的激活函数从 SiLU 换成 GELU，且没有 GQA——即 `num_key_value_heads == num_attention_heads`）列出需要新建的文件、继承的基类/模板，并写出最小骨架代码。

> 以下为**示例代码**，仅说明结构，不可直接运行（省略了 import 与部分实现细节）。

**第 1 步：文件清单与继承关系**

```text
lightllm/models/myllama/
├── __init__.py
├── model.py                         # MyllamaTpPartModel(TpPartBaseModel)
├── infer_struct.py                  # MyllamaInferStateInfo(InferStateInfo)  —— 复用 llama 的 RoPE 状态
├── layer_infer/
│   ├── __init__.py
│   ├── pre_layer_infer.py           # MyllamaPreLayerInfer(PreLayerInferTpl)         —— 几乎照抄 llama
│   ├── post_layer_infer.py          # MyllamaPostLayerInfer(PostLayerInferTpl)       —— 几乎照抄 llama
│   └── transformer_layer_infer.py   # MyllamaTransformerLayerInfer(TransformerLayerInferTpl) —— 改 _ffn
└── layer_weights/
    ├── __init__.py
    ├── pre_and_post_layer_weight.py # MyllamaPreAndPostLayerWeight(PreAndPostLayerWeight) —— 照抄 llama
    └── transformer_layer_weight.py  # MyllamaTransformerLayerWeight(TransformerLayerWeight) —— 照抄 llama
```

> 由于 myllama 与 llama 结构高度相似，绝大多数文件可以**直接复制 llama 对应文件改名**，只改两处：注册名与 `_ffn` 钩子的激活函数。

**第 2 步：model.py（注册 + 插槽 + 校验）**

```python
# 示例代码：lightllm/models/myllama/model.py
from lightllm.models.registry import ModelRegistry
from lightllm.common.basemodel import TpPartBaseModel
# ... 省略各组件 import ...

@ModelRegistry("myllama")                       # 与 config.json 的 model_type 一致
class MyllamaTpPartModel(TpPartBaseModel):
    pre_and_post_weight_class   = MyllamaPreAndPostLayerWeight
    transformer_weight_class    = MyllamaTransformerLayerWeight
    pre_layer_infer_class       = MyllamaPreLayerInfer
    post_layer_infer_class      = MyllamaPostLayerInfer
    transformer_layer_infer_class = MyllamaTransformerLayerInfer
    infer_state_class           = MyllamaInferStateInfo            # 复用带 RoPE 状态的类

    def __init__(self, kvargs):
        super().__init__(kvargs)

    def _init_config(self):
        super()._init_config()
        self._reset_num_key_value_heads()       # myllama 无 GQA，KV 头数 = 注意力头数

    def _verify_params(self):
        assert self.load_way in ["HF", "DS"]
        assert self.config["num_attention_heads"] % self.tp_world_size_ == 0
        # myllama 无 GQA，故 num_key_value_heads == num_attention_heads，整除性同上

    def _init_custom(self):
        self._init_to_get_rotary()              # 复用基类提供的基础 RoPE
```

**第 3 步：transformer_layer_infer.py（只改 `_ffn`）**

```python
# 示例代码：lightllm/models/myllama/layer_infer/transformer_layer_infer.py
import torch.functional as F
from lightllm.common.basemodel import TransformerLayerInferTpl

class MyllamaTransformerLayerInfer(TransformerLayerInferTpl):
    def __init__(self, layer_num, network_config):
        super().__init__(layer_num, network_config)
        self.eps_ = network_config["rms_norm_eps"]
        self.tp_q_head_num_ = network_config["num_attention_heads"] // self.tp_world_size_
        self.tp_k_head_num_ = self.tp_q_head_num_                   # 无 GQA
        self.tp_v_head_num_ = self.tp_q_head_num_
        self.tp_o_head_num_ = self.tp_q_head_num_
        self.head_dim_ = network_config["hidden_size"] // network_config["num_attention_heads"]
        self.embed_dim_ = network_config["hidden_size"]

    # 其余 6 个钩子（_att_norm/_ffn_norm/_get_qkv/两个注意力核/_get_o）与 llama 相同，照抄

    def _ffn(self, input, infer_state, layer_weight):               # ★ 唯一实质改动
        input = input.view(-1, self.embed_dim_)
        up_gate_out = layer_weight.gate_up_proj.mm(input)
        size = up_gate_out.size(1)
        gate_out, up_out = up_gate_out[:, : size // 2], up_gate_out[:, size // 2:]
        act_out = F.gelu(gate_out, approximate="tanh")             # SiLU → GELU
        act_out = act_out * up_out
        ffn2_out = layer_weight.down_proj.mm(act_out)
        return ffn2_out
```

**第 4 步：注册（最容易漏的一步）**

在 [lightllm/models/__init__.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/__init__.py) 末尾加一行：

```python
# 示例代码
from lightllm.models.myllama.model import MyllamaTpPartModel
```

**第 5 步：验证闭环**

1. **校验①**：故意用 `--tp 3`（假设头数 32 不能被 3 整除）启动，预期 `_verify_params` 的 `assert` 失败。
2. **校验②**：故意把 `_init_weight_names` 里的 HF 权重名写错，预期 `verify_load` 报缺权重。
3. **校验③**：正确启动 `python -m lightllm.server.api_server --model_dir <myllama权重> --tp 2`，用 `curl` 发一次 `/generate` 请求，确认能输出文本；有条件再用 `lm_eval` 评一个小数据集比对精度。

**验收标准**：能说出每个新建文件继承自哪个模板/基类、能解释为什么 `_ffn` 是唯一需要改的钩子、能复述「注册 = import 副作用」并指出 `__init__.py` 那行不可省。

## 6. 本讲小结

- **新增模型 = 一个目录 + 三件套**：在 `lightllm/models/<model_type>/` 下放 `model.py`（入口）、`layer_weights/`（两个权重类）、`layer_infer/`（三个推理类），可选 `infer_struct.py` 与 `triton_kernel/`；文件清单可直接对照 llama 目录复制改名。
- **现代写法与官方指南有两处关键差异**：构造函数统一用 `__init__(self, kvargs)`（而非老式多参数），权重用「填 HF 名 + 选元权重类型」的元权重写法（而非手写 TP 切片）——照搬指南老写法会报错。
- **模板方法模式压降重复**：transformer 推理模板写死 prefill/decode 两条对称残差骨架与 KV 落池，只留 7 个 `need to impl` 钩子；llama 子类正好只覆写这 7 个钩子，结构越接近 llama 改动越少。
- **可覆写的初始化钩子**有固定位置：`_init_config`/`_verify_params`/`_init_mem_manager`/`_init_custom` 等，其中 `_init_custom` 在 `_load_hf_weights` 之前，适合算 RoPE 等常量。
- **注册是 import 的副作用**：`@ModelRegistry` 装饰器登记、`models/__init__.py` 的 import 触发；忘了加 import 就等于没注册，`get_model` 查表时报 `not supported`。
- **校验分三层**：启动期 `_verify_params` 断言、加载期元权重 `verify_load`、运行期 `skills/test_model` 的 lm_eval 回归；三者构成「跑起来 → 跑对」闭环。

## 7. 下一步学习建议

- 想看「结构偏离标准 transformer 时要改多少」的真实对照，建议阅读 **bloom**（alibi 位置编码，需覆写 `_init_att_backend` 强制用 Triton 后端）与 **mixtral/deepseek2**（MoE 层替换普通 FFN）的 `model.py` 与 `transformer_layer_infer.py`，这正是 [u5-l4 MoE 模型推理](./u5-l4-moe-model-infer.md) 的主题。
- 若你关注 MLA（多头潜变量注意力）这种「连权重结构与 KV 形状都变了」的深度定制，继续看 [u5-l5 MLA 注意力实现](./u5-l5-mla-attention.md)，那里会展示新增模型工作量与「结构偏离程度」成正比的极致案例。
- 若想在「填好钩子后」进一步榨性能，进入第六单元：[u6-l1 CUDA Graph 捕获与重放](./u6-l1-cuda-graph.md) 讲你新增的模型如何自动获得图重放加速，[u6-l3 FP8 KV Cache 量化](./u6-l3-fp8-kv-quant.md) 讲如何通过 `--llm_kv_type` 让你的模型走量化内存管理器——这些都不需要改模型代码，是基类已经留好的扩展点。
