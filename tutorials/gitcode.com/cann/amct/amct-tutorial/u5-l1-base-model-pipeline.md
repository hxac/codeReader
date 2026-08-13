# LLM 模型适配基类 BaseModel

## 1. 本讲目标

本讲是「模型适配」单元的第一篇。在前几讲里，我们已经知道 AMCT 的 LLM 训练后量化（PTQ）是一条四阶段链路（eval → extract_ptq_data → ptq → deploy），并且这些 Workflow 把真正的活儿交给一个「模型适配器」去做——逐层加载权重、逐层前向、把一层切成可量化的小单元。

学完本讲，你应当能够：

- 说清 `BaseModel` 如何用 HuggingFace 的 `AutoConfig/AutoTokenizer/AutoModelForCausalLM` 加载模型配置，以及它为什么不把整个模型一次性塞进显存，而是「搭空壳 + 逐层读盘」。
- 画出 embedding → block → head 三段式逐层前向流水线，并解释 `Catcher` 如何在 embedding 阶段一次性捕获 `position_ids/attention_mask/position_embeddings`，供后续每一层复用。
- 解释 `iter_ptq_units` 如何根据 `--quant_target` 把一层 decoder 划分为 attn / mlp / moe 三类 PTQ 单元，以及 `iter_deploy_bindings` 如何在部署阶段枚举所有 `QuantLinear`。

## 2. 前置知识

本讲默认你已掌握以下概念（前几讲已建立）：

- **PTQ 单元（PtqUnit）**：AMCT 量化的最小工作单元，attn 和 mlp 各算 1 个单元，MoE 的每个 expert 各算 1 个单元（见 u4-l2）。
- **量化目标 quant_target**：取值 `mlp/moe/attn-linear/attn-cache`，是「模块分类名」而非具体层名，决定本阶段量化哪类子模块（见 u3-l1）。
- **重建（reconstruction）**：训练目标是量化子模块的输出逼近原始浮点子模块的输出，只训练算法的可学习参数、原始权重冻结（见 u4-l3）。
- **逐层（layer-wise）PTQ**：AMCT 不把整模型一起量化，而是「加载一层 → 量化一层 → 存盘 → 丢弃 → 下一层」，因此需要一套逐层加载与前向的机制。本讲的主角 `BaseModel` 就是这套机制的基类。

一个关键直觉：**「逐层」是 AMCT 在显存有限时量化超大模型的根本手段**。一个上百层的 LLM 如果整模型加载 + 整模型前向 + 反向传播，显存会爆；AMCT 的做法是把模型拆成独立的 decoder block，每次只让一个 block 真正占显存。要做到这一点，就必须解决两个问题：① 怎么只读某一层的权重？② 每层前向时所需的「位置信息」从哪来？这两个问题正是 `BaseModel` 要回答的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [amct_pytorch/common/models/llm/common/base.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py) | 本讲主角。`BaseModel` 抽象基类，定义初始化、逐层权重加载、embedding/block/head 三段前向、PTQ 单元划分与部署绑定等核心抽象，供各模型适配器继承。 |
| [amct_pytorch/common/models/llm/common/capture.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/capture.py) | 两种「截获中间张量」的工具：`Catcher`（靠抛 `ValueError` 截住第 0 层输入与位置参数）与 `register_forward_hooks`（靠 PyTorch forward hook 截任意子模块输出）。 |
| [amct_pytorch/common/models/llm/common/ptq_params.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/ptq_params.py) | PTQ 参数的导出/装载设施：`PtqParamHandler`（按模块粒度 export/load 可学习参数）、`PtqParamStore`（按 PtqUnit 粒度读写 `.pt` 文件、支撑断点续跑）。 |
| [amct_pytorch/common/models/llm/common/ptq_units.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/ptq_units.py) | `PtqUnit` 数据类与 `make_ptq_unit/iter_indexed_units` 两个构造工厂，是 `iter_ptq_units` 的底层积木。 |
| [amct_pytorch/common/models/llm/qwen/qwen3/qwen3.py](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/qwen/qwen3/qwen3.py) | 一个具体适配器示例（Qwen3 dense），展示子类如何补齐 `self.cls/self.model`、覆写 `get_layer_weight_prefix`、`build_quant_block`。 |

## 4. 核心概念与源码讲解

### 4.1 BaseModel 初始化与逐层权重加载

#### 4.1.1 概念说明

`BaseModel` 不是某个具体模型的实现，而是一套**适配器基类**：它把「所有 Transformer decoder 都长得差不多」的部分固化下来（逐层加载、逐层前向、单元划分），把「每个模型不一样」的部分留成空方法（如 `get_layer_weight_prefix`、`init_cls`），由子类补齐。这种「模板方法」模式正是 AMCT 能用同一套 PTQ 主流程支持 DeepSeek/Qwen/GLM/HyV3 等多家模型的关键（注册机制见 u3-l3、多模型适配细节见 u5-l2）。

