# 字节对编码（BPE）与 tiktoken

## 1. 本讲目标

在上一讲 `u2-l1` 里，我们手写了一个最简陋的**词级**分词器 `SimpleTokenizerV2`：用正则把文本切成「单词 + 标点」，再用一张固定的词表把它们映射成整数 ID。它能跑，但有一个绕不开的硬伤——**遇到词表里没有的词就会罢工**。我们在 `u2-l1` 里不得不引入 `<|unk|>` 这个「未知词」占位符来兜底，可一旦很多词都变成 `<|unk|>`，模型实际能读到的信息就被严重稀释了。

真实世界的 LLM（GPT-2 到 GPT-4、Llama 3 等）用的是另一种分词方案——**字节对编码（Byte Pair Encoding, BPE）**。它的妙处在于：**几乎不会出现真正的「未知词」**，任何生僻词、甚至拼写错误的词，都能被拆成模型见过的更小单元（子词或字符），从而被稳定地编码。

本讲学完后，你应当能够：

1. 说清楚 **BPE 的核心思想**：从「逐字符」出发，反复把出现频率最高的相邻「字节对」合并成新 token，从而把词表从「全靠死记」升级为「能拆能拼」。
2. 读懂仓库里**从零实现的** `BPETokenizerSimple`：理解 `train`（学合并规则）、`encode`（套用合并规则）、`decode`（还原文本）三件事各自在做什么。
3. 会用 OpenAI 的工业级分词库 **tiktoken** 加载 GPT-2 的预训练 BPE 编码器，完成编码/解码，并理解它为什么**不需要 `<|unk|>`**。
4. 通过动手对比，直观感受「词级分词 vs. BPE」「自写实现 vs. tiktoken」在 **token 数量**上的差异。

> 本讲对应原书第 2 章 **2.5 节**，并配套阅读附加材料 `ch02/05_bpe-from-scratch/`。本讲只讲「分词」这一步；把 token ID 进一步变成向量的「嵌入层」是下一讲 `u2-l3`、`u2-l4` 的内容。

---

## 2. 前置知识

本讲是 `u2-l1` 的直接后续，下面这些 `u2-l1` 已建立的概念默认你已经掌握：

- **token / token ID / 词表（vocabulary）**：文本被切成的最小单元叫 token；给每个不同 token 编一个整数号得到 token ID；所有 token↔ID 的映射合称词表。
- **`SimpleTokenizerV1/V2` 的局限**：词表是「封闭」的——训练时没见过的词，编码时只能报 `KeyError` 或退化为 `<|unk|>`。
- **特殊 token `<|endoftext|>`**：GPT-2 用它表示一段文本的结束，也用于拼接多篇独立文本、以及做 padding。

此外，本讲用到两个新概念，先有个直觉即可：

- **字节（byte）与字符（character）**：一个字节是 8 个比特（bit），能表示 \(2^8 = 256\) 种不同的值（0~255）。计算机底层用字节存储一切，文本也不例外——一段英文文本可以先转成「字节数组」，再变成一串 0~255 的整数。BPE 名字里的 "Byte" 正是源于此。
- **子词（subword）**：介于「整词」和「单字符」之间的单位。比如 `unfamiliar` 可能被拆成 `["unfam", "iliar"]` 这样的子词。子词是 BPE 的主战场。

> 术语速查：BPE（Byte Pair Encoding，字节对编码）、子词（subword）、合并（merge）、`tiktoken`（OpenAI 开源的 BPE 分词库，核心用 Rust 实现）、`Ġ`（GPT-2 用来表示「一个空格」的特殊记号，4.2 节会讲）。

---

## 3. 本讲源码地图

本讲涉及第 2 章的两个文件，**都是 Jupyter notebook**：

