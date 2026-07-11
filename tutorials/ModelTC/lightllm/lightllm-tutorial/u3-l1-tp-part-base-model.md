# TpPartBaseModel 推理框架

## 1. 本讲目标

本讲打开上一讲（u2-l4）里 `ModeBackend.init_model` 调用 `get_model(...)` 之后那个「黑盒」——模型对象自身是如何被构造出来的。

学完本讲你应当能够：

- 说清 `TpPartBaseModel` 这个「所有模型的基类」到底做了什么、不做什么，以及为什么 46 个模型族能共用同一套初始化与推理框架。
- 把 `__init__` 里近 20 个 `_init_*` 步骤按顺序背出来，并解释每一步的职责与它们之间的依赖关系。
- 理解 `InferStateInfo` 这个「推理状态」对象在一次前向（forward）中扮演的上下文角色，以及它与 `ModelInput` 的区别。
- 能够打开任意一个具体模型目录（如 llama），看懂它「插了哪些插槽」就推断出它的推理行为。

本讲只覆盖三个最小模块：**模型基类**、**组件初始化**、**推理状态**。prefill/decode 的具体前向流程、层推理模板、权重切分、注意力后端等内容分别属于后续讲义（u3-l2 起）。

## 2. 前置知识

在阅读本讲前，建议先建立以下几个直觉（都用大白话解释）：

- **基类与模板方法（Template Method）**：面向对象里一种常见设计——父类把「做事的步骤顺序」写死，但每一步具体「怎么做」交给子类去填。本讲的 `TpPartBaseModel` 就是这种思路：它把初始化流水线写死，子类只需要声明「用哪个权重类、哪个推理类」。
- **张量并行（Tensor Parallelism, TP）**：把一个大模型横向切成 `tp` 份，每张 GPU 只持有「一片」。例如注意力有 32 个头、`tp=4`，则每张卡只算 8 个头。名字里的 `TpPart` 就是「张量并行的一片」之意。每张卡上跑的就是一个 `TpPartBaseModel` 实例。
- **prefill 与 decode 两阶段**：LLM 推理分两段——prefill（把整条提示词一次性算出 KV Cache）和 decode（每次生成一个新 token）。本讲的基类对二者提供了统一的 `forward` 入口，具体差异在 u3-l2 详讲。
- **KV Cache**：注意力计算中 Key/Value 的缓存，是显存大户。它由「内存管理器（MemoryManager）」分配回收，本讲会看到基类如何把它组装进来（细节在第四单元）。
- **`config.json`**：HuggingFace 格式模型目录下的配置文件，记录了 `num_attention_heads`、`hidden_size`、`num_hidden_layers` 等结构超参。模型的初始化几乎全靠它驱动。
- **组件组合（Composition）**：一个完整模型 = 权重对象 + 推理层对象 + 内存管理器 + 注意力后端 + 推理状态。基类的工作就是「把这些零件组装起来」。

承接 u2-l4：上一讲我们看到 `ModeBackend.init_model` 构造了一个 `model_kvargs` 字典，调用 `get_model(model_cfg, model_kvargs)` 拿到一个模型实例，随后访问 `self.model.mem_manager`、`self.model.req_manager`、`self.model.vocab_size`。本讲就回答：这次构造在模型内部到底发生了什么，这些属性又是从哪儿来的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [basemodel.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py) | 本讲主角。定义 `TpPartBaseModel`：声明组件插槽、写死初始化流水线、提供 `forward`/`_create_inferstate` 等运行时方法。 |
| [infer_struct.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/infer_struct.py) | 定义 `InferStateInfo`，即「推理状态」上下文对象，贯穿一次前向。 |
| [batch_objs.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/batch_objs.py) | 定义 `ModelInput`/`ModelOutput` 两个数据类，是前向的输入输出容器。 |
| [llama/model.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/model.py) | 一个具体子类范例，展示「插槽如何被填上」。 |
| [base_backend.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py) | u2-l4 的主角，本讲只引用其 `init_model` 中构造模型的那几行，承接上下游。 |

## 4. 核心概念与源码讲解

### 4.1 模型基类：TpPartBaseModel 的「插槽 + 组装」设计

#### 4.1.1 概念说明

`TpPartBaseModel` 是 lightllm 里**所有模型**的基类。它的核心设计思想可以用一句话概括：

> 基类不实现任何「模型专属」逻辑，只负责「组装零件」；模型之间的差异，全部通过子类填入不同的「零件类」来表达。

