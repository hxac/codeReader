# 分词与词表构建

## 1. 本讲目标

在第 1 单元里，我们已经能跑通 `ch04/01_main-chapter-code/gpt.py`，让一个未训练的 GPT 模型吐出一串「像 token、却没语义」的文字。但有个问题被我们刻意跳过了：**模型到底是怎么把人类写的句子读进去的？** 模型只会做张量（多维数字数组）运算，它既不认识字母 `H`，也不认识单词 `Hello`。所以喂给模型之前，必须先把文本「翻译」成数字。

本讲是「文本数据处理流水线」的第一步，学完后你应当能够：

1. 说清楚**为什么**要把文本变成数字，以及「词嵌入（word embedding）」在整个流水线里的位置。
2. 用一段简单的 Python + 正则表达式，把一段英文切成**词（word）和标点**这样的 token。
3. 从切出来的 token 构建一个**词表（vocabulary）**，并实现「文本 ↔ 整数 ID」的**编码（encode）和解码（decode）**。
4. 理解 `<|endoftext|>`、`<|unk|>` 这类**特殊上下文 token** 为什么必须存在，以及它们解决了什么问题。

> 本讲对应原书第 2 章的 **2.1 ～ 2.4 节**。2.5 节的 BPE（字节对编码）留给下一讲 `u2-l2`，滑动窗口采样和嵌入层留给 `u2-l3` / `u2-l4`。

---

## 2. 前置知识

本讲是整本手册里最「轻量」的一讲，不需要你懂深度学习，但下面几个概念最好先有个直觉：

- **张量 / 数字数组**：神经网络内部的所有计算都是在数字数组上进行的（加、乘、求导）。所以任何输入，最终都得是数字。图片是像素值数组，文本也不例外——只是「文字→数字」这一步没那么直观。
- **字符（character）与单词（word）**：`Hello` 是 5 个字符组成的 1 个单词。分词（tokenization）就是决定「按什么粒度把文本切碎」。
- **正则表达式（regex）**：一种描述文本模式的小语言。比如 `\s` 代表「任意空白字符」，`[,.]` 代表「逗号或句号」。本讲只用到了 `re.split` 这一个函数，不必系统学正则。
- **字典（dict）与集合（set）**：Python 基础。`set` 自动去重，`dict` 做「键→值」映射，这两者是构建词表的核心工具。
- **「从零（from scratch）」的含义**：如 `u1-l1` 所述，本书尽量不用 `transformers` 这类高层库。但分词器是个**例外**——真正实用的分词器（BPE）我们会直接用 `tiktoken` 库（下一讲）。本讲我们**手写**一个最简陋的词级分词器，目的不是好用，而是让你理解「分词 + 词表」这件事本身。

如果你已经会读 Python、知道神经网络要吃数字输入，就足够了。

---

## 3. 本讲源码地图

本讲只涉及第 2 章主目录下的文件：

| 文件 | 作用 |
| --- | --- |
| [`ch02/01_main-chapter-code/ch02.ipynb`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch02/01_main-chapter-code/ch02.ipynb) | **唯一的核心源码**。第 2 章全部代码都在这个 notebook 里逐行演进，本讲引用其中的 2.1～2.4 节。 |
| [`ch02/01_main-chapter-code/the-verdict.txt`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch02/01_main-chapter-code/the-verdict.txt) | 训练用的小语料：Edith Wharton 的短篇小说《The Verdict》全文，共 **20479** 个字符。 |
| [`ch02/01_main-chapter-code/README.md`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch02/01_main-chapter-code/README.md) | 说明 `ch02.ipynb` 是正文代码、`dataloader.ipynb` 是精简版数据加载流水线。 |

> **关于永久链接与「行号」的约定**：`ch02.ipynb` 是 Jupyter notebook（本质是 JSON），GitHub 渲染时按**单元格（cell）和章节标题**展示，不像 `.py` 文件那样有可直接跳转的文件行号。因此本讲引用 notebook 时，链接文本里会写明**小节号（如 §2.2）和该处代码做了什么**，而不是编造对读者无意义的 JSON 行号。所有链接都指向当前 HEAD `ff0b3d9` 的固定版本。

