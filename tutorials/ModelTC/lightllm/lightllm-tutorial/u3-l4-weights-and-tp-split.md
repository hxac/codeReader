# 权重加载与张量并行切分

## 1. 本讲目标

本讲承接 [u3-l1 TpPartBaseModel 推理框架] 与 [u3-l2 prefill 与 decode 推理主流程]。在 u3-l1 里我们学到：`TpPartBaseModel` 的初始化流水线里有一段 `_init_weights()`（建权重空壳）→ `_load_hf_weights()`（从磁盘读真实权重）的步骤；在 u3-l3 里我们学到：推理层会反复调用 `layer_weight` 上的权重去做前向。但「权重是怎么从 HuggingFace 格式的文件里读进来」「为什么 `tp=2` 时每张卡只拿到一半的矩阵」「少一个张量为什么会直接 assert 失败」这些底层的加载与切分问题，到此还是黑盒。

读完本讲，你应当能够：

1. 说出 LightLLM 权重体系的「两层结构」：外层 `TransformerLayerWeight`/`PreAndPostLayerWeight` 只是**容器**，真正存储和切分权重的是内层的**元权重（meta weight）**。
2. 看懂 `hf_load_utils.py` 是如何多线程、多文件地把 `.safetensors` / `.bin` 读进来的。
3. 解释 `q_proj`/`kv_proj`、`o_proj`、`gate_up_proj`/`down_proj` 这几类权重分别沿哪一维、用什么切分器切到各个 TP rank，以及为什么 `o_proj`/`down_proj` 的 bias 要除以 `tp_world_size`。
4. 指出 `verify_load()` 这道「加载完成校验」在做什么，少权重时它如何把问题暴露出来。

---

## 2. 前置知识

### 2.1 HuggingFace（HF）权重格式

LightLLM 直接读 HuggingFace 训练出来的权重，文件通常是：

- `config.json`：模型结构配置（层数、隐藏维 `hidden_size`、注意力头数 `num_attention_heads`、KV 头数 `num_key_value_heads`、词表大小 `vocab_size` 等）。
- `*.safetensors`（首选）或 `pytorch_model*.bin`：真正的权重张量，以 `{名字: 张量}` 的字典形式存放。例如 `model.layers.0.self_attn.q_proj.weight` 就是第 0 层的 q 投影矩阵。

一个大模型常被切成多个分片文件（`model-00001-of-00005.safetensors` ……），加载时要把它们拼起来。

### 2.2 线性层的权重矩阵

一个线性层 `y = xWᵀ + b`，输入 `x` 维度为 `in_dim`，输出 `y` 维度为 `out_dim`。LightLLM 里把权重**存成 `[out_dim, in_dim]` 的形状**（注意是「外维在前」），这样 `yᵀ = W @ xᵀ`，一行权重对应一个输出特征。这是后面理解「按行切 / 按列切」的基础，源码里也有一句明确注释（见 4.3.3）。

### 2.3 张量并行（Tensor Parallelism, TP）

把一个太大的矩阵乘法拆到多张 GPU 上算。Megatron 风格的 TP 有两种基本切法：

- **列并行（输出并行）**：把权重沿**输出维**切开，每张卡算不同的输出列。代表是 `q/k/v_proj`、`gate/up_proj`。切完后各卡结果不需要立刻求和，可以直接进下一个按元素运算（如 SwiGLU 或 attention 的 head reshape）。
- **行并行（输入并行）**：把权重沿**输入维**切开，每张卡算「整段输出的一部分」，最后把各卡结果**相加（all-reduce）**才得到完整输出。代表是 `o_proj`、`down_proj`。

> 命名提醒：LightLLM 源码里把这两类分别叫 `ROWMMWeight`（沿权重的「行」= 输出维切）和 `COLMMWeight`（沿权重的「列」= 输入维切）。这里的「行/列」指的是权重矩阵 `[out,in]` 的物理行列，**和 Megatron 的「row/column parallel」名字正好相反含义**。本讲统一用「输出维切 / 输入维切」来描述，避免混淆。

### 2.4 TP 身份从哪来

每个 GPU 进程在启动时会被环境变量打上身份标记：`LIGHTLLM_CURRENT_RANK_IN_DP`（在 DP 组内的 rank，本讲即 TP rank）与 `LIGHTLLM_DP_WORLD_SIZE`（DP 组大小，本讲即 TP 并行度）。权重切分时就读这两个值来决定「我是第几片 / 一共几片」：