它要解决的第一个工程问题是：**怎么在不把整个模型读进显存的前提下，按需读取某一层？** 答案是 `accelerate.init_empty_weights()`（meta 设备，只建结构不分配真实显存）搭一个「空壳骨架」，再用 `safetensors` 按前缀从分片文件里只读需要的那一层权重。

#### 4.1.2 核心流程

`BaseModel` 的初始化与逐层加载流程：

```text
BaseModel.__init__(args)
  ├─ AutoConfig.from_pretrained(model_path)        # 读 config.json，得到模型超参
  ├─ AutoTokenizer.from_pretrained(model_path)     # 读 tokenizer
  └─ 建 PtqParamHandler / PtqParamStore            # 准备 PTQ 参数存取设施

子类 __init__（如 Qwen3）补齐：
  ├─ self.cls = <某 DecoderLayer 类>               # 单层 decoder 的 Python 类
  └─ self.model = self.empty_weights_model()       # meta 空壳骨架（占显存≈0）

需要某一层时：
  block(layer_idx)
  ├─ self.cls(self.config, layer_idx)              # 用类实例化一个空层
  ├─ load_layer_weight(get_layer_weight_prefix(idx))  # 按前缀只读这一层的权重
  ├─ decoder_layer.load_state_dict(..., strict=True)
  └─ decoder_layer.eval().bfloat16()               # 进推理态、统一 bf16
```

注意「搭空壳」与「读真权重」是分开的两步：`self.model` 这个骨架几乎不占显存，真正占显存的是 `block(layer_idx)` 临时构建的那个单层——用完即被 `gc` + `empty_cache` 回收（见 4.2）。

#### 4.1.3 源码精读

**基类构造函数**只读 config/tokenizer、准备参数存取设施，不碰具体层：

[base.py:56-74](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L56-L74) —— `BaseModel.__init__`：用 `AutoConfig/AutoTokenizer` 加载配置与分词器；`position_ids/attention_mask/position_embeddings/input_ids` 四个槽位先置 `None`，留给 4.2 的 embedding 阶段填充；`PtqParamStore` 把 `self.iter_ptq_units` 作为回调传进去，使其能按单元粒度装载参数。

两个类属性给出 decoder block 上两处 norm 的默认名字（HuggingFace 命名约定），命名不同的模型在子类里覆写：

[base.py:50-54](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L50-L54) —— `attn_norm_name="input_layernorm"`、`ffn_norm_name="post_attention_layernorm"`。这两个名字正是 u4-l1 里 hook 选择「attn 目标 hook input_layernorm、mlp/moe 目标 hook post_attention_layernorm」的来源。

**空壳骨架**靠 `init_empty_weights` 上下文把整模型建在 meta 设备上：

[base.py:99-104](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L99-L104) —— `empty_weights_model()`：在 `accelerate.init_empty_weights()` 里 `AutoModelForCausalLM.from_config(...)`，得到一个结构完整但权重为空（meta tensor）的对象。这是「逐层量化」省显存的第一道闸门。

**逐层读权重**的核心是「按前缀过滤 safetensors 分片」：

[base.py:140-157](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L140-L157) —— `load_layer_weight(prefix)`：先用 `get_weight_mappings` 拿到「权重名 → 分片文件名」的全表，挑出所有以 `prefix` 开头的权重所在文件，再只打开这几个分片、只取出以 `prefix` 开头的 key（并剥掉前缀），拼成该层的 `state_dict`。末尾 `pop('self_attn.rotary_emb.inv_freq', ...)` 是因为 `inv_freq` 是缓存派生量、checkpoint 里通常没有。

**把空层填满**就是上面两步的组合：

[base.py:159-164](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L159-L164) —— `block(layer_idx)`：`self.cls(config, layer_idx)` 建空层 → `load_layer_weight` 读真权重 → `strict=True` 严格装载 → `eval().bfloat16()`。`self.cls` 与 `get_layer_weight_prefix` 基类里都是空（`init_cls`/`get_layer_weight_prefix` 见 [base.py:106-107](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L106-L107) 与 [base.py:290-291](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L290-L291)），由子类填。

以 Qwen3 子类为例，看它如何补齐这两处：

