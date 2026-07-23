# BaseOP 体系与 Llama 模型实现

## 1. 本讲目标

本讲打开 Mini-SGLang 的「模型层」，回答两个问题：

1. Mini-SGLang 为什么**不继承 `torch.nn.Module`**，而是自己写了一套约 100 行的 `BaseOP`？这套极简骨架怎么管理参数、怎么加载权重？
2. 一个具体的模型（Llama）是如何用 `BaseOP` 像搭积木一样，从 `embedding` → `decoder layers` → `norm` → `lm_head` 组合出来的？其中的 `GatedMLP` 和 `RopeAttn` 两个核心子模块前向到底算了什么？

学完后你应该能够：

- 说清 `BaseOP` 的 `state_dict` / `load_state_dict` 如何靠 `__dict__` 反射**递归**收集与下放权重，以及下划线前缀 `_` 的跳过规则。
- 画出 Llama 的层结构，并能解释「残差融合」（residual fusion）是如何把残差加法揉进 RMSNorm 里的。
- 讲清 `GatedMLP` 的门控前馈（SwiGLU）与 `RopeAttn` 的 QKV 合并投影 + 旋转位置编码的流程。
- 对照 `nn.Module`，列出 `BaseOP` 主动「不做」的功能，并解释为什么在**纯推理**场景下这样取舍仍然够用。

本讲是「模型实现与权重加载」单元的第一篇，只讲**模型骨架与层组合**；权重如何被流式加载、按 TP 分片、合并 qkv，留到 u8-l2。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（来自前置讲义）：

- **纯推理、无训练**：Mini-SGLang 的整个前向都在 `@torch.inference_mode()` 下运行（见 u4-l1），没有反向传播、没有优化器。这一点决定了 `BaseOP` 可以大胆砍掉 autograd 相关设施。
- **meta 建图 + 权重替换**：Engine 在初始化模型时，先用 `torch.device("meta")` 零显存搭出模型骨架，再调用 `load_state_dict` 把真实权张量**替换**进去（见 u5-l1）。本讲的 `load_state_dict` 正是这套机制在模型侧的落点。
- **全局上下文 Context**：模型的 `forward` 不接收 `input_ids` / `positions` 作为参数，而是从模块级单例 `get_global_ctx()` 里取当前 batch（见 u2-l1）。这是 Mini-SGLang 的代码风格，不是 `nn.Module` 的常规写法。
- **张量并行（TP）**：模型里的 `Linear` / `Embedding` 都是 TP 版本，权重按 rank 切分（见 u9-l1）。本讲重点关注**结构组合**，TP 切分细节留到 u9-l1。
- **RMSNorm 与残差融合**：RMSNorm 比 LayerNorm 少一次「减均值」归约；Mini-SGLang 用 flashinfer 的 `fused_add_rmsnorm` 把「加残差」与「RMSNorm」融成一个 kernel，省一次显存读写。

> 术语提示：下文「参数 / 权重」指可训练的张量（如 `weight`），「子模块」指嵌套的 `BaseOP` 对象。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `python/minisgl/layers/base.py` | 定义 `BaseOP` / `StateLessOP` / `OPList`——整个自定义模型体系的根基，替代 `nn.Module`。 |
| `python/minisgl/models/base.py` | 定义 `BaseLLMModel`（`ABC` + `BaseOP`），所有具体模型的标记基类，只要求实现 `forward`。 |
| `python/minisgl/models/llama.py` | Llama 模型：`LlamaForCausalLM` / `LlamaModel` / `LlamaDecoderLayer`，是层组合的范本。 |
| `python/minisgl/models/utils.py` | 复用的积木块：`GatedMLP`（门控前馈）、`RopeAttn`（旋转注意力）、`MoEMLP`。Llama 通过别名引用它们。 |
| `python/minisgl/models/register.py` | `_MODEL_REGISTRY` 注册表 + `get_model_class` 动态导入，按字符串名实例化模型。 |
| `python/minisgl/engine/engine.py` | Engine 里「meta 建图 → `load_state_dict`」的两行代码，是 `BaseOP` 权重接口的真实调用点。 |

依赖关系：本讲承接 u5-l2（Engine forward 与采样），是 u8-l2（配置解析与权重加载分片）和 u9（算子层与张量并行）的前置。

---

## 4. 核心概念与源码讲解

### 4.1 BaseOP / OPList / StateLessOP：自己造一个极简「模型基类」

#### 4.1.1 概念说明

PyTorch 的 `nn.Module` 是个庞然大物：它同时承担参数容器、自动求导、模块树管理、hook 机制、序列化、设备搬运、训练/推理模式切换等十几项职责。这对训练很合适，但对 Mini-SGLang 这样的**纯推理引擎**来说，其中绝大部分是「死重」：不会反向传播、不需要 `register_buffer/register_parameter`、不需要 hook、不需要 `.to/.eval`、不需要优化器迭代器。而且 `nn.Module` 每实例化一个对象都要维护 `._parameters` / `._buffers` / `._modules` 三个 `OrderedDict`——在「每张卡上建一棵含几十上百层的模型树」时是实打实的开销与复杂度。

于是 Mini-SGLang 选择**只保留「权重装载」这一项核心能力**，自己写一个约 100 行的 `BaseOP` 体系：

- `BaseOP`：带权重的算子，能递归地 `state_dict` / `load_state_dict`。
- `StateLessOP`：「无状态」算子（不持有需要加载的权重），它的 `state_dict` 永远是空。
- `OPList`：把一组同型 `BaseOP`（比如 32 个 decoder layer）打包成有序容器，键名用整数下标。

核心设计思想是：**靠反射而不是登记**。`nn.Module` 靠重写 `__setattr__` 在你写 `self.x = nn.Linear(...)` 时自动登记；`BaseOP` 完全不改 `__setattr__`，而是在 `state_dict` / `load_state_dict` 时**遍历 `self.__dict__`**，用 `isinstance` 类型判断现场识别「谁是权重、谁是子模块」，并用「下划线前缀」这一命名约定区分「权重」与「内部字段」。

#### 4.1.2 核心流程

整个体系只做一件事：**把一棵 BaseOP 树与一个扁平的 `{键: 张量}` 字典互转**。

**导出 `state_dict`**（自顶向下收集，伪代码）：

```
state_dict(node, prefix=""):
    for (name, value) in node.__dict__:
        if name 以 "_" 开头: continue        # 配置/内部字段，跳过
        if value 是 torch.Tensor:             # 叶子权重
            result[prefix + name] = value
        elif value 是 BaseOP:                 # 子树，递归，前缀加一层
            state_dict(value, prefix + name + ".")
    return result
```

**导入 `load_state_dict`**（消费式装载，伪代码）：

```
load_state_dict(node, sd, prefix="", _internal=False):
    for (name, value) in node.__dict__:
        if name 以 "_" 开头: continue
        if value 是 Tensor:
            item = sd.pop(prefix + name)      # 取出并移除
            断言 shape / dtype 一致
            setattr(node, name, item)         # 覆盖占位张量
        elif value 是 BaseOP:
            load_state_dict(value, sd, prefix + name + ".", _internal=True)
    if 不是 _internal 且 sd 非空:              # 只有最外层调用检查
        报错 "Unexpected keys: ..."
```

