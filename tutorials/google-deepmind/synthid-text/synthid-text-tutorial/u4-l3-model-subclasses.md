# 子类化 Gemma 与 GPT-2

## 1. 本讲目标

本讲要回答一个听起来很简单、却藏着关键设计的问题：

> 为什么只写两行 `pass`，就能让一个普通的 HuggingFace 模型「自动带水印」？

学完本讲你应该能够：

- 说清 `SynthIDGPT2LMHeadModel` 与 `SynthIDGemmaForCausalLM` 这两个「空壳类」到底做了什么。
- 解释 Python 多重继承与方法解析顺序（MRO）是如何把水印注入到 `model.generate(...)` 的。
- 掌握「`from_pretrained` → `generate`」这一从加载到生成的最小端到端用法，以及它与原模型的细微 API 差异。
- 理解为什么 GPT-2 和 Gemma 两个完全不同的模型可以共用同一套水印 API。

本讲不重复讲解水印施加的内部数学（已在 u3 系列）和 `_sample` 循环的逐行实现（已在 u4-l2），而是站在「组合 / 装配」的角度，把 Mixin 和具体模型「焊接」在一起。

## 2. 前置知识

在进入源码前，先建立三个直觉。如果你已经熟悉，可以跳过本节。

### 2.1 Mixin 模式

**Mixin（混入）** 是一种「只提供能力、不提供完整实现」的类。它本身不能独立工作，必须和另一个「主干类」一起被继承。打个比方：transformers 的 `GPT2LMHeadModel` 是一台「裸车」，`SynthIDSparseTopKMixin` 是一个「可选的水印套件」。把两者多重继承组合起来，就得到一台「自带水印套件的车」，而车的发动机（权重、前向计算）完全没变。

### 2.2 方法解析顺序（MRO）

当子类有多个父类，且它们定义了同名方法时，Python 用 **C3 线性化** 算法决定「先找哪个父类的方法」。规则的核心一句话：

> 写在 **前面** 的父类，优先级更高。

所以 `class Child(Mixin, BaseModel)` 中，`Mixin` 的方法会「盖过」`BaseModel` 里的同名方法。本讲的水印注入，正是靠这一条规则。

### 2.3 `from_pretrained` 用的是 `cls`

HuggingFace 的 `from_pretrained` 是定义在 `PreTrainedModel` 上的 **类方法**，它内部用 `cls(...)` 来实例化对象。这意味着：你调用 `SynthIDGPT2LMHeadModel.from_pretrained('gpt2')` 时，被实例化的是 `SynthIDGPT2LMHeadModel`（而不是普通 `GPT2LMHeadModel`），加载的权重则完全相同。这是「换类名即换能力、权重零成本」的根本原因。

## 3. 本讲源码地图

本讲只涉及两个文件，但它们的角色截然不同：

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `src/synthid_text/synthid_mixin.py` | 水印侧核心（PyTorch） | 末尾两个空壳子类（396–405 行）；以及它们继承的 `SynthIDSparseTopKMixin`（70 行） |
| `notebooks/synthid_text_huggingface_integration.ipynb` | 端到端示例 | `load_model` 函数（按开关选择带/不带水印的类）、`generate` 调用 |

源码地图回顾（见 u1-l3）：水印侧全部用 **PyTorch**，本讲的两个子类都建立在 `transformers` 的 PyTorch 模型之上，因此与检测侧（JAX）无直接关系。

## 4. 核心概念与源码讲解

### 4.1 SynthIDGPT2LMHeadModel：多重继承的「魔法」

#### 4.1.1 概念说明

`SynthIDGPT2LMHeadModel` 是把 SynthID 水印能力挂到 GPT-2 上的「成品类」。它解决的问题是：

> 水印逻辑（`_sample`、`_get_logits_warper`）已经在 `SynthIDSparseTopKMixin` 里写好了，怎么让用户在调用 `model.generate(...)` 时**无感地**用上它，而不必改 transformers 源码？

答案就是：写一个继承了「Mixin + 官方模型」的子类。用户只要把类名从 `GPT2LMHeadModel` 换成 `SynthIDGPT2LMHeadModel`，其余代码一行不改，生成出来的文本就带上了水印。

#### 4.1.2 核心流程

水印注入的完整链路如下（关键在于「谁的方法被调用」）：

