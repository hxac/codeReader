# 数据去污染（Decontamination）

> 本讲承接 [u4-l1 Distilabel 数据生成流水线](u4-l1-distilabel-pipeline.md)。蒸馏/采集来的数据在进入训练和评估之前，必须先剔除那些与公开基准测试集（AIME、MATH-500、GPQA、LiveCodeBench）雷同的样本——否则模型在评估时会因为「背过答案」而虚高，分数不可信。`scripts/decontaminate.py` 正是干这件事的独立命令行工具。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清「数据去污染」要解决什么问题，以及为什么用 **8-gram 重叠** 作为判定标准。
- 读懂 `scripts/decontaminate.py` 里的四个核心函数：`normalize_string`、`word_ngrams`、`build_ngram_lookup`、`build_ngram_single`。
- 理解主流程如何为五个基准集分别建 n-gram 倒排索引，再用 `find_contaminated` 给每条训练样本打上 `contaminated_<基准名>` 标注列。
- 掌握 `--cleanup` 如何删掉受污染行并把结果 `push_to_hub` 成新数据集。
- 在不联网的情况下，用脚本里的真实函数复现一遍「判定 + 清理」逻辑。

## 2. 前置知识

本讲只依赖两个概念，先通俗解释清楚。

**基准污染（benchmark contamination）**。当你拿一份公开题库（比如 MATH-500 的 500 道题）去评估模型时，前提是模型在训练时**没见过**这些题。如果训练数据里混进了和题库几乎一字不差的原文，模型就会靠「记忆」而非「推理」作答，评估分数被人为抬高。去污染就是**在训练前把这些雷同样本挑出来删掉**。

**n-gram（n 元语法）**。把一段文本按词切开后，每相邻 *n* 个词组成一个片段，就叫一个 n-gram。例如 "the quick brown fox jumps" 的 3-gram 有 "the quick brown"、"quick brown fox"、"brown fox jumps"。去污染用 **8-gram**：连续 8 个词完全相同，几乎不可能是巧合，强烈提示这两段文本同源。n 太小（如 3）会因为常见短语误报太多；n 太大（如 13）会漏掉那些「改了几个字」的近似重复；8 是社区（s1 论文）常用的折中值。

> 本讲出现的「基准集」一词，指被当作「答案不能泄漏」的评估数据集；「训练样本」指我们要检查/清洗的目标数据集。

## 3. 本讲源码地图

本讲只涉及**一个文件**，这也是 open-r1 里少数几个放在 `scripts/`（独立命令行工具）而非 `src/open_r1/`（可 import 的包）里的脚本：

| 文件 | 作用 |
| --- | --- |
| [scripts/decontaminate.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/scripts/decontaminate.py) | 用 8-gram 检查一个数据集与五个基准集的重叠，给每行打 `contaminated_*` 标注，可选删除受污染行并推送到 Hub |

整个脚本的结构很清晰，自上而下分四块：

1. **工具函数**：`normalize_string`、`word_ngrams`（文本预处理与切分）。
2. **索引函数**：`build_ngram_lookup`（给一批文档建倒排索引）、`build_ngram_single`（给单条文档算 n-gram 集合）。
3. **主流程 `if __name__ == "__main__"`**：解析命令行参数 → 加载目标数据集 → 为五个基准集建索引 → 用 `find_contaminated` 逐行打标注。
4. **清理与推送**：`cleanup` 删除受污染行 → `push_to_hub` 上传新数据集。

这也是本讲第 4 节四个最小模块的划分依据。

## 4. 核心概念与源码讲解

### 4.1 文本归一化与 n-gram 切分（normalize_string / word_ngrams）

#### 4.1.1 概念说明

两段文本要比较「是否共享某个 8-gram」，必须先把它们**标准化**到同一种形式：大小写、多余空白、换行符的差异都不能影响判定。这就是 `normalize_string` 的职责。标准化之后，再用 `word_ngrams` 把文本切成连续 8 词片段。这两个函数是后续所有判定的地基。

