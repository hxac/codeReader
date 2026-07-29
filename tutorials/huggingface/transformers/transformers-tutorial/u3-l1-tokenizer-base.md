# 分词器基础与 PreTrainedTokenizerBase

## 1. 本讲目标

本讲是「分词器（Tokenizer）」单元的第一篇，目标是让你建立对所有分词器的统一认知。读完本讲，你应该能够：

- 说清 `PreTrainedTokenizerBase` 在整个分词器体系中的角色：它是一份「抽象契约（contract）」，规定了任何一个分词器必须提供的能力。
- 理解分词器的三大核心属性族：**词表（vocabulary）**、**特殊 token（special tokens）**、**长度与对齐参数（model_max_length / padding_side / truncation_side）**。
- 掌握分词器对外暴露的四组接口：`tokenize`（切词）、`encode` / `__call__`（编码为 id）、`decode`（id 还原为字符串）、`convert_tokens_to_ids` / `convert_ids_to_tokens`（双向查表）。
- 区分「基类里已经写好的通用逻辑」与「子类必须实现的抽象方法」，并能读懂一个具体分词器是如何补齐这些抽象方法的。

本讲只讲**基类**，不深入任何具体分词算法（BPE、SentencePiece、Unigram 等留给后续讲义）。

## 2. 前置知识

在进入源码前，先用最朴素的语言把几个概念讲清楚。

### 2.1 什么是「分词」与「token / id」

模型并不直接读字符串，它只认数字。于是需要一个翻译过程：

1. 把一段文本切成一个个小块，每个小块叫一个 **token**（词、子词或字符）。
2. 查一张固定的对照表，把每个 token 换成一个整数 **id**。

这张对照表就叫**词表（vocabulary）**。例如 `"Hello"` 可能对应 `id = 15043`。所谓「分词器」，就是「文本 ↔ id」这段翻译过程的封装。

### 2.2 特殊 token（special tokens）

除了来自文本的正常 token，模型还需要一些「控制信号」，它们不属于任何具体词语，却必须出现在序列里，例如：

- `bos_token`（序列开头）、`eos_token`（序列结尾）
- `pad_token`（用来把长短不一的句子补齐成等长，方便组 batch）
- `cls_token`、`sep_token`、`mask_token`（BERT 等模型专用）

这些统称**特殊 token**。它们和普通 token 共享同一套 id 空间，但需要被单独管理（例如解码时可以被「跳过」）。

### 2.3 承接上一讲的统一范式

在 [u2-l2](u2-l2-from-pretrained-paradigm.md) 我们已经学到：config / model / tokenizer / processor 四类对象共享 `from_pretrained` / `save_pretrained` 统一接口，其「读取底盘」是自由函数 `cached_file`，「写入底盘」是混入基类 `PushToHubMixin`。

本讲要回答的下一个问题是：**分词器自身的「能力契约」长什么样？** 也就是说，无论你用的是 `LlamaTokenizer`、`BertTokenizer` 还是任何别的具体类，它们对外暴露的方法、属性、行为为什么如此一致？答案就在 `PreTrainedTokenizerBase`。

## 3. 本讲源码地图

本讲涉及的文件很少，但 `tokenization_utils_base.py` 是一个大文件（约 3700 行），我们只读其中最核心的部分。

| 文件 | 作用 |
|---|---|
| [`src/transformers/tokenization_utils_base.py`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py) | 本讲主角：所有分词器的抽象基类 `PreTrainedTokenizerBase`，以及 `BatchEncoding` 容器。 |
| [`src/transformers/tokenization_utils_tokenizers.py`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_tokenizers.py) | 「快速分词器后端」`TokenizersBackend`，它**继承基类并实现全部抽象方法**。本讲用它说明抽象契约如何被补齐。 |
| [`src/transformers/models/llama/tokenization_llama.py`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/models/llama/tokenization_llama.py) | 具体类 `LlamaTokenizer`，继承 `TokenizersBackend`。综合实践中阅读它。 |

> 提示：本仓库（v5）里「慢速分词器」基类位于 `tokenization_python.py`（`PreTrainedTokenizer`），其上又有 `tokenization_utils_sentencepiece.py` 的 `SentencePieceBackend`。慢速与快速的区别是 [u3-l3](u3-l3-slow-fast-convert.md) 的主题，本讲只需知道「基类之下有两套具体后端」即可。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **PreTrainedTokenizerBase：所有分词器的抽象契约**
2. **核心属性体系：词表、特殊 token 与长度参数**
3. **编码与解码接口：tokenize / encode / __call__ / decode**

---

### 4.1 PreTrainedTokenizerBase：所有分词器的抽象契约

#### 4.1.1 概念说明

`PreTrainedTokenizerBase` 是「抽象基类」——它本身**不能直接用来分词**（直接实例化它，几乎所有方法都会抛 `NotImplementedError`）。它的价值在于**统一契约**：