---

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块，正好对应原书的 2.1～2.4 节：先理解动机，再切词，再做 ID 映射，最后补上特殊 token。

### 4.1 词嵌入动机：为什么要先把文本变成数字

#### 4.1.1 概念说明

一句话：**神经网络只会算数字，不会读文字。** 所以无论训练还是推理，文本都必须先经历这样一条流水线：

```
原始文本  →  分词(tokenization)  →  token 序列  →  查词表  →  token ID 序列  →  嵌入层  →  连续向量序列  →  进入 Transformer
```

前三步（分词、查词表、得到 ID）就是**本讲的全部内容**；「嵌入层把 ID 变成向量」会在 `u2-l4` 讲。这里先建立一个直觉：

- **token**：文本被切成的最小单元，本讲里一个 token ≈ 一个单词或一个标点。
- **token ID**：给每个不同 token 分配的一个整数编号（0, 1, 2, …）。
- **词表（vocabulary）**：所有「出现过」的 token 到 ID 的映射表。
- **词嵌入（word embedding）**：把一个整数 ID 查表换成一个**固定长度的连续向量**（比如 256 维）。这一步是为了让模型能用「距离」「方向」来衡量词与词的关系，而不是把 `Hello` 当成一个孤立的整数 42。

嵌入层本质上就是一次查表（lookup）：

\[
\mathbf{e} = W[\,\text{token\_id}\,], \qquad W \in \mathbb{R}^{|V|\times d}
\]

其中 \( |V| \) 是词表大小，\( d \) 是向量维度。注意：**没有词表和 ID，就没有嵌入**——这就是为什么我们必须先学会分词。

#### 4.1.2 核心流程

notebook 2.1 节本身没有代码（纯概念），紧接着的第一段代码是**加载语料**：

1. 把 `the-verdict.txt` 整篇读进一个字符串 `raw_text`。
2. 打印总字符数和开头片段，确认数据可用。

#### 4.1.3 源码精读

加载语料的代码（[`ch02.ipynb` §2.2 — 读取 the-verdict.txt](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch02/01_main-chapter-code/ch02.ipynb)）：

```python
with open("the-verdict.txt", "r", encoding="utf-8") as f:
    raw_text = f.read()

print("Total number of character:", len(raw_text))
print(raw_text[:99])
```

输出告诉我们整篇共 **20479** 个字符，开头是 `I HAD always thought Jack Gisburn rather a cheap genius--though a good fellow enough--so it was no `。注意里面的 `--`（双连字符，破折号）——后面分词时我们要专门处理它。

> 小贴士：notebook 里还有一段用 `requests` 下载 `the-verdict.txt` 的代码（仓库里已经自带该文件，所以你不必再下载）。它的作用只是「本地没有语料时自动拉取」。

#### 4.1.4 代码实践

**目标**：亲手确认语料的基本规模，建立对「待处理文本」的体感。

1. 进入 `ch02/01_main-chapter-code/` 目录，启动 Python。
2. 执行上面那段读取代码。
3. 观察：

   - `len(raw_text)` 应输出 `20479`。
   - 开头片段里能看到的标点有 `,`、`--` 等。

**预期结果**：字符数与 notebook 输出一致；你会注意到文本里既有英文单词，也有逗号、句号、双连字符等标点——这正是后面分词要照顾的对象。

#### 4.1.5 小练习与答案

**练习 1**：为什么我们不能直接把 `raw_text` 这个字符串塞给 GPT 模型？
**答案**：模型只接受数字张量作为输入。字符串必须先切成 token、再映射成整数 ID、最后通过嵌入层变成向量，模型才能处理。

**练习 2**：词表大小 \( |V| \) 和嵌入矩阵 \( W \) 的形状有什么关系？
**答案**：\( W \) 的行数等于词表大小 \( |V| \)（每个 token 占一行），列数等于嵌入维度 \( d \)。词表越大，嵌入矩阵越大。

---

### 4.2 简单分词与词表构建

#### 4.2.1 概念说明

**分词（tokenization）** 就是把一长串文本切成一个个 token。最直觉的做法是「按空格切」，但这样会把 `Hello,`（带逗号）当成一个 token，导致 `Hello,` 和 `Hello` 成了两个不同的词，词表会膨胀，模型也学不到「逗号是逗号、词是词」的结构。