这本质是一种**依赖注入**模式——基类声明了若干「插槽（slot）」（也就是类属性），它们默认是 `None`；子类把这些插槽填成具体的类，基类的 `__init__` 与 `forward` 就会自动用这些类去实例化零件、驱动推理。

为什么这么设计？因为 lightllm 要支持 46 个以上的模型族（llama、qwen、deepseek、mixtral……）。如果每个模型都从头写一遍「读 config、建权重、建内存管理器、建注意力后端、跑前向」，会产生海量重复代码。把公共流程抽到基类、把差异点收拢成几个插槽，新增一个模型就只需要「填插槽 + 改少数钩子」，这正是 lightllm「易扩展」特色在代码层面的根基（u5-l3 会专门讲新增模型流程）。

名字里的 **`TpPart`** = Tensor-Parallel Part（张量并行的一片），提醒我们：每个 GPU 进程里跑的就是「一片」模型，它只持有全模型的一部分注意力头和一部分权重。这一点直接决定了后面 `_init_mem_manager` 里 `head_num = num_attention_heads // tp_world_size_` 的切分逻辑。

#### 4.1.2 核心流程

一个具体模型（如 llama）「被使用」的链路如下：

```
ModeBackend.init_model                         [base_backend.py]
        │  构造 model_kvargs 字典（weight_dir、max_total_token_num、run_mode…）
        ▼
get_model(model_cfg, model_kvargs)             [models 包的注册机制，见 u5-l1]
        │  依据 config.json 匹配到具体子类，如 LlamaTpPartModel
        ▼
LlamaTpPartModel(kvargs)  →  触发 TpPartBaseModel.__init__(kvargs)
        │  读取子类填好的「插槽」，逐个实例化零件
        ▼
返回一个组装完毕的模型实例（持有 .mem_manager / .req_manager / .vocab_size 等）
```

关键在于：**子类几乎不写 `__init__` 的逻辑，只声明插槽**。插槽一共分三组：

| 插槽组 | 类属性 | 含义 |
| --- | --- | --- |
| weight class | `pre_and_post_weight_class`、`transformer_weight_class` | embedding/lm_head 权重 与 每层 transformer 权重 |
| infer class | `pre_layer_infer_class`、`post_layer_infer_class`、`transformer_layer_infer_class` | embedding 推理、logits 后处理、每层 transformer 推理 |
| infer state class | `infer_state_class`（默认 `InferStateInfo`） | 推理状态上下文类型 |

#### 4.1.3 源码精读

先看基类如何声明插槽（注意它们都是 `None`，等待子类填入）：

[basemodel.py:47-58](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L47-L58) 声明了上面表格里的全部六类插槽。

再看一个具体子类是怎么填插槽的。以 llama 为例：