- 它规定了一个分词器「长什么样」：必须有哪些属性（`vocab_size`、`bos_token`……）、哪些方法（`encode`、`decode`、`__call__`……）。
- 它把**与具体算法无关的通用逻辑**（比如处理 `padding`/`truncation` 参数、管理特殊 token 列表、组装 `BatchEncoding`、`from_pretrained`/`save_pretrained`）**写在基类里**，所有子类免费继承。
- 它把**与算法强相关的部分**（比如「如何把文本切成 token」「如何保存词表文件」）**留空（抛 `NotImplementedError`）**，交给子类实现。

这是一种经典的「**模板方法 + 抽象方法**」设计：基类搭好骨架并控制流程，子类只填空几个「钩子」。

#### 4.1.2 核心流程：三层继承

在现代 v5 代码里，分词器的继承是三层结构：

```
PreTrainedTokenizerBase            # 抽象契约（本讲主角）
        ▲
        │（实现所有抽象方法）
   ┌────┴─────┐
   │          │
TokenizersBackend      SentencePieceBackend
(快速/Rust 后端)        (慢速/Python 后端)
   ▲
   │（只覆盖少量模型专属逻辑）
具体模型类，如 LlamaTokenizer / BertTokenizer / ...
```

- 第一层 `PreTrainedTokenizerBase` 只定义契约和通用逻辑。
- 第二层「后端」类把抽象方法全部实现：快速后端 `TokenizersBackend` 委托给 Rust 库 `tokenizers`；慢速后端 `SentencePieceBackend` 用纯 Python（SentencePiece）实现。
- 第三层「具体模型类」通常只覆盖与该模型相关的少量行为（如默认 `padding_side`、特殊 token 默认值），其余能力全部复用上一层。

这样，添加一个新模型时，几乎不用重写编码/解码逻辑，只需继承对应后端、填几个默认值即可——这正是「集中化、少重复」理念的体现（参见 [u1-l1](u1-l1-project-overview.md) 的设计哲学）。

#### 4.1.3 源码精读

**(1) 类声明与类级属性**

类继承自 `PushToHubMixin`（提供 `push_to_hub`，承接 u2-l2 的写入底盘），并声明了一组**类级常量**，这些常量本应是空壳，由具体模型子类去覆盖填充：

[src/transformers/tokenization_utils_base.py:972-L998](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L972-L998) 定义 `PreTrainedTokenizerBase`，并给出 `vocab_files_names`、`model_input_names`、`padding_side`、`truncation_side` 等默认值：

```python
class PreTrainedTokenizerBase(PushToHubMixin):
    """Base class for all tokenizer backends."""

    vocab_files_names: dict[str, str] = {}
    pretrained_vocab_files_map: dict[str, dict[str, str]] = {}
    _auto_class: str | None = None

    # first name has to correspond to main model input name
    # to make sure `tokenizer.pad(...)` works correctly
    model_input_names: list[str] = ["input_ids", "attention_mask"]
    padding_side: str = "right"
    truncation_side: str = "right"
    slow_tokenizer_class = None

    SPECIAL_TOKENS_ATTRIBUTES = [
        "bos_token", "eos_token", "unk_token", "sep_token",
        "pad_token", "cls_token", "mask_token",
    ]
```

几个关键点的中文说明：

- `vocab_files_names`：描述「需要哪些词表文件、文件名是什么」。基类留空 `{}`，由具体子类填充，例如 Llama 是 `{"vocab_file": "tokenizer.model", "tokenizer_file": "tokenizer.json"}`（见后文综合实践）。
- `model_input_names`：分词器默认产出的张量键名。第一项必须是模型的主输入（`input_ids`），这点注释特意强调「为了 `tokenizer.pad(...)` 正常工作」。
- `padding_side` / `truncation_side`：填充与截断发生在序列的哪一侧（`right`/`left`）。注意 decoder-only 模型常需要 `left` 填充（因为生成时新 token 接在右侧），Llama 就是 `left`。
- `SPECIAL_TOKENS_ATTRIBUTES`：七个「命名特殊 token」的属性名清单，是特殊 token 管理的基础（4.2 节细讲）。

**(2) `__init__`：把 kwargs 分类归位**

构造函数接受任意 `**kwargs`，它的主要工作是**把这些散乱的参数归位到正确的内部字段**：

[src/transformers/tokenization_utils_base.py:1000-L1106](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L1000-L1106) 中关键片段：

```python
def __init__(self, **kwargs):
    self.init_inputs = ()
    ...
    self.init_kwargs = copy.deepcopy(kwargs)
    self.name_or_path = kwargs.pop("name_or_path", "")
    ...
    # V5: Separate storage for named special tokens and extra special tokens
    self._special_tokens_map = dict.fromkeys(self.SPECIAL_TOKENS_ATTRIBUTES)
    self._extra_special_tokens = []
    ...
    # Directly set hidden values to allow init with tokens not yet in vocab
    for key in list(kwargs.keys()):
        if key in self.SPECIAL_TOKENS_ATTRIBUTES:
            value = kwargs.pop(key)
            ...
            self._special_tokens_map[key] = value
        elif key == "extra_special_tokens":
            ...
    ...
    model_max_length = kwargs.pop("model_max_length", kwargs.pop("max_len", None))
    self.model_max_length = model_max_length if model_max_length is not None else VERY_LARGE_INTEGER

    self.padding_side = kwargs.pop("padding_side", self.padding_side)
    ...
    self.truncation_side = kwargs.pop("truncation_side", self.truncation_side)
    ...
    self.model_input_names = kwargs.pop("model_input_names", self.model_input_names)
    ...
    self.chat_template = kwargs.pop("chat_template", None)
    ...
```