```text
用户调用 model.generate(do_sample=True, ...)
        │
        │  generate 来自 GenerationMixin，本讲子类并未重写它
        ▼
generate 内部调用 self._get_logits_warper(...)
        │
        │  MRO 让 Mixin 的版本胜出（而非 transformers 默认版本）
        ▼
Mixin._get_logits_warper 返回 [SynthIDLogitsProcessor]（长度恒为 1）
        │
        ▼
generate 内部调用 self._sample(...)
        │
        │  MRO 让 Mixin 的版本胜出
        ▼
Mixin._sample 在采样循环里调用 watermarked_call(...) 施加水印
        │
        ▼
返回带水印的 token 序列
```

注意三个要点：

1. 子类 **没有重写 `generate`**，也没有重写前向传播 `forward`——它只是改变了 `generate` 依赖的两个「钩子方法」的解析结果。
2. Mixin 必须写在继承列表的 **第一个**，否则 MRO 会让 transformers 默认的 `_sample` 胜出，水印失效。
3. 整个机制是 **声明式** 的：能力来自继承顺序，而非方法体内的代码。

#### 4.1.3 源码精读

先看这个子类本身——它的方法体只有一句 `pass`：

[子类定义 synthid_mixin.py:396-399](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L396-L399) —— `SynthIDGPT2LMHeadModel(SynthIDSparseTopKMixin, transformers.GPT2LMHeadModel)`，方法体为空。

```python
class SynthIDGPT2LMHeadModel(
    SynthIDSparseTopKMixin, transformers.GPT2LMHeadModel
):
  pass
```

这句 `pass` 正是本讲的「主角」。它没有新增任何字段或方法，全部能力来自两个父类：

- 第一个父类 [SynthIDSparseTopKMixin（synthid_mixin.py:70）](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L70) 继承自 `transformers.GenerationMixin`，提供了两个被改写的钩子：`_get_logits_warper`（[第 85–127 行](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L85-L127)）与 `_sample`（[第 129 行起](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L129)）。
- 第二个父类 `transformers.GPT2LMHeadModel` 提供了完整的 GPT-2 实现：权重结构、`forward`、`from_pretrained` 等。

因为 `SynthIDSparseTopKMixin` 排在前面，按 MRO 规则，`self._get_logits_warper` 与 `self._sample` 会解析到 **Mixin 的版本**，而 `forward`、`from_pretrained` 等只有 transformers 提供的方法则照常来自第二个父类。这就是「声明即生效」的全部秘密。

> 术语提示：**MRO（Method Resolution Order）** 是 Python 查找属性/方法时的线性顺序。对 `SynthIDGPT2LMHeadModel` 而言，`SynthIDSparseTopKMixin` 排在 `GPT2LMHeadModel` 之前（由 C3 线性化和继承列表顺序共同保证），所以 Mixin 的覆盖一定生效。

#### 4.1.4 代码实践

**实践目标**：用 Python 内省工具亲眼确认「Mixin 的方法确实盖过了 transformers 的方法」，而不是停留在口头结论。

**操作步骤**：

1. 在已安装 `synthid-text` 的环境中运行：

   ```python
   # 示例代码：仅做内省，不需要加载权重或 GPU
   from synthid_text import synthid_mixin

   cls = synthid_mixin.SynthIDGPT2LMHeadModel

   # 1) 打印 MRO 前几个类，确认 SynthIDSparseTopKMixin 排在 GPT2LMHeadModel 之前
   print([c.__name__ for c in cls.__mro__][:6])

   # 2) 确认 _sample 与 _get_logits_warper 来自 Mixin，而非 transformers
   print("_sample 来自:", cls._sample.__qualname__)
   print("_get_logits_warper 来自:", cls._get_logits_warper.__qualname__)
   ```

2. 思考一个问题：如果把继承顺序写成 `class X(transformers.GPT2LMHeadModel, SynthIDSparseTopKMixin)`，第 2 步的两个 `__qualname__` 会变成什么？

**需要观察的现象**：

- 第 1 步打印结果中，`SynthIDSparseTopKMixin` 出现在 `GPT2LMHeadModel` 之前。
- 第 2 步的 `__qualname__` 应形如 `SynthIDSparseTopKMixin._sample` / `SynthIDSparseTopKMixin._get_logits_warper`，证明方法确实定义在 Mixin 中。

**预期结果**：水印钩子方法解析到 Mixin。若顺序写反，`_sample` 会解析到 transformers 的版本（`GenerationMixin._sample` 之类），水印将完全不生效。

