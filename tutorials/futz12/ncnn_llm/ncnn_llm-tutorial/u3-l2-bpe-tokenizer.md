# BPE / BBPE 分词器实现

## 1. 本讲目标

本讲深入 ncnn_llm 的 BPE 分词器实现，把 u3-l1 里「构造函数如何得到一个 BpeTokenizer 对象」推进到「这个对象内部到底怎么把字符串切成 token id、又怎么拼回字符串」。

读完本讲你应当能够：

- 说清楚 `vocab.txt` 与 `merges.txt` 的格式，以及它们如何被 `LoadFromFiles` 翻译成内存中的两张表。
- 手推 BPE 合并算法：给定一段文本和 merges 表，能模拟出最终切分结果。
- 解释 `use_byte_encoder`（bbpe）与普通 bpe（SentencePiece 风格）在「预分词」和「处理不可见字节」上的本质区别，知道为什么不能对一个 vocab 随意切换这两种模式。
- 理解 `encode` / `decode` 的完整往返流程，包括特殊令牌的最长匹配、`fallback_to_chars` 兜底、以及 `▁` 标记在解码时的还原。

## 2. 前置知识

### 2.1 什么是 BPE

BPE（Byte Pair Encoding，字节对编码）最初是一种数据压缩算法，后来被借用到分词里。它的核心想法非常朴素：

> 从「把文本拆成最小单位」开始，反复合并出现频率最高（或优先级最高）的相邻符号对，直到无法继续合并为止。

这里有两个关键概念：

- **词表（vocab）**：模型认识的全部 token 字符串，每个 token 有一个 id。在本项目里，`vocab.txt` 一行一个 token，**行号就是 id**（第 1 行 id=0，第 2 行 id=1……）。
- **合并规则（merges）**：一张有序的「符号对 → 该合并的优先级」表。排在越前面的规则优先级越高（rank 越小）。

合并是一个贪心过程：每一步都在当前符号序列里找出**优先级最高（rank 最小）**的那个相邻对，把它合并成一个符号，然后重复，直到没有任何相邻对能再合并。

### 2.2 BPE 与 SentencePiece 的关系

很多模型（Qwen、LLaMA、mclip 等）的分词器底层都是 BPE，但「在什么粒度上做 BPE」有两派：

| 流派 | 工作粒度 | 空格怎么处理 | 本项目对应开关 |
|------|----------|--------------|----------------|
| SentencePiece-BPE（bpe） | UTF-8 字符 | 空格变成特殊标记 `▁`（U+2581），按词预分词 | `use_byte_encoder=false` |
| 字节级 BPE（bbpe，GPT-2 风格） | 单个字节 | 把每个字节映射成一个「安全的可见码点」，整段做 BPE | `use_byte_encoder=true` |

理解这两派的差异是本讲的难点，4.3 节会结合源码详细拆解。

### 2.3 为什么要在字节上做 BPE

`vocab.txt` 是按行解析的纯文本文件。如果某个 token 是一个原始的换行符或空格，它会**破坏按行读取**的逻辑——解析器会把它当成两行。bbpe 的解法是：先把每个字节（0–255）映射成一个「可见、不会断行」的 Unicode 码点，再在映射后的字符串上做 BPE。这样所有 256 个字节值都能被安全地表示，vocab 文件也不会被不可见字符破坏。

