# BaseOP 体系与 Llama 模型实现

## 1. 本讲目标

前面几讲我们一路从「进程架构」「调度器」追到了「Engine 前向与采样」。在 Engine 的前向里有一个一直被当作黑盒的调用：`self.model.forward()`。本讲就打开这个黑盒，回答一个问题——**Mini-SGLang 的模型是怎么用代码「搭」出来的？**

读完本讲，你应当能够：

1. 说清为什么 Mini-SGLang **不用 `torch.nn.Module`**，而是自己实现了一套极简的 `BaseOP` 体系，以及它在纯推理场景下「够用」的原因。
2. 理解 `BaseOP.state_dict` / `load_state_dict` 如何靠 `__dict__` 反射**递归**地收集与装载权重，并用「下划线前缀跳过」这一约定区分「需要加载的权重」与「纯配置字段」。
3. 读懂 `LlamaForCausalLM` 的层级结构：`embed_tokens → 多层 decoder → norm → lm_head`，以及它的 `forward` 为什么**不接收任何参数**、而是从全局上下文 `get_global_ctx().batch` 里取输入。
4. 拆解两个核心子模块 `GatedMLP`（门控 MLP，含 gate_up 合并与残差融合）和 `RopeAttn`（旋转位置编码注意力，含可选的 q/k norm）。

本讲是「模型实现与权重加载」单元的第一篇，只讲**模型骨架与层组合**；权重如何被流式加载、按 TP 分片、合并 qkv，留到 [u8-l2](u8-l2-config-weight-loading.md)。

## 2. 前置知识

- **`torch.nn.Module` 与 `state_dict`**：PyTorch 里几乎所有模型都继承自 `nn.Module`，它内置了 `state_dict()` / `load_state_dict()`、自动注册子模块、buffer、hook、autograd 反向传播等大量功能。本讲的核心参照系就是这个「标准做法」。
- **RMSNorm**：比 LayerNorm 更省算力的归一化。给定向量 \(x\)，先算均方根再缩放：

  \[ \text{rms}(x) = \sqrt{\frac{1}{n}\sum_{i=1}^{n} x_i^2 + \epsilon}, \qquad y_i = \frac{x_i}{\text{rms}(x)} \cdot w_i \]

  没有「减均值」这一步，所以比 LayerNorm 少一次归约。

- **残差与「残差融合」**：Transformer 每个子层输出都会 `x = x + sublayer(x)`，这个 `+` 的左侧叫 residual（残差）。Mini-SGLang 用 flashinfer 的 `fused_add_rmsnorm` 把「加残差」和「RMSNorm」**融成一个 kernel**，省掉一次显存读写，这就是「残差融合」。
- **RoPE（旋转位置编码）**：通过对 query/key 向量按位置做二维旋转来注入位置信息，是 Llama/Qwen 系列的标配。
- **全局上下文 `Context`**：见 [u2-l1](u2-l1-core-data-structures.md)。模型 `forward` 时需要的 `input_ids`、`positions`、`attn_backend` 等都挂在进程级单例 `get_global_ctx()` 上，模型层不必把这些东西一层层传参。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [layers/base.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/base.py) | **`BaseOP` / `StateLessOP` / `OPList`**——替代 `nn.Module` 的极简基类，提供递归 `state_dict` / `load_state_dict`。本讲地基。 |
| [models/base.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/base.py) | `BaseLLMModel`——所有模型的最顶端抽象，只是「一个有 `forward` 的 `BaseOP`」。 |
| [models/llama.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/llama.py) | **`LlamaForCausalLM`** 的完整实现：`LlamaDecoderLayer` / `LlamaModel` / `LlamaForCausalLM` 三层嵌套。 |
| [models/utils.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/utils.py) | 跨模型复用的积木：**`GatedMLP`** / `MoEMLP` / **`RopeAttn`**。llama/qwen2/qwen3 都从这里 import 这些块。 |
| [models/register.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/register.py) | 模型注册表 `_MODEL_REGISTRY` 与工厂 `get_model_class`，把字符串架构名映射到具体模型类。 |

辅助理解（不是本讲重点，但会被引用）：

- [engine/engine.py:L48-L52](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L48-L52)：Engine 用 `meta` device 建图 + `load_state_dict` 装权重的「低内存建图」入口。
- [layers/linear.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/linear.py)：TP Linear 族，`GatedMLP`/`RopeAttn` 内部用的就是它们（u9-l1 详讲）。
- [layers/norm.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/norm.py)：`RMSNorm` / `RMSNormFused`。
- [layers/attention.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/attention.py)：`AttentionLayer`，`RopeAttn` 内部真正算注意力的部件。

## 4. 核心概念与源码讲解

### 4.1 BaseOP / StateLessOP / OPList —— 自己造一个最小骨架

#### 4.1.1 概念说明：为什么不直接用 nn.Module？

PyTorch 的 `nn.Module` 是一个「全家桶」：它同时承担了**参数容器**、**自动求导**、**模块树管理**、**hook 机制**、**序列化**（`state_dict`）、**设备搬运**、**训练/推理模式切换**等十几项职责。这对训练场景非常合适，但对 Mini-SGLang 这样的**纯推理引擎**来说，其中绝大部分都是「死重」：

- 不会反向传播（`autograd` 完全用不到）；
- 不需要 `register_buffer` / `register_parameter` 这套注册机制；
- 不需要 hook、不需要 `train()`/`eval()`；
- 不需要 `.to(device)` / `.cuda()`（设备由 tensor 自己决定）。

`nn.Module` 每实例化一个对象都要维护 `._parameters` / `._buffers` / `._modules` 三个 `OrderedDict`，并触发一堆内部同步，在「每张卡上建一棵含几十上百层的模型树」时是实打实的开销与复杂度。

于是 Mini-SGLang 选择**只保留「权重装载」这一项核心能力**，自己写一个约 100 行的 `BaseOP` 体系：

- `BaseOP`：带权重的算子，能递归地 `state_dict` / `load_state_dict`；
- `StateLessOP`：「无状态」算子（不持有需要加载的权重），它的 state_dict 永远是空；
- `OPList`：把一组同型 `BaseOP`（比如 32 个 decoder layer）打包成有序容器，键名用数组下标。

这套体系的设计哲学是：**用 Python 的 `__dict__` 直接当参数容器**，靠命名约定（下划线前缀）区分「权重」与「配置」，避免任何额外注册开销。

