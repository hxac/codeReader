# Unigram 分词器实现

## 1. 本讲目标

本讲承接 u3-l2（BPE / BBPE 分词器）。在 BPE 里，分词靠「贪心合并 rank 最小的相邻对」；本讲要讲项目里的另一种分词器——**UnigramTokenizer**。它用在多语言 CLIP（mclip）等嵌入模型的运行时里（见 u3-l1 提到的「真正的 unigram 分支只在嵌入运行时」）。

学完本讲你应该能够：

- 理解 Unigram 分词模型的核心数据：**词表 + 每个词的对数概率（logprob）**。
- 看懂项目如何用一棵 **Trie** 快速枚举某位置所有可能的词匹配。
- 手算一遍 **Viterbi 动态规划**如何为一段文本选出「总对数概率最大」的分段。
- 说清楚 `unk_penalty`（未登录词惩罚）在什么时候、以什么方式介入。
- 能够加载 `assets/mclip_unigram_tokenizer.txt`，对一句中英文混合句子做 encode，并和 BPE 的分段风格做对比。

本讲只讲「字符串 → token id」这一步的实现细节；它产出的 id 如何喂给嵌入/推理网络，留给后续讲义。

## 2. 前置知识

### 2.1 什么叫「Unigram 语言模型分词」

SentencePiece 提供两种主流分词算法：BPE 和 Unigram。它们的方向正好相反：

- **BPE**：自底向上。从单个字符开始，不断把出现频率最高（rank 最小）的相邻对合并成一个新词。u3-l2 讲的就是它。
- **Unigram**：自顶向下。先准备一个很大的词表，每个词带一个「出现概率」（训练时用 EM 算法估出来）。给一段文本，目标是**找出一种切分方式，使整段文本的概率最大**。词表里多余的词在训练阶段被逐步剪枝，这里推理时只用最终的词表。

换句话说，Unigram 分词本质上是一个 **最优化问题**：在所有把字符串切成若干词的方案里，选总概率最大的那一种。因为有指数级种切法，我们用动态规划（Viterbi）来高效求解。

### 2.2 为什么用对数概率

概率都是 0 到 1 之间的小数，连乘会越来越小、容易下溢。取对数后，**连乘变成连加**：

\[
\log(p_1 \cdot p_2 \cdots p_k) = \log p_1 + \log p_2 + \cdots + \log p_k
\]

于是「最大化总概率」等价于「最大化总对数概率」（对数是单调递增函数，不改变 argmax）。词表文件里每个词存的就是 \(\log p\)，通常是一个负数（因为 \(0<p<1\)，\(\log p<0\)）。我们要的是 **总对数概率最大**（即绝对值最小、最接近 0 的那个负数和）。

### 2.3 一点 Trie 树常识

Trie（前缀树）是一种把若干字符串按公共前缀组织起来的树。本项目用的是**按字节（0–255）展开**的 Trie：每个节点有 256 个子指针 `next[256]`，沿字符串的字节往下走。它的好处是：给定一个起点，顺着走一遍就能一次性列出「从该起点开始、所有能在词表里匹配上的词及其长度」。这正是 Viterbi 每一步要的候选集。

### 2.4 承接的前置认知

- u3-l1 讲过 `SpecialTokensConfig`（7 个 `optional<string>`）和 `SpecialTokenIds`（7 个 `int`，默认 -1）这两个结构，本讲的 `LoadFromFile` 正好用到它们。
- u3-l2 讲过 SentencePiece 预分词里把空格变成 `▁`（U+2581）前缀的约定，本讲的 Unigram 也用同样的约定，因为它们都来自 SentencePiece 体系。
- u1-l3 提到「分词器单独成库 `ncnn_tokenizer`」，本讲的 `unigram_tokenizer.cpp` 就在这个库里。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/utils/tokenizer/unigram_tokenizer.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/unigram_tokenizer.h) | `UnigramTokenizer` 类声明：公开 `LoadFromFile`/`encode`/`decode`，私有 Trie 节点、Viterbi、缓存等。 |
| [src/utils/tokenizer/unigram_tokenizer.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/unigram_tokenizer.cpp) | 全部实现：模型加载、Trie 构造、Viterbi 分段、encode/decode。 |
| [examples/unigram_main.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/examples/unigram_main.cpp) | 最小调用示例：加载模型、encode、decode 并打印。 |
| [src/utils/tokenizer/tokenizer_types.h](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/tokenizer_types.h) | `SpecialTokensConfig` / `SpecialTokenIds`（u3-l1 已讲，本讲复用）。 |
| [src/ncnn_embedding.cpp](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.cpp) | 真正用到 `UnigramTokenizer` 的地方：嵌入运行时按 `tokenizer.type == "unigram"` 分支加载。 |