> 承接 u3-l1：`model.json` 的 `tokenizer.type` 在 `"bpe"` 与 `"bbpe"` 之间切换，二者**都用同一个 `BpeTokenizer` 类**，区别仅在于构造时是否传入 `use_byte_encoder=true`。本讲就把这个开关内部到底做了什么讲透。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/utils/tokenizer/bpe_tokenizer.h` | `BpeTokenizer` 类声明：成员变量、公开 API（`LoadFromFiles` / `encode` / `decode`）、私有算法函数 |
| `src/utils/tokenizer/bpe_tokenizer.cpp` | 全部实现：文件加载、UTF-8 工具、byte encoder、BPE 合并核心、预分词、encode/decode |
| `src/utils/tokenizer/tokenizer_types.h` | `SpecialTokensConfig` / `SpecialTokenIds` 两个纯数据结构（u3-l1 已讲） |
| `examples/bytelevelbpe_main.cpp` | 一个最小可读示例：`LoadFromFiles` → `encode` → `decode`，用 qwen3_0.6b 演示 bbpe |

> 重要提醒（承接 u1-l2）：`bytelevelbpe_main.cpp` 虽然在 `examples/` 下，但 **`xmake.lua` 没有为它定义 target**，所以不能用 `xmake run bytelevelbpe_main` 跑它。它是一个「参考用最小示例」，本讲实践部分会说明如何独立编译。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，对应一条完整链路：

> 加载（`LoadFromFiles`）→ 合并核心（`BpeForPiece`）→ 预分词/字节编码两条路径（bpe vs bbpe）→ 公开 API（`encode`/`decode`）

### 4.1 LoadFromFiles：词表、合并表与配置加载

#### 4.1.1 概念说明

`LoadFromFiles` 是一个**静态工厂函数**：给它两个文件路径 + 一份特殊令牌配置，它返回一个组装好的 `BpeTokenizer` 对象。它要做四件事：

1. 读 `vocab.txt`，建立「id ↔ token 字符串」的双向映射。
2. 读 `merges.txt`，建立「符号对 → rank」的映射。
3. 根据 `use_byte_encoder` 决定是否初始化字节映射表（4.3 节）。
4. 把特殊令牌（bos/eos/...）登记进词表（承接 u3-l1 的 `EnsureSpecialTokens`）。

#### 4.1.2 核心流程

```
LoadFromFiles(vocab_path, merges_path, spec, add_special_if_missing, fallback_to_chars, use_byte_encoder)
  ├─ LoadVocab(vocab_path)          → id_to_token_ (vector<string>)
  ├─ BuildTokenToId(id_to_token_)   → token_to_id_ (string→int 反查表)
  ├─ LoadMergesRank(merges_path)    → merges_rank_ (PairKey→rank)
  ├─ 记录 fallback_to_chars_ / use_byte_encoder_
  ├─ if use_byte_encoder_: InitByteMaps()
  └─ EnsureSpecialTokens(spec, add_special_if_missing)
```

注意 `BpeTokenizer` 的构造函数是 `private` 的（`bpe_tokenizer.h:53`），外部**只能**通过 `LoadFromFiles` 创建对象——这强制了「必须先加载词表才能用」的不变量。

#### 4.1.3 源码精读

**读 vocab.txt——逐行读，行号即 id：**

[src/utils/tokenizer/bpe_tokenizer.cpp:9-26](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/bpe_tokenizer.cpp#L9-L26) 逐行读取，跳过空行，并剥掉行尾的 `\r`（兼容 Windows CRLF）；`reserve(50000)` 是为常见模型词表规模预分配，避免反复扩容。读完后 `id_to_token_` 的下标天然就是 token id。

**建反查表：**

[src/utils/tokenizer/bpe_tokenizer.cpp:28-35](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/bpe_tokenizer.cpp#L28-L35) 把 `id_to_token_` 翻转成 `token_to_id_`（`unordered_map<string,int>`），`reserve(2×)` 降低哈希冲突。有了它，encode 时才能由 token 字符串查到 id。

**读 merges.txt——有序规则，rank 即出现顺序：**

[src/utils/tokenizer/bpe_tokenizer.cpp:37-54](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/bpe_tokenizer.cpp#L37-L54) 跳过空行和以 `#` 开头的注释行（很多 merges.txt 首行是 `#version: 0.2` 之类的头）；每行用 `iss >> a >> b` 读两个符号，用 `PairKey(a,b)` 拼成键，`rank` 从 0 自增。**rank 就是该规则在文件里的行序**——越靠前优先级越高。

`PairKey` 的实现值得一看：