#### 4.1.2 核心流程

`normalize_string` 做两步：

1. `text.lower().strip()`：全转小写、去掉首尾空白。
2. `" ".join(text.split())`：`split()` 不带参数时会按**任意空白**（空格、制表符、连续空格、换行）切分，再用单个空格拼回。这一步把所有「多空格 / 换行 / 制表符」都压成单个空格。

`word_ngrams(text, n)` 则是滑动窗口：

```
words = text.split()                      # 按空格切成词列表
对每个起点 i ∈ [0, len(words)-n]：
    取 words[i : i+n]，用空格拼成一个 n-gram 字符串
返回所有 n-gram 组成的列表
```

注意一个**边界情况**：当 `len(words) < n` 时，`range(len(words) - n + 1)` 是空的，返回空列表 `[]`。这意味着**很短（不足 8 个词）的样本天然无法被判定为污染**——它根本没有 8-gram 可比。

#### 4.1.3 源码精读

归一化函数：

[scripts/decontaminate.py:36-42](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/scripts/decontaminate.py#L36-L42) —— 把文本转小写并压缩所有空白为单个空格，是后续比较的统一前置处理。

n-gram 切分函数：

[scripts/decontaminate.py:45-48](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/scripts/decontaminate.py#L45-L48) —— 用切片 `words[i:i+n]` 生成词级 n-gram，返回的是**列表**（保留重复与顺序）。

#### 4.1.4 代码实践

这是一个不需要联网的快速验证。直接 `import` 脚本里的真实函数，看归一化和切分的效果：

```python
# 示例代码：观察 normalize_string 与 word_ngrams 的行为
import importlib.util
spec = importlib.util.spec_from_file_location(
    "decon", "<仓库根目录>/scripts/decontaminate.py"
)
decon = importlib.util.module_from_spec(spec)
spec.loader.exec_module(decon)

raw = "Find   ALL\treal\nsolutions to the equation"
print(decon.normalize_string(raw))
# 预期：'find all real solutions to the equation'（大小写归一、空白压平）

print(decon.word_ngrams("a b c d e", 3))
# 预期：['a b c', 'b c d', 'c d e']

print(decon.word_ngrams("too short", 8))
# 预期：[]（不足 8 个词，无法产生 8-gram）
```

需要观察：把 `<仓库根目录>` 换成本地仓库实际路径；带 `\t`、`\n`、多空格的输入是否被压平成单空格小写；不足 8 词时是否返回空列表。**此实践可离线运行，预期结果如注释所示。**

#### 4.1.5 小练习与答案

**练习 1**：`word_ngrams("one two three four", 4)` 返回什么？为什么只有一个元素？

> **答案**：返回 `['one two three four']`。因为 4 个词只有 1 个起点（`i=0`），`range(4-4+1)=range(1)` 只含 0。

**练习 2**：若不先 `normalize_string`，"Hello world" 与 "hello   world" 会被判为共享同一个 2-gram 吗？

> **答案**：不会。前者 2-gram 是 `"Hello world"`，后者经 split 后是 `"hello world"`，大小写不同导致字典键不一致。必须先归一化才能正确匹配。

---

### 4.2 为基准集建 n-gram 倒排索引（build_ngram_lookup / build_ngram_single）

#### 4.2.1 概念说明

要把「训练样本里有没有 8-gram 出现在某个基准集里」这个问题算得快，不能每来一条训练样本就和基准集逐条比对。正确做法是**预处理**：先把整个基准集的所有 8-gram 收集进一个字典（倒排索引），之后每条训练样本只需查「我的 8-gram 在不在这个字典的键里」。`build_ngram_lookup` 负责建索引，`build_ngram_single` 负责为单条训练样本算出它的 8-gram 集合。

#### 4.2.2 核心流程

先给出去污染的形式化定义，便于理解后面代码。

设基准集为 \(B=\{b_1,b_2,\dots,b_m\}\)（例如 MATH-500 的 500 道题），待检查的训练样本为 \(r\)。记归一化后文本 \(x\) 的所有词级 \(n\)-gram 集合为：

\[
N_n(x)=\{\,\text{word}_i\,\dots\,\text{word}_{i+n-1}\mid i=1,\dots,|x|-n+1\,\}
\]

基准集的 \(n\)-gram 全集为 \(U_n(B)=\bigcup_{j} N_n(b_j)\)。样本 \(r\) 被判为「污染」当且仅当：

\[
N_n(r)\cap U_n(B)\neq\varnothing
\]

这是一个**单向、只要有一处重叠就判污染**的保守判定。

`build_ngram_lookup(documents, ngram_size)` 的流程：

```
lookup = defaultdict(set)            # n-gram -> 这条 8-gram 出现在哪些基准文档里
for 每条基准文档 document（带序号 doc_id）：
    text = normalize_string(document)
    for 每个 8-gram in word_ngrams(text, ngram_size)：
        lookup[8-gram].add(doc_id)   # 记录该 8-gram 来自第几条基准题
return lookup
```

`build_ngram_single(document, ngram_size)` 更简单：归一化 → 切 8-gram → 转成 `set` 返回（去重，只关心「有没有」）。

**复杂度**：建索引的开销是 \(O(W_B)\)（\(W_B\) 为基准集总词数，每个词进入一个 8-gram）；查询单条样本是 \(O(|r|-n+1)\) 次字典查找，每次 \(O(1)\)。配合 `num_proc=8` 并行，可处理大数据集。

#### 4.2.3 源码精读

倒排索引构建：

[scripts/decontaminate.py:51-61](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/scripts/decontaminate.py#L51-L61) —— 遍历每条基准文档，把它的所有 8-gram 存入字典，值是「出现过该 8-gram 的基准文档序号集合」。

单条样本的 8-gram 集合：

[scripts/decontaminate.py:64-68](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/scripts/decontaminate.py#L64-L68) —— 对单条训练样本归一化、切 8-gram 并转 `set`，供后续做成员判定。

> **阅读笔记（值得注意）**：`build_ngram_lookup` 的值是「基准文档序号集合」`set[int]`，它**记录了每个 8-gram 来自哪道基准题**。但后面 `find_contaminated` 只用了字典的**键存在性**（`ngram in ngram_lookup`），并没有用到序号集合。也就是说，这套序号信息目前只是「建了但没被消费」——如果你想扩展脚本，让它报告「这条样本究竟和哪道基准题撞了」，这个集合正好现成可用。

#### 4.2.4 代码实践

仍是离线实践：用一个迷你基准集体验「建索引 → 查询」的完整链路。

```python
# 示例代码：建索引并查询
benchmark = [
    "Find all real solutions to the equation x squared plus two x minus three equals zero."
]
lookup = decon.build_ngram_lookup(benchmark, ngram_size=8)
print("索引里有几个 8-gram 键：", len(lookup))

# 一条与基准集有 8 个连续词重叠的训练样本
hit = "Please find all real solutions to the equation x carefully."
grams = decon.build_ngram_single(hit, ngram_size=8)
print("是否命中基准集：", any(g in lookup for g in grams))   # 预期 True
```

需要观察：索引键的数量；含 "find all real solutions to the equation x" 这 8 个连续词的样本是否被命中。**此实践可离线运行。**

#### 4.2.5 小练习与答案

**练习 1**：基准集有 500 道题，每题平均 60 个词。`build_ngram_lookup` 大约会向字典里塞多少个 8-gram 键（忽略重复）？

> **答案**：每题约 \(60-8+1=53\) 个 8-gram，500 题约 \(500\times53=26500\) 个（实际会更少，因为重复的 8-gram 会合并到同一个键、其值集合追加 doc_id）。

**练习 2**：为什么 `build_ngram_single` 要返回 `set` 而 `word_ngrams` 返回 `list`？

> **答案**：建索引时需要保留每个出现位置（虽然这里只存 doc_id），`word_ngrams` 保持通用；而查询单条样本时只关心「这个 8-gram 出没出现过」，去重后用集合做成员判定更高效，也避免重复查询同一个 8-gram。

---

### 4.3 逐行污染判定与标注列注入（find_contaminated）

#### 4.3.1 概念说明

有了每个基准集的 8-gram 索引后，下一步是给目标数据集的**每一行**算出一个布尔标注：这条样本有没有和这个基准集撞上。open-r1 的做法是用 `datasets.map` 给数据集**新增一列** `contaminated_<基准名>`，这样你可以先「只看标注」再决定要不要删。本模块讲清这套标注是怎么打上去的，以及它一次只针对一个基准集。

#### 4.3.2 核心流程

主流程先固定要对比的五个基准集及其「题目列名」（各基准集的列名并不统一）：

| 键名 | 数据集 | 题目列名 |
| --- | --- | --- |
| `aime_2024` | `HuggingFaceH4/aime_2024` (train) | `problem` |
| `aime_2025` | `yentinglin/aime_2025` (train) | `problem` |
| `math_500` | `HuggingFaceH4/MATH-500` (test) | `problem` |
| `gpqa` | `Idavidrein/gpqa` (`gpqa_diamond`, trust_remote_code) | `Question` |
| `lcb` | `livecodebench/code_generation_lite` (`v4_v5`, trust_remote_code) | `question_content` |

接着对每个基准集：

```
1. 用 build_ngram_lookup 为该基准集的题目列建 8-gram 索引
2. 定义 find_contaminated(row)：
       ngrams = build_ngram_single(row[problem_column])      # 这条训练样本的 8-gram 集合
       row["contaminated_<基准名>"] = 这些 8-gram 里有没有任意一个出现在索引键里
3. ds.map(find_contaminated, num_proc=8)                     # 并行打标注
```

五个基准集各跑一遍，最终数据集会多出五列：`contaminated_aime_2024`、`contaminated_aime_2025`、`contaminated_math_500`、`contaminated_gpqa`、`contaminated_lcb`。

#### 4.3.3 源码精读

五个基准集的加载与列名（注意列名差异：`problem` / `Question` / `question_content`）：

[scripts/decontaminate.py:100-111](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/scripts/decontaminate.py#L100-L111) —— 把每个基准集与其题目列名组成字典；GPQA 与 LiveCodeBench 因需要执行远程代码而带 `trust_remote_code=True`。

为每个基准集建索引：

[scripts/decontaminate.py:112-114](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/scripts/decontaminate.py#L112-L114) —— 遍历 `eval_datasets`，用各自的题目列构建 8-gram 索引，存入 `ngram_lookups`。

逐行打标注的核心：

[scripts/decontaminate.py:116-124](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/scripts/decontaminate.py#L116-L124) —— `find_contaminated` 给每行新增一列 `contaminated_<基准名>`，并用 `num_proc=8` 并行执行 `ds.map`。

> **阅读笔记（两个易踩坑点）**：
>
> 1. **第 121 行的 `any(set(...))` 是冗余写法**。`ngram in ngram_lookup` 本身已是布尔值，外层 `set(...)` 把一串布尔包成集合再交给 `any`，结果与直接写 `any(ngram in ngram_lookup for ngram in ngrams)` 完全等价；而且 `set(...)` 会先把整个生成器物化、**反而破坏了 `any` 的短路求值**（找到一个 True 就能停）。功能正确，但略多余。
> 2. **闭包捕获循环变量**。`find_contaminated` 定义在 `for eval_name, ngram_lookup in ...` 循环内部，引用了 `eval_name` 和 `ngram_lookup`。因为同一次循环里立刻 `ds.map(find_contaminated)` 把它消费掉了，所以不存在 Python 常见的「闭包延迟绑定」bug；但若有人改写成「先收集所有函数最后统一调用」，就会所有列都写成最后一个基准名。重构时务必当心。

#### 4.3.4 代码实践

用 `datasets.Dataset` 复现「打标注列」的效果（离线，迷你基准集）：

```python
# 示例代码：复现 find_contaminated 的列注入
from datasets import Dataset

benchmark = [
    "Find all real solutions to the equation x squared plus two x minus three equals zero."
]
lookup = decon.build_ngram_lookup(benchmark, ngram_size=8)

rows = [
    {"problem": "Please find all real solutions to the equation x carefully."},  # 含 8-gram 重叠
    {"problem": "Solve the quadratic by completing the square step by step."},   # 干净
]
ds = Dataset.from_list(rows)

def find_contaminated(row):
    ngrams = decon.build_ngram_single(row["problem"], ngram_size=8)
    row["contaminated_bench"] = any(g in lookup for g in ngrams)   # 注意：直接用 any，省去冗余的 set()
    return row

ds = ds.map(find_contaminated)
print(ds[:])
# 预期：contaminated_bench 列为 [True, False]
```

需要观察：新增的 `contaminated_bench` 列是否第一条为 `True`、第二条为 `False`。**此实践可离线运行。**

#### 4.3.5 小练习与答案

**练习 1**：脚本默认 `--problem_column problem`，但 GPQA 的题目列叫 `Question`。这个差异会影响去污染吗？

> **答案**：不影响。`--problem_column` 指的是**目标数据集**里题目的列名；而各基准集的列名（`Question` 等）是脚本内部在 `eval_datasets` 里写死的，与命令行参数无关。两者各自独立指定。

**练习 2**：一条训练样本同时和 MATH-500、AIME 都撞了 8-gram，它的 `contaminated_*` 列会是什么样？

> **答案**：`contaminated_math_500=True` 且 `contaminated_aime_2024=True`（可能还有别的列也为 True）。每个基准集独立打标，同一行可以同时命中多个。

---

### 4.4 清理受污染行并推送 Hub（cleanup / push_to_hub）

#### 4.4.1 概念说明

打完五列标注后，数据集只是「被标记」，受污染样本仍在里面。`--cleanup` 选项负责**真正删掉**这些样本，并把五列标注一并去掉，最后把干净的数据集推送到 Hugging Face Hub 成一个新数据集。注意：默认会**无条件推送**到 Hub（即便不加 `--cleanup`，它也会把「带标注列」的数据集推上去），这是脚本设计的最后一步。

#### 4.4.2 核心流程

`cleanup(dataset)` 的流程：

```
记下 initial_size
找出所有以 "contaminated_" 开头的列
for 每个污染列 col：
    用 filter 保留「该列为 False」的行（即删掉该基准判定的污染行）
    若有删除，打印 "Removed N samples from '<基准名>'"
删掉所有 contaminated_* 列
打印 initial_size / final_size
```

随后主流程：

```
new_ds_name = --new_dataset_name 或 "{原数据集名}_decontaminated"
config_name = --config 或 "default"
url = ds.push_to_hub(new_ds_name, config_name=config_name, split="train")
打印 url
```

#### 4.4.3 源码精读

清理函数：

[scripts/decontaminate.py:127-138](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/scripts/decontaminate.py#L127-L138) —— 逐列 `filter` 删除受污染行并打印删除数，最后 `remove_columns` 去掉所有标注列。

按需清理、命名与推送：

[scripts/decontaminate.py:140-146](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/scripts/decontaminate.py#L140-L146) —— 仅当带 `--cleanup` 才删除行；新数据集名缺省时自动追加 `_decontaminated` 后缀；最终 `push_to_hub`（默认 split 为 `train`）。

> **阅读笔记**：
>
> - 清理是**按列串行 filter** 的：先按 `contaminated_aime_2024` 删一轮，剩下的再按 `contaminated_math_500` 删一轮……由于 filter 只会缩小集合，顺序不影响最终结果，但每一步都会打印该轮删了多少。
> - README 给的样例命令里数据集是 `open-r1/verifiable-coding-problems-python`，对应题目列恰好叫 `problem`，所以样例没传 `--problem_column`；若你的数据集题目列叫别的名字，必须显式传。
> - 脚本结尾**总是** `push_to_hub`，所以即使只想本地看看标注、不想上传，也不能直接照跑——需要改脚本或参考第 5 节的离线实践。

#### 4.4.4 代码实践

离线复现 `cleanup` 的删除逻辑（不推送 Hub）：

```python
# 示例代码：复现 cleanup 的过滤（沿用 4.3.4 得到的 ds）
initial_size = len(ds)
contamination_cols = [c for c in ds.column_names if c.startswith("contaminated_")]
for col in contamination_cols:
    size_prior = len(ds)
    ds = ds.filter(lambda x: not x[col], num_proc=8)
    if len(ds) < size_prior:
        print(f"Removed {size_prior - len(ds)} samples from '{col}'")
ds = ds.remove_columns(contamination_cols)
print(f"Initial size: {initial_size}, Final size: {len(ds)}")
# 预期：Removed 1 samples from 'contaminated_bench'；Initial size: 2, Final size: 1
```

需要观察：受污染的那一条是否被删掉、最终只剩 1 条干净样本、标注列是否被移除。**此实践可离线运行。**

> **完整脚本运行（待本地验证）**：在能联网且已配置 Hugging Face token 的环境里，按 README 执行：
> ```shell
> python scripts/decontaminate.py \
>     --dataset open-r1/verifiable-coding-problems-python \
>     --problem_column problem \
>     --cleanup
> ```
> 脚本会自动下载五个基准集（GPQA/LCB 需 `trust_remote_code`）、建索引、打标注、删除受污染行，并把结果推到 `open-r1/verifiable-coding-problems-python_decontaminated`。因需要网络与 Hub 写权限，结果以你本地实际运行为准。

#### 4.4.5 小练习与答案

**练习 1**：不加 `--cleanup` 直接运行脚本，会发生什么？

> **答案**：脚本仍会为五列标注完成，**不会删除任何行**，但最后照样 `push_to_hub`，推上去的是一个「带着五个 `contaminated_*` 列」的数据集（默认名带 `_decontaminated` 后缀）。即「只标注、不清洗」。

**练习 2**：为什么 cleanup 用「逐列 filter」而不是「一次 filter 掉任一列为 True 的行」？

> **答案**：两者最终留下的样本集相同（都是「所有列都为 False」的行）。逐列写法的好处是能在日志里**分基准报告每轮删了多少**，便于排查哪个基准贡献了最多污染。功能等价，区别只在可观测性。

---

## 5. 综合实践

把本讲四个模块串起来：**构造一个含 8-gram 重叠的迷你数据集 → 用脚本真实函数打标注 → 清理 → 打印前后规模**。全程离线，无需下载真实基准集，用一条合成「基准题」代替。

```python
# 示例代码：综合实践——离线版去污染全流程
import importlib.util
from datasets import Dataset

# 1) 从仓库真实脚本加载函数（不触发 __main__ 里的 argparse）
spec = importlib.util.spec_from_file_location(
    "decon", "<仓库根目录>/scripts/decontaminate.py"
)
decon = importlib.util.module_from_spec(spec)
spec.loader.exec_module(decon)

# 2) 合成一个「基准集」：只有一道题（替代真实 MATH-500/AIME）
benchmark = [
    "Find all real solutions to the equation x squared plus two x minus three equals zero."
]
lookup = decon.build_ngram_lookup(benchmark, ngram_size=8)

# 3) 目标数据集：3 条，其中 2 条与基准题有 8 词连续重叠，1 条干净
target = Dataset.from_list([
    {"problem": "Please find all real solutions to the equation x carefully."},          # 命中
    {"problem": "We must find all real solutions to the equation x today."},             # 命中
    {"problem": "Derive the quadratic formula from first principles now."},              # 干净
])

# 4) 打标注（等价于 find_contaminated，去掉冗余 set()）
def find_contaminated(row):
    ngrams = decon.build_ngram_single(row["problem"], ngram_size=8)
    row["contaminated_bench"] = any(g in lookup for g in ngrams)
    return row
target = target.map(find_contaminated)
print("标注结果：", target["contaminated_bench"])   # 预期 [True, True, False]

# 5) 清理（等价于 cleanup）
initial_size = len(target)
cols = [c for c in target.column_names if c.startswith("contaminated_")]
for col in cols:
    size_prior = len(target)
    target = target.filter(lambda x: not x[col])
    if len(target) < size_prior:
        print(f"Removed {size_prior - len(target)} samples from '{col}'")
target = target.remove_columns(cols)
print(f"Initial size: {initial_size}, Final size: {len(target)}")   # 预期 Initial 3, Final 1
```

**操作步骤**：

1. 把 `<仓库根目录>` 替换为本机仓库路径。
2. `pip install datasets`（若未装）。
3. 运行脚本。

**需要观察的现象**：

- 标注列 `[True, True, False]`——两条含 "find all real solutions to the equation x" 这 8 个连续词的样本被判污染。
- 日志打印 `Removed 2 samples`，最终 `Initial size: 3, Final size: 1`。
- 清理后数据集不再有 `contaminated_*` 列，且只剩那条干净样本。

**预期结果**：受污染的 2 条被删除，留下 1 条干净样本。若想进一步验证「短样本不可判」，可把第 3 条改成不足 8 个词（如 "Derive it."），它同样会得 `False`——印证 4.1.2 提到的边界情况。**此综合实践可离线运行，预期结果如上。**

## 6. 本讲小结

- **去污染的动机**：训练数据若与评估基准集雷同，模型会靠记忆作答、分数虚高；`scripts/decontaminate.py` 在训练前用 **8-gram 重叠**剔除这类样本，方法是 s1 论文方案的直接移植。
- **判定原理**：把基准集所有 8-gram 收进倒排索引，训练样本只要**有任意一个 8-gram 命中索引键**即判为污染（\(N_n(r)\cap U_n(B)\neq\varnothing\)），是单向保守判定。
- **四个函数的分工**：`normalize_string` 归一化、`word_ngrams` 切分；`build_ngram_lookup` 建索引、`build_ngram_single` 算单条 8-gram 集合。
- **主流程**：为 `aime_2024 / aime_2025 / math_500 / gpqa / lcb` 五个基准集各建索引，用 `find_contaminated` 给每行注入 `contaminated_<基准名>` 列。
- **清理与推送**：`--cleanup` 逐列 filter 删掉受污染行并移除标注列，最终**总是** `push_to_hub`，新名缺省时加 `_decontaminated` 后缀。
- **两个易踩坑点**：第 121 行 `any(set(...))` 的冗余写法；以及 `build_ngram_lookup` 里「基准文档序号集合」目前建了但未被消费——这正是你想扩展「报告撞了哪道题」时的现成抓手。

## 7. 下一步学习建议

- **回到训练链路**：去污染产出的 `_decontaminated` 数据集（如 `open-r1/verifiable-coding-problems-python_decontaminated`）会被 [u2-l2 数据集加载与混合](u2-l2-dataset-loading.md) 的 `get_dataset` / `dataset_mixture` 当作普通数据集加载。可回头验证：去污染后的数据集能否被 `dataset_name` 分支正常加载。
- **关注实际用法**：仓库里 `recipes/dataset_filtering/filter_python.yaml` 与 `recipes/OlympicCoder-*/sft/config_v00.00.yaml`（`dataset_config: solutions_decontaminated`）都直接消费了去污染产物，可作为「去污染在真实配方里如何被引用」的范例。
- **进阶练习**：尝试修改脚本，利用 `build_ngram_lookup` 里未被消费的 `set[int]` 值，让 `find_contaminated` 额外记录「撞上了第几道基准题」，便于人工复核误报。
- **下一讲方向**：本单元（数据生成与清洗）到此结束。后续可进入 [u5 代码奖励与沙箱执行](u5-l1-code-reward-template.md)，看这些清洗过的编程题数据如何配合 `code_reward` 在沙箱里跑测试用例打分。