> ⚠️ 重要事实：`examples/unigram_main.cpp` 这个文件**存在**，但它**并没有在 `xmake.lua` 里注册成 target**（u1-l2 已经指出 README 提到的一些 target 实际未定义，`unigram_main` 就是其中之一）。所以「直接 `xmake build unigram_main`」会失败，第 5 节的实践会给出两种让它跑起来的办法。

## 4. 核心概念与源码讲解

encode 的整体数据流是这样的（自顶向下四步）：

```
原始文本 text
   │  ① PretokenizeSentencePiece：按 Unicode 空白切词，每个词前加 ▁
   ▼
pieces: ["▁Hello", "▁世界!", ...]
   │  ② SegmentPiece（每个 piece 跑一次 Viterbi，带缓存）
   ▼
token 串: ["▁He","llo","▁世","界","!"]
   │  ③ TokensToIds：词串查表 → id（OOV 走 unk）
   ▼
ids: [123, 456, ...]
   │  ④ 首尾按需插入 cls/bos/sep/eos
   ▼
最终 id 序列
```

本讲的四个最小模块对应其中不同环节：

- **4.1 LoadFromFile**：把词表文件读成 `id_to_token_` + `token_logprob_`，构造反向表，并注册特殊 token。
- **4.2 BuildTrie / MatchAt**：把词表组织成 Trie，提供「某位置有哪些词匹配」的查询。
- **4.3 SegmentPiece（Viterbi）**：用动态规划为一段文本求总对数概率最大的切分。
- **4.4 unk_penalty**：当某个位置完全没有任何词匹配时，如何给未登录词打分、如何落到 `unk_id`。

---

### 4.1 LoadFromFile：词表 + 对数概率的加载

#### 4.1.1 概念说明

Unigram 模型文件是一个**纯文本**文件，每行一个词及其对数概率，格式是「词 `<空白>` 对数概率」。例如 `assets/mclip_unigram_tokenizer.txt` 的开头几行：

```
<s> 0.0
<pad> 0.0
</s> 0.0
<unk> 0.0
, -3.4635426998138428
. -3.625642776489258
▁ -3.9299705028533936
s -5.072621822357178
▁de -5.306643009185791
```

关键约定（和BPE 的 vocab.txt 一样）：**行号就是 token id**。也就是说第 0 行 `<s>` 的 id 是 0，第 1 行 `<pad>` 的 id 是 1，依此类推。所以词表同时承担「词→id」和「id→词」两个方向。

特殊 token（`<s>`/`<pad>` 等）也写在词表里，它们的概率填 0.0（实际不会参与正文分词）。

#### 4.1.2 核心流程

`LoadFromFile` 是唯一的构造入口（构造函数是 private），它做这几件事：

1. 调用 `LoadModel` 逐行解析文件，得到 `tokens`（词串数组）和 `scores`（对数概率数组）。
2. 把 `tokens` 存为 `id_to_token_`（id→词），`scores` 存为 `token_logprob_`。
3. 用 `BuildTokenToId` 构建 `token_to_id_`（词→id 的哈希表）。
4. 记下 `fallback_to_chars_` 和 `unk_penalty_` 两个开关。
5. `BuildTrie()` 把词表组织成 Trie（4.2 节）。
6. `EnsureSpecialTokens()` 确保特殊 token 有 id（4.1.3 末尾讲）。

#### 4.1.3 源码精读

逐行解析靠 `ParseTokenAndScore`：它找到一行里**最后一个空白**，把空白之前当词、之后当对数概率，用 `std::stod` 解析数字，解析失败就跳过该行。