更好的做法是：**既按空格切，也把标点单独切出来**。这样 `Hello, world.` 会被切成 `['Hello', ',', 'world', '.']`，标点成为独立的 token。

本书用一个**正则表达式**一次性完成「切空格 + 切标点」：

```python
re.split(r'([,.:;?_!"()\']|--|\s)', text)
```

拆开看这个模式的含义：

- `[,.:;?_!"()\' ]` 这一串字符类，匹配「逗号、句号、冒号、分号、问号、下划线、感叹号、引号、括号、单引号」中的任意一个；
- `|--` 表示「或者双连字符 `--`」；
- `|\s` 表示「或者任意空白字符」；
- 外层的括号 `(...)` 是**捕获组**，让 `re.split` 在切分的同时**保留**这些分隔符本身（否则标点会被丢掉）。

#### 4.2.2 核心流程

notebook 是**循序渐进**演示这个正则的，分三步演进：

1. **只按空格切**：`re.split(r'(\s)', text)` → 得到 `['Hello,', ' ', 'world.', ' ', 'This,', ...]`，标点还黏在词上。
2. **加上逗号和句号**：`re.split(r'([,.]|\s)', text)` → 标点被切出来了，但会产生**空字符串** `''`（因为逗号后面紧跟空格，两个分隔符之间什么都没有）。
3. **补全所有标点 + 双连字符，并过滤空串**：

```python
preprocessed = re.split(r'([,.:;?_!"()\']|--|\s)', raw_text)
preprocessed = [item.strip() for item in preprocessed if item.strip()]
```

第二步的关键是 `[item for item in ... if item.strip()]`：先 `strip()` 去掉首尾空白，再把空串过滤掉，最后得到干净的 token 列表。

切完之后，**构建词表**只要两步——先去重排序，再用 `enumerate` 编号：

```python
all_words = sorted(set(preprocessed))      # 去重 + 按字典序排序
vocab = {token: integer for integer, token in enumerate(all_words)}
```

这里用 `set` 去重、`sorted` 让顺序确定（同一个语料每次得到的词表都一致），词表大小就是 `len(all_words)`。对《The Verdict》全文，切出来一共 **4690** 个 token，去重后得到 **1130** 个不同的词/标点。

#### 4.2.3 源码精读

正则分词的最终一步（[`ch02.ipynb §2.2 — 把完整正则应用到 raw_text`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch02/01_main-chapter-code/ch02.ipynb)）：

```python
preprocessed = re.split(r'([,.:;?_!"()\']|--|\s)', raw_text)
preprocessed = [item.strip() for item in preprocessed if item.strip()]
print(preprocessed[:30])
# ['I', 'HAD', 'always', 'thought', 'Jack', 'Gisburn', 'rather', 'a',
#  'cheap', 'genius', '--', 'though', ...]
```

注意 `--` 被正确地切成独立 token，而不是两个 `-`。

词表构建（[`ch02.ipynb §2.3 — 由 token 构建词表`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch02/01_main-chapter-code/ch02.ipynb)）：

```python
all_words = sorted(set(preprocessed))
vocab_size = len(all_words)          # 1130
vocab = {token:integer for integer,token in enumerate(all_words)}
```

词表前几项长这样（标点排在字母前面，因为 ASCII 里标点编码更小）：`('!', 0)`、`('"', 1)`、`("'", 2)`、`(',', 5)`、`('.', 7)`、`('--', 6)`、`('A', 11)` ……

#### 4.2.4 代码实践

**目标**：亲眼看到「按空格切」的缺陷，以及过滤空串的必要性。

1. 在 Python 里执行：

   ```python
   import re
   text = "Hello, world. This, is a test."
   print(re.split(r'([,.]|\s)', text))
   ```

2. 观察输出里出现的**空字符串** `''`。

3. 再加上过滤：

   ```python
   result = [item for item in re.split(r'([,.]|\s)', text) if item.strip()]
   print(result)
   ```