[qwen3.py:44-50](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/qwen/qwen3/qwen3.py#L44-L50) —— 子类 `__init__` 先 `super().__init__(args)` 跑基类逻辑，再设置 `self.cls = Qwen3DecoderLayer`、`self.model = self.empty_weights_model()`，并调 `parse_quant_mode()` 做模型专属校验（如 dense 模型不允许 `quant_target='moe'`）。

[qwen3.py:62-63](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/qwen/qwen3/qwen3.py#L62-L63) —— `get_layer_weight_prefix` 返回 `f"model.layers.{layer_idx}."`，这正是 HuggingFace checkpoint 里每一层权重的前缀，`load_layer_weight` 据此只读这一层。

#### 4.1.4 代码实践

> **实践目标**：理解「空壳骨架 + 按前缀读盘」如何避免整模型占显存。

1. 打开 [base.py:159-164](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L159-L164) 的 `block()` 方法。
2. 追踪一次 `model.block(5)` 调用：`self.cls(...)` 创建的对象此刻有没有真实权重？（提示：它只是 `from_config` 出来的普通实例，权重是随机初始化的，直到 `load_state_dict` 才被真值覆盖。）
3. 再读 [base.py:140-157](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L140-L157) 的 `load_layer_weight`，回答：如果想读第 5 层，`file_list` 里会有几个 safetensors 分片？为什么用 `set` 而不是 `list` 收集？
4. **需要观察的现象**：在第 2 步实例化后、第 3 步装载前，如果打印 `decoder_layer.self_attn.q_proj.weight.sum()`，会得到一个「有值的随机数」；装载后才会等于 checkpoint 里的真值。这说明 `block()` 的两步是「先有结构、后有数值」。
5. **预期结果**：你能用自己的话解释「为什么不直接 `AutoModelForCausalLM.from_pretrained` 把整模型读进来」——因为那样会一次性占满显存，无法支撑后续逐层量化的反向传播。

> 本实践为源码阅读型，无需真实模型权重即可完成推理；若要在本地验证第 4 步现象，需要一个真实的 HuggingFace 模型目录，否则标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`BaseModel` 的 `get_layer_weight_prefix` 在基类里是 `pass`，如果某个适配器忘了覆写，调用 `block(layer_idx)` 会发生什么？

**参考答案**：`get_layer_weight_prefix` 返回 `None`，`load_layer_weight(None)` 会在 `weight_name.startswith(prefix)` 处抛 `TypeError`（`startswith` 不接受 `None`）。所以覆写它是子类的隐性强约束——这正是「模板方法」留给具体模型的扩展点。

**练习 2**：`empty_weights_model()` 用的是 `from_config` 而 `float_model()` 用的是 `from_pretrained`（[base.py:93-97](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L93-L97)）。两者各用在什么场景？

**参考答案**：`float_model()` 把整模型按真实 bf16 权重读进显存，用于需要「完整浮点模型」一次性前向的场景（如 eval 阶段算 PPL 基线）；`empty_weights_model()` 只建 meta 空壳，用于逐层量化的主流程——骨架几乎不占显存，真正占显存的是每次 `block(idx)` 临时建的那一层。

---

### 4.2 逐层前向流水线：embedding → block → head

#### 4.2.1 概念说明

逐层量化的第二个工程问题是：**既然每次只把一层搬进显存，那这一层前向所需的输入和位置信息从哪来？**

Transformer 一层 decoder 的输入有两类：

1. **隐状态（hidden states）**：上一层的输出，逐层接力。
2. **位置/注意力上下文**：`position_ids`（位置序号）、`position_embeddings`（如 RoPE 的 cos/sin）、`attention_mask`（因果掩码）。这些在整模型前向里是「全局算一次、每层复用」，但在逐层前向里必须**提前捕获并重放**。

`BaseModel` 用三段式流水线解决：

- **`do_embedding_forward`**：只跑 embedding（`embed_tokens`）+ 第 0 层，靠 `Catcher` 截获「第 0 层的输入」（= embedding 输出，作为第 1 层前向的接力输入）和「位置上下文」，并保存下来。
- **`do_block_forward`**：对第 `i` 层，把上一层输出当作输入、把 embedding 阶段捕获的位置上下文当 kwargs，跑一次前向，得到本层输出。如此逐层接力。
- **`do_head_forward`**：最后一层输出经 `norm + lm_head` 得到 logits，用于 eval 算 PPL。

捕获中间激活（用于 extract_ptq_data 录制校准数据）则用另一种手段：`register_forward_hooks` 给指定子模块挂 forward hook。

#### 4.2.2 核心流程

```text
do_embedding_forward(samples, hook_name=...)
  ├─ load_embed_state_dict()                  # 装载 embed_tokens/norm/lm_head 真权重
  ├─ layers[0] = Catcher(layers[0], outs)     # 把第 0 层换成「捕获器」
  ├─ for inputs in samples:
  │     try: self.model(inputs)               # 跑整模型前向
  │     except ValueError: pass               # Catcher 在第 0 层就抛错，中止后续层
  ├─ self.position_ids / position_embeddings / attention_mask = layers[0].<...>  # 取回捕获值
  ├─ save_ptq_kwargs(...)                     # 可选：落盘供 ptq 阶段读回
  └─ layers[0] = layers[0].module             # 还原第 0 层
  → 返回 outs（每个 sample 的 embedding 输出 = 第 0 层输入）

do_block_forward(layer_idx, samples, hook_name, ...)
  ├─ block = block(layer_idx)（或 build_quant_block，见 4.1）
  ├─ register_forward_hooks(block, hook_name, ...)   # 可选：挂 hook 录激活
  ├─ kwargs = get_block_forward_kwargs()             # 把捕获的位置上下文打包
  ├─ for sample in samples:
  │       out = block(sample, **kwargs)              # 重放位置上下文，跑本层前向
  │       outs.append(out)
  ├─ save_block_hook_inputs(...)                     # 可选：把 hook 录到的激活落盘
  └─ gc + empty_cache                                # 回收本层显存

do_head_forward(inps)
  └─ norm → lm_head → logits（eval 算 PPL 用）
```

`Catcher` 的妙处在于「用异常控制流做切片」：它替换第 0 层后，整模型前向一旦进入第 0 层就立刻 `raise ValueError`，于是第 1…N 层根本不会执行——既省算力，又顺带把第 0 层该收的输入和位置参数收齐。

#### 4.2.3 源码精读

先看 `Catcher` 的实现，它是整个流水线的「捕获心脏」：

[capture.py:23-50](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/capture.py#L23-L50) —— `Catcher` 是一个 `nn.Module` 包装器。它的 `forward(inp, **kwargs)` 做三件事：① 把 `inp` 搬到 CPU 追加进 `self.dataset`（`inp` 就是 embedding 输出，即第 0 层输入）；② 首次出现时把 `attention_mask/position_ids/position_embeddings` 从 `kwargs` 里捞出来存档；③ `raise ValueError` 主动中断。`__getattr__`（[capture.py:35-39](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/capture.py#L35-L39)）把属性访问透传给被包装的 `self.module`，使得 `layers[0].position_ids` 这样的写法能取到捕获值。

再看 `do_embedding_forward` 如何使用它：

[base.py:189-215](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L189-L215) —— `do_embedding_forward`：先 `load_embed_state_dict()` 装 embed 权重；把 `layers[0]` 换成 `Catcher`；在 `torch.no_grad()` 下逐 sample 跑 `self.model(inputs)`，每次都会在第 0 层抛 `ValueError` 被 `except` 吞掉；跑完后从 `layers[0].position_ids/position_embeddings/attention_mask` 取回三件套存到 `self`；若传了 `hook_name`（block 粒度 workflow 会传）则调 `save_ptq_kwargs` 落盘；最后 `layers[0] = layers[0].module` 把第 0 层还原成普通层。

落盘契约（与 u4-l1 的 extract 阶段对接）：

[base.py:207-213](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L207-L213) —— 调用 `save_ptq_kwargs(position_ids, position_embeddings, attention_mask, data_dir)`，三者分别存为 `position_ids.pkl / position_embeddings.pkl / attention_mask.pkl`（见 [ptq_io.py:23-33](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/ptq_io.py#L23-L33)）。ptq 阶段会经 `load_ptq_inps` 读回这些 `.pkl`，复现同一份位置上下文。

接着是 `get_block_forward_kwargs`——把捕获值打包成层前向的 kwargs：

[base.py:166-177](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L166-L177) —— `get_block_forward_kwargs()`：按「非空才加入」的原则，把 `position_ids`、`position_embeddings`（注意它是个二元组 `(cos, sin)`，分别搬设备）、`attention_mask` 组装成字典。注意 `position_embeddings` 在 [base.py:171-174](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L171-L174) 取的是 `self.position_embeddings[0]` 与 `[1]`——因为 `Catcher` 存的是 `(cos, sin)` 二元组。

然后看 `do_block_forward` 如何「重放」这些 kwargs：

[base.py:234-273](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L234-L273) —— `do_block_forward`：先用 `_build_block_for_forward` 取层（普通层或量化层）；若 `use_quant_block` 且无 hook，则给所有 `QuantLinear` 打开 `eval_mode` 并清缓存（eval 缓存机制见 u7-l1）；若传了 `hook_name` 则 `register_forward_hooks` 挂钩；然后 `block_kwargs = self.get_block_forward_kwargs()` 取出位置上下文，逐 sample 调 `block(sample, **call_kwargs)`——**这就是对 embedding 阶段捕获值的重放**。

[capture.py:66-73](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/capture.py#L66-L73) —— `register_forward_hooks(block, target_name, hooks, act_stat)`：遍历 `block.named_modules()`，只要模块名里含 `target_name`（如 `"input_layernorm"`），就给它挂一个 forward hook，钩子函数 `_stat_input_hook`（[capture.py:62-63](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/capture.py#L62-L63)）把该模块的输出张量追加进 `act_stat`。这正是 u4-l1 里「hook 紧挨 norm」录制校准激活的实现。

[base.py:275-288](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L275-L288) —— 前向结束后：移除所有 hook、调 `save_block_hook_inputs` 把录到的激活落盘、`remove_hook_from_module` 清理、`block = None` + `gc.collect()` + `torch.npu.empty_cache()` 显式回收显存。**「用完即回收」是逐层量化能跑超大模型的第二道闸门**（第一道是 4.1 的空壳骨架）。

最后是 `do_head_forward`，用于 eval 阶段把最后一层隐状态变成 logits：

[base.py:217-229](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L217-L229) —— `do_head_forward`：把 `model.norm` 与 `lm_head` 搬到设备，逐 sample 做 `norm → lm_head`，并切掉最后一个时间步 `[:, :-1, :]`（对齐预测位），收集 logits 返回。

#### 4.2.4 代码实践（本讲指定实践）

> **实践目标**：把「embedding 阶段捕获位置上下文 ↔ block 阶段重放」这条链路彻底读通，回答指定问题。

阅读以下两段代码，回答后面的问题：

1. [base.py:234-273](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L234-L273) `do_block_forward`，重点关注 `block_kwargs = self.get_block_forward_kwargs()` 与 `block(sample, **call_kwargs)`。
2. [base.py:189-215](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L189-L215) `do_embedding_forward`，重点关注 `layers[0] = Catcher(...)` 与取回 `position_ids/position_embeddings/attention_mask`。
3. [capture.py:41-50](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/capture.py#L41-L50) `Catcher.forward`。

**需要回答与观察的问题**：

- **Q1（捕获）**：`Catcher.forward` 是在什么时机被调用的？为什么它在末尾 `raise ValueError`？如果不抛这个异常，会发生什么？
  - *提示*：`self.model(inputs)` 是整模型前向，第 0 层已被替换成 `Catcher`；进入第 0 层即触发 `Catcher.forward`。抛 `ValueError` 是为了让前向在第 0 层之后立刻中止（第 1…N 层不跑），既省算力又避免在没有真权重的空壳层上算出无意义结果。外层 `except ValueError: pass` 把它吞掉。
- **Q2（三件套来源）**：`position_ids/attention_mask/position_embeddings` 分别来自哪里？
  - *提示*：它们是 HuggingFace 模型在调用第 0 层 `DecoderLayer.forward` 时传入的 kwargs（由上层 `model.forward` 根据输入算好）。`Catcher` 用 `if ... is None` 做首次捕获，只存第一个 sample 的值（因为对所有 sample 这些上下文结构相同）。
- **Q3（落盘与重放）**：这三件套怎么从 embedding 阶段传到 block 阶段？有「内存直传」和「落盘中转」两条路，分别对应什么场景？
  - *提示*：内存直传——同一进程内（如 eval 阶段）`self.position_ids = layers[0].position_ids` 后，`do_block_forward` 经 `get_block_forward_kwargs()` 直接取用。落盘中转——block 粒度的 extract/ptq 跨进程时，`save_ptq_kwargs` 写成 `*.pkl`，ptq 阶段 `load_ptq_inps` 读回（见 [ptq_io.py:42-48](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/datasets/ptq_io.py#L42-L48)）。
- **Q4（重放点）**：`do_block_forward` 里真正「重放」这些上下文的代码是哪一行？为什么每一层都要重放同一份 kwargs？
  - *提示*：是 `out = block(sample, **call_kwargs)`。每一层 decoder 都需要同一套位置编码与掩码（在整模型前向里它们本来就是每层共用），逐层单独前向时必须手动喂回去。

**预期结果**：你能画出这样一张数据流图：

```text
samples ──► embed_tokens ──► (第0层输入=embedding输出)
                                  │ Catcher 在此捕获 inp + (position_ids,
                                  │ attention_mask, position_embeddings)
                                  ▼
                         self.position_ids / ...  ──► save_ptq_kwargs ──► *.pkl
                                  │                                            │
                                  │ 内存直传                                   │ ptq 阶段 load_ptq_inps 读回
                                  ▼                                            ▼
                         get_block_forward_kwargs() ◄──────────────────────────┘
                                  │
                                  ▼
             do_block_forward: block(sample, **kwargs)  ← 每层都重放
```

> 本实践为源码阅读型；如需在本地观察 `Catcher` 实际捕获的张量形状，需一个真实 HuggingFace 模型与 NPU/CPU 环境，否则标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`Catcher.__getattr__` 把属性访问透传给 `self.module`（[capture.py:35-39](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/capture.py#L35-L39)）。`do_embedding_forward` 末尾 `layers[0] = layers[0].module`（[base.py:214](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L214)）为什么必须做这一步「还原」？

**参考答案**：因为 `Catcher` 是为了「在 embedding 阶段临时截获」才替换上去的，它本身不是真正的 decoder layer——它的 `forward` 会抛异常，不能用于正常的逐层前向。取完捕获值后必须把第 0 层换回原始模块，否则后续若再用到 `layers[0]`（如正常前向或权重访问）就会出错。

**练习 2**：`register_forward_hooks` 用「`target_name in name`」做子串匹配（[capture.py:68](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/capture.py#L68)）。如果 `hook_name="norm"`，可能会误挂到哪些模块？

**参考答案**：所有名字里含 `norm` 的模块都会被挂上，包括 `input_layernorm`、`post_attention_layernorm`、`model.norm`，甚至某层内可能叫 `...norm...` 的其它子模块。所以 AMCT 实际传入的 `hook_name` 是较完整的目标名（如 `input_layernorm` / `post_attention_layernorm`，见 u4-l1），以缩小匹配范围、避免误挂。

---

### 4.3 PTQ 单元划分与部署绑定

#### 4.3.1 概念说明

一层 decoder 并不总是「一个」量化单元。`BaseModel` 用 `iter_ptq_units` 回答「一层要切成几个 PtqUnit」：

- **attn-linear / attn-cache**：整层的注意力当作 1 个单元（`self_attn` 或 `linear_attn`）。
- **mlp**：整层的 MLP 当作 1 个单元。
- **moe**：MoE 的**每个 expert 各 1 个单元**，所以一层 N 个 expert 就切出 N 个单元。

这套划分是 `extract_ptq_data`（每单元录输入）、`ptq`（每单元独立训练）、断点续跑（每单元一个 `.pt`）共同的工作粒度（见 u4-l2）。

与 `iter_ptq_units` 配对的是 `iter_deploy_bindings`：部署阶段不需要「单元」概念，而是要把每个被量化的 `QuantLinear` 的权重名与模块一一绑定，供 deploy 烘焙（见 u4-l4）。

`PtqParamStore/PtqParamHandler` 则负责把这些单元的可学习参数存盘/读回，是断点续跑的底层支撑。

#### 4.3.2 核心流程

`iter_ptq_units` 的分发逻辑：

```text
iter_ptq_units(layer_idx, block):
  if quant_target ∈ {attn-linear, attn-cache}:
      attn_name = "self_attn"（或 linear_attn，看 layer_type）
      yield 1 个 PtqUnit(kind="attn", name=attn_name, module=block.<attn_name>)
      return
  mlp = block.mlp
  if "moe" in quant_target:
      experts = mlp.experts（经 iter_ptq_expert_modules 或 expert_modules 取列表）
      yield from iter_indexed_units(kind="moe", name_prefix="expert",
                                    items=experts, metadata_fn=记 expert_idx)
      → 每个 expert 产 1 个 PtqUnit(name="expert_0/1/...")
      return
  if "mlp" in quant_target:
      yield 1 个 PtqUnit(kind="mlp", name="mlp", module=block.mlp)
```

`iter_deploy_bindings` 则简单粗暴地枚举模块树里所有 `QuantLinear`：

```text
iter_deploy_bindings(layer_idx, block):
  weight_prefix = get_layer_weight_prefix(layer_idx)   # 如 "model.layers.5."
  for name, module in block.named_modules():
      if isinstance(module, QuantLinear):
          yield f"{weight_prefix}{name}.weight", module
```

#### 4.3.3 源码精读

先看单元的「数据载体」：

[ptq_units.py:26-36](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/ptq_units.py#L26-L36) —— `PtqUnit` 数据类：`kind`（类别，用于读输入文件名）、`name`（单元名，用于存 `.pt`）、`layer_idx`（层号）、`module`（被量化的子模块对象）、`metadata`（如 `expert_idx`）。`save_name` 属性把 `name` 里的 `.` 换成 `_`，用于拼 `.pt` 文件名。

注意两类文件名的区别（容易混）：

- **输入文件**（extract 录的校准激活）：`block_{layer_idx}_{kind}_in.pkl`，用 `kind`（attn/mlp/moe）。
- **参数文件**（ptq 存的可学习参数）：`layer_{layer_idx}_{save_name}.pt`，用 `save_name`（self_attn/mlp/expert_0…）。

[base.py:80-82](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L80-L82) —— `load_unit_inputs` 用 `unit.kind` 调 `load_ptq_inps`，所以同层 MoE 的所有 expert 都读同一个 `block_{idx}_moe_in.pkl`（共享输入，见 u4-l2）。

再看核心的 `iter_ptq_units`：

[base.py:293-322](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L293-L322) —— 三分支：① attn 目标，先按 `block.layer_type` 决定注意力模块叫 `self_attn`（标准）还是 `linear_attn`（线性注意力变体），再 `make_ptq_unit("attn", attn_name, ...)`；② moe 目标，用 `iter_indexed_units` 给每个 expert 生成一个 `PtqUnit(name="expert_{idx}", metadata={"expert_idx": idx})`，其中 experts 列表优先用模型自带的 `iter_ptq_expert_modules()`（迭代器）或 `expert_modules`（列表）；③ mlp 目标，单单元 `make_ptq_unit("mlp", "mlp", ...)`。

[ptq_units.py:51-70](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/ptq_units.py#L51-L70) —— `iter_indexed_units`：对 `items` 枚举，每个产出一个 `name=f"{name_prefix}_{idx}"` 的单元，可经 `module_fn` 把「列表元素」转换成「真正的模块对象」、经 `metadata_fn` 附加元数据（如 expert 编号）。

再看部署绑定：

[base.py:324-329](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L324-L329) —— `iter_deploy_bindings`：遍历 `block.named_modules()`，**只挑 `QuantLinear`**，产出 `(完整权重名, 模块)` 二元组。完整权重名 = 层前缀 + 模块内相对路径 + `.weight`，这正是 checkpoint 里的标准权重键，deploy 阶段据此把量化 payload 写回对应位置（见 u4-l4 的烘焙逻辑）。`PlainLinear` 与原始 `nn.Linear` 不是 `QuantLinear`，自然被忽略——这就是「只烘焙 QuantLinear」的代码根源。

最后看支撑断点续跑的参数存取：

[ptq_params.py:105-124](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/ptq_params.py#L105-L124) —— `PtqParamStore.load_saved_unit`：按 `layer_{idx}_{save_name}.pt` 拼路径，文件不存在则按 `strict` 决定报错或返回 `False`（返回 `False` 即「该单元没存过、需要训练」，这就是断点续跑的判定点）；存在则 `torch.load` 后交 `PtqParamHandler.load_unit` 装载。

[ptq_params.py:126-142](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/ptq_params.py#L126-L142) —— `PtqParamStore.load_layer`：对一层调 `iter_ptq_units_fn`（即 `BaseModel.iter_ptq_units`）枚举所有单元，逐个 `load_saved_unit`，返回 `{loaded, missing}` 两个名单。

[ptq_params.py:83-102](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/ptq_params.py#L83-L102) —— `PtqParamHandler.export_unit/load_unit`：优先用模块自带的 `export_ptq_params/load_ptq_params`（算法自定义格式），否则退回到「按子模块枚举 / 按 requires_grad 枚举」的通用导出，或按字典结构装载。这给了各算法灵活的自定义序列化空间。

#### 4.3.4 代码实践

> **实践目标**：追踪一个 moe 目标的单元划分，理解「一层多单元」与断点续跑文件名。

1. 阅读 [base.py:293-322](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/base.py#L293-L322) 的 `iter_ptq_units`，假设某 MoE 模型一层有 64 个 expert、`quant_target=["moe"]`。
2. 回答：`iter_ptq_units(5, block)` 会 yield 出几个 `PtqUnit`？每个的 `kind/name/save_name` 分别是什么？
3. 再读 [ptq_params.py:110-124](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/ptq_params.py#L110-L124) 的 `load_saved_unit`，写出第 5 层第 10 个 expert 的 `.pt` 参数文件名；并回答：这 64 个单元各自读的**输入**文件是否相同？为什么？
4. **需要观察的现象**：在 `param_dir` 目录下应能看到 `layer_5_expert_0.pt … layer_5_expert_63.pt` 共 64 个参数文件（ptq 阶段产物）；而 `data_dir` 下第 5 层的输入只有一个 `block_5_moe_in.pkl`（extract 阶段产物）。
5. **预期结果**：你能解释「参数文件按单元命名（每 expert 一个），输入文件按类别命名（每层每类一个）」的设计——因为参数是每单元独立训练的成果，必须分开存；而输入是同层各 expert 共享的同一份激活，存一份即可。

> 本实践为源码阅读型；若要本地观察实际文件，需跑过一次 moe 目标的 extract + ptq，否则标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`iter_ptq_units` 的 attn 分支里，为什么要用 `block.layer_type` 来决定 `attn_name` 是 `self_attn` 还是 `linear_attn`？

**参考答案**：因为不同模型族的注意力实现不同：标准 Transformer 用 `self_attn`（如 Qwen3、DeepSeek），而某些采用「线性注意力」变体的模型（如部分 GLM/HyV3）用 `linear_attn`。`Catcher` 在初始化时也读了 `layer_type`（[capture.py:29](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/capture.py#L29)）。通过 `getattr(block, "layer_type", None)` 动态判定，`BaseModel` 能用同一份代码兼容两种命名，体现「基类吸收差异」的设计。

**练习 2**：`iter_deploy_bindings` 只挑 `QuantLinear`，那 `PlainLinear`（见 u5-l3）和原始 `nn.Linear` 会被怎样处理？

**参考答案**：它们都不是 `QuantLinear` 的实例，`isinstance(module, QuantLinear)` 为假，不会被 yield。这意味着 deploy 烘焙名单里没有它们——它们要么是「故意不量化的线性层」（`PlainLinear`，签名对齐但保留浮点），要么是「未被量化逻辑替换的原始层」。这与 u4-l4 里 `get_quant_ignore_linear_names` 区分 QuantLinear / PlainLinear / 原始 Linear 的三分类一致。

**练习 3**：`PtqParamHandler.load_unit` 优先调 `unit.module.load_ptq_params`（[ptq_params.py:91-93](https://github.com/gitcode.com/cann/amct/blob/ba53a0fdd6f3ed91a2d4875141d02aff746191a3/amct_pytorch/common/models/llm/common/ptq_params.py#L91-L93)），否则退回通用装载。这种「两段式」设计有什么好处？

**参考答案**：不同算法的可学习参数结构差异很大（如 FlatQuant 存正交变换矩阵、LAC 存截断系数），强行用统一格式会丢失算法语义。让算法自带 `export_ptq_params/load_ptq_params` 自定义序列化，基类只提供「按 requires_grad 枚举」「按子模块枚举」两种通用兜底，既给算法自由度，又对简单算法免去样板代码。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「纸上的逐层量化推演」。

**任务**：假设你拿到一个 32 层的 dense Qwen3 模型，要对 `mlp` 做 W8A8 量化（granularity=block），请按 `BaseModel` 的视角回答整条链路的每一步：

1. **初始化（4.1）**：`Qwen3.__init__` 会设置哪两个关键属性（`self.cls` / `self.model`）？`self.model` 此刻占多少显存，为什么？
2. **embedding（4.2）**：`do_embedding_forward` 跑完后，`self.position_ids` 等三件套从哪里来？它们会被存成哪几个 `.pkl` 文件？
3. **逐层前向（4.2）**：对第 10 层，`do_block_forward(10, samples, ...)` 调用 `block(sample, **call_kwargs)` 时，`call_kwargs` 里包含哪些键？这些值是「现算的」还是「重放的」？
4. **单元划分（4.3）**：`iter_ptq_units(10, block)` 会 yield 几个单元？其 `kind/name` 各是什么？对应的输入文件和参数文件分别叫什么名字？
5. **回收**：一层处理完后，`do_block_forward` 末尾做了哪三件事来释放显存？为什么这一步对量化 32 层乃至上百层模型至关重要？

**参考要点**（先自己答，再对照）：

1. `self.cls = Qwen3DecoderLayer`、`self.model = empty_weights_model()`；`self.model` 是 meta 空壳，显存≈0。
2. 由 `Catcher` 在第 0 层捕获（HuggingFace 传入的 kwargs）；存成 `position_ids.pkl / position_embeddings.pkl / attention_mask.pkl`。
3. `call_kwargs` 含 `position_ids / position_embeddings / attention_mask`（非空才有）；是「重放」embedding 阶段捕获的值，不是现算。
4. dense 模型 mlp 目标：1 个单元，`kind="mlp" / name="mlp"`；输入文件 `block_10_mlp_in.pkl`，参数文件 `layer_10_mlp.pt`。
5. `block = None` + `gc.collect()` + `torch.npu.empty_cache()`；逐层量化显存峰值≈单层而非整模型，是跑超大模型的关键。

> 本综合实践为源码阅读型推理，无需运行；若要在本地真跑一遍，需 NPU 环境、真实模型与校准数据，相关命令见 u1-l4。

## 6. 本讲小结

- `BaseModel` 是所有 LLM 适配器的**模板方法基类**：固化「逐层加载 + 逐层前向 + 单元划分」，把 `cls / get_layer_weight_prefix / build_quant_block` 等模型相关部分留给子类。
- 省显存的两道闸门：① `empty_weights_model()` 建 meta 空壳骨架；② `do_block_forward` 末尾 `gc + empty_cache` 用完即回收。两者使显存峰值≈单层而非整模型。
- 逐层前向三段式：`do_embedding_forward`（捕获层 0 输入与位置上下文）→ `do_block_forward`（逐层重放上下文接力前向）→ `do_head_forward`（norm+lm_head 出 logits）。
- **`Catcher` 的本质是用异常做切片**：替换第 0 层后整模型前向在第 0 层即 `raise ValueError` 中止，顺带捕获 `position_ids/attention_mask/position_embeddings`，供每一层前向重放或落盘跨进程复用。
- `iter_ptq_units` 按 `quant_target` 切单元：attn/mlp 各 1 个，moe 每 expert 1 个；`iter_deploy_bindings` 则按 `QuantLinear` 枚举部署绑定——两套枚举分别服务「训练/校准」与「烘焙导出」。
- `PtqParamStore/PtqParamHandler` 以 PtqUnit 粒度存取 `.pt`，文件不存在即「未训练过」，是断点续跑的判定点。

## 7. 下一步学习建议

- **u5-l2 模型注册与多模型适配**：本讲只看了 Qwen3 一个子类，下一讲会遍历 `register_llm_models` 注册的 DeepSeek/LongCat/GLM/HyV3 等家族，看它们各自覆写了哪些方法、如何处理 MoE/dense 变体与多模态外壳前缀（`_embed_base_prefix`）。
- **u5-l3 量化算子挂载 quant_apply**：本讲的 `build_quant_block` 调用了 `apply_quant_to_attn/apply_quant_to_moe_mlp`，下一讲打开这两个函数，看原始 `Linear/MLP/Attention` 是如何被包装成 `QuantLinear/QuantGatedMLP` 的。
- **顺带可重温**：u4-l1（extract 如何调 `do_embedding_forward/do_block_forward` + `save_block_hook_inputs`）、u4-l2（ptq 如何用 `iter_ptq_units` 驱动逐单元训练）、u4-l4（deploy 如何用 `iter_deploy_bindings` 烘焙 QuantLinear），把本讲的基类方法放回它们各自的 Workflow 主线中理解。
