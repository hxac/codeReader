# 慢速 vs 快速分词器与转换

## 1. 本讲目标

本讲承接 u3-l1（`PreTrainedTokenizerBase` 抽象契约）与 u3-l2（`BatchEncoding` 与对齐），回答一个在前两讲被刻意回避的问题：**「分词」这件事到底是谁在干？**

学完后你应当能够：

- 区分 transformers 的「慢速后端」与「快速后端」两条实现路线，并理解它们各自依赖的底层库；
- 读懂 `SentencePieceBackend`（慢）与 `TokenizersBackend`（快）两个核心后端类的源码，知道它们如何把抽象方法落地；
- 理解 **Converter 体系**：当仓库里只有「慢速资产」（如 SentencePiece 的 `tokenizer.model`）而没有预制的 `tokenizer.json` 时，库如何把它「翻译」成快速后端能直接使用的 Rust 对象；
- 知道在 v5 里如何通过 `backend` 参数选择后端，并能用 `convert_slow_tokenizer` 完成一次手动转换。

> ⚠️ **重要：v5 架构变化**。如果你看过旧版教程，会发现过去有 `LlamaTokenizer`（慢）和 `LlamaTokenizerFast`（快）两个类。在当前 v5（本讲所基于的 HEAD）中，**「具体模型分词器类」统一继承自快速后端 `TokenizersBackend`，不再有 `*Fast` 后缀的伴生类**。慢速路径退化为一个通用的 `SentencePieceBackend`。这一变化贯穿全讲，务必牢记。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**什么是「后端（backend）」？** 在 u3-l1 里我们说过，`PreTrainedTokenizerBase` 只定义「分词器要能做什么」（抽象契约），不规定「怎么做」。真正把文本切成 token 的代码就是「后端」。transformers 支持两套后端，它们对上层暴露完全一样的接口（`__call__` / `encode` / `decode` / `convert_tokens_to_ids` ……），但底层实现完全不同。

**两套后端的差别**：

| 维度 | 慢速后端 | 快速后端 |
|---|---|---|
| 实现语言 | Python 调用 C++（`sentencepiece` 库） | 直接调用 Rust（`tokenizers` 库） |
| 代表类 | `SentencePieceBackend` | `TokenizersBackend` |
| `is_fast` 属性 | `False` | `True` |
| 典型词表文件 | `tokenizer.model`（SentencePiece protobuf） | `tokenizer.json`（Rust 序列化） |
| 速度 | 逐条处理，较慢 | 批量并行，快 5～50 倍 |
| 对齐信息（`word_ids`/offsets，见 u3-l2） | 无（`_encodings` 为空） | 有 |

为什么需要「快」？因为训练和大规模推理要对成百万条文本反复分词，几十倍的速度差是实打实的成本。这也是 v5 把「具体模型类」默认绑到快速后端的根本原因。

**为什么还需要「慢」与「转换」？** 两个现实原因：

1. **可移植性与校验**：快速后端依赖 Rust 库；当环境装不上它、或想用一个「参考实现」来核对快速分词结果是否正确时，慢速后端是 ground truth。
2. **历史资产**：许多开源模型只发布了 SentencePiece 训练出的 `tokenizer.model`，并没有现成的 `tokenizer.json`。为了让快速后端也能加载它们，库需要一个「翻译器」把慢速资产转成快速对象——这就是本讲后半段的 **Converter 体系**。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `src/transformers/tokenization_utils_base.py` | 所有后端的抽象基类 `PreTrainedTokenizerBase`（u3-l1 已讲），本讲关注其中记录后端类型的 `self.backend` 字段 |
| `src/transformers/tokenization_python.py` | 慢速后端的抽象基类 `PythonBackend`（别名 `PreTrainedTokenizer`），声明 `is_fast=False` 与若干抽象方法 |
| `src/transformers/tokenization_utils_sentencepiece.py` | **慢速后端** `SentencePieceBackend`：用 `sentencepiece` 库落地分词 |
| `src/transformers/tokenization_utils_tokenizers.py` | **快速后端** `TokenizersBackend`：封装 Rust `tokenizers` 库，并负责「加载时按需转换」 |
| `src/transformers/convert_slow_tokenizer.py` | **Converter 体系**：把慢速分词器/`.model` 文件转换成 Rust `TokenizersBackend` 能用的对象 |
| `src/transformers/models/auto/tokenization_auto.py` | `AutoTokenizer`，通过 `backend` 参数选择后端 |

---

## 4. 核心概念与源码讲解

### 4.1 慢速 vs 快速：两条实现路线与后端选择

#### 4.1.1 概念说明

整个分词体系的继承关系可以这样理解（自上而下）：

```
PreTrainedTokenizerBase            （抽象契约：encode/__call__/decode/属性，u3-l1）
        │
        ├── PythonBackend          （慢速抽象基类：is_fast=False，留空 _tokenize 等）
        │       └── SentencePieceBackend   （慢速具体后端：用 sentencepiece 库实现）
        │
        └── TokenizersBackend      （快速后端：is_fast=True，委托给 Rust self._tokenizer）
                └── LlamaTokenizer / T5Tokenizer / ...   （各模型类，v5 直接继承快速后端）
```