| 文件 | 作用 |
| --- | --- |
| [`ch02/01_main-chapter-code/ch02.ipynb`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch02/01_main-chapter-code/ch02.ipynb) | 正文 notebook。本讲只引用其中的 **§2.5 BytePair encoding** 一节：用 `tiktoken` 加载 GPT-2 的 BPE 编码器，演示 encode/decode。 |
| [`ch02/05_bpe-from-scratch/bpe-from-scratch-simple.ipynb`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch02/05_bpe-from-scratch/bpe-from-scratch-simple.ipynb) | **附加（bonus）材料**。从零实现一个教学版 BPE 分词器 `BPETokenizerSimple`，并解释 BPE 算法原理（字节、合并规则、训练流程）。这是本讲「原理」部分的主要依据。 |
| [`ch02/05_bpe-from-scratch/README.md`](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch02/05_bpe-from-scratch/README.md) | 说明 `bpe-from-scratch-simple.ipynb` 偏「可读性」，而同目录的 `bpe-from-scratch.ipynb` 是更接近 tiktoken 行为的「严肃版」。 |

> **关于永久链接的约定**（与 `u2-l1` 一致）：这两个文件都是 notebook（本质是 JSON），GitHub 按**单元格（cell）和小节标题**渲染，不像 `.py` 文件那样有可直接跳转的文件行号。因此本讲引用 notebook 时，链接文字会写明**小节号（如 §2.5）和该处代码做了什么**，而不编造对读者无意义的 JSON 行号。所有链接都指向当前 HEAD `ff0b3d9` 的固定版本。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：先讲 **BPE 原理**（为什么要有子词、合并规则是怎么来的），再**从零实现**一个最简 BPE 看清内部机制，最后用 **tiktoken** 走工业级用法。

### 4.1 BPE 原理：从「逐字符」到「子词合并」

#### 4.1.1 概念说明

`u2-l1` 的词级分词器有两个极端都不理想：

- **粒度太粗（整词）**：词表必须穷尽所有可能出现的词。但语言是开放的，新词、错别字、专有名词层出不穷，词表永远不够用，于是大量词退化成 `<|unk|>`。
- **粒度太细（逐字符）**：把每个字符当成一个 token 一定不会「未知」，但一段短文本会产生海量 token，序列拉得很长，模型学起来又慢又难。

BPE 走的是**中间路线**：先用最细的粒度（字符/字节）打底，保证「永远不会未知」；然后**学习**哪些字符组合经常一起出现，把它们逐步合并成更大的子词，从而压缩序列长度。常见词（如 `the`、`tion`）会被合并成单个 token，罕见词则保留为多个子词 token 的拼接。

一句话概括 BPE 的训练过程：**反复合并当前文本中出现频率最高的那对相邻 token**。

#### 4.1.2 核心流程

先看「为什么从字节出发」。任何文本都能先转成字节数组，每个字节是一个 0~255 的整数：

\[
\text{文本} \;\xrightarrow{\text{UTF-8}}\; [\,b_1, b_2, \dots, b_n\,], \quad b_i \in \{0,1,\dots,255\}
\]

因为只有 256 种字节值，**用这 256 个值作为初始词表，就能编码任意文本、永远不会未知**——这正是 BPE 名字里 "Byte" 的由来，也是它不需要 `<|unk|>` 的根本原因。

BPE 的训练（学习合并规则）是一个反复迭代的过程，每轮做三件事：

1. **找最高频相邻对**：扫描当前的 token 序列，统计所有相邻 token 对 `(t_i, t_{i+1})` 的出现次数，挑出频次最高的那一对。
2. **替换并登记**：把这对 token 用一个**新的、尚未使用的 ID** 替换（初始词表用掉 0~255，所以第一个新 ID 就是 256），并把「这对 → 新 ID」记进一张**合并表（merges）**。词表大小是超参（GPT-2 是 50,257）。
3. **重复**：回到第 1 步，直到达到目标词表大小，或没有可再合并的对（某对出现次数 ≤ 1）。

**解码**则是反过来：按引入合并规则的相反顺序，把每个 ID 逐步展开回它代表的字节/字符序列，再拼回文本。

#### 4.1.3 源码精读