[unigram_tokenizer.cpp:L18-L38](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/unigram_tokenizer.cpp#L18-L38) — 把「`<s> 0.0`」这样的行切成 `tok="<s>"`、`score=0.0`。用「最后一个空白」而不是「第一个」是为了兼容词本身含空格的写法。

[unigram_tokenizer.cpp:L112-L127](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/unigram_tokenizer.cpp#L112-L127) — `LoadModel`：打开文件失败直接抛异常；逐行调用上面的解析器；全部解析失败（词表为空）也抛异常。`reserve(100000)` 是为 25 万量级词表预分配、避免反复扩容。

[unigram_tokenizer.cpp:L232-L252](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/unigram_tokenizer.cpp#L232-L252) — `LoadFromFile` 的完整串联。注意最后一行 `return std::move(tok);`，注释说是为避免 MSVC 尝试拷贝（因为拷贝构造被 `delete` 了，见头文件 [unigram_tokenizer.h:L32-L33](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/unigram_tokenizer.h#L32-L33)）。这是一个**只能移动、不能拷贝**的对象，因为它内部有 `mutable mutex`（缓存锁），拷贝含互斥量的对象没有意义。

[unigram_tokenizer.cpp:L129-L136](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/unigram_tokenizer.cpp#L129-L136) — `BuildTokenToId`：遍历 `id_to_token_`，建「词→id」哈希表。`reserve(size*2)` 降低哈希冲突。

[unigram_tokenizer.cpp:L138-L162](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/unigram_tokenizer.cpp#L138-L162) — `EnsureSpecialTokens`：对 bos/eos/unk/sep/pad/cls/mask 七个特殊 token，先在词表里查；查不到且 `add_if_missing=true` 就**追加到词表末尾**并赋一个新 id，对数概率给 `-1e9`（极低分，确保它们绝不参与正文 Viterbi 竞争），同时顺手加进 Trie。这和 u3-l1 讲的「额外特殊令牌须单独注册」是同一套思路。

#### 4.1.4 代码实践

**目标**：亲手看清「文件行 → id」的对应关系。

**步骤**：

1. 打开 `assets/mclip_unigram_tokenizer.txt`，看前 4 行（`<s>`/`<pad>`/`</s>`/`<unk>`，id 分别是 0/1/2/3）。
2. 在 `LoadModel` 里下断点（或加一行 `fprintf(stderr, ...)` 临时日志，实践后记得删掉），打印前 5 个 `tokens[i]` 和 `scores[i]`。
3. 加载完成后调用 `tokenizer.vocab_size()` 和 `tokenizer.id_to_token()[0]`，确认它们和文件首行一致。

**观察与预期**：

- `vocab_size()` 应约等于文件行数（250002），若传了不在词表里的特殊 token 还会再多几个。
- `id_to_token()[0]` 应为 `"<s>"`，`id_to_token()[3]` 应为 `"<unk>"`。
- 具体打印数值「待本地验证」（取决于实际词表）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `LoadFromFile` 要写成静态工厂，而不是公开构造函数？

**参考答案**：构造分多步（加载文件、建哈希、建 Trie、注册特殊 token），任何一步都可能抛异常；若用公开构造函数，失败时对象处于半初始化状态。静态工厂要么返回一个完整可用对象、要么抛异常，状态更干净。配合 private 默认构造和 deleted 拷贝构造，把对象构造路径收敛到唯一一处。

**练习 2**：`ParseTokenAndScore` 用「最后一个空白」分割，而不是「第一个」，这是为什么？

**参考答案**：词本身可能包含空格（虽然 Unigram 词表里少见，但格式上允许）。用最后一个空白，能保证分割出的「数字部分」是行末的连续无空白串，而「词部分」可以含中间空格。这样对含空格的词更健壮。

---

### 4.2 BuildTrie / MatchAt：用 Trie 枚举候选词

#### 4.2.1 概念说明

Viterbi 每到一个位置 i，都需要知道「从 i 开始，词表里有哪些词能匹配上、各有多长」。如果每次都去哈希表里枚举所有可能长度的子串，复杂度会是 \(O(\text{词表大小})\)，对 25 万词表完全不可接受。

Trie 解决了这个问题：把词表所有词按字节前缀组织成一棵树，从位置 i 顺着文本字节往下走，**每碰到一个词的终止节点就记录一条匹配**，走不动就停。一次查询复杂度只和「从 i 开始能匹配的最长串长度」有关，与词表大小无关。

#### 4.2.2 核心流程

Trie 节点的内存表示（[unigram_tokenizer.h:L63-L69](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/unigram_tokenizer.h#L63-L69)）：

```
struct TrieNode {
    int next[256];   // 按字节值索引的子节点下标，-1 表示无
    int token_id;    // 若某个词在此节点终止，存其 id；否则 -1
};
```

整棵 Trie 用一个 `vector<TrieNode> trie_` 平铺存储，下标 0 是根节点。这种「数组 + 下标」的写法比指针节点缓存更友好、分配更少。

- `BuildTrie`：清空、建根，然后对每个词调用 `AddToTrie`。
- `AddToTrie(token, id)`：从根开始，按 token 的每个字节往下走，缺节点就 `emplace_back` 新建，最后把 `id` 写到终止节点的 `token_id`。
- `MatchAt(s, pos, out)`：从根开始，沿 `s[pos], s[pos+1], ...` 的字节往下走，每遇到 `token_id >= 0` 的节点就往 `out` 里追加一条 `(id, 长度)`。走到 `next[c] == -1` 就停。

注意 `MatchAt` 会收集**所有前缀匹配**，不止最长的一个。例如词表里有 `▁`、`▁d`、`▁de`，在 `▁de...` 处会一次性返回三条匹配。

#### 4.2.3 源码精读

[unigram_tokenizer.cpp:L165-L171](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/unigram_tokenizer.cpp#L165-L171) — `BuildTrie`：先 `emplace_back()` 造根节点（下标 0），再遍历 `id_to_token_` 逐个插入。

[unigram_tokenizer.cpp:L173-L185](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/unigram_tokenizer.cpp#L173-L185) — `AddToTrie`：核心插入循环。`trie_[node].next[c] == -1` 时新建节点（`nxt = trie_.size()` 后 `emplace_back`）。终止处写 `trie_[node].token_id = token_id`，注释「唯一 token」表示同一字符串不会重复插入。

[unigram_tokenizer.cpp:L187-L201](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/unigram_tokenizer.cpp#L187-L201) — `MatchAt`：`for (i = pos; i < s.size(); ++i)` 逐字节走，`nxt == -1` 时 `break`；`token_id >= 0` 时追加 `(tid, i+1-pos)`。这正是 Viterbi 要的候选枚举。

#### 4.2.4 代码实践

**目标**：观察 `MatchAt` 一次返回多条匹配。

**步骤**：

1. 假设词表里有 `▁`、`▁d`、`▁de`（mclip 词表里确实有 `▁ -3.929...`、`▁de -5.306...` 这类条目）。
2. 在 `MatchAt` 里临时打印 `pos` 和每次追加的 `(tid, len)`。
3. 对输入 `"▁de xxx"` 调用一次 encode，观察日志。

**观察与预期**：

- 在 `pos=0`（指向 `▁` 的首字节）处，应一次性输出形如 `(▁ 的 id, len_a)`、`(▁d 的 id, len_b)`、`(▁de 的 id, len_c)` 三条（具体 id「待本地验证」）。
- 这说明 Trie 不只返回最长匹配，而是返回沿途所有词终止点——这正是 Viterbi 需要的全集。

#### 4.2.5 小练习与答案

**练习 1**：为什么 Trie 节点用 `int next[256]` 而不是 `std::unordered_map<unsigned char, int>`？

**参考答案**：用定长数组 `[256]` 查询是 \(O(1)\) 且无哈希开销，缓存连续、常数因子小。代价是每个节点固定占 256 个 int（约 1KB）。对分词器这种「构造一次、查询海量次」的场景，查询速度更重要，且总词表 Trie 节点数可控，内存开销可接受。

**练习 2**：`MatchAt` 返回的匹配条目是按什么顺序排列的？这会影响 Viterbi 结果吗？

**参考答案**：按「长度从小到大」排列（因为沿字节顺序往下走，先碰到的终止点短、后碰到的长）。不影响 Viterbi 结果，因为 Viterbi 会遍历所有候选取最大值，与候选顺序无关。

---

### 4.3 SegmentPiece：Viterbi 最优分段

这是 Unigram 分词的**核心算法**，也是它和 BPE 最大的区别。

#### 4.3.1 概念说明

给定一段文本 `piece`（注意是预分词后的一个「词」，已经带上了 `▁` 前缀），我们要把它切成若干个词表里的词，使得**这些词的对数概率之和最大**。

设 `piece` 长度为 \(n\)。定义 \(dp[i]\) 为「**从位置 \(i\) 到末尾**这段后缀，最佳切分的最大总对数概率」。边界：

\[
dp[n] = 0
\]

对每个位置 \(i\)，枚举所有从 \(i\) 出发能匹配上的词（设词 id 为 \(t\)、长度为 \(L\)、对数概率为 \(\text{logprob}[t]\)），有转移：

\[
dp[i] = \max_{(t,L)\in \text{matches}(i)} \big(\text{logprob}[t] + dp[i+L]\big)
\]

最终答案（整段的最大总对数概率）是 \(dp[0]\)。记录每个位置选了哪个 \(L\)（`back_len[i]`）和哪个 \(t\)（`back_tid[i]`），从 \(i=0\) 顺着回溯就能复原切分结果。

> 这种「从右往左填表」的写法和教科书上「从左往右」的 Viterbi 等价，都求得全局最优；这里采用从右往左只是实现选择。

#### 4.3.2 核心流程

`SegmentPiece(piece)` 的步骤：

1. \(n = \text{piece.size()}\)；建三个长度 \(n+1\) 的数组：`dp`（初值 \(-\infty\)）、`back_len`（初值 0）、`back_tid`（初值 -1）；`dp[n]=0`。
2. **从 \(i=n-1\) 倒着到 \(0\)**：
   - 调 `MatchAt(piece, i, matches)` 拿候选集。
   - 若 `matches` 非空：对每条 `(t, L)`，算 `cand = logprob[t] + dp[i+L]`，若 `cand > dp[i]` 就更新 `dp[i]/back_len/back_tid`。
   - 若 `matches` 为空：走「单字符回退 / unk 惩罚」分支（见 4.4 节）。
3. **回溯**：从 \(i=0\) 起，按 `back_len[i]` 切出子串、`i += back_len[i]`，直到 \(i=n\)。带一个安全兜底：若 `back_len[i] <= 0` 就强制前进一个 UTF-8 字符，防止死循环。

整个 encode 流程还会在 `SegmentPiece` 外面套一层缓存（`SegmentPieceCached`）：同一个 piece 第二次出现直接返回缓存结果，因为分词结果是无状态的纯函数。

#### 4.3.3 源码精读

[unigram_tokenizer.cpp:L269-L345](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/unigram_tokenizer.cpp#L269-L345) — `SegmentPiece` 全函数。重点几段：

- [L274-L277](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/unigram_tokenizer.cpp#L274-L277)：三个 DP 数组初始化，`dp` 用 \(-\infty\)（`-std::numeric_limits<double>::infinity()`），`dp[n]=0` 是后缀为空时概率为 1（\(\log 1 = 0\)）。
- [L281-L295](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/unigram_tokenizer.cpp#L281-L295)：`matches` 非空时的主转移，正是公式 \(dp[i]=\max(\text{logprob}[t]+dp[i+L])\)。
- [L326-L344](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/unigram_tokenizer.cpp#L326-L344)：回溯段。`back_len[i] <= 0` 的兜底保证即使 DP 没填上也不会卡死，强制按 UTF-8 字符前进。

[unigram_tokenizer.cpp:L255-L267](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/unigram_tokenizer.cpp#L255-L267) — `SegmentPieceCached`：双检查锁，先加锁查缓存，未命中则调 `SegmentPiece`，再加锁写缓存。`piece_cache_` 和 `cache_mu_` 都是 `mutable`（[unigram_tokenizer.h:L81-L82](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/unigram_tokenizer.h#L81-L82)），所以 `encode` 能保持 `const`。

#### 4.3.4 代码实践

**目标**：手算一个最小例子，理解 Viterbi 选「总对数概率最大」的切分。

**步骤**：

1. 假设一个玩具词表（仅用于手算，**不是**项目真实词表）：

   | 词 | logprob |
   |----|---------|
   | `▁` | -1.0 |
   | `▁a` | -2.0 |
   | `a` | -3.0 |
   | `▁ab` | -5.0 |
   | `b` | -2.5 |

2. 对输入 `piece = "▁ab"`（\(n=3\)，假设 `▁` 占 3 字节但这里按「逻辑字符」简化为 3 个符号位置），手算 DP：
   - \(dp[3]=0\)
   - \(i=2\)（`b`）：匹配 `b`（len 1）→ \(dp[2] = -2.5 + dp[3] = -2.5\)
   - \(i=1\)（`ab` 中 `a`）：匹配 `a`（len 1）→ \(-3.0 + dp[2] = -5.5\)
   - \(i=0\)（`▁`）：匹配 `▁`（len 1, -1.0）、`▁a`（len 2, -2.0）、`▁ab`（len 3, -5.0）
     - `▁` + \(dp[1]\)：\(-1.0 + (-5.5) = -6.5\)
     - `▁a` + \(dp[2]\)：\(-2.0 + (-2.5) = -4.5\)
     - `▁ab` + \(dp[3]\)：\(-5.0 + 0 = -5.0\)
     - 三者最大是 **-4.5**，选 `▁a`。
   - 回溯：`▁a`（位置 0–1）+ `b`（位置 2）→ 切成 `["▁a", "b"]`，总 logprob = -4.5。

3. 对照源码 L281–L295 确认：DP 取的确实是「`logprob[t] + dp[i+L]` 的最大值」。

**观察与预期**：

- 即使 `▁ab` 是一个整词，Viterbi 也不会选它（-5.0 < -4.5），因为切成 `▁a`+`b` 总概率更高。这正是 Unigram 相对贪心 BPE 的特点：**全局最优**而非局部贪心。
- 注意：上面是为讲解而简化的手算，真实 `▁` 是 3 字节 UTF-8，真实 Trie 按字节走；要用项目代码跑请见第 5 节综合实践。

#### 4.3.5 小练习与答案

**练习 1**：如果把某个高频词的 logprob 调得很大（接近 0），它的切分倾向会怎样变化？

**参考答案**：DP 转移里 `cand = logprob[t] + dp[i+L]`，logprob 越大（越接近 0）`cand` 越大、越容易被选中。所以高频词（logprob 大）会被优先「吸附」进切分结果，低频长词反而容易被拆成更小的、总概率更高的组合。这也解释了为什么训练时要剪掉那些「总是拼不过子组合」的词。

**练习 2**：为什么 `SegmentPiece` 要从 \(i=n-1\) 倒着填，而不是从 \(i=0\) 正着填？

**参考答案**：因为状态定义 \(dp[i]\) 依赖**右侧**的 \(dp[i+L]\)（后缀的最优值）。要算 \(dp[i]\) 必须先算好比它更靠右的所有 \(dp[i+L]\)，所以只能从右往左填。若把状态改成「前缀的最优值」就可以从左往右填，两种定义等价，作者选了后缀版。

---

### 4.4 unk_penalty：未登录词的惩罚

#### 4.4.1 概念说明

词表再大也覆盖不了所有字符。当某个位置 \(i\) 上**完全没有任何词表词能匹配**（`MatchAt` 返回空），Viterbi 不能直接卡死，必须给这条「无词可切」的路径打一个分数，让它仍能参与比较。这个分数就是 `unk_penalty`（默认 -10.0）。

直觉上：一个真正的词，logprob 大概在 -3 到 -8 之间；而一个未登录字符要付出 -10 的代价。所以只要存在任何「用真实词覆盖该位置」的切法，它的 `cand` 都会比走 unk 的 -10 高，unk 路径就会被淘汰。只有当一个字符既不在词表、又不能拼进任何词时，才会被迫走 unk 路径，最终在 `TokensToIds` 里映射成 `unk_id`。

注意区分两层「惩罚」：

- 词表里若收录了字符本身（如 `s`、`▁`），它有自己的 logprob（如 -5.07），这不算 unk，是正常切分。
- `unk_penalty=-10` 只在「字符连词条都匹配不上」时才用，是一个**实现层面的兜底分数**，不是词表里的真实概率。

#### 4.4.2 核心流程

`SegmentPiece` 的 `else` 分支（`matches` 为空时）：

1. 用 `NextUtf8` 取从 \(i\) 开始的一个完整 UTF-8 字符（`cplen` 字节）。
2. 在 `token_to_id_` 里查这个单字符：
   - **查得到**：用它的真实 logprob 做 `cand`，正常更新 DP（这种情况下 `back_tid >= 0`，不是 unk）。
   - **查不到**：用 `unk_penalty_` 做 `cand`，`back_tid[i] = -1`（标记为未知），更新 DP。
3. 回溯后，`back_tid == -1` 的位置切出的子串在 `TokensToIds` 里找不到 id，于是映射成 `unk_id`（若 `unk_id >= 0`），否则该字符被丢弃。

注意一个重要细节：**只有当某位置完全无词匹配时才进入单字符回退分支**。若该位置有任意词匹配（哪怕只是个短词），就只在 `matches` 里选优，不会把单字符/unk 选项混进来比较。

#### 4.4.3 源码精读

[unigram_tokenizer.cpp:L296-L323](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/unigram_tokenizer.cpp#L296-L323) — `SegmentPiece` 的 `else`（无匹配）分支。重点：

- [L300-L304](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/unigram_tokenizer.cpp#L300-L304)：`NextUtf8` 推进一个码点，失败就按单字节处理（防损坏字节流死循环）。
- [L305-L313](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/unigram_tokenizer.cpp#L305-L313)：单字符在词表里 → 用真实 logprob，`back_tid` 为正 id。
- [L314-L322](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/unigram_tokenizer.cpp#L314-L322)：单字符不在词表 → 用 `unk_penalty_`，`back_tid = -1` 标记未知。

[unigram_tokenizer.cpp:L348-L384](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/unigram_tokenizer.cpp#L348-L384) — `TokensToIds`：把切分出的词串变 id。`fallback_to_chars_=true` 时，对找不到的词再按字符分解查表，实在查不到就 `push_back(unk_id)`（[L364-L366](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/unigram_tokenizer.cpp#L364-L366) 与 [L376-L377](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/unigram_tokenizer.cpp#L376-L377)）。若 `unk_id < 0`（没注册 unk），未知字符被静默丢弃。

`unk_penalty` 的默认值在两处给出：函数签名默认值 [unigram_tokenizer.h:L15](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/unigram_tokenizer.h#L15) 是 `-10.0`；实际调用方（`ncnn_embedding.cpp` 和 `unigram_main.cpp`）也传 `-10.0`。

#### 4.4.4 代码实践

**目标**：观察 unk 路径的触发与 `unk_id` 的出现。

**步骤**：

1. 用 mclip 词表（覆盖中英日韩等多语种）encode 一句含「生僻字」或 emoji 的句子，例如 `"测试🚀rocket"`（emoji 多半不在词表）。
2. 在 `SegmentPiece` 的 `else` 分支里临时打印「位置 \(i\)、字符、是否查到」。
3. 在 `TokensToIds` 里打印每个最终 id，重点看是否出现 `unk_id`（mclip 的 `<unk>` id 是 3）。

**观察与预期**：

- 对词表覆盖好的中文/英文字符，应走 `matches` 非空分支，不会触发 unk_penalty。
- 对 emoji 之类完全不在词表、且其字节序列不构成任何词前缀的字符，应走 `else` 分支，`back_tid=-1`，最终 `TokensToIds` 输出 `unk_id=3`。
- 把 `unk_penalty` 改成 `0.0` 不会改变「是否出现 unk」（因为那些字符本就无词可切），但会改变 DP 在「有部分匹配」边界处的取舍——具体输出「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：把 `unk_penalty` 设得非常大（例如 0.0 或正数），会改变分词结果吗？为什么？

**参考答案**：在「该位置完全无词匹配」的分支里，`cand = unk_penalty + dp[i+cplen]`。若 unk_penalty 被抬高，那些被迫走 unk 的位置分数变高，可能让某些边界情况从「切短词 + 走 unk」翻盘。但对绝大多数有正常词覆盖的文本，最优切分不变，因为根本进不了 `else` 分支。所以「局部影响存在，全局影响通常很小」。

**练习 2**：为什么项目把 `unk_penalty` 默认设成 `-10.0` 这样一个比正常词 logprob（约 -3~-8）更负的值？

**参考答案**：为了让「走 unk」始终是最后才被接受的选项。只要存在任何用真实词覆盖该字符的切法（其 logprob 远大于 -10），DP 都会优先选它。只有真的无词可切时才接受 -10 的代价，并把对应字符最终映射成 `unk_id`。这保证分词器在词表覆盖范围内尽量精确，OOV 时才退化。

---

## 5. 综合实践

把四个模块串起来，完成一次「加载 → encode → decode → 与 BPE 对比」。

### 5.1 让 `unigram_main` 可运行

如前所述，`examples/unigram_main.cpp` 没有在 `xmake.lua` 里注册成 target。有两种让它跑起来的办法（任选其一）：

**办法 A（推荐）：临时加一个 xmake target。** 在 `xmake.lua` 末尾追加（这是你在自己工作副本里的练习操作，不属于修改源码逻辑）：

```lua
target("unigram_main")
    set_kind("binary")
    set_languages("c++20")
    add_files("examples/unigram_main.cpp")
    add_deps("ncnn_tokenizer")
    set_rundir("$(projectdir)/")
```

注意 `unigram_main.cpp` 只依赖 `ncnn_tokenizer` 这个静态库（它编译 `src/utils/tokenizer/*.cpp`，已含 `unigram_tokenizer.cpp`），不需要 ncnn 和 nlohmann_json。然后：

```
xmake build unigram_main
xmake run unigram_main assets/mclip_unigram_tokenizer.txt "Hello 世界 abc"
```

**办法 B：不改 xmake，直接读源码跟踪流程。** 参考下面 5.3 的源码阅读型实践。

> 具体构建命令与运行结果「待本地验证」（取决于本地 xmake/编译器环境）。

### 5.2 观察输出并对比 BPE

`unigram_main.cpp` 的流程（[examples/unigram_main.cpp:L12-L32](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/unigram_main.cpp#L12-L32)）：配置特殊 token → `LoadFromFile` → `encode(text, add_bos=true, add_eos=true)` → 打印 ids → `decode(ids, skip_special=true)` → 打印还原文本。

跑起来后请记录：

1. **Encoded IDs** 序列（首尾应是 bos/eos 的 id）。
2. **Decoded** 文本，验证它和输入基本一致（开头空格可能被 `decode` 的 `▁`→空格 + 去前导空格逻辑吃掉，见 [unigram_tokenizer.cpp:L427-L441](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/unigram_tokenizer.cpp#L427-L441)）。

**与 BPE 的对比**（呼应 u3-l2）：

| 维度 | BPE（u3-l2） | Unigram（本讲） |
|------|--------------|-----------------|
| 切分依据 | merges.txt 的 rank，**贪心**合并最小 rank 相邻对 | 词表每个词的 logprob，**全局 Viterbi** 求最大总概率 |
| 最优性 | 局部贪心，不保证全局最优 | 全局最优（在给定词表下） |
| 数据文件 | vocab.txt + merges.txt 两个文件 | 单个 model.txt（词 + logprob） |
| 预分词 | SentencePiece 风格，空格→`▁` | 同样 SentencePiece 风格，空格→`▁` |
| OOV 处理 | bbpe 走 byte encoder；bpe 走 unk | `unk_penalty` 兜底 + `unk_id` |
| 项目用途 | gpt 主运行时（LLM/VLM/OCR/ASR） | 嵌入运行时（mclip） |

两者的根本差异：BPE 像「能合就合、按合并优先级」；Unigram 像「在所有切法里挑总概率最高的那种」。所以对同一句话，Unigram 往往会把高频长词整段保留（只要它的 logprob 比拆开更划算），而 BPE 的切分取决于 merges 表里有哪些合并规则。

### 5.3 源码阅读型实践（无需运行）

如果暂时无法构建，按下面顺序读一遍源码、在笔记里画出数据流：

1. [encode](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/unigram_tokenizer.cpp#L387-L407)：`PretokenizeSentencePiece` → 对每个 piece 调 `SegmentPieceCached` → `TokensToIds`。
2. [PretokenizeSentencePiece](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/unigram_tokenizer.cpp#L88-L109)：注意 `IsUnicodeSpace` 切词、`▁` 前缀。
3. [SegmentPiece](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/utils/tokenizer/unigram_tokenizer.cpp#L269-L345)：DP 数组、`MatchAt` 候选枚举、`else` 分支 unk 兜底、回溯。
4. 回答：为什么 `encode` 是 `const` 却仍能写缓存？（答：缓存表与锁是 `mutable`。）

## 6. 本讲小结

- Unigram 分词的本质是**最优化**：在所有切分方案里选「总对数概率最大」的，靠 **Viterbi 动态规划** 求解，与 BPE 的局部贪心合并不同。
- 词表文件每行「词 + 对数概率」，**行号即 id**；`LoadFromFile` 是唯一构造入口，对象只能移动、不能拷贝。
- 用一棵**按字节展开的 Trie**（`vector<TrieNode>` + `next[256]`）来一次性枚举某位置所有候选词，使 Viterbi 每步与词表大小无关。
- `SegmentPiece` 用 \(dp[i]=\max(\text{logprob}[t]+dp[i+L])\) 从右往左填表，再回溯复原切分；`SegmentPieceCached` 给它套了线程安全的缓存。
- `unk_penalty`（默认 -10.0）只在某位置**完全无词匹配**时兜底打分，最终经 `TokensToIds` 映射成 `unk_id`；它比正常词 logprob 更负，保证 OOV 是最后选项。
- Unigram 用在 mclip 等嵌入运行时（`ncnn_embedding.cpp` 的 `tokenizer.type == "unigram"` 分支），而 gpt 主运行时用 BPE。

## 7. 下一步学习建议

- **横向对比**：回到 u3-l2，把 BPE 的 `BpeForPiece`（贪心合并）和本讲的 `SegmentPiece`（Viterbi）放在一起读，体会两种分词范式。
- **向上承接**：U6（u6-l1 文本嵌入 API、u6-l2 CLIP 多模态嵌入）会用到本讲的 Unigram 分词器——嵌入运行时 `encode_text` 内部就是 `unigram_tokenizer->encode(text, true, true)`（见 [ncnn_embedding.cpp:L291-L292](https://github.com/futz12/ncnn_llm/blob/f2f29e41be164c788c36cc44bdbf2d0d4810477e/src/ncnn_embedding.cpp#L291-L292)）。学完本讲再去看嵌入 API，就能看清「文本 → id → 向量」的完整链路。
- **深入算法**：如果想彻底理解 Unigram 的训练侧（词表怎么来的、概率怎么估的），可查阅 SentencePiece 原始论文和 EM 算法；本讲只覆盖推理侧的分段。
- **下一站**：U4（位置编码 RoPE）。分词器解决「文本→id」，接下来要看 id 进入模型前后的位置编码如何生成。
