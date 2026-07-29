# BatchEncoding 与编码/解码/对齐

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `tokenizer(text)` 这一「魔法调用」内部到底做了什么——它是如何把一段字符串变成模型能吃的 `input_ids`、`attention_mask` 的。
- 掌握 `__call__` 的核心编码选项：`padding`（填充）、`truncation`（截断）、`max_length`、`return_tensors`，理解它们各自对应的策略枚举 `PaddingStrategy` / `TruncationStrategy`。
- 理解 `BatchEncoding` 这个返回容器为什么「既能像字典用、又能像对象用、还能当张量用」，以及它内部如何用 `convert_to_tensors` / `as_tensor` 做张量转换。
- 会用快速分词器（fast tokenizer）提供的 `word_ids`、`token_to_chars`、`char_to_token` 等对齐方法，把 token 级输出映射回原始单词或字符，完成 NER（命名实体识别）等任务中常见的标注对齐。

本讲承接 [u3-l1](u3-l1-tokenizer-base.md)：上一讲建立了「`PreTrainedTokenizerBase` 是抽象契约、`__call__` 返回 `BatchEncoding`」的认知，本讲就深入这个 `BatchEncoding` 与 `__call__` 的内部机制。

## 2. 前置知识

- **token 与 id**：分词器把字符串切成一段段子串叫 token，每个 token 在词表里有个整数编号叫 id。`input_ids` 就是 id 序列。
- **batch（批次）**：模型通常一次处理多条样本。把多条长度不一的样本叠成一个规整的矩形张量，就需要「填充」到等长。
- **填充（padding）**：用 `pad_token`（如 `[PAD]`）把短样本补齐到固定长度，配套的 `attention_mask` 标记哪些位置是真 token（1）、哪些是补的（0）。
- **截断（truncation）**：超长样本砍掉一部分，使其不超过模型允许的最大长度 `max_length`。
- **快速 vs 慢速分词器**：快速分词器（`XxxTokenizerFast`）底层是 Rust 的 `tokenizers` 库，除了速度更快，还能记录每个 token 来自原始文本的哪个字符区间（offsets）和哪个单词（word ids）；慢速分词器（Python 实现）则没有这些对齐信息。本讲的「对齐」部分只在快速分词器上可用。

如果你对上述概念还陌生，建议先读 [u3-l1 分词器基础](u3-l1-tokenizer-base.md)。

## 3. 本讲源码地图

本讲几乎全部围绕同一个文件展开：

| 文件 | 作用 |
| --- | --- |
| [src/transformers/tokenization_utils_base.py](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py) | 分词器抽象基类 `PreTrainedTokenizerBase`、返回容器 `BatchEncoding`、策略枚举 `TruncationStrategy`、span 命名元组 `CharSpan`/`TokenSpan`、以及填充的真正实现 `_pad`。本讲的主战场。 |
| [src/transformers/utils/generic.py](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/generic.py) | 策略枚举 `PaddingStrategy` 与 `TensorType`（`pt`/`np`/`mlx`）的定义。 |
| [src/transformers/tokenization_python.py](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_python.py) | 慢速（Python）分词器后端的编码实现，包含截断算法 `truncate_sequences` 与单样本装配 `prepare_for_model`。快速分词器的截断则由 Rust 后端完成，逻辑等价。 |

记住一句话：**「填充策略写在基类里（`_pad`），截断策略写在后端里（`truncate_sequences`）」**，这是理解本讲代码分布的钥匙。

## 4. 核心概念与源码讲解

### 4.1 BatchEncoding 容器：既能当字典、又能当对象、还能当张量

#### 4.1.1 概念说明

每次你调用 `tokenizer(...)`，拿到的返回值就是一个 `BatchEncoding`。它看起来像个字典：

```python
enc = tokenizer("hello world")
print(enc["input_ids"])        # 字典式访问
print(enc.input_ids)           # 属性式访问 —— 也能用！
```

`BatchEncoding` 同时承担两个角色：

1. **数据容器**：存放 `input_ids`、`attention_mask`、`token_type_ids` 等字段，本质是 `UserDict` 的子类。
2. **对齐接口**：如果底层是快速分词器，它还会额外挂载一份 Rust 后端返回的 `Encoding` 对象，提供 token↔word↔char 的对齐方法（4.3 节详解）。

之所以要专门设计这个类而不是直接返回普通 `dict`，正是因为第 2 点——普通字典无法承载「每个 token 来自原始文本哪里」这种偏移信息。

#### 4.1.2 核心流程

`BatchEncoding` 在构造时做三件事：