读这段 `__init__` 你会注意到一个统一的模式：**「用户传的 kwargs 覆盖类级默认值，用完就从 kwargs 里 pop 掉」**。这与 u2-l2 中 `from_pretrained`「用户 kwargs 覆盖文件值」的思想一脉相承。

还有一个细节值得注意：`model_max_length` 没给时，会被设成一个巨大的占位常量：

[src/transformers/tokenization_utils_base.py:130-L131](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L130-L131) 定义 `VERY_LARGE_INTEGER = int(1e30)`，即 \(10^{30}\)，当作「无限长」的哨兵值。它会在 4.2 节解释「为什么不设 `max_length` 时不会自动截断」。

**(3) 抽象方法清单：子类必须填的「空」**

下面这张表是本讲最重要的结论之一。基类中凡是 `raise NotImplementedError` 的成员，都是子类**必须实现**的抽象契约（行号均可在永久链接中核对）：

| 抽象成员 | 行号 | 输入 → 输出 | 作用 |
|---|---|---|---|
| `_add_tokens` | [L1260](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L1260-L1261) | `list[token], special=False` → `int`（新增数量） | 向词表新增 token |
| `added_tokens_decoder`（属性） | [L1428](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L1428-L1430) | → `dict[id, AddedToken]` | 查询「id → 新增 token」映射 |
| `__len__` | [L1444](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L1444-L1445) | → `int` | 词表总大小（含新增 token） |
| `vocab_size`（属性） | [L1447](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L1447-L1452) | → `int` | **基础**词表大小（不含新增） |
| `get_vocab` | [L1454](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L1454-L1464) | → `dict[str, int]` | 返回「token → id」完整字典 |
| `convert_ids_to_tokens` | [L1482](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L1482-L1496) | `id(s)` → `token(s)` | id 反查 token |
| `save_vocabulary` | [L2203](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L2203-L2219) | `目录, 前缀` → `tuple[路径]` | 保存词表文件 |
| `tokenize` | [L2221](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L2221-L2239) | `text` → `list[str]` | 文本切成 token 串（**算法核心**） |
| `num_special_tokens_to_add` | [L2303](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L2303-L2304) | `pair=False` → `int` | 单/双句会插入多少个特殊 token |
| `_encode_plus` | [L2553](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L2553-L2576) | `text, text_pair, ...` → `BatchEncoding` | 真正的编码主逻辑（含切词/加特殊 token/填充截断） |
| `convert_tokens_to_string` | [L2850](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L2850-L2861) | `list[str]` → `str` | token 串拼回字符串（含去空格等清理） |
| `_decode` | [L2950](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L2950-L2957) | `list[int]` → `str` | id 序列还原为字符串 |