「从字节出发」的动机，在 `bpe-from-scratch-simple.ipynb` §1.1 里有最直观的演示（[`bpe-from-scratch-simple.ipynb` §1.1 — 文本转字节数组](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch02/05_bpe-from-scratch/bpe-from-scratch-simple.ipynb)）：

```python
text = "This is some text"
byte_ary = bytearray(text, "utf-8")
ids = list(byte_ary)
print(ids)
# [84, 104, 105, 115, 32, 105, 115, 32, 115, 111, 109, 101, 32, 116, 101, 120, 116]
```

这段代码把 17 个字符的文本变成 17 个 0~255 的整数。**这是一种合法的「文本→token ID」方式**，可以直接喂给嵌入层——缺点就是太啰嗦：17 个字符要 17 个 token。而 GPT-2 的 BPE 分词器能把同一句话压成只有 **4** 个 token（[`bpe-from-scratch-simple.ipynb` §1.1 — tiktoken 把同一句压成 4 个 token](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch02/05_bpe-from-scratch/bpe-from-scratch-simple.ipynb)）：

```python
import tiktoken
gpt2_tokenizer = tiktoken.get_encoding("gpt2")
gpt2_tokenizer.encode("This is some text")
# [1212, 318, 617, 2420]
```

`1212` 就对应整词 `This`，`318` 是 ` is`（注意带前导空格），`617` 是 ` some`，`2420` 是 ` text`——这些「常见整词/子词」都是 BPE 在海量语料上**学**出来的合并结果。压缩比从 17→4，正是 BPE 的价值。

BPE 算法的三步循环，notebook 用一个极小的例子讲得很清楚（[`bpe-from-scratch-simple.ipynb` §1.4 — 在 "the cat in the hat" 上演示合并](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch02/05_bpe-from-scratch/bpe-from-scratch-simple.ipynb)）。训练文本是 `the cat in the hat`：

- **第 1 轮**：相邻对 `("t","h")` 出现 2 次（最多）→ 用新 ID `256` 替换 → 文本变成 `<256>e cat in <256>e hat`，合并表记 `{(t,h): 256}`。
- **第 2 轮**：现在 `<256>e`（即 `the`）出现 2 次 → 用 `257` 替换 → `<257> cat in <257> hat`，合并表追加 `{(<256>,e): 257}`。
- **第 3 轮**：`<257> `（`the` 加一个空格）出现 2 次 → 用 `258` 替换 → `<258>cat in <258>hat`……

可以看到，合并表是一张**层层嵌套**的映射：`258` 展开成 `<257> `，`257` 再展开成 `<256>e`，`256` 再展开成 `th`。解码时只要按相反顺序层层展开，就能无损还原原文 `the cat in the hat`。

> 小贴士：词表大小是超参数。常见量级——GPT-2 为 **50,257**，GPT-4（tiktoken 里叫 `cl100k_base`）为 **100,256**，GPT-4o（`o200k_base`）为 **199,997**。词表越大，能记住的整词/子词越多，序列越短，但嵌入矩阵也越大。

#### 4.1.4 代码实践

**目标**：亲手感受「逐字符」与「BPE」在 token 数量上的巨大差距。

1. 确保已 `pip install tiktoken`（见 `u1-l2` 的依赖安装）。
2. 在 Python 中运行：

   ```python
   import tiktoken
   text = "This is some text"

   # 逐字符（字节）基线
   char_ids = list(bytearray(text, "utf-8"))
   print("字符/字节 token 数:", len(char_ids))

   # GPT-2 的 BPE
   enc = tiktoken.get_encoding("gpt2")
   bpe_ids = enc.encode(text)
   print("BPE token 数:", len(bpe_ids), bpe_ids)
   ```

3. 观察两个数字的对比。

**预期结果**：字符/字节 token 数为 `17`，BPE token 数为 `4`（即 `[1212, 318, 617, 2420]`）。压缩比约 4 倍多。

#### 4.1.5 小练习与答案