```
BatchEncoding(data, encoding, tensor_type)
   │
   ├─ 1. super().__init__(data)            # 把 data 存进 self.data（UserDict 的存储）
   ├─ 2. 规整 encoding：单个 -> [单个]       # 统一成列表形式存入 self._encodings
   ├─ 3. 推断 n_sequences（1 句 / 2 句对）   # 来自 encoding[0].n_sequences
   └─ 4. convert_to_tensors(tensor_type)   # 若要求张量，就地把列表转成 torch/np/mlx 张量
```

其中 `_encodings` 是关键：它非空时（即快速分词器），`is_fast` 为真，所有对齐方法才可用。

#### 4.1.3 源码精读

**构造函数**：[`tokenization_utils_base.py:L223-L244`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L223-L244) 把 `encoding`（可能是单个对象）统一成列表存入 `self._encodings`，并在构造末尾调用 `convert_to_tensors`：

```python
self._encodings = encoding
...
self.convert_to_tensors(tensor_type=tensor_type, prepend_batch_axis=prepend_batch_axis)
```

**双模式 `__getitem__`**：[`tokenization_utils_base.py:L264-L284`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L264-L284) 按键的类型分流——字符串键取数据字段、整数键取该样本的 `Encoding`、切片键取数据子集。这就是它能同时当「字典」和「按样本索引」用的原因：

```python
if isinstance(item, str):
    return self.data[item]            # enc["input_ids"]
elif self._encodings is not None:
    return self._encodings[item]      # enc[0] -> 第 0 个样本的 Encoding
elif isinstance(item, slice):
    return {key: self.data[key][item] for key in self.data}  # enc[:2]
```

**属性式访问**：[`tokenization_utils_base.py:L286-L290`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L286-L290) 通过 `__getattr__` 把 `enc.input_ids` 透明转发到 `self.data["input_ids"]`，找不到时把 `KeyError` 转成 `AttributeError`（符合 Python 属性访问语义）。

**`is_fast` 判定**：[`tokenization_utils_base.py:L306-L311`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L306-L311) 一行揭示本质——是否快速分词器，就看 `_encodings` 是否非空：

```python
@property
def is_fast(self) -> bool:
    return self._encodings is not None
```

**张量转换 `convert_to_tensors`**：[`tokenization_utils_base.py:L675-L735`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L675-L735) 是「`return_tensors='pt'` 真正生效」的地方。它按 `tensor_type` 选择不同的 `as_tensor` 闭包：

```python
if tensor_type == TensorType.PYTORCH:
    def as_tensor(value, dtype=None):
        ...
        return torch.tensor(value, dtype=dtype)
elif tensor_type == TensorType.MLX:
    def as_tensor(value, dtype=None):
        return mx.array(value, dtype=dtype)
else:  # numpy 分支
    def as_tensor(value, dtype=None):
        ...  # 处理「参差不齐的列表(ragged)」等边界
        return np.asarray(value, dtype=dtype)
```

随后对 `self` 里每个字段调用 `as_tensor` 就地替换。注意这里的容错提示很关键——[`tokenization_utils_base.py:L754-L765`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L754-L765) 在转换失败时给出的报错正是新手最常遇到的「长度不一无法成张量」，并提示你启用 padding：

> Unable to create tensor, you should probably activate truncation and/or padding with `padding=True` `truncation=True`...

**`to(device)`**：[`tokenization_utils_base.py:L769-L792`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L769-L792) 顺带把所有值搬到指定设备（PyTorch），因此常见写法是 `tokenizer(text, return_tensors="pt").to("cuda")`。

> **张量转换的两种时机**：你既可以在 `__call__` 时传 `return_tensors="pt"` 让 `BatchEncoding` 构造时一次转好（走上面的路径），也可以先拿到列表形式的 `BatchEncoding` 再事后调用 `enc.convert_to_tensors("pt")`——二者最终都调用同一个方法。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `BatchEncoding` 的三种访问方式和 `return_tensors` 的作用。

**操作步骤**：

```python
from transformers import AutoTokenizer

# 用一个公开的小模型；首次会自动下载
tok = AutoTokenizer.from_pretrained("bert-base-uncased")

enc = tok("hello world", return_tensors=None)   # 默认返回 python list
print(type(enc))                                  # transformers.tokenization_utils_base.BatchEncoding
print(enc["input_ids"])                           # 字典式
print(enc.input_ids)                              # 属性式 —— 同样的值
print(enc.keys())                                 # dict_keys(['input_ids', 'token_type_ids', 'attention_mask'])

enc_pt = tok("hello world", return_tensors="pt")
print(type(enc_pt["input_ids"]))                  # <class 'torch.Tensor'>
print(enc_pt["input_ids"].dtype)                  # torch.int64
```