> 待本地验证：MRO 的完整列表会随你安装的 `transformers` 版本略有差异（例如 `GenerationMixin` 是否在 `PreTrainedModel` 链中），但「Mixin 先于具体模型」这一关键顺序在所有版本下都成立。

#### 4.1.5 小练习与答案

**练习 1**：子类方法体只有 `pass`，那它和「直接用 Mixin」相比，多了什么？

> **参考答案**：直接用 `SynthIDSparseTopKMixin` 没有 GPT-2 的权重结构与 `forward`，无法生成文本。子类把 Mixin 的水印能力和 `GPT2LMHeadModel` 的完整模型实现「焊接」在一起，既能动（有权重、能前向），又带水印（`generate` 走改写后的采样循环）。

**练习 2**：为什么不能写成 `class SynthIDGPT2LMHeadModel(transformers.GPT2LMHeadModel, SynthIDSparseTopKMixin)`？

> **参考答案**：这样 `GPT2LMHeadModel` 排在前面，MRO 会让 transformers 默认的 `_sample` / `_get_logits_warper` 胜出，水印钩子永远不会被调用。即使代码不报错，生成结果也不会带水印——这种「静默失效」比报错更危险。

### 4.2 SynthIDGemmaForCausalLM：换一个模型，同一套水印

#### 4.2.1 概念说明

`SynthIDGemmaForCausalLM` 把同样的水印能力挂到 Gemma 模型上。本节要回答一个自然产生的疑问：

> GPT-2 和 Gemma 是完全不同的两个模型（架构、词表、张量名都不同），为什么水印代码可以原封不动地复用？

答案揭示了一个重要设计：**水印逻辑与具体模型解耦**。`SynthIDSparseTopKMixin` 只依赖 `transformers.GenerationMixin` 这一层「采样协议」，从不直接访问任何 GPT-2 或 Gemma 的私有结构。因此，只要一个模型是 transformers 的因果语言模型（遵守同一套 `generate` / `_sample` 协议），就能用同样一行 `pass` 接入水印。

#### 4.2.2 核心流程

`SynthIDGemmaForCausalLM` 的装配流程与 4.1 完全对称，只是「第二个父类」换成了 Gemma：

```text
SynthIDGemmaForCausalLM
        ├── SynthIDSparseTopKMixin      （第一个父类，提供水印钩子，与 4.1 完全相同）
        └── transformers.GemmaForCausalLM （第二个父类，提供 Gemma 的权重/forward/from_pretrained）
```

由于 Mixin 不引用任何 Gemma 特有的符号，整个水印施加流程（温度缩放 → 稀疏 top_k → ngram key → g 值 → `update_scores`，详见 u3 系列）对两个模型而言是一字不差的。

#### 4.2.3 源码精读

对比两个子类的定义，可以看到它们在结构上**逐字符对称**：

[子类定义 synthid_mixin.py:402-405](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L402-L405) —— `SynthIDGemmaForCausalLM(SynthIDSparseTopKMixin, transformers.GemmaForCausalLM)`，方法体同样为空。

```python
class SynthIDGemmaForCausalLM(
    SynthIDSparseTopKMixin, transformers.GemmaForCausalLM
):
  pass
```

把它和 [4.1.3 的 GPT-2 子类（synthid_mixin.py:396-399）](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L396-L399) 放在一起：

| 维度 | GPT-2 子类 | Gemma 子类 |
| --- | --- | --- |
| 第一个父类 | `SynthIDSparseTopKMixin` | `SynthIDSparseTopKMixin`（相同） |
| 第二个父类 | `transformers.GPT2LMHeadModel` | `transformers.GemmaForCausalLM` |
| 方法体 | `pass` | `pass`（相同） |
| 提供水印的方式 | MRO 覆盖 `_sample`/`_get_logits_warper` | 完全相同 |

这就解释了「两种模型共享同一套水印 API」：共享的不是巧合，而是因为水印逻辑全部封装在同一个模型无关的 Mixin 里，两个子类只是把 Mixin 分别焊到不同的官方模型上。

> 提醒：在 Notebook 中加载 Gemma 时用了 `torch_dtype=torch.bfloat16`（见 4.3.3），这是 Gemma 模型推荐的精度，与水印本身无关，但会影响显存与生成质量。

#### 4.2.4 代码实践

**实践目标**：确认两个子类共享同一份水印实现，体会「模型无关」。

**操作步骤**：