**预期结果**：第一步会看到形如 `['Hello', ',', '', ' ', 'world', '.', '', ' ', ...]` 的列表（含空串）；第二步得到干净的 `['Hello', ',', 'world', '.', 'This', ',', 'is', 'a', 'test', '.']`。

#### 4.2.5 小练习与答案

**练习 1**：为什么正则外层要加捕获组括号 `(...)`？去掉会怎样？
**答案**：捕获组让 `re.split` 把分隔符本身也保留在结果列表里。去掉括号，标点和空白会被当作分隔符**丢弃**，`Hello,` 会和 `,` 没法分开保留。

**练习 2**：为什么用 `sorted(set(...))` 而不是直接 `set(...)`？
**答案**：`set` 本身无序，词表顺序会随运行而变，导致 token ID 不稳定。`sorted` 保证每次构建的词表顺序一致、ID 一致，结果可复现。

---

### 4.3 Token ID 映射：SimpleTokenizerV1 的编码与解码

#### 4.3.1 概念说明

有了词表，我们就能把任意文本（在词表范围内的）转成 ID 序列，也能把 ID 序列还原回文本。本书把这两件事封装成一个 **`SimpleTokenizerV1`** 类，它内部维护**两张互相反向的映射**：

- `str_to_int`：词表本身，`token → ID`，用于**编码（encode）**。
- `int_to_str`：`ID → token`，由词表反推，用于**解码（decode）**。

\[
\text{encode}(t) = \text{str\_to\_int}[t], \qquad
\text{decode}(i) = \text{int\_to\_str}[i]
\]

#### 4.3.2 核心流程

- **`encode(text)`**：对输入文本跑一遍 §4.2 的正则切分与过滤，再把每个 token 查 `str_to_int` 表，得到一串整数 ID。
- **`decode(ids)`**：把每个 ID 查 `int_to_str` 还原成 token，用空格拼回字符串；最后用一个正则把**标点前的空格去掉**（因为切词时标点是独立 token，拼回来会多出空格，如 `world .` 要变回 `world.`）。

```
encode:  text →正则切分→ tokens →查表→ [id, id, ...]
decode:  [id, id, ...] →查表→ tokens →空格拼接→ 去标点前空格 → text
```

#### 4.3.3 源码精读

`SimpleTokenizerV1` 的完整定义（[`ch02.ipynb §2.3 — SimpleTokenizerV1 类`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch02/01_main-chapter-code/ch02.ipynb)）：

```python
class SimpleTokenizerV1:
    def __init__(self, vocab):
        self.str_to_int = vocab
        self.int_to_str = {i:s for s,i in vocab.items()}

    def encode(self, text):
        preprocessed = re.split(r'([,.:;?_!"()\']|--|\s)', text)
        preprocessed = [item.strip() for item in preprocessed if item.strip()]
        ids = [self.str_to_int[s] for s in preprocessed]
        return ids

    def decode(self, ids):
        text = " ".join([self.int_to_str[i] for i in ids])
        # 去掉指定标点前的空格
        text = re.sub(r'\s+([,.?!"()\'])', r'\1', text)
        return text
```

两处要点：

1. `int_to_str` 用字典推导式 `{i:s for s,i in vocab.items()}` 反转词表。
2. `decode` 里的 `re.sub(r'\s+([,.?!"()\'])', r'\1', text)` 把「一个或多个空白 + 标点」替换成「只保留标点」，修复 `world .` → `world.`。

notebook 验证它工作正常：对一句引用编码再解码，能还原出可读文本（标点前的多余空格被清理掉了）。

#### 4.3.4 代码实践

**目标**：跑通一次完整的「编码 → 解码」往返，并理解解码时的空格修复。

1. 复用 §4.2 建好的 `vocab`，实例化并编码一句书里的话：

   ```python
   tokenizer = SimpleTokenizerV1(vocab)
   text = """"It's the last he painted, you know,"
              Mrs. Gisburn said with pardonable pride."""
   ids = tokenizer.encode(text)
   print(ids)
   ```

2. 解码回来：

   ```python
   print(tokenizer.decode(ids))
   ```