**练习 1**：为什么 BPE「永远不会出现未知词」，而 `u2-l1` 的 `SimpleTokenizerV1` 会？
**答案**：BPE 的初始词表是全部 256 个字节值，任何文本都能先拆成字节、保证可编码；遇到没见过的整词，就退回到它由哪些子词/字节组成来表示。而 `SimpleTokenizerV1` 的词表是封闭的字符/词集合，遇到没登记的词无法拆解，只能报错或变 `<|unk|>`。

**练习 2**：BPE 训练时「合并最高频对」的终止条件是什么？
**答案**：达到预设的词表大小（如 GPT-2 的 50,257），或者没有任何相邻对的出现次数大于 1（再合并已无收益）时停止。

---

### 4.2 子词合并：从零实现 `BPETokenizerSimple`

#### 4.2.1 概念说明

光讲原理不够直观，`bpe-from-scratch-simple.ipynb` 提供了一个教学版实现 `BPETokenizerSimple`，把上一节的算法落成可运行代码。它的接口刻意模仿 `tiktoken`：有 `train`（学合并规则）、`encode`（文本→ID）、`decode`（ID→文本）三个方法。读懂它，你就能回答两个关键问题：

- **训练阶段**：合并表 `bpe_merges` 到底是怎么一步步攒出来的？
- **编码阶段**：拿到一段**新**文本，怎么套用学好的合并规则把它压成尽量少的 token？

> 注意作者反复强调：这是**为可读性写的 naive 实现**，性能远不如 tiktoken；同目录的 `bpe-from-scratch.ipynb` 才是行为接近 tiktoken 的「严肃版」。生产中请直接用 tiktoken（4.3 节）。

#### 4.2.2 核心流程

`BPETokenizerSimple` 的三个方法职责分明：

1. **`train(text, vocab_size)`**：先建立「字符→ID」的初始词表（前 256 个），把文本转成 ID 序列；然后循环 `vocab_size - 256` 次，每次用 `find_freq_pair` 找最高频对、用 `replace_pair` 把它替换成新 ID，并记入 `bpe_merges`。
2. **`encode(text)`**：先按空格把文本切成词，对每个词：如果它整体已在词表里就直接取 ID；否则调 `tokenize_with_bpe` 把它拆成字符、再**按学习顺序**反复套用 `bpe_merges` 合并相邻对，直到合不动为止。
3. **`decode(token_ids)`**：逐个 ID 查词表拼回字符串，并把表示空格的 `Ġ` 还原成真正的空格。

一个需要特别说明的细节是 **`Ġ` 记号**：GPT-2 的 BPE 不把空格单独当一个 token，而是把「空格 + 后面的词」粘在一起，用字符 `Ġ`（U+0120）代表那个空格。所以 `"Hello world"` 会被表示成 `["Hello", "Ġworld"]`。`encode` 里给「跟在空格后面的词」前面补一个 `Ġ`，`decode` 里再把 `Ġ` 换回空格，就是这个机制。这是 GPT-2 分词器的一个「怪癖」（GPT-4 的分词器已改进，直接用 `" world"`）。

#### 4.2.3 源码精读

合并表是怎么攒出来的，关键在 `train` 里的循环（[`bpe-from-scratch-simple.ipynb` §2 — train 中反复找最高频对并替换](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch02/05_bpe-from-scratch/bpe-from-scratch-simple.ipynb)）：

```python
# BPE steps 1-3: Repeatedly find and replace frequent pairs
for new_id in range(len(self.vocab), vocab_size):
    if len(token_ids) < 2:
        break
    pair_id = self.find_freq_pair(token_ids, mode="most")
    if pair_id is None:            # 没有可合并的对了，停止训练
        break
    updated = self.replace_pair(token_ids, pair_id, new_id)
    if updated == token_ids:       # 没发生任何替换，停止
        break
    token_ids = updated
    self.bpe_merges[pair_id] = new_id   # 登记合并规则
```

「找最高频对」靠 `Counter` 统计相邻对（[`bpe-from-scratch-simple.ipynb` §2 — find_freq_pair 用 Counter 统计相邻对](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch02/05_bpe-from-scratch/bpe-from-scratch-simple.ipynb)）：