三个值得记住的设计点：

1. **前缀拼接用点号**：`_concat_prefix(prefix, name)` 产出 `a.b.c` 这样的层级键名，与 HuggingFace ckpt 的命名风格一致（如 `model.layers.0.self_attn.qkv_proj.weight`）。
2. **`pop` 而非读**：`load_state_dict` 用 `sd.pop(...)` **消费**字典。递归进入子模块时传 `_internal=True`，让子模块**不要**自己报「意外键」——因为字典是全树共享的，子模块看到的「剩余键」其实属于别的分支。只有最外层（`_internal=False`）在遍历完整棵树后检查剩余键，**有任何多余权重就 `raise RuntimeError`**。这是一份严格的「模型结构 ⇄ 权重文件」契约——少一个、多一个都不行。
3. **下划线前缀跳过 + 类型判断双重过滤**：决定一个属性是否进字典有两道关——先是「名字是否以 `_` 开头」（L23 / L40 的 `if name.startswith("_"): continue`），再是「是否为 `Tensor` 或 `BaseOP`」。`int` / `float` / 函数引用等非权重属性即便不带下划线也会被第二道关挡掉；而下划线前缀（如 `_layer_id`、`_comm`）则是「明确声明这是私有、不该进字典」的语义标记。

`StateLessOP` 是这套体系的「减法」：它把 `state_dict` 固定为返回空、`load_state_dict` 固定为只检查多余键，用于「逻辑上是子层、但不持有可训练权重」的部件（典型例子是 4.4 的 `AttentionLayer`）。`OPList` 则是「加法」：专门处理「N 个同型子层」的有序容器，键名用整数下标（`layers.0`、`layers.1`……）。

#### 4.1.3 源码精读

先看 `_concat_prefix` 与 `BaseOP` 本体：