> **隐式抽象方法**：基类中**已经有实现**的 `convert_tokens_to_ids`（[L1466-L1480](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L1466-L1480)）会调用 `self._convert_token_to_id_with_added_voc(token)`，但基类并没有定义这个方法——所以它也是一个必须由子类补齐的「隐式抽象方法」（快速后端在 [tokenization_utils_tokenizers.py:716](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_tokenizers.py#L716) 实现它）。

> **一个例外**：`get_special_tokens_mask`（[L1321 前后](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L1321-L1351)）在基类里有一个默认实现，但它对「非格式化序列」会抛 `NotImplementedError`，需要子类按需覆盖。

这十几个抽象方法在第二层后端类里被全部实现。例如 `TokenizersBackend` 实现了 `tokenize`、`_encode_plus`、`_decode`、`convert_tokens_to_string`、`get_vocab`、`vocab_size`、`__len__`、`save_vocabulary` 等（见 [tokenization_utils_tokenizers.py:598-L1017](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_tokenizers.py#L598-L1017)），从而让第三层的 `LlamaTokenizer` 等具体类「开箱即用」。

#### 4.1.4 代码实践：实例化并打印「自画像」

基类提供了 `__repr__`，它会把一个分词器的关键属性打印成一行——这是认识一个陌生分词器最快的入口。

1. **实践目标**：用 `__repr__` 一眼看清分词器的核心字段，并验证「直接实例化基类会失败」。
2. **操作步骤**：
   ```python
   # 示例代码（需要一个能联网下载 checkpoint 的环境）
   from transformers import AutoTokenizer, PreTrainedTokenizerBase

   tok = AutoTokenizer.from_pretrained("bert-base-uncased")
   print(repr(tok))          # 看核心字段
   print(type(tok).__mro__)  # 看继承链：应能看到 PreTrainedTokenizerBase
   ```
3. **需要观察的现象**：
   - `repr(tok)` 输出形如 `BertTokenizerFast(name_or_path=..., vocab_size=..., model_max_length=..., padding_side=..., truncation_side=..., special_tokens={...}, added_tokens_decoder={...})`。`__repr__` 的格式来自 [L1432-L1442](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L1432-L1442)。
   - `__mro__`（方法解析顺序）里能看到 `PreTrainedTokenizerBase` 处于链条中。
4. **预期结果**：具体类能正常打印；若尝试 `PreTrainedTokenizerBase()` 后调用 `tok.tokenize("hi")`，会抛 `NotImplementedError`，印证「基类不可直接使用」。
5. 本步骤是否真的下载成功**待本地验证**（取决于网络与缓存）；即便无法联网，阅读 `__repr__` 的实现也能理解输出含义。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `PreTrainedTokenizerBase` 把 `tokenize` 设计成抽象方法，却把 `encode` 写成具体方法？

**参考答案**：因为「文本如何切成 token」强依赖具体算法（BPE、WordPiece…），无法统一，所以留空；而 `encode` 的流程（确定 padding/truncation 策略 → 调 `_encode_plus` → 取 `input_ids`）与算法无关，可以在基类里固化，复用给所有子类。这正是模板方法模式：可变的留给子类，不变的写进基类。

**练习 2**：`vocab_size` 和 `__len__` 都表示「词表大小」，它们的区别是什么？

**参考答案**：`vocab_size`（[L1447](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L1447-L1452)）是**基础**词表大小，不含后续通过 `add_tokens` 新增的 token；`__len__`（[L1444](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L1444-L1445)）是**总**大小（基础 + 新增）。新增 token 会让两者不相等。

---

### 4.2 核心属性体系：词表、特殊 token 与长度参数

#### 4.2.1 概念说明

一个分词器对外暴露的「状态」可以归为三族属性。本模块逐一对照源码讲清它们的含义与取值规则。

1. **词表属性**：`vocab_size`、`__len__`、`get_vocab()`、`added_tokens_decoder`——回答「这个分词器认识多少 token、如何双向查表」。
2. **特殊 token 属性**：`bos_token`、`eos_token`…… 以及聚合视图 `special_tokens_map`、`all_special_tokens`、`all_special_ids`——回答「有哪些控制 token，它们的字符串值和 id 是什么」。
3. **长度与对齐参数**：`model_max_length`、`padding_side`、`truncation_side`、`model_input_names`——回答「最长允许多少 token、向哪一侧填充/截断、产出哪些张量键」。

#### 4.2.2 核心流程

特殊 token 的存储与查询流程可以画成：

```
__init__ 时：
  命名特殊 token  → 存入  _special_tokens_map  （dict，key 是属性名）
  额外特殊 token  → 存入  _extra_special_tokens （list）

查询时：
  tok.bos_token 等      →  __getattr__ 代理到 _special_tokens_map
  special_tokens_map    →  只含「命名」token 的 {attr: str} 字典
  all_special_tokens    →  命名 + 额外，去重后的字符串列表
  all_special_ids       →  对 all_special_tokens 做 convert_tokens_to_ids
```

「命名」与「额外」两套存储是 v5 的新设计（代码注释里标注了 `V5:`），目的是把七种固定命名 token（`bos/eos/...`）和模型专属的额外 token（如多模态的图像占位 token）干净地分开。

#### 4.2.3 源码精读

**(1) 词表查表的双向接口**

[src/transformers/tokenization_utils_base.py:1466-L1480](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L1466-L1480)：`convert_tokens_to_ids` 是**基类的具体方法**，它只是对单个/列表做个分发，真正的查表委托给（隐式抽象的）`_convert_token_to_id_with_added_voc`：

```python
def convert_tokens_to_ids(self, tokens: str | list[str]) -> int | list[int]:
    if isinstance(tokens, str):
        return self._convert_token_to_id_with_added_voc(tokens)
    return [self._convert_token_to_id_with_added_voc(token) for token in tokens]
```

`get_vocab()`（[L1454-L1464](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L1454-L1464)）则返回完整的 `token → id` 字典，文档注释里特别点出：`tokenizer.get_vocab()[token]` 等价于 `convert_tokens_to_ids(token)`（当 token 在词表中时）。

**(2) 特殊 token 的聚合视图**

[src/transformers/tokenization_utils_base.py:1353-L1370](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L1353-L1370)：`special_tokens_map` 只汇总「命名」特殊 token，且把 `AddedToken` 类型统一 `str()` 成字符串：

```python
@property
def special_tokens_map(self) -> dict[str, str]:
    return {
        attr: str(self._special_tokens_map[attr])
        for attr in self.SPECIAL_TOKENS_ATTRIBUTES
        if self._special_tokens_map.get(attr) is not None
    }
```

[src/transformers/tokenization_utils_base.py:1375-L1402](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L1375-L1402)：`all_special_tokens` 则把「命名 + 额外」两套合并、用 `seen` 集合去重，再转成字符串列表。而 [L1404-L1409](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L1404-L1409) 的 `all_special_ids` 直接复用上一步：

```python
@property
def all_special_ids(self) -> list[int]:
    return self.convert_tokens_to_ids(self.all_special_tokens)
```

这是一个很好的「基类方法调用基类方法」的例子——`all_special_ids` 不关心 token 是怎么变成 id 的，只管调用 `convert_tokens_to_ids`，后者再下沉到子类。

**(3) `model_max_length` 与填充/截断侧**

这三者在 `__init__` 里被读取（见 4.1.3 的 (2) 段）。要特别理解 `model_max_length` 的两重身份：

- 它是「这个模型能吃下的最大 token 数」的上限提示。
- 当你**没有显式指定** `max_length`，但开启了 `truncation=True` 或 `padding='max_length'` 时，分词器会用它作为兜底长度。
- 默认值 `VERY_LARGE_INTEGER = int(1e30)`（[L130](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L130-L131)）意味着「不限制」——所以「不设 max_length 就不截断」。

`padding_side` / `truncation_side` 在 `__init__` 里被校验只能是 `"right"` 或 `"left"`，否则报错（[L1063-L1073](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L1063-L1073)）。

#### 4.2.4 代码实践：观察三族属性

1. **实践目标**：动手打印一个分词器的三族属性，把抽象概念对应到具体数值。
2. **操作步骤**：
   ```python
   # 示例代码
   tok = AutoTokenizer.from_pretrained("bert-base-uncased")
   print("vocab_size   =", tok.vocab_size)
   print("len(tokenizer)=", len(tok))
   print("special_tokens_map =", tok.special_tokens_map)
   print("all_special_tokens =", tok.all_special_tokens)
   print("all_special_ids    =", tok.all_special_ids)
   print("model_max_length   =", tok.model_max_length)
   print("padding_side / truncation_side =", tok.padding_side, tok.truncation_side)
   ```
3. **需要观察的现象**：
   - `vocab_size` 与 `len(tok)` 通常相等（除非新增过 token）。
   - `all_special_tokens` 与 `all_special_ids` 一一对应。
   - `model_max_length` 若 checkpoint 元数据里有 `max_position_embeddings` 等信息，会是具体数字（如 512）；否则是 `1e30`。
4. **预期结果**：BERT 的 `special_tokens_map` 应包含 `unk_token`、`sep_token`、`pad_token`、`cls_token`、`mask_token`。
5. 网络可达性**待本地验证**；可改用本地已有的任意 checkpoint。

#### 4.2.5 小练习与答案

**练习 1**：`special_tokens_map` 和 `all_special_tokens` 的区别是什么？

**参考答案**：`special_tokens_map`（[L1353](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L1353-L1370)）是 `{属性名: 字符串}` 的字典，且**只含命名 token**；`all_special_tokens`（[L1375](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L1375-L1402)）是字符串列表，**含命名 + 额外**两类，且去重。

**练习 2**：如果不传 `model_max_length`，默认值是多少？它为什么不会导致默认截断？

**参考答案**：默认 `VERY_LARGE_INTEGER = int(1e30)`（[L130](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L130-L131)）。因为只有当用户显式开启截断（`truncation=True`）或请求 `padding='max_length'` 时，才会用到这个上限；否则它形同「无限大」，不触发任何截断。

---

### 4.3 编码与解码接口：tokenize / encode / __call__ / decode

#### 4.3.1 概念说明

这是分词器最常用的四组接口。先给一张全景：

| 接口 | 输入 → 输出 | 是否在基类实现 | 说明 |
|---|---|---|---|
| `tokenize` | `str` → `list[str]` | 抽象 | 只切词、不查表、不加特殊 token |
| `convert_tokens_to_ids` | `list[str]` → `list[int]` | 具体 | 查表把 token 变 id |
| `encode` | `str` → `list[int]` | 具体 | 切词+查表+加特殊 token（可选填充/截断），只返回 `input_ids` |
| `__call__` | `str/list` → `BatchEncoding` | 具体 | 最常用：返回 `input_ids`、`attention_mask` 等完整字典 |
| `decode` / `batch_decode` | `list[int]` → `str` | 具体 | id 还原字符串 |

一句话记忆：`tokenize` 是最底层、`encode` 是「只要 id」、`__call__` 是「要喂给模型的完整张量字典」、`decode` 是反向。

#### 4.3.2 核心流程

`__call__` 的内部控制流可以概括为（省略分支）：

```
__call__(text, ...)
  ├─ 校验：text 与 text_target 不能同时为空
  ├─ _get_padding_truncation_strategies(...)   # 把 padding/truncation 这些「灵活参数」归一成枚举策略
  ├─ 若有 text：        _encode_plus(text, ...)        → BatchEncoding
  ├─ 若有 text_target： _encode_plus(text_target, ...) → BatchEncoding（可放进 encodings["labels"]）
  └─ 返回 BatchEncoding
```

而 `_encode_plus`（抽象）才是真正的「编码引擎」，它在子类里完成：

```
_encode_plus(text, ...)
  ├─ tokenize(text)                       # 切词（抽象）
  ├─ convert_tokens_to_ids(tokens)        # 查表（基类具体 → 隐式抽象）
  ├─ 插入特殊 token（bos/eos/...）
  ├─ 按 padding/truncation 策略填充或截断
  ├─ 组装 attention_mask / token_type_ids 等（依 model_input_names）
  └─ 返回 BatchEncoding
```

`decode` 的流程则是对称的反向：

```
decode(token_ids)
  ├─ to_py_obj(token_ids)        # 把 tensor/np 统一成 python list
  ├─ 若是 batch：逐条 _decode
  └─ _decode(list[int])          # 抽象：id→token→string（convert_tokens_to_string）
```

#### 4.3.3 源码精读

**(1) `__call__`：参数归并与策略推断**

[src/transformers/tokenization_utils_base.py:2428-L2551](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L2428-L2551)。它先把所有显式参数和 `tokenizer_kwargs`、`**kwargs` 合并（显式参数优先），再用 `_get_padding_truncation_strategies` 把 `padding`、`truncation`、`max_length` 这些「可以传 bool 也可以传字符串」的灵活参数，归一成确定的 `PaddingStrategy` / `TruncationStrategy` 枚举，最后交给 `_encode_plus`：

```python
def __call__(self, text=None, text_pair=None, text_target=None, ...,
             padding=False, truncation=None, max_length=None, ...,
             return_tensors=None, **kwargs) -> BatchEncoding:
    ...
    all_kwargs = {"add_special_tokens": add_special_tokens, "padding": padding,
                  "truncation": truncation, "max_length": max_length, ...}
    ...
    padding_strategy, truncation_strategy, max_length, kwargs = self._get_padding_truncation_strategies(...)
    if text is not None:
        ...
        encodings = self._encode_plus(text=text, text_pair=text_pair,
                                      padding_strategy=padding_strategy,
                                      truncation_strategy=truncation_strategy,
                                      max_length=max_length, **all_kwargs)
    if text_target is not None:
        ...
        target_encodings = self._encode_plus(text=text_target, ...)
    ...
    if text_target is None:
        return encodings
    elif text is None:
        return target_encodings
    else:
        encodings["labels"] = target_encodings["input_ids"]
        return encodings
```

注意末尾 `encodings["labels"] = target_encodings["input_ids"]` 这一行——它解释了一个常见用法：当你同时给 `text` 和 `text_target` 时（如训练 seq2seq），分词器会把目标文本编码后塞进 `labels` 字段，正好契合训练循环对标签的期望。

**(2) `encode`：`__call__` 的「只要 id」简化版**

[src/transformers/tokenization_utils_base.py:2251-L2301](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L2251-L2301)：

```python
def encode(self, text, text_pair=None, add_special_tokens=True, ...,
           max_length=None, stride=0, ..., **kwargs) -> list[int]:
    padding_strategy, truncation_strategy, max_length, kwargs_updated = self._get_padding_truncation_strategies(...)
    ...
    encoded_inputs = self._encode_plus(text, text_pair=text_pair, ...)
    return encoded_inputs["input_ids"]
```

它和 `__call__` 共用 `_get_padding_truncation_strategies` + `_encode_plus`，只是**只取 `input_ids`** 返回。文档注释里的等价描述很形象：`encode` ≈ `convert_tokens_to_ids(tokenize(text))`（再叠加特殊 token 与填充截断）。

**(3) `decode` / `_decode`**

[src/transformers/tokenization_utils_base.py:2863-L2909](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L2863-L2909)：`decode` 是具体方法，负责把输入统一成 list、处理 batch，最后下沉到抽象的 `_decode`：

```python
def decode(self, token_ids, skip_special_tokens=False, **kwargs) -> str | list[str]:
    token_ids = to_py_obj(token_ids)                       # tensor/np → list
    if isinstance(token_ids, (list, tuple)) and ... isinstance(token_ids[0], (list, tuple)):
        return [self._decode(token_ids=seq, skip_special_tokens=skip_special_tokens, ...) for seq in token_ids]
    return self._decode(token_ids=token_ids, skip_special_tokens=skip_special_tokens, **kwargs)
```

`skip_special_tokens=True` 时，特殊 token 不会出现在还原后的字符串里——这依赖 `all_special_tokens`（4.2 节）来判断哪些该跳过。

**(4) `BatchEncoding`：编码结果容器**

`__call__` 返回的不是一个普通 dict，而是 `BatchEncoding`（[L195](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L195-L306) 定义）。它同时具备 dict 行为（`enc["input_ids"]`）和属性行为（`enc.input_ids`），并能通过 `convert_to_tensors` / `to` 把 list 转成 PyTorch/NumPy 张量并搬到指定设备。其 `is_fast` 属性（[L306-L311](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L306-L311)）标识是否来自快速分词器（决定能否用 `word_ids`、`token_to_chars` 等对齐方法，详见 [u3-l2](u3-l2-batch-encoding-decode.md)）。

#### 4.3.4 代码实践：对比四组接口的输入输出

1. **实践目标**：用同一段文本依次走 `tokenize` → `convert_tokens_to_ids` → `encode` → `__call__` → `decode`，亲眼看清它们各自返回什么。
2. **操作步骤**：
   ```python
   # 示例代码
   tok = AutoTokenizer.from_pretrained("bert-base-uncased")
   s = "Hello, transformers!"

   toks = tok.tokenize(s)                  # 1) 切词
   ids_from_tokens = tok.convert_tokens_to_ids(toks)  # 2) 查表
   ids_encode = tok.encode(s)              # 3) 切词+查表+特殊 token
   batch = tok(s, return_tensors="pt")     # 4) 喂给模型的完整输入

   print("tokenize        ->", toks)
   print("tokens_to_ids   ->", ids_from_tokens)
   print("encode(+special)->", ids_encode)
   print("__call__ keys   ->", list(batch.keys()))
   print("round-trip      ->", tok.decode(ids_encode))
   print("decode skip_sp  ->", tok.decode(ids_encode, skip_special_tokens=True))
   ```
3. **需要观察的现象**：
   - `ids_from_tokens` 通常**不含**特殊 token；而 `ids_encode` 的首尾会多出 `cls_token`、`sep_token` 的 id（BERT）。
   - `batch` 是 `BatchEncoding`，既能 `batch["input_ids"]` 也能 `batch.input_ids`；`list(batch.keys())` 一般含 `input_ids`、`token_type_ids`、`attention_mask`。
   - `decode` 默认保留特殊 token；`skip_special_tokens=True` 后 `[CLS]`、`[SEP]` 消失。
4. **预期结果**：round-trip 还原出的文本与原文基本一致（BPE/WordPiece 可能大小写或空格有细微差异）。
5. 网络可达性**待本地验证**；可换任意本地 checkpoint。

#### 4.3.5 小练习与答案

**练习 1**：`encode("hi")` 和 `[convert_tokens_to_ids(t) for t in tokenize("hi")]` 的结果一定相同吗？

**参考答案**：不一定。`encode` 默认 `add_special_tokens=True`，会额外插入 `bos`/`cls`/`eos`/`sep` 等特殊 token 的 id；而 `tokenize + convert_tokens_to_ids` 不加特殊 token。只有当 `encode("hi", add_special_tokens=False)` 时二者才相等（参见 [L2267](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L2267-L2267) 的等价说明）。

**练习 2**：为什么说 `__call__` 同时传 `text` 和 `text_target` 很适合训练？

**参考答案**：因为基类会把 `text_target` 编码后的 `input_ids` 放进返回结果的 `labels` 字段（[L2550](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L2550-L2551)），正好是训练循环计算损失时需要的标签位置，省去了手工拼接。

---

## 5. 综合实践：读懂一个具体分词器如何补齐抽象契约

本实践直接对应本讲的学习目标——**通过阅读一个具体分词器，反向理解基类定义的抽象契约**。

### 实践目标

确认「`PreTrainedTokenizerBase` 定义的抽象方法，在具体类中是由谁、在哪里实现的」，并能为每个抽象方法说清它的输入输出。

### 操作步骤

1. 打开具体类 [src/transformers/models/llama/tokenization_llama.py](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/models/llama/tokenization_llama.py)。先看类声明与类属性（[L39-L90](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/models/llama/tokenization_llama.py#L39-L90)）：
   ```python
   class LlamaTokenizer(TokenizersBackend):
       ...
       vocab_files_names = VOCAB_FILES_NAMES   # {"vocab_file": "tokenizer.model", "tokenizer_file": "tokenizer.json"}
       padding_side = "left"
       model_input_names = ["input_ids", "attention_mask"]
       model = BPE
   ```
   你会发现 `LlamaTokenizer` **几乎没有定义任何「编码/解码」方法**——它只覆盖了三个类级默认值（`padding_side` 设为 `"left"`、`vocab_files_names`、`model`），其余能力全部继承自 `TokenizersBackend`。

2. 打开 [src/transformers/tokenization_utils_tokenizers.py](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_tokenizers.py)，确认 [L84](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_tokenizers.py#L84) 的 `class TokenizersBackend(PreTrainedTokenizerBase)`。

3. 在该文件中搜索 4.1.3 表格里的抽象方法名，逐一确认它们的实现位置。例如：
   - `tokenize` → [L778](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_tokenizers.py#L778)
   - `_encode_plus` → [L856](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_tokenizers.py#L856)
   - `_decode` → [L1017](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_tokenizers.py#L1017)
   - `convert_tokens_to_string` → [L1010](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_tokenizers.py#L1010)
   - `get_vocab` → [L604](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_tokenizers.py#L604)
   - `vocab_size` → [L598](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_tokenizers.py#L598)
   - `__len__` → [L649](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_tokenizers.py#L649)
   - `_convert_token_to_id_with_added_voc`（隐式抽象） → [L716](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_tokenizers.py#L716)
   - `save_vocabulary` → [L508](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_tokenizers.py#L508)

4. **填写下面的「契约映射表」**（这是本实践的产出）：

   | 抽象方法（基类） | 输入 | 输出 | 实现位置 |
   |---|---|---|---|
   | `tokenize` | `text: str` | `list[str]`（token 串） | `TokenizersBackend.tokenize` |
   | `_encode_plus` | `text, text_pair, 策略...` | `BatchEncoding` | `TokenizersBackend._encode_plus` |
   | `_decode` | `list[int]` | `str` | `TokenizersBackend._decode` |
   | `convert_tokens_to_string` | `list[str]` | `str` | `TokenizersBackend.convert_tokens_to_string` |
   | `convert_ids_to_tokens` | `id(s)` | `token(s)` | `TokenizersBackend`（继承实现） |
   | `get_vocab` | — | `dict[str,int]` | `TokenizersBackend.get_vocab` |
   | `vocab_size` | — | `int` | `TokenizersBackend.vocab_size` |
   | `__len__` | — | `int` | `TokenizersBackend.__len__` |
   | `save_vocabulary` | `目录, 前缀` | `tuple[str]` | `TokenizersBackend.save_vocabulary` |
   | `_add_tokens` | `list[token]` | `int`（新增数） | `TokenizersBackend._add_tokens`（[L725](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_tokenizers.py#L725)） |
   | `added_tokens_decoder` | — | `dict[int, AddedToken]` | `TokenizersBackend`（[L620](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_tokenizers.py#L620)） |

### 需要观察的现象

- `LlamaTokenizer` 自身几乎没有方法体，证明「具体类只填默认值，能力全部来自后端基类」。
- `TokenizersBackend` 的实现大多是对 Rust `tokenizers` 库 `self._tokenizer` 的薄封装（例如 `_decode` 内部最终调用底层的 Rust 解码）。

### 预期结果

你能得出结论：**「抽象契约在 `PreTrainedTokenizerBase`，实现在 `TokenizersBackend`，具体类只做轻量定制」**。这种分层正是 transformers 能用一套接口服务数百个模型的关键。

> 本实践为纯源码阅读型，无需运行环境即可完成；表中的「实现位置」均可在永久链接中核对。

## 6. 本讲小结

- `PreTrainedTokenizerBase` 是所有分词器的**抽象契约**：本身不能直接用，它把与算法无关的通用逻辑（参数归并、特殊 token 管理、`from_pretrained`/`save_pretrained`）写进基类，把算法相关的 `tokenize`/`_decode` 等留空（`NotImplementedError`）交给子类。
- 现代分词器是**三层继承**：基类 → 后端（`TokenizersBackend` 快速 / `SentencePieceBackend` 慢速）→ 具体模型类；具体类通常只覆盖少量默认值（如 `padding_side`）。
- **三族核心属性**：词表（`vocab_size`/`__len__`/`get_vocab`）、特殊 token（`special_tokens_map`/`all_special_tokens`/`all_special_ids`，分「命名」与「额外」两套存储）、长度与对齐（`model_max_length` 默认 `1e30`、`padding_side`/`truncation_side`）。
- **四组接口**：`tokenize`（切词，抽象）→ `convert_tokens_to_ids`（查表，具体）→ `encode`（只要 id）/ `__call__`（返回 `BatchEncoding` 完整字典）→ `decode`（反向还原，具体方法下沉到抽象 `_decode`）。
- `__call__` 的核心是「合并参数 → `_get_padding_truncation_strategies` 归一策略 → `_encode_plus` 执行编码」；同时传 `text` 与 `text_target` 时会把目标编码进 `labels`，方便训练。
- 编码结果统一封装为 `BatchEncoding`，兼具 dict 与属性两种访问方式，并能转换为 PyTorch/NumPy 张量。

## 7. 下一步学习建议

本讲只讲了「基类契约」。建议接下来：

1. 阅读 [u3-l2：BatchEncoding 与编码/解码/对齐](u3-l2-batch-encoding-decode.md)，深入 `BatchEncoding` 容器、`padding`/`truncation` 的细节策略，以及快速分词器独有的 `word_ids` / `token_to_chars` 对齐能力。
2. 阅读 [u3-l3：慢速 vs 快速分词器与转换](u3-l3-slow-fast-convert.md)，对比 `TokenizersBackend`（Rust）与 `SentencePieceBackend`（Python）两套后端的差异与互转。
3. 阅读 [u3-l4：聊天模板 Chat Template](u3-l4-chat-template.md)，了解 `__init__` 里出现的 `chat_template` 字段如何把对话渲染成模型输入。
4. 想验证理解，可挑选任意一个具体模型目录（如 `models/bert/`、`models/gpt2/`），重复本讲综合实践的「契约映射表」练习，体会不同模型如何复用同一套基类。