**预期结果**：`encode` 输出一串整数（如 `[1, 56, 2, 850, 988, ...]`）；`decode` 输出可读句子，标点紧贴前一个词（`know,"` 而不是 `know ,"`）。注意：由于这个分词器会把 `It's` 切成 `It`、`'`、`s` 三段，解码后可能呈现 `It' s` 这样的轻微走样——这是词级分词的固有粗糙之处，正是下一讲 BPE 要改进的地方。

#### 4.3.5 小练习与答案

**练习 1**：`encode` 里如果不做 `if item.strip()` 过滤，会发生什么？
**答案**：切分会产生空字符串 `''`，而 `''` 不在词表里，`str_to_int['']` 会抛 `KeyError`。

**练习 2**：为什么 `decode` 要专门处理「标点前的空格」？
**答案**：因为标点被切成独立 token，用空格 `join` 还原时，标点前会多一个空格（`world .`）。`re.sub` 把这个多余空格去掉，文本才符合书写习惯。

---

### 4.4 特殊上下文 token：`<|endoftext|>` 与 `<|unk|>`

#### 4.4.1 概念说明

`SimpleTokenizerV1` 有个致命缺陷：**遇到词表里没有的词就崩溃**。notebook 演示了对一句含 `Hello` 的话编码——而《The Verdict》里根本没出现 `Hello`——结果直接抛出 `KeyError: 'Hello'`。

真实世界里，训练语料不可能覆盖所有词，用户输入更会出现各种生词。业界用**特殊上下文 token（special context tokens）**来兜底，常见的有：

- `[BOS]`（beginning of sequence）：标记序列开始。
- `[EOS]` / `<|endoftext|>`（end of sequence）：标记文本结束；也用来**拼接多篇互不相关的文本**（比如两篇不同的文章之间插一个，告诉模型「前面的内容到此为止」）。
- `[PAD]`（padding）：把长短不一的句子补齐到等长，便于批量训练。
- `[UNK]` / `<|unk|>`（unknown）：代表「词表里没有的生词」。

> **GPT-2 的取舍**（notebook 明确说明）：为了降低复杂度，GPT-2 **不**用 BOS/PAD/UNK 这一套，**只**用一个 `<|endoftext|>`——它同时承担「文本结束」和「padding」两种角色（padding 时反正会被注意力掩码忽略，所以用什么 token 都无所谓）。而生词问题，GPT-2 用 **BPE 子词分词**（下一讲）解决，因此**不需要** `<|unk|>`。

本讲为了教学，给我们的简易分词器加上 `<|endoftext|>` 和 `<|unk|>` 两个特殊 token，做出 `SimpleTokenizerV2`。

#### 4.4.2 核心流程

1. **扩充词表**：在原有 token 列表末尾追加两个特殊 token，再重新 `enumerate` 编号。词表大小从 **1130 → 1132**。

   ```python
   all_tokens = sorted(list(set(preprocessed)))
   all_tokens.extend(["<|endoftext|>", "<|unk|>"])
   vocab = {token:integer for integer,token in enumerate(all_tokens)}
   ```

   于是 `<|endoftext|>` 的 ID = 1130，`<|unk|>` 的 ID = 1131。

2. **改 `encode` 兜底生词**：切分后，凡是不在词表里的 token，统统替换成 `<|unk|>`，再查表，就不会再 `KeyError`。

3. **用 `<|endoftext|>` 拼接两段独立文本**：

   ```python
   text = " <|endoftext|> ".join((text1, text2))
   ```

#### 4.4.3 源码精读

**触发问题**的那段代码（[`ch02.ipynb §2.4 — 用 V1 编码含生词的句子，抛 KeyError`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch02/01_main-chapter-code/ch02.ipynb)）：

```python
tokenizer = SimpleTokenizerV1(vocab)
text = "Hello, do you like tea. Is this-- a test?"
tokenizer.encode(text)   # → KeyError: 'Hello'
```

**扩充词表**（[`ch02.ipynb §2.4 — 加入 <|endoftext|> 与 <|unk|>`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch02/01_main-chapter-code/ch02.ipynb)）：

```python
all_tokens = sorted(list(set(preprocessed)))
all_tokens.extend(["<|endoftext|>", "<|unk|>"])
vocab = {token:integer for integer,token in enumerate(all_tokens)}
len(vocab.items())   # 1132
```