[base.py:11-30](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/base.py#L11-L30)——`_concat_prefix`（L11-12）负责拼接带点号的路径，空前缀时不加多余的点。`BaseOP.state_dict`（L19-30）注意第 22 行遍历的是 `self.__dict__.items()`——**直接拿实例字典，没有任何注册表参与**；L23-24 是「下划线跳过」；L25-28 用 `isinstance` 把对象分成「Tensor 叶子」「BaseOP 子树」两类分别处理。

[base.py:32-53](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/base.py#L32-L53)——`BaseOP.load_state_dict`。L43 `state_dict.pop(...)` 是消费式装载的关键；L45 用 `assert` 校验 shape/dtype，防止悄悄错位；L46 用 `setattr(self, name, item)` **覆盖**占位张量——这正是 Engine 在 meta device 上「先建空壳、再填真权重」的落点。L52-53 在最外层（`_internal=False`）校验「没有多余 key」。

再看 `StateLessOP` 与 `OPList`：

[base.py:56-71](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/base.py#L56-L71)——`StateLessOP`：`state_dict` 恒返回空（L70-71），`load_state_dict` 只做「多余 key」检查（L60-68）。它让 `AttentionLayer` 这类部件既能挂在 BaseOP 树里（被父节点 `isinstance(param, BaseOP)` 识别并递归），又不会贡献任何权重。

[base.py:77-99](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/base.py#L77-L99)——`OPList`。它**必须重写** `state_dict` / `load_state_dict`：因为 `BaseOP` 默认逻辑只认 `Tensor` 和 `BaseOP`，而 `self.op_list` 是个 Python `list`，会被默认逻辑直接跳过。`OPList` 改成用整数下标 `0/1/2...` 作前缀逐个递归（L84-85、L95-96），这样一组 decoder layer 的键就长成 `layers.0.self_attn...`、`layers.1.self_attn...`，而 `op_list` 这个属性名本身不出现在键里。

最后看这套体系在 Engine 里的真实用法：

[engine.py:48-52](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L48-L52)——在 `torch.device("meta")` 上下文里 `create_model(...)`，建出来的所有张量都是 **meta tensor（零显存的占位）**；紧接着一句 `self.model.load_state_dict(self._load_weight_state_dict(config))`，用 `setattr` 把真权重逐个覆盖上去。这两行是 u5-l1 讲过的「低内存建图」的微观实现，而它能 work 的全部前提就是 `BaseOP` 提供了这个递归 `load_state_dict`。

#### 4.1.4 代码实践

**实践目标**：对照 `nn.Module`，列出 `BaseOP` 主动放弃的功能，并解释为何在纯推理场景下仍然够用（本讲规格指定的实践任务）。

**操作步骤**：

1. 打开 [base.py:15-53](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/base.py#L15-L53)，把它和 `torch.nn.Module` 的方法清单做对比。
2. 按下表逐项判断「BaseOP 是否实现」，并写下「推理场景下为什么可以不做」。

参考对照表（请你自行核对补全）：

| `nn.Module` 能力 | BaseOP 是否有 | 推理场景下是否需要 | 理由 |
| --- | --- | --- | --- |
| autograd 反向传播 | ❌ 无 | 不需要 | 全程 `inference_mode`，不需要计算图 |
| `parameters()` / `named_parameters()` | ❌ 无 | 不需要 | 权重通过 `state_dict` 整体装载，无需逐个遍历给优化器 |
| `register_parameter` / `nn.Parameter` | ❌ 无 | 不需要 | 直接用裸 `torch.Tensor`，权重加载一次后不再更新 |
| `register_buffer` | ❌ 无 | 不需要 | buffer（如 RoPE 的 cos/sin）直接存普通属性即可 |
| `.to/.cuda/.half` 设备迁移 | ❌ 无 | 不需要 | 建图时即落在目标 device（meta→真实），见 engine.py:50-52 |
| `.train()/.eval()` 模式开关 | ❌ 无 | 不需要 | 只有 eval 一种模式，无 dropout/batchnorm 训练期行为 |
| forward / backward hook | ❌ 无 | 不需要 | 纯前向，无诊断 hook 需求 |
| `__setattr__` 自动登记子模块 | ❌ 无 | 够用 | 改靠 `__dict__` + `isinstance` 反射，效果等价 |
| 识别 `list` 中的子模块 | `nn.ModuleList` | `OPList` 手动包一层 | 同理 |
| `state_dict` / `load_state_dict` | ✅ **有** | **需要** | 装载权重的唯一通路 |

3. **关键观察**：`BaseOP` 用「类型反射」识别子模块，副作用是裸 `list` 不被识别（必须包进 `OPList`），且 `int` / `float` / 函数引用等非权重属性被静默忽略——这其实正好满足「只挑出 `Tensor`」的需求。

**需要观察的现象**：你会确认 `BaseOP` 只剩下 `forward` + `state_dict` + `load_state_dict` 三个能力，恰好覆盖「搭结构 + 装权重」这一最小推理需求集。

**预期结果**：得到一张说明「砍掉的都是训练/迁移相关、推理用不到」的对照表。

> 待本地验证：可用 `grep` 在 `python/minisgl/models/` 与 `layers/` 下搜索 `autograd`、`backward`、`register_buffer`、`.train()` 等关键词，确认模型层确实没有训练路径调用它们（注意区分「模型层」与「kernel/通信」层，后者可能用 `.to()` 做 tensor 搬运，那不属于 `nn.Module` 能力）。

#### 4.1.5 小练习与答案

**练习 1**：如果给某个 `BaseOP` 子类加一个 `self._comm = DistributedCommunicator()`，它会不会被 `state_dict` 收录？为什么？

> **答案**：不会。名字以 `_` 开头，在 L23 / L40 的 `if name.startswith("_"): continue` 处被跳过。这也是为什么 `LinearOProj`、`VocabParallelEmbedding` 都把通信器命名为 `self._comm`。

**练习 2**：`load_state_dict` 为什么用 `pop` 而不是 `get`？如果改成 `get` 会丢掉什么校验？

> **答案**：`pop` 会从字典里删掉已消费的键。遍历完整棵树后，字典里剩下的就是「模型里找不到对应位置」的意外键，最外层据此报错（L52-53）。改用 `get` 后键不会被删除，就无法检测「权重字典多出了模型不需要的键」——推理场景下这种不匹配通常意味着换模型忘改配置，早 fail 比默默用错权重安全。

**练习 3**：`OPList` 的实例属性 `self.op_list`（无下划线）为什么没有出现在最终的 state_dict 键名里（比如没有 `layers.op_list.0.weight`）？

> **答案**：因为 `OPList` **重写**了 `state_dict` / `load_state_dict`，直接遍历 `self.op_list` 列表并用整数下标 `i` 作为键名（L84-85、L95-96），不再走 `BaseOP` 那套「遍历 `__dict__`」的逻辑，所以 `op_list` 这个属性名本身被绕过了，子层直接以 `layers.0`、`layers.1` 出现。

---

### 4.2 LlamaForCausalLM 与层结构（含残差融合）

#### 4.2.1 概念说明

有了 `BaseOP` 这个积木底座，Llama 模型就是经典的 decoder-only「四大件」组合：

```
LlamaForCausalLM
├── model: LlamaModel
│   ├── embed_tokens: VocabParallelEmbedding   # token id → 隐状态
│   ├── layers: OPList[LlamaDecoderLayer × N]   # N 层 Transformer 解码层
│   └── norm: RMSNormFused                      # 最终归一化
└── lm_head: ParallelLMHead                     # 隐状态 → 词表 logits
```

数据流（前向）是一条直线：

```
input_ids ──embed_tokens──▶ h
            ┌──────── layers (循环 N 层) ────────┐
            ▼                                     │
        LlamaDecoderLayer（自注意力 + MLP + 残差融合）│
            └─────────────────────────────────────┘
                              ▼
                        norm（最终归一化）
                              ▼
                         lm_head → logits
```

这里有两个 Mini-SGLang 特有的设计选择：

1. **积木复用**：`llama.py` 里 `LlamaAttn` 其实就是 `utils.RopeAttn`，`LlamaMLP` 就是 `utils.GatedMLP`（见 [llama.py:10-12](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/llama.py#L10-L12)）。qwen2/qwen3/mistral 也复用同一批积木，只是换了组合方式或个别开关。这就是为什么 `utils.py` 被放在 `models/` 而不是 `layers/`——它是「模型级」的复用层。
2. **forward 不收参数**：`LlamaForCausalLM.forward(self)` 没有任何入参，它直接从全局上下文 `get_global_ctx().batch.input_ids` 取输入。这让 Engine 调用模型时只需 `self.model.forward()`，无需把 batch 一路透传。

还有个贯穿全模型的关键设计——**残差融合（residual fusion）**：Transformer 每层都有 `x = x + sublayer(x)` 的残差加法。Mini-SGLang 不单独做这个加法 kernel，而是把它**揉进 RMSNorm** 里（用 flashinfer 的 `fused_add_rmsnorm`，一次 kernel 同时算「相加 + 归一化」）。为了让这种「延后相加」成立，残差必须作为一个独立变量在层与层之间传递。

#### 4.2.2 核心流程

一个 batch 的前向（伪代码）：

```
LlamaForCausalLM.forward():
    input_ids = get_global_ctx().batch.input_ids     # 从全局上下文取
    x = model.embed_tokens.forward(input_ids)        # token → hidden
    residual = None
    for layer in model.layers.op_list:               # 串行过 N 层
        x, residual = layer.forward(x, residual)     # 残差跨层传递
    x = model.norm.forward(x, residual)[0]           # 最后一层 norm 收尾残差
    logits = lm_head.forward(x)                      # hidden → vocab logits
    return logits
```

每层 `LlamaDecoderLayer.forward` 的内部节奏（残差融合的关键所在）：

```
Layer.forward(x, residual):
    x, residual = input_layernorm.forward(x, residual)        # 融合：norm(x+residual)
    x = self_attn.forward(x)                                  # 注意力子层
    x, residual = post_attention_layernorm.forward(x, residual)  # 再融合一次
    x = mlp.forward(x)                                        # MLP 子层
    return x, residual                                        # 残差继续传给下一层
```

从数学上看，标准 pre-norm Transformer 一层等价于：

\[
h = x + \mathrm{Attn}(\mathrm{norm}_1(x)), \qquad
\mathrm{out} = h + \mathrm{MLP}(\mathrm{norm}_2(h))
\]

Mini-SGLang 把其中两次「`+`」分别推迟到 `post_attention_layernorm` 与**下一层的 `input_layernorm`** 里去做（最后一层的相加由 `LlamaModel` 末尾的 `norm` 收尾）。这样残差加法与 RMSNorm 共用一个 kernel，省掉独立的 add 开销，N 层只需 N 次显存读写来完成「加残差」。

#### 4.2.3 源码精读

先看顶层 `LlamaForCausalLM`：

[llama.py:68-82](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/llama.py#L68-L82)——`__init__` 组装 `self.model`（`LlamaModel`）与 `self.lm_head`（`ParallelLMHead`）。注意 L74-75：`lm_head` 接收 `tie_word_embeddings` 开关与 `tied_embedding`——当模型采用「词嵌入与输出头权重绑定」（weight tying，`tie_word_embeddings=True`）时，`lm_head` 直接复用 `embed_tokens` 的权重，不再单独存一份，省一半词表显存。`forward`（L79-82）不接收参数，而是 `get_global_ctx().batch.input_ids` 取输入，跑 `model.forward` 得到隐状态，再过 `lm_head` 出 logits。注意 L77 的 `super().__init__()` 放在最后——`BaseLLMModel` / `BaseOP` 的 `__init__` 实际是空的（`object.__init__`），真正「建图」是靠前面几行往 `self.__dict__` 里塞属性完成的。

再看 `LlamaModel`：

[llama.py:46-65](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe75daee49998176275667eb58f2/python/minisgl/models/llama.py#L46-L65)——`__init__` 把 N 层 `LlamaDecoderLayer` 塞进 `OPList`（L52-54），这正是 4.1 里 `OPList` 的用武之地。`forward`（L60-65）先用 `embed_tokens` 取 embedding，然后 `for layer in self.layers.op_list:` 手动循环（注意是直接遍历 `.op_list`，因为 `OPList` 没有自己的 `forward`），每层接收并回传 `(x, residual)`，最后 `self.norm.forward(x, residual)[0]` 用末尾 norm 收尾残差，`[0]` 取归一化结果、舍掉更新后的 `residual`（模型出口不再需要残差流）。

最关键的是 `LlamaDecoderLayer`：

[llama.py:18-43](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/llama.py#L18-L43)——两个别名：`self.self_attn = LlamaAttn` 其实是 `utils.RopeAttn`，`self.mlp = LlamaMLP` 其实是 `utils.GatedMLP`（见 [llama.py:10-12](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/llama.py#L10-L12)）。`self._layer_id = layer_id`（L31）用**下划线前缀**，是为了配合 `@nvtx_annotate("Layer_{}", layer_id_field="_layer_id")` 打 NVTX 标签但不进 `state_dict`。`forward`（L34-43）正是 4.2.2 描述的残差融合流程：两次 `RMSNormFused.forward(x, residual)`，中间夹 `self_attn` 与 `mlp`。

补充：`RMSNormFused` 的残差融合实现在 [norm.py:23-38](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/norm.py#L23-L38)——`residual is None` 时退化为普通 rmsnorm 并把 `x` 作为初始残差返回（L35-36）；否则调 flashinfer 的 `fused_add_rmsnorm(x, residual, ...)` 原地完成「相加 + 归一化」（L37-38）。

最后看模型如何被「按名实例化」：

[models/base.py:12-14](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/base.py#L12-L14)——`BaseLLMModel(ABC, BaseOP)`：多重继承，既是抽象基类（`ABC`，强制子类实现 `forward`）又是 `BaseOP`（获得 `state_dict`/`load_state_dict`），只声明一个 `@abstractmethod forward`。

[register.py:5-21](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/register.py#L5-L21)——`_MODEL_REGISTRY` 把 HF 的架构名（如 `"LlamaForCausalLM"`、`"Qwen3ForCausalLM"`）映射到 `(模块路径, 类名)`；`get_model_class` 用 `importlib.import_module(..., package=__package__)` **动态导入**对应模块并 `getattr` 取类、立即实例化（L18-21）。L19 的 `package=__package__` 让 `.llama` 这种相对路径解析为 `minisgl.models.llama`。这套机制让「新增一个模型」只需写一个 `models/xxx.py` + 在注册表加一行（详见 u10-l3）。`create_model`（[models/__init__.py:7-8](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/__init__.py#L7-L8)）取 `model_config.architectures[0]` 作 key 调用它。

#### 4.2.4 代码实践

**实践目标**：跟踪残差变量 `residual` 在多层之间的流转，确认「最后一层的 mlp 残差加法由谁完成」，并手推一个 2 层 Llama 的 state_dict 键名。

**操作步骤**：

1. 打开 [llama.py:60-65](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/llama.py#L60-L65)。假设 `num_layers = 3`，在纸上列出每一层 `forward` 返回的 `(x, residual)` 语义：
   - 第 0 层入口：`input_layernorm(embed, None)` → 残差初始化为 `embed`。
   - 第 0 层出口：`x = mlp(...)`，`residual = embed + attn(...)`（mlp 还没加进残差）。
   - 第 1 层入口：`input_layernorm(x, residual)` → 把第 0 层的 mlp 加进残差。
   - ……
   - 第 2 层（最后）出口：`x = mlp(...)`，其残差加法**尚未发生**。
2. 回答：最后一层 mlp 的残差加法在哪里完成？
3. 额外：按「`LlamaForCausalLM` → `self.model` → `self.lm_head`」的装配关系（假设 `tie_word_embeddings=False`、无 qk_norm），手写推导一个 2 层 Llama 的全部 state_dict 键名。

**需要观察的现象**：

- 每一层都「欠」一次 mlp 残差加法，由下一层开头补上；最后一层没有「下一层」。
- 手写键名应出现形如 `model.embed_tokens.weight`、`model.layers.0.self_attn.qkv_proj.weight`、`model.layers.0.mlp.down_proj.weight`、`model.norm.weight`、`lm_head.weight`。`layers.0` / `layers.1` 来自 `OPList` 的整数下标；`model.` 前缀来自属性名 `self.model`。

**预期结果**：最后一层的 mlp 残差加法由 `LlamaModel.forward` 末尾的 `self.norm.forward(x, residual)` 完成——末尾 `RMSNormFused` 在 `residual is not None` 分支里做 `fused_add_rmsnorm`，等价于先 `x + residual` 再归一化。手写键名的层级与命名风格与 HF ckpt 一致（具体投影矩阵的合并/拆分差异留到 u8-l2）。

> 待本地验证：是否与某具体 HF ckpt 的键名一一对应，取决于模型；本实践只验证「层级结构」正确。

#### 4.2.5 小练习与答案

**练习 1**：`LlamaModel.forward` 里为什么是 `for layer in self.layers.op_list` 而不是 `for layer in self.layers`？

> **答案**：`OPList` 只是个容器，没有实现 `__iter__` 也没有 `forward`；真实的层列表存在它的 `self.op_list` 属性里，所以要显式取 `.op_list`。

**练习 2**：`LlamaModel.forward` 末尾 `return self.norm.forward(x, residual)[0]` 里的 `[0]` 舍掉了什么？为什么可以舍掉？

> **答案**：`RMSNormFused.forward` 返回 `(x, residual)` 元组；`[0]` 取归一化后的 `x`，舍掉更新后的 `residual`。可以舍掉是因为这是最后一层 norm，后面直接进 `lm_head`，不再需要残差流。残差只在层间传递，到模型出口就结束。

**练习 3**：`tie_word_embeddings=True` 时，`lm_head` 的权重会重复出现在 state_dict 里吗？

> **答案**：不会。`tie_word_embeddings=True` 时 `lm_head` 传入 `tied_embedding=self.model.embed_tokens`（同一对象引用）。`ParallelLMHead` 在权重绑定时重写了 `state_dict`/`load_state_dict`（见 [embedding.py:59-85](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/embedding.py#L59-L85)），让它不贡献自己的权重，词表权重只以 `model.embed_tokens.weight` 出现一次。这正是 weight tying 省显存的原理——一份权重两处用。

---

### 4.3 GatedMLP 门控前馈网络

#### 4.3.1 概念说明

`GatedMLP` 是 Llama 的 SwiGLU 前馈子层。它解决的问题是：用一个「门控」结构让 MLP 更有表达力——把中间隐状态拆成「门（gate）」和「上（up）」两路，用门对上做逐元素调制：

\[
\text{gate},\, \text{up} = W_{\text{gate}}\, x,\; W_{\text{up}}\, x
\]
\[
y = \big(\mathrm{SiLU}(\text{gate}) \odot \text{up}\big), \qquad
\text{out} = W_{\downarrow}\, y
\]

其中 \(\mathrm{SiLU}(z) = z \cdot \sigma(z)\)，\(\odot\) 是逐元素乘。门控让网络能在「该激活」「该抑制」之间动态选择，表达能力比固定激活强；代价是参数量翻倍（gate 和 up 各一份），所以 Llama 配置里 `intermediate_size` 通常约等于 \(\tfrac{8}{3}\times\text{hidden\_size}\) 再对齐到 256 的倍数。

为了省一次矩阵乘的 kernel 启动开销，Mini-SGLang 把 \(W_{\text{gate}}\) 和 \(W_{\text{up}}\) **合并**成一个大矩阵 `gate_up_proj`（输出维度 = `2 × intermediate_size`），一次乘法同时算出两路，再用 `silu_and_mul` / `gelu_and_mul` 把结果沿最后一维切两半并做门控乘法。这与张量并行配合得当：合并投影走 column-parallel（按输出维切，无需通信），收尾的 `down_proj` 走 row-parallel（按输入维切，并在出口 `all_reduce`）——TP 细节见 u9-l1。

#### 4.3.2 核心流程

```
GatedMLP.forward(x):                              # x: [num_tokens, hidden_size]
    gate_up = gate_up_proj.forward(x)             # 一次合并 GEMM → [num_tokens, 2*intermediate]
    del x                                          # 立即释放输入，省显存
    y = act_fn(gate_up)                            # silu_and_mul: 劈半 + SiLU(gate)*up → [num_tokens, intermediate]
    del gate_up                                    # 立即释放中间
    return down_proj.forward(y)                    # 行并行 GEMM + all_reduce → [num_tokens, hidden_size]
```

`gate_up_proj` 用 `LinearColParallelMerged`，构造时传入**两个** `intermediate_size`（gate 一份、up 一份），表示「把这两个输出合并」。中间穿插的 `del x` / `del gate_up` 是 Mini-SGLang 贯穿模型层的**显存卫生习惯**：显式 `del` 能让中间结果在进入下一阶段前尽快归还显存，对大 batch / 长序列非常关键。

#### 4.3.3 源码精读

[utils.py:25-50](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/utils.py#L25-L50)——`GatedMLP` 全貌。逐段看：

- L27-31：`gate_up_proj = LinearColParallelMerged(hidden_size, [intermediate, intermediate], has_bias=False)`。输出维度传的是**列表** `[intermediate_size, intermediate_size]`（[linear.py:56-68](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/linear.py#L56-L68) 会把列表里每个 size 各自除以 tp_size 再求和，得到本卡的合并输出维度）——这正是「gate 和 up 两路、各自 TP 切分、再拼一起」的实现。
- L33-37：用一张 `FN_MAP` 把 `config.hidden_act`（`"silu"` 或 `"gelu"`）映射到 `silu_and_mul` / `gelu_and_mul`，遇到不支持的激活**直接抛错**而非静默回退。这两个激活函数实现在 [activation.py:9-18](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/activation.py#L9-L18)，都是 flashinfer 的 fused 实现（把「SiLU + 逐元素乘」融成一个 kernel）。
- L38-42：`down_proj = LinearRowParallel(intermediate→hidden)`，出口线性层，TP>1 时 `all_reduce`（[linear.py:109-127](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/linear.py#L109-L127)）。
- L44-50 的 `forward`：严格按 4.3.2 的流程，`@nvtx_annotate("MLP")` 给 Nsight profiler 打标签，两个 `del` 是显存卫生。

> 顺带一提：同文件的 [utils.py:53-76](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/utils.py#L53-L76) 是 `MoEMLP`（混合专家 MLP），结构与 `GatedMLP` 对偶：把「单一 gate_up/down」换成 `MoELayer`（N 个专家）+ 一个 `LinearReplicated` 路由（gate）。它服务于 Qwen3-MoE 等模型，本讲不展开，详见 u10-l1。

#### 4.3.4 代码实践

**实践目标**：确认 `gate_up_proj` 的合并输出形状，并手算 `silu_and_mul` 的「劈半 + 门控」语义。

**操作步骤**（纸笔计算 + 可选验证）：

1. 设 `hidden_size=1024`、`intermediate_size=2816`、`num_tokens=4`、`tp_size=1`。推算 `gate_up_proj(x)` 的输出形状：`[4, 2×2816] = [4, 5632]`；`act_fn(gate_up)` 输出形状 `[4, 2816]`；`down_proj(y)` 输出形状 `[4, 1024]`。
2. 设 `intermediate_size = 4`，假设某次前向对一个 token 输出 8 维向量（gate 4 维 + up 4 维拼接）：
   - `gate = [1.0, -1.0, 2.0, 0.0]`
   - `up   = [2.0,  3.0, 0.5, 1.0]`
3. 用 SiLU 定义 \(\mathrm{SiLU}(z)=z\sigma(z)\) 手算 `silu_and_mul` 的输出（4 维）。
4. （可选）若环境装了 flashinfer，可写两行代码验证：`from minisgl.layers import silu_and_mul`，喂一个 `(1,8)` 张量，看输出是否与手算一致。

**需要观察的现象**：`gate` 为负的位置（如 -1.0）经 SiLU 后会得到一个接近 0 的负数，再乘 `up` 后对输出贡献很小——这就是「门控抑制」。维度先翻倍（合并 gate/up）、再减半（门控乘法）、再回到 hidden。

**预期结果**：

- 维度链：`[4,1024] → [4,5632] → [4,2816] → [4,1024]`。
- \(\mathrm{SiLU}(1.0) = 1.0 \cdot \sigma(1.0) \approx 1.0 \cdot 0.731 = 0.731\)，乘 `up=2.0` → \(\approx 1.462\)。
- \(\mathrm{SiLU}(-1.0) = -1.0 \cdot \sigma(-1.0) \approx -1.0 \cdot 0.269 = -0.269\)，乘 `up=3.0` → \(\approx -0.807\)（被「门」大幅压低）。

> 待本地验证：第 4 步具体数值取决于 kernel 实现（是否 fused、精度），以 kernel 实际输出为准；手算用于确认语义。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `gate_up_proj` 用「列并行」而 `down_proj` 用「行并行」？

> **答案**：列并行切输出维——各卡独立算出自己那段 gate/up，无需通信即可进入 `silu_and_mul`（激活是逐元素、逐通道的，各卡处理自己的通道互不干扰）。行并行切输入维——`down_proj` 各卡算部分和，只有把所有部分和加起来才得到完整结果，所以需要一次 `all_reduce`。一个 MLP 全程只需一次通信。若反过来：`gate_up` 行并行会导致各卡拿到「部分激活」，门控语义错误。

**练习 2**：`GatedMLP.forward` 里的 `del x` / `del gate_up` 起什么作用？少写一个会出错吗？

> **答案**：不会出错，只是少回收一点显存。`del` 是显存卫生优化、不是正确性要求；模型 forward 是一个大函数，中间变量不会立刻离开作用域，显式 `del` 能让显存更早归还，降低峰值占用。「用完即删」的顺序（`del x` → 用 `gate_up` 算 `y` → `del gate_up`）是自然的。

**练习 3**：`config.hidden_act` 既可能是 `"silu"` 也可能是 `"gelu"`。如果传入 `"relu"` 会发生什么？

> **答案**：`FN_MAP.get("relu")` 返回 `None`，构造函数在 [utils.py:35-36](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/utils.py#L35-L36) 直接 `raise ValueError`。模型不会静默用错激活——这是「显式优于隐式」的设计。

---

### 4.4 RopeAttn 旋转位置注意力

#### 4.4.1 概念说明

`RopeAttn` 是 Llama 的自注意力子层，由四部分串起来：

1. **`qkv_proj`（`LinearQKVMerged`）**：把 `hidden_size` 的输入一次性投影成 Q、K、V 三路。GQA（Grouped-Query Attention）下 K/V 的头数比 Q 少（`num_kv_heads < num_qo_heads`），所以三路维度不同。
2. **可选的 `q_norm` / `k_norm`（`RMSNorm`）**：Qwen3 等模型对 Q、K 也做一层 RMSNorm（Per-Head Normalization），Llama 默认关闭。
3. **`attn`（`AttentionLayer`）**：切分 QKV、施加 RoPE 旋转位置编码、调用注意力后端算注意力。
4. **`o_proj`（`LinearOProj`）**：把注意力输出投回 `hidden_size`。

前向数据流：

\[
Q, K, V = W_{\text{qkv}}\, x, \qquad Q, K \leftarrow \mathrm{RoPE}(Q, K, \text{pos}), \qquad O = \mathrm{Attention}(Q, K, V), \qquad \text{out} = W_o\, O
\]

RoPE 通过对 query/key 向量按位置做二维旋转来注入位置信息，核心运算是：

\[
\text{Attn}(Q,K,V) = \mathrm{softmax}\!\left(\frac{QK^{\top}}{\sqrt{d_k}}\right) V
\]

RoPE 的「旋转」作用于 head_dim 内的二维子空间。关键性质：两个 token 的 Q·K 内积只依赖它们的**相对位置**，所以 RoPE 天然编码相对位置，无需显式 position embedding，且**只作用在 q/k 上**（V 不参与注意力分数计算，给它加位置无意义）。

> 回扣 4.1：`AttentionLayer` 继承 `StateLessOP`——它自己不持有可加载权重（`rotary` 的参数是按公式算的、不是学出来的），它持有的 `q_norm`/`k_norm` 引用归 `RopeAttn` 所有，所以用 `StateLessOP` 避免重复序列化。此外 `RopeAttn` 自己不实现 softmax/attention kernel，而是把 Q/K/V 交给 `attn_backend`（FlashInfer / FA 等，见 u7），实现「位置编码」与「注意力后端」解耦，换后端不用动模型代码。

#### 4.4.2 核心流程

```
RopeAttn.forward(x):
    qkv = qkv_proj.forward(x)               # 一次合并 GEMM 出 [Q | K | V] 拼接
    del x
    o = attn.forward(qkv)                    # AttentionLayer 内部:
                                             #   split qkv -> q, k, v
                                             #   (可选) q_norm/k_norm 原地归一化
                                             #   RoPE(q, k, positions)
                                             #   attn_backend.forward(...)  -> paged attention
    return o_proj.forward(o)                 # 输出投影 (行并行 + all_reduce)
```

`AttentionLayer.forward` 把合并的 `qkv` 沿最后一维切成三段 `[Q | K | V]`，切分点由本地（按 TP 切分后的）头数决定：

\[
\text{qo\_dim} = n_{qo}\cdot d_{head}, \qquad \text{kv\_dim} = n_{kv}\cdot d_{head}
\]

其中 \(n_{qo}\)、\(n_{kv}\) 是**本 rank** 的头数（`div_even` 按 TP 切分，GQA 下 K/V 头数不足时允许复制，见 u5-l1 / u9-l1）。切分必须与 `qkv_proj` 的本地输出布局 `[Q | K | V]` 一致。

#### 4.4.3 源码精读

[utils.py:79-123](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/utils.py#L79-L123)——`RopeAttn` 全貌。`__init__`（L80-116）：

- L89-95：`qkv_proj = LinearQKVMerged(...)`，传入 `num_qo_heads`、`num_kv_heads`、`head_dim`，由 Linear 内部按 TP 切分与 GQA 复制处理（[linear.py:71-88](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/linear.py#L71-L88)）。
- L96-102：`has_qk_norm` 开关。开启时建两个 `RMSNorm(head_dim, ...)`（注意是按 **head_dim** 归一化，不是 hidden_size！），关闭时置 `None`。
- L103-111：`attn = AttentionLayer(...)`，把 `layer_id`、头数、`rotary_config`、以及上面两个 norm 一并传入。`AttentionLayer` 持有这些只是「为了 forward 时用」，**不产生权重**。
- L112-116：`o_proj = LinearOProj(head_dim*num_qo_heads, hidden_size, has_bias=False)`，出口投影，row-parallel。

`forward`（L118-123）：标准三步 `qkv_proj → attn → o_proj`，`@nvtx_annotate("MHA")` 打标签。

再看 `AttentionLayer` 如何把「全局 TP 头数」转成「本卡头数」并完成前向：

[attention.py:18-45](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/attention.py#L18-L45)——`__init__`：L32 取 `tp_size`；L33 `num_qo_heads = div_even(num_qo_heads, tp_size)`（Q 头必须能整除，不能复制）；L34 `num_kv_heads = div_even(num_kv_heads, tp_size, allow_replicate=True)`（KV 头允许复制，解决 GQA 头数少于卡数的问题）；L35-36 据此算出本卡的 `qo_attn_dim` / `kv_attn_dim`。L37-43 用 `rotary_config` 建一个 `rotary` 算子（RoPE 的 cos/sin 表）。

[attention.py:47-57](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/attention.py#L47-L57)——`forward` 是 4.4.2 伪代码的真实落点：

- L49 `qkv.split([self.qo_attn_dim, self.kv_attn_dim, self.kv_attn_dim], dim=-1)`：按本卡维度切成 Q、K、V 三段。切分点必须与 `qkv_proj` 的本地输出布局 `[Q | K | V]` 一致。
- L50-53：可选地对 Q、K 做 per-head RMSNorm（先 `view(-1, num_heads, head_dim)` 再 norm）。
- L54 `self.rotary.forward(ctx.batch.positions, q, k)`：从全局上下文取**每个 token 的位置**，对 q/k 施加 RoPE。
- L56 `ctx.attn_backend.forward(q, k, v, self.layer_id, ctx.batch)`：交给注意力后端算 paged attention（KV 来自池，见 u6），结果 reshape 回 `qo_attn_dim`。

#### 4.4.4 代码实践

**实践目标**：弄清 QKV 切分维度如何由头数决定，以及 `q_norm`/`k_norm` 开关如何改变 `RopeAttn` 的 state_dict。

**操作步骤**（源码阅读型 + 推算）：

1. 假设 `num_qo_heads=32`、`num_kv_heads=8`（GQA）、`head_dim=128`、`tp_size=1`。推算 `qo_attn_dim = 32×128 = 4096`，`kv_attn_dim = 8×128 = 1024`；合并 `qkv` 最后一维 = `4096 + 2×1024 = 6144`，对应 `LinearQKVMerged` 的 `local_osize`。切分点 `[4096, 1024, 1024]` 把 6144 维精确分成 Q/K/V 三段。
2. 打开 [utils.py:79-123](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/utils.py#L79-L123)，分别假设 `has_qk_norm=False` 与 `has_qk_norm=True`，写出 `RopeAttn` 一个实例的 state_dict 键名（结合 4.1 的规则）。
3. 打开 [norm.py:8-20](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/norm.py#L8-L20)，确认 `RMSNorm` 的 `self.weight = torch.empty(size)` 里 `size` 是什么——在 `RopeAttn` 里它是 `head_dim`（[utils.py:98-99](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/utils.py#L98-L99)），而 `LlamaDecoderLayer` 里的 `input_layernorm` 是 `hidden_size`（[llama.py:22-29](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/llama.py#L22-L29)）。理解「同一个 RMSNorm 类，归一化维度可变」。

**需要观察的现象**：

- `has_qk_norm=False`：键名只有 `qkv_proj.weight`、`o_proj.weight`。
- `has_qk_norm=True`：多出 `q_norm.weight`、`k_norm.weight`，且 shape 是 `(head_dim,)` 而非 `(hidden_size,)`。
- `attn` 子对象（`AttentionLayer`，`StateLessOP`）**不贡献任何键**，即使它内部引用了 `q_norm`/`k_norm`——因为同一对象已在 `RopeAttn` 的 `self.q_norm`/`self.k_norm` 下被收集过一次，而 `AttentionLayer` 是无状态的（不会重复收集）。这同时印证了 4.1 里 `StateLessOP` 的作用。

**预期结果**：能写出两种开关下的键名差异，并解释为什么 `attn.q_norm` 不会导致 `q_norm.weight` 重复出现。Llama 默认 `has_qk_norm=False`（[llama.py:20](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/llama.py#L20) 用默认参数 `LlamaAttn(config, layer_id)`），Qwen3 会显式传 `True`（见 u10-l3）。

> 待本地验证：可在 CPU 上构造一个 `RopeAttn(config, 0, has_qk_norm=True)`（需 mock 一个 `ModelConfig` 与 TP 上下文），打印 `state_dict()` 键名核对。

#### 4.4.5 小练习与答案

**练习 1**：`AttentionLayer` 持有 `self.q_norm`（指向 `RopeAttn` 传入的 RMSNorm），而 `RopeAttn` 也持有 `self.q_norm` 指向**同一个对象**。state_dict 里 `q_norm.weight` 会出现几次？

> **答案**：只出现一次，键名为 `q_norm.weight`（在 `RopeAttn` 下）。原因是 `AttentionLayer` 继承 `StateLessOP`，它的 `state_dict` 恒返回空、`load_state_dict` 不递归——所以它「看不见」自己引用的 `q_norm`。权重唯一的所有者是 `RopeAttn`。这种「同一对象两处引用、但只有一个所有者负责序列化」正是 `StateLessOP` 的价值。

**练习 2**：`qkv.split([qo_attn_dim, kv_attn_dim, kv_attn_dim], dim=-1)` 的切分点由谁决定？为什么不能用 `num_qo_heads` / `num_kv_heads`（全局头数）？

> **答案**：由**本 rank** 的 `num_qo_heads` / `num_kv_heads`（`div_even` 切分后的本地头数）决定。因为 `qkv_proj` 也是按本地头数切分输出的，张量最后一维已经是本地维度，切分点必须用本地维度才能对齐。

**练习 3**：`RopeAttn.forward` 里 RoPE 作用在 q 和 k 上，为什么**不**作用在 v 上？`o_proj` 为什么用 `LinearOProj`（row-parallel）而不是 column-parallel？

> **答案**：RoPE 的目的是让 `Q·K^T` 只依赖相对位置，位置信息通过 q、k 的旋转「对消」进内积里；V 不参与注意力分数的计算（只在内积之后被加权求和），给它加位置信息没有数学意义。至于 `o_proj`：注意力输出按 Q 头分布在各 rank 上，`o_proj` 把每个 rank 负责的那部分头投回 `hidden_size` 后，需要把所有 rank 的部分和**加起来**才是完整输出——这正是 row-parallel 的 `all_reduce` 语义；column-parallel 不会做这个求和。

---

## 5. 综合实践：手搓一个迷你 Llama 结构，验证 BaseOP 的键生成与权重替换

本任务把本讲四个模块串起来：用 `BaseOP` / `OPList` 搭一个**形状像 Llama** 的迷你模型，验证两件事——(a) `state_dict` 生成的键路径是否符合 `model.layers.0.xxx` 的预期；(b) 在 meta device 上建图后，`load_state_dict` 能否把 meta 张量替换成真实张量并严格校验键集合。这也直接对应规格里「对照 nn.Module，解释 BaseOP 为何够用」的实践任务——你会亲眼看到 `state_dict`/`load_state_dict` 这两个能力是如何独立于任何 `nn.Module` 机制工作的。

> 以下为**示例代码**（非项目原有代码）。它直接复刻本讲引用的 [base.py:15-99](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/base.py#L15-L99) 三个类，**只依赖 `torch`**，可在纯 CPU 环境（无需 flashinfer/GPU）运行：

```python
# 示例代码：复刻 BaseOP 体系，验证键生成与 meta→真实权重替换
import torch

def _concat_prefix(prefix, name):
    return f"{prefix}.{name}" if prefix else name

class BaseOP:
    def forward(self, *a, **k): ...
    def state_dict(self, *, prefix="", result=None):
        result = result if result is not None else {}
        for name, param in self.__dict__.items():
            if name.startswith("_"):
                continue
            if isinstance(param, torch.Tensor):
                result[_concat_prefix(prefix, name)] = param
            elif isinstance(param, BaseOP):
                param.state_dict(prefix=_concat_prefix(prefix, name), result=result)
        return result
    def load_state_dict(self, sd, *, prefix="", _internal=False):
        for name, param in self.__dict__.items():
            if name.startswith("_"):
                continue
            if isinstance(param, torch.Tensor):
                item = sd.pop(_concat_prefix(prefix, name))
                assert param.shape == item.shape and param.dtype == item.dtype
                setattr(self, name, item)
            elif isinstance(param, BaseOP):
                param.load_state_dict(sd, prefix=_concat_prefix(prefix, name), _internal=True)
        if not _internal and sd:
            raise RuntimeError(f"Unexpected keys: {list(sd.keys())}")

class StateLessOP(BaseOP):
    def state_dict(self, *, prefix="", result=None):
        return result if result is not None else {}
    def load_state_dict(self, sd, *, prefix="", _internal=False):
        if not _internal and sd:
            raise RuntimeError(f"Unexpected keys: {list(sd.keys())}")

class OPList(BaseOP):
    def __init__(self, ops):
        self.op_list = ops
    def state_dict(self, *, prefix="", result=None):
        result = result if result is not None else {}
        for i, op in enumerate(self.op_list):
            op.state_dict(prefix=_concat_prefix(prefix, str(i)), result=result)
        return result
    def load_state_dict(self, sd, *, prefix="", _internal=False):
        for i, op in enumerate(self.op_list):
            op.load_state_dict(sd, prefix=_concat_prefix(prefix, str(i)), _internal=True)

# 迷你 Llama：Leaf 模拟一个带 weight 的子层（如 qkv_proj / norm）
class Leaf(BaseOP):
    def __init__(self, size):
        self.weight = torch.empty(size)
        self._private = "skip_me"      # 下划线前缀，应被跳过

class DecoderLayer(BaseOP):
    def __init__(self, size):
        self.self_attn = Leaf(size)    # 命名仿照 LlamaDecoderLayer
        self.mlp = Leaf(size)
        self._layer_id = 0             # 下划线，跳过

class MiniLlama(BaseOP):
    def __init__(self, size, n_layers):
        inner = BaseOP()               # 内层命名空间 model
        inner.embed_tokens = Leaf(size)
        inner.layers = OPList([DecoderLayer(size) for _ in range(n_layers)])
        inner.norm = Leaf(size)
        self.model = inner
        self.lm_head = Leaf(size)

# ① 在 meta device 上建图（零显存搭骨架，模仿 engine.py:50-51）
with torch.device("meta"):
    net = MiniLlama(size=4, n_layers=2)

# ② 生成 state_dict，观察键路径
sd = net.state_dict()
print("keys:", list(sd.keys()))
# 预期：
#   model.embed_tokens.weight
#   model.layers.0.self_attn.weight
#   model.layers.0.mlp.weight
#   model.layers.1.self_attn.weight
#   model.layers.1.mlp.weight
#   model.norm.weight
#   lm_head.weight
assert all(v.is_meta for v in sd.values()), "建图阶段应为 meta 张量"
assert "_private" not in str(sd.keys()) and "layer_id" not in str(sd.keys())

# ③ 伪造真实权重（CPU），执行替换
real_sd = {k: torch.randn_like(v) for k, v in sd.items()}
net.load_state_dict(real_sd)                       # 应不抛错：键集合完全匹配
assert len(real_sd) == 0, "load 完毕字典应被 pop 干净"
print("OK: 键路径与权重替换均符合预期")

# ④ 故意多塞一个假 key，验证严格匹配
net2 = MiniLlama(size=4, n_layers=2)
bad_sd = {k: torch.randn_like(v) for k, v in net2.state_dict().items()}
bad_sd["model.layers.999.self_attn.weight"] = torch.randn(4)
try:
    net2.load_state_dict(bad_sd)
    print("未抛错——与预期不符")
except RuntimeError as e:
    print("如预期严格报错:", e)
```

**操作步骤**：

1. 把上述示例代码存为 `toy_baseop.py`，`python toy_baseop.py` 运行（只需 `torch`，CPU 即可）。
2. 观察打印的 `keys:` 是否与注释里的预期完全一致，特别注意 `OPList` 的整数下标 `0`/`1`、`_private` 与 `_layer_id` 是否被正确跳过。
3. 确认第 ③ 步 `load_state_dict` 后 `real_sd` 被 `pop` 清空、且无报错。
4. 确认第 ④ 步多塞假 key 时抛 `RuntimeError: Unexpected keys`。

**需要观察的现象**：

- 键路径呈树状，`OPList` 产出 `layers.0.` / `layers.1.` 的整数段。
- 下划线前缀属性（`_private`、`_layer_id`）不出现在键里。
- 建图阶段全是 `is_meta=True`，`load_state_dict` 后被 `setattr` 替换为真实张量、且输入字典被 `pop` 清空。

**预期结果**：正常跑通打印两行 `OK` / `如预期严格报错`。这正好对应 Engine 在 [engine.py:50-52](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L50-L52) 的 `meta 建图 → load_state_dict` 真实流程，也印证了 4.1 里「反射收集 + pop 校验 + setattr 替换」三件事——这三个能力就是 `BaseOP` 替代 `nn.Module` 后唯一保留的、且推理场景下确实足够的全部本事。

> 待本地验证：若想直接 `from minisgl.layers.base import BaseOP, OPList, StateLessOP`，注意导入 `minisgl.layers` 包会触发 `layers/__init__.py`，后者会间接触发对 flashinfer 等 CUDA 依赖的引用——因此在无 GPU 环境下，上面的「复刻类」方式更稳妥。在有完整依赖的环境里，可直接用项目原类。

## 6. 本讲小结

- **`BaseOP` 用反射代替登记**：不重写 `__setattr__`、不继承 `nn.Module`，而是在 `state_dict` / `load_state_dict` 时遍历 `self.__dict__`，用 `isinstance` 识别 `Tensor`（叶子权重）与 `BaseOP`（子模块）并递归；下划线前缀 `_` 的属性一律跳过，非 Tensor/非 BaseOP 属性也被类型检查挡掉。
- **`load_state_dict` 用 `pop` + `_internal` 标志做严格校验**：递归子模块时传 `_internal=True` 禁止其报错，只有根模型在遍历完后检查剩余键，保证权重字典与模型结构一一对应；`setattr` 把 meta 占位张量替换为真实权重——这是 Engine「meta 建图 + 权重替换」的落点。
- **`OPList` 是必需的容器**：因为裸 `list` 会被默认反射跳过，`OPList` 用整数下标 `0/1/2...` 作前缀递归，产出 `layers.0.xxx` 这类键；`StateLessOP` 让自身不持权重的层（如 `AttentionLayer`）避免重复序列化。
- **Llama 是经典四件套组合**：`embed_tokens → OPList[DecoderLayer] → norm → lm_head`，`forward` 不收参数、从全局上下文取输入；`RopeAttn`/`GatedMLP` 以别名从 `utils.py` 复用。
- **残差融合**：残差作为独立变量在层间传递，相加被揉进 `RMSNormFused` 的 `fused_add_rmsnorm`，最后一层的 mlp 残差由末尾 `norm` 收尾，省掉独立的 add kernel。
- **`GatedMLP` 合并 gate/up 投影**：`LinearColParallelMerged` 一次乘法产出两路，`silu_and_mul` 融合门控乘法，`down_proj`（row-parallel）出口 `all_reduce`；`RopeAttn` 把 qkv 合并投影、可选 q/k norm（按 head_dim 归一化）、RoPE（只作用 q/k）、注意力后端调用、输出投影串成一块，注意力计算外包给 `attn_backend`。

## 7. 下一步学习建议

- **u8-l2 模型配置解析与权重加载分片**：本讲只讲了 `load_state_dict` 怎么把扁平字典塞回模型，但没讲这个字典**怎么从 HF safetensors 读出来**、`q_proj/k_proj/v_proj` 怎么合并成 `qkv_proj`、`gate_proj/up_proj` 怎么合并成 `gate_up_proj`——这正是 u8-l2 的主题，建议紧接着读。
- **u9-l1 张量并行 Linear 与分布式通信**：本讲多次提到 `LinearColParallelMerged` / `LinearRowParallel` / `LinearOProj` 的切分与 `all_reduce`，细节在 u9-l1 展开。
- **u9-l2 Embedding / Norm / RoPE 与 AttentionLayer**：本讲对 `VocabParallelEmbedding`、`RMSNormFused`、`AttentionLayer` 内部只点到为止，完整前向与 RoPE 细节留到 u9-l2。
- **u10-l1 MoE 后端（Fused MoE）**：想看 `MoEMLP`（与 `GatedMLP` 对偶）的完整实现，读 u10-l1。
- **u10-l3 支持新模型架构**：想动手接入一个新模型？参照 `register.py` 的注册表与 `RopeAttn(has_qk_norm=True)` 这类开关，u10-l3 给出完整 checklist。
- **延伸阅读**：对比阅读 `torch.nn.Module` 的 `__setattr__` 与 `state_dict` 实现，能更深刻理解 Mini-SGLang「砍掉登记、改用反射」这一取舍的得失。