#### 4.1.2 核心流程：递归 state_dict / load_state_dict

整个体系只做一件事：**把一棵 BaseOP 树与一个扁平的 `{名字: 张量}` 字典互转**。流程可以概括为两段对称的递归。

**导出 `state_dict`**（自顶向下收集）：

```
state_dict(node, prefix=""):
    for (name, value) in node.__dict__:
        if name 以 "_" 开头:        # 配置/内部字段，跳过
            continue
        if value 是 torch.Tensor:   # 叶子权重
            result[prefix + name] = value
        elif value 是 BaseOP:       # 子树，递归，前缀加一层
            state_dict(value, prefix + name + ".")
    return result
```

**导入 `load_state_dict`**（消费式装载）：

```
load_state_dict(node, state_dict, prefix=""):
    for (name, value) in node.__dict__:
        if name 以 "_" 开头: skip
        if value 是 Tensor:
            item = state_dict.pop(prefix + name)   # 取出并移除
            断言 shape / dtype 一致
            setattr(node, name, item)              # 覆盖占位张量
        elif value 是 BaseOP:
            load_state_dict(value, state_dict, prefix + name + ".")
    # 最外层调用结束后，若字典还有剩余 key → 报错
```

三个值得注意的设计点：

1. **前缀拼接用点号**：`_concat_prefix(prefix, name)` 产出 `a.b.c` 这样的层级键名，与 HuggingFace ckpt 的命名风格一致（如 `model.layers.0.self_attn.qkv_proj.weight`）。
2. **`pop` 而非读**：`load_state_dict` 用 `state_dict.pop(...)` **消费**字典，装载完毕后字典应当被掏空；最外层（`_internal=False`）会检查剩余 key，**有任何多余权重就 `raise RuntimeError`**。这是一份严格的「模型结构 ⇄ 权重文件」契约——少一个、多一个都不行。
3. **下划线前缀跳过**：这是唯一的「配置 vs 权重」判据。例如 `RopeAttn` 里 `self.has_qk_norm`（布尔配置）会被收进 state_dict 吗？不会——但它不是因为「是 bool」被跳过（`state_dict` 只收 `Tensor` 和 `BaseOP`），而是因为它**不是 Tensor 也不是 BaseOP**。真正用上「下划线跳过」的是像 `_layer_id`、`_LinearTPImpl` 的内部标量这类**想藏起来的字段**。两种机制叠加，确保只有「真权重」进字典。

`StateLessOP` 是这套体系的「减法」：它把 `state_dict` 固定为返回空、`load_state_dict` 固定为只检查多余 key，用于「逻辑上是子层、但不持有可训练权重」的部件（典型例子就是 `AttentionLayer`——它的 q/k/v 投影权重在 `RopeAttn` 上，自己只存 `layer_id`、`rotary` 这些常量）。

`OPList` 则是「加法」：专门处理「N 个同型子层」的有序容器，键名用整数下标（`layers.0`、`layers.1`……），递归逻辑与 `BaseOP` 几乎一致，只是遍历对象从 `self.__dict__` 换成 `self.op_list`。

#### 4.1.3 源码精读

先看 `_concat_prefix` 与 `BaseOP` 本体：