`SimpleTokenizerV2`（[`ch02.ipynb §2.4 — SimpleTokenizerV2 类`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch02/01_main-chapter-code/ch02.ipynb)），相比 V1 只多了「生词→`<|unk|>`」一步：

```python
class SimpleTokenizerV2:
    def __init__(self, vocab):
        self.str_to_int = vocab
        self.int_to_str = {i:s for s,i in vocab.items()}

    def encode(self, text):
        preprocessed = re.split(r'([,.:;?_!"()\']|--|\s)', text)
        preprocessed = [item.strip() for item in preprocessed if item.strip()]
        preprocessed = [
            item if item in self.str_to_int
            else "<|unk|>" for item in preprocessed    # 生词兜底
        ]
        ids = [self.str_to_int[s] for s in preprocessed]
        return ids

    def decode(self, ids):
        text = " ".join([self.int_to_str[i] for i in ids])
        text = re.sub(r'\s+([,.:;?!"()\'])', r'\1', text)   # 注意比 V1 多了 : ;
        return text
```

注意 V2 的 `decode` 正则比 V1 多了 `:` 和 `;`（`[,.:;?!"()\']`），更完整。notebook 还演示了用 ` <|endoftext|> ` 拼接两段话，编码出的 ID 序列里会插入 `1130`（即 `<|endoftext|>`），把两段独立文本清晰隔开。

#### 4.4.4 代码实践

**目标**：亲手复现 `KeyError`，再用 V2 解决它。

1. 用 `SimpleTokenizerV1` 编码含 `Hello` 的句子，确认报错：

   ```python
   SimpleTokenizerV1(vocab).encode("Hello, do you like tea. Is this-- a test?")
   # KeyError: 'Hello'
   ```

2. 改用扩充词表后的 `SimpleTokenizerV2`，再次编码：

   ```python
   t2 = SimpleTokenizerV2(vocab)
   ids = t2.encode("Hello, do you like tea? <|endoftext|> In the sunlit terraces of the palace.")
   print(ids)
   print(t2.decode(ids))
   ```

**预期结果**：第 1 步抛 `KeyError: 'Hello'`；第 2 步成功得到 ID 序列，且解码输出里 `Hello` 和 `palace`（若不在词表）会显示为 `<|unk|>`，而 `<|endoftext|>` 作为分隔符正常出现。**待本地验证**：具体哪些词变成 `<|unk|>` 取决于《The Verdict》实际词表，请在你本地的输出里核对。

#### 4.4.5 小练习与答案

**练习 1**：为什么 GPT-2 不需要 `<|unk|>` 这种「未知词」token？
**答案**：GPT-2 用 BPE 子词分词，任何生词都能被拆成更小的子词单元甚至单个字符，因此总能编码、不会遇到「词表里完全没有」的情况，也就不需要 `<|unk|>`。

**练习 2**：`<|endoftext|>` 在训练数据里同时承担哪两个职责？
**答案**：（1）标记一段文本的结束；（2）作为 padding 把短句补齐到等长（训练时这些位置会被注意力掩码忽略）。

---

## 5. 综合实践

把本讲 4 个模块串起来，完成一个端到端的小流水线：**对 `the-verdict.txt` 做词级分词 → 构建词表 → 把一段文本编码成 token ID → 再解码回来**。

**步骤**：

1. 准备工作目录与语料（进入 `ch02/01_main-chapter-code/`，确保 `the-verdict.txt` 在当前目录）。