```python
@staticmethod
def find_freq_pair(token_ids, mode="most"):
    if len(token_ids) < 2:
        return None
    pairs = Counter(zip(token_ids, token_ids[1:]))   # 统计所有相邻对
    if not pairs:
        return None
    if mode == "most":
        return max(pairs.items(), key=lambda x: x[1])[0]   # 频次最高的那对
    ...
```

「替换」则是用一个 `deque` 线性扫描序列，遇到目标对就合并成一个新 ID（[`bpe-from-scratch-simple.ipynb` §2 — replace_pair 用 deque 扫描并替换](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch02/05_bpe-from-scratch/bpe-from-scratch-simple.ipynb)）：

```python
@staticmethod
def replace_pair(token_ids, pair_id, new_id):
    dq = deque(token_ids)
    replaced = []
    while dq:
        current = dq.popleft()
        if dq and (current, dq[0]) == pair_id:
            replaced.append(new_id)   # 合并成新 ID
            dq.popleft()              # 丢掉对的第二个元素
        else:
            replaced.append(current)
    return replaced
```

训练好之后，notebook 在《The Verdict》全文上以 `vocab_size=1000` 训练，得到的词表正好 1000 项、合并了 **742** 次（≈ `1000 - 256`）（[`bpe-from-scratch-simple.ipynb` §3.1 — 训练并查看词表/合并数](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch02/05_bpe-from-scratch/bpe-from-scratch-simple.ipynb)）：

```python
tokenizer = BPETokenizerSimple()
tokenizer.train(text, vocab_size=1000, allowed_special={"<|endoftext|>"})
print(len(tokenizer.vocab))      # 1000
print(len(tokenizer.bpe_merges)) # 742
```

用它编码一句话，42 个字符被压成 20 个 token，而且能完美解码回来（[`bpe-from-scratch-simple.ipynb` §3.1 — encode/decode 往返一致](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch02/05_bpe-from-scratch/bpe-from-scratch-simple.ipynb)）：

```python
input_text = "Jack embraced beauty through art and life."
token_ids = tokenizer.encode(input_text)
print(len(input_text), len(token_ids))   # 42 20
print(tokenizer.decode(token_ids))       # Jack embraced beauty through art and life.
```

逐个 token 解码能看到子词结构，比如 `em|br|ac|ed`、`be|a|ut|y`、` through`（带前导空格的整词）等（[`bpe-from-scratch-simple.ipynb` §3.1 — 逐 token 解码看子词结构](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch02/05_bpe-from-scratch/bpe-from-scratch-simple.ipynb)）：

```python
for token_id in token_ids:
    print(f"{token_id} -> {tokenizer.decode([token_id])}")
# 424 -> Jack
# 654 -> em
# 531 -> br
# 302 -> ac
# 311 -> ed
# 595 ->  through
# ...
```

> ⚠️ 一个重要的真实行为（实践时会遇到）：`BPETokenizerSimple` 的初始词表是 `chr(i) for i in range(256)`（Unicode 码点 0~255）**加上训练文本里出现的字符**，它处理的是**字符**而非真正的 UTF-8 **字节**。所以一段纯英文/拉丁字母的「生僻词」能被拆成子词（因为这些字母都在词表里）；但若出现词表里没有的字符（比如汉字 `中`，码点远超 255 且训练文本里没有），`tokenize_with_bpe` 会抛 `ValueError: Characters not found in vocab`。**严肃版** `bpe-from-scratch.ipynb` 改为在真正的 UTF-8 字节上工作，从而能处理任意字符——这也是 BPE 名字里 "Byte" 的真正含义。

#### 4.2.4 代码实践

**目标**：亲手训练一个 BPE，并观察它对「没在训练集里整体出现过的词」是如何拆成子词的。