- [lightllm/utils/dist_utils.py:192-193](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/dist_utils.py#L192-L193) —— `get_dp_world_size()` 读 `LIGHTLLM_DP_WORLD_SIZE`，返回 TP 并行度。
- [lightllm/utils/dist_utils.py:216-217](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/dist_utils.py#L216-L217) —— `get_current_rank_in_dp()` 读 `LIGHTLLM_CURRENT_RANK_IN_DP`，返回当前 TP rank。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [lightllm/common/basemodel/layer_weights/base_layer_weight.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/base_layer_weight.py) | 权重容器基类 `BaseLayerWeight`：持有 `tp_rank_`/`tp_world_size_`，提供「遍历所有元权重并加载 / 校验」的通用方法。 |
| [lightllm/common/basemodel/layer_weights/transformer_layer_weight.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/transformer_layer_weight.py) | transformer 层权重容器 `TransformerLayerWeight`，本身不存权重，只定义加载骨架。 |
| [lightllm/common/basemodel/layer_weights/pre_and_post_layer_weight.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/pre_and_post_layer_weight.py) | 首尾层权重容器 `PreAndPostLayerWeight`（embedding / lm_head / final_norm）。 |
| [lightllm/common/basemodel/layer_weights/hf_load_utils.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/hf_load_utils.py) | HF 权重的**入口**：多线程读多文件，把每份 `weights` 字典分发给各层。 |
| [lightllm/common/basemodel/layer_weights/meta_weights/parameter_weight.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/parameter_weight.py) | 元权重 `ParameterWeight` / `TpParameterWeight`：最朴素的「整取」与「按维 narrow 切片」。 |
| [lightllm/common/basemodel/layer_weights/meta_weights/mm_weight/mm_weight.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/mm_weight/mm_weight.py) | 矩阵乘元权重模板 `MMWeightTpl`：`load_hf_weights` 调度「切分器 + 量化方法」完成加载与校验。 |
| [lightllm/common/basemodel/layer_weights/meta_weights/mm_weight/mm_slicer.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/mm_weight/mm_slicer.py) | 切分器 `RowSliceMixin` / `ColSliceMixin`：真正决定沿哪一维切、切哪一段的核心逻辑。 |
| [lightllm/common/basemodel/layer_weights/meta_weights/mm_weight/colmm_weight.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/mm_weight/colmm_weight.py) 与 [rowmm_weight.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/mm_weight/rowmm_weight.py) | 具体权重类 `COLMMWeight` / `ROWMMWeight` / `KVROWNMMWeight` / `QKVROWNMMWeight`：把「按维切」装配成可用对象。 |
| [lightllm/common/basemodel/layer_weights/meta_weights/embedding_weight.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/embedding_weight.py) | 词表维度的 embedding / lm_head 切分（沿词表 0 维切）。 |
| [lightllm/models/llama/layer_weights/](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_weights/transformer_layer_weight.py) | 一个**具体模型**如何把上述元权重拼装成完整的 llama 层。 |

---

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**4.1 权重基类**（容器与元权力的拼装）、**4.2 HF 权重加载**（磁盘→内存）、**4.3 张量并行切分**（整张权重→各 rank 的一片）。

### 4.1 权重基类与「元权重」拼装

#### 4.1.1 概念说明

LightLLM 的权重体系是「**两层结构**」：

- **外层容器**：`PreAndPostLayerWeight`（首尾层）和 `TransformerLayerWeight`（每一层 transformer）。它们本身**几乎不存任何张量**，只负责「声明这一层由哪些零件组成」。
- **内层元权重（meta weight）**：真正持有张量、负责加载与切分的最小单元，如 `ROWMMWeight`（q/gate/up 的输出维切）、`COLMMWeight`（o/down 的输入维切）、`KVROWNMMWeight`（GQA 的 k/v）、`RMSNormWeight`（归一化，不切）、`EmbeddingWeight`（词表切）等。

这样设计的好处是：容器只管「拼装与遍历」，切分/量化/校验这些**可复用的机械逻辑**全下沉到元权重里。新增模型时，你只需要在容器里「声明」用哪些元权重（给名字、给形状、给切分类型），加载和校验的代码一行都不用写。

#### 4.1.2 核心流程

把 u3-l1 的初始化流水线与本讲衔接，权重相关的三步是：

```
_init_weights()      # 1) 建空壳：实例化 PreAndPostLayerWeight 和 N 个 TransformerLayerWeight
                     #    —— 每个空壳在 __init__ 里把自己用到的元权重都 new 出来（分配好形状正确的空张量）
...
_load_hf_weights()   # 2) 读磁盘：调 hf_load_utils 把 .safetensors 读进内存，分发给每一层
                     #    —— 每个元权重的 load_hf_weights 把自己那段权重 copy_/narrow 进空张量
                     # 3) 校验：pre_post_weight.verify_load() + 每层 verify_load()
                     #    —— 逐个元权重检查 load_ok 标志，任何一个没加载成功就 assert 失败
```

注意「先建空壳、后填数据」是刻意的：建空壳时就已经按当前 rank 的 TP 身份算好了**这一片该有的形状**，加载时只需把对应那一段数据塞进去，无需在运行期再切。

#### 4.1.3 源码精读

**(1) 容器基类 `BaseLayerWeight`：遍历所有元权重做加载与校验**

[lightllm/common/basemodel/layer_weights/base_layer_weight.py:8-43](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/base_layer_weight.py#L8-L43) —— 构造时记下 `tp_rank_`/`tp_world_size_`（来自环境变量），并提供两个通用遍历方法。其中 `verify_load` 是本模块的关键校验入口：

```python
def verify_load(self):
    """verify all load is ok"""
    for attr_name in dir(self):
        attr = getattr(self, attr_name)
        if isinstance(attr, BaseWeight):
            ...
            assert attr.verify_load(), f"Loading {attr_name} of layers {layer_num} fails."
```

它的做法很朴素：用 `dir(self)` **反射遍历自己的所有属性**，凡是 `BaseWeight`（元权重的抽象基类）的，就调它的 `verify_load()`；只要有一个返回 `False`，就 `assert` 报错并指出是哪一层的哪个属性没加载成功。`load_hf_weights` 同理——遍历所有元权重并调它们的 `load_hf_weights(weights)`。

> 这种「用 `dir()` 反射」的写法意味着：你在容器里把一个元权重**命名为任意名字**（如 `self.q_proj`、`self.att_norm_weight_`），它会自动被发现和加载，不需要手动维护一个清单。

**(2) 两个容器本身非常薄**

[lightllm/common/basemodel/layer_weights/transformer_layer_weight.py:33-43](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/transformer_layer_weight.py#L33-L43) —— `TransformerLayerWeight.load_hf_weights` 也只是反射遍历：

```python
def load_hf_weights(self, weights):
    for attr_name in dir(self):
        attr = getattr(self, attr_name, None)
        if isinstance(attr, MMWeightTpl) and len(attr.weight_names) >= 2:
            with self.lock:
                attr.load_hf_weights(weights)
        elif isinstance(attr, BaseWeight):
            attr.load_hf_weights(weights)
```

注意一个小细节：对于 `MMWeightTpl`（矩阵乘元权重）且 `weight_names >= 2` 的情况（例如把 gate 和 up 两个权重塞进同一个对象、或 q/k/v 三个塞进一个对象），加载时加了 `self.lock` 互斥——因为这种「一个对象管多个权重」的场景里内部有共享存储，需要线程安全（多文件并发加载时见 4.2）。

[lightllm/common/basemodel/layer_weights/pre_and_post_layer_weight.py:5-23](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/pre_and_post_layer_weight.py#L5-L23) —— `PreAndPostLayerWeight` 完全同理，构造时调 `init_static_params()`，加载时反射遍历。

**(3) 入口在 basemodel**

[lightllm/common/basemodel/basemodel.py:166-189](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L166-L189) —— `_init_weights` 建空壳（一个首尾层 + N 个 transformer 层），`_load_hf_weights` 读盘 + 校验：

```python
def _load_hf_weights(self):
    load_hf_weights(self.data_type, weight_dir=self.weight_dir_,
                    pre_post_layer=self.pre_post_weight,
                    transformer_layer_list=self.trans_layers_weight,
                    weight_dict=self.weight_dict)
    self.pre_post_weight.verify_load()
    [weight.verify_load() for weight in self.trans_layers_weight]
```

在 `_init_weights` 之前还有一道 TP 合法性预检 [basemodel.py:157-160](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L157-L160)：要求 `load_way == "HF"` 且 KV 头数能被 TP 整除（GQA 切分的前置条件，详见 4.3）。

#### 4.1.4 代码实践

> **实践目标**：验证「容器只负责遍历，真正的形状/切分逻辑在元权重里」这一结论。

1. 打开 [lightllm/models/llama/layer_weights/transformer_layer_weight.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_weights/transformer_layer_weight.py)，看 `_init_weight()`（L19-24）：它把 llama 一层拆成 `_init_qkv / _init_o / _init_ffn / _init_norm` 四步，每步 new 出若干元权重对象挂到 `self` 上。
2. 数一数：llama 的一层 transformer 一共挂了哪些元权重属性（`q_proj`、`kv_proj`、`o_proj`、`gate_up_proj`、`down_proj`、`att_norm_weight_`、`ffn_norm_weight_`）。
3. 再回到 `BaseLayerWeight.verify_load`（L29-40），确认它确实不认识「q_proj」这个名字，而是靠 `isinstance(attr, BaseWeight)` 反射发现的。

**需要观察的现象**：容器代码里**没有任何**与具体权重名相关的硬编码逻辑；改名一个元权重属性（比如把 `self.q_proj` 改名 `self.qq`），加载与校验依然能工作（只要推理层那边也跟着改引用）。这正是反射遍历带来的灵活性。

**预期结果**：你能用一句话说清「容器 = 反射遍历器，元权重 = 自带 load/verify/split 的积木」。

#### 4.1.5 小练习与答案

**练习 1**：如果新增一个元权重对象但忘记在 `_init_weight` 里挂到 `self`，会发生什么？
**答案**：`dir(self)` 发现不了它，`load_hf_weights` 不会加载、`verify_load` 也检查不到它。它既不会被填充也不会报错——但推理层用到它时会拿到空张量，行为不可预期。所以「挂到 self」是让权重进入加载/校验闭环的必要条件。

**练习 2**：`BaseLayerWeight.verify_load` 里为什么要区分 `hasattr(self, "layer_num_")`？
**答案**：transformer 层有 `layer_num_`（报错时能指出是第几层），而 `PreAndPostLayerWeight` 没有，报错时 `layer_num=None`。这只是为了让报错信息更可定位，不影响校验逻辑。

---

### 4.2 HF 权重加载

#### 4.2.1 概念说明

`hf_load_utils.py` 解决的问题是：**把磁盘上的一堆权重分片文件，变成内存里的 `{名字: 张量}` 字典，并分发给所有层。** 它要处理三件事：

1. **格式**：优先 `.safetensors`（快、安全），找不到再退到 `pytorch_model*.bin`。
2. **多文件并发**：大模型权重动辄几十上百 GB、切成多片，串行读太慢，用线程池并发读。
3. **分发**：每个分片文件里的权重可能横跨很多层，读进来后要分发给「首尾层 + 所有 transformer 层」，让它们各自挑走属于自己的那几个张量。

#### 4.2.2 核心流程

```
load_hf_weights(data_type, weight_dir, pre_post_layer, transformer_layer_list, weight_dict)
   │
   ├─ 若传入现成 weight_dict（如单测/已加载）→ 直接分发给各层后 return
   │
   ├─ 列出目录，挑出所有 .safetensors（无则退到 .bin）
   ├─ 用 ThreadPool（数量由环境变量 LOADWORKER 控制，默认 1）并发跑 load_func
   │
   └─ load_func(每个文件):
        ├─ safe_open / PetrelHelper.load 把文件读成 {名字: 张量} 的 weights 字典
        ├─ pre_post_layer.load_hf_weights(weights)   # 首尾层挑自己的
        └─ for layer in transformer_layer_list:
              layer.load_hf_weights(weights)          # 每层挑自己的（找不到就跳过）
```

关键点：**每个文件都被喂给所有层**，每层在 `load_hf_weights` 里只挑「名字匹配自己层号」的张量（见 4.3 里 `f"model.layers.{self.layer_num_}.self_attn.q_proj.weight"` 这种命名），挑不到就静默跳过。这样文件→层的映射完全靠**命名约定**自动完成，无需一个全局索引表。

#### 4.2.3 源码精读

**(1) 单文件加载 `load_func`**

[lightllm/common/basemodel/layer_weights/hf_load_utils.py:10-28](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/hf_load_utils.py#L10-L28)：

```python
def load_func(file_, use_safetensors=False, pre_post_layer=None,
              transformer_layer_list=None, weight_dir=None):
    import torch.distributed as dist
    torch.cuda.set_device(get_current_device_id())   # 多线程时每个线程要重新钉住自己的 GPU
    if use_safetensors:
        weights = safe_open(os.path.join(weight_dir, file_), "pt", "cpu")
        weights = {k: weights.get_tensor(k) for k in weights.keys()}
    else:
        weights = utils.PetrelHelper.load(os.path.join(weight_dir, file_), map_location="cpu")
    if pre_post_layer is not None:
        pre_post_layer.load_hf_weights(weights)
    if transformer_layer_list is not None:
        for layer in transformer_layer_list:
            layer.load_hf_weights(weights)
    del weights; gc.collect()
```

两个细节值得注意：

- 第一行注释解释了 `torch.cuda.set_device(get_current_device_id())` 的用意：**多线程加载时，每个线程内的 CUDA device 会默认切回 0**，所以每个线程开头都要显式钉回自己那张卡，否则权重会被 copy 到错误的 GPU。
- 读完后 `del weights; gc.collect()` 及时释放 CPU 内存——权重可能很大，读完即丢，避免峰值内存翻倍。

**(2) 入口 `load_hf_weights`**

[lightllm/common/basemodel/layer_weights/hf_load_utils.py:31-72](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/hf_load_utils.py#L31-L72)：先 `assert` 校验传入层的 `data_type_` 与请求的 `data_type` 一致；若给了 `weight_dict` 就走内存捷径；否则挑文件、用 `ThreadPool` + `imap_unordered` 并发分发，并用 `tqdm` 显示进度。并发度由 `os.environ.get("LOADWORKER", 1)` 控制——默认是单线程，想加速大模型加载可以设环境变量 `LOADWORKER=4` 之类。

#### 4.2.4 代码实践

> **实践目标**：理解「文件→层」是靠命名自动匹配，而非显式索引。

1. 打开 [llama 的 `_init_weight_names`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_weights/transformer_layer_weight.py#L36-L60)，注意 q 权重的名字是 `f"model.layers.{self.layer_num_}.self_attn.q_proj.weight"`——层号是拼进字符串的。
2. 假设磁盘上有分片文件 `model-00001-of-00003.safetensors`，里面既有第 0 层的 q 权重也有第 5 层的 q 权重。
3. 追踪：`load_func` 把整个字典喂给**所有**层；第 0 层只认 `...layers.0...`，第 5 层只认 `...layers.5...`，互不干扰。

**需要观察的现象**：不需要任何「张量→文件」的索引表，命名串里嵌的层号就是天然的匹配键。

**预期结果**：能解释为什么 `load_func` 对每个文件都把**全部**层遍历一遍，却不会重复加载同一个权重——因为每个名字在所有文件里全局唯一，且每层只挑自己的名字。

#### 4.2.5 小练习与答案

**练习 1**：为什么多线程加载时每个线程要重新 `torch.cuda.set_device`？
**答案**：见 `load_func` 的注释——多线程环境下线程内默认 CUDA device 会切回 0，不钉住的话元权重 `load_hf_weights` 里的 `.cuda()` 会把数据放到 0 号卡，导致 TP 时各 rank 权重串卡。

**练习 2**：如果想加速加载，该调什么？
**答案**：设置环境变量 `LOADWORKER`（如 `LOADWORKER=4`），让 `ThreadPool` 用 4 个线程并发读多个分片文件。

---

### 4.3 张量并行切分

这是本讲的核心。我们要回答：「**整张权重是怎么变成每个 rank 手里那一片的？**」

#### 4.3.1 概念说明

LightLLM 里有两套切分实现，对应两类元权重：

| 切分实现 | 适用对象 | 怎么切 |
| --- | --- | --- |
| `TpParameterWeight`（[parameter_weight.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/parameter_weight.py)） | 朴素的「按某一维 narrow」 | 构造时指定 `dim`，加载时用 `torch.narrow` 取 `[start:end]` 那一段 |
| `MMWeightTpl` + 切分器（[mm_slicer.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/mm_weight/mm_slicer.py)） | 矩阵乘权重（q/k/v/o/gate/up/down） | 通过 `RowSliceMixin`（输出维切）/ `ColSliceMixin`（输入维切）切片 |

权重矩阵按 `[out_dim, in_dim]` 存储，所以源码里 [mm_slicer.py:49-50](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/mm_weight/mm_slicer.py#L49-L50) 明确注释：

> 默认 weight 的 shape 是 `out×in`。所以 row-wise 是沿着 `dim=0` 切（输出维），col-wise 是沿着 `dim=1` 切（输入维）。

由此得到与具体模型（llama）的对应关系：

| llama 权重 | 用哪个元权重 | 沿哪维切 | Megatron 对应 | 是否需 all-reduce |
| --- | --- | --- | --- | --- |
| `q_proj` | `ROWMMWeight` | 输出维（head 切分） | 列并行 | 否（结果进 attention） |
| `k_proj`/`v_proj` | `KVROWNMMWeight` | 输出维（GQA 复制） | 列并行 | 否 |
| `o_proj` | `COLMMWeight` | 输入维 | 行并行 | **是** |
| `gate_proj`/`up_proj` | `ROWMMWeight` | 输出维（`n_inter` 切分） | 列并行 | 否（进 SwiGLU） |
| `down_proj` | `COLMMWeight` | 输入维 | 行并行 | **是** |
| embedding / lm_head | `EmbeddingWeight` | 词表维（0 维） | — | 分别 reduce / gather |

> 边界提醒：本讲只讲「权重这一片怎么切」。`o_proj`/`down_proj` 切完后各卡只算了部分和，**all-reduce 发生在推理层**（由 attention/ffn 的 TPSP 通信完成，见 [u3-l3] 与 [u6-l2 microbatch overlap 与 TPSP]）。权重侧只负责「给对的那一片」。

#### 4.3.2 核心流程

**(a) 普通按维切（`TpParameterWeight`）**：构造时算出 `split_n_embed = n_embed // tp_world_size` 和本 rank 的 `[start, end)`；加载时 `weights[name].narrow(dim, start, end-start)` 取出这一段 copy 进预分配的「这一片大小」的张量。

**(b) 矩阵乘切分（`MMWeightTpl` + slicer）**：

```
ROWMMWeight/COLMMWeight 构造：
  ├─ ROWMM: out_dims = [dim // tp for dim in out_dims]   # 预先算好每片输出大小
  │         param_slicer = RowSliceMixin                  # 输出维切
  ├─ COLMM: in_dim = in_dim // tp                         # 预先算好每片输入大小
  │         param_slicer = ColSliceMixin                  # 输入维切
  └─ MMWeightTpl._create_weight(): 按缩小后的尺寸分配空张量

加载 load_hf_weights(weights):
  ├─ 对每个 weight_name:
  │    slicer._slice_weight(weights[name])   # 切出本 rank 那一段
  │    quant_method.load_weight(切片, 本rank的存储)  # 量化方法把数据装进去（NoQuant 时直接 copy）
  └─ verify_load(): 检查每个子片 load_ok 标志
```

#### 4.3.3 源码精读

**(1) 朴素按维切：`TpParameterWeight`**

[lightllm/common/basemodel/layer_weights/meta_weights/parameter_weight.py:53-93](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/parameter_weight.py#L53-L93)。构造时做两件事：断言切分维能被 TP 整除，并算出本片的形状；加载时用 `narrow` 取片：

```python
def load_hf_weights(self, weights):
    start = self.split_n_embed * self.tp_rank_
    end   = self.split_n_embed * (self.tp_rank_ + 1)
    if self.weight_name in weights:
        t_weight = weights[self.weight_name].narrow(self.dim, start, end - start)
        self.weight.copy_(t_weight.to(self.data_type_))
        self.weight.load_ok = True
```

这就是「按 tp 维切到不同 rank」的最直观体现：rank 0 取 `[0 : n/tp]`，rank 1 取 `[n/tp : 2n/tp]`，依此类推。

**(2) 矩阵乘切分器：`RowSliceMixin` / `ColSliceMixin`**

[lightllm/common/basemodel/layer_weights/meta_weights/mm_weight/mm_slicer.py:51-67](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/mm_weight/mm_slicer.py#L51-L67)（输出维切，对应 q/gate/up）：

```python
class RowSliceMixin(SliceMixinTpl):
    def _slice_weight(self, weight):
        assert weight.shape[0] * self.repeat_times_ % self.tp_world_size_ == 0, ...
        start, end = self._get_slice_start_end(weight.shape[0])
        return weight[start:end, :]          # 沿 dim=0（输出维）切
```

[mm_slicer.py:91-103](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/mm_weight/mm_slicer.py#L91-L103)（输入维切，对应 o/down）：

```python
class ColSliceMixin(SliceMixinTpl):
    def _slice_weight(self, weight):
        assert weight.shape[1] * self.repeat_times_ % self.tp_world_size_ == 0, ...
        start, end = self._get_slice_start_end(weight.shape[1])
        return weight[:, start:end]          # 沿 dim=1（输入维）切
    def _slice_bias(self, bias):
        return bias / self.tp_world_size_ * self.repeat_times_   # 关键！bias 要除以 tp
```

**这里有一个最值得记住的细节**：`ColSliceMixin._slice_bias` 把 bias **除以 `tp_world_size`**。原因是 `COLMMWeight`（o/down，行并行）切完后各卡算的是部分和，最终要 all-reduce 求和；如果每张卡都加完整的 bias，求和后 bias 就被加了 `tp` 次。所以每卡只加 `bias/tp`，求和后才等于原 bias。这是判断一个权重是不是「行并行/需 all-reduce」的硬证据——`RowSliceMixin._slice_bias` 是直接 `bias[start:end]` 切片（列并行，不 reduce）。

切片的起止由 [mm_slicer.py:25-29](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/mm_weight/mm_slicer.py#L25-L29) 的 `_get_slice_start_end` 算：

```python
def _get_slice_start_end(self, size):
    tp_size = size * self.repeat_times_ // self.tp_world_size_
    start = tp_size * (self.tp_rank_ // self.repeat_times_)
    end = start + tp_size
    return start, end
```

当 `repeat_times_ == 1`（最常见）时退化成 `tp_size = size/tp`、`start = tp_size*rank`，与 `TpParameterWeight` 的 `narrow` 完全一致。

**(3) 装配：`ROWMMWeight` / `COLMMWeight`**

[rowmm_weight.py:11-38](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/mm_weight/rowmm_weight.py#L11-L38) —— `ROWMMWeight` 构造时先把**输出维**除以 tp（`out_dims = [self._get_tp_dim(d) for d in out_dims]`），再挂上 `RowSliceMixin`：

```python
out_dims = [self._get_tp_dim(out_dim) for out_dim in out_dims]   # 输出维 /tp
...
self.param_slicer = get_row_slice_mixin(self.quant_method.method_name, ...)
```

[colmm_weight.py:13-40](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/mm_weight/colmm_weight.py#L13-L40) —— `COLMMWeight` 则把**输入维**除以 tp（`in_dim = self._get_tp_dim(in_dim)`），挂上 `ColSliceMixin`。`_get_tp_dim` 在 [mm_weight.py:166-170](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/mm_weight/mm_weight.py#L166-L170) 里会 `assert dim % tp == 0`，这把「形状不能整除 TP」的错误挡在构造期。

**(4) GQA 的 k/v：`KVROWNMMWeight` 与 repeat_times**

GQA 模型里 KV 头数比 Q 头数少（如 Q=32、KV=8）。当 TP 数多于 KV 头数时（如 `tp=8` 但只有 4 个 KV 头），无法「每个 rank 分若干 KV 头」，于是 lightllm 用 `repeat_times` 让多个 rank **复制同一组 KV 头**：

[rowmm_weight.py:76-88](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/mm_weight/rowmm_weight.py#L76-L88)：

```python
def _get_repeat_times(self, kv_head_num):
    assert kv_head_num % self.tp_world_size_ == 0 or self.tp_world_size_ % kv_head_num == 0, ...
    if kv_head_num % self.tp_world_size_ == 0:
        return 1                                  # KV 头够分，正常切
    else:
        return self.tp_world_size_ // kv_head_num # KV 头不够分，让多个 rank 复制同一组
```

`QKVROWNMMWeight`（[rowmm_weight.py:91-149](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/mm_weight/rowmm_weight.py#L91-L149)）更进一步——它把 q/k/v 三个权重塞进一个对象，但 **q 用 `q_param_slicer`、k/v 用 `kv_param_slicer`**（不同 repeat_times），靠 `_get_param_slicer(sub_child_index)`（0=q, 1=k, 2=v）分发。

**(5) 词表维度的切分：`EmbeddingWeight`**

[embedding_weight.py:10-38](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/embedding_weight.py#L10-L38) —— embedding 表 `[vocab, dim]` 沿**词表维（0 维）**均分给各 rank，并记下 `tp_vocab_start_id`/`tp_vocab_end_id`：

```python
split_indexes = np.linspace(0, self.vocab_size, self.tp_world_size_ + 1, dtype=np.int64)
self.tp_vocab_start_id = int(split_indexes[self.tp_rank_])
self.tp_vocab_end_id   = int(split_indexes[self.tp_rank_ + 1])
...
self.weight.copy_(t_weight[self.tp_vocab_start_id:self.tp_vocab_end_id, :].to(self.data_type_))
```

推理时 embedding 用 `adjusted_ids = input_ids - tp_vocab_start_id` 把全局 token id 折算成本 rank 的局部下标（[embedding_weight.py:46-47](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/embedding_weight.py#L46-L47)）。`LMHeadWeight`（[L87-120](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/embedding_weight.py#L87-L120)）同理切词表，且支持 `tie_word_embeddings`（绑定 embedding）时直接复用 embedding 权重、不再单独加载。

**(6) 不切的权重：`RMSNormWeight`**

[norm_weight.py:12-30](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/norm_weight.py#L12-L30) —— 归一化权重是一维的 `[hidden]`，每个 rank 都要完整一份，所以构造时写死 `super().__init__(tp_rank=0, tp_world_size=1)`，加载时整取不切。

#### 4.3.4 代码实践

> **实践目标**：跟踪 `o_proj`（行并行）和 `q_proj`（列并行）在加载后被切到不同 rank 的全过程，并指出 verify 流程的关键校验。这是任务规格指定的实践。

**操作步骤（源码阅读型）**：

1. 设定场景：llama 模型，`hidden_size=4096`、`num_attention_heads=32`、`num_key_value_heads=8`、`head_dim=128`、`intermediate_size=11008`，启动 `--tp 2`。于是 `tp_world_size_=2`，rank 0 和 rank 1 各持有一片。

2. 追踪 **`o_proj`（输入维切，行并行）**：
   - 在 [llama `_init_o`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_weights/transformer_layer_weight.py#L83-L93)：`o_proj = COLMMWeight(in_dim=32*128=4096, out_dims=[4096], ...)`。
   - 进入 [COLMMWeight 构造](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/mm_weight/colmm_weight.py#L13-L40)：`in_dim = _get_tp_dim(4096) = 2048`（**输入维减半**），挂 `ColSliceMixin`。
   - 加载时 [ColSliceMixin._slice_weight](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/mm_weight/mm_slicer.py#L91-L100)：原始 `o_proj.weight` 形状 `[4096(out), 4096(in)]`，沿 dim=1 切 → rank 0 取 `[:, 0:2048]`，rank 1 取 `[:, 2048:4096]`，各自得到 `[4096, 2048]`。
   - 结论：每卡只掌握「一半的输入特征」，所以前向时各卡只算出部分和，**需要 all-reduce**（在推理层完成）。

3. 追踪 **`q_proj`（输出维切，列并行）**：
   - 在 [llama `_init_qkv`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/transformer_layer_weight.py#L62-L81)：`q_proj = ROWMMWeight(in_dim=4096, out_dims=[32*128=4096], ...)`。
   - 进入 [ROWMMWeight 构造](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/mm_weight/rowmm_weight.py#L11-L38)：`out_dims = [_get_tp_dim(4096)] = [2048]`（**输出维减半**），挂 `RowSliceMixin`。
   - 加载时 [RowSliceMixin._slice_weight](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/mm_weight/mm_slicer.py#L55-L60)：原始 `q_proj.weight` 形状 `[4096(out), 4096(in)]`，沿 dim=0 切 → rank 0 取 `[0:2048, :]`（即前 16 个 head），rank 1 取 `[2048:4096, :]`（后 16 个 head）。
   - 结论：每卡掌握「一半的 Q 头」，输出直接进 attention，**无需 all-reduce**。

4. **verify/load 流程的关键校验**（任务要求指出）：
   - 形状整除校验：构造期 `_get_tp_dim` 里 `assert dim % tp_world_size_ == 0`（[mm_weight.py:166-170](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/mm_weight/mm_weight.py#L166-L170)）、切片器里 `assert weight.shape[*] * repeat_times_ % tp_world_size_ == 0`。比如 `--tp 3` 配 32 个头会在构造期就 assert 失败。
   - 加载完成校验：每个元权重加载成功后会置 `load_ok=True`；`verify_load`（[mm_weight.py:159-164](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/mm_weight/mm_weight.py#L159-L164)）检查 `mm_param_list` 里每个子片都 `load_ok`、bias（若有）也都 `load_ok`，否则 `logger.warning` 并返回 `False`，最终被 `BaseLayerWeight.verify_load` 的 `assert` 拦下。
   - 词表大小校验：`EmbeddingWeight.load_hf_weights` 里 `assert loaded_vocab_size == self.vocab_size`（[embedding_weight.py:34-36](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/embedding_weight.py#L34-L36)），防止 config 与权重不符。

**需要观察的现象（手算核对）**：

- `o_proj`：rank0 拿 `[4096, 2048]`，rank1 拿 `[4096, 2048]`，二者沿**输入维**互补；若 o_proj 有 bias，bias 会被 `ColSliceMixin` 除以 2（每卡加一半，all-reduce 后还原）。
- `q_proj`：rank0 拿 `[2048, 4096]`（前 16 头），rank1 拿 `[2048, 4096]`（后 16 头），沿**输出维**互补；bias 直接切片。

**预期结果**：你能画一张表，对 llama 的 `q/kv/o/gate_up/down` 六类权重分别填出「元权重类型 / 切哪一维 / rank0 拿到的形状 / 是否 all-reduce」四列，且 `o` 与 `down` 两行勾选「需要 all-reduce」。

**可选的动手验证（示例代码，需本地 GPU 环境，待本地验证）**：

下面这段独立脚本不在 lightllm 运行路径内，仅用纯 PyTorch 复现「`tp=2` 时 o_proj 与 q_proj 的切分」，帮你直观对照上面的手算结果：

```python
# 示例代码：仅用于复现切片形状，不依赖 lightllm 运行时
import torch
H, IN, TP = 4096, 4096, 2
o_full = torch.randn(H, IN)          # 模拟 o_proj.weight [out=4096, in=4096]
# ColSlice: 输入维(dim=1) 切
o_rank0 = o_full[:, 0:IN//TP]        # [4096, 2048]
o_rank1 = o_full[:, IN//TP:]         # [4096, 2048]
q_full = torch.randn(H, IN)          # 模拟 q_proj.weight [out=4096, in=4096]
# RowSlice: 输出维(dim=0) 切
q_rank0 = q_full[0:H//TP, :]         # [2048, 4096]
q_rank1 = q_full[H//TP:, :]          # [2048, 4096]
print(o_rank0.shape, q_rank0.shape)  # 预期 torch.Size([4096, 2048]) torch.Size([2048, 4096])
```

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ColSliceMixin._slice_bias` 要把 bias 除以 `tp_world_size`，而 `RowSliceMixin._slice_bias` 是直接切片？
**答案**：`COLMMWeight`（o/down）是行并行——各卡算部分和后 all-reduce，若每卡都加完整 bias，求和后 bias 被放大 `tp` 倍，故每卡只加 `bias/tp`。`ROWMMWeight`（q/gate/up）是列并行——各卡输出不同列、不 reduce，bias 随输出维一起切片即可。

**练习 2**：`--tp 3` 跑一个 `num_attention_heads=32` 的模型，会在哪一步、由谁报错？
**答案**：在权重**构造期**就会失败。`ROWMMWeight` 算 q 的 `out_dim` 时 `_get_tp_dim` 会 `assert 32*128 % 3 == 0`（不成立）报错；切片器里也有同样的 `weight.shape[0] % tp == 0` 断言。根本原因是头数无法被 TP 数整除。

**练习 3**：`tie_word_embeddings=True` 时，`lm_head` 的权重会从磁盘再读一次吗？
**答案**：不会。`LMHeadWeight` 构造时传入 `embedding_weight=self.wte_weight_`，`_create_weight` 直接把 `self.weight` 指向 embedding 的权重对象（[embedding_weight.py:99-103](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/embedding_weight.py#L99-L103)），`load_hf_weights` 检测到 `_embedding_weight is not None` 直接 return（[L105-108](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/embedding_weight.py#L105-L108)），复用已加载的 embedding，省一份显存。

---

## 5. 综合实践

**任务**：给一个「假想的新模型」写一份权重拼装说明（不要求可运行），把本讲三个最小模块串起来。

假设新模型结构与 llama 几乎一致，但它有一个**额外的门控分支** `router_proj`（输入 `hidden_size`，输出 `num_experts`，且需要 all-reduce 后再 softmax）。请你完成：

1. **选元权重**：`router_proj` 应该用 `ROWMMWeight` 还是 `COLMMWeight`？为什么？
   - 提示：它需要 all-reduce 后再 softmax，说明各卡算的是部分和 → 输入维切 → 用 `COLMMWeight`。
2. **命名与挂载**：参照 [llama `_init_weight_names`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/models/llama/layer_weights/transformer_layer_weight.py#L36-L60)，写出它的 HF 权重名 `f"model.layers.{self.layer_num_}.mlp.router_proj.weight"`，并在 `_init_weight` 里 `self.router_proj = COLMMWeight(...)` 挂到 self。
3. **加载闭环**：说明你**不需要**写任何 load 代码——容器 `BaseLayerWeight` 的反射遍历会自动发现 `self.router_proj` 并加载，`verify_load` 会检查它的 `load_ok`。
4. **TP 校验**：指出如果用户用 `--tp` 使得 `num_experts % tp != 0`，会在构造期 `_get_tp_dim` 的 `assert` 处失败。

**验收标准**：你能解释清楚「选型理由（行/列并行的 all-reduce 语义）→ 命名挂载 → 自动加载校验 → TP 整除校验」这条完整链路，说明你已掌握「容器只拼装、元权重自带 load/verify/split」的设计。

---

## 6. 本讲小结

- LightLLM 权重是**两层结构**：外层 `TransformerLayerWeight`/`PreAndPostLayerWeight` 是**反射遍历器**（靠 `dir(self)` + `isinstance(BaseWeight)` 发现零件），真正的存储/切分/校验在内层**元权重**里。
- 加载流程是「**先建空壳（已按本 rank 算好这一片的形状）→ `hf_load_utils` 多线程读盘分发 → `verify_load` 逐个检查 `load_ok`**」；文件→层的映射完全靠权重名里嵌入的层号自动完成，无需索引表。
- `hf_load_utils` 优先读 `.safetensors`，多线程（`LOADWORKER`）并发，每个线程必须重新 `set_device` 防止权重串卡。
- 切分有两条路：`TpParameterWeight` 用 `narrow` 按任意维切；矩阵乘权重用 `MMWeightTpl` + 切分器，`RowSliceMixin` 切**输出维**（q/kv/gate/up，列并行）、`ColSliceMixin` 切**输入维**（o/down，行并行）。
- 判断行/列并行的硬证据：`ColSliceMixin._slice_bias` 把 bias **除以 `tp_world_size`**（all-reduce 语义），`RowSliceMixin` 直接切片。
- GQA 的 k/v 用 `repeat_times` 处理「TP 数多于 KV 头数」的复制情况；embedding/lm_head 沿词表维切并记 `tp_vocab_start_id`；归一化权重不切。
- 形状整除校验在**构造期**（`_get_tp_dim` 的 `assert`），加载完成校验在**加载后**（`verify_load` 的 `load_ok`），二者把配错 TP / 缺权重的问题尽早暴露。

---

## 7. 下一步学习建议

- 顺着 [u3-l3 推理层模板与层推理]，看这些切好的权重在**前向**里如何被使用：尤其是 `o_proj`/`down_proj` 之后那一步 all-reduce 是怎么挂在层模板上的，把「权重切分」与「计算通信」拼成闭环。
- 阅读 [u5-l2 以 Llama 为例理解完整模型实现] 与 [u5-l3 如何新增模型支持]，动手按本讲第 5 节的思路给一个新模型拼装权重。注意 `docs/.../add_new_model.md` 里展示的是**旧式手动切片写法**（`self._cuda(weights[...][start:end])`），当前代码已迁到本讲的**元权重写法**，新模型应优先用元权重。
- 若关心量化（如 AWQ / GPTQ）下的切分，可读 [mm_slicer.py 的 `Quantized*SliceMixin` 与 `AwqQuantized*SliceMixin`](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/layer_weights/meta_weights/mm_weight/mm_slicer.py#L70-L143)，以及 `MMWeightTpl` 里 `quant_method` 如何参与 `load_weight` / `create_weight`。
- MoE 的专家权重切分见 [u5-l4 MoE 模型推理]，它用的是 `FusedMoeWeight`，切分逻辑与这里的 `ROWMM/COLMM` 同源。