**需要观察的现象**：

1. 不传 `return_tensors` 时，`input_ids` 是普通 `list[int]`；传 `"pt"` 后变成 `torch.Tensor`。
2. `enc["input_ids"]` 与 `enc.input_ids` 完全等价。
3. （可选）尝试 `print(enc_pt[0])`——只有快速分词器才返回 `Encoding` 对象，`bert-base-uncased` 默认加载的是快速版，应能成功。

**预期结果**：`input_ids` 形如 `[101, 7592, 2088, 102]`（`101`/`102` 是 BERT 的 `[CLS]`/`[SEP]` 特殊 token）。若环境无网络，可用本地已有 checkpoint 替换，预期行为不变。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `BatchEncoding` 要继承 `UserDict` 而不是普通 `dict`？
**参考答案**：直接继承 `dict` 会带来两点麻烦——其一，`dict` 的部分内建方法（如 `__init__`、序列化 `pickle`）行为难以定制；其二，`UserDict` 把真正的数据放在内部 `self.data` 属性里，子类可以干净地覆写 `__getattr__`/`__getitem__` 而不与字典协议冲突。`BatchEncoding` 正是借助 `UserDict.data` 来实现属性式访问与 pickle（`__getstate__`/`__setstate__`，见 [`L292-L300`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L292-L300)）。

**练习 2**：不传 `return_tensors` 却直接把 `enc["input_ids"]` 喂给一个需要张量的模型，会发生什么？
**参考答案**：模型 `forward` 期望 `torch.LongTensor`，拿到 python `list` 会报类型错误。解决方式是构造时传 `return_tensors="pt"`，或事后 `enc.convert_to_tensors("pt")`。

---

### 4.2 `__call__` 编码选项：填充、截断与返回张量

#### 4.2.1 概念说明

`__call__` 是分词器最常用的入口，它把「字符串 → 模型可用张量」这条链路一口气做完。它最核心的几组开关是：

| 选项 | 作用 | 取值 |
| --- | --- | --- |
| `padding` | 是否/如何填充 | `True`/`"longest"`（补到批次最长）、`"max_length"`（补到 `max_length`）、`False`/`"do_not_pad"` |
| `truncation` | 是否/如何截断 | `True`/`"longest_first"`、`"only_first"`、`"only_second"`、`False`/`"do_not_truncate"` |
| `max_length` | 填充/截断的目标长度 | 整数；不填则回退到 `model_max_length` |
| `return_tensors` | 返回 python list 还是张量 | `None`、`"pt"`、`"np"`、`"mlx"` |
| `stride` | 溢出 token 的重叠窗口大小 | 配合 `return_overflowing_tokens=True` 使用 |
| `is_split_into_words` | 输入是否已按词切好 | 布尔，影响「list[str]」的歧义消解 |