```python
# 示例代码：内省，无需加载权重
from synthid_text import synthid_mixin

a = synthid_mixin.SynthIDGPT2LMHeadModel
b = synthid_mixin.SynthIDGemmaForCausalLM

# 比较两个子类的 _sample 是否为同一个函数对象
print("_sample 是否相同:", a._sample is b._sample)
print("_get_logits_warper 是否相同:", a._get_logits_warper is b._get_logits_warper)
```

**需要观察的现象**：两个 `is` 判断都应为 `True`。

**预期结果**：因为两个子类都没有重写这两个方法，它们解析到的都是 `SynthIDSparseTopKMixin` 里同一个函数对象。这正是「同一套水印」的字面证据。

#### 4.2.5 小练习与答案

**练习 1**：如果未来想给 Llama 也加上 SynthID 水印，照这个模式应该怎么写？

> **参考答案**：只需新增一个空壳子类，例如 `class SynthIDLlamaForCausalLM(SynthIDSparseTopKMixin, transformers.LlamaForCausalLM): pass`，前提是 `LlamaForCausalLM` 遵守与 `generate` / `_sample` 相同的采样协议，且其版本与 Mixin 改写的 `_sample` 兼容（参考实现锁定了 transformers 版本，见 u1-l2）。不需要改 Mixin 任何代码。

**练习 2**：为什么 SynthID 没有直接用「猴子补丁」（运行时替换 `GPT2LMHeadModel._sample`）来实现水印？

> **参考答案**：猴子补丁会全局修改 `GenerationMixin` 或模型类，影响所有模型、所有代码路径，且难以撤销和测试。多重继承 + Mixin 是**选择性启用**的——只有显式使用 `SynthID*` 子类的代码才会带水印，普通 `GPT2LMHeadModel` 的行为完全不受影响（这在 Notebook 里被用来同时生成带水印与不带水印的对照样本，见 4.3.3）。

### 4.3 from_pretrained + generate：端到端用法

#### 4.3.1 概念说明

前两节讲了「类是怎么定义的」，本节讲「类是怎么用的」。核心结论是：

> 用 SynthID 子类加载和生成，与用原模型几乎完全一样——**唯一可见的差异是类名**，以及为保证水印有效而必须遵守的几条采样参数约束。

Notebook 里的 `load_model` 函数把这套用法封装得很清楚：它用一个 `enable_watermarking` 开关，在「带水印子类」与「原模型」之间切换，其余加载逻辑分毫不差。

#### 4.3.2 核心流程

端到端使用分为三步：

1. **选类**：根据是否要水印，选择 `SynthIDGPT2LMHeadModel` 或普通 `GPT2LMHeadModel`（Gemma 同理选 `SynthIDGemmaForCausalLM` 或 `GemmaForCausalLM`）。
2. **加载**：对选中的类调用 `from_pretrained(...)`，加载官方权重——因为 `from_pretrained` 用 `cls` 实例化，所以 SynthID 子类加载到的就是带水印能力的实例，权重与原模型完全一致。
3. **生成**：调用 `.generate(do_sample=True, temperature=..., top_k=..., ...)`。注意 `do_sample=True` 对水印是**强制**的，贪心解码会让水印失效（见 4.3.4）。

#### 4.3.3 源码精读

Notebook 的 `load_model` 是本节最关键的参考代码（[notebooks/synthid_text_huggingface_integration.ipynb](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/notebooks/synthid_text_huggingface_integration.ipynb)）。摘录其核心：

```python
# 示例代码：摘自 Notebook 的 load_model，仅保留关键逻辑
def load_model(model_name, expected_device, enable_watermarking=False):
  if model_name == ModelName.GPT2:
    # 用三元表达式在「带水印子类」与「原模型」之间切换
    model_cls = (
        synthid_mixin.SynthIDGPT2LMHeadModel
        if enable_watermarking
        else transformers.GPT2LMHeadModel
    )
    model = model_cls.from_pretrained(model_name.value, device_map='auto')
    model.generation_config.pad_token_id = model.generation_config.eos_token_id
  else:
    model_cls = (
        synthid_mixin.SynthIDGemmaForCausalLM
        if enable_watermarking
        else transformers.GemmaForCausalLM
    )
    # Gemma 额外指定 bfloat16 精度
    model = model_cls.from_pretrained(
        model_name.value, device_map='auto', torch_dtype=torch.bfloat16,
    )
  ...
  return model
```

注意几个要点：