1. 打开 `ch02/05_bpe-from-scratch/bpe-from-scratch-simple.ipynb`，依次运行到 `tokenizer.train(text, vocab_size=1000, ...)`。
2. 用一句**训练集里没有**的话编码，比如：

   ```python
   s = "Jack embraced beauty through art and life."
   ids = tokenizer.encode(s)
   print(len(s), len(ids))                 # 字符数 vs token 数
   for tid in ids:
       print(tid, "->", tokenizer.decode([tid]))
   ```

3. 观察输出：注意像 `embraced` 被拆成 `em|br|ac|ed`，而 ` through` 这种高频片段被合并成单个 token。

**预期结果**：42 个字符 ≈ 20 个 token；解码能还原原文；常见子词片段（如 ` through`、` and`）合并成了单 token，生疏的词被拆成 2 字符子词。具体 ID 数值会因实现细节而异——以本地实际输出为准。

#### 4.2.5 小练习与答案

**练习 1**：`bpe_merges` 这个字典的键和值分别是什么？为什么它能同时服务于「训练」和「编码」？
**答案**：键是一对相邻 token ID `(id1, id2)`，值是合并后的新 ID。训练时它记录「我是怎么一步步合并出大词表的」；编码新文本时，`tokenize_with_bpe` 按同样的规则反复合并相邻对，从而用一致的粒度切分任意文本。

**练习 2**：为什么 `encode` 要给「跟在空格后的词」前面加 `Ġ`，而 `decode` 又要把 `Ġ` 换回空格？
**答案**：这是为了模仿 GPT-2 的分词约定——把「空格 + 词」当作一个整体 token（如 `Ġworld`），而不是把空格单独切出来。`encode` 加 `Ġ`、`decode` 去 `Ġ`，两端对称，才能保证 `decode(encode(x)) == x`。

---

### 4.3 tiktoken：用工业级 GPT-2 BPE 处理任意文本

#### 4.3.1 概念说明

教学版实现帮我们理解原理，但真正用来给 LLM 喂数据的是工业级库。本书（以及 Llama 3 等大多数项目）统一用 OpenAI 开源的 **tiktoken**：它内部用 Rust 实现，速度比纯 Python 快很多（`ch02.ipynb` §2.5 提到在样本文本上约快 5 倍），并且**自带 GPT-2/GPT-4 预训练好的词表与合并规则**，我们直接加载即可，不需要自己训练。

`u2-l1` 的 `SimpleTokenizerV2` 需要 `<|unk|>` 兜底，而 tiktoken 加载的 GPT-2 BPE **不需要任何 `<|unk|>`**——理由正是 4.1 节讲的：任何文本都能拆成字节/子词，根本不存在「未知词」。

#### 4.3.2 核心流程

用 tiktoken 走一遍编码/解码只要三步：

1. **加载编码器**：`tiktoken.get_encoding("gpt2")` 返回一个预训练好的 GPT-2 分词器对象（词表 50,257）。
2. **编码**：`enc.encode(text)` 得到 token ID 列表。如果文本里含有 `<|endoftext|>` 这类**特殊 token**，必须通过 `allowed_special={"<|endoftext|>"}` 显式放行，否则 tiktoken 会**报错**——这是一个安全机制，防止文本里意外出现的特殊记号被当成控制符。
3. **解码**：`enc.decode(ids)` 把 ID 列表无损还原成文本。

#### 4.3.3 源码精读