这些字符串取值背后都对应一个枚举：`PaddingStrategy`（`LONGEST`/`MAX_LENGTH`/`DO_NOT_PAD`，定义于 [`generic.py:L579-L587`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/generic.py#L579-L587)）、`TruncationStrategy`（`ONLY_FIRST`/`ONLY_SECOND`/`LONGEST_FIRST`/`DO_NOT_TRUNCATE`，定义于 [`tokenization_utils_base.py:L154-L163`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L154-L163)）、`TensorType`（`PYTORCH="pt"`/`NUMPY="np"`/`MLX="mlx"`，定义于 [`generic.py:L590-L598`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/utils/generic.py#L590-L598)）。

一个容易踩的坑：**`padding=True` 配合 `truncation` 缺省时，`max_length` 会被忽略并给出警告**。因为「补到批次最长」与「补到固定 max_length」是两种不同的填充策略，二义性必须消除。

#### 4.2.2 核心流程

`__call__` 的整体控制流可以画成：

```
tokenizer(text, padding=..., truncation=..., max_length=..., return_tensors=...)
   │
   ├─ 1. 合并参数：all_kwargs（显式参数 + tokenizer_kwargs + 其他 kwargs）
   │
   ├─ 2. _get_padding_truncation_strategies(...)
   │      ├─ 旧兼容：只给 max_length 则默认开启 longest_first 截断
   │      ├─ padding True -> PaddingStrategy.LONGEST；字符串 -> 对应枚举
   │      ├─ truncation True -> TruncationStrategy.LONGEST_FIRST；字符串 -> 对应枚举
   │      ├─ max_length 缺省 -> 回退 self.model_max_length（若 > 1e20 则关闭该策略）
   │      └─ 校验：要填充却无 pad_token -> 报错
   │
   ├─ 3. _encode_plus(text, padding_strategy, truncation_strategy, max_length, ...)   # 后端实现
   │      └─ 单样本: prepare_for_model -> 加特殊 token -> 截断 -> 填充(_pad) -> BatchEncoding
   │      └─ 批次:  逐条 _encode_plus -> 汇总 -> pad() 对齐 -> BatchEncoding
   │
   └─ 4. 若同时给了 text_target：把目标编码结果塞进 encodings["labels"]
```

要点：**`__call__` 自己不真正做切词和填充，它只负责「解析参数、决定策略、分发到后端」**。真正的切词在后端，填充的真正实现在基类的 `_pad`。

#### 4.2.3 源码精读

**策略解析 `_get_padding_truncation_strategies`**：[`tokenization_utils_base.py:L2346-L2425`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L2346-L2425)。三个关键片段：

向后兼容——只设了 `max_length` 就默认开截断（[`L2355-L2356`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L2355-L2356)）：

```python
if max_length is not None and padding is False and truncation is None:
    truncation = "longest_first"
```

布尔值映射为默认枚举（[`L2359-L2388`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L2359-L2388)）：

```python
if padding is True:
    padding_strategy = PaddingStrategy.LONGEST
...
if truncation is True:
    truncation_strategy = TruncationStrategy.LONGEST_FIRST
```

`max_length` 回退到 `model_max_length`，且若模型最大长度过大（`> LARGE_INTEGER`，即 `1e20`，[`L131`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L131)）则干脆关闭对应策略（[`L2391-L2402`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L2391-L2402)）：

```python
if max_length is None:
    if padding_strategy == PaddingStrategy.MAX_LENGTH:
        if self.model_max_length > LARGE_INTEGER:
            padding_strategy = PaddingStrategy.DO_NOT_PAD
        else:
            max_length = self.model_max_length
```

这就解释了 u3-l1 提到的「`model_max_length` 默认 `1e30`（`VERY_LARGE_INTEGER`）故默认不自动截断/填充」——因为 `1e30 > 1e20`，策略被降级为「不做」。

**`__call__` 主体**：[`tokenization_utils_base.py:L2428-L2551`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L2428-L2551)。它先组装 `all_kwargs`，调用上面的策略解析，再分发：

```python
encodings = self._encode_plus(
    text=text, text_pair=text_pair,
    padding_strategy=padding_strategy,
    truncation_strategy=truncation_strategy,
    max_length=max_length,
    **all_kwargs,
)
```

末尾对 `text_target` 的处理（[`L2545-L2551`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L2545-L2551)）——同时给输入和目标时，把目标的 `input_ids` 重命名为 `labels`，这正是 seq2seq 训练所需的格式：

```python
else:
    encodings["labels"] = target_encodings["input_ids"]
    return encodings
```

**填充的真正实现 `_pad`**：[`tokenization_utils_base.py:L2768-L2848`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L2768-L2848)（写在基类，所有后端共用）。核心逻辑是根据 `padding_side` 在左或右补 `pad_token_id`，并同步补 `attention_mask`（补 0）、`token_type_ids`（补 `pad_token_type_id`）：

```python
difference = max_length - len(required_input)
padding_side = padding_side if padding_side is not None else self.padding_side
if padding_side == "right":
    if return_attention_mask:
        encoded_inputs["attention_mask"] = encoded_inputs["attention_mask"] + [0] * difference
    encoded_inputs[self.model_input_names[0]] = required_input + [self.pad_token_id] * difference
elif padding_side == "left":
    if return_attention_mask:
        encoded_inputs["attention_mask"] = [0] * difference + encoded_inputs["attention_mask"]
    encoded_inputs[self.model_input_names[0]] = [self.pad_token_id] * difference + required_input
```

注意 `pad_to_multiple_of` 会让 `max_length` 向上取整到指定倍数（[`L2812-L2813`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L2812-L2813)），常见于让序列长度对齐到 8 的倍数以利用 GPU Tensor Core。

**批量填充入口 `pad()`**：[`tokenization_utils_base.py:L2578-L2766`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L2578-L2766)。它不仅能被 `__call__` 内部使用，还能单独拿来「事后补齐」一组已编码的字典，甚至直接当 PyTorch `DataLoader` 的 `collate_fn`——因为它会把 `list[dict]` 转成 `dict[list]`（[`L2656-L2662`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L2656-L2662)），并对批次内每条样本逐个 `_pad`。`LONGEST` 策略在此被解析成「先取批次最长，再按 `MAX_LENGTH` 补」（[`L2734-L2736`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L2734-L2736)）。

**截断的真正实现 `truncate_sequences`**：慢速后端写在 [`tokenization_python.py:L1231-L1296`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_python.py#L1231-L1296)（快速后端由 Rust 等价实现）。单句时按 `truncation_side`（默认 `right`）从尾部或头部砍掉 `num_tokens_to_remove` 个 token，并把被砍下的部分放进 `overflowing_tokens`（配合 `stride` 可形成滑动窗口）：

```python
if truncation_strategy == TruncationStrategy.ONLY_FIRST or (
    truncation_strategy == TruncationStrategy.LONGEST_FIRST and pair_ids is None
):
    window_len = min(len(ids), stride + num_tokens_to_remove)
    if self.truncation_side == "left":
        overflowing_tokens = ids[:window_len]
        ids = ids[num_tokens_to_remove:]
    else:
        overflowing_tokens = ids[-window_len:]
        ids = ids[:-num_tokens_to_remove]
```

三者的差异在于「句对」时砍哪一句：`only_first` 只砍第一句、`only_second` 只砍第二句、`longest_first` 则两边轮流砍（[`L1261-L1284`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_python.py#L1261-L1284)）。

**调用点**：`prepare_for_model`（[`tokenization_python.py:L1177-L1210`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_python.py#L1177-L1210)）在「加上特殊 token 之后的总长 > `max_length`」时才触发截断，注意 `num_tokens_to_remove = total_len - max_length`，**长度预算里已经预留了特殊 token 的位置**：

```python
num_special = self.num_special_tokens_to_add(pair=pair) if add_special_tokens else 0
total_len = len(ids) + len(pair_ids or []) + num_special
if truncation_strategy != TruncationStrategy.DO_NOT_TRUNCATE and max_length and total_len > max_length:
    ids, pair_ids, overflowing_tokens = self.truncate_sequences(
        ids, pair_ids=pair_ids, num_tokens_to_remove=total_len - max_length, ...)
```

#### 4.2.4 代码实践

**实践目标**：对一批长度不一的文本设置「动态填充 + 截断」，观察 `attention_mask` 与最终张量形状。这是日常训练前处理最典型的用法。

**操作步骤**：

```python
from transformers import AutoTokenizer

tok = AutoTokenizer.from_pretrained("bert-base-uncased")

texts = ["short", "a somewhat longer sentence than the first", "tiny"]

# 动态填充到批次最长，同时限制最大长度
enc = tok(
    texts,
    padding=True,        # 等价于 "longest"
    truncation=True,     # 等价于 "longest_first"
    max_length=16,
    return_tensors="pt",
)

print(enc["input_ids"].shape)        # torch.Size([3, L])，L = min(批次最长, 16)
print(enc["attention_mask"])         # 1 表示真 token，0 表示填充
print(tok.convert_ids_to_tokens(enc["input_ids"][1]))   # 看第 2 条样本的 token 序列
```

**需要观察的现象**：

1. `input_ids` 是一个 `[3, L]` 的整齐矩形张量，`L` 取「三条样本中最长的（含特殊 token）」与 16 的较小值。
2. `attention_mask` 每行的前若干位是 1、后面是 0，0 的数量正好是填充长度。
3. 把 `padding=True` 换成 `padding="max_length"`：此时 `L` 恒为 16，短样本会被补更多 0。
4. 把 `truncation` 去掉、并给一条超过 16 的长文本：会触发 `_get_padding_truncation_strategies` 里「`max_length` 默认开截断」的旧兼容逻辑（前提是模型本身 `model_max_length` 不超 `1e20`）。

**预期结果**：动态填充下，三条样本对齐到同一长度；`attention_mask` 能正确区分真 token 与填充位。具体长度数值「待本地验证」（依赖各文本实际切词数）。

#### 4.2.5 小练习与答案

**练习 1**：`padding=True` 与 `padding="max_length"` 有何区别？什么场景该用后者？
**参考答案**：`True`（`LONGEST`）补到「当前批次里最长那条」，批次不同则补的长度不同；`"max_length"`（`MAX_LENGTH`）恒定补到 `max_length`（或 `model_max_length`）。前者省算力、是训练常态；后者在推理服务里为保证张量形状固定（便于批处理/编译）时更常用。

**练习 2**：为什么对一个 `pad_token is None` 的分词器调用 `padding=True` 会报错？报错来自哪段代码？
**参考答案**：填充需要往序列里塞 `pad_token_id`，没有 pad token 就无从填充。校验在 [`_get_padding_truncation_strategies:L2405-L2410`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L2405-L2410)，提示你手动设 `tokenizer.pad_token = tokenizer.eos_token` 之类。许多 decoder-only 模型（如 GPT/Llama）默认无 pad token，需要借一个 eos 充当。

**练习 3**：`return_overflowing_tokens=True` 配合 `stride` 有什么用？
**参考答案**：当文本超长被截断时，把「溢出部分」也作为额外样本返回，并用 `stride` 让相邻溢出样本有若干 token 重叠。这用于把一篇长文档切成多个有上下文重叠的片段分别编码（典型于问答、长文档处理）。注意：句对 + `longest_first` 不支持返回溢出 token（见 [`tokenization_python.py:L1160-L1169`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_python.py#L1160-L1169)）。

---

### 4.3 Span 对齐：token ↔ word ↔ char

#### 4.3.1 概念说明

很多任务需要把「模型在 token 层面的输出」映射回「原始文本的字符或单词」。例如：

- **NER（命名实体识别）**：模型对每个 token 预测一个标签，但标注往往在「单词」或「字符」层面，需要把 token 标签聚合回单词。
- **问答抽取**：模型输出答案 token 的起止位置，要把它们翻译成原文本的字符区间，才能高亮原文。

要做到这件事，分词器必须在编码时同时记录三套坐标之间的映射：

```
原始文本（char 空间）
   │  字符切分
   ▼
单词序列（word 空间）
   │  子词切分
   ▼
token 序列（token 空间）
```

这三套空间两两之间都可互查：token↔word、token↔char、word↔char。`BatchEncoding` 提供了一组方法做这件事。**但这些方法只在快速分词器上可用**——因为对齐信息（offsets、word ids）来自 Rust 后端的 `Encoding` 对象，慢速分词器没有，调用会抛 `ValueError`。

返回的「区间」用两个命名元组表示：`CharSpan(start, end)`（[`tokenization_utils_base.py:L166-L176`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L166-L176)）表示原文本字符区间，`TokenSpan(start, end)`（[`L179-L189`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L179-L189)）表示 token 区间。注意 `end` 都是「尾后 exclusive」索引（即 `[start, end)`）。

#### 4.3.2 核心流程

最常用的是 `word_ids`，它返回一个与 token 序列等长的列表，告诉你每个 token 属于第几个单词（特殊 token 为 `None`）：

```
输入:  "unaffable"
单词:   "unaffable"          （word 0）
token: [CLS]  un   ##aff   ##able  [SEP]
word_ids: None   0     0       0     None
```

有了这个列表，就能把 token 级别的预测按 `word_id` 分组，聚合到单词级别。

其余对齐方法都是「单点查询」版本，支持两种调用形式（单样本时省略 batch index）：

| 方法 | 方向 | 返回 |
| --- | --- | --- |
| `word_ids(batch_index)` | token → word | `list[int\|None]` |
| `token_to_word(i, token_index)` | 单 token → word | `int` |
| `word_to_tokens(i, word_index)` | word → token 区间 | `TokenSpan\|None` |
| `token_to_chars(i, token_index)` | 单 token → 字符区间 | `CharSpan\|None` |
| `char_to_token(i, char_index)` | 字符 → token | `int` |
| `word_to_chars(i, word_index)` | word → 字符区间 | `CharSpan` |
| `char_to_word(i, char_index)` | 字符 → word | `int` |

此外 `tokens(batch_index)` 返回 token 字符串列表，`sequence_ids(batch_index)` 标记每个 token 属于第 0 句/第 1 句/特殊 token（None）——句对任务里用来区分两段输入。

#### 4.3.3 源码精读

所有对齐方法的结构高度一致：**先检查 `_encodings` 非空（否则报错），再把请求委托给对应样本的 `Encoding` 对象**。

**`word_ids`**：[`tokenization_utils_base.py:L363-L380`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L363-L380)

```python
def word_ids(self, batch_index: int = 0) -> list[int | None]:
    if not self._encodings:
        raise ValueError("word_ids() is not available when using non-fast tokenizers ...")
    return self._encodings[batch_index].word_ids
```

**`token_to_chars`**（返回 `CharSpan`）：[`tokenization_utils_base.py:L512-L549`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L512-L549)。注意它把后端返回的二元组包装成命名元组，特殊 token（无对应字符）返回 `None`：

```python
span_indices = self._encodings[batch_index].token_to_chars(token_index)
return CharSpan(*span_indices) if span_indices is not None else None
```

**`word_to_tokens`**（返回 `TokenSpan`）：[`tokenization_utils_base.py:L459-L510`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L459-L510)，同理把后端区间包成 `TokenSpan`，找不到时返回 `None`（典型于 `[CLS]` 这类特殊 token 占了位置）。

**`char_to_token`**：[`tokenization_utils_base.py:L551-L589`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L551-L589)，把原文本第 `char_index` 个字符定位到所属 token 的索引，是问答任务把「字符答案」转「token 答案」的关键。

> **设计要点**：这些方法的「单参数/双参数」两种调用形式（`f(i)` 或 `f(batch_index, i)`）通过 `batch_or_*_index` + 可选第二个 index 实现（见 `token_to_chars` 的 [`L542-L546`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L542-L546)），同时支持负索引，减少批处理时的样板代码。

#### 4.3.4 代码实践

**实践目标**：用 `word_ids` 把 token 级输出对齐回原始单词，这是 NER 等任务前处理的核心一步。

**操作步骤**：

```python
from transformers import AutoTokenizer

tok = AutoTokenizer.from_pretrained("bert-base-uncased")  # 默认是 fast 版

text = "unaffable"
enc = tok(text, return_tensors=None)

tokens = enc.tokens()              # ['[CLS]', 'un', '##aff', '##able', '[SEP]']
wids = enc.word_ids()              # [None, 0, 0, 0, None]

# 模拟：模型对每个 token 给了一个预测标签
token_labels = ["O", "B-PER", "I-PER", "I-PER", "O"]

# 按 word_id 把 token 标签聚合回单词（这里单词 0 由 3 个子词组成）
from collections import defaultdict
word_to_token_labels = defaultdict(list)
for tok_label, wid in zip(token_labels, wids):
    if wid is not None:                       # 跳过特殊 token
        word_to_token_labels[wid].append(tok_label)

for word_id, labels in sorted(word_to_token_labels.items()):
    print(f"word {word_id} <- tokens {labels}")
    # 你可以在这里决定聚合策略：取第一个非 O 标签、投票、取首 token 标签等

# 额外：字符级对齐
print(enc.token_to_chars(1))   # CharSpan(start=0, end=2)  -> 'un'
print(enc.token_to_chars(2))   # CharSpan(start=2, end=5)  -> 'aff'
```

**需要观察的现象**：

1. `word_ids()` 中 `[CLS]`/`[SEP]` 对应 `None`，三个子词 `un`/`##aff`/`##able` 都映射到同一个 `word_id = 0`。
2. `token_to_chars(1)` 返回的 `CharSpan(start=0, end=2)` 恰好是原文本 `"un"` 的下标区间，验证 token↔char 对齐正确。
3. 把 `AutoTokenizer.from_pretrained(..., use_fast=False)` 改成慢速分词器，再调 `word_ids()` 会抛 `ValueError`，直观验证「对齐是 fast 专属」。

**预期结果**：聚合后单词 0 收到三个 token 标签 `['B-PER', 'I-PER', 'I-PER']`，可按需取首个非 `O` 标签作为该单词的最终标签。字符区间下标与原文本切片一致。`end` 为尾后索引（即 `text[start:end]` 正好取出该 token 对应子串）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `word_ids` 在慢速分词器上不可用？技术上缺了什么？
**参考答案**：对齐信息是快速分词器（Rust `tokenizers` 库）在切词时额外计算并随 `Encoding` 对象返回的（含 offsets、word ids 等）。慢速分词器是纯 Python 实现，只产 id 序列、不记录这些偏移信息，故 `BatchEncoding._encodings` 为 `None`，`is_fast` 为假，所有对齐方法在开头的 `if not self._encodings` 处直接报错（见 [`L375-L379`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L375-L379)）。

**练习 2**：`sequence_ids()` 在句对输入（如 `(text_a, text_b)`）时返回什么？它和 `word_ids()` 有何不同？
**参考答案**：`sequence_ids()` 返回每个 token 属于第几个输入句子：特殊 token 为 `None`、属于第一句的为 `0`、属于第二句的为 `1`。它回答「属于哪一句」，而 `word_ids()` 回答「属于哪个单词」。问答任务里常先用 `sequence_ids()` 找出属于上下文（第二句）的 token 范围，再在该范围内定位答案。

---

### 4.4 解码：从 id 还原回字符串

为完整起见，简述与编码对应的「解码」方向。`decode` / `batch_decode` 定义于 [`tokenization_utils_base.py:L2863-L2944`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L2863-L2944)。

- `decode` 现在原生支持批量输入（[`L2893-L2903`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L2893-L2903)）：检测到输入是「列表的列表」就对每条分别调用 `_decode`，否则按单条解码。
- `batch_decode`（[`L2911-L2944`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L2911-L2944)）只是为向后兼容保留，内部直接转发给 `decode`。
- 真正的解码算法 `_decode`（[`L2950`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L2950) 起声明为抽象）下沉到各后端实现（慢速在 `tokenization_python.py`，快速在 `tokenization_utils_tokenizers.py`），承接 u3-l1 所述的「模板方法」分层。

两个常用开关：`skip_special_tokens=True` 去掉 `[CLS]`/`[SEP]` 等特殊 token；`clean_up_tokenization_spaces` 控制是否清理子词拼接产生的多余空格（批量解码时默认 `False`，见 [`L2894`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L2894)）。

> 实践：对 4.1.4 得到的 `enc["input_ids"]` 调 `tok.decode(enc["input_ids"], skip_special_tokens=True)`，应还原出 `"hello world"`。

## 5. 综合实践

**任务**：搭建一个「迷你 NER 前处理」流程，把本讲三块知识串起来。

要求：

1. 选一个快速分词器（如 `bert-base-uncased`）。
2. 准备两条长度不一、且含子词切分的文本（例如 `["HuggingFace is in NYC", "unaffable word"]`）。
3. 用 `__call__` 一次性完成：`padding=True`、`truncation=True`、`max_length=20`、`return_tensors="pt"`。
4. 打印 `input_ids.shape`、`attention_mask`，验证填充生效。
5. 对批次里的**每一条**样本，调用 `enc.word_ids(batch_index=i)` 与 `enc.tokens(batch_index=i)`，把「假想的逐 token 标签」按 `word_id` 聚合到单词级，打印每个单词及其聚合标签。
6. 任选一个非特殊 token，用 `enc.token_to_chars(i, token_index)` 取出它的字符区间，用 `text[token_span.start:token_span.end]` 切片验证确实对应该 token 的原文。

**检查点**：

- `attention_mask` 与 `input_ids` 中 pad 位置一一对应。
- 聚合时正确跳过 `word_id is None` 的特殊 token。
- 字符切片结果与该 token 字符串一致（注意 BERT 类的 `##` 前缀在原文中没有，字符区间指向的是去掉 `##` 后的原文子串）。

这个任务同时用到了编码选项（4.2）、`BatchEncoding` 容器（4.1）与 word/char 对齐（4.3），是训练真实 NER/问答模型前处理流程的缩影。具体输出「待本地验证」。

## 6. 本讲小结

- `tokenizer(...)` 返回的 `BatchEncoding` 是 `UserDict` 子类，既可字典式也可属性式访问；它额外挂载的 `_encodings`（仅快速分词器非空）是对齐能力的来源，`is_fast` 即据此判定。
- `__call__` 本身只做「参数合并 + 策略解析 + 分发」，真正的切词在后端、填充的实现在基类 `_pad`、截断的实现在后端 `truncate_sequences`。
- `padding`/`truncation`/`max_length` 背后是 `PaddingStrategy`/`TruncationStrategy` 枚举；`_get_padding_truncation_strategies` 负责把布尔与字符串归一为枚举，并在 `max_length` 缺省时回退到 `model_max_length`（过大则关闭策略）。
- `return_tensors` 由 `BatchEncoding.convert_to_tensors` 在构造时把列表就地转成 `pt`/`np`/`mlx` 张量；长度不齐时报错会提示你开 padding。
- 快速分词器提供 token↔word↔char 三套空间的对齐方法（`word_ids`、`token_to_chars`、`char_to_token` 等），区间用 `CharSpan`/`TokenSpan`（尾后索引）表示，是 NER/问答任务前处理的关键。
- 解码方向上，`decode` 现原生支持批量，`batch_decode` 仅为兼容保留，真正算法 `_decode` 下沉到各后端。

## 7. 下一步学习建议

- **走向具体模型**：本讲的所有机制都来自基类。下一讲 [u3-l3 慢速 vs 快速分词器与转换](u3-l3-slow-fast-convert.md) 会对比两种后端的差异，并讲 `convert_slow_tokenizer` 如何把慢速转快速——正好解释了本讲反复出现的「fast 才有 `_encodings`」的根源。
- **阅读一个真实分词器**：对照 [`models/bert/tokenization_bert_fast.py`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/models/bert/tokenization_bert_fast.py)，看它如何继承快速后端、只覆盖 `_batch_encode_plus`/`_encode_plus` 把 Rust `Encoding` 包成 `BatchEncoding`，把本讲的抽象落到具体。
- **进阶对齐**：在 [HuggingFace 课程](https://huggingface.co/learn/nlp-course) 的 token-classification 章节里，`word_ids` 的聚合模式（取首 token、BIO 标签对齐）有完整实战，可作为本讲 4.3 的延伸练习。