关键点：**两个后端是「兄弟」而非「父子」**——它们都直接继承 `PreTrainedTokenizerBase`，互不相干。上层代码只看接口，不关心底下是 Python+C++ 还是 Rust。

#### 4.1.2 核心流程

加载一个分词器时，「选哪个后端」由 `AutoTokenizer` 决定：

1. 读 checkpoint 的 `config.json` 得到 `model_type`，查 `TOKENIZER_MAPPING_NAMES` 得到具体类名（如 `LlamaTokenizer`）；
2. v5 里这些具体类本身就是 `TokenizersBackend` 的子类，所以**默认就是快速后端**；
3. 用户可通过 `backend="sentencepiece"` 显式要求走慢速 `SentencePieceBackend`；
4. 旧的 `use_fast` 参数在 v5 已被忽略（向后兼容）。

#### 4.1.3 源码精读

抽象基类 `PreTrainedTokenizerBase` 是「所有后端之祖」，并在 v5 新增了一个记录「用了哪个后端」的字段：

- [src/transformers/tokenization_utils_base.py:L972-L975](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L972-L975) —— 文档字符串写明它是「Base class for all tokenizer backends」，两个后端都继承自它。
- [src/transformers/tokenization_utils_base.py:L1103-L1105](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_base.py#L1103-L1105) —— `self.backend = kwargs.pop("backend", None)` 与 `self.files_loaded`。这是 v5 新增的「后端溯源」信息，后面 `Processor` 会用它判断当前 tokenizer 是否为快速（见 4.3.3）。

慢速抽象基类 `PythonBackend`（注意它的别名）：

- [src/transformers/tokenization_python.py:L400-L410](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_python.py#L400-L410) —— 文档明说它是「Base class for all slow tokenizers」。
- [src/transformers/tokenization_python.py:L453-L455](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_python.py#L453-L455) —— `is_fast` 恒为 `False`，这是「慢速」的身份标识。
- [src/transformers/tokenization_python.py:L680-L687](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_python.py#L680-L687) 与 [L699-L700](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_python.py#L699-L700) —— `_tokenize` 与 `_convert_token_to_id` 在此层仍是抽象方法（`raise NotImplementedError`），交给具体后端实现。这正是 u3-l1 讲过的「模板方法」。
- [src/transformers/tokenization_python.py:L1424](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_python.py#L1424) —— `PreTrainedTokenizer = PythonBackend`。这行别名解释了为什么很多文件里仍写着 `from .tokenization_python import PreTrainedTokenizer`：那是历史名称，现在指的就是 `PythonBackend`。

`AutoTokenizer` 选择后端的入口与参数文档：

- [src/transformers/models/auto/tokenization_auto.py:L635-L637](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/models/auto/tokenization_auto.py#L635-L637) —— `from_pretrained` 的返回类型标注为 `TokenizersBackend | SentencePieceBackend`，两种后端都可能返回。
- [src/transformers/models/auto/tokenization_auto.py:L679-L682](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/models/auto/tokenization_auto.py#L679-L682) —— `backend` 参数说明：默认 `"tokenizers"`（快），可选 `"sentencepiece"`（慢）。
- [src/transformers/models/auto/tokenization_auto.py:L718-L719](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/models/auto/tokenization_auto.py#L718-L719) —— `use_fast = kwargs.pop("use_fast", None)`，注释写明「V5: Always use fast tokenizers, ignore use_fast parameter」：旧参数被吞掉，改用 `backend`。

#### 4.1.4 代码实践

**实践目标**：用 `backend` 参数分别加载同一个 checkpoint 的快速与慢速分词器，对比它们的身份标识。

**操作步骤**（需联网下载 checkpoint）：

```python
# 示例代码
from transformers import AutoTokenizer

repo = "hf-internal-testing/llama-tokenizer"

tok_fast = AutoTokenizer.from_pretrained(repo, backend="tokenizers")     # 默认：快速
tok_slow = AutoTokenizer.from_pretrained(repo, backend="sentencepiece")  # 慢速

for name, tok in [("fast", tok_fast), ("slow", tok_slow)]:
    print(name, "→", type(tok).__name__, "| is_fast =", tok.is_fast, "| backend =", tok.backend)
```

**需要观察的现象 / 预期结果**：

- 快速那条：`is_fast = True`，`backend = "tokenizers"`，类名应是 `TokenizersBackend` 或其子类（如 `LlamaTokenizer`）。
- 慢速那条：`is_fast = False`，`backend = "sentencepiece"`，类名应为 `SentencePieceBackend`。
- 两者对同一句 `"Hello world"` 的 `encode` 结果应当一致（因为底层词表相同）。

> ⚠️ 不同 HEAD 下 `backend="sentencepiece"` 的确切路由细节可能微调。若 `tok_slow` 的类型或 `backend` 属性与上述不符，请以本地实际输出为准（**待本地验证**）。重点是理解「`is_fast` 与 `backend` 这两个属性如何标识后端」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 v5 不再为每个模型维护一个 `XxxTokenizerFast` 类？

> **参考答案**：因为 v5 把「具体模型类」直接定义为 `TokenizersBackend` 的子类（如 `LlamaTokenizer(TokenizersBackend)`），快速实现成了默认且唯一的实现。慢速需求由通用的 `SentencePieceBackend` 兜底，不再需要逐模型复制一份慢速类，从而消除了「慢/快两类必须保持同步」的维护负担（这也呼应 u7-l3 将要讲的 Modular「减少重复」理念）。

**练习 2**：`PreTrainedTokenizer` 这个名字在 v5 指向哪个类？

> **参考答案**：通过 `tokenization_python.py` 末尾的别名 `PreTrainedTokenizer = PythonBackend`，它就是慢速抽象基类 `PythonBackend`，是 `SentencePieceBackend` 的父类。

---

### 4.2 SentencePieceBackend：慢速后端源码精读

#### 4.2.1 概念说明

`SentencePieceBackend` 是「慢速」路线里最常见的一个具体后端。它的「慢」不是指代码写得差，而是指它通过 Python 调用 Google 的 **SentencePiece** C++ 库（`import sentencepiece as spm`），逐条文本、逐次调用 C++ 接口来完成切词，缺少 Rust 后端的批量并行能力。

它解决两个问题：

1. 把 `PythonBackend` 里那些 `raise NotImplementedError` 的抽象方法（`_tokenize`、`_convert_token_to_id` 等）用 `sentencepiece` 的 API 真正实现出来；
2. 直接读取 SentencePiece 训练产物 `tokenizer.model`（一个 protobuf 二进制），无需任何预制 JSON。

#### 4.2.2 核心流程

构造一个 `SentencePieceBackend` 的过程：

1. `requires_backends(self, "sentencepiece")` —— 检查可选依赖是否安装（呼应 u1-l4 的 `is_*_available` 机制）；未装则报清晰错误。
2. 创建 `spm.SentencePieceProcessor()`，调用 `.Load(vocab_file)` 把 `.model` 读进内存，得到 `self.sp_model`。
3. 记录词表大小 `total_vocab_size = self.sp_model.get_piece_size()`，再调用父类 `__init__` 完成特殊 token、trie 等通用初始化。

分词与查表时，所有「真正干活」的调用都转发给 `self.sp_model`：

- 切词 `_tokenize(text)` → `self.sp_model.encode(text, out_type=str)`
- token→id `_convert_token_to_id(token)` → `self.sp_model.piece_to_id(token)`
- id→token `_convert_id_to_token(i)` → `self.sp_model.IdToPiece(i)`
- 词表大小 `vocab_size` → `self.sp_model.get_piece_size()`

#### 4.2.3 源码精读

类定义与依赖声明：

- [src/transformers/tokenization_utils_sentencepiece.py:L39](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_sentencepiece.py#L39) —— `VOCAB_FILES_NAMES = {"vocab_file": "tokenizer.model"}`：慢速后端认的词表文件就是 SentencePiece 的 `tokenizer.model`（对比快速后端的 `tokenizer.json`）。
- [src/transformers/tokenization_utils_sentencepiece.py:L45-L56](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_sentencepiece.py#L45-L56) —— `class SentencePieceBackend(PreTrainedTokenizer)`，即继承慢速抽象基类 `PythonBackend`。

构造函数里加载 SentencePiece 模型：

- [src/transformers/tokenization_utils_sentencepiece.py:L62](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_sentencepiece.py#L62) —— `requires_backends(self, "sentencepiece")`：把 `sentencepiece` 这个可选依赖的缺失检测前置。
- [src/transformers/tokenization_utils_sentencepiece.py:L75-L76](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_sentencepiece.py#L75-L76) —— `tokenizer = spm.SentencePieceProcessor(...)` + `tokenizer.Load(self.vocab_file)`：这里才真正绑定到 C++ 后端。`self.sp_model` 就是这个处理器。
- [src/transformers/tokenization_utils_sentencepiece.py:L70-L71](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_sentencepiece.py#L70-L71) —— `kwargs["backend"] = "sentencepiece"`：在构造时就把 `self.backend` 标成 `"sentencepiece"`，与 4.1.3 看到的字段对上。

抽象方法的落地（「模板方法」被填充）：

- [src/transformers/tokenization_utils_sentencepiece.py:L204-L221](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_sentencepiece.py#L204-L221) —— `_tokenize`。它处理了一个 SentencePiece 著名的坑：当关闭 `add_dummy_prefix`（即 `legacy=False`）时，直接 encode 会丢掉句首的 `▁`，所以这里用「先拼上 `unk_token` 再 encode、再切掉前缀」的技巧来还原正确结果。注释里给了具体例子。
- [src/transformers/tokenization_utils_sentencepiece.py:L223-L225](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_sentencepiece.py#L223-L225) —— `_convert_token_to_id` 一行转调 `self.sp_model.piece_to_id(token)`。
- [src/transformers/tokenization_utils_sentencepiece.py:L100-L102](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_sentencepiece.py#L100-L102) —— `vocab_size` 属性转调 `self.sp_model.get_piece_size()`。
- [src/transformers/tokenization_utils_sentencepiece.py:L232-L235](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_sentencepiece.py#L232-L235) —— `convert_tokens_to_string`：把 `▁`（`SPIECE_UNDERLINE`）替换回空格再拼接，这是 SentencePiece 系解码的标准动作。

> 一句话总结本节：`SentencePieceBackend` 几乎所有「真活儿」都是对 `self.sp_model`（C++ 处理器）的一行转调，它本身只负责「适配」与「填抽象方法」。

#### 4.2.4 代码实践

**实践目标**：直接实例化 `SentencePieceBackend`，观察它的属性与编码行为，体会它对 `sp_model` 的依赖。

**操作步骤**（需 `pip install sentencepiece`，并准备一个含 `tokenizer.model` 的目录，如已缓存的 `hf-internal-testing/llama-tokenizer`）：

```python
# 示例代码
from transformers import SentencePieceBackend  # 慢速后端（若该名不可直接导入，可用 AutoTokenizer(backend="sentencepiece") 等价获得）

tok = SentencePieceBackend.from_pretrained("hf-internal-testing/llama-tokenizer")

print("is_fast     =", tok.is_fast)
print("backend     =", tok.backend)
print("vocab_size  =", tok.vocab_size)
print("sp_model类型 =", type(tok.sp_model))   # sentencepiece.SentencePieceProcessor
print("encode结果  =", tok.encode("Hello world"))
```

**需要观察的现象 / 预期结果**：

- `is_fast = False`，`backend = "sentencepiece"`。
- `sp_model` 是 `sentencepiece.SentencePieceProcessor` 实例（C++ 对象）。
- `vocab_size` 与 `encode` 结果应与 4.1.4 中快速后端的一致。
- 若 `SentencePieceBackend` 不能直接 `import`，改用 `AutoTokenizer.from_pretrained(repo, backend="sentencepiece")` 同样可获得慢速实例（**待本地验证**导入路径）。

#### 4.2.5 小练习与答案

**练习**：阅读 [_tokenize 的源码](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_sentencepiece.py#L204-L221)。为什么在 `legacy=False` 分支里要先 `encode(self.unk_token + text)` 再切掉前 `unk_token_length` 个 token，而不是直接 `encode(text)`？

> **参考答案**：SentencePiece 在 `add_dummy_prefix=False`（非 legacy 模式）下会丢弃句首占位 `▁`，导致句首单词的切分与训练时不一致。先把 `unk_token` 拼到句首「骗」SentencePiece 产生前缀，再删掉这些前缀 token，就能在不改词表的前提下还原出正确切分。这是「用现有 C++ 工具变通实现期望语义」的典型适配代码。

---

### 4.3 TokenizersBackend：快速后端源码精读

#### 4.3.1 概念说明

`TokenizersBackend` 是 v5 的默认后端，也是所有「具体模型分词器类」的父类。它把全部重活儿委托给一个 Rust 对象 `self._tokenizer`（类型是 `tokenizers.Tokenizer`）。相比慢速后端，它有三个核心优势：

1. **快**：分词在 Rust 侧批量并行完成；
2. **自带对齐信息**：Rust `Encoding` 对象记录了每个 token 的字符偏移与所属 word，这正是 u3-l2 讲的 `word_ids()` / `token_to_chars()` 得以工作的基础（慢速后端没有这些）；
3. **单一序列化文件**：整个分词器（词表 + 归并 + normalizer + decoder + post_processor）可序列化为一个 `tokenizer.json`，加载时无需拼装。

#### 4.3.2 核心流程

`TokenizersBackend` 在 `__init__` 里要构造出 `self._tokenizer`（Rust 对象），有四条来源（按优先级）：

1. **`tokenizer.json` 直接加载**：`TokenizerFast.from_file(tokenizer_file)`——最快、最常见；
2. **从已有 `tokenizers.Tokenizer` 对象深拷贝**（`tokenizer_object` 参数）；
3. **从 `vocab`/`merges` 现场拼装**一个 BPE/Unigram 的 Rust Tokenizer；
4. **从慢速资产（`.model`）现场转换**——这正是本讲后半段 Converter 体系的触发点。

一旦 `self._tokenizer` 就位，此后几乎所有方法都只是对它的转发。它还把 `self.backend` 标为 `"tokenizers"`。

#### 4.3.3 源码精读

类定义与文件约定：

- [src/transformers/tokenization_utils_tokenizers.py:L56](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_tokenizers.py#L56) —— `TOKENIZER_FILE = "tokenizer.json"`：快速后端认的序列化文件。
- [src/transformers/tokenization_utils_tokenizers.py:L80](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_tokenizers.py#L80) —— `VOCAB_FILES_NAMES = {"tokenizer_file": TOKENIZER_FILE, "vocab_file": TIKTOKEN_VOCAB_FILE}`：与慢速后端的 `{"vocab_file": "tokenizer.model"}` 形成对照。
- [src/transformers/tokenization_utils_tokenizers.py:L84-L95](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_tokenizers.py#L84-L95) —— `class TokenizersBackend(PreTrainedTokenizerBase)`，注意它**直接继承基类**，不走 `PythonBackend`。
- [src/transformers/tokenization_utils_tokenizers.py:L98-L99](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_tokenizers.py#L98-L99) —— `model = None` 与 `_tokenizer = None`：类级占位，`_tokenizer` 就是那个 Rust 对象的槽位。

构造 Rust 后端对象的四条来源：

- [src/transformers/tokenization_utils_tokenizers.py:L349-L388](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_tokenizers.py#L349-L388) —— `__init__` 中决定 `fast_tokenizer` 的分支：`tokenizer_object` 深拷贝 → `from_file` → gguf → 从 vocab/merges 拼 → 否则报错；最后 `self._tokenizer = fast_tokenizer`。
- [src/transformers/tokenization_utils_tokenizers.py:L412-L414](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_tokenizers.py#L412-L414) —— `kwargs["backend"] = "tokenizers"`：把后端身份标成快速。
- [src/transformers/tokenization_utils_tokenizers.py:L490-L492](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_tokenizers.py#L490-L492) —— `is_fast` 恒为 `True`，与慢速后端对称。

「转发给 Rust」的典型方法（注意它们都只是一行转调）：

- [src/transformers/tokenization_utils_tokenizers.py:L722-L723](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_tokenizers.py#L722-L723) —— `_convert_id_to_token` → `self._tokenizer.id_to_token(...)`。
- [src/transformers/tokenization_utils_tokenizers.py:L778-L779](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_tokenizers.py#L778-L779) —— `tokenize` 委托给 `_encode_plus(...).tokens()`。
- [src/transformers/tokenization_utils_tokenizers.py:L1010-L1015](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_tokenizers.py#L1010-L1015) —— `convert_tokens_to_string` → `self.backend_tokenizer.decoder.decode(tokens)`，用 Rust 的 decoder 完成还原。

`self.backend` 被下游使用的真实例子（证明这个字段不是摆设）：

- [src/transformers/processing_utils.py:L2047](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/processing_utils.py#L2047) —— `is_tokenizers_fast = self.tokenizer.backend == "tokenizers"`：多模态 Processor 正是通过这个字段判断「我手里的 tokenizer 是不是快速后端」，从而决定能否使用对齐等高级特性。这是 v5 后端溯源信息的实际用途。

#### 4.3.4 代码实践

**实践目标**：定位快速后端的 Rust 对象，并对比快速/慢速后端在大批量编码上的速度差。

**操作步骤**：

```python
# 示例代码
import time
from transformers import AutoTokenizer

repo = "hf-internal-testing/llama-tokenizer"
text = "The quick brown fox jumps over the lazy dog. " * 2000   # 构造一个长文本
batch = [text] * 50

tok_fast = AutoTokenizer.from_pretrained(repo, backend="tokenizers")
tok_slow = AutoTokenizer.from_pretrained(repo, backend="sentencepiece")

# 1. 看一眼 Rust 后端对象
print("Rust后端类型:", type(tok_fast._tokenizer).__module__ + "." + type(tok_fast._tokenizer).__name__)

# 2. 速度对比
for name, tok in [("fast", tok_fast), ("slow", tok_slow)]:
    t0 = time.perf_counter()
    for _ in range(5):
        tok(batch)
    print(f"{name}: {(time.perf_counter() - t0) * 1000:.1f} ms")
```

**需要观察的现象 / 预期结果**：

- `tok_fast._tokenizer` 是 `tokenizers.Tokenizer`（Rust 对象）。
- 快速后端耗时应明显低于慢速后端（具体倍数**待本地验证**，随文本长度与机器而变）。
- 两者 `batch` 编码出的 `input_ids` 应逐一致。

> 这个实践呼应了「为什么要分两个后端」：同样的接口、同样的结果，但快速后端凭借 Rust 批量处理获得数量级的速度优势。

#### 4.3.5 小练习与答案

**练习 1**：u3-l2 讲过 `word_ids()`、`token_to_chars()` 等对齐方法只在快速分词器上可用。结合本节源码，解释为什么慢速后端做不到。

> **参考答案**：对齐信息（每个 token 的字符偏移、所属 word）由 Rust `tokenizers` 库在分词时一并计算并保存在 `Encoding` 对象里；`TokenizersBackend` 直接持有这些 `Encoding`（即 `BatchEncoding.encodings`）。而 `SentencePieceBackend` 调用的 `sentencepiece` C++ 接口不返回偏移信息，也没有等价的 `Encoding` 结构，因此无法提供 span 对齐。

**练习 2**：`TokenizersBackend` 与 `SentencePieceBackend` 谁是另一个的子类吗？

> **参考答案**：都不是。二者都直接继承 `PreTrainedTokenizerBase`，是并列的「兄弟」后端，只是各自把抽象方法用不同底层库实现了一遍。

---

### 4.4 Converter 体系：从慢速「翻译」成快速

#### 4.4.1 概念说明

很多模型只在 Hub 上放了 SentencePiece 的 `tokenizer.model`，并没有现成的 `tokenizer.json`。如果用户用的是默认的快速后端，库就必须**现场把这份慢速资产翻译成 Rust `tokenizers.Tokenizer`**。承担这一翻译工作的就是 `convert_slow_tokenizer.py` 里的 **Converter 体系**。

Converter 体系有三层：

1. **`Converter` 基类**：定义统一接口 `converted() -> tokenizers.Tokenizer`，输入是「一个慢速分词器实例」，输出是「一个等价的 Rust Tokenizer」。
2. **按算法/模型家族的子类**：如 `BertConverter`（WordPiece）、`GPT2Converter`（BPE）、`SpmConverter`（SentencePiece/Unigram）。每个子类知道「如何把自己家族的词表与规则翻译成 Rust 的 model + normalizer + pre_tokenizer + decoder + post_processor 五件套」。
3. **注册表 `SLOW_TO_FAST_CONVERTERS`**：一张「慢速类名 → Converter 类」的字典，是查找入口。

此外还有一个**提取器** `SentencePieceExtractor`：它不依赖某个慢速分词器实例，而是**直接解析 `.model` 这个 protobuf 二进制**，把词表、分数、特殊 token、normalizer 的 charsmap 取出来——这让「没有慢速分词器类、只有 `.model` 文件」的场景也能完成转换。

#### 4.4.2 核心流程

转换的两条触发路径：

**路径 A：有慢速分词器实例**（调用 `convert_slow_tokenizer(tok)`）：

```
convert_slow_tokenizer(slow_tok)
   │  查 SLOW_TO_FAST_CONVERTERS[slow_tok.__class__.__name__]
   ▼
ConverterClass(slow_tok).converted()
   │  读取 slow_tok 的词表/规则
   ▼
   组装 Rust Tokenizer（model + normalizer + pre_tokenizer + decoder + post_processor）
   ▼
返回 tokenizers.Tokenizer
```

**路径 B：只有 `.model` 文件**（加载时自动触发，见 4.4.4）：

```
SentencePieceExtractor(vocab_file).extract(...)
   │  直接解析 protobuf proto
   ▼
得到 vocab / merges / special tokens / charsmap
   ▼
若存在模型专属 Converter，再调其 convert_from_spm(...) 做调整
   ▼
组装 Rust Tokenizer
```

SentencePiece 模型翻译成 Rust 五件套的对应关系：

\[ \underbrace{\text{proto.pieces}}_{\text{词表}} \Rightarrow \text{model (Unigram/BPE)};\quad \underbrace{\text{normalizer\_spec.precompiled\_charsmap}}_{\text{归一化规则}} \Rightarrow \text{normalizer};\quad \underbrace{\text{▁ 占位约定}}_{\text{Metaspace}} \Rightarrow \text{pre\_tokenizer / decoder} \]

#### 4.4.3 源码精读

基类与「实例式」转换：

- [src/transformers/convert_slow_tokenizer.py:L218-L223](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/convert_slow_tokenizer.py#L218-L223) —— `class Converter`，`converted()` 抛 `NotImplementedError`，是所有 Converter 的接口。
- [src/transformers/convert_slow_tokenizer.py:L226-L260](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/convert_slow_tokenizer.py#L226-L260) —— `BertConverter.converted()` 是一个完整范例：它从慢速 `BertTokenizer` 取词表，构造 `WordPiece` 模型，再依次装配 `BertNormalizer`、`BertPreTokenizer`、`TemplateProcessing`（加 `[CLS]/[SEP]`）、`WordPiece` decoder。这五步正是「翻译」的本质——把 WordPiece 家族的规则逐一映射到 Rust 组件。
- [src/transformers/convert_slow_tokenizer.py:L2039-L2058](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/convert_slow_tokenizer.py#L2039-L2058) —— 公开函数 `convert_slow_tokenizer(transformer_tokenizer, from_tiktoken=False)`：用 `tokenizer.__class__.__name__` 查注册表，命中则 `converter_class(tok).converted()`。这是「路径 A」的总入口。

SentencePiece 家族的翻译器 `SpmConverter`：

- [src/transformers/convert_slow_tokenizer.py:L632-L635](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/convert_slow_tokenizer.py#L632-L635) —— `class SpmConverter(Converter)`，带 `handle_byte_fallback`、`special_tokens` 等类属性，便于子类微调。
- [src/transformers/convert_slow_tokenizer.py:L722-L770](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/convert_slow_tokenizer.py#L722-L770) —— `tokenizer(proto)`：根据 proto 的 `model_type`（1=Unigram、2=BPE）构造对应的 Rust 模型，并把 SentencePiece 里 type 为 3/4 的 piece 当作 added/special token 加回去。这是「词表与特殊 token」的翻译。
- [src/transformers/convert_slow_tokenizer.py:L794-L816](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/convert_slow_tokenizer.py#L794-L816) —— `converted()`：组装流水线，依次设置 normalizer、pre_tokenizer（`Metaspace`，处理 `▁`）、decoder、post_processor，返回最终 Rust `Tokenizer`。
- [src/transformers/convert_slow_tokenizer.py:L685-L693](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/convert_slow_tokenizer.py#L685-L693) —— 类方法 `convert_from_spm`：与「路径 B」配套的钩子，允许在没有慢速实例、直接面对 proto 产物时对 vocab 做模型专属调整。

模型专属子类示例 `LlamaConverter`：

- [src/transformers/convert_slow_tokenizer.py:L1615-L1654](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/convert_slow_tokenizer.py#L1615-L1654) —— `class LlamaConverter(SpmConverter)`，`handle_byte_fallback = True`，并覆盖 `vocab`/`unk_id`/`decoder`/`normalizer`/`pre_tokenizer` 以匹配 Llama 的字节回退（ByteFallback）与无归一化策略。这展示了「同属 SentencePiece 家族、但各有定制」的写法。

注册表与提取器：

- [src/transformers/convert_slow_tokenizer.py:L1979-L2036](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/convert_slow_tokenizer.py#L1979-L2036) —— `SLOW_TO_FAST_CONVERTERS` 字典，例如 `"LlamaTokenizer": LlamaConverter`、`"T5Tokenizer": T5Converter`、`"BertTokenizer": BertConverter`。它是 `convert_slow_tokenizer()` 的查找依据。
- [src/transformers/convert_slow_tokenizer.py:L144-L194](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/convert_slow_tokenizer.py#L144-L194) —— `SentencePieceExtractor`：`__init__` 直接 `ParseFromString` 读取 `.model` 的 protobuf；`extract` 把 proto 翻译成 `vocab`（带分数）、`merges`、`additional_special_tokens` 与 `_spm_precompiled_charsmap`。它让「路径 B」无需任何慢速类即可工作。

#### 4.4.4 代码实践（源码阅读型）

**实践目标**：跟踪「加载一个只有 `.model` 的 checkpoint」时，快速后端如何在加载阶段自动触发转换。

**操作步骤**（纯源码跟踪，无需运行）：

1. 打开 [tokenization_utils_tokenizers.py:L102](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_tokenizers.py#L102) 的类方法 `convert_to_native_format`，这是「把各类序列化文件归一成 Rust Tokenizer」的调度中心。
2. 跟到 [L210-L217](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_tokenizers.py#L210-L217)：当 `vocab_file` 以 `.model` 结尾时，调用 `SentencePieceExtractor(vocab_file).extract(cls.model, ...)`，直接从 protobuf 取词表——这就是 4.4.3 的「路径 B」。
3. 再看 [L219-L225](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/tokenization_utils_tokenizers.py#L219-L225)：随后查 `SLOW_TO_FAST_CONVERTERS.get(cls.__name__)`，若该模型有专属 Converter 且实现了 `convert_from_spm`，就调用它做模型级微调。
4. 把这条链路用一句话写出来：「`.model` → SentencePieceExtractor 取词表 → 模型专属 Converter 微调 → 装配成 Rust `self._tokenizer`」。

**需要观察的现象 / 预期结果**：

- 你应当能解释：为什么一个**只发布了 `tokenizer.model`** 的 checkpoint，用默认快速后端也能加载成功——因为在 `__init__` 阶段，`convert_to_native_format` 已经「现场翻译」出了一个等价的 Rust Tokenizer。
- 若想手动复现「路径 A」，可阅读并运行库自带的测试 [tests/utils/test_convert_slow_tokenizer.py](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/tests/utils/test_convert_slow_tokenizer.py)（如 `test_spm_converter_bytefallback_warning`），它直接 `from transformers.convert_slow_tokenizer import SpmConverter` 验证转换行为。

#### 4.4.5 小练习与答案

**练习 1**：`convert_slow_tokenizer(slow_tok)` 是如何决定用哪个 Converter 的？

> **参考答案**：它取 `slow_tok.__class__.__name__`（如 `"LlamaTokenizer"`），到 `SLOW_TO_FAST_CONVERTERS` 字典里查对应的 Converter 类（如 `LlamaConverter`），再 `LlamaConverter(slow_tok).converted()`。所以注册表是「类名 → Converter」的单一映射。

**练习 2**：`SentencePieceExtractor` 与 `SpmConverter` 都处理 SentencePiece，它们分工有何不同？

> **参考答案**：`SentencePieceExtractor` 是「数据提取层」——它直接解析 `.model` 的 protobuf，取出原始的词表、分数、特殊 token 与 charsmap，不关心最终怎么装配。`SpmConverter` 是「装配层」——它消费这些提取结果，按照 Rust `tokenizers` 的五件套（model/normalizer/pre_tokenizer/decoder/post_processor）组装出一个可用的 Rust Tokenizer，并允许子类（如 `LlamaConverter`）覆盖其中任一环节以匹配模型定制。提取器让转换可以脱离「慢速分词器实例」独立工作。

**练习 3**：为什么 `SpmConverter.__init__` 里会对 `byte_fallback` 发出警告？

> **参考答案**：当原始 SentencePiece 开启了 `byte_fallback`（未知词会被拆成字节序列）但当前 Converter 不处理它（`handle_byte_fallback=False`）时，转换出的快速版本无法复现这一行为，可能产生 `<unk>` 而慢速版本则不会。警告提醒用户：快速版与慢速版在该特性上不完全等价。`LlamaConverter` 通过设 `handle_byte_fallback = True` 并定制 decoder 来消除这个差异。

---

## 5. 综合实践

把本讲三块知识（两个后端 + Converter）串起来，完成下面这个「分词器体检」小任务。

**任务**：给定一个 SentencePiece 系 checkpoint（如 `hf-internal-testing/llama-tokenizer`），完成三件事并记录结论。

1. **双后端对比**：分别用 `backend="tokenizers"` 与 `backend="sentencepiece"` 加载，打印 `type()`、`is_fast`、`backend`、`vocab_size`，确认它们走的是不同后端但词表大小一致。

2. **手动转换（路径 A）**：阅读 [convert_slow_tokenizer.py:L2039](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/convert_slow_tokenizer.py#L2039) 的 `convert_slow_tokenizer` 函数。尝试把慢速实例喂给它（若其类名不在 `SLOW_TO_FAST_CONVERTERS` 中，函数会抛出可读的 `ValueError` 并列出所有支持的类名——这本身就是一个有用的观察）。若成功，打印返回对象的类型，确认它是 `tokenizers.Tokenizer`。

   ```python
   # 示例代码
   from transformers.convert_slow_tokenizer import convert_slow_tokenizer
   # 假设 tok_slow 是上一步得到的慢速实例
   try:
       rust_tok = convert_slow_tokenizer(tok_slow)
       print("转换成功，类型 =", type(rust_tok))
   except Exception as e:
       print("转换失败：", e)
   ```

3. **等价性核对**：取一句话，分别用慢速实例、默认快速实例、以及第 2 步手动转换得到的 Rust 对象（用 `rust_tok.encode(...).ids`）编码，比较三者得到的 id 序列是否一致。

**预期结果与现象**：

- 第 1 步：两个后端 `vocab_size` 相同，`is_fast`/`backend` 不同。
- 第 2 步：若慢速实例的类名在注册表中，转换应成功返回 `tokenizers.Tokenizer`；否则你会看到那条列出「currently available converters」的错误信息——这正是注册表内容的实时写照（具体能否命中**待本地验证**，取决于该 HEAD 下慢速实例的实际类名）。
- 第 3 步：三者 id 序列应当一致，证明「慢速后端」「快速后端」「手动转换的 Rust 对象」三者语义等价——这正是整个 Converter 体系存在的意义。

> 完成本任务后，你应当能用自己的话讲清：「为什么只发了 `.model` 的模型也能用快速分词器，且结果和慢速分词器一致。」

## 6. 本讲小结

- transformers 有**两条并列的分词后端**：慢速 `SentencePieceBackend`（Python 调 `sentencepiece` C++ 库，`is_fast=False`）与快速 `TokenizersBackend`（封装 Rust `tokenizers` 库，`is_fast=True`），二者都直接继承 `PreTrainedTokenizerBase`，互不为父子。
- **v5 的关键变化**：具体模型分词器类（如 `LlamaTokenizer`）统一继承 `TokenizersBackend`，不再有 `*Fast` 伴生类；旧的 `use_fast` 参数被忽略，改用 `AutoTokenizer(..., backend="tokenizers"|"sentencepiece")` 选择后端。
- 慢速后端的几乎所有方法都是对 `self.sp_model`（C++ 处理器）的一行转调，它本身只负责「填抽象方法 + 适配 SentencePiece 的坑」。
- 快速后端把全部重活委托给 Rust 对象 `self._tokenizer`，并因此获得速度优势与对齐信息（u3-l2 的 `word_ids` 等）；`self.backend == "tokenizers"` 还会被 Processor 等下游用来探测能力。
- **Converter 体系**（`convert_slow_tokenizer.py`）负责把慢速资产翻译成 Rust 对象：`Converter` 基类 → 家族子类（`SpmConverter`/`BertConverter`/`LlamaConverter`…）→ `SLOW_TO_FAST_CONVERTERS` 注册表；`SentencePieceExtractor` 让转换可以脱离慢速实例、直接解析 `.model` 的 protobuf。
- 加载阶段，`TokenizersBackend.convert_to_native_format` 会自动触发转换：有 `tokenizer.json` 就直接加载，否则用 `SentencePieceExtractor` + 模型专属 Converter 现场「翻译」出 Rust Tokenizer。

## 7. 下一步学习建议

- **继续分词器主线**：下一讲 u3-l4 将讲**聊天模板 Chat Template**——它建立在分词器之上，把多轮对话渲染成模型期望的输入格式，是使用大模型做对话的必备知识。
- **深入模型实现**：学完本讲后，u7-l1/u7-l2 会带你走进 `models/llama/` 目录，把分词器与 `configuration_llama.py`、`modeling_llama.py` 串成一个完整模型；届时你会看到 `LlamaTokenizer(TokenizersBackend)` 如何与模型协作。
- **想动手扩展**：若你对「如何让一个全新模型也能被快速后端加载」感兴趣，可提前翻阅 u7-l3（Modular 机制）与 u11-l2（添加新模型），它们都会涉及 Converter 与注册表的协作。
- **建议阅读的源码**：再读一遍 [`SpmConverter.converted()`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/convert_slow_tokenizer.py#L794-L816)，对照 [`BertConverter.converted()`](https://github.com/huggingface/transformers/blob/af71155683b4d34dd92d8f037392fa6bf334035e/src/transformers/convert_slow_tokenizer.py#L226-L260)，体会「不同家族如何被翻译成同一套 Rust 五件套」——这是理解整个分词器架构最划算的一段源码。