[src/utils/tokenizer/bpe_tokenizer.cpp:225-232](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/bpe_tokenizer.cpp#L225-L232) 用制表符 `\t` 把两个符号拼起来当键。这里**不能**用直接字符串拼接（`a+b`），否则 `("a","bc")` 和 `("ab","c")` 会撞键；插入一个不会出现在 token 里的分隔符 `\t` 就能区分。

**工厂主函数：**

[src/utils/tokenizer/bpe_tokenizer.cpp:339-362](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/bpe_tokenizer.cpp#L339-L362) 串起上述四步。词表为空直接抛异常；`use_byte_encoder_` 为真时调用 `InitByteMaps()`（4.3 节）；最后 `std::move(tok)` 返回。注释「显式移动，避免 MSVC 尝试拷贝」是因为 `BpeTokenizer` 删除了拷贝构造（见 `bpe_tokenizer.h:47-48`），不显式 move 在某些编译器上可能误判。

**登记特殊令牌（承接 u3-l1）：**

[src/utils/tokenizer/bpe_tokenizer.cpp:364-388](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/bpe_tokenizer.cpp#L364-L388) `ensure` 是一个 lambda：对每个特殊令牌字符串，先在 `token_to_id_` 里查；查到就用其 id，查不到且 `add_if_missing` 为真就把它**追加到词表末尾**（新 id = 当前词表大小）。这就解释了 u3-l1 里「空串得 -1、拼错抛异常」的细节——配置里给了字符串但词表里没有、又不允许新增时，id 落为 -1。

#### 4.1.4 代码实践

**目标**：验证「vocab 行号即 id」与「merges rank 即行序」这两条隐含契约。

**操作**（纯源码阅读 + 文件观察，无需运行模型）：

1. 找一个已下载模型目录里的 `vocab.txt` 与 `merges.txt`（例如 `assets/qwen3_0.6b/`）。
2. 看 `vocab.txt` 第 0、1、2 行分别是什么 token，确认 `id_to_token_[0/1/2]` 就是这三行内容。
3. 看 `merges.txt` 的前几行（跳过 `#` 注释行），确认 `LoadMergesRank` 给它们的 rank 是 0、1、2……
4. 思考：如果两行 merges 顺序对调，rank 也对调，最终分词结果会怎样？（答：rank 决定合并优先级，对调会改变优先冲突时谁先合并。）

**预期结果**：能用自己的话讲清「为什么 vocab 的行序天然等于 id，merges 的行序天然等于优先级」。

### 4.2 BPE 合并核心 BpeForPiece 与缓存

#### 4.2.1 概念说明

`BpeForPiece` 是整个分词器的心脏：输入**一个预分词片段**（piece），输出它在 BPE 合并后的 token 字符串列表。例如片段 `"▁hello"` 经过合并可能变成 `["▁he", "llo"]` 两个 token。

注意它的输入是「一个 piece」，不是整段文本——整段文本会先被预分词切成多个 piece（4.3 节），每个 piece 独立做 BPE。这是 SentencePiece 系分词器的特点：**词与词之间的边界一旦由预分词确定，就不会跨词合并**。

#### 4.2.2 核心流程

```
BpeForPiece(piece):
  symbols = Utf8Chars(piece)        # 先切成 UTF-8 字符序列
  if symbols.size() <= 1: return symbols   # 单字符，无需合并
  while symbols.size() >= 2:
      在所有相邻对里找 rank 最小的 (best_i, best_rank)
      if 没有任何相邻对在 merges_rank_ 里: break
      把 symbols[best_i] 和 symbols[best_i+1] 合并成一个
      删掉 symbols[best_i+1]
  return symbols
```

每轮只合并**一个**优先级最高的相邻对，合并后序列变短一个，再进下一轮——这是教科书式的贪心 BPE。

合并的判据用数学语言写就是：设当前符号序列为 \(s_0, s_1, \dots, s_{m-1}\)，相邻对 \((s_i, s_{i+1})\) 的合并代价为

\[
\text{rank}(s_i, s_{i+1}) = \text{merges\_rank\_}[\text{PairKey}(s_i, s_{i+1})]
\]

（若该对不在表中，代价为 \(+\infty\)）。每轮选择

\[
i^* = \arg\min_i \text{rank}(s_i, s_{i+1})
\]

合并 \(s_{i^*}\) 与 \(s_{i^*+1}\)。当所有相邻对的 rank 都是 \(+\infty\)（即没有可合并的对）时停止。

#### 4.2.3 源码精读

**BPE 合并主循环：**

[src/utils/tokenizer/bpe_tokenizer.cpp:248-273](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/bpe_tokenizer.cpp#L248-L273) 先 `Utf8Chars` 把 piece 按码点切成字符（保证多字节字符不被拆成半个字节）；然后进入 `while` 循环：内层 `for` 遍历所有相邻对，用 `merges_rank_.find` 查 rank，记录最小者；`best_i < 0` 表示没有任何对能合并，`break` 退出；否则字符串拼接合并、`erase` 删掉后一个。`best_rank` 初值取 `int` 最大值，保证任何真实 rank 都更小。

**带锁缓存：**

[src/utils/tokenizer/bpe_tokenizer.cpp:234-246](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/bpe_tokenizer.cpp#L234-L246) `BpeForPieceCached` 包了一层：先查 `bpe_cache_`（`unordered_map<string, vector<string>>`），命中直接返回引用；未命中才调用 `BpeForPiece` 计算并插入缓存。两次访问都用 `std::lock_guard<mutex>` 保护。缓存键就是 piece 字符串本身。这是合理的，因为自然语言里同一个词（如 `"▁the"`）会反复出现，缓存能避免重复合并。

> 为什么用 `mutable`？因为 `encode` 是 `const` 方法，但缓存写入需要修改 `bpe_cache_`，所以这两个成员标为 `mutable`（见 `bpe_tokenizer.h:94-95`）。`cache_mu_` 的存在让 `BpeTokenizer` 在多线程并发 encode 时也是安全的。

**token 字符串 → id（带兜底）：**

[src/utils/tokenizer/bpe_tokenizer.cpp:275-294](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/bpe_tokenizer.cpp#L275-L294) `TokensToIds` 把合并后的 token 字符串查成 id。如果一个 token 不在词表里：`fallback_to_chars_=true` 时把它**再拆成单字符**逐个查，单字符还查不到就输出 `unk_id`（若 ≥0）；`fallback_to_chars_=false` 时直接输出 `unk_id`。这就是 `LoadFromFiles` 第五个参数 `fallback_to_chars` 的作用——保证任何输入都不会「查不到 id 而崩溃」。

#### 4.2.4 代码实践

**目标**：手推一次 BPE 合并，确认你读懂了贪心过程。

**操作**：假设有如下 merges 表（rank 从小到大）：

```
l l      # rank 0：合并两个 l
h e      # rank 1：合并 h 和 e
he ll    # rank 2：合并 he 和 ll
```

对片段 `"hello"`（先切成字符 `h e l l o`）手推合并过程：

1. 相邻对：`(h,e)` rank=1、`(e,l)` ∞、`(l,l)` rank=0、`(l,o)` ∞。最小是 `(l,l)` rank=0 → 合并 → `h e ll o`。
2. 相邻对：`(h,e)` rank=1、`(e,ll)` ∞、`(ll,o)` ∞。最小是 `(h,e)` rank=1 → 合并 → `he ll o`。
3. 相邻对：`(he,ll)` rank=2、`(ll,o)` ∞。最小是 `(he,ll)` rank=2 → 合并 → `hello o`。
4. 相邻对：`(hello,o)` ∞ → 无可合并 → 停止。结果 `["hello", "o"]`。

**预期结果**：你能独立手推，并能解释「为什么不能跳过中间步骤一次性合并」——因为合并会改变相邻关系，必须一轮一轮来。

> 注意：上表是为讲解构造的**示例** merges，不是任何真实模型的 merges。真实结果以本地词表为准。

#### 4.2.5 小练习与答案

**练习 1**：如果把上面 merges 表里 `he ll` 这条规则删掉，`"hello"` 的最终切分会变成什么？

**答案**：第三轮 `(he,ll)` 查不到 rank（∞），只剩 `(ll,o)` 也是 ∞，于是停止在 `["he", "ll", "o"]`。少了高 rank 的规则，合并更不充分，token 更多。

**练习 2**：`BpeForPieceCached` 为什么用 piece 字符串而不是「字符序列」作为缓存键？

**答案**：piece 字符串本身就是确定且可哈希的，直接用它作键最简单；而且自然文本里词级 piece 复用率极高（"the"/"and" 反复出现），缓存命中率足够高。用字符序列还要额外序列化，徒增开销。

### 4.3 预分词与 byte encoder：bpe 与 bbpe 的两条路径

#### 4.3.1 概念说明

`BpeForPiece` 处理的是「一个 piece」，但**整段文本是怎么被切成 piece 的**？这正是 bpe 与 bbpe 的分水岭。`encode` 里有个内部函数 `flush_buffer`，它会根据 `use_byte_encoder_` 走两条完全不同的路：

| 路径 | `use_byte_encoder_` | 预处理 | 喂给 BPE 的单位 |
|------|---------------------|--------|-----------------|
| bpe（SentencePiece） | `false` | `PretokenizeSentencePiece`：按空格切词，每个词前缀加 `▁` | 逐个 piece 分别 BPE |
| bbpe（字节级） | `true` | `ByteEncode`：每个字节映射成可见码点 | 整段 encoded 串一次性 BPE |

> ⚠️ **关键认知**：一个模型的 `vocab.txt` 是**针对其中一种模式训练出来的**。bbpe 词表里的 token 是「字节映射后的码点串」，bpe 词表里的 token 是「带 `▁` 前缀的词片段」。**不能对一个词表随意切换 `use_byte_encoder`**——必须与 `model.json` 的 `tokenizer.type` 一致。例如 qwen3 是 `"bbpe"`，必须 `use_byte_encoder=true`（见 `bytelevelbpe_main.cpp` 的最后一个 `true`）。

#### 4.3.2 核心流程

**bpe 路径（SentencePiece 预分词）：**

```
PretokenizeSentencePiece("hello world")
  → 扫描码点，遇 Unicode 空格就把累积串作为一个词
  → 每个词前面加 ▁（U+2581）
  → ["▁hello", "▁world"]
再对每个 piece 调 BpeForPieceCached
```

**bbpe 路径（字节编码）：**

```
ByteEncode("hello world")
  → 对串里每个字节 b，查 byte_encoder_[b] 得到一个码点 cp
  → 把 cp 重新编码成 UTF-8 字符串
  → 得到一个「每个逻辑字符对应一个原始字节」的新字符串
再对整个新字符串调 BpeForPieceCached
```

#### 4.3.3 源码精读

**SentencePiece 预分词：**

[src/utils/tokenizer/bpe_tokenizer.cpp:198-221](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/bpe_tokenizer.cpp#L198-L221) 逐码点扫描，遇到 Unicode 空格（`IsUnicodeSpace`）就把已累积的 `curr` 作为一词、加上 `▁` 前缀输出。注意 `▁` 是 U+2581（一个三字节 UTF-8 字符），不是普通空格。这样 `"hello world"` 变成 `["▁hello", "▁world"]`，**空格信息被吸收进 `▁` 前缀**，token 里不含裸空格。

> 配合判空函数 [src/utils/tokenizer/bpe_tokenizer.cpp:62-75](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/bpe_tokenizer.cpp#L62-L75) 看：它不仅识别 ASCII 空白，还识别一串「CJK 常见全角空格 / 不间断空格」（如 U+3000 全角空格、U+00A0）。所以中日韩文本里夹的空格也能正确断词。

**字节映射表初始化（bbpe 核心）：**

[src/utils/tokenizer/bpe_tokenizer.cpp:127-149](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/bpe_tokenizer.cpp#L127-L149) `InitByteMaps` 构造两张表：`byte_encoder_[0..255]`（字节→码点）与 `byte_decoder_`（码点→字节的反查）。判定「可见」的规则是

\[
\text{printable}(b) \iff (0x21 \le b \le 0x7E) \lor (0xA1 \le b \le 0xAC) \lor (0xAE \le b \le 0xFF)
\]

可见字节映射到自身，不可见字节（控制符、空格 0x20、0x7F–0xA0、0xAD 等）映射到 `256+n`（n 随不可见字节递增）。代码注释说明这是「基于 GPT-2 byte encoder」的思路：目的是让所有 256 个字节都能用可见、不断行的码点表示。这样空格（0x20）会被映射成一个 256+ 开头的码点，再编码成多字节 UTF-8，**绝不会在 vocab 文件里变成裸空格**。

**字节编码：**

[src/utils/tokenizer/bpe_tokenizer.cpp:151-175](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/bpe_tokenizer.cpp#L151-L175) `ByteEncode` 逐字节查 `byte_encoder_` 得到码点，再手工把码点按 UTF-8 规则重新编码成字节串。关键是：输出串里**每个「逻辑字符」正好对应输入的一个字节**，后续 `BpeForPiece` 用 `Utf8Chars` 切分时，会把每个码点还原成一个符号——于是 bbpe 的 BPE 是在「字节级」上做的。

**字节解码：**

[src/utils/tokenizer/bpe_tokenizer.cpp:177-195](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/bpe_tokenizer.cpp#L177-L195) `ByteDecode` 是 `ByteEncode` 的逆操作：逐码点查 `byte_decoder_` 还原成原始字节。映射表外的码点直接丢弃（注释说明：确保纯字节还原）。

#### 4.3.4 代码实践

**目标**：用纸笔对比两种路径对同一个输入 `"a b"`（中间一个空格）的处理，理解空间信息为何不会丢失。

**操作**：

1. **bpe 路径**：`PretokenizeSentencePiece("a b")` → 空格触发断词 → `["▁a", "▁b"]`。注意第二个词 `b` 也带 `▁` 前缀，空格变成了 `▁`。
2. **bbpe 路径**：`ByteEncode("a b")` → 字节 `a`(0x61, 可见→自身)、空格(0x20, **不可见**→256+n)、`b`(0x62, 可见→自身) → 得到一个「a + 一个多字节码点 + b」的串。空格字节被改写成可见码点，BPE 在字节级合并。
3. 思考：为什么两种路径都不会丢失「这里曾有一个空格」的信息？

**预期结果**：bpe 把空格编码进 `▁` 前缀，bbpe 把空格字节改写成可见码点——殊途同归，都是为了「让空格/控制符不破坏 vocab 的按行解析」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `byte_encoder_` 要把空格（0x20）映射走，而字母 `a`（0x61）却映射到自身？

**答案**：因为 `0x20 < 0x21`，空格不满足 `printable` 条件，若直接保留会在 vocab.txt 里变成裸空格、破坏按行解析；而 `a` 在 `0x21..0x7E` 可见区间内，保留自身既能正确表示又不影响解析。

**练习 2**：如果对一个 bbpe 词表错误地设了 `use_byte_encoder=false`，会发生什么？

**答案**：`flush_buffer` 会走 SentencePiece 预分词，给每个词加 `▁` 前缀再去词表里查；但 bbpe 词表里根本没有 `▁` 前缀的 token，于是大量查不到，最终靠 `fallback_to_chars` 退化成一堆 unk/单字符，输出基本不可用。这印证了「开关必须与词表训练方式一致」。

### 4.4 encode / decode：公开 API 与往返行为

#### 4.4.1 概念说明

`encode` 和 `decode` 是对外接口（u3-l1 里 gpt 构造函数最终调用的就是这俩）。`encode` 把字符串变成 token id 序列，`decode` 把 id 序列还原成字符串。理想情况下 `decode(encode(s)) == s`（往返一致），但实际是否一致取决于模式与特殊令牌。

#### 4.4.2 核心流程

**encode 主流程：**

```
encode(text, add_bos, add_eos, add_cls, add_sep):
  if add_cls/add_bos: 头部插入 cls_id/bos_id
  扫描 text 的每个字节位置 i:
      在 additional_special_tokens 里做「最长匹配」
      if 命中某个 special token (matched_index):
          flush_buffer()                  # 先把已累积的普通文本做 BPE
          ids.push_back(该 special 的 id) # special 整体作为一个 id，绕过 BPE
          i += matched_len
      else:
          buffer.push_back(text[i]); i++  # 累积普通字符
  flush_buffer()                          # 处理尾部残余
  if add_sep/add_eos: 尾部插入 sep_id/eos_id
  return ids

flush_buffer():  # 把 buffer 里的普通文本变成 id
  if use_byte_encoder_: ByteEncode(buffer) → BpeForPieceCached(整串) → TokensToIds
  else: PretokenizeSentencePiece(buffer) → 逐 piece BpeForPieceCached → TokensToIds
```

**decode 主流程：**

```
decode(ids, skip_special_tokens):
  for id in ids:
      tok = id_to_token_[id]
      if skip_special_tokens 且 id 是特殊令牌: 跳过
      处理转义 \\t \\n \\r，拼接 tok 到 s
  if use_byte_encoder_: return ByteDecode(s)        # 字节级还原
  else: 把每个 ▁(U+2581) 换成空格，再 lstrip 前导空格  # SentencePiece 还原
```

#### 4.4.3 源码精读

**encode——特殊令牌的最长匹配：**

[src/utils/tokenizer/bpe_tokenizer.cpp:431-505](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/bpe_tokenizer.cpp#L431-L505) 这是 u3-l1 提到的「特殊令牌作为整体原子」的落地处。核心在 [src/utils/tokenizer/bpe_tokenizer.cpp:471-494](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/bpe_tokenizer.cpp#L471-L494)：对每个位置 `i`，遍历所有 `additional_special_tokens_`，记录能匹配上的**最长**那个（`len <= matched_len` 的跳过，保证 longest match）。命中后先 `flush_buffer()` 把之前的普通文本冲掉，再直接 `push_back` special id——**special token 永远不进 BPE**，整体占一个 id 位。这正是 VLM 里 `<|image_pad|>` 占位能被 `inject_image_embeds` 整段替换的前提（见 u5-l3）。

> 头尾的 `add_cls`/`add_bos`/`add_sep`/`add_eos` 都用 `id >= 0` 守卫，对应 id 为 -1（配置没给）时静默跳过，与 u3-l1「可选令牌用 find 静默得 -1」一致。

**flush_buffer——两条路径的分叉点：**

[src/utils/tokenizer/bpe_tokenizer.cpp:449-467](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/bpe_tokenizer.cpp#L449-L467) 一个 lambda，按 `use_byte_encoder_` 选 4.3 节的两条路径之一。注意 bpe 分支里**对每个 piece 分别**调 `BpeForPieceCached`，而 bbpe 分支对**整个 encoded 串**调一次——这正是「按词预分词」与「整段字节」的差别在代码里的直接体现。

**decode——模式相关的还原：**

[src/utils/tokenizer/bpe_tokenizer.cpp:507-557](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/bpe_tokenizer.cpp#L507-L557) 先拼 token 字符串（跳过特殊令牌、还原 `\\t\\n\\r` 转义），再按模式还原：bbpe 调 `ByteDecode` 做字节级逆映射；bpe 把 `▁`(U+2581) 换回空格并 `lstrip` 前导空格 [src/utils/tokenizer/bpe_tokenizer.cpp:538-554](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/bpe_tokenizer.cpp#L538-L554)。`lstrip` 是因为句子首个 piece 的 `▁` 前缀还原后会变成开头空格，通常需要去掉。

**往返一致性的边界**：bbpe 在字节级上是精确可逆的（`ByteEncode` 与 `ByteDecode` 互逆），所以 `decode(encode(s))` 对普通文本能精确还原。bpe 路径则因 `▁↔空格` 转换 + 前导空格 lstrip，**开头空格会丢失**，对绝大多数自然语言句子无影响，但不是逐字节可逆。

#### 4.4.4 代码实践

**目标**：用真实词表验证 encode→decode 往返一致；并观察 bbpe 模式下 token 序列的形态。

**关键前提**：`bytelevelbpe_main.cpp` 不是 xmake target（承接 u1-l2），但**分词器目录不依赖 ncnn**（本讲已确认 `src/utils/tokenizer/` 下无任何 `#include "ncnn..."`），所以可以把它单独编译出来，不需要 Vulkan/ncnn 环境。

**操作步骤**：

1. 准备一个已下载模型的 `vocab.txt` 与 `merges.txt`（例如 `assets/qwen3_0.6b/`；若没有，本实践退化为源码阅读）。

2. 在仓库根目录写一个 `bpe_test.cpp`（**示例代码，非项目原有文件**）：

   ```cpp
   #include <iostream>
   #include "utils/tokenizer/bpe_tokenizer.h"

   int main() {
       // qwen3 是 bbpe，所以第 6 个参数 use_byte_encoder = true
       // 参数顺序: vocab, merges, spec, add_special_if_missing,
       //          fallback_to_chars, use_byte_encoder
       auto tok = BpeTokenizer::LoadFromFiles(
           "assets/qwen3_0.6b/vocab.txt",
           "assets/qwen3_0.6b/merges.txt",
           SpecialTokensConfig{},   // 不登记额外特殊令牌
           true, true, true);       // use_byte_encoder=true → bbpe

       std::string s = "Hello world";
       auto ids = tok.encode(s);
       std::cout << "ids (" << ids.size() << "): ";
       for (int id : ids) std::cout << id << " ";
       std::cout << "\ndecode: [" << tok.decode(ids) << "]\n";
       std::cout << "round-trip ok: " << (tok.decode(ids) == s ? "YES" : "NO") << "\n";
       return 0;
   }
   ```

3. 直接编译（只需 C++20，不链接 ncnn）：

   ```bash
   g++ -std=c++20 -I src bpe_test.cpp src/utils/tokenizer/bpe_tokenizer.cpp -o bpe_test
   ./bpe_test
   ```

4. 把第 6 个参数改成 `false` 再编译运行一次，对比 token 数量与 decode 结果。

**需要观察的现象**：

- `use_byte_encoder=true`（bbpe，与词表匹配）：`decode` 结果应与输入一致（`round-trip ok: YES`），token id 序列长度合理。
- `use_byte_encoder=false`（与 bbpe 词表不匹配）：大量 token 查不到、退化成 unk/单字符，decode 结果**严重失真**——这验证了 4.3 节「开关必须与词表一致」。

**预期结果**：bbpe 模式往返一致；错误模式输出失真。具体 token id 数值**待本地验证**（取决于词表内容）。

#### 4.4.5 小练习与答案

**练习 1**：`encode` 为什么在命中特殊令牌时要先 `flush_buffer()` 再 `push_back` special id，而不是把整个文本一起 BPE？

**答案**：因为特殊令牌（如 `<|image_pad|>`）是「原子」，必须整体占一个 id、不能被 BPE 拆碎。先 flush 把它之前的普通文本单独 BPE 掉，再单独输出 special id，才能保证 special token 边界清晰——这是 VLM 后续整段替换占位的前提。

**练习 2**：`decode` 默认 `skip_special_tokens=true`。如果想在解码结果里保留 `<eos>` 这样的特殊令牌字符串，该怎么做？

**答案**：调用 `decode(ids, /*skip_special_tokens=*/false)`。此时 eos 等 id 不会被跳过，会把对应的 token 字符串（如 `"<|endoftext|>"`）原样拼进输出。

**练习 3**：`BpeTokenizer` 为什么删除了拷贝构造（`bpe_tokenizer.h:47-48`）却保留移动构造？

**答案**：成员里有 `mutable mutex cache_mu_`（互斥量不可拷贝）和几个大表（`unordered_map`/`vector`），拷贝代价高且 mutex 不能拷。删除拷贝、提供带锁的移动构造/赋值（见 `bpe_tokenizer.cpp:297-336`），既避免误拷又允许所有权转移（比如把临时对象从 `LoadFromFiles` 移出）。

## 5. 综合实践

把本讲四个模块串起来：**用源码追踪一次完整的 `encode("Hello world")` 调用**，标注每一步落在哪个函数、哪一行，并画出数据流。

任务清单：

1. 从 `encode` 入口 [src/utils/tokenizer/bpe_tokenizer.cpp:431](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/bpe_tokenizer.cpp#L431) 出发，假设没有特殊令牌命中，跟踪到 `flush_buffer`（449–467 行）。
2. 选定 `use_byte_encoder=true`（bbpe）分支：`ByteEncode`（151–175）→ `BpeForPieceCached`（234–246）→ `BpeForPiece`（248–273）→ `TokensToIds`（275–294）。
3. 在一张纸上画出：`"Hello world"` 的字节 → `ByteEncode` 后的码点串 → `Utf8Chars` 切分后的符号序列 → 每轮合并 → 最终 token 字符串 → id 序列。
4. 再跟踪 `decode`（507–557）的逆过程：id → token 字符串拼接 → `ByteDecode`（177–195）→ 原始字节。
5. 用 4.4.4 的 `bpe_test.cpp` 实际跑一次，把你纸上的中间结果与程序打印的 id 序列对照（token 字符串可通过 `id_to_token()` 查）。

**验收标准**：你能不查源码地讲出「一个字符从进入 `encode` 到变成 id，再从 id 变回字符」的完整旅程，并指出 bpe 与 bbpe 在哪一步分叉、为什么。

## 6. 本讲小结

- `LoadFromFiles` 是唯一入口：`vocab.txt` 行号即 id、`merges.txt` 行序即 rank（优先级），`PairKey` 用 `\t` 分隔避免键冲突。
- `BpeForPiece` 是贪心 BPE：每轮合并 rank 最小的相邻对，循环到无可合并；`BpeForPieceCached` 用 `mutable` + `mutex` 缓存，支持并发 encode。
- `use_byte_encoder` 是 bpe 与 bbpe 的唯一开关：bpe 走 SentencePiece 预分词（空格→`▁` 前缀、按词 BPE），bbpe 走 `ByteEncode`（每字节映射可见码点、整段 BPE）。开关必须与词表训练方式一致。
- byte encoder 的本质目的是让所有 256 个字节都用「可见、不断行」的码点表示，避免裸空格/控制符破坏 vocab 的按行解析。
- `encode` 用最长匹配把特殊令牌作为原子整体输出、绕过 BPE；`decode` 按模式做 `▁→空格`（bpe）或 `ByteDecode`（bbpe）还原，bbpe 在字节级精确可逆，bpe 会丢失前导空格。
- `TokensToIds` 的 `fallback_to_chars` 兜底保证任何输入都不会因查不到 id 而崩溃；`BpeTokenizer` 删拷贝、留移动，匹配其 mutex + 大表的成员构成。

## 7. 下一步学习建议

本讲把 BPE 分词器的内部机制讲透了，但 ncnn_llm 还支持另一种分词器——**Unigram（基于对数概率的 Viterbi 分段）**，用于 mclip 这类多语言嵌入模型。下一讲 **u3-l3 Unigram 分词器实现** 会对照本讲，讲解 `UnigramTokenizer` 如何用 Trie + Viterbi 选择最优切分，以及 `unk_penalty` 如何影响未登录词。

此外，本讲的 `encode` 输出 token id 后，这些 id 会进入 u2 的推理主链路（u2-l3 的 `bpe->encode` → embed）。如果你对「分词之后 token id 怎么变成向量」感兴趣，可以回看 u2-l3 的 prefill 流程；而采样的另一面（id 怎么从 logits 选出来）会在 u3-l4 采样策略里展开。