- `from_pretrained` 的调用方式在带水印与不带水印时**完全一致**（GPT-2 都是 `device_map='auto'`；Gemma 都是 `device_map='auto'` + `torch_dtype=torch.bfloat16`）。差异只在 `model_cls` 这一个变量上。这正是本讲反复强调的「换类名即换能力、权重零成本」。
- GPT-2 单独设了 `pad_token_id = eos_token_id`，这是 GPT-2 tokenizer 没有 pad token 的常规处理，与水印无关。
- Gemma 用 `bfloat16` 降低显存，因为 Gemma 体量更大。

加载完成后，生成调用同样简洁（摘自 Notebook「Generate watermarked output」单元格）：

```python
# 示例代码：摘自 Notebook，带水印生成
model = load_model(MODEL_NAME, expected_device=DEVICE, enable_watermarking=True)
torch.manual_seed(0)
outputs = model.generate(
    **inputs,
    do_sample=True,   # 水印强制要求采样
    temperature=0.7,
    max_length=1024,
    top_k=40,
)
```

这段 `generate` 调用和你在普通 GPT-2 上写的代码几乎没有区别。真正发生的水印注入，全部隐藏在 [Mixin 改写的 `_sample`（synthid_mixin.py:129 起）](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L129) 内部（详见 u4-l2）。

#### 4.3.4 代码实践

**实践目标**：写出用 `SynthIDGPT2LMHeadModel` 生成水印文本的最小代码片段，并说明它与普通 `GPT2LMHeadModel` 的 API 差异（可不实际运行）。

**操作步骤**：

```python
# 示例代码：最小水印生成片段（GPT-2）
import torch
import transformers
from synthid_text import synthid_mixin

tokenizer = transformers.AutoTokenizer.from_pretrained("gpt2")
tokenizer.pad_token = tokenizer.eos_token
tokenizer.padding_side = "left"

inputs = tokenizer("I enjoy walking with my cute dog", return_tensors="pt")

# 关键：用 SynthID 子类替代普通 GPT2LMHeadModel
model = synthid_mixin.SynthIDGPT2LMHeadModel.from_pretrained("gpt2")
model.generation_config.pad_token_id = model.generation_config.eos_token_id

torch.manual_seed(0)
outputs = model.generate(
    **inputs,
    do_sample=True,      # ① 必须为 True
    temperature=0.7,     # ② 必须落在 (0, 1]
    top_k=40,            # ③ 必须 >= 2
    max_length=64,
)

print(tokenizer.decode(outputs[0], skip_special_tokens=True))
```

**与普通 `GPT2LMHeadModel` 的 API 差异**（这是本实践要回答的核心）：

