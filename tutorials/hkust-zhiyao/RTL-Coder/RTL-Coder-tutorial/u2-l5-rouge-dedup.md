# 基于 ROUGE-L 的指令去重

## 1. 本讲目标

本讲只解决一个问题：**当 GPT-3.5 变异出一条新指令后，RTL-Coder 如何判断它和已有指令「太像」而把它丢掉？**

学完后你应该能够：

- 说清 ROUGE-L 指标的直觉：它基于「最长公共子序列 LCS」衡量两段文本的顺序敏感重叠度。
- 读懂 `instruction_gen.py` 里 `RougeScorer`、`_score_lcs`、`fmeasure` 三件套各自的职责。
- 解释为什么阈值取 `0.7`，以及 `most_similar_instructions` / `avg_similarity_score` 两个字段记录了什么。
- 自己写脚本对一组指令两两算 ROUGE-L，并据此判断哪些会被 `0.7` 过滤掉。

本讲把 u2-l4 留作黑盒的「ROUGE-L 0.7 去重」彻底打开。

## 2. 前置知识

在进入源码前，先用三个小概念把直觉建立起来。

**（1）为什么需要去重？**
RTL-Coder 用 `#method#` 提示词让 GPT 对种子题目做「功能不同、方法/器件相似」的变异（见 u2-l2）。但 GPT 经常偷懒：换个位宽、调两句话顺序，本质还是同一道题。如果这些近似重复混进训练集，模型会反复见到同一种模式，泛化能力下降。所以每来一条新指令，都要和「已经留下的全部指令」比一遍相似度，太像就丢。

**（2）什么叫「最长公共子序列（LCS）」？**
给定两段文本，把它们切成词（token）后，找出一条**保持先后顺序、但不要求连续**的最长公共词序列。

例如：

- 序列 A：`["设计", "一个", "加法器", "和", "一个", "乘法器"]`
- 序列 B：`["设计", "一个", "乘法器"]`

LCS 是 `["设计", "一个", "乘法器"]`，长度 3。注意「乘法器」在 A 中排在「加法器」之后，在 B 中也排在前面两个词之后，顺序一致，所以算公共子序列。它**不要求相邻**——这是它与「最长公共子串（必须连续）」的关键区别。

**（3）ROUGE-L 是什么？**
ROUGE 是一类文本相似度指标。ROUGE-**L** 里的 L 就是 LCS。它用 LCS 长度算出三个数：

- 精确率（precision）：LCS 长度 占 新指令词数的比例。
- 召回率（recall）：LCS 长度 占 已有指令词数的比例。
- F 值（fmeasure）：精确率和召回率的调和平均，是一个 0 到 1 之间的单一相似度分数。

本讲代码最终只看 F 值，并用它和 `0.7` 比较。

> 一句话直觉：**两条指令共享的、保序的词越多（相对于各自总词数），ROUGE-L 的 F 值越接近 1，越像重复。**

## 3. 本讲源码地图

本讲只涉及一个文件，但只读其中一小段。

| 文件 | 本讲关注范围 | 作用 |
| --- | --- | --- |
| `data_generation/instruction_gen.py` | 第 12 行的 import、第 109–114 行的打分器初始化、第 138–154 行的去重主循环 | 用 ROUGE-L 给每条新指令打分，决定保留还是丢弃 |

回顾 u2-l4：主循环 `generate_instruction_following_data` 每轮调用 `askGPT35` 拿到响应，用 `post_process_gpt3_response_uniq` 解析成 `{Instruction, Input}` 字典列表。**本讲要精读的，就是紧接着「解析完之后、写盘之前」的那段去重代码（第 138–154 行）。**