正文 notebook §2.5 的核心演示（[`ch02.ipynb` §2.5 — 加载 GPT-2 BPE 并编码含生僻词的文本](https://github.com/rasbt/LLMs-from-scratch/blob/ff0b3d92e3383f52aa732366024c601c45d869a6/ch02/01_main-chapter-code/ch02.ipynb)）：

```python
import tiktoken
tokenizer = tiktoken.get_encoding("gpt2")

text = (
    "Hello, do you like tea? <|endoftext|> In the sunlit terraces"
    "of someunknownPlace."
)

integers = tokenizer.encode(text, allowed_special={"<|endoftext|>"})
print(integers)
# [15496, 11, 466, 345, 588, 8887, 30, 220, 50256, 554, 262, 4252,
#  18250, 8812, 2114, 1659, 617, 34680, 27271, 13]

strings = tokenizer.decode(integers)
print(strings)
# Hello, do you like tea? <|endoftext|> In the sunlit terracesof someunknownPlace.
```

这段代码同时展示了 BPE 的两个关键能力：

- **没有 `<|unk|>`，照样处理生造词**：`someunknownPlace` 是个 GPT-2 词表里不可能有的「假词」，但它没有报错、也没退化成未知符，而是被拆成了若干子词 ID（`34680`、`27271` 等），解码后能完整还原。这正是 `u2-l1` 的词级分词器做不到的。
- **特殊 token 显式放行**：`<|endoftext|>` 被编码成 ID `50256`（GPT-2 词表的最后一个、也是 `<|endoftext|>` 的固定编号）。如果删掉 `allowed_special={...}` 参数，`encode` 会抛错——因为 tiktoken 默认禁止把特殊 token 当普通文本编码。

> 小贴士：`u2-l1` 里我们看到 GPT-2 「只用 `<|endoftext|>`、不用 `<|unk|>`」；现在能补全原因了——BPE 能拆字，根本不需要未知符。这也是为什么本仓库从第 2 章往后，分词一律用 tiktoken，再没出现过 `<|unk|>`。

#### 4.3.4 代码实践

**目标**：亲眼看到 tiktoken 对「生造词」的子词拆分，并验证 `allowed_special` 的安全行为。

1. 运行上面的代码，确认能编码/解码。
2. 把 `allowed_special={"<|endoftext|>"}` 改成 `allowed_special=set()`（即不放行任何特殊 token），再次 `encode`，观察是否报错。
3. 单独编码一个生造词，逐 token 解码看它被拆成了什么：

   ```python
   enc = tiktoken.get_encoding("gpt2")
   for tid in enc.encode("someunknownPlace"):
       print(tid, "->", repr(enc.decode([tid])))
   ```

**预期结果**：第 2 步会抛出类似 `... disallowed special token ...` 的错误（待本地验证具体报错文案）；第 3 步会看到 `someunknownPlace` 被拆成多个子词，如 `some`、`unknown`、`Place` 等片段（具体切分以本地输出为准），但 `decode` 能把它拼回原词。

#### 4.3.5 小练习与答案

**练习 1**：`tokenizer.encode(text, allowed_special={"<|endoftext|>"})` 里的 `allowed_special` 参数解决了什么问题？去掉它会怎样？
**答案**：它显式声明「文本里的 `<|endoftext|>` 是合法的，请把它编码成对应的特殊 token ID（50256）」。去掉后，tiktoken 默认会拒绝编码含特殊 token 的文本并报错，这是一种防止特殊控制符被意外注入的安全机制。

**练习 2**：相比 `u2-l1` 的 `SimpleTokenizerV2`，用 tiktoken 的 GPT-2 BPE 有哪两个最直接的好处？
**答案**：（1）不需要 `<|unk|>`——任何生词都能拆成子词/字节被稳定编码；（2）词表与合并规则是 GPT-2 在海量语料上预训练好的，常见词被压成单 token，序列更短、更贴近真实 LLM 的输入。

---

## 5. 综合实践

把本讲三个模块串起来，完成**规格里要求的核心实践**：对比「自写的简单 BPE」与「tiktoken 的 GPT-2 BPE」对同一段含生僻词文本的编码结果，统计 token 数量差异。

**实践目标**：直观体会「词表大小 + 训练语料」如何影响分词的压缩效果，并验证两种分词器都能无损往返。

**操作步骤**：

1. 按 4.2 节，用《The Verdict》全文训练一个 `BPETokenizerSimple`（`vocab_size=1000`）。准备一段测试文本，里面放一两个生僻/生造词，例如：

   ```python
   test_text = "The aurora shimmered over someunknownPlace at dawn."
   ```

2. 用自写 BPE 编码并计数：

   ```python
   mine_ids = tokenizer.encode(test_text)
   print("自写BPE token 数:", len(mine_ids))
   print("还原:", tokenizer.decode(mine_ids))
   ```

3. 用 tiktoken 的 GPT-2 编码同一段文本并计数：

   ```python
   import tiktoken
   enc = tiktoken.get_encoding("gpt2")
   tt_ids = enc.encode(test_text)
   print("tiktoken token 数:", len(tt_ids))
   print("还原:", enc.decode(tt_ids))
   ```

4. 对比两者的 token 数，并逐 token 解码 `someunknownPlace` 这一个词，看各自把它拆成了几段。

**需要观察的现象**：

- 两种分词器都能 `decode(encode(x)) == x`，无损还原原文（验证 BPE 的可逆性）。
- **token 数量通常不同**：tiktoken（词表 50,257、在海量语料上训练）一般比只在 2 万字符短文上训练、词表仅 1,000 的自写 BPE 产生**更少**的 token，因为它记住了更多整词。
- `someunknownPlace` 在两者里都会被拆成多个子词（不是整词），但拆法、段数不同。

**预期结果**：自写 BPE 的 token 数 > tiktoken 的 token 数（具体数值待本地验证）。这个差距正是「更大词表 + 更大训练语料」带来的压缩收益，也解释了为什么真实 LLM 都用预训练好的工业级分词器，而不是自己临时训练一个小词表。

> 进阶（可选）：把自写 BPE 的 `vocab_size` 调大到 5000、或在更大语料上训练，观察它与 tiktoken 的 token 数差距是否缩小——体会「词表大小与训练数据量」对压缩率的边际影响。

---

## 6. 本讲小结

- **BPE 的本质**是「先按字节/字符打底保证永不未知，再反复合并最高频相邻对来压缩序列」，从而在「整词太粗、逐字符太细」之间找到子词这个平衡点。
- 合并规则记录在 **`bpe_merges`** 表里，训练时攒出来，编码新文本时套用——这正是 `BPETokenizerSimple` 的 `train` / `encode` / `decode` 三件事。
- BPE 的初始词表覆盖全部 **256 个字节值**，所以**不需要 `<|unk|>`**，任何生僻词都能拆成子词被稳定编码，这是它相对 `u2-l1` 词级分词器的根本优势。
- **tiktoken** 提供预训练好的 GPT-2 BPE（词表 50,257），用 Rust 实现性能高；`encode` 时遇到 `<|endoftext|>` 等特殊 token 必须用 `allowed_special` 显式放行。
- 词表大小是超参：GPT-2 为 50,257、GPT-4 (`cl100k_base`) 为 100,256、GPT-4o (`o200k_base`) 为 199,997；词表越大压缩越好，但嵌入矩阵也越大。
- 本仓库从第 2 章起统一用 tiktoken 分词，分词得到的 token ID 序列，就是下一讲「滑动窗口采样」和「嵌入层」的输入。

---

## 7. 下一步学习建议

到这里，「文本 → token → token ID」这一步已经彻底打通。接下来：

- **`u2-l3` 滑动窗口数据采样与 DataLoader**：把一长串 token ID（如本讲用 tiktoken 把《The Verdict》编码得到的 5145 个 ID）用滑动窗口切成一批批 `input/target` 训练样本，并封装成 PyTorch 的 `DataLoader`。建议先去看 `ch02/01_main-chapter-code/ch02.ipynb` §2.6，那里紧接着本讲的 §2.5。
- **`u2-l4` Token 嵌入与位置嵌入**：把 token ID 用 `nn.Embedding` 查表变成连续向量，再叠加位置嵌入，得到真正喂给 Transformer 的输入。
- **想深挖 BPE**：阅读同目录的 `bpe-from-scratch.ipynb`（严肃版，行为接近 tiktoken，且工作在真正的 UTF-8 字节上），以及 `ch02/02_bonus_bytepair-encoder/compare-bpe-tiktoken.ipynb`（自写实现与 tiktoken 的并排对比，含速度基准）。