1. **类名不同**：`GPT2LMHeadModel` → `SynthIDGPT2LMHeadModel`，其余（`from_pretrained`、`generate`、`tokenizer.decode`）签名一致。
2. **`do_sample=True` 实际成为强制项**：在 Mixin 的 `_sample` 中，[第 335 行的 `assert indices_mapping is not None`](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_mixin.py#L335) 位于 `if do_sample:` 块之外；只有采样分支（[第 281–298 行](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L281-L298)）才会给 `indices_mapping` 赋值。所以若 `do_sample=False`（贪心），断言会失败——贪心路径不施加水印。
3. **采样参数取值更严**：`temperature` 须经 [Mixin 的两层校验（synthid_mixin.py:107-114）](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L107-L114)（落在 `[0,1]`，processor 进一步要求 `>0`），`top_k` 须 [>= 1（synthid_mixin.py:118-125）](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L118-L125)（processor 进一步要求 `>1`）。非法取值会在生成前抛 `ValueError`。
4. **`top_p` 等参数被静默忽略**：`generate(top_p=0.99)` 不会报错，但 [Mixin 的 `_get_logits_warper` 只构造 `SynthIDLogitsProcessor` 一个 warper](https://github.com/google-deepmind/synthid-text/blob/addb4a158143c7c6851a1308f78b89fceed59683/src/synthid_text/synthid_mixin.py#L85-L127)，不构造 `TopPWarper`，所以 `top_p` 实际不生效。

**需要观察的现象**（若实际运行）：生成文本应通顺（水印对分布的扭曲很小）；若把 `do_sample` 改成 `False`，应在断言处报错；若把 `temperature=0.0`，应在生成前抛 `ValueError`。

**预期结果**：上述非法配置都会在生成阶段被拦下，而不会产生「看似成功却没水印」的迷惑性输出。

> 提示：这段代码与 Notebook 的「Generate watermarked output」单元格本质相同，仅把 `max_length` 调小以便快速验证。

#### 4.3.5 小练习与答案

**练习 1**：Notebook 里为什么能同时拿到「带水印」和「不带水印」的样本做对比（例如比较 perplexity）？

> **参考答案**：因为水印是**选择性启用**的——`load_model(enable_watermarking=True)` 用 `SynthIDGPT2LMHeadModel`，`enable_watermarking=False` 用普通 `GPT2LMHeadModel`，两者加载相同权重、占用相同的生成接口，只是前者走了带水印的 `_sample`。这让对照实验只需切换一个布尔参数。

**练习 2**：为什么 SynthID 把 `top_p` 设计成「静默忽略」而不是直接报错？这种设计有什么隐患？

> **参考答案**：`_get_logits_warper` 的签名是 `(self, generation_config, **unused_kw)`，它只读取 `temperature` 和 `top_k`，其余采样参数不构造对应 warper，于是 `top_p` 自然失效。「静默忽略」的好处是与 HF 默认 `generate` 调用兼容（用户传了 `top_p` 也不会崩）；隐患是用户可能误以为 `top_p` 生效，而实际采样分布只受 `temperature` 与 `top_k`（水印）控制。这是使用 SynthID 子类时必须知晓的语义差异。

## 5. 综合实践

把本讲三节串起来，完成一个「封装带开关的水印生成器」的小任务：

**任务**：参考 Notebook 的 `load_model`，自己写一个 `generate_text(prompt, enable_watermarking)` 函数，要求：

1. 内部根据 `enable_watermarking` 选择 `SynthIDGPT2LMHeadModel` 或 `transformers.GPT2LMHeadModel`（**只用 GPT-2**，确保本地可跑）。
2. 用 `from_pretrained("gpt2", device_map="auto")` 加载，并设好 `pad_token_id`。
3. 在 `generate` 中设置正确的采样参数（`do_sample=True`、合法的 `temperature` 与 `top_k`）。
4. 返回解码后的字符串。

**自检清单**：

- [ ] 当 `enable_watermarking=True` 时，传 `do_sample=False` 会不会报错？为什么？（对照 4.3.4 第 2 点）
- [ ] 当 `enable_watermarking=False` 时，传 `top_p=0.9` 是否生效？与 `True` 时有何不同？（对照 4.3.5 练习 2）
- [ ] 你的函数里，带水印与不带水印两个分支的 `from_pretrained` 调用是否完全一致？（应当一致）

> 若无法运行，可只做设计：把函数骨架写出来，并在注释里标明「这一步为何这样写」，重点说清类名选择与采样参数约束。

## 6. 本讲小结

- `SynthIDGPT2LMHeadModel` 与 `SynthIDGemmaForCausalLM` 都是 **方法体为 `pass` 的空壳子类**，全部能力来自继承。
- 水印注入靠 **MRO**：`SynthIDSparseTopKMixin` 必须写在继承列表第一位，使其 `_get_logits_warper` 与 `_sample` 盖过 transformers 默认实现。
- `from_pretrained` 是用 `cls` 实例化的类方法，所以 SynthID 子类加载到的实例**权重与原模型完全相同**，只是多了水印能力——「换类名即换能力、权重零成本」。
- 两种模型共享同一套水印 API，是因为水印逻辑全部封装在 **模型无关** 的 `SynthIDSparseTopKMixin` 中，两个子类只是把它焊到不同官方模型上。
- 端到端用法与原模型几乎一致，**唯一可见差异是类名**；但水印强制 `do_sample=True`，并对 `temperature`/`top_k` 取值有更严校验，`top_p` 等参数会被静默忽略。

## 7. 下一步学习建议

本讲完成了「水印施加侧的装配」——Mixin 与具体模型的焊接。接下来建议：

- **进入检测侧**：水印生成后，如何判断一段文本是否带水印？请学习 u5-l1「检测所需的掩码体系」，了解检测侧如何用 `compute_eos_token_mask` 与 `compute_context_repetition_mask` 构造 `combined_mask`。
- **回顾端到端**：如果对「生成 → 检测」的全局还缺乏直觉，可回到 u1-l4 端到端流程总览，把本讲的子类用法放回整条数据流中理解。
- **进阶扩展**：若想给其他 transformers 模型（如 Llama）接入水印，可参考 4.2.5 练习 1，并注意参考实现锁定的 transformers 版本（见 u1-l2），版本不兼容可能导致 `_sample` 改写失效。