[llama/model.py:22-37](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/model.py#L22-L37) 把六个插槽分别填成 llama 自己的权重类、推理类与状态类。注意 llama 的 `__init__` 体里只有一句 `super().__init__(kvargs)`——它**完全没有改写初始化流程**，只是「换了零件」。

最后承接 u2-l4，看「谁创建了模型、传了什么参数」：

[base_backend.py:132-153](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py#L132-L153) 这段代码构造了 `model_kvargs`（即本讲里 `__init__` 收到的 `kvargs`），并调用 `get_model(model_cfg, model_kvargs)` 拿到模型实例。注意它后面会用到 `self.model.mem_manager`、`self.model.req_manager`、`self.model.vocab_size`——这三个属性正是基类 `__init__` 在下一步流水线里创建出来的（见 4.2）。

#### 4.1.4 代码实践

**实践目标**：亲手验证「插槽 → 具体类」的映射，理解一个模型就是「一组零件的声明」。

**操作步骤**：

1. 打开 [llama/model.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/model.py) 顶部的 import 和第 22–37 行的插槽声明。
2. 对照下表，把每个插槽填的类，对应到它在 llama 目录下的源文件：

   | 插槽 | 填入的类 | 所在文件（llama 目录下） |
   | ---- | ------- | ---------------------- |
   | `pre_and_post_weight_class` | `LlamaPreAndPostLayerWeight` | `layer_weights/pre_and_post_layer_weight.py` |
   | `transformer_weight_class` | `LlamaTransformerLayerWeight` | `layer_weights/transformer_layer_weight.py` |
   | `pre_layer_infer_class` | `LlamaPreLayerInfer` | `layer_infer/pre_layer_infer.py` |
   | `post_layer_infer_class` | `LlamaPostLayerInfer` | `layer_infer/post_layer_infer.py` |
   | `transformer_layer_infer_class` | `LlamaTransformerLayerInfer` | `layer_infer/transformer_layer_infer.py` |
   | `infer_state_class` | `LlamaInferStateInfo` | `infer_struct.py` |

3. 再打开另一个模型目录（如 `lightllm/models/qwen2/model.py`），找出它填的六个插槽分别是什么类，确认「换模型 = 换一组零件类」这一规律。

**需要观察的现象**：不同模型的 `model.py` 顶部结构高度相似——都是「import 六个类 + 填六个插槽 + 一个几乎为空的 `__init__`」。

**预期结果**：你会得出结论——模型间的差异被收拢到了六个插槽和少数钩子方法里，公共流程完全复用基类。

> 说明：本实践为源码阅读型实践，不需要运行；若想确认 import 路径存在，可用 `Read`/`Glob` 工具核对文件。

#### 4.1.5 小练习与答案

**练习 1**：如果一个子类忘了填 `transformer_layer_infer_class`（仍为 `None`），会在什么时候、以什么方式报错？

**参考答案**：会在 `__init__` 流水线中的 `_init_infer_layer()`（见 4.2）执行到 `self.transformer_layer_infer_class(i, network_config=self.config)` 时抛出 `TypeError: 'NoneType' object is not callable`。因为插槽默认是 `None`，基类直接拿它当类来实例化。

**练习 2**：为什么 `infer_state_class` 有默认值 `InferStateInfo`，而权重类/推理类插槽都是 `None`？

**参考答案**：因为推理状态的结构对绝大多数模型是通用的（都是输入 id、batch 形状、KV 句柄等），只有少数模型（如带 MRoPE 的多模态模型）需要扩展字段，所以基类给了一个可用的默认值；而权重类、推理类与模型结构强绑定，没有「通用默认」，必须由子类显式提供。

---

### 4.2 组件初始化：__init__ 的固定流水线

#### 4.2.1 概念说明

`TpPartBaseModel.__init__` 是一个**写死的、有严格顺序的初始化流水线**。它先把传入的 `kvargs` 解析成实例属性，然后依次调用近 20 个 `_init_*` / `_check_*` / `_autotune_*` 方法，把上一节提到的零件一个个造出来并互相挂接。

之所以强调「顺序」，是因为步骤之间存在依赖：

- 必须先 `_init_config()` 读到 `self.config`，后面所有步骤才能取到 `num_attention_heads`、`n_layer` 等超参。
- 必须先 `_init_weights()` 建好权重对象，才能 `_load_hf_weights()` 把磁盘上的权重灌进去。
- 必须先 `_init_mem_manager()` 建好 KV 内存管理器，才能在 `_check_mem_size()` 里确定真实的 `max_total_token_num`。
- 必须先 `_init_att_backend()`，`forward` 时才能创建注意力状态。

这种「把固定流程写在基类、个别步骤允许子类覆写」的做法，正是模板方法模式。

#### 4.2.2 核心流程

完整的初始化顺序（按 `__init__` 中的调用次序）如下：

```
1.  解析 kvargs → 实例属性           (run_mode, weight_dir, max_total_token_num, mem_fraction, tp_world_size_, ...)
2.  _init_config()                 读 config.json，统一字段命名（repair_config）
3.  _verify_must()                 断言 num_attention_heads 能被 tp 整除
4.  _verify_params()               断言 load_way、num_key_value_heads 能被 tp 整除
5.  _init_quant()                  建量化配置 Quantcfg
6.  _init_weights()                建 pre_post_weight + N 个 transformer 层权重对象（此时还没装数据）
7.  _init_req_manager()            建请求管理器 ReqManager
8.  _init_mem_manager()            建 KV 内存管理器 MemoryManager
9.  req_manager.mem_manager = ...  把内存管理器挂到请求管理器上（顺序刻意靠后，见源码注释）
10. _check_mem_size()              用 mem_manager.size 回填 max_total_token_num，并做上下限校验
11. _init_infer_layer()            建 pre/post/transformer 三类推理层对象
12. _init_some_value()             计算 head_dim_、tp_k_head_num_、layers_num、vocab_size
13. _init_custom()                 子类钩子（如 llama 在这里初始化 RoPE 旋转位置编码）
14. _load_hf_weights()             真正从磁盘加载权重并 verify
15. _init_att_backend()/_init_att_backend1()   建 prefill/decode 注意力后端
16. _autotune_warmup()             Triton kernel 自动调参预热
17. _full_att_decode_autotune()    FA3 decode num_splits 调参（默认关）
18. _init_padded_req()             预热 padding 用到的占位请求
19. _init_cudagraph()              捕获 decode CUDA Graph（若启用）
20. _init_prefill_cuda_graph()     捕获 prefill CUDA Graph（若启用）
21. _check_max_len_infer()         用最大长度跑一次 prefill，试探是否会 OOM
22. empty_cache + set_model_init_status(True)   清缓存、标记模型初始化完成
```

其中最值得记的「关键依赖」有三处：

- **第 2 步**决定一切超参来源；
- **第 8→9→10 步**：先建内存管理器，再挂到请求管理器，再用它的真实容量回填 `max_total_token_num`；
- **第 11→13→14 步**：先建推理层对象，再跑子类的 `_init_custom`（很多自定义张量在这里分配），最后才加载真实权重。

#### 4.2.3 源码精读

先看 `__init__` 的整体骨架——上半段解析 kvargs，下半段是流水线：

[basemodel.py:60-103](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L60-L103) 解析 `kvargs`。注意几个关键赋值：`self.tp_world_size_ = get_dp_world_size()`（当前 rank 的张量并行宽度）、`self.data_type = get_llm_data_type()`（推理精度）、`self.graph_max_batch_size` 会因 microbatch overlap 与 mtp_step 被修订。

[basemodel.py:104-139](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L104-L139) 这就是上面流程图对应的完整流水线。逐行对应 4.2.2 的 22 个步骤。

下面挑几个最能体现「顺序依赖」的方法细看。

**读 config 并统一字段名**——一切超参的源头：

[basemodel.py:141-150](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L141-L150) 读取 `config.json`，并用 `repair_config` 把不同模型族的不同命名（如 `n_head`/`num_attention_heads`、`n_embd`/`hidden_size`、`n_layer`/`num_hidden_layers`）统一成标准名。这就是为什么后面代码能放心地用 `self.config["num_attention_heads"]`，而不必关心原始字段叫什么。

**建权重对象（空壳）**：

[basemodel.py:166-177](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L166-L177) 用子类填入的 `pre_and_post_weight_class` 和 `transformer_weight_class` 创建 `pre_post_weight` 和长度为 `n_layer` 的 `trans_layers_weight` 列表。此刻这些权重对象只是「容器」，真正的张量数据要到第 14 步 `_load_hf_weights` 才填入。

**建内存管理器并切分注意力头**：

[basemodel.py:191-201](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L191-L201) 这里体现了 `TpPart` 的核心——每张卡只负责一部分注意力头：

\[
\text{head\_num\_per\_rank} = \left\lfloor \frac{\text{num\_attention\_heads}}{\text{tp\_world\_size}} \right\rfloor
\]

`select_mem_manager_class()` 会按是否开启 FP8 量化等条件选具体的内存管理器实现（u4-l1 / u6-l3 详讲），`layer_num` 还额外加上 `get_added_mtp_kv_layer_num()` 以兼容 MTP 草稿模型的额外 KV 层。

**刻意延后的「挂接」与「回填」**：

[basemodel.py:112-114](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L112-L114) 注意这行 `self.req_manager.mem_manager = self.mem_manager` 被刻意放在 `_init_mem_manager` 之后。源码注释解释：像 qwen3.5 这类 linear 架构模型，`req_manager` 会保存大量运行时 linear state，可能占用大量显存，所以必须先建好 `mem_manager` 再挂接，避免重复占用。

[basemodel.py:203-222](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L203-L222) `_check_mem_size` 用 `self.mem_manager.size` **回填** `self.max_total_token_num`。这意味着：你在命令行传的 `--max_total_token_num` 只是一个「期望上限」，真实容量由内存管理器按可用显存（`mem_fraction`）profiling 后决定（详见 u4-l1）。这里还做了两条断言：容量必须大于 `batch_max_tokens`；非个人性能模式下，容量必须能放下 `max_seq_length`。

**子类钩子 `_init_custom`**：

[basemodel.py:339-340](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L339-L340) 基类里它是空的 `pass`，是留给子类的扩展点。例如 llama 在 [llama/model.py:70-99](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/model.py#L70-L99) 覆写它，根据 `rope_scaling` 的类型选择不同的旋转位置编码初始化方式（default/yarn/dynamic/su/llama3/mrope）。这是「公共流程 + 局部定制」的典型体现。

**收尾**：

[basemodel.py:136-139](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L136-L139) 最后 `_check_max_len_infer` 会用最大长度试跑一次 prefill 探测 OOM，然后清缓存、调用 `set_model_init_status(True)` 把「模型已就绪」标记为真——这个标记会被 backend 进程的初始化握手用到（u2-l4 提到的 `init ok` 握手）。

#### 4.2.4 代码实践

**实践目标**（本讲指定的核心实践）：在 `__init__` 中梳理 `TpPartBaseModel` 的初始化步骤，列出各 `_init_*` 方法的调用顺序与职责，并找出步骤间的依赖。

**操作步骤**：

1. 打开 [basemodel.py:104-139](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L104-L139)，按调用次序写下每一个 `self._init_*()` / `self._check_*()` / `self._autotune_*()`。
2. 对每一步，跳转到它的方法定义（如 `_init_config` 在 L141、`_init_weights` 在 L166、`_init_mem_manager` 在 L191、`_check_mem_size` 在 L203、`_init_infer_layer` 在 L235、`_init_some_value` 在 L244、`_init_att_backend` 在 L254、`_init_cudagraph` 在 L265），用一句话记录它的职责。
3. 画出依赖关系。至少回答这三个问题：
   - 为什么 `_init_config` 必须最先？（答：后面所有方法都依赖 `self.config`。）
   - 为什么 `_load_hf_weights` 必须在 `_init_weights` 之后？（答：`_init_weights` 只建空壳对象，`_load_hf_weights` 才把张量灌进去。）
   - 为什么 `req_manager.mem_manager` 的赋值要放在 `_init_mem_manager` 之后？（答：见源码 L112-L114 注释，避免 linear 架构模型的显存重复占用。）
4. （可选动手）在本地起服务时设置环境变量 `DISABLE_CHECK_MAX_LEN_INFER=1`（见 [basemodel.py:1032-1036](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L1032-L1036)），对比启动日志中是否少了 `check max_len ... infer ok` 这一行，从而验证第 21 步确实被跳过。

**需要观察的现象**：步骤之间是严格的串行依赖；删除或调换任意一步，后续步骤大概率因属性未定义（`AttributeError`）或断言失败而报错。

**预期结果**：得到一张与 4.2.2 流程图一致的「步骤—职责—依赖」三列表。

> 说明：步骤 4 涉及实际启动服务，若本地无 GPU 环境则为「待本地验证」；前 3 步为纯源码阅读，可直接完成。

#### 4.2.5 小练习与答案

**练习 1**：`_verify_must` 被标记为 `@final`（不可覆写），而 `_verify_params` 没有。想一想为什么一个用 `final`、一个不用？

**参考答案**：`num_attention_heads % tp == 0` 是任何 TP 模型都必须满足的硬性数学约束，不应被子类绕过，所以用 `@final` 锁死；而 `load_way`、`num_key_value_heads` 的校验可能因模型而异（例如 llama 的 `_verify_params` 允许 `load_way in ["HF", "DS"]`，见 [llama/model.py:51-55](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/model.py#L51-L55)），所以留给子类覆写。

**练习 2**：如果用户没传 `--max_total_token_num`，最终模型的 `max_total_token_num` 由谁决定？

**参考答案**：由 `_check_mem_size` 中的 `self.max_total_token_num = self.mem_manager.size` 决定（[basemodel.py:204](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L204)）。而 `mem_manager.size` 是内存管理器按 `mem_fraction`（默认 0.9）对可用显存做 profiling 后得出的真实可缓存 token 数（u4-l1 详讲）。

---

### 4.3 推理状态：InferStateInfo 与一次前向的上下文

#### 4.3.1 概念说明

模型组装好之后，每次推理（一个 prefill batch 或一个 decode batch）都需要一个「上下文对象」来携带这批数据在本次前向中用到的全部信息：输入 id、batch 形状、KV 内存索引、当前是 prefill 还是 decode、派生出的位置编码与累计长度张量等等。这个对象就是 `InferStateInfo`。

要特别注意它与 `ModelInput` 的区别，初学者很容易混淆：

- **`ModelInput`**（[batch_objs.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/batch_objs.py)）：是一个 `@dataclass`，**从外部（backend）传进来**的原始输入容器，字段偏「静态/原始」——如 `input_ids`、`b_req_idx`、`b_seq_len`、`mem_indexes`。
- **`InferStateInfo`**（[infer_struct.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/infer_struct.py)）：是模型**内部创建**的运行时上下文，除了拷贝 `ModelInput` 的部分字段，还持有 `mem_manager`/`req_manager` 的引用、注意力状态对象，以及一批**派生张量**（`b_q_seq_len`、`b1_cu_q_seq_len`、`position_ids` 等，由 `init_some_extra_state` 现算）。

简言之：`ModelInput` 是「请求方打包的行李」，`InferStateInfo` 是「模型方开展工作的工作台」。

`InferStateInfo` 还是一个**可扩展基类**——子类可以继承它来塞进模型专属字段（如 MRoPE 的位置增量、MTP 草稿的隐藏态输入）。基类插槽 `infer_state_class` 默认就是它。

#### 4.3.2 核心流程

一次前向的状态生命周期：

```
backend 构造好 ModelInput（含 input_ids、mem_indexes、b_req_idx、b_seq_len…）
        │
        ▼
TpPartBaseModel.forward(model_input)                      [basemodel.py:342]
        │  按 model_input.is_prefill 分发到 _prefill / _decode
        ▼
_create_inferstate(model_input)                           [basemodel.py:352]
        │  1) infer_state = self.infer_state_class()      新建空状态
        │  2) 把 ModelInput 的字段搬进 infer_state
        │  3) 挂上 self.mem_manager / self.req_manager
        │  4) 由注意力后端创建 prefill_att_state / decode_att_state
        ▼
infer_state.init_some_extra_state(self)                   [infer_struct.py:105]
        │  现算 b_q_seq_len / cu_q_seq_len / b_kv_seq_len / position_ids 等派生张量
        ▼
infer_state.init_att_state()                              [infer_struct.py:129]
        │  初始化注意力后端的内部状态
        ▼
_context_forward / _token_forward（层推理，u3-l2/u3-l3 详讲）
```

注意：`InferStateInfo` 是**一次性的**——每个 batch、每次前向都新建一个，用完即弃。这与 `mem_manager`/`req_manager`（模型级常驻）形成对比。

#### 4.3.3 源码精读

**前向入口与分发**：

[basemodel.py:342-350](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L342-L350) `forward` 先把输入搬到 CUDA，再按 `is_prefill` 分发到 `_prefill` 或 `_decode`。注意这里的 `@torch.no_grad()`——推理全程不需要梯度。

**状态对象的创建与装配**：

[basemodel.py:352-404](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L352-L404) `_create_inferstate` 是本模块的核心。它做了四件事：

1. `infer_state = self.infer_state_class()`——用子类填的插槽（默认 `InferStateInfo`）新建对象。
2. 把 `ModelInput` 上的字段逐一搬到 `infer_state`（`input_ids`、`batch_size`、`b_req_idx`、`b_seq_len`、`mem_index` 等）。
3. 挂上模型级常驻组件：`infer_state.mem_manager = self.mem_manager`、`infer_state.req_manager = self.req_manager`、`infer_state.dist_group = dist_group_manager.get_group(microbatch_index)`。
4. 根据 `is_prefill`，由注意力后端创建对应的注意力状态：`create_att_prefill_state` 或 `create_att_decode_state`（多后端扩展位 `..._backend1` 同理）。

**InferStateInfo 的字段全景**：

[infer_struct.py:17-103](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/infer_struct.py#L17-L103) 字段可粗分四类：

| 类别 | 代表字段 | 说明 |
| ---- | ------- | ---- |
| 注意力状态 | `prefill_att_state`、`decode_att_state`（及扩展位 `..._state1`） | 由注意力后端创建，承载 attention kernel 需要的内部状态 |
| batch 形状 | `batch_size`、`total_token_num`、`b_req_idx`、`b_seq_len`、`b_ready_cache_len` | 来自 ModelInput |
| 资源句柄 | `mem_manager`、`req_manager`、`mem_index` | 指向模型级常驻组件与本批的 KV 索引 |
| 模式标记 | `is_prefill`、`is_cuda_graph`、`is_token_healing`、`microbatch_index` | 控制不同推理路径的开关 |

后续的「派生张量」（`b_q_seq_len`、`b1_cu_q_seq_len`、`b_kv_seq_len`、`b1_cu_kv_seq_len`、`position_ids` 等）在 L71-L82 声明为 `None`，等 `init_some_extra_state` 来填。

**派生张量的现算**：

[infer_struct.py:105-127](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/infer_struct.py#L105-L127) `init_some_extra_state` 按 prefill/decode 分别调用 `gen_prefill_params` / `gen_decode_params`（Triton kernel 辅助函数），算出累计长度张量（`cu_q_seq_len` 即 CSR 风格的偏移）和位置 id。prefill 还会从 `b1_cu_q_seq_len` 切出 `b_q_start_loc`；decode 则切出 `b_kv_start_loc`。这些是注意力计算定位每个请求 KV 的关键索引。

**注意力状态的初始化**：

[infer_struct.py:129-137](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/infer_struct.py#L129-L137) `init_att_state` 调用当前阶段注意力状态对象的 `init_state()`（若启用了第二个注意力后端，也会一并初始化）。注意力后端本身是单例（见 [base_att.py:11-32](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/attention/base_att.py#L11-L32) 的 `__new__` 单例实现），而 `prefill_att_state`/`decode_att_state` 是每次前向新建的轻量状态，二者分离。

#### 4.3.4 代码实践

**实践目标**：跟踪 `_create_inferstate`，画出「字段来源」映射图，彻底分清 `ModelInput` 与 `InferStateInfo`。

**操作步骤**：

1. 打开 [basemodel.py:352-404](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L352-L404)。
2. 列一张表，左列为 `InferStateInfo` 的字段，右列填它的来源。来源只有三类：
   - 来自 `model_input`（如 `input_ids`、`batch_size`、`b_req_idx`）；
   - 来自 `self`（模型级常驻，如 `mem_manager`、`req_manager`）；
   - 来自注意力后端（如 `prefill_att_state = self.prefill_att_backend.create_att_prefill_state(...)`）。
3. 对照 [infer_struct.py:17-61](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/infer_struct.py#L17-L61)，标出哪些字段在 `_create_inferstate` 里被赋值，哪些要等到 `init_some_extra_state`/`init_att_state` 才有值。
4. 回答：为什么 `b_q_seq_len`、`position_ids` 不在 `_create_inferstate` 里直接算，而要单独放到 `init_some_extra_state`？

**需要观察的现象**：`_create_inferstate` 只做「搬运 + 挂接」，不做计算；所有派生张量都推迟到 `init_some_extra_state`。

**预期结果**：得到一张清晰的「字段—来源」表，并理解「分离搬运与计算」是为了让 CUDA Graph 捕获时能只重放计算部分、复用搬运结果（与 u6-l1 CUDA Graph 呼应）。

> 说明：第 4 题的参考答案——把派生计算独立出来，便于在 CUDA Graph 重放路径里用 `copy_for_cuda_graph`（[infer_struct.py:139-149](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/infer_struct.py#L139-L149)）只拷贝张量而不重跑参数生成，降低开销。

#### 4.3.5 小练习与答案

**练习 1**：`InferStateInfo` 是模型级常驻对象，还是每批次新建？为什么这样设计？

**参考答案**：每批次（每次前向）新建。因为状态里携带的是「这一批数据」的专属信息（输入 id、batch 形状、派生索引），不同 batch 互不相同；而真正昂贵的资源（KV Cache、权重）常驻在 `mem_manager` 和权重对象里，状态对象本身只是轻量的「指针与索引集合」，新建成本很低。

**练习 2**：`_create_inferstate` 接受一个 `microbatch_index` 参数（默认 0）。结合 u2-l4 提到的双 batch overlap，猜猜它有什么用？

**参考答案**：在 microbatch overlap（双流重叠）模式下，会有 `infer_state0`（microbatch_index=0）和 `infer_state1`（microbatch_index=1）两个状态同时存在，分别绑定不同的分布式通信组 `dist_group = dist_group_manager.get_group(microbatch_index)`，从而让两个 microbatch 在不同流上重叠执行、隐藏通信延迟（详见 u6-l2）。

## 5. 综合实践

把本讲三个模块串起来，完成一次「端到端追踪」：

**任务**：从 u2-l4 的 `ModeBackend.init_model` 出发，一直追到一次 `forward` 的状态创建，画出一张完整的数据流图，并写出每一步用到的源码位置。

**要求覆盖的环节**：

1. **上游入参**：在 [base_backend.py:132-153](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/base_backend.py#L132-L153) 找出 `model_kvargs` 里都有哪些键，它们如何变成 `TpPartBaseModel.__init__` 的 `kvargs`（对照 [basemodel.py:60-103](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L60-L103)）。
2. **插槽填充**：说明 `get_model` 如何依据 `config.json` 选到具体子类（如 `LlamaTpPartModel`），子类的六个插槽分别是什么（4.1）。
3. **初始化流水线**：按 4.2.2 的 22 步，标注 config→weights→mem_manager→infer_layer→load→att_backend→cudagraph 的顺序，并指出 backend 后续依赖的 `self.model.mem_manager`、`self.model.req_manager`、`self.model.vocab_size` 分别在哪一步诞生。
4. **运行时状态**：假设一次 prefill 到来，写出 `forward → _create_inferstate → init_some_extra_state → init_att_state` 的调用链（4.3），并标注 `InferStateInfo` 各字段的来源。

**交付物**：一张数据流图 + 一份「属性诞生地」清单。

**预期结果**：你能用一句话向别人解释清楚——「lightllm 的每个 GPU 进程里，一个模型 = 子类填好的六个零件插槽 + 基类一条写死的初始化流水线；每次推理 = 一个新建的 InferStateInfo 上下文穿过 pre/post/transformer 推理层」。这正好为下一讲（u3-l2 prefill/decode 主流程）打好地基。

> 说明：本实践为源码阅读型综合实践，不需要运行环境；如需验证某步行为，可结合 `unit_tests/` 下相关测试断言对照。

## 6. 本讲小结

- `TpPartBaseModel` 是所有模型的基类，自身不含模型专属逻辑，靠「六个类属性插槽」（权重类 ×2、推理类 ×3、推理状态类 ×1）让子类注入零件，名字里的 `TpPart` 表示「张量并行的一片」。
- `__init__` 是一条写死的、近 20 步的初始化流水线，顺序严格：先读 config → 建权重空壳 → 建请求/内存管理器 → 回填容量 → 建推理层 → 子类钩子 `_init_custom` → 加载真实权重 → 建注意力后端 → 预热与 CUDA Graph → 试跑测 OOM → 标记就绪。
- 关键依赖有三处：config 是一切超参之源；内存管理器建好后才回填真实 `max_total_token_num`；权重对象先建空壳、后灌数据。
- `req_manager.mem_manager` 的挂接被刻意延后到 `_init_mem_manager` 之后，是为兼容 linear 架构模型（如 qwen3.5）的显存占用特点。
- `InferStateInfo` 是「一次前向的上下文对象」，每批次新建、用完即弃；它搬运 `ModelInput` 字段、挂接模型级常驻组件、再由 `init_some_extra_state`/`init_att_state` 现算派生张量与注意力状态。
- `ModelInput` 是外部传入的原始输入容器，`InferStateInfo` 是模型内部的工作台，二者职责分离、不要混淆。

## 7. 下一步学习建议

本讲只讲了模型「如何被组装、如何创建推理状态」，还没有进入真正的前向计算。建议按以下顺序继续：

1. **u3-l2 prefill 与 decode 推理主流程**：打开 `_prefill`/`_decode`/`_context_forward`/`_token_forward` 四个方法，看 `InferStateInfo` 是如何穿过 embedding→transformer 层→logits 的，以及 prefill 与 decode 在 padding、CUDA Graph 上的差异。
2. **u3-l3 推理层模板与层推理**：进入 `pre/post/transformer` 三类推理层的模板实现，看 `context_forward`/`token_forward` 如何统一两阶段。
3. **u3-l4 权重加载与张量并行切分**：搞清楚 `_init_weights` 建的空壳是如何按 tp 维度切分并加载的。
4. **u4-l1 KV Cache 内存管理**：深入 `_init_mem_manager` 选用的 `MemoryManager`，理解 `mem_manager.size` 到底怎么算出来的。
5. **u5-l1 模型注册机制**：弄懂 `get_model(model_cfg, model_kvargs)` 是如何依据 `config.json` 匹配到具体子类的，补全本讲 4.1 里略过的注册细节。

建议阅读源码时，把本讲的「插槽表」和「22 步流水线」打印在手边对照，会大幅降低迷路概率。