[base.py:L11-L30](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/base.py#L11-L30) 定义前缀拼接工具与 `BaseOP.state_dict`。注意第 22 行遍历的是 `self.__dict__.items()`——**直接拿实例字典，没有任何注册表参与**；第 23-24 行就是「下划线跳过」约定；第 25-28 行用 `isinstance` 把对象分成「Tensor 叶子」「BaseOP 子树」两类分别处理。

[base.py:L32-L53](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/base.py#L32-L53) 是 `BaseOP.load_state_dict`。第 43 行 `state_dict.pop(...)` 是消费式装载的关键；第 45 行用 `assert` 校验 shape/dtype，防止悄悄错位；第 46 行用 `setattr(self, name, item)` **覆盖**占位张量——这正是 Engine 在 meta device 上「先建空壳、再填真权重」的落点（见 4.1.4）。第 52-53 行在最外层校验「没有多余 key」。

再看 `StateLessOP` 与 `OPList`：

[base.py:L56-L71](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/base.py#L56-L71) 是 `StateLessOP`：`state_dict` 恒返回空，`load_state_dict` 只做「多余 key」检查。它让 `AttentionLayer` 这类部件既能挂在 BaseOP 树里（被父节点 `isinstance(param, BaseOP)` 识别并递归），又不会贡献任何权重。

[base.py:L77-L99](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/base.py#L77-L99) 是 `OPList`：构造时把传入列表存到 `self.op_list`（**不带下划线**，所以它本身是个会被 `BaseOP` 识别的 BaseOP 子属性——但注意 `OPList` 自己重写了 `state_dict`/`load_state_dict`，直接遍历 `self.op_list`，所以 `op_list` 这个名字不会出现在最终 key 里，子层直接以 `0/1/2...` 为名）。

最后看一眼这套体系在 Engine 里的实际用法：

[engine.py:L48-L52](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L48-L52)：在 `torch.device("meta")` 上下文里 `create_model(...)`，建出来的所有张量都是 **meta tensor（零显存的占位）**；紧接着一句 `self.model.load_state_dict(self._load_weight_state_dict(config))`，用 `setattr` 把真权重逐个覆盖上去。这两行是 [u5-l1](u5-l1-engine-init-memory.md) 讲过的「低内存建图」的微观实现，而它能 work 的全部前提就是 `BaseOP` 提供了这个递归 `load_state_dict`。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `BaseOP` 的递归 `state_dict` 行为，确认「下划线跳过」「点号前缀」「严格匹配」三条规则。

**操作步骤**（纯 CPU，无需 GPU，约 5 分钟）：

1. 在项目根目录启一个 `python`（确保能 `import minisgl`）。
2. 写一个最小 BaseOP 树，故意混入「下划线字段、普通字段、子 BaseOP、Tensor」：

```python
# 示例代码：用于观察 state_dict 的键名生成规则
import torch
from minisgl.layers import BaseOP, OPList, StateLessOP

class Leaf(BaseOP):
    def __init__(self):
        self.weight = torch.zeros(3, 4)          # Tensor，应被收集
        self._secret = "hide me"                # 下划线，应被跳过
        self.config_field = 42                  # 非 Tensor/非 BaseOP，应被跳过

class NoWeight(StateLessOP):
    def __init__(self):
        self.layer_id = 7                       # 无状态，state_dict 应为空

class Root(BaseOP):
    def __init__(self):
        self.embed = Leaf()
        self.noise = NoWeight()
        self.layers = OPList([Leaf(), Leaf()])

r = Root()
sd = r.state_dict()
for k, v in sd.items():
    print(f"{k:30} shape={tuple(v.shape)}")
```

3. 再做一次「严格匹配」实验：故意给一个**多出来的假 key**，调用 `load_state_dict`，观察报错。

```python
# 示例代码：验证多余 key 会触发严格匹配错误
r2 = Root()
bad_sd = r2.state_dict()
bad_sd["model.layers.999.weight"] = torch.zeros(1)
try:
    r2.load_state_dict(bad_sd)
except RuntimeError as e:
    print("如预期报错:", e)
```

**需要观察的现象**：

- 第 2 步打印的键名应当**只有** `embed.weight`、`layers.0.weight`、`layers.1.weight` 三项；`_secret`、`config_field`、`layer_id`、`noise.*` 全部不出现。
- `embed.weight` 的 shape 是 `(3, 4)`，前缀点号拼接正确。

**预期结果**：键名集合严格等于 `{embed.weight, layers.0.weight, layers.1.weight}`；第 3 步抛出 `RuntimeError: Unexpected keys in state_dict: [...]`。

> 待本地验证：若你的 `import minisgl` 因缺 CUDA 依赖失败，可把 `BaseOP`/`OPList`/`StateLessOP` 的源码（约 100 行）单独复制到一个 `.py` 文件里跑，逻辑与是否装了 torch 强相关、与 CUDA 无关。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `RMSNorm.__init__` 里的 `self.eps = eps` 改成 `self._eps = eps`，对 `state_dict` 的输出有影响吗？

> **答案**：没有影响。`eps` 是 `float`，原本就因为「既不是 Tensor 也不是 BaseOP」而被跳过；改成 `_eps` 只是多了一道「下划线跳过」保险，结果一样。真正决定是否进入字典的是**类型判断**，下划线是第二道关。

**练习 2**：`OPList` 的实例属性叫 `self.op_list`（无下划线）。为什么它没有出现在最终的 state_dict 键名里（比如没有 `layers.op_list.0.weight`）？

> **答案**：因为 `OPList` **重写**了 `state_dict`/`load_state_dict`，直接遍历 `self.op_list` 列表并用整数下标 `i` 作为键名（`base.py:L84-L85`），不再走 `BaseOP` 那套「遍历 `__dict__`」的逻辑，所以 `op_list` 这个属性名本身被绕过了，子层直接以 `layers.0`、`layers.1` 出现。

**练习 3**：`load_state_dict` 用 `pop` 消费字典、并在末尾检查「剩余 key」。请说出这个设计相比「只读不删」的好处。

> **答案**：能在装载结束后**立刻发现「权重文件里有、但模型结构里没有」的多余权重**。推理场景下，权重要么来自 HF ckpt、要么来自 `state_dict()` 自身生成，任何不匹配都意味着结构对不上（比如换模型忘改配置），早 fail 比默默用错权重安全得多。

---

### 4.2 LlamaForCausalLM —— 模型的顶层组装

#### 4.2.1 概念说明：标准 decoder-only 架构

`LlamaForCausalLM` 是一个最典型的 **decoder-only 因果语言模型**，数据流是一条直线：

```
input_ids (token id 序列)
   │  VocabParallelEmbedding
   ▼
hidden_states (每个 token → 一个 hidden_size 向量)
   │  × N 层 LlamaDecoderLayer (attn + mlp + 残差)
   ▼
final RMSNorm
   │  ParallelLMHead (→ vocab 维 logits)
   ▼
logits
```

它的「因果」体现在注意力里——每个位置只能看到自己和之前的位置（由 attn backend 保证，本讲不展开）。

这里有两个 Mini-SGLang 特有的设计选择值得点出：

1. **积木复用**：`llama.py` 里 `LlamaAttn` 其实就是 `utils.RopeAttn`，`LlamaMLP` 就是 `utils.GatedMLP`（见 [llama.py:L10-L12](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/llama.py#L10-L12)）。qwen2/qwen3/mistral 也复用同一批积木，只是换了组合方式或个别开关。这就是为什么 `utils.py` 被放在 `models/` 而不是 `layers/`——它是「模型级」的复用层。
2. **forward 不收参数**：`LlamaForCausalLM.forward(self)` 没有任何入参，它直接从全局上下文 `get_global_ctx().batch.input_ids` 取输入。这让 Engine 调用模型时只需 `self.model.forward()`，无需把 batch 一路透传。

#### 4.2.2 核心流程：从 token 到 logits

一个 batch 的前向（伪代码，省略残差细节）：

```
LlamaForCausalLM.forward():
    input_ids = get_global_ctx().batch.input_ids     # 从全局上下文取
    x = model.embed_tokens.forward(input_ids)        # token → hidden
    residual = None
    for layer in model.layers.op_list:               # 串行过 N 层
        x, residual = layer.forward(x, residual)     # 残差跨层传递
    x = model.norm.forward(x, residual)[0]           # 最后一层 norm
    logits = lm_head.forward(x)                      # hidden → vocab logits
    return logits
```

每层 `LlamaDecoderLayer.forward` 的内部节奏（**残差融合**的关键所在）：

```
Layer.forward(x, residual):
    x, residual = input_layernorm.forward(x, residual)   # 加残差 + RMSNorm 融合
    x = self_attn.forward(x)                             # 注意力子层
    x, residual = post_attention_layernorm.forward(x, residual)  # 再融合一次
    x = mlp.forward(x)                                   # MLP 子层
    return x, residual                                   # 残差继续传给下一层
```

注意 `residual` 是**贯穿所有层的单条流**：每到一个 norm，就把它和当前 `x` 一起喂给 `fused_add_rmsnorm`，原地加完再做归一化。这样 N 层只需 N 次显存读写来完成「加残差」，而不是每次子层都单独存一份。

#### 4.2.3 源码精读

先看三个类自底向上的组装。

[llama.py:L18-L43](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/llama.py#L18-L43) 是 `LlamaDecoderLayer`：构造时挂四个子部件（`self_attn` / `mlp` / `input_layernorm` / `post_attention_layernorm`），把 `layer_id` 存到**带下划线**的 `self._layer_id`（L31）——这就是 4.1 里「下划线跳过」的典型用法：`layer_id` 是给 `nvtx_annotate` 标注用的配置，不该进 state_dict。`forward`（L34-L43）严格按上面伪代码的「norm → attn → norm → mlp」节奏，返回 `(x, residual)` 元组。

[llama.py:L46-L65](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/llama.py#L46-L65) 是 `LlamaModel`：`embed_tokens`（词表并行 embedding）+ `layers`（一个 `OPList`，装 N 个 `LlamaDecoderLayer`）+ `norm`（最终 RMSNorm）。`forward` 第 62-64 行就是那条「串行过层 + 残差接力」的主循环；第 65 行 `[0]` 表示只取 norm 返回的 `(x, residual)` 中的 `x`。

[llama.py:L68-L82](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/llama.py#L68-L82) 是 `LlamaForCausalLM`：组合 `model`（LlamaModel）+ `lm_head`（`ParallelLMHead`）。重点看 L74-75：`lm_head` 接收 `tie_word_embeddings` 开关与 `tied_embedding`——当模型采用「词嵌入与输出头权重绑定」（weight tying，`tie_word_embeddings=True`）时，`lm_head` 直接复用 `embed_tokens` 的权重，不再单独存一份，省一半词表显存。第 80 行 `get_global_ctx().batch.input_ids` 就是「forward 不收参数」的落点。注意 L77 的 `super().__init__()` 放在最后——因为 `BaseLLMModel` / `BaseOP` 的 `__init__` 实际是空的（`object.__init__`），这里只是形式上调用，真正「建图」是靠前面几行往 `self.__dict__` 里塞属性完成的。

[models/base.py:L12-L14](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/base.py#L12-L14) 是 `BaseLLMModel`：`class BaseLLMModel(ABC, BaseOP)`——多重继承，既是抽象基类（`ABC`，强制子类实现 `forward`）又是 `BaseOP`（获得 `state_dict`/`load_state_dict`）。它只声明一个 `@abstractmethod forward`，把「模型必须能前向」这一契约钉死。

模型如何被「按名实例化」？看注册表：

[register.py:L5-L21](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/register.py#L5-L21)：`_MODEL_REGISTRY` 把 HF 的架构名（如 `"LlamaForCausalLM"`、`"Qwen3ForCausalLM"`）映射到 `(模块路径, 类名)`；`get_model_class` 用 `importlib.import_module(..., package=__package__)` **动态导入**对应模块并 `getattr` 取类、立即实例化。注意 L19 的 `package=__package__` 让 `.llama` 这种相对路径解析为 `minisgl.models.llama`。这套机制让「新增一个模型」只需：写一个 `models/xxx.py` + 在注册表加一行（详见 [u10-l3](u10-l3-add-new-model.md)）。`create_model`（[models/__init__.py:L7-L8](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/__init__.py#L7-L8)）取 `model_config.architectures[0]` 作 key 调用它。

#### 4.2.4 代码实践

**实践目标**：用「源码阅读型」方式，确认 `LlamaForCausalLM` 的 state_dict 键名与 HF 权重文件对得上。

**操作步骤**：

1. 打开 [llama.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/llama.py)，按「`LlamaForCausalLM` → `self.model` → `self.lm_head`」的装配关系，**手写推导**一个 2 层 Llama 的全部 state_dict 键名（假设 `tie_word_embeddings=False`、无 qk_norm）。
2. 把你推导的键名，与任意一个真实 Llama-3 的 `model.safetensors.index.json`（HF Hub 上可下）里的键名对照（注意 HF 用 `q_proj/k_proj/v_proj/o_proj`，Mini-SGLang 运行时已合并成 `qkv_proj`——这正是 [u8-l2](u8-l2-config-weight-loading.md) 要讲的「合并」步骤，本步只需对照 `embed_tokens`、`norm`、`layers.N.*` 的命名层级）。

**需要观察的现象**：

- 你的推导里应当出现形如 `model.embed_tokens.weight`、`model.layers.0.self_attn.qkv_proj.weight`、`model.layers.0.mlp.down_proj.weight`、`model.norm.weight`、`lm_head.weight` 的键。
- `layers.0` / `layers.1` 来自 `OPList` 的整数下标；`model.` 前缀来自 `LlamaForCausalLM` 里属性名 `self.model`。

**预期结果**：手写键名的**层级与命名风格**与 HF ckpt 一致（具体投影矩阵的合并/拆分差异留到 u8-l2）。

> 待本地验证：是否真正一一对应，取决于具体模型的 HF 命名；本实践只验证「层级结构」正确，不验证「投影矩阵拆分」。

#### 4.2.5 小练习与答案

**练习 1**：`LlamaForCausalLM.forward()` 没有参数。如果改成 `forward(self, input_ids)`，需要改动哪些地方？

> **答案**：至少要改 Engine 里调用模型的地方（`engine.py` 里 `self.model.forward()` 要传入 `batch.input_ids`），以及 CUDA Graph 捕获时对 forward 的调用路径。Mini-SGLang 选择用全局上下文是为了让模型层内部（如 `AttentionLayer` 也能取 `positions`、`attn_backend`）统一从 `get_global_ctx()` 拿，避免把 batch 透传到每一层。代价是隐式依赖全局状态、可测试性略低。

**练习 2**：`LlamaModel.forward` 末尾 `return self.norm.forward(x, residual)[0]` 里的 `[0]` 舍掉了什么？为什么可以舍掉？

> **答案**：`RMSNormFused.forward` 返回 `(x, residual)` 元组；`[0]` 取的是归一化后的 `x`，舍掉的是更新后的 `residual`。可以舍掉是因为这是**最后一层 norm**，后面直接进 `lm_head`，不再需要残差流了。残差只在层间传递，到模型出口就结束。

**练习 3**：`tie_word_embeddings=True` 时，`lm_head` 的权重会出现在 state_dict 里吗？

> **答案**：取决于 `ParallelLMHead` 的实现。从 `llama.py:L74-L75` 看，权重绑定时传入 `tied_embedding=self.model.embed_tokens`，`lm_head` 会复用这个对象；只要 `ParallelLMHead` 内部把绑定的 embedding 当作「同一个 BaseOP 引用」而不是新建一份权重，`state_dict` 在递归时就会只产生一份 `model.embed_tokens.weight`（具体由 `ParallelLMHead` 决定，详见 layers/embedding.py）。这正是 weight tying 省显存的原理——一份权重两处用。

---

### 4.3 GatedMLP —— 门控前馈网络

#### 4.3.1 概念说明：SwiGLU 门控

Llama 系列用的不是普通的两层 MLP，而是 **Gated MLP（门控 MLP）**，最常见的形式是 SwiGLU。直觉是：与其让一个投影把 hidden 直接变成 intermediate，不如**分成两路**——一路算「值」，一路算「门」，再用门的 sigmoid 激活去筛选值：

\[ \text{gate}, \text{up} = W_{\text{gate}} x,\; W_{\text{up}} x \]
\[ y = (\text{SiLU}(\text{gate}) \odot \text{up}) \]
\[ \text{out} = W_{\text{down}}\, y \]

其中 \(\text{SiLU}(z) = z \cdot \sigma(z) = z \cdot \frac{1}{1+e^{-z}}\)，\(\odot\) 是逐元素乘。

好处是：门控让网络能在「该激活」「该抑制」之间动态选择，表达能力比固定激活强。代价是参数量翻倍（gate 和 up 各一份），所以 Llama 配置里 `intermediate_size` 通常约等于 \( \frac{8}{3} \times \text{hidden\_size}\) 再对齐到 256 的倍数。

Mini-SGLang 在工程上做了两个优化：

1. **gate / up 合并成一个 `gate_up_proj`**：把 `W_gate` 和 `W_up` 在输出维上拼成一个大矩阵，一次矩阵乘就算完两路，省一次 GEMM 调用。算完再用 `silu_and_mul` 这个 kernel 沿中间维度劈成两半、做 SiLU 与逐元素乘。这就是 4.3.3 里要看到的 `LinearColParallelMerged`。
2. **TP 切分位置**：`gate_up_proj` 是**列并行**（切输出维，各卡算自己那段 gate/up，无需通信）；`down_proj` 是**行并行**（切输入维，各卡算部分和），只在最后做一次 `all_reduce`。所以一个 MLP 全程只需**一次通信**（u9-l1 详讲）。

#### 4.3.2 核心流程

```
GatedMLP.forward(x):
    gate_up = gate_up_proj.forward(x)   # 一次 GEMM 出 [gate | up] 两路拼接
    del x                                # 立即释放输入，省显存
    y = act_fn(gate_up)                  # silu_and_mul: 劈半 + SiLU(gate)*up
    del gate_up                          # 立即释放中间
    return down_proj.forward(y)          # 行并行 GEMM + all_reduce
```

这里的 `del x` / `del gate_up` 是 Mini-SGLang 贯穿模型层的**显存卫生习惯**：在 GPU 上，一个张量只要还有引用就不会被回收；显式 `del` 能让中间结果在进入下一阶段前尽快归还显存，对大 batch / 长序列非常关键。

#### 4.3.3 源码精读

[utils.py:L25-L50](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/utils.py#L25-L50) 是 `GatedMLP`。逐段看：

- L27-L31：`gate_up_proj` 用 `LinearColParallelMerged`，输出维度传的是**列表** `[intermediate_size, intermediate_size]`（[linear.py:L56-L68](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/linear.py#L56-L68) 会把列表里每个 size 各自除以 tp_size 再求和，得到本卡的合并输出维度）——这正是「gate 和 up 两路、各自 TP 切分、再拼一起」的实现。
- L33-L37：用一张 `FN_MAP` 把 `config.hidden_act`（`"silu"` 或 `"gelu"`）映射到 `silu_and_mul` / `gelu_and_mul`，遇到不支持的激活**直接抛错**而非静默回退。
- L38-L42：`down_proj` 是 `LinearRowParallel`（行并行），负责把 intermediate 维还原回 hidden_size。
- L44-L50 的 `forward`：严格按 4.3.2 的流程，`@nvtx_annotate("MLP")` 给 Nsight profiler 打标签；两个 `del` 是显存卫生。

> 顺带一提：同文件的 [utils.py:L53-L76](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/utils.py#L53-L76) 是 `MoEMLP`（混合专家 MLP），结构与 `GatedMLP` 对偶：把「单一 gate_up/down」换成 `MoELayer`（N 个专家）+ 一个 `LinearReplicated` 路由（gate）。它服务于 Qwen3-MoE 等模型，本讲不展开，详见 [u10-l1](u10-l1-moe-fused.md)。

#### 4.3.4 代码实践

**实践目标**：理解 `silu_and_mul` 的「劈半 + 门控」语义，并能手算一个小例子。

**操作步骤**（纸笔计算 + 可选验证）：

1. 设 `intermediate_size = 4`、`hidden_size` 任意，假设某次前向 `gate_up_proj` 对一个 token 输出 8 维向量（gate 4 维 + up 4 维拼接）：
   - `gate  = [ 1.0, -1.0,  2.0,  0.0]`
   - `up    = [ 2.0,  3.0,  0.5,  1.0]`
2. 用 SiLU 定义 \(\text{SiLU}(z)=z\sigma(z)\) 手算 `silu_and_mul` 的输出（4 维）。
3. （可选）若环境装了 `sgl_kernel`，写两行代码验证：`from minisgl.layers import silu_and_mul`，喂一个 `(1,8)` 张量，看输出是否与你手算一致。

**需要观察的现象**：`gate` 为负的位置（如 -1.0）经 SiLU 后会得到一个接近 0 的负数，再乘 `up` 后对输出贡献很小——这就是「门控抑制」。

**预期结果**：

- \(\text{SiLU}(1.0) = 1.0 \cdot \sigma(1.0) \approx 1.0 \cdot 0.731 = 0.731\)，乘 `up=2.0` → \(\approx 1.462\)
- \(\text{SiLU}(-1.0) = -1.0 \cdot \sigma(-1.0) \approx -1.0 \cdot 0.269 = -0.269\)，乘 `up=3.0` → \(\approx -0.807\)（被「门」大幅压低）
- 其余两项按同样方法算。

> 待本地验证：第 3 步的具体数值取决于 kernel 实现（是否 fused、精度），以 kernel 实际输出为准；手算用于确认语义。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `gate_up_proj` 用「列并行」而 `down_proj` 用「行并行」？如果反过来会怎样？

> **答案**：列并行切输出维——各卡独立算出自己那段 gate/up，无需通信即可进入 `silu_and_mul`（因为激活是逐元素、逐通道的，各卡处理自己的通道互不干扰）。行并行切输入维——`down_proj` 各卡算部分和，只有把所有部分和加起来才得到完整结果，所以需要一次 `all_reduce`。若反过来：`gate_up` 行并行会导致各卡拿到的是「部分激活」，门控语义错误；`down` 列并行则各卡只拿到部分 hidden 维，后面没法直接进 norm。所以这个切分是**由「激活逐元素」和「最终需聚合」两点共同决定**的。

**练习 2**：`GatedMLP.forward` 里为什么 `del x` 之后才 `del gate_up`？少写一个 `del` 会出错吗？

> **答案**：不会出错，只是少回收一点显存。`del` 是显存卫生优化、不是正确性要求；Python 的引用计数会在变量离开作用域时自动回收，但模型 forward 是一个大函数，中间变量不会立刻离开作用域，显式 `del` 能让显存更早归还，降低峰值占用。顺序上 `del x`（释放输入）→ 用 `gate_up` 算 `y` → `del gate_up`（释放中间）是「用完即删」的自然顺序。

**练习 3**：`config.hidden_act` 既可能是 `"silu"` 也可能是 `"gelu"`。如果传入 `"relu"` 会发生什么？

> **答案**：`FN_MAP.get("relu")` 返回 `None`，构造函数在 [utils.py:L35-L36](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/utils.py#L35-L36) 直接 `raise ValueError`。模型不会静默用错激活——这是「显式优于隐式」的设计。

---

### 4.4 RopeAttn —— 旋转位置编码注意力

#### 4.4.1 概念说明：注意力 + RoPE + qk_norm

`RopeAttn` 把「标准多头注意力 + 旋转位置编码」打包成一个可复用块。它的前向是经典三步：

\[ Q, K, V = W_{\text{qkv}}\, x \]
\[ Q, K = \text{RoPE}(Q, K, \text{pos}) \quad (\text{给 Q/K 注入位置}) \]
\[ O = \text{Attention}(Q, K, V) \]
\[ \text{out} = W_o\, O \]

其中 Attention 的核心运算是：

\[ \text{Attn}(Q,K,V) = \text{softmax}\!\left(\frac{QK^{\top}}{\sqrt{d_k}}\right) V \]

RoPE 的「旋转」作用于 head_dim 内的二维子空间。对维度对 \((x_{2i}, x_{2i+1})\) 与位置 \(m\)：

\[ \begin{pmatrix} x_{2i}' \\ x_{2i+1}' \end{pmatrix} = \begin{pmatrix} \cos(m\theta_i) & -\sin(m\theta_i) \\ \sin(m\theta_i) & \cos(m\theta_i) \end{pmatrix} \begin{pmatrix} x_{2i} \\ x_{2i+1} \end{pmatrix}, \qquad \theta_i = \text{base}^{-2i/d} \]

关键性质：两个 token 的 Q·K 内积只依赖它们的**相对位置**（\(m-n\)），所以 RoPE 天然编码相对位置，无需显式 position embedding。

Mini-SGLang 在工程上同样做了合并与并行：

1. **qkv 合并**：用 `LinearQKVMerged` 把 Q/K/V 三个投影拼成一个大矩阵一次算出（类比 GatedMLP 的 gate_up 合并）。
2. **GQA 兼容**：当 KV 头数少于 Q 头数（Grouped-Query Attention，如 Llama-3 的 32 Q 头 / 8 KV 头），`LinearQKVMerged` 在 TP 下对 KV 头做复制（`allow_replicate`），保证每卡至少有一个 KV 头。
3. **可选 q/k norm**：Qwen3 等模型在 RoPE **之前**对 Q、K 各做一次 RMSNorm（`q_norm`/`k_norm`），用以稳定训练。`RopeAttn` 通过 `has_qk_norm` 开关决定是否挂这两个 norm——Llama 默认关闭，Qwen3 开启。
4. **注意力计算外包**：`RopeAttn` 自己不实现 softmax/attention kernel，而是把 Q/K/V 交给 `AttentionLayer`，后者再调全局上下文里的 `attn_backend`（FlashInfer / FA 等，见 [u7](u7-l1-attention-backend-abstract.md)）。这样「位置编码」与「注意力后端」解耦，换后端不用动模型代码。

#### 4.4.2 核心流程

`RopeAttn.forward(x)` 内部：

```
qkv = qkv_proj.forward(x)               # 一次 GEMM 出 [Q | K | V] 拼接
del x
o = attn.forward(qkv)                    # AttentionLayer 内部:
                                         #   split qkv -> q, k, v
                                         #   (可选) q_norm/k_norm 原地归一化
                                         #   RoPE(q, k, positions)
                                         #   attn_backend.forward(...)  -> paged attention
return o_proj.forward(o)                 # 输出投影 (行并行 + all_reduce)
```

`AttentionLayer.forward`（[attention.py:L47-L57](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/attention.py#L47-L57)）的关键几步：

- `qkv.split([qo_dim, kv_dim, kv_dim], dim=-1)`：按列切成 q/k/v 三段——切分依据是本卡的 Q 头数、KV 头数（已经过 TP 切分，见 L33-L34）。
- `q_norm`/`k_norm`（若非 None）：`forward_inplace` 原地把每个 head 的 head_dim 向量归一化。
- `self.rotary.forward(ctx.batch.positions, q, k)`：从全局上下文取**每个 token 的位置**（position id），对 q/k 施加 RoPE。
- `ctx.attn_backend.forward(q, k, v, layer_id, ctx.batch)`：交给注意力后端算 paged attention（KV 来自池，见 [u6](u6-l1-kvcache-pool-prefix-abstract.md)）。

注意 `AttentionLayer` 继承自 `StateLessOP`（[attention.py:L18](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/attention.py#L18)）——它自己不持有任何权重（qkv/o 投影权重在 `RopeAttn` 上，q/k norm 权重也由 `RopeAttn` 持有并传入），是个纯「算子粘合层」。

#### 4.4.3 源码精读

[utils.py:L79-L123](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/utils.py#L79-L123) 是 `RopeAttn`。构造函数（L80-L116）：

- L89-L95：`qkv_proj = LinearQKVMerged(...)`，传入 `num_qo_heads`（Q+输出头数）、`num_kv_heads`、`head_dim`，由 Linear 内部按 TP 切分与 GQA 复制处理。
- L96-L102：`has_qk_norm` 开关。开启时建两个 `RMSNorm(head_dim, ...)`（注意是按 **head_dim** 归一化，不是 hidden_size！），关闭时置 `None`。
- L103-L111：`attn = AttentionLayer(...)`，把 `layer_id`、头数、rotary 配置、以及上面两个 norm 一并传入。`AttentionLayer` 持有这些只是「为了 forward 时用」，**不产生权重**。
- L112-L116：`o_proj = LinearOProj(...)`，输出投影。

`forward`（L118-L123）：标准三步 `qkv_proj → attn → o_proj`，`@nvtx_annotate("MHA")` 打标签。

再看 `AttentionLayer` 的构造如何把「全局 TP 头数」转成「本卡头数」：

[attention.py:L18-L45](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/attention.py#L18-L45)：L32 取 `tp_size`；L33 `num_qo_heads = div_even(num_qo_heads, tp_size)`（Q 头必须能整除，不能复制）；L34 `num_kv_heads = div_even(num_kv_heads, tp_size, allow_replicate=True)`（KV 头允许复制，解决 GQA 头数少于卡数的问题）；L35-L36 据此算出本卡的 q/k/v 维度。L37-L43 用 `rotary_config` 建一个 `rotary` 算子（RoPE 的 cos/sin 表）。

[attention.py:L47-L57](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/attention.py#L47-L57) 的 `forward` 就是 4.4.2 伪代码的真实落点：split → (可选 qk norm 原地) → rotary → backend.forward → reshape。

#### 4.4.4 代码实践

**实践目标**：搞清 `has_qk_norm` 开关如何改变 `RopeAttn` 的 state_dict，以及 q/k norm 归一化的维度。

**操作步骤**（源码阅读型）：

1. 打开 [utils.py:L79-L123](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/utils.py#L79-L123)，分别假设 `has_qk_norm=False` 与 `has_qk_norm=True`，写出 `RopeAttn` 一个实例的 state_dict 键名（结合 4.1 的规则）。
2. 打开 [norm.py:L8-L20](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/norm.py#L8-L20)，确认 `RMSNorm` 的 `self.weight = torch.empty(size)` 里 `size` 是什么——在 `RopeAttn` 里它是 `head_dim`（[utils.py:L98-L99](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/utils.py#L98-L99)），而 `LlamaDecoderLayer` 里的 `input_layernorm` 是 `hidden_size`（[llama.py:L22-L29](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/models/llama.py#L22-L29)）。理解「同一个 RMSNorm 类，归一化维度可变」。

**需要观察的现象**：

- `has_qk_norm=False`：键名只有 `qkv_proj.weight`、`o_proj.weight`。
- `has_qk_norm=True`：多出 `q_norm.weight`、`k_norm.weight`，且 shape 是 `(head_dim,)` 而非 `(hidden_size,)`。
- `attn` 子对象（`AttentionLayer`，`StateLessOP`）**不贡献任何键**，即使它内部引用了 `q_norm`/`k_norm`——因为同一对象已在 `RopeAttn` 的 `self.q_norm`/`self.k_norm` 下被收集过一次，而 `AttentionLayer` 是无状态的（不会重复收集）。这同时印证了 4.1 里 `StateLessOP` 的作用。

**预期结果**：能写出两种开关下的键名差异，并解释为什么 `attn.q_norm` 不会导致 `q_norm.weight` 重复出现。

> 待本地验证：可在 CPU 上构造一个 `RopeAttn(config, 0, has_qk_norm=True)`（需 mock 一个 `ModelConfig` 与 TP 上下文），打印 `state_dict()` 键名核对。

#### 4.4.5 小练习与答案

**练习 1**：`AttentionLayer` 持有 `self.q_norm`（指向 `RopeAttn` 传入的 RMSNorm），而 `RopeAttn` 也持有 `self.q_norm` 指向**同一个对象**。state_dict 里 `q_norm.weight` 会出现几次？

> **答案**：只出现一次，键名为 `q_norm.weight`（在 `RopeAttn` 下）。原因是 `AttentionLayer` 继承 `StateLessOP`，它的 `state_dict` 恒返回空、`load_state_dict` 不递归子 BaseOP——所以它「看不见」自己引用的 `q_norm`。权重唯一的所有者是 `RopeAttn`。这种「同一对象两处引用、但只有一个所有者负责序列化」正是 `StateLessOP` 的价值。

**练习 2**：为什么 q/k norm 的归一化维度是 `head_dim` 而不是 `hidden_size`？

> **答案**：因为 RoPE 与注意力都是「按 head 独立」计算的，每个 head 是一个 head_dim 维的子空间。q/k norm 想稳定的是「每个 head 内部」的数值分布，所以归一化要在 head_dim 上做（见 [attention.py:L51-L53](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/attention.py#L51-L53) 先 `view(-1, num_heads, head_dim)` 再 norm）。而层输入 norm 稳定的是整条 hidden 向量，所以在 hidden_size 上做。

**练习 3**：`RopeAttn.forward` 里 RoPE 作用在 q 和 k 上，为什么**不**作用在 v 上？

> **答案**：RoPE 的目的是让 `Q·K^T` 只依赖相对位置——位置信息通过 q、k 的旋转「对消」进内积里。V 不参与注意力分数的计算（只在内积之后被加权求和），给它加位置信息没有数学意义。所以标准实现只旋转 q/k。

---

## 5. 综合实践：对照 nn.Module，盘点 BaseOP 的「功能缺口」

本实践把本讲四个模块串起来，并直接完成规格里指定的实践任务。

### 实践目标

用一个表格，系统列出 `BaseOP` 相比 `torch.nn.Module` **缺少的功能**，并对每一项说明：**在纯推理场景下，这个缺口是否可接受、为什么**。这将帮助你理解 Mini-SGLang 「自造骨架」这一架构决策的边界。

### 操作步骤

1. **准备对照基准**。回忆或查阅 `torch.nn.Module` 提供的能力，至少包括：自动求导（autograd）、`parameters()` / `named_parameters()`、`register_buffer` / `register_parameter`、`register_forward_hook`、`.to()` / `.cuda()` / `.half()`、`.train()` / `.eval()`、`state_dict` / `load_state_dict`、子模块自动注册（`__setattr__` 拦截）、`apply(fn)` 等。
2. **逐项核对本讲源码**。打开 [layers/base.py](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/layers/base.py)，确认 `BaseOP` 只实现了 `state_dict` / `load_state_dict` / `forward`（抽象）。把上面那些能力逐一标注「有 / 无」。
3. **填表**。按下表格式产出一份 Markdown 表格（写到你的学习笔记里，不写进项目源码）：

   | nn.Module 能力 | BaseOP 是否有 | 推理场景下是否需要 | 理由 |
   | --- | --- | --- | --- |
   | autograd 反向传播 | 无 | 不需要 | 推理只前向，全程在 `@torch.inference_mode` 下，不需要计算图 |
   | `parameters()` 迭代器 | 无 | 不需要 | 权重通过 `state_dict` 整体装载，无需逐个遍历 |
   | `register_buffer` | 无 | 不需要 | buffer（如 RoPE 的 cos/sin 表）直接存为普通属性，不进 state_dict 即可 |
   | forward / backward hook | 无 | 不需要 | 不做调试插桩、不做梯度修改 |
   | `.to()/.cuda()/.half()` | 无 | 不需要 | 设备/dtype 在建图时由 `torch.device("meta")` + `torch_dtype` 上下文统一设定，权重装载时已是目标 dtype |
   | `.train()/.eval()` | 无 | 不需要 | 没有 dropout/batchnorm 这类训练期行为 |
   | `state_dict`/`load_state_dict` | **有** | **需要** | 装载权重的唯一通路 |
   | 子模块自动注册 | 半有 | 够用 | 不靠 `__setattr__` 拦截，而靠 `__dict__` + `isinstance` 反射，效果等价 |
   | `apply(fn)` 递归 | 无 | 不需要 | 不需要批量改模块 |

4. **用本讲的三个实例验证你的判断**：`GatedMLP`（4.3）、`RopeAttn`（4.4）、`LlamaForCausalLM`（4.2）——确认它们**确实没有用到** autograd、`.to()`、hook 等能力，而只依赖 `state_dict`/`load_state_dict` + `forward`。可以在 [engine.py:L48-L52](https://github.com/sgl-project/mini-sglang/blob/9a91cfafe754aa85daee49998176275667eb58f2/python/minisgl/engine/engine.py#L48-L52) 看到：模型建出来后只调一次 `load_state_dict`，之后 Engine 在 `@torch.inference_mode` 下只反复调 `forward`。
5. **写一段结论**（3-5 句）：回答「在纯推理场景下，BaseOP 为何仍然足够？」

### 需要观察的现象 / 预期结果

- 你的表格应当显示：`BaseOP` **只保留**了「权重装载」与「forward 契约」两项核心能力，其余训练相关能力全部缺失。
- 三个模型实例的源码里，找不到任何 `super().__init__()` 之外对 `nn.Module` 特性的依赖（事实上它们根本不继承 `nn.Module`）。
- 结论应当指出：**推理引擎不需要训练能力，自造骨架换来的是更小的内存 footprint、更可预测的权重装载契约（严格匹配）、以及与 meta device 建图无缝配合的 `setattr` 覆盖机制**。

> 待本地验证：第 4 步可以用 `grep` 在 `models/` 与 `layers/` 下搜索 `autograd`、`backward`、`.to(`、`register_buffer`、`train()` 等关键词，确认确实没有训练路径调用它们（注意区分「模型层」与「kernel/通信」层，后者可能用到 `.to()` 做 tensor 搬运，那不属于 nn.Module 能力）。

## 6. 本讲小结

- Mini-SGLang **不使用 `torch.nn.Module`**，而是用约 100 行的 `BaseOP` / `StateLessOP` / `OPList` 自建模型骨架，只保留「权重装载」这一推理必需能力，换取更低的内存开销与严格的权重匹配契约。
- `BaseOP` 的 `state_dict` / `load_state_dict` 靠 **`__dict__` 反射 + 递归前缀拼接**实现；用 `isinstance` 区分「Tensor 叶子 / BaseOP 子树」，用**下划线前缀**跳过内部字段；`load_state_dict` 用 `pop` 消费字典、末尾校验「无多余 key」，形成「结构 ⇄ 权重」的严格契约。
- `StateLessOP` 是「无权重」减法（如 `AttentionLayer`），`OPList` 是「有序子层」加法（如 32 个 decoder layer 用整数下标 `layers.0/1/...` 命名）。
- `LlamaForCausalLM` 是标准 decoder-only 架构：`embed_tokens → N×(attn+mlp) → norm → lm_head`；`forward` 不收参数，从全局上下文 `get_global_ctx().batch` 取输入；残差跨层单条传递，靠 `RMSNormFused` 做残差融合。
- `GatedMLP`（SwiGLU）把 gate/up 合并成 `gate_up_proj`（列并行）一次 GEMM 算两路，经 `silu_and_mul` 门控后由 `down_proj`（行并行 + 一次 all_reduce）还原；全程 `del` 中间张量做显存卫生。
- `RopeAttn` 把 qkv 合并投影、可选的 q/k norm（按 head_dim 归一化、Qwen3 启用）、RoPE（只作用 q/k）、注意力后端调用、输出投影串成一块；注意力计算本身外包给 `attn_backend`，实现「位置编码」与「注意力后端」解耦。

## 7. 下一步学习建议

- 想搞清「HF 的 `q_proj/k_proj/v_proj` 三个权重如何被合并成运行时的 `qkv_proj`、并按 TP 分片」？继续读 [u8-l2 配置解析与权重加载分片](u8-l2-config-weight-loading.md)，那里讲 `ModelConfig.from_hf` 与 `load_weight` 的合并/分片规则。
- 想深入「列并行 / 行并行 / GQA 复制 / all_reduce」的通信细节？读 [u9-l1 张量并行 Linear 与分布式通信](u9-l1-linear-tp-distributed.md)。
- 想理解词表并行 embedding、RMSNormFused 的残差融合、AttentionLayer 完整前向？读 [u9-l2 Embedding / Norm / RoPE 与 AttentionLayer](u9-l2-embedding-norm-rope-attention.md)。
- 想看 MoE（`MoEMLP`）的完整实现？读 [u10-l1 MoE 后端（Fused MoE）](u10-l1-moe-fused.md)。
- 想亲手为一个新的 decoder 架构接入 Mini-SGLang？读 [u10-l3 支持新模型架构](u10-l3-add-new-model.md)，那里以 `register.py` + config + llama/qwen3 为模板给出接入 checklist。