另外提一个真实的坑（u2-l2 已点到）：代码用了 `from rouge_score import rouge_scorer`（[instruction_gen.py:L12](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/instruction_gen.py#L12)），但 `rouge_score` 并**没有**写进 `requirements.txt`。要本地复现本讲实验，需先手动 `pip install rouge_score`（注意包名带下划线，不是 `rouge-score` 的命令行工具）。

## 4. 核心概念与源码讲解

按「打分器初始化 → 逐条算分 → 阈值过滤」三步拆成三个最小模块。

### 4.1 打分器与词表初始化：RougeScorer

#### 4.1.1 概念说明

ROUGE 的计算需要两样东西：

1. 一个**分词器**：把自然语言句子切成词（token）列表，ROUGE 是在「词级别」上比对的。
2. 一个**打分函数**：给定两条 token 列表，算出 ROUGE-L 分数。

`rouge_score` 库把这两样打包在一个对象里——`rouge_scorer.RougeScorer`。创建时传入两个关键参数：

- `["rougeL"]`：只算 ROUGE-L 一种指标（该库还支持 ROUGE-1、ROUGE-2、ROUGE-3 等，分别对应 1-gram、2-gram、3-gram 重叠，本讲用不到）。
- `use_stemmer=False`：**不做词干还原**。意思是「counter」和「counters」会被当作两个不同的词，必须逐字符完全一致（且不区分大小写）才算匹配。这让比较更严格——变异出来的指令只有措辞真正接近才会被判重。

> 顺带理解选择 ROUGE-L 而非 ROUGE-1 的原因：ROUGE-1 只数「重叠的词的个数」（词袋），忽略词序；ROUGE-L 基于 LCS，要求重叠的词**顺序也一致**。指令是结构化文本，顺序敏感的 LCS 更贴近「是否在说同一件事」的直觉。

#### 4.1.2 核心流程

打分器只在主循环开始前创建**一次**，然后把「已有全部指令」一次性预分词缓存起来，避免每轮重复切词：

```text
1. 汇总 all_instructions = 种子指令 + 已生成并落盘的指令
2. 创建 scorer = RougeScorer(["rougeL"], use_stemmer=False)
3. all_instruction_tokens = 对每条指令预先 tokenize，存成「token 列表的列表」
```

第 3 步是性能优化：后面每来一条新指令，都要拿它和「全部已有指令」逐一比较。预先把已有指令切好词，比较时就只切新指令一次。

#### 4.1.3 源码精读

打分器与预分词的初始化在主函数开头（[instruction_gen.py:L109-L114](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/instruction_gen.py#L109-L114)）：

```python
all_instructions = [d["Instruction"] for d in seed_instruction_data] + [
    d["Instruction"] for d in target_instruction_data
]

scorer = rouge_scorer.RougeScorer(["rougeL"], use_stemmer=False)
all_instruction_tokens = [scorer._tokenizer.tokenize(inst) for inst in all_instructions]
```

- 第 109–111 行：把「种子指令」和「已落盘的目标指令」拼成 `all_instructions`，即**当前要去重比对的基准集合**。这保证断点续跑时，已生成的指令也参与比对，不会重复生成。
- 第 113 行：创建打分器，`use_stemmer=False` 关闭词干还原。
- 第 114 行：`scorer._tokenizer` 是打分器内部的分词器（一个会把文本转小写、按非字母数字切分的默认分词器），`.tokenize(inst)` 返回 token 列表。对全部基准指令预切词，结果存进 `all_instruction_tokens`。

> 注意 `_tokenizer` 前面有下划线，按 Python 惯例是「内部实现」。项目直接访问它，是为了复用同一个分词逻辑、保证新指令和已有指令用同一种方式切词——切词方式不一致会让分数失真。

#### 4.1.4 代码实践

**实践目标：** 直观感受 `use_stemmer=False` 对匹配结果的影响。

**操作步骤（示例代码，非项目原有）：**

```python
from rouge_score import rouge_scorer

scorer = rouge_scorer.RougeScorer(["rougeL"], use_stemmer=False)
# 看分词结果（会转小写、按非字母数字切开）
print(scorer._tokenizer.tokenize("Design a 4-bit Counter."))
# 对比：单复数不同的两个词
a = scorer._tokenizer.tokenize("design a counter")
b = scorer._tokenizer.tokenize("design two counters")
print(rouge_scorer._score_lcs(a, b).fmeasure)
```

**需要观察的现象：** 第一行会把句子切成 `['design', 'a', '4', 'bit', 'counter']` 这样的小写词列表；第二行的 F 值会因为 `counter` 与 `counters` 不完全相等而被拉低（`_score_lcs` 在 4.2 详讲）。

**预期结果：** 分词确实转小写并按非字母数字切分；`counter`/`counters` 不匹配导致 LCS 变短、F 值低于 1。具体数值**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1：** 如果把 `use_stemmer=False` 改成 `use_stemmer=True`，对「design a counter」和「design two counters」的 ROUGE-L 会变高还是变低？
**参考答案：** 变高。开启词干还原后，`counters` 会被还原成 `counter`，两个序列的公共子序列变长，LCS 增大，F 值上升。项目选 `False` 是为了更严格地识别「措辞真正接近」的近似重复。

**练习 2：** 为什么 `all_instruction_tokens` 要在主循环外预先计算，而不是每轮重新算？
**参考答案：** 每轮都要拿新指令和**全部**已有指令逐一比较。若每次都重新切全部已有指令，重复计算量随数据量平方膨胀；预切一次、之后只追加新指令的切词结果（见 4.3），把重复劳动降到最低。

---

### 4.2 逐条比对：_score_lcs 与 fmeasure

#### 4.2.1 概念说明

打分器建好后，真正的「算分」由函数 `rouge_scorer._score_lcs(new_tokens, existing_tokens)` 完成。它接收两条 token 列表，返回一个含 `precision`、`recall`、`fmeasure` 三个字段的 `Score` 对象。

它的数学内核是**最长公共子序列的动态规划**。设新指令 token 长度为 \(m\)，已有指令 token 长度为 \(n\)，二者最长公共子序列长度为 \(L\)，则：

\[
P = \frac{L}{m}, \qquad R = \frac{L}{n}, \qquad F = \frac{2 \cdot P \cdot R}{P + R}
\]

\(P\)（精确率）衡量「新指令里有多少比例的词出现在公共子序列中」，\(R\)（召回率）衡量「已有指令里有多少比例的词出现在公共子序列中」，\(F\) 是二者调和平均，落在 \([0,1]\)。

LCS 长度 \(L\) 用经典动态规划求解，递推关系为（\(x_i,y_j\) 为两条序列的第 \(i,j\) 个 token）：

\[
\mathrm{LCS}(i,j) =
\begin{cases}
0, & i=0 \text{ 或 } j=0 \\
\mathrm{LCS}(i-1,j-1)+1, & x_i = y_j \\
\max\big(\mathrm{LCS}(i-1,j),\ \mathrm{LCS}(i,j-1)\big), & \text{否则}
\end{cases}
\]

填满一张 \((m+1)\times(n+1)\) 的表后，右下角 \(\mathrm{LCS}(m,n)\) 即为 \(L\)。单次比较的时间与空间复杂度为 \(O(m\cdot n)\)。

> 关键性质：\(F\) 关于 \(P,R\) 对称，即交换两条指令的位置，\(F\) 不变。所以本讲里「新指令 vs 已有指令」和「已有指令 vs 新指令」结果一致，这也是代码最终只用 `fmeasure` 做判定的原因。

#### 4.2.2 核心流程

对每一条刚解析出来的新指令，执行：

```text
对每条新指令 new：
  1. new_tokens = scorer._tokenizer.tokenize(new["Instruction"])
  2. 对 all_instruction_tokens 里的每一条 existing_tokens：
       scores = _score_lcs(new_tokens, existing_tokens)   # 返回 Score 对象
  3. 抽出每个 Score 的 .fmeasure，得到一个相似度列表 rouge_scores
```

第 2 步是 \(O(N)\) 次比较（\(N\) = 已有指令数），每次比较内部又是 \(O(m\cdot n)\) 的 DP——所以这一步是整个数据生成流水线里最耗 CPU 的部分，也是数据集规模最终停在约 2.7 万条的一个现实约束。

#### 4.2.3 源码精读

逐条比对发生在去重循环开头（[instruction_gen.py:L139-L141](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/instruction_gen.py#L139-L141)）：

```python
new_instruction_tokens = scorer._tokenizer.tokenize(instruction_data_entry["Instruction"])
rouge_scores = [rouge_scorer._score_lcs(new_instruction_tokens,item) for item in all_instruction_tokens]
rouge_scores = [score.fmeasure for score in rouge_scores]
```

- 第 139 行：把这条新指令切词。注意 `instruction_gen.py` 里这个循环变量名也叫 `instruction_data_entry`，切的是它的 `Instruction` 字段（题目描述），**不含 `Input`**（端口骨架代码）——判重只看自然语言题目，不看代码模板。
- 第 140 行：列表推导，拿新指令的 token 和 `all_instruction_tokens` 里**每一条**预切好词的已有指令调 `_score_lcs`，得到一串 `Score` 对象。`_score_lcs` 是 `rouge_score` 库的模块级函数，内部跑 LCS 动态规划。
- 第 141 行：只保留每个 `Score` 的 `fmeasure` 字段。`rouge_scores` 现在是一个浮点列表，长度等于已有指令数，每个元素是新指令与某条已有指令的 ROUGE-L F 值。

#### 4.2.4 代码实践

**实践目标：** 用纸笔 + 代码，验证「共享保序词越多，F 值越高」。

**操作步骤（示例代码，非项目原有）：**

```python
from rouge_score import rouge_scorer

s = rouge_scorer.RougeScorer(["rougeL"], use_stemmer=False)

def f(a, b):
    return rouge_scorer._score_lcs(
        s._tokenizer.tokenize(a), s._tokenizer.tokenize(b)
    ).fmeasure

# 三组对照
print(f("design a full adder", "design a full adder"))        # 完全相同 → 1.0
print(f("design a full adder", "design a full adder with carry"))  # 后者多了词
print(f("design a full adder", "implement a memory controller"))  # 几乎无关
```

**需要观察的现象：** 第一组完全相同，LCS 等于全长，\(F=1\)；第二组大量重叠，\(F\) 较高但仍 \(<1\)；第三组几乎没有保序公共词，\(F\) 很低。

**预期结果：** 第一组 `1.0`，第二组明显高（介于 0.5–0.9 之间，具体**待本地验证**），第三组接近 0。这说明只有「措辞高度重合」才会逼近 1，无关题目分数很低。

#### 4.2.5 小练习与答案

**练习 1：** 假设新指令和某条已有指令各有 100 个 token，其中 60 个构成公共子序列。求 \(F\)。
**参考答案：** \(L=60,\ m=100,\ n=100\)，\(P=R=0.6\)，\(F = \frac{2\times0.6\times0.6}{0.6+0.6} = \frac{0.72}{1.2} = 0.6\)。当两序列等长时，\(F\) 恰好等于重叠比例 \(L/n\)。

**练习 2：** 为什么判重用 `fmeasure` 而不是单独用 `precision` 或 `recall`？
**参考答案：** 单用 `precision` 会被「新指令很短、恰好是已有指令的子串」误判为重复；单用 `recall` 会被「新指令很长、包含了某条已有指令」误判。\(F\) 是二者的调和平均，只有两边都高才算高，且它对称、与「新/已有」的指派无关，更适合做单一阈值判定。

---

### 4.3 阈值过滤与相似记录：0.7 与 most_similar_instructions

#### 4.3.1 概念说明

算出 `rouge_scores`（新指令对全部已有指令的 F 值列表）之后，要做两个决定：

1. **留还是丢？** 看最大相似度 `max(rouge_scores)`。如果连「最像的那条」都超过 `0.7`，说明这条新指令不过是已有指令的近似复述，**丢弃**；否则**保留**。
2. **记录留痕。** 即便保留，也把「与它最像的 10 条已有指令及其分数」写进该条记录的 `most_similar_instructions` 字段，把平均相似度写进 `avg_similarity_score`，方便事后人工抽查数据质量。

`0.7` 是一个经验阈值，与 Self-Instruct 系列数据生成流水线常用的取值一致。直观理解：对两条等长指令，\(F>0.7\) 意味着超过 70% 的词构成保序公共子序列——这是「换皮复述」的典型特征；而 RTL-Coder 每条指令都套着一段相同的「专业 Verilog 设计师」前缀（见 u2-l2），这段共享**套话**只会贡献十来个公共 token，相对于上百 token 的题目主体，不足以把两条**不同电路**的题目推过 0.7。所以 `0.7` 恰好能挡住「偷懒复述」，又放过「措辞不同但同属 Verilog 领域」的合法新题。

#### 4.3.2 核心流程

对算好的 `rouge_scores` 列表：

```text
1. most_similar = 取分数最高的 10 条已有指令及其分数（降序）
2. if max(rouge_scores) > 0.7:
       continue                      # 判为重复，丢弃，不计入结果
   else:
       keep += 1
3. 给这条记录挂上 most_similar_instructions 和 avg_similarity_score
4. 把它追加进结果列表 target_instruction_data
5. 把它的原文与切词结果分别追加进 all_instructions / all_instruction_tokens
   —— 这样后续新指令会和它也比一遍（基准集合是「不断增长的」）
```

第 5 步是关键设计：基准集合不是固定的，而是**每保留一条就长一条**。这样即使两条相似指令在相邻两轮被生成，第二条也会因为第一条已进入基准集合而被正确判重。

#### 4.3.3 源码精读

过滤与留痕逻辑（[instruction_gen.py:L142-L154](https://github.com/hkust-zhiyao/RTL-Coder/blob/b2847073be62d5f1d6d9b17bb247f0cfeb1ce642/data_generation/instruction_gen.py#L142-L154)）：

```python
most_similar_instructions = {
    all_instructions[i]: rouge_scores[i] for i in np.argsort(rouge_scores)[-10:][::-1]
}
if max(rouge_scores) > 0.7:
    continue
else:
    keep += 1
instruction_data_entry["most_similar_instructions"] = most_similar_instructions
instruction_data_entry["avg_similarity_score"] = float(np.mean(rouge_scores))
target_instruction_data.append(instruction_data_entry)
all_instructions.append(instruction_data_entry["Instruction"])
all_instruction_tokens.append(new_instruction_tokens)
progress_bar.update(1)
```

- 第 142–144 行：`np.argsort(rouge_scores)` 返回分数**升序**排列的下标；`[-10:]` 取最大的 10 个；`[::-1]` 反转成降序。用这 10 个下标从 `all_instructions` 里取出对应原文作 key、`rouge_scores` 里对应分数作 value，组成 `most_similar_instructions` 字典。当已有指令不足 10 条时，`[-10:]` 自动取全部，不会报错。
- 第 145–148 行：核心判定。`max(rouge_scores) > 0.7` 即「与最相似的已有指令 F 值超过 0.7」，`continue` 直接跳过本条，既不挂字段也不追加，等价于丢弃；否则 `keep += 1`。
- 第 149–150 行：给保留的记录挂两个审计字段。`most_similar_instructions` 是「最像的 10 条 + 分数」的字典；`avg_similarity_score` 是新指令对**全部**已有指令 F 值的均值（`np.mean`），反映它整体上有多「不新颖」。`float()` 是为了把 numpy 标量转成原生 float，便于后续 `save_json` 序列化。
- 第 151–154 行：把记录追加进结果列表，并把它原文、切词结果分别追加进基准集合 `all_instructions`、`all_instruction_tokens`，让基准集合增长，最后推进度条。

> 注意一个执行顺序细节：`most_similar_instructions` 字典在第 145 行判定**之前**就构造好了，但只有通过判定的记录（第 149 行）才会真正挂上它。被 `continue` 丢掉的记录不会进 `target_instruction_data`，自然也不会写盘。

#### 4.3.4 代码实践

**实践目标：** 复刻项目里「top-10 + 0.7 过滤」的判定，观察一批指令哪些会被丢、哪些会留。

**操作步骤（示例代码，非项目原有）：**

```python
import numpy as np
from rouge_score import rouge_scorer

scorer = rouge_scorer.RougeScorer(["rougeL"], use_stemmer=False)

# 模拟一个「已有基准集合」（先放两条不同的题）
pool = [
    "Design a full adder that takes three 1-bit inputs and produces a sum and a carry.",
    "Implement a 4-to-1 multiplexer with a 2-bit select signal.",
]
pool_tokens = [scorer._tokenizer.tokenize(x) for x in pool]

# 三条候选新指令：一条复述 full adder、一条全新、一条与自身比
candidates = [
    "Design a full adder taking three 1-bit inputs to produce sum and carry.",  # 近似复述
    "Build an arithmetic logic unit supporting add, subtract, and, or.",         # 全新
]

for c in candidates:
    c_tok = scorer._tokenizer.tokenize(c)
    scores = [rouge_scorer._score_lcs(c_tok, t).fmeasure for t in pool_tokens]
    top = {pool[i]: scores[i] for i in np.argsort(scores)[-10:][::-1]}
    verdict = "丢弃(重复)" if max(scores) > 0.7 else "保留"
    print(f"{verdict} | max={max(scores):.3f} | avg={np.mean(scores):.3f} | 最像: {list(top.items())[0]}")
```

**需要观察的现象：** 第一条候选与 full adder 高度重合，`max` 应超过 0.7 被判「丢弃」；第二条候选与两条基准都不像，`max` 远低于 0.7 被判「保留」。同时能看到 `top` 字典里最像的那条及其分数。

**预期结果：** 近似复述那条 `max` 明显偏高（具体是否恰好越过 0.7 视措辞而定，**待本地验证**）；全新那条 `max` 很低。这正好对应项目里 GPT「偷懒复述被挡、合法新题被留」的过滤行为。

#### 4.3.5 小练习与答案

**练习 1：** `np.argsort(rouge_scores)[-10:][::-1]` 如果去掉 `[::-1]`，会影响去重结果吗？会影响 `most_similar_instructions` 吗？
**参考答案：** 不影响去重结果——判定只依赖 `max(rouge_scores)`，与排序方向无关。但会影响 `most_similar_instructions` 的展示顺序：去掉 `[::-1]` 后字典里 10 条记录会按分数**升序**排列（最像的排最后），加上 `[::-1]` 才是**降序**（最像的排最前），便于人工优先看最相似的。

**练习 2：** 假设数据集已经保留了一条 full adder 题目。下一轮 GPT 又生成了一条几乎一样的 full adder，这条会被保留吗？为什么？
**参考答案：** 不会。因为上一条 full adder 已通过判定、被追加进了 `all_instructions` 与 `all_instruction_tokens`（第 152–153 行），基准集合增长了。新来的近似 full adder 与它算出的 `max(rouge_scores)` 会很高（接近 1），大于 0.7，于是被 `continue` 丢弃。这正体现了「基准集合不断增长、后续相似指令会被逐一挡掉」的设计。

## 5. 综合实践

把三个模块串起来，完成本讲规格要求的实践：**对 `data_generation/data_sample.json` 中的 10 条指令两两计算 ROUGE-L，找出最相似的指令对，并解释 0.7 阈值如何过滤近似重复。**

`data_sample.json` 是 JSONL（每行一个 JSON，见 u1-l4），共 10 条，每条有 `Instruction`/`Input`/`Response`。我们只取 `Instruction`。

**完整脚本（示例代码，保存为 `rouge_pairwise.py`，放在仓库根目录运行）：**

```python
import json
import numpy as np
from rouge_score import rouge_scorer

# 1) 读取 10 条指令的 Instruction 字段
instructions = []
with open("data_generation/data_sample.json", "r") as fh:
    for line in fh:
        line = line.strip()
        if line:
            instructions.append(json.loads(line)["Instruction"])
print(f"读入 {len(instructions)} 条指令")

# 2) 复刻项目的打分器与预切词
scorer = rouge_scorer.RougeScorer(["rougeL"], use_stemmer=False)
toks = [scorer._tokenizer.tokenize(x) for x in instructions]

# 3) 两两计算 ROUGE-L 的 fmeasure，找最相似的一对
best = (0.0, -1, -1)
mat = np.zeros((len(instructions), len(instructions)))
for i in range(len(instructions)):
    for j in range(i + 1, len(instructions)):
        f = rouge_scorer._score_lcs(toks[i], toks[j]).fmeasure
        mat[i, j] = mat[j, i] = f
        if f > best[0]:
            best = (f, i, j)

print(f"最相似指令对：第 {best[1]+1} 条 与 第 {best[2]+1} 条，ROUGE-L = {best[0]:.3f}")
print("全部两两分数的最大值 =", mat.max())

# 4) 阈值演示：指令与自身（必然 1.0 > 0.7 → 视为重复）
self_f = rouge_scorer._score_lcs(toks[0], toks[0]).fmeasure
print(f"指令 1 与自身 ROUGE-L = {self_f:.3f}（>0.7，若进入流水线会被判重复）")
print(f"指令 1 与指令 2 ROUGE-L = {mat[0,1]:.3f}（不同电路，应远低于 0.7）")
```

**操作步骤：**

1. 先 `pip install rouge_score numpy`（仓库 `requirements.txt` 未列这两个，需手动装）。
2. 将脚本放在仓库根目录运行 `python rouge_pairwise.py`。

**需要观察的现象与解释：**

- 「最相似指令对」的分数大概率**远低于 0.7**。原因：这 10 条虽都套着相同的「Please act as a professional Verilog designer …」前缀，但前缀只贡献十来个公共 token，而每条题目主体上百 token、讲的是不同电路（均衡器、乘法器、频率合成器、环形计数器……），保序公共子序列很短。
- 这恰好解释了 0.7 阈值的作用：**共享领域套话不足以触发过滤，只有「措辞高度重合的近似复述」才会越过 0.7 被丢掉**。所以即便所有指令长得「风格一致」，去重也不会误伤合法的多样性。
- 「指令与自身」恒为 1.0，演示了 F 值上界与「完全重复必被过滤」的极端情形。

**预期结果：** 两两最大分数较低（**待本地验证**具体数值），最相似对落在某两条共享套话最多/主题最接近的指令上；自身比对为 1.0。若把任意一条指令复制一份混进候选，它与原条目的分数会接近 1，超过 0.7——验证了过滤器的有效性。

## 6. 本讲小结

- 去重发生在主循环「解析响应之后、写盘之前」，只对 `Instruction`（自然语言题目）判重，不看 `Input` 代码骨架。
- `RougeScorer(["rougeL"], use_stemmer=False)` 创建一次、`use_stemmer=False` 要求词形完全一致才匹配，比较更严格。
- `_score_lcs` 内部跑最长公共子序列动态规划，单次比较 \(O(m\cdot n)\)；代码最终只用对称的 `fmeasure` 做判定。
- 判定规则就一句：`max(rouge_scores) > 0.7` 则丢弃，否则保留。
- 基准集合 `all_instructions` / `all_instruction_tokens` **每保留一条就增长一条**，保证后续近似指令会被正确挡掉。
- `most_similar_instructions`（top-10 降序）与 `avg_similarity_score` 是审计字段，便于事后抽查数据质量。

## 7. 下一步学习建议

本讲把数据生成流水线里最后一块「黑盒」打开。至此，`data_generation/` 目录下的去重机制已完整：

- 若你想看「去重后保留下来的指令最终如何参与训练」，请进入进阶层 u2-l6（训练方案总览与共享数据管线），那里开始讲 `train/` 目录。
- 若你对数据如何被消费成训练样本感兴趣，接着读 u2-l7（`mle.py` 标准监督微调），看 `{Instruction, Response}` 如何变成带 `-100` 掩码的 `labels`。
- 想横向加深 ROUGE 理解，可自行把 `["rougeL"]` 换成 `["rouge1","rouge2","rougeL"]`，对比三种指标在同一批指令上的差异，体会 LCS 对词序的敏感性。