2. 在一个新脚本或 notebook 单元格里，把本讲学到的零件组装起来（**示例代码**，整合自 `ch02.ipynb` 的 2.2～2.4 节）：

   ```python
   import re

   # ① 读取语料
   with open("the-verdict.txt", "r", encoding="utf-8") as f:
       raw_text = f.read()

   # ② 正则分词（含标点与 --），并过滤空串
   preprocessed = re.split(r'([,.:;?_!"()\']|--|\s)', raw_text)
   preprocessed = [item.strip() for item in preprocessed if item.strip()]
   print("token 总数:", len(preprocessed))          # 期望 4690

   # ③ 构建词表（追加 <|endoftext|> 与 <|unk|>）
   all_tokens = sorted(list(set(preprocessed)))
   all_tokens.extend(["<|endoftext|>", "<|unk|>"])
   vocab = {t: i for i, t in enumerate(all_tokens)}
   print("词表大小:", len(vocab))                    # 期望 1132

   # ④ 用 SimpleTokenizerV2 思路做 encode/decode
   str_to_int = vocab
   int_to_str = {i: s for s, i in vocab.items()}

   def encode(text):
       tokens = [t.strip() for t in re.split(r'([,.:;?_!"()\']|--|\s)', text) if t.strip()]
       tokens = [t if t in str_to_int else "<|unk|>" for t in tokens]
       return [str_to_int[t] for t in tokens]

   def decode(ids):
       text = " ".join(int_to_str[i] for i in ids)
       return re.sub(r'\s+([,.:;?!"()\'])', r'\1', text)

   # ⑤ 取原文开头一句话，编码再解码
   sample = raw_text[:99]            # 例如 "I HAD always thought Jack Gisburn ..."
   ids = encode(sample)
   print("token IDs:", ids[:10], "...")   # 只看前 10 个
   print("解码还原:", decode(ids))
   ```

3. **需要观察的现象**：
   - token 总数应为 **4690**，词表大小应为 **1132**。
   - `encode(sample)` 得到一串整数，`decode(ids)` 还原出与原文接近的句子（标点紧贴前词）。
   - 若你把 `sample` 换成一句含《The Verdict》里没有的词（如 `Hello`），解码后会看到 `<|unk|>`。

4. **预期结果**：数字与上述一致，编码/解码往返可读。如果 token 总数或词表大小对不上，多半是正则写错或忘了过滤空串——回去对照 §4.2.3 的源码。

5. **思考延伸**：你会发现词级分词对 `It's` 这类带撇号的词处理得很粗糙（被切成三段）。记下这个不满——它正是下一讲引入 BPE 的动机。

---

## 6. 本讲小结

- **文本必须先变数字**：模型只会算张量，所以流水线第一步是 `文本 → token → token ID → 嵌入向量`；本讲负责前三步。
- **分词用正则**：`re.split(r'([,.:;?_!"()\']|--|\s)', text)` 既切空格又把标点和 `--` 切成独立 token，再用 `if item.strip()` 过滤空串。
- **词表 = 去重排序 + 编号**：`sorted(set(preprocessed))` 保证词表稳定可复现，《The Verdict》得到 1130 个不同 token。
- **SimpleTokenizerV1** 用两张反向字典 `str_to_int` / `int_to_str` 实现 encode/decode，解码时用正则修复标点前的多余空格。
- **生词会 `KeyError`**：于是引入 `<|unk|>`（兜底未知词）和 `<|endoftext|>`（标记文本结束 / 拼接独立文本 / padding）两个特殊 token，词表扩到 1132，升级为 `SimpleTokenizerV2`。
- **这只是教学玩具**：真实的 GPT-2 用 BPE 子词分词，既不崩溃也无需 `<|unk|>`——这是下一讲的主题。

---

## 7. 下一步学习建议

- **下一讲 `u2-l2`（BPE 与 tiktoken）**：学习字节对编码如何把生词拆成子词、彻底消灭 `<|unk|>`，并用 `tiktoken.get_encoding("gpt2")` 加载 GPT-2 真正使用的分词器。可对照阅读 [`ch02/05_bpe-from-scratch/bpe-from-scratch-simple.ipynb`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch02/05_bpe-from-scratch/bpe-from-scratch-simple.ipynb)。
- **之后 `u2-l3`（滑动窗口与 DataLoader）**：把一长串 token ID 切成「input / 右移一位的 target」训练样本，实现 `GPTDatasetV1` 与 `create_dataloader_v1`。
- **再之后 `u2-l4`（嵌入层）**：把 token ID 通过 `nn.Embedding` 查表变成连续向量，并加上位置嵌入——至此第 2 章的数据流水线就完整了，可以正式进入第 3 章的注意力机制。
- **想加深的读者**：`ch02/01_main-chapter-code/exercise-solutions.ipynb` 有本章习题的官方答案，建议在完成本讲综合实践后挑战。
